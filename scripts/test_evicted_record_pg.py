#!/usr/bin/env python3
"""Gate: what rides in place of turns that fell out of the window is the
Host's own close_turn notes, and only when turns did fall out
(brain/mastercode/evicted_record.py, runtime/session.py's history fit).

A rolling summary, written by a separate model call, once led the history
of a thread that fit its window whole. Against a real kernel schema, with
invented values:

  N. nothing fell out (the window starts at the thread's first row): nothing
     leads the history; and no rolling summary leads it either;
  R. turns fell out: the block is the evicted turns' own close_turn notes
     (handoff next step, open work, identifiers verbatim, goal set/met,
     topic), dated, newest first, then how many earlier turn rows are not
     shown and the chat_threads call that opens them;
  D. two builds over the same rows are byte-identical;
  C. it never exceeds RECORD_MAX_CHARS, however many notes there are;
  H. a replay's rows (a hidden lane) are not counted or read;
  S. the session reserves the block's ceiling and fits the window again
     before it inserts it, only when the probe found turns that fell out.
"""
import asyncio
import contextlib
import importlib
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server and docker unavailable — this gate needs a real database.")
    sys.exit(2)

FAILED = 0
E = 1
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
# The rolling summary's module on the trees that still had it (bc5d4a2).
RETIRED_SUMMARY_MODULE = "mastercode." + "thread_" + "consolidator"


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:900]}"))
    if not cond:
        FAILED += 1


async def turn(pool, scope, n, role, text, close=None, app_id="circles"):
    md = {"role": role, "source_context": scope, "app_id": app_id}
    if close is not None:
        md["close_turn"] = close
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, source_session,"
        " metadata, created_at) VALUES ($1, $2, 'turn', 0.5, 'active', 's-ev', $3::jsonb, $4) RETURNING id",
        E, text, json.dumps(md), T0 + timedelta(minutes=n))


async def run(pool):
    from mastercode import circles_threads as ct
    tid = (await ct.create_thread(pool, E, title="a long thread"))["id"]
    S = ct.descriptor_for(tid)
    ids = []
    for i in range(60):
        if i % 2 == 0:
            ids.append(await turn(pool, S, i, "user", f"invented message {i} about the garden plan"))
        else:
            ids.append(await turn(pool, S, i, "assistant", f"invented answer {i}", close={
                "handoff": {"next_step": f"step after {i}", "open_work": [f"item {i}"],
                            "identifiers": [f"  /srv/plan-{i}.txt"]},
                **({"goal": {"set": "Plant the beds by May"}} if i == 3 else {}),
                **({"topic": "garden layout"} if i == 5 else {})}))
    try:
        er = importlib.import_module("mastercode.evicted_record")
    except ImportError as e:
        check("the evicted-turn record exists", False, repr(e))
        er = None

    print("N. nothing fell out")
    # What the bc5d4a2 tree put ahead of a thread that fit whole: its live
    # rolling summary. With the writer retired none is read.
    await pool.execute(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata)"
        " VALUES ($1, 'an older account of the thread', 'summary', 'active', $2::jsonb)",
        E, json.dumps({"source_kind": "thread_consolidator", "source_context": S, "summary_version": 1}))
    lead = None
    try:
        tc = importlib.import_module(RETIRED_SUMMARY_MODULE)
        lead = await tc.get_live_summary(pool, E, S)
    except ImportError:
        lead = None
    if er is not None:
        lead = lead or await er.evicted_record(pool, E, S, "s-ev", oldest_kept_id=ids[0])
    check("N a thread whose window starts at its first row has nothing leading its history",
          lead is None, lead)
    check("N ...and no rolling summary is read for it",
          "get_live_summary" not in turn_source(ROOT))
    if er is None:
        return

    print("R. turns fell out")
    cut = ids[40]          # rows 0..39 fell out
    rec = await er.evicted_record(pool, E, S, "s-ev", oldest_kept_id=cut)
    lines = (rec or "").splitlines()
    check("R it starts with its own heading", lines and lines[0] == er.PREFIX, lines[:1])
    check("R it carries the evicted turns' own notes, newest first, dated",
          len(lines) > 3 and lines[1].startswith("- [2026-09-01] ") and "step after 39" in lines[1]
          and "step after 37" in lines[2], lines[:4])
    check("R identifiers are verbatim, leading spaces kept", "  /srv/plan-39.txt" in rec, rec[:400])
    check("R a goal set and a topic named in a turn are kept with its note",
          "goal set: Plant the beds by May" in rec and "topic: garden layout" in rec, rec[-800:])
    check("R nothing from the turns still in the window", "step after 41" not in rec)
    check("R it says how many earlier rows are not shown and how to open them",
          lines[-1].startswith("40 earlier turn rows of this conversation are not shown here")
          and f"chat_threads op='read' kind='thread' ref='{tid}'" in lines[-1], lines[-1])

    print("D. deterministic")
    rec2 = await er.evicted_record(pool, E, S, "s-ev", oldest_kept_id=cut)
    check("D two builds over the same rows are byte-identical", rec == rec2)

    print("C. bounded")
    tid2 = (await ct.create_thread(pool, E, title="a very long thread"))["id"]
    S2 = ct.descriptor_for(tid2)
    big = []
    for i in range(200):
        role = "user" if i % 2 == 0 else "assistant"
        big.append(await turn(pool, S2, i, role, f"row {i}", close=None if role == "user" else {
            "handoff": {"next_step": "x" * 900, "open_work": ["y" * 300]}}))
    rec3 = await er.evicted_record(pool, E, S2, "s-ev", oldest_kept_id=big[-2])
    check("C it never exceeds RECORD_MAX_CHARS (its last line aside)",
          rec3 and len(rec3) <= er.RECORD_MAX_CHARS + 300, len(rec3 or ""))
    check("C ...and still says how many rows it leaves out",
          "198 earlier turn rows of this conversation are not shown here" in (rec3 or ""), (rec3 or "")[-300:])
    tid3 = (await ct.create_thread(pool, E, title="one long note"))["id"]
    S3 = ct.descriptor_for(tid3)
    one = [await turn(pool, S3, 0, "user", "row 0"),
           await turn(pool, S3, 1, "assistant", "row 1", close={
               "handoff": {"next_step": "z" * 1000, "open_work": ["w" * 900] * 8}}),
           await turn(pool, S3, 2, "user", "row 2")]
    rec5 = await er.evicted_record(pool, E, S3, "s-ev", oldest_kept_id=one[2])
    check("C a newest note longer than the ceiling rides cut, never as 'no note'",
          rec5 and len(rec5.splitlines()) == 3 and rec5.splitlines()[1].endswith("…")
          and "the notes above are the 1 newest" in rec5 and len(rec5) <= er.RECORD_MAX_CHARS + 300,
          (len(rec5 or ""), (rec5 or "")[-200:]))

    print("H. hidden lanes")
    await turn(pool, S, 10.5, "assistant", "a replay's answer",
               close={"handoff": {"next_step": "replayed step"}}, app_id="circles-replay")
    rec4 = await er.evicted_record(pool, E, S, "s-ev", oldest_kept_id=cut)
    check("H a replay's rows are neither counted nor read", "replayed step" not in rec4
          and rec4.splitlines()[-1].startswith("40 earlier"), rec4.splitlines()[-1])

    print("S. the session's fit")
    src = turn_source(ROOT)
    blk = src[src.index("kept = _fit_window(eligible, max_hist)"):src.index("_hist_rows = eligible")]
    first = blk.index("_probe = await _er.evicted_record(")
    check("S the block is probed, its ceiling reserved, the window fitted again, then built",
          first < blk.index("if _probe:") < blk.index("max_hist = max(max_hist - _er.RECORD_MAX_CHARS, 0)")
          < blk.index("kept = _fit_window(eligible, max_hist)", first) < blk.index("_record_msg = {"), "")
    check("S nothing is dispatched after the turn", "dispatch_topic_summary" not in src)


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        await run(pool)
    except Exception as e:  # noqa: BLE001
        check("the gate ran to its end", False, repr(e))
    finally:
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else rc)
