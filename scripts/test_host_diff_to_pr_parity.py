#!/usr/bin/env python3
"""Host parity: the Host codes a
diff to an existing repository on its own harnesses, files it as a pull
request without retyping it, and reads and comments on pull requests.

Measured before this change:
  - a build at the root of a project on a repository was briefed and checked as
    a private app: the build failed "no index.html anywhere in the
    output", and the task prompt, the desktop brief, the
    Antigravity note and the Claude Code harness's benchmark rule all named app
    deliverables or a scored submission whatever the target;
  - gitea op='propose' took only whole files, so a harness diff had to be
    retyped through the model;
  - create_project took only a name and a description, and import_repo
    turned off the forge credential, so a private forge repo could not be
    cloned;
  - gitea could not read a pull request, its files, reviews or CI, or comment
    on one; review.propose_commit never committed.

  A  target_contracts: repo_change has its own contract and no entry or
     manifest check; an unknown target is refused; root_target
  B  a repo_change build is briefed with that contract only (no deployment or
     SDK contract, no desktop brief), and lands with no index.html; the build
     prompt names no app deliverable
  C  a build at a mounted project's root resolves to repo_change (single build
     and compare), unless the repository has an app manifest at its root
  D  the Claude Code harness keeps its benchmark rule off a repository change
  E  gitea propose: edits (a non-unique old_string is refused), a patch, and
     from_project read from a real project repository and merged on the branch
  F  gitea PR reads are read-safe, comment is a write behind write:gitea, and a
     PR by another party marks the turn
  G  create_project mounts or forks; import_repo offers the forge credential to
     the forge only; review.propose_commit commit=true commits

Container-free (temp dirs, real git, stubbed forge and pool). Run:
APP_ROOT=<repo> python3 scripts/test_host_diff_to_pr_parity.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
logging.disable(logging.CRITICAL)

FAILS: list[str] = []
PASSED = 0


def check(label, ok, detail=""):
    global PASSED
    print(f"[diff-to-pr] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {str(detail)[:400]}" if detail and not ok else ""))
    if ok:
        PASSED += 1
    else:
        FAILS.append(label)


def part(fn):
    """A part that crashes is a failed part, and the others still run."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        check(f"{fn.__name__} ran", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _git(cwd, *args, env=None):
    e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@t", **(env or {})}
    r = subprocess.run(["git", *args], cwd=str(cwd), env=e, capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


# ── A. target contracts ─────────────────────────────────────────────────────
def part_a_contracts():
    from server.applications import target_contracts as tc
    check("A1 checks_for('repo_change') has no entry and no manifest check",
          tc.checks_for("repo_change") == {"entry": False, "manifest": False},
          tc.checks_for("repo_change"))
    pre = tc.contract_prefix("repo_change", "DEPLOY-CONTRACT", "SDK-CONTRACT")
    check("A2 contract_prefix('repo_change') is its own contract: no index.html, no app contracts",
          "index.html" not in pre and "app.yaml" not in pre and "DEPLOY-CONTRACT" not in pre
          and "SDK-CONTRACT" not in pre and "REPOSITORY CHANGE" in pre, pre[:300])
    check("A3 the app targets still get the app contracts",
          "DEPLOY-CONTRACT" in tc.contract_prefix(None, "DEPLOY-CONTRACT", "SDK-CONTRACT")
          and "DEPLOY-CONTRACT" in tc.contract_prefix("store_app", "DEPLOY-CONTRACT", "SDK-CONTRACT"))
    try:
        tc.contract_prefix("no_such_target", "DEPLOY-CONTRACT", "SDK-CONTRACT")
        raised = False
    except ValueError:
        raised = True
    check("A4 an unknown target is refused, never briefed as an app", raised)
    check("A5 root_target: a project on a repository is repo_change; others keep the default",
          tc.root_target({"repo_seed": {"repo": "owner/repo", "done": True}}) == "repo_change"
          and tc.root_target(json.dumps({"repo_seed": {"repo": "x/y"}})) == "repo_change"
          and tc.root_target({}) is None and tc.root_target(None) is None)
    from server.routes import project_modules
    check("A6 repo_change is never a module target", "repo_change" not in project_modules.TARGETS)
    # A repository with an app manifest at its root is an app, mounted or
    # not: a ticket fix on the Host's own app repo keeps the app contract,
    # the SDK contract and the index.html entry check.
    seed = {"repo_seed": {"repo": "alex/notes-app", "done": True}}
    with tempfile.TemporaryDirectory() as app_dir, tempfile.TemporaryDirectory() as code_dir:
        (Path(app_dir) / "app.yaml").write_text("id: notes-app\n")
        (Path(code_dir) / "README.md").write_text("a library\n")
        got_app = tc.root_target(seed, app_dir)
        check("A7 a mounted repo with app.yaml at its root resolves to the app target",
              got_app == tc.DEFAULT_TARGET, got_app)
        check("A8 ...which keeps the entry and manifest checks and the app contracts",
              tc.checks_for(got_app) == {"entry": True, "manifest": True}
              and "SDK-CONTRACT" in tc.contract_prefix(got_app, "DEPLOY-CONTRACT", "SDK-CONTRACT"),
              tc.checks_for(got_app))
        got_code = tc.root_target(json.dumps(seed), code_dir)
        check("A9 a mounted repo without an app manifest stays repo_change",
              got_code == "repo_change", got_code)
        check("A10 the manifest it looks for is the one the build contract validates",
              getattr(tc, "APP_MANIFEST", None) == "app.yaml"
              and '"app.yaml"' in (ROOT / "server/applications/build_contract.py").read_text())
    # What the Host reads says the same: the build-workflow skill's paragraph
    # on mounted projects names the app-manifest exception, so the Host does
    # not tell its user an app repo's fix skips the app contract.
    skill = (ROOT / "default-skills/build-workflow.md").read_text()
    para = next((p for p in skill.split("\n- ") if "`repo_change`" in p), "")
    check("A11 the build-workflow skill says a mounted repo with app.yaml at its root builds as an app",
          "`app.yaml` at its root" in para and "`private_app`" in para, para[:400])


# ── B. what a repo_change build is told, and that it lands ──────────────────
def part_b_single_build():
    from server.routes import review as rv
    from server.routes import _preference_profile as pp
    from mastercode.runtime import tools as T

    task = "Fix the taskbar clock in the shell's dock: brain/mastercode/clock.py rounds wrong"
    sent: dict = {}

    def _stub(worktree, prompt):
        sent["prompt"] = prompt
        (worktree / "brain").mkdir(exist_ok=True)
        (worktree / "brain" / "clock.py").write_text("ROUND = 60\n")
        return {"ok": True, "harness": "stub", "exit_code": 0, "model": "stub",
                "elapsed_seconds": 0.0, "transcript_full": "edited brain/clock.py"}

    async def _target(h, model=None):
        return "claude-code", None, None

    async def _no_settings():
        return {}

    with tempfile.TemporaryDirectory() as td:
        saved = (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
                 pp.summarize_for_prompt, rv.resolve_build_target, rv._coding_harness_settings,
                 os.environ.get("FACTORY_ENGINE"), os.environ.get("CSERIES_SMOKE"))
        try:
            rv.PROJECTS_ROOT = Path(td) / "projects"
            rv.SESSIONS_ROOT = Path(td) / "sessions"
            os.environ["FACTORY_ENGINE"], os.environ["CSERIES_SMOKE"] = "stub", "1"
            rv._stub_harness_build = _stub
            pp.summarize_for_prompt = lambda pid: ""
            rv.resolve_build_target = _target
            rv._coding_harness_settings = _no_settings
            (rv.PROJECTS_ROOT / "501").mkdir(parents=True)
            out = run(rv.run_single_harness_build(project_id=501, prompt=task, harness="claude-code",
                                                  billing_key="factory-build-s7a",
                                                  target="repo_change"))
            p = sent.get("prompt", "")
            check("B1 a repo_change build with no index.html lands",
                  out.get("ok") is True and (rv.PROJECTS_ROOT / "501" / "brain" / "clock.py").is_file(),
                  out.get("error"))
            check("B1b ...and carries no reviewer verdict (the diff reviewer is gone, 2026-10-20)",
                  "review" not in out, sorted(out))
            check("B2 it is briefed with the repository-change contract only",
                  "REPOSITORY CHANGE" in p and "PLATFORM DEPLOYMENT CONTRACT" not in p
                  and "APP SDK CONTRACT" not in p, p[:300])
            check("B3 no desktop brief, though the task names the taskbar and the dock",
                  "THE TEMPLATE" not in p and "<!doctype" not in p.lower(), len(p))
            # The same task as an app build still gets the app contract and is
            # held to its entry page — the control for B1.
            sent.clear()
            (rv.PROJECTS_ROOT / "502").mkdir(parents=True)
            out2 = run(rv.run_single_harness_build(project_id=502, prompt="build a clock app",
                                                   harness="claude-code",
                                                   billing_key="factory-build-s7b"))
            check("B4 an app build without index.html is still refused",
                  out2.get("ok") is False and "index.html" in str(out2.get("error")),
                  out2.get("error"))
        finally:
            (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build, pp.summarize_for_prompt,
             rv.resolve_build_target, rv._coding_harness_settings, fe, sm) = saved
            for k, v in (("FACTORY_ENGINE", fe), ("CSERIES_SMOKE", sm)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    bp = T._compose_build_prompt("change clock.py", "")
    check("B5 the build task prompt names no app deliverable",
          "index.html" not in bp and "app.yaml" not in bp and "scaffold" not in bp
          and "change clock.py" in bp, bp)
    note = rv.HARNESS_PROMPT_NOTES.get("antigravity", "")
    check("B6 the Antigravity note does not use app files as its example",
          "index.html" not in note and "app.yaml" not in note, note[:200])
    check("B7 the desktop brief is for app targets only",
          rv._with_desktop_brief("design the taskbar", target="repo_change") == "design the taskbar"
          and rv._with_desktop_brief("design the taskbar", target="component") == "design the taskbar")


# ── C. a mounted project's root builds as a repository change ──────────────
def part_c_root_resolution():
    from mastercode.runtime import tools as T

    class _Pool:
        def __init__(self, cfg):
            self.cfg = cfg

        async def fetchval(self, sql, *a):
            assert "config FROM projects" in sql
            return self.cfg

    # The project directories live under T._PROJECTS_ROOT; a temp root keeps
    # this independent of what a machine running it holds there.
    saved_root = T._PROJECTS_ROOT
    tmp_root = tempfile.mkdtemp(prefix="hp-roots-")
    try:
        T._PROJECTS_ROOT = tmp_root
        mounted = json.dumps({"repo_seed": {"repo": "owner/repo", "done": True}})
        check("C1 a project mounted on a repository builds at its root as repo_change",
              run(T._root_build_target(_Pool(mounted), 7)) == "repo_change")
        check("C2 a project of its own keeps the app default",
              run(T._root_build_target(_Pool({"work_branch": "host/build"}), 7)) is None)
        os.makedirs(os.path.join(tmp_root, "8"))
        Path(tmp_root, "8", "app.yaml").write_text("id: notes-app\n")
        got = run(T._root_build_target(_Pool(mounted), 8))
        check("C5 a mounted project whose repository is an app builds at its root as one",
              got == "private_app", got)
    finally:
        T._PROJECTS_ROOT = saved_root
        shutil.rmtree(tmp_root, ignore_errors=True)
    src = tools_source(ROOT)
    i = src.find("_mod_target = (")
    check("C3 the build lane resolves a root build through it",
          i > 0 and "_root_build_target(pool, project_id)" in src[i:i + 200])
    k = src.find("raise _SeedPending(_seed_block)")
    r = src.find("_mod_target = await _root_build_target", k)
    check("C6 ...and again once the project's code is read in, before the build runs",
          k > 0 and "_mod_target = await _root_build_target(pool, project_id)" in src[k:k + 600]
          and src.find("_pool_build(", k) > r)
    # Only a ROOT build is resolved again: a module build keeps its module's
    # own target (a public_site module must never be re-briefed as an app).
    g = src.rfind("if _module_row is None:", k, r)
    check("C7 ...and only for a root build: the second resolution sits under `if _module_row is None:`",
          k < g < r and src[g:r].count("\n") <= 5, src[k:r + 60])
    rsrc = (ROOT / "server/routes/review.py").read_text()
    j = rsrc.find("from server.applications.target_contracts import root_target")
    check("C4 a compare at a project's root resolves its target the same way, over its directory",
          j > 0 and "req.target = root_target(" in rsrc[j:j + 400]
          and "cand)" in rsrc[j:j + 400])


# ── D. the Claude Code harness's working rule ───────────────────────────────
def part_d_harness_rule():
    import importlib.util
    sys.path.insert(0, str(ROOT / "cli"))
    sys.path.insert(0, str(ROOT / "system-apps"))
    spec = importlib.util.spec_from_file_location(
        "cch_main_s7", str(ROOT / "system-apps/claude-code-harness/bin/main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app_rule = mod._system_rule(None)
    repo_rule = mod._system_rule("repo_change")
    check("D1 an app build keeps the commit-first rule", app_rule == mod._COMMIT_RULE)
    check("D2 a repository change is not told it is scored against a clock",
          repo_rule and "scor" not in repo_rule.lower() and "time runs out" not in repo_rule
          and "partial" not in repo_rule, repo_rule)
    from server.routes import review as rv
    src = (ROOT / "server/routes/review.py").read_text()
    pod = src.split("async def _run_on_pod(")[1].split("\nasync def ")[0]
    check("D3 the run's target rides the pod payload", 'payload["target"] = target' in pod)
    check("D4 the harness reads it", 'body.get("target")' in
          (ROOT / "system-apps/claude-code-harness/bin/main.py").read_text())


# ── E. gitea propose: edits, patch, from_project ────────────────────────────
class _Forge:
    """The upstream repository, no fork yet; records every request."""

    def __init__(self):
        self.calls = []

    def creds(self):
        return ("entity-7", "tok-secret", "forge.example")

    async def request(self, method, path, *, params=None, json=None, raw_text=False, bearer=None):
        self.calls.append((method, path, json))
        up, fk = "/api/v1/repos/owner/widget", "/api/v1/repos/entity-7/widget"
        if method == "GET" and path == up:
            return {"full_name": "owner/widget", "owner": {"login": "owner"}, "default_branch": "main"}
        if method == "GET" and path == f"{up}/branches/main":
            return {"name": "main"}
        if method == "GET" and path.endswith("/forks"):
            return []
        if method == "POST" and path == f"{up}/forks":
            return {"full_name": "entity-7/widget", "owner": {"login": "entity-7"}}
        if method == "POST" and path.endswith("/merge-upstream"):
            return {}
        if method == "GET" and path.startswith(f"{fk}/branches/"):
            from fastapi import HTTPException
            raise HTTPException(404, "gitea: branch does not exist")
        if method == "GET" and path == f"{up}/pulls":
            return []
        if method == "POST" and path == f"{up}/pulls":
            return {"number": 7, "html_url": "https://forge.example/owner/widget/pulls/7"}
        from fastapi import HTTPException
        raise HTTPException(500, f"unexpected {method} {path}")


class _AuditPool:
    def __init__(self, project_row=None):
        self.rows = []
        self.project_row = project_row

    async def execute(self, sql, *a):
        if sql.lstrip().upper().startswith("INSERT"):
            self.rows.append(a)
        return "OK"

    async def fetch(self, sql, *a):
        if "entity_domain_access" in sql:
            return [{"domain_id": "external.operator", "access_level": "read_write"}]
        return []

    async def fetchrow(self, sql, *a):
        if "FROM projects" in sql:
            return self.project_row
        return None

    async def fetchval(self, sql, *a):
        return None


def _real_git_lane(GP, upstream: Path):
    """_run_git against real git, with every fetch/push URL pointed at local
    bare repos (the upstream, and a fork made on first push)."""
    real = GP._run_git
    fork = upstream.parent / "fork.git"

    def run_git(argv, cwd, env, timeout=GP._GIT_TIMEOUT):
        argv = list(argv)
        for i, a in enumerate(argv):
            if a == "https://forge.example/owner/widget.git":
                argv[i] = str(upstream)
            elif a == "https://forge.example/entity-7/widget.git":
                if not fork.exists():
                    subprocess.run(["git", "clone", "-q", "--bare", str(upstream), str(fork)],
                                   check=True, capture_output=True)
                argv[i] = str(fork)
        env = {**env, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        return real(argv, cwd, env, timeout)
    return run_git, fork


def part_e_propose_modes():
    from fastapi import HTTPException
    from server.routes import gitea_propose as GP
    from server.routes import gitea_client as GC
    from server.routes import project_git as PG
    from server import brain_service

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        seedw = td / "seed"
        seedw.mkdir()
        _git(seedw, "init", "-q", "-b", "main")
        (seedw / "probe.py").write_text("LIMIT = 80\nLIMIT_LOG = 80\n\n\nname = 'probe'\n")
        (seedw / "README.md").write_text("# widget\n")
        _git(seedw, "add", "-A")
        _git(seedw, "commit", "-q", "-m", "start")
        base_sha = _git(seedw, "rev-parse", "HEAD")
        upstream = td / "upstream.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seedw), str(upstream)], check=True)

        forge = _Forge()
        pool = _AuditPool()
        saved = (GC.gitea_creds, GC.gitea_request, GC.gitea_proxy, GP._run_git,
                 brain_service.get_pool, brain_service._entity_id, PG._PROJECTS_ROOT)
        try:
            GC.gitea_creds, GC.gitea_request = forge.creds, forge.request
            GC.gitea_proxy = lambda: ""
            GP._run_git, fork = _real_git_lane(GP, upstream)
            brain_service.get_pool = lambda: pool
            brain_service._entity_id = 7
            os.environ["ENTITY_ID"] = "7"

            body = {"repo": "owner/widget", "title": "Raise the probe limit"}
            # E1: an old_string that matches twice is refused, nothing pushed.
            try:
                run(GP.propose({**body, "files": [{"path": "probe.py", "edits": [
                    {"old_string": "80", "new_string": "90"}]}]}))
                err = None
            except HTTPException as e:
                err = e
            nothing_pushed = (not fork.exists()
                              or _git(fork, "branch", "--list", "propose/*") == "")
            check("E1 an old_string that matches more than once is refused, by name, nothing pushed",
                  err is not None and err.status_code == 400 and "matches 2 places" in err.detail
                  and nothing_pushed, getattr(err, "detail", None))
            try:
                run(GP.propose({**body, "files": [{"path": "probe.py", "edits": [
                    {"old_string": "LIMIT = 90", "new_string": "LIMIT = 95"}]}]}))
                err = None
            except HTTPException as e:
                err = e
            check("E2 an old_string that is not there is refused",
                  err is not None and "was not found" in err.detail, getattr(err, "detail", None))

            # E3: a unique edit lands as one commit on the fork's branch.
            out = run(GP.propose({**body, "files": [{"path": "probe.py", "edits": [
                {"old_string": "LIMIT = 80\n", "new_string": "LIMIT = 90\n"}]}]}))
            got = _git(fork, "show", f"{out['branch']}:probe.py")
            check("E3 edits change the file in place and keep the rest",
                  got == "LIMIT = 90\nLIMIT_LOG = 80\n\n\nname = 'probe'", got)

            # E4: a unified patch.
            patch = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
                     "@@ -1 +1,2 @@\n # widget\n+Watches disks.\n")
            out = run(GP.propose({**body, "title": "Say what it watches", "patch": patch}))
            got = _git(fork, "show", f"{out['branch']}:README.md")
            check("E4 a unified patch applies", got == "# widget\nWatches disks.", got)
            try:
                run(GP.propose({**body, "title": "Bad patch", "patch": patch.replace("# widget\n+", "# other\n+")}))
                err = None
            except HTTPException as e:
                err = e
            check("E5 a patch that does not apply is refused",
                  err is not None and err.status_code == 409 and "does not apply" in err.detail,
                  getattr(err, "detail", None))

            # E6: from_project — a project copied from owner/widget at base_sha,
            # changed on disk (committed and not), proposed without retyping.
            proots = td / "projects"
            pdir = proots / "41"
            subprocess.run(["git", "clone", "-q", str(upstream), str(pdir)], check=True,
                           capture_output=True)
            (pdir / "probe.py").write_text("LIMIT = 80\nLIMIT_LOG = 70\n\n\nname = 'probe'\n")
            (pdir / "tests").mkdir()
            (pdir / "tests" / "test_probe.py").write_text("def test_limit():\n    assert True\n")
            (pdir / "README.md").unlink()
            PG._PROJECTS_ROOT = proots
            pool.project_row = {"repo": "entity-7/widget-p41",
                                "forked_from": json.dumps({"repo": "owner/widget", "commit": base_sha}),
                                "config": {"repo_seed": {"repo": "entity-7/widget-p41", "done": True,
                                                         "default_branch": "main"}}}
            # The upstream moved meanwhile on another line of probe.py.
            up_work = td / "upwork"
            subprocess.run(["git", "clone", "-q", str(upstream), str(up_work)], check=True,
                           capture_output=True)
            (up_work / "probe.py").write_text("LIMIT = 80\nLIMIT_LOG = 80\n\n\nname = 'disk-probe'\n")
            _git(up_work, "commit", "-q", "-am", "rename")
            _git(up_work, "push", "-q", "origin", "main")
            out = run(GP.propose({**body, "title": "Lower the log limit", "from_project": 41}))
            b = out["branch"]
            check("E6 from_project carries every changed file, merged over the upstream's own change",
                  _git(fork, "show", f"{b}:probe.py") == "LIMIT = 80\nLIMIT_LOG = 70\n\n\nname = 'disk-probe'"
                  and _git(fork, "show", f"{b}:tests/test_probe.py").startswith("def test_limit")
                  and "README.md" not in _git(fork, "ls-tree", "--name-only", b)
                  and sorted(out["files"]) == ["README.md", "probe.py", "tests/test_probe.py"],
                  out.get("files"))
            check("E7 the project's own index is untouched",
                  _git(pdir, "status", "--porcelain").count("??") == 1)
            # A change that overlaps the upstream's is refused with the file named.
            (pdir / "probe.py").write_text("LIMIT = 80\nLIMIT_LOG = 70\n\n\nname = 'widget-probe'\n")
            try:
                run(GP.propose({**body, "title": "Rename it too", "from_project": 41}))
                err = None
            except HTTPException as e:
                err = e
            check("E8 an overlap with the upstream's change is refused, naming the file",
                  err is not None and err.status_code == 409 and "probe.py" in err.detail,
                  getattr(err, "detail", None))
            try:
                run(GP.propose({**body, "title": "Wrong repo", "from_project": 41,
                                "repo": "owner/other"}))
                err = None
            except HTTPException as e:
                err = e
            check("E9 from_project on a repository the project is not on is refused",
                  err is not None and err.status_code == 400 and "not on owner/other" in err.detail,
                  getattr(err, "detail", None))
            pushes_up = [c for c in forge.calls if c[0] != "GET" and "/repos/owner/" in c[1]]
            check("E10 the upstream got only fork and pull-request requests, never a push",
                  all(p[1].endswith(("/forks", "/pulls")) for p in pushes_up)
                  and _git(upstream, "branch", "--list", "propose/*") == "", pushes_up)

            # E11: a file rewritten at the same size in the same second as the
            # project's last index write. Its cached stat still matches, so only
            # git's racy-entry re-read finds the change, and that holds only
            # while the snapshot's index keeps the project index's own time.
            # E6 hit this by chance (3 of 24 runs under load); here it is built
            # on purpose: the write lands in the clone's second, the propose in
            # a later one.
            head_sha = _git(upstream, "rev-parse", "main")
            racy = proots / "42"
            for _ in range(20):
                shutil.rmtree(racy, ignore_errors=True)
                subprocess.run(["git", "clone", "-q", str(upstream), str(racy)], check=True,
                               capture_output=True)
                old = (racy / "probe.py").read_text()
                new = old.replace("LIMIT_LOG = 80", "LIMIT_LOG = 60")
                (racy / "probe.py").write_text(new)
                st = (racy / "probe.py").stat()
                dbg = _git(racy, "ls-files", "--debug", "--", "probe.py")
                ent = {k: int(v.split(":")[0]) for k, v in
                       (ln.strip().split(": ", 1) for ln in dbg.splitlines()[1:3])}
                same_second = (len(new) == len(old) and new != old
                               and ent["ctime"] == int(st.st_ctime) == int(st.st_mtime)
                               == ent["mtime"] == int((racy / ".git" / "index").stat().st_mtime))
                if same_second:
                    break
            time.sleep(1.05 - time.time() % 1)
            pool.project_row = {"repo": "entity-1/widget-p42",
                                "forked_from": json.dumps({"repo": "owner/widget", "commit": head_sha}),
                                "config": {"repo_seed": {"repo": "entity-1/widget-p42", "done": True,
                                                         "default_branch": "main"}}}
            try:
                out, err = run(GP.propose({**body, "title": "Lower the log limit again",
                                           "from_project": 42})), None
            except HTTPException as e:
                out, err = {}, e
            check("E11 a same-size edit in the same second as the project's last index write is carried",
                  same_second and err is None and out["files"] == ["probe.py"]
                  and _git(fork, "show", f"{out['branch']}:probe.py") == new.rstrip("\n"),
                  {"same_second": same_second, "files": out.get("files"),
                   "refused": getattr(err, "detail", None)})
        finally:
            (GC.gitea_creds, GC.gitea_request, GC.gitea_proxy, GP._run_git,
             brain_service.get_pool, brain_service._entity_id, PG._PROJECTS_ROOT) = saved


# ── F. gitea pull-request reads and comment ─────────────────────────────────
def part_f_pr_reads():
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.runtime import tools as T
    from mastercode.runtime import turn_taint
    from server.routes import gitea_client as GC
    from server import brain_service

    schema = PUBLIC_TOOL_SCHEMAS["gitea"]
    ops = read_safe_ops("gitea", schema)
    want = {"pulls", "pull", "pull_files", "pull_reviews", "commit_status", "branches"}
    check("F1 read_safe_ops('gitea') holds the pull-request reads", want <= ops, sorted(ops))
    check("F2 propose and comment are not reads; the tool stays on the operate lane",
          not ({"propose", "comment"} & ops) and lane_for("gitea", schema) == "operate")
    from mastercode.runtime.tool_access import is_read_safe_tool
    check("F3 'pull' and 'commit' are not read words for any other tool",
          read_safe_ops("some_app_tool", {"function": {"parameters": {"properties": {
              "op": {"enum": ["pull", "commit_status", "pulls"]}}}}}) == frozenset()
          and not is_read_safe_tool("some_app_tool", {"function": {"parameters": {"properties": {
              "op": {"enum": ["pull"]}}}}}))

    base = "/api/v1/repos/owner/repo"
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
            "diff --git a/b.md b/b.md\nnew file mode 100644\n--- /dev/null\n+++ b/b.md\n@@ -0,0 +1 @@\n+hi\n")
    posted = []

    async def req(method, path, *, params=None, json=None, raw_text=False, bearer=None):
        if method == "POST":
            posted.append((path, json))
            return {"id": 99, "html_url": "https://forge.example/owner/repo/pulls/12#c99"}
        if path == base:
            return {"full_name": "owner/repo", "owner": {"login": "owner"}, "default_branch": "main",
                    "permissions": {"pull": True}}
        if path == "/api/v1/user/repos":
            return [{"full_name": "owner/repo", "owner": {"login": "owner"}}]
        if path == f"{base}/pulls":
            return [{"number": 12, "title": "Fix x", "state": "open", "user": {"login": "entity-9"},
                     "head": {"ref": "propose/fix-x", "sha": "abc"}, "base": {"ref": "main"},
                     "mergeable": True}]
        if path == f"{base}/pulls/12":
            return {"number": 12, "title": "Fix x", "body": "why", "state": "open",
                    "user": {"login": "owner"}, "head": {"ref": "fix-x", "sha": "abc"},
                    "base": {"ref": "main"}, "mergeable": True, "merged": False}
        if path == f"{base}/issues/12/comments":
            return [{"id": 1, "user": {"login": "owner"}, "body": "looks right"}]
        if path == f"{base}/pulls/12/files":
            return [{"filename": "a.py", "status": "changed", "additions": 1, "deletions": 1},
                    {"filename": "b.md", "status": "added", "additions": 1, "deletions": 0}]
        if path == f"{base}/pulls/12.diff":
            return diff
        if path == f"{base}/pulls/12/reviews":
            return [{"id": 5, "user": {"login": "owner"}, "state": "REQUEST_CHANGES", "body": "b",
                     "comments_count": 1}]
        if path == f"{base}/pulls/12/reviews/5/comments":
            return [{"path": "a.py", "position": 1, "user": {"login": "owner"}, "body": "why 2?"}]
        if path == f"{base}/commits/abc/status":
            return {"sha": "abc", "state": "success",
                    "statuses": [{"context": "ci/pre-merge", "status": "success"}]}
        if path == f"{base}/branches":
            return [{"name": "main", "commit": {"id": "abc"}, "protected": True}]
        from fastapi import HTTPException
        raise HTTPException(404, f"unexpected {path}")

    class _Pool:
        async def fetch(self, sql, *a):
            return [{"domain_id": "external.operator", "access_level": self.level}]
        level = "read_write"

    pool = _Pool()

    async def _user():
        return "owner"
    saved = (GC.gitea_creds, GC.gitea_request, GC.gitea_netloc, T._gitea_user_login,
             brain_service.get_pool)
    try:
        GC.gitea_creds = lambda: ("entity-7", "tok", "forge.example")
        GC.gitea_request = req
        GC.gitea_netloc = lambda: "forge.example"
        T._gitea_user_login = _user
        brain_service.get_pool = lambda: pool

        def call(args):
            tok = turn_taint.begin_turn()
            try:
                r = run(T._exec_gitea(args, entity_id=7, pool=pool))
                return r, turn_taint.tainted()
            finally:
                turn_taint.end_turn(tok)

        r, tainted = call({"op": "pull", "repo": "owner/repo", "number": 12})
        res = r.get("result") or {}
        check("F4 op='pull' reads one PR and its conversation",
              r.get("status") == "ok" and res.get("head_sha") == "abc" and res.get("body") == "why"
              and res["comments"][0]["body"] == "looks right", r)
        check("F5 a PR by the user is not marked untrusted", tainted is False)
        r, tainted = call({"op": "pulls", "repo": "owner/repo"})
        check("F6 op='pulls' lists PRs; one by another Host marks the turn",
              r.get("status") == "ok" and r["result"]["pulls"][0]["number"] == 12 and tainted is True, r)
        r, _ = call({"op": "pull_files", "repo": "owner/repo", "number": 12})
        files = (r.get("result") or {}).get("files") or []
        check("F7 op='pull_files' gives each changed file its patch",
              len(files) == 2 and "+x = 2" in (files[0].get("patch") or "")
              and "+hi" in (files[1].get("patch") or ""), r)
        r, _ = call({"op": "pull_files", "repo": "owner/repo", "number": 12, "path": "a.py"})
        check("F8 path= returns that file's whole patch",
              r.get("status") == "ok" and "+x = 2" in r["result"]["content"]
              and "b.md" not in r["result"]["content"], r)
        r, _ = call({"op": "pull_reviews", "repo": "owner/repo", "number": 12})
        rv0 = ((r.get("result") or {}).get("reviews") or [{}])[0]
        check("F9 op='pull_reviews' carries each review's inline comments",
              rv0.get("state") == "REQUEST_CHANGES" and rv0["comments"][0]["path"] == "a.py", r)
        r, _ = call({"op": "commit_status", "repo": "owner/repo", "number": 12})
        check("F10 op='commit_status' of a PR reads its head's CI",
              (r.get("result") or {}).get("state") == "success"
              and r["result"]["checks"][0]["context"] == "ci/pre-merge", r)
        r, _ = call({"op": "branches", "repo": "owner/repo"})
        check("F11 op='branches' lists branches", (r.get("result") or {}).get("count") == 1, r)
        r, _ = call({"op": "pull", "repo": "owner/repo"})
        check("F12 a PR read without number is refused by name",
              r.get("status") != "ok" and "number" in str(r.get("result")), r)
        r, _ = call({"op": "comment", "repo": "owner/repo", "number": 12, "body": "Found: a.py:1"})
        check("F13 op='comment' posts on the PR's conversation",
              r.get("status") == "ok" and posted
              and posted[-1] == (f"{base}/issues/12/comments", {"body": "Found: a.py:1"}), (r, posted))
        pool.level = "read"
        posted.clear()
        r, _ = call({"op": "comment", "repo": "owner/repo", "number": 12, "body": "x"})
        check("F14 a read-only grant cannot comment", r.get("status") == "denied" and not posted, r)
        from mastercode.runtime import root_turn
        # The root thread proposes to a named repo (the
        # executor refuses another party's) and never comments.
        check("F15 the root thread never comments; a propose must name its repo",
              not root_turn.admits("gitea", {"op": "comment", "repo": "owner/repo"})
              and not root_turn.admits("gitea", {"op": "propose"})
              and root_turn.admits("gitea", {"op": "propose", "repo": "owner/repo"}))
    finally:
        (GC.gitea_creds, GC.gitea_request, GC.gitea_netloc, T._gitea_user_login,
         brain_service.get_pool) = saved


# ── G. create_project, import_repo, propose_commit ──────────────────────────
def part_g_projects():
    from mastercode.runtime import tools as T
    from server.routes import projects as P

    calls = []

    async def plan(pool, eid, repo, *, fork=False):
        calls.append(("plan", repo, fork))
        if repo == "owner/locked" and not fork:
            raise P.ProjectsError(409, "The Host can read owner/locked but can't change it. "
                                       "Fork it into a new project instead.")
        return {"repo": repo, "fork": fork, "facts": {"default_branch": "main", "owned_by_host": False}}

    async def insert_mounted(pool, eid, name, description, mount):
        calls.append(("insert_mounted", mount["repo"]))
        return {"id": 77, "status": "active", "created_at": None}

    async def mount(pool, pid, m):
        calls.append(("mount", pid, m["repo"]))
        return {"repo": m["repo"], "mounted": True}

    async def fork(pool, pid, name, m):
        calls.append(("fork", pid, m["repo"]))
        return {"repo": f"entity-7/{name}-p{pid}", "created": True, "forked_from": m["repo"]}

    async def own_repo(pool, pid, name):
        calls.append(("own_repo", pid))
        return {"repo": f"entity-7/{name}"}

    class _Pool:
        async def fetchrow(self, sql, *a):
            calls.append(("insert_plain",))
            return {"id": 78, "name": a[1], "status": "active", "created_at": None}

        async def fetchval(self, *a):
            return None

        async def execute(self, *a):
            calls.append(("execute", a[0][:20]))

    saved = (P.plan_repo_mount, P.insert_mounted_project, P.mount_project_repo,
             P.fork_repo_for_project, P.ensure_project_repo, P.ensure_project_dir, T._write_started_from)

    async def _no_marker(*a, **k):
        return None
    try:
        P.plan_repo_mount, P.insert_mounted_project, P.mount_project_repo = plan, insert_mounted, mount
        P.fork_repo_for_project, P.ensure_project_repo = fork, own_repo
        P.ensure_project_dir = lambda pid: None
        T._write_started_from = _no_marker
        r = run(T._exec_create_project({"name": "cseries fix", "repo": "owner/repo"}, 9003, _Pool()))
        check("G1 create_project repo= mounts the repository",
              r.get("status") == "ok" and ("insert_mounted", "owner/repo") in calls
              and ("mount", 77, "owner/repo") in calls and ("own_repo", 77) not in calls, (r, calls))
        calls.clear()
        r = run(T._exec_create_project({"name": "cseries fix", "repo": "owner/repo", "fork": True},
                                       3, _Pool()))
        check("G2 fork=true copies it into a new repository for the project",
              r.get("status") == "ok" and ("plan", "owner/repo", True) in calls
              and ("fork", 78, "owner/repo") in calls
              and r["result"].get("forked_from") == "owner/repo", (r, calls))
        calls.clear()
        r = run(T._exec_create_project({"name": "locked", "repo": "owner/locked"}, 9003, _Pool()))
        check("G3 a repository the Host can only read is refused before any row, naming the fork",
              r.get("status") != "ok" and "Fork it" in str(r.get("result"))
              and not any(c[0].startswith("insert") for c in calls), (r, calls))
    finally:
        (P.plan_repo_mount, P.insert_mounted_project, P.mount_project_repo,
         P.fork_repo_for_project, P.ensure_project_repo, P.ensure_project_dir,
         T._write_started_from) = saved

    # import_repo: the forge credential, to the forge only.
    from server.routes import gitea_client as GC
    saved = (GC.gitea_creds, GC.gitea_proxy)
    try:
        GC.gitea_creds = lambda: ("entity-7", "tok-forge", "forge.example")
        GC.gitea_proxy = lambda: ""
        args, env = T._forge_clone_auth("https://forge.example/owner/repo.git")
        a2, e2 = T._forge_clone_auth("https://github.com/owner/repo.git")
        a3, e3 = T._forge_clone_auth("https://x:y@forge.example/owner/repo.git")
        check("G4 the forge credential is offered for the forge only",
              args and env.get("PRIVATAE_FORGE_TOKEN") == "tok-forge"
              and not a2 and not e2 and not a3 and not e3)
        check("G5 the token is never on the command line", "tok-forge" not in " ".join(args))
        r = subprocess.run(["git", *args, "credential", "fill"],
                           input="protocol=https\nhost=forge.example\npath=owner/repo.git\n\n",
                           env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0", **env},
                           capture_output=True, text=True)
        r2 = subprocess.run(["git", *args, "credential", "fill"],
                            input="protocol=https\nhost=github.com\n\n",
                            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                 "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                                 "GIT_ASKPASS": "/bin/false", **env},
                            capture_output=True, text=True)
        check("G6 real git reads the Host's forge login through the helper",
              "username=entity-7" in r.stdout and "password=tok-forge" in r.stdout, r.stderr)
        check("G7 ...and gives it to no other host", "tok-forge" not in r2.stdout, r2.stdout)
    finally:
        GC.gitea_creds, GC.gitea_proxy = saved

    # review.propose_commit commit=true commits the picks.
    from server.routes import review as rv
    from server.routes import project_git as PG
    from server import brain_service
    synced = []

    async def _sync(pool, project_id, **kw):
        synced.append((project_id, kw))
        return {"synced": True, "commit": "abc1234"}
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        saved = (rv.SESSIONS_ROOT, rv.PROJECTS_ROOT, PG.sync_project_repo, brain_service.get_pool,
                 getattr(rv, "_snapshot_to_project"))
        try:
            rv.SESSIONS_ROOT = td / "sessions"
            rv.PROJECTS_ROOT = td / "projects"
            pdir = rv.PROJECTS_ROOT / "88"
            pdir.mkdir(parents=True)
            sd = rv.SESSIONS_ROOT / "s1"
            (sd / "work" / "codex").mkdir(parents=True)
            (sd / "work" / "codex" / "app.py").write_text("x = 2\n")
            (sd / "manifest.json").write_text(json.dumps({
                "project_dir": str(pdir), "project_id": 88, "worktree_root": str(sd / "work")}))
            PG.sync_project_repo = _sync
            brain_service.get_pool = lambda: object()
            rv._snapshot_to_project = lambda *a, **k: []
            out = run(rv.propose_commit(rv.ProposeCommitReq(
                session_id="s1", picks={"app.py": "codex"}, commit_message="Take codex's app.py",
                commit=True)))
            check("G8 propose_commit commit=true commits the picked files as a save point",
                  out.get("applied") == ["app.py"] and synced
                  and synced[0][0] == 88 and synced[0][1].get("paths") == ["app.py"]
                  and synced[0][1].get("phase_label") == "Take codex's app.py"
                  and (out.get("committed") or {}).get("synced") is True, (out, synced))
            synced.clear()
            out = run(rv.propose_commit(rv.ProposeCommitReq(session_id="s1", picks={"app.py": "codex"})))
            check("G9 without commit nothing is committed", not synced and "committed" not in out)
        finally:
            (rv.SESSIONS_ROOT, rv.PROJECTS_ROOT, PG.sync_project_repo, brain_service.get_pool,
             rv._snapshot_to_project) = saved


for _p in (part_a_contracts, part_b_single_build, part_c_root_resolution, part_d_harness_rule,
           part_e_propose_modes, part_f_pr_reads, part_g_projects):
    part(_p)

if FAILS:
    print(f"[diff-to-pr] FAIL {len(FAILS)} of {len(FAILS) + PASSED}: " + "; ".join(FAILS)[:2000])
    sys.exit(1)
print(f"[diff-to-pr] OK {PASSED} checks")
