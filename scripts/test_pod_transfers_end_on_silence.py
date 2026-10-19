#!/usr/bin/env python3
"""
Transfers to and from a harness pod end on silence, never on a wall clock, and
say why; the session and scratch exports are kept up to the worktree's bound
and a failure to keep them is said.

Collect-out used ClientTimeout(total=120): a worktree still arriving
after 120 s was cut off, and the run read "could not be collected ...:
TimeoutError" (str(TimeoutError()) is empty). The body already goes to disk,
so the total protected nothing.
  1. collect-out has no total; a read waits for the idle bound
  2. a body that keeps arriving (gaps under the idle bound) is collected
  3. a body that stalls past the idle bound ends, in a sentence naming
     collect-out and the silence

Clone-in used ClientTimeout(total=120), built the whole tar in Host
memory, and its bare TimeoutError reached the user as "harness run failed".
  4. the upload is sent from a file with its Content-Length (the sidecar
     reads by length), .git left out, and arrives whole
  5. a pod that stops taking bytes ends the upload, in a sentence naming
     clone-in and the silence
  6. a pod that takes everything and then never answers ends it, naming the
     wait for the answer
  7. building the upload stops, and says so, before it would leave the
     Host's disk under its floor
  8. _run_on_pod's catch-all never records an empty reason

The scratch (fallback) and native-session exports were refused past
64 MiB, and a failed session collect was a log line only.
  9. a 70 MiB session export is kept
 10. a 70 MiB fallback export is taken
 11. the scratch and session exports have no total either
 12. a failed session collect is said in the run terminal, and the build
     and compare paths record it on the result

Localhost aiohttp servers and temp dirs; no pod, no DB. Run:
APP_ROOT=<repo> python3 scripts/test_pod_transfers_end_on_silence.py
"""

import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[pod-transfer] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _tar_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _tar_file_of(path: Path, name: str, size: int) -> Path:
    """A tar holding one `size`-byte file, written to disk (not memory)."""
    with tarfile.open(str(path), mode="w") as tf:
        ti = tarfile.TarInfo(name)
        ti.size = size

        class _Zeros(io.RawIOBase):
            left = size

            def readable(self):
                return True

            def readinto(self, b):
                n = min(len(b), self.left)
                b[:n] = bytes(n)
                self.left -= n
                return n
        tf.addfile(ti, io.BufferedReader(_Zeros(), 1 << 20))
    return path


class _Capture:
    """A ClientSession stand-in that records each call's timeout and then
    refuses it, so the call site's own arguments can be read."""

    def __init__(self):
        self.timeouts = []

    def _record(self, *_a, **k):
        self.timeouts.append(k.get("timeout"))
        raise _Stop()

    get = post = _record


class _Stop(Exception):
    pass


async def _serve(handlers):
    from aiohttp import web
    app = web.Application(client_max_size=1 << 30)
    for method, path, fn in handlers:
        app.router.add_route(method, path, fn)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))

    import aiohttp
    from aiohttp import web
    import server.routes.review as rv

    idle = 0.4
    if hasattr(rv, "_POD_TRANSFER_IDLE_S"):
        rv._POD_TRANSFER_IDLE_S = idle
        rv._POD_EXTRACT_S_PER_MIB = 0.0

    def no_total(t):
        return t is not None and t.total is None and bool(t.sock_read)

    async def run_all(td: Path):
        # ── collect-out ────────────────────────────────────────────────
        cap = _Capture()
        wt0 = td / "wt0"
        (wt0 / ".git").mkdir(parents=True)
        try:
            await rv._collect_out(cap, "http://pod", "d", wt0)
        except _Stop:
            pass
        check("1. collect-out has no total; a read waits for the idle bound",
              len(cap.timeouts) == 1 and no_total(cap.timeouts[0]),
              f"timeout={cap.timeouts[:1]}")

        body = _tar_of({"hello.txt": b"hello from the pod\n" * 4000})
        mode = {"collect": "trickle"}

        async def export(request):
            resp = web.StreamResponse()
            resp.content_length = len(body)
            await resp.prepare(request)
            pieces = [body[i:i + 8192] for i in range(0, len(body), 8192)]
            try:
                for i, p in enumerate(pieces):
                    await resp.write(p)
                    if mode["collect"] == "trickle":
                        await asyncio.sleep(idle / 4)
                    elif mode["collect"] == "stall" and i == 1:
                        await asyncio.sleep(idle * 4)
                await resp.write_eof()
            except ConnectionError:
                pass                 # the client stopped the transfer: the point of 3
            return resp

        got = {}
        load_mode = {"m": "ok"}

        async def load(request):
            got["headers"] = dict(request.headers)
            if load_mode["m"] == "stop_reading":
                await request.content.read(1 << 16)
                await asyncio.sleep(idle * 8)
                return web.json_response({"ok": True})
            data = await request.read()
            got["body"] = data
            if load_mode["m"] == "no_answer":
                await asyncio.sleep(idle * 5)
            return web.json_response({"ok": True})

        big = {"size": 0}

        async def session_export(request):
            p = _tar_file_of(td / "sess-src.tar", "projects/run.jsonl", big["size"])
            return web.FileResponse(p)

        async def scratch_export(request):
            if request.query.get("dir") != "scratch":
                return await export(request)
            p = _tar_file_of(td / "scr-src.tar", "big.bin", big["size"])
            return web.FileResponse(p)

        runner, base = await _serve([
            ("GET", "/workspace/export", scratch_export),
            ("POST", "/workspace/load", load),
            ("GET", "/session/export", session_export),
        ])
        try:
            wt = td / "wt"
            (wt / ".git").mkdir(parents=True)
            t0 = time.monotonic()
            try:
                async with aiohttp.ClientSession() as http:
                    await asyncio.wait_for(rv._collect_out(http, base, "d", wt), 30)
                ok2 = (wt / "hello.txt").is_file()
                err2 = ""
            except BaseException as e:  # noqa: BLE001
                ok2, err2 = False, repr(e)
            check("2. a body that keeps arriving is collected (gaps under the idle bound)",
                  ok2 and time.monotonic() - t0 > idle, f"err={err2}")

            mode["collect"] = "stall"
            wt2 = td / "wt2"
            (wt2 / ".git").mkdir(parents=True)
            msg3 = ""
            try:
                async with aiohttp.ClientSession() as http:
                    await asyncio.wait_for(rv._collect_out(http, base, "d", wt2), 30)
            except BaseException as e:  # noqa: BLE001
                msg3 = str(e)
            check("3. a stalled collect-out ends in a sentence naming collect-out and the silence",
                  "collect-out" in msg3 and "no bytes" in msg3 and f"{idle:g} s" in msg3,
                  f"msg={msg3!r}")

            # ── clone-in ───────────────────────────────────────────────
            src = td / "src"
            (src / ".git").mkdir(parents=True)
            (src / ".git" / "config").write_text("[core]\n")
            (src / "app.py").write_text("print('hi')\n")
            (src / "data.bin").write_bytes(os.urandom(1 << 20))
            load_mode["m"] = "ok"
            err4 = ""
            try:
                async with aiohttp.ClientSession() as http:
                    await asyncio.wait_for(rv._clone_in(http, base, src, "d"), 30)
            except BaseException as e:  # noqa: BLE001
                err4 = repr(e)
            names = []
            if got.get("body"):
                with tarfile.open(fileobj=io.BytesIO(got["body"]), mode="r:") as tf:
                    names = tf.getnames()
            hdr = {k.lower(): v for k, v in (got.get("headers") or {}).items()}
            check("4. clone-in arrives whole, with its Content-Length, no chunking, .git left out",
                  not err4 and "./app.py" in names and "./data.bin" in names
                  and not any(".git" in n for n in names)
                  and hdr.get("content-length") == str(len(got.get("body") or b""))
                  and "transfer-encoding" not in hdr,
                  f"err={err4} names={names[:6]} hdr_len={hdr.get('content-length')}")

            bigsrc = td / "bigsrc"
            (bigsrc / ".git").mkdir(parents=True)
            with open(bigsrc / "blob.bin", "wb") as fh:
                for _ in range(48):
                    fh.write(os.urandom(1 << 20))
            load_mode["m"] = "stop_reading"
            msg5 = ""
            t0 = time.monotonic()
            try:
                async with aiohttp.ClientSession() as http:
                    await asyncio.wait_for(rv._clone_in(http, base, bigsrc, "d"), idle * 6)
            except BaseException as e:  # noqa: BLE001
                msg5 = str(e) or repr(e)
            check("5. a pod that stops taking bytes ends clone-in, naming clone-in and the silence",
                  "clone-in" in msg5 and "took no bytes" in msg5, f"msg={msg5!r}")

            load_mode["m"] = "no_answer"
            msg6 = ""
            try:
                async with aiohttp.ClientSession() as http:
                    await asyncio.wait_for(rv._clone_in(http, base, src, "d"), 30)
            except BaseException as e:  # noqa: BLE001
                msg6 = str(e) or repr(e)
            check("6. a pod that never answers after the upload ends clone-in, naming the wait",
                  "clone-in" in msg6 and "answer" in msg6, f"msg={msg6!r}")

            saved_free = rv._hin.free_bytes
            rv._hin.free_bytes = lambda _p: rv._hin.disk_floor(_p) + (256 << 10)
            msg7 = ""
            left = []
            try:
                async with aiohttp.ClientSession() as http:
                    await rv._clone_in(http, base, src, "d")
            except BaseException as e:  # noqa: BLE001
                msg7 = str(e) or repr(e)
            finally:
                rv._hin.free_bytes = saved_free
                left = list(Path(tempfile.gettempdir()).glob("cseries-clone-in-*"))
            check("7. building the upload stops before the Host's disk floor, and says so",
                  "clone-in" in msg7 and "free on the Host's disk" in msg7 and not left,
                  f"msg={msg7!r} left={left}")

            # ── session and scratch exports ────────────────────────────
            big["size"] = 70 << 20
            sdir = td / "session"
            sdir.mkdir()
            msg9, kept = "", False
            try:
                async with aiohttp.ClientSession() as http:
                    kept = await asyncio.wait_for(
                        rv._collect_session(http, base, sdir, "claude-code"), 60)
            except BaseException as e:  # noqa: BLE001
                msg9 = str(e) or repr(e)
            check("9. a 70 MiB session export is kept",
                  kept is True and (sdir / "native-sessions" / "claude-code.tar").is_file(),
                  f"err={msg9!r}")

            msg10, path10 = "", None
            try:
                async with aiohttp.ClientSession() as http:
                    path10 = await asyncio.wait_for(
                        rv._export_fallback_dir(http, base, "scratch"), 60)
            except BaseException as e:  # noqa: BLE001
                msg10 = str(e) or repr(e)
            check("10. a 70 MiB fallback (scratch) export is taken",
                  path10 is not None and Path(path10).stat().st_size > (70 << 20),
                  f"err={msg10!r}")
            if path10:
                Path(path10).unlink(missing_ok=True)
        finally:
            await runner.cleanup()

        cap = _Capture()
        for call in (rv._collect_session(cap, "http://pod", sdir, "x"),
                     rv._export_fallback_dir(cap, "http://pod", "scratch")):
            try:
                await call
            except _Stop:
                pass
        check("11. the session and scratch exports have no total either",
              len(cap.timeouts) == 2 and all(no_total(t) for t in cap.timeouts),
              f"timeouts={cap.timeouts}")

    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run_all(Path(td)))

        # 8. the catch-all's reason
        async def _noop(*_a, **_k):
            return None

        async def _clone(*_a, **_k):
            raise asyncio.TimeoutError()
        saved = {k: getattr(rv, k) for k in (
            "_await_routable", "_clone_in", "_persist_rotated_creds",
            "_maybe_cache_model_catalog")}
        rv._await_routable = _noop
        rv._clone_in = _clone
        rv._persist_rotated_creds = _noop
        rv._maybe_cache_model_catalog = _noop
        try:
            w = Path(td) / "kimi"
            w.mkdir()
            s = Path(td) / "sess8"
            s.mkdir()
            out = asyncio.run(rv._run_on_pod(None, "http://pod:8847", "kimi", "kimi",
                                             "do a thing", w, 15, 0.0, session_dir=s))
        finally:
            for k, v in saved.items():
                setattr(rv, k, v)
        check("8. a reason-less exception is never recorded as an empty error",
              out.get("ok") is False and "TimeoutError" in str(out.get("error")),
              f"error={out.get('error')!r}")

        # 12. a failed session collect is said
        s12 = Path(td) / "sess12"
        s12.mkdir()
        say = getattr(rv, "_session_not_kept", None)
        why = say(s12, ["kimi", "kimi-s2"], RuntimeError("session export refused: body passed "
                                                         "the 536870912-byte cap")) if say else ""
        frames = []
        for label in ("kimi", "kimi-s2"):
            p = rv._events_path(s12, label)
            frames += [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        check("12a. a failed session collect is a status frame in every run's terminal",
              len(frames) == 2 and all(f["kind"] == "status"
                                       and f["payload"].get("state") == "session_not_kept"
                                       and "536870912-byte cap" in f["payload"].get("detail", "")
                                       for f in frames)
              and "536870912" in why, f"frames={frames}")
    src_text = Path(app_root, "server/routes/review.py").read_text()
    build = src_text.split("async def _dispatch_to_pod_harness(")[1].split("\nasync def ")[0]
    group = src_text.split("async def _harness_group(")[1].split("\n    async def ")[0] \
        if "async def _harness_group(" in src_text else ""
    check("12b. the build path records it on the result and in the terminal",
          'result["session_export_error"] = _session_not_kept(' in build, "")
    check("12c. the compare path does too, for every variant on the pod",
          '_session_not_kept(session_dir, [v["vid"] for v in group], exc)' in src_text
          and 'r["session_export_error"] = why' in src_text, f"group_found={bool(group)}")

    if FAILS:
        print(f"[pod-transfer] {len(FAILS)} FAILED")
        return 1
    print("[pod-transfer] all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
