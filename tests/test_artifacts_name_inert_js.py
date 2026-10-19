"""A Host-chosen file name or revision note is inert in the Artifacts window —
the REAL app (system-apps/artifacts) in Chromium against a stubbed kernel.

The variant tabs put each root page's file name into data-entry="…", and the
history strip put each revision's note (which carries file names: a promote's
"a.html → index.html", an import's list) into title="…". Both went through an
escape that left quotes alone, and the names are the Host's choice
(_write_files refuses only absolute paths, `..` and .history). So a file
named  x" onclick="…" data-y=".html  gave its tab a click handler that ran in
the person's session, same-origin with the shell (found in review,
2026-10-01). The tabs and the strip are now built node by node.

Pinned here, for names that try both quotes, a click handler, and a focus
handler that fires with no click at all:
  - no element in either bar carries a handler attribute (or autofocus);
  - each name survives exactly as its value (data-entry, title, label);
  - clicking every tab and every revision runs nothing.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_artifacts_name_inert_js.py
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qs, urlsplit

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the app JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "artifacts"

RUN = "window.__pwned=(window.__pwned||0)+1"
NAMES = [
    "index.html",
    f'x" onclick="{RUN}" data-y=".html',
    f'f" autofocus onfocus="{RUN}" data-z=".html',
    f"s' onclick='{RUN}' data-q='.html",
]
NOTE = f'promote a" onclick="{RUN}" onmouseover="{RUN}" data-n=" → index.html'
HISTORY = [
    {"rev": "0002", "ts": "2026-10-01T10:00:00Z", "actor": "host", "op": "promote", "note": NOTE},
    {"rev": "0001", "ts": "2026-10-01T09:00:00Z", "actor": "host", "op": "create", "note": ""},
]
LIST = {"artifacts": [{"artifact_id": 1, "name": "Named by the Host", "description": "",
                       "updated_at": "2026-10-01T10:00:00Z", "desktop": False}]}
PAGE = "<!DOCTYPE html><html><body><p>page</p></body></html>"


def _serve(route, seen):
    url = route.request.url
    path = url[len(ORIGIN):].split("?")[0]
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
    if path.startswith(("/js/", "/css/")):
        f = _ROOT / "dashboard/base" / path.lstrip("/")
        ctype = "application/javascript" if path.startswith("/js/") else "text/css"
        return route.fulfill(status=200, content_type=ctype, body=f.read_text() if f.is_file() else "")
    if path == "/api/artifacts":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(LIST))
    if path == "/api/artifacts/1/files":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"artifact_id": 1, "files": NAMES}))
    if path == "/api/artifacts/1/history":
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"artifact_id": 1, "history": HISTORY}))
    if path.startswith("/api/artifacts/1/") and path.endswith("/render"):
        seen.append(url)
        return route.fulfill(status=200, content_type="text/html", body=PAGE)
    return route.fulfill(status=404, body="")


# Every attribute in a bar whose name would run something, or move focus to it.
_HANDLERS = """sel => [...document.querySelectorAll(sel + ' *'), document.querySelector(sel)]
    .flatMap(el => [...el.attributes].map(a => a.name))
    .filter(n => n.startsWith('on') || n === 'autofocus')"""


@pytest.fixture()
def app():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        seen: list[str] = []
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route(f"{ORIGIN}/**", lambda route: _serve(route, seen))
        pg.goto(f"{ORIGIN}/system-apps/artifacts/index.html")
        pg.wait_for_function("document.querySelectorAll('.artifact-card').length === 1")
        pg.click(".artifact-card")
        pg.wait_for_function(
            "document.getElementById('variant-bar') && !document.getElementById('variant-bar').hidden"
            " && document.getElementById('variant-bar').querySelectorAll('button').length >= 4")
        pg.wait_for_timeout(300)   # let an autofocus/onfocus fire, if any could
        pg.seen, pg.errors = seen, errors  # type: ignore[attr-defined]
        yield pg
        b.close()


def test_variant_tabs_carry_no_handler_and_keep_each_name(app):
    assert app.evaluate(_HANDLERS, "#variant-bar") == []
    tabs = app.locator("#variant-bar .variant-tab")
    assert tabs.count() == len(NAMES)
    got = [tabs.nth(i).evaluate("el => [el.dataset.entry, el.textContent]") for i in range(len(NAMES))]
    assert got == [[n, n[:-len(".html")]] for n in NAMES]
    assert app.evaluate("window.__pwned") is None


def test_clicking_every_tab_runs_nothing_and_opens_that_page(app):
    for i in range(1, len(NAMES)):
        app.locator("#variant-bar .variant-tab").nth(i).click()
        app.wait_for_timeout(150)
        assert app.evaluate(_HANDLERS, "#variant-bar") == []
    assert app.evaluate("window.__pwned") is None
    # Each clicked name reached the render request whole, as its entry value.
    asked = {v for u in app.seen for v in parse_qs(urlsplit(u).query).get("entry", [])}
    for n in NAMES[1:]:
        assert n in asked, (n, asked)
    assert app.errors == []


def test_history_strip_carries_no_handler_and_clicking_runs_nothing(app):
    app.click("#btn-history")
    app.wait_for_function("!document.getElementById('history-bar').hidden"
                          " && document.querySelectorAll('#history-bar .hist-rev').length === 2")
    assert app.evaluate(_HANDLERS, "#history-bar") == []
    first = app.locator("#history-bar .hist-rev").first
    assert first.get_attribute("title") == NOTE
    assert first.get_attribute("data-rev") == "0002"
    first.hover()
    first.click()
    app.wait_for_function("document.querySelectorAll('#history-bar .hist-rev.active').length === 1")
    assert app.evaluate(_HANDLERS, "#history-bar") == []
    assert app.evaluate("window.__pwned") is None
    assert app.errors == []
