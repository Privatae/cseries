#!/usr/bin/env python3
"""An outside agent's open GET /mcp stream never holds up the Host's stop.

uvicorn's graceful stop waits for every open response to finish before the
app's own shutdown runs (lifespan: the database pool, the member door).
An agent keeps its event stream open all the time, for up to
routes/mcp.py STREAM_LIFE_S, so without help a stop with an agent connected
waits that long, and the runtime's stop timeout kills the process first,
skipping that shutdown. The server launch.py runs ends every open agent
stream the moment it is told to stop (agent_access.sessions.end_all), and
what had not been sent waits for the session's next stream.

In-process: a real uvicorn server on an ephemeral loopback port, told to
stop through its own handle_exit (no signal is sent to any process).
"""
import asyncio
import os
import signal
import socket
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from server import launch  # noqa: E402
from server.agent_access import sessions  # noqa: E402
from server.routes import mcp as M  # noqa: E402

failed = 0
SID = "s" * 32


def check(ok, what, detail=None):
    global failed
    print(("ok   " if ok else "FAIL ") + what + ("" if ok or detail is None else f"  -> {detail!r}"))
    if not ok:
        failed += 1


async def main() -> int:
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        return M._event_stream(SID)

    sessions._remember(SID, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server_cls = getattr(launch, "HostServer", uvicorn.Server)
    server = server_cls(uvicorn.Config(app, log_level="warning", lifespan="off",
                                         timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.02)

    got = b""
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("GET", f"http://127.0.0.1:{port}/stream") as r:
            it = r.aiter_bytes()
            got += await it.__anext__()  # the retry line: the stream is open
            check(b"retry:" in got, "the agent's stream is open", got)
            t0 = time.monotonic()
            server.handle_exit(signal.SIGTERM, None)
            server._captured_signals.clear()  # never re-raised on this process
            try:
                await asyncio.wait_for(asyncio.shield(serving), 8)
            except asyncio.TimeoutError:
                pass
            took = time.monotonic() - t0
    if not serving.done():
        server.force_exit = True
        await asyncio.wait_for(serving, 30)
    check(took < 5, f"told to stop, the server ends the open stream and stops ({took:.1f}s)", took)
    sessions.answered("nothing")  # no stream left behind to put into
    check(SID not in sessions._streams, "and the stream is let go", list(sessions._streams))
    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
