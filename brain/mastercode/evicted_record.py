"""evicted_record.py — what the Host itself wrote about turns that left the window.

When a conversation outgrows its history budget, its oldest turns stop riding
(runtime/session.py, chunked eviction). What rides in their place is built
here, from the Host's own words only: the close_turn record each of those
turns ended with (its handoff: next step, open work, identifiers verbatim; and
any goal it set or met), dated, newest first, plus how many earlier turns are
not shown and how to open them.

No model writes it. It replaces the rolling summary, which a separate call
outside the Host's turn rewrote from its own previous version every 60 rows,
stored unchecked and set ahead of the whole verbatim thread even when nothing
had overflowed. This block is built only when
turns actually fell out, and two builds over the same rows are byte-identical,
so it neither drifts nor moves the cached prefix while the cut point holds.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("cseries.mastercode.evicted_record")

from mastercode.context_budget import RECORD_PREFIX as PREFIX  # noqa: E402

# The block's ceiling, in characters (context_budget.CHARS_PER_TOKEN = 4, so
# about 1,500 tokens). The rolling summary it replaces aimed at 250 words
# (SUMMARY_TARGET_WORDS, about 1,300 characters) for a whole conversation; a
# close_turn handoff is one next step (at most close_turn._MAX_TEXT, 1,000
# characters), a few open items and its identifiers, so this carries at least
# the last few evicted turns' notes whole, and a note that would pass the
# ceiling is left out and counted, one chat_threads read away. The history
# budget reserves it before the window is fitted (session.py), so it never
# pushes a kept turn out after the fact.
RECORD_MAX_CHARS = 6_000
# At most this many notes are read; older ones are counted, not shown.
RECORD_MAX_NOTES = 20


def _md(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (TypeError, ValueError):
            return {}
    return v if isinstance(v, dict) else {}


def _day(ts) -> str:
    try:
        return ts.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return "undated"


def _note_lines(ct: dict) -> list[str]:
    """The parts of one close_turn record that say where the work stood."""
    out: list[str] = []
    goal = ct.get("goal") if isinstance(ct.get("goal"), dict) else {}
    if goal.get("set"):
        out.append(f"goal set: {goal['set']}")
    if goal.get("met") is True:
        out.append("goal met")
    ho = ct.get("handoff") if isinstance(ct.get("handoff"), dict) else {}
    if ho.get("next_step"):
        out.append(f"next step: {ho['next_step']}")
    if ho.get("open_work"):
        out.append("open work: " + "; ".join(str(x) for x in ho["open_work"]))
    if ho.get("identifiers"):
        out.append("identifiers: " + ", ".join(str(x) for x in ho["identifiers"]))
    if ct.get("topic"):
        out.append(f"topic: {ct['topic']}")
    return out


async def evicted_record(pool, entity_id: int, scope: Optional[dict], session_id: str, *,
                         oldest_kept_id: Optional[int], lane: Optional[str] = None) -> Optional[str]:
    """The block for the turns of `scope` older than the oldest turn row that
    still rides (`oldest_kept_id`; None: no turn rides, so every turn is
    older). None when no turn is older: nothing fell out, nothing rides.
    Read from the transcript's own scope filter (memory_engine._turn_row_where,
    as recall_recent_turns reads it), hidden lanes left out."""
    if not (isinstance(scope, dict) and scope.get("kind")):
        return None
    from mastercode.memory_engine import _turn_row_where
    args: list = [entity_id]
    where = _turn_row_where(args, session_id=session_id, source_context=scope)
    if lane:
        args.append(str(lane))
        where.append(f"metadata->>'task_lane' = ${len(args)}")
    if oldest_kept_id is not None:
        args.append(int(oldest_kept_id))
        k = len(args)
        where.append(f"(created_at, id) < (SELECT created_at, id FROM entity_memory WHERE id = ${k})")
    # Rows that could have ridden: the person's and the Host's turns (a
    # replay's variant, a marker or a note never rides the window).
    where.append("metadata->>'role' IN ('user', 'assistant')")
    cond = " AND ".join(where)
    try:
        older = int(await pool.fetchval(f"SELECT count(*) FROM entity_memory WHERE {cond}", *args) or 0)
        if older == 0:
            return None
        args.append(RECORD_MAX_NOTES)
        rows = await pool.fetch(
            f"SELECT id, created_at, metadata->'close_turn' AS ct FROM entity_memory"
            f" WHERE {cond} AND metadata->>'role' = 'assistant' AND metadata ? 'close_turn'"
            f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)}", *args)
    except Exception as e:  # noqa: BLE001 — history rides without the block
        log.warning("evicted record unavailable (entity %s): %s", entity_id, e)
        return None
    lines: list[str] = []
    used = len(PREFIX)
    shown = 0
    for r in rows:
        parts = _note_lines(_md(r["ct"]))
        if not parts:
            continue
        line = f"- [{_day(r['created_at'])}] " + " | ".join(parts)
        if used + len(line) + 1 > RECORD_MAX_CHARS:
            if shown:
                break
            # The newest note alone passes the ceiling: it rides cut, so the
            # block never claims there was no note.
            line = line[:max(RECORD_MAX_CHARS - used - 2, 0)] + "…"
        lines.append(line)
        used += len(line) + 1
        shown += 1
    kind, ref = str(scope.get("kind")), str(scope.get("ref") or "")
    tail = (f"{older} earlier turn row{'s' if older != 1 else ''} of this conversation "
            f"{'are' if older != 1 else 'is'} not shown here"
            + (f"; the notes above are the {shown} newest you wrote among them" if shown else
               "; you wrote no end-of-turn note among them")
            + f". chat_threads op='read' kind='{kind}' ref='{ref}' opens them in order.")
    return "\n".join([PREFIX, *lines, tail])


def is_record(message: Any) -> bool:
    return (isinstance(message, dict) and isinstance(message.get("content"), str)
            and message["content"].startswith(PREFIX))
