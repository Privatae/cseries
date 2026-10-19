"""Render one self-contained page and measure the frame it mounts.

The browser half of the desktop RENDER check (2026-09-30). The kernel's desktop
floor (server/applications/desktop_contract.py check_document) reads a design
as TEXT. On one Host, five desktop designs a coding harness built were
published to the shared Designs shelf without ever being rendered: the
harness images have no browser, their acceptance checks were shell greps, and
the publish and set-desktop doors ran only that text floor. A document can
pass every text rule and still throw on its first animation tick or paint
nothing at all.

So the kernel asks the one real browser the platform already runs. It sends a
complete page it composed itself (the shared sandbox CSP and the desktop
stand-in bridge it serves, mounting the document the way the Designs shelf
does — server/applications/desktop_render.py). This route loads that page,
waits for a real readiness signal, and reports what the browser saw. It does
NOT judge: the desktop contract lives in the kernel, which reads this report
against it, so a contract change never needs a new sidecar image.

What it reads:
  - uncaught exceptions from every frame (`pageerror`, CDP
    Runtime.exceptionThrown), with the first stack location — the document's
    line, since the stand-in and the policy are injected without a newline;
  - console errors, through CDP like the exceptions (reported; the kernel
    judges which matter — a blocked font is not a crash, but the stand-in
    bridge logs an exception its CSeriesDesktop.on swallowed here);
  - the page CRASHING (a renderer that dies: out of memory, a crash) and the
    subject frame NAVIGATING ITSELF after its document loaded (a reload, a
    jump to about:blank or anywhere) — in the shell either one takes the
    desktop away, so each is reported with what is known, never as a 500;
  - after `load` + two animation frames inside the subject frame + a settle:
    the subject frame's pixels (how much of it differs from its single most
    common colour), where its visible content sits (text, images, canvases)
    and how much of each of those spots is painted, and per probe how many
    elements match a selector — attached at all, visibly rendered (optionally
    inside a rectangle), or visible AND reachable by a click at their centre.

THE SUBJECT FRAME IS FOUND BY ITS PLACE AND ITS ELEMENT in the page the
kernel composed — the main frame's only child, `iframe#<subject>` — never by
the frame's window.name: the document can set its own window.name, and did,
as a state-persistence idiom.

INDISTINGUISHABLE FROM THE REAL DESKTOP, as far as the document can tell:
the shared browser runs without the automation flag (CHECK_BROWSER_ARGS —
navigator.webdriver reads false, as in a person's browser), and nothing the
check needs lives in the document's world — errors arrive through CDP, the
probes run in an isolated world.

MEASURED FROM OUTSIDE THE DOCUMENT. Every script this route runs in the
subject frame — the readiness wait and the probes — runs in a CDP isolated
world (Page.createIsolatedWorld): the same DOM, but its own globals and its
own prototypes. A document that replaces querySelectorAll,
getBoundingClientRect, getComputedStyle or requestAnimationFrame in its main
world changes nothing these see, so it cannot talk its way past the check.
The pixels come from a screenshot, decoded in a separate blank page.

NO NETWORK. The page arrives in the request body and is fulfilled from
memory at a reserved name that never resolves; every other request the page
or its frame makes is aborted and listed. Unlike /v1/verify there is no
kernel origin to reach — nothing here dials anything. One context per call,
closed on exit, nothing persisted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("browser.document_verify")

router = APIRouter(prefix="/v1/verify/document", tags=["verify"])

# `.invalid` is reserved (RFC 2606): the name can never resolve, so a route
# that somehow fell through to the network would fail rather than reach a host.
PAGE_URL = "http://document-check.invalid/"
# A desktop with inlined fonts and base64 images passes 4 MiB, and the kernel
# puts no size bound of its own on a desktop document, so a lower bound here
# meant such a desktop could never be checked. What stays is a guard for the
# pod's memory (the page is held in this process and again in the browser),
# set well above any real desktop and refused in plain words (HTTP 413).
MAX_PAGE_CHARS = int(os.environ.get("BROWSER_DOCUMENT_MAX_CHARS", str(64 * 1024 * 1024)))
LOAD_TIMEOUT_MS = 20_000
STEP_TIMEOUT_S = 10.0
SETTLE_MS_DEFAULT = 2_000
SETTLE_MS_MAX = 10_000
MAX_PROBES = 16
# A pixel within this distance (per channel, 0-255) of the most common colour
# counts as that colour, so anti-aliasing at the edge of one flat fill does not
# read as content. A gradient or any drawn shape clears it easily.
PAINT_TOLERANCE = 8
_MAX_ITEMS = 20
_MAX_TEXT = 400
_MAX_CONTENT_RECTS = 40
# No new round (a reloaded copy's own first look and settle) starts after this
# many seconds into a check: the steps' own timeouts add up to more than the
# kernel's 150 s call timeout (app_verify.py), and a check that outlives it
# reports nothing.
ROUND_DEADLINE_S = 90.0
_MAX_SELF_RELOADS = 1        # a srcdoc reloading itself ONCE is harmless; a copy that reloads again is a loop
# The shared Chromium is launched with these (control/app.py _boot_browser):
# without the automation flag a page reads navigator.webdriver === false, the
# same as in a person's browser, so a document cannot draw one thing for the
# check and another for the desktop.
CHECK_BROWSER_ARGS = ("--disable-blink-features=AutomationControlled",)

_browser_provider: Optional[Callable[[], Any]] = None


def set_browser_provider(fn: Callable[[], Any]) -> None:
    """app.py hands over `lambda: state.browser` — the shared Chromium."""
    global _browser_provider
    _browser_provider = fn


def _browser():
    b = _browser_provider() if _browser_provider is not None else None
    if b is None:
        raise HTTPException(status_code=503, detail="chromium not ready")
    return b


def _clip(s: Any) -> str:
    t = str(s or "")
    return t if len(t) <= _MAX_TEXT else t[:_MAX_TEXT] + "…"


class Probe(BaseModel):
    selector: str = Field(..., min_length=1, max_length=500)
    # [x, y, width, height] in the subject frame's CSS pixels. Omitted = the
    # whole frame viewport. Ignored by kind 'attached'.
    rect: Optional[list[float]] = Field(None, min_length=4, max_length=4)
    # shown     — how many matches a person could see (a box, not hidden, no
    #             ancestor at opacity 0, inside the rectangle);
    # attached  — how many matches are in the document at all, seen or not
    #             (the shell's own test for tiles and a drawn bar);
    # reachable — {shown, reachable, cover}: of the shown matches, how many a
    #             click at the centre would land on, and what took the click
    #             from the others.
    kind: Literal["shown", "attached", "reachable"] = "shown"


class DocumentBody(BaseModel):
    page: str = Field(..., min_length=1,
                      description="A complete, self-contained HTML page.")
    subject: str = Field("subject", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                         description="The id of the iframe element, in the page, that "
                                     "mounts the document to measure.")
    width: int = Field(1440, ge=320, le=3840)
    height: int = Field(900, ge=240, le=2160)
    settle_ms: int = Field(SETTLE_MS_DEFAULT, ge=0, le=SETTLE_MS_MAX,
                           description="How long to keep watching after the first frames.")
    probes: dict[str, Probe] = Field(default_factory=dict)


# First location in a V8 stack after the "Name: message" line:
#   "    at tick (about:srcdoc:7:3)"  /  "    at about:srcdoc:196:48"
_LOC_RE = re.compile(r"\bat (?:.*?\()?([a-z][\w+.-]*:[^\s()]*?):(\d+):(\d+)\)?\s*$")


def _location(stack: str) -> dict:
    for line in str(stack or "").splitlines()[1:]:
        m = _LOC_RE.search(line.strip())
        if m:
            return {"url": _clip(m.group(1)), "line": int(m.group(2)),
                    "column": int(m.group(3))}
    return {}


# Runs in the SUBJECT frame's isolated world. "Shown" is what a person could
# see: a non-empty box, not visibility:hidden, no ancestor at opacity 0, inside
# the frame viewport (or the probe's rectangle).
_PROBE_JS = """(probes) => {
  const vw = innerWidth, vh = innerHeight, out = {};
  const shown = (el) => {
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (Number(getComputedStyle(n).opacity) === 0) return false;
    }
    return true;
  };
  const label = (el) => {
    if (!el) return 'nothing';
    let t = el.tagName.toLowerCase();
    if (el.id) t += '#' + String(el.id).slice(0, 40);
    const c = el.getAttribute && el.getAttribute('class');
    if (c) t += '.' + String(c).trim().split(/\\s+/)[0].slice(0, 40);
    return t;
  };
  for (const name of Object.keys(probes)) {
    const p = probes[name];
    let els;
    try { els = Array.from(document.querySelectorAll(p.selector)); } catch (e) { out[name] = -1; continue; }
    if (p.kind === 'attached') { out[name] = els.filter((el) => el.isConnected).length; continue; }
    const b = p.rect || [0, 0, vw, vh];
    const vis = [];
    for (const el of els) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.right <= b[0] || r.bottom <= b[1] || r.left >= b[0] + b[2] || r.top >= b[1] + b[3]) continue;
      if (shown(el)) vis.push([el, r]);
    }
    if (p.kind !== 'reachable') { out[name] = vis.length; continue; }
    let reach = 0;
    const covers = {};
    for (const [el, r] of vis) {
      const x = Math.min(Math.max((r.left + r.right) / 2, 0), vw - 1);
      const y = Math.min(Math.max((r.top + r.bottom) / 2, 0), vh - 1);
      const hit = document.elementFromPoint(x, y);
      if (hit && (hit === el || el.contains(hit))) { reach++; continue; }
      const k = label(hit);
      covers[k] = (covers[k] || 0) + 1;
    }
    let cover = null, most = 0;
    for (const k of Object.keys(covers)) if (covers[k] > most) { most = covers[k]; cover = k; }
    out[name] = { shown: vis.length, reachable: reach, cover };
  }
  return out;
}"""

# Runs in the SUBJECT frame's isolated world: where the visible content is —
# text a person could read and images / canvases / video — as rectangles the
# screenshot is then read at. A flat wallpaper with a small clock is not blank;
# a page whose only text is invisible is.
_CONTENT_JS = """(max) => {
  const vw = innerWidth, vh = innerHeight, rects = [];
  let text = 0, media = 0;
  const shown = (el) => {
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (Number(getComputedStyle(n).opacity) === 0) return false;
    }
    return true;
  };
  const add = (r, kind) => {
    const x = Math.max(0, r.left), y = Math.max(0, r.top);
    const w = Math.min(vw, r.right) - x, h = Math.min(vh, r.bottom) - y;
    if (w < 1 || h < 1) return false;
    if (rects.length < max) rects.push([Math.floor(x), Math.floor(y), Math.ceil(w), Math.ceil(h), kind]);
    return true;
  };
  const root = document.body || document.documentElement;
  if (root) {
    const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TITLE']);
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      if (!/\\S/.test(n.nodeValue || '')) continue;
      const el = n.parentElement;
      if (!el || skip.has(el.tagName) || !shown(el)) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      let any = false;
      for (const r of range.getClientRects()) { if (add(r, 'text')) any = true; }
      if (any) text++;
    }
    for (const el of document.querySelectorAll('img,svg,canvas,video,picture,object,embed')) {
      if (!shown(el)) continue;
      if (add(el.getBoundingClientRect(), 'media')) media++;
    }
  }
  return { text, media, rects };
}"""

# Runs in a separate blank page, never in the page under test: the pixels of
# the subject frame's screenshot, decoded by the browser itself.
_PAINT_JS = """async ([b64, tol, rects]) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + b64;
  await img.decode();
  const w = img.naturalWidth, h = img.naturalHeight;
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d', { willReadFrequently: true });
  g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, w, h).data;
  const counts = new Map();
  for (let i = 0; i < d.length; i += 4) {
    const k = (d[i] << 16) | (d[i + 1] << 8) | d[i + 2];
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  let top = 0, key = 0;
  for (const [k, v] of counts) if (v > top) { top = v; key = k; }
  const r = (key >> 16) & 255, gg = (key >> 8) & 255, bb = key & 255;
  let near = 0;
  for (let i = 0; i < d.length; i += 4) {
    if (Math.abs(d[i] - r) <= tol && Math.abs(d[i + 1] - gg) <= tol && Math.abs(d[i + 2] - bb) <= tol) near++;
  }
  const n = w * h;
  // Per content rectangle: how many of its pixels differ from the dominant
  // colour — text drawn in the background's own colour paints none.
  const at = [];
  for (const q of (rects || [])) {
    const x0 = Math.max(0, q[0] | 0), y0 = Math.max(0, q[1] | 0);
    const x1 = Math.min(w, x0 + (q[2] | 0)), y1 = Math.min(h, y0 + (q[3] | 0));
    let c = 0;
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const i = (y * w + x) * 4;
        if (Math.abs(d[i] - r) > tol || Math.abs(d[i + 1] - gg) > tol || Math.abs(d[i + 2] - bb) > tol) c++;
      }
    }
    at.push(c);
  }
  return { width: w, height: h, distinct_colors: counts.size,
           dominant: '#' + key.toString(16).padStart(6, '0'),
           dominant_share: n ? top / n : 1, painted_share: n ? 1 - near / n : 0,
           content_painted_px: at };
}"""

_TWO_FRAMES_JS = ("() => new Promise(r => requestAnimationFrame("
                  "() => requestAnimationFrame(() => r(true))))")

_WORLD = "document-verify"


class _IsolatedWorld:
    """A JS context in the subject frame that the document cannot reach.

    The frame is in its parent's renderer or in its own (Chromium may isolate
    sandboxed frames out of process); a frame with no session of its own is
    addressed through the page's session by its frame id, read from the
    iframe ELEMENT in the page (DOM.describeNode) — never from anything the
    document itself can change."""

    def __init__(self, session, context_id: int):
        self._s = session
        self._ctx = context_id

    @classmethod
    async def open(cls, context, page, frame, subject: str) -> "_IsolatedWorld":
        try:
            session = await context.new_cdp_session(frame)       # out of process
            tree = await session.send("Page.getFrameTree")
            frame_id = tree["frameTree"]["frame"]["id"]
        except Exception:  # noqa: BLE001 — "part of the parent frame's session"
            session = await context.new_cdp_session(page)
            root = await session.send("DOM.getDocument", {"depth": 0})
            found = await session.send("DOM.querySelector", {
                "nodeId": root["root"]["nodeId"], "selector": f"iframe#{subject}"})
            node = (await session.send("DOM.describeNode", {"nodeId": found["nodeId"]}))["node"] \
                if found.get("nodeId") else {}
            frame_id = node.get("frameId")
            if not frame_id:
                raise RuntimeError(f"no iframe#{subject} in the page")
        world = await session.send("Page.createIsolatedWorld",
                                   {"frameId": frame_id, "worldName": _WORLD})
        return cls(session, world["executionContextId"])

    async def gone(self) -> bool:
        """True when this world's document is no longer there — the frame
        committed a NEW document (a reload, a jump anywhere): its isolated
        world went with it. A hash change or history.replaceState keeps the
        same document, and the same world. A renderer that does not answer
        is not "gone" — that is a lock-up, read elsewhere."""
        try:
            await asyncio.wait_for(self._s.send("Runtime.evaluate", {
                "expression": "1", "contextId": self._ctx, "returnByValue": True}), 2.0)
            return False
        except asyncio.TimeoutError:
            return False
        except Exception as e:  # noqa: BLE001
            return _context_destroyed(e)

    async def call(self, fn_js: str, arg: Any = None) -> Any:
        expr = f"({fn_js})({json.dumps(arg)})"
        r = await self._s.send("Runtime.evaluate", {
            "expression": expr, "contextId": self._ctx,
            "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text"))
        return (r.get("result") or {}).get("value")


_DESTROYED = ("Cannot find context", "context was destroyed", "Execution context was destroyed",
              "Frame was detached", "frame was detached",
              # DOM.describeNode on the iframe element, answered after the
              # frame's document was replaced under the read:
              "Could not find node with given id")


def _context_destroyed(e: Any) -> bool:
    text = str(e)
    return any(m in text for m in _DESTROYED)


def _first_line(e: Any) -> str:
    # Playwright appends a multi-line call log to its errors.
    return (str(e).strip().splitlines() or [""])[0]


@router.post("")
async def verify_document(body: DocumentBody) -> JSONResponse:
    """Load `page`, wait for the subject frame's first painted frames, watch
    for `settle_ms`, report. Never raises for what the PAGE does — a crash, a
    self-navigation or a lock-up is the finding, reported with every error
    already seen."""
    if len(body.page) > MAX_PAGE_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"the page is {len(body.page)} characters; this check holds at "
                   f"most {MAX_PAGE_CHARS} to protect the browser's memory, so "
                   f"this desktop was not checked")
    if len(body.probes) > MAX_PROBES:
        raise HTTPException(status_code=400, detail=f"at most {MAX_PROBES} probes")
    browser = _browser()
    t0 = time.monotonic()
    report: dict[str, Any] = {
        "loaded": False, "load_error": None,
        "subject_found": False, "ready": False, "ready_error": None,
        "uncaught": [], "console_errors": [], "blocked": [],
        "paint": None, "paint_error": None, "content": None,
        "probes": {}, "probe_error": None, "unresponsive": None,
        "crashed": None, "navigated": None, "reload_loop": False,
        "reload_load_error": None, "deadline": None, "check_error": None,
        "settle_ms": body.settle_ms,
        "viewport": [body.width, body.height],
        "timings": {},
    }

    def ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    context = await browser.new_context(viewport={"width": body.width, "height": body.height})
    try:
        async def _guard(route, *_rest) -> None:
            req = route.request
            if req.url == PAGE_URL:
                await route.fulfill(status=200, body=body.page,
                                    content_type="text/html; charset=utf-8")
                return
            if len(report["blocked"]) < _MAX_ITEMS:
                report["blocked"].append(_clip(req.url))
            await route.abort("blockedbyclient")

        await context.route("**/*", _guard)
        page = await context.new_page()

        def _on_pageerror(err) -> None:
            if len(report["uncaught"]) >= _MAX_ITEMS:
                return
            stack = str(getattr(err, "stack", "") or "")
            report["uncaught"].append({
                "message": _clip(getattr(err, "message", err)),
                "name": _clip(getattr(err, "name", "") or ""),
                "stack": _clip(stack),
                "at_ms": ms(),
                **_location(stack),
            })

        def _on_console(msg) -> None:
            try:
                if msg.type != "error" or len(report["console_errors"]) >= _MAX_ITEMS:
                    return
                loc = msg.location or {}
                text = str(msg.text or "")
                report["console_errors"].append({
                    "text": _clip(text), "url": _clip(loc.get("url")),
                    "line": loc.get("lineNumber"),
                    # A logged stack (the stand-in bridge logs what a
                    # CSeriesDesktop.on callback threw): where it was thrown.
                    "stack_at": _location(text) or None})
            except Exception:
                pass

        def _on_crash(_page) -> None:
            report["crashed"] = (f"the page crashed {ms() / 1000:.1f} s in — its renderer died "
                                 "(out of memory, or a crash)")

        # Every navigation of a frame in the page, in order. The subject's own
        # first document is about:srcdoc; anything it commits after that is
        # the document navigating itself.
        navs: list = []
        page.on("pageerror", _on_pageerror)
        page.on("console", _on_console)
        page.on("crash", _on_crash)
        page.on("framenavigated", lambda f: navs.append((f, f.url)))

        try:
            await _measure(context, page, body, report, navs, ms)
        except Exception as e:  # noqa: BLE001 — never a 500 for what the page did
            if not (report["crashed"] or report["navigated"]):
                report["check_error"] = _clip(_first_line(e))
    finally:
        try:
            await context.close()
        except Exception:
            pass
    report["timings"]["elapsed_ms"] = ms()
    return JSONResponse(report)


def _left_document(navs: list, frame) -> Optional[str]:
    """A commit, after the subject's own about:srcdoc, to a DIFFERENT
    address (about:blank, data:, anywhere): the document navigated itself
    away. Same-address commits (a reload, history.replaceState) and fragment
    changes are told apart by the isolated world instead (_IsolatedWorld.gone):
    only a new document takes the world with it."""
    urls = [u for f, u in navs if f is frame]
    try:
        first = urls.index("about:srcdoc")
    except ValueError:
        return None
    for u in urls[first + 1:]:
        if not (u == "about:srcdoc" or u.startswith("about:srcdoc#")):
            return u or "a new document"
    return None


async def _measure(context, page, body: DocumentBody, report: dict, navs: list, ms) -> None:
    async def gone(err: Any = None) -> bool:
        """The page crashed or the document navigated away: stop measuring —
        what is left to read is not the document."""
        nonlocal world
        if report["crashed"] or report["navigated"] or report["reload_load_error"]:
            return True
        if frame is None:
            return False
        url = _left_document(navs, frame)
        if url is None and ((err is not None and _context_destroyed(err))
                            or (world is not None and await world.gone())):
            try:
                url = frame.url or "a new document"
            except Exception:  # noqa: BLE001
                url = "a new document"
            if url == "about:srcdoc" or url.startswith("about:srcdoc#"):
                if attached[0] < _MAX_SELF_RELOADS:
                    # The document reloaded ITSELF once: a srcdoc shows the
                    # same document again (a hash change before it makes no
                    # difference), which is harmless. Measure the new copy.
                    try:
                        await reattach()
                        return False
                    except Exception as again:  # noqa: BLE001 — cannot read the new copy
                        if _context_destroyed(again):
                            # The new copy went away while it was being read:
                            # it reloaded again. That is the loop, named.
                            report["reload_loop"] = True
                        elif isinstance(again, asyncio.TimeoutError):
                            # It reloaded and then did not finish loading: a
                            # slow load, not a navigation.
                            report["reload_load_error"] = _clip(
                                f"after it reloaded itself, the new copy did not finish "
                                f"loading and painting in time ({int(LOAD_TIMEOUT_MS // 1000)} s "
                                "to load)")
                            return True
                else:
                    report["reload_loop"] = True    # the copy reloaded again
        if url:
            report["navigated"] = _clip(url)
        return bool(url)

    world = None
    frame = None
    attached = [0]

    async def reattach() -> None:
        nonlocal world
        # The same load allowance as the first copy had.
        await asyncio.wait_for(frame.wait_for_load_state("load"), LOAD_TIMEOUT_MS / 1000)
        world = await asyncio.wait_for(
            _IsolatedWorld.open(context, page, frame, body.subject), STEP_TIMEOUT_S)
        await asyncio.wait_for(world.call(_TWO_FRAMES_JS), STEP_TIMEOUT_S)
        attached[0] += 1

    try:
        await page.goto(PAGE_URL, wait_until="load", timeout=LOAD_TIMEOUT_MS)
        report["loaded"] = True
    except Exception as e:  # noqa: BLE001 — a page that will not load IS the finding
        first = _first_line(e)
        if "Timeout" in first:
            first = (f"it did not finish loading within {LOAD_TIMEOUT_MS // 1000} s "
                     "(a script that never returns holds the page's load forever)")
        report["load_error"] = _clip(first)
    report["timings"]["load_ms"] = ms()
    if report["crashed"]:
        return

    if report["loaded"]:
        # The page the kernel composed holds ONE iframe, the subject: it is
        # the main frame's only child. Read from Playwright's own frame tree
        # (no round trip into a renderer the document may have locked), and
        # never by window.name, which the document can set. The isolated
        # world below addresses it by its element, iframe#<subject>.
        kids = page.main_frame.child_frames
        frame = kids[0] if len(kids) == 1 else None
    report["subject_found"] = frame is not None
    if frame is None:
        return

    # THE READINESS SIGNAL: the subject's own `load`, then two of its own
    # animation frames — the first rendering opportunity after load has come
    # and gone, so the first paint happened and every rAF callback registered
    # during load (an animation's first tick) ran.
    try:
        await asyncio.wait_for(frame.wait_for_load_state("load"), STEP_TIMEOUT_S)
        world = await asyncio.wait_for(
            _IsolatedWorld.open(context, page, frame, body.subject), STEP_TIMEOUT_S)
        await asyncio.wait_for(world.call(_TWO_FRAMES_JS), STEP_TIMEOUT_S)
        report["ready"] = True
    except Exception as e:  # noqa: BLE001
        if await gone(e):
            return
        if attached[0]:
            report["ready"] = True        # reloaded before its first look; reattached
        else:
            report["ready_error"] = _clip(
                "no frame was painted within "
                f"{int(STEP_TIMEOUT_S)}s" if isinstance(e, asyncio.TimeoutError) else _first_line(e))
    report["timings"]["ready_ms"] = ms()
    if not report["ready"]:
        return

    # Each COPY of the document gets its own first look and its own full
    # settle: a document that reloads itself once is measured again from its
    # first paint, so what it does in its first seconds counts the second time
    # too. One that reloads again is a loop (gone() fails it).
    while True:
        epoch = attached[0]
        if epoch and ms() > ROUND_DEADLINE_S * 1000:
            report["deadline"] = _clip(
                f"it reloaded itself and the check ran out of its {int(ROUND_DEADLINE_S)} s "
                "before the new copy could be watched")
            return
        # The first look, right after the first frames: what the document drew
        # before the settle. The end of the settle looks again (below) and a
        # document that took its tiles or bar away in between is refused.
        if body.probes:
            try:
                report["probes_first"] = await asyncio.wait_for(
                    world.call(_PROBE_JS, {k: v.model_dump() for k, v in body.probes.items()}),
                    STEP_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 — the end-of-settle probe reports it
                if await gone(e):
                    return
            if attached[0] != epoch:
                continue                  # it reloaded: the new copy starts over

        # The settle. A plain wait — nothing here asks the page, so a page that
        # crashes or navigates during it cannot break the wait; the handlers
        # above record it.
        await asyncio.sleep(body.settle_ms / 1000)
        report["timings"]["settled_ms"] = ms()
        if await gone():
            return
        if attached[0] != epoch:
            continue                      # reloaded during the settle: its own settle, below

        # A step that TIMES OUT after the first frames is the document locking
        # its renderer (a script that never returns), not the check breaking —
        # reported apart, so the kernel can fail it.
        hung = (f"after its first frames the document stopped responding for "
                f"{int(STEP_TIMEOUT_S)} s (a script that never returns)")
        try:
            if body.probes:
                report["probes"] = await asyncio.wait_for(
                    world.call(_PROBE_JS, {k: v.model_dump() for k, v in body.probes.items()}),
                    STEP_TIMEOUT_S)
            report["content"] = await asyncio.wait_for(
                world.call(_CONTENT_JS, _MAX_CONTENT_RECTS), STEP_TIMEOUT_S)
        except asyncio.TimeoutError:
            report["unresponsive"] = hung
        except Exception as e:  # noqa: BLE001
            if await gone(e):
                return
            if attached[0] != epoch:
                continue                  # it reloaded under the probe: the new copy starts over
            report["probe_error"] = _clip(_first_line(e))
        if report["crashed"] or report["navigated"]:
            return
        if report["unresponsive"]:
            # Its renderer is locked: there is no frame to capture, and asking
            # would only hang this call too.
            return
        try:
            png = await asyncio.wait_for(
                page.locator(f"iframe#{body.subject}").screenshot(
                    type="png", timeout=int(STEP_TIMEOUT_S * 1000)),
                STEP_TIMEOUT_S)
            rects = [r[:4] for r in ((report["content"] or {}).get("rects") or [])]
            scratch = await context.new_page()
            try:
                report["paint"] = await asyncio.wait_for(
                    scratch.evaluate(_PAINT_JS, [base64.b64encode(png).decode("ascii"),
                                                 PAINT_TOLERANCE, rects]),
                    STEP_TIMEOUT_S)
            finally:
                await scratch.close()
        except Exception as e:  # noqa: BLE001
            if await gone(e):
                return
            if attached[0] != epoch:
                continue
            if isinstance(e, asyncio.TimeoutError) or "Timeout" in str(e):
                report["unresponsive"] = hung
            else:
                report["paint_error"] = _clip(_first_line(e))
        if not report["unresponsive"]:
            await gone()
            if attached[0] != epoch:
                continue
        break
