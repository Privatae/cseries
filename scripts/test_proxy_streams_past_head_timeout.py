#!/usr/bin/env python3
"""Gate: the app proxy bounds the wait for an app's HEAD, never its body.

Both proxy lanes (proxy._serve_external, app pods / helper / external sidecars,
and proxy._serve_process, loopback sidecars) used
aiohttp.ClientTimeout(total=60). aiohttp keeps `total` running while the body
streams, so any download, SSE stream or long-poll from the user's own app was
cut at 60 s with a bare TimeoutError mid-body and no error to anyone.

What this pins, against a REAL aiohttp upstream on loopback (timeouts scaled
down 100x so the gate runs in seconds — the scaling applies to whatever
total= the code asks for, so the same gate run against the old code cuts):
  1. a body that streams for longer than the head wait arrives WHOLE, on
     both lanes;
  2. an app that does not start answering within the head wait is still a
     504 that says so (the replay-safety split in
     scripts/test_proxy_timeout_is_502.py is unchanged).

  3. a stream silent for longer than the head wait arrives whole, and
     the upstream socket carries TCP keepalive (60 s idle, 15 s x 8 probes),
     which is how a DEAD upstream ends a stream that has no clock.

No pod, no DB. Run: APP_ROOT=<repo> python3 scripts/test_proxy_streams_past_head_timeout.py
"""
import asyncio
import os
import socket
import sys
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

FAILED: list[str] = []
SCALE = 100.0          # every timeout the code asks for, divided by this


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[proxy-stream] {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


class _Req:
    def __init__(self, method: str = "GET") -> None:
        self.method = method
        self.headers = {}
        self.url = type("U", (), {"query": ""})()

    async def body(self) -> bytes:
        return b""


async def main() -> None:
    import aiohttp
    from aiohttp import web
    from fastapi import HTTPException

    from server.applications import proxy as P

    CHUNKS, GAP = 6, 0.25            # 1.5 s of body; the scaled head wait is 0.6 s

    async def slow_body(_request):
        resp = web.StreamResponse()
        await resp.prepare(_request)
        for i in range(CHUNKS):
            await resp.write(f"chunk-{i};".encode())
            await asyncio.sleep(GAP)
        await resp.write_eof()
        return resp

    async def slow_head(_request):
        await asyncio.sleep(1.5)
        return web.Response(text="late")

    async def quiet_body(_request):
        # alive but silent for longer than the head wait, twice
        resp = web.StreamResponse()
        await resp.prepare(_request)
        for i in range(3):
            await resp.write(f"tick-{i};".encode())
            await asyncio.sleep(1.2)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/stream", slow_body)
    app.router.add_get("/late", slow_head)
    app.router.add_get("/quiet", quiet_body)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    real_timeout = aiohttp.ClientTimeout

    def scaled(total=None, **kw):
        return real_timeout(total=(total / SCALE if total else total), **kw)

    aiohttp.ClientTimeout = scaled
    if hasattr(P, "_HEAD_TIMEOUT_S"):
        P._HEAD_TIMEOUT_S = 60.0 / SCALE

    class _Sidecar:
        base_url = f"http://127.0.0.1:{port}"

        def is_alive(self):
            return True

    class _Sup:
        def get(self, _app_id):
            return _Sidecar()

    real_sup = P.get_supervisor
    P.get_supervisor = lambda: _Sup()

    async def drain(resp) -> bytes:
        out = b""
        try:
            async for chunk in resp.body_iterator:
                out += chunk if isinstance(chunk, bytes) else chunk.encode()
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            out += f"<<cut: {type(e).__name__}>>".encode()
        return out

    # Every upstream dial goes through the keepalive socket factory;
    # record the sockets it makes to read their options.
    made: list = []
    real_factory = getattr(P, "_keepalive_socket", None)
    if real_factory is not None:
        def _spy(ai):
            sock = real_factory(ai)
            made.append((sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE),
                         sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE),
                         sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
                         sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)))
            return sock
        P._keepalive_socket = _spy

    want = "".join(f"chunk-{i};" for i in range(CHUNKS)).encode()
    lanes = {
        "_serve_process": lambda path: P._serve_process("t", path, _Req()),
        "_serve_external": lambda path: P._serve_external(
            "t", port, path, _Req(), strip_prefix=True, upstream_host=f"127.0.0.1:{port}"),
    }
    try:
        for lane, call in lanes.items():
            try:
                resp = await call("stream")
                got = await drain(resp)
                check(f"{lane}: a body streaming past the head wait arrives WHOLE",
                      got == want, repr(got))
            except HTTPException as e:
                check(f"{lane}: a body streaming past the head wait arrives WHOLE", False,
                      f"HTTP {e.status_code}: {e.detail}")
            try:
                await call("late")
                check(f"{lane}: an app that does not start answering is a 504", False,
                      "answered")
            except HTTPException as e:
                check(f"{lane}: an app that does not start answering in time is a 504 that "
                      "says so", e.status_code == 504 and "within" in str(e.detail),
                      f"{e.status_code} {e.detail}")
        # A dead upstream is noticed by TCP
        # keepalive, never by a clock — so a quiet but alive stream is whole.
        for lane, call in lanes.items():
            made.clear()
            resp = await call("quiet")
            got = await drain(resp)
            check(f"{lane}: a stream silent for longer than the head wait, twice, arrives whole",
                  got == b"tick-0;tick-1;tick-2;", repr(got))
            ka = list(made)
            check(f"{lane}: the upstream socket carries TCP keepalive (60 s idle, 15 s x 8 probes), "
                  "so a dead upstream ends the stream",
                  bool(ka) and all(k[0] != 0 and k[1:] == (60, 15, 8) for k in ka), repr(ka))
    finally:
        if real_factory is not None:
            P._keepalive_socket = real_factory
        aiohttp.ClientTimeout = real_timeout
        P.get_supervisor = real_sup
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(main())
    if FAILED:
        print(f"\n[proxy-stream] {len(FAILED)} FAILED")
        sys.exit(1)
    print("\n[proxy-stream] all green")
