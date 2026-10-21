#!/usr/bin/env python3
"""Smoke gate: what the per-turn block pushes, and what `recall` reaches, for
documents and captured tool results (conversation depth).

The context janitor this gate was named for (a separate model call that kept,
rewrote or dismissed a thread's captured tool results) is retired with the
rolling summary it rode beside: no model outside the Host's turn writes what
the Host reads. A Host keeps the captured rows it has; nothing writes new ones.

WHAT THIS RATCHETS:
  1. RETIRED — the summary writer and its janitor do not ship and nothing
     imports them.
  2. PRE-FIRE vs RECALL — build_memory_context pre-fires the matching
     preference under "### Relevant Right Now" and pushes no document: the
     user's document and a kept captured tool result stay out of the per-turn
     block (the pre-fire reads memory only). `recall`'s own call
     (memory_retrieval.unified_search, rendered by format_unified_text)
     reaches both through the documents store; a dismissed captured row is
     reached by neither; and an emptied documents store answers "no
     matches", never UNAVAILABLE, with the memory pre-fire intact.

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

TEST_EID = 99326  # synthetic; never a real entity
SRC = {"kind": "app", "ref": "circles"}
SESSION = "janitor-gate-session"
TURN_ID = f"{SESSION}:7"

# Distinctive tokens so lexical recall is deterministic (the in-container
# embed lane is down — the lexical arms carry every search here).
DROP_TOKEN = "droptoken23"
COND_TOKEN = "condensedtoken29"
MEM_TOKEN = "memtoken31"
QUERY_TOKEN = "janquery37"  # shared by the memory row and the doc rows
DOC_TOKEN = "doctoken41"

BIG_DROP = f"directory listing {DROP_TOKEN}: bin etc usr var " + "x " * 80
CONDENSED = f"CONDENSED {COND_TOKEN} ({QUERY_TOKEN}): gate = green smoke + signed manifest"


def _section(block: str, heading: str, nxt: str = "\n### ") -> str:
    """The text under `heading` up to the next heading (`nxt`) — so a check
    that a row was PRE-FIRED cannot pass on the same row rendering in the
    standing list below it, and a check on recall's documents store cannot
    pass on the same words in its memory store."""
    i = block.find(heading)
    if i == -1:
        return ""
    j = block.find(nxt, i + len(heading))
    return block[i:] if j == -1 else block[i:j]


DOCS_HEADING = "## The user's documents"


async def _recall_text(pool, entity_id: int, query: str) -> str:
    """The answer the Host's `recall(query)` returns: the verb's own call
    (session.py, the recall branch: unified_search with every store, limit
    10, offset 0, no turn type) rendered by format_unified_text."""
    from mastercode.memory_retrieval import format_unified_text, unified_search
    out = await unified_search(pool, entity_id, query, stores=None,
                               limit=10, offset=0, context=None)
    return format_unified_text(out)

async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=2)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [context-janitor] OK {name}")
        else:
            failures.append(name)
            print(f"  [context-janitor] FAIL {name} {detail}")

    from server import brain_service
    _saved_pool = brain_service._pool
    brain_service._pool = pool  # embed.enqueue_job resolves its pool here

    try:
        import os
        from server.routes.context import search_context_service

        # ── 1. Retired ───────────────────────────────────────────────
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        check("the summary writer and its janitor do not ship",
              not os.path.exists(os.path.join(base, "brain/mastercode/thread_consolidator.py")))
        try:
            __import__("mastercode." + "thread_" + "consolidator")
            gone = False
        except ImportError:
            gone = True
        check("...and nothing can import them", gone)

        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) VALUES ($1, 'JanitorGate', 'standard') "
            "ON CONFLICT (id) DO NOTHING", TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM user_context WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM embed_jobs WHERE entity_id=$1", TEST_EID)

        # Two captured tool results in the shape the retired capture wrote
        # (through the one staging writer, as it did): one its janitor kept
        # (condensed), one it dismissed.
        from server.routes.context import stage_context_service as _stage

        async def captured(tool, content, status, verdict):
            await _stage(pool, TEST_EID, source=f"tool:{tool}", items=[{
                "content": content, "title": f"{tool} result",
                "metadata": {"artifact": True, "source_context": SRC, "turn_id": TURN_ID,
                             "artifact_processed": True, "artifact_verdict": verdict}}])
            await pool.execute("UPDATE user_context SET status = $3 WHERE entity_id = $1 AND source = $2",
                               TEST_EID, f"tool:{tool}", status)
        await captured("web_search", CONDENSED, "raw", "keep")
        await captured("read_url", BIG_DROP, "dismissed", "drop")
        found = await search_context_service(pool, TEST_EID, query=f"what about {QUERY_TOKEN}?", limit=5)
        contents = [r.get("content") or "" for r in found.get("results", [])]
        check("live recall sees a kept captured result", any(COND_TOKEN in c for c in contents), str(contents)[:200])
        gone_rows = await search_context_service(pool, TEST_EID, query=DROP_TOKEN, limit=5)
        check("live recall cannot see a dismissed one", not (gone_rows.get("results") or []), str(gone_rows)[:200])

        # ── 2. Pre-fire vs recall ────────────────────────────────────
        from mastercode import memory_engine as _me_mod
        from mastercode.memory_engine import MemoryEngine, last_prefire_items
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, "
            "status, importance) VALUES ($1, $2, 'preference', 'active', 0.5)",
            TEST_EID,
            f"Prefers the {MEM_TOKEN} runbook ({QUERY_TOKEN}): restart the relay first")
        # A document of the user's own, beside the kept tool artifact. The
        # pre-fire volunteers neither (it reads preferences and
        # relationship notes only, and never prefires
        # operational memories). `recall` reaches both, on request.
        from server.routes.context import stage_context_service
        await stage_context_service(pool, TEST_EID, source="upload", items=[{
            "content": f"Relay notes ({QUERY_TOKEN}): {DOC_TOKEN} restart order",
            "title": "relay notes"}])
        engine = MemoryEngine(pool)
        q = f"what about {QUERY_TOKEN}?"
        block = await engine.build_memory_context(
            TEST_EID, {}, max_tokens=1500, query=q)
        check("memory section renders",
              "### Relevant Right Now" in block, block[:200])
        check("memory hit prefired",
              MEM_TOKEN in _section(block, "### Relevant Right Now"), block[:300])
        check("the per-turn block pushes no document unasked",
              DOC_TOKEN not in block
              and "### From Your Research & Documents" not in block, block[:400])
        check("a kept captured result is never prefired (recall finds it on request)",
              COND_TOKEN not in block, block[:400])
        _fired = last_prefire_items(TEST_EID)
        check("the pre-fire stash records the memory hit and no document",
              any(MEM_TOKEN in x for x in _fired)
              and not any(DOC_TOKEN in x or COND_TOKEN in x for x in _fired)
              and not hasattr(_me_mod, "last_prefire_doc_items"),
              f"items={_fired}")

        # The same question, asked of `recall`: the document is reachable
        # through the documents store, and so is the kept tool artifact.
        rtxt = await _recall_text(pool, TEST_EID, q)
        docs_sec = _section(rtxt, DOCS_HEADING, "\n\n## ")
        check("the user's document is reachable through recall's documents store",
              DOC_TOKEN in docs_sec, rtxt[:600])
        check("recall finds the kept captured result in the documents store",
              COND_TOKEN in docs_sec, rtxt[:600])

        # Empty doc store: recall says it searched and found nothing (never
        # UNAVAILABLE), and the memory pre-fire is untouched.
        await pool.execute(
            "UPDATE user_context SET status='dismissed' WHERE entity_id=$1",
            TEST_EID)
        block_empty = await engine.build_memory_context(
            TEST_EID, {}, max_tokens=1500, query=q)
        check("empty doc store keeps the memory section",
              MEM_TOKEN in _section(block_empty, "### Relevant Right Now"),
              block_empty[:300])
        rtxt_empty = await _recall_text(pool, TEST_EID, q)
        docs_empty = _section(rtxt_empty, DOCS_HEADING, "\n\n## ")
        check("an empty documents store answers 'no matches', never UNAVAILABLE",
              "no matches" in docs_empty and "UNAVAILABLE" not in docs_empty
              and DOC_TOKEN not in docs_empty, rtxt_empty[:600])

    finally:
        brain_service._pool = _saved_pool
        try:
            from mastercode import memory_engine as _me
            _me._last_prefire_items.pop(TEST_EID, None)
        except Exception:
            pass
        try:
            await pool.execute(
                "DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM user_context WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM embed_jobs WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        except Exception:
            pass
        await pool.close()

    if failures:
        print(f"  [context-janitor] {len(failures)} FAILURES: {failures}")
        return 1
    print("  [context-janitor] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
