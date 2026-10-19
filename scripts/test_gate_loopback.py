#!/usr/bin/env python3
"""Gate: scripts/gate_loopback.py's ClientClosesFirst waits for the client only
after a response the client can see the end of, and never carries that wait
over to a request it did not answer.

On a kept-alive connection, a handler that answered one request with a
Content-Length and then fails on the next one, before answering it, used to
inherit the first response's framing: finish() then waited the whole
client_close_wait_s for a client that was itself waiting for an answer.
The server must close that connection at once.

The rest of the file is the other helpers of scripts/gate_loopback.py, each
checked on a real connection:
  - client_closes_first_aiohttp: an aiohttp server leaves a "Connection:
    close" client's connection open, for a plain reply and a chunked one,
    until the client hangs up or its wait runs out; it leaves an HTTP/1.0
    request, a keep-alive request and a WebSocket as aiohttp has them. It
    reaches into aiohttp's StreamResponse._keep_alive, so an aiohttp that stops
    honouring that fails here instead of quietly leaving TIME_WAITs.
  - await_peer_close: true when the peer hangs up, false after its wait.
  - get_client_first: the client's FIN reaches the server before the server
    has answered.
  - the connector kit's pod bridge: a join that cannot dial the gate says why
    in the gate's own output (a pod's client only ever reports "Remote end
    closed connection without response"), and a join that can prints nothing.
  - every uvicorn.Config and uvicorn.run in scripts/*.py passes
    timeout_keep_alive (the gates use IDLE_KEEP_ALIVE_S), in the code and in
    code a gate runs from a string (a child's source). uvicorn's default
    closes an idle connection after 5 s, from the server's side; the book
    gate's mock failed CI runs that way. Both scans are
    checked on planted calls.
"""
import ast
import asyncio
import contextlib
import http.client
import http.server
import io
import os
import re
import select
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_loopback import (ClientClosesFirst, await_peer_close,  # noqa: E402
                           client_closes_first_aiohttp, get_client_first)

FAILS = []
WAIT_S = 3.0


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  [{detail}]"))
    if not ok:
        FAILS.append(name)


class Handler(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep the connection open between requests
    client_close_wait_s = WAIT_S

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/boom":
            raise RuntimeError("handler fails before it answers")
        body = b"fine"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                               # the failure is the point of the test


def check_client_closes_first_mixin():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=WAIT_S * 3)
        conn.request("GET", "/ok")
        first = conn.getresponse()
        check("a kept-alive request is answered with its length", first.status == 200
              and first.read() == b"fine", first.status)

        conn.request("GET", "/boom")
        started = time.monotonic()
        try:
            conn.getresponse().read()
            outcome = "answered"
        except (http.client.RemoteDisconnected, ConnectionResetError, http.client.BadStatusLine):
            outcome = "closed"
        except OSError as e:
            outcome = f"error {e!r}"
        took = time.monotonic() - started
        check("a request the handler failed on ends with the connection closed", outcome == "closed", outcome)
        check(f"...at once, not after the {WAIT_S:.0f} s wait the last response earned",
              took < WAIT_S / 2, f"{took:.2f} s")
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


# ── client_closes_first_aiohttp ───────────────────────────────────────────

async def _read_reply(reader):
    """One HTTP/1.1 reply, framed by Content-Length or chunked: (status,
    headers, body)."""
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
    body = b""
    if "content-length" in hdrs:
        body = await asyncio.wait_for(reader.readexactly(int(hdrs["content-length"])), 5)
    elif "chunked" in hdrs.get("transfer-encoding", ""):
        while True:
            size = int((await asyncio.wait_for(reader.readline(), 5)).strip() or b"0", 16)
            if size == 0:
                await asyncio.wait_for(reader.readline(), 5)
                break
            body += await asyncio.wait_for(reader.readexactly(size), 5)
            await asyncio.wait_for(reader.readexactly(2), 5)
    return status, hdrs, body


async def _after_reply(port, request: bytes, wait_s: float):
    """Send `request`, read the reply, then say whether the server hung up
    within wait_s: (status, body, "open" | "closed", seconds waited)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        status, _hdrs, body = await _read_reply(reader)
        t0 = time.monotonic()
        try:
            got = await asyncio.wait_for(reader.read(1), wait_s)
            state = "closed" if got == b"" else f"data {got!r}"
        except asyncio.TimeoutError:
            state = "open"
        return status, body, state, time.monotonic() - t0
    finally:
        writer.close()


async def check_aiohttp():
    import aiohttp
    from aiohttp import web

    async def plain(request):
        return web.Response(text="ok")

    async def stream(request):
        r = web.StreamResponse(status=200)
        await r.prepare(request)
        for part in (b"a", b"b", b"c"):
            await r.write(part)
        await r.write_eof()
        return r

    async def echo(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            await ws.send_str(msg.data)
        return ws

    async def serve(wait_s):
        app = web.Application()
        app.router.add_get("/plain", plain)
        app.router.add_get("/stream", stream)
        app.router.add_get("/ws", echo)
        client_closes_first_aiohttp(app, wait_s)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    close = b"GET /%s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    runner, port = await serve(WAIT_S)
    try:
        st, body, state, _ = await _after_reply(port, close % b"plain", 0.5)
        check("aiohttp: a 'Connection: close' client gets its reply and the server does not hang up first",
              (st, body, state) == (200, b"ok", "open"), (st, body, state))
        st, body, state, _ = await _after_reply(port, close % b"stream", 0.5)
        check("aiohttp: ...for a chunked reply too",
              (st, body, state) == (200, b"abc", "open"), (st, body, state))

        # A keep-alive client is served as aiohttp serves it: one connection, two requests.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            ok = []
            for _ in range(2):
                writer.write(b"GET /plain HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                st, _h, body = await _read_reply(reader)
                ok.append((st, body))
        finally:
            writer.close()
        check("aiohttp: a keep-alive client still reuses its connection", ok == [(200, b"ok")] * 2, ok)

        # HTTP/1.0 has no keep-alive to turn over: the server closes as before.
        st, body, state, _ = await _after_reply(port, b"GET /plain HTTP/1.0\r\n\r\n", 2.0)
        check("aiohttp: an HTTP/1.0 request is closed by the server as before",
              (st, body, state) == (200, b"ok", "closed"), (st, body, state))

        # A WebSocket works, and ends.
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"http://127.0.0.1:{port}/ws", timeout=5) as ws:
                await ws.send_str("hi")
                msg = await asyncio.wait_for(ws.receive(), 5)
        check("aiohttp: a WebSocket still echoes and closes", msg.type.name == "TEXT" and msg.data == "hi",
              (msg.type, msg.data))
    finally:
        await runner.cleanup()

    # The wait is capped: a client that never hangs up is hung up on.
    runner, port = await serve(1.0)
    try:
        st, body, state, waited = await _after_reply(port, close % b"plain", 20.0)
        check("aiohttp: a client that never hangs up is hung up on after the wait (no new hang)",
              (st, body, state) == (200, b"ok", "closed") and 0.7 <= waited < 15.0,
              (st, body, state, f"{waited:.2f} s"))
    finally:
        await runner.cleanup()


# ── await_peer_close ──────────────────────────────────────────────────────

async def _until(cond, limit=20.0):
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < limit:
        await asyncio.sleep(0.02)


async def check_peer_close():
    seen = {"closes": [], "never": []}

    def handler_for(key, wait_s):
        async def handler(reader, writer):
            t0 = time.monotonic()
            seen[key].append((await await_peer_close(reader, wait_s), time.monotonic() - t0))
            writer.close()
        return handler

    patient = await asyncio.start_server(handler_for("closes", 10.0), "127.0.0.1", 0)
    brief = await asyncio.start_server(handler_for("never", 1.0), "127.0.0.1", 0)
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", patient.sockets[0].getsockname()[1])
        w.write(b"ignored bytes the peer still sends")
        await w.drain()
        await asyncio.sleep(0.2)
        w.close()
        await _until(lambda: seen["closes"])
        check("await_peer_close: true when the peer hangs up (and drops what it still sent)",
              len(seen["closes"]) == 1 and seen["closes"][0][0] is True and seen["closes"][0][1] < 9.0,
              seen["closes"])

        _r, w = await asyncio.open_connection("127.0.0.1", brief.sockets[0].getsockname()[1])
        await _until(lambda: seen["never"])
        check("await_peer_close: false after its wait when the peer never does",
              len(seen["never"]) == 1 and seen["never"][0][0] is False and 0.8 <= seen["never"][0][1] < 15.0,
              seen["never"])
        w.close()
    finally:
        for srv in (patient, brief):
            srv.close()
            await srv.wait_closed()


# ── get_client_first ──────────────────────────────────────────────────────

class _EofProbe(http.server.BaseHTTPRequestHandler):
    """Answers only once it can see whether the client has already hung up its
    sending side (a peek that reads end-of-file), and says what it saw."""
    seen: list = []
    wait_s = 10.0       # long enough for a client on a loaded box; returns at once on end-of-file

    def log_message(self, *a):
        pass

    def do_GET(self):
        ready, _, _ = select.select([self.connection], [], [], self.wait_s)
        eof = bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
        _EofProbe.seen.append(eof)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_get_client_first():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EofProbe)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        status, body = get_client_first(port, "/health", timeout=30)
        check("get_client_first: the answer comes back whole", (status, body) == (200, b"{}"), (status, body))
        check("get_client_first: the client's FIN was on the connection before the server answered",
              _EofProbe.seen == [True], _EofProbe.seen)
        _EofProbe.seen.clear()
        _EofProbe.wait_s = 0.3      # the control never hangs up first: do not wait for it
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            r.read()
        check("(control) a urllib client has not hung up when the server answers",
              _EofProbe.seen == [False], _EofProbe.seen)
    finally:
        srv.shutdown()
        srv.server_close()


# ── the connector kit's pod bridge ────────────────────────────────────────

def _bridge_to(port: int, wait_for_line: bool) -> tuple[bool, str]:
    """Open a connection to a _UnixBridge that dials 127.0.0.1:`port`; say
    whether the bridge closed it, and what the gate printed meanwhile."""
    import connector_gate_kit as kit
    d = tempfile.mkdtemp(prefix="pn-")
    path = os.path.join(d, str(port))
    bridge = kit._UnixBridge(path, port)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(20)
            c.connect(path)
            if wait_for_line:
                closed = c.recv(1) == b""
            else:
                time.sleep(0.5)             # the join had its time to print, if it was going to
                closed = False
            c.close()
        return closed, out.getvalue()
    finally:
        bridge.close()
        shutil.rmtree(d, ignore_errors=True)


def check_bridge_names_a_failed_dial():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()                           # nothing listens on it now
    closed, said = _bridge_to(dead, wait_for_line=True)
    check("bridge: a pod's connection is closed when the gate cannot be dialled", closed)
    check("bridge: ...and the gate's output names the port, the errno and what the port holds",
          f"127.0.0.1:{dead}" in said and "errno=111" in said and "ECONNREFUSED" in said
          and "listener on that port: no" in said, said)

    live = socket.socket()
    live.bind(("127.0.0.1", 0))
    live.listen(4)
    try:
        _closed, said = _bridge_to(live.getsockname()[1], wait_for_line=False)
    finally:
        live.close()
    check("bridge: a join that can dial the gate prints nothing", said == "", said)


_UVICORN_SERVERS = ("Config", "run")


def uvicorn_configs_without_keep_alive(paths):
    """`name:line` of every uvicorn.Config or uvicorn.run call in `paths`
    that passes no timeout_keep_alive keyword (a **splat counts as none)."""
    bad = []
    for p in paths:
        try:
            tree = ast.parse(p.read_text(), str(p))
        except SyntaxError as e:
            bad.append(f"{p.name}: does not parse ({e.msg})")
            continue
        lines = sorted(n.lineno for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr in _UVICORN_SERVERS
                       and isinstance(n.func.value, ast.Name) and n.func.value.id == "uvicorn"
                       and not any(k.arg == "timeout_keep_alive" for k in n.keywords))
        bad += [f"{p.name}:{line}" for line in lines]
    return bad


def uvicorn_texts_without_keep_alive(paths):
    """The same, read as text: each uvicorn.Config or uvicorn.run call from
    its name to its closing bracket, so a server in code held in a string (a
    child process's source) is read too. `name:line` of each whose text has
    no timeout_keep_alive."""
    bad = []
    for p in paths:
        text = p.read_text()
        for m in re.finditer(r"\buvicorn\.(?:%s)\(" % "|".join(_UVICORN_SERVERS), text):
            depth, i = 1, m.end()
            while i < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            if "timeout_keep_alive" not in text[m.end():i]:
                bad.append(f"{p.name}:{text.count(chr(10), 0, m.start()) + 1}")
    return bad


def check_uvicorn_keeps_idle_connections():
    from pathlib import Path
    files = sorted(Path(__file__).resolve().parent.glob("*.py"))
    for scan, what in ((uvicorn_configs_without_keep_alive, "in the code"),
                       (uvicorn_texts_without_keep_alive, "in the text, code in strings included")):
        bad = scan(files)
        check(f"uvicorn: every uvicorn.Config and uvicorn.run in scripts/*.py passes timeout_keep_alive "
              f"(gate_loopback.IDLE_KEEP_ALIVE_S), {what}", not bad,
              "add timeout_keep_alive=IDLE_KEEP_ALIVE_S at scripts/" + ", scripts/".join(bad))
    # Planted: built from parts, so this file's own text never matches.
    cfg, run = "uvicorn." + "Config(", "uvicorn." + "run("
    with tempfile.TemporaryDirectory() as d:
        planted = Path(d) / "planted.py"
        planted.write_text("import uvicorn\n"
                           f"a = uvicorn.Server({cfg}app, host='127.0.0.1', log_level='error'))\n"
                           f"b = {cfg}app, timeout_keep_alive=IDLE_KEEP_ALIVE_S)\n"
                           f"c = {cfg}app, **kw)\n"
                           f"{run}app, host='127.0.0.1', port=1)\n"
                           f"{run}app, port=1, timeout_keep_alive=IDLE_KEEP_ALIVE_S)\n")
        found_code = uvicorn_configs_without_keep_alive([planted])
        found_text = uvicorn_texts_without_keep_alive([planted])
        child = Path(d) / "child.py"
        child.write_text("CHILD = r\'\'\'\n"
                         "import uvicorn\n"
                         f"server = uvicorn.Server({cfg}app, host='127.0.0.1',\n"
                         "                                       log_level='error'))\n"
                         f"{run}app, port=1)\n"
                         f"ok = {cfg}app, timeout_keep_alive=IDLE_KEEP_ALIVE_S)\n"
                         "\'\'\'\n")
        child_code = uvicorn_configs_without_keep_alive([child])
        child_text = uvicorn_texts_without_keep_alive([child])
    want = ["planted.py:2", "planted.py:4", "planted.py:5"]
    check("uvicorn: both scans name planted calls without the keyword (Config, a **splat, run), not those with it",
          found_code == want and found_text == want, (found_code, found_text))
    check("uvicorn: the text scan names a Config and a run inside a code string, which the code scan cannot see",
          child_code == [] and child_text == ["child.py:3", "child.py:5"], (child_code, child_text))


def main():
    check_uvicorn_keeps_idle_connections()
    check_client_closes_first_mixin()
    asyncio.run(check_aiohttp())
    asyncio.run(check_peer_close())
    check_get_client_first()
    check_bridge_names_a_failed_dial()
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        sys.exit(1)
    print("PASS: a loopback test server lets its client hang up first, and waits for it only as long as it can see why")


if __name__ == "__main__":
    main()
