"""Every /api route that does NOT require an app-token permission, and why.

THE RULE (scripts/test_api_route_guards.py enforces it against FastAPI's built
route table, not the source text): every /api route — HTTP and WebSocket, on a
router or on the app — either

  1. depends on `requires_permission(<scope>)` (middleware.py), so a caller
     needs an app token that declared that scope; or
  2. is listed below, ONE place, with the kind of guard it has instead and the
     reason it needs no scope.

A new route that is neither fails the gate by name. That is the point: once
nothing compared the route table with the permission model, and
POST /api/agents/dispatch shipped with no check at all — any app on the Host,
including an origin-isolated community app whose /api calls land on this same
pod, could start a scoped agent with the tool scope and capability grant of its
choosing. ApplicationTokenMiddleware does not close that kind of hole: in its
default `audit` mode a token-less call is only logged, and even in `strict`
mode it asks for SOME token, which every app window has.

This list is NOT the middleware's `_ALLOWLIST_PREFIXES`. That list says which
paths may arrive with no token at all once APP_TOKEN_MODE=strict; this one says
which routes need no scope. A route here can still need a token (`self-token`),
and a path there can still hold scoped routes (most of /api/applications).

Kinds — the gate checks the ones it can see structurally:

  bootstrap     The desktop shell (or an <img>/CSS load it makes) reads this
                before, and independently of, any app window. GET only.
  public        The same for every Host and every caller: a constant, the
                platform's own catalogue, the route table. GET only.
  shell-mint    Hands out an app token, so it cannot ask for one. Refuses the
                isolated-app origin and any cross-origin browser inline, before
                any lookup (pinned by scripts/test_shell_token.py).
  app-origin-mint  Hands an ISOLATED app its own window token, so it cannot
                ask for one. Answers only on that app's own origin
                (apps-<key>-<handle>, applications/app_origin.py) and only to a
                same-origin page there; refuses every other origin and any
                cross-origin browser inline, before any lookup (pinned by
                scripts/test_app_origin.py). The token it mints is honoured on
                that origin alone.
  shell-origin  A token-less caller must be this desktop's own page: the
                `shell_origin_when_tokenless` dependency below refuses the
                isolated-app origin and cross-origin browsers. The gate checks
                the dependency is on the route. A caller WITH a token is held
                by the route itself (its own app only, or a scope).
  self-token    Needs the caller's OWN app token and reaches only that app's
                own namespace (`_require_self`), so there is no scope to ask.
  own-auth      Authenticated in the handler by a credential that is not an app
                token — a bastion bearer, a one-time pairing secret, a per-wake
                harness capability, an app runtime pod's source token, or a
                one-shot ticket. Each reason names which.
  oauth-callback A top-level browser redirect from an OAuth provider, which can
                carry no app token; bound by the state this Host minted.
  stream        An EventSource, which cannot send the token header; serves
                status/progress frames only, for an id the caller already has.
                GET only.
  not-found     The /api catch-all that turns an unmatched path into a 404.
  internal-secret  Only the in-pod INTERNAL_API_SECRET loopback. (None today:
                the one loopback route, POST /api/applications/build, is
                scoped and allow-listed in middleware._INTERNAL_LOOPBACK_PATHS.)
"""
from __future__ import annotations

from typing import Callable

from fastapi import Request

KINDS = frozenset({
    "bootstrap", "public", "shell-mint", "app-origin-mint", "shell-origin", "self-token",
    "own-auth", "oauth-callback", "stream", "not-found", "internal-secret",
})

# Kinds whose routes may only read.
READ_ONLY_KINDS = frozenset({"bootstrap", "public", "stream"})

# Attribute stamped on the shell-origin dependency so the gate can see it.
SHELL_ORIGIN_ATTR = "_shell_origin"


def shell_origin_when_tokenless(what: str, permission: str | None = None) -> Callable:
    """FastAPI dependency for a route the desktop shell calls WITHOUT a token.

    No token: the request must come from this desktop's own page — the same two
    refusals /launch and /shell-token make (server/routes/applications.py
    `_refuse_isolated_origin`, `_refuse_cross_origin_browser`). Without this an
    isolated app could leave its own token off and be taken for the shell.

    With a token: `permission`, when given, is required exactly as
    requires_permission requires it (and is stamped as the route's scope, so
    the api contract and kernel_op see it); when not given, the route's own
    handler decides (e.g. an app may close only its own window).
    """
    from server.applications.api_contract import SCOPE_ATTR
    from server.applications.middleware import requires_permission

    perm_dep = requires_permission(permission) if permission else None

    async def _dep(request: Request) -> None:
        if getattr(request.state, "app_token", None) is None:
            from server.routes.applications import (
                _refuse_cross_origin_browser, _refuse_isolated_origin,
            )
            _refuse_isolated_origin(request, what)
            _refuse_cross_origin_browser(request, what)
            return
        if perm_dep is not None:
            await perm_dep(request)

    setattr(_dep, SHELL_ORIGIN_ATTR, True)
    if permission:
        setattr(_dep, SCOPE_ATTR, permission)
    return _dep


def _e(methods: str, path: str, kind: str, reason: str) -> list[tuple[str, str, str, str]]:
    return [(m, path, kind, reason) for m in methods.split(",")]


_BASTION_BEARER = ("the bastion's OWN bearer (mcp_client_tokens, operator_kind="
                   "'bastion', not revoked), checked in the handler; the token id "
                   "comes from the bearer, never the body (server/routes/network.py)")
_CONNECTOR_CAP = ("the connector capability a messaging connector app's pod presents "
                  "(X-CSeries-App-Id + X-CSeries-Connector-Cap), constant-time checked against "
                  "the grant in the database by require_connector_cap "
                  "(server/routes/connector.py); the pod has no app window")
_RUNTIME_TOKEN = ("the app's own runtime pod presents X-App-Runtime-Source-Token "
                  "(harness_backend.check_app_source_token) — a per-app capability "
                  "the operator placed in that pod; anyone else gets 403")
_SHELL_READ = "the desktop shell paints itself from this before any app window exists"

_ENTRIES: list[tuple[str, str, str, str]] = [
    # ── the desktop shell's own reads ────────────────────────────────────
    *_e("GET", "/api/applications", "bootstrap",
        "the launcher's app list (operator-only apps removed); " + _SHELL_READ),
    *_e("GET", "/api/applications/{app_id}", "bootstrap",
        "the launcher reads one app's manifest before it launches it"),
    *_e("GET", "/api/applications/store/catalog", "bootstrap",
        "the store's public catalogue, marked with which of its apps this Host "
        "has installed (the same installed set GET /api/applications lists)"),
    *_e("GET", "/api/applications/store/icon/{app_id}", "bootstrap",
        "a store icon PNG, used as an image URL that cannot carry a token"),
    *_e("GET", "/api/appearance", "bootstrap", "wallpaper, theme and desktop layout; " + _SHELL_READ),
    *_e("GET", "/api/appearance/image", "bootstrap",
        "the wallpaper bytes, loaded as a CSS background that cannot carry a token"),
    *_e("GET", "/api/appearance/desktop", "bootstrap",
        "the HTML desktop the shell mounts at boot; served with CSP sandbox, "
        "scoped to this Host's own artifact by construction (appearance.serve_desktop_html)"),
    *_e("GET", "/api/appearance/desktop/meta", "bootstrap", "the HTML desktop's change stamp, polled by the shell"),
    *_e("GET", "/api/images/{image_id}", "bootstrap",
        "a generated image by a 128-bit id, loaded by an <img> or a link that "
        "cannot carry a token; raster bytes only (server/routes/images.py)"),
    *_e("GET", "/api/system/health", "bootstrap", "liveness probe (the image HEALTHCHECK)"),
    *_e("GET", "/api/system/health/deep", "bootstrap", "readiness probe"),
    *_e("GET", "/api/system/status", "bootstrap",
        "read by the shell's own chrome (ws-client.js, dock-menu.js, desktop-surface.js)"),
    *_e("GET", "/api/system/resources", "bootstrap", "the taskbar tray: this pod's memory, CPU and processes, the main "
        "container's and the database container's alike; process command lines go only to "
        "a token holding read:system, everyone else gets the executable's name alone"),
    *_e("GET", "/api/system/app-token-audit", "bootstrap",
        "the token audit itself — must stay reachable to drive the strict flip"),

    # ── platform-wide, identical for every caller ────────────────────────
    *_e("GET", "/api/applications/scopes", "public", "the permission catalogue (scopes.PERMISSIONS)"),
    *_e("GET", "/api/applications/api-contract", "public",
        "the route table and each route's scope, introspected (api_contract.py)"),
    *_e("GET", "/api/applications/store/preview/{app_id}", "public",
        "a store bundle's declared access (egress, scopes, licence), read before install"),
    *_e("GET", "/api/applications/sites/catalog/templates", "public",
        "the platform's admitted site templates; no owner data"),

    # ── token mints ──────────────────────────────────────────────────────
    *_e("GET", "/api/applications/shell-token", "shell-mint",
        "mints the shell's own read-only token (SHELL_SCOPES)"),
    *_e("POST", "/api/applications/{app_id}/launch", "shell-mint",
        "mints an app's window token from its manifest; an app token may "
        "re-launch only its own app (C-2); a browser never gets an ISOLATED "
        "app's token here — that one comes from frame-token"),
    *_e("POST", "/api/applications/{app_id}/frame-token", "app-origin-mint",
        "an isolated app's window token, minted only on that app's own origin "
        "for a same-origin page there, bound to that origin"),

    # ── shell writes, token-less only from the shell's own page ──────────
    *_e("POST", "/api/applications/{app_id}/close", "shell-origin",
        "the shell closes any window; an app token may close only its own app's (C-2)"),
    *_e("POST", "/api/applications/{app_id}/window-beat", "shell-origin",
        "the shell says an open window of a pod-run app is still there (its hold "
        "ends when the beats stop); an app token may beat only for its own app"),
    *_e("POST", "/api/applications/{app_id}/runtime", "shell-origin",
        "the shell drives any resident app's backend; an app token may drive only "
        "its own app's, or any as the App Store"),
    *_e("PUT", "/api/appearance/layout", "shell-origin",
        "the shell saves dock pins and icon order on every drag; an app token "
        "needs write:appearance"),

    *_e("POST", "/api/appearance/desktop/loop", "shell-origin",
        "the shell reports that the worn desktop's frame reloaded itself over and over "
        "(desktop-surface.js noteLoad); an app token needs write:appearance. It can only "
        "take the worn version OFF: the exact pin stamp must match, the version must have "
        "been worn for LOOP_MIN_WORN_S by the server's clock, and the version before it "
        "comes back with a notice that it was taken off after a reload report "
        "(unconfirmed). The report never records the bytes as failed, so a later wear "
        "is not refused on it (desktop_pin.take_off_reloading)"),

    # ── the caller's own app, nothing else ───────────────────────────────
    *_e("GET", "/api/applications/{app_id}/config", "self-token",
        "an app's own key/value store; _require_self refuses any other app"),
    *_e("GET,PUT,DELETE", "/api/applications/{app_id}/config/{key}", "self-token",
        "an app's own key/value store; _require_self refuses any other app"),

    # ── credentials that are not app tokens ──────────────────────────────
    # The owner's sign-in (server/routes/owner.py, server/owner_login.py): the
    # browser has no app token until the owner has signed in.
    *_e("GET", "/api/owner/status", "bootstrap",
        "the setup and sign-in pages read whether an owner exists and whether this "
        "browser's session cookie is live (two booleans) before any token exists"),
    *_e("POST", "/api/owner/setup", "own-auth",
        "the one-time setup code printed to this Host's log on first boot, checked by "
        "owner_login.create_owner; refused once an owner exists"),
    *_e("POST", "/api/owner/login", "own-auth",
        "the owner's email and password, checked by owner_login.sign_in with a "
        "per-client backoff on wrong attempts"),
    *_e("POST", "/api/owner/logout", "own-auth",
        "ends only the session named by the caller's own session cookie "
        "(owner_login.sign_out); without one there is nothing to end"),
    *_e("GET", "/api/applications/{app_id}/source", "own-auth", _RUNTIME_TOKEN),
    *_e("POST", "/api/applications/{app_id}/runtime-log", "own-auth", _RUNTIME_TOKEN),
    *_e("POST", "/api/network/bastions/redeem", "own-auth",
        "a new bastion redeems its single-use pairing secret, minted by the "
        "write:network /bastions/setup route; no app session exists yet"),
    *_e("POST", "/api/network/bastions/heartbeat", "own-auth", _BASTION_BEARER),
    *_e("GET", "/api/network/bastions/git-credential", "own-auth", _BASTION_BEARER),
    *_e("POST", "/api/network/bastions/dcn-propose", "own-auth",
        _BASTION_BEARER + "; the draft stays inert until the user's step-up"),
    *_e("GET", "/api/network/bastions/context-source", "own-auth",
        _BASTION_BEARER + ", plus a one-shot ticket minted for that machine and row"),
    *_e("WEBSOCKET", "/api/network/sessions/{server}", "own-auth",
        "the first frame must redeem a short-lived, single-use ticket minted by the "
        "write:network /sessions/{server}/ticket route for this server"),
    *_e("POST", "/api/connector/pair", "own-auth", _CONNECTOR_CAP + " (an open pairing)"),
    *_e("POST", "/api/connector/platform", "own-auth", _CONNECTOR_CAP),
    *_e("WEBSOCKET", "/api/connector/gateway", "own-auth",
        _CONNECTOR_CAP + ", in the handshake's headers (Discord's gateway, held by the Host)"),
    *_e("POST", "/api/connector/messages", "own-auth", _CONNECTOR_CAP + " (an approved grant)"),
    *_e("POST", "/api/connector/messages/sent", "own-auth",
        _CONNECTOR_CAP + " (an approved grant)"),
    *_e("GET", "/api/connector/turns/{turn_id}", "own-auth",
        _CONNECTOR_CAP + " (an approved grant)"),

    # ── OAuth redirects ──────────────────────────────────────────────────
    *_e("GET", "/api/gitea/oauth/callback", "oauth-callback",
        "gitea's redirect; the code is exchanged only for a PKCE state this Host holds"),
    *_e("GET", "/api/integrations/callback/{key}", "oauth-callback",
        "a data provider's redirect; refused unless oauth.verify_state accepts the "
        "state this Host signed for that provider"),
    *_e("GET", "/api/mcp/oauth/callback", "oauth-callback",
        "an MCP server's authorization server's redirect; acts only on a pending sign-in "
        "found by the sha-256 of its state (single use, ten minutes), checks iss before "
        "anything else, and trades the code only with that sign-in's PKCE verifier "
        "(server/mcp_oauth.finish)"),

    # ── EventSource streams ──────────────────────────────────────────────
    *_e("GET", "/api/review/sessions/{session_id}/events", "stream",
        "a Workbench run's state flips only (no diffs, no transcripts); the "
        "Workbench reads it with EventSource"),
    *_e("GET", "/api/training-memory/jobs/{job_id}/events", "stream",
        "a job's phase, progress and counters only; the job id is the one the "
        "write:brain_corrections create route returned"),

    # ── the catch-all ────────────────────────────────────────────────────
    *_e("GET,POST,PUT,PATCH,DELETE", "/api/{rest:path}", "not-found",
        "turns an unmatched /api call into a 404 instead of the SPA (server/main.py)"),
]

# (METHOD, path template) -> (kind, reason). METHOD is "WEBSOCKET" for a socket.
UNSCOPED_ROUTES: dict[tuple[str, str], tuple[str, str]] = {}
for _m, _p, _k, _r in _ENTRIES:
    if (_m, _p) in UNSCOPED_ROUTES:
        raise ValueError(f"route_policy lists {_m} {_p} twice")
    if _k not in KINDS:
        raise ValueError(f"route_policy: {_m} {_p} has unknown kind {_k!r}")
    UNSCOPED_ROUTES[(_m, _p)] = (_k, _r)
del _m, _p, _k, _r
