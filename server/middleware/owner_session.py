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

from urllib.parse import urlencode

from server import owner_login
from server.applications import app_origin
from server.home_origin import HOME
from server.middleware.localhost_only import (
    LOOPBACK_HOSTS, OAUTH_CALLBACK_PATHS, OWNER_PUBLIC_PATHS, _unmap_v4,
)

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


def _wants_page(scope) -> bool:
    if scope.get("method") != "GET":
        return False
    for key, value in scope.get("headers") or ():
        if key == b"accept":
            return b"text/html" in value
    return False


class OwnerSessionGate:

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        client = _unmap_v4((scope.get("client") or (None,))[0])
        path = scope.get("path") or ""
        if client in LOOPBACK_HOSTS or path in OWNER_PUBLIC_PATHS or path in OAUTH_CALLBACK_PATHS:
            return await self.app(scope, receive, send)

        email, _token = await live_session(scope)
        if email:
            headers = [(k, v) for k, v in scope.get("headers") or () if k != _IDENTITY_HEADER]
            headers.append((_IDENTITY_HEADER, email.encode("latin-1")))
            scope["headers"] = headers
            scope.setdefault("state", {})["owner_session"] = True
            return await self.app(scope, receive, send)

        if scope["type"] == "http" and _wants_page(scope):
            host = request_host(scope)
            if own_app_origin(host) and not owner_login.needs_setup():
                qs = (scope.get("query_string") or b"").decode("latin-1")
                target = join_url(host, path + (f"?{qs}" if qs else ""))
            else:
                target = "/setup" if owner_login.needs_setup() else "/login"
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", target.encode()),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        # No session: the localhost gate decides, and refuses a non-loopback
        # peer unless the path authenticates itself.
        return await self.app(scope, receive, send)
