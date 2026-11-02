"""Write one file in a folder as a NEW inode, renamed over the name.

The kernel's writers into project folders opened the destination and wrote:
`Path.write_text`, `shutil.copyfile`, `Path.write_bytes`. Each follows a link
that is the destination, and each writes into the existing inode. So a name
that is a symbolic link wrote wherever it points, inside .git or outside the
project, and a name that is a hard link to a file elsewhere changed that file
too. A check made before the write (a resolved path, an lstat of the link
count) does not hold against a link swapped in after it.

The caller names a folder it trusts (the project's or the workspace's own)
and a path inside it. Each folder on the way is opened from the one before
with O_NOFOLLOW, so a link anywhere below the root is refused, not followed;
the bytes go to a fresh file beside the name, opened O_EXCL|O_NOFOLLOW in the
last folder's own descriptor, and os.replace renames it over the name. A
rename changes which inode the NAME refers to: it never writes through a hard
link (the other name keeps the old bytes) and never through a symbolic link.
A name that is a symbolic link, or a folder, is refused rather than replaced,
so a tracked link is never silently turned into a file. An existing file's
permission bits are kept (a project script stays executable); a new file is
0644, what shutil.copyfile gave under the kernel's umask.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Optional, Union

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: an open for reading of a named pipe waits for a writer, and it
# runs on the kernel's event loop. shutil.copy2, which these copies replace,
# refused a pipe before opening it; this opens at once and the S_ISREG check
# on the descriptor refuses it. A regular file reads the same either way.
_SRC_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CHUNK = 1 << 20


class LinkRefused(OSError):
    """The destination, or the folder it is in, is a link or not a plain file."""


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _open_parent(root: Path, parts: list[str], make_dirs: bool) -> int:
    """A descriptor for the folder that holds the last of `parts`, reached from
    `root` one name at a time, none of them followed if it is a link. `root`
    is the caller's own folder and is trusted; everything below it is not."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for seg in parts[:-1]:
            if make_dirs:
                try:
                    os.mkdir(seg, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(seg, _DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                raise LinkRefused(f"{seg!r} on the way to the file is a link or not a "
                                  f"folder ({e.strerror})") from None
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _parts(rel: str) -> list[str]:
    """The names in `rel`, read exactly as pathlib reads it, because every
    caller checked the path with pathlib (Path(rel).parts, resolve()) before
    calling: './x' is x, 'a//b' is a/b, and a backslash is a character of a
    name, as it is on Linux. Splitting on a backslash would write somewhere
    the caller never checked: `.git\\config` passes a .git check as ONE name
    and would land in .git/config."""
    s = str(rel)
    parts = list(PurePosixPath(s).parts)
    if (not parts or s.startswith("/")
            or any(p == ".." or "\0" in p for p in parts)):
        raise LinkRefused(f"{rel!r} is not a path inside the folder")
    return parts


def _replace(root: Path, rel: str, fill, make_dirs: bool, mode_from=None) -> None:
    """`fill(fd)` writes the bytes. `mode_from(fd)` (optional) names the mode
    the new file gets, over the old file's: the copy2-like copy's source mode."""
    parts = _parts(rel)
    name = parts[-1]
    dfd = _open_parent(Path(root), parts, make_dirs)
    tmp = None
    try:
        mode = 0o644
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise LinkRefused(f"{rel} is a link; not written through it")
            if not stat.S_ISREG(st.st_mode):
                raise LinkRefused(f"{rel} is not a plain file")
            # rwx bits only: a set-user-ID or set-group-ID bit on the old
            # file must not land on a new inode the kernel owns, holding
            # bytes it was handed.
            mode = stat.S_IMODE(st.st_mode) & 0o777
        tmp = f".{name[:100]}.{secrets.token_hex(6)}.new"
        fd = os.open(tmp, _NEW_FLAGS, mode, dir_fd=dfd)
        try:
            fill(fd)
            if mode_from is not None:
                mode = mode_from(fd)
            os.fchmod(fd, mode)   # the umask applied at create; the old file's bits win
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        os.close(dfd)


def replace_file(root: Path, rel: str, data: Union[bytes, str], *, make_dirs: bool = True) -> None:
    """Make `root/rel` hold `data`, as a new inode renamed over the name, with
    no link followed below `root`."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    _replace(root, rel, lambda fd: _write_all(fd, raw), make_dirs)


def open_no_follow(root: Path, rel: str) -> int:
    """A read descriptor for `root/rel`, reached from `root` with no link
    followed on any part of the way (a folder swapped for a link after a
    caller's resolve() check is refused, not read through)."""
    parts = _parts(rel)
    dfd = _open_parent(Path(root), parts, make_dirs=False)
    try:
        return os.open(parts[-1], _SRC_FLAGS, dir_fd=dfd)
    finally:
        os.close(dfd)


def replace_file_from_fd(root: Path, rel: str, src_fd: int, *, make_dirs: bool = True) -> None:
    """Make `root/rel` hold what the open descriptor `src_fd` reads from its
    current position, streamed a chunk at a time, as a new inode renamed over
    the name. The caller keeps and closes `src_fd`."""
    def fill(fd: int) -> None:
        while chunk := os.read(src_fd, _CHUNK):
            _write_all(fd, chunk)
    _replace(root, rel, fill, make_dirs)


def replace_file_from(root: Path, rel: str, src: Path, *, make_dirs: bool = True,
                      like_copy2: bool = False, src_root: Optional[Path] = None) -> None:
    """Make `root/rel` hold the bytes of the file `src`, streamed a chunk at a
    time, as a new inode renamed over the name. `src` itself is opened without
    following a link. like_copy2: the new file takes the source's permission
    bits (rwx only) and its access and modification times, as shutil.copy2
    gave the callers it replaces (an adopted script stays executable).
    src_root: `src` is a path relative to it, opened with no link followed on
    any part of the way (open_no_follow), not only its last name."""
    src_st: list = []

    def fill(fd: int) -> None:
        sfd = open_no_follow(src_root, str(src)) if src_root is not None else os.open(src, _SRC_FLAGS)
        try:
            st = os.fstat(sfd)
            if not stat.S_ISREG(st.st_mode):
                raise LinkRefused(f"{src} is not a plain file")
            src_st.append(st)
            while chunk := os.read(sfd, _CHUNK):
                _write_all(fd, chunk)
        finally:
            os.close(sfd)
        if like_copy2:
            os.utime(fd, ns=(src_st[0].st_atime_ns, src_st[0].st_mtime_ns))

    def mode_from(fd: int) -> int:
        return stat.S_IMODE(src_st[0].st_mode) & 0o777

    _replace(root, rel, fill, make_dirs, mode_from if like_copy2 else None)
