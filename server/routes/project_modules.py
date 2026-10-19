"""Project modules and their versions.

A project holds
several MODULES, each a directory of the project's one tree
(<PROJECTS_ROOT>/<project>/<module>/, its own app.yaml at its root — one tree
with a directory per module). Each module knows what it
ships as (`target`), chosen at alignment, which shapes the build
(contract_for_target). Its `validation` is optional and recorded only; it
decides nothing (a build either landed or failed).

A landed run becomes a VERSION of the module it was built into or adopted
into. A version is a snapshot of the module directory at that moment plus the
save-point commit when the project repo made one; the module's current version
is a POINTER. Undo moves the pointer back and restores the directory from the
earlier snapshot — no version ROW is ever deleted, and the pointer can move
forward again. Snapshots are kept for the current version and the newest
KEEP_SNAPSHOTS versions; an older version keeps its row and commit (the
history) but can no longer be restored from disk.

Snapshots live OUTSIDE the project tree (VERSIONS_ROOT), so they are never
committed, never seeded into a build's worktree and never landed. The commit
is the history record (and works only when gitea is connected); the snapshot
is what restore and preview read, so both work without it.

A project with no module rows keeps the single-app root layout it always had.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECTS_ROOT = Path("/var/lib/cseries/projects")
VERSIONS_ROOT = Path("/var/lib/cseries/module-versions")

MODULE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TARGETS = ("private_app", "public_site", "store_app", "component", "op", "live_context")
VALIDATIONS = ("checks", "proof", "review")
# What the platform can install, start and poll (app_verify).
INSTALLABLE_TARGETS = frozenset({"private_app", "store_app"})

# Directory names a module may not take: they collide with the machinery the
# project tree already carries. A module named like a review dir (a harness id,
# a <harness>-sN variant, diffs/) would be excluded from seeding, landing and
# the repo by _is_review_dir / project_git._write_exclude — silently.
_RESERVED = frozenset({"git", "adopt-backups", "node-modules", "diffs"})


def _reserved(name: str) -> bool:
    if name in _RESERVED:
        return True
    try:
        from server.routes.review import _is_review_dir
        return _is_review_dir(name)
    except Exception:  # pragma: no cover — review always ships beside this
        return False

_TARGET_WORDS = {
    "private_app": "a private app (installed on the user's own Host)",
    "public_site": "a public site (static, published to the web)",
    "store_app": "a store app (published to the App Store)",
    "component": "a component of another module (ships as nothing itself)",
    "op": "an op (a program the Host runs on the user's machines: dry run, approval, apply)",
}


class ModuleError(ValueError):
    """A module request that cannot be honoured; the message says why."""


def validate_module_fields(name: str, target: str, validation: str) -> None:
    """Raise ModuleError naming the first thing wrong, or return None."""
    if not isinstance(name, str) or not MODULE_NAME_RE.fullmatch(name):
        raise ModuleError(
            f"module name {name!r} must be a directory name: lowercase letters, digits "
            "and '-', starting with a letter or digit, at most 40 characters")
    if _reserved(name):
        raise ModuleError(
            f"module name {name!r} is taken by the build machinery (engine names, "
            "their -sN runs, diffs, git) — pick another, e.g. 'app', 'site', 'api'")
    if target not in TARGETS:
        raise ModuleError(f"module {name!r}: target must be one of {', '.join(TARGETS)}")
    # `validation` is optional and decides nothing (a build either
    # landed or failed). A value that is given must still be a known one.
    if validation and validation not in VALIDATIONS:
        raise ModuleError(
            f"module {name!r}: validation must be one of {', '.join(VALIDATIONS)}")


def module_dir(project_id: int, name: str) -> Path:
    """The module's directory in the project tree. Validates the name, and
    refuses a module directory that is a link: the tree is user-writable
    through git (a fast-forward can land a symlink), and restore, snapshot,
    adopt, preview and build all act on this path — through a link they would
    act on its target."""
    if not MODULE_NAME_RE.fullmatch(name or "") or _reserved(name):
        raise ModuleError(f"not a module name: {name!r}")
    p = PROJECTS_ROOT / str(int(project_id)) / name
    if p.is_symlink():
        raise ModuleError(f"module {name!r}: its directory is a link — refused")
    return p


def version_dir(project_id: int, name: str, number: int) -> Path:
    if not MODULE_NAME_RE.fullmatch(name or ""):
        raise ModuleError(f"not a module name: {name!r}")
    return VERSIONS_ROOT / str(int(project_id)) / name / f"v{int(number)}"


def target_words(target: str) -> str:
    return _TARGET_WORDS.get(target, target)


def _row(r) -> dict:
    return {k: r[k] for k in r.keys()} if r is not None else None


# ── modules ─────────────────────────────────────────────────────────────────

async def upsert_module(pool, project_id: int, *, name: str, target: str,
                        validation: str, description: str = "",
                        adopt_dir: bool = False) -> dict:
    """Create the module, or update its target/validation/description. Its
    directory is created in the project tree. Returns the module row.

    A NEW module whose directory already holds files is refused unless
    `adopt_dir`: that folder belongs to the project's root app (its `api/`,
    its `site/`), and silently making it a module would hand it to builds,
    versions and restores that were never asked for."""
    validate_module_fields(name, target, validation)
    # The column is NOT NULL (v168); nothing reads it to decide an outcome.
    validation = validation or "review"
    if not adopt_dir and await get_module(pool, project_id, name) is None:
        d = module_dir(project_id, name)
        if d.is_dir() and any(c.name not in _NOT_CONTENT for c in d.iterdir()):
            raise ModuleError(
                f"the project already has a folder {name!r} with files in it (part of "
                f"the root app) — name the module something else, or pass "
                f"adopt_dir=true to make that folder the module")
    row = await pool.fetchrow(
        """INSERT INTO project_modules (project_id, name, target, validation, description)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (project_id, name) DO UPDATE
              SET target = EXCLUDED.target, validation = EXCLUDED.validation,
                  description = EXCLUDED.description, updated_at = now()
           RETURNING id, project_id, name, target, validation, description,
                     current_version_id, created_at, updated_at""",
        int(project_id), name, target, validation, (description or "")[:2000])
    try:
        await asyncio.to_thread(module_dir(project_id, name).mkdir, parents=True, exist_ok=True)
    except OSError as e:  # the row is the record; the dir is made again on first build
        logger.warning("module dir for %s/%s not created: %s", project_id, name, e)
    return _row(row)


async def get_module(pool, project_id: int, name: str) -> Optional[dict]:
    return _row(await pool.fetchrow(
        "SELECT id, project_id, name, target, validation, description, current_version_id, "
        "created_at, updated_at FROM project_modules WHERE project_id=$1 AND name=$2",
        int(project_id), name))


async def get_module_by_id(pool, module_id: int) -> Optional[dict]:
    return _row(await pool.fetchrow(
        "SELECT id, project_id, name, target, validation, description, current_version_id, "
        "created_at, updated_at FROM project_modules WHERE id=$1", int(module_id)))


async def list_modules(pool, project_id: int) -> list[dict]:
    """Every module of the project with its CURRENT version and version count —
    the shape list_projects and the Workbench read, so the host talks about
    "version 3 of the reader", never run ids."""
    rows = await pool.fetch(
        """SELECT m.id, m.name, m.target, m.validation, m.description,
                  m.current_version_id, m.updated_at,
                  v.number AS current_number, v.run_id AS current_run_id,
                  v.commit_sha AS current_commit, v.source AS current_source,
                  v.created_at AS current_at,
                  (SELECT COUNT(*) FROM module_versions mv WHERE mv.module_id = m.id)
                      AS version_count
             FROM project_modules m
             LEFT JOIN module_versions v ON v.id = m.current_version_id
            WHERE m.project_id = $1
            ORDER BY m.name""", int(project_id))
    out = []
    for r in rows:
        cur = None
        if r["current_version_id"] is not None:
            cur = {"number": r["current_number"], "run_id": r["current_run_id"],
                   "commit": (r["current_commit"] or "")[:7] or None,
                   "source": r["current_source"],
                   "at": r["current_at"].isoformat() if r["current_at"] else None}
        out.append({"name": r["name"], "target": r["target"],
                    "validation": r["validation"], "description": r["description"],
                    "current_version": cur,
                    "version_count": int(r["version_count"] or 0)})
    return out


async def modules_by_project(pool, project_ids: list[int]) -> dict[int, list[dict]]:
    """list_modules for many projects in ONE statement (list_projects lists up
    to 50): {project_id: [module, …]}, projects without modules absent."""
    ids = [int(i) for i in project_ids]
    if not ids:
        return {}
    rows = await pool.fetch(
        """SELECT m.project_id, m.name, m.target, m.validation, m.description,
                  m.current_version_id,
                  v.number AS current_number, v.run_id AS current_run_id,
                  v.commit_sha AS current_commit, v.source AS current_source,
                  v.created_at AS current_at,
                  (SELECT COUNT(*) FROM module_versions mv WHERE mv.module_id = m.id)
                      AS version_count
             FROM project_modules m
             LEFT JOIN module_versions v ON v.id = m.current_version_id
            WHERE m.project_id = ANY($1::int[])
            ORDER BY m.project_id, m.name""", ids)
    out: dict[int, list[dict]] = {}
    for r in rows:
        cur = None
        if r["current_version_id"] is not None:
            cur = {"number": r["current_number"], "run_id": r["current_run_id"],
                   "commit": (r["current_commit"] or "")[:7] or None,
                   "source": r["current_source"],
                   "at": r["current_at"].isoformat() if r["current_at"] else None}
        out.setdefault(int(r["project_id"]), []).append(
            {"name": r["name"], "target": r["target"], "validation": r["validation"],
             "description": r["description"], "current_version": cur,
             "version_count": int(r["version_count"] or 0)})
    return out


async def list_versions(pool, project_id: int, name: str) -> list[dict]:
    mod = await get_module(pool, project_id, name)
    if mod is None:
        raise ModuleError(f"project {project_id} has no module {name!r}")
    rows = await pool.fetch(
        "SELECT id, number, run_id, commit_sha, source, note, created_at, undone_at "
        "FROM module_versions WHERE module_id=$1 ORDER BY number DESC", mod["id"])
    return [{"number": r["number"], "run_id": r["run_id"],
             "commit": (r["commit_sha"] or "")[:7] or None, "source": r["source"],
             "note": r["note"], "current": r["id"] == mod["current_version_id"],
             "restorable": version_dir(project_id, name, r["number"]).is_dir(),
             "undone": r["undone_at"] is not None,
             "at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in rows]


# ── versions ────────────────────────────────────────────────────────────────

# Machinery that can live in a module directory and is not the module's
# content: adopt's rollback store and provenance (an adopt INTO a module keeps
# them there) and git. Never snapshotted, never removed by a restore.
_NOT_CONTENT = frozenset({".git", ".adopt-backups", ".adopt-provenance.json"})
# Derived, regenerable trees — the directories adopt skips too
# (review._ADOPT_SKIP). A version is the module's SOURCE: a back end with its
# dependencies installed would otherwise copy node_modules/.venv into every
# version. A restore leaves them where they are rather than backing them up.
_DERIVED = frozenset({"node_modules", "__pycache__", ".venv", "venv", ".cache",
                      ".pytest_cache"})
_SKIP = _NOT_CONTENT | _DERIVED

# How many snapshots a module keeps (plus its current version). 10 is the
# rollback depth the standard deploy tools settle on: Kubernetes keeps 10 old
# ReplicaSets (Deployment revisionHistoryLimit default) and Helm keeps 10
# releases (--history-max default). Rows and commits are never pruned.
KEEP_SNAPSHOTS = 10


def _copy_content(src: Path, dst: Path) -> int:
    """Copy src's content into dst (which exists). Links and adopt's
    bookkeeping are not copied (the land and adopt rules). Returns the file
    count."""
    n = 0
    if not src.is_dir():
        return 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP and not (Path(root) / d).is_symlink()]
        for fn in files:
            p = Path(root) / fn
            if p.is_symlink() or fn in _NOT_CONTENT:
                continue
            rel = p.relative_to(src)
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)
            n += 1
    return n


def _snapshot_blocking(src: Path, dst: Path) -> int:
    """Copy the module directory to its (fresh) version snapshot."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    return _copy_content(src, dst)


def _restore_blocking(snap: Path, dst: Path, backup: Path) -> int:
    """Make the module directory exactly the snapshot. What is in it now —
    including edits and files no version holds — is MOVED to `backup` first,
    never deleted; adopt's bookkeeping stays where it is. Only ever called on
    a module directory (module_dir validated it)."""
    if not snap.is_dir():
        raise ModuleError(f"version snapshot missing: {snap.name}")
    dst.mkdir(parents=True, exist_ok=True)
    moved = [c for c in dst.iterdir() if c.name not in _SKIP]
    if moved:
        backup.mkdir(parents=True, exist_ok=True)
        for child in moved:
            os.replace(child, backup / child.name)
    return _copy_content(snap, dst)


def _restore_backup_dir(project_id: int, name: str, from_number) -> Path:
    """Where a restore puts what it displaces: the project's adopt backup
    store, which every seed, land and git path already treats as machinery.
    It carries no manifest.json, so adopt_undo never selects it."""
    base = f"version-restore-{name}-from-v{from_number or 0}-{int(time.time())}"
    root = PROJECTS_ROOT / str(int(project_id)) / ".adopt-backups"
    # The store is user-writable through git (a TRACKED symlink is not covered
    # by info/exclude): moving the user's files "into the backup" through a
    # link would move them wherever it points.
    if root.is_symlink():
        raise ModuleError(".adopt-backups in this project is a link — restore refused")
    out, n = root / base, 1
    while out.exists():
        n += 1
        out = root / f"{base}.{n}"
    return out


async def record_version(pool, project_id: int, name: str, *, source: str,
                         run_id: Optional[int] = None, commit_sha: Optional[str] = None,
                         note: str = "") -> dict:
    """The module's directory as it is NOW becomes its next version, and the
    pointer moves to it. Called after a build or adopt has landed files into
    the module and the save point was attempted. Returns the version."""
    mod = await get_module(pool, project_id, name)
    if mod is None:
        raise ModuleError(f"project {project_id} has no module {name!r}")
    src = module_dir(project_id, name)
    # THE COPY RUNS OUTSIDE THE LOCK. A snapshot of a large module took the
    # module row lock (and an open transaction) for as long as the copy ran.
    # It is taken into a staging dir first; under the lock the directory's
    # fingerprint is compared with the one the copy started from, and only a
    # directory that CHANGED meanwhile (a restore or another landing got in)
    # is copied again, under the lock. The number is still assigned under it.
    staging = VERSIONS_ROOT / str(int(project_id)) / name / f".staging-{os.getpid()}-{time.time_ns()}"
    fp = await asyncio.to_thread(_fingerprint, src)
    files = await asyncio.to_thread(_snapshot_blocking, src, staging)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize version numbering per module.
                await conn.execute("SELECT 1 FROM project_modules WHERE id=$1 FOR UPDATE", mod["id"])
                src = module_dir(project_id, name)      # re-checked under the lock
                if await asyncio.to_thread(_fingerprint, src) != fp:
                    files = await asyncio.to_thread(_snapshot_blocking, src, staging)
                number = int(await conn.fetchval(
                    "SELECT COALESCE(MAX(number), 0) + 1 FROM module_versions WHERE module_id=$1",
                    mod["id"]))
                dst = version_dir(project_id, name, number)
                if dst.exists():
                    await asyncio.to_thread(shutil.rmtree, dst)
                os.replace(staging, dst)
                vid = await conn.fetchval(
                    """INSERT INTO module_versions (module_id, number, run_id, commit_sha, source, note)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    mod["id"], number, run_id, commit_sha, source, (note or "")[:500])
                await conn.execute(
                    "UPDATE project_modules SET current_version_id=$1, updated_at=now() WHERE id=$2",
                    vid, mod["id"])
                # Still under the lock: a restore cannot be copying a snapshot
                # this removes.
                pruned = await _prune_locked(conn, project_id, name, mod["id"])
    finally:
        if staging.exists():
            await asyncio.to_thread(shutil.rmtree, staging, True)
    return {"module": name, "number": number, "run_id": run_id,
            "commit": (commit_sha or "")[:7] or None, "source": source, "files": files,
            "pruned_snapshots": pruned}


def _fingerprint(src: Path) -> tuple:
    """What a snapshot would read: (path, size, mtime) of every content file,
    walked with the same skips. Cheap (stat only) next to the copy."""
    out = []
    if not src.is_dir():
        return ()
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP and not (Path(root) / d).is_symlink())
        for fn in sorted(files):
            p = Path(root) / fn
            if p.is_symlink() or fn in _NOT_CONTENT:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((str(p.relative_to(src)), st.st_size, st.st_mtime_ns))
    return tuple(out)


async def prune_snapshots(pool, project_id: int, name: str) -> list[int]:
    """Delete the snapshots of versions older than the newest KEEP_SNAPSHOTS,
    never the current version's. The rows stay (number, run, commit: the
    history); such a version can no longer be restored from disk. Returns the
    version numbers whose snapshot was removed.

    Under the module row lock — the one record_version and set_current_version
    take — so a prune never deletes the snapshot a restore is copying."""
    mod = await get_module(pool, project_id, name)
    if mod is None:
        return []
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM project_modules WHERE id=$1 FOR UPDATE", mod["id"])
            return await _prune_locked(conn, project_id, name, mod["id"])


async def _prune_locked(conn, project_id: int, name: str, module_id: int) -> list[int]:
    """The prune itself; the caller holds the module row lock."""
    cur_id = await conn.fetchval(
        "SELECT current_version_id FROM project_modules WHERE id=$1", module_id)
    rows = await conn.fetch(
        "SELECT id, number FROM module_versions WHERE module_id=$1 ORDER BY number DESC",
        module_id)
    keep = {r["number"] for r in rows[:KEEP_SNAPSHOTS]}
    keep |= {r["number"] for r in rows if r["id"] == cur_id}
    out = []
    for r in rows:
        if r["number"] in keep:
            continue
        d = version_dir(project_id, name, r["number"])
        if d.is_dir():
            await asyncio.to_thread(shutil.rmtree, d, True)
            out.append(int(r["number"]))
    return out


def base_dir(project_id: int, name: str) -> Path:
    """The module as it stood BEFORE its versions — kept when an undo takes
    the module back past its first version (an adopt's own backup restored
    it), so a later version can be undone back to it too."""
    return version_dir(project_id, name, 0)


async def _move_pointer(pool, project_id: int, name: str, number: Optional[int], *,
                        undo: bool) -> dict:
    """Make the module directory version `number`'s snapshot (None: the
    pre-version base) and point at it. With `undo`, the version the pointer
    moves OFF is marked undone, so no later undo lands on it again; the
    version it moves ONTO is never undone afterwards. What the directory held
    is kept in a backup (named in the result)."""
    mod = await get_module(pool, project_id, name)
    if mod is None:
        raise ModuleError(f"project {project_id} has no module {name!r}")
    module_dir(project_id, name)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The same per-module lock record_version takes: a restore and a
            # new version never interleave on one directory.
            cur = await conn.fetchrow(
                "SELECT m.current_version_id, v.number FROM project_modules m "
                "LEFT JOIN module_versions v ON v.id = m.current_version_id "
                "WHERE m.id=$1 FOR UPDATE OF m", mod["id"])
            row = None
            if number is not None:
                row = await conn.fetchrow(
                    "SELECT id, number, commit_sha FROM module_versions "
                    "WHERE module_id=$1 AND number=$2", mod["id"], int(number))
                if row is None:
                    raise ModuleError(f"module {name!r} has no version {number}")
                snap = version_dir(project_id, name, number)
                if not snap.is_dir():
                    raise ModuleError(
                        f"module {name!r} version {number}: its snapshot was pruned (only the "
                        f"newest {KEEP_SNAPSHOTS} and the current are kept) — its commit is "
                        f"{(row['commit_sha'] or '')[:7] or 'not recorded'}")
            else:
                snap = base_dir(project_id, name)
                if not snap.is_dir():
                    raise ModuleError(f"module {name!r} has no state before its first version")
            # Checked AGAIN under the lock: waiting for it can take as long as
            # another snapshot, and a fast-forward in that window can turn the
            # module directory into a link.
            mdir = module_dir(project_id, name)
            backup = _restore_backup_dir(project_id, name, cur["number"] if cur else None)
            label = f"version {number}" if number is not None else "its state before version 1"
            try:
                files = await asyncio.to_thread(_restore_blocking, snap, mdir, backup)
            except ModuleError:
                raise
            except OSError as e:
                # Part-way: some of the directory may already be in the backup.
                # Say where, so nothing looks lost.
                where = (f".adopt-backups/{backup.name}" if backup.exists()
                         else "nowhere — nothing had been moved yet")
                raise ModuleError(
                    f"restoring {name!r} to {label} failed part-way ({e}); what "
                    f"the directory held is in {where}; the pointer did not move") from None
            await conn.execute(
                "UPDATE project_modules SET current_version_id=$1, updated_at=now() WHERE id=$2",
                row["id"] if row else None, mod["id"])
            if row is not None:
                await conn.execute("UPDATE module_versions SET undone_at=NULL WHERE id=$1", row["id"])
            if undo and cur and cur["current_version_id"] is not None:
                await conn.execute("UPDATE module_versions SET undone_at=now() WHERE id=$1",
                                   cur["current_version_id"])
    return {"module": name, "number": row["number"] if row else None, "files": files,
            "commit": ((row["commit_sha"] or "")[:7] or None) if row else None,
            "backup": (f".adopt-backups/{backup.name}" if backup.exists() else None)}


async def set_current_version(pool, project_id: int, name: str, number: int) -> dict:
    """Move the pointer to version `number` and make the module directory that
    version's snapshot. No version is created or deleted; what the directory
    held is kept in a backup (named in the result)."""
    return await _move_pointer(pool, project_id, name, int(number), undo=False)


async def clear_current_version(pool, project_id: int, name: str) -> None:
    """After an adopt's own backup has put the module back as it was before
    its FIRST version: the pointer goes to no version, that version is marked
    undone, and the directory as it now stands is kept as the module's base,
    so a later version can be undone back to it. Rows stay."""
    mod = await get_module(pool, project_id, name)
    if mod is None:
        raise ModuleError(f"project {project_id} has no module {name!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchval(
                "SELECT current_version_id FROM project_modules WHERE id=$1 FOR UPDATE", mod["id"])
            await asyncio.to_thread(_snapshot_blocking, module_dir(project_id, name),
                                    base_dir(project_id, name))
            await conn.execute(
                "UPDATE project_modules SET current_version_id=NULL, updated_at=now() WHERE id=$1",
                mod["id"])
            if cur is not None:
                await conn.execute("UPDATE module_versions SET undone_at=now() WHERE id=$1", cur)


async def previous_version(pool, module_id: int, number: int) -> Optional[int]:
    """The version an undo from `number` lands on: the latest below it that
    was not itself undone (an undone version is never reinstated by undo)."""
    return await pool.fetchval(
        "SELECT MAX(number) FROM module_versions WHERE module_id=$1 AND number < $2 "
        "AND undone_at IS NULL", module_id, number)


async def undo_version(pool, project_id: int, name: str) -> dict:
    """Undo the module's CURRENT version: the pointer goes to the latest
    earlier version that was not itself undone, or — when there is none but
    the module's pre-version state was kept (clear_current_version) — back to
    that state. The version it leaves is marked undone. Refused when there is
    nothing to go back to."""
    mod = await get_module(pool, project_id, name)
    if mod is None:
        raise ModuleError(f"project {project_id} has no module {name!r}")
    if mod["current_version_id"] is None:
        raise ModuleError(f"module {name!r} has no version yet — nothing to undo")
    cur = await pool.fetchval("SELECT number FROM module_versions WHERE id=$1",
                              mod["current_version_id"])
    prev = await previous_version(pool, mod["id"], cur)
    if prev is None and not base_dir(project_id, name).is_dir():
        raise ModuleError(f"module {name!r} is at its first version (v{cur}) — nothing earlier")
    out = await _move_pointer(pool, project_id, name,
                              int(prev) if prev is not None else None, undo=True)
    out["from"] = cur
    return out

# ── the Workbench's view ────────────────────────────────────────────────────

# How many recent runs of the project are looked at for version candidates,
# and how many candidates one module shows. A candidate is an alternative the
# user has not taken yet; the newest few are the ones anyone compares.
CANDIDATE_SCAN = 60
CANDIDATES_PER_MODULE = 10


def _read_manifest(sessions_root: Path, session_id: str) -> Optional[dict]:
    import json
    if not session_id or "/" in session_id or session_id.startswith("."):
        return None
    d = sessions_root / session_id
    try:
        m = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict):
        return None
    # picks.json: a pick recorded by an older session (review.py writes it no
    # more — the live signals are read in run_candidates).
    m["_picked"] = (d / "picks.json").exists()
    return m


async def run_candidates(pool, project_id: int, names: list[str], *,
                         sessions_root: Optional[Path] = None) -> dict[str, list[dict]]:
    """{module: [candidate, …]} — finished runs that were built FOR a module
    (their session manifest names it) whose session the user has not decided
    yet: a compare's variants before the user takes one. Newest first. Read
    from harness_run (the numbered runs log) and only the manifests of those
    runs' sessions.

    A session is DECIDED — none of its runs is a candidate any more — once the
    user took any run from it, by either door:
      - "use as version" / review.adopt(into=module) records the run's id on
        the version (module_versions.run_id), and so does a landed build;
      - review.propose_commit records its version WITHOUT a run id, but
        marks the picked harness in harness_usage.committed (compute
        .mark_committed), which is the signal read here.
    Taking one variant of a compare is the choice among all of them, so the
    other variants leave the list with it."""
    if not names:
        return {}
    if sessions_root is None:
        from server.routes.review import SESSIONS_ROOT as sessions_root
    rows = await pool.fetch(
        """SELECT id, session_id, harness, model, created_at FROM harness_run
            WHERE project_id = $1 AND ok AND status <> 'suppressed'
            ORDER BY id DESC LIMIT $2""", int(project_id), CANDIDATE_SCAN)
    sids = sorted({r["session_id"] for r in rows if r["session_id"]})
    decided: set[str] = set()
    if sids:
        decided |= {r["session_id"] for r in await pool.fetch(
            """SELECT DISTINCT hr.session_id FROM harness_run hr
                 JOIN module_versions v ON v.run_id = hr.id
                WHERE hr.session_id = ANY($1::text[])""", sids)}
        decided |= {r["session_id"] for r in await pool.fetch(
            """SELECT DISTINCT session_id FROM harness_usage
                WHERE session_id = ANY($1::text[]) AND committed""", sids)}
    wanted = set(names)
    manifests: dict[str, Optional[dict]] = {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        sid = r["session_id"] or ""
        if sid not in manifests:
            manifests[sid] = await asyncio.to_thread(_read_manifest, sessions_root, sid)
        m = manifests[sid]
        if not m or m.get("_picked") or sid in decided:
            continue
        mod = m.get("module")
        if mod not in wanted:
            continue
        lst = out.setdefault(mod, [])
        if len(lst) >= CANDIDATES_PER_MODULE:
            continue
        lst.append({"run_id": int(r["id"]), "session_id": sid, "harness": r["harness"],
                    "model": r["model"], "kind": m.get("kind") or "compare",
                    "at": r["created_at"].isoformat() if r["created_at"] else None})
    return out


async def modules_view(pool, project_id: int) -> dict:
    """What the Workbench's MODULES section renders: every module (list_modules)
    with its version candidates and — for a target the platform installs — the
    OBSERVED state of its app (server.applications.app_state, read from the
    module's own app.yaml), plus the same state for the project root, which is
    the app of a project with no modules. Installed/running is a badge on the
    module; the run card no longer carries it."""
    from server.applications.app_state import empty_state, project_app_state
    mods = await list_modules(pool, project_id)
    cands = await run_candidates(pool, project_id, [m["name"] for m in mods])
    for m in mods:
        m["candidates"] = cands.get(m["name"], [])
        m["app"] = None
        if m["target"] in INSTALLABLE_TARGETS:
            try:
                m["app"] = await project_app_state(pool, project_id,
                                                   module_dir(project_id, m["name"]))
            except Exception as e:  # noqa: BLE001 — a card renders without it
                logger.debug("module app state unavailable for %s/%s: %s",
                             project_id, m["name"], e)
                m["app"] = empty_state()
    try:
        root_app = await project_app_state(pool, project_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("root app state unavailable for %s: %s", project_id, e)
        root_app = empty_state()
    return {"modules": mods, "root_app": root_app}
