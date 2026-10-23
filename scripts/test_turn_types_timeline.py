#!/usr/bin/env python3
"""
Turn types: the timeline block.

Container-free: APP_ROOT=<repo> python3 scripts/<this>. No pod, no DB, no
model; the facts reader runs its real SQL text against a stub pool that
answers by table, the renderer and the registry run for real.

  1. untyped is plain: on a Host with no types every line carries its title,
     no type labels; nothing to list renders nothing
  2. only platform facts cross types: a typed turn sees an item's own words
     only for its own type; other types' and untyped items show type, kind,
     id and time; an untyped turn on a typed Host sees only untyped words; a
     closed turn sees none
  3. the sources: chat turns from the registry (this turn left out, wake-up
     lanes left to their scheduled task, old finished turns dropped), agents
     and builds from entity_tasks, scheduled work and its runs from the
     calendar; ids are the ones the Host's tools take
  4. the type is recorded where work is recorded: a ContextVar per turn,
     stamped on every entity_tasks insert (v186), reported to the registry
     by a 2:turn_type frame, set by the session and by the runner
  5. wiring: a PER_TURN block after FRAME and the thread's index

Exit 0 = all pass; exit 1 + [turn-types-timeline] FAIL lines otherwise.
"""

import asyncio
import re
import os
import sys
from datetime import datetime, timedelta, timezone
from turn_source import turn_source  # session.py and the modules split out of it
from tools_source import tools_source  # tools.py and the modules split out of it

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types-timeline] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


FIN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OPS = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


class Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


class Pool:
    """Answers the timeline's reads by table; records every SQL + args."""

    def __init__(self, types=None, tasks=None, events=None, runs=None, fail=()):
        self.types, self.tasks = types or [], tasks or []
        self.events, self.runs = events or [], runs or []
        self.fail, self.seen = set(fail), []

    async def fetch(self, sql, *a):
        self.seen.append((sql, a))
        for key, rows in (("FROM turn_types", self.types), ("FROM entity_tasks", self.tasks),
                          ("FROM calendar_event_runs", self.runs),
                          ("FROM calendar_events", self.events)):
            if key in sql:
                if key in self.fail:
                    raise RuntimeError("db down")
                if key == "FROM entity_tasks":
                    # Answer the way the SQL asks: its statuses, its limit,
                    # and the total the window function would give.
                    hit = [r for r in rows if r["status"] in a[1]]
                    import re as _re
                    lim = int(_re.search(r"LIMIT (\d+)", sql).group(1))
                    return [Row(dict(r, total=len(hit))) for r in hit[:lim]]
                if key == "FROM calendar_events" and "recurrence IS NOT NULL" in sql:
                    # Every scheduled event's schedule: the events, plus the
                    # events the runs belong to.
                    seen, out = set(), []
                    for r in list(rows) + list(self.runs):
                        if r["event_id"] not in seen and r.get("recurrence"):
                            seen.add(r["event_id"])
                            out.append(Row({"event_id": r["event_id"], "recurrence": r["recurrence"]}))
                    return out
                if key == "FROM calendar_events":
                    import re as _re
                    lim = int(_re.search(r"LIMIT (\d+)", sql).group(1))
                    if "NOT (event_id::text = ANY($2::text[]))" in sql:
                        rows = [r for r in rows if r["status"] == "running" or r["event_id"] not in a[1]]
                    return [Row(dict(r, total=sum(1 for x in rows if x["status"] == r["status"])))
                            for r in rows[:lim]]
                if key == "FROM calendar_event_runs" and "NOT x.routine OR (NOT x.ok AND x.rn = 1)" in sql:
                    # Answer the way the SQL asks, with ITS inputs: routine =
                    # the event id is in the list the code passed (a[2]); a
                    # ceiling skip (error starts with a[3]) is its own group.
                    routine_ids, skip = set(a[2]), a[3]
                    groups: dict = {}
                    for r in rows:
                        sk = str(r.get("error") or "").startswith(skip)
                        groups.setdefault((r["event_id"], r["ok"], sk), []).append(r)
                    kept = []
                    for (eid_, ok_, _sk), grp in groups.items():
                        grp = sorted(grp, key=lambda r: r["ran_at"], reverse=True)
                        for i, r in enumerate(grp):
                            rt = eid_ in routine_ids
                            if not rt or (not ok_ and i == 0):
                                kept.append(dict(r, routine=rt, n_same=len(grp)))
                    kept.sort(key=lambda r: r["ran_at"], reverse=True)
                    return [Row(dict(r, total=len(kept))) for r in kept[:40]]
                return [Row(dict(r, total=len(rows))) for r in rows]
        return []


class Sess:
    def __init__(self, turn_type=None, session_id="s-me"):
        self.entity_id, self.session_id, self.turn_type = 7, session_id, turn_type


def _type(tid, name, status="active"):
    return {"id": tid, "name": name, "status": status}


def main() -> int:
    # The tree this script is in, unless APP_ROOT names another (/app in the
    # image is this script's own tree too).
    app_root = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(app_root, "brain"))
    sys.path.insert(0, app_root)
    src = lambda *p: open(os.path.join(app_root, *p), encoding="utf-8").read()
    from mastercode.runtime import timeline as tl
    from mastercode.runtime import turn_types as tt
    run = asyncio.run

    def facts(pool, sess, turns=()):
        return run(tl.timeline_facts(pool, sess, now=NOW, turns=list(turns)))

    ev = lambda eid, title, mins, type_id=None, status="scheduled", op=False, rec=None: {
        "event_id": eid, "title": title, "start_time": NOW + timedelta(minutes=mins),
        "recurrence": rec, "status": status, "type_id": type_id, "is_op": op}
    task = lambda tid, title, status, type_id=None, kind="agent", mins=-10: {
        "task_id": tid, "title": title, "task_type": kind, "status": status,
        "created_at": NOW + timedelta(minutes=mins), "started_at": NOW + timedelta(minutes=mins),
        "completed_at": (NOW + timedelta(minutes=mins)) if status in ("completed", "failed") else None,
        "type_id": type_id}

    # ── 1. Untyped is plain.
    p = Pool(events=[ev("e-1", "check the deploy", 30, rec="daily")],
             tasks=[task(88, "research vendors", "in_progress")],
             runs=[{"event_id": "e-0", "title": "morning brief", "ran_at": NOW - timedelta(hours=1),
                    "ok": True, "error": None, "type_id": None, "is_op": False}])
    out = tl.render_timeline(facts(p, Sess()), None)
    check("an untyped Host's timeline lists running, finished and next with their titles",
          out and out.startswith("## Timeline")
          and '- agent task 88 "research vendors" — running, since 11:50 UTC (10 min ago)' in out
          and '- wake-up event_id e-0 "morning brief" — done, 11:00 UTC (60 min ago)' in out
          and '- wake-up event_id e-1 "check the deploy" — due 12:30 UTC (in 30 min), repeats daily' in out,
          out)
    check("...with no type labels on a Host that has no types", "[" not in out.split("\n", 2)[2], out)
    check("...and names the tools that re-read an item by the ids it prints",
          "schedule_task op='list' (event_id)" in out and "entity_tasks op='list' (task_id)" in out)
    check("nothing to list renders nothing", tl.render_timeline(facts(Pool(), Sess()), None) is None)

    # ── 2. Only platform facts cross types.
    typed = Pool(types=[{"id": FIN, "name": "Finance"}, {"id": OPS, "name": "Ops"}],
                 events=[ev("e-f", "reconcile the ledger", 60, FIN),
                         ev("e-o", "restart web-1 now", 90, OPS),
                         ev("e-u", "daily brief", 120)],
                 tasks=[task(90, "pay the invoice", "in_progress", FIN),
                        task(91, "rotate the key", "in_progress", OPS, kind="factory_build")])
    fin_view = tl.render_timeline(facts(typed, Sess(_type(FIN, "Finance"))), _type(FIN, "Finance"))
    check("a typed turn sees its own type's words",
          '- [Finance] wake-up event_id e-f "reconcile the ledger"' in fin_view
          and '- [Finance] agent task 90 "pay the invoice"' in fin_view, fin_view)
    check("...never another type's: type, kind, id and time only",
          "- [Ops] wake-up event_id e-o — due" in fin_view and "restart web-1" not in fin_view
          and "- [Ops] build task 91 — running" in fin_view and "rotate the key" not in fin_view, fin_view)
    check("...nor an untyped item's", "- [no type] wake-up event_id e-u — due" in fin_view
          and "daily brief" not in fin_view, fin_view)
    check("...and says why some lines carry no words",
          "An item's own words are shown only for work of this turn's type." in fin_view)
    un_view = tl.render_timeline(facts(typed, Sess()), None)
    check("an untyped turn on a typed Host sees only untyped words (no type's words reach the widest turn)",
          '"daily brief"' in un_view and "reconcile" not in un_view and "restart" not in un_view
          and "pay the invoice" not in un_view, un_view)
    closed = tt.closed_type("unreadable")
    cl_view = tl.render_timeline(facts(typed, Sess(closed)), closed)
    check("a closed turn sees no item's words at all",
          '"' not in cl_view.split("\n", 3)[3] and "[Finance]" in cl_view, cl_view)
    gone = Pool(types=[{"id": FIN, "name": "Finance"}], events=[ev("e-x", "old", 5, OPS)])
    check("an item of a type that no longer exists is labelled so, not guessed",
          "[a removed type] wake-up event_id e-x — due" in tl.render_timeline(facts(gone, Sess()), None))
    messy = Pool(events=[ev("e-m", 'line one\n\n"quoted"  ' + "x" * 80, 5)])
    mv = tl.render_timeline(facts(messy, Sess()), None)
    check("an item's words are one line, quotes neutralised, capped",
          "\"line one 'quoted' xxx" in mv and mv.count("\n") == 3 and "…\"" in mv, mv)

    # ── 3. The sources.
    turns = [
        {"turn_id": "t-mine-000", "thread_key": "chat|7|none", "status": "running", "session_id": "s-me",
         "started_at": (NOW - timedelta(minutes=1)).timestamp(), "user_message": "this turn"},
        {"turn_id": "t-other-00", "thread_key": "chat|7|none|th-2", "status": "running", "session_id": "s-2",
         "started_at": (NOW - timedelta(minutes=4)).timestamp(), "user_message": "draft the memo",
         "turn_type_id": None, "turn_type_status": "untyped"},
        {"turn_id": "t-fin-0000", "thread_key": "chat|7|none|th-3", "status": "running", "session_id": "s-3",
         "started_at": (NOW - timedelta(minutes=2)).timestamp(), "user_message": "pay rent",
         "turn_type_id": FIN, "turn_type_status": "active"},
        {"turn_id": "t-closed-0", "thread_key": "chat|7|none|th-4", "status": "running", "session_id": "s-4",
         "started_at": (NOW - timedelta(minutes=3)).timestamp(), "user_message": "x",
         "turn_type_status": "closed"},
        {"turn_id": "t-wake-000", "thread_key": "wakeup|7|55", "status": "running", "session_id": "s-5",
         "started_at": (NOW - timedelta(minutes=1)).timestamp(), "user_message": "(wake-up)"},
        {"turn_id": "t-done-000", "thread_key": "chat|7|none", "status": "done", "session_id": "s-6",
         "started_at": (NOW - timedelta(minutes=9)).timestamp(),
         "finished_at": (NOW - timedelta(minutes=8)).timestamp(), "user_message": "summarise",
         "turn_type_status": "untyped"},
        {"turn_id": "t-new-0000", "thread_key": "chat|7|none|th-9", "status": "running", "session_id": "s-9",
         "started_at": (NOW - timedelta(seconds=5)).timestamp(), "user_message": "wire the refund"},
        {"turn_id": "t-old-0000", "thread_key": "chat|7|none", "status": "done", "session_id": "s-7",
         "started_at": (NOW - timedelta(hours=9)).timestamp(),
         "finished_at": (NOW - timedelta(hours=8)).timestamp(), "user_message": "ancient"},
    ]
    tv = tl.render_timeline(facts(Pool(types=[{"id": FIN, "name": "Finance"}]), Sess(), turns), None)
    check("other chat turns are listed; THIS turn is not",
          '[no type] chat turn "draft the memo" — running' in tv and '"this turn"' not in tv, tv)
    check("a running turn's type comes from the registry; a closed one is labelled closed",
          "[Finance] chat turn — running" in tv and "[closed type] chat turn — running" in tv
          and "pay rent" not in tv, tv)
    check("a wake-up's registry lane is left to its scheduled task (one line per piece of work)",
          "(wake-up)" not in tv and tv.count("chat turn") == 5, tv)
    check("a turn whose type has not been reported yet is never taken for untyped: no words cross",
          "[type not known yet] chat turn — running" in tv and "wire the refund" not in tv, tv)
    nv = tl.render_timeline(facts(Pool(), Sess(), [t for t in turns if t["turn_id"] == "t-new-0000"]), None)
    check("...while on a Host with no types (nothing to cross) its words show",
          nv and '"wire the refund"' in nv, nv)
    check("a finished turn is listed with its outcome; one past the window is not",
          '[no type] chat turn "summarise" — done, 11:52 UTC (8 min ago)' in tv and "ancient" not in tv, tv)
    many = Pool(events=[ev(f"e-{i}", f"t{i}", 10 + i) for i in range(8)])
    mview = tl.render_timeline(facts(many, Sess()), None)
    check("each section lists at most PER_SECTION items and counts the rest",
          mview.count("— due") == tl.PER_SECTION and "- …and 3 more" in mview
          and mview.index('"t0"') < mview.index('"t4"'), mview)
    sk = Pool(runs=[{"event_id": "e-s", "title": "t", "ran_at": NOW - timedelta(minutes=5), "ok": False,
                     "error": __import__("mastercode.brain.offline_task_runner", fromlist=["x"]).CEILING_SKIP_PREFIX + " x",
                     "type_id": None, "is_op": True}],
              tasks=[task(92, "b", "failed", kind="factory_build", mins=-30)],
              events=[ev("e-r", "sync", -5, status="running", op=True)])
    sv = tl.render_timeline(facts(sk, Sess()), None)
    check("a ceiling skip reads as a skip, a failed build as failed, a running op as running",
          '- scheduled op event_id e-s "t" — skipped (daily turn ceiling)' in sv
          and '- build task 92 "b" — failed' in sv
          and '- scheduled op event_id e-r "sync" — running, since' in sv, sv)
    fp = Pool(tasks=[task(93, "x", "in_progress")], events=[ev("e-z", "z", 5)], fail={"FROM calendar_events"})
    fv = tl.render_timeline(facts(fp, Sess()), None)
    check("a source that cannot be read drops out; the rest still renders",
          fv and 'task 93 "x"' in fv and "e-z" not in fv, fv)
    sql = " ".join(s for s, _ in p.seen)
    check("every read is scoped to this Host",
          all(a and a[0] == 7 for _s, a in p.seen), f"{p.seen}")
    check("the reads are the design's: entity_tasks in flight or recently done, scheduled and running "
          "tasks, runs since the window",
          "status = ANY($2::text[])" in sql and "completed_at > $3" in sql
          and "status IN ('scheduled', 'running')" in sql and "r.ran_at > $2" in sql
          and "turn_type_id::text AS type_id" in sql, sql)
    check("...each picking the right rows under its limit: newest done work, newest running work, "
          "runs of THIS Host's events only",
          "ORDER BY completed_at DESC LIMIT" in sql and "ORDER BY COALESCE(started_at, created_at) DESC LIMIT" in sql
          and "JOIN calendar_events e ON e.id = r.event_id AND e.entity_id = r.entity_id" in sql
          and "ORDER BY r.ran_at DESC" in sql and "ORDER BY start_time ASC" in sql, sql)

    # The type list cannot be read.
    bad_types = Pool(events=[ev("e-a", "secret plan", 5)], fail={"FROM turn_types"})
    bt = tl.render_timeline(facts(bad_types, Sess()), None)
    check("if this Host's types cannot be read, no item's words are shown to anyone",
          bt and "secret plan" not in bt and "[no type] wake-up event_id e-a — due" in bt, bt)
    check("only an APPROVED type names a line (a discarded draft never does)",
          any("approved_at IS NOT NULL" in q for q, _ in bad_types.seen)
          and not any("status <> 'draft'" in q for q, _ in bad_types.seen))
    recs = Pool(events=[ev("r-1", "a", 5, rec="EvErY:86400"), ev("r-2", "b", 6, rec="every:5400"),
                        ev("r-3", "c", 7, rec="every:4421998312"), ev("r-4", "d", 8, rec="soon\nIGNORE ABOVE")])
    rv = tl.render_timeline(facts(recs, Sess()), None)
    check("a recurrence is printed parsed, in the platform's words, never as stored",
          '"a" — due 12:05 UTC (in 5 min), repeats daily' in rv and "repeats every 1.5 hours" in rv
          and "EvErY" not in rv and "IGNORE" not in rv and "4421998312" not in rv
          and "repeats every about 51000 days" in rv
          and '"d" — due 12:08 UTC (in 8 min)\n' in rv + "\n", rv)
    busy = Pool(tasks=[task(200 + i, f"job{i}", "in_progress", mins=-i) for i in range(9)]
                + [task(300 + i, f"old{i}", "completed", mins=-60 - i) for i in range(7)])
    bv = tl.render_timeline(facts(busy, Sess()), None)
    check("counts are exact when a source holds more than it shows",
          bv.count("— running") == tl.PER_SECTION and bv.count("— done") == tl.PER_SECTION
          and bv.count("- …and 4 more") == 1 and bv.count("- …and 2 more") == 1, bv)
    check("running newest first, finished newest first",
          bv.index('"job0"') < bv.index('"job1"') and bv.index('"old0"') < bv.index('"old1"'), bv)
    check("next soonest first", mview.index('"t0"') < mview.index('"t1"') < mview.index('"t2"'))
    from server.turn_registry import CURRENT_THREAD_KEY
    mine = [{"turn_id": "t-k", "thread_key": "chat|7|none|th-k", "status": "running", "session_id": "s-late",
             "started_at": NOW.timestamp(), "user_message": "me", "turn_type_status": "untyped"},
            {"turn_id": "t-p", "thread_key": "session|s-me", "status": "done", "session_id": "s-me",
             "started_at": (NOW - timedelta(minutes=3)).timestamp(),
             "finished_at": (NOW - timedelta(minutes=2)).timestamp(), "user_message": "earlier",
             "turn_type_status": "untyped"}]

    async def own_key_facts():
        CURRENT_THREAD_KEY.set("chat|7|none|th-k")
        return await tl.timeline_facts(Pool(), Sess(), now=NOW, turns=mine)
    ov = tl.render_timeline(run(own_key_facts()), None)
    check("this turn is left out by its registry key, even before its session frame lands",
          ov and '"me"' not in ov, ov)
    check("...and only while running: this session's earlier finished turn is listed",
          ov and '"earlier" — done' in ov, ov)
    bsrc0 = src("server", "brain_service.py")
    i_key = bsrc0.find("CURRENT_THREAD_KEY.set(thread_key)")
    i_start = bsrc0.find("rec, mode = get_turn_registry().start_or_attach(")
    check("brain_service sets the key before start_or_attach creates the turn's task",
          0 < i_key < i_start, f"{i_key} {i_start}")
    rsrc0 = src("server", "turn_registry.py")
    check("...and the registry creates that task inside start_or_attach (so it inherits the key)",
          "rec.task = asyncio.create_task(" in rsrc0[rsrc0.find("def start_or_attach"):rsrc0.find("def _finish") if "def _finish" in rsrc0 else None])
    from server.brain_service import turn_type_frame
    import json as _json
    fr = _json.loads(turn_type_frame({"turn_type_id": FIN, "turn_type_status": "active"})[2:])
    fr0 = _json.loads(turn_type_frame({"turn_type_id": None, "turn_type_status": None})[2:])
    check("the 2:turn_type frame carries ids and a status, never a name, and 'untyped' by default",
          fr == [{"type": "turn_type", "turn_type_id": FIN, "turn_type_status": "active"}]
          and fr0[0]["turn_type_status"] == "untyped" and "name" not in fr[0], f"{fr} {fr0}")
    asrc = src("server", "routes", "applications.py")
    i_lt = asrc.find("_proj_type = await load_turn_type(")
    i_sv = asrc.find("out[\"verify_task_id\"] = await start_verify(")
    check("an app check the Ship button starts records the project's type (no turn to take it from)",
          0 < i_lt < i_sv and "if CURRENT_TURN_TYPE.get() is None:" in asrc[i_lt - 300:i_lt]
          and "_tt_tok = CURRENT_TURN_TYPE.set(_proj_type)" in asrc[i_lt:i_sv]
          and "_CTT.reset(_tt_tok)" in asrc[i_sv:i_sv + 2000])
    lots = Pool(events=[ev(f"n-{i}", f"n{i}", 10 + i) for i in range(45)])
    lv = tl.render_timeline(facts(lots, Sess()), None)
    check("the 'next' count is the table's total, beyond what one read returns (45 due, 40 read)",
          lv.count("— due") == tl.PER_SECTION and "- …and 40 more" in lv, lv)

    # ── 4. The type is recorded where work is recorded.
    check("the current type is None until a turn sets it", tt.current_turn_type_id() is None)

    async def two_turns():
        seen = {}

        async def one(name, t):
            tt.set_current_turn_type(t)
            await asyncio.sleep(0.01)
            seen[name] = tt.current_turn_type_id()
        await asyncio.gather(one("fin", _type(FIN, "Finance")), one("none", None),
                             one("closed", tt.closed_type("x")))
        return seen
    got = run(two_turns())
    check("each turn sees only its own type (no leak between turns side by side)",
          got == {"fin": FIN, "none": None, "closed": None}, f"{got}")
    from mastercode.brain.task_manager import TaskManager
    inserted = []

    class TPool:
        async def fetchrow(self, sql, *a):
            inserted.append((sql, a))
            return {"task_id": 5}

        async def execute(self, *a, **k):
            return None

    async def create_under(t):
        tt.set_current_turn_type(t)
        return await TaskManager(TPool(), 7).create_task("x", task_type="agent")
    run(create_under(_type(FIN, "Finance")))
    run(create_under(None))
    check("an agent's task row records the type of the turn that started it (v186)",
          "turn_type_id" in inserted[0][0] and inserted[0][1][-1] == FIN and inserted[1][1][-1] is None,
          f"{inserted}")
    tsrc = tools_source()
    avsrc = src("brain", "mastercode", "runtime", "app_verify.py")
    check("every entity_tasks insert stamps it: compare, build and app check too",
          tsrc.count("status, started_at, turn_type_id, result) ") == 2
          and tsrc.count("_current_turn_type_id(), json.dumps(result),") == 2
          and "TASK_TYPE, _current_turn_type_id(), json.dumps(result))" in avsrc)
    import re as _re
    inserts = []
    for root, _d, files in os.walk(os.path.join(app_root, "brain")):
        for fn in files:
            if fn.endswith(".py"):
                s_ = open(os.path.join(root, fn), encoding="utf-8").read()
                inserts += [(fn, m.start()) for m in _re.finditer(r"INSERT INTO entity_tasks \(", s_)
                            if "turn_type_id" not in s_[m.start():m.start() + 200]]
    for root, _d, files in os.walk(os.path.join(app_root, "server")):
        for fn in files:
            if fn.endswith(".py"):
                s_ = open(os.path.join(root, fn), encoding="utf-8").read()
                inserts += [(fn, m.start()) for m in _re.finditer(r"INSERT INTO entity_tasks \(", s_)
                            if "turn_type_id" not in s_[m.start():m.start() + 200]]
    check("no entity_tasks insert anywhere in brain/ or server/ leaves the type out (repo swept)",
          inserts == [], f"{inserts}")
    msrc = src("migrations", "v186_entity_tasks_turn_type.sql")
    check("v186 adds entity_tasks.turn_type_id",
          "ALTER TABLE entity_tasks" in msrc and "turn_type_id UUID NULL REFERENCES turn_types(id)" in msrc)
    from server.turn_registry import TurnRecord, TurnRegistry
    rec = TurnRecord(turn_id="t", thread_key="k", entity_id=7, app_id=None, user_message="m", started_at=0.0)
    reg = TurnRegistry()
    reg._observe(rec, '2:[{"type":"session","session_id":"s","turn_token":"tok"}]\n')
    reg._observe(rec, '2:[{"type":"turn_type","turn_type_id":"%s","turn_type_status":"active"}]\n' % FIN)
    d = rec.describe()
    check("the registry takes a turn's type off its 2:turn_type frame, after the session frame",
          rec.session_id == "s" and d["turn_type_id"] == FIN and d["turn_type_status"] == "active"
          and "finished_at" in d, f"{d}")
    rec2 = TurnRecord(turn_id="t2", thread_key="k2", entity_id=7, app_id=None, user_message="m", started_at=0.0)
    reg._observe(rec2, '2:[{"type":"turn_type","turn_type_id":null,"turn_type_status":"closed"}]\n')
    reg._observe(rec2, '2:[{"type":"session","session_id":"s2","turn_token":"t2"}]\n')
    check("...in either order, and a closed turn is recorded closed",
          rec2.turn_type_status == "closed" and rec2.turn_type_id is None and rec2.session_id == "s2")
    bsrc = src("server", "brain_service.py")
    check("brain_service turns the event into a data frame of ids only",
          'if kind == "turn_type":' in bsrc and '"type": "turn_type",' in bsrc
          and '"turn_type_id": event.get("turn_type_id"),' in bsrc)
    ssrc = turn_source()
    i_set = ssrc.find("set_current_turn_type(getattr(sess, \"turn_type\", None))")
    i_res = ssrc.find("sess.turn_type = await resolve_session_type(")
    i_prompt = ssrc.find("_pt_block = await build_per_turn_prompt(self, sess, user_message)")
    # The report is the first turn_type event after the record, and it comes
    # before the prompt is built; held to that order, not to a distance, so
    # another stamp of the type between the two (the memory rows' own) does
    # not read as a missing report.
    i_evt = ssrc.find('"event": "turn_type",', i_set) if i_set > 0 else -1
    check("the session records the type once it resolves, before the prompt is built, and reports it",
          0 < i_res < i_set < i_evt < i_prompt, (i_res, i_set, i_evt, i_prompt))
    check("...saying 'untyped' outright, so the registry can tell it from 'not known yet'",
          i_evt > 0 and '"turn_type_status": (_tt_now.get("status") or "untyped") if _tt_now else "untyped",'
          in ssrc[i_evt:i_evt + 600])
    # The runner: the op lane records the task's type for what the op starts.
    from mastercode.brain import offline_task_runner as otr
    import mastercode.runtime.direct_ops as dops
    seen_op = []

    async def fake_op(tool, args, **kw):
        seen_op.append(tt.current_turn_type_id())
        return {"status": "ok", "result": "done"}
    dops.run_direct_op = fake_op
    dops.is_unattended_op_tool = lambda n: True

    async def by_id(pool, eid, tid):
        return _type(FIN, "Finance")
    tt.load_turn_type_by_id = by_id

    async def quiet(*a, **k):
        return None
    otr._report_and_finalize = quiet

    async def zero(*a, **k):
        return 0
    otr.runs_in_last_24h = zero

    async def fire():
        await otr._run_one(None, 7, {"id": 1, "title": "t", "recurrence": None, "start_time": None,
                                     "payload": {"type_id": FIN, "op": {"tool": "list_projects", "args": {}}}})
    asyncio.run(fire())
    check("a scheduled op runs with its task's type recorded (what it starts is stamped)",
          seen_op == [FIN], f"{seen_op}")

    # ── 5. Wiring.
    from mastercode.runtime import prompt_schema as ps
    names = [b.name for b in ps.PROMPT_SCHEMA]
    blk = next((b for b in ps.PROMPT_SCHEMA if b.name == "timeline"), None)
    check("the timeline is a PER_TURN block, for every turn, after FRAME and the thread's index",
          blk is not None and blk.freshness == ps.Freshness.PER_TURN and blk.gate(Sess()) is True
          and names.index("frame") < names.index("knowledge_toc") < names.index("timeline"), f"{names}")

    class RT:
        pool = None

    async def canned(pool, sess, **k):
        return {"now": NOW, "types": {FIN: "Finance"}, "typed_host": True,
                "running": [], "finished": [], "counts": {"next": 2},
                "next": [{"kind": "wake-up", "ref": "event_id f", "type_id": FIN, "text": "fin words",
                          "at": NOW + timedelta(minutes=5)},
                         {"kind": "wake-up", "ref": "event_id u", "type_id": None, "text": "plain words",
                          "at": NOW + timedelta(minutes=6)}]}
    save = tl.timeline_facts
    tl.timeline_facts = canned
    bview = run(ps._b_timeline(RT(), Sess(_type(FIN, "Finance")), "hi"))
    check("the prompt block renders for THIS turn's type (the viewer is the session's type)",
          bview and '"fin words"' in bview and "plain words" not in bview, bview)
    tl.timeline_facts = save

    async def boom(*a, **k):
        raise RuntimeError("down")
    save = tl.timeline_facts
    tl.timeline_facts = boom
    check("a timeline that cannot be read never fails the turn (the block is skipped)",
          run(ps._b_timeline(RT(), Sess(), "hi")) is None)
    tl.timeline_facts = save

    # ── Routine work: scheduled work repeating more often than
    # hourly — decided by its schedule, never by counting its runs.
    def _run(eid_, title, mins_ago, ok, rec, err=None, op=True):
        return {"event_id": eid_, "title": title, "ok": ok, "error": err, "recurrence": rec,
                "ran_at": NOW - timedelta(minutes=mins_ago), "type_id": None, "is_op": op}
    routine_ok = [_run("e-r", "oversight sweep", 5 * i, True, "every:300") for i in range(1, 13)]
    new_routine = [_run("e-n", "new 5-min op", 5 * i, True, "every:300") for i in range(1, 4)]
    flapping = [_run("e-f", "support sweep", 5 * i, False, "every:300", "store unreachable")
                for i in range(1, 21)]
    hourly7 = [_run("e-h", "hourly digest", 60 * i - 1, True, "hourly", op=False) for i in range(1, 8)]
    half_hour = [_run("e-30", "half-hourly check", 30 * i, True, "every:1800") for i in range(1, 4)]
    brief = [_run("e-b", "morning brief", 120, True, None, op=False)]
    p = Pool(events=[ev("e-r", "oversight sweep", 3, op=True, rec="every:300"),
                     ev("e-h", "hourly digest", 40, rec="hourly"),
                     ev("e-d", "check the deploy", 90, rec="daily")],
             runs=routine_ok + new_routine + flapping + hourly7 + half_hour + brief)
    f = facts(p, Sess())
    fin = [(i["ref"], i["outcome"]) for i in f["finished"]]
    allfin = fin  # PER_SECTION caps the list; check what was kept via the SQL answer too
    kept_refs = {r["event_id"] for r in run(p.fetch(
        [q for q, _a in p.seen if "FROM calendar_event_runs" in q][-1],
        *[a for q, a in p.seen if "FROM calendar_event_runs" in q][-1]))}
    check("routine: successful runs of routine work never come back from the read",
          "e-r" not in kept_refs and "e-n" not in kept_refs and "e-30" not in kept_refs, f"{kept_refs}")
    check("routine: a new routine op is left out from its first run (schedule, not run count)",
          "e-n" not in kept_refs, f"{kept_refs}")
    check("routine: a failing routine op is ONE line — its latest failure, with the count",
          ("event_id e-f", "failed (20 times in 6 h)") in fin
          and sum(1 for ref, _o in fin if ref == "event_id e-f") == 1, f"{fin}")
    check("routine: hourly work is not routine, even with 7 runs in the window",
          "e-h" in kept_refs, f"{kept_refs}")
    check("routine: a one-off result is not crowded out",
          "e-b" in kept_refs, f"{kept_refs}")
    nxt = [i["ref"] for i in f["next"]]
    check("routine: 'Next' leaves out routine work, in SQL",
          "event_id e-r" not in nxt and "event_id e-h" in nxt and "event_id e-d" in nxt, f"{nxt}")
    check("routine: 'Next' count counts only what the SQL returned (no routine in the total)",
          f["counts"]["next"] == 2, f"{f['counts']}")
    runs_sql = [q for q, _a in p.seen if "FROM calendar_event_runs" in q][-1]
    check("routine: the routine rule sits in SQL before the LIMIT",
          runs_sql.index("NOT x.routine") < runs_sql.index("LIMIT 40"))
    runs_args = [a for q, a in p.seen if "FROM calendar_event_runs" in q][-1]
    check("routine: the ids handed to SQL are the scheduler's own verdict",
          set(runs_args[2]) == {"e-r", "e-n", "e-f", "e-30"}, f"{runs_args[2]}")
    for form, want in (("every:300", True), ("every: 300", True), ("every:+300", True),
                       ("every:3_00", True), ("EVERY:300", True), ("every:3599", True),
                       ("every:3600", False), ("hourly", False), ("daily", False),
                       ("every:0", False), ("", False), (None, False),
                       ("every:99999999999999999999", False), ("garbage", False)):
        check(f"routine: {form!r} is {'routine' if want else 'not routine'} (the scheduler's reading)",
              tl._is_routine(form) is want)
    runs_sql_text = [q for q, _a in p.seen if "FROM calendar_event_runs" in q][-1]
    check("routine: the real query groups runs by event, ok AND ceiling-skip, in both windows",
          runs_sql_text.count("PARTITION BY r.event_id, r.ok, ") == 2
          and runs_sql_text.count("(left(coalesce(r.error, ''), length($4)) = $4)") == 2
          and "(e.event_id::text = ANY($3::text[])) AS routine" in runs_sql_text,
          runs_sql_text[:300])
    check("routine: no schedule text is parsed in SQL (nothing to overflow or disagree)",
          not any("::bigint" in q or "every:" in q for q, _a in p.seen))
    # A ceiling skip is its own group: it neither hides a real failure nor is
    # counted as one.
    skips = [_run("e-c", "prompt task", 5 * i, False, "every:300",
                  "ScheduledTurnCeiling: daily ceiling reached") for i in range(1, 4)]
    fails = [_run("e-c", "prompt task", 30 + 5 * i, False, "every:300", "model error")
             for i in range(1, 3)]
    p2 = Pool(runs=skips + fails)
    fin2 = [(i["ref"], i["outcome"]) for i in facts(p2, Sess())["finished"]]
    check("routine: the latest ceiling skip and the latest real failure are separate lines; "
          "the failure count counts failures only",
          ("event_id e-c", "skipped (daily turn ceiling)") in fin2
          and ("event_id e-c", "failed (2 times in 6 h)") in fin2, f"{fin2}")

    print()
    if FAILS:
        print(f"[turn-types-timeline] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("[turn-types-timeline] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
