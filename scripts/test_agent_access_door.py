#!/usr/bin/env python3
"""An outside agent's MCP lane through the Host's door and gates.

An agent's MCP client reaches /mcp and its six sign-in paths with no session
of the person's. Each answers for itself (server/agent_access, routes/mcp.py),
so the door and the gates must let them reach their handler, and never send
the client to a sign-in page it cannot use:

  * the front door's question (owner_session.door_decision) about POST /mcp,
    the metadata and the token, registration and revoke endpoints: 200 (was
    401 for /mcp and the token endpoint, 303 for a metadata GET asking for a
    page);
  * the door answers 200 for exactly those paths and methods: another method
    on one of them, a path next to one of them, or /mcp carrying either
    confined header is decided as any request without a session is (401);
  * the same requests, passed on, meet the localhost gate from the door's
    address and reach their handler (was 403 "sign in first");
  * the two confined lanes on /mcp stay confined: the harness capability or
    the internal secret from a peer that is neither this container's loopback
    nor the runtime's network is refused, and so is either from a loopback
    peer naming another machine (a relay, or a page whose name was re-pointed);
  * loopback never stands in for /mcp's token: the real handler, behind the
    real gates, answers a loopback call with no token 401 naming the sign-in;
  * the connect page and the approval page stay the person's: without a
    session a page is sent to sign in, and anything else is refused.

In-process; no database, no network. The handlers are stand-ins that only
say they were reached, except /mcp's own where named.
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile

os.environ["CSERIES_URL"] = "https://home.box.example"
os.environ["CSERIES_TRUSTED_PROXIES"] = "10.88.0.1/32"
os.environ.pop("CSERIES_RUNTIME_NET", None)
KEY_DIR = tempfile.mkdtemp(prefix="door-key-")
atexit.register(shutil.rmtree, KEY_DIR, True)
KEY_FILE = os.path.join(KEY_DIR, "door-secret")
KEY = "k3y-for-the-door-0123456789abcdefABCDEF_-~"
with open(KEY_FILE, "w") as _f:
    _f.write(KEY + "\n")
os.environ["CSERIES_DOOR_SECRET_FILE"] = KEY_FILE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402

from server.middleware import localhost_only as G  # noqa: E402
from server.middleware.app_origin_guard import AppOriginGuard  # noqa: E402
from server.middleware.host_header import HostHeaderGuard  # noqa: E402
from server.middleware.owner_session import OwnerSessionGate  # noqa: E402
from server.routes import owner as R  # noqa: E402

failed = 0
CHECKS = 0
PROXY = ("10.88.0.1", 41000)        # the door, a trusted proxy
DESKTOP = "home.box.example"
SIGN_IN = ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
           "/.well-known/oauth-authorization-server")
POSTS = ("/oauth/register", "/oauth/token", "/oauth/revoke")


def check(cond, what, detail=""):
    global failed, CHECKS
    CHECKS += 1
    print(("OK    " if cond else "FAIL  ") + what + ("" if cond else f"  {detail}"))
    if not cond:
        failed = 1


def build(real_mcp=False):
    app = FastAPI()
    app.include_router(R.router)

    async def reached():
        return {"reached": True}
    if real_mcp:
        from server.routes import mcp as M
        app.include_router(M.router)
    else:
        app.add_api_route("/mcp", reached, methods=["POST", "GET"])
    for p in POSTS:
        app.add_api_route(p, reached, methods=["POST"])
    for p in SIGN_IN + ("/oauth/authorize", "/agent-access/ask/abcdefghijklmnopqrst"):
        app.add_api_route(p, reached, methods=["GET"])
    app.add_middleware(G.LocalhostOnlyMiddleware)
    app.add_middleware(G.LocalhostOnlyWebSocketGate)
    app.add_middleware(OwnerSessionGate)
    app.add_middleware(AppOriginGuard)
    app.add_middleware(HostHeaderGuard)
    return app


async def run(app, method, path, *, peer, headers=(), host=DESKTOP):
    pairs = [("host", host)] + list(headers)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": [(k.lower().encode(), v.encode()) for k, v in pairs],
             "client": peer, "server": ("10.88.0.5", 8800)}
    sent, done = [], asyncio.Event()

    async def receive():
        if not getattr(receive, "given", False):
            receive.given = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "http.response.body" and not msg.get("more_body"):
            done.set()

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body


async def door(app, method, uri, accept="application/json", extra=()):
    # Caddy's forward_auth question carries a copy of the request's own
    # headers (`extra`) beside its own.
    status, headers, _ = await run(app, "GET", "/__host/door", peer=PROXY, headers=[
        ("accept", accept), ("x-forwarded-host", DESKTOP), ("x-forwarded-proto", "https"),
        ("x-cseries-door", KEY), ("x-forwarded-method", method), ("x-forwarded-uri", uri)]
        + list(extra))
    return status, headers.get("location")


async def main():
    from server.agent_access.oauth import SELF_AUTH_PATHS
    check(G.AGENT_PATHS == SELF_AUTH_PATHS | {"/mcp"},
          "the gates' agent paths are the sign-in's own self-answering paths and /mcp, nothing else",
          sorted(G.AGENT_PATHS ^ (SELF_AUTH_PATHS | {"/mcp"})))
    app = build()

    # ── the door's question ──
    st, loc = await door(app, "POST", "/mcp")
    check(st == 200, "door: POST /mcp passes to its own token check", (st, loc))
    for p in SIGN_IN:
        st, loc = await door(app, "GET", p, accept="text/html")
        check(st == 200 and loc is None, f"door: GET {p} passes, even asked as a page", (st, loc))
    for p in POSTS:
        st, loc = await door(app, "POST", p)
        check(st == 200, f"door: POST {p} passes to its own check", (st, loc))
    st, loc = await door(app, "POST", "/%6dcp")
    check(st == 200, "door: /mcp written encoded is read as the server routes it (/mcp)", (st, loc))
    # Exactly those methods: anything else on an agent path, asked without a
    # session, is refused at the door as the gate behind it refuses it.
    for m, p in [(m, "/mcp") for m in ("DELETE", "PUT", "PATCH", "OPTIONS", "HEAD")] \
            + [(m, p) for p in POSTS for m in ("GET", "PUT", "DELETE")] \
            + [(m, p) for p in SIGN_IN for m in ("POST", "PUT", "DELETE")]:
        st, loc = await door(app, m, p)
        check(st == 401, f"door: {m} {p} is not an agent's request and is refused without a session", (st, loc))
    # Exactly those paths.
    for p in ("/mcp/", "/MCP", "//mcp", "/mcp/x", "/mcp%2f", "/mcp%00", "/oauth/token/", "/oauth/tokenx",
              "/oauth/./token", "/oauth/x/../token", "/.well-known/oauth-authorization-server/x",
              "/.well-known/oauth-protected-resource/", "/.well-known/oauth-protected-resource/mcpx"):
        for m in ("POST", "GET"):
            st, loc = await door(app, m, p)
            check(st == 401, f"door: {m} {p} is no agent path and is refused without a session", (st, loc))
    # The confined lanes are never an outside caller's: the question about
    # /mcp carrying either header is refused, whichever method.
    for h in ("x-cseries-harness-cap", "x-cseries-internal-auth"):
        for m in ("POST", "GET"):
            st, loc = await door(app, m, "/mcp", extra=[(h, "from-outside")])
            check(st == 401, f"door: {m} /mcp carrying {h} is refused at the door", (st, loc))
    st, loc = await door(app, "GET", "/oauth/authorize?client_id=x", accept="text/html")
    check(st == 303 and loc in ("/login", "/setup"),
          "door: the connect page without a session goes to sign in (or setup, before an owner)", (st, loc))
    st, loc = await door(app, "GET", "/agent-access/ask/abcdefghijklmnopqrst", accept="text/html")
    check(st == 303, "door: the approval page without a session goes to sign in", (st, loc))
    st, loc = await door(app, "POST", "/oauth/authorize")
    check(st == 401, "door: the connect page's answer without a session is refused", (st, loc))
    st, loc = await door(app, "POST", "/api/agent-access/connections/1/revoke")
    check(st == 401, "door: Settings' revoke without a session is refused", (st, loc))

    # ── passed on, from the door's address ──
    st, _h, body = await run(app, "POST", "/mcp", peer=PROXY, headers=[("content-type", "application/json")])
    check(st == 200 and b"reached" in body, "gate: POST /mcp from the door reaches its handler", (st, body))
    for p in POSTS:
        st, _h, body = await run(app, "POST", p, peer=PROXY)
        check(st == 200 and b"reached" in body, f"gate: POST {p} from the door reaches its handler", (st, body))
    for p in SIGN_IN:
        st, h, body = await run(app, "GET", p, peer=PROXY, headers=[("accept", "text/html")])
        check(st == 200 and b"reached" in body, f"gate: GET {p} reaches its handler, never a redirect",
              (st, h.get("location")))
    st, _h, body = await run(app, "POST", "/mcp", peer=PROXY, headers=[("x-cseries-harness-cap", "cap")])
    check(st == 403, "gate: the harness capability from the door's address is refused", (st, body))
    st, _h, body = await run(app, "POST", "/mcp", peer=PROXY, headers=[("x-cseries-internal-auth", "s")])
    check(st == 403, "gate: the internal secret from the door's address is refused", (st, body))
    st, _h, body = await run(app, "POST", "/oauth/token", peer=PROXY,
                             headers=[("origin", "https://evil.example")])
    check(st == 403, "gate: a browser page from another site is refused on the sign-in paths", (st, body))
    st, _h, body = await run(app, "GET", "/oauth/authorize", peer=PROXY, headers=[("accept", "application/json")])
    check(st == 403, "gate: the connect page without a session is still the person's only", (st, body))
    loop = ("127.0.0.1", 5000)
    st, _h, body = await run(app, "POST", "/mcp", peer=loop, host="localhost:8800")
    check(st == 200 and b"reached" in body, "gate: loopback /mcp reaches the handler, whose token check decides",
          (st, body))
    # Each confined lane from where it lives: loopback naming this machine.
    for h in ("x-cseries-harness-cap", "x-cseries-internal-auth"):
        st, _h, body = await run(app, "POST", "/mcp", peer=loop, host="localhost:8800", headers=[(h, "v")])
        check(st == 200 and b"reached" in body, f"gate: {h} from loopback naming this machine reaches the handler",
              (st, body))
        # A loopback peer naming another machine is a relay or a page whose
        # name was re-pointed at 127.0.0.1, not an in-container caller.
        for name in ("rebound.example", DESKTOP):
            st, _h, body = await run(app, "POST", "/mcp", peer=loop, host=name, headers=[(h, "v")])
            check(st == 403, f"gate: {h} from loopback naming {name} is refused", (st, body))

    # Loopback never stands in for /mcp's token: the real handler behind the
    # real gates answers a call with no token 401 and names the sign-in, from
    # loopback as from the door.
    real = build(real_mcp=True)
    rpc = [("content-type", "application/json")]
    for who, peer, host in (("loopback naming this machine", loop, "localhost:8800"),
                            ("loopback naming another", loop, "rebound.example"),
                            ("the door's address", PROXY, DESKTOP)):
        st, h, body = await run(real, "POST", "/mcp", peer=peer, host=host, headers=rpc)
        check(st == 401 and "resource_metadata=" in h.get("www-authenticate", ""),
              f"real /mcp: from {who}, no token is 401 naming the sign-in", (st, h.get("www-authenticate"), body))
    st, h, body = await run(real, "POST", "/mcp", peer=loop, host="localhost:8800",
                            headers=rpc + [("x-cseries-harness-cap", "not-current")])
    check(st == 401, "real /mcp: a harness capability that is not current is 401 from loopback too", (st, body))

    # The /mcp page answers anyone, the internet through the remote door
    # included, with no token: it names the server and no version. The
    # Host's version is in initialize's serverInfo, behind the token
    # (test_agent_access_pg.py), as /api/system/version is behind the session.
    # On an https desktop the address for agents is that address.
    import json as _json
    from server.routes import mcp as _mcp
    from server.routes.system import _load_build_info
    built = str(_load_build_info().get("tag") or "dev")
    check(_mcp.SERVER_INFO.get("version") == built,
          f"initialize's serverInfo carries the Host's own version ({built})", _mcp.SERVER_INFO)
    for who, peer, host, hdrs in (("loopback", loop, "localhost:8800", []),
                                  ("the door", PROXY, DESKTOP, [("x-forwarded-proto", "https")])):
        st, _h, body = await run(real, "GET", "/mcp", peer=peer, host=host,
                                 headers=hdrs + [("accept", "application/json")])
        info = _json.loads(body or b"{}") if st == 200 else {}
        check(st == 200 and info.get("server") == {"name": "host"}
              and f'"{built}"' not in body.decode("utf-8", "replace"),
              f"real GET /mcp from {who}, with no token: names the server and not its version",
              (st, info.get("server")))
    st, _h, body = await run(real, "GET", "/mcp", peer=PROXY, host=DESKTOP,
                             headers=[("accept", "application/json"), ("x-forwarded-proto", "https")])
    info = _json.loads(body or b"{}") if st == 200 else {}
    check(info.get("address") == f"https://{DESKTOP}/mcp",
          "real GET /mcp through the door: an https desktop gives its own address to agents", info)

    # An agent on this computer (agent_access/origin.py), on a desktop whose
    # plain-http port is the scheme's own: a client writes http://127.0.0.1/mcp
    # (no :80, in its Host header and its resource), so that is the loopback
    # agent origin, the one whose connect page is the desktop's.
    from types import SimpleNamespace
    from server import home_origin
    from server.agent_access import origin as O
    saved = home_origin.HOME
    try:
        for url, port in (("http://home.cseries.localhost", ""), ("http://home.cseries.localhost:8800", ":8800")):
            home_origin.HOME = home_origin.parse(url)
            seen = O.request_origin(SimpleNamespace(headers={"host": f"127.0.0.1{port}"},
                                                    url=SimpleNamespace(scheme="http")))
            check(O.loopback_agent_origins() == {f"http://127.0.0.1{port}", f"http://localhost{port}",
                                                 f"http://[::1]{port}"}
                  and seen in O.loopback_agent_origins()
                  and O.authorization_origin(seen) == home_origin.HOME.origin
                  and O.agent_address(home_origin.HOME.origin) == f"http://127.0.0.1{port}/mcp"
                  and f"http://127.0.0.1{port}/mcp" in O.agent_resources(home_origin.HOME.origin),
                  f"on {url} the loopback agent origin is the one a client writes (http://127.0.0.1{port})",
                  (sorted(O.loopback_agent_origins()), seen, O.authorization_origin(seen)))
        home_origin.HOME = home_origin.parse("https://home.box.example")
        check(O.loopback_agent_origins() == frozenset() and O.authorization_origin("http://127.0.0.1:443")
              == "http://127.0.0.1:443" and O.agent_resources(home_origin.HOME.origin)
              == {home_origin.HOME.origin + "/mcp"},
              "an https desktop has no loopback agent origin: its connect page takes only its own resource")
    finally:
        home_origin.HOME = saved


asyncio.run(main())
print(f"\n{CHECKS} checks; {'FAILED' if failed else 'all passed'}")
sys.exit(failed)
