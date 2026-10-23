#!/usr/bin/env python3
"""The build lane's limits judge the work, not the platform, and cut nothing
without a word.

  1. A phase's retry budget and its automatic repairs are spent only by the
     WORK failing. A failure on the platform's side (a wake that failed, a
     dropped pod connection, a lost stream, a collect that failed, a kernel
     restart, the build-stuck reap, the kernel's own crash) is told apart by
     the words its writer stores, gets its own retry, and is reported as the
     platform's. A links_refused row counts toward the repairs no more than
     toward the retry.
  2. A mesh / lab coding-agent run defaults far above real work, the Host waits
     on the box's own heartbeat instead of a total clock, and a run it stopped
     waiting for is reported as that, never as "no live harness worker".
  3. An acceptance check's declared timeout is honoured, not cut to 1800 s; a
     value the pod cannot time is refused by name when the plan is written.
  4. A build row stores every landed file, and the closing report the Host
     reads says when it was cut and where the whole of it is (the build row,
     the build envelope and a compare's summary).

Pure Python, no pod, no DB. Run: APP_ROOT=<repo> python3 scripts/test_build_lane_limits.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain"), os.path.join(ROOT, "system-apps")]

FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        if extra:
            print(f"        {str(extra)[:400]}")
        FAILS.append(name)


def run(coro):
    return asyncio.run(coro)


from mastercode.runtime import tools as T  # noqa: E402

# The error text each platform-side writer actually stores in entity_tasks.error
# (the factory-build lane appends its own continue advice after it; see
# tools._pool_build), and work failures that only mention such words.
WAKE = ("harness wake failed: operator /harness/wake 504: {\"detail\":\"harness-claude-code "
        "not Ready within 60s\"} — the failed attempt left no recoverable files")
PLATFORM_ERRORS = {
    "wake": WAKE,
    "routable": "harness kimi never became routable in 30s: health 503 — the failed attempt left no recoverable files",
    "pod connection": ("harness kimi pod connection failed after 12.3s with no usable response: "
                       "ServerDisconnectedError('Server disconnected') — the run may have started"),
    "bare disconnect (older rows)": "Server disconnected — the failed attempt left no recoverable files",
    "stream": "the live stream died mid-run after 6 frames; the build was not re-run and its partial work is kept",
    "collect": "the run finished, but its files could not be collected from the harness: TimeoutError",
    "kernel restart": "kernel restarted mid-run — the running task did not survive",
    "stuck reap": "build stuck (kernel restarted or pod run wedged)",
    "kernel crash": "build crashed: 'NoneType' object has no attribute 'get'",
    "nothing ran": "no build ran: the project's seed is still being copied",
}
WORK_ERRORS = {
    "the CLI's own error": "error: failed to run prompt: provider.connection_error: terminated",
    "a stall": "kimi produced no output for 10 minutes and was stopped as stuck",
    "stderr that mentions a disconnect": "TypeError: x is undefined | upstream said: Server disconnected",
    "a test failure": "3 tests failed: harness wake failed was printed by the app under test",
}


def section1():
    print("== 1. the retry budget and the repairs count the work, not the platform ==")
    kind = getattr(T, "_failed_build_kind", None)
    check("one classifier exists (tools._failed_build_kind)", callable(kind))
    if callable(kind):
        for name, err in PLATFORM_ERRORS.items():
            check(f"platform-side: {name}", kind({"error": err}) == "platform", kind({"error": err}))
        for name, err in WORK_ERRORS.items():
            check(f"the work's own: {name}", kind({"error": err}) == "work", kind({"error": err}))
        for flag in ("acceptance_refused", "links_refused", "integration_conflict"):
            check(f"a {flag} row counts toward nothing",
                  kind({"error": WAKE, flag: True}) == "refused")

    # The classifier keys on words other files write: if a writer rewords its
    # failure, this says so here instead of the failure quietly counting as
    # the work's again.
    def src(rel):
        return open(os.path.join(ROOT, rel)).read()
    writers = {
        "server/routes/review.py": ("harness wake failed: ", "never became routable in ",
                                    "pod connection failed after ",
                                    "the live stream died mid-run after ",
                                    "the run finished, but its files could not be collected "),
        "brain/mastercode/runtime/agent_dispatch.py": (
            "kernel restarted mid-run — the running task did not survive",
            "build stuck (kernel restarted or pod run wedged)"),
        "brain/mastercode/runtime/tools.py": ('f"build crashed: {e}"', 'f"no build ran: {e}"'),
    }
    for rel, words in writers.items():
        text = tools_source(ROOT) if rel.endswith("runtime/tools.py") else src(rel)
        for w in words:
            check(f"the writer still says {w!r} ({rel.rsplit('/', 1)[-1]})", w in text)

    def row(err=None, **flags):
        return {"status": "failed", "error": err or "error: tests failed",
                "result": {"project_id": 9, "phase_id": 7, **flags}}

    class Pool:
        """Answers both the old SQL count (COUNT(*) ... the SQL's own exclusions)
        and the failed-row read, so the same scenario runs against either tree."""

        def __init__(self, rows, reviews=0):
            self.rows, self.reviews, self.notes = rows, reviews, []

        def _sql_count(self, sql):
            n = 0
            for r in self.rows:
                res = r["result"]
                if r["status"] != "failed":
                    continue
                if "acceptance_refused" in sql and res.get("acceptance_refused"):
                    continue
                if "links_refused" in sql and res.get("links_refused"):
                    continue
                if "integration_conflict" in sql and "integration_conflict" in res:
                    continue
                n += 1
            return n

        async def fetchval(self, sql, *a):
            if "build_chain_halt" in sql:
                return False
            if "COUNT(*) FROM entity_tasks" in sql:
                return self._sql_count(sql)
            return None

        async def fetchrow(self, sql, *a):
            if "FROM project_phases" in sql:
                return {"id": 7, "phase_number": 2, "title": "Backend", "spec": "brief",
                        "description": None, "status": "failed", "gate": "none",
                        "approved_at": None, "acceptance": None}
            if "COUNT(*) FILTER" in sql:
                out = {"reviews": self.reviews}
                if "AS fails" in sql:
                    out["fails"] = self._sql_count(sql)
                return out
            return None

        async def fetch(self, sql, *a):
            if "FROM entity_tasks" in sql and "status='failed'" in sql:
                return [{"error": r["error"],
                         "acceptance_refused": bool(r["result"].get("acceptance_refused")),
                         "links_refused": bool(r["result"].get("links_refused")),
                         "integration_conflict": "integration_conflict" in r["result"]}
                        for r in self.rows if r["status"] == "failed"]
            return []

        async def execute(self, sql, *a):
            self.notes.append(" ".join(str(x) for x in a))
            return "UPDATE 1"

    def pick(rows, failure):
        p = Pool(rows)
        out = run(T._phase_retry_pick(pool=p, entity_id=1, project_id=9, phase_id=7,
                                      failure=failure, chain_depth=1, finished_task_id=2))
        return out, p

    out, _ = pick([row(WAKE), row("error: tests failed")], "error: tests failed")
    check("a wake failure then the work's first failure: the seeded retry still fires",
          isinstance(out, tuple) and "tests failed" in out[1], out)
    out, _ = pick([row("error: tests failed"), row(PLATFORM_ERRORS["stream"])], PLATFORM_ERRORS["stream"])
    check("the work failed once, then the platform lost the stream: retried, not 'failed twice'",
          isinstance(out, tuple), out)
    check("...and the retry is told the platform stopped it, not to change its approach",
          isinstance(out, tuple) and "stopped by the platform" in out[1]
          and "do not repeat the same approach" not in out[1], out)
    out, p = pick([row("error: a"), row("error: b")], "error: b")
    check("two failures of the work still stop the chain (the budget is unchanged)",
          out is None and any("FAILED 2 times" in n for n in p.notes), (out, p.notes))
    out, p = pick([row(WAKE), row(PLATFORM_ERRORS["pod connection"])], PLATFORM_ERRORS["pod connection"])
    check("the platform failing twice stops the chain and says it was the platform",
          out is None and any("platform's side" in n and "none used its retry" in n for n in p.notes)
          and not any("FAILED" in n for n in p.notes), (out, p.notes))

    from mastercode.memory_engine import CURRENT_TURN_WOKEN

    def repair(rows, reviews=0):
        tok = CURRENT_TURN_WOKEN.set({"session_id": "s", "woken": True})
        try:
            return run(T.phase_repair_refusal(Pool(rows, reviews), 1, 9,
                                              {"id": 7, "phase_number": 2, "title": "Backend"}))
        finally:
            CURRENT_TURN_WOKEN.reset(tok)

    r = repair([row("error: a"), row(WAKE), row(PLATFORM_ERRORS["stream"])])
    check("repairs: one failure of the work and two of the platform leave repairs (woken turn)",
          r is None, r)
    r = repair([row("error: a", links_refused=True)] * 3)
    check("repairs: links_refused rows spend none (as the retry budget already did)", r is None, r)
    r = repair([row("error: a"), row("error: b"), row("error: c")])
    check("repairs: three landings of the work that did not complete still spend them",
          r is not None and r.get("reason") == "repairs_spent", r)
    r = repair([row(WAKE)] * 3)
    check("repairs: the platform failing three times refuses the woken rebuild, naming the platform",
          r is not None and r.get("reason") == "platform_failing"
          and "platform's side" in r["result"] and "none used its repairs" in r["result"], r)


def section2():
    print("== 2. a mesh / lab coding-agent run: liveness, a far default, a timeout said plainly ==")
    from server.network import nac_mgmt, nac_harness, nac_approval, nac_stepup

    check("the default is far above real coding work (>= 240 min)",
          getattr(nac_harness, "HARNESS_RUN_DEFAULT_MINUTES", 10) >= 240)

    class FakeWS:
        """A box: after the H frame it sends `beats` O frames `every` seconds apart,
        then (if `finish`) the J result and the X exit."""

        def __init__(self, beats, every, finish=True):
            self.beats, self.every, self.finish = beats, every, finish
            self.queue: asyncio.Queue = asyncio.Queue()
            self.sent = []

        async def send_bytes(self, b):
            self.sent.append(b)
            if b[:1] == b"H":
                asyncio.ensure_future(self._play())

        async def _play(self):
            for _ in range(self.beats):
                await asyncio.sleep(self.every)
                await self.queue.put(b"Orunning\n")
            if self.finish:
                await self.queue.put(b"J" + json.dumps({"ok": True}).encode())
                await self.queue.put(b"X0")

        async def receive_bytes(self):
            return await self.queue.get()

        async def receive(self):
            await asyncio.sleep(3600)

    async def relay_run(ws, timeout):
        relay = nac_harness.HarnessRelay()
        w = nac_harness.HarnessWorker(ws, 5, lane="harness")
        serve = asyncio.ensure_future(w.serve(relay))
        await asyncio.sleep(0.01)
        t0 = time.monotonic()
        try:
            return await relay.dispatch(5, {"harness_id": "x"}, timeout=timeout), time.monotonic() - t0
        except Exception as e:  # noqa: BLE001
            return e, time.monotonic() - t0
        finally:
            serve.cancel()

    _silence = getattr(nac_harness, "HARNESS_SILENCE_S", None)
    if _silence is not None:
        nac_harness.HARNESS_SILENCE_S = 0.3
    try:
        out, took = run(relay_run(FakeWS(beats=12, every=0.1), timeout=10))
        check("a run the box keeps reporting is waited for and its result returned",
              isinstance(out, tuple) and out[2] == 0, out)
        out, took = run(relay_run(FakeWS(beats=0, every=0.1, finish=False), timeout=3))
        check("a box that goes silent is noticed by its silence, not after the whole wait",
              type(out).__name__ == "HarnessRunStopped" and getattr(out, "why", "") == "silent"
              and took < 1.5, (out, round(took, 2)))
        out, took = run(relay_run(FakeWS(beats=100, every=0.05, finish=False), timeout=0.6))
        check("a run still reported at the backstop is stopped as 'over', not as no worker",
              type(out).__name__ == "HarnessRunStopped" and getattr(out, "why", "") == "over", out)
    finally:
        if _silence is not None:
            nac_harness.HARNESS_SILENCE_S = _silence

    # The tool side, driven with fakes the way test_bastion_service_slot does.
    class FakePool:
        async def fetchval(self, q, *a):
            return 7 if "token_id" in q else None

        async def fetchrow(self, q, *a):
            return None

        async def fetch(self, q, *a):
            return []

        async def execute(self, q, *a):
            return "INSERT 1"

    class FakeRelay:
        def __init__(self, stop=None):
            self.sent, self.timeouts, self.stop = [], [], stop

        def worker_count(self, token_id, lane="harness"):
            return 1

        async def dispatch(self, token_id, spec, timeout=0):
            self.sent.append(spec)
            self.timeouts.append(timeout)
            if self.stop is not None:
                raise self.stop
            return json.dumps({"ok": True}).encode(), b"", 0

    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return set()

    async def _grants(*a, **k):
        return [{"server": "box", "ops": [nac_mgmt.HARNESS_RUN_OP, "image:*"]}]

    async def _obtain(*a, **k):
        return {"state": "approved", "approval": "APPROVAL"}

    audits = []

    async def _audit(pool, eid, server, op, *, exit_code, decision):
        audits.append(decision)

    async def _creds(*a, **k):
        return None

    saved = (nac_stepup.entity_verify_pubkey, nac_mgmt.load_mgmt_grants, nac_mgmt.load_suspended_servers,
             nac_mgmt.load_quarantined, nac_mgmt.audit_mgmt, nac_harness.resolve_agent_creds,
             nac_harness.get_harness_relay, nac_approval.obtain)
    nac_stepup.entity_verify_pubkey = _none
    nac_mgmt.load_mgmt_grants = _grants
    nac_mgmt.load_suspended_servers = _empty
    nac_mgmt.load_quarantined = _empty
    nac_mgmt.audit_mgmt = _audit
    nac_harness.resolve_agent_creds = _creds
    nac_approval.obtain = _obtain
    # An agent run (mode='worktree') is the platform operator's alone
    # (nac_harness.host_mode_refusal), so the run under test is the operator's.
    from mastercode.runtime.tools import OPERATOR_ENTITY_ID as OPERATOR
    try:
        relay = FakeRelay()
        nac_harness.get_harness_relay = lambda: relay
        run(nac_harness.run_harness_op(FakePool(), OPERATOR, "box", "h1", image="my/agent:1",
                                       mode="worktree", prompt="fix it"))
        sent = relay.sent[0] if relay.sent else {}
        check("an unset max_minutes reaches the box as the far default, not 10",
              sent.get("max_minutes") == getattr(nac_harness, "HARNESS_RUN_DEFAULT_MINUTES", -1),
              sent.get("max_minutes"))
        mm = int(sent.get("max_minutes") or 0)
        check("the Host's backstop sits above the run's own limit (twice its budget)",
              relay.timeouts and relay.timeouts[0] >= 2 * (mm * 60 + 30), relay.timeouts)
        stop_cls = getattr(nac_harness, "HarnessRunStopped", None)
        for why, words in (("silent", "went silent"), ("over", "still being reported")):
            if stop_cls is None:
                check(f"a run the Host stopped waiting for ({why}) is reported as that", False,
                      "nac_harness.HarnessRunStopped does not exist")
                continue
            relay = FakeRelay(stop=stop_cls(why, 900.0))
            nac_harness.get_harness_relay = lambda: relay
            try:
                run(nac_harness.run_harness_op(FakePool(), OPERATOR, "box", "h1", image="my/agent:1",
                                               mode="worktree", prompt="fix it"))
                err = None
            except nac_mgmt.MgmtError as e:
                err = e
            check(f"a run the Host stopped waiting for ({why}) is reported as that, in plain words",
                  err is not None and err.status == 504 and words in err.detail
                  and "no live harness worker" not in err.detail, err and err.detail)
        check("...and the audit says the run started", any(str(d).startswith("stopped-waiting") for d in audits),
              audits)
    finally:
        (nac_stepup.entity_verify_pubkey, nac_mgmt.load_mgmt_grants, nac_mgmt.load_suspended_servers,
         nac_mgmt.load_quarantined, nac_mgmt.audit_mgmt, nac_harness.resolve_agent_creds,
         nac_harness.get_harness_relay, nac_approval.obtain) = saved

    # Every caller leaves the default to the lane.
    tsrc = tools_source(ROOT)
    nsrc = open(os.path.join(ROOT, "server/routes/network.py")).read()
    check("mesh_harness_run, lab_run and POST /harness/run carry no 10-minute default of their own",
          'a.get("max_minutes") or 10' not in tsrc and 'body.get("max_minutes") or 10' not in nsrc)
    from mastercode import public_tool_registry as R
    P = {**getattr(R, "PUBLIC_TOOL_SCHEMAS", {}), **getattr(R, "OPERATOR_PLATFORM_TOOL_SCHEMAS", {})}
    for tool in ("mesh_harness_run", "lab_run"):
        d = (P.get(tool, {}).get("function", {}).get("parameters", {})
             .get("properties", {}).get("max_minutes", {}).get("description", ""))
        check(f"{tool}.max_minutes no longer says the run is cut off at a 10-minute default",
              d and "default 10" not in d and "240" in d, d)


def section3():
    print("== 3. an acceptance check's declared timeout is honoured, or refused by name ==")
    norm = T._normalize_acceptance
    out = norm([{"cmd": "python3 -m pytest", "timeout_s": 3600}])
    check("a 3600 s check keeps 3600 s (it was cut to 1800 without a word)",
          out[0]["timeout_s"] == 3600, out)
    check("the default is still 300 s", norm(["make test"])[0]["timeout_s"] == 300)
    for bad, why in ((10 ** 7, "over what the pod can time"), (-5, "negative")):
        try:
            got = norm([{"cmd": "x", "timeout_s": bad}])
            check(f"a {why} timeout is refused by name at plan time", False, got)
        except ValueError as e:
            check(f"a {why} timeout is refused by name at plan time", "timeout_s" in str(e), str(e))


def section4():
    print("== 4. landed files whole; the closing report marked where it was cut ==")
    import server.routes.review as rv

    captured = {}

    class RowPool:
        async def fetchrow(self, sql, *a):
            return None

        async def execute(self, sql, *a):
            captured["sql"], captured["args"] = sql, a
            return "UPDATE 1"

    files = [f"src/f{i}.py" for i in range(120)]
    run(T._factory_build_task_finish(RowPool(), 41, {"status": "ok", "result": {
        "summary": "y" * 9000, "changed_files": files, "run_id": 77, "harness": "kimi",
        "session_id": "s-1"}}))
    merge = json.loads(captured["args"][0]) if captured.get("args") else {}
    check("the build row stores every landed file (it kept 50)",
          merge.get("changed_files") == files, len(merge.get("changed_files") or []))
    check("the row's report says it was cut and where the whole of it is",
          "[cut: " in str(merge.get("summary")) and "harness_transcript(run_id=77)" in str(merge.get("summary"))
          and "[cut: " in str(merge.get("result")), str(merge.get("summary"))[-200:])
    check("...and a short report is stored as it is",
          True if not hasattr(T, "_mark_cut") else T._mark_cut("short", 4000, "x") == "short")

    async def _rshb(**kw):
        return {"ok": True, "final_report": "z" * 9000, "transcript": "", "changed_files": [],
                "run_id": 5, "harness": "kimi", "session_id": "s-5"}

    class P2:
        async def fetchval(self, *a, **k):
            return 1

        async def fetch(self, *a, **k):
            return []

        async def fetchrow(self, *a, **k):
            return None

    orig = rv.run_single_harness_build
    rv.run_single_harness_build = _rshb
    try:
        env = run(T._pool_build(entity_id=1, pool=P2(), project_id=43, prompt="p",
                                research_id=None, timeout_s=60, harness="kimi",
                                billing_key="factory-build-1", one_shot=True))
    finally:
        rv.run_single_harness_build = orig
    summ = str((env.get("result") or {}).get("summary") or "")
    check("the build envelope's report says it was cut and names harness_transcript(run_id=5)",
          env.get("status") == "ok" and "[cut: " in summ and "harness_transcript(run_id=5)" in summ
          and "of 9000 characters" in summ, summ[-200:] or env)

    # A compare whose summary runs long (sixteen variants, each with a long error).
    vids = [f"v{i}" for i in range(16)]
    rows = {}

    class FanPool:
        async def execute(self, sql, *a):
            rows["args"] = a
            return "UPDATE 1"

    async def _await(sid, **k):
        return {"session_id": sid, "harnesses": vids, "completed_at": 1.0,
                "results": {v: {"ok": False, "error": "E" * 400} for v in vids}}

    oa, os_, od = getattr(rv, "await_session", None), rv._read_session_state, T._get_runtime_dispatch
    rv.await_session = _await
    rv._read_session_state = lambda sd: {v: {"state": "failed", "error": "E" * 400} for v in vids}
    T._get_runtime_dispatch = lambda: None
    try:
        run(T._watch_fan_out(FanPool(), 501, "fan-1"))
    finally:
        if oa is not None:
            rv.await_session = oa
        rv._read_session_state, T._get_runtime_dispatch = os_, od
    a = rows.get("args") or ()
    fin = {}
    for x in a:
        if isinstance(x, str) and x.startswith("{"):
            fin = json.loads(x)
    check("a compare's long summary says it was cut and where every variant's state is",
          "[cut: " in str(fin.get("summary")) and "review_session('fan-1')" in str(fin.get("summary")),
          str(fin.get("summary"))[-200:])


def main() -> int:
    for s in (section1, section2, section3, section4):
        try:
            s()
        except Exception as e:  # noqa: BLE001 — a section that cannot run is a failure
            import traceback
            traceback.print_exc()
            check(f"{s.__name__} ran to the end", False, repr(e))
    print(f"\n[build-lane-limits] {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
