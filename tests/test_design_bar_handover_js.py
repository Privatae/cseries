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


def _serve(route, doc, apps=None, icons=None):
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
                             body=json.dumps({"applications": APPS if apps is None else apps}))
    if icons and path in icons:
        headers, body = icons[path]
        return route.fulfill(status=200, headers=headers, body=body)
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


def _shell(browser, doc, *, width=1440, height=900, apps=None, icons=None, before=""):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.set_default_timeout(HANG_MS)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route(f"{ORIGIN}/**", lambda route: _serve(route, doc, apps, icons))
    pg.goto(f"{ORIGIN}/")
    pg.wait_for_function("window.DesktopSurface && window.Taskbar")
    if before:
        pg.evaluate(before)
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


def _press(pg, frame, selector):
    """Put the mouse button down on a bar control and keep it down."""
    box = frame.locator(selector).bounding_box()
    pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pg.mouse.down()


def test_a_press_on_a_window_entry_survives_the_bar_being_redrawn_before_the_release(browser):
    # The shell re-posts its state 60 ms after every window event, and a click
    # is a press and a release: a post that changes the bar's markup in between
    # used to replace the pressed button, so the release landed on a new one and
    # no click fired. This holds the button down while another window opens,
    # waits for the redrawn bar to arrive in the frame, and only then releases.
    pg, frame, errors = _shell(browser, TEMPLATE_HTML)
    pg.evaluate("WindowManager.create('win-1-a', null, 'Artifact 249')")
    frame.wait_for_selector('[data-cseries-window="win-1-a"]')
    _press(pg, frame, '[data-cseries-window="win-1-a"]')
    pg.evaluate("WindowManager.create('win-2-b', null, 'Notes')")   # win-1-a loses focus
    frame.wait_for_selector('[data-cseries-window="win-2-b"]')       # the redrawn bar is in
    pg.mouse.up()
    # Its entry toggles: a window that is neither minimized nor focused is focused.
    pg.wait_for_function("WindowManager.getAll()[0].el.classList.contains('focused')")
    assert errors == []


# A design that leaves the window list to the shell's own renderer (the
# data-cseries-windows container the bridge fills) and draws none itself.
_SHELL_DRAWN_WINDOWS = TEMPLATE_HTML.replace(
    '<div id="wins"></div>', '<div id="wins"></div><div id="dw" data-cseries-windows></div>', 1,
).replace("S.windows.filter", "[].filter")


def test_a_press_on_an_entry_the_shell_drew_survives_the_bar_being_redrawn_before_the_release(browser):
    pg, frame, errors = _shell(browser, _SHELL_DRAWN_WINDOWS)
    entry = '#dw [data-cseries-window="win-1-a"]'
    pg.evaluate("WindowManager.create('win-1-a', null, 'Artifact 249')")
    frame.wait_for_selector(entry)
    _press(pg, frame, entry)
    pg.evaluate("WindowManager.create('win-2-b', null, 'Notes')")
    frame.wait_for_selector('#dw [data-cseries-window="win-2-b"]')
    pg.mouse.up()
    pg.wait_for_function("WindowManager.getAll()[0].el.classList.contains('focused')")
    assert errors == []


def test_a_press_on_a_dock_item_survives_the_bar_being_redrawn_before_the_release(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML)
    _press(pg, frame, '#dock [data-cseries-activate="circles"]')
    pg.evaluate("WindowManager.create('win-1-w', 'workbench', 'Workbench')")   # it starts running
    frame.wait_for_selector('#dock [data-cseries-activate="workbench"].on')    # the redrawn bar is in
    pg.mouse.up()
    pg.wait_for_function("window.__launched.length === 1")
    assert pg.evaluate("window.__launched") == ["circles"]
    assert errors == []



def _tag(frame, sel, name):
    frame.evaluate("(a) => { document.querySelector(a[0]).__mine = a[1]; }", [sel, name])


def _tags(frame, box):
    """Each entry's window id with the tag put on its element, in document order."""
    return frame.evaluate("""(b) => Array.from(document.querySelectorAll(b + ' [data-cseries-window]'))
        .map((e) => [e.getAttribute('data-cseries-window'), e.__mine || null])""", box)


def test_a_press_on_the_label_of_an_entry_survives_the_label_being_rewritten(browser):
    # The press lands on the label's own element (the button's child). A window
    # that is renamed while the button is held rewrites that label; the release
    # must still be a click on the button, so the label is updated in place.
    pg, frame, errors = _shell(browser, _SHELL_DRAWN_WINDOWS)
    pg.evaluate("WindowManager.create('win-1-a', null, 'Artifact 249')")
    label = '#dw [data-cseries-window="win-1-a"] .sk-window-title'
    frame.wait_for_selector(label)
    _press(pg, frame, label)
    pg.evaluate("wins[0].title = 'Renamed'; wins[0].el.classList.remove('focused'); ev('window:focused', { id: 'x' })")
    _frame_until(frame, "document.querySelector('#dw .sk-window-title').textContent === 'Renamed'")
    pg.mouse.up()
    pg.wait_for_function("WindowManager.getAll()[0].el.classList.contains('focused')")
    assert errors == []


def test_a_window_entry_is_never_kept_for_a_different_window_with_the_same_label(browser):
    # A control stays the same element only while it is the same control. The
    # first window goes and a second with the same label comes in one post: the
    # new entry is a new element, and carries nothing the old one was given.
    pg, frame, errors = _shell(browser, _SHELL_DRAWN_WINDOWS)
    pg.evaluate("WindowManager.create('win-1-a', null, 'Notes')")
    frame.wait_for_selector('#dw [data-cseries-window="win-1-a"]')
    _tag(frame, '#dw [data-cseries-window="win-1-a"]', "first")
    pg.evaluate("wins.splice(0, 1); WindowManager.create('win-2-b', null, 'Notes')")
    frame.wait_for_selector('#dw [data-cseries-window="win-2-b"]')
    assert _tags(frame, "#dw") == [["win-2-b", None]]
    assert errors == []


def test_a_reordered_window_list_moves_each_entry_with_its_own_window(browser):
    pg, frame, errors = _shell(browser, _SHELL_DRAWN_WINDOWS)
    for n in "abc":
        pg.evaluate(f"WindowManager.create('win-{n}', null, 'Window {n}')")
    frame.wait_for_selector('#dw [data-cseries-window="win-c"]')
    for n in "abc":
        _tag(frame, f'#dw [data-cseries-window="win-{n}"]', n)
    pg.evaluate("wins.reverse(); ev('window:focused', { id: 'win-a' })")
    _frame_until(frame, "document.querySelector('#dw [data-cseries-window]').getAttribute('data-cseries-window') === 'win-c'")
    assert _tags(frame, "#dw") == [["win-c", "c"], ["win-b", "b"], ["win-a", "a"]]
    assert frame.evaluate("Array.from(document.querySelectorAll('#dw .sk-window-title')).map((e) => e.textContent)") == [
        "Window c", "Window b", "Window a"]
    assert errors == []


def test_two_windows_with_one_id_are_two_entries_and_neither_is_lost(browser):
    pg, frame, errors = _shell(browser, _SHELL_DRAWN_WINDOWS)
    pg.evaluate("WindowManager.create('win-1-a', null, 'One')")
    frame.wait_for_selector('#dw [data-cseries-window="win-1-a"]')
    _tag(frame, '#dw [data-cseries-window="win-1-a"]', "kept")
    pg.evaluate("WindowManager.create('win-1-a', null, 'Two')")
    _frame_until(frame, "document.querySelectorAll('#dw [data-cseries-window]').length === 2")
    assert _tags(frame, "#dw") == [["win-1-a", "kept"], ["win-1-a", None]]
    pg.evaluate("wins.splice(1, 1); ev('window:closed', { id: 'win-1-a' })")
    _frame_until(frame, "document.querySelectorAll('#dw [data-cseries-window]').length === 1")
    assert _tags(frame, "#dw") == [["win-1-a", "kept"]]
    assert errors == []


# A shell-drawn container the author gave placeholder content: text, and an
# element that is no control.
_PLACEHOLDER_WINDOWS = _SHELL_DRAWN_WINDOWS.replace(
    '<div id="dw" data-cseries-windows></div>',
    '<div id="dw" data-cseries-windows>No windows open<em>none</em></div>', 1)


def test_content_the_author_left_in_a_shell_drawn_container_is_replaced_as_before(browser):
    pg, frame, errors = _shell(browser, _PLACEHOLDER_WINDOWS)
    pg.evaluate("WindowManager.create('win-1-a', null, 'Notes')")
    frame.wait_for_selector('#dw [data-cseries-window="win-1-a"]')
    assert frame.evaluate("document.getElementById('dw').innerHTML.indexOf('No windows') < 0")
    assert frame.evaluate("document.querySelector('#dw em') === null")
    assert errors == []


# ── The icon the document is handed is one fixed, safe form ───────────────
# A design inserts `a.icon` as markup, and an image icon is a URL from the
# app's manifest whose response (headers included) the app's own sidecar
# chooses. Whatever the source, the document must get no code.

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5f645400000000049454e44ae426082")
_ICON_APP = {"id": "evilapp", "name": "Evil", "system": True, "icon_url": "/icons/evil.png"}
_PWNED = "window.__pwned"


def _tile_icon(frame, app_id):
    frame.wait_for_selector(f'#apps [data-cseries-app="{app_id}"]')
    return frame.evaluate("""(id) => document.querySelector(
        '#apps [data-cseries-app="' + id + '"] .ic').innerHTML""", app_id)


def test_an_icon_response_cannot_put_code_into_the_tag_through_its_content_type(browser):
    # The type of the fetched icon became part of the <img> tag. A type that
    # closes the src attribute and opens an event handler used to run in the
    # design's frame (the handler fires when the broken image errors).
    hostile = {"content-type": 'image/png"/onerror=window.__pwned=1//'}
    pg, frame, errors = _shell(browser, TEMPLATE_HTML, apps=APPS + [_ICON_APP],
                               icons={"/icons/evil.png": (hostile, _PNG)})
    icon = _tile_icon(frame, "evilapp")
    frame.evaluate("new Promise((r) => setTimeout(r, 300))")    # an error event would have fired by now
    assert "onerror" not in icon and "pwned" not in icon, icon
    assert frame.evaluate(_PWNED) is None
    assert errors == []


def test_an_icon_the_shell_cannot_vouch_for_becomes_the_apps_letter(browser):
    for ctype in ("text/html", "image/png\"x", ""):
        headers = {"content-type": ctype} if ctype else {}
        pg, frame, errors = _shell(browser, TEMPLATE_HTML, apps=APPS + [_ICON_APP],
                                   icons={"/icons/evil.png": (headers, _PNG)})
        icon = _tile_icon(frame, "evilapp")
        assert icon == '<span class="sk-app-letter">E</span>', (ctype, icon)
        assert errors == []


def test_a_real_image_icon_still_renders_as_an_image(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML, apps=APPS + [_ICON_APP],
                               icons={"/icons/evil.png": ({"content-type": "image/png"}, _PNG)})
    icon = _tile_icon(frame, "evilapp")
    assert icon.startswith('<img src="data:image/png;base64,iVBOR') and icon.endswith('alt="">'), icon
    _frame_until(frame, "document.querySelector('#apps [data-cseries-app=evilapp] img').naturalWidth === 1")
    assert errors == []


# Anything the markup source could ever return, hostile or not.
_HOSTILE_ICON = (
    '<svg viewBox="0 0 24 24" onload="window.__pwned=1" style="x:y" fill="url(javascript:1)">'
    '<script>window.__pwned=1</script><path d="M1 1L2 2" onclick="window.__pwned=1" stroke="currentColor"/>'
    '<foreignObject><iframe srcdoc="x"></iframe></foreignObject><a href="javascript:1"><circle r="3"/></a>'
    '<style>*{x:y}</style></svg>'
    '<img src=x onerror="window.__pwned=1"><img src="javascript:1"><span class="sk-app-letter" onclick="1">A&lt;</span>')


def test_whatever_the_icon_source_returns_the_document_gets_only_the_safe_form(browser):
    pg, frame, errors = _shell(browser, TEMPLATE_HTML,
                               before="window.iconMarkup = () => %s" % json.dumps(_HOSTILE_ICON))
    icon = _tile_icon(frame, "circles")
    frame.evaluate("new Promise((r) => setTimeout(r, 300))")
    assert icon == ('<svg viewBox="0 0 24 24"><path d="M1 1L2 2" stroke="currentColor"></path>'
                    '</svg><span class="sk-app-letter">A&lt;</span>'), icon
    assert frame.evaluate(_PWNED) is None
    assert errors == []


def _glyph_ids():
    src = (_JS / "icons.js").read_text()
    block = src[src.index("const GLYPHS"):src.index("const ALIASES")]
    import re
    return [a or b for a, b in re.findall(r"^\s{8}(?:'([a-z0-9-]+)'|([a-z0-9_]+)):\s*`", block, re.M)]


def test_every_real_glyph_reaches_the_document_unchanged(browser):
    ids = _glyph_ids()
    assert len(ids) >= 30
    apps = [{"id": i, "name": i, "system": True} for i in ids]
    pg, frame, errors = _shell(browser, TEMPLATE_HTML, apps=apps)
    frame.wait_for_selector(f'#apps [data-cseries-app="{ids[-1]}"]')
    norm = """(h) => { const t = document.createElement('template'); t.innerHTML = h; return t.innerHTML; }"""
    for i in ids:
        want = pg.evaluate("(a) => window.iconMarkup({ id: a, name: a }, 40)", i)
        got = frame.evaluate("""(a) => document.querySelector('#apps [data-cseries-app="' + a + '"] .ic').innerHTML""", i)
        assert pg.evaluate(norm, got) == pg.evaluate(norm, want), i
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

