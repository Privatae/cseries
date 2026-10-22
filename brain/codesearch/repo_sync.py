"""
repo_sync — every repo the Host can read on its gitea is in its code index.

search_my_code covers the Host's repos and its user's: whatever the Host has
read access to is indexed, so it can reason "I can read it, so it is in my
index". A Host's OWN repo is read by the Host itself; an agent is dispatched
only to read a foreign repo.

Who may read what is the forge's answer, asked again on every pass: GET
/api/v1/user/repos as the Host, with its own credential — the list the gitea
tool's op='list' shows (the repos it owns, and the user's repos the user
granted it). There is no second record of access. code_repos (v224) only
remembers what the last pass did to each repo.

One pass (sync_once):
  * a repo the forge lists with read access is cloned (shallow, its default
    branch) into REPOS_ROOT/<owner>/<name> — the tree embed jobs already
    cloned into — and indexed into code_chunks under project "owner/name";
  * on later passes the default branch's head is read from the forge (one
    API call a repo) and compared with the head last indexed in full; only a
    moved head fetches, and the indexer re-embeds only the files that changed
    (or every file once, after the chunker changes how files are split:
    indexer.chunked_by_this_version; or once with the new model, after the
    Host switches embedding models: indexer.vectors_current);
  * a repo in the index, on disk or on record that the forge no longer lists
    loses its rows, its checkout and its record;
  * a repo that would not fit the room left on the disk is skipped and
    recorded with the sentence saying so, and nothing of it stays on the
    disk. The room is checked three times: the forge's size before anything
    is fetched; the fetch itself, which git cannot write past the room (a
    file-size limit on the process); and, before a single file of the work
    tree is written, the tree's real size read from the fetched objects (a
    file of zeros packs to almost nothing) plus the database rows its index
    will take, and the files and folders it makes against the disk's free
    inodes (nested trees of a few KiB can name a billion empty files); the
    listing is read as it streams and git stopped once the tree is past the
    room;
  * git runs with no global or system config, the shared hardened -c set and
    this module's own (_SYNC_GIT_CONFIG), and $GIT_DIR/info/attributes turns
    every checkout-time transform off: nothing in a repo (.gitattributes,
    .gitmodules, a link) makes git run a program, follow a link out of the
    checkout, or write a file larger than its blob.
A pass whose list could not be read in full changes nothing: a missing answer
is never taken for "access gone".

Foreign repos — GitHub, another user's repo on the forge that this Host was
not granted — never enter this list, so they are never cloned or indexed in
the pod; an isolated agent reads them.

Embedding goes through codesearch's own lane (bridge_embed, via the indexer);
with no embedder answering, files are still written for keyword search.
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import shutil
import signal
import stat
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import quote, urlsplit

from . import indexer, store

log = logging.getLogger("codesearch.repo_sync")

# How often a pass runs. Each one costs one listing (a page per 50 repos) and
# one branch read per repo; a fetch and an index only for a repo that moved.
SYNC_S = float(os.environ.get("CODESEARCH_REPO_SYNC_S", "600"))
# The first pass waits this long after start, so the bridge and the embed lane
# are up before it asks for vectors.
FIRST_SYNC_S = float(os.environ.get("CODESEARCH_REPO_FIRST_SYNC_S", "90"))
LIST_PAGE = 50            # gitea's default MAX_RESPONSE_ITEMS
LIST_MAX_PAGES = 200      # 10,000 repos; past it the answer is incomplete
# What the index of one file costs in the database: code_chunks' heap, its
# TOAST (the vectors), the HNSW and GIN indexes and the unique btree, all of
# it (pg_total_relation_size). Measured with 2560-dimension halfvecs, the
# second-largest size the index stores (store.COLUMN_BY_DIM): a smaller
# model's vectors cost less, so for most models this errs high; a 4096 one's
# are 60% bigger and have no HNSW index. Measured on the
# kernel's own schema, indexing three real trees of this repository with dense
# 2560-dim vectors (default-skills 55 files / 0.5 MB, circles-web 99 / 1.1 MB,
# server/routes 86 / 3.7 MB: 10.6, 10.2 and 7.7 bytes per source byte); a
# least-squares fit over the three predicts each within 2%. A tree of many
# small files costs more per byte (a chunk's vector is the same 5 KB whatever
# its length), hence the per-file term.
DB_BYTES_PER_FILE = 37_000
DB_BYTES_PER_SOURCE_BYTE = 6.8

# Every git call here, on top of the shared hardened set
# (server/routes/project_git.HARDENED_GIT_CONFIG: core.hooksPath=/dev/null,
# core.fsmonitor=false, diff.external=). The environment also drops the global
# and system config (GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM), so no
# filter, hook path, url rewrite or submodule setting can come from there, and
# the credential store is named here instead of in ~/.gitconfig.
_SYNC_GIT_CONFIG = (
    "-c", "credential.helper=store",          # ~/.git-credentials (entrypoint.sh)
    "-c", "core.symlinks=true",               # a link is written as a link, never followed
    "-c", "core.autocrlf=false",
    "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
    "-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
    "-c", "init.templateDir=",                # no template hooks copied in
    "-c", "protocol.allow=never",             # + the clone URL's own scheme, in checkout()
    # Keep a fetch as ONE pack file, so the file-size limit bounds all of it
    # (below 100 objects git would otherwise explode it into loose files).
    "-c", "fetch.unpackLimit=1", "-c", "transfer.unpackLimit=1",
    "-c", "gc.auto=0", "-c", "maintenance.auto=false",
)
# $GIT_DIR/info/attributes outranks every .gitattributes in the tree
# (gitattributes(5)). With these unset a checkout writes each blob's bytes as
# they are: no smudge/process filter (git-lfs included: a pointer stays the
# small text file it is), no eol conversion (a file of newlines would double
# under eol=crlf), no $Id$ expansion (each 4 bytes would become 46), no
# re-encoding (UTF-16 doubles). So the tree's blob sizes are what it writes.
_NO_TRANSFORMS = "* -text -eol -crlf -filter -ident -working-tree-encoding\n"

_locks: dict[str, asyncio.Lock] = {}
_wake_event: Optional[asyncio.Event] = None


def _lock(repo: str) -> asyncio.Lock:
    return _locks.setdefault(repo, asyncio.Lock())


def wake() -> None:
    """Run the next pass now (a grant or a revoke in the Gitea app)."""
    if _wake_event is not None:
        _wake_event.set()


async def remember_grant(db, repo: str) -> None:
    """A repo was just granted in the Gitea app: put it on record, so every
    pass from now on asks the forge about it (readable_repos `also`) — a read
    grant on a public repo is not in gitea's listing — and a restart before
    the pass does not lose it. The record says only that it was granted; the
    pass asks the forge whether it is still readable, indexes it if so, and
    drops the record if not."""
    repo = str(repo or "").strip().strip("/")
    if not _valid(repo):
        return
    await db.execute("""
        INSERT INTO code_repos (repo, state, reason)
        VALUES ($1, 'pending', 'granted in the Gitea app; the next pass indexes it')
        ON CONFLICT (repo) DO NOTHING
    """, repo)


def _valid(repo: str) -> bool:
    try:
        indexer.project_root_for(repo)
    except ValueError:
        return False
    return repo.count("/") == 1


def _gitea():
    from server.routes import gitea_client
    return gitea_client


def _readable_row(r: dict, host_login: str) -> Optional[dict]:
    full = str(r.get("full_name") or "")
    perms = r.get("permissions") or {}
    if not _valid(full) or not (perms.get("pull") or perms.get("push") or perms.get("admin")):
        return None
    return {"repo": full,
            "default_branch": str(r.get("default_branch") or ""),
            "size_kib": int(r.get("size") or 0),
            "owned": ((r.get("owner") or {}).get("login") or "") == host_login,
            "empty": bool(r.get("empty"))}


async def readable_repos(also: Iterable[str] = ()) -> Optional[list[dict]]:
    """Every repo the Host can read on its gitea, as the forge answers it —
    [{repo, default_branch, size_kib, owned, empty}] — or None when that
    answer could not be had in full (no credential, an error, more pages than
    LIST_MAX_PAGES). A caller must never read None as "no access".

    `also` names repos the listing may leave out although they were granted:
    gitea keeps no `access` row for a READ grant on a PUBLIC repo (models/perm/
    access RecalculateUserAccess keeps rows from Write up when the repo is not
    private), so /user/repos never lists one. Each such name is asked of the
    forge directly, and kept only when the Host is its collaborator: a public
    repo that anyone may read but that nobody granted is foreign."""
    g = _gitea()
    cred = g.gitea_creds()
    if not cred:
        return None
    host_login = cred[0]
    out: dict[str, dict] = {}
    for page in range(1, LIST_MAX_PAGES + 1):
        try:
            data = await g.gitea_request("GET", "/api/v1/user/repos",
                                         params={"limit": LIST_PAGE, "page": page})
        except Exception as e:  # noqa: BLE001
            log.warning("code search repos: listing page %d failed (%s)", page,
                        getattr(e, "detail", None) or type(e).__name__)
            return None
        if not isinstance(data, list):
            return None
        if not data:
            break
        for r in data:
            row = _readable_row(r, host_login) if isinstance(r, dict) else None
            if row is not None:
                out[row["repo"]] = row
    else:
        log.warning("code search repos: more than %d pages of repos; this pass changes nothing",
                    LIST_MAX_PAGES)
        return None
    listed = {k.lower() for k in out}
    for full in sorted({str(a or "").strip().strip("/") for a in also}):
        if not _valid(full) or full.lower() in listed:
            continue
        owner, name = full.split("/", 1)
        base = f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        try:
            # 204: a collaborator. 404: not one, or the repo is not there
            # or not visible to the Host — read access is gone either way.
            await g.gitea_request("GET", f"{base}/collaborators/{quote(host_login, safe='')}",
                                  raw_text=True)
            r = await g.gitea_request("GET", base)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 404:
                continue
            log.warning("code search repos: grant on %s not confirmed (%s)", full,
                        getattr(e, "detail", None) or type(e).__name__)
            return None
        row = _readable_row(r, host_login) if isinstance(r, dict) else None
        if row is not None:
            out[row["repo"]] = row
            listed.add(row["repo"].lower())
    return sorted(out.values(), key=lambda r: r["repo"])


async def remote_head(repo: str, branch: str) -> Optional[str]:
    """The commit at the tip of `branch` on the forge, or None."""
    owner, name = repo.split("/", 1)
    try:
        data = await _gitea().gitea_request(
            "GET", f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
                   f"/branches/{quote(branch, safe='/')}")
    except Exception as e:  # noqa: BLE001
        log.info("code search repos: head of %s@%s not read (%s)", repo, branch,
                 getattr(e, "detail", None) or type(e).__name__)
        return None
    head = ((data or {}).get("commit") or {}).get("id") if isinstance(data, dict) else None
    return str(head) if head else None


def clone_url(host: str, repo: str) -> str:
    """The forge's clone URL. Never carries the token: git reads that from
    its own credential store (~/.git-credentials, entrypoint.sh)."""
    return f"https://{host}/{repo}.git"


def _tree_bytes(path: Path) -> int:
    """Bytes a checkout takes on the disk (allocated blocks, links not followed)."""
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            total += (getattr(st, "st_blocks", 0) * 512) or st.st_size
    return total


def _repos_root() -> Path:
    """REPOS_ROOT, made when missing, open to this Host's own user only
    (0700, as the vault and the harness vault folders are held:
    server/integrations/core/credentials.py, server/routes/harness_vault.py).
    A checkout is a copy of a repo the Host was granted; nothing else that
    shares the volume under another user or group (the pod's fsGroup) may
    list or read it."""
    root = Path(indexer.REPOS_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(root)
    if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(root, 0o700)
    return root


def _room() -> tuple[int, int, int]:
    """(room, free, keep) on the disk holding REPOS_ROOT: `room` is what one
    repo may still take. The disk keeps back what every big writer on the
    Host keeps back (server/harness_input.disk_floor: a tenth of the volume,
    at least 256 MiB, at most 1 GiB), and one repo may take at most half of
    what is free above that: the index is a copy of something the forge
    already holds, and must never crowd out the user's own files."""
    from server.harness_input import disk_floor, free_bytes
    root = _repos_root()
    free, keep = free_bytes(root), disk_floor(root)
    return max(0, (free - keep) // 2), free, keep


def room_refusal(need_bytes: int, what: str = "its checkout and its index need") -> Optional[str]:
    """None when `need_bytes` more bytes fit the room one repo may take
    (_room); else the sentence saying why they do not."""
    need = max(0, int(need_bytes))
    if need <= 0:
        return None
    room, free, keep = _room()
    if need <= room:
        return None
    return (f"too big for the room left on this Host's disk: {what} about "
            f"{max(1, need >> 20)} MiB, {max(0, free) >> 20} MiB is free and "
            f"{keep >> 20} MiB is kept free; one repo may use at most half of the rest")


def _inode_room() -> Optional[int]:
    """How many more files and folders one repo may make on the disk holding
    REPOS_ROOT, on the same terms as _room (a tenth of the volume's inodes
    kept back, at most half of the rest), or None when the filesystem reports
    no inode count. A tree of a million empty files packs to a few KiB and
    writes no data block, yet takes a million inodes."""
    try:
        st = os.statvfs(_repos_root())
    except OSError:
        return None
    if not st.f_files:
        return None
    return max(0, (int(st.f_favail) - int(st.f_files) // 10) // 2)


# The listing git prints for a tree: `-t` names every folder once (a record of
# its own), so a folder is counted without remembering every path seen.
_LS_TREE = ("ls-tree", "-r", "-t", "-l", "-z")


class _TreeSize:
    """What writing a tree costs, one `git ls-tree -r -t -l -z` record at a
    time: every blob's size rounded up to the disk's block, its entry in
    .git/index (62 bytes plus its path, padded to 8), a block per folder, and
    the database rows the index of the files it reads will take (indexer.
    indexes_path; DB_BYTES_PER_*); plus how many inodes (files and folders)
    it makes. A submodule entry (a commit) is never fetched or written, so it
    costs nothing."""

    def __init__(self, block: int = 4096):
        self.block = max(1, int(block))
        self.files = self.dirs = self.disk = self.index_files = self.index_bytes = 0

    def feed(self, rec: bytes) -> None:
        meta, tab, path = rec.partition(b"\t")
        fields_ = meta.split()
        if not tab or len(fields_) != 4:
            return
        if fields_[1] == b"tree":
            self.dirs += 1
            self.disk += self.block
            return
        if fields_[1] != b"blob":
            return
        try:
            size = int(fields_[3])
        except ValueError:
            return
        rel = path.decode("utf-8", "surrogateescape")
        self.files += 1
        self.disk += -(-size // self.block) * self.block + (62 + len(path) + 8) // 8 * 8
        if fields_[0] != b"120000" and indexer.indexes_path(rel, size):
            self.index_files += 1
            self.index_bytes += size

    @property
    def inodes(self) -> int:
        return self.files + self.dirs

    @property
    def need(self) -> int:
        return self.disk + int(DB_BYTES_PER_FILE * self.index_files
                               + DB_BYTES_PER_SOURCE_BYTE * self.index_bytes)

    def result(self) -> dict:
        db = self.need - self.disk
        return {"files": self.files, "dirs": self.dirs, "inodes": self.inodes,
                "tree_bytes": self.disk, "index_files": self.index_files,
                "index_bytes": self.index_bytes, "db_bytes": db, "need": self.need}


def tree_need(listing: bytes, block: int = 4096) -> dict:
    """What writing a tree costs (_TreeSize), from the whole of a
    `git ls-tree -r -t -l -z <rev>` listing. Returns {"files", "dirs",
    "inodes", "tree_bytes", "index_files", "index_bytes", "db_bytes",
    "need"}."""
    size = _TreeSize(block)
    for rec in listing.split(b"\0"):
        size.feed(rec)
    return size.result()


_OVER = -1000   # _measure stopped git: the tree is past the room it was given


async def _measure(git: tuple, rev: str, cwd: str, env: dict, block: int, *,
                   max_need: Optional[int] = None, max_inodes: Optional[int] = None,
                   timeout: float = 600.0) -> tuple[int, "_TreeSize"]:
    """Size the tree at `rev` (_TreeSize) from git's listing as it streams,
    never holding the listing whole. Stops git and returns (_OVER, the size
    so far) once the tree needs more than `max_need` bytes or `max_inodes`
    files and folders; else (git's exit code, the size)."""
    size = _TreeSize(block)
    proc = await asyncio.create_subprocess_exec(
        *git, *_LS_TREE, rev, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    buf = b""
    rc: Optional[int] = None
    try:
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(1 << 16),
                                           timeout=max(0.1, deadline - loop.time()))
            if not chunk:
                break
            *recs, buf = (buf + chunk).split(b"\0")
            for rec in recs:
                size.feed(rec)
            if ((max_need is not None and size.need > max_need)
                    or (max_inodes is not None and size.inodes > max_inodes)):
                rc = _OVER
                break
            if len(buf) > (1 << 20):      # no record is a MiB long: not a listing
                rc = 1
                break
        if rc is None:
            if buf:
                size.feed(buf)
            rc = await asyncio.wait_for(proc.wait(), timeout=max(0.1, deadline - loop.time()))
    except asyncio.TimeoutError:
        rc = 124
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return rc, size


def _limit_file_size(limit: int) -> Callable[[], None]:
    """preexec: no file git writes may grow past `limit` bytes (a fetch is
    one pack file: _SYNC_GIT_CONFIG unpackLimit), and no core file. SIGXFSZ
    is ignored, so the write past the limit fails with EFBIG and git says
    "File too large" (measured on git 2.39.5, the Host image's, and 2.53):
    killed by the signal, index-pack dies silently and the clone reports
    only "invalid index-pack output"."""
    def _pre() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    return _pre


def _discard(dest: Path) -> None:
    """Remove a checkout (or what a refused fetch left), and its owner's
    folder once empty. `dest` is inside the repos root (checkout checks)."""
    shutil.rmtree(dest, ignore_errors=True)
    try:
        dest.parent.rmdir()
    except OSError:
        pass


async def checkout(repo: str, *, ref: str = "") -> dict:
    """Clone (first time) or fetch + reset (refresh) `repo` into
    REPOS_ROOT/<owner>/<name> as the HOST — its own gitea credential, through
    the pod's egress proxy; no token in a URL or an argument. With no ref, the
    remote's CURRENT default branch, asked of the remote each time.

    The objects are fetched first (no work tree), within the room (_room) as
    a file-size limit on git; then the tree is sized from them (_measure,
    _TreeSize: its files as they will be written, their .git/index entries,
    its folders and its index rows; and how many inodes it makes, against
    _inode_room) and written only if that fits. Returns {"ok", "dest", "head", "need"} or {"ok": False,
    "error"}; with "skipped": True (and "need" when measured) when the repo
    does not fit, and then nothing of it is left on the disk."""
    g = _gitea()
    cred = g.gitea_creds()
    if not cred:
        return {"ok": False, "error": "this host has no gitea identity yet (no credential in the pod)"}
    _user, token, host = cred
    try:
        dest = indexer.project_root_for(repo)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    # Belt and braces before anything is deleted or written: the
    # resolved destination must sit strictly inside the repos root.
    repos_root = Path(indexer.REPOS_ROOT).resolve()
    try:
        rel = dest.resolve().relative_to(repos_root)
    except ValueError:
        rel = None
    if rel is None or len(rel.parts) != 2:
        return {"ok": False, "error": f"refusing to write outside the repos root for {repo!r}"}
    owner_dir = dest.parent
    url = clone_url(host, repo)
    # The credential travels through git's own store (credential.helper
    # store, ~/.git-credentials) — never on the command line. The proxy is
    # the same egress lane every gitea API call takes. No global or system
    # config is read (_SYNC_GIT_CONFIG says what that keeps out).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
           "https_proxy": g.gitea_proxy(), "HTTPS_PROXY": g.gitea_proxy()}

    # The repo can hold harness output a build pushed (host/build), so the
    # clone/fetch/reset carry the shared hardened -c set too. git may speak
    # only the clone URL's own protocol: nothing in the repo can make it
    # fetch from anywhere else.
    from server.routes.project_git import HARDENED_GIT_CONFIG
    scheme = (urlsplit(url).scheme or "file").lower()
    config = (*HARDENED_GIT_CONFIG, *_SYNC_GIT_CONFIG, "-c", f"protocol.{scheme}.allow=always")

    async def _git(*argv: str, cwd: Optional[str] = None, timeout: float = 600.0,
                   max_file: Optional[int] = None):
        proc = await asyncio.create_subprocess_exec(
            "git", *config, *argv, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limit_file_size(max_file) if max_file else None)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "timed out"
        text = ((err or b"") + (out or b"")).decode("utf-8", "ignore").strip()
        # Never let the token echo into a job row or a log line.
        return proc.returncode, (text.replace(token, "***") if token else text)[:400]

    try:
        _repos_root()
        owner_dir.mkdir(exist_ok=True)
        fresh = not (dest / ".git").exists()
        # The fetch: the forge's size was checked before this call
        # (sync_repo); here git itself cannot write a pack past the room.
        room, _free, _keep = _room()
        if room <= 0:
            return {"ok": False, "skipped": True, "error": room_refusal(1, "its fetch needs")}
        if not fresh:
            # The copy is the kernel's own clone, but git is never run in a
            # tree whose .git holds what the kernel does not write there.
            from server.routes.project_git import repo_refusal
            why = repo_refusal(dest)
            if why:
                log.warning("repo sync of %s refused: %s", repo, why)
                return {"ok": False, "error": f"git was not run in the copy of {repo}: {why}"}
            # Refresh. With no ref, follow the remote's CURRENT default
            # branch, asked of the remote each time — a shallow clone made
            # with --branch has no origin/HEAD, and a plain clone's
            # origin/HEAD is a local symref frozen at clone time, so both
            # would silently stay on an old branch.
            want = ref
            if not want:
                rc, out = await _git("ls-remote", "--symref", url, "HEAD", cwd=str(dest), timeout=60)
                if rc == 0:
                    for line in out.splitlines():
                        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
                            want = line.split()[1][len("refs/heads/"):]
                            break
                if not want:
                    return {"ok": False, "error": f"could not read the default branch of {repo}: "
                                                  f"{out or 'no symref'}"}
            rc, out = await _git("fetch", "--depth", "1", "--no-tags", "origin", want,
                                 cwd=str(dest), max_file=room)
            target = "FETCH_HEAD"
        else:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)  # a half clone, inside repos_root by the check above
            argv = (["clone", "--no-checkout", "--depth", "1", "--no-tags"]
                    + (["--branch", ref] if ref else []) + [url, str(dest)])
            rc, out = await _git(*argv, max_file=room)
            target = "HEAD"
        if rc != 0:
            if rc == -signal.SIGXFSZ or "File too large" in out:
                # More than the room, by at least a byte: fetched again only
                # once there is more room than there is now.
                _discard(dest)
                return {"ok": False, "skipped": True, "need": room + 1,
                        "error": room_refusal(room + 1, "its fetch needs") or "its fetch outgrew the room"}
            if fresh:
                _discard(dest)
            return {"ok": False, "error": f"git failed for {repo}: {out or 'unknown error'}"}

        # Nothing of the work tree is written yet. Turn off every transform
        # a checkout could apply, then size the tree from the objects.
        info = dest / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text(_NO_TRANSFORMS)
        block = os.statvfs(dest).f_frsize or 4096
        have = _TreeSize(block)
        if not fresh:
            # The tree written now, and its rows, are replaced: they count as
            # room the repo already has.
            rc, have = await _measure(("git", *config), "HEAD", str(dest), env, block)
            if rc != 0:
                have = _TreeSize(block)
        inode_room = _inode_room()
        # Read the listing as it streams, and stop git the moment the tree
        # is past the room: a few KiB of nested trees can name a billion
        # files, whose listing would never fit in memory.
        rc, new = await _measure(
            ("git", *config), target, str(dest), env, block,
            max_need=room + have.need,
            max_inodes=None if inode_room is None else inode_room + have.inodes)
        if rc not in (0, _OVER):
            if fresh:
                _discard(dest)
            return {"ok": False, "error": f"git failed for {repo}: could not list {target}"}
        if inode_room is not None and new.inodes - have.inodes > inode_room:
            _discard(dest)
            return {"ok": False, "skipped": True,
                    "error": (f"too many files for this Host's disk: its checkout makes at least "
                              f"{new.inodes - have.inodes} files and folders, and one repo may make "
                              f"at most {inode_room}, half of the disk's free inodes above a tenth "
                              f"kept free")}
        refusal = room_refusal(max(new.need - have.need, room + 1 if rc == _OVER else 0))
        if refusal:
            _discard(dest)
            return {"ok": False, "skipped": True, "error": refusal, "need": new.need}
        new = new.result()
        rc, out = await _git("reset", "-q", "--hard", target, cwd=str(dest))
        if rc != 0:
            if fresh:
                _discard(dest)
            return {"ok": False, "error": f"git failed for {repo}: {out or 'unknown error'}"}
        rc, out = await _git("rev-parse", "HEAD", cwd=str(dest), timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"git failed for {repo}: {e}"}
    return {"ok": True, "dest": dest, "head": out.strip() if rc == 0 else "", "need": new["need"]}


def _remove_checkout(repo: str) -> None:
    try:
        dest = indexer.project_root_for(repo)
        rel = dest.resolve().relative_to(Path(indexer.REPOS_ROOT).resolve())
    except ValueError:
        return
    if len(rel.parts) == 2 and dest.exists() and not dest.is_symlink():
        shutil.rmtree(dest, ignore_errors=True)
        try:
            dest.parent.rmdir()          # the owner's folder, once it is empty
        except OSError:
            pass


async def drop_repo(db, repo: str) -> int:
    """Read access is gone: the repo's rows, its checkout and its record go."""
    async with _lock(repo):
        n = await store.drop_project(db, project=repo)
        _remove_checkout(repo)
        await db.execute("DELETE FROM code_repos WHERE repo = $1", repo)
    log.info("code search repos: %s dropped (%d chunks) — no longer readable", repo, n)
    return n


async def _record(db, repo: str, **f) -> None:
    """Upsert the repo's code_repos row. `head` is written only when given
    (None clears it: the next pass indexes again). `need_head`/`need_bytes`
    are written every time (absent clears them)."""
    await db.execute("""
        INSERT INTO code_repos (repo, default_branch, head, forge_size_kib, disk_bytes,
                                state, reason, checked_at, indexed_at, need_head, need_bytes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now(), CASE WHEN $8 THEN now() END, $10, $11)
        ON CONFLICT (repo) DO UPDATE SET
            default_branch = EXCLUDED.default_branch,
            head = CASE WHEN $9 THEN EXCLUDED.head ELSE code_repos.head END,
            forge_size_kib = EXCLUDED.forge_size_kib,
            disk_bytes = COALESCE(EXCLUDED.disk_bytes, code_repos.disk_bytes),
            state = EXCLUDED.state, reason = EXCLUDED.reason, checked_at = now(),
            indexed_at = COALESCE(EXCLUDED.indexed_at, code_repos.indexed_at),
            need_head = EXCLUDED.need_head, need_bytes = EXCLUDED.need_bytes
    """, repo, f.get("default_branch"), f.get("head"), f.get("size_kib"), f.get("disk_bytes"),
        f["state"], f.get("reason"), bool(f.get("indexed")), "head" in f,
        f.get("need_head"), f.get("need_bytes"))


async def sync_repo(db, row: dict, *, force: bool = False,
                    before_index: Optional[Callable[[], Awaitable[bool]]] = None) -> dict:
    """Bring one readable repo's index up to its default branch. `row` is
    one entry of readable_repos(). Returns {"repo", "state", ...}:
    unchanged | indexed | skipped | failed. `force` indexes even when the
    head has not moved (an embed job asked for it). `before_index`, when
    given, is awaited after the checkout and may return False to stop there."""
    repo = row["repo"]
    branch = row.get("default_branch") or ""
    size_kib = int(row.get("size_kib") or 0)
    async with _lock(repo):
        prev = await db.fetchrow("SELECT * FROM code_repos WHERE repo = $1", repo)
        base = {"default_branch": branch, "size_kib": size_kib}
        if row.get("empty") or not branch:
            await _record(db, repo, **base, state="skipped", reason="the repository is empty")
            return {"repo": repo, "state": "skipped", "reason": "the repository is empty"}
        head = await remote_head(repo, branch)
        if not head:
            reason = f"could not read the head of {branch} on the forge"
            await _record(db, repo, **base, state="failed", reason=reason)
            return {"repo": repo, "state": "failed", "reason": reason}
        try:
            dest = indexer.project_root_for(repo)
        except ValueError as e:
            await _record(db, repo, **base, state="failed", reason=str(e))
            return {"repo": repo, "state": "failed", "reason": str(e)}
        if (not force and prev is not None and prev["state"] == "indexed"
                and prev["head"] == head and (dest / ".git").exists()
                and await indexer.chunked_by_this_version(db, project=repo, project_root=dest)
                and await indexer.vectors_current(db, project=repo)):
            return {"repo": repo, "state": "unchanged", "head": head}   # nothing written
        refusal = None
        need = None
        if (prev is not None and prev["state"] == "skipped" and prev["need_head"] == head
                and prev["need_bytes"]):
            # Refused at this very head after a fetch: fetched again only
            # once the room it measured is there.
            need = int(prev["need_bytes"])
            refusal = room_refusal(need)
        elif not (dest / ".git").exists():
            # Nothing on the disk yet: the forge's own size of the repo (its
            # packed history) must fit before a byte is fetched.
            refusal = room_refusal(size_kib * 1024, "its fetch needs")
        if refusal is None:
            co = await checkout(repo)
            if co.get("skipped"):
                refusal, need = co.get("error") or "too big for the room left", co.get("need")
            elif not co.get("ok"):
                await _record(db, repo, **base, state="failed", reason=co.get("error"))
                return {"repo": repo, "state": "failed", "reason": co.get("error")}
        if refusal:
            await _record(db, repo, **base, state="skipped", reason=refusal,
                          need_head=head if need else None, need_bytes=need)
            log.info("code search repos: %s skipped — %s", repo, refusal)
            return {"repo": repo, "state": "skipped", "reason": refusal}
        disk = await asyncio.to_thread(_tree_bytes, co["dest"])
        if before_index is not None and await before_index() is False:
            await _record(db, repo, **base, disk_bytes=disk, state="failed",
                          reason="stopped before indexing")
            return {"repo": repo, "state": "failed", "reason": "stopped before indexing"}
        try:
            summary = await indexer.index_project(db, project=repo, project_root=str(co["dest"]))
        except Exception as e:  # noqa: BLE001 — the database or the lane failed mid-way
            summary = {"error": f"index failed for {repo}: {e}"}
        if summary.get("error"):
            await _record(db, repo, **base, disk_bytes=disk, state="failed",
                          reason=str(summary["error"])[:500])
            return {"repo": repo, "state": "failed", "reason": str(summary["error"])[:500]}
        # Whole = every chunk has its vector from the configured model. With
        # no model set up nothing failed, yet nothing has one: the summary's
        # `embedder` sentence says so, and it is the reason recorded.
        whole = not summary.get("chunks_failed") and not summary.get("embedder")
        reason = None if whole else (summary.get("embedder") or
                                     f"{summary.get('chunks_failed')} chunks not embedded yet; "
                                     "the next pass tries again")
        # The head is recorded once nothing is left for a later pass at this
        # head: every chunk has its vector, or no model is set up — then every
        # file is written for keyword search and nothing can have a vector
        # until one is (the reason above says so; indexer.vectors_current sees
        # the new model's empty column then, and the repo is indexed again).
        # Until then the next pass indexes again (an unchanged file costs a
        # hash; the fetch before it costs a round trip to the forge).
        current = whole or bool(summary.get("no_embedding_model"))
        await _record(db, repo, **base, disk_bytes=disk, state="indexed", reason=reason,
                      indexed=True, head=(co.get("head") or head) if current else None)
        return {"repo": repo, "state": "indexed", "head": co.get("head") or head,
                **{k: summary.get(k) for k in ("files_changed", "chunks_written",
                                                "chunks_failed", "files_deleted")},
                **({"reason": reason} if reason else {})}


async def known_repos(db) -> set[str]:
    """Every repo this Host's index knows of: on record, checked out, or with
    rows in code_chunks ("owner/name" projects — a Workbench project's name
    never has a slash)."""
    have = {r["repo"] for r in await db.fetch("SELECT repo FROM code_repos")}
    have |= set(indexer.list_repos())
    have |= {r["project"] for r in await db.fetch(
        "SELECT DISTINCT project FROM code_chunks WHERE project LIKE '%/%'")}
    return have


async def sync_once(db) -> dict:
    """One pass over every repo the Host can read. Never raises for one
    repo's failure; returns what it did."""
    known = await known_repos(db)       # a grant just made is on record (remember_grant)
    readable = await readable_repos(also=known)
    if readable is None:
        log.info("code search repos: the forge's list could not be read; nothing changed this pass")
        return {"ok": False, "reason": "could not read which repos this host can read"}
    want = {r["repo"]: r for r in readable}
    out: dict = {"ok": True, "readable": len(want), "dropped": [], "indexed": [],
                 "unchanged": [], "skipped": [], "failed": []}
    for repo in sorted(known - set(want)):
        if _valid(repo):
            await drop_repo(db, repo)
            out["dropped"].append(repo)
    for repo in sorted(want):
        try:
            res = await sync_repo(db, want[repo])
        except Exception as e:  # noqa: BLE001
            log.warning("code search repos: %s not synced (%s)", repo, e)
            res = {"repo": repo, "state": "failed", "reason": str(e)[:300]}
        bucket = out.get(res["state"])
        if isinstance(bucket, list):
            bucket.append(repo if res["state"] in ("indexed", "unchanged")
                          else {"repo": repo, "reason": res.get("reason")})
    log.info("code search repos: %d readable, %d indexed, %d unchanged, %d skipped, %d failed, "
             "%d dropped", len(want), len(out["indexed"]), len(out["unchanged"]),
             len(out["skipped"]), len(out["failed"]), len(out["dropped"]))
    return out


async def not_searchable(db) -> list[dict]:
    """Readable repos the index does not hold whole, and why — for the
    search result, so the Host knows what it is not seeing."""
    return [{"repo": r["repo"], "state": r["state"], "reason": r["reason"]}
            for r in await db.fetch("""
                SELECT repo, state, reason FROM code_repos
                 WHERE state <> 'indexed' OR reason IS NOT NULL
                 ORDER BY repo""")]


async def run_forever(db, *, interval: float | None = None, first: float | None = None) -> None:
    """The background loop: a pass FIRST_SYNC_S after start, then every
    SYNC_S, or at once when wake() is called. Never raises."""
    global _wake_event
    _wake_event = asyncio.Event()
    delay = FIRST_SYNC_S if first is None else first
    every = SYNC_S if interval is None else interval
    while True:
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        _wake_event.clear()
        try:
            await sync_once(db)
        except Exception as e:  # noqa: BLE001
            log.warning("code search repos: pass failed (%s)", e)
        delay = every
