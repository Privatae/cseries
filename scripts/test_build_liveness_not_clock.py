#!/usr/bin/env python3
"""The kernel judges a build by liveness, never by its clock.

  OVER    _session_is_over called a session over at started_at + max_minutes ×
        variants + checks + 900 s, from when max_minutes was a total cap. At
        the default of 15 a working build was shown FAILED at 30 minutes, the
        status stream closed and compare() unlocked while it ran (kimi's p95
        run is 39 minutes). Now: held by this process -> live; otherwise over
        only after the session has written nothing for longer than any live
        run can stay silent.
  WAIT    The buffered /run fallback waited max_minutes*60+30 in total. Now it
        waits for as long as the pod answers its health probe, and says so
        when the pod stops answering.
  BUDGET  The default build budget was 900 s / 15 min while the Host was told
        there is no total cap; it is now the model-wait backstop (3600 s),
        matching the pod, and the 4 h maximum is named once.
  DEAD    The uncalled live detector (_run_watchdog.progress_verdict) and its
        stall/flail kill constants are removed, so they cannot be wired in.

Run: APP_ROOT=<repo> python3 scripts/test_build_liveness_not_clock.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[build-liveness] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


async def _bg_forever():
    await asyncio.sleep(3600)


def main() -> int:
    repo = Path(os.environ.get("APP_ROOT", "/app"))
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "brain"))
    os.environ.setdefault("CSERIES_SESSIONS_ROOT", tempfile.mkdtemp())
    import server.routes.review as review
    # One import for both local stub servers below:
    # each section may run without the other.
    from aiohttp import web

    tmp = Path(tempfile.mkdtemp(prefix="sess-live-"))

    def _over(m, sd=None):
        try:
            return review._session_is_over(m, sd)
        except TypeError:          # a tree whose rule takes the manifest only
            return review._session_is_over(m)
    long_ago = time.time() - 5 * 3600

    print("== a session is over by silence, never by elapsed time ==")

    async def _held_case():
        sid = "held-1"
        task = asyncio.ensure_future(_bg_forever())
        review._SESSION_TASKS[sid] = task
        try:
            m = {"session_id": sid, "started_at": long_ago, "max_minutes": 15}
            return _over(m, tmp / "absent")
        finally:
            review._SESSION_TASKS.pop(sid, None)
            task.cancel()
    over = asyncio.run(_held_case())
    check("a session this process is still running is live 5 h in", over is False,
          f"over={over}")

    sd = tmp / "writing"
    sd.mkdir()
    (sd / "kimi.events.jsonl").write_text('{"kind":"status"}\n')
    m = {"session_id": "writing", "started_at": long_ago, "max_minutes": 15}
    over = _over(m, sd)
    check("a session not held here but written to 0 s ago is live 5 h in",
          over is False, f"over={over}")

    sd2 = tmp / "silent"
    sd2.mkdir()
    (sd2 / "kimi.events.jsonl").write_text('{"kind":"status"}\n')
    old = time.time() - 3 * 3600
    os.utime(sd2 / "kimi.events.jsonl", (old, old))
    os.utime(sd2, (old, old))
    m = {"session_id": "silent", "started_at": long_ago, "max_minutes": 15}
    check("a session silent past every live window is over (restart safety net)",
          _over(m, sd2) is True)
    check("a stamped session is over", review._session_is_over({"completed_at": 1.0}) is True)

    print("== the buffered /run waits on the pod's liveness, not a total ==")
    has_helper = hasattr(review, "_buffered_run_watched")
    check("the buffered fallback has a liveness wait (_buffered_run_watched)", has_helper)
    src = (repo / "server/routes/review.py").read_text()
    check("no total timeout of max_minutes*60+30 on the buffered /run",
          "ClientTimeout(total=max_minutes * 60 + 30)" not in src)

    if has_helper:
        async def _serve(run_after_s: float, health_ok: bool):
            async def _run(request):
                await asyncio.sleep(run_after_s)
                return web.json_response({"ok": True, "exit_code": 0,
                                          "transcript": "done", "diff": ""})

            async def _health(request):
                if not health_ok:
                    await asyncio.sleep(3600)
                return web.json_response({"ok": True})
            app = web.Application()
            app.router.add_post("/run", _run)
            app.router.add_get("/health", _health)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            return runner, f"http://127.0.0.1:{port}"

        async def _case(run_after_s, health_ok, idle_s):
            import aiohttp
            review._BUFFERED_PROBE_S = 0.3
            saved = review._STREAM_IDLE_TIMEOUT_S
            review._STREAM_IDLE_TIMEOUT_S = idle_s
            runner, base = await _serve(run_after_s, health_ok)
            try:
                async with aiohttp.ClientSession() as http:
                    t0 = time.monotonic()
                    try:
                        out = await review._buffered_run_watched(
                            http, base, {"prompt": "x", "max_minutes": 0.001},
                            "kimi", None)
                        return out, None, time.monotonic() - t0
                    except Exception as e:  # noqa: BLE001
                        return None, e, time.monotonic() - t0
            finally:
                review._STREAM_IDLE_TIMEOUT_S = saved
                await runner.cleanup()

        out, err, el = asyncio.run(_case(2.5, True, 1.0))
        res = out[0] if isinstance(out, tuple) else out
        check("a buffered run answering after 2.5 s (pod alive, silence bound 1 s, "
              "max_minutes ~0) returns its result", err is None and isinstance(res, dict)
              and res.get("ok") is True, f"err={err!r} out={out!r} el={el:.1f}")
        out, err, el = asyncio.run(_case(30, False, 1.0))
        check("a pod that stops answering ends the wait, and the error says so",
              err is not None and "stopped answering" in str(err) and el < 10,
              f"err={err!r} el={el:.1f}")

    print("== HR-9/HR-14: the pod is held awake while acceptance checks run ==")
    import inspect as _insp
    takes_hid = "harness_id" in _insp.signature(review._run_acceptance).parameters
    check("_run_acceptance holds the pod's lease (takes the harness id)", takes_hid)
    if takes_hid:
        import server.applications.harness_backend as hb
        wakes = []

        async def _fake_bridge(op, *a, **k):
            wakes.append((op, a))
            return {}

        async def _acc_case():
            import aiohttp

            async def _check(request):
                await asyncio.sleep(1.2)
                return web.json_response({"ok": True, "results": [
                    {"cmd": "true", "exit": 0, "ok": True, "timed_out": False}]})
            app = web.Application()
            app.router.add_post("/check", _check)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            saved_b, saved_hb = review._harness_bridge, hb.HARNESS_LEASE_HEARTBEAT_S
            review._harness_bridge, hb.HARNESS_LEASE_HEARTBEAT_S = _fake_bridge, 0.3
            try:
                async with aiohttp.ClientSession() as http:
                    return await review._run_acceptance(
                        http, f"http://127.0.0.1:{port}", "/w", [{"cmd": "true"}],
                        harness_id="kimi")
            finally:
                review._harness_bridge, hb.HARNESS_LEASE_HEARTBEAT_S = saved_b, saved_hb
                await runner.cleanup()
        asyncio.run(_acc_case())
        check("a 1.2 s check re-stamps the pod's wake while it runs",
              sum(1 for op, a in wakes if op == "harness.wake" and a == ("kimi",)) >= 2,
              repr(wakes))

    print("== no dead live-kill thresholds left to wire in ==")
    import server.routes._run_watchdog as wd
    dead = [n for n in ("progress_verdict", "STALL_KILL_S", "STALL_PROBE_S",
                        "FLAIL_PROBES", "PROBE_INTERVAL_S") if hasattr(wd, n)]
    check("the uncalled live detector and its stall/flail constants are gone",
          not dead, f"still present: {dead}")
    check("...and the post-run loop classifier is untouched",
          wd.analyze_transcript("")["parsed"] is False and wd.LOOP_KILL == 5)

    print("== a failed rung is labelled by what stopped it ==")

    def _label(err, elapsed=3500.0):
        row = {"state": "failed", "error": err, "elapsed_seconds": elapsed}
        try:
            return wd.classify_failure(row, None)
        except Exception as e:  # noqa: BLE001 — an old signature is a failure here
            return f"raised {type(e).__name__}"
    check("a long run that failed for its own reason is 'error', not 'stalled-at-cap'",
          _label("harness exited 1: tests failed") == "error",
          _label("harness exited 1: tests failed"))
    check("the progress backstop is labelled no-progress",
          _label("kimi made no progress for 120 minutes …") == "no-progress",
          _label("kimi made no progress for 120 minutes …"))
    check("a liveness stop is labelled stuck, not error",
          _label("codex showed no sign of life for 10 minutes …") == "stuck",
          _label("codex showed no sign of life for 10 minutes …"))
    check("a model call that never answered is labelled model-hung",
          _label("kimi waited on the model for 60 minutes …") == "model-hung",
          _label("kimi waited on the model for 60 minutes …"))
    lsrc = (repo / "server/routes/ladder.py").read_text()
    check("the ladder waits for a rung to end, not max_minutes*60+180 s",
          "mm * 60 + 180" not in lsrc and "while time.time() < deadline" not in lsrc)

    print("== the build's default is the model-wait backstop ==")
    check("FanOutReq's max_minutes default is 60, not 15",
          review.FanOutReq(prompt="x").max_minutes == 60,
          str(review.FanOutReq(prompt="x").max_minutes))
    import inspect
    d = inspect.signature(review.run_single_harness_build).parameters["max_minutes"].default
    check("run_single_harness_build's default is 60, not 15", d == 60, str(d))
    tsrc = (repo / "brain/mastercode/runtime/tools.py").read_text()
    check("tools.py names the build's model-wait default (3600 s) and maximum (4 h)",
          "BUILD_MODEL_WAIT_S = 3600" in tsrc and "BUILD_MODEL_WAIT_MAX_S = 14400" in tsrc)
    check("no build default of 900 s / 15 min remains in the build doors",
          'a.get("timeout") or 900' not in tsrc
          and 'args.get("max_minutes") or 15)' not in tsrc
          and 'a.get("max_minutes") or 15)' not in tsrc)

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
