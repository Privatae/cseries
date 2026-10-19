"""Path-scoped filesystem operations for the FSA polyfill.

Every call enforces that the resolved absolute path stays within the
session's granted root. The granted root is set via the control API's
POST /grant-fs and held in the GrantState singleton.

The polyfill JS calls these via Playwright's expose_binding bridge —
no HTTP, no CORS, no cross-origin headaches. Each binding becomes a
window-scoped JS function the polyfill awaits, and the call lands here
synchronously (Python-side) with full path enforcement.

Errors are raised with PolyfillFSError so the JS side gets typed
errors it can convert to DOMExceptions matching the WICG spec.
"""
from __future__ import annotations

import base64
import dataclasses
import logging
import os
import stat as _stat
from pathlib import Path
from typing import Optional

log = logging.getLogger("cseries-browser.fs_api")


class PolyfillFSError(Exception):
    """Raised when a polyfill FS call violates scope, hits a missing
    path, or fails I/O. The JS side maps to DOMException.

    The string form is `"{code}: {message}"` because Playwright's
    expose_binding propagates the exception to JS via its message
    field. The polyfill's _throwFromError parses this format to
    populate DOMException.name (so handle methods can detect
    NotFoundError to take the create-on-missing branch)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclasses.dataclass
class _Grant:
    """The current scope. None when nothing is granted."""

    root: Optional[Path] = None
    name: Optional[str] = None  # display name returned to JS handle


# Module-level singleton. v1 supports one grant at a time. Per-session
# grants get added in Ship 4 alongside multi-context support.
grant_state = _Grant()


def grant(path_str: str, name: Optional[str] = None) -> dict:
    """Set the granted root. Path must exist and be a directory.

    Called from control.app on POST /grant-fs. Subsequent polyfill
    operations are scoped to this root.
    """
    p = Path(path_str).resolve()
    if not p.exists():
        raise PolyfillFSError("NotFoundError", f"grant path does not exist: {p}")
    if not p.is_dir():
        raise PolyfillFSError("TypeMismatchError", f"grant path is not a directory: {p}")
    grant_state.root = p
    grant_state.name = name or p.name
    log.info("granted FS root: %s (name=%s)", p, grant_state.name)
    return {"path": str(p), "name": grant_state.name}


def revoke() -> dict:
    """Clear the grant. Polyfill calls fail with NotAllowedError after this."""
    prev = grant_state.root
    grant_state.root = None
    grant_state.name = None
    log.info("revoked FS grant (was: %s)", prev)
    return {"revoked": True}


def current_grant() -> dict:
    """Polyfill calls this to learn the granted root + name."""
    if grant_state.root is None:
        return {"granted": False}
    return {
        "granted": True,
        "path": str(grant_state.root),
        "name": grant_state.name,
    }


def _resolve(relative_path: str) -> Path:
    """Resolve relative_path against the granted root and enforce containment.

    Throws PolyfillFSError if no grant is set, or if the resolved path
    escapes the granted root (via .. components, absolute paths, or
    symlinks pointing outside).
    """
    if grant_state.root is None:
        raise PolyfillFSError("NotAllowedError", "no FS grant active")
    root = grant_state.root
    rel = (relative_path or "").lstrip("/")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PolyfillFSError(
            "SecurityError",
            f"path escapes granted root: {candidate} not under {root}",
        )
    return candidate


def list_entries(relative_path: str) -> list[dict]:
    """Return [{name, kind}] for entries directly under relative_path."""
    target = _resolve(relative_path or "")
    if not target.exists():
        raise PolyfillFSError("NotFoundError", f"directory does not exist: {target}")
    if not target.is_dir():
        raise PolyfillFSError("TypeMismatchError", f"not a directory: {target}")
    entries = []
    for child in sorted(target.iterdir(), key=lambda c: c.name):
        try:
            child_stat = child.lstat()
        except OSError:
            continue
        if _stat.S_ISLNK(child_stat.st_mode):
            # Skip symlinks — they could point outside the grant. v2
            # can opt-in to symlink resolution with a re-containment
            # check.
            continue
        kind = "directory" if child.is_dir() else "file"
        entries.append({"name": child.name, "kind": kind})
    return entries


def stat_entry(relative_path: str) -> dict:
    """Return {name, kind, size, last_modified_ms} for a single entry."""
    target = _resolve(relative_path)
    if not target.exists():
        raise PolyfillFSError("NotFoundError", f"does not exist: {target}")
    st = target.lstat()
    if _stat.S_ISLNK(st.st_mode):
        raise PolyfillFSError("SecurityError", f"symlink not supported: {target}")
    return {
        "name": target.name,
        "kind": "directory" if target.is_dir() else "file",
        "size": st.st_size if target.is_file() else 0,
        "last_modified_ms": int(st.st_mtime * 1000),
    }


def read_file(relative_path: str) -> dict:
    """Read a file's bytes, return base64-encoded for JSON transport."""
    target = _resolve(relative_path)
    if not target.exists():
        raise PolyfillFSError("NotFoundError", f"file does not exist: {target}")
    if not target.is_file():
        raise PolyfillFSError("TypeMismatchError", f"not a file: {target}")
    data = target.read_bytes()
    st = target.stat()
    return {
        "name": target.name,
        "size": len(data),
        "last_modified_ms": int(st.st_mtime * 1000),
        # base64 keeps the payload JSON-safe + preserves binary content.
        # Playwright expose_binding serializes return values as JSON,
        # so this is the simplest cross-language transport.
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


def write_file(relative_path: str, data_b64: str, create: bool = True) -> dict:
    """Write bytes to a file. Creates parent dirs implicitly (within scope).

    `create` mirrors the FSA getFileHandle{create:true} flag — when
    false and the file doesn't exist we error.
    """
    target = _resolve(relative_path)
    if target.exists() and target.is_dir():
        raise PolyfillFSError("TypeMismatchError", f"path is a directory: {target}")
    if not target.exists() and not create:
        raise PolyfillFSError("NotFoundError", f"file does not exist: {target}")
    parent = target.parent
    if not parent.exists():
        if not create:
            raise PolyfillFSError("NotFoundError", f"parent does not exist: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
    try:
        data = base64.b64decode(data_b64.encode("ascii"))
    except Exception as exc:
        raise PolyfillFSError("InvalidStateError", f"bad base64: {exc}")
    target.write_bytes(data)
    return {
        "name": target.name,
        "size": len(data),
    }


def mkdir(relative_path: str, create_parents: bool = True) -> dict:
    """Ensure a directory exists. `create_parents` mirrors mkdir -p."""
    target = _resolve(relative_path)
    if target.exists() and not target.is_dir():
        raise PolyfillFSError("TypeMismatchError", f"path is a file: {target}")
    target.mkdir(parents=create_parents, exist_ok=True)
    return {"name": target.name, "kind": "directory"}


def remove_entry(relative_path: str, recursive: bool = False) -> dict:
    """Delete a file, or a directory if recursive=true.

    Mirrors `removeEntry(name, {recursive})` on FileSystemDirectoryHandle.
    """
    target = _resolve(relative_path)
    if not target.exists():
        raise PolyfillFSError("NotFoundError", f"does not exist: {target}")
    if target.is_dir():
        if recursive:
            import shutil
            shutil.rmtree(target)
        else:
            # Non-recursive on non-empty dir → InvalidModificationError per spec.
            try:
                target.rmdir()
            except OSError as exc:
                raise PolyfillFSError(
                    "InvalidModificationError",
                    f"directory not empty (pass recursive=true): {exc}",
                )
    else:
        target.unlink()
    return {"removed": str(target)}
