#!/usr/bin/env python3
"""Gate: a phase build never waits on the forge; the forge's CI holds only the
automatic merge/install (pipeline build step 2, reduced 2026-10-01).

Drives tools._run_phase_build itself — no DB, no pod, no forge: fakes at the
seams (the pool, _pool_build, the save point, the forge poll) — and pins:

  1. a pipeline with a gitea-actions row does NOT make a phase build wait on
     the forge: no poll, one save point, the PR body is not refreshed later,
     and the phase lands 'complete'
  2. the outcome is landed or failed and nothing else: a failing check, a
     check the step-up does not cover, a stored pipeline that still lists the
     retired diff reviewer (no review hold is taken for it; removed
     2026-10-20), or a save point that
     did not reach the forge never turn a landed build into anything but
     'complete'; a crashed build is 'failed' and the seeded retry carries just
     the build's own failure text
  3. what the forge's CI still holds: _ci_merge_gate (the CI on the build PR's
     head) before an automatic merge/install, the head-pinned merge, and the
     Host's ship tool

Run: APP_ROOT=<repo> python3 scripts/test_forge_check_build.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[forge-check-build] ok   {label}")
    else:
        FAILS.append(label)
        print(f"[forge-check-build] FAIL {label}" + (f" — {detail}" if detail else ""))


ROW = {"provider": "gitea-actions", "required": True, "contexts": "check/gates",
       "wait_budget_s": 900}
PIPE = {"format": "privatae.pipeline/v1",
        "check": [{"provider": "harness-check"}, dict(ROW)]}
SHA = "b" * 40


class Pool:
    def __init__(self, pipe):
        self.events: list = []
        self.pipe = pipe

    async def fetchval(self, sql, *args):
        if "SELECT acceptance FROM project_phases" in sql:
            return json.dumps([{"cmd": "true", "expect_exit": 0, "timeout_s": 60}])
        if "SELECT config FROM projects" in sql:
            return json.dumps({"pipeline": self.pipe})
        return None

    async def fetchrow(self, sql, *args):
        if "phase_number, title FROM project_phases" in sql:
            return {"phase_number": 3, "title": "API"}
        return None

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        # Any write of projects.config.review_hold is recorded: since the
        # diff reviewer went (2026-10-20) a phase build never takes one.
        if "review_hold" in sql:
            self.events.append(("review_hold", sql))
        return "UPDATE 1"


ALL_PHASE_WRITES: list = []   # every phase status any scenario wrote


def scenario(*, forge=None, build_ok=True, approved=True, pushed=True, acc_ok=True,
             review=None, pipe=None):
    """Run one phase build; returns (events, phase status written)."""
    from mastercode.runtime import tools as t
    from mastercode.runtime import pipeline as pl
    from server.routes import forge_status as fs
    from server.routes import project_git as pg
    from server.routes import projects as pj
    pool = Pool(pipe or PIPE)
    ev = pool.events
    saved = {}

    def patch(obj, name, fn):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, fn)

    async def pool_build(**kw):
        ev.append(("pool_build",))
        if not build_ok:
            return {"status": "error", "result": "build crashed: boom"}
        return {"status": "ok", "result": {
            "acceptance": {"ran": True, "ok": acc_ok, "results": [{"ok": acc_ok}]},
            **({"review": review} if review else {}),
            "changed_files": ["api.py"], "run_id": 5}}

    async def sync(pool_, project_id, **kw):
        ev.append(("sync", kw.get("checks")))
        if not pushed:
            # What _sync_blocking returns when the push fails: committed,
            # with a commit hash, not pushed (project_git._sync_blocking).
            return {"synced": False, "committed": True, "pushed": False, "commit": SHA[:7],
                    "commit_sha": SHA, "error": "git push: refused"}
        return {"synced": True, "repo": "owner/app", "committed": True, "pushed": True,
                "commit": SHA[:7], "commit_sha": SHA, "pr": 4}

    async def wait(repo, sha, row, **kw):
        ev.append(("wait", repo, sha, row, kw.get("ignore_creators")))
        return forge(sha)

    async def load_approved(pool_, eid, pid, cfg):
        return [dict(ROW)] if approved else []

    async def set_status(pool_, phase_id, status):
        ev.append(("phase", status))

    async def noop(*a, **k):
        return None

    async def landed(*a, **k):
        return True

    async def nothing_next(*a, **k):
        return []

    patch(t, "_pool_build", pool_build)
    patch(t, "_factory_build_task_finish", landed)
    patch(t, "_set_phase_status", set_status)
    patch(t, "_set_phase_steps_status", noop)
    patch(t, "_ticket_phase_outcome", noop)
    patch(t, "_chain_stop", lambda *a, **k: _rec(ev, "chain_stop", a))
    patch(t, "_explain_idle", noop)
    patch(t, "_autoadvance_pick_next", nothing_next)
    async def retry_pick(**kw):
        ev.append(("retry_pick", kw.get("failure")))
        return None

    patch(t, "_phase_retry_pick", retry_pick)

    patch(t, "_append_build_activity", lambda p, tid, msg: _rec(ev, "activity", (msg,)))
    patch(t, "_get_runtime_dispatch", lambda: None)
    from server.routes import gitea_client as gc
    patch(gc, "gitea_creds", lambda: ("host-3", "tok", "git.invalid"))
    patch(pl, "load_approved", load_approved)
    patch(fs, "wait", wait)
    patch(pg, "sync_project_repo", sync)
    patch(pg, "prepare_workspace", lambda *a, **k: _ret({}))
    patch(pj, "ensure_project_repo", lambda *a, **k: _ret({"repo": "owner/app"}))
    try:
        asyncio.run(t._run_phase_build(
            entity_id=9003, pool=pool, session_id=None, task_id=77, project_id=9,
            phase_id=11, intent="build the API", research_id=None, timeout_s=600,
            harness="codex"))
    finally:
        for (obj, name), fn in saved.items():
            setattr(obj, name, fn)
    phases = [e[1] for e in ev if e[0] == "phase"]
    ALL_PHASE_WRITES.extend(phases)
    return ev, (phases[-1] if phases else None)


async def _ret(v):
    return v


async def _rec(ev, kind, args):
    ev.append((kind, *args))


def _idx(ev, kind):
    return next((i for i, e in enumerate(ev) if e[0] == kind), -1)


# ── the merge gate: the CI on the PR's CURRENT head ─────────────────────
HEAD = "c" * 40


def run_gate(*, pipe=PIPE, approved=True, prs=None, statuses=None, forge_error=None,
             cfg_raw=None, wait=False, seq=None, cfg_fails=0):
    """tools._ci_merge_gate against a fake project row and a fake forge."""
    import contextlib
    import types
    from mastercode.runtime import tools as t
    from mastercode.runtime import pipeline as pl
    from server.routes import project_forge as pf
    from server.routes import gitea_client as gc
    calls = []
    said = []
    fails = [cfg_fails]

    class P:
        async def fetchval(self, sql, *a):
            if fails[0] > 0:
                fails[0] -= 1
                raise RuntimeError("db blip")
            if cfg_raw is not None:
                return cfg_raw
            return json.dumps({"pipeline": pipe} if pipe is not None else {})

        async def fetchrow(self, sql, *a):
            return {"entity_id": 9003}

    async def req(method, path, params=None, **kw):
        calls.append((method, path, params))
        if forge_error:
            raise RuntimeError(forge_error)
        if path.endswith("/pulls"):
            return prs if prs is not None else [{"number": 4, "head": {"ref": "host/build", "sha": HEAD}}]
        if seq is not None:
            n = sum(1 for c in calls if c[1].endswith("/status"))
            return {"statuses": seq[min(n, len(seq)) - 1]}
        return {"statuses": statuses or []}

    @contextlib.asynccontextmanager
    async def open_forge(pool, eid, forge):
        yield types.SimpleNamespace(request=req)

    async def project_forge(pool, pid):
        return types.SimpleNamespace(repo="owner/app", on_machine=False)

    async def load_approved(pool, eid, pid, cfg):
        from mastercode.runtime import pipeline as _p
        norm, _ = _p.validate(json.loads(await P().fetchval("SELECT config")).get("pipeline") or {})
        return _p._check_rows_everywhere(norm or {}) if approved else []
    saved = [(pf, "open_forge", pf.open_forge), (pf, "project_forge", pf.project_forge),
             (pl, "load_approved", pl.load_approved), (gc, "gitea_creds", gc.gitea_creds)]
    pf.open_forge, pf.project_forge, pl.load_approved = open_forge, project_forge, load_approved
    gc.gitea_creds = lambda: ("host-3", "tok", "git.invalid")
    from server.routes import forge_status as fs
    import functools
    saved.append((fs, "wait", fs.wait))
    clock = [0.0]

    async def fake_sleep(sec):
        clock[0] += sec
    # The CI wait runs on a fake clock: a budget runs out at once, never in
    # real minutes.
    fs.wait = functools.partial(fs.wait, poll_s=5.0, sleep=fake_sleep, clock=lambda: clock[0])

    async def on_wait(msg):
        said.append(msg)
    try:
        if wait:
            return asyncio.run(t._ci_merge_gate(P(), 9, wait=True, on_wait=on_wait)), (calls, said)
        return asyncio.run(t._ci_merge_gate(P(), 9)), calls
    finally:
        for obj, name, fn in saved:
            setattr(obj, name, fn)


def gate_tests():
    def st(state, who="gitea-actions"):
        return [{"context": "check/gates", "status": state, "creator": {"login": who},
                 "updated_at": "2026-09-25T10:00:00Z"}]
    (r, sha), calls = run_gate(statuses=st("success"))
    check("gate: the PR head's statuses passed → go, naming the head it checked",
          r is None and sha == HEAD
          and any(p == f"/api/v1/repos/owner/app/commits/{HEAD}/status" for _m, p, _q in calls), f"{r} {calls}")
    (r, sha), _ = run_gate(statuses=st("pending"))
    check("gate: the head is still pending → held, saying so",
          r and "has not yet passed the build pull request's latest commit (ccccccc" in r and sha == HEAD, r)
    (r, _), _ = run_gate(statuses=st("failure"))
    check("gate: the head failed → held", r and "CI failed the build pull request's latest commit" in r, r)
    (r, _), _ = run_gate(statuses=st("success", who="host-3"))
    check("gate: a pass the Host's own account posted does not count", r is not None, r)
    (r, sha), calls = run_gate(pipe={"format": "privatae.pipeline/v1", "check": [{"provider": "harness-check"}]})
    check("gate: no gitea-actions row → go, and the forge is never asked", r is None and not calls, f"{r} {calls}")
    (r, sha), _ = run_gate(pipe=None)
    check("gate: no pipeline setting → go", r is None)
    (r, _), calls = run_gate(approved=False, statuses=st("success"))
    check("gate: a CI row the step-up does not cover → held, named", r and "authenticator code" in r and not calls, r)
    (r, _), _ = run_gate(cfg_raw=json.dumps({"pipeline": {"format": "privatae.pipeline/v1",
                                                         "check": [{"provider": "gitea-actions"}]}}))
    check("gate: a setting naming the CI that no longer validates → held", r and "no longer validates" in r, r)
    (r, _), _ = run_gate(forge_error="HTTP 502")
    check("gate: the forge cannot be read → held (never an unchecked install)",
          r and "could not be read" in r, r)
    (r, sha), _ = run_gate(prs=[])
    check("gate: no open build PR → go (nothing unmerged to judge)", r is None and sha is None)
    (r, sha), (calls, said) = run_gate(wait=True, seq=[st("pending"), st("pending"), st("success")])
    check("gate(wait): a head the CI has not answered on yet is waited for, then passes",
          r is None and sha == HEAD and len([c for c in calls if c[1].endswith("/status")]) == 3
          and said and "waiting for the forge's CI on the build pull request's latest commit ccccccc" in said[0],
          f"{r} {calls} {said}")
    (r, sha), _ = run_gate(wait=True, seq=[st("pending"), st("failure")])
    check("gate(wait): …and a failure while waiting holds", r and "CI failed" in r, r)
    two = {**PIPE, "modules": {"api": {"check": [{"provider": "gitea-actions",
                                                  "contexts": "check/api", "wait_budget_s": 900}]}}}
    (r, sha), (calls, _) = run_gate(pipe=two, wait=True, seq=[st("failure")])
    check("gate(wait): with two CI rows, the first that fails ends the wait (one deadline, "
          "never the sum of budgets)",
          r and "CI failed" in r and len([c for c in calls if c[1].endswith("/status")]) == 1,
          f"{r} {calls}")
    (r, sha), _ = run_gate(statuses=st("success"), cfg_fails=1)
    check("gate: one failed read of the setting is retried", r is None and sha == HEAD, r)
    (r, sha), _ = run_gate(statuses=st("success"), cfg_fails=2)
    check("gate: a setting unreadable twice holds (whether the CI gates it is what is unknown)",
          r and "could not be read" in r, r)


def merge_pin_tests():
    import types
    from server.routes import project_git as pg
    posts = []

    def sess(head):
        async def req(method, path, params=None, json=None, **kw):
            if method == "POST":
                posts.append(json)
                return {}
            if path.endswith("/pulls"):
                return [{"number": 4, "head": {"ref": "host/build", "sha": head}, "body": "",
                         "mergeable": True}]
            return {}
        return types.SimpleNamespace(request=req, forge=types.SimpleNamespace(repo="owner/app"),
                                     pr_url=lambda pr: None)
    r = asyncio.run(pg._merge_rolling_pr(sess("d" * 40), 9, HEAD))
    check("merge: a head that moved after the CI's check is refused, head_moved, nothing posted",
          r.get("merged") is False and r.get("head_moved") is True and not posts, f"{r} {posts}")
    r = asyncio.run(pg._merge_rolling_pr(sess(HEAD), 9, HEAD))
    check("merge: the checked head merges, pinned for the forge too (head_commit_id)",
          r.get("merged") is True and posts == [{"Do": "merge", "head_commit_id": HEAD}], f"{r} {posts}")
    posts.clear()
    asyncio.run(pg._merge_rolling_pr(sess(HEAD), 9))
    check("merge: without a CI gate, the merge is as before", posts == [{"Do": "merge"}], f"{posts}")


def auto_deliver_tests():
    """A follow-up run with auto-deliver ON: the gate decides merge AND install."""
    from mastercode.runtime import tools as t
    from server.routes import project_git as pg
    from mastercode.runtime import app_verify as av

    def go(gate, merged=None, merge_raises=False, release_fails=False):
        ev = []

        class P(Pool):
            async def fetchval(self, sql, *args):
                if "auto_merge" in sql:
                    return True
                return await super().fetchval(sql, *args)
        pool = P({"format": "privatae.pipeline/v1", "check": [{"provider": "harness-check"}]})

        async def execute(sql, *args):
            if "deliver_after" in sql:
                if release_fails and "|| $1::jsonb" in sql and "- 'deliver_after'" in sql:
                    ev.append(("release_failed",))
                    raise RuntimeError("db blip on the carry write")
                ev.append(("release" if "- 'deliver_after'" in sql else "stamp",))
            return "UPDATE 1"
        pool.execute = execute
        saved = {}

        def patch(obj, name, fn):
            saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, fn)

        async def pool_build(**kw):
            return {"status": "ok", "result": {"changed_files": ["a.py"]}}

        async def sync(pool_, pid, **kw):
            return {"synced": True, "pushed": True, "commit_sha": HEAD, "pr": 4, "repo": "owner/app"}

        async def gate_fn(pool_, pid, **kw):
            ev.append(("gate", kw.get("wait", False)))
            if merge_raises and kw.get("wait"):
                # Stands for anything in the step raising before the CI's
                # answer is in (the plan end's last commit, the wait itself).
                raise RuntimeError("the step raised before the CI answered")
            return gate

        async def merge(pool_, pid, expect_head=None):
            ev.append(("merge", expect_head))
            return merged or {"merged": True}

        async def ship(**kw):
            ev.append(("ship",))
            return {"shipped": True}

        async def targets(*a, **k):
            return [(None, "app")]

        async def ret(v=None, *a, **k):
            return v
        patch(t, "_pool_build", pool_build)
        async def finish(*a, **k):
            ev.append(("finish",))
            return True
        patch(t, "_factory_build_task_finish", finish)
        patch(t, "_ci_delivery_budget", lambda *a, **k: ret(900 if gate != (None, None) else 0))
        patch(t, "_ci_merge_gate", gate_fn)
        patch(t, "_active_review_hold", lambda *a, **k: ret(None))
        patch(t, "_delivery_targets", targets)
        patch(t, "_append_build_activity", lambda *a, **k: ret())
        patch(t, "_get_runtime_dispatch", lambda: None)
        patch(pg, "sync_project_repo", sync)
        patch(pg, "merge_project_pr", merge)
        patch(pg, "prepare_workspace", lambda *a, **k: ret({}))
        from server.routes import projects as pj
        patch(pj, "ensure_project_repo", lambda *a, **k: ret({"repo": "owner/app"}))
        patch(av, "ship_and_verify", ship)
        try:
            asyncio.run(t._run_phase_build(
                entity_id=9003, pool=pool, session_id=None, task_id=78, project_id=9,
                phase_id=None, intent="follow-up", research_id=None, timeout_s=600,
                harness="codex"))
        finally:
            for (obj, name), fn in saved.items():
                setattr(obj, name, fn)
        return ev
    ev = go(("the forge's CI has not yet passed …", HEAD))
    check("auto-deliver: the CI has not passed the PR head → neither merged nor installed",
          ("gate", True) in ev and not any(e[0] in ("merge", "ship") for e in ev), f"{ev}")
    ev = go((None, HEAD))
    check("auto-deliver: asks the CI WAITING (its own save point is usually the head), then "
          "merges pinned to the head it checked, then installs",
          ("merge", HEAD) in ev and ("ship",) in ev
          and ev.index(("gate", True)) < ev.index(("merge", HEAD)), f"{ev}")
    ev = go((None, HEAD), merged={"merged": False, "head_moved": True, "reason": "newer"})
    check("auto-deliver: the head moved before the merge → not installed either",
          ("merge", HEAD) in ev and ("ship",) not in ev, f"{ev}")
    ev = go((None, None))
    check("auto-deliver: no CI in the pipeline → merged and installed as before (unpinned)",
          ("merge", None) in ev and ("ship",) in ev, f"{ev}")
    check("…and its report is never held (no CI to wait on)", ("stamp",) not in ev, f"{ev}")
    ev = go((None, HEAD))
    order = [e for e in ev if e in (("stamp",), ("finish",), ("gate", True), ("merge", HEAD),
                                    ("ship",), ("release",))]
    check("auto-deliver with a CI to wait on: the report is held BEFORE the row turns terminal, "
          "renewed before the CI wait, the merge and the install, released only after both",
          order == [("stamp",), ("finish",), ("stamp",), ("gate", True), ("stamp",), ("merge", HEAD),
                    ("stamp",), ("ship",), ("release",)], f"{ev}")
    ev = go((None, HEAD), release_fails=True)
    check("…a failed release write is retried on its own, so the report is not held to expiry",
          ("release_failed",) in ev and ("release",) in ev[ev.index(("release_failed",)):], f"{ev}")
    ev = go(("held: CI failed", HEAD), merged=None, merge_raises=True)
    check("auto-deliver: the merge step raises before the CI was asked → the CI is asked "
          "(no wait) and holds the install",
          ("gate", False) in ev and ("ship",) not in ev and not any(e[0] == "merge" for e in ev), f"{ev}")
    ev = go((None, None), merge_raises=True)
    check("…and with no CI in the pipeline the tree still installs after a raise, as before",
          ("gate", False) in ev and ("ship",) in ev, f"{ev}")


def main() -> int:
    def never(sha):
        raise AssertionError("the forge was asked")

    ev, status = scenario(forge=never)
    syncs = [e for e in ev if e[0] == "sync"]
    check("a phase with a gitea-actions row lands complete", status == "complete", f"{status} {ev}")
    check("...without waiting on the forge at all", _idx(ev, "wait") == -1, f"{ev}")
    check("...the save point is pushed once, after the build",
          len(syncs) == 1 and _idx(ev, "pool_build") < _idx(ev, "sync"), f"{ev}")
    from server.routes import project_git as _pg
    check("...and nothing rewrites the PR body afterwards (refresh_pr_checks is gone)",
          not hasattr(_pg, "refresh_pr_checks"))
    check("...and the status is written before the push (no early save point)",
          next(i for i, e in enumerate(ev) if e[0] == "phase" and e[1] == "complete")
          < _idx(ev, "sync"), f"{ev}")
    check("the forge row is left out of the phase's PR-body checks (it was not asked, "
          "and is never reported as 'not run')",
          not [c for c in syncs[0][1] if c["provider"] == "gitea-actions"]
          and any(c["provider"] == "harness-check" for c in syncs[0][1]), f"{syncs[0][1]}")
    check("no activity line says the build is waiting for the forge",
          not any(e[0] == "activity" and "waiting for the forge" in e[1] for e in ev), f"{ev}")
    check("no hold is taken for the CI (every automatic merge asks the CI about the PR head)",
          _idx(ev, "review_hold") == -1, f"{ev}")

    ev, status = scenario(forge=never, approved=False)
    check("a row the step-up does not cover changes nothing: complete, no wait, no chain stop",
          status == "complete" and _idx(ev, "wait") == -1 and _idx(ev, "chain_stop") == -1
          and len([e for e in ev if e[0] == "sync"]) == 1, f"{status} {ev}")

    ev, status = scenario(forge=never, build_ok=False)
    seeds = [e[1] for e in ev if e[0] == "retry_pick"]
    check("a crashed build → failed, never waited on, no push", status == "failed"
          and _idx(ev, "wait") == -1 and _idx(ev, "sync") == -1, f"{status} {ev}")
    check("...and the seeded retry is told only the build's own failure",
          seeds and seeds[0] == "build crashed: boom", f"{seeds}")

    ev, status = scenario(forge=never, acc_ok=False)
    check("the platform's own check failing never fails a landed build: complete, pushed once",
          status == "complete" and _idx(ev, "wait") == -1
          and len([e for e in ev if e[0] == "sync"]) == 1 and _idx(ev, "retry_pick") == -1,
          f"{status} {ev}")
    check("...and the failed check rides the PR body as information",
          any(c["provider"] == "harness-check" and c["ok"] is False
              for c in [e for e in ev if e[0] == "sync"][0][1]), f"{ev}")

    # A project whose stored pipeline still lists the retired diff reviewer,
    # and a build result still carrying a blocked verdict of the old shape
    # (a pod image from before 2026-10-20): the row is dropped, nothing is
    # held, and the phase is complete.
    blocked = {"ran": True, "ok": False, "blocking": [{"file": "a.py", "line": 1, "issue": "x"}]}
    pipe_rev = {**PIPE, "check": PIPE["check"] + [{"provider": "diff-review"}]}
    ev, status = scenario(forge=never, review=blocked, pipe=pipe_rev)
    syncs = [e for e in ev if e[0] == "sync"]
    check("a stored pipeline listing the retired diff-review: complete, pushed once, "
          "no review hold written",
          status == "complete" and len(syncs) == 1 and _idx(ev, "review_hold") == -1,
          f"{status} {ev}")
    check("...and the retired row is not in the PR-body checks",
          syncs and not [c for c in syncs[0][1] if c["provider"] == "diff-review"]
          and any(c["provider"] == "harness-check" for c in syncs[0][1]),
          f"{syncs[0][1] if syncs else ev}")

    ev, status = scenario(forge=never, pushed=False)
    check("a save point that did not reach the forge does not change the outcome: complete",
          status == "complete" and _idx(ev, "wait") == -1 and _idx(ev, "chain_stop") == -1,
          f"{status} {ev}")
    check("'review' is never written as a phase status in any scenario above",
          ALL_PHASE_WRITES and "review" not in ALL_PHASE_WRITES, str(ALL_PHASE_WRITES))

    tsrc = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
    ship = tsrc[tsrc.index("async def _exec_ship_project_app"):tsrc.index("async def _exec_artifact")]
    check("the Host's ship tool refuses a tree the CI has not passed, before it installs",
          "_ci_held, _ = await _ci_merge_gate(pool, project_id)" in ship
          and ship.index("_ci_merge_gate(") < ship.index("await ship_project_service("))
    gate_tests()
    merge_pin_tests()
    auto_deliver_tests()
    print(f"\n[forge-check-build] {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
