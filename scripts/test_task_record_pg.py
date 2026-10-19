#!/usr/bin/env python3
"""Gate: the task record against a real kernel schema.

  M. migrations/v208 on the full current schema: the four tables exist, and
     running the file again, twice over, fails nothing.
  O. the record opens with the goal: record_open is false until the project
     has a check, true after.
  F. a search's sources become found units once each (note_found), under one
     spelling (source_key: case, default port, fragment, trailing slash).
  Q. a finding is kept only when its quote is in text the platform fetched for
     that source in this task:
       - a source never read is refused, naming read_url;
       - the quote is matched through curly quotes, dashes, an ellipsis,
         whitespace runs and a word broken at a line end, both readings;
       - a reworded quote is refused with the time the source was read, and a
         truncated read says to read on;
       - a quote under four words is refused;
       - the stored quote has passed screen_for_model.
  C. coverage: covered needs the source read (unless unreadable) and, for
     outcome findings, a live finding from it; a claim held by another thread
     is refused, by the same thread repeated.
  K. checks: answered needs a linked live finding; unanswerable needs every
     found source covered; a check of another project is refused.
  R. read returns the checks with their finding counts, the coverage counts,
     the unread units and a coverage statement with the right numbers.
  P. another entity's project is refused before anything runs.
  S. MUTATION: with task_findings off the sandbox's write allowlist, a finding
     is denied by the sandbox; the allowlist entry is what lets it through.
Fresh clone of the KERNEL's schema (scripts/kernel_schema.py) on TEST_PG_DSN /
PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres through docker.
  PYTHONPATH=brain python3 scripts/test_task_record_pg.py
"""
import asyncio
import contextlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the task record's SQL and v208.")
    sys.exit(2)

FAILED = 0
E = 1
MIGRATION = ROOT / "migrations" / "v208_task_record.sql"
TABLES = ("goal_checks", "task_coverage", "task_sources", "task_findings")

PAGE = ("The pilot ran for “eighteen months” and stored 4.2 MWh — more than the "
        "council’s target… Its opera-\ntors called the result well-\nknown in the region.\n\n"
        "Costs   fell   by a third.")


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def apply_v208(dsn):
    return _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))


async def migration_checks(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        tables = [await pool.fetchval("SELECT to_regclass($1)", f"public.{t}") for t in TABLES]
        check("M all four tables exist", all(tables), tables)
        r1 = apply_v208(dsn)
        check("M v208 runs again on the full schema and fails nothing", r1.returncode == 0, r1.stderr[-400:])
        r2 = apply_v208(dsn)
        check("M ...and a second time", r2.returncode == 0, r2.stderr[-400:])
        again = [await pool.fetchval("SELECT to_regclass($1)", f"public.{t}") for t in TABLES]
        check("M ...leaving the four tables as they were", again == tables, (tables, again))
    finally:
        await pool.close()


async def store_checks(dsn):
    from mastercode.runtime import task_record as tr
    from mastercode.sandbox import sql_proxy

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=4)
    try:
        pid = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'tidal pilot') "
                                  "RETURNING id", E)
        other = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'other') "
                                    "RETURNING id", E)

        def op(a, project=pid, thread="thread-A"):
            return tr.run_task_record_op(pool, E, a, project_id=project, thread_key=thread,
                                         session_id="sess-1")

        # ── O ──
        check("O no record before the goal", await tr.record_open(pool, E, pid) is False)
        c1 = (await op({"op": "check", "question": "How much energy did the pilot store?"}))["result"]["check"]
        c2 = (await op({"op": "check", "question": "Did costs fall?"}))["result"]["check"]
        c_other = (await op({"op": "check", "question": "unrelated"}, project=other))["result"]["check"]
        check("O the record opens with the first check", await tr.record_open(pool, E, pid) is True)

        # ── F ──
        n = await tr.note_found(pool, E, pid, [
            {"url": "HTTPS://Example.ORG:443/pilot/#results", "title": "Pilot"},
            {"url": "https://example.org/report", "title": "Report"},
            {"url": "https://example.org/unread", "title": "Never read"}], "web_search")
        check("F three sources found", n == 3, n)
        odd = await tr.note_found(pool, E, pid, ["https://example.org/a-string", None, 7,
                                                 {"url": "http://[::1"}], "web_search")
        check("F entries that are not sources, and an unparseable url, are skipped, never raised", odd == 0, odd)
        bad = await op({"op": "finding", "claim": "c", "quote": "q", "source": "http://[::1"})
        check("F a finding naming a web address that does not parse says so, not 'source required'",
              bad.get("status") != "ok" and "not a web address" in str(bad), bad)
        again = await tr.note_found(pool, E, pid, [{"url": "https://example.org/pilot"}], "research")
        check("F a source found again is the same unit (one spelling)", again == 0, again)
        check("F stored under source_key",
              await pool.fetchval("SELECT count(*) FROM task_coverage WHERE source_ref = "
                                  "'https://example.org/pilot'") == 1)

        # ── Q ──
        r = await op({"op": "finding", "claim": "It stored 4.2 MWh",
                      "quote": "stored 4.2 MWh - more than", "source": "https://example.org/pilot",
                      "checks": [c1]})
        check("Q a source never read is refused, naming read_url",
              r.get("status") != "ok" and "read_url" in str(r.get("result")), r)
        v = await tr.keep_source(pool, E, pid, "https://example.org/pilot/", PAGE,
                                 read_range={"offset": 0, "chars": len(PAGE), "truncated": False},
                                 title="Pilot")
        check("Q the read is kept with its version", bool(v) and len(v) == 64, v)
        r = await op({"op": "finding", "claim": "It ran 18 months and stored 4.2 MWh",
                      "quote": 'The pilot ran for "eighteen months" and stored 4.2 MWh - more than the '
                               "council's target...",
                      "source": "https://example.org/pilot", "checks": [c1]})
        check("Q straight quotes, a plain dash and three dots match the page's curly ones",
              r.get("status") == "ok", r)
        f1 = r.get("result", {}).get("finding")
        r = await op({"op": "finding", "claim": "operators", "quote": "Its operators called the result",
                      "source": "https://example.org/pilot"})
        check("Q a word broken at a line end matches joined", r.get("status") == "ok", r)
        r = await op({"op": "finding", "claim": "well known", "quote": "the result well-known in the region",
                      "source": "https://example.org/pilot"})
        check("Q a real hyphen at a line end matches kept", r.get("status") == "ok", r)
        r = await op({"op": "finding", "claim": "cost", "quote": "Costs fell by a third.",
                      "source": "https://example.org/pilot", "checks": [c2]})
        check("Q whitespace runs collapse", r.get("status") == "ok", r)
        r = await op({"op": "finding", "claim": "x", "quote": "The pilot ran for nineteen months",
                      "source": "https://example.org/pilot"})
        check("Q a reworded quote is refused with the time the source was read",
              r.get("status") != "ok" and "not in https://example.org/pilot as read at" in str(r.get("result")), r)
        r = await op({"op": "finding", "claim": "x", "quote": "stored 4.2",
                      "source": "https://example.org/pilot"})
        check("Q a quote under four words is refused", r.get("status") != "ok" and "4 words" in str(r), r)
        await tr.keep_source(pool, E, pid, "https://example.org/report", "First part of a long report text.",
                             read_range={"offset": 0, "chars": 34, "next_offset": 34, "truncated": True})
        r = await op({"op": "finding", "claim": "x", "quote": "words from the second part only",
                      "source": "https://example.org/report"})
        check("Q a truncated read says to read on", r.get("status") != "ok" and "read on" in str(r), r)
        stored = await pool.fetchval("SELECT quote FROM task_findings WHERE id = $1", f1)
        check("Q the stored quote is the quote as given (screened)", stored.startswith("The pilot ran"), stored)
        r = await op({"op": "finding", "claim": "x", "quote": "Costs fell by a third.",
                      "source": "https://example.org/pilot", "checks": [c_other]})
        check("Q a check of another project is refused", r.get("status") != "ok", r)

        # ── C ──
        r = await op({"op": "covered", "source": "https://example.org/unread", "outcome": "nothing_relevant"})
        check("C a source never read cannot be covered as nothing_relevant",
              r.get("status") != "ok" and "not read" in str(r), r)
        r = await op({"op": "covered", "source": "https://example.org/report", "outcome": "findings"})
        check("C outcome findings needs a live finding from the source", r.get("status") != "ok", r)
        r = await op({"op": "covered", "source": "https://example.org/pilot", "outcome": "findings"})
        check("C the pilot page covered with findings", r.get("status") == "ok", r)
        r = await op({"op": "claim", "source": "https://example.org/report"})
        check("C a claim", r.get("status") == "ok", r)
        r = await op({"op": "claim", "source": "https://example.org/report"}, thread="thread-B")
        check("C a claim held by another thread is refused",
              r.get("status") != "ok" and "thread-A" in str(r), r)
        r = await op({"op": "claim", "source": "https://example.org/report"})
        check("C the same thread may claim again", r.get("status") == "ok", r)

        # ── K ──
        r = await op({"op": "check", "id": c2, "status": "answered"})
        check("K answered with a linked finding", r.get("status") == "ok", r)
        c3 = (await op({"op": "check", "question": "Who funded it?"}))["result"]["check"]
        r = await op({"op": "check", "id": c3, "status": "answered"})
        check("K answered with no linked finding is refused", r.get("status") != "ok", r)
        r = await op({"op": "check", "id": c3, "status": "unanswerable"})
        check("K unanswerable with found sources unread is refused, listing them",
              r.get("status") != "ok" and "example.org/unread" in str(r), r)
        r = await op({"op": "check", "id": c_other, "status": "open"})
        check("K another project's check is refused", r.get("status") != "ok", r)

        # ── R ──
        r = (await op({"op": "read"}))["result"]
        cov = r["coverage"]
        check("R coverage counts", cov == {"found": 3, "covered": 1, "claimed": 1, "unread": 2}, cov)
        check("R the statement says what was read and what was not",
              r["coverage_statement"].startswith("Read 1 of the 3 sources found; 2 not read:"),
              r["coverage_statement"])
        check("R checks carry their finding counts",
              {c["id"]: c["findings"] for c in r["checks"]}.get(c1) == 1
              and {c["id"]: c["status"] for c in r["checks"]}.get(c2) == "answered", r["checks"])
        await op({"op": "covered", "source": "https://example.org/report", "outcome": "nothing_relevant"})
        await op({"op": "covered", "source": "https://example.org/unread", "outcome": "unreadable"})
        r = await op({"op": "check", "id": c3, "status": "unanswerable", "note": "no source names a funder"})
        check("K unanswerable once every found source is covered", r.get("status") == "ok", r)

        # ── P ──
        foreign = await pool.fetchval(
            "INSERT INTO projects (entity_id, name) VALUES ($1, 'theirs') RETURNING id", E + 1) \
            if await pool.fetchval("SELECT 1 FROM entity_registry WHERE id = $1", E + 1) else None
        r = await tr.run_task_record_op(pool, E, {"op": "read"}, project_id=foreign or 999_999)
        check("P a project that is not this entity's is refused", r.get("status") != "ok", r)

        # ── X: a redirect, a claim race, the version cap ──
        await tr.note_found(pool, E, pid, [{"url": "https://short.example/a"}], "web_search")
        await tr.keep_source(pool, E, pid, "https://landed.example/page", "The landed page says the grid held.",
                             asked="https://short.example/a")
        r = await op({"op": "finding", "claim": "held", "quote": "The landed page says the grid",
                      "source": "https://short.example/a"})
        check("X a finding naming the asked URL is checked against the page it landed on, and names that page",
              r.get("status") == "ok" and r["result"]["source"] == "https://landed.example/page", r)
        r = await op({"op": "covered", "source": "https://short.example/a", "outcome": "findings"})
        rows = {x["source_ref"]: (x["status"], x["outcome"]) for x in await pool.fetch(
            "SELECT source_ref, status, outcome FROM task_coverage WHERE project_id = $1 "
            "AND source_ref IN ('https://short.example/a', 'https://landed.example/page')", pid)}
        check("X covering the asked URL covers both rows",
              r.get("status") == "ok" and rows == {"https://short.example/a": ("covered", "findings"),
                                                   "https://landed.example/page": ("covered", "findings")}, (r, rows))
        await tr.note_found(pool, E, pid, [{"url": "https://race.example/x"}], "web_search")
        got = await asyncio.gather(*(op({"op": "claim", "source": "https://race.example/x"}, thread=f"t{i}")
                                     for i in range(6)))
        check("X six threads claim one unit at once: exactly one holds it",
              sum(1 for g in got if g.get("status") == "ok") == 1, [g.get("status") for g in got])
        for i in range(7):
            await tr.keep_source(pool, E, pid, "https://churn.example/p", f"Version {i} of a changing page here.")
        r = await op({"op": "finding", "claim": "v0", "quote": "Version 6 of a changing page",
                      "source": "https://churn.example/p"})
        # Five newer reads push version 6 out of the newest five; its finding keeps it.
        for i in range(7, 12):
            await tr.keep_source(pool, E, pid, "https://churn.example/p", f"Version {i} of a changing page here.")
        kept = await pool.fetchval("SELECT count(*) FROM task_sources WHERE project_id = $1 "
                                   "AND source_ref = 'https://churn.example/p'", pid)
        still = await pool.fetchval("SELECT count(*) FROM task_sources WHERE project_id = $1 "
                                    "AND source_ref = 'https://churn.example/p' AND text LIKE 'Version 6 %'", pid)
        check("X at most KEEP_VERSIONS reads of a source are kept, plus any a live finding was checked against",
              r.get("status") == "ok" and kept == tr.KEEP_VERSIONS + 1 and still == 1, (kept, still, r))

        # ── S ──
        real = (sql_proxy.WRITE_MEMORY_TABLES, sql_proxy.WRITE_CONTEXT_TABLES)
        try:
            sql_proxy.WRITE_MEMORY_TABLES = real[0] - {"task_findings"}
            sql_proxy.WRITE_CONTEXT_TABLES = real[1] - {"task_findings"}
            try:
                await op({"op": "finding", "claim": "x", "quote": "Costs fell by a third.",
                          "source": "https://example.org/pilot"})
                denied = False
            except sql_proxy.SQLDenied:
                denied = True
        finally:
            sql_proxy.WRITE_MEMORY_TABLES, sql_proxy.WRITE_CONTEXT_TABLES = real
        check("S MUTATION: off the allowlist, a finding is denied by the sandbox", denied)
        r = await op({"op": "finding", "claim": "x", "quote": "Costs fell by a third.",
                      "source": "https://example.org/pilot"})
        check("S and with the entry back, it lands", r.get("status") == "ok", r)
    finally:
        await pool.close()
    return 1 if FAILED else 0


async def main(dsn):
    await migration_checks(dsn)
    return await store_checks(dsn)


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
