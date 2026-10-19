"""
Codespace routes — recursive workspace tree for 3D visualization.

Returns hierarchical file tree with size, language, and line estimates
for the CodeCity visualization in the Codespace system app.
"""

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Query, Depends
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.pc.codespace")

router = APIRouter(prefix="/api/codespace", tags=["codespace"])

WORKSPACE = Path(os.environ.get("CEIGAS_WORKSPACE", "/app/workspace"))
# Per-entity persistent projects (named projects + per-harness review/bake-off
# worktrees) live here as one directory per project/worktree.
PROJECTS_ROOT = Path("/var/lib/cseries/projects")
# The default number of files one /tree answer lists. The caller may ask for
# more (`max_files`, no ceiling) or open one directory at a time (`path`).
# The walk once stopped at this number whatever
# `max_files` said, and `truncated` was computed against `max_files`, so a
# 7,000-file repo asked for with max_files=10000 came back as 5,000 files and
# "truncated: false". Every answer now counts EVERY file (`total_files`) and
# says how many it lists (`listed_files`).
MAX_FILES = 5000
# One /tree answer lists at most this many files: a
# PAGE, not a ceiling — `offset` + `next_offset` reach every file, and
# `total_files` is the real count. An unbounded max_files let one call build
# a JSON tree of any size in the pod's memory.
_PAGE_MAX = 20_000
# How many directory entries one walk may visit while COUNTING. A real repo is
# far below it (the Linux kernel is ~80k files, Chromium ~400k without .git);
# it protects the Host's own worker thread from a pathological tree (a
# generated directory of tens of millions of entries). Past it the count stops
# and the answer says `total_is_floor: true` — "at least this many".
_VISIT_BUDGET = 1_000_000

# Extension → language mapping
_EXT_LANG = {
    ".py": "python", ".pyx": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".less": "css",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".sql": "sql",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".xml": "xml",
    ".md": "markdown", ".mdx": "markdown",
    ".txt": "text",
    ".dockerfile": "docker",
    ".lua": "lua",
    ".r": "r",
    ".vue": "vue",
    ".svelte": "svelte",
}

# Skip these directories
_SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".next", "dist", "build",
    ".tox", ".eggs", "*.egg-info",
}


def _lang_for(name: str) -> str:
    """Detect language from filename."""
    lower = name.lower()
    if lower == "dockerfile":
        return "docker"
    if lower == "makefile":
        return "shell"
    ext = Path(lower).suffix
    return _EXT_LANG.get(ext, "other")


def _sorted_entries(path: str) -> list:
    """A directory's entries, directories first then by name — never
    following a link to decide which is which."""
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except (PermissionError, OSError):
        return []

    def _key(e):
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        return (not is_dir, e.name.lower())

    entries.sort(key=_key)
    return entries


def _tree_page(root: Path, rel: str = "", *, offset: int = 0,
               limit: int = MAX_FILES, budget: int | None = None) -> tuple[dict, dict]:
    """(tree, stats) for `root`, walked once, iteratively, in the order the
    tree is drawn (a directory's subdirectories first, then its files).

    The walk used to follow a link to a directory
    (`a -> .` made it recurse until the path got too long; two such links made
    it exponential) and ran on the event loop. Now:
      * a symlink is LISTED as a link entry and never descended or stat-ed
        through, and a directory reached twice (a bind mount) is walked once;
      * files are counted in walk order; those with index in
        [offset, offset + limit) are listed, every one is counted;
      * the walk stops counting after `budget` directory entries and says so
        (`total_is_floor`).
    stats = {listed, seen, links, visited, total_is_floor}."""
    budget = _VISIT_BUDGET if budget is None else budget
    stats = {"listed": 0, "seen": 0, "links": 0, "visited": 0, "total_is_floor": False}
    root_node = {"name": root.name or "workspace", "path": rel or "/", "type": "directory",
                 "children": [], "size": 0, "file_count": 0}
    parent_of: dict = {}
    dir_nodes = [root_node]
    seen_dirs: set = set()
    try:
        st = os.stat(root, follow_symlinks=False)
        seen_dirs.add((st.st_dev, st.st_ino))
    except OSError:
        return root_node, stats
    stack = [(root_node, rel, iter(_sorted_entries(str(root))))]
    end = offset + limit
    while stack:
        node, node_rel, it = stack[-1]
        entry = next(it, None)
        if entry is None:
            stack.pop()
            continue
        stats["visited"] += 1
        if stats["visited"] > budget:
            stats["total_is_floor"] = True
            break
        name = entry.name
        child_rel = f"{node_rel}/{name}" if node_rel else name
        try:
            if entry.is_symlink():
                if name.startswith("."):
                    continue
                stats["links"] += 1
                if stats["links"] <= limit:
                    try:
                        target = os.readlink(entry.path)
                    except OSError:
                        target = None
                    node["children"].append({"name": name, "path": child_rel,
                                             "type": "link", "target": target})
                continue
            if entry.is_dir(follow_symlinks=False):
                if name.startswith(".") or name in _SKIP_DIRS:
                    continue
                dst = entry.stat(follow_symlinks=False)
                key = (dst.st_dev, dst.st_ino)
                if key in seen_dirs:
                    continue
                seen_dirs.add(key)
                child = {"name": name, "path": child_rel, "type": "directory",
                         "children": [], "size": 0, "file_count": 0}
                parent_of[id(child)] = node
                dir_nodes.append(child)
                node["children"].append(child)
                stack.append((child, child_rel, iter(_sorted_entries(entry.path))))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        index = stats["seen"]
        stats["seen"] += 1
        if not (offset <= index < end):
            continue  # counted, not listed
        try:
            fst = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        stats["listed"] += 1
        node["children"].append({"name": name, "path": child_rel, "type": "file",
                                 "size": fst.st_size, "language": _lang_for(name),
                                 "modified": fst.st_mtime})
        node["size"] += fst.st_size
        node["file_count"] += 1
    # Totals bubble up, deepest first (dir_nodes is in walk order, so a
    # child always comes after its parent). A directory with nothing listed
    # under it — no file, no link — is dropped, as before.
    for d in reversed(dir_nodes):
        d["children"] = [c for c in d["children"]
                         if c["type"] != "directory" or c.pop("_keep", False)]
        links = sum(1 for c in d["children"] if c["type"] == "link")
        if links:
            d["link_count"] = links
        d["_keep"] = d["file_count"] > 0 or any(
            c["type"] in ("link", "directory") for c in d["children"])
        parent = parent_of.get(id(d))
        if parent is not None:
            parent["size"] += d["size"]
            parent["file_count"] += d["file_count"]
    root_node.pop("_keep", None)
    return root_node, stats


def _walk_tree(root: Path, rel: str = "", depth: int = 0, counter: list = None,
               limit: int = MAX_FILES) -> dict:
    """The tree for `root` (see _tree_page). counter = [listed, seen] is
    filled in for callers that pass one."""
    tree, stats = _tree_page(root, rel, offset=0, limit=limit)
    if counter is not None:
        while len(counter) < 2:
            counter.append(0)
        counter[0], counter[1] = stats["listed"], stats["seen"]
    return tree


def _count_files(root: Path, limit: int = MAX_FILES) -> int:
    """Cheap bounded file count for a project dir — used to flag empty
    projects in the selector without building a full tree. It stops at
    `limit` on purpose: the selector only needs "empty or not", and /tree
    gives the true count."""
    n = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune noise dirs in place so os.walk doesn't descend into them.
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            n += len(filenames)
            if n >= limit:
                return limit
    except OSError:
        return n
    return n


def _resolve_project(project: str) -> Path | None:
    """Map a project name to its directory, realpath-guarded.

    The empty/missing name resolves to the default WORKSPACE. Any other
    value must resolve (after following symlinks) to a directory that is
    PROJECTS_ROOT itself or lives strictly beneath it. Returns None on any
    traversal/escape attempt or a non-existent dir.
    """
    if not project:
        ws = WORKSPACE.resolve()
        return ws if (ws.exists() and ws.is_dir()) else None

    root = PROJECTS_ROOT.resolve()
    target = (PROJECTS_ROOT / project).resolve()
    try:
        # relative_to raises if target is not under root; equality lets the
        # root itself through (it isn't "relative_to" a strict descendant).
        if target != root:
            target.relative_to(root)
    except ValueError:
        return None
    if not target.exists() or not target.is_dir():
        return None
    return target


@router.get("/projects", dependencies=[Depends(requires_permission("read:workspace_files"))])
async def list_projects():
    """List selectable projects for the CodeCity selector.

    Returns the default workspace plus every directory directly under
    /var/lib/cseries/projects (named projects AND per-harness review/bake-off
    worktrees). Each entry carries a bounded file_count so the frontend can
    default to the first non-empty project instead of the empty workspace.
    """
    return await asyncio.to_thread(_list_projects_sync)


def _list_projects_sync() -> dict:
    projects = []

    # Default workspace first — always selectable even when empty.
    ws = WORKSPACE.resolve()
    projects.append({
        "name": "",
        "label": "workspace",
        "is_default": True,
        "file_count": _count_files(ws) if (ws.exists() and ws.is_dir()) else 0,
    })
    projects[0]["file_count_is_floor"] = projects[0]["file_count"] >= MAX_FILES

    root = PROJECTS_ROOT.resolve()
    if root.exists() and root.is_dir():
        try:
            entries = sorted(
                (e for e in root.iterdir() if e.is_dir() and not e.name.startswith(".")),
                key=lambda e: e.name.lower(),
            )
        except OSError:
            entries = []
        for entry in entries:
            n = _count_files(entry)
            projects.append({
                "name": entry.name,
                "label": entry.name,
                "is_default": False,
                "file_count": n,
                # The count stops at MAX_FILES (it only flags empty projects):
                # true means "at least this many" — /tree has the real count.
                "file_count_is_floor": n >= MAX_FILES,
            })

    return {"projects": projects}


@router.get("/tree", dependencies=[Depends(requires_permission("read:workspace_files"))])
async def workspace_tree(
    project: str = Query(default=""),
    max_files: int = Query(default=MAX_FILES, ge=1),
    path: str = Query(default=""),
    offset: int = Query(default=0, ge=0),
):
    """Return a recursive file tree for CodeCity visualization.

    With no `project`, reads the default WORKSPACE (legacy behavior). With
    `project=<name>`, reads /var/lib/cseries/projects/<name> after a
    realpath guard. A name that escapes PROJECTS_ROOT or doesn't exist
    returns an empty tree rather than leaking files.

    `path` opens ONE directory of it (lazy loading, RT-B7). `max_files` files
    are listed from `offset` (one page, at most _PAGE_MAX — `max_files_clamped`
    says when it was), `total_files` counts every file under the root and
    `next_offset` says where the next page starts. A symlink is listed as a
    link entry and never followed (B3). The walk runs on a worker thread,
    never on the event loop.
    """
    # Called directly (the host's own tools, the gates), a parameter left out
    # is still its Query() marker, not its default.
    project = project if isinstance(project, str) else ""
    path = path if isinstance(path, str) else ""
    max_files = max_files if isinstance(max_files, int) else MAX_FILES
    offset = max(0, offset) if isinstance(offset, int) else 0
    ws = _resolve_project(project)
    label = project or "workspace"
    empty = {"tree": {"name": label, "path": "/", "type": "directory",
                      "children": [], "size": 0, "file_count": 0},
             "truncated": False, "total_files": 0, "listed_files": 0,
             "offset": offset, "next_offset": None, "total_is_floor": False,
             "total_links": 0, "project": project}
    if ws is None:
        return empty
    root, rel = ws, ""
    if path.strip("/"):
        sub = (ws / path.strip("/")).resolve()
        try:
            sub.relative_to(ws)
        except ValueError:
            return empty
        if not sub.is_dir():
            return empty
        root, rel = sub, path.strip("/")

    page = min(max_files, _PAGE_MAX)
    tree, stats = await asyncio.to_thread(_tree_page, root, rel, offset=offset, limit=page)
    tree["name"] = label if not rel else root.name
    end = offset + stats["listed"]
    out = {
        "tree": tree,
        "truncated": stats["seen"] > stats["listed"],
        "total_files": stats["seen"],
        "listed_files": stats["listed"],
        "offset": offset,
        "next_offset": end if end < stats["seen"] else None,
        "total_is_floor": stats["total_is_floor"],
        "total_links": stats["links"],
        "project": project,
    }
    if page < max_files:
        out["max_files_clamped"] = True
        out["note"] = (f"one answer lists at most {_PAGE_MAX} files; "
                       "read on with offset=next_offset")
    if stats["total_is_floor"]:
        out["note"] = (out.get("note", "") + "; " if out.get("note") else "") + (
            f"the count stopped after {_VISIT_BUDGET} entries — there are at least "
            f"{stats['seen']} files; open a directory with `path` to count it")
    return out


# One PAGE of a file in the viewer: a larger file — a
# lockfile, generated JSON — is read in pages with offset/length instead of
# refused as "too_large". It bounds one answer to what a browser tab renders
# comfortably; every answer says the file's size and where the next page starts.
_MAX_FILE_BYTES = 512 * 1024  # 512KB per page


@router.get("/file", dependencies=[Depends(requires_permission("read:workspace_files"))])
async def workspace_file(path: str = Query(..., min_length=1),
                         project: str = Query(default=""),
                         offset: int = Query(default=0, ge=0),
                         length: int | None = Query(default=None, ge=1)):
    """Return raw file contents for the Files tab viewer.

    With no `project`, reads under the default WORKSPACE. With
    `project=<name>` it reads under /var/lib/cseries/projects/<name> — the
    SAME base `/tree` uses — so the viewer can open files from any project
    (incl. per-harness bake-off subdirs), not just the empty default
    workspace. Without this the tree shows a project's files but opening
    one 404s against the workspace root (the "no code shows up" bug).

    Sandboxed: symlinks are followed via .resolve() and the result must
    still live under the resolved base. Binary files return a marker.

    Paged: `offset`/`length` (bytes; one page is at most _MAX_FILE_BYTES) read
    only that range. `size` is the whole file, `next_offset` the next page
    (None at the end), `truncated` true when more follows. A page starts and
    ends on whole UTF-8 characters.
    """
    base = _resolve_project(project) if project else WORKSPACE
    if base is None:
        return {"ok": False, "error": "unknown_project"}
    ws = base.resolve()
    target = (ws / path.lstrip("/")).resolve()

    try:
        target.relative_to(ws)
    except ValueError:
        return {"ok": False, "error": "path_outside_workspace"}

    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "not_found"}

    size = target.stat().st_size
    want = _MAX_FILE_BYTES if length is None else min(length, _MAX_FILE_BYTES)
    with open(target, "rb") as fh:
        head = fh.read(8192)
        if b"\x00" in head:
            return {"ok": True, "binary": True, "size": size,
                    "language": _lang_for(target.name)}
        fh.seek(offset)
        raw = fh.read(want)

    from server.routes.files import _utf8_end, _utf8_start
    skip = _utf8_start(raw) if offset else 0
    body = raw[skip:]
    if offset + len(raw) < size:
        cut = _utf8_end(body)
        if cut > 0:
            body = body[:cut]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1", errors="replace")

    start = offset + skip
    end = start + len(body)
    more = end < size
    return {
        "ok": True,
        "binary": False,
        "size": size,
        "language": _lang_for(target.name),
        "content": text,
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
        "offset": start,
        "length": len(body),
        "next_offset": end if more else None,
        "truncated": more,
    }


# No size cap on a save (it once refused past 1 MB): it is the
# user's own file, and the real limit is the disk, which the write itself meets
# (a full disk answers write_failed with the OS's own "No space left on device").


from pydantic import BaseModel  # noqa: E402


class WriteFileReq(BaseModel):
    path: str
    content: str


@router.post(
    "/file",
    dependencies=[Depends(requires_permission("write:workspace_files"))],
)
async def write_workspace_file(req: WriteFileReq):
    """Save a workspace file from the Codespace IDE.

    Sandboxed to $CEIGAS_WORKSPACE. Creates the parent dir if needed.
    Atomic write via temp+rename. Refuses binary content (we're an
    editor, not a file upload service).
    """
    ws = WORKSPACE.resolve()
    target = (ws / req.path.lstrip("/")).resolve()

    try:
        target.relative_to(ws)
    except ValueError:
        return {"ok": False, "error": "path_outside_workspace"}

    if "\x00" in req.content[:8192]:
        return {"ok": False, "error": "binary_content_refused"}

    raw = req.content.encode("utf-8", errors="replace")

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        tmp.replace(target)
    except OSError as e:
        try:
            tmp.unlink()  # a half-written temp never stays behind
        except OSError:
            pass
        return {"ok": False, "error": f"write_failed: {e}"}

    return {
        "ok": True,
        "path": req.path,
        "size": len(raw),
        "lines": req.content.count("\n") + (0 if req.content.endswith("\n") else 1),
    }
