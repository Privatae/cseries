"""
Application-token middleware + route dependency.

Pipeline:

  1. Middleware reads the X-Application-Token header (or the
     `app_token` cookie). If present, verifies signature + expiry, then
     attaches the parsed AppToken to request.state.app_token. If
     absent, request.state.app_token is None.

  2. Routes that require a specific permission declare it via the
     `requires_permission("<verb>:<resource>")` dependency. The
     dependency reads request.state.app_token, looks for the
     permission in the token's permission set, and rejects with
     403 if the token is missing or under-scoped.

Mode is APP_TOKEN_MODE (default `audit`): audit records every would-deny
without blocking; strict 401s a missing/invalid token on any /api route
not in the shell-bootstrap allowlist (_ALLOWLIST_PREFIXES). The
`requires_permission` dependency 401/403s on its annotated routes in EVERY
mode, so per-scope enforcement does not wait for the strict flip.

Strict-flip readiness (asserted by scripts/smoke_test.sh):
  - The token-less shell-bootstrap surface is the explicit, documented
    allowlist below; a strict-mode container is smoke-tested to confirm
    the launcher still boots (shell routes 200) while a gated route 401s.
  - EventSource/SSE clients (e.g. training-memory) cannot set an
    X-Application-Token header, but this middleware ALSO reads the
    `app_token` cookie (see dispatch), so an isolated app delivers the
    token to its SSE calls via the cookie on its own origin. That is the
    supported strict-mode path for EventSource; SDK cookie-set lands with
    the SDK runtime work, so the strict flip is gated on it.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from server.applications.tokens import AppToken, TokenError, verify

log = logging.getLogger("privatae.applications.middleware")


HEADER_NAME = "X-Application-Token"
COOKIE_NAME = "app_token"

# Lockdown mode:
#   permissive — original behavior: never act on a missing token (legacy).
#   audit      — DON'T block, but LOUDLY record every /api call that WOULD be denied
#                (no/invalid token, not pre-login allow-listed) so the lockdown is
#                driven by evidence. Default while wiring.
#   strict     — reject those calls 401 (and still record them, so a missed route
#                surfaces loudly instead of silently breaking).
_MODE = (os.environ.get("APP_TOKEN_MODE") or "audit").lower()

# Pre-login / shell-trusted /api routes that never carry an app token. Kept SMALL and
# explicit; the app_token_audit table reveals anything that needs to be here. (Non-/api
# paths — the desktop shell html, /js, /css, assets — are never gated.)
#
# STRICT-MODE NOTE: this is the documented token-less surface — the calls the desktop
# SHELL itself makes before (and independent of) any app window having a token. The
# shell is the trusted first-party desktop, not an app.
#
# It is no longer true that the shell holds NO token. The shell chrome polls three
# scope-gated routes to render itself (/api/entity/state, /api/inbox/unread-count,
# /api/calendar/list), and an allowlist entry cannot help there — see the next
# paragraph: allowlisting exempts a route from the BLANKET requirement only, and
# `requires_permission` still 401s a token-less caller in every mode. So the shell
# mints its own narrow token from /api/applications/shell-token (SHELL_SCOPES in
# server/routes/applications.py) and stamps it on those three calls specifically.
# Everything listed below stays genuinely token-less bootstrap.
#
# Each entry below is shell-bootstrap. Crucially, allow-listing a prefix here ONLY exempts a
# route from the BLANKET token requirement — it does NOT bypass `requires_permission`:
# the privileged mutations under /api/applications (install/build/publish/yank/set-price)
# and the per-app config routes (_require_self) enforce their own 401/403 regardless of
# mode. So the launcher subtree is reachable for the shell while its mutations stay gated.
#
# Being under a prefix here says nothing about whether a route needs a SCOPE. That
# is server/applications/route_policy.py: every /api route requires a permission or
# is listed there with a reason, and scripts/test_api_route_guards.py fails the
# build otherwise. Some routes listed there are NOT covered here yet (the bastion
# pairing and dcn-propose lanes, the OAuth and EventSource routes), so strict mode
# would refuse them — reconcile the two before flipping APP_TOKEN_MODE.
_ALLOWLIST_PREFIXES = (
    "/api/system/health",
    "/api/system/status",
    "/api/system/version",
    "/api/system/app-token-audit",   # the diagnostic itself must stay reachable
    "/api/system/resources",         # taskbar tray: pod memory/cpu/processes (read-only self-read)
    # Shell launcher bootstrap (token-less by design; privileged sub-routes keep their
    # own requires_permission gate — see note above):
    "/api/applications",             # list/get/scopes/launch — the launcher surface
    "/api/appearance",               # wallpaper/theme bootstrap
    # Generated images kept on this Host (routes/images.py). Same reason as the
    # wallpaper: the consumer is an <img> or a clicked link, which cannot carry
    # an app token — without this line every image load logged a would-deny in
    # audit mode and the strict flip would have 401'd every generated image. The
    # route serves only raster bytes by a 128-bit id and takes nothing else.
    "/api/images/",
    # gitea OAuth2 callback: a top-level browser redirect FROM gitea, so it carries
    # the edge forward-auth but not an app token. Secured by OAuth state + PKCE (and
    # gitea validated the registered redirect); no privileged data crosses it.
    "/api/gitea/oauth/callback",
    # An MCP server's sign-in coming back (server/mcp_oauth.py): the same kind
    # of top-level redirect, bound by the pending sign-in its state names.
    "/api/mcp/oauth/callback",
    # A data provider's sign-in coming back (routes/integrations.py): the same
    # kind of redirect, bound by a state this Host minted and spends there
    # (integrations/core/oauth.claim_state).
    "/api/integrations/callback/",
    # A messaging connector app's own pod: no app window, so no app token. Every route under it
    # checks the connector capability in its dependency
    # (routes/connector.require_connector_cap); mirrors its exemption from the
    # localhost gate and the CEIGAS gate.
    "/api/connector/",
    # The bastion's own heartbeat: a machine on the user's mesh, no app window
    # and so no app token. Self-authenticating by the bastion's OWN bearer
    # (routes/network.py; exempted from the localhost gate the same way in
    # middleware/localhost_only.py). Without this line every heartbeat logged a
    # would-deny every two minutes (a live Host's pod) and the strict
    # flip would have 401'd every bastion off the network.
    "/api/network/bastions/heartbeat",
    # The extraction PULL path: the bastion fetches the ONE document it
    # was asked to extract. Same reason as the heartbeat — a machine on the
    # user's mesh has no app window and so no app token — and it is
    # self-authenticating twice over: its own bastion bearer, plus a single-use
    # ticket the host minted for that machine and that row. The route takes no
    # ctx_id, so this exemption grants no read of anything.
    "/api/network/bastions/context-source",
    # A one-time download link for one backup on this machine (routes/backup.py
    # download_backup_link): the browser follows it as a plain navigation, which
    # carries no app token. The link authenticates itself: minted only for a
    # window holding read:backup_settings, spent on first use, good for a
    # minute, and only on the origin of the window that asked for it. The
    # trailing slash keeps every other /api/backup/files route behind its token
    # (and /files/{stamp}/download, which a path under this prefix can also
    # name, has its own scope check).
    "/api/backup/files/download/",
)

_ID_SEG = re.compile(r"^(?:[0-9]+|[0-9a-fA-F]{16,}|[0-9a-fA-F-]{32,})$")


def _normalize_path(p: str) -> str:
    """Collapse id-like segments so the audit groups by route, not per-id."""
    return "/".join(":id" if _ID_SEG.match(seg) else seg for seg in p.split("/"))


async def _record_would_deny(method: str, path: str, reason: str) -> None:
    """Loud + queryable: upsert one aggregated row per (method, route, reason).
    Best-effort — auditing must never break a request."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return
        await pool.execute(
            "INSERT INTO app_token_audit (method, path, reason, mode, hits, last_seen) "
            "VALUES ($1, $2, $3, $4, 1, now()) "
            "ON CONFLICT (method, path, reason) DO UPDATE "
            "  SET hits = app_token_audit.hits + 1, mode = $4, last_seen = now()",
            method, path, reason, _MODE,
        )
    except Exception as e:
        log.debug("app_token audit record failed: %s", e)


class ApplicationTokenMiddleware(BaseHTTPMiddleware):
    """Reads the per-window app token off each request, and (audit/strict mode)
    surfaces every /api call lacking a valid token.

    The signing secret lives on `request.app.state.kernel_secret`, populated by the
    lifespan once PG is up. Before that, no token verification happens (the startup
    window stays permissive).
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request.state.app_token = None

        secret: bytes | None = getattr(request.app.state, "kernel_secret", None)
        if secret is None:
            return await call_next(request)

        # HEADER ONLY. The `app_token` cookie this also used to read had no
        # setter anywhere in this tree (no Set-Cookie, no page writes it), so it
        # carried nothing legitimate — and a cookie is the one token source a
        # page on ANOTHER origin can plant: any page in the zone can set it for
        # the whole zone. A token on the wrong origin is a 403, so a
        # planted cookie would have locked every header-less request on every
        # origin of this Host out, and outlived the app that planted it.
        # COOKIE_NAME stays defined for code that imports it; nothing reads it.
        token_str = request.headers.get(HEADER_NAME)
        reason = "no_token"
        if token_str:
            try:
                request.state.app_token = verify(secret, token_str)
            except TokenError as e:
                log.debug("rejected app token: %s", e)
                reason = "invalid_token"

        # One origin per isolated app (applications/app_origin.py): a
        # window token is honoured only on the origin it belongs to. An isolated
        # app's token carries its origin's key and is refused anywhere else; a
        # token with no key — the desktop's, a same-origin app's, the Host's —
        # is refused on every app origin. So app A's origin cannot spend app B's
        # token, and a token lifted from the desktop opens nothing on Settings'
        # origin. A refusal, not a silent drop to "no token": audit mode would
        # otherwise wave the request through as if it had never carried one.
        tok = request.state.app_token
        if tok is not None:
            from server.applications import app_origin
            origin = app_origin.classify(app_origin.browser_host(request.headers))
            if not app_origin.token_fits_origin(tok.origin_key, origin):
                log.warning("app token for %r refused on %s origin (token origin %r)",
                            tok.app_id, origin.kind, tok.origin_key or "-")
                request.state.app_token = None
                return JSONResponse(
                    {"detail": "this window token belongs to another origin"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )

        if _MODE in ("audit", "strict") and request.state.app_token is None:
            path = request.scope["path"]  # the routed path; request.url.path follows the Host header
            if path.startswith("/api/") and not path.startswith(_ALLOWLIST_PREFIXES):
                # The in-container loopback bypass (INTERNAL_API_SECRET) is trusted —
                # never count or block it (mirrors requires_permission).
                # Inside this container only (localhost_only.in_container), and
                # compared in constant time, as _admit compares it.
                import hmac as _hmac
                from server.middleware.localhost_only import in_container
                internal = request.headers.get("x-cseries-internal-auth")
                internal_secret = os.environ.get("INTERNAL_API_SECRET", "")
                if not (internal and internal_secret and in_container(request)
                        and _hmac.compare_digest(internal, internal_secret)):
                    npath = _normalize_path(path)
                    log.warning("APP_TOKEN %s would-deny %s %s (%s)", _MODE, request.method, npath, reason)
                    await _record_would_deny(request.method, npath, reason)
                    if _MODE == "strict":
                        return JSONResponse(
                            {"detail": "application token required", "path": npath},
                            status_code=status.HTTP_401_UNAUTHORIZED,
                        )

        app_id = getattr(request.state.app_token, "app_id", None)
        if app_id:
            # A second-factor code sent with an app token counts on that
            # caller's own attempt record: the Host's kernel-op token
            # (kernel_ops.run_kernel_op) on the Host's, any other app on its
            # own and on the one all such apps share, and only the owner's
            # own windows on the owner's (server/network/nac_stepup.py,
            # attempt limit, which also says what a same-origin page can
            # still do).
            from server.network import nac_stepup
            with (nac_stepup.host_attempts() if app_id == "host"
                  else nac_stepup.app_attempts(app_id)):
                return await call_next(request)
        return await call_next(request)


_log = logging.getLogger("privatae.pc.app_middleware")

# In-pod loopback routes the INTERNAL_API_SECRET may authorize. Explicit
# allow-list so the secret can't grant full permission on
# arbitrary /api routes — only the specific kernel loopbacks that need it.
#   /api/applications/build  ← ship_project loopback (mcp.py _tool_ship_project)
_INTERNAL_LOOPBACK_PATHS: frozenset[str] = frozenset({"/api/applications/build"})


def requires_permission(permission: str) -> Callable:
    """FastAPI dependency. Reject unless the request carries an app token that
    declared this permission.

    Two distinct rejections, and callers depend on telling them apart:
      401 — no token at all (or one the middleware couldn't verify). Recoverable:
            the caller can go mint one. The desktop shell retries on 401 for
            exactly this reason (dashboard/base/js/shell-token.js).
      403 — a valid token that did not declare this scope. Not recoverable by
            re-minting; the caller genuinely lacks the permission.
    (This docstring used to say "Reject 403" for both, which read as though a
    token-less caller had been refused on scope.)

    Usage:
        @router.get("/memory")
        async def read_memory(_: None = Depends(requires_permission("read:brain_memory"))):
            ...
    """

    async def _dep(request: Request) -> None:
        await _admit(request, (permission,))

    # Tag the dependency with its scope so the code-true app-facing API contract
    # (server/applications/api_contract.py) can recover each route's required
    # permission by introspection instead of parsing source.
    _dep._app_scope = permission  # type: ignore[attr-defined]
    return _dep


def requires_any_permission(*permissions: str) -> Callable:
    """FastAPI dependency. Like requires_permission, admitting a token that
    declared ANY ONE of `permissions`.

    For a route a narrow scope opens beside a wide one that already did, so a
    new caller can hold only the narrow one while existing callers keep
    working: POST /api/contacts/request-peer takes request:peer (asking a
    Host found on your machine to connect, and nothing else) or
    write:address_book (which also opens contact delete, block and accept).
    The first permission is the route's scope in the API contract; all of
    them are its `_app_scopes`, which the contract and the gates read.
    """
    perms = tuple(p for p in permissions if p)
    if not perms:
        raise ValueError("requires_any_permission needs at least one permission")

    async def _dep(request: Request) -> None:
        await _admit(request, perms)

    _dep._app_scope = perms[0]  # type: ignore[attr-defined]
    _dep._app_scopes = perms  # type: ignore[attr-defined]
    return _dep


async def _admit(request: Request, perms: tuple[str, ...]) -> None:
    """The check both dependencies make: 401 with no token, 403 with a token
    that declared none of `perms`."""
    # Loopback subprocess bypass — NARROWED: the
    # INTERNAL_API_SECRET authorizes ONLY the specific in-pod loopback routes
    # that need it (ship_project -> POST /api/applications/build), not blanket
    # full permission on every /api route. The secret on any non-allowlisted
    # route does NOT bypass (falls through to normal app-token auth) and is
    # logged CRITICAL, so a missed legitimate loopback surfaces loudly instead
    # of silently regaining full power. Compared only for a caller inside this
    # container (localhost_only.in_container: loopback, not carried by the mTLS
    # front door); from anywhere else a request bringing it is refused before
    # any compare, so no caller outside can test a guess against it.
    import os as _os, hmac as _hmac
    from server.middleware.localhost_only import in_container
    internal_hdr = request.headers.get("x-cseries-internal-auth")
    internal_secret = _os.environ.get("INTERNAL_API_SECRET", "")
    if internal_hdr and not in_container(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="the internal secret is accepted only from inside this container",
        )
    if internal_hdr and internal_secret and _hmac.compare_digest(internal_hdr, internal_secret):
        if request.scope["path"] in _INTERNAL_LOOPBACK_PATHS:
            return
        _log.critical(
            "INTERNAL_API_SECRET presented on non-allowlisted route %s — NOT "
            "bypassing auth (scoped to %s). Add it to _INTERNAL_LOOPBACK_PATHS "
            "if this is a legitimate in-pod loopback.",
            request.scope["path"], sorted(_INTERNAL_LOOPBACK_PATHS))

    tok: AppToken | None = getattr(request.state, "app_token", None)
    if tok is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="application token required",
        )
    if not any(p in tok.permissions for p in perms):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"app {tok.app_id!r} did not declare permission {perms[0]!r}" if len(perms) == 1
                    else f"app {tok.app_id!r} did not declare any of the permissions {list(perms)!r}"),
        )
