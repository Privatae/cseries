"""What keeps a turn open and what wakes one: the runaway backstops, holds on tasks the turn started,
a turn's stop and its unread results, live turns and answers written into them, woken turns.

Moved out of runtime/session.py, which re-exports every name defined here."""

import asyncio
import os
import logging
import re
from typing import Optional
from mastercode.runtime.turn_input import ASK_POLL_S, _nudge_ids, _nudge_keeps_copy, _nudge_keys, _nudge_tags, consume_nudges, has_pending_input, push_nudge, push_turn_event
from mastercode.runtime.turn_lanes import wakeup_delivery_room

log = logging.getLogger("cseries.mastercode.entity_runtime")


# task_id -> (session_id, the turn's asyncio task). A hold is LIVE while that
# task runs and the turn has not released it at its exit, so a turn that died
# (a crash, a cancel, an early return) can never swallow a result: its task is
# done, the hold reads dead, and the result wakes a real turn instead.
_task_holds: dict[int, tuple[str, "asyncio.Task | None"]] = {}
# What each held task is, in a few words — the waiting card names it.
_task_labels: dict[int, str] = {}
# Sessions of a person's turn waiting at their exit right now — what a hold
# costs the process. A woken turn that holds is counted once, as a woken turn.
_holding_now: set[str] = set()
# Woken turns running now (_wake_host_turn), and how many have run in each
# thread since the person last wrote there.
_woken_running = 0
_woken_since_person: dict[str, int] = {}
# A chain of woken turns with nobody watching: each result wakes a turn that
# may start more work, whose result wakes another. A plan bounds itself (its
# phases are finite, auto-advance has its own backstop) and a phase's repairs
# are bounded at the build door (tools.PHASE_AUTO_REPAIRS); anything else is
# bounded here, per thread, counted since the person's last message. The turn
# past it still wakes — the result is reported by the Host — but it starts no
# work: it may only ask, in the question box.
#
# A RUNAWAY BACKSTOP, NOT A WORKFLOW LIMIT. At 3 — exactly the longest
# legitimate chain seen (dispatch -> result -> a follow-up agent or fix pass
# -> its result -> a report) — the first longer piece of continuous work hit
# it and the Host had to ask for a go-ahead, which a Host must never end a
# turn asking for.
# Money is bounded by the turn allowance, and every woken turn has
# its own round backstop. 40 is over 13x the longest real chain seen; like
# the round backstop it is set by env (CSERIES_WOKEN_TURNS_MAX) and <= 0
# disables it.
def _backstop_env(name: str, default: int) -> int:
    """A runaway backstop's value: the env override when set (<= 0 disables
    it — a number no chain reaches), else `default`."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        logging.getLogger("cseries.mastercode.entity_runtime").warning(
            "%s=%r is not an integer — using %d", name, raw, default)
        return default
    return 10**9 if n <= 0 else n


WOKEN_TURNS_MAX = _backstop_env("CSERIES_WOKEN_TURNS_MAX", 40)
# Turns woken by a watched MCP call (runtime/mcp_watch.py) in each thread since
# the person last wrote. A game moves on by one woken turn per move, so such a
# turn is the work advancing, like a plan's phase landing, and is not counted
# in the chain above. This is its own runaway backstop: a watch only wakes a
# turn when the server's answer changed after at least one "nothing yet", so
# what it guards against is a server that flips its answer to keep a Host
# busy. A board game's moves are bounded by its board (a Reversi player makes
# at most 30, a Four in a Row player 21), so 500 is many whole games in a row
# with nobody writing to the Host; env CSERIES_WATCH_WAKES_MAX, <= 0 disables
# it. Past it the woken turn still reports, and starts nothing.
WATCH_WAKES_MAX = _backstop_env("CSERIES_WATCH_WAKES_MAX", 500)
_watch_wakes: dict[str, int] = {}
# Research rounds (a project with no plan: continue_after_round) in each thread
# since the person last wrote — EVERY round counts, whatever its agents'
# status: an agent that answers "I couldn't access the docs" is
# `completed` (agent_dispatch records any non-empty answer as completed), so
# status cannot tell progress apart. A HIGH backstop, never a budget: research
# takes one to three rounds (gather, then follow up what the first round
# turned up, now and then a third to settle a conflict). What steers first is the outcome fed back in each round's
# brief (which agents failed, which round this is). At the backstop the
# continuation starts no new work and puts what came back so far in the
# question box. A plan's phases are bounded by tools.PHASE_AUTO_REPAIRS
# instead; the person's message starts this count again.
# 15 (5 sat under 2x the normal three): five
# times the longest normal research; env CSERIES_RESEARCH_ROUNDS_MAX, <= 0
# disables it.
RESEARCH_ROUNDS_MAX = _backstop_env("CSERIES_RESEARCH_ROUNDS_MAX", 15)

# The consecutive all-failed-rounds breaker in the tool loop. At 3, with a
# message calling the tools "currently unavailable" whatever the errors
# were, it was a 3-round workflow stop.
# Each failed call's own words are in its tool result (tool_failures.py's
# per-tool advice runs only on a Host whose steering is on, runtime/
# steering.py, off by default); this is the runaway stop behind them: 25
# rounds in a
# row where every call failed is over 3x the longest run of back-to-back
# failing calls observed (7), and it stops well before the 200-round
# backstop. env CSERIES_ALL_FAIL_ROUNDS_MAX, <= 0 disables it.
ALL_FAIL_BACKSTOP_ROUNDS = _backstop_env("CSERIES_ALL_FAIL_ROUNDS_MAX", 25)

# The constants above are the DEFAULTS (with the operator's env). Each is one
# of the person's limits (runtime/user_limits): their value, when
# they set one, wins, and is read at the moment it is enforced.
def _woken_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("thread.woken_turns", WOKEN_TURNS_MAX)


def _research_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("thread.research_rounds", RESEARCH_ROUNDS_MAX)


def _watch_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("thread.watch_wakes", WATCH_WAKES_MAX)


def _all_fail_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("turn.all_fail_rounds", ALL_FAIL_BACKSTOP_ROUNDS)


def _failed_wakes_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("thread.failed_wakes", FAILED_WAKES_MAX)


def _limit_line(name: str, thread_key=None, what: str = "") -> str:
    """user_limits.hit_line for `name`, noting the hit for Settings > Limits."""
    from mastercode.runtime import user_limits
    try:
        from server import brain_service as _bs
        user_limits.note_hit(_bs._entity_id, name, what or name, thread_key)
    except Exception:  # noqa: BLE001
        pass
    return user_limits.hit_line(name)


_research_rounds: dict[str, int] = {}


def _hold_live(h) -> bool:
    return bool(h) and (h[1] is None or not h[1].done())


def hold_task(task_id, session_id: str, label: str | None = None) -> None:
    """This turn started `task_id`: its result comes back into the turn while
    the turn runs."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return
    if session_id:
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        _task_holds[tid] = (session_id, cur)
        _task_labels[tid] = (label or f"task {tid}")[:120]


def drop_hold(task_id) -> None:
    """`task_id` was stopped on purpose and delivers nothing (a watched MCP
    call stopped by unwatch or the person's Kill, runtime/mcp_watch.py): a
    turn holding for it stops waiting at hold_for's next look, instead of
    waiting out HOLD_S."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return
    _task_holds.pop(tid, None)
    _task_labels.pop(tid, None)


def waiting_on(asks: list[int], tasks: list[int]) -> str:
    """What a holding turn waits on, in words for its waiting card."""
    parts = [f"your answer to ticket #{t} in the question box" for t in (asks or [])]
    parts += [_task_labels.get(int(t)) or f"task {t}" for t in (tasks or [])]
    return "; ".join(parts) or "nothing"


def held_tasks(session_id: str) -> list[int]:
    """The tasks this session started whose results have not come back."""
    return sorted(t for t, h in list(_task_holds.items())
                  if h[0] == session_id and _hold_live(h))


def release_holds(session_id: str) -> None:
    """The turn is ending: whatever it started and has not heard back from
    now wakes a turn of its own when it lands. From here it reads no more
    input (turn_stopped_reading) until it goes round its loop again
    (mark_turn_live)."""
    for t in [t for t, h in list(_task_holds.items()) if h[0] == session_id or not _hold_live(h)]:
        _task_holds.pop(t, None)
        _task_labels.pop(t, None)
    _live_turns.pop(session_id, None)
    for sid in [k for k, v in list(_live_turns.items()) if not _hold_live((k, v))]:
        _live_turns.pop(sid, None)
    if session_id:
        _stopped_reading[session_id] = None
        while len(_stopped_reading) > _STOPPED_READING_MAX:
            _stopped_reading.pop(next(iter(_stopped_reading)), None)


# Sessions whose turn has stopped reading input: release_holds
# marks one; a new turn on that session clears it where it STARTS
# (turn_started: send_message's entry, and the turn registry when it sees the
# turn's session frame — a durable session keeps one id
# across turns, so the mark of the turn before must not outlive it), and
# mark_turn_live — the top of the loop, where it drains — clears it for a
# hold that resumes. A message pushed into such a session would sit in a queue
# nobody drains, so the turn registry and the nudge route start it as a turn
# of its own once this one is done (turn_registry.start_or_attach,
# routes/circles.nudge). Not turn_is_live: that is also False before a turn
# first marks itself live, while its first model call runs — a message then
# is still read, at the loop's first drain. Insertion-ordered and capped,
# oldest first: a chat session id is per turn, so its entry is never read
# again once its turn is over (a durable one's is cleared by its next turn).
_stopped_reading: dict[str, None] = {}
_STOPPED_READING_MAX = 1024


def turn_stopped_reading(session_id: str) -> bool:
    """The turn of `session_id` reads no more input: it released its holds
    and has not gone round its loop since."""
    return bool(session_id) and session_id in _stopped_reading


def turn_started(session_id: str) -> None:
    """A turn of `session_id` has started: it reads input from here, whatever
    the turn before it on the same session (a durable session — voice, demo,
    `session|<sid>` threads — keeps one id across turns) marked at its exit."""
    if session_id:
        _stopped_reading.pop(session_id, None)


# Words a turn ended without reading that are the server's to deliver (not
# _nudge_keeps_copy),
# by session, as (text, tags, the sender's id or None, the pending_words
# key(s) or None): the turn registry
# takes them when that turn's record ends (turn_registry._requeue_undelivered)
# and sends them on as the thread's next turn, once, whose user row keeps the
# ids (a retry of a Workbench Index send finds its words delivered by it,
# thread_pick.find_index_send). Insertion-ordered and capped, oldest first,
# for a turn no registry record ran (nothing would ever take its entry).
_unread_for_next_turn: dict[str, list[tuple[str, Optional[dict], object, object]]] = {}
_UNREAD_FOR_NEXT_TURN_MAX = 256


def end_turn_input(session_id: str) -> None:
    """The turn of `session_id` reads no more input: THE one place a turn's
    input ends (TurnEnd runs it once per turn, from EntityRuntime.send_message's
    `finally`, so every exit goes through it — the reply, the round backstop,
    the allowance's stop, a model error, a crash, a
    cancel). Its holds go (release_holds: from here, words for it start a turn
    of their own), and the words still queued, which no call of this turn
    reads, leave the queue in the same step (no await between), so no later
    turn on a session that keeps its id reads them again:

      - words that came through a route whose client keeps a copy (the
        Workbench page's nudge route: _nudge_keeps_copy) got no nudge_read
        receipt, so their client sends them again as its next message;
      - every other nudge's words are the server's: held for the turn
        registry, which sends them on as the thread's next turn
        (take_unread_for_next_turn), with the pending_words rows that hold
        them (the Circles nudge route's too: a copy in page memory is lost
        on a reload)."""
    if not session_id:
        return
    release_holds(session_id)
    texts_tags = list(_nudge_tags.get(session_id) or [])
    keeps = list(_nudge_keeps_copy.get(session_id) or [])
    # Each one's sender id too, read in the same step: the next turn's user
    # row keeps them, as the nudge's own row would have.
    ids = list(_nudge_ids.get(session_id) or [])
    # ...and the rows that hold them: they go with the words, and the turn
    # that takes the words settles them (pending_words).
    pkeys = list(_nudge_keys.get(session_id) or [])
    texts = consume_nudges(session_id)
    sent_on = [(t, texts_tags[i] if i < len(texts_tags) else None, ids[i] if i < len(ids) else None,
                pkeys[i] if i < len(pkeys) else None)
               for i, t in enumerate(texts) if not (i < len(keeps) and keeps[i])]
    if sent_on:
        _unread_for_next_turn.setdefault(session_id, []).extend(sent_on)
        while len(_unread_for_next_turn) > _UNREAD_FOR_NEXT_TURN_MAX:
            _old_sid, _old = next(iter(_unread_for_next_turn.items()))
            _unread_for_next_turn.pop(_old_sid, None)
            log.error("%d unread nudge(s) of session %s were never taken by a turn record "
                      "and are dropped", len(_old), _old_sid)
        log.info("%d unread nudge(s) the server delivers go on as the thread's next "
                 "turn (session %s)", len(sent_on), session_id)
    if len(texts) > len(sent_on):
        log.warning("%d nudge(s) queued at the turn's end were not read by it — their client "
                    "keeps a copy and sends them again as its next message (session %s)",
                    len(texts) - len(sent_on), session_id)


def take_unread_for_next_turn(session_id: str) -> list[tuple[str, Optional[dict], object, object]]:
    """What end_turn_input held for the thread's next turn, as (text, tags,
    the sender's id or None, the pending_words key(s) or None), taken once
    (the turn registry, as the turn's record ends)."""
    if not session_id:
        return []
    return _unread_for_next_turn.pop(session_id, None) or []


# Results handed to a running turn and not yet read by it: session_id ->
# [(brief, on_lost)]. A result must never be lost when the turn ends without
# reading it — the person pressed Stop mid-hold, or the turn died. The turn's
# task carries a done-callback: whatever it had not read is delivered the
# other way (on_lost: the fixed line in the thread, and a woken turn unless
# the end was a Stop).
_handed: dict[str, list[tuple[str, object]]] = {}
_watched: set[int] = set()
# How a turn was ended from outside, by session (turn_registry notes it): only
# the person's Stop ("user") means "do not carry on"; the turn-time ceiling, a
# shutdown, a crash all leave a result to wake a turn.
_stop_reasons: dict[str, str] = {}
# Re-deliveries in flight — held, because the loop keeps only a weak
# reference to a task nobody awaits.
_redeliveries: set = set()


def note_turn_stop(session_id: str, reason: str) -> None:
    """turn_registry: the turn of `session_id` is being torn down for `reason`
    ('user' = the person's Stop, 'timeout', 'cancelled')."""
    if session_id:
        _stop_reasons[session_id] = reason
        while len(_stop_reasons) > 256:   # read only when a result was handed
            _stop_reasons.pop(next(iter(_stop_reasons)), None)


def _deliver_unread(session_id: str, task: "asyncio.Task") -> None:
    _watched.discard(id(task))
    reason = _stop_reasons.pop(session_id, None)
    unread = _handed.pop(session_id, [])
    if not unread:
        return
    stopped = reason == "user"
    for _text, on_lost in unread:
        if on_lost is None:
            continue
        try:
            _t = asyncio.ensure_future(on_lost(not stopped))
            _redeliveries.add(_t)
            _t.add_done_callback(_redeliveries.discard)
        except Exception as e:  # noqa: BLE001
            log.error("hold: an unread result of session %s could not be re-delivered: %s",
                      session_id, e)
    log.warning("hold: the turn of session %s ended with %d result(s) unread (%s) — "
                "delivered to the thread instead", session_id, len(unread),
                "stopped by the user" if stopped else (reason or "ended"))


def results_read(session_id: str, texts: list[str]) -> None:
    """The turn injected these results: they are read, nothing to re-deliver."""
    left = [h for h in _handed.get(session_id, []) if h[0] not in texts]
    if left:
        _handed[session_id] = left
    else:
        _handed.pop(session_id, None)


def hand_result_to_turn(task_id, text: str, on_lost=None) -> bool:
    """Hand a finished task's brief to the turn that started it, if that turn
    is still running. True = handed (the turn continues from it); False = no
    live turn holds it (the caller wakes one). `on_lost(wake: bool)` is
    awaited if the turn ends before reading it (a Stop, a crash): the caller's
    way to write the result into the thread after all, and to wake a turn
    for it (wake=False after a Stop — the person asked the Host to stop)."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return False
    h = _task_holds.pop(tid, None)
    if not _hold_live(h):
        return False
    push_turn_event(h[0], text)
    _handed.setdefault(h[0], []).append((text, on_lost))
    task = h[1]
    if task is not None and id(task) not in _watched:
        _watched.add(id(task))
        task.add_done_callback(lambda t, sid=h[0]: _deliver_unread(sid, t))
    return True


# A woken turn that deals with a FAILED run can retry it, and the retry can
# fail again: that chain is the one a result can drive with nobody watching.
# After FAILED_WAKES_MAX failures in a row in one thread (a success resets
# it), the next failure is written but wakes no turn. A runaway backstop for
# the platform, far above a real retry (auto-advance itself retries once).
FAILED_WAKES_MAX = 3
_failed_wakes: dict[str, int] = {}


def kill_of(record: dict) -> str | None:
    """Who KILLED this build or compare ('user' / 'host'), or None.

    A kill (server/routes/review.kill_session_runs) is someone stopping the
    work on purpose, not the work failing. Its delivery is stated by the
    platform and wakes no turn: the failure brief tells the Host to run the
    work again, which is the one thing a kill must not lead to. A build
    carries it on its row (tools._factory_build_task_finish). A compare that
    ends with no version landed is a kill when any variant was killed: the
    brief would have the Host run the whole compare again, killed variants
    included, and the line lists every variant's outcome, so one that failed
    on its own is still stated. A compare with a version landed is the
    user's pick as always."""
    if not isinstance(record, dict):
        return None
    if record.get("task_type") == "factory_build":
        who = record.get("killed_by")
        return str(who) if who else None
    if record.get("task_type") == "fan_out":
        per = record.get("per_harness") or {}
        if not isinstance(per, dict):
            return None
        rows = [e or {} for e in per.values()]
        whos = [str(e["killed_by"]) for e in rows if e.get("killed_by")]
        if whos and not any(e.get("state") == "done" for e in rows):
            return "user" if "user" in whos else whos[0]
    return None


def delivery_wake_key(entity_id, app_id, project_id, thread_id) -> str:
    return f"{entity_id}|{app_id or 'chat'}|{project_id if project_id is not None else 'none'}|{thread_id or ''}"


# Turns running their tool loop right now: session_id -> the turn's task.
# Marked every round and cleared at the turn's exit; a turn that died reads
# dead through its task, so an answer is never pushed into a session nobody
# will read.
_live_turns: dict[str, "asyncio.Task | None"] = {}


def mark_turn_live(session_id: str) -> None:
    if session_id:
        # Reading again (a hold that released and then found input queued
        # goes round).
        _stopped_reading.pop(session_id, None)
        try:
            _live_turns[session_id] = asyncio.current_task()
        except RuntimeError:
            _live_turns[session_id] = None
        # A round boundary is a sign of life for the turn registry's liveness
        # judgement (a turn is stopped for silence, never for elapsed time).
        try:
            from server.turn_registry import note_alive
            note_alive(session_id)
        except Exception:  # noqa: BLE001 — no registry in this process
            pass


def turn_is_live(session_id: str) -> bool:
    return bool(session_id) and session_id in _live_turns and _hold_live(
        (session_id, _live_turns[session_id]))


# The ask card's answer, as app-web-kit/ask.ts answerText writes it.
_ANSWER_RE = re.compile(r"^Ticket #(\d+) — I chose\b")


def answer_into_turn(session_id: str, ticket_id, text: str, *, pending_key=None) -> bool:
    """A ticket the turn in `session_id` asked has been decided: put the
    person's answer into that turn, in the ask card's words, while the turn
    runs. True = pushed (the turn reads it at its next boundary, or its hold
    wakes on it); False = the turn has ended (the answer is on the ticket, and
    a client that holds it sends it as the next message).

    `pending_key`: the pending_words row the caller wrote the answer to
    (routes/tickets._answer_to_turn), so a restart before a turn has it
    still delivers it; it goes with the words (push_nudge)."""
    if not turn_is_live(session_id):
        return False
    # Whole: the person's answer is the input,
    # never cut. It was cut at 2,000 characters with nothing said to anyone.
    # The server delivers it: the decide route
    # answers every surface {answered_into_turn: true} and no client keeps a
    # copy from there (the Circles Tickets pane never did; the Workbench and
    # Circles ask cards settle on it), so words the turn never reads go on
    # as the thread's next turn, once (end_turn_input).
    if pending_key:
        push_nudge(session_id, text or "", pending_key=pending_key)
    else:
        push_nudge(session_id, text or "")
    return True


def held_turn_write(pool, entity_id, session_id, content: str, *, token: str, held: bool,
                    after=None, project_id=None, tags=None, created_at=None):
    """Write a holding turn's one row (memory_engine.write_turn_row_by_token)
    in the background, after the write `after` (the previous one for the same
    turn) has landed — so the row is inserted before it is rewritten, and its
    id is reused. Returns the task (held in _redeliveries until done)."""
    async def _run():
        rid = None
        if after is not None:
            try:
                rid = await after
            except BaseException:  # noqa: BLE001 — a lost first write: insert anew
                rid = None
        from mastercode.memory_engine import write_turn_row_by_token
        return await write_turn_row_by_token(
            pool, entity_id, session_id, "assistant", content, token=token, held=held,
            row_id=rid, project_id=project_id, tags=tags, created_at=created_at)
    t = asyncio.ensure_future(_run())
    _redeliveries.add(t)
    t.add_done_callback(_redeliveries.discard)
    return t


# How long the answer waits for the asking turn to finish its last steps
# (writing its row) before the answer's own turn starts in that thread.
ANSWER_AFTER_WAIT_S = 20.0


async def answer_after_turn(entity_id, room: dict, text: str, *,
                            asked_session: str | None = None,
                            max_chars: int | None = None,
                            keeps_copy: bool = False,
                            pending_key=None) -> bool:
    """The turn that asked has ENDED: the person's answer starts a turn in the
    room it asked in, as their message — the same as the Workbench and Circles
    cards' resend, from every surface, the Tickets pane included. True once
    that turn is registered; the cards then follow
    it (reattach) instead of sending the answer again.

    A turn of the asking session still finishing its last steps on that thread
    is waited out first (bounded), so the answer never becomes a nudge into a
    turn past its last read. Any other turn running there takes the answer as
    the person's words, as a message typed into it would.

    `max_chars`: None (the default) — the person's words go in whole, an
    answer to a card and a typed message alike (never cut with nothing
    said). A caller may
    still pass a bound; none does.

    `keeps_copy`: the caller's client keeps the words and sends them again
    itself, so if they become a nudge into another turn running there, the
    server never sends them on (_nudge_keeps_copy). No caller passes it since
    the Circles nudge route's words became the server's.

    `pending_key`: the pending_words row(s) holding the words; they go with
    the words (brain_service.send_message pending_keys)."""
    if not isinstance(room, dict) or entity_id is None:
        return False
    text = (text or "").strip()
    if max_chars:
        text = text[:max_chars]
    if not text:
        return False
    # A room recorded on a hidden lane (a ticket from before escalate mapped
    # it) is mapped the way a delivery is: the answer's turn is a person's,
    # and it runs where they can see it — never on a lane nobody reads.
    try:
        from mastercode.memory_engine import HIDDEN_LANE_APP_IDS
    except Exception:  # noqa: BLE001
        HIDDEN_LANE_APP_IDS = ("circles-replay", "circles-ab", "scheduled_task")
    if room.get("app_id") in HIDDEN_LANE_APP_IDS:
        # The conversation the wake-up thought in: a ticket's room names it
        # `interaction_context` (the escalate path), the turn registry's room
        # `history_context` (brain_service.send_message). The answer is the
        # person's own turn IN the room that wake-up reports into, so it is
        # not run borrowed from it: a borrowed turn writes its rows outside
        # the conversation it thinks in (_row_project_id), and the same ask
        # answered from the Tickets pane starts a plain turn there.
        _app, _proj = wakeup_delivery_room(
            "scheduled_task", room.get("project_id"),
            room.get("interaction_context") or room.get("history_context"),
            room.get("thread_id"))
        room = {**room, "app_id": _app, "project_id": _proj, "history_context": None}
        if room.get("app_id") in HIDDEN_LANE_APP_IDS:
            log.warning("answer: the asking room %r is a lane nobody reads — no turn started", room)
            return False
    try:
        from server import brain_service
        from server.turn_registry import compose_thread_key, get_turn_registry
        # history_context: the conversation the asking turn thought in when
        # it was not its app's own (brain_service.send_message records it in
        # the room); the answer runs there too, under that conversation's lock.
        key = compose_thread_key(entity_id, room.get("app_id"), room.get("project_id"),
                                 thread_id=room.get("thread_id"),
                                 history_context=room.get("history_context"))
        rec = get_turn_registry().active_for(key)
        if rec is not None and asked_session and rec.session_id == asked_session:
            try:
                await asyncio.wait_for(rec.done.wait(), timeout=ANSWER_AFTER_WAIT_S)
            except asyncio.TimeoutError:
                log.warning("answer: the asking turn on %s is still finishing; the answer "
                            "goes to it as the person's words", key)
        agen = brain_service.send_message(
            entity_id=entity_id, message=text, session_id=None,
            app_id=room.get("app_id"), project_id=room.get("project_id"),
            thread_id=room.get("thread_id"),
            history_context=room.get("history_context"),
            # The person's answer (a ticket they decided, a connector grant
            # they chose, a Circles nudge whose route checked its caller):
            # the root thread takes it (root_turn.words_refusal).
            from_person=True,
            **({"nudge_keeps_copy": True} if keeps_copy else {}),
            **({"pending_keys": pending_key} if pending_key else {}))
        first = await agen.__anext__()
    except StopAsyncIteration:
        return False
    except Exception as e:  # noqa: BLE001 — the decide already stands
        log.warning("answer: the answer's turn did not start: %s", e)
        return False
    if (first or "").startswith("3:"):
        log.warning("answer: the answer's turn was refused: %s", first[:200])
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _drain() -> None:
        # The turn runs detached in the registry; this only follows it to its end.
        try:
            async for _frame in agen:
                pass
        except Exception as e:  # noqa: BLE001
            log.debug("answer: following the answer's turn ended early: %s", e)
    _t = asyncio.ensure_future(_drain())
    _redeliveries.add(_t)
    _t.add_done_callback(_redeliveries.discard)
    return True


def max_holding_turns() -> int | None:
    """How many more turns of the Host's own its memory holds now
    (host_memory.turns_that_fit, every running turn counted at a turn's
    measured cost). None: no readable limit. Not a budget: a person's message
    never needs a slot, it always starts."""
    from mastercode.runtime import host_memory
    return host_memory.turns_that_fit()


def background_turns() -> int:
    """Turns in the background budget now: a person's turns holding at their
    exit, and woken turns (a woken turn that holds is counted once)."""
    return len(_holding_now) + _woken_running


def may_hold(woken: bool = False) -> bool:
    """A turn may hold at its exit when the Host's memory holds it. A woken
    turn was counted when it started."""
    from mastercode.runtime import host_memory
    return woken or host_memory.one_more_fits()


def may_wake() -> bool:
    """A woken turn starts when the Host's memory holds one more turn."""
    from mastercode.runtime import host_memory
    return host_memory.one_more_fits()


# How often a woken turn waiting for a background slot looks again.
WAKE_SLOT_POLL_S = 2.0


def woken_turn_started() -> None:
    global _woken_running
    _woken_running += 1


def woken_turn_ended() -> None:
    global _woken_running
    _woken_running = max(0, _woken_running - 1)


def person_wrote(thread_key: str | None) -> None:
    """The person wrote in this thread (a message, or words read into a
    running turn): the chain of woken turns starts counting again."""
    if thread_key:
        _woken_since_person.pop(thread_key, None)
        _research_rounds.pop(thread_key, None)
        _watch_wakes.pop(thread_key, None)


async def hold_for(session_id: str, asks_open: bool, timeout_s: float, *,
                   count: bool = True) -> bool:
    """Wait at the turn's exit, at most `timeout_s`, for what the turn is
    waiting on: the answer to an open ask (`asks_open` — it arrives as the
    person's words, pushed by whichever surface they answered on) or the
    result of a task it started (held_tasks). True the moment input is queued
    for the session (left queued: the loop's drain takes it); False at the
    cap, or when nothing is left to wait for."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_s or 0.0))
    if count:
        _holding_now.add(session_id)
    try:
        while True:
            if has_pending_input(session_id):
                return True
            now = loop.time()
            if now >= deadline:
                return False
            if not asks_open and not held_tasks(session_id):
                return False
            await asyncio.sleep(min(ASK_POLL_S, max(0.0, deadline - now)))
    finally:
        _holding_now.discard(session_id)
