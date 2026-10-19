"""
indexer — walk the entity's named projects, chunk (tree-sitter), embed with the
Host's configured embedding model on its configured lane (bridge_embed), upsert
into the Host's own Postgres (store.code_chunks), each vector into the column of
that model's size. Incremental: only changed files re-index, and an unchanged
file whose chunks have no vector in the configured model's column (the model
was switched, or no embedder answered last time) gets one; vanished files are
dropped. Ported from the platform code index's indexer.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from . import bridge_embed, chunker, store

log = logging.getLogger("codesearch.indexer")

PROJECTS_ROOT = os.environ.get("CODESEARCH_PROJECTS_ROOT", "/var/lib/cseries/projects")
# Repos the host can read on its forge, cloned into the pod by an
# embed_jobs target='gitea_repo' job (server/routes/embed.py::_run_repo_job)
# after the USER said yes. A sibling of the projects dir
# (…/cseries/repos), laid out <owner>/<name>, and named in the index by that
# same "owner/name" — a repo's project name is its gitea full name and never
# collides with a numeric project id.
REPOS_ROOT = os.environ.get(
    "CODESEARCH_REPOS_ROOT",
    os.path.join(os.path.dirname(PROJECTS_ROOT.rstrip("/")), "repos"))
MAX_FILE_BYTES = int(os.environ.get("CODESEARCH_MAX_FILE_BYTES", str(1_000_000)))  # skip >1MB
# A source file the chunker parses by definition is indexed up to this size:
# the platform's own tool dispatcher (brain/mastercode/runtime/tools.py) is
# 1.14 MB and had no chunk at all under the 1 MB limit, so nothing defined in
# it could be found by search. Every chunk is held to MAX_CHUNK_CHARS whatever
# the file's size. Text, data and files without a parser keep MAX_FILE_BYTES,
# and so does a file over it that is not written as source (written_as_source).
MAX_SOURCE_FILE_BYTES = int(os.environ.get("CODESEARCH_MAX_SOURCE_FILE_BYTES", str(8_000_000)))
INDEX_BATCH = int(os.environ.get("CODESEARCH_INDEX_BATCH", "64"))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", "target", ".cache", "vendor", ".pytest_cache", ".mypy_cache"}
_EXTRA_TEXT = {".md", ".txt", ".rst"}


def _skip(parts: tuple, is_dir: bool) -> bool:
    return is_dir and (parts[-1] in SKIP_DIRS or parts[-1].startswith("."))


def _iter_files(tree):
    """The files of an open tree (server/applications/tree_walk.py) the index
    reads. A link is never followed: what the index holds is sent to the
    embedder and returned by the Host's code search, so a link to
    /proc/self/environ or a mounted secret would put its target there. A
    link to a file inside the project adds nothing — the file is indexed
    under its own path."""
    for f in tree.walk(skip=_skip):
        # index only files the chunker recognizes (has a language) or plain text;
        # skips binaries/assets without a language.
        if chunker.detect_language(f.rel) is None and Path(f.rel).suffix.lower() not in _EXTRA_TEXT:
            continue
        yield f


def max_bytes_for(rel: str) -> int:
    """The largest file at `rel` the index reads: MAX_SOURCE_FILE_BYTES for a
    language the chunker splits by definition, MAX_FILE_BYTES for anything
    else."""
    lang = chunker.detect_language(rel)
    if lang is not None and lang in chunker.CHUNKABLE_NODE_TYPES:
        return max(MAX_FILE_BYTES, MAX_SOURCE_FILE_BYTES)
    return MAX_FILE_BYTES


def written_as_source(content: str) -> bool:
    """Whether a file over MAX_FILE_BYTES is still indexed under
    MAX_SOURCE_FILE_BYTES: none of its lines is longer than a chunk
    (chunker.MAX_CHUNK_CHARS). A minified bundle is build output on a few
    lines of megabytes; this repo checks in two (system-apps/circles and
    system-apps/workbench assets, 2.8 and 3.4 MB, 3,845 chunks between them
    against tools.py's 589), and their names change with every build, so
    reading them would embed them again on each one. The longest line of
    tools.py is 242 characters."""
    return all(len(line) <= chunker.MAX_CHUNK_CHARS for line in content.split("\n"))


def indexes_path(rel: str, size: int) -> bool:
    """Whether index_project reads a regular file at `rel` (root-relative,
    posix) of `size` bytes: the same folders skipped, the same kinds of file
    and the same size limit as _iter_files and index_project. For sizing a
    tree before it is written (codesearch.repo_sync). It sees no content, so
    a source file over MAX_FILE_BYTES that index_project then skips as not
    written as source (written_as_source) is counted: the size errs high."""
    parts = tuple(p for p in str(rel).split("/") if p)
    if not parts or size > max_bytes_for(rel):
        return False
    if any(_skip(parts[:i + 1], True) for i in range(len(parts) - 1)):
        return False
    return (chunker.detect_language(rel) is not None
            or Path(rel).suffix.lower() in _EXTRA_TEXT)


def project_root_for(project: str) -> Path:
    """Where a named project's files live: a Workbench project dir under
    PROJECTS_ROOT, or — for an "owner/name" — a cloned gitea repo under
    REPOS_ROOT. One resolver for the indexer, the tools and the route.

    Raises ValueError for anything that is not a plain name or a plain
    owner/name: a "." or ".." segment (or an empty one) never resolves — the
    caller would otherwise be handed the projects root itself, or /var/lib,
    as "a project"."""
    project = (project or "").strip().strip("/")
    parts = project.split("/") if project else []
    if not parts or len(parts) > 2 or any((not p) or (not p.strip(".")) for p in parts):
        raise ValueError(f"not a project name: {project!r}")
    if len(parts) == 2:
        root = Path(REPOS_ROOT)
    else:
        root = Path(PROJECTS_ROOT)
    path = root / project
    # Resolve without following the leaf (it may not exist yet) and require
    # the result to stay under its root.
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"not a project name: {project!r}")
    return path


async def index_project(db, *, project: str, project_root: str | None = None) -> dict:
    """(Re)index one project incrementally. Returns a summary dict."""
    try:
        root = Path(project_root) if project_root else project_root_for(project)
    except ValueError as e:
        return {"project": project, "error": str(e)}
    from server.applications.tree_walk import (TreeEscape, open_tree_no_escape,
                                               universal_newlines)
    if not root.is_dir():
        return {"project": project, "error": f"not a directory: {root}"}
    try:
        tree = open_tree_no_escape(root)
    except TreeEscape:
        return {"project": project, "error": f"not a directory (a link?): {root}"}

    # The configured model and the column its size lives in, read once for
    # the pass: every vector this pass writes is that model's, in that column.
    # None when no model is set up (or the config cannot be read): the pass
    # then writes changed files for keyword search only and embeds nothing.
    space, no_space = await bridge_embed.active_space()
    stored = await store.get_file_state(db, project=project, space=space)
    on_disk: dict[str, str] = {}
    changed: list[tuple[str, list[chunker.Chunk]]] = []
    files_changed = 0
    to_embed: set[str] = set()     # unchanged files written without vectors

    with tree:
        for f in _iter_files(tree):
            rel = f.rel
            cap = max_bytes_for(rel)
            if f.size > cap:
                continue
            data = tree.read(f, limit=cap + 1)
            if data is None or len(data) > cap:
                continue
            # What Path.read_text("utf-8", "ignore") returned, so the stored
            # hashes of unchanged files still match.
            content = universal_newlines(data.decode("utf-8", "ignore"))
            if len(data) > MAX_FILE_BYTES and not written_as_source(content):
                continue    # a minified bundle keeps the 1 MB limit
            fhash = chunker.file_hash(content)
            on_disk[rel] = fhash
            old_hash, embedded = stored.get(rel, (None, False))
            if old_hash == fhash:
                if embedded or space is None:
                    continue  # unchanged (or nothing to embed it with)
                # Current content with no vector in the configured model's
                # column — written while no embedder answered, or before the
                # Host switched models: it gets its vectors now if the lane
                # answers (a refresh, not a change — files_changed does not
                # count it). Its other models' vectors stay (replace_file).
                to_embed.add(rel)
                changed.append((rel, chunker.chunk_file(rel, content)))
                continue
            # changed/new. Its old chunks stay searchable until the new ones
            # are embedded; replace_file below swaps them in one transaction.
            changed.append((rel, chunker.chunk_file(rel, content)))
            files_changed += 1

    # Delete chunks for files that vanished from disk.
    files_deleted = 0
    for rel in stored:
        if rel not in on_disk:
            files_deleted += await store.delete_file(db, project=project, file_path=rel)

    # A changed file with nothing left to chunk (emptied) has nothing to embed:
    # its old chunks just go.
    for rel, cs in changed:
        if not cs:
            await store.delete_file(db, project=project, file_path=rel)

    # Embed in waves (embed_many is bounded-concurrent calls on the lane), and
    # replace each file once ALL its chunks have vectors: old out, new in, one
    # transaction — a search mid-reindex sees the old version or the new one,
    # never neither and never half. A file with any chunk unembedded keeps its
    # old version (and its old hash, so the next pass tries it again).
    #
    # When the FIRST wave comes back with no vector at all, no embedder is
    # answering (the lane is down, or the Host has none): the pass stops
    # asking, and each changed file is written as it is now with no vectors
    # (v224), so the lexical arm finds it today and a later pass embeds it.
    # A file already written that way is left as it is. One wave is the most
    # a pass spends finding out.
    pending = [c for _, cs in changed for c in cs]   # each file's chunks contiguous
    written = 0
    failed = 0
    unembedded_files = 0
    cur: dict = {"file": None, "rows": [], "failed": False}

    async def _flush() -> None:
        nonlocal written
        if cur["file"] is not None and not cur["failed"]:
            written += await store.replace_file(db, project=project, file_path=cur["file"],
                                                chunks=cur["rows"], space=space)
        cur.update(file=None, rows=[], failed=False)

    def _row(c: chunker.Chunk) -> store.Chunk:
        return store.Chunk(
            project=project, file_path=c.file_path, chunk_index=c.chunk_index,
            content=c.content, language=c.language, chunk_kind=c.chunk_kind,
            symbol=c.symbol, start_line=c.start_line, end_line=c.end_line,
            file_hash=c.file_hash)

    lane_down = space is None and bool(pending)
    for i in range(0, len(pending) if space is not None else 0, INDEX_BATCH):
        wave = pending[i:i + INDEX_BATCH]
        vecs = await bridge_embed.embed_many([c.content for c in wave], space=space)
        if i == 0 and all(v is None for v in vecs):
            lane_down = True
            break
        for c, v in zip(wave, vecs):
            if c.file_path != cur["file"]:
                await _flush()
                cur["file"] = c.file_path
            if v is None:
                failed += 1
                cur["failed"] = True
                cur["rows"] = []
            elif not cur["failed"]:
                cur["rows"].append((_row(c), v))
    await _flush()

    if lane_down:
        failed = len(pending)
        for rel, cs in changed:
            if cs and rel not in to_embed:
                written += await store.replace_file(
                    db, project=project, file_path=rel,
                    chunks=[(_row(c), None) for c in cs], space=space, unembedded=True)
                unembedded_files += 1

    log.info("indexed project=%s files_changed=%d chunks=%d chunks_failed=%d files_deleted=%d"
             " files_without_vectors=%d", project, files_changed, written, failed, files_deleted,
             unembedded_files)
    out = {"project": project, "files_changed": files_changed,
           "chunks_written": written, "chunks_failed": failed, "files_deleted": files_deleted}
    if space is None:
        # No model set up (or its config unreadable): nothing here can have a
        # vector, whether or not a file changed — said on every pass, so a
        # repo is never recorded as searchable by meaning (repo_sync).
        out["embedder"] = f"{no_space}: files indexed for keyword search only"
        out["files_without_vectors"] = unembedded_files
        # Every changed file was written for keyword search and no vector
        # can exist until a model is set up: nothing is left for another
        # pass at this head (repo_sync records it; vectors_current says when
        # a model set up later has a column to fill).
        out["no_embedding_model"] = True
    elif lane_down:
        out["embedder"] = "not answering: changed files written for keyword search only"
        out["files_without_vectors"] = unembedded_files
    if space is not None:
        out["embedding_model"] = space.model
    return out


async def vectors_current(db, *, project: str) -> bool:
    """Whether the project has nothing left to embed with the Host's
    configured embedding model: every chunk has its vector in that model's
    column. False after a switch to a model whose column the project has not
    filled yet: a repo whose head has not moved is otherwise never indexed
    again (repo_sync), so its new column would never be filled.

    True with no model set up (or its config unreadable this pass): there is
    nothing to embed with, so a pass at an unchanged head would fetch and walk
    the repo to write nothing. Once a model is set up its column is empty here,
    this is False, and the next pass fills it."""
    space, _why = await bridge_embed.active_space()
    return space is None or await store.missing_vectors(db, project=project, space=space) == 0


async def chunked_by_this_version(db, *, project: str, project_root) -> bool:
    """Whether the project's index was chunked by this chunker
    (chunker.CHUNKER_VERSION): one indexed file, read from the tree, hashes
    to what its rows carry. A repo whose head has not moved is otherwise
    never indexed again (repo_sync), so without this a change to the chunker
    reached a repo only when it next moved. True when there is nothing to
    compare (no rows, the file gone or unreadable): the indexer's own pass
    settles those."""
    row = await db.fetchrow(
        "SELECT file_path, file_hash FROM code_chunks WHERE project = $1 "
        "ORDER BY file_path LIMIT 1", project)
    if row is None or not row["file_hash"]:
        return True
    from server.applications.tree_walk import (TreeEscape, open_tree_no_escape,
                                               universal_newlines)
    rel = row["file_path"]
    try:
        tree = open_tree_no_escape(Path(project_root))
    except (TreeEscape, OSError):
        return True
    with tree:
        data = tree.read(rel, limit=max_bytes_for(rel) + 1)
    if data is None:
        return True
    content = universal_newlines(data.decode("utf-8", "ignore"))
    return chunker.file_hash(content) == row["file_hash"]


async def retire_sqlite_index(db) -> dict:
    """One pass: index here every project the old SQLite index held, then
    delete that file (migrations/v200). The file is the only record of WHICH
    Workbench projects were indexed, so it goes only once each of them is
    fully indexed again; while any is not, the file stays and the pass says
    which. A project with nothing to index — its directory gone, a name that
    is not a project, a link — does not hold the file back.

    The embedding lane is asked once before any project is walked: at pod start
    the bridge is usually not connected yet, and a lane that is down would
    otherwise cost a full walk and a failed embed per chunk to find out."""
    projects = store.legacy_sqlite_projects()
    if projects is None:
        return {"retired": False, "reason": "no old index"}
    present: list[str] = []
    for project in projects:
        try:
            root = project_root_for(project)
        except ValueError:
            continue
        if root.is_dir():
            present.append(project)
    if present and await bridge_embed.embed("code search") is None:
        log.info("code search: embedding lane not answering; old SQLite index kept (%s)", present)
        return {"retired": False, "projects": projects, "pending": present,
                "reason": "embedding lane not answering"}
    pending: list[str] = []
    for project in present:
        try:
            res = await index_project(db, project=project)
        except Exception as e:  # noqa: BLE001 — the database or the lane failed mid-way
            log.warning("code search: re-indexing %s failed (%s)", project, e)
            pending.append(project)
            continue
        if res.get("error"):
            # index_project returns an error only for a tree it will never
            # index (not a directory, a link): nothing to wait for.
            log.info("code search: %s not re-indexed (%s)", project, res["error"])
            continue
        if res.get("chunks_failed"):
            pending.append(project)
    if pending:
        log.warning("code search: old SQLite index kept until these index again: %s", pending)
        return {"retired": False, "projects": projects, "pending": pending}
    store.remove_legacy_sqlite()
    log.info("code search: old SQLite index removed; re-indexed %s", present)
    return {"retired": True, "projects": projects}


RETIRE_RETRY_S = float(os.environ.get("CODESEARCH_RETIRE_RETRY_S", "60"))
RETIRE_RETRY_MAX_S = float(os.environ.get("CODESEARCH_RETIRE_RETRY_MAX_S", "3600"))


async def retire_sqlite_index_until_done(db, *, retry_s: float | None = None,
                                         retry_max_s: float | None = None) -> dict:
    """The start-up task: retire_sqlite_index, again after a wait (doubling,
    up to an hour) until the old file is gone. A lane that was down at start —
    the bridge still connecting, the owner's bastion off — is picked up when it
    comes back, not at the next pod start. Runs in the background; never raises."""
    delay = RETIRE_RETRY_S if retry_s is None else retry_s
    cap = RETIRE_RETRY_MAX_S if retry_max_s is None else retry_max_s
    while True:
        try:
            res = await retire_sqlite_index(db)
        except Exception as e:  # noqa: BLE001
            log.warning("code search: retiring the old SQLite index failed (%s)", e)
            res = {"retired": False, "error": str(e)}
        if res.get("retired") or res.get("reason") == "no old index":
            return res
        await asyncio.sleep(delay)
        delay = min(delay * 2, cap)


def list_projects() -> list[str]:
    """The entity's named project dirs under PROJECTS_ROOT, plus every cloned
    gitea repo under REPOS_ROOT as "owner/name"."""
    out: list[str] = []
    root = Path(PROJECTS_ROOT)
    if root.is_dir():
        out.extend(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
    out.extend(list_repos())
    return sorted(out)


def list_repos() -> list[str]:
    """Cloned gitea repos under REPOS_ROOT, as "owner/name" (only dirs that
    are real git checkouts — a half-finished clone has no .git yet)."""
    root = Path(REPOS_ROOT)
    if not root.is_dir():
        return []
    out: list[str] = []
    for owner in root.iterdir():
        if not owner.is_dir() or owner.name.startswith("."):
            continue
        for repo in owner.iterdir():
            if repo.is_dir() and not repo.name.startswith(".") and (repo / ".git").exists():
                out.append(f"{owner.name}/{repo.name}")
    return sorted(out)
