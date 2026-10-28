"""POST /api/network/harness/run (and the routes beside it in server/routes/network.py)
read their body under a cap, and refuse a larger one with 413 before it is held whole.

THE HOLE THIS CLOSES. Every body route in server/routes/network.py read its body with
`await request.json()`, which collects the whole body in memory before looking at it. A
body sent chunked carries no Content-Length to check first, so any caller that could
reach a route (an installed app holding `write:network`, a paired bastion, and for
/bastions/redeem anyone) could make the Host buffer as much as it cared to send.
/harness/run is the largest: its body carries the worktree as base64, and the bastion
reads at most 32 MiB of a harness spec (the bastion's client-go/lane_bounds.go
harnessSpecMaxBytes), so the Host has no reason to hold more.

WHAT IS REAL HERE. The REAL router, the REAL `requires_permission` dependency and the
REAL bounded reader (server/harness_input.py::read_request) run. Only the pod-external
edges are stood in for: the DB pool and the service functions the routes dispatch to.
The route is driven two ways:

  * straight through the ASGI interface, where `receive` is the caller's socket and is
    counted. That is what shows a body was refused WITHOUT being read: a declared
    Content-Length over the cap must leave `receive` untouched, and a chunked body must
    stop within one chunk of the cap. (Starlette's TestClient hands a bytes body over
    in one piece, so it cannot show either.)
  * behind a real uvicorn server and a raw socket, where the oversized request is
    answered while the client has still not sent the rest of its body.

MUTATION (run before shipping):
  - Put `body = await request.json()` back in harness_run -> the declared-length,
    chunked, boundary and real-server tests fail (the body is read whole, then json.loads
    refuses it as garbage with a 500, or the service is reached).
  - Raise _HARNESS_BODY_MAX above 32 MiB, or drop the cap argument from the
    harness_run call -> test_harness_run_cap_is_the_benches_32_mib and the boundary test
    fail.
  - Put `await request.json()` back in any other route of the file, under any spelling
    (`req.json()` on a Request named otherwise, an alias of it, getattr, Request.json),
    or give a route a JSON body parameter FastAPI reads itself ->
    test_no_route_here_reads_a_whole_body fails; test_the_whole_body_scan_sees_every_spelling
    holds the scan to each of those spellings.

Run: pytest tests/test_harness_run_body_bound.py
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import socket
import sys
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

import server.brain_service as BS  # noqa: E402
import server.routes.network as N  # noqa: E402
from server.applications.tokens import AppToken  # noqa: E402
from server.network import nac_harness, nac_mgmt  # noqa: E402

PREFIX = "/api/network"
HARNESS_URL = f"{PREFIX}/harness/run"
MIB = 1 << 20
CHUNK = MIB
HARNESS_CAP = 32 * MIB     # what the bastion reads of a harness spec; the contract, not read from N

HARNESS_BODY = {"server": "box", "harness_id": "claude", "prompt": "hi", "mode": "echo"}


class _Pool:
    """Enough of an asyncpg pool that get_pool() is not None. `fetchrow` answers like
    a live bastion token, so the bearer-authed routes reach their body read."""

    async def execute(self, *a):
        return None

    async def fetch(self, *a):
        return []

    async def fetchrow(self, *a):
        return {"id": 7, "entity_id": 9003}

    async def fetchval(self, *a):
        return None


class _Stamp:
    """Pure-ASGI stand-in for the app-token middleware: puts a token with exactly
    `permissions` where the browser's request has one. A pure ASGI wrapper (not
    BaseHTTPMiddleware) so `receive` reaches the route untouched and can be counted.
    `delivered` counts the body bytes the route took from `receive`."""

    def __init__(self, app, permissions):
        self.app, self.permissions, self.delivered = app, permissions, 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["app_token"] = AppToken(
                app_id="nosy", permissions=list(self.permissions), window_id="w1", iat=0)

            async def counted():
                msg = await receive()
                self.delivered += len(msg.get("body", b"") or b"")
                return msg
            return await self.app(scope, counted, send)
        return await self.app(scope, receive, send)


def _app(permissions=("write:network",)) -> _Stamp:
    app = FastAPI()
    app.include_router(N.router)
    return _Stamp(app, permissions)


@pytest.fixture()
def reached(monkeypatch):
    """Every dispatch that got past the body read into a service."""
    calls: list[tuple] = []

    async def _harness(pool, eid, server, harness_id, **kw):
        calls.append(("harness", server, harness_id, kw.get("mode"), len(kw.get("worktree_b64") or "")))
        return {"server": server, "harness_id": harness_id, "exit": 0, "stdout": "", "stderr": ""}

    async def _mgmt(pool, eid, server, op, args, **kw):
        calls.append(("mgmt", server, op))
        return {"server": server, "op": op, "exit": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(nac_harness, "run_harness_op", _harness)
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", _mgmt)
    monkeypatch.setattr(BS, "get_pool", lambda: _Pool())
    monkeypatch.setattr(BS, "get_control_pool", lambda: _Pool())
    monkeypatch.setattr(BS, "_entity_id", 9003, raising=False)
    return calls


# ── a request through the ASGI interface, with `receive` counted ─────────────

class _Wire:
    """What the route did with the caller's socket."""
    def __init__(self):
        self.receive_calls = 0
        self.status = None
        self.body = b""
        self.error = None


def _call(app, path, *, headers, chunks) -> _Wire:
    """POST to `path` with the given headers; `receive` hands out `chunks` (an iterator
    of bytes) one per call, lazily. Returns what the route did."""
    wire = _Wire()
    it = iter(chunks)

    async def receive():
        wire.receive_calls += 1
        try:
            return {"type": "http.request", "body": next(it), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            wire.status = msg["status"]
        elif msg["type"] == "http.response.body":
            wire.body += msg.get("body", b"")

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), *[(k.encode(), v.encode()) for k, v in headers]]}
    # A private loop, never set as the thread's: asyncio.run() would leave the main
    # thread without one on 3.14, which tests/conftest.py documents breaking other files.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    except Exception as e:  # noqa: BLE001 — a route that fails after its 500 is the test's to judge
        wire.error = e
    finally:
        loop.close()
    return wire


def _slices(data: bytes, n: int = CHUNK):
    for i in range(0, len(data), n):
        yield data[i:i + n]


def _forever(total: int, n: int = CHUNK):
    """`total` bytes in chunks of n, made on demand (nothing is held)."""
    block = b"x" * n
    left = total
    while left > 0:
        take = min(n, left)
        yield block[:take]
        left -= take


def _json_of_len(total: int) -> bytes:
    """A harness body of exactly `total` bytes, nearly all of it worktree_b64."""
    head = b'{"server":"box","harness_id":"claude","mode":"echo","worktree_b64":"'
    tail = b'"}'
    return head + b"A" * (total - len(head) - len(tail)) + tail


# ── 1. a declared Content-Length over the cap: refused before a byte is read ─

def test_oversized_declared_length_is_refused_without_reading_any_of_it(reached):
    app = _app()
    cap = HARNESS_CAP
    wire = _call(app, HARNESS_URL,
                 headers=[("content-type", "application/json"), ("content-length", str(cap + 1))],
                 chunks=_forever(cap + 1))
    assert wire.status == 413, (wire.status, wire.body)
    assert wire.receive_calls == 0, f"the body was read ({wire.receive_calls} reads) before the refusal"
    assert app.delivered == 0
    assert reached == [], "an oversized request reached the harness service"
    text = wire.body.decode()
    assert "32 MiB" in text, text
    assert "platform" not in text.lower(), "the refusal blames the platform"


# ── 2. a chunked body (no Content-Length) is stopped while it arrives ────────

def test_oversized_chunked_body_is_stopped_at_the_cap(reached):
    app = _app()
    cap = HARNESS_CAP
    wire = _call(app, HARNESS_URL,
                 headers=[("content-type", "application/json"), ("transfer-encoding", "chunked")],
                 chunks=_forever(3 * cap))
    assert wire.status == 413, (wire.status, wire.body)
    assert app.delivered <= cap + CHUNK, (
        f"the route took {app.delivered} bytes of a body it should have stopped at {cap}")
    assert wire.receive_calls <= cap // CHUNK + 2, wire.receive_calls
    assert reached == []


# ── 3. the cap is exact: 32 MiB goes through, one byte more does not ─────────

def test_body_of_exactly_the_cap_is_served_and_one_byte_more_is_not(reached):
    cap = HARNESS_CAP
    ok = _json_of_len(cap)
    wire = _call(_app(), HARNESS_URL,
                 headers=[("content-type", "application/json"), ("content-length", str(cap))],
                 chunks=_slices(ok))
    assert wire.status == 200, (wire.status, wire.body[:300])
    assert [c[0] for c in reached] == ["harness"] and reached[0][4] > 31 * MIB

    over = _json_of_len(cap + 1)
    reached.clear()
    wire = _call(_app(), HARNESS_URL,
                 headers=[("content-type", "application/json"), ("transfer-encoding", "chunked")],
                 chunks=_slices(over))
    assert wire.status == 413, wire.status
    assert reached == []


def test_harness_run_cap_is_the_benches_32_mib():
    # the bastion's client-go/lane_bounds.go: harnessSpecMaxBytes = 2 x the 16 MiB
    # websocket message. The Host refuses what the bastion would refuse.
    assert getattr(N, "_HARNESS_BODY_MAX", None) == HARNESS_CAP


# ── 4. a normal request still works, including a big worktree ────────────────

def test_a_normal_harness_request_still_dispatches(reached):
    r = TestClient(_app()).post(HARNESS_URL, json=HARNESS_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["harness_id"] == "claude"
    assert reached == [("harness", "box", "claude", "echo", 0)]


def test_a_large_worktree_under_the_cap_is_not_throttled(reached):
    # Far over the 1 MiB a control message gets, well under the 32 MiB a spec can carry.
    body = dict(HARNESS_BODY, mode="worktree", worktree_b64="A" * (20 * MIB))
    r = TestClient(_app()).post(HARNESS_URL, json=body)
    assert r.status_code == 200, r.text[:300]
    assert reached == [("harness", "box", "claude", "worktree", 20 * MIB)]


def test_a_body_that_is_not_json_is_a_400_not_a_crash(reached):
    r = TestClient(_app()).post(HARNESS_URL, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text
    r = TestClient(_app()).post(HARNESS_URL, content=b"", headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text
    assert reached == []


@pytest.mark.parametrize("raw", [b"[]", b"null", b"7", b'"text"', b'[["server","box"]]'])
def test_json_that_is_not_an_object_is_a_400_not_a_crash(reached, raw):
    # Every route in the file reads its body as an object (body.get, body[...]); valid
    # JSON of another shape used to reach body.get and answer 500.
    c = TestClient(_app(), raise_server_exceptions=False)
    for path in (HARNESS_URL, f"{PREFIX}/nac/policy/propose", f"{PREFIX}/bastions/redeem"):
        r = c.post(path, content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 400, (path, raw, r.status_code, r.text[:200])
        assert "object" in r.text, r.text
    assert reached == []


# ── 5. the routes beside it, each at its own cap ─────────────────────────────

_SIBLINGS = [
    # (path, the cap the route reads under, extra headers)
    (f"{PREFIX}/mgmt/run", 12 * MIB, []),
    (f"{PREFIX}/nac/policy/propose", 1 * MIB, []),
    (f"{PREFIX}/nac/dcn/plan/propose", 8 * MIB, []),
    (f"{PREFIX}/bastions/heartbeat", 8 * MIB, [("authorization", "Bearer b")]),
    (f"{PREFIX}/bastions/redeem", 64 * 1024, []),      # public: read before the secret is checked
]


@pytest.mark.parametrize("path,cap,extra", _SIBLINGS, ids=[s[0].rsplit("/", 2)[-2] + "/" + s[0].rsplit("/", 1)[-1] for s in _SIBLINGS])
def test_sibling_routes_refuse_an_oversized_body_unread(reached, path, cap, extra):
    app = _app()
    hdr = [("content-type", "application/json"), *extra]
    wire = _call(app, path, headers=[*hdr, ("content-length", str(cap + 1))], chunks=_forever(cap + 1))
    assert wire.status == 413 and wire.receive_calls == 0, (wire.status, wire.receive_calls, wire.body)
    app = _app()
    wire = _call(app, path, headers=[*hdr, ("transfer-encoding", "chunked")], chunks=_forever(3 * cap))
    assert wire.status == 413, (wire.status, wire.body)
    assert app.delivered <= cap + CHUNK, app.delivered
    assert reached == []


def test_a_request_that_is_not_json_is_left_unread_where_the_route_never_read_it(reached):
    # /bastions/redeem with no JSON content type has no body to read: {} as before.
    app = _app()
    wire = _call(app, f"{PREFIX}/bastions/redeem", headers=[("content-length", str(10 * MIB))],
                 chunks=_forever(10 * MIB))
    assert wire.receive_calls == 0 and wire.status != 413, (wire.status, wire.receive_calls)


# ── 6. the class: no route in the file reads a whole body unbounded ──────────

_WHOLE_BODY = ("json", "body", "form", "stream")   # stream: a read loop of the file's own


class _SpecModel(BaseModel):
    """A pydantic body, for the scan's own test (module level: this file's annotations
    are strings, and FastAPI resolves them in the module)."""
    server: str


def _whole_body_reads(src: str) -> list[str]:
    """Every place `src` touches a request's body itself rather than through _json_in:
    .json/.body/.form/.stream on a name that holds the request (a parameter annotated
    Request whatever it is called, `request`, or a name assigned from one), getattr of
    one of those, or Request.json(request) and the like. Not by name of the variable
    alone: `req.json()` reads a whole body just as `request.json()` did."""
    out = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
        held = {"request"} | {a.arg for a in args if a.annotation is not None
                              and ast.unparse(a.annotation).split(".")[-1] == "Request"}
        grew = True
        while grew:                       # r = request; s = r -> both hold it
            grew = False
            for n in ast.walk(fn):
                if (isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Name)
                        and n.value.id in held):
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        if isinstance(t, ast.Name) and t.id not in held:
                            held.add(t.id)
                            grew = True
        for n in ast.walk(fn):
            if (isinstance(n, ast.Attribute) and n.attr in _WHOLE_BODY
                    and isinstance(n.value, ast.Name) and n.value.id in held | {"Request"}):
                out.append(f"line {n.lineno}: {ast.unparse(n)}")
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"
                    and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) and n.args[0].id in held
                    and isinstance(n.args[1], ast.Constant) and n.args[1].value in _WHOLE_BODY):
                out.append(f"line {n.lineno}: {ast.unparse(n)}")
    return sorted(set(out))


def _routes_fastapi_reads_whole(router) -> list[str]:
    """Routes with a body parameter (a pydantic model, Body(), Form(), UploadFile):
    FastAPI reads those bodies whole before the route's own code runs, where no scan
    of the route's source can see it."""
    from fastapi.routing import APIRoute
    return [f"{sorted(r.methods)} {r.path}: {[p.name for p in r.dependant.body_params]}"
            for r in router.routes if isinstance(r, APIRoute) and r.dependant.body_params]


def test_no_route_here_reads_a_whole_body():
    bad = _whole_body_reads(pathlib.Path(N.__file__).read_text())
    assert not bad, ("server/routes/network.py reads a body with no cap; read it with "
                     "_json_in(request, cap):\n  " + "\n  ".join(bad))
    bad = _routes_fastapi_reads_whole(N.router)
    assert not bad, ("a route takes a body parameter, which FastAPI reads whole with no cap; "
                     "take `request: Request` and read it with _json_in:\n  " + "\n  ".join(bad))


def test_the_whole_body_scan_sees_every_spelling():
    # The scan above is what keeps the cap from coming undone in a later edit, so it is
    # held to each way a whole body can be read. The first scan matched only the literal
    # `request.json()` and passed every spelling here but the first.
    spellings = {
        "request.json()": "async def f(request: Request):\n    return await request.json()\n",
        "another name": "async def f(req: Request):\n    return await req.json()\n",
        "a dotted annotation": "async def f(r: starlette.requests.Request):\n    return await r.body()\n",
        "an alias": "async def f(request: Request):\n    r = request\n    return await r.json()\n",
        "an alias of an alias": ("async def f(request: Request):\n    a = request\n    b = a\n"
                                 "    return await b.form()\n"),
        "getattr": "async def f(request: Request):\n    return await getattr(request, 'json')()\n",
        "the class": "async def f(request: Request):\n    return await Request.json(request)\n",
        "a stream of its own": ("async def f(request: Request):\n"
                                "    return b''.join([c async for c in request.stream()])\n"),
    }
    missed = [k for k, src in spellings.items() if not _whole_body_reads(src)]
    assert not missed, f"the scan does not see: {missed}"
    # What the file legitimately does is not flagged: the bounded reader, a header read,
    # and .json() on something that is not the request (a reply from another service).
    fine = ("async def f(request: Request, resp):\n"
            "    body = await _json_in(request, 1 << 20)\n"
            "    ct = request.headers.get('content-type')\n"
            "    return (await resp.json()), body, ct\n")
    assert _whole_body_reads(fine) == []

    from fastapi import APIRouter, Body

    r = APIRouter()

    @r.post("/a")
    async def a(spec: _SpecModel):
        return spec

    @r.post("/b")
    async def b(payload: dict = Body(...)):
        return payload

    @r.post("/c")
    async def c(request: N.Request):
        return await N._json_in(request)

    assert [x.split(":")[0] for x in _routes_fastapi_reads_whole(r)] == ["['POST'] /a", "['POST'] /b"]


# ── 7. behind a real server, from a raw socket ───────────────────────────────

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


def _read_status(sock) -> tuple[int, bytes]:
    """The status and the whole response (head and body) of one answer."""
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


def test_real_server_answers_a_declared_oversize_before_the_body_is_sent(reached):
    app = _app()
    cap = HARNESS_CAP
    with _Server(app) as srv, socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
        s.sendall((f"POST {HARNESS_URL} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {cap + 1}\r\n\r\n").encode() + b'{"server":"box"')
        # The client has sent 15 bytes of a body it says is 32 MiB + 1 and sends no more.
        status, raw = _read_status(s)
    assert status == 413, raw[:300]
    assert b"32 MiB" in raw
    assert app.delivered < MIB, app.delivered
    assert reached == []


def test_real_server_stops_a_chunked_body_at_the_cap(reached):
    app = _app()
    cap = HARNESS_CAP
    chunk = b"x" * CHUNK
    frame = b"%x\r\n" % len(chunk) + chunk + b"\r\n"
    status, raw, sent = None, b"", 0
    with _Server(app) as srv, socket.create_connection(("127.0.0.1", srv.port), timeout=10) as s:
        s.sendall(f"POST {HARNESS_URL} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  "Transfer-Encoding: chunked\r\n\r\n".encode())
        s.setblocking(False)
        deadline = time.monotonic() + 20
        pending = memoryview(b"")
        while status is None and sent < 4 * cap and time.monotonic() < deadline:
            if not pending:
                pending = memoryview(frame)
            try:
                pending = pending[s.send(pending):]      # a send may take part of a frame
                if not pending:
                    sent += len(chunk)
            except BlockingIOError:
                time.sleep(0.005)       # the server is not reading: its buffers are full
            except ConnectionError:
                break                   # the server closed on us: read what it said
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
    assert app.delivered <= cap + CHUNK, f"the server took {app.delivered} bytes of a {cap}-byte cap"
    assert reached == []
