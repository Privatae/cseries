"""
Propose a change to a repo the host does NOT own — fork, branch, pull request.

This is the host's ONLY write lane to someone else's repository, and it is
PR-only by construction. Before it, the host could open a
PR on exactly one kind of repo: its OWN project repo (project_git._ensure_pr,
one rolling host/build PR), and the `gitea` tool was read-only by contract.
Proposing a fix to someone else's repository needed either this
or a write-permission collaborator grant plus a branch push; this one keeps
"no write access to the upstream" literally true.

What one call does, all as the host's own gitea identity (entity-<id>):

  1. read the upstream repo (refuses a repo the host owns — that path
     already exists, and it is not a fork-and-PR),
  2. ensure a fork under the host's account exists (reused if present),
  3. sync the fork's base branch from upstream (gitea merge-upstream),
  4. in a temp clone: branch propose/<slug>-<id> off the base, apply the
     change, ONE commit, push the branch to the FORK,
  5. open a PR upstream, head entity-<id>:<branch> → base — or, when the
     same branch_name comes back, push the new commit and update the PR
     that is already open instead of opening a second one,
  6. record the call in gitea_propose_audit (opened / updated / refused /
     error — every call, the way the management lane records nac_mgmt_audit).

The change comes in one of three forms, each applied in the
temp clone on the propose branch's head when the branch already exists (a
revision), else on the upstream base:

  files         [{path, content | edits | op='delete'}]. `content` is the
                WHOLE file; `edits` [{old_string, new_string, replace_all?}]
                change it in place, the way Claude Code edits — an old_string
                that is missing, or matches more than once without
                replace_all, refuses the call;
  patch         a unified diff (git diff format), checked with
                `git apply --check` and applied to the index;
  from_project  a Workbench project mounted on, or copied from, this repo: the
                files it changed against the commit it started from are read
                from its tree server-side, so a harness build becomes the PR
                without passing through the model (_project_changes).

The upstream never receives a push: every git push here names the fork's
URL, and the only non-GET requests that touch the upstream are "create
fork" and "open/update pull request". Merging is never done here — the
user or the upstream owner merges.

Credentials: git reads them from the pod's own credential store
(entrypoint.sh wires ~/.git-credentials + credential.helper store), the same
way embed._run_repo_job clones a granted repo. No token in a URL, no token in
an argument, and git's output is scrubbed of it before it lands anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.pc.gitea_propose")

router = APIRouter(prefix="/api/gitea", tags=["gitea"])
_WRITE = Depends(requires_permission("write:gitea"))

# No file-count or per-file size cap. The old
# 50 files / 1 MiB refused a legitimate refactor and a lockfile, and protected
# nothing: the whole body is already parsed into memory before _validate runs.
# The change travels as a git push, so the real limits are git's and the
# forge's, and a push the forge refuses comes back as a 502 carrying git's own
# words ("git push to the fork: …").
BRANCH_PREFIX = "propose/"
_GIT_TIMEOUT = 300

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


# ── request shape ───────────────────────────────────────────────────────────

class ProposeEdit(BaseModel):
    old_string: str
    new_string: str = ""
    replace_all: bool = False


class ProposeFile(BaseModel):
    path: str
    content: str = ""
    encoding: str = "utf-8"        # 'utf-8' or 'base64'
    op: str = "write"              # 'write', 'delete' or 'edit'
    edits: list[ProposeEdit] | None = None


class ProposeBody(BaseModel):
    repo: str
    title: str
    body: str = ""
    files: list[ProposeFile] = []
    patch: str | None = None
    from_project: int | None = None
    paths: list[str] | None = None
    base: str | None = None
    branch_name: str | None = None


@router.post("/propose", dependencies=[_WRITE])
async def propose_route(body: ProposeBody) -> dict:
    return await propose(body.model_dump(exclude_none=True))


# ── validation (nothing below touches gitea until this passes) ──────────────

def _entity_id() -> int:
    try:
        return int(os.environ.get("ENTITY_ID", "0") or 0)
    except ValueError:
        return 0


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40].strip("-") or "change"


def _clean_rel_path(path: str) -> str:
    """A repo-relative path with no way out of the tree: no absolute, no drive,
    no '..' segment, no empty segment, and never inside .git."""
    p = (path or "").strip().replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise HTTPException(400, f"file path must be repo-relative: {path!r}")
    parts = p.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        raise HTTPException(400, f"file path must be a clean repo-relative path: {path!r}")
    # .git at ANY depth: the top-level one is the checkout's own config and
    # hooks (a write there runs under attacker-controlled git settings); a
    # nested one is a submodule's, which is not this repo's content either.
    if any(seg.lower() == ".git" for seg in parts):
        raise HTTPException(400, f"refusing to write inside .git: {path!r}")
    return "/".join(parts)


def _jailed_target(root: Path, rel: str, *, leaf_link_ok: bool) -> Path:
    """The on-disk path for a cleaned repo-relative path, or a refusal.

    The name check above cannot see the checkout: an upstream repo may ship
    a symlink (`cfg -> .git/config`, `vendor -> /`), and a path THROUGH it
    resolves wherever the link points — inside root for the .git case,
    which a plain "resolved under root" jail waves through (reproduced with
    real git). So: no component of the UN-resolved
    path may be a symlink — except the leaf on a delete, where the link
    itself is what gets removed — and the resolved target may never be
    root/.git or anything under it.
    """
    parts = rel.split("/")
    cur = root
    for i, seg in enumerate(parts):
        cur = cur / seg
        if os.path.islink(cur) and not (leaf_link_ok and i == len(parts) - 1):
            raise HTTPException(400, f"{rel!r} goes through a symlink in the checkout; "
                                     "delete the link first if you mean to replace it")
    target = root / rel
    resolved = (target.parent.resolve() / target.name) if (leaf_link_ok and target.is_symlink()) \
        else target.resolve()
    git_dir = root / ".git"
    if resolved == git_dir or git_dir in resolved.parents or root not in resolved.parents:
        raise HTTPException(400, f"{rel!r} resolves outside the repo")
    return target


def _decode(f: dict) -> bytes:
    enc = (f.get("encoding") or "utf-8").lower()
    content = f.get("content") or ""
    if enc == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except Exception:
            raise HTTPException(400, f"content of {f.get('path')!r} is not valid base64")
    elif enc in ("utf-8", "utf8", "text"):
        data = content.encode("utf-8")
    else:
        raise HTTPException(400, f"encoding must be 'utf-8' or 'base64' (got {enc!r})")
    return data


def _validate_edits(rel: str, edits) -> list[dict]:
    if not isinstance(edits, list) or not edits:
        raise HTTPException(400, f"edits for {rel!r} must be a non-empty list of "
                                 "{old_string, new_string, replace_all?}")
    out = []
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            raise HTTPException(400, f"edits[{i}] for {rel!r} must be an object")
        old = e.get("old_string")
        new = e.get("new_string", "")
        if not isinstance(old, str) or not old:
            raise HTTPException(400, f"edits[{i}] for {rel!r} needs a non-empty old_string")
        if not isinstance(new, str):
            raise HTTPException(400, f"edits[{i}] for {rel!r}: new_string must be text")
        if old == new:
            raise HTTPException(400, f"edits[{i}] for {rel!r}: old_string and new_string are the same")
        out.append({"old": old, "new": new, "all": bool(e.get("replace_all"))})
    return out


_PATCH_PATH_RE = re.compile(r"^(?:diff --git a/(.+?) b/(.+)|--- (?:a/)?(.+)|\+\+\+ (?:b/)?(.+)|"
                            r"rename (?:from|to) (.+)|copy (?:from|to) (.+))$")


_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _patch_paths(patch: str) -> list[str]:
    """Every path a unified diff names, cleaned like a file path (no absolute,
    no '..', never .git). git apply has its own checks; these refuse before
    anything is fetched, in the same words as a bad file path.

    Only header lines name files. A hunk's body is content, and a removed
    "-- comment" line reads "--- comment" (an added "++ x", "+++ x"): the
    line counts in each "@@ -a,b +c,d @@" say where the body ends, the way
    git apply reads it."""
    out: list[str] = []
    old_left = new_left = 0
    for raw in patch.split("\n"):
        line = raw.rstrip("\r")
        if old_left > 0 or new_left > 0:
            c = line[:1]
            if c == " " or line == "":
                old_left -= 1
                new_left -= 1
                continue
            if c == "-":
                old_left -= 1
                continue
            if c == "+":
                new_left -= 1
                continue
            if c == "\\":                 # "\ No newline at end of file"
                continue
            old_left = new_left = 0       # a short hunk: read it as a header
        h = _HUNK_RE.match(line)
        if h:
            old_left = int(h.group(1)) if h.group(1) is not None else 1
            new_left = int(h.group(2)) if h.group(2) is not None else 1
            continue
        m = _PATCH_PATH_RE.match(line)
        if not m:
            continue
        for g in m.groups():
            if g is None:
                continue
            g = g.strip()
            if g == "/dev/null" or not g:
                continue
            g = g.split("\t", 1)[0]
            rel = _clean_rel_path(g)
            if rel not in out:
                out.append(rel)
    return out


def _validate(payload: dict) -> dict:
    p = payload or {}
    repo = str(p.get("repo") or "").strip().strip("/")
    if not _REPO_RE.match(repo):
        raise HTTPException(400, "repo must be 'owner/name' on the forge")
    title = " ".join(str(p.get("title") or "").split())
    if not title:
        raise HTTPException(400, "title is required — it becomes the PR title and the commit subject")
    body = str(p.get("body") or "").strip()
    files_in = p.get("files")
    patch = p.get("patch")
    from_project = p.get("from_project")
    sources = [k for k, v in (("files", files_in), ("patch", patch),
                              ("from_project", from_project)) if v not in (None, "", [])]
    if not sources:
        raise HTTPException(400, "files must be a non-empty list of {path, content | edits, op} "
                                 "— or pass a unified diff in patch, or from_project")
    if len(sources) > 1:
        raise HTTPException(400, f"pass one of files, patch or from_project, not {' and '.join(sources)}")
    base = str(p.get("base") or "").strip() or None
    if base and not _BRANCH_RE.match(base):
        raise HTTPException(400, "base must be a branch name")
    branch = str(p.get("branch_name") or "").strip() or None
    if branch:
        if not branch.startswith(BRANCH_PREFIX):
            branch = BRANCH_PREFIX + branch
        if not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith("/"):
            raise HTTPException(400, "branch_name must be a clean branch name")
    spec = {"repo": repo, "title": title[:200], "body": body, "files": [],
            "base": base, "branch": branch, "patch": None, "from_project": None, "paths": None}
    if patch not in (None, ""):
        if not isinstance(patch, str):
            raise HTTPException(400, "patch must be a unified diff (text)")
        names = _patch_paths(patch)
        if not names:
            raise HTTPException(400, "patch names no file — it must be a unified diff "
                                     "(git diff format)")
        spec["patch"] = patch if patch.endswith("\n") else patch + "\n"
        spec["patch_paths"] = names
        return spec
    if from_project not in (None, ""):
        try:
            spec["from_project"] = int(from_project)
        except (TypeError, ValueError):
            raise HTTPException(400, "from_project must be a project id")
        paths = p.get("paths")
        if paths not in (None, []):
            if not isinstance(paths, list):
                raise HTTPException(400, "paths must be a list of repo-relative paths")
            spec["paths"] = [_clean_rel_path(str(x)) for x in paths]
        return spec
    if not isinstance(files_in, list):
        raise HTTPException(400, "files must be a non-empty list of {path, content, op}")
    files: list[dict] = []
    seen: set[str] = set()
    for f in files_in:
        if not isinstance(f, dict):
            raise HTTPException(400, "each file must be an object {path, content, op}")
        rel = _clean_rel_path(str(f.get("path") or ""))
        if rel in seen:
            raise HTTPException(400, f"{rel!r} appears twice")
        seen.add(rel)
        op = str(f.get("op") or "write").lower()
        if op not in ("write", "delete", "edit"):
            raise HTTPException(400, f"op must be 'write', 'delete' or 'edit' for {rel!r}")
        if f.get("edits") not in (None, []):
            # `op` defaults to 'write' (the route's model fills it in): edits
            # make it an edit, and never a whole-file write of empty content.
            if op == "delete":
                raise HTTPException(400, f"{rel!r}: a delete takes no edits")
            op = "edit"
        elif op == "edit":
            raise HTTPException(400, f"{rel!r}: op='edit' needs edits")
        if op == "edit":
            if f.get("content"):
                raise HTTPException(400, f"{rel!r}: pass content (the whole file) or edits, not both")
            files.append({"path": rel, "op": "edit", "data": b"",
                          "edits": _validate_edits(rel, f.get("edits"))})
            continue
        files.append({"path": rel, "op": op, "data": _decode(f) if op == "write" else b""})
    spec["files"] = files
    return spec


# ── the git half (runs in a thread; every push names the FORK) ──────────────

def _run_git(argv: list[str], cwd: str, env: dict, timeout: int = _GIT_TIMEOUT) -> tuple[int, str]:
    """One git call, no shell. Module-level so tests replace it.
    The checkout holds files the caller supplies (which can be a harness's
    output), so every call carries the shared hardened -c set
    (routes/project_git.HARDENED_GIT_CONFIG)."""
    from server.routes.project_git import HARDENED_GIT_CONFIG
    try:
        r = subprocess.run(["git", *HARDENED_GIT_CONFIG, *argv], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    return r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()


def _apply_edits(target: Path, rel: str, edits: list[dict]) -> bytes:
    """The file at `target` with each edit applied in order — the exact-string
    replacement Claude Code makes. Refuses a missing file, a non-text file, an
    old_string that is not there, and one that matches more than once without
    replace_all (which place was meant is unknowable)."""
    if target.is_symlink() or not target.is_file():
        raise HTTPException(400, f"edits need an existing file: {rel!r} is not a file on this branch")
    try:
        text = target.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, f"{rel!r} is not utf-8 text — send its whole content instead")
    for i, e in enumerate(edits):
        n = text.count(e["old"])
        if n == 0:
            raise HTTPException(400, f"edits[{i}] for {rel!r}: old_string was not found in the "
                                     "file on this branch — read it again and copy the text exactly")
        if n > 1 and not e["all"]:
            raise HTTPException(400, f"edits[{i}] for {rel!r}: old_string matches {n} places — add "
                                     "surrounding lines so it matches one, or set replace_all")
        text = text.replace(e["old"], e["new"]) if e["all"] else text.replace(e["old"], e["new"], 1)
    return text.encode("utf-8")


def _merge_file(ours: bytes, base: bytes, theirs: bytes) -> bytes | None:
    """git merge-file of three versions of one file: the merged bytes, or None
    when the changes overlap (or the file is not text)."""
    tmp = tempfile.mkdtemp(prefix="gitea-propose-merge-")
    try:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            f = Path(tmp) / name
            f.write_bytes(data)
            paths.append(str(f))
        from server.routes.project_git import HARDENED_GIT_CONFIG
        try:
            r = subprocess.run(["git", *HARDENED_GIT_CONFIG, "merge-file", "-p", "-q", *paths],
                               cwd=tmp, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return None
        return r.stdout if r.returncode == 0 else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _resolve_project_file(root: Path, f: dict, own: bool) -> dict | None:
    """What one file of a from_project proposal becomes on this checkout:
    {path, op, data, exec} to write or delete, or None when the checkout
    already holds the project's version. `f` = {path, base, theirs, exec}
    (bytes or None for absent). The checkout's copy ("ours") decides:
    unchanged since the project's base, or written by an earlier commit of
    this same proposal (`own`) -> the project's version; else the two are
    merged, and overlapping changes refuse."""
    rel = f["path"]
    target = _jailed_target(root, rel, leaf_link_ok=f["theirs"] is None)
    if target.is_symlink():
        raise HTTPException(409, f"{rel!r} is a symlink on this branch; propose writes files")
    ours = target.read_bytes() if target.is_file() else None
    theirs = f["theirs"]
    if ours == theirs:
        return None
    out_op = "delete" if theirs is None else "write"
    if ours == f["base"] or own:
        return {"path": rel, "op": out_op, "data": theirs or b"", "exec": f.get("exec")}
    if ours is None or theirs is None or f["base"] is None:
        raise _Conflict(rel)
    merged = _merge_file(ours, f["base"], theirs)
    if merged is None:
        raise _Conflict(rel)
    return {"path": rel, "op": "write", "data": merged, "exec": f.get("exec")}


class _Conflict(Exception):
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


def _git_work(*, host: str, host_login: str, token: str, upstream_full: str,
              fork_full: str, base: str, branch: str, branch_exists: bool,
              files: list[dict], message: str, patch: str | None = None,
              patch_paths: list[str] | None = None,
              project_files: list[dict] | None = None,
              own_paths: frozenset = frozenset()) -> dict:
    from server.routes.gitea_client import gitea_proxy
    upstream_url = f"https://{host}/{upstream_full}.git"
    fork_url = f"https://{host}/{fork_full}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "https_proxy": gitea_proxy(), "HTTPS_PROXY": gitea_proxy()}
    ident = ["-c", f"user.name={host_login}", "-c", f"user.email={host_login}@{host}"]
    tmp = tempfile.mkdtemp(prefix="gitea-propose-")
    root = Path(tmp).resolve()

    def git(*argv: str) -> tuple[int, str]:
        rc, out = _run_git(list(argv), str(root), env)
        # The token must never echo into a result, a log line or an audit row.
        return rc, (out.replace(token, "***") if token else out)[:400]

    try:
        rc, out = git("init", "-q")
        if rc != 0:
            raise HTTPException(502, f"git init: {out}")
        # Start from the branch's own tip when updating an open proposal (so
        # the PR keeps its history), else from the upstream base tip — the
        # fork was just synced to it, so the fork already holds that commit
        # and a shallow push of one new commit on top is enough.
        src_url, src_ref = (fork_url, branch) if branch_exists else (upstream_url, base)
        rc, out = git("fetch", "-q", "--depth", "1", src_url, f"refs/heads/{src_ref}")
        if rc != 0:
            raise HTTPException(502, f"git fetch {src_ref} from {src_url}: {out}")
        rc, out = git("checkout", "-q", "-b", branch, "FETCH_HEAD")
        if rc != 0:
            raise HTTPException(502, f"git checkout: {out}")

        touched: list[str] = []
        modes: list[tuple[str, bool]] = []
        if project_files is not None:
            # from_project: each file the project changed, resolved against
            # this checkout (the propose branch's head on a revision).
            files = []
            conflicts = []
            for pf in project_files:
                try:
                    r = _resolve_project_file(root, pf, pf["path"] in own_paths)
                except _Conflict as c:
                    conflicts.append(c.path)
                    continue
                if r is not None:
                    files.append(r)
            if conflicts:
                raise HTTPException(409, "the upstream changed these files since the project's "
                                         "base and the changes overlap with the project's: "
                                         + ", ".join(conflicts[:20])
                                         + " — bring the project up to date with the upstream "
                                           "first, then propose again")
            if not files:
                raise HTTPException(400, "no changes — the branch already holds the project's files")
        for f in files:
            # The path was cleaned before any request went out; this second
            # check sees the checkout and refuses symlinks and .git.
            target = _jailed_target(root, f["path"], leaf_link_ok=(f["op"] == "delete"))
            if f["op"] == "delete":
                if target.is_symlink() or target.is_file():
                    target.unlink()          # a link: the LINK goes, never its target
                    touched.append(f["path"])
                continue
            data = _apply_edits(target, f["path"], f["edits"]) if f["op"] == "edit" else f["data"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            touched.append(f["path"])
            if f.get("exec") is not None:
                modes.append((f["path"], bool(f["exec"])))
        if patch is not None:
            # The paths were cleaned before any request; this sees the
            # checkout (no symlink on the way, never .git).
            for rel in patch_paths or []:
                _jailed_target(root, rel, leaf_link_ok=True)
            pfile = Path(tempfile.mkdtemp(prefix="gitea-propose-patch-")) / "change.patch"
            try:
                pfile.write_text(patch, encoding="utf-8")
                rc, out = git("apply", "--check", "--index", str(pfile))
                if rc != 0:
                    raise HTTPException(409, f"the patch does not apply to {branch if branch_exists else base}: {out}")
                rc, out = git("apply", "--index", str(pfile))
                if rc != 0:
                    raise HTTPException(409, f"git apply: {out}")
            finally:
                shutil.rmtree(pfile.parent, ignore_errors=True)
        if not touched and patch is None:
            raise HTTPException(400, "nothing to change — every delete named a path that does not exist")
        if touched:
            rc, out = git("add", "-A", "--", *touched)
            if rc != 0:
                raise HTTPException(502, f"git add: {out}")
        if patch is not None:
            # Already in the index (apply --index); a deleted path would make
            # `git add` fail on a pathspec that matches nothing.
            touched.extend(p for p in (patch_paths or []) if p not in touched)
        for rel, executable in modes:
            git("update-index", f"--chmod={'+' if executable else '-'}x", "--", rel)
        from server.routes.project_git import HARDENED_DIFF_FLAGS
        rc, _ = git("diff", "--cached", "--quiet", *HARDENED_DIFF_FLAGS)
        if rc == 0:
            raise HTTPException(400, "no changes — the files already match the branch")
        rc, out = git(*ident, "commit", "-q", "-m", message)
        if rc != 0:
            raise HTTPException(502, f"git commit: {out}")
        rc, sha = git("rev-parse", "HEAD")
        rc, out = git("push", "-q", fork_url, f"HEAD:refs/heads/{branch}")
        if rc != 0 and not branch_exists:
            # A fork whose base could not be synced may lack the parent of
            # our shallow commit; give git the full ancestry and try once more.
            rc2, out2 = git("fetch", "-q", "--unshallow", upstream_url, f"refs/heads/{base}")
            if rc2 == 0:
                rc, out = git("push", "-q", fork_url, f"HEAD:refs/heads/{branch}")
        if rc != 0:
            raise HTTPException(502, f"git push to the fork: {out}")
        return {"commit": (sha or "").strip()[:40], "files": touched}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── the gitea half ──────────────────────────────────────────────────────────

def _q(s: str) -> str:
    return quote(s, safe="")


async def _ensure_fork(gitea_request, host_login: str, owner: str, name: str) -> dict:
    """The host's fork of owner/name, created if it does not exist yet."""
    async def _find() -> dict | None:
        for page in (1, 2, 3):
            forks = await gitea_request("GET", f"/api/v1/repos/{_q(owner)}/{_q(name)}/forks",
                                        params={"limit": 50, "page": page})
            forks = forks if isinstance(forks, list) else []
            for f in forks:
                if ((f.get("owner") or {}).get("login")) == host_login:
                    return f
            if len(forks) < 50:
                break
        return None

    found = await _find()
    if found:
        return found
    try:
        return await gitea_request("POST", f"/api/v1/repos/{_q(owner)}/{_q(name)}/forks", json={})
    except HTTPException as e:
        if e.status_code != 409:
            raise
        # 409 = a repo of that name already exists under the host. Either it
        # is the fork (a race) or an unrelated repo; only the first is usable.
        found = await _find()
        if found:
            return found
        raise HTTPException(409, f"{host_login}/{name} already exists and is not a fork of {owner}/{name}; "
                                 "rename or remove it before proposing")


async def _sync_fork(gitea_request, fork_full: str, base: str) -> tuple[bool, str]:
    fo, fn = fork_full.split("/", 1)
    try:
        await gitea_request("POST", f"/api/v1/repos/{_q(fo)}/{_q(fn)}/merge-upstream",
                            json={"branch": base})
        return True, ""
    except HTTPException as e:
        return False, str(e.detail)[:200]


async def _branch_exists(gitea_request, repo_full: str, branch: str) -> bool:
    o, n = repo_full.split("/", 1)
    try:
        r = await gitea_request("GET", f"/api/v1/repos/{_q(o)}/{_q(n)}/branches/{_q(branch)}")
        return isinstance(r, dict) and bool(r.get("name"))
    except HTTPException as e:
        if e.status_code == 404:
            return False
        raise


async def _ensure_pr(gitea_request, owner: str, name: str, host_login: str, fork_full: str,
                     branch: str, base: str, title: str, body: str) -> tuple[dict, bool]:
    """(pr, updated). One open PR per proposal branch — a second call with the
    same branch updates it (the shape project_git._ensure_pr uses)."""
    head_label = f"{host_login}:{branch}"
    for page in (1, 2, 3):
        prs = await gitea_request("GET", f"/api/v1/repos/{_q(owner)}/{_q(name)}/pulls",
                                  params={"state": "open", "limit": 50, "page": page})
        prs = prs if isinstance(prs, list) else []
        for pr in prs:
            head = pr.get("head") or {}
            same_ref = head.get("ref") == branch
            same_repo = ((head.get("repo") or {}).get("full_name") == fork_full
                         or head.get("label") == head_label)
            if same_ref and same_repo:
                updated = await gitea_request(
                    "PATCH", f"/api/v1/repos/{_q(owner)}/{_q(name)}/pulls/{pr['number']}",
                    json={"title": title, "body": body})
                return (updated if isinstance(updated, dict) and updated.get("number") else pr), True
        if len(prs) < 50:
            break
    pr = await gitea_request("POST", f"/api/v1/repos/{_q(owner)}/{_q(name)}/pulls",
                             json={"head": head_label, "base": base, "title": title, "body": body})
    return pr, False


# ── audit (every call, the way nac_mgmt.audit_mgmt records the mgmt lane) ───
#
# The table is created by migrations/v134_gitea_propose_audit.sql (and
# init-db.sql for new entities), not here — never a DDL per call.
# `files` is the JSON list of paths and `commit` the sha, so a proposal is
# still reconstructable after the fork branch is deleted.

async def _audit(*, repo: str, fork: str | None, branch: str | None, pr_number: int | None,
                 pr_url: str | None, files: list[str], outcome: str, detail: str = "",
                 commit: str | None = None) -> None:
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return
        await pool.execute(
            "INSERT INTO gitea_propose_audit (entity_id, op, repo, fork, branch, pr_number, pr_url, "
            "files, file_count, commit, outcome, detail) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12)",
            _entity_id(), "gitea:propose", repo, fork, branch, pr_number, pr_url,
            json.dumps(list(files or [])), len(files or []), commit, outcome, (detail or "")[:500])
    except Exception as e:  # pragma: no cover - the audit must never mask the result
        log.warning("gitea propose audit write failed: %s", e)


# ── from_project: the files a Workbench project changed ─────────────────────

def _project_git(project_dir: Path, *argv: str, env_extra: dict | None = None,
                 text: bool = True) -> subprocess.CompletedProcess:
    """One git call in a project's tree, hardened the way every host-side git
    call in a harness-written tree is (project_git.HARDENED_GIT_CONFIG), with
    no global or system config. Module-level so tests can see it."""
    from server.routes.project_git import HARDENED_GIT_CONFIG
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           **(env_extra or {})}
    return subprocess.run(["git", *HARDENED_GIT_CONFIG, "-C", str(project_dir), *argv],
                          env=env, capture_output=True, text=text, timeout=_GIT_TIMEOUT)


def _project_changes_blocking(project_dir: Path, base_ref: str, default_branch: str,
                              paths: list[str] | None) -> dict:
    """{base, files: [{path, base, theirs, exec}]} — every file the project's
    tree (as it is on disk, committed or not, the project's ignore rules
    applied) changed against its base commit: `base_ref` (a fork's recorded
    commit), or the merge base of the work branch with the repository's
    default branch for a project mounted on it. A temporary index holds the
    snapshot, so the project's own index is never touched."""
    from server.routes.project_git import HARDENED_DIFF_FLAGS
    if not (project_dir / ".git").is_dir():
        raise HTTPException(409, "the project has no repository in its directory yet — its code "
                                 "has not been read in")
    if not base_ref:
        r = _project_git(project_dir, "merge-base", "HEAD", f"refs/remotes/origin/{default_branch}")
        base_ref = (r.stdout or "").strip()
        if r.returncode != 0 or not base_ref:
            raise HTTPException(409, "the project's starting commit could not be found (its "
                                     f"branch shares no history with {default_branch})")
    r = _project_git(project_dir, "rev-parse", "--verify", "-q", f"{base_ref}^{{commit}}")
    if r.returncode != 0:
        raise HTTPException(409, f"the project's base commit {base_ref[:12]} is not in its repository")
    base_sha = r.stdout.strip()
    tmpdir = tempfile.mkdtemp(prefix="gitea-propose-index-")
    idx = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
    try:
        # Start from a copy of the project's own index when it has one: its
        # stat cache spares `add -A` re-reading every unchanged file of a large
        # repository. After `add -A` the snapshot is the tree on disk either way.
        # copy2, never copyfile: git trusts a cached stat only for entries older
        # than the index file itself, and re-reads the rest ("racily clean").
        # A copy stamped now made every entry look older, so a file rewritten at
        # the same size in the same second as the project's last index write was
        # read as unchanged and left out of the PR (3 of 24 runs under load).
        own_index = project_dir / ".git" / "index"
        if own_index.is_file():
            shutil.copy2(own_index, idx["GIT_INDEX_FILE"])
        else:
            r = _project_git(project_dir, "read-tree", base_sha, env_extra=idx)
            if r.returncode != 0:
                raise HTTPException(502, f"git read-tree: {r.stderr.strip()[:200]}")
        r = _project_git(project_dir, "add", "-A", env_extra=idx)
        if r.returncode != 0:
            raise HTTPException(502, f"git add: {r.stderr.strip()[:200]}")
        spec = ["--", *paths] if paths else []
        r = _project_git(project_dir, "diff", "--cached", "--name-status", "--no-renames", "-z",
                         *HARDENED_DIFF_FLAGS, base_sha, *spec, env_extra=idx)
        if r.returncode != 0:
            raise HTTPException(502, f"git diff: {r.stderr.strip()[:200]}")
        parts = [x for x in r.stdout.split("\0") if x != ""]
        changed = list(zip(parts[0::2], parts[1::2]))
        files: list[dict] = []
        refused: list[str] = []
        for status, rel in changed:
            rel = _clean_rel_path(rel)
            theirs = mode = None
            if status != "D":
                ls = _project_git(project_dir, "ls-files", "-s", "--", rel, env_extra=idx)
                mode = (ls.stdout.split(" ", 1)[0] if ls.stdout else "")
                if mode not in ("100644", "100755"):
                    refused.append(rel)       # a symlink or a nested repository
                    continue
                b = _project_git(project_dir, "cat-file", "blob", f":{rel}",
                                 env_extra=idx, text=False)
                theirs = b.stdout
            base_blob = None
            if status != "A":
                b = _project_git(project_dir, "cat-file", "blob", f"{base_sha}:{rel}", text=False)
                if b.returncode != 0:
                    refused.append(rel)       # it was not a file at the base
                    continue
                base_blob = b.stdout
            files.append({"path": rel, "base": base_blob, "theirs": theirs,
                          "exec": None if mode is None else mode == "100755"})
        if refused:
            raise HTTPException(400, "propose carries files only; these are links or nested "
                                     "repositories in the project: " + ", ".join(refused[:20]))
        return {"base": base_sha, "files": files}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _project_changes(project_id: int, repo: str, paths: list[str] | None) -> dict:
    """The from_project change set: the project must be this Host's, and be
    mounted on `repo` or copied from it (projects.forked_from). Read under the
    project's lock, so a build landing at that moment is never half-read."""
    from server import brain_service
    from server.routes.project_git import _PROJECTS_ROOT, _seed_state, project_lock
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "the database is not ready")
    row = await pool.fetchrow(
        "SELECT repo, forked_from, config FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, brain_service._entity_id)
    if row is None:
        raise HTTPException(404, f"project {project_id} not found")
    ff = row["forked_from"]
    if isinstance(ff, str):
        try:
            ff = json.loads(ff)
        except ValueError:
            ff = None
    seed = _seed_state(row["config"]) or {}
    if isinstance(ff, dict) and str(ff.get("repo") or "").lower() == repo.lower():
        base_ref = str(ff.get("commit") or "")
        if not base_ref:
            raise HTTPException(409, f"project {project_id}'s copy of {repo} has not finished "
                                     "reading in yet; propose once it has")
    elif str(row["repo"] or "").lower() == repo.lower():
        base_ref = ""
    else:
        raise HTTPException(400, f"project {project_id} is not on {repo} (it is "
                                 f"{row['repo'] or 'not on a repository'}"
                                 + (f", copied from {ff.get('repo')}" if isinstance(ff, dict) else "")
                                 + "); from_project needs a project mounted on, or copied from, "
                                   "the repository the PR goes to")
    if seed and not seed.get("done"):
        raise HTTPException(409, f"project {project_id}'s code has not been read in yet; "
                                 "propose once it has")
    default_branch = str(seed.get("default_branch") or "main")
    async with project_lock(project_id):
        return await asyncio.to_thread(
            _project_changes_blocking, _PROJECTS_ROOT / str(int(project_id)),
            base_ref, default_branch, paths)


async def _own_paths(repo: str, branch: str) -> frozenset:
    """The files earlier commits of this same proposal wrote (its audit rows):
    on a revision the branch holds those, so the project's current version
    replaces them instead of being merged against them."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return frozenset()
        rows = await pool.fetch(
            "SELECT files FROM gitea_propose_audit WHERE entity_id = $1 AND repo = $2 "
            "AND branch = $3 AND outcome IN ('opened', 'updated')",
            _entity_id(), repo, branch)
    except Exception as e:  # noqa: BLE001 — merged against instead; overlaps refuse
        log.warning("gitea propose: earlier files of %s unreadable: %s", branch, e)
        return frozenset()
    out: set[str] = set()
    for r in rows or []:
        f = r["files"]
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except ValueError:
                f = []
        out.update(str(x) for x in (f or []))
    return frozenset(out)


# ── the whole thing ─────────────────────────────────────────────────────────

async def propose(payload: dict) -> dict:
    """Fork → branch → commit → push to the fork → PR upstream. Raises
    HTTPException with a plain sentence on every refusal; returns
    {pr_url, pr_number, fork, branch, commit, updated, fork_synced, files}."""
    repo_for_audit = str((payload or {}).get("repo") or "")[:200]
    raw_files = (payload or {}).get("files")
    # What was ASKED for, for the audit row of a refusal (cleaned paths are
    # not available until validation passes).
    asked = [str((f or {}).get("path") or "")[:300] for f in raw_files
             if isinstance(f, dict)] if isinstance(raw_files, list) else []
    try:
        spec = _validate(payload)
    except HTTPException as e:
        await _audit(repo=repo_for_audit, fork=None, branch=None, pr_number=None, pr_url=None,
                     files=asked, outcome="refused", detail=str(e.detail))
        raise
    paths = [f["path"] for f in spec["files"]]

    from server.routes import gitea_client as GC
    cred = GC.gitea_creds()
    if not cred:
        await _audit(repo=spec["repo"], fork=None, branch=None, pr_number=None, pr_url=None,
                     files=paths, outcome="refused", detail="no gitea identity")
        raise HTTPException(409, "this host has no gitea identity yet (no credential in the pod)")
    host_login, token, host = cred
    gitea_request = GC.gitea_request
    owner, name = spec["repo"].split("/", 1)

    fork_full = branch = None
    pr_number = pr_url = None
    work: dict = {}
    project_files = None
    try:
        if spec["from_project"] is not None:
            # Read before anything is sent: a project that is not on this
            # repository, or has nothing to propose, refuses here.
            changes = await _project_changes(spec["from_project"], spec["repo"], spec["paths"])
            project_files = changes["files"]
            paths = [f["path"] for f in project_files]
            if not project_files:
                raise HTTPException(400, f"project {spec['from_project']} has no changes against "
                                         f"the commit it started from ({changes['base'][:12]})")
        elif spec["patch"] is not None:
            paths = list(spec["patch_paths"])
        if owner == host_login:
            raise HTTPException(400, f"{spec['repo']} is the host's own repo — commit to it directly "
                                     "(the project/Workbench path); propose is for repos the host does not own")
        upstream = await gitea_request("GET", f"/api/v1/repos/{_q(owner)}/{_q(name)}")
        if not isinstance(upstream, dict):
            raise HTTPException(502, "gitea returned no repo facts")
        if ((upstream.get("owner") or {}).get("login")) == host_login:
            raise HTTPException(400, f"{spec['repo']} is owned by the host — propose is for repos it does not own")
        base = spec["base"] or upstream.get("default_branch") or "main"
        if not await _branch_exists(gitea_request, spec["repo"], base):
            raise HTTPException(404, f"branch {base!r} does not exist on {spec['repo']}")

        fork = await _ensure_fork(gitea_request, host_login, owner, name)
        fork_full = fork.get("full_name") or f"{host_login}/{name}"
        synced, sync_err = await _sync_fork(gitea_request, fork_full, base)

        branch = spec["branch"] or f"{BRANCH_PREFIX}{_slug(spec['title'])}-{secrets.token_hex(3)}"
        exists = await _branch_exists(gitea_request, fork_full, branch)
        message = spec["title"] + ("\n\n" + spec["body"] if spec["body"] else "")
        own = (await _own_paths(spec["repo"], branch)
               if exists and project_files is not None else frozenset())
        work = await asyncio.to_thread(
            _git_work, host=host, host_login=host_login, token=token,
            upstream_full=spec["repo"], fork_full=fork_full, base=base, branch=branch,
            branch_exists=exists, files=spec["files"], message=message,
            patch=spec["patch"], patch_paths=spec.get("patch_paths"),
            project_files=project_files, own_paths=own)

        pr, updated = await _ensure_pr(gitea_request, owner, name, host_login, fork_full,
                                       branch, base, spec["title"], spec["body"])
        pr_number = pr.get("number")
        pr_url = pr.get("html_url")
    except HTTPException as e:
        await _audit(repo=spec["repo"], fork=fork_full, branch=branch, pr_number=pr_number,
                     pr_url=pr_url, files=work.get("files") or paths, commit=work.get("commit"),
                     outcome="refused" if e.status_code < 500 else "error", detail=str(e.detail))
        raise
    except Exception as e:  # noqa: BLE001
        await _audit(repo=spec["repo"], fork=fork_full, branch=branch, pr_number=None, pr_url=None,
                     files=work.get("files") or paths, commit=work.get("commit"),
                     outcome="error", detail=repr(e))
        raise HTTPException(502, f"propose failed: {e}")

    await _audit(repo=spec["repo"], fork=fork_full, branch=branch, pr_number=pr_number,
                 pr_url=pr_url, files=work["files"], commit=work["commit"],
                 outcome="updated" if updated else "opened",
                 detail="" if synced else f"fork not synced: {sync_err}")
    return {"pr_url": pr_url, "pr_number": pr_number, "fork": fork_full, "branch": branch,
            "base": base, "commit": work["commit"], "files": work["files"],
            "updated": updated, "fork_synced": synced,
            "note": ("The upstream received no push: the branch lives on the fork and only the PR "
                     "touches " + spec["repo"] + ". The user or the upstream owner merges; the host never does.")}
