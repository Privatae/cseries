#!/usr/bin/env python3
"""
Hermetic factory-build gate — the "verify builds before a person tests
them" ratchet.

Drives the REAL brain build path end to end IN-IMAGE, without a Claude
subscription or the harness pool (neither exists in the smoke
container): plan -> build through `_exec_build`, with review.py's env-gated
stub harness (_harness_stub_active) standing in for a pod run — it writes a
deterministic app into the worktree so the SAME copy-into-project -> preview
chain is exercised. Asserts: the factory_build row reaches 'completed', phases
exist, files landed in /var/lib/cseries/projects/<id>/, and the preview file
(index.html) is present.

The stub is IMPOSSIBLE to enable outside this gate: it demands BOTH
FACTORY_ENGINE=stub AND the smoke-only marker CSERIES_SMOKE=1 (never set in a
real entity pod), set here at process start.

Prints a single OK.../FAIL... line (tail-keyed by smoke_test.sh). Exit 0 on
pass, 1 on fail.
"""
import os
os.environ["FACTORY_ENGINE"] = "stub"
os.environ["CSERIES_SMOKE"] = "1"

import asyncio
import json
import sys
import shutil
import time
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/brain")

PROJECTS_ROOT = Path("/var/lib/cseries/projects")


async def _main() -> int:
    import asyncpg
    from mastercode.runtime import tools

    # Embedded PG — same DSN the other smoke gates use. Env overrides let the
    # gate run STANDALONE against a local PG (how the script logic itself is
    # verified before it ever rides the ship pipeline).
    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=1, max_size=2)
    # entity_tasks.entity_id (and friends) FK entity_registry — a hardcoded id
    # violates the FK on any box whose seed row differs. Resolve the real one,
    # the way the other smoke scripts do.
    eid = await pool.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
    if eid is None:
        print("FAIL: entity_registry has no rows — cannot exercise the build path")
        await pool.close()
        return 1
    pid = None
    try:
        pid = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            eid, "smoke-build-gate", "hermetic factory-build gate")

        # 1) PLAN — must write phases (structural gate needs them).
        # Passes `phases` because a plan with no briefs is no longer buildable:
        # omitting them writes a DRAFT of placeholder titles that lands
        # 'planning', and the build door correctly refuses it — which is
        # exactly how this gate once red-failed a release. That refusal is
        # the feature; a host now composes the plan and passes it, so the
        # fixture composes one too.
        plan = await tools._exec_build(
            {"action": "plan", "project_id": pid, "intent": "a tiny counter app",
             "phases": [{"title": "Scaffold", "spec": "Create index.html and app.yaml.", "validation": "review"},
                        {"title": "Counter", "spec": "A button that increments a number.", "validation": "review"}]},
            eid, pool, None)
        if plan.get("status") != "ok" or not (plan.get("result") or {}).get("phases"):
            print(f"FAIL: plan did not compose phases: {plan}")
            return 1
        phase_count = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid)
        if not phase_count:
            print("FAIL: no project_phases rows after plan")
            return 1

        # 2) GATE — a project WITHOUT a plan must refuse to build. Prove the
        #    structural gate on a fresh project.
        pid2 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            eid, "smoke-build-gate-noplan", "gate check")
        refused = await tools._exec_build(
            {"action": "build", "project_id": pid2}, eid, pool, None)
        await pool.execute("DELETE FROM projects WHERE id=$1", pid2)
        if refused.get("status") != "error" or "plan" not in str(refused.get("result", "")).lower():
            print(f"FAIL: build did NOT refuse a plan-less project: {refused}")
            return 1

        # (2b, the parity check through the deleted Claude-subscription host
        #  engine's _handle_build, retired 2026-08-19 with that engine:
        #  the host now calls the SAME gated `build` meta-tool directly.)

        # 2c) CONTINUE is plan-gated too — a "continue" on a never-planned
        #     project is a first build wearing a different argument.
        pid4 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            eid, "smoke-build-gate-continue", "continue gate check")
        cont = await tools._exec_build(
            {"action": "continue", "project_id": pid4, "message": "add a footer"},
            eid, pool, None)
        await pool.execute("DELETE FROM projects WHERE id=$1", pid4)
        if cont.get("status") != "error" or "plan" not in str(cont.get("result", "")).lower():
            print(f"FAIL: continue did NOT refuse a plan-less project: {cont}")
            return 1

        # 3) BUILD — real brain path (detached run over the stub harness).
        built = await tools._exec_build(
            {"action": "build", "project_id": pid, "timeout": 120}, eid, pool, None)
        if built.get("status") != "ok":
            print(f"FAIL: build did not start: {built}")
            return 1
        tid = (built.get("result") or {}).get("task_id")
        if tid is None:
            print("FAIL: build returned no task_id")
            return 1

        # Await the detached background task (same process).
        task = tools._FACTORY_BUILD_TASKS.get(tid)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=90)
        # Poll the row to 'completed' (belt + suspenders if the ref was popped).
        status = None
        for _ in range(60):
            status = await pool.fetchval(
                "SELECT status FROM entity_tasks WHERE task_id=$1", tid)
            if status in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)
        if status != "completed":
            err = await pool.fetchval(
                "SELECT error FROM entity_tasks WHERE task_id=$1", tid)
            print(f"FAIL: factory_build row status={status!r} (error={err!r})")
            return 1

        # 4) Files landed in the canonical project dir + preview present.
        proj_dir = PROJECTS_ROOT / str(pid)
        index = proj_dir / "index.html"
        appyaml = proj_dir / "app.yaml"
        if not index.is_file() or not appyaml.is_file():
            print(f"FAIL: build artifacts missing in {proj_dir} "
                  f"(index.html={index.is_file()}, app.yaml={appyaml.is_file()})")
            return 1

        # 5) Phase transitioned to the status the build EARNED. A build's outcome
        #    is whether it LANDED its files: the stub landed
        #    index.html and app.yaml above, so the first phase is 'complete' —
        #    whether or not it declared acceptance checks (these fixture phases
        #    declare none, and 'review' is never written any more). The checks'
        #    own behaviour is sections 8 and 9.
        complete_phase = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1 AND status='complete'", pid)
        if not complete_phase:
            statuses = await pool.fetch(
                "SELECT phase_number, status FROM project_phases WHERE project_id=$1 "
                "ORDER BY phase_number", pid)
            print("FAIL: no phase reached 'complete' after a landed build "
                  f"({[(r['phase_number'], r['status']) for r in statuses]})")
            return 1
        held = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1 AND status='review'", pid)
        if held:
            print(f"FAIL: {held} phase(s) read 'review' — a landed build is complete, never held")
            return 1

        # 6) Building the remaining phases completes the PROJECT (P2: this is
        #    what unblocks "Install as my app" — ship_project_service requires
        #    projects.status='complete').
        # Auto-advance means a successful phase STARTS THE NEXT ONE
        # itself, so this loop can no longer assume it is the thing driving the
        # plan forward. It used to call build() per remaining phase; with the
        # chain running, that second call correctly hits the per-project
        # in-flight guard and the old form red-failed on the platform working as
        # designed. Now: if a build is already in flight, WAIT for it; only call
        # build() when nothing is running (the chain stopping is legitimate —
        # e.g. a phase that is not ready).
        for _ in range(20):
            nxt = await pool.fetchval(
                # The shipped predicate (_PHASE_OPEN_SQL, tools.py): open work is
                # neither complete, archived NOR a legacy 'review' row.
                "SELECT COUNT(*) FROM project_phases WHERE project_id=$1 "
                "AND status NOT IN ('complete','archived','review')", pid)
            if not nxt:
                break
            inflight = await pool.fetchval(
                "SELECT task_id FROM entity_tasks WHERE entity_id=$1 "
                "AND task_type='factory_build' AND status='in_progress' "
                "AND (result->>'project_id')::int = $2 ORDER BY task_id DESC LIMIT 1",
                eid, pid)
            if inflight is not None:
                t2 = tools._FACTORY_BUILD_TASKS.get(inflight)
                if t2 is not None:
                    await asyncio.wait_for(asyncio.shield(t2), timeout=90)
                else:
                    await asyncio.sleep(0.5)
                continue
            b = await tools._exec_build(
                {"action": "build", "project_id": pid, "timeout": 120}, eid, pool, None)
            if b.get("status") != "ok":
                print(f"FAIL: follow-on phase build did not start: {b}")
                return 1
            t2 = tools._FACTORY_BUILD_TASKS.get((b.get("result") or {}).get("task_id"))
            if t2 is not None:
                await asyncio.wait_for(asyncio.shield(t2), timeout=90)
        # Every planned phase has now run and landed, so each is 'complete' and
        # the project reads 'complete' (no phase is open).
        not_done = await pool.fetch(
            "SELECT phase_number, status FROM project_phases WHERE project_id=$1 "
            "AND status <> 'complete' ORDER BY phase_number", pid)
        if not_done:
            print(f"FAIL: phase(s) not complete after every landed build: "
                  f"{[(r['phase_number'], r['status']) for r in not_done]}")
            return 1
        proj_status = await pool.fetchval("SELECT status FROM projects WHERE id=$1", pid)
        if proj_status != "complete":
            print(f"FAIL: every phase landed but the project reads {proj_status!r}, not 'complete'")
            return 1

        # 7) OUTCOME INTEGRITY (from a live failure:
        #    zero files landed yet the run scored completed and the phase was
        #    marked done). An empty run MUST terminate failed and leave the
        #    phase buildable (back to 'ready'), never complete. Monkeypatch
        #    the stub to write nothing — the pool-side landing check is what's
        #    under test, on the REAL path.
        from server.routes import review as _review
        pid5 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            eid, "smoke-build-gate-empty", "empty-run integrity check")
        plan5 = await tools._exec_build(
            {"action": "plan", "project_id": pid5, "intent": "empty-run check",
             "research_id": "smoke",
             "phases": [{"title": "Empty run",
                         "spec": "Produce nothing; the integrity check owns this.",
                         "validation": "review"}]},
            eid, pool, None)
        if plan5.get("status") != "ok":
            print(f"FAIL: integrity-check plan failed: {plan5}")
            return 1
        _orig_stub = _review._stub_harness_build

        def _empty_stub(worktree, prompt):
            return {"ok": True, "harness": "stub", "exit_code": 0,
                    "model": "stub", "elapsed_seconds": 0.0,
                    "transcript_full": "stub wrote nothing"}

        _review._stub_harness_build = _empty_stub
        try:
            b5 = await tools._exec_build(
                {"action": "build", "project_id": pid5, "timeout": 60},
                eid, pool, None)
            if b5.get("status") != "ok":
                print(f"FAIL: integrity-check build did not start: {b5}")
                return 1
            _t5_id = (b5.get("result") or {}).get("task_id")
            t5 = tools._FACTORY_BUILD_TASKS.get(_t5_id)
            if t5 is not None:
                await asyncio.wait_for(asyncio.shield(t5), timeout=90)
            # Retry-once-then-stop dispatches a FAILED phase's one
            # seeded rerun as a NEW detached task at the very end of
            # _run_phase_build — but only from inside a chain: _phase_retry_pick
            # bails at chain_depth < 1, and a hand-started build like this one
            # has depth 0 (its docstring says why). So no retry runs here and
            # the retry path is NOT exercised by this gate.
            #
            # The wait is kept as a rest loop anyway: it polls BOTH the task set
            # and the phase status, so if a retry ever does start from depth 0
            # the assertions below still read the phase at rest rather than a
            # tick before the retry flips it. Bounded — the retry budget is one.
            _deadline = time.monotonic() + 180
            while time.monotonic() < _deadline:
                await asyncio.sleep(0.5)
                _pending = [t for _tid, t in list(tools._FACTORY_BUILD_TASKS.items())
                            if not t.done()]
                _cur = await pool.fetchval(
                    "SELECT status FROM project_phases WHERE project_id=$1 "
                    "ORDER BY phase_number LIMIT 1", pid5)
                if not _pending and _cur != "in_progress":
                    break
            # The FIRST build's row — the retry opens its own, and this
            # assertion is about the run the gate started.
            st5 = await pool.fetchval(
                "SELECT status FROM entity_tasks WHERE task_id=$1", _t5_id)
            done5 = await pool.fetchval(
                "SELECT COUNT(*) FROM project_phases WHERE project_id=$1 AND status='complete'",
                pid5)
            # BUILDABLE, not literally 'ready'. _exec_build's readiness gate
            # accepts ready OR failed (a failed phase is a retry of one that
            # already had a brief), and _PHASE_FAILED has been 'failed' rather
            # than 'ready' since v108. This counted status='ready' and passed
            # only by accident: the old fabricated draft always wrote THREE
            # phases, so phases 2-3 sat 'ready' whatever happened to phase 1 —
            # the assertion never actually tested the failed phase's
            # retryability. A one-phase plan (which is what a composed fixture
            # looks like now) exposed it.
            buildable5 = await pool.fetchval(
                "SELECT COUNT(*) FROM project_phases WHERE project_id=$1 "
                "AND status IN ('ready','failed')", pid5)
        finally:
            _review._stub_harness_build = _orig_stub
            await pool.execute("DELETE FROM projects WHERE id=$1", pid5)
            shutil.rmtree(PROJECTS_ROOT / str(pid5), ignore_errors=True)
        if st5 != "failed":
            print(f"FAIL: zero-files run scored {st5!r} (must be 'failed')")
            return 1
        if done5:
            print("FAIL: zero-files run marked a phase complete")
            return 1
        if not buildable5:
            print("FAIL: failed phase not returned to 'ready' (stuck unbuildable)")
            return 1

        # 8) THE ACCEPTANCE CHAIN. Before the diff reviewer,
        #    run_single_harness_build never returned the pod's acceptance
        #    envelope. Checks are information only now:
        #    this section declares them and the stub runs them in
        #    the worktree the way the pod's /check does
        #    (review._stub_run_acceptance), so the envelope is exercised end to
        #    end: pass -> envelope ok on the row, complete, auto-advance, project
        #    complete; section 9: fail -> envelope not ok, the phase still complete.
        pid8 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            eid, "smoke-build-gate-acceptance", "v124 acceptance chain check")
        try:
            plan8 = await tools._exec_build(
                {"action": "plan", "project_id": pid8, "intent": "acceptance chain check",
                 "phases": [{"title": "Scaffold", "spec": "Create index.html and app.yaml.", "validation": "review"},
                            {"title": "Second", "spec": "Keep index.html; nothing else.", "validation": "review"}]},
                eid, pool, None)
            if plan8.get("status") != "ok":
                print(f"FAIL: acceptance-chain plan failed: {plan8}")
                return 1
            _p8 = await pool.fetch(
                "SELECT id FROM project_phases WHERE project_id=$1 ORDER BY phase_number", pid8)
            for _row in _p8:
                _sp = await tools._exec_project_phase(
                    {"op": "set_phase_spec", "project_id": pid8, "phase_id": _row["id"],
                     "spec": "Create index.html and app.yaml.",
                     "acceptance": [{"cmd": "test -f index.html", "expect_exit": 0, "timeout_s": 10}]},
                    eid, pool)
                if _sp.get("status") != "ok":
                    print(f"FAIL: could not declare acceptance on phase {_row['id']}: {_sp}")
                    return 1
            b8 = await tools._exec_build(
                {"action": "build", "project_id": pid8, "timeout": 60}, eid, pool, None)
            if b8.get("status") != "ok":
                print(f"FAIL: acceptance-chain build did not start: {b8}")
                return 1
            _t8_id = (b8.get("result") or {}).get("task_id")
            # Let the chain run to rest: phase 1 completes and auto-advance
            # starts phase 2 on its own; wait until no build task is pending and
            # no phase is in progress or waiting.
            _deadline = time.monotonic() + 240
            while time.monotonic() < _deadline:
                await asyncio.sleep(0.5)
                _pending = [t for _tid, t in list(tools._FACTORY_BUILD_TASKS.items()) if not t.done()]
                _running = await pool.fetchval(
                    "SELECT COUNT(*) FROM project_phases WHERE project_id=$1 "
                    "AND status IN ('in_progress','ready')", pid8)
                if not _pending and not _running:
                    break
            _st8 = await pool.fetch(
                "SELECT phase_number, status FROM project_phases WHERE project_id=$1 "
                "ORDER BY phase_number", pid8)
            _statuses = [(r["phase_number"], r["status"]) for r in _st8]
            if any(s != "complete" for _, s in _statuses):
                print(f"FAIL: acceptance chain — phases did not all reach 'complete': {_statuses}")
                return 1
            _acc8 = await pool.fetchval(
                "SELECT result->'acceptance' FROM entity_tasks WHERE task_id=$1", _t8_id)
            _acc8 = json.loads(_acc8) if isinstance(_acc8, str) else _acc8
            if not (isinstance(_acc8, dict) and _acc8.get("ran") and _acc8.get("ok") is True):
                print(f"FAIL: the build row does not carry a passed acceptance envelope: {_acc8}")
                return 1
            _ps8 = await pool.fetchval("SELECT status FROM projects WHERE id=$1", pid8)
            if _ps8 != "complete":
                print(f"FAIL: every phase proven but the project reads {_ps8!r}, not 'complete'")
                return 1
            _rev8 = await pool.fetchval(
                "SELECT result->'review' FROM entity_tasks WHERE task_id=$1", _t8_id)
            _rev8 = json.loads(_rev8) if isinstance(_rev8, str) else _rev8
            # The diff reviewer was removed 2026-10-20 (the Host reviews in its
            # own turn), so a landed build's row carries no reviewer verdict.
            if _rev8 is not None:
                print(f"FAIL: the build row still carries a reviewer verdict: {_rev8}")
                return 1
        finally:
            await pool.execute("DELETE FROM projects WHERE id=$1", pid8)
            shutil.rmtree(PROJECTS_ROOT / str(pid8), ignore_errors=True)

        # 9) A FAILING check does not fail the build: the phase is complete, the envelope says it failed.
        pid9 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            eid, "smoke-build-gate-acceptance-fail", "v124 failing check")
        try:
            plan9 = await tools._exec_build(
                {"action": "plan", "project_id": pid9, "intent": "failing check",
                 "phases": [{"title": "Scaffold", "spec": "Create index.html and app.yaml.", "validation": "review"}]},
                eid, pool, None)
            if plan9.get("status") != "ok":
                print(f"FAIL: failing-check plan failed: {plan9}")
                return 1
            _ph9 = await pool.fetchval(
                "SELECT id FROM project_phases WHERE project_id=$1 ORDER BY phase_number LIMIT 1", pid9)
            _sp9 = await tools._exec_project_phase(
                {"op": "set_phase_spec", "project_id": pid9, "phase_id": _ph9,
                 "spec": "Create index.html and app.yaml.",
                 "acceptance": [{"cmd": "test -f does-not-exist.txt", "expect_exit": 0, "timeout_s": 10}]},
                eid, pool)
            if _sp9.get("status") != "ok":
                print(f"FAIL: could not declare the failing check: {_sp9}")
                return 1
            b9 = await tools._exec_build(
                {"action": "build", "project_id": pid9, "timeout": 60}, eid, pool, None)
            if b9.get("status") != "ok":
                print(f"FAIL: failing-check build did not start: {b9}")
                return 1
            _t9_id = (b9.get("result") or {}).get("task_id")
            # The build landed its files, so the phase is 'complete' the moment
            # the task finishes; the wait below is the same rest loop section 7
            # uses.
            _deadline = time.monotonic() + 240
            while time.monotonic() < _deadline:
                await asyncio.sleep(0.5)
                _pending = [t for _tid, t in list(tools._FACTORY_BUILD_TASKS.items()) if not t.done()]
                _cur9 = await pool.fetchval(
                    "SELECT status FROM project_phases WHERE id=$1", _ph9)
                if not _pending and _cur9 != "in_progress":
                    break
            _cur9 = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", _ph9)
            if _cur9 != "complete":
                print(f"FAIL: a failing acceptance check left a landed phase {_cur9!r}, not 'complete'")
                return 1
            _st9 = await pool.fetchval("SELECT status FROM entity_tasks WHERE task_id=$1", _t9_id)
            _acc9 = await pool.fetchval(
                "SELECT result->'acceptance' FROM entity_tasks WHERE task_id=$1", _t9_id)
            _acc9 = json.loads(_acc9) if isinstance(_acc9, str) else _acc9
            if _st9 != "completed" or not (isinstance(_acc9, dict) and _acc9.get("ran")
                                            and _acc9.get("ok") is False):
                print(f"FAIL: failing check — task {_st9!r} (want 'completed'), envelope {_acc9}")
                return 1
            _chk9 = await pool.fetchval(
                "SELECT result->'checks' FROM entity_tasks WHERE task_id=$1", _t9_id)
            _chk9 = json.loads(_chk9) if isinstance(_chk9, str) else _chk9
            if not any(isinstance(c, dict) and c.get("provider") == "harness-check"
                       and c.get("ran") is True and c.get("ok") is False for c in (_chk9 or [])):
                print(f"FAIL: the task row's checks do not record the failing check: {_chk9}")
                return 1
        finally:
            await pool.execute("DELETE FROM projects WHERE id=$1", pid9)
            shutil.rmtree(PROJECTS_ROOT / str(pid9), ignore_errors=True)

        print("OK: plan->gate->build->completed; phases + index.html landed; "
              "a landed phase is 'complete' (declared checks or not) and the plan completes the "
              "project; empty run fails + phase returns to ready; declared checks that pass "
              "ride the row and the chain auto-advances to a complete project; a failing check "
              "is recorded on the row and never fails or holds the landed phase")
        return 0
    finally:
        try:
            if pid is not None:
                await pool.execute("DELETE FROM projects WHERE id=$1", pid)
                shutil.rmtree(PROJECTS_ROOT / str(pid), ignore_errors=True)
        except Exception:
            pass
        await pool.close()


if __name__ == "__main__":
    try:
        rc = asyncio.run(_main())
    except Exception as e:
        print(f"FAIL: gate raised {type(e).__name__}: {e}")
        rc = 1
    sys.exit(rc)
