"""Reading a whole tree to send it somewhere, without ever leaving the tree.

Every reader that takes a project, repository, artifact or app folder and
sends its bytes elsewhere (a published site, an app bundle, the code index
and its embedder, an artifact's history) walks the folder through this
module. A symbolic link in the folder can point anywhere the process can
read: `/proc/self/environ`, a mounted secret. A link gets into a folder by
an ordinary `git checkout` of a repository that holds one (git's default),
and a GitHub repository reaches the user's Gitea through the migrate API
(server/routes/github_mirror.py), so its links are not the user's choice
either.

The rule: nothing outside the tree is ever read.

- The tree's root is opened once and pinned as a descriptor. The root's own
  name is opened with O_NOFOLLOW, so a folder that is itself a link (a
  `dist` pointing at `/`) is refused, not walked.
- Every directory and file below it is opened from that descriptor one
  component at a time with O_NOFOLLOW, so a link anywhere on the way is
  refused, including one swapped in after the walk listed the name.
- A file is read only after fstat says it is a regular file (O_NONBLOCK:
  a FIFO planted where a file was must not hang the open).
- A link is skipped, unless the caller asks for links="in_tree": then a
  RELATIVE link whose target, resolved inside the tree, is a regular file
  or a folder the same walk would itself include (the caller's `skip` drops
  neither it nor any folder on its path), reached with no further link, is
  read from that target: a file's bytes, or a folder's files (walked the
  same contained way, shown under the link's name). Its bytes are already
  in what the caller sends, so the copy discloses nothing new. An absolute
  link, a link out of the tree, a link to a skipped part, a link to a link
  and a folder link that loops back are not followed; `refused` names them
  for a caller that must say so rather than drop them.

The target of an in-tree link is resolved lexically (`a/../b` is `b`) and
then opened component by component without following anything, so the
bytes read are always the in-tree file at that normalised path.

Origin (2026-10-01): an independent review reproduced three readers that
followed a link out of a project folder: "Publish as site" put the target's
content in a PUBLIC site's zip, the app bundle carried it, and the code
index sent it to the embedder and into the Host's code search.
"""
from __future__ import annotations

import os
import posixpath
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

# skip(parts, is_dir): True drops the entry (a directory is not entered).
# `parts` are the entry's root-relative path components.
Skip = Callable[[tuple, bool], bool]

LINKS_SKIP = "skip"
LINKS_IN_TREE = "in_tree"
# Linked folders one in_tree walk follows. Each is walked whole, so links
# that fan out into one another multiply what a walk reads; past this many
# the rest are refused (and reported), never followed.
_MAX_LINKED_DIRS = 256

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class TreeEscape(OSError):
    """The tree's own root is a link or not a directory."""


class TreeFile(NamedTuple):
    rel: str      # where the file appears in the tree (posix, root-relative)
    source: str   # where its bytes are read from: `rel`, or an in-tree link's target
    size: int
    mode: int     # st_mode and st_mtime of the file its bytes come from
    mtime: float


class TreeEntry(NamedTuple):
    """One name in the tree, as it is: never followed."""
    rel: str               # posix, root-relative
    kind: str              # 'dir', 'file' or 'link'
    mode: int              # lstat st_mode (0 for a link)
    mtime_ns: int          # lstat st_mtime_ns (0 for a link)
    target: Optional[str]  # a link's own target text; None otherwise


def _at_open(tree: "Tree", parts: tuple) -> None:
    """The moment before a folder or file below the root is opened. Does
    nothing; a gate replaces it to swap a link in at exactly this point and
    prove the open still does not follow it."""


def link_leaves_tree(rel: str, target: str) -> bool:
    """Whether the link at `rel` (root-relative) with target text `target`
    points outside the tree: an absolute target, or a relative one that
    climbs above the root. Decided on the text, lexically; a link that only
    leads out by way of another link is caught as that other link."""
    if not target or target.startswith("/"):
        return True
    parent = posixpath.dirname(rel)
    norm = posixpath.normpath(posixpath.join(parent, target) if parent else target)
    return norm == ".." or norm.startswith("../") or norm.startswith("/")


def _parts(rel) -> Optional[tuple]:
    """A tree-relative POSIX path as components, or None when it could leave
    the tree (absolute, `..`, NUL). A backslash is an ordinary character in a
    Linux name, so it is not a separator here."""
    rel = str(rel or "")
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    parts = tuple(p for p in rel.split("/") if p not in ("", "."))
    if not parts or ".." in parts:
        return None
    return parts


class Tree:
    """An open tree. Use as a context manager (open_tree_no_escape).

    `root`'s own name is opened without following a link; the folders ABOVE
    it are opened as named, so they must be the server's own (a projects
    root, a repos root, a workspace). A folder below what a person can write
    (a project's build folder, a revision) is reached with `within`, from
    the project's own folder, one component at a time without following a
    link: nothing on that path can be swapped for a link between choosing
    it and opening it."""

    def __init__(self, root, within: str = ""):
        self._fd = None
        root = Path(root)
        parent, name = root.parent, root.name
        if not name:   # "/" or similar: nothing above it to open from
            try:
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            except OSError as e:
                raise TreeEscape(e.errno, f"cannot open {root}: {e.strerror}") from None
        else:
            try:
                pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            except OSError as e:
                raise TreeEscape(e.errno, f"cannot open {parent}: {e.strerror}") from None
            try:
                fd = os.open(name, _DIR_FLAGS, dir_fd=pfd)
            except OSError as e:
                raise TreeEscape(e.errno, f"{root} is a link or not a directory") from None
            finally:
                os.close(pfd)
        if within:
            parts = _parts(within)
            if parts is None:
                os.close(fd)
                raise TreeEscape(0, f"{within!r} is not a path inside {root}")
            try:
                for p in parts:
                    nfd = os.open(p, _DIR_FLAGS, dir_fd=fd)
                    os.close(fd)
                    fd = nfd
            except OSError as e:
                os.close(fd)
                raise TreeEscape(e.errno, f"{root}/{within} is reached through a link "
                                          "or is not a directory") from None
        self._fd = fd

    # ── lifetime ────────────────────────────────────────────────────────────
    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "Tree":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── opening below the root, never through a link ───────────────────────
    def _open_dir(self, parts: tuple) -> int:
        """A descriptor on the directory at `parts` (the root itself for ()).
        Raises OSError when any component is a link or not a directory."""
        _at_open(self, parts)
        fd = os.dup(self._fd)
        try:
            for p in parts:
                nfd = os.open(p, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nfd
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open(self, rel) -> Optional[int]:
        """A read-only descriptor on the regular file at `rel`, or None. The
        caller owns it. No component (the file included) may be a link."""
        parts = _parts(rel)
        if parts is None or self._fd is None:
            return None
        _at_open(self, parts)
        try:
            dfd = self._open_dir(parts[:-1])
        except OSError:
            return None
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dfd)
        except OSError:
            return None
        finally:
            os.close(dfd)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                return fd
        except OSError:
            pass
        os.close(fd)
        return None

    def read(self, rel, limit: Optional[int] = None) -> Optional[bytes]:
        """The bytes of the regular file at `rel` (a TreeFile reads its
        `source`), at most `limit` of them, or None. Never raises."""
        if isinstance(rel, TreeFile):
            rel = rel.source
        fd = self.open(rel)
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
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)

    # ── the walk ────────────────────────────────────────────────────────────
    def _list(self, parts: tuple) -> list:
        """(name, kind, stat, link_target) for every entry of the directory at
        `parts`, sorted by name; kind is 'dir', 'file', 'link' or 'other'.
        [] when the directory cannot be opened without following a link."""
        try:
            fd = self._open_dir(parts)
        except OSError:
            return []
        out = []
        try:
            with os.scandir(fd) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            out.append((e.name, "link", None, os.readlink(e.name, dir_fd=fd)))
                        elif e.is_dir(follow_symlinks=False):
                            out.append((e.name, "dir", e.stat(follow_symlinks=False), None))
                        elif e.is_file(follow_symlinks=False):
                            out.append((e.name, "file", e.stat(follow_symlinks=False), None))
                        else:
                            out.append((e.name, "other", None, None))
                    except OSError:
                        continue
        except OSError:
            return []
        finally:
            os.close(fd)
        out.sort(key=lambda x: x[0])
        return out

    def _resolve_link(self, real_dir: tuple, target: str,
                      skip: Optional[Skip]) -> tuple:
        """Where a link in the real folder `real_dir` leads: (kind, parts,
        stat, why). kind is 'file' or 'dir' for a regular file or folder
        inside the tree, reached from the root with no further link and
        dropped by neither `skip` nor any folder on its path; otherwise
        'refused', with `why` in plain words. The target is resolved
        lexically against the link's REAL folder, as the kernel would."""
        if not target or target.startswith("/") or "\x00" in target:
            return "refused", (), None, "points outside the folder"
        norm = posixpath.normpath(posixpath.join(*real_dir, target) if real_dir else target)
        if norm in ("..",) or norm.startswith("../") or norm.startswith("/"):
            return "refused", (), None, "points outside the folder"
        if norm in ("", "."):
            return "refused", (), None, "loops back into a folder that holds it"
        tparts = tuple(norm.split("/"))
        if skip is not None and (any(skip(tparts[:i], True) for i in range(1, len(tparts)))):
            return "refused", (), None, "points into a part that is left out"
        fd = self.open(norm)
        if fd is not None:
            try:
                st = os.fstat(fd)
            finally:
                os.close(fd)
            if skip is not None and skip(tparts, False):
                return "refused", (), None, "points into a part that is left out"
            return "file", tparts, st, None
        try:
            dfd = self._open_dir(tparts)
        except OSError:
            return ("refused", (), None,
                    "points at nothing here (missing, or reached through another link)")
        os.close(dfd)
        if skip is not None and skip(tparts, True):
            return "refused", (), None, "points into a part that is left out"
        return "dir", tparts, None, None

    def entries(self, *, skip: Optional[Skip] = None) -> Iterator[TreeEntry]:
        """Every folder, regular file and link under the root, in sorted
        pre-order (a folder before what it holds), none of them followed.
        `skip(parts, is_dir)` drops an entry (a dropped folder is not
        entered; a link is offered as is_dir=False). Sockets, FIFOs and
        devices are left out."""
        if self._fd is None:
            return
        stack = [((), iter(self._list(())))]
        while stack:
            dparts, items = stack[-1]
            nxt = next(items, None)
            if nxt is None:
                stack.pop()
                continue
            name, kind, st, target = nxt
            if kind == "other":
                continue
            parts = dparts + (name,)
            if skip is not None and skip(parts, kind == "dir"):
                continue
            rel = "/".join(parts)
            if kind == "link":
                yield TreeEntry(rel, "link", 0, 0, target)
                continue
            yield TreeEntry(rel, kind, st.st_mode, st.st_mtime_ns, None)
            if kind == "dir":
                stack.append((parts, iter(self._list(parts))))

    def walk(self, *, skip: Optional[Skip] = None, links: str = LINKS_SKIP,
             refused: Optional[list] = None) -> Iterator[TreeFile]:
        """Every regular file under the root, in sorted(Path.rglob) order.

        `skip(parts, is_dir)` drops an entry (a dropped directory is not
        entered). Links: see the module docstring; with links="in_tree" a
        linked FOLDER inside the tree is walked too, under the link's name,
        through the same contained opens, never into a folder already being
        walked (a loop) and at most _MAX_LINKED_DIRS of them. `refused`, when
        given, receives (rel, target, why) for each link the walk did not
        follow, so a caller that must not drop anything silently can say
        which. Sockets, FIFOs and devices are never yielded."""
        if links not in (LINKS_SKIP, LINKS_IN_TREE):
            raise ValueError(f"links must be {LINKS_SKIP!r} or {LINKS_IN_TREE!r}")
        if self._fd is None:
            return
        # A stack of (parts as shown, parts on disk, remaining entries):
        # iterative, so a deep tree needs neither recursion nor one open
        # descriptor per level. The two parts differ only below a followed
        # linked folder: shown under the link's name, read from its target.
        stack = [((), (), iter(self._list(())))]
        followed = 0
        while stack:
            shown, real, items = stack[-1]
            nxt = next(items, None)
            if nxt is None:
                stack.pop()
                continue
            name, kind, st, target = nxt
            parts, rparts = shown + (name,), real + (name,)
            if skip is not None and skip(parts, kind == "dir"):
                continue
            rel = "/".join(parts)
            if kind == "dir":
                stack.append((parts, rparts, iter(self._list(rparts))))
            elif kind == "file":
                yield TreeFile(rel, "/".join(rparts), st.st_size, st.st_mode, st.st_mtime)
            elif kind == "link":
                if links != LINKS_IN_TREE:
                    continue
                tkind, tparts, tst, why = self._resolve_link(real, target, skip)
                if tkind == "dir" and skip is not None and skip(parts, True):
                    continue      # left out by its own name, as a real folder would be
                if tkind == "dir":
                    # A loop: the target is a folder already being walked, or
                    # one that holds it.
                    if any(r[:len(tparts)] == tparts for _s, r, _i in stack):
                        tkind, why = "refused", "loops back into a folder that holds it"
                    elif followed >= _MAX_LINKED_DIRS:
                        tkind, why = "refused", (f"is past the {_MAX_LINKED_DIRS} linked "
                                                 "folders one walk follows")
                if tkind == "file":
                    yield TreeFile(rel, "/".join(tparts), tst.st_size, tst.st_mode, tst.st_mtime)
                elif tkind == "dir":
                    followed += 1
                    stack.append((parts, tparts, iter(self._list(tparts))))
                elif refused is not None:
                    refused.append((rel, target, why))


# How much text either side of a window a scanner sees, so a match or a
# comment that crosses the window's edge is read whole by the window it
# starts in.
SCAN_CONTEXT_BYTES = 64 * 1024


def _decode(data: bytes) -> str:
    return universal_newlines(data.decode("utf-8", errors="ignore"))


def scan_windows(root, *, suffixes: tuple, skip_dirs, exclude_top=frozenset(),
                 window_bytes: int) -> Iterator[tuple]:
    """(rel, text, lo, hi) for EVERY source file a contract scanner reads
    under `root`: files with one of `suffixes`, none under a `skip_dirs` name
    or an `exclude_top` folder, in sorted order, decoded as
    read_text(errors="ignore") did. A link is read only where it leads inside
    the tree (links="in_tree"), so a scanner that quotes what it matched can
    never quote a file outside it. Nothing when `root` is not a real folder.

    No file is skipped for its size or its place in the list (a gate that
    skipped the 401st file, or a 2 MB bundled app.js, passed whatever was in
    it). Memory stays bounded instead: a file up to `window_bytes` is one
    window; a bigger one is read in windows of `window_bytes`, each with
    SCAN_CONTEXT_BYTES of the file on either side. A scanner counts a match
    only when it STARTS in text[lo:hi], so each match is counted once, by the
    window that holds it with its context."""
    def _skip(parts: tuple, is_dir: bool) -> bool:
        return any(p in skip_dirs for p in parts) or parts[0] in exclude_top
    try:
        tree = open_tree_no_escape(root)
    except TreeEscape:
        return
    with tree:
        files = [f for f in tree.walk(skip=_skip, links=LINKS_IN_TREE)
                 if posixpath.splitext(f.rel)[1].lower() in suffixes]
        for f in files:
            if f.size <= window_bytes:
                data = tree.read(f, limit=window_bytes + 1)
                if data is None:
                    continue
                if len(data) <= window_bytes:
                    text = _decode(data)
                    yield f.rel, text, 0, len(text)
                    continue
            # Bigger than a window (or grew since the walk): stream it.
            fd = tree.open(f.source)
            if fd is None:
                continue
            try:
                size = os.fstat(fd).st_size
                ctx = SCAN_CONTEXT_BYTES
                for start in range(0, max(size, 1), window_bytes):
                    lead_at = max(0, start - ctx)
                    end = min(size, start + window_bytes)
                    chunk = os.pread(fd, min(size, end + ctx) - lead_at, lead_at)
                    lead = _decode(chunk[:start - lead_at])
                    body = _decode(chunk[start - lead_at:end - lead_at])
                    tail = _decode(chunk[end - lead_at:])
                    yield f.rel, lead + body + tail, len(lead), len(lead) + len(body)
            except OSError:
                continue
            finally:
                os.close(fd)


def describe_refused(refused: list, limit: int = 10) -> str:
    """`a` → `/etc` (points outside the folder), … for a message."""
    named = ", ".join(f"`{rel}` → `{target}` ({why})" for rel, target, why in refused[:limit])
    if len(refused) > limit:
        named += f", and {len(refused) - limit} more"
    return named


def open_tree_no_escape(root, within: str = "") -> Tree:
    """Open `root` for a walk that never leaves it. Raises TreeEscape when the
    root's own name is a link or not a directory. `within` (root-relative)
    makes the tree that folder instead, reached from `root` one component at
    a time without following a link."""
    return Tree(root, within)


def open_dir_no_follow(root, within: str = "") -> int:
    """A descriptor on the folder `root` (or `within` it), opened exactly as
    open_tree_no_escape opens a tree's root. The caller owns it. Raises
    TreeEscape."""
    tree = Tree(root, within)
    fd, tree._fd = tree._fd, None
    return fd


def links_leaving(root, *, skip: Optional[Skip] = None) -> list:
    """(rel, target) of every link under `root` (as `skip` leaves it) whose
    target is outside the tree (link_leaves_tree)."""
    with open_tree_no_escape(root) as tree:
        return [(e.rel, e.target) for e in tree.entries(skip=skip)
                if e.kind == "link" and link_leaves_tree(e.rel, e.target)]


def copy_tree_no_escape(src, dest, *, skip: Optional[Skip] = None) -> int:
    """Copy the tree at `src` into the existing, empty folder `dest`: its
    folders, its regular files (bytes, permission bits and mtime, as
    shutil.copy2 keeps them) and every link AS a link with its own target
    text. Nothing is followed, so a link, or a folder swapped for a link
    while the copy runs, never brings in what it points at. Sockets, FIFOs
    and devices are left out. Returns the number of files and links
    written. Raises TreeEscape when `src` itself is a link."""
    dest = Path(dest)
    written = 0
    dirs: list = []
    with open_tree_no_escape(src) as tree:
        for e in tree.entries(skip=skip):
            out = dest / e.rel
            if e.kind == "dir":
                out.mkdir(exist_ok=True)
                dirs.append((out, e))
            elif e.kind == "link":
                os.symlink(e.target, out)
                written += 1
            else:
                fd = tree.open(e.rel)
                if fd is None:
                    continue          # gone, or swapped for a link, since it was listed
                with os.fdopen(fd, "rb") as fin:
                    st = os.fstat(fin.fileno())
                    with open(out, "xb") as fout:
                        shutil.copyfileobj(fin, fout)
                os.chmod(out, stat.S_IMODE(st.st_mode))
                os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
                written += 1
    # A folder's own bits and time last, as copytree does: writing into it
    # moved its mtime, and a read-only one could not be written into.
    for out, e in reversed(dirs):
        os.chmod(out, stat.S_IMODE(e.mode))
        os.utime(out, ns=(e.mtime_ns, e.mtime_ns))
    return written


def universal_newlines(text: str) -> str:
    """What Path.read_text returns for the same file: \\r\\n and \\r read as
    \\n. Readers that moved from read_text to bytes keep their output (and
    the code index its stored hashes) unchanged."""
    return text.replace("\r\n", "\n").replace("\r", "\n")
