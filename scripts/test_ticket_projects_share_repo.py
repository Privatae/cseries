#!/usr/bin/env python3
"""Gate: ticket projects share a Code Repository, each on its own branch (v199).

Shared repo, own branch: every ticket is its own project
(projects.config.ticket, or config.support_ticket), and many tickets are about
one repository. A ticket's project mounts that repository after it is made
(update_project repo=..., projects.mount_ticket_repo_service) and works there
on a branch named for its ticket, with its own Code Approval. Every other
project keeps the rule of one live project per repository.

  T1. two ticket projects mount one repository at once, on different
      branches, each named for its ticket;
  T2. mounting again is answered as done, and changes nothing;
  T3. a project that is not a ticket's cannot be mounted after it is made,
      a read-only repository is refused, and so is a second repository;
  T4. the database still refuses two live NON-ticket projects on one
      repository, and the start dialog's check still offers a fork when one
      holds it — but ticket projects there hold nothing;
  T5. the database refuses two live projects on one branch of a repository;
  T6. a ticket project with no repository makes no empty one: the build door
      is refused with the reason, and a save point saves nothing.

Runs on the entity's own database in the smoke container (the socket at
/var/run/postgresql), or on any database carrying init-db.sql + migrations:

  CSERIES_TEST_PG_HOST=<dir holding .s.PGSQL.5432> CSERIES_TEST_PG_DB=<db> \\
      APP_ROOT=<repo> python3 scripts/test_ticket_projects_share_repo.py

With no database to reach, only the static checks run. The last line is
OK... / FAIL... (smoke reads it).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT), str(ROOT / "brain")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from server.routes import projects as P  # noqa: E402
from server.routes import project_git as G  # noqa: E402
from server.routes import gitea_client as GC  # noqa: E402

REPO = "alex/project"
mig = ROOT / "migrations" / "v199_ticket_projects_share_a_repository.sql"
check("v199 exists and keeps v191's NOTICE-not-fail shape",
      mig.exists() and "RAISE NOTICE" in mig.read_text())
check("init-db.sql declares the branch index for fresh entities",
      "uq_projects_live_repo_branch" in (ROOT / "init-db.sql").read_text())

# ── stubs: the forge ─────────────────────────────────────────────────────────
FORGE = {"alex/project": {"push": True}, "alex/readonly": {"push": False},
         "alex/other": {"push": True}}
CALLS: list = []


async def _access(repo):
    perms = FORGE.get(repo.lower())
    if perms is None:
        raise P.ProjectsError(404, f"The Host can't see the Code Repository {repo}.")
    return {"full_name": repo, "owner": repo.split("/")[0], "name": repo.split("/")[1],
            "owned_by_host": False, "can_write": perms["push"], "private": True,
            "empty": False, "mirror": False, "default_branch": "main",
            "description": "", "updated_at": None}


async def _mount(pool, project_id, mount):
    CALLS.append(("seed", project_id, mount["repo"]))
    return {"repo": mount["repo"], "created": False, "mounted": True}


# Merged pull requests on alex/project, by head branch (T9 fills it).
MERGED: list = []


async def _gitea(method, path, **kw):
    CALLS.append(("gitea", method, path))
    if method == "GET" and path.endswith("/pulls"):
        state = (kw.get("params") or {}).get("state")
        return [] if state == "open" else list(MERGED)
    return {"full_name": "entity-x/made-for-nothing"}


P.host_repo_access = _access
P._host_login = lambda: "entity-x"
P.mount_project_repo = _mount
# Any read-in into the pod, by either door (the background one a mount used,
# and the one a build's prepare runs).
G.seed_in_background = lambda pool, pid: CALLS.append(("seed", pid, "background"))


async def _seed_now(pool, pid):
    CALLS.append(("seed", pid, "build"))
    await pool.execute("UPDATE projects SET config = jsonb_set(config, '{repo_seed,done}', "
                       "'true'::jsonb) WHERE id = $1", pid)
    return {"seeded": True, "pending": False}


G.seed_project_workspace = _seed_now
_REAL_PREPARE = G._prepare_blocking      # T10 runs the real one, with real git
G._prepare_blocking = lambda pid, repo, machine=None, branch=None: (
    CALLS.append(("prepare", pid, repo, branch)) or {"prepared": True})
GC.gitea_creds = lambda: ("entity-x", "tok", "git.example")
GC.gitea_request = _gitea
from server.routes import code_approvals as CA  # noqa: E402


async def _no_origin(_eid):
    return ""          # the real one asks the bridge; nothing leaves the box here


CA.host_origin = _no_origin

# T7 — a ticket's branch is always the branch the code reads back. A name
# work_branch_of refuses is read as host/build, which every such project would
# then share on the one repository, with the branch index (keyed on the
# recorded name) never seeing it.
for _label in ("ticket/ABC-101", "ticket/x.lock./T-1", "ticket/a.lock-/b", "ticket/T-1.lock.",
               "ticket/..", "ticket/support-12"):
    _b = G.mounted_work_branch("entity-x", 7, _label)
    check(f"T7: the branch for {_label!r} is read back as itself, never host/build",
          G.work_branch_of({"work_branch": _b}) == _b and _b != "host/build", _b)


def _git_gate():
    """T10 — a project's copy holds its default branch and its own work branch,
    never another project's: on a repository every ticket shares, `refs/heads/*`
    would copy each of their branches into each of them. Real git, against bare
    repositories standing in for the forge (the URL the code builds is rewritten
    by git's own environment config; the code under test is unchanged)."""
    import shutil
    import subprocess
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ticket-share-git-"))
    saved_env = {k: os.environ.get(k) for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                                                 "GIT_CONFIG_VALUE_0")}
    saved_root = G._PROJECTS_ROOT
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]

    def sh(*a, cwd=None):
        return subprocess.run(["git", *ident, *a], cwd=cwd, capture_output=True, text=True,
                              check=True).stdout.strip()

    try:
        forge = tmp / "forge" / "alex" / "project.git"
        forge.parent.mkdir(parents=True)
        sh("init", "-q", "--bare", "-b", "main", str(forge))
        w = tmp / "w"
        sh("init", "-q", "-b", "main", str(w))
        (w / "README.md").write_text("platform\n")
        sh("add", "-A", cwd=w)
        sh("commit", "-qm", "main", cwd=w)
        sh("push", "-q", str(forge), "main", cwd=w)
        own = "host/entity-x/ticket/ABC-101-aaaaaa"
        others = {}
        for i, ref in enumerate([own] + [f"host/entity-x/ticket/T-{3000 + n}-bbbbbb"
                                         for n in range(5)]):
            sh("checkout", "-q", "-B", f"b{i}", "main", cwd=w)
            (w / f"work{i}.bin").write_bytes(os.urandom(64 * 1024))
            sh("add", "-A", cwd=w)
            sh("commit", "-qm", f"work {i}", cwd=w)
            sh("push", "-q", str(forge), f"HEAD:refs/heads/{ref}", cwd=w)
            if ref != own:
                others[ref] = sh("rev-parse", f"HEAD:work{i}.bin", cwd=w)
        os.environ.update({"GIT_CONFIG_COUNT": "1",
                           "GIT_CONFIG_KEY_0": f"url.file://{tmp / 'forge'}/.insteadOf",
                           "GIT_CONFIG_VALUE_0": "https://entity-x:tok@git.example/"})
        G._PROJECTS_ROOT = tmp / "projects"

        def remotes(pid):
            return sorted(sh("for-each-ref", "--format=%(refname)", "refs/remotes/",
                             cwd=tmp / "projects" / str(pid)).split())

        def holds_any(pid):
            d = tmp / "projects" / str(pid)
            return [r for r, blob in others.items()
                    if subprocess.run(["git", "-C", str(d), "cat-file", "-e", blob],
                                      capture_output=True).returncode == 0]

        out = G._seed_blocking(41, "alex/project", "main", None, own)
        check("T10: a ticket's copy is read in from its own work branch",
              out.get("seeded") and out.get("from") == own
              and (tmp / "projects" / "41" / "work0.bin").is_file(), repr(out))
        check("T10: ...and holds the default branch and its own branch, nothing else",
              remotes(41) == sorted([f"refs/remotes/origin/{own}", "refs/remotes/origin/main"]),
              repr(remotes(41)))
        check("T10: ...and none of another ticket's objects", holds_any(41) == [],
              repr(holds_any(41)))

        new_branch = "host/entity-x/ticket/ABC-102-cccccc"     # not on the forge yet
        out = G._seed_blocking(42, "alex/project", "main", None, new_branch)
        check("T10: a first mount (no work branch yet) starts from the default branch",
              out.get("seeded") and out.get("from") == "main", repr(out))
        check("T10: ...holding only the default branch",
              remotes(42) == ["refs/remotes/origin/main"] and holds_any(42) == [],
              f"{remotes(42)} {holds_any(42)}")

        (tmp / "projects" / "42" / "fix.txt").write_text("the fix\n")
        synced = G._sync_blocking(42, "alex/project", "phase 1", [], ["fix.txt"],
                                  remote_branch=new_branch, mounted=True)
        check("T10: a save point pushes the ticket's own branch",
              synced.get("pushed") is True
              and subprocess.run(["git", "--git-dir", str(forge), "rev-parse", "--verify", "-q",
                                  f"refs/heads/{new_branch}"], capture_output=True).returncode == 0,
              repr(synced))
        check("T10: ...and fetches no other ticket's branch",
              holds_any(42) == [] and all("T-30" not in r for r in remotes(42)), repr(remotes(42)))

        prep = _REAL_PREPARE(41, "alex/project", None, own)
        check("T10: before a build, the copy is brought to its own branch",
              prep.get("prepared") is True, repr(prep))
        check("T10: ...fetching no other ticket's branch",
              holds_any(41) == [] and all("T-30" not in r for r in remotes(41)), repr(remotes(41)))
    finally:
        G._PROJECTS_ROOT = saved_root
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


_git_gate()


async def _db_gate(host: str):
    import asyncpg
    pool = await asyncpg.create_pool(host=host, user=os.environ.get("CSERIES_TEST_PG_USER", "entity"),
                                     database=os.environ.get("CSERIES_TEST_PG_DB")
                                     or os.environ.get("SMOKE_PG_DB", "hostmem"),
                                     min_size=1, max_size=3)
    eid = 990_199
    mount = getattr(P, "mount_ticket_repo_service", None)

    async def new(name, cfg=None, repo=None):
        return await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status, config, repo) "
            "VALUES ($1, $2, 'planning', $3::jsonb, $4) RETURNING id",
            eid, name, json.dumps(cfg or {}), repo)

    async def branch(pid):
        return json.loads(await pool.fetchval("SELECT config FROM projects WHERE id=$1", pid)
                          ).get("work_branch")

    try:
        await pool.execute("DELETE FROM projects WHERE entity_id = $1", eid)
        a = await new("ABC-101 — resource axes", {"ticket": {"ref": "ABC-101"}})
        b = await new("Ticket #12", {"support_ticket": 12})
        plain = await new("Platform")

        # T1
        if mount is None:
            check("T1: a ticket project can be mounted after it is made", False,
                  "no mount_ticket_repo_service")
        else:
            ra = await mount(pool, eid, a, REPO)
            rb = await mount(pool, eid, b, REPO)
            ba, bb = await branch(a), await branch(b)
            repos = [r["repo"] for r in await pool.fetch(
                "SELECT repo FROM projects WHERE id = ANY($1::int[]) ORDER BY id", [a, b])]
            check("T1: two ticket projects are mounted on one repository at once",
                  repos == [REPO, REPO], repr(repos))
            check("T1: each on its own branch, named for its ticket",
                  bool(ba and bb and ba != bb
                       and re.fullmatch(r"host/entity-x/ticket/ABC-101-[0-9a-f]{6}", ba)
                       and re.fullmatch(r"host/entity-x/ticket/support-12-[0-9a-f]{6}", bb)),
                  f"{ba} {bb}")
            check("T1: each branch is one the save point and Code Approval will use",
                  G.work_branch_of({"work_branch": ba}) == ba and ra.get("work_branch") == ba
                  and rb.get("work_branch") == bb, f"{ra} {rb}")
            seeded = [c for c in CALLS if c[0] == "seed"]
            check("T1: a ticket's mount reads nothing into the pod", seeded == [], repr(seeded))

            # T2
            again = await mount(pool, eid, a, "Alex/Project.git")
            check("T2: mounting the same repository again is answered as done",
                  again.get("already") is True and await branch(a) == ba, repr(again))
            check("T2: ...and reads nothing in", [c for c in CALLS if c[0] == "seed"] == [])

            # T12 — the ticket's first build reads its code in, then works on its own branch
            prep = await G.prepare_workspace(pool, a)
            got = [c for c in CALLS if c[0] in ("seed", "prepare") and c[1] == a]
            check("T12: the first build reads the ticket's code in, then prepares its own branch",
                  prep.get("prepared") is True
                  and got == [("seed", a, "build"), ("prepare", a, REPO, ba)], f"{prep} {got}")
            CALLS[:] = [c for c in CALLS if c[0] != "prepare"]

            # T3
            ro = await new("T-1 — ro", {"ticket": {"ref": "T-1"}})
            for what, pid, repo in (
                    ("a project that is not a ticket's is refused", plain, REPO),
                    ("a second repository on a mounted ticket project is refused", a, "alex/other"),
                    ("a repository the Host can only read is refused", ro, "alex/readonly")):
                try:
                    await mount(pool, eid, pid, repo)
                    check(f"T3: {what}", False, "mounted")
                except P.ProjectsError as e:
                    check(f"T3: {what} (409)", e.status == 409, f"{e.status} {e.detail}")
            check("T3: the refused non-ticket project still has no repository",
                  await pool.fetchval("SELECT repo FROM projects WHERE id=$1", plain) is None)

            # update_project carries the mount (the Host tool and PUT share it)
            c = await new("ABC-103 — exec plan", {"ticket": {"ref": "ABC-103"}})
            r = await P.update_project_service(pool, eid, c, repo=REPO)
            check("update_project(repo=...) mounts a ticket project",
                  bool(r.get("updated")) and (r.get("repository") or {}).get("repo") == REPO, repr(r))

            # T8 — an archived ticket's project is not read in; a restore carries its mount
            arch = await new("ABC-104 — archived", {"ticket": {"ref": "ABC-104"}})
            await pool.execute("UPDATE projects SET status='archived' WHERE id=$1", arch)
            seeds = len([c for c in CALLS if c[0] == "seed"])
            try:
                await mount(pool, eid, arch, REPO)
                check("T8: an archived ticket project is not mounted", False, "mounted")
            except P.ProjectsError as e:
                check("T8: an archived ticket project is not mounted (409)", e.status == 409,
                      f"{e.status} {e.detail}")
            check("T8: ...it keeps no repository and nothing is read in",
                  await pool.fetchval("SELECT repo FROM projects WHERE id=$1", arch) is None
                  and len([c for c in CALLS if c[0] == "seed"]) == seeds)
            try:
                r = await P.update_project_service(pool, eid, arch, status="planning", repo=REPO)
                st = await pool.fetchrow("SELECT status, repo FROM projects WHERE id=$1", arch)
                check("T8: restoring and mounting in one update_project call does both",
                      (r.get("repository") or {}).get("repo") == REPO
                      and st["status"] == "planning" and st["repo"] == REPO, f"{r} {dict(st)}")
            except P.ProjectsError as e:
                check("T8: restoring and mounting in one update_project call does both", False,
                      e.detail)

            # T9 — a ticket's chat announces its own merge, never another ticket's
            await pool.execute(
                "UPDATE projects SET config = config || jsonb_build_object("
                "'code_approvals_since', '2000-01-01T00:00:00Z') WHERE id = ANY($1::int[])", [a, b])
            MERGED[:] = [
                {"number": n, "merged": True, "state": "closed", "title": f"fix {n}",
                 "merged_at": "2026-10-03T10:00:00Z", "body": "",
                 "head": {"ref": ref, "repo_id": 1}, "base": {"ref": "main", "repo_id": 1}}
                for n, ref in ((501, ba), (502, bb), (503, "feat/someone-elses-change"))]
            lst = await CA.list_approvals(pool, eid, a)
            owed = sorted(x["number"] for x in lst["approvals"] if x["announce"])
            check("T9: a ticket project announces only its own merged change",
                  owed == [501], f"announce owed for {owed}")
            check("T9: ...and still lists the others, its own first",
                  [x["number"] for x in lst["approvals"]][:1] == [501] and len(lst["approvals"]) == 3,
                  repr([(x["number"], x["own"]) for x in lst["approvals"]]))
            MERGED.clear()

            # T11 — a mount with other fields lands whole or not at all
            t11 = await new("ABC-105 — atomic", {"ticket": {"ref": "ABC-105"}})
            seeds = len([c for c in CALLS if c[0] == "seed"])
            try:
                await P.update_project_service(pool, eid, t11, name="renamed", bucket_id=987654321,
                                               repo=REPO)
                check("T11: a refused field refuses the mount too", False, "accepted")
            except P.ProjectsError as e:
                st = await pool.fetchrow("SELECT name, repo FROM projects WHERE id=$1", t11)
                check("T11: a refused field refuses the mount too — nothing written, nothing read in",
                      e.status == 404 and st["repo"] is None and st["name"] == "ABC-105 — atomic"
                      and len([c for c in CALLS if c[0] == "seed"]) == seeds,
                      f"{e.status} {dict(st)}")
            real_access = P.host_repo_access

            async def _archived_meanwhile(repo):
                await pool.execute("UPDATE projects SET status='archived' WHERE id=$1", t11)
                return await real_access(repo)
            P.host_repo_access = _archived_meanwhile
            try:
                await P.update_project_service(pool, eid, t11, name="renamed", repo=REPO)
                check("T11: archived during the mount — the rename does not land alone", False,
                      "accepted")
            except P.ProjectsError as e:
                st = await pool.fetchrow("SELECT name, repo FROM projects WHERE id=$1", t11)
                check("T11: archived during the mount — the rename does not land alone",
                      e.status == 409 and st["repo"] is None and st["name"] == "ABC-105 — atomic",
                      f"{e.status} {dict(st)}")
            finally:
                P.host_repo_access = real_access
            await pool.execute("UPDATE projects SET status='planning' WHERE id=$1", t11)
            r = await P.update_project_service(pool, eid, t11, name="renamed", repo=REPO)
            st = await pool.fetchrow("SELECT name, repo FROM projects WHERE id=$1", t11)
            check("T11: rename + mount together land together, and nothing is read in yet",
                  st["name"] == "renamed" and st["repo"] == REPO
                  and (r.get("repository") or {}).get("repo") == REPO
                  and len([c for c in CALLS if c[0] == "seed"]) == seeds, f"{r} {dict(st)}")

            # T13 — a compare never runs a ticket from an empty directory
            ready = getattr(G, "ticket_code_ready", None)
            if ready is None:
                check("T13: a compare reads a ticket's code in first", False, "no ticket_code_ready")
            else:
                t13 = await new("ABC-106 — compare", {"ticket": {"ref": "ABC-106"}})
                await mount(pool, eid, t13, REPO)
                why = await ready(pool, t13)
                check("T13: a compare reads a mounted ticket's code in first",
                      why is None and ("seed", t13, "build") in CALLS, f"{why} {CALLS[-3:]}")
                bare_t = await new("ABC-107 — unmounted", {"ticket": {"ref": "ABC-107"}})
                why = await ready(pool, bare_t)
                check("T13: an unmounted ticket's compare is refused with the reason to mount",
                      bool(why) and "mount" in why, repr(why))
                t15 = await new("ABC-108 — unreadable", {"ticket": {"ref": "ABC-108"}})
                await mount(pool, eid, t15, REPO)
                real_seed = G.seed_project_workspace

                async def _fails(pool_, pid):
                    return {"seeded": False, "pending": True, "error": "forge down"}
                G.seed_project_workspace = _fails
                try:
                    why = await ready(pool, t15)
                finally:
                    G.seed_project_workspace = real_seed
                check("T13: ...and one whose code cannot be read in is refused, saying why",
                      bool(why) and "forge down" in why, repr(why))
                n_seed = len([c for c in CALLS if c[0] == "seed"])
                other = await new("Not a ticket", {"repo_seed": {"repo": "o/r", "done": False}},
                                  repo="o/r")
                check("T13: any other project is left exactly as it was",
                      await ready(pool, other) is None
                      and len([c for c in CALLS if c[0] == "seed"]) == n_seed)
            src = (ROOT / "server" / "routes" / "review.py").read_text()
            fo = src[src.index("async def fan_out("):]
            fo = fo[:fo.index("\nasync def ")]
            check("T13: fan_out (a compare) asks before it makes a session",
                  "ticket_code_ready(" in fo
                  and fo.index("ticket_code_ready(") < fo.index("session_id = uuid.uuid4().hex"))

        # T4 — the one-live-per-repository rule for everything else
        holder = await new("Main project", repo=REPO)
        try:
            await new("Second main project", repo=REPO)
            check("T4: the database refuses a second live non-ticket project on one repository",
                  False, "inserted")
        except Exception as e:  # noqa: BLE001
            check("T4: the database refuses a second live non-ticket project on one repository",
                  "unique" in type(e).__name__.lower(), repr(e))
        try:
            await P.plan_repo_mount(pool, eid, REPO)
            check("T4: the start dialog still offers a fork when a project holds it", False, "allowed")
        except P.ProjectsError as e:
            check("T4: the start dialog still offers a fork when a project holds it",
                  e.status == 409 and "Fork" in e.detail, e.detail)
        await pool.execute("UPDATE projects SET status='archived' WHERE id=$1", holder)
        tickets_there = await pool.fetchval(
            "SELECT count(*) FROM projects WHERE entity_id=$1 AND repo=$2 AND status<>'archived'",
            eid, REPO)
        try:
            m = await P.plan_repo_mount(pool, eid, REPO)
            check("T4: ticket projects on a repository hold nothing — a new project may mount it",
                  tickets_there >= 2 and m.get("repo") == REPO and not m.get("fork"),
                  f"{tickets_there} live ticket projects there; {m!r}")
        except P.ProjectsError as e:
            check("T4: ticket projects on a repository hold nothing — a new project may mount it",
                  False, e.detail)

        # T5 — one branch, one live project
        cfg = {"ticket": {"ref": "T-5"}, "work_branch": "host/entity-x/ticket/T-5-aaaaaa"}
        await new("T-5 — one", cfg, repo=REPO)
        try:
            await new("T-5 — two", dict(cfg, ticket={"ref": "T-5b"}), repo=REPO)
            check("T5: the database refuses two live projects on one branch of a repository",
                  False, "inserted")
        except Exception as e:  # noqa: BLE001
            check("T5: the database refuses two live projects on one branch of a repository",
                  "unique" in type(e).__name__.lower(), repr(e))

        # T6 — no empty repository for a ticket
        d = await new("ABC-109 — leftover volumes", {"ticket": {"ref": "ABC-109"}})
        CALLS.clear()
        made = await P.ensure_project_repo(pool, d, "ABC-109 — leftover volumes")
        check("T6: ensure_project_repo makes no repository for a ticket project",
              made.get("repo") is None and not [c for c in CALLS if c[0] == "gitea"],
              f"{made} {CALLS}")
        prep = await G.prepare_workspace(pool, d)
        check("T6: the build door is refused, with the reason to mount first",
              prep.get("seed_pending") is True and "mount" in (prep.get("error") or ""), repr(prep))
        synced = await G.sync_project_repo(pool, d, phase_label="phase 1")
        check("T6: a save point saves nothing and says to mount first",
              synced.get("synced") is False and "mount" in (synced.get("error") or "")
              and not [c for c in CALLS if c[0] == "gitea"], repr(synced))
        check("T6: the project still has no repository",
              await pool.fetchval("SELECT repo FROM projects WHERE id=$1", d) is None)

        # A project that is not a ticket's keeps getting its own repository.
        e2 = await new("A new app")
        made = await P.ensure_project_repo(pool, e2, "A new app")
        check("T6: an ordinary project still gets its own new repository",
              made.get("created") is True and any(c[:2] == ("gitea", "POST") for c in CALLS), repr(made))
    finally:
        await pool.execute("DELETE FROM projects WHERE entity_id = $1", eid)
        await pool.close()


pg_host = (os.environ.get("CSERIES_TEST_PG_HOST") or os.environ.get("SMOKE_PG_HOST")
           or ("/var/run/postgresql" if Path("/var/run/postgresql/.s.PGSQL.5432").exists() else ""))
if pg_host:
    asyncio.run(_db_gate(pg_host))
else:
    print("  skip  real-database gate (no database socket; set CSERIES_TEST_PG_HOST to run it)")

print("OK: ticket projects share a repository, each on its own branch" if not fails
      else f"FAIL: ticket projects share a repository — {fails} check(s) failed")
sys.exit(1 if fails else 0)
