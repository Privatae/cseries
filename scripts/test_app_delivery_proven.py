#!/usr/bin/env python3
"""Gate: DELIVERY IS PROVEN.

The defect: the build pipeline ended at "installed". The ship packed the tree,
installed it and returned; nothing started the app, read its ready check or
its output; a pod-backed app had no log_tail at all. The user pasted the
bootstrap failure into the chat three times while the host had nothing to go
on.

What this pins (no DB, no pod, no container — fakes at the seams):
  1. runtime_log: the pod's pushed lines land in the SAME file the supervisor
     writes; the file rotates; a non-zero exit is stamped on the lane state
     and a fresh wake clears it; the route exists and the middleware lets its
     shape through to in-handler auth; pushed stats (pid, memory) are kept.
  2. system.app_status reads that file for a pod app and reports last_exit;
     a dead process behind a Ready pod is the verdict, not "running".
  3. app_verify._judge: exit → failed; static → delivered; not running /
     ready {ok:false} / error-shaped tail → keep polling; clean → delivered.
  4. The proof end-to-end against a scripted status: a window app is started,
     proven and stopped again; the row is finalized with the typed envelope
     and delivered; a failed proof with auto-deliver ON decides + starts ONE
     seeded repair (origin.repair round 1); the budget stops it after
     MAX_AUTO_REPAIRS; auto-deliver OFF hands the seed to the host; a build
     in flight blocks it; a recorded exit fails fast.
  5. session._verify_delivery is deterministic text from the envelope.
  6. Wiring pins: ship_project_service takes verify=; the plan-end / follow-up
     hook calls ship_and_verify; the contract, the brief, the skill and the
     Workbench briefing carry the same rule; the image copies run.py.

Run: APP_ROOT=<repo> python3 scripts/test_app_delivery_proven.py
"""
import asyncio
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[delivery-proven] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[delivery-proven] FAIL {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakePool:
    """Records every execute/fetchrow; answers the few reads the proof makes."""

    def __init__(self, *, auto=True, in_flight=None, verify_in_flight=None):
        self.auto = auto
        self.in_flight = in_flight
        # An app_verify row already in_progress for this app.
        self.verify_in_flight = verify_in_flight
        self.executed: list[tuple[str, tuple]] = []
        self.inserted = 0

    async def fetchval(self, sql, *args):
        if "auto_merge" in sql:
            return self.auto
        if "task_type='factory_build'" in sql:
            return self.in_flight
        if "result->>'app_id'" in sql:
            return self.verify_in_flight
        return None

    async def fetchrow(self, sql, *args):
        if "INSERT INTO entity_tasks" in sql:
            self.inserted += 1
            self.executed.append((sql, args))
            return {"task_id": 900 + self.inserted}
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def main() -> int:
    from server.applications import registry, storage, supervisor as S
    from server.applications import harness_backend as hb
    from server.applications import runtime_log as RL
    from mastercode.runtime import tools, app_verify as AV, session as SESS

    tmp = Path(tempfile.mkdtemp(prefix="delivery-proven-"))
    storage.APPDATA_ROOT = tmp / "appdata"

    # ── 1. runtime_log ────────────────────────────────────────────────────
    n = RL.append_runtime_log("podapp", ["phase: booting", "E: no such exchange"], exit_code=None)
    path = RL.runtime_log_path("podapp")
    check("pushed lines land in the supervisor's log file",
          n == 2 and path.name == S.LOG_FILENAME and path.parent.name == S.LOG_SUBDIR
          and "no such exchange" in path.read_text())
    check("no exit stamped for exit_code=None", RL.last_exit("podapp") is None)
    RL.append_runtime_log("podapp", ["[app-runtime] command exited with code 2"], exit_code=2)
    ex = RL.last_exit("podapp")
    check("a non-zero exit is stamped on the lane state", ex is not None and ex["code"] == 2, repr(ex))
    check("the lane entry is app-rt shaped",
          (hb._state.get("podapp") or {}).get("harness") == hb._APP_RT_MARK)
    RL.clear_last_exit("podapp")
    check("clear_last_exit forgets it", RL.last_exit("podapp") is None)
    RL.append_runtime_log("podapp", ["[app-runtime] command exited with code -15"], exit_code=-15)
    check("a signal exit (the operator's scale-to-0) is a stop, never a recorded crash",
          RL.last_exit("podapp") is None)
    RL.append_runtime_log("podapp", ["old pod dying"], exit_code=2, pod="app-rt-podapp-aaa")
    check("an exit from the current pod is stamped", (RL.last_exit("podapp") or {}).get("code") == 2)
    RL.append_runtime_log("podapp", ["new pod booting"], exit_code=None, pod="app-rt-podapp-bbb")
    check("the first POST from a NEW pod clears the previous generation's exit",
          RL.last_exit("podapp") is None)
    RL.append_runtime_log("podapp", ["straggler"], exit_code=2, pod="app-rt-podapp-aaa")
    check("a straggler exit from the superseded pod is ignored", RL.last_exit("podapp") is None)
    RL.record_stats("podapp", {"pid": 17, "mem_bytes": 123456789})
    stt = RL.last_stats("podapp")
    check("pushed stats (pid, memory) are kept on the lane state",
          stt is not None and stt["pid"] == 17 and stt["mem_bytes"] == 123456789, repr(stt))
    # rotation
    path.write_bytes(b"x" * (S.LOG_ROTATE_BYTES + 10))
    RL.append_runtime_log("podapp", ["after rotate"])
    check("the file rotates past LOG_ROTATE_BYTES",
          path.with_suffix(path.suffix + ".1").exists() and path.read_text().strip() == "after rotate")
    src = (_ROOT / "server/applications/harness_backend.py").read_text()
    check("a fresh wake pops last_exit", 'st.pop("last_exit", None)' in src)
    from server.routes import applications as A
    paths = {getattr(r, "path", "") for r in A.router.routes}
    check("POST /api/applications/{app_id}/runtime-log exists",
          "/api/applications/{app_id}/runtime-log" in paths)
    from server.middleware import localhost_only as L
    # A standalone Host has no app runtime pods: the gate exempts nothing
    # here, so a runtime-log POST from off the box needs the owner's session.
    check("middleware does not exempt the runtime-log path on a standalone Host",
          not any("/api/applications/x/runtime-log".startswith(p) for p in L._SELF_AUTH_PREFIXES)
          and not hasattr(L, "_SELF_AUTH_SHAPES"))
    rsrc = (_ROOT / "server/routes/applications.py").read_text()
    check("the route checks the source token before writing",
          "check_app_source_token(pool, app_id, presented)" in
          rsrc.split("async def app_runtime_log(")[1].split("\nasync def ")[0])

    # ── 2. app_status for a pod app ───────────────────────────────────────
    d = tmp / "podapp"
    d.mkdir(exist_ok=True)
    (d / "app.yaml").write_text(textwrap.dedent("""\
        id: podapp
        name: P
        entrypoint: index.html
        provenance: custom
        version: 0.1.0
        permissions: []
        resources:
          baseline_memory_mi: 512
        network: [example.com]
        runtime:
          kind: process
          image: python
          command: ["bash", "run.sh"]
          ready_path: /api/v1/ping
        """))
    row = registry.InstalledApp.__new__(registry.InstalledApp)
    row.__dict__.update(dict(id="podapp", name="P", version="0.1.0", provenance="custom",
                             entrypoint="index.html", source_dir=str(d), status="active",
                             permissions=[], resident_enabled=None))
    rows = {"podapp": row}

    async def fake_get(pool, app_id):
        return rows.get(app_id)
    registry.get = fake_get
    from server import brain_service
    brain_service.get_pool = lambda: object()

    class PodView:
        app_id = "podapp"
        pid = None
        started_at = 0.0
        resident = False
        base_url = "http://[::1]:1"

        def is_alive(self):
            return True

    class FakeSup:
        started: list = []
        stopped: list = []
        opened: list = []
        closed: list = []

        def get(self, app_id):
            return PodView()

        def list_running(self):
            return [PodView()]

        async def start_app(self, app_id, *, pool, resident=None):
            self.started.append(app_id)

        async def stop_app(self, app_id):
            self.stopped.append(app_id)
            return True

        async def open_window(self, *, installed, manifest, window_id, kernel_url=None):
            self.opened.append((installed.id, window_id))
            return PodView()

        async def close_window(self, app_id, window_id):
            self.closed.append((app_id, window_id))
    S._SUPERVISOR = FakeSup()
    RL.append_runtime_log("podapp", ["Traceback (most recent call last):", "boom"], exit_code=1)
    st = run(tools._exec_system_app_status({"app_id": "podapp"}))["result"]
    check("app_status reads the pushed log for a pod app", "boom" in st["log_tail"], repr(st)[:200])
    check("app_status carries last_exit", (st.get("last_exit") or {}).get("code") == 1, repr(st.get("last_exit")))
    check("a dead process behind a live pod is the verdict",
          st["verdict"].startswith("the app's process EXITED"), st["verdict"])
    check("app_status carries the pod's pushed memory",
          (st.get("runtime_stats") or {}).get("mem_bytes") == 123456789, repr(st.get("runtime_stats")))
    RL.clear_last_exit("podapp")
    ssrc_sys = (_ROOT / "server/routes/system.py").read_text()
    check("the machine panel attributes a pod app's pushed memory",
          "last_stats(" in ssrc_sys and '"placement"] = "pod"' in ssrc_sys)

    # ── 3. _judge ─────────────────────────────────────────────────────────
    J = AV._judge
    check("exit → failed", J({"installed": True, "runtime_kind": "process"}, {"code": 2})[0] == "failed")
    check("static → delivered", J({"installed": True, "runtime_kind": "static"}, None)[0] == "delivered")
    check("not installed → failed", J({"installed": False}, None)[0] == "failed")
    check("not running → keep polling",
          J({"installed": True, "runtime_kind": "process", "running": False}, None)[0] is None)
    v, why = J({"installed": True, "runtime_kind": "process", "running": True,
                "ready": {"ok": False, "status": 200, "body": {"ok": False, "phase": "installing"}}}, None)
    check("ready {ok:false} → keep polling, naming the phase", v is None and "installing" in why, why)
    v, why = J({"installed": True, "runtime_kind": "process", "running": True,
                "ready": {"ok": True, "status": 200, "body": {"ok": True}},
                "log_tail": "phase: done\nERROR could not load markets\n"}, None)
    check("ready with an error-shaped tail → keep polling", v is None and "error-shaped" in why, why)
    # A LOGGER NAME is not an error. These four lines are uvicorn's healthy
    # startup banner, in the exact shape uvicorn writes it;
    # `\berror\b` matched every one of them because `.` is a word boundary, so
    # the app could never be proven and auto-deliver dispatched a coder at it.
    _uvicorn_banner = (
        "2026-01-05 10:00:01,100 - uvicorn.error - INFO - Started server process [41]\n"
        "2026-01-05 10:00:01,102 - uvicorn.error - INFO - Waiting for application startup.\n"
        "2026-01-05 10:00:01,104 - uvicorn.error - INFO - Application startup complete.\n"
        "2026-01-05 10:00:01,106 - uvicorn.error - INFO - Uvicorn running on http://127.0.0.1:18080\n"
        "2026-01-05 10:00:08,200 - tradebot.worker - INFO - Bot heartbeat. state='RUNNING'\n")
    v, why = J({"installed": True, "runtime_kind": "process", "running": True,
                "ready": {"ok": True, "status": 200, "body": {"status": "pong"}},
                "log_tail": _uvicorn_banner}, None)
    check("a healthy uvicorn banner is NOT error-shaped (uvicorn.error is a name)",
          v == "delivered", why)
    # …and the words that mean it really did break still do, dotted or not.
    for _tail, _lbl in (
            ("ERROR could not load markets", "bare ERROR"),
            ("the bootstrap failed at step 3", "failed"),
            ("Traceback (most recent call last):", "traceback"),
            ("requests.exceptions.ConnectionError: refused", "dotted exception"),
            ("FATAL: out of memory", "fatal")):
        v, why = J({"installed": True, "runtime_kind": "process", "running": True,
                    "ready": {"ok": True, "status": 200, "body": {"status": "pong"}},
                    "log_tail": f"phase: running\n{_tail}\n"}, None)
        check(f"a real error still stops delivery ({_lbl})",
              v is None and "error-shaped" in why, f"{_lbl}: {why}")
    v, why = J({"installed": True, "runtime_kind": "process", "running": True,
                "ready": {"ok": True, "status": 200, "body": {"status": "pong"}},
                "log_tail": "phase: running\n"}, None)
    check("ready + clean tail → delivered", v == "delivered", why)
    seed = AV._seed_text("podapp", "0.4.1", "failed", "exited", {"log_tail": "E: boom",
                         "ready": {"path": "/api/v1/ping", "status": 200, "ok": False,
                                   "body": {"ok": False}}}, {"code": 2}, 1)
    check("the seed carries the log, the exit and the three rules",
          "E: boom" in seed and "code 2" in seed and "EVERY boot" in seed
          and "ready_path must answer" in seed and "bump" in seed)

    dsrc = (_ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()

    # ── 4. the proof end to end ───────────────────────────────────────────
    AV.POLL_S = 0.01
    AV.MIN_WINDOW_S = 1
    AV.MAX_WINDOW_S = 1   # the proof window: manifest ready_timeout + 120 clamps here
    delivered: list = []

    class FakeDispatch:
        async def _deliver(self, task_id):
            delivered.append(task_id)

    class FakeRuntime:
        agent_dispatch = FakeDispatch()
    brain_service.get_runtime = lambda: FakeRuntime()

    script: list[dict] = []
    orig_status = tools._exec_system_app_status

    async def scripted(args):
        s = script.pop(0) if len(script) > 1 else script[0]
        return {"status": "ok", "result": s}
    tools._exec_system_app_status = scripted

    GOOD = {"installed": True, "runtime_kind": "process", "running": True,
            "ready": {"ok": True, "status": 200, "body": {"status": "pong"}}, "log_tail": "ok\n",
            "log_errors": []}
    BAD = {"installed": True, "runtime_kind": "process", "running": True,
           "ready": {"ok": False, "status": 200, "body": {"ok": False, "error": "no markets"}},
           "log_tail": "ERROR could not load markets\n", "log_errors": ["ERROR could not load markets"]}

    async def _proof(**kw):
        return await AV._run_verify(**kw)

    # 4a. delivered: window app started, proven twice, stopped again
    pool = FakePool(auto=True)
    FakeSup.started.clear(); FakeSup.stopped.clear()
    script[:] = [{**BAD}, {**GOOD}, {**GOOD}, {**GOOD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=901,
               origin={"session_id": "s1"}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='completed'" in e[0]]
    check("a proven app finalizes the row completed", bool(fin), str(pool.executed)[-300:])
    env = json.loads(fin[0][1][0]) if fin else {}
    check("the row carries the typed envelope", (env.get("verify") or {}).get("verdict") == "delivered"
          and "ready" in (env.get("verify") or {}), json.dumps(env)[:200])
    check("a window app is opened under the proof's own window and closed again — never start/stop",
          FakeSup.opened == [("podapp", AV.PROOF_WINDOW_ID)] and not FakeSup.started
          and not FakeSup.stopped, f"{FakeSup.opened} {FakeSup.started} {FakeSup.stopped}")
    # podapp runs in a pod: the close is the lane's graceful window-closed hook
    check("...and a pod app's close goes through the lane's graceful hook",
          not FakeSup.closed and hb._state.get("podapp") is not None)
    check("the row was delivered", 901 in delivered, repr(delivered))

    async def _open():
        tid = await AV.start_verify(pool=pool, entity_id=9128, project_id=1, app_id="podapp",
                                    origin={"session_id": "s1"}, reason="ship")
        await asyncio.sleep(0.3)   # let the detached proof run on this loop
        return tid
    script[:] = [{**GOOD}]
    tid = run(_open())
    check("start_verify opens the registry row with task_type app_verify and runs detached",
          tid is not None and any(
              "INSERT INTO entity_tasks" in e[0] and e[1][3] == AV.TASK_TYPE for e in pool.executed)
          and tid in delivered, repr((tid, delivered)))

    # 4a-bis. ONE proof per app: the plan-end chain
    # and the ship route both reached the same shipped app 40s apart, opened
    # tasks 69 and 70, and delivered the identical "PROVEN running" message
    # twice a second apart. A proof already in flight wins; the second caller
    # gets its id to report against instead of a duplicate row.
    dpool = FakePool(auto=False, verify_in_flight=69)
    dtid = run(AV.start_verify(pool=dpool, entity_id=9128, project_id=1,
                               app_id="podapp", reason="ship"))
    check("a proof already in flight blocks a second one, and returns its id",
          dtid == 69 and dpool.inserted == 0, repr((dtid, dpool.inserted)))
    check("...and the in-flight check is scoped to THIS app, not the entity",
          "result->>'app_id' = $3" in
          (_ROOT / "brain/mastercode/runtime/app_verify.py").read_text())
    # An orphaned proof row must never wedge the app's next delivery proof.
    # A proof is a detached in-process task, so a kernel restart kills it; the
    # boot reap has to terminalise it the way it does the other three, and the
    # de-dup only counts a row still inside its own window.
    _ad = (_ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
    check("the boot reap terminalises orphaned app_verify rows too",
          "'agent','factory_build','fan_out','app_verify'" in _ad)
    check("...and the de-dup ignores a proof past its own window",
          "2.0 * COALESCE((result->>'timeout_s')::float" in
          (_ROOT / "brain/mastercode/runtime/app_verify.py").read_text())

    # 4b. failed + auto ON → one seeded repair, tagged origin.repair round 1
    started_repairs: list = []

    async def fake_task_start(pool, entity_id, **kw):
        started_repairs.append(kw)
        return 777

    async def fake_run_phase_build(**kw):
        return None
    tools._factory_build_task_start = fake_task_start
    tools._run_phase_build = fake_run_phase_build
    from server.routes import review as RV

    async def fake_resolve(explicit, model=None):
        return ("kimi", None, None)
    RV.resolve_build_target = fake_resolve

    pool = FakePool(auto=True)
    delivered.clear()
    script[:] = [{**BAD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=902,
               origin={"session_id": "s1"}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='failed'" in e[0]]
    check("a failed proof finalizes the row failed with the reason",
          bool(fin) and "not ready within" in fin[0][1][0], str(fin)[:200])
    env = json.loads(fin[0][1][2]) if fin else {}
    check("the failed envelope carries the seed and the repair decision",
          "seed" in (env.get("verify") or {}) and (env.get("repair") or {}).get("start") is True
          and env["repair"]["round"] == 1 and env["repair"]["harness"] == "kimi", json.dumps(env)[:300])
    check("the repair was started AFTER delivery, seeded, tagged origin.repair",
          902 in delivered and len(started_repairs) == 1
          and started_repairs[0]["origin"]["repair"] == {"app_id": "podapp", "round": 1}
          and "REPAIR ROUND 1" in started_repairs[0]["intent"]
          and started_repairs[0]["harness"] == "kimi", str(started_repairs)[:300])

    # 4c. budget spent → no repair, why says so
    pool = FakePool(auto=True)
    started_repairs.clear()
    script[:] = [{**BAD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=903,
               origin={}, repair_round=AV.MAX_AUTO_REPAIRS, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='failed'" in e[0]]
    env = json.loads(fin[0][1][2]) if fin else {}
    check("past the budget no repair starts and the reason is stated",
          not started_repairs and (env.get("repair") or {}).get("start") is False
          and "budget" in (env.get("repair") or {}).get("why", ""), json.dumps(env.get("repair")))

    # 4d. auto OFF → the host holds the seed
    pool = FakePool(auto=False)
    script[:] = [{**BAD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=904,
               origin={}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='failed'" in e[0]]
    env = json.loads(fin[0][1][2]) if fin else {}
    check("auto-deliver off → no repair, the host dispatches",
          not started_repairs and (env.get("repair") or {}).get("start") is False
          and "host dispatches" in (env.get("repair") or {}).get("why", ""))

    # 4e. a build in flight → no repair
    pool = FakePool(auto=True, in_flight=55)
    script[:] = [{**BAD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=905,
               origin={}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='failed'" in e[0]]
    env = json.loads(fin[0][1][2]) if fin else {}
    check("a build already running blocks the repair",
          not started_repairs and "task 55" in (env.get("repair") or {}).get("why", ""))

    # 4f. a recorded exit fails fast
    pool = FakePool(auto=False)
    RL.append_runtime_log("podapp", ["died"], exit_code=3)
    script[:] = [{**GOOD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=906,
               origin={}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='failed'" in e[0]]
    check("a recorded non-zero exit fails the proof even over a green ready",
          bool(fin) and "exited with code 3" in fin[0][1][0], str(fin)[:200])
    RL.clear_last_exit("podapp")

    # 4g. a helper app: no supervisor process — woken on the lane, judged the same
    woke: list = []

    async def fake_wake(app_id):
        woke.append(app_id)
        return "[fd00::1]:8850"
    orig_wake = hb.ensure_awake_app
    hb.ensure_awake_app = fake_wake
    dh = tmp / "helperapp"
    dh.mkdir(exist_ok=True)
    (dh / "app.yaml").write_text(textwrap.dedent("""\
        id: helperapp
        name: H
        entrypoint: index.html
        provenance: community
        version: 0.1.0
        permissions: []
        resources:
          baseline_memory_mi: 64
        network: [example.com]
        runtime:
          kind: helper
        """))
    hrow = registry.InstalledApp.__new__(registry.InstalledApp)
    hrow.__dict__.update(dict(id="helperapp", name="H", version="0.1.0", provenance="community",
                              entrypoint="index.html", source_dir=str(dh), status="active",
                              permissions=[], resident_enabled=None))
    rows["helperapp"] = hrow
    pool = FakePool(auto=False)
    FakeSup.started.clear(); FakeSup.opened.clear()
    HGOOD = {**GOOD, "runtime_kind": "helper"}
    script[:] = [{**HGOOD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="helperapp", task_id=907,
               origin={}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='completed'" in e[0]]
    check("a helper app is woken on the lane (no supervisor start) and proven",
          woke == ["helperapp"] and not FakeSup.started and not FakeSup.opened and bool(fin),
          f"{woke} {FakeSup.started} {str(pool.executed)[-200:]}")
    hb.ensure_awake_app = orig_wake
    # the envelope's lifecycle is the EFFECTIVE one (manifest resident, user switched it off)
    (d / "app.yaml").write_text((d / "app.yaml").read_text().replace(
        "  ready_path: /api/v1/ping\n", "  ready_path: /api/v1/ping\n  lifecycle: resident\n"))
    row.__dict__["resident_enabled"] = False
    pool = FakePool(auto=False)
    FakeSup.started.clear(); FakeSup.opened.clear()
    script[:] = [{**GOOD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=908,
               origin={}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='completed'" in e[0]]
    env = json.loads(fin[0][1][0]) if fin else {}
    check("resident manifest + user switch OFF → started as a window app, envelope says window",
          FakeSup.opened and not FakeSup.started and env.get("verify", {}).get("lifecycle") == "window",
          f"{FakeSup.opened} {FakeSup.started} {env.get('verify', {}).get('lifecycle')}")
    row.__dict__["resident_enabled"] = None
    pool = FakePool(auto=False)
    FakeSup.started.clear(); FakeSup.opened.clear()
    script[:] = [{**GOOD}]
    run(_proof(pool=pool, entity_id=9128, project_id=1, app_id="podapp", task_id=909,
               origin={}, repair_round=0, harness=None, model=None))
    fin = [e for e in pool.executed if "SET status='completed'" in e[0]]
    env = json.loads(fin[0][1][0]) if fin else {}
    check("resident manifest + switch untouched → start_app, stays up, envelope says resident",
          FakeSup.started == ["podapp"] and not FakeSup.opened
          and env.get("verify", {}).get("lifecycle") == "resident")
    check("the proof's row carries max_repairs and its own timeout", 
          env.get("max_repairs") == AV.MAX_AUTO_REPAIRS
          and any("timeout_s" in (e[1][0] if e[1] else "") for e in pool.executed))
    check("the seed redacts secret-shaped output",
          "[redacted]" in AV.redact("api_key=abcdef0123456789 and sk_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd")
          and "phase: running" == AV.redact("phase: running"))
    check("proofs are pinned in _PROOF_TASKS while they run", isinstance(AV._PROOF_TASKS, dict))
    check("the monitor and the bench see app_verify rows",
          dsrc.count("'app_verify'") >= 3 and "_PROOF_TASKS" in dsrc)
    tools._exec_system_app_status = orig_status

    # ── 5. the delivered turn is deterministic ───────────────────────────
    txt, toast, lvl = SESS._verify_delivery({
        "status": "completed", "app_id": "podapp",
        "verify": {"verdict": "delivered", "reason": "running and ready with a clean log",
                   "elapsed_s": 42, "version": "0.4.2", "kind": "process", "lifecycle": "resident"}})
    check("delivered turn: proven, resident, from the envelope",
          txt.startswith("Delivered: podapp v0.4.2") and "PROVEN" in txt and "resident" in txt
          and lvl == "success", txt)
    txt, toast, lvl = SESS._verify_delivery({
        "status": "failed", "app_id": "podapp",
        "verify": {"verdict": "failed", "reason": "not ready within 180s", "version": "0.4.1",
                   "log_errors": ["ERROR could not load markets"], "exit": {"code": 2}, "seed": "x"},
        "repair": {"start": True, "round": 1, "harness": "kimi"}})
    check("not-delivered turn names the reason, the exit, the log and the repair round",
          txt.startswith("Not delivered: podapp v0.4.1") and "code 2" in txt
          and "could not load markets" in txt and "repair round 1" in txt and lvl == "error", txt)
    txt, _t, _l = SESS._verify_delivery({
        "status": "failed", "app_id": "podapp", "verify": {"verdict": "failed", "reason": "r", "seed": "SEED"},
        "repair": {"start": False, "round": 3, "why": "the automatic repair budget (2 rounds) is spent"}})
    check("budget-spent turn points the host at the seed", "budget" in txt and "seed" in txt, txt)
    ssrc = (_ROOT / "brain/mastercode/runtime/session.py").read_text()
    check("deliver_agent_result routes app_verify rows to the deterministic text",
          "_is_verify = (record.get(\"task_type\") == \"app_verify\")" in ssrc
          and "if _is_verify:\n            content, toast_summary, toast_level = _verify_delivery(record)" in ssrc)
    check("the delivery record carries verify + repair + app_id",
          '"verify": res.get("verify")' in dsrc and '"repair": res.get("repair")' in dsrc)

    # ── 6. wiring pins ────────────────────────────────────────────────────
    check("ship_project_service takes verify= and starts the proof",
          "async def ship_project_service(pool, project_id: int, *, verify: bool = True" in rsrc
          and "start_verify(" in rsrc)
    tsrc = (_ROOT / "brain/mastercode/runtime/tools.py").read_text()
    rpb = tsrc.split("async def _run_phase_build(")[1].split("\nasync def _exec_build(")[0]
    check("the plan-end / follow-up hook ships and proves under the one switch",
          "ship_and_verify(" in rpb and "if _plan_complete or phase_id is None:" in rpb
          and '"auto_ship"' in rpb)
    check("the hook reads origin.repair to carry the round", '_orig.get("repair")' in rpb)
    from server.applications.build_contract import app_sdk_build_contract, host_runtime_brief
    c = app_sdk_build_contract()
    check("the contract says READY MEANS READY and appdata persists",
          "READY MEANS READY" in c and "PERSISTS ACROSS RE-INSTALLS" in c and "PROVES it" in c)
    b = host_runtime_brief()
    check("the host brief carries 'Delivery is proven'",
          "## Delivery is proven, not announced" in b and "ship_project_app" in b
          and "build(action='continue', message=<the seed>)" in b)
    # The rule has ONE copy — the skill. The
    # Workbench manifest points at build-workflow instead of restating it.
    wb = (_ROOT / "system-apps/workbench/app.yaml").read_text()
    check("the Workbench briefing points at the skill instead of restating the rule",
          "build-workflow" in wb and "DELIVERY IS PROVEN, NOT ANNOUNCED" not in wb)
    sk = (_ROOT / "default-skills/build-workflow.md").read_text()
    check("the build-workflow skill carries §6", "### 6. DELIVERY IS PROVEN" in sk)
    check("the build-workflow skill carries what to say before and after the proof",
          "installed, being proven" in sk and "say \"delivered\" only when it says delivered" in sk)
    reg = (_ROOT / "brain/mastercode/public_tool_registry.py").read_text()
    check("ship_project_app's description says install is not delivery",
          "INSTALL IS NOT DELIVERY" in reg)
    df = (_ROOT / "sidecars/app-runtime-python/Dockerfile").read_text()
    check("the toolchain image ships run.py", "run.py /opt/app-runtime/run.py" in df)
    import ast
    rp = (_ROOT / "sidecars/app-runtime-python/run.py").read_text()
    ast.parse(rp)
    check("run.py parses, posts to /runtime-log and samples memory",
          "/runtime-log" in rp and "mem_bytes" in rp and "memory.current" in rp)

    if FAILED:
        print(f"FAIL: delivery proven — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: delivery proven — install starts, proves and repairs; a pod app's log, exit and memory reach the host")
    return 0


if __name__ == "__main__":
    sys.exit(main())
