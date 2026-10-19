"""Calendar reaches a dock its owner has arranged, once, driven in a real browser
against the REAL shell modules (desktop-layout.js, icons.js, taskbar.js).

The count of what is still scheduled today moved from the Circles bubble to the
Calendar dock icon, and a new Host's dock holds Calendar after Circles.
But an arranged dock keeps its owner's list forever, so it would never get
Calendar, and its owner would lose the count. So arranged docks get Calendar
once too. This pins:

  - an arranged dock gets Calendar after Circles (at the end without Circles),
    saved with dock_offered, and the dock shows it;
  - an arranged dock that already had it offered is left as its owner left it:
    taken out stays out, and nothing is saved;
  - a dock nobody arranged shows the defaults (Calendar after Circles) and
    saves nothing;
  - taking Calendar out after the offer saves the dock without it and keeps the
    offer recorded, and a later start leaves it out;
  - arranging a dock nobody had arranged records the offer, so taking Calendar
    out later sticks; Reset puts the defaults back with the offer recorded;
  - every app offered once is also in the default dock (arranging the default
    dock and Reset rely on it);
  - a desktop page that does not know the offer (one loaded before the
    update, still open) saving a dock without Calendar makes the current
    desktop offer it once more, rather than losing it for good;
  - the server keeps dock_offered like the other id lists, and merges as above.

The stubbed store below validates and merges every save with the server's own
functions (server/routes/appearance.py _clean_layout, _merge_layout).

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_dock_offer_once_js.py
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
sys.path.insert(0, str(_ROOT / "brain"))
_JS = _ROOT / "dashboard" / "base" / "js"
_CSS = _ROOT / "dashboard" / "base" / "css"
ORIGIN = "http://shell.test"
APPS = [{"id": i, "name": i.title(), "system": True}
        for i in ("circles", "calendar", "workbench", "artifacts", "context-manager", "app-store", "settings")]
HANG_MS = 30_000

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style></head><body>
<div id="desktop"><div id="window-layer"></div></div>
<div id="taskbar"><div id="taskbar-left"></div><div id="taskbar-center"></div>
  <div id="taskbar-right"><button id="btn-show-desktop"></button></div></div>
<script>
  window.WindowManager = { getAll: () => [], focus() {}, minimize() {}, restore() {}, close() {} };
  window.AppRunner = { launch() {} };
</script>
%(scripts)s
</body></html>"""

_MODULES = ["desktop-layout.js", "icons.js", "taskbar.js"]

from server.routes.appearance import _clean_layout, _merge_layout  # noqa: E402


class Store:
    """GET /api/appearance and PUT /api/appearance/layout, merging key by key
    as server/routes/appearance.py does."""

    def __init__(self, layout=None):
        self.layout = dict(layout) if layout is not None else None
        self.puts: list[dict] = []

    def serve(self, route):
        req = route.request
        url = req.url
        if url.rstrip("/") == ORIGIN:
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
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"desktop_layout": self.layout}))
        if path == "/api/appearance/layout" and req.method == "PUT":
            body = json.loads(req.post_data or "{}")
            self.puts.append(body)
            self.save(body)
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"desktop_layout": self.layout}))
        return route.fulfill(status=404, body="")

    def save(self, body):
        """As the server's PUT /api/appearance/layout stores a save."""
        self.layout = _merge_layout(self.layout, _clean_layout(body))


@pytest.fixture()
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _desktop(browser, store):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.set_default_timeout(HANG_MS)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route(f"{ORIGIN}/**", store.serve)
    pg.goto(f"{ORIGIN}/")
    pg.wait_for_function("window.DesktopLayout && window.Taskbar")
    pg.evaluate("DesktopLayout.ready")
    return pg, errors


def _dock(pg):
    return pg.evaluate("Array.from(document.querySelectorAll('#taskbar-center .dock-item'))"
                       ".map((b) => b.dataset.appId)")


def _settle(pg, store, n):
    """Wait for the n-th save (the shell debounces its saves by 400 ms)."""
    for _ in range(100):
        if len(store.puts) >= n:
            return
        pg.wait_for_timeout(50)


def test_an_arranged_dock_gets_calendar_after_circles_once(browser):
    store = Store({"dock_pinned": ["workbench", "circles", "settings"]})
    pg, errors = _desktop(browser, store)
    _settle(pg, store, 1)
    assert store.puts == [{"dock_pinned": ["workbench", "circles", "calendar", "settings"],
                           "dock_offered": ["calendar"]}], store.puts
    pg.wait_for_function("document.querySelector('.dock-item[data-app-id=\"calendar\"]')")
    assert _dock(pg) == ["workbench", "circles", "calendar", "settings"]
    # The next start finds it offered and changes nothing.
    pg2, errors2 = _desktop(browser, store)
    pg2.wait_for_timeout(800)
    assert len(store.puts) == 1, store.puts
    assert _dock(pg2) == ["workbench", "circles", "calendar", "settings"]
    assert errors == [] and errors2 == []


def test_an_arranged_dock_without_circles_gets_it_at_the_end(browser):
    store = Store({"dock_pinned": ["workbench", "settings"]})
    pg, errors = _desktop(browser, store)
    _settle(pg, store, 1)
    assert store.puts[0]["dock_pinned"] == ["workbench", "settings", "calendar"], store.puts
    assert errors == []


def test_taken_out_after_the_offer_stays_out(browser):
    store = Store({"dock_pinned": ["workbench", "circles"], "dock_offered": ["calendar"]})
    pg, errors = _desktop(browser, store)
    pg.wait_for_timeout(800)
    assert store.puts == [], store.puts
    assert _dock(pg) == ["workbench", "circles"]
    assert errors == []


def test_a_dock_nobody_arranged_shows_the_defaults_and_saves_nothing(browser):
    store = Store(None)
    pg, errors = _desktop(browser, store)
    pg.wait_for_timeout(800)
    assert store.puts == [], store.puts
    assert _dock(pg)[:3] == ["circles", "calendar", "workbench"], _dock(pg)
    assert errors == []


def test_taking_it_out_after_the_offer_is_saved_and_kept(browser):
    store = Store({"dock_pinned": ["circles", "settings"]})
    pg, errors = _desktop(browser, store)
    _settle(pg, store, 1)
    pg.evaluate("DesktopLayout.unpin('calendar')")
    _settle(pg, store, 2)
    assert store.puts[1] == {"dock_pinned": ["circles", "settings"], "dock_offered": ["calendar"]}, store.puts
    pg2, errors2 = _desktop(browser, store)
    pg2.wait_for_timeout(800)
    assert len(store.puts) == 2 and _dock(pg2) == ["circles", "settings"], (store.puts, _dock(pg2))
    assert errors == [] and errors2 == []


def test_arranging_the_default_dock_records_the_offer(browser):
    store = Store(None)
    pg, errors = _desktop(browser, store)
    pg.evaluate("DesktopLayout.unpin('artifacts')")
    _settle(pg, store, 1)
    assert store.puts[0]["dock_offered"] == ["calendar"] and "calendar" in store.puts[0]["dock_pinned"], store.puts
    pg.evaluate("DesktopLayout.unpin('calendar')")
    _settle(pg, store, 2)
    pg2, errors2 = _desktop(browser, store)
    pg2.wait_for_timeout(800)
    assert "calendar" not in _dock(pg2) and len(store.puts) == 2, (_dock(pg2), store.puts)
    # Reset puts the defaults back, the offer still recorded.
    pg2.evaluate("DesktopLayout.resetLayout()")
    _settle(pg2, store, 3)
    assert store.puts[2]["dock_pinned"][:2] == ["circles", "calendar"] \
        and store.puts[2]["dock_offered"] == ["calendar"], store.puts[2]
    assert errors == [] and errors2 == []


def test_every_offered_app_is_also_a_default(browser):
    pg, errors = _desktop(browser, Store(None))
    offered, defaults = pg.evaluate("[DesktopLayout.OFFER_ONCE, DesktopLayout.DEFAULT_PINNED]")
    assert offered and set(offered) <= set(defaults), (offered, defaults)
    assert errors == []


def test_a_page_that_does_not_know_the_offer_cannot_lose_calendar(browser):
    store = Store({"dock_pinned": ["circles", "calendar", "settings"], "dock_offered": ["calendar"]})
    # A desktop page loaded before the update, still open, rearranges the dock
    # from the list it had then, which has no Calendar.
    store.save({"dock_pinned": ["settings", "circles"]})
    assert store.layout.get("dock_offered") == [], store.layout
    pg, errors = _desktop(browser, store)
    _settle(pg, store, 1)
    assert store.puts == [{"dock_pinned": ["settings", "circles", "calendar"], "dock_offered": ["calendar"]}], \
        store.puts
    assert errors == []


def test_the_server_merges_a_save_without_the_offer_as_a_page_that_never_saw_it():
    stored = {"dock_pinned": ["circles", "calendar"], "dock_offered": ["calendar"], "desktop_order": ["a"]}
    # A dock without Calendar, from a page that does not know the offer.
    assert _merge_layout(stored, {"dock_pinned": ["circles"]}) == \
        {"dock_pinned": ["circles"], "dock_offered": [], "desktop_order": ["a"]}
    # The same page keeping Calendar keeps the offer.
    assert _merge_layout(stored, {"dock_pinned": ["calendar", "circles"]})["dock_offered"] == ["calendar"]
    # The current desktop says what was offered, and is taken at its word.
    assert _merge_layout(stored, {"dock_pinned": ["circles"], "dock_offered": ["calendar"]})["dock_offered"] == \
        ["calendar"]
    # A save with no dock in it (the walkthrough's) leaves the offer alone.
    assert _merge_layout(stored, {"walkthrough": {"done": True}})["dock_offered"] == ["calendar"]
    assert _merge_layout(None, {"dock_pinned": ["circles"]}) == {"dock_pinned": ["circles"]}
    assert stored["dock_offered"] == ["calendar"], "the stored layout is not changed in place"


def test_the_server_keeps_dock_offered_like_the_other_id_lists():
    from fastapi import HTTPException
    assert _clean_layout({"dock_offered": ["calendar", " ships "], "nonsense": 1}) == \
        {"dock_offered": ["calendar", "ships"]}
    with pytest.raises(HTTPException):
        _clean_layout({"dock_offered": "calendar"})
    with pytest.raises(HTTPException):
        _clean_layout({"dock_offered": [""]})
