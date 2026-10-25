#!/usr/bin/env python3
"""The front door's question, GET /__host/door (server/middleware/
owner_session.py door_decision), through the Host's own gates.

A Host on a server of its own sits behind a reverse proxy that asks
/__host/door about every request before passing it on (Caddy forward_auth),
with the browser's headers and the request in X-Forwarded-Method and
X-Forwarded-Uri. Pinned here:

  1. Only a trusted proxy (CSERIES_TRUSTED_PROXIES) is answered; anyone
     else's X-Forwarded-* gets 401, whatever path it names.
  2. 200 for the paths a visitor without a session may reach: sign-in,
     setup, the passkey sign-ins, an app origin's session handoff, the OAuth
     return, the health probe; and for a live session on the browser's host.
  3. Without a session: a browser opening a page gets 303 to /login (or
     /setup before there is an owner), an app origin's page 303 to the
     desktop's join; anything else 401. A session issued on another host is
     no session here.
  4. The path is read as the server reads it ("%69" decoded, "//x" a path).
  5. A WebSocket handshake is asked about with a handshake (measured on
     Caddy 2.6.2): the gates let it reach the door, which answers with a
     plain HTTP status (the ASGI denial response) and never opens a socket;
     no redirect for a socket.
  6. The door answers on an app origin too (AppOriginGuard lets it through).
  7. The question must carry the door key (CSERIES_DOOR_SECRET_FILE) in
     X-Cseries-Door, once: the trusted proxy also passes browsers' own
     requests on, and a browser can send X-Forwarded-Uri of its own. No key
     file, an unreadable or short one, a missing, wrong or doubled header:
     401 whatever the path. A new key in the file is used without a restart.
     A question about /__host/door itself is never let through.
  8. The OAuth returns (integrations, gitea, MCP) are let through for GET
     without a session, and only for GET.
  9. The unlock page's ticket form (POST /api/owner/claim, box-sign-in)
     comes from the unlock page's origin, and the proxy's question carries
     that Origin: the door lets it through (the route checks the Origin
     itself), only with the key and from the trusted proxy. Every other
     path keeps the Origin rule at the door.

No database, no network: the session store is a stand-in.

    python3 scripts/test_host_door_unit.py           (from the repo root)
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

# An https Host, as on a server of its own. Set before the server modules
# read it at import.
os.environ["CSERIES_URL"] = "https://home.box.example"
os.environ["CSERIES_TRUSTED_PROXIES"] = "10.88.0.1/32"
# The door key, as the box mounts it: a file only the Host and the proxy read.
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

from server import owner_login as L  # noqa: E402
from server.applications import app_origin as AO  # noqa: E402
from server.middleware import localhost_only as G  # noqa: E402
from server.middleware.app_origin_guard import AppOriginGuard  # noqa: E402
from server.middleware.host_header import HostHeaderGuard  # noqa: E402
from server.middleware.owner_session import OwnerSessionGate  # noqa: E402
from server.routes import owner as R  # noqa: E402

failed = 0
PROXY = ("10.88.0.1", 41000)
STRANGER = ("203.0.113.7", 41000)
DESKTOP = "home.box.example"
APP = AO.app_host("settings", DESKTOP)


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


class Pool:
    """owner_sessions as a dict: token hash -> row."""

    def __init__(self):
        self.sessions = {}

    async def fetchrow(self, sql, *args):
        if "FROM owner_sessions s" in sql:
            return self.sessions.get(args[0])
        return None

    async def execute(self, sql, *args):
        return "UPDATE 1"


POOL = Pool()


def live(token, origin):
    now = datetime.now(timezone.utc)
    POOL.sessions[L._token_hash(token)] = {
        "last_seen_at": now, "expires_at": now + L.SESSION_MAX, "origin": origin,
        "parent": None, "parent_seen": None, "parent_expires": None}


def build():
    app = FastAPI()
    app.include_router(R.router)
    # main.py's order, innermost first.
    app.add_middleware(G.LocalhostOnlyMiddleware)
    app.add_middleware(G.LocalhostOnlyWebSocketGate)
    app.add_middleware(OwnerSessionGate)
    app.add_middleware(AppOriginGuard)
    app.add_middleware(HostHeaderGuard)
    return app


def _headers(pairs):
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in pairs]


async def ask(app, uri, *, method="GET", host=DESKTOP, peer=PROXY, accept="*/*", cookie=None,
              extra=(), key=KEY):
    """What the door says about `method uri` on `host`, asked from `peer`
    with the door key `key` (None: no key header)."""
    pairs = [("host", host), ("accept", accept), ("x-forwarded-host", host),
             ("x-forwarded-proto", "https"), ("x-forwarded-for", "198.51.100.20")]
    if key is not None:
        pairs.append(("x-cseries-door", key))
    if method is not None:
        pairs.append(("x-forwarded-method", method))
    if uri is not None:
        pairs.append(("x-forwarded-uri", uri))
    if cookie:
        pairs.append(("cookie", f"{L.cookie_name()}={cookie}"))
    pairs.extend(extra)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/__host/door", "raw_path": b"/__host/door",
             "query_string": b"", "root_path": "", "headers": _headers(pairs),
             "client": peer, "server": ("10.88.0.5", 8800)}
    sent, done = [], asyncio.Event()

    async def receive():
        if not sent and not getattr(receive, "given", False):
            receive.given = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "http.response.body" and not msg.get("more_body"):
            done.set()

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


async def ask_ws(app, uri, *, host=DESKTOP, peer=PROXY, cookie=None, origin=None, key=KEY):
    """The door's question for a WebSocket handshake: the proxy sends it as
    a handshake of its own. Returns ("http", status) for a denial response,
    or ("accept",)/("close", code)."""
    pairs = [("host", host), ("x-forwarded-host", host), ("x-forwarded-method", "GET"),
             ("x-forwarded-uri", uri), ("upgrade", "websocket"), ("connection", "Upgrade"),
             ("accept", "text/html")]
    if key is not None:
        pairs.append(("x-cseries-door", key))
    if cookie:
        pairs.append(("cookie", f"{L.cookie_name()}={cookie}"))
    if origin:
        pairs.append(("origin", origin))
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "scheme": "ws", "path": "/__host/door", "raw_path": b"/__host/door",
             "query_string": b"", "root_path": "", "headers": _headers(pairs),
             "client": peer, "server": ("10.88.0.5", 8800), "subprotocols": [],
             "extensions": {"websocket.http.response": {}}}
    sent, first = [], [True]

    async def receive():
        if first[0]:
            first[0] = False
            return {"type": "websocket.connect"}
        await asyncio.sleep(3600)

    async def send(msg):
        sent.append(msg)

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    for m in sent:
        if m["type"] == "websocket.http.response.start":
            return ("http", m["status"])
        if m["type"] == "websocket.accept":
            return ("accept",)
        if m["type"] == "websocket.close":
            return ("close", m.get("code"))
    return ("nothing",)


async def main():
    L._pool = lambda: POOL
    L._owner_email, L._cookie_suffix = "owner@example.com", "0a1b2c3d"
    L._cache.clear()
    app = build()
    check(L.cookie_name() == "__Host-cseries_session_0a1b2c3d", "the https Host's cookie is __Host- named")

    # 1. only a trusted proxy is answered
    st, _h = await ask(app, "/login", peer=STRANGER)
    check(st == 401, "a stranger asking about /login gets 401", st)
    st, _h = await ask(app, "/api/system/health", peer=STRANGER)
    check(st == 401, "a stranger gets 401 for any path", st)
    st, _h = await ask(app, None)
    check(st == 401, "no X-Forwarded-Uri: 401", st)
    st, _h = await ask(app, "/login", method=None)
    check(st == 401, "no X-Forwarded-Method: 401", st)
    st, _h = await ask(app, "login")
    check(st == 401, "a URI that is not a path: 401", st)

    # 2. the paths a visitor without a session may reach
    for uri in ("/login", "/setup", "/api/owner/login", "/api/owner/setup", "/api/owner/status",
                "/api/owner/passkey/login/options", "/api/owner/passkey/login/verify",
                "/api/owner/recovery/login", "/api/system/health",
                "/api/mcp/oauth/callback?code=x&state=y", "/__host/renew"):
        st, _h = await ask(app, uri, method="POST" if "verify" in uri or uri.endswith("login") else "GET")
        check(st == 200, f"no session, {uri}: 200", st)
    st, _h = await ask(app, "/__host/session?code=abc&rd=%2F", host=APP)
    check(st == 200, "an app origin's session handoff without a session: 200", st)

    # 3. without a session
    st, h = await ask(app, "/", accept="text/html,application/xhtml+xml")
    check(st == 303 and h.get("location") == "/login" and h.get("cache-control") == "no-store",
          "a page without a session: 303 to /login, not cached", (st, h))
    st, h = await ask(app, "/system-apps/settings/index.html?tab=keys", host=APP,
                      accept="text/html")
    loc = urlsplit(h.get("location", ""))
    q = parse_qs(loc.query)
    check(st == 303 and f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://home.box.example/__host/join"
          and q.get("to") == [f"https://{APP}"] and q.get("rd") == ["/system-apps/settings/index.html?tab=keys"],
          "an app origin's page without a session: 303 to the desktop's join", (st, h))
    st, _h = await ask(app, "/api/network/bastions/setup", method="POST")
    check(st == 401, "an API call without a session: 401", st)
    st, _h = await ask(app, "/", method="POST", accept="text/html")
    check(st == 401, "a form POST without a session: 401, not a redirect", st)
    L._owner_email = None
    st, h = await ask(app, "/", accept="text/html")
    check(st == 303 and h.get("location") == "/setup", "before there is an owner, a page goes to /setup", (st, h))
    L._owner_email = "owner@example.com"

    # 2. with a live session
    live("tok-desktop", DESKTOP)
    st, _h = await ask(app, "/api/network/bastions/setup", method="POST", cookie="tok-desktop",
                       extra=[("origin", "https://home.box.example")])
    check(st == 200, "a live session on the desktop: 200 for an API call", st)
    st, _h = await ask(app, "/api/x", cookie="tok-desktop", host=APP)
    check(st == 401, "the desktop's session is no session on an app origin", st)
    live("tok-app", APP)
    st, _h = await ask(app, "/api/x", cookie="tok-app", host=APP)
    check(st == 200, "an app origin's own session: 200 there", st)
    st, _h = await ask(app, "/api/x", cookie="made-up")
    check(st == 401, "a cookie that is no session: 401", st)
    st, _h = await ask(app, "/api/x", method="POST", cookie="tok-desktop",
                       extra=[("origin", "https://evil.example")])
    check(st == 403, "a request from another site is refused at the door as the Host would", st)

    # 4. the path as the server reads it
    st, _h = await ask(app, "/log%69n")
    check(st == 200, "/log%69n is /login, as the server routes it", st)
    st, _h = await ask(app, "//login", accept="application/json")
    check(st == 401, "//login is a path, not a host named login", st)
    st, _h = await ask(app, "/login/../api/x", accept="application/json")
    check(st == 401, "a dot-segment path is not the public path it starts with", st)

    # 5. WebSocket handshakes
    check(G.websocket_refusal({"type": "websocket", "path": "/__host/door", "client": PROXY,
                               "headers": []}) is None,
          "the WebSocket gate lets the door's own handshake reach the door")
    got = await ask_ws(app, "/ws", cookie="tok-desktop", origin="https://home.box.example")
    check(got == ("http", 200), "a socket with a live session: 200, no socket opened", got)
    got = await ask_ws(app, "/ws")
    check(got == ("http", 401), "a socket without a session: 401, no redirect", got)
    got = await ask_ws(app, "/ws", cookie="tok-desktop", peer=STRANGER)
    check(got == ("http", 401), "a stranger's handshake to the door: 401", got)

    # 6. on an app origin the door is reachable
    st, _h = await ask(app, "/system-apps/settings/app.js", host=APP, cookie="tok-app")
    check(st == 200, "the door answers on an app origin", st)

    # 7. the door key
    st, _h = await ask(app, "/login", key=None)
    check(st == 401, "the trusted proxy's peer without the door key: 401 for a public path", st)
    st, _h = await ask(app, "/api/x", cookie="tok-desktop", key=None)
    check(st == 401, "without the door key a live session is no answer either", st)
    st, _h = await ask(app, "/login", key="k3y-for-the-door-0123456789abcdefABCDEF_-X")
    check(st == 401, "a wrong door key: 401", st)
    st, _h = await ask(app, "/login", key=KEY[:-1])
    check(st == 401, "a door key one character short: 401", st)
    st, _h = await ask(app, "/login", extra=[("x-cseries-door", KEY)])
    check(st == 401, "the door key sent twice: 401", st)
    st, _h = await ask(app, "/login", key="{file." + KEY_FILE + "}")
    check(st == 401, "an unexpanded placeholder (Caddy 2.6.2 has no {file.*}): 401", st)
    st, _h = await ask(app, "/login", key=KEY, peer=STRANGER)
    check(st == 401, "the door key from a peer that is no trusted proxy: 401", st)
    got = await ask_ws(app, "/ws", cookie="tok-desktop", origin="https://home.box.example", key=None)
    check(got == ("http", 401), "a socket question without the door key: 401", got)
    # A browser's own request for /__host/door, passed on by the proxy with
    # its own X-Forwarded-*: the proxy asks about /__host/door first, and the
    # door never lets that through; the request itself carries no key.
    st, _h = await ask(app, "/__host/door", cookie="tok-desktop")
    check(st == 401, "a question about /__host/door itself: 401, even with a session", st)
    st, _h = await ask(app, "/__host/%64oor", accept="text/html")
    check(st == 401, "...and spelled with an escape: 401, no redirect", st)
    st, _h = await ask(app, "/api/x", cookie="tok-desktop", key=None,
                       extra=[("x-forwarded-uri", "/login")])
    check(st == 401, "a browser's request for /__host/door naming a public path: 401", st)
    # The key file is the box's: a new key is used once written, the old one not.
    new_key = "an0ther-k3y-for-the-door-0123456789abcdef"
    with open(KEY_FILE, "w") as f:
        f.write(new_key)
    os.utime(KEY_FILE, ns=(1, 1))
    st_old, _h = await ask(app, "/login")
    st_new, _h = await ask(app, "/login", key=new_key)
    check(st_old == 401 and st_new == 200, "a new key in the file is used without a restart",
          (st_old, st_new))
    with open(KEY_FILE, "w") as f:
        f.write("short")
    st, _h = await ask(app, "/login", key="short")
    check(st == 401, "a key file holding too short a key: 401 for every question", st)
    os.environ["CSERIES_DOOR_SECRET_FILE"] = os.path.join(KEY_DIR, "missing")
    st, _h = await ask(app, "/login")
    check(st == 401, "an unreadable key file: 401 for every question", st)
    os.environ.pop("CSERIES_DOOR_SECRET_FILE")
    st, _h = await ask(app, "/login")
    check(st == 401, "no CSERIES_DOOR_SECRET_FILE: the door answers nobody", st)
    with open(KEY_FILE, "w") as f:
        f.write(KEY)
    os.environ["CSERIES_DOOR_SECRET_FILE"] = KEY_FILE
    st, _h = await ask(app, "/login")
    check(st == 200, "with the key file back, the door answers again", st)

    # 8. the OAuth returns, without a session
    for uri in ("/api/integrations/callback/gmail?code=c&state=s",
                "/api/gitea/oauth/callback?code=c&state=s",
                "/api/mcp/oauth/callback?code=c&state=s"):
        st, _h = await ask(app, uri, accept="text/html")
        check(st == 200, f"no session, GET {uri.split('?')[0]}: 200", st)
        st, _h = await ask(app, uri, method="POST")
        check(st == 401, f"no session, POST {uri.split('?')[0]}: 401", st)
    for uri in ("/api/integrations/callback/", "/api/integrations/callback/gmail/x",
                "/api/integrations/callback/_google_base", "/api/integrations/callback/Gmail"):
        st, _h = await ask(app, uri)
        check(st == 401, f"no session, GET {uri}: not an OAuth return, 401", st)

    # 9. the unlock page's ticket form (routes/owner.py _from_unlock_page):
    # the proxy asks about it with the browser's headers, Origin among them,
    # and that Origin is the unlock page's, not the Host's
    unlock = "https://unlock-" + DESKTOP
    for uri in ("/api/owner/claim", "/api/owner/box-sign-in", "/api/owner/cl%61im"):
        st, _h = await ask(app, uri, method="POST", accept="text/html", extra=[("origin", unlock)])
        check(st == 200, f"the unlock page's form to {uri}, its Origin forwarded: 200 (the route checks it)", st)
    st, _h = await ask(app, "/api/owner/claim", method="POST", extra=[("origin", unlock)], key=None)
    check(st == 401, "the same question without the door key: 401", st)
    st, _h = await ask(app, "/api/owner/claim", method="POST", extra=[("origin", unlock)], peer=STRANGER)
    check(st == 401, "and from a stranger: 401", st)
    for uri in ("/api/owner/login", "/api/owner/setup", "/login"):
        st, _h = await ask(app, uri, method="POST", extra=[("origin", unlock)])
        check(st == 403, f"another path with that Origin keeps the Origin rule at the door: {uri} 403", st)
    st, _h = await ask(app, "/api/owner/login", method="POST", extra=[("origin", "https://" + DESKTOP)])
    check(st == 200, "and the Host's own Origin passes there as before", st)


asyncio.run(main())
sys.exit(failed)
