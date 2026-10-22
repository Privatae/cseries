"""A build's report says what auto-deliver actually did, and auto-deliver
never merges or ships a change a standing review hold covers.

The reviewer BLOCKED a follow-up
build ("the diff touches an entirely different application"), auto-deliver
merged and shipped it anyway (a follow-up has no phase to hold in 'review'),
and the report — built from the pre-ship app snapshot, with no auto-deliver
fields on the record — told the user the app was not installed and "the phase
stays in review".

The diff reviewer is gone (the Host reviews in its own turn),
so nothing records a new hold. A hold already stored on a project
(projects.config.review_hold) is still honoured by auto-deliver and the install
door until the person merges its PR; that is what the second half pins.

AST/source checks plus the pure helper, so this runs outside the image.
"""
import ast
import os
import sys
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


session = turn_source(ROOT)
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
# No code records a new hold: the reviewer and its writer are gone.
check("no new review hold is ever recorded (the reviewer and its writer are gone)",
      "_set_review_hold" not in tools and "_review_blocks" not in tools
      and "diff_review_verdict" not in tools and "'{review_hold}', CASE" not in tools)
g = tools.index("_hold = await _active_review_hold(pool, project_id)\n            if _hold is not None:\n"
                "                _auto_deliver = False")
m = tools.index("from server.routes.project_git import merge_project_pr", g)
gate = tools[g:m]
# A phase never pushes an early save point or waits for the forge; the forge's
# CI is asked only by _ci_merge_gate before an automatic merge/install.
check("a phase has no early save point or forge wait",
      "# FORGE-SIDE CHECKS" not in tools and "_early = await _save_point(" not in tools
      and "_forge_fail_text" not in tools)
check("...while the automatic merge still asks the forge's CI first (waiting), pinned to its head",
      "_ci_held, _ci_head = await _ci_merge_gate(" in tools
      and "pool, project_id, wait=True, on_wait=_activity)" in tools
      and tools.index("_ci_held, _ci_head = await _ci_merge_gate(")
      < tools.index("await merge_project_pr(pool, project_id, expect_head=_ci_head)"))
check("every auto-deliver consults a STORED hold BEFORE the merge, and states it on the record",
      "_auto_deliver = False" in gate and "_held = _review_hold_reason(_hold)" in gate
      and 'out["result"]["auto_merge"] = {"merged": False, "held": _held}' in gate
      and 'out["result"]["auto_ship"] = {"shipped": False, "reason": _held}' in gate
      and tools.rindex("if _auto_deliver:", 0, g) > tools.rindex("_save_point(_checks)", 0, g))
ship = tools[tools.index("async def _exec_ship_project_app"):tools.index("async def _exec_artifact")]
check("the host's ship tool refuses while a hold stands",
      "_hold = await _active_review_hold(pool, project_id)" in ship
      and ship.index("_active_review_hold") < ship.index("await ship_project_service("))

# ── a stored hold, run against an in-memory project row and a fake gitea ──
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import types  # noqa: E402

ttree = ast.parse(tools)
hs = [n for n in ttree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
      and n.name in ("_active_review_hold", "_review_hold_reason")]
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
    def __init__(self, hold=None):
        self.config, self.fail = {}, False
        if hold is not None:
            self.config["review_hold"] = hold

    async def execute(self, sql, pid, *a):
        if self.fail:
            raise RuntimeError("db down")
        if "- 'review_hold'" in sql:
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
    # A hold stored while the reviewer could still block, on open pull request 1.
    PRS.clear()
    pool, out = Pool({"task_id": 9404, "pr": 1}), []
    PRS[1] = {"number": 1, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    out.append(await tns["_active_review_hold"](pool, 9239))            # held on #1
    PRS[1].update(state="closed")                                       # closed, NOT merged
    PRS[2] = {"number": 2, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    out.append(await tns["_active_review_hold"](pool, 9239))            # moves to #2
    PRS[2].update(state="closed", merged=True)                          # the user merged it
    out.append(await tns["_active_review_hold"](pool, 9239))            # cleared
    out.append(pool.config.get("review_hold"))
    return out

held1, moved, after_merge, cfg = asyncio.run(story())
check("a stored hold holds every later auto-deliver while its PR is open",
      held1 == {"task_id": 9404, "pr": 1}, str(held1))
check("closing the PR unmerged does not release it: the hold follows the open build PR",
      moved == {"task_id": 9404, "pr": 2}, str(moved))
check("the user merging the PR releases the hold and clears it",
      after_merge is None and cfg is None, f"{after_merge} {cfg}")


async def no_hold():
    PRS.clear()
    PRS[1] = {"number": 1, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    return await tns["_active_review_hold"](Pool(), 9239)

check("a project with no stored hold is not held", asyncio.run(no_hold()) is None)


async def prless_hold():
    # A hold stored before its PR was known: it finds the open build PR.
    PRS.clear()
    pool = Pool({"task_id": 420, "pr": None})
    PRS[2] = {"number": 2, "state": "open", "merged": False, "head": {"ref": "host/build"}}
    return await tns["_active_review_hold"](pool, 9239), dict(pool.config["review_hold"])

seen, stored = asyncio.run(prless_hold())
check("a stored hold with no PR finds the open build PR and keeps holding",
      seen == {"task_id": 420, "pr": 2} and stored == {"task_id": 420, "pr": 2}, f"{seen} {stored}")


async def outage():
    pool = Pool({"task_id": 9, "pr": 5})

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
    return await tns["_active_review_hold"](pool, 1)

h = asyncio.run(db_down())
check("a database error fails closed: the read holds",
      h and h.get("unreadable") and "could not be read" in tns["_review_hold_reason"](h), str(h))
check("an unreadable PR keeps the hold (never ships held code on a gitea error)",
      asyncio.run(outage()) == {"task_id": 9, "pr": 5})
r = tns["_review_hold_reason"]({"task_id": 9404, "pr": 1})
check("the held sentence names the task, the PR, and what releases it",
      "build task 9404" in r and "pull request #1" in r and "merge that pull request yourself" in r
      and "ask me to undo it first; merging still releases it" in r and "close it" not in r, r)
f = facts({"auto_merge": {"merged": False, "held": r}, "auto_ship": {"shipped": False, "reason": r}})
check("the held line is not doubled", f.count("Nothing") == 1, f)
print("all gates passed" if not failures else f"{len(failures)} failing")
sys.exit(1 if failures else 0)
