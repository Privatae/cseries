"""Refuse a request whose Host header could not be a host.

Starlette before 1.0.1 rebuilt request.url from the Host header unchecked
(CVE-2026-48710), so `Host: x/api/system/health?` made request.url.path read
/api/system/health whatever path the request was actually routed on. The localhost-only gate
exempted health probes by that value, so any peer that could reach :8800 got
past it to every route (found 2026-09-28). Every path check now reads
request.scope["path"], the path routing uses, and this gate, outermost,
refuses the malformed header itself.

request.scope["path"] is the only safe source for a decision about the path.
request.url can still differ from it behind this gate: an encoded `?` or `#`
in the path (`/api/system/health%3F/x`) is decoded into scope["path"] and then
read as a query or fragment when request.url is rebuilt.

A real Host is a name or an address with an optional port. None carries a
slash, backslash, `?`, `#`, `@`, or an ASCII space or control character.
Surrounding spaces and tabs are trimmed first: the production parser
(httptools) keeps trailing ones, and they are legal around a header value.

X-Forwarded-Host is removed here unless the request came from a proxy in
CSERIES_TRUSTED_PROXIES (server/trusted_proxies.py). The app-origin code
believes it before Host (app_origin.browser_host), and a page may set it on
its own fetch, so an isolated app's page could otherwise name the desktop and
be served what only the desktop may load.
"""
from __future__ import annotations

import logging

from server import trusted_proxies

log = logging.getLogger("cseries.host_header")

_NOT_IN_A_HOST = frozenset("/\\?#@")


def host_is_malformed(host: str) -> bool:
    return any(c in _NOT_IN_A_HOST or ord(c) < 0x21 or ord(c) == 0x7F for c in host.strip(" \t"))


class HostHeaderGuard:
    """Pure ASGI, so it covers WebSocket upgrades as well as HTTP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            for key, value in scope.get("headers") or ():
                if key == b"host" and host_is_malformed(value.decode("latin-1")):
                    client = (scope.get("client") or ("?",))[0]
                    log.warning("refused a malformed Host header from %s on %s", client, scope.get("path"))
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                        return
                    await send({"type": "http.response.start", "status": 400,
                                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                    await send({"type": "http.response.body", "body": b"malformed Host header"})
                    return
            headers = scope.get("headers") or ()
            if any(k == b"x-forwarded-host" for k, _v in headers) and \
                    not trusted_proxies.peer_is_trusted((scope.get("client") or (None,))[0]):
                scope["headers"] = [(k, v) for k, v in headers if k != b"x-forwarded-host"]
        await self.app(scope, receive, send)
