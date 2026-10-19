"""
Applications router — launcher + registry surface.

GET  /api/applications              list installed apps (active)
GET  /api/applications/{id}         single app details
POST /api/applications/{id}/launch  open a window (and mint its token, except
                                    an isolated app's for a browser — below)
POST /api/applications/{id}/frame-token  an isolated app's window token, minted
                                    ON that app's own origin

These endpoints are unauthenticated against the new token system on
purpose — they ARE the system that mints tokens. The dashboard reaches
them directly. Future phases will require an admin token to install /
uninstall.
"""

from __future__ import annotations

import hashlib as _hashlib
import json
import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request, status

from server import brain_service
from server.applications import bundles, entity_signing, installer, registry, storage, supervisor, tokens
from server.applications.dburl import mask_db_url
from server.applications.identity import identity
from server.applications.manifest import load as load_manifest
from server.applications.middleware import requires_permission
from server.applications.route_policy import shell_origin_when_tokenless
# ONE source of truth for the two serving predicates. The launcher payload below
# must agree with what proxy.py actually DOES when it serves the app's HTML: a
# disagreement means the shell runs a token handshake with an app that got no
# SDK, or (worse) an app gets an SDK and never gets a token — and every
# CSeriesApp.storage call then 401s on _require_self in this same file.
from server.applications.proxy import is_isolated, is_sdk_managed
from server.vault import api as vault_api
from fastapi import Depends
from pathlib import Path

log = logging.getLogger("privatae.applications.routes")

router = APIRouter(prefix="/api/applications", tags=["applications"])


def _serialize(app: registry.InstalledApp, origin_keys: dict | None = None) -> dict:
    """`origin_keys` is app_origin.keys_for(<every active app>): an isolated
    app's origin key, or None when its key collides with another installed
    app's (the desktop then refuses to frame it). Without it no app gets a key,
    which the desktop also refuses for an isolated app — fail closed."""
    base = {
        "id": app.id,
        "name": app.name,
        "version": app.version,
        "provenance": app.provenance,
        "entrypoint": app.entrypoint,
        "permissions": app.permissions,
        "source_signature": app.source_signature,
        "installed_at": app.installed_at.isoformat() if app.installed_at else None,
        "upgraded_at": app.upgraded_at.isoformat() if app.upgraded_at else None,
        "status": app.status,
        # Launcher fields, populated from the on-disk manifest when readable.
        # /api/apps/list (filesystem scan) is the legacy launcher source and
        # is being retired in favor of this endpoint, so the registry surface
        # must carry every field the launcher needs.
        "description": "",
        "icon_url": None,
        "icon_small_url": None,
        "url": f"/system-apps/{app.id}/{app.entrypoint}",
        "system": False,
        "operator_only": False,
        "window": {},
        # TWO independent flags — see the long note on is_sdk_managed/is_isolated
        # in server/applications/proxy.py.
        #   isolated    — the shell loads the app cross-origin from its OWN
        #                 origin, apps-<key>-<handle> (origin_key below).
        #                 Community (published,
        #                 untrusted) apps are ALWAYS isolated — not optional.
        #   sdk_managed — the kernel injected window.CSeriesApp into the app's HTML,
        #                 so the shell must run the cseries:app-init token handshake
        #                 with it. TRUE for every isolated app AND for entity-built
        #                 (`custom`, i.e. Workbench) apps, which stay same-origin.
        #                 Without this a custom app throws ReferenceError on
        #                 `await CSeriesApp.ready` — the exact break this pair fixes.
        "isolated": is_isolated(app.id, app.provenance),
        "sdk_managed": is_sdk_managed(app.id, app.provenance),
        # The key in the app's own origin, apps-<key>-<handle>
        # (server/applications/app_origin.py). Only an isolated app has one.
        "origin_key": ((origin_keys or {}).get(app.id)
                       if is_isolated(app.id, app.provenance) else None),
    }
    if not app.source_dir:
        return base
    try:
        manifest = load_manifest(Path(app.source_dir) / "app.yaml")
    except Exception as e:
        log.debug("manifest load for %s failed: %s", app.id, e)
        return base
    base["description"] = manifest.description or ""
    if manifest.icon:
        base["icon_url"] = f"/system-apps/{app.id}/{manifest.icon}"
    if manifest.icon_small:
        base["icon_small_url"] = f"/system-apps/{app.id}/{manifest.icon_small}"
    base["system"] = manifest.system
    base["operator_only"] = identity(manifest).operator_only
    base["window"] = {
        "title": manifest.window.title,
        "width": manifest.window.width,
        "height": manifest.window.height,
        "resizable": manifest.window.resizable,
        "min_width": manifest.window.min_width,
        "start_maximized": manifest.window.start_maximized,
        "min_height": manifest.window.min_height,
        # Phone-mode rendering: "scale" (shrink the desktop layout to fit)
        # or "responsive" (app reflows itself; render native full-width).
        "mobile": manifest.window.mobile,
    }
    return base


@router.get("")
async def list_apps() -> dict:
    pool = brain_service.get_pool()
    if pool is None:
        return {"applications": []}
    apps = await registry.list_active(pool)
    # Operator-only apps are physically excluded from the user-facing launcher —
    # users only ever see their own apps. operator_only is derived once via
    # AppIdentity (server.applications.identity), the single source of truth.
    from server.applications import app_origin
    keys = app_origin.keys_for(apps)
    serialized = [_serialize(a, keys) for a in apps]
    return {"applications": [s for s in serialized if not s.get("operator_only")]}


@router.get("/scopes")
async def list_scopes() -> dict:
    """The full permission catalog an app can declare.

    Single source of truth is server.applications.scopes.PERMISSIONS;
    The Workbench's ship flow fetches this so its permission picker can never
    drift from the enforced set (it previously hardcoded a stale copy
    that carried an invalid scope and was missing ~26 real ones).
    """
    from server.applications.scopes import PERMISSIONS
    return {"scopes": sorted(PERMISSIONS)}


@router.get("/api-contract")
async def api_contract(request: Request) -> dict:
    """The code-true app-facing API surface: every `/api/...` route an app may call,
    each with its required permission scope and (where a route declares a model) its
    request/response key set. Generated LIVE from the route table + the
    requires_permission dependencies, so it can never drift from what's enforced.

    Apps build against THIS — never invent a route or a response key. The same contract
    backs the build-time conformance gate (server/applications/build_contract.py:
    validate_build), which rejects an app that calls a nonexistent route or a
    scope-gated route its manifest doesn't declare."""
    from server.applications.api_contract import build_api_contract
    return build_api_contract(request.app)


# The desktop shell's own scope set. The shell is the trusted first-party
# surface, not an app, so it has no manifest to declare permissions in — this
# list IS its manifest. Keep it MINIMAL and read-only: every entry is a route
# the shell chrome itself polls to render, and nothing here mutates.
#   read:session  -> /api/entity/state        (presence.js, the ambient line)
#   read:inbox    -> /api/inbox/unread-count  (floating-apps.js, circles badge)
#   read:calendar -> /api/calendar/list       (floating-apps.js, the calendar's dock badge)
SHELL_APP_ID = "__shell__"
SHELL_SCOPES: tuple[str, ...] = ("read:session", "read:inbox", "read:calendar")


def _host_of(value: str) -> str:
    """The bare host in a Host / X-Forwarded-Host value: the first hop, no port,
    lowercased — and an IPv6 literal understood (`[::1]:8800` -> `::1`). A
    split on ":" turned that literal into "[", which the Origin comparison below
    then refused. A malformed value (an unclosed bracket) is no host at all."""
    first = (value or "").split(",")[0].strip()
    if not first:
        return ""
    from urllib.parse import urlsplit
    try:
        return (urlsplit("//" + first).hostname or "").lower()
    except ValueError:
        return ""


def _refuse_isolated_origin(request: Request, what: str) -> None:
    """403 when the request came from the origin-isolated app origin.

    The token MINTS — /shell-token and /launch — are the two routes on the
    token-less shell allowlist (middleware.py `_ALLOWLIST_PREFIXES`) that hand
    a token out, so neither can require one; what bounds them is the browser
    origin. An isolated app (every community and published app) is framed from
    an `apps-` origin — its own `apps-<key>-<handle>` — and its own
    `/api` calls land on the SAME pod, so a mint
    that did not look at the origin would give a third-party app any token it
    asked for. /shell-token has refused that origin since it was written;
    /launch did not, and it minted ANY installed app's token for a caller that
    simply sent no token of its own — the C-2 check below only sees a caller
    who announces itself.

    XFH is authoritative (the direct-router rewrites Host to the pod SAN);
    Host is the no-rewrite fallback. Either naming an apps- origin refuses.
    See shell_token's docstring for why Host alone was once dead code here.
    """
    for header in ("x-forwarded-host", "host"):
        if _host_of(request.headers.get(header) or "").startswith("apps-"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{what} is not available on the isolated-app origin",
            )


def _refuse_cross_origin_browser(request: Request, what: str) -> None:
    """403 when a BROWSER says this request did not come from this origin.

    The isolated-origin check above sees only requests ADDRESSED to the apps-
    host. An isolated app can also address the SHELL's host directly: apps-<h>
    and <h> are the same site, so the browser attaches the SameSite=Lax session
    cookie, and X-Forwarded-Host then names the shell — the check above passes.
    The app cannot read the answer (nothing here sends Access-Control-Allow-*),
    but the mint still happens, and /launch of a process-runtime app starts its
    sidecar.

    So the mint asks the browser's own Fetch Metadata and Origin, the same pair
    the direct-router already uses to keep switch-account same-origin
    (the router's switch path). It forwards
    both headers to this pod untouched. `Sec-Fetch-Site` other than same-origin
    or none, or an `Origin` whose host is not the host this request was
    addressed to (a "null" origin included), refuses. A caller that sends
    NEITHER — curl, the CLI, smoke, a test harness, a very old browser — is not
    something either signal can speak to, and passes, which is the router's
    posture too; the dozen scripted /launch callers in this repo are all of that
    kind. Same-origin frames still pass, and that is the limit stated at C-2.
    """
    sfs = (request.headers.get("sec-fetch-site") or "").strip().lower()
    origin = request.headers.get("origin")
    addressed = _host_of(request.headers.get("x-forwarded-host")
                         or request.headers.get("host") or "")
    cross = bool(sfs) and sfs not in ("same-origin", "none")
    if origin is not None:
        from urllib.parse import urlparse
        try:
            origin_host = (urlparse(origin.strip()).hostname or "").lower()
        except ValueError:          # a malformed Origin names no host at all
            origin_host = ""
        if not origin_host or origin_host != addressed:
            cross = True
    if cross:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{what} is only available to this desktop's own page",
        )


@router.get("/shell-token")
async def shell_token(request: Request) -> dict:
    """Mint the desktop shell's own scoped token.

    WHY THIS EXISTS: the three routes above are gated by
    `requires_permission` (middleware.py:175), which 401s any caller with no
    app token — in EVERY mode, not just strict. The shell held no token, so
    those polls 401'd by construction and presence.js retried in a loop. The
    alternative was dropping the gate on inbox/calendar, which would open a
    user's mail and diary to any caller in the pod that never declared the
    scope. This keeps the permission model uniform instead: the shell holds a
    token like everyone else, carrying only what its chrome reads.

    ORIGIN GATE — the reason this is safe to leave token-less. `/api/applications`
    is on the shell-bootstrap allowlist (middleware.py:85), so this route can't
    require a token to hand one out. What bounds it is the browser origin: an
    origin-isolated app is framed from `apps-<shellhost>` (app-runner.js:152)
    and its own `/api` calls land on the SAME pod, so without this check a
    community app could ask for shell scopes it never declared.

    READ X-FORWARDED-HOST, NOT Host. This is the whole correctness of the gate
    and it is not obvious. By the time a request reaches this process the
    direct-router has rewritten `Host` to the pod SAN, so `Host` is IDENTICAL
    for the shell origin and the apps- origin and can never discriminate them.
    The browser-facing name survives only in X-Forwarded-Host. The live
    app-isolation code does exactly this for exactly this reason —
    server/main.py:1111-1114 detects the apps- origin off `x-forwarded-host`
    and says so in its comment; server/routes/gitea.py:206 also prefers XFH and
    falls back to Host. uvicorn runs with no `--proxy-headers`
    (entrypoint.sh:372), so nothing repairs `Host` from XFH inside the app.
    The first version of this gate tested `Host` alone: it was dead code that
    could never fire, and an isolated app could have collected shell scopes
    with the gate reading green.

    Both headers are checked. XFH is the authoritative one; `Host` is kept as a
    belt-and-braces check for any path that reaches this process without a
    rewrite (a direct in-pod call, a future topology). Either one naming an
    apps- origin refuses.

    NOT A BOUNDARY AGAINST SAME-ORIGIN APPS, and deliberately so: circles,
    claude-code and codespace are still framed same-origin (kernel.py:119-125),
    so they can already reach `window.parent`. Note this is a slightly WIDER
    reach than the legacy contentWindow path, which hands an app its OWN
    per-window token (app-runner.js:249) — window.ShellToken exposes the
    shell's broader-scoped one. Those three are first-party and already have
    full parent-DOM access, so they are not the boundary; community and
    published apps are ALWAYS origin-isolated (proxy.py:380, installer.py:279)
    and are held out by the XFH check above. Host-built (`custom`) apps are
    NOT: they are framed same-origin too (proxy.is_isolated), so they reach
    this route, /launch and window.parent exactly as those three do, and
    nothing on this side tells them from the shell.
    """
    _refuse_isolated_origin(request, "shell token")
    _refuse_cross_origin_browser(request, "shell token")

    secret = getattr(request.app.state, "kernel_secret", None)
    if secret is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kernel secret not loaded")

    token_str, decoded = tokens.mint(
        secret,
        app_id=SHELL_APP_ID,
        permissions=list(SHELL_SCOPES),
    )
    return {
        "token": token_str,
        "app_id": decoded.app_id,
        "permissions": decoded.permissions,
    }


@router.get("/{app_id}")
async def get_app(app_id: str) -> dict:
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    app = await registry.get(pool, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"not installed: {app_id}")
    return _serialize(app, await _origin_keys(pool))


async def _origin_keys(pool) -> dict:
    """app_origin.keys_for over every active app — the collision ranking needs
    them all, not just the one being asked about."""
    from server.applications import app_origin
    return app_origin.keys_for(await registry.list_active(pool))


@router.post("/{app_id}/launch")
async def launch_app(app_id: str, request: Request) -> dict:
    """Mint a token bound to this app's manifest permissions.

    The caller opens the app's window with this token; the window's
    HTTP requests carry it (header or cookie) and the middleware
    enforces permission against it.
    """
    # FIRST, before any lookup: the isolated-app origin may not mint at all, and
    # neither may a browser page on any other origin that addresses this host
    # (the two _refuse_* checks say why). Ahead of the registry read so neither
    # can learn which apps are installed from 404-vs-403.
    _refuse_isolated_origin(request, "launching an app")
    _refuse_cross_origin_browser(request, "launching an app")

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    app = await registry.get(pool, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"not installed: {app_id}")
    if app.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"app not active: {app.status}")

    # C-2: an APP must not mint ANOTHER app's token. /launch is a shell action — the
    # desktop shell sends it token-less. An app window carries its own; allow it to
    # re-launch ITSELF (same scopes, no escalation) but refuse launching a different,
    # possibly more-privileged app (which would hand it that app's scope set).
    #
    # THIS CHECK ONLY SEES A CALLER THAT ANNOUNCES ITSELF. An app that sends no
    # token — an XMLHttpRequest, which the SDK's fetch wrapper never stamps —
    # arrives here as caller=None, exactly like the shell. For the isolated
    # origin that is closed by _refuse_isolated_origin above. For SAME-origin
    # frames it cannot be closed at this layer at all: such a frame shares the
    # shell's origin and cookie and can reach window.parent, so nothing on the
    # server tells its request from the shell's (see shell_token: "NOT A
    # BOUNDARY AGAINST SAME-ORIGIN APPS"). That includes every Host-built
    # (`custom`) app: it can launch any same-origin app with no token and hold
    # that app's token. A header check cannot close it: the frame can make the
    # request from the shell's own window (window.parent.fetch, or the shell's
    # launch function), with the shell's Origin, Sec-Fetch-Site, Referer and
    # cookie.
    #
    # An ISOLATED app's token is the exception, and it is closed below rather
    # than here: a browser never receives one from this route at all.
    # The window gets its token on its own origin (frame_token), where only
    # that app's code runs and only its code can read the answer — so nothing
    # on the desktop's origin, the shell included, ever holds Settings' token.
    #
    # LOAD-BEARING: "token-less" is a property of the CALL, not of the shell. Since
    # /shell-token the shell does hold a token (SHELL_APP_ID), and it stamps that
    # token on exactly the three chrome polls and nothing else. If a future change
    # installs a blanket fetch wrapper on the shell, this check starts seeing
    # app_id="__shell__" on every launch and refuses ALL of them — app launching
    # breaks wholesale. Widen the condition here in the same change, don't discover
    # it in production.
    caller = getattr(request.state, "app_token", None)
    if caller is not None and caller.app_id != app_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"app {caller.app_id!r} may not launch {app_id!r} or mint its token",
        )

    secret = getattr(request.app.state, "kernel_secret", None)
    if secret is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kernel secret not loaded")

    # An isolated app's token never goes to a browser from here. Every
    # page on the desktop's origin — the shell, Circles, the Workbench, every
    # Host-built app — makes the same request with the same headers and can
    # read what the shell reads, so a token handed to the shell is a token
    # handed to all of them. The window fetches its own from frame_token, on
    # its own origin. A browser is told by what a page cannot leave off:
    # Sec-Fetch-Site (every current browser, every request) and Origin (every
    # POST). A caller with neither is a script on the user's own session (the
    # CLI, smoke, a test harness) and keeps the token as before.
    withhold = _from_browser(request) and is_isolated(app.id, app.provenance)
    if withhold:
        import secrets as _secrets
        from server.applications.scopes import RETIRED_PERMISSIONS
        # Built here, not through tokens.mint, so drop retired permissions the
        # same way mint does: an old registry row still lists them, and the
        # launch response must never report a scope that grants nothing.
        token_str, decoded = None, tokens.AppToken(
            app_id=app.id,
            permissions=[p for p in app.permissions if p not in RETIRED_PERMISSIONS],
            window_id=_secrets.token_urlsafe(12), iat=0)
    else:
        token_str, decoded = tokens.mint(
            secret,
            app_id=app.id,
            permissions=app.permissions,
        )

    # If the app declares a process runtime, ensure its sidecar is
    # running before we hand back the entrypoint URL — otherwise the
    # window would race the spawn.
    try:
        manifest = load_manifest(Path(app.source_dir) / "app.yaml")
    except Exception as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"manifest load failed: {e}",
        ) from None

    if manifest.runtime.kind == "process":
        sup = supervisor.get_supervisor()
        if sup is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "supervisor unavailable"
            )
        try:
            await sup.open_window(
                installed=app,
                manifest=manifest,
                window_id=decoded.window_id,
            )
        except supervisor.SupervisorError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"sidecar launch failed: {e}",
            ) from None

    # An app whose server side runs in its own pod: the open window holds
    # that pod while its client beats (harness_backend.app_window_opened /
    # app_window_beat). The answer tells the shell
    # to beat for this window (window_beat_s).
    window_beat_s = None
    try:
        from server.applications.manifest import runs_in_pod
        if runs_in_pod(manifest):
            from server.applications import harness_backend
            harness_backend.app_window_opened(app.id, decoded.window_id)
            window_beat_s = harness_backend.WINDOW_BEAT_S
    except Exception as e:  # noqa: BLE001 — the launch itself succeeded
        log.debug("app-rt open hook (%s): %s", app.id, e)

    log.info(
        "launched %s (window=%s, kind=%s, perms=%d)",
        app.id, decoded.window_id, manifest.runtime.kind, len(decoded.permissions),
    )
    return {
        "token": token_str,
        # "app-origin": the window fetches its own token on its own origin
        # (frame_token); the desktop gets none.
        "token_delivery": "app-origin" if withhold else "launch",
        "app_id": decoded.app_id,
        "window_id": decoded.window_id,
        "permissions": decoded.permissions,
        "exp": decoded.exp,
        "runtime_kind": manifest.runtime.kind,
        "entrypoint": f"/system-apps/{app.id}/{app.entrypoint}",
        **({"window_beat_s": window_beat_s} if window_beat_s else {}),
    }


def _from_browser(request: Request) -> bool:
    """Did a browser send this? A page cannot omit Sec-Fetch-Site (sent by
    every current browser on every request) nor Origin (sent on every POST),
    and cannot forge either — both are forbidden request headers."""
    return bool((request.headers.get("sec-fetch-site") or "").strip()
                or request.headers.get("origin") is not None)


@router.post("/{app_id}/frame-token")
async def frame_token(app_id: str, request: Request) -> dict:
    """An isolated app's window token, minted ON that app's own origin.

    The window's page (dashboard/base/js/app-frame-init.js, which the kernel
    adds to an isolated app's HTML) asks for it with a same-origin fetch from
    apps-<key>-<handle>. Only that app's files are served on that origin
    (middleware/app_origin_guard.py), and no other origin can read the answer
    (no CORS anywhere in this kernel), so only the app itself receives its
    token. The token carries the origin's key and is honoured nowhere else
    (applications/middleware.py), so it is worthless if it ever leaves.

    Refused: on any origin but this app's own (the desktop included), from a
    browser page on another origin, for an app that is not isolated, and for
    an app whose origin key another installed app already holds."""
    from server.applications import app_origin
    origin = app_origin.classify(app_origin.browser_host(request.headers))
    if origin.kind != app_origin.APP or app_origin.origin_key(app_id) != origin.key:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "an app's window token is issued only on that app's own address",
        )
    _refuse_cross_origin_browser(request, "a window token")

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    app = await registry.get(pool, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"not installed: {app_id}")
    if app.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"app not active: {app.status}")
    if not is_isolated(app.id, app.provenance):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"{app_id!r} is not served from its own address")
    if (await _origin_keys(pool)).get(app.id) != origin.key:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"another installed app holds {app_id!r}'s address")
    caller = getattr(request.state, "app_token", None)
    if caller is not None and caller.app_id != app_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"app {caller.app_id!r} may not mint {app_id!r}'s token")

    secret = getattr(request.app.state, "kernel_secret", None)
    if secret is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kernel secret not loaded")
    token_str, decoded = tokens.mint(
        secret, app_id=app.id, permissions=app.permissions, origin_key=origin.key)
    log.info("window token for %s minted on its own origin (window=%s)",
             app.id, decoded.window_id)
    return {
        "token": token_str,
        "app_id": decoded.app_id,
        "window_id": decoded.window_id,
        "permissions": decoded.permissions,
    }


@router.get("/{app_id}/source")
async def app_source(app_id: str, request: Request):
    """The app's install dir as a tar.gz — pulled by the app's OWN runtime pod
    at start (Form 2). The pod holds
    nothing of this entity's data; it fetches exactly one thing, this app's
    source, presenting the per-app capability the operator placed in it
    (X-App-Runtime-Source-Token, derived by harness_backend.app_source_token).
    Constant-time compared; any other caller is a 403 regardless of source
    IP (the middleware lets the path reach this check, nothing more)."""
    from server.applications import harness_backend
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    presented = request.headers.get("X-App-Runtime-Source-Token", "")
    if not await harness_backend.check_app_source_token(pool, app_id, presented):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid source token")
    app = await registry.get(pool, app_id)
    if app is None or app.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"not installed: {app_id}")
    install_dir = installer.INSTALL_ROOT / app_id
    if not install_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"install dir missing: {app_id}")
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # The install dir as it is on disk (the landed, signed bundle). No
        # .git — the pod runs the app, it does not version it.
        for p in sorted(install_dir.rglob("*")):
            rel = p.relative_to(install_dir)
            if rel.parts and rel.parts[0] == ".git":
                continue
            if p.is_symlink():
                continue  # never carry a link out of the install dir
            tar.add(p, arcname=str(rel), recursive=False)
    from fastapi.responses import Response as _Resp
    return _Resp(content=buf.getvalue(), media_type="application/gzip",
                 headers={"Content-Disposition": f'attachment; filename="{app_id}.tar.gz"'})


@router.post("/{app_id}/runtime-log")
async def app_runtime_log(app_id: str, request: Request) -> dict:
    """What the app's OWN runtime pod printed, pushed back to its kernel.
    The pod's main container runs the
    manifest command under the toolchain image's wrapper
    (sidecars/app-runtime-python/run.py), which tees stdout+stderr here in
    batches, presenting the same per-app source token fetch.py presents to
    /source — the pod already held that token, and it reaches exactly these
    two routes. The kernel appends the lines to the SAME file the in-container
    supervisor writes for its sidecars (storage.workspace_dir/<logs>/
    sidecar.log), so `system.app_status` log_tail, the verify-after-ship
    loop and the host read one shape for both placements. A non-zero exit
    code is recorded on the app-runtime lane's state so status can say the
    process died even while the pod's own probe kept it 'Ready'.

    Body: {"lines": [str, ...], "exit_code": int | null}. Capped: lines beyond
    a batch ceiling are dropped with a note; the file rotates at the
    supervisor's rotation size. Any caller without the token is 403 regardless
    of source IP (the middleware lets the shape reach this check, nothing
    more)."""
    from server.applications import harness_backend
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    presented = request.headers.get("X-App-Runtime-Source-Token", "")
    if not await harness_backend.check_app_source_token(pool, app_id, presented):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid source token")
    # The app's own runtime pod runs the app's code: its body is read under a
    # cap, never whole (server/harness_input.py). 500 lines of
    # 4000 chars (the kernel's own caps below) is ~2 MB; 4 MiB bounds it.
    from server import harness_input as _hin
    try:
        body = await _hin.read_request_json(request, 4 * 1024 * 1024, "runtime log")
    except _hin.HarnessInputRefused as e:
        if "invalid JSON" in str(e):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
        raise HTTPException(413, str(e)) from None
    lines = body.get("lines") if isinstance(body, dict) else None
    if not isinstance(lines, list):
        lines = []
    # The caps are the kernel's, not the wrapper's: 500 lines of at most 4000
    # chars per POST, whatever the pod sends.
    lines = [str(x)[:4000] for x in lines[:500]]
    exit_code = body.get("exit_code") if isinstance(body, dict) else None
    pod = body.get("pod") if isinstance(body, dict) else None
    from server.applications.runtime_log import append_runtime_log, record_stats
    written = append_runtime_log(
        app_id, lines,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        pod=str(pod)[:253] if isinstance(pod, str) and pod else None)
    # {pid, mem_bytes} sampled by the wrapper — what the machine panel shows.
    stats = body.get("stats") if isinstance(body, dict) else None
    if isinstance(stats, dict):
        record_stats(app_id, stats)
    return {"ok": True, "app_id": app_id, "lines": written}


@router.post(
    "/{app_id}/window-beat",
    dependencies=[Depends(shell_origin_when_tokenless("keeping an app window's hold"))],
)
async def window_beat(app_id: str, request: Request) -> dict:
    """The shell says one window of an app that runs in its own pod is still
    open (every harness_backend.WINDOW_BEAT_S). The window holds the pod
    only while these arrive; a reloaded page or a closed tab stops them, and
    its hold ends. An app token may beat only for
    its own app, as with /close."""
    caller = getattr(request.state, "app_token", None)
    if caller is not None and caller.app_id != app_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"app {caller.app_id!r} may not hold {app_id!r}'s window",
        )
    body = await request.json() if int(request.headers.get("content-length", "0")) else {}
    window_id = body.get("window_id") if isinstance(body, dict) else None
    if not window_id or not isinstance(window_id, str) or len(window_id) > 128:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_id required in body")
    from server.applications import harness_backend
    return {"app_id": app_id, "window_id": window_id,
            "held": harness_backend.app_window_beat(app_id, window_id)}


@router.post(
    "/{app_id}/close",
    dependencies=[Depends(shell_origin_when_tokenless("closing an app window"))],
)
async def close_window(app_id: str, request: Request) -> dict:
    """Release one window's reference to an app's sidecar. The last
    close terminates the sidecar.

    A token-less caller is taken for the desktop shell, so the dependency
    first makes sure it IS the shell's own page: without it an isolated app
    could leave its token off and close any other app's window (the hole
    _refuse_isolated_origin's docstring describes for /launch)."""
    # C-2 self-check (mirrors launch_app): an app window carries its own
    # app-token. Allow it to close ITSELF, but refuse one app closing a
    # DIFFERENT app's window/sidecar (a cross-app denial-of-service —
    # terminating another, possibly more-privileged, app's process). The
    # desktop shell carries no app-token and may close any window.
    caller = getattr(request.state, "app_token", None)
    if caller is not None and caller.app_id != app_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"app {caller.app_id!r} may not close {app_id!r}'s window/sidecar",
        )
    body = await request.json() if int(request.headers.get("content-length", "0")) else {}
    window_id = body.get("window_id")
    if not window_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "window_id required in body"
        )
    sup = supervisor.get_supervisor()
    if sup is not None:
        await sup.close_window(app_id, window_id)
    # App runtime (helper) apps: the window closing is the release signal —
    # schedule the pod's scale-to-0 after the grace (a re-open cancels it;
    # the operator idle-sweep backstops a close that never fires). No-op for
    # every non-helper app: the hook checks its own state and returns.
    try:
        from server.applications import harness_backend
        harness_backend.app_window_closed(app_id, window_id)
    except Exception as e:
        log.debug("app-rt close hook (%s): %s", app_id, e)
    return {"closed": window_id, "app_id": app_id}


# The runtime switch's ops that only REDUCE running activity. The CEIGAS gate
# reads a request's op with runtime_op_of, exactly as app_runtime below does,
# and lets these two through the kill switch, audited (server/middleware/
# ceigas_gate.py STOP_ROUTES). start, enable and status are not stops.
RUNTIME_STOP_OPS = frozenset({"stop", "disable"})


def runtime_op_of(body: dict) -> str:
    """The op a /runtime body asks for. One reading, shared with the gate."""
    return str(body.get("op") or "status").strip().lower()


@router.post(
    "/{app_id}/runtime",
    dependencies=[Depends(shell_origin_when_tokenless("driving an app's backend"))],
)
async def app_runtime(app_id: str, request: Request) -> dict:
    """The user's switch for a process app's backend (resident lifecycle).
    Body {op}: 'status' | 'start' | 'stop' | 'enable' | 'disable'.

      start   — run it now with no window open (a resident app then stays up)
      stop    — stop it now, resident or not
      enable  — start now AND persist ON (boot + crash restart)
      disable — stop now AND persist OFF (nothing restarts it until enable)
      status  — {running, resident, enabled, lifecycle}

    Callers: the desktop shell (token-less — and the dependency holds a
    token-less caller to the shell's own page), the App Store (the panel that
    shows the switch), or the app itself. Any other app is refused — the
    same C-2 rule launch/close apply: one app must never stop another's
    process. Persistence goes through registry.set_resident_enabled, the
    kernel-owned column an app's own config store cannot reach.
    """
    caller = getattr(request.state, "app_token", None)
    if caller is not None and caller.app_id not in (app_id, "app-store"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"app {caller.app_id!r} may not drive {app_id!r}'s runtime",
        )
    body = await request.json() if int(request.headers.get("content-length", "0")) else {}
    op = runtime_op_of(body)
    if op not in ("status", "start", "stop", "enable", "disable"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown op {op!r}")

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    sup = supervisor.get_supervisor()
    if sup is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "supervisor unavailable")
    app = await registry.get(pool, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"not installed: {app_id}")

    lifecycle = None
    try:
        lifecycle = load_manifest(Path(app.source_dir) / "app.yaml").runtime.lifecycle
    except Exception:  # noqa: BLE001 - status must still answer for a bad manifest
        pass
    if op in ("start", "enable", "disable") and lifecycle != "resident":
        # A window-lifecycle app runs while its window is open; started with
        # no window it would sit ref-less until idle reclaim, and enable/
        # disable would persist a flag nothing reads. Restart is the tool for
        # a running one (system.restart_app); stop and status work for any.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{app_id!r} is not a resident app (runtime.lifecycle={lifecycle!r}); "
            f"{op} applies to runtime.lifecycle: resident only",
        )

    starting: str | None = None
    try:
        if op in ("start", "enable"):
            # Persist the switch BEFORE the start, and start as RESIDENT. The
            # user pressed the switch; whether the pod answers its first wake
            # in time is a separate fact. Measured on one Host:
            # 'enable' on a pod app whose first boot outran the wake
            # raised here, the switch was never written, the start ran with
            # the OLD switch (off) so no hold was taken, and the kernel's
            # next boot skipped the app again. Now the switch is on, the
            # hold is in place, and the lane keeps re-waking the pod.
            if op == "enable":
                await registry.set_resident_enabled(pool, app_id, True)
            try:
                await sup.start_app(app_id, pool=pool, resident=True)
            except supervisor.SupervisorError as e:
                # Pod apps: a resident hold survives an unanswered wake, so
                # this is "starting", not "failed" — report it as status
                # with the reason, never as a 502 that hides the hold.
                from server.applications import manifest as _manifest_mod
                m = _manifest_mod.load(Path(app.source_dir) / "app.yaml")
                if not _manifest_mod.runs_in_pod(m):
                    raise
                starting = str(e)
        elif op in ("stop", "disable"):
            await sup.stop_app(app_id)
            if op == "disable":
                await registry.set_resident_enabled(pool, app_id, False)
    except supervisor.SupervisorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{op} failed: {e}") from None

    app = await registry.get(pool, app_id) or app
    running = sup.get(app_id)
    return {
        "app_id": app_id,
        "op": op,
        "lifecycle": lifecycle,
        "running": bool(running and running.is_alive()),
        "resident": bool(running and running.resident) or starting is not None,
        "pid": running.pid if running else None,
        # None = untouched (the manifest decides); True/False = the user's word.
        "enabled": app.resident_enabled,
        # A pod app whose wake has not answered yet: the hold is in place and
        # the lane keeps re-waking it; this is the operator's last word.
        "starting": starting,
        # An always-on app in a crash loop: how many crashes in a row and when
        # the next restart comes ({state, crashes_in_a_row, delay_s, message});
        # None when it has not crashed (Supervisor.restart_status).
        "restarting": sup.restart_status(app_id),
    }


# Workbench project directories live on the
# entity's persistent volume, NOT image-baked /app/projects/ (which
# is wiped on pod restart and shared across entities running the
# same image — the same leak class was once found in local_grep). Both
# the claude-code sidecar's default cwd and this finalize endpoint
# now agree on /var/lib/cseries/projects/<id>/.
PROJECTS_ROOT = Path("/var/lib/cseries/projects")


# ── The browser proof switch ─────────────────────────────────────────────
# app_verify's page stage wakes the Chromium sidecar on every would-be
# DELIVERED proof, including plan-end auto-ships and repair rounds with
# nobody present. It is OFF until the user turns it on here; the key lives in
# the entity's own settings blob beside the time zone and the explanation
# level (behavioral_params.browser_proof). Same actor as shipping.

async def _read_behavioral_params(pool, eid: int) -> dict:
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    bp = row["behavioral_params"] if row and row["behavioral_params"] else {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    return dict(bp or {})


@router.get(
    "/proof/browser",
    dependencies=[Depends(requires_permission("execute:ship_project"))],
)
async def get_browser_proof() -> dict:
    """Whether a delivered proof also loads the page in the browser sidecar."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    bp = await _read_behavioral_params(pool, brain_service._entity_id)
    return {"browser_proof": bp.get("browser_proof") is True}


@router.put(
    "/proof/browser",
    dependencies=[Depends(requires_permission("execute:ship_project"))],
)
async def set_browser_proof(request: Request) -> dict:
    """Body {"enabled": true|false}. On = the next delivered proof wakes the
    browser sidecar (billed on the user's plan, released after the check)."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    enabled = (body or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "enabled must be true or false")
    eid = brain_service._entity_id
    bp = await _read_behavioral_params(pool, eid)
    if enabled:
        bp["browser_proof"] = True
    else:
        bp.pop("browser_proof", None)
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(bp), eid)
    log.info("browser proof %s (eid=%s)", "ON" if enabled else "off", eid)
    return {"browser_proof": enabled}


@router.post(
    "/build",
    # execute:ship_project, NOT execute:install. Shipping your own
    # completed project is a narrower act than installing arbitrary bundles,
    # buying from the store, or uninstalling apps — see scopes.py. The in-pod
    # loopback (mcp.py _tool_ship_project) is unaffected: it authorizes via
    # x-cseries-internal-auth and returns before this dependency's token check.
    dependencies=[Depends(requires_permission("execute:ship_project"))],
)
async def build_from_project(request: Request) -> dict:
    """Pack a completed Workbench project into a signed .cseries bundle, sign it
    with the per-container entity key, and install it via the normal install
    pipeline. The result is a provenance=custom app launchable from the dashboard.

    Body: {"project_id": int}

    Project requirements:
      - row in `projects` table (any status EXCEPT a build in flight — see
        _pack_and_sign_project; shippability is a property of the TREE, not the
        plan state, and a mid-plan ship is disclosed via `open_phases` rather
        than refused)
      - directory at /var/lib/cseries/projects/<id>/ containing app.yaml + assets
      - app.yaml must declare `provenance: custom`

    Thin adapter: the pack+sign+install logic + every security
    invariant (no build in flight, provenance=custom, build-contract validator,
    sign-time SDK integrity gate, SDK-binding, entity-key signing, entitlement-free
    self-install) lives in ship_project_service so the ship_project_app host tool
    shares the exact path without faking a Request. Maps ShipError → HTTPException;
    response shape unchanged.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None

    try:
        project_id = int(body.get("project_id"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project_id (int) required") from None

    try:
        # `module`: install one module of the project.
        return await ship_project_service(pool, project_id, module=(str(body.get("module") or "").strip().lower() or None))
    except ShipError as e:
        raise HTTPException(e.status, e.detail)


@router.post(
    "/install",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def install_app(request: Request) -> dict:
    """Install a signed .cseries bundle. Two body shapes:

    1. **Raw bundle bytes** (Content-Type: application/zip, application/
       octet-stream, or unset) — verified + extracted as before. This
       is the legacy CLI path.
    2. **JSON `{app_id, version?}`** (Content-Type: application/json) —
       the kernel fetches the signed bundle from the app store via the
       bridge, then runs the same install. This is the post-app-store
       v1 path that decouples app distribution from the kernel image.

    Auth: `execute:install` permission required (same as
    /api/applications/build). The install endpoint was once
    unauthenticated, letting any in-pod actor that
    could reach kernel HTTP submit signed bundles. The Ed25519 + trust
    store gate still applies; this adds the second layer.

    Trust path on first boot: `default_install.py` calls
    `installer.install_bundle` directly (no HTTP), bypassing this route
    entirely, so the auth requirement doesn't break the bootstrap.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    from server.applications.platform_tools import (
        allowed_for_entity, current_entity_id,
    )
    eid = current_entity_id()

    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
            app_id = str(body["app_id"]).strip()
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"JSON body must include app_id ({e})")
        # Platform-tool gate (defense in depth — purchase also enforces).
        if not allowed_for_entity(app_id, eid):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{app_id!r} is a platform tool; this host is not in the install allow-list",
            )
        version = (body.get("version") or "").strip() or None
        try:
            data = await _fetch_bundle_from_store(app_id, version)
        except _StoreError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    else:
        data = await request.body()
        if not data:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty bundle")

    try:
        app = await installer.install_bundle(pool, data, source="manual")
    except installer.AppRuntimeOverPlan as e:
        # 402, not 400: this app is fine, the machine is full. The payload
        # carries needs_mi / headroom_mi / upgrade_to so the surface can render
        # the upgrade instead of "install failed" — the same shape the
        # wake-time paywall already returns. The install rolled itself back.
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, {
            "error": "capability_over_plan",
            "app_id": e.app_id,
            "needs_mi": e.needs_mi,
            "headroom_mi": e.headroom_mi,
            "upgrade_to": e.upgrade_to,
            "message": str(e),
        }) from None
    except installer.InstallError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    # Post-install PLATFORM_TOOLS allow-list check (a SEPARATE gate from
    # target_entities — that one is now enforced inside install_bundle and would
    # have raised before reaching here). This catches a hardcoded platform-tool
    # installed on an entity that isn't in its allow-list; roll back if so.
    if not allowed_for_entity(app.id, eid):
        # Roll back: uninstall the freshly-installed app.
        try:
            await installer.uninstall_app(pool, app.id, purge=True)
        except Exception:
            pass
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{app.id!r} is a platform tool; this host is not in the install allow-list",
        )
    return _serialize(app)


class _StoreError(Exception):
    """`status` is the store's own answer where it gave one (the bridge
    renders the controller's status as "bundle fetch <status>"): a 404 is an
    app id the store does not have — the caller's outcome
    (tool_call_ledger.refused_if_caller). Unreachable, partial or empty
    answers carry none: the platform's."""

    def __init__(self, msg: str = "", *, status: int | None = None):
        super().__init__(msg)
        self.status = status


async def _fetch_bundle_from_store(app_id: str, version: str | None) -> bytes:
    """Pull a signed .cseries bundle from the central app store via the
    bridge. Returns the raw bytes ready to pass to installer.install_bundle."""
    import base64
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise _StoreError(f"bridge unavailable: {e}") from None
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise _StoreError("bridge not connected")
    # Fetched in slices: one answer has to fit
    # this kernel's 10 MiB bridge reader line as base64, so a bundle over
    # ~7.5 MiB could never be installed. A bridge or store without slices
    # answers with the whole bundle and no total_size, which is used as is.
    import hashlib
    params = {"app_id": app_id, "offset": 0, "length": _STORE_SLICE_BYTES}
    if version:
        params["version"] = version
    buf = bytearray()
    while True:
        try:
            envelope = await bc.request_full("appstore.fetch_bundle", params)
        except Exception as e:
            raise _StoreError(f"store unreachable: {e}") from None
        if not envelope.get("ok"):
            _err = envelope.get("error") or "store returned no data"
            _m = re.match(r"bundle fetch (\d{3})\b", str(_err))
            raise _StoreError(_err, status=int(_m.group(1)) if _m else None)
        data = envelope.get("data") or {}
        b64 = data.get("bundle_b64")
        if not b64 and not buf:
            raise _StoreError("store returned no bundle bytes")
        try:
            chunk = base64.b64decode(b64 or "")
        except Exception as e:
            raise _StoreError(f"bundle b64 invalid: {e}") from None
        total = int(data.get("total_size") or 0)
        if not total:
            if buf:
                raise _StoreError("the store stopped answering in slices part-way through")
            return chunk
        if data.get("version"):
            params["version"] = data["version"]       # every slice from one release
        buf += chunk
        if len(buf) >= total:
            break
        if not chunk:
            raise _StoreError(f"the store sent {len(buf)} of {total} bytes and then nothing")
        params["offset"] = len(buf)
    want = data.get("sha256")
    if len(buf) != total or (want and hashlib.sha256(buf).hexdigest() != want):
        raise _StoreError(f"the bundle did not arrive whole ({len(buf)} of {total} bytes, or "
                          "its sha256 does not match)")
    return bytes(buf)


# One slice of a store bundle: the bridge's _BUNDLE_FETCH_PART_MAX (6 MiB),
# whose base64 fits this kernel's 10 MiB bridge reader line.
_STORE_SLICE_BYTES = 6 * 1024 * 1024


@router.get("/store/icon/{app_id}")
async def store_icon(app_id: str):
    """Proxy the catalog icon for an app through the bridge so the
    Settings → Apps tab can render it without exposing the platform
    operator directly to the dashboard."""
    import base64
    from fastapi.responses import Response
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")
    try:
        envelope = await bc.request_full("appstore.icon", {"app_id": app_id})
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, envelope.get("error") or "no icon")
    data = envelope.get("data") or {}
    b64 = data.get("icon_b64")
    if not b64:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no icon")
    try:
        png = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "icon b64 invalid")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/store/preview/{app_id}")
async def store_preview(app_id: str, version: str | None = None) -> dict:
    """The declared access an app requests — its `network[]` egress hosts, its
    permission scopes, and its provenance — read from the signed store bundle's
    own manifest. Drives the App Store install-consent surface so a user sees
    where a (community) app may phone home and what it can touch BEFORE they
    install it. Read-only metadata; degrades to ok=false (never errors) for an
    image-shipped first-party app that isn't in the central store."""
    import yaml as _yaml
    empty = {"ok": False, "app_id": app_id, "provenance": None,
             "permissions": [], "network": [],
             "runtime_kind": None, "baseline_memory_mi": None,
             "license": {}, "source": {}, "build": {}}
    try:
        data = await _fetch_bundle_from_store(app_id, version)
    except _StoreError as e:
        return {**empty, "reason": str(e)}
    try:
        contents = bundles._read_zip(data)
        m = _yaml.safe_load(contents.manifest_yaml) or {}
    except Exception as e:
        return {**empty, "reason": f"manifest unreadable: {e}"}
    prov = str(m.get("provenance") or m.get("tier") or "standard")
    if prov == "premium":
        prov = "standard"
    # A retired permission is not shown at consent: the bundle may predate its
    # retirement, and it grants nothing (scopes.RETIRED_PERMISSIONS).
    from server.applications.scopes import RETIRED_PERMISSIONS
    perms = [str(p) for p in (m.get("permissions") or [])
             if str(p) not in RETIRED_PERMISSIONS]
    net = [str(h).strip() for h in (m.get("network") or []) if str(h).strip()]
    # Cost surface (App Store rebuild): the same pre-install
    # consent moment that shows egress + permissions now also says WHERE the
    # app runs and what it draws from the plan's machine memory. Read
    # tolerantly off the raw manifest (this route must keep degrading, never
    # erroring, on odd bundles) — a malformed figure renders as no figure,
    # which the store shows as "no declared cost", never as 0.
    rt_kind = str((m.get("runtime") or {}).get("kind") or "static") \
        if isinstance(m.get("runtime") or {}, dict) else "static"
    baseline_mi = None
    res_raw = m.get("resources")
    if isinstance(res_raw, dict):
        bm = res_raw.get("baseline_memory_mi")
        if isinstance(bm, int) and not isinstance(bm, bool) and bm > 0:
            baseline_mi = bm
    # The recipe and the licence: the same consent moment shows the author's terms in the
    # rights' own names, where the code came from, and how it is assembled.
    # Read tolerantly off the raw manifest like everything above — a
    # malformed block renders as absent, never as a granted right.
    lic_raw = m.get("license") if isinstance(m.get("license"), dict) else {}
    lic = {k: (lic_raw.get(k) is True)
           for k in ("publish_site", "redistribute", "source_visible")}
    lic["flavors"] = str(lic_raw.get("flavors") or "open")
    lic["notes"] = str(lic_raw.get("notes") or "")
    src_raw = m.get("source") if isinstance(m.get("source"), dict) else {}
    up_raw = src_raw.get("upstream") if isinstance(src_raw.get("upstream"), dict) else {}
    src = {"repo": src_raw.get("repo") or None,
           "commit": src_raw.get("commit") or None,
           "upstream_url": up_raw.get("url") or None,
           "upstream_ref": up_raw.get("ref") or None,
           "upstream_license": up_raw.get("license") or None}
    bld_raw = m.get("build") if isinstance(m.get("build"), dict) else {}
    bld = {"lockfile": bld_raw.get("lockfile") or None,
           "lockfile_sha256": bld_raw.get("lockfile_sha256") or None}
    return {"ok": True, "app_id": app_id, "provenance": prov,
            "permissions": perms, "network": net,
            "runtime_kind": rt_kind, "baseline_memory_mi": baseline_mi,
            "license": lic, "source": src, "build": bld}


@router.get("/store/catalog")
async def store_catalog(request: Request) -> dict:
    """List installable apps from the central store, joined with this
    entity's currently-installed versions. Drives the Settings → Apps tab."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")
    try:
        envelope = await bc.request_full("appstore.list", {})
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            envelope.get("error") or "store error")
    catalog = (envelope.get("data") or {}).get("apps", [])
    # Join with local install state.
    installed_rows = await registry.list_active(pool)
    installed_by_id = {a.id: a for a in installed_rows}

    # Local manifests (one read per installed app, reused below for three
    # different annotations):
    #   deprecated          — lives in the on-disk app.yaml, not yet a DB column.
    #   runtime_kind        — static / process / external: WHERE the app runs,
    #                         which the store's card copy is built on.
    #   baseline_memory_mi  — the declared machine cost (manifest.ResourcesSpec).
    from pathlib import Path
    from server.applications.manifest import load as load_manifest, ManifestError
    deprecated_ids: set[str] = set()
    local_manifests: dict[str, object] = {}
    for app in installed_rows:
        if app.source_dir:
            try:
                m = load_manifest(Path(app.source_dir) / "app.yaml")
                local_manifests[app.id] = m
                if getattr(m, "deprecated", False):
                    deprecated_ids.add(app.id)
            except (ManifestError, OSError, FileNotFoundError):
                pass  # manifest gone / unreadable; treat as not-deprecated

    # Platform-tool gate: production entities don't see dev/platform apps
    # (cert-inspector, training-memory) at all.
    from server.applications.platform_tools import (
        allowed_for_entity, current_entity_id,
    )
    eid = current_entity_id()

    # The image-baked set, read from the baked bundles' own manifests
    # (installer.image_baked_ids — the same source install_image_bundles
    # trusts). App Store rebuild: a static app the image
    # ships is not a choice — it uses no memory and there is nothing to
    # install — so the store hides it from the browse grid. The flag is an
    # ANNOTATION, not a server-side filter, because runtime-bearing
    # image-baked apps (VS Code) DO belong in the store: where they run and
    # what they cost is exactly what the store now explains.
    image_baked = installer.image_baked_ids()

    def _cost_fields(aid: str) -> dict:
        """runtime_kind + baseline_memory_mi from the local manifest, when
        installed and readable. Absent stays absent — the store renders a
        missing figure as 'no declared cost', never as 0."""
        m = local_manifests.get(aid)
        if m is None:
            return {"runtime_kind": None, "runtime_lifecycle": None,
                    "resident_enabled": None, "baseline_memory_mi": None}
        local = installed_by_id.get(aid)
        return {
            "runtime_kind": getattr(getattr(m, "runtime", None), "kind", None),
            # resident lifecycle: the store's cost line says
            # "runs 24/7" and shows the user's on/off for it.
            "runtime_lifecycle": getattr(getattr(m, "runtime", None), "lifecycle", None),
            "resident_enabled": getattr(local, "resident_enabled", None),
            "baseline_memory_mi": getattr(
                getattr(m, "resources", None), "baseline_memory_mi", None),
        }

    out = []
    for entry in catalog:
        aid = entry["app_id"]
        if not allowed_for_entity(aid, eid):
            continue  # platform tool, this entity isn't on the allow-list
        local = installed_by_id.get(aid)
        installed_ver = local.version if local else None
        latest = entry.get("latest_version")
        status_str = "not_installed"
        if local:
            if latest and installed_ver == latest:
                status_str = "up_to_date"
            elif latest and installed_ver != latest:
                status_str = "update_available"
            else:
                status_str = "installed"
        out.append({
            **entry,
            "installed_version": installed_ver,
            "install_status": status_str,
            "deprecated": aid in deprecated_ids,
            "image_baked": aid in image_baked,
            **_cost_fields(aid),
        })

    # Image-baked apps WITH a runtime that have no store row (the image is
    # their whole distribution — e.g. VS Code, distribution='image'
    # platform-side, so appstore.list never carries them). They still belong
    # on the store's shelf: they are apps the user opens that run in their own
    # container, and the store is where "where does this run / what does it
    # cost" is answered. Served from the local registry — real install state,
    # real manifest — and marked local_only so the UI knows there is nothing
    # to fetch from the central store for them.
    listed = {e["app_id"] for e in out}
    for app in installed_rows:
        m = local_manifests.get(app.id)
        if (app.id in listed or app.id not in image_baked or m is None
                or getattr(getattr(m, "runtime", None), "kind", "static")
                == "static"):
            continue
        icon = getattr(m, "icon", None)
        out.append({
            "app_id": app.id,
            "name": app.name,
            "summary": getattr(m, "description", "") or "",
            # The image lane is first-party by construction (native-bundles
            # are packed from this repo and signed with the C-Series key), so
            # naming C-Series as publisher states the provenance, not a guess.
            "publisher": "C-Series",
            "is_default": False,
            "latest_version": app.version,
            "size_bytes": None,
            "published_at": None,
            "price_micros": 0,
            "installed_version": app.version,
            "install_status": "installed",
            "deprecated": app.id in deprecated_ids,
            "image_baked": True,
            "local_only": True,
            "icon_url": (f"/system-apps/{app.id}/{icon}" if icon else None),
            **_cost_fields(app.id),
        })
    return {"apps": out}


@router.post(
    "/store/purchase",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def store_purchase(request: Request):
    """Record a FREE-path entitlement (free app, or publisher self-purchase).
    Proxies to the bridge's appstore.purchase op. Idempotent on this
    entity + app — re-call is safe.

    PAID apps: Stripe Connect is the single purchase lane, on every surface
    and every channel. The bridge answers
    {checkout_required: true}; this route lifts that to 409 so the frontend
    switches to /store/checkout. There is no stored balance to pay from.

    Returns:
      200  {entitlement_id, is_free: true, ...}                free/self recorded
      200  {already_owned: true, entitlement_id, ...}          already entitled
      409  {checkout_required: true, reason, price_micros}     paid app — use
                                                               /store/checkout
    """
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")

    # Platform-tool gate: production entities can't purchase dev/platform apps.
    from server.applications.platform_tools import (
        allowed_for_entity, current_entity_id, is_platform_tool,
    )
    eid = current_entity_id()
    if not allowed_for_entity(app_id, eid):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{app_id!r} is a platform tool; this host is not in the install allow-list",
        )

    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")

    try:
        envelope = await bc.request_full("appstore.purchase", {"app_id": app_id})
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        # Bridge dispatched, op raised a hard error (unknown app, etc.)
        error = envelope.get("error") or "purchase failed"
        code = int(envelope.get("code") or 502)
        raise HTTPException(code, error)

    data = envelope.get("data") or {}
    # Paid app — structured 409, the frontend goes through Stripe checkout.
    if data.get("checkout_required"):
        return JSONResponse(status_code=409, content=data)
    return data


@router.post(
    "/store/refund",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def store_refund(request: Request):
    """SELLER refunds a buyer. The seller's call, on the seller's policy.

    There is NO platform refund window: apps run inside
    the buyer's own container so revocation cannot un-deliver them, and
    under Connect direct charges the money is the seller's, so a mandate
    would impose the loss on indie devs. The seller decides, against the
    refund policy they published on the listing.

    Proxies appstore.refund, which requires the CALLER to be the app's
    publisher and names the buyer. A non-publisher gets 403 from the bridge
    and learns nothing about who owns what.

    body: {app_id, buyer_entity_id, reason?}

    The refund is always full, and the platform's 10% comes back to the
    seller with it — a reversed sale costs the seller the sale, not the sale
    plus our cut.

    Returns:
      200  {refunded: true, lane: 'connect', ...}              money returned
      200  {already_refunded: true}                            idempotent
      403  not this app's publisher
      409  {not_refundable: true, reason: 'free_app'|
            'no_payment_reference'}                            nothing to refund
      503  {refund_unavailable: true, reason: ...}             inert config wall
    """
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")
    try:
        buyer_entity_id = int(body.get("buyer_entity_id"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "buyer_entity_id required") from None
    reason = str(body.get("reason") or "").strip()[:1000]

    # Same accessor + guard as every other bridge route in this file
    # (server/main.py:60 — returns the BridgeClient, or None before
    # lifespan boot).
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")

    try:
        envelope = await bc.request_full(
            "appstore.refund",
            {"app_id": app_id, "buyer_entity_id": buyer_entity_id,
             "reason": reason})
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        error = envelope.get("error") or "refund failed"
        code = int(envelope.get("code") or 502)
        raise HTTPException(code, error)

    data = envelope.get("data") or {}
    if data.get("refund_unavailable"):
        # Inert-config / not-connected walls — structured 503.
        return JSONResponse(status_code=503, content=data)
    if data.get("not_refundable"):
        # Not an error, but not a refund either — 409 so the UI renders the
        # reason instead of a success toast.
        return JSONResponse(status_code=409, content=data)
    return data


async def _store_bridge_call(op: str, payload: dict):
    """Shared bridge hop for the store routes below.

    Same accessor + guard as every other bridge route in this file:
    server/main.py:60-62 defines get_bridge_client() as returning the running
    BridgeClient "or None before lifespan boot", so the None arm is live, not
    defensive padding. Lifted out so three near-identical proxies do not each
    re-derive it and drift apart.
    """
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")
    try:
        envelope = await bc.request_full(op, payload)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        raise HTTPException(int(envelope.get("code") or 502),
                            envelope.get("error") or f"{op} failed")
    return envelope.get("data") or {}


@router.post(
    "/store/refund/request",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def store_refund_request(request: Request):
    """BUYER asks the seller for a refund. Moves no money, grants nothing.

    The platform sets no refund window and does not refund on the seller's
    behalf. This only puts the ask somewhere the seller
    can see it — appstore.refund.request. The seller decides.

    Deliberately NOT the same route as /store/refund: that one moves money
    and is publisher-only. Sharing a path would invite a UI to call the
    wrong one.

    body: {app_id, reason?}
    Returns 200 {requested, seller_refund_policy, policy_stated,
    decided_by:'seller'} · 404 if you have no active purchase of that app.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")
    reason = str(body.get("reason") or "").strip()[:1000]
    return await _store_bridge_call(
        "appstore.refund.request", {"app_id": app_id, "reason": reason})


@router.post(
    "/store/publisher/refund-policy",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def store_set_refund_policy(request: Request):
    """SELLER sets the refund policy shown on their listing.

    The platform stores and displays this text; it does not parse or enforce
    it. Sending an empty string CLEARS it, which means "the seller has not
    stated a policy" — NOT "no refunds". Callers must render that absence
    explicitly; policy_stated in the response is what to branch on, never
    the emptiness of the string.

    body: {app_id, refund_policy}   ·   403 if the app is not yours.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")
    if "refund_policy" not in body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "refund_policy required (empty string clears it)")
    return await _store_bridge_call(
        "appstore.publisher.set_refund_policy",
        {"app_id": app_id, "refund_policy": body.get("refund_policy")})


@router.get(
    "/store/publisher/refund-requests",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def store_refund_requests() -> dict:
    """The refund asks waiting on YOU as publisher. Read-only, oldest first.

    Publisher-scoped in the bridge, so this only ever returns asks against
    apps you publish. Answering one means calling /store/refund — which is
    a decision, not a formality.
    """
    return await _store_bridge_call("appstore.publisher.refund_requests", {})


@router.post(
    "/store/checkout",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def store_checkout(request: Request):
    """Mint a Stripe Connect Checkout Session for a PAID app — the human
    purchase lane. Proxies appstore.checkout.create;
    the bridge fixes the buyer from the mTLS handshake and creates a
    direct-charge session on the PUBLISHER's connected account. The
    frontend opens checkout_url in a new tab and polls /store/entitlement
    until the controller-side webhook fulfillment lands the entitlement.

    Returns:
      200  {checkout_url, session_id}                          go pay
      200  {checkout_required: false, reason: 'free_app'|
            'self_purchase'}                                   use /store/purchase
      200  {already_owned: true, entitlement_id}               just install
      503  {checkout_unavailable: true, reason:
            'stripe_not_configured'|'publisher_not_connected'} show state
    """
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")

    from server.applications.platform_tools import (
        allowed_for_entity, current_entity_id,
    )
    eid = current_entity_id()
    if not allowed_for_entity(app_id, eid):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{app_id!r} is a platform tool; this host is not in the install allow-list",
        )

    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")

    params = {"app_id": app_id}
    for k in ("success_url", "cancel_url"):
        v = body.get(k)
        if isinstance(v, str) and v:
            params[k] = v[:512]
    try:
        envelope = await bc.request_full("appstore.checkout.create", params)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        error = envelope.get("error") or "checkout failed"
        code = int(envelope.get("code") or 502)
        raise HTTPException(code, error)
    data = envelope.get("data") or {}
    if data.get("checkout_unavailable"):
        # Inert-config / not-connected walls — structured 503 so the UI
        # renders the appropriate state from `reason`.
        return JSONResponse(status_code=503, content=data)
    return data


@router.get(
    "/store/entitlement",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def store_entitlement(request: Request) -> dict:
    """Does THIS entity own app_id? Thin proxy over the same
    appstore.entitlement.check the installer gate uses — the store UI
    polls this after opening Stripe checkout to flip Buy → Install when
    the webhook fulfillment lands."""
    app_id = str(request.query_params.get("app_id") or "").strip()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")
    try:
        envelope = await bc.request_full("appstore.entitlement.check", {"app_id": app_id})
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"store unreachable: {e}")
    if not envelope.get("ok"):
        raise HTTPException(int(envelope.get("code") or 502),
                            envelope.get("error") or "entitlement check failed")
    return envelope.get("data") or {}


# ── Publisher proxy routes (PR1.5) ────────────────────────────────
#
# The Publisher app calls these from the in-pod kernel. Each proxies a
# caller-scoped bridge op; the bridge enforces entity_id from the mTLS
# handshake, so a publisher cannot read another publisher's books or
# set someone else's prices regardless of what the kernel sends.

async def _publisher_call(op: str, params: dict) -> dict:
    """Thin adapter over publisher_op_service — the caller-scoped publisher/store
    bridge gate + dispatch now lives in the service so the publish_app host tool
    shares it. Maps PublisherError → HTTPException; the bridge still
    fixes entity_id from the mTLS handshake, so a publisher cannot read another
    publisher's books or set someone else's prices. Response shape unchanged."""
    try:
        return await publisher_op_service(op, params)
    except PublisherError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/publisher/my-apps", dependencies=[Depends(requires_permission("publish:apps"))])
async def publisher_my_apps() -> dict:
    return await _publisher_call("appstore.publisher.my_apps", {})


@router.get("/publisher/sales", dependencies=[Depends(requires_permission("publish:apps"))])
async def publisher_sales(request: Request) -> dict:
    qs = request.query_params
    params: dict = {}
    app_id = (qs.get("app_id") or "").strip()
    if app_id:
        params["app_id"] = app_id
    limit = qs.get("limit")
    if limit:
        try:
            params["limit"] = int(limit)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be int")
    return await _publisher_call("appstore.publisher.sales", params)


@router.post(
    "/publisher/set-price",
    dependencies=[Depends(requires_permission("write:billing"))],
)
async def publisher_set_price(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    # platform_fee_bps is PLATFORM-OWNED (10% standard);
    # the bridge rejects it too, this just fails fast with the same message.
    if "platform_fee_bps" in body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "platform_fee_bps is platform-owned")
    params = {
        "app_id": body.get("app_id"),
        "price_micros": body.get("price_micros"),
    }
    return await _publisher_call("appstore.publisher.set_price", params)


# ── Publish + yank from the Publisher app (Ship D) ────────────────
#
# The Publisher system app's new Publish tab POSTs here. Multipart
# upload carries the app zip; form fields carry version/price/etc.
# Gated on the new publish:apps scope so only apps that declare it
# (today: the Publisher system app itself) can publish through here.

@router.post(
    "/publisher/publish",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def publisher_publish(request: Request) -> dict:
    import base64
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file (zip) required")
    raw = await upload.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty zip")
    # The early guard before round-tripping the bytes, the same bound the
    # platform applies: no zip larger than what its zip-bomb guard lets a
    # bundle unpack to can pass there (it was a flat 50 MB). Past one
    # bridge frame the bundle goes up in parts
    # (bridge_client.stage_bundle_params).
    if len(raw) > _BUNDLE_ZIP_MAX:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"zip too large: {len(raw) // (1024 * 1024)} MB. The platform takes a bundle "
            f"that unpacks to at most {_BUNDLE_UNPACKED_MAX // (1024 * 1024)} MB in "
            f"{_BUNDLE_FILES_MAX} files, so no zip over {_BUNDLE_ZIP_MAX // (1024 * 1024)} MB "
            "can pass.",
        )

    # Strict admission bar for untrusted (community) apps:
    # no privileged scopes, SDK-bound. First-party tiers skip it.
    from server.applications.build_contract import validate_publish
    _pub_errors = validate_publish(raw)
    if _pub_errors:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "publish rejected (untrusted-app bar): " + "; ".join(_pub_errors),
        )
    # The licence gate — same rule as
    # the Workbench lane: bytes of someone else's installed app whose licence
    # withholds redistribution do not leave this machine as a new app.
    _pool = brain_service.get_pool()
    if _pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "cannot check licences (pool down) — publish refused")
    from server.applications import licensing as _licensing
    _c = bundles._read_zip(raw)
    _ff_form = form.get("forked_from_app_id")
    _lic_errs = await _licensing.publish_gate(
        _pool, payload=_c.payload, binaries=_c.binaries,
        forked_from_app_id=(str(_ff_form) if _ff_form else None))
    if _lic_errs:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "publish refused (licence): " + " ".join(_lic_errs))

    def _intf(key: str, default: int = 0) -> int:
        v = form.get(key)
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be int") from None

    def _strf(key: str) -> str | None:
        v = form.get(key)
        return str(v) if v not in (None, "") else None

    def _boolf(key: str) -> bool:
        v = form.get(key)
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return bool(v)

    params = {
        "zip_b64": base64.b64encode(raw).decode("ascii"),
        "price_micros": _intf("price_micros", 0),
        "audience": _strf("audience") or "public",
        "is_default": _boolf("is_default"),
    }
    if _strf("version"):
        params["version"] = _strf("version")
    if _strf("summary"):
        params["summary"] = _strf("summary")
    # Store lineage: agreement acceptance + fork provenance +
    # the publisher's own fork policy ride the same submit. The operator
    # validates; this route only relays what the UI collected.
    if form.get("agreement_version") not in (None, ""):
        params["agreement_version"] = _intf("agreement_version")
    if _strf("fork_policy"):
        params["fork_policy"] = _strf("fork_policy")
    if _strf("forked_from_app_id"):
        params["forked_from_app_id"] = _strf("forked_from_app_id")
    if _strf("forked_from_version"):
        params["forked_from_version"] = _strf("forked_from_version")
    # audience='circle' on the HAND-UPLOAD lane too, not only the Workbench one.
    # A gate that covers the button we happened to think of is the shape
    # _license_gate_site_zip's docstring already names as the failure: "the
    # honest path has to be gated everywhere it exists". Both lanes reach the
    # same store op, so both record the same local obligation.
    if (params["audience"] or "public") == "circle":
        from server.applications import appstore_circle
        from server.groups.roster import RosterError
        group_ref = _strf("group_ref")
        if not group_ref:
            raise PublisherError(status.HTTP_400_BAD_REQUEST,
                                 "audience 'circle' needs group_ref — the group whose "
                                 "roster may see and fetch this app")
        pool = brain_service.get_pool()
        if pool is None:
            raise PublisherError(status.HTTP_503_SERVICE_UNAVAILABLE, "host not ready")
        _circle_app_id = _declared_app_id_in_zip(raw)
        if not _circle_app_id:
            raise PublisherError(status.HTTP_400_BAD_REQUEST,
                                 "this bundle declares no app id — a circle app is keyed "
                                 "on one, so there is nothing to gate")
        try:
            await appstore_circle.check_publishable(pool, group_ref)
        except RosterError as e:
            raise PublisherError(e.status, e.detail)
        params["group_ref"] = group_ref
        _out = await _publisher_call("appstore.publish_bundle", params)
        try:
            await appstore_circle.record_publication(pool, _circle_app_id, group_ref)
        except Exception as _rp:
            log.warning("circle publication record failed for %s (non-fatal): %s",
                        _circle_app_id, _rp)
        return _out
    return await _publisher_call("appstore.publish_bundle", params)


@router.get(
    "/store/groups",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def publisher_owned_groups() -> dict:
    """The groups this host OWNS, for the circle-audience picker.

    OWNED ONLY, and it is not a convenience filter: a circle app is gated on a
    roster its publisher holds, so a group this host merely replicates is a
    choice the publish route would refuse (appstore_circle.record_publication).
    Offering it would be offering a button that cannot work.

    It lives here, behind `publish:apps`, rather than sending the Publisher app
    to /api/groups behind `read:group`. The Publisher needs exactly one fact —
    which rosters it may gate an app on — and granting it the whole group
    surface to learn that would widen the app's reach for a dropdown.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "host not ready")
    from server.groups import roster as _roster
    rows = await _roster.list_groups(pool, int(brain_service._entity_id))
    return {"groups": [
        {"group_id": r["group_id"], "name": r["name"], "members": r["members"]}
        for r in rows if r.get("owned") and r.get("revoked_at") is None
    ]}


@router.get(
    "/publisher/agreement",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def publisher_agreement() -> dict:
    """The current publishing agreement (version + text) from the store.
    The Publisher UI presents it; the accept checkbox sends its version
    back with the publish — that round-trip is the recorded acceptance."""
    return await _publisher_call("appstore.agreement", {})


@router.get(
    "/publisher/requests",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def publisher_requests() -> dict:
    """This publisher's review-queue requests (every publish is human-
    reviewed before it appears; this is the status surface)."""
    return await _publisher_call("appstore.publish_requests", {})


@router.post(
    "/publisher/yank",
    dependencies=[Depends(requires_permission("publish:apps"))],
)
async def publisher_yank(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    params = {
        "app_id": body.get("app_id"),
        "version": body.get("version"),
    }
    if body.get("reason"):
        params["reason"] = str(body["reason"])
    return await _publisher_call("appstore.yank_version", params)


# ── Sites proxy routes ────────────────────────────────────────────
#
# The Sites system app calls these. Each proxies a caller-scoped
# webapp.* bridge op; the bridge fixes entity_id from the mTLS
# handshake, so a host can only publish/control its OWN sites. Sites
# are served from the public webapp zone, never from this pod.

@router.get("/sites/list", dependencies=[Depends(requires_permission("read:sites"))])
async def sites_list() -> dict:
    return await _publisher_call("webapp.sites", {})


@router.get("/sites/analytics", dependencies=[Depends(requires_permission("read:sites"))])
async def sites_analytics(request: Request) -> dict:
    params: dict = {}
    hours = request.query_params.get("hours")
    if hours:
        try:
            params["hours"] = int(hours)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "hours must be int")
    return await _publisher_call("webapp.analytics", params)


# The raw zip-upload publish route is GONE: a user should
# never handle a bundle, and an open zip intake on the kernel is exactly the
# "upload anything" surface the funnel exists to close. Publishing is now only
# artifact-from-source: /sites/publish-project (Workbench build output) and the
# publish_site host tool's repo lane (the entity's own gitea, its own token).
# The webapp.publish_bundle bridge op is unchanged — it is the trust-zone
# transport those lanes ride, not a user surface.

@router.post(
    "/sites/publish-project",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_publish_project(request: Request) -> dict:
    """One-click "Publish as site" (Sites Funnel batch 3): zips a Workbench
    project's built web output SERVER-SIDE and routes it through the same
    caller-scoped webapp.publish_bundle op as the upload path — the user never
    handles a zip. A NEW site goes live public (publish_site_service says
    why); its owner sets other visibility in Sites."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        pid = int(body.get("project_id"))
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "project_id must be an integer") from None
    slug = (body.get("slug") or "").strip().lower()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "pool unavailable")
    try:
        return await publish_site_service(pool, pid, slug=slug, module=(str(body.get("module") or "").strip().lower() or None))
    except ShipError as e:
        raise HTTPException(e.status, e.detail)
    except PublisherError as e:
        raise HTTPException(e.status, e.detail)


def _sites_slug_op(op_name: str, scope: str = "publish:sites"):
    async def _handler(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
        slug = (body.get("slug") or "").strip().lower()
        if not slug:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
        return await _publisher_call(op_name, {"slug": slug})
    return _handler


router.add_api_route(
    "/sites/unpublish", _sites_slug_op("webapp.unpublish"),
    methods=["POST"], dependencies=[Depends(requires_permission("publish:sites"))])
router.add_api_route(
    "/sites/suspend", _sites_slug_op("webapp.suspend"),
    methods=["POST"], dependencies=[Depends(requires_permission("publish:sites"))])
router.add_api_route(
    "/sites/resume", _sites_slug_op("webapp.resume"),
    methods=["POST"], dependencies=[Depends(requires_permission("publish:sites"))])
router.add_api_route(
    "/sites/restart", _sites_slug_op("webapp.restart"),
    methods=["POST"], dependencies=[Depends(requires_permission("publish:sites"))])


@router.post(
    "/sites/exposure",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_exposure(request: Request) -> dict:
    """Set a site's public-exposure tier. Pure pass-through to the bridge —
    NO identity logic and NO DB lookup here. The creator-email seed for a
    private site (tier=authenticated with no allow_emails) and the
    fail-closed validation both live in the bridge's webapp.set_exposure op,
    which resolves the caller from the mTLS-fixed entity_id (the trust anchor
    the kernel cannot supply). The kernel only shape-checks the body and
    forwards it unchanged.

    Tier MEMBERSHIP is validated at the controller (EXPOSURE_TIERS,
    service.py) — the kernel validates only that a non-empty `tier` is
    present, so a new tier (e.g. Phase-3 'paywall') is not a synchronized
    multi-repo edit."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip().lower()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    policy = body.get("policy")
    if not isinstance(policy, dict) or not (
        isinstance(policy.get("tier"), str) and policy.get("tier").strip()
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "policy must be an object with a non-empty 'tier'")
    return await _publisher_call("webapp.set_exposure", {"slug": slug, "policy": policy})


# ── Customer pools + BYO-Stripe (the Access/Payments tabs) ───────────────
# Same trust model as /sites/exposure: pure pass-throughs. The bridge fixes
# the owner from its mTLS cert (webapp.customers.* / webapp.stripe.* ops) and
# the controller enforces ownership + policy; the kernel only shape-checks.


@router.get(
    "/sites/customers",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_customers_list() -> dict:
    return await _publisher_call("webapp.customers.list", {})


@router.post(
    "/sites/customers/add",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_customers_add(request: Request) -> dict:
    """Add/reactivate one customer; status='suspended' doubles as suspend."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    email = (body.get("email") or "").strip()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email required")
    params = {"email": email}
    if body.get("status"):
        params["status"] = str(body["status"])
    return await _publisher_call("webapp.customers.add", params)


@router.post(
    "/sites/customers/remove",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_customers_remove(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    email = (body.get("email") or "").strip()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email required")
    return await _publisher_call("webapp.customers.remove", {"email": email})


@router.post(
    "/sites/customers/import",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_customers_import(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    emails = body.get("emails")
    if not isinstance(emails, list) or not emails:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "emails must be a non-empty list")
    return await _publisher_call("webapp.customers.import", {"emails": emails})


@router.get(
    "/sites/stripe",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_stripe_status() -> dict:
    return await _publisher_call("webapp.stripe.status", {})


@router.post(
    "/sites/stripe/connect",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_stripe_connect(request: Request) -> dict:
    """Store the owner's webhook signing secret (sealed at the controller,
    stripped from audit rows at the bridge, never echoed back)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    ws = (body.get("webhook_secret") or "").strip()
    if not ws.startswith("whsec_"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "webhook_secret must be a whsec_… signing secret")
    return await _publisher_call("webapp.stripe.connect", {
        "webhook_secret": ws,
        "publishable_key": str(body.get("publishable_key") or "")})


@router.post(
    "/sites/stripe/onboard",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_stripe_onboard() -> dict:
    """Start Stripe Connect onboarding (the recommended lane): returns the
    Stripe-hosted URL the browser opens in a new tab. Only the resulting
    acct_… id is ever stored platform-side — no key or secret passes through
    this UI. 409s until the platform Connect keys are configured."""
    return await _publisher_call("webapp.stripe.onboard", {})


# ── Managed Postgres (SDK station 4 — the Database tab) ─────────────
# Pure pass-throughs (same trust as customers/stripe): the bridge fixes the
# owner from its mTLS cert (webapp.db.* ops), the controller records the request
# + seals/serves the URL, and the zone agent does the real provisioning. The
# kernel only shape-checks.


@router.get(
    "/sites/db",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_status() -> dict:
    return await _publisher_call("webapp.db.status", {})


@router.post(
    "/sites/db/attach",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_attach(request: Request) -> dict:
    """Request a managed Postgres DB for a site (by slug)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    return await _publisher_call("webapp.db.attach", {"slug": slug})


@router.post(
    "/sites/db/detach",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_detach(request: Request) -> dict:
    """Tear down a site's managed DB (destructive — the UI confirms first)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    return await _publisher_call("webapp.db.detach", {"slug": slug})


@router.post(
    "/sites/db/reveal",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_reveal(request: Request) -> dict:
    """Reveal the site's sealed DATABASE_URL (deliberate + audited)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    return await _publisher_call("webapp.db.reveal", {"slug": slug})


@router.post(
    "/sites/db/save-to-keys",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_save_to_keys(request: Request) -> dict:
    """Deposit the site's DATABASE_URL into the Keys vault, server-side — the
    plaintext never reaches the browser. Reveal-then-store happens entirely in
    this process; the response carries only the vault name and a masked URL.
    Raw reveal from then on is the Keys app's TOTP-gated path. Re-saving
    rotates (the vault supersedes by logical name)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "ENTITY_ID not set; vault unavailable")
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain_not_initialized")
    data = await _publisher_call("webapp.db.reveal", {"slug": slug})
    conn_url = data.get("conn_url") if isinstance(data, dict) else None
    if not conn_url:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "database not ready — no connection URL to save")
    name = f"site-{slug}-database-url"
    await vault_api.store_secret(
        pool, entity_id=int(eid), name=name, cred_type="database_url",
        plaintext=conn_url.encode(), actor="app:sites",
    )
    # Fixed-shape response by construction: never the URL, never the password.
    return {"ok": True, "name": name, "masked_url": mask_db_url(conn_url)}


@router.post(
    "/sites/db/backup",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_backup(request: Request) -> dict:
    """Queue a backup of the site's managed DB."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    return await _publisher_call("webapp.db.backup", {"slug": slug})


@router.get(
    "/sites/db/backups",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_backups(slug: str) -> dict:
    """The site's backups + restore history."""
    if not slug.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    return await _publisher_call("webapp.db.backups", {"slug": slug.strip()})


@router.post(
    "/sites/db/restore",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_db_restore(request: Request) -> dict:
    """Restore a chosen backup into the site's DB (destructive; UI confirms)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    backup_id = body.get("backup_id")
    if not slug or not isinstance(backup_id, int):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "slug and integer backup_id required")
    return await _publisher_call(
        "webapp.db.restore", {"slug": slug, "backup_id": backup_id})


# ── Where a new site lands. Was the owner's {handle}.{zone} namespace
#    (migration 039); flat {slug}.{zone} again now
#    (the middle label dropped), so the controller answers with the bare zone and
#    there is no handle left to claim. The READ stays — the Sites app needs
#    the suffix to preview an address and to enable its Deploy button — and
#    the CLAIM is gone with the dialog that called it. ─────────────────

@router.get("/sites/handle", dependencies=[Depends(requires_permission("read:sites"))])
async def sites_handle() -> dict:
    """Where this owner's new sites land: {"handle", "zone", "suffix"}.

    `suffix` is the bare zone now. The name of this route is historical.
    """
    return await _publisher_call("webapp.handle.get", {})


# ── App data follows the person ──────────────────────────────────────
#
# Somebody used a free public webapp, liked it, and now runs their own Host with
# the same app installed. These two routes are how they get the rows they made
# there. The whole surface is two questions: "is there anything of mine on a
# site running this app?" and "bring it over".
#
# THE SUBJECT IS NEVER SENT FROM HERE. The controller resolves it on both sides
# from the platform's own records and refuses a move where the requesting Host's
# owner is not that site member. A Host that could name a subject could ask
# about a stranger's membership of every site on the zone, which is a disclosure
# even when the move itself would still be refused. So the kernel sends a site,
# an app and an address; the identity half is decided somewhere the kernel
# cannot reach.
#
# execute:install, NOT publish:sites, and the choice is load-bearing rather than
# arbitrary. This is the install flow — the App Store app is the surface that
# calls it and it already declares execute:install (bit 16 in
# ENTITY_SCOPE_TO_BITS). publish:sites is the SITE OWNER's scope: it gates
# publishing, suspending and the managed-DB stack. The person bringing their
# data across is a site's USER, usually not its owner, and gating their own rows
# behind the owner's scope would mean the app that needs this could not call it
# while an app with no business here could.
#
# UNDO IS FREE AND THE UI SAYS SO. The export never deletes: the rows stay on
# the site exactly as they were, so "bring my data over" is a copy and changing
# your mind costs nothing.

@router.get(
    "/sites/data/offers",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def sites_data_offers(request: Request) -> dict:
    """Sites running `app_id` where this Host's owner already has rows.

    Returns `{"offers": [...]}`, and an EMPTY list is the normal answer — most
    apps have no public site, and most people have no rows on the ones that do.
    A `reason` of owner_subject_unresolvable means the account has not signed in
    since the platform started recording the identifier this is matched on; the
    card says so rather than showing an error, because signing in once fixes it.
    """
    app_id = (request.query_params.get("app_id") or "").strip().lower()
    if not app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_id required")
    return await _publisher_call("webapp.subject_move.offers", {"app_id": app_id})


@router.post(
    "/sites/data/import",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def sites_data_import(request: Request) -> dict:
    """Ask for one site's rows, and land them once they have arrived.

    CALLED REPEATEDLY, ON PURPOSE. The export is asynchronous — the controller
    records the request, the public-zone agent picks it up on its own 15-second
    poll and ships the rows up — so the first call almost always returns
    `state: "queued"` with nothing landed yet. Calling again progresses it. That
    is why this is one route rather than two: the caller does not have to know
    which phase a move is in, and the install card can poll the same thing it
    clicked.

    Requesting a move that already exists is a no-op that returns the same
    move_id (the controller's idempotency index), and importing a payload twice
    lands one copy of each row (subject_import deduplicates on the table's
    primary key), so a caller that polls enthusiastically cannot do damage.
    """
    import base64 as _b64

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    app_id = str(body.get("app_id") or "").strip().lower()
    member_email = str(body.get("member_email") or "").strip()
    site_id = body.get("site_id")
    if not app_id or not member_email or site_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "app_id, site_id and member_email required")
    try:
        site_id = int(site_id)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "site_id must be int") from None

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "the Host database is not available")
    app = await registry.get(pool, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"{app_id} is not installed on this Host")

    # The move first. This is where identity is decided, so it happens before
    # anything is fetched or written — and a refusal here carries the
    # controller's own machine code, which the card shows verbatim rather than
    # translating into a guess about why.
    requested = await _publisher_call("webapp.subject_move.request", {
        "site_id": site_id, "app_id": app_id, "member_email": member_email,
    })
    move_id = requested.get("move_id")
    if move_id is None:
        return {"state": "refused", "detail": "no move was created"}

    # Has the export landed yet? A move that is still pending has no payload and
    # that is the expected first answer, not a failure.
    try:
        got = await publisher_op_service("webapp.subject_move.payload",
                                         {"move_id": move_id})
    except PublisherError as e:
        if e.status == status.HTTP_404_NOT_FOUND:
            return {"state": "queued", "move_id": move_id,
                    "detail": "the site's rows are being exported — this can "
                              "take a few seconds"}
        raise HTTPException(e.status, e.detail)

    payload_b64 = got.get("payload_b64") or ""
    if not payload_b64:
        return {"state": "queued", "move_id": move_id,
                "detail": "the export has not arrived yet"}

    from server.applications import manifest as _manifest_mod
    from server.applications import subject_import as _subject_import

    manifest = _manifest_mod.load(Path(app.source_dir) / "app.yaml")
    try:
        raw = _b64.b64decode(payload_b64)
    except Exception:
        await _report_import(move_id, False, error="payload was not base64")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "the exported payload was not readable") from None

    # Verify the digest the controller recorded at upload, against the bytes
    # that actually arrived here. The controller checks it on the way out too,
    # so this is the third check and the only one on THIS side of the bridge —
    # which is the point: it is the last moment a payload can be refused before
    # it becomes rows in somebody's database, and the only check whose failure
    # this Host can act on rather than merely report. An absent digest is not
    # treated as a pass; it is only skipped when the lane did not supply one,
    # which an older bridge will not.
    expect = (got.get("sha256") or "").strip().lower()
    if expect and _hashlib.sha256(raw).hexdigest() != expect:
        await _report_import(
            move_id, False,
            error="payload digest did not match what the controller recorded")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "the exported payload did not match its recorded digest — nothing "
            "was imported")

    try:
        payload = json.loads(raw)
    except Exception:
        # Report the failure UP as well as answering the caller. A payload this
        # Host cannot read is a fact the move record should carry, or the
        # controller goes on believing it delivered.
        await _report_import(move_id, False, error="payload was not readable JSON")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "the exported payload was not readable") from None
    try:
        result = await _subject_import.import_payload(
            pool, payload, manifest, expect_app_id=app_id)
    except _subject_import.ImportRefused as e:
        await _report_import(move_id, False, error=str(e))
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None

    await _report_import(move_id, True, tables=result["tables"],
                         rows_imported=result["rows_imported"])
    log.info("subject import landed: move=%s app=%s tables=%d rows=%d skipped=%d",
             move_id, app_id, len(result["tables"]), result["rows_imported"],
             result["rows_skipped"])
    return {"state": "done", "move_id": move_id, **result}


async def _report_import(move_id, ok: bool, *, tables=None,
                         rows_imported=None, error: str = "") -> None:
    """Tell the controller what happened. Best-effort, and deliberately so.

    The rows are already written (or already refused) by the time this runs, and
    raising here would turn a successful import into a 5xx the user reads as a
    failure. A lost report leaves the move looking un-imported, which the next
    call corrects — the import itself is idempotent, so re-running it is safe.
    """
    try:
        await publisher_op_service("webapp.subject_move.import_report", {
            "move_id": move_id, "ok": bool(ok), "tables": tables,
            "rows_imported": rows_imported, "error": error,
        })
    except Exception as e:
        log.warning("subject import report not delivered (move=%s): %s", move_id, e)


# ── Service catalog (one-click third-party stacks — the Catalog tab).
#    Browse is ungated like /sites/list (public metadata); deploy is
#    publish:sites. The bridge fixes entity_id; born private. ──────────

@router.get("/sites/catalog/templates")
async def sites_catalog_templates() -> dict:
    """Admitted catalog templates: header metadata + the ${VAR:-default}
    settings map the deploy form renders."""
    return await _publisher_call("webapp.catalog.templates", {})


@router.post(
    "/sites/catalog/deploy",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_catalog_deploy(request: Request) -> dict:
    """Deploy an admitted template as a new site (born private; the owner
    flips exposure in Visibility like any site)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip().lower()
    template_key = (body.get("template_key") or "").strip()
    if not slug or not template_key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "slug and template_key required")
    params: dict = {"slug": slug, "template_key": template_key}
    settings = body.get("settings")
    if settings is not None:
        if not isinstance(settings, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "settings must be an object")
        params["settings"] = settings
    return await _publisher_call("webapp.catalog.deploy", params)


# ── Catalog app data volumes (the Data tab) — backup/restore of a catalog
#    site's PVCs; the volume sibling of the db lanes above. ────────────

@router.get(
    "/sites/volumes",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_volumes(slug: str) -> dict:
    """A catalog site's data volumes + backup/restore history."""
    if not slug.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    return await _publisher_call("webapp.volume.backups", {"slug": slug.strip()})


@router.post(
    "/sites/volumes/backup",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_volumes_backup(request: Request) -> dict:
    """Queue volume backup(s); omitting volume backs up every data volume."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    params: dict = {"slug": slug}
    volume = (body.get("volume") or "").strip()
    if volume:
        params["volume"] = volume
    return await _publisher_call("webapp.volume.backup", params)


@router.post(
    "/sites/volumes/restore",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_volumes_restore(request: Request) -> dict:
    """Restore a chosen volume backup (destructive of the volume's current
    contents; the UI confirms first)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip()
    backup_id = body.get("backup_id")
    if not slug or not isinstance(backup_id, int):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "slug and integer backup_id required")
    return await _publisher_call(
        "webapp.volume.restore", {"slug": slug, "backup_id": backup_id})


# ── Custom domains (connect mode) ─────────────────────────────────

@router.get("/sites/domains", dependencies=[Depends(requires_permission("read:sites"))])
async def sites_domains_list() -> dict:
    return await _publisher_call("webapp.domain_list", {})


@router.post(
    "/sites/domains/add",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_domain_add(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    slug = (body.get("slug") or "").strip().lower()
    domain = (body.get("domain") or "").strip().lower()
    method = (body.get("method") or "connect").strip().lower()
    if not slug or not domain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug and domain required")
    if method not in ("connect", "delegate"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "method must be 'connect' or 'delegate'")
    return await _publisher_call(
        "webapp.domain_add", {"slug": slug, "domain": domain, "method": method})


def _sites_domain_op(op_name: str):
    async def _handler(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
        domain = (body.get("domain") or "").strip().lower()
        if not domain:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "domain required")
        return await _publisher_call(op_name, {"domain": domain})
    return _handler


router.add_api_route(
    "/sites/domains/verify", _sites_domain_op("webapp.domain_verify"),
    methods=["POST"], dependencies=[Depends(requires_permission("publish:sites"))])
router.add_api_route(
    "/sites/domains/remove", _sites_domain_op("webapp.domain_remove"),
    methods=["POST"], dependencies=[Depends(requires_permission("publish:sites"))])


# ── DNS records on a delegated (Lane A) domain ───────────────────────

@router.post(
    "/sites/domains/records",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_domain_records(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    domain = (body.get("domain") or "").strip().lower()
    if not domain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "domain required")
    return await _publisher_call("webapp.dns_records_list", {"domain": domain})


@router.post(
    "/sites/domains/records/set",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_domain_record_set(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    domain = (body.get("domain") or "").strip().lower()
    name = (body.get("name") or "").strip()
    rtype = (body.get("type") or "").strip().upper()
    values = body.get("values") or []
    if not domain or not name or not rtype:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "domain, name and type required")
    if not isinstance(values, list) or not [v for v in values if str(v).strip()]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one value required")
    return await _publisher_call("webapp.dns_record_set", {
        "domain": domain, "name": name, "type": rtype,
        "values": values, "ttl": int(body.get("ttl") or 3600),
    })


@router.post(
    "/sites/domains/records/delete",
    dependencies=[Depends(requires_permission("publish:sites"))],
)
async def sites_domain_record_delete(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    domain = (body.get("domain") or "").strip().lower()
    name = (body.get("name") or "").strip()
    rtype = (body.get("type") or "").strip().upper()
    if not domain or not name or not rtype:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "domain, name and type required")
    return await _publisher_call("webapp.dns_record_delete", {
        "domain": domain, "name": name, "type": rtype,
    })


@router.delete(
    "/{app_id}",
    dependencies=[Depends(requires_permission("execute:install"))],
)
async def uninstall(app_id: str, purge: bool = False) -> dict:
    """Uninstall a non-native app.

    Default: removes bundle dir + registry row. Config rows and the
    workspace dir survive so a reinstall reattaches them.

    With ?purge=true: also deletes every survivable artifact (config
    rows + workspace dir). No undo.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    # Stop any running sidecar for this app before tearing the bundle
    # out from under it.
    sup = supervisor.get_supervisor()
    if sup is not None:
        running = sup.get(app_id)
        if running is not None:
            await sup._terminate(running)
            async with sup._lock:
                sup._by_app.pop(app_id, None)
                sup._refs.pop(app_id, None)

    try:
        return await installer.uninstall_app(pool, app_id, purge=purge)
    except installer.UninstallError as e:
        msg = str(e)
        code = (
            status.HTTP_404_NOT_FOUND if "not installed" in msg
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(code, msg) from None


# ── Per-app config storage ────────────────────────────────────────
#
# Each app owns a JSON key/value namespace. Reads + writes scope to
# the calling app's token: app_id in the URL must equal token.app_id,
# so apps can never see each other's config.

def _require_self(request: Request, app_id: str) -> None:
    tok = getattr(request.state, "app_token", None)
    if tok is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "application token required"
        )
    if tok.app_id != app_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"token for {tok.app_id!r} cannot reach config of {app_id!r}",
        )


@router.get("/{app_id}/config")
async def list_app_config(app_id: str, request: Request) -> dict:
    _require_self(request, app_id)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    return {"app_id": app_id, "config": await storage.list_config(pool, app_id=app_id)}


@router.get("/{app_id}/config/{key}")
async def get_app_config(app_id: str, key: str, request: Request) -> dict:
    _require_self(request, app_id)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    value = await storage.get_config(pool, app_id=app_id, key=key)
    if value is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such key: {key}")
    return {"app_id": app_id, "key": key, "value": value}


# An app config value is read under this bound BEFORE it is held, never whole
# (server/harness_input.read_request_json: refused as it streams past the cap,
# parsed off the loop when large, and refused past its JSON_MAX_NODES values,
# the bound on the objects one parse may inflate into). 16 MiB is the bound the
# kernel already holds one JSON request body to (proxy.py _VERIFY_BODY_MAX,
# browser_agents.py _BROWSER_JSON_MAX). It replaces a 64 KiB check that ran
# AFTER request.json() had read the whole body.
CONFIG_BODY_MAX = 16 * 1024 * 1024


@router.put("/{app_id}/config/{key}")
async def put_app_config(app_id: str, key: str, request: Request) -> dict:
    _require_self(request, app_id)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    from server import harness_input as _hin
    try:
        raw = await _hin.read_request(request, CONFIG_BODY_MAX, "app config value")
        if not raw.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON")
        value = await _hin.parse_json(raw, "app config value")
    except _hin.HarnessInputRefused as e:
        if "invalid JSON" in str(e):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"{e} — keep data this size in the app's files") from None
    try:
        await storage.put_config(pool, app_id=app_id, key=key, value=value)
    except storage.StorageError as e:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(e)) from None
    return {"app_id": app_id, "key": key, "ok": True}


@router.delete("/{app_id}/config/{key}")
async def delete_app_config(app_id: str, key: str, request: Request) -> dict:
    _require_self(request, app_id)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    n = await storage.delete_config(pool, app_id=app_id, key=key)
    return {"app_id": app_id, "key": key, "deleted": n}


# ── On-demand store-upgrade sweep ────────────────────────────────────
#
# default_install._sync_store_upgrades is the function the kernel
# calls at boot to refresh locally-installed non-native apps when the
# central store has newer versions. Pre-this-route the only way to
# trigger it was a pod restart — which meant every Publisher-published
# frontend ship required ~30s of chat downtime on every entity that
# needed the update.
#
# This route exposes the same function on demand. Operator-side
# pushes (e.g. `curl ... /api/applications/refresh-store` after
# republishing a platform tool) now refresh in-place without rolling the
# pod. Uses the live gunicorn worker's bridge singleton, which is
# the same one the boot-time call uses — no parallel state, no
# divergence with the boot path.
#
# Gated on write:update_request — same scope the image-apply +
# bundle-fetch routes use; it's the canonical "trigger a system-state
# update" capability. The Settings app declares it.

@router.post(
    "/refresh-store",
    dependencies=[Depends(requires_permission("write:update_request"))],
)
async def refresh_store(request: Request) -> dict:
    """Trigger an on-demand store-upgrade sweep.

    Walks every locally-installed non-native app, asks the central
    store for its latest version, and reinstalls in place if newer.
    Returns the same envelope shape `_sync_store_upgrades` returns
    on boot: {status, upgraded: [{app_id, from, to}, ...], failed: [...]}.

    No request body. Idempotent — a no-op when every local app is
    already at the store's latest version.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"bridge module unavailable: {e}",
        ) from None
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "bridge not connected — cannot reach store",
        )
    from server.applications.default_install import _sync_store_upgrades
    return await _sync_store_upgrades(bc, pool)


# ───────────────────────────────────────────────────────────────────────────────────────
# Service layer: the publisher-proxy + factory-ship LOGIC lives here
# as module-level helpers so the publish_app / ship_project_app host CHAT tools
# share the EXACT same path (gate + bridge dispatch + pack/sign/install) WITHOUT
# faking an HTTP Request. Each route below is a thin adapter that parses the body,
# calls the service, and maps the service's small Error class to an HTTPException.
# Mirrors the nac_mgmt.run_mgmt_op pattern. Response shapes UNCHANGED.
# NOTE: place these helpers ABOVE the routes; insert just after PROJECTS_ROOT is
# defined (it is referenced by ship_project_service) and after _StoreError, or
# simply before the `build_from_project` route. They reference module-level
# `_serialize`, `bundles`, `entity_signing`, `installer`, `load_manifest`, `log`,
# `PROJECTS_ROOT`, all already defined in this file.
# ───────────────────────────────────────────────────────────────────────────────────────

class PublisherError(Exception):
    """A publisher/store proxy failure the caller surfaces. `status` is the HTTP
    code the route maps it to; the publish_app host tool just reports `detail`."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ShipError(Exception):
    """A factory-ship (pack+sign+install) failure. `status` is the HTTP code the
    /build route maps it to; the ship_project_app host tool reports `detail`."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def publisher_op_service(op: str, params: dict) -> dict:
    """Proxy ONE caller-scoped publisher/store bridge op — the SINGLE source of truth
    for the publisher_* proxy routes AND the publish_app host tool. The bridge fixes
    entity_id from the mTLS handshake, so a publisher can only ever read its OWN books
    or set its OWN prices regardless of what the kernel sends. Returns the op's data
    dict; raises PublisherError(status, detail) on any failure (bridge unavailable /
    not connected / unreachable / op error). Reads NOTHING from a Request."""
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise PublisherError(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise PublisherError(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")
    try:
        envelope = await bc.request_full(op, params)
    except Exception as e:
        raise PublisherError(status.HTTP_502_BAD_GATEWAY, f"bridge unreachable: {e}")
    if not envelope.get("ok"):
        code = int(envelope.get("code") or 502)
        raise PublisherError(code, envelope.get("error") or "publisher op failed")
    return envelope.get("data") or {}


async def _module_dir_for(pool, project_id: int, module: str | None,
                          allowed_targets: tuple, action: str):
    """The directory a ship/publish acts on. No module
    = the project root (the single-app layout, unchanged). A module must exist
    and its TARGET must be one this action serves — a public site is not
    installed as an app, a private app is not submitted to the store."""
    if not module:
        return PROJECTS_ROOT / str(project_id)
    from server.routes.project_modules import ModuleError, get_module, module_dir, target_words
    mod = await get_module(pool, project_id, module)
    if mod is None:
        raise ShipError(status.HTTP_404_NOT_FOUND,
                        f"project {project_id} has no module {module!r}")
    if mod["target"] not in allowed_targets:
        raise ShipError(
            status.HTTP_400_BAD_REQUEST,
            f"module {module!r} is {target_words(mod['target'])} — it cannot be {action}")
    try:
        return module_dir(project_id, module)
    except ModuleError as e:
        raise ShipError(status.HTTP_400_BAD_REQUEST, str(e)) from None


async def _pack_and_sign_project(pool, project_id: int, module: str | None = None,
                                 _allowed: tuple = ("private_app", "store_app"),
                                 _action: str = "installed as an app") -> dict:
    """Validate a completed Workbench project + pack + SDK-bind + entity-sign it into a
    .cseries bundle, WITHOUT installing or publishing. The SINGLE source of the
    validate→pack→sign steps shared by ship_project_service (install) AND
    publish_project_service (submit to store), so the security invariants live in ONE
    place. Returns {signed, manifest, entity_id}. Raises ShipError(status, detail)."""
    import yaml as _yaml
    from server.applications.build_contract import validate_build
    from server.applications import sdk_store

    async with pool.acquire() as conn:
        proj = await conn.fetchrow(
            "SELECT id, entity_id, name, status FROM projects WHERE id = $1",
            project_id,
        )
    if proj is None:
        raise ShipError(status.HTTP_404_NOT_FOUND, f"project {project_id} not found")
    # SHIPPABILITY IS ABOUT THE TREE, NOT THE PLAN STATE. This once required
    # status == 'complete', which read as "something was built" — but the two
    # checks below prove that substantively: an app.yaml must exist and
    # validate_build must pass the App SDK contract over the actual directory.
    # The status gate added nothing on top of that, and it cost something
    # real: re-planning flips a completed project back to 'active' (tools.py,
    # action='plan') precisely WHERE status='complete', so the moment the user
    # opened any maintenance work on a live app it became un-republishable
    # until every new phase finished. Combined with phases that could not be
    # retired (fixed in v110), a maintenance plan could strand a shipped app
    # indefinitely.
    #
    # The hazard the gate was reaching for is real but narrower: do not pack a
    # worktree while a harness is WRITING to it, or the bundle is torn. That is
    # an in-flight check, not a plan-state check — the same signal the build
    # door already uses for its per-project in-flight guard.
    _inflight = await pool.fetchval(
        "SELECT task_id FROM entity_tasks "
        "WHERE entity_id = $1 AND task_type = 'factory_build' "
        "AND status = 'in_progress' AND (result->>'project_id')::int = $2 "
        "ORDER BY task_id DESC LIMIT 1",
        proj["entity_id"], project_id)
    if _inflight is not None:
        raise ShipError(
            status.HTTP_409_CONFLICT,
            f"a build is still running on this project (task {_inflight}) — "
            "packaging now would capture a half-written tree. It reports into "
            "the thread when it lands; ship then.",
        )

    # DISCLOSE a mid-plan ship; never block it. Shipping is allowed to depend on the TREE
    # alone, so a project with phases still open CAN ship, including its first
    # ship: the manifest and build-contract checks below decide whether it is a
    # working app, and refusing a working app because a plan row says otherwise
    # is the platform second-guessing the user. It is also the very error this
    # rule exists to stop — reading plan state as a proxy for something it does not
    # mean. What the user must not do is find out AFTERWARDS, so the count
    # rides the result for the caller to surface.
    # Reuse the ONE definition of "still open" rather than spelling it again —
    # a second literal copy in a second file is precisely the drift
    # _PHASE_OPEN_SQL was created to end, and writing one here while removing
    # seven elsewhere would be the same mistake in a new place. Imported
    # lazily: this module is imported by the brain runtime that owns the
    # constant, so a module-level import would cycle.
    from mastercode.runtime.tools import _PHASE_OPEN_SQL
    _open_phases = await pool.fetchval(
        "SELECT COUNT(*) FROM project_phases WHERE project_id = $1 AND "
        + _PHASE_OPEN_SQL, project_id) or 0

    project_dir = await _module_dir_for(pool, project_id, module, _allowed, _action)
    manifest_path = project_dir / "app.yaml"
    if not manifest_path.exists():
        raise ShipError(
            status.HTTP_400_BAD_REQUEST,
            f"project {project_id}" + (f" module {module!r}" if module else "")
            + " has no app.yaml — declare a manifest first",
        )
    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        raise ShipError(status.HTTP_400_BAD_REQUEST, f"manifest invalid: {e}") from None
    if manifest.provenance != "custom":
        raise ShipError(
            status.HTTP_400_BAD_REQUEST,
            f"factory-built apps must declare provenance=custom (got {manifest.provenance!r})",
        )

    try:
        from server.main import app as _fastapi_app
    except Exception:
        _fastapi_app = None  # gate degrades to manifest+source checks if app unavailable
    # The ROOT app of a project that also has modules: the module folders are
    # other apps (or sites, or components) — never part of the root's bundle
    # or its contract scan.
    _exclude_top = frozenset()
    if module is None:
        _exclude_top = frozenset(r["name"] for r in await pool.fetch(
            "SELECT name FROM project_modules WHERE project_id=$1", project_id))
    _build_errors = validate_build(project_dir, manifest, app=_fastapi_app,
                                   exclude_top=_exclude_top)
    if _build_errors:
        raise ShipError(
            status.HTTP_400_BAD_REQUEST,
            "app build rejected: " + "; ".join(_build_errors),
        )

    # Sign-time SDK gate: refuse to sign unless the host's installed
    # SDK — the TCB that validated this build — is provably Privatae-signed + intact.
    try:
        _sdk = sdk_store.verify_current()
    except sdk_store.SdkStoreError as e:
        raise ShipError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"app signing refused — App SDK integrity check failed: {e}",
        ) from None

    try:
        unsigned = bundles.pack(project_dir, exclude_top=_exclude_top)
        # Bind the app to the SDK that validated it — signature-covered provenance.
        _c = bundles._read_zip(unsigned)
        _m = _yaml.safe_load(_c.manifest_yaml) or {}
        _m["validated_under_sdk"] = {"version": _sdk["version"], "hash": _sdk["hash"]}
        # The platform's build record:
        # git facts of the project dir (commit, tree, dirty, who authored each
        # commit — the HOST's Gitea identity vs the user's), every payload
        # file's sha256, the lockfile's sha. Written INTO the payload as
        # attestation.json so the signature covers it, and the platform-only
        # values are stamped into the manifest (source.commit/tree,
        # build.lockfile_sha256 — the keys validate_build refuses in a source
        # tree). The store bar (validate_publish → attestation.verify_bundle)
        # requires the record and a clean, committed tree; the runtime pod
        # verifies the fetched files against it before running them.
        from server.applications import attestation as _attest
        _host_email = _host_name = None
        try:
            from server.routes.gitea_client import gitea_creds as _gitea_creds
            _cred = _gitea_creds()
            if _cred:
                _host_name, _host_email = _cred[0], f"{_cred[0]}@{_cred[2]}"
        except Exception as _e:  # the record still says who committed, by email
            log.warning("attestation: gitea identity unavailable: %s", _e)
        # Subprocess git off the event loop, as project_git does: a
        # status over a tree with an untracked node_modules is seconds,
        # and this coroutine serves a request.
        import asyncio as _asyncio
        _git = await _asyncio.to_thread(
            _attest.collect_git, project_dir,
            host_email=_host_email, host_name=_host_name)
        _payload = {k: v for k, v in _c.payload.items() if k != _attest.FILENAME}
        _record = _attest.build(
            _m, _payload, git=_git,
            sdk={"version": _sdk["version"], "hash": _sdk["hash"]},
            built_by=f"entity-{int(proj['entity_id'])} kernel",
            binaries=_c.binaries)
        _m = _attest.stamp(_m, _record)
        _payload[_attest.FILENAME] = json.dumps(
            _record, sort_keys=True, indent=1).encode("utf-8")
        unsigned = bundles._build_zip(
            _yaml.safe_dump(_m, sort_keys=True).encode("utf-8"),
            _payload, _c.binaries, signature=None,
        )
        signed = await entity_signing.sign_with_entity_key(
            pool, int(proj["entity_id"]), unsigned,
        )
    except ShipError:
        raise
    except Exception as e:
        log.exception("pack+sign failed for project %d", project_id)
        raise ShipError(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"pack+sign failed: {e}",
        ) from None

    return {"open_phases": int(_open_phases),
            "signed": signed, "manifest": manifest, "entity_id": int(proj["entity_id"])}


async def publish_project_service(
    pool, project_id: int, *, price_micros: int = 0, audience: str = "public",
    summary: str | None = None, version: str | None = None,
    agreement_version: int | None = None, fork_policy: str = "open",
    group_ref: str | None = None,
    module: str | None = None,
) -> dict:
    """Pack+sign a project (the SAME validate→pack→sign as ship) and SUBMIT it
    to the App Store via the caller-scoped `appstore.publish_bundle` bridge op — the same
    op the Publisher app's upload uses. The host publishes the artifact it BUILT; it never
    hand-re-uploads bytes. The bridge fixes the publisher from its mTLS cert, so a host can
    only publish under its OWN identity.

    Store lineage: the operator HOLDS every publish for human
    review, so success is a pending envelope (request_id + review status),
    not a published row. `agreement_version` is the publishing-agreement
    version the USER explicitly accepted (fetch via appstore.agreement) —
    the operator refuses without it. If the project row carries
    `forked_from` (captured at fork time), it is submitted as the flavor's
    parent edge; it is never client-invented here. Raises ShipError /
    PublisherError on failure."""
    import base64 as _b64
    import json as _json
    from server.applications.build_contract import validate_publish

    r = await _pack_and_sign_project(pool, project_id, module, ("store_app",),
                                     "submitted to the App Store")
    signed = r["signed"]
    # Publishing to the store carried no mid-plan disclosure while installing
    # did — the same ship, one lane silent.
    _open = int(r.get("open_phases") or 0)
    # Untrusted-app admission bar — same gate the upload route applies.
    _errs = validate_publish(signed)
    if _errs:
        raise ShipError(
            status.HTTP_400_BAD_REQUEST,
            "publish rejected (untrusted-app bar): " + "; ".join(_errs),
        )
    # Fork provenance rides from the PROJECT row (recorded at fork time,
    # migration v116) — the publish lane relays it, never invents it.
    _ff_row = await pool.fetchrow(
        "SELECT forked_from, repo FROM projects WHERE id = $1", project_id)
    _ff = _ff_row["forked_from"] if _ff_row else None
    if isinstance(_ff, str):
        try:
            _ff = _json.loads(_ff)
        except ValueError:
            _ff = None
    _ff_app = str(_ff["app_id"]) if isinstance(_ff, dict) and _ff.get("app_id") else None
    # The licence gate: a submission that
    # descends from an installed app someone else authored, whose licence
    # withholds redistribution, is refused unless it is that app's declared
    # flavour. Rights come from the installed row; bytes from INSTALL_ROOT.
    from server.applications import licensing as _licensing
    _c = bundles._read_zip(signed)
    _lic_errs = await _licensing.publish_gate(
        pool, payload=_c.payload, binaries=_c.binaries, forked_from_app_id=_ff_app)
    if _lic_errs:
        raise ShipError(status.HTTP_403_FORBIDDEN,
                        "publish refused (licence): " + " ".join(_lic_errs))
    # A project whose repo MIRRORS an external repository (github_mirror.py:
    # gitea `mirror: true` with `original_url`) came from somewhere, and the
    # recipe must say so: the manifest's source.upstream {url, license} is
    # required, and its url must be the mirror's origin. Read from Gitea, the
    # authority on what the repo is; unreachable Gitea refuses rather than
    # guesses (the store shows the upstream and its licence to every buyer).
    _platform_repo = (_ff_row["repo"] if _ff_row else None) or ""
    from server.routes import project_forge as _pf
    try:
        _forge = await _pf.project_forge(pool, project_id)
    except _pf.ForgeError as _e:
        raise ShipError(status.HTTP_503_SERVICE_UNAVAILABLE,
                        f"cannot tell where the project's code lives ({_e.detail}) — publish "
                        f"refused rather than guessed about where its code came from") from None
    # The project's forge: the platform, or the code server on the user's
    # machine for a project moved there (project_forge) — its repo there must
    # exist. A moved project's platform repo is where its code came FROM (its
    # main seeded the machine's), so a mirror there still names the upstream.
    _repo_full = _forge.repo or _platform_repo
    if _repo_full:
        try:
            from server.routes.gitea_client import gitea_request, split_repo
            async with _pf.open_forge(pool, int(r["entity_id"]), _forge) as _sess:
                _o, _n = split_repo(_repo_full)
                _info = await _sess.request("GET", f"/api/v1/repos/{_o}/{_n}") or {}
            if _forge.on_machine and _platform_repo and not _info.get("mirror"):
                try:
                    _po, _pn = split_repo(_platform_repo)
                    _pinfo = await gitea_request("GET", f"/api/v1/repos/{_po}/{_pn}") or {}
                    if _pinfo.get("mirror") and _pinfo.get("original_url"):
                        _info = {**_info, "mirror": True, "original_url": _pinfo["original_url"]}
                except HTTPException as _pe:
                    if _pe.status_code != 404:   # gone is history; unreadable is not
                        raise
        except _pf.ForgeError as _e:
            raise ShipError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"cannot read the project's repo {_repo_full} ({_e.detail}) — publish "
                f"refused rather than guessed about where its code came from") from None
        except HTTPException as _e:
            if _e.status_code == 404:
                raise ShipError(
                    status.HTTP_409_CONFLICT,
                    f"the project's repo {_repo_full} no longer exists on Gitea — the "
                    f"recipe names a repo and a commit, so publish from a project whose "
                    f"repo is there") from None
            raise ShipError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"cannot read the project's repo {_repo_full} on Gitea ({_e.detail}) — "
                f"publish refused rather than guessed about where its code came from"
            ) from None
        except Exception as _e:
            raise ShipError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"cannot read the project's repo {_repo_full} on Gitea ({_e}) — publish "
                f"refused rather than guessed about where its code came from") from None
        if _info.get("mirror") and _info.get("original_url"):
            _declared = getattr(getattr(r["manifest"], "source", None), "upstream", None)
            _decl_url = (getattr(_declared, "url", None) or "").rstrip("/").removesuffix(".git")
            _orig = str(_info["original_url"]).rstrip("/").removesuffix(".git")
            if not _decl_url:
                raise ShipError(
                    status.HTTP_400_BAD_REQUEST,
                    f"this project's repo mirrors {_orig} — declare where the code came "
                    f"from in app.yaml: source.upstream {{url: {_orig}, license: <SPDX>}}. "
                    f"The store shows the upstream and its licence to every installer.")
            if _decl_url.lower() != _orig.lower():
                raise ShipError(
                    status.HTTP_400_BAD_REQUEST,
                    f"app.yaml declares source.upstream.url {_decl_url} but the project's "
                    f"repo mirrors {_orig} — the declared upstream must be the real one")
    # AUDIENCE 'circle' IS CHECKED BEFORE THE UPLOAD, NOT AFTER.
    # record_publication refuses a group this host does not own, and doing it
    # here means a publisher learns that before a bundle crosses the wire and
    # before the operator's review queue has a row in it. It also writes the
    # local obligation: from this moment the reconciler owes the store a roster
    # snapshot for this app (server/applications/appstore_circle.py).
    _circle_app_id = None
    if (audience or "public") == "circle":
        from server.applications import appstore_circle
        from server.groups.roster import RosterError
        if not group_ref:
            raise ShipError(status.HTTP_400_BAD_REQUEST,
                            "audience 'circle' needs group_ref — the group whose roster "
                            "may see and fetch this app")
        _circle_app_id = _declared_app_id_in_zip(signed)
        if not _circle_app_id:
            # _declared_app_id_in_zip returns "" for a bundle it cannot read.
            # Recording that would key the publication row on the empty string
            # and give the reconciler a row it can never satisfy.
            raise ShipError(status.HTTP_400_BAD_REQUEST,
                            "this bundle declares no app id — a circle app is keyed on "
                            "one, so there is nothing to gate")
        # CHECKED here, RECORDED after the store accepts it (below). The check
        # has to come first so a publisher learns their group is wrong before a
        # bundle crosses the wire; the RECORD must not, because a publish is a
        # REVIEW REQUEST and not a listing. A row written now for a submission
        # the operator later rejects is a row reconcile_rosters retries on every
        # beat for ever, against an app the store will answer 404 for.
        try:
            await appstore_circle.check_publishable(pool, group_ref)
        except RosterError as e:
            raise ShipError(e.status, e.detail)
    params = {
        "zip_b64": _b64.b64encode(signed).decode("ascii"),
        "price_micros": int(price_micros or 0),
        "audience": audience or "public",
        "is_default": False,
    }
    if group_ref:
        # Relayed so the STORE records which group gates the app: the platform
        # refuses a later roster snapshot whose group_id does not match what was
        # recorded at publish, so a publisher cannot re-point a live circle app
        # at a different roster by posting one.
        params["group_ref"] = str(group_ref)
    if version:
        params["version"] = version
    if summary:
        params["summary"] = summary
    if agreement_version is not None:
        params["agreement_version"] = int(agreement_version)
    if fork_policy:
        params["fork_policy"] = str(fork_policy)
    if _ff_app:
        params["forked_from_app_id"] = _ff_app
        if isinstance(_ff, dict) and _ff.get("version"):
            params["forked_from_version"] = str(_ff["version"])
    _res = await publisher_op_service("appstore.publish_bundle", params)
    # The store took it. NOW the obligation to keep its replica current is real,
    # so it is written — after the act it depends on, not before it.
    if _circle_app_id and group_ref:
        from server.applications import appstore_circle as _ac
        try:
            await _ac.record_publication(pool, _circle_app_id, group_ref)
        except Exception as _rp:
            # The submission landed. Failing the publish over bookkeeping would
            # tell the publisher it did not, which is the more damaging lie —
            # and reconcile_rosters is idempotent, so a re-publish repairs it.
            log.warning("circle publication record failed for %s (non-fatal): %s",
                        _circle_app_id, _rp)
    if isinstance(_res, dict):
        # >0 means this was published MID-PLAN. Allowed on purpose, but the
        # user hears it from the publish rather than discovering it later —
        # the same disclosure ship_project_service returns.
        _res["open_phases"] = _open
    return _res


# ── Publish-from-Workbench: project → public webapp zone (Sites Funnel batch 3) ──
# A SITE bundle is a plain zip of built web output (index.html at its root), not
# a .cseries app bundle — so this lane shares publisher_op_service with the store
# path but NOT pack/sign: the platform signs site bundles at the controller,
# after annotating them for live-edit.

# Standard build-output dirs of the mainstream toolchains (vite / CRA / next
# export), tried in order; the project root itself is the hand-rolled fallback.
_SITE_WEBROOT_CANDIDATES = ("dist", "build", "out", "public")
_SITE_ZIP_SKIP_DIRS = frozenset(
    {"node_modules", ".git", "__pycache__", ".venv", "venv", ".cache",
     # An artifact's past revisions and log.jsonl: never public, static or dynamic.
     ".history",
     # review.adopt's rollback store. It holds every version of every file the
     # user replaced — i.e. exactly the drafts they decided not to ship — and
     # _site_webroot falls back to the project root, which is the static-site
     # case adopt was built for. Without this, choosing a new design publishes
     # the old one alongside it.
     ".adopt-backups"})
# Root-level bookkeeping files that are not directories. .adopt-provenance.json
# lists session ids, harness names, run ids and file paths; none of that belongs
# on a public origin.
_SITE_ZIP_SKIP_FILES = frozenset({".adopt-provenance.json"})
# What a bundle may UNPACK to: the platform's zip-bomb guard, which protects
# it and stays (the platform's MAX_UNPACKED_BYTES / MAX_FILES; the zip
# itself is bounded only by that, MAX_BUNDLE_BYTES). Mirrored here so a bundle the platform would refuse is
# refused while its author is looking. It was a flat 50 MB on both sides.
_BUNDLE_UNPACKED_MAX = 200 * 1024 * 1024
_BUNDLE_FILES_MAX = 50_000
_BUNDLE_ZIP_MAX = _BUNDLE_UNPACKED_MAX + _BUNDLE_FILES_MAX * 1024
_SITE_MAX_ZIP = _BUNDLE_UNPACKED_MAX


def _declared_app_id(*dirs) -> str:
    """The app id a tree declares about ITSELF, read tolerantly from the first
    app.yaml found in `dirs`. Returns "" when there is no manifest, it will not
    parse, or it names no id.

    Deliberately NOT the full manifest loader: a half-written app.yaml in a
    project must not turn into a 500 on an unrelated publish, and nothing here
    trusts the file's contents beyond the id. The id is used only to LOOK UP
    the installed row — the rights themselves are read from that row, which was
    written from the signed bundle at install time. So editing a copied
    manifest cannot grant the copier anything.
    """
    import yaml as _yaml
    for d in dirs:
        if d is None:
            continue
        p = d / "app.yaml"
        try:
            if not p.exists():
                continue
            raw = _yaml.safe_load(p.read_text()) or {}
        except Exception:
            continue
        if isinstance(raw, dict) and raw.get("id"):
            return str(raw["id"]).strip()
    return ""


def _authored_here(installed) -> bool:
    """Did THIS machine write the app? Two signals, either is sufficient.

    `authored_here` is the recorded one. `provenance == 'custom'` is the
    structural one and is not a proxy: the entity's own signing key is the only
    key registered for that provenance (entity_signing.py:85-89 against
    trust_store.py:87 / :125), so a custom row could only have been signed
    here. Both are checked because a row written before the column existed
    carries NULL, and refusing an author their own app is the one outcome this
    gate must never produce.
    """
    if getattr(installed, "authored_here", None) is True:
        return True
    return getattr(installed, "provenance", None) == "custom"


async def _license_gate_site(pool, *dirs) -> None:
    """Refuse publishing a site built from an app whose AUTHOR did not allow it.

    The rule, in one line: an app's licence travels with it, and publishing a
    website from someone else's app is a right its author grants or withholds
    (LicenseSpec.publish_site, default deny).

    What this does and does not decide:
      - It fires only when the tree DECLARES an app id that is installed here.
        A plain website with no app.yaml has no author but you, so it is never
        gated.
      - Authorship beats the licence: `authored_here` means this machine wrote
        the app, and an author is never blocked by their own terms.
      - The rights come from the installed ROW, never from the tree's own
        app.yaml, so a copied-and-edited manifest cannot widen its own licence.
      - LIMIT, stated plainly: this enforces the licence of an app the tree
        admits to being. Someone who strips the manifest or renames the id is
        not stopped HERE — that is what store review and lineage are for. This
        gate makes the honest path correct; it is not an anti-copy mechanism.
    """
    await _license_check_app_id(pool, _declared_app_id(*dirs))


def _declared_app_id_in_zip(raw: bytes) -> str:
    """The app id a BUNDLE declares about itself — the zip equivalent of
    _declared_app_id, for the lane that publishes bytes rather than a tree.

    Precisely: the shallowest PARSEABLE app.yaml carrying an id wins, so a
    decoy deeper in the tree cannot shadow the real one. Same-depth ties break
    on path length, then on archive order. A zip may legally carry two entries
    with the identical name and this reads the first while most extractors keep
    the last — a parsing differential, but every bundle that has one is
    attacker-authored bytes, which is the id-rename/manifest-strip class this
    gate already says it does not defend.

    Unreadable zip -> "", and the caller treats that as nothing to enforce:
    this gate decides a right, and a corrupt bundle fails later on its own
    merits rather than here."""
    import io as _io
    import zipfile as _zipfile
    import yaml as _yaml
    # Bytes, or an open seekable binary file (zipfile reads only the central
    # directory and the app.yaml members from it, never the whole archive).
    src = _io.BytesIO(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else raw
    try:
        with _zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "app.yaml"]
            names.sort(key=lambda n: (n.count("/"), len(n)))
            for n in names:
                try:
                    d = _yaml.safe_load(zf.read(n).decode("utf-8", "replace")) or {}
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    return str(d["id"]).strip()
    except Exception:
        return ""
    return ""


async def _license_gate_site_zip(pool, raw) -> None:
    """The same rule as _license_gate_site, for the host-tool lane that hands a
    prebuilt zip to webapp.publish_bundle (runtime/tools.py manage_site
    op='publish'). That lane bypassed the gate entirely while leaving the
    manifest intact inside the bundle, which is weaker than the documented
    limit — the honest path has to be gated everywhere it exists, or "the
    author decides" is only true of the buttons we happened to cover.

    `raw` is the bundle's bytes, or an open, seekable binary file of it — the
    tool lane passes the file it is about to send, so a bundle of any size is
    checked without a second copy in memory."""
    await _license_check_app_id(pool, _declared_app_id_in_zip(raw))


async def _license_check_app_id(pool, app_id: str) -> None:
    """The one decision, shared by every publish lane. Rights come from the
    installed ROW (written from the signed bundle at install), never from
    whatever the thing being published claims about itself."""
    if not app_id:
        return
    try:
        from server.applications import registry as _registry
        installed = await _registry.get(pool, app_id)
    except Exception as e:
        # A registry read that fails must not silently open the gate.
        log.warning("license gate: registry read failed for %s: %s", app_id, e)
        raise ShipError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"cannot check the licence for app {app_id!r} right now — "
            f"publish refused rather than guessed",
        ) from None
    if installed is None:
        # Not an installed app — a project of your own that happens to carry a
        # manifest. Nothing to enforce.
        return
    if _authored_here(installed):
        return
    if (installed.app_license or {}).get("publish_site") is True:
        return
    raise ShipError(
        status.HTTP_403_FORBIDDEN,
        f"{installed.name!r} is published by someone else, and its licence "
        f"does not allow publishing a website from it. You can run it here; "
        f"putting it on the web is the author's call. Ask them to set "
        f"license.publish_site in the app, or publish a site of your own.",
    )


def _linked_build_dir(project_dir, cand: str) -> str:
    """Where a build folder that is a LINK leads, as a project-relative path:
    a folder inside the project, reached with no further link (`dist ->
    build/web`). Anything else (a link out of the project, an absolute one,
    a broken one, one to a file or through another link) is a ShipError(400)
    that names the link. Never a reason to publish something else instead:
    falling back to the project root would put its source, package.json and
    .env on a public site where the built page was meant to go."""
    from server.applications.tree_walk import (TreeEscape, link_leaves_tree,
                                               open_tree_no_escape)
    try:
        target = os.readlink(project_dir / cand)
    except OSError:
        target = "?"
    fix = (f" Replace the link with the folder itself, or point it at a folder "
           f"inside the project, then publish again.")
    if link_leaves_tree(cand, target):
        raise ShipError(status.HTTP_400_BAD_REQUEST,
                        f"the build folder `{cand}` is a link to `{target}`, outside the "
                        f"project; a site is published only from the project's own "
                        f"files." + fix)
    rel = os.path.normpath(target)
    try:
        open_tree_no_escape(project_dir, within=rel).close()
    except TreeEscape:
        raise ShipError(status.HTTP_400_BAD_REQUEST,
                        f"the build folder `{cand}` is a link to `{target}`, which is not "
                        f"a folder in the project (missing, a file, or reached through "
                        f"another link)." + fix) from None
    # A folder the zip leaves out (.adopt-backups, .history, .git,
    # node_modules ...) is not somewhere a site may be built from: the zip
    # skips those by name. Other dot folders are fine (docs/.vitepress/dist).
    bad = [c for c in rel.split("/") if c in _SITE_ZIP_SKIP_DIRS]
    if bad:
        raise ShipError(status.HTTP_400_BAD_REQUEST,
                        f"the build folder `{cand}` is a link to `{target}`, into `{bad[0]}`, "
                        f"which a published site never carries (backups, history, "
                        f".git and node_modules are left out)." + fix)
    return rel


_BUNDLER_SRC_EXT = (".ts", ".tsx", ".jsx", ".vue", ".svelte")
_RE_MODULE_SCRIPT = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_RE_ATTR = re.compile(r"""([a-zA-Z:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")


def _root_page_is_bundler_template(project_dir) -> bool:
    """Whether the project's root index.html is a bundler's TEMPLATE rather
    than a page: it has a `<script type="module">` whose src is a
    .ts/.tsx/.jsx/.vue/.svelte file, or points into `src/` when the project
    has a package.json (Vite, and the like). Such
    a page does nothing until it is built, so publishing it would put the
    project's source on a public site. A hand-written page whose build only
    compiles assets (sass, a css bundle) is not one. Unreadable counts as no."""
    page = project_dir / "index.html"
    try:
        if page.is_symlink() or not page.is_file():
            return False
        with open(page, "rb") as fh:
            html = fh.read(1 << 20).decode("utf-8", errors="ignore")
    except OSError:
        return False
    has_package_json = (project_dir / "package.json").is_file()
    for tag in _RE_MODULE_SCRIPT.findall(html):
        attrs = {m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "")
                 for m in _RE_ATTR.finditer(tag)}
        if attrs.get("type", "").strip().lower() != "module":
            continue
        src = attrs.get("src", "").split("?", 1)[0].split("#", 1)[0].strip()
        norm = src.lstrip("/")
        while norm.startswith("./"):
            norm = norm[2:]
        if norm.lower().endswith(_BUNDLER_SRC_EXT):
            return True
        # A src/ folder alone proves nothing (a buildless site may keep its
        # modules there); with a package.json beside the page it is a project
        # a bundler builds.
        if norm.startswith("src/") and has_package_json:
            return True
    return False


def _site_webroot(project_dir) -> tuple:
    """Resolve what a project publishes. Dynamic app (privatae.json at the
    project root) -> the whole tree, its own server needs the source; static ->
    the first standard build dir carrying index.html, else the project root.
    The root is refused (400, build first) only when its index.html is a
    bundler template (_root_page_is_bundler_template): a project whose build
    only compiles assets, or whose dist/ holds no page, still publishes its
    root. A build dir that is a link is followed only to a folder inside the
    project, and is then the web root (walked through the contained walk,
    _zip_site_dir); any other link is refused by name (_linked_build_dir),
    never passed over for the project root. Raises ShipError(400) when
    nothing is servable."""
    if (project_dir / "privatae.json").exists():
        return project_dir, "dynamic"
    for cand in _SITE_WEBROOT_CANDIDATES:
        if (project_dir / cand).is_symlink():
            rel = _linked_build_dir(project_dir, cand)
            if (project_dir / rel / "index.html").exists():
                return project_dir / rel, "static"
            continue      # an in-tree folder with no page, as a real one
        if (project_dir / cand / "index.html").exists():
            return project_dir / cand, "static"
    if (project_dir / "index.html").exists():
        if _root_page_is_bundler_template(project_dir):
            raise ShipError(
                status.HTTP_400_BAD_REQUEST,
                "index.html at the project root is a bundler template (it loads "
                "a module script from src/) and there is no built page in "
                + "/".join(_SITE_WEBROOT_CANDIDATES)
                + " — build the site first, then publish")
        return project_dir, "static"
    raise ShipError(
        status.HTTP_400_BAD_REQUEST,
        "no publishable web root — expected index.html in "
        + "/".join(_SITE_WEBROOT_CANDIDATES)
        + " or the project root (build the project first), "
        "or a privatae.json manifest for a dynamic app",
    )


def _zip_site_dir(root, exclude_top=frozenset(), wear_sheet: str = "", *,
                  project_dir=None, dynamic: bool = False) -> bytes:
    """The site's files as a zip. `exclude_top` names top-level folders that
    are not this site — a project's module directories when its ROOT is the
    site being published. `wear_sheet`, when given, is the desktop's token
    sheet baked into every HTML page (a published ARTIFACT keeps the look it
    had on the day it went up — server/applications/artifact_style.py).

    A published site is PUBLIC, so nothing outside `root` may ever reach the
    zip (server/applications/tree_walk.py). A link is followed only to a
    file inside the site that the zip carries anyway (`latest.html ->
    v3.html`); a link out of the site, an absolute link, a link into a
    skipped folder are left out; a linked folder inside the site is walked
    the same way. A static site leaves out every name starting with "."
    at any depth, except a top-level .well-known/ folder; `dynamic` keeps
    them (only _SITE_ZIP_SKIP_DIRS stay out). `root` itself must be a real folder, not a link. With
    `project_dir` (the folder `root` was chosen in, _site_webroot), the walk
    opens the project folder and reaches `root` from it one component at a
    time without following a link, so no folder on the way can be swapped
    for a link either."""
    import io as _io
    import time as _time
    import zipfile as _zipfile
    from server.applications.tree_walk import (LINKS_IN_TREE, TreeEscape,
                                               open_tree_no_escape, universal_newlines)

    def _skip(parts: tuple, is_dir: bool) -> bool:
        if any(part in _SITE_ZIP_SKIP_DIRS for part in parts):
            return True
        # A STATIC site publishes nothing hidden (.env, .git*), at any depth,
        # except a top-level .well-known/ folder, which domain verification
        # needs. A DYNAMIC site's zip is built and run by the zone, which
        # needs its .env, .npmrc, .streamlit/ ...; only the folders in
        # _SITE_ZIP_SKIP_DIRS (.git, .history, .adopt-backups) stay out.
        if not dynamic:
            hidden = parts[1:] if parts[0] == ".well-known" and (is_dir or len(parts) > 1) else parts
            if any(part.startswith(".") for part in hidden):
                return True
        if parts[0] in exclude_top:
            return True
        return not is_dir and parts[-1] in _SITE_ZIP_SKIP_FILES

    def _too_big() -> ShipError:
        return ShipError(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"site bundle exceeds {_SITE_MAX_ZIP // (1024 * 1024)} MB unpacked, the "
            "platform's limit on what a bundle may unpack to — publish the BUILT "
            "output (dist/build), not the source tree",
        )

    buf = _io.BytesIO()
    total = 0
    try:
        if project_dir is not None:
            within = Path(root).relative_to(project_dir).as_posix()
            tree = open_tree_no_escape(project_dir, within="" if within == "." else within)
        else:
            tree = open_tree_no_escape(root)
    except (TreeEscape, ValueError):
        raise ShipError(status.HTTP_400_BAD_REQUEST,
                        f"the site folder {Path(root).name!r} is a link or not a folder; "
                        "publish a real folder") from None
    with tree, _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in tree.walk(skip=_skip, links=LINKS_IN_TREE):
            total += f.size
            if total > _SITE_MAX_ZIP:
                raise _too_big()
            data = tree.read(f, limit=_SITE_MAX_ZIP + 1)
            if data is None:
                continue          # gone, or swapped for a link, since the walk listed it
            total += len(data) - f.size      # it may have grown since the walk
            if total > _SITE_MAX_ZIP:
                raise _too_big()
            if wear_sheet and os.path.splitext(f.rel)[1].lower() in (".html", ".htm"):
                from server.applications.artifact_style import wear_desktop
                try:
                    page = universal_newlines(data.decode("utf-8"))
                except UnicodeDecodeError:
                    page = None
                if page is not None:
                    zf.writestr(f.rel, wear_desktop(page, wear_sheet))
                    continue
            # What zf.write(path) recorded: the file's mtime and mode.
            when = _time.localtime(f.mtime)[:6]
            info = _zipfile.ZipInfo(f.rel, when if when[0] >= 1980 else (1980, 1, 1, 0, 0, 0))
            info.external_attr = (f.mode & 0xFFFF) << 16
            info.compress_type = _zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


async def publish_site_service(pool, project_id: int, *, slug: str,
                               module: str | None = None) -> dict:
    """Publish a Workbench project's built web output to the public webapp zone —
    the one-click sibling of publish_project_service. Zips the resolved web root
    server-side and submits via the caller-scoped webapp.publish_bundle op (the
    bridge fixes the publisher from its mTLS cert; the controller annotates the
    HTML for live-edit and signs). A site this creates goes live PUBLIC, the
    controller's default (the private look at a build is its artifact, and publishing is
    the intentional act of making it public). It used to be born on the
    authenticated tier as a "private preview", which the controller stores with
    no email list, so every logged-in platform account could open it while the
    Host and the Workbench said it was private. A republish keeps the owner's
    chosen visibility; Private is chosen in Sites (webapp.set_exposure, which
    scopes it to the creator). Raises ShipError / PublisherError."""
    import base64 as _b64

    async with pool.acquire() as conn:
        proj = await conn.fetchrow(
            "SELECT id, entity_id, name, status, kind FROM projects WHERE id = $1",
            project_id,
        )
    if proj is None:
        raise ShipError(status.HTTP_404_NOT_FOUND, f"project {project_id} not found")
    # A host-made artifact is shown wearing the desktop; publishing freezes
    # that look into its pages. A Workbench project's site is left as built.
    wear_sheet = ""
    if proj["kind"] == "artifact":
        try:
            from server.routes.appearance import token_sheet
            wear_sheet = await token_sheet()
        except Exception as e:  # noqa: BLE001 — publish as written, never fail on style
            log.warning("publish_site: desktop style unavailable for artifact %s: %s",
                        project_id, e)
    project_dir = await _module_dir_for(pool, project_id, module, ("public_site",),
                                        "published as a site")
    if not project_dir.exists():
        raise ShipError(
            status.HTTP_404_NOT_FOUND,
            f"project {project_id} has no working tree at {project_dir}",
        )
    root, runtime_kind = _site_webroot(project_dir)
    # The author's licence decides before anything is packed: check the web
    # root AND the project root, because a built tree carries the manifest at
    # whichever of the two the toolchain left it.
    await _license_gate_site(pool, root, project_dir)
    # A ROOT site in a project that also has modules: when the web root is
    # the project root itself, the module folders are other apps and sites —
    # never part of this one (the same rule as the root app's bundle).
    _exclude_top = frozenset()
    if module is None and root == project_dir:
        _exclude_top = frozenset(r["name"] for r in await pool.fetch(
            "SELECT name FROM project_modules WHERE project_id=$1", project_id))
    raw = _zip_site_dir(root, _exclude_top, wear_sheet=wear_sheet, project_dir=project_dir,
                        dynamic=runtime_kind == "dynamic")
    # WHICH APP THIS SITE RUNS, recorded at publish. Without it a publish
    # carries the bytes, the slug and an exposure policy, and the app behind
    # the site is simply lost. It is needed because
    # "bring my data from <site>" is only truthful when the site and the Host
    # are running the SAME app, and a guess there would offer somebody a move
    # from a site they have no rows on.
    #
    # Read with _declared_app_id over the SAME two directories the licence gate
    # just used, deliberately. The zip-reading sibling (_declared_app_id_in_zip)
    # would only see the built web root, and a toolchain that leaves app.yaml at
    # the project root would then publish with no lineage while the licence gate
    # two lines up found the app perfectly well — two readers disagreeing about
    # which app this is, which is exactly the kind of split the licence gate's
    # own two-directory read exists to avoid.
    #
    # Empty means UNKNOWN and is sent as nothing. A hand-built site with no
    # manifest is legitimate; it simply never appears as an offer.
    _site_app_id = _declared_app_id(root, project_dir)
    res = await publisher_op_service("webapp.publish_bundle", {
        "zip_b64": _b64.b64encode(raw).decode("ascii"),
        "slug": (slug or "").strip().lower(),
        **({"source_app_id": _site_app_id} if _site_app_id else {}),
    })
    log.info("published project %d as site %s (%s web root, %d bytes)",
             project_id, res.get("hostname"), runtime_kind, len(raw))
    # Durable inbox row:
    # "your site is live, here's the link" is the second of the two default
    # reach-out triggers. Identifiers + the public hostname only. Best-effort —
    # the publish already succeeded; a failed inbox write is logged, never raised.
    try:
        from server.bridge.memory_handlers import handle_inbox_append as _inbox_append
        from server.deep_link import app_link as _app_link
        _host = (res.get("hostname") or "").strip()
        _r = await _inbox_append({
            "subject": (f"Site published: {_host}" if _host
                        else f"Site published from project {project_id}"),
            # True on a republish too: a new site is public, and a republish
            # keeps the visibility its owner set (the controller's publish
            # response does not say which one this was).
            "body": (f"Live at {_host}. A new site is public; set who can see it in Sites."
                     if _host else ""),
            "message_kind": "system_notification",
            "payload": {
                "event": "site_published",
                "project_id": project_id,
                "slug": (slug or "").strip().lower(),
                "hostname": _host or None,
                # The Sites app, not the site: a reach-out's link only ever
                # opens this host (reachout/service.doorbell_link says why),
                # and Sites is where its visibility is set.
                "link": _app_link("sites"),
            },
        })
        if not (_r or {}).get("ok"):
            log.warning("publish_site_service: inbox row not written (project=%d): %s",
                        project_id, (_r or {}).get("error"))
    except Exception as _e:
        log.warning("publish_site_service: inbox append failed (project=%d): %s", project_id, _e)
    # A scheduled task waiting for this site wakes (server/host_events.py).
    from server import host_events
    await host_events.emit(pool, None, "site_published",
                           {"project_id": project_id, "slug": (slug or "").strip().lower(),
                            "hostname": res.get("hostname")})
    return {
        **res,
        "runtime": runtime_kind,
        "project_id": project_id,
        # A new site is public (see docstring); a republish keeps the tier.
        # The Workbench prints this under "Live at" (ops-pane.tsx PublishSection).
        "staging_note": "live — a new site is public; set who can see it in the Sites app",
    }


_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")


def _entity_git(args: list, *, cwd=None, timeout: int = 240):
    """Run git AS THIS ENTITY against its own gitea. The token comes from the
    identity-reconciler env (GITEA_API_TOKEN) and is passed via an inline
    credential helper + env — never on the command line, never on disk."""
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ)
    env["CSERIES_ENTITY_GIT_TOKEN"] = _os.environ.get("GITEA_API_TOKEN", "")
    base = [
        "git",
        "-c", "credential.helper=",
        "-c", 'credential.helper=!f() { echo "username=oauth2"; '
              'echo "password=$CSERIES_ENTITY_GIT_TOKEN"; }; f',
    ]
    return _sp.run(base + args, cwd=cwd, env=env,
                   capture_output=True, text=True, timeout=timeout)


async def publish_site_repo_service(*, repo: str, slug: str, ref: str = "") -> dict:
    """Publish a site straight from one of the entity's gitea repos — no
    Workbench project, no upload. Shallow-clones the repo AS THE ENTITY (its own
    gitea token; gitea's ACL decides what it may read — exactly the consent
    granted in the Git permissions panel), resolves the same web root as the
    project lane (_site_webroot: privatae.json -> dynamic whole-tree, else built
    dir, else root index.html), zips, and submits via the caller-scoped
    webapp.publish_bundle op. Like the project lane, a NEW site goes live
    public (see publish_site_service). Raises ShipError / PublisherError."""
    import asyncio as _asyncio
    import base64 as _b64
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    m = _REPO_RE.match((repo or "").strip())
    if not m:
        raise ShipError(status.HTTP_400_BAD_REQUEST,
                        "repo must be owner/name (this host's gitea)")
    if not _os.environ.get("GITEA_API_TOKEN") or not _os.environ.get("GITEA_URL"):
        raise ShipError(status.HTTP_503_SERVICE_UNAVAILABLE,
                        "no gitea identity on this host (GITEA_URL/GITEA_API_TOKEN)")
    host = _os.environ["GITEA_URL"].rstrip("/")
    url = f"{host}/{m.group(1)}/{m.group(2)}.git"

    tmp = _tempfile.mkdtemp(prefix="site-publish-")
    try:
        args = ["clone", "--depth", "1"]
        if (ref or "").strip():
            args += ["--branch", ref.strip()]
        args += [url, tmp]
        r = await _asyncio.to_thread(_entity_git, args)
        if r.returncode != 0:
            raise ShipError(
                status.HTTP_400_BAD_REQUEST,
                "clone failed (does this host have access to the repo?): "
                + (r.stderr or "").strip()[:300],
            )
        root, runtime_kind = _site_webroot(Path(tmp))
        # Same licence gate as the project lane. A repo is the likelier way to
        # arrive here holding someone else's app — a shared repo the entity was
        # granted read on is exactly the "publish their work as my site" shape.
        # This service takes no pool (it is repo-only), so read the same one the
        # routes use; no pool means the check cannot run, and an unrunnable
        # check refuses rather than waves through.
        _pool = brain_service.get_pool()
        if _pool is None:
            raise ShipError(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "cannot check the app licence right now (no pool) "
                            "— publish refused rather than guessed")
        await _license_gate_site(_pool, root, Path(tmp))
        raw = _zip_site_dir(root, project_dir=Path(tmp),  # skips .git/node_modules/… by name
                            dynamic=runtime_kind == "dynamic")
        res = await publisher_op_service("webapp.publish_bundle", {
            "zip_b64": _b64.b64encode(raw).decode("ascii"),
            "slug": (slug or "").strip().lower(),
        })
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)
    log.info("published repo %s as site %s (%s web root, %d bytes)",
             repo, res.get("hostname"), runtime_kind, len(raw))
    from server import host_events
    await host_events.emit(brain_service.get_pool(), None, "site_published",
                           {"repo": repo, "slug": (slug or "").strip().lower(),
                            "hostname": res.get("hostname")})
    return {
        **res,
        "runtime": runtime_kind,
        "repo": repo,
        "staging_note": "live — a new site is public; set who can see it in the Sites app",
    }


async def ship_project_service(pool, project_id: int, *, verify: bool = True,
                               module: str | None = None) -> dict:
    """Pack a completed Workbench project into a signed .cseries bundle, sign it with
    the per-container entity key, and install it via the normal pipeline — the SINGLE
    source of truth for POST /build (the route) AND the ship_project_app host tool.

    `verify`: install is not delivery. With verify=True (the
    route and the host tool) the kernel starts the app and PROVES it runs
    after this returns (mastercode.runtime.app_verify — the app's own ready
    check + the lines it printed, then the host is woken with the verdict,
    and a failed proof opens a seeded repair when the project auto-delivers).
    The plan-end chain passes verify=False and starts the proof itself with
    its own origin (app_verify.ship_and_verify). The returned dict carries
    `verify_task_id` when a proof was started.

    Holds EVERY invariant the route had: project must exist and have no build
    in flight (the status='complete' requirement was removed — it blocked
    re-shipping a live app the moment any maintenance plan opened, and the tree
    checks below decide shippability substantively);
    a real app.yaml on disk declaring provenance=custom; the static build-contract
    validator (validate_build); the sign-time SDK integrity gate (sdk_store.
    verify_current — a poisoned validator must not certify apps); SDK-binding the
    bundle (validated_under_sdk); entity-key signing; entitlement-free self-install.

    Returns {project_id, installed, bundle_size_bytes, open_phases}. Raises
    ShipError(status, detail) for every HTTP-ish failure the route raised. Reads
    NOTHING from a Request.

    `open_phases` > 0 means the project shipped MID-PLAN — allowed on purpose
    (shippability is a property of the tree, not the plan state), but the user
    must hear it from the ship rather than discover it later."""
    _packed = await _pack_and_sign_project(pool, project_id, module)
    signed = _packed["signed"]
    try:
        # Entity self-built bundles (provenance=custom, free for the owning entity)
        # bypass the per-entity premium gate.
        installed = await installer.install_bundle(
            pool, signed, source="self", check_entitlement=False,
        )
    except installer.AppRuntimeOverPlan as e:
        # The build is good; the machine is full. Ship surfaces it as the
        # upgrade it is (402 + the numbers), never as a build failure — the
        # user's own project must not read as broken because the plan is.
        raise ShipError(status.HTTP_402_PAYMENT_REQUIRED, str(e)) from None
    except installer.InstallError as e:
        raise ShipError(status.HTTP_400_BAD_REQUEST, f"install failed: {e}") from None

    log.info("factory built %s from project %d", installed.id, project_id)
    out = {
        "project_id": project_id,
        "installed": _serialize(installed),
        "bundle_size_bytes": len(signed),
        # >0 means this shipped MID-PLAN. Allowed deliberately — the tree
        # decides, not the plan — but the user hears it from the ship instead
        # of finding out later.
        "open_phases": int(_packed.get("open_phases") or 0),
    }
    if verify:
        # The check records the turn type its work belongs to (v186, turn
        # types step 6). Shipped from a turn, that turn's type is already set;
        # shipped from the Ship button there is no turn, so the project's own
        # type binding names it — never "untyped" by default for a typed
        # project (a failed read records none rather than guessing).
        _tt_tok = None
        try:
            from mastercode.runtime.turn_types import (
                CURRENT_TURN_TYPE, load_turn_type)
            if CURRENT_TURN_TYPE.get() is None:
                _proj_type = await load_turn_type(
                    pool, int(_packed["entity_id"]), project_id=project_id)
                if _proj_type is not None:
                    _tt_tok = CURRENT_TURN_TYPE.set(_proj_type)
        except Exception as e:  # noqa: BLE001 — a record never blocks the proof
            log.debug("post-ship proof: project type not read (recorded untyped): %s", e)
        try:
            from mastercode.runtime.app_verify import start_verify
            out["verify_task_id"] = await start_verify(
                pool=pool, entity_id=int(_packed["entity_id"]), project_id=project_id,
                app_id=installed.id, reason="ship",
                # The module this app was packed from: a repair of a failed
                # proof rebuilds that directory, not the project root.
                origin={"module": module} if module else None)
            out["verify_note"] = (
                "The platform is now starting the app and proving it runs (its own "
                "ready check + what it prints). The verdict is reported in the "
                "project thread; do not call the app delivered before it lands.")
        except Exception as e:  # noqa: BLE001 — the install stands; the proof is reported
            log.warning("post-ship proof for %s could not start: %s", installed.id, e)
            out["verify_note"] = f"proof could not start: {e}"
        finally:
            if _tt_tok is not None:
                from mastercode.runtime.turn_types import CURRENT_TURN_TYPE as _CTT
                _CTT.reset(_tt_tok)
    return out
