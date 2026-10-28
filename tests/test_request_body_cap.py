"""Every request body the Host's app reads is read under a cap
(server/middleware/body_cap.py RequestBodyCap), and a move plan too large to run
is refused when it is proposed (server/network/nac_propose.py validate_moves).

THE HOLE THIS CLOSES. About 110 routes outside server/routes/network.py read their
body whole with no limit (`await request.json()`, `.body()`, `.form()`, or a
pydantic / Body() parameter FastAPI reads before the route runs). A body sent
chunked has no Content-Length to check first, so a caller could make the Host hold
as much as it cared to send. One middleware now caps every route, with a cap per
route where the real senders send more than a control message.

WHAT IS REAL HERE. The REAL middleware in front of a real FastAPI app whose routes
read their bodies the unbounded ways (request.json(), request.body(), request.form(),
a pydantic body, a route that swallows the read's error), with a BaseHTTPMiddleware
between them as server/main.py has. Driven straight through the ASGI interface with
`receive` counted (which shows a body refused WITHOUT being read), and behind a real
uvicorn from a raw socket. The guard tests import the REAL server/main.py app in a
child process (its import mounts every router) and read the routes off it.

MUTATION (run before shipping):
  - Take app.add_middleware(RequestBodyCap) out of server/main.py ->
    test_main_app_reads_every_body_under_the_cap fails.
  - Move it inside the CEIGAS gate, the app-token middleware or a BaseHTTPMiddleware ->
    the same test fails.
  - Drop a ROUTE_CAPS entry (e.g. /api/brain/context/upload) -> the route's own cap
    is above the default: test_no_route_reads_more_than_the_middleware_lets_through
    and test_upload_routes_accept_their_real_maximum fail.
  - Lower the default below 1 MiB, or an entry below its source ->
    test_upload_routes_accept_their_real_maximum fails.
  - Rename a route an entry names -> test_every_route_cap_names_a_real_route fails.
  - In RequestBodyCap, call the app before checking Content-Length ->
    test_declared_oversize_is_refused_unread fails (receive is called).
  - Stop counting in capped_receive -> the chunked tests fail.
  - Remove the size check from validate_moves -> test_a_plan_too_large_to_run_is_refused
    fails.
  - Drop the /api/clipboard/write or /api/integrations/connections/{connection_id}/call
    entry -> test_free_form_content_is_not_held_to_the_control_message_default and
    test_free_form_content_past_the_hosts_memory_is_refused_413 fail for it.

Run: pytest tests/test_request_body_cap.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from server.middleware import body_cap as BC  # noqa: E402

MIB = 1 << 20
KIB = 1 << 10
CHUNK = 64 * KIB          # what uvicorn hands an app per receive, at most


# ── a stand-in app: every unbounded way a route reads a body ─────────────────

class _Spec(BaseModel):
    name: str


def _toy(reached: list) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")          # a BaseHTTPMiddleware between, as in server/main.py
    async def _between(request, call_next):
        return await call_next(request)

    @app.post("/api/x/json")
    async def as_json(request: Request):
        body = await request.json()
        reached.append("json")
        return {"keys": len(body)}

    @app.post("/api/x/body")
    async def as_body(request: Request):
        n = len(await request.body())
        reached.append("body")
        return {"n": n}

    @app.post("/api/x/form")
    async def as_form(request: Request):
        form = await request.form()
        reached.append("form")
        return {"fields": len(form)}

    @app.post("/api/x/model")
    async def as_model(spec: _Spec):
        reached.append("model")
        return {"name": spec.name[:10]}

    @app.post("/api/x/swallow")
    async def swallows(request: Request):
        try:
            await request.json()
        except Exception:     # noqa: BLE001 — the pattern under test
            pass
        reached.append("swallow")
        return {"ok": True}

    return app


class _Counted:
    """Outermost: counts the body bytes the stack below took from the socket."""

    def __init__(self, app):
        self.app, self.delivered = app, 0

    async def __call__(self, scope, receive, send):
        async def counted():
            msg = await receive()
            self.delivered += len(msg.get("body", b"") or b"")
            return msg
        return await self.app(scope, counted if scope["type"] == "http" else receive, send)


CAP = 1 * MIB


def _stack(reached, cap=CAP, routes=()):
    return _Counted(BC.RequestBodyCap(_toy(reached), default=cap, routes=routes))


class _Wire:
    def __init__(self):
        self.receive_calls = 0
        self.status = None
        self.headers = {}
        self.body = b""


def _call(app, path, *, headers, chunks, method="POST") -> _Wire:
    wire = _Wire()
    it = iter(chunks)
    ended = False
    answered = asyncio.Event()

    async def receive():
        nonlocal ended
        if ended:
            # As a server does: past the body, receive waits for the client to
            # go, here once the answer is sent (StreamingResponse listens for it).
            await answered.wait()
            return {"type": "http.disconnect"}
        wire.receive_calls += 1
        try:
            return {"type": "http.request", "body": next(it), "more_body": True}
        except StopIteration:
            ended = True
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            wire.status = msg["status"]
            wire.headers = {k.decode().lower(): v.decode() for k, v in msg.get("headers") or ()}
        elif msg["type"] == "http.response.body":
            wire.body += msg.get("body", b"")
            if not msg.get("more_body"):
                answered.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), *[(k.encode(), v.encode()) for k, v in headers]]}
    # A private loop, never set as the thread's (tests/conftest.py: asyncio.run()
    # leaves the main thread without one on 3.14, which breaks other files).
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return wire


def _forever(total: int, n: int = CHUNK):
    block = b"x" * n
    left = total
    while left > 0:
        take = min(n, left)
        yield block[:take]
        left -= take


def _multipart(total: int, n: int = CHUNK):
    head = (b'--zz\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n')
    yield head
    yield from _forever(total - len(head), n)


_CT = {"json": "application/json", "body": "application/octet-stream",
       "form": "multipart/form-data; boundary=zz", "model": "application/json",
       "swallow": "application/json"}


def _chunks_for(kind, total):
    return _multipart(total) if kind == "form" else _forever(total)


# ── 1. a declared Content-Length over the cap: refused before a byte is read ─

@pytest.mark.parametrize("kind", list(_CT))
def test_declared_oversize_is_refused_unread(kind):
    reached = []
    app = _stack(reached)
    wire = _call(app, f"/api/x/{kind}", headers=[("content-type", _CT[kind]),
                                                 ("content-length", str(CAP + 1))],
                 chunks=_chunks_for(kind, CAP + 1))
    assert wire.status == 413, (wire.status, wire.body)
    assert wire.receive_calls == 0, f"the body was read ({wire.receive_calls} reads) before the refusal"
    assert app.delivered == 0
    assert reached == []
    detail = json.loads(wire.body)["detail"]
    assert "1 MiB" in detail and "platform" not in detail.lower(), detail
    assert wire.headers.get("connection") == "close"


# ── 2. a chunked body is stopped within one chunk of the cap ─────────────────

@pytest.mark.parametrize("kind", list(_CT))
def test_chunked_oversize_is_stopped_at_the_cap(kind):
    reached = []
    app = _stack(reached)
    wire = _call(app, f"/api/x/{kind}", headers=[("content-type", _CT[kind]),
                                                 ("transfer-encoding", "chunked")],
                 chunks=_chunks_for(kind, 64 * CAP))
    assert wire.status == 413, (wire.status, wire.body[:200])
    assert app.delivered <= CAP + CHUNK, f"took {app.delivered} bytes of a body capped at {CAP}"
    if kind == "swallow":
        # The route caught the refusal and answered 200 on: its answer is replaced,
        # so the caller is never told a refused body was served.
        assert reached == ["swallow"]
    else:
        assert reached == [], f"a route ran on a refused body: {reached}"
    assert "1 MiB" in json.loads(wire.body)["detail"]
    assert wire.headers.get("connection") == "close"


def test_a_body_of_exactly_the_cap_is_served_and_one_byte_more_is_not():
    reached = []
    wire = _call(_stack(reached), "/api/x/body", headers=[("content-length", str(CAP))],
                 chunks=_forever(CAP))
    assert wire.status == 200 and json.loads(wire.body) == {"n": CAP}, wire.body[:200]
    wire = _call(_stack(reached), "/api/x/body", headers=[("transfer-encoding", "chunked")],
                 chunks=_forever(CAP + 1))
    assert wire.status == 413, wire.status
    assert reached == ["body"]


def test_a_route_cap_overrides_the_default_both_ways():
    routes = [BC._cap("POST", "/api/x/body", 4 * MIB, "test"),
              BC._cap("POST", "/api/x/json", 64 * KIB, "test")]
    reached = []
    wire = _call(_stack(reached, routes=routes), "/api/x/body",
                 headers=[("content-length", str(4 * MIB))], chunks=_forever(4 * MIB))
    assert wire.status == 200, wire.status
    wire = _call(_stack(reached, routes=routes), "/api/x/json",
                 headers=[("content-type", "application/json"), ("content-length", str(64 * KIB + 1))],
                 chunks=_forever(64 * KIB + 1))
    assert wire.status == 413 and "64 KiB" in wire.body.decode(), wire.body
    # A method the entry does not name gets the default.
    assert BC.RequestBodyCap(None, routes=routes).cap_for("PUT", "/api/x/body") == BC.DEFAULT_CAP


def test_a_request_with_a_bad_content_length_is_refused_unread():
    for value in ("12x", "-1", "1, 2"):
        wire = _call(_stack([]), "/api/x/body", headers=[("content-length", value)],
                     chunks=_forever(10))
        assert wire.status == 400 and wire.receive_calls == 0, (value, wire.status)


def test_websockets_and_lifespan_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = BC.RequestBodyCap(inner)
    loop = asyncio.new_event_loop()
    try:
        for t in ("websocket", "lifespan"):
            loop.run_until_complete(mw({"type": t, "path": "/ws", "headers": []}, None, None))
    finally:
        loop.close()
    assert seen == ["websocket", "lifespan"]


# ── 3. behind a real uvicorn, from a raw socket ──────────────────────────────

class _Server:
    def __init__(self, app):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                                    lifespan="off", ws="none"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(15)


def _read_answer(sock) -> tuple[int, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        part = sock.recv(65536)
        if not part:
            break
        buf += part
    head, _, rest = buf.partition(b"\r\n\r\n")
    size = 0
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            size = int(v)
    while len(rest) < size:
        part = sock.recv(65536)
        if not part:
            break
        rest += part
    return int(head.split(b" ", 2)[1]), head + b"\r\n\r\n" + rest


def test_real_server_answers_a_declared_oversize_before_the_body_is_sent():
    reached = []
    app = _stack(reached, cap=32 * MIB)
    with _Server(app) as srv, socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
        s.sendall((f"POST /api/x/json HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {32 * MIB + 1}\r\n\r\n").encode() + b'{"name":"a"')
        # 11 bytes of a body declared as 32 MiB + 1, and no more.
        status, raw = _read_answer(s)
    assert status == 413, raw[:300]
    assert b"32 MiB" in raw and b"connection: close" in raw.lower()
    assert app.delivered == 0, app.delivered
    assert reached == []


def test_real_server_stops_a_chunked_body_at_the_cap():
    reached = []
    cap = 8 * MIB
    app = _stack(reached, cap=cap)
    chunk = b"x" * MIB
    frame = b"%x\r\n" % len(chunk) + chunk + b"\r\n"
    status, raw, sent = None, b"", 0
    with _Server(app) as srv, socket.create_connection(("127.0.0.1", srv.port), timeout=10) as s:
        s.sendall(b"POST /api/x/body HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
        s.setblocking(False)
        deadline = time.monotonic() + 20
        pending = memoryview(b"")
        while status is None and sent < 8 * cap and time.monotonic() < deadline:
            if not pending:
                pending = memoryview(frame)
            try:
                pending = pending[s.send(pending):]
                if not pending:
                    sent += len(chunk)
            except BlockingIOError:
                time.sleep(0.005)
            except ConnectionError:
                break
            try:
                part = s.recv(65536)
                if part:
                    raw += part
                    if b"\r\n\r\n" in raw:
                        status = int(raw.split(b" ", 2)[1])
            except BlockingIOError:
                pass
            except ConnectionError:
                break
    assert status == 413, (status, sent, raw[:200])
    # uvicorn hands an app what one socket read parsed, so a server chunk may be larger
    assert app.delivered <= cap + MIB, f"the server took {app.delivered} bytes of an {cap}-byte cap"
    assert reached == []


# ── 4. a move plan too large to run is refused when it is proposed ───────────

def _plan(n: int, path_len: int) -> list:
    return [{"from": f"{i:06d}/" + "a" * path_len, "to": f"Archive/{i:06d}/" + "a" * path_len,
             "size_bytes": 123456789} for i in range(n)]


_STORE = dict(entity_id=9003, store_id=12, server="box1", root="/home/someone/Documents", title="Reorganize")


def _real_apply_frame(moves) -> int:
    """The frame run_move_plan would send for these moves once approved on the box,
    built by the same functions it uses, with a box approval of the real shape."""
    from server.network import nac_dcn, nac_mgmt, nac_stepup
    terms = {"kind": "dcn_move_plan", "entity": _STORE["entity_id"], "store_id": _STORE["store_id"],
             "plan_id": 4242, "server": _STORE["server"], "root": _STORE["root"], "archive": "Archive",
             "moves": moves, "expires": "2026-10-28T04:44:58+00:00"}
    approval = json.dumps({"v": 1, "purpose": nac_stepup.purpose_for_plan_terms(terms), "nonce": "0" * 32,
                           "expires": "2026-10-28T05:44:58Z", "uses": 1, "key": "K" * 44, "sig": "S" * 88})
    terms.update(approval=approval, approval_where="box")
    payload = nac_dcn.plan_apply_payload(terms, "G" * 88, "dry_run")
    return len(nac_mgmt.job_frame("dcn_plan_apply", [str(_STORE["store_id"]), payload]))


def test_a_plan_too_large_to_run_is_refused():
    from server.network import nac_mgmt, nac_propose
    big = _plan(5000, 700)          # ~7 MiB of moves: 12.4 MiB once packed for the box
    assert _real_apply_frame(big) > nac_mgmt.MGMT_FRAME_MAX_BYTES, "the fixture must not fit"
    with pytest.raises(ValueError) as e:
        nac_propose.validate_moves(big, "Archive", **_STORE)
    words = str(e.value)
    assert "too large to run" in words and "12 MiB" in words and "Split" in words, words
    assert "platform" not in words.lower()


def test_a_plan_that_fits_is_still_accepted():
    from server.network import nac_mgmt, nac_propose
    fits = _plan(5000, 640)         # ~6.4 MiB of moves: under the frame once packed
    assert _real_apply_frame(fits) <= nac_mgmt.MGMT_FRAME_MAX_BYTES
    assert len(nac_propose.validate_moves(fits, "Archive", **_STORE)) == 5000
    # The shape checks still come first and still stand on their own.
    assert nac_propose.validate_moves([{"from": "a", "to": "Archive/a"}], "Archive") == [
        {"from": "a", "to": "Archive/a"}]


def test_the_plan_bound_sits_just_above_the_largest_plan_that_runs():
    # The refusal is not a guess well below the frame: walk the path length until the
    # real frame passes 12 MiB, and the bound refuses there and not more than one
    # step (5000 x 2 bytes of path) before.
    from server.network import nac_mgmt, nac_propose
    lo, hi = 600, 720
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _real_apply_frame(_plan(5000, mid)) <= nac_mgmt.MGMT_FRAME_MAX_BYTES:
            lo = mid
        else:
            hi = mid
    nac_propose.validate_moves(_plan(5000, lo - 2), "Archive", **_STORE)
    with pytest.raises(ValueError):
        nac_propose.validate_moves(_plan(5000, hi), "Archive", **_STORE)


def test_propose_refuses_a_plan_too_large_before_it_is_filed():
    from server.network import nac_propose

    class _Pool:
        def __init__(self):
            self.inserts = 0

        async def fetchrow(self, *a):
            return {"id": 12, "class": "machine", "server": "box1", "root": "/home/someone/Documents",
                    "status": "granted"}

        async def fetchval(self, *a):
            self.inserts += 1
            return 1

    pool = _Pool()
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError, match="too large to run"):
            loop.run_until_complete(nac_propose.propose_dcn_move_plan(
                pool, 9003, store_id=12, moves=_plan(5000, 700)))
        assert pool.inserts == 0, "the plan was filed"
        res = loop.run_until_complete(nac_propose.propose_dcn_move_plan(
            pool, 9003, store_id=12, moves=_plan(10, 20)))
    finally:
        loop.close()
    assert res["status"] == "draft" and res["move_count"] == 10 and pool.inserts == 1


# ── 5. the class: every route of the real app reads its body under the cap ──

# Run in a child: importing server/main.py mounts every router, and other files in
# this lane stand a stub in for server.main in sys.modules. The dashboard directory
# StaticFiles checks for exists only in the image, so the check is skipped.
_CHILD = textwrap.dedent(r'''
    import ast, inspect, json, sys
    sys.path[:0] = [sys.argv[1], sys.argv[1] + "/brain"]
    import starlette.staticfiles as SF
    _init = SF.StaticFiles.__init__
    def _no_dir_check(self, *a, **kw):
        kw["check_dir"] = False
        _init(self, *a, **kw)
    SF.StaticFiles.__init__ = _no_dir_check
    import server.main as M
    from fastapi.routing import APIRoute

    def flat(routes, prefix=""):
        # FastAPI 0.133 (the image) copies an included router's routes; later
        # versions keep the router and include it lazily.
        for r in routes:
            if type(r).__name__ == "_IncludedRouter":
                yield from flat(r.original_router.routes, prefix + (r.include_context.prefix or ""))
            else:
                yield prefix, r

    READERS = ("read_request", "read_request_json", "_json_in", "_body")

    def module_funcs(mod):
        try:
            tree = ast.parse(inspect.getsource(mod))
        except (OSError, TypeError):
            return {}
        return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def own_caps(fn, mod, funcs, seen):
        """The cap of every bounded read `fn` makes, itself or through a helper of
        its own module: the second argument of read_request & co, or the helper's
        default. A cap held in a local is evaluated from its assignment."""
        ns = dict(vars(mod))
        for n in ast.walk(fn):           # imports made inside the function
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                try:
                    exec(compile(ast.Module([n], []), "<import>", "exec"), ns)
                except Exception:
                    pass
        local = {n.targets[0].id: n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}

        def ev(expr):
            if isinstance(expr, ast.Name) and expr.id in local and expr.id not in ns:
                expr = local[expr.id]
                if isinstance(expr, ast.IfExp):
                    return max(ev(expr.body), ev(expr.orelse))
            return eval(compile(ast.Expression(expr), "<cap>", "eval"), ns)

        out = []
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name in READERS:
                if len(n.args) > 1:
                    out.append(ev(n.args[1]))
                elif name in funcs:
                    a = funcs[name].args
                    pos = [*a.posonlyargs, *a.args]
                    out.append(ev(a.defaults[1 - (len(pos) - len(a.defaults))]))
            elif isinstance(n.func, ast.Name) and name in funcs and name not in seen:
                out += own_caps(funcs[name], mod, funcs, seen | {name})
        return out

    routes = []
    for prefix, r in flat(M.app.routes):
        if not isinstance(r, APIRoute):
            continue
        mod = sys.modules[r.endpoint.__module__]
        funcs = module_funcs(mod)
        node = funcs.get(r.endpoint.__name__)
        routes.append({"methods": sorted(r.methods - {"HEAD"}), "path": prefix + r.path,
                       "where": f"{r.endpoint.__module__}.{r.endpoint.__name__}",
                       "caps": own_caps(node, mod, funcs, {node.name}) if node else []})
    from server.routes import appearance, applications, connector, context, designs, mcp
    from server.peer import app_lane
    from server.capabilities import file_access
    consts = {"MAX_FILE_UPLOAD_BYTES": context.MAX_FILE_UPLOAD_BYTES,
              "APPEARANCE_MAX_SIZE_BYTES": appearance.MAX_SIZE_BYTES,
              "MAX_DESIGN_BYTES": designs.MAX_DESIGN_BYTES,
              "_BUNDLE_ZIP_MAX": applications._BUNDLE_ZIP_MAX,
              "CONFIG_BODY_MAX": applications.CONFIG_BODY_MAX,
              "_PLATFORM_BODY_MAX": connector._PLATFORM_BODY_MAX,
              "_MCP_MAX_BODY_BYTES": mcp._MCP_MAX_BODY_BYTES,
              "MAX_PAYLOAD_BYTES": app_lane.MAX_PAYLOAD_BYTES,
              "MAX_WRITE_BYTES": file_access.MAX_WRITE_BYTES}
    print(json.dumps({"stack": [getattr(m.cls, "__name__", str(m.cls)) for m in M.app.user_middleware],
                      "routes": routes, "consts": consts}))
''')


@pytest.fixture(scope="module")
def real_app():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-I", "-c", _CHILD, str(_ROOT)], capture_output=True,
                       text=True, timeout=240, env=env, cwd=str(_ROOT))
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _sample(template: str) -> str:
    """A concrete path a route template matches."""
    import re
    return re.sub(r"\{[^}:]+:path\}", "x1/y1", re.sub(r"\{[^}:]+(:(str|int|float|uuid))?\}", "1", template))


def test_main_app_reads_every_body_under_the_cap(real_app):
    stack = real_app["stack"]          # outermost first
    assert "RequestBodyCap" in stack, f"server/main.py does not install the body cap: {stack}"
    outside = stack[:stack.index("RequestBodyCap")]
    # Only these may sit outside it: they read headers and the path, never the body,
    # and refuse without reading it (FrontDoorProof strips identity headers).
    assert outside == ["HostHeaderGuard", "AppOriginGuard", "FrontDoorProof"], (
        f"the body cap must sit outside every middleware that may read a body; outside it: {outside}")


def test_every_route_cap_names_a_real_route(real_app):
    have = {}
    for r in real_app["routes"]:
        have.setdefault(r["path"], set()).update(r["methods"])
    stale = [f"{sorted(rc.methods)} {rc.path}" for rc in BC.ROUTE_CAPS
             if not rc.methods <= have.get(rc.path, set())]
    assert not stale, f"ROUTE_CAPS names routes the app does not have (renamed?): {stale}"


def test_no_route_reads_more_than_the_middleware_lets_through(real_app):
    # A route that reads under its own cap (network.py _json_in, harness_input.read_request,
    # connector._body) must get at least that cap from the middleware, or the middleware
    # cuts off what the route was built to accept. A new route with a large cap of its own
    # fails here until ROUTE_CAPS names it.
    mw = BC.RequestBodyCap(None)
    seen, low = 0, []
    for r in real_app["routes"]:
        if not r["caps"]:
            continue
        seen += 1
        for m in r["methods"]:
            got = mw.cap_for(m, _sample(r["path"]))
            if got is not None and got < max(r["caps"]):     # None: a STREAMED route
                low.append(f"{m} {r['path']} ({r['where']}): reads up to {max(r['caps'])}, middleware {got}")
    assert seen >= 40, f"the scan found only {seen} routes with their own cap; it is not seeing them"
    assert not low, "the middleware's cap is below the route's own:\n  " + "\n  ".join(low)


def test_no_other_app_serves_http_in_the_kernel():
    # The cap holds because every HTTP route is a route of server/main.py's app. A second
    # ASGI app inside the kernel would serve its routes without it.
    import re
    pat = re.compile(r"^\s*\w+\s*=\s*(FastAPI|Starlette)\(", re.M)
    found = [str(p.relative_to(_ROOT)) for p in (_ROOT / "server").rglob("*.py")
             if pat.search(p.read_text(errors="replace"))]
    assert found == ["server/main.py"], found


# ── 6. each upload-type route still takes its real maximum ───────────────────

def _real_maxima(c: dict) -> list:
    """(method, path, the largest body a real sender sends, where it comes from),
    each computed from the constant that bounds it in the real app."""
    design = json.dumps({"name": "n" * 80, "html": "\U0001F600" * (c["MAX_DESIGN_BYTES"] // 4)})
    b64 = lambda n: 4 * -(-n // 3)                       # noqa: E731 — base64 length of n bytes
    return [
        ("POST", "/api/appearance/upload", c["APPEARANCE_MAX_SIZE_BYTES"], "a 20 MiB wallpaper"),
        ("POST", "/api/brain/context/upload", c["MAX_FILE_UPLOAD_BYTES"], "a 25 MiB file"),
        ("POST", "/api/brain/context/import", c["MAX_FILE_UPLOAD_BYTES"], "an export as items"),
        ("POST", "/api/applications/install", c["_BUNDLE_ZIP_MAX"], "the largest bundle"),
        ("POST", "/api/applications/publisher/publish", c["_BUNDLE_ZIP_MAX"] + 64 * KIB,
         "the largest bundle as a form, with its text fields"),
        ("POST", "/api/designs", len(design.encode()), "4 MiB of emoji html, ASCII-escaped"),
        ("POST", "/api/files/write", b64(c["MAX_WRITE_BYTES"]) + 256, "a 50 MiB file as base64"),
        ("POST", "/api/codespace/file", c["MAX_WRITE_BYTES"] + 256, "a 50 MiB source file"),
        ("POST", "/api/connector/platform", c["_PLATFORM_BODY_MAX"], "the connector's own cap"),
        ("POST", "/mcp", c["_MCP_MAX_BODY_BYTES"], "the route's own cap"),
        # A message to a peer's copy of an app: its payload is at most
        # app_lane.MAX_PAYLOAD_BYTES as compact JSON, inside the default.
        ("POST", "/api/peer/app/send", c["MAX_PAYLOAD_BYTES"] + 4 * KIB, "the largest payload"),
        ("PUT", "/api/applications/app1/config/key1", c["CONFIG_BODY_MAX"], "the route's own cap"),
        ("POST", "/api/applications/app1/runtime-log", 4 * MIB, "the route's own cap"),
        ("POST", "/api/circles/messages", BC._PROVIDER_REQUEST_MAX, "the largest provider request"),
        ("POST", "/api/brain/messages", BC._PROVIDER_REQUEST_MAX, "the largest provider request"),
        # Two files at the 25 MiB a file may be: 70 MB. Three (105 MB) pass every
        # provider's one-request limit, so no turn could carry them.
        ("POST", "/api/circles/messages", 2 * b64(c["MAX_FILE_UPLOAD_BYTES"]) + 64 * KIB,
         "two 25 MiB files"),
        ("POST", "/api/world/sql", 200_000 * 12 + 1024, "200,000 astral characters escaped"),
        ("POST", "/api/applications/sites/customers/import", 64 * MIB, "the bridge line"),
        ("POST", "/api/artifacts", 64 * MIB, "the browser check's largest page"),
        ("POST", "/api/clipboard/write", 25 * MIB, "the text of the largest file the Host takes"),
        ("POST", "/api/integrations/connections/c1/call", BC._PROVIDER_REQUEST_MAX,
         "the chat message's floor: the integrations layer sets no attachment size"),
    ]


def _streaming_counter():
    """A stand-in route that reads any body as it arrives and holds none of it."""
    got = {}

    async def app(scope, receive, send):
        n = 0
        while True:
            m = await receive()
            n += len(m.get("body") or b"")
            if not m.get("more_body"):
                break
        got["n"] = n
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app, got


def test_upload_routes_accept_their_real_maximum(real_app):
    c = real_app["consts"]
    mw = BC.RequestBodyCap(None)
    for method, path, real, what in _real_maxima(c):
        cap = mw.cap_for(method, path)
        assert cap is None or cap >= real, f"{method} {path}: cap {cap} is below {what} ({real})"
    # And through the middleware, for real: each maximum is let through whole.
    for method, path, real, what in _real_maxima(c):
        inner, got = _streaming_counter()
        wire = _call(BC.RequestBodyCap(inner), path, method=method,
                     headers=[("transfer-encoding", "chunked")], chunks=_forever(real, MIB))
        assert wire.status == 200 and got["n"] == real, (method, path, what, wire.status, wire.body[:200])


def test_route_caps_match_their_source_constants(real_app):
    # The numbers in body_cap.py are copies (it imports no route); each must still be
    # the constant it names.
    c = real_app["consts"]
    assert BC._MAX_FILE_UPLOAD == c["MAX_FILE_UPLOAD_BYTES"]
    assert BC._BUNDLE_ZIP_MAX == c["_BUNDLE_ZIP_MAX"]
    assert BC._MAX_WRITE == c["MAX_WRITE_BYTES"]
    mw = BC.RequestBodyCap(None)
    assert mw.cap_for("POST", "/api/appearance/upload") == c["APPEARANCE_MAX_SIZE_BYTES"]
    assert mw.cap_for("POST", "/api/designs") >= 3 * c["MAX_DESIGN_BYTES"]
    src = (_ROOT / "sidecars/browser/control/document_verify.py").read_text()
    assert 'str(64 * 1024 * 1024)' in src, "the browser check's page limit moved: move /api/artifacts too"


def test_only_streaming_routes_go_uncapped(real_app):
    uncapped = {rc.path for rc in BC.ROUTE_CAPS if rc.cap is None}
    assert uncapped == set(BC.STREAMED), uncapped
    # Each is a real route, and each one's handler is the one that does not hold it.
    where = {r["path"]: r["where"] for r in real_app["routes"]}
    assert where["/system-apps/{app_id}/{path:path}"] == "server.applications.proxy.serve_app"
    assert where["/api/training-memory/jobs"].startswith("server.routes.training_memory.")
    assert where["/api/backup/files/upload"] == "server.routes.backup.upload_backup_file"


def test_a_backup_restore_upload_streams_its_pieces_through():
    # The owner's backup reaches the Host in pieces of CHUNK_BYTES, each written to
    # disk as it arrives (host_restore.receive_upload), so the middleware leaves the
    # route alone: a whole piece goes through, declared or chunked.
    import re
    src = (_ROOT / "system-apps/backups/restore-file.js").read_text()
    m = re.search(r"export const CHUNK_BYTES = (\d+) \* 1024 \* 1024;", src)
    assert m, "the restore upload's piece size moved: check /api/backup/files/upload"
    piece = int(m.group(1)) * MIB
    assert BC.RequestBodyCap(None).cap_for("PUT", "/api/backup/files/upload") is None
    for headers in ([("content-length", str(piece))], [("transfer-encoding", "chunked")]):
        inner, got = _streaming_counter()
        wire = _call(BC.RequestBodyCap(inner), "/api/backup/files/upload", method="PUT",
                     headers=headers, chunks=_forever(piece, MIB))
        assert wire.status == 200 and got["n"] == piece, (headers, wire.status, wire.body[:200])


# ── 7. the app proxy streams a large body on instead of holding it ───────────

class _Upstream:
    """A real server standing in for an app's backend: counts and hashes the body
    as it arrives, and keeps the headers it was sent."""

    def __init__(self):
        self.received = 0
        self.headers = {}
        self.digest = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        import hashlib
        self.headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        h = hashlib.sha256()
        while True:
            m = await receive()
            body = m.get("body") or b""
            self.received += len(body)
            h.update(body)
            if not m.get("more_body"):
                break
        self.digest = h.hexdigest()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"n": self.received}).encode()})


def _proxy_front(port):
    from server.applications import proxy as P
    app = FastAPI()

    @app.api_route("/system-apps/{app_id}/{path:path}", methods=["POST", "PUT"])
    async def forward(app_id: str, path: str, request: Request):
        return await P._serve_external(app_id, port, path, request)
    return _Counted(BC.RequestBodyCap(app))


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_the_app_proxy_streams_a_large_upload_to_the_app(declared):
    import hashlib
    total = 40 * MIB
    up = _Upstream()
    seen_mid_body = []

    def body():
        for i in range(total // MIB):
            if i == total // MIB // 2:
                seen_mid_body.append(up.received)   # what the app had when half was sent
            yield b"%c" % (65 + i % 26) * MIB

    expect = hashlib.sha256(b"".join(b"%c" % (65 + i % 26) * MIB for i in range(total // MIB))).hexdigest()
    hdrs = [("content-type", "application/octet-stream"),
            ("content-length", str(total)) if declared else ("transfer-encoding", "chunked")]
    with _Server(up) as srv:
        wire = _call(_proxy_front(srv.port), "/system-apps/someapp/files/put", headers=hdrs,
                     chunks=body())
    assert wire.status == 200, (wire.status, wire.body[:300])
    assert up.received == total and up.digest == expect, "the app did not get the body intact"
    assert seen_mid_body and seen_mid_body[0] > 0, (
        "the app had nothing when half the body was sent: the Host held the body")
    if declared:
        assert up.headers.get("content-length") == str(total), up.headers
    else:
        assert up.headers.get("transfer-encoding") == "chunked", up.headers


def test_a_small_proxy_body_is_held_as_before_and_a_started_stream_is_never_replayed():
    from starlette.requests import Request as StarletteRequest
    from server.applications import proxy as P

    def req(headers, chunks):
        it = iter(chunks)

        async def receive():
            try:
                return {"type": "http.request", "body": next(it), "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}
        return StarletteRequest({"type": "http", "method": "POST", "path": "/", "headers":
                                 [(k.encode(), v.encode()) for k, v in headers], "query_string": b""},
                                receive)

    loop = asyncio.new_event_loop()
    try:
        small = req([("content-length", "5")], [b"hello"])
        assert loop.run_until_complete(P._request_body(small)) == b"hello"
        assert loop.run_until_complete(P._request_body(small)) == b"hello"   # a replay reads it again
        assert loop.run_until_complete(P._request_body(req([], []))) == b""  # no body at all

        big = req([("content-length", str(2 * MIB))], [b"x" * MIB, b"y" * MIB])
        first = loop.run_until_complete(P._request_body(big))
        assert not isinstance(first, (bytes, bytearray))
        # A dial that failed before reading: the replay gets the whole body.
        again = loop.run_until_complete(P._request_body(big))

        async def drain(g):
            return b"".join([c async for c in g])
        assert loop.run_until_complete(drain(again)) == b"x" * MIB + b"y" * MIB
        # Once started, the body cannot be sent twice.
        with pytest.raises(P.HTTPException) as e:
            loop.run_until_complete(P._request_body(big))
        assert e.value.status_code == 502
    finally:
        loop.close()


# ── 8. what a first cut of the caps cut short ────────────────────────────────

def _call_slow(app, path, *, headers, chunks, gap: float) -> _Wire:
    """_call, with the client taking `gap` seconds over each chunk, as a person's
    upload over a slow link does."""
    wire = _Wire()
    it = iter(chunks)
    ended = False
    answered = asyncio.Event()

    async def receive():
        nonlocal ended
        if ended:
            await answered.wait()
            return {"type": "http.disconnect"}
        await asyncio.sleep(gap)
        try:
            return {"type": "http.request", "body": next(it), "more_body": True}
        except StopIteration:
            ended = True
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            wire.status = msg["status"]
        elif msg["type"] == "http.response.body":
            wire.body += msg.get("body", b"")
            if not msg.get("more_body"):
                answered.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), *[(k.encode(), v.encode()) for k, v in headers]]}
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return wire


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_a_slow_upload_through_the_app_proxy_is_not_cut_by_the_head_clock(declared, monkeypatch):
    # An app answers once it has the whole body, so its head comes after the
    # upload. The head clock (_HEAD_TIMEOUT_S) must start when the body has gone:
    # started at the dial, every upload slower than the clock got a 504 mid-body.
    from server.applications import proxy as P
    monkeypatch.setattr(P, "_HEAD_TIMEOUT_S", 0.5)
    total, n = 2 * MIB, 8
    up = _Upstream()
    hdrs = [("content-type", "application/octet-stream"),
            ("content-length", str(total)) if declared else ("transfer-encoding", "chunked")]
    with _Server(up) as srv:
        wire = _call_slow(_proxy_front(srv.port), "/system-apps/someapp/files/put", headers=hdrs,
                          chunks=(b"z" * (total // n) for _ in range(n)), gap=0.15)
    assert wire.status == 200, (wire.status, wire.body[:300])
    assert up.received == total


def test_a_proxy_app_that_never_answers_still_gets_the_head_clock_once_the_body_is_sent(monkeypatch):
    from server.applications import proxy as P
    monkeypatch.setattr(P, "_HEAD_TIMEOUT_S", 0.3)

    async def silent(scope, receive, send):        # reads the body, then never answers
        if scope["type"] != "http":
            return
        while (await receive()).get("more_body"):
            pass
        await asyncio.sleep(30)

    with _Server(silent) as srv:
        t0 = time.monotonic()
        wire = _call_slow(_proxy_front(srv.port), "/system-apps/someapp/x",
                          headers=[("content-length", str(2 * MIB))],
                          chunks=(b"q" * (MIB // 2) for _ in range(4)), gap=0.05)
        took = time.monotonic() - t0
        srv.server.force_exit = True
    assert wire.status == 504, (wire.status, wire.body[:300])
    assert took < 10, took


def test_a_chat_message_takes_what_the_hosts_memory_holds(monkeypatch):
    # The composer takes any number of files of up to 25 MiB (circles-web
    # src/lib/attachments.ts), every one is kept on the Host, and pictures are
    # fitted and documents read to text before a provider sees them
    # (attachment_parts): a provider's request size is not the message's.
    b64 = lambda n: 4 * -(-n // 3)                       # noqa: E731
    four = 4 * b64(25 * MIB) + 64 * KIB                  # four of the largest files: 140 MB
    monkeypatch.setattr(BC, "_memory_room", lambda: 4 << 30, raising=False)   # a Host with 4 GiB free
    mw = BC.RequestBodyCap(None)
    for path in ("/api/circles/messages", "/api/brain/messages"):
        cap = mw.cap_for("POST", path)
        assert cap is not None and cap >= four, (path, cap)
        inner, got = _streaming_counter()
        wire = _call(BC.RequestBodyCap(inner), path, headers=[("transfer-encoding", "chunked")],
                     chunks=_forever(four, MIB))
        assert wire.status == 200 and got["n"] == four, (path, wire.status, wire.body[:200])
    # A Host with no room left still takes the floor, and refuses past its memory.
    monkeypatch.setattr(BC, "_memory_room", lambda: 0, raising=False)
    assert mw.cap_for("POST", "/api/circles/messages") == BC._PROVIDER_REQUEST_MAX
    monkeypatch.setattr(BC, "_memory_room", lambda: None, raising=False)
    assert mw.cap_for("POST", "/api/circles/messages") == BC._PROVIDER_REQUEST_MAX
    monkeypatch.setattr(BC, "_memory_room", lambda: 3 * 200 * MIB, raising=False)
    wire = _call(BC.RequestBodyCap(_streaming_counter()[0]), "/api/circles/messages",
                 headers=[("content-length", str(200 * MIB + 1))], chunks=())
    assert wire.status == 413 and "200 MiB" in wire.body.decode(), wire.body


def test_a_proposed_change_is_not_held_to_the_control_message_default(monkeypatch):
    # routes/gitea_propose.py takes whole files, any number, no size cap (at
    # 1 MiB a file it refused a lockfile). One largest file always fits.
    monkeypatch.setattr(BC, "_memory_room", lambda: None, raising=False)
    cap = BC.RequestBodyCap(None).cap_for("POST", "/api/gitea/propose")
    assert cap >= 4 * -(-BC._MAX_WRITE // 3), cap
    monkeypatch.setattr(BC, "_memory_room", lambda: 3 << 30, raising=False)
    assert BC.RequestBodyCap(None).cap_for("POST", "/api/gitea/propose") == 1 << 30


# Two routes carry free-form content with no size of their own: a person's text for
# the clipboard (routes/clipboard.py ClipboardWriteRequest.content) and an app's call
# to a provider through a connection the Host granted it (routes/integrations.py
# call_provider, whose `body: dict` goes to the provider as it is, a mail with its
# attachments among it). The integrations layer sets no attachment size (nothing in
# server/integrations, the route, or the broker's request side; the broker's 64 KiB
# is on the response), so the call takes the chat message's floor.
_FREE_FORM = [
    pytest.param("/api/clipboard/write", 25 * MIB, id="clipboard"),
    pytest.param("/api/integrations/connections/c1/call", BC._PROVIDER_REQUEST_MAX,
                 id="integration-call"),
]


def _served(path, size, *, declared=False):
    """Send `size` bytes to `path` through the real middleware with the real caps;
    (the wire, the bytes the route read)."""
    inner, got = _streaming_counter()
    if declared:
        wire = _call(BC.RequestBodyCap(inner), path,
                     headers=[("content-length", str(size))], chunks=_forever(size, MIB))
    else:
        wire = _call(BC.RequestBodyCap(inner), path,
                     headers=[("transfer-encoding", "chunked")], chunks=_forever(size, MIB))
    return wire, got.get("n")


@pytest.mark.parametrize("path,floor", _FREE_FORM)
def test_free_form_content_is_not_held_to_the_control_message_default(monkeypatch, path, floor):
    # Just over the 1 MiB default is a real message, and so is a body past the floor
    # on a Host with room for it: the memory sets the cap, no platform number does.
    monkeypatch.setattr(BC, "_memory_room", lambda: 4 << 30, raising=False)
    for size in (BC.DEFAULT_CAP + 1, floor + 1):
        for declared in (False, True):
            wire, n = _served(path, size, declared=declared)
            assert wire.status == 200 and n == size, (path, size, declared, wire.status, wire.body[:200])
    # A Host with no room left, or none that can be read, still takes the floor whole.
    for room in (0, None):
        monkeypatch.setattr(BC, "_memory_room", lambda room=room: room, raising=False)
        assert BC.RequestBodyCap(None).cap_for("POST", path) == floor
        wire, n = _served(path, floor)
        assert wire.status == 200 and n == floor, (path, room, wire.status, wire.body[:200])


@pytest.mark.parametrize("path,floor", _FREE_FORM)
def test_free_form_content_past_the_hosts_memory_is_refused_413(monkeypatch, path, floor):
    # Past the floor the cap is a third of the room (the body is held three times
    # over while it is read): a body over it is refused with 413 that names it, a
    # declared one before a byte is read.
    room_mib = 3 * 200
    monkeypatch.setattr(BC, "_memory_room", lambda: room_mib * MIB, raising=False)
    cap = 200 * MIB
    assert BC.RequestBodyCap(None).cap_for("POST", path) == cap
    inner, got = _streaming_counter()
    wire = _call(BC.RequestBodyCap(inner), path, headers=[("content-length", str(cap + 1))], chunks=())
    assert wire.status == 413 and "200 MiB" in wire.body.decode(), (wire.status, wire.body)
    assert wire.receive_calls == 0 and "n" not in got
    # A body with no length is stopped at the cap, never held past one chunk.
    monkeypatch.setattr(BC, "_memory_room", lambda: 0, raising=False)
    inner, got = _streaming_counter()
    wire = _call(BC.RequestBodyCap(inner), path, headers=[("transfer-encoding", "chunked")],
                 chunks=_forever(floor + 1, MIB))
    assert wire.status == 413 and BC.size_words(floor) in wire.body.decode(), (wire.status, wire.body)
    assert wire.headers.get("connection") == "close"


def test_the_free_form_floors_come_from_real_senders(real_app):
    # 25 MiB is the largest file the Host takes in one go (routes/context.py), so the
    # text of one fits a clipboard write; the integration call's floor is the chat
    # message's, as the integrations layer has no attachment size of its own.
    assert BC.RequestBodyCap(None).cap_for("POST", "/api/clipboard/write") >= real_app["consts"]["MAX_FILE_UPLOAD_BYTES"]
    assert BC._CLIPBOARD_FLOOR == 25 * MIB
    assert BC.RequestBodyCap(None).cap_for(
        "POST", "/api/integrations/connections/c1/call") >= BC._PROVIDER_REQUEST_MAX
    sized = []
    for p in (_ROOT / "server/integrations").rglob("*.py"):
        text = p.read_text(errors="replace")
        if "attachment" in text.lower() and any(w in text for w in ("MiB", "MAX_", "_MAX")):
            sized.append(str(p.relative_to(_ROOT)))
    assert not sized, f"the integrations layer now sizes attachments: take its number as the floor: {sized}"


def test_the_memory_room_is_the_hosts_own_reading(monkeypatch):
    from mastercode.runtime import host_memory
    monkeypatch.setattr(host_memory, "limit_bytes", lambda: 3 << 30)
    monkeypatch.setattr(host_memory, "working_set_bytes", lambda: 1 << 30)
    assert BC._memory_room() == 2 << 30
    monkeypatch.setattr(host_memory, "limit_bytes", lambda: None)
    room = BC._memory_room()            # no cgroup limit: the machine's free memory
    assert room is None or room > 0


def test_a_route_that_raises_after_its_body_was_refused_answers_413_not_500():
    # A route that catches the refusal and raises an error of its own: the error
    # passes up through the cap to Starlette's ServerErrorMiddleware, which sits
    # outside every added middleware and would answer 500.
    app = FastAPI()
    app.add_middleware(BC.RequestBodyCap, default=CAP, routes=())

    @app.post("/api/x/rethrow")
    async def rethrow(request: Request):
        try:
            await request.json()
        except Exception:     # noqa: BLE001 — the pattern under test
            raise RuntimeError("could not read the body") from None
        return {"ok": True}

    wire = None
    try:
        wire = _call(app, "/api/x/rethrow", headers=[("content-type", "application/json"),
                                                     ("transfer-encoding", "chunked")],
                     chunks=_forever(4 * CAP))
    except RuntimeError:
        pass
    assert wire is not None and wire.status == 413, getattr(wire, "status", "raised")


def test_the_front_door_copies_a_chunk_as_it_arrives():
    # mtls_frontdoor relays a chunked body before any cap behind it counts a
    # byte: a chunk of whatever size its line declares must go on as it arrives,
    # never be held whole first.
    from server import mtls_frontdoor as FD

    class _W:
        def __init__(self):
            self.out = bytearray()

        def write(self, b):
            self.out += b

        async def drain(self):
            pass

    async def run():
        r = asyncio.StreamReader(limit=FD.STREAM_LIMIT)
        w = _W()
        r.feed_data(b"40000000\r\n")              # a 1 GiB chunk declared
        r.feed_data(b"m" * MIB)                   # one MiB of it sent so far
        task = asyncio.ensure_future(FD._copy_chunked(r, w))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(w.out) >= MIB:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(w.out) - len(b"40000000\r\n")

    loop = asyncio.new_event_loop()
    try:
        relayed = loop.run_until_complete(run())
    finally:
        loop.close()
    assert relayed == MIB, f"the front door held the chunk: {relayed} of {MIB} bytes relayed"
