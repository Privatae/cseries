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


# ── the address an agent on this computer uses ──────────────────────────────
#
# An installed Host's desktop is plain http on a *.localhost name
# (http://home.cseries.localhost:8800). MCP clients refuse to send a token
# request over plain http to any name but localhost, 127.0.0.1 and ::1
# (Claude Code: "OAuth token requests MUST use TLS (localhost / 127.0.0.1 /
# ::1 are exempt)"), and a program that is not a browser may not resolve a
# *.localhost name at all. The VM forwards the desktop's port to this
# computer's 127.0.0.1 (the installer reaches the Host there), so an agent on
# this computer uses http://127.0.0.1:<port>/mcp. On that address the
# metadata, registration, token and revocation stay on 127.0.0.1, and the
# issuer is 127.0.0.1's; only the connect page is the desktop's own, where the
# person is signed in and their passkey works (a passkey belongs to the
# address it was made on). RFC 8414 lets an authorization endpoint live on
# another host, and MCP 2025-11-25 says the authorization server may be
# separate from the resource. The client names the address it uses in the
# RFC 8707 `resource` parameter, which MCP requires on both the authorize and
# the token request: the connect page takes it only when it is one of this
# Host's agent addresses, sends it back as `iss`, and the code and its tokens
# are for that address alone.
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def _loopback(name: str) -> str | None:
    """http://<name>[:<the desktop's port>], written as a client writes it
    (the port left out when it is 80, as in the Host header and in the
    resource a client sends, which request_origin compares with), or None
    when the desktop's own address is https (a TLS front end or the cloud
    box: agents use that)."""
    from server import home_origin
    home = home_origin.HOME
    if home.scheme != "http":
        return None
    return f"http://{name}" + (f":{home.port}" if home.port else "")


def loopback_agent_origin() -> str | None:
    """http://127.0.0.1:<the desktop's port>, or None when the desktop's own
    address is https."""
    return _loopback("127.0.0.1")


def loopback_agent_origins() -> frozenset[str]:
    """The loopback agent origin under each loopback name."""
    if loopback_agent_origin() is None:
        return frozenset()
    return frozenset(_loopback(name) for name in LOOPBACK_NAMES)


def authorization_origin(origin: str) -> str:
    """Where the connect page is for a client that found this Host at
    `origin`: the desktop's own address for a loopback agent origin, else
    `origin` itself."""
    if origin in loopback_agent_origins():
        from server import home_origin
        return home_origin.HOME.origin
    return origin


def agent_resources(page_origin: str) -> frozenset[str]:
    """The `resource` values (RFC 8707) the connect page at `page_origin`
    accepts: its own address's /mcp and, on the desktop's own address, the
    loopback agent origins' (whose connect page it is)."""
    from server import home_origin
    out = {page_origin + "/mcp"}
    if page_origin == home_origin.HOME.origin:
        out |= {o + "/mcp" for o in loopback_agent_origins()}
    return frozenset(out)


def issuer_for(resource: str, page_origin: str) -> str:
    """The issuer a client that asked for `resource` discovered: the origin of
    that resource (one agent_resources accepted), else the page's own."""
    if resource and resource.endswith("/mcp") and resource in agent_resources(page_origin):
        return resource[:-len("/mcp")]
    return page_origin


def agent_address(origin: str) -> str:
    """The /mcp address to give a person for their agents, as seen from
    `origin` (an address of this Host): an https address as it is, else the
    loopback agent origin."""
    if origin.startswith("https://"):
        return origin + "/mcp"
    return (loopback_agent_origin() or origin) + "/mcp"


def zone_origin(request) -> str:
    """The desktop origin of the zone the request's name is in (the desktop
    itself or one of its app origins), else the request's own origin."""
    try:
        from server import home_origin
        from server.applications import app_origin
        name = (app_origin.browser_host(request.headers.raw) or "").split(":")[0].lower()
        for o in home_origin.ORIGINS:
            if name == o.host or name.endswith(o.zone):
                return o.origin
    except Exception:  # noqa: BLE001 — the request's own origin still serves
        pass
    return request_origin(request)
