#!/usr/bin/env python3
"""A path check decides on the path the request is ROUTED on.

Starlette before 1.0.1 rebuilt request.url from the Host header unchecked, so
`Host: x/api/system/health?` made request.url.path read /api/system/health for
a request routed anywhere. The localhost-only gate exempted health probes by
that value: any peer that reached :8800 got past it to every route. Proven
against an earlier image in-process before this fix.

  A. The Host guard (server/middleware/host_header.py) refuses a Host that
     could not be a host — HTTP with 400, a WebSocket upgrade with a close —
     and lets every real one through.
  B. It is the OUTERMOST middleware in server/main.py (added last).
  C. The security middleware never reads request.url: the routed path only.
  D. The localhost gate, with request.url rebuilt from Host the way Starlette
     0.41.3 did it (patched in, so this fails on the old code whatever
     Starlette the gate runs on): a crafted Host no longer opens a route, and
     the gate still admits what it admitted.
  E. WebSocket upgrades, which a BaseHTTPMiddleware never sees, pass the same
     source gate: outside peers are closed before a handler runs; in-pod
     peers, the router cert, the bastion lanes with the operator cert, a
     self-authenticating connector path, and a ship proof's page with its own app's verify cap (in a
     cookie) pass.

Container-free: in-process ASGI, no pod, no network.
"""
import ast
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.environ.get("APP_ROOT") or str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
# The router certificate CN the gate trusts is deployment config with no
# default; give it one so the mTLS path has a router to recognise.
os.environ.setdefault("DASHBOARD_ROUTER_CN", "router.example.test")

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.datastructures import URL  # noqa: E402
from starlette.requests import Request  # noqa: E402

from server.middleware.host_header import HostHeaderGuard, host_is_malformed  # noqa: E402

# ── A. the Host guard ───────────────────────────────────────────────────────
for bad in ("x/api/system/health?", "x?y", "x#y", "user@x", "x\\y", "x y", "x\ty", "x\x00"):
    check(f"A Host that could not be a host is malformed: {bad!r}", host_is_malformed(bad))
for good in ("localhost:8800", "127.0.0.1", "[::1]:8800", "[fd12:3456:789a::23]", "h.host.example.test",
             "cseries.entity-1.svc.cluster.local:8800", "apps-host-a.host.example.test:443",
             "entity-1.zone.example.test", "name.test   ", "\tname.test", "trailing.dot.", "xn--l-0ga.test",
             "l\xe0.test"):
    check(f"…and a real one is not: {good!r}", not host_is_malformed(good))


def _guarded_app():
    inner = FastAPI()

    @inner.get("/anything")
    async def anything():
        return {"reached": True}
    return HostHeaderGuard(inner)


async def _http(app, host, client=("127.0.0.1", 40000), path="/anything"):
    t = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=t, base_url="http://pod") as c:
        return await c.get(path, headers={"host": host} if host is not None else {})

r = asyncio.run(_http(_guarded_app(), "x/api/system/health?"))
check("HTTP with a malformed Host → 400, and the route never runs", r.status_code == 400 and "reached" not in r.text,
      (r.status_code, r.text))
r = asyncio.run(_http(_guarded_app(), "h.host.example.test"))
check("HTTP with a real Host reaches the route", r.status_code == 200 and r.json() == {"reached": True}, r.text)


async def _ws(host):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)

    reached = []

    async def inner(scope, receive_, send_):
        reached.append(scope["path"])
    await HostHeaderGuard(inner)({"type": "websocket", "path": "/ws", "headers": [(b"host", host.encode())],
                                  "client": ("10.0.0.9", 1)}, receive, send)
    return sent, reached

sent, reached = asyncio.run(_ws("x/api/system/health?"))
check("A WebSocket upgrade with a malformed Host is closed before the app sees it",
      sent == [{"type": "websocket.close", "code": 1008}] and not reached, (sent, reached))
sent, reached = asyncio.run(_ws("h.host.example.test"))
check("…and one with a real Host goes through", reached == ["/ws"] and not sent, (sent, reached))

# X-Forwarded-Host: a page can set it on its own fetch, and the app-origin
# code believes it before Host, so only a trusted proxy's survives the guard.
async def _xfh_seen(client, trusted=""):
    seen = {}

    async def inner(scope, receive_, send_):
        seen.update(dict(scope["headers"]))
    saved = os.environ.get("CSERIES_TRUSTED_PROXIES")
    os.environ["CSERIES_TRUSTED_PROXIES"] = trusted
    try:
        await HostHeaderGuard(inner)({"type": "http", "path": "/user-apps/", "client": (client, 1),
                                      "headers": [(b"host", b"apps-x-home.cseries.localhost:8800"),
                                                  (b"x-forwarded-host", b"home.cseries.localhost")]},
                                     None, None)
    finally:
        if saved is None:
            os.environ.pop("CSERIES_TRUSTED_PROXIES", None)
        else:
            os.environ["CSERIES_TRUSTED_PROXIES"] = saved
    return seen.get(b"x-forwarded-host")

check("X-Forwarded-Host from a peer that is not a trusted proxy is removed",
      asyncio.run(_xfh_seen("172.17.0.1")) is None)
check("…also with proxies configured that it is not one of",
      asyncio.run(_xfh_seen("172.17.0.1", "10.9.0.0/16")) is None)
check("…and kept from a trusted proxy",
      asyncio.run(_xfh_seen("10.9.0.4", "10.9.0.0/16")) == b"home.cseries.localhost")

# ── B. outermost ────────────────────────────────────────────────────────────
import re as _re  # noqa: E402


def _registrations(tree):
    """(lineno, top_level, is_guard) for every add_middleware call and every
    @….middleware decorator anywhere in the module."""
    top = {id(n) for n in tree.body}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_middleware":
            guard = bool(node.args) and isinstance(node.args[0], ast.Name) and node.args[0].id == "HostHeaderGuard"
            out.append((node.lineno, None, guard))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                if isinstance(f, ast.Attribute) and f.attr == "middleware":
                    out.append((d.lineno, id(node) in top, False))
    # a call is top-level when its statement is
    stmt_lines = {n.lineno for n in tree.body if isinstance(n, ast.Expr)}
    return [(ln, top_level if top_level is not None else ln in stmt_lines, g) for ln, top_level, g in out]


_regs = _registrations(ast.parse((ROOT / "server/main.py").read_text()))
_guard = [ln for ln, _t, g in _regs if g]
check("The Host guard is registered last in server/main.py, at module level, so it is the outermost layer",
      len(_guard) == 1 and all(t for _ln, t, _g in _regs) and _guard[0] == max(ln for ln, _t, _g in _regs), _regs)
_elsewhere = []
for _p in list((ROOT / "server").rglob("*.py")) + list((ROOT / "brain").rglob("*.py")):
    if _p == ROOT / "server/main.py":
        continue
    _txt = _p.read_text(errors="replace")
    if _re.search(r"add_middleware\(|\.middleware\(", _txt):
        _elsewhere.append(str(_p.relative_to(ROOT)))
check("…and nothing else in server/ or brain/ registers a middleware that could sit outside it",
      not _elsewhere, _elsewhere)

# ── C. no request.url in a security decision ────────────────────────────────
_GATES = ["server/middleware/localhost_only.py", "server/middleware/ceigas_gate.py",
          "server/applications/middleware.py", "sidecars/browser/control/app.py"]
for rel in _GATES:
    tree = ast.parse((ROOT / rel).read_text())
    uses = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "url"
            and isinstance(n.value, ast.Name) and n.value.id == "request"]
    check(f"{rel} never reads request.url (the routed path is request.scope['path'])", not uses, uses)

# ── D. the localhost gate under a Host-rebuilt request.url ──────────────────
_real_url = Request.url


def _url_from_host(self):
    # What Starlette 0.41.3 did: scheme://<Host header><path>?<query>.
    host = self.headers.get("host") or "testserver"
    qs = self.scope.get("query_string", b"").decode("latin-1")
    return URL(f"{self.scope.get('scheme', 'http')}://{host}{self.scope['path']}" + (f"?{qs}" if qs else ""))

Request.url = property(_url_from_host)
try:
    check("(the patch reproduces the rebuild: a crafted Host moves request.url.path)",
          Request({"type": "http", "path": "/api/protected", "query_string": b"", "scheme": "http",
                   "headers": [(b"host", b"x/api/system/health?")]}).url.path == "/api/system/health")

    from server.middleware.localhost_only import LocalhostOnlyMiddleware  # noqa: E402
    gated = FastAPI()
    gated.add_middleware(LocalhostOnlyMiddleware)

    @gated.get("/api/protected")
    async def protected():
        return {"reached": "protected"}

    @gated.get("/api/system/health")
    async def health():
        return {"ok": True}

    outside = ("203.0.113.9", 40000)
    r = asyncio.run(_http(gated, None, outside, "/api/protected"))
    check("A peer outside the pod is refused a protected route", r.status_code == 403, (r.status_code, r.text))
    for host in ("x/api/system/health?", "x/api/connector/?", "x/api/system/health#"):
        r = asyncio.run(_http(gated, host, outside, "/api/protected"))
        check(f"…and a crafted Host does not open it: {host!r}", r.status_code == 403, (r.status_code, r.text))
    r = asyncio.run(_http(gated, None, outside, "/api/system/health"))
    check("The probe path still answers a peer outside the pod", r.status_code == 200, (r.status_code, r.text))
    r = asyncio.run(_http(gated, "localhost:8800", ("127.0.0.1", 40000), "/api/protected"))
    check("…and the pod itself still reaches a protected route", r.status_code == 200, (r.status_code, r.text))
finally:
    Request.url = _real_url

# ── E. WebSocket upgrades pass the same source gate ────────────────────────
# LocalhostOnlyMiddleware is a BaseHTTPMiddleware and never sees an upgrade;
# LocalhostOnlyWebSocketGate is its WebSocket half.
from server.middleware import localhost_only as LO  # noqa: E402


def _ws_scope(path, client, cn=None):
    # A loopback peer must also name this machine (a DNS-rebinding page
    # reaches loopback under its own name), so the scopes say localhost.
    headers = [(b"host", b"localhost:8800")]
    if cn is not None:
        headers.append((b"x-client-cert-cn", cn.encode()))
    return {"type": "websocket", "path": path, "headers": headers, "client": (client, 5000)}

_OUT = "203.0.113.9"
for path in ("/ws", "/system-apps/browser/viewport/ws", "/bastion-egress", "/api/network/sessions/x"):
    check(f"WS {path} from a peer outside the pod is refused",
          LO.websocket_refusal(_ws_scope(path, _OUT)) == "no owner session")
check("…and a CN header from outside the pod is ignored, not trusted",
      LO.websocket_refusal(_ws_scope("/ws", _OUT, cn=LO.ROUTER_CN)) == "no owner session")
check("A self-authenticating connector path (it proves its capability in the handler) passes from anywhere",
      LO.websocket_refusal(_ws_scope("/api/connector/x", _OUT)) is None)
for client in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
    check(f"An in-pod peer ({client}) passes", LO.websocket_refusal(_ws_scope("/ws", client)) is None)
# The Docker bridge gateway is where every visitor to a published port comes
# from, so it is not in-pod: it needs the owner's session like anyone else.
check("The Docker bridge gateway (172.17.0.1) is refused without a session",
      LO.websocket_refusal(_ws_scope("/ws", "172.17.0.1")) == "no owner session")
check("On the mTLS path the router cert passes",
      LO.websocket_refusal(_ws_scope("/ws", "::1", cn=LO.ROUTER_CN)) is None)
check("…a bastion lane passes with its operator cert (its handler binds the cert too)",
      all(LO.websocket_refusal(_ws_scope(p_, "::1", cn="operator")) is None for p_ in LO._BASTION_WS_PATHS))
check("…and not with another tenant's cert",
      all(LO.websocket_refusal(_ws_scope(p_, "::1", cn="domain-9")) == "cross-tenant cert"
          for p_ in LO._BASTION_WS_PATHS))

# A ship proof's verify cap opened its app's WS from the browser pod. A
# standalone Host has no browser pod, so the cap is no exemption here: the
# WebSocket needs the owner's session like any other.
from mastercode.runtime import app_verify as _AV  # noqa: E402
_capA = _AV._mint_verify_cap("appA")
try:
    def _with_cookie(path, cookie):
        sc = _ws_scope(path, _OUT)
        sc["headers"].append((b"cookie", cookie.encode()))
        return sc
    check("A proof's verify cap in a cookie opens no WebSocket without the owner's session",
          LO.websocket_refusal(_with_cookie("/system-apps/appA/ws", f"a=1; cseries_verify_cap={_capA}"))
          == "no owner session"
          and LO.websocket_refusal(_with_cookie("/system-apps/appA/ws", f'cseries_verify_cap="{_capA}"'))
          == "no owner session")
finally:
    _AV._forget_verify_cap("appA")
check("…and any other cert is refused anywhere else, as over HTTP",
      LO.websocket_refusal(_ws_scope("/ws", "::1", cn="domain-9")) == "cross-tenant cert"
      and LO.websocket_refusal(_ws_scope("/system-apps/a/b", "::1", cn="operator")) == "cross-tenant cert")
_lanes = set(_re.findall(r'@app\.websocket\("(/bastion-[a-z-]+)"\)', (ROOT / "server/main.py").read_text()))
check("The bastion-lane list is exactly the bastion WS routes in server/main.py",
      _lanes and _lanes == set(LO._BASTION_WS_PATHS), (_lanes, LO._BASTION_WS_PATHS))


async def _through_gate(scope):
    sent, reached = [], []

    async def inner(scope_, receive_, send_):
        reached.append(scope_["path"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)
    await LO.LocalhostOnlyWebSocketGate(inner)(scope, receive, send)
    return sent, reached

sent, reached = asyncio.run(_through_gate(_ws_scope("/system-apps/browser/x", _OUT)))
check("The gate closes a refused upgrade before any handler runs",
      sent == [{"type": "websocket.close", "code": 1008}] and not reached, (sent, reached))
sent, reached = asyncio.run(_through_gate(_ws_scope("/ws", "::1", cn=LO.ROUTER_CN)))
check("…and hands an admitted one on untouched", reached == ["/ws"] and not sent, (sent, reached))
sent, reached = asyncio.run(_through_gate({"type": "http", "path": "/x", "headers": [], "client": (_OUT, 1)}))
check("…and leaves HTTP to the HTTP gate", reached == ["/x"] and not sent)
check("server/main.py registers the WebSocket gate",
      "app.add_middleware(LocalhostOnlyWebSocketGate)" in (ROOT / "server/main.py").read_text())

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall gates passed")
