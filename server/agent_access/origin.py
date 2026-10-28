"""This Host's own origin, as an outside agent reaches it.

The authorization server's issuer and the protected resource are both this
origin (RFC 8414 §2, RFC 9728 §2), and an MCP client checks the resource it is
told against the URL it used. That is the desktop origin whose name the
request is for: the Host's own address (home_origin.HOME) or its remote
address (home_origin.REMOTE). A request for any other name (a test client on
this machine) gets the origin it named itself.
"""
from __future__ import annotations

import re

_HOST = re.compile(r"[A-Za-z0-9.\-]+(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]+\](:[0-9]{1,5})?")


def request_origin(request) -> str:
    host = (request.headers.get("host") or "").strip()
    if not _HOST.fullmatch(host):
        host = "localhost"
    scheme = "https" if request.url.scheme == "https" else "http"
    return f"{scheme}://{host}"


async def public_origin(request) -> str:
    try:
        from server import home_origin
        from server.applications import app_origin
        name = app_origin.browser_host(request.headers.raw) or ""
        found = home_origin.desktop_origin(name.split(":")[0])
    except Exception:  # noqa: BLE001 — the request's own origin still serves
        found = None
    return found.origin if found is not None else request_origin(request)
