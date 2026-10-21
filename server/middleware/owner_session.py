"""The owner's session on requests from outside the container.

Pure ASGI, so it sees WebSocket upgrades as well as HTTP. It sits inside
FrontDoorProof (which removes any person header a request brought with it)
and outside the localhost gate.

A request carrying a live owner session cookie (server/owner_login.py) is
marked as the owner's: the owner's email is put in the identity header the
routes already read (server/operator_identity.request_email), and
scope["state"]["owner_session"] tells the localhost gate to let it through.
Everything else passes on unchanged and meets the localhost gate, which
refuses any peer that is not loopback. The one thing added for a person
without a session is a redirect: a browser opening a page is sent to the
sign-in page (or the setup page, before there is an owner) instead of
getting a JSON 403. A page on one of this Host's app origins is sent instead
to the desktop's /__host/join, which issues that origin a session of its own
when the desktop has one (server/routes/owner.py).

A session counts only on the host name it was issued on
(owner_login.session_email), read from Host — X-Forwarded-Host too, but only
from a trusted proxy: HostHeaderGuard removes anyone else's.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from urllib.parse import unquote, urlencode

from server import owner_login, trusted_proxies
from server.applications import app_origin
from server.home_origin import HOME
from server.middleware.localhost_only import (
    DOOR_PATH, LOOPBACK_HOSTS, OWNER_PUBLIC_PATHS, _PROBE_EXEMPT_PATHS, _unmap_v4, is_oauth_callback,
    runtime_peer,
)

log = logging.getLogger("cseries.owner_session")

_IDENTITY_HEADER = b"x-authentik-email"
# A page on a parent domain (an app origin's, say) can set a cookie of the
# same name for the whole zone, so every value sent is tried, up to this many.
_MAX_TRIED = 4


def _cookies(scope) -> list[str]:
    """The session cookie's values, in the order sent. Split by hand, not
    with SimpleCookie: that stops at the first cookie it rejects (a JSON
    value, a space), and cookies are shared by every port of a host, so any
    other program served on that name could otherwise hide the owner's
    session."""
    want = owner_login.cookie_name()
    out = []
    for key, value in scope.get("headers") or ():
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            name, sep, val = part.partition("=")
            val = val.strip().strip('"')
            if sep and name.strip() == want and val:
                out.append(val)
    return out[:_MAX_TRIED]


def _cookie(scope) -> str | None:
    """The first session cookie value sent, for the routes that act on one
    (sign-out)."""
    values = _cookies(scope)
    return values[0] if values else None


def request_host(scope) -> str:
    return app_origin.browser_host(scope.get("headers") or ())


async def live_session(scope) -> tuple[str | None, str | None]:
    """(email, token) for the first cookie value that is a live session on
    this request's host, else (None, None)."""
    host = request_host(scope)
    for value in _cookies(scope):
        email = await owner_login.session_email(value, host)
        if email:
            return email, value
    return None, None


def own_app_origin(host: str) -> bool:
    """Whether `host` is one of this Host's app origins (apps-<key>-<handle>
    in the home zone)."""
    o = app_origin.classify(host)
    return o.kind == app_origin.APP and o.handle == HOME.handle and o.zone == HOME.zone


def join_url(app_host: str, rd: str) -> str:
    return HOME.origin + "/__host/join?" + urlencode({"to": HOME.origin_of(app_host), "rd": rd})


def _wants_page(scope, method: str | None = None) -> bool:
    if (method if method is not None else scope.get("method")) != "GET":
        return False
    for key, value in scope.get("headers") or ():
        if key == b"accept":
            return b"text/html" in value
    return False


def _page_redirect(scope, path: str, query: str) -> str:
    """Where a browser opening a page without a session is sent: an app
    origin's page to the desktop's join, anything else to the sign-in page
    (or the setup page, before there is an owner)."""
    host = request_host(scope)
    if own_app_origin(host) and not owner_login.needs_setup():
        return join_url(host, path + (f"?{query}" if query else ""))
    return "/setup" if owner_login.needs_setup() else "/login"


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return ""


# ── the front door's question ───────────────────────────────────────────────
#
# A Host on a server of its own sits behind a reverse proxy on that server
# (Caddy), which asks GET /__host/door before it passes a request on
# (forward_auth). The question carries the browser's own headers (Host,
# Cookie, Accept, Origin) and the request it is about in X-Forwarded-Method
# and X-Forwarded-Uri. The answer is this gate's own decision for that
# request, so nothing reaches the Host from outside that the Host would not
# have let in itself: 200 for the paths a visitor without a session may
# reach (sign-in, setup, an app origin's session handoff, the OAuth return,
# the health probe) and for a live session on the browser's host; 303 to the
# sign-in page, or to the desktop's join for an app origin, for a browser
# opening a page without one; 401 for anything else. The proxy hands a non-2xx
# answer back to the browser as it is, Location and all (measured on Caddy
# 2.6.2).
#
# Who is answered: a trusted proxy (CSERIES_TRUSTED_PROXIES), and only on the
# question it asks itself. The same proxy also passes the browser's own
# requests on, and a browser can send X-Forwarded-Method and X-Forwarded-Uri
# of its own (Caddy 2.6.2 passes them through to the upstream unchanged), so
# the peer's address alone cannot tell the proxy's question from a browser's
# request for /__host/door. The question therefore carries the door key, the
# contents of the file CSERIES_DOOR_SECRET_FILE, in DOOR_KEY_HEADER; the
# proxy sets that header on its forward_auth request only (Caddy: header_up
# in the forward_auth block), and replaces any value a browser sent. Without
# the file, or without the key, nothing is answered but 401. The door never
# lets a request for /__host/door itself through, so only the proxy's own
# question reaches it.

DOOR_KEY_HEADER = b"x-cseries-door"
DOOR_KEY_FILE_ENV = "CSERIES_DOOR_SECRET_FILE"
# What a key may be: printable, no spaces, long enough not to be guessed.
# 32 random bytes in hex or base64 are 64 or 44 characters.
_DOOR_KEY_RE = re.compile(rb"[A-Za-z0-9._~+/=-]{32,512}")
# (path, mtime_ns, size, key or None): the file is read again only when it
# changes, so the box can replace the key without restarting the Host.
_door_key_cache: tuple | None = None


def _door_key() -> bytes | None:
    """The door key from CSERIES_DOOR_SECRET_FILE, or None when that is not
    set, cannot be read, or does not hold a key. Never logged."""
    global _door_key_cache
    path = os.environ.get(DOOR_KEY_FILE_ENV, "").strip()
    if not path:
        if _door_key_cache != ("", 0, 0, None):
            _door_key_cache = ("", 0, 0, None)
            log.error("the front door is answered with 401 only: %s is not set, so no "
                      "question can carry the door key", DOOR_KEY_FILE_ENV)
        return None
    try:
        st = os.stat(path)
    except OSError as exc:
        if _door_key_cache is None or _door_key_cache[:3] != (path, -1, -1):
            _door_key_cache = (path, -1, -1, None)
            log.error("the front door is answered with 401 only: %s %s cannot be read (%s)",
                      DOOR_KEY_FILE_ENV, path, exc.__class__.__name__)
        return None
    stamp = (path, st.st_mtime_ns, st.st_size)
    if _door_key_cache is not None and _door_key_cache[:3] == stamp:
        return _door_key_cache[3]
    key = None
    try:
        with open(path, "rb") as f:
            raw = f.read(1024).strip()
        if _DOOR_KEY_RE.fullmatch(raw):
            key = raw
        else:
            log.error("the front door is answered with 401 only: %s %s does not hold a key "
                      "(32 to 512 characters, letters, digits and ._~+/=-)", DOOR_KEY_FILE_ENV, path)
    except OSError as exc:
        log.error("the front door is answered with 401 only: %s %s cannot be read (%s)",
                  DOOR_KEY_FILE_ENV, path, exc.__class__.__name__)
    _door_key_cache = (*stamp, key)
    return key


def _carries_door_key(scope) -> bool:
    """Whether the request carries the door key, exactly once."""
    key = _door_key()
    if key is None:
        return False
    sent = [v for k, v in scope.get("headers") or () if k == DOOR_KEY_HEADER]
    return len(sent) == 1 and hmac.compare_digest(sent[0].strip(), key)


async def door_decision(scope) -> tuple[int, str | None]:
    """(status, Location or None) for the request a trusted proxy is asking
    about. A WebSocket handshake is asked about the same way and gets no
    redirect."""
    client = _unmap_v4((scope.get("client") or (None,))[0])
    if not trusted_proxies.peer_is_trusted(client) or not _carries_door_key(scope):
        return 401, None
    method = _header(scope, b"x-forwarded-method").strip().upper()
    uri = _header(scope, b"x-forwarded-uri").strip()
    if not method or not uri.startswith("/"):
        return 401, None
    raw_path, _sep, query = uri.partition("?")
    # Split and decoded as the server reads the path it routes (uvicorn: the
    # request target up to "?", unquoted), so the door and the Host agree on
    # which path a request is for; "//x" stays a path, not a host.
    path = unquote(raw_path)
    if path == DOOR_PATH:
        return 401, None        # only the proxy's own question reaches the door
    if path in OWNER_PUBLIC_PATHS or path in _PROBE_EXEMPT_PATHS:
        return 200, None
    if is_oauth_callback(path) and method == "GET":
        return 200, None        # the localhost gate lets a callback through for GET only
    email, _token = await live_session(scope)
    if email:
        return 200, None
    if scope.get("type") == "http" and _wants_page(scope, method):
        return 303, _page_redirect(scope, path, query)
    return 401, None


class OwnerSessionGate:

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        client = _unmap_v4((scope.get("client") or (None,))[0])
        path = scope.get("path") or ""
        if client in LOOPBACK_HOSTS or path in OWNER_PUBLIC_PATHS or is_oauth_callback(path):
            return await self.app(scope, receive, send)
        if runtime_peer(client):
            # A container of the local runtime has no browser session to make,
            # so it is never sent to the sign-in page: the localhost gate
            # decides, and passes only the few routes it may use.
            return await self.app(scope, receive, send)

        email, _token = await live_session(scope)
        if email:
            headers = [(k, v) for k, v in scope.get("headers") or () if k != _IDENTITY_HEADER]
            headers.append((_IDENTITY_HEADER, email.encode("latin-1")))
            scope["headers"] = headers
            scope.setdefault("state", {})["owner_session"] = True
            return await self.app(scope, receive, send)

        if scope["type"] == "http" and _wants_page(scope):
            qs = (scope.get("query_string") or b"").decode("latin-1")
            target = _page_redirect(scope, path, qs)
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", target.encode()),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        # No session: the localhost gate decides, and refuses a non-loopback
        # peer unless the path authenticates itself.
        return await self.app(scope, receive, send)
