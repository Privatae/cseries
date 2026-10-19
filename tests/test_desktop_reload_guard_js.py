"""The shell takes off a desktop that reloads itself, however slowly — the REAL
desktop-surface.js in Chromium against a stubbed kernel.

The render check watches a document for about two seconds, so a desktop that
reloads every 3 s or every 10 s passes it and then flickers forever in the
shell. The shell therefore counts the `load` events of the worn desktop's
frame: more than 3 in 45 s and it reports the loop (POST
/api/appearance/desktop/loop, with the stamp of the version it is showing) and
mounts what the kernel then wears (desktop_pin.take_off_reloading puts the
previous version back).

This pins, with real timers and real reloads:
  - a document that reloads every 3 s is reported (at its 4th load) and the
    shell ends up showing the previous version, which does not reload;
  - the same for one that reloads every 10 s;
  - a document that reloads ONCE, and one that changes its hash and reloads
    once, are never reported.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_desktop_reload_guard_js.py
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
        {"id": "settings", "name": "Settings", "system": True}]


def _reloading(every_ms: int) -> str:
    return TEMPLATE_HTML.replace(
        "</body>", "<script>setTimeout(function(){location.reload()},%d)</script></body>" % every_ms, 1)


LOOP_3S = _reloading(3000)
LOOP_10S = _reloading(10000)
ONCE = TEMPLATE_HTML.replace(
    "</body>", "<script>if(window.name!=='once'){window.name='once';"
    "setTimeout(function(){location.reload()},500)}</script></body>", 1)
HASH_ONCE = TEMPLATE_HTML.replace(
    "</body>", "<script>if(window.name!=='h1'){window.name='h1';location.hash='x';"
    "setTimeout(function(){location.reload()},500)}</script></body>", 1)

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style></head><body>
<div id="desktop"><div id="window-layer"></div><div id="presence-line">Sage is here</div></div>
<div id="floating-dock"></div>
<div id="taskbar"><div id="taskbar-left"></div><div id="taskbar-center"></div>
  <div id="taskbar-right"><span id="entity-name">Sage</span>
  <button id="tray-resources">1.2G of 4G</button><button id="btn-support">?</button>
  <button id="btn-show-desktop"></button></div></div>
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
            "taskbar.js", "desktop-surface.js"]


def _serve(route, scene):
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
        return route.fulfill(status=200, content_type="text/html", body=scene["doc"])
    if path == "/api/appearance/desktop/meta":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"updated_at": scene["stamp"]}))
    if path == "/api/appearance/desktop/loop" and route.request.method == "POST":
        scene["reports"].append(json.loads(route.request.post_data or "{}"))
        if scene.get("refuse", 0) > 0:
            # "Not worn long enough yet" (desktop_pin.LOOP_MIN_WORN_S): the
            # kernel changes nothing and the shell must report again.
            scene["refuse"] -= 1
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"taken_off": False}))
        # What the kernel does: the previous version is worn again, under a
        # new stamp.
        scene["doc"], scene["stamp"] = TEMPLATE_HTML, "2"
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"taken_off": True}))
    if path == "/api/system/status":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"entity_display_name": "Sage"}))
    return route.fulfill(status=404, body="")


def _open(ctx, doc, refuse=0):
    scene = {"doc": doc, "stamp": "1", "reports": [], "refuse": refuse}
    pg = ctx.new_page()
    pg.route(f"{ORIGIN}/**", lambda route: _serve(route, scene))
    pg.goto(f"{ORIGIN}/")
    pg.wait_for_function("window.DesktopSurface && window.Taskbar")
    pg.evaluate("""DesktopSurface.apply({wallpaper: 'html',
        desktop_html_url: '/api/appearance/desktop', desktop_updated_at: '1'})""")
    pg.wait_for_function("DesktopSurface.bound", timeout=8000)
    return pg, scene


@pytest.fixture(scope="module")
def shells():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctxs = [b.new_context(viewport={"width": 1200, "height": 800}) for _ in range(5)]
        # All four run at once: the 10 s loop needs ~30 s of real time.
        opened = {n: _open(c, d, refuse=r) for (n, d, r), c in zip(
            [("loop3", LOOP_3S, 0), ("loop10", LOOP_10S, 0), ("once", ONCE, 0),
             ("hash_once", HASH_ONCE, 0), ("early", LOOP_3S, 1)], ctxs)}
        out = {}
        for n in ("loop3", "loop10"):
            pg, scene = opened[n]
            deadline_ms = 44000
            while not scene["reports"] and deadline_ms > 0:
                pg.wait_for_timeout(500)
                deadline_ms -= 500
            # The shell then mounts the version the kernel wears now.
            pg.wait_for_function(
                "(() => { const f = document.querySelector('iframe.desktop-surface.is-live');"
                " return !!f && !/location\\.reload/.test(f.srcdoc); })()", timeout=8000)
            out[n] = (scene["reports"], pg.evaluate(
                "document.querySelectorAll('iframe.desktop-surface').length"))
        pg, scene = opened["early"]
        deadline_ms = 20000
        while len(scene["reports"]) < 2 and deadline_ms > 0:
            pg.wait_for_timeout(500)
            deadline_ms -= 500
        pg.wait_for_function(
            "(() => { const f = document.querySelector('iframe.desktop-surface.is-live');"
            " return !!f && !/location\\.reload/.test(f.srcdoc); })()", timeout=8000)
        out["early"] = (scene["reports"], 0)
        for n in ("once", "hash_once"):
            pg, scene = opened[n]
            pg.wait_for_timeout(3000)
            out[n] = (list(scene["reports"]), pg.evaluate(
                "document.querySelector('iframe.desktop-surface')._skLoads.length"))
        for c in ctxs:
            c.close()
        b.close()
    return out


def test_a_3_second_loop_is_reported_and_taken_off(shells):
    reports, _ = shells["loop3"]
    assert len(reports) == 1 and reports[0] == {"updated_at": "1"}, reports


def test_a_10_second_loop_is_reported_and_taken_off(shells):
    reports, _ = shells["loop10"]
    assert len(reports) == 1 and reports[0] == {"updated_at": "1"}, reports


def test_a_single_reload_is_not_reported(shells):
    reports, loads = shells["once"]
    assert reports == [] and loads == 2, (reports, loads)


def test_a_hash_change_and_one_reload_is_not_reported(shells):
    reports, loads = shells["hash_once"]
    assert reports == [] and loads == 2, (reports, loads)


def test_a_report_refused_as_too_early_is_made_again(shells):
    reports, _ = shells["early"]
    assert len(reports) == 2 and reports[0] == reports[1] == {"updated_at": "1"}, reports
