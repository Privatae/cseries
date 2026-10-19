"""Code Repositories and Code Approvals.

  A. The plain-English format: every save-point label reads as a sentence, the
     title is never `Build: <project>`, the body carries its sections, and
     the build-contract refusal rides a hidden marker (the old phrase still
     counts, for bodies an older Host wrote).
  B. Who wrote it: a commit's Host-Run trailer names the run, and the run's
     coding tool and model are what the body says; no run → the Host itself,
     or the person in the editor.
  C. Auto-merge refuses on the marker — the words can change, the refusal
     cannot.
  D. A project mounts one repository: refusals before any row (not visible,
     read-only, already in use), and the fork offer in the words.
  E. Seeding: a mounted repository's code is the new project's workspace, an
     existing host/build is continued, a fork copies the source's history
     into the new repository, an empty repository gets a first commit — and a
     non-empty project directory is never touched. Real git, with the forge
     URL rewritten to local bare repositories through git's own env config.
  F. Deleting a project's repository is refused BEFORE anything changes when
     it is not allowed, and the chat line for a merge is written once.
  G. No route merges with the Host's login any more.

Container-free: fakes for the forge and the pool; git runs against temp dirs.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.environ.get("APP_ROOT") or str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


from server.routes import code_approvals as ca  # noqa: E402
from server.routes import project_git as pg  # noqa: E402
from server.routes import projects as pj  # noqa: E402

# ── A. the format ──────────────────────────────────────────────────────────
check("A phase label reads as its title", ca.plain_item("phase 3: add a login page") == "Add a login page")
check("A module phase names the module", ca.plain_item("shop: phase 2: cart") == "Cart (in shop)")
check("A build label reads as its intent", ca.plain_item("build: make the header blue") == "Make the header blue")
check("A Host write says what the Host changed", ca.plain_item("host: write src/app.js") == "The Host changed src/app.js")
check("A Host delete says what it removed", ca.plain_item("host: delete old.txt") == "The Host removed old.txt")
check("An adopt names the tool whose version was kept",
      ca.plain_item("adopt run 612: claude-code into shop") == "Kept the version Claude Code wrote for shop")
check("A version move reads as one", ca.plain_item("shop: version 4 is current") == "Switched shop to version 4"
      and ca.plain_item("shop: back to version 3") == "Put shop back to version 3")
check("The plan-end sweep is bookkeeping, not a change", ca.plain_item("plan end: remaining landed work") is None)

check("One change → its own words as the title", ca.make_title(["Add a login page"], "desc", "P") == "Add a login page")
check("Several → the person's request, first sentence",
      ca.make_title(["A", "B"], "Build me a shop. It sells hats.", "P") == "Build me a shop.")
check("No request → the project's name, never 'Build:'",
      ca.make_title(["A", "B"], "", "hat shop") == "Hat shop")
long_t = ca.make_title(["x" * 200], "", "P")
check("The title stays under 70 characters", len(long_t) <= 70 and long_t.endswith("…"), long_t)

iw = [{"item": "Add a login page", "writer": "Claude Code on claude-opus-5", "models": ["claude-opus-5"]}]
body = ca.build_body(items_writers=iw, description="a login page", project_name="P",
                     project_link="https://h.host.example.test/?open=workbench&view=project:1",
                     gate_violations=[], check_lines=["- harness-check: passed (2 checked)"],
                     phase_label="phase 1: add a login page", default_branch="main",
                     repo_full="entity-9003/p-p1", changed_files=3, additions=40, deletions=2)
check("The body opens with the format marker and has every section in order",
      body.startswith(ca.MARKER) and body.index("**What this changes**") < body.index("**Why**")
      < body.index("**What was checked**") < body.index("**Who wrote it**") < body.index("<details>"), body)
check("Why quotes the person's request and links the project",
      "You asked: “a login page”" in body and "[Open the project](https://h.host.example.test/" in body)
check("Who wrote it names the tool and the model", "- Add a login page: Claude Code on claude-opus-5." in body)
check("Details say where it lands and what changed",
      "3 files changed (+40 −2)" in body and "into `main` of the Code Repository `entity-9003/p-p1`" in body)
check("A passing contract carries no refusal marker", not ca.has_violations(body))
check("The models ride a hidden line the list reads", ca.models_of(body) == ["claude-opus-5"])
check("The list row's summary is the first change", ca.summary_of(body, "t") == "Add a login page")
bad = ca.build_body(items_writers=[], description="", project_name="P", project_link="",
                    gate_violations=["index.html missing"], check_lines=[], phase_label="phase 1: x",
                    default_branch="main", repo_full="o/r")
check("Violations: said in plain words AND marked for the merge refusal",
      "not ready to approve" in bad and "  - index.html missing" in bad and ca.has_violations(bad), bad)
check("No checks → it says none ran", "No other checks ran" in bad)
check("An older Host's phrase still refuses", ca.has_violations("Build-contract violations (not ready to accept):\n- x"))

# ── B. who wrote it ────────────────────────────────────────────────────────


class RunPool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, ids):
        return [r for r in self.rows if r["id"] in ids]


commits = [
    {"sha": "c3", "commit": {"message": "host: write notes.md", "author": {"name": "entity-9003", "date": "2026-09-27T03:00:00Z"}},
     "author": {"login": "entity-9003"}},
    {"sha": "c1", "commit": {"message": "phase 1: login page\n\n- a.html\n\nHost-Run: 41",
                             "author": {"name": "entity-9003", "date": "2026-09-27T01:00:00Z"}},
     "author": {"login": "entity-9003"}},
    {"sha": "c2", "commit": {"message": "fix the typo", "author": {"name": "Alex", "date": "2026-09-27T02:00:00Z"}},
     "author": {"login": "alex"}},
    {"sha": "c0", "commit": {"message": "plan end: remaining landed work",
                             "author": {"name": "entity-9003", "date": "2026-09-27T04:00:00Z"}}},
]
w = asyncio.run(ca.writers_of(RunPool([{"id": 41, "harness": "codex", "model": "gpt-6-astra"}]),
                              commits, "entity-9003"))
check("Oldest first, bookkeeping dropped", [x["item"] for x in w] ==
      ["Login page", "Fix the typo", "The Host changed notes.md"], w)
check("A run's tool and model are the writer", w[0]["writer"] == "Codex on gpt-6-astra" and w[0]["models"] == ["gpt-6-astra"])
check("A person's commit is theirs, from the editor", w[1]["writer"] == "Alex in the editor")
check("The Host's own commit is the Host's", w[2]["writer"] == "the Host itself")
w2 = asyncio.run(ca.writers_of(RunPool([{"id": 41, "harness": "claude-code", "model": "default"}]),
                               commits[1:2], "entity-9003"))
check("A run on the tool's default model says so", w2[0]["writer"] == "Claude Code on its default model"
      and w2[0]["models"] == [])

# ── C. auto-merge refuses on the marker ────────────────────────────────────


class Sess:
    def __init__(self, body):
        self.forge = types.SimpleNamespace(repo="o/r")
        self.user = "o"            # the Host owns o/r, so its branch is host/build
        self.body = body
        self.posted = []

    def pr_url(self, pr):
        return None

    async def request(self, method, path, **kw):
        if method == "POST":
            self.posted.append(path)
            return {}
        return [{"number": 5, "head": {"ref": "host/build", "sha": "abc"}, "body": self.body, "mergeable": True}]


s1 = Sess(bad)
r1 = asyncio.run(pg._merge_rolling_pr(s1, 1))
check("Auto-merge refuses a body carrying the marker, and merges nothing",
      r1.get("merged") is False and "build-contract" in r1.get("reason", "") and not s1.posted, r1)
s2 = Sess(body)
r2 = asyncio.run(pg._merge_rolling_pr(s2, 1))
check("…and merges a clean one", r2.get("merged") is True and s2.posted, r2)

# ── D. mounting refusals ───────────────────────────────────────────────────
for badname in ("", "noslash", "a/b/c", "../x", "o/..", "o/r;rm"):
    try:
        pj.clean_repo_name(badname)
        check(f"a bad repository name is refused: {badname!r}", False)
    except pj.ProjectsError as e:
        check(f"a bad repository name is refused: {badname!r}", e.status == 400)
check("owner/name.git is read as owner/name", pj.clean_repo_name(" owner/repo.git ") == "owner/repo")


def fake_forge(repos: dict):
    """server.routes.gitea_client, answering from `repos` as the Host `entity-9003`."""
    from fastapi import HTTPException
    m = types.ModuleType("server.routes.gitea_client")
    m.gitea_creds = lambda: ("entity-9003", "tok", "git.example")
    m.split_repo = lambda r: tuple(r.split("/", 1))
    calls = []

    async def req(method, path, **kw):
        calls.append((method, path, kw))
        for full, info in repos.items():
            if path == f"/api/v1/repos/{full}":
                if method == "DELETE":
                    return ""
                return info
            if path.startswith(f"/api/v1/repos/{full}/branches/"):
                return {"user_can_merge": info.get("_can_merge", False)}
        raise HTTPException(404, "gitea: not found")
    m.gitea_request = req
    m.calls = calls
    return m


def repo(full, *, push=True, mirror=False, can_merge=False):
    o, n = full.split("/")
    return {"full_name": full, "name": n, "owner": {"login": o}, "permissions": {"push": push},
            "mirror": mirror, "default_branch": "main", "_can_merge": can_merge}


class ProjPool:
    """Answers the few project queries the services make."""

    def __init__(self, live=None, others=None, row=None):
        self.live = live
        self.others = others or []
        self.row = row
        self.executed = []

    async def fetchrow(self, sql, *a):
        if "status <> 'archived'" in sql and "lower(repo)" in sql:
            return self.live
        return self.row

    async def fetch(self, sql, *a):
        return self.others

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return "UPDATE 1"


_real = sys.modules.get("server.routes.gitea_client")
forge = fake_forge({"owner/repo": repo("owner/repo", push=True),
                    "owner/private": repo("owner/private", push=False),
                    "owner/mirror": repo("owner/mirror", push=True, mirror=True),
                    "entity-9003/shop-p9": repo("entity-9003/shop-p9", can_merge=True)})
sys.modules["server.routes.gitea_client"] = forge
try:
    def refused(coro):
        try:
            asyncio.run(coro)
            return None
        except pj.ProjectsError as e:
            return e

    e = refused(pj.plan_repo_mount(ProjPool(), 3, "owner/nothere"))
    check("A repository the Host cannot see is refused", e is not None and e.status == 404, e)
    e = refused(pj.plan_repo_mount(ProjPool(), 3, "owner/private"))
    check("Read-only → refused, with the fork offer", e is not None and e.status == 409 and "Fork it" in e.detail, e)
    e = refused(pj.plan_repo_mount(ProjPool(), 3, "owner/mirror"))
    check("A pull mirror refuses pushes → the fork offer", e is not None and e.status == 409, e)
    e = refused(pj.plan_repo_mount(ProjPool(live={"id": 7, "name": "Circles"}), 3, "owner/repo"))
    check("Already in use → refused, naming the project and offering the fork",
          e is not None and e.status == 409 and "“Circles”" in e.detail and "Fork it" in e.detail, e)
    ok = asyncio.run(pj.plan_repo_mount(ProjPool(), 3, "owner/repo"))
    check("A writable, unused repository mounts", ok["repo"] == "owner/repo" and ok["fork"] is False)
    fk = asyncio.run(pj.plan_repo_mount(ProjPool(live={"id": 7, "name": "Circles"}), 3, "owner/repo", fork=True))
    check("A fork is allowed where a mount is not", fk["fork"] is True)

    # F. delete refused before anything changes
    pool = ProjPool(row={"id": 5, "name": "P", "repo": "owner/repo", "auto_merge": False})
    from server.routes import project_forge as pj_forge
    _pf_real = pj_forge.project_forge

    async def _platform(pool_, pid):
        return pj_forge.Forge("platform", "owner/repo")
    pj_forge.project_forge = _platform
    try:
        e = refused(pj.project_archive_service(pool, 3, 5, delete_repo=True))
        check("Deleting a repository the Host does not own is refused, and nothing is archived",
              e is not None and e.status == 409 and "belongs to owner" in e.detail and not pool.executed, e)
        info = asyncio.run(pj.project_repository_service(
            ProjPool(row={"id": 5, "name": "P", "repo": "owner/repo", "auto_merge": False}), 3, 5))
        check("Auto-Approve is not offered where the Host cannot merge (a platform repository)",
              info["host_can_merge"] is False and info["can_delete"] is False)

        async def _shop(pool_, pid):
            return pj_forge.Forge("platform", "entity-9003/shop-p9")
        pj_forge.project_forge = _shop
        pool2 = ProjPool(row={"id": 9, "name": "Shop", "repo": "entity-9003/shop-p9", "auto_merge": True},
                         others=[{"id": 4, "name": "Old shop", "status": "archived"}])
        e = refused(pj.project_archive_service(pool2, 3, 9, delete_repo=True))
        check("…and where another project (even archived) still uses it",
              e is not None and "“Old shop”, archived" in e.detail and not pool2.executed, e)
        pool3 = ProjPool(row={"id": 9, "name": "Shop", "repo": "entity-9003/shop-p9", "auto_merge": True})
        out = asyncio.run(pj.project_archive_service(pool3, 3, 9, delete_repo=True))
        check("The Host's own, unshared repository is deleted after the archive, and the project forgets it",
              out.get("repo_deleted") is True and any(c[0] == "DELETE" for c in forge.calls)
              and any("repo = NULL" in q for q in pool3.executed), out)
        info = asyncio.run(pj.project_repository_service(
            ProjPool(row={"id": 9, "name": "Shop", "repo": "entity-9003/shop-p9", "auto_merge": True}), 3, 9))
        check("Auto-Approve is offered where the Host can merge", info["host_can_merge"] is True)
    finally:
        pj_forge.project_forge = _pf_real
finally:
    if _real is not None:
        sys.modules["server.routes.gitea_client"] = _real
    else:
        del sys.modules["server.routes.gitea_client"]

# ── E. seeding, with real git ──────────────────────────────────────────────


def git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env=env or os.environ, check=True)


tmp = Path(tempfile.mkdtemp(prefix="code-approvals-gate-"))
forge_dir = tmp / "forge"
forge_dir.mkdir()
ident = ["-c", "user.email=t@t", "-c", "user.name=t"]


def bare(full):
    d = forge_dir / f"{full}.git"
    d.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(d)], check=True)
    return d


def seed_source(full, files, extra_branch=None):
    d = bare(full)
    w = tmp / ("w-" + full.replace("/", "-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(w)], check=True)
    for k, v in files.items():
        (w / k).write_text(v)
    subprocess.run(["git", *ident, "-C", str(w), "add", "-A"], check=True)
    subprocess.run(["git", *ident, "-C", str(w), "commit", "-qm", "first"], check=True)
    subprocess.run(["git", "-C", str(w), "push", "-q", str(d), "main"], check=True)
    if extra_branch:
        (w / "wip.txt").write_text("unfinished\n")
        subprocess.run(["git", *ident, "-C", str(w), "add", "-A"], check=True)
        subprocess.run(["git", *ident, "-C", str(w), "commit", "-qm", "wip"], check=True)
        subprocess.run(["git", "-C", str(w), "push", "-q", str(d), f"HEAD:refs/heads/{extra_branch}"], check=True)
    return d


seed_source("owner/app", {"README.md": "hello\n", "app.py": "print(1)\n"})
seed_source("owner/resume", {"a.txt": "a\n"}, extra_branch="host/build")
seed_source("owner/closed", {"a.txt": "a\n"}, extra_branch="host/build")   # a closed approval's leftover
bare("entity-9003/app-p21")          # the fork's new, empty repository
bare("owner/empty")


def seed(pid, repo, *, source=None, remote_branch="host/build"):
    return pg._seed_blocking(pid, repo, "main", source, remote_branch)


def status_of(d):
    return subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                          capture_output=True, text=True).stdout.rstrip("\n")


# The forge URL the seed builds, rewritten to the local bare repositories by
# git's own environment config — the code under test is unchanged.
os.environ.update({"GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": f"url.file://{forge_dir}/.insteadOf",
                   "GIT_CONFIG_VALUE_0": "https://entity-9003:tok@git.example/"})
fake = types.ModuleType("server.routes.gitea_client")
fake.gitea_creds = lambda: ("entity-9003", "tok", "git.example")
sys.modules["server.routes.gitea_client"] = fake
pg._PROJECTS_ROOT = tmp / "projects"
try:
    out = seed(20, "owner/app")
    p20 = tmp / "projects" / "20"
    check("A mounted repository's code is the new project's workspace",
          out.get("seeded") and (p20 / "app.py").read_text() == "print(1)\n" and out.get("from") == "main", out)
    head = subprocess.run(["git", "-C", str(p20), "symbolic-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    check("…on the work branch, from the default branch", head == "refs/heads/host/build")
    cfg = (p20 / ".git" / "config").read_text()
    check("…and the token is never on disk", "tok@" not in cfg and "git.example/owner/app.git" in cfg, cfg)
    check("…with nothing staged to delete (the first save point proposes no deletions)",
          status_of(p20) == "", status_of(p20))

    out = seed(22, "owner/resume")
    check("A project's own work branch is continued when its code is read in again (its directory was lost)",
          out.get("from") == "host/build" and (tmp / "projects" / "22" / "wip.txt").is_file(), out)

    def forge_ref(repo, ref):
        return subprocess.run(["git", "--git-dir", str(forge_dir / f"{repo}.git"), "rev-parse", ref],
                              capture_output=True, text=True).stdout.strip()
    left = forge_ref("owner/closed", "host/build")
    out = seed(29, "owner/closed", remote_branch="host/entity-9003/p29")
    check("A mounted project starts on its OWN branch from main — never on work another project left there",
          out.get("from") == "main" and not (tmp / "projects" / "29" / "wip.txt").exists(), out)
    check("…and the branch that other project left is untouched: nothing reset, nothing deleted",
          left and forge_ref("owner/closed", "host/build") == left)
    (tmp / "projects" / "29" / "b.txt").write_text("new work\n")
    saved = pg._sync_blocking(29, "owner/closed", "phase 1: b", [], ["b.txt"], mounted=True,
                              remote_branch="host/entity-9003/p29")
    diff = subprocess.run(["git", "--git-dir", str(forge_dir / "owner/closed.git"), "diff", "--name-status",
                           "main", "host/entity-9003/p29"], capture_output=True, text=True).stdout.strip()
    check("…and its first save point lands on its own branch, carrying only the new work",
          saved.get("pushed") and diff == "A\tb.txt", (saved, diff))

    out = seed(21, "entity-9003/app-p21", source="owner/app")
    filled = subprocess.run(["git", "--git-dir", str(forge_dir / "entity-9003/app-p21.git"), "log", "--oneline", "main"],
                            capture_output=True, text=True).stdout
    check("A fork fills the new repository with the source's history, and the workspace from it",
          out.get("seeded") and out.get("filled") and "first" in filled
          and (tmp / "projects" / "21" / "README.md").is_file(), (out, filled))
    before = subprocess.run(["git", "--git-dir", str(forge_dir / "entity-9003/app-p21.git"), "rev-parse", "main"],
                            capture_output=True, text=True).stdout.strip()
    out = seed(30, "entity-9003/app-p21", source="owner/app")
    after = subprocess.run(["git", "--git-dir", str(forge_dir / "entity-9003/app-p21.git"), "rev-parse", "main"],
                           capture_output=True, text=True).stdout.strip()
    check("Reading a fork again (its work branch was lost) never moves its main: filled once, while empty",
          out.get("seeded") and not out.get("filled") and before == after, (out, before, after))

    out = seed(23, "owner/empty")
    root = subprocess.run(["git", "--git-dir", str(forge_dir / "owner/empty.git"), "log", "--format=%s", "main"],
                          capture_output=True, text=True).stdout.strip()
    check("An empty repository gets a first, empty commit so an approval has a base",
          out.get("seeded") and root == "project start", (out, root))

    # A build landed before the code was read in: its files are kept, the
    # repository's other files are filled in, and only the build's change shows.
    busy = tmp / "projects" / "24"
    busy.mkdir(parents=True)
    (busy / "app.py").write_text("print(2)\n")
    (busy / "feature.py").write_text("new\n")
    out = seed(24, "owner/app")
    st = status_of(busy)
    check("A directory a build wrote into: its files kept, the repository's filled in, nothing to delete",
          out.get("seeded") and out.get("kept_files") and (busy / "app.py").read_text() == "print(2)\n"
          and (busy / "README.md").read_text() == "hello\n"
          and st.splitlines() == [" M app.py", "?? feature.py"], (out, st))

    # A seed that fails leaves the directory as it found it.
    out = seed(26, "owner/nothere")
    check("A failed seed leaves no half-made repository behind",
          not out.get("seeded") and out.get("error") and not (tmp / "projects" / "26" / ".git").exists(), out)

    # A mounted repository: the project's own branch, fresh from main.
    out = seed(27, "owner/app", remote_branch="host/entity-9003/p27")
    check("…and a mounted repository starts fresh on the project's own branch",
          out.get("seeded") and out.get("from") == "main", out)

    # The whole read-in path (_seed_locked), not only its git half: a fork
    # and an empty repository — the two a lookup against an empty forge
    # repository once left pending for good.
    class _SeedDb:
        def __init__(self, cfg):
            self.cfg, self.executed = cfg, []

        async def fetchval(self, sql, *a):
            return self.cfg

        async def execute(self, sql, *a):
            self.executed.append((sql, a))
            return "UPDATE 1"
    bare("entity-9003/app-p31")
    db = _SeedDb({"repo_seed": {"repo": "entity-9003/app-p31", "default_branch": "main",
                                "source": "owner/app", "done": False}})
    r = asyncio.run(pg.seed_project_workspace(db, 31))
    check("A fork is read in end to end: filled from its source, marked done, the commit it copied recorded",
          r.get("pending") is False and r.get("filled")
          and any("repo_seed,done" in q for q, _a in db.executed)
          and any("forked_from" in q and (a or (None,))[-1] == r.get("commit") for q, a in db.executed), (r, db.executed))
    bare("owner/empty2")
    db = _SeedDb({"repo_seed": {"repo": "owner/empty2", "default_branch": "main", "done": False},
                  "work_branch": "host/entity-9003/p32"})
    r = asyncio.run(pg.seed_project_workspace(db, 32))
    check("An empty repository is mounted end to end: first commit made, marked done",
          r.get("pending") is False and forge_ref("owner/empty2", "main") != "", r)

    # The backstop: a mounted project with no work branch saves nothing.
    raw = tmp / "projects" / "28"
    raw.mkdir(parents=True)
    (raw / "only.txt").write_text("build output\n")
    res = pg._sync_blocking(28, "owner/app", "phase 1: x", [], [], mounted=True)
    after = subprocess.run(["git", "--git-dir", str(forge_dir / "owner/app.git"), "branch", "--list"],
                           capture_output=True, text=True).stdout
    check("A mounted project whose code was never read in saves nothing (it would delete the repository)",
          not res.get("committed") and "never read in" in (res.get("error") or "")
          and "host/build" not in after, (res, after))
finally:
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        os.environ.pop(k, None)
    if _real is not None:
        sys.modules["server.routes.gitea_client"] = _real
    else:
        sys.modules.pop("server.routes.gitea_client", None)
    shutil.rmtree(tmp, ignore_errors=True)

# ── E2. the branch, the lookup, and a pending seed ─────────────────────────
check("A project with no recorded branch uses host/build (every project the Host made)",
      pg.work_branch_of({}) == "host/build" and pg.work_branch_of(None) == "host/build")
import re as _re  # noqa: E402
_wb1, _wb2 = pg.mounted_work_branch("entity-9003", 41), pg.mounted_work_branch("entity-9003", 41)
check("A mounted project's own branch is unique even for a re-issued project id, recorded and read back",
      bool(_re.fullmatch(r"host/entity-9003/p41-[0-9a-f]{6}", _wb1)) and _wb1 != _wb2
      and pg.work_branch_of({"work_branch": _wb1}) == _wb1
      and pg.work_branch_of('{"work_branch": "%s"}' % _wb1) == _wb1, (_wb1, _wb2))
_odd = [pg.mounted_work_branch(x, 7) for x in ("foo.lock", "a..b", ".dot.", "", None, "x/y z", "build", "Build")]
check("…and whatever the login, the name is one git accepts — never the shared host/build",
      all(pg.work_branch_of({"work_branch": w}) == w and w != "host/build" for w in _odd), _odd)
check("…and a Host whose login is `build` never gets host/build/… (git refuses it beside host/build)",
      all(not w.lower().startswith("host/build/") for w in _odd[-2:]), _odd[-2:])
from mastercode import pii_queue as _pq  # noqa: E402
check("A merge's chat line keeps its approval number in PII mode (the chat waits for it)",
      _pq.scrub_metadata({"origin": "code_approval", "code_approval": 12, "app_id": "workbench"})
      == {"origin": "code_approval", "code_approval": 12, "app_id": "workbench"})
_ann = (ROOT / "server/routes/code_approvals.py").read_text()
check("…and it is the tag the announce writes",
      '"code_approval": int(number)' in _ann)
check("…and the tag the Workbench chat reads",
      "t.metadata?.code_approval" in (ROOT / "workbench-web/src/components/workbench/chat-pane.tsx").read_text())
check("…and a recorded value that is not a work branch is never used",
      pg.work_branch_of({"work_branch": "main"}) == "host/build"
      and pg.work_branch_of({"work_branch": "host/../x"}) == "host/build")
_src = (ROOT / "brain/mastercode/runtime/tools.py").read_text() + (ROOT / "server/routes/code_approvals.py").read_text() \
    + (ROOT / "brain/mastercode/runtime/support_collect.py").read_text()
check("Every lookup of the project's approval reads the project's own branch",
      _src.count("project_work_branch(pool, project_id)") >= 2 and "work_branch_of(cfg)" in _src
      and "project_work_branch(pool, row[\"id\"])" in _src)
_pj = (ROOT / "server/routes/projects.py").read_text()
_imp = _pj[_pj.index("async def insert_mounted_project("):_pj.index("async def mount_project_repo(")]
check("A mounted project's row, its own branch and its pending read-in are written in ONE transaction",
      "conn.transaction()" in _imp and "INSERT INTO projects" in _imp and "'work_branch'" in _imp
      and "'repo_seed'" in _imp and "insert_mounted_project(pool, _eid()" in _pj)


def pages_of(prs, per=50):
    async def req(method, path, params=None, **kw):
        page = int((params or {}).get("page") or 1)
        return prs[(page - 1) * per: page * per]
    return req


other = [{"number": i, "head": {"ref": f"feature-{i}", "repo_id": 1}, "base": {"repo_id": 1}}
         for i in range(1, 120)]
fork_pr = {"number": 500, "head": {"ref": "host/build", "repo_id": 9, "repo": {"full_name": "mallory/repo"}},
           "base": {"repo_id": 1}}
ours = {"number": 501, "head": {"ref": "host/build", "repo_id": 1, "repo": {"full_name": "owner/repo"}},
        "base": {"repo_id": 1}}
found = asyncio.run(pg.find_work_pr(pages_of([*other, fork_pr, ours]), "owner", "repo", "host/build"))
check("The project's approval is found past the first page, and a fork's same-named branch is never taken",
      (found or {}).get("number") == 501, found)
check("…and none is found when only a fork has the branch",
      asyncio.run(pg.find_work_pr(pages_of([*other, fork_pr]), "owner", "repo", "host/build")) is None)
_fork_row = ca._row({"number": 4, "head": {"ref": "host/b", "repo_id": 9}, "base": {"repo_id": 1}, "state": "open"},
                    {}, set(), branch="host/b", owner="o", name="r")
_own_row = ca._row({"number": 5, "head": {"ref": "host/b", "repo_id": 1}, "base": {"repo_id": 1}, "state": "open"},
                   {}, set(), branch="host/b", owner="o", name="r")
check("The list marks the project's own approval, never a fork's with the same branch name",
      _fork_row["own"] is False and _own_row["own"] is True)
renamed = {"number": 502, "head": {"ref": "host/build", "repo_id": 1, "repo": {"full_name": "owner/repo-new"}},
           "base": {"repo_id": 1}}
check("A renamed repository still finds its approval (the id decides, not the name)",
      (asyncio.run(pg.find_work_pr(pages_of([renamed]), "owner", "repo", "host/build")) or {}).get("number") == 502)


class SeedPool:
    def __init__(self, cfg):
        self.cfg = cfg
        self.executed = []

    async def fetchval(self, sql, *a):
        return self.cfg

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return "UPDATE 1"


_calls = []
_real_seed = pg._seed_blocking
_real_find = pg.find_work_pr


async def _no_open_approval(*a, **k):
    return None


pg._seed_blocking = lambda *a: (_calls.append(a), {"seeded": False, "error": "offline"})[1]
pg.find_work_pr = _no_open_approval
try:
    r = asyncio.run(pg.seed_project_workspace(SeedPool({"repo_seed": {"repo": "owner/app", "done": False}}), 40))
    check("A failed seed stays pending (so builds and save points refuse)",
          r.get("pending") is True and len(_calls) == 1, r)
    _calls.clear()
    r = asyncio.run(pg.seed_project_workspace(SeedPool({"repo_seed": {"repo": "owner/app", "done": True}}), 41))
    check("A finished seed is never run again", r.get("pending") is False and not _calls, r)
    r = asyncio.run(pg.seed_project_workspace(SeedPool({}), 42))
    check("A project that mounted nothing has nothing pending", r.get("pending") is False and not _calls, r)

finally:
    pg._seed_blocking = _real_seed
    pg.find_work_pr = _real_find

# A caller cancelled while the seed runs does not cut it short: it finishes,
# marks the project read in, and releases the lock on its own.
import time as _time  # noqa: E402

_real_seed = pg._seed_blocking


def _slow_seed(*a):
    _time.sleep(0.3)
    return {"seeded": True, "commit": "c" * 40, "from": "main"}


pg._seed_blocking = _slow_seed
pg.find_work_pr = _no_open_approval
try:
    async def _cancel_one():
        pool = SeedPool({"repo_seed": {"repo": "owner/app", "done": False, "source": "owner/x"}})
        waiter = asyncio.ensure_future(pg.seed_project_workspace(pool, 50))
        await asyncio.sleep(0.05)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        task = pg._SEEDS.get(50)
        again = await pg.seed_project_workspace(pool, 50)   # shares the running seed
        await asyncio.sleep(0)
        return pool, task, again
    pool, task, again = asyncio.run(_cancel_one())
    check("A cancelled caller leaves the seed to finish on its own, and the next caller shares it",
          task is not None and again.get("pending") is False
          and any("repo_seed,done" in q for q in pool.executed)
          and not pg.project_lock(50).locked(), (again, pool.executed))
finally:
    pg._seed_blocking = _real_seed
    pg.find_work_pr = _real_find

tsrc = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
blk = tsrc[tsrc.index("_prep = await prepare_workspace(pool, project_id)"):]
check("A build whose project's code is not read in yet does not run",
      "seed_pending" in blk[:600] and blk.index("raise _SeedPending") < blk.index("out = await _pool_build("))

# ── E3. a refusal survives a save point that did not re-check ──────────────
sent = []


async def _req(method, path, params=None, json=None, **kw):
    sent.append((method, path, json))
    if path.endswith("/pulls") and method == "GET":
        return [{"number": 7, "head": {"ref": "host/build", "repo_id": 1, "sha": "a" * 40},
                 "base": {"repo_id": 1}, "body": bad}]
    if method == "GET" and path.endswith("/commits"):
        return []
    if method == "GET":
        return {"default_branch": "main", "id": 1}
    return {"number": 7}


fake2 = types.ModuleType("server.routes.gitea_client")
fake2.gitea_request = _req
fake2.gitea_creds = lambda: ("entity-9003", "t", "h")
fake2.split_repo = lambda r: tuple(r.split("/", 1))
sys.modules["server.routes.gitea_client"] = fake2
try:
    asyncio.run(pg._ensure_pr("entity-9003/p", "P", "host: write notes.md", [], None))
    body_after = next(j["body"] for m, _p, j in sent if m == "PATCH")
    check("A Host file write (the contract not re-run) keeps an earlier refusal",
          ca.has_violations(body_after) and "no build has checked it again since" in body_after, body_after)
    sent.clear()
    asyncio.run(pg._ensure_pr("entity-9003/p", "P", "shop: phase 2: cart", [], None, contract_scope="shop"))
    body_after = next(j["body"] for m, _p, j in sent if m == "PATCH")
    check("A clean check of a module does not clear the root app's refusal",
          ca.has_violations(body_after) and ca.violation_scopes(body_after) == {""}, body_after)
    sent.clear()
    asyncio.run(pg._ensure_pr("entity-9003/p", "P", "phase 2: fix it", [], None, contract_scope=""))
    body_after = next(j["body"] for m, _p, j in sent if m == "PATCH")
    check("…and a clean check of the root app clears it", not ca.has_violations(body_after), body_after)
finally:
    if _real is not None:
        sys.modules["server.routes.gitea_client"] = _real
    else:
        sys.modules.pop("server.routes.gitea_client", None)

# ── F. the chat line is written once ───────────────────────────────────────


class AnnPool:
    def __init__(self):
        self.claimed = False

    async def fetchrow(self, sql, *a):
        return {"id": 1, "name": "P", "description": "", "repo": "entity-9003/p", "config": {}}

    async def fetchval(self, sql, *a):
        if self.claimed:
            return None
        self.claimed = True
        return 1


src = (ROOT / "server/routes/code_approvals.py").read_text()
fn = src[src.index("async def announce_merge("):]
check("The announcement claims the number in the same UPDATE that tests it (one line, never two)",
      "NOT (COALESCE(config->'code_approvals_announced'" in fn and "RETURNING id" in fn
      and fn.index("RETURNING id") < fn.index("post_to_origin_thread"))
check("…and is written from the forge's merge record, not the click",
      "merged_by" in fn and "merged_at" in fn and "merge_commit_sha" in fn)

# ── G. no route merges as the Host ─────────────────────────────────────────
wb = (ROOT / "server/routes/workbench.py").read_text()
check("The Workbench has no merge route", "/merge" not in wb and "def merge_pr" not in wb)
api = (ROOT / "workbench-web/src/lib/api.ts").read_text()
check("…and the app calls none", "/api/workbench/pr/merge" not in api and "mergePr" not in api)
tools = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
check("Auto-Approve's merge writes the same chat line", "announce_merge(pool, entity_id, project_id" in tools)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall gates passed")
