"""A build's report says what auto-deliver actually did, and auto-deliver
never merges or ships a change the diff reviewer blocked.

The reviewer BLOCKED a follow-up
build ("the diff touches an entirely different application"), auto-deliver
merged and shipped it anyway (a follow-up has no phase to hold in 'review'),
and the report — built from the pre-ship app snapshot, with no auto-deliver
fields on the record — told the user the app was not installed and "the phase
stays in review".

AST/source checks plus the pure helper, so this runs outside the image.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


session = open(os.path.join(ROOT, "brain/mastercode/runtime/session.py")).read()
tree = ast.parse(session)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_auto_deliver_facts")
ns: dict = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "session.py", "exec"), ns)
facts = ns["_auto_deliver_facts"]

# A build task's record, as it was stored.
t9404 = {"auto_merge": {"pr": 1, "merged": True},
        "auto_ship": {"app_id": "document-workspace", "shipped": True, "verify_task_id": 405}}
f = facts(t9404)
check("merged + shipped is stated, with the PR and the app",
      "merged Code Approval #1 into the project's Code Repository" in f
      and "installed document-workspace" in f and "arrives in this thread as its own message" in f, f)
held = {"auto_merge": {"merged": False, "held": "the code review blocked this change, so it waits"},
        "auto_ship": {"shipped": False, "reason": "the code review blocked this change, so it waits"}}
f = facts(held)
check("a held delivery says so once, with the reason",
      f.count("code review blocked") == 1 and f.startswith("Auto-Approve was held:"), f)
f = facts({"auto_merge": {"merged": False, "pr": 3, "reason": "PR is not mergeable (conflict) — left open"},
           "auto_ship": {"shipped": False, "reason": "no installable module has a version yet"}})
check("a refused merge and an empty ship each give their own reason",
      "did NOT merge Code Approval #3: PR is not mergeable" in f and "did not install anything: no installable" in f, f)
check("auto-deliver off: nothing to say", facts({}) == "")
check("fact lines are facts, not prohibitions", "Do not" not in facts(t9404) + facts(held))

# The stale snapshot is not stated once the app shipped.
i = session.index("_ship = record.get(\"auto_ship\")")
blk = session[i:i + 600]
check("the pre-ship app snapshot is skipped once auto-deliver shipped",
      "if not _ship.get(\"shipped\"):" in blk and blk.index("if not _ship") < blk.index("_describe_app"))
check("the build facts carry the auto-deliver lines", "_facts += _auto_deliver_facts(record)" in session)

disp = open(os.path.join(ROOT, "brain/mastercode/runtime/agent_dispatch.py")).read()
d = disp[disp.index("def delivery_record"):disp.index("class AgentDispatch")]
check("delivery_record carries auto_merge and auto_ship by name",
      '"auto_merge": res.get("auto_merge")' in d and '"auto_ship": res.get("auto_ship")' in d)

f = facts({"auto_ship": {"app_id": "x", "shipped": True, "reason": "installed id unknown — no proof started"}})
check("no proof promised when none was started",
      "proving" not in f and "no proof that it runs was started (installed id unknown" in f, f)

tools = open(os.path.join(ROOT, "brain/mastercode/runtime/tools.py")).read()
g = tools.index("if _hold_blocked and (_sync or {}).get(\"pr\") is not None:")
m = tools.index("from server.routes.project_git import merge_project_pr", g)
gate = tools[g:m]
pre = tools.index("_hold_blocked = _review_blocks(_pipe, _pipeline.diff_review_verdict(_rev_env))")
push = tools.index("_label, _sync = await _save_point(_checks)", pre)
check("a blocked landing is held BEFORE its commit is pushed to the build PR",
      "_hold_written = await _set_review_hold(pool, project_id, task_id, None)" in tools[pre:push])
# A phase never pushes an early save point or waits for the forge; the forge's
# CI is asked only by _ci_merge_gate before an automatic merge/install.
check("a phase has no early save point or forge wait (the hold has one push to precede)",
      "# FORGE-SIDE CHECKS" not in tools and "_early = await _save_point(" not in tools
      and "_forge_fail_text" not in tools)
check("...while the automatic merge still asks the forge's CI first (waiting), pinned to its head",
      "_ci_held, _ci_head = await _ci_merge_gate(" in tools
      and "pool, project_id, wait=True, on_wait=_activity)" in tools
      and tools.index("_ci_held, _ci_head = await _ci_merge_gate(")
      < tools.index("await merge_project_pr(pool, project_id, expect_head=_ci_head)"))
gate = tools[tools.index("if _hold_blocked and (_sync or {}).get(\"pr\") is not None:"):m]
check("every auto-deliver consults the standing hold BEFORE the merge, and a failed write still holds",
      "_hold = await _active_review_hold(pool, project_id)" in gate and "_auto_deliver = False" in gate
      and "if _hold is None and _hold_blocked:" in gate
      and 'out["result"]["auto_merge"] = {"merged": False, "held": _held}' in gate)
ship = tools[tools.index("async def _exec_ship_project_app"):tools.index("async def _exec_artifact")]
check("the host's ship tool refuses while a hold stands",
      "_hold = await _active_review_hold(pool, project_id)" in ship
      and ship.index("_active_review_hold") < ship.index("await ship_project_service("))

# ── the hold itself, run against an in-memory project row and a fake gitea ──
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import types  # noqa: E402

ttree = ast.parse(tools)
hs = [n for n in ttree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
      and n.name in ("_set_review_hold", "_active_review_hold", "_review_hold_reason", "_review_blocks")]
tns = {"json": json, "log": logging.getLogger("t")}
exec(compile(ast.Module(body=hs, type_ignores=[]), "tools.py", "exec"), tns)

PRS = {}          # number -> {"state", "merged", "head": {"ref"}}


async def gitea_request(method, path, params=None, json=None):
    if path.endswith("/pulls"):
        return [p for p in PRS.values() if p["state"] == "open"]
    return PRS.get(int(path.rsplit("/", 1)[1]))

sys.modules["server"] = types.ModuleType("server")
sys.modules["server.routes"] = types.ModuleType("server.routes")
sys.modules["server.routes.gitea_client"] = types.SimpleNamespace(
    gitea_request=gitea_request, split_repo=lambda r: r.split("/", 1))
# The real lookup the hold uses (project_git.find_work_pr / remote_work_branch),
# lifted from the source so the stub cannot drift from it.
_pg_src = open(os.path.join(ROOT, "server/routes/project_git.py")).read()
_pg_tree = ast.parse(_pg_src)
_pg_nodes = [n for n in _pg_tree.body
             if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in ("work_branch_of", "project_work_branch", "same_repo_pr", "find_work_pr"))
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("_WORK_BRANCH", "_PR_LIST_PAGES",
                                                                             "_BRANCH_RE")
                                                   for t in n.targets))]
import re as _re  # noqa: E402
_pg_ns: dict = {"re": _re}
exec(compile(ast.Module(body=_pg_nodes, type_ignores=[]), "project_git.py", "exec"), _pg_ns)
sys.modules["server.routes.project_git"] = types.SimpleNamespace(
    _WORK_BRANCH=_pg_ns["_WORK_BRANCH"], find_work_pr=_pg_ns["find_work_pr"],
    work_branch_of=_pg_ns["work_branch_of"], project_work_branch=_pg_ns["project_work_branch"])


# The review hold asks the project's forge (project_forge) where its repo
# lives; here it is always the platform forge, answered by the stub above.
class _Sess:
    def __init__(self, forge):
        self.forge = forge

    async def request(self, *a, **k):
        return await sys.modules["server.routes.gitea_client"].gitea_request(*a, **k)


async def _project_forge(pool, pid):
    row = await pool.fetchrow("SELECT repo", pid)
    return types.SimpleNamespace(on_machine=False, repo=row["repo"])


@contextlib.asynccontextmanager
async def _open_forge(pool, eid, forge):
    yield _Sess(forge)

sys.modules["server.routes.project_forge"] = types.SimpleNamespace(
    project_forge=_project_forge, open_forge=_open_forge)


class Pool:
    def __init__(self):
        self.config, self.fail = {}, False

    async def execute(self, sql, pid, *a):
        if self.fail:
            raise RuntimeError("db down")
        if "jsonb_set(COALESCE" in sql:          # _set_review_hold
            new, pr = json.loads(a[0]), a[1]
            cur = self.config.get("review_hold")
            if cur is None or pr is None:
                self.config["review_hold"] = new
            elif cur.get("pr") is None:
                cur["pr"] = pr
            elif cur.get("pr") != pr:
                self.config["review_hold"] = new
        elif "- 'review_hold'" in sql:
            self.config.pop("review_hold", None)
        elif "jsonb_set(config" in sql:
            self.config["review_hold"] = json.loads(a[0])

    async def fetchval(self, sql, pid):
        if self.fail:
            raise RuntimeError("db down")
        return json.dumps(self.config)

    async def fetchrow(self, sql, pid):
        if self.fail:
            raise RuntimeError("db down")
        return {"repo": "entity-9003/p9239", "entity_id": 9003,
                "hold": json.dumps(self.config.get("review_hold"))
                if "review_hold" in self.config else None}


async def story():
    pool, out = Pool(), []
    PRS[1] = {"number": 1, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    await tns["_set_review_hold"](pool, 9239, 9404, None)                # before the push
    await tns["_set_review_hold"](pool, 9239, 9404, 1)                   # PR known after it
    out.append(await tns["_active_review_hold"](pool, 9239))            # held on #1
    await tns["_set_review_hold"](pool, 9239, 407, None)               # a later block, same PR
    await tns["_set_review_hold"](pool, 9239, 407, 1)
    out.append(dict(pool.config["review_hold"]))                        # names the newest
    PRS[1].update(state="closed")                                       # closed, NOT merged
    PRS[2] = {"number": 2, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    out.append(await tns["_active_review_hold"](pool, 9239))            # moves to #2
    PRS[2].update(state="closed", merged=True)                          # the user merged it
    out.append(await tns["_active_review_hold"](pool, 9239))            # cleared
    out.append(pool.config.get("review_hold"))
    return out

held1, kept, moved, after_merge, cfg = asyncio.run(story())
check("a blocked landing holds every later auto-deliver while its PR is open",
      held1 == {"task_id": 9404, "pr": 1}, str(held1))
check("a second block on the same PR is the one named, still on that PR", kept == {"task_id": 407, "pr": 1}, str(kept))
check("closing the PR unmerged does not release it: the hold follows the open build PR",
      moved == {"task_id": 407, "pr": 2}, str(moved))
check("the user merging the PR releases the hold and clears it",
      after_merge is None and cfg is None, f"{after_merge} {cfg}")


async def outage():
    pool = Pool()
    pool.config["review_hold"] = {"task_id": 9, "pr": 5}

    async def boom(*a, **k):
        raise RuntimeError("gitea down")
    sys.modules["server.routes.gitea_client"].gitea_request = boom
    try:
        return await tns["_active_review_hold"](pool, 1)
    finally:
        sys.modules["server.routes.gitea_client"].gitea_request = gitea_request

async def db_down():
    pool = Pool()
    pool.fail = True
    wrote = await tns["_set_review_hold"](pool, 1, 9, None)
    return wrote, await tns["_active_review_hold"](pool, 1)

wrote, h = asyncio.run(db_down())
check("a database error fails closed: the write reports failure, the read holds",
      wrote is False and h and h.get("unreadable") and "could not be read" in tns["_review_hold_reason"](h), str(h))
blk = tns["_review_blocks"]
check("the hold counts the review exactly as pipeline.decide does",
      blk({"check": [{"provider": "diff-review", "required": True}]}, {"ran": True, "ok": False})
      and not blk({"check": [{"provider": "diff-review", "required": False}]}, {"ran": True, "ok": False})
      and not blk({"check": [{"provider": "harness-check"}]}, {"ran": True, "ok": False})
      and not blk({"check": [{"provider": "diff-review"}]}, {"ran": False, "ok": None}))

async def merged_then_blocked_again():
    # The user merges held pull request 1 by hand; before anything
    # re-reads the hold, follow-up 410 is blocked too and lands on pull request 2.
    PRS.clear()
    pool = Pool()
    PRS[1] = {"number": 1, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    await tns["_set_review_hold"](pool, 9239, 9404, None)
    await tns["_set_review_hold"](pool, 9239, 9404, 1)
    PRS[1].update(state="closed", merged=True)                          # the user accepted 9404
    PRS[2] = {"number": 2, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    await tns["_set_review_hold"](pool, 9239, 410, None)                # 410 blocked, pre-push
    await tns["_set_review_hold"](pool, 9239, 410, 2)                   # PR known
    return dict(pool.config["review_hold"]), await tns["_active_review_hold"](pool, 9239)

stored, seen = asyncio.run(merged_then_blocked_again())
check("a stale hold on a merged PR never absorbs a new block: 410 holds on #2",
      stored == {"task_id": 410, "pr": 2} and seen == {"task_id": 410, "pr": 2}, f"{stored} {seen}")

check("an unreadable PR keeps the hold (never ships blocked code on a gitea error)",
      asyncio.run(outage()) == {"task_id": 9, "pr": 5})
r = tns["_review_hold_reason"]({"task_id": 9404, "pr": 1})
check("the held sentence names the task, the PR, and what releases it",
      "build task 9404" in r and "pull request #1" in r and "merge that pull request yourself" in r
      and "ask me to undo it first; merging still releases it" in r and "close it" not in r, r)
f = facts({"auto_merge": {"merged": False, "held": r}, "auto_ship": {"shipped": False, "reason": r}})
check("the held line is not doubled", f.count("Nothing") == 1, f)

async def stale_then_prless_block():
    # A leftover hold on merged pull request 1, a new block whose sync
    # returned no PR, auto-deliver off. The new block must still be recorded.
    PRS.clear()
    pool = Pool()
    PRS[1] = {"number": 1, "state": "closed", "merged": True, "head": {"ref": "host/build"}}
    pool.config["review_hold"] = {"task_id": 9404, "pr": 1}
    await tns["_set_review_hold"](pool, 9239, 420, None)                # sync gave no PR
    PRS[2] = {"number": 2, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    return dict(pool.config["review_hold"]), await tns["_active_review_hold"](pool, 9239)

stored, seen = asyncio.run(stale_then_prless_block())
check("a new block with no PR replaces a leftover hold and then finds the open PR",
      stored == {"task_id": 420, "pr": None} and seen == {"task_id": 420, "pr": 2}, f"{stored} {seen}")
print("all gates passed" if not failures else f"{len(failures)} failing")
sys.exit(1 if failures else 0)
