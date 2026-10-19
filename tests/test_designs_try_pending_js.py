"""The Designs app follows a pending desktop door to its answer — the REAL app
(system-apps/designs) in Chromium against a stubbed kernel.

Try and Publish load the design in a real browser before anything changes
(server/applications/desktop_render.py). When that outlasts the router's 30 s
the door answers 202 with a job; the app must follow it to the same answer
the request would have given. This pins:
  - a pending Try that ends well reads "wearing …" and offers Keep / Go back;
  - a pending Try that ends refused shows the refusal — the error and its
    line — in the app's status line, and nothing reads as worn.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_designs_try_pending_js.py
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
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "designs"
LIST = {"designs": [{"id": 9, "name": "Samhain", "shelf": "house", "author_name": "Sage"}],
        "local": [], "wearing": {"wallpaper": "default", "artifact_id": None, "entry": None},
        "host": "Sage", "publishes_to": "account", "library_error": None}
REFUSAL = DR.format_render({
    "checked": True, "ok": False,
    "failures": ["an uncaught error at line 196, column 48: `null.boom()` — "
                 "TypeError: Cannot read properties of null (reading 'boom')"]})


def _json(route, status, obj):
    return route.fulfill(status=status, content_type="application/json", body=json.dumps(obj))


def _serve(route, scene):
    req = route.request
    path = req.url[len(ORIGIN):].split("?")[0]
    if path in ("/", "/system-apps/designs/", "/system-apps/designs/index.html"):
        return route.fulfill(status=200, content_type="text/html",
                             body=(APP / "index.html").read_text())
    if path.startswith("/system-apps/designs/"):
        f = APP / path[len("/system-apps/designs/"):]
        ctype = {".css": "text/css", ".js": "application/javascript"}.get(
            f.suffix, "application/octet-stream")
        return (route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path.startswith("/js/") or path.startswith("/css/"):
        f = _ROOT / "dashboard/base" / path.lstrip("/")
        ctype = "application/javascript" if path.startswith("/js/") else "text/css"
        return (route.fulfill(status=200, content_type=ctype, body=f.read_text())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path == "/api/designs":
        return _json(route, 200, LIST)
    if path == "/api/applications":
        return _json(route, 200, {"applications": [{"id": "circles", "name": "Circles"}]})
    if path == "/api/designs/9/document":
        return _json(route, 200, {"design_id": 9, "name": "Samhain", "html": TEMPLATE_HTML})
    if path == "/api/designs/9/install" and req.method == "POST":
        return _json(route, 202, DR.pending_body("job-9", "worn"))
    if path.startswith("/api/appearance/desktop/jobs/"):
        scene["polls"] = scene.get("polls", 0) + 1
        answers = scene["job"]
        return _json(route, 200, answers[min(scene["polls"], len(answers)) - 1])
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


def _open(browser, scene):
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.route(f"{ORIGIN}/**", lambda route: _serve(route, scene))
    pg.goto(f"{ORIGIN}/system-apps/designs/index.html")
    pg.wait_for_selector('.card[data-key="d9"] [data-act="try"]')
    return pg


def test_a_pending_try_that_passes_reads_as_worn(browser):
    pg = _open(browser, {"job": [{"status": "running"},
                                 {"status": "done", "result": {
                                     "installed": True, "artifact_id": 77, "name": "Samhain",
                                     "previous": LIST["wearing"], "discarded": False}}]})
    pg.click('.card[data-key="d9"] [data-act="try"]')
    pg.wait_for_function(
        "document.getElementById('footer-status').textContent.includes('wearing')",
        timeout=15000)
    assert pg.locator("#trying").is_visible()
    pg.close()


def test_a_pending_try_that_is_refused_shows_why(browser):
    pg = _open(browser, {"job": [{"status": "running"},
                                 {"status": "failed", "http_status": 422, "detail": REFUSAL}]})
    pg.click('.card[data-key="d9"] [data-act="try"]')
    pg.wait_for_function(
        "document.getElementById('footer-status').textContent.includes('line 196')",
        timeout=15000)
    # textContent, not inner_text: the footer is styled upper-case.
    status = pg.evaluate("document.getElementById('footer-status').textContent")
    assert "does not render in a browser" in status and "null.boom()" in status
    assert not pg.locator("#trying").is_visible()
    pg.close()
