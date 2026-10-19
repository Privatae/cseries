"""Which page a preview opens — the ONE rule, for a run's tree and a project's.

Origin (2026-09-30). A kimi run changed one desktop
theme and added four more, all at themes/<name>/index.html. Every preview
lane picked "the shallowest index.html" and broke the five-way tie by
filesystem order, so the run card and the project preview both opened
Graveyard Shift, a page the user had not asked about, while the theme under
discussion (Elm Lantern) was one of the five. Two copies of the rule existed
(review._find_entry_index and projects.resolve_project_entry) and both
silently chose.

The rule, in order; the first step that finds anything is the answer:

  manifest   (project trees only) app.yaml's entrypoint, when that file exists.
  changed    the entry pages the RUN changed (its own diff) — a run card shows
             what that run produced, not whatever sits shallowest in the tree.
  enclosing  the run changed no entry page but changed files under one (a
             CSS or script fix): the nearest index.html above each of them.
  root       index.html at the root.
  tree       every index.html at the shallowest depth.

Several entries is an ANSWER, never a tie to break: the caller shows the list
and the person picks — with ONE exception, the product default: when the
root index.html is among the pages the run changed, it
opens directly and the others are reachable from a page switcher
(EntryChoice.default). Only a real page counts (page_in_tree, below): a
regular file inside the root, reached without a symlink, outside the
directories the caller skips. Each caller passes its own skips, as it did
before this rule existed: a run's tree skips .git (a real page under a build
tool's .output/public/ counts); a project's tree step skips review machinery
and dot-directories; an artifact skips .git and its revision store. The
app.yaml entrypoint may name any real file (a process app's main.py).

A page named by a caller (?entry=, an artifact's entry) goes through
entry_param: 400 when the name could leave the tree at all (a NUL or other
control character, an absolute path, a `..` component), 404 when it is safe
but names no real file in the tree. A real file is a regular file inside the
root, reached without a symlink anywhere on the way (a link could point out
of the jail). Which directories hold no pages (.git, the artifact revision
store, review machinery) is each caller's own rule, passed as `skip_dir`;
this module adds none, so a real page under a dot-directory (a build tool's
.output/public/index.html) is as visible here as it was before.

Choosing a file and reading it are two moments, and the tree can change in
between. So every read on this path (the page itself, each asset inlined
into it, a file served whole) goes through open_in_tree / read_in_tree,
which open the file from the root down one component at a time and never
follow a symlink. What was checked can be swapped for a link afterwards;
the open still does not follow it.

Origin (2026-09-30): the artifact render joined its `entry` onto the
workspace unchecked, and GET /api/artifacts/{id}/render?entry= passes the
query straight through, so `entry=../../<file>` returned any file the
kernel could read, HTML or not. The revision render
(/api/artifacts/{id}/history/{rev}/render) had the same join.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional


ENTRY_NAME = "index.html"


@dataclass(frozen=True)
class EntryChoice:
    """Root-relative POSIX paths of the pages to offer, and which step found
    them (manifest | changed | enclosing | root | tree | none)."""
    entries: tuple[str, ...]
    basis: str

    @property
    def single(self) -> Optional[str]:
        return self.entries[0] if len(self.entries) == 1 else None

    @property
    def default(self) -> Optional[str]:
        """The page to open without asking: the only one, or — the product
        default — the root index.html when it is among several the run changed. None
        means the person picks from `entries`."""
        if len(self.entries) == 1:
            return self.entries[0]
        if ENTRY_NAME in self.entries and self.basis in ("changed", "enclosing"):
            return ENTRY_NAME
        return None


class EntryParamError(ValueError):
    """A named page refused: `status` 400 (not a safe page name) or 404 (safe,
    but not a real page in this tree)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _skipped(parts: Iterable[str], skip_dir: Optional[Callable[[str], bool]]) -> bool:
    return skip_dir is not None and any(skip_dir(p) for p in parts)


def page_in_tree(root: Path, rel: str, *,
                 skip_dir: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """`rel` normalised when it names a real file inside `root`, else None.

    Refuses absolute paths, a `..` component, directories the caller skips,
    symlinks (the file itself or any directory on the way), anything whose
    real path leaves the root, and anything that is not a regular file."""
    rel = str(rel or "")
    if _CONTROL.search(rel):
        return None   # a NUL reaches the OS as ValueError; never a path here
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith("/"):
        return None
    pp = PurePosixPath(rel)
    if ".." in pp.parts:
        return None
    if _skipped(pp.parts[:-1], skip_dir):
        return None
    try:
        root_real = root.resolve()
        target = root.joinpath(*pp.parts)
        cur = root
        for part in pp.parts:
            cur = cur / part
            if cur.is_symlink():
                return None
        real = target.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if root_real not in real.parents or not real.is_file():
        return None
    return pp.as_posix()


def entry_param(root: Path, rel, *,
                skip_dir: Optional[Callable[[str], bool]] = None) -> str:
    """A page NAMED by a caller, checked; the normalised root-relative path.

    Raises EntryParamError(400) when the name could leave the tree at all —
    empty, a NUL or other control character, an absolute path, `..`
    anywhere — and EntryParamError(404) when it is safe but names no real
    file in the tree (missing, a symlink, skipped, not a regular file)."""
    raw = "" if rel is None else str(rel)
    if _CONTROL.search(raw):
        raise EntryParamError(400, "the page name contains a control character")
    name = raw.strip().replace("\\", "/")
    if not name:
        raise EntryParamError(400, "no page named")
    if name.startswith("/") or ".." in PurePosixPath(name).parts:
        raise EntryParamError(400, f"{raw!r} is not a path inside this tree")
    found = page_in_tree(root, name, skip_dir=skip_dir)
    if found is None:
        raise EntryParamError(404, f"{raw!r} is not a page here")
    return found


def _at_read(root: Path, parts: tuple) -> None:
    """The moment between choosing a file and opening it. Does nothing; the
    gate replaces it to swap a symlink in at exactly this point."""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: a FIFO planted where a file was must not hang the open; the
# fstat below refuses it. It changes nothing for a regular file.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_in_tree(root: Path, rel) -> Optional[int]:
    """A read-only descriptor for the regular file at `rel` under `root`, or
    None. The caller owns it and must close it.

    Opens `root` (the caller's own jail, chosen by the server), then each
    component of `rel` relative to the one before it, with O_NOFOLLOW on
    every one, and keeps the final descriptor only after fstat says it is a
    regular file. A symlink anywhere below the root, including one swapped
    in after the caller checked the path, is never followed. Refuses
    absolute paths, `..` and control characters. Never raises."""
    rel = str(rel or "")
    if _CONTROL.search(rel):
        return None
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/"):
        return None
    parts = PurePosixPath(rel).parts
    if not parts or ".." in parts:
        return None
    _at_read(root, parts)
    fds: list[int] = []
    try:
        fds.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        for part in parts[:-1]:
            fds.append(os.open(part, _DIR_FLAGS, dir_fd=fds[-1]))
        fds.append(os.open(parts[-1], _FILE_FLAGS, dir_fd=fds[-1]))
        if not stat.S_ISREG(os.fstat(fds[-1]).st_mode):
            return None
        return fds.pop()
    except (OSError, ValueError):
        return None
    finally:
        for fd in fds:
            os.close(fd)


def fd_file_response(fd: int, *, media_type: str):
    """A FileResponse that serves the file behind `fd` (opened by
    open_in_tree) and closes `fd` when the response ends, however it ends.

    Starlette re-opens a FileResponse's path to send the body (each range
    too). The path given here is /proc/self/fd/<fd>, which names the open
    file itself, never a name in the tree, so nothing swapped into the tree
    after the open can change what is sent. Headers (length, ETag,
    Last-Modified, ranges) are Starlette's own, from fstat of that file."""
    from starlette.responses import FileResponse

    class _OpenFileResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                os.close(fd)

    return _OpenFileResponse(f"/proc/self/fd/{fd}", media_type=media_type,
                             stat_result=os.fstat(fd))


def read_in_tree(root: Path, rel, limit: Optional[int] = None) -> Optional[str]:
    """The text of the regular file at `rel` under `root` (its first `limit`
    bytes when given), read from the descriptor open_in_tree opened, or None.
    Never raises."""
    fd = open_in_tree(root, rel)
    if fd is None:
        return None
    try:
        chunks, left = [], limit
        while left is None or left > 0:
            chunk = os.read(fd, 1 << 20 if left is None else min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            if left is not None:
                left -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _tree_entries(root: Path, skip_dir: Optional[Callable[[str], bool]]) -> list[str]:
    """Every index.html at the shallowest depth that has one (pruned walk:
    directories the caller skips are never entered, links never followed)."""
    best: list[str] = []
    best_depth: Optional[int] = None
    try:
        walker = os.walk(root)
    except OSError:
        return []
    for cur, dirs, files in walker:
        rel_dir = Path(cur).relative_to(root)
        depth = len(rel_dir.parts) + 1
        if best_depth is not None and depth > best_depth:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not _skipped((d,), skip_dir))
        if ENTRY_NAME in files:
            rel = (PurePosixPath(*rel_dir.parts) / ENTRY_NAME).as_posix()
            if page_in_tree(root, rel, skip_dir=skip_dir) is None:
                continue
            if best_depth is None or depth < best_depth:
                best, best_depth = [rel], depth
            elif depth == best_depth:
                best.append(rel)
    return sorted(best)


def _manifest_entry(root: Path) -> Optional[str]:
    """app.yaml's entrypoint when it names a real file in the tree. app.yaml is
    read by read_in_tree (a linked app.yaml is not read: its answer would leak
    a bit about a file outside), and the entrypoint may be any real file, in
    any folder: a process app's main.py is its entry."""
    manifest = read_in_tree(root, "app.yaml")
    if manifest is None:
        return None
    try:
        import yaml
        entry = str((yaml.safe_load(manifest) or {}).get("entrypoint") or "").strip()
    except Exception:
        return None  # an invalid manifest never blocks previewing index.html
    return page_in_tree(root, entry) if entry else None


def choose_entry(root: Path, *, changed: Iterable[str] = (), manifest: bool = False,
                 skip_dir: Optional[Callable[[str], bool]] = None) -> EntryChoice:
    """The page(s) to open for `root`. `changed` = the root-relative paths the
    run changed (its diff); leave it empty for a tree no run is asking about.
    `manifest=True` lets app.yaml's entrypoint answer first (project trees).
    `skip_dir` is the caller's own rule for directories that hold no pages;
    it applies to the changed, enclosing and tree steps (the root page has no
    directory to skip, and the entrypoint is the manifest's to name).
    Never raises; a missing or unreadable root is EntryChoice((), "none")."""
    try:
        if not root.is_dir():
            return EntryChoice((), "none")
    except OSError:
        return EntryChoice((), "none")

    if manifest:
        m = _manifest_entry(root)
        if m:
            return EntryChoice((m,), "manifest")

    changed = [str(c) for c in changed if c]
    pages = sorted({p for p in (page_in_tree(root, c, skip_dir=skip_dir)
                                for c in changed if PurePosixPath(c).name == ENTRY_NAME) if p})
    if pages:
        return EntryChoice(tuple(pages), "changed")

    enclosing: set[str] = set()
    for c in changed:
        for d in PurePosixPath(c).parents:
            rel = (d / ENTRY_NAME).as_posix() if str(d) != "." else ENTRY_NAME
            found = page_in_tree(root, rel, skip_dir=skip_dir)
            if found:
                enclosing.add(found)
                break
    if enclosing:
        return EntryChoice(tuple(sorted(enclosing)), "enclosing")

    if page_in_tree(root, ENTRY_NAME, skip_dir=skip_dir):
        return EntryChoice((ENTRY_NAME,), "root")

    tree = _tree_entries(root, skip_dir)
    return EntryChoice(tuple(tree), "tree" if tree else "none")


def choice_headers(choice: EntryChoice, shown: str) -> dict:
    """Response headers for a page opened by default while others qualify
    (the root among several changed pages): the window's page switcher reads
    them. JSON, ASCII-escaped, so any file name is a legal header value."""
    import json
    if len(choice.entries) < 2:
        return {}
    return {"X-Preview-Entries": json.dumps(list(choice.entries[:50])),
            "X-Preview-Entry": json.dumps(shown)}


def choice_detail(choice: EntryChoice, whose: str = "this run") -> str:
    """The sentence a caller shows above the list of pages."""
    n = len(choice.entries)
    if choice.basis == "changed":
        return f"{whose} changed {n} pages — choose one to open"
    if choice.basis == "enclosing":
        return f"{whose} changed files under {n} pages — choose one to open"
    return f"{n} pages sit at the top of {whose} — choose one to open"
