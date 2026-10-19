"""
Artifact service — the ARTIFACT primitive.

An ARTIFACT is a DISTINCT first-class thing, NOT an app and NOT (quite) a
project. The host builds a live artifact that is disposable but convertible
to an app: fast, lightweight, spun up in a default harness pod, stored as a
project like an app but not bound to the app SDK. The user can convert an
artifact to an app with the app SDK, and the two terms are never conflated.

This module is the SINGLE source of truth for the artifact lifecycle, shared by
the HTTP routes (server/routes/artifacts.py) AND the host tools (create_artifact
/ artifact_preview / convert_artifact in runtime/tools.py) so every invariant
lives in ONE place and neither caller can bypass it.

DISTINCTNESS (no conflation):
  * An artifact is a `projects` row with kind='artifact' (v075). A normal
    project is kind='project'. An APP is an installed_applications row produced
    by the App SDK. These are three different things; the artifact NEVER passes
    through the App SDK while it is an artifact.
  * It reuses the project WORKSPACE path (/var/lib/cseries/projects/<id>) so the
    in-pod live-build preview, code-index, and (on convert) the ship pipeline
    all work for free — but it is kept SEPARATE by the kind marker and by its
    own tools, never surfaced or shipped as an app until the user converts it.

RUNTIME (design fork a — see HOST tool docs + the returned `design_forks`):
  An artifact is a fast, disposable, in-pod LIVE build: a workspace of static
  assets (HTML/JS/CSS/SVG) that the host writes, served self-contained by
  artifact_render() (mirrors review.preview_harness_index — local-asset inlining,
  worktree-jailed). This is the cheapest "spun up live build" the harness infra
  allows today: zero pod spin-up, instant, disposable. A real default-harness-pod
  live build (a served server runtime) is a LATER upgrade; it depends
  on what the harness pool cheaply allows per artifact.

CONVERT (design fork c):
  convert_to_app() turns an artifact into an app via the App SDK: it requires a
  provenance=custom app.yaml in the workspace, flips the row to kind='project'
  + status='complete' + disposable=false, then hands the SAME workspace to
  ship_project_service (pack → SDK-bind → entity-sign → install). The artifact
  and the app are still distinct records — the app is the installed bundle; the
  (now-promoted) project row is its build source.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional

log = logging.getLogger("privatae.applications.artifacts")


class ArtifactError(Exception):
    """Service-layer failure carrying an HTTP-ish status + detail (mirrors
    ShipError) so the route maps it to an HTTPException and the host tool
    surfaces .detail."""

    def __init__(self, status: int, detail: str, entries: list | None = None,
                 basis: str | None = None):
        self.status = status
        self.detail = detail
        # For a 300 (several pages, none to open by default): the pages, so a
        # route can answer with the same {detail, entries, basis} shape the
        # preview routes use and a window can offer them.
        self.entries = entries
        self.basis = basis
        super().__init__(detail)


def _projects_root() -> Path:
    # Lazy import — server.routes.applications owns PROJECTS_ROOT and importing
    # it at module load would be a circular import (this module is imported by
    # that route file's siblings). Same pattern as _exec_import_repo.
    from server.routes.applications import PROJECTS_ROOT
    return Path(PROJECTS_ROOT)


# A single entry file the artifact build writes; the host can override it.
_DEFAULT_ENTRY = "index.html"
# A display name, not a path: the workspace lives under the artifact's id, so
# the name never touches the filesystem. Dashes (en/em), curly and straight
# quotes, and the ordinary title punctuation below are allowed — the host
# names its designs the way it writes ("Inkwell — dark variant") and the old
# `.,()-` set refused the em dash AFTER an agent had been woken to place the
# artifact (two tasks: one wasted run each).
_NAME_RE = re.compile(r"^[\w .,()\-–—'’\"“”:;&!?+]{1,120}$")


def is_valid_artifact_name(name: str) -> bool:
    """Does this name satisfy the artifact naming rule?

    Exposed so callers that create an artifact LATER can apply the same rule
    NOW. The design library publishes under a name and only builds
    `Desktop - <name>` at install time, so without this a name containing `/`
    would sit in the gallery looking real and fail for everyone who picked it.
    One rule, one place — a second copy of the character class would drift the
    first time either moved.
    """
    return _NAME_RE.match((name or "").strip()) is not None


# ─────────────────────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────────────────────
async def create_artifact(
    pool,
    entity_id: int,
    *,
    name: str,
    files: list[dict] | None = None,
    entry: str = _DEFAULT_ENTRY,
    description: str = "",
) -> dict:
    """Create a disposable, lightweight LIVE artifact for `entity_id`.

    Inserts a projects row with kind='artifact', disposable=true, status='active'
    and writes the supplied `files` into its workspace at
    /var/lib/cseries/projects/<id>/. Each file is {path, content}; paths are
    workspace-jailed (no abs/traversal). Returns {artifact_id, name, entry,
    files_written, preview_url}.

    This is the fast path — no pod spin-up. The workspace is immediately
    renderable via artifact_render() once it contains an entry doc.
    """
    name = (name or "").strip()
    if not name:
        raise ArtifactError(400, "name is required")
    if not is_valid_artifact_name(name):
        raise ArtifactError(
            400,
            "name must be 1-120 chars of letters/digits/space and .,()-–—'\"“”:;&!?+")
    entry = (entry or _DEFAULT_ENTRY).strip() or _DEFAULT_ENTRY
    _reject_unsafe_path(entry)

    # No design template is stamped: there is no per-project style, and an
    # artifact wears whatever desktop the user is running when it is shown
    # (server/applications/artifact_style.py).
    row = await pool.fetchrow(
        "INSERT INTO projects (entity_id, name, description, status, kind, disposable) "
        "VALUES ($1,$2,$3,'active','artifact',true) RETURNING id",
        entity_id, name, description or f"Artifact: {name}",
    )
    artifact_id = row["id"]
    workspace = _projects_root() / str(artifact_id)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        written = _write_files(workspace, files or [])
    except ArtifactError:
        # Roll back the row so a bad file payload doesn't orphan an artifact.
        await pool.execute(
            "DELETE FROM projects WHERE id=$1 AND entity_id=$2 AND kind='artifact'",
            artifact_id, entity_id)
        raise
    except Exception as e:  # pragma: no cover - defensive
        await pool.execute(
            "DELETE FROM projects WHERE id=$1 AND entity_id=$2 AND kind='artifact'",
            artifact_id, entity_id)
        raise ArtifactError(500, f"workspace write failed: {e}") from None

    _snapshot(workspace, actor="host", op="create")
    log.info("artifact %d created for entity %d (%d files)", artifact_id, entity_id, written)
    return {
        "artifact_id": artifact_id,
        "name": name,
        "entry": entry,
        "files_written": written,
        "preview_url": f"/api/artifacts/{artifact_id}/render",
        "disposable": True,
    }


async def _desktop_after_write(pool, entity_id: int, artifact_id: int,
                               wait_s: Optional[float]) -> Optional[dict]:
    """Every write below ends here: when the artifact is the WORN desktop, its
    new render goes through the browser render check before it reaches the
    screen (server/applications/desktop_pin.py). None for any other artifact.
    Never fails the write it follows."""
    try:
        from server.applications import desktop_pin
        return await desktop_pin.after_write(pool, entity_id, int(artifact_id), wait_s=wait_s)
    except Exception as e:  # noqa: BLE001
        log.warning("worn-desktop check after a write to %s failed: %s", artifact_id, e)
        return None


async def write_files(pool, entity_id: int, artifact_id: int, files: list[dict],
                      *, actor: str = "host", note: str = "",
                      desktop_wait_s: Optional[float] = None) -> dict:
    """Write/overwrite files in an existing artifact's workspace (the host
    iterates on a live artifact). Refuses anything that isn't the host's own
    kind='artifact' row. Every write appends a change-history revision
    (actor = who caused it: 'host', 'user' for tune-mode edits)."""
    await _require_artifact(pool, entity_id, artifact_id)
    workspace = _projects_root() / str(artifact_id)
    workspace.mkdir(parents=True, exist_ok=True)
    written = _write_files(workspace, files or [])
    _snapshot(workspace, actor=actor, op="write", note=note)
    # Stamp the row — updated_at is the Artifacts window's live-refresh signal
    # (the viewer polls the list and re-renders when this moves).
    await pool.execute(
        "UPDATE projects SET updated_at=now() WHERE id=$1 AND entity_id=$2",
        artifact_id, entity_id)
    out = {"artifact_id": artifact_id, "files_written": written}
    if (hnote := _take_history_note(workspace)):
        out["history"] = hnote
    desk = await _desktop_after_write(pool, entity_id, artifact_id, desktop_wait_s)
    if desk is not None:
        out["desktop"] = desk
    return out


# ─────────────────────────────────────────────────────────────────────────────
# FILE OPERATIONS THAT NEVER TOUCH A MODEL
# ─────────────────────────────────────────────────────────────────────────────
# Origin: an agent spent 5m14s and ~11k output tokens re-typing a
# 37 KB variant-2.html into index.html because "make that file the entry" had
# no operation behind it — the only way to copy a file was render → model →
# write. Before that, two tasks spent 57 minutes and ~1.2M tokens moving
# the same document out of a project workspace the same way. A copy is a copy:
# these two do it server-side, byte-for-byte, as one revision each.

async def promote_entry(pool, entity_id: int, artifact_id: int, source: str,
                        target: str = _DEFAULT_ENTRY, *, actor: str = "user",
                        desktop_wait_s: Optional[float] = None) -> dict:
    """Make one root file of the artifact its entry: copy `source` (e.g.
    variant-2.html) over `target` (default index.html) byte-for-byte, inside
    the workspace jail, as ONE recorded revision. The source stays in place —
    variants remain pickable. Sub-second, zero tokens."""
    await _require_artifact(pool, entity_id, artifact_id)
    source = (source or "").strip()
    target = (target or _DEFAULT_ENTRY).strip() or _DEFAULT_ENTRY
    _reject_unsafe_path(source)
    _reject_unsafe_path(target)
    workspace = _projects_root() / str(artifact_id)
    ws = workspace.resolve()
    src = (workspace / source).resolve()
    dst = (workspace / target).resolve()
    for p in (src, dst):
        if not (p == ws or ws in p.parents):
            raise ArtifactError(400, "path escapes the artifact workspace")
    if not src.is_file():
        raise ArtifactError(404, f"{source!r} not found in artifact {artifact_id}")
    if src == dst:
        return {"artifact_id": artifact_id, "promoted": source, "entry": target,
                "bytes": src.stat().st_size, "changed": False}
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    rev = _snapshot(workspace, actor=actor, op="promote", note=f"{source} → {target}")
    await pool.execute(
        "UPDATE projects SET updated_at=now() WHERE id=$1 AND entity_id=$2",
        artifact_id, entity_id)
    log.info("artifact %d: promoted %s → %s (%d bytes) for entity %d",
             artifact_id, source, target, len(data), entity_id)
    out = {"artifact_id": artifact_id, "promoted": source, "entry": target,
           "bytes": len(data), "changed": True, "rev": rev}
    if (hnote := _take_history_note(workspace)):
        out["history"] = hnote
    desk = await _desktop_after_write(pool, entity_id, artifact_id, desktop_wait_s)
    if desk is not None:
        out["desktop"] = desk
    return out


async def import_files(pool, entity_id: int, artifact_id: int, project_id: int,
                       files: list[dict], *, actor: str = "host",
                       desktop_wait_s: Optional[float] = None) -> dict:
    """Copy files from ANOTHER of this entity's project workspaces into the
    artifact — the landed output of a build becoming a live artifact without
    a model re-emitting it. `files` = [{from, to?}] relative to each
    workspace; `to` defaults to the same relative path. Both sides are jailed
    to their own workspace; the source must belong to this entity. One
    revision for the whole batch."""
    await _require_artifact(pool, entity_id, artifact_id)
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        raise ArtifactError(400, "project_id must be an integer") from None
    if project_id == int(artifact_id):
        raise ArtifactError(400, "import source is the artifact itself — use op='promote' for that")
    owner = await pool.fetchval(
        "SELECT entity_id FROM projects WHERE id=$1", project_id)
    if owner is None or int(owner) != int(entity_id):
        raise ArtifactError(404, f"project {project_id} not found")
    src_ws = (_projects_root() / str(project_id)).resolve()
    if not src_ws.is_dir():
        raise ArtifactError(404, f"project {project_id} has no workspace on disk")
    workspace = _projects_root() / str(artifact_id)
    workspace.mkdir(parents=True, exist_ok=True)
    ws = workspace.resolve()
    if not files:
        raise ArtifactError(400, "files=[{from, to?}] is required")
    copied: list[dict] = []
    for f in files:
        if not isinstance(f, dict):
            raise ArtifactError(400, "each file must be {from, to?}")
        rel_from = (f.get("from") or "").strip()
        rel_to = (f.get("to") or rel_from).strip()
        _reject_unsafe_path(rel_from)
        _reject_unsafe_path(rel_to)
        src = (src_ws / rel_from).resolve()
        dst = (workspace / rel_to).resolve()
        if not (src == src_ws or src_ws in src.parents) or _HISTORY_DIR in src.parts:
            raise ArtifactError(400, f"source path escapes project {project_id}: {rel_from!r}")
        if not (dst == ws or ws in dst.parents):
            raise ArtifactError(400, f"target path escapes the artifact workspace: {rel_to!r}")
        if not src.is_file():
            raise ArtifactError(404, f"{rel_from!r} not found in project {project_id}")
        data = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        copied.append({"from": rel_from, "to": rel_to, "bytes": len(data)})
    rev = _snapshot(workspace, actor=actor, op="import",
                    note=f"from project {project_id}: " + ", ".join(c["to"] for c in copied))
    await pool.execute(
        "UPDATE projects SET updated_at=now() WHERE id=$1 AND entity_id=$2",
        artifact_id, entity_id)
    log.info("artifact %d: imported %d file(s) from project %d for entity %d",
             artifact_id, len(copied), project_id, entity_id)
    out = {"artifact_id": artifact_id, "from_project_id": project_id,
           "files": copied, "rev": rev}
    if (hnote := _take_history_note(workspace)):
        out["history"] = hnote
    desk = await _desktop_after_write(pool, entity_id, artifact_id, desktop_wait_s)
    if desk is not None:
        out["desktop"] = desk
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE HISTORY (the artifact's timeline — every write is a revision)
# ─────────────────────────────────────────────────────────────────────────────
# Layout inside the workspace jail:
#     .history/log.jsonl        one line per revision (rev, ts, actor, op, note)
#     .history/<rev>/…          full snapshot of the workspace at that revision
# Invariant: every mutating path (create / write / bridge edit / revert)
# snapshots AFTER the mutation, so the newest revision always equals the
# current state. Identical-content snapshots are skipped (digest compare).
# .history never renders as a variant (root-glob only), never ships in a
# bundle (bundles.pack skips hidden entries), and render_workspace's
# index.html fallback excludes it.
_HISTORY_DIR = ".history"
# Every revision keeps EVERY file, whatever its size: a revision used to leave
# out files over 1 MiB, and a restore, which empties the workspace first, then
# deleted them. A file unchanged since the revision before is a hard link to
# that revision's copy, so it is stored once however many revisions hold it
# (.history/sums/<rev>.json records each file's sha256 to decide). Revisions
# are never pruned by count. The oldest go only when the next one's new bytes
# do not fit above _DISK_RESERVE_FRACTION of the volume (ext4's default
# reserved-blocks share, the space a full volume keeps for the system), and
# then only the shortest run whose OWN bytes cover the shortfall — a volume
# full of other data never wipes the history for nothing. The revision says
# what was removed, or that the volume is past its reserve; a change the disk
# cannot hold at all is not recorded, and the op's answer says so
# (_take_history_note).
_SUMS_DIR = "sums"
_DISK_RESERVE_FRACTION = 0.05
# What the last snapshot of a workspace had to say about the disk (a revision
# not recorded, or the volume past its reserve), taken by the op that caused
# it so the answer says so: {str(workspace): note}.
_HISTORY_NOTES: dict = {}


def _take_history_note(workspace: Path) -> str:
    return _HISTORY_NOTES.pop(str(workspace), "")
# An older revision may leave out files over this size. Its log
# line has no "complete" mark, and restoring it keeps such files rather than
# deleting what the revision never recorded.
_LEGACY_FILE_MAX = 1_048_576
_REV_RE = re.compile(r"^\d{4,}$")


def _history_root(workspace: Path) -> Path:
    return workspace / _HISTORY_DIR


# Everything that WRITES into .history goes through a descriptor on the real
# folder (_history_fd), never through the path: .history is the server's own
# store, and a `.history` swapped for a link would otherwise carry every
# snapshot, the log and the prune out of the workspace (the prune's rmtree
# into whatever it points at). A linked .history is REPLACED by a real folder
# on the next write: only the link is removed (unlinkat; its target is never
# touched), and history goes on recording. Refusing instead would stop the
# history silently and for good, because a snapshot never fails the write it
# records and has no one to tell. Reads (the log, a revision, the trial mark)
# read nothing through a link: until the next write replaces it, a linked
# .history is no history.
_HDIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_HREAD_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _history_fd(workspace: Path, *, create: bool) -> int | None:
    """A descriptor on the workspace's own .history folder, reached without
    following a link (the caller owns it), or None when there is none. With
    `create`, a missing one is made and a LINK in its place is replaced by a
    real folder. A regular file named .history is not ours to remove: OSError."""
    import stat as _st
    from server.applications.tree_walk import TreeEscape, open_dir_no_follow
    try:
        wfd = open_dir_no_follow(workspace)
    except TreeEscape:
        if create:
            raise
        return None
    try:
        try:
            return os.open(_HISTORY_DIR, _HDIR_FLAGS, dir_fd=wfd)
        except FileNotFoundError:
            if not create:
                return None
        except OSError:
            if not create:
                return None
            if not _st.S_ISLNK(os.stat(_HISTORY_DIR, dir_fd=wfd, follow_symlinks=False).st_mode):
                raise
            os.unlink(_HISTORY_DIR, dir_fd=wfd)
            log.warning("artifact %s: .history was a link; replaced it with a real folder "
                        "(the link's target was not touched)", workspace.name)
        try:
            os.mkdir(_HISTORY_DIR, 0o755, dir_fd=wfd)
        except FileExistsError:
            pass
        return os.open(_HISTORY_DIR, _HDIR_FLAGS, dir_fd=wfd)
    finally:
        os.close(wfd)


def _read_at(dfd: int, name: str) -> bytes | None:
    """The regular file `name` in the folder `dfd`, never through a link."""
    try:
        fd = os.open(name, _HREAD_FLAGS, dir_fd=dfd)
    except OSError:
        return None
    try:
        import stat as _st
        if not _st.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parent_at(dfd: int, rel: str, *, create: bool) -> tuple[int, str]:
    """(a descriptor on the folder holding `rel` below `dfd`, the leaf name),
    making the folders when `create`, following no link on the way (a link
    where a folder should be is an OSError). The caller owns the descriptor."""
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise OSError(f"not a path inside the folder: {rel!r}")
    cur = os.dup(dfd)
    try:
        for p in parts[:-1]:
            if create:
                try:
                    os.mkdir(p, 0o755, dir_fd=cur)
                except FileExistsError:
                    pass
            nfd = os.open(p, _HDIR_FLAGS, dir_fd=cur)
            os.close(cur)
            cur = nfd
    except BaseException:
        os.close(cur)
        raise
    return cur, parts[-1]


def _write_at(dfd: int, rel: str, data: bytes, *, append: bool = False) -> None:
    """Write `data` to `rel` below the folder `dfd`, making its folders, and
    following no link on the way (a link where a folder or the file should
    be is an OSError, never a write elsewhere)."""
    cur, leaf = _parent_at(dfd, rel, create=True)
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
                 | (os.O_APPEND if append else os.O_TRUNC))
        fd = os.open(leaf, flags, 0o644, dir_fd=cur)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    finally:
        os.close(cur)


def _copy_fd_at(src_fd: int, dfd: int, rel: str) -> str:
    """Stream the open file `src_fd` into a NEW file `rel` below `dfd`
    (folders made, no link followed, an existing name refused) and return
    the sha256 of the bytes written. Never holds the file in memory."""
    import hashlib
    cur, leaf = _parent_at(dfd, rel, create=True)
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o644, dir_fd=cur)
        try:
            h = hashlib.sha256()
            while chunk := os.read(src_fd, 1 << 20):
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            return h.hexdigest()
        finally:
            os.close(fd)
    finally:
        os.close(cur)


def _link_at(dfd: int, src_rel: str, dst_rel: str) -> bool:
    """Hard-link the regular file `src_rel` to the new name `dst_rel`, both
    below `dfd`, following no link on either path. False when it cannot
    (missing, not a regular file, the link count is full): the caller copies."""
    import stat as _st
    try:
        sfd, sleaf = _parent_at(dfd, src_rel, create=False)
    except OSError:
        return False
    try:
        if not _st.S_ISREG(os.stat(sleaf, dir_fd=sfd, follow_symlinks=False).st_mode):
            return False
        dfd2, dleaf = _parent_at(dfd, dst_rel, create=True)
        try:
            os.link(sleaf, dleaf, src_dir_fd=sfd, dst_dir_fd=dfd2, follow_symlinks=False)
            return True
        finally:
            os.close(dfd2)
    except OSError:
        return False
    finally:
        os.close(sfd)


def write_history_file(workspace: Path, name: str, data: bytes) -> None:
    """Write one file of the server's own into the artifact's .history
    (a design's trial mark), never through a link. Raises OSError."""
    hfd = _history_fd(workspace, create=True)
    try:
        _write_at(hfd, name, data)
    finally:
        os.close(hfd)


def read_history_file(workspace: Path, name: str) -> bytes | None:
    """One file of the artifact's .history, read through no link, or None."""
    hfd = _history_fd(workspace, create=False)
    if hfd is None:
        return None
    try:
        return _read_at(hfd, name)
    finally:
        os.close(hfd)


def remove_history_file(workspace: Path, name: str) -> None:
    """Remove one file from the artifact's .history (never through a link)."""
    hfd = _history_fd(workspace, create=False)
    if hfd is None:
        return
    try:
        os.unlink(name, dir_fd=hfd)
    except OSError:
        pass
    finally:
        os.close(hfd)


def _workspace_files(tree):
    """All snapshot-able files of an open workspace tree: everything except
    .history. A link is never followed (server/applications/tree_walk.py): a
    snapshot is a copy that the revision render serves and a revert writes
    back as a real file, so a followed link would turn its target's bytes
    into workspace content, and from there into a published site."""
    return tree.walk(skip=lambda parts, is_dir: _HISTORY_DIR in parts)


def _parse_log(raw: bytes | None) -> list[dict]:
    entries = []
    for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(e, dict):
            entries.append(e)
    return entries


def _history_log(workspace: Path) -> list[dict]:
    """The revision log, read through no link (a linked .history has none)."""
    return _parse_log(read_history_file(workspace, "log.jsonl"))


def _read_sums(hfd: int, rev: str) -> dict:
    """{rel: sha256} of every file revision `rev` holds, or {} (a revision
    written before sums existed, or none): its files are then copied."""
    try:
        sfd, leaf = _parent_at(hfd, f"{_SUMS_DIR}/{rev}.json", create=False)
    except OSError:
        return {}
    try:
        raw = _read_at(sfd, leaf)
    finally:
        os.close(sfd)
    try:
        sums = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return {}
    return sums if isinstance(sums, dict) else {}


def _disk_room(workspace: Path) -> tuple[int, int]:
    """(bytes free to this process, bytes in the volume) where `workspace` lives."""
    st = os.statvfs(workspace)
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def _rev_inodes(hfd: int, rev: str) -> list:
    """(inode key, link count, bytes on disk) of every regular file in
    revision `rev`, walked without following a link. A file that another
    revision also holds (a hard link, st_nlink > 1) is freed only when every
    revision holding it goes."""
    import stat as _st
    out = []
    try:
        top = os.open(rev, _HDIR_FLAGS, dir_fd=hfd)
    except OSError:
        return out
    stack = [top]
    try:
        while stack:
            dfd = stack.pop()
            try:
                for name in os.listdir(dfd):
                    try:
                        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    except OSError:
                        continue
                    if _st.S_ISDIR(st.st_mode):
                        try:
                            stack.append(os.open(name, _HDIR_FLAGS, dir_fd=dfd))
                        except OSError:
                            pass
                    elif _st.S_ISREG(st.st_mode):
                        out.append(((st.st_dev, st.st_ino), st.st_nlink,
                                    st.st_blocks * 512))
            finally:
                os.close(dfd)
    finally:
        for fd in stack:
            os.close(fd)
    return out


def _plan_prune(hfd: int, entries: list, deficit: int, *, keep: str) -> tuple[list, int]:
    """The SHORTEST run of oldest revisions whose removal frees at least
    `deficit` bytes, and what it frees — or ([], what removing every
    candidate would free) when no run frees enough. Only bytes a revision
    holds alone count: a file linked from a newer revision is freed only once
    every revision holding it is in the run."""
    seen: dict = {}
    nlink: dict = {}
    size: dict = {}
    freed = 0
    run: list = []
    for e in entries:
        rev = str(e.get("rev") or "")
        if rev == keep or not _REV_RE.match(rev):
            continue
        run.append(rev)
        for key, links, blocks in _rev_inodes(hfd, rev):
            nlink[key] = links
            size[key] = blocks
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == links:
                freed += blocks
        if freed >= deficit:
            return run, freed
    return [], freed


def _remove_revs(hfd: int, revs: list) -> None:
    """Remove revision folders and their sums. Only a revision-shaped name,
    and only inside .history: the log is a file in the workspace, and a name
    in it like "../x" must not become an rmtree somewhere else."""
    import shutil
    for rev in revs:
        if not _REV_RE.match(rev):
            continue
        shutil.rmtree(rev, dir_fd=hfd, ignore_errors=True)
        try:
            sfd, leaf = _parent_at(hfd, f"{_SUMS_DIR}/{rev}.json", create=False)
            try:
                os.unlink(leaf, dir_fd=sfd)
            finally:
                os.close(sfd)
        except OSError:
            pass


def _prune_for_space(hfd: int, workspace: Path, entries: list, need: int,
                     *, keep: str) -> tuple[list, str, bool]:
    """Make room for a revision that adds `need` bytes. Returns
    (revisions removed oldest first, a plain note or "", whether to write it).

    Nothing is removed unless the new bytes really exceed what the volume has
    free above its reserve (_DISK_RESERVE_FRACTION), and then only the
    shortest run of OLDEST revisions whose own bytes (st_nlink == 1 once the
    run is gone) cover the shortfall. When no run of revisions could cover it
    — the volume is full of OTHER data — none is removed: wiping history that
    frees nothing is never done. The revision is still
    written when the disk physically holds it, and the note says the volume
    is past its reserve; when it does not, it is not written and the note
    says so. Never `keep` (the revision the next one links to)."""
    free, total = _disk_room(workspace)
    reserve = int(total * _DISK_RESERVE_FRACTION)
    deficit = need - (free - reserve)
    if deficit <= 0:
        return [], "", True
    run, freed = _plan_prune(hfd, entries, deficit, keep=keep)
    if run:
        _remove_revs(hfd, run)
        return run, (f"the volume could not hold this revision and keep "
                     f"{int(_DISK_RESERVE_FRACTION * 100)}% free, so the oldest "
                     f"{len(run)} revision(s) were removed ({freed} bytes they held "
                     f"alone)"), True
    if need <= free:
        return [], (f"the volume is past the {int(_DISK_RESERVE_FRACTION * 100)}% it "
                    f"keeps free ({free} bytes left), mostly with other data: no "
                    f"revision was removed, since removing every older one would free "
                    f"only {freed} bytes"), True
    return [], (f"this change was NOT recorded in the history: it needs {need} bytes, "
                f"the disk has {free} free, and removing every older revision would "
                f"free only {freed}"), False


def _snapshot(workspace: Path, *, actor: str, op: str, note: str = "") -> str | None:
    """Append one revision. Returns the rev id, or None when skipped
    (identical to the newest revision). Best-effort by design: history must
    never break the write it documents — failures log and return None."""
    try:
        import hashlib
        from datetime import datetime, timezone

        from server.applications.tree_walk import open_tree_no_escape

        # Pass 1: hash every file, streamed (never the whole tree in memory).
        # The digest is the one earlier revisions recorded (rel, NUL, bytes per
        # file in walk order), so an unchanged workspace still skips.
        digest = hashlib.sha256()
        files = []   # (rel, source, size, sha256)
        with open_tree_no_escape(workspace) as tree:
            for f in _workspace_files(tree):
                fd = tree.open(f.source)
                if fd is None:
                    continue
                try:
                    one, size = hashlib.sha256(), 0
                    digest.update(f.rel.encode()); digest.update(b"\0")
                    while chunk := os.read(fd, 1 << 20):
                        digest.update(chunk); one.update(chunk); size += len(chunk)
                except OSError:
                    continue
                finally:
                    os.close(fd)
                files.append((f.rel, f.source, size, one.hexdigest()))
        digest = digest.hexdigest()

        # Every write below is through `hfd`, the real .history (_history_fd).
        hfd = _history_fd(workspace, create=True)
        try:
            import shutil
            entries = _parse_log(_read_at(hfd, "log.jsonl"))
            if entries and entries[-1].get("digest") == digest:
                return None  # no content change — nothing to record

            rev = f"{(int(entries[-1]['rev']) + 1) if entries else 1:04d}"
            prev = str(entries[-1].get("rev") or "") if entries else ""
            prev_sums = _read_sums(hfd, prev) if _REV_RE.match(prev) else {}
            # A folder under this name that the log does not hold is what a
            # snapshot that never finished left behind: never write into it
            # (one of its files may be a link to an older revision's copy).
            shutil.rmtree(rev, dir_fd=hfd, ignore_errors=True)

            new_bytes = sum(size for rel, _src, size, sha in files
                            if prev_sums.get(rel) != sha)
            pruned, disk_note, write_it = _prune_for_space(
                hfd, workspace, entries, new_bytes, keep=prev)
            if pruned:
                # Only revision-shaped names were removed (_prune_for_space);
                # the log loses exactly those lines, before anything else can
                # fail, so it never lists a revision that is gone.
                gone = set(pruned)
                _write_at(hfd, "log.jsonl", "".join(
                    json.dumps(e) + "\n" for e in entries
                    if str(e.get("rev") or "") not in gone).encode())
                log.warning("artifact %s: the disk could not hold the next revision; "
                            "removed the oldest %d revision(s) %s to make room",
                            workspace.name, len(pruned), ", ".join(pruned))
            if disk_note:
                _HISTORY_NOTES[str(workspace)] = disk_note
            if not write_it:
                log.warning("artifact %s: %s", workspace.name, disk_note)
                return None

            # Pass 2: an unchanged file is a hard link to the previous
            # revision's copy; anything else is streamed in. What is stored
            # is what the sums record (a file that changed between the passes
            # is recorded as copied).
            sums: dict = {}
            stored = 0
            with open_tree_no_escape(workspace) as tree:
                for rel, source, size, sha in files:
                    if prev_sums.get(rel) == sha and _link_at(hfd, f"{prev}/{rel}", f"{rev}/{rel}"):
                        sums[rel] = sha
                        continue
                    fd = tree.open(source)
                    if fd is None:
                        continue
                    try:
                        sums[rel] = _copy_fd_at(fd, hfd, f"{rev}/{rel}")
                        stored += size
                    finally:
                        os.close(fd)
            if not sums:
                try:
                    os.mkdir(rev, 0o755, dir_fd=hfd)
                except FileExistsError:
                    pass
            _write_at(hfd, f"{_SUMS_DIR}/{rev}.json", json.dumps(sums).encode())

            line = {
                "rev": rev,
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": actor, "op": op, "note": (note or "")[:200],
                "files": len(sums), "bytes": sum(s for _r, _s2, s, _h in files),
                "stored_bytes": stored, "digest": digest,
                # every file of the workspace is in this revision, whatever
                # its size (a restore removes only what it did not hold)
                "complete": True,
            }
            if pruned:
                line["pruned_for_space"] = pruned
            if disk_note:
                line["disk_note"] = disk_note
            _write_at(hfd, "log.jsonl", (json.dumps(line) + "\n").encode(), append=True)
            return rev
        finally:
            os.close(hfd)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("artifact history snapshot failed for %s: %s", workspace, e)
        return None


async def list_history(pool, entity_id: int, artifact_id: int, *,
                       limit: Optional[int] = None, offset: int = 0) -> dict:
    """The artifact's change history, newest first (rev, ts, actor, op, note).
    Every revision is kept, so the list pages: `limit` (None = all of it) from
    `offset`, with the true `total` and `next_offset` (None at the end)."""
    await _require_artifact(pool, entity_id, artifact_id)
    workspace = _projects_root() / str(artifact_id)
    entries = _history_log(workspace)
    entries.reverse()
    total = len(entries)
    offset = max(0, int(offset or 0))
    end = total if limit is None else min(total, offset + max(1, int(limit)))
    page = entries[offset:end]
    return {"artifact_id": artifact_id, "total": total, "offset": offset,
            "next_offset": end if end < total else None,
            "history": [
                {k: e.get(k) for k in ("rev", "ts", "actor", "op", "note", "files",
                                       "bytes", "pruned_for_space", "disk_note")}
                for e in page]}


async def render_history(pool, entity_id: int, artifact_id: int, rev: str,
                         entry: str = _DEFAULT_ENTRY) -> str:
    """Render one PAST revision — same self-contained document lane as the
    live render, jailed to the revision's snapshot dir."""
    await _require_artifact(pool, entity_id, artifact_id)
    if not _REV_RE.match(rev or ""):
        raise ArtifactError(400, "invalid revision id")
    workspace = _projects_root() / str(artifact_id)
    rev_dir = _history_root(workspace) / rev
    # The revision folder must be reached without a link (a linked .history
    # has no revisions to show), as revert reads it.
    from server.applications.tree_walk import TreeEscape, open_tree_no_escape
    try:
        open_tree_no_escape(workspace, within=f"{_HISTORY_DIR}/{rev}").close()
    except TreeEscape:
        raise ArtifactError(404, f"revision {rev} not found") from None
    return await inline_own_images(pool, entity_id, render_workspace(rev_dir, entry))


def _clear_workspace(workspace: Path, *, keep: set) -> None:
    """Remove everything in the workspace except .history and the files in
    `keep` (root-relative). A link is removed as a link, never followed or
    emptied."""
    import shutil
    if not keep:
        for entry_path in list(workspace.iterdir()):
            if entry_path.name == _HISTORY_DIR:
                continue
            # A linked folder is unlinked, never emptied: rmtree refuses a link
            # (and ignore_errors hid that), which left it in place for the
            # writes below to go through.
            if entry_path.is_dir() and not entry_path.is_symlink():
                shutil.rmtree(entry_path, ignore_errors=True)
            else:
                entry_path.unlink(missing_ok=True)
        return
    for root, dirs, names in os.walk(workspace, topdown=False, followlinks=False):
        rel_root = Path(root).relative_to(workspace)
        if rel_root.parts and rel_root.parts[0] == _HISTORY_DIR:
            continue
        for name in names:
            rel = (rel_root / name).as_posix()
            if rel not in keep:
                (Path(root) / name).unlink(missing_ok=True)
        for name in dirs:
            if not rel_root.parts and name == _HISTORY_DIR:
                continue
            p = Path(root) / name
            if p.is_symlink():
                p.unlink(missing_ok=True)
            else:
                try:
                    p.rmdir()       # only an emptied folder goes
                except OSError:
                    pass


async def revert_artifact(pool, entity_id: int, artifact_id: int, rev: str,
                          *, actor: str = "user",
                          desktop_wait_s: Optional[float] = None) -> dict:
    """Restore the workspace to a past revision, exactly (files not present in
    the revision are removed). The restore itself becomes the newest revision,
    so a revert is always revertable; updated_at stamps so the open window
    live-refreshes."""
    await _require_artifact(pool, entity_id, artifact_id)
    if not _REV_RE.match(rev or ""):
        raise ArtifactError(400, "invalid revision id")
    workspace = _projects_root() / str(artifact_id)
    rev_dir = _history_root(workspace) / rev
    if not rev_dir.is_dir():
        raise ArtifactError(404, f"revision {rev} not found")

    from server.applications.tree_walk import TreeEscape, open_tree_no_escape
    # An older revision may leave out files over 1 MiB (its log
    # line has no "complete" mark). Restoring it must not delete what it never
    # recorded: such files stay as they are now, and the answer names them.
    line = next((e for e in _history_log(workspace) if str(e.get("rev") or "") == rev), {})
    complete = bool(line.get("complete"))
    # The revision's own files only: a link in the snapshot is not followed,
    # and a revision folder that is itself a link is not a revision. Each file
    # is streamed back (never the whole revision in memory); the revision
    # folder sits inside .history, which the clear below never touches.
    try:
        rtree = open_tree_no_escape(workspace, within=f"{_HISTORY_DIR}/{rev}")
    except TreeEscape:
        raise ArtifactError(404, f"revision {rev} not found") from None
    with rtree:
        restore = list(rtree.walk())
        kept: list = []
        if not complete:
            held = {f.rel for f in restore}
            with open_tree_no_escape(workspace) as wtree:
                kept = sorted(f.rel for f in _workspace_files(wtree)
                              if f.size > _LEGACY_FILE_MAX and f.rel not in held)
        _clear_workspace(workspace, keep=set(kept))
        for f in restore:
            fd = rtree.open(f.source)
            if fd is None:
                continue
            try:
                dest = workspace / f.rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as out_f:
                    while chunk := os.read(fd, 1 << 20):
                        out_f.write(chunk)
            finally:
                os.close(fd)

    _snapshot(workspace, actor=actor, op="revert", note=f"restored revision {rev}")
    await pool.execute(
        "UPDATE projects SET updated_at=now() WHERE id=$1 AND entity_id=$2",
        artifact_id, entity_id)
    out = {"artifact_id": artifact_id, "restored": rev}
    if (hnote := _take_history_note(workspace)):
        out["history"] = hnote
    if kept:
        out["kept"] = kept
        out["note"] = (f"revision {rev} was recorded before revisions held every file, "
                       f"and it left out {len(kept)} file(s) over 1 MiB; they were kept "
                       f"as they are now, not deleted: {', '.join(kept)}")
    desk = await _desktop_after_write(pool, entity_id, artifact_id, desktop_wait_s)
    if desk is not None:
        out["desktop"] = desk
    return out


# ─────────────────────────────────────────────────────────────────────────────
# RENDER (the live build — in-pod, self-contained)
# ─────────────────────────────────────────────────────────────────────────────
def render_workspace(workspace: Path, entry: str = _DEFAULT_ENTRY, *,
                     exact: bool = False) -> str:
    """Return a self-contained, renderable HTML document for `workspace`.

    The named `entry` is checked by entry_pages.entry_param: a name that
    could leave the workspace (a NUL or other control character, an
    absolute path, `..`) is refused with 400 and nothing is read. It used to
    be joined onto the workspace unchecked, so ?entry=../../<file> on
    /api/artifacts/{id}/render (and on the revision render) returned any
    file the kernel could read. Any other name renders the real file it
    names, as before. One that names no real file here (missing, a symlink)
    falls back to WHICH page entry_pages.choose_entry finds — the one rule
    every preview lane uses: root index.html, else every index.html at the
    shallowest depth outside .git and the revision store, never a symlink.
    Several equal pages and no root is answered with 300 naming them, never
    a silent pick (it was "the shallowest", broken by filesystem order). As
    before, a revision snapshot (itself inside the revision store) has no
    fallback: a name it lacks is 404. With `exact`, neither does any
    workspace: the render check asks about ONE named document, and a fallback
    page would be checked (and pinned) under a name it is not.

    The page's LOCAL <link>/<script> assets are inlined (resolved relative to
    the entry dir, never escaping the workspace) so it renders standalone in
    a sandboxed iframe. The page and every asset are read by
    entry_pages.read_in_tree, which never follows a symlink, including one
    swapped in after the page was chosen.
    """
    from server.applications.entry_pages import (
        EntryParamError, choice_detail, choose_entry, entry_param, read_in_tree)
    try:
        rel = entry_param(workspace, entry or _DEFAULT_ENTRY)
    except EntryParamError as e:
        if e.status != 404:
            raise ArtifactError(e.status, e.detail) from None
        if exact:
            raise ArtifactError(404, f"'{entry}' is not in the artifact") from None
        internal = (".git", _HISTORY_DIR)
        if any(part in internal for part in workspace.parts):
            # A revision snapshot: the fallback never had a page here.
            raise ArtifactError(404, "no renderable entry (index.html) in the artifact") from None
        choice = choose_entry(workspace, skip_dir=lambda d: d in internal)
        if not choice.entries:
            raise ArtifactError(404, "no renderable entry (index.html) in the artifact") from None
        if choice.default is None:
            raise ArtifactError(300, choice_detail(choice, "this artifact") + ": "
                                + ", ".join(choice.entries) + " — name one with entry=",
                                entries=list(choice.entries), basis=choice.basis) from None
        rel = choice.default
    html = read_in_tree(workspace, rel)
    if html is None:
        raise ArtifactError(404, "artifact entry unreadable")
    page_dir = PurePosixPath(rel).parent

    def _inline(ref: str):
        # Beside or below the page only: read_in_tree refuses absolute paths,
        # `..` and every symlink.
        return read_in_tree(workspace, (page_dir / ref).as_posix())

    def _css(m):
        c = _inline(m.group(1))
        return f"<style>{c}</style>" if c is not None else m.group(0)

    def _js(m):
        c = _inline(m.group(1))
        if c is None:
            return m.group(0)
        open_m = re.match(r"<script[^>]*>", m.group(0), re.I)
        open_tag = open_m.group(0) if open_m else "<script>"
        open_tag = re.sub(r'\s+src=["\'][^"\']*["\']', "", open_tag, flags=re.I)
        return f"{open_tag}{c}</script>"

    html = re.sub(r'<link[^>]*href=["\']([^"\']+\.css)["\'][^>]*>', _css, html, flags=re.I)
    html = re.sub(r'<script[^>]*src=["\']([^"\']+\.js)["\'][^>]*>\s*</script>', _js, html, flags=re.I)
    return html


async def render(pool, entity_id: int, artifact_id: int, entry: str = _DEFAULT_ENTRY,
                 *, exact: bool = False) -> str:
    """Entity-scoped render of one artifact (the host's own only). `exact`:
    the named document or 404, never a fallback (render_workspace)."""
    await _require_artifact(pool, entity_id, artifact_id)
    html = render_workspace(_projects_root() / str(artifact_id), entry, exact=exact)
    return await inline_own_images(pool, entity_id, html)


# An artifact (a desktop design included) may use the Host's own images by
# name: /api/images/<id>, the rows of generated_images that image_generate
# made or an app such as Blender Engine rendered.
# The frame an artifact renders in has no network, so each such name is
# replaced by the image itself as a data: URL when the artifact is rendered;
# the stored files keep the short name, and every render reads the current
# image. A name that is not one of this Host's images is left as written.
_OWN_IMAGE_REF = re.compile(r"/api/images/(gimg_[A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])")


def workspace_texts(artifact_id: int, paths) -> list[str]:
    """The text of each of `paths` inside the artifact's workspace, for a check
    that reads what a server-side copy put there (import_files: content no
    model typed in the call, so the call's own arguments do not carry it).
    UTF-8 files only; a binary file, a missing one, or a path that leaves the
    workspace reads as nothing."""
    ws = (_projects_root() / str(int(artifact_id))).resolve()
    out: list[str] = []
    for rel in paths or []:
        try:
            p = (ws / str(rel or "")).resolve()
            if not (ws in p.parents) or not p.is_file():
                continue
            out.append(p.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return out


async def missing_own_images(pool, entity_id: int, texts) -> list[str]:
    """The /api/images/<id> names in `texts` that are not this Host's images:
    they would render as broken images, so the artifact tool says so when it
    writes them, in the turn that can fix them."""
    ids = sorted({i for t in texts for i in _OWN_IMAGE_REF.findall(str(t or ""))})
    if not ids:
        return []
    have = {r["id"] for r in await pool.fetch(
        "SELECT id FROM generated_images WHERE entity_id = $1 AND id = ANY($2::text[])",
        int(entity_id), ids)}
    return [f"/api/images/{i}" for i in ids if i not in have]


async def inline_own_images(pool, entity_id: int, html: str) -> str:
    """`html` with each of this Host's /api/images/<id> names replaced by
    that image as a data: URL."""
    ids = sorted(set(_OWN_IMAGE_REF.findall(html or "")))
    if not ids:
        return html
    import base64
    rows = await pool.fetch(
        "SELECT id, content_type, bytes FROM generated_images "
        "WHERE entity_id = $1 AND id = ANY($2::text[])", int(entity_id), ids)
    found = {r["id"]: f"data:{r['content_type']};base64,"
                      + base64.b64encode(bytes(r["bytes"])).decode("ascii") for r in rows}
    if not found:
        return html
    return _OWN_IMAGE_REF.sub(lambda m: found.get(m.group(1), m.group(0)), html)


# ─────────────────────────────────────────────────────────────────────────────
# LIST / DELETE
# ─────────────────────────────────────────────────────────────────────────────
def _entry_is_desktop(artifact_id: int) -> bool:
    """Whether the artifact's index.html declares itself a desktop design —
    the static-head rule set_desktop applies (desktop_contract.detect_mode),
    read from the first 64 KB only. Lets the Artifacts window sort desktops
    from everything else without rendering each one."""
    from server.applications.desktop_contract import MAX_HEAD_BYTES, detect_mode
    from server.applications.entry_pages import read_in_tree
    # read_in_tree never follows a symlink: a linked index.html would
    # otherwise answer this yes/no about a file outside the artifact.
    head = read_in_tree(_projects_root() / str(int(artifact_id)), _DEFAULT_ENTRY,
                        limit=MAX_HEAD_BYTES)
    if head is None:
        return False
    return detect_mode(head) is not None


_LIST_PAGE_MAX = 200   # one page; every row is reachable through offset


async def list_artifacts(pool, entity_id: int, limit: int = 50, offset: int = 0) -> dict:
    """One page of the host's artifacts, most recently changed first, with
    the true `total` and `next_offset` (None on the last page). A page larger
    than _LIST_PAGE_MAX is served as _LIST_PAGE_MAX and says so."""
    asked = int(limit or 50)
    limit = max(1, min(asked, _LIST_PAGE_MAX))
    offset = max(0, int(offset or 0))
    total = await pool.fetchval(
        "SELECT count(*) FROM projects WHERE entity_id=$1 AND kind='artifact'",
        entity_id)
    rows = await pool.fetch(
        "SELECT id, name, description, status, disposable, created_at, updated_at "
        "FROM projects WHERE entity_id=$1 AND kind='artifact' "
        "ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT $2 OFFSET $3",
        entity_id, limit, offset)
    total = int(total or 0)
    end = offset + len(rows)
    out = {"total": total, "offset": offset, "limit": limit,
           "next_offset": end if (rows and end < total) else None}
    if asked > limit and out["next_offset"] is not None:
        out["note"] = (f"a page is at most {_LIST_PAGE_MAX} artifacts; "
                       f"pass offset={end} for the next")
    out["artifacts"] = [
        {"artifact_id": r["id"], "name": r["name"], "description": r["description"],
         "status": r["status"], "disposable": r["disposable"],
         "desktop": _entry_is_desktop(r["id"]),
         "created_at": r["created_at"].isoformat() if r["created_at"] else None,
         # The Artifacts window's live-refresh signal: it polls the list and
         # re-fetches the render when this moves.
         "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
         "preview_url": f"/api/artifacts/{r['id']}/render"}
        for r in rows]
    return out


async def list_entry_files(pool, entity_id: int, artifact_id: int) -> dict:
    """Root-level .html files of the artifact's workspace — the variant set the
    Artifacts window renders as tabs. index.html sorts first."""
    await _require_artifact(pool, entity_id, artifact_id)
    workspace = _projects_root() / str(artifact_id)
    files = sorted(
        (p.name for p in workspace.glob("*.html") if p.is_file()),
        key=lambda n: (n != "index.html", n))
    return {"artifact_id": artifact_id, "files": list(files)}


# Bridge edit rate cap: per-artifact sliding minute window (contract:
# enforcement is invisible plumbing — excess is refused, never dialogued).
_BRIDGE_RATE: dict[int, list[float]] = {}
_BRIDGE_RATE_PER_MIN = 20
# The tune edit's text is bounded by the ENTRY FILE it edits, not a fixed
# 2,048: `before` must occur exactly once in
# the file, so it can never be longer than the file; `after` may be up to the
# file's own length (one edit at most doubles the page). Pages smaller than
# _BRIDGE_TEXT_FLOOR still take an edit of that size. The page is untrusted,
# so a bound stays — sized by the document, never below a real paragraph.
_BRIDGE_TEXT_FLOOR = 2048


async def apply_bridge_edit(pool, entity_id: int, artifact_id: int, *,
                            entry: str, before: str, after: str) -> dict:
    """Apply ONE tune-mode text edit.

    Server-side re-validation — the viewer's checks are not trusted:
      * plain text only: markup ([<>]) in either field is refused
      * before must be non-empty; after is at most the entry file's length
      * `before` must occur EXACTLY ONCE in the entry file (0 = stale/forged,
        >1 = ambiguous — refused rather than guessed)
      * rate-capped per artifact
    The write goes through write_files, so updated_at stamps and the open
    window live-refreshes."""
    import time as _time
    await _require_artifact(pool, entity_id, artifact_id)
    if not isinstance(before, str) or not isinstance(after, str) or not before.strip():
        raise ArtifactError(400, "edit requires non-empty `before` and `after` strings")
    if any(c in before for c in "<>") or any(c in after for c in "<>"):
        raise ArtifactError(400, "edit is plain text only — markup is refused")
    stamps = _BRIDGE_RATE.setdefault(artifact_id, [])
    now = _time.monotonic()
    stamps[:] = [t for t in stamps if now - t < 60.0]
    if len(stamps) >= _BRIDGE_RATE_PER_MIN:
        raise ArtifactError(429, "bridge edit rate exceeded — try again shortly")
    entry = (entry or _DEFAULT_ENTRY).strip() or _DEFAULT_ENTRY
    _reject_unsafe_path(entry)
    workspace = _projects_root() / str(artifact_id)
    target = workspace / entry
    if not target.is_file():
        raise ArtifactError(404, f"entry {entry!r} not found in the artifact")
    content = target.read_text(errors="replace")
    room = max(_BRIDGE_TEXT_FLOOR, len(content))
    if len(after) > room:
        raise ArtifactError(400, f"edit text is {len(after)} chars; one tune edit may "
                                 f"be at most the page's own length ({room} chars)")
    n = content.count(before)
    if n == 0:
        raise ArtifactError(409, "`before` text not found — the artifact changed under the edit")
    if n > 1:
        raise ArtifactError(409, "`before` text is ambiguous (occurs more than once) — refused")
    stamps.append(now)
    res = await write_files(pool, entity_id, artifact_id,
                            [{"path": entry, "content": content.replace(before, after, 1)}],
                            actor="user",
                            note=f"tune edit: {before[:60]!r} → {after[:60]!r}")
    out = {"artifact_id": artifact_id, "entry": entry, "applied": True}
    if res.get("desktop") is not None:
        out["desktop"] = res["desktop"]
    return out


async def delete_artifact(pool, entity_id: int, artifact_id: int) -> dict:
    """Dispose of an artifact — drop its row + wipe its workspace. Only an
    entity's OWN kind='artifact' row (a converted artifact is now kind='project'
    backing an app, so it's protected from this disposal path)."""
    await _require_artifact(pool, entity_id, artifact_id)
    import shutil
    workspace = _projects_root() / str(artifact_id)
    shutil.rmtree(workspace, ignore_errors=True)
    await pool.execute(
        "DELETE FROM projects WHERE id=$1 AND entity_id=$2 AND kind='artifact'",
        artifact_id, entity_id)
    from server.applications import desktop_pin
    desktop_pin.forget(artifact_id)       # its worn-desktop pins go with it
    return {"artifact_id": artifact_id, "deleted": True}


# ─────────────────────────────────────────────────────────────────────────────
# CONVERT artifact → app (App SDK handoff)
# ─────────────────────────────────────────────────────────────────────────────
async def convert_to_app(pool, entity_id: int, artifact_id: int) -> dict:
    """Convert one of the host's OWN artifacts into an installed app via the App
    SDK. The artifact's workspace must contain a provenance=custom app.yaml (the
    host writes it with set_project_manifest, which validates it). This:

      1. requires the row is the host's own kind='artifact'
      2. requires app.yaml on disk (the App-SDK boundary the artifact was NOT
         bound to until now)
      3. promotes the row: kind='project', status='complete', disposable=false
         (it now backs a real app, so it is no longer a disposable artifact)
      4. hands the SAME workspace to ship_project_service — the App SDK's single
         pack → validate → SDK-bind → entity-sign → install path

    On any failure AFTER the promotion, the row is reverted to kind='artifact'
    so a failed convert leaves a still-usable artifact, not a half-promoted
    project. Returns ship_project_service's result + {converted_from_artifact}.
    """
    await _require_artifact(pool, entity_id, artifact_id)
    workspace = _projects_root() / str(artifact_id)
    manifest = workspace / "app.yaml"
    if not manifest.is_file():
        raise ArtifactError(
            400,
            "artifact has no app.yaml — write a provenance=custom manifest first "
            "(set_project_manifest) before converting to an app",
        )

    # Promote: distinct artifact → project (the App SDK only ever sees projects).
    await pool.execute(
        "UPDATE projects SET kind='project', status='complete', disposable=false, "
        "updated_at=now() WHERE id=$1 AND entity_id=$2",
        artifact_id, entity_id)
    try:
        from server.routes.applications import ship_project_service, ShipError
        try:
            res = await ship_project_service(pool, artifact_id)
        except ShipError as e:
            raise ArtifactError(e.status, f"convert failed at ship: {e.detail}") from None
    except ArtifactError:
        # Revert the promotion so a failed convert doesn't strand the row.
        await pool.execute(
            "UPDATE projects SET kind='artifact', status='active', disposable=true, "
            "updated_at=now() WHERE id=$1 AND entity_id=$2",
            artifact_id, entity_id)
        raise
    except Exception as e:
        await pool.execute(
            "UPDATE projects SET kind='artifact', status='active', disposable=true, "
            "updated_at=now() WHERE id=$1 AND entity_id=$2",
            artifact_id, entity_id)
        raise ArtifactError(500, f"convert failed: {e}") from None

    res = dict(res or {})
    res["converted_from_artifact"] = artifact_id
    return res


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _reject_unsafe_path(rel: str) -> None:
    if not rel or os.path.isabs(rel) or ".." in Path(rel).parts:
        raise ArtifactError(400, f"unsafe path: {rel!r}")
    # The revision store is never a source or a destination: a
    # write/promote/import INTO .history would clobber the log or plant a
    # file that survives every revert, breaking "revert restores exactly";
    # a promote FROM it would leak a snapshot as the live entry.
    if _HISTORY_DIR in Path(rel).parts:
        raise ArtifactError(400, f"{_HISTORY_DIR}/ is the revision store — not a file path: {rel!r}")


def _write_files(workspace: Path, files: list[dict]) -> int:
    """Write {path, content} entries into the workspace, jailed to it. Returns
    the count written. Raises ArtifactError on any unsafe path."""
    written = 0
    ws = workspace.resolve()
    for f in files or []:
        if not isinstance(f, dict):
            raise ArtifactError(400, "each file must be {path, content}")
        rel = (f.get("path") or "").strip()
        _reject_unsafe_path(rel)
        dest = (workspace / rel).resolve()
        if not (dest == ws or ws in dest.parents):
            raise ArtifactError(400, f"path escapes the artifact workspace: {rel!r}")
        content = f.get("content")
        if content is None:
            raise ArtifactError(400, f"file {rel!r} has no content")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(str(content))
        written += 1
    return written


async def _require_artifact(pool, entity_id: int, artifact_id: int):
    """Confirm the row exists, belongs to the entity, AND is kind='artifact'.
    A normal project or an already-converted (kind='project') row is NOT an
    artifact and is refused here — keeping the primitives distinct."""
    try:
        artifact_id = int(artifact_id)
    except (TypeError, ValueError):
        raise ArtifactError(400, "artifact_id must be an integer") from None
    row = await pool.fetchrow(
        "SELECT id, kind FROM projects WHERE id=$1 AND entity_id=$2",
        artifact_id, entity_id)
    if row is None:
        raise ArtifactError(404, f"artifact {artifact_id} not found")
    if row["kind"] != "artifact":
        raise ArtifactError(
            409,
            f"project {artifact_id} is not an artifact (kind={row['kind']}) — "
            "the term is distinct from a project/app",
        )
    return row
