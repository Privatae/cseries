#!/usr/bin/env python3
"""Smoke gate: the thread consolidator.

WHAT THIS RATCHETS:
  1. TRIGGER — verbatim history over ~60% of the history budget (or 8 turns
     since the last run) consolidates; under both thresholds it skips.
  2. FULL-WINDOW INPUT — the summarizer sees the previous summary AND the
     full current verbatim window: the oldest (evicted-candidate) turn AND
     the newest turn, so a mid-thread correction reaches the summary.
  3. CORRECTIONS PLUMBING — the prompt carries the corrections rule; with a
     scripted model the stored summary is exactly the model's output (the
     stale instruction does not leak in through any other channel).
  4. ATOMIC SUPERSEDE — the new summary row lands and the prior one gets
     t_valid_until + resolution_pointer in ONE transaction; exactly one live
     summary per scope afterwards; turn rows are never modified.
  5. IN-FLIGHT GUARD — a second trigger while a consolidation is running is
     a no-op (returns None, model never called twice).
  6. NEXT-TURN ASSEMBLY SEAM — get_live_summary (what session.py prepends
     ahead of the verbatim turns) returns the new summary.

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

TEST_EID = 99325  # synthetic; never a real entity
SRC = {"kind": "app", "ref": "circles"}
SESSION = "cons-gate-session"

# The mid-thread reversal: turn 2 instructs prod, the LAST turn corrects it.
STALE = "deploy straight to prod, skip staging entirely"
CORRECTION = "no, deploy to staging first — forget what I said about prod"


async def _seed_turns(pool, n: int, session: str = SESSION) -> None:
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"turn {i} " + ("x" * 400)
        if i == 2:
            content = f"turn 2 decision: {STALE} " + ("x" * 380)
        if i == n - 1:
            content = f"turn {n - 1} correction: {CORRECTION} " + ("x" * 340)
        md = {"role": role, "app_id": "circles", "source_context": SRC}
        await pool.execute(
            """
            INSERT INTO entity_memory
              (entity_id, content, memory_type, importance, status,
               source_session, metadata)
            VALUES ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb)
            """,
            TEST_EID, content, session, json.dumps(md))


async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=2)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [thread-consolidator] OK {name}")
        else:
            failures.append(name)
            print(f"  [thread-consolidator] FAIL {name} {detail}")

    try:
        from mastercode import thread_consolidator as tc

        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'ConsolidatorGate', 'standard') ON CONFLICT (id) DO NOTHING",
            TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        await _seed_turns(pool, 12)

        turns_before = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id=$1 "
            "AND memory_type='turn'", TEST_EID)

        captured: list = []

        async def fake_model(messages):
            captured.append(messages)
            return {"content": "SUMMARY v1: current position is staging-first "
                               "(correction: an earlier prod-first instruction "
                               "was reversed)"}

        # 1+2+3. Pressure trigger (12 turns × ~440 chars ≈ 1300 tokens > 60%
        # of a 1000-token budget) AND cadence (12 ≥ 8) both trip.
        res = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=fake_model)
        check("consolidation fires over threshold",
              isinstance(res, dict) and res.get("status") == "consolidated",
              str(res))
        check("model was called exactly once", len(captured) == 1)
        prompt_text = "\n".join(str(m.get("content")) for m in (captured[0] or []))
        check("input contains the full window: oldest turn present",
              "turn 0" in prompt_text)
        check("input contains the full window: newest turn present",
              "turn 11" in prompt_text)
        check("input carries the corrections rule",
              "Corrections rule" in prompt_text)
        check("input contains the stale instruction AND the correction",
              STALE in prompt_text and CORRECTION in prompt_text)
        check("first run notes there is no previous summary",
              "none — this is the first consolidation" in prompt_text)

        # 4a. Summary row landed, version 1, scope-tagged, live.
        row = await pool.fetchrow(
            "SELECT id, content, metadata, t_valid_until FROM entity_memory "
            "WHERE entity_id=$1 AND memory_type='summary' "
            "AND metadata->>'source_kind'='thread_consolidator'", TEST_EID)
        check("summary row written", row is not None)
        md = row["metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        check("summary row tagged with the thread scope + version 1",
              md.get("summary_version") == 1
              and (md.get("source_context") or {}).get("kind") == "app"
              and (md.get("source_context") or {}).get("ref") == "circles",
              json.dumps(md))
        check("stored summary is exactly the model's output",
              (row["content"] or "").startswith("SUMMARY v1:"))
        check("new summary row is live", row["t_valid_until"] is None)
        v1_id = row["id"]

        # 4b. Turn rows never modified.
        turns_after = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id=$1 "
            "AND memory_type='turn' AND t_valid_until IS NULL", TEST_EID)
        check("turn rows untouched by consolidation",
              turns_after == turns_before,
              f"{turns_before} → {turns_after}")

        # 6. Next-turn assembly seam.
        live = await tc.get_live_summary(pool, TEST_EID, SRC)
        check("get_live_summary returns the new summary for assembly",
              live is not None and live.get("version") == 1
              and live["content"].startswith("SUMMARY v1:"))

        # 1b. Under threshold → skip (turns already consolidated: 0 since).
        res_skip = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=10_000_000, model_fn=fake_model)
        check("under threshold skips", isinstance(res_skip, dict)
              and res_skip.get("status") == "skipped"
              and res_skip.get("reason") == "below_threshold",
              str(res_skip))
        check("skip did not call the model again", len(captured) == 1)

        # 4c. Second consolidation (forced): ONE live summary, v1 superseded
        # with resolution_pointer → v2.
        async def fake_model_v2(messages):
            captured.append(messages)
            return {"content": "SUMMARY v2: staging-first stands"}
        res2 = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=fake_model_v2, force=True)
        check("second consolidation runs (forced)",
              isinstance(res2, dict) and res2.get("status") == "consolidated",
              str(res2))
        check("second run's input carries the PREVIOUS summary",
              "SUMMARY v1:" in "\n".join(
                  str(m.get("content")) for m in (captured[-1] or [])))
        old = await pool.fetchrow(
            "SELECT t_valid_until, resolution_pointer FROM entity_memory "
            "WHERE id=$1", v1_id)
        check("prior summary superseded (t_valid_until + resolution_pointer)",
              old["t_valid_until"] is not None
              and old["resolution_pointer"] == res2.get("summary_id"),
              f"pointer={old['resolution_pointer']} new={res2.get('summary_id')}")
        live_count = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id=$1 "
            "AND memory_type='summary' "
            "AND metadata->>'source_kind'='thread_consolidator' "
            "AND (t_valid_until IS NULL OR t_valid_until > NOW())", TEST_EID)
        check("exactly one live summary after supersede", live_count == 1,
              f"live={live_count}")
        live2 = await tc.get_live_summary(pool, TEST_EID, SRC)
        check("live summary is now v2",
              live2 is not None and live2.get("version") == 2)

        # 5a. In-flight guard: a pre-set guard key makes the trigger a no-op.
        guard_key = (TEST_EID, "app", "circles")
        tc._INFLIGHT.add(guard_key)
        try:
            res_guard = await tc.maybe_consolidate(
                pool, TEST_EID, SESSION, SRC,
                history_budget_tokens=1000, model_fn=fake_model, force=True)
            check("in-flight guard: second trigger is a no-op (None)",
                  res_guard is None, str(res_guard))
            check("in-flight guard: model not called",
                  len(captured) == 2, f"calls={len(captured)}")
        finally:
            tc._INFLIGHT.discard(guard_key)

        # 5b. In-flight guard under REAL concurrency: a slow model_fn holds
        # the guard while a concurrent trigger must no-op.
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_model(messages):
            entered.set()
            await release.wait()
            return {"content": "SUMMARY v3: slow"}
        t1 = asyncio.create_task(tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=slow_model, force=True))
        await asyncio.wait_for(entered.wait(), timeout=5)
        res_concurrent = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=fake_model, force=True)
        release.set()
        res_slow = await asyncio.wait_for(t1, timeout=5)
        check("concurrent trigger while running returns None",
              res_concurrent is None, str(res_concurrent))
        check("the running consolidation completes",
              isinstance(res_slow, dict)
              and res_slow.get("status") == "consolidated", str(res_slow))

        # Failure path: a raising model_fn degrades to skip, never raises.
        async def broken_model(messages):
            raise RuntimeError("cheap model down")
        res_err = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=broken_model, force=True)
        check("model failure degrades to skip (never raises)",
              isinstance(res_err, dict) and res_err.get("status") == "skipped",
              str(res_err))

        # 6. Failure backoff: after a failed run the scope does
        # not re-fire on the very next turn — the trigger conditions stay
        # true until a summary lands, so without this every turn re-ran a
        # doomed call. force=True still bypasses (operator intent).
        n_before = len(captured)
        res_bo = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=fake_model)
        check("post-failure trigger backs off (skipped/backoff)",
              isinstance(res_bo, dict) and res_bo.get("reason") == "backoff",
              str(res_bo))
        check("backoff did not call the model", len(captured) == n_before)
        res_bo_force = await tc.maybe_consolidate(
            pool, TEST_EID, SESSION, SRC,
            history_budget_tokens=1000, model_fn=fake_model, force=True)
        check("force bypasses the backoff",
              isinstance(res_bo_force, dict) and res_bo_force.get("status") == "consolidated",
              str(res_bo_force))
        check("a successful run clears the backoff",
              (TEST_EID, "app", "circles") not in tc._LAST_FAIL)

        # 7. Truncated structured output is a FAILURE, not a summary — a reply
        # that started as our JSON object but doesn't parse must not be
        # stored as '{"summary": "...' text in the window.
        parsed_trunc = tc._parse_consolidation_output(
            '{"summary": "Decisions: the user chose staging-first and then')
        check("truncated JSON reply → empty summary (skip + backoff), not raw text",
              parsed_trunc["summary"] == "", str(parsed_trunc))
        parsed_plain = tc._parse_consolidation_output("Just a plain prose summary.")
        check("plain prose reply is still accepted as a bare summary",
              parsed_plain["summary"] == "Just a plain prose summary.")
        parsed_ok = tc._parse_consolidation_output(
            '{"summary": "S", "topic_changed": true, "topic": "billing"}')
        check("well-formed JSON reply parses",
              parsed_ok["summary"] == "S" and parsed_ok["topic_changed"] is True
              and parsed_ok["topic"] == "billing")

        # 8. The consolidator's own input is budgeted against its model's
        # window: newest turns kept, oldest dropped, previous summary always
        # accounted for; unknown window → everything.
        fake_turns = [{"role": "user", "content": f"turn {i} " + "x" * 3800}
                      for i in range(20)]   # ~950 tokens each
        kept, dropped = tc._fit_turns_to_consolidator_window(fake_turns, "", None)
        check("unknown consolidator window → all turns, none dropped",
              len(kept) == 20 and dropped == 0)
        kept2, dropped2 = tc._fit_turns_to_consolidator_window(
            fake_turns, "p" * 4000, 8_000)   # 8k window, 1k-token prev summary
        check("small consolidator window keeps the NEWEST turns",
              dropped2 > 0 and kept2 and kept2[-1]["content"].startswith("turn 19")
              and kept2[0]["content"].startswith(f"turn {20 - len(kept2)} "),
              f"kept={len(kept2)} dropped={dropped2}")
        used = sum((len(t["content"]) + 16) // 4 for t in kept2)
        check("kept turns fit inside (window − reserve − scaffold − summary) × 0.95",
              used <= int((8_000 - tc.CONSOLIDATOR_OUTPUT_RESERVE - tc.CONSOLIDATOR_SCAFFOLD_TOKENS - 1000) * 0.95),
              str(used))
        check("cadence follows the sweep (60), summary target ~250 words",
              tc.TURNS_SINCE_TRIGGER == 60 and tc.SUMMARY_TARGET_WORDS == 250)

    finally:
        try:
            tc_key = (TEST_EID, "app", "circles")
            from mastercode import thread_consolidator as _tc
            _tc._INFLIGHT.discard(tc_key)
            _tc._LAST_FAIL.pop(tc_key, None)
        except Exception:
            pass
        try:
            await pool.execute(
                "DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        except Exception:
            pass
        await pool.close()

    if failures:
        print(f"  [thread-consolidator] {len(failures)} FAILURES: {failures}")
        return 1
    print("  [thread-consolidator] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
