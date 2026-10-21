#!/usr/bin/env python3
"""Gate: one live memory per slot (type, scope, topic), against a real kernel
schema (brain/mastercode/memory_slots.py, migrations/v241_memory_slots.sql).

A Host's memory once kept two contradicting entries with the same type and
scope live side by side, the older listed first. Here, with no embedding model configured (the default Host):

  S1. close_turn: preference A, then B in the same scope and topic: A is
      retired with its pointer on B, and the memory block carries B only;
  S2. the same through memory_curate add; an add with a topic and no scope
      replaces the entry in that topic where this conversation reads it;
  S3. relationship notes: the topic is who it is about, and the newer wins;
  S4. two taught rules keyed into one slot: the newer wins;
  S5. a plain preference written into a rule's slot is refused and changes
      nothing; a rule keyed into a plain preference's slot replaces it;
  S6. a second live row in a slot, written directly, raises
      UniqueViolationError;
  S7. goals keep their own rule (one open goal per thread, newest wins) and
      the thread state dates the goal;
  S8. a cited fact is never retired by another source's: both stay live;
  S9. keying an older row into a newer row's slot retires the older into the
      newer; memory_scope moving a topic'd row into a held slot settles it by
      the same rule;
  S10. deleting the thread a replacement was made in brings the replaced row
      back, one live entry in the slot;
  S11. v241: existing rows get their scope by the writers' own rule (SQL and
      Python agree), no topic; live rolling summaries are archived, and the
      topic markers read from their chain keep every flip; the migration
      runs twice clean.
  S12. a thread adopted into a project: an entry said there with a topic and
      no scope decision is read in the project now, so its slot moves with it
      and a correction made in the project replaces it.
  S14. a Split: an entry learned from the moved messages is read in the new
      thread, so its slot moves with it and a correction made there replaces
      it.

Every value is invented. A throwaway cseries-postgres through docker (removed
afterwards), or TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN.

  APP_ROOT=<repo> python3 scripts/test_memory_slots_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the one-live-entry-per-slot rule.")
    sys.exit(2)

FAILED = 0
E = 1
V241 = ROOT / "migrations" / "v241_memory_slots.sql"


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:700]}"))
    if not cond:
        FAILED += 1


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    from server import brain_service as _bs
    saved = (_bs._pool, _bs._entity_id)
    _bs._pool, _bs._entity_id = pool, E
    try:
        await run(pool, dsn)
    except Exception as e:  # noqa: BLE001 — a missing piece is a failure, not a crash
        check("the gate ran to its end", False, repr(e))
    finally:
        _bs._pool, _bs._entity_id = saved
        await pool.close()
    return 1 if FAILED else 0


async def run(pool, dsn):
    from mastercode import circles_threads as ct
    from mastercode.memory_engine import MemoryEngine
    from mastercode.runtime import close_turn as close
    from mastercode.runtime.tools import _exec_memory_curate, apply_memory_scope

    async def row(i):
        if i is None:
            return {}
        r = await pool.fetchrow(
            "SELECT * FROM entity_memory WHERE id = $1", int(i))
        if not r:
            return {}
        d = dict(r)
        d["live"] = d.get("status") == "active" and d.get("t_valid_until") is None
        return d

    async def live_like(words):
        return [dict(r) for r in await pool.fetch(
            "SELECT id, content FROM entity_memory WHERE entity_id = $1 AND content ILIKE '%'||$2||'%'"
            " AND status = 'active' AND t_valid_until IS NULL ORDER BY id", E, words)]

    async def block(scope, query):
        return await MemoryEngine(pool).build_memory_context(E, {}, max_tokens=16000, query=query,
                                                             source_context=scope)

    T1 = ct.descriptor_for((await ct.create_thread(pool, E, title="slot thread one"))["id"])
    T2 = ct.descriptor_for((await ct.create_thread(pool, E, title="slot thread two"))["id"])

    async def pref(text, topic, scope, *, turn=T1, session="s-slots"):
        item = {"text": text, "reason": "said so", "scope": scope}
        if topic is not None:
            item["topic"] = topic
        res = await close.apply(pool, E, {"preferences": [item]},
                                session_id=session, turn_scope=turn, turn_type={"name": "chat"})
        return (res.get("preferences") or [{}])[0]

    print("S1. close_turn: same type, scope and topic")
    a = await pref("Replies use British English spelling", "spelling", {"kind": "global"})
    b = await pref("Replies use American English spelling", "Spelling ", {"kind": "global"})
    ra, rb = await row(a.get("id")), await row(b.get("id"))
    check("S1 A is retired, its pointer on B, both in the slot",
          ra and not ra.get("live") and ra.get("resolution_pointer") == b.get("id") and rb.get("live")
          and ra.get("slot_topic") == rb.get("slot_topic") == "spelling"
          and ra.get("slot_scope") == rb.get("slot_scope") == "global", (ra, rb))
    check("S1 the answer says what it replaced", b.get("replaced") == a.get("id"), b)
    blk1 = await block(T1, "Which spelling should replies use?")
    blk2 = await block(T1, "hello there")
    check("S1 the memory block carries B only, matched or not",
          "American English spelling" in blk1 and "British" not in blk1
          and "American English spelling" in blk2 and "British" not in blk2, (blk1, blk2))
    check("S1 the line names its topic and #id",
          f"topic spelling, #{b.get('id')}]" in blk1, blk1)
    again = await pref("Replies use American English spelling", "spelling", {"kind": "global"})
    check("S1 the same words again are held, nothing written",
          again.get("status") == "already_held" and again.get("id") == b.get("id"), again)

    print("S2. memory_curate add")
    c1 = (await _exec_memory_curate({"op": "add", "about": "person", "topic": "coffee",
                                     "content": "The user takes coffee black"}, session_id="s-c")).get("result") or {}
    c2 = (await _exec_memory_curate({"op": "add", "about": "person", "topic": "coffee",
                                     "content": "The user takes coffee with oat milk"},
                                    session_id="s-c")).get("result") or {}
    r1 = await row(c1.get("memory_id") if isinstance(c1, dict) else None)
    check("S2 the older add is retired into the newer", r1 and not r1.get("live")
          and r1.get("resolution_pointer") == c2.get("memory_id") and c2.get("replaced") == c1.get("memory_id"),
          (c1, c2, r1))
    check("S2 the answer shows the topics in use where it was written",
          any(t.get("topic") == "coffee" for t in ((c2.get("topics_here") or {}).get("topics") or [])), c2)
    # A correction by topic, with no scope named, replaces what it corrects
    # where this conversation reads it: the global spelling entry.
    c3 = (await _exec_memory_curate({"op": "add", "about": "person", "topic": "spelling",
                                     "content": "Replies use Canadian English spelling"},
                                    session_id="s-c")).get("result") or {}
    check("S2 an add with a topic and no scope replaces the entry in that topic",
          c3.get("replaced") == b.get("id") and not (await row(b.get("id"))).get("live"), c3)

    print("S3. relationship notes")
    n1 = (await _exec_memory_curate({"op": "add", "about": "person", "memory_type": "relationship",
                                     "topic": "sam", "content": "Sam and the user speak informally"},
                                    session_id="s-r")).get("result") or {}
    n2 = (await _exec_memory_curate({"op": "add", "about": "person", "memory_type": "relationship",
                                     "topic": "Sam", "content": "Sam and the user speak formally now"},
                                    session_id="s-r")).get("result") or {}
    check("S3 the newer note about the same person replaces the older",
          n1.get("memory_id") and not (await row(n1.get("memory_id"))).get("live")
          and n2.get("replaced") == n1.get("memory_id"), (n1, n2))

    print("S4/S5. taught rules")
    eng = MemoryEngine(pool)
    await eng._store_teaching_memory(E, {"content": "Always give distances in kilometres",
                                         "trigger": "remember this:"}, "s-t", source_context=T1)
    k1 = (await _exec_memory_curate({"op": "key", "content": "Always give distances in kilometres",
                                     "topic": "units"}, session_id="s-t")).get("result") or {}
    await asyncio.sleep(0.05)
    await eng._store_teaching_memory(E, {"content": "Always give distances in miles",
                                         "trigger": "remember this:"}, "s-t", source_context=T1)
    k2 = (await _exec_memory_curate({"op": "key", "content": "Always give distances in miles",
                                     "topic": "units"}, session_id="s-t")).get("result") or {}
    check("S4 two taught rules in one slot: the newer one stays",
          k2.get("status") == "keyed" and k2.get("replaced") == [k1.get("memory_id")]
          and not (await row(k1.get("memory_id"))).get("live"), (k1, k2))
    p5 = await pref("Give distances in nautical miles", "units", {"kind": "global"})
    check("S5 a plain preference in a rule's slot is refused, the rule untouched",
          p5.get("status") == "held_by_rule" and p5.get("id") == k2.get("memory_id")
          and (await row(k2.get("memory_id"))).get("live")
          and not await live_like("nautical"), p5)
    plain = await pref("Dates are written day first", "dates", {"kind": "global"})
    await eng._store_teaching_memory(E, {"content": "Write dates as year-month-day",
                                         "trigger": "remember this:"}, "s-t", source_context=T1)
    k3 = (await _exec_memory_curate({"op": "key", "content": "Write dates as year-month-day",
                                     "topic": "dates"}, session_id="s-t")).get("result") or {}
    check("S5 a rule keyed into a plain preference's slot replaces it (the person's rule outranks)",
          plain.get("id") and k3.get("replaced") == [plain.get("id")]
          and not (await row(plain.get("id"))).get("live"), (plain, k3))

    print("S6. the database holds the rule")
    try:
        await pool.execute("INSERT INTO entity_memory (entity_id, content, memory_type, slot_topic, slot_scope)"
                           " VALUES ($1, 'a second live row', 'preference', 'units', 'global')", E)
        check("S6 a second live row in a slot raises UniqueViolationError", False, "insert went through")
    except asyncpg.UniqueViolationError as e:
        check("S6 a second live row in a slot raises UniqueViolationError",
              e.constraint_name == "ux_entity_memory_live_slot", e.constraint_name)
    except Exception as e:  # noqa: BLE001
        check("S6 a second live row in a slot raises UniqueViolationError", False, repr(e))
    try:
        await pool.execute("INSERT INTO entity_memory (entity_id, content, memory_type, slot_topic, slot_scope)"
                           " VALUES ($1, 'x', 'preference', 'Not Normalized', 'global')", E)
        check("S6 a topic that is not the normalized form is refused", False)
    except asyncpg.CheckViolationError:
        check("S6 a topic that is not the normalized form is refused", True)
    except Exception as e:  # noqa: BLE001
        check("S6 a topic that is not the normalized form is refused", False, repr(e))

    print("S7. goals")
    g1 = (await close.apply(pool, E, {"goal": {"set": "Book a flight to Lisbon"}}, session_id="s-g",
                            turn_scope=T2)).get("goal", {}).get("set", {})
    g2 = (await close.apply(pool, E, {"goal": {"set": "Book a train to Porto instead"}}, session_id="s-g",
                            turn_scope=T2)).get("goal", {}).get("set", {})
    st = await close.thread_state(pool, E, T2)
    check("S7 one open goal per thread: the newest", g2.get("replaced") == [g1.get("id")]
          and not (await row(g1.get("id"))).get("live"), (g1, g2))
    check("S7 the thread state dates it", st and "Goal (set " in st and "Porto" in st and "Lisbon" not in st, st)

    print("S8. cited facts")
    from mastercode.memory_consolidation import MemoryConsolidator
    cons = MemoryConsolidator(pool)
    P = {"kind": "project", "ref": "7"}
    f1 = await cons._insert_new_memory(E, "The library opens at 9am on Saturdays", "cited_fact", 1.0, None,
                                       metadata_extra={"written_by": "task_record", "source_context": P,
                                                       "cited": {"finding": "1", "source": "https://a.example"}})
    f2 = await cons._insert_new_memory(E, "The library opens at 10am on Saturdays", "cited_fact", 1.0, None,
                                       metadata_extra={"written_by": "task_record", "source_context": P,
                                                       "cited": {"finding": "2", "source": "https://b.example"}})
    r1, r2 = await row(f1), await row(f2)
    check("S8 two sources that disagree: both stay live, neither is slotted",
          r1.get("live") and r2.get("live") and r1.get("slot_topic") is None and r2.get("slot_scope") is None,
          (r1, r2))
    try:
        from mastercode.memory_slots import write_in_slot
        await write_in_slot(pool, E, content="x", memory_type="cited_fact", scope="global", topic="library")
        check("S8 the slot writer refuses a cited fact", False)
    except ValueError:
        check("S8 the slot writer refuses a cited fact", True)
    except Exception as e:  # noqa: BLE001
        check("S8 the slot writer refuses a cited fact", False, repr(e))

    print("S9. keying and rescoping")
    old = await pref("Meetings go on Tuesdays", None, {"kind": "global"})
    await asyncio.sleep(0.05)
    new = await pref("Meetings go on Thursdays", "meetings", {"kind": "global"})
    k = (await _exec_memory_curate({"op": "key", "memory_id": old.get("id"), "topic": "meetings"},
                                   session_id="s-k")).get("result") or {}
    check("S9 keying an older row into a newer row's slot retires the older into the newer",
          k.get("status") == "replaced_by" and k.get("replaced_by") == new.get("id")
          and (await row(old.get("id"))).get("resolution_pointer") == new.get("id")
          and (await row(new.get("id"))).get("live"), k)
    here = await pref("Sign emails as the team", "sign_off", {"kind": "thread"}, turn=T1)
    glob = await pref("Sign emails with a first name", "sign_off", {"kind": "global"})
    check("S9 a thread's slot and the global slot of one topic are two slots",
          (await row(here.get("id"))).get("live") and (await row(glob.get("id"))).get("live"), (here, glob))
    mv = await apply_memory_scope(pool, E, here.get("id"), "global", "")
    hr, gr = await row(here.get("id")), await row(glob.get("id"))
    check("S9 rescoping a topic'd row into a held slot settles it (the moved row is the decision now)",
          mv.get("status") == "ok" and hr.get("slot_scope") == "global" and hr.get("live")
          and not gr.get("live"), (mv, hr, gr))

    print("S10. a thread delete")
    T3 = ct.descriptor_for((await ct.create_thread(pool, E, title="slot thread three"))["id"])
    keep = await pref("The weekly report is due on Monday", "report_day", {"kind": "global"}, turn=T1)
    repl = await pref("The weekly report is due on Friday", "report_day", {"kind": "global"}, turn=T3,
                      session="s-del")
    check("S10 fixture: the replacement made in thread three holds the slot",
          not (await row(keep.get("id"))).get("live") and (await row(repl.get("id"))).get("live"))
    await ct.delete_thread(pool, E, T3["ref"])
    lives = await live_like("weekly report is due")
    check("S10 deleting that thread brings the replaced row back, one live entry in the slot",
          [x["id"] for x in lives] == [keep.get("id")], lives)

    print("S12. a thread adopted into a project")
    from server.routes.projects import NEW_PROJECT_SQL, adopt_thread_core
    T4 = ct.descriptor_for((await ct.create_thread(pool, E, title="slot thread four"))["id"])
    proj = int((await pool.fetchrow(NEW_PROJECT_SQL, E, "Invented garden project", "invented"))["id"])
    P4 = {"kind": "project", "ref": str(proj)}

    async def said_in(scope, session, words):
        # A turn row of the session in that place: memory_curate reads where
        # it is said from the session's newest turn (session_source_context).
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, source_session,"
            " project_id, metadata) VALUES ($1, $2, 'turn', 0.5, $3, $4, $5::jsonb)",
            E, words, session, proj if scope["kind"] == "project" else None,
            json.dumps({"role": "user", "source_context": scope,
                        "app_id": "workbench" if scope["kind"] == "project" else "circles"}))

    await said_in(T4, "s-adopt", "invented message about the ferns")
    w1 = (await _exec_memory_curate({"op": "add", "about": "person", "topic": "watering",
                                     "content": "The ferns are watered on Sundays"},
                                    session_id="s-adopt")).get("result") or {}
    w1row = await row(w1.get("memory_id") if isinstance(w1, dict) else None)
    check("S12 fixture: the entry is slotted where it was said",
          w1row.get("slot_scope") == f"thread:{T4['ref']}" and w1row.get("slot_topic") == "watering", w1row)
    await adopt_thread_core(pool, E, proj, "thread", T4["ref"])
    w1row = await row(w1.get("memory_id"))
    check("S12 the adopted entry is read in the project, and its slot says so",
          json.loads(w1row.get("metadata") or "{}").get("source_context") == P4
          and w1row.get("slot_scope") == f"project:{proj}", w1row)
    await said_in(P4, "s-proj", "invented message in the project")
    w2 = (await _exec_memory_curate({"op": "add", "about": "person", "topic": "watering",
                                     "content": "The ferns are watered on Wednesdays"},
                                    session_id="s-proj")).get("result") or {}
    check("S12 a correction made in the project replaces it: one live entry there",
          w2.get("replaced") == w1.get("memory_id") and not (await row(w1.get("memory_id"))).get("live")
          and [x["content"] for x in await live_like("ferns are watered")]
          == ["The ferns are watered on Wednesdays"], (w2, await live_like("ferns are watered")))
    # The project tag a row has is the tag a row written there has: none
    # (circles_threads.carry_learned), so an adopted entry is read by a
    # project-scoped turn type exactly as the correction made there is.
    check("S12 the adopted entry carries the project tag a row written in the project has",
          (await row(w1.get("memory_id"))).get("project_id") == (await row(w2.get("memory_id"))).get("project_id"),
          ((await row(w1.get("memory_id"))).get("project_id"), (await row(w2.get("memory_id"))).get("project_id")))

    print("S13. lock order: a replace and a key of one row")
    # key_row and move_scope lock the row, then the slot. A replace
    # (write_in_slot with also_retire) must take the same order, or a key of
    # #R and a replace of #R into one slot wait on each other until Postgres
    # aborts one of them.
    from mastercode.memory_slots import _lock_slot, write_in_slot
    tea = await pref("Tea is served black", "tea", {"kind": "global"})
    conn = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    await conn.fetchrow("SELECT id FROM entity_memory WHERE id = $1 FOR UPDATE", int(tea.get("id")))
    writer = asyncio.create_task(write_in_slot(
        pool, E, content="Tea is served with lemon", memory_type="preference", scope="global",
        topic="tea_service", also_retire=(int(tea.get("id")),)))
    await asyncio.sleep(0.5)
    keyer_err = None
    try:
        await asyncio.wait_for(_lock_slot(conn, E, "preference", "global", "tea_service"), timeout=10)
    except Exception as e:  # noqa: BLE001 — a deadlock lands here or in the writer
        keyer_err = e
    finally:
        try:
            await (tr.rollback() if keyer_err else tr.commit())
        finally:
            await pool.release(conn)
    try:
        wrote = await asyncio.wait_for(writer, timeout=10)
    except Exception as e:  # noqa: BLE001
        wrote = e
    check("S13 the row's holder takes the slot without a deadlock, and the replace then lands",
          keyer_err is None and isinstance(wrote, dict) and wrote.get("status") == "stored"
          and wrote.get("replaced") == [int(tea.get("id"))], (repr(keyer_err), wrote))

    print("S14. a Split")
    # An entry learned from the messages a Split moves goes with them
    # (metadata.from_turns, circles_threads._split_in_transaction): it is read
    # in the new thread, so its slot must say so, and a correction made there
    # replaces it.
    T6 = ct.descriptor_for((await ct.create_thread(pool, E, title="slot thread six"))["id"])
    said6 = []
    for i, (role, words) in enumerate((("user", "invented talk about the porch"), ("assistant", "Noted."),
                                       ("user", "invented talk about the plant pots"),
                                       ("assistant", "Terracotta it is."))):
        said6.append(int(await pool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, source_session,"
            " metadata, created_at) VALUES ($1, $2, 'turn', 0.5, 's-split',"
            " $3::jsonb, now() - make_interval(mins => $4)) RETURNING id",
            E, words, json.dumps({"role": role, "source_context": T6, "app_id": "circles"}), 10 - i)))
    pots = await write_in_slot(pool, E, content="Plant pots are terracotta", memory_type="preference",
                               scope=f"thread:{T6['ref']}", topic="plant_pots",
                               metadata={"source_context": T6, "from_turns": said6[2:]})
    split = await ct.split_thread(pool, E, T6["ref"], from_turn_id=said6[2], title="pots")
    T7 = ct.descriptor_for(split["id"])
    prow = await row(pots.get("id"))
    pmd = prow.get("metadata")
    pmd = json.loads(pmd) if isinstance(pmd, str) else dict(pmd or {})
    check("S14 fixture: the entry learned from the moved messages is the new thread's",
          pmd.get("source_context") == T7, prow)
    check("S14 ...and its slot says it is read there",
          prow.get("slot_scope") == f"thread:{T7['ref']}" and prow.get("slot_topic") == "plant_pots", prow)
    fix = await write_in_slot(pool, E, content="Plant pots are glazed", memory_type="preference",
                              scope=f"thread:{T7['ref']}", topic="plant_pots",
                              metadata={"source_context": T7})
    check("S14 a correction made in the new thread replaces it: one live entry there",
          fix.get("replaced") == [pots.get("id")] and [x["content"] for x in await live_like("plant pots are")]
          == ["Plant pots are glazed"], (fix, await live_like("plant pots are")))

    print("S11. v241 on existing rows")
    legacy = []
    for content, mtype, md in (
            ("legacy decided thread pref", "preference", {"about": {"kind": "thread", "ref": T2["ref"]}}),
            ("legacy said-in pref", "preference", {"source_context": {"kind": "project", "ref": "7"}}),
            ("legacy rule", "preference", {"rule_kind": "taught", "source_context": T1}),
            ("legacy global decision", "preference", {"about": {"kind": "global"}, "source_context": T1}),
            ("legacy note", "relationship", {"source_context": T2}),
            ("legacy goal", "goal", {"source_context": T2}),
            ("legacy plain", "preference", {})):
        legacy.append(await pool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, metadata) VALUES ($1, $2, $3, $4::jsonb)"
            " RETURNING id", E, content, mtype, json.dumps(md)))
    summ = await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, metadata) VALUES ($1, 'legacy summary',"
        " 'summary', $2::jsonb) RETURNING id", E, json.dumps({"source_context": T2}))
    # A thread whose summary chain recorded one topic flip, as the retired
    # writer left it: v1 superseded, v2 live. Its markers (the Circles split
    # suggestions, circles_threads.markers) are read from that chain.
    tid5 = str((await ct.create_thread(pool, E, title="slot thread five"))["id"])
    T5 = ct.descriptor_for(tid5)
    for version, topic, live_row in ((1, "Lisbon trip", False), (2, "Porto trip", True)):
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, metadata, t_valid_until)"
            " VALUES ($1, $2, 'summary', $3::jsonb, CASE WHEN $4 THEN NULL ELSE now() END)",
            E, f"invented summary v{version}", json.dumps({
                "source_kind": "thread_consolidator", "source_context": T5,
                "summary_version": version, "segment_topic": topic}), live_row)
    flips_before = [m.get("topic") for m in await ct.markers(pool, E, tid5)]
    for _ in range(2):
        r = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(V241))
        check("S11 v241 runs clean (twice)", r.returncode == 0, r.stderr[-400:])
    from mastercode.memory_slots import slot_scope_for
    got = await pool.fetch("SELECT id, memory_type, metadata, slot_scope, slot_topic FROM entity_memory"
                           " WHERE id = ANY($1::int[]) ORDER BY id", legacy)
    want = ["thread:" + T2["ref"], "project:7", "global", "global", "global", "thread:" + T2["ref"], "global"]
    check("S11 each existing row gets its scope by the writers' own rule",
          [g["slot_scope"] for g in got] == want, [g["slot_scope"] for g in got])
    check("S11 ...the SQL and the Python rule agree", all(
        g["slot_scope"] == slot_scope_for(g["memory_type"], g["metadata"]) for g in got),
        [(g["slot_scope"], slot_scope_for(g["memory_type"], g["metadata"])) for g in got])
    check("S11 no topic is guessed for an existing row", all(g["slot_topic"] is None for g in got))
    sr = await row(summ)
    check("S11 a live rolling summary is archived, kept for the as-of view",
          sr.get("status") == "archived" and sr.get("status_reason") == "summary writer retired", sr)
    flips_after = [m.get("topic") for m in await ct.markers(pool, E, tid5)]
    check("S11 a thread's topic markers keep every flip its chain recorded, the newest included",
          flips_before == ["Porto trip"] and flips_after == flips_before, (flips_before, flips_after))


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else rc)
