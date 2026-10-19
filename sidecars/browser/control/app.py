"""cseries-browser sidecar — FastAPI control API.

First-cut scope: /health + /screenshot only. Everything else (session
lifecycle, FSA polyfill bridge, cookie auth) ships in later iterations
of this sidecar.

Lifecycle:
- App startup launches one persistent Playwright Chromium instance
  attached to the Xvfb display (started by entrypoint.sh before us).
- A single shared browser context + page is held by the module. The
  page navigates to about:blank initially; /screenshot captures
  whatever is currently displayed.
- App shutdown closes the page + context + browser cleanly.

The first-cut surface is intentionally narrow — enough to prove the
image boots, Chromium runs, and the control API answers. The live
viewport (control/viewport.py) streams the page itself over CDP
screencast on this same port — the noVNC/x11vnc/websockify desktop
stream it replaced is gone.

The multi-context lane this docstring once deferred now lives in
control/sessions.py, mounted at /v1/sessions. It is ADDITIVE: the
shared page above is unchanged, and a session is deliberately not it —
own context, own storage_state, no FS bindings, navigation pinned to
hosts the caller declared. See that module's docstring for why each of
those three differences exists.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from control import (cookies, document_verify, formcapture, fs_api, recorder,
                     sessions, verify, viewport)

log = logging.getLogger("cseries-browser.control")

CHROMIUM_PATH = "/usr/bin/chromium"
DISPLAY = os.environ.get("DISPLAY", ":99")
POLYFILL_PATH = Path("/app/polyfill/fsa-bridge.js")
POLYFILL_TEST_PATH = Path("/app/polyfill/fsa-bridge.test.html")


def _bypass_list() -> list[str]:
    """NO_PROXY translated for Chromium. The env is comma-separated and Chromium's
    bypass list is semicolon-separated, so it is translated rather than passed
    through. Loopback is always bypassed — the sidecar serves its own test page on
    127.0.0.1 and must not proxy to itself."""
    no_proxy = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
    bypass = [p.strip() for p in no_proxy.split(",") if p.strip()]
    for always in ("localhost", "127.0.0.1", "::1"):
        if always not in bypass:
            bypass.append(always)
    return bypass


def _proxy_config() -> Optional[dict]:
    """Where Chromium sends this browser's traffic, as Playwright's `proxy=` argument.

    TWO LANES, and which one is chosen is the whole point of the feature:

    * `NAC_EGRESS_LANE` (v127) — the kernel's egress app lane. Traffic through it is
      matched against the user's SIGNED egress rules under the subject we authenticate
      as, so a rule pinning `harness:browser` to one of the user's own machines routes
      these pages out that machine's connection. This is what makes a bank see the
      user's home address instead of a datacentre one, which is the entire reason the
      Browser could not log in to some sites.
    * else `HTTPS_PROXY`/`HTTP_PROXY` — the node squid, as before. A plain forward
      proxy: the page leaves from the NODE's own public address, shared with every
      other entity on that node.

    WHY A PLAYWRIGHT ARGUMENT AND NOT JUST THE ENV: Chromium on Linux does not reliably
    follow the proxy env vars — and it has no flag at all for a proxy CREDENTIAL. It
    answers a `407 Proxy-Authentication-Required` with Basic and nothing else, so the
    lane's per-subject token has to ride Playwright's username/password, which is what
    fills that challenge. Relying on the env alone is the bad failure: traffic still
    flows, it just leaves by the pod's default route instead of the user's, and nothing
    looks broken.

    The subject is `harness:<PRIVATAE_APP_ID or 'browser'>` and the token is minted by the
    kernel (harness_backend.egress_subject_token) and injected by the operator. No
    token means no lane: we fall back to squid rather than dial a lane that will only
    answer 407, because a browser that cannot load anything is a worse failure than one
    that loads from the wrong address and says so in the log.
    """
    lane = (os.environ.get("NAC_EGRESS_LANE") or "").strip()
    token = (os.environ.get("NAC_EGRESS_SUBJECT_TOKEN") or "").strip()
    subject = (os.environ.get("NAC_EGRESS_SUBJECT") or "").strip() or \
        ("harness:" + (os.environ.get("PRIVATAE_APP_ID") or "browser"))
    if lane and token:
        log.info("chromium egress: NAC app lane %s as %s (bypass %d entries) — "
                 "the user's signed egress rules choose the way out", lane, subject,
                 len(_bypass_list()))
        return {"server": lane, "username": subject, "password": token,
                "bypass": ",".join(_bypass_list())}
    if lane and not token:
        log.warning("NAC_EGRESS_LANE is set but NAC_EGRESS_SUBJECT_TOKEN is not — the "
                    "lane would answer 407 for every request, so falling back to the "
                    "node proxy. Pinned egress rules will NOT apply to this browser.")
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    if not proxy:
        log.info("no proxy in env — chromium will use the pod's default route")
        return None
    log.info("chromium egress: node proxy %s (bypass %d entries) — pages leave from "
             "the NODE's public address", proxy, len(_bypass_list()))
    return {"server": proxy, "bypass": ",".join(_bypass_list())}


class _State:
    """Holds the singleton Playwright + browser + context + page.

    Held in a class rather than module globals so the lifespan handler
    has one place to mutate state and tests can introspect."""

    playwright: Optional[object] = None  # async_playwright instance
    # True when the live context was built from a restored profile. Reported
    # by /health so the kernel can tell "signed in" from "clean" without
    # reading any cookie.
    profile_loaded: bool = False
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    chromium_version: Optional[str] = None
    boot_error: Optional[str] = None


state = _State()


async def _boot_browser() -> None:
    """Start the persistent Chromium under the Xvfb display.

    --no-sandbox would be needed if we ran as root, but the image runs
    as user `cseries` with the chromium-sandbox setuid helper installed,
    so the standard sandbox is functional and we keep it.

    Why still headed under Xvfb: sites that fingerprint headless
    Chromium behave differently, and the FSA polyfill lane was built
    and verified against the headed profile. The viewport no longer
    needs the display (CDP screencast reads the renderer, not X), so
    headless is now merely untested here, not blocked.

    Expose the FSA polyfill bindings on the context BEFORE
    creating the first page, then add_init_script the polyfill JS so
    every page (including iframes) has window.showDirectoryPicker
    overridden at navigation time.
    """
    state.playwright = await async_playwright().start()
    # proxy= rather than a --proxy-server flag: the NAC app lane authenticates with
    # Basic over a 407 challenge, and Chromium has no flag for a proxy credential.
    # Playwright fills the challenge from username/password. See _proxy_config.
    _proxy = _proxy_config()
    state.browser = await state.playwright.chromium.launch(
        executable_path=CHROMIUM_PATH,
        headless=False,
        proxy=_proxy,
        args=[
            f"--display={DISPLAY}",
            # Stable window size matching SCREEN_GEOMETRY so the X
            # window fits the virtual display.
            "--window-position=0,0",
            "--window-size=1440,900",
            # Disable a few things that fight Xvfb / are noisy in logs.
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,InterestCohort",
            # No automation flag: navigator.webdriver reads false, as in a
            # person's browser. The desktop render check must look like the
            # real desktop to the document it loads, so a document cannot
            # draw one thing for the check and another for the person
            # (document_verify.CHECK_BROWSER_ARGS); the sites a person
            # browses here see the same thing they would in their own.
            *document_verify.CHECK_BROWSER_ARGS,
            # We use Playwright's default user-data dir under /home/cseries,
            # which is on writable volume — fine inside the sidecar.
        ],
    )
    state.chromium_version = state.browser.version
    # Since 0.4.0 the shared page always starts clean. Until then boot
    # re-loaded the storage_state the retired /auth/anthropic/persist route
    # wrote — one vendor's login, silently back in the page on every boot —
    # and a pod that ran 0.3.x may still hold that blob on the PVC with no
    # button left to remove it. Scrub it here; a login the user wants kept
    # lives in the sessions lane, which holds nothing on disk.
    cookies.scrub_legacy()
    await _create_context_and_page()
    log.info("chromium booted, version=%s", state.chromium_version)


async def _create_context_and_page(storage_state: Optional[dict] = None) -> None:
    """Create a context + first page, with the polyfill bindings + init script
    wired. Called at boot; a caller that wants to drop the in-memory cookies
    closes the context and calls it again.

    `storage_state` restores a profile the KERNEL kept for the user (cookies +
    localStorage, sealed in the vault — see server/applications/browser_profile.py).
    None is a clean context, which is what boot did unconditionally before and what
    a signed-out user still gets.

    Why a profile at all: this pod scales to zero, so without one every visit is a
    brand-new device and a bank re-challenges every single time — a gate entirely
    separate from which IP the request came from, and one that fixing the egress
    exit does nothing about."""
    state.context = await state.browser.new_context(
        viewport={"width": 1440, "height": 900},
        storage_state=storage_state or None,
    )
    state.profile_loaded = bool(storage_state)

    # ── FSA polyfill wiring ────────────────────────────────────────
    # expose_binding lives on the context, so every page in this
    # context gets the bindings + the init script. Bindings become
    # window.__cseries_fs_*(...) callable from page JS. We use the
    # _ignored leading positional source param convention (Playwright
    # passes a source dict as the first arg).
    async def _b_grant(_src):
        return fs_api.current_grant()

    async def _b_list(_src, rel: str = ""):
        return fs_api.list_entries(rel or "")

    async def _b_stat(_src, rel: str):
        return fs_api.stat_entry(rel)

    async def _b_read(_src, rel: str):
        return fs_api.read_file(rel)

    async def _b_write(_src, rel: str, data_b64: str, create: bool = True):
        return fs_api.write_file(rel, data_b64, create=create)

    async def _b_mkdir(_src, rel: str, create_parents: bool = True):
        return fs_api.mkdir(rel, create_parents=create_parents)

    async def _b_remove(_src, rel: str, recursive: bool = False):
        return fs_api.remove_entry(rel, recursive=recursive)

    await state.context.expose_binding("__cseries_fs_current_grant", _b_grant)
    await state.context.expose_binding("__cseries_fs_list", _b_list)
    await state.context.expose_binding("__cseries_fs_stat", _b_stat)
    await state.context.expose_binding("__cseries_fs_read", _b_read)
    await state.context.expose_binding("__cseries_fs_write", _b_write)
    await state.context.expose_binding("__cseries_fs_mkdir", _b_mkdir)
    await state.context.expose_binding("__cseries_fs_remove", _b_remove)

    if POLYFILL_PATH.exists():
        await state.context.add_init_script(path=str(POLYFILL_PATH))
        log.info("polyfill injected: %s", POLYFILL_PATH)
    else:
        log.warning("polyfill not found at %s — FSA override disabled", POLYFILL_PATH)

    # ── the recorder ("Create agent") ─────────────────────────────
    # Two bindings and one init script on the SAME shared context the user
    # drives. The script is inert until __cseries_rec_active() answers true,
    # and it never sends a field's contents (control/recorder.py).
    async def _b_rec_active(_src) -> bool:
        return recorder.is_active()

    async def _b_rec(_src, payload: str) -> bool:
        return await recorder.on_event(_src, payload)

    await state.context.expose_binding("__cseries_rec_active", _b_rec_active)
    await state.context.expose_binding("__cseries_rec", _b_rec)
    await state.context.add_init_script(recorder._RECORDER_JS)

    def _rec_wire_page(p, index: int) -> None:
        p.on("framenavigated", recorder.on_frame_navigated)
        p.on("download", recorder.on_download)
        if index > 0:
            recorder.on_popup(index)

    _page_count = {"n": 0}

    def _on_new_page(p) -> None:
        _rec_wire_page(p, _page_count["n"])
        _page_count["n"] += 1

    state.context.on("page", _on_new_page)

    state.page = await state.context.new_page()
    await state.page.goto("about:blank")


async def _shutdown_browser() -> None:
    """Close everything in reverse order. Swallow individual failures so
    we always reach playwright.stop()."""
    for attr in ("page", "context", "browser"):
        obj = getattr(state, attr, None)
        if obj is None:
            continue
        try:
            await obj.close()
        except Exception as e:
            log.warning("error closing %s: %s", attr, e)
        setattr(state, attr, None)
    if state.playwright is not None:
        try:
            await state.playwright.stop()
        except Exception as e:
            log.warning("error stopping playwright: %s", e)
        state.playwright = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot Chromium once at startup; tear down at shutdown."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Registered before boot so the /v1/sessions routes answer 503 with a
    # real reason rather than raising on an unset provider if boot fails.
    sessions.set_browser_provider(lambda: state.browser)
    # The ship-proof page check draws a throwaway context off the same
    # Chromium (control/verify.py) — same provider, same 503-with-a-reason
    # behaviour if boot failed.
    verify.set_browser_provider(lambda: state.browser)
    # The desktop render check (control/document_verify.py): the same
    # throwaway-context shape, the same provider.
    document_verify.set_browser_provider(lambda: state.browser)
    viewport.set_providers(lambda: state.context, lambda: state.page)
    # Form capture reads the SAME page the viewport streams — what the
    # user is looking at is what "save these entries" must save.
    formcapture.set_page_provider(lambda: state.page)
    # The recorder watches that same page: "Create agent" records what the
    # user does in the viewport, never a named session.
    recorder.set_page_provider(lambda: state.page)
    try:
        await _boot_browser()
    except Exception as e:
        # Don't fail the whole process — /health will report degraded
        # so the orchestrator surfaces the problem instead of crashing
        # the pod in a tight loop.
        state.boot_error = f"{type(e).__name__}: {e}"
        log.exception("chromium boot failed")
    # Timer-driven, because reaping only on create/list means a caller
    # that makes one session and never calls either again pins its
    # context forever.
    sessions.start_reaper()
    yield
    # Sessions first: each saves its storage_state through its own
    # context, which _shutdown_browser is about to close.
    await sessions.shutdown_all()
    await _shutdown_browser()


app = FastAPI(
    title="cseries-browser",
    version="0.2.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# ── Bearer-token gate ──────────────────────────────────────────────────
# The control API had NO authentication. Loopback-only was the containment:
# the sidecar shares the entity pod's network namespace, so only the
# entity's own kernel could dial it. That is containment, not
# authentication, and it stops being true the moment this container leaves
# the entity pod — which is exactly what putting the browser on the
# on-demand (scale-to-zero) lifecycle requires, because a Deployment of its
# own is reachable over the pod network.
#
# This API drives allow-listed sites using stored logins and can be told to
# type a vault-held password. Unauthenticated on a routable address is not a
# thing to ship, so the token lands BEFORE the move, not with it.
#
# UNSET = previous behaviour, byte for byte. An empty BROWSER_CONTROL_TOKEN
# leaves the API open and loopback-contained, so this changes nothing for
# the in-pod sidecar running today and cannot break a live entity. Whoever
# stops binding loopback sets the token in the same change.
_CONTROL_TOKEN = os.environ.get("BROWSER_CONTROL_TOKEN", "").strip()

# /health is exempt so the kubelet's exec probe needs no secret. It reveals
# only whether Chromium is up — which anyone who can reach the port can
# determine anyway — and the alternative is a probe script that handles a
# credential, which is a worse thing to own. Every other route, /docs
# included (it enumerates the surface), is gated.
_OPEN_PATHS = frozenset({"/health"})


@app.middleware("http")
async def _require_token(request, call_next):
    # The routed path: request.url.path is rebuilt from the Host header, so
    # `Host: x/health?` would open every route.
    if not _CONTROL_TOKEN or request.scope["path"] in _OPEN_PATHS:
        return await call_next(request)
    presented = request.headers.get("x-browser-token") or ""
    if not presented:
        auth = request.headers.get("authorization") or ""
        scheme, _, rest = auth.partition(" ")
        if scheme.lower() == "bearer":
            presented = rest.strip()
    # compare_digest, not ==, so a wrong token cannot be recovered a byte at
    # a time from response timing. Both sides encoded first: compare_digest
    # raises on non-ASCII str, and this header is attacker-controlled.
    if not secrets.compare_digest(presented.encode("utf-8"),
                                  _CONTROL_TOKEN.encode("utf-8")):
        # Never echo what was presented, and never distinguish "absent" from
        # "wrong" — they are the same answer to a prober.
        log.warning("control API: rejected an unauthenticated %s %s",
                    request.method, request.scope["path"])
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# The general lane — named, isolated, allow-listed sessions. Additive:
# every route below still drives the single shared page.
app.include_router(sessions.router)
# The live viewport — CDP screencast + input over one WebSocket on this
# same port. NOTE the bearer middleware above is HTTP-scope only and
# does not run for websockets; viewport.py enforces the token itself.
app.include_router(viewport.router)
# "Save these entries" — reads the shared page's filled fields on an
# explicit click so the kernel can seal them. Behind the same bearer gate
# as everything else here; it returns plaintext form values, so it must
# never be reachable without it.
app.include_router(formcapture.router)
# "Create agent" — record what the user does on the shared page as replayable
# steps (control/recorder.py). Field values it read at commit points are
# handed over through formcapture's single-use path. Behind the bearer gate.
app.include_router(recorder.router)
# Load one page the way a browser does and report what a browser sees — the
# browser-verify half of the deployment-contract gate. One host,
# one context per call, nothing persisted. Behind the bearer gate.
app.include_router(verify.router)
# Render one self-contained page the kernel composed (a desktop design
# mounted the way the shell mounts it) and report what the browser saw —
# errors, pixels, what is visibly drawn. No network at all: the page
# arrives in the body. Behind the bearer gate.
app.include_router(document_verify.router)


@app.get("/health")
async def health() -> JSONResponse:
    """Readiness probe.

    Returns ok=true once Chromium is up and the persistent page exists.
    If boot failed, ok=false carries the error string for the operator
    to see in the supervisor's status surface.

    `is_connected()` is the part that matters. state.browser and state.page
    stay non-None after Chromium DIES — nothing clears them — so a
    presence check reported a healthy sidecar with a dead browser behind
    it, and the readiness probe kept the pod in service. Ask the driver
    whether the connection is actually alive.
    """
    alive = False
    if state.browser is not None:
        try:
            alive = state.browser.is_connected()
        except Exception:
            alive = False
    if state.page is not None and alive:
        return JSONResponse(
            {
                "ok": True,
                "chromium_version": state.chromium_version,
                "display": DISPLAY,
                "sessions": sessions.live_count(),
                # Whether this context was built from a restored profile. The
                # kernel reads it to distinguish "signed in" from "clean"
                # without ever reading a cookie.
                "profile_loaded": bool(state.profile_loaded),
                # Plaintext form values resident right now, awaiting the
                # user's confirm. Count only — never the values.
                "pending_captures": formcapture.live_count(),
            }
        )
    if state.browser is not None and not alive:
        return JSONResponse(
            status_code=503,
            content={"ok": False,
                     "error": "chromium was running and is no longer connected"},
        )
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error": state.boot_error or "chromium not ready",
        },
    )


# ── the user's profile: cookies + localStorage, kept BY THE KERNEL ──────
#
# This pod scales to zero when nobody is looking at it, so without a profile
# every visit is a brand-new device and a bank re-challenges every single time.
# That gate is entirely separate from which IP the request came from — fixing
# the egress exit does nothing about it, and vice versa; a working bank login
# needs both.
#
# The bytes live in the VAULT, not here and not on the PVC: a site session
# cookie is a BEARER credential — whoever holds it IS the user, with no password
# and no second factor. The sidecar cannot reach the vault (no host key, no DB),
# so the kernel supplies the blob on wake and collects it on release. This
# container stays a cache, which is the same layering control/sessions.py uses
# and the reason its own state is RAM-only.


class ProfileIn(BaseModel):
    storage_state: dict


@app.get("/profile")
async def profile_get() -> JSONResponse:
    """Export the live context's storage_state for the kernel to seal.

    Returns `{"loaded": bool, "storage_state": {...}}`. `loaded` says whether
    THIS context was built from a restored profile, so the kernel can tell a
    real sign-out from a pod that simply never had one."""
    if state.context is None:
        raise HTTPException(status_code=503, detail="no browser context")
    try:
        st = await state.context.storage_state()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"could not export state: {e}")
    return JSONResponse({"loaded": bool(state.profile_loaded), "storage_state": st})


@app.post("/profile")
async def profile_restore(body: ProfileIn) -> JSONResponse:
    """Replace the live context with one built from this storage_state.

    The context is recreated rather than patched: Playwright can only take
    storage_state at context creation, and add_cookies would restore cookies
    while silently dropping localStorage — which is half a login, and the half
    that is missing is the half sites use for device recognition.

    The open page is lost, which is why the kernel does this on the COLD wake
    before the user is looking at anything."""
    if state.browser is None:
        raise HTTPException(status_code=503, detail="browser not ready")
    # BUILD THE NEW CONTEXT FIRST. new_context validates storage_state and raises
    # on a malformed blob (a cross-version skew, a corrupted envelope) — and if the
    # old context were already closed, state.context/state.page would be left
    # pointing at dead objects while /health still read "ok" (it checks the browser
    # connection and page presence, not this). The pod would serve nothing until a
    # recycle. Closing only after the replacement exists makes a bad blob a clean
    # 400 with the session untouched.
    old = state.context
    try:
        await _create_context_and_page(storage_state=body.storage_state)
    except Exception as e:  # noqa: BLE001
        log.warning("profile restore refused: %r", e)
        raise HTTPException(status_code=400, detail=f"unusable profile: {e}")
    try:
        if old is not None:
            await old.close()
    except Exception:
        pass
    log.info("profile restored (%d cookies)",
             len(body.storage_state.get("cookies") or []))
    return JSONResponse({"ok": True, "loaded": True})


@app.delete("/profile")
async def profile_clear() -> JSONResponse:
    """Sign out everywhere: drop the live context and come back clean.

    The kernel deletes the sealed copy separately — this only clears what is in
    memory, so calling it alone would restore the profile on the next wake. The
    user-facing 'Sign out' does both, in that order, so a failure here cannot
    leave the sealed copy deleted but the live session still signed in."""
    if state.browser is None:
        raise HTTPException(status_code=503, detail="browser not ready")
    try:
        if state.context is not None:
            await state.context.close()
    except Exception:
        pass
    await _create_context_and_page()
    log.info("profile cleared — context is clean")
    return JSONResponse({"ok": True, "loaded": False})


@app.get("/screenshot")
async def screenshot() -> Response:
    """Capture a PNG of the current page.

    The first cut's only real interaction surface — proves the page is live
    and Chromium can render. Used by the e2e verification step at the
    end of this ship.
    """
    if state.page is None:
        raise HTTPException(status_code=503, detail="page not ready")
    png = await state.page.screenshot(type="png", full_page=False)
    return Response(content=png, media_type="image/png")


@app.get("/")
async def root() -> JSONResponse:
    """Index — points operators at the interesting surfaces. Hints
    only: how this container is reached (kernel proxy, port-forward,
    in-pod loopback) is the deployment's business, not ours."""
    return JSONResponse(
        {
            "service": "cseries-browser",
            "version": app.version,
            "endpoints": {
                "health": "/health",
                "screenshot": "/screenshot",
                "grant-fs": "POST /grant-fs {path, name?}",
                "revoke-fs": "POST /revoke-fs",
                "current-grant": "GET /current-grant",
                "navigate": "POST /navigate {url}",
                "polyfill-test": "GET /polyfill-test.html (round-trip test page)",
                "docs": "/docs",
                "viewport": ("WS /viewport/ws — CDP screencast + input; "
                             "reach it through the kernel proxy at "
                             "/system-apps/browser/viewport/ws, never the "
                             "pod IP"),
                "form-capture": ("POST /v1/form-capture — read the current "
                                 "page's filled fields on the user's explicit "
                                 "click (preview, secrets masked); "
                                 "POST /v1/form-capture/{id}/emit {keys[]} — "
                                 "hand the approved values over ONCE"),
            },
            # The general lane. Isolated context per session, no FS
            # bindings, navigation pinned to the hosts the caller
            # declared at create time.
            "sessions": {
                "list": "GET /v1/sessions",
                "create": "POST /v1/sessions {name, allow[], persist?, storage_state?}",
                "get": "GET /v1/sessions/{name}",
                "close": "DELETE /v1/sessions/{name}?save=true",
                "act": "POST /v1/sessions/{name}/act {action, ...}",
                "actions": [
                    "navigate", "snapshot", "click", "type", "type_secret",
                    "press", "select", "scroll", "wait", "wait_for_text",
                    "text", "screenshot", "pages", "switch_page",
                    "close_page", "downloads", "export_state",
                ],
            },
        }
    )


# The Ship-4 "claude.ai cookie auth lifecycle" (/auth/anthropic/status,
# /begin, /persist, DELETE /auth/anthropic) lived here until 0.4.0. It drove
# the shared page to one vendor's login and persisted that vendor's cookies
# to disk. A general browser has no business holding a vendor-login flow, and
# the platform does not ship one. A login the user wants
# kept goes through the sessions lane, which keeps nothing on disk.


# ── FSA grant control + driving the page ────────────────────────────


class GrantBody(BaseModel):
    path: str
    name: Optional[str] = None


@app.post("/grant-fs")
async def grant_fs(body: GrantBody) -> JSONResponse:
    """Set the polyfill's granted root.

    Subsequent showDirectoryPicker() calls from page JS receive a
    handle rooted at {path}. Operator-facing — called once per browser
    session to choose which project dir Claude Design can see.
    """
    try:
        result = fs_api.grant(body.path, body.name)
    except fs_api.PolyfillFSError as e:
        raise HTTPException(status_code=400, detail=f"{e.code}: {e.message}")
    return JSONResponse(result)


@app.post("/revoke-fs")
async def revoke_fs() -> JSONResponse:
    return JSONResponse(fs_api.revoke())


@app.get("/current-grant")
async def current_grant_endpoint() -> JSONResponse:
    return JSONResponse(fs_api.current_grant())


class NavigateBody(BaseModel):
    url: str


@app.post("/navigate")
async def navigate(body: NavigateBody) -> JSONResponse:
    """Navigate the persistent page to a URL — whatever the user typed in
    the address bar. /polyfill-test.html is the round-trip test page; on any
    site that asks for a local folder the polyfill auto-grants the project
    the user pointed the browser at."""
    if state.page is None:
        raise HTTPException(status_code=503, detail="page not ready")
    # An address typed while recording is a `go` step; the page event that
    # follows is the same navigation and is not written twice.
    recorder.note_bar_navigate(body.url)
    try:
        resp = await state.page.goto(body.url, wait_until="load", timeout=30000)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"navigation failed: {e}")
    return JSONResponse(
        {
            "url": state.page.url,
            "status": resp.status if resp else None,
            "title": await state.page.title(),
        }
    )


@app.get("/polyfill-test.html")
async def polyfill_test() -> Response:
    """Serve the round-trip test page from the sidecar itself.

    Same-origin from the sidecar's perspective so the polyfill (added
    via init script) is present without any auth dance. Use as the
    verification target: POST /navigate {url: "http://127.0.0.1:8830/polyfill-test.html"}.
    """
    if not POLYFILL_TEST_PATH.exists():
        raise HTTPException(status_code=404, detail="test page not found")
    return Response(
        content=POLYFILL_TEST_PATH.read_bytes(),
        media_type="text/html",
    )


@app.post("/verify-polyfill")
async def verify_polyfill() -> JSONResponse:
    """Drive the polyfill from Python and report what happened.

    Exercises the full round-trip:
      1. showDirectoryPicker() → root handle
      2. entries() — list directly under granted root
      3. getFileHandle('verify.txt', create:true) → writable → write → close
      4. getFile() → text()
      5. getDirectoryHandle('subdir', create:true) → getFileHandle nested
      6. removeEntry('verify.txt'), removeEntry('subdir', recursive:true)
      7. final entries() to confirm cleanup

    Returns the round-trip log as JSON so an external test can assert.
    The grant must be set via /grant-fs first (returns 503 otherwise).
    """
    if state.page is None:
        raise HTTPException(status_code=503, detail="page not ready")
    g = fs_api.current_grant()
    if not g.get("granted"):
        raise HTTPException(status_code=503, detail="no FS grant; POST /grant-fs first")

    # Navigate to an about:blank-ish page so the polyfill init script
    # is loaded into the page's window. polyfill-test.html works but
    # gives us extra DOM noise — about:blank is enough since the
    # polyfill is context-wide.
    await state.page.goto("about:blank")

    script = r"""
        async () => {
            const log = [];
            const expectMs = Date.now();
            const r = await window.showDirectoryPicker();
            log.push({step: 'pick', name: r.name, kind: r.kind});

            const entries0 = [];
            for await (const [n, h] of r.entries()) entries0.push({n, k: h.kind});
            log.push({step: 'list-initial', entries: entries0});

            const fname = 'verify-' + expectMs + '.txt';
            const fh = await r.getFileHandle(fname, {create: true});
            const w = await fh.createWritable();
            const payload = 'polyfill round-trip at ' + new Date(expectMs).toISOString() + '\n';
            await w.write(payload);
            await w.close();
            log.push({step: 'write', name: fh.name, bytes: payload.length});

            const fh2 = await r.getFileHandle(fname);
            const file = await fh2.getFile();
            const text = await file.text();
            log.push({step: 'read', name: file.name, size: file.size, text, match: text === payload});

            const dh = await r.getDirectoryHandle('verify-subdir-' + expectMs, {create: true});
            const inner = await dh.getFileHandle('inner.txt', {create: true});
            const iw = await inner.createWritable();
            await iw.write('nested');
            await iw.close();
            log.push({step: 'mkdir-nested', dir: dh.name, file: inner.name});

            const entries1 = [];
            for await (const [n, h] of r.entries()) entries1.push({n, k: h.kind});
            log.push({step: 'list-after-create', entries: entries1});

            await r.removeEntry(fname);
            await r.removeEntry('verify-subdir-' + expectMs, {recursive: true});
            log.push({step: 'cleanup', ok: true});

            const entries2 = [];
            for await (const [n, h] of r.entries()) entries2.push({n, k: h.kind});
            log.push({step: 'list-final', entries: entries2});

            return {
                ok: true,
                polyfillVersion: window.__cseries_fsa_version,
                log,
            };
        }
    """
    try:
        result = await state.page.evaluate(script)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"{type(e).__name__}: {e}"},
        )
    return JSONResponse(result)
