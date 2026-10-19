#!/usr/bin/env python3
"""Gate: the project index — POST /api/projects/search (v131).

The lexical arm is the floor
(core Postgres FTS + a name ILIKE boost, no extension); the semantic arm is
present ONLY when the host has an embedding model configured, and its
absence is a setup state the response reports as semantic:false — never an
outage (default-skills/embedding.md: there is no platform embedder).

Container-free by default: stub pools drive both the configured and the
unconfigured path through the real service. With
CSERIES_TEST_PG_HOST=<dir holding .s.PGSQL.5432> the plan's gate also runs on a
real database: ~30 seeded projects, three words from one project's
description rank it first, with the embedding route unconfigured the same
call still returns ranked rows and semantic:false, and with a (fake) lane
configured the stored embeddings lift the semantic match.

  APP_ROOT=<repo> python3 scripts/test_projects_search_route.py
  CSERIES_TEST_PG_HOST=/tmp/wbpg-sock python3 scripts/test_projects_search_route.py
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT), str(ROOT / "brain")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from server.routes import projects as P  # noqa: E402

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

# ── 1. the query builder ─────────────────────────────────────────────────────
check("every word is a prefix term, OR-joined, lowercased",
      P._search_tsquery("Trading soft-ware") == "'trading':* | 'soft':* | 'ware':*", P._search_tsquery("Trading soft-ware"))
check("punctuation-only text yields an empty query (the SQL skips the arm)", P._search_tsquery("?!") == "")
check("no extension is assumed: the SQL is core to_tsvector/to_tsquery/ts_rank_cd",
      "to_tsquery('english'" in P._SEARCH_ROWS_SQL and "ts_rank_cd(" in P._SEARCH_ROWS_SQL
      and "pg_trgm" not in P._SEARCH_ROWS_SQL and "similarity(" not in P._SEARCH_ROWS_SQL)
check("the document covers name (A), description (B) and phase titles (C)",
      "COALESCE(d.name, '')), 'A')" in P._SEARCH_ROWS_SQL and "COALESCE(d.description, '')), 'B')" in P._SEARCH_ROWS_SQL
      and "d.phase_titles), 'C')" in P._SEARCH_ROWS_SQL)
check("entity-scoped", "WHERE p.entity_id = $1" in P._SEARCH_ROWS_SQL)
_src = (ROOT / "server/routes/projects.py").read_text()
check("last_activity_at is the ONE expression list_projects uses (no second copy)",
      P._LAST_ACTIVITY_SQL in P._SEARCH_ROWS_SQL
      and "{_LAST_ACTIVITY_SQL} AS last_activity_at" in _src.split("async def list_projects")[1].split("return {")[0]
      and _src.count("t.result->>'project_id'") == 1)
init = (ROOT / "init-db.sql").read_text()
check("init-db.sql declares the search_embedding columns; v131 adds them idempotently",
      "search_embedding       REAL[]" in init
      and "ADD COLUMN IF NOT EXISTS search_embedding REAL[]" in (ROOT / "migrations/v131_project_search_embedding.sql").read_text())


# ── 2. stub pool: both paths through the real service ───────────────────────
def _row(i, name, desc, lex, *, name_hit=False, emb=None, emb_model=None, emb_hash=None,
         status="active", phases=""):
    return {"id": i, "name": name, "description": desc, "status": status, "folder_id": None,
            "repo": None, "created_at": NOW, "updated_at": NOW, "phase_count": 0,
            "phase_titles": phases, "last_activity_at": NOW - timedelta(minutes=i),
            "search_embedding": emb, "search_embedding_model": emb_model,
            "search_embedding_hash": emb_hash, "lex_rank": lex, "name_hit": name_hit}


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return self.rows

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "UPDATE 1"


# Fixtures carry the lexical rank the SQL would have computed for the query
# under test ("trading"): the engine's strongest hit, a name-only hit, a miss.
ROWS = [
    _row(1, "Trading software", "backtesting engine for equities strategies", 0.4, name_hit=True),
    _row(2, "Weather app", "forecast dashboard with radar tiles", 0.0),
    _row(3, "Trading journal", "log every trade with tags", 0.1, name_hit=True),
    _row(4, "Finance month view", "monthly budget tracker", 0.0, status="archived"),
]
# The same projects for a query ("radar forecast") with NO lexical hit anywhere.
ROWS_NO_LEX = [dict(r, lex_rank=0.0, name_hit=False) for r in ROWS]


async def _stub():
    # Unconfigured: lexical only, semantic:false, ranked, floor applied.
    pool = _Pool(ROWS)
    r = await P.search_projects_service(pool, 3, "trading", limit=5, lane=(None, ""))
    check("unconfigured lane → semantic:false and the lexical ranking stands (rank, then name boost)",
          r["semantic"] is False and [p["id"] for p in r["projects"]] == [1, 3]
          and r["projects"][0]["score"] > r["projects"][1]["score"], repr(r))
    q, args = pool.calls[0]
    check("the bound params are (entity, tsquery, ilike, candidate cap)",
          args == (3, "'trading':*", "%trading%", P._SEARCH_CANDIDATE_CAP), repr(args))
    # The cap is what stops a keystroke dragging the pod's DB: every row carries
    # a REAL[] embedding, and this runs once per keystroke past a 200 ms debounce
    # against a postgres at max_connections=16. Assert the SQL is bounded AND
    # ordered — a LIMIT without that ORDER BY would silently drop rows that
    # should have scored, turning a performance fix into a correctness bug.
    check("the candidate set is bounded and ordered so the cap cannot drop a winner",
          "LIMIT $4" in q and "ORDER BY lex_rank DESC" in q, q[-300:])
    check("rows carry what the picker renders: group, bucket_id, phase_count, last_activity_at, score",
          set(r["projects"][0]) >= {"id", "name", "description", "status", "group", "bucket_id",
                                    "last_activity_at", "phase_count", "score"}, repr(r["projects"][0]))
    check("no refresh is scheduled without a model", not any("UPDATE projects" in q for q, _ in pool.calls))

    # Empty query: the "before you type" state — recent, non-archived, score null.
    r0 = await P.search_projects_service(_Pool(ROWS), 3, "   ", limit=5, lane=(None, ""))
    check("empty query → recent non-archived projects, score null, semantic:false",
          [p["id"] for p in r0["projects"]] == [1, 2, 3] and r0["projects"][0]["score"] is None
          and r0["semantic"] is False, repr(r0))

    # Configured: a fake lane; the stored embedding lifts row 2 (no lexical hit at all).
    def vec(text):
        # deterministic bag-of-words vector over a fixed vocabulary
        vocab = ["weather", "forecast", "radar", "trading", "trade", "budget"]
        return [1.0 if w in text.lower() else 0.0 for w in vocab]

    async def embed(text):
        return vec(text)

    model = "fake-embed-v1"
    rows = [dict(x) for x in ROWS_NO_LEX]
    rows[1]["search_embedding"] = vec("weather forecast radar")
    rows[1]["search_embedding_model"] = model
    rows[1]["search_embedding_hash"] = P._search_hash(model, P._search_text_of(rows[1]["name"], rows[1]["description"], ""))
    pool = _Pool(rows)
    r = await P.search_projects_service(pool, 3, "radar forecast", limit=5, lane=(embed, model),
                                        refresh_in_background=False)
    check("configured lane → semantic:true and the embedded match is found with no lexical hit",
          r["semantic"] is True and [p["id"] for p in r["projects"]] == [2], repr(r))

    # A stored vector from ANOTHER model is never compared (dims may differ) and is stale.
    rows[1]["search_embedding_model"] = "older-model"
    r = await P.search_projects_service(_Pool(rows), 3, "radar forecast", limit=5, lane=(embed, model),
                                        refresh_in_background=False)
    check("a vector stored under another model is ignored (stale), not compared",
          r["semantic"] is True and r["projects"] == [], repr(r))

    # The refresh writes the vector + model + hash, entity-scoped, and stops when the lane goes quiet.
    seen = []

    async def embed_then_quiet(text):
        seen.append(text)
        return vec(text) if len(seen) == 1 else None

    pool = _Pool([])
    stale = [(2, "Weather app\nforecast dashboard with radar tiles", "h2"), (3, "Trading journal", "h3")]
    n = await P._refresh_project_embeddings(pool, 3, embed_then_quiet, model, stale)
    upd = [c for c in pool.calls if "UPDATE projects" in c[0]]
    check("refresh stores vector + model + hash for the row it embedded and stops when the lane returns None",
          n == 1 and len(upd) == 1 and upd[0][1][:2] == (2, 3) and upd[0][1][3:] == (model, "h2")
          and "search_embedding = $3::real[]" in upd[0][0] and "entity_id = $2" in upd[0][0], repr((n, upd)))

    # The lane resolver never raises and reports unconfigured as (None, "").
    class _Dead:
        async def fetchrow(self, *a):
            raise RuntimeError("db down")

    lane = await P._search_embedding_lane(_Dead(), 3)
    check("an unreadable/unconfigured lane resolves to (None, '') — absent, never an error", lane == (None, ""))


asyncio.run(_stub())


# ── 3. the plan's gate on a real database ────────────────────────────────────
SEED = [
    ("Trading software", "backtesting engine for equities strategies with walk-forward validation"),
    ("Weather app", "forecast dashboard with radar tiles and hourly alerts"),
    ("Finance month view", "monthly budget tracker with envelope categories"),
    ("Recipe box", "family recipes with grocery list generation"),
    ("Garden planner", "raised bed layouts and planting calendar"),
    ("Podcast pipeline", "transcribe episodes and publish show notes"),
    ("Client portal", "invoices and shared documents for consulting clients"),
    ("Home inventory", "photograph every item with serial numbers for insurance"),
    ("Chess trainer", "spaced repetition of opening lines"),
    ("Book club", "reading schedule and discussion prompts"),
    ("Marathon plan", "sixteen week training schedule with pace targets"),
    ("Wine cellar", "bottle tracking with drink-by windows"),
    ("Tenant ledger", "rent receipts and maintenance requests per unit"),
    ("Sourdough log", "starter feeding times and bake outcomes"),
    ("Kids chores", "weekly chore rotation with allowance"),
    ("Language drills", "daily spanish vocabulary with audio"),
    ("Photo archive", "deduplicate and tag two decades of photos"),
    ("Car maintenance", "service intervals and receipts per vehicle"),
    ("Volunteer roster", "shift signups for the food bank"),
    ("Band setlists", "song keys and rehearsal notes"),
    ("Thesis notes", "literature review cards with citations"),
    ("Startup CRM", "pipeline stages for early customers"),
    ("Camping gear", "packing lists by trip type"),
    ("Meal prep", "weekly menus with macro totals"),
    ("Ski season", "resort days and pass usage"),
    ("Pet health", "vet visits and vaccination reminders"),
    ("Woodshop", "cut lists and lumber inventory"),
    ("Newsletter", "issue drafts and subscriber growth"),
    ("Aquarium log", "water parameters and stocking plan"),
    ("Board game nights", "who won what and house rules"),
]


async def _db_gate(host: str):
    import asyncpg
    pool = await asyncpg.create_pool(host=host, user=os.environ.get("CSERIES_TEST_PG_USER", "entity"),
                                     database=os.environ.get("CSERIES_TEST_PG_DB", "hostmem"),
                                     min_size=1, max_size=2)
    eid = 990_131
    try:
        await pool.execute("DELETE FROM projects WHERE entity_id = $1", eid)
        ids = {}
        for name, desc in SEED:
            ids[name] = await pool.fetchval(
                "INSERT INTO projects (entity_id, name, description, status) VALUES ($1, $2, $3, 'active') RETURNING id",
                eid, name, desc)
        # A phase title is part of the document too.
        await pool.execute(
            "INSERT INTO project_phases (project_id, phase_number, title) VALUES ($1, 1, 'walk-forward harness')",
            ids["Trading software"])

        r = await P.search_projects_service(pool, eid, "walk-forward validation equities", limit=5, lane=(None, ""))
        check("db: three words from one description rank that project FIRST (lexical only)",
              r["projects"] and r["projects"][0]["id"] == ids["Trading software"], repr(r["projects"][:3]))
        check("db: with embeddings unconfigured the call still returns ranked rows and semantic:false",
              r["semantic"] is False and all(p["score"] is not None for p in r["projects"]), repr(r))
        r = await P.search_projects_service(pool, eid, "chore rotat", limit=5, lane=(None, ""))
        check("db: partial words (prefix terms) still match", r["projects"] and r["projects"][0]["id"] == ids["Kids chores"], repr(r["projects"][:2]))
        r = await P.search_projects_service(pool, eid, "harness", limit=5, lane=(None, ""))
        check("db: a phase title is searchable", r["projects"] and r["projects"][0]["id"] == ids["Trading software"], repr(r["projects"][:2]))
        r = await P.search_projects_service(pool, eid, "wine", limit=5, lane=(None, ""))
        check("db: a name hit outranks a mere description hit", r["projects"] and r["projects"][0]["id"] == ids["Wine cellar"], repr(r["projects"][:2]))
        r = await P.search_projects_service(pool, eid, "zzzz qqqq", limit=5, lane=(None, ""))
        check("db: nothing matches → empty list, not an error", r["projects"] == [] and r["semantic"] is False)
        r = await P.search_projects_service(pool, eid, "", limit=5, lane=(None, ""))
        check("db: empty query → 5 recent projects", len(r["projects"]) == 5 and r["projects"][0]["score"] is None)

        # A fake configured lane: character-trigram vectors, deterministic, no network.
        def vec(text):
            t = re.sub(r"\s+", " ", text.lower())
            v = [0.0] * 256
            for i in range(len(t) - 2):
                v[int(hashlib.md5(t[i:i + 3].encode()).hexdigest(), 16) % 256] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            return [x / n for x in v]

        async def embed(text):
            return vec(text)

        model = "fake-trigram-256"
        r1 = await P.search_projects_service(pool, eid, "aquarium", limit=5, lane=(embed, model), refresh_in_background=False)
        check("db: first configured search answers from the lexical arm while nothing is embedded yet",
              r1["semantic"] is True and r1["projects"] and r1["projects"][0]["id"] == ids["Aquarium log"], repr(r1["projects"][:2]))
        rows = await pool.fetch(P._SEARCH_ROWS_SQL, eid, "", "", P._SEARCH_CANDIDATE_CAP)
        stale = [(x["id"], P._search_text_of(x["name"], x["description"], x["phase_titles"]),
                  P._search_hash(model, P._search_text_of(x["name"], x["description"], x["phase_titles"]))) for x in rows]
        n = await P._refresh_project_embeddings(pool, eid, embed, model, stale)
        check("db: the background refresh embeds the whole set (30 ≤ one batch of 40)", n == 30, str(n))
        stored = await pool.fetchrow(
            "SELECT array_length(search_embedding, 1) AS dim, search_embedding_model FROM projects WHERE id = $1",
            ids["Aquarium log"])
        check("db: the vector is stored as REAL[] at the model's dim with the model stamp",
              stored["dim"] == 256 and stored["search_embedding_model"] == model, repr(dict(stored)))
        r2 = await P.search_projects_service(pool, eid, "water parameters stocking", limit=5,
                                             lane=(embed, model), refresh_in_background=False)
        check("db: with the lane configured and vectors stored, the match ranks first and semantic:true",
              r2["semantic"] is True and r2["projects"] and r2["projects"][0]["id"] == ids["Aquarium log"], repr(r2["projects"][:2]))
        check("db: a second search over fresh vectors finds nothing stale",
              not [x for x in await pool.fetch(P._SEARCH_ROWS_SQL, eid, "", "", P._SEARCH_CANDIDATE_CAP)
                   if x["search_embedding_hash"] != P._search_hash(model, P._search_text_of(x["name"], x["description"], x["phase_titles"]))])
    finally:
        await pool.execute("DELETE FROM projects WHERE entity_id = $1", eid)
        await pool.close()


pg_host = os.environ.get("CSERIES_TEST_PG_HOST")
if pg_host:
    asyncio.run(_db_gate(pg_host))
else:
    print("  skip  real-database gate (set CSERIES_TEST_PG_HOST to run it)")

print("\nPASS projects_search_route" if not fails else f"\nFAIL projects_search_route: {fails} check(s)")
sys.exit(1 if fails else 0)
