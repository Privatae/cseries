#!/usr/bin/env python3
"""Gate: the final-shape migrations (v235-v238) bring a Host that HOLDS DATA
forward, not only a fresh database.

Found 2026-10-17 by running the boot section on a copy of a Host's data:
v235 retyped the rows of retired memory types to 'retired' while the OLD
memory_type CHECK, which does not allow 'retired', was still in place, so the
migration failed on every Host holding such a row. Every other gate builds a
fresh schema, where no such row exists, and passed. This gate builds the
schema as it stood just BEFORE v235 (kernel_schema upto=235), writes the rows
a lived-in Host has, then runs the boot section over the current tree, as an
upgrading Host's boot does (kernel_schema.complete), and checks:

  - the boot section applies v235-v238 on that data;
  - a row of each retired type the old CHECK allowed becomes memory_type
    'retired', keeps its old type in metadata.prior_memory_type, and is
    archived if it was active (a row already archived stays archived);
  - a taught skill_memory rule becomes a preference marked rule_kind 'taught';
  - a constitutional values-pack copy is archived (v235), the platform's own
    entity-0 values rows are archived (v236: they are code now), and a Host's
    own constitutional row becomes a preference rule (v236);
  - turns and preferences are untouched;
  - the new CHECK refuses a retired type, and the dropped columns are gone;
  - v235 runs a second time without error.

    APP_ROOT=<repo> python3 scripts/test_one_memory_migration_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import _psql, complete, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove v235-v238 on a Host's data.")
    sys.exit(2)

FAILED = 0
E = 1
V235 = ROOT / "migrations" / "v235_one_memory.sql"
RETIRED = ("fact", "decision", "topic", "emotion", "unresolved", "knowledge",
           "skill_memory", "moral_precedent", "legacy")


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


async def seed(dsn) -> dict:
    """The rows a lived-in Host holds, written into the schema before v235.
    A type the old CHECK refuses is skipped (the old schema never held it)."""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    ids: dict = {}

    async def add(key, eid, content, mtype, status="active", meta=None):
        try:
            ids[key] = await pool.fetchval(
                "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata)"
                " VALUES ($1, $2, $3, 1.0, $4, $5::jsonb) RETURNING id",
                eid, content, mtype, status, json.dumps(meta or {}))
        except asyncpg.CheckViolationError:
            ids[key] = None

    try:
        await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, 'gate host')"
                           " ON CONFLICT (id) DO NOTHING", E)
        for t in RETIRED:
            await add(f"active:{t}", E, f"an active {t} row", t)
        await add("archived:fact", E, "an archived fact row", "fact", status="archived")
        await add("taught_rule", E, "always sign as Alex", "skill_memory",
                  meta={"trigger": "remember this:"})
        await add("pack", E, "a values-pack copy", "constitutional",
                  meta={"origin": "constitutional-pack-v1"})
        await add("own_const", E, "a rule this Host was given", "constitutional")
        await add("platform", 0, "a platform value row", "constitutional",
                  meta={"origin": "platform-guardrails-v1"})
        await add("turn", E, "a turn of the conversation", "turn")
        await add("pref", E, "the person likes short answers", "preference")
    finally:
        await pool.close()
    return ids


async def verify(dsn, ids: dict) -> None:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        led = {r["version"] for r in await pool.fetch("SELECT version FROM schema_migrations")}
        check("the boot section applied v235, v236, v237 and v238 on a Host with data",
              {235, 236, 237, 238} <= led, sorted(v for v in led if v >= 230))

        async def row(key):
            i = ids.get(key)
            return None if i is None else await pool.fetchrow(
                "SELECT memory_type, status, metadata FROM entity_memory WHERE id = $1", i)

        def md(r):
            m = r["metadata"]
            return json.loads(m) if isinstance(m, str) else (m or {})

        seeded = [t for t in RETIRED if ids.get(f"active:{t}") is not None]
        check("the old schema took rows of the retired types (the case this gate exists for)",
              len(seeded) >= 5, seeded)
        for t in seeded:
            r = await row(f"active:{t}")
            check(f"an active {t} row is retired, archived, and keeps its old type",
                  r and r["memory_type"] == "retired" and r["status"] == "archived"
                  and md(r).get("prior_memory_type") == t, dict(r) if r else None)
        r = await row("archived:fact")
        check("an already-archived fact row is retired and stays archived",
              r and r["memory_type"] == "retired" and r["status"] == "archived", dict(r) if r else None)
        r = await row("taught_rule")
        check("a taught skill_memory rule becomes a preference marked rule_kind 'taught'",
              r and r["memory_type"] == "preference" and r["status"] == "active"
              and md(r).get("rule_kind") == "taught", dict(r) if r else None)
        r = await row("pack")
        check("a values-pack copy is archived", r and r["status"] == "archived", dict(r) if r else None)
        r = await row("own_const")
        check("a Host's own constitutional row becomes a preference rule (v236)",
              r and r["memory_type"] == "preference" and r["status"] == "active"
              and md(r).get("rule_kind") == "taught"
              and md(r).get("prior_memory_type") == "constitutional", dict(r) if r else None)
        live0 = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = 0"
                                    " AND memory_type = 'constitutional' AND status = 'active'")
        check("no platform value row of entity 0 stays active (they are code now, v236)", live0 == 0, live0)
        for key, t in (("turn", "turn"), ("pref", "preference")):
            r = await row(key)
            check(f"a {t} row is untouched", r and r["memory_type"] == t and r["status"] == "active",
                  dict(r) if r else None)
        try:
            await pool.execute("INSERT INTO entity_memory (entity_id, content, memory_type)"
                               " VALUES ($1, 'x', 'fact')", E)
            refused = False
        except asyncpg.CheckViolationError:
            refused = True
        check("the new CHECK refuses a retired type", refused)
        cols = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'entity_memory'")}
        gone = {"dimension_vector", "dimension_scores", "episode_id", "extracted_at", "graph_extracted_at"}
        check("the dropped columns are gone", not (cols & gone), sorted(cols & gone))
        left = [r["nspname"] for r in await pool.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname IN ('ag_catalog', 'cseries_world')")]
        age = await pool.fetchval("SELECT count(*) FROM pg_extension WHERE extname = 'age'")
        check("the graph extension and the schemas it left are gone", not left and age == 0, (left, age))
    finally:
        await pool.close()
    again = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(V235))
    check("v235 runs a second time without error", again.returncode == 0, again.stderr[-400:])


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, upto=235, entity_id=E) as _db:
    _ids = asyncio.run(seed(_db))
    try:
        complete(_db)
        _ran = True
    except Exception as e:  # noqa: BLE001 — the boot section failing IS the finding
        _ran = False
        check("the boot section applies v235-v238 on a Host with data", False, str(e)[-1200:])
    if _ran:
        asyncio.run(verify(_db, _ids))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
