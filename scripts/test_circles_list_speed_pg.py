#!/usr/bin/env python3
"""Gate: what Circles' two lists cost at a Host's size, before the book polls
them. A real kernel schema holding 300 threads and 100,000 turn rows (70,000
in threads, 25,000 in the original conversation, 5,000 elsewhere), 60 open
tickets, 25,000 messages from 200 peer Hosts and 100 people, half of every
kind marked seen.

  P. the thread list (GET /api/circles/threads) reads each thread's newest
     row, newest reply and unseen reply through v219's two thread indexes,
     and the original conversation's count and marks through its two, never
     a scan of entity_memory, planned for the call's arguments and as the
     generic plan a prepared statement can switch to.
  B. the thread reads touch at most BUFFERS_PER_THREAD buffers a thread, and
     the original conversation's at most ORIGINAL_BUFFERS (EXPLAIN ANALYZE):
     the cost follows the number of threads, never the rows' metadata.
  T. wall-clock ceilings on both lists, a backstop only (CEILING_MS is well
     above a loaded machine's time), and the timings printed: the median of
     RUNS calls of each list and the time of each query the thread list
     sends. (Measured once on these rows: the thread list with
     the marks and without v219's two thread indexes took about 81 s.)

Run against another tree (APP_ROOT=<that tree>, e.g. main) it times that
tree's lists on the same rows and reports which checks the tree fails.
Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker.

  PYTHONPATH=brain python3 scripts/test_circles_list_speed_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to measure the lists.")
    sys.exit(2)

FAILED = 0
E = 1
ME_EMAIL = "robin@example.com"
THREADS, THREAD_TURNS, ORIGINAL_TURNS, OTHER_TURNS = 300, 70_000, 25_000, 5_000
PEERS, PEER_MESSAGES, PEOPLE, PEOPLE_MESSAGES = 200, 20_000, 100, 5_000
THREAD_INDEXES = ("idx_entity_memory_thread_rows", "idx_entity_memory_thread_replies")
ORIGINAL_INDEXES = ("idx_entity_memory_original_rows", "idx_entity_memory_original_replies")
INDEXES = THREAD_INDEXES + ORIGINAL_INDEXES
RUNS = 7
CEILING_MS = 2_000
# Measured on these rows: about 3 a thread for each of the three
# reads (an index descent and at most a row). Reading every row of a thread
# instead is about 230 a thread.
BUFFERS_PER_THREAD = 12
# The original conversation's count reads its index entries (and, where the
# visibility map does not yet cover a page, that heap page), never its rows'
# metadata. 25,000 rows hold about 4,000 buffers of table and TOAST.
ORIGINAL_BUFFERS = 400

_stub_main = types.ModuleType("server.main")
_stub_main.app = None
_stub_main.get_bridge_client = lambda: None
sys.modules.setdefault("server.main", _stub_main)


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def _r(g, salt):
    """A repeatable fraction in [0, 1) from the row number."""
    return f"(('x' || substr(md5(({g})::text || ':{salt}'), 1, 8))::bit(32)::bigint / 4294967296.0)"


# A turn row as the turn writers lay it down: role, the conversation's
# descriptor and app tag, and on a Host reply its turn token, metrics and the
# context ledger that make its metadata a few kB.
def _turns_sql(scope_sql: str, app_sql: str, salt: str) -> str:
    return f"""
    WITH s AS (
        SELECT g, {scope_sql} AS sc,
               now() - make_interval(secs => floor({_r('g', salt + 't')} * 300 * 86400)::int) AS ts
          FROM generate_series(1, $2) g)
    INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, source_session,
                               metadata, created_at, landed_at)
    SELECT $1, 'turn ' || s.g, 'turn', 0.5, 'active', 'sess-' || (s.g % 977),
           jsonb_build_object('role', CASE WHEN s.g % 2 = 0 THEN 'user' ELSE 'assistant' END,
                              'app_id', {app_sql}, 'source_context', s.sc)
           || CASE WHEN s.g % 2 = 1 THEN jsonb_build_object(
                  'turn_token', md5(s.g::text || '{salt}'), 'gen_ms', 1200, 'model', 'anthropic/claude-opus-5',
                  'est_cost_usd', 0.01,
                  'context_ledger', jsonb_build_object('spend', jsonb_build_object('calls', 2,
                      'tokens', jsonb_build_object('uncached', 900, 'output', 120)),
                      'filler', repeat('x', 2500)))
              ELSE '{{}}'::jsonb END,
           s.ts, CASE WHEN s.g % 2 = 1 THEN s.ts + interval '20 seconds' END
      FROM s"""


async def seed(pool, has_marks: bool):
    await pool.execute(
        "INSERT INTO circles_threads (entity_id, title, created_at) "
        "SELECT $1, 'Thread ' || g, now() - make_interval(days => 300 - g % 300) FROM generate_series(1, $2) g",
        E, THREADS)
    ids = [str(r["id"]) for r in await pool.fetch("SELECT id FROM circles_threads ORDER BY created_at, id")]
    # Skewed: a few threads hold most of the rows, as a Host's do.
    await pool.execute(
        _turns_sql(f"jsonb_build_object('kind', 'thread', 'ref', ($3::text[])[1 + floor($4 * power({_r('g', 'k')}, 2.2))::int])",
                   "CASE WHEN g % 100 = 7 THEN 'scheduled_task' ELSE 'circles' END", "th"),
        E, THREAD_TURNS, ids, THREADS)
    await pool.execute(
        _turns_sql("CASE WHEN g % 5 = 0 THEN NULL ELSE jsonb_build_object('kind', 'app', 'ref', 'circles') END",
                   "'circles'", "or"),
        E, ORIGINAL_TURNS)
    await pool.execute(
        _turns_sql("jsonb_build_object('kind', 'app', 'ref', 'workbench')", "'workbench'", "ot"),
        E, OTHER_TURNS)
    await pool.execute(
        "INSERT INTO entity_tickets (entity_id, source, sitrep, status, metadata) "
        "SELECT $1, 'manual', 'Pick one', 'open', jsonb_build_object('asked_in', jsonb_build_object("
        "  'app_id', 'circles', 'thread_id', ($2::text[])[1 + g * 5], 'project_id', NULL)) "
        "FROM generate_series(1, 59) g", E, ids)
    await pool.execute(
        "INSERT INTO entity_tickets (entity_id, source, sitrep, status, metadata) VALUES ($1, 'manual', 'x', 'open', "
        "'{\"asked_in\": {\"app_id\": \"circles\", \"thread_id\": null, \"project_id\": null}}'::jsonb)", E)
    # Peer Hosts and people: half the messages theirs, half ours.
    await pool.execute(
        "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, subject, body, "
        "  message_kind, sent_at, delivered_at) "
        "SELECT 'a', 'b', CASE WHEN g % 2 = 0 THEN 1000 + g % $2 ELSE $1 END, "
        "       CASE WHEN g % 2 = 0 THEN $1 ELSE 1000 + g % $2 END, 'Message', 'body ' || g, 'message', "
        "       now() - make_interval(secs => g * 60), now() - make_interval(secs => g * 60) "
        "FROM generate_series(1, $3) g", E, PEERS, PEER_MESSAGES)
    await pool.execute(
        "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, from_user_email, "
        "  to_user_email, subject, body, message_kind, sent_at, delivered_at) "
        "SELECT 'a', 'b', 2000 + g % $2, $1, 'p' || (g % $2) || '@example.com', $3, 'Message', 'hi ' || g, "
        "       'message', now() - make_interval(secs => g * 60), now() - make_interval(secs => g * 60) "
        "FROM generate_series(1, $4) g", E, PEOPLE, ME_EMAIL, PEOPLE_MESSAGES)
    if has_marks:
        await pool.execute(
            "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at, seen_turns) "
            "SELECT $1, 'self', id::text, '', now() - interval '20 days', ARRAY[md5('1th'), md5('3th'), md5('5th')] "
            "FROM circles_threads WHERE (('x' || substr(md5(id::text), 1, 2))::bit(8)::int) % 2 = 0", E)
        await pool.execute(
            "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at) "
            "SELECT $1, 'entity', (1000 + g)::text, '', now() - interval '3 days' FROM generate_series(0, $2 / 2) g",
            E, PEERS)
        await pool.execute(
            "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at) "
            "SELECT $1, 'dm', 'p' || g || '@example.com', $3, now() - interval '3 days' FROM generate_series(0, $2 / 2) g",
            E, PEOPLE, ME_EMAIL)
        await pool.execute(
            "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at) "
            "VALUES ($1, 'self', 'original', '', now() - interval '20 days')", E)
    # As autovacuum leaves an insert-heavy table: visibility map set, statistics
    # current.
    await pool.execute("VACUUM ANALYZE entity_memory")
    await pool.execute("ANALYZE")
    return ids


def walk(plan, out, parent=None):
    out.append({"type": plan.get("Node Type"), "rel": plan.get("Relation Name"), "index": plan.get("Index Name"),
                "parent": parent, "buffers": plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0),
                "loops": plan.get("Actual Loops", 1)})
    for ch in plan.get("Plans") or []:
        walk(ch, out, plan.get("Node Type"))
    return out


async def run(dsn):
    from fastapi.requests import Request

    from server import brain_service as bs
    import server.routes.circles as C

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    saved = (bs._pool, bs._entity_id)
    bs._pool, bs._entity_id = pool, E
    has_marks = bool(await pool.fetchval("SELECT to_regclass('circles_page_marks') IS NOT NULL"))
    try:
        print(f"-- seeding {THREADS} threads, {THREAD_TURNS + ORIGINAL_TURNS + OTHER_TURNS} turn rows, "
              f"{PEER_MESSAGES + PEOPLE_MESSAGES} messages")
        await seed(pool, has_marks)
        n = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE memory_type = 'turn'")
        print(f"   {n} turn rows")

        def req():
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                            "headers": [(b"x-authentik-email", ME_EMAIL.encode())]}, receive)

        async def timed(fn):
            await fn()                                      # warm the cache and the statements
            ms = []
            for _ in range(RUNS):
                t0 = time.perf_counter()
                await fn()
                ms.append((time.perf_counter() - t0) * 1000)
            return statistics.median(ms)

        threads_ms = await timed(C.list_circles_threads)
        conv_ms = await timed(lambda: C.list_conversations(req()))
        out = await C.list_circles_threads()
        print(f"   GET /api/circles/threads        median {threads_ms:7.1f} ms  ({len(out['threads'])} pages)")
        print(f"   GET /api/circles/conversations  median {conv_ms:7.1f} ms")

        # Every query the route sends, captured from the pool with its time.
        captured = []

        class Capturing:
            """The pool, recording each query the list makes and its time."""

            def __getattr__(self, name):
                attr = getattr(pool, name)
                if name in ("fetch", "fetchrow", "fetchval"):
                    async def recorded(q, *a, **k):
                        t0 = time.perf_counter()
                        try:
                            return await attr(q, *a, **k)
                        finally:
                            captured.append((q, a, (time.perf_counter() - t0) * 1000))
                    return recorded
                return attr
        bs._pool = Capturing()
        try:
            await C.list_circles_threads()
        finally:
            bs._pool = pool
        for q, _a, ms in captured:
            head = " ".join(q.split())[:90]
            print(f"   {ms:7.1f} ms  {head}")
        tq = next(((q, a) for q, a, _ms in captured if "circles_threads c" in q and "LATERAL" in q), None)
        oq = next(((q, a) for q, a, _ms in captured if "ref = 'original'" in q and "FROM (SELECT 1) one" in q),
                  None)
        check("P the thread list reads each thread's marks in its one query (LATERAL per thread)",
              tq is not None, [q[:80] for q, _a, _m in captured])
        # There is no original conversation: the list no
        # longer reads its count or marks at all.
        check("P ...and no query reads the original conversation's count or marks",
              oq is None and not any("ref = 'original'" in q for q, _a, _m in captured),
              [q[:80] for q, _a, _m in captured])

        async def plans(q, args):
            async with pool.acquire() as c:
                raw = await c.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + q, *args)
                await c.execute("SET plan_cache_mode = force_generic_plan")
                await c.execute(f"PREPARE g AS {q}")
                try:
                    gen = await c.fetchval("EXPLAIN (FORMAT JSON) EXECUTE g(" + ", ".join(
                        "$$%s$$" % a for a in args) + ")")
                finally:
                    await c.execute("DEALLOCATE g")
                    await c.execute("RESET plan_cache_mode")
            return (walk((json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"], []),
                    walk((json.loads(gen) if isinstance(gen, str) else gen)[0]["Plan"], []))
        for what, found, wanted in (("each thread's reads", tq, THREAD_INDEXES),
                                    ("the original conversation's reads", oq, ORIGINAL_INDEXES)):
            if found is None:
                continue
            nodes, gnodes = await plans(*found)
            for label, ns in (("custom", nodes), ("generic", gnodes)):
                seq = [x for x in ns if x["type"] == "Seq Scan" and x["rel"] == "entity_memory"]
                used = {x["index"] for x in ns if x["index"]}
                check(f"P ({label} plan) {what}: no sequential scan of entity_memory", not seq, seq)
                check(f"P ({label} plan) {what} go through v219's {' and '.join(wanted)}",
                      set(wanted) <= used, sorted(i for i in used if i))
            em = [x for x in nodes if x["index"] in wanted]
            total = sum(x["buffers"] for x in em)
            if found is tq:
                per = total / max(1, THREADS)
                print(f"   thread query: {total} buffers in the v219 index reads, {per:.1f} a thread")
                check(f"B the per-thread reads touch at most {BUFFERS_PER_THREAD} buffers a thread",
                      bool(em) and per <= BUFFERS_PER_THREAD, per)
            else:
                print(f"   original conversation: {total} buffers in the v219 index reads "
                      f"({ORIGINAL_TURNS} rows)")
                check(f"B the original conversation's reads touch at most {ORIGINAL_BUFFERS} buffers, "
                      "never its rows' metadata", bool(em) and total <= ORIGINAL_BUFFERS, total)
        check(f"T the thread list answers under {CEILING_MS} ms (median)", threads_ms < CEILING_MS, threads_ms)
        check(f"T the conversations list answers under {CEILING_MS} ms (median)", conv_ms < CEILING_MS, conv_ms)
    finally:
        bs._pool, bs._entity_id = saved
        await pool.close()


async def main(dsn):
    try:
        await run(dsn)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("ran to the end", False, f"{type(e).__name__}: {e}")
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
