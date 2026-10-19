"""The bar hand-over, driven in a real browser against the REAL shell modules.

The toolbar was extra noise; integrating its function into the
desktop UI template makes a better product. A desktop that declares
<meta name="cseries-bar" content="bottom 52"> draws the bar itself; the shell
hides #taskbar and keeps windows out of that strip. This loads, unmodified,
sandbox-csp.js, viewport.js, desktop-layout.js, icons.js, taskbar.js and
desktop-surface.js into a page that stubs only what the rest of the shell
would provide (WindowManager, AppRunner, DockMenu, ContextMenu), mounts
desktop_contract.TEMPLATE_HTML through the real DesktopSurface.apply, and pins:

  - the hand-over: body.design-bar, #taskbar hidden, the window layer inset by
    exactly the declared 52 px, and the design's own bar visible in the frame;
  - the design's bar DRIVES the shell: a real click on its dock item goes
    through Taskbar.activate (→ AppRunner.launch for an app not running), a
    click on a window entry minimizes / brings back that window, and
    show-desktop holds every window down;
  - the gesture rule still holds: a script call with no click answers false;
  - a design that declares NO bar leaves the shell's taskbar in place and is
    told nothing about windows or the dock;
  - on a phone viewport the shell keeps its taskbar and tells the design
    `ownsBar: false`, so the template hides its bar.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_design_bar_handover_js.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the shell JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

_JS = _ROOT / "dashboard" / "base" / "js"
_CSS = _ROOT / "dashboard" / "base" / "css"
ORIGIN = "http://shell.test"
APPS = [{"id": "circles", "name": "Circles", "system": True},
        {"id": "workbench", "name": "Workbench", "system": True},
        {"id": "settings", "name": "Settings", "system": True}]
NO_BAR = TEMPLATE_HTML.replace('<meta name="cseries-bar" content="bottom 52">\n', "", 1)

# The shell page: the real stylesheet and the real modules, in index.html's
# order, with only the rest of the shell stubbed.
_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style></head><body>
<div id="desktop"><div id="window-layer"></div><div id="presence-line">Sage is here</div></div>
<div id="floating-dock"></div>
<div id="taskbar"><div id="taskbar-left"></div><div id="taskbar-center"></div>
  <div id="taskbar-right"><span id="entity-name">Sage</span>
  <button id="tray-resources">1.2G of 4G</button><button id="btn-support">?</button>
  <button id="btn-show-desktop"></button></div></div>
<div id="tray-resources-panel" class="tray-panel hidden"><div id="tray-resources-detail"></div></div>
<div id="support-modal" class="hidden support-modal">help</div>
<script>
  window.__launched = [];
  const wins = [];
  function ev(name, detail) { document.dispatchEvent(new CustomEvent(name, { detail })); }
  window.WindowManager = {
    getAll: () => wins.slice(),
    create(id, appId, title) {
      const el = document.createElement('div'); el.className = 'window focused';
      document.getElementById('window-layer').appendChild(el);
      wins.forEach((w) => w.el.classList.remove('focused'));
      const w = { id, appId, title, minimized: false, el }; wins.push(w);
      ev('window:created', { id, appId, title }); ev('window:focused', { id });
    },
    focus(id) { wins.forEach((w) => w.el.classList.toggle('focused', w.id === id)); ev('window:focused', { id }); },
    minimize(id) { const w = wins.find((x) => x.id === id); w.minimized = true; w.el.classList.remove('focused'); ev('window:minimized', { id }); },
    restore(id) { const w = wins.find((x) => x.id === id); w.minimized = false; this.focus(id); ev('window:restored', { id }); },
    close(id) {},
  };
  window.AppRunner = { launch: (id) => window.__launched.push(id) };
  window.DockMenu = { open() { window.__launcher = (window.__launcher || 0) + 1; }, close() {} };
  window.ContextMenu = { show() {}, close() {} };
</script>
%(scripts)s
</body></html>"""

_MODULES = ["sandbox-csp.js", "viewport.js", "desktop-layout.js", "icons.js",
            "taskbar.js", "tray-status.js", "desktop-surface.js"]
RESOURCES = {"memory": {"used_bytes": 1288490188, "limit_bytes": 4294967296},
             "cpu": {"percent": 3, "cores": 2},
             "processes": {"count": 1, "top": [{"name": "uvicorn-cseries", "pid": 41,
                                                "rss_bytes": 314572800}]}}


def _serve(route, doc):
    url = route.request.url
    if url.endswith("/"):
        body = _PAGE % {
            "css": (_CSS / "tokens.css").read_text() + "\n" + (_CSS / "desktop.css").read_text(),
            "scripts": "\n".join(f'<script src="/js/{m}"></script>' for m in _MODULES),
        }
        return route.fulfill(status=200, content_type="text/html", body=body)
    path = url[len(ORIGIN):].split("?")[0]
    if path.startswith("/js/"):
        return route.fulfill(status=200, content_type="application/javascript",
                             body=(_JS / path[len("/js/"):]).read_text())
    if path == "/api/applications":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"applications": APPS}))
    if path == "/api/appearance":
        return route.fulfill(status=200, content_type="application/json", body="{}")
    if path == "/api/appearance/desktop":
        return route.fulfill(status=200, content_type="text/html", body=doc)
    if path == "/api/appearance/desktop/meta":
        # The shell's 3 s change poll: unchanged (a 404 here means "pointer
        # cleared" and the shell would take the desktop down mid-test).
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"updated_at": "1"}))
    if path == "/api/system/resources":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps(RESOURCES))
    if path == "/api/system/status":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"entity_display_name": "Sage"}))
    return route.fulfill(status=404, body="")


# Every wait below is on a condition: the page's state, never a length of
# time. A busy machine only makes a condition arrive later. HANG_MS is how
# long a condition is waited for before the test says it never held; it
# bounds a broken build, not the code's speed.
HANG_MS = 30_000


def _frame_until(frame, cond: str) -> None:
    """Wait until `cond` holds in the design's frame. Its CSP forbids
    wait_for_function's eval, so the page checks it from a promise."""
    frame.evaluate("""new Promise((ok, no) => {
        setTimeout(() => no(new Error("never held: %s")), %d);
        (function w() { (%s) ? ok(true) : setTimeout(w, 20); })();
    })""" % (cond.replace('"', "'"), HANG_MS, cond))


# The design has its first state from the shell (CSeriesDesktop.on calls back at
# once when it has).
_READY = ("window.CSeriesDesktop && (function () { var g = false;"
          " CSeriesDesktop.on(function () { g = true; })(); return g; })()")


@pytest.fixture()
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _shell(browser, doc, *, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.set_default_timeout(HANG_MS)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route(f"{ORIGIN}/**", lambda route: _serve(route, doc))
    pg.goto(f"{ORIGIN}/")
    pg.wait_for_function("window.DesktopSurface && window.Taskbar")
    pg.evaluate("""DesktopSurface.apply({wallpaper: 'html',
        desktop_html_url: '/api/appearance/desktop', desktop_updated_at: '1'})""")
    pg.wait_for_function("DesktopSurface.bound")
    pg.wait_for_function("window.frames.length > 0")
    frame = next(f for f in pg.frames if f != pg.main_frame)
    _frame_until(frame, _READY)
    return pg, frame, errors


def test_the_design_takes_the_bar_and_windows_keep_out_of_it(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML)
    assert pg.evaluate("document.body.classList.contains('design-bar')")
    assert pg.evaluate("getComputedStyle(document.getElementById('taskbar')).display") == "none"
    layer = pg.evaluate("""(() => { const d = document.getElementById('desktop').getBoundingClientRect(),
        w = document.getElementById('window-layer').getBoundingClientRect();
        return [d.bottom, w.bottom]; })()""")
    assert layer[0] - layer[1] == 52, layer
    assert frame.evaluate("CSeriesDesktop.ownsBar") is True
    assert frame.locator("#bar").is_visible()
    assert frame.locator("#dock [data-cseries-activate]").count() >= 1
    assert errors == []


def test_the_designs_bar_drives_the_real_shell(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML)
    # The dock: an app that is not running is launched through Taskbar.activate.
    frame.locator('#dock [data-cseries-activate="circles"]').click()
    pg.wait_for_function("window.__launched.length === 1")   # the port is async
    assert pg.evaluate("window.__launched") == ["circles"]

    # A shell window with no app shows in the bar's window list; a click
    # minimizes it (it is focused), a second brings it back.
    pg.evaluate("WindowManager.create('win-1-a', null, 'Artifact 249')")
    frame.wait_for_selector('[data-cseries-window="win-1-a"]')
    frame.locator('[data-cseries-window="win-1-a"]').click()
    pg.wait_for_function("WindowManager.getAll()[0].minimized === true")
    frame.locator('[data-cseries-window="win-1-a"]').click()
    pg.wait_for_function("WindowManager.getAll()[0].minimized === false")

    # Show desktop holds every window down; the launcher opens the real one.
    frame.locator("[data-cseries-show-desktop]").click()
    pg.wait_for_function("WindowManager.getAll().every((w) => w.minimized)")
    frame.locator("[data-cseries-launcher]").click()
    pg.wait_for_function("window.__launcher === 1")
    # Help is the shell's own panel.
    frame.locator("[data-cseries-help]").click()
    pg.wait_for_function("!document.getElementById('support-modal').classList.contains('hidden')")
    assert errors == []


def test_the_shells_machine_panel_lives_outside_the_taskbar():
    # body.design-bar hides #taskbar (display:none); a panel inside it can never
    # show, whatever the design's bar does.
    html = (_ROOT / "dashboard" / "base" / "index.html").read_text()
    tb = html.index('<div id="taskbar"')
    panel = html.index('id="tray-resources-panel"')
    # #taskbar's closing tag: the first line after it that closes at its indent.
    indent = html[html.rindex("\n", 0, tb) + 1:tb]
    tb_end = html.index("\n" + indent + "</div>", tb)
    assert not (tb < panel < tb_end), "the machine panel is inside #taskbar"


def test_the_designs_machine_line_opens_the_shells_process_list(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML)
    panel = "document.getElementById('tray-resources-panel')"
    shown = f"getComputedStyle({panel}).display !== 'none'"
    _frame_until(frame, "CSeriesDesktop.machine !== ''")
    frame.locator("[data-cseries-machine]").click()
    pg.wait_for_function(f"{shown} && {panel}.textContent.includes('uvicorn-cseries')")
    # The process list is the shell's: the frame was never handed it.
    assert "uvicorn-cseries" not in frame.evaluate("JSON.stringify(CSeriesDesktop)")
    # A second click on the same control closes it (the press must not close
    # the panel only for the click to reopen it).
    frame.locator("[data-cseries-machine]").click()
    pg.wait_for_function(f"!({shown})")
    # A press anywhere else in the design closes it too.
    frame.locator("[data-cseries-machine]").click()
    pg.wait_for_function(shown)
    pg.mouse.click(1000, 450)        # the design's empty middle, not its bar
    pg.wait_for_function(f"!({shown})")
    assert errors == []


def test_a_machine_press_in_a_design_without_the_bar_still_closes_the_panel(browser):
    # desktop:machine is the bar owner's alone. A design that does NOT own the
    # bar but marks something data-cseries-machine must not get the press
    # exemption either: the press closes the shell's panel like any other.
    doc = NO_BAR.replace("</body>", '<button id="mx" type="button" data-cseries-machine '
                         'style="position:fixed;left:400px;top:400px;z-index:9">m</button></body>', 1)
    pg, frame, errors = _shell(browser, doc)
    assert pg.evaluate("document.body.classList.contains('design-bar')") is False
    panel = "document.getElementById('tray-resources-panel')"
    shown = f"getComputedStyle({panel}).display !== 'none'"
    pg.locator("#tray-resources").click()           # the shell's own chip
    pg.wait_for_function(shown)
    frame.locator("#mx").click()
    pg.wait_for_function(f"!({shown})")
    assert errors == []


def test_no_click_no_action(browser):
    pg, frame, _ = _shell(browser, TEMPLATE_HTML)
    frame.wait_for_timeout(1200)   # past the bridge's one-second gesture window
    assert frame.evaluate("CSeriesDesktop.toggleDesktop()") is False
    assert frame.evaluate("CSeriesDesktop.activate('circles')") is False
    assert frame.evaluate("CSeriesDesktop.openMachine()") is False
    assert pg.evaluate("window.__launched") == []


def test_a_design_without_a_bar_keeps_the_shells_taskbar(browser):
    pg, frame, errors = _shell(browser, NO_BAR)
    assert not pg.evaluate("document.body.classList.contains('design-bar')")
    assert pg.evaluate("getComputedStyle(document.getElementById('taskbar')).display") != "none"
    _frame_until(frame, "CSeriesDesktop.ownsBar === false")      # re-told asynchronously
    assert frame.evaluate("CSeriesDesktop.dock.length") == 0
    assert frame.evaluate("CSeriesDesktop.windows.length") == 0
    assert not frame.locator("#bar").is_visible()
    assert errors == []


def test_a_phone_keeps_the_shells_taskbar(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML, width=390, height=844)
    assert not pg.evaluate("document.body.classList.contains('design-bar')")
    assert frame.evaluate("CSeriesDesktop.ownsBar") is False
    assert not frame.locator("#bar").is_visible()
    assert errors == []


# A document that patches MessagePort.prototype.postMessage to capture the
# bridge's private port on the next relay (this worked before the bridge
# pinned its send), then uses it with no click.
_THIEF = TEMPLATE_HTML.replace("<body>", """<body>
<script>
  (function () {
    var real = MessagePort.prototype.postMessage;
    MessagePort.prototype.postMessage = function (m) { window.__stolen = this; return real.apply(this, arguments); };
    (function w() {          // tries once the test says the bait has passed (__go)
      if (!window.__go) return setTimeout(w, 20);
      if (window.__stolen) window.__stolen.postMessage({ type: "desktop:show-desktop" });
      window.__tried = true;
    })();
  })();
</script>""", 1)


def test_a_patched_postmessage_cannot_steal_the_port(browser):
    pg, frame, _ = _shell(browser, _THIEF)
    pg.evaluate("WindowManager.create('win-2-b', null, 'Notes')")
    # One real click on the bare desktop — its pointerdown relay is what a thief waits for.
    frame.locator("#sky").click(position={"x": 700, "y": 110}, force=True)
    frame.evaluate("window.__go = true")              # the click's relay has gone by
    _frame_until(frame, "window.__tried === true")    # and the thief has had its go
    assert frame.evaluate("!!window.__stolen") is False
    assert pg.evaluate("WindowManager.getAll()[0].minimized") is False


# Declares the bar, then draws neither a dock nor a window list (the floor
# refuses this; a document can still reach the screen mid-iteration).
_BARLESS = TEMPLATE_HTML.replace('<div id="dock"></div>', "").replace('<div id="wins"></div>', "") \
    .replace('$("dock").innerHTML', "void 0").replace('$("wins").innerHTML', "void 0") \
    .replace("S.dock.map", "[].map").replace("S.windows.filter", "[].filter")


def test_a_declared_bar_that_draws_nothing_gives_the_taskbar_back(browser):
    pg, frame, errors = _shell(browser, _BARLESS)
    assert pg.evaluate("document.body.classList.contains('design-bar')")
    pg.wait_for_function("!document.body.classList.contains('design-bar')")
    assert pg.evaluate("getComputedStyle(document.getElementById('taskbar')).display") != "none"
    _frame_until(frame, "CSeriesDesktop.ownsBar === false")      # re-told asynchronously


# The receive side: MessageEvent.prototype.data is a getter whose `this.target`
# is the bridge's port — a document that redefined it used to get the port on
# the shell's next routine state post, with no click at all.
_DATA_THIEF = TEMPLATE_HTML.replace("<body>", """<body>
<script>
  (function () {
    var d = Object.getOwnPropertyDescriptor(MessageEvent.prototype, "data");
    Object.defineProperty(MessageEvent.prototype, "data", { configurable: true,
      get: function () { if (this.target instanceof MessagePort) window.__stolen = this.target; return d.get.call(this); } });
    (function w() {          // tries once the test says the bait has passed (__go)
      if (!window.__go) return setTimeout(w, 20);
      if (window.__stolen) window.__stolen.postMessage({ type: "desktop:show-desktop" });
      window.__tried = true;
    })();
  })();
</script>""", 1)


def test_a_patched_message_data_getter_cannot_steal_the_port(browser):
    pg, frame, _ = _shell(browser, _DATA_THIEF)
    pg.evaluate("WindowManager.create('win-3-c', null, 'Notes')")   # a state post follows
    frame.wait_for_selector('[data-cseries-window="win-3-c"]')        # ...and has arrived
    frame.evaluate("window.__go = true")
    _frame_until(frame, "window.__tried === true")    # the thief has had its go
    assert frame.evaluate("!!window.__stolen") is False
    assert pg.evaluate("WindowManager.getAll()[0].minimized") is False


# Declares the bar and draws its dock from state — but only 3 s in, after the
# safety net has already given the taskbar back. The shell keeps sending the
# dock while the net holds, so the late bar is seen and the bar handed back.
_LATE = _BARLESS.replace("CSeriesDesktop.on(draw);", """CSeriesDesktop.on(draw);
  setTimeout(function () { CSeriesDesktop.on(function (S) {
    var d = document.getElementById("late-dock") || document.body.appendChild(document.createElement("div"));
    d.id = "late-dock";
    d.innerHTML = S.dock.map(function (a) { return '<button data-cseries-activate="' + a.id + '">' + a.name + '</button>'; }).join("");
  }); }, 3000);""", 1)


def test_a_bar_drawn_late_is_handed_back(browser):
    pg, frame, errors = _shell(browser, _LATE)
    pg.wait_for_function("!document.body.classList.contains('design-bar')")
    # No further state post is needed: the bridge re-checks while the net holds.
    pg.wait_for_function("document.body.classList.contains('design-bar')")

