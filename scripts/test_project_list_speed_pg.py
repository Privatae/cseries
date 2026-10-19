#!/usr/bin/env python3
"""Gate: the projects list (GET /api/projects) and the project search read
through migrations/v216, and read the values they read before it. A real
kernel schema, holding a Host's worth of rows: 300 projects, 75,000 turn
rows in projects and 25,000 in Circles threads, 5,000 other memory rows,
5,000 tasks.

  M. v216's file run over a Host that holds those rows and lacks the three
     indexes (the schema under test already carries v216, so the gate drops
     the indexes first): the three indexes are built and valid, and
     entity_memory.landed_at is timestamptz, nullable with no default; the
     run touches no row (the table keeps its file and fills no landed_at:
     nothing is filled at boot); a second run changes nothing and fails nothing.
  V. every list row's last_activity_at, last_landed_at, waiting_on_you and
     ask_cards_open, and the list's order, are the unindexed computation's
     (read before the indexes are built), and the search returns the same
     projects in the same order with the same scores before and after the
     indexes. The rows include the cases each field turns on: a project with
     nothing in it, a task newer than every turn (finished, running, queued),
     replies that landed long after they began, replies that carry no
     landed_at, and another entity's rows pointing at this entity's projects.
  P. the plans the two routes send (captured from the route, not copied here)
     use the new indexes and never scan entity_memory whole, and the list
     reads a project's last landed reply as one entry of the replies index (a
     scan under a Limit), never all of its replies: planned for the call's
     arguments and as the generic plan a prepared statement can switch to,
     with the statistics from before v216 and again after ANALYZE.
  B. each of the list's two reads of entity_memory touches at most
     BUFFERS_PER_PROJECT buffers for each project (EXPLAIN ANALYZE): the cost
     follows the number of projects, never the number of replies. Buffers do
     not depend on how loaded the machine is; a clock does.
  T. a wall-clock ceiling on each route, as a backstop only (CEILING_MS is
     well above a loaded machine's time and well below the time of a read
     that walks every reply).

Fresh clones of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker. Run against another tree
(APP_ROOT=<that tree>) it reports which checks that tree fails.

  PYTHONPATH=brain python3 scripts/test_project_list_speed_pg.py
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

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the plans and v216.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2
VERSION = 216
# Found by its number, so a tree whose v216 has another name is still measured.
MIGRATION = next(iter(sorted((ROOT / "migrations").glob(f"v{VERSION}_*.sql"))), None)
INDEXES = {
    "idx_entity_memory_project_turns": "entity_memory",
    "idx_entity_memory_project_replies": "entity_memory",
    "idx_entity_tasks_project": "entity_tasks",
}
REPLIES_INDEX = "idx_entity_memory_project_replies"
PROJECTS, PROJECT_TURNS, CIRCLES_TURNS, OTHER_MEMORY, TASKS = 300, 75_000, 25_000, 5_000, 5_000
CEILING_MS = 1_000
# Measured 2026-10-08 on this data: about 3 a project for each read (an index
# descent and one row). Reading every reply instead took about 125 a project.
BUFFERS_PER_PROJECT = 10


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


# ── the rows ────────────────────────────────────────────────────────────────
# Pseudo-random but repeatable: a fraction in [0, 1) from md5 of the row number
# and a salt, so two runs seed the same Host.
def _r(g, salt):
    return f"(('x' || substr(md5(({g})::text || ':{salt}'), 1, 8))::bit(32)::bigint / 4294967296.0)"


# A pool of 400 paragraphs to cut turn and task text from (no table of its own).
_PARAS = """pa AS MATERIALIZED (
    SELECT array_agg(t ORDER BY g) AS a FROM (
        SELECT g, string_agg(translate(substr(md5(((g * 7919 + k * 104729) % 2000)::text), 1, 3 + (g + k) % 6),
                                       '0123456789', 'ghijklmnop'), ' ') || '.' AS t
          FROM generate_series(1, 400) g, generate_series(1, 40 + g % 40) k
         GROUP BY g) x)"""

# A turn row as this tree's writers lay it down (memory_engine):
# metadata.role, the source_context descriptor, the landed time in landed_at,
# and on a Host reply the metrics and the context ledger (session.py) that
# make its metadata a few kB. Rows older than 200 days carry no landed_at, and
# so do a few younger ones (a row from before the column reads by created_at).
_PROJECT_TURNS_SQL = f"""
WITH {_PARAS},
s AS (
    SELECT g, ($2::int[])[1 + floor($3 * power({_r('g', 'p')}, 2.2))::int] AS pid,
           now() - make_interval(secs => floor({_r('g', 't')} * 300 * 86400)::int) AS ts
      FROM generate_series(1, $4) g)
INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, source_session,
                           project_id, metadata, landed_at, created_at)
SELECT $1,
       CASE WHEN s.g % 2 = 0 THEN pa.a[1 + s.g % 400]
            ELSE array_to_string(pa.a[1 + s.g % 390 : 1 + s.g % 390 + 1 + s.g % 4], E'\\n\\n') END,
       'turn', 0.5, 'active', 'sess-' || (s.g % 977), s.pid,
       jsonb_build_object('source_kind', 'chat_turn', 'role', CASE WHEN s.g % 2 = 0 THEN 'user' ELSE 'assistant' END,
                          'app_id', 'workbench', 'turn_token', md5(s.g::text),
                          'source_context', jsonb_build_object('kind', 'project', 'ref', s.pid::text))
       || CASE WHEN s.g % 2 = 1 THEN jsonb_build_object(
              'gen_ms', 1000 + s.g % 30000, 'model', 'anthropic/claude-opus-5',
              'est_cost_usd', round((s.g % 500) / 1000.0, 4),
              'context_ledger', jsonb_build_object(
                  'calls', 1 + s.g % 24,
                  'per_call', (SELECT jsonb_agg(jsonb_build_object(
                                   'in', 20000 + k * 13, 'cached', 18000, 'cache_write', 300, 'out', 400 + k,
                                   'usd', round(0.01 * k, 4), 'model', 'anthropic/claude-opus-5',
                                   'tools_chars', 31000, 'tools_sha', md5((s.g * k)::text),
                                   'request_id', 'req_' || md5((s.g + k)::text)))
                                 FROM generate_series(1, 1 + s.g % 24) k),
                  'spend', jsonb_build_object('calls', 1 + s.g % 24, 'spent_tokens', 30000 + s.g,
                                              'tokens', jsonb_build_object('uncached', 2000, 'cache_read', 18000,
                                                                           'cache_write', 300, 'output', 400)),
                  'system_chars', 41000, 'user_chars', 300 + s.g % 900,
                  'per_turn_blocks', jsonb_build_object('memory', 2400, 'timeline', 800, 'skills', 1200),
                  'history', jsonb_build_object('turns', 12, 'chars', 22000, 'summary', false),
                  'tools', jsonb_build_object('count', 40, 'chars', 31000, 'groups', jsonb_build_array('core', 'projects')),
                  'tool_results', (SELECT jsonb_agg(jsonb_build_object('tool', 'project_files', 'chars', 900 + k))
                                     FROM generate_series(1, s.g % 7) k)))
          ELSE '{{}}'::jsonb END,
       CASE WHEN s.g % 997 = 1 OR s.g % 991 = 1 OR s.g % 983 = 1 THEN NULL
            WHEN s.ts > now() - interval '200 days'
            THEN to_timestamp(round(LEAST(extract(epoch FROM now()),
                     extract(epoch FROM s.ts) + CASE WHEN s.g % 2 = 1 THEN s.g % 3600 ELSE 0 END)::numeric, 3)::float8)
       END,
       s.ts
  FROM s CROSS JOIN pa"""

_CIRCLES_TURNS_SQL = f"""
WITH {_PARAS}
INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, source_session,
                           project_id, metadata, landed_at, created_at)
SELECT $1, pa.a[1 + g % 400], 'turn', 0.5, 'active', 'circ-' || (g % 400), NULL,
       jsonb_build_object('source_kind', 'chat_turn', 'role', CASE WHEN g % 2 = 0 THEN 'user' ELSE 'assistant' END,
                          'app_id', 'circles',
                          'source_context', jsonb_build_object('kind', 'thread', 'ref', md5((g % 400)::text))),
       to_timestamp(round(extract(epoch FROM now())::numeric - g, 3)::float8),
       now() - make_interval(secs => floor({_r('g', 'c')} * 300 * 86400)::int)
  FROM generate_series(1, $2) g CROSS JOIN pa"""

_OTHER_MEMORY_SQL = f"""
WITH {_PARAS}
INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata, created_at, project_id)
SELECT $1, pa.a[1 + g % 400], (ARRAY['cited_fact','preference','goal','research','relationship','summary'])[1 + g % 6],
       0.7, 'active', jsonb_build_object('source_kind', 'extracted'), now() - make_interval(days => g % 300),
       CASE WHEN g % 5 = 0 THEN ($3::int[])[1 + g % $4] END
  FROM generate_series(1, $2) g CROSS JOIN pa"""

# Tasks: builds, fan-outs, agents, proofs, research. project_id as the writers
# put it (a JSON number; also a string, a null, no key, no result at all).
_TASKS_SQL = f"""
WITH {_PARAS},
s AS (
    SELECT g, now() - make_interval(secs => floor({_r('g', 'k')} * 300 * 86400)::int) AS ts,
           ($3::int[])[1 + floor($4 * power({_r('g', 'q')}, 1.8))::int] AS pid,
           (ARRAY['completed','completed','completed','completed','completed','completed','failed',
                  'cancelled','in_progress','queued'])[1 + g % 10] AS st
      FROM generate_series(1, $2) g)
INSERT INTO entity_tasks (entity_id, title, description, task_type, status, result, created_at,
                          started_at, completed_at)
SELECT $1, 'task ' || s.g, pa.a[1 + s.g % 400],
       (ARRAY['build','fan_out','agent','app_verify','research'])[1 + s.g % 5], s.st,
       CASE WHEN s.g % 15 = 14 THEN NULL
            ELSE jsonb_build_object('intent', left(pa.a[1 + (s.g * 3) % 400], 400), 'harness', 'claude-code',
                                    'timeout_s', 3600,
                                    'summary', array_to_string(pa.a[1 + s.g % 390 : 1 + s.g % 390 + s.g % 5], ' '))
                 || CASE WHEN s.g % 5 = 4 THEN '{{}}'::jsonb
                         WHEN s.g % 37 = 0 THEN jsonb_build_object('project_id', s.pid::text)
                         WHEN s.g % 41 = 0 THEN jsonb_build_object('project_id', NULL)
                         ELSE jsonb_build_object('project_id', s.pid) END END,
       s.ts, CASE WHEN s.st <> 'queued' THEN s.ts + interval '5 seconds' END,
       CASE WHEN s.st IN ('completed', 'failed', 'cancelled') THEN s.ts + make_interval(secs => 60 + s.g % 7200) END
  FROM s CROSS JOIN pa"""


async def seed(pool):
    """The Host's rows; returns the special projects by name."""
    pids = [await pool.fetchval(
        "INSERT INTO projects (entity_id, name, description, status, kind, config, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, 'project', '{}'::jsonb, now() - make_interval(days => $5), "
        "LEAST(now() - interval '1 hour', now() - make_interval(days => $5) + make_interval(hours => $6))) "
        "RETURNING id",
        E, f"Project {i}", f"what project {i} builds",
        ("active", "active", "planning", "review", "complete", "archived")[i % 6], 300 - i, (i * 37) % 500)
        for i in range(PROJECTS)]
    await pool.execute(
        "INSERT INTO projects (entity_id, name, status, kind, created_at, updated_at) "
        "SELECT $1, 'Artifact ' || g, 'active', 'artifact', now() - interval '3 days', now() - interval '3 days' "
        "FROM generate_series(1, 20) g", E)
    # The last four projects carry the special cases; turns go to the others.
    busy, special = pids[:-4], pids[-4:]
    quiet, task_done, task_running, task_queued = special
    await pool.execute(
        "INSERT INTO project_phases (project_id, phase_number, title, status) "
        "SELECT p, n, 'Phase ' || n, (ARRAY['planning','in_progress','complete','review'])[1 + (p + n) % 4] "
        "FROM unnest($1::int[]) p, generate_series(1, 6) n WHERE n <= p % 7", pids)
    await pool.execute(
        "INSERT INTO phase_steps (phase_id, step_number, title, status) "
        "SELECT ph.id, n, 'Step ' || n, (ARRAY['pending','implementing','done','done','failed','checking'])"
        "[1 + (ph.id + n) % 6] FROM project_phases ph, generate_series(1, 8) n WHERE n <= ph.id % 9")
    await pool.execute(_PROJECT_TURNS_SQL, E, busy, len(busy), PROJECT_TURNS)
    await pool.execute(_CIRCLES_TURNS_SQL, E, CIRCLES_TURNS)
    await pool.execute(_OTHER_MEMORY_SQL, E, OTHER_MEMORY, pids, len(pids))
    await pool.execute(_TASKS_SQL, E, TASKS, busy, len(busy))
    # A task newer than every turn: finished, running (no completed_at: its
    # start counts), queued (only created_at).
    await pool.execute(
        "INSERT INTO entity_tasks (entity_id, title, task_type, status, result, created_at, started_at, completed_at) "
        "VALUES ($1, 'done now', 'build', 'completed', jsonb_build_object('project_id', $2::int), "
        "        now() - interval '10 minutes', now() - interval '9 minutes', now() - interval '1 minute'), "
        "       ($1, 'running', 'build', 'in_progress', jsonb_build_object('project_id', $3::int), "
        "        now() - interval '2 minutes', now() - interval '30 seconds', NULL), "
        "       ($1, 'queued', 'agent', 'queued', jsonb_build_object('project_id', $4::int), "
        "        now() - interval '10 seconds', NULL, NULL)",
        E, task_done, task_running, task_queued)
    # Another entity's rows pointing at this entity's projects, newer than
    # anything: they must never count.
    await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, 'other host') "
                       "ON CONFLICT (id) DO NOTHING", OTHER_E)
    await pool.execute(
        "INSERT INTO projects (entity_id, name, status, created_at, updated_at) "
        "SELECT $1, 'Other ' || g, 'active', now(), now() FROM generate_series(1, 5) g", OTHER_E)
    await pool.execute(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, project_id, metadata, "
        "landed_at, created_at) "
        "SELECT $1, 'not yours', 'turn', 'active', p, jsonb_build_object('role', 'assistant'), "
        "now(), now() FROM unnest($2::int[]) p",
        OTHER_E, [busy[0], quiet, task_done])
    await pool.execute(
        "INSERT INTO entity_tasks (entity_id, title, status, result, created_at, completed_at) "
        "SELECT $1, 'not yours', 'completed', jsonb_build_object('project_id', p), now(), now() "
        "FROM unnest($2::int[]) p", OTHER_E, [busy[1], quiet])
    await pool.execute(
        "INSERT INTO entity_tickets (entity_id, source, sitrep, status, options, metadata, opened_at) "
        "SELECT $1, 'host', 'ask ' || g, CASE WHEN g % 4 = 0 THEN 'open' ELSE 'decided' END, "
        "'[{\"id\":\"a\",\"label\":\"yes\"}]'::jsonb, "
        "jsonb_build_object('asked_in', jsonb_build_object('project_id', ($2::int[])[1 + g % $3]::text)), "
        "now() - make_interval(hours => g) FROM generate_series(1, 400) g", E, pids, len(pids))
    # Statistics as a Host has them: autovacuum analyzes a table this busy.
    for table in ("projects", "entity_memory", "entity_tasks", "entity_tickets", "project_phases", "phase_steps"):
        await pool.execute(f"ANALYZE {table}")
    return {"busy": busy[0], "busy_task": busy[1], "quiet": quiet, "task_done": task_done,
            "task_running": task_running, "task_queued": task_queued}


# ── reading the routes ──────────────────────────────────────────────────────

class Capture:
    """The pool, keeping the SQL and arguments of every fetch and its rows."""

    def __init__(self, pool):
        self._pool, self.calls = pool, []

    async def fetch(self, q, *a):
        rows = await self._pool.fetch(q, *a)
        self.calls.append((q, a, rows))
        return rows

    def __getattr__(self, name):
        return getattr(self._pool, name)


async def read_list(P, bs, pool):
    """(rows, sql, args, wall ms) of the Workbench's own call."""
    cap = Capture(pool)
    bs._pool, bs._entity_id = cap, E
    t0 = time.perf_counter()
    out = await P.list_projects(include_archived=True, limit=1000)
    ms = (time.perf_counter() - t0) * 1000
    q, a, _rows = cap.calls[-1]
    return out["projects"], q, a, ms


async def read_search(P, pool, text="project"):
    """(the service's ranked answer, the SQL's own rows, sql, args, wall ms) of
    a search through the real service; no embedding lane, no background
    refresh."""
    cap = Capture(pool)
    t0 = time.perf_counter()
    out = await P.search_projects_service(cap, E, text, limit=50, lane=(None, ""), refresh_in_background=False)
    ms = (time.perf_counter() - t0) * 1000
    q, a, rows = cap.calls[-1]
    return out, rows, q, a, ms


async def attempt(coro):
    """The awaited value, or the exception it raised (a tree without the
    column fails the check that needs it instead of ending the run)."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — reported by the check that reads it
        return e


async def plan_of(pool, q, a, generic=False):
    """Every node of the plan: (node type, relation or None, index or None,
    the parent's node type). `generic`: the plan without the arguments'
    values, which a prepared statement (asyncpg prepares every query) may
    switch to after five runs — a polled list runs the same statement over
    and over."""
    async with pool.acquire() as c:
        if generic:
            await c.execute("SET plan_cache_mode = force_generic_plan")
        try:
            raw = await c.fetchval("EXPLAIN (FORMAT JSON) " + q, *a)
        finally:
            if generic:
                await c.execute("RESET plan_cache_mode")
    out = []

    def walk(n, parent):
        out.append((n.get("Node Type"), n.get("Relation Name"), n.get("Index Name"), parent))
        for c in n.get("Plans") or []:
            walk(c, n.get("Node Type"))
    walk((json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"], None)
    return out


async def index_tables(pool):
    return {r["indexname"]: r["tablename"] for r in await pool.fetch(
        "SELECT indexname, tablename FROM pg_indexes WHERE schemaname = 'public' "
        "AND tablename IN ('entity_memory', 'entity_tasks')")}


async def check_plan(pool, label, q, a, want, one_entry=()):
    """`one_entry`: indexes the plan must read only under a Limit, one entry
    for each time it runs (MAX() over an index on its argument)."""
    of = await index_tables(pool)
    for how, generic in (("", False), (" (generic plan)", True)):
        nodes = await plan_of(pool, q, a, generic=generic)
        seq = [n for n in nodes if n[0] == "Seq Scan" and n[1] == "entity_memory"]
        on_memory = sorted({i for _t, _r, i, _p in nodes if i and of.get(i) == "entity_memory"})
        used = {i for _t, _r, i, _p in nodes if i}
        check(f"P {label}{how}: no sequential scan of entity_memory", not seq, seq)
        check(f"P {label}{how}: entity_memory is read only through the new indexes",
              bool(on_memory) and set(on_memory) <= {i for i, t in INDEXES.items() if t == "entity_memory"},
              on_memory)
        check(f"P {label}{how}: uses {', '.join(sorted(want))}", want <= used, sorted(used))
        for idx in one_entry:
            under = [p for _t, _r, i, p in nodes if i == idx]
            check(f"P {label}{how}: {idx} is read one entry a project (under a Limit), never every reply",
                  bool(under) and all(p == "Limit" for p in under), under)


async def buffers_by_read(pool, q, a):
    """Buffers (hit + read) of each read of entity_memory in the plan the
    query runs, keyed by its index (or node type), summed over its loops."""
    raw = await pool.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + q, *a)
    out: dict = {}

    def walk(n):
        if n.get("Relation Name") == "entity_memory":
            k = n.get("Index Name") or n.get("Node Type")
            out[k] = out.get(k, 0) + n.get("Shared Hit Blocks", 0) + n.get("Shared Read Blocks", 0)
        for c in n.get("Plans") or []:
            walk(c)
    walk((json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"])
    return out


async def _timed(one, runs=3):
    """Median of up to `runs` timings, stopping after a run already far over
    the ceiling."""
    t = []
    for _ in range(runs):
        t.append(await one())
        if t[-1] > 3 * CEILING_MS:
            break
    return statistics.median(t)


async def exec_ms(pool, q, a):
    """The server's own execution time (EXPLAIN ANALYZE), for the record."""
    async def one():
        raw = await pool.fetchval("EXPLAIN (ANALYZE, FORMAT JSON) " + q, *a)
        return (json.loads(raw) if isinstance(raw, str) else raw)[0]["Execution Time"]
    return await _timed(one)


async def wall_ms(fn):
    return await _timed(fn)


async def indexdefs(pool):
    return {r["indexname"]: r["indexdef"] for r in await pool.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY($1::text[])", list(INDEXES))}


async def valid(pool):
    return {r["relname"] for r in await pool.fetch(
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = ANY($1::text[]) AND i.indisvalid AND i.indisready", list(INDEXES))}


async def landed_column(pool):
    """entity_memory.landed_at as the catalog has it, or None."""
    r = await pool.fetchrow(
        "SELECT format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS notnull,"
        "       pg_get_expr(d.adbin, d.adrelid) AS dflt"
        "  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
        " WHERE a.attrelid = 'entity_memory'::regclass AND a.attname = 'landed_at' AND NOT a.attisdropped")
    return dict(r) if r else None


# ── the run ─────────────────────────────────────────────────────────────────

async def run(dsn):
    from server import brain_service as bs
    import server.routes.projects as P

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    # The unindexed computation, read without touching the schema: a pool
    # whose planner may not use an index, so the values the indexes must not
    # change are computed the way they were before v216.
    plain = await asyncpg.create_pool(dsn, min_size=1, max_size=1, server_settings={
        "enable_indexscan": "off", "enable_bitmapscan": "off", "enable_indexonlyscan": "off"})
    saved = (bs._pool, bs._entity_id)
    try:
        print("-- seeding")
        t0 = time.perf_counter()
        special = await seed(pool)
        n_turns = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'", E)
        n_proj = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn' "
                                     "AND project_id IS NOT NULL", E)
        n_tasks = await pool.fetchval("SELECT count(*) FROM entity_tasks WHERE entity_id = $1", E)
        print(f"   {n_turns} turn rows ({n_proj} in projects), {n_tasks} tasks, "
              f"{await pool.fetchval('SELECT count(*) FROM entity_memory')} memory rows in all, "
              f"{round(time.perf_counter() - t0, 1)} s")
        check("M seeded the Host the gate is about",
              n_turns == PROJECT_TURNS + CIRCLES_TURNS and n_proj == PROJECT_TURNS, (n_turns, n_proj))
        print("-- the unindexed computation")
        check("M the schema carries v216's three indexes", set(await indexdefs(pool)) == set(INDEXES),
              sorted(await indexdefs(pool)))
        act_sql = (f"SELECT p.id, {P._LAST_ACTIVITY_SQL} AS last_activity_at FROM projects p "
                   f"WHERE p.entity_id = $1 AND {P._PROJECTS_ONLY_SQL} "
                   "ORDER BY last_activity_at DESC NULLS LAST, p.id DESC")
        marks_sql = (f"WITH {P._PAGE_MARKS_CTES} SELECT p.id, {P._PAGE_MARKS_COLS} FROM projects p "
                     f"{P._PAGE_MARKS_JOINS} WHERE p.entity_id = $1 AND {P._PROJECTS_ONLY_SQL}")
        t0 = time.perf_counter()
        act_before = [(r["id"], r["last_activity_at"]) for r in await plain.fetch(act_sql, E)]
        act_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        marks_before = {r["id"]: dict(r) for r in await plain.fetch(marks_sql, E)}
        marks_ms = (time.perf_counter() - t0) * 1000
        s_before, s_empty_before = await read_search(P, plain), await read_search(P, plain, "")
        print(f"   last activity {round(act_ms)} ms, marks {round(marks_ms)} ms, "
              f"search {round(s_before[4])} ms (wall)")

        print("-- v216")
        if MIGRATION is None:
            check(f"M the tree carries migrations/v{VERSION}_*.sql", False, "no such file")
        else:
            file_before = await pool.fetchval("SELECT pg_relation_filenode('entity_memory')")
            landed = ("SELECT count(*) FILTER (WHERE landed_at IS NULL) AS unstamped, count(landed_at) AS stamped "
                      "FROM entity_memory")
            rows_before = tuple((await pool.fetchrow(landed)).values())
            t0 = time.perf_counter()
            r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
            print(f"   {MIGRATION.name} applied in {round(time.perf_counter() - t0, 2)} s on this table")
            check("M v216 runs again on a populated table", r1.returncode == 0, r1.stderr[-400:])
            col = await landed_column(pool)
            check("M ...and entity_memory.landed_at is timestamptz, nullable, no default",
                  col is not None and col["type"] == "timestamp with time zone"
                  and not col["notnull"] and col["dflt"] is None, col)
            rows_after = tuple((await pool.fetchrow(landed)).values())
            check("M ...touching no row: the table keeps its file and no landed_at is filled (no backfill at boot)",
                  await pool.fetchval("SELECT pg_relation_filenode('entity_memory')") == file_before
                  and rows_before == rows_after and rows_before[0] > 0, (rows_before, rows_after))
            first = await indexdefs(pool)
            check("M ...and leaves all three indexes, valid and ready",
                  set(first) == set(INDEXES) and await valid(pool) == set(INDEXES), (sorted(first), await valid(pool)))
            r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
            check("M a second run fails nothing and changes nothing",
                  r2.returncode == 0 and await indexdefs(pool) == first and await landed_column(pool) == col,
                  r2.stderr[-400:])

        # What a Host's autovacuum does after a write this size.
        await pool.execute("VACUUM entity_memory")

        print("-- values")
        after, lq, la, _ms = await read_list(P, bs, pool)
        act_after = [(r["id"], r["last_activity_at"]) for r in after]
        diff = [(b, x) for b, x in zip(act_before, act_after) if b != x]
        check("V the list: every row's last_activity_at, and the order, are the unindexed computation's",
              len(act_before) == len(act_after) == PROJECTS and not diff, diff[:3] or (len(act_before), len(after)))
        marks = ("last_landed_at", "waiting_on_you", "ask_cards_open")
        m_diff = [(r["id"], {k: (r[k], (marks_before.get(r["id"]) or {}).get(k)) for k in marks})
                  for r in after if any(r[k] != (marks_before.get(r["id"]) or {}).get(k) for k in marks)]
        check("V the list: every row's last_landed_at, waiting_on_you and ask_cards_open are the unindexed "
              "computation's",
              len(marks_before) == len(after) == PROJECTS and not m_diff, m_diff[:3] or len(marks_before))
        decided = await attempt(pool.fetchval(
            "SELECT count(*) FROM (SELECT project_id FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'"
            " AND project_id IS NOT NULL AND metadata->>'role' = 'assistant' GROUP BY project_id"
            " HAVING max(COALESCE(landed_at, created_at)) > max(created_at)) x", E))
        check("V ...and landed_at decides it: in most projects the last reply landed after every reply began",
              isinstance(decided, int) and decided > PROJECTS // 2, decided)
        s_after, s_empty_after = await read_search(P, pool), await read_search(P, pool, "")

        def ranked(s):
            return [(x["id"], x.get("score"), x["last_activity_at"]) for x in s[0]["projects"]]

        def sql_rows(s):
            return [(r["id"], r["lex_rank"], r["name_hit"], r["last_activity_at"]) for r in s[1]]
        check("V the search ranks the same projects in the same order with the same scores before and after v216 "
              "(a word, and the empty query's most recent first)",
              len(ranked(s_after)) >= 5 and ranked(s_before) == ranked(s_after)
              and len(ranked(s_empty_after)) >= 5 and ranked(s_empty_before) == ranked(s_empty_after)
              and sql_rows(s_before) == sql_rows(s_after),
              ([x for x, y in zip(ranked(s_before), ranked(s_after)) if x != y][:2], len(ranked(s_after))))
        by_id = dict(act_before)
        s_diff = [(r["id"], r["last_activity_at"], by_id.get(r["id"])) for r in s_after[1]
                  if r["last_activity_at"] != by_id.get(r["id"])]
        check("V the search: every project's last_activity_at is the unindexed list's",
              len(s_after[1]) == PROJECTS and not s_diff, s_diff[:3] or len(s_after[1]))
        row = {r["id"]: r for r in after}
        q = row[special["quiet"]]
        quiet_raw = await pool.fetchrow("SELECT created_at, updated_at FROM projects WHERE id = $1", special["quiet"])
        check("V a project with nothing in it: its own times, and nothing landed (another entity's rows never count)",
              q["last_activity_at"] == max(quiet_raw["created_at"], quiet_raw["updated_at"])
              and q["last_landed_at"] is None, (q["last_activity_at"], q["last_landed_at"]))
        tasks = {r["title"]: r for r in await pool.fetch(
            "SELECT title, created_at, started_at, completed_at FROM entity_tasks WHERE entity_id = $1 "
            "AND title IN ('done now', 'running', 'queued')", E)}
        check("V a task newer than every turn moves last_activity_at: finished (its completion), running (its "
              "start), queued (its creation)",
              row[special["task_done"]]["last_activity_at"] == tasks["done now"]["completed_at"]
              and row[special["task_running"]]["last_activity_at"] == tasks["running"]["started_at"]
              and row[special["task_queued"]]["last_activity_at"] == tasks["queued"]["created_at"],
              [row[special[k]]["last_activity_at"] for k in ("task_done", "task_running", "task_queued")])
        check("V ...and only the finished one is landed",
              row[special["task_done"]]["last_landed_at"] == tasks["done now"]["completed_at"]
              and row[special["task_queued"]]["last_landed_at"] is None,
              (row[special["task_done"]]["last_landed_at"], row[special["task_queued"]]["last_landed_at"]))
        other_at = await pool.fetchval("SELECT max(created_at) FROM entity_memory WHERE entity_id = $1", OTHER_E)
        check("V another entity's turn row and task in this entity's projects never count",
              row[special["busy"]]["last_activity_at"] < other_at
              and row[special["busy"]]["last_landed_at"] < other_at
              and row[special["busy_task"]]["last_activity_at"] < other_at, other_at)

        print("-- plans")
        list_want = set(INDEXES)
        search_want = {"idx_entity_memory_project_turns", "idx_entity_tasks_project"}
        sq, sa = s_after[2], s_after[3]
        await check_plan(pool, "the list, with the statistics from before v216", lq, la, list_want, (REPLIES_INDEX,))
        await check_plan(pool, "the search, with the statistics from before v216", sq, sa, search_want)
        await pool.execute("ANALYZE entity_memory")
        await pool.execute("ANALYZE entity_tasks")
        await check_plan(pool, "the list, after ANALYZE", lq, la, list_want, (REPLIES_INDEX,))
        await check_plan(pool, "the search, after ANALYZE", sq, sa, search_want)

        print("-- buffers")
        bufs = await buffers_by_read(pool, lq, la)
        print(f"   the list's reads of entity_memory: {bufs} buffers for {PROJECTS} projects")
        check(f"B each of the list's reads of entity_memory touches at most {BUFFERS_PER_PROJECT} buffers a project",
              {"idx_entity_memory_project_turns", REPLIES_INDEX} <= set(bufs)
              and all(v <= BUFFERS_PER_PROJECT * PROJECTS for v in bufs.values()), bufs)

        print("-- time (a backstop)")

        async def list_once():
            return (await read_list(P, bs, pool))[3]

        async def search_once():
            return (await read_search(P, pool))[4]
        l_ms, s_ms = await wall_ms(list_once), await wall_ms(search_once)
        print(f"   list: {round(l_ms, 1)} ms wall; server execution {round(await exec_ms(pool, lq, la), 1)} ms")
        print(f"   search: {round(s_ms, 1)} ms wall; server execution {round(await exec_ms(pool, sq, sa), 1)} ms")
        check(f"T the list answers in under {CEILING_MS} ms", l_ms < CEILING_MS, round(l_ms))
        check(f"T the search answers in under {CEILING_MS} ms", s_ms < CEILING_MS, round(s_ms))
    finally:
        bs._pool, bs._entity_id = saved
        await pool.close()
        await plain.close()


def main():
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv) as dsn:
        asyncio.run(run(dsn))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
