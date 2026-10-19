"""
server/error_log.py — the ONE error recorder.

Failed tool calls should not be shown to the user; errors are logged so they
can be diagnosed quickly without spending tokens hunting for them. Entity
container logs die with the pod (entity namespaces are
deliberately NOT shipped to central Loki — sovereignty), and the one existing
hook (session_manifest.record_error) writes to a module absent from the
image. This module replaces that void: every failure a choke point sees
lands as a row in pod-PG `error_events` (v098; survives restarts on the
pgdata volume) AND as an `ERROR_EVENT` log line for anyone tailing the pod.

Call sites (keep this list current — it IS the coverage map):
  - session.py host tool loop           → source='tool'
      through record_tool_result_later(), from the per-call ledger note
      (_ledger_note), which every tool call passes — including the
      dedup refusal and the ephemeral write block, which `continue` before
      the activity block
  - (source='sdk_tool' is historical — the deleted Claude-subscription
     host engine wrote it; old rows may still carry it)
  - turn_registry crash/timeout         → source='turn'
  - brain_service stream crash          → source='stream'

WHY THE TOOL LANE WAS ONCE SILENT.
The host tool loop's call to this recorder sat inside session.py's
`if _prompt_console:` block — the live prompt-console telemetry hook, which
nothing in the image ever wires (triage.set_prompt_console has no caller), so
the call was dead from the day it was written. The only rows the lane ever
held came from the SDK engine's wrappers (source='sdk_tool'); that engine
stopped being used and was deleted, and the
lane went quiet while a live Host logged tool errors daily in tool_call_log. The
coverage gate (scripts/test_error_lane.py) only checked that the import text
was present. scripts/test_error_lane_records.py now checks that the call is
REACHABLE: no recorder call may sit under a telemetry gate.

Contract: record() NEVER raises and never blocks the caller's failure path —
an error-logging failure must not compound the error being logged. Reads go
through GET /api/system/errors/recent (server/routes/system.py) — that
endpoint, not log spelunking, is where diagnosis starts.

Nothing is cut silently: a message or context value over its cap keeps its
head and says how many characters were left out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

log = logging.getLogger("cseries.error_log")

# An error is read in full where it is diagnosed. 16 KiB holds a whole stack
# trace or a tool's complete error body; past it the row says how much was
# left out rather than ending mid-sentence with no sign.
_MSG_CAP = 16_384
_CTX_CAP = 1500
_LOG_PREVIEW = 300
_RETENTION_DAYS = 30

# On-write retention, throttled: at most one prune per hour per process.
# Errors are (supposed to be) rare — a cron would be more machinery than the
# volume justifies, and a dead cron is a silent retention failure.
_last_prune = 0.0

# Tool-result statuses that are FAILURES, and the kind each is recorded as.
# Everything else a tool answers — ok, dispatched (an agent started),
# not_found (an honest "there is no such key"), ungrounded (a research answer
# with no source), pending — is an outcome, not an error, and is not
# recorded: the end-to-end test's errors_clean check reads every row here as a
# failure of the run.
_TOOL_FAILURE_KINDS = {
    "error": "error",
    "denied": "denied",
    "blocked": "denied",
    "unavailable": "unavailable",
}

# Tasks scheduled by record_tool_result_later(), held until done so the event
# loop cannot garbage-collect a pending INSERT (asyncio keeps only weak
# references) — the same discipline as runtime/tool_call_ledger.record_later.
_PENDING: set = set()


def _preview(value: Any, cap: int) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        try:
            s = str(value)
        except Exception:
            s = f"<unprintable {type(value).__name__}>"
    if len(s) <= cap:
        return s
    return s[:cap] + f"… [{len(s) - cap} more characters not kept]"


async def record(pool, *, source: str, message: str, kind: str = "error",
                 tool: Optional[str] = None, **context: Any) -> None:
    """Land one error event. Fire-and-forget safe: swallows everything after
    logging the ERROR_EVENT line (which fires even when the DB write can't —
    the log line is the floor, the row is the goal)."""
    global _last_prune
    msg = _preview(message, _MSG_CAP)
    # The ROW keeps the whole message (the Host's own database, read through
    # GET /api/system/errors/recent). The pod LOG line is a short pointer to
    # it: a tool error can quote the person's own data, and pod logs are read
    # by operators and may be collected off the Host, so the text itself
    # stays in the row.
    log.warning("ERROR_EVENT source=%s kind=%s tool=%s msg=%s",
                source, kind, tool or "-", _preview(msg, _LOG_PREVIEW))
    if pool is None:
        return
    try:
        ctx = {k: _preview(v, _CTX_CAP) for k, v in context.items() if v is not None}
        await pool.execute(
            "INSERT INTO error_events (source, kind, tool, message, context) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            source, kind, tool, msg, json.dumps(ctx))
        now = time.monotonic()
        if now - _last_prune > 3600:
            _last_prune = now
            await pool.execute(
                "DELETE FROM error_events WHERE at < now() - make_interval(days => $1)",
                _RETENTION_DAYS)
    except Exception as e:
        log.warning("error_events write failed (non-fatal, line above is the record): %s", e)


def tool_failure(result: Any) -> Optional[tuple[str, str]]:
    """(kind, message) when a tool result is a FAILURE, else None.

    Reads the envelope every tool returns ({status, result, reason?}). A
    result served from this turn's dedup cache (`cached`) is None: the first
    call already recorded it, and recording the repeat would count one
    failure twice."""
    if not isinstance(result, dict) or result.get("cached"):
        return None
    status = str(result.get("status") or "ok")
    kind = _TOOL_FAILURE_KINDS.get(status)
    if kind is None:
        return None
    for key in ("result", "error", "message"):
        body = result.get(key)
        if body not in (None, ""):
            break
    else:
        body = result
    message = body if isinstance(body, str) else _preview(body, _MSG_CAP)
    if not message.strip():
        message = f"(the tool answered status '{status}' with no text)"
    return kind, message


async def record_tool_result(pool, *, tool: str, result: Any, source: str = "tool",
                             session_id: Optional[str] = None, args: Any = None,
                             **context: Any) -> bool:
    """The tool-call choke point's ONE call: records the result iff it is a
    failure (tool_failure). True when a failure was recorded. Never raises."""
    try:
        failed = tool_failure(result)
        if failed is None:
            return False
        kind, message = failed
        reason = result.get("reason") if isinstance(result, dict) else None
        await record(pool, source=source, kind=kind, tool=tool, message=message,
                     session_id=session_id,
                     args=args if isinstance(args, dict) else ({} if args is None else args),
                     status=str(result.get("status")), reason=reason, **context)
        return True
    except Exception as e:  # noqa: BLE001 — the recorder never compounds a failure
        log.warning("error-lane tool record failed (non-fatal): %s", e)
        return False


def record_tool_result_later(pool, **kw: Any) -> Optional[asyncio.Task]:
    """record_tool_result scheduled as its own task, so the tool loop never
    waits on the pool for it. Nothing is scheduled for a result that is not a
    failure. Returns the task (tests await it), or None. Never raises."""
    try:
        if tool_failure(kw.get("result")) is None:
            return None
        loop = asyncio.get_running_loop()
        task = loop.create_task(record_tool_result(pool, **kw))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
        return task
    except Exception as e:  # noqa: BLE001
        log.warning("error-lane tool record not scheduled (non-fatal): %s", e)
        return None
