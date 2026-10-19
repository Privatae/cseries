"""The Calendar app's dock icon and its count, driven in a real browser against
the REAL shell modules.

The calendar left Circles for an app of its own, and the count of what is still
scheduled today left the Circles bubble with it. This loads, unmodified,
desktop-layout.js, icons.js, taskbar.js, shell-token.js and floating-apps.js
into a page that stubs only what the rest of the shell would provide
(WindowManager, AppRunner), answers the shell's token mint and the two reads
the badges poll, and pins:

  - a dock nobody has changed holds Calendar, right after Circles, drawn with
    its own glyph;
  - the count on Calendar's icon is what the calendar has scheduled from now to
    the person's own midnight: the read says exactly that (scheduled, since now,
    until the end of the local day, soonest first, a page of 500), carries the
    shell's own token (minted again after a 401), and the icon says what it
    counts; a full page reads "more than";
  - the count survives the dock being drawn again, goes at 0, and caps at 99+;
  - the Circles bubble counts unread inbox messages from the route's own
    `unread`, and no calendar events any more.

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_calendar_dock_badge_js.py
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the shell JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS = _ROOT / "dashboard" / "base" / "js"
_CSS = _ROOT / "dashboard" / "base" / "css"
ORIGIN = "http://shell.test"
TZ = "America/New_York"
APPS = [{"id": "circles", "name": "Circles", "system": True},
        {"id": "calendar", "name": "Calendar", "system": True},
        {"id": "workbench", "name": "Workbench", "system": True},
        {"id": "settings", "name": "Settings", "system": True}]
TOKEN = "shell-token-1"
HANG_MS = 30_000

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style></head><body>
<div id="desktop"><div id="window-layer"></div></div>
<div id="floating-dock"></div>
<div id="taskbar"><div id="taskbar-left"></div><div id="taskbar-center"></div>
  <div id="taskbar-right"><button id="btn-show-desktop"></button></div></div>
<script>
  window.WindowManager = { getAll: () => [], focus() {}, minimize() {}, restore() {}, close() {} };
  window.AppRunner = { launch() {} };
</script>
%(scripts)s
</body></html>"""

_MODULES = ["desktop-layout.js", "icons.js", "taskbar.js", "shell-token.js", "floating-apps.js"]


class Kernel:
    """The routes the badges read, as the kernel answers them."""

    def __init__(self, events=None, next_offset=None, unread=3):
        self.events = events if events is not None else []
        self.next_offset = next_offset
        self.unread = unread
        self.reads: list[dict] = []
        self.mints = 0

    def serve(self, route):
        req = route.request
        url = req.url
        if url.rstrip("/") == ORIGIN:
            body = _PAGE % {
                "css": (_CSS / "tokens.css").read_text() + "\n" + (_CSS / "desktop.css").read_text(),
                "scripts": "\n".join(f'<script src="/js/{m}"></script>' for m in _MODULES),
            }
            return route.fulfill(status=200, content_type="text/html", body=body)
        parts = urlsplit(url)
        path = parts.path
        if path.startswith("/js/"):
            return route.fulfill(status=200, content_type="application/javascript",
                                 body=(_JS / path[len("/js/"):]).read_text())
        if path == "/api/applications":
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"applications": APPS}))
        if path == "/api/appearance":
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if path == "/api/applications/shell-token":
            self.mints += 1
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"token": TOKEN}))
        gated = path in ("/api/calendar/list", "/api/inbox/unread-count")
        if gated and req.headers.get("x-application-token") != TOKEN:
            return route.fulfill(status=401, content_type="application/json",
                                 body=json.dumps({"detail": "application token required"}))
        if path == "/api/calendar/list":
            self.reads.append({k: v[0] for k, v in parse_qs(parts.query).items()})
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "events": self.events, "count": len(self.events), "next_offset": self.next_offset}))
        if path == "/api/inbox/unread-count":
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"ok": True, "entity_id": 7, "unread": self.unread}))
        return route.fulfill(status=404, body="")


@pytest.fixture()
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _shell(browser, kernel):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, timezone_id=TZ, locale="en-US")
    ctx.set_default_timeout(HANG_MS)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route(f"{ORIGIN}/**", kernel.serve)
    pg.goto(f"{ORIGIN}/")
    pg.wait_for_function("window.Taskbar && window.FloatingApps && document.querySelector('.dock-item')")
    return pg, errors


def _events(n):
    at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    return [{"event_id": f"e{i}", "event_type": "scheduled_task", "event_at": at, "status": "scheduled",
             "summary": f"task {i}"} for i in range(n)]


def test_a_dock_nobody_changed_holds_calendar_right_after_circles(browser):
    pg, errors = _shell(browser, Kernel())
    order = pg.evaluate("Array.from(document.querySelectorAll('#taskbar-center .dock-item'))"
                        ".map((b) => b.dataset.appId)")
    assert order[:3] == ["circles", "calendar", "workbench"], order
    # Its own glyph, not a letter tile: the page, its rings and the lit day.
    glyph = pg.evaluate("document.querySelector('.dock-item[data-app-id=\"calendar\"] svg').innerHTML")
    assert "<rect" in glyph and "M3 9.5h18" in glyph, glyph
    assert errors == []


def test_the_calendar_icon_counts_what_is_left_today(browser):
    kernel = Kernel(events=_events(2))
    pg, errors = _shell(browser, kernel)
    pg.wait_for_function("document.querySelector('.dock-item[data-app-id=\"calendar\"] .dock-item-badge')")
    badge = pg.locator('.dock-item[data-app-id="calendar"] .dock-item-badge')
    assert badge.text_content() == "2"
    assert pg.locator('.dock-item[data-app-id="calendar"]').get_attribute("title") == \
        "Calendar · 2 events left today"
    # The read: scheduled, from now to the person's own midnight, soonest first.
    q = kernel.reads[-1]
    now = datetime.now(timezone.utc)
    since = datetime.fromisoformat(q["since"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(q["until"].replace("Z", "+00:00"))
    local_now = since.astimezone(ZoneInfo(TZ))
    midnight = datetime.combine(local_now.date() + timedelta(days=1), datetime.min.time(), ZoneInfo(TZ))
    assert q["status"] == "scheduled" and q["order"] == "asc" and q["limit"] == "500", q
    assert abs((since - now).total_seconds()) < 120, (since, now)
    assert until == (midnight - timedelta(milliseconds=1)).astimezone(timezone.utc), (until, midnight)
    # The first poll may beat the token: a 401 mints again rather than badging 0.
    assert kernel.mints >= 1
    assert errors == []


def test_the_count_survives_a_redraw_goes_at_zero_and_caps(browser):
    pg, errors = _shell(browser, Kernel(events=_events(1)))
    sel = '.dock-item[data-app-id="calendar"] .dock-item-badge'
    pg.wait_for_function(f"document.querySelector('{sel}')")
    assert pg.locator('.dock-item[data-app-id="calendar"]').get_attribute("title") == \
        "Calendar · 1 event left today"
    pg.evaluate("document.dispatchEvent(new CustomEvent('cseries:layout-changed'))")
    assert pg.locator(sel).text_content() == "1"
    pg.evaluate("Taskbar.setBadge('calendar', 150, 'more than 150 events left today')")
    assert pg.locator(sel).text_content() == "99+"
    pg.evaluate("Taskbar.setBadge('calendar', 0)")
    assert pg.locator(sel).count() == 0
    assert pg.locator('.dock-item[data-app-id="calendar"]').get_attribute("title") == "Calendar"
    assert errors == []


def test_a_full_page_reads_more_than(browser):
    pg, errors = _shell(browser, Kernel(events=_events(3), next_offset=3))
    pg.wait_for_function("document.querySelector('.dock-item[data-app-id=\"calendar\"] .dock-item-badge')")
    assert pg.locator('.dock-item[data-app-id="calendar"]').get_attribute("title") == \
        "Calendar · more than 3 events left today"
    assert errors == []


def test_the_circles_bubble_counts_unread_messages_only(browser):
    pg, errors = _shell(browser, Kernel(events=_events(4), unread=3))
    pg.wait_for_function("document.querySelector('.floating-bubble[data-app-id=\"circles\"] .floating-bubble-badge')")
    assert pg.locator('.floating-bubble[data-app-id="circles"] .floating-bubble-badge').text_content() == "3"
    assert errors == []
