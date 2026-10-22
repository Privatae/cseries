"""A turn's reply pieces and its live trace: joining pieces, tool-call chips, the trace a page reads,
the reply a turn sent and what it held.

Moved out of runtime/session.py, which re-exports every name defined here."""

import logging
from mastercode.runtime.turn_holds import held_turn_write

log = logging.getLogger("cseries.mastercode.entity_runtime")


# ── Live turn trace ──────────────────────────────────────────────────
# What a RUNNING turn has done so far, so a turn that never reaches its
# own bottom can still leave a record. Same shape as the nudge queue
# (turn_input.py) and for the same reason: turn_registry lives in the server
# process and has no handle on the runtime's locals, so a module-level
# dict keyed by session_id is the seam.
#
# WHY: a cancelled turn left NO trace.
# turn_registry marks the record cancelled and the runtime task is torn
# down before session.py's assistant-row write at the bottom of
# send_message, so the next turn's history (recall_recent_turns) has no
# assistant row for it at all. Tool calls that COMPLETED inside the
# cancelled turn — an artifact created, a set_desktop refused — were
# invisible to the next turn, and the host would cheerfully redo them or
# deny they had happened.
#
# KEYED BY TURN TOKEN, not session_id. A session_id spans every
# turn in a thread, and there are exit paths that do not clear — the
# registry's own crash branch — so a
# session-keyed trace from a dead turn was returned for the NEXT turn in that
# session and its tool calls attributed to it. The token is minted per
# send_message call and rides the session frame the registry already reads, so
# a leftover can only ever be evicted, never returned.
_live_turn_traces: dict[str, dict] = {}

# Leaked entries the trace table keeps, over the turns running now. A turn that
# dies without clearing leaks one entry; the token key means it can never be
# MIS-READ, but it would still accumulate over a long-lived pod. Oldest-first
# eviction past this many plus the turns running (there is no count of turns:
# host_memory), so a running turn's trace is never the one evicted.
_MAX_LIVE_TRACES = 64


# The separator between the pieces of one reply — each piece of visible text
# the turn sends (a completion's text, the dispatch narration): the stream
# emits it before a piece's first text when text already went out, and the
# stored row joins the pieces with it, so the live text, the stored row and
# what the end-to-end test reads from the frames are one string (the single
# recorder is send_message._send).
REPLY_SEP = "\n\n"


def join_pieces(pieces) -> str:
    """Reply pieces as one string: REPLY_SEP between pieces, empty ones left
    out — except a piece marked `glue` (the continuation of a reply the output
    limit cut), which joins the piece before it with nothing:
    the cut can fall in the middle of a word or a line of code."""
    out = ""
    for p in pieces or []:
        t = str(p.get("text") or "") if isinstance(p, dict) else ""
        if not t:
            continue
        if out and not p.get("glue"):
            out += REPLY_SEP
        out += t
    return out


def join_reply(drafts, final: str) -> str:
    """Pieces of a reply joined with REPLY_SEP, empty ones left out — every
    piece is kept ("Done" then "Done — all three checks pass." are two things
    the person read)."""
    return REPLY_SEP.join(p for p in [*(drafts or []), final or ""] if p)


WM_MARKER = "## Working Memory"


def strip_wm(text: str) -> str:
    """One reply part with a parroted Working Memory block removed — the
    block runs to the first blank line after its [HEAT: line; a part that is
    nothing else becomes empty. Applied to each part before join_reply, so a
    part after a streamed draft is cleaned too."""
    t = text or ""
    if not t.lstrip().startswith(WM_MARKER):
        return t
    t = t.lstrip()
    heat = t.find("[HEAT:")
    end = t.find("\n\n", heat) if heat >= 0 else -1
    return t[end:].strip() if 0 < end < len(t) - 10 else ""


def tool_call_chip(ta: dict) -> dict:
    """The bounded per-call record that rides a turn row's
    metadata.tool_calls — what a chat card replays and what the next
    turn's tool trail reads back (runtime/tool_trail.py).

    ONE writer. This was inline at the bottom of send_message; the
    cancelled-turn path needs the identical shape, and two copies of a
    chip builder is how the replay card and the trail drift apart.
    """
    args = ta.get("args") or {}
    summary = ""
    for k in ("intent", "message", "query", "pattern",
              "name", "path", "key", "search"):
        v = args.get(k)
        if isinstance(v, str) and v:
            summary = v[:120]
            break
    chip = {
        "tool": ta.get("tool"),
        "args_summary": summary,
        "status": ta.get("result_status", "ok"),
        "error": (ta.get("error_detail") or "")[:300] or None,
    }
    # Differentiate agent-vs-tool at the data model (spec "Agent UI",
    # docs/host-orchestrator-turn.md): a dispatch is an AGENT the host
    # spawned, not a tool it ran. The chip carries kind + task_id so both
    # live render and replay can show it distinctly. task_id also rides
    # BUILD chips (kind stays "tool") — the run-surface terminal mounts
    # off it on reload.
    if ta.get("tool") == "dispatch":
        chip["kind"] = "agent"
    if ta.get("task_id") is not None:
        chip["task_id"] = ta["task_id"]
    # Compare chips carry their session id — the per-variant terminals
    # remount off it.
    if ta.get("session_id"):
        chip["session_id"] = ta["session_id"]
    # Tool trail fields (runtime/tool_trail.py): the record the next turn
    # reads back. Bounded at digest time. `op` is also what a replayed
    # chat card titles itself by ("<Tool> <op>", circles-web
    # tool-card.tsx) — one writer.
    # `operate_tool` is the inner tool of an operate call — the
    # lane every write takes — so the trail can say WHAT was operated and the
    # skill gate can see a desktop write (a key the digest produces and this
    # whitelist drops never reaches a persisted chip).
    # `read_tool` is the same for a search-mode read(tool=X): the tool read,
    # which the next turn's pick and trail read back (the same rule).
    # A groups-mode call never carries it, so its chip is unchanged.
    for k in ("op", "operate_tool", "read_tool", "provider", "harness", "next_step", "digest"):
        if ta.get(k) not in (None, "", {}):
            chip[k] = ta[k]
    return chip


def _bug_verdict_number(record: dict) -> int | None:
    """The report number a finished validator agent recorded a verdict on, or
    None when this run was not one. Read from the run's own action record
    (`tool_actions`, written by scoped_agent from what it EXECUTED, never from
    the agent's text): exactly one successful `bug_review` op='verdict'. The
    delivery for such a run is a fixed sentence — runtime/bug_review.py."""
    hits: list[int] = []
    for ta in (record or {}).get("tool_actions") or []:
        if not isinstance(ta, dict) or ta.get("tool") != "bug_review":
            continue
        args = ta.get("args") if isinstance(ta.get("args"), dict) else {}
        if args.get("op") != "verdict" or str(ta.get("result_status") or "") != "ok":
            continue
        try:
            hits.append(int(args.get("number")))
        except (TypeError, ValueError):
            continue
    # An exact repeat of the same call is served from the tool cache and
    # recorded twice; it is still one verdict on one report.
    return hits[0] if len(set(hits)) == 1 else None


def _bug_validator_number(record: dict) -> int | None:
    """The report a validator agent READ, whether or not it reached a verdict.
    A run that touched bug_review at all is a validator run: its own words are
    never delivered or stored (runtime/bug_review.py)."""
    for ta in (record or {}).get("tool_actions") or []:
        if isinstance(ta, dict) and ta.get("tool") == "bug_review":
            args = ta.get("args") if isinstance(ta.get("args"), dict) else {}
            if args.get("op") in ("read", "verdict"):
                try:
                    return int(args.get("number"))
                except (TypeError, ValueError):
                    return 0
    return None


def publish_turn_trace(turn_token: str, *, tool_actions: list,
                       project_id=None, tags: dict | None = None) -> None:
    """Record what the running turn has done, for the stopped-turn row.
    Never raises and never blocks: a trace that can break the turn it
    watches is worse than no trace."""
    if not turn_token:
        return
    try:
        chips = []
        for ta in (tool_actions or []):
            if isinstance(ta, dict):
                chips.append(tool_call_chip(ta))
        from mastercode.runtime.host_memory import in_flight_turns
        if (turn_token not in _live_turn_traces
                and len(_live_turn_traces) >= _MAX_LIVE_TRACES + in_flight_turns()):
            # Evict the oldest. dicts preserve insertion order, and a turn that
            # leaked its entry can never be read back anyway (wrong token) — it
            # is only occupying space.
            _live_turn_traces.pop(next(iter(_live_turn_traces)), None)
        _prev = _live_turn_traces.get(turn_token) or {}
        _live_turn_traces[turn_token] = {
            "tool_calls": chips,
            "project_id": project_id,
            "tags": dict(tags or {}),
            # kept across publishes: the turn's reply is already written
            # (held, calls_kept, held_task, held_text), and what it has sent
            # (pieces, reply_at — note_turn_reply)
            **{k: _prev[k] for k in _TRACE_KEPT if k in _prev},
        }
    except Exception as e:  # noqa: BLE001
        log.debug("turn trace publish failed (non-fatal): %s", e)


_TRACE_KEPT = ("held", "calls_kept", "held_task", "held_text", "pieces", "reply_at",
               "go_ahead_now")


def note_turn_reply(turn_token: str, pieces: list, reply_at, *,
                    project_id=None, tags: dict | None = None, record=None) -> None:
    """The turn sent its first visible text: from here its trace
    carries what it sends — the recorder's own list of pieces, by reference,
    so it is never copied per delta — so a Stop outside a hold, or a model
    error mid-turn, stores what the person saw (keep_sent_reply) instead of a
    note with none of it. `record`: the turn's go-ahead record for what it
    has sent so far (a callable, read when the row is written),
    so that row carries metadata.go_ahead like every other Host row."""
    if not turn_token:
        return
    try:
        cur = _live_turn_traces.setdefault(turn_token, {
            "tool_calls": [], "project_id": project_id, "tags": dict(tags or {})})
        cur["pieces"] = pieces
        cur["reply_at"] = reply_at
        if record is not None:
            cur["go_ahead_now"] = record
    except Exception as e:  # noqa: BLE001
        log.debug("turn trace reply note failed (non-fatal): %s", e)


def sent_reply(trace: dict | None) -> str:
    """What the turn sent so far, joined as its stored reply is joined."""
    pieces = (trace or {}).get("pieces") or []
    return join_pieces([p for p in pieces if isinstance(p, dict)])


def keep_sent_reply(pool, entity_id, session_id, token: str, trace: dict | None, *,
                    ended: str):
    """Store the reply the person saw when the turn ends before writing its
    own row: a Stop outside a hold (turn_registry) or a model error mid-turn
    (send_message). The turn's held row is rewritten in place when it holds
    less than was sent; when it already holds every piece, or nothing was
    sent, nothing is written. Returns the write's task, or None.

    The row carries the go-ahead record for exactly the text it stores:
    the documented measures read only rows that have one, and
    a held row rewritten in place keeps any key the rewrite does not replace
    (memory_engine merges metadata), so a record taken at the hold, for
    shorter text, would otherwise stay on it."""
    text = sent_reply(trace)
    if not text.strip() or not token:
        return None
    trace = trace or {}
    if trace.get("held_text") == text:
        return None
    tags = {**(trace.get("tags") or {}), "ended": ended}
    _rec_now = trace.get("go_ahead_now")
    if callable(_rec_now):
        try:
            _rec = _rec_now()
        except Exception as e:  # noqa: BLE001 — the reply is stored either way
            log.debug("go-ahead record for the kept reply failed (non-fatal): %s", e)
            _rec = None
        if _rec:
            tags["go_ahead"] = _rec
    return held_turn_write(
        pool, entity_id, session_id, text, token=token, held=False,
        after=trace.get("held_task"), project_id=trace.get("project_id"),
        tags=tags, created_at=trace.get("reply_at"))


def clear_turn_held(turn_token: str) -> None:
    """The turn resumed: it is no longer waiting on anything."""
    cur = _live_turn_traces.get(turn_token) if turn_token else None
    if isinstance(cur, dict):
        # calls_kept stays: the reply row written before the hold carries the
        # calls before it, so a later Stop lists only the ones after.
        cur.pop("held", None)


def note_turn_held(turn_token: str, waiting: str, calls_kept: int,
                   project_id=None, tags: dict | None = None,
                   held_task=None, held_text: str | None = None) -> None:
    """The turn wrote its reply so far and is holding: a Stop now keeps that
    reply, and the stopped-turn note says what the
    turn was waiting on and lists only the calls made after the reply was
    written (the reply row carries the ones before). `held_task` /
    `held_text`: that row's write and the text it holds — a Stop after the
    turn resumed and sent more rewrites it in place (keep_sent_reply)."""
    if not turn_token:
        return
    try:
        cur = _live_turn_traces.setdefault(turn_token, {
            "tool_calls": [], "project_id": project_id, "tags": dict(tags or {})})
        cur["held"] = (waiting or "")[:300]
        cur["calls_kept"] = int(calls_kept)
        if held_task is not None:
            cur["held_task"] = held_task
            cur["held_text"] = held_text
    except Exception as e:  # noqa: BLE001
        log.debug("turn trace hold note failed (non-fatal): %s", e)


def take_turn_trace(turn_token: str) -> dict | None:
    """Pop THIS turn's trace, or None when it has none.

    None is a meaningful answer and the caller must honour it: it means
    either that no tool call completed before the stop, or that the turn
    already wrote its own assistant row and cleared. In both cases a
    stopped-turn row would be wrong — empty in the first, a duplicate
    contradicting the real reply in the second.

    Popping also means a duplicate teardown cannot write a second row.
    """
    if not turn_token:
        return None
    return _live_turn_traces.pop(turn_token, None)


def clear_turn_trace(turn_token: str) -> None:
    """Drop the trace for a turn that ended on its own terms — normally, or
    through an early return that already wrote its own row. Every exit path
    out of send_message that is not a cancellation calls this."""
    if turn_token:
        _live_turn_traces.pop(turn_token, None)
