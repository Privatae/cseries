#!/usr/bin/env python3
"""Smoke gate: the unified memory recall engine (P0/P1/P1b ratchet).

WHAT THIS RATCHETS:
  1. FILTER COHERENCE — superseded (t_valid_until set) and non-active rows
     surface in ZERO recall lanes. Before P0, the chat recall tool had no
     status/bitemporal predicate at all and could quote dead memories.
  2. WORKS WITHOUT EMBEDDINGS — with the embed lane down (this container's
     natural state) recall still finds exact tokens lexically via the v096
     generated search_tsv column, arms honestly reported. ILIKE is gone.
  3. LEXEME NORMALIZATION — 'budget.csv'-style tokens match a search for
     "budget" (the v095 rule, applied to memory).
  4. HONEST TOTAL FAILURE — when NO arm can run, RecallUnavailable raises;
     an empty result is only ever produced by a search that ran.
  5. ONE FRONT DOOR — federated_recall spans memory + documents with labeled
     sections; host-facing memory_search is fully retired from the tool
     surface (the external ceigas_memory_search name lives on, routed through
     brain_service.search_memory → this engine).

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

TEST_EID = 99322  # synthetic; never a real entity


async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=2)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [recall-engine] OK {name}")
        else:
            failures.append(name)
            print(f"  [recall-engine] FAIL {name} {detail}")

    try:
        # entity_memory carries an entity_registry FK; explicit license_tier
        # because the column DEFAULT is CHECK-invalid (the v094 lesson).
        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'RecallGate', 'standard') ON CONFLICT (id) DO NOTHING",
            TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM user_context WHERE entity_id=$1", TEST_EID)

        # Seed: A live fact with file-token content; B superseded; C archived;
        # D live fact sharing the federated token with a user_context row.
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance) "
            "VALUES ($1, 'Acme payroll lands in the budget.csv ledger, account x0000', "
            "'cited_fact', 'active', 1.2)", TEST_EID)
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance, t_valid_until) "
            "VALUES ($1, 'coherencetoken99 superseded version of the ledger fact', "
            "'cited_fact', 'active', 1.0, now() - interval '1 hour')", TEST_EID)
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance) "
            "VALUES ($1, 'coherencetoken99 archived copy, must never surface', "
            "'cited_fact', 'archived', 1.0)", TEST_EID)
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance) "
            "VALUES ($1, 'Conclusion about gatetoken77 drawn from the user document', "
            "'cited_fact', 'active', 1.0)", TEST_EID)
        await pool.execute(
            "INSERT INTO user_context (entity_id, content, source, title, status, search_tsv) "
            "VALUES ($1, 'raw document row mentioning gatetoken77 in full', 'smoke', "
            "'gatefile.txt', 'raw', to_tsvector('english', 'gatefile txt raw document "
            "row mentioning gatetoken77 in full'))", TEST_EID)

        from server import brain_service
        brain_service._pool = pool
        brain_service._entity_id = TEST_EID
        from mastercode import memory_retrieval as mr
        from mastercode.memory_engine import RecallUnavailable

        # 2+3: lexical arm finds the live fact; normalized token matches.
        res = await mr.recall_memories(pool, TEST_EID, "budget")
        ids = [h["content"][:20] for h in res["hits"]]
        check("lexical arm finds 'budget' inside budget.csv token",
              any("Acme" in (h.get("content") or "") for h in res["hits"])
              and "lexical" in res["arms"], f"got arms={res['arms']} hits={ids}")

        # 1: filter coherence — engine.
        res = await mr.recall_memories(pool, TEST_EID, "coherencetoken99")
        check("superseded + archived rows surface in zero engine hits",
              len(res["hits"]) == 0, f"got {res['hits']}")

        # 1: filter coherence — search_memory lane (backs ceigas MCP too).
        sm = await brain_service.search_memory("coherencetoken99", limit=10)
        check("search_memory lane honors the same filters",
              len(sm) == 0, f"got {sm}")
        sm = await brain_service.search_memory("budget", limit=10)
        check("search_memory lane finds live rows",
              any("Acme" in (r.get("content") or "") for r in sm), f"got {sm}")

        # 4: both arms dead → RecallUnavailable, never a silent empty.
        real_vec, real_lex = mr._vector_arm, mr._lexical_arm
        async def _dead(*a, **k):
            return [], False, "forced dead (gate)"
        mr._vector_arm = _dead
        mr._lexical_arm = _dead
        try:
            raised = False
            try:
                await mr.recall_memories(pool, TEST_EID, "anything")
            except RecallUnavailable:
                raised = True
            check("no arm ran -> RecallUnavailable raised", raised)
        finally:
            mr._vector_arm, mr._lexical_arm = real_vec, real_lex

        # 5: one front door — both stores, labeled sections.
        fed = await mr.federated_recall(pool, TEST_EID, "gatetoken77")
        mem_hit = any("gatetoken77" in (h.get("content") or "")
                      for h in (fed.get("memory") or {}).get("hits") or [])
        doc_hit = any("gatetoken77" in (r.get("content") or "")
                      for r in (fed.get("documents") or {}).get("results") or [])
        check("federated recall spans memory AND documents",
              mem_hit and doc_hit, f"mem={mem_hit} doc={doc_hit}")
        text = mr.format_recall_text(fed)
        check("formatted result labels both sources",
              "Your memory" in text and "The user's documents" in text,
              f"got {text[:200]!r}")

        # P3: composite ranking — equal lexical match, adversarial RRF
        # order (low-importance old row inserted FIRST so it wins raw RRF);
        # importance+recency must flip it.
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance, created_at) "
            "VALUES ($1, 'ranktoken55 alpha entry', 'cited_fact', 'active', 0.5, now() - interval '200 days')",
            TEST_EID)
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance) "
            "VALUES ($1, 'ranktoken55 beta entry', 'cited_fact', 'active', 1.9)", TEST_EID)
        res = await mr.recall_memories(pool, TEST_EID, "ranktoken55")
        ordered = [h["content"] for h in res["hits"] if "ranktoken55" in (h.get("content") or "")]
        check("composite ranking: important+recent outranks old+unimportant",
              len(ordered) == 2 and "beta" in ordered[0], f"got {ordered}")

        # P4: write→retrievable probe — live row true; superseded row not
        # reachable (it must fail loud, never read as absence).
        live_id = await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id=$1 AND content LIKE 'ranktoken55 beta%'",
            TEST_EID)
        dead_id = await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id=$1 AND t_valid_until IS NOT NULL LIMIT 1",
            TEST_EID)
        p_live = await mr.verify_retrievable(pool, TEST_EID, live_id)
        p_dead = await mr.verify_retrievable(pool, TEST_EID, dead_id)
        check("retrievable probe: live write verifies",
              p_live.get("retrievable") is True and p_live.get("lexically_indexed") is True,
              f"got {p_live}")
        check("retrievable probe: superseded row reports NOT reachable",
              p_dead.get("retrievable") is False and p_dead.get("reachable") is False,
              f"got {p_dead}")

        # 5: memory_search retired from the host tool surface.
        from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
        check("memory_search absent from public tool registry",
              "memory_search" not in PUBLIC_TOOL_SCHEMAS)
        sess_src = open("/app/brain/mastercode/runtime/session.py").read()
        check("session chat surface carries no memory_search",
              "memory_search" not in sess_src)
        check("recall tool description covers documents",
              "uploaded" in sess_src and '"scope"' in sess_src)
    finally:
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM user_context WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        await pool.close()

    if failures:
        print(f"recall-engine gate: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("recall-engine gate: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
