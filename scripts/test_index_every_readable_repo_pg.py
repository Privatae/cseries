#!/usr/bin/env python3
"""
Gate: every repo the Host can read on its gitea is in its code index, and
search_my_code searches all of it at once (codesearch/repo_sync.py,
migrations/v224).

search_my_code covers the Host's repos and its user's; if the Host
can read a repo, it is indexed; the Host reads its OWN repos itself, and an
agent is only for reading a foreign repo.

A real Postgres carrying the kernel's schema (init-db.sql + every migration),
a fake forge (the /user/repos listing and branch heads the Host's gitea
answers, served from bare git repos on disk that the real clone/fetch code
reads over file://), the real indexer and store, a counting fake embedder:

  G1  one pass indexes the Host's own repo AND the user's repo granted to it;
      both are searchable, each hit naming its repo
  G2  a pass where no head moved changes nothing and embeds nothing
  G3  a push to one file re-indexes that file only; the new text is found,
      the old is gone
  G4  read access gone (the forge stops listing it): its rows, its checkout
      and its record go; the other repo and the Workbench project stay
  G5  a pass whose listing failed changes nothing — never read as "no access"
  F1  a foreign repo (on the forge, not granted; or listed without read) is
      never cloned or indexed; embed_jobs and an old queued job refuse it
  F2  rows of a repo the forge does not list (an old job's clone) are dropped
  D1  a repo too big for the room on the disk is skipped with the reason,
      never cloned, and search_my_code says it is not fully indexed
  L1  no embedder answering: the repo is still written and found by keyword
      search (the tsvector arm alone); a later pass with an embedder fills
      the vectors and records the head
  S1  search_my_code with no project searches projects AND repos; a project
      argument narrows to that one
  Z1  a repo whose checkout expands far past its pack (a file of zeros) is
      skipped with the reason before any of its work tree is written, is not
      fetched again at the same head, and is indexed once the room is there
  Z2  a fetch whose pack outgrows the room (the forge's size understated it)
      is cut off by git's file-size limit: skipped, nothing left on the disk
  Z3  a tree that fits but whose index rows would not (many small files) is
      skipped with the reason; search_my_code names it
  Z4  nested trees of a few KiB naming 22,500 empty files: each file costs
      its .git/index entry; the listing is read as it streams and git is
      stopped once past the room; more files than the disk's free inodes
      allow is skipped with the reason, nothing written
  K1  links to /etc/passwd, /etc, ../../ are written as links (even with a
      global core.symlinks=false) and never read into the index
  K2  a .gitattributes naming filters (defined in the global config, as
      git-lfs is), ident and eol=crlf, plus a global hooks path: no filter or
      hook runs, every file is written as its blob, an LFS pointer is indexed
      as its small text
  K3  the repos root is open to the Host's own user only (0700)
  P2  a grant made in the Gitea app survives a restart before the next pass

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a docker throwaway. Prints RESULT: ALL PASS.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database for code_chunks and code_repos.")
    sys.exit(2)

FAILED = 0
HOST = "entity-7"          # the Host's own gitea login
USER = "host-a"             # its user
OWN = f"{HOST}/notes-app"  # a repo the Host owns
GRANTED = f"{USER}/trader"  # the user's repo, granted to the Host
FOREIGN = "mallory/secret-sauce"   # on the forge, never granted
NOREAD = f"{USER}/listed-no-read"  # listed, but gitea reports no read
BIG = f"{USER}/monorepo"          # too big for the room on the disk


def check(label, cond, detail=""):
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:500]}]"))
    if not cond:
        FAILED += 1


def _git(*argv, cwd=None):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {argv}: {r.stderr}")
    return r.stdout.strip()


class Forge:
    """The Host's gitea, as far as code search asks it: /user/repos (what
    THIS Host can read) and a branch's head. Repos live as bare git repos
    under `root`, cloned by the real checkout code over file://."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.listed: dict[str, dict] = {}   # what /user/repos answers
        self.fail_list = False
        self.calls: list[str] = []
        # Gitea 1.26: anyone signed in may read a PUBLIC repo, and a READ grant
        # on one leaves no `access` row, so /user/repos never lists it; the
        # collaborator endpoint (204 / 404) is where the grant shows.
        self.public: set[str] = set()
        self.collab: set[str] = set()

    def bare(self, full: str) -> pathlib.Path:
        return self.root / "bare" / f"{full}.git"

    def make(self, full: str, files: dict[str, str]) -> None:
        bare = self.bare(full)
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "-q", "--bare", "-b", "main", str(bare))
        work = self.root / "work" / full
        work.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=work)
        _git("remote", "add", "origin", f"file://{bare}", cwd=work)
        self.push(full, files)

    def push(self, full: str, files: dict) -> None:
        """`files`: rel -> text, bytes, ("link", target) for a symbolic
        link, or None to delete."""
        work = self.root / "work" / full
        for rel, text in files.items():
            f = work / rel
            if text is None:
                f.unlink()
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, tuple):
                os.symlink(text[1], f)
            elif isinstance(text, bytes):
                f.write_bytes(text)
            else:
                f.write_text(text)
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "change", cwd=work)
        _git("push", "-q", "origin", "main", cwd=work)

    def head(self, full: str) -> str:
        return _git("rev-parse", "main", cwd=self.bare(full))

    def list_(self, full: str, *, pull=True, size_kib=40) -> None:
        owner = full.split("/")[0]
        self.listed[full] = {"full_name": full, "owner": {"login": owner}, "default_branch": "main",
                             "size": size_kib, "empty": False, "private": True,
                             "permissions": {"admin": owner == HOST, "push": owner == HOST, "pull": pull}}

    async def request(self, method, path, *, params=None, json=None, raw_text=False, bearer=None):
        from fastapi import HTTPException
        self.calls.append(path)
        if path == "/api/v1/user/repos":
            if self.fail_list:
                raise HTTPException(502, "gitea: <html>bad gateway</html>")
            rows = [self.listed[k] for k in sorted(self.listed)]
            page, limit = int(params["page"]), int(params["limit"])
            return rows[(page - 1) * limit: page * limit]
        m = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)/collaborators/([^/]+)", path)
        if m:
            full = f"{m.group(1)}/{m.group(2)}"
            if m.group(3) == HOST and full in self.collab and self.bare(full).exists():
                return ""
            raise HTTPException(404, "gitea: not found")
        m = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)", path)
        if m:
            full = f"{m.group(1)}/{m.group(2)}"
            if full in self.listed:
                return self.listed[full]
            if self.bare(full).exists() and (full in self.public or full in self.collab):
                return {"full_name": full, "owner": {"login": m.group(1)}, "default_branch": "main",
                        "size": 40, "empty": False, "private": full not in self.public,
                        "permissions": {"admin": False, "push": False, "pull": True}}
            raise HTTPException(404, "gitea: not found")
        m = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)/branches/(.+)", path)
        if m:
            full = f"{m.group(1)}/{m.group(2)}"
            if not self.bare(full).exists():
                raise HTTPException(404, "gitea: not found")
            return {"name": m.group(3), "commit": {"id": self.head(full)}}
        raise HTTPException(404, f"gitea: no route {path}")


DIM = 2560


def fake_vec(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    v = [((b / 255.0) - 0.5) for b in h] + [0.0] * (DIM - len(h))
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


async def run(dsn: str) -> None:
    from codesearch import bridge_embed, indexer, repo_sync, store
    from server import brain_service
    from server.routes import embed as embed_routes
    from server.routes import gitea_client
    from mastercode.runtime import tools

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="idx-repos-"))
    forge = Forge(tmp / "forge")
    lane = {"up": True}
    embedded: list[str] = []

    async def _embed_many(texts, *, space=None):
        if not lane["up"]:
            return [None for _ in texts]
        embedded.extend(texts)
        return [fake_vec(t) for t in texts]

    async def _embed(text, *, space=None):
        return fake_vec(text) if lane["up"] else None

    async def _rerank(query, docs):
        return None

    async def _no_emit(payload):
        return None

    saved = (indexer.PROJECTS_ROOT, indexer.REPOS_ROOT, bridge_embed.embed_many, bridge_embed.embed,
             bridge_embed.rerank, gitea_client.gitea_creds, gitea_client.gitea_request,
             gitea_client.gitea_proxy, repo_sync.clone_url, brain_service._pool, embed_routes._emit)
    indexer.PROJECTS_ROOT, indexer.REPOS_ROOT = str(tmp / "projects"), str(tmp / "repos")
    bridge_embed.embed_many, bridge_embed.embed, bridge_embed.rerank = _embed_many, _embed, _rerank
    gitea_client.gitea_creds = lambda: (HOST, "s3cr3t-token", "git.forge.test")
    gitea_client.gitea_request = forge.request
    gitea_client.gitea_proxy = lambda: ""
    repo_sync.clone_url = lambda host, repo: f"file://{forge.bare(repo)}"
    brain_service._pool = pool
    embed_routes._emit = _no_emit

    async def rows(project):
        return await pool.fetch("SELECT file_path, content, embedding IS NOT NULL AS has_vec "
                                "FROM code_chunks WHERE project = $1 ORDER BY file_path, chunk_index",
                                project)

    async def search(**args):
        r = await tools._exec_search_my_code(args)
        return r.get("result") if r.get("status") == "ok" else r

    def projects_of(res):
        return {h.get("project") for h in (res or {}).get("results") or []}

    async def grant_route_names(repo):
        """What the Gitea app's grant route does once the forge took the
        grant (server/routes/gitea.py grant_host_access)."""
        import inspect
        from server.routes import gitea as gitea_routes
        done = gitea_routes._index_readable_repos_now(repo)
        if inspect.isawaitable(done):
            await done

    try:
        # The Host's embedding setup (what apply_embedding_switch writes): a
        # 2560-dimension model, whose vectors live in v200's `embedding`
        # column, the one rows() reads. The index follows it (v231).
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = COALESCE(behavioral_params, '{}'::jsonb) "
            "|| $1::jsonb WHERE id = $2",
            '{"embedding_model": "qwen3-embedding:4b", "embedding_dim": %d}' % DIM, brain_service._entity_id)
        # A Workbench project the Host built, indexed the old way.
        (tmp / "projects" / "wb1").mkdir(parents=True)
        (tmp / "projects" / "wb1" / "ledger.py").write_text(
            "def reconcile_ledger_entries(book):\n    return sorted(book)\n")
        await indexer.index_project(pool, project="wb1")

        forge.make(OWN, {"app.py": "def render_sticky_note(note):\n    return note.title\n",
                         "README.md": "Notes app the Host owns.\n"})
        forge.make(GRANTED, {"strategy.py": "def momentum_crossover_signal(prices):\n"
                                            "    return prices[-1] > prices[-20]\n",
                             "risk.py": "def kelly_fraction_sizing(edge, odds):\n    return edge / odds\n"})
        forge.make(FOREIGN, {"sauce.py": "def proprietary_mallory_formula():\n    return 42\n"})
        forge.public.add(FOREIGN)              # anyone may read it; nobody granted it
        forge.make(NOREAD, {"x.py": "def listed_without_read_access():\n    return 0\n"})
        forge.make(BIG, {"m.py": "def enormous_monorepo_symbol():\n    return 1\n"})
        forge.list_(OWN)
        forge.list_(GRANTED)
        forge.list_(NOREAD, pull=False)
        forge.list_(BIG, size_kib=10 ** 10)    # ~9.3 TiB as gitea would size it

        print("G1/F1/D1. the first pass")
        r1 = await repo_sync.sync_once(pool)
        check("G1 the pass read the forge's list and indexed the Host's own repo and the granted one",
              r1.get("ok") and sorted(r1["indexed"]) == sorted([OWN, GRANTED]), r1)
        own_rows, granted_rows = await rows(OWN), await rows(GRANTED)
        check("G1 both repos' files are in code_chunks under 'owner/name', every chunk with a vector",
              {x["file_path"] for x in own_rows} == {"app.py", "README.md"}
              and {x["file_path"] for x in granted_rows} == {"strategy.py", "risk.py"}
              and all(x["has_vec"] for x in own_rows + granted_rows), (own_rows, granted_rows))
        rec = {x["repo"]: dict(x) for x in await pool.fetch("SELECT * FROM code_repos")}
        check("G1 code_repos records each indexed repo at the forge's head",
              rec.get(OWN, {}).get("head") == forge.head(OWN)
              and rec.get(GRANTED, {}).get("head") == forge.head(GRANTED)
              and rec[OWN]["state"] == rec[GRANTED]["state"] == "indexed", rec)
        check("G1 the checkout is in the repos tree the pod already used for gitea repos",
              (tmp / "repos" / "host-a" / "trader" / "strategy.py").is_file()
              and (tmp / "repos" / HOST / "notes-app" / ".git").is_dir())
        check("F1 a repo on the forge that was never granted is never cloned or indexed",
              not (tmp / "repos" / "mallory").exists() and not await rows(FOREIGN)
              and FOREIGN not in rec
              and not any("mallory" in c for c in forge.calls), forge.calls)
        check("F1 a repo listed without read access is not indexed",
              not await rows(NOREAD) and NOREAD not in rec
              and not (tmp / "repos" / "host-a" / "listed-no-read").exists())
        check("D1 a repo too big for the room left is skipped with the reason, nothing cloned",
              rec.get(BIG, {}).get("state") == "skipped"
              and "too big for the room left" in (rec[BIG]["reason"] or "")
              and not (tmp / "repos" / "host-a" / "monorepo").exists() and not await rows(BIG),
              rec.get(BIG))

        print("S1. search_my_code over everything")
        res = await search(query="momentum_crossover_signal")
        top = (res.get("results") or [{}])[0]
        check("S1 a search with no project finds the granted repo's function, the hit naming its repo",
              top.get("project") == GRANTED and top.get("file_path") == "strategy.py", res)
        res = await search(query="render_sticky_note reconcile_ledger_entries kelly_fraction_sizing", limit=20)
        check("S1 one search returns hits across the Workbench project and both repos",
              {"wb1", OWN, GRANTED} <= projects_of(res), projects_of(res))
        res = await search(query="render_sticky_note reconcile_ledger_entries", project=GRANTED)
        check("S1 project='owner/name' narrows the search to that repo",
              projects_of(res) == {GRANTED}, projects_of(res))
        res = await search(query="enormous_monorepo_symbol")
        gaps = {g["repo"]: g for g in res.get("not_fully_indexed") or []}
        check("D1 search_my_code names the readable repo it is not seeing, and why",
              BIG in gaps and "too big" in (gaps[BIG].get("reason") or ""), res)

        print("F1. the doors that index on request refuse a foreign repo")
        r = await tools._exec_embed_jobs({"op": "enqueue", "target": "gitea_repo", "repo": FOREIGN})
        check("F1 embed_jobs target='gitea_repo' refuses a repo the forge does not list for this Host",
              r.get("status") == "error" and "foreign repo is never indexed" in str(r.get("result")), r)
        job_id = await pool.fetchval(
            "INSERT INTO embed_jobs (entity_id, job_type, target, status, metadata) "
            "VALUES (1, 'repo', 'gitea_repo', 'running', $1::jsonb) RETURNING id",
            '{"repo": "%s"}' % FOREIGN)
        await embed_routes.JobRunner()._run_repo_job({"id": job_id, "metadata": {"repo": FOREIGN}})
        job = await pool.fetchrow("SELECT status, error FROM embed_jobs WHERE id = $1", job_id)
        check("F1 a gitea_repo job queued before this change fails for a foreign repo, nothing cloned",
              job["status"] == "error" and "foreign repo" in (job["error"] or "")
              and not (tmp / "repos" / "mallory").exists() and not await rows(FOREIGN), dict(job))

        print("G2. nothing moved")
        embedded.clear()
        r2 = await repo_sync.sync_once(pool)
        check("G2 a pass where no head moved re-indexes nothing and embeds nothing",
              sorted(r2["unchanged"]) == sorted([OWN, GRANTED]) and not r2["indexed"] and not embedded,
              (r2, len(embedded)))

        print("G3. a push to one file")
        forge.push(GRANTED, {"risk.py": "def drawdown_circuit_breaker(equity):\n    return equity < 0.8\n"})
        embedded.clear()
        before_strategy = await pool.fetch(
            "SELECT id FROM code_chunks WHERE project = $1 AND file_path = 'strategy.py'", GRANTED)
        r3 = await repo_sync.sync_once(pool)
        after_strategy = await pool.fetch(
            "SELECT id FROM code_chunks WHERE project = $1 AND file_path = 'strategy.py'", GRANTED)
        check("G3 the moved repo is re-indexed, the other is unchanged",
              r3["indexed"] == [GRANTED] and r3["unchanged"] == [OWN], r3)
        check("G3 only the changed file was embedded again; the unchanged file's rows are the same rows",
              embedded and all("drawdown_circuit_breaker" in t for t in embedded)
              and [x["id"] for x in before_strategy] == [x["id"] for x in after_strategy],
              (embedded, before_strategy, after_strategy))
        res = await search(query="drawdown_circuit_breaker")
        old = await search(query="kelly_fraction_sizing", project=GRANTED)
        check("G3 the pushed text is found and the replaced text is gone",
              (res.get("results") or [{}])[0].get("file_path") == "risk.py"
              and not any("kelly_fraction_sizing" in h["snippet"] for h in old.get("results") or []),
              (res, old))
        check("G3 the record follows the new head",
              await pool.fetchval("SELECT head FROM code_repos WHERE repo = $1", GRANTED) == forge.head(GRANTED))

        print("G5. the forge could not be asked")
        forge.fail_list = True
        r5 = await repo_sync.sync_once(pool)
        forge.fail_list = False
        check("G5 a pass whose listing failed changes nothing — no repo dropped",
              not r5.get("ok") and await rows(OWN) and await rows(GRANTED)
              and (tmp / "repos" / "host-a" / "trader").is_dir(), r5)

        print("F2. rows of a repo the forge does not list")
        await store.upsert_chunks(pool, space=store.Space(model="qwen3-embedding:4b", dim=DIM),
                                  chunks=[(store.Chunk(
            project=FOREIGN, file_path="sauce.py", chunk_index=0,
            content="def proprietary_mallory_formula(): ...", language="python", chunk_kind="function",
            symbol=None, start_line=1, end_line=1, file_hash="x"), fake_vec("x"))])
        (tmp / "repos" / "mallory" / "secret-sauce" / ".git").mkdir(parents=True)

        print("G4. read access goes")
        del forge.listed[GRANTED]
        r4 = await repo_sync.sync_once(pool)
        check("G4 the revoked repo loses its rows, its checkout and its record",
              GRANTED in r4["dropped"] and not await rows(GRANTED)
              and not (tmp / "repos" / "host-a" / "trader").exists()
              and await pool.fetchval("SELECT count(*) FROM code_repos WHERE repo = $1", GRANTED) == 0, r4)
        check("F2 a repo the forge does not list (an old job's clone) is dropped with its rows",
              FOREIGN in r4["dropped"] and not await rows(FOREIGN)
              and not (tmp / "repos" / "mallory").exists(), r4)
        check("G4 the Host's own repo and the Workbench project are untouched",
              await rows(OWN) and await rows("wb1") and OWN in r4["unchanged"], r4)
        res = await search(query="momentum_crossover_signal")
        check("G4 search_my_code no longer returns the revoked repo",
              GRANTED not in projects_of(res), projects_of(res))

        print("P1. a read grant on a PUBLIC repo (gitea's listing leaves it out)")
        pub, pub2 = f"{USER}/public-lib", f"{USER}/public-tools"
        forge.make(pub, {"lib.py": "def public_granted_quaternion_slerp(a, b):\n    return a\n"})
        forge.make(pub2, {"t.py": "def public_granted_hint_symbol():\n    return 2\n"})
        forge.public |= {pub, pub2}
        forge.collab |= {pub, pub2}             # the Gitea app's grant: collaborator, read
        r = await tools._exec_embed_jobs({"op": "enqueue", "target": "gitea_repo", "repo": pub})
        # (this database has no embedding model set, so an accepted repo stops
        # at enqueue with that sentence; a refused one says it is not granted)
        check("P1 embed_jobs does not refuse a read-granted public repo the listing leaves out",
              "not a repo this host owns or was granted" not in str(r.get("result")), r)
        job_id = await pool.fetchval(
            "INSERT INTO embed_jobs (entity_id, job_type, target, status, metadata) "
            "VALUES (1, 'repo', 'gitea_repo', 'running', $1::jsonb) RETURNING id", '{"repo": "%s"}' % pub)
        await embed_routes.JobRunner()._run_repo_job({"id": job_id, "metadata": {"repo": pub}})
        job = await pool.fetchrow("SELECT status, error FROM embed_jobs WHERE id = $1", job_id)
        check("P1 its job indexes it", job["status"] == "done" and await rows(pub), dict(job))
        rp = await repo_sync.sync_once(pool)
        check("P1 the next pass keeps it: asked of the forge again, not dropped as unlisted",
              pub in rp["unchanged"] and pub not in rp["dropped"] and await rows(pub), rp)
        await grant_route_names(pub2)          # what the grant route does
        rp = await repo_sync.sync_once(pool)
        check("P1 a grant in the Gitea app gets the public repo indexed on the pass it wakes",
              pub2 in rp["indexed"] and await rows(pub2), rp)
        await grant_route_names(FOREIGN)       # even if someone names it
        rp = await repo_sync.sync_once(pool)
        check("P1 another user's public repo, readable by anyone but never granted, stays out",
              FOREIGN not in rp["indexed"] and not await rows(FOREIGN)
              and not (tmp / "repos" / "mallory").exists(), rp)
        r = await tools._exec_embed_jobs({"op": "enqueue", "target": "gitea_repo", "repo": FOREIGN})
        check("P1 embed_jobs still refuses that public foreign repo",
              r.get("status") == "error" and "foreign repo is never indexed" in str(r.get("result")), r)
        forge.collab -= {pub, pub2}
        rp = await repo_sync.sync_once(pool)
        check("P1 revoking the grants drops both public repos' rows and checkouts",
              {pub, pub2} <= set(rp["dropped"]) and not await rows(pub) and not await rows(pub2)
              and not (tmp / "repos" / "host-a" / "public-lib").exists(), rp)

        print("L1. no embedder answering")
        lane["up"] = False
        journal = f"{USER}/journal"
        forge.make(journal, {"journal.py": "def trading_journal_lexical_only_entry():\n    return 'x'\n"})
        forge.list_(journal)
        rl = await repo_sync.sync_once(pool)
        jr = await rows(journal)
        check("L1 with no embedder the repo is still written, its chunks without vectors",
              jr and not any(x["has_vec"] for x in jr) and journal in rl["indexed"], (rl, jr))
        res = await search(query="trading_journal_lexical_only_entry")
        check("L1 keyword search alone finds it (the tsvector arm stands without an embedder)",
              (res.get("results") or [{}])[0].get("project") == journal, res)
        rec = await pool.fetchrow("SELECT head, reason FROM code_repos WHERE repo = $1", journal)
        check("L1 its head is not recorded and the reason says why, so a later pass embeds it",
              rec["head"] is None and "keyword search only" in (rec["reason"] or ""), dict(rec))
        lane["up"] = True
        embedded.clear()
        await repo_sync.sync_once(pool)
        jr = await rows(journal)
        rec = await pool.fetchrow("SELECT head, reason FROM code_repos WHERE repo = $1", journal)
        check("L1 once an embedder answers, the next pass fills the vectors and records the head",
              jr and all(x["has_vec"] for x in jr) and rec["head"] == forge.head(journal)
              and rec["reason"] is None and any("trading_journal" in t for t in embedded), (jr, dict(rec)))

        print("N1. no embedding model set up")
        # A Host that has not set embeddings up (there is no platform
        # embedder): every repo is indexed for keyword search once, and a
        # pass at an unmoved head fetches nothing — it has nothing to embed
        # with. Setting a model up then fills every repo's column.
        await pool.execute("UPDATE entity_registry SET behavioral_params = behavioral_params "
                           "- 'embedding_model' - 'embedding_dim' WHERE id = $1", brain_service._entity_id)
        notes = f"{USER}/field-notes"
        forge.make(notes, {"notes.py": "def field_notes_unembedded_symbol():\n    return 'n'\n"})
        forge.list_(notes)
        embedded.clear()
        rn = await repo_sync.sync_once(pool)
        nr = await rows(notes)
        rec = await pool.fetchrow("SELECT head, reason FROM code_repos WHERE repo = $1", notes)
        check("N1 the new repo is written for keyword search, no embedder asked, and the reason says "
              "embeddings are not set up yet",
              nr and not any(x["has_vec"] for x in nr) and notes in rn["indexed"] and not embedded
              and "not set up yet" in (rec["reason"] or ""), (rn, dict(rec)))
        fetched_n: list = []
        real_checkout_n = repo_sync.checkout

        async def counting_checkout_n(*a, **k):
            fetched_n.append(a)
            return await real_checkout_n(*a, **k)
        repo_sync.checkout = counting_checkout_n
        try:
            rn2 = await repo_sync.sync_once(pool)
        finally:
            repo_sync.checkout = real_checkout_n
        check("N1 the next pass at the same heads fetches no repo (nothing to embed with)",
              not fetched_n and notes in rn2["unchanged"] and not rn2["indexed"], (fetched_n, rn2))
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = COALESCE(behavioral_params, '{}'::jsonb) "
            "|| $1::jsonb WHERE id = $2",
            '{"embedding_model": "qwen3-embedding:4b", "embedding_dim": %d}' % DIM, brain_service._entity_id)
        embedded.clear()
        rn3 = await repo_sync.sync_once(pool)
        nr = await rows(notes)
        rec = await pool.fetchrow("SELECT head, reason FROM code_repos WHERE repo = $1", notes)
        check("N1 once a model is set up, the next pass fills the repo's vectors and clears the reason",
              notes in rn3["indexed"] and nr and all(x["has_vec"] for x in nr) and rec["reason"] is None
              and any("field_notes_unembedded_symbol" in t for t in embedded), (rn3, dict(rec)))
        del forge.listed[notes]
        await repo_sync.sync_once(pool)

        from server import harness_input

        @contextlib.contextmanager
        def room(nbytes):
            """The disk as code search sees it: `nbytes` is what one repo may
            take (half of what is free above the 1 GiB kept back)."""
            saved_room = harness_input.free_bytes, harness_input.disk_floor
            harness_input.free_bytes = lambda p: (1 << 30) + 2 * nbytes
            harness_input.disk_floor = lambda p, **k: 1 << 30
            try:
                yield
            finally:
                harness_input.free_bytes, harness_input.disk_floor = saved_room

        async def record(repo):
            r = await pool.fetchrow("SELECT * FROM code_repos WHERE repo = $1", repo)
            return dict(r) if r else {}

        print("Z1. a checkout that expands far past its pack")
        zeros = f"{USER}/zeros"
        forge.make(zeros, {"big.txt": b"\0" * (16 << 20),
                           "ok.py": "def beside_the_zeros_symbol():\n    return 0\n"})
        forge.list_(zeros)                      # the forge sizes it at 40 KiB: its pack
        with room(4 << 20):
            rz = await repo_sync.sync_once(pool)
        rec = await record(zeros)
        check("Z1 a repo whose files are far bigger than its pack is skipped with the reason",
              rec.get("state") == "skipped" and "too big for the room left" in (rec.get("reason") or "")
              and any(isinstance(s, dict) and s["repo"] == zeros for s in rz["skipped"]), (rz, rec))
        check("Z1 none of its work tree was written and nothing of it is left on the disk",
              not (tmp / "repos" / USER / "zeros").exists() and not await rows(zeros),
              list((tmp / "repos" / USER).iterdir()) if (tmp / "repos" / USER).exists() else None)
        res = await search(query="beside_the_zeros_symbol")
        check("Z1 search_my_code names it as not fully indexed, and why",
              zeros in {g["repo"]: g for g in res.get("not_fully_indexed") or []}
              and "too big" in {g["repo"]: g for g in res.get("not_fully_indexed")}[zeros]["reason"], res)
        fetched: list = []
        real_checkout = repo_sync.checkout

        async def counting_checkout(*a, **k):
            fetched.append(a)
            return await real_checkout(*a, **k)
        repo_sync.checkout = counting_checkout
        try:
            with room(4 << 20):
                await repo_sync.sync_once(pool)
        finally:
            repo_sync.checkout = real_checkout
        check("Z1 the next pass at the same head does not fetch it again",
              not fetched and (await record(zeros)).get("state") == "skipped", fetched)
        with room(64 << 20):
            rz = await repo_sync.sync_once(pool)
        check("Z1 once the room is there it is checked out whole and indexed",
              zeros in rz["indexed"] and await rows(zeros)
              and (tmp / "repos" / USER / "zeros" / "big.txt").stat().st_size == 16 << 20, rz)
        del forge.listed[zeros]
        await repo_sync.sync_once(pool)

        print("Z2. a fetch whose pack outgrows the room")
        noise = f"{USER}/noise"
        forge.make(noise, {"blob.bin": os.urandom(3 << 20),
                           "n.py": "def noise_repo_symbol():\n    return 1\n"})
        forge.list_(noise)                      # the forge's size (40 KiB) understates it
        with room(1 << 20):
            rn = await repo_sync.sync_once(pool)
        rec = await record(noise)
        check("Z2 the fetch is cut off at the room and the repo skipped with the reason",
              rec.get("state") == "skipped" and "too big for the room left" in (rec.get("reason") or "")
              and "fetch" in (rec.get("reason") or ""), (rn, rec))
        check("Z2 nothing of it is left on the disk",
              not (tmp / "repos" / USER / "noise").exists() and not await rows(noise))
        del forge.listed[noise]
        await repo_sync.sync_once(pool)

        print("Z3. a tree that fits whose index rows would not")
        many = f"{USER}/many-small"
        forge.make(many, {f"pkg/m{i:03d}.py": f"def many_small_symbol_{i}(x):\n    return x + {i}\n" * 20
                          for i in range(120)})
        forge.list_(many)
        with room(3 << 20):
            rm = await repo_sync.sync_once(pool)
        rec = await record(many)
        tree_only = sum((forge.root / "work" / many / "pkg" / f).stat().st_size
                        for f in os.listdir(forge.root / "work" / many / "pkg"))
        check("Z3 the files alone fit, their index rows do not: skipped with the reason",
              tree_only < (1 << 20) and rec.get("state") == "skipped"
              and "too big for the room left" in (rec.get("reason") or "")
              and "index" in (rec.get("reason") or "") and not await rows(many), (rm, rec, tree_only))
        res = await search(query="many_small_symbol_7")
        check("Z3 search_my_code lists it as skipped",
              many in {g["repo"] for g in res.get("not_fully_indexed") or []}, res)
        del forge.listed[many]
        await repo_sync.sync_once(pool)

        print("Z4. a few KiB of nested trees naming tens of thousands of empty files")
        swarm = f"{USER}/swarm"
        forge.make(swarm, {"README.md": "swarm\n"})
        sbare = forge.bare(swarm)
        empty = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=sbare, input=b"",
                               capture_output=True, check=True).stdout.decode().strip()

        def mktree(entries: str) -> str:
            return subprocess.run(["git", "mktree"], cwd=sbare, input=entries.encode(),
                                  capture_output=True, check=True).stdout.decode().strip()
        leaf = mktree("".join(f"100644 blob {empty}\tf{i:03d}\n" for i in range(150)))
        top = mktree("".join(f"040000 tree {leaf}\td{i:03d}\n" for i in range(150)))
        commit = _git("commit-tree", top, "-m", "swarm", cwd=sbare)
        _git("update-ref", "refs/heads/main", commit, cwd=sbare)
        forge.list_(swarm)
        listing = subprocess.run(["git", "ls-tree", "-r", "-t", "-l", "-z", "main"], cwd=sbare,
                                 capture_output=True, check=True).stdout
        sized = repo_sync.tree_need(listing)
        check("Z4 every file costs its .git/index entry and inode, even an empty one",
              sized["files"] == 22500 and sized["need"] >= 22500 * 64 + 150 * 4096, sized)
        measure = getattr(repo_sync, "_measure", None)
        streamed = None
        if measure is not None:
            streamed = await measure(("git",), "main", str(sbare), dict(os.environ), 4096,
                                     max_inodes=1000)
        check("Z4 the listing is read as it streams and git is stopped once past the room",
              streamed is not None and streamed[0] != 0 and streamed[1].inodes < 22500,
              streamed and (streamed[0], streamed[1].inodes))
        saved_inodes = getattr(repo_sync, "_inode_room", None)
        repo_sync._inode_room = lambda: 5000
        try:
            with room(64 << 20):
                rs = await repo_sync.sync_once(pool)
        finally:
            if saved_inodes is not None:
                repo_sync._inode_room = saved_inodes
            else:
                del repo_sync._inode_room
        rec = await record(swarm)
        check("Z4 more files than the disk's inodes allow: skipped with the reason, nothing written",
              rec.get("state") == "skipped" and "too many files" in (rec.get("reason") or "")
              and not (tmp / "repos" / USER / "swarm").exists() and not await rows(swarm), (rs, rec))
        with room(64 << 20):
            rs = await repo_sync.sync_once(pool)
        check("Z4 once the inodes are there it is checked out whole",
              swarm in rs["indexed"]
              and (tmp / "repos" / USER / "swarm" / "d149" / "f149").is_file(), rs)
        del forge.listed[swarm]
        await repo_sync.sync_once(pool)

        hostile = tmp / "hostile"
        marks = hostile / "marks"
        hooks = hostile / "hooks"
        marks.mkdir(parents=True)
        hooks.mkdir()
        for h in ("post-checkout", "post-merge", "reference-transaction", "post-index-change"):
            (hooks / h).write_text(f"#!/bin/sh\necho ran >> {marks}/hook-{h}\nexit 0\n")
            (hooks / h).chmod(0o755)
        gcfg = hostile / "gitconfig"
        # (double quotes: an unquoted `;` starts a comment in git config)
        gcfg.write_text(
            "[core]\n\tsymlinks = false\n\thooksPath = %s\n\tautocrlf = true\n"
            "[filter \"evil\"]\n\tsmudge = \"sh -c 'echo ran >> %s/smudge; cat'\"\n\tclean = cat\n"
            "[filter \"lfs\"]\n\tsmudge = \"sh -c 'echo ran >> %s/lfs; cat'\"\n\tclean = cat\n"
            % (hooks, marks, marks))
        probe = hostile / "probe"
        subprocess.run(["git", "init", "-q", str(probe)], check=True)
        got = subprocess.run(["git", "config", "filter.evil.smudge"], cwd=probe, capture_output=True,
                             text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": str(gcfg)}).stdout
        check("K2 (setup) the hostile global config defines the filter whole",
              got.strip() == f"sh -c 'echo ran >> {marks}/smudge; cat'", got)

        @contextlib.contextmanager
        def hostile_global_config():
            """A ~/.gitconfig a process in the pod could have written (git-lfs
            install writes filter.lfs.* there)."""
            old = os.environ.get("GIT_CONFIG_GLOBAL")
            os.environ["GIT_CONFIG_GLOBAL"] = str(gcfg)
            try:
                yield
            finally:
                if old is None:
                    os.environ.pop("GIT_CONFIG_GLOBAL", None)
                else:
                    os.environ["GIT_CONFIG_GLOBAL"] = old

        print("K1. links out of the checkout")
        links = f"{USER}/links"
        forge.make(links, {"app.py": "def links_repo_real_function():\n    return 'real'\n",
                           "passwd.py": ("link", "/etc/passwd"),
                           "etc": ("link", "/etc"),
                           "up": ("link", "../../"),
                           "docs/upfile.md": ("link", "../../../../../etc/passwd")})
        forge.list_(links)
        with hostile_global_config():
            rk = await repo_sync.sync_once(pool)
        co = tmp / "repos" / USER / "links"
        check("K1 the repo is indexed", links in rk["indexed"], rk)
        check("K1 every link is written as a link (core.symlinks), whatever the global config says",
              all(os.path.islink(co / p) for p in ("passwd.py", "etc", "up", "docs/upfile.md")),
              {p: os.path.islink(co / p) for p in ("passwd.py", "etc", "up", "docs/upfile.md")})
        lr = await rows(links)
        check("K1 nothing a link points at is read into the index",
              {x["file_path"] for x in lr} == {"app.py"}
              and not any("root:" in x["content"] for x in await pool.fetch(
                  "SELECT content FROM code_chunks WHERE project = $1", links)), lr)
        del forge.listed[links]
        await repo_sync.sync_once(pool)

        print("K2. filters, transforms and hooks a repo names")
        filt = f"{USER}/filtered"
        ident_text = "$Id$\n" * 2000
        pointer = ("version https://git-lfs.github.com/spec/v1\n"
                   "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
                   "size 12345\n")
        forge.make(filt, {".gitattributes": "*.py filter=evil\n*.txt ident text eol=crlf\n"
                                            "*.md filter=lfs diff=lfs merge=lfs -text\n",
                          "code.py": "def filtered_repo_function():\n    return 'clean'\n",
                          "notes.txt": ident_text,
                          "model.md": pointer})
        forge.list_(filt)
        with hostile_global_config():
            rf = await repo_sync.sync_once(pool)
        co = tmp / "repos" / USER / "filtered"
        check("K2 the repo is indexed", filt in rf["indexed"], rf)
        check("K2 no filter driver and no hook ran (the global config defines both)",
              not list(marks.iterdir()), [p.name for p in marks.iterdir()])
        check("K2 every file is written as its blob: no smudge, no $Id$ expansion, no CRLF",
              (co / "code.py").read_text() == "def filtered_repo_function():\n    return 'clean'\n"
              and (co / "notes.txt").read_bytes() == ident_text.encode()
              and (co / "model.md").read_text() == pointer,
              {f: (co / f).stat().st_size for f in ("code.py", "notes.txt", "model.md")})
        fr = {x["file_path"] for x in await rows(filt)}
        check("K2 an LFS pointer is indexed as the small text it is (nothing is downloaded)",
              "model.md" in fr and "code.py" in fr, fr)
        del forge.listed[filt]
        await repo_sync.sync_once(pool)

        print("K3. who can read the checkouts")
        mode = stat.S_IMODE(os.stat(tmp / "repos").st_mode)
        check("K3 the repos root is open to the Host's own user only (0700)", mode & 0o077 == 0, oct(mode))

        print("P2. a grant survives a restart before the next pass")
        import importlib
        pub3 = f"{USER}/granted-before-restart"
        forge.make(pub3, {"g.py": "def granted_before_restart_symbol():\n    return 3\n"})
        forge.public.add(pub3)
        forge.collab.add(pub3)
        await grant_route_names(pub3)            # what the grant route does
        importlib.reload(repo_sync)              # the pod restarts: the module's memory is gone
        repo_sync.clone_url = lambda host, repo: f"file://{forge.bare(repo)}"
        rr = await repo_sync.sync_once(pool)
        check("P2 the first pass after the restart indexes the repo granted before it",
              pub3 in rr["indexed"] and await rows(pub3), rr)
    finally:
        (indexer.PROJECTS_ROOT, indexer.REPOS_ROOT, bridge_embed.embed_many, bridge_embed.embed,
         bridge_embed.rerank, gitea_client.gitea_creds, gitea_client.gitea_request,
         gitea_client.gitea_proxy, repo_sync.clone_url, brain_service._pool, embed_routes._emit) = saved
        await pool.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv) as dsn:
        asyncio.run(run(dsn))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
