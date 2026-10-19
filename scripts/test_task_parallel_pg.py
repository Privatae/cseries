#!/usr/bin/env python3
"""Gate: parallel task runs on a real kernel schema, the Host's turns scripted.

Each thread costs this much, the pool has this much free, so the
Host can run this many threads — in the Host's own code, never a platform number.

  M. v213 run again over the full current schema fails nothing, twice over; a
     run of 0 threads is refused.
  A. threads=3: each round wakes three thread turns at once (they overlap in
     time), each on its own lane task-<run>-t<n>; each brief names its thread
     and offers a slice of the next sources no other thread's does.
  O. a claim made in thread n's turn is owned run-<id>-t<n>, never shared;
     after each round every thread's claim is released; a person's claim in
     the project survives the run.
  E. the run finishes when its checks close and leaves no claim of any thread;
     the record's read shows the threads and the Host's memory.
  F. a Host whose memory holds two more turns runs two threads a round though
     three were asked; one whose memory holds none waits, starts nothing, and
     runs when memory frees.
  S. a thread whose turn did not land sits out the next round while the others
     go on, then runs again.
  L. the wake: a lane gives the turn its own registry key beside the project's,
     so a person's message there never waits behind a thread.
"""
import asyncio
import contextlib
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove parallel runs and v213.")
    sys.exit(2)

FAILED = 0
E = 1
MIGRATION = ROOT / "migrations" / "v213_task_run_threads.sql"
# A thread's own slice: the "- <url>" lines of the next sources, not the
# coverage statement above them (which lists every unread source).
URL_RE = re.compile(r"^- (https://p\.example/[a-z0-9-]+)", re.M)


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:500]}"))
    if not cond:
        FAILED += 1


def cgroup(path, limit_mb, used_mb):
    for name, v in (("memory.max", str(int(limit_mb * 1048576))),
                    ("memory.current", str(int(used_mb * 1048576))),
                    ("memory.stat", "anon 1\ninactive_file 0\n")):
        with open(os.path.join(path, name), "w") as f:
            f.write(v)


class ParRuntime:
    """Stands in for EntityRuntime: every woken turn is recorded with its lane
    and the moment it ran; `behave(kw)` does the turn's work against the real
    record and says whether it landed."""

    def __init__(self, behave):
        self.behave = behave
        self.calls: list = []
        self.now = 0
        self.peak = 0

    async def _wake_host_turn(self, **kw):
        # The lane the woken turn reads its history by (task_serial
        # CURRENT_WORK_LANE): a research thread reads its own turns only.
        from mastercode.runtime import task_serial as _ts
        kw = {**kw, "history_lane": _ts.CURRENT_WORK_LANE.get()}
        self.calls.append(kw)
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            return await self.behave(kw)
        finally:
            self.now -= 1


def thread_of(kw):
    lane = kw.get("lane")
    return int(lane.rsplit("-t", 1)[1]) if lane else None


async def wait_ended(pool, rid, timeout=30.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        st = await pool.fetchval("SELECT status FROM task_runs WHERE id = $1", rid)
        if st != "running":
            return st
        await asyncio.sleep(0.05)
    return "running"


def rounds_of(calls, run_id):
    """The calls grouped by round (each brief says its turn number)."""
    out: dict = {}
    for kw in calls:
        m = re.search(r"Turn (\d+) of this task", kw["brief"])
        out.setdefault(int(m.group(1)) if m else 0, []).append(kw)
    return out


async def main(dsn, cg_dir):
    r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
    r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
    check("M v213 runs again on the full schema, twice over, and fails nothing",
          r1.returncode == 0 and r2.returncode == 0, (r1.stderr[-300:], r2.stderr[-300:]))

    from mastercode.runtime import task_record as tr
    from mastercode.runtime import task_serial as ts
    from mastercode.runtime import host_memory as hm
    from server import brain_service

    ts.SKIP_PAUSE_S = 0.0
    ts.MEMORY_PAUSE_S = 0.1
    hm.CGROUP_ROOT = cg_dir           # empty: no readable limit until a test writes one
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    rt_holder = {}
    brain_service.get_runtime = lambda: rt_holder.get("rt")

    async def project(name, units=12):
        pid = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, $2) RETURNING id", E, name)
        await tr.note_found(pool, E, pid, [{"url": f"https://p.example/{name}-u{i}"} for i in range(units)],
                            "web_search")
        return pid

    def op(pid, a, thread_key="th"):
        return tr.run_task_record_op(pool, E, a, project_id=pid, thread_key=thread_key, session_id="s")

    def worker(pid, check_id, *, finish_at_turn, fail=None, seen=None):
        """A thread turn: claim the first source its brief offers, read it,
        write a finding, cover it; thread 0 closes the check at finish_at_turn."""
        async def behave(kw):
            t = thread_of(kw)
            m = re.search(r"Turn (\d+) of this task", kw["brief"])
            turn = int(m.group(1)) if m else 0
            if fail and fail(turn, t):
                await asyncio.sleep(0.05)
                return False
            if t is None:                      # the run's last turn (thread None)
                return True
            urls = URL_RE.findall(kw["brief"])
            if not urls:
                return True
            url = urls[0]
            await op(pid, {"op": "claim", "source": url})
            owner = await pool.fetchval("SELECT claimed_by FROM task_coverage WHERE project_id = $1 "
                                        "AND source_ref = $2", pid, url)
            if seen is not None:
                seen.append((turn, t, url, owner))
            await asyncio.sleep(0.1)           # long enough that a round's turns overlap
            await tr.keep_source(pool, E, pid, url, f"Page {url} says the value is {turn}-{t}.")
            await op(pid, {"op": "finding", "claim": f"value {turn}-{t}", "source": url,
                           "quote": f"Page {url} says the value is {turn}-{t}", "checks": [check_id]})
            await op(pid, {"op": "covered", "source": url, "outcome": "findings"})
            if t == 0 and turn >= finish_at_turn:
                await op(pid, {"op": "check", "id": check_id, "status": "answered"})
            return True
        return behave

    try:
        # ── M: 0 threads refused ──
        p0 = await project("zero", units=1)
        await op(p0, {"op": "check", "question": "q?"})
        r0 = await op(p0, {"op": "run", "threads": 0})
        check("M a run of 0 threads is refused", r0.get("status") != "ok" and "1 or more" in str(r0), r0)

        # ── A, O, E: three threads, memory not readable (no number held back) ──
        pa = await project("par")
        ca = (await op(pa, {"op": "check", "question": "What is the value?"}))["result"]["check"]
        await op(pa, {"op": "claim", "source": "https://p.example/par-u11"}, thread_key="person-thread")
        seen: list = []
        rt = ParRuntime(worker(pa, ca, finish_at_turn=2, seen=seen))
        rt_holder["rt"] = rt
        r = await op(pa, {"op": "run", "request": "find the value", "threads": 3})
        rid = (r.get("result") or {}).get("run")
        check("A run with threads=3 is a parallel run", r.get("status") == "ok"
              and r["result"]["mode"] == "parallel" and r["result"]["threads"] == 3, r)
        st = await wait_ended(pool, rid)
        rounds = rounds_of(rt.calls, rid)
        r1c = rounds.get(1, [])
        check("A round 1 wakes three thread turns, each on its own lane",
              sorted(kw.get("lane") for kw in r1c) == [f"task-{rid}-t0", f"task-{rid}-t1", f"task-{rid}-t2"],
              [kw.get("lane") for kw in r1c])
        check("A ...and they run at once", rt.peak >= 3, rt.peak)
        check("A each research thread reads only its own lane's turns as history, never a sibling's",
              all(kw.get("history_lane") == kw.get("lane") for kw in r1c),
              [(kw.get("lane"), kw.get("history_lane")) for kw in r1c])
        check("A each brief names its thread", all(f"thread {thread_of(kw) + 1} of 3" in kw["brief"] for kw in r1c),
              [kw["brief"][:160] for kw in r1c])
        firsts = {t: url for (turn, t, url, _o) in seen if turn == 1}
        check("A each thread is offered a slice no other thread is", len(set(firsts.values())) == 3, firsts)
        owners = {(turn, t): o for (turn, t, _u, o) in seen}
        check("O a claim in thread n's turn is owned run-<id>-t<n>",
              all(o == f"run-{rid}-t{t}" for (turn, t), o in owners.items()) and len(owners) >= 3, owners)
        check("E the run finishes when its checks close", st == "finished", st)
        left = await pool.fetch("SELECT source_ref, claimed_by FROM task_coverage WHERE project_id = $1 "
                                "AND status = 'claimed'", pa)
        check("E no claim of any thread is left; a person's claim survives",
              [(x["source_ref"], x["claimed_by"]) for x in left] == [("https://p.example/par-u11", "person-thread")],
              [dict(x) for x in left])
        rd = (await op(pa, {"op": "read"}))["result"].get("run") or {}
        check("E the read shows the threads and the Host's memory",
              rd.get("threads") == 3 and "more_turns_fit" in (rd.get("memory") or {}), rd)

        # ── F: memory decides the width ──
        cost = hm.TURN_COST_BYTES / 1048576
        cgroup(cg_dir, 1024, 1024 - 2.5 * cost)            # room for two more turns
        pf = await project("mem")
        cf = (await op(pf, {"op": "check", "question": "What is the value?"}))["result"]["check"]
        rtf = ParRuntime(worker(pf, cf, finish_at_turn=1))
        rt_holder["rt"] = rtf
        rf = (await op(pf, {"op": "run", "request": "r", "threads": 3}))["result"]["run"]
        await wait_ended(pool, rf)
        lanes1 = sorted(kw.get("lane") for kw in rounds_of(rtf.calls, rf).get(1, []))
        check("F a Host whose memory holds two more turns runs two threads a round, not the three asked",
              lanes1 == [f"task-{rf}-t0", f"task-{rf}-t1"], lanes1)
        cgroup(cg_dir, 1024, 1024 - 0.5 * cost)            # room for none
        pn = await project("full")
        cn = (await op(pn, {"op": "check", "question": "What is the value?"}))["result"]["check"]
        rtn = ParRuntime(worker(pn, cn, finish_at_turn=1))
        rt_holder["rt"] = rtn
        rn = (await op(pn, {"op": "run", "request": "r", "threads": 2}))["result"]["run"]
        await asyncio.sleep(0.5)
        held = len(rtn.calls)
        cgroup(cg_dir, 1024, 100)                          # memory frees
        stn = await wait_ended(pool, rn)
        check("F with no room the run waits and starts nothing, then runs when memory frees",
              held == 0 and stn == "finished" and len(rtn.calls) >= 2, (held, stn, len(rtn.calls)))
        for n in ("memory.max", "memory.current", "memory.stat"):
            os.remove(os.path.join(cg_dir, n))

        # ── S: a failed thread sits out one round ──
        ps = await project("sit")
        cs = (await op(ps, {"op": "check", "question": "What is the value?"}))["result"]["check"]
        rts = ParRuntime(worker(ps, cs, finish_at_turn=3, fail=lambda turn, t: turn == 1 and t == 1))
        rt_holder["rt"] = rts
        rs_ = (await op(ps, {"op": "run", "request": "r", "threads": 3}))["result"]["run"]
        await wait_ended(pool, rs_)
        rr = rounds_of(rts.calls, rs_)
        l2 = sorted(thread_of(kw) for kw in rr.get(2, []))
        l3 = sorted(thread_of(kw) for kw in rr.get(3, []))
        check("S a thread whose turn did not land sits out the next round; the others go on",
              l2 == [0, 2], l2)
        check("S ...and runs again the round after", l3 == [0, 1, 2], l3)

        # ── L: the wake gives a lane its own registry key ──
        from mastercode.runtime import session as sess
        from server import turn_registry as reg_mod

        class _Reg:
            def active_for(self, key):
                return None
        sent: list = []

        async def _send(**kw):
            sent.append(kw)
            yield '0:"done"\n'

        saved = (brain_service.send_message, reg_mod.get_turn_registry, sess.may_wake, sess.unseen_lane)
        brain_service.send_message = _send
        reg_mod.get_turn_registry = lambda: _Reg()
        sess.may_wake = lambda: True
        sess.unseen_lane = lambda *a, **k: False
        try:
            rtw = sess.EntityRuntime.__new__(sess.EntityRuntime)
            kw = dict(entity_id=E, app_id="workbench", project_id=pa, thread_id=None, brief="b",
                      origin_request="r", display_message="d", anchor="t", title="t")
            sess._woken_since_person.clear()
            await rtw._wake_host_turn(**kw, lane="task-9-t1", chain=True)
            lane_counted = any(k.endswith("|task-9-t1") for k in sess._woken_since_person)
            await rtw._wake_host_turn(**kw)
        finally:
            (brain_service.send_message, reg_mod.get_turn_registry, sess.may_wake, sess.unseen_lane) = saved
        check("L a thread's lane is never chained (the run has its own stall rule; nothing would reset it)",
              not lane_counted, dict(sess._woken_since_person))
        base = reg_mod.compose_thread_key(E, "workbench", pa)
        check("L a thread's turn runs on its own key beside the project's; a plain wake on the project's",
              sent[0].get("thread_key") == f"{base}|task-9-t1" and "thread_key" not in sent[1],
              [s.get("thread_key") for s in sent])
    finally:
        await pool.close()
    return 1 if FAILED else 0


with tempfile.TemporaryDirectory() as _cg, \
        (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db, _cg))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
