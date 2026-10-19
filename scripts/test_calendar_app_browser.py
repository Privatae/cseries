#!/usr/bin/env python3
"""Gate: the Calendar app in a real browser, over the real calendar routes and a
real kernel schema.

No mock of the API: server/routes/calendar.py is mounted as the kernel mounts
it, its handlers read and write calendar_events and calendar_event_runs on a
fresh clone of the schema a Host builds at boot, and the app's window token is
the one the kernel would mint for it (read:calendar and write:calendar). The
page is the app's own index.html, served with the desktop's stylesheets, in
headless Chromium, in New York's time zone so a day on the grid is a day on the
person's own clock. Every scenario at 1440 x 900, the phone ones at 390 x 844
with touch:

  M. the month: six weeks, Sunday first, today marked; each event on its own
     day with its time and title; a day with five shows three and "+2 more";
     ‹ and › read the weeks on screen soonest first (the grid's first and last
     moments, local midnights, in the read), and Today comes back.
  U. Coming up, the list it opens on: only what is scheduled from now on,
     soonest first, each of the Host's tasks labelled as the Host's with how it
     repeats.
  F. the list's other choices: Everything newest first, 50 at a time with
     Show more; the Host's tasks only (the month narrows with it); one kind;
     Today from the person's own midnight to the next.
  D. a day picked on the grid lists that day's events, under its name; All
     days, or the same day again, goes back.
  T. a task in full: when it runs next, how it repeats, who set it, what it
     does, and its runs, 20 at a time, a failed run with its error; no Mark
     done on a task.
  X. cancelling a task: the dialog says it will not run again; Keep it sends
     nothing; Cancel task sends the reason, and the page then says cancelled
     and why, with no buttons, and Coming up no longer lists it. Mark done on
     the person's own event.
  N. + Event: no title, no save; one Save (pressed twice) makes one event, at
     the time the person picked on their own clock, as theirs; it opens on its
     own day.
  H. a request held for the person's next message reads as held, with no
     buttons; titles and a task's words are text, never markup.
  P. the phone: dots instead of chips, nothing wider than the screen, the list
     under the month, an event as a screen of its own and back, + Event inside
     the screen with finger-sized fields.
  L. links: view event:<id> opens that event and brings its month; view
     day:<date> picks that day; a cseries:app-view message moves an open window.
  E. a refusal is said in words: a pod not ready yet, an event that is gone.
  C. Show more goes on from the last row shown, so an event that ran or was
     cancelled between pages costs the next page no row.
  S. a slow answer to an older choice never draws over the newer one.
  B. a month with more events than the grid draws says so.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; Playwright with Chromium,
fastapi and uvicorn on a loopback port; screenshots in a mkdtemp, removed on a
pass (SHOTS_DIR keeps them). ONLY=scenario_phone,... runs only those.

  python3 scripts/test_calendar_app_browser.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import asyncpg  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate drives the real calendar routes on a real database.")
    sys.exit(2)

APP_DIR = ROOT / "system-apps" / "calendar"
CSS_DIR = ROOT / "dashboard" / "base" / "css"
E = 1
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="calendar-app-"))
SHOTS.mkdir(parents=True, exist_ok=True)
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the kernel side: the real router, a real pool, the app's own token ───────
STATE: dict = {"calls": [], "delay": None, "pool_off": False, "dsn": None, "pool": None}


def build_app(dsn: str) -> FastAPI:
    from server import brain_service as bs
    from server.applications.tokens import AppToken
    from server.routes import calendar as cal_routes

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        STATE["pool"] = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        bs._pool = STATE["pool"]
        bs._entity_id = E
        yield
        await STATE["pool"].close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def window_token(request: Request, call_next):
        # What the kernel's token middleware leaves on the request for a window
        # the Calendar app was launched in: its declared scopes, nothing more.
        if request.url.path.startswith("/api/"):
            STATE["calls"].append({"method": request.method, "path": request.url.path,
                                   "query": {k: v for k, v in request.query_params.items()}})
            delay = STATE.get("delay")
            # The whole query, not a part of it: the newer read's query
            # holds the older one's words too.
            slow = bool(delay and request.url.query == delay[0])
            if slow:
                await asyncio.sleep(delay[1])
            request.state.app_token = AppToken(app_id="calendar", permissions=["read:calendar", "write:calendar"],
                                               window_id="gate", iat=0)
            bs._pool = None if STATE["pool_off"] else STATE["pool"]
            response = await call_next(request)
            if slow:
                STATE["slow_answered"] = True
            return response
        return await call_next(request)

    app.include_router(cal_routes.router)

    @app.get("/css/{name}")
    def css(name: str):
        target = (CSS_DIR / name).resolve()
        if target.parent != CSS_DIR.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/css")

    @app.get("/system-apps/calendar/{rest:path}")
    def static(rest: str):
        target = (APP_DIR / (rest or "index.html")).resolve()
        if APP_DIR.resolve() not in target.parents:
            return Response(status_code=403)
        return FileResponse(str(target)) if target.is_file() else Response(status_code=404)

    return app


def serve(app) -> int:
    # Loopback on purpose: the only client is the headless Chromium this same
    # process launches on this same box.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


# ── rows, written straight into the schema ───────────────────────────────────
# Playwright's sync API keeps an event loop running in this thread, so the
# gate's own reads and writes run on a loop of their own, in a thread.
_DB_LOOP = asyncio.new_event_loop()
threading.Thread(target=_DB_LOOP.run_forever, daemon=True).start()


def db(stmt: str, *args):
    async def _go():
        if STATE.get("conn") is None:
            STATE["conn"] = await asyncpg.connect(STATE["dsn"])
        return await STATE["conn"].fetch(stmt, *args)
    return asyncio.run_coroutine_threadsafe(_go(), _DB_LOOP).result(timeout=60)


def db_close():
    async def _go():
        if STATE.get("conn") is not None:
            await STATE["conn"].close()
            STATE["conn"] = None
    asyncio.run_coroutine_threadsafe(_go(), _DB_LOOP).result(timeout=30)


def reset():
    # This Host's rows only: the schema is the kernel's own, never emptied whole.
    db("DELETE FROM calendar_event_runs WHERE entity_id = $1", E)
    db("DELETE FROM calendar_events WHERE entity_id = $1", E)
    STATE["calls"].clear()
    STATE["delay"] = None
    STATE["pool_off"] = False


def ny(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, dtime(hh, mm), NY).astimezone(UTC)


def add(summary, at, *, kind="event", status="scheduled", source="user", recurrence=None, payload=None,
        reason=None) -> tuple[str, int]:
    row = db(
        "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, summary, "
        "source, status, recurrence, payload, cancelled_reason, category, created_by) "
        "VALUES ($1, left($2::text, 500), $3::timestamptz, $3::timestamptz, $4::text, $3::timestamptz, $2::text, "
        "        $5::text, $6, $7, $8::jsonb, $9, $4::text, $5::text) RETURNING event_id::text, id",
        E, summary, at, kind, source, status, recurrence, json.dumps(payload or {}), reason)[0]
    return row[0], row[1]


def task(summary, at, **kw):
    kw.setdefault("source", "entity")
    return add(summary, at, kind="scheduled_task", **kw)


def add_runs(pk: int, n: int, newest: datetime, *, failed_at=()):
    for i in range(n):
        ok = i not in failed_at
        db("INSERT INTO calendar_event_runs (event_id, entity_id, ran_at, ok, output_excerpt, error) "
           "VALUES ($1, $2, $3, $4, $5, $6)", pk, E, newest - timedelta(days=i), ok,
           f"Backups fine, run {i}." if ok else None, None if ok else f"disk full on run {i}")


def row(event_id: str):
    return db("SELECT status, cancelled_reason, source, event_type, summary, event_at FROM calendar_events "
              "WHERE event_id = $1::uuid", event_id)[0]


def calls(path_part: str, method: str = "GET"):
    return [c for c in STATE["calls"] if path_part in c["path"] and c["method"] == method]


def when_q(c, key):
    v = c["query"].get(key)
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


# ── the person's clock ───────────────────────────────────────────────────────
def today_ny() -> date:
    return datetime.now(NY).date()


def grid_bounds(y: int, m: int) -> tuple[datetime, datetime]:
    first = date(y, m, 1)
    lead = (first.weekday() + 1) % 7          # Sunday first, as the app draws it
    start = first - timedelta(days=lead)
    return (datetime.combine(start, dtime(0), NY).astimezone(UTC),
            (datetime.combine(start + timedelta(days=42), dtime(0), NY) - timedelta(milliseconds=1)).astimezone(UTC))


def month_label(d: date) -> str:
    return f"{d:%B} {d.year}"


def chip_time(at: datetime) -> str:
    return f"{at.astimezone(NY):%-I:%M %p}"


def long_day(d: date) -> str:
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def same_month_day(offset: int) -> date:
    """A day `offset` days from today that is still in this month."""
    t = today_ny()
    d = t + timedelta(days=offset)
    return d if d.month == t.month else t - timedelta(days=offset)


# ── the page ─────────────────────────────────────────────────────────────────
_OPENS = iter(range(1, 1_000_000))


def open_app(page, base, view: str = ""):
    """A window as the shell opens one: a fresh load (the shell adds a query
    of its own to the frame's address), with any view in the hash."""
    page.goto(f"{base}/system-apps/calendar/index.html?w={next(_OPENS)}" + (f"#view={view}" if view else ""),
              wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator("#cal-grid .cal-cell")).to_have_count(42, timeout=10_000)


def wait_list(page):
    expect(page.locator("#cal-list .cal-note")).to_have_count(0, timeout=10_000)


def rows_text(page):
    return page.locator("#cal-list .cal-row .cal-row-title").all_inner_texts()


def cell(page, d: date):
    return page.locator(f'#cal-grid .cal-cell[data-day="{d.isoformat()}"]')


def fact_value(page, label):
    """The value under a label on the open event (waits for the label: the
    full read of an event lands after its list row has drawn the page)."""
    loc = page.locator(f'#cal-detail-body dt:text-is("{label}") + dd')
    expect(loc).to_have_count(1, timeout=10_000)
    return loc.text_content()


def settled(page):
    """Every animation on the page finished: a dialog scales in from 95 %."""
    page.wait_for_function("document.getAnimations().every((a) => a.playState !== 'running')", timeout=5_000)


def detail_fact(page, label):
    """The value under a label on the open event, '' when it is not there."""
    return page.evaluate("""(label) => {
        const dts = Array.from(document.querySelectorAll('#cal-detail-body dt'));
        const dt = dts.find((n) => n.textContent === label);
        return dt && dt.nextElementSibling ? dt.nextElementSibling.textContent : '';
    }""", label)


# ── scenarios ────────────────────────────────────────────────────────────────
def scenario_month(page, base):
    t = today_ny()
    busy = same_month_day(3)
    add("Dentist", ny(t, 10))
    task("Check the build", ny(t, 15, 30), recurrence="daily", payload={"prompt": "Read the last build."})
    for i in range(5):
        add(f"Busy {i}", ny(busy, 9 + i), kind="app_installed", source="entity", status="completed")
    prev_first = (date(t.year, t.month, 1) - timedelta(days=1)).replace(day=1)
    add("Last month's thing", ny(prev_first + timedelta(days=9), 12))
    open_app(page, base)

    expect(page.locator("#cal-month-label")).to_have_text(month_label(t))
    check("M six weeks of days, Sunday first", page.locator("#cal-grid .cal-cell").count() == 42
          and page.locator(".cal-dow span").first.inner_text() == "Sun")
    today_cells = page.locator("#cal-grid .cal-cell.today")
    check("M today is marked, once", today_cells.count() == 1
          and today_cells.first.get_attribute("data-day") == t.isoformat())
    expect(cell(page, t).locator(".cal-chip")).to_have_count(2, timeout=10_000)
    chips = cell(page, t).locator(".cal-chip").all_inner_texts()
    check("M each event sits on its day with its time and title, soonest first",
          len(chips) == 2 and "Dentist" in chips[0] and chip_time(ny(t, 10)) in chips[0]
          and "Check the build" in chips[1] and chip_time(ny(t, 15, 30)) in chips[1], chips)
    busy_cell = cell(page, busy)
    check("M a day with five shows three and '+2 more'",
          busy_cell.locator(".cal-chip").count() == 3 and busy_cell.locator(".cal-chips .cal-more-n").inner_text() == "+2 more",
          busy_cell.inner_text())
    check("M a task's dot is the Host's colour, the person's event their own",
          cell(page, t).locator(".cal-chip .cal-dot.k-yours").count() == 1
          and cell(page, t).locator(".cal-chip .cal-dot.k-task").count() == 1)
    lo, hi = grid_bounds(t.year, t.month)
    month_reads = [c for c in calls("/api/calendar/list") if c["query"].get("until")]
    first = month_reads[0] if month_reads else {"query": {}}
    check("M the month reads its six weeks soonest first, from the first cell's midnight to the last's end",
          when_q(first, "since") == lo and when_q(first, "until") == hi and first["query"].get("order") == "asc"
          and first["query"].get("limit") == "500", (first, lo, hi))

    STATE["calls"].clear()
    page.locator("#cal-prev").click()
    expect(page.locator("#cal-month-label")).to_have_text(month_label(prev_first))
    expect(cell(page, prev_first + timedelta(days=9)).locator(".cal-chip")).to_have_text(
        [f"{chip_time(ny(prev_first + timedelta(days=9), 12))}Last month's thing"], timeout=10_000)
    lo2, hi2 = grid_bounds(prev_first.year, prev_first.month)
    reads = [c for c in calls("/api/calendar/list") if c["query"].get("until")]
    check("M ‹ reads the month before, its own six weeks", bool(reads) and when_q(reads[-1], "since") == lo2
          and when_q(reads[-1], "until") == hi2, reads[-1:] or None)
    page.locator("#cal-next").click()
    page.locator("#cal-next").click()
    nxt = date(t.year + (t.month == 12), t.month % 12 + 1, 1)
    expect(page.locator("#cal-month-label")).to_have_text(month_label(nxt))
    page.locator("#cal-today").click()
    expect(page.locator("#cal-month-label")).to_have_text(month_label(t))
    expect(cell(page, t).locator(".cal-chip")).to_have_count(2, timeout=10_000)
    check("M Today comes back to this month", True)
    page.screenshot(path=str(SHOTS / "month-desktop.png"))


def scenario_coming_up(page, base):
    t = today_ny()
    now = datetime.now(UTC)
    add("Old scheduled", now - timedelta(days=1))
    add("Cancelled later", now + timedelta(hours=5), status="cancelled", reason="not needed")
    add("Lunch with Sam", now + timedelta(days=1, hours=1))
    task("Daily digest", now + timedelta(hours=2), recurrence="daily", payload={"prompt": "Digest."})
    task("Done already", now + timedelta(hours=3), status="completed")
    open_app(page, base)
    wait_list(page)
    expect(page.locator("#cal-list .cal-row")).to_have_count(2, timeout=10_000)
    check("U Coming up is what is scheduled from now on, soonest first",
          rows_text(page) == ["Daily digest", "Lunch with Sam"], rows_text(page))
    subs = page.locator("#cal-list .cal-row .cal-row-sub").all_inner_texts()
    check("U the Host's task says it is the Host's and how it repeats; the person's event says it is theirs",
          subs[0].startswith("Your Host's task · every day") and subs[1].startswith("Your event"), subs)
    heads = page.locator("#cal-list .cal-day-head").all_inner_texts()
    check("U the list is grouped under each day's name", len(heads) >= 1 and heads[0].upper() in ("TODAY", "TOMORROW"),
          heads)
    up = [c for c in calls("/api/calendar/list") if c["query"].get("status") == "scheduled"]
    q = up[0]["query"] if up else {}
    since = when_q(up[0], "since") if up else None
    check("U ...read as: scheduled, from now, soonest first",
          q.get("order") == "asc" and since is not None and abs((since - now).total_seconds()) < 120, q)
    check("U the filter opens on Coming up", page.locator("#cal-filter").input_value() == "upcoming")
    del t


def scenario_filters(page, base):
    t = today_ny()
    now = datetime.now(UTC)
    for i in range(60):
        add(f"Installed {i:02d}", now - timedelta(days=1, hours=i), kind="app_installed", source="entity",
            status="completed")
    for i in range(3):
        task(f"Task {i}", now + timedelta(days=i + 1), recurrence="weekly")
    add("Mine today", ny(t, 0, 30) if datetime.now(NY).hour >= 1 else ny(t, 23, 30))
    open_app(page, base)
    wait_list(page)

    STATE["calls"].clear()
    page.locator("#cal-filter").select_option("all")
    expect(page.locator("#cal-list .cal-row")).to_have_count(50, timeout=10_000)
    check("F Everything shows 50 at a time, with Show more", page.locator("#cal-more").is_visible())
    first_all = [c for c in calls("/api/calendar/list") if not c["query"].get("until")]
    check("F ...newest first", bool(first_all) and first_all[-1]["query"].get("order") == "desc", first_all[-1:])
    page.locator("#cal-more").click()
    expect(page.locator("#cal-list .cal-row")).to_have_count(64, timeout=10_000)
    more = [c for c in calls("/api/calendar/list") if c["query"].get("order") == "desc" and c["query"].get("until")]
    fiftieth = db("SELECT event_at FROM calendar_events WHERE entity_id = $1 ORDER BY event_at DESC, id DESC "
                  "OFFSET 49 LIMIT 1", E)[0]["event_at"]
    check("F Show more reads on from the last row shown (its moment, past the one row there)",
          len(more) == 1 and when_q(more[0], "until") == fiftieth and more[0]["query"].get("offset") == "1",
          (STATE["calls"][-3:], fiftieth))
    check("F ...and the last page hides Show more", not page.locator("#cal-more").is_visible())
    titles = rows_text(page)
    check("F the pages join newest first", titles[:3] == ["Task 2", "Task 1", "Task 0"]
          and titles[-1] == "Installed 59", titles[:4] + titles[-2:])

    STATE["calls"].clear()
    page.locator("#cal-filter").select_option("tasks")
    expect(page.locator("#cal-list .cal-row")).to_have_count(3, timeout=10_000)
    check("F the Host's tasks only", all(s.startswith("Your Host's task")
          for s in page.locator("#cal-list .cal-row-sub").all_inner_texts()))
    narrowed = [c for c in calls("/api/calendar/list") if c["query"].get("until")
                and c["query"].get("event_type") == "scheduled_task"]
    check("F ...and the month narrows with it", len(narrowed) == 1, STATE["calls"])
    check("F ...the grid draws tasks only",
          page.locator("#cal-grid .cal-dot.k-yours, #cal-grid .cal-dot.k-log").count() == 0)

    page.locator("#cal-filter").select_option("type:app_installed")
    expect(page.locator("#cal-list .cal-row")).to_have_count(50, timeout=10_000)
    check("F one kind", all(s.startswith("App installed")
          for s in page.locator("#cal-list .cal-row-sub").all_inner_texts()))

    STATE["calls"].clear()
    page.locator("#cal-filter").select_option("today")
    expect(page.locator("#cal-list .cal-row")).to_have_count(1, timeout=10_000)
    check("F Today lists today's events", rows_text(page) == ["Mine today"], rows_text(page))
    today_q = [c for c in calls("/api/calendar/list") if c["query"].get("until") and not c["query"].get("event_type")
               and when_q(c, "since") == datetime.combine(t, dtime(0), NY).astimezone(UTC)]
    check("F ...from the person's own midnight to the next, soonest first",
          len(today_q) == 1 and when_q(today_q[0], "until")
          == (datetime.combine(t + timedelta(days=1), dtime(0), NY) - timedelta(milliseconds=1)).astimezone(UTC)
          and today_q[0]["query"].get("order") == "asc", STATE["calls"])


def scenario_day(page, base):
    d = same_month_day(2)
    add("Morning", ny(d, 8))
    task("Noon task", ny(d, 12), recurrence="every:7200")
    add("Evening", ny(d, 20))
    add("Elsewhere", ny(same_month_day(5), 9))
    open_app(page, base)
    expect(cell(page, d).locator(".cal-chip")).to_have_count(3, timeout=10_000)
    cell(page, d).click()
    expect(page.locator("#cal-list .cal-row")).to_have_count(3)
    check("D a day picked lists that day's events in their order", rows_text(page) == ["Morning", "Noon task", "Evening"],
          rows_text(page))
    check("D ...under the day's name", page.locator("#cal-list .cal-day-head").first.inner_text().upper()
          == long_day(d).upper(), page.locator("#cal-list .cal-day-head").all_inner_texts())
    check("D ...a task that repeats every two hours says so", "every 2 hours"
          in page.locator("#cal-list .cal-row-sub").nth(1).inner_text())
    check("D the day is marked on the grid", cell(page, d).get_attribute("aria-pressed") == "true")
    page.locator("#cal-day-clear").click()
    expect(page.locator("#cal-day-clear")).to_be_hidden()
    check("D All days goes back to the list", page.locator("#cal-list .cal-day-head").count() == 0
          or page.locator("#cal-list .cal-day-head").first.inner_text().upper() != long_day(d).upper())
    cell(page, d).click()
    cell(page, d).click()
    check("D the same day again goes back too", cell(page, d).get_attribute("aria-pressed") == "false"
          and page.locator("#cal-day-clear").is_hidden())


def open_row(page, title):
    page.locator("#cal-list .cal-row", has_text=title).first.click()
    expect(page.locator("#cal-detail")).to_be_visible()
    expect(page.locator("#cal-detail-body .cal-title")).to_have_text(title, timeout=10_000)


def scenario_task(page, base):
    t = today_ny()
    eid, pk = task("Nightly backup check", ny(t + timedelta(days=1), 2), recurrence="daily",
                   payload={"prompt": "Check last night's backup and tell me if it failed.",
                            "origin": {"thread_id": str(uuid.uuid4())}})
    add_runs(pk, 25, datetime.now(UTC) - timedelta(hours=3), failed_at=(2,))
    open_app(page, base)
    wait_list(page)
    open_row(page, "Nightly backup check")
    body = page.locator("#cal-detail-body")
    expect(body.locator(".cal-run")).to_have_count(20, timeout=10_000)
    page.screenshot(path=str(SHOTS / "task-desktop-top.png"))
    pills = body.locator(".cal-pills .cseries-pill").all_inner_texts()
    check("T a task in full: the Host's, and scheduled", [p.upper() for p in pills] == ["YOUR HOST'S TASK", "SCHEDULED"],
          pills)
    check("T ...when it runs next", fact_value(page, "Next run").startswith(
        f"{(t + timedelta(days=1)):%A}"), detail_fact(page, "Next run"))
    check("T ...how it repeats and who set it", detail_fact(page, "Repeats") == "every day"
          and detail_fact(page, "Set by") == "Your Host")
    check("T ...what it does, in its own words", detail_fact(page, "What it does")
          == "Check last night's backup and tell me if it failed.")
    check("T ...its runs, 20 of 25, newest first",
          body.locator(".cal-runs > .label-sys").inner_text().upper() == "RUNS · 25"
          and "run 0." in body.locator(".cal-run").first.inner_text())
    failed = body.locator(".cal-run", has_text="disk full on run 2")
    check("T a failed run says so, with its error", failed.count() == 1
          and failed.locator(".cseries-pill").inner_text().upper() == "FAILED")
    first_run = body.locator(".cal-run").first
    check("T a run is one line until opened", not first_run.locator(".cal-run-out").is_visible())
    first_run.locator("summary").click()
    expect(first_run.locator(".cal-run-out")).to_have_text("Backups fine, run 0.")
    check("T ...and opened, shows all of what it said", first_run.locator(".cal-run-out").is_visible())
    page.locator("#cal-runs-more").click()
    expect(body.locator(".cal-run")).to_have_count(25, timeout=10_000)
    runs_reads = calls(f"/api/calendar/events/{eid}/runs")
    check("T Show older runs reads the next page", [c["query"].get("offset") for c in runs_reads] == ["0", "20"]
          and all(c["query"].get("limit") == "20" for c in runs_reads), runs_reads)
    check("T ...and then there is none older", page.locator("#cal-runs-more").count() == 0)
    check("T an opened run stays open as older runs load",
          body.locator(".cal-run").first.locator(".cal-run-out").is_visible())
    # The page reads itself again (as it does every minute, and when the window
    # is shown again): the runs are drawn anew, and the opened one stays open.
    body.locator(".cal-run").first.evaluate("el => { el.dataset.drawnBefore = '1'; }")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("(() => { const r = document.querySelector('#cal-detail-body .cal-run');"
                           " return r && !r.dataset.drawnBefore; })()", timeout=10_000)
    check("T ...and as the page reads itself again",
          body.locator(".cal-run").count() == 25
          and body.locator(".cal-run").first.locator(".cal-run-out").is_visible())
    # text_content, not inner_text: a button's words are drawn in capitals.
    check("T no Mark done on a task; Cancel task instead", page.locator("#cal-done").count() == 0
          and page.locator("#cal-cancel").text_content() == "Cancel task")
    page.screenshot(path=str(SHOTS / "task-desktop.png"))


def scenario_cancel(page, base):
    now = datetime.now(UTC)
    keep_id, _ = task("Water the plants", now + timedelta(hours=6), recurrence="daily")
    cut_id, _ = task("Old newsletter", now + timedelta(hours=4), recurrence="weekly")
    lunch_id, _ = add("Lunch", now + timedelta(hours=8))
    open_app(page, base)
    wait_list(page)

    open_row(page, "Water the plants")
    page.locator("#cal-cancel").click()
    expect(page.locator("#cal-confirm")).to_be_visible()
    check("X the dialog says the task will not run again",
          page.locator("#cal-c-message").inner_text().startswith("Your Host will not run it again, on any day."),
          page.locator("#cal-c-message").inner_text())
    page.locator("#cal-c-cancel").click()
    expect(page.locator("#cal-confirm")).to_be_hidden()
    check("X Keep it sends nothing", not calls("/api/calendar/cancel", "POST") and row(keep_id)["status"] == "scheduled")
    page.locator("#cal-back").click()

    open_row(page, "Old newsletter")
    page.locator("#cal-cancel").click()
    page.locator("#cal-c-input").fill("Not needed any more")
    settled(page)
    page.screenshot(path=str(SHOTS / "cancel-dialog.png"))
    page.locator("#cal-c-ok").click()
    expect(page.locator("#cal-detail-body .cal-pills .cseries-pill").nth(1)).to_have_text("cancelled", timeout=10_000)
    posted = calls("/api/calendar/cancel", "POST")
    check("X Cancel task sends the reason, once", len(posted) == 1 and posted[0]["query"].get("reason")
          == "Not needed any more", posted)
    r = row(cut_id)
    check("X ...the row is cancelled, with the reason", r["status"] == "cancelled"
          and r["cancelled_reason"] == "Not needed any more", dict(r))
    check("X ...the page says why, and offers nothing more", detail_fact(page, "Why cancelled") == "Not needed any more"
          and page.locator("#cal-detail-body .cal-actions").count() == 0)
    page.locator("#cal-back").click()
    expect(page.locator("#cal-list .cal-row", has_text="Old newsletter")).to_have_count(0, timeout=10_000)
    check("X ...and Coming up no longer lists it", "Old newsletter" not in rows_text(page), rows_text(page))

    open_row(page, "Lunch")
    check("X the person's own event offers Mark done", page.locator("#cal-done").is_visible()
          and page.locator("#cal-cancel").text_content() == "Cancel event")
    page.locator("#cal-done").click()
    expect(page.locator("#cal-detail-body .cal-pills .cseries-pill").nth(1)).to_have_text("done", timeout=10_000)
    check("X Mark done marks it done", row(lunch_id)["status"] == "completed")


def scenario_create(page, base):
    t = today_ny()
    open_app(page, base)
    wait_list(page)
    page.locator("#cal-new").click()
    expect(page.locator("#cal-compose")).to_be_visible()
    check("N + Event opens with the title to type", page.evaluate("document.activeElement.id") == "cal-f-title")
    pre = page.locator("#cal-f-when").input_value()
    check("N ...and a time already in (the next whole hour)", pre.endswith(":00") and len(pre) == 16, pre)
    page.locator("#cal-f-save").click()
    expect(page.locator("#cal-f-error")).to_have_text("Give it a title.")
    check("N no title, no save", not calls("/api/calendar", "POST"))
    tomorrow = t + timedelta(days=1)
    page.locator("#cal-f-title").fill("Dinner with Ana")
    check("N the complaint goes once a title is typed", page.locator("#cal-f-error").is_hidden())
    page.locator("#cal-f-when").fill(f"{tomorrow.isoformat()}T19:00")
    settled(page)
    page.screenshot(path=str(SHOTS / "create-dialog.png"))
    page.locator("#cal-f-save").dblclick()
    expect(page.locator("#cal-compose")).to_be_hidden(timeout=10_000)
    expect(page.locator("#cal-detail-body .cal-title")).to_have_text("Dinner with Ana", timeout=10_000)
    made = [c for c in STATE["calls"] if c["path"] == "/api/calendar" and c["method"] == "POST"]
    check("N one Save, pressed twice, makes one event", len(made) == 1, made)
    rows = db("SELECT event_type, source, status, event_at FROM calendar_events WHERE summary = 'Dinner with Ana'")
    check("N ...the person's own, at the time they picked on their own clock",
          len(rows) == 1 and rows[0]["event_type"] == "event" and rows[0]["source"] == "user"
          and rows[0]["status"] == "scheduled" and rows[0]["event_at"] == ny(tomorrow, 19), [dict(r) for r in rows])
    check("N it opens as theirs", page.locator("#cal-detail-body .cal-pills .cseries-pill").first.inner_text().upper()
          == "YOUR EVENT")
    check("N ...on its own day, picked on the grid", cell(page, tomorrow).get_attribute("aria-pressed") == "true"
          or tomorrow.month != t.month)


def scenario_held_and_text(page, base):
    now = datetime.now(UTC)
    held_id, _ = task("Send the invoice", now - timedelta(minutes=5), status="cancelled", reason="held_for_person")
    add('<img src=x onerror="window.__pwned=1">', now + timedelta(hours=1))
    task("Tidy up", now + timedelta(hours=2), payload={"prompt": "<script>window.__pwned2=1</script>"})
    open_app(page, base)
    page.locator("#cal-filter").select_option("all")
    expect(page.locator("#cal-list .cal-row")).to_have_count(3, timeout=10_000)
    held = page.locator("#cal-list .cal-row", has_text="Send the invoice")
    check("H a request held for the person reads as held, not cancelled",
          held.locator(".cseries-pill").inner_text().upper() == "HELD")
    open_row(page, "Send the invoice")
    check("H ...it runs with their next message, and offers no buttons",
          "Runs with your next message." in page.locator("#cal-detail-body .cal-pills").inner_text()
          and page.locator("#cal-detail-body .cal-actions").count() == 0)
    page.locator("#cal-back").click()
    open_row(page, '<img src=x onerror="window.__pwned=1">')
    page.locator("#cal-back").click()
    open_row(page, "Tidy up")
    check("H a title and a task's words are text, never markup: nothing ran",
          page.evaluate("window.__pwned === undefined && window.__pwned2 === undefined"))
    check("H ...no element was made from them",
          page.locator("#cal-detail-body img, #cal-list img, #cal-grid img, #cal-detail-body script").count() == 0)
    check("H ...the task's words show as written", fact_value(page, "What it does")
          == "<script>window.__pwned2=1</script>", detail_fact(page, "What it does"))
    del held_id


def scenario_phone(page, base):
    t = today_ny()
    now = datetime.now(UTC)
    add("Phone call", ny(t, 11))
    add("Run club", ny(same_month_day(1), 7))
    eid, pk = task("Morning brief", now + timedelta(hours=1), recurrence="daily",
                   payload={"prompt": "Brief me."})
    add_runs(pk, 3, now - timedelta(hours=20))
    open_app(page, base)
    wait_list(page)
    expect(cell(page, t).locator(".cal-dots .cal-dot").first).to_be_visible(timeout=10_000)
    on_today = 1 + ((now + timedelta(hours=1)).astimezone(NY).date() == t)
    check("P the grid shows a dot for each event, not chips", cell(page, t).locator(".cal-dots").is_visible()
          and cell(page, t).locator(".cal-dots .cal-dot").count() == on_today
          and not cell(page, t).locator(".cal-chips").is_visible(),
          (cell(page, t).locator(".cal-dots .cal-dot").count(), on_today))
    widths = page.evaluate("""() => ({doc: document.documentElement.scrollWidth,
        body: document.getElementById('cal-body').scrollWidth, view: window.innerWidth})""")
    check("P nothing is wider than the screen", widths["doc"] <= widths["view"] and widths["body"] <= widths["view"],
          widths)
    grid_box = page.locator("#cal-grid").bounding_box()
    side_box = page.locator("#cal-side").bounding_box()
    check("P the list is under the month", side_box["y"] >= grid_box["y"] + grid_box["height"] - 1,
          (grid_box, side_box))
    cell_box = page.locator("#cal-grid .cal-cell").first.bounding_box()
    check("P a day is big enough to tap (44 px)", cell_box["height"] >= 44, cell_box)
    page.locator("#cal-list .cal-row", has_text="Morning brief").first.tap()
    expect(page.locator("#cal-detail-body .cal-title")).to_have_text("Morning brief", timeout=10_000)
    expect(page.locator("#cal-detail-body .cal-run")).to_have_count(3, timeout=10_000)
    check("P an event is a screen of its own (the month steps aside)",
          not page.locator(".cal-month").is_visible() and page.locator("#cal-detail").is_visible())
    page.screenshot(path=str(SHOTS / "task-phone.png"))
    page.locator("#cal-back").tap()
    expect(page.locator(".cal-month")).to_be_visible()
    check("P Back returns to the month and the list", page.locator("#cal-list-view").is_visible())
    page.screenshot(path=str(SHOTS / "month-phone.png"))
    page.locator("#cal-new").tap()
    expect(page.locator("#cal-compose")).to_be_visible()
    settled(page)
    form = page.locator("#cal-compose-form").bounding_box()
    title_h = page.locator("#cal-f-title").bounding_box()["height"]
    check("P + Event fits the screen, with finger-sized fields",
          form["x"] >= 0 and form["x"] + form["width"] <= 390 and title_h >= 44, (form, title_h))
    page.locator("#cal-f-title").fill("Coffee")
    page.locator("#cal-f-save").tap()
    expect(page.locator("#cal-detail-body .cal-title")).to_have_text("Coffee", timeout=10_000)
    check("P a phone saves an event too", len(db("SELECT 1 FROM calendar_events WHERE summary = 'Coffee'")) == 1)
    del eid


def scenario_links(page, base):
    t = today_ny()
    nxt = date(t.year + (t.month == 12), t.month % 12 + 1, 15)
    far_id, _ = task("Quarterly review", ny(nxt, 10), recurrence="every:7776000")
    near_id, _ = add("Near thing", ny(t, 13))
    d = same_month_day(4)
    add("On the linked day", ny(d, 9))
    open_app(page, base, f"event:{far_id}")
    expect(page.locator("#cal-detail-body .cal-title")).to_have_text("Quarterly review", timeout=10_000)
    expect(page.locator("#cal-month-label")).to_have_text(month_label(nxt), timeout=10_000)
    check("L event:<id> opens that event, and brings its month", True)
    check("L ...a 90-day period reads as every 90 days", detail_fact(page, "Repeats") == "every 90 days",
          detail_fact(page, "Repeats"))
    page.evaluate("(id) => window.postMessage({type: 'cseries:app-view', view: 'event:' + id}, '*')", near_id)
    expect(page.locator("#cal-detail-body .cal-title")).to_have_text("Near thing", timeout=10_000)
    check("L a cseries:app-view message moves the open window", True)
    open_app(page, base, f"day:{d.isoformat()}")
    expect(page.locator("#cal-list .cal-day-head").first).to_have_text(long_day(d), ignore_case=True,
                                                                        timeout=10_000)
    expect(page.locator("#cal-list .cal-row")).to_have_count(1, timeout=10_000)
    check("L day:<date> picks that day", cell(page, d).get_attribute("aria-pressed") == "true"
          and rows_text(page) == ["On the linked day"], rows_text(page))


def scenario_errors(page, base):
    add("Anything", datetime.now(UTC) + timedelta(hours=1))
    STATE["pool_off"] = True
    open_app(page, base)
    expect(page.locator("#cal-banner")).to_have_text("The calendar could not be read: pool not ready", timeout=10_000)
    expect(page.locator("#cal-grid-note")).to_have_text("This month could not be read: pool not ready")
    check("E a pod not ready yet is said in words, on the list and the month", True)
    STATE["pool_off"] = False
    open_app(page, base, f"event:{uuid.uuid4()}")
    expect(page.locator("#cal-detail-banner")).to_have_text("This event is not on the calendar any more.",
                                                            timeout=10_000)
    check("E an event that is gone says so", True)


def scenario_more_after_a_change(page, base):
    now = datetime.now(UTC)
    ids = [add(f"Soon {i:02d}", now + timedelta(minutes=10 * i))[0] for i in range(1, 61)]
    open_app(page, base)
    expect(page.locator("#cal-list .cal-row")).to_have_count(50, timeout=10_000)
    # Between the first page and the next, the first event stops being scheduled
    # (it ran, or was cancelled elsewhere): every offset after it moves by one.
    db("UPDATE calendar_events SET status = 'cancelled' WHERE event_id = $1::uuid", ids[0])
    page.locator("#cal-more").click()
    expect(page.locator("#cal-list .cal-row")).to_have_count(60, timeout=10_000)
    titles = rows_text(page)
    check("C Show more after the list moved skips no row and repeats none",
          titles == [f"Soon {i:02d}" for i in range(1, 61)], titles[48:53])


def scenario_stale(page, base):
    now = datetime.now(UTC)
    for i in range(4):
        add(f"Log {i}", now - timedelta(days=i + 1), kind="pr_shipped", source="entity", status="completed")
    task("Only task", now + timedelta(days=1))
    open_app(page, base)
    wait_list(page)
    STATE["delay"] = ("order=desc&limit=50&offset=0", 1.5)
    STATE["slow_answered"] = False
    page.locator("#cal-filter").select_option("all")
    page.locator("#cal-filter").select_option("tasks")
    expect(page.locator("#cal-list .cal-row")).to_have_count(1, timeout=10_000)
    # Waited on the slow answer itself, then a moment for the page to take it.
    deadline = time.time() + 15
    while not STATE["slow_answered"] and time.time() < deadline:
        page.wait_for_timeout(50)
    page.wait_for_timeout(300)
    STATE["delay"] = None
    check("S the older choice's slow answer did arrive", STATE["slow_answered"])
    check("S ...and never drew over the newer one", rows_text(page) == ["Only task"], rows_text(page))


def scenario_bound(page, base):
    t = today_ny()
    first = datetime.combine(date(t.year, t.month, 1), dtime(0), NY).astimezone(UTC)
    db("INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, summary, source, "
       "status) SELECT $1, 'bulk', x.t, x.t, 'inference_run', x.t, 'bulk ' || g, 'entity', 'completed' "
       "FROM generate_series(1, 3100) g, LATERAL (SELECT $2::timestamptz + make_interval(mins => g * 9)) x(t)",
       E, first)
    open_app(page, base)
    expect(page.locator("#cal-grid-note")).to_contain_text("More than 3,000 events in these six weeks",
                                                           timeout=15_000)
    reads = [c for c in calls("/api/calendar/list") if c["query"].get("until")]
    # Each read after the first goes on from the last row the one before it
    # returned: the 500th, 1000th, ... row of the month, at first + n * 9 min.
    ends = [first + timedelta(minutes=9 * 500 * k) for k in range(1, 6)]
    check("B a month with more than the grid draws says so, after six reads of 500, each going on "
          "from the last row the one before it read",
          len(reads) == 6 and reads[0]["query"].get("offset") == "0"
          and [when_q(c, "since") for c in reads[1:]] == ends
          and all(c["query"].get("offset") == "1" for c in reads[1:]),
          [(c["query"].get("since"), c["query"].get("offset")) for c in reads])


PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2}
SCENARIOS = (
    (scenario_month, {}), (scenario_coming_up, {}), (scenario_filters, {}), (scenario_day, {}),
    (scenario_task, {}), (scenario_cancel, {}), (scenario_create, {}), (scenario_held_and_text, {}),
    (scenario_phone, PHONE), (scenario_links, {}), (scenario_errors, {}), (scenario_more_after_a_change, {}),
    (scenario_stale, {}),
    (scenario_bound, {}),
)


def main() -> int:
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no Calendar app at {APP_DIR}")
        return 1
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv, entity_id=E) as dsn:
        STATE["dsn"] = dsn
        port = serve(build_app(dsn))
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for fn, opts in SCENARIOS:
                if ONLY and fn.__name__ not in ONLY:
                    continue
                reset()
                ctx = browser.new_context(**{"viewport": {"width": 1440, "height": 900}, "locale": "en-US",
                                             "timezone_id": "America/New_York", **opts})
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                try:
                    fn(page, base)
                except Exception as e:  # a scenario that throws is a failed gate, not a crash
                    check(f"{fn.__name__}: completed", False, f"{type(e).__name__}: {e}")
                    page.screenshot(path=str(SHOTS / f"{fn.__name__}-error.png"))
                check(f"{fn.__name__}: no uncaught page errors", not errors, "; ".join(errors)[:300])
                ctx.close()
            browser.close()
        # Closed before the database is dropped, which waits on no session.
        db_close()
    print("\nPASS calendar_app_browser" if not fails else f"\nFAIL calendar_app_browser: {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    else:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
