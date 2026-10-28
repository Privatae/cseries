"""The parts of a connector store app's gate that do not depend on the
platform: the network guard, the fake model provider, a test CA, the seam
that sends the kernel's call to a platform host to a fake on loopback, the
recording proxy between a connector and its Host, the connector process and
its pod, and the install of the signed bundle.

Taken from scripts/test_connector_telegram_example.py for the
Slack gate (scripts/test_connector_slack_example.py), which is that gate's
shape for another platform; the Telegram gate keeps its own copy. Not a gate
itself (no test_ prefix): scripts/test_gate_coverage.py counts test_*.py and
check_*.py only.

Module state (the lists below) is the one run's, as it was in the Telegram
gate: one gate process imports this once.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import errno
import http.client
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import zlib

from gate_loopback import client_closes_first_aiohttp, get_client_first

fails: list[str] = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("   [" + str(detail)[:1500] + "]") if detail and not cond else ""))
    if not cond:
        fails.append(label)


def log_error_detail(logged: str, i: int, *, after: int = 14, before: int = 3) -> str:
    """The detail for a failed "the logs hold no error" check, all on ONE line:
    the matched line, a traceback's cause when the match opens one, the lines
    after it, then the few before it.

    The gate runner's report keeps a failed check's first line (pre_merge_gates.sh,
    report). The line that matches is often a traceback's first, which names a
    thread rather than the error: one CI run kept "Exception in thread Thread-2
    (work_forever):" and lost the cause."""
    lines = logged.splitlines()

    def one(xs):
        return " ⏎ ".join(x.strip() for x in xs if x.strip())
    # A traceback's cause is its first unindented line after the header. On the
    # pods' Python 3.14 the threading frames alone (with their caret lines) take
    # 7 lines, so in a thread's traceback the cause sits past any short window:
    # it goes right after the matched line.
    cause = None
    for t in range(i, min(i + 2, len(lines))):
        if lines[t].strip().startswith("Traceback (most recent call last)"):
            cause = next((k for k in range(t + 1, len(lines))
                          if lines[k].strip() and not lines[k][0].isspace()), None)
            break
    head = [lines[i]] + ([lines[cause]] if cause not in (None, i) else [])
    window = [ln for k, ln in enumerate(lines[i + 1:i + 1 + after], i + 1) if k != cause]
    prior = one(lines[max(0, i - before):i])
    return one(head + window) + (f"   (before it: {prior})" if prior else "")


# ── nothing leaves the box ────────────────────────────────────────────────
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
ALLOWED_PORTS: set[int] = set()
_DB = {"host": "", "port": 0}
BLOCKED: list[str] = []


def _net_guard(event, args):
    if event == "socket.connect":
        addr = args[1]
        if not isinstance(addr, tuple):
            return
        host, port = str(addr[0]), int(addr[1])
        if (host in _LOOPBACK and port in ALLOWED_PORTS) or port == _DB["port"]:
            return
        BLOCKED.append(f"connect {host}:{port}")
        raise ConnectionRefusedError(111, f"refused by the gate's network guard: {host}:{port}")
    if event == "socket.getaddrinfo":
        host = args[0].decode() if isinstance(args[0], bytes) else str(args[0] or "")
        if host and host not in _LOOPBACK and host != _DB["host"]:
            BLOCKED.append(f"lookup {host}")
            raise socket.gaierror(socket.EAI_NONAME, f"refused by the gate's network guard: {host}")


def guard_network(dsn: str, *ports: int) -> None:
    from urllib.parse import urlsplit
    u = urlsplit(dsn)
    host, port = u.hostname or "", u.port or 5432
    if not host or host.startswith("/") or host in _LOOPBACK:
        ALLOWED_PORTS.add(port)
    else:
        _DB["host"], _DB["port"] = host, port
    ALLOWED_PORTS.update(ports)
    sys.addaudithook(_net_guard)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def serve(handler, *, ssl_context=None, max_size=40 * 1024 * 1024):
    """An aiohttp app on loopback answering every path with `handler`:
    (runner, port). The connector apps' clients send "Connection: close"
    (urllib), and aiohttp would hang up first on every one of them; the app
    leaves the closing to its client instead (scripts/gate_loopback.py)."""
    from aiohttp import web
    app = web.Application(client_max_size=max_size)
    app.router.add_route("*", "/{tail:.*}", handler)
    client_closes_first_aiohttp(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


# ── the fake model provider (as test_connector_route.py) ─────────────────
MODEL = "claude-opus-5-5"
REQUESTS: list[dict] = []
# The model calls the turns make, in order. A step is a list of SSE events, a
# (status, body) error, or ("hold", event, step).
TURNS: list = []


def sse_text(*pieces: str, stop="end_turn") -> list[dict]:
    ev = [{"type": "message_start", "message": {"id": "msg_gate", "model": MODEL,
                                                "usage": {"input_tokens": 50, "output_tokens": 1}}},
          {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    ev += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}}
           for p in pieces]
    ev += [{"type": "content_block_stop", "index": 0},
           {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 9}},
           {"type": "message_stop"}]
    return ev


def sse_tool(tool_id: str, name: str, args: dict) -> list[dict]:
    raw = json.dumps(args)
    return [{"type": "message_start", "message": {"id": "msg_gate_t", "model": MODEL,
                                                  "usage": {"input_tokens": 50, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": raw}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}},
            {"type": "message_stop"}]


def _is_pick(body) -> bool:
    # A model call whose only tool is pick_thread: the separate call that once
    # picked a message's thread before the turn. A message is placed in
    # software now (thread_pick.place_connector) and the Host picks inside its
    # own turn (runtime/thread_choice), so the gates count these and expect none.
    names = [t.get("name") or (t.get("function") or {}).get("name")
             for t in (body or {}).get("tools") or []]
    return names == ["pick_thread"]


def picks_made() -> int:
    """Separate pick calls made before a turn: none is expected."""
    return sum(1 for r in REQUESTS if r["pick"])


async def provider(request):
    from aiohttp import web
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if request.method != "POST" or request.path != "/v1/messages":
        return web.Response(status=404, text="not served by the gate's provider")
    pick = _is_pick(body)
    REQUESTS.append({"pick": pick, "body": body})
    if pick:
        step = (500, '{"type":"error","error":{"type":"api_error",'
                     '"message":"a separate pick call: a message is placed in software"}}')
    else:
        step = TURNS.pop(0) if TURNS else sse_text("(an unscripted turn call)")
    if isinstance(step, tuple) and step and step[0] == "hold":
        await step[1].wait()
        step = step[2]
    if isinstance(step, tuple):
        return web.Response(status=step[0], text=step[1], content_type="application/json")
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    try:
        await resp.prepare(request)
        for e in step:
            await resp.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
            await asyncio.sleep(0)
        await resp.write_eof()
    except ConnectionResetError:
        pass        # the turn was stopped (a revoke) while this call was held
    return resp


# ── a test CA, and the kernel's seam to a fake platform ──────────────────

def make_tls(dirpath: str, names: list[str]) -> tuple[str, ssl.SSLContext]:
    """A test CA and one certificate for every name in `names`, signed by it:
    (the CA file, the fake's server context)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "connector gate test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(hours=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_file = os.path.join(dirpath, "ca.pem")
    cert_file = os.path.join(dirpath, "leaf.pem")
    key_file = os.path.join(dirpath, "leaf.key")
    with open(ca_file, "wb") as fh:
        fh.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(cert_file, "wb") as fh:
        fh.write(leaf.public_bytes(serialization.Encoding.PEM))
    with open(key_file, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    os.chmod(ca_file, 0o644)           # a pod (uid 1000) reads it, mounted
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_file, key_file)
    return ca_file, ctx


def route_hosts_to(hosts: set[str], port: int, ca_file: str) -> None:
    """THE seam, in this process only: aiohttp resolves each of `hosts` (on
    443) to the fake's loopback port, and trusts the fake's CA alongside the
    system's. Every other name resolves as it always does (and the network
    guard refuses anything off the box)."""
    import aiohttp.abc
    import aiohttp.connector
    import aiohttp.resolver

    class _Here(aiohttp.abc.AbstractResolver):
        def __init__(self, *a, **k):
            self._real = aiohttp.resolver.ThreadedResolver()

        async def resolve(self, host, port_=0, family=socket.AF_INET):
            if host in hosts and port_ == 443:
                return [{"hostname": host, "host": "127.0.0.1", "port": port,
                         "family": socket.AF_INET, "proto": 0, "flags": socket.AI_NUMERICHOST}]
            return await self._real.resolve(host, port_, family)

        async def close(self):
            await self._real.close()

    aiohttp.connector.DefaultResolver = _Here
    aiohttp.connector._SSL_CONTEXT_VERIFIED.load_verify_locations(cafile=ca_file)
    # The broker refuses a destination that resolves to this machine's
    # loopback as the Host itself (server/vault/destination.py; its own
    # gate is test_operator_header_trust.py). The fake platform IS on loopback,
    # so the guard is lifted here, in this process only, with the rest of the seam.
    from server.vault import destination as _dest
    _dest.session_kwargs = lambda: {}


# ── the proxy between the connector and its Host ─────────────────────────
PROXY_LOG: list[dict] = []
CUTS: list[dict] = []       # {"method", "path", "after": n lines}: the next match is cut


WS_APP: dict = {}           # {"app": the Host's ASGI app}: what a WebSocket upgrade is run by


async def asgi_ws(request, entry: dict):
    """A WebSocket upgrade to the Host, run on the Host's own ASGI app
    (WS_APP["app"], the FastAPI app the gate's uvicorn serves) in this
    process. uvicorn needs a WebSocket library (websockets or wsproto) to
    serve one and the gates' Python has neither, so this is the server side
    of the handshake: the scope uvicorn would build, the route, its
    capability check and everything behind it run as they are. A close before
    the accept is answered 403, as uvicorn answers it. Every text frame is
    recorded in entry["frames"] as ("up", text) from the connector and
    ("down", text) to it."""
    from aiohttp import web, WSMsgType
    app = WS_APP["app"]
    entry["frames"] = []
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "scheme": "ws", "path": request.path,
             "raw_path": request.path.encode(), "root_path": "",
             "query_string": request.query_string.encode(),
             "headers": [(k.lower().encode("latin-1"), v.encode("latin-1"))
                         for k, v in request.headers.items()],
             "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0), "subprotocols": [],
             "state": {}}
    inbox: asyncio.Queue = asyncio.Queue()
    inbox.put_nowait({"type": "websocket.connect"})
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, autoping=True)
    opened, done = asyncio.Event(), asyncio.Event()
    refused: dict = {}

    async def receive():
        return await inbox.get()

    async def send(msg):
        kind = msg.get("type")
        if kind == "websocket.accept":
            await ws.prepare(request)
            opened.set()
        elif kind == "websocket.send":
            if msg.get("text") is not None:
                entry["frames"].append(("down", msg["text"]))
                await ws.send_str(msg["text"])
            elif msg.get("bytes") is not None:
                await ws.send_bytes(msg["bytes"])
        elif kind == "websocket.close":
            if not opened.is_set():
                refused["code"], refused["reason"] = msg.get("code", 1000), msg.get("reason") or ""
                done.set()
            elif not ws.closed:
                await ws.close(code=msg.get("code", 1000),
                               message=(msg.get("reason") or "").encode()[:120])

    async def run_app():
        try:
            await app(scope, receive, send)
        finally:
            done.set()
    task = asyncio.create_task(run_app())
    waits = [asyncio.create_task(opened.wait()), asyncio.create_task(done.wait())]
    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    for w in waits:
        w.cancel()
    if not opened.is_set():
        entry["status"] = 403
        entry["refused"] = refused
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)
        return web.Response(status=403, text=f"refused: {refused.get('reason', '')}")
    entry["status"] = 101
    try:
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                entry["frames"].append(("up", m.data))
                inbox.put_nowait({"type": "websocket.receive", "text": m.data})
            elif m.type == WSMsgType.BINARY:
                inbox.put_nowait({"type": "websocket.receive", "bytes": m.data})
    finally:
        entry["close_code"] = ws.close_code
        inbox.put_nowait({"type": "websocket.disconnect", "code": ws.close_code or 1005})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 15)
        if not task.done():
            task.cancel()
    return ws


async def proxy(request, upstream: str, session):
    from aiohttp import web
    import aiohttp
    if request.headers.get("Upgrade", "").lower() == "websocket" and WS_APP.get("app"):
        entry = {"method": "WEBSOCKET", "path": request.path, "query": request.query_string,
                 "status": None, "lines": [], "cut": False, "body": None}
        PROXY_LOG.append(entry)
        return await asgi_ws(request, entry)
    body = await request.read()
    entry = {"method": request.method, "path": request.path, "status": None, "lines": [],
             "cut": False, "body": None}
    with contextlib.suppress(ValueError):
        entry["body"] = json.loads(body) if body else None
    PROXY_LOG.append(entry)
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("x-cseries-app-id", "x-cseries-connector-cap", "content-type")}
    try:
        return await _forward(request, entry, session, upstream, body, headers)
    except (aiohttp.ClientError, ConnectionResetError) as e:
        entry["status"] = 502          # the Host is gone (the gate is tearing down)
        return web.Response(status=502, text=f"the gate's Host is not there: {type(e).__name__}")


async def _forward(request, entry, session, upstream, body, headers):
    from aiohttp import web
    async with session.request(request.method, upstream + request.path_qs, data=body or None,
                               headers=headers) as up:
        entry["status"] = up.status
        ctype = up.headers.get("Content-Type", "")
        if "ndjson" not in ctype:
            data = await up.read()
            with contextlib.suppress(ValueError):
                entry["answer"] = json.loads(data) if data else None
            return web.Response(status=up.status, body=data,
                                headers={"Content-Type": ctype or "application/octet-stream"})
        rule = next((c for c in CUTS if c["method"] == request.method and c["path"] == request.path),
                    None)
        if rule is not None:
            CUTS.remove(rule)
        resp = web.StreamResponse(status=up.status, headers={"Content-Type": ctype})
        await resp.prepare(request)
        n = 0
        # Lines split here, not by aiohttp's readline: that refuses a line
        # past 128 KiB, and a picture's line is megabytes.
        buf, scanned = bytearray(), 0
        async for chunk in up.content.iter_any():
            buf.extend(chunk)
            while True:
                at = buf.find(b"\n", scanned)
                if at < 0:
                    scanned = len(buf)
                    break
                line = bytes(buf[:at + 1])
                del buf[:at + 1]
                scanned = 0
                await resp.write(line)
                obj = None
                with contextlib.suppress(ValueError):
                    obj = json.loads(line)
                if not isinstance(obj, dict) or obj.get("type") == "alive":
                    continue
                obj.pop("data_b64", None)      # recorded without the picture's bytes
                entry["lines"].append(obj)
                n += 1
                if rule is not None and n >= rule["after"]:
                    entry["cut"] = True
                    await asyncio.sleep(0.05)
                    request.transport.close()      # the stream drops here
                    return resp
        await resp.write_eof()
        return resp


def proxied(method: str, path_re: str) -> list[dict]:
    return [e for e in PROXY_LOG if e["method"] == method and re.fullmatch(path_re, e["path"])]


def posted(message_id) -> dict:
    """The proxy's record of the POST /api/connector/messages for one message."""
    return next((e for e in proxied("POST", "/api/connector/messages")
                 if (e["body"] or {}).get("message_id") == str(message_id)), {})


def reports(turn_id) -> list[dict]:
    """The /sent items the Host has answered for one turn."""
    return [i for e in proxied("POST", "/api/connector/messages/sent")
            if e["status"] is not None and (e["body"] or {}).get("turn_id") == turn_id
            for i in e["body"]["sent"]]


def line_of(entry: dict, kind: str) -> dict:
    return next((x for x in entry.get("lines", []) if x.get("type") == kind), {})


# ── the connector process ────────────────────────────────────────────────
# Every connector this run starts, and every pod: killed on every way out
# (main's finally, atexit, and SIGTERM/SIGINT/SIGHUP, which the gate turns
# into sys.exit). Measured on the Telegram gate: 96 connector trees
# left running, ppid 1. A connector in a private PID namespace is that
# namespace's PID 1 and unshare ignores SIGTERM while it waits, so each starts
# in its own session and is SIGKILLed as a group, and setpriv --pdeathsig KILL
# takes it down with the gate even when the gate itself is SIGKILLed.
SPAWNED: list = []
PODS: set = set()


def kill_spawned() -> None:
    for c in list(SPAWNED):
        c.stop()
    for name in list(PODS):
        with contextlib.suppress(Exception):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        PODS.discard(name)


def install_exit_handlers() -> None:
    """kill_spawned on every way out: SIGTERM, SIGINT and SIGHUP end in
    sys.exit, which runs atexit."""
    import atexit
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: sys.exit(128 + signum))
    atexit.register(kill_spawned)


def processes_naming(marker: str) -> list[str]:
    """This run's processes still alive: any whose command line names
    `marker` (a path unique to the run)."""
    mark = marker.encode()
    out = []
    for d in pathlib.Path("/proc").iterdir():
        if d.name.isdigit():
            with contextlib.suppress(OSError):
                if mark in (d / "cmdline").read_bytes():
                    out.append(d.name)
    return out


def can_unshare() -> bool:
    if not shutil.which("unshare"):
        return False
    r = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
                        "--kill-child=SIGKILL", "true"], capture_output=True)
    return r.returncode == 0


class Connector:
    """A connector.py the install extracted, run as its own process under
    `wrapper` (a script the gate wrote: argv is [wrapper, connector.py,
    host port, *extra]), in its own session, killed as a group."""

    def __init__(self, *, app: str, script: pathlib.Path, wrapper: str, cap: str,
                 proxy_port: int, state_dir: str, log_path: str, namespaced: bool,
                 extra_args=(), env=None):
        self.port = free_port()
        ALLOWED_PORTS.add(self.port)
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                              "ALL_PROXY", "all_proxy")}
        clean.update({"PORT": str(self.port), "PRIVATAE_APP_ID": app,
                      "CSERIES_KERNEL_URL": f"http://127.0.0.1:{proxy_port}",
                      "CSERIES_CONNECTOR_CAP": cap, "CONNECTOR_STATE_DIR": state_dir,
                      "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        clean.update(env or {})
        argv = [sys.executable, wrapper, str(script), str(proxy_port), *map(str, extra_args)]
        if namespaced:
            argv = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
                    "--kill-child=SIGKILL"] + argv
        if shutil.which("setpriv"):
            argv = ["setpriv", "--pdeathsig", "KILL", "--"] + argv
        self._log = open(log_path, "ab")
        self.proc = subprocess.Popen(argv, env=clean, stdout=self._log, stderr=subprocess.STDOUT,
                                     cwd=str(script.parent), start_new_session=True)
        SPAWNED.append(self)

    def health(self):
        # The app's own server hangs up as soon as it has answered; this client
        # sends its FIN first so the TIME_WAIT is on the gate's side.
        try:
            status, body = get_client_first(self.port, "/health")
            return json.loads(body) if 200 <= status < 300 else None
        except Exception:  # noqa: BLE001
            return None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        """SIGKILL the connector's whole group: unshare and the connector it
        forked (its own session, so the group is its pid)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(10)
        if not self._log.closed:
            self._log.close()
        if self in SPAWNED:
            SPAWNED.remove(self)


def log_text(path: str) -> str:
    with contextlib.suppress(OSError):
        return pathlib.Path(path).read_text(errors="replace")
    return ""


async def wait_for(cond, timeout=30.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        got = cond()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return True
        await asyncio.sleep(step)
    return False


def png(w: int, h: int, rgb=(30, 90, 200)) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h, 9)) + chunk(b"IEND", b""))


async def install_row(pool, app_id: str, provenance: str = "custom") -> None:
    from server.applications import registry
    from server.applications.manifest import BuildSpec, LicenseSpec, SourceSpec
    await registry.register(pool, types.SimpleNamespace(
        id=app_id, name=app_id.title(), version="1.0.0", provenance=provenance,
        entrypoint="index.html", permissions=[], source_dir=None, license=LicenseSpec(),
        source=SourceSpec(), build=BuildSpec()))


# What the install told the desktop shell. installer.install_bundle announces
# an install with server.main.broadcast and asks server.main for the bridge;
# server.main is the kernel's whole web app and does not import off the
# container, so the gate puts this one-module stand-in in its place: no
# bridge, and a broadcast that records.
SHELL_PUSHES: list[dict] = []


def stand_in_server_main() -> None:
    mod = types.ModuleType("server.main")

    async def broadcast(msg: dict) -> None:
        SHELL_PUSHES.append(msg)
    mod.broadcast = broadcast
    mod.get_bridge_client = lambda: None
    sys.modules["server.main"] = mod


async def part_s(pool, *, root: pathlib.Path, app: str, platform: str, other_platform: str,
                 source: pathlib.Path, bundle: pathlib.Path, install_root: pathlib.Path,
                 privatae_pub: pathlib.Path, script: str = "connector.py") -> bool:
    """The signed factory bundle installs the way the App Store and
    install_app install it (installer.install_bundle, source='manual'; the
    entitlement check, which asks the bridge, is the one gate skipped: the app
    is free and there is no bridge here)."""
    print("[S] the signed store bundle installs through installer.install_bundle")
    import yaml
    import zipfile
    from server.applications import installer, proxy as app_proxy, registry, trust_store
    from server.network import nac_connector as nc
    installer.INSTALL_ROOT = app_proxy.INSTALL_ROOT = install_root
    trust_store.CSERIES_PUBLIC_KEY_PATH = privatae_pub
    stand_in_server_main()
    await trust_store.seed_cseries_key(pool)
    pub = privatae_pub.read_bytes()
    privatae = next((k for k in await trust_store.list_active_keys(pool) if k.public_key == pub), None)
    check("S the trust store holds the image's C-Series key, allowed native and standard only",
          privatae is not None and sorted(privatae.provenance_allowed) == ["native", "standard"],
          privatae)
    if not bundle.is_file():
        check(f"S {bundle.relative_to(root)} exists (scripts/rebuild_factory_bundles.sh)", False)
        return False
    # The signed bundle carries the app's committed files and nothing else.
    try:
        with zipfile.ZipFile(bundle) as z:
            packed = sorted(n[len("payload/"):] for n in z.namelist()
                            if n.startswith("payload/") and not n.endswith("/"))
    except (OSError, zipfile.BadZipFile) as e:
        packed = [f"unreadable: {e!r}"]
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--", str(source)],
                             capture_output=True, text=True)
    want_files = sorted(str(pathlib.Path(p).relative_to(source.relative_to(root)))
                        for p in tracked.stdout.split() if p) if tracked.returncode == 0 else None
    if want_files is not None:
        want_files = [p for p in want_files if p != "app.yaml"]
    check("S the bundle's payload is exactly the app's committed files (no stray file signed in)",
          want_files is not None and packed == want_files, (packed, want_files))
    try:
        await installer.install_bundle(pool, bundle.read_bytes(), source="manual",
                                       check_entitlement=False)
    except Exception as e:  # noqa: BLE001
        check(f"S {bundle.relative_to(root)} installs through installer.install_bundle", False,
              f"{type(e).__name__}: {e}")
        return False
    check(f"S {bundle.relative_to(root)} installs through installer.install_bundle", True)
    want = yaml.safe_load((source / "app.yaml").read_text()) or {}
    row = await registry.get(pool, app)
    check("S the row: active, standard, the source's version, signed by the Privatae key",
          row is not None and row.status == "active" and row.provenance == "standard"
          and row.version == str(want.get("version"))
          and privatae is not None and row.source_signature == privatae.fingerprint,
          row and (row.status, row.provenance, row.version, row.source_signature))
    installed = install_root / app
    got = (installed / script).read_bytes() if (installed / script).is_file() else b""
    check(f"S the {script} the install extracted is the source's, byte for byte",
          bool(got) and got == (source / script).read_bytes(), len(got))
    check("S the install holds no index.html: the window is the connector's own page (W)",
          not (installed / "index.html").exists())
    refusal = nc.app_refusal(app, platform)
    check(f"S nac_connector.app_refusal reads the INSTALLED manifest and accepts it for {platform}",
          refusal is None, refusal)
    check(f"S ...and refuses it for {other_platform} (the pin is per platform)",
          nc.app_refusal(app, other_platform) is not None)
    check("S the desktop was told it is installed (app_installed)",
          any(p.get("type") == "app_installed" and (p.get("data") or {}).get("app_id") == app
              for p in SHELL_PUSHES), SHELL_PUSHES)
    return True


async def part_w(hport: int, conn, hb, *, app: str, page_text: str) -> None:
    """The window. The manifest's entrypoint, index.html, has no file, so the
    real proxy route forwards the window's page to the app's pod (here, the
    connector process: harness_backend.ensure_awake_app answers with it)."""
    print("[W] the app's window is the connector's own page, through the proxy")
    import aiohttp
    woke: list[str] = []

    async def _pod(app_id):
        woke.append(app_id)
        return f"127.0.0.1:{conn.port}" if app_id == app else None
    saved = hb.ensure_awake_app
    hb.ensure_awake_app = _pod
    try:
        async with aiohttp.ClientSession() as s:
            for path in ("", "index.html"):
                async with s.get(f"http://127.0.0.1:{hport}/system-apps/{app}/{path}") as r:
                    body, ctype = await r.text(), r.headers.get("Content-Type", "")
                check(f"W /system-apps/{app}/{path} is answered by the connector's status page",
                      r.status == 200 and ctype.startswith("text/html") and page_text in body,
                      (r.status, ctype, body[:300]))
    finally:
        hb.ensure_awake_app = saved
    check("W ...because the proxy woke the app's pod and forwarded to it", woke == [app, app], woke)


# ── the app's pod, under its memory limit ─────────────────────────────────
# The pod's memory LIMIT is the manifest's resources.baseline_memory_mi (the
# operator renders it as the app container's limits.memory). That limit
# covers the whole container: the platform's wrapper (run.py, which the
# operator puts in front of the manifest command), the connector it runs, and
# the probes the kubelet execs in it. So the cases run in a container of the
# runtime image, started as the operator starts it, under
# `docker run --memory=<limit> --memory-swap=<limit>`, and the verdict is the
# kernel's: not OOM-killed (State.OOMKilled and memory.events oom_kill).
RUNTIME_FILES = ("sidecars/app-runtime-python/Dockerfile", "sidecars/app-runtime-python/fetch.py",
                 "sidecars/app-runtime-python/run.py", "sidecars/app-egress/egress.py")


def runtime_image(root: pathlib.Path, kind: str) -> tuple[str | None, str]:
    """(the image, how it was got). APP_RUNTIME_TEST_IMAGE names one; else the
    image this tree's sidecars/app-runtime-python/Dockerfile builds, tagged by
    the hash of what goes into it and built only when absent."""
    import hashlib
    named = os.environ.get("APP_RUNTIME_TEST_IMAGE")
    if named:
        return named, "APP_RUNTIME_TEST_IMAGE"
    h = hashlib.sha256()
    for rel in RUNTIME_FILES:
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    tag = f"cseries-app-runtime-python-gate:{h.hexdigest()[:12]}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag, "built from this tree before"
    with tempfile.TemporaryDirectory(prefix="app-runtime-ctx-") as ctx:
        for rel in RUNTIME_FILES:
            dst = pathlib.Path(ctx, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, dst)
        r = subprocess.run(["docker", "build", "-q", "--label", f"cseries.gate.image={kind}",
                            "-f", str(pathlib.Path(ctx, RUNTIME_FILES[0])), "-t", tag, ctx],
                           capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        # The connector gates run side by side, so two of them can build this
        # image at once, and the one that tags second fails with "image ...
        # already exists" (seen in CI). The tag is the hash of the
        # inputs, so the image the other build tagged is this one.
        if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
            return tag, "built from this tree by a gate running beside this one"
        return None, f"docker build failed: {(r.stderr or r.stdout)[-800:]}"
    return tag, "built from this tree now"


def docker(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def reap_pods(kind: str) -> None:
    """Remove the pods a killed run of this gate left: labelled with their
    maker (Pod, PodNet), removed only when that maker is gone, by
    kernel_schema's rule (maker_gone), each with a line saying why."""
    from kernel_schema import reap_leftovers
    reap_leftovers(kind)


# ── the pod's network ─────────────────────────────────────────────────────
# A pod's containers share one network namespace, which Kubernetes keeps in
# the pod's pause container. Here a holder container plays that part: the
# app's container joins it (--network container:<holder>), and every port
# the app reaches on the gate (its Host, a platform's fake) is carried from
# the pod's own 127.0.0.1 to the gate's through a Unix socket in a directory
# both sides see. The gate asks the app (/health) the same way, through the
# socket "app" there, so the pod publishes no port on the gate's host.
# Measured on the CI runner: with a published port (-p
# 127.0.0.1::<port>), the rootless daemon picks the host port in its own
# namespace and RootlessKit then failed to bind it on the host ("listen tcp4
# 127.0.0.1:32776: bind: address already in use") while other gates ran.
#
# The pod used to run with `--network host`, which reaches the gate only where
# Docker's host network is the gate's. The CI runner's Docker is rootless
# (ci-runner-docker.service, slirp4netns, host loopback disabled): its "host"
# network is its own namespace. Measured on the runner: the gate
# was in net:[4026531833] (pid 1's), dockerd in net:[4026533026], and the pod
# logged "runtime-log POST failed: Connection refused" for the gate's Host.
#
# The directory, and every path the pod mounts, must be one the Docker daemon
# sees. The CI workflow puts TMPDIR in the job's workspace for that reason:
# the runner's dockerd has its own /tmp (PrivateTmp), and a pod mounting the
# gate's /tmp got an empty /app-src ("can't open file '/app-src/connector.py'",
# same run).
POD_NET_FORWARDER = r"""
import os, socket, sys, threading
def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass
def join(conn, family, addr):
    peer = socket.socket(family)
    try:
        peer.connect(addr)
    except OSError:
        conn.close(); peer.close(); return
    back = threading.Thread(target=pump, args=(peer, conn), daemon=True)
    back.start(); pump(conn, peer); back.join(); conn.close(); peer.close()
def serve(lsock, family, addr):
    while True:
        conn, _ = lsock.accept()
        threading.Thread(target=join, args=(conn, family, addr), daemon=True).start()
app_port, gate_ports = int(sys.argv[1]), sys.argv[2:]
for port in gate_ports:
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", int(port)))
    lsock.listen(64)
    threading.Thread(target=serve, args=(lsock, socket.AF_UNIX, "/gate-net/" + port),
                     daemon=True).start()
app = socket.socket(socket.AF_UNIX)
app.bind("/gate-net/app")
os.chmod("/gate-net/app", 0o777)
app.listen(64)
threading.Thread(target=serve, args=(app, socket.AF_INET, ("127.0.0.1", app_port)),
                 daemon=True).start()
print("forwarding", *gate_ports, flush=True)
threading.Event().wait()
"""


class _UnixHTTP(http.client.HTTPConnection):
    """An HTTP connection over the Unix socket at `path`."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


def _port_note(port: int) -> str:
    """What /proc/net/tcp holds for local port `port` (IPv4): whether a socket
    still listens on it, and how many TIME_WAIT sockets sit on it, which are
    the server's side of connections it closed first. A dial refused while a
    listener is there and TIME_WAIT sockets sit on its port is the CI runner's
    firewall refusing a source port that reuses one of them; a dial refused
    with no listener is a gate that has already shut that server down."""
    try:
        with open("/proc/net/tcp") as fh:
            rows = [ln.split() for ln in fh.read().splitlines()[1:]]
    except OSError as e:
        return f"/proc/net/tcp unreadable: {e}"
    listening = time_wait = 0
    for f in rows:
        if len(f) > 3 and int(f[1].rsplit(":", 1)[1], 16) == port:
            listening += f[3] == "0A"
            time_wait += f[3] == "06"
    return f"listener on that port: {'yes' if listening else 'no'}; TIME_WAIT sockets on it: {time_wait}"


class _UnixBridge:
    """A Unix socket at `path`; each connection to it is joined to the gate's
    127.0.0.1:`port`. A join that cannot dial the gate closes the pod's
    connection, which its client can only report as "Remote end closed
    connection without response"; the reason is printed to the gate's own
    output (never to the pod, whose logs check M reads)."""

    def __init__(self, path: str, port: int):
        self.port = port
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._join, args=(conn,), daemon=True).start()

    def _join(self, conn: socket.socket) -> None:
        try:
            gate = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            gate.settimeout(None)
        except OSError as e:
            print(f"  BRIDGE  a pod's connection to the gate's 127.0.0.1:{self.port} was not made: "
                  f"{type(e).__name__} errno={e.errno} ({errno.errorcode.get(e.errno or 0, '?')}) "
                  f"{e}; {_port_note(self.port)}", flush=True)
            conn.close()
            return
        back = threading.Thread(target=_pump, args=(gate, conn), daemon=True)
        back.start()
        _pump(conn, gate)
        back.join()
        conn.close()
        gate.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.srv.close()


class PodNet:
    """The network a pod's container joins: a holder container from the pod's
    image, with each of `gate_ports` forwarded from the pod's 127.0.0.1 to the
    gate's, the app's `app_port` reachable from the gate through the Unix
    socket `app_sock` (health() asks it), and `add_hosts` ("name:ip") in its
    /etc/hosts, which the pod shares. Nothing is published on the gate's host.
    `allowed` is the gate's network guard's port set and `pods` its cleanup
    set; the holder carries the pod's `labels`, so a killed run's holder is
    reaped with its pod. `started` is the holder's docker run, or what stopped
    it."""

    def __init__(self, image: str, *, name: str, labels: dict, app_port: int,
                 gate_ports, add_hosts=(), allowed: set, pods: set):
        self.name = name + "-net"
        self.app_sock = ""
        self.dir = tempfile.mkdtemp(prefix="pn-")
        self.bridges: list[_UnixBridge] = []
        ports = sorted({int(p) for p in gate_ports})
        for leaf in ("app", *map(str, ports)):
            path = os.path.join(self.dir, leaf)
            if len(path.encode()) > 100:           # sun_path holds 108 bytes
                self.started = subprocess.CompletedProcess([], 1, "", f"socket path too long: {path}")
                return
        for p in ports:
            allowed.add(p)                         # the bridge connects to it
            self.bridges.append(_UnixBridge(os.path.join(self.dir, str(p)), p))
        argv = ["run", "-d", "--name", self.name, "--user", "0:0", "-v", f"{self.dir}:/gate-net"]
        for h in add_hosts:
            argv += ["--add-host", h]
        for k, v in labels.items():
            argv += ["--label", f"{k}={v}"]
        pods.add(self.name)
        self.started = docker(*argv, image, "python3", "-c", POD_NET_FORWARDER,
                              str(app_port), *map(str, ports), timeout=120)
        if self.started.returncode != 0:
            return
        deadline = time.monotonic() + 30
        while "forwarding" not in docker("logs", self.name).stdout:
            if time.monotonic() > deadline:
                self.started = subprocess.CompletedProcess(
                    argv, 1, "", "the pod's network holder never started forwarding: "
                    + (docker("logs", self.name).stderr or "")[-500:])
                return
            time.sleep(0.2)
        self.app_sock = os.path.join(self.dir, "app")

    def health(self):
        """The app's /health as JSON, asked through the holder, or None."""
        if not self.app_sock:
            return None
        conn = _UnixHTTP(self.app_sock, timeout=2)
        try:
            conn.request("GET", "/health")
            r = conn.getresponse()
            return json.loads(r.read()) if 200 <= r.status < 300 else None
        except Exception:  # noqa: BLE001
            return None
        finally:
            conn.close()

    def close(self, pods: set) -> None:
        docker("rm", "-f", self.name, timeout=120)
        pods.discard(self.name)
        for b in self.bridges:
            b.close()
        shutil.rmtree(self.dir, ignore_errors=True)


# The processes of a pod's container, read inside it: `docker top` needs the
# daemon's cgroups and the host's /proc shows the gate only processes it may
# see, and on the CI runner it has neither (rootless Docker with no cgroups,
# and ProtectProc=invisible over uid-mapped container users). Prints
# "<runpy|connector> VmHWM: <kB> kB VmRSS: <kB> kB" for run.py and the
# connector, chosen by their second argument so this script's own command line
# never matches.
_POD_PROCESSES = r"""for d in /proc/[0-9]*; do
  a=$(tr '\0' '\n' < "$d/cmdline" 2>/dev/null | sed -n 2p)
  case "$a" in */run.py) k=runpy;; connector.py) k=connector;; *) continue;; esac
  echo "$k $(grep -E '^(VmHWM|VmRSS):' "$d/status" 2>/dev/null | tr -s ' \t\n' ' ')"
done"""


def pod_processes(name: str) -> dict:
    """{"runpy_hwm_kib", "runpy_rss_kib", "connector_hwm_kib", ...} for the
    pod's run.py and connector, as their own /proc in the container says."""
    out: dict = {}
    for line in docker("exec", name, "sh", "-c", _POD_PROCESSES).stdout.splitlines():
        key, _, rest = line.partition(" ")
        for field, label in (("VmHWM:", "hwm"), ("VmRSS:", "rss")):
            m = re.search(re.escape(field) + r"\s*(\d+)", rest)
            if m:
                out[f"{key}_{label}_kib"] = int(m.group(1))
    return out


def limit_applied(name: str, limit_mi: int) -> tuple[bool, str]:
    """Is the pod's container held to `limit_mi` MiB, as the kernel sees it
    (its cgroup's memory.max)? A Docker that runs without cgroups (rootless
    with no systemd: the CI runner's, as measured, cgroup driver
    "none") takes --memory and applies nothing, and an OOM verdict there
    would mean nothing."""
    want = limit_mi * 1024 * 1024
    got = docker("exec", name, "cat", "/sys/fs/cgroup/memory.max").stdout.strip()
    if got == str(want):
        return True, got
    info = docker("info", "--format", "cgroup driver {{.CgroupDriver}}; {{json .Warnings}}")
    return False, (f"the container's memory.max is {got or '(unreadable)'}, not {want}; "
                   f"the Docker daemon reports {(info.stdout or info.stderr).strip()[:400]}")


# The CI runner's Docker applies no memory limit (rootless, cgroup driver
# "none", as measured), so a pod's OOM verdict cannot be produced
# there. .gitea/workflows/check.yml sets this variable, and only there: part
# M still runs each pod fully and every other check, and says on its own line
# and in the gate's last line that the memory verdict was NOT RUN. Anywhere
# the variable is unset (pre_merge_gates.sh on the build machine), a pod not held to its
# limit fails the gate.
NO_CGROUP_LIMITS_ENV = "CSERIES_GATE_DOCKER_NO_CGROUP_LIMITS"
NOT_RUN: list[str] = []


def limit_held(held: bool, why: str, limit_mi: int, check_fn=None) -> bool:
    """Check the pod's container is held to `limit_mi` MiB. False stops part
    M. Where the CI variable is set and the limit is not applied, the memory
    verdict is reported NOT RUN and part M goes on."""
    check_fn = check_fn or check
    label = f"M the pod's container is held to its {limit_mi} MiB limit (its cgroup's memory.max)"
    if held or os.environ.get(NO_CGROUP_LIMITS_ENV) != "1":
        check_fn(label, held, why)
        return held
    if not NOT_RUN:
        NOT_RUN.append(f"the pods' {limit_mi} MiB memory verdict (not run on this Docker: no cgroup limits)")
    print(f"  NOT RUN  {label}: not run on this Docker (no cgroup limits; "
          f"{NO_CGROUP_LIMITS_ENV}=1)   [{why}]")
    return True


def memory_verdict(case: str, limit_mi: int, m: dict, check_fn=None) -> None:
    """The verdict on one part M case: the pod was not OOM-killed under its
    limit and the connector still serves. Where the limit is not applied (see
    limit_held), only "still serves" is checked, and the label says so."""
    check_fn = check_fn or check
    serves = m["running"] and m["serving"] and "connector_hwm_kib" in m
    if NOT_RUN:
        check_fn(f"M {case}: the connector still serves (its {limit_mi} MiB memory verdict: NOT RUN "
                 f"on this Docker, no cgroup limits)", serves, m)
    else:
        check_fn(f"M {case}: the pod was not OOM-killed under its {limit_mi} MiB limit, and the "
                 f"connector still serves", serves and not m["oom_killed"] and m["oom_kill"] == 0, m)


def passed_line() -> str:
    """A passing gate's last line, which is what pre_merge_gates.sh prints."""
    return "ALL GATES PASSED" + (" — NOT RUN: " + "; ".join(NOT_RUN) if NOT_RUN else "")


# The kubelet's readiness probe runs every PROBE_PERIOD_S in a gate's pod. A
# kill waits up to PROBE_SETTLE_S for the probe on its way, the time the
# gates' cleanup already gives their probers.
PROBE_PERIOD_S = 5
PROBE_SETTLE_S = 30


class Pod:
    """A connector app's pod as the operator renders it: the app's container
    from the runtime image, uid 1000, the app's files at /app-src (the init
    step's emptyDir), its appdata volume at /var/lib/cseries/appdata/<app>, and
    the command `python3 /opt/app-runtime/run.py -- python3 connector.py` with
    the env the operator gives it; this tree's run.py is mounted over the
    image's. It joins its PodNet, where 127.0.0.1:`proxy_port` is the gate's
    Host and each of `gate_ports` the gate's port of that number. The kubelet's
    readiness probe (the operator's exec of curl against the app's ready_path)
    runs every 5 s, in the container. `extra_args` are docker run arguments a
    platform's fake needs (a mounted test CA); `add_hosts` go to the pod's
    /etc/hosts."""

    def __init__(self, image: str, *, root: pathlib.Path, app: str, kind: str, cap: str,
                 proxy_port: int, state_dir: str, limit_mi: int, source: pathlib.Path,
                 extra_args=(), env=None, gate_ports=(), add_hosts=()):
        from kernel_schema import maker_labels
        self.port = free_port()
        self.name = f"{kind}-{os.getpid()}-{self.port}"
        self._stop = asyncio.Event()
        self._killed = False
        self._prober: asyncio.Task | None = None
        self._logs: str | None = None
        self.probes: list[int] = []
        labels = maker_labels(kind)
        self.net = PodNet(image, name=self.name, labels=labels, app_port=self.port,
                          gate_ports=(proxy_port, *gate_ports), add_hosts=add_hosts,
                          allowed=ALLOWED_PORTS, pods=PODS)
        if self.net.started.returncode != 0:
            self.started = self.net.started
            return
        argv = ["run", "-d", "--name", self.name, "--network", f"container:{self.net.name}",
                f"--memory={limit_mi}m", f"--memory-swap={limit_mi}m", "--user", "1000:1000",
                "-w", "/app-src", "-v", f"{source}:/app-src:ro",
                "-v", f"{state_dir}:/var/lib/cseries/appdata/{app}",
                "-v", f"{root / 'sidecars' / 'app-runtime-python' / 'run.py'}:/opt/app-runtime/run.py:ro",
                "-e", f"PORT={self.port}", "-e", f"PRIVATAE_APP_ID={app}",
                "-e", f"CSERIES_KERNEL_URL=http://127.0.0.1:{proxy_port}",
                "-e", f"CSERIES_CONNECTOR_CAP={cap}", "-e", "HOME=/tmp/home",
                "-e", "POD_IP=127.0.0.1", "-e", "APP_RUNTIME_SOURCE_TOKEN=gate-source-token",
                *extra_args]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        for k, v in labels.items():
            argv += ["--label", f"{k}={v}"]
        PODS.add(self.name)
        self.started = docker(*argv, image, "python3", "/opt/app-runtime/run.py", "--",
                              "python3", "connector.py", timeout=120)

    def health(self):
        return self.net.health()

    def limit_applied(self, limit_mi: int) -> tuple[bool, str]:
        return limit_applied(self.name, limit_mi)

    def probing(self) -> asyncio.Task:
        """Start the kubelet's probe in it, every PROBE_PERIOD_S; kill() waits
        for this task."""
        self._prober = asyncio.create_task(self._probe_forever())
        return self._prober

    async def _probe_forever(self) -> None:
        # The operator's probe command, verbatim but for the port and path.
        cmd = ('case "$POD_IP" in *:*) H="[$POD_IP]";; *) H="$POD_IP";; esac; '
               f'curl --noproxy "*" -gfsS -m 3 "http://$H:{self.port}/health"')
        while True:                              # at least one probe, however soon the kill
            p = await asyncio.create_subprocess_exec(
                "docker", "exec", self.name, "sh", "-c", cmd,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await p.wait()
            if not self._killed:                 # a probe the container's removal cut short is not one
                self.probes.append(rc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), PROBE_PERIOD_S)
            if self._stop.is_set():
                return

    async def kill(self) -> None:
        """The gate's own kill of the pod: no new probe starts, the probe on
        its way comes back from the live container, then stop(). A kill that
        cut the only probe short left the pod with none, and check M read that
        as a pod whose probe never answered. A probe still out after
        PROBE_SETTLE_S is a failed one: the kill goes ahead and the pod shows
        no answer for it."""
        self._stop.set()
        if self._prober is not None:
            await asyncio.wait({self._prober}, timeout=PROBE_SETTLE_S)
        await asyncio.to_thread(self.stop)

    def read(self, path: str) -> str:
        r = docker("exec", self.name, "cat", path)
        return r.stdout if r.returncode == 0 else ""

    def measure(self) -> dict:
        """The container's cgroup (now, peak, OOM kills) and the peak and
        current resident set of run.py and the connector in it, in KiB; and
        whether it is up."""
        out: dict = {}
        with contextlib.suppress(ValueError):
            out["peak_kib"] = int(self.read("/sys/fs/cgroup/memory.peak").strip() or 0) // 1024
        with contextlib.suppress(ValueError):
            out["current_kib"] = int(self.read("/sys/fs/cgroup/memory.current").strip() or 0) // 1024
        stat = dict(ln.split() for ln in self.read("/sys/fs/cgroup/memory.stat").splitlines()
                    if len(ln.split()) == 2)
        with contextlib.suppress(ValueError):
            out["anon_kib"] = int(stat.get("anon", "0")) // 1024
            out["file_kib"] = int(stat.get("file", "0")) // 1024
        events = dict(ln.split() for ln in self.read("/sys/fs/cgroup/memory.events").splitlines()
                      if len(ln.split()) == 2)
        out["oom_kill"] = int(events.get("oom_kill", "-1"))
        out.update(pod_processes(self.name))
        st = docker("inspect", "-f", "{{.State.Running}} {{.State.OOMKilled}} {{.State.ExitCode}}",
                    self.name).stdout.split()
        out["running"], out["oom_killed"] = st[:1] == ["true"], st[1:2] == ["true"]
        out["exit_code"] = st[2] if len(st) > 2 else "?"
        return out

    def logs(self) -> str:
        if self._logs is not None:
            return self._logs
        r = docker("logs", self.name)
        return (r.stdout or "") + (r.stderr or "")

    def stop(self) -> None:
        """Kill it (SIGKILL, as a pod is killed), keeping what it printed.
        The gate's cases kill through kill(); this is the cleanup's."""
        if self._logs is None:
            self._stop.set()
            self._logs = self.logs()
            self._killed = True
            docker("rm", "-f", self.name, timeout=120)
            PODS.discard(self.name)
            self.net.close(PODS)


def no_database() -> int:
    lane = os.environ.get("GATE_LANE")
    if lane:
        print(f"FAIL — the {lane} lane gave no test server (TEST_DSN/PEER_TEST_DSN) and docker is "
              "unavailable; this gate never writes to a database it did not create.")
        return 1
    print("SKIP — no test server and docker unavailable; this gate drives real Host turns "
          "against a database it creates, and a skip proves nothing.")
    return 2
