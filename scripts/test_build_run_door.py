#!/usr/bin/env python3
"""One build door.

Before it, `build` refused any project without a plan and the only unplanned door
was review.fan_out, so the host could describe a single run only with that
tool's word; four models on one harness were inexpressible in one call; and
review.compare had a model pick the winner. What this pins:

  1. build(action='run') on a ZERO-phase project dispatches ONE engine and
     never touches project_phases — and the pool is told it is a one-shot.
  2. The plan gate still holds for a phase build: action='build' on a
     zero-phase project refuses with today's text, and _pool_build without
     one_shot refuses with its own.
  3. Four codex variants (four models) through the compare door become four
     worktrees on ONE pod with ONE release, each run carrying its own model.
  4. An unknown harness in variants is a 400 that names it; mixing variants
     with samples/harness_model is a 400; the build door relays the reason.
  5. Checks declared on a compare run on every variant, after its harness, in
     that variant's own pod directory, and the verdict sits on its state row.
  6. review.compare returns candidates side by side and asks NO model.
  7. review.fan_out is gone: no schema, no dispatch, retired in the guard.

Container-free: the pool, the bridge and the pod are stubbed; nothing else is.

Run: APP_ROOT=<repo> python3 scripts/test_build_run_door.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.routes import review as rv  # noqa: E402
from server.routes import harnesses as _hn  # noqa: E402
from server.routes import _preference_profile as _pp  # noqa: E402
from mastercode.runtime import tools as t  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── a pool that records every statement ────────────────────────────────────
class _Pool:
    def __init__(self, phases=0):
        self.phases = phases
        self.sql: list = []

    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        if "FROM projects WHERE id=$1 AND entity_id=$2" in sql:
            return 1
        if "COUNT(*) FROM project_phases" in sql:
            return self.phases
        return None

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        if "INSERT INTO entity_tasks" in sql:
            return {"task_id": 77}
        return None

    async def execute(self, sql, *a):
        self.sql.append(sql)
        return "UPDATE 1"

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        return []

    def phase_writes(self):
        return [s for s in self.sql if "project_phases" in s
                and any(w in s.upper() for w in ("INSERT", "UPDATE", "DELETE"))]


print("== 1. run on a zero-phase project: one engine, no plan touched ==")
_saved = (rv.resolve_build_target, t._run_phase_build)
captured: dict = {}


async def _resolve(h, model=None):
    return (h or "codex"), None, None


async def _fake_phase_build(**kw):
    captured.update(kw)

rv.resolve_build_target = _resolve
t._run_phase_build = _fake_phase_build
try:
    pool = _Pool(phases=0)

    async def _go():
        out = await t._exec_build({"action": "run", "prompt": "make a reader app",
                                   "harness": "codex"},
                                  3, pool, "sess-1", "project:9240")
        await asyncio.sleep(0)
        return out
    out = run(_go())
    res = out.get("result") or {}
    check("run is accepted with no plan", out.get("status") == "ok", out)
    check("...on the ONE engine named", res.get("harness") == "codex" and res.get("task_id") == 77, res)
    check("...the project came from the conversation (project:9240)", res.get("project_id") == 9240, res)
    check("...the detached build is a one-shot with no phase",
          captured.get("one_shot") is True and captured.get("phase_id") is None
          and captured.get("project_id") == 9240, captured)
    check("...and project_phases was never written", pool.phase_writes() == [], pool.phase_writes())
    check("...no plan note on an unplanned project", "does NOT advance the plan" not in res.get("note", ""), res)

    pool2 = _Pool(phases=2)
    out2 = run(t._exec_build({"action": "run", "prompt": "tweak the css", "project_id": 9240},
                             3, pool2, "sess-1", "project:9240"))
    check("on a PLANNED project a run says it does not advance the plan",
          "does NOT advance the plan" in (out2.get("result") or {}).get("note", ""), out2)
    check("...and still writes no phase", pool2.phase_writes() == [], pool2.phase_writes())

    out3 = run(t._exec_build({"action": "run", "project_id": 9240}, 3, _Pool(), "s", None))
    check("a run with no prompt is refused by name", out3.get("status") == "error"
          and "prompt" in out3.get("result", ""), out3)
    out4 = run(t._exec_build({"action": "run", "prompt": "x", "project_id": 9240, "seed": False},
                             3, _Pool(), "s", None))
    check("seed=false on a single build is refused, not ignored",
          out4.get("status") == "error" and "seed" in out4.get("result", ""), out4)

    print("== 2. the plan gate still holds for every phase lane ==")
    out5 = run(t._exec_build({"action": "build", "project_id": 9240}, 3, _Pool(phases=0), "s", None))
    check("action='build' on a zero-phase project refuses with today's text",
          out5.get("status") == "error"
          and out5.get("result", "").startswith("This project has no plan yet — refusing to build."), out5)
    out6 = run(t._pool_build(entity_id=9003, pool=_Pool(phases=0), project_id=9240, prompt="p",
                             research_id=None, timeout_s=60))
    check("_pool_build without one_shot refuses a planless project",
          out6.get("status") == "error"
          and out6.get("result", "").startswith("This project has no plan — refusing to run a build."), out6)
finally:
    rv.resolve_build_target, t._run_phase_build = _saved

_saved_rsb = rv.run_single_harness_build
seen_rsb: dict = {}


async def _rsb(**kw):
    seen_rsb.update(kw)
    return {"ok": False, "error": "stub pod"}
rv.run_single_harness_build = _rsb
try:
    p = _Pool(phases=0)
    run(t._pool_build(entity_id=9003, pool=p, project_id=9240, prompt="p", research_id=None,
                      timeout_s=60, one_shot=True))
    check("_pool_build(one_shot=True) passes the gate to the pool without counting phases",
          seen_rsb.get("project_id") == 9240
          and not any("project_phases" in s for s in p.sql), p.sql)
finally:
    rv.run_single_harness_build = _saved_rsb


print("== 3-5. compare: four codex models → four worktrees, one pod, one release ==")


class _Ledger:
    def __init__(self):
        self.calls: list = []

    async def request_full(self, op, params=None):
        self.calls.append((op, dict(params or {})))
        if op == "harness.capacity":
            hs = list((params or {}).get("harnesses") or [])
            return {"ok": True, "data": {"gated": True, "fits": hs, "refused": [],
                                         "headroom_mi": 4096, "remaining_mi": 2048,
                                         "upgrade_to": None, "pool_add_mi": None}}
        if op == "harness.wake":
            return {"ok": True, "data": {"url": f"http://{params['harness']}.fake", "cap": None}}
        if op == "harness.release":
            return {"ok": True, "data": {}}
        return {"ok": False, "error": f"Unknown operation: {op}"}

    def n(self, op):
        return sum(1 for o, _ in self.calls if o == op)


runs: list = []
checks_ran: list = []
dropped: list = []


async def _fake_run_on_pod(http, base, harness_id, label, prompt, worktree, max_minutes,
                           started, *, model=None, effort=None, session_dir=None,
                           keep_dir=False, **_):
    runs.append({"label": label, "model": model, "worktree": str(worktree),
                 "keep_dir": keep_dir, "base": base})
    return {"harness": label, "ok": True, "exit_code": 0, "elapsed_seconds": 1.0,
            "model": model, "diff": "", "transcript_full": "", "collected": True}


async def _fake_acceptance(http, base, project_dir, commands, harness_id=None):
    checks_ran.append(project_dir)
    ok = not project_dir.endswith("-codex-s3")
    return {"ran": True, "ok": ok, "results": [{"cmd": commands[0]["cmd"], "ok": ok}]}


async def _fake_drop(http, base, dir_name):
    dropped.append(dir_name)
    return True


async def _fake_collect(http, base, session_dir, harness_id):
    return True


async def _installed():
    return ["codex", "claude-code"]


async def _no_settings():
    return {}


async def _refresh_off():
    return False

_saved_c = (rv._installed_harnesses, rv._coding_harness_settings, _hn.harness_validity,
            _hn.platform_refresh_enabled, _pp.ban_for, rv._prepare_worktree,
            rv.SESSIONS_ROOT, rv.PROJECTS_ROOT, rv._bridge, rv._run_on_pod,
            rv._collect_session, rv._harness_pod_creds, rv._run_acceptance, rv._drop_pod_dir)
with tempfile.TemporaryDirectory() as td:
    try:
        ledger = _Ledger()
        rv._installed_harnesses = _installed
        rv._coding_harness_settings = _no_settings
        _hn.harness_validity = lambda pid: {"valid": True, "present": True}
        _hn.platform_refresh_enabled = _refresh_off
        _pp.ban_for = lambda c: None

        def _prep(parent, vid, seed=False, seed_from=None):
            p = Path(parent) / vid
            p.mkdir(parents=True, exist_ok=True)
            return p
        rv._prepare_worktree = _prep
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        rv.PROJECTS_ROOT = Path(td) / "projects"
        rv.SESSIONS_ROOT.mkdir()
        rv.PROJECTS_ROOT.mkdir()
        rv._bridge = lambda: ledger
        rv._run_on_pod = _fake_run_on_pod
        rv._collect_session = _fake_collect
        rv._harness_pod_creds = lambda h: {}
        rv._run_acceptance = _fake_acceptance
        rv._drop_pod_dir = _fake_drop

        models = ["gpt-5.5", "gpt-5.5-mini", "o6", "gpt-5.4"]
        req = rv.FanOutReq(prompt="build the reader",
                           variants=[{"harness": "codex", "model": m} for m in models],
                           acceptance=[{"cmd": "test -f index.html", "expect_exit": 0,
                                        "timeout_s": 60}])
        async def _dispatch_and_wait():
            o = await rv.fan_out(req)
            for _ in range(400):
                mf = json.loads((rv.SESSIONS_ROOT / o["session_id"] / "manifest.json").read_text())
                if mf.get("completed_at"):
                    return o, mf
                await asyncio.sleep(0.02)
            return o, {}
        # One loop for the dispatch AND the wait: fan_out's runner is a task
        # on the loop that created it.
        out, m = run(_dispatch_and_wait())
        sid = out["session_id"]
        check("fan_out names every run it started, one per variant",
              [v["vid"] for v in out.get("variants", [])]
              == ["codex", "codex-s2", "codex-s3", "codex-s4"], out.get("variants"))
        st = rv._read_session_state(rv.SESSIONS_ROOT / sid)
        check("the compare completed", bool(m.get("completed_at")), m)
        check("four runs, four distinct worktrees",
              len(runs) == 4 and len({r["worktree"] for r in runs}) == 4, runs)
        check("...each with ITS model (four models on one harness in one call)",
              [r["model"] for r in runs] == models, [r["model"] for r in runs])
        check("...all on ONE pod", len({r["base"] for r in runs}) == 1, {r["base"] for r in runs})
        check("...closed by ONE release", ledger.n("harness.release") == 1, ledger.calls)
        check("checks ran on every variant, in its own pod directory",
              sorted(checks_ran) == sorted(f"/var/lib/cseries/projects/{sid}-{v}"
                                           for v in ("codex", "codex-s2", "codex-s3", "codex-s4")),
              checks_ran)
        check("...the directory was kept for them and dropped after",
              all(r["keep_dir"] for r in runs) and len(dropped) == 4, (runs, dropped))
        check("...and each verdict sits on the variant's state row",
              st["codex"]["acceptance"]["ok"] is True
              and st["codex-s3"]["acceptance"]["ok"] is False
              and st["codex-s3"]["state"] == "done", st)

        print("== 6. review.compare shows; no model picks ==")
        import mastercode.provider_gateway as _pg
        _saved_cm = _pg.call_model
        called = []

        async def _no_model(*a, **k):
            called.append(1)
            raise AssertionError("compare asked a model")
        _pg.call_model = _no_model
        try:
            cmp = run(rv.compare(rv.CompareReq(session_id=sid)))
        finally:
            _pg.call_model = _saved_cm
        cands = cmp.get("candidates") or []
        check("compare returns every candidate", [c["variant"] for c in cands]
              == ["codex", "codex-s2", "codex-s3", "codex-s4"], cands)
        check("...with its model and its check verdict",
              cands and cands[2]["model"] == "o6" and cands[2]["checks"]["ok"] is False
              and cands[0]["checks"]["ok"] is True, cands[:3])
        check("...and a preview route per variant",
              all(c["preview_url"].endswith(f"/preview/{c['variant']}") for c in cands), cands)
        check("...asking no model and writing no picks",
              not called and "picks" not in cmp
              and not (rv.SESSIONS_ROOT / sid / "picks.json").exists(), cmp)
        check("...and telling the host the user picks",
              "user picks" in cmp.get("next_step", ""), cmp.get("next_step"))

        print("== 4. refusals name what is wrong ==")
        from fastapi import HTTPException
        for label, body, needle in (
            ("a variant id where a harness goes",
             {"variants": [{"harness": "codex"}, {"harness": "codex-s2"}]}, "'codex-s2'"),
            ("variants mixed with samples",
             {"variants": [{"harness": "codex"}], "samples": {"codex": 2}}, "cannot be combined"),
            ("a variant with no harness", {"variants": [{"model": "o6"}]}, "names no harness"),
        ):
            try:
                run(rv.fan_out(rv.FanOutReq(prompt="p", **body)))
                check(f"{label} -> 400", False, "accepted")
            except HTTPException as e:
                check(f"{label} -> 400 naming it", e.status_code == 400 and needle in str(e.detail),
                      (e.status_code, e.detail))

        _saved_r2 = rv.resolve_build_target
        rv.resolve_build_target = _resolve
        try:
            out7 = run(t._exec_build({"action": "run", "prompt": "p", "project_id": 9240,
                                      "variants": [{"harness": "codex-s2"}]},
                                     3, _Pool(), "s", None))
        finally:
            rv.resolve_build_target = _saved_r2
        check("the build door relays the 400's reason, not a class name",
              out7.get("status") == "error" and "compare refused" in out7.get("result", "")
              and "'codex-s2'" in out7.get("result", ""), out7)
    finally:
        (rv._installed_harnesses, rv._coding_harness_settings, _hn.harness_validity,
         _hn.platform_refresh_enabled, _pp.ban_for, rv._prepare_worktree,
         rv.SESSIONS_ROOT, rv.PROJECTS_ROOT, rv._bridge, rv._run_on_pod,
         rv._collect_session, rv._harness_pod_creds, rv._run_acceptance,
         rv._drop_pod_dir) = _saved_c


print("== 8. one_shot is set in ONE place, and a run's repair inherits it ==")
import ast as _ast  # noqa: E402


def _one_shot_sites(path):
    tree = _ast.parse(Path(path).read_text())
    out = []
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Call):
                for kw in n.keywords:
                    if kw.arg == "one_shot":
                        out.append((fn.name, _ast.unparse(kw.value)))
    return out

sites = (_one_shot_sites(ROOT / "brain/mastercode/runtime/tools.py")
         + _one_shot_sites(ROOT / "brain/mastercode/runtime/app_verify.py"))
# ast.walk visits nested defs from the outer function too; keep the innermost.
allowed = {("_exec_build_run", "True"), ("_start_repair", "True"),
           ("_run_phase_build", "one_shot")}
literal_true = {f for f, v in sites if v == "True"}
check("one_shot=True is passed only by a run and by a kernel repair",
      literal_true == {"_exec_build_run", "_start_repair"}, sites)
check("every other one_shot keyword only forwards what it was given",
      {x for x in sites if x[1] != "True"} <= allowed, sites)

from mastercode.runtime import app_verify as _av  # noqa: E402
_saved_av = (t._run_phase_build, t._factory_build_task_start, _av._in_flight)
got: list = []


async def _rpb(**kw):
    got.append(kw.get("one_shot"))


async def _fbts(*a, **k):
    return 91


async def _none(*a, **k):
    return None
t._run_phase_build, t._factory_build_task_start, _av._in_flight = _rpb, _fbts, _none
try:
    async def _rep(origin):
        await _av._start_repair(pool=_Pool(), entity_id=9003, project_id=9240, app_id="reader",
                                seed="boot error", repair={"round": 1, "harness": "codex"},
                                origin=origin)
        await asyncio.sleep(0)
    run(_rep({"session_id": "s"}))
    check("a repair of a shipped app is a one-shot (a manual Ship on a plan-less project too)",
          got == [True], got)
finally:
    t._run_phase_build, t._factory_build_task_start, _av._in_flight = _saved_av
tsrc = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
check("no origin carries a one_shot flag (repairs do not depend on who shipped)",
      '"one_shot"' not in tsrc
      and '"one_shot"' not in (ROOT / "brain/mastercode/runtime/app_verify.py").read_text())

print("== 9. a compare refuses one-engine args instead of dropping them ==")
_saved_r3 = rv.resolve_build_target
rv.resolve_build_target = _resolve
try:
    o9 = run(t._exec_build({"action": "run", "prompt": "p", "project_id": 9240, "model": "o6",
                            "variants": [{"harness": "codex"}]}, 3, _Pool(), "s", None))
finally:
    rv.resolve_build_target = _saved_r3
check("model= with variants is refused by name", o9.get("status") == "error"
      and "model" in o9.get("result", "") and "variants" in o9.get("result", ""), o9)

print("== 10. a session is over by silence, not by elapsed time ==")
# A session not held by this process is
# over only once it has written nothing for longer than any live run can be
# quiet; elapsed time since it started never decides it.
_now = time.time()


def _sdir(silent_s: float) -> Path:
    d = Path(tempfile.mkdtemp(prefix="sess-"))
    (d / "codex.events.jsonl").write_text("{}\n")
    t0 = _now - silent_s
    os.utime(d / "codex.events.jsonl", (t0, t0))
    os.utime(d, (t0, t0))
    return d


m4 = {"started_at": _now - 40 * 60, "max_minutes": 15,
      "variants": [{"vid": v, "harness": "codex"} for v in ("codex", "codex-s2", "codex-s3", "codex-s4")]}
m1 = {"started_at": _now - 40 * 60, "max_minutes": 15, "variants": [{"vid": "codex", "harness": "codex"}]}
check("four codex variants 40 min in, still writing: still live",
      rv._session_is_over(m4, _sdir(5)) is False)
check("one variant 40 min in, still writing: still live (no clock)",
      rv._session_is_over(m1, _sdir(5)) is False)
check("a session silent for 3 h: over (the safety net still works)",
      rv._session_is_over(m1, _sdir(3 * 3600)) is True)
m_acc = dict(m1, acceptance=[{"cmd": "a", "timeout_s": 300}, {"cmd": "b", "timeout_s": 300}])
check("declared checks extend how long a live session may be silent (35 min)",
      rv._session_is_over(m_acc, _sdir(35 * 60)) is False
      and rv._session_is_over(dict(m_acc, acceptance=[]), _sdir(35 * 60)) is True)

print("== 7. review.fan_out is gone ==")
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES  # noqa: E402
from mastercode.code_exec_guard import RETIRED_CODE_TOOLS, HOST_DOOR_TOOLS  # noqa: E402
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS  # noqa: E402
check("no schema", "review.fan_out" not in PUBLIC_TOOL_SCHEMAS)
check("not a meta tool name", "review.fan_out" not in PUBLIC_META_TOOL_NAMES)
check("no caps row", "review.fan_out" not in META_TOOL_CAPS)
check("retired in the guard, not a host door",
      "review.fan_out" in RETIRED_CODE_TOOLS and "review.fan_out" not in HOST_DOOR_TOOLS)
out8 = run(t._execute_meta_tool("review.fan_out", {"prompt": "p"}, 3, _Pool()))
check("a stray call is refused before anything runs", out8.get("status") in ("error", "denied"), out8)
b_enum = PUBLIC_TOOL_SCHEMAS["build"]["function"]["parameters"]["properties"]["action"]["enum"]
check("build offers action='run'", "run" in b_enum, b_enum)

print("== 10. runs started together are not refused by the one already running ==")
# entity-9003: four build(action='run') calls on project 9239 in one
# round; the first opened a build and the other three were refused because
# a build was already running. A run has its own worktree and merges on
# land, the same as a parallel phase, so a running build is no reason to
# refuse it.


class _BusyPool(_Pool):
    """A project with build 404 in flight on codex, plus the one just opened."""

    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        if "FROM projects WHERE id=$1 AND entity_id=$2" in sql:
            return 1
        if "status='in_progress'" in sql and "(result->>'project_id')::int" in sql:
            return 404
        if "COUNT(*) FROM project_phases" in sql:
            return 0
        return None


_saved10 = (rv.resolve_build_target, t._run_phase_build)
cap10: list = []


async def _cap_phase_build(**kw):
    cap10.append(kw)

rv.resolve_build_target = _resolve
t._run_phase_build = _cap_phase_build
try:
    async def _go10():
        out = await t._exec_build({"action": "run", "prompt": "review pass",
                                   "harness": "codex", "model": "gpt-5.6-luna"},
                                  3, _BusyPool(), "sess-1", "project:9239")
        await asyncio.sleep(0)
        return out
    o10 = run(_go10())
    check("a run while another build is in flight is started, not refused",
          o10.get("status") == "ok" and (o10.get("result") or {}).get("task_id") == 77, o10)
    check("...and it is detached as a one-shot (the wake sizes the pod)",
          len(cap10) == 1 and cap10[0].get("one_shot") is True
          and cap10[0].get("phase_id") is None, cap10)
    o10p = run(t._exec_build({"action": "plan", "project_id": 9239, "intent": "x"},
                             3, _BusyPool(), "s", None))
    check("re-planning mid-build is still refused",
          o10p.get("status") == "error" and "already running" in o10p.get("result", ""), o10p)
    o10c = run(t._exec_build({"action": "continue", "project_id": 9239, "message": "x"},
                             3, _BusyPool(), "s", None))
    check("a continue mid-build is still refused",
          o10c.get("status") == "error" and "already running" in o10c.get("result", ""), o10c)

    class _TwoRunning(_BusyPool):
        async def fetch(self, sql, *a):
            self.sql.append(sql)
            if "status='in_progress'" in sql and "(result->>'project_id')::int" in sql:
                return [{"task_id": 404}, {"task_id": 406}]
            return []
    o10s = run(t._exec_build({"action": "stop", "project_id": 9239}, 3, _TwoRunning(), "s", None))
    r10s = o10s.get("result") or {}
    check("stop names every build still running, not just the newest",
          r10s.get("running_task_ids") == [404, 406]
          and "Tasks 404, 406 are still running" in r10s.get("next_step", ""), o10s)
finally:
    rv.resolve_build_target, t._run_phase_build = _saved10

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
