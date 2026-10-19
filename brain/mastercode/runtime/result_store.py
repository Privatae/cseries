"""mastercode.runtime.result_store — big tool results stay in the database.

Measured: tool output piled up inside a turn and rode
every later call. One turn sent 6,068,805 input tokens for 6,031 out, most of
it earlier tool output; in another the cut that kept that in check
(context_budget.drop_used_tool_results) replaced the dispatched agent's own
report with a stub that said to call the tool again, and the Host went on
without it.

A result over STORE_OVER_CHARS is written once, whole, to the Host's own pod
Postgres (turn_tool_results, v223), keyed by the turn and the call. The turn's
message list carries its first PREVIEW_CHARS, the handle, the total size and
how to read on: read(handle, offset, length) pages it, exact characters. A
result the cut takes out after it was used leaves the same handle behind
(context_budget.dropped_result_stub). A stored row never changes, so a
preview or a stub sent once is sent the same way on every later call and the
cached prefix holds.

The threshold, from one Host's own results (context ledger, 30 days, 1,326 results over 287 turns): median 1,749 chars, p75 7,685,
p90 16,592, p95 36,764, p99 138,714, max 258,351. Over 8,000 are 24% of the
results and 84% of their characters: three results in four ride whole, the
quarter that carries most of the weight is stored.

Rows go when their session is no longer open (sweep, from
session_handler.cleanup): the session's message list is the only thing that
holds a handle.

Nothing leaves the pod.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("cseries.mastercode.result_store")

# Over this many characters (the result as serialized for the transcript) a
# result is stored and previewed. The measured p75 is 7,685 (module docstring).
STORE_OVER_CHARS = 8_000
# The first part the message list carries.
PREVIEW_CHARS = 2_000
HANDLE_PREFIX = "res-"
STORED_KEY = "stored_result"
# A session in one of these is still running turns; its rows stay.
OPEN_SESSION_STATUSES = ("active", "idle", "stale")
# A result whose session has no row: kept as long as a session can run with
# active use (a session ends after 1h inactivity, active use to 24h).
ORPHAN_KEEP_HOURS = 24


# Reads whose size the caller already chose, which reach the turn whole (the
# transcript wall still bounds them): a code_read or a forge read of a line
# range the Host asked for, and a code_grep page (at most 50 matches and
# 24,000 characters, tools._CODE_GREP_PAGE_CHARS). Storing them made reading a
# 300-line file one call plus up to three read(handle) pages, where Claude
# Code's Read returns up to 2,000 lines at once.
_RANGE_READ_OPS = {"code_read": None, "gitea": "read", "github": "read"}


def caller_sized(tool, args) -> bool:
    """Whether `tool` called with `args` is a read the caller sized itself:
    exempt from storing (STORE_OVER_CHARS)."""
    if tool == "code_grep":
        return True
    if tool not in _RANGE_READ_OPS or not isinstance(args, dict):
        return False
    op = _RANGE_READ_OPS[tool]
    if op is not None and str(args.get("op") or "").strip() != op:
        return False
    return args.get("start_line") is not None or args.get("end_line") is not None


def handle_of(row_id: int) -> str:
    return f"{HANDLE_PREFIX}{int(row_id)}"


def row_id_of(handle) -> int | None:
    """The row a handle names, or None for anything that is not one."""
    if not isinstance(handle, str):
        return None
    h = handle.strip()
    if not h.startswith(HANDLE_PREFIX):
        return None
    tail = h[len(HANDLE_PREFIX):]
    if not tail.isdigit():
        return None
    try:
        n = int(tail)
    except ValueError:
        return None
    return n if 0 < n < 2 ** 63 else None


def serialize(result) -> str:
    """A result as the transcript carries it, whole (tool_result_wall's
    serialization, before its wall)."""
    try:
        return json.dumps(result, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps({"status": "error", "result": "unserializable tool result"})


def is_result_read(func_name, func_args) -> bool:
    """A `read` call that pages a stored result: it names a handle and no
    tool."""
    if func_name != "read" or not isinstance(func_args, dict):
        return False
    if str(func_args.get("tool") or "").strip():
        return False
    return isinstance(func_args.get("handle"), str) and bool(func_args["handle"].strip())


# A dispatched agent's way to page its own stored results (scoped_agent): the
# same handle, offset and length as the Host's read door, and nothing else. An
# agent holds it from the round after its first result is stored, and may read
# only handles its own run wrote.
AGENT_READ_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "read",
        "description": ("Read more of one of your stored tool results: a result over "
                        f"{STORE_OVER_CHARS:,} characters reaches you as its first part and a "
                        "handle. Exact characters from offset."),
        "parameters": {
            "type": "object",
            "required": ["handle"],
            "properties": {
                "handle": {"type": "string", "description": "The stored result's handle (res-<n>)."},
                "offset": {"type": "integer", "description": "First character to return. Default 0."},
                "length": {"type": "integer",
                           "description": "How many characters. Default: as many as one result carries."},
            },
        },
    },
}


def read_more_line(handle: str, offset: int) -> str:
    return (f"read(handle='{handle}', offset={int(offset)}) returns the rest from there; "
            f"add length for less.")


def preview(tool: str, handle: str, text: str, preview_chars: int = PREVIEW_CHARS) -> str:
    """What the message list carries for a stored result: its first part,
    the handle, its size and how to read on. JSON, like every tool result."""
    part = text[:preview_chars]
    body = {"handle": handle, "tool": tool, "chars": len(text),
            "preview": part, "next_offset": len(part),
            "read_more": read_more_line(handle, len(part))}
    return json.dumps({STORED_KEY: body}, default=str)


def is_preview(content) -> bool:
    return isinstance(content, str) and content.startswith('{"' + STORED_KEY + '"')


async def put(pool, *, entity_id: int, session_id, turn_token: str, tool_call_id: str,
              tool: str, text: str) -> str | None:
    """Write one result, whole; returns its handle, or None when it could not
    be written (the caller keeps the result in the turn as before). A second
    write for the same turn and call returns the first row's handle and
    changes nothing."""
    if pool is None or not turn_token or not tool_call_id or not isinstance(text, str):
        return None
    try:
        row_id = await pool.fetchval(
            "INSERT INTO turn_tool_results"
            " (entity_id, session_id, turn_token, tool_call_id, tool, chars, content)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)"
            " ON CONFLICT (turn_token, tool_call_id) DO UPDATE"
            " SET turn_token = turn_tool_results.turn_token"
            " RETURNING id",
            int(entity_id), (str(session_id) if session_id else None), str(turn_token),
            str(tool_call_id), str(tool or "tool")[:200], len(text), text)
    except Exception as e:  # noqa: BLE001 — the turn goes on with the result inline
        log.warning("result store write failed (kept in the turn): %s", e)
        return None
    return handle_of(row_id) if row_id else None


def _int_arg(args: dict, key: str):
    v = args.get(key)
    if v is None:
        return None, None
    if isinstance(v, bool):
        return None, f"`{key}` must be an integer."
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None, f"`{key}` must be an integer."
    if n < 0:
        return None, f"`{key}` must be 0 or more."
    return n, None


async def read_page(pool, entity_id: int, args: dict, *, session_id=None,
                    turn_token=None) -> dict:
    """read(handle, offset, length): exact characters of a stored result,
    from `offset` (0 by default), `length` of them or as many as one result
    carries (tool_result_wall.read_page_cap). A tool result dict; never
    raises.

    Scoped to the reader: a handle reads only a row its own session wrote
    (`session_id`; the Host's turns), or, with no session, its own run or
    turn (`turn_token`; a dispatched agent, a session-less turn). Handles are
    sequential ids, so the entity alone is no wall: a Host turn could read a
    result another of its sessions stored, an eval or sandbox session the
    person's own, or the raw untrusted content an agent stored, which is the
    content the agent was dispatched to keep out of the Host's context. With
    neither, nothing is read."""
    a = args if isinstance(args, dict) else {}
    sid = str(session_id).strip() if session_id else ""
    tok = str(turn_token).strip() if turn_token else ""
    if not sid and not tok:
        return {"status": "error", "reason": "not_found",
                "result": "No stored result can be read here: this run has no session."}
    handle = str(a.get("handle") or "").strip()
    row_id = row_id_of(handle)
    if row_id is None:
        return {"status": "error", "reason": "args_schema",
                "result": f"`{handle}` is not a stored result's handle (they look like "
                          f"'{HANDLE_PREFIX}123')."}
    offset, err = _int_arg(a, "offset")
    if err is None:
        length, err = _int_arg(a, "length")
    if err is not None:
        return {"status": "error", "reason": "args_schema", "result": err}
    offset = offset or 0
    from mastercode.runtime.tool_result_wall import fit_page, read_page_cap
    room = read_page_cap("chars")
    want = room if not length else min(length, room)
    try:
        row = await pool.fetchrow(
            "SELECT tool, chars, substr(content, $3, $4) AS part FROM turn_tool_results"
            " WHERE id = $1 AND entity_id = $2"
            + (" AND session_id = $5" if sid else " AND turn_token = $5"),
            row_id, int(entity_id), offset + 1, want, sid or tok)
    except Exception as e:  # noqa: BLE001
        log.warning("result store read failed: %s", e)
        return {"status": "error", "result": f"The stored result {handle} could not be read: {e}"}
    if row is None:
        return {"status": "error", "reason": "not_found",
                "result": (f"No stored result {handle} here: it belonged to a session that has "
                           f"ended or to another session or agent, or it never existed. "
                           f"Run the tool again.")}
    total = int(row["chars"])
    part = row["part"] or ""
    page = {"handle": handle, "tool": row["tool"], "chars": total, "offset": offset,
            "offset_unit": "chars", "content": part, "next_offset": offset + len(part)}
    fit_page(page)
    page["eof"] = page["next_offset"] >= total
    if page["eof"]:
        page["next_offset"] = None
    else:
        page["read_more"] = read_more_line(handle, page["next_offset"])
    return {"status": "ok", "result": page}


# A dispatched agent's run (session 'agent:<task_id>', entity_tasks.session_id)
# is still going in one of these (timeline._RUNNING_TASK).
OPEN_TASK_STATUSES = ("queued", "in_progress", "blocked")


async def forget(pool, *, turn_token: str) -> int:
    """Delete the rows one run or turn stored (a dispatched agent's, when it
    ends: nothing else holds its handles). Returns how many; never raises."""
    if pool is None or not turn_token:
        return 0
    try:
        status = await pool.execute(
            "DELETE FROM turn_tool_results WHERE turn_token = $1", str(turn_token))
        return int(str(status).split()[-1])
    except Exception as e:  # noqa: BLE001 — the sweep takes them later
        log.warning("result store forget failed (the sweep removes them): %s", e)
        return 0


async def sweep(pool) -> int:
    """Delete every stored result whose session or agent run is no longer
    open: its session row is closed, expired or ended, or its agent task
    (entity_tasks.session_id 'agent:<task_id>') has finished, or (neither row
    at all: a session whose row is written later, or never) the result is
    older than a session can run. Returns how many went; never raises."""
    try:
        status = await pool.execute(
            "DELETE FROM turn_tool_results r WHERE NOT EXISTS ("
            " SELECT 1 FROM entity_sessions s WHERE s.session_id = r.session_id"
            " AND s.status = ANY($1::text[]))"
            " AND NOT EXISTS (SELECT 1 FROM entity_tasks t WHERE t.session_id = r.session_id"
            " AND t.status = ANY($3::text[]))"
            " AND (EXISTS (SELECT 1 FROM entity_sessions s WHERE s.session_id = r.session_id)"
            " OR EXISTS (SELECT 1 FROM entity_tasks t WHERE t.session_id = r.session_id)"
            " OR r.created_at < now() - make_interval(hours => $2))",
            list(OPEN_SESSION_STATUSES), ORPHAN_KEEP_HOURS, list(OPEN_TASK_STATUSES))
    except Exception as e:  # noqa: BLE001
        log.warning("result store sweep failed: %s", e)
        return 0
    try:
        n = int(str(status).split()[-1])
    except (ValueError, IndexError):
        n = 0
    if n:
        log.info("result store sweep: %d stored result(s) of ended sessions removed", n)
    return n
