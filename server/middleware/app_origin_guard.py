"""An app's origin serves that app and nothing else.

Each isolated app is framed from its own origin, apps-<key>-<handle><zone>
(server/applications/app_origin.py). That only walls apps apart if each origin
runs only its own app's code. The pod would otherwise answer any path on any
name: /system-apps/<id>/ for every app (applications/proxy.py serve_app has no
host check), /user-apps (the Host's project files, main.py), and the
entity-writable custom/ overlay (main.py OverlayStaticFiles). The key in the
label is public — a hash of the app id — so without this an app could frame
https://apps-<key of settings>-<handle>/system-apps/<itself>/x.html and run its
own code on Settings' origin.

On an app origin this answers 404 (HTTP) or closes 1008 (WebSocket) for every
path app_origin.path_allowed does not name. On the old shared origin,
apps-<handle>, and on any other "apps-" label in a zone, it answers 404 for
everything: a desktop on this release never frames there.

It also marks the scope (scope["app_origin"]) so the static overlay serves the
shared files from the image's base/ only on an app origin.

Pure ASGI, so it covers WebSocket upgrades as well as HTTP (a
BaseHTTPMiddleware never sees an upgrade), and it decides on scope["path"], the
path routing uses (host_header.py says why request.url cannot be trusted).
"""
from __future__ import annotations

import logging

from server.applications import app_origin

log = logging.getLogger("cseries.app_origin_guard")

_REFUSAL_HEADERS = [
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"x-content-type-options", b"nosniff"),
    (b"content-security-policy", b"sandbox; default-src 'none'"),
    (b"cache-control", b"no-store"),
]


class AppOriginGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        origin = app_origin.classify(app_origin.browser_host(scope.get("headers") or ()))
        if origin.kind in (app_origin.DESKTOP, app_origin.OTHER):
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if origin.kind == app_origin.APP and app_origin.path_allowed(origin, path):
            scope["app_origin"] = origin
            return await self.app(scope, receive, send)
        log.info("app origin %s refused %s (%s)", origin.kind, path[:120],
                 origin.key or origin.handle or "-")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = (b"not served on this app's address\n" if origin.kind == app_origin.APP
                else b"this app address is retired; reload the desktop\n"
                if origin.kind == app_origin.LEGACY_APP
                else b"unknown app address\n")
        await send({"type": "http.response.start", "status": 404, "headers": _REFUSAL_HEADERS})
        await send({"type": "http.response.body", "body": body})
