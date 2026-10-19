"""Where a Workbench turn lives: the project its WORK touched, not the window
it was typed into.

Measured: the user typed the ask
into a stub project's thread, the host drafted the plan on a second project it
created, and from then on every reply and every "yes" stayed in the first while every
build report landed in the second — two hours of one conversation split across two
threads. The reply was written to the project the window sent
(session.py `_row_project_id`), the report to the project the build ran on,
and nothing reconciled them.

The rule this module implements: a turn's home is the project its WRITE tools
acted on. From a chat turn today that surface is build / ladder.run (the
host's own doors) and create_project through `operate` from
Circles; the other entries in _WRITE_TOOLS run on the dispatch lane, whose
calls never enter a chat turn's records — they are listed so a lane move does
not silently lose re-homing (check_tool_lanes.py ratchets the lane table). When exactly one such project differs from the window's, the
exchange (the user message and the reply) is re-homed to it, a marker turn is
left where it was typed, and the client is told so it can follow. Reads never
move a turn — "compare with project 5" is conversation about 5, not work in 5.
Two different projects written in one turn is ambiguous, so the turn stays.

This is decided from the tool records the turn already keeps (ta_entry in
session.py: tool, args, digest), never from the model's prose.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("cseries.turn_home")

# Tools whose call WRITES into a project, and where the project id sits in the
# call. `args` = args.project_id. `create` = the created id from the digest
# (tool_trail) or the result. An op set narrows a multi-op tool to its write
# ops; None means every op of that tool is a write.
_WRITE_TOOLS: dict[str, tuple[str, frozenset[str] | None]] = {
    # run/plan/build land work in the project; stop/continue-as-check-in are
    # control of a run and typed as asides ("stop the build on 300").
    # continue IS a seeded build pass, so it counts; run is a build or a
    # compare (the multi-engine door review.fan_out was, before 2026-09-22).
    "build": ("args", frozenset({"run", "plan", "build", "continue"})),
    "ladder.run": ("args", None),
    "ship_project_app": ("args", None),
    "publish_site": ("args", None),
    "set_project_manifest": ("args", None),
    "update_project": ("args", None),
    "project_phase": ("args", frozenset({
        "add_phase", "set_phase_spec", "archive_phase", "add_step", "set_step_status"})),
    "workspace_file": ("args", frozenset({"write", "delete"})),
    "project_adopt_thread": ("args", None),
    "create_project": ("create", None),
}

# `operate` wraps a registry op: {tool, args, intent}. Its digest carries the
# op's own project_id (tool_trail); its args carry the op name + args.
_OPERATE = "operate"


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _project_of_action(ta: dict) -> int | None:
    """The project id one tool record wrote to, or None when it did not write."""
    tool = str(ta.get("tool") or "")
    args = ta.get("args") if isinstance(ta.get("args"), dict) else {}
    digest = ta.get("digest") if isinstance(ta.get("digest"), dict) else {}
    status = str(ta.get("result_status") or "ok")
    if status not in ("ok", "unknown"):
        # A refused or failed write moved nothing.
        return None

    if tool == _OPERATE:
        inner = str(args.get("tool") or "")
        inner_args = args.get("args") if isinstance(args.get("args"), dict) else {}
        spec = _WRITE_TOOLS.get(inner)
        if spec is None:
            return None
        how, ops = spec
        if ops is not None and str(inner_args.get("op") or "") not in ops:
            return None
        if how == "create":
            return _int_or_none(digest.get("project_id"))
        return _int_or_none(inner_args.get("project_id")) or _int_or_none(digest.get("project_id"))

    spec = _WRITE_TOOLS.get(tool)
    if spec is None:
        return None
    how, ops = spec
    # `build` keys its op on `action`; every other multi-op tool on `op`.
    # Normalized the way _exec_build reads it (strip + lower), so "Plan "
    # is the same write the executor ran.
    raw_op = args.get("action") if tool == "build" else args.get("op")
    op_value = str(raw_op or "").strip().lower()
    if ops is not None and op_value not in ops:
        return None
    if how == "create":
        # The chat loop keeps no `result` on the record (session.py ta_entry
        # carries it for image_generate's generate only); the tool trail's
        # digest is where a created id lives (tool_trail.digest_tool_action).
        return _int_or_none(digest.get("project_id"))
    return _int_or_none(args.get("project_id"))


def written_projects(tool_actions: list[dict] | None) -> set[int]:
    """Every project id the turn's tool calls WROTE to."""
    out: set[int] = set()
    for ta in tool_actions or []:
        if not isinstance(ta, dict):
            continue
        pid = _project_of_action(ta)
        if pid is not None:
            out.add(pid)
    return out


def decide_turn_home(window_project_id: int | None,
                     tool_actions: list[dict] | None) -> int | None:
    """The project this turn should be filed under when it is NOT the window's.

    None = stay where it was typed. That is the answer whenever the window is
    not a project (a conversation outside a project stays whole where it was
    typed — create_project only leaves a marker in the new project), when
    nothing was written, when the only writes were to this project, or when
    two different projects were written (ambiguous — never guess).
    """
    if window_project_id is None:
        return None
    others = {p for p in written_projects(tool_actions) if p != window_project_id}
    if len(others) != 1:
        return None
    return next(iter(others))


def marker_text(to_project_id: int, to_name: str | None) -> str:
    label = f"project #{to_project_id}" + (f" “{to_name}”" if to_name else "")
    return (f"→ This exchange moved to {label} — that is the project it worked on. "
            "Nothing was deleted; the conversation continues in that project's thread.")


async def rehome_turn(pool, entity_id: int, *, from_project: int, to_project: int,
                      user_row_task: "asyncio.Task | None", session_id: str,
                      user_content: str | None = None,
                      turn_started_at: datetime | None = None) -> dict:
    """Retag the turn's USER row to `to_project` and leave a marker in
    `from_project`. The assistant row is written by the caller with the new
    home directly; only the user row (already queued before the tools ran)
    needs moving. Awaits the user row's write so the retag cannot race it.

    Returns {moved_user_row: bool, marker: bool, to_name}. Never raises: a
    failure here leaves the thread where it was typed, which is the pre-2026-09
    behaviour, and logs why.
    """
    out = {"moved_user_row": False, "marker": False, "to_name": None}
    try:
        row = await pool.fetchrow(
            "SELECT name FROM projects WHERE id=$1 AND entity_id=$2", to_project, entity_id)
        to_name = (row["name"] or "").strip() if row else None
        out["to_name"] = to_name or None

        user_row_id = None
        if user_row_task is not None:
            try:
                user_row_id = await asyncio.wait_for(asyncio.shield(user_row_task), timeout=30)
            except Exception as e:  # noqa: BLE001 - fall back to a bounded lookup
                log.info("rehome: user row task did not resolve (%s); locating by content", e)
        if user_row_id is None and user_content is not None and turn_started_at is not None:
            user_row_id = await pool.fetchval(
                """
                SELECT id FROM entity_memory
                 WHERE entity_id=$1 AND memory_type='turn' AND project_id=$2
                   AND metadata->>'role'='user' AND content=$3
                   AND created_at >= $4
                 ORDER BY created_at DESC LIMIT 1
                """, entity_id, from_project, user_content, turn_started_at)

        new_sctx = json.dumps({"kind": "project", "ref": str(to_project)})
        adopted_from = json.dumps({"kind": "project", "ref": str(from_project)})
        if user_row_id is not None:
            res = await pool.execute(
                """
                UPDATE entity_memory
                   SET project_id = $2,
                       metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                           'source_context', $3::jsonb,
                           'adopted_from',   $4::jsonb,
                           'adopted_at',     to_jsonb(now()::timestamptz),
                           'rehomed',        true)
                 WHERE id = $1 AND entity_id = $5 AND project_id = $6
                """, user_row_id, to_project, new_sctx, adopted_from, entity_id, from_project)
            out["moved_user_row"] = str(res).endswith(" 1")
        if not out["moved_user_row"]:
            # Nothing moved (PII mode returns no row id; the row was not where
            # it was expected; the DB refused) → no marker, and the caller
            # keeps the whole turn in the source. Half a move is the split.
            log.info("rehome: entity=%d user row not moved (%s) — turn stays in project %d",
                     entity_id, user_row_id, from_project)
            return out

        marker_md = {
            "role": "assistant",
            "source_kind": "thread_marker",
            "source_context": {"kind": "project", "ref": str(from_project)},
            "app_id": "workbench",
            "marker": {"project_id": to_project, "project_name": to_name,
                       "moved_turns": 2, "rehomed": True},
        }
        await pool.execute(
            """
            INSERT INTO entity_memory
              (entity_id, content, memory_type, importance, status,
               source_session, project_id, metadata, created_at)
            VALUES ($1, $2, 'turn', 0.1, 'active', $3, $4, $5::jsonb, $6)
            """,
            entity_id, marker_text(to_project, to_name),
            f"rehome:{to_project}", from_project, json.dumps(marker_md),
            datetime.now(timezone.utc))
        out["marker"] = True
        log.info("rehome: entity=%d turn moved project %d -> %d (user_row=%s)",
                 entity_id, from_project, to_project, user_row_id)
    except Exception as e:  # noqa: BLE001 - never a failed turn
        log.warning("rehome failed (turn stays in project %s): %s", from_project, e)
    return out
