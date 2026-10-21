#!/usr/bin/env python3
"""
In-image gate: the pluggable pipeline, step 1, end to end against the real DB.

Drives the REAL build door with the smoke-only stub harness, the way
scripts/test_factory_build_gate.py does, and checks what the project's
pipeline setting (projects.config.pipeline) does — and does not do — to the
phase a build lands. A build's outcome is whether it landed
(2026-10-01): the checks are reported, never decisive.

  A. no setting        — a landed build with a passing declared check is
                         'complete', and the task row names each check's provider
  B. harness-check made informational (required=false) — still 'complete'; the
                         row records the check as informational. The setting
                         also lists the retired diff-review (a setting stored
                         before 2026-10-20): it is read without that row
  C. a FAILING declared check — 'complete' (not 'failed', not 'review'), under
                         the default setting and under an informational one; the
                         check ran and failed on the pod, and the delivery's
                         checks line states it as information
  D. a stored setting that no longer validates — 'complete', with a passing
                         or a failing check; the setting no longer sets status
  E. the setting cannot be read at build start — 'complete' as well; no
                         pipeline reason rides the delivery
  F. step 3 — the host proposes, the user approves: status reads it; propose
                         files a ticket locked to the user (the host's decide
                         is refused); 'keep' writes nothing; 'apply' writes the
                         setting and the NEXT build runs under it; a proposal
                         made stale by another change writes nothing; one open
                         proposal per project; the POST route cannot forge a
                         proposal; a widen (a check that runs outside the
                         platform) needs the user's authenticator code, signed
                         into pipeline_approval (_widen_step_up)
  G. a build's report held while its delivery waits on the forge's CI
                         (deliver_after): neither _deliver nor a monitor tick
                         claims it before then; after, it is delivered once
                         (_report_hold)

Every check reads the DELIVERY RECORD (agent_dispatch.delivery_record over
the same row query _deliver runs), never the raw task row.

Prints a single OK.../FAIL... line. Exit 0 on pass, 1 on fail. SMOKE_PG_* env
overrides run it standalone against a local PG.
"""
import os

os.environ["FACTORY_ENGINE"] = "stub"
os.environ["CSERIES_SMOKE"] = "1"

import asyncio  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

APP = Path(os.environ.get("APP_ROOT") or "/app")
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "brain"))

PROJECTS_ROOT = Path(os.environ.get("SMOKE_PROJECTS_ROOT") or "/var/lib/cseries/projects")


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


class _ConfigReadFails:
    """The pool, except that reading projects.config raises — the path where
    the pipeline setting cannot be read at build start."""
    def __init__(self, pool):
        self._pool = pool

    async def fetchval(self, q, *a, **kw):
        if q.strip().startswith("SELECT config FROM projects"):
            raise RuntimeError("config read failed (injected)")
        return await self._pool.fetchval(q, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._pool, name)


async def _route_check(tk, rtid, _Req, HTTPException) -> str:
    """'' when only a person's surface can decide/dismiss the user_only
    ticket `rtid` through the real routes, else the failure line."""
    for bad_app in ("some-community-app", "claude-code"):
        try:
            await tk.decide_ticket(rtid, _Req(bad_app, {"option_id": "apply"}))
            return f"FAIL: F — app {bad_app!r} decided a user_only ticket as the user"
        except HTTPException as e:
            if e.status_code != 403:
                return f"FAIL: F — {bad_app!r} refused with {e.status_code}, not 403"
        try:
            await tk.dismiss_ticket(rtid, _Req(bad_app, {}))
            return f"FAIL: F — app {bad_app!r} dismissed a user_only ticket"
        except HTTPException as e:
            if e.status_code != 403:
                return f"FAIL: F — {bad_app!r} dismiss refused with {e.status_code}, not 403"
    ok_out = await tk.decide_ticket(rtid, _Req("workbench", {"option_id": "keep"}))
    if (ok_out.get("decision") or {}).get("option_id") != "keep":
        return f"FAIL: F — the Workbench (a person's surface) could not decide: {ok_out}"
    return ""


_STEPUP_IDS = ("nac_stepup_enrolled", "nac_stepup_totp", "nac_stepup_last_counter")


async def _widen_step_up(pool, tools, tk, eid, pid, fmt) -> str:
    """'' when a pipeline widen carries the step-up end to end, else the
    failure line. The pod's own second-factor rows are set aside for the run
    and put back after."""
    from mastercode.runtime import pipeline as _pl
    from server.applications.tokens import _SECRET_SCHEMA
    from server.network import nac_stepup
    await pool.execute(_SECRET_SCHEMA)
    # The project's config as the caller left it — put back on the way out,
    # so the checks after this one see the state they were written against.
    entry_cfg = await pool.fetchval("SELECT config FROM projects WHERE id=$1", pid)
    saved = await pool.fetch("SELECT id, secret FROM kernel_secrets WHERE id = ANY($1::text[])",
                             list(_STEPUP_IDS))
    await pool.execute("DELETE FROM kernel_secrets WHERE id = ANY($1::text[])", list(_STEPUP_IDS))
    _pl.PROVIDERS["t-forge"] = _pl.ProviderSpec(
        "t-forge", "check", where="forge_ci", proves=True,
        config_schema={"workflow": {"type": "string", "default": "ci.yml"}})
    row = {"provider": "t-forge", "required": True, "workflow": "ci.yml"}
    doc = {"format": fmt, "check": [{"provider": "harness-check"}, row]}

    async def cfg():
        return _j(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pid)) or {}

    async def propose(pipeline, reason="your repo has a ci.yml"):
        return await tools._exec_pipeline({"op": "propose", "project_id": pid, "reason": reason,
                                           "pipeline": pipeline}, eid, pool)

    async def refused(tid, code, what):
        try:
            await tk.decide_ticket_service(pool, eid, tid, option_id="apply", actor="user",
                                           stepup_code=code)
            return f"FAIL: F — a widen applied {what}"
        except tk.TicketError as e:
            if e.status != 403:
                return f"FAIL: F — a widen {what} was refused with {e.status}, not 403"
        st = await pool.fetchval("SELECT status FROM entity_tickets WHERE id=$1", tid)
        return "" if st == "open" else f"FAIL: F — the card closed on a refused code ({what}): {st}"

    try:
        await pool.execute("UPDATE projects SET config = COALESCE(config,'{}'::jsonb) "
                           "- 'pipeline' - 'pipeline_approval' WHERE id=$1", pid)
        # No factor enrolled: nothing could approve it, so nothing is filed.
        r = await propose(doc)
        if r.get("status") != "error" or "no second factor is enrolled" not in r.get("result", ""):
            return f"FAIL: F — a widen was proposed with no factor enrolled: {r}"
        await nac_stepup.mark_enrolled(pool)
        secret = await nac_stepup.ensure_stepup_secret(pool)
        r = await propose(doc)
        rr = r.get("result") or {}
        tid = rr.get("ticket_id")
        md = _j(await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1", tid)) or {}
        if r.get("status") != "ok" or (rr.get("ask") or {}).get("stepup_option") != "apply" \
                or "runs outside the platform: t-forge" not in rr["ask"]["sitrep"] \
                or md.get("pipeline_proposal", {}).get("requires_stepup") is not True:
            return f"FAIL: F — the widen proposal is not marked for the step-up: {r} {md}"
        for code, what in ((None, "with no code"), ("12345", "with a malformed code")):
            err = await refused(tid, code, what)
            if err:
                return err
        good = nac_stepup.current_code(secret)
        wrong = f"{(int(good[0]) + 5) % 10}{good[1:]}"
        err = await refused(tid, wrong, "with a wrong code")
        if err:
            return err
        if "pipeline" in await cfg():
            return "FAIL: F — a refused code wrote the setting"
        out = await tk.decide_ticket_service(pool, eid, tid, option_id="apply", actor="user",
                                             stepup_code=good)
        c = await cfg()
        ap = out.get("pipeline_apply") or {}
        approved = await _pl.load_approved(pool, eid, pid, c)
        if not ap.get("applied") or not ap.get("stepped_up") or approved != [row] \
                or c.get("pipeline_approval", {}).get("ticket_id") != tid:
            return f"FAIL: F — the code did not apply and sign the widen: {out} {c}"
        if "unapproved" in _pl.resolve(c, approved=approved):
            return "FAIL: F — an approved widen still resolves as unapproved"
        st = (await tools._exec_pipeline({"op": "status", "project_id": pid}, eid, pool))["result"]
        if st.get("unapproved") != [] or st["environment"].get("step_up_enrolled") is not True:
            return f"FAIL: F — status does not show the approval: {st}"
        # The code is burned: the same code cannot approve a second widen.
        r = await propose({**doc, "check": [{"provider": "harness-check"},
                                            {**row, "workflow": "deploy.yml"}]})
        tid2 = r["result"]["ticket_id"]
        err = await refused(tid2, good, "with a code already used")
        if err:
            return err
        await tk.dismiss_ticket_service(pool, eid, tid2)
        # An edited approval covers nothing.
        signed = c["pipeline_approval"]
        await pool.execute(
            "UPDATE projects SET config = jsonb_set(config, '{pipeline_approval,terms,rows,0,workflow}', "
            "'\"evil.yml\"'::jsonb) WHERE id=$1", pid)
        c2 = await cfg()
        if _pl.resolve(c2, approved=await _pl.load_approved(pool, eid, pid, c2)
                       ).get("unapproved") != ["t-forge"]:
            return "FAIL: F — an edited approval still covered the row"
        await pool.execute("UPDATE projects SET config = jsonb_set(config, '{pipeline_approval}', "
                           "$2::jsonb) WHERE id=$1", pid, json.dumps(signed))
        # A change that adds nothing outside the platform applies with no code
        # and keeps the approval as it was.
        r = await propose({"format": fmt, "check": [row]}, "the forge CI proves it alone")
        if (r.get("result") or {}).get("ask", {}).get("stepup_option"):
            return f"FAIL: F — a change adding nothing outside asked for a code: {r}"
        out = await tk.decide_ticket_service(pool, eid, r["result"]["ticket_id"], option_id="apply")
        c3 = await cfg()
        a3 = c3.get("pipeline_approval") or {}
        if not (out.get("pipeline_apply") or {}).get("applied") \
                or a3.get("terms") != signed["terms"] or a3.get("ticket_id") != tid \
                or await _pl.load_approved(pool, eid, pid, c3) != [row]:
            return f"FAIL: F — a covered change did not keep the approval: {out} {a3}"
        # Narrowing revokes: approve two outside rows, keep one, and the one
        # removed needs a fresh code to come back (review NB1). One provider
        # appears once per check list, so the second row is a module's. The
        # next window's code is valid now (±1 drift) and newer than the burned one.
        import time as _t
        row_b = {**row, "workflow": "deploy.yml"}
        two = {"format": fmt, "check": [row], "modules": {"api": {"check": [row_b]}}}
        r = await propose(two, "two workflows")
        out = await tk.decide_ticket_service(
            pool, eid, r["result"]["ticket_id"], option_id="apply", actor="user",
            stepup_code=nac_stepup.current_code(secret, at=_t.time() + 30))
        c4 = await cfg()
        if not (out.get("pipeline_apply") or {}).get("stepped_up") \
                or sorted(await _pl.load_approved(pool, eid, pid, c4), key=str) != sorted([row, row_b], key=str):
            return f"FAIL: F — the two-row widen did not apply: {out}"
        r = await propose({"format": fmt, "check": [row]}, "one workflow")
        await tk.decide_ticket_service(pool, eid, r["result"]["ticket_id"], option_id="apply")
        if await _pl.load_approved(pool, eid, pid, await cfg()) != [row]:
            return "FAIL: F — narrowing kept the removed row's approval"
        r = await propose(two, "both again")
        if (r.get("result") or {}).get("ask", {}).get("stepup_option") != "apply":
            return f"FAIL: F — a removed outside row came back without a code: {r}"
        # The apply re-checks coverage itself: a proposal whose flag was lost
        # (or never set) cannot land an uncovered row without a code (NB3).
        nf = r["result"]["ticket_id"]
        await pool.execute(
            "UPDATE entity_tickets SET metadata = jsonb_set(metadata, "
            "'{pipeline_proposal,requires_stepup}', 'false'::jsonb) WHERE id=$1", nf)
        before = (await cfg()).get("pipeline")
        out = await tk.decide_ticket_service(pool, eid, nf, option_id="apply")
        if (out.get("pipeline_apply") or {}).get("applied") is not False \
                or "did not approve it" not in out["pipeline_apply"].get("error", "") \
                or (await cfg()).get("pipeline") != before:
            return f"FAIL: F — an uncovered row applied with no code: {out}"
        # Removing the outside check drops the approval with it.
        r = await propose({"format": fmt}, "back to the defaults")
        await tk.decide_ticket_service(pool, eid, r["result"]["ticket_id"], option_id="apply")
        if "pipeline_approval" in await cfg():
            return "FAIL: F — the approval outlived the check it covered"
        # A build under an outside check nobody approved: the check is named as
        # not run, and the landed build is complete.
        stb, res = await _one(pool, tools, eid, "pipeline-gate-unapproved-widen", doc, "test -f index.html")
        forge = next((x for x in res.get("checks") or [] if x.get("provider") == "t-forge"), {})
        if stb != "complete" or forge.get("ran") is not False \
                or forge.get("reason") != _pl.UNAPPROVED_REASON:
            return f"FAIL: F — an unapproved outside check changed the build's outcome or was not named: {stb} {forge}"
        return ""
    finally:
        del _pl.PROVIDERS["t-forge"]
        # The pod's own second factor first: a failure restoring the test
        # project must never leave the real factor set aside.
        try:
            await pool.execute("DELETE FROM kernel_secrets WHERE id = ANY($1::text[])",
                               list(_STEPUP_IDS))
            for s in saved:
                await pool.execute("INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2)",
                                   s["id"], s["secret"])
        finally:
            await pool.execute("UPDATE projects SET config = $2::jsonb WHERE id=$1", pid,
                               entry_cfg if isinstance(entry_cfg, str) or entry_cfg is None
                               else json.dumps(entry_cfg))


async def _machine_source(pool, tools, tk, eid, pid, fmt) -> str:
    """'' when moving a project's repo to the code server on the user's
    machine is proposed and applied only as it should be, else the failure
    line. Leaves the project's config and the rows it made as it found them."""
    from server.routes import project_forge as _pf
    from server.vault import api as vault_api
    entry_cfg = await pool.fetchval("SELECT config FROM projects WHERE id=$1", pid)
    src = {"provider": "machine-forge", "machine": "gate-desk", "service": "gate-forge",
           "repo": "hostacct/shop"}
    key = "service-push:gate-desk:gate-forge"
    grant_id = None
    try:
        await pool.execute("UPDATE projects SET config = COALESCE(config,'{}'::jsonb) "
                           "- 'pipeline' - 'pipeline_approval' WHERE id=$1", pid)

        async def propose(reason="keep it on your machine"):
            return await tools._exec_pipeline({"op": "propose", "project_id": pid, "reason": reason,
                                               "pipeline": {"format": fmt, "source": src}}, eid, pool)

        r = await propose()
        if r.get("status") != "error" or "has not approved this Host reaching" not in r.get("result", ""):
            return f"FAIL: F — a machine source was proposed with no reach grant: {r}"
        grant_id = await pool.fetchval(
            "INSERT INTO service_reach_grants (entity_id, server, slot, subject, status, approved_at, "
            "expires_at) VALUES ($1,'gate-desk','gate-forge','gate-host','granted',now(), "
            "now() + interval '1 day') RETURNING id", eid)
        r = await propose()
        if r.get("status") != "error" or key not in r.get("result", ""):
            return f"FAIL: F — a machine source was proposed with no token in Keys: {r}"
        await vault_api.store_secret(pool, entity_id=eid, name=key, cred_type="api_key",
                                     plaintext=b"gate-token-not-real", actor="gate")
        st = (await tools._exec_pipeline({"op": "status", "project_id": pid}, eid, pool))["result"]
        if {"machine": "gate-desk", "service": "gate-forge", "token_key": key,
                "token_in_keys": True} not in st["environment"]["code_servers"]:
            return f"FAIL: F — status does not list the code server: {st['environment']}"
        r = await propose()
        rr = r.get("result") or {}
        if r.get("status") != "ok" or "keeps this project's code on gate-desk" not in rr["ask"]["sitrep"] \
                or rr["ask"].get("stepup_option"):
            return f"FAIL: F — the machine source was not proposed in plain words: {r}"
        # A review hold set before the move names a PR on the OLD forge.
        await pool.execute("UPDATE projects SET config = jsonb_set(config, '{review_hold}', "
                           "'{\"task_id\": 9, \"pr\": 5}'::jsonb) WHERE id=$1", pid)
        out = await tk.decide_ticket_service(pool, eid, rr["ticket_id"], option_id="apply")
        forge = await _pf.project_forge(pool, pid)
        if not (out.get("pipeline_apply") or {}).get("applied") or not forge.on_machine \
                or (forge.machine, forge.service, forge.repo) != ("gate-desk", "gate-forge", "hostacct/shop"):
            return f"FAIL: F — the applied machine source is not the project's forge: {out} {forge}"
        hold = _j(await pool.fetchval("SELECT config->'review_hold' FROM projects WHERE id=$1", pid))
        if hold != {"task_id": 9, "pr": None}:
            return f"FAIL: F — the move did not re-target the review hold: {hold}"
        # The Workbench finds the moved project by its platform name, and — a
        # project that never had one — by its name on the machine.
        plat = await pool.fetchval("SELECT repo FROM projects WHERE id=$1", pid)
        if plat and not (await _pf.forge_for_repo(pool, eid, plat)).on_machine:
            return "FAIL: F — the platform name no longer finds the moved project"
        await pool.execute("UPDATE projects SET repo = NULL WHERE id=$1", pid)
        try:
            by_machine = await _pf.forge_for_repo(pool, eid, "hostacct/shop")
            from server import brain_service as _bs
            from server.routes import projects as _projects
            _saved = (_bs._pool, _bs._entity_id)
            _bs._pool, _bs._entity_id = pool, eid
            try:
                listed = await _projects.list_projects(include_archived=True, limit=1000)
            finally:
                _bs._pool, _bs._entity_id = _saved
            me = next((p for p in listed["projects"] if p["id"] == pid), {})
            if not by_machine.on_machine or me.get("repo") != "hostacct/shop" \
                    or me.get("code_on_machine") is not True:
                return (f"FAIL: F — a machine-only project is not reachable from the Workbench: "
                        f"{by_machine} {me.get('repo')} {me.get('code_on_machine')}")
        finally:
            await pool.execute("UPDATE projects SET repo = $2 WHERE id=$1", pid, plat)
        # A setting that stops validating never sends the code back to the platform.
        await pool.execute(
            "UPDATE projects SET config = jsonb_set(config, '{pipeline,source,service}', "
            "'\"Not Valid\"'::jsonb) WHERE id=$1", pid)
        try:
            await _pf.project_forge(pool, pid)
            return "FAIL: F — a broken machine setting fell back to the platform forge"
        except _pf.ForgeError:
            pass
        # …and a save point for it writes nothing anywhere, saying why.
        from server.routes.project_git import sync_project_repo
        res = await sync_project_repo(pool, pid, phase_label="gate")
        if res.get("synced") or "no longer validates" not in (res.get("error") or ""):
            return f"FAIL: F — a save point under a broken machine setting: {res}"
        return ""
    finally:
        await pool.execute("UPDATE projects SET config = $2::jsonb WHERE id=$1", pid,
                           entry_cfg if isinstance(entry_cfg, str) or entry_cfg is None
                           else json.dumps(entry_cfg))
        if grant_id:
            await pool.execute("DELETE FROM service_reach_grants WHERE id=$1", grant_id)
        try:
            await vault_api.revoke_secret(pool, entity_id=eid, name=key)
        except Exception:  # noqa: BLE001
            pass


async def _one(pool, tools, eid, name, pipeline, check_cmd, build_pool=None):
    """Plan one phase with `check_cmd` as its acceptance, store `pipeline`
    (None = no setting), build, wait for rest. Returns (phase status, task
    result dict)."""
    pid = await pool.fetchval(
        "INSERT INTO projects (entity_id, name, description, status) "
        "VALUES ($1,$2,$3,'active') RETURNING id", eid, name, "pipeline gate")
    try:
        if pipeline is not None:
            await pool.execute(
                "UPDATE projects SET config = jsonb_set(COALESCE(config,'{}'::jsonb), "
                "'{pipeline}', $2::jsonb, true) WHERE id=$1", pid, json.dumps(pipeline))
        plan = await tools._exec_build(
            {"action": "plan", "project_id": pid, "intent": name,
             "phases": [{"title": "Scaffold", "spec": "Create index.html and app.yaml.",
                         "validation": "review"}]}, eid, pool, None)
        if plan.get("status") != "ok":
            raise AssertionError(f"{name}: plan failed: {plan}")
        ph = await pool.fetchval(
            "SELECT id FROM project_phases WHERE project_id=$1 ORDER BY phase_number LIMIT 1", pid)
        sp = await tools._exec_project_phase(
            {"op": "set_phase_spec", "project_id": pid, "phase_id": ph,
             "spec": "Create index.html and app.yaml.",
             "acceptance": [{"cmd": check_cmd, "expect_exit": 0, "timeout_s": 10}]}, eid, pool)
        if sp.get("status") != "ok":
            raise AssertionError(f"{name}: could not declare the check: {sp}")
        b = await tools._exec_build({"action": "build", "project_id": pid, "timeout": 60},
                                    eid, build_pool or pool, None)
        if b.get("status") != "ok":
            raise AssertionError(f"{name}: build did not start: {b}")
        tid = (b.get("result") or {}).get("task_id")
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            pending = [t for t in list(tools._FACTORY_BUILD_TASKS.values()) if not t.done()]
            cur = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", ph)
            if not pending and cur not in ("in_progress", "ready"):
                break
        status = await pool.fetchval("SELECT status FROM project_phases WHERE id=$1", ph)
        # The record the delivery REALLY builds: the same row query as
        # AgentDispatch._deliver, through the same delivery_record — so a key
        # the task row carries but the delivery drops fails this gate.
        from mastercode.runtime.agent_dispatch import DELIVERY_ROW_SQL, delivery_record
        row = await pool.fetchrow(DELIVERY_ROW_SQL, tid)
        result = _j(row["result"]) or {}
        return status, delivery_record(row, result)
    finally:
        await pool.execute("DELETE FROM projects WHERE id=$1", pid)
        shutil.rmtree(PROJECTS_ROOT / str(pid), ignore_errors=True)


async def _report_hold(pool, tools, eid) -> str:
    """G. deliver_after against the real delivery claim and monitor scan."""
    from mastercode.runtime import agent_dispatch as ad
    written = []

    class _Rt:
        async def deliver_agent_result(self, record):
            written.append(record["task_id"])
    origin = {"app_id": "workbench", "entity_id": eid, "session_id": None}
    tid = await pool.fetchval(
        "INSERT INTO entity_tasks (entity_id, title, description, task_type, status, "
        "started_at, completed_at, result) VALUES ($1, 'report-hold gate', '', "
        "'factory_build', 'in_progress', NOW(), NULL, $2::jsonb) RETURNING task_id",
        eid, json.dumps({"origin": origin}))
    try:
        if not await tools._hold_report(pool, tid, 600):
            return "FAIL: G — the report hold was not written"
        # The row turns terminal the way a build finishes it (a merge: the stamp stays).
        await pool.execute(
            "UPDATE entity_tasks SET status='completed', completed_at=NOW(), "
            "result = COALESCE(result,'{}'::jsonb) || $1::jsonb WHERE task_id=$2",
            json.dumps({"summary": "built"}), tid)
        d = ad.AgentDispatch(pool)
        d.runtime = _Rt()
        await d._deliver(tid)
        # A tick with a stuck horizon no gate row can reach: only delivery runs.
        await d._monitor_tick(10 ** 9)
        res = _j(await pool.fetchval("SELECT result FROM entity_tasks WHERE task_id=$1", tid))
        if tid in written or "delivered_at" in res:
            return f"FAIL: G — a held report was delivered during the wait: {written} {res}"
        # The build releases it (the carry update) and delivers: exactly once.
        await pool.execute(
            "UPDATE entity_tasks SET result = (COALESCE(result,'{}'::jsonb) "
            "- 'deliver_after') || $1::jsonb WHERE task_id=$2", json.dumps({}), tid)
        await d._deliver(tid)
        await d._monitor_tick(10 ** 9)
        if written.count(tid) != 1:
            return f"FAIL: G — the released report was delivered {written.count(tid)} times"
        # An expired stamp (the Host restarted mid-wait) never strands a report.
        tid2 = await pool.fetchval(
            "INSERT INTO entity_tasks (entity_id, title, description, task_type, status, "
            "started_at, completed_at, result) VALUES ($1, 'report-hold gate 2', '', "
            "'factory_build', 'completed', NOW(), NOW(), $2::jsonb) RETURNING task_id",
            eid, json.dumps({"origin": origin,
                             "deliver_after": "2000-01-01 00:00:00+00"}))
        try:
            await d._deliver(tid2)
            if tid2 not in written:
                return "FAIL: G — an expired hold stranded the report"
        finally:
            await pool.execute("DELETE FROM entity_tasks WHERE task_id=$1", tid2)
        return ""
    finally:
        await pool.execute("DELETE FROM entity_tasks WHERE task_id=$1", tid)


async def _main() -> int:
    import asyncpg
    from mastercode.runtime import tools

    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=1, max_size=2)
    try:
        eid = await pool.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
        if eid is None:
            print("FAIL: entity_registry has no rows — cannot exercise the build path")
            return 1
        fmt = "privatae.pipeline/v1"
        passing, failing = "test -f index.html", "test -f does-not-exist.txt"

        # A. no setting: the build landed, so complete; the row names the providers.
        st, res = await _one(pool, tools, eid, "pipeline-gate-default", None, passing)
        checks = res.get("checks") or []
        if st != "complete":
            print(f"FAIL: A — no pipeline setting, passing check: phase {st!r}, not 'complete'")
            return 1
        if [c.get("provider") for c in checks] != ["harness-check"] \
                or checks[0].get("ok") is not True or checks[0].get("where") != "harness_pod":
            print(f"FAIL: A — the task row's checks do not name the providers: {checks}")
            return 1

        # B. harness-check informational: still complete. The stored setting
        #    still lists the retired diff-review row; it is dropped on read,
        #    never a reason to refuse the setting.
        info = {"format": fmt, "check": [{"provider": "harness-check", "required": False},
                                         {"provider": "diff-review"}]}
        st, res = await _one(pool, tools, eid, "pipeline-gate-informational", info, passing)
        if st != "complete":
            print(f"FAIL: B — an informational passing check made the phase {st!r}, not 'complete'")
            return 1
        if [c.get("provider") for c in (res.get("checks") or [])] != ["harness-check"]:
            print(f"FAIL: B — the retired diff-review row was not dropped: {res.get('checks')}")
            return 1
        if (res.get("checks") or [{}])[0].get("required") is not False:
            print(f"FAIL: B — the row does not record the check as informational: {res.get('checks')}")
            return 1
        if res.get("phase_why") or res.get("pipeline_custom"):
            print(f"FAIL: B — a pipeline reason still rides the delivery: {res.get('phase_why')}")
            return 1

        # C. a failing check never changes the outcome.
        from mastercode.runtime.session import _acceptance_facts
        st, res = await _one(pool, tools, eid, "pipeline-gate-default-fail", None, failing)
        if st != "complete":
            print(f"FAIL: C — a FAILING check under the default setting made the phase {st!r}, "
                  "not 'complete'")
            return 1
        c0 = (res.get("checks") or [{}])[0]
        if not (c0.get("provider") == "harness-check" and c0.get("ran") is True
                and c0.get("ok") is False and c0.get("failed") == 1):
            print(f"FAIL: C — the check did not actually run and fail: {res.get('checks')}")
            return 1
        proof = _acceptance_facts(res)
        if "information only" not in proof or "did not pass" not in proof:
            print(f"FAIL: C — the delivery's checks line misstates it: {proof!r}")
            return 1
        st, res = await _one(pool, tools, eid, "pipeline-gate-informational-fail", info, failing)
        if st != "complete":
            print(f"FAIL: C — an informational FAILING check made the phase {st!r}, not 'complete'")
            return 1
        c0 = (res.get("checks") or [{}])[0]
        if not (c0.get("ran") is True and c0.get("ok") is False and c0.get("required") is False):
            print(f"FAIL: C — the informational check did not run and fail: {res.get('checks')}")
            return 1

        # D. a stored setting that no longer validates no longer sets the status.
        bad = {"format": fmt, "check": [{"provider": "no-such-provider"}]}
        for nm, cmd in (("pipeline-gate-invalid", passing), ("pipeline-gate-invalid-fail", failing)):
            st, res = await _one(pool, tools, eid, nm, bad, cmd)
            if st != "complete":
                print(f"FAIL: D — invalid setting ({nm}): phase {st!r}, not 'complete'")
                return 1
            if res.get("phase_why"):
                print(f"FAIL: D — a pipeline reason still rides the delivery: {res.get('phase_why')}")
                return 1

        # E. the setting cannot be READ at build start: complete as well.
        st, res = await _one(pool, tools, eid, "pipeline-gate-readfail", None, passing,
                             build_pool=_ConfigReadFails(pool))
        if st != "complete" or res.get("phase_why"):
            print(f"FAIL: E — unreadable setting: phase {st!r}, reason {res.get('phase_why')!r}")
            return 1

        # F. propose → approve.
        from server.routes import tickets as tk
        pidf = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id", eid, "pipeline-gate-propose", "step 3")
        try:
            b = await tools._exec_pipeline({"op": "status", "project_id": pidf}, eid, pool)
            br = b.get("result") or {}
            if b.get("status") != "ok" or br["pipeline"]["configured"] is not False \
                    or [r["provider"] for r in br["pipeline"]["effective"]["check"]] \
                    != ["harness-check"] or br.get("pending_proposal") is not None:
                print(f"FAIL: F — status on a fresh project: {b}")
                return 1
            info_only = {"format": fmt, "check": [{"provider": "harness-check", "required": False}]}
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf,
                                            "pipeline": info_only}, eid, pool)
            if r.get("status") != "error" or "reason is required" not in r.get("result", ""):
                print(f"FAIL: F — a proposal without a reason was not refused: {r}")
                return 1
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf,
                                            "pipeline": {"format": fmt}, "reason": "x"}, eid, pool)
            if (r.get("result") or {}).get("unchanged") is not True:
                print(f"FAIL: F — proposing the pipeline already in force filed something: {r}")
                return 1
            # Through the lane the Host really uses: `operate` →
            # direct_ops.run_direct_op (the caps gate and the op wall fire
            # there), whose envelope is what the chat streams under "operate"
            # and app-web-kit/ask.ts reads the card from.
            from mastercode.runtime.direct_ops import run_direct_op
            from mastercode.runtime.tool_lanes import lane_for
            if lane_for("pipeline") != "operate":
                print(f"FAIL: F — pipeline's lane is {lane_for('pipeline')!r}; ask.ts reads the "
                      "card from an operate frame — re-check the card path for the new lane")
                return 1
            # The caps gate inside needs the Host's real grant. A smoke DB's
            # entity has none, so this gate grants core.entity read_write —
            # what a Host is provisioned with — only if it has no row for
            # that domain, and removes exactly the row it added.
            _grant_id = await pool.fetchval(
                "INSERT INTO entity_domain_access (entity_id, domain_id, access_level, "
                "granted_by, grant_reason) SELECT $1, 'core.entity', 'read_write', "
                "'test_pipeline_db', 'caps for operate(pipeline)' WHERE NOT EXISTS ("
                "SELECT 1 FROM entity_domain_access WHERE entity_id=$1 "
                "AND domain_id='core.entity') RETURNING id", eid)
            try:
                r = await run_direct_op("pipeline", {"op": "propose", "project_id": pidf,
                                                     "pipeline": info_only,
                                                     "reason": "your CI runs these checks"},
                                        entity_id=eid, pool=pool)
            finally:
                if _grant_id:
                    await pool.execute("DELETE FROM entity_domain_access WHERE id=$1", _grant_id)
            rr = r.get("result") or {}
            tid = rr.get("ticket_id")
            if r.get("status") != "ok" or not tid or not rr.get("ask") \
                    or "harness-check (informational)" not in rr["ask"]["sitrep"]:
                print(f"FAIL: F — propose did not file the ask: {r}")
                return 1
            cfg = _j(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pidf)) or {}
            if "pipeline" in cfg:
                print(f"FAIL: F — propose WROTE the setting before any approval: {cfg}")
                return 1
            again = await tools._exec_pipeline({"op": "propose", "project_id": pidf,
                                                "pipeline": {"format": fmt, "check": []},
                                                "reason": "y"}, eid, pool)
            if again.get("status") != "error" or f"#{tid}" not in again.get("result", ""):
                print(f"FAIL: F — a second open proposal was not refused: {again}")
                return 1
            try:
                await tk.decide_ticket_service(pool, eid, tid, option_id="apply", actor="host")
                print("FAIL: F — the HOST decided its own pipeline proposal")
                return 1
            except tk.TicketError as e:
                if e.status != 403:
                    print(f"FAIL: F — host decide refused with {e.status}, not 403")
                    return 1
            out = await tk.decide_ticket_service(pool, eid, tid, option_id="apply", actor="user")
            cfg = _j(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pidf)) or {}
            if not (out.get("pipeline_apply") or {}).get("applied") \
                    or cfg.get("pipeline", {}).get("check", [{}])[0].get("required") is not False \
                    or cfg.get("pipeline_decision") != tid:
                print(f"FAIL: F — 'apply' did not write the setting: {out} {cfg}")
                return 1
            b = await tools._exec_pipeline({"op": "status", "project_id": pidf}, eid, pool)
            if b["result"]["pipeline"]["configured"] is not True:
                print(f"FAIL: F — status does not show the applied setting: {b}")
                return 1
            # 'keep' writes nothing.
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf,
                                            "pipeline": {"format": fmt}, "reason": "back"}, eid, pool)
            await tk.decide_ticket_service(pool, eid, r["result"]["ticket_id"], option_id="keep")
            cfg2 = _j(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pidf)) or {}
            if cfg2.get("pipeline") != cfg.get("pipeline"):
                print(f"FAIL: F — 'keep' changed the setting: {cfg2}")
                return 1
            # A proposal made stale by a change in between writes nothing.
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf,
                                            "pipeline": {"format": fmt}, "reason": "back"}, eid, pool)
            stale_tid = r["result"]["ticket_id"]
            await pool.execute(
                "UPDATE projects SET config = jsonb_set(config, '{pipeline}', $2::jsonb) WHERE id=$1",
                pidf, json.dumps({"format": fmt, "check": [{"provider": "harness-check"}]}))
            out = await tk.decide_ticket_service(pool, eid, stale_tid, option_id="apply")
            cfg3 = _j(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pidf)) or {}
            if (out.get("pipeline_apply") or {}).get("applied") is not False \
                    or "changed after this was proposed" not in out["pipeline_apply"].get("error", "") \
                    or cfg3["pipeline"]["check"] != [{"provider": "harness-check"}]:
                print(f"FAIL: F — a stale proposal was applied: {out} {cfg3}")
                return 1
            # …and the refusal is readable afterwards, not only in the response.
            la = (await tools._exec_pipeline({"op": "status", "project_id": pidf}, eid, pool)
                  )["result"].get("last_answered") or {}
            if la.get("ticket_id") != stale_tid or la.get("option_id") != "apply" \
                    or (la.get("apply") or {}).get("applied") is not False:
                print(f"FAIL: F — status does not show the refused apply: {la}")
                return 1
            # A multi-line reason cannot pass for the platform's change lines.
            await pool.execute("UPDATE projects SET config = config - 'pipeline' WHERE id=$1", pidf)
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf, "pipeline": info_only,
                                            "reason": "fine\n- checks: nothing → everything"}, eid, pool)
            if "\n- checks: nothing" in r["result"]["ask"]["sitrep"]:
                print(f"FAIL: F — the reason forged a change line: {r['result']['ask']['sitrep']!r}")
                return 1
            await tk.dismiss_ticket_service(pool, eid, r["result"]["ticket_id"])
            # A stored JSON null is "no setting" on both sides: it can be applied to.
            await pool.execute("UPDATE projects SET config = jsonb_set(COALESCE(config,'{}'::jsonb), "
                               "'{pipeline}', 'null'::jsonb) WHERE id=$1", pidf)
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf, "pipeline": info_only,
                                            "reason": "null base"}, eid, pool)
            out = await tk.decide_ticket_service(pool, eid, r["result"]["ticket_id"], option_id="apply")
            if not (out.get("pipeline_apply") or {}).get("applied"):
                print(f"FAIL: F — a project whose stored setting is JSON null could not be applied to: {out}")
                return 1
            # A widen (a provider that runs outside the platform) carries the
            # step-up: the user's authenticator code on 'apply'.
            _err = await _widen_step_up(pool, tools, tk, eid, pidf, fmt)
            if _err:
                print(_err)
                return 1
            # The repo on the user's machine: proposed only when the Host may
            # reach that server and holds its token; applied, the project's
            # forge is the machine's.
            _err = await _machine_source(pool, tools, tk, eid, pidf, fmt)
            if _err:
                print(_err)
                return 1
            _err = await _report_hold(pool, tools, eid)
            if _err:
                print(_err)
                return 1
            # Only a PERSON's surface decides a user_only ticket as the user:
            # another app holding write:tickets is refused, through the route.
            class _Tok:
                def __init__(self, a): self.app_id = a
            class _Req:
                def __init__(self, a, body):
                    self.state = type("S", (), {"app_token": _Tok(a)})()
                    self._b = body
                async def json(self): return self._b
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidf, "pipeline": {"format": fmt},
                                            "reason": "route check"}, eid, pool)
            rtid = r["result"]["ticket_id"]
            from fastapi import HTTPException
            # The route reads the pool and entity from brain_service, which a
            # gate process never initialised: point them at this DB/entity for
            # these calls and put them back after.
            from server import brain_service as _bs
            _saved = (_bs._pool, _bs._entity_id)
            _bs._pool, _bs._entity_id = pool, eid
            try:
                _route_err = await _route_check(tk, rtid, _Req, HTTPException)
            finally:
                _bs._pool, _bs._entity_id = _saved
            if _route_err:
                print(_route_err)
                return 1
            # The POST route strips the kind and the payload: no forged proposal.
            stripped = tk._strip_platform_locks({"kind": "pipeline_proposal", "pipeline_proposal": {
                "project_id": pidf, "pipeline": {"format": fmt, "check": []}, "base": None}})
            if stripped:
                print(f"FAIL: F — the route would keep a forged proposal: {stripped}")
                return 1
        finally:
            await pool.execute("DELETE FROM entity_tickets WHERE entity_id=$1 AND "
                               "metadata->>'kind' = 'pipeline_proposal' AND "
                               "(metadata->'pipeline_proposal'->>'project_id')::int = $2", eid, pidf)
            await pool.execute("DELETE FROM projects WHERE id=$1", pidf)

        # F2. what 'apply' WRITES is what a build reads and obeys: the setting a
        # user-applied proposal stored (normalized by the validator) is carried
        # verbatim onto a fresh project and built.
        pidg = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id", eid, "pipeline-gate-approved-build", "step 3")
        try:
            r = await tools._exec_pipeline({"op": "propose", "project_id": pidg,
                                            "pipeline": info, "reason": "informational"}, eid, pool)
            await tk.decide_ticket_service(pool, eid, r["result"]["ticket_id"], option_id="apply")
            cfg = _j(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pidg))
        finally:
            await pool.execute("DELETE FROM entity_tickets WHERE entity_id=$1 AND "
                               "metadata->>'kind' = 'pipeline_proposal' AND "
                               "(metadata->'pipeline_proposal'->>'project_id')::int = $2", eid, pidg)
            await pool.execute("DELETE FROM projects WHERE id=$1", pidg)
        st, res = await _one(pool, tools, eid, "pipeline-gate-approved-next-build",
                             cfg["pipeline"], failing)
        # The setting governs which checks are listed and how each is recorded;
        # the landed build is complete whatever they say.
        if st != "complete" or (res.get("checks") or [{}])[0].get("required") is not False:
            print(f"FAIL: F2 — the approved setting did not govern the next build's checks: {st} {res.get('checks')}")
            return 1

        print("OK: pipeline — no setting is unchanged and names providers on the row; an "
              "failing check, an informational one, an invalid setting and an unreadable one all leave a "
              "landed phase complete; the checks line states a failing check as information; "
              "the host proposes, only the user's apply writes it, stale/duplicate/widen/forged are refused, "
              "the next build runs under it, and a report waiting on the CI is held until it is done")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        rc = asyncio.run(_main())
    except Exception as e:
        print(f"FAIL: gate raised {type(e).__name__}: {e}")
        rc = 1
    sys.exit(rc)
