#!/usr/bin/env python3
"""Gate: what was learned from a turn follows the turn, on every path that
moves, folds, splits, re-homes or adopts it, against a real kernel schema
(circles_threads.carry_learned, the one rule every move takes).

For each path, a turn in the source conversation teaches a rule ("Remember
this:", session._keep_taught), corrects a preference its earlier turn
stated (close_turn, the same slot: the earlier one is retired), sets the
conversation's goal and hands off (close_turn), and writes a relationship
note and a slotted preference (memory_curate). Then the path runs, and:

  * the rule, the preferences, the goal and the notes are said where the
    turn is now (source_context), the place the rule was taught is there,
    a slotted row's slot scope is there, and the retired preference's
    record of who retired it names it; what an earlier turn that stayed
    learned stays; no row gets a project tag, as a row written there fresh
    has none (memory_engine._carry_typed_place is the one writer of one);
  * where the turn stands (close_turn.thread_state) is read there: its
    goal and its handoff, and not in the place it left;
  * deleting the place it left (a thread) or archiving it (a project)
    takes nothing that moved and brings nothing back; deleting the place
    it went to takes what moved and brings back the preference it retired;
    archiving a project changes no row.

Every turn has an id from its start (session.py's turn_token, on its
message row and in memory_engine.CURRENT_TURN_ROW), and what it learns
names it by that id as well as by the message row's: in PII mode the row is
queued and only the token is known to the write.

The turn also narrows an older row (said in an earlier turn) to "this
conversation" (memory_scope): that decision was made in the turn and follows
it, while the row stays where it was said.

Paths: the Host's pick (thread to thread, a new topic folded in, thread to
project, a Workbench page to another), a turn re-homed to the project its
work touched, Split, an adopt into a project (a thread, a project, the
original Circles conversation; whole and from a point). Then the delete's
own re-homing (a row a live conversation also said, in its words or in
other words) and a late write's takeover tag no project either, a record
naming two turns of which one moves is split between the two places, and a
close_turn whose message row lands late still names it. Then: the topic and
title close_turn names before the pick go to the thread the turn ends in;
the words the person sent into the running turn (nudge rows) move with its
message on a pick and a re-home; and in PII mode (the PII queue, its service
replaced by a stand-in that tokenizes words, so no row holds the words sent)
Split and an adopt from a point carry everything the turn learned, and so do
the Host's pick (to a listed thread, to a thread of its own) and a re-home
made at the turn's end while its writes are still queued: the move waits for
them, finds the message row by the turn's token, and what the turn writes
after it lands where it went. A move whose turn's writes do not land within
its bound is not made, and nothing of it is: what the turn wrote in the
thread it picked comes back where it stayed.

A path the kernel under test does not have (the Host's pick, memory slots)
is reported as absent, so the gate also runs on an earlier commit.

Invented values only. A throwaway cseries-postgres (docker, removed
afterwards), or TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN.
"""
import asyncio
import contextlib
import importlib.util
import inspect
import json
import os
import pathlib
import subprocess
import sys
import uuid
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
_LAST = [None]
HAVE_PICK = importlib.util.find_spec("mastercode.runtime.thread_choice") is not None
HAVE_SLOTS = importlib.util.find_spec("mastercode.memory_slots") is not None


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:900]}"))
    if not cond:
        FAILED += 1


def absent(what):
    print(f"  ABSENT {what}: not in the kernel under test")


def _at():
    """Now, and always later than the last row written: the rows of one
    fixture sort as said, on the clock the writers' own stamps (a
    decision's scoped_at) are read on, as in a turn."""
    now = datetime.now(timezone.utc)
    if _LAST[0] is not None and now <= _LAST[0]:
        now = _LAST[0] + timedelta(microseconds=1)
    _LAST[0] = now
    return now


def _md(v):
    return json.loads(v) if isinstance(v, str) else dict(v or {})


def _place(sc):
    return {"kind": str((sc or {}).get("kind")), "ref": str((sc or {}).get("ref"))} if sc else None


async def run(pool):
    from mastercode import circles_threads as ct
    from mastercode import memory_engine as me
    from mastercode.memory_engine import MemoryEngine, rule_taught_places, write_turn_row
    from mastercode.runtime import close_turn
    from mastercode.runtime import turn_home as th
    from mastercode.runtime.tools import _exec_memory_curate, _exec_memory_scope
    from server.routes.projects import adopt_thread_core
    tc = None
    if HAVE_PICK:
        from mastercode.runtime import thread_choice as tc

    def done(v):
        f = asyncio.get_running_loop().create_future()
        f.set_result(v)
        return f

    def kw_for(fn, **kw):
        # What `fn` takes of `kw`: an earlier commit's move took the message's
        # words and stamp to find its row by (required), this one its turn's
        # token.
        params = inspect.signature(fn).parameters
        return {k: v for k, v in kw.items() if k in params}

    # What the kernel under test takes (an earlier commit takes less).
    have_tokens = "turn_tokens" in inspect.signature(MemoryEngine._store_teaching_memory).parameters

    def taught_kw(tok):
        # session._keep_taught hands the rule its turn's token.
        return {"turn_tokens": [tok]} if have_tokens and tok else {}

    @contextlib.contextmanager
    def in_turn(sid, scope, row_task, token=None, landed=None):
        # What session.py sets as a turn starts: its conversation, the turn
        # (its token, and the write of its message row) and, for a person's
        # turn, the holder of the topic close_turn names (`landed`: where the
        # message was placed), each where the kernel has it.
        t1 = me.CURRENT_TURN_SCOPE.set({"session_id": sid, "scope": scope})
        var = getattr(me, "CURRENT_TURN_ROW", None)
        t2 = var.set({"session_id": sid, "task": row_task, "token": token}) if var is not None else None
        hv = getattr(close_turn, "TURN_TOPIC", None)
        t3 = (hv.set({"session_id": sid, "entity_id": E, "landed": landed})
              if hv is not None and landed is not None else None)
        try:
            yield
        finally:
            me.CURRENT_TURN_SCOPE.reset(t1)
            if t2 is not None:
                var.reset(t2)
            if t3 is not None:
                hv.reset(t3)

    async def thread():
        return ct.descriptor_for(str((await ct.create_thread(pool, E, title=None))["id"]))

    async def project(name):
        pid = int(await pool.fetchval("INSERT INTO projects (entity_id, name, status) VALUES ($1, $2, 'active')"
                                      " RETURNING id", E, name))
        return {"kind": "project", "ref": str(pid)}

    def pid_of(scope):
        return int(scope["ref"]) if scope["kind"] == "project" else None

    def app_of(scope):
        return "workbench" if scope["kind"] == "project" else "circles"

    async def turn_row(scope, sid, role, words, extra=None):
        return int(await write_turn_row(pool, E, sid, role, words, project_id=pid_of(scope),
                                        tags={"source_context": scope, "app_id": app_of(scope), **(extra or {})},
                                        created_at=_at()))

    async def row(rid):
        r = await pool.fetchrow(
            "SELECT id, status, project_id, metadata, (status = 'active' AND t_valid_until IS NULL) AS live,"
            + (" slot_scope, slot_topic" if HAVE_SLOTS else " NULL AS slot_scope, NULL AS slot_topic")
            + " FROM entity_memory WHERE id = $1", int(rid))
        d = dict(r)
        d["md"] = _md(d.pop("metadata"))
        return d

    def _pref(text, reason, kind, topic):
        p = {"text": text, "reason": reason, "scope": {"kind": kind}}
        if HAVE_SLOTS:
            p["topic"] = topic
        return p

    async def seed(src, tag, *, earlier=True, reply_now=True):
        """One conversation `src` with an earlier exchange (when `earlier`),
        then the turn that moves. Returns the ids."""
        sid = f"s-{tag}"
        ids: dict = {"sid": sid}
        topic = f"kettle_{tag}"
        tok0, tok1 = uuid.uuid4().hex, uuid.uuid4().hex
        if earlier:
            ids["u0"] = await turn_row(src, sid, "user", f"{tag}: how full is the kettle",
                                       {"turn_token": tok0})
            with in_turn(sid, src, done(ids["u0"]), tok0):
                r0 = await close_turn.execute(
                    {"preferences": [_pref(f"{tag}: the kettle is filled to one litre", "said so",
                                           src["kind"], topic)]},
                    entity_id=E, pool=pool, session_id=sid)
                win = await _exec_memory_curate({"op": "add", "about": "person",
                                                 "content": f"{tag}: the windows open at seven"},
                                                session_id=sid)
            ids["old_pref"] = int(r0["result"]["writes"]["preferences"][0]["id"])
            ids["scoped_old"] = int(win["result"]["memory_id"])
            ids["a0"] = await turn_row(src, sid, "assistant", f"{tag}: one litre, noted",
                                       {"turn_token": tok0})
        words = f"{tag} mugs are rinsed before use"
        ids["u1"] = await turn_row(src, sid, "user", f"remember this: {words}", {"turn_token": tok1})
        ids["tok1"] = tok1
        rule = await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": words, "trigger": "remember this:"}, sid, source_context=src,
            from_turns=[ids["u1"]], **taught_kw(tok1))
        ids["rule"] = int(rule["id"])
        signal = {"preferences": [_pref(f"{tag}: the kettle is filled to half a litre", "corrected",
                                        src["kind"], topic)],
                  "goal": {"set": f"{tag} goal: descale the kettle"},
                  "handoff": {"next_step": f"{tag} next: buy descaler"}}
        with in_turn(sid, src, done(ids["u1"]), tok1):
            if "scoped_old" in ids:
                # The turn narrows the earlier turn's row to this conversation.
                sc = await _exec_memory_scope({"kind": src["kind"], "ref": src["ref"],
                                               "memory_id": ids["scoped_old"]}, session_id=sid)
                check(f"{tag}: fixture: the turn narrowed the earlier row to this conversation",
                      sc.get("status") == "ok", sc)
            r1 = await close_turn.execute(signal, entity_id=E, pool=pool, session_id=sid)
            rel = await _exec_memory_curate({"op": "add", "about": "person", "memory_type": "relationship",
                                             "content": f"{tag}: Ada next door lends the descaler"},
                                            session_id=sid)
            if HAVE_SLOTS:
                tea = await _exec_memory_curate({"op": "add", "about": "person", "topic": f"tea_{tag}",
                                                 "content": f"{tag}: tea is brewed for four minutes"},
                                                session_id=sid)
                ids["tea"] = int(tea["result"]["memory_id"])
        w = r1["result"]["writes"]
        ids["new_pref"] = int(w["preferences"][0]["id"])
        ids["goal"] = int(w["goal"]["set"]["id"])
        ids["rel"] = int(rel["result"]["memory_id"])
        ids["signal"] = signal
        if reply_now:
            # A history the act moves later (Split, adopt): the reply is
            # already written where the turn was.
            ids["a1"] = await turn_row(src, sid, "assistant", f"{tag}: half a litre then",
                                       {"close_turn": signal, "turn_token": tok1})
        return ids

    async def reply_after_move(ids, dst, tag):
        # session.py writes the reply where the move filed the exchange.
        ids["a1"] = await turn_row(dst, ids["sid"], "assistant", f"{tag}: half a litre then",
                                   {"close_turn": ids["signal"], "turn_token": ids["tok1"]})

    def learned(ids):
        return [k for k in ("rule", "new_pref", "goal", "rel", "tea") if k in ids]

    async def landed(name, ids, src, dst, *, moved_earlier=False, handoff=True):
        """Where every row is after the path ran. `handoff`: the reply row
        keeps close_turn's signal (PII mode keeps no such text on a row)."""
        for k in ("u1", "a1"):
            r = await row(ids[k])
            check(f"{name}: the moved turn's {'message' if k == 'u1' else 'reply'} is in the place it went",
                  _place(r["md"].get("source_context")) == dst and r["project_id"] == pid_of(dst)
                  and r["status"] == "active", (k, r["md"].get("source_context"), r["project_id"]))
        for k in learned(ids):
            r = await row(ids[k])
            check(f"{name}: the {k} learned in the turn is said where the turn is now",
                  _place(r["md"].get("source_context")) == dst and r["live"],
                  (k, r["md"].get("source_context"), r["status"]))
            check(f"{name}: ...the {k} carries no project tag, as a row written there has none",
                  r["project_id"] is None, (k, r["project_id"]))
        rule = await row(ids["rule"])
        check(f"{name}: the place the rule was taught is where the turn is now",
              rule_taught_places(rule["md"]) == [dst], rule["md"].get("held_in"))
        npr = await row(ids["new_pref"])
        check(f"{name}: what the turn decided its preference is about ('this conversation') is where the turn is now",
              _place(npr["md"].get("about")) == dst
              and (not HAVE_SLOTS or npr["slot_scope"] == f"{dst['kind']}:{dst['ref']}"),
              (npr["md"].get("about"), npr["slot_scope"]))
        if HAVE_SLOTS:
            tea = await row(ids["tea"])
            check(f"{name}: a slotted row learned in the turn holds its slot there",
                  tea["slot_scope"] == f"{dst['kind']}:{dst['ref']}" and tea["slot_topic"],
                  (tea["slot_scope"], tea["slot_topic"]))
        if "old_pref" in ids:
            old = await row(ids["old_pref"])
            if HAVE_SLOTS:
                sup = old["md"].get("superseded_in") or {}
                check(f"{name}: the preference the turn corrected stays retired, its record naming where the turn is",
                      not old["live"] and _place(sup) == dst, (old["live"], sup))
            check(f"{name}: the earlier turn's preference "
                  + ("moved with the conversation" if moved_earlier else "stays where it was said"),
                  _place(old["md"].get("source_context")) == (dst if moved_earlier else src)
                  and _place(old["md"].get("about")) == (dst if moved_earlier else src),
                  (old["md"].get("source_context"), old["md"].get("about")))
            for k in ("u0", "a0"):
                r = await row(ids[k])
                check(f"{name}: the earlier turn's {k} " + ("moved with the conversation" if moved_earlier
                                                           else "stays where it was"),
                      _place(r["md"].get("source_context")) == (dst if moved_earlier else src),
                      r["md"].get("source_context"))
        if "scoped_old" in ids:
            so = await row(ids["scoped_old"])
            check(f"{name}: an earlier turn's row the moved turn narrowed to 'this conversation' "
                  "is about where the turn is now",
                  so["live"] and _place(so["md"].get("about")) == dst
                  and (not HAVE_SLOTS or so["slot_scope"] == f"{dst['kind']}:{dst['ref']}"),
                  (so["status"], so["md"].get("about"), so["slot_scope"]))
            check(f"{name}: ...and is said where it was said "
                  + ("(it moved with the conversation)" if moved_earlier else "(it stays)"),
                  _place(so["md"].get("source_context")) == (dst if moved_earlier else src),
                  so["md"].get("source_context"))
        tag = ids["sid"][2:]
        state = getattr(close_turn, "thread_state", None)
        if state is None:
            absent("close_turn.thread_state")
            return
        there = await state(pool, E, dst) or ""
        left = await state(pool, E, src) or ""
        if handoff:
            check(f"{name}: where the turn stands (its goal and handoff) is read where it is now",
                  f"{tag} goal" in there and f"{tag} next" in there, there)
            check(f"{name}: ...and not in the place it left",
                  f"{tag} goal" not in left and f"{tag} next" not in left, left)
        else:
            check(f"{name}: where the turn stands (its goal) is read where it is now",
                  f"{tag} goal" in there, there)
            check(f"{name}: ...and not in the place it left", f"{tag} goal" not in left, left)

    async def snapshot(ids):
        out = {}
        for k, v in ids.items():
            if isinstance(v, int):
                r = await row(v)
                out[k] = (r["status"], r["live"], r["project_id"], r["slot_scope"],
                          json.dumps(r["md"].get("source_context"), sort_keys=True))
        return out

    async def gone_side(name, ids, scope, *, takes, kept_old):
        """Delete (a thread) or archive (a project) one side; `takes`: the
        learned rows are said there now."""
        before = await snapshot(ids)
        if scope["kind"] == "thread":
            await ct.delete_thread(pool, E, scope["ref"])
        else:
            await pool.execute("UPDATE projects SET status = 'archived', updated_at = now() WHERE id = $1",
                               int(scope["ref"]))
            after = await snapshot(ids)
            check(f"{name}: archiving the project changes no row", after == before,
                  {k: (before[k], v) for k, v in after.items() if before.get(k) != v})
            return
        for k in learned(ids):
            r = await row(ids[k])
            if takes:
                check(f"{name}: deleting the place the turn went takes the {k} learned there",
                      r["status"] == "archived", (k, r["status"]))
            else:
                check(f"{name}: deleting the place the turn left keeps the {k} it learned",
                      r["live"], (k, r["status"], r["md"].get("source_context")))
        if "old_pref" in ids and HAVE_SLOTS and kept_old is not None:
            old = await row(ids["old_pref"])
            if kept_old:
                check(f"{name}: ...and brings back the preference the turn corrected",
                      old["live"], (old["status"], old["md"].get("superseded_in")))
            else:
                check(f"{name}: ...and brings back nothing the moved turn retired", not old["live"],
                      (old["status"], old["md"].get("superseded_in")))

    async def pick(src, dst, ids, *, surface="circles", provisional=False):
        ch = tc.from_frame({"thread_choice": tc.frame_value(
            surface=surface, candidates=[{"kind": dst["kind"], "ref": dst["ref"], "title": "there"}],
            landed={"kind": src["kind"], "ref": src["ref"], "title": "here", "provisional": provisional})})
        ch.picked = {"kind": dst["kind"], "ref": dst["ref"], "title": "there", "new": False, "reason": "it fits"}
        return await tc.move_exchange(pool, E, ch, **kw_for(
            tc.move_exchange, src=src, user_row_task=done(ids["u1"]), turn_token=ids["tok1"],
            user_row_stamp=None, user_content=None))

    # ── the Host's pick ─────────────────────────────────────────────────────
    if not HAVE_PICK:
        absent("1-4 and 15: the Host's pick (runtime/thread_choice)")
    else:
        print("1. the Host's pick, thread to thread (the thread it landed in has a history)")
        for side in ("left", "went"):
            S, D = await thread(), await thread()
            ids = await seed(S, f"p1{side[0]}", reply_now=False)
            res = await pick(S, D, ids)
            await reply_after_move(ids, D, f"p1{side[0]}")
            if side == "left":
                check("1: the pick moved the exchange", res.get("moved"), res)
                await landed("1", ids, S, D)
                await gone_side("1", ids, S, takes=False, kept_old=False)
            else:
                await gone_side("1", ids, D, takes=True, kept_old=True)

        print("2. the Host's pick from a new topic made for the message (folded in)")
        S, D = await thread(), await thread()
        ids = await seed(S, "p2", earlier=False, reply_now=False)
        res = await pick(S, D, ids, provisional=True)
        await reply_after_move(ids, D, "p2")
        check("2: the pick moved the exchange and folded the topic",
              res.get("moved") and await pool.fetchval(
                  "SELECT merged_into FROM circles_threads WHERE id = $1::uuid", S["ref"]) == f"thread:{D['ref']}",
              res)
        await landed("2", ids, S, D)
        await gone_side("2", ids, D, takes=True, kept_old=None)

        print("3. the Host's pick, thread to project")
        for side in ("left", "went"):
            S, D = await thread(), await project(f"Kettle shelf {side}")
            ids = await seed(S, f"p3{side[0]}", reply_now=False)
            res = await pick(S, D, ids)
            await reply_after_move(ids, D, f"p3{side[0]}")
            if side == "left":
                check("3: the pick moved the exchange", res.get("moved"), res)
                await landed("3", ids, S, D)
                await gone_side("3", ids, S, takes=False, kept_old=False)
            else:
                await gone_side("3", ids, D, takes=True, kept_old=None)

        print("4. the Host's pick on the Workbench, page to page")
        for side in ("left", "went"):
            S, D = await project(f"Pantry {side}"), await project(f"Larder {side}")
            ids = await seed(S, f"p4{side[0]}", reply_now=False)
            res = await pick(S, D, ids, surface="index")
            await reply_after_move(ids, D, f"p4{side[0]}")
            if side == "left":
                check("4: the pick moved the exchange", res.get("moved"), res)
                await landed("4", ids, S, D)
            await gone_side("4", ids, S if side == "left" else D, takes=side != "left", kept_old=None)

    # ── a turn re-homed to the project its work touched ─────────────────────
    print("5. a turn re-homed to the project its work touched")
    for side in ("left", "went"):
        S, D = await project(f"Shed {side}"), await project(f"Garden {side}")
        ids = await seed(S, f"p5{side[0]}", reply_now=False)
        res = await th.rehome_turn(pool, E, from_project=int(S["ref"]), to_project=int(D["ref"]),
                                   user_row_task=done(ids["u1"]), session_id=ids["sid"],
                                   **kw_for(th.rehome_turn, turn_token=ids["tok1"]))
        await reply_after_move(ids, D, f"p5{side[0]}")
        if side == "left":
            check("5: the re-home moved the message", res.get("moved_user_row"), res)
            await landed("5", ids, S, D)
        await gone_side("5", ids, S if side == "left" else D, takes=side != "left", kept_old=None)

    # ── Split ───────────────────────────────────────────────────────────────
    print("6. Split from the turn onward")
    for side in ("left", "went"):
        S = await thread()
        ids = await seed(S, f"p6{side[0]}")
        new = await ct.split_thread(pool, E, S["ref"], from_turn_id=ids["u1"])
        D = ct.descriptor_for(str(new["id"]))
        if side == "left":
            await landed("6", ids, S, D)
            await gone_side("6", ids, S, takes=False, kept_old=False)
        else:
            await gone_side("6", ids, D, takes=True, kept_old=True)

    # ── adopt into a project ────────────────────────────────────────────────
    for n, (label, mk_src, window) in enumerate((
            ("a thread, whole", "thread", False),
            ("a thread, from the turn on", "thread", True),
            ("a project, whole", "project", False),
            ("a project, from the turn on", "project", True)), start=7):
        print(f"{n}. adopt into a project: {label}")
        for side in ("left", "went"):
            S = await thread() if mk_src == "thread" else await project(f"Src {n}{side}")
            P = await project(f"Dst {n}{side}")
            ids = await seed(S, f"p{n}{side[0]}")
            since = None
            if window:
                since = (await pool.fetchval("SELECT created_at FROM entity_memory WHERE id = $1",
                                             ids["u1"])).isoformat()
            res = await adopt_thread_core(pool, E, int(P["ref"]), S["kind"], S["ref"], since=since)
            if side == "left":
                check(f"{n}: the adopt moved the turns", res.get("moved_turns") == (2 if window else 4), res)
                await landed(str(n), ids, S, P, moved_earlier=not window)
                await gone_side(str(n), ids, S, takes=False, kept_old=False if window else None)
            else:
                await gone_side(str(n), ids, P, takes=True, kept_old=None)

    print("8b. adopt a thread's turn into a project, a later turn that stays having narrowed its rule")
    S = await thread()
    P = await project("Dst 8b")
    ids = await seed(S, "p8b")
    tok2 = uuid.uuid4().hex
    u2 = await turn_row(S, ids["sid"], "user", "p8b: keep that rule to this thread", {"turn_token": tok2})
    with in_turn(ids["sid"], S, done(u2), tok2):
        # The memory_scope tool, as the Host calls it: the decision names
        # this turn, which stays.
        sc = await _exec_memory_scope({"kind": S["kind"], "ref": S["ref"], "memory_id": ids["rule"]},
                                      session_id=ids["sid"])
    check("8b: fixture: the later turn narrowed the rule to the thread", sc.get("status") == "ok", sc)
    await turn_row(S, ids["sid"], "assistant", "p8b: done", {"turn_token": tok2})
    a1_at = await pool.fetchval("SELECT created_at FROM entity_memory WHERE id = $1", ids["a1"])
    u1_at = await pool.fetchval("SELECT created_at FROM entity_memory WHERE id = $1", ids["u1"])
    res = await adopt_thread_core(pool, E, int(P["ref"]), "thread", S["ref"],
                                  since=u1_at.isoformat(), until=a1_at.isoformat())
    check("8b: the adopt moved the one exchange", res.get("moved_turns") == 2, res)
    rr, npr = await row(ids["rule"]), await row(ids["new_pref"])
    check("8b: the rule is said in the project and taught there",
          _place(rr["md"].get("source_context")) == P and rule_taught_places(rr["md"]) == [P],
          (rr["md"].get("source_context"), rr["md"].get("held_in")))
    check("8b: ...and the later turn's narrowing, which stayed, still names the thread",
          _place(rr["md"].get("about")) == S, rr["md"].get("about"))
    check("8b: the moved turn's own decision about its preference follows it",
          _place(npr["md"].get("about")) == P, npr["md"].get("about"))
    so = await row(ids["scoped_old"])
    check("8b: the moved turn's decision on an earlier turn's row follows it; the row stays where said",
          _place(so["md"].get("about")) == P and _place(so["md"].get("source_context")) == S,
          (so["md"].get("about"), so["md"].get("source_context")))

    print("11. adopt the original Circles conversation into a project")
    ORIG = {"kind": "app", "ref": "circles"}
    P = await project("From the original conversation")
    ids = await seed(ORIG, "p11")
    res = await adopt_thread_core(pool, E, int(P["ref"]), "app", "circles")
    check("11: the adopt moved the conversation", (res.get("moved_turns") or 0) >= 4, res)
    await landed("11", ids, ORIG, P, moved_earlier=True)
    await gone_side("11", ids, P, takes=True, kept_old=None)

    # ── the delete's own re-homing, and a late write's takeover ─────────────
    async def fresh_tag(P, words):
        # The tag a write said in the project has: a row of close_turn's.
        sid = f"s-fresh-{P['ref']}"
        u = await turn_row(P, sid, "user", f"{words} (asked)")
        with in_turn(sid, P, done(u)):
            r = await close_turn.execute({"preferences": [{"text": words, "reason": "said",
                                                           "scope": {"kind": "global"}}]},
                                         entity_id=E, pool=pool, session_id=sid)
        return (await row(r["result"]["writes"]["preferences"][0]["id"]))["project_id"]

    print("12. a row a live project conversation also said, when its thread is deleted")
    T = await thread()
    P = await project("Said here too")
    ut = await turn_row(T, "s-k1", "user", "about the hedge")
    with in_turn("s-k1", T, done(ut)):
        held = await _exec_memory_curate({"op": "add", "about": "person",
                                          "content": "The hedge is trimmed in June"}, session_id="s-k1")
    hid = int(held["result"]["memory_id"])
    up = await turn_row(P, "s-k2", "user", "about the hedge again")
    with in_turn("s-k2", P, done(up)):
        again = await _exec_memory_curate({"op": "add", "about": "person",
                                           "content": "The hedge is trimmed in June"}, session_id="s-k2")
    check("12: fixture: the project conversation's saying is recorded on the row",
          (again.get("result") or {}).get("memory_id") == hid, again)
    await ct.delete_thread(pool, E, T["ref"])
    k = await row(hid)
    check("12: deleting the thread gives the row to the project conversation that also said it",
          k["live"] and _place(k["md"].get("source_context")) == P, (k["status"], k["md"].get("source_context")))
    check("12: ...tagged as a row written there is",
          k["project_id"] == await fresh_tag(P, "Gutters are cleared in October"), k["project_id"])

    print("13. a row a live project conversation said in other words, when its thread is deleted")
    from mastercode.memory_consolidation import MemoryConsolidator
    T = await thread()
    P = await project("Said otherwise")
    ut = await turn_row(T, "s-o1", "user", "about the shed door")
    with in_turn("s-o1", T, done(ut)):
        base_ = await _exec_memory_curate({"op": "add", "about": "person",
                                           "content": "The shed door sticks in winter"}, session_id="s-o1")
    bid = int(base_["result"]["memory_id"])
    up = await turn_row(P, "s-o2", "user", "the shed door again")
    await MemoryConsolidator(pool).note_held(E, bid, {**P, "from_turns": [up]},
                                             words="The shed door sticks when it is cold")
    await ct.delete_thread(pool, E, T["ref"])
    own = await pool.fetchrow("SELECT id, project_id, metadata->'source_context' AS sc FROM entity_memory"
                              " WHERE entity_id = $1 AND content = 'The shed door sticks when it is cold'"
                              " AND status = 'active'", E)
    check("13: the project conversation keeps a row of its own words",
          own is not None and _place(_md(own["sc"])) == P, own and dict(own))
    check("13: ...tagged as a row written there is",
          own is not None and own["project_id"] == await fresh_tag(P, "Gates are oiled in spring"),
          own and own["project_id"])

    print("14. a late write a live project conversation takes over from a deleted thread")
    T = await thread()
    P = await project("Took it over")
    ut = await turn_row(T, "s-t1", "user", "about the compost")
    with in_turn("s-t1", T, done(ut)):
        late = await _exec_memory_curate({"op": "add", "about": "person",
                                          "content": "The compost is turned weekly"}, session_id="s-t1")
    lid = int(late["result"]["memory_id"])
    # The thread is deleted while the write is still landing: the row is the
    # deleted thread's until its forget runs (forget_if_deleted).
    await pool.execute("UPDATE circles_threads SET deleted_at = now() WHERE id = $1::uuid", T["ref"])
    up = await turn_row(P, "s-t2", "user", "the compost again")
    took = await MemoryConsolidator(pool).note_held(E, lid, {**P, "from_turns": [up]})
    t_ = await row(lid)
    check("14: the project conversation takes the row over",
          took and _place(t_["md"].get("source_context")) == P, (took, t_["md"].get("source_context")))
    check("14: ...tagged as a row written there is",
          t_["project_id"] == await fresh_tag(P, "Leaves are raked in November"), t_["project_id"])

    # ── a record naming two turns, one of which moves ───────────────────────
    if HAVE_PICK:
        print("15. a conversation said a row again in two turns, and one of them moves")
        O, S, D = await thread(), await thread(), await thread()
        uo = await turn_row(O, "s-r0", "user", "the bins")
        with in_turn("s-r0", O, done(uo)):
            b = await _exec_memory_curate({"op": "add", "about": "person",
                                           "content": "Bins go out on Tuesday night"}, session_id="s-r0")
        bin_id = int(b["result"]["memory_id"])
        turns = []
        for i in range(2):
            u = await turn_row(S, "s-r1", "user", f"bins again {i}")
            turns.append(u)
            with in_turn("s-r1", S, done(u)):
                await _exec_memory_curate({"op": "add", "about": "person",
                                           "content": "Bins go out on Tuesday night"}, session_id="s-r1")
        ch = tc.from_frame({"thread_choice": tc.frame_value(
            surface="circles", candidates=[{"kind": "thread", "ref": D["ref"], "title": "there"}],
            landed={"kind": "thread", "ref": S["ref"], "title": "here", "provisional": False})})
        ch.picked = {"kind": "thread", "ref": D["ref"], "title": "there", "new": False, "reason": "bins"}
        await tc.move_exchange(pool, E, ch, **kw_for(tc.move_exchange, src=S, user_row_task=done(turns[1]),
                                                     user_row_stamp=None, user_content=None))
        held_in = (await row(bin_id))["md"].get("held_in") or []
        by = {(e.get("kind"), e.get("ref")): sorted(e.get("from_turns") or []) for e in held_in}
        check("15: the place it went holds the moved turn's saying, and the place it left keeps its own",
              by.get(("thread", D["ref"])) == [turns[1]] and by.get(("thread", S["ref"])) == [turns[0]], held_in)
        await ct.delete_thread(pool, E, S["ref"])
        await ct.delete_thread(pool, E, O["ref"])
        r15 = await row(bin_id)
        check("15: deleting the row's own thread and the place the turn left keeps it for the place it went",
              r15["live"] and _place(r15["md"].get("source_context")) == D,
              (r15["status"], r15["md"].get("source_context")))

    # ── a rule taught again in its own conversation, and that turn moves ────
    if HAVE_PICK:
        print("17. a rule taught again in its own thread, narrowed there, and the repeat moves")
        from mastercode.memory_engine import rule_place_clause
        from mastercode.runtime.tools import apply_memory_scope
        S, D = await thread(), await thread()
        words = "Boots come off at the door"
        u1 = await turn_row(S, "s-rp", "user", f"remember this: {words}")
        r = await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": words, "trigger": "remember this:"}, "s-rp", source_context=S, from_turns=[u1])
        rid = int(r["id"])
        await apply_memory_scope(pool, E, rid, S["kind"], S["ref"])
        u5 = await turn_row(S, "s-rp", "user", f"remember this: {words}")
        await MemoryEngine(pool)._store_teaching_memory(
            E, {"content": words, "trigger": "remember this:"}, "s-rp", source_context=S, from_turns=[u5])
        ch = tc.from_frame({"thread_choice": tc.frame_value(
            surface="circles", candidates=[{"kind": "thread", "ref": D["ref"], "title": "there"}],
            landed={"kind": "thread", "ref": S["ref"], "title": "here", "provisional": False})})
        ch.picked = {"kind": "thread", "ref": D["ref"], "title": "there", "new": False, "reason": "boots"}
        await tc.move_exchange(pool, E, ch, **kw_for(tc.move_exchange, src=S, user_row_task=done(u5),
                                                     user_row_stamp=None, user_content=None))

        async def rides(place):
            sql, args = rule_place_clause(place, 2)
            return bool(await pool.fetchval(f"SELECT 1 FROM entity_memory WHERE id = $1 AND {sql}", rid, *args))
        rr = await row(rid)
        check("17: the rule stays in the thread it was first taught in",
              _place(rr["md"].get("source_context")) == S, rr["md"].get("source_context"))
        check("17: ...and, narrowed there, it also rides where the repeat teaching went",
              D in rule_taught_places(rr["md"]) and S in rule_taught_places(rr["md"])
              and await rides(D) and await rides(S), rr["md"].get("held_in"))

    async def thread_names(scope):
        r = await pool.fetchrow("SELECT topic, title FROM circles_threads WHERE id = $1::uuid", scope["ref"])
        return (r["topic"], r["title"]) if r else (None, None)

    async def finish(moved):
        # session.py, as the turn ends, past the pick and the re-home.
        fin = getattr(close_turn, "finish_topic", None)
        if fin is not None:
            await fin(pool, moved=moved)

    def pick_choice(src, dst):
        ch = tc.from_frame({"thread_choice": tc.frame_value(
            surface="circles", candidates=[{"kind": "thread", "ref": dst["ref"], "title": "there"}],
            landed={"kind": "thread", "ref": src["ref"], "title": "here", "provisional": False})})
        ch.picked = {"kind": "thread", "ref": dst["ref"], "title": "there", "new": False, "reason": "it fits"}
        return ch

    # ── the topic and title close_turn names before the pick ────────────────
    if HAVE_PICK:
        print("18. close_turn names the topic and title, then the Host picks another thread")
        S, D = await thread(), await thread()
        tok = uuid.uuid4().hex
        u = await turn_row(S, "s-tt", "user", "the shed roof leaks", {"turn_token": tok})
        with in_turn("s-tt", S, done(u), tok, landed=S):
            # session.py hands close_turn the thread the message landed in.
            await close_turn.execute({"topic": "Shed roof repair", "title": "The shed roof"},
                                     entity_id=E, pool=pool, session_id="s-tt", thread_id=S["ref"])
            moved = await tc.move_exchange(pool, E, pick_choice(S, D), **kw_for(
                tc.move_exchange, src=S, user_row_task=done(u), turn_token=tok,
                user_row_stamp=None, user_content=None))
            await finish(moved)
        check("18: fixture: the pick moved the exchange", moved.get("moved"), moved)
        check("18: the topic and title are the thread's the turn went to",
              await thread_names(D) == ("Shed roof repair", "The shed roof"), await thread_names(D))
        check("18: ...and the thread the message left keeps none", await thread_names(S) == (None, None),
              await thread_names(S))

        print("18b. the Host picks first, close_turn names the topic, and the move cannot be made")
        S, D = await thread(), await thread()
        tok = uuid.uuid4().hex
        u = await turn_row(S, "s-tt2", "user", "the gate hinge squeaks", {"turn_token": tok})
        with in_turn("s-tt2", S, done(u), tok, landed=S):
            # After the pick, session.py's thread is the picked one.
            await close_turn.execute({"topic": "Gate hinge", "title": "The gate"},
                                     entity_id=E, pool=pool, session_id="s-tt2", thread_id=D["ref"])
            # The picked thread is deleted before the turn ends: the turn
            # stays whole where it landed.
            await pool.execute("UPDATE circles_threads SET deleted_at = now() WHERE id = $1::uuid", D["ref"])
            moved = await tc.move_exchange(pool, E, pick_choice(S, D), **kw_for(
                tc.move_exchange, src=S, user_row_task=done(u), turn_token=tok,
                user_row_stamp=None, user_content=None))
            await finish(moved)
        check("18b: fixture: the move could not be made", not moved.get("moved"), moved)
        check("18b: the topic and title are the thread's the turn stayed in",
              await thread_names(S) == ("Gate hinge", "The gate"), await thread_names(S))

    # ── the words the person sent into the running turn ─────────────────────
    async def nudge_row(scope, sid, tok, words, *, delay=0.0):
        # session.py's drain: a user row of the turn's own, with its tags.
        if delay:
            await asyncio.sleep(delay)
        return await turn_row(scope, sid, "user", words, {"turn_token": tok, "nudge": True})

    if HAVE_PICK:
        print("19. the Host's pick, with words the person sent into the turn")
        S, D = await thread(), await thread()
        ids = await seed(S, "p19", earlier=False, reply_now=False)
        n_now = await nudge_row(S, ids["sid"], ids["tok1"], "p19: and the lid too")
        late = asyncio.ensure_future(nudge_row(S, ids["sid"], ids["tok1"], "p19: and the spout", delay=0.3))
        res = await tc.move_exchange(pool, E, pick_choice(S, D), **kw_for(
            tc.move_exchange, src=S, user_row_task=done(ids["u1"]), turn_token=ids["tok1"],
            nudge_row_tasks=[late], user_row_stamp=None, user_content=None))
        n_late = await late
        check("19: fixture: the pick moved the exchange", res.get("moved"), res)
        for k, nid in (("already written", n_now), ("still being written", n_late)):
            r = await row(nid)
            check(f"19: words sent into the turn ({k}) are in the thread its message went to",
                  _place(r["md"].get("source_context")) == D and r["status"] == "active",
                  r["md"].get("source_context"))

    print("19b. a turn re-homed to the project its work touched, with words sent into it")
    S, D = await project("Nudge shed"), await project("Nudge garden")
    ids = await seed(S, "p19b", earlier=False, reply_now=False)
    n_now = await nudge_row(S, ids["sid"], ids["tok1"], "p19b: and the rake")
    late = asyncio.ensure_future(nudge_row(S, ids["sid"], ids["tok1"], "p19b: and the hose", delay=0.3))
    res = await th.rehome_turn(pool, E, from_project=int(S["ref"]), to_project=int(D["ref"]),
                               user_row_task=done(ids["u1"]), session_id=ids["sid"],
                               **kw_for(th.rehome_turn, turn_token=ids["tok1"], nudge_row_tasks=[late]))
    n_late = await late
    check("19b: fixture: the re-home moved the message", res.get("moved_user_row"), res)
    for k, nid in (("already written", n_now), ("still being written", n_late)):
        r = await row(nid)
        check(f"19b: words sent into the turn ({k}) are in the project its message went to",
              _place(r["md"].get("source_context")) == D and r["project_id"] == int(D["ref"]),
              (r["md"].get("source_context"), r["project_id"]))

    # ── PII mode: the message row is queued ─────────────────────────────────
    from types import SimpleNamespace
    from mastercode import pii_client as _pc
    from mastercode import pii_queue as _pq
    saved_env = {k: os.environ.get(k) for k in ("PII_MODE", "PII_QUEUE_WORKER")}
    saved_st = _pc.sterilize
    # What the stand-in for the sterilizing service replaces: a word of every
    # message and of most of what a turn learns, so a row never holds the
    # words that were sent, as it never does with the service's tokens.
    TOKENS = {"kettle": "[ITEM_1]", "mugs": "[ITEM_2]", "Ada": "[PERSON_1]", "descaler": "[ITEM_3]"}

    def tokenized(text):
        for w, t in TOKENS.items():
            text = text.replace(w, t)
        return text

    async def stand_in(text, scope, **kw):
        # The sterilizing service, replaced: no request leaves the box.
        return _pc.PiiResult(ok=True, text=tokenized(text), findings=1)

    async def drain():
        while await _pq.drain_once():
            pass

    async def drain_later(delay):
        # The queue's drain worker (off in this gate), landing the turn's
        # writes while its move waits for them.
        await asyncio.sleep(delay)
        await drain()

    def queued_turn_row(scope, sid, role, words, tok, extra=None, at=None):
        # session.py's write of a turn row, queued in PII mode (returns None).
        return asyncio.ensure_future(write_turn_row(
            pool, E, sid, role, words, project_id=pid_of(scope),
            tags={"source_context": scope, "app_id": app_of(scope), "turn_token": tok, **(extra or {})},
            created_at=at or _at()))

    async def by_token(tok, role, *, nudge=False):
        return int(await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'"
            " AND metadata->>'turn_token' = $2 AND metadata->>'role' = $3"
            " AND (COALESCE(metadata->>'nudge', '') = 'true') = $4 ORDER BY id LIMIT 1",
            E, tok, role, nudge))

    async def live_id(words):
        # The row a write left, found by the words the service gave it.
        return int(await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id = $1 AND content = $2 AND memory_type <> 'turn'"
            " ORDER BY id DESC LIMIT 1", E, tokenized(words)))

    async def seed_pii(src, tag, *, mid=None, end=None):
        """seed(), with PII mode on: every write of a turn is queued and lands
        when the queue drains, its words tokenized; no write learns its
        message row's id. The turn that moves runs as session.py runs it: it
        has its token from its start and its message row is queued under it;
        the rule taught in the message is written by a task of its own
        (session._keep_taught); it narrows an earlier row to this
        conversation, and close_turn keeps a corrected preference and a goal;
        `mid(turn)` runs next (the Host's pick); then memory_curate keeps a
        relationship note and a slotted note. With `end(turn)`, the person
        also sends words into the turn (a nudge row) and `end` makes the move
        at the turn's end, with all of it still queued, and returns its
        result and where the turn is then; the reply is written there, and
        the turn writes once more (memory_curate). Without, the reply is
        written where the turn is. Then the queue drains, and the ids."""
        sid = f"s-{tag}"
        ids: dict = {"sid": sid}
        topic = f"kettle_{tag}"
        tok0, tok1 = uuid.uuid4().hex, uuid.uuid4().hex
        row_var = getattr(me, "CURRENT_TURN_ROW", None)
        with in_turn(sid, src, None, tok0):
            await queued_turn_row(src, sid, "user", f"{tag}: how full is the kettle", tok0)
            await close_turn.execute({"preferences": [_pref(f"{tag}: the kettle is filled to one litre",
                                                            "said so", src["kind"], topic)]},
                                     entity_id=E, pool=pool, session_id=sid)
            await _exec_memory_curate({"op": "add", "about": "person",
                                       "content": f"{tag}: the windows open at seven"}, session_id=sid)
        await queued_turn_row(src, sid, "assistant", f"{tag}: one litre, noted", tok0)
        await drain()
        ids["old_pref"] = await live_id(f"{tag}: the kettle is filled to one litre")
        ids["scoped_old"] = await live_id(f"{tag}: the windows open at seven")
        words = f"{tag} mugs are rinsed before use"
        ids["tok1"], ids["words"] = tok1, f"remember this: {words}"
        signal = {"preferences": [_pref(f"{tag}: the kettle is filled to half a litre", "corrected",
                                        src["kind"], topic)],
                  "goal": {"set": f"{tag} goal: descale the kettle"},
                  "handoff": {"next_step": f"{tag} next: buy descaler"}}
        with in_turn(sid, src, None, tok1):
            stamp = _at()
            t1 = queued_turn_row(src, sid, "user", ids["words"], tok1, at=stamp)
            if row_var is not None:
                # session.py: the turn's message row, once its write starts.
                row_var.set({"session_id": sid, "task": t1, "token": tok1})
            rid = await t1
            check(f"{tag}: fixture: PII mode queues the message row (no id to the turn)", rid is None, rid)

            async def keep_taught():
                async def taught(text):
                    # session._keep_taught's write, landed by the queue's drain.
                    return await MemoryEngine(pool)._store_teaching_memory(
                        E, {"content": text, "trigger": "remember this:"}, sid, source_context=src,
                        from_turns=None, **taught_kw(tok1))
                await _pq.submit(words, f"entity:{E}:memory", taught, verify=True, pool=pool, entity_id=E)
            turn = {"sid": sid, "tok": tok1, "row": t1, "words": ids["words"], "stamp": stamp,
                    "learned": asyncio.ensure_future(keep_taught()), "nudges": [],
                    "sess": SimpleNamespace(entity_id=E, session_id=sid, recall_scope=dict(src),
                                            thread_id=src["ref"] if src["kind"] == "thread" else None)}
            sc = await _exec_memory_scope({"kind": src["kind"], "ref": src["ref"],
                                           "memory_id": ids["scoped_old"]}, session_id=sid)
            check(f"{tag}: fixture: the turn narrowed the earlier row to this conversation",
                  sc.get("status") == "ok", sc)
            r1 = await close_turn.execute(signal, entity_id=E, pool=pool, session_id=sid)
            if mid is not None:
                await mid(turn)
            await _exec_memory_curate({"op": "add", "about": "person", "memory_type": "relationship",
                                       "content": f"{tag}: Ada next door lends the descaler"},
                                      session_id=sid)
            if HAVE_SLOTS:
                await _exec_memory_curate({"op": "add", "about": "person", "topic": f"tea_{tag}",
                                           "content": f"{tag}: tea is brewed for four minutes"},
                                          session_id=sid)
            check(f"{tag}: fixture: close_turn's writes are queued",
                  (r1["result"]["writes"].get("goal") or {}).get("status") == "queued", r1)
            if end is not None:
                # session.py's drain: a row of the turn's own, said where the
                # message landed.
                turn["nudges"].append(queued_turn_row(src, sid, "user", f"{tag}: and the mugs on the shelf",
                                                      tok1, {"nudge": True}))
                ids["move"], to = await end(turn)
                # session.py past the move: the reply where the turn is, and
                # one more write the turn makes.
                await queued_turn_row(to, sid, "assistant", f"{tag}: half a litre then", tok1,
                                      {"close_turn": signal})
                await _exec_memory_curate({"op": "add", "about": "person",
                                           "content": f"{tag}: the shelf is dusted on Fridays"}, session_id=sid)
        if end is None:
            await queued_turn_row(src, sid, "assistant", f"{tag}: half a litre then", tok1,
                                  {"close_turn": signal})
        await drain()
        ids["u0"], ids["a0"] = await by_token(tok0, "user"), await by_token(tok0, "assistant")
        ids["u1"], ids["a1"] = await by_token(tok1, "user"), await by_token(tok1, "assistant")
        ids["rule"] = await live_id(words)
        ids["new_pref"] = await live_id(f"{tag}: the kettle is filled to half a litre")
        ids["goal"] = await live_id(f"{tag} goal: descale the kettle")
        ids["rel"] = await live_id(f"{tag}: Ada next door lends the descaler")
        if HAVE_SLOTS:
            ids["tea"] = await live_id(f"{tag}: tea is brewed for four minutes")
        if end is not None:
            ids["nudge"] = await by_token(tok1, "user", nudge=True)
            ids["after"] = await live_id(f"{tag}: the shelf is dusted on Fridays")
        return ids

    def host_pick(src, dst=None):
        # The Host's pick_thread call, answered in its turn (thread_choice.
        # pick): a listed thread `dst`, or, with none, a thread of its own.
        async def _mid(turn):
            ch = tc.from_frame({"thread_choice": tc.frame_value(
                surface="circles", candidates=[{"kind": dst["kind"], "ref": dst["ref"], "title": "there"}]
                if dst else [], landed={"kind": src["kind"], "ref": src["ref"], "title": "here",
                                        "provisional": False})})
            args = ({"thread": f"{dst['kind']}:{dst['ref']}", "reason": "it fits"} if dst
                    else {"new": True, "title": f"Mug shelf {turn['sid']}", "reason": "a subject of its own"})
            res = await tc.pick(pool, turn["sess"], args, ch)
            check(f"{turn['sid']}: fixture: the Host's pick is taken", res.get("status") == "ok", res)
            turn["choice"] = ch
        return _mid

    async def move_turn(turn, src, *, rehome_to=None, bound=None, land_first=0):
        """The move at the turn's end, made as session.py makes it, with the
        turn's writes still queued: the queue drains 0.3 s into it, or, with
        `bound` (the move's own wait, seconds), its first `land_first` writes
        land before the move and the rest only after it. Then the turn's
        place follows the move, or goes back where it landed. Returns the
        move's result and where the turn is."""
        saved_wait, drainer = me.TURN_ROW_WAIT, None
        if bound is None:
            drainer = asyncio.ensure_future(drain_later(0.3))
        else:
            me.TURN_ROW_WAIT = bound
            for _ in range(land_first):
                await _pq.drain_once()
        try:
            if rehome_to is None:
                res = await tc.move_exchange(pool, E, turn["choice"], **kw_for(
                    tc.move_exchange, src=src, user_row_task=turn["row"], turn_token=turn["tok"],
                    learned_task=turn["learned"], nudge_row_tasks=turn["nudges"],
                    user_row_stamp=turn["stamp"], user_content=turn["words"]))
                moved = bool(res.get("moved"))
                to = res["to"] if moved else src
                if moved and hasattr(tc, "place_turn"):
                    tc.place_turn(turn["sess"], to)
                elif not moved and hasattr(tc, "stay"):
                    tc.stay(turn["sess"], turn["choice"])
            else:
                res = await th.rehome_turn(pool, E, from_project=int(src["ref"]), to_project=int(rehome_to["ref"]),
                                           user_row_task=turn["row"], session_id=turn["sid"], **kw_for(
                                               th.rehome_turn, turn_token=turn["tok"], learned_task=turn["learned"],
                                               nudge_row_tasks=turn["nudges"], user_content=turn["words"],
                                               turn_started_at=turn["stamp"]))
                moved = bool(res.get("moved_user_row"))
                to = rehome_to if moved else src
                if moved and hasattr(tc, "place_turn"):
                    tc.place_turn(turn["sess"], to)
        finally:
            me.TURN_ROW_WAIT = saved_wait
            if drainer is not None:
                await drainer
        return res, to

    async def pii_followed(name, ids, where):
        """The PII-mode rows landed() does not read: the message row holds
        the service's words, and the words sent into the turn and what it
        wrote after its move are said where it is now."""
        um = await pool.fetchval("SELECT content FROM entity_memory WHERE id = $1", ids["u1"])
        check(f"{name}: fixture: the message row holds the service's words, not the words sent",
              um == tokenized(ids["words"]) and um != ids["words"], um)
        for k, what in (("nudge", "the words sent into the turn"),
                        ("after", "what the turn wrote after its move")):
            r = await row(ids[k])
            check(f"{name}: {what} are said where the turn is now",
                  _place(r["md"].get("source_context")) == where and r["status"] == "active"
                  and (k != "nudge" or r["project_id"] == pid_of(where)),
                  (r["md"].get("source_context"), r["project_id"], r["status"]))

    async def stayed(name, ids, src, went):
        """A move that was not made: the turn, all it learned, and what it
        wrote after it, are said where the message landed, and nothing of it
        in the place it would have gone."""
        for k in ["u1", "a1", "nudge", "after"] + learned(ids):
            r = await row(ids[k])
            check(f"{name}: the turn's {k} is said where the turn stayed",
                  _place(r["md"].get("source_context")) == src and r["status"] == "active",
                  (k, r["md"].get("source_context"), r["status"]))
        rule = await row(ids["rule"])
        check(f"{name}: the place the rule was taught is where the turn stayed",
              rule_taught_places(rule["md"]) == [src], rule["md"].get("held_in"))
        # What the turn wrote before its pick never left, so it carries no
        # record of a move (circles_threads.carry_learned skips a row already
        # said where the move goes).
        for k in ("rule", "new_pref", "goal"):
            if k in ids:
                r = await row(ids[k])
                check(f"{name}: the turn's {k}, written before the pick, carries no mark of a move",
                      not any(m in r["md"] for m in ("adopted_from", "split_from")),
                      (k, r["md"].get("adopted_from"), r["md"].get("split_from")))
        n = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND status = 'active'"
            "   AND metadata->'source_context'->>'kind' = $2 AND metadata->'source_context'->>'ref' = $3"
            "   AND (metadata->>'turn_token' = $4 OR metadata->'from_turn_tokens' ? $4)",
            E, went["kind"], went["ref"], ids["tok1"])
        check(f"{name}: nothing of the turn is said in the place it did not go", n == 0, n)
        state = getattr(close_turn, "thread_state", None)
        if state is not None:
            tag = ids["sid"][2:]
            here, there = await state(pool, E, src) or "", await state(pool, E, went) or ""
            check(f"{name}: where the turn stands (its goal) is read where it stayed",
                  f"{tag} goal" in here and f"{tag} goal" not in there, (here, there))

    os.environ["PII_MODE"], os.environ["PII_QUEUE_WORKER"] = "on", "off"
    _pc.sterilize = stand_in
    try:
        print("20. PII mode: Split from the turn onward")
        for side in ("left", "went"):
            S = await thread()
            ids = await seed_pii(S, f"q20{side[0]}")
            new = await ct.split_thread(pool, E, S["ref"], from_turn_id=ids["u1"])
            D = ct.descriptor_for(str(new["id"]))
            if side == "left":
                await landed("20", ids, S, D, handoff=False)
                await gone_side("20", ids, S, takes=False, kept_old=False)
            else:
                await gone_side("20", ids, D, takes=True, kept_old=True)

        for n, (label, mk_src) in enumerate((("a thread", "thread"), ("a project", "project")), start=21):
            print(f"{n}. PII mode: adopt into a project, {label} from the turn on")
            for side in ("left", "went"):
                S = await thread() if mk_src == "thread" else await project(f"PII src {n}{side}")
                P = await project(f"PII dst {n}{side}")
                ids = await seed_pii(S, f"q{n}{side[0]}")
                since = (await pool.fetchval("SELECT created_at FROM entity_memory WHERE id = $1",
                                             ids["u1"])).isoformat()
                res = await adopt_thread_core(pool, E, int(P["ref"]), S["kind"], S["ref"], since=since)
                if side == "left":
                    check(f"{n}: the adopt moved the turn", res.get("moved_turns") == 2, res)
                    await landed(str(n), ids, S, P, handoff=False)
                    await gone_side(str(n), ids, S, takes=False, kept_old=False)
                else:
                    await gone_side(str(n), ids, P, takes=True, kept_old=None)

        if not HAVE_PICK:
            absent("23-25: the Host's pick in PII mode (runtime/thread_choice)")
        else:
            print("23. PII mode: the Host's pick, thread to thread, the turn's writes still queued at its end")
            S, D = await thread(), await thread()
            ids = await seed_pii(S, "q23", mid=host_pick(S, D), end=lambda t: move_turn(t, S))
            check("23: the pick moved the exchange", ids["move"].get("moved"), ids["move"])
            await landed("23", ids, S, D, handoff=False)
            await pii_followed("23", ids, D)

            print("24. PII mode: the Host's pick of a thread of its own, the turn's writes still queued")
            S = await thread()
            ids = await seed_pii(S, "q24", mid=host_pick(S), end=lambda t: move_turn(t, S))
            N = ids["move"].get("to") if ids["move"].get("moved") else None
            check("24: the pick moved the exchange to a thread made for it",
                  N is not None and N["kind"] == "thread" and N["ref"] != S["ref"], ids["move"])
            if N is not None:
                await landed("24", ids, S, N, handoff=False)
                await pii_followed("24", ids, N)

            print("25. PII mode: the Host's pick, and the turn's writes do not land within the move's bound")
            S, D = await thread(), await thread()
            ids = await seed_pii(S, "q25", mid=host_pick(S, D),
                                 end=lambda t: move_turn(t, S, bound=0.5, land_first=1))
            check("25: the move is not made, and nothing of it is", not ids["move"].get("moved"), ids["move"])
            await stayed("25", ids, S, D)

        print("26. PII mode: a turn re-homed to the project its work touched, its writes still queued")
        S, D = await project("PII shed 26"), await project("PII garden 26")
        ids = await seed_pii(S, "q26", end=lambda t: move_turn(t, S, rehome_to=D))
        check("26: the re-home moved the message", ids["move"].get("moved_user_row"), ids["move"])
        await landed("26", ids, S, D, handoff=False)
        await pii_followed("26", ids, D)

        print("27. PII mode: a re-home whose turn's writes do not land within the move's bound")
        S, D = await project("PII shed 27"), await project("PII garden 27")
        ids = await seed_pii(S, "q27", end=lambda t: move_turn(t, S, rehome_to=D, bound=0.5, land_first=1))
        check("27: the re-home is not made, and nothing of it is", not ids["move"].get("moved_user_row"),
              ids["move"])
        await stayed("27", ids, S, D)
        check("20-27: the PII queue drained (nothing left unlanded)", _pq.stats()["pending"] == 0, _pq.stats())
    finally:
        _pc.sterilize = saved_st
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── a write whose message row lands late ────────────────────────────────
    print("16. close_turn in a turn whose message row is still being written")
    S = await thread()
    ready = asyncio.get_running_loop().create_future()

    async def _late():
        return await ready
    late_task = asyncio.ensure_future(_late())
    um = await turn_row(S, "s-l1", "user", "the porch light")

    async def _land():
        await asyncio.sleep(0.4)
        ready.set_result(um)
    asyncio.ensure_future(_land())
    with in_turn("s-l1", S, late_task):
        rl = await close_turn.execute({"preferences": [{"text": "The porch light is on a timer",
                                                        "reason": "said", "scope": {"kind": "thread"}}]},
                                      entity_id=E, pool=pool, session_id="s-l1")
    lp = await row(rl["result"]["writes"]["preferences"][0]["id"])
    check("16: the preference names the message once its row is written (waited for, bounded)",
          lp["md"].get("from_turns") == [um], lp["md"].get("from_turns"))


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
