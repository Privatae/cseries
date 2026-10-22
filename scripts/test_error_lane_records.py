#!/usr/bin/env python3
"""
The error lane RECORDS a failed host tool call.

The defect. error_events held nothing newer than 2026-08-04 on any Host
while one Host's tool_call_log logged host-lane `error` rows daily (42 in 30
days, the newest that morning). Every row the lane ever held on one Host was
source='sdk_tool', written by the SDK engine's wrappers, which were deleted on
2026-08-19. The host tool loop's own call to the recorder (session.py,
source='tool') sat inside `if _prompt_console:`, the prompt-console
telemetry hook, which nothing wires (triage.set_prompt_console has no
caller), so the call never ran. scripts/test_error_lane.py checked only that
the import text was present in session.py, so it stayed green.

What this pins:
  1. REACHABLE: every call session.py makes into server.error_log runs
     whether or not a telemetry console is wired. No such call sits under an
     `if` that tests `_prompt_console`, and at least one exists.
  2. A failed tool result is recorded: status error, denied, blocked and
     unavailable each land one INSERT (source 'tool', the tool's name, the
     right kind). ok, dispatched, not_found, ungrounded and a dedup-cache
     repeat land nothing.
  3. The error is kept in full: a 5,000-character tool error lands whole,
     where the old call site cut it at 2,000. Past the 16 KiB cap the row
     says how many characters were left out.
  4. record_tool_result_later schedules the write (the tool loop never waits
     on the pool) and the scheduled write lands. A pool that explodes, or no
     pool at all, never raises into the caller.

Stdlib only; loads server/error_log.py straight from file, reads session.py
as a syntax tree. No pod, no DB. Exit 0 = pass.

ERROR_LANE_ERROR_LOG / ERROR_LANE_SESSION_PY override the two paths (used to
prove the test fails against origin/main's copies).

All four checks run every time: the turn's half of the fix (its recorder
call outside `if _prompt_console:`) has landed.
"""

import ast
import asyncio
import importlib.util
import logging
import os
import sys

from turn_source import turn_source  # session.py and the modules split out of it

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
ERROR_LOG = os.environ.get("ERROR_LANE_ERROR_LOG") or os.path.join(ROOT, "server", "error_log.py")
# Unset: the turn, session.py and every module split out of it. Set: that one file.
SESSION_PY = os.environ.get("ERROR_LANE_SESSION_PY")

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[error-lane-records] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


spec = importlib.util.spec_from_file_location("error_log_under_test", ERROR_LOG)
el = importlib.util.module_from_spec(spec)
spec.loader.exec_module(el)
# The recorder's ERROR_EVENT mirror line is the floor, not what is tested here.
el.log.setLevel(logging.CRITICAL)


class _Pool:
    def __init__(self):
        self.inserts = []

    async def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("INSERT INTO ERROR_EVENTS"):
            self.inserts.append(args)
        return "OK"


class _ExplodingPool:
    async def execute(self, *_a, **_k):
        raise RuntimeError("db down")


# ── 1. reachable: no recorder call under the telemetry console gate ────────

def _recorder_names(tree: ast.AST) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "server.error_log":
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def _tests_console(test: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "_prompt_console" for n in ast.walk(test))


def reachable_checks():
    if SESSION_PY:
        with open(SESSION_PY, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    else:
        tree = ast.parse(turn_source(ROOT))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names = _recorder_names(tree)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names]
    check("session.py calls the error-lane recorder", bool(calls),
          f"no call to any of {sorted(names)} imported from server.error_log")
    gated = []
    for call in calls:
        node = call
        while node in parents:
            parent = parents[node]
            if isinstance(parent, ast.If) and node in parent.body and _tests_console(parent.test):
                gated.append(call.lineno)
                break
            node = parent
    check("no error-lane call sits under `if _prompt_console:` (nothing wires it, so the call is dead)",
          not gated, f"gated call(s) at session.py line(s) {gated}")
    tool_calls = [c for c in calls
                  if any(k.arg == "source" and isinstance(k.value, ast.Constant)
                         and k.value.value == "tool" for k in c.keywords)]
    check("the host tool loop records with source='tool'", bool(tool_calls))


# ── 2-4. the recorder side ─────────────────────────────────────────────────

async def recorder_checks():
    rtr = getattr(el, "record_tool_result", None)
    later = getattr(el, "record_tool_result_later", None)
    check("error_log.record_tool_result exists", callable(rtr))
    check("error_log.record_tool_result_later exists", callable(later))
    if not (callable(rtr) and callable(later)):
        return

    for status, kind in (("error", "error"), ("denied", "denied"),
                         ("blocked", "denied"), ("unavailable", "unavailable")):
        pool = _Pool()
        landed = await rtr(pool, tool="operate", source="tool", session_id="s1",
                           args={"op": "x"},
                           result={"status": status, "result": f"it {status}",
                                   "reason": "bad_args_json"})
        row = pool.inserts[0] if pool.inserts else None
        check(f"status {status!r} lands one row as kind {kind!r}",
              landed is True and row is not None and row[0] == "tool" and row[1] == kind
              and row[2] == "operate" and row[3] == f"it {status}",
              f"landed={landed} rows={pool.inserts}")

    for res in ({"status": "ok", "result": "fine"}, {"status": "dispatched", "task_id": 7},
                {"status": "not_found", "result": "no key"}, {"status": "ungrounded"},
                {"status": "error", "result": "first call failed", "cached": True},
                "plain text", None):
        pool = _Pool()
        landed = await rtr(pool, tool="recall", result=res)
        check(f"{res!r:.60} lands nothing", landed is False and not pool.inserts,
              f"rows={pool.inserts}")

    pool = _Pool()
    long_err = "Traceback line\n" * 333 + "E" * 5        # 5,000 characters
    import logging as _logging
    seen_lines: list = []

    class _Grab(_logging.Handler):
        def emit(self, record):
            seen_lines.append(record.getMessage())

    grab = _Grab()
    el.log.addHandler(grab)
    el.log.setLevel(_logging.WARNING)
    el.log.propagate = False             # captured here, not printed
    try:
        await rtr(pool, tool="build", result={"status": "error", "result": long_err})
    finally:
        el.log.removeHandler(grab)
        el.log.setLevel(_logging.CRITICAL)
        el.log.propagate = True
    check("a 5,000-character error lands whole",
          bool(pool.inserts) and pool.inserts[0][3] == long_err,
          f"kept {len(pool.inserts[0][3]) if pool.inserts else 0} of {len(long_err)}")
    line = next((x for x in seen_lines if x.startswith("ERROR_EVENT")), "")
    check("...while the pod log line is a short pointer, not the person's text",
          line and len(line) < 500 and "more characters" in line,
          f"{len(line)} chars")

    pool = _Pool()
    huge = "x" * (el._MSG_CAP + 1234)
    await rtr(pool, tool="build", result={"status": "error", "result": huge})
    msg = pool.inserts[0][3] if pool.inserts else ""
    check("an error past the cap says how much was left out",
          msg.startswith("x" * 100) and "1234 more characters" in msg, msg[-80:])

    pool = _Pool()
    task = later(pool, tool="operate", source="tool", session_id="s1", args={},
                 result={"status": "error", "result": "boom"})
    check("record_tool_result_later schedules a failure", isinstance(task, asyncio.Task))
    if isinstance(task, asyncio.Task):
        await task
    check("the scheduled write lands", len(pool.inserts) == 1, f"rows={pool.inserts}")
    check("nothing is scheduled for an ok call",
          later(_Pool(), tool="recall", result={"status": "ok"}) is None)

    try:
        await rtr(_ExplodingPool(), tool="t", result={"status": "error", "result": "x"})
        await rtr(None, tool="t", result={"status": "error", "result": "x"})
        t = later(_ExplodingPool(), tool="t", result={"status": "error", "result": "x"})
        if t is not None:
            await t
        check("an exploding pool or no pool never raises", True)
    except Exception as e:  # noqa: BLE001
        check("an exploding pool or no pool never raises", False, repr(e))


if __name__ == "__main__":
    reachable_checks()
    asyncio.run(recorder_checks())
    if FAILS:
        print(f"[error-lane-records] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("[error-lane-records] all checks passed")
    sys.exit(0)
