#!/usr/bin/env python3
"""Gate: no Host-reachable writer puts a .git into a project folder, and the
kernel never runs git in a tree whose .git it did not make.

The hole, link by link: the `artifacts` tool's write op wrote any path but an
absolute one, `..` and .history into the artifact's folder, so `.git/config`
and `.gitattributes` were accepted; workspace_file takes an artifact's id as a
project id and, after a write, project_git.sync_project_repo runs `git init`,
`add`, `commit` and `push` in that folder; and HARDENED_GIT_CONFIG names three
keys while git still reads the repo-local config, where a
`[filter "x"] clean = <cmd>` plus `* filter=x` runs <cmd> on `git add` as the
kernel's user. Other keys run programs too (credential.helper,
core.sshCommand, include.path pulling in another file, a .git FILE pointing at
another repository's config).

Both halves are checked here:

  A. writers — an artifact write (and promote, import, revert) of `.git/config`,
     `.GIT/config`, `sub/.git/hooks/x`, through a link to .git, or through a hard
     link to a .git file, never lands in a .git; workspace_file refuses `.GIT`
     and a link into .git as well;
  B. git — project_git.repo_refusal refuses a tree whose .git/config holds a key
     the kernel never writes (each case below), whose .git is a file or a link,
     or that names a commondir; the sync, the review diff, attestation and
     gitea_propose's project read run no git there and the planted command never
     runs; and a repository the kernel made itself (its own sync, its own clone
     shape) passes, so a normal save point still commits and pushes. A folder
     with no .git but laid out as a bare repository (HEAD, objects/, refs/,
     config: ordinary file names) is never taken for one by the hardened git,
     including through propose_commit's whole-tree staging. A project as a
     restore leaves it (host_restore.defuse_git: a config of only its format,
     sha1 or sha256) passes.

Real git, temp folders, a stub pool; no DB, no network, no container.
Fails on c864a678d (APP_ROOT=<export of it>): every planted case runs or is
accepted there.

Run:  python3 scripts/test_git_config_planted.py
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


WORK = Path(tempfile.mkdtemp(prefix="git-planted-")).resolve()
atexit.register(shutil.rmtree, WORK, True)

GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def git(cwd, *args, check_rc=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, text=True)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r


from server.routes import applications as A          # noqa: E402
from server.applications import artifacts as AR      # noqa: E402
from server.routes import project_git as PG          # noqa: E402

PROJECTS = WORK / "projects"
PROJECTS.mkdir()
A.PROJECTS_ROOT = PROJECTS
PG._PROJECTS_ROOT = PROJECTS
# Absent before the fix: every refusal check below then fails on its own and
# the sync checks show what git actually did (the planted command ran).
repo_refusal = getattr(PG, "repo_refusal", lambda tree: None)


class _Pool:
    """The artifact ops read one row (_require_artifact) and stamp updated_at;
    workspace_file reads ownership and kind."""

    async def fetchrow(self, sql="", *a):
        return {"id": a[0] if a else 0, "kind": "artifact"}

    async def fetchval(self, sql="", *a):
        if "kind" in sql:
            return "artifact"
        return 1

    async def execute(self, *a):
        return "UPDATE 1"


POOL = _Pool()


def run(coro):
    return asyncio.run(coro)


def refused(coro) -> bool:
    try:
        run(coro)
    except AR.ArtifactError:
        return True
    return False


PLANT = "[filter \"x\"]\n\tclean = touch {marker}\n"

# ── A. writers ──────────────────────────────────────────────────────────────
print("\nA1. the artifacts write op never writes a .git path")
art = PROJECTS / "501"
art.mkdir()
for p in (".git/config", ".GIT/config", "sub/.git/hooks/x", ".git./config", ".Git/info/attributes"):
    ok = refused(AR.write_files(POOL, 1, 501, [{"path": p, "content": "[core]\n"}]))
    check(f"write {p!r} is refused", ok)
    check(f"...and {p!r} is not on disk", not (art / p).exists())

print("\nA2. not through a link to .git, and not through a hard link into it")
(art / ".git").mkdir(exist_ok=True)
(art / ".git" / "config").write_text("[core]\n\tbare = false\n")
(art / "cfg").symlink_to(".git", target_is_directory=True)
check("write 'cfg/config' (cfg -> .git) is refused",
      refused(AR.write_files(POOL, 1, 501, [{"path": "cfg/config", "content": PLANT}])))
check("...and .git/config is unchanged", (art / ".git" / "config").read_text() == "[core]\n\tbare = false\n")
os.link(art / ".git" / "config", art / "notes.txt")
run(AR.write_files(POOL, 1, 501, [{"path": "notes.txt", "content": "my notes"}]))
check("write to a hard link of .git/config writes only that name",
      (art / ".git" / "config").read_text() == "[core]\n\tbare = false\n"
      and (art / "notes.txt").read_text() == "my notes")

print("\nA3. promote and import refuse .git on either side")
(art / "index.html").write_text("<p>a</p>")
check("promote index.html -> .git/config is refused",
      refused(AR.promote_entry(POOL, 1, 501, "index.html", ".git/config")))
check("promote index.html -> cfg/config (link) is refused",
      refused(AR.promote_entry(POOL, 1, 501, "index.html", "cfg/config")))
src_proj = PROJECTS / "502"
(src_proj / ".git").mkdir(parents=True)
(src_proj / ".git" / "config").write_text(PLANT.format(marker=WORK / "never"))
(src_proj / "page.html").write_text("<p>b</p>")
check("import page.html -> .GIT/config is refused",
      refused(AR.import_files(POOL, 1, 501, 502, [{"from": "page.html", "to": ".GIT/config"}])))
check("import .git/config from a project is refused",
      refused(AR.import_files(POOL, 1, 501, 502, [{"from": ".git/config", "to": "c.txt"}])))

print("\nA4. revert never writes a revision's .git back, and keeps the repository")
art3 = PROJECTS / "503"
(art3 / ".git").mkdir(parents=True)
KERNEL_CFG = "[core]\n\trepositoryformatversion = 0\n"
(art3 / ".git" / "config").write_text(KERNEL_CFG)
(art3 / "index.html").write_text("<p>now</p>")
rev = art3 / ".history" / "0001"
(rev / ".git").mkdir(parents=True)
(rev / "index.html").write_text("<p>then</p>")
(rev / ".git" / "config").write_text(PLANT.format(marker=WORK / "never"))
(art3 / ".history" / "log.jsonl").write_text(
    '{"rev": "0001", "ts": "2026-01-01T00:00:00+00:00", "actor": "host", "op": "write", '
    '"note": "", "files": 2, "bytes": 1, "stored_bytes": 1, "digest": "x", "complete": true}\n')
run(AR.revert_artifact(POOL, 1, 503, "0001"))
check("the revision's content is restored", (art3 / "index.html").read_text() == "<p>then</p>")
_cfg3 = art3 / ".git" / "config"
check("the revision's .git/config is not written back, the repository's stays",
      _cfg3.is_file() and _cfg3.read_text() == KERNEL_CFG,
      _cfg3.read_text() if _cfg3.is_file() else "gone")
newest = sorted(p.name for p in (art3 / ".history").iterdir() if p.name[:1].isdigit())[-1]
check("the new revision holds no .git", not (art3 / ".history" / newest / ".git").exists(), newest)

print("\nA5. workspace_file refuses .GIT and a link into .git in a project")
from server import brain_service                     # noqa: E402
from server.routes import projects as projects_mod   # noqa: E402
from mastercode.runtime import tools as tools_mod    # noqa: E402

projects_mod.PROJECT_FILES_ROOT = PROJECTS


async def _fake_sync(pool, project_id, **kw):
    return {"synced": True}


_real_sync = PG.sync_project_repo
PG.sync_project_repo = _fake_sync
brain_service._pool = POOL
wp = PROJECTS / "504"
(wp / ".git").mkdir(parents=True)
(wp / ".git" / "config").write_text("[core]\n")
(wp / "cfg").symlink_to(".git", target_is_directory=True)
for p in (".GIT/config", "cfg/config", ".git/config"):
    r = run(tools_mod._exec_workspace_file({"op": "write", "project_id": 504, "path": p,
                                            "content": PLANT.format(marker=WORK / "never")}))
    check(f"workspace_file write {p!r} is refused", r.get("status") == "error", str(r)[:160])
check("...and .git/config is unchanged", (wp / ".git" / "config").read_text() == "[core]\n")
check("...and nothing named .GIT was made", not (wp / ".GIT").exists())
r = run(tools_mod._exec_workspace_file({"op": "write", "project_id": 504, "path": "notes/a.md",
                                        "content": "a decision"}))
check("an ordinary workspace_file write still lands", r.get("status") != "error"
      and (wp / "notes" / "a.md").read_text() == "a decision", str(r)[:160])
PG.sync_project_repo = _real_sync

# ── B. git ──────────────────────────────────────────────────────────────────
print("\nB1. a planted repo-local config is refused before any git runs")
BARE = WORK / "forge.git"
git(WORK, "init", "-q", "--bare", "-b", "main", str(BARE))
seedw = WORK / "seedw"
git(WORK, "init", "-q", "-b", "main", str(seedw))
(seedw / "README").write_text("start\n")
git(seedw, "add", "-A")
git(seedw, "commit", "-qm", "start")
git(seedw, "push", "-q", str(BARE), "main")


def machine(bare: Path):
    # The machine-forge shape of _sync_blocking: a URL that is a local bare
    # repository stands in for the loopback relay, so no gitea is needed.
    return {"user": "host", "host": "example.invalid", "env": {},
            "origin": "privatae-machine://box/forge/o/r", "url": str(bare)}


def sync(pid: int, bare: Path = BARE) -> dict:
    # Its caller (sync_project_repo) turns a raise into a failed sync; the
    # gate reads it the same way, so a crash before the fix is a FAIL line.
    try:
        return PG._sync_blocking(pid, "o/r", "save point", [], [], None, None, machine(bare))
    except Exception as e:  # noqa: BLE001
        return {"committed": None, "error": f"raised {e!r}"}


def planted_project(pid: int, cfg_extra: str, attrs: bool = True) -> Path:
    d = PROJECTS / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    git(d, "init", "-q")
    with open(d / ".git" / "config", "a") as fh:
        fh.write(cfg_extra)
    if attrs:
        (d / ".gitattributes").write_text("* filter=x\n")
    (d / "app.js").write_text("console.log(1)\n")
    return d


cases = [
    ("filter.x.clean", lambda m: PLANT.format(marker=m)),
    ("filter.x.process", lambda m: f"[filter \"x\"]\n\tprocess = touch {m}\n"),
    ("credential.helper", lambda m: f"[credential]\n\thelper = !touch {m}\n"),
    ("core.sshcommand", lambda m: f"[core]\n\tsshCommand = touch {m}\n"),
    ("include.path", None),
    ("core.worktree", lambda m: "[core]\n\tworktree = /\n"),
    ("remote.origin.uploadpack", lambda m: f"[remote \"origin\"]\n\tuploadpack = touch {m}\n"),
    ("remote.origin.url", lambda m: "[remote \"origin\"]\n\turl = ext::sh -c touch% /x\n"),
]
pid = 600
for key, make in cases:
    pid += 1
    marker = WORK / f"marker-{pid}"
    if make is None:
        inc = WORK / f"inc-{pid}.cfg"
        inc.write_text(PLANT.format(marker=marker))
        extra = f"[include]\n\tpath = {inc}\n"
    else:
        extra = make(marker)
    d = planted_project(pid, extra)
    why = repo_refusal(d)
    check(f"repo_refusal names {key}", bool(why) and key in (why or "").lower(), str(why))
    out = sync(pid)
    check(f"{key}: the sync commits nothing", out.get("committed") is False, str(out)[:200])
    check(f"{key}: the sync says why", "git was not run" in str(out.get("error")), str(out)[:200])
    check(f"{key}: the planted command never ran", not marker.exists())

# A remote NAMED by a URL: git reads `remote.<that URL>.url` for a push or
# fetch given that URL, so the planted section sends the kernel's own push
# somewhere else. The kernel only ever names its remote `origin`.
real_fwd = WORK / "fwd-real.git"
elsewhere = WORK / "fwd-elsewhere.git"
for b in (real_fwd, elsewhere):
    git(WORK, "init", "-q", "--bare", "-b", "main", str(b))
git(seedw, "push", "-q", str(real_fwd), "main")
fwd_url = f"file://{real_fwd}"
d = planted_project(640, f"[remote \"{fwd_url}\"]\n\turl = {elsewhere}\n", attrs=False)
why = repo_refusal(d)
check("repo_refusal names a remote whose name is a URL",
      bool(why) and f"remote.{fwd_url}.url" in (why or ""), str(why))
mach = {**machine(real_fwd), "url": fwd_url}
try:
    out = PG._sync_blocking(640, "o/r", "save point", [], [], None, None, mach)
except Exception as e:  # noqa: BLE001
    out = {"committed": None, "error": f"raised {e!r}"}
check("a URL-named remote: the sync pushes nothing", out.get("pushed") is not True, str(out)[:200])
check("a URL-named remote: nothing was pushed to where it points",
      git(elsewhere, "for-each-ref", check_rc=False).stdout.strip() == "",
      git(elsewhere, "for-each-ref", check_rc=False).stdout[:200])

print("\nB2. a .git that is a file, a link, or names a commondir is refused")
evil = planted_project(650, PLANT.format(marker=WORK / "marker-650"))
for pid, how in ((651, "file"), (652, "link"), (653, "commondir"), (654, "info link"),
                 (655, "alternates")):
    d = PROJECTS / str(pid)
    d.mkdir()
    (d / ".gitattributes").write_text("* filter=x\n")
    (d / "a.txt").write_text("a\n")
    if how == "file":
        (d / ".git").write_text(f"gitdir: {evil / '.git'}\n")
    elif how == "link":
        (d / ".git").symlink_to(evil / ".git", target_is_directory=True)
    elif how == "commondir":
        git(d, "init", "-q")
        (d / ".git" / "commondir").write_text(str(evil / ".git") + "\n")
    elif how == "info link":
        git(d, "init", "-q")
        shutil.rmtree(d / ".git" / "info")
        (WORK / f"elsewhere-{pid}").mkdir()
        (d / ".git" / "info").symlink_to(WORK / f"elsewhere-{pid}", target_is_directory=True)
    else:
        git(d, "init", "-q")
        (d / ".git" / "objects" / "info" / "alternates").write_text(str(evil / ".git" / "objects") + "\n")
    check(f".git as a {how} is refused", bool(repo_refusal(d)), str(repo_refusal(d)))
    out = sync(pid)
    check(f".git as a {how}: the sync runs no git", out.get("committed") is False
          and "git was not run" in str(out.get("error")), str(out)[:200])
check("the planted command in the other repository never ran", not (WORK / "marker-650").exists())
check("nothing was written through the linked .git/info",
      not any((WORK / "elsewhere-654").iterdir()))

print("\nB3. the other git callers refuse the same tree")
from server.applications import attestation as AT    # noqa: E402
from server.routes import review as RV               # noqa: E402
from server.routes import gitea_propose as GP        # noqa: E402
from fastapi import HTTPException                    # noqa: E402

m = WORK / "marker-670"
d = planted_project(670, PLANT.format(marker=m))
git(d, "-c", "filter.x.clean=cat", "add", "-A")
git(d, "-c", "filter.x.clean=cat", "commit", "-qm", "one")
head = git(d, "-c", "filter.x.clean=cat", "rev-parse", "HEAD").stdout.strip()
(d / "app.js").write_text("console.log(2)\n")
facts = AT.collect_git(d)
check("attestation: no facts read, the reason named",
      facts.get("commit") is None and "filter.x.clean" in str(facts.get("git_error")), str(facts)[:200])
check("review: the host-side diff is empty", RV._worktree_diff_host(d) == "")
try:
    GP._project_changes_blocking(d, head, "main", None)
    gp = "ran"
except HTTPException as e:
    gp = f"{e.status_code} {e.detail}"
check("gitea_propose: the project read is a 409 naming the key",
      gp.startswith("409") and "filter.x.clean" in gp, gp[:200])
check("review base blobs: none read", RV._worktree_base_blobs(d) is None)
check("committable_paths stages nothing", PG.committable_paths(670, ["app.js"]) == [])
check("none of them ran the planted command", not m.exists())

print("\nB4. a repository the kernel made passes, and a save point still works")
# Its own forge copy: the planted cases above must not decide what this one
# can push (before the fix they push to BARE).
BARE2 = WORK / "forge2.git"
git(WORK, "init", "-q", "--bare", "-b", "main", str(BARE2))
git(seedw, "push", "-q", str(BARE2), "main")
d = PROJECTS / "690"
d.mkdir()
(d / "index.html").write_text("<p>one</p>")
out = sync(690, BARE2)
check("first sync on a fresh folder commits and pushes",
      out.get("committed") is True and out.get("pushed") is True, str(out)[:200])
check("its .git passes the check", repo_refusal(d) is None, str(repo_refusal(d)))
keys = git(d, "config", "--file", ".git/config", "--list", "--name-only").stdout.split()
check("the kernel's own sync wrote branch tracking (allowlisted)",
      any(k.startswith("branch.") for k in keys), str(keys))
(d / "index.html").write_text("<p>two</p>")
out = sync(690, BARE2)
check("a second sync commits and pushes", out.get("committed") is True and out.get("pushed") is True,
      str(out)[:200])
cl = WORK / "clone"
git(WORK, "-c", "protocol.file.allow=always", "clone", "-q", "--no-tags", "--depth", "1",
    f"file://{BARE2}", str(cl))
git(cl, "remote", "set-url", "origin", "https://forge.example.invalid/o/r.git")
check("a clone's config (as import_repo / repo_sync make it) passes",
      repo_refusal(cl) is None, str(repo_refusal(cl)))

print("\nB5. a folder with no .git, laid out as a bare repository, is not taken for one")
# git looks for `.git` first and then asks whether the folder ITSELF is a
# repository (HEAD, objects/, refs/): a bare one, whose config is the folder's
# own `config` file. There is no .git for repo_refusal to read, and every name
# here is an ordinary file name a Host or a harness pick may write.
BARE_SHAPE = {"HEAD": "ref: refs/heads/main\n", "objects/keep": "", "refs/keep": "",
              ".gitattributes": "* filter=x\n", "app.js": "console.log(1)\n"}


def bare_config(marker: Path) -> str:
    return ("[core]\n\trepositoryformatversion = 0\n\tworktree = .\n"
            + PLANT.format(marker=marker))


m = WORK / "marker-bare-direct"
d = PROJECTS / "695"
for rel, text in {**BARE_SHAPE, "config": bare_config(m)}.items():
    (d / rel).parent.mkdir(parents=True, exist_ok=True)
    (d / rel).write_text(text)
env_h = {**GIT_ENV}
r1 = subprocess.run(["git", *PG.HARDENED_GIT_CONFIG, "-C", str(d), "rev-parse", "--git-dir"],
                    env=env_h, capture_output=True, text=True)
check("hardened git does not take the folder for a bare repository",
      r1.returncode != 0, (r1.stdout + r1.stderr)[:200])
subprocess.run(["git", *PG.HARDENED_GIT_CONFIG, "-C", str(d), "add", "-A"],
               env=env_h, capture_output=True)
check("...so its `config` runs nothing on `add`", not m.exists())

# The same layout through review.propose_commit, the whole-tree pick: a fresh
# project (never synced, so no .git) gets the picked files, then its staging
# asks `rev-parse --git-dir` and runs `add -A` there.
m = WORK / "marker-bare-pick"
sess = WORK / "sessions"
sd = sess / "s-bare"
hwt = sd / "work" / "codex"
for rel, text in {**BARE_SHAPE, "config": bare_config(m)}.items():
    (hwt / rel).parent.mkdir(parents=True, exist_ok=True)
    (hwt / rel).write_text(text)
proj = PROJECTS / "696"
proj.mkdir()
(sd / "manifest.json").write_text(
    '{"session_id": "s-bare", "project_dir": "%s", "worktree_root": "%s", '
    '"variants": [{"vid": "codex", "harness": "codex"}]}' % (proj, sd / "work"))
_saved_sessions = RV.SESSIONS_ROOT
RV.SESSIONS_ROOT = sess
try:
    picks = {rel: "codex" for rel in [*BARE_SHAPE, "config"]}
    try:
        out = run(RV.propose_commit(RV.ProposeCommitReq(session_id="s-bare", picks=picks)))
    except Exception as e:  # noqa: BLE001
        out = {"error": repr(e)[:200]}
finally:
    RV.SESSIONS_ROOT = _saved_sessions
check("propose_commit: the picks land", sorted(out.get("applied") or []) == sorted(picks), str(out)[:200])
check("propose_commit: the planted `config` ran nothing while staging", not m.exists(), str(out)[:200])
check("propose_commit: staging made the project its own .git and read the diff from it",
      (proj / ".git" / "HEAD").is_file() and "+console.log(1)" in str(out.get("staged_diff")),
      str(out)[:300])

print("\nB6. a config changed after a pass is read again")
with open(cl / ".git" / "config", "a") as fh:
    fh.write(PLANT.format(marker=WORK / "never"))
check("the cached pass does not hide a later plant", bool(repo_refusal(cl)))

print("\nB7. a project as a restore leaves it passes; its planted config did not survive")
# A restore resets every Git directory of a backup to a config of only its
# format (host_restore.defuse_git); a sha256 repository keeps
# extensions.objectformat. The kernel wrote that config, so the sync must
# not refuse the restored project for it.
from server import host_restore as HR                # noqa: E402
staged = WORK / "staged"
restored = {}
for fmt in ("sha1", "sha256"):
    d = staged / "projects" / fmt
    d.mkdir(parents=True)
    git(d, "init", "-q", f"--object-format={fmt}")
    with open(d / ".git" / "config", "a") as fh:
        fh.write(PLANT.format(marker=WORK / "never"))
    restored[fmt] = d
check("a backup's planted project config is refused before the reset",
      all(repo_refusal(d) for d in restored.values()))
reset = HR.defuse_git(staged)
check("the reset found both Git directories",
      sorted(reset) == ["projects/sha1/.git", "projects/sha256/.git"], str(reset))
for fmt, d in restored.items():
    check(f"a restored {fmt} project's .git passes the check", repo_refusal(d) is None,
          str(repo_refusal(d)))
check("no planted command ran anywhere in this gate", not (WORK / "never").exists())

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: " + "; ".join(FAILS))
    sys.exit(1)
print("all checks passed")
