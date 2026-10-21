#!/usr/bin/env python3
"""Gate: every memory row a turn writes carries the turn's type, stamped by
the platform (entity_memory.turn_type, migrations/v241), never chosen by the
model.

close_turn was handed the turn type and never used it: no row said what kind
of turn wrote it. Here, against a real
kernel schema:

  T1. a close_turn preference written in a typed turn carries the type's
      name; one in an untyped turn says 'untyped';
  T2. a goal carries it too;
  T3. inside a running turn (memory_engine.CURRENT_TURN_TYPE, set by
      runtime/session.py once the type is known) memory_curate's rows carry
      it, slotted or not;
  T4. a rule taught with "Remember this:" carries the type it was taught in;
  T5. the stamp is provenance, not part of the slot: a correction written in
      a turn of another type replaces the entry it corrects;
  T6. session.py sets the stamp right where the turn's type is resolved.

Invented values only. A throwaway cseries-postgres (docker, removed
afterwards), or TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN.
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
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


async def stamp_of(pool, rid):
    try:
        return await pool.fetchval("SELECT turn_type FROM entity_memory WHERE id = $1", int(rid))
    except Exception as e:  # noqa: BLE001 — a schema with no column says so
        return f"<{type(e).__name__}>"


async def run(pool):
    from mastercode import circles_threads as ct
    from mastercode import memory_engine as ME
    from mastercode.runtime import close_turn as close
    from mastercode.runtime.tools import _exec_memory_curate

    T = ct.descriptor_for((await ct.create_thread(pool, E, title="typed thread"))["id"])

    print("T1/T2. close_turn")
    res = await close.apply(pool, E, {"preferences": [{"text": "Answers in British English", "reason": "said",
                                                       "topic": "spelling", "scope": {"kind": "global"}}],
                                      "goal": {"set": "Draft the garden plan"}},
                            session_id="s-typed", turn_scope=T, turn_type={"name": "research", "status": "active"})
    pid = (res.get("preferences") or [{}])[0].get("id")
    gid = ((res.get("goal") or {}).get("set") or {}).get("id")
    check("T1 a preference written in a typed turn carries the type's name",
          await stamp_of(pool, pid) == "research", await stamp_of(pool, pid))
    check("T2 so does the goal", await stamp_of(pool, gid) == "research", await stamp_of(pool, gid))
    res = await close.apply(pool, E, {"preferences": [{"text": "Keep replies short", "reason": "said",
                                                       "topic": "length", "scope": {"kind": "global"}}]},
                            session_id="s-untyped", turn_scope=T, turn_type=None)
    uid = (res.get("preferences") or [{}])[0].get("id")
    check("T1 one written in an untyped turn says 'untyped'", await stamp_of(pool, uid) == "untyped",
          await stamp_of(pool, uid))

    print("T3. memory_curate inside a running turn")
    tok = ME.CURRENT_TURN_TYPE.set({"session_id": "s-curate", "name": "coding"}) \
        if hasattr(ME, "CURRENT_TURN_TYPE") else None
    try:
        a = (await _exec_memory_curate({"op": "add", "about": "person", "topic": "editor",
                                        "content": "The user edits in a terminal editor"},
                                       session_id="s-curate")).get("result") or {}
        b = (await _exec_memory_curate({"op": "add", "about": "person",
                                        "content": "The user likes dark themes"},
                                       session_id="s-curate")).get("result") or {}
    finally:
        if tok is not None:
            ME.CURRENT_TURN_TYPE.reset(tok)
    check("T3 a slotted memory_curate row carries the running turn's type",
          await stamp_of(pool, a.get("memory_id")) == "coding", (a, await stamp_of(pool, a.get("memory_id"))))
    check("T3 so does one with no topic", await stamp_of(pool, b.get("memory_id")) == "coding",
          (b, await stamp_of(pool, b.get("memory_id"))))

    print("T4. a taught rule")
    eng = ME.MemoryEngine(pool)
    try:
        r = await eng._store_teaching_memory(E, {"content": "Always cite the source", "trigger": "remember this:"},
                                             "s-teach", source_context=T, turn_type="writing")
    except TypeError as e:
        r = None
        check("T4 the teaching writer takes the turn type", False, repr(e))
    if r:
        check("T4 a rule taught in a typed turn carries its type", await stamp_of(pool, r["id"]) == "writing",
              await stamp_of(pool, r["id"]))

    print("T5. provenance, not part of the slot")
    res = await close.apply(pool, E, {"preferences": [{"text": "Answers in American English", "reason": "changed",
                                                       "topic": "spelling", "scope": {"kind": "global"}}]},
                            session_id="s-other", turn_scope=T, turn_type={"name": "chat", "status": "active"})
    p2 = (res.get("preferences") or [{}])[0]
    check("T5 a correction in a turn of another type replaces the entry it corrects",
          p2.get("replaced") == pid and await stamp_of(pool, p2.get("id")) == "chat", p2)

    print("T6. where the stamp is set")
    src = (ROOT / "brain/mastercode/runtime/session.py").read_text()
    i = src.find("set_current_turn_type(getattr(sess, \"turn_type\", None))")
    j = src.find("_CTT.set({\"session_id\": sess.session_id, \"name\": _turn_type_name})")
    check("T6 session.py stamps the turn's type right where it is resolved", 0 < i < j < i + 1500, (i, j))


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
