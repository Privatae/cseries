#!/usr/bin/env python3
"""The kill — end a running build or compare on its engine's pod, now.

Before it, the only stops were build(action='stop') — halts auto-advance at
the phase boundary and lets the running phase finish — and the idle
watchdog, which never stops a run that keeps working. A run that was busy
and going nowhere burned tokens until it ended by itself. What this pins:

  1. A kill with nothing of the session on a pod is an ordinary answer, and
     touches no pod.
  2. A kill marks EVERY run on the pod before it stops the pod — the pod's
     /stop ends all of them — and names the runs of other sessions it took
     with it; each run gets a `status/killed` frame.
  3. A stop that fails un-marks the runs: they are still running, so they are
     never reported (or later labelled) as killed.
  4. `harness` narrows a compare to one variant's pod.
  5. A killed run's result says who stopped it, keeps the pod's own words as
     error_detail, and is never ok.
  6. halt_chain_for_session halts only a factory build's project.
  7. The Host's build(action='kill', project_id) kills the project's running
     builds and compares: a fan-out by its recorded session, a factory build by
     factory-build-<id>; nothing building is an ordinary answer.
  8. harness_stop still answers 400/502 exactly as before the refactor.
  9. The build tool's schema offers kill and adds no parameter for it.

Container-free: the pod stop, the frame writer and the pool are stubbed.

Run: APP_ROOT=<repo> python3 scripts/test_harness_run_kill.py
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from fastapi import HTTPException  # noqa: E402

from server.routes import review as rv  # noqa: E402
from mastercode.runtime import tools as t  # noqa: E402
from mastercode import public_tool_registry as ptr  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


STOPS: list = []
FRAMES: list = []
STOP_ANSWER: dict = {}


async def _fake_pod_stop(harness):
    STOPS.append(harness)
    return {"harness": harness, **STOP_ANSWER}


def _fake_frame(session_dir, label, kind, payload):
    FRAMES.append((pathlib.Path(session_dir).name, label, kind, dict(payload)))


def _reset():
    rv._live_pod_runs.clear()
    # Each check runs on its own event loop; a lock a contended kill used is
    # bound to the loop it waited on.
    getattr(rv, "_kill_locks", {}).clear()
    getattr(rv, "_build_prep", {}).clear()
    getattr(rv, "_kill_before_start", {}).clear()
    STOPS.clear()
    FRAMES.clear()
    STOP_ANSWER.clear()
    STOP_ANSWER.update({"ok": True, "held": True, "stopped": 3, "could_not_stop": 0})


_saved = (rv._pod_stop, rv._append_run_frame)
rv._pod_stop = _fake_pod_stop
rv._append_run_frame = _fake_frame

S = pathlib.Path("/tmp/kill-test-sessions")
try:
    # ── 1. nothing running ──────────────────────────────────────────────────
    print("\n1. nothing of the session on a pod")
    _reset()
    out = run(rv.kill_session_runs("factory-build-41"))
    check("answers ok with nothing stopped", out["ok"] and out["stopped"] == [], out)
    check("says so in words", "nothing" in (out.get("detail") or ""), out)
    check("touches no pod", STOPS == [], STOPS)

    # ── 2. marks every run on the pod, names the others ─────────────────────
    print("\n2. the pod's stop takes every run on it")
    _reset()
    mine = rv._register_live_run("codex", S / "factory-build-41", "codex")
    theirs = rv._register_live_run("codex", S / "fan-7", "codex")
    elsewhere = rv._register_live_run("claude-code", S / "fan-7", "claude-code")
    out = run(rv.kill_session_runs("factory-build-41", by="user"))
    check("the codex pod was stopped once, and only it", STOPS == ["codex"], STOPS)
    check("both runs on that pod are marked killed by the user",
          mine["killed_by"] == "user" and theirs["killed_by"] == "user", (mine, theirs))
    check("the run on another engine is untouched", elsewhere["killed_by"] is None, elsewhere)
    names = {(s["session_id"], s["other_session"]) for s in out["stopped"]}
    check("the answer names this run and the other session's run",
          names == {("factory-build-41", False), ("fan-7", True)}, out["stopped"])
    killed_frames = [f for f in FRAMES if f[2] == "status" and f[3].get("state") == "killed"]
    check("each stopped run gets a status/killed frame",
          {(f[0], f[1]) for f in killed_frames} == {("factory-build-41", "codex"), ("fan-7", "codex")},
          FRAMES)
    rv._unregister_live_run("codex", mine)
    rv._unregister_live_run("codex", theirs)
    check("unregistering the last run on a pod drops the pod's entry",
          "codex" not in rv._live_pod_runs, rv._live_pod_runs)

    # ── 3. a failed stop un-marks ───────────────────────────────────────────
    print("\n3. a stop that fails reports nothing as killed")
    _reset()
    STOP_ANSWER.clear()
    STOP_ANSWER.update({"ok": False, "held": True, "error": "the running codex image has no /stop yet"})
    e1 = rv._register_live_run("codex", S / "factory-build-42", "codex")
    out = run(rv.kill_session_runs("factory-build-42"))
    check("not ok, with the pod's reason", not out["ok"] and out["errors"]
          and "no /stop" in out["errors"][0]["error"], out)
    check("nothing listed as stopped", out["stopped"] == [], out)
    check("the run is un-marked — it is still running", e1["killed_by"] is None, e1)
    check("no killed frame was written", not any(f[3].get("state") == "killed" for f in FRAMES), FRAMES)

    # ── 4. narrowing to one variant ─────────────────────────────────────────
    print("\n4. harness narrows a compare to one variant's pod")
    _reset()
    a = rv._register_live_run("codex", S / "fan-9", "codex")
    b = rv._register_live_run("claude-code", S / "fan-9", "claude-code")
    run(rv.kill_session_runs("fan-9", harness="claude-code", by="user"))
    check("only the claude-code pod was stopped", STOPS == ["claude-code"], STOPS)
    check("only that variant is marked", b["killed_by"] == "user" and a["killed_by"] is None, (a, b))
    run(rv.kill_session_runs("fan-9", by="host"))
    check("without harness, every pod the session is still running on is stopped; a pod "
          "an earlier kill already stopped for it is not stopped again, and that run "
          "keeps who killed it",
          sorted(STOPS) == ["claude-code", "codex"] and a["killed_by"] == "host"
          and b["killed_by"] == "user", (STOPS, a, b))

    # ── 5. the label ────────────────────────────────────────────────────────
    print("\n5. a killed run's result says who stopped it")
    r = {"ok": True, "exit_code": -9, "error": "process exited -9"}
    rv._apply_kill_label(r, {"killed_by": "host"})
    check("never ok", r["ok"] is False, r)
    check("names who", r["killed_by"] == "host" and "your Host" in r["error"], r)
    check("keeps the pod's own words", r.get("error_detail") == "process exited -9", r)
    r2 = {"ok": True}
    rv._apply_kill_label(r2, {"killed_by": None})
    check("an unkilled run is left alone", r2 == {"ok": True}, r2)
    r3 = {"ok": False, "error": rv._killed_sentence("user")}
    rv._apply_kill_label(r3, {"killed_by": "user"})
    check("the kill sentence is not copied into error_detail", "error_detail" not in r3, r3)

    # ── 5b. the halt lands BEFORE the stop ──────────────────────────────────
    print("\n5b. the kill halts the chain before it stops the pod")
    _reset()
    ORDER: list = []
    _real_halt = rv.halt_chain_for_session

    async def _order_halt(sid):
        ORDER.append(("halt", sid))
        return 9239

    async def _order_stop(h):
        ORDER.append(("stop", h))
        return {"harness": h, "ok": True, "held": True, "stopped": 1, "could_not_stop": 0}

    rv.halt_chain_for_session, rv._pod_stop = _order_halt, _order_stop
    try:
        rv._register_live_run("codex", S / "factory-build-43", "codex")
        out = run(rv.kill_session_runs("factory-build-43"))
        check("halt first, then stop — the retry decision sees the halt",
              ORDER == [("halt", "factory-build-43"), ("stop", "codex")], ORDER)
        check("the answer carries the halted project", out.get("halted_project_id") == 9239, out)
        ORDER.clear()
        out = run(rv.kill_session_runs("factory-build-44"))
        check("nothing running → nothing halted, nothing stopped",
              ORDER == [] and out.get("halted_project_id") is None, (ORDER, out))
    finally:
        rv.halt_chain_for_session, rv._pod_stop = _real_halt, _fake_pod_stop

    # ── 5c. the run ends while the halt is written ──────────────────────────
    print("\n5c. a run that ends during the halt takes no other work down with it")
    _reset()
    target = rv._register_live_run("codex", S / "factory-build-45", "codex")
    bystander = rv._register_live_run("codex", S / "fan-8", "codex")

    async def _halt_while_run_ends(sid):
        # The pod hands the run back while the halt is being written.
        rv._unregister_live_run("codex", target)
        return 9239

    rv.halt_chain_for_session = _halt_while_run_ends
    try:
        out = run(rv.kill_session_runs("factory-build-45"))
        check("the pod is not stopped — the run being killed is already over",
              STOPS == [], STOPS)
        check("the other session's run on that pod is untouched",
              bystander["killed_by"] is None, bystander)
        check("the answer says it ended first, nothing stopped, the halt reported",
              out["ok"] and out["stopped"] == [] and "ended" in (out.get("detail") or "")
              and out.get("halted_project_id") == 9239, out)
    finally:
        rv.halt_chain_for_session = _real_halt
        rv._unregister_live_run("codex", bystander)

    # ── 6. halting the chain ────────────────────────────────────────────────
    print("\n6. halt_chain_for_session")
    _saved_pool = rv.brain_service.get_pool

    class _HaltPool:
        def __init__(self, project_id):
            self.project_id = project_id
            self.sql: list = []

        async def fetchval(self, q, *args):
            self.sql.append((q, args))
            return self.project_id

        async def execute(self, q, *args):
            self.sql.append((q, args))

    hp = _HaltPool(9239)
    rv.brain_service.get_pool = lambda: hp
    try:
        got = run(rv.halt_chain_for_session("factory-build-411"))
        check("a factory build halts its project", got == 9239, got)
        check("by setting build_chain_halt on that project only",
              any("build_chain_halt" in q and args[0] == 9239 for q, args in hp.sql), hp.sql)
        hp2 = _HaltPool(9239)
        rv.brain_service.get_pool = lambda: hp2
        check("a compare session halts nothing and reads nothing",
              run(rv.halt_chain_for_session("fan-9")) is None and hp2.sql == [], hp2.sql)
    finally:
        rv.brain_service.get_pool = _saved_pool

    # ── 7. the Host's kill ──────────────────────────────────────────────────
    print("\n7. build(action='kill') — the project's running builds")
    CALLS: list = []

    async def _fake_kill(session_id, *, harness=None, by="user"):
        CALLS.append((session_id, harness, by))
        return {"ok": True, "session_id": session_id, "errors": [],
                "halted_project_id": 9239 if session_id.startswith("factory-build-") else None,
                "stopped": [{"harness": "codex", "session_id": session_id,
                             "label": "codex", "other_session": False}]}

    _sk = rv.kill_session_runs
    rv.kill_session_runs = _fake_kill

    class _TaskPool:
        def __init__(self, rows):
            self.rows = rows
            self.sql: list = []

        async def fetch(self, q, *args):
            self.sql.append((q, args))
            return self.rows

    try:
        out = run(t._exec_build({"action": "kill", "project_id": "x"}, 3, _TaskPool([]), "s"))
        check("no usable project_id → refused, nothing killed", out.get("status") != "ok" and not CALLS, out)
        p0 = _TaskPool([])
        out = run(t._exec_build({"action": "kill", "project_id": 9239}, 3, p0, "s"))
        check("nothing building → an ordinary answer, nothing killed",
              out.get("status") == "ok" and out["result"]["stopped"] == [] and not CALLS, out)
        check("…reading only this entity's in-progress builds and compares of that project",
              p0.sql and p0.sql[0][1] == (3, 9239) and "status='in_progress'" in p0.sql[0][0]
              and "'factory_build', 'fan_out'" in p0.sql[0][0], p0.sql)
        rows = [{"task_id": 411, "task_type": "factory_build", "sid": None},
                {"task_id": 412, "task_type": "fan_out", "sid": "fan-xyz"}]
        out = run(t._exec_build({"action": "kill", "project_id": 9239, "harness": "codex"},
                                3, _TaskPool(rows), "s"))
        check("each running task is killed by its session, by the host, narrowed to the engine",
              CALLS == [("factory-build-411", "codex", "host"), ("fan-xyz", "codex", "host")], CALLS)
        check("…the answer names the tasks and the halted project",
              out.get("status") == "ok" and out["result"]["halted_project_id"] == 9239
              and "411" in out["result"]["next_step"] and "412" in out["result"]["next_step"], out)
        check("…and each stopped run carries its task id",
              sorted(s["task_id"] for s in out["result"]["stopped"]) == [411, 412], out)

        async def _kill_stop_failed(session_id, *, harness=None, by="user"):
            return {"ok": False, "session_id": session_id, "stopped": [],
                    "errors": [{"harness": "codex", "error": "the pod did not answer"}],
                    "halted_project_id": 9239}

        rv.kill_session_runs = _kill_stop_failed
        out = run(t._exec_build({"action": "kill", "project_id": 9239}, 3,
                                _TaskPool(rows[:1]), "s"))
        check("a failed stop is an error that still says the plan is halted",
              out.get("status") == "error" and "did not answer" in out["result"]
              and "Auto-advance is halted" in out["result"]
              and out.get("halted_project_id") == 9239, out)

        async def _kill_ended_first(session_id, *, harness=None, by="user"):
            return {"ok": True, "session_id": session_id, "stopped": [], "errors": [],
                    "halted_project_id": 9239, "detail": "it ended before the kill reached it"}

        rv.kill_session_runs = _kill_ended_first
        out = run(t._exec_build({"action": "kill", "project_id": 9239}, 3,
                                _TaskPool(rows[:1]), "s"))
        check("a run that ended first stops nothing and still says the plan is halted",
              out.get("status") == "ok" and out["result"]["stopped"] == []
              and "Auto-advance is halted" in out["result"]["next_step"], out)
    finally:
        rv.kill_session_runs = _sk

    # ── 8. harness_stop unchanged ───────────────────────────────────────────
    print("\n8. harness_stop answers as before")

    async def _stop_502(h):
        return {"harness": h, "ok": False, "held": True, "http": 502,
                "error": "could not reach codex: x"}

    rv._pod_stop = _stop_502
    try:
        run(rv.harness_stop(rv.HarnessStopReq(harness="codex")))
        check("an unreachable pod is still a 502", False, "no exception")
    except HTTPException as he:
        check("an unreachable pod is still a 502",
              he.status_code == 502 and "could not reach" in he.detail, he)
    try:
        run(rv.harness_stop(rv.HarnessStopReq(harness="no-such-engine")))
        check("an unknown harness is still a 400", False, "no exception")
    except HTTPException as he:
        check("an unknown harness is still a 400", he.status_code == 400, he)

    # ── 10. through _run_on_pod, with every network edge stubbed ────────────
    # The shape of scripts/test_pod_conn_retry.py's driver. The kill lands
    # WHILE the stream is open (the stub calls it), which is the only moment a
    # person can press it. What must hold: the result says who stopped it, and
    # neither redo path — the re-dispatch or the buffered /run, both of which
    # run the build again from scratch — ever follows a kill.
    print("\n10. a kill mid-stream, through _run_on_pod")
    import tempfile
    import aiohttp
    rv._pod_stop = _fake_pod_stop

    class _Resp:
        def __init__(self, payload):
            self._payload, self.status = payload, 200

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Http:
        """Only the buffered /run reaches here; `posts` counts it."""

        def __init__(self):
            self.posts = 0

        def post(self, url, **_kw):
            if str(url).rstrip("/").endswith("/run"):
                self.posts += 1
            return _Resp({"ok": True, "exit_code": 0, "transcript": "re-ran", "diff": ""})

    def drive(stream_behaviour, *, kill: bool):
        attempts = {"n": 0}

        async def _noop(*_a, **_k):
            return None

        async def _stream(_http, _base, _payload, _label, session_dir, *,
                          idle_timeout, frames_seen=None, host_rediffs=False):
            attempts["n"] += 1
            if kill:
                await rv.kill_session_runs(pathlib.Path(session_dir).name, by="user")
            return stream_behaviour(frames_seen)

        names = ("_await_routable", "_clone_in", "_stream_harness_run", "_collect_out",
                 "_drop_pod_dir", "_persist_rotated_creds", "_maybe_cache_model_catalog")
        saved = {k: getattr(rv, k) for k in names}
        for k in names:
            setattr(rv, k, _noop)
        rv._stream_harness_run = _stream
        try:
            with tempfile.TemporaryDirectory() as td:
                worktree = pathlib.Path(td) / "codex"
                worktree.mkdir()
                sdir = pathlib.Path(td) / "factory-build-500"
                sdir.mkdir()
                http = _Http()
                out = run(rv._run_on_pod(http, "http://pod:8847", "codex", "codex",
                                         "do a thing", worktree, 15, 0.0, session_dir=sdir))
                return out, attempts["n"], http.posts
        finally:
            for k, v in saved.items():
                setattr(rv, k, v)

    def pod_returns_killed(frames):
        frames["n"] = 12
        frames["response"] = True
        return {"ok": False, "exit_code": -9, "error": "the CLI was killed"}

    def drops_after_headers(frames):
        frames["response"] = True
        raise aiohttp.ServerDisconnectedError()

    def drops_before_answer(frames):
        raise aiohttp.ServerDisconnectedError()

    _reset()
    out, n, posts = drive(pod_returns_killed, kill=True)
    check("the pod's own result is relabelled: stopped by you, never ok",
          out.get("killed_by") == "user" and out.get("ok") is False
          and "stopped by you" in (out.get("error") or ""), out)
    check("the pod's words are kept as error_detail",
          out.get("error_detail") == "the CLI was killed", out)
    check("the run left the registry", rv._live_pod_runs == {}, rv._live_pod_runs)

    _reset()
    out, n, posts = drive(drops_after_headers, kill=True)
    check("a kill then a dropped stream does NOT re-run the build through buffered /run",
          posts == 0, posts)
    check("…and reads as stopped by you", out.get("killed_by") == "user", out)
    check("…and closes the terminal with a killed complete frame",
          any(f[3].get("state") == "complete" and f[3].get("killed_by") == "user" for f in FRAMES),
          FRAMES)

    _reset()
    out, n, posts = drive(drops_before_answer, kill=True)
    check("a kill then a pre-answer drop is NOT re-dispatched", n == 1, n)
    check("…nor re-run buffered", posts == 0, posts)

    _reset()
    out, n, posts = drive(drops_after_headers, kill=False)
    check("control: without a kill the same drop still takes the buffered fallback "
          "(the branch above is the kill's, not a change to the drop path)",
          posts == 1 and not out.get("killed_by"), (posts, out))

    # ── 11. two kills at once ───────────────────────────────────────────────
    # Two presses (the person and the Host, or two windows) on one pod. The
    # second must not send the pod a second /stop: the first already ended the
    # run, and a second stop only takes down work that reached the pod since —
    # unmarked, so it reads as a plain failure.
    print("\n11. two kills at once stop the pod once")
    _reset()
    gate = asyncio.Event()
    LATE: dict = {}

    async def _slow_stop(h):
        STOPS.append(h)
        if len(STOPS) == 1:
            # Work of another session reaches the pod while the first stop runs.
            LATE["e"] = rv._register_live_run(h, S / "fan-late", "codex")
            await gate.wait()
        return {"harness": h, "ok": True, "held": True, "stopped": 1, "could_not_stop": 0}

    rv._pod_stop = _slow_stop
    try:
        rv._register_live_run("codex", S / "factory-build-60", "codex")

        async def _two():
            k1 = asyncio.ensure_future(rv.kill_session_runs("factory-build-60", by="user"))
            await asyncio.sleep(0.01)
            k2 = asyncio.ensure_future(rv.kill_session_runs("factory-build-60", by="host"))
            await asyncio.sleep(0.01)
            gate.set()
            return await k1, await k2
        o1, o2 = run(_two())
        check("the pod got ONE /stop for two kills", STOPS == ["codex"], STOPS)
        check("work that reached the pod after the first stop is not marked killed",
              LATE["e"]["killed_by"] is None, LATE)
        check("the second answer says an earlier kill already stopped it",
              o2["ok"] and o2["stopped"] == [] and "earlier kill" in (o2.get("detail") or ""), o2)
    finally:
        rv._pod_stop = _fake_pod_stop
        rv._live_pod_runs.clear()

    # ── 12. a kill while the run is still waking its pod ────────────────────
    # The run terminal shows Kill from the moment it mounts — through the wake,
    # which on a cold pod is the longest wait of all. A kill there answered
    # "nothing is running" and halted nothing, and the build then ran in full.
    # It must stop the run before it starts, without stopping the pod (nothing
    # of it runs there; other work may).
    print("\n12. a kill during the wake stops the run before it starts")
    _reset()

    async def _drive_wake_kill(wake_ok=True):
        import aiohttp as _aio
        streamed = {"n": 0}
        answers: dict = {}
        sdir = pathlib.Path(tempfile.mkdtemp()) / "factory-build-70"
        sdir.mkdir()
        wt = sdir / "work" / "codex"
        wt.mkdir(parents=True)

        async def _bridge(op, harness_id, **_kw):
            if op == "harness.wake":
                answers["kill"] = await rv.kill_session_runs("factory-build-70", by="user")
                if not wake_ok:
                    return {"ok": False, "error": "operator /harness/wake 402: over plan"}
                return {"ok": True, "url": "http://pod:8840"}
            return {"ok": True}

        async def _stream(*_a, **_k):
            streamed["n"] += 1
            return {"ok": True, "exit_code": 0, "transcript": "ran"}

        async def _noop(*_a, **_k):
            return None

        names = ("_harness_bridge", "_await_routable", "_clone_in", "_stream_harness_run",
                 "_collect_out", "_drop_pod_dir", "_persist_rotated_creds",
                 "_maybe_cache_model_catalog", "_collect_session", "_stop_leftovers",
                 "_harness_pod_creds")
        saved = {k: getattr(rv, k) for k in names}
        for k in names:
            setattr(rv, k, _noop)
        rv._harness_bridge, rv._stream_harness_run = _bridge, _stream
        rv._harness_pod_creds = lambda h: None
        for d in (rv._harness_holders, rv._pod_slots, rv._pod_running, rv._pod_slot_cond,
                  rv._pod_ready, rv._window_shared, rv._woken):
            d.clear()
        try:
            async with _aio.ClientSession() as http:
                res = await rv._dispatch_to_pod_harness(http, "codex", "p", wt, 5, 0.0,
                                                         session_dir=sdir)
        finally:
            for k, v in saved.items():
                setattr(rv, k, v)
        return res, streamed["n"], answers.get("kill") or {}

    res, streamed, ans = run(_drive_wake_kill())
    check("the run never reached the pod's /run/stream", streamed == 0, streamed)
    check("its result says who stopped it, and is not ok",
          res.get("killed_by") == "user" and res.get("ok") is False, res)
    check("the pod was not stopped for a run that was not on it", STOPS == [], STOPS)
    check("the kill's answer names the run it stopped",
          [(s["session_id"], s["other_session"]) for s in ans.get("stopped") or []]
          == [("factory-build-70", False)], ans)
    check("the run left the registry", rv._live_pod_runs == {}, rv._live_pod_runs)
    _reset()
    res, streamed, ans = run(_drive_wake_kill(wake_ok=False))
    check("killed while waking, then the wake failed: it reads as stopped, the wake's "
          "words kept beside it",
          res.get("killed_by") == "user" and "stopped by you" in (res.get("error") or "")
          and "wake failed" in (res.get("error_detail") or ""), res)
    check("...and it left the registry", rv._live_pod_runs == {}, rv._live_pod_runs)

    # ── 12b. a kill while the build prepares its workspace ──────────────────
    # The Host reads the build's row as running from the moment it starts, but
    # the lane first prepares the workspace (a fetch, the seed copy) and only
    # registers its run at the wake. A kill then found nothing, halted
    # nothing, and the build went on to run in full.
    print("\n12b. a kill during workspace prep: the build never starts")
    _reset()
    _bp_open = getattr(rv, "build_prep_open", None)
    check("the lane can mark its prep window (review.build_prep_open)", callable(_bp_open))
    if callable(_bp_open):
        _bp_open("factory-build-71", "codex")
        ans = run(rv.kill_session_runs("factory-build-71", by="host"))
        check("the kill answers that it stopped the build before it started",
              ans["ok"] and [(s["session_id"], s.get("not_started")) for s in ans["stopped"]]
              == [("factory-build-71", True)], ans)
        check("...and stops no pod", STOPS == [], STOPS)
        check("the lane can read it (review.killed_before_start)",
              rv.killed_before_start("factory-build-71") == "host")
        e = rv._register_live_run("codex", S / "factory-build-71", "codex", on_pod=False)
        check("a registration of that build takes the kill as its mark",
              e["killed_by"] == "host", e)
        rv._unregister_live_run("codex", e)
        rv.build_prep_close("factory-build-71")
        ans = run(rv.kill_session_runs("factory-build-71", by="user"))
        check("after the run is over, a kill says nothing is running (not 'stopped before it "
              "started')", ans["stopped"] == [] and "nothing" in (ans.get("detail") or ""), ans)
        _bp_open("factory-build-72", None)
        ans = run(rv.kill_session_runs("factory-build-72", harness="grok", by="host"))
        check("a kill narrowed to an engine the build may not run on leaves it alone",
              ans["stopped"] == [] and rv.killed_before_start("factory-build-72") is None, ans)
        rv.build_prep_close("factory-build-72")

    # ── 12c. the same, through the build lane ───────────────────────────────
    print("\n12c. through _run_phase_build: a build killed during prep does not build")
    _reset()
    from server.routes import project_git as _pg
    from server.routes import projects as _pj
    from mastercode.runtime import pipeline as _pl
    LANE: dict = {"pool_build": 0, "finish": None, "retry": None}

    class _LanePool:
        async def fetchval(self, q, *a):
            return None

        async def fetchrow(self, q, *a):
            return None

        async def fetch(self, q, *a):
            return []

        async def execute(self, q, *a):
            return "UPDATE 1"

    async def _lane_prep(*_a, **_k):
        # The Host's kill lands while the workspace is being prepared.
        await rv.kill_session_runs("factory-build-77", by="host")
        return {}

    async def _lane_pool_build(**_kw):
        LANE["pool_build"] += 1
        return {"status": "ok", "result": {"changed_files": []}}

    async def _lane_finish(_pool, _tid, out):
        LANE["finish"] = out
        return True

    async def _lane_retry(**kw):
        LANE["retry"] = kw
        return None

    async def _anoop(*_a, **_k):
        return None

    async def _alist(*_a, **_k):
        return []

    async def _arepo(*_a, **_k):
        return {"repo": "owner/app"}

    _patches = [(t, "_pool_build", _lane_pool_build), (t, "_factory_build_task_finish", _lane_finish),
                (t, "_phase_retry_pick", _lane_retry), (t, "_set_phase_status", _anoop),
                (t, "_set_phase_steps_status", _anoop), (t, "_ticket_phase_outcome", _anoop),
                (t, "_chain_stop", _anoop), (t, "_explain_idle", _anoop),
                (t, "_autoadvance_pick_next", _alist), (t, "_append_build_activity", _anoop),
                (t, "_get_runtime_dispatch", lambda: None), (_pl, "load_approved", _alist),
                (_pg, "prepare_workspace", _lane_prep), (_pj, "ensure_project_repo", _arepo)]
    _saved_p = [(o, n, getattr(o, n)) for o, n, _f in _patches]
    for o, n, f in _patches:
        setattr(o, n, f)
    try:
        run(t._run_phase_build(entity_id=9003, pool=_LanePool(), session_id=None, task_id=77,
                               project_id=9, phase_id=11, intent="build the API",
                               research_id=None, timeout_s=600, harness="codex", chain_depth=1))
    finally:
        for o, n, f in _saved_p:
            setattr(o, n, f)
    _fin = LANE["finish"] or {}
    check("the build did not run", LANE["pool_build"] == 0, LANE)
    check("its row says who stopped it, before it started",
          _fin.get("killed_by") == "host" and "before it started" in str(_fin.get("result")), _fin)
    check("...and the phase's retry decision is told it was killed",
          (LANE["retry"] or {}).get("killed") is True, LANE["retry"])
    check("the prep window closed with the lane",
          "factory-build-77" not in getattr(rv, "_build_prep", {}), getattr(rv, "_build_prep", None))

    # ── 13. a kill during a re-dispatch ─────────────────────────────────────
    # The pod connection dropped before the pod answered, so the run is being
    # re-dispatched (routable wait + clone-in again). A kill then must keep
    # the redo from starting the build again — and must not stop the pod,
    # which holds nothing of this run during the redo.
    print("\n13. a kill during a re-dispatch")
    _reset()

    async def _drive_redispatch_kill():
        attempts = {"stream": 0, "clone": 0}
        td = pathlib.Path(tempfile.mkdtemp())
        sdir = td / "factory-build-80"
        sdir.mkdir()
        wt = td / "codex"
        wt.mkdir()

        async def _clone(*_a, **_k):
            attempts["clone"] += 1
            if attempts["clone"] == 2:
                # The redo is cloning in when the person presses kill.
                await rv.kill_session_runs("factory-build-80", by="user")

        async def _stream(*_a, frames_seen=None, **_k):
            attempts["stream"] += 1
            if attempts["stream"] == 1:
                raise aiohttp.ServerDisconnectedError()
            return {"ok": True, "exit_code": 0, "transcript": "ran again"}

        async def _noop(*_a, **_k):
            return None

        names = ("_await_routable", "_clone_in", "_stream_harness_run", "_collect_out",
                 "_drop_pod_dir", "_persist_rotated_creds", "_maybe_cache_model_catalog")
        saved = {k: getattr(rv, k) for k in names}
        for k in names:
            setattr(rv, k, _noop)
        rv._clone_in, rv._stream_harness_run = _clone, _stream
        try:
            out = await rv._run_on_pod(_Http(), "http://pod:8847", "codex", "codex", "p",
                                       wt, 15, 0.0, session_dir=sdir)
        finally:
            for k, v in saved.items():
                setattr(rv, k, v)
        return out, attempts

    out, att = run(_drive_redispatch_kill())
    check("the re-dispatch did not start the build again", att["stream"] == 1, att)
    check("the pod was not stopped while it held nothing of the run", STOPS == [], STOPS)
    check("the result says who stopped it",
          out.get("killed_by") == "user" and out.get("ok") is False, out)
    check("the run left the registry", rv._live_pod_runs == {}, rv._live_pod_runs)

    # ── 13b. a stop that fails while the run ends on its own ────────────────
    # The kill marks the run, then waits on the pod's /stop. The run finishes
    # by itself during that wait, and the stop then fails (an image without
    # /stop answers 404). The person is told the kill failed — so the run must
    # not be labelled killed either; its finished work stands.
    print("\n13b. a failed stop never labels a run that ended on its own")
    _reset()

    async def _drive_failed_stop_race():
        td = pathlib.Path(tempfile.mkdtemp())
        sdir = td / "factory-build-85"
        sdir.mkdir()
        wt = td / "codex"
        wt.mkdir()
        released = asyncio.Event()
        answers: dict = {}

        async def _stop_fails_late(h):
            STOPS.append(h)
            await released.wait()
            return {"harness": h, "ok": False, "held": True,
                    "error": "the running codex image has no /stop yet"}

        async def _kill():
            answers["kill"] = await rv.kill_session_runs("factory-build-85", by="user")

        async def _stream(*_a, frames_seen=None, **_k):
            asyncio.ensure_future(_kill())
            await asyncio.sleep(0.01)            # the kill marks the run, its stop is out
            asyncio.get_running_loop().call_later(0.05, released.set)
            if frames_seen is not None:
                frames_seen["n"], frames_seen["response"] = 3, True
            return {"ok": True, "exit_code": 0, "transcript": "done on its own"}

        async def _noop(*_a, **_k):
            return None

        names = ("_await_routable", "_clone_in", "_stream_harness_run", "_collect_out",
                 "_drop_pod_dir", "_persist_rotated_creds", "_maybe_cache_model_catalog",
                 "_pod_stop")
        saved = {k: getattr(rv, k) for k in names}
        for k in names:
            setattr(rv, k, _noop)
        rv._stream_harness_run, rv._pod_stop = _stream, _stop_fails_late
        try:
            out = await rv._run_on_pod(_Http(), "http://pod:8847", "codex", "codex", "p",
                                       wt, 15, 0.0, session_dir=sdir)
            for _ in range(50):
                if "kill" in answers:
                    break
                await asyncio.sleep(0.01)
        finally:
            for k, v in saved.items():
                setattr(rv, k, v)
        return out, answers.get("kill") or {}

    out, ans = run(_drive_failed_stop_race())
    check("the kill reports that its stop failed", ans.get("errors") and not ans.get("ok"), ans)
    check("...and the run that finished on its own is not labelled killed",
          out.get("ok") is True and not out.get("killed_by"), out)

    # ── 14. the kill reaches the build's record ─────────────────────────────
    # _normalize_run carries killed_by, but the build lane rebuilt its result
    # field by field and dropped it, so the factory build's row, the delivery
    # and the retry budget only ever saw "a failure".
    print("\n14. the build lane carries the kill to the record")
    import mastercode.runtime.agent_dispatch as _ad
    _saved_env = {k: os.environ.get(k) for k in ("FACTORY_ENGINE", "CSERIES_SMOKE")}
    _saved_rv = (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
                 rv.resolve_build_target, rv._coding_harness_settings)

    async def _target(h, model=None):
        return "claude-code", None, None

    async def _no_settings():
        return {}

    def _killed_stub(worktree, prompt):
        return {"ok": False, "harness": "stub", "exit_code": None,
                "error": rv._killed_sentence("user"), "killed_by": "user"}

    try:
        _td = pathlib.Path(tempfile.mkdtemp())
        rv.PROJECTS_ROOT, rv.SESSIONS_ROOT = _td / "projects", _td / "sessions"
        (rv.PROJECTS_ROOT / "31").mkdir(parents=True)
        os.environ["FACTORY_ENGINE"], os.environ["CSERIES_SMOKE"] = "stub", "1"
        rv._stub_harness_build = _killed_stub
        rv.resolve_build_target, rv._coding_harness_settings = _target, _no_settings
        rsb = run(rv.run_single_harness_build(project_id=31, prompt="p", harness="claude-code",
                                              billing_key="factory-build-90"))
        check("run_single_harness_build's result keeps killed_by",
              rsb.get("killed_by") == "user", rsb)

        _saved_rsb = rv.run_single_harness_build

        async def _rsb_killed(**_kw):
            return dict(rsb, killed_by="user")

        class _PbPool:
            async def fetchval(self, q, *a):
                return 1   # the project has a plan

        rv.run_single_harness_build = _rsb_killed
        try:
            env = run(t._pool_build(entity_id=9003, pool=_PbPool(), project_id=31, prompt="p",
                                    research_id=None, timeout_s=60))
        finally:
            rv.run_single_harness_build = _saved_rsb
        check("_pool_build's failure envelope keeps killed_by",
              env.get("status") == "error" and env.get("killed_by") == "user", env)

        class _FinishPool:
            def __init__(self):
                self.sql: list = []

            async def fetchrow(self, q, *a):
                return None

            async def execute(self, q, *a):
                self.sql.append((q, a))
                return "UPDATE 1"

        fp = _FinishPool()
        run(t._factory_build_task_finish(fp, 90, dict(env, killed_by="user")))
        _failed_writes = [a for q, a in fp.sql if "status='failed'" in q]
        check("the failed row is stamped killed_by",
              bool(_failed_writes) and '"killed_by": "user"' in _failed_writes[0][2], fp.sql)

        _row = {"task_id": 90, "entity_id": 9003, "title": "t", "description": "d",
                "task_type": "factory_build", "status": "failed", "error": "e"}
        rec = _ad.delivery_record(_row, {"killed_by": "user", "project_id": 31})
        check("delivery_record carries killed_by to the delivery",
              rec.get("killed_by") == "user", rec)
    finally:
        (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
         rv.resolve_build_target, rv._coding_harness_settings) = _saved_rv
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── 15. a kill spends no retry and starts none ─────────────────────────
    print("\n15. a killed build is not a failure of the work")
    KILLED_ERR = rv._killed_sentence("user") + " — the failed attempt left no recoverable files"
    check("a killed row counts as stopped, not as the work failing",
          t._failed_build_kind({"error": KILLED_ERR, "killed": True}) == "stopped",
          t._failed_build_kind({"error": KILLED_ERR, "killed": True}))

    class _RetryPool:
        """A chained phase (chain_depth 1); the project is not halted."""

        def __init__(self, rows):
            self.rows, self.notes, self.sql = rows, [], []

        async def fetchval(self, q, *a):
            return False

        async def fetchrow(self, q, *a):
            return {"id": 7, "phase_number": 2, "title": "Backend", "spec": "brief",
                    "description": None, "status": "failed", "gate": "none",
                    "approved_at": None, "acceptance": None}

        async def fetch(self, q, *a):
            self.sql.append(q)
            # The killed column exists only where the query asks for it.
            return [dict({"error": err, "acceptance_refused": False, "links_refused": False,
                          "integration_conflict": False},
                         **({"killed": killed} if "AS killed" in q else {}))
                    for err, killed in self.rows]

        async def execute(self, q, *a):
            self.notes.append(" ".join(str(x) for x in a))
            return "UPDATE 1"

    rp = _RetryPool([(KILLED_ERR, True), ("error: tests failed", False)])
    got = run(t._phase_retry_pick(pool=rp, entity_id=1, project_id=9, phase_id=7,
                                  failure="error: tests failed", chain_depth=1,
                                  finished_task_id=2))
    check("a kill, then the work's first failure: the seeded retry still fires "
          "(the kill spent nothing)",
          isinstance(got, tuple) and not any("FAILED 2 times" in n for n in rp.notes),
          (got, rp.notes))
    rp2 = _RetryPool([(KILLED_ERR, True)])
    try:
        got = run(t._phase_retry_pick(pool=rp2, entity_id=1, project_id=9, phase_id=7,
                                      failure=KILLED_ERR, chain_depth=1, finished_task_id=2,
                                      killed=True))
    except TypeError as te:
        got = ("no killed parameter", str(te))
    check("the killed build itself is never retried, even in a project that is not halted, "
          "and no chain-stop note is written",
          got is None and rp2.notes == [], (got, rp2.notes))

    # ── 16. the delivery never hands a kill to the Host as a failure ────────
    print("\n16. the delivery of a killed run")
    import mastercode.runtime.session as _S
    _kill_of = getattr(_S, "kill_of", None)
    check("session.kill_of exists", callable(_kill_of))
    if callable(_kill_of):
        check("a killed build is a kill",
              _kill_of({"task_type": "factory_build", "killed_by": "host"}) == "host")
        check("a compare whose every variant was killed is a kill",
              _kill_of({"task_type": "fan_out", "per_harness": {
                  "codex": {"state": "failed", "killed_by": "user"},
                  "grok": {"state": "failed", "killed_by": "user"}}}) == "user")
        check("a compare with nothing landed, one variant killed and one failed on its own, "
              "is a kill (the failure brief would re-run the killed variant too)",
              _kill_of({"task_type": "fan_out", "per_harness": {
                  "codex": {"state": "failed", "killed_by": "user"},
                  "grok": {"state": "failed", "error": "boom"}}}) == "user")
        check("a compare where a version landed is not (the user picks, as always)",
              _kill_of({"task_type": "fan_out", "per_harness": {
                  "codex": {"state": "failed", "killed_by": "user"},
                  "grok": {"state": "done"}}}) is None)
        check("a compare nobody killed is not",
              _kill_of({"task_type": "fan_out", "per_harness": {
                  "grok": {"state": "failed", "error": "boom"}}}) is None)
        check("an ordinary failed build is not", _kill_of({"task_type": "factory_build"}) is None)
    _ok, _sum, _per, _done, _failed = t._summarize_fan_out(
        {"session_id": "fan-1", "harnesses": ["codex"]},
        {"codex": {"state": "failed", "error": rv._killed_sentence("user"), "killed_by": "user"}})
    check("a compare's summary keeps who killed each variant",
          _per["codex"].get("killed_by") == "user", _per)

    # ── 9. the schema ───────────────────────────────────────────────────────
    print("\n9. the build tool offers kill")
    params = ptr._BUILD_SCHEMAS["build"]["function"]["parameters"]
    check("kill is in the action enum", "kill" in params["properties"]["action"]["enum"], params)
    check("kill needs no new parameter: project_id (already required) and harness",
          "task_id" not in params["properties"] and "harness" in params["properties"]
          and "project_id" in params["required"], list(params["properties"]))

    # ── 10. a run stopped before it was sent records no sent prompt ─────────
    # manifest `sent_prompt` is what the run view shows as what the host sent
    # the harness; a run killed while its pod woke never got one.
    print("\n10. a run stopped before it was sent")
    import json as _json
    import tempfile as _tf

    async def _noop(*_a, **_k):
        return None
    _saved_pod = (rv._await_routable, rv._clone_in, rv._harness_bridge)
    rv._await_routable = rv._clone_in = rv._harness_bridge = _noop
    try:
        with _tf.TemporaryDirectory() as _td:
            _sd = pathlib.Path(_td) / "sess-killed"
            _sd.mkdir()
            (_sd / "manifest.json").write_text(_json.dumps({"prompt": "task"}))
            _wt = pathlib.Path(_td) / "wt"
            _wt.mkdir()
            _out = run(rv._run_on_pod(
                None, "http://127.0.0.1:9", "fakeh", "v1", "FULL-PROMPT", _wt, 5, 0.0,
                session_dir=_sd, live={"killed_by": "user", "on_pod": False, "sent": False}))
            _m = _json.loads((_sd / "manifest.json").read_text())
            check("the run is stopped before it started",
                  _out.get("ok") is False and _out.get("killed_by") == "user", _out)
            check("...and the manifest records no prompt as sent to it",
                  "v1" not in (_m.get("sent_prompt") or {}), _m)
    finally:
        rv._await_routable, rv._clone_in, rv._harness_bridge = _saved_pod
finally:
    rv._pod_stop, rv._append_run_frame = _saved

print(f"\n{'PASS' if not FAILS else 'FAIL'} — {len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
