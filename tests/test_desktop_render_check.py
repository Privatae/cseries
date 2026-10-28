"""The desktop RENDER check, end to end in a real browser.

On one Host, five harness-built desktop designs reached the shared Designs
shelf without ever being rendered: every door ran only the text floor. The
render check loads the document the way the shell mounts it and fails one
that throws, paints nothing, or does not draw what it declares.

This drives the REAL pieces together, with nothing stubbed but the Chromium
provider:
  - the kernel composes the page (server/applications/desktop_render.py
    compose_page — the served sandbox-csp.js + desktop-preview.js, mounting
    the document in a sandbox="allow-scripts" srcdoc frame);
  - the browser sidecar's route loads and measures it
    (sidecars/browser/control/document_verify.py verify_document);
  - the kernel judges the report against the contract (judge).

It pins:
  - the platform template — an ANIMATED desktop that declares the bar — passes;
  - a document that throws on its first animation tick fails, and the error
    is named with the document's own line and that line's text;
  - a draw() that throws inside CSeriesDesktop.on (swallowed by the real bridge)
    fails too, named;
  - a blank page and a single-colour wallpaper fail: they paint nothing;
  - a desktop that draws no app tile fails; one that declares the bar and
    hides it fails;
  - an error thrown 1.5 s after load — inside the settle — is caught;
  - a document that overrides querySelectorAll, getBoundingClientRect,
    getComputedStyle and requestAnimationFrame to fake tiles and a bar still
    fails: the check measures from an isolated world and the pixels;
  - a script that never returns fails as "did not load" instead of hanging,
    and one that locks up on its first frame fails as "never painted";
  - a page that CRASHES (its renderer runs out of memory) and a document
    that NAVIGATES ITSELF after it loads (a reload, about:blank) fail with
    that reason, never as "not checked" and never as a 500;
  - it fails what is BROKEN and nothing else — the shell's own contract: an
    auto-hiding bar, tiles a start menu reveals, a slow fade-in, a flat
    wallpaper with a small clock and a document that sets its own
    window.name all pass; an overlay covering every visible tile fails (and
    tiles hidden at opacity 0 elsewhere do not rescue it), invisible text on
    a flat colour fails as blank, and a strip whose only control is
    visibility:hidden is no bar;
  - the document cannot tell the check from the desktop: navigator.webdriver
    is false and nothing in its world marks the check, so a design that
    draws its tiles only for an automated browser draws none here;
  - the page's own network requests are ABORTED at the route (a local
    server that counts hits sees none).

The same payloads were run against the live sidecar image (cseries-browser
v0.5.2: Playwright 1.48, Debian Chromium 152, headed under Xvfb) with
identical verdicts. The kernel's doors — publish refuses
when the browser cannot be asked, wear proceeds and says so — are pinned
with stubs in scripts/test_desktop_render_doors.py.

Skips when Chromium is unavailable — and FAILS instead in the gate lane,
where it is expected (CSERIES_REQUIRE_CHROMIUM=1, tests/conftest.py).

Run: pytest tests/test_desktop_render_check.py
"""
from __future__ import annotations

import asyncio
import http.server
import json
import pathlib
import sys
import threading

import pytest

async_api = pytest.importorskip(
    "playwright.async_api", reason="playwright not available to drive the render check"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))       # mastercode.runtime.app_verify, for the phone pass
sys.path.insert(0, str(_ROOT / "sidecars" / "browser"))

from control import document_verify as DV  # noqa: E402
from server.applications import desktop_render as DR  # noqa: E402
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

APPS = [{"id": "circles", "name": "Circles"}, {"id": "workbench", "name": "Workbench"},
        {"id": "settings", "name": "Settings"}, {"id": "designs", "name": "Designs"}]

_FIRST_TICK = "<script>requestAnimationFrame(function () { null.boom(); });</script>"
THROWS_ON_FIRST_TICK = TEMPLATE_HTML.replace("</body>", _FIRST_TICK + "\n</body>", 1)
DRAW_THROWS = TEMPLATE_HTML.replace(
    "CSeriesDesktop.on(", "CSeriesDesktop.on(function () { missingHelper(); }); CSeriesDesktop.on(", 1)
BLANK = "<!DOCTYPE html><html><head></head><body></body></html>"
FLAT_WALLPAPER = ('<!DOCTYPE html><html><head><meta name="cseries-desktop" content="wallpaper">'
                  '</head><body style="margin:0;background:#1a2b3c"></body></html>')
NO_TILES = TEMPLATE_HTML.replace('data-cseries-app="', 'data-not-an-app="')
# Declares the bar and draws none: no dock, launcher or window list exists,
# and the strip shows nothing.
BAR_HIDDEN = (TEMPLATE_HTML.replace("body.owns-bar #bar { display:flex; }",
                                    "body.owns-bar #bar { display:none; }", 1)
              .replace("data-cseries-activate", "data-x-activate")
              .replace("data-cseries-window", "data-x-window"))
HANGS = TEMPLATE_HTML.replace("</body>", "<script>while (true) {}</script></body>", 1)
# Loads fine, then locks up on its first animation frame: the readiness wait
# (load + two frames inside the document) is what turns this into a failure
# rather than a check that could not read the frame.
HANGS_ON_FIRST_TICK = TEMPLATE_HTML.replace(
    "</body>", "<script>addEventListener('load', function () { requestAnimationFrame("
    "function () { while (true) {} }); });</script></body>", 1)
# Paints, then locks up once the check has seen its first frames — inside the
# settle. A fixed timer cannot place the lock-up there: on a busy machine the
# check's first look can come after any timer, and the document then reads as
# "never painted". So the lock-up is ARMED by the check itself (see
# _locks_after_its_first_look), through the DOM the worlds share.
_LOCK_MARK = "data-test-lock-now"
HANGS_AFTER_PAINT = TEMPLATE_HTML.replace(
    "</body>", "<script>setInterval(function () { if (document.documentElement.hasAttribute("
    f"'{_LOCK_MARK}')) {{ while (true) {{}} }} }}, 50);</script></body>", 1)
# Draws no tile and no bar, and LIES about it from its own main world: every
# DOM query answers fake visible elements, every box is on screen, every
# style is visible, and animation frames fire at once. The check measures
# from an isolated world and the screenshot, so none of it may change the
# verdict.
SPOOFED = """<!DOCTYPE html><html><head>
<meta name="cseries-desktop" content="desktop"><meta name="cseries-bar" content="bottom 52">
</head><body style="margin:0;height:100vh;background:linear-gradient(135deg,#123,#a50)">
<h1 style="color:#fff;font:48px sans-serif;padding:40px;margin:0">Nothing to launch here</h1>
<script>
(function () {
  var fake = [1, 2, 3].map(function () { return document.createElement("button"); });
  Document.prototype.querySelectorAll = function () { return fake; };
  Element.prototype.querySelectorAll = function () { return fake; };
  Element.prototype.getBoundingClientRect = function () {
    return {x: 10, y: 860, left: 10, top: 860, right: 90, bottom: 890, width: 80, height: 30};
  };
  window.getComputedStyle = function () { return {visibility: "visible", opacity: "1", display: "block"}; };
  window.requestAnimationFrame = function (cb) { cb(0); return 1; };
})();
</script></body></html>"""
# Throws 1.5 s after load: inside the settle, so the check must still be
# watching when it happens — the settle is a real wait, not a formality.
LATE = TEMPLATE_HTML.replace(
    "</body>", "<script>setTimeout(function () { throw new Error('late tick'); }, 1500);</script></body>", 1)


def _with(snippet: str) -> str:
    return TEMPLATE_HTML.replace("</body>", snippet + "</body>", 1)


# The page goes away under the check. The test browser runs V8 with a small
# heap (see _run_all) so the out-of-memory crash comes in a second, not in
# minutes; the sidecar's browser hits the same crash at its own limit.
OOM = _with("<script>setTimeout(function(){var a=[];for(;;)a.push(new Array(1e6).fill(Math.random()))},300)</script>")
# Reloads itself ONCE (window.name survives a reload of the same frame); a
# srcdoc shows the same document again.
RELOADS = _with("<script>if(window.name!=='reloaded'){window.name='reloaded';"
                "setTimeout(function(){location.reload()},400)}</script>")
# Reloads on every load, at every interval the settle could miss (it is 2 s): a
# loop that would run forever in the shell. Each copy gets its own settle, so
# the second copy's reload is seen.
def _loop_every(ms: int) -> str:
    return _with("<script>setTimeout(function(){location.reload()},%d)</script>" % ms)


RELOAD_EVERY_1000 = _loop_every(1000)
RELOAD_EVERY_1500 = _loop_every(1500)
RELOAD_EVERY_1900 = _loop_every(1900)
# Reloads at 1.5 s and throws at 1.8 s: on every copy.
RELOAD_THEN_ERROR_LOOP = _with("<script>setTimeout(function(){location.reload()},1500);"
                               "setTimeout(function(){throw new Error('after')},1800)</script>")
# "init fails, catch, reload in 1.2 s"
CATCH_AND_RELOAD = _with("<script>try{null.init()}catch(e){setTimeout(function(){location.reload()},1200)}</script>")
# A one-time reload at 1.5 s whose NEW copy throws at 1.8 s: only a settle of
# the new copy sees it.
RELOAD_ONCE_THEN_ERROR = _with(
    "<script>if(window.name!=='r1'){window.name='r1';setTimeout(function(){location.reload()},1500)}"
    "else{setTimeout(function(){throw new Error('second copy broke')},1800)}</script>")
# A one-time reload at 1.5 s whose new copy is fine. A timer places it, so on a
# slow machine the reload can come before the check's first look: that is fine
# here, because this document must PASS either way (it is a case of
# test_what_the_shell_accepts_passes). A test that needs the reload to be SEEN
# uses RELOAD_ONCE_ON_MARK.
RELOAD_ONCE_LATE = _with("<script>if(window.name!=='r2'){window.name='r2';"
                         "setTimeout(function(){location.reload()},1500)}</script>")
# A hash change, then ONE reload: the same rule as a plain reload.
HASH_RELOAD_ONCE = _with("<script>if(window.name!=='r3'){window.name='r3';location.hash='x';"
                         "setTimeout(function(){location.reload()},300)}</script>")
# Reloads on every load: a loop, which is a failure.
RELOAD_LOOP = _with("<script>setTimeout(function(){location.reload()},300)</script>")
NAV_BLANK = _with("<script>setTimeout(function(){location.href='about:blank'},400)</script>")
NAV_DATA = _with("<script>setTimeout(function(){location.href='data:text/html,<p>other</p>'},400)</script>")
NAV_HTTP = _with("<script>setTimeout(function(){location.href='http://127.0.0.1:9/other'},400)</script>")
# Draws its tiles and bar, then removes (or hides) the tiles inside the settle:
# after the check's first look, before its last. A fixed timer cannot place
# that on a busy machine, so the change is ARMED by the check once its first
# look returns (_after_its_first_look), through the DOM the worlds share.
_ACT_MARK = "data-test-act-now"


def _on_mark(js: str) -> str:
    return _with("<script>var _t=setInterval(function(){if(document.documentElement.hasAttribute("
                 f"'{_ACT_MARK}')){{clearInterval(_t);{js}}}}},50)</script>")


REMOVES_TILES = _on_mark("document.querySelectorAll('[data-cseries-app]').forEach(function(e){e.remove()})")
HIDES_TILES = _on_mark("document.querySelectorAll('[data-cseries-app]')"
                       ".forEach(function(e){e.style.visibility='hidden'})")
# Reloads itself ONCE, when the check has made its first look (armed through
# the DOM the worlds share, as REMOVES_TILES is) and not at a fixed time after
# the script ran: a fixed 1.5 s lands before the check has opened its world
# whenever the machine is slow enough to take longer than that to get from the
# document's `load` to the first look, and a reload that came before the first
# look is not a reload the check sees at all (see _reloads_after_its_first_look).
RELOAD_ONCE_ON_MARK = _on_mark("if(window.name!=='r5'){window.name='r5';location.reload()}")
# A tab kept in the address's fragment, and history.replaceState: the SAME
# document — not a navigation away. A reload after a fragment change is.
SAME_DOCUMENT = _with("<script>setTimeout(function(){location.hash='tab-2';"
                      "history.replaceState({}, '')},300)</script>")
RELOAD_AFTER_HASH = _with("<script>setTimeout(function(){location.hash='x';"
                          "setTimeout(function(){location.reload()},200)},300)</script>")
# Not broken, and refused before: the shell's contract is that the tiles and
# the bar EXIST (desktop-surface.js safetyNet / barDrawn), not that they are
# visible at one instant.
SETS_WINDOW_NAME = _with("<script>window.name='desk-state'</script>")
AUTOHIDE_BAR = TEMPLATE_HTML.replace("</style>", "#bar{opacity:0;transition:opacity .2s} #bar:hover{opacity:1}</style>", 1)
START_MENU = TEMPLATE_HTML.replace("</style>", "[data-cseries-apps],.apps{display:none}</style>", 1)
SLOW_FADE_IN = TEMPLATE_HTML.replace(
    "</style>", "@keyframes skf{from{opacity:0}to{opacity:1}} [data-cseries-app]{opacity:0;animation:skf .6s ease 3s forwards}</style>", 1)
TINY_CLOCK = ('<!DOCTYPE html><html><head><meta name="cseries-desktop" content="wallpaper"></head>'
              '<body style="margin:0;height:100vh;background:#101418;color:#c8d0d8;font:13px system-ui">'
              '<div style="position:fixed;right:24px;bottom:20px">21:04</div></body></html>')
# Text in the background's own colour shows nothing: blank.
INVISIBLE_TEXT = TINY_CLOCK.replace("color:#c8d0d8", "color:#101418")
# Every visible tile under an overlay; one more tile, at opacity 0, sits
# where the overlay is not. A hidden tile cannot be clicked by a person, so
# it does not rescue the desktop.
OVERLAY = ('<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop"></head>'
           '<body style="margin:0;height:100vh;background:linear-gradient(135deg,#123,#a50)">'
           '<div style="position:fixed;left:20px;top:20px;display:flex;gap:12px">'
           '<button data-cseries-app="circles" style="width:90px;height:90px">Circles</button>'
           '<button data-cseries-app="workbench" style="width:90px;height:90px">Workbench</button></div>'
           '<div style="position:fixed;right:20px;top:20px;opacity:0">'
           '<button data-cseries-app="settings" style="width:90px;height:90px">Settings</button></div>'
           '<div style="position:fixed;left:0;top:0;width:60vw;height:100vh;z-index:9;'
           'background:rgba(0,0,0,.5)"></div></body></html>')
# A bar declared and drawn in its own markup, whose only control is hidden.
HIDDEN_BAR_CONTROL = (
    '<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop">'
    '<meta name="cseries-bar" content="bottom 52"></head>'
    '<body style="margin:0;height:100vh;background:linear-gradient(#123,#456)">'
    '<div data-cseries-apps style="padding:20px"></div>'
    '<div style="position:fixed;left:0;right:0;bottom:0;height:52px;background:#000">'
    '<button style="visibility:hidden;width:120px;height:40px">Start</button></div></body></html>')
# Draws its tiles only for an automated browser, or when the check marks
# itself in the document's world. Indistinguishable means it draws none here.
DETECTS_THE_CHECK = (
    '<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop"></head>'
    '<body style="margin:0;height:100vh;background:#222;color:#eee;font:20px system-ui">'
    '<p>an ordinary desktop</p><script>'
    'var s=document.querySelector("script[data-cseries-desktop]");'
    'if(navigator.webdriver||/var R=true/.test(s?s.textContent:"")){'
    'var g=document.createElement("div");g.setAttribute("data-cseries-apps","");'
    'document.body.appendChild(g);CSeriesDesktop.render(g)}'
    '</script></body></html>')

CASES = {
    "template": TEMPLATE_HTML, "first_tick": THROWS_ON_FIRST_TICK, "draw_throws": DRAW_THROWS,
    "blank": BLANK, "flat": FLAT_WALLPAPER, "no_tiles": NO_TILES, "bar_hidden": BAR_HIDDEN,
    "hangs": HANGS, "late": LATE, "hangs_on_first_tick": HANGS_ON_FIRST_TICK,
    "spoofed": SPOOFED, "hangs_after_paint": HANGS_AFTER_PAINT,
    "oom": OOM, "reloads": RELOADS, "nav_blank": NAV_BLANK, "window_name": SETS_WINDOW_NAME,
    "same_document": SAME_DOCUMENT, "reload_after_hash": RELOAD_AFTER_HASH,
    "autohide_bar": AUTOHIDE_BAR, "start_menu": START_MENU, "slow_fade_in": SLOW_FADE_IN,
    "tiny_clock": TINY_CLOCK, "invisible_text": INVISIBLE_TEXT, "overlay": OVERLAY,
    "hidden_bar_control": HIDDEN_BAR_CONTROL, "detects": DETECTS_THE_CHECK,
    "reload_loop": RELOAD_LOOP, "reload_1000": RELOAD_EVERY_1000,
    "reload_1500": RELOAD_EVERY_1500, "reload_1900": RELOAD_EVERY_1900,
    "reload_error_loop": RELOAD_THEN_ERROR_LOOP, "catch_reload": CATCH_AND_RELOAD,
    "reload_once_error": RELOAD_ONCE_THEN_ERROR, "reload_once_late": RELOAD_ONCE_LATE,
    "hash_reload_once": HASH_RELOAD_ONCE, "nav_data": NAV_DATA, "nav_http": NAV_HTTP, "removes_tiles": REMOVES_TILES,
    "hides_tiles": HIDES_TILES,
}


class _Counter(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):  # noqa: N802
        _Counter.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()

    def log_message(self, *_a):
        pass


class _Watched:
    """The browser handed to the route, recording why each request it made
    failed. The route's own abort reads net::ERR_BLOCKED_BY_CLIENT; a request
    let through fails some other way, or reaches the server."""

    def __init__(self, browser, failures: list):
        self._b, self._f = browser, failures

    async def new_context(self, **kw):
        ctx = await self._b.new_context(**kw)
        ctx.on("requestfailed", lambda r: self._f.append((r.url, r.failure or "")))
        return ctx


async def _abort_probe(browser) -> tuple[dict, list, list]:
    """The ROUTE's abort, exercised directly: a page of our own (no sandbox
    policy in it to stop the request first) asks a live local server for an
    image. The route must abort it, list it, and the server must see
    nothing."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Counter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/pixel.png"
    failures: list = []
    DV.set_browser_provider(lambda: _Watched(browser, failures))
    try:
        body = DV.DocumentBody(
            page=(f'<!doctype html><html><body><img src="{url}">'
                  '<iframe id="subject" srcdoc="<p>hello</p>"></iframe></body></html>'),
            subject="subject", settle_ms=200)
        report = json.loads((await DV.verify_document(body)).body)
    finally:
        DV.set_browser_provider(lambda: browser)
        srv.shutdown()
    return report, list(_Counter.hits), failures


def _line_of(doc: str, needle: str) -> int:
    return next(i for i, ln in enumerate(doc.splitlines(), 1) if needle in ln)


async def _check(html: str) -> tuple[dict, dict]:
    expect = DR.expectations(html)
    body = DV.DocumentBody(
        page=DR.compose_page(html, apps=APPS, host="Sage"), subject=DR.SUBJECT,
        width=DR.VIEWPORT[0], height=DR.VIEWPORT[1], settle_ms=DR.SETTLE_MS,
        probes=DR.probes_for(expect, len(APPS)))
    report = json.loads((await DV.verify_document(body)).body)
    return report, DR.judge(report, html=html, expect=expect, apps_handed=len(APPS))


async def _run_all() -> dict:
    async with async_api.async_playwright() as p:
        try:
            # The sidecar's own launch arguments (control/app.py), plus a small
            # V8 heap so the out-of-memory case crashes in a second.
            browser = await p.chromium.launch(
                args=[*DV.CHECK_BROWSER_ARGS, "--js-flags=--max-old-space-size=64"])
        except Exception as exc:  # no browser binary installed
            return {"skip": f"chromium unavailable: {exc}"}
        DV.set_browser_provider(lambda: browser)
        real = DV.LOAD_TIMEOUT_MS, DV.STEP_TIMEOUT_S
        try:
            # Every case that loads and answers is judged at the PRODUCTION
            # limits: on a busy machine a healthy page can take seconds to
            # answer one step, and a shortened limit then reads it as a
            # lock-up. Six at a time.
            out: dict = {}
            gate = asyncio.Semaphore(6)

            async def one(n):
                async with gate:
                    return await _check(CASES[n])

            healthy = [n for n in CASES if n not in _DISRUPTIVE and n not in _ARMED]
            out.update(zip(healthy, await asyncio.gather(*(one(n) for n in healthy))))
            # The documents that change inside the settle, on their own: the
            # check arms each one after its first look.
            out.update(zip(_ARMED, await _after_its_first_look(_ARMED)))
            # The lock-ups and the out-of-memory crash run AFTER the rest, one
            # at a time, so a renderer they pin or a heap they grow can never
            # slow another case. Each is held to a short limit ONLY on the step
            # it genuinely hangs in, where waiting longer changes nothing; every
            # step before that runs at the production limit.
            for n in _DISRUPTIVE:
                DV.LOAD_TIMEOUT_MS, DV.STEP_TIMEOUT_S = real
                if n == "hangs":                    # never loads: the load is what hangs
                    DV.LOAD_TIMEOUT_MS, DV.STEP_TIMEOUT_S = _HUNG_LOAD_MS, _HUNG_STEP_S
                    out[n] = await _check(CASES[n])
                elif n == "hangs_on_first_tick":    # loads, then locks before its first frames
                    DV.STEP_TIMEOUT_S = _HUNG_STEP_S
                    out[n] = await _check(CASES[n])
                elif n == "hangs_after_paint":      # short only once it is locked
                    out[n] = await _locks_after_its_first_look()
                else:                               # oom: waits for the crash itself
                    out[n] = await _check(CASES[n])
            DV.LOAD_TIMEOUT_MS, DV.STEP_TIMEOUT_S = real
            out["abort"] = await _abort_probe(browser)
        finally:
            DV.LOAD_TIMEOUT_MS, DV.STEP_TIMEOUT_S = real
            await browser.close()
        return out


_DISRUPTIVE = ("hangs", "hangs_on_first_tick", "hangs_after_paint", "oom")
_ARMED = ("removes_tiles", "hides_tiles")


async def _after_its_first_look(names) -> list:
    """Runs CASES[n] for each name, setting _ACT_MARK in each document as soon
    as the check's first look (its first probe of that copy) has returned.
    The settle and the end-of-settle look are the shipped code, unchanged."""
    real_call = DV._IsolatedWorld.call
    armed: set = set()

    async def call(self, fn_js, arg=None):
        got = await real_call(self, fn_js, arg)
        if fn_js == DV._PROBE_JS and id(self) not in armed:
            armed.add(id(self))
            await real_call(self, f"() => document.documentElement.setAttribute('{_ACT_MARK}', '')")
        return got

    DV._IsolatedWorld.call = call
    try:
        return await asyncio.gather(*(_check(CASES[n]) for n in names))
    finally:
        DV._IsolatedWorld.call = real_call
# The limits a step that genuinely hangs is held to (production: 20 s / 10 s).
_HUNG_LOAD_MS, _HUNG_STEP_S = 2_500, 2.5


async def _locks_after_its_first_look() -> tuple[dict, dict]:
    """HANGS_AFTER_PAINT, locked up the moment the check has seen its first
    frames: the readiness wait (load + two frames) returns, and only then is
    the document told to lock up and the step limit shortened. Everything
    the check does after that (the first look, the settle, the end-of-settle
    probe that must time out) is the shipped code, unchanged."""
    real_call = DV._IsolatedWorld.call

    async def call(self, fn_js, arg=None):
        got = await real_call(self, fn_js, arg)
        if fn_js == DV._TWO_FRAMES_JS:
            DV.STEP_TIMEOUT_S = _HUNG_STEP_S
            await real_call(self, f"() => document.documentElement.setAttribute('{_LOCK_MARK}', '')")
        return got

    DV._IsolatedWorld.call = call
    try:
        return await _check(HANGS_AFTER_PAINT)
    finally:
        DV._IsolatedWorld.call = real_call


@pytest.fixture(scope="module")
def results():
    out = asyncio.run(_run_all())
    if "skip" in out:
        pytest.skip(out["skip"])
    return out


def test_the_animated_template_passes(results):
    report, verdict = results["template"]
    assert verdict["checked"] and verdict["ok"], verdict["failures"]
    assert report["uncaught"] == []
    # It really drew: tiles for every app handed in, each reachable by a
    # click, a bar, and far more than one colour.
    assert report["probes"]["tiles"] == len(APPS) and report["probes"]["bar"] >= 1
    assert report["probes"]["tiles_reach"] == {"shown": len(APPS), "reachable": len(APPS),
                                               "cover": None}
    assert report["paint"]["painted_share"] > 10 * DR.MIN_PAINTED_SHARE
    # ...and it was watched for the whole settle after its first frames.
    t = report["timings"]
    assert t["settled_ms"] - t["ready_ms"] >= DR.SETTLE_MS


def test_an_error_on_the_first_tick_fails_named_with_its_line(results):
    _, verdict = results["first_tick"]
    assert verdict["checked"] and verdict["ok"] is False
    err = verdict["errors"][0]
    assert "reading 'boom'" in err["message"]
    assert err["line"] == _line_of(THROWS_ON_FIRST_TICK, "null.boom()")
    assert "null.boom()" in err["source"]
    assert f"at line {err['line']}" in verdict["why"] and "reading 'boom'" in verdict["why"]


def test_an_error_during_the_settle_is_still_caught(results):
    _, verdict = results["late"]
    assert verdict["ok"] is False
    assert verdict["errors"][0]["message"].endswith("late tick")


def test_a_draw_that_throws_inside_cseriesdesktop_on_fails_named(results):
    report, verdict = results["draw_throws"]
    assert verdict["ok"] is False
    err = verdict["errors"][0]
    assert "missingHelper is not defined" in err["message"] and err.get("callback")
    assert err["line"] == _line_of(DRAW_THROWS, "missingHelper()")
    assert "CSeriesDesktop.on callback" in verdict["why"]
    # Read from the console over CDP: the stand-in swallowed it, as the real
    # bridge does, and nothing was rethrown onto the document's window.
    assert report["uncaught"] == []


def test_a_blank_page_fails_it_shows_nothing(results):
    report, verdict = results["blank"]
    assert verdict["ok"] is False and report["uncaught"] == []
    assert report["paint"]["painted_share"] == 0
    assert any("shows nothing" in f for f in verdict["failures"])


def test_a_single_colour_wallpaper_fails_it_shows_nothing(results):
    report, verdict = results["flat"]
    assert verdict["ok"] is False
    assert report["paint"]["dominant"] == "#1a2b3c"
    assert any("shows nothing" in f and "#1a2b3c" in f for f in verdict["failures"])


def test_a_flat_wallpaper_with_a_small_clock_is_not_blank(results):
    report, verdict = results["tiny_clock"]
    # Below the whole-frame threshold — and still a wallpaper: the clock is
    # visible text with pixels drawn.
    assert report["paint"]["painted_share"] < DR.MIN_PAINTED_SHARE
    assert report["content"]["text"] == 1
    assert max(report["paint"]["content_painted_px"]) >= DR.CONTENT_MIN_PX
    assert verdict["ok"] is True, verdict["failures"]


def test_text_drawn_in_the_background_colour_is_blank(results):
    report, verdict = results["invisible_text"]
    assert report["content"]["text"] == 1
    assert max(report["paint"]["content_painted_px"]) < DR.CONTENT_MIN_PX
    assert verdict["ok"] is False and any("shows nothing" in f for f in verdict["failures"])


def test_a_desktop_that_draws_no_app_tile_fails(results):
    report, verdict = results["no_tiles"]
    assert verdict["ok"] is False and report["probes"]["tiles"] == 0
    assert any("no app tile" in f for f in verdict["failures"])


def test_a_declared_bar_that_is_not_drawn_fails(results):
    report, verdict = results["bar_hidden"]
    assert report["probes"]["bar"] == 0 and report["probes"]["bar_strip"] == 0
    assert verdict["ok"] is False
    assert any("cseries-bar" in f and "bottom 52" in f for f in verdict["failures"]), verdict


def test_a_strip_whose_only_control_is_hidden_is_no_bar(results):
    report, verdict = results["hidden_bar_control"]
    assert report["probes"]["bar"] == 0 and report["probes"]["bar_strip"] == 0
    assert verdict["ok"] is False and any("draws no bar" in f for f in verdict["failures"])


def test_a_document_cannot_fake_its_tiles_or_bar_from_its_own_scripts(results):
    report, verdict = results["spoofed"]
    assert report["uncaught"] == [] and report["paint"]["painted_share"] > DR.MIN_PAINTED_SHARE
    assert report["probes"]["tiles"] == 0 and report["probes"]["bar"] == 0, report["probes"]
    assert report["probes"]["bar_strip"] == 0
    assert verdict["ok"] is False
    assert any("no app tile" in f for f in verdict["failures"])
    assert any("cseries-bar" in f for f in verdict["failures"])


def test_the_document_cannot_tell_the_check_from_the_desktop(results):
    report, verdict = results["detects"]
    # It would draw tiles for an automated browser or a marked check; it
    # drew none, so it saw neither.
    assert report["probes"]["tiles"] == 0
    assert verdict["ok"] is False and any("no app tile" in f for f in verdict["failures"])


def test_a_page_that_crashes_fails_with_the_crash(results):
    report, verdict = results["oom"]
    assert report["crashed"] and "crashed" in report["crashed"]
    assert verdict["checked"] is True and verdict["ok"] is False
    assert "crashed" in verdict["why"]


@pytest.mark.parametrize("case,where", [("reload_loop", "about:srcdoc"),
                                        ("reload_1000", "about:srcdoc"), ("reload_1500", "about:srcdoc"),
                                        ("reload_1900", "about:srcdoc"), ("catch_reload", "about:srcdoc"),
                                        ("reload_error_loop", "about:srcdoc"), ("nav_blank", "about:blank"), ("nav_data", "data:"),
                                        ("nav_http", "chrome-error"),
                                        ("reload_after_hash", "about:srcdoc#x")])
def test_a_document_that_navigates_itself_away_fails(results, case, where):
    report, verdict = results[case]
    assert report["navigated"].startswith(where) or report["navigated"] == where, report["navigated"]
    assert verdict["checked"] is True and verdict["ok"] is False
    assert "navigated itself away" in verdict["why"] and where in verdict["why"]


def test_a_document_that_reloads_itself_is_not_a_failure(results):
    # A srcdoc reloads to the same document: harmless, and the new copy is
    # what gets measured (its tiles are read, so the verdict is a real one).
    report, verdict = results["reloads"]
    assert report["navigated"] is None, report["navigated"]
    assert verdict["checked"] and verdict["ok"], verdict["failures"]
    assert report["probes"]["tiles"] == len(APPS)


def test_a_reload_loop_is_named_as_one(results):
    for case in ("reload_loop", "reload_1000", "reload_1500", "reload_1900", "catch_reload"):
        report, verdict = results[case]
        assert report["reload_loop"] is True, case
        assert "reload loop" in verdict["why"], (case, verdict["why"])


def test_the_reloaded_copy_gets_its_own_settle(results):
    # Its new copy throws 1.8 s after it loads: seen only if the new copy is
    # watched for a settle of its own.
    report, verdict = results["reload_once_error"]
    assert report["navigated"] is None
    assert verdict["ok"] is False and "second copy broke" in " ".join(verdict["failures"]), verdict["failures"]


def test_tiles_removed_during_the_settle_fail_with_the_reason(results):
    report, verdict = results["removes_tiles"]
    assert report["probes_first"]["tiles"] == len(APPS) and report["probes"]["tiles"] == 0
    assert verdict["ok"] is False
    assert "removed them by the end" in verdict["why"], verdict["failures"]
    assert not any("no app tile" in f for f in verdict["failures"])


def test_tiles_hidden_during_the_settle_fail_with_the_reason(results):
    report, verdict = results["hides_tiles"]
    assert report["probes_first"]["tiles_reach"]["shown"] == len(APPS)
    assert report["probes"]["tiles_reach"]["shown"] == 0
    assert verdict["ok"] is False and any("hidden by the end" in f for f in verdict["failures"]), verdict["failures"]


@pytest.mark.parametrize("case", ["reload_once_late", "hash_reload_once", "window_name", "autohide_bar", "start_menu", "slow_fade_in",
                                  "same_document"])
def test_what_the_shell_accepts_passes(results, case):
    report, verdict = results[case]
    assert verdict["checked"] and verdict["ok"], (case, verdict["failures"], report["probes"])


def test_tiles_hidden_by_a_start_menu_exist_but_are_not_shown(results):
    report, _ = results["start_menu"]
    assert report["probes"]["tiles"] == len(APPS)
    assert report["probes"]["tiles_reach"]["shown"] == 0


def test_an_overlay_over_every_visible_tile_fails(results):
    report, verdict = results["overlay"]
    reach = report["probes"]["tiles_reach"]
    # The tile at opacity 0 is outside the overlay and clickable by a script,
    # but no person can see it: it is not counted, so it rescues nothing.
    assert reach == {"shown": 2, "reachable": 0, "cover": "div"}, reach
    assert verdict["ok"] is False and any("covers every app tile" in f for f in verdict["failures"])


def test_the_route_aborts_the_pages_own_requests(results):
    report, hits, failures = results["abort"]
    assert hits == [], hits
    assert any(u.endswith("/pixel.png") for u in report["blocked"]), report["blocked"]
    # Aborted by the route itself — not let through to fail on its own (the
    # browser's private-network rule would also stop this one, so the
    # server's silence alone could not tell the two apart).
    mine = [f for u, f in failures if u.endswith("/pixel.png")]
    assert len(mine) == 1 and mine[0].startswith("net::ERR_BLOCKED_BY_CLIENT"), failures


def test_the_sidecar_launches_its_browser_without_the_automation_flag():
    src = (_ROOT / "sidecars/browser/control/app.py").read_text()
    assert "*document_verify.CHECK_BROWSER_ARGS," in src
    assert DV.CHECK_BROWSER_ARGS == ("--disable-blink-features=AutomationControlled",)


def test_a_script_that_never_returns_fails_instead_of_hanging(results):
    report, verdict = results["hangs"]
    assert report["loaded"] is False and verdict["ok"] is False
    assert "did not load" in verdict["why"]


def test_a_document_that_locks_up_on_its_first_frame_fails(results):
    report, verdict = results["hangs_on_first_tick"]
    assert report["loaded"] is True and report["ready"] is False
    assert verdict["checked"] is True and verdict["ok"] is False
    assert "never painted a frame" in verdict["why"]


def test_a_document_that_locks_up_after_it_paints_fails(results):
    report, verdict = results["hangs_after_paint"]
    assert report["ready"] is True and report["unresponsive"]
    assert verdict["checked"] is True and verdict["ok"] is False
    assert "stopped responding" in verdict["why"]


def test_the_check_reaches_no_network(results):
    for name, res in results.items():
        if name in ("abort", "nav_http"):      # nav_http asks for a page on purpose
            continue
        report = res[0]
        assert report["blocked"] == [], (name, report["blocked"])


# ── checks that need their own limits ────────────────────────────────────────

async def _solo(html: str, **patch) -> tuple[dict, dict]:
    """One check in its own browser with module limits patched."""
    async with async_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch(args=list(DV.CHECK_BROWSER_ARGS))
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        DV.set_browser_provider(lambda: browser)
        real = {k: getattr(DV, k) for k in patch}
        for k, v in patch.items():
            setattr(DV, k, v)
        try:
            return await _check(html)
        finally:
            for k, v in real.items():
                setattr(DV, k, v)
            await browser.close()


# The limits for a test that is about something other than how long a busy
# machine may take to answer: a healthy page then cannot read as a lock-up
# under load, as it can at the production 10 s step.
_ROOMY = dict(LOAD_TIMEOUT_MS=60_000, STEP_TIMEOUT_S=30.0)


async def _reloads_after_its_first_look(html: str, *, on_reloaded=None, **patch) -> tuple[dict, dict]:
    """_solo(html, **patch), where the document reloads itself the moment the
    check has made its first look, and the check goes on only once the reload
    has COMMITTED (its first copy's isolated world is gone). `on_reloaded`, if
    given, is called then, once: a limit that must be short only for what comes
    next (the reloaded copy's load) is shortened there, not for the first
    look, which waits for the reload under the same step limit.

    A document that reloads on a timer of its own cannot be placed relative to
    the check: the check is the later of the two on a slow machine, and a
    reload that came before its first look is a copy the check simply opens
    as its first, so there is no reload for it to count (the deadline test
    read "no deadline" there). Here the reload is armed by the check itself
    and awaited, so every run is the same sequence: first look, reload, the
    settle, the end-of-settle look sees the new copy. The settle, the deadline
    and the reattach are the shipped code, unchanged."""
    real_call = DV._IsolatedWorld.call
    armed = [False]                  # once per check: the reloaded copy is not told to reload

    async def call(self, fn_js, arg=None):
        got = await real_call(self, fn_js, arg)
        if fn_js == DV._PROBE_JS and not armed[0]:
            armed[0] = True
            await real_call(self, f"() => document.documentElement.setAttribute('{_ACT_MARK}', '')")
            # The document's own poll (50 ms) reloads it; the world goes with
            # the old copy. Bounded by the step limit the check's wait_for
            # puts on this call, so a reload that never comes fails the test
            # instead of hanging it.
            while not await self.gone():
                await asyncio.sleep(0.05)
            if on_reloaded:
                on_reloaded()
        return got

    DV._IsolatedWorld.call = call
    try:
        return await _solo(html, **patch)
    finally:
        DV._IsolatedWorld.call = real_call


def test_no_new_round_starts_after_the_deadline():
    # This test is about the round deadline, not about how long a busy machine
    # may take to answer, so the page is held to the roomy limits.
    roomy = _ROOMY
    # One reload, after the check's first look: its new copy would need a round
    # of its own, which the (shortened) deadline no longer allows. The settle
    # alone is 2 s, so the deadline (1 s) has passed by the time the reload is
    # seen, on any machine.
    report, verdict = asyncio.run(_reloads_after_its_first_look(
        RELOAD_ONCE_ON_MARK, ROUND_DEADLINE_S=1.0, **roomy))
    assert report["deadline"] and "ran out of" in report["deadline"], report
    assert report["timings"]["settled_ms"] > 1_000 and report["reload_loop"] is False, report
    assert verdict["checked"] is True and verdict["ok"] is False
    assert "ran out of" in verdict["why"]
    # The limit does not touch a document that never reloads: even a deadline
    # that is over before the first look (0 s) is only for a reloaded copy's round.
    report, verdict = asyncio.run(_solo(TEMPLATE_HTML, ROUND_DEADLINE_S=0.0, **roomy))
    assert report["deadline"] is None and verdict["ok"], verdict["failures"]


def test_a_reloaded_copy_has_the_first_copys_load_allowance():
    # The reloaded copy takes 3 s to finish loading: longer than a step (1.5 s
    # here), well inside the load allowance (8 s here) the first copy had. The
    # delay is put on the wait for the copy's `load` itself (a renderer held
    # at parse answers the check differently, as a lock-up), so it is exact.
    # The document reloads once the check has made its first look; the step
    # is 1.5 s only from the moment that reload has committed, which is all
    # the load wait needs (the first look waits for the reload under the
    # roomy step, so a busy machine cannot time it out).
    Frame = async_api.Frame
    real_wait = Frame.wait_for_load_state
    calls = [0]

    async def slow_wait(self, *a, **kw):
        calls[0] += 1
        if calls[0] >= 2:
            await asyncio.sleep(3.0)
            # The copy's load has been waited for: the steps after it read a
            # healthy page, and are held to the production limit again so a
            # busy machine cannot turn this into a false lock-up.
            DV.STEP_TIMEOUT_S = production_step
        return await real_wait(self, *a, **kw)

    production_step = DV.STEP_TIMEOUT_S

    Frame.wait_for_load_state = slow_wait
    try:
        report, verdict = asyncio.run(_reloads_after_its_first_look(
            RELOAD_ONCE_ON_MARK, LOAD_TIMEOUT_MS=8_000, STEP_TIMEOUT_S=_ROOMY["STEP_TIMEOUT_S"],
            on_reloaded=lambda: setattr(DV, "STEP_TIMEOUT_S", 1.5)))
    finally:
        Frame.wait_for_load_state = real_wait
    assert calls[0] >= 2, calls
    assert report["navigated"] is None and report["reload_load_error"] is None, report
    assert verdict["checked"] and verdict["ok"], verdict["failures"]


def test_a_reload_that_never_finishes_loading_is_a_slow_load_not_a_navigation():
    # Its new copy never becomes readable (the second look at the frame times
    # out, as a copy stuck loading does). A renderer genuinely held at parse
    # answers as a lock-up instead, so the timeout is injected at the world
    # the check opens on the new copy. The document reloads once the check has
    # made its first look, at the roomy limits.
    real_open = DV._IsolatedWorld.open.__func__
    calls = [0]

    async def slow_open(cls, *a, **kw):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.TimeoutError()
        return await real_open(cls, *a, **kw)

    DV._IsolatedWorld.open = classmethod(slow_open)
    try:
        report, verdict = asyncio.run(_reloads_after_its_first_look(RELOAD_ONCE_ON_MARK, **_ROOMY))
    finally:
        DV._IsolatedWorld.open = classmethod(real_open)
    assert report["navigated"] is None, report["navigated"]
    assert report["reload_load_error"] and "did not finish loading" in report["reload_load_error"]
    assert verdict["checked"] is True and verdict["ok"] is False
    assert "slow load, not a navigation" in verdict["why"], verdict["why"]
    assert "navigated itself away" not in " ".join(verdict["failures"])


def test_a_copy_that_leaves_again_while_it_is_read_is_named_a_loop():
    # The second copy reloads again while the check is still reading it: the
    # read fails with a destroyed context (what CPU contention produces
    # intermittently; injected here so it is certain). The failure must still
    # say "reload loop", not only "navigated itself away". The document
    # reloads once the check has made its first look, at the roomy limits.
    real_open = DV._IsolatedWorld.open.__func__
    calls = [0]

    async def gone_open(cls, *a, **kw):
        calls[0] += 1
        if calls[0] >= 2:
            raise RuntimeError("Cannot find context with specified id")
        return await real_open(cls, *a, **kw)

    DV._IsolatedWorld.open = classmethod(gone_open)
    try:
        report, verdict = asyncio.run(_reloads_after_its_first_look(RELOAD_ONCE_ON_MARK, **_ROOMY))
    finally:
        DV._IsolatedWorld.open = classmethod(real_open)
    assert report["reload_loop"] is True, report
    assert verdict["ok"] is False and "reload loop" in verdict["why"], verdict["why"]


# ── the phone pass: render_check end to end, at both sizes ──────────────────
#
# A desktop is worn on the phone too, and the worn design on
# One Host put all 15 apps in one centred row — 8 of them wholly past the edges
# of a phone screen. render_check now loads a document that passes at desktop
# size again as a phone (desktop_render.PHONE_VIEWPORT, CSeriesDesktop.phone
# true, no bar). This runs the kernel's real render_check with the sidecar's
# real verify_document in place of the HTTP hop, so both looks, the phone
# stand-in, the phone probes and the joined verdict are all the shipped code.

# Breaks only on a narrow screen: a draw step that throws below 600 px.
PHONE_THROWS = _with("<script>if (innerWidth < 600) requestAnimationFrame(function () "
                     "{ null.narrowLayout(); });</script>")
# Hides everything below 600 px: a desktop that shows a phone nothing.
PHONE_BLANK = TEMPLATE_HTML.replace(
    "</style>", "@media (max-width:600px){html,body{background:#0a0e0f} body *{display:none !important}}</style>", 1)
# A centred row of fixed-size tiles, the way one Host's "Slate Mist" lays
# its dock out: fine at 1440 px, off both sides of a phone.
ROW_DOCK = ('<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop"></head>'
            '<body style="margin:0;height:100vh;overflow:hidden;background:linear-gradient(#1a0d12,#2b1d22);'
            'color:#f0e6dc;font:14px system-ui"><h1 style="text-align:center;margin-top:30vh">10:47</h1>'
            '<nav id="dock" style="position:fixed;left:50%;bottom:74px;transform:translateX(-50%);'
            'display:flex;gap:10px"></nav><script>CSeriesDesktop.on(function (S) {'
            'document.getElementById("dock").innerHTML = S.apps.map(function (a) {'
            'return \'<button data-cseries-app="\' + a.id + \'" style="flex:none;width:52px;height:52px">\''
            ' + a.name.charAt(0) + "</button>"; }).join(""); });</script></body></html>')

PHONE_APPS = [{"id": f"app-{i}", "name": f"App {i}"} for i in range(24)]


def _drawer(closed_css: str) -> str:
    """A grid on a desktop; on a phone, a drawer an "Apps" button slides in."""
    return ('<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop"><style>'
            'body{margin:0;height:100vh;overflow:hidden;background:linear-gradient(#10202a,#1d3440);'
            'color:#eef;font:14px system-ui}h1{position:fixed;right:40px;top:30px;margin:0}'
            '#apps{position:fixed;left:40px;top:120px;display:grid;grid-template-columns:repeat(6,72px);gap:10px}'
            '#apps button{width:72px;height:72px}#open{display:none}'
            'body.phone #open{display:block;position:fixed;right:20px;bottom:20px;width:56px;height:56px}'
            'body.phone #apps{left:0;top:0;bottom:0;width:300px;grid-template-columns:repeat(3,72px);'
            'transform:translateX(-100%);' + closed_css + '}'
            'body.phone #apps.open{transform:none;visibility:visible}'
            '</style></head><body><h1>10:47</h1><button id="open">Apps</button><nav id="apps"></nav>'
            '<script>CSeriesDesktop.on(function (S) {document.body.classList.toggle("phone", !!S.phone);'
            'document.getElementById("apps").innerHTML = S.apps.map(function (a) {'
            'return \'<button data-cseries-app="\' + a.id + \'">\' + a.name + "</button>"; }).join(""); });'
            'document.getElementById("open").onclick = function () {'
            'document.getElementById("apps").classList.toggle("open"); };</script></body></html>')


# Closed by moving it off the screen only: its tiles sit past the left edge.
DRAWER_SLID = _drawer("")
# Closed and visibility:hidden, as desktop skill §2b says a drawer should be.
DRAWER_HIDDEN = _drawer("visibility:hidden")


async def _phone_run() -> dict:
    import tempfile
    from mastercode.runtime import app_verify as AV
    from server.applications import artifacts as ART

    looks: list = []

    async def post(path, payload, *, what, label="", linger_s=0.0):
        looks.append((payload["width"], payload["height"], payload["page"]))
        body = DV.DocumentBody(**{k: payload[k] for k in
                                  ("page", "subject", "width", "height", "settle_ms", "probes")})
        return {"checked": True, "report": json.loads((await DV.verify_document(body)).body)}

    async def stand_in(_pool):
        return PHONE_APPS, "Sage"

    async def no_release(_linger_s):
        return None

    real = (AV.browser_sidecar_post, DR._stand_in, DR._release_after_first_look, ART._projects_root)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="phone-pass-"))
    AV.browser_sidecar_post, DR._stand_in = post, stand_in
    DR._release_after_first_look = no_release
    ART._projects_root = lambda: tmp / "projects"       # failed.json lives beside it
    out: dict = {}
    async with async_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch(args=list(DV.CHECK_BROWSER_ARGS))
        except Exception as exc:  # no browser binary installed
            return {"skip": f"chromium unavailable: {exc}"}
        DV.set_browser_provider(lambda: browser)
        try:
            for name, html in (("template", TEMPLATE_HTML), ("throws", PHONE_THROWS),
                               ("blank", PHONE_BLANK), ("row", ROW_DOCK), ("desktop_fails", NO_TILES),
                               ("drawer_slid", DRAWER_SLID), ("drawer_hidden", DRAWER_HIDDEN)):
                looks.clear()
                verdict = await DR.render_check(None, html, linger_s=0.0)
                out[name] = (verdict, list(looks))
        finally:
            AV.browser_sidecar_post, DR._stand_in, DR._release_after_first_look, ART._projects_root = real
            await browser.close()
    return out


@pytest.fixture(scope="module")
def phone():
    out = asyncio.run(_phone_run())
    if "skip" in out:
        pytest.skip(out["skip"])
    return out


def _sizes(looks):
    return [(w, h) for w, h, _page in looks]


def test_the_template_passes_on_a_phone_with_every_tile_inside_the_width(phone):
    verdict, looks = phone["template"]
    assert verdict["checked"] and verdict["ok"], verdict.get("failures")
    assert _sizes(looks) == [DR.VIEWPORT, DR.PHONE_VIEWPORT]
    assert verdict["phone"] == {"checked": True, "ok": True, "why": verdict["phone"]["why"],
                                "tiles_off": 0}
    # 24 apps on a 360 px screen: the grid wraps and scrolls, nothing sideways.
    assert not verdict.get("warnings"), verdict.get("warnings")


def test_the_phone_look_is_mounted_as_a_phone(phone):
    _verdict, looks = phone["template"]
    desktop_page, phone_page = looks[0][2], looks[1][2]
    assert '"phone": true' not in desktop_page
    assert '"phone": true' in phone_page
    assert f"width:{DR.PHONE_VIEWPORT[0]}px;height:{DR.PHONE_VIEWPORT[1]}px" in phone_page


def test_a_document_that_throws_only_on_a_phone_is_refused_on_a_phone(phone):
    verdict, looks = phone["throws"]
    assert _sizes(looks) == [DR.VIEWPORT, DR.PHONE_VIEWPORT]
    assert verdict["checked"] is True and verdict["ok"] is False
    assert verdict["why"].startswith(f"on a phone ({DR.PHONE_VIEWPORT[0]}×{DR.PHONE_VIEWPORT[1]}): ")
    assert "narrowLayout" in verdict["why"]
    assert f"line {_line_of(PHONE_THROWS, 'narrowLayout')}" in verdict["why"], verdict["why"]
    assert verdict["phone"]["ok"] is False


def test_a_document_that_shows_a_phone_nothing_is_refused(phone):
    verdict, looks = phone["blank"]
    assert verdict["ok"] is False and "on a phone" in verdict["why"]
    assert "shows nothing" in verdict["why"], verdict["why"]
    # A blank first look is looked at once more with the longer settle, at the
    # phone size too.
    assert _sizes(looks) == [DR.VIEWPORT, DR.PHONE_VIEWPORT, DR.PHONE_VIEWPORT]


def test_tiles_past_the_side_of_a_phone_pass_with_a_warning(phone):
    verdict, _looks = phone["row"]
    assert verdict["checked"] and verdict["ok"], verdict.get("failures")
    warns = verdict.get("warnings") or []
    assert len(warns) == 1 and "past the screen's side edges" in warns[0], warns
    # 24 tiles of 52 px with 10 px gaps, centred: 1478 px across a 360 px
    # screen, so most of them sit wholly past an edge.
    assert "of the 24 app tiles" in warns[0], warns[0]
    # The count a person's own door (Settings → Host design) words for them.
    assert f"{verdict['phone']['tiles_off']} of the 24 app tiles" in warns[0]
    assert verdict["phone"]["tiles_off"] > 0
    assert warns[0] in DR.format_render(verdict)
    assert DR.summary(verdict)["warnings"] == warns


def test_a_drawer_hidden_while_closed_passes_on_a_phone_without_a_warning(phone):
    # Desktop skill §2b and the warning both say a drawer is fine — this is
    # the drawer they describe, through the real probe.
    verdict, looks = phone["drawer_hidden"]
    assert _sizes(looks) == [DR.VIEWPORT, DR.PHONE_VIEWPORT]
    assert verdict["checked"] and verdict["ok"], verdict.get("failures")
    assert not verdict.get("warnings"), verdict.get("warnings")


def test_a_drawer_only_moved_off_the_screen_is_warned_with_how_to_close_it(phone):
    # The same drawer without visibility:hidden: its 24 tiles sit wholly past
    # the left edge, so the warning fires — and it must say what makes a
    # drawer pass, not just that a drawer "is fine".
    verdict, _looks = phone["drawer_slid"]
    assert verdict["checked"] and verdict["ok"], verdict.get("failures")
    warns = verdict.get("warnings") or []
    assert len(warns) == 1 and "24 of the 24 app tiles" in warns[0], warns
    assert "visibility:hidden while closed" in warns[0], warns[0]


def test_a_document_that_fails_at_desktop_size_is_not_loaded_again_as_a_phone(phone):
    verdict, looks = phone["desktop_fails"]
    assert verdict["ok"] is False and "phone" not in verdict
    assert _sizes(looks) == [DR.VIEWPORT]
