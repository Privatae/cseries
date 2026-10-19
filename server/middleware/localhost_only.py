"""
Localhost-Only Middleware — Rejects any request not from 127.0.0.1 or ::1,
EXCEPT:
  1. Requests that came through the in-pod mTLS front terminator
     (server/mtls_frontdoor.py) carrying a VERIFIED client-cert CN header,
     and ONLY when that CN is the dashboard router (DASHBOARD_ROUTER_CN).
     The terminator owns :8443, verifies the CEIGAS client cert, strips any
     client-supplied CN, and injects the real one — so the header is
     trustworthy only when the immediate peer is loopback (the terminator).
     A peer entity's `domain-<id>` cert is NOT the router CN → no dashboard
     trust → 403 (Layer 2 of the cross-tenant fix; closes the
     "any ceigas cert on :8443 was trusted" hole even if the network-layer
     Layer-1 deny is ever mis-set).
  2. Requests carrying the owner's signed-in session, which
     server/middleware/owner_session.py marks in scope["state"]. A peer's
     address alone never grants access: on a published Docker port every
     visitor arrives from the bridge gateway.
  3. The MCP protocol surface (/mcp) — it carries its OWN bearer +
     hardware-fingerprint auth, entity-bound via mcp_client_tokens
     (server/routes/mcp.py:_auth_or_401). MCP clients (the user's gateway,
     Claude Desktop/Code/Cursor) reach the host over mTLS presenting an
     operator/machine cert, NEVER the dashboard router CN, so the item-1
     router-CN gate would wrongly 403 them. /mcp is let through to its own
     auth, which still rejects a cross-tenant caller (no valid token+fingerprint
     for THIS entity). The dashboard cross-tenant lock (item 1) is unchanged —
     every other path stays router-CN-only on the mTLS path.

This is the FIRST line of defense for the HTTP-only dashboard listener.
Defense-in-depth — port :8800 is also bound only to 127.0.0.1 on the host.
"""

import ipaddress
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

# The ONLY client-cert identity granted the dashboard bypass on the mTLS path.
# This is the CN the dashboard router presents when it proxies a Host's
# dashboard to the pod. Any other verified cert (a peer entity's domain-<id>
# cert) gets no dashboard trust. No default: unset, no cert gets the bypass.
ROUTER_CN = os.environ.get("DASHBOARD_ROUTER_CN", "").strip()

# Header injected by the in-pod mTLS front terminator (server/mtls_frontdoor.py)
# carrying the VERIFIED client-cert CN. Loopback alone never proved it: any
# caller on the pod's loopback could write it. It reaches this gate
# only on a request the front door forwarded — FrontDoorProof
# (server/middleware/router_identity.py, outside this gate) removes it from
# anything else. The loopback test below stays as a second condition.
_CLIENT_CN_HEADER = "x-client-cert-cn"

# Origins allowed for CSRF purposes besides the dashboard's own: a
# comma-separated list of hostnames, for a Host reached through a proxy under
# another name.
_EXTRA_ORIGIN_HOSTS = {h.strip().lower() for h in
                       os.environ.get("CSERIES_ALLOWED_ORIGINS", "").split(",") if h.strip()}

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_parts(origin: str) -> tuple[str, int | None] | None:
    """(hostname, port) of an Origin header, the port None when it is the
    scheme's default. None for "null" and anything that is not http(s)."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(origin.strip())
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return None
    if not host or u.scheme not in _DEFAULT_PORTS:
        return None
    return host, (None if port == _DEFAULT_PORTS[u.scheme] else port)


def _host_parts(host_header: str | None) -> tuple[str, int | None] | None:
    """(hostname, port) of a Host header. 80 and 443 read as no port: a
    browser leaves its scheme's default out of Origin, and this hop may not
    know which scheme the browser used."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit("//" + (host_header or "").strip())
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return None
    if not host:
        return None
    return host, (None if port in (80, 443) else port)


def origin_allowed(origin: str, host_header: str | None, *, own_origin: bool) -> bool:
    """Whether a request whose browser sent `origin` may proceed.

    own_origin=True is the browser lane (a request carrying the owner's
    session, or the sign-in pages): the origin must be the dashboard's own,
    host AND port, or a name in CSERIES_ALLOWED_ORIGINS. Cookies are shared
    by every port of a host and SameSite counts every port as the same site,
    so a page some other program serves on localhost:3000 sends the owner's
    cookie with its requests; its Origin names port 3000, and that is what
    refuses it.

    own_origin=False is the loopback lane, which needs no session: only
    localhost names or CSERIES_ALLOWED_ORIGINS. The request's own Host does
    not count there, because a page whose DNS name was re-pointed at
    127.0.0.1 sends that name as both Host and Origin; and the port must be
    the dashboard's, so a page another program serves on localhost:3000 is
    refused."""
    parts = _origin_parts(origin)
    if parts is None:
        return False
    if parts[0] in _EXTRA_ORIGIN_HOSTS:
        return True
    if own_origin:
        return parts == _host_parts(host_header)
    host = _host_parts(host_header)
    return parts[0] in ALLOWED_HOSTS and host is not None and parts[1] == host[1]


def loopback_host_ok(host_header: str | None) -> bool:
    """Whether a session-free loopback request names this machine in its Host
    header: a localhost name or one in CSERIES_ALLOWED_ORIGINS."""
    host = _host_parts(host_header)
    return host is not None and (host[0] in ALLOWED_HOSTS or host[0] in _EXTRA_ORIGIN_HOSTS)


def _unmap_v4(host: str | None) -> str | None:
    """Collapse an IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) to its IPv4
    form (`127.0.0.1`).

    The dashboard listener is bound dual-stack (server/launch.py), so an IPv4
    client reaches this middleware wearing its v4-mapped v6 spelling. Both
    checks below are written in IPv4 terms — `ALLOWED_HOSTS` names
    "127.0.0.1" — so without this every IPv4 caller 403s, including the
    image's own HEALTHCHECK (dials 127.0.0.1).

    This widens nothing: it makes the addresses those rules ALREADY name
    match when they arrive over the dual-stack socket. Anything outside
    loopback is still refused, and a mapped address that is not loopback
    still falls through to the same rejection.
    """
    if not host:
        return host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return host


def _is_allowed(host: str | None) -> bool:
    """Allow loopback only. A request from anywhere else needs the owner's
    session (server/middleware/owner_session.py)."""
    # Normalise here too, not only at the call site: this function is the
    # actual boundary, and a caller that forgets would silently 403 every
    # IPv4 peer on the dual-stack socket. Idempotent, so double-normalising
    # from dispatch() costs nothing.
    host = _unmap_v4(host)
    if not host:
        return False
    return host in ALLOWED_HOSTS


def _mtls_client_cn(request: Request, client_host: str | None) -> str | None:
    """The VERIFIED client-cert CN if this request came through the in-pod mTLS
    front terminator, else None. The terminator (server/mtls_frontdoor.py) runs
    on loopback, verifies the CEIGAS client cert, STRIPS any client-supplied CN
    and injects the real one — so the header is only trustworthy when the
    immediate peer is loopback. A header arriving from any non-loopback source
    is ignored (it can only be a forgery; the genuine path is always loopback)."""
    cn = request.headers.get(_CLIENT_CN_HEADER)
    if cn is None:
        return None
    if client_host not in LOOPBACK_HOSTS:
        return None
    return cn


# Health endpoints exempt from the source-IP gate. Kubernetes liveness/
# readiness probes come from the kubelet on the node — not from
# localhost, not from a docker-bridge gateway. Without this exemption,
# every k8s probe gets 403 and the pod restart-loops (caught on a
# rollout). The endpoints return only uptime + a
# health status object, so external exposure here is low-risk.
_PROBE_EXEMPT_PATHS = {
    "/api/system/health",
}

# Paths whose handler carries its OWN bearer + hardware-fingerprint auth
# (entity-bound via mcp_client_tokens — see server/routes/mcp.py:_auth_or_401).
# MCP clients (the user's gateway, Claude Desktop/Code/Cursor) reach the
# host over mTLS with an operator/machine cert, NEVER the dashboard router CN,
# so the router-CN gate below would wrongly 403 them. These are let through to
# their own auth, which still refuses a cross-tenant caller (no valid
# token+fingerprint for THIS entity). The dashboard cross-tenant lock is
# unchanged: every other path stays router-CN-only on the mTLS path.
# /api/network/bastions/heartbeat is also self-authenticating: a bastion reaches it
# over its tunnel + its own operator cert (NOT the router CN), carrying its bastion
# bearer (server/routes/network.py:bastion_heartbeat derives token_id from that
# bearer). Let it through to that check, same as /mcp; the route still refuses any
# bearer that isn't an active bastion token.
# /api/network/bastions/dcn-propose is the same posture: the `cseries-bastion
# add-folder` draft lane, bearer-authed in-handler (server derived from the
# bearer's own heartbeat — a bastion can only propose folders on itself), and
# the draft is inert until the user's step-up approval.
# /api/network/bastions/context-source is that posture again (the
# extraction PULL path): the bastion's own bearer, PLUS a one-shot ticket the
# host minted for that machine and that one document. The route takes no ctx_id
# at all, so letting it through to its own check grants no read — a machine with
# a valid bearer and no live ticket gets a 404.
# The owner's sign-in and setup pages and their API: reachable without a
# session from any peer, since that is how a session is made. They still meet
# the Origin check below. server/middleware/owner_session.py reads this too.
# /__host/session and /__host/renew are an app origin's: the first redeems a
# one-time join code the desktop made (or sends the frame to get one), the
# second answers whether this origin's session is live. Both check the
# session themselves (server/routes/owner.py).
OWNER_PUBLIC_PATHS = frozenset({"/login", "/setup", "/api/owner/login", "/api/owner/setup",
                                "/api/owner/status", "/api/owner/logout",
                                "/__host/session", "/__host/renew"})

# An MCP server's sign-in coming back (server/mcp_oauth.py). The authorization
# server sends the browser to the loopback address (http://127.0.0.1:<port>),
# where it has no session: an authorization server sends a code back only to
# https or to the loopback, and the desktop's *.localhost name is neither. The
# handler acts only on a pending sign-in this Host started, found by the hash
# of its state, used once, within ten minutes; the code is useless without
# that sign-in's PKCE verifier. server/middleware/owner_session.py reads this
# too, so the browser is not sent to the sign-in page instead.
OAUTH_CALLBACK_PATHS = frozenset({"/api/mcp/oauth/callback"})

_MCP_CLIENT_PATHS = {"/mcp", "/api/network/bastions/heartbeat",
                     "/api/network/bastions/dcn-propose",
                     "/api/network/bastions/context-source",
                     "/api/network/bastions/git-credential"}   # bastion bearer, like heartbeat

# Paths reachable cross-pod over plain HTTP (NOT the mTLS front terminator, so the
# _MCP_CLIENT_PATHS bypass above — which lives inside the verified-CN branch — never
# fires for them) that carry their OWN in-handler auth: a caller without the live
# capability 403s in the handler regardless of source IP, and the same-entity CNP
# already fences who can reach :8800 at all. Let them past the source-IP gate to
# that check — same posture as the k8s probe exemption, but self-authenticating
# rather than open.
# The messaging connectors:
# a connector app's process-form pod, cross-pod, plain HTTP, with no app token;
# every route under the prefix depends on routes/connector.require_connector_cap,
# the per-app capability constant-time checked against the grant in the database
# (scripts/test_connector_gates.py fails a route under it that does not).
_SELF_AUTH_PREFIXES = ("/api/connector/",)


class LocalhostOnlyMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        # Normalise before ANY source check: the listener is dual-stack, so an
        # IPv4 peer arrives as `::ffff:127.0.0.1` and neither the loopback set
        # nor the bridge-range test would recognise it. See _unmap_v4.
        client_host = _unmap_v4(request.client.host if request.client else None)
        # The path routing serves, never request.url.path: that is rebuilt from
        # the Host header, so `Host: x/api/system/health?` once passed every
        # exemption below for any route (middleware/host_header.py).
        path = request.scope["path"]

        # mTLS path: a verified client-cert CN delivered by the in-pod front
        # terminator is the trust boundary. Grant the dashboard bypass ONLY to
        # the router cert; any other verified identity (e.g. a peer entity's
        # domain-<id> cert) is refused — it has no business on this dashboard.
        cn = _mtls_client_cn(request, client_host)
        if cn is not None:
            # Self-authenticating MCP surface: let it reach its own bearer +
            # hardware-fingerprint auth instead of gating on the router CN.
            # (A cross-tenant cert still cannot pass that auth — the dashboard
            # lock holds; see _MCP_CLIENT_PATHS above.)
            if path in _MCP_CLIENT_PATHS:
                return await call_next(request)
            if ROUTER_CN and cn == ROUTER_CN:
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"error": "access denied: cross-tenant cert"},
            )

        # k8s liveness/readiness probes need to reach health endpoints
        # from the node IP. Exempt those paths only.
        if path in _PROBE_EXEMPT_PATHS:
            return await call_next(request)

        # The messaging connectors authenticate themselves in the handler
        # (routes/connector.require_connector_cap). The other cross-pod
        # surfaces the platform used (an app runtime pod's source and log, a
        # harness's capability, the ship proof's browser) have no caller on a
        # standalone Host, so they are not exempt here.
        if path.startswith(_SELF_AUTH_PREFIXES):
            return await call_next(request)

        # The OAuth callback authenticates itself by its state (above). GET
        # only: it is a browser navigation and nothing else.
        if path in OAUTH_CALLBACK_PATHS and request.method == "GET":
            return await call_next(request)

        # The loopback lane needs no session, so a page whose DNS name was
        # re-pointed at 127.0.0.1 could use it from the owner's browser when
        # this runs outside a container. It must name this machine.
        owner_state = bool((request.scope.get("state") or {}).get("owner_session"))
        if not owner_state and path not in OWNER_PUBLIC_PATHS \
                and _is_allowed(client_host) \
                and not loopback_host_ok(request.headers.get("host")):
            return JSONResponse(
                status_code=403,
                content={"error": "access denied: unexpected Host"},
            )

        owner = bool((request.scope.get("state") or {}).get("owner_session")) \
            or path in OWNER_PUBLIC_PATHS
        if not owner and not _is_allowed(client_host):
            return JSONResponse(
                status_code=403,
                content={"error": "access denied: sign in first"},
            )

        # Validate Origin header (CSRF protection)
        origin = request.headers.get("origin")
        if origin and not origin_allowed(
                origin, request.headers.get("host"), own_origin=owner):
            return JSONResponse(
                status_code=403,
                content={"error": "access denied: invalid origin"},
            )

        return await call_next(request)


# ── WebSocket upgrades ─────────────────────────────────────────────────────
# LocalhostOnlyMiddleware is a BaseHTTPMiddleware, which never sees a WebSocket
# upgrade: every WS route on :8800 once skipped this gate. Two had
# no check of their own that holds against a direct peer — serve_app_ws relays
# to an app's backend (the browser app's with its control token attached), and
# /ws trusts an X-Authentik-Email header. This gate applies the same source
# rule to WS: on the mTLS path only the router cert passes, except the bastion
# lanes, whose handlers bind the operator cert themselves (main.py
# _enforce_bastion_binding); the connector routes pass to their own check, as
# over HTTP; the owner's session passes from its own origin; loopback passes
# when it names this machine; any other peer is closed before the handler runs.
_BASTION_WS_PATHS = frozenset({"/bastion-egress", "/bastion-mgmt", "/bastion-pgwire",
                               "/bastion-harness", "/bastion-embed", "/bastion-git"})


_BASTION_CN = "operator"


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def websocket_refusal(scope) -> str | None:
    """Why a WebSocket upgrade is refused, or None to let it through."""
    client_host = _unmap_v4((scope.get("client") or (None,))[0])
    path = scope["path"]
    cn = _header(scope, _CLIENT_CN_HEADER.encode()) if client_host in LOOPBACK_HOSTS else None
    if cn is not None:
        if ROUTER_CN and cn == ROUTER_CN:
            return None
        if path in _BASTION_WS_PATHS and cn == _BASTION_CN:
            return None
        return "cross-tenant cert"
    if path.startswith(_SELF_AUTH_PREFIXES):
        return None
    if (scope.get("state") or {}).get("owner_session"):
        # The owner's cookie rides any same-site handshake, and a WebSocket
        # is not covered by the HTTP Origin check above: hold it to the same
        # rule. A browser always sends Origin on a WebSocket handshake.
        origin = _header(scope, b"origin")
        if origin and not origin_allowed(
                origin, _header(scope, b"host"), own_origin=True):
            return "origin not allowed"
        return None
    if not _is_allowed(client_host):
        return "no owner session"
    if not loopback_host_ok(_header(scope, b"host")):
        return "unexpected Host"
    origin = _header(scope, b"origin")
    if origin and not origin_allowed(origin, _header(scope, b"host"), own_origin=False):
        return "origin not allowed"
    return None


class LocalhostOnlyWebSocketGate:
    """Pure ASGI: the WebSocket half of the localhost-only gate."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "websocket":
            why = websocket_refusal(scope)
            if why is not None:
                import logging
                logging.getLogger("cseries.localhost_only").warning(
                    "WS %s refused from %s: %s", scope.get("path"), (scope.get("client") or ("?",))[0], why)
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)
