#!/usr/bin/env python3
"""The hold's bounds and its records, against a REAL database (5j's query
runs against a real database, not a stub pool).

Needs a Postgres with the kernel's schema — init-db.sql and every
migrations/v*.sql applied, as the kernel's entrypoint applies them — reached
through SMOKE_PG_HOST (a socket directory or host; SMOKE_PG_PORT / USER /
PASSWORD / DB as test_pipeline_db.py reads them). SKIPS when SMOKE_PG_HOST is
unset. Everything it writes is in one transaction that is rolled back.

  1. tools.phase_repair_refusal's count: a phase's landings that did not
     complete — 'review' and failed — count; a refused acceptance, a split
     conflict, another phase, another project and a completed landing do not;
     a success never resets it; only a woken turn is refused, at the third.
  2. memory_engine.write_turn_row_by_token: the held row is written once
     and rewritten in place (one row, held cleared, created_at kept). (The turn
     consolidator's queue it was kept out of is gone.)
  3. The success-measure queries in docs/host-orchestrator-turn.md run against
     entity_memory as it is, and count what they say they count.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
sys.path.insert(0, APP_ROOT)

FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        if extra:
            print(f"        {extra}")
        FAILS.append(name)


class _Rollback(Exception):
    pass


class ConnPool:
    """One connection inside one transaction, shaped like the pool the code
    under test calls (fetch / fetchrow / fetchval / execute)."""

    def __init__(self, conn):
        self.c = conn

    async def fetch(self, q, *a):
        return await self.c.fetch(q, *a)

    async def fetchrow(self, q, *a):
        return await self.c.fetchrow(q, *a)

    async def fetchval(self, q, *a):
        return await self.c.fetchval(q, *a)

    async def execute(self, q, *a):
        return await self.c.execute(q, *a)


async def main() -> int:
    import asyncpg
    conn = await asyncpg.connect(
        host=os.environ["SMOKE_PG_HOST"], port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"))
    try:
        tr = conn.transaction()
        await tr.start()
        try:
            await _run(conn)
        finally:
            await tr.rollback()
    finally:
        await conn.close()
    return 0


async def _run(conn):
    from mastercode.runtime import tools as T
    from mastercode import memory_engine as ME
    pool = ConnPool(conn)
    eid = await conn.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
    project, phase = 251, 77

    async def landing(status, phase_status=None, *, ph=phase, proj=project, **extra):
        res = {"project_id": proj, "phase_id": ph, **({"phase_status": phase_status} if phase_status else {}), **extra}
        await conn.execute(
            "INSERT INTO entity_tasks (entity_id, title, task_type, status, result) "
            "VALUES ($1, 'build phase 2', 'factory_build', $2, $3::jsonb)",
            eid, status, json.dumps(res))

    prow = {"id": phase, "phase_number": 2, "title": "Tin Robot"}

    async def refused(woken=True):
        tok = ME.CURRENT_TURN_WOKEN.set({"session_id": "s", "woken": woken})
        try:
            return await T.phase_repair_refusal(pool, eid, project, prow)
        finally:
            ME.CURRENT_TURN_WOKEN.reset(tok)

    # ── 1. the phase's automatic repairs ──────────────────────────────────
    await landing("completed", "review")              # the first build: blocked
    await landing("completed", "review")              # repair 1: blocked again
    # what never counts
    await landing("failed", acceptance_refused=True)  # refused at the door: nothing built
    await landing("failed", integration_conflict={"files": []})  # the split failed, not the coder
    await landing("completed", "review", ph=78)       # another phase
    await landing("completed", "review", proj=252)    # another project
    await landing("completed", "complete")            # a landing that completed
    check("two landings that did not complete leave a repair (woken turn)", await refused() is None)
    await landing("failed")                           # repair 2 failed: the third
    r = await refused()
    check("the third (a failed one counts too) spends them: the woken turn is refused, naming the box",
          r is not None and "automatic repairs are spent" in r["result"] and "(2 in review, 1 failed)" in r["result"]
          and "question box" in r["result"], r)
    check("a person asking is never refused for it", await refused(woken=False) is None)
    await landing("completed", "complete")            # a success later: no reset
    check("a success never resets the count", await refused() is not None)

    # ── 2. the held row ───────────────────────────────────────────────────
    began = datetime.now(timezone.utc) - timedelta(seconds=30)
    tok = "tok-held-db"
    rid = await ME.write_turn_row_by_token(
        pool, eid, "sess-db", "assistant", "Elm Lantern is building — task 420.",
        token=tok, held=True, project_id=project,
        tags={"app_id": "workbench", "close_turn": {"handoff": {"next_step": "x"}}}, created_at=began)
    row = await conn.fetchrow("SELECT id, content, metadata, created_at FROM entity_memory WHERE id = $1", rid)
    md = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
    check("the reply as it stands is written before the hold, held, with its handoff",
          row is not None and md.get("held") is True and md.get("turn_token") == tok
          and md.get("close_turn", {}).get("handoff") == {"next_step": "x"}, dict(row) if row else None)
    rid2 = await ME.write_turn_row_by_token(
        pool, eid, "sess-db", "assistant", "Elm Lantern is building — task 420.\n\nEmber Wake landed.",
        token=tok, held=False, project_id=project, tags={"app_id": "workbench", "go_ahead": {"final_asks": None}},
        created_at=datetime.now(timezone.utc))
    rows = await conn.fetch("SELECT id, content, metadata, created_at FROM entity_memory "
                            "WHERE metadata->>'turn_token' = $1", tok)
    md2 = json.loads(rows[0]["metadata"]) if rows and isinstance(rows[0]["metadata"], str) else (rows[0]["metadata"] if rows else {})
    check("the end rewrites the SAME row in place: one row, final text, held cleared",
          rid2 == rid and len(rows) == 1 and rows[0]["content"].endswith("Elm Lantern landed.")
          and "held" not in md2 and "go_ahead" in md2, [dict(r) for r in rows])
    check("...its created_at stays when the reply began",
          abs((rows[0]["created_at"] - began).total_seconds()) < 1, rows[0]["created_at"] if rows else None)

    # ── 3. the documented success-measure queries ─────────────────────────
    doc = open(os.path.join(APP_ROOT, "docs/host-orchestrator-turn.md")).read()
    blocks = [b.split("```", 1)[0] for b in doc.split("```sql")[1:]]

    def _block(marker):
        """The doc's SQL block that opens with this comment line."""
        return next(b for b in blocks if marker in b)
    block = _block("-- per day: Host replies")
    q1, q2 = [q.strip() for q in block.split(";") if q.strip().upper().count("SELECT")][:2]
    samples = [
        {"final_asks": None, "final_level": None, "asked_in_box": False},              # asks nothing
        {"final_asks": "asks for a reply (c)", "final_level": "core", "asked_in_box": False,
         "fired": [{"outcome": "unrepaired", "level": "core", "where": "reply"}]},      # still asks, core
        {"final_asks": "makes a Host step wait on the user (b)", "final_level": "explicit",
         "asked_in_box": False,
         "fired": [{"outcome": "unrepaired", "level": "explicit", "where": "reply"}]},  # still asks, explicit
        {"final_asks": "makes a Host step wait on the user (b)", "final_level": "judged",
         "asked_in_box": False,
         "fired": [{"outcome": "not_an_ask", "level": "judged", "where": "reply"}]},    # still asks, not_an_ask
        {"final_asks": None, "final_level": None, "asked_in_box": True,
         "fired": [{"outcome": "escalated", "level": "core", "where": "reply", "ticket_id": 41}]},
        {"final_asks": None, "final_level": None, "asked_in_box": False,
         "fired": [{"outcome": "unrepaired", "level": "core", "where": "reply"},
                   {"outcome": "work_started", "level": "explicit", "where": "reply",
                    "task_ids": [420]}]},                                                # judged on the LAST
        {"final_asks": None, "final_level": None, "asked_in_box": False,
         "fired": [{"outcome": "rewrote", "level": None, "where": "handoff"}]},          # a handoff wait
    ]
    await conn.execute("DELETE FROM entity_memory WHERE metadata ? 'go_ahead'")
    for g in samples:
        await conn.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata) "
            "VALUES ($1, 'reply', 'turn', 0.5, 'active', $2::jsonb)",
            eid, json.dumps({"role": "assistant", "go_ahead": g}))
    day = await conn.fetch(q1)
    check("the daily query: replies, still asking (any / explicit incl. core / core), fired, last not_an_ask",
          len(day) == 1 and (day[0]["replies"], day[0]["still_asks"], day[0]["still_explicit"],
                             day[0]["still_core"], day[0]["guard_fired"],
                             day[0]["last_not_an_ask"]) == (7, 3, 2, 1, 6, 1), [dict(r) for r in day])
    by = {(r["last_outcome"], r["last_level"]): (r["turns"], r["still_asks"]) for r in await conn.fetch(q2)}
    check("the by-last-firing query judges each turn on its LAST firing, level beside it, a handoff as such",
          by == {("unrepaired", "core"): (1, 1), ("unrepaired", "explicit"): (1, 1),
                 ("not_an_ask", "judged"): (1, 1), ("escalated", "core"): (1, 0),
                 ("work_started", "explicit"): (1, 0), ("rewrote", "handoff"): (1, 0)}, by)

    # ── 4. the documented tool-prose measure (context_ledger.reply) ────────
    q3 = [q.strip() for q in _block("-- per day: tool turns").split(";") if "SELECT" in q.upper()][0]
    await conn.execute("DELETE FROM entity_memory WHERE metadata ? 'context_ledger'")
    for prose, chars, tool in ((0, 300, True), (120, 900, True), (400, 1500, True), (0, 200, False)):
        md = {"role": "assistant", "context_ledger": {"reply": {"chars": chars, "tool_prose_parts": 1 if prose else 0,
                                                                "tool_prose_chars": prose}}}
        if tool:
            md["tool_calls"] = [{"tool": "build"}]
        await conn.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata) "
            "VALUES ($1, 'reply', 'turn', 0.5, 'active', $2::jsonb)", eid, json.dumps(md))
    tp = await conn.fetch(q3)
    check("the tool-prose query: tool turns per day, the median prose beside tool calls and reply length",
          len(tp) == 1 and tp[0]["tool_turns"] == 3 and float(tp[0]["median_tool_prose_chars"]) == 120.0
          and float(tp[0]["median_reply_chars"]) == 900.0, [dict(r) for r in tp])

if __name__ == "__main__":
    if not os.environ.get("SMOKE_PG_HOST"):
        print("SKIP — SMOKE_PG_HOST is unset (needs a Postgres with the kernel's schema)")
        sys.exit(0)
    asyncio.run(main())
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("all checks passed against a real database")
