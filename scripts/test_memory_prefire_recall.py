#!/usr/bin/env python3
"""Smoke gate: per-turn pre-fire recall in build_memory_context (2026-08-04).

WHAT THIS RATCHETS:
  1. MESSAGE-SEMANTIC LEAD — with a query (the live user message), the memory
     block leads with a "### Relevant Right Now" section containing the
     memory that matches THIS turn, even when that memory would lose the
     standing importance/tier/recency ordering.
  2. NO DOUBLE PAY — a prefired memory is deduped out of the grouped
     standing sections; its content appears exactly once in the block.
  3. BACKWARD COMPAT — query="" renders no pre-fire section (session boot,
     test rigs, and any caller that never learned about the parameter).
  4. REAL EMPTY, NOT ERROR — an unrelated query (arms run, nothing matches)
     yields no section and no exception; the standing block still renders.
  5. WORKS WITHOUT EMBEDDINGS — this container's embed lane is down; the
     lexical arm alone must carry the pre-fire section.
  6. ONLY PROMPT TYPES RIDE — a fact matching the query as well as the
     preference does is never volunteered, in the pre-fire or the standing
     list (memory_engine.PROMPT_MEMORY_TYPES); recall still reaches it.

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

TEST_EID = 99323  # synthetic; never a real entity

# Distinctive tokens so lexical recall is deterministic.
HIT_TOKEN = "prefiretoken42"
MISS_TOKEN = "unrelatedtokenzz"
HIT_CONTENT = f"Prefers the {HIT_TOKEN} deploy gate to require a green smoke run first"
FACT_CONTENT = f"The {HIT_TOKEN} embeddings lane does not work"
OTHER_CONTENT = "Standing preference: dashboards over walls of text"


def _section(block: str, heading: str) -> str:
    """The text under `heading` up to the next ### heading — so a check that a
    row was PRE-FIRED cannot pass on the same row rendering in the standing
    list below it."""
    i = block.find(heading)
    if i == -1:
        return ""
    j = block.find("\n### ", i + len(heading))
    return block[i:] if j == -1 else block[i:j]


async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=2)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [prefire-recall] OK {name}")
        else:
            failures.append(name)
            print(f"  [prefire-recall] FAIL {name} {detail}")

    try:
        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'PrefireGate', 'standard') ON CONFLICT (id) DO NOTHING",
            TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)

        # The hit row: LOW importance (0.5 — would rank last in the standing
        # list) so the test proves the query, not importance, led the block.
        # A preference, so the row is ALSO eligible for the standing list
        # (dedup target).
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, "
            "importance) "
            "VALUES ($1, $2, 'preference', 'active', 0.5)",
            TEST_EID, HIT_CONTENT)
        # A task product (cited_fact) the query matches just as well, at HIGH
        # importance: it would lead both the pre-fire and the standing list
        # if types were not gated.
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, "
            "importance) "
            "VALUES ($1, $2, 'cited_fact', 'active', 2.0)",
            TEST_EID, FACT_CONTENT)
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, "
            "importance) "
            "VALUES ($1, $2, 'preference', 'active', 1.5)",
            TEST_EID, OTHER_CONTENT)

        from mastercode.memory_engine import MemoryEngine
        engine = MemoryEngine(pool)

        # 1+5. A query matching the low-importance row leads the block.
        block = await engine.build_memory_context(
            TEST_EID, {}, max_tokens=1500, query=f"what about {HIT_TOKEN}?")
        check("pre-fire section renders for a matching query",
              "### Relevant Right Now" in block, block[:200])
        check("matching low-importance memory is prefired",
              HIT_CONTENT in _section(block, "### Relevant Right Now"),
              _section(block, "### Relevant Right Now")[:300])
        sec_start = block.find("### Relevant Right Now")
        # Anchor on the PREFERENCES section: OTHER_CONTENT is a standing
        # preference that is not prefired, so the section always renders and
        # the ordering check cannot pass vacuously.
        standing_start = block.find("### Their Preferences")
        check("pre-fire section leads the standing sections",
              sec_start != -1 and standing_start != -1 and sec_start < standing_start,
              f"prefire={sec_start} standing={standing_start}")

        # 6. The matching, high-importance fact rides nowhere in the block.
        check("a matching fact is never volunteered",
              FACT_CONTENT not in block and "### Known Facts" not in block,
              block[:400])

        # 2. Dedup — the prefired content appears exactly once.
        check("prefired memory is not repeated in standing sections",
              block.count(HIT_CONTENT) == 1,
              f"count={block.count(HIT_CONTENT)}")

        # Standing (non-prefired) memory still renders.
        check("standing importance-ordered sections still render",
              OTHER_CONTENT in block)

        # 3. No query → no pre-fire section (backward compatible).
        block_noq = await engine.build_memory_context(
            TEST_EID, {}, max_tokens=1500)
        check("query='' renders no pre-fire section",
              "### Relevant Right Now" not in block_noq)
        check("query='' still renders the standing block",
              OTHER_CONTENT in block_noq)
        check("query='' standing list holds no fact",
              FACT_CONTENT not in block_noq)

        # 4. Unrelated query → real empty, no section, no error, standing
        # block intact.
        block_miss = await engine.build_memory_context(
            TEST_EID, {}, max_tokens=1500, query=MISS_TOKEN)
        check("unrelated query renders no pre-fire section",
              "### Relevant Right Now" not in block_miss)
        check("unrelated query keeps the standing block",
              OTHER_CONTENT in block_miss)

    finally:
        try:
            await pool.execute(
                "DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        except Exception:
            pass
        await pool.close()

    if failures:
        print(f"  [prefire-recall] {len(failures)} FAILURES: {failures}")
        return 1
    print("  [prefire-recall] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
