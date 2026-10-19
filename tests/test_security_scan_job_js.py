"""A scan that outlasts the router answers 202 with a job — the REAL Security
window (system-apps/security) in Chromium against a stubbed kernel.

The router in front of the Host gives a request 30 s
(server/applications/long_request.py); a scan is ~20 system commands and a
model call that can write up to 8192 tokens, so POST /api/security/scan
answers 202 + a job when it is not done in time (server/routes/security.py).
This pins that the window follows the job to the scan's result, shows a
refusal as a failed scan, and still takes a scan answered in time.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_security_scan_job_js.py
"""
from __future__ import annotations

import json
import pathlib

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the app JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "security"
RESULT = {"overall_status": "secure", "summary": "Nothing needs attention.", "model": "the-model",
          "findings": [], "system_profile": {}}
PENDING = {"pending": True, "job": "scan-1", "poll": "/api/security/jobs/scan-1",
           "message": "The scan is still running — the result will appear here when it is done."}


def _json(route, status, obj):
    return route.fulfill(status=status, content_type="application/json", body=json.dumps(obj))


def _serve(route, scene):
    req = route.request
    path = req.url[len(ORIGIN):].split("?")[0]
    if path in ("/system-apps/security/", "/system-apps/security/index.html"):
        return route.fulfill(status=200, content_type="text/html", body=(APP / "index.html").read_text())
    if path.startswith("/system-apps/security/"):
        f = APP / path[len("/system-apps/security/"):]
        ctype = {".css": "text/css", ".js": "application/javascript"}.get(f.suffix, "application/octet-stream")
        return (route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path.startswith("/css/"):
        f = _ROOT / "dashboard/base" / path.lstrip("/")
        return (route.fulfill(status=200, content_type="text/css", body=f.read_text())
                if f.is_file() else route.fulfill(status=404, body=""))
    if path == "/api/security/scan" and req.method == "POST":
        return _json(route, *scene["scan"])
    if path.startswith("/api/security/jobs/"):
        scene["polls"] = scene.get("polls", 0) + 1
        answers = scene["job"]
        return _json(route, *answers[min(scene["polls"], len(answers)) - 1])
    if path == "/api/security/status":
        return _json(route, 200, {"status": "no_scan"})
    if path.startswith("/api/"):
        return _json(route, 200, [] if "incidents" in path or "signals" in path else {})
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


def _scan(browser, scene) -> dict:
    pg = browser.new_page(viewport={"width": 1300, "height": 900})
    pg.route(f"{ORIGIN}/**", lambda route: _serve(route, scene))
    pg.goto(f"{ORIGIN}/system-apps/security/index.html")
    pg.wait_for_selector("#btn-scan", state="attached", timeout=15000)
    pg.eval_on_selector("#btn-scan", "el => el.click()")
    pg.wait_for_function(
        "(() => { const b = document.getElementById('btn-scan');"
        " return b && b.textContent === 'Run Scan' && !b.disabled"
        " && document.getElementById('status-headline').textContent !== 'No scan yet'; })()",
        timeout=20000)
    out = pg.evaluate("({headline: document.getElementById('status-headline').textContent,"
                      " detail: document.getElementById('status-detail').textContent,"
                      " label: document.getElementById('progress-label').textContent})")
    out["polls"] = scene.get("polls", 0)
    pg.close()
    return out


def test_a_scan_past_the_router_window_is_followed_to_its_result(browser):
    out = _scan(browser, {"scan": (202, PENDING),
                          "job": [(200, {"status": "running"}), (502, {"detail": "bad upstream"}),
                                  (200, {"status": "done", "result": RESULT})]})
    assert out["headline"] == "All clear" and out["detail"] == "Nothing needs attention."
    assert out["label"] == "Scan complete (analyzed by the-model)" and out["polls"] == 3


def test_a_scan_refused_after_the_202_reads_as_a_failed_scan(browser):
    out = _scan(browser, {"scan": (202, PENDING),
                          "job": [(200, {"status": "failed", "http_status": 503,
                                         "detail": "no research route"})]})
    assert out["headline"] == "Scan could not complete"


def test_a_scan_answered_in_time_needs_no_job(browser):
    out = _scan(browser, {"scan": (200, RESULT), "job": [(500, {})]})
    assert out["headline"] == "All clear" and out["polls"] == 0
