"""Load one page the way a browser does and report what a browser sees.

The browser-verify half of the deployment-contract gate.
The static half refuses bytes that cannot work; this half LOADS the
built app and catches what bytes cannot show: a script that resolves to the
wrong place, an uncaught exception on load, a page that never mounts.

Why a real browser and not a fetch. On a live Host a shipped app
proved "delivered — running and ready with a clean log" four times while its
window rendered blank: its page carried <base href="/app/app/">, so a
relative module chunk resolved one level too deep and came back as HTML. A
liveness ping cannot see that. Three attempts to catch it with a regex and
two HTTP calls each produced a FALSE PASS one layer deeper (the kernel's JSON
404, the ?v= buster on classic scripts, the SDK tag the kernel injects first
in <head>). A simulator of the thing that defines correctness cannot
converge; the browser's behaviour is the spec. So: ask the browser.

What this reads, all of it signals the page itself produces:
  - uncaught exceptions (`pageerror`) — a module that failed to evaluate;
  - console errors — what the app said went wrong;
  - script requests that failed or answered >= 400 — the missing chunk;
  - the App SDK's own `cseries:app-ready` — the kernel injects app-sdk.js into
    every served app page and it posts that message when the page is up
    (app-sdk.js). At top level `window.parent` IS `window`, so an init script
    listening on `message` records it.

THIS IS NOT THE SESSIONS LANE. /v1/sessions is a recipe surface whose caller
names public hosts, and its jail forbids loopback, `.svc` and
`.cluster.local` because a recipe pointed there is a request-forgery tool.
Here the caller IS the kernel, asking about a page on its own origin, and it
passes that origin. The guard below allows exactly that one host and aborts
everything else — including CDNs. A verify never fetches third parties; a
blocked third-party script shows up as a blocked resource in the report, not
as a pass and not as an error. The guard is narrower than the sessions jail,
not a hole in it.

One context per call, closed on exit. Nothing persists: no storage_state,
no download dir, no filesystem bindings (see app._create_context_and_page
for what the operator page has and this deliberately does not).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("browser.verify")

router = APIRouter(prefix="/v1/verify", tags=["verify"])

# Bounds. A page that has not settled in this long is not going to.
NAV_TIMEOUT_MS = 20_000
SETTLE_MS_DEFAULT = 3_000
SETTLE_MS_MAX = 15_000
# Route walk. Bounded twice — a count and a wall-clock budget
# — so a big nav or a slow app can never stretch a proof without limit.
_MAX_WALK = 8
_WALK_SETTLE_MS = 1_200
_WALK_BUDGET_S = 45.0
# How many of each signal ride the report. Enough to diagnose; never a log dump.
_MAX_ITEMS = 20
_MAX_TEXT = 400

_browser_provider: Optional[Callable[[], Any]] = None


def set_browser_provider(fn: Callable[[], Any]) -> None:
    """app.py hands over `lambda: state.browser` — the same shared Chromium
    the sessions lane draws contexts from."""
    global _browser_provider
    _browser_provider = fn


def _browser():
    b = _browser_provider() if _browser_provider is not None else None
    if b is None:
        raise HTTPException(status_code=503, detail="chromium not ready")
    return b


# The init script. Installed before any page script runs, so it sees the SDK
# post `cseries:app-ready` even if that happens synchronously during load.
_INIT_SCRIPT = """
(() => {
  window.__cseriesVerify = { appReady: false, messages: 0 };
  window.addEventListener('message', (e) => {
    try {
      window.__cseriesVerify.messages += 1;
      if (e && e.data && e.data.type === 'cseries:app-ready') {
        window.__cseriesVerify.appReady = true;
      }
    } catch (_) {}
  }, true);
})();
"""

_PROBE_SCRIPT = """
() => ({
  app_ready: !!(window.__cseriesVerify && window.__cseriesVerify.appReady),
  sdk_present: typeof window.CSeriesApp !== 'undefined',
  scripts: document.scripts ? document.scripts.length : 0,
  body_chars: document.body ? (document.body.innerText || '').length : 0,
  title: document.title || ''
})
"""


class VerifyBody(BaseModel):
    url: str = Field(..., description="The page to load, on the caller's own origin.")
    settle_ms: int = Field(SETTLE_MS_DEFAULT, ge=0, le=SETTLE_MS_MAX,
                           description="How long to keep listening after load.")
    # Sent on EVERY request of the load. The kernel's source-IP gate refuses
    # a cross-pod fetch of /system-apps/<id>/... unless it carries the
    # one-shot verify capability the kernel minted; without this on the
    # page, its scripts and the SDK alike, every proof read "HTTP 403".
    # Applied to the page host only — never
    # leaked to a third party the page happens to load.
    headers: dict[str, str] = Field(default_factory=dict,
                                    description="Extra request headers for the page host.")
    # The paths the KERNEL itself serves at the origin root — the shared
    # design system (/css/*), the shell scripts (/js/*) and the SDK it
    # injects. A same-host request outside the app's mount is otherwise read
    # as "built for the wrong base"; these are the platform's, not the app's.
    # The kernel names them, because it is the one that serves them; when an
    # older kernel sends nothing, _PLATFORM_FILES_DEFAULT applies.
    platform_paths: Optional[list[str]] = Field(
        None, description="Root paths the caller's kernel serves itself.")


# What a verify may reach besides the page's own host: the public internet
# — because that is what the user's browser reaches, and a custom app has no
# CSP in its real origin (applications/proxy._app_csp is community-only) so
# its CDN scripts DO load there. Blocking them here failed every app with a
# <script src="https://cdn…">. What it may NOT
# reach is loopback, private ranges and cluster-internal names: a page that
# fetches those is using the verify as a request-forgery tool, and those are
# aborted and reported, never fetched. Same lists the sessions lane uses.
def _internal_host(host: str) -> bool:
    from control.sessions import _FORBIDDEN_HOSTS, _FORBIDDEN_SUFFIXES
    h = (host or "").strip().lower().rstrip(".")
    if not h or h in _FORBIDDEN_HOSTS:
        return True
    if any(h == suf.lstrip(".") or h.endswith(suf) for suf in _FORBIDDEN_SUFFIXES):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(h.strip("[]"))
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False


def _clip(s: Any) -> str:
    t = str(s or "")
    return t if len(t) <= _MAX_TEXT else t[:_MAX_TEXT] + "…"


def _is_script(req) -> bool:
    try:
        return req.resource_type == "script"
    except Exception:
        return False


def _is_data(req) -> bool:
    """An XHR/fetch — the app asking its backend for data, not for code."""
    try:
        return req.resource_type in ("xhr", "fetch")
    except Exception:
        return False


def _mount_of(path: str) -> Optional[str]:
    """The app's mount prefix for a verify url, or None when the url is not
    under /system-apps/<id>/ — nothing to compare against, so no check."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "system-apps":
        return f"/system-apps/{parts[1]}/"
    return None


# What may legitimately sit OUTSIDE the app's mount on the page host.
#
# A prefix ("/api/") for the one case the contract defines that way: apps call
# their own origin's /api/... (build_contract.py:534-535) and CSeriesApp.storage
# does exactly that (app-sdk.js:98).
#
# Exact FILES for everything the kernel serves at the root. Every system app
# links the shared design system from there (/css/tokens.css, motion.css,
# cseries-components.css — keys, workbench, settings, browser, network,
# orchestrator all do), some load shell scripts (/js/qrcode.js,
# /js/model-picker.js), and the kernel injects /js/app-sdk.js itself
# (proxy.py:442). Allowing only app-sdk.js made every system app read as
# "built for the wrong base". Allowing the whole /js/ and /css/ prefixes
# swung the other way: a hand-built app with <script src="/js/app.js"> —
# root-absolute, genuinely wrong — became invisible. So: the kernel passes
# the exact set it serves (VerifyBody.platform_paths, listed from its own
# dashboard dirs), and this default mirrors dashboard/base/{css,js} for a
# kernel too old to send one. /favicon.ico is the browser's own request on
# a page with no icon link — the app never wrote it.
_PLATFORM_PATH_PREFIXES = ("/api/",)
_PLATFORM_FILES_DEFAULT: frozenset[str] = frozenset({
    "/favicon.ico",
    # The design system's @font-face (tokens.css:15-37) loads these
    # root-absolute, so every system app that renders text requests them —
    # a sub-resource of a stylesheet, which no leaf-request test models.
    # Measured in real Chromium: Keys and Settings both escaped on
    # /fonts/inter-var-latin.woff2 while Browser (never applies --font-body)
    # passed, so a manual look at one app would not have shown it.
    *(f"/fonts/{n}" for n in (
        "inter-var-latin-ext.woff2", "inter-var-latin.woff2",
        "jetbrains-mono-var-latin.woff2")),
    *(f"/css/{n}" for n in (
        "boot-screen.css", "desktop.css", "dock-menu.css", "floating-apps.css",
        "model-picker.css", "motion.css",
        "cseries-components.css", "theme.css", "tokens.css", "widget-layer.css")),
    *(f"/js/{n}" for n in (
        "app-runner.js", "app-sdk.js", "chat-stream.js", "desktop-layout.js",
        "desktop-restore.js", "desktop-surface.js", "desktop.js", "dock-menu.js",
        "floating-apps.js", "icons.js", "markdown.js", "model-picker.js",
        "presence.js", "qrcode.js", "session-keeper.js",
        "shell-token.js", "taskbar.js", "tray-status.js", "viewport.js",
        "walkthrough-autolaunch.js", "widget-layer.js", "ws-client.js")),
})


def _escapes_mount(path: str, mount: str,
                   platform: frozenset[str] = _PLATFORM_FILES_DEFAULT) -> bool:
    if path.startswith(mount):
        return False
    if any(path.startswith(p) for p in _PLATFORM_PATH_PREFIXES):
        return False
    return path not in platform


def _is_config_store(path: str) -> bool:
    """CSeriesApp.storage's own endpoint. A 404 there is 'no value yet' by
    design (applications.py:2239-2249; app-sdk.js:110-112)."""
    return path.startswith("/api/applications/") and "/config" in path


def _wants_json(req) -> bool:
    try:
        accept = (req.headers or {}).get("accept", "").lower()
    except Exception:
        return False
    return "application/json" in accept and "text/html" not in accept


@router.post("")
async def verify(body: VerifyBody) -> JSONResponse:
    """Load `url`, listen, report. Never raises for what the PAGE does — a
    broken page is the finding. Raises only for a request this route will
    not make."""
    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400,
                            detail="url must be an absolute http(s) url")
    host = parsed.hostname.lower()

    browser = _browser()
    t0 = time.monotonic()
    report: dict[str, Any] = {
        "url": body.url, "final_url": None, "http_status": None,
        "loaded": False, "app_ready": False, "sdk_present": False,
        "uncaught": [], "console_errors": [],
        "failed_resources": [], "blocked_resources": [],
        # The two signals the blank-window failure above
        # needed and the original listeners could not see.
        #   escaped_mount — the page asked its own origin for something OUTSIDE
        #     its mount. That is a build compiled for base '/' and it 404s.
        #     Recorded whatever the status, because a root-absolute URL that
        #     happens to answer is still the wrong app's bytes.
        #   api_failures — an XHR that failed, or that asked for JSON and was
        #     handed text/html (the SPA fallback answering an API path). The
        #     old handlers only counted resource_type == "script", so an app
        #     whose every API call returned 200 text/html read as a clean pass
        #     while it rendered nothing.
        "escaped_mount": [], "api_failures": [],
        # api_notes — 4xx answers that did NOT fail the verdict. A 4xx is
        # ambiguous: a missing route and "no trade with that id" look the
        # same from here. They are reported so the host can read them, and
        # never judged, so a working app is never failed on one.
        "api_notes": [],
        "routes_walked": [], "routes_found": 0, "routes_not_walked": 0,
        "mount": None,
        "scripts": 0, "body_chars": 0, "title": "",
        "settle_ms": body.settle_ms, "elapsed_ms": 0,
    }
    mount = _mount_of(parsed.path)
    report["mount"] = mount
    platform = (frozenset(body.platform_paths) if body.platform_paths
                else _PLATFORM_FILES_DEFAULT)

    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    # The cap also rides a cookie on the page host: context.route never sees
    # a WebSocket handshake, so a page's WS to its own app carries the cap only
    # this way (the kernel's WS gate reads it; localhost_only.websocket_refusal).
    _cap = next((v for k, v in (body.headers or {}).items() if k.lower() == "x-cseries-verify-cap"), None)
    if _cap:
        # httpOnly: the page's own scripts never read the cap (the header
        # never showed it to them either).
        await context.add_cookies([{"name": "cseries_verify_cap", "value": _cap, "httpOnly": True,
                                    "url": f"{parsed.scheme}://{parsed.netloc}/"}])
    context.set_default_timeout(NAV_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    # URLs this route itself aborted. The driver still emits `requestfailed`
    # for them, so without this set a blocked third party was counted as a
    # script the page needed and failed the verdict — the module docstring
    # said "not an error" and the code said the opposite.
    aborted: set[str] = set()
    try:
        # THE GUARD. The page host always; the public internet, because the
        # user's browser reaches it and a custom app's real origin has no CSP
        # (its CDN scripts load there, so they must load here); never
        # loopback, private ranges or cluster-internal names. Arity note
        # copied from sessions._open_context — Playwright calls the handler
        # with (route, request); `*_rest` absorbs either convention.
        async def _guard(route, *_rest) -> None:
            req = route.request
            rp = urlparse(req.url)
            rh = (rp.hostname or "").lower()
            if rp.scheme in ("http", "https") and rh == host:
                # Escaped-mount detector. Every same-host request is either
                # under the app's mount or one of the two platform paths; a
                # third case means the build was compiled for the wrong base.
                # Checked HERE rather than on the response because the finding
                # is the URL the page ASKED for, independent of what came back.
                if mount and _escapes_mount(rp.path, mount, platform) \
                        and len(report["escaped_mount"]) < _MAX_ITEMS:
                    report["escaped_mount"].append(
                        {"url": _clip(req.url),
                         "type": _clip(getattr(req, "resource_type", ""))})
                # The cap rides the page host only.
                if body.headers:
                    await route.continue_(headers={**req.headers, **body.headers})
                else:
                    await route.continue_()
                return
            if rp.scheme in ("http", "https") and rh and not _internal_host(rh):
                await route.continue_()
                return
            aborted.add(req.url)
            if len(report["blocked_resources"]) < _MAX_ITEMS:
                report["blocked_resources"].append(
                    {"url": _clip(req.url), "type": _clip(getattr(req, "resource_type", ""))})
            await route.abort("blockedbyclient")

        await context.route("**/*", _guard)
        await context.add_init_script(_INIT_SCRIPT)
        page = await context.new_page()

        def _on_pageerror(err):
            if len(report["uncaught"]) < _MAX_ITEMS:
                report["uncaught"].append(_clip(getattr(err, "message", err)))

        def _on_console(msg):
            try:
                if msg.type == "error" and len(report["console_errors"]) < _MAX_ITEMS:
                    report["console_errors"].append(_clip(msg.text))
            except Exception:
                pass

        def _on_requestfailed(req):
            if req.url in aborted:
                return  # ours, already in blocked_resources — not a page failure
            if _is_script(req) and len(report["failed_resources"]) < _MAX_ITEMS:
                report["failed_resources"].append(
                    {"url": _clip(req.url), "status": None,
                     "error": _clip(getattr(req, "failure", None))})

        def _on_response(resp):
            try:
                req = resp.request
                ctype = _clip(resp.headers.get("content-type", ""))
                if _is_script(req) and resp.status >= 400 \
                        and len(report["failed_resources"]) < _MAX_ITEMS:
                    report["failed_resources"].append(
                        {"url": _clip(resp.url), "status": resp.status,
                         "content_type": ctype})
                    return
                # An API call that failed, or that asked for JSON and got HTML.
                # The second is the silent one: a mis-prefixed API path misses
                # the /api/ branch, lands on the SPA fallback, and answers 200
                # text/html. The app then renders empty with nothing in the
                # console — exactly what happened in the blank-window failure.
                if _is_data(req) and len(report["api_failures"]) < _MAX_ITEMS:
                    # Three tiers. FAILS the verdict: a 5xx (the backend is
                    # broken) or JSON asked for and HTML received (the
                    # blank-window defect — a mis-prefixed path fell through to
                    # the SPA fallback and answered 200 text/html). SKIPPED
                    # outright: a 404 from CSeriesApp.storage, which is "no
                    # value yet" by design, and 401/403, which is "not signed
                    # in yet". Failing a correct app on either sends a healthy
                    # build to a repair round with a wrong brief. NOTED, not
                    # judged: every other 4xx — a missing route and "no such
                    # record" look identical here, so the host is shown them
                    # and left to read them.
                    rpath = urlparse(resp.url).path
                    if resp.status >= 500:
                        report["api_failures"].append(
                            {"url": _clip(resp.url), "status": resp.status,
                             "content_type": ctype, "why": "server error"})
                    elif "text/html" in ctype.lower() and _wants_json(req):
                        report["api_failures"].append(
                            {"url": _clip(resp.url), "status": resp.status,
                             "content_type": ctype,
                             "why": "asked for JSON, received HTML"})
                    elif resp.status >= 400:
                        expected = (resp.status in (401, 403)
                                    or (resp.status == 404 and _is_config_store(rpath)))
                        if not expected and len(report["api_notes"]) < _MAX_ITEMS:
                            report["api_notes"].append(
                                {"url": _clip(resp.url), "status": resp.status,
                                 "content_type": ctype})
            except Exception:
                pass

        page.on("pageerror", _on_pageerror)
        page.on("console", _on_console)
        page.on("requestfailed", _on_requestfailed)
        page.on("response", _on_response)

        try:
            resp = await page.goto(body.url, wait_until="load")
            report["loaded"] = True
            report["http_status"] = resp.status if resp is not None else None
        except Exception as e:  # noqa: BLE001 — a page that will not load IS the finding
            report["load_error"] = _clip(e)

        if report["loaded"]:
            # Keep listening: the SDK's ready message and the app's first
            # errors land after `load`. Stop early once ready is seen.
            deadline = time.monotonic() + body.settle_ms / 1000.0
            while time.monotonic() < deadline:
                try:
                    probe = await page.evaluate(_PROBE_SCRIPT)
                    if probe.get("app_ready"):
                        break
                except Exception:
                    break
                await asyncio.sleep(0.25)
            try:
                probe = await page.evaluate(_PROBE_SCRIPT)
                report.update({
                    "app_ready": bool(probe.get("app_ready")),
                    "sdk_present": bool(probe.get("sdk_present")),
                    "scripts": int(probe.get("scripts") or 0),
                    "body_chars": int(probe.get("body_chars") or 0),
                    "title": _clip(probe.get("title")),
                })
            except Exception as e:  # noqa: BLE001
                report["probe_error"] = _clip(e)
            report["final_url"] = page.url

            # ── walk the app's own links ─────────────────────────────────
            # The entrypoint alone proves the SHELL renders. In the blank-window
            # failure the shell rendered perfectly while three linked routes
            # were empty, because their route chunks were fetched from the
            # origin root. Only a navigation pulls a lazily-imported chunk, so
            # only a walk can see that.
            #
            # NAVIGATION, not clicking: a click can fire an arbitrary in-app
            # action (an app may have start/stop buttons). Following an <a href>
            # under the app's own mount is the
            # same GET a user's address bar would make.
            if mount:
                try:
                    hrefs = await page.eval_on_selector_all(
                        "a[href]",
                        "els => els.map(e => e.href)")
                except Exception:
                    hrefs = []
                seen = {page.url.split("#")[0]}
                candidates = []
                for h in hrefs:
                    hp = urlparse(h)
                    base = h.split("#")[0]
                    if (hp.hostname or "").lower() != host:
                        continue
                    if not hp.path.startswith(mount) or base in seen:
                        continue
                    seen.add(base)
                    candidates.append(base)
                # Every distinct route is COUNTED even though only the first
                # few are walked, so the verdict can say how much of the app
                # the proof did not see.
                todo = candidates[:_MAX_WALK]
                report["routes_found"] = len(candidates)
                walk_deadline = time.monotonic() + _WALK_BUDGET_S
                for target in todo:
                    if time.monotonic() > walk_deadline:
                        break
                    try:
                        r = await page.goto(target, wait_until="load")
                        await page.wait_for_timeout(_WALK_SETTLE_MS)
                        report["routes_walked"].append(
                            {"url": _clip(target),
                             "status": r.status if r is not None else None})
                    except Exception as e:  # noqa: BLE001 — a route that will
                        # not load IS the finding, same as the entrypoint.
                        report["routes_walked"].append(
                            {"url": _clip(target), "status": None,
                             "error": _clip(e)})
    finally:
        try:
            await context.close()
        except Exception:
            pass

    report["routes_not_walked"] = max(
        0, report["routes_found"] - len(report["routes_walked"]))
    report["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    report["ok"], report["why"] = _judge(report)
    return JSONResponse(report)


def _judge(r: dict) -> tuple[bool, str]:
    """One verdict from the signals, most specific reason first."""
    if not r.get("loaded"):
        return False, f"the page did not load: {r.get('load_error') or 'navigation failed'}"
    st = r.get("http_status")
    if isinstance(st, int) and st >= 400:
        return False, f"the page itself answered HTTP {st}"
    if r.get("failed_resources"):
        f = r["failed_resources"][0]
        return False, (f"a script the page needs failed: {f.get('url')} "
                       f"({'HTTP ' + str(f['status']) if f.get('status') else f.get('error') or 'request failed'})")
    # Ahead of `uncaught`: both of these CAUSE the exceptions and empty pages
    # further down, so naming the cause beats naming the symptom in the repair
    # brief the failing verdict becomes.
    if r.get("escaped_mount"):
        e = r["escaped_mount"][0]
        return False, (
            f"the page asked for {e.get('url')}, which is outside its own "
            f"mount {r.get('mount')} — the build was compiled for base '/' "
            f"and those requests do not reach this app")
    if r.get("api_failures"):
        a = r["api_failures"][0]
        return False, (f"an API call the page needs failed: {a.get('url')} "
                       f"(HTTP {a.get('status')}, {a.get('content_type')} — "
                       f"{a.get('why')})")
    walk_errs = [w for w in (r.get("routes_walked") or [])
                 if w.get("error") or (isinstance(w.get("status"), int)
                                       and w["status"] >= 400)]
    if walk_errs:
        w = walk_errs[0]
        return False, (f"a route the page links to did not load: {w.get('url')} "
                       f"({'HTTP ' + str(w['status']) if w.get('status') else w.get('error')})")
    if r.get("uncaught"):
        return False, f"an uncaught exception on load: {r['uncaught'][0]}"
    if r.get("sdk_present") and not r.get("app_ready"):
        return False, ("the App SDK loaded but the page never signalled "
                       "cseries:app-ready within the settle window")
    # Say what was actually checked. "page loaded" over a page whose routes
    # were never visited is the same over-claim the delivery proof made
    # four times running.
    walked = len(r.get("routes_walked") or [])
    scope = (f", {walked} linked route{'s' if walked != 1 else ''} loaded"
             if walked else "; no in-app links to walk")
    skipped = int(r.get("routes_not_walked") or 0)
    if skipped:
        scope += (f"; PARTIAL proof: the page links to {r.get('routes_found')} "
                  f"routes and {skipped} were not walked (a walk is limited to "
                  f"{_MAX_WALK} routes and {int(_WALK_BUDGET_S)}s)")
    return True, (("page loaded, app signalled ready" if r.get("app_ready")
                   else "page loaded with no script failures or uncaught exceptions")
                  + scope)
