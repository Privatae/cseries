#!/usr/bin/env python3
"""
Turn types: parallel wake-ups, each under its own type.

Container-free: APP_ROOT=<repo> python3 scripts/<this>. No pod, no DB, no
model; the runner, the schedule_task executor and the loop run for real
against stubs.

  1. untyped is unchanged: a payload with no type_id fires untyped (no
     type on the op, none on the turn), and an untyped turn schedules with
     no type unless it names one
  2. lanes: every wake-up has its own turn lane (distinct keys per event),
     and the loop runs due tasks side by side up to half the turn ceiling —
     never more — without waiting on one to claim the next
  3. fire time: a task whose type is no longer active runs NOTHING (neither
     lane) and says so; an active type rides into the op (the gate's wall)
     and into the turn (pinned, never the thread's binding)
  4. scheduling from a typed turn: never untyped, and only ever under its
     OWN type (no hidden notes between types — the thread is the chain of
     thought); an inactive type is refused; a closed turn schedules nothing;
     calendar_log schedules nothing for any Host turn
  4b. the thread: the plan is written into the thread the task was set in
     before it can fire (withdrawn if that write fails), a saved template
     likewise, and the fired turn opens on the platform's pointer, thinks in
     that thread, and its own rows stay off the transcript
  5. wiring: the session prefers the pinned type over the thread binding,
     brain_service forwards it, the loop cancels its wake-ups on shutdown

Exit 0 = all pass; exit 1 + [turn-types-wake] FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys
from turn_source import turn_source  # session.py and the modules split out of it

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types-wake] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


FIN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OPS = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
GONE = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _type(tid, name, status="active"):
    return {"id": tid, "name": name, "status": status, "keywords": [], "context": {},
            "skill": None, "tool_groups": ["comms"], "grant_names": ["read_context"],
            "keys": [], "bastions": [], "model": None, "thread_id": None, "version": 1}


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    sys.path.insert(0, app_root)

    from mastercode.brain import offline_task_runner as otr
    from mastercode.runtime import turn_types as tt
    from mastercode.runtime import tools as tools_mod
    import server.brain_service as bs
    src = lambda *p: open(os.path.join(app_root, *p), encoding="utf-8").read()
    ssrc_all = turn_source()
    rsrc = src("brain", "mastercode", "brain", "offline_task_runner.py")

    TYPES = {FIN: _type(FIN, "Finance"), OPS: _type(OPS, "Ops"), GONE: _type(GONE, "Gone", "retired")}

    async def fake_by_id(pool, eid, type_id):
        t = TYPES.get(str(type_id))
        if t is None:
            return tt.closed_type("the turn type this task was scheduled under no longer exists")
        if t["status"] != "active":
            return tt.closed_type(f"the turn type this task was scheduled under is {t['status']}")
        return dict(t)
    tt.load_turn_type_by_id = fake_by_id

    reports = []

    async def fake_report(pool, eid, event_id, title, recurrence, start_time, ok, content, error, **kw):
        reports.append({"event": event_id, "ok": ok, "content": content, "error": error,
                        "recurrence": recurrence})
    otr._report_and_finalize = fake_report

    async def zero_runs(*a, **k):
        return 0
    otr.runs_in_last_24h = zero_runs

    op_calls, turn_calls = [], []

    posts, post_ok, order = [], {"v": True}, []

    async def fake_post(pool, eid, origin, text, **kw):
        posts.append({"origin": origin, "text": text, **kw})
        order.append("post")
        return post_ok["v"]
    real_post = otr.post_to_origin_thread
    otr.post_to_origin_thread = fake_post

    async def fake_run_direct_op(tool, args, **kw):
        op_calls.append({"tool": tool, **kw})
        return {"status": "ok", "result": "done"}
    import mastercode.runtime.direct_ops as dops
    dops.run_direct_op = fake_run_direct_op
    dops.is_unattended_op_tool = lambda name: True

    async def fake_send_message(**kw):
        turn_calls.append(kw)
        yield '0:"fine"\n'
    bs.send_message = fake_send_message
    bs.get_runtime = lambda: object()

    def row(eid_, payload):
        return {"id": eid_, "title": "t", "recurrence": None, "start_time": None, "payload": payload}

    run = asyncio.run

    # ── 1. Untyped is unchanged.
    run(otr._run_one(None, 7, row(1, {"op": {"tool": "list_projects", "args": {}}})))
    check("an untyped op fires with no type", op_calls and op_calls[-1].get("turn_type") is None, f"{op_calls}")
    run(otr._run_one(None, 7, row(2, {"prompt": "check the build"})))
    check("an untyped turn fires with no pinned type", turn_calls and turn_calls[-1].get("turn_type_id") is None,
          f"{turn_calls[-1] if turn_calls else None}")
    check("payload_type_id reads nothing from an untyped payload",
          otr.payload_type_id({}) is None and otr.payload_type_id({"type_id": "  "}) is None)

    # ── 2. Lanes.
    check("every wake-up has its own lane", otr.wakeup_thread_key(7, 1) != otr.wakeup_thread_key(7, 2)
          and otr.wakeup_thread_key(7, 1).startswith("wakeup|"))
    check("the fired turn uses its own lane", turn_calls[-1].get("thread_key") == otr.wakeup_thread_key(7, 2),
          f"{turn_calls[-1].get('thread_key')}")
    # No count of turns: as many wake-ups as the Host's
    # memory holds. A Host with room for three more turns.
    import tempfile as _tf
    from mastercode.runtime import host_memory as _hm
    _cg_saved, _cg = _hm.CGROUP_ROOT, _tf.mkdtemp(prefix="cg-")
    _cost_mb = _hm.TURN_COST_BYTES / 1048576
    for _n, _v in (("memory.max", str(512 * 1048576)),
                   ("memory.current", str(int((512 - 3.5 * _cost_mb) * 1048576))),
                   ("memory.stat", "anon 1\ninactive_file 0\n")):
        with open(os.path.join(_cg, _n), "w") as _f:
            _f.write(_v)
    _hm.CGROUP_ROOT = _cg
    check("concurrency is what the Host's memory holds (three more turns here)",
          otr.wakeup_concurrency() == 3 == _hm.turns_that_fit(), otr.wakeup_concurrency())

    # The real loop: 7 due tasks, each takes a while; count how many run at once.
    state = {"now": 0, "peak": 0, "done": 0, "claimed": set()}
    due_ids = list(range(100, 107))

    class LoopPool:
        executed = []

        async def execute(self, sql, *a):
            LoopPool.executed.append((sql, a))
            return "UPDATE 0"

        async def fetch(self, sql, *a):
            left = [i for i in due_ids if i not in state["claimed"]]
            return [{"id": i} for i in left[: a[1]]]

        async def fetchrow(self, sql, *a):
            if a[0] in state["claimed"]:
                return None
            state["claimed"].add(a[0])
            return row(a[0], {"prompt": "x"})

    tagged = []

    async def slow_run_one(pool, eid, claimed):
        # The task the loop made for this claim carries its id, which is what
        # keeps the orphan sweep off it.
        tagged.append(getattr(asyncio.current_task(), "_event_id", None) == claimed["id"])
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        state["done"] += 1

    real_run_one = otr._run_one
    otr._run_one = slow_run_one
    otr.POLL_INTERVAL_S = 0.005

    async def drive():
        loop_task = asyncio.create_task(otr.run_offline_tasks_loop(LoopPool(), 7))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if state["done"] == len(due_ids):
                break
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
    run(drive())
    otr._run_one = real_run_one
    limit = otr.wakeup_concurrency()
    check("due tasks run side by side (more than one at once)", state["peak"] > 1 or limit == 1, f"peak={state['peak']}")
    check("never more at once than the Host's memory holds", state["peak"] <= limit,
          f"peak={state['peak']} limit={limit}")
    _hm.CGROUP_ROOT = _cg_saved
    check("every due task ran", state["done"] == len(due_ids), f"done={state['done']}")
    check("every running wake-up task carries its row id for the sweep",
          tagged and all(tagged), f"{tagged}")
    check("the loop sweeps orphaned 'running' rows back at startup",
          any("status='running'" in q and "SET status='scheduled'" in q for q, _ in LoopPool.executed),
          f"{LoopPool.executed[:1]}")

    # ── 3. Fire time.
    op_calls.clear(); turn_calls.clear(); reports.clear()
    run(otr._run_one(None, 7, row(3, {"type_id": GONE, "op": {"tool": "list_projects", "args": {}}})))
    run(otr._run_one(None, 7, row(4, {"type_id": GONE, "prompt": "note"})))
    check("a task whose type is no longer active runs NOTHING, in either lane",
          op_calls == [] and turn_calls == [] and len(reports) == 2
          and all(r["ok"] is False and "Nothing ran" in (r["error"] or "") for r in reports), f"{reports}")
    run(otr._run_one(None, 7, row(5, {"type_id": FIN, "op": {"tool": "list_projects", "args": {}}})))
    check("an active type rides into the op (the gate's wall applies)",
          op_calls and (op_calls[-1].get("turn_type") or {}).get("id") == FIN, f"{op_calls}")
    run(otr._run_one(None, 7, row(6, {"type_id": FIN, "prompt": "note"})))
    check("an active type is pinned on the fired turn",
          turn_calls and turn_calls[-1].get("turn_type_id") == FIN, f"{turn_calls}")

    # ── 4. Scheduling from a typed turn.
    written = []

    append_ok = {"v": True}

    async def fake_append(req):
        order.append("insert")
        if not append_ok["v"]:
            return {"ok": False, "error": "calendar write refused"}
        written.append(req)
        return {"ok": True, "event_id": req.get("event_id") or 900 + len(written), "event_at": req["event_at"]}
    import server.bridge.memory_handlers as mh
    mh.handle_calendar_append = fake_append
    # A recurring prompt task waits for the person's card
    # (runtime/scheduled_approval, gated in scripts/test_scheduled_spend_pg.py);
    # what this file checks is the type the task fires under, so the card is
    # skipped here.
    import mastercode.runtime.scheduled_approval as _sa
    _sa.needs_approval = lambda **k: False
    bs.get_pool = lambda: object()
    bs._entity_id = 7

    def sched(args, turn_type=None, thread_id=None):
        base = {"op": "create", "title": "t", "fire_at": "2026-09-27T09:00:00Z"}
        base.update(args)
        return run(tools_mod._exec_schedule_task(base, turn_type=turn_type, thread_id=thread_id))

    fin = _type(FIN, "Finance")
    written.clear()
    r = sched({"prompt": "look again"})
    check("an untyped turn schedules with no type", r.get("status") == "ok" and "type_id" not in written[-1]["payload"],
          f"{r} {written[-1] if written else None}")
    r = sched({"prompt": "look again", "type_id": OPS})
    check("an untyped turn may name an active type", written[-1]["payload"].get("type_id") == OPS, f"{r}")
    r = sched({"prompt": "look again"}, turn_type=fin)
    check("a typed turn is never untyped: it defaults to its own type",
          r.get("status") == "ok" and written[-1]["payload"].get("type_id") == FIN and r["result"]["type_id"] == FIN,
          f"{r}")
    n = len(written)
    r = sched({"prompt": "restart web-1 if the check fails", "type_id": OPS}, turn_type=fin)
    check("a typed turn cannot schedule a prompt under ANOTHER type (no hidden notes)",
          r.get("status") == "error" and "cannot set work for another type" in r["result"]
          and len(written) == n, f"{r}")
    r = sched({"op_tool": "list_projects", "op_args": {}, "type_id": OPS}, turn_type=fin)
    check("...nor an op", r.get("status") == "error" and len(written) == n, f"{r}")
    r = sched({"prompt": "x", "type_id": FIN}, turn_type=fin)
    check("naming its own type is fine", r.get("status") == "ok" and written[-1]["payload"].get("type_id") == FIN, f"{r}")
    r = sched({"op_tool": "list_projects", "op_args": {}}, turn_type=fin)
    check("an op from a typed turn carries its own type", written[-1]["payload"].get("type_id") == FIN, f"{r}")
    n = len(written)
    r = sched({"prompt": "x", "type_id": GONE})
    check("an inactive type is refused", r.get("status") == "error" and "retired" in r["result"] and len(written) == n, f"{r}")
    r = sched({"prompt": "x"}, turn_type=tt.closed_type("unreadable"))
    check("a closed turn schedules nothing", r.get("status") == "error" and "holds nothing" in r["result"]
          and len(written) == n, f"{r}")

    # ── 6. calendar_log never schedules work: schedule_task is the one door
    # (the one that writes the plan into the thread); runbook channels carry
    # the turn's type.
    a2, d2 = tt.stamp_scheduled_work(fin, "calendar_log",
                                     {"event_type": "scheduled_task", "status": "scheduled", "payload": {"prompt": "x"}})
    check("calendar_log cannot write a scheduled task from a typed turn",
          d2 is not None and d2.get("reason") == "use_schedule_task", f"{a2} {d2}")
    a3, d3 = tt.stamp_scheduled_work(None, "calendar_log", {"event_type": "scheduled_task", "payload": {}})
    check("...nor from an untyped turn (it would run a plan the thread never showed)",
          d3 is not None and d3.get("reason") == "use_schedule_task", f"{d3}")
    a4, d4 = tt.stamp_scheduled_work(fin, "calendar_log", {"event_type": "meeting", "payload": {}})
    check("a calendar_log that schedules nothing is unchanged", d4 is None and a4 == {"event_type": "meeting", "payload": {}})
    a5, d5 = tt.stamp_scheduled_work(tt.closed_type("x"), "calendar_log", {"event_type": "scheduled_task"})
    check("a closed turn creates no scheduled work", d5 is not None and d5.get("status") == "denied", f"{d5}")
    a6, d6 = tt.stamp_scheduled_work(fin, "reachout_channel", {"kind": "runbook", "action": {"type": "runbook", "template_id": 3}})
    check("a runbook channel built from a typed turn carries the type",
          d6 is None and a6["action"]["type_id"] == FIN, f"{a6}")
    a7, _ = tt.stamp_scheduled_work(fin, "reachout_channel", {"kind": "email", "action": {"type": "email"}})
    check("a non-runbook channel is unchanged", "type_id" not in a7["action"])
    tsrc_ = src("brain", "mastercode", "runtime", "tools.py")
    i_auth = tsrc_.find("    if not allowed:\n        return denial\n\n    # ── Turn types step 5: scheduled work carries")
    check("the stamp runs at the one gate, right after authorization",
          i_auth > 0 and "args, _stamp_denial = _stamp_work(turn_type, name, args)" in tsrc_[i_auth:i_auth + 800])

    # Templates carry the type; the runbook transport and schedule_task honour it.
    tpl_inserts = []

    tpl_deleted = []

    class TplPool:
        async def execute(self, sql, *a):
            if "DELETE FROM entity_task_templates" in sql:
                tpl_deleted.append(a)
            return "DELETE 1"

        async def fetchrow(self, sql, *a):
            if "INSERT INTO entity_task_templates" in sql:
                tpl_inserts.append(a)
                return {"id": 55, "created_at": None}
            if "FROM entity_task_templates" in sql:
                return {"prompt": "p", "steps": [], "recurrence": None, "type_id": OPS, "name": "t"}
            return None
    bs.get_pool = lambda: TplPool()
    posts.clear()
    r = run(tools_mod._exec_task_template({"op": "create", "name": "t", "prompt": "p"}, turn_type=fin,
                                          thread_id="th-9"))
    check("a template written by a typed turn carries its type", r.get("status") == "ok" and tpl_inserts and tpl_inserts[-1][-1] == FIN,
          f"{r} {tpl_inserts}")
    check("a saved template is written into the thread it was saved in",
          posts and posts[-1]["origin"] == {"thread_id": "th-9"} and "Saved runbook template **t**" in posts[-1]["text"]
          and posts[-1]["text"].endswith("\np"), f"{posts}")
    post_ok["v"] = False
    r = run(tools_mod._exec_task_template({"op": "create", "name": "t2", "prompt": "p"}, turn_type=fin))
    post_ok["v"] = True
    check("a template whose entry could not be written into the thread is removed again",
          r.get("status") == "error" and tpl_deleted and tpl_deleted[-1][0] == 55, f"{r} {tpl_deleted}")
    run(tools_mod._exec_task_template({"op": "create", "name": "u", "prompt": "p"}))
    check("an untyped template carries none", tpl_inserts[-1][-1] is None)
    r = run(tools_mod._exec_task_template({"op": "create", "name": "c", "prompt": "p"}, turn_type=tt.closed_type("x")))
    check("a closed turn writes no template", r.get("status") == "error")
    written.clear()
    r = run(tools_mod._exec_schedule_task({"op": "create", "title": "t", "fire_at": "2026-09-27T09:00:00Z",
                                           "template_id": 55}))
    check("an untyped turn stamping a typed template gets the template's type",
          r.get("status") == "ok" and written and written[-1]["payload"].get("type_id") == OPS, f"{r}")
    n = len(written)
    r = run(tools_mod._exec_schedule_task({"op": "create", "title": "t", "fire_at": "2026-09-27T09:00:00Z",
                                           "template_id": 55}, turn_type=fin))
    check("a typed turn cannot run a template saved under another type",
          r.get("status") == "error" and "another turn type" in r["result"] and len(written) == n, f"{r}")
    r = run(tools_mod._exec_schedule_task({"op": "create", "title": "t", "fire_at": "2026-09-27T09:00:00Z",
                                           "template_id": 55, "type_id": FIN}))
    check("an untyped turn cannot name one type for a template saved under another",
          r.get("status") == "error" and "than the type_id given" in r["result"] and len(written) == n, f"{r}")
    import server.reachout.transports as tr
    runbook_writes = []

    async def cap_append(req):
        runbook_writes.append(req)
        return {"ok": True, "event_id": 77}
    mh.handle_calendar_append = cap_append
    run(tr._send_runbook({"action": {"type": "runbook", "template_id": 55, "type_id": FIN},
                          "pool": TplPool(), "entity_id": 7, "render": lambda x: x}))
    check("a runbook fires under the channel's type", runbook_writes and runbook_writes[-1]["payload"].get("type_id") == FIN,
          f"{runbook_writes}")
    run(tr._send_runbook({"action": {"type": "runbook", "template_id": 55},
                          "pool": TplPool(), "entity_id": 7, "render": lambda x: x}))
    check("...else under the template's type", runbook_writes[-1]["payload"].get("type_id") == OPS, f"{runbook_writes[-1]}")
    mh.handle_calendar_append = fake_append

    # M9 / M16: the resolution itself.
    async def by_id_fin(pool, eid, tid):
        return dict(TYPES[FIN])

    async def bound_ops(pool, eid, thread_id=None, project_id=None):
        return dict(TYPES[OPS])

    async def boom(*a, **k):
        raise RuntimeError("db down")
    tt_mod = sys.modules["mastercode.runtime.turn_types"]
    save = (tt_mod.load_turn_type_by_id, tt_mod.load_turn_type)
    tt_mod.load_turn_type_by_id, tt_mod.load_turn_type = by_id_fin, bound_ops
    t = run(tt_mod.resolve_session_type(None, 7, pinned_type_id=FIN, thread_id="th", project_id=None))
    check("the PINNED type wins over the thread's binding", t and t["id"] == FIN, f"{t}")
    t = run(tt_mod.resolve_session_type(None, 7, pinned_type_id=None, thread_id="th", project_id=None))
    check("without a pin, the thread's binding decides", t and t["id"] == OPS, f"{t}")
    tt_mod.load_turn_type_by_id = boom
    t = run(tt_mod.resolve_session_type(None, 7, pinned_type_id=FIN, thread_id=None, project_id=None))
    check("a pinned resolution that fails runs CLOSED", t and t.get("status") == "closed", f"{t}")
    tt_mod.load_turn_type = boom
    t = run(tt_mod.resolve_session_type(None, 7, pinned_type_id=None, thread_id=None, project_id=None))
    check("no pin, no binding: a failure is untyped (None)", t is None, f"{t}")
    tt_mod.load_turn_type_by_id, tt_mod.load_turn_type = save
    check("the session resolves through resolve_session_type",
          "sess.turn_type = await resolve_session_type(" in ssrc_all)
    check("N2: a pinned (background) turn never uses up a Once grant",
          'and not _pinned):\n                from mastercode.runtime.turn_type_approval import consume_once' in ssrc_all)

    # M14: an unhandled error is recorded and finalized.
    fin_calls = []

    async def rec(*a, **k):
        fin_calls.append(("record", a))

    async def finz(*a, **k):
        fin_calls.append(("finalize", a))
    save2 = (otr._record_run, otr._finalize, otr._run_one)
    otr._record_run, otr._finalize = rec, finz

    async def raising(*a, **k):
        raise ValueError("boom")
    otr._run_one = raising
    run(otr._guarded_run(None, 7, row(20, {})))
    check("an unhandled run error is recorded AND finalized", [c[0] for c in fin_calls] == ["record", "finalize"], f"{fin_calls}")

    # a cancelled wake-up is put back.
    requeued = []

    async def rq(pool, eid, event_id):
        requeued.append(event_id)
    save3 = otr._requeue
    otr._requeue = rq

    async def sleeper(*a, **k):
        await asyncio.sleep(10)
    otr._run_one = sleeper

    async def cancel_one():
        t_ = asyncio.create_task(otr._guarded_run(None, 7, row(21, {})))
        await asyncio.sleep(0.01)
        t_.cancel()
        await asyncio.gather(t_, return_exceptions=True)
    run(cancel_one())
    check("N5: a wake-up cancelled at shutdown is put back to 'scheduled'", requeued == [21], f"{requeued}")
    otr._record_run, otr._finalize, otr._run_one = save2

    # a full process requeues instead of failing.
    requeued.clear(); reports.clear()

    async def full_send(**kw):
        yield '3:' + json.dumps(f"{otr.TURN_CAP_MARK}: 510 MB of 512 MB in use") + '\n'
    bs.send_message = full_send
    run(otr._run_one(None, 7, row(22, {"prompt": "x"})))
    check("N4: a wake-up that meets a full process is requeued, not failed", requeued == [22] and reports == [],
          f"{requeued} {reports}")
    bs.send_message = fake_send_message
    otr._requeue = save3
    check("N4: the loop claims only what the Host's memory holds", "free_turn_slots(len(_RUNNING))" in rsrc)

    # a retired type stops a recurring task.
    reports.clear()
    finalized_with = []

    async def fake_report2(pool, eid, event_id, title, recurrence, start_time, ok, content, error, **kw):
        finalized_with.append(recurrence)
    otr._report_and_finalize = fake_report2
    run(otr._run_one(None, 7, {"id": 23, "title": "t", "recurrence": "hourly", "start_time": None,
                               "payload": {"type_id": GONE, "prompt": "x"}}))
    check("N3: a recurring task under a retired type is stopped, not re-armed", finalized_with == [None], f"{finalized_with}")
    otr._report_and_finalize = fake_report

    # 4b. The thread is the chain of thought.
    bs.get_pool = lambda: object()
    written.clear(); posts.clear()
    r = sched({"prompt": "check the deploy and tell me", "recurrence": "daily"}, turn_type=fin, thread_id="th-1")
    check("the plan is written into the thread it was set in, in the words it was given",
          r.get("status") == "ok" and r["result"].get("plan_in_thread") is True and posts
          and posts[-1]["origin"] == {"thread_id": "th-1"}
          and "Scheduled **t**" in posts[-1]["text"] and "repeating daily" in posts[-1]["text"]
          and posts[-1]["text"].endswith("check the deploy and tell me"), f"{r} {posts}")
    check("the task records its thread and that its plan is there",
          written[-1]["payload"].get("origin") == {"thread_id": "th-1"}
          and written[-1]["payload"].get("plan_in_thread") is True, f"{written[-1]}")
    sched({"steps": ["look", "fix"]}, thread_id="th-1")
    check("steps are written as a numbered plan", posts[-1]["text"].endswith("1. look\n2. fix"), f"{posts[-1]}")
    sched({"op_tool": "list_projects", "op_args": {"a": 1}}, thread_id="th-1")
    check("an op's plan names the op and its exact args",
          "it will run `list_projects` with {\"a\": 1}" in posts[-1]["text"], f"{posts[-1]}")
    # The plan is written BEFORE the row exists, so nothing can fire unseen.
    order.clear()
    r = sched({"prompt": "x"}, thread_id="th-1")
    check("the plan is written into the thread before the task row is inserted",
          order == ["post", "insert"], f"{order}")
    check("the plan entry and the row name the same event id",
          posts[-1]["extra_tags"]["scheduled_event_id"] == written[-1]["event_id"] == r["result"]["event_id"],
          f"{posts[-1]} {written[-1]}")
    check("a plan written directly needs no fire-time lookup (no plan_event_id)",
          "plan_event_id" not in written[-1]["payload"], f"{written[-1]}")
    import mastercode.pii_queue as pq
    _save_en = pq.enabled
    pq.enabled = lambda: True
    r = sched({"prompt": "x"}, thread_id="th-1")
    pq.enabled = _save_en
    check("a plan write that is only queued (PII mode) makes the runner look for it at fire time",
          written[-1]["payload"].get("plan_event_id") == written[-1]["event_id"], f"{written[-1]}")
    n = len(written); order.clear()
    post_ok["v"] = False
    r = sched({"prompt": "x"}, thread_id="th-1")
    post_ok["v"] = True
    check("a task whose plan could not be written into the thread is never inserted",
          r.get("status") == "error" and "was not set" in r["result"] and len(written) == n
          and order == ["post"], f"{r} {order}")
    append_ok["v"] = False
    r = sched({"prompt": "x"}, thread_id="th-1")
    append_ok["v"] = True
    check("a plan whose row then fails to insert is followed by a visible 'not set' line",
          r.get("status") == "error" and posts[-1]["text"].startswith("(Not set after all"), f"{r} {posts[-1]}")
    bs.get_pool = lambda: object()
    # The fired turn: pointer, then its plan, in its thread.
    turn_calls.clear()
    run(otr._run_one(None, 7, row(24, {"type_id": FIN, "prompt": "check the deploy",
                                       "plan_in_thread": True,
                                       "origin": {"thread_id": "th-1"}})))
    msg = turn_calls[-1]["message"] if turn_calls else ""
    check("the fired turn opens on the platform's pointer to its thread, then its plan",
          msg.startswith("(Your scheduled wake-up **t** is due.") and msg.endswith("\nThe plan: check the deploy"), msg)
    check("the fired turn thinks in the thread it was set in", turn_calls[-1].get("thread_id") == "th-1",
          f"{turn_calls[-1].get('thread_id')}")
    check("...on the hidden write lane (its own rows are not the thread's messages)",
          turn_calls[-1].get("app_id") == "scheduled_task")
    run(otr._run_one(None, 7, row(25, {"prompt": "sync the list"})))
    msg = turn_calls[-1]["message"]
    check("a task whose text was not written into a thread is not called the Host's own plan",
          msg.startswith("(Scheduled wake-up **t** is due. What it asks follows.") and "Your scheduled" not in msg, msg)
    turn_calls.clear()
    run(otr._run_one(None, 7, row(26, {"steps": ["look", "fix"], "plan_in_thread": True})))
    check("every step of a steps task opens on the pointer",
          len(turn_calls) == 2 and all(c["message"].startswith("(Your scheduled wake-up") for c in turn_calls)
          and turn_calls[1]["message"].endswith("Step 2 of 2 in the plan: fix"), f"{[c['message'] for c in turn_calls]}")
    # Fire time: a queued plan must have landed before the task runs.
    import mastercode.pii_queue as pq

    class PlanPool:
        def __init__(self, found, boom=False):
            self.found, self.boom, self.sql = found, boom, []

        async def fetchval(self, sql, *a):
            assert "metadata->>'scheduled_event_id'" in sql, sql
            if self.boom:
                raise RuntimeError("db down")
            return 1 if self.found else None

        async def execute(self, sql, *a):
            self.sql.append((sql, a))
            return "UPDATE 1"
    waits, confirmed = [], []

    async def fake_wait(pool, eid, event_id, n):
        waits.append((event_id, n))

    async def fake_confirm(pool, eid, event_id):
        confirmed.append(event_id)
    save_wc = (otr._wait_for_plan, otr._plan_confirmed)
    otr._wait_for_plan, otr._plan_confirmed = fake_wait, fake_confirm
    save_pq = (pq.enabled, pq.stats)

    def fire(eid_, pool, **extra):
        rw = {"id": eid_, "title": "t", "recurrence": "hourly", "start_time": None,
              "payload": {"prompt": "go", "plan_in_thread": True, "plan_event_id": f"e-{eid_}", **extra}}
        run(otr._run_one(pool, 7, rw))
    turn_calls.clear(); reports.clear()
    fire(27, PlanPool(True))
    check("a task whose queued plan has landed runs, and is never looked up again",
          len(turn_calls) == 1 and confirmed == [27], f"{turn_calls} {confirmed}")
    turn_calls.clear()
    pq.enabled = lambda: False
    fire(28, PlanPool(False))
    check("a task whose plan never reached the thread runs NOTHING, says so, and stops (not re-armed)",
          turn_calls == [] and reports and reports[-1]["ok"] is False
          and "never reached the thread" in reports[-1]["error"] and reports[-1]["recurrence"] is None,
          f"{reports}")
    pq.enabled, pq.stats = (lambda: True), (lambda: {"pending": 3})
    reports.clear()
    fire(29, PlanPool(False))
    check("while the PII queue may still land it, the task waits (counted), not failed",
          waits == [(29, 1)] and reports == [] and turn_calls == [], f"{waits} {reports}")
    fire(30, PlanPool(False), plan_waits=otr.PLAN_WAIT_MAX)
    check("...but only PLAN_WAIT_MAX times: then nothing runs and the thread says so",
          len(waits) == 1 and reports and "never reached the thread" in reports[-1]["error"], f"{waits} {reports}")
    reports.clear()
    fire(31, PlanPool(False, boom=True))
    check("a failed lookup is not 'missing': the task waits",
          waits[-1] == (31, 1) and reports == [], f"{waits} {reports}")
    fire(32, PlanPool(False, boom=True), plan_waits=otr.PLAN_WAIT_MAX)
    check("...and a lookup that keeps failing ends in a refusal that says it could not be checked",
          reports and "could not be checked" in reports[-1]["error"] and turn_calls == [], f"{reports}")
    otr._wait_for_plan, otr._plan_confirmed = save_wc
    pq.enabled, pq.stats = save_pq
    wp = PlanPool(False)
    run(otr._wait_for_plan(wp, 7, 33, 4))
    check("a wait moves the task off the head of the due queue and counts it (no starvation)",
          wp.sql and "start_time = NOW() + make_interval(secs => $3)" in wp.sql[-1][0]
          and "'{plan_waits}'" in wp.sql[-1][0] and wp.sql[-1][1] == (33, 7, float(otr.PLAN_WAIT_S), 4)
          and "status='running'" in wp.sql[-1][0], f"{wp.sql}")
    run(otr._plan_confirmed(wp, 7, 33))
    check("a confirmed plan's lookup id is dropped from the payload",
          "payload - 'plan_event_id'" in wp.sql[-1][0], f"{wp.sql[-1]}")
    check("the scheduler's ids survive the PII metadata scrub (the runner looks the plan up by them)",
          pq.scrub_metadata({"scheduled_event_id": "e", "scheduled_task_id": 1, "task_template_id": 2,
                             "scheduled_plan": True, "agent_title": "prose"})
          == {"scheduled_event_id": "e", "scheduled_task_id": 1, "task_template_id": 2, "scheduled_plan": True})
    # The real poster's contract.
    import mastercode.memory_engine as me
    save_w = me.write_turn_row
    for landed, pii_on, want, label in ((7, False, True, "a landed row"),
                                        (None, False, False, "a row that did not land"),
                                        (None, True, True, "a row queued in PII mode")):
        async def _w(*a, _r=landed, **k):
            return _r
        me.write_turn_row = _w
        pq.enabled = (lambda v=pii_on: v)
        got = run(real_post(None, 7, {"thread_id": "th-1"}, "text"))
        check(f"post_to_origin_thread reports {label} as {want}", got is want, f"{got}")
    me.write_turn_row = save_w
    pq.enabled, pq.stats = save_pq
    check("no fired turn carries a cross-type header any more", "A note left by" not in rsrc and "from_type" not in rsrc)
    tsrc0 = src("brain", "mastercode", "runtime", "tools.py")
    check("schedule_task writes no cross-type provenance", "from_type" not in tsrc0)
    from mastercode.circles_threads import NOT_REPLAY_ROW
    check("a thread's transcript hides the fired turn's own rows (the report is the message)",
          "NOT IN ('circles-replay', 'circles-ab', 'scheduled_task')" in NOT_REPLAY_ROW)
    check("operate hands the calling thread through to schedule_task",
          'thread_id=getattr(sess, "thread_id", None),\n                            )' in ssrc_all
          and "thread_id=thread_id,\n        )" in src("brain", "mastercode", "runtime", "direct_ops.py"))
    check("the dispatcher passes the thread into schedule_task and task_template",
          "turn_type=turn_type,\n            # ...and the plan is written into the thread it was set in.\n            thread_id=thread_id)" in tsrc0
          and "interaction_context=(origin_context or interaction_context),\n            thread_id=thread_id)" in tsrc0)

    # ── 7. A full process.
    # A steps task that meets a full process AFTER a step ran is not
    # requeued (that would run the step again) — it fails once.
    calls = {"n": 0}

    async def second_full(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            yield '0:"step one done"\n'
        else:
            yield '3:' + json.dumps(f"{otr.TURN_CAP_MARK}: 510 MB of 512 MB in use") + '\n'
    bs.send_message = second_full
    requeued2 = []

    async def rq2(pool, eid, event_id):
        requeued2.append(event_id)
    save4 = otr._requeue
    otr._requeue = rq2
    reports.clear()
    run(otr._run_one(None, 7, row(30, {"steps": ["one", "two"]})))
    check("a full process after step 1 fails the fire, never requeues it (no step runs twice)",
          requeued2 == [] and reports and reports[-1]["ok"] is False, f"{requeued2} {reports}")
    check("the failure report carries what the finished steps said",
          "What ran before it stopped" in (reports[-1]["error"] or "")
          and "**Step 1/2: one**\nstep one done" in (reports[-1]["error"] or ""), f"{reports[-1]}")
    calls["n"] = 1   # the next call is "full" immediately
    reports.clear()
    run(otr._run_one(None, 7, row(31, {"steps": ["one", "two"]})))
    check("a full process before ANY step requeues", requeued2 == [31] and reports == [], f"{requeued2} {reports}")
    otr._requeue = save4
    bs.send_message = fake_send_message
    check("N4: the slot count is what the Host's memory holds, the loop's own claims counted",
          "slots = free_turn_slots(len(_RUNNING))" in rsrc)
    check("N5: orphans are swept periodically, not only at startup",
          "if time.monotonic() - _last_sweep >= ORPHAN_AFTER_S:\n                await _sweep_orphans(pool, entity_id)" in rsrc)

    # A calendar_log carrying its own type_id is refused all the same.
    _a8, d8 = tt.stamp_scheduled_work(fin, "calendar_log",
                                      {"event_type": "scheduled_task",
                                       "payload": {"type_id": FIN, "prompt": "x"}})
    check("a calendar_log naming the turn's own type still schedules nothing", d8 is not None, f"{d8}")

    # End to end: a real calendar_log through the gate reaches the bridge stamped.
    import types as _types
    bridged = []

    class FakeBC:
        _connected = True

        async def request_full(self, op, args):
            bridged.append((op, args))
            return {"ok": True, "data": {"event_id": 1}}
    fake_main = _types.ModuleType("server.main")
    fake_main.get_bridge_client = lambda: FakeBC()
    sys.modules["server.main"] = fake_main
    from mastercode.runtime import meta_tool_caps as caps

    async def _all_scopes(pool, entity_id, **kw):
        return set(caps.ENTITY_SCOPE_TO_BITS.keys())
    caps._resolve_entity_scopes = _all_scopes


    class GatePool:
        async def execute(self, *a, **k): return None
        async def fetch(self, *a, **k): return []
        async def fetchval(self, *a, **k): return None
        async def fetchrow(self, *a, **k): return None
    comms_fin = dict(fin, grant_names=["read_context", "read_memory", "write_context", "write_memory", "execute_tool"])
    r = run(tools_mod._execute_meta_tool(
        "calendar_log", {"event_type": "scheduled_task", "summary": "x", "status": "scheduled",
                         "event_at": "2026-09-27T09:00:00Z", "payload": {"prompt": "restart everything"}},
        entity_id=7, pool=GatePool(), turn_type=comms_fin))
    check("end to end: calendar_log scheduling from a typed turn never reaches the bridge",
          r.get("status") == "denied" and r.get("reason") == "use_schedule_task" and bridged == [], f"{r} {bridged}")
    r = run(tools_mod._execute_meta_tool(
        "calendar_log", {"event_type": "scheduled_task", "summary": "x", "status": "scheduled",
                         "event_at": "2026-09-27T09:00:00Z", "payload": {"prompt": "restart everything"}},
        entity_id=7, pool=GatePool()))
    check("end to end: ...nor from an untyped turn",
          r.get("status") == "denied" and bridged == [], f"{r} {bridged}")
    # M6: schedule_task through the gate carries the turn's type.
    bs.get_pool = lambda: object()
    written.clear()
    r = run(tools_mod._execute_meta_tool(
        "schedule_task", {"op": "create", "title": "t", "fire_at": "2026-09-27T09:00:00Z", "prompt": "x"},
        entity_id=7, pool=GatePool(), turn_type=comms_fin))
    check("M6: schedule_task through the gate stamps the calling turn's type",
          written and written[-1]["payload"].get("type_id") == FIN, f"{r}")

    # Reach-out: a typed turn sets off only a runbook built under its type.
    class ChPool:
        def __init__(self, kind, action):
            self.kind, self.action = kind, action

        async def fetchrow(self, sql, *a):
            if "FROM reachout_channels" in sql:
                return {"id": 7, "name": "nightly", "created_by": "host",
                        "kind": self.kind, "action": self.action}
            return None

        async def fetchval(self, sql, *a):
            return None   # its template carries no type
    import server.reachout.service as ro_svc
    fired = []

    async def fake_test_fire(pool, eid, ch):
        fired.append(ch)
        return {"ok": True}
    ro_svc.test_fire = fake_test_fire
    bs.get_pool = lambda: ChPool("runbook", {"type": "runbook", "template_id": 1})
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "nightly"}, turn_type=fin))
    check("a typed turn cannot set off an UNTYPED runbook", r.get("status") == "denied" and fired == [], f"{r}")
    bs.get_pool = lambda: ChPool("runbook", {"type": "runbook", "template_id": 1, "type_id": OPS})
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "nightly"}, turn_type=fin))
    check("...nor another type's runbook", r.get("status") == "denied" and fired == [], f"{r}")
    bs.get_pool = lambda: ChPool("runbook", {"type": "runbook", "template_id": 1, "type_id": FIN})
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "nightly"}, turn_type=fin))
    check("...but can set off a runbook built under its own type", fired == ["nightly"], f"{r} {fired}")
    fired.clear()
    import json as _json
    bs.get_pool = lambda: ChPool("webhook", _json.dumps({"type": "runbook", "template_id": 1, "type_id": OPS}))
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "nightly"}, turn_type=fin))
    check("the guard keys on action.type (what the transport dispatches on), and reads a JSON-string action",
          r.get("status") == "denied" and fired == [], f"{r}")
    # The dispatcher strips the type before it looks the transport up
    # (reachout/service.py), so a spaced " runbook" still runs a runbook and
    # the type guard reads it the same way.
    bs.get_pool = lambda: ChPool("runbook", {"type": " runbook ", "template_id": 1, "type_id": OPS})
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "nightly"}, turn_type=fin))
    check("a spaced runbook type is still another type's runbook to a typed turn",
          r.get("status") == "denied" and fired == [], f"{r}")
    added = []

    async def fake_add_trigger(pool, eid, **kw):
        added.append(kw)
        return {"id": 1, "created_at": None}
    ro_svc.add_trigger = fake_add_trigger
    bs.get_pool = lambda: ChPool("runbook", {"type": "runbook", "template_id": 1, "type_id": OPS})
    r = run(tools_mod._exec_reachout({"op": "add_trigger", "channel": "nightly", "label": "x",
                                      "match_kind": "event"}, turn_type=fin))
    check("a typed turn cannot subscribe another type's runbook (add_trigger)",
          r.get("status") == "denied" and added == [], f"{r}")

    class TplTypePool(ChPool):
        async def fetchval(self, sql, *a):
            return FIN if "FROM entity_task_templates" in sql else None
    proposed_triggers = []

    async def _fake_propose_trigger(pool, eid, **kw):
        proposed_triggers.append(kw)
        return {"status": "ok", "result": {"awaiting_approval": True, "ticket_id": 1}}
    import mastercode.runtime.scheduled_approval as _sa_rb
    _sa_rb.propose_trigger = _fake_propose_trigger
    bs.get_pool = lambda: TplTypePool("runbook", {"type": "runbook", "template_id": 1})
    r = run(tools_mod._exec_reachout({"op": "add_trigger", "channel": "nightly", "label": "x",
                                      "match_kind": "event"}, turn_type=fin))
    # Past the type guard, a trigger on a runbook is the person's to add
    # (runtime/scheduled_approval): what "may" means now is that the card goes
    # up rather than a denial.
    check("...but a runbook whose template is of its own type fires under it, so it may",
          r.get("status") == "ok" and (r.get("result") or {}).get("awaiting_approval")
          and proposed_triggers, f"{r}")
    # The runbook check reads the channel the way the dispatcher and
    # add_trigger do (review of 7bb1b4557): the dispatcher strips action.type,
    # and add_trigger resolves " 7" by NAME, so neither may slip past the card.
    proposed_triggers.clear()
    added.clear()
    bs.get_pool = lambda: ChPool("webhook", _json.dumps({"type": " runbook", "template_id": 1}))
    r = run(tools_mod._exec_reachout({"op": "add_trigger", "channel": "nightly", "label": "x",
                                      "match_kind": "event"}))
    check("RB1: a runbook whose stored type carries spaces still waits for the card",
          (r.get("result") or {}).get("awaiting_approval") and proposed_triggers and not added,
          f"{r} {added}")

    class SplitPool:
        """Channel id 7 is a log; the channel NAMED "7" (id 9) is a runbook."""
        async def fetchrow(self, sql, *a):
            if "FROM reachout_channels" not in sql:
                return None
            if "WHERE name=" in sql:
                return ({"id": 9, "name": "7", "created_by": "host",
                         "action": {"type": "runbook", "template_id": 1}} if a[0] == "7" else None)
            if a[0] == 7:
                return {"id": 7, "name": "invented-log", "created_by": "host", "action": {"type": "log"}}
            if a[0] == 9:
                return {"id": 9, "name": "7", "created_by": "host",
                        "action": {"type": "runbook", "template_id": 1}}
            return None

        async def fetchval(self, sql, *a):
            return None
    proposed_triggers.clear()
    added.clear()
    bs.get_pool = lambda: SplitPool()
    r = run(tools_mod._exec_reachout({"op": "add_trigger", "channel": " 7", "label": "x",
                                      "match_kind": "event"}))
    check("RB2: the channel checked is the one add_trigger resolves (' 7' is the name '7')",
          (r.get("result") or {}).get("awaiting_approval") and not added
          and proposed_triggers and proposed_triggers[0].get("channel_id") == 9,
          f"{r} {added} {proposed_triggers}")
    proposed_triggers.clear()
    bs.get_pool = lambda: ChPool("runbook", {"type": "runbook", "template_id": 1})
    r = run(tools_mod._exec_reachout({"op": "add_trigger", "channel": "nightly", "label": " ",
                                      "match_kind": "event"}))
    check("RB3: a card that its click could only refuse is never filed",
          r.get("status") != "ok" and not proposed_triggers and not added, f"{r}")
    check("the dispatcher hands the turn's type to reachout",
          "return await _exec_reachout(args, turn_type=turn_type)" in tsrc0)
    fired.clear()
    bs.get_pool = lambda: ChPool("email", {"type": "email"})
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "mail"}, turn_type=fin))
    check("a non-runbook channel is unaffected", fired == ["mail"], f"{r}")
    fired.clear()
    bs.get_pool = lambda: ChPool("runbook", {"type": "runbook", "template_id": 1})
    r = run(tools_mod._exec_reachout({"op": "test", "channel": "nightly"}))
    check("an untyped turn sets off any runbook, as before", fired == ["nightly"], f"{r}")

    check("M16: the session's own except path still closes when a pin exists",
          'if _tt_thread_id or _tt_project_id is not None or getattr(sess, "pinned_turn_type_id", None):' in ssrc_all)

    # The sweep never takes a live row.
    swept = []

    class SweepPool:
        async def execute(self, sql, *a):
            swept.append((sql, a))
            return "UPDATE 0"

    async def hold():
        t_ = asyncio.create_task(asyncio.sleep(1))
        t_._event_id = 4242
        otr._RUNNING.add(t_)
        await otr._sweep_orphans(SweepPool(), 7)
        t_.cancel()
        otr._RUNNING.discard(t_)
    run(hold())
    check("the orphan sweep excludes every row this process is running",
          swept and "NOT (id = ANY($3::bigint[]))" in swept[-1][0] and swept[-1][1][2] == [4242], f"{swept}")
    from server.turn_registry import TURN_IDLE_S, TURN_TIMEOUT_S
    # A turn is stopped for showing no sign of life
    # (TURN_IDLE_S), and has a total only when a Host opts in (TURN_TIMEOUT_S,
    # infinite by default) — the threshold is whichever is sooner, + 5 min.
    check("the orphan threshold is derived from the turn's idle window (or its opt-in total)",
          otr.ORPHAN_AFTER_S == min(float(TURN_IDLE_S), float(TURN_TIMEOUT_S)) + 300.0,
          f"{otr.ORPHAN_AFTER_S}")
    touched = []

    async def fake_touch(pool, eid, event_id):
        touched.append(event_id)
    save5 = otr._touch
    otr._touch = fake_touch
    run(otr._run_one(None, 7, row(40, {"steps": ["a", "b", "c"]})))
    otr._touch = save5
    check("a steps task marks itself alive after every step", touched == [40, 40, 40], f"{touched}")

    import server.turn_registry as treg
    save_to = treg.TURN_TIMEOUT_S
    treg.TURN_TIMEOUT_S = 999
    check("the orphan threshold follows TURN_TIMEOUT_S", otr._orphan_after_s() == 1299.0,
          f"{otr._orphan_after_s()}")
    treg.TURN_TIMEOUT_S = save_to

    # A runbook channel cannot be built on another type's template.
    built = []

    async def fake_add_channel(pool, eid, **kw):
        built.append(kw)
        return {"id": 1, "created_at": None}
    ro_svc.add_channel = fake_add_channel

    class ChTplPool:
        async def fetchval(self, sql, *a):
            return OPS if "FROM entity_task_templates" in sql else None
    bs.get_pool = lambda: ChTplPool()
    r = run(tools_mod._exec_reachout_channel(
        {"name": "n", "kind": "runbook", "action": {"type": "runbook", "template_id": 3}}, turn_type=fin))
    check("R1: a typed turn cannot build a runbook channel on another type's template",
          r.get("status") == "denied" and built == [], f"{r}")
    r = run(tools_mod._exec_reachout_channel(
        {"name": "n", "kind": "runbook", "action": '{"type": "runbook", "template_id": 3}'}, turn_type=fin))
    check("R1: ...a JSON-string action included", r.get("status") == "denied" and built == [], f"{r}")
    r = run(tools_mod._exec_reachout_channel(
        {"name": "n", "kind": "runbook", "action": {"type": "runbook", "template_id": 3}},
        turn_type=_type(OPS, "Ops")))
    check("R1: ...but a turn of the template's own type can", r.get("status") == "ok" and len(built) == 1, f"{r}")
    r = run(tools_mod._exec_reachout_channel(
        {"name": "n", "kind": "runbook", "action": {"type": "runbook", "template_id": 3}}))
    check("R1: an untyped turn builds as before (the task fires under the template's type)",
          r.get("status") == "ok" and len(built) == 2, f"{r}")
    check("R1: the dispatcher hands the turn's type to reachout_channel",
          "return await _exec_reachout_channel(args, turn_type=turn_type)" in tsrc0)

    # A wake-up's hidden reply row is not counted twice in its thread.
    _where = me._turn_row_where([7], session_id="", source_context={"kind": "thread", "ref": "th-1"},
                                live_only=False)
    check("a thread's scope leaves out a wake-up's hidden reply row (the report carries it)",
          me.NOT_HIDDEN_LANE_ROW in _where and "'scheduled_task'" in me.NOT_HIDDEN_LANE_ROW, f"{_where}")
    from mastercode.runtime.session import wakeup_delivery_room as wdr
    check("a wake-up's agent result set in a project delivers to that project",
          wdr("scheduled_task", None, "project:12", None) == ("workbench", 12))
    check("...set in a Circles thread, to Circles (the thread descriptor names which)",
          wdr("scheduled_task", None, "circles", "th-1") == ("circles", None)
          and wdr("scheduled_task", None, "project:12", "th-1") == ("circles", None))
    check("...set in the original conversation, to Circles", wdr("scheduled_task", None, "", None) == ("circles", None))
    check("...any other app is untouched", wdr("workbench", 4, "project:4", None) == ("workbench", 4))
    check("deliver_agent_result maps through it",
          'app_id, project_id = wakeup_delivery_room(\n            app_id, project_id, interaction_context, origin.get("thread_id"))' in ssrc_all)
    import mastercode.circles_threads as ct
    _cw = " AND ".join(ct.conversation_where([7], "th-1"))
    _sw = " AND ".join(ct.shown_where([7], "th-1"))
    check("Clear's row set includes a wake-up's hidden rows in the thread",
          "<> 'scheduled_task'" not in _cw, _cw)
    check("...while the transcript, counts and Split's move set leave them out",
          "'scheduled_task')" in _sw, _sw)

    # ── 5. Wiring.
    real_by_id = None
    ssrc = turn_source()
    check("the session passes the pin into the resolution",
          "pinned_type_id=_pinned," in ssrc)
    check("the session records the pin on every path", "sess.pinned_turn_type_id = turn_type_id" in ssrc)
    bsrc = src("server", "brain_service.py")
    check("brain_service forwards the pinned type to the runtime", "turn_type_id=turn_type_id," in bsrc)
    check("the loop cancels its wake-ups on shutdown", "for t in list(_RUNNING):\n                t.cancel()" in rsrc)
    import importlib
    import mastercode.runtime.turn_types as tt2
    importlib.reload(tt2)

    async def gone_row(sql, *a):
        return {**_type(GONE, "Gone", "retired")}

    class P:
        fetchrow = staticmethod(gone_row)
    t = run(tt2.load_turn_type_by_id(P(), 7, GONE))
    check("load_turn_type_by_id: a retired type is CLOSED, never untyped",
          t.get("status") == "closed" and "retired" in t.get("closed_reason", ""), f"{t}")

    async def none_row(sql, *a):
        return None

    class P2:
        fetchrow = staticmethod(none_row)
    t = run(tt2.load_turn_type_by_id(P2(), 7, GONE))
    check("load_turn_type_by_id: a missing type is CLOSED", t.get("status") == "closed", f"{t}")

    print()
    if FAILS:
        print(f"[turn-types-wake] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("[turn-types-wake] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
