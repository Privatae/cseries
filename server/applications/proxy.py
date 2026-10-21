"""
Per-app request proxy. The dashboard loads
`/system-apps/<id>/<whatever>` to render an app window. This module
answers that request by looking at the app's installed runtime:

  * kind=static   -> serve a file off the install dir
  * kind=process  -> forward the request to the sidecar's localhost
                     port, byte-for-byte, including the
                     X-Application-Token header — which, for a sidecar
                     in the Host container, must be THIS app's own window
                     token (_require_own_window)

Replaces the StaticFiles mount on /system-apps. The new mount is a
FastAPI route that branches on the registry's runtime metadata for
the requested app id.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import posixpath
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

import asyncio
import socket

import aiohttp
import yaml
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from starlette.responses import FileResponse, Response, StreamingResponse

from server import brain_service
from server import host_names
from server import static_paths
from server.applications import registry
from server.applications.kernel import SDK_MANAGED_APPS
from server.applications.manifest import IMAGE_BAKED_PROVENANCES
from server.applications.supervisor import get_supervisor

log = logging.getLogger("privatae.applications.proxy")

INSTALL_ROOT = Path("/var/lib/cseries/apps")


router = APIRouter(tags=["applications", "proxy"])


# Headers we never forward in either direction (hop-by-hop +
# things that would confuse the destination server).
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}


def _filter_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in items if k.lower() not in _HOP_HEADERS}


# The browser sidecar's control API grew a bearer gate (sidecars/browser/
# control/app.py) so it can survive leaving the entity pod. THIS PROXY IS A
# THIRD CONSUMER of that token and it is the easiest one to miss: the
# Browser system-app is `kind: external, port: 8830` (system-apps/browser/
# app.yaml), so every click in its UI arrives here and gets forwarded to the
# control API. Without the header, a token that is set anywhere 401s the
# whole UI while the vault broker — which does send it — keeps working, so
# the failure looks like "the app is broken" rather than "auth is on".
#
# Keyed on app id, not port: a port is a coincidence, the app id is the
# contract. Empty token = send nothing, which is the in-pod default and
# leaves every existing entity byte-identical.
_BROWSER_CONTROL_APP_ID = "browser"
_BROWSER_CONTROL_TOKEN = os.environ.get("BROWSER_CONTROL_TOKEN", "").strip()


def _upstream_auth(app_id: str) -> dict[str, str]:
    """Auth header this app's upstream requires, if any."""
    if app_id == _BROWSER_CONTROL_APP_ID and _BROWSER_CONTROL_TOKEN:
        return {"X-Browser-Token": _BROWSER_CONTROL_TOKEN}
    return {}


def _read_runtime_kind(install_dir: Path) -> str:
    """Read manifest from disk and return runtime.kind."""
    return _read_runtime(install_dir)[0]


def _read_runtime(install_dir: Path) -> tuple[str, int | None, bool]:
    """Read manifest from disk and return (runtime.kind, runtime.port, strip_prefix).

    Port is only meaningful for kind=external. Returns None for the
    other kinds. strip_prefix (default True) applies to kind=external: True
    strips the /system-apps/<id>/ prefix before forwarding (sidecar serves at
    root, e.g. the Browser sidecar); False forwards the full path unchanged
    (sidecars that emit ABSOLUTE asset URLs under the public path). Defaults to ('static', None, True) when the manifest is
    missing or malformed (matches pre-Ship-5 behavior)."""
    manifest_path = install_dir / "app.yaml"
    if not manifest_path.exists():
        return ("static", None, True)
    try:
        raw = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError:
        return ("static", None, True)
    rt = raw.get("runtime") or {}
    kind = str(rt.get("kind") or "static")
    strip = bool(rt.get("strip_prefix", True))
    port = rt.get("port")
    if isinstance(port, int) and 0 < port <= 65535:
        return (kind, port, strip)
    return (kind, None, strip)


_HELPER_TOKEN_MISSING = ("helper access requires the app's window token "
                         "(use CSeriesApp.helperFetch)")
_BACKEND_TOKEN_MISSING = ("this app's backend answers only its own window: "
                          "send the window's X-Application-Token")


async def _require_own_token(pool, app_id: str, request: Request,
                             missing: str = _HELPER_TOKEN_MISSING) -> None:
    """Requester→app binding for the runtime surface: the caller's
    window token (X-Application-Token) must decode to THIS app's id, and be
    honoured on the origin the request arrived on (an isolated app's
    token only on its own origin). Each isolated app has its own
    origin, but a same-origin app's backend is still reached from the desktop
    origin every same-origin app shares, so the token, not the origin, says
    which app is asking. Fails closed: no token, bad token, or another app's
    token are all the same 403. The SDK's helperFetch stamps the header; a
    hand fetch without it is refused.

    HEADER ONLY, never a cookie: a cookie rides every request to the origin,
    whichever app's page sent it, so it cannot say which app is asking. (The
    /api middleware does not read an `app_token` cookie either.)

    No time check: tokens carry no expiry (tokens.py, "Lifetime"). A token
    signed under a rotated kernel secret fails verify() and is refused here."""
    from server.applications import tokens
    raw = request.headers.get("X-Application-Token", "")
    if not raw:
        raise HTTPException(status.HTTP_403_FORBIDDEN, missing)
    try:
        secret = await tokens.get_secret(pool)
        tok = tokens.verify(secret, raw)
    except Exception:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid app token")
    if tok.app_id != app_id:
        log.warning("window token binding refused: token app %r on path app %r",
                    tok.app_id, app_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "token is bound to a different app")
    # The origin half, checked here as well as in the token middleware:
    # a WebSocket upgrade reaches this through serve_app_ws, and no
    # BaseHTTPMiddleware sees an upgrade.
    from server.applications import app_origin
    if not app_origin.token_fits_origin(
            tok.origin_key, app_origin.classify(app_origin.browser_host(request.headers))):
        log.warning("window token for %r refused: belongs to another origin", tok.app_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "this window token belongs to another origin")


async def _require_own_window(pool, app_id: str, request: Request) -> None:
    """The gate in front of every app backend that runs IN the Host container:
    a `kind: process` app with no runtime.image (its sidecar is a child of
    the kernel, with the kernel's filesystem and env) and a `kind: external`
    app with no runtime.harness (a loopback port in the Host pod).

    Why: this route forwards any method and any path to such a backend, the
    /api token middleware never sees /system-apps/, and every isolated or
    community app was framed on the one apps-<handle> origin with CSP
    connect-src 'self' — so without this, app B's page could call
    /system-apps/<A>/<any backend route> for as long as A's backend is up.
    The one shipped in-container backend, the Claude Code sidecar, served
    POST /sessions (an agent with a shell in the Host container) and
    POST /disconnect (deletes the Claude credential) that way.

    Two callers pass, nothing else:
      * THIS app's own window — its X-Application-Token decodes to app_id
        (_require_own_token). A same-origin app gets the header from the
        shell's fetch wrapper (dashboard/base/js/app-runner.js,
        installTokenShim); an SDK-managed one from app-sdk.js.
      * the kernel's own page check of THIS app — a current verify
        capability minted for app_id (mastercode.runtime.app_verify). The
        Browser sidecar loading the page for check_page or the ship proof
        has no window and so no token; the cap is the kernel's proof that it
        asked for exactly this app's page. For HTTP, serve_app has already
        refused a cap that is not current; serve_app_ws has no cap check of
        its own, so for a WebSocket this is the only one."""
    cap = request.headers.get("x-cseries-verify-cap")
    if cap is not None:
        from mastercode.runtime.app_verify import verify_cap_is_current
        if verify_cap_is_current(app_id, cap):
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verify capability not current")
    await _require_own_token(pool, app_id, request, missing=_BACKEND_TOKEN_MISSING)


def _read_image(install_dir: Path) -> str | None:
    """runtime.image from the on-disk manifest, or None — the raw-YAML twin of
    manifest.runs_in_pod for a process app (this module reads manifests raw,
    see _read_runtime). Non-None means the process runs in its own pod."""
    manifest_path = install_dir / "app.yaml"
    if not manifest_path.exists():
        return None
    try:
        raw = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError:
        return None
    img = (raw.get("runtime") or {}).get("image")
    return str(img).strip().lower() if img else None


# ── sidecar paths the USER'S SESSION must never reach ────────────────────
#
# This proxy forwards EVERY non-static path under /system-apps/<app>/* to the
# sidecar and injects the control token on the caller's behalf (_upstream_auth),
# and the app-token middleware only gates /api/ — so without this list, anything
# holding the user's dashboard session can call any sidecar endpoint with the
# kernel's own credential.
#
# For most endpoints that is the design: the Browser app's own window IS the user
# driving the sidecar. The exception is an endpoint that hands back a CREDENTIAL.
# `GET /profile` returns the browser's storage_state — every cookie and every
# localStorage entry in plaintext, which for a bank is a bearer credential with no
# password and no second factor behind it. The kernel reads it to seal it into the
# vault (server/applications/browser_profile.py) and gates its own routes on
# write:vault; forwarding the raw export here would route around that gate
# entirely, and the profile design's "there is no read path" claim is only true
# with this in place.
#
# Keyed by app so it cannot silently widen: a new app gets no entries and behaves
# exactly as before.
_KERNEL_ONLY_PATHS: dict[str, tuple] = {
    "browser": ("profile",),
}


def _refuse_kernel_only(app_id: str, path: str) -> None:
    """404 a kernel-only sidecar path reached through the user-facing proxy.

    404 rather than 403: the answer to "does the user's session have a way to
    export this" should be "there is nothing here", not "there is something here
    and you may not have it"."""
    first = (path or "").lstrip("/").split("/", 1)[0].split("?", 1)[0].lower()
    if first and first in _KERNEL_ONLY_PATHS.get(app_id, ()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


# ── harness control paths an IN-POD sidecar is never sent ─────────────────
#
# cli/cseries_sidecar.py serves these to one caller only: the dispatcher, which
# dials an own-pod harness's Service directly (server/routes/review.py
# _clone_in / _collect_out / _collect_session) and never comes through this
# proxy. The shared sidecar once served them in EVERY process that
# imported it, including the Claude Code app's sidecar inside the Host
# container, and this proxy forwarded them there with no token check: a
# community app's page on apps-<host> could delete every project with one
# POST /system-apps/claude-code/workspace/load?dir= or read them all with
# GET .../workspace/export. The sidecar now registers them only in a harness
# image (CSERIES_SIDECAR_ROLE); this is the second wall, for any in-pod sidecar
# built on an older copy of the library.
#
# Must equal cseries_sidecar.HARNESS_ONLY_ROUTES (paths, no leading slash);
# scripts/test_sidecar_workspace_routes_harness_only.py holds them in step.
_HARNESS_ONLY_SIDECAR_PATHS = frozenset({
    "workspace/load", "workspace/export", "session/export", "run/progress",
})


def _upstream_request_path(fwd: str, query: str) -> str:
    """The path-and-query aiohttp will put on the request line when this proxy
    forwards `fwd` (the path AFTER Starlette decoded it once) with `query`.

    Built exactly as _serve_process / _serve_external build the upstream URL,
    then parsed the way aiohttp parses it: URL(str) with yarl's normalisation
    on, sent as url.raw_path_qs. That normalisation is the reason a check on
    `fwd` alone is not enough. yarl decodes %2e and every other unreserved
    escape and THEN removes dot segments, so a request for
    x/%252e%252e/workspace/load reaches Starlette as x/%2e%2e/workspace/load
    and reaches the sidecar as /workspace/load (measured on yarl 1.24.5, the
    aiohttp 3.14.3 client). A path yarl refuses to parse is one aiohttp could
    not send either; the caller refuses it."""
    from yarl import URL
    url = f"http://sidecar.invalid/{fwd}" + (f"?{query}" if query else "")
    return URL(url).raw_path_qs


def _refuse_harness_only(fwd: str, query: str = "") -> None:
    """404 a harness control path this proxy is about to forward to an in-pod
    sidecar (_names_harness_route). 404, as _refuse_kernel_only: there is
    nothing here for this caller. The WebSocket lane (serve_app_ws) asks
    _names_harness_route directly and closes instead."""
    if _names_harness_route(fwd, query):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _names_harness_route(fwd: str, query: str = "") -> bool:
    """Does forwarding `fwd` (the path exactly as it will be appended to the
    upstream base) with `query` reach a harness-only sidecar route?

    Tested on what the sidecar will RECEIVE (_upstream_request_path), in two
    readings; True if either names a harness route:
      * exact: the request line read the way cseries_sidecar routes it,
        urlparse(line).path, which also drops a trailing ;params;
      * loose: that line with every %-escape decoded until stable, ;params
        dropped from each segment, dot segments resolved, slashes collapsed
        and case folded. It over-refuses on purpose (a triple-encoded dot
        that no current stack decodes), which costs a path no sidecar serves.
    A path yarl cannot parse is one aiohttp could not send either: True."""
    try:
        line = _upstream_request_path(fwd or "", query or "")
    except (ValueError, TypeError):
        return True
    exact = urlparse(line).path.strip("/")
    loose = line.split("?", 1)[0]
    for _ in range(8):
        nxt = unquote(loose)
        if nxt == loose:
            break
        loose = nxt
    loose = loose.split("?", 1)[0].split("#", 1)[0]
    loose = "/".join(seg.split(";", 1)[0] for seg in loose.split("/"))
    loose = posixpath.normpath("/" + loose.lstrip("/")).strip("/").lower()
    return exact in _HARNESS_ONLY_SIDECAR_PATHS or loose in _HARNESS_ONLY_SIDECAR_PATHS


# A request carrying a verify capability comes from the browser on the
# harness pool (mastercode.runtime.app_verify), loading a shipped app for its
# proof. Its body is read under a cap (server/harness_input.py);
# an app page's own requests are small. Other requests are the user's own and
# keep the forwarders' behaviour.
_VERIFY_BODY_MAX = 16 * 1024 * 1024


async def _request_body(request: Request) -> bytes:
    if request.headers.get("x-cseries-verify-cap") is None:
        return await request.body()
    from server import harness_input as _hin
    try:
        return bytes(await _hin.read_request(request, _VERIFY_BODY_MAX, "verify request"))
    except _hin.HarnessInputRefused as e:
        raise HTTPException(413, str(e))


def _read_harness(install_dir: Path) -> str | None:
    """The harness id backing this external app (runtime.harness), or None for an
    in-pod sidecar. A harness-backed app runs on the harness pool and is woken on
    demand + reached via its Service DNS (see server/applications/harness_backend)."""
    manifest_path = install_dir / "app.yaml"
    if not manifest_path.exists():
        return None
    try:
        raw = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError:
        return None
    h = (raw.get("runtime") or {}).get("harness")
    return str(h) if h else None


def _files_here(app_id: str, app, path: str, conn) -> str:
    """app_origin.app_files_here for this request: SERVE, IMAGE or REFUSE.
    `conn` is the Request or WebSocket; the browser's name is read the way
    every origin check reads it (X-Forwarded-Host first, then Host). A row
    with no provenance is taken as community — isolated, the closed answer."""
    from server.applications import app_origin
    origin = app_origin.classify(app_origin.browser_host(conn.headers))
    provenance = getattr(app, "provenance", None) or "community"
    return app_origin.app_files_here(
        app_id, is_isolated(app_id, provenance), origin, path or "")


@router.api_route(
    "/system-apps/{app_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def serve_app(app_id: str, path: str, request: Request) -> Response:
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    app = await registry.get(pool, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"not installed: {app_id}")
    if app.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"app not active: {app.status}")

    # Where this app's files may be served (app_origin.app_files_here):
    # an ISOLATED app's pages run only on its own origin — on the desktop's
    # origin they would run same-origin with the live desktop — and only its
    # images are answered there, sandboxed, for the dock.
    from server.applications import app_origin
    here = _files_here(app_id, app, path, request)
    if here == app_origin.REFUSE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not served on this address")

    # A cross-pod caller presenting a ship-proof capability (the browser
    # sidecar loading this app's page — middleware/localhost_only lets the
    # request past the source-IP gate ONLY because this header is present).
    # The cap is one-shot per verify, minted for THIS app id, and checked
    # constant-time; a wrong, stale or other-app cap is refused here. With no
    # header this branch is inert and the request is exactly what it was.
    _presented = request.headers.get("x-cseries-verify-cap")
    if _presented is not None:
        from mastercode.runtime.app_verify import verify_cap_is_current
        if not verify_cap_is_current(app_id, _presented):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "verify capability not current")

    install_dir = INSTALL_ROOT / app_id
    if not install_dir.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"install dir missing: {app_id}")

    # A store app may not register a service worker. A service worker answers
    # the app's own requests with responses it builds in the browser, and those
    # carry no Content-Security-Policy, so the page or worker it answers runs
    # past the wall (_app_csp): measured in Chromium, a store app's
    # service worker handed it a worker and a page that both reached an
    # undeclared host. The browser marks the fetch of a service worker's own
    # script with this header (Service Workers, "Update"), so refusing it here
    # makes the registration fail, on every lane below.
    if (request.headers.get("service-worker", "").strip().lower() == "script"
            and _app_meta(install_dir)[0] == "community"):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "a store app may not register a service worker")

    if here == app_origin.IMAGE:
        # A regular FILE on disk or nothing: an image request never reaches an
        # app's backend, which could answer it with anything, and never a
        # directory, which _serve_static would answer with its index.html.
        if not _is_static_file(install_dir, path):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not served on this address")
        resp = _serve_static(install_dir, path, app_id)
        resp.headers.update(app_origin.IMAGE_HEADERS)
        return resp

    kind, port, strip_prefix = _read_runtime(install_dir)

    if kind == "static":
        return _serve_static(install_dir, path or "index.html", app_id)

    if kind == "process":
        # UI shell (index.html, css/, js/, etc.) is static on disk; only
        # backend routes hit the process. Try the install dir first —
        # if no matching file, fall through to the process.
        if _has_static(install_dir, path or "index.html"):
            return _serve_static(install_dir, path or "index.html", app_id)
        if _read_image(install_dir):
            # The app's own runtime pod (Form 2): same wake +
            # 502-repair as the helper lane, but EVERY non-static path is
            # forwarded — the process is the app's whole backend, not a
            # fetch proxy under one subpath. No window-token binding here,
            # and NOT because only the user's own apps land here: a
            # community app may declare process with an image
            # (installer.py, the community runtime gate). The binding is
            # absent because such a process may serve the app's own pages
            # (any path with no file on disk reaches it), and a frame
            # navigation carries no X-Application-Token header, so a header
            # gate here could blank such an app. Cross-app reach into these
            # pods is a separate, open question; the in-container sidecar
            # below is gated.
            from server.applications import harness_backend
            host = await harness_backend.ensure_awake_app(app_id)
            if host is None:
                raise _not_awake(app_id, f"{app_id} is starting — retry")
            try:
                return await _serve_external(app_id, 0, path, request,
                                             strip_prefix=True, upstream_host=host)
            except HTTPException as exc:
                if exc.status_code != status.HTTP_502_BAD_GATEWAY:
                    raise
                harness_backend.invalidate_app_url(app_id)
                host = await harness_backend.ensure_awake_app(app_id)
                if host is None:
                    if harness_backend.wake_refused(app_id):
                        raise _not_awake(app_id, "") from None
                    raise
                return await _serve_external(app_id, 0, path, request,
                                             strip_prefix=True, upstream_host=host)
        # In the Host container: the sidecar is a child of the kernel. Only
        # THIS app's own window reaches it. Static files above stay
        # token-less: the frame's navigation to index.html cannot carry the
        # header, and they are the same bytes every app can already read.
        await _require_own_window(pool, app_id, request)
        return await _serve_process(app_id, path, request)

    if kind == "helper":
        # App runtime: the platform's
        # fetch+cache helper in its own scale-to-zero pod. Static-first for
        # the UI shell; ONLY the /helper/* subpath reaches the pod, and that
        # subpath is the design's binding point: every isolated app once
        # shared ONE origin (apps-<handle>) — and same-origin
        # apps still share the desktop's — so same-origin fetch let app A
        # dial app B's paths — the window token's app_id must therefore
        # match the PATH's app id or A could drive B's helper (exfiltrate
        # via query strings to hosts the user consented to for B, read or
        # poison B's cache). Static assets stay token-less, unchanged.
        if _has_static(install_dir, path or "index.html"):
            return _serve_static(install_dir, path or "index.html", app_id)
        if not path.startswith("helper/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"no such file: {path}")
        await _require_own_token(pool, app_id, request)
        from server.applications import harness_backend
        host = await harness_backend.ensure_awake_app(app_id)
        if host is None:
            raise _not_awake(app_id, f"{app_id} helper is starting — retry")
        # /system-apps/<id>/helper/fetch/<host>/<path> -> /fetch/<host>/<path>
        fwd = path[len("helper/"):]
        try:
            return await _serve_external(app_id, 0, fwd, request,
                                         strip_prefix=True, upstream_host=host)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_502_BAD_GATEWAY:
                raise
            # Reaped/moved pod: a dead cached IP must cost one
            # failed dial + a re-wake, never a permanently broken window.
            harness_backend.invalidate_app_url(app_id)
            host = await harness_backend.ensure_awake_app(app_id)
            if host is None:
                if harness_backend.wake_refused(app_id):
                    raise _not_awake(app_id, "") from None
                raise
            return await _serve_external(app_id, 0, fwd, request,
                                         strip_prefix=True, upstream_host=host)

    if kind == "external":
        # k8s pod-level sidecar — port comes from manifest, no
        # supervisor lookup. Same static-first pattern as kind=process:
        # UI shell (index.html, icon, css, js) is served from the
        # install dir on disk; everything else forwards to the
        # sidecar. Lets the Browser app drop a plain HTML shell into
        # /var/lib/cseries/apps/browser/ that talks to the sidecar's
        # API via fetch.
        if port is None:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"external runtime missing runtime.port: {app_id}",
            )
        _refuse_kernel_only(app_id, path)
        if _has_static(install_dir, path or "index.html"):
            return _serve_static(install_dir, path or "index.html", app_id)
        harness_id = _read_harness(install_dir)
        if harness_id:
            from server.applications import harness_backend
            host = await harness_backend.ensure_awake(app_id, harness_id)
            if host is None:
                raise _not_awake(app_id, f"{app_id} is starting — retry")
            return await _serve_external(app_id, port, path, request, strip_prefix,
                                         upstream_host=host)
        # No harness: a loopback port in the Host pod, the same exposure as an
        # in-container process app. No shipped app takes this branch (the
        # browser declares runtime.harness); a manifest that does gets
        # the same own-window gate.
        await _require_own_window(pool, app_id, request)
        return await _serve_external(app_id, port, path, request, strip_prefix)

    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        f"unknown runtime kind: {kind}",
    )


def _not_awake(app_id: str, starting: str) -> HTTPException:
    """The answer to a request whose app container did not wake. A wake this
    machine refused for a reason the owner can act on (a short disk, no room)
    answers with the refusal's own status and its words, one line of text
    (harness_backend.wake_refused); an app paused while a backup copies its
    data is 504 in the runtime's words for a wake then; anything else is
    still starting, 503."""
    from server.applications import harness_backend, local_runtime
    if local_runtime.exporting_app_data(app_id):
        return HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, local_runtime.EXPORT_PAUSED)
    why = harness_backend.wake_refused(app_id)
    if why:
        return HTTPException(why["code"], why["message"])
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, starting)


def _is_static_file(install_dir: Path, path: str) -> bool:
    """A regular file inside the install dir — never a directory, never outside."""
    target = install_dir / path
    try:
        target.resolve().relative_to(install_dir.resolve())
    except ValueError:
        return False
    return target.is_file()


def _static_target(install_dir: Path, path: str) -> Path | None:
    """install_dir/path, or its index.html when it is a directory; None when
    either resolves outside install_dir. The index.html is checked on its own:
    a directory inside the tree can hold an index.html that links out of it."""
    if static_paths.inside(install_dir, path) is None:
        return None
    target = install_dir / path
    if target.is_dir():
        if static_paths.inside(install_dir, posixpath.join(path, "index.html")) is None:
            return None
        target = target / "index.html"
    return target


def _has_static(install_dir: Path, path: str) -> bool:
    target = _static_target(install_dir, path)
    return target is not None and target.is_file()


_BUILD_TAG_CACHE: dict = {"value": None}


def _build_tag() -> str:
    """Read the current image's build tag once and cache. Used as the
    cache-buster query value on every <script>/<link> reference in
    system-app HTML so each image bump forces a fresh asset fetch."""
    v = _BUILD_TAG_CACHE["value"]
    if v is not None:
        return v
    try:
        import json as _json
        with open("/app/system/build-info.json") as f:
            data = _json.load(f)
        v = (data.get("tag") or "dev").strip() or "dev"
    except Exception:
        v = "dev"
    _BUILD_TAG_CACHE["value"] = v
    return v


_HTML_REF_RE = re.compile(
    r'(<(?:script|link)\b[^>]*?\s(?:src|href)=")(?!https?:|//|data:)([^"?#]+)([^"]*)(")',
    re.IGNORECASE,
)


def _rewrite_html_with_buster(text: str, tag: str) -> str:
    """Append ?v=<tag> to relative src= / href= on <script> and <link>.
    Skips absolute URLs and already-versioned references."""
    def sub(m):
        prefix, url, qs, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        if "v=" in qs:
            return m.group(0)  # already has a version
        # NEVER buster an ES module entry (<script type="module">) or a
        # modulepreload <link>. A module's own dynamic imports use BARE relative
        # specifiers, so a `?v=` on the entry URL makes the browser evaluate the
        # module a SECOND time (query URL vs the bare URL the lazy chunks import)
        # → two copies of React → "Invalid hook call" (React error 321) and a torn-down tree
        # the moment a lazy chunk renders (e.g. Circles' shiki code-block). The
        # Vite content-hashed filename already busts cache, so the query is both
        # redundant and breaks module identity here. Classic scripts/CSS are safe.
        low = prefix.lower()
        if (
            'type="module"' in low
            or "type='module'" in low
            or 'rel="modulepreload"' in low
            or "rel='modulepreload'" in low
        ):
            return m.group(0)
        sep = "&" if qs.startswith("?") else "?"
        return f"{prefix}{url}{qs}{sep}v={tag}{suffix}"
    return _HTML_REF_RE.sub(sub, text)


_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)


def _inject_app_sdk(text: str) -> str:
    """Inject the C-Series App SDK as the FIRST element in <head>, for an
    SDK-MANAGED app (is_sdk_managed — NOT the same set as is_isolated). The SDK
    receives the per-window token over postMessage from the shell and stamps it
    onto API calls; that postMessage lane is the only one that works for an
    isolated app (the shell can't reach a cross-origin iframe) and works
    identically for a same-origin one. The SDK is NON-BLOCKING — it never holds
    a fetch or gates first paint. Served at /js/app-sdk.js (the same pod answers
    any Host). Idempotent.

    The script carries Subresource Integrity (`integrity="sha384-…"`) from the
    host's INSTALLED, Privatae-signed SDK (sdk_store), so the browser refuses to
    run a tampered runtime. If no verified SDK is installed this FAILS CLOSED —
    it injects nothing rather than loading an unverifiable runtime (the app then
    serves un-isolated, which the trust model already allows for trusted apps)."""
    if "/js/app-sdk.js" in text:
        return text
    try:
        from server.applications import sdk_store
        sdk = sdk_store.current()
    except Exception as e:
        log.error("app-sdk inject SKIPPED — no verified SDK installed: %s", e)
        return text
    # No ?v= buster: the /js/app-sdk.js route sends Cache-Control: no-store, and
    # the SRI hash changes whenever the SDK changes, so the browser re-fetches on
    # any SDK rev anyway. Keeping the src bare avoids a double ?v= from the HTML
    # cache-buster rewrite that runs after injection.
    tag_html = (
        f'<script src="/js/app-sdk.js" '
        f'integrity="{sdk["sri"]}" crossorigin="anonymous"></script>'
    )
    m = _HEAD_OPEN_RE.search(text)
    if m:
        return text[: m.end()] + tag_html + text[m.end():]
    return tag_html + text


_SDK_TAG_RE = re.compile(r'<script src="/js/app-sdk\.js"[^>]*></script>')


def _inject_frame_init(text: str, app_id: str) -> str:
    """Add the isolated-app frame bootstrap (dashboard/base/js/app-frame-init.js)
    right after the SDK tag — or first in <head> when no SDK was injected
    — so the SDK's listener exists before the token is raised. A same-origin
    file, served on an app origin from the image only (OverlayStaticFiles), so
    it fits a strict community CSP's script-src 'self'. Idempotent."""
    if "/js/app-frame-init.js" in text:
        return text
    safe_id = re.sub(r"[^a-z0-9-]", "", app_id)
    tag_html = f'<script src="/js/app-frame-init.js" data-app-id="{safe_id}"></script>'
    m = _SDK_TAG_RE.search(text)
    if m:
        return text[: m.end()] + tag_html + text[m.end():]
    m = _HEAD_OPEN_RE.search(text)
    if m:
        return text[: m.end()] + tag_html + text[m.end():]
    return tag_html + text


# Parsed manifests, keyed by the file's (inode, ctime, mtime, size). _app_meta runs on every
# hashed-asset request (_asset_cache_control), and yaml.safe_load of a
# long-changelog app.yaml measured 9 ms on one machine and +32 ms per request
# on a live Host — all on the event loop. Measured on a rollout: first open
# of the Workbench was ~100 ms slower than the release before, until this cache.
#
# The HTML path reads provenance from here too, and provenance decides the CSP
# exfil wall — a stale "standard" for an app that became "community" would serve
# it without the wall. So the key includes ctime and the inode, not just mtime:
# metadata-preserving copies (cp -p, tar, rsync -a, shutil.copy2) can restore an
# old mtime, but any write or rename sets a new ctime or inode, and neither can
# be set from userspace.
_APP_META_CACHE: dict[Path, tuple[tuple[int, int, int, int], tuple[str, list[str], bool]]] = {}


def _app_meta(install_dir: Path) -> tuple[str, list[str], bool]:
    """Cached _read_app_meta — re-read whenever app.yaml is written, replaced or touched."""
    try:
        st = (install_dir / "app.yaml").stat()
        key = (st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size)
    except OSError:
        return _read_app_meta(install_dir)   # missing file: fail closed, uncached
    hit = _APP_META_CACHE.get(install_dir)
    if hit is None or hit[0] != key:
        hit = (key, _read_app_meta(install_dir))
        _APP_META_CACHE[install_dir] = hit
    prov, net, strict = hit[1]
    return prov, list(net), strict


def _read_app_meta(install_dir: Path) -> tuple[str, list[str], bool]:
    """(provenance, network, strict_inline) from the installed manifest — for
    serve-time isolation + CSP. The installer writes the manifest as `app.yaml`
    (reading `manifest.yaml`, the bundle-internal name, found nothing → silently
    disabled the CSP wall for every real app). Reads `provenance` with a legacy
    `tier` fallback (the field's older name). `strict_inline` is the optional
    `csp_strict_inline` opt-in. FAILS CLOSED: an unreadable/unparseable
    manifest is treated as the most restrictive (community → isolated + CSP), but
    NOT strict (strict on an unreadable app could break it; isolation is the wall)."""
    try:
        m = yaml.safe_load((install_dir / "app.yaml").read_text(encoding="utf-8")) or {}
        net = [str(h).strip() for h in (m.get("network") or []) if str(h).strip()]
        prov = str(m.get("provenance") or m.get("tier") or "standard")
        if prov == "premium":
            prov = "standard"
        return prov, net, bool(m.get("csp_strict_inline"))
    except Exception:
        return "community", [], False   # fail closed — isolate + CSP-wall an unreadable app


# Inline-script CSP hardening (C1). A community app opts into a strict script-src
# by declaring `csp_strict_inline: true` in its (signature-covered) manifest. When
# it does AND its served HTML uses no un-hashable inline event handlers /
# javascript: URIs, script-src is pinned to the sha256 hashes of the app's OWN
# inline scripts with NO 'unsafe-inline' — so an injected (XSS) inline script that
# has no matching hash cannot execute. Apps that don't opt in are unchanged, so
# this never breaks an existing app; it is a per-author hardening path.
_INLINE_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_SRC_ATTR_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)
_INLINE_HANDLER_RE = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)
_JS_URI_RE = re.compile(r"\b(?:href|src|action|formaction)\s*=\s*['\"]?\s*javascript:", re.IGNORECASE)


def _inline_script_hashes(html: str) -> tuple[list[str], bool]:
    """Return (sha256-b64 CSP source-expressions for the inline <script> blocks,
    uninlineable). `uninlineable` is True when the HTML uses inline event handlers
    (onclick=…) or javascript: URIs — neither can be expressed as a script-src
    hash, so the page can't go strict without breaking and the caller must keep
    'unsafe-inline'."""
    if _INLINE_HANDLER_RE.search(html) or _JS_URI_RE.search(html):
        return [], True
    hashes: list[str] = []
    for attrs, body in _INLINE_SCRIPT_RE.findall(html):
        if _SRC_ATTR_RE.search(attrs):
            continue  # external script — governed by 'self'/host + SRI, not a hash
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        hashes.append("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return hashes, False


def _app_csp(network: list[str], script_hashes: list[str] | None = None) -> str:
    """Per-app Content-Security-Policy — the runtime wall for an untrusted (published)
    app. connect-src (fetch/XHR/WebSocket/EventSource) and img-src are pinned to the
    app's own origin plus ONLY the hosts it declared in manifest.network; form-action is
    pinned to 'self' alone (an app submits data via its own /api with fetch, never a
    cross-origin form POST — so form-action carries no legitimate need and only a
    navigation-exfil risk). The browser blocks everything else. Code may run (inline +
    named CDN libs) but it is contained: it can't phone home past the declared hosts, and
    SOP + frame-ancestors keep it out of the desktop shell. object-src/base-uri locked
    down. When the app opted into strict inline (script_hashes is not None) script-src
    is pinned to 'self' + the app's own inline-script hashes with NO 'unsafe-inline',
    so injected inline script can't run; otherwise 'unsafe-inline' is kept (connect-src
    + SOP remain the load-bearing exfil wall regardless). Both forms carry
    'wasm-unsafe-eval': without it the browser refuses to compile WebAssembly, so a
    wasm build (PhotoCraft's web app, any Rust or C++ app) opens to a blank window. It
    lets a script that is already allowed to run compile wasm, which runs under the
    same connect-src; it allows no JS eval, unlike 'unsafe-eval'."""
    ext = " ".join(
        "https://" + h for h in network
        if h and "://" not in h and "/" not in h and "*" not in h and " " not in h
    )
    own = "'self'" + ((" " + ext) if ext else "")
    cdn = "https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com"
    if script_hashes is not None:
        # Strict opt-in: pin inline scripts to their hashes, drop 'unsafe-inline'.
        # An empty list means the page ships no inline script → fully strict.
        inline = (" ".join(script_hashes) + " ") if script_hashes else ""
        script_src = f"script-src 'self' 'wasm-unsafe-eval' {inline}{cdn}"
    else:
        script_src = f"script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' {cdn}"
    return (
        "default-src 'self'; "
        f"{script_src}; "
        f"style-src 'self' 'unsafe-inline' {cdn} https://fonts.googleapis.com; "
        f"img-src {own} data:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        f"connect-src {own}; "
        "form-action 'self'; "
        "object-src 'none'; base-uri 'none'; "
        f"frame-ancestors {host_names.frame_ancestors()}"
    )


# ── Who gets what: TWO INDEPENDENT decisions ────────────────────────
#
# These were one flag, and that single conflation is what broke every
# Workbench-built app: `provenance: custom` was in neither branch, so an app
# built exactly to the SDK build contract was served with no `window.CSeriesApp`
# at all — `await CSeriesApp.ready` threw ReferenceError on its first line and
# CSeriesApp.storage, the only durable per-app store the contract offers, did not
# exist. They are two questions and they are kept apart here:
#
#   is_sdk_managed — the app gets `window.CSeriesApp` injected into its served
#       HTML AND the shell runs the `cseries:app-init` token handshake with it
#       (dashboard/base/js/app-runner.js). The two ALWAYS travel together: an
#       injected SDK with no handshake never resolves `CSeriesApp.ready`, and
#       every CSeriesApp.storage call 401s regardless of APP_TOKEN_MODE, because
#       the per-app config routes gate on the token directly (_require_self,
#       server/routes/applications.py) rather than through the middleware's
#       audit/strict mode. This is what the build contract promises every app
#       author (server/applications/build_contract.py).
#
#   is_isolated — the app is ALSO served from its OWN origin,
#       `apps-<key>-<handle>` (applications/app_origin.py), so the
#       browser's same-origin policy walls it off from the desktop shell and
#       from every other app, plus (community only) the per-app CSP exfil wall.
#
# A Workbench-built app declares `provenance: custom` (enforced by the factory
# ship gate in server/routes/applications.py) and is SDK-MANAGED but NOT
# origin-isolated. That is a deliberate choice, not an omission:
#
#   * The contract promises the SDK, not an origin. Injecting the SDK is what
#     makes an app behave as documented; moving its origin is a separate
#     decision that needs per-app evidence.
#   * An origin move is not a no-op for an ALREADY-INSTALLED app. localStorage
#     is per-origin, and so is the router's session cookie that the shell's
#     session-keeper renews on a 5-minute timer — the keeper runs on the shell
#     origin only and says so (dashboard/base/js/session-keeper.js). The
#     SDK_MANAGED_APPS roster was only ever grown one app at a time, after
#     verifying against the INSTALLED app files that the app kept no
#     localStorage state (see the comments on that frozenset in kernel.py).
#     That verification cannot be performed for custom apps: they are built and
#     installed per entity, on per-entity pod disks, and are in no repo here.
#     The same standard therefore says do not move them.
#   * Custom apps are the entity's OWN code, entity-signed. The origin wall
#     exists for third-party code (`community`), which stays isolated.
#
# To give a further app the SDK + token WITHOUT moving its origin (the safe
# order for any first-party app that is framed same-origin today: get it a
# scoped token first, gate its routes second), widen is_sdk_managed alone and
# leave is_isolated untouched.
def is_isolated(app_id: str, provenance: str) -> bool:
    """Is this app served cross-origin from its own `apps-<key>-<handle>`
    origin, and, for a community app, behind the per-app CSP wall?"""
    return app_id in SDK_MANAGED_APPS or provenance == "community"


def is_sdk_managed(app_id: str, provenance: str) -> bool:
    """Does this app get `window.CSeriesApp` injected + the shell's token
    handshake? Every isolated app, PLUS entity-built (`custom`) apps, which
    stay same-origin. Widening this does NOT move any app's origin."""
    return is_isolated(app_id, provenance) or provenance == "custom"


def _serve_static(install_dir: Path, path: str, app_id: str = "") -> Response:
    target = _static_target(install_dir, path)
    if target is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "path escapes install dir")
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such file: {path}")

    # HTML gets rewritten + no-store, so every open reads the current build's
    # file names. Content-hashed build assets are then cached for good (see
    # _asset_cache_control); everything else stays no-store.
    if target.suffix.lower() in (".html", ".htm"):
        provenance, network, strict_inline = _app_meta(install_dir)
        # SDK injection follows is_sdk_managed, NOT is_isolated (see above): every
        # isolated app plus entity-built (custom) apps, which stay same-origin.
        # Community (published, untrusted) apps are ALWAYS both — SDK-injected and
        # served with the per-app CSP exfil wall — regardless of the managed
        # roster. First-party isolated apps stay on the kernel roster.
        try:
            text = target.read_text(encoding="utf-8")
            tag = _build_tag()
            if is_sdk_managed(app_id, provenance):
                text = _inject_app_sdk(text)
            if is_isolated(app_id, provenance):
                # The frame fetches its own window token on its own origin and
                # keeps that origin's session alive (app-frame-init.js).
                text = _inject_frame_init(text, app_id)
            text = _rewrite_html_with_buster(text, tag)
        except OSError as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"read failed: {e}")
        headers = {"Cache-Control": "no-store, max-age=0"}
        if provenance == "community":
            # C1 — if the app opted into strict inline AND its HTML has no
            # un-hashable inline handlers, pin script-src to its inline-script
            # hashes (computed over the FINAL served bytes) and drop 'unsafe-inline'.
            script_hashes = None
            if strict_inline:
                hashes, uninlineable = _inline_script_hashes(text)
                if not uninlineable:
                    script_hashes = hashes
            headers["Content-Security-Policy"] = _app_csp(network, script_hashes=script_hashes)
        return Response(
            content=text,
            media_type="text/html; charset=utf-8",
            headers=headers,
        )
    headers = {"Cache-Control": _asset_cache_control(path, install_dir)}
    provenance, network, strict_inline = _app_meta(install_dir)
    if provenance == "community":
        # The wall must ride every file, not only the page. A worker runs under
        # the CSP of its OWN script's response, not the page's: a store app's
        # `new Worker('w.js')`, served with no policy here, fetched any host past
        # manifest.network (measured in Chromium). Script, style and
        # image loads ignore a CSP header, so this changes only what a browser
        # runs as its own context (a worker, or an SVG opened directly). A
        # worker has no inline script to hash, so a strict-inline app's files
        # get the fully strict form.
        headers["Content-Security-Policy"] = _app_csp(
            network, script_hashes=[] if strict_inline else None)
    return FileResponse(str(target), headers=headers)


# Vite names every emitted asset <name>-<8-char content hash>.<ext>. The hash
# covers the bytes, so a changed file is a new URL and a given URL never
# changes — the browser can keep it. Measured before this: the
# Workbench's 3.2 MB main chunk was re-downloaded on every open of the window.
#
# The hash is 8 base64url characters and must contain an upper-case letter or
# a digit: a hyphenated word ("app-settings.js", "main-controls.js") is a name,
# not a hash, and must not be cached forever. A real hash with neither (about
# 0.07% of them) just stays no-store — safe, only uncached.
_HASHED_ASSET_RE = re.compile(
    r"^assets/[^/]+-(?=[A-Za-z0-9_-]{0,7}[A-Z0-9])[A-Za-z0-9_-]{8}\.(?:js|css|wasm|woff2?)$")
_IMMUTABLE = "private, max-age=31536000, immutable"
_NO_STORE = "no-store, max-age=0"


def _asset_cache_control(path: str, install_dir: Path) -> str:
    """Cache header for a non-HTML file of an installed app.

    Immutable only for a Vite-hashed file under assets/ of a FIRST-PARTY app
    (provenance native/standard — baked into the image, built by Vite in this
    repo). Host-built (custom) apps have no build step: their files are written
    by hand and edited in place on every re-ship (build_contract.py: "there is
    no build step"), so a name never tracks its bytes. Community apps are
    third-party. Both stay no-store. `private` keeps shared caches out — these
    are served behind the Host's own login."""
    if _HASHED_ASSET_RE.match(path) and _app_meta(install_dir)[0] in IMAGE_BAKED_PROVENANCES:
        return _IMMUTABLE
    return _NO_STORE


async def _serve_external(
    app_id: str, port: int, path: str, request: Request,
    strip_prefix: bool = True, upstream_host: str | None = None,
) -> Response:
    """HTTP forward to a k8s pod-level sidecar listening on localhost:port.

    No supervisor involvement — the sidecar's lifecycle is managed by
    kubernetes via the pod spec. Shared netns means localhost:port
    reaches the sidecar container directly.

    strip_prefix (default True) strips /system-apps/<id>/ before forwarding
    (sidecar serves at root); False forwards the full path so a sidecar with an
    absolute base-path resolves its own asset URLs.

    Mirrors _serve_process's body shape (aiohttp streaming) but skips
    the alive-check + spawn-on-demand flow."""
    fwd = path if strip_prefix else f"system-apps/{app_id}/{path}"
    if upstream_host is None:
        # The loopback branch is a sidecar IN the Host pod; see
        # _HARNESS_ONLY_SIDECAR_PATHS. Harness- and pod-backed upstreams pass
        # upstream_host and are not the Host's filesystem. Checked on `fwd`,
        # what is actually sent: with strip_prefix off, dot segments in
        # `path` can climb out of the system-apps/<id>/ prefix upstream.
        _refuse_harness_only(fwd, request.url.query)
    # harness-backed apps pass upstream_host (the harness Service DNS); in-pod
    # sidecars fall back to loopback. Never hardcode a bare IP for the harness path.
    upstream_url = f"http://{upstream_host or f'127.0.0.1:{port}'}/{fwd}"
    if request.url.query:
        upstream_url += "?" + request.url.query

    body = await _request_body(request)
    headers = _filter_headers(request.headers.items())
    headers.update(_upstream_auth(app_id))
    if upstream_host is not None and port:
        # Harness-backed app: present a localhost Host. Added for the VS Code
        # editor (since removed), whose openvscode-server rejected (403) any
        # request whose Host wasn't localhost/127.0.0.1 (DNS-rebinding
        # protection); every harness upstream has been sent it since. (_HOP_HEADERS already strips the client Host, so
        # aiohttp would otherwise derive Host from the pod-IP URL and get 403'd.)
        # Helper pods pass port=0: they check no Host, and a "localhost:0"
        # stamp would be an invented header — aiohttp derives Host from the
        # upstream URL instead.
        headers["Host"] = f"localhost:{port}"

    try:
        # sock_connect bounds the DIAL. Without it a blackholed pod IP — a
        # scale-to-zero app pod whose cached IP went stale — burns the entire
        # total= budget before anything notices. Measured on a live Host:
        # 60s per request, 300 times in 24h,
        # while the app itself was healthy throughout. A dial that fails inside
        # this budget raises ConnectionTimeoutError, which IS an
        # aiohttp.ClientError, so it lands on the 502 below in ~5s instead.
        #
        # The wait is on the HEAD only (_HEAD_TIMEOUT_S, asyncio.wait_for).
        # A body that is still streaming is never cut: total=60 used to keep
        # running after the head arrived, so any download, SSE stream or
        # long-poll from the user's own app was truncated at 60 s with no
        # error to anyone (measured with aiohttp 3.14.3).
        # The body ends when the upstream ends it or the client goes away.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5)
        session = _upstream_session(timeout)
        try:
            upstream = await asyncio.wait_for(session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=body if body else None,
                allow_redirects=False,
            ), _HEAD_TIMEOUT_S)
        # ORDER MATTERS, and the split is about REPLAY SAFETY, not tidiness.
        # serve_app treats 502 as "dead upstream" and REPLAYS the request
        # (:263-270 Form-2 lane, :301-310 helper lane) — correct ONLY when the
        # upstream never received it. Dial-phase failures are exactly that, and
        # ConnectionTimeoutError/ServerTimeoutError subclass ClientError, so
        # they land here and may safely replay.
        # DO NOT add sock_read= to the timeout above: it raises
        # SocketTimeoutError (also a ClientError) for a request the upstream
        # DID receive, which would make the replay unsafe.
        except aiohttp.ClientError as e:
            await session.close()
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"external sidecar {app_id} unreachable ({upstream_host or port}): {e}",
            )
        # The head wait (asyncio.wait_for) raises a BARE TimeoutError — NOT an
        # aiohttp.ClientError. It means the upstream ACCEPTED the request and
        # then took too long to answer, so it may already have run it:
        # replaying a POST here would execute it twice. 504 is both
        # accurate and un-replayable — serve_app re-raises anything that isn't
        # 502 (:263-264). Before this clause the bare timeout escaped every
        # handler and surfaced as an unexplained 500.
        except asyncio.TimeoutError:
            await session.close()
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                f"{app_id} did not start answering within {int(_HEAD_TIMEOUT_S)}s",
            )
    except HTTPException:
        raise

    async def stream():
        try:
            async for chunk in upstream.content.iter_chunked(8192):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    resp_headers = _filter_headers(upstream.headers.items())
    _wall_upstream(resp_headers, app_id)
    return StreamingResponse(
        stream(),
        status_code=upstream.status,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


_UPSTREAM_CSP = {"content-security-policy", "content-security-policy-report-only"}
# Headers that name an address the BROWSER sends reports to on its own, past
# connect-src: Network Error Logging and the Reporting API. A store app's pod
# could point them at any host.
_UPSTREAM_REPORTING = {"nel", "report-to", "reporting-endpoints"}


def _wall_upstream(headers: dict[str, str], app_id: str) -> None:
    """A store app's answer from its OWN pod carries the same wall as its files
    on disk (_serve_static). A store app may run a process in its own pod
    (installer.py, the community runtime gate), and every path with no file on
    disk is that pod's to answer, headers included: with no policy here, a page
    or worker script the pod served ran past manifest.network. The pod's own
    policy, enforcing or report-only, is replaced, never kept: a report-only
    policy's report-uri is a request the wall does not govern. The page form
    keeps 'unsafe-inline' for a strict-inline app, as _serve_static does for a
    page it cannot hash: a streamed body is never read here."""
    provenance, network, _strict = _app_meta(INSTALL_ROOT / app_id)
    if provenance != "community":
        return
    for k in [k for k in headers if k.lower() in _UPSTREAM_CSP | _UPSTREAM_REPORTING]:
        del headers[k]
    headers["Content-Security-Policy"] = _app_csp(network)


# How long an app may take to START answering (its status line and headers).
# Past it the person gets a 504 that says so. Once the head has arrived the
# body streams for as long as the app keeps sending it.
_HEAD_TIMEOUT_S = 60.0

# A streaming body has no clock, so a DEAD upstream — a pod that went
# away without a FIN, a node that dropped off the network — is noticed by TCP
# keepalive on the upstream socket instead: after _KEEPALIVE_IDLE_S of silence
# the kernel probes every _KEEPALIVE_INTVL_S and gives up after _KEEPALIVE_CNT
# unanswered probes (~3 minutes), the read fails and the stream ends. A quiet
# but ALIVE app answers the probes in its TCP stack without sending a byte, so
# its stream is never cut — which a sock_read idle timeout would do.
_KEEPALIVE_IDLE_S = 60
_KEEPALIVE_INTVL_S = 15
_KEEPALIVE_CNT = 8


def _keepalive_socket(addr_info) -> socket.socket:
    """The socket factory for upstream dials: TCP keepalive on, with the
    probe timings above where the platform offers them (Linux does)."""
    family, type_, proto, _canon, _addr = addr_info
    sock = socket.socket(family=family, type=type_, proto=proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", _KEEPALIVE_IDLE_S),
                            ("TCP_KEEPINTVL", _KEEPALIVE_INTVL_S),
                            ("TCP_KEEPCNT", _KEEPALIVE_CNT)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError as e:     # a platform without the option: the dial still works
        log.debug("upstream keepalive not set: %s", e)
    return sock


def _upstream_session(timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
    """One proxied request's session: its dials carry TCP keepalive
    (_keepalive_socket). Closing the session closes its connector."""
    return aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(socket_factory=lambda ai: _keepalive_socket(ai)))


async def _serve_process(app_id: str, path: str, request: Request) -> Response:
    # An in-container process app runs with the kernel's filesystem; the
    # harness control paths never reach it (_HARNESS_ONLY_SIDECAR_PATHS).
    # First, before the supervisor is asked, so a refused request cannot
    # start or touch a sidecar. Same path and query as upstream_url below.
    _refuse_harness_only(path, request.url.query)
    supervisor = get_supervisor()
    if supervisor is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "supervisor unavailable"
        )

    sidecar = supervisor.get(app_id)
    if sidecar is None or not sidecar.is_alive():
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"no live sidecar for {app_id} — open a window via /api/applications/{app_id}/launch first",
        )

    upstream_url = f"{sidecar.base_url}/{path}"
    if request.url.query:
        upstream_url += "?" + request.url.query

    body = await _request_body(request)
    headers = _filter_headers(request.headers.items())

    try:
        # Same two-clause split as _serve_external above, same reasons — and
        # the same head-only wait: a streaming body is never cut.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5)
        session = _upstream_session(timeout)
        try:
            upstream = await asyncio.wait_for(session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=body if body else None,
                allow_redirects=False,
            ), _HEAD_TIMEOUT_S)
        except aiohttp.ClientError as e:
            await session.close()
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"sidecar {app_id} unreachable: {e}",
            )
        # A loopback sidecar that ACCEPTS the connection and then stalls is
        # slow, not unreachable — saying "unreachable" here would be a lie, and
        # a 502 would replay a request the sidecar already has.
        except asyncio.TimeoutError:
            await session.close()
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                f"sidecar {app_id} did not start answering within {int(_HEAD_TIMEOUT_S)}s",
            )
    except HTTPException:
        raise

    async def stream():
        try:
            async for chunk in upstream.content.iter_chunked(8192):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    resp_headers = _filter_headers(upstream.headers.items())
    return StreamingResponse(
        stream(),
        status_code=upstream.status,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ── WebSocket proxy for kind=external apps ──────────────────────────
#
# noVNC's websockify endpoint streams over WebSocket. The cseries kernel
# fronts every system-app under /system-apps/<id>/* via the HTTP
# `serve_app` route above, but FastAPI splits WS routes from HTTP
# routes — they need their own decorator. This route catches WS
# upgrade requests for the same path space and relays them, frame by
# frame, to the sidecar.
#
# kind=static and kind=process apps don't get WS — the upstream
# doesn't terminate WS in any current use case. If a process-kind app
# ever needs WS, extending the branch is one if-clause.


@router.websocket("/system-apps/{app_id}/{path:path}")
async def serve_app_ws(websocket: WebSocket, app_id: str, path: str) -> None:
    pool = brain_service.get_pool()
    if pool is None:
        await websocket.close(code=1011, reason="brain pool down")
        return

    app = await registry.get(pool, app_id)
    if app is None:
        await websocket.close(code=1008, reason=f"not installed: {app_id}")
        return
    if app.status != "active":
        await websocket.close(code=1008, reason=f"not active: {app.status}")
        return
    # The same decision serve_app makes. A socket is never an image.
    from server.applications import app_origin
    if _files_here(app_id, app, path, websocket) != app_origin.SERVE:
        await websocket.close(code=1008, reason="not served on this address")
        return

    install_dir = INSTALL_ROOT / app_id
    if not install_dir.exists():
        await websocket.close(code=1011, reason="install dir missing")
        return

    kind, port, strip_prefix = _read_runtime(install_dir)
    if kind != "external" or port is None:
        await websocket.close(code=1008, reason="ws only supported for kind=external")
        return

    # Harness-backed app: opening the WS is a live session — wake on demand and
    # proxy to the harness Service (not loopback); the WS lifetime = the open window.
    harness_id = _read_harness(install_dir)
    ws_host = None
    # Imported unconditionally: the relay below stamps activity on every client
    # frame, including on the loopback path where harness_id is None. Binding it
    # only inside the branch left the relay referencing a name that did not
    # exist there. touch_activity is a no-op for an app with no state.
    from server.applications import harness_backend
    if harness_id:
        ws_host = await harness_backend.session_opened(app_id, harness_id)
        # Opening counts as use, so a session is never idle before its first frame.
        harness_backend.touch_activity(app_id)
        if ws_host is None:
            why = harness_backend.wake_refused(app_id)
            if why:
                # A close reason holds 123 bytes and the refusal names each
                # app's data, so it is one error frame (the viewport shows
                # its detail as text), then the close.
                import json as _json
                await websocket.accept()
                await websocket.send_text(_json.dumps(
                    {"t": "error", "detail": why["message"], "refused": why["code"]}))
                await websocket.close(code=1013, reason="refused by this machine")
                return
            await websocket.close(code=1011, reason="harness starting")
            return
    else:
        # Loopback: a port in the Host pod, the WS twin of serve_app's
        # no-harness external branch, and gated the same way. A browser's
        # WebSocket cannot set request headers, so a browser page is refused
        # here; no shipped app opens one (the browser is harness-backed).
        try:
            await _require_own_window(pool, app_id, websocket)
        except HTTPException as e:
            await websocket.close(code=1008, reason=str(e.detail)[:120])
            return

    qs = ""
    if websocket.scope.get("query_string"):
        qs = "?" + websocket.scope["query_string"].decode("latin-1")
    fwd = path if strip_prefix else f"system-apps/{app_id}/{path}"
    if not harness_id and _names_harness_route(fwd, qs[1:]):
        # The loopback lane dials a sidecar IN the Host pod: the same wall as
        # the HTTP forwarders (_HARNESS_ONLY_SIDECAR_PATHS), before any dial.
        # (Not exploitable today: a WS handshake is a GET and /workspace/load
        # is POST-only; closed so "refused before it dials" is true here too.)
        await websocket.close(code=1008, reason="not found")
        return
    upstream_url = f"ws://{ws_host or f'127.0.0.1:{port}'}/{fwd}{qs}"

    requested_protocols = [
        h[1].decode("latin-1")
        for h in websocket.scope.get("headers", [])
        if h[0] == b"sec-websocket-protocol"
    ]
    protocols: list[str] = []
    for v in requested_protocols:
        protocols.extend([p.strip() for p in v.split(",") if p.strip()])

    await websocket.accept(
        subprotocol=protocols[0] if protocols else None,
    )

    # Harness-backed app: present a localhost Host + Origin on the upstream WS
    # handshake, the same as the HTTP path (added for the removed VS Code
    # editor's DNS-rebinding / cross-site checks).
    ws_headers: dict[str, str] = {}
    if harness_id and ws_host:
        ws_headers = {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}
    # Upstream bearer (the browser control token): the HTTP path injects it in
    # _serve_external; the sidecar's viewport WS enforces the same token, so the
    # WS handshake has to carry it too or the viewport 1008s the moment the
    # token is set (i.e., in every harness deployment).
    ws_headers.update(_upstream_auth(app_id))
    ws_headers = ws_headers or None

    session = aiohttp.ClientSession()
    try:
        try:
            upstream = await session.ws_connect(
                upstream_url,
                protocols=protocols or None,
                heartbeat=30,
                max_msg_size=0,
                headers=ws_headers,
            )
        except (aiohttp.ClientError, aiohttp.WSServerHandshakeError) as e:
            await session.close()
            log.warning("ws upstream %s unreachable: %s", upstream_url, e)
            # REPAIR THE STALE UPSTREAM. ensure_awake returns the cached url
            # with no liveness check, and the only other thing that clears it
            # is session_closed's delayed release — which every reconnect
            # CANCELS on its way through ensure_awake. So without this an
            # auto-reconnecting viewport loops on a dead Service forever, and a
            # manual reload does not help either: the shell is static-served
            # and opens a fresh WS into the same stale url. browser_agents has
            # had the right shape all along (forget on refusal -> cold re-wake);
            # this mirrors it on the WS lane, which is what makes "the next
            # client frame wakes it cold" actually true.
            if harness_id:
                harness_backend.forget_upstream(app_id)
            try:
                await websocket.close(code=1011, reason="upstream unreachable")
            except Exception:
                pass
            return

        async def client_to_upstream():
            try:
                while True:
                    msg = await websocket.receive()
                    t = msg.get("type")
                    if t == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        harness_backend.touch_activity(app_id)
                        await upstream.send_str(msg["text"])
                    elif msg.get("bytes") is not None:
                        harness_backend.touch_activity(app_id)
                        await upstream.send_bytes(msg["bytes"])
            except WebSocketDisconnect:
                pass
            except Exception as e:
                log.debug("client->upstream relay ended: %s", e)
            finally:
                try:
                    await upstream.close()
                except Exception:
                    pass

        async def upstream_to_client():
            try:
                async for msg in upstream:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await websocket.send_text(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await websocket.send_bytes(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            except Exception as e:
                log.debug("upstream->client relay ended: %s", e)
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass

        await asyncio.gather(
            client_to_upstream(),
            upstream_to_client(),
            return_exceptions=True,
        )
    finally:
        try:
            await session.close()
        except Exception:
            pass
        # Window closed -> schedule the harness release (grace absorbs refresh).
        if harness_id:
            from server.applications import harness_backend
            harness_backend.session_closed(app_id, harness_id)
