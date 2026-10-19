#!/usr/bin/env python3
"""Gate: the Calendar app's two reads, against a real kernel schema.

The calendar became an app of its own, and two of its reads needed what the
routes did not give: what is coming up, soonest first (the list sorted newest
first, so a read from now returned the farthest event first), and a task's runs
a page at a time (an hourly task gathers about 720 runs a month, and the route
returned every one). Both go through the real handlers on the schema a Host
builds at boot:

  O. the list (handle_calendar_list):
     - with no order it is newest first, rows that share a moment in the
       order of the table's id, last made first;
     - order='asc' is soonest first, and 'since now, scheduled, soonest first,
       three' is the next three scheduled events;
     - rows that share a moment (two daily tasks set for 09:00 do) keep one
       order across pages, both ways: paging two at a time returns every row
       once, none twice;
     - an order that is neither is refused (a 400), and the route only takes
       'asc' or 'desc', 'desc' when absent;
     - another Host's rows never appear, and each row says how it repeats;
     - on 20,000 rows the soonest-first page and the default page are read
       through a calendar index, never a sort of the table, touching a
       handful of buffers.
  R. a task's runs (handle_calendar_runs):
     - with no limit every run comes back, newest first, in the answer's old
       shape (no total, no next_offset): the Host's calendar_read op='runs';
     - with a limit, one page, the count of every run and where the next page
       starts; the pages put together are every run once; a limit that is not
       a number is a 400; another Host's task is not found.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback.

  PYTHONPATH=brain python3 scripts/test_calendar_order_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the calendar's order and paging.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


def status_of(unwrap, env):
    from fastapi import HTTPException
    try:
        unwrap(env)
    except HTTPException as e:
        return e.status_code
    return 200


async def seed(pool, now):
    """Thirty events of this Host from ten days ago to twenty ahead, five tasks
    that share one moment, a recurring task, and another Host's rows."""
    from server.bridge import memory_handlers as MH
    kinds = ["scheduled_task", "app_installed", "event", "pr_shipped", "research_task"]
    statuses = ["scheduled", "completed", "cancelled", "scheduled", "failed"]
    for i in range(30):
        at = now + timedelta(hours=i * 24 - 240, minutes=7 * i)
        env = await MH.handle_calendar_append({
            "event_type": kinds[i % 5], "summary": f"event {i}", "event_at": at.isoformat(),
            "status": statuses[i % 5], "source": "user" if i % 5 == 2 else "entity",
            "recurrence": "daily" if i == 13 else None,
        })
        assert env.get("ok"), env
    tie = now + timedelta(days=3, hours=2)
    for i in range(5):
        env = await MH.handle_calendar_append({
            "event_type": "scheduled_task", "summary": f"tie {i}", "event_at": tie.isoformat(),
            "status": "scheduled", "source": "entity", "recurrence": "daily"})
        assert env.get("ok"), env
    for i in range(4):
        await pool.execute(
            "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, "
            "summary, source, status) VALUES ($1, 'x', $2, $2, 'event', $2, $3, 'user', 'scheduled')",
            OTHER_E, now + timedelta(hours=i + 1), f"another host {i}")


async def all_rows(pool, order):
    # Named eid, never id: an ORDER BY name is the output column's first, and
    # an output column called id would sort rows sharing a moment by their
    # uuid's text instead of by the table's id.
    rows = await pool.fetch(
        f"SELECT event_id::text AS eid FROM calendar_events WHERE entity_id = $1 "
        f"ORDER BY event_at {order}, calendar_events.id {order}", E)
    return [r["eid"] for r in rows]


async def section_o(pool, now):
    from server.bridge import memory_handlers as MH
    from server.routes import calendar as cal_routes
    print("O. the list")
    want_desc = await all_rows(pool, "DESC")
    want_asc = await all_rows(pool, "ASC")
    got = await MH.handle_calendar_list({"limit": 500})
    ids = [e["event_id"] for e in got.get("events", [])]
    check("O with no order the list is newest first (rows sharing a moment: the last made first)",
          ids == want_desc, (ids[:6], want_desc[:6]))

    got = await MH.handle_calendar_list({"limit": 500, "order": "asc"})
    ids = [e["event_id"] for e in got.get("events", [])]
    check("O order='asc' is soonest first", ids == want_asc, (ids[:6], want_asc[:6]))

    nxt = await pool.fetch(
        "SELECT event_id::text AS eid FROM calendar_events WHERE entity_id = $1 AND status = 'scheduled' "
        "AND event_at >= $2 ORDER BY event_at, calendar_events.id LIMIT 3", E, now)
    got = await MH.handle_calendar_list({"since": now.isoformat(), "status": "scheduled", "order": "asc", "limit": 3})
    ids = [e["event_id"] for e in got.get("events", [])]
    check("O since now, scheduled, soonest first, three: the next three scheduled events",
          ids == [r["eid"] for r in nxt] and len(ids) == 3, ids)
    check("O ...and a full page says where the next one starts", got.get("next_offset") == 3, got.get("next_offset"))

    for order, want in (("desc", want_desc), ("asc", want_asc)):
        seen, offset = [], 0
        while True:
            page = await MH.handle_calendar_list({"limit": 2, "offset": offset, "order": order})
            seen += [e["event_id"] for e in page["events"]]
            if page.get("next_offset") is None:
                break
            offset = page["next_offset"]
        check(f"O paging two at a time ({order}) returns every row once, rows sharing a moment included",
              seen == want and len(set(seen)) == len(seen), (len(seen), len(want)))

    bad = await MH.handle_calendar_list({"order": "random()"})
    check("O an order that is neither is refused", bad.get("ok") is False and "invalid order" in bad.get("error", ""),
          bad)
    check("O ...and the refusal is a 400", status_of(cal_routes._unwrap, bad) == 400, bad)

    every = await MH.handle_calendar_list({"limit": 500})
    texts = {e["summary"] for e in every["events"]}
    check("O another Host's rows never appear", not any(t.startswith("another host") for t in texts),
          sorted(texts)[:5])
    rec = {e["summary"]: e.get("recurrence") for e in every["events"]}
    check("O each row says how it repeats", rec.get("event 13") == "daily" and rec.get("event 12") is None
          and all(rec.get(f"tie {i}") == "daily" for i in range(5)),
          {k: rec.get(k) for k in ("event 12", "event 13")})


async def section_o_plans(pool, now):
    print("O. the plans on 20,000 rows")
    await pool.execute(
        "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, summary, "
        "source, status) "
        "SELECT $1, 'bulk', x.t, x.t, 'inference_run', x.t, 'bulk ' || g, 'entity', "
        "       (ARRAY['completed','scheduled','failed'])[1 + g % 3] "
        "  FROM generate_series(1, 20000) g, "
        "       LATERAL (SELECT $2::timestamptz + make_interval(mins => g * 37 - 400000)) x(t)",
        E, now)
    await pool.execute("ANALYZE calendar_events")
    cases = {
        "soonest first from now": ("WHERE entity_id = $1 AND event_at >= $2::timestamptz AND status = $3", "ASC",
                                   [E, now, "scheduled"]),
        "the default page": ("WHERE entity_id = $1", "DESC", [E]),
    }
    for label, (where, direction, args) in cases.items():
        n = len(args)
        sql = (f"SELECT event_id, event_at FROM calendar_events {where} "
               f"ORDER BY event_at {direction}, id {direction} LIMIT ${n + 1} OFFSET ${n + 2}")
        plan = json.loads(await pool.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, *args, 50, 0))[0]["Plan"]
        text = json.dumps(plan)
        nodes = re.findall(r'"Node Type": "([^"]+)"', text)
        indexes = set(re.findall(r'"Index Name": "([^"]+)"', text))
        bufs = plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)
        check(f"O {label}: read through a calendar index, never a sort of the table",
              any(i.startswith("idx_calendar") for i in indexes) and "Sort" not in nodes and "Seq Scan" not in nodes,
              (nodes, indexes))
        check(f"O {label}: a page of 50 touches a handful of buffers (under 60)", bufs < 60, bufs)


async def section_r(pool, now):
    from server.bridge import memory_handlers as MH
    from server.routes import calendar as cal_routes
    print("R. a task's runs")
    env = await MH.handle_calendar_append({
        "event_type": "scheduled_task", "summary": "hourly check",
        "event_at": (now + timedelta(minutes=20)).isoformat(),
        "status": "scheduled", "source": "entity", "recurrence": "hourly", "payload": {"prompt": "look"}})
    eid = env["event_id"]
    pk = await pool.fetchval("SELECT id FROM calendar_events WHERE event_id = $1::uuid", eid)
    for i in range(45):
        # Every tenth run shares its moment with the run before it.
        at = now - timedelta(hours=i - (1 if i % 10 == 1 else 0))
        await pool.execute(
            "INSERT INTO calendar_event_runs (event_id, entity_id, ran_at, ok, output_excerpt, error) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            pk, E, at, i % 7 != 3, f"run {i}", f"error {i}" if i % 7 == 3 else None)
    want = [r["output_excerpt"] for r in await pool.fetch(
        "SELECT output_excerpt FROM calendar_event_runs WHERE event_id = $1 ORDER BY ran_at DESC, id DESC", pk)]

    whole = await MH.handle_calendar_runs({"event_id": eid})
    got = [r["output_excerpt"] for r in whole.get("runs", [])]
    check("R with no limit every run comes back, newest first", got == want and len(got) == 45, (len(got), got[:3]))
    check("R ...in the answer's old shape (no total, no next_offset)",
          set(whole) == {"ok", "event_id", "count", "runs"}, sorted(whole))

    first = await MH.handle_calendar_runs({"event_id": eid, "limit": 20})
    check("R a limit gives one page, the count of every run and where the next page starts",
          first.get("count") == 20 and first.get("total") == 45 and first.get("next_offset") == 20
          and first.get("offset") == 0, {k: first.get(k) for k in ("count", "total", "next_offset", "offset")})
    pages, offset = [], 0
    while offset is not None:
        page = await MH.handle_calendar_runs({"event_id": eid, "limit": 20, "offset": offset})
        pages += [r["output_excerpt"] for r in page["runs"]]
        offset = page.get("next_offset")
    check("R the pages put together are every run once, in order", pages == want, (len(pages), pages[:3]))
    last = await MH.handle_calendar_runs({"event_id": eid, "limit": 20, "offset": 40})
    check("R the last page is short and says it is the last",
          last.get("count") == 5 and last.get("next_offset") is None, last.get("next_offset"))

    bad = await MH.handle_calendar_runs({"event_id": eid, "limit": "ten"})
    check("R a limit that is not a number is a 400",
          bad.get("ok") is False and status_of(cal_routes._unwrap, bad) == 400, bad)

    other = uuid.uuid4()
    other_pk = await pool.fetchval(
        "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, summary, source, "
        "status, event_id) VALUES ($1, 'x', now(), now(), 'scheduled_task', now(), 'theirs', 'entity', 'scheduled', $2) "
        "RETURNING id", OTHER_E, other)
    await pool.execute("INSERT INTO calendar_event_runs (event_id, entity_id, ok) VALUES ($1, $2, true)",
                       other_pk, OTHER_E)
    theirs = await MH.handle_calendar_runs({"event_id": str(other), "limit": 20})
    check("R another Host's task is not found", theirs.get("ok") is False and theirs.get("error") == "not found",
          theirs)


async def run(dsn):
    from server import brain_service as _bs
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = (_bs._pool, _bs._entity_id)
    _bs._pool = pool
    _bs._entity_id = E
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        await seed(pool, now)
        await section_o(pool, now)
        await section_r(pool, now)
        await section_o_plans(pool, now)
    finally:
        _bs._pool, _bs._entity_id = saved
        await pool.close()
    return 1 if FAILED else 0


def route_contract():
    """What FastAPI enforces before a handler runs: the list's order is 'asc' or
    'desc' ('desc' when absent; anything else is a 422), and the runs route
    takes a limit (none by default) and an offset."""
    import inspect
    import typing
    from server.routes import calendar as cal_routes
    p = inspect.signature(cal_routes.list_events).parameters.get("order")
    # The module defers its annotations, so the Literal is read resolved.
    args = typing.get_args(typing.get_type_hints(cal_routes.list_events).get("order"))
    check("O the route's order is 'asc' or 'desc', 'desc' when absent",
          p is not None and set(args) == {"asc", "desc"} and getattr(p.default, "default", None) == "desc",
          (p, args))
    q = inspect.signature(cal_routes.event_runs).parameters
    check("R the runs route takes a limit (none by default) and an offset",
          "limit" in q and getattr(q["limit"].default, "default", 0) is None and "offset" in q, list(q))


route_contract()
with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(run(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else rc)
