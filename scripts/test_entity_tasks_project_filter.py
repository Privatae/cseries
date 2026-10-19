#!/usr/bin/env python3
"""entity_tasks takes a project filter.

Found on one Host (project 9423). The Host called
entity_tasks with project_id to see one project's tasks and got "unknown
field `project_id` (the fields are: action, limit, offset, op, status,
task_id)": the tool had no way to narrow its list to a project.

Where the project lives on a row (read from the writers): a factory build
and a compare stamp it at the top of entity_tasks.result
(tools._factory_build_task_start, tools._fan_out_task_start); a dispatched
agent carries it only in result.origin (agent_dispatch._row_to_agent reads
it from there as origin_project_id).

What this pins:
  1. The schema accepts project_id (tool_doors.validate_args), so the call is
     not refused.
  2. op='list' with project_id filters the list and its per-status counts by
     either place the project lives, and says which project it filtered by.
  3. op='list' with no project_id runs exactly the query it always did.
  4. A project_id that is not an integer is refused, not run.

Container-free: a stub pool records the SQL.

Run: APP_ROOT=<repo> python3 scripts/test_entity_tasks_project_filter.py
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server import brain_service  # noqa: E402
from mastercode import public_tool_registry as ptr  # noqa: E402
from mastercode.runtime import tool_doors  # noqa: E402
from mastercode.runtime import tools as t  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Pool:
    def __init__(self):
        self.calls: list = []

    async def fetch(self, q, *args):
        self.calls.append((" ".join(q.split()), args))
        if "GROUP BY status" in q:
            return [{"status": "failed", "cnt": 1}]
        return []


params = ptr.PUBLIC_TOOL_SCHEMAS["entity_tasks"]["function"]["parameters"]

print("== 1. the schema accepts project_id ==")
errs = tool_doors.validate_args(params, {"op": "list", "project_id": 9423})
check("op='list' with project_id is a valid call", errs == [], errs)
check("project_id is declared an integer",
      (params["properties"].get("project_id") or {}).get("type") == "integer",
      params["properties"].get("project_id"))

saved_pool, saved_eid = brain_service._pool, brain_service._entity_id
try:
    print("== 2. the list and its counts are filtered by the project ==")
    pool = _Pool()
    brain_service._pool, brain_service._entity_id = pool, 3
    out = run(t._exec_entity_tasks({"op": "list", "project_id": 9423}))
    check("the call ran", out.get("status") == "ok", out)
    lst = [c for c in pool.calls if "ORDER BY created_at" in c[0]]
    stats = [c for c in pool.calls if "GROUP BY status" in c[0]]
    check("one list query and one count query", len(lst) == 1 and len(stats) == 1, pool.calls)
    q, a = lst[0] if lst else ("", ())
    check("the list matches the project at the top of result (builds, compares)",
          "result->>'project_id'" in q, q)
    check("...and in result.origin (dispatched agents)",
          "result->'origin'->>'project_id'" in q, q)
    check("...against the asked project", "9423" in a and 3 in a, a)
    sq, sa = stats[0] if stats else ("", ())
    check("the per-status counts are the project's too",
          "result->>'project_id'" in sq and "9423" in sa, (sq, sa))
    check("the answer names the project it filtered by",
          (out.get("result") or {}).get("project_id") == 9423, out)

    print("== 3. no project_id: the query is the one it always was ==")
    pool2 = _Pool()
    brain_service._pool = pool2
    run(t._exec_entity_tasks({"op": "list"}))
    q2, a2 = [c for c in pool2.calls if "ORDER BY created_at" in c[0]][0]
    check("no project clause", "project_id" not in q2, q2)
    check("entity, limit, offset only", a2 == (3, 100, 0), a2)

    print("== 4. a project_id that is not a number is refused ==")
    pool3 = _Pool()
    brain_service._pool = pool3
    bad = run(t._exec_entity_tasks({"op": "list", "project_id": "four-two-three"}))
    check("refused, with nothing queried", bad.get("status") == "error" and pool3.calls == [],
          (bad, pool3.calls))
finally:
    brain_service._pool, brain_service._entity_id = saved_pool, saved_eid

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
