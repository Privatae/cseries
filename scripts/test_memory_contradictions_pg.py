#!/usr/bin/env python3
"""Gate: two contradicting entries of every type, each written the way the Host writes it, on a default
Host with NO embedding model, against a real kernel schema.

For every type: after A and then a contradicting B with the same metadata,
what stays live, and what does the Host read?

  preference, close_turn        one live (B); the block carries B only
  preference, memory_curate     one live (B); the block carries B only
  relationship, memory_curate   one live (B); the block carries B only
  taught rule, "Remember this:" one live (B, keyed in its turn); B rides taught
  goal, close_turn              one live (B); thread_state says B, dated
  cited fact, task_record       BOTH live: two sources may disagree, each a
                                quote of its own source; recall shows both

Before memory slots both entries stayed live for every type but goal, and
the older was listed first. Every value is invented. A throwaway cseries-postgres through docker, removed
afterwards (or TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN).
"""
import asyncio
import contextlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server and docker unavailable — this gate needs a real database.")
    sys.exit(2)

FAILED = 0
E = 1


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:900]}"))
    if not cond:
        FAILED += 1


async def run(pool):
    from mastercode import circles_threads as ct
    from mastercode.memory_engine import MemoryEngine, _get_memory_embedding
    from mastercode.memory_retrieval import recall_memories
    from mastercode.runtime import close_turn as close
    from mastercode.runtime.tools import _exec_memory_curate
    eng = MemoryEngine(pool)
    T = ct.descriptor_for((await ct.create_thread(pool, E, title="probe thread"))["id"])
    check("no embedding model is configured (the default Host)",
          await _get_memory_embedding("probe", pool=pool, entity_id=E) is None)

    async def live(words):
        return [r["content"] for r in await pool.fetch(
            "SELECT content FROM entity_memory WHERE entity_id = $1 AND content ILIKE '%'||$2||'%'"
            " AND status = 'active' AND t_valid_until IS NULL ORDER BY id", E, words)]

    async def block(query):
        return await eng.build_memory_context(E, {}, max_tokens=16000, query=query, source_context=T)

    async def ct_pref(text, topic):
        return await close.apply(pool, E, {"preferences": [{"text": text, "reason": "said", "topic": topic,
                                                           "scope": {"kind": "global"}}]},
                                 session_id="s-probe", turn_scope=T)

    async def curate(**a):
        return await _exec_memory_curate({"op": "add", "about": "person", **a}, session_id="s-probe")

    rows = []

    print("preference via close_turn")
    await ct_pref("Replies use British English spelling", "spelling")
    await ct_pref("Replies use American English spelling", "spelling")
    lv, blk = await live("English spelling"), await block("Which English spelling should replies use?")
    rows.append(("preference / close_turn", lv))
    check("close_turn preference: one live, the newer", lv == ["Replies use American English spelling"], lv)
    check("close_turn preference: the block carries the newer only", "American" in blk and "British" not in blk, blk)

    print("preference via memory_curate")
    await curate(content="The user takes coffee black", topic="coffee")
    await curate(content="The user takes coffee with oat milk", topic="coffee")
    lv, blk = await live("coffee"), await block("how does the user take their coffee?")
    rows.append(("preference / memory_curate", lv))
    check("memory_curate preference: one live, the newer", lv == ["The user takes coffee with oat milk"], lv)
    check("memory_curate preference: the block carries the newer only", "oat milk" in blk and "black" not in blk, blk)

    print("relationship via memory_curate")
    await curate(memory_type="relationship", topic="the user", content="The user and the Host speak informally")
    await curate(memory_type="relationship", topic="the user", content="The user and the Host speak formally")
    lv, blk = await live("speak"), await block("should I speak formally or informally?")
    rows.append(("relationship / memory_curate", lv))
    check("relationship: one live, the newer", lv == ["The user and the Host speak formally"], lv)
    check("relationship: the block carries the newer only", "formally" in blk and "informally" not in blk, blk)

    print("taught rule")
    for words in ("Always give distances in kilometres", "Always give distances in miles"):
        await eng._store_teaching_memory(E, {"content": words, "trigger": "remember this:"}, "s-probe",
                                         source_context=T)
        await _exec_memory_curate({"op": "key", "content": words, "topic": "units"}, session_id="s-probe")
        await asyncio.sleep(0.02)
    lv, blk = await live("distances"), await block("how far is it?")
    rows.append(("taught rule", lv))
    check("taught rule: one live, the newer", lv == ["Always give distances in miles"], lv)
    check("taught rule: the newer rides taught, the older not at all", "in miles" in blk and "kilometres" not in blk,
          blk)

    print("goal")
    for g in ("Book a flight to Lisbon for May", "Book a train to Porto for June instead"):
        await close.apply(pool, E, {"goal": {"set": g}}, session_id="s-probe", turn_scope=T)
    lv, st = await live("Book a"), await close.thread_state(pool, E, T)
    rows.append(("goal", lv))
    check("goal: one live, the newer", lv == ["Book a train to Porto for June instead"], lv)
    check("goal: the thread state says the newer, dated", st and "Porto" in st and "Lisbon" not in st
          and "Goal (set " in st, st)

    print("cited fact")
    from mastercode.memory_consolidation import MemoryConsolidator
    cons = MemoryConsolidator(pool)
    P = {"kind": "project", "ref": "7"}
    for i, (words, src) in enumerate((("The library opens at 9am on Saturdays", "https://a.example/hours"),
                                       ("The library opens at 10am on Saturdays", "https://b.example/hours"))):
        await cons._insert_new_memory(E, words, "cited_fact", 1.0, None, metadata_extra={
            "written_by": "task_record", "source_context": P,
            "cited": {"finding": str(i), "source": src, "quote": words}})
    lv = await live("library opens")
    rows.append(("cited fact (two sources)", lv))
    check("cited fact: both stay live (two sources may disagree)", len(lv) == 2, lv)
    rec = await recall_memories(pool, E, "When does the library open on Saturdays?", limit=10)
    check("cited fact: recall finds both, the newer first",
          [h["content"] for h in rec["hits"] if h["memory_type"] == "cited_fact"][:2]
          == ["The library opens at 10am on Saturdays", "The library opens at 9am on Saturdays"], rec["hits"])

    print("\nwhat stayed live, by type:")
    for label, lv in rows:
        print(f"  {label:32} {len(lv)} live: {lv}")


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    from server import brain_service as _bs
    saved = (_bs._pool, _bs._entity_id)
    _bs._pool, _bs._entity_id = pool, E
    try:
        await run(pool)
    except Exception as e:  # noqa: BLE001
        check("the gate ran to its end", False, repr(e))
    finally:
        _bs._pool, _bs._entity_id = saved
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else rc)
