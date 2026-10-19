"""Choosing "Host design" in Settings is a checked wear — the REAL app
(system-apps/settings) in Chromium against a stubbed kernel.

The kernel loads the design's CURRENT version in a real browser before it is
worn (server/applications/desktop_pin.py wear_current). When the check
outlasts the router's 30 s the swatch's PUT answers 202 with a job; when the
newest edit does not render, the desktop shows the last version that did and
says so. This pins that the Settings window:
  - follows a pending job to its answer;
  - shows the "last version that rendered" note instead of a bare "Saved.";
  - shows a refusal (no version ever rendered) as a failure.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_settings_host_design_js.py
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
from server.applications import desktop_render as DR  # noqa: E402

ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "settings"
REFUSAL = DR.format_render({
    "checked": True, "ok": False,
    "failures": ["an uncaught error at line 12, column 3: `draw()` — "
                 "ReferenceError: draw is not defined"]})
STALE_NOTE = "Your desktop shows the last version that rendered — the newest edit does not.\n" + REFUSAL


def _json(route, status, obj):
    return route.fulfill(status=status, content_type="application/json", body=json.dumps(obj))


def _serve(route, scene):
    req = route.request
    path = req.url[len(ORIGIN):].split("?")[0]
    if path in ("/", "/system-apps/settings/", "/system-apps/settings/index.html"):
        return route.fulfill(status=200, content_type="text/html",
                             body=(APP / "index.html").read_text())
    if path.startswith("/system-apps/settings/"):
        f = APP / path[len("/system-apps/settings/"):]
        ctype = {".css": "text/css", ".js": "application/javascript"}.get(
            f.suffix, "application/octet-stream")
        return (route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path.startswith("/js/") or path.startswith("/css/"):
        f = _ROOT / "dashboard/base" / path.lstrip("/")
        ctype = "application/javascript" if path.startswith("/js/") else "text/css"
        return (route.fulfill(status=200, content_type=ctype, body=f.read_text())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path == "/api/appearance" and req.method == "GET":
        return _json(route, 200, {"wallpaper": "default", "has_desktop_html": True,
                                  "available": ["default", "html"]})
    if path == "/api/appearance" and req.method == "PUT":
        return _json(route, *scene["put"])
    if path.startswith("/api/appearance/desktop/jobs/"):
        scene["polls"] = scene.get("polls", 0) + 1
        answers = scene["job"]
        return _json(route, 200, answers[min(scene["polls"], len(answers)) - 1])
    if path.startswith("/api/"):
        return _json(route, 200, {})
    return route.fulfill(status=404, body="")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _choose_host_design(browser, scene) -> str:
    pg = browser.new_page(viewport={"width": 1300, "height": 900})
    pg.route(f"{ORIGIN}/**", lambda route: _serve(route, scene))
    pg.goto(f"{ORIGIN}/system-apps/settings/index.html")
    swatch = '#wallpaper-grid [data-wallpaper="html"]'
    pg.wait_for_selector(swatch, state="attached", timeout=15000)
    pg.eval_on_selector(swatch, "el => el.click()")
    pg.wait_for_function(
        "(() => { const s = document.getElementById('wallpaper-status');"
        " return s && /Saved|last version|Failed/.test(s.textContent); })()", timeout=15000)
    text = pg.evaluate("document.getElementById('wallpaper-status').textContent")
    pg.close()
    return text


def test_a_pending_wear_whose_newest_edit_fails_says_the_last_good_version_shows(browser):
    text = _choose_host_design(browser, {
        "put": (202, DR.pending_body("job-s", "worn")),
        "job": [{"status": "running"},
                {"status": "done", "result": {"wallpaper": "html", "desktop": {
                    "pinned": False, "message": STALE_NOTE}}}]})
    assert "last version that rendered" in text and "line 12" in text


def test_a_wear_that_passes_reads_saved(browser):
    text = _choose_host_design(browser, {
        "put": (200, {"wallpaper": "html", "desktop": {"pinned": True}})})
    assert text.strip() == "Saved."


def test_a_wear_with_no_version_that_ever_rendered_is_a_failure(browser):
    text = _choose_host_design(browser, {
        "put": (202, DR.pending_body("job-r", "worn")),
        "job": [{"status": "failed", "http_status": 422, "detail": REFUSAL}]})
    assert text.startswith("Failed") and "line 12" in text
