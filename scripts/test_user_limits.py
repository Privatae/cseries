#!/usr/bin/env python3
"""The person's limits (runtime/user_limits.py): every limit
that can stop work the person asked for is their setting, with a coded
default; the Host reads every one and asks to change one on a card only the
person decides.

  D  each limit's default is the enforcing module's own constant, so the
     Limits page never shows a number the code does not use;
  R  every limit is read where it is enforced (its name appears outside
     user_limits.py), and the reader's order holds: the person's value, then
     the operator's env, then the default; 0 is no limit; junk falls through;
  S  a spending limit's "no limit" is written as its off switch
     (turn_rounds / scheduled_rounds = 0), never as $0, which the turn code
     reads as "unset" and would price the built-in figure back in;
  W  only the person writes: the route takes Settings, Circles and the
     Workbench and refuses the Host's token and a community app's; the Host's
     `limits` tool has no op that writes; a POST /api/tickets cannot forge an
     applying card;
  P  (real database) propose files ONE card locked to the person; the Host
     cannot decide it; the person's 'apply' writes the value, 'keep' writes
     nothing, and a card filed before the limit changed writes nothing;
     write() edits only its own key; a hit is recorded and read back.

Why a real database for P: the writes are jsonb SQL and the decide path is the
tickets service; a stub pool would assert my reading of both.

Run:  python3 scripts/test_user_limits.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other _pg gates do. With
      neither, the P checks are skipped loudly and the rest still run.)
"""
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
ROOT = Path(_ROOT)

# The reader falls back to the operator's env; none of it may leak in here.
for _lim_env in ("CSERIES_MAX_TOOL_ROUNDS", "CSERIES_ALL_FAIL_ROUNDS_MAX", "CSERIES_WOKEN_TURNS_MAX",
                 "CSERIES_RESEARCH_ROUNDS_MAX", "CSERIES_WATCH_WAKES_MAX", "HARNESS_IDLE_MINUTES",
                 "HARNESS_PROGRESS_MINUTES", "HARNESS_TOOL_STEP_MAX_MINUTES",
                 "BROWSER_AGENT_RUN_TIMEOUT_S", "BROWSER_AGENT_PAUSE_TIMEOUT_S"):
    os.environ.pop(_lim_env, None)

from mastercode.runtime import user_limits as UL  # noqa: E402

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:500]) if extra else ""))
    if not cond:
        FAILS.append(msg)


def _env_default(path: str, env: str) -> float:
    """The literal default of `os.environ.get(env, "<n>")` in a file the
    kernel cannot import (the harness pod's runtime)."""
    m = re.search(re.escape(env) + r'"\s*,\s*"([0-9.]+)"', (ROOT / path).read_text())
    return float(m.group(1)) if m else float("nan")


def unit_checks() -> None:
    print("D. each default is the enforcing module's own constant")
    from mastercode.runtime import turn_allowance, task_serial, app_verify
    from mastercode.runtime import session as S
    from mastercode.runtime import tools as T
    from mastercode.brain import offline_task_runner as OTR
    from mastercode import world_db
    from server import browser_agents as BA
    hc = "system-apps/_harness_runtime/harness_core.py"
    pairs = {
        "scheduled.turns_per_day": OTR.DEFAULT_MAX_RUNS_PER_DAY,
        "turn.tool_rounds": turn_allowance.BACKSTOP_TOOL_ROUNDS,
        "turn.all_fail_rounds": S.ALL_FAIL_BACKSTOP_ROUNDS,
        "thread.woken_turns": S.WOKEN_TURNS_MAX,
        "thread.research_rounds": S.RESEARCH_ROUNDS_MAX,
        "thread.watch_wakes": S.WATCH_WAKES_MAX,
        "thread.failed_wakes": S.FAILED_WAKES_MAX,
        "task_run.stall_turns": task_serial.STALL_TURNS,
        "task_run.skipped_turns": task_serial.SKIP_LIMIT,
        "build.auto_repairs": T.PHASE_AUTO_REPAIRS,
        "app.auto_repairs": app_verify.MAX_AUTO_REPAIRS,
        "build.chain_phases": T._AUTOADVANCE_MAX_CHAIN,
        "build.model_wait_max_minutes": T.BUILD_MODEL_WAIT_MAX_S / 60,
        "build.idle_minutes": _env_default(hc, "HARNESS_IDLE_MINUTES"),
        "build.progress_minutes": _env_default(hc, "HARNESS_PROGRESS_MINUTES"),
        "build.tool_step_minutes": _env_default(hc, "HARNESS_TOOL_STEP_MINUTES"),
        "build.tool_step_max_minutes": _env_default(hc, "HARNESS_TOOL_STEP_MAX_MINUTES"),
        "browser_agent.run_minutes": BA.RUN_TIMEOUT_S / 60,
        "browser_agent.pause_minutes": BA.PAUSE_TIMEOUT_S / 60,
        "host_data.query_max_minutes": world_db.MAX_TIMEOUT_S / 60,
    }
    for name, lim in UL.LIMITS.items():
        if lim.default is None:
            check(bool(lim.off_key and lim.derived),
                  f"{name}: a derived default says how it is worked out and has an off switch")
            continue
        check(name in pairs and float(pairs[name]) == float(lim.default),
              f"{name}: default {lim.default} is the enforcing constant", pairs.get(name))
    check(set(pairs) <= set(UL.LIMITS), "no constant above is paired with a limit that is gone",
          set(pairs) - set(UL.LIMITS))

    print("R. every limit is read where it is enforced, and the reader's order holds")
    code = ""
    for d in ("brain", "server", "system-apps"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name != "user_limits.py":
                code += p.read_text(errors="replace")
    for name, lim in UL.LIMITS.items():
        read = f'"{name}"' in code or bool(lim.bp_key and f'"{lim.bp_key}"' in code)
        check(read, f"{name} is read outside user_limits.py")
    bp = {"limits": {"thread.woken_turns": 7}}
    check(UL.value("thread.woken_turns", bp) == 7.0, "the person's value wins")
    os.environ["CSERIES_WOKEN_TURNS_MAX"] = "9"
    check(UL.value("thread.woken_turns", bp) == 7.0, "...over the operator's env")
    check(UL.value("thread.woken_turns", {}) == 9.0, "the env is used when the person set nothing")
    check(UL.source("thread.woken_turns", {}) == "operator", "...and says so")
    os.environ.pop("CSERIES_WOKEN_TURNS_MAX")
    check(UL.value("thread.woken_turns", {}) == 40.0 and UL.source("thread.woken_turns", {}) == "default",
          "the default holds when nobody set one")
    check(UL.value("thread.woken_turns", {"limits": {"thread.woken_turns": 0}}) is None,
          "0 is no limit")
    check(UL.value("thread.woken_turns", {"limits": {"thread.woken_turns": "lots"}}) == 40.0,
          "a value that is not a number falls through to the default")
    check(UL.person_or("thread.woken_turns", 123, {}) == 123,
          "person_or keeps the module's constant when the person set nothing")
    check(UL.person_or("thread.woken_turns", 123, {"limits": {"thread.woken_turns": 5}}) == 5,
          "person_or takes the person's value")
    check(UL.person_or("thread.woken_turns", 123, {"limits": {"thread.woken_turns": 0}}) == 10 ** 9,
          "person_or turns their 0 into a number nothing reaches")
    check(UL.person_seconds_or("browser_agent.run_minutes", 1500.0,
                               {"limits": {"browser_agent.run_minutes": 40}}) == 2400.0,
          "a time limit in minutes reads as seconds")
    check(UL.person_seconds_or("browser_agent.run_minutes", 1500.0,
                               {"limits": {"browser_agent.run_minutes": 0}}) is None,
          "...and its 0 is no timeout at all")
    check(UL.value("turn.tool_rounds", {"max_tool_rounds": 300}) == 300.0,
          "a limit that was a setting before keeps its old key (max_tool_rounds)")
    line = UL.hit_line("build.idle_minutes", {})
    check("`build.idle_minutes`" in line and "10 minutes" in line and "Settings > Limits" in line
          and "limits(op='propose')" in line, "the hit line names the limit, its value and the way on",
          line)
    for raw, ok in ((-1, False), (2.5, False), ("x", False), (3, True), (0, True), (None, True)):
        _v, why = UL.check_new_value("build.auto_repairs", raw)
        check((why is None) == ok, f"check_new_value(build.auto_repairs, {raw!r}) "
              + ("accepted" if ok else "refused"), why)
    check(bool(UL.check_new_value("no.such", 1)[1]), "an unknown limit is refused by name")

    print("O. 'no limit' means what the enforcing code does with it")
    # build.idle_minutes: the pods read idle_minutes 0 as their own default
    # (`body.get("idle_minutes") or IDLE_MINUTES`), so a stored 0 would show
    # "no limit" while builds kept stopping at 10 minutes. It is refused.
    _v, why = UL.check_new_value("build.idle_minutes", 0)
    check(why is not None and "build.idle_minutes" in why,
          "build.idle_minutes refuses 0: the pod has no 'off' for it", why)
    check(UL.check_new_value("build.idle_minutes", 30) == (30, None),
          "...and still takes a longer window")
    # The pod windows the person turned off reach the pod as an off it reads.
    from server.routes import review as RV
    _held = dict(UL._SNAPSHOT)
    try:
        UL._SNAPSHOT.clear()
        UL._SNAPSHOT.update({"build.tool_step_max_minutes": 0, "build.progress_minutes": 0})
        pl = RV._pod_limits()
        check(pl.get("progress_minutes") == 0, "progress off reaches the pod as 0 (its off)", pl)
        # 0 there drops a grok child step (no readable timeout) from the
        # 600-minute cap to TOOL_STEP_MINUTES: stricter, not off.
        check((pl.get("tool_step_max_minutes") or 0) > 600,
              "tool step 'no limit' reaches the pod as a cap no step reaches, never 0", pl)
        UL._SNAPSHOT.clear()
        check(RV._pod_limits() == {}, "nothing set: nothing sent, the pod keeps its own defaults")
    finally:
        UL._SNAPSHOT.clear()
        UL._SNAPSHOT.update(_held)

    print("N. a stopped build step names the limit that stopped it, or none")
    # harness_core cannot be imported here (pod runtime); its pure helper is
    # loaded from source on its own.
    _src = (ROOT / "system-apps/_harness_runtime/harness_core.py").read_text()
    _fn = _src[_src.index("def step_limit_name("):]
    _fn = _fn[:_fn.index("\ndef ", 10)]
    _ns: dict = {"Optional": __import__("typing").Optional}
    exec(_fn, _ns)
    name = _ns["step_limit_name"]
    check(name(0.0, 7200.0, 7200.0, 36000.0) == "build.tool_step_minutes",
          "a step that asked for no time ran past build.tool_step_minutes")
    check(name(36000.0, 36600.0, 7200.0, 36000.0) == "build.tool_step_max_minutes",
          "a step whose request was cut to the cap ran past build.tool_step_max_minutes")
    check(name(9000.0, 9600.0, 7200.0, 36000.0) is None,
          "a step that ran past its OWN request names no limit of the person's")
    check(name(600.0, 7200.0, 7200.0, 36000.0) == "build.tool_step_minutes",
          "a short request inside the default window is the default window's stop")
    check(name(36000.0, 42000.0, 42000.0, 36000.0) == "build.tool_step_minutes",
          "a step window set above the cap is what stopped a capped request")
    _oc = _src[_src.index("step_req = max(r for _, r in running)"):][:400]
    check("min(step_req, tool_step_max_s)" in _oc,
          "an opencode step's request is held to this run's cap, as a reported call's is")
    from server.routes import review as _RV
    check(("tool_step_minutes", "build.tool_step_minutes") in _RV._POD_LIMITS,
          "the step window is sent to the pod with the run")
    for _pod in ("system-apps/_harness_runtime/harness_core.py", "system-apps/claude-code-harness/bin/main.py"):
        _t = (ROOT / _pod).read_text()
        check('_run_minutes("tool_step_minutes", TOOL_STEP_MINUTES)' in _t
              and "step_limit_name(" in _t
              and 'payload["limit"] = "build.tool_step_max_minutes"' not in _t,
              f"{_pod.split('/')[1]}: reads the per-run step window and names the stop by what stopped it")

    print("C. POST /api/calendar answers a task above the turn limit 400, not 502")
    from server import brain_service as _BS
    from server.bridge import memory_handlers as _MH
    from server.routes import calendar as _CAL
    _saved = (_BS._pool, _BS._entity_id)
    _BS._pool, _BS._entity_id = object(), 1   # refused before any query
    try:
        _r = asyncio.run(_MH.handle_calendar_append({
            "event_type": "scheduled_task", "summary": "invented task", "status": "scheduled",
            "payload": {"prompt": "invented", "max_runs_per_day": 10 ** 6}}))
    finally:
        _BS._pool, _BS._entity_id = _saved
    try:
        _CAL._unwrap(_r)
        check(False, "a refused max_runs_per_day is the caller's error (400)", _r)
    except Exception as e:  # noqa: BLE001
        check(getattr(e, "status_code", None) == 400,
              "a refused max_runs_per_day is the caller's error (400)", repr(e))

    print("S. a spending limit's no-limit is its off switch")
    check(UL.state("turn.allowance_usd", {}) == UL.DERIVED,
          "unset: the derived figure, not 'no limit'")
    check(UL.state("turn.allowance_usd", {"turn_rounds": 0}) is None, "turn_rounds 0: no limit")
    check(UL.state("turn.allowance_usd", {"turn_allowance_usd": 0.75}) == 0.75,
          "a dollar figure is the limit")
    check(UL.state("turn.allowance_usd", {"turn_allowance_usd": 0}) == UL.DERIVED,
          "a stored $0 is 'unset', as the turn code reads it")

    print("W. only the person writes")
    from server.routes import limits as LR

    class _Tok:
        def __init__(self, app_id):
            self.app_id = app_id

    class _St:
        def __init__(self, app_id):
            self.app_token = _Tok(app_id)

    class _Req:
        def __init__(self, app_id):
            self.state = _St(app_id)

    for app_id in ("settings", "circles", "workbench"):
        check(LR._person(_Req(app_id)), f"the person's {app_id} may write a limit")
    for app_id in ("host", "some-community-app", ""):
        check(not LR._person(_Req(app_id)), f"{app_id or 'no app'} may not")
    src = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
    fn = src[src.index("async def _exec_limits("):]
    fn = fn[:fn.index("\nasync def ", 10)]
    check(".write(" not in fn, "the Host's limits tool has no path that writes a limit")

    # The per-thread allowance overrides the person's turn.allowance_usd, so
    # its two routes take it from the person only, whatever scope the caller holds.
    # circles.py imports server.main at module scope and server.main mounts
    # circles at import: the stub every circles gate uses breaks the loop.
    import types
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    from server.routes import brain as BR, circles as CR

    class _BodyReq(_Req):
        def __init__(self, app_id, body):
            super().__init__(app_id)
            self._b = body

        async def json(self):
            return self._b

    for label, call in (
            ("PUT /api/brain/thread-context", lambda r: BR.put_thread_context(r)),
            ("PUT /api/circles/thread-context", lambda r: CR.put_thread_context("self", "t-1", r))):
        try:
            asyncio.run(call(_BodyReq("some-community-app", {"turn_allowance_usd": 50.0})))
            check(False, f"{label}: a community app cannot set a thread's allowance")
        except Exception as e:  # noqa: BLE001
            check(getattr(e, "status_code", None) == 403,
                  f"{label}: a community app cannot set a thread's allowance", repr(e))


def registry_checks() -> None:
    from mastercode import public_tool_registry as reg
    from mastercode.runtime import meta_tool_caps as caps
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    spec = reg.PUBLIC_TOOL_SCHEMAS.get("limits") or {}
    ops = (((spec.get("function") or {}).get("parameters") or {}).get("properties") or {}).get("op", {})
    check(set(ops.get("enum") or []) == {"status", "propose"}, "limits: status and propose, nothing else",
          ops)
    check(caps.READ_SAFE_OPS.get("limits") == {"status": 1}, "limits(op='status') is a read")
    check("limits" in DIRECT_OP_TOOLS, "the Host runs limits itself (operate)")
    from server.routes import tickets as TK
    md = TK._strip_platform_locks({"kind": "limit_change_proposal",
                                   "limit_change": {"name": "build.auto_repairs", "value": 99}})
    check("kind" not in md and "limit_change" not in md,
          "a POST /api/tickets cannot forge a card that writes a limit", md)
    from server import host_events
    check(tuple((host_events.EVENTS.get("limit_changed") or {}).get("keys") or ()) == ("name",),
          "limit_changed is an event a task can wait on, keyed by the limit's name")


async def pg_checks(dsn: str) -> None:
    import asyncpg
    from server import brain_service as BS
    from server.routes import tickets as TK
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = (BS._pool, BS._entity_id)
    BS._pool, BS._entity_id = pool, 1
    try:
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = 1",
            json.dumps({"keep_me": "yes", "max_tool_rounds": 150}))
        print("P. writes, the card, and the person's click")
        r = await UL.write(pool, 1, "build.auto_repairs", 5)
        bp = await UL.read_bp(pool, 1)
        check(r["ok"] and (bp.get("limits") or {}).get("build.auto_repairs") == 5,
              "write stores the person's value under limits", (r, bp))
        check(bp.get("keep_me") == "yes" and bp.get("max_tool_rounds") == 150,
              "...and leaves every other key as it was", bp)
        check(UL.value("build.auto_repairs") == 5.0, "the held values follow the write at once")
        await UL.write(pool, 1, "build.auto_repairs", None)
        bp = await UL.read_bp(pool, 1)
        check("build.auto_repairs" not in (bp.get("limits") or {})
              and UL.value("build.auto_repairs") == 2.0, "None puts the default back", bp)
        await UL.write(pool, 1, "turn.allowance_usd", 0)
        bp = await UL.read_bp(pool, 1)
        check(bp.get("turn_rounds") == 0 and "turn_allowance_usd" not in bp,
              "a spending limit's 0 is written as its off switch", bp)
        from mastercode.context_budget import read_turn_defaults
        td = await read_turn_defaults(pool, 1)
        check(td["turn_rounds"] == 0 and td["turn_allowance_usd"] is None,
              "...which the turn code reads as no built-in figure", td)
        await UL.write(pool, 1, "turn.allowance_usd", 1.5)
        bp = await UL.read_bp(pool, 1)
        check(bp.get("turn_allowance_usd") == 1.5 and "turn_rounds" not in bp,
              "a dollar figure clears the off switch", bp)

        res = await UL.propose(pool, 1, "build.idle_minutes", 30, "the suite runs quiet for 20 minutes")
        tid = (res.get("result") or {}).get("ticket_id")
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_tickets WHERE id = $1",
                                            tid) or "{}")
        check(res.get("status") == "ok" and md.get("kind") == UL.PROPOSAL_KIND
              and md.get("user_only") and md.get("no_pattern"),
              "propose files one card locked to the person", (res, md))
        again = await UL.propose(pool, 1, "build.idle_minutes", 45, "")
        check(again.get("status") == "error" and "already waiting" in again.get("error", ""),
              "a second card for the same limit waits for the first", again)
        try:
            await TK.decide_ticket_service(pool, 1, tid, option_id="apply", actor="host",
                                           deliver_answer=False)
            check(False, "the Host cannot decide its own card")
        except TK.TicketError as e:
            check(e.status == 403, "the Host cannot decide its own card", e.detail)
        check(UL.value("build.idle_minutes", await UL.read_bp(pool, 1)) == 10.0,
              "...and nothing was written")
        await TK.decide_ticket_service(pool, 1, tid, option_id="apply", actor="user",
                                       deliver_answer=False)
        check(UL.value("build.idle_minutes", await UL.read_bp(pool, 1)) == 30.0,
              "the person's 'apply' writes the value")
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_tickets WHERE id = $1", tid))
        check((md.get("limit_change_apply") or {}).get("applied") is True,
              "...and the outcome is on the ticket for the Host to read", md.get("limit_change_apply"))

        res = await UL.propose(pool, 1, "build.idle_minutes", 60, "")
        tid = res["result"]["ticket_id"]
        await TK.decide_ticket_service(pool, 1, tid, option_id="keep", actor="user", deliver_answer=False)
        check(UL.value("build.idle_minutes", await UL.read_bp(pool, 1)) == 30.0, "'keep' writes nothing")

        res = await UL.propose(pool, 1, "build.idle_minutes", 90, "")
        tid = res["result"]["ticket_id"]
        await UL.write(pool, 1, "build.idle_minutes", 45)   # the person changed it meanwhile
        await TK.decide_ticket_service(pool, 1, tid, option_id="apply", actor="user", deliver_answer=False)
        check(UL.value("build.idle_minutes", await UL.read_bp(pool, 1)) == 45.0,
              "a card filed before the limit changed writes nothing")

        from server.bridge import memory_handlers as MH
        r = await MH.handle_calendar_append({
            "event_type": "scheduled_task", "summary": "invented task", "status": "scheduled",
            "payload": {"prompt": "invented", "max_runs_per_day": 100000}})
        check(not r.get("ok") and "scheduled.turns_per_day" in str(r.get("error")),
              "POST /api/calendar cannot store a task above the person's turn limit", r)
        r = await MH.handle_calendar_append({
            "event_type": "scheduled_task", "summary": "invented task", "status": "scheduled",
            "payload": {"prompt": "invented", "max_runs_per_day": 50}})
        check(r.get("ok"), "...and takes one under it", r)

        await UL.record_hit(pool, 1, "build.idle_minutes", "a build of project 7", "thread-x")
        hits = await UL.last_hits(pool, 1)
        check((hits.get("build.idle_minutes") or {}).get("what") == "a build of project 7",
              "a hit is recorded and read back for Settings > Limits", hits)
    finally:
        BS._pool, BS._entity_id = saved
        await pool.close()


def main() -> int:
    unit_checks()
    registry_checks()
    server = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not server and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server and docker unavailable — the P checks need a real database.")
    else:
        from kernel_schema import kernel_schema_db, throwaway_server
        with (contextlib.nullcontext(server) if server else throwaway_server()) as srv, \
                kernel_schema_db(srv, entity_id=1) as dsn:
            asyncio.run(pg_checks(dsn))
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
