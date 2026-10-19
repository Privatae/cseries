"""The Artifacts gallery shows live previews — the REAL app (system-apps/
artifacts) in Chromium against a stubbed kernel.

2026-09-28: Artifacts had no organization and showed blocks of text; it shows
live previews now. This pins:
  - every card on screen carries a live preview frame, sandboxed
    (allow-scripts only) and sealed with the shared CSP;
  - a desktop design's preview gets the inert stand-in bridge, so it draws;
  - search and the All / Artifacts / Desktops filters narrow the grid;
  - the 3 s poll does NOT reload the previews when nothing changed, and an
    artifact that changed is re-rendered.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_artifacts_gallery_js.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the app JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "artifacts"
LIST = {"artifacts": [
    {"artifact_id": 1, "name": "Sales chart", "description": "Q3 numbers",
     "updated_at": "2026-09-28T10:00:00Z", "desktop": False},
    {"artifact_id": 2, "name": "Slate Mist", "description": "Minimal desktop",
     "updated_at": "2026-09-28T09:00:00Z", "desktop": True},
]}
RENDERS: dict[int, str] = {
    1: "<!DOCTYPE html><html><body><h1 id=t>Q3</h1></body></html>",
    2: TEMPLATE_HTML,
}


def _serve(route, counts):
    path = route.request.url[len(ORIGIN):].split("?")[0]
    if path in ("/", "/system-apps/artifacts/", "/system-apps/artifacts/index.html"):
        return route.fulfill(status=200, content_type="text/html",
                             body=(APP / "index.html").read_text())
    if path.startswith("/system-apps/artifacts/"):
        f = APP / path[len("/system-apps/artifacts/"):]
        ctype = {".css": "text/css", ".js": "application/javascript",
                 ".png": "image/png"}.get(f.suffix, "application/octet-stream")
        if not f.is_file():
            return route.fulfill(status=404, body="")
        return route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
    if path.startswith("/js/"):
        return route.fulfill(status=200, content_type="application/javascript",
                             body=(_ROOT / "dashboard/base" / path.lstrip("/")).read_text())
    if path.startswith("/css/"):
        return route.fulfill(status=200, content_type="text/css",
                             body=(_ROOT / "dashboard/base" / path.lstrip("/")).read_text())
    if path == "/api/artifacts":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(LIST))
    if path.startswith("/api/artifacts/") and path.endswith("/render"):
        aid = int(path.split("/")[3])
        counts[aid] = counts.get(aid, 0) + 1
        return route.fulfill(status=200, content_type="text/html", body=RENDERS[aid])
    return route.fulfill(status=404, body="")


@pytest.fixture()
def app():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        counts: dict[int, int] = {}
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route(f"{ORIGIN}/**", lambda route: _serve(route, counts))
        pg.goto(f"{ORIGIN}/system-apps/artifacts/index.html")
        pg.wait_for_function("document.querySelectorAll('.artifact-card').length === 2")
        pg.wait_for_function(
            "[...document.querySelectorAll('.artifact-card .thumb iframe')].every(f => f.srcdoc)")
        pg.counts, pg.errors = counts, errors  # type: ignore[attr-defined]
        yield pg
        b.close()


def test_every_card_is_a_sandboxed_live_preview(app):
    frames = app.locator(".artifact-card .thumb iframe")
    assert frames.count() == 2
    for i in range(2):
        f = frames.nth(i)
        assert f.get_attribute("sandbox") == "allow-scripts"
        assert "Content-Security-Policy" in f.evaluate("el => el.srcdoc")
    # The desktop design got the stand-in bridge — its own script draws tiles.
    desk = app.locator('.artifact-card[data-id="2"] .thumb iframe')
    assert "window.CSeriesDesktop=" in desk.evaluate("el => el.srcdoc")
    assert app.errors == []


def test_search_and_filters_narrow_the_grid(app):
    app.fill("#gallery-search", "slate")
    assert app.locator(".artifact-card").count() == 1
    app.fill("#gallery-search", "")
    app.click('.filter-btn[data-filter="desktop"]')
    assert [app.locator(".artifact-card").nth(i).get_attribute("data-id")
            for i in range(app.locator(".artifact-card").count())] == ["2"]
    app.click('.filter-btn[data-filter="other"]')
    assert app.locator(".artifact-card").count() == 1
    assert app.locator(".artifact-card").first.get_attribute("data-id") == "1"


def test_the_poll_does_not_reload_unchanged_previews(app):
    before = dict(app.counts)
    app.wait_for_timeout(3500)          # one poll
    assert app.counts == before, (before, app.counts)
    # An artifact that changed is re-rendered on the next poll.
    LIST["artifacts"][0]["updated_at"] = "2026-09-28T11:00:00Z"
    try:
        app.wait_for_timeout(3500)
        assert app.counts.get(1, 0) == before.get(1, 0) + 1, app.counts
        assert app.counts.get(2, 0) == before.get(2, 0)
    finally:
        LIST["artifacts"][0]["updated_at"] = "2026-09-28T10:00:00Z"
