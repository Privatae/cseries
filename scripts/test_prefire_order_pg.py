#!/usr/bin/env python3
"""Gate: the memory block lists the newest first, matches a message on a
default Host (no embedder), and never drops an entry without saying so
(memory_engine.build_memory_context, memory_retrieval._lexical_arm).

Before this: of two same-scope preferences that match equally, the OLDER,
contradicted one led "Relevant Right Now" until it was about a week older;
"Which spelling do you want me to use?" matched nothing, because the lexical
arm needed every word of the message; and
past five entries per section the oldest stopped riding with no word to the
Host. Against a real kernel schema, no embedding model, invented values:

  O. two entries with no topic that match equally: the newer is listed first,
     in the block and in recall;
  W. a message that shares two words with an entry finds it; one common word
     alone does not; an entry's topic matches on its own;
  C. seven preferences: the newest five ride, and the block says two more are
     not shown and how to read them; six taught rules: five and "1 more";
  R. the turn records which rows rode (the caller's prefire_sink "rode");
  P. a project-typed turn is shown no topic from anywhere else.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

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
NOW = datetime.now(timezone.utc)


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:900]}"))
    if not cond:
        FAILED += 1


async def mem(pool, content, *, mtype="preference", days_ago=0.0, md=None, topic=None):
    meta = {"about": {"kind": "global"}, "written_by": "close_turn", **(md or {})}
    if topic:
        try:
            return await pool.fetchval(
                "INSERT INTO entity_memory (entity_id, content, memory_type, metadata, created_at, t_valid_from,"
                " slot_topic, slot_scope) VALUES ($1, $2, $3, $4::jsonb, $5, $5, $6, 'global') RETURNING id",
                E, content, mtype, json.dumps(meta), NOW - timedelta(days=days_ago), topic)
        except asyncpg.UndefinedColumnError:
            pass
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, metadata, created_at, t_valid_from)"
        " VALUES ($1, $2, $3, $4::jsonb, $5, $5) RETURNING id",
        E, content, mtype, json.dumps(meta), NOW - timedelta(days=days_ago))


def section(text, head):
    if f"### {head}" not in text:
        return []
    body = text.split(f"### {head}", 1)[1].split("\n###", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith("- ")]


async def clean(pool):
    await pool.execute("UPDATE entity_memory SET resolution_pointer = NULL WHERE entity_id = $1", E)
    await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND memory_type <> 'turn'", E)


async def run(pool):
    from mastercode.memory_engine import MemoryEngine
    from mastercode.memory_retrieval import recall_memories
    eng = MemoryEngine(pool)
    scope = {"kind": "app", "ref": "circles"}

    async def block(query, **kw):
        return await eng.build_memory_context(E, {}, max_tokens=16000, query=query, source_context=scope, **kw)

    print("O. ties go to the newest")
    for older_by in (0.0, 2.0, 5.0):
        await clean(pool)
        old = await mem(pool, "Replies to the user use British English spelling", days_ago=older_by + 0.001)
        new = await mem(pool, "Replies to the user use American English spelling", days_ago=0.0)
        rel = section(await block("Which English spelling should replies use?"), "Relevant Right Now")
        check(f"O the newer of two equal matches leads Relevant Right Now (the older {older_by:g} days older)",
              len(rel) >= 2 and "American" in rel[0] and "British" in rel[1], rel)
        rec = await recall_memories(pool, E, "Which English spelling should replies use?", limit=10,
                                    memory_types=["preference"])
        order = [h["id"] for h in rec["hits"]]
        check(f"O ...and in recall ({older_by:g} days)", order[:2] == [new, old], order)

    print("W. matching with no embedder")
    await clean(pool)
    await mem(pool, "Replies to the user use British English spelling")
    await mem(pool, "Use the 24-hour clock in every answer")
    rel = section(await block("Which spelling do you want me to use?"), "Relevant Right Now")
    check("W a message that shares two words with an entry finds it",
          any("British English spelling" in x for x in rel), rel)
    check("W one common word alone ('use') is no match", not any("24-hour" in x for x in rel), rel)
    await mem(pool, "No peanuts in any recipe for the user", topic="dietary_limits")
    rel = section(await block("Any dietary limits I should know about?"), "Relevant Right Now")
    check("W an entry's topic matches on its own", any("peanuts" in x for x in rel), rel)

    print("C. nothing left out unannounced")
    await clean(pool)
    for i in range(7):
        await mem(pool, f"Invented standing preference number {i}", days_ago=7 - i)
    sink: dict = {}
    text = await block("hello there", prefire_sink=sink)
    prefs = section(text, "Their Preferences")
    shown = [x for x in prefs if "standing preference number" in x]
    check("C the newest five ride, newest first", [s.split("number ")[1][0] for s in shown] == list("65432"),
          prefs)
    check("C ...and the section says two more are not shown and how to read them",
          any(x.startswith("- (2 more preferences not shown here; recall finds them") for x in prefs), prefs)
    print("R. the turn records what rode")
    rode = sink.get("rode") or []
    check("R the caller's sink names every row the block showed",
          len([r for r in rode if r.get("section") == "standing"]) == 5, rode)
    for i in range(6):
        await eng._store_teaching_memory(E, {"content": f"Invented taught rule number {i}",
                                             "trigger": "remember this:"}, "s-rules")
        await asyncio.sleep(0.01)
    taught = section(await block("hello there"), "Things You Were Taught")
    check("C six taught rules: five ride, and the section says one more is not shown",
          len([x for x in taught if "taught rule number" in x]) == 5
          and any(x.startswith("- (1 more rules you were taught not shown here") for x in taught), taught)

    print("P. a project-typed turn sees no other topic")
    await mem(pool, "Invented topic'd preference", topic="elsewhere_topic")
    text = await eng.build_memory_context(E, {}, max_tokens=16000, query="hello", source_context=scope,
                                          context={"memory": "projects", "projects": [3], "documents": False})
    check("P no topics line for a project-typed turn", "elsewhere_topic" not in text, text[-300:])
    text = await block("hello")
    check("P an ordinary turn is told the topics in use", "Topics in use" in text
          and "elsewhere_topic" in text, text[-300:])


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        await run(pool)
    except Exception as e:  # noqa: BLE001
        check("the gate ran to its end", False, repr(e))
    finally:
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else rc)
