"""Drive `dashboard/base/js/desktop-preview.js` in a real browser.

The Designs shelf shows every design as the real document, rendered live. A
design reads `window.CSeriesDesktop` from its first line (the template greets
the Host by name), and outside the shell that object does not exist — so a
bare preview renders blank. desktop-preview.js injects an inert stand-in.
This pins, against the platform's own desktop template (desktop_contract
TEMPLATE_HTML, the document every Host starts from):

  - the template draws: one tile per app handed in, the Host's name in the
    greeting, and no script error;
  - the stand-in is INERT: launch / openLauncher answer false;
  - an app name carrying `</script>` cannot close the injected script and
    run markup of its own;
  - a wallpaper-mode design gets no app list (the same scoping as the real
    bridge — a wallpaper does not draw tiles).

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_desktop_preview_js.py
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

_JS = (_ROOT / "dashboard" / "base" / "js" / "desktop-preview.js").read_text()

APPS = [{"id": "circles", "name": "Circles"}, {"id": "designs", "name": "Designs"},
        {"id": "evil", "name": "</script><script>window.__pwned=1</script>"}]


@pytest.fixture()
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _instrument(browser, html, opts):
    """Run the real module to produce the instrumented document."""
    pg = browser.new_page()
    pg.set_content("<!doctype html><html><body></body></html>")
    pg.add_script_tag(content=_JS)
    out = pg.evaluate("([h, o]) => window.DesktopPreview.instrument(h, o)", [html, opts])
    pg.close()
    return out


def _render(browser, doc):
    pg = browser.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content(doc)
    pg.wait_for_timeout(300)
    return pg, errors


def test_the_template_draws_with_the_hosts_apps_and_name(browser):
    doc = _instrument(browser, TEMPLATE_HTML, {"apps": APPS, "host": "Sage"})
    pg, errors = _render(browser, doc)
    assert errors == []
    assert pg.locator("#apps .tile").count() == len(APPS)
    assert "Sage" in pg.locator("#greet").inner_text()
    assert pg.evaluate("window.CSeriesDesktop.launch('circles')") is False
    assert pg.evaluate("window.CSeriesDesktop.openLauncher()") is False


def test_a_design_that_draws_the_bar_is_previewed_with_it(browser):
    """The template declares cseries-bar: its bar shows, with a dock of the
    host's apps, and every bar control is inert."""
    doc = _instrument(browser, TEMPLATE_HTML, {"apps": APPS, "host": "Sage"})
    pg, errors = _render(browser, doc)
    assert errors == []
    assert pg.evaluate("window.CSeriesDesktop.ownsBar") is True
    assert pg.locator("#bar").is_visible()
    assert pg.locator("#dock [data-cseries-activate]").count() == len(APPS)
    assert "Sage" in pg.locator("#who-name").inner_text()
    for call in ("activate('circles')", "focusWindow('w1')", "toggleDesktop()", "help()"):
        assert pg.evaluate(f"window.CSeriesDesktop.{call}") is False


def test_on_a_phone_the_bar_stays_hidden(browser):
    doc = _instrument(browser, TEMPLATE_HTML, {"apps": APPS, "host": "Sage", "phone": True})
    pg, errors = _render(browser, doc)
    assert errors == []
    assert pg.evaluate("window.CSeriesDesktop.ownsBar") is False
    assert not pg.locator("#bar").is_visible()


def test_an_app_name_cannot_break_out_of_the_injected_script(browser):
    doc = _instrument(browser, TEMPLATE_HTML, {"apps": APPS, "host": "Sage"})
    pg, errors = _render(browser, doc)
    assert pg.evaluate("window.__pwned") is None
    assert errors == []


def test_a_wallpaper_gets_no_app_list(browser):
    wall = TEMPLATE_HTML.replace('content="desktop"', 'content="wallpaper"', 1)
    doc = _instrument(browser, wall, {"apps": APPS, "host": "Sage"})
    pg, errors = _render(browser, doc)
    assert pg.evaluate("window.CSeriesDesktop.apps.length") == 0
    assert pg.evaluate("window.CSeriesDesktop.mode") == "wallpaper"
    assert json.loads(pg.evaluate("JSON.stringify(window.CSeriesDesktop.all)")) == []


# A subscriber that throws: the real bridge swallows it (desktop-surface.js
# BRIDGE_SRC `on`), and so does every stand-in — the preview and the render
# check alike, so a document cannot tell them apart. The stand-in also LOGS it
# to the console through console.error captured before any document script
# ran: the render check reads that over CDP to name the line a design's draw()
# broke on. Nothing reaches the document's own window.onerror.
_THROWING_DRAW = TEMPLATE_HTML.replace(
    "CSeriesDesktop.on(", "CSeriesDesktop.on(function(){ missingHelper(); }); CSeriesDesktop.on(", 1)


def test_a_preview_swallows_a_throwing_subscriber_like_the_shell(browser):
    doc = _instrument(browser, _THROWING_DRAW, {"apps": APPS, "host": "Sage"})
    pg, errors = _render(browser, doc)
    assert errors == []
    assert pg.locator("#apps .tile").count() == len(APPS)


def test_it_is_logged_with_the_documents_own_stack_and_nothing_the_document_sees(browser):
    # The document replaces console.error and watches window.onerror from its
    # first script: neither may see the logged throw.
    watched = _THROWING_DRAW.replace(
        "<head>", "<head><script>window.__seen=[];console.error=function(){window.__seen.push('console')};"
                  "window.onerror=function(){window.__seen.push('onerror')};</script>", 1)
    doc = _instrument(browser, watched, {"apps": APPS, "host": "Sage"})
    pg = browser.new_page()
    logged: list[str] = []
    uncaught: list[str] = []
    pg.on("console", lambda m: logged.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: uncaught.append(str(e)))
    pg.set_content(doc)
    pg.wait_for_timeout(300)
    assert uncaught == []
    marked = [t for t in logged if t.startswith("cseries desktop: a CSeriesDesktop.on callback threw")]
    assert len(marked) == 1 and "missingHelper is not defined" in marked[0]
    # The ORIGINAL stack, thrown inside CSeriesDesktop.on: its frames are the
    # document's. (Line numbers against the document itself are pinned in a
    # srcdoc frame, the way the check mounts it: tests/test_desktop_render_check.py.)
    assert "Object.on" in marked[0]
    assert pg.evaluate("window.__seen") == []
    # ...and the rest of the design still drew.
    assert pg.locator("#apps .tile").count() == len(APPS)


def test_the_stand_in_carries_no_flag_the_document_could_read(browser):
    doc = _instrument(browser, TEMPLATE_HTML, {"apps": APPS, "host": "Sage", "reportErrors": True})
    assert "var R=" not in doc and "reportErrors" not in doc


def test_with_no_memory_line_the_machine_control_is_not_drawn(browser):
    """The stand-in has no memory line (machine is ''), as the real shell has
    none until its first resources poll lands. The template hides #machine
    then; `#bar button {display:inline-grid}` must not override `hidden` and
    leave an empty pressable button in the bar."""
    doc = _instrument(browser, TEMPLATE_HTML, {"apps": APPS, "host": "Sage"})
    pg, errors = _render(browser, doc)
    assert errors == []
    assert pg.evaluate("window.CSeriesDesktop.machine") == ""
    assert pg.evaluate("document.getElementById('machine').hidden") is True
    assert not pg.locator("#machine").is_visible()
