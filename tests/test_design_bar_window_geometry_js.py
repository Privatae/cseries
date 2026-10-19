"""Windows under a design's bar keep their place when grabbed — the REAL
window manager (dashboard/base/js/desktop.js) and the real desktop.css, in
Chromium, with the bar on every edge.

With `body.design-bar` the window layer is inset by the bar's strip.
Windows are positioned in the LAYER's coordinates, and drag / resize / maximize
used to read viewport coordinates and write them back as layer ones — so with a
top or left bar every grab moved the window by the bar's thickness (reproduced:
a 1 px drag took a window from 200/200 to 201/252). This pins:

  - a drag moves a window by exactly the pointer's travel, on every edge;
  - maximize then un-maximize returns the window to where it was;
  - with no design bar at all, nothing about window geometry changes.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_design_bar_window_geometry_js.py
"""
from __future__ import annotations

import pathlib

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the shell JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CSS = (_ROOT / "dashboard/base/css/tokens.css").read_text() + "\n" + \
    (_ROOT / "dashboard/base/css/desktop.css").read_text()
_JS = (_ROOT / "dashboard/base/js/desktop.js").read_text()

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>%(css)s</style></head>
<body class="%(body)s" style="%(vars)s">
<div id="desktop"><div id="window-layer"></div></div>
<script>%(js)s</script></body></html>"""


@pytest.fixture()
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _page(browser, edge):
    body, vars_ = "", ""
    if edge:
        body = f"design-bar design-bar-{edge}"
        vars_ = f"--design-bar-{edge}:52px"
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content(_PAGE % {"css": _CSS, "js": _JS, "body": body, "vars": vars_})
    pg.evaluate("WindowManager.create({id: 'w1', title: 'T', x: 200, y: 200, width: 500, height: 300})")
    pg.errors = errors  # type: ignore[attr-defined]
    return pg


def _pos(pg):
    return pg.evaluate("(() => { const s = document.getElementById('w1').style;"
                       " return [parseFloat(s.left), parseFloat(s.top)]; })()")


def _drag(pg, dx, dy):
    box = pg.locator("#w1 .window-title").bounding_box()
    x, y = box["x"] + 5, box["y"] + box["height"] / 2
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x + dx, y + dy, steps=3)
    pg.mouse.up()


@pytest.mark.parametrize("edge", [None, "top", "left", "bottom", "right"])
def test_a_drag_moves_a_window_by_the_pointers_travel(browser, edge):
    pg = _page(browser, edge)
    assert _pos(pg) == [200, 200]
    _drag(pg, 30, 20)
    assert _pos(pg) == [230, 220], (edge, _pos(pg))
    assert pg.errors == []


@pytest.mark.parametrize("edge", [None, "top", "left"])
def test_maximize_and_back_returns_the_window_where_it_was(browser, edge):
    pg = _page(browser, edge)
    pg.evaluate("WindowManager.toggleMaximize('w1')")
    pg.evaluate("WindowManager.toggleMaximize('w1')")
    assert _pos(pg) == [200, 200], (edge, _pos(pg))
