"""Named, isolated browser sessions — the general control surface.

The Ship-1..4 surface in `app.py` drives ONE shared page: a single
context, a single storage_state on disk (claude.ai), and the File
System Access polyfill bound into that context. That shape is correct
for what it was built for and is left untouched — every existing route
still drives that page.

This module adds the general lane beside it, for an in-pod caller (a
`runtime.kind=process` app under the user's own `custom` provenance)
that wants to drive a site it named. Three properties make it
different from the shared page, and all three are the point:

1. ISOLATION. Each session is its own BrowserContext with its own
   storage_state, held separately per session. A recipe logged into
   site A cannot read site B's cookies, and neither can read the
   shared page's cookies.

2. NO FILESYSTEM. Session contexts get NO `__cseries_fs_*` bindings and
   no FSA polyfill (contrast app._create_context_and_page, which wires
   both). The shared page's whole job is handing page JS a directory
   handle; a session's job is the opposite — a page it drives must not
   reach the entity's disk.

3. A DECLARED BLAST RADIUS. A session is created with the hosts it may
   navigate to. Enforcement is a context-level route guard on DOCUMENT
   requests, so a redirect or a link click cannot walk the session onto
   a host the caller never declared. Subresources (CDNs, fonts, XHR)
   are NOT filtered — filtering them breaks ordinary sites and buys
   little, since the guard already pins which site the page IS.

There is deliberately no `eval` action. The caller gets snapshot plus
named actions, never arbitrary JS in the page. A surface that accepts
JS has a blast radius of "whatever the model was talked into", which is
the failure mode this design exists to avoid.

Refs, not selectors: `snapshot` enumerates the visible interactive
elements across every frame, assigns each a short ref (e1, e2, …), and
keeps the ref->ElementHandle map IN THIS PROCESS. `click`/`type`/`select`
take a ref. The page cannot see or alter that map — it used to live in a
page global, where a page could swap the element behind a ref
after the snapshot so the guard checked one element and fill() wrote to
another. Refs are dropped when the main frame navigates (including
History-API navigation, which issues no request), and every use re-checks
that the node is still connected, so a dead ref is a 409 asking for a
fresh snapshot — never a silent click on whatever now occupies that spot.

Playwright is pinned at 1.48.0 (see ../Dockerfile). Everything here
uses APIs that exist in that release: no aria_snapshot (1.49+), no
locator handles. The snapshot is plain DOM built in-page, which is also
why it is version-proof.

WHAT THIS DOES NOT DEFEND AGAINST — state it plainly rather than let a
reader infer safety from the guards above:

- PROMPT INJECTION. Every `name` in a snapshot and every byte of `text`
  is attacker-controlled content from the page, and it is going to a
  model that is deciding the next action. Nothing here sanitises it and
  nothing can: the whole point of the payload is to describe the page.
  The allow-list bounds WHERE a recipe can act, never what a page can
  talk it into doing on the host it is already on. A caller that feeds
  this to a model must treat the response as untrusted input.
- THE PLAIN `type` ACTION. It takes a literal string, so anything sent
  through it is held by the caller. That is fine for a search box and
  wrong for a password: use `type_secret`, which only the vault broker
  drives (server/vault/authz.py::_do_browser_type opens the sealed
  envelope in the kernel and posts the bytes here over pod loopback, so
  neither the recipe nor a model ever holds them). Nothing stops a
  caller putting a password through `type` — the guard is that it does
  not have to.

WHERE SESSION STATE LIVES — in RAM here, sealed in the vault there.

An earlier revision persisted each session's storage_state as 0600 JSON
on the entity PVC, mirroring what control/cookies.py did for the shared
page's vendor login until 0.4.0. That is the wrong home for it: a site session cookie is
a BEARER credential — whoever holds it IS the user, with no password and
no second factor — and a bank's is the clearest case.

The sidecar cannot reach the vault itself. Sealing runs in the kernel
container, which holds the host's private key and the DB pool
(server/vault/api.py::open_secret). So the layering is:

    vault (durable, sealed)  <->  caller holding read/write:vault
                                        |  supplies + collects the blob
                                        v
                              sidecar (in-memory only)

The caller passes `storage_state` on create and collects it back with
the `export_state` action. A sidecar restart therefore loses the login
and the caller re-supplies it from the vault on the next create — which
is correct: the vault is the store of record and this container is a
cache.

DOWNLOADS are the one thing that IS written to disk, deliberately. A
downloaded statement is the user's own data and the entire point of the
recipe, so it goes to DOWNLOAD_ROOT/<session>/ on the entity PVC where
the kernel can pick it up. Playwright refuses downloads unless the
context asks for them and deletes the temp file when the context closes,
so they are accepted at create and saved eagerly on the event. The
filename comes from the SITE and is sanitised before it is joined onto a
path.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control import cookies

log = logging.getLogger("cseries-browser.sessions")

# NO CREDENTIAL IS WRITTEN TO DISK — see the module docstring.
#
# Downloads are the deliberate exception, and they are not credentials: a
# statement or a transaction export is the user's own data and the entire
# reason the recipe ran, so it has to land somewhere the kernel can read
# it. That is the entity PVC, which both containers mount at the same
# path. Rooted off the one existing definition of where sidecar state
# lives (cookies.STATE_ROOT = /var/lib/cseries) rather than introducing a
# second hardcoded absolute path.
DOWNLOAD_ROOT = (
    Path(os.environ["BROWSER_DOWNLOAD_ROOT"])
    if os.environ.get("BROWSER_DOWNLOAD_ROOT")
    else cookies.STATE_ROOT / "browser-downloads"
)

# A site chooses its own download filename, so it is untrusted input:
# strip every path component and anything that is not plainly a name.
_SAFE_NAME_RX = re.compile(r"[^A-Za-z0-9._-]")

# Hosts a session may NEVER be pointed at, however the caller words it.
# A browser inside the pod that can reach these is a server-side request
# forgery engine: the cloud metadata endpoint hands out credentials, the
# cluster API is one hop from everything, and loopback is the sidecar's own
# unauthenticated control API — a session allowed to reach 127.0.0.1 can
# drive itself and read every other session's cookies via export_state.
# Measured 2026-08-24: allow[] accepted every one of these.
_FORBIDDEN_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
    "169.254.169.254", "metadata.google.internal", "metadata",
    "kubernetes.default", "kubernetes.default.svc",
    "kubernetes.default.svc.cluster.local",
})
# Suffixes that are infrastructure, not the open web.
_FORBIDDEN_SUFFIXES = (".localhost", ".local", ".internal", ".svc",
                       ".cluster.local", ".privatae.internal")
# A wildcard must name a REGISTRABLE domain, not a public suffix:
# `*.example.com` is one site's subdomains, `*.com` and `*.co.uk` are every
# site in a registry. Without pulling in a full public-suffix list, the two
# rules that catch the realistic cases are "at least two labels" and a short
# list of the common multi-label suffixes.
_MIN_WILDCARD_LABELS = 2
_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.za", "co.jp", "or.jp",
    "com.br", "com.mx", "com.cn", "co.in", "co.kr", "com.sg",
    "github.io", "herokuapp.com", "vercel.app", "netlify.app",
    "pages.dev", "workers.dev", "azurewebsites.net", "cloudfront.net",
})


def _reject_host(host: str) -> Optional[str]:
    """Why this host may not go in allow[], or None if it may."""
    h = host.strip().lower().rstrip(".")
    if not h:
        return "empty"
    if h in _FORBIDDEN_HOSTS:
        return (f"{h!r} is loopback, link-local or cluster infrastructure — "
                f"a browser pointed there is a request-forgery tool, and "
                f"loopback reaches this sidecar's own unauthenticated API")
    if any(h == suf.lstrip(".") or h.endswith(suf) for suf in _FORBIDDEN_SUFFIXES):
        return f"{h!r} is an internal suffix, not a public host"
    # Bare IPv4/IPv6 literals: no DNS, no certificate identity, and the
    # private ranges are exactly what SSRF wants.
    if re.fullmatch(r"[0-9.]+", h) or ":" in h:
        return f"{h!r} is an IP literal — name the host instead"
    if "." not in h:
        return f"{h!r} has no dot — not a public hostname"
    return None

# Concurrent contexts held open in this sidecar. A cap on the PLATFORM's
# memory, not on the caller's work: recipes may run serially without
# limit, and the idle reap frees a slot automatically.
#
# Why 3 and not a rounder number: the entity container's memory limit is
# 2Gi on the base plan, 4Gi or 8Gi on larger ones. The browser sidecar is not
# in the pod spec yet, so it has no limit of its own to size against —
# until it does, the safe assumption is that it shares a modest ceiling
# with everything else on the pod. A Chromium context with one live page
# is tens of MB on top of a browser process baseline in the hundreds.
# Three is a number that cannot hurt a 2Gi pod; raise it deliberately
# against the sidecar's OWN limit once the operator sets one, not by
# guessing here.
MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "3"))
# An untouched session's context is closed after this long. Its
# storage_state is captured into memory first, so a reap gives the
# browser's RAM back without costing the login.
IDLE_TTL_S = int(os.environ.get("BROWSER_SESSION_IDLE_TTL_S", "1800"))

NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_NAV_TIMEOUT_MS", "30000"))
ACTION_TIMEOUT_MS = int(os.environ.get("BROWSER_ACTION_TIMEOUT_MS", "10000"))
# A wait the caller asks for (a slow report, an emailed code, a queued
# job) is honoured up to the idle TTL: past that the session itself would
# be reaped, so a longer wait cannot mean what it says.
MAX_WAIT_MS = IDLE_TTL_S * 1000


def _wait_limit_note(asked_ms: int) -> str:
    return (f"asked for {asked_ms}ms; a wait is limited to {MAX_WAIT_MS}ms, the "
            f"session idle timeout — call again to keep waiting")

# Snapshot bounds. The caller is usually paying a model per token for
# this payload, so the default is "enough to act on", not "the page".
MAX_ELEMENTS = 200
MAX_NAME_CHARS = 120
MAX_TEXT_CHARS = 20000

_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

# app.py registers this during lifespan so this module never imports app
# (which would be circular). Returns the live Browser, or None pre-boot.
_browser_provider: Optional[Callable[[], Any]] = None


def set_browser_provider(fn: Callable[[], Any]) -> None:
    """Register the callable that yields the live Playwright Browser."""
    global _browser_provider
    _browser_provider = fn


def _browser():
    b = _browser_provider() if _browser_provider is not None else None
    if b is None:
        raise HTTPException(status_code=503, detail="chromium not ready")
    return b


# ── session registry ────────────────────────────────────────────────


@dataclass
class Session:
    name: str
    allow: tuple[str, ...]
    persist: bool
    context: Any = None
    page: Any = None            # the ACTIVE page
    pages: list = field(default_factory=list)   # active + any popups
    # The live storage_state (cookies + localStorage), held in RAM ONLY.
    # Supplied by the caller from the vault on create, refreshed on close
    # or idle reap, and handed back on export_state. Never written to disk.
    state: Optional[dict] = None
    # Files the site handed us, saved to DOWNLOAD_ROOT/<session>/. The
    # deliverable of a "export my statement" recipe.
    downloads: list = field(default_factory=list)
    # Set once a brokered secret has been typed in this session. Surfaced
    # in info() so an operator can see it happened; the value never is.
    secret_typed: bool = False
    # ref -> ElementHandle, held HERE and never in the page. See _snapshot.
    refs: dict = field(default_factory=dict)
    # In-flight download captures, drained before the context closes.
    download_tasks: set = field(default_factory=set)
    created_ms: int = 0
    last_used_ms: int = 0
    navigations: int = 0
    blocked: int = 0
    # One action at a time per session. Two concurrent `act` calls share
    # one Page: a navigate landing mid-click, or two clicks resolving
    # refs against different DOMs, corrupts state in a way that is
    # invisible in the response. Serialising per session is the only
    # honest option — the Page is genuinely single-threaded.
    lock: Any = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used_ms = int(time.time() * 1000)

    def prune_pages(self) -> None:
        """Drop closed pages; keep `page` pointing at something live."""
        self.pages = [p for p in self.pages if not p.is_closed()]
        if self.page is not None and self.page.is_closed():
            self.page = self.pages[-1] if self.pages else None

    def info(self) -> dict:
        return {
            "name": self.name,
            "allow": list(self.allow),
            "persist": self.persist,
            "created_ms": self.created_ms,
            "last_used_ms": self.last_used_ms,
            "navigations": self.navigations,
            "blocked_navigations": self.blocked,
            "open": self.context is not None,
            "pages_open": len(self.pages),
            "url": self.page.url if self.page is not None else None,
            # Whether a login is currently held in memory, never the blob
            # itself — info() is a status surface, not a credential one.
            "has_state": self.state is not None,
            "state_cookies": len((self.state or {}).get("cookies") or []),
            "downloads": len(self.downloads),
            "secret_typed": self.secret_typed,
        }


_sessions: dict[str, Session] = {}
_lock = asyncio.Lock()


def _validate_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _NAME_RX.match(n):
        raise HTTPException(
            status_code=400,
            detail="name must be 2-40 chars of lowercase letters, digits and "
                   "hyphens, starting and ending alphanumeric",
        )
    return n


def _host_allowed(host: Optional[str], patterns: tuple[str, ...]) -> bool:
    """Exact host, or `*.base` matching base and any subdomain.

    create_session refuses a bare `*`, so this only ever returns True
    for a host the caller named.
    """
    h = (host or "").lower()
    if not h:
        return False
    for raw in patterns:
        p = raw.lower().strip()
        if p.startswith("*."):
            # Subdomains ONLY — `*.example.com` does NOT match the apex
            # `example.com`. This follows the convention every other
            # allow-list here uses (TLS wildcards, cookie domains), and
            # the safe direction is the narrow one: a caller who wants
            # both lists both. An earlier revision matched the apex too,
            # which silently widened every wildcard by one host.
            if h.endswith("." + p[2:]):
                return True
        elif h == p:
            return True
    return False


async def _close(s: Session, save: bool) -> None:
    """Close the context, optionally capturing its state into RAM first.

    `save` captures to s.state — it does NOT write anywhere. The caller
    collects it with export_state and seals it in the vault.
    """
    # Drain downloads FIRST. Playwright deletes the artifact when the
    # context closes, so closing over an in-flight capture loses the file —
    # and the file is the entire point of an "export my statement" recipe.
    if s.download_tasks:
        log.info("session %s: draining %d in-flight download(s) before close",
                 s.name, len(s.download_tasks))
        try:
            await asyncio.wait(set(s.download_tasks), timeout=30)
        except Exception as e:
            log.warning("session %s: download drain failed: %s", s.name, e)
    await _clear_refs(s)
    if save and s.context is not None:
        try:
            s.state = await s.context.storage_state()
        except Exception as e:
            log.warning("could not capture state for %s: %s", s.name, e)
    for attr in ("page", "context"):
        obj = getattr(s, attr)
        if obj is None:
            continue
        try:
            await obj.close()
        except Exception as e:
            log.warning("error closing %s for %s: %s", attr, s.name, e)
        setattr(s, attr, None)


async def _reap_idle() -> None:
    """Close contexts untouched for longer than IDLE_TTL_S.

    The Session row stays in the registry with page=None; _require
    reopens it from the saved state on next use.
    """
    now = int(time.time() * 1000)
    for s in list(_sessions.values()):
        if s.context is None or now - s.last_used_ms < IDLE_TTL_S * 1000:
            continue
        if s.lock.locked():
            continue  # an action is in flight; reap it on the next tick
        log.info("reaping idle session %s (idle %ds)",
                 s.name, (now - s.last_used_ms) // 1000)
        await _close(s, save=s.persist)


async def _open_context(s: Session) -> None:
    """Create the context + page for a session, with the nav guard wired.

    Note what is NOT here versus app._create_context_and_page: no
    expose_binding calls, no polyfill init script. A session page has no
    route to the entity filesystem.
    """
    # Playwright accepts storage_state as a dict as well as a path
    # (Union[StorageState, str, Path]) — the dict form is what keeps this
    # off disk. Log the cookie COUNT, never the contents.
    if s.state:
        log.info("session %s restoring storage_state from memory (%d cookies)",
                 s.name, len((s.state.get("cookies") or [])))

    s.context = await _browser().new_context(
        viewport={"width": 1440, "height": 900},
        storage_state=s.state or None,
        # A recipe that exports a statement needs the download to land
        # somewhere. Off by default in Playwright, so a download would
        # silently go nowhere — which is precisely the mechanism the
        # bank/statement use case depends on.
        accept_downloads=True,
    )
    s.context.set_default_timeout(ACTION_TIMEOUT_MS)
    s.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)

    # ARITY IS LOAD-BEARING. Playwright 1.48.0 ALWAYS invokes a route
    # handler with two positional args — _impl/_helper.py:334 does
    # `self.handler(route, route.request)` in the async branch (and :323
    # in the sync one), with no arity adaptation anywhere. The public
    # type hint advertises Callable[[Route], Any] as well, but in Python
    # that annotation is a lie: a one-arg handler raises TypeError on the
    # FIRST request and the guard never runs. `*_rest` accepts either
    # calling convention so this cannot rot if the pin moves.
    async def _guard(route, *_rest) -> None:
        req = route.request
        # Navigation requests only — see the module docstring. Using
        # is_navigation_request() rather than resource_type=="document"
        # because it is the property we actually mean, and it excludes
        # non-navigation document fetches.
        if not req.is_navigation_request():
            await route.continue_()
            return
        parsed = urlparse(req.url)
        if parsed.scheme not in ("http", "https"):
            s.blocked += 1
            log.warning("session %s blocked non-http navigation %s", s.name, req.url)
            await route.abort("blockedbyclient")
            return
        if _host_allowed(parsed.hostname, s.allow):
            await route.continue_()
            return
        # A cross-origin SUB-FRAME (an iframed IdP, a payment widget) is
        # a navigation too. Blocking it silently breaks real logins in a
        # way that looks like the site is broken, so it is called out
        # separately in the log and counted the same. If a recipe needs
        # one, the caller lists that host in allow[] — an explicit,
        # auditable decision rather than a hole opened by default.
        is_subframe = False
        try:
            is_subframe = req.frame is not None and req.frame.parent_frame is not None
        except Exception:
            pass
        s.blocked += 1
        log.warning("session %s blocked %s navigation to %s (allow=%s)",
                    s.name, "sub-frame" if is_subframe else "top-level",
                    parsed.hostname, list(s.allow))
        await route.abort("blockedbyclient")

    await s.context.route("**/*", _guard)

    # Popups. window.open / target=_blank creates a NEW Page in this same
    # context — the route guard already covers it (_impl/_page.py:314
    # falls through to the context handler), but without tracking it the
    # caller cannot SEE or drive it, and an OAuth popup login simply
    # dead-ends. Track them so `switch_page` can reach one.
    # Reset first: on a reopen after an idle reap this list still holds
    # the dead pages from the previous context.
    s.pages = []

    def _invalidate_refs(frame) -> None:
        # framenavigated fires for same-document (History API) navigation
        # too, which is the case that matters: a SPA route change issues no
        # request, so the route guard never runs and the old ref map stayed
        # live against a DOM that had been replaced.
        #
        # MAIN FRAME ONLY. Firing on every frame meant a rotating ad iframe
        # dropped the map continuously on a busy page, and the caller could
        # not keep a ref alive long enough to use it. Refs into a SUBframe
        # that navigates are still safe: _element asks the DOM whether the
        # node isConnected and returns 409 if it does not, so a stale
        # subframe ref fails closed rather than acting on the wrong page.
        try:
            if frame is not None and frame.parent_frame is not None:
                return
        except Exception:
            pass
        if s.refs:
            log.info("session %s: dropping %d refs (frame navigated to %s)",
                     s.name, len(s.refs), (frame.url or "?")[:80])
            s.refs = {}

    def _on_page(p) -> None:
        s.pages.append(p)
        p.on("framenavigated", _invalidate_refs)
        # Downloads are a PAGE event, not a context one (verified against
        # 1.48: Page carries the "download" overload, BrowserContext has
        # none), so every page needs its own listener — including popups,
        # which is often where an export actually fires.
        # Tracked, not fire-and-forget: Playwright deletes the artifact when
        # the context closes, so a statement still being written at shutdown
        # or at an idle reap was silently lost. _close drains these first.
        def _dl(d):
            t = asyncio.create_task(_capture_download(s, d))
            s.download_tasks.add(t)
            t.add_done_callback(s.download_tasks.discard)
        p.on("download", _dl)
        log.info("session %s: new page -> %d open", s.name, len(s.pages))

    s.context.on("page", _on_page)

    # The handler above fires for this one too, so s.pages becomes [page].
    s.page = await s.context.new_page()


def _live_count() -> int:
    return sum(1 for s in _sessions.values() if s.context is not None)


def live_count() -> int:
    """Open contexts, for /health. Public because app.py reports it."""
    return _live_count()


def _ensure_capacity() -> None:
    """Refuse to open an (N+1)th context.

    Called on BOTH paths that open one — create, and the reopen inside
    _require. Checking only at create was a real hole: create MAX, let
    them reap, create MAX more, then touch the first batch and every one
    of them silently reopens. The cap is a memory guard; a guard that
    only counts the front door is not one.
    """
    # No `excluding` parameter: both callers reach here only when the
    # session in question has context=None, so it never counts itself.
    live = _live_count()
    if live >= MAX_SESSIONS:
        raise HTTPException(
            status_code=429,
            detail=f"{MAX_SESSIONS} browser contexts already open; close one "
                   f"or wait for an idle reap ({IDLE_TTL_S}s)")


def _safe_filename(raw: str, fallback: str) -> str:
    """A filename the SITE chose, made safe to join onto a directory."""
    base = os.path.basename(raw or "").strip() or fallback
    base = _SAFE_NAME_RX.sub("_", base).lstrip(".") or fallback
    return base[:120]


async def _capture_download(s: Session, download) -> None:
    """Save a download into the session's directory on the entity PVC.

    Runs as a task off the page's download event, so it must never raise
    into the event loop — a failed download is recorded and reported, not
    thrown. Playwright deletes the temp file when the context closes, so
    saving eagerly is the only way to keep it.
    """
    entry: dict[str, Any] = {"ms": int(time.time() * 1000)}
    try:
        entry["url"] = download.url
        name = _safe_filename(download.suggested_filename, f"download-{entry['ms']}")
        dest_dir = DOWNLOAD_ROOT / s.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        await download.save_as(str(dest))
        failure = await download.failure()
        if failure:
            entry.update({"ok": False, "error": failure, "filename": name})
        else:
            entry.update({"ok": True, "filename": name, "path": str(dest),
                          "bytes": dest.stat().st_size if dest.exists() else None})
            log.info("session %s: saved download %s (%s bytes)",
                     s.name, name, entry["bytes"])
    except Exception as e:            # noqa: BLE001 — must not escape the task
        entry.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        log.warning("session %s: download failed: %s", s.name, e)
    s.downloads.append(entry)


async def _require(name: str) -> Session:
    n = _validate_name(name)
    s = _sessions.get(n)
    if s is None:
        raise HTTPException(status_code=404, detail=f"no such session: {n}")
    if s.context is None:
        # Reaped while idle — bring it back with its saved state, but only
        # if there is room for another context RIGHT NOW.
        #
        # The check and the open must be atomic. _ensure_capacity is sync
        # and _open_context awaits, so without the lock every concurrent
        # caller passed the same check before any of them created anything:
        # measured 2026-08-24, four concurrent acts on one reaped session
        # produced four contexts and left three orphaned with nothing
        # referencing them (the reaper only walks _sessions, so they were
        # never collected), and six reaped sessions acting at once produced
        # six live contexts against a cap of three with zero 429s.
        async with _lock:
            if s.context is None:              # re-check under the lock
                _ensure_capacity()
                await _open_context(s)
    s.prune_pages()
    if s.page is None:
        raise HTTPException(
            status_code=409,
            detail=f"session {n} has no open page (all were closed)")
    s.touch()
    return s


# ── the in-page snapshot ────────────────────────────────────────────

# ── the in-page snapshot ────────────────────────────────────────────

# Collects the interactive elements of ONE frame and returns them together
# with the live DOM nodes. The nodes come back as a JSHandle array which
# the Python side converts into ElementHandles it keeps ITSELF — see
# _snapshot, which runs this per frame and merges the results.
#
# The ref map used to live in a page global, in the page's own
# world. A page could therefore rewrite it after the snapshot: the guard
# would inspect a genuine password input while fill() wrote to the
# attacker's sink. Demonstrated end to end on 2026-08-24. Anything the
# page can reach is not a place to keep the mapping that decides where a
# credential goes, so it is now a dict in this process and the page has no
# handle on it at all.
_SNAPSHOT_JS = r"""
(limits) => {
  const {maxElements, maxName} = limits;
  const offset = limits.offset || 0;
  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="radio"]',
    '[role="tab"]', '[role="menuitem"]', '[role="option"]', '[role="switch"]',
    '[role="textbox"]', '[role="combobox"]', '[contenteditable="true"]'
  ].join(',');

  const clip = (s) => {
    if (!s) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    return s.length > maxName ? s.slice(0, maxName) + '\u2026' : s;
  };

  // NEVER fall through to el.value, and never take innerText from a field.
  // An <input> has innerText '', so `... || el.innerText || el.value` hands
  // the FIELD CONTENTS back as the element's "name" — for a password box
  // with no aria-label (a <label for=…> plus a bare input, the canonical
  // accessible login form) that is the password itself, inside the payload
  // built to be shown to a model. Measured 2026-08-24 against real
  // Chromium. The submit case is kept: there .value genuinely is the label.
  const nameOf = (el) => clip(
    el.getAttribute('aria-label') ||
    labelText(el) ||
    el.getAttribute('placeholder') ||
    el.getAttribute('alt') ||
    el.getAttribute('title') ||
    (el.tagName === 'INPUT' && el.type === 'submit' ? el.value : '') ||
    (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ||
     el.isContentEditable ? '' : el.innerText) ||
    ''
  );

  // <label for=x> is how most real login forms name their fields, and
  // without it a password box reads as name:"" and is indistinguishable
  // from every other unlabelled input.
  const labelText = (el) => {
    try {
      if (el.labels && el.labels.length) return el.labels[0].innerText;
      const id = el.getAttribute('id');
      if (id) {
        const root = el.getRootNode();
        const l = root.querySelector('label[for="' + CSS.escape(id) + '"]');
        if (l) return l.innerText;
      }
      const anc = el.closest && el.closest('label');
      if (anc) return anc.innerText;
    } catch (e) { /* CSS.escape or closest unavailable */ }
    return '';
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const it = (el.type || 'text').toLowerCase();
      if (it === 'checkbox') return 'checkbox';
      if (it === 'radio') return 'radio';
      if (it === 'submit' || it === 'button') return 'button';
      return 'textbox';
    }
    return 'generic';
  };

  // The old filter checked the element's OWN computed style and its own
  // rect. Measured 2026-08-24: it passed 7 of 9 genuinely invisible
  // elements — opacity:0 on a PARENT (opacity does not inherit as a
  // computed value), off-screen, clipped, covered. That is the delivery
  // mechanism for a decoy nobody watching the screen can see.
  // checkVisibility does the cascade properly where it exists; the rect
  // test then rejects what is merely parked off-screen.
  const visible = (el) => {
    try {
      if (el.checkVisibility && !el.checkVisibility(
            {checkOpacity: true, checkVisibilityCSS: true})) return false;
    } catch (e) { /* older engine: fall through to the style checks */ }
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    if (parseFloat(cs.opacity || '1') < 0.05) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const vw = innerWidth || 1440, vh = innerHeight || 900;
    // Off-screen in any direction. A generous margin keeps elements just
    // below the fold — which a recipe scrolls to — in the list.
    if (r.bottom < -vh || r.top > vh * 3) return false;
    if (r.right < 0 || r.left > vw) return false;
    return true;
  };

  // querySelectorAll does not pierce shadow roots, so a login form built
  // out of web components was entirely invisible. Walk them.
  const collect = (root, out, depth) => {
    if (depth > 8) return;
    let els = [];
    try { els = Array.from(root.querySelectorAll(SEL)); } catch (e) { return; }
    for (const el of els) out.push(el);
    let all = [];
    try { all = Array.from(root.querySelectorAll('*')); } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) collect(el.shadowRoot, out, depth + 1);
    }
  };

  const found = [];
  collect(document, found, 0);

  // Rank before truncating. 250 off-screen filler buttons used to consume
  // the whole budget and push the real login form out of the list; an
  // in-viewport element now outranks one that is merely present.
  const scored = [];
  for (const el of found) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    const inView = r.top >= 0 && r.top < (innerHeight || 900) ? 0 : 1;
    scored.push({el, inView, top: r.top});
  }
  scored.sort((a, b) => a.inView - b.inView || a.top - b.top);
  const chosen = scored.slice(offset, offset + maxElements);
  const truncated = offset + chosen.length < scored.length;

  const items = [], nodes = [];
  let n = 0;
  for (const {el} of chosen) {
    const ref = 'e' + (++n);
    nodes.push(el);
    const item = {ref: ref, role: roleOf(el), name: nameOf(el),
                  tag: el.tagName.toLowerCase()};
    if (el.type) item.type = String(el.type).toLowerCase();
    if (el.disabled) item.disabled = true;
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    if (el.tagName === 'A' && el.href) item.href = clip(el.href);
    if (el.tagName === 'SELECT') {
      item.options = Array.from(el.options).slice(0, 40)
        .map(o => clip(o.value || o.text));
    }
    items.push(item);
  }

  return {
    info: {
      url: location.href,
      title: document.title,
      elements: items,
      element_count: items.length,
      truncated: truncated,
      considered: found.length,
      visible_total: scored.length,
    },
    nodes: nodes,
  };
}
"""

# What KIND of element a ref points at, asked of the DOM rather than of an
# attribute the page chose. `<div contenteditable type="password">` answers
# "password" to getAttribute, and Playwright's fill() writes to a
# contenteditable — that combination defeated the password guard AND the
# screenshot mask at once, since input[type=password] matches no div.
# `isConnected` rides along: a handle to a removed node still resolves, so
# without it a stale ref produced a 10s timeout instead of a clean 409.
_FIELD_INFO_JS = r"""
(el) => ({
  tag: el.tagName,
  type: (el.getAttribute('type') || '').toLowerCase(),
  contentEditable: !!el.isContentEditable,
  idl_type: (typeof el.type === 'string' ? el.type : '').toLowerCase(),
  connected: !!el.isConnected,
})
"""

_TEXT_JS = r"""
(args) => {
  const max = args.max, offset = args.offset || 0;
  const t = (document.body ? document.body.innerText : '') || '';
  const s = t.replace(/\n{3,}/g, '\n\n').trim();
  const end = Math.min(s.length, offset + max);
  const more = end < s.length;
  const out = {text: s.slice(offset, end), truncated: more,
               total_chars: s.length, offset: offset};
  if (more) out.next_offset = end;
  return out;
}
"""

_FIND_TEXT_JS = r"""
(needle) => ((document.body ? document.body.innerText : '') || '')
  .toLowerCase().includes(String(needle).toLowerCase())
"""


async def _clear_refs(s: Session) -> None:
    """Drop the ref map and release the handles behind it.

    ElementHandles pin DOM nodes in the renderer until disposed, so a
    session that snapshots in a loop leaks the whole page each time.
    """
    for h in s.refs.values():
        try:
            await h.dispose()
        except Exception:
            pass
    s.refs = {}


async def _snapshot(s: Session, page, offset: int = 0) -> dict:
    """Snapshot every frame, keeping the ref -> ElementHandle map HERE.

    Frames were invisible before: querySelectorAll on the top document
    never sees into an iframe, so an iframed login form — which is what
    a bank hands you — produced an empty element list and looked like a
    broken page rather than an unsupported one.
    """
    await _clear_refs(s)
    merged: list[dict] = []
    # The page is cut from one ranked sequence (main frame first, then each
    # iframe), so the offset skips what earlier frames hold before this one
    # starts, and the element budget is shared rather than per frame.
    skip = offset
    visible_total = 0
    top: dict = {}
    truncated = False
    considered = 0
    n = 0

    for frame in page.frames:
        limits = {"maxElements": max(0, MAX_ELEMENTS - len(merged)),
                  "maxName": MAX_NAME_CHARS, "offset": skip}
        try:
            handle = await frame.evaluate_handle(_SNAPSHOT_JS, limits)
        except Exception as e:
            # A cross-origin frame we cannot script, or one that navigated
            # mid-snapshot. Skip it rather than fail the whole call.
            log.info("session %s: frame %s not snapshotable: %s",
                     s.name, (frame.url or "?")[:80], type(e).__name__)
            continue
        try:
            info = await (await handle.get_property("info")).json_value()
            nodes = await (await handle.get_property("nodes")).get_properties()
            is_main = frame is page.main_frame
            if is_main:
                top = info
            truncated = truncated or bool(info.get("truncated"))
            considered += int(info.get("considered") or 0)
            frame_total = int(info.get("visible_total") or 0)
            visible_total += frame_total
            skip = max(0, skip - frame_total)
            by_ref = {}
            for _k, jh in nodes.items():
                el = jh.as_element()
                if el is not None:
                    by_ref[len(by_ref)] = el
            for idx, item in enumerate(info.get("elements") or []):
                el = by_ref.get(idx)
                if el is None:
                    continue
                n += 1
                ref = f"e{n}"
                s.refs[ref] = el
                item = dict(item)
                item["ref"] = ref
                if not is_main:
                    item["frame"] = (frame.url or "")[:120]
                merged.append(item)
        finally:
            try:
                await handle.dispose()
            except Exception:
                pass

    out = {
        "url": top.get("url") or page.url,
        "title": top.get("title"),
        "elements": merged,
        "element_count": len(merged),
        "truncated": truncated,
        "considered": considered,
        "frames": len(page.frames),
        "offset": offset,
        "total_elements": visible_total,
    }
    if offset + len(merged) < visible_total:
        out["truncated"] = True
        out["next_offset"] = offset + len(merged)
    return out


async def _element(s: Session, ref: str):
    """Resolve a ref from the map THIS PROCESS holds, and prove it is live.

    Two failures this closes. The map is no longer reachable from the
    page, so a page cannot swap the element behind a ref between the
    snapshot and the action. And a handle to a REMOVED node still
    resolves — `as_element()` is non-null because the handle holds a
    strong reference — so the old code sailed past the staleness check and
    the caller got a 10-second timeout surfaced as a 502 instead of the
    409 the docstring promised.
    """
    el = s.refs.get(ref)
    if el is None:
        raise HTTPException(
            status_code=409,
            detail=f"ref {ref!r} is not in this session's current snapshot — "
                   f"take a fresh one (refs are cleared on every navigation)")
    try:
        info = await el.evaluate(_FIELD_INFO_JS)
    except Exception:
        raise HTTPException(
            status_code=409,
            detail=f"ref {ref!r} no longer exists on the page — take a fresh snapshot")
    if not info.get("connected"):
        raise HTTPException(
            status_code=409,
            detail=f"ref {ref!r} was removed from the page since the snapshot — "
                   f"take a fresh one")
    return el, info


# ── request models ──────────────────────────────────────────────────


class CreateBody(BaseModel):
    name: str = Field(
        description="session name — also the directory downloads land in")
    allow: list[str] = Field(
        description="hosts this session may navigate to: 'app.example.com' "
                    "or '*.example.com'. Required — a session with no "
                    "declared blast radius is refused.")
    persist: bool = Field(
        default=True,
        description="keep the storage_state in memory when the context "
                    "closes or is reaped, so export_state can hand it back "
                    "for sealing; false = drop it on close.")
    storage_state: Optional[dict] = Field(
        default=None,
        description="a previously exported storage_state, unsealed by the "
                    "caller from the vault. Omit to start signed out. This "
                    "is the ONLY way a login is restored — the sidecar "
                    "keeps nothing on disk.")


class ActBody(BaseModel):
    action: str = Field(
        description="navigate | snapshot | click | type | press | select | "
                    "scroll | wait | wait_for_text | text | screenshot | "
                    "type_secret | pages | switch_page | close_page | downloads | "
                    "export_state")
    url: Optional[str] = None
    ref: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    values: Optional[list[str]] = None
    ms: Optional[int] = None
    dy: Optional[int] = None
    index: Optional[int] = None
    offset: int = Field(
        default=0, ge=0,
        description="text and snapshot only: where this page starts (characters "
                    "for text, ranked elements for snapshot). A response that "
                    "has more says so and carries next_offset.")
    submit: bool = False
    clear: bool = True
    allow_non_password: bool = Field(
        default=False,
        description="type_secret only: permit a target that is not "
                    "input[type=password]. Deliberate escape hatch — the "
                    "default refusal is what stops a secret being typed "
                    "into a visible field on a page that asked nicely.")


router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get("/{name}/downloads/{filename}")
async def fetch_download(name: str, filename: str):
    """The bytes of one file the site handed this session.

    DOWNLOAD_ROOT is on THIS container's disk. In the legacy in-pod placement
    that is the entity PVC the kernel also mounts; on the on-demand harness
    lane it is the harness pod's own volume and the kernel cannot read it —
    so a statement exported by an agent run was stranded where nothing could
    collect it. The runner (server/browser_agents.py) fetches it here and
    writes it where the agent said. Confined to the session's own directory:
    the name is reduced to a basename and must be one this session saved.
    """
    from fastapi.responses import FileResponse
    s = _sessions.get(_validate_name(name))
    if s is None:
        raise HTTPException(status_code=404, detail=f"no such session: {name}")
    base = os.path.basename(filename or "")
    if not base or base != filename:
        raise HTTPException(status_code=400, detail="filename must be a bare name")
    known = {d.get("filename") for d in s.downloads if d.get("ok")}
    if base not in known:
        raise HTTPException(status_code=404,
                            detail=f"{base!r} is not a download of session {s.name}")
    path = (DOWNLOAD_ROOT / s.name / base).resolve()
    if path.parent != (DOWNLOAD_ROOT / s.name).resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="file is gone")
    return FileResponse(str(path), filename=base,
                        media_type="application/octet-stream")


@router.get("")
async def list_sessions() -> JSONResponse:
    await _reap_idle()
    return JSONResponse({
        "sessions": [s.info() for s in _sessions.values()],
        "max_sessions": MAX_SESSIONS,
        "idle_ttl_s": IDLE_TTL_S,
        "state_storage": "memory-only; seal via the caller's vault",
    })


@router.post("")
async def create_session(body: CreateBody) -> JSONResponse:
    name = _validate_name(body.name)

    allow = tuple(a.strip().lower() for a in body.allow if a and a.strip())
    if not allow:
        raise HTTPException(
            status_code=400,
            detail="allow[] is required — name the hosts this session may "
                   "reach so the recipe has a declared blast radius")
    for a in allow:
        if a.startswith("*") and not a.startswith("*."):
            raise HTTPException(
                status_code=400,
                detail=f"allow pattern {a!r} is a catch-all; list hosts "
                       f"explicitly or use '*.example.com'")
        is_wild = a.startswith("*.")
        host = a[2:] if is_wild else a
        if "/" in host or "@" in host or "\\" in host:
            raise HTTPException(
                status_code=400,
                detail=f"allow entry {a!r} must be a bare hostname "
                       f"(no scheme, credentials, port or path)")
        why = _reject_host(host)
        if why:
            raise HTTPException(status_code=400,
                                detail=f"allow entry {a!r} refused: {why}")
        # `*.co.uk` or `*.com` is a country, not a site. Requiring a label
        # below the registrable suffix is a blunt rule but it is the right
        # direction: a caller that genuinely wants breadth can list hosts.
        if is_wild and (len(host.split(".")) < _MIN_WILDCARD_LABELS
                        or host in _PUBLIC_SUFFIXES):
            raise HTTPException(
                status_code=400,
                detail=f"wildcard {a!r} is too broad — {host!r} is a public "
                       f"suffix, so this covers every site registered under "
                       f"it. Name the domain (e.g. '*.yourbank{'.' + host if '.' not in host else '.' + host}') "
                       f"or list the hosts.")

    async with _lock:
        await _reap_idle()
        if name in _sessions:
            raise HTTPException(status_code=409, detail=f"session exists: {name}")
        _ensure_capacity()

        now = int(time.time() * 1000)
        s = Session(name=name, allow=allow, persist=body.persist,
                    state=body.storage_state or None,
                    created_ms=now, last_used_ms=now)
        await _open_context(s)
        _sessions[name] = s

    log.info("session %s created (allow=%s persist=%s)",
             name, list(allow), body.persist)
    return JSONResponse(s.info(), status_code=201)


@router.get("/{name}")
async def get_session(name: str) -> JSONResponse:
    s = await _require(name)
    return JSONResponse(s.info())


@router.delete("/{name}")
async def delete_session(name: str, save: bool = True) -> JSONResponse:
    n = _validate_name(name)
    s = _sessions.get(n)
    if s is None:
        raise HTTPException(status_code=404, detail=f"no such session: {n}")
    will_save = save and s.persist
    # Close BEFORE dropping the registry entry: popping first meant a
    # throw inside _close leaked the context with nothing left pointing
    # at it. The finally guarantees the entry goes either way.
    try:
        await _close(s, save=will_save)
    finally:
        _sessions.pop(n, None)
    return JSONResponse({"ok": True, "closed": n, "state_saved": will_save})


@router.post("/{name}/act")
async def act(name: str, body: ActBody) -> JSONResponse:
    """The single action surface: one endpoint, a named action.

    Every branch returns the same envelope — ok, action, url, title —
    plus whatever that action produced.
    """
    s = await _require(name)
    a = (body.action or "").strip().lower()
    # Serialise: one action at a time per session (see Session.lock).
    async with s.lock:
        return await _act_locked(s, a, body)


async def _act_locked(s: Session, a: str, body: ActBody) -> JSONResponse:
    s.prune_pages()
    page = s.page
    if page is None:
        raise HTTPException(409, f"session {s.name} has no open page")
    out: dict[str, Any] = {"ok": True, "action": a}

    try:
        if a == "navigate":
            if not body.url:
                raise HTTPException(400, "navigate requires url")
            # TWO parsers, ONE decision — the classic allow-list bypass.
            # Python's urlparse reads `https://evil.test\\@app.example.com/`
            # as host app.example.com (allowed); Chromium reads it as
            # evil.test and goes there. Verified against real Chrome
            # 2026-08-24. Rather than try to out-parse the browser, refuse
            # the characters the two disagree about. The route guard is the
            # backstop, but a guard that only ever fires on a bypass is not
            # a design, it is luck.
            if "\\" in body.url or "@" in urlparse(body.url).netloc:
                raise HTTPException(
                    400,
                    "url contains a backslash or embedded credentials — refused "
                    "because Python and Chromium disagree about which host that "
                    "names, and the allow-list decision must not depend on which "
                    "parser answers")
            parsed = urlparse(body.url)
            if parsed.scheme not in ("http", "https"):
                raise HTTPException(400, "url must be http or https")
            if not _host_allowed(parsed.hostname, s.allow):
                raise HTTPException(
                    403,
                    f"{parsed.hostname} is not in this session's allow list "
                    f"{list(s.allow)}")
            # A heavy page can take longer than the nav budget to fire its
            # `load` event (every image, font and tracker through the egress
            # proxy) while being fully usable once the document is parsed.
            # So the budget buys DOMContentLoaded first, and `load` is
            # waited for only with what is left of it. A page that parsed
            # but never finished loading is reported as navigated, with
            # load_event false, instead of failing a page the caller can use.
            t0 = time.monotonic()
            resp = await page.goto(body.url, wait_until="domcontentloaded",
                                   timeout=NAV_TIMEOUT_MS)
            s.navigations += 1
            out["status"] = resp.status if resp else None
            left_ms = int(NAV_TIMEOUT_MS - (time.monotonic() - t0) * 1000)
            load_event = False
            if left_ms > 0:
                try:
                    await page.wait_for_load_state("load", timeout=left_ms)
                    load_event = True
                except Exception as le:
                    if type(le).__name__ != "TimeoutError":
                        raise
            out["load_event"] = load_event
            if not load_event:
                out["note"] = (
                    f"the page's document loaded but its load event had not "
                    f"fired within {NAV_TIMEOUT_MS}ms; images or scripts may "
                    f"still be arriving — snapshot again or wait_for_text")

        elif a == "snapshot":
            out.update(await _snapshot(s, page, body.offset))

        elif a == "click":
            if not body.ref:
                raise HTTPException(400, "click requires ref")
            el, _info = await _element(s, body.ref)
            await el.click(timeout=ACTION_TIMEOUT_MS)
            out["clicked"] = body.ref

        elif a == "type":
            if not body.ref or body.text is None:
                raise HTTPException(400, "type requires ref and text")
            el, _info = await _element(s, body.ref)
            if body.clear:
                await el.fill("", timeout=ACTION_TIMEOUT_MS)
            await el.type(body.text, timeout=ACTION_TIMEOUT_MS)
            if body.submit:
                await el.press("Enter", timeout=ACTION_TIMEOUT_MS)
            out["typed_into"] = body.ref
            out["submitted"] = body.submit

        elif a == "type_secret":
            # The vault broker's landing point. The KERNEL resolves the
            # credential and calls this; the recipe and the model only
            # ever named it. Three differences from `type`, all of them
            # the reason this is a separate action rather than a flag:
            #
            #  - the target must actually be a password field. The model
            #    still chooses WHICH element, so a hostile page could
            #    offer a plausible-looking box that is not one and
            #    harvest the secret in the clear. Refusing anything but
            #    input[type=password] closes the realistic version of
            #    that; allow_non_password is the deliberate escape hatch
            #    for sites that do something stranger.
            #  - nothing about the value is logged or echoed. The
            #    response says which ref was filled, never what with.
            #  - no `submit` shortcut. Pressing Enter is a separate,
            #    visible step, so a secret type can never also be the
            #    thing that performs the action.
            if not body.ref or body.text is None:
                raise HTTPException(400, "type_secret requires ref and text")
            el, info = await _element(s, body.ref)
            # A REAL <input type=password>, judged by the DOM rather than by
            # an attribute the page chose. Anything contenteditable is
            # refused outright even with the override: the screenshot mask
            # is `input[type=password]`, so a contenteditable secret is
            # unmaskable, and the renderer does not dot it either.
            is_password_input = (info.get("tag") == "INPUT"
                                 and info.get("idl_type") == "password")
            field_type = info.get("idl_type") or info.get("type") or ""
            if info.get("contentEditable"):
                raise HTTPException(
                    409,
                    "refusing to type a secret into a contenteditable element: "
                    "it cannot be masked in a screenshot (the mask selector is "
                    "input[type=password]) and the renderer will not obscure it. "
                    "This is refused even with allow_non_password.")
            if not is_password_input and not body.allow_non_password:
                raise HTTPException(
                    409,
                    f"refusing to type a secret into <{(info.get('tag') or '?').lower()}"
                    f"{' type=' + field_type if field_type else ''}> — expected a real "
                    f"input[type=password]. Take a fresh snapshot and target the "
                    f"password box, or pass allow_non_password if the site genuinely "
                    f"uses something else.")
            if body.clear:
                await el.fill("", timeout=ACTION_TIMEOUT_MS)
            await el.fill(body.text, timeout=ACTION_TIMEOUT_MS)
            s.secret_typed = True
            log.info("session %s: secret typed into %s (field_type=%s)",
                     s.name, body.ref, field_type or "-")
            out["typed_into"] = body.ref
            out["field_type"] = field_type

        elif a == "press":
            if not body.key:
                raise HTTPException(400, "press requires key")
            await page.keyboard.press(body.key)
            out["pressed"] = body.key

        elif a == "select":
            if not body.ref or not body.values:
                raise HTTPException(400, "select requires ref and values")
            el, _info = await _element(s, body.ref)
            picked = await el.select_option(body.values, timeout=ACTION_TIMEOUT_MS)
            out["selected"] = picked

        elif a == "scroll":
            dy = body.dy if body.dy is not None else 600
            await page.mouse.wheel(0, dy)
            out["scrolled_by"] = dy

        elif a == "wait":
            want = int(body.ms or 1000)
            ms = max(0, min(want, MAX_WAIT_MS))
            await page.wait_for_timeout(ms)
            out["waited_ms"] = ms
            if want > MAX_WAIT_MS:
                out["ms_limited"] = _wait_limit_note(want)

        elif a == "wait_for_text":
            if not body.text:
                raise HTTPException(400, "wait_for_text requires text")
            want = int(body.ms) if body.ms else ACTION_TIMEOUT_MS
            budget = max(0, min(want, MAX_WAIT_MS))
            limited = _wait_limit_note(want) if want > MAX_WAIT_MS else ""
            deadline = time.monotonic() + (budget / 1000)
            found = False
            while True:
                if await page.evaluate(_FIND_TEXT_JS, body.text):
                    found = True
                    break
                if time.monotonic() >= deadline:
                    break
                await page.wait_for_timeout(250)
            if not found:
                raise HTTPException(
                    408,
                    f"text {body.text!r} did not appear within "
                    f"{budget}ms" + (f" ({limited})" if limited else ""))
            out["found"] = True
            if limited:
                out["ms_limited"] = limited

        elif a == "text":
            out.update(await page.evaluate(
                _TEXT_JS, {"max": MAX_TEXT_CHARS, "offset": body.offset}))

        elif a == "screenshot":
            # Password fields are ALWAYS masked, not just after a secret
            # was typed here. A screenshot is the quietest leak path in
            # this whole surface: the image goes back to a caller and
            # usually onward to a model, and a rendered credential in it
            # is invisible to every other guard. Masking unconditionally
            # costs nothing and removes the class.
            # One locator per FRAME. page.locator() matches the main frame
            # only, and this file's own thesis is that bank logins live in
            # iframes — so the main-frame-only mask missed exactly the case
            # it was written for. Defence in depth either way: the renderer
            # dots a real password input, so glyphs never appear. The mask
            # is for the field that is not what it claims to be.
            masks = []
            for fr in page.frames:
                try:
                    masks.append(fr.locator("input[type=password]"))
                except Exception:
                    continue
            png = await page.screenshot(type="png", full_page=False, mask=masks)
            out["png_base64"] = base64.b64encode(png).decode("ascii")
            out["bytes"] = len(png)
            out["masked"] = "input[type=password]"
            out["masked_frames"] = len(masks)

        elif a == "pages":
            out["pages"] = [{"index": i, "url": p.url,
                             "active": p is s.page}
                            for i, p in enumerate(s.pages)]

        elif a == "switch_page":
            # An OAuth popup is a new Page in this context. It is already
            # covered by the route guard, but until the caller can make it
            # the active page the login simply dead-ends.
            if body.index is None:
                raise HTTPException(400, "switch_page requires index")
            if not 0 <= body.index < len(s.pages):
                raise HTTPException(
                    404, f"no page at index {body.index} "
                         f"({len(s.pages)} open; use action=pages)")
            s.page = s.pages[body.index]
            page = s.page
            out["switched_to"] = body.index

        elif a == "close_page":
            if len(s.pages) <= 1:
                raise HTTPException(409, "cannot close the last page")
            await page.close()
            s.prune_pages()
            page = s.page
            out["pages_open"] = len(s.pages)

        elif a == "downloads":
            # The deliverable of an "export my statement" recipe: what the
            # site handed over, and where it landed on the PVC so the
            # kernel can pick it up.
            out["downloads"] = list(s.downloads)
            out["download_dir"] = str(DOWNLOAD_ROOT / s.name)

        elif a == "export_state":
            # The ONE action that returns credential-bearing material, and
            # it exists so the caller can seal it in the vault. The blob is
            # every cookie in the session: treat the response as a secret,
            # never log it, never show it to a model.
            s.state = await s.context.storage_state()
            out["storage_state"] = s.state
            out["cookies"] = len(s.state.get("cookies") or [])

        else:
            raise HTTPException(400, f"unknown action: {a}")

    except HTTPException:
        raise
    except Exception as e:
        # A blocked document navigation surfaces here as a Playwright
        # error; name it rather than leaking the raw driver message.
        msg = str(e)
        if "blockedbyclient" in msg.lower() or "ERR_BLOCKED_BY_CLIENT" in msg:
            raise HTTPException(
                403,
                f"navigation blocked — the page tried to reach a host outside "
                f"this session's allow list {list(s.allow)}")
        raise HTTPException(502, f"{a} failed: {type(e).__name__}: {e}")

    out["url"] = page.url
    try:
        out["title"] = await page.title()
    except Exception:
        out["title"] = None
    s.touch()
    return JSONResponse(out)


# ── the reaper ──────────────────────────────────────────────────────

_reaper: Optional[asyncio.Task] = None


async def _reaper_loop() -> None:
    """Enforce IDLE_TTL_S on a timer.

    _reap_idle used to run only inside create_session and list_sessions,
    which means a caller that creates one session and then never calls
    either again holds its Chromium context forever. The TTL was
    advertised in the API and enforced by nothing. A tick at a quarter of
    the TTL bounds the overshoot.
    """
    interval = max(30, IDLE_TTL_S // 4)
    while True:
        try:
            await asyncio.sleep(interval)
            await _reap_idle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reaper tick failed")  # never let one tick kill the loop


def start_reaper() -> None:
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reaper_loop())
        log.info("session reaper started (ttl=%ds)", IDLE_TTL_S)


async def shutdown_all() -> None:
    """Close every session at process shutdown, saving persisted state."""
    global _reaper
    if _reaper is not None:
        _reaper.cancel()
        try:
            await _reaper
        except (asyncio.CancelledError, Exception):
            pass
        _reaper = None
    for name in list(_sessions):
        s = _sessions.pop(name)
        await _close(s, save=s.persist)
