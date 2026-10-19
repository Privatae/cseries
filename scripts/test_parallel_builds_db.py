#!/usr/bin/env python3
"""
In-image gate: parallel builds in the Workbench, end to end.

Drives the REAL build door and chain against the real DB, with the smoke-only
stub harness (review._harness_stub_active) standing in for a pod run, exactly
as scripts/test_factory_build_gate.py does. The stub reads two smoke-only
lines from a phase brief: `STUB-WRITE <path> <text>` writes that file and
`STUB-SLEEP <s>` holds the run open, so one phase can still be building when
another lands.

The plan's three gates:
  1. two independent phases start within one tick — one build call opens
     both rows;
  2. a dependent one waits — C (depends on A and B) has not started when A
     lands while B builds, and starts when B lands;
  3. a manufactured conflict produces the integration failure, not a phase
     failure — two phases create the same file differently; the second lands
     nothing, its phase is back to 'ready' with integration_error set, its row
     says split_conflict, and no retry was started.

Prints a single OK.../FAIL... line (tail-keyed by smoke_test.sh). Exit 0 on
pass, 1 on fail. SMOKE_PG_* env overrides run it standalone against a local PG.
"""
import os
os.environ["FACTORY_ENGINE"] = "stub"
os.environ["CSERIES_SMOKE"] = "1"

import asyncio
import json
import sys
from pathlib import Path

APP = Path(os.environ.get("APP_ROOT") or "/app")
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "brain"))

PROJECTS_ROOT = Path(os.environ.get("SMOKE_PROJECTS_ROOT") or "/var/lib/cseries/projects")


async def _drain(tools, timeout=90.0):
    """Wait until every detached build task in this process has FINISHED —
    not just its row: a row turns terminal before the phase status, the
    plan-complete flip and the next phase's start that follow it."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while tools._FACTORY_BUILD_TASKS and loop.time() < end:
        pending = list(tools._FACTORY_BUILD_TASKS.values())
        await asyncio.wait(pending, timeout=max(0.1, end - loop.time()))
        await asyncio.sleep(0.05)


async def _wait_rows(pool, pid, n, timeout=60.0):
    """Every factory_build row of the project, after every build task has
    finished."""
    from mastercode.runtime import tools
    await _drain(tools, timeout)
    return await pool.fetch(
        "SELECT task_id, status, result FROM entity_tasks WHERE task_type='factory_build' "
        "AND (result->>'project_id')::int=$1 ORDER BY task_id", pid)


def _res(r):
    v = r["result"]
    return json.loads(v) if isinstance(v, str) else (v or {})


async def _main() -> int:
    import asyncpg
    from mastercode.runtime import tools
    if os.environ.get("SMOKE_PROJECTS_ROOT"):
        from server.routes import review as rv
        rv.PROJECTS_ROOT = PROJECTS_ROOT
        tools._PROJECTS_ROOT = str(PROJECTS_ROOT)
        import server.routes.project_modules as pm
        if hasattr(pm, "PROJECTS_ROOT"):
            pm.PROJECTS_ROOT = PROJECTS_ROOT

    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=1, max_size=4)
    eid = await pool.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
    if eid is None:
        print("FAIL: entity_registry has no rows — cannot exercise the build path")
        await pool.close()
        return 1
    made: list = []
    acc = lambda f: [{"cmd": f"test -f {f}", "timeout_s": 10}]  # noqa: E731
    try:
        # ── 1 + 2: A and B independent, C waits for both ─────────────────────
        pid = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,'smoke-parallel','parallel builds gate','active') RETURNING id", eid)
        made.append(pid)
        plan = await tools._exec_build(
            {"action": "plan", "project_id": pid, "intent": "three pieces",
             "phases": [
                 {"title": "A", "key": "a", "depends_on": [], "acceptance": acc("a.txt"),
                  "spec": "Write a.txt.\nSTUB-WRITE a.txt alpha"},
                 {"title": "B", "key": "b", "depends_on": [], "acceptance": acc("b.txt"),
                  "spec": "Write b.txt.\nSTUB-SLEEP 4\nSTUB-WRITE b.txt beta"},
                 {"title": "C", "depends_on": ["a", "b"], "acceptance": acc("c.txt"),
                  "spec": "Write c.txt.\nSTUB-WRITE c.txt gamma"}]},
            eid, pool, None)
        if plan.get("status") != "ok":
            print(f"FAIL: plan refused: {plan}")
            return 1
        ph = {p["title"]: p for p in plan["result"]["phases"]}
        if ph["C"].get("depends_on") != sorted([ph["A"]["id"], ph["B"]["id"]]) \
                and sorted(ph["C"].get("depends_on") or []) != sorted([ph["A"]["id"], ph["B"]["id"]]):
            print(f"FAIL: C's dependencies were not written: {ph['C']}")
            return 1

        built = await tools._exec_build({"action": "build", "project_id": pid, "timeout": 120},
                                        eid, pool, None)
        started = (built.get("result") or {}).get("started") or []
        if built.get("status") != "ok" or sorted(s["phase_title"] for s in started) != ["A", "B"]:
            print(f"FAIL: one build call did not start A and B together: {built}")
            return 1
        c_status = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", ph["C"]["id"])
        if c_status != "ready":
            print(f"FAIL: C started before its dependencies (status={c_status!r})")
            return 1

        # A lands (no sleep) while B is still building: C must still wait.
        a_tid = next(s["task_id"] for s in started if s["phase_title"] == "A")
        t = tools._FACTORY_BUILD_TASKS.get(a_tid)
        if t is not None:
            await asyncio.wait_for(asyncio.shield(t), timeout=60)
        a_st = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", ph["A"]["id"])
        b_st = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", ph["B"]["id"])
        c_st = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", ph["C"]["id"])
        if a_st != "complete" or b_st != "in_progress" or c_st != "ready":
            print(f"FAIL: after A landed with B building, expected A complete / B in_progress / "
                  f"C ready — got {a_st} / {b_st} / {c_st}")
            return 1

        rows = await _wait_rows(pool, pid, 3, timeout=90)
        by_phase = {_res(r).get("phase_id"): r for r in rows}
        c_row = by_phase.get(ph["C"]["id"])
        if c_row is None or c_row["status"] != "completed":
            print(f"FAIL: C did not start and complete after B landed: "
                  f"{[(r['task_id'], r['status'], _res(r).get('phase_id')) for r in rows]}")
            return 1
        for f in ("a.txt", "b.txt", "c.txt"):
            if not (PROJECTS_ROOT / str(pid) / f).is_file():
                print(f"FAIL: {f} missing from the project after all three landed")
                return 1
        proj_status = await pool.fetchval("SELECT status FROM projects WHERE id=$1", pid)
        if proj_status != "complete":
            print(f"FAIL: project not complete after every phase landed (status={proj_status!r})")
            return 1

        # ── 3: two phases create the same file differently ───────────────────
        pid2 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,'smoke-parallel-conflict','split conflict gate','active') RETURNING id", eid)
        made.append(pid2)
        plan2 = await tools._exec_build(
            {"action": "plan", "project_id": pid2, "intent": "a bad split",
             "phases": [
                 {"title": "Left", "depends_on": [], "acceptance": acc("shared.txt"),
                  "spec": "Write shared.txt.\nSTUB-WRITE shared.txt from-left"},
                 {"title": "Right", "depends_on": [], "acceptance": acc("shared.txt"),
                  "spec": "Write shared.txt.\nSTUB-SLEEP 2\nSTUB-WRITE shared.txt from-right"}]},
            eid, pool, None)
        if plan2.get("status") != "ok":
            print(f"FAIL: conflict plan refused: {plan2}")
            return 1
        right_id = next(p["id"] for p in plan2["result"]["phases"] if p["title"] == "Right")
        b2 = await tools._exec_build({"action": "build", "project_id": pid2, "timeout": 120},
                                     eid, pool, None)
        if b2.get("status") != "ok" or len((b2.get("result") or {}).get("started") or []) != 2:
            print(f"FAIL: the conflict plan did not start both phases: {b2}")
            return 1
        rows2 = await _wait_rows(pool, pid2, 2, timeout=60)
        right_rows = [r for r in rows2 if _res(r).get("phase_id") == right_id]
        if len(right_rows) != 1:
            print(f"FAIL: expected exactly one build of the colliding phase (no retry), "
                  f"got {len(right_rows)}")
            return 1
        rr = _res(right_rows[0])
        if right_rows[0]["status"] != "failed" or not isinstance(rr.get("integration_conflict"), dict):
            print(f"FAIL: the colliding build's row does not carry the integration conflict: {rr}")
            return 1
        if rr.get("phase_status") != "split_conflict" or "SPLIT CONFLICT" not in (rr.get("chain_stopped") or ""):
            print(f"FAIL: the delivery does not say split conflict: "
                  f"phase_status={rr.get('phase_status')!r} chain_stopped={rr.get('chain_stopped')!r}")
            return 1
        prow = await pool.fetchrow(
            "SELECT status, integration_error FROM project_phases WHERE id=$1", right_id)
        if prow["status"] != "ready" or not prow["integration_error"]:
            print(f"FAIL: the colliding phase should be ready with integration_error, got {dict(prow)}")
            return 1
        shared = (PROJECTS_ROOT / str(pid2) / "shared.txt").read_text().strip()
        if shared != "from-left":
            print(f"FAIL: the project's shared.txt was overwritten by the colliding run: {shared!r}")
            return 1
        # A re-build of the held phase is a scheduling hold, not a crash.
        again = await tools._exec_build({"action": "build", "project_id": pid2}, eid, pool, None)
        if again.get("status") != "error" or "collided" not in str(again.get("result")):
            print(f"FAIL: the door did not hold the colliding phase by name: {again}")
            return 1

        print("OK: parallel builds — two phases start from one call, a dependent one waits "
              "and starts when both land, a collision is a split conflict (nothing landed, "
              "phase ready, not retried)")
        return 0
    finally:
        for p in made:
            try:
                await pool.execute(
                    "DELETE FROM entity_tasks WHERE task_type='factory_build' "
                    "AND (result->>'project_id')::int=$1", p)
                await pool.execute("DELETE FROM projects WHERE id=$1", p)
            except Exception:
                pass
            import shutil
            shutil.rmtree(PROJECTS_ROOT / str(p), ignore_errors=True)
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
