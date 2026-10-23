#!/usr/bin/env python3
"""list_projects carries plan state, so a stub project is never silent.

WHAT THIS PROTECTS.
A fresh entity's Workbench held a "Trading software" project with a repo,
zero phases, no routing and an empty description. The build door refuses to
build without a plan (runtime/tools.py plan gate), but the READ side never
said so: list_projects returned id/name/status/kind/description only, so the
host could not tell a stub from a live project and never offered to draft a
plan. Empty states are where new users bounce.

  1. list_projects (runtime/tools_projects.py::_exec_list_projects, ~11162) aggregates
     project_phases per row — has_plan, phase_count, phases_ready,
     phases_complete, last_phase_at — in ONE lateral join, and attaches a
     `hint` to every kind='project' row without a plan, plus a result-level
     `without_plan` count and note.
  2. The schema description and the Workbench briefing tell the host what to
     do with it: surface the stub and offer the plan on selection.

WHAT THIS DOES NOT CATCH. The executor runs here against a fake pool that
returns synthetic rows, so the SQL is checked for shape (tables, aggregates,
qualified columns) but not executed. The in-image smoke run is where the
statement meets Postgres.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_list_projects_plan_state.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class FakePool:
    """Records the SQL and returns synthetic rows shaped like the lateral join."""

    def __init__(self):
        self.sql = None
        self.params = None

    async def fetch(self, sql, *params):
        # The modules of the listed projects are a SECOND, batched statement,
        # recorded apart so the checks below read
        # the listing's own SQL.
        if "FROM project_modules" in sql:
            self.module_sql, self.module_params = sql, params
            return [{"project_id": 8, "name": "reader", "target": "private_app",
                     "validation": "checks", "description": "", "current_version_id": 3,
                     "current_number": 2, "current_run_id": 701,
                     "current_commit": "abcdef1234", "current_source": "build",
                     "current_at": NOW, "version_count": 2}]
        self.sql, self.params = sql, params
        return [
            {"id": 7, "name": "Trading software", "description": "", "status": "planning",
             "kind": "project", "created_at": NOW, "updated_at": NOW,
             "phase_count": 0, "phases_ready": 0, "phases_complete": 0, "phases_open": 0,
             "last_phase_at": None, "next_phase_status": None},
            {"id": 8, "name": "Personal dashboard", "description": "  A dashboard  ",
             "status": "active", "kind": "project", "created_at": NOW, "updated_at": NOW,
             "phase_count": 4, "phases_ready": 2, "phases_complete": 1, "phases_open": 3,
             "last_phase_at": NOW, "next_phase_status": "ready"},
            {"id": 9, "name": "Homepage mock", "description": None, "status": "active",
             "kind": "artifact", "created_at": NOW, "updated_at": NOW,
             "phase_count": 0, "phases_ready": 0, "phases_complete": 0, "phases_open": 0,
             "last_phase_at": None, "next_phase_status": None},
            # A drafted plan: phases exist but every one is a 'planning'
            # placeholder with no brief — the build door refuses it.
            {"id": 10, "name": "Drafted only", "description": None, "status": "planning",
             "kind": "project", "created_at": NOW, "updated_at": NOW,
             "phase_count": 3, "phases_ready": 0, "phases_complete": 0, "phases_open": 3,
             "last_phase_at": NOW, "next_phase_status": "planning"},
            # Everything shipped: phases exist, none open.
            {"id": 11, "name": "Shipped app", "description": None, "status": "complete",
             "kind": "project", "created_at": NOW, "updated_at": NOW,
             "phase_count": 2, "phases_ready": 0, "phases_complete": 2, "phases_open": 0,
             "last_phase_at": NOW, "next_phase_status": None},
        ]


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("list_projects — a stub project is never silent")

    from mastercode.runtime import tools as t  # noqa: E402

    pool = FakePool()
    out = asyncio.run(t._exec_list_projects({"search": "  ", "limit": "5"}, 42, pool))
    check("executor returns ok", out.get("status") == "ok", str(out)[:200])
    res = out.get("result") or {}
    rows = {r["project_id"]: r for r in res.get("projects") or []}

    # ── 0. Modules ride the listing, in ONE extra statement ───────────────
    check("modules are fetched once, for the listed projects only (not the artifact)",
          getattr(pool, "module_params", None) is not None
          and sorted(pool.module_params[0]) == [7, 8, 10, 11],
          str(getattr(pool, "module_params", None)))
    check("a project with modules lists each with its CURRENT version",
          (rows.get(8, {}).get("modules") or [{}])[0].get("current_version", {}).get("number") == 2,
          str(rows.get(8)))
    check("a project without modules carries no modules key", "modules" not in rows.get(7, {}),
          str(rows.get(7)))

    # ── 1. The SQL asks the right question ────────────────────────────────
    sql = pool.sql or ""
    check("one statement joins project_phases laterally",
          "LEFT JOIN LATERAL" in sql and "FROM project_phases WHERE project_id = p.id" in sql)
    check("aggregates count ready/in_progress as ready, and complete with legacy "
          "'review' rows (they landed) as complete",
          "status IN ('ready','in_progress'))" in sql
          and "status IN ('complete','review'))" in sql and "MAX(updated_at)" in sql)
    check("open phases and the door's next phase use the door's own predicate",
          sql.count(t._PHASE_OPEN_SQL) == 2 and "ORDER BY phase_number ASC LIMIT 1" in sql)
    check("project columns are qualified (no ambiguity with the lateral)",
          "p.entity_id = $1" in sql and "ORDER BY p.updated_at" in sql)
    check("entity scope, limit and offset are bound (offset pages)",
          pool.params == (42, 5, 0), str(pool.params))

    # ── 2. Plan state on every row ────────────────────────────────────────
    stub, live, art = rows.get(7), rows.get(8), rows.get(9)
    drafted, shipped = rows.get(10), rows.get(11)
    check("five rows came back", all((stub, live, art, drafted, shipped)))
    check("live project is buildable (next open phase is ready)",
          live["buildable"] is True and live["next_phase_status"] == "ready"
          and live["phases_open"] == 3, str(live))
    check("drafted plan: has_plan true but NOT buildable, hint says finish the plan",
          drafted["has_plan"] is True and drafted["buildable"] is False
          and "next phase is 'planning'" in (drafted.get("hint") or "")
          and "set_phase_spec" in drafted["hint"], str(drafted))
    check("drafted plan is not counted as plan-less", res.get("without_plan") == 1)
    check("shipped project: nothing left to build, hint says so",
          shipped["buildable"] is False and shipped["next_phase_status"] is None
          and "nothing left to build" in (shipped.get("hint") or ""), str(shipped))
    check("stub: has_plan false, phase_count 0",
          stub["has_plan"] is False and stub["phase_count"] == 0, str(stub))
    check("stub carries a hint that offers the plan",
          "no plan yet" in (stub.get("hint") or "")
          and "build(action='plan')" in stub["hint"], str(stub.get("hint")))
    check("live project: has_plan true with counts + last_phase_at",
          live["has_plan"] is True and live["phase_count"] == 4
          and live["phases_ready"] == 2 and live["phases_complete"] == 1
          and live["last_phase_at"] == NOW.isoformat(), str(live))
    check("live project carries no hint", "hint" not in live)
    check("an artifact row is not called a stub (kind != project)",
          art["has_plan"] is False and "hint" not in art, str(art))
    check("description is still trimmed / None-normalised",
          live["description"] == "A dashboard" and stub["description"] is None
          and art["description"] is None)

    # ── 3. Result-level surfacing ─────────────────────────────────────────
    check("result counts plan-less projects", res.get("without_plan") == 1, str(res.get("without_plan")))
    check("result note says a stub is an empty state — said plainly, planned when the request "
          "covers it, the question box when it is the user's call (never an offer)",
          "no plan yet" in (res.get("note") or "") and "say so plainly" in res["note"]
          and "question box" in res["note"] and "offer" not in res["note"],
          str(res.get("note")))
    check("search of whitespace is normalised to None", res.get("search") is None)

    # No stubs → no note, no without_plan chatter.
    class AllPlanned(FakePool):
        async def fetch(self, sql, *params):
            rows_ = await super().fetch(sql, *params)
            if "FROM project_modules" in sql:
                return rows_
            return [r for r in rows_ if r["id"] == 8]
    out2 = asyncio.run(t._exec_list_projects({}, 42, AllPlanned()))
    res2 = out2["result"]
    check("no stubs → without_plan 0 and no note",
          res2["without_plan"] == 0 and "note" not in res2, str(res2))

    # ── 4. The host is told what to do with it ────────────────────────────
    reg = _read(repo, "brain/mastercode/public_tool_registry.py")
    lp = reg[reg.find('"list_projects": {'):reg.find('"create_project"')]
    check("list_projects schema description names the plan-state fields",
          all(k in lp for k in ("has_plan", "phase_count", "phases_ready",
                                "phases_complete", "last_phase_at")))
    check("list_projects schema description says a stub is an empty state and to ask about the plan in the box",
          "STUB" in lp and "ask in the box to plan it" in lp)
    # The long form lives in the skill's
    # list_projects guidance (checked above); the manifest keeps one line.
    # The manifest's one line moved to the skill too.
    bw = " ".join(_read(repo, "default-skills/build-workflow.md").split())
    check("build-workflow tells the host to surface a stub on selection",
          "A project with a repo but zero phases" in bw and "question box" in bw)

    print()
    if FAILED:
        print(f"FAIL list_projects_plan_state: {FAILED} check(s) failed")
        return 1
    print("PASS list_projects_plan_state: a stub project is never silent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
