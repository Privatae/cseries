#!/usr/bin/env python3
"""Gate: the thread a message belongs to is placed in software and, when that
is unclear, chosen by the Host in its own turn (runtime/thread_choice.py,
server/connectors/thread_pick.py), against a real kernel schema.

  C. Circles: a clear score-based match joins its topic with no model call
     and offers nothing; an unclear message starts an untitled topic of its
     own and carries the topics it may continue (the one the person was just
     in when the message opens like a reply), kept on the new topic's marker
     for a send that asked first (pending_choice); with nothing to offer it
     carries none;
  M. the Host picks another topic in its turn: the person's message moves
     there with every row the new topic held (a rule taught in the message),
     its markers go, a marker in the chosen topic says it was picked and why,
     and the new topic is folded into the chosen one (merged_into) so a page
     naming it sends there (live_thread_id) and no empty topic is left;
  N. from a thread with its own history the move leaves a marker where it
     landed instead, and nothing is folded;
  T. ...and what was learned from the message moves with it, as a Split moves
     it: a rule taught in the message, the place it was taught, the
     preference it retired (its stamp) and a learned row's slot are the
     picked thread's, so deleting that thread takes the rule and brings the
     preference back, and deleting the thread it left keeps both as they are;
  W. a rule whose write lands after the message row is waited for, and moves;
  R. a turn re-homed to the project its work touched (turn_home.rehome_turn)
     carries what was learned from its message the same way: the rule, the
     place it was taught (so a narrowed rule rides there), the preference it
     retired and a learned row's slot are that project's; the rule's write
     is waited for; a carry that fails moves nothing;
  P. a rule the Host's pick files in a project, from a thread that stays or a
     new topic folded in, ends as a rule taught there does (no project tag);
  I. the Workbench's pages are scored the same way (page_scores), only the
     pages asked about.

Invented values only. A throwaway cseries-postgres (docker, removed
afterwards), or TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
from turn_source import turn_source  # session.py and the modules split out of it

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
    from mastercode.memory_engine import MemoryEngine, write_turn_row
    from server.connectors import thread_pick as tp
    from mastercode.runtime import thread_choice as tc

    async def topic(title, *texts):
        tid = str((await ct.create_thread(pool, E, title=title))["id"])
        for i, t in enumerate(texts):
            await write_turn_row(pool, E, "s-tc", "user" if i % 2 == 0 else "assistant", t,
                                 tags={"source_context": ct.descriptor_for(tid), "app_id": "circles"})
        return tid

    X = await topic("Orchid care", "how often should the orchids be watered in winter",
                    "Every ten days in winter.")
    C = await topic("Kitchen budget", "the kitchen renovation budget is four thousand",
                    "Noted, four thousand for the kitchen.")

    print("C. Circles placement")
    n_threads = await pool.fetchval("SELECT count(*) FROM circles_threads WHERE deleted_at IS NULL")
    land = await tp.pick_topic(pool, E, "how often should the orchids be watered in winter",
                               current_thread_id=C)
    check("C a clear match joins its topic, nothing offered, no topic made",
          land.thread_id == X and not land.new and not land.choice
          and await pool.fetchval("SELECT count(*) FROM circles_threads WHERE deleted_at IS NULL") == n_threads,
          land)
    land = await tp.pick_topic(pool, E, "ok and also the cabinets for that", current_thread_id=C)
    P = land.thread_id
    check("C an unclear message that opens like a reply starts an untitled topic of its own",
          land.new and P not in (C, X) and land.title in ("", None)
          and await pool.fetchval("SELECT title FROM circles_threads WHERE id = $1::uuid", P) is None, land)
    check("C ...and offers the topic the person was just in",
          [c["ref"] for c in land.choice] == [C], land.choice)
    check("C the offer is kept on the new topic's marker for a send that asked first",
          [c["ref"] for c in await tp.pending_choice(pool, E, P)] == [C])
    alone = await tp.pick_topic(pool, E, "plan a weekend in the mountains", current_thread_id=None,
                                current_known=False)
    check("C with nothing to offer it starts a topic and carries no choice", alone.new and not alone.choice, alone)

    print("M. the Host's pick moves it, and folds the new topic")
    Ps = ct.descriptor_for(P)
    urow = await write_turn_row(pool, E, "s-tc", "user", "ok and also the cabinets for that",
                                tags={"source_context": Ps, "app_id": "circles"})
    rule = await MemoryEngine(pool)._store_teaching_memory(
        E, {"content": "Cabinets are always quoted with fitting", "trigger": "remember this:"}, "s-tc",
        source_context=Ps)
    choice = tc.from_frame({"thread_choice": tc.frame_value(
        surface="circles", candidates=list(land.choice),
        landed={"kind": "thread", "ref": P, "title": "a new topic", "provisional": True})})
    choice.picked = {"kind": "thread", "ref": C, "title": "Kitchen budget", "new": False,
                     "reason": "it is about the kitchen"}

    async def _row_task():
        return urow
    task = asyncio.ensure_future(_row_task())
    res = await tc.move_exchange(pool, E, choice, src=Ps, user_row_task=task)
    sc = await pool.fetchval("SELECT metadata->'source_context' FROM entity_memory WHERE id = $1", urow)
    check("M the person's message moved to the chosen topic",
          res.get("moved") and json.loads(sc) == {"kind": "thread", "ref": C}, (res, sc))
    rsc = await pool.fetchval("SELECT metadata->'source_context' FROM entity_memory WHERE id = $1", rule["id"])
    check("M a rule taught in the message went with it", json.loads(rsc) == {"kind": "thread", "ref": C}, rsc)
    gone = await pool.fetchrow("SELECT deleted_at, merged_into FROM circles_threads WHERE id = $1::uuid", P)
    check("M the new topic is folded into the chosen one",
          gone["deleted_at"] is not None and gone["merged_into"] == f"thread:{C}", dict(gone))
    check("M a page that still names it sends to the chosen topic",
          await ct.live_thread_id(pool, E, P) == C)
    check("M no live row is left in the folded topic",
          await pool.fetchval("SELECT count(*) FROM entity_memory WHERE status = 'active'"
                              " AND metadata->'source_context'->>'ref' = $1", P) == 0)
    mk = await pool.fetchval("SELECT content FROM entity_memory WHERE metadata->>'source_kind' = $1"
                             " AND metadata->'source_context'->>'ref' = $2 AND content LIKE '%picked%'",
                             tp.TOPIC_MARKER_KIND, C)
    check("M a marker in the chosen topic says it was picked and why",
          mk == "Circles message, picked this thread: it is about the kitchen", mk)

    print("N. from a thread with its own history")
    choice2 = tc.from_frame({"thread_choice": tc.frame_value(
        surface="connector", platform="telegram", candidates=[{"kind": "thread", "ref": X, "title": "Orchid care"}],
        landed={"kind": "thread", "ref": C, "title": "Kitchen budget", "provisional": False})})
    choice2.picked = {"kind": "thread", "ref": X, "title": "Orchid care", "new": False, "reason": "about orchids"}
    u2 = await write_turn_row(pool, E, "s-tc", "user", "and the orchid pot size?",
                              tags={"source_context": ct.descriptor_for(C), "app_id": "circles"})

    async def _t2():
        return u2
    res2 = await tc.move_exchange(pool, E, choice2, src=ct.descriptor_for(C), user_row_task=asyncio.ensure_future(_t2()))
    left = await pool.fetchval("SELECT content FROM entity_memory WHERE metadata->>'source_kind' = 'thread_marker'"
                               " AND metadata->'source_context'->>'ref' = $1", C)
    check("N the place it landed keeps a marker saying where it went, and stays",
          res2.get("moved") and left and left.startswith("→ This exchange moved to Orchid care")
          and await pool.fetchval("SELECT deleted_at FROM circles_threads WHERE id = $1::uuid", C) is None,
          (res2, left))

    print("T. a rule taught in a message the Host files in another existing thread")

    async def taught_and_moved(words, landed_title, picked_title, plain_title):
        # Where it landed and where the Host files it both have a history of
        # their own, so nothing is folded (N): the move takes the non-fold arm.
        A = await topic(landed_title, f"first thing said in {landed_title}", "Noted.")
        B = await topic(picked_title, f"first thing said in {picked_title}", "Noted.")
        X_ = await topic(plain_title, f"first thing said in {plain_title}", "Noted.")
        As, Bs = ct.descriptor_for(A), ct.descriptor_for(B)
        # The same words inferred earlier as a plain preference in a third
        # thread: the teaching retires it, stamped with the conversation that
        # taught it (memory_engine._retire_into_rule).
        plain = await pool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata)"
            " VALUES ($1, $2, 'preference', 0.6, 'active', $3::jsonb) RETURNING id",
            E, words, json.dumps({"source_context": ct.descriptor_for(X_)}))
        await pool.execute("UPDATE entity_memory SET slot_scope = $2 WHERE id = $1", plain,
                           f"thread:{X_}")
        m = await write_turn_row(pool, E, "s-tc", "user", f"remember this: {words}",
                                 tags={"source_context": As, "app_id": "circles"})
        # As the turn writes it (session._keep_taught): where the message
        # landed, from the message's own row.
        r = await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": words, "trigger": "remember this:"}, "s-tc", source_context=As,
            from_turns=[int(m)])
        # A preference a pass learned from the same message, in a slot of the
        # thread it landed in.
        from mastercode.memory_slots import write_in_slot
        learned = await write_in_slot(
            pool, E, content=f"{words} (learned)", memory_type="preference", scope=f"thread:{A}",
            topic="learned_from_message", metadata={"source_context": As, "from_turns": [int(m)]})
        ch = tc.from_frame({"thread_choice": tc.frame_value(
            surface="connector", platform="telegram",
            candidates=[{"kind": "thread", "ref": B, "title": picked_title}],
            landed={"kind": "thread", "ref": A, "title": landed_title, "provisional": False})})
        ch.picked = {"kind": "thread", "ref": B, "title": picked_title, "new": False, "reason": "it belongs there"}

        async def _t():
            return m
        res = await tc.move_exchange(pool, E, ch, src=As, user_row_task=asyncio.ensure_future(_t()))
        return {"A": A, "B": B, "As": As, "Bs": Bs, "m": int(m), "rule": int(r["id"]), "plain": int(plain),
                "learned": int(learned["id"]), "res": res}

    async def row(rid):
        r = await pool.fetchrow("SELECT status, t_valid_until, metadata, slot_scope, slot_topic"
                                " FROM entity_memory WHERE id = $1", rid)
        md = r["metadata"]
        return {"status": r["status"], "live": r["status"] == "active" and r["t_valid_until"] is None,
                "md": json.loads(md) if isinstance(md, str) else dict(md or {}),
                "slot_scope": r["slot_scope"], "slot_topic": r["slot_topic"]}

    from mastercode.memory_engine import rule_taught_places
    t1 = await taught_and_moved("Quotes always include delivery", "Shed plans", "Bike repair", "Old notes")
    check("T the move took the non-fold arm and the message is in the picked thread",
          t1["res"].get("moved") and (await row(t1["m"]))["md"].get("source_context") == t1["Bs"]
          and await pool.fetchval("SELECT deleted_at FROM circles_threads WHERE id = $1::uuid", t1["A"]) is None,
          t1["res"])
    r1 = await row(t1["rule"])
    check("T the rule taught in the message is tagged with the picked thread",
          r1["md"].get("source_context") == t1["Bs"], r1["md"].get("source_context"))
    check("T ...the place it was taught is the picked thread (where a narrowed rule still rides)",
          rule_taught_places(r1["md"]) == [t1["Bs"]], r1["md"].get("held_in"))
    check("T ...and, unscoped, its slot is still read everywhere",
          r1["slot_scope"] == "global" and r1["live"], r1)
    p1 = await row(t1["plain"])
    check("T the preference the teaching retired is stamped with the picked thread",
          not p1["live"] and {k: p1["md"].get("superseded_in", {}).get(k) for k in ("kind", "ref")} == t1["Bs"],
          p1["md"].get("superseded_in"))
    l1 = await row(t1["learned"])
    check("T a row learned from the message moved with it, and its slot follows",
          l1["md"].get("source_context") == t1["Bs"] and l1["slot_scope"] == f"thread:{t1['B']}"
          and l1["slot_topic"] == "learned_from_message" and l1["live"], l1)

    await ct.delete_thread(pool, E, t1["B"])
    r1 = await row(t1["rule"])
    check("T deleting the thread the message now lives in takes the rule with it",
          r1["status"] == "archived", (r1["status"], r1["md"].get("source_context"), r1["md"].get("held_in")))
    check("T ...brings back the preference the teaching retired",
          (await row(t1["plain"]))["live"], await row(t1["plain"]))
    check("T ...and takes the row learned from the message",
          (await row(t1["learned"]))["status"] == "archived")

    t2 = await taught_and_moved("Invoices go out on Fridays", "Errands", "Billing", "Scratch")
    await ct.delete_thread(pool, E, t2["A"])
    r2 = await row(t2["rule"])
    check("T deleting the thread the message left keeps the rule, still the picked thread's",
          r2["live"] and r2["md"].get("source_context") == t2["Bs"],
          (r2["status"], r2["md"].get("source_context"), r2["md"].get("held_in")))
    check("T ...keeps the preference the teaching retired retired",
          not (await row(t2["plain"]))["live"])
    l2 = await row(t2["learned"])
    check("T ...and keeps the row learned from the message, in its slot",
          l2["live"] and l2["slot_scope"] == f"thread:{t2['B']}", l2)

    print("W. a rule whose write lands after the message row")
    # session._keep_taught writes the rule once the message row exists, and
    # the move waits on that same row: the move waits for the rule's write
    # too (learned_task), or the rule can land after it, left behind.
    A3 = await topic("Porch", "first thing said about the porch", "Noted.")
    B3 = await topic("Gutters", "first thing said about the gutters", "Noted.")
    A3s, B3s = ct.descriptor_for(A3), ct.descriptor_for(B3)
    row_ready = asyncio.get_running_loop().create_future()

    async def _user_row():
        return await row_ready
    row_task = asyncio.ensure_future(_user_row())

    async def _keep_taught():
        rid = await row_task
        await asyncio.sleep(0.5)
        return await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": "Gutters are cleared in October", "trigger": "remember this:"}, "s-tc",
            source_context=A3s, from_turns=[int(rid)])
    taught_task = asyncio.ensure_future(_keep_taught())
    m3 = await write_turn_row(pool, E, "s-tc", "user", "remember this: Gutters are cleared in October",
                              tags={"source_context": A3s, "app_id": "circles"})
    row_ready.set_result(m3)
    ch3 = tc.from_frame({"thread_choice": tc.frame_value(
        surface="connector", platform="telegram", candidates=[{"kind": "thread", "ref": B3, "title": "Gutters"}],
        landed={"kind": "thread", "ref": A3, "title": "Porch", "provisional": False})})
    ch3.picked = {"kind": "thread", "ref": B3, "title": "Gutters", "new": False, "reason": "about gutters"}
    try:
        res3 = await tc.move_exchange(pool, E, ch3, src=A3s, user_row_task=row_task, learned_task=taught_task)
    except TypeError as e:
        res3 = {"error": repr(e)}
    rule3 = await taught_task
    r3 = await row(int(rule3["id"]))
    check("W the move waited for the rule's write, and the rule is the picked thread's",
          res3.get("moved") and r3["md"].get("source_context") == B3s
          and rule_taught_places(r3["md"]) == [B3s], (res3, r3["md"].get("source_context")))
    # The turn hands the move that write: the task _keep_taught runs in.
    import ast
    tree = ast.parse(turn_source(ROOT))
    starts = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "_taught_task" for t in n.targets)
              and isinstance(n.value, ast.Call) and "_keep_taught" in ast.dump(n.value)]
    hands = [k for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "move_exchange"
             for k in n.keywords if k.arg == "learned_task"
             and isinstance(k.value, ast.Name) and k.value.id == "_taught_task"]
    check("W the turn starts the rule's write as _taught_task and hands it to the move",
          len(starts) == 1 and len(hands) == 1, (len(starts), len(hands)))

    print("R. a rule taught in a turn re-homed to the project its work touched")
    from mastercode.memory_engine import rule_place_clause, typed_rule_clause
    from mastercode.memory_slots import write_in_slot
    from mastercode.runtime import turn_home as th

    async def project(name):
        return int(await pool.fetchval("INSERT INTO projects (entity_id, name, status) VALUES ($1, $2, 'active')"
                                       " RETURNING id", E, name))

    def ps(pid):
        return {"kind": "project", "ref": str(pid)}

    def done(v):
        f = asyncio.get_running_loop().create_future()
        f.set_result(v)
        return f

    async def rides(rule_id, place):
        sql, args = rule_place_clause(place, 2)
        return bool(await pool.fetchval(f"SELECT 1 FROM entity_memory WHERE id = $1 AND {sql}", rule_id, *args))

    async def taught_in(pid, words, older):
        # The same words inferred earlier as a plain preference in a Circles
        # thread: the teaching retires it, stamped with the turn's project.
        plain = await pool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata, slot_scope)"
            " VALUES ($1, $2, 'preference', 0.6, 'active', $3::jsonb, $4) RETURNING id",
            E, words, json.dumps({"source_context": ct.descriptor_for(older)}), f"thread:{older}")
        m = await write_turn_row(pool, E, "s-rh", "user", f"remember this: {words}", project_id=pid,
                                 tags={"source_context": ps(pid), "app_id": "workbench"})
        r = await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": words, "trigger": "remember this:"}, "s-rh", source_context=ps(pid),
            from_turns=[int(m)])
        learned = await write_in_slot(
            pool, E, content=f"{words} (learned)", memory_type="preference", scope=f"project:{pid}",
            topic="rehomed_learned", metadata={"source_context": ps(pid), "from_turns": [int(m)]})
        return int(m), int(r["id"]), int(plain), int(learned["id"])

    R1, R2 = await project("Seed shed"), await project("Seed garden")
    Rx = await topic("Seed notes", "first thing said about seeds", "Noted.")
    rm, rr, rp, rl = await taught_in(R1, "Seed orders go through the co-op", Rx)
    rres = await th.rehome_turn(pool, E, from_project=R1, to_project=R2, user_row_task=done(rm),
                                session_id="s-rh")
    um = await pool.fetchrow("SELECT project_id, metadata FROM entity_memory WHERE id = $1", rm)
    check("R the user row moved to the project the turn worked on",
          rres.get("moved_user_row") and um["project_id"] == R2
          and json.loads(um["metadata"]).get("source_context") == ps(R2), (rres, dict(um)))
    q = await row(rr)
    check("R the rule taught in the turn is tagged with that project",
          q["md"].get("source_context") == ps(R2), q["md"].get("source_context"))
    check("R ...the place it was taught is that project",
          rule_taught_places(q["md"]) == [ps(R2)], q["md"].get("held_in"))
    await pool.execute("UPDATE entity_memory SET metadata = metadata || jsonb_build_object('about', $2::jsonb)"
                       " WHERE id = $1", rr, json.dumps(ct.descriptor_for(Rx)))
    on_new, on_old = await rides(rr, ps(R2)), await rides(rr, ps(R1))
    await pool.execute("UPDATE entity_memory SET metadata = metadata - 'about' WHERE id = $1", rr)
    check("R ...so narrowed by the Host it still rides in that project, not the one the turn left",
          on_new and not on_old, (on_new, on_old))
    check("R ...and it keeps no project tag, as a rule taught in that project has none",
          await pool.fetchval("SELECT project_id FROM entity_memory WHERE id = $1", rr) is None
          and q["slot_scope"] == "global" and q["live"], q)
    p_ = await row(rp)
    check("R the preference the teaching retired is stamped with that project",
          not p_["live"] and {k: p_["md"].get("superseded_in", {}).get(k) for k in ("kind", "ref")} == ps(R2),
          p_["md"].get("superseded_in"))
    l_ = await row(rl)
    check("R a row learned from the message moved with it, and its slot follows",
          l_["md"].get("source_context") == ps(R2) and l_["slot_scope"] == f"project:{R2}"
          and l_["slot_topic"] == "rehomed_learned" and l_["live"], l_)
    check("R the project the turn left keeps one marker naming where it went",
          await pool.fetchval("SELECT count(*) FROM entity_memory WHERE project_id = $1"
                              " AND metadata->>'source_kind' = 'thread_marker' AND content = $2",
                              R1, th.marker_text(R2, "Seed garden")) == 1)

    # A carry that fails leaves the whole turn where it was typed: the user
    # row, the rule and the marker move together or not at all.
    R3, R4 = await project("Kiln room"), await project("Kiln yard")
    km, _, _, _ = await taught_in(R3, "Kiln loads are logged by weight", Rx)
    saved_carry = tc._carry_learned

    async def _refuse(*a, **k):
        raise RuntimeError("carry refused (test)")
    tc._carry_learned = _refuse
    try:
        kres = await th.rehome_turn(pool, E, from_project=R3, to_project=R4, user_row_task=done(km),
                                    session_id="s-rh")
    finally:
        tc._carry_learned = saved_carry
    check("R a carry that fails moves nothing: the user row stays, no marker, the turn is told so",
          not kres.get("moved_user_row") and not kres.get("marker")
          and await pool.fetchval("SELECT project_id FROM entity_memory WHERE id = $1", km) == R3
          and await pool.fetchval("SELECT count(*) FROM entity_memory WHERE project_id = $1"
                                  " AND metadata->>'source_kind' = 'thread_marker'", R3) == 0, kres)

    # The rule's write waits on the same user row the re-home does, so it can
    # land after: the re-home waits for it (learned_task), as the pick does.
    R5, R6 = await project("Loft"), await project("Loft roof")
    late_ready = asyncio.get_running_loop().create_future()

    async def _late_row():
        return await late_ready
    late_row = asyncio.ensure_future(_late_row())

    async def _late_rule():
        rid = await late_row
        await asyncio.sleep(0.5)
        return await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": "Loft ladders are tied off", "trigger": "remember this:"}, "s-rh",
            source_context=ps(R5), from_turns=[int(rid)])
    late_task = asyncio.ensure_future(_late_rule())
    lm = await write_turn_row(pool, E, "s-rh", "user", "remember this: Loft ladders are tied off",
                              project_id=R5, tags={"source_context": ps(R5), "app_id": "workbench"})
    late_ready.set_result(lm)
    try:
        lres = await th.rehome_turn(pool, E, from_project=R5, to_project=R6, user_row_task=late_row,
                                    session_id="s-rh", learned_task=late_task)
    except TypeError as e:
        lres = {"error": repr(e)}
    late = await late_task
    lr = await row(int(late["id"]))
    check("R the re-home waited for the rule's write, and the rule is the new project's",
          lres.get("moved_user_row") and lr["md"].get("source_context") == ps(R6)
          and rule_taught_places(lr["md"]) == [ps(R6)], (lres, lr["md"].get("source_context")))
    rehands = [k for n in ast.walk(tree) if isinstance(n, ast.Call)
               and ((isinstance(n.func, ast.Name) and n.func.id == "rehome_turn")
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == "rehome_turn"))
               for k in n.keywords if k.arg == "learned_task"
               and isinstance(k.value, ast.Name) and k.value.id == "_taught_task"]
    check("R the turn hands the rule's write to the re-home", len(rehands) == 1, len(rehands))

    print("P. a message the Host files in a project")
    # What a pick files in a project ends as a rule taught there is: a project
    # tag is set only where it names something else (_carry_typed_place), so
    # neither arm of the move sets one.
    P7 = await project("Bench")
    tm = await write_turn_row(pool, E, "s-pd", "user", "remember this: Bench clamps hang on the left",
                              project_id=P7, tags={"source_context": ps(P7), "app_id": "workbench"})
    there = int((await MemoryEngine(pool)._store_teaching_memory(
        E, {"content": "Bench clamps hang on the left", "trigger": "remember this:"}, "s-pd",
        source_context=ps(P7), from_turns=[int(tm)]))["id"])

    async def filed(landed, provisional, words):
        Ls = ct.descriptor_for(landed)
        fm = await write_turn_row(pool, E, "s-pf", "user", f"remember this: {words}",
                                  tags={"source_context": Ls, "app_id": "circles"})
        fr = await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": words, "trigger": "remember this:"}, "s-pf", source_context=Ls,
            from_turns=[int(fm)])
        fch = tc.from_frame({"thread_choice": tc.frame_value(
            surface="circles", candidates=[{"kind": "project", "ref": str(P7), "title": "Bench"}],
            landed={"kind": "thread", "ref": landed, "title": "x", "provisional": provisional})})
        fch.picked = {"kind": "project", "ref": str(P7), "title": "Bench", "new": False, "reason": "bench"}
        fres = await tc.move_exchange(pool, E, fch, src=Ls, user_row_task=done(fm))
        return fres, int(fr["id"])

    async def shape(rid):
        sql, args = typed_rule_clause(ps(P7), [P7], 2)
        r = await pool.fetchrow(
            f"SELECT project_id, slot_scope, metadata->'source_context' AS sc, ({sql}) AS typed"
            " FROM entity_memory WHERE id = $1", rid, *args)
        return (r["project_id"], r["slot_scope"], json.loads(r["sc"]), r["typed"])
    kept = await topic("Bench chat", "first thing said about the bench", "Noted.")
    res_n, nr = await filed(kept, False, "Bench vise is oiled monthly")
    fold = str((await ct.create_thread(pool, E, title=None))["id"])
    res_f, fr_ = await filed(fold, True, "Bench offcuts go in the bin")
    want = await shape(there)
    check("P the rule a pick files in a project, from a thread that stays, is as one taught there",
          res_n.get("moved") and await shape(nr) == want, (res_n, await shape(nr), want))
    check("P ...and from a new topic folded into it",
          res_f.get("moved") and await shape(fr_) == want, (res_f, await shape(fr_), want))

    print("I. pages")
    pid = await pool.fetchval("INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Greenhouse', 'active')"
                              " RETURNING id", E)
    other = await pool.fetchval("INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Taxes', 'active')"
                                " RETURNING id", E)
    await write_turn_row(pool, E, "s-tc", "user", "the greenhouse heater needs a thermostat",
                         project_id=pid, tags={"source_context": {"kind": "project", "ref": str(pid)}})
    sc_ = await tp.page_scores(pool, E, "greenhouse heater thermostat", [pid, other])
    check("I a page whose conversation holds every word scores 1.0", sc_ == [(f"project:{pid}", 1.0)], sc_)
    check("I ...a clear match", tp.clear_match(sc_, {f"project:{pid}", f"project:{other}"}) == f"project:{pid}")
    check("I only the pages asked about are scored",
          await tp.page_scores(pool, E, "greenhouse heater thermostat", [other]) == [])


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    from server import brain_service as _bs
    saved = (_bs._pool, _bs._entity_id)
    _bs._pool, _bs._entity_id = pool, E
    try:
        await run(pool)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
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
