#!/usr/bin/env python3
"""Smoke gate: the context janitor + warm recall (conversation depth).

WHAT THIS RATCHETS:
  1. ARTIFACT CAPTURE — a turn's tool results land as user_context rows:
     source='tool:<name>', status 'raw', metadata tagged with the thread's
     source_context + turn id, search_tsv populated. Trivially small results
     and within-turn duplicates are skipped.
  2. JANITOR KEEP/DROP — with a stubbed model the KEEP row is marked
     processed (+ condensed content + rebuilt lexical index) and stays live;
     the DROP row is marked processed AND dismissed, which the real
     user_context recall lane (search_context_service) then cannot see.
  3. EMBED SUBMISSION — kept rows ride ONE per-ids embed_jobs row through
     the existing machinery (the consolidator submits, the worker embeds).
  4. PRE-FIRE vs RECALL — build_memory_context pre-fires the matching
     preference under "### Relevant Right Now" and pushes no document: the
     user's document and the kept tool artifact stay out of the per-turn
     block (the pre-fire reads memory only). `recall`'s own call
     (memory_retrieval.unified_search, rendered by format_unified_text)
     reaches both through the documents store, and an emptied documents
     store answers "no matches", never UNAVAILABLE, with the memory
     pre-fire intact.
  5. TOPIC SEGMENTS — a stubbed topic flip stores segment_topic and the
     "Previously: X" continuity line, superseding atomically (exactly one
     live summary per scope).
  6. TRIGGERS — with neither budget pressure nor turn cadence reached,
     maybe_consolidate does NOT fire; `force` still does. (The 45-minute
     idle-gap trigger was removed with the post-turn lane it
     belonged to: it asked "did THIS turn return from idle", which only a
     post-turn caller can know. The rolling summary is now written by
     batch_consolidator on the consolidation cron.)

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up.
"""
from __future__ import annotations

import asyncio
import json
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
KEEP_TOKEN = "keeptoken17"
DROP_TOKEN = "droptoken23"
COND_TOKEN = "condensedtoken29"
MEM_TOKEN = "memtoken31"
QUERY_TOKEN = "janquery37"  # shared by the memory row and the doc rows
DOC_TOKEN = "doctoken41"

BIG_KEEP = (f"Research findings on the {KEEP_TOKEN} deploy gate ({QUERY_TOKEN}): "
            "the gate requires a green smoke run and a signed manifest. " + "detail " * 60)
BIG_DROP = f"directory listing {DROP_TOKEN}: bin etc usr var " + "x " * 80
CONDENSED = f"CONDENSED {COND_TOKEN} ({QUERY_TOKEN}): gate = green smoke + signed manifest"


def _janitor_or_summary(messages, summary_obj):
    """One stub for both consolidator calls: the janitor prompt carries the
    janitor system text; everything else is the summary prompt."""
    sys_msg = str(messages[0].get("content")) if messages else ""
    if "context janitor" in sys_msg:
        user_msg = str(messages[-1].get("content"))
        if KEEP_TOKEN in user_msg:
            return {"content": json.dumps(
                {"verdict": "keep", "content": CONDENSED})}
        return {"content": json.dumps({"verdict": "drop", "content": ""})}
    return {"content": json.dumps(summary_obj)}



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
        from mastercode import thread_consolidator as tc
        # ensure_table() went with the runtime DDL — user_context is declared in
        # init-db.sql / migrations/v090 and applied by entrypoint.sh.
        from server.routes.context import search_context_service

        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier, "
            "behavioral_params) VALUES ($1, 'JanitorGate', 'standard', "
            "'{\"embedding_model\": \"gate-embed-model\"}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM user_context WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM embed_jobs WHERE entity_id=$1", TEST_EID)

        # ── 1. Artifact capture ──────────────────────────────────────
        # Tools here must be REGISTERED, external-content tools (only
        # ARTIFACT_CATEGORIES are stashed; an unregistered name like the old
        # 'research_db' is now not_worthy by design). read_url is the DROP
        # candidate; personality_state is the host's own self-state and must
        # never reach the corpus at all.
        artifacts = [
            {"tool": "web_search", "content": BIG_KEEP},
            {"tool": "web_search", "content": "ok"},          # too small
            {"tool": "web_search", "content": BIG_KEEP},      # in-turn dupe
            {"tool": "read_url", "content": BIG_DROP},
            {"tool": "personality_state", "content": BIG_DROP + " (self-state)"},
        ]
        res = await tc.stash_tool_artifacts(
            pool, TEST_EID, SESSION, SRC, artifacts, turn_id=TURN_ID)
        check("capture stashes the two substantive results",
              res.get("stashed") == 2, str(res))
        check("capture skips the small result and the duplicate",
              res.get("skipped") == 2, str(res))
        check("capture refuses the host's self-state read (not_worthy)",
              res.get("not_worthy") == 1, str(res))
        rows = await pool.fetch(
            "SELECT id, source, status, content, metadata FROM user_context "
            "WHERE entity_id=$1 AND source LIKE 'tool:%%' ORDER BY id", TEST_EID)
        check("exactly two artifact rows landed", len(rows) == 2,
              f"rows={len(rows)}")
        by_source = {r["source"]: r for r in rows}
        check("rows carry source='tool:<name>'",
              set(by_source) == {"tool:web_search", "tool:read_url"},
              str(sorted(by_source)))
        wr = by_source.get("tool:web_search")
        md = wr["metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        check("row tagged with the thread scope + turn id, status raw",
              (md.get("source_context") or {}).get("kind") == "app"
              and (md.get("source_context") or {}).get("ref") == "circles"
              and md.get("turn_id") == TURN_ID
              and wr["status"] == "raw",
              json.dumps(md))
        check("content capped at the artifact ceiling",
              all(len(r["content"]) <= tc.ARTIFACT_MAX_CHARS for r in rows))
        tsv_hit = await pool.fetchval(
            "SELECT search_tsv @@ plainto_tsquery('english', $1) "
            "FROM user_context WHERE id=$2", KEEP_TOKEN, wr["id"])
        check("lexical index populated at write time", bool(tsv_hit))

        # ── 2+3. Janitor KEEP/DROP + embed submission ────────────────
        async def stub(messages):
            return _janitor_or_summary(
                messages,
                {"summary": "s", "topic_changed": False, "topic": "t"})

        jan = await tc._curate_artifacts(pool, TEST_EID, SRC, stub)
        check("janitor reviewed both artifacts",
              jan.get("reviewed") == 2 and jan.get("kept") == 1
              and jan.get("dropped") == 1, str(jan))
        kept = await pool.fetchrow(
            "SELECT content, status, metadata, search_tsv FROM user_context "
            "WHERE entity_id=$1 AND source='tool:web_search'", TEST_EID)
        kmd = kept["metadata"]
        if isinstance(kmd, str):
            kmd = json.loads(kmd)
        check("KEEP row: processed, live, condensed content",
              kmd.get("artifact_processed") is True
              and kmd.get("artifact_verdict") == "keep"
              and kept["status"] == "raw"
              and kept["content"] == CONDENSED, json.dumps(kmd))
        check("KEEP row: lexical index rebuilt from the condensed bytes",
              bool(await pool.fetchval(
                  "SELECT search_tsv @@ plainto_tsquery('english', $1) "
                  "FROM user_context WHERE id=$2", COND_TOKEN, wr["id"])))
        dropped = await pool.fetchrow(
            "SELECT status, metadata FROM user_context "
            "WHERE entity_id=$1 AND source='tool:read_url'", TEST_EID)
        dmd = dropped["metadata"]
        if isinstance(dmd, str):
            dmd = json.loads(dmd)
        check("DROP row: processed + dismissed",
              dropped["status"] == "dismissed"
              and dmd.get("artifact_processed") is True
              and dmd.get("artifact_verdict") == "drop", json.dumps(dmd))

        # The REAL recall predicate: search_context_service (the lane the
        # federated pre-fire reads) must see the kept row and not the drop.
        # (plainto_tsquery ANDs non-stopword terms — the query is stopwords
        # plus the shared token, the same shape the prefire gate proved.)
        found = await search_context_service(
            pool, TEST_EID, query=f"what about {QUERY_TOKEN}?", limit=5)
        contents = [r.get("content") or "" for r in found.get("results", [])]
        check("live recall sees the kept artifact",
              any(COND_TOKEN in c for c in contents), str(contents)[:200])
        gone = await search_context_service(
            pool, TEST_EID, query=DROP_TOKEN, limit=5)
        check("live recall cannot see the dropped artifact",
              not (gone.get("results") or []), str(gone)[:200])

        job = await pool.fetchrow(
            "SELECT id, job_type, target, status, metadata FROM embed_jobs "
            "WHERE entity_id=$1 ORDER BY id DESC LIMIT 1", TEST_EID)
        jmd = (job["metadata"] if job else None) or {}
        if isinstance(jmd, str):
            jmd = json.loads(jmd)
        check("embed job enqueued for the kept row",
              job is not None and job["job_type"] == "tool_artifacts"
              and job["target"] == "user_context"
              and wr["id"] in (jmd.get("ids") or []),
              f"job={dict(job) if job else None}"[:300])
        check("janitor stats carry the embed job id",
              jan.get("embed_job") == (job["id"] if job else None), str(jan))

        # A second janitor pass finds nothing unprocessed.
        jan2 = await tc._curate_artifacts(pool, TEST_EID, SRC, stub)
        check("processed rows are not re-reviewed",
              jan2.get("reviewed") == 0, str(jan2))

        # ── 4. Pre-fire vs recall ────────────────────────────────────
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
        check("the kept tool artifact is never prefired (recall finds it on request)",
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
        check("recall finds the kept tool artifact in the documents store",
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

        # ── 5. Topic segmentation ────────────────────────────────────
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)

        async def _seed_turns(n: int) -> None:
            for i in range(n):
                role = "user" if i % 2 == 0 else "assistant"
                md = {"role": role, "app_id": "circles", "source_context": SRC}
                await pool.execute(
                    "INSERT INTO entity_memory (entity_id, content, "
                    "memory_type, importance, status, source_session, "
                    "metadata) VALUES ($1, $2, 'turn', 0.5, 'active', $3, "
                    "$4::jsonb)",
                    TEST_EID, f"turn {i} " + "x" * 400, SESSION,
                    json.dumps(md))

        await _seed_turns(10)
        seg_calls: list = []

        async def seg_model_v1(messages):
            seg_calls.append(messages)
            return _janitor_or_summary(messages, {
                "summary": "SEG1 summary body", "topic_changed": False,
                "topic": "deploy planning"})

        r1 = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=seg_model_v1)
        check("first consolidation lands",
              isinstance(r1, dict) and r1.get("status") == "consolidated",
              str(r1))
        s1 = await pool.fetchrow(
            "SELECT id, content, metadata FROM entity_memory "
            "WHERE entity_id=$1 AND memory_type='summary' "
            "AND metadata->>'source_kind'='thread_consolidator'", TEST_EID)
        s1md = s1["metadata"]
        if isinstance(s1md, str):
            s1md = json.loads(s1md)
        check("segment topic stored in summary metadata",
              s1md.get("segment_topic") == "deploy planning", json.dumps(s1md))
        check("structured summary body stored verbatim",
              s1["content"] == "SEG1 summary body", s1["content"][:80])
        check("summary prompt asks for the JSON shape",
              '"topic_changed"' in "\n".join(
                  str(m.get("content")) for m in (seg_calls[0] or [])))

        async def seg_model_v2(messages):
            return _janitor_or_summary(messages, {
                "summary": "SEG2 summary body", "topic_changed": True,
                "topic": "billing research"})

        r2 = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=seg_model_v2, force=True)
        check("flip consolidation lands",
              isinstance(r2, dict) and r2.get("status") == "consolidated"
              and r2.get("topic_changed") is True, str(r2))
        s2 = await pool.fetchrow(
            "SELECT content, metadata FROM entity_memory WHERE id=$1",
            r2.get("summary_id"))
        s2md = s2["metadata"]
        if isinstance(s2md, str):
            s2md = json.loads(s2md)
        check("flip notes the previous topic in one line",
              s2["content"].startswith("Previously: deploy planning"),
              s2["content"][:80])
        check("new segment topic stored",
              s2md.get("segment_topic") == "billing research",
              json.dumps(s2md))
        old = await pool.fetchrow(
            "SELECT t_valid_until, resolution_pointer FROM entity_memory "
            "WHERE id=$1", s1["id"])
        check("flip supersedes the prior segment atomically",
              old["t_valid_until"] is not None
              and old["resolution_pointer"] == r2.get("summary_id"))
        live_n = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id=$1 "
            "AND memory_type='summary' "
            "AND metadata->>'source_kind'='thread_consolidator' "
            "AND (t_valid_until IS NULL OR t_valid_until > NOW())", TEST_EID)
        check("exactly one live summary after the flip", live_n == 1,
              f"live={live_n}")
        live = await tc.get_live_summary(pool, TEST_EID, SRC)
        check("get_live_summary exposes the current segment topic",
              live is not None and live.get("segment_topic") == "billing research",
              str(live))

        # ── 6. Idle-gap trigger ──────────────────────────────────────
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        md_old = {"role": "assistant", "source_context": SRC}
        md_new = {"role": "user", "source_context": SRC}
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, "
            "importance, status, source_session, metadata, created_at) "
            "VALUES ($1, 'short old reply', 'turn', 0.5, 'active', $2, "
            "$3::jsonb, NOW() - interval '50 minutes')",
            TEST_EID, SESSION, json.dumps(md_old))
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, "
            "importance, status, source_session, metadata) "
            "VALUES ($1, 'back after a while', 'turn', 0.5, 'active', $2, "
            "$3::jsonb)",
            TEST_EID, SESSION, json.dumps(md_new))

        async def idle_model(messages):
            return _janitor_or_summary(messages, {
                "summary": "IDLE summary", "topic_changed": True,
                "topic": "new topic"})

        # Tiny content (no pressure), 2 turns (no cadence). The 50-minute gap
        # used to fire the consolidator on its own; that trigger went with the
        # post-turn lane, so the correct answer now is SKIPPED.
        r3 = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=10_000_000, model_fn=idle_model)
        check("an idle gap alone no longer fires the consolidator",
              isinstance(r3, dict) and r3.get("status") == "skipped"
              and r3.get("reason") == "below_threshold", str(r3))
        check("the retired trigger is gone from the module, not just unused",
              not hasattr(tc, "IDLE_GAP_TRIGGER_S")
              and not hasattr(tc, "_scope_idle_gap_seconds"))
        # force= is the manual path that remains, and it still works.
        r3f = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=10_000_000, model_fn=idle_model, force=True)
        check("force still consolidates (the manual entry point)",
              isinstance(r3f, dict) and r3f.get("status") == "consolidated",
              str(r3f))

        # Control: a back-to-back exchange stays under every trigger.
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        for content in ("first quick message", "second quick message"):
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, "
                "importance, status, source_session, metadata) "
                "VALUES ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb)",
                TEST_EID, content, SESSION, json.dumps(md_new))
        r4 = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=10_000_000, model_fn=idle_model)
        check("back-to-back exchange does not fire",
              isinstance(r4, dict) and r4.get("status") == "skipped"
              and r4.get("reason") == "below_threshold", str(r4))

    finally:
        brain_service._pool = _saved_pool
        try:
            from mastercode import thread_consolidator as _tc
            _tc._INFLIGHT.discard((TEST_EID, "app", "circles"))
        except Exception:
            pass
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
