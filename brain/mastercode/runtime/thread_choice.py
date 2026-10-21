"""runtime/thread_choice.py — which thread a message belongs to, chosen in the
Host's own turn.

Every surface first places a message in software (connectors/thread_pick):
a reply to a mapped message, a mapped platform thread, a command, a short
follow-on, or a CLEAR score-based match (thread_pick.clear_match) joins its
thread with no model call. When none of those settles it, no separate model
call decides. The message lands somewhere deterministic (a new topic in
Circles; the chat's last thread, else a new one, for a messaging app; the page
the person has open, from the Workbench Index), and the Host's own turn
starts there with the candidate threads in its frame and one more tool:

  pick_thread(thread="<id from the list>", reason="...")   continue that one
  pick_thread(new=true, title="...", reason="...")        a thread of its own
  (no call)                                                stay where it landed

The pick runs in the turn: the Host decides with its own prompt, rules and
memory, the call is in the turn's record, and its cost is the turn's. A pick
of another thread hands the Host that thread's state, last exchanges and the
memories it reads there (the tool result), so it can answer from them. When
the turn ends, the exchange moves there (move_exchange): the person's message
and the reply are filed in the chosen thread with what was learned in the
turn (a rule taught in it, what close_turn and memory_curate kept:
circles_threads.carry_learned), a marker says the Host picked it, and the
client is told (the thread_moved frame). A message that landed in
a new topic made only for it leaves no empty topic behind: the topic is
folded into the one chosen (circles_threads.merged_into), so a page still
naming it sends to the chosen one.

This replaces thread_pick._ask_host, the one short call on the chat model
that picked the thread for messaging apps, the Workbench Index and Circles
before the turn: that call decided which
history, thread preferences, goal and summary the turn would read, outside
the turn and hidden from the activity feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("cseries.mastercode.thread_choice")

TOOL_NAME = "pick_thread"
FRAME_KEY = "thread_choice"
# How many candidates the frame lists.
CHOICE_MAX = 8
# What a pick of another thread hands back: its last exchanges, at most this
# many messages of at most this many characters each, and the memory block it
# reads (memory_engine.build_memory_context) at most this many tokens.
RECENT_MESSAGES = 8
RECENT_CHARS = 600
MEMORY_TOKENS = 1500
# How long a move waits for the write of a rule taught in the message
# (seconds): the bound the message row itself is awaited with (_await_row).
LEARNED_WAIT = 30

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Only in a turn whose frame lists threads this message may belong to: continue one of "
            "them (thread = its id from the list) or start a thread of its own (new=true, title). "
            "Call it before anything else; the exchange is filed there when the turn ends. "
            "With no call the message stays where it landed."),
        "parameters": {
            "type": "object",
            "properties": {
                "thread": {"type": "string", "description": "An id from the list, e.g. 'thread:6f1c…'."},
                "new": {"type": "boolean"},
                "title": {"type": "string", "description": "With new=true: a few words."},
                "reason": {"type": "string", "description": "One short sentence, written where it is filed."},
            },
            "required": ["reason"],
        },
    },
}

SURFACE_WORDS = {"circles": "Circles", "connector": "Messaging app", "index": "Workbench Index"}


@dataclass
class Choice:
    """A turn's open thread choice: where the message landed, and the
    candidates the Host may move it to."""
    surface: str
    landed: dict                          # {kind, ref, title, provisional}
    candidates: list = field(default_factory=list)
    platform: str = ""
    picked: Optional[dict] = None         # {kind, ref, title, new, reason} once the Host picked
    answered: bool = False
    # Where the turn wrote and read before a pick of a listed thread moved it
    # there (place_turn): {scope, recall_scope, thread_id}, for stay().
    before: Optional[dict] = None

    def keys(self) -> set:
        return {str(c.get("key")) for c in self.candidates}


def frame_value(*, surface: str, landed: dict, candidates: list, platform: str = "") -> dict:
    """What a surface puts in the turn's frame under FRAME_KEY (read back by
    from_frame)."""
    return {"surface": surface, "landed": dict(landed), "platform": platform,
            "candidates": [dict(c) for c in candidates[:CHOICE_MAX]]}


def from_frame(frame: Any) -> Optional[Choice]:
    """The turn's Choice from its frame, or None when the frame offers none.
    The value is set by the platform's own routes, never by a page."""
    v = (frame or {}).get(FRAME_KEY) if isinstance(frame, dict) else None
    if not isinstance(v, dict):
        return None
    landed = v.get("landed")
    cands = v.get("candidates")
    if not (isinstance(landed, dict) and landed.get("kind") and isinstance(cands, list)):
        return None
    surface = str(v.get("surface") or "")
    if surface not in SURFACE_WORDS:
        return None
    clean = []
    for c in cands[:CHOICE_MAX]:
        if isinstance(c, dict) and c.get("kind") in ("thread", "project") and c.get("ref"):
            clean.append({**c, "key": f"{c['kind']}:{c['ref']}"})
    return Choice(surface=surface, landed=dict(landed), candidates=clean,
                  platform=str(v.get("platform") or ""))


def _clip(s: Any, n: int) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def render_block(choice: Choice) -> str:
    """The frame's lines for the choice: where the message landed and the
    threads it may continue, each with its id, title, last activity and last
    exchange."""
    where = choice.landed.get("title") or "a new thread"
    head = (f"Thread choice: this message landed in {where!r}"
            + (" (a new thread made for it)" if choice.landed.get("provisional") else "")
            + ", because nothing matched it clearly.")
    if not choice.candidates:
        return head + " If it is a thread of its own, name it with close_turn's title."
    if choice.surface == "index":
        # From the Workbench the message is on a page already: the Host moves
        # it to another listed page or leaves it, never into a Circles thread.
        how = ("If it belongs on one of these pages instead, call pick_thread(thread=<id>, reason=...) "
               "before anything else. With no call it stays on this page.")
    else:
        how = ("If it continues one of these, call pick_thread(thread=<id>, reason=...) before "
               "anything else; to give it a thread of its own call pick_thread(new=true, title=..., "
               "reason=...). With no call it stays where it landed.")
    lines = [head, how]
    for c in choice.candidates:
        bits = [f"[{c['key']}] {_clip(c.get('title'), 120) or 'untitled'}"]
        if c.get("last_activity"):
            bits.append(f"last active {str(c['last_activity'])[:10]}")
        lines.append("- " + ", ".join(bits))
        if c.get("last_user"):
            lines.append(f"    person: {_clip(c['last_user'], 280)}")
        if c.get("last_host"):
            lines.append(f"    you: {_clip(c['last_host'], 280)}")
    return "\n".join(lines)


def _scope_of(kind: str, ref: str) -> dict:
    if kind == "project":
        return {"kind": "project", "ref": str(int(ref))}
    return {"kind": "thread", "ref": str(ref)}


async def _thread_title(pool, entity_id: int, kind: str, ref: str) -> str:
    try:
        if kind == "project":
            return str(await pool.fetchval("SELECT name FROM projects WHERE id = $1 AND entity_id = $2",
                                           int(ref), entity_id) or "")
        return str(await pool.fetchval(
            "SELECT COALESCE(NULLIF(title, ''), topic, '') FROM circles_threads"
            " WHERE id = $1::uuid AND entity_id = $2", str(ref), entity_id) or "")
    except Exception:  # noqa: BLE001
        return ""


async def _open(conn, entity_id: int, kind: str, ref: str) -> bool:
    if kind == "project":
        return bool(await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1 AND entity_id = $2 AND status <> 'archived'",
            int(ref), entity_id))
    return bool(await conn.fetchval(
        "SELECT 1 FROM circles_threads WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL",
        str(ref), entity_id))


async def _context_of(pool, entity_id: int, session_id: str, scope: dict, query: str) -> dict:
    """What a pick of `scope` hands the Host: where the thread stands, its
    last exchanges, and the memory it reads there."""
    out: dict = {}
    try:
        from mastercode.runtime.close_turn import thread_state
        st = await thread_state(pool, entity_id, scope)
        if st:
            out["where_it_stands"] = st
    except Exception as e:  # noqa: BLE001
        log.debug("thread choice: state unavailable: %s", e)
    try:
        from mastercode.memory_engine import recall_recent_turns
        turns = await recall_recent_turns(pool, entity_id, session_id, limit=RECENT_MESSAGES,
                                          source_context=scope)
        out["last_messages"] = [{"role": t.get("role"), "content": _clip(t.get("content"), RECENT_CHARS)}
                                for t in turns if t.get("role") in ("user", "assistant")]
    except Exception as e:  # noqa: BLE001
        log.debug("thread choice: history unavailable: %s", e)
    try:
        from mastercode.memory_engine import MemoryEngine
        block = await MemoryEngine(pool).build_memory_context(
            entity_id, {}, max_tokens=MEMORY_TOKENS, query=query, source_context=scope)
        if block:
            out["memory_there"] = block
    except Exception as e:  # noqa: BLE001
        log.debug("thread choice: memory unavailable: %s", e)
    return out


async def pick(pool, sess, args: Any, choice: Choice, *, user_message: str = "") -> dict:
    """Run the Host's pick_thread call. The first valid call decides; the
    exchange moves when the turn ends (move_exchange). Never raises."""
    a = args if isinstance(args, dict) else {}
    if choice.answered:
        return {"status": "ok", "result": "the thread was already picked this turn; this call changed nothing"}
    reason = _clip(a.get("reason"), 300)
    named = str(a.get("thread") or "").strip()
    new = a.get("new") is True
    if new and named:
        return {"status": "error", "result": "name a thread from the list or ask for a new one, not both"}
    if new and choice.surface == "index":
        return {"status": "error", "result": "this message is on a Workbench page already: pick a listed page, "
                                             "or make no call and it stays where it is"}
    if new:
        title = _clip(a.get("title"), 120)
        if not title:
            return {"status": "error", "result": "a new thread needs a title of a few words"}
        choice.answered = True
        if choice.landed.get("provisional"):
            # It already is a thread of its own: it keeps the Host's title.
            try:
                from mastercode.circles_threads import set_thread_topic
                await set_thread_topic(pool, sess.entity_id, choice.landed["ref"], None, title=title)
            except Exception as e:  # noqa: BLE001
                log.debug("thread choice: title not set: %s", e)
            return {"status": "ok", "result": f"kept in its own new thread, titled {title!r}"}
        choice.picked = {"kind": "thread", "ref": None, "title": title, "new": True, "reason": reason}
        return {"status": "ok", "result": f"this exchange is filed in a new thread {title!r} when "
                                          "the turn ends; answer as usual"}
    if not named:
        return {"status": "error", "result": "pick_thread needs thread=<an id from the list> or new=true"}
    if named not in choice.keys():
        return {"status": "error", "result": f"{named!r} is not one of the threads listed in this turn's frame"}
    kind, _, ref = named.partition(":")
    if not await _open(pool, sess.entity_id, kind, ref):
        return {"status": "error", "result": f"{named} was closed or deleted meanwhile; the message stays where it landed"}
    title = await _thread_title(pool, sess.entity_id, kind, ref)
    choice.answered = True
    choice.picked = {"kind": kind, "ref": ref, "title": title, "new": False, "reason": reason}
    scope = _scope_of(kind, ref)
    # Everything the rest of this turn writes lands in the chosen thread:
    # close_turn's preferences and goal, memory_curate's rows (memory_engine.
    # session_source_context reads this). A move that cannot be made at the
    # turn's end puts the turn back where it landed (stay) and brings these
    # writes back with it (_learned_stay).
    try:
        from mastercode.memory_engine import CURRENT_TURN_SCOPE
        cur = CURRENT_TURN_SCOPE.get()
        mine = isinstance(cur, dict) and str(cur.get("session_id")) == str(sess.session_id)
        choice.before = {"scope": dict(cur["scope"]) if mine and isinstance(cur.get("scope"), dict) else None,
                         "recall_scope": getattr(sess, "recall_scope", None),
                         "thread_id": getattr(sess, "thread_id", None)}
        place_turn(sess, scope)
    except Exception as e:  # noqa: BLE001
        log.debug("thread choice: turn scope not moved: %s", e)
    ctx = await _context_of(pool, sess.entity_id, sess.session_id, scope, user_message)
    return {"status": "ok", "result": {
        "moved_to": {"thread": named, "title": title},
        "note": ("This exchange is filed there when the turn ends. Below is that thread's state, its "
                 "last messages and the memory you hold there: answer from them."),
        **ctx}}


def place_turn(sess, scope: dict) -> None:
    """The rest of the running turn writes in `scope` and reads there: the
    turn's scope (memory_engine.CURRENT_TURN_SCOPE, what session_source_context
    gives every write in the turn), changed in place so a task the turn
    started earlier reads the change too, and the session's recall scope and
    thread. A write the PII queue holds keeps the scope it was queued with
    (its writer took it then), so a move waits for those first (pii_queue.
    wait_turn). Set by the pick of a listed thread, and by session.py after a
    move, to where the exchange went (a thread the move made, a project).
    Never raises."""
    try:
        from mastercode.memory_engine import CURRENT_TURN_SCOPE
        scope = dict(scope)
        cur = CURRENT_TURN_SCOPE.get()
        if isinstance(cur, dict) and str(cur.get("session_id")) == str(sess.session_id):
            cur["scope"] = scope
        else:
            CURRENT_TURN_SCOPE.set({"session_id": sess.session_id, "scope": scope})
        sess.recall_scope = scope
        if scope.get("kind") == "thread":
            sess.thread_id = scope["ref"]
    except Exception as e:  # noqa: BLE001 — never a failed turn
        log.warning("thread choice: the turn's place was not set to %s: %s", scope, e)


def stay(sess, choice: Choice) -> None:
    """The move could not be made: the rest of the turn writes and reads where
    the message landed, as it did before the pick (Choice.before). Never
    raises."""
    before = choice.before
    choice.before = None
    if not before:
        return
    try:
        from mastercode.memory_engine import CURRENT_TURN_SCOPE
        cur = CURRENT_TURN_SCOPE.get()
        if (before.get("scope") and isinstance(cur, dict)
                and str(cur.get("session_id")) == str(sess.session_id)):
            cur["scope"] = dict(before["scope"])
        sess.recall_scope = before.get("recall_scope")
        sess.thread_id = before.get("thread_id")
    except Exception as e:  # noqa: BLE001
        log.debug("thread choice: turn scope not put back: %s", e)


async def _await_row(user_row_task):
    """The message row's id from its write (bounded), or None: in PII mode
    the write is queued and returns none (turn_message_row finds the row)."""
    if user_row_task is None:
        return None
    from mastercode.memory_engine import TURN_ROW_WAIT
    try:
        return await asyncio.wait_for(asyncio.shield(user_row_task), timeout=TURN_ROW_WAIT)
    except Exception as e:  # noqa: BLE001
        log.info("thread choice: user row task did not resolve (%s); locating it by the turn's token", e)
        return None


async def turn_message_row(conn, entity_id: int, token: Optional[str], *, src: Optional[dict] = None,
                           project_id: Optional[int] = None) -> Optional[int]:
    """The id of the turn's message row: the person's row, not a nudge's,
    carrying the turn's token (metadata.turn_token, on it from the turn's
    start and kept through the PII queue's scrub), still said in `src` or
    still in project `project_id`. Never by its words: in PII mode the row
    holds the tokenized ones. None when it is not there."""
    if not token:
        return None
    sql = ("SELECT id FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'"
           " AND metadata->>'turn_token' = $2 AND metadata->>'role' = 'user'"
           " AND COALESCE(metadata->>'nudge', '') <> 'true'")
    args: list = [entity_id, str(token)]
    if src is not None:
        sql += " AND metadata->'source_context'->>'kind' = $3 AND metadata->'source_context'->>'ref' = $4"
        args += [str(src.get("kind") or ""), str(src.get("ref") or "")]
    if project_id is not None:
        args.append(int(project_id))
        sql += f" AND project_id = ${len(args)}"
    rid = await conn.fetchval(sql + " ORDER BY created_at, id LIMIT 1", *args)
    return int(rid) if rid is not None else None


async def turn_settled(token: Optional[str], learned_task=None, nudge_row_tasks=None) -> Optional[str]:
    """Wait (bounded) for everything the turn wrote before its move: a rule
    taught in its message (`learned_task`), the words the person sent into it
    (`nudge_row_tasks`), and, in PII mode, every write the turn queued
    (pii_queue.wait_turn: the message row, its nudge rows, close_turn's and
    memory_curate's writes, the taught rule). None when all of it is written;
    else why not, and the move is not made: a write that lands after the move
    lands in the place the turn left, which is half a move."""
    if not await _await_learned(learned_task):
        return "the taught rule's write did not finish"
    if not await await_turn_rows(nudge_row_tasks):
        return "the rows of the words sent into the turn did not finish"
    from mastercode import pii_queue
    from mastercode.memory_engine import TURN_ROW_WAIT
    if not await pii_queue.wait_turn(token, TURN_ROW_WAIT):
        return f"the PII queue did not land what the turn wrote within {TURN_ROW_WAIT}s"
    return None


def _marker_kind(surface: str) -> str:
    from server.connectors import thread_pick as tp
    return {"circles": tp.TOPIC_MARKER_KIND, "connector": tp.PICK_MARKER_KIND,
            "index": tp.INDEX_PICK_MARKER_KIND}.get(surface, tp.PICK_MARKER_KIND)


async def _marker(conn, entity_id: int, scope: dict, text: str, *, source_kind: str,
                  session: str, extra: Optional[dict] = None) -> None:
    md = {"role": "assistant", "source_kind": source_kind, "source_context": scope, **(extra or {})}
    if scope["kind"] == "thread":
        md["app_id"] = "circles"
    await conn.execute(
        "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status,"
        " source_session, project_id, metadata, created_at)"
        " VALUES ($1, $2, 'turn', 0.1, 'active', $3, $4, $5::jsonb, $6)",
        entity_id, text, session, int(scope["ref"]) if scope["kind"] == "project" else None,
        json.dumps(md), datetime.now(timezone.utc))


async def _await_learned(task) -> bool:
    """Wait (bounded) for the write of a rule taught in the message
    (session._keep_taught), so the move finds it and carries it. That write
    waits on the same message row this move does: unawaited, it can land
    after the move, tagged with the place the message left. False when the
    bound passed with it still running (a write that failed is done)."""
    if task is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=LEARNED_WAIT)
    except TimeoutError:
        log.info("thread choice: the taught rule's write did not finish within %ss", LEARNED_WAIT)
        return False
    except Exception as e:  # noqa: BLE001 — it failed: nothing of it lands later
        log.info("thread choice: the taught rule's write failed (%s)", e)
    return True


class _NotMoved(Exception):
    """The move found its message row gone inside its transaction: raised to
    roll the transaction back, so the turn stays whole where it landed."""


async def await_turn_rows(tasks) -> bool:
    """Wait (bounded, TURN_ROW_WAIT in all) for the writes of the rows the
    person sent into the running turn (session.py's drain writes each as a
    user row of its own), so a move at the turn's end finds them and takes
    them with the message (turn_rows): unawaited, one could land after the
    move, in the place the turn left. False when the bound passed with one
    still running."""
    pending = [t for t in tasks or [] if t is not None and not t.done()]
    if not pending:
        return True
    from mastercode.memory_engine import TURN_ROW_WAIT
    _done, still = await asyncio.wait(pending, timeout=TURN_ROW_WAIT)
    if still:
        log.info("thread choice: %d of the turn's nudge rows did not finish within %ss",
                 len(still), TURN_ROW_WAIT)
        return False
    return True


async def turn_rows(conn, entity_id: int, message_id: int, src: dict) -> list[int]:
    """The other user rows of the turn whose message row is `message_id`:
    what the person sent into the running turn (session.py's drain:
    metadata.nudge), each carrying the turn's token (metadata.turn_token),
    still said in `src`. They are the turn's own words and go where its
    message goes."""
    rows = await conn.fetch(
        "SELECT n.id FROM entity_memory m JOIN entity_memory n"
        "   ON n.entity_id = m.entity_id AND n.memory_type = 'turn' AND n.status = 'active'"
        "  AND n.metadata->>'turn_token' = m.metadata->>'turn_token'"
        "  AND n.metadata->>'role' = 'user' AND n.metadata->>'nudge' = 'true'"
        "  AND n.metadata->'source_context'->>'kind' = $3"
        "  AND n.metadata->'source_context'->>'ref' = $4"
        " WHERE m.id = $1 AND m.entity_id = $2 AND COALESCE(m.metadata->>'turn_token', '') <> ''"
        " ORDER BY n.created_at, n.id",
        int(message_id), entity_id, str(src.get("kind") or ""), str(src.get("ref") or ""))
    return [int(r["id"]) for r in rows]


async def move_exchange(pool, entity_id: int, choice: Choice, *, src: dict, user_row_task,
                        turn_token: Optional[str] = None, learned_task=None,
                        nudge_row_tasks=None) -> dict:
    """File the turn's exchange where the Host picked (choice.picked), in one
    transaction: the person's message row moves, with the words they sent
    into the turn (turn_rows); a new thread is made when the Host asked for
    one; a marker in the chosen thread says it was picked and why; the place
    it left keeps a marker and the rows learned from the message follow it
    (_carry_learned), or, when it was a new topic made only for this message,
    is folded into the chosen thread (merged_into) with every row it held.

    Everything the turn wrote before the move is waited for first, bounded
    (turn_settled: the message row's write, a rule taught in it
    (`learned_task`), the nudge rows' writes (`nudge_row_tasks`), and in PII
    mode every write the turn queued), so none of it lands after the move in
    the place the turn left. The message row is found by the turn's token
    (`turn_token`, turn_message_row), never by its words. When a bound
    passes, or the row is not found, nothing moves.

    Returns {moved, to: scope, title, user_row_id}; moved False leaves the
    turn whole where it landed (the caller then writes the reply there, and
    puts the turn's place back: stay), and what the turn wrote in the picked
    thread since the pick comes back with it (_learned_stay). Never
    raises."""
    out = await _move(pool, entity_id, choice, src=src, user_row_task=user_row_task,
                      turn_token=turn_token, learned_task=learned_task,
                      nudge_row_tasks=nudge_row_tasks)
    if choice.picked and not out.get("moved"):
        await _learned_stay(pool, entity_id, choice, src, turn_token)
    return out


async def _learned_stay(pool, entity_id: int, choice: Choice, src: dict, token: Optional[str]) -> None:
    """A move that could not be made, after the pick of a listed thread: what
    the turn wrote since the pick went to that thread (pick, place_turn), and
    is said where the exchange stays, by the rule every move takes
    (circles_threads.carry_learned, from the picked thread back to `src`).
    Now for what has landed; in PII mode behind the writes the turn still has
    queued (pii_queue.after_turn), so they come back once they land. A pick
    of a new thread moved nothing (it is made by the move). Never raises."""
    picked = choice.picked or {}
    if picked.get("new") or not picked.get("ref") or not token:
        return
    went = _scope_of(str(picked["kind"]), str(picked["ref"]))

    async def _back():
        rid = await turn_message_row(pool, entity_id, token, src=src)
        if rid is None:
            log.warning("thread choice: the message row is gone; what the turn wrote after the pick stays in %s:%s",
                        went["kind"], went["ref"])
            return
        from mastercode import circles_threads as ct
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ct.carry_learned(conn, entity_id, [rid], went, src)
    try:
        from mastercode import pii_queue
        await pii_queue.after_turn(token, _back, pool=pool, entity_id=entity_id)
    except Exception as e:  # noqa: BLE001 — never a failed turn
        log.warning("thread choice: what the turn wrote after the pick was not brought back: %s", e)


async def _move(pool, entity_id: int, choice: Choice, *, src: dict, user_row_task,
                turn_token: Optional[str], learned_task, nudge_row_tasks) -> dict:
    out: dict = {"moved": False}
    picked = choice.picked
    if not picked:
        return out
    try:
        rid = await _await_row(user_row_task)
        unsettled = await turn_settled(turn_token, learned_task, nudge_row_tasks)
        if unsettled:
            log.info("thread choice: %s; the turn stays where it landed", unsettled)
            return out
        if rid is None:
            rid = await turn_message_row(pool, entity_id, turn_token, src=src)
        if rid is None:
            log.info("thread choice: the message row was not found; the turn stays where it landed")
            return out
        from mastercode import circles_threads as ct
        async with pool.acquire() as conn:
            async with conn.transaction():
                if picked.get("new"):
                    row = await ct.create_thread(conn, entity_id, title=picked["title"], origin="user")
                    kind, ref, title = "thread", str(row["id"]), row.get("title") or picked["title"]
                else:
                    kind, ref, title = picked["kind"], str(picked["ref"]), picked.get("title") or ""
                    if not await _open(conn, entity_id, kind, ref):
                        log.info("thread choice: %s:%s closed meanwhile; the turn stays", kind, ref)
                        return out
                dst = _scope_of(kind, ref)
                ids = [int(rid)] + await turn_rows(conn, entity_id, int(rid), src)
                res = await conn.fetch(
                    "UPDATE entity_memory SET project_id = $3,"
                    " metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object("
                    "   'source_context', $4::jsonb, 'adopted_from', $5::jsonb,"
                    "   'adopted_at', to_jsonb(now()::timestamptz), 'rehomed', true)"
                    " WHERE id = ANY($1::int[]) AND entity_id = $2 RETURNING id",
                    ids, entity_id, int(ref) if kind == "project" else None,
                    json.dumps(dst), json.dumps(src))
                if int(rid) not in {int(r["id"]) for r in res}:
                    # The message row is gone meanwhile: nothing moves (the
                    # raise rolls back what this transaction changed).
                    raise _NotMoved()
                why = picked.get("reason") or ""
                where = SURFACE_WORDS.get(choice.surface, "Message")
                verb = "started this thread" if picked.get("new") else "picked this thread"
                await _marker(conn, entity_id, dst, f"{where} message, {verb}" + (f": {why}" if why else "."),
                              source_kind=_marker_kind(choice.surface), session=f"pick:{choice.surface}")
                if choice.landed.get("provisional") and src.get("kind") == "thread":
                    await _fold(conn, entity_id, src, dst, ids)
                else:
                    await _carry_learned(conn, entity_id, ids, src, dst)
                    # The rows moved and the reply written there.
                    await _marker(conn, entity_id, src,
                                  f"→ This exchange moved to {title or ref}"
                                  + (f": {why}" if why else ". Nothing was deleted."),
                                  source_kind="thread_marker", session=f"pick:{choice.surface}",
                                  extra={"marker": {"moved_to": dst, "moved_turns": len(ids) + 1,
                                                    "rehomed": True}})
        out.update({"moved": True, "to": dst, "title": title, "user_row_id": int(rid),
                    "new": bool(picked.get("new"))})
    except _NotMoved:
        log.info("thread choice: the message row changed meanwhile; the turn stays where it landed")
        return {"moved": False}
    except Exception as e:  # noqa: BLE001 — never a failed turn: it stays whole
        log.warning("thread choice: the exchange could not be moved (it stays): %s", e)
        return {"moved": False}
    return out


async def _carry_learned(conn, entity_id: int, moved_ids: list[int], src: dict, dst: dict) -> None:
    """What was learned from the moved message goes with it when the place it
    left stays (a thread or page with a history of its own), by the one rule
    every move takes (circles_threads.carry_learned): a rule taught in the
    message, what close_turn and memory_curate learned in its turn, the
    records of what they did, the place a rule was taught and their slot
    scope are the chosen thread's; the project tag stays as it was.
    turn_home.rehome_turn carries a re-homed turn's message through here."""
    from mastercode import circles_threads as ct
    await ct.carry_learned(conn, entity_id, moved_ids, src, dst)


async def _fold(conn, entity_id: int, src: dict, dst: dict, moved_ids: list[int]) -> None:
    """A new topic made only for this message, now that the Host filed the
    message elsewhere: its markers go, every row still tagged with it moves to
    the chosen thread — its turn rows, and what was learned there by the one
    rule every move takes, the whole conversation moving (circles_threads.
    carry_learned: a rule taught in the message, anything the turn wrote
    before the pick, the records of what they did) — its decisions and every
    other record naming it follow, since it is gone, and it is folded into
    the chosen thread so a page that still names it sends there."""
    sref = str(src["ref"])
    from mastercode import circles_threads as ct
    from server.connectors import thread_pick as tp
    await conn.execute(
        "UPDATE entity_memory SET status = 'archived', status_reason = 'folded into the picked thread',"
        " status_changed_at = now()"
        " WHERE entity_id = $1 AND memory_type = 'turn' AND status = 'active'"
        "   AND metadata->'source_context'->>'kind' = 'thread' AND metadata->'source_context'->>'ref' = $2"
        "   AND metadata->>'source_kind' = ANY($3::text[])",
        entity_id, sref, [tp.TOPIC_MARKER_KIND, tp.PICK_MARKER_KIND, "thread_marker"])
    proj = int(dst["ref"]) if dst["kind"] == "project" else None
    await conn.execute(
        "UPDATE entity_memory SET project_id = $4,"
        " metadata = jsonb_set(metadata, '{source_context}', $3::jsonb)"
        " WHERE entity_id = $1 AND memory_type = 'turn'"
        "   AND metadata->'source_context'->>'kind' = 'thread'"
        "   AND metadata->'source_context'->>'ref' = $2",
        entity_id, sref, json.dumps(dst), proj)
    await ct.carry_learned(conn, entity_id, moved_ids, src, dst, whole=True, resync=False)
    await conn.execute(
        "UPDATE entity_memory SET metadata = jsonb_set(metadata, '{about}', $3::jsonb)"
        " WHERE entity_id = $1 AND metadata->'about'->>'kind' = 'thread' AND metadata->'about'->>'ref' = $2",
        entity_id, sref, json.dumps(dst))
    # The stamps naming it (who retired or closed a row, and the records of
    # other conversations saying it): the chosen thread did those now, so a
    # later forget of the folded topic undoes nothing.
    for key in ("superseded_in", "resolved_in", "scoped_in"):
        await conn.execute(
            f"UPDATE entity_memory SET metadata = jsonb_set(metadata, '{{{key}}}',"
            f" (metadata->'{key}') || $3::jsonb)"
            f" WHERE entity_id = $1 AND metadata->'{key}'->>'kind' = 'thread'"
            f"   AND metadata->'{key}'->>'ref' = $2", entity_id, sref, json.dumps(dst))
    for key in ("held_in", "retired_also_in", "resolved_also_in"):
        await conn.execute(
            f"UPDATE entity_memory SET metadata = jsonb_set(metadata, '{{{key}}}', ("
            f"   SELECT jsonb_agg(CASE WHEN e->>'kind' = 'thread' AND e->>'ref' = $2"
            f"                         THEN e || $3::jsonb ELSE e END)"
            f"     FROM jsonb_array_elements(metadata->'{key}') e))"
            f" WHERE entity_id = $1 AND jsonb_typeof(metadata->'{key}') = 'array'"
            f"   AND EXISTS (SELECT 1 FROM jsonb_array_elements(metadata->'{key}') e"
            f"                WHERE e->>'kind' = 'thread' AND e->>'ref' = $2)",
            entity_id, sref, json.dumps(dst))
    from mastercode.memory_slots import resync_scopes
    await resync_scopes(conn, entity_id)
    await conn.execute(
        "UPDATE circles_threads SET deleted_at = now(), merged_into = $3"
        " WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL",
        sref, entity_id, f"{dst['kind']}:{dst['ref']}")
