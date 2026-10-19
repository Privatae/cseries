"""The Artifacts window shows a render refusal to the PERSON — the REAL app
(system-apps/artifacts) in Chromium against a stubbed kernel.

"Use as desktop" now waits on the browser render check
(server/applications/desktop_render.py). The window must put its refusal —
the error and the line — on screen where the person clicked, not only hand
it to the Host; and when the check outlasts the router's 30 s window the door
answers 202 with a job, which the window must follow to the same answer.

This pins:
  - a direct 422 render refusal is shown in the window's report box;
  - a 202 pending answer is polled until its job ends, and a refusal that
    arrives that way is shown the same way;
  - a pending job that ends in success reads "this is your desktop now";
  - a write to the worn desktop that did NOT reach it (the check failed)
    is reported in the window.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_artifacts_use_as_desktop_js.py
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
APP = _ROOT / "system-apps" / "artifacts"
LIST = {"artifacts": [{"artifact_id": 2, "name": "Slate Mist", "description": "desktop",
                       "updated_at": "2026-09-30T09:00:00Z", "desktop": True}]}
# The refusal text the kernel really sends, built by the real formatter.
REFUSAL = DR.format_render({
    "checked": True, "ok": False,
    "failures": ["an uncaught error at line 196, column 48: `null.boom()` — "
                 "TypeError: Cannot read properties of null (reading 'boom')"]})


def _json(route, status, obj):
    return route.fulfill(status=status, content_type="application/json", body=json.dumps(obj))


def _serve(route, scene):
    req = route.request
    path = req.url[len(ORIGIN):].split("?")[0]
    if path in ("/", "/system-apps/artifacts/", "/system-apps/artifacts/index.html"):
        return route.fulfill(status=200, content_type="text/html",
                             body=(APP / "index.html").read_text())
    if path.startswith("/system-apps/artifacts/"):
        f = APP / path[len("/system-apps/artifacts/"):]
        ctype = {".css": "text/css", ".js": "application/javascript"}.get(
            f.suffix, "application/octet-stream")
        return (route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path.startswith("/js/") or path.startswith("/css/"):
        f = _ROOT / "dashboard/base" / path.lstrip("/")
        ctype = "application/javascript" if path.startswith("/js/") else "text/css"
        return route.fulfill(status=200, content_type=ctype, body=f.read_text())
    if path == "/api/artifacts":
        return _json(route, 200, LIST)
    if path == "/api/artifacts/2/render":
        return route.fulfill(status=200, content_type="text/html", body=TEMPLATE_HTML)
    if path == "/api/artifacts/2/files":
        return _json(route, 200, {"files": ["index.html"]})
    if path == "/api/appearance/desktop/check":
        return _json(route, 200, {"ok": True, "mode": "desktop", "errors": [],
                                  "warnings": [], "tokens": 11, "report": ""})
    if path == "/api/appearance/desktop" and req.method == "PUT":
        return _json(route, *scene["put"])
    if path == "/api/artifacts/2/bridge" and req.method == "POST":
        return _json(route, 200, scene["bridge"])
    if path.startswith("/api/appearance/desktop/jobs/"):
        answers = scene["job"]
        scene["polls"] = scene.get("polls", 0) + 1
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


@pytest.fixture()
def run(browser):
    pages = []

    def _go(scene):
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pages.append(pg)
        pg.route(f"{ORIGIN}/**", lambda route: _serve(route, scene))
        pg.goto(f"{ORIGIN}/system-apps/artifacts/index.html")
        pg.wait_for_function("document.querySelectorAll('.artifact-card').length === 1")
        pg.click('.artifact-card[data-id="2"]')
        pg.wait_for_function("!document.getElementById('btn-desktop').disabled")
        return pg
    yield _go
    for pg in pages:
        pg.close()


def _report(pg) -> str:
    pg.wait_for_function(
        "(() => { const b = document.getElementById('desktop-report');"
        " return b && !b.hidden && b.textContent.length > 0; })()", timeout=15000)
    return pg.locator("#desktop-report").inner_text()


def test_a_render_refusal_is_shown_to_the_person(run):
    pg = run({"put": (422, {"detail": REFUSAL})})
    pg.click("#btn-desktop")
    text = _report(pg)
    assert "does not render in a browser" in text and "line 196" in text
    assert pg.locator("#desktop-report").is_visible()
    assert "refused" in pg.locator("#viewer-status").inner_text().lower()


def test_a_pending_check_is_followed_to_the_same_refusal(run):
    pending = DR.pending_body("job-1", "worn")
    pg = run({"put": (202, pending),
              "job": [{"status": "running"},
                      {"status": "failed", "http_status": 422, "detail": REFUSAL}]})
    pg.click("#btn-desktop")
    text = _report(pg)
    assert "line 196" in text and "null.boom()" in text


def test_a_pending_check_that_passes_reads_as_set(run):
    pending = DR.pending_body("job-2", "worn")
    pg = run({"put": (202, pending),
              "job": [{"status": "running"},
                      {"status": "done", "result": {"mode": "desktop", "tokens": 11,
                                                    "warnings": []}}]})
    pg.click("#btn-desktop")
    pg.wait_for_function(
        "document.getElementById('viewer-status').textContent.includes('your desktop now')",
        timeout=15000)


def test_an_edit_that_did_not_reach_the_worn_desktop_is_reported(run):
    """Restore is one of the writes to the worn desktop: its reply carries the
    same `desktop` block every write path returns (bridge edit, Use this one,
    Restore), and the window puts a refusal from it on screen."""
    msg = ("This change is NOT on the desktop: the desktop stays at the last version "
           "that rendered.\n" + REFUSAL)
    pg = run({"put": (200, {})})
    pg.route(f"{ORIGIN}/api/artifacts/2/history",
             lambda route: _json(route, 200, {"history": [
                 {"rev": "0001", "ts": "2026-09-30T00:00:00Z", "actor": "host",
                  "op": "write", "note": "", "files": 1}]}))
    pg.route(f"{ORIGIN}/api/artifacts/2/history/0001/render**",
             lambda route: route.fulfill(status=200, content_type="text/html",
                                         body=TEMPLATE_HTML))
    pg.route(f"{ORIGIN}/api/artifacts/2/revert",
             lambda route: _json(route, 200, {"artifact_id": 2, "restored": "0001",
                                              "desktop": {"worn": True, "advanced": False,
                                                          "message": msg}}))
    pg.click("#btn-history")
    pg.wait_for_selector('[data-rev="0001"]', timeout=10000)
    pg.click('[data-rev="0001"]')
    pg.wait_for_selector("#btn-restore", timeout=10000)
    pg.click("#btn-restore")
    text = _report(pg)
    assert "NOT on the desktop" in text and "line 196" in text
