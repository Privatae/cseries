"""
File Gateway — CEIGAS-governed file operations.

All paths sandboxed to /app/workspace and /app/user-files.
Path traversal blocked: .., ~, absolute paths, symlink escapes.
Reads and lists are paged (offset/length, offset/limit) with the true size or
total and the next offset; one read answer carries at most 10MB. Binary
support via base64.

Every operation goes through CEIGAS gate middleware (device.filesystem domain).
"""

import base64
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from server.applications.middleware import requires_permission
from pydantic import BaseModel

logger = logging.getLogger("privatae.pc.routes.files")

router = APIRouter(prefix="/api/files", tags=["files"])


def _emit(signal_type: str, metadata: dict) -> None:
    """Non-fatal signal emission helper."""
    try:
        from server.engine import rules as rules_engine
        rules_engine.emit_local_signal(signal_type, metadata)
    except Exception as e:
        logger.debug("signal emit failed: %s", e)

WORKSPACE = Path(os.environ.get("CEIGAS_WORKSPACE", "/app/workspace"))
USER_FILES = Path("/app/user-files")
# The most one /read answer carries: a PAGE, never a refusal. A file of any size is read in pages with offset/length;
# an answer that stops short says so (truncated, next_offset, the file's size).
# It bounds one JSON answer in the Host pod's memory (the bytes plus their
# base64 form for a binary page).
MAX_READ_BYTES = 10 * 1024 * 1024  # 10MB per page
# The default page of a /list or /index answer. Every answer reports the TRUE
# total and the next offset (RT-B11: `total` used to be the cut count, so the
# 501st entry was invisible and nothing said so).
MAX_LIST_ENTRIES = 500


def _page(items: list, offset: int, limit: Optional[int]) -> tuple[list, dict]:
    """One page of `items` and the paging fields every list answer carries."""
    offset = max(0, int(offset or 0))
    limit = MAX_LIST_ENTRIES if limit is None else max(1, int(limit))
    page = items[offset:offset + limit]
    nxt = offset + len(page)
    return page, {"total": len(items), "offset": offset, "limit": limit,
                  "returned": len(page),
                  "next_offset": nxt if nxt < len(items) else None,
                  "truncated": nxt < len(items)}


def _utf8_start(data: bytes) -> int:
    """Bytes to skip so `data` starts on a UTF-8 character boundary (an offset
    may land inside a multi-byte character)."""
    i = 0
    while i < min(3, len(data)) and (data[i] & 0xC0) == 0x80:
        i += 1
    return i


def _utf8_end(data: bytes) -> int:
    """Length of `data` cut back to end on a whole UTF-8 character."""
    n = len(data)
    for back in range(1, min(4, n) + 1):
        b = data[n - back]
        if (b & 0xC0) == 0x80:
            continue
        if b >= 0xC0:
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return n if back >= need else n - back
        return n
    return n


def _shared_roots() -> list[dict]:
    """Return active + mounted Portal sources as additional filesystem roots.

    Portal sources live under /app/portal/<slug>. A source becomes a
    real root only if (a) Portal has it in its source registry AND
    (b) the mount_path actually exists in the container (user added the
    docker-compose bind-mount and restarted).
    """
    try:
        from server.portal import sources as sources_mod
    except Exception:
        return []
    roots = []
    for s in sources_mod.list_sources():
        if s.get("type") != "folder":
            continue
        mount = s.get("mount_path") or ""
        if not mount:
            continue
        p = Path(mount)
        if not p.is_dir():
            continue
        roots.append({
            "id": s["id"],
            "prefix": f"portal/{s['id']}",
            "display_name": s.get("display_name", s["id"]),
            "mount_path": mount,
        })
    return roots


def _validate_path(requested: str) -> Path:
    """
    Validate and resolve a path within allowed directories.

    Blocks: .., ~, absolute paths, symlink escapes.
    Returns resolved absolute path guaranteed to be within sandbox.
    """
    if not requested:
        raise HTTPException(status_code=400, detail="path is required")

    parts = Path(requested).parts
    for part in parts:
        if part in ("..", "~") or part.startswith("/"):
            raise HTTPException(status_code=403, detail=f"path traversal blocked: {requested}")
        if "\x00" in part:
            raise HTTPException(status_code=400, detail="null byte in path")

    # Portal shared folders: "portal/<source_id>/..."
    if parts and parts[0] == "portal" and len(parts) >= 2:
        source_id = parts[1]
        for root in _shared_roots():
            if root["id"] == source_id:
                rel = Path(*parts[2:]) if len(parts) > 2 else Path(".")
                candidate = (Path(root["mount_path"]) / rel).resolve()
                mount_resolved = Path(root["mount_path"]).resolve()
                try:
                    candidate.relative_to(mount_resolved)
                    return candidate
                except ValueError:
                    pass
        raise HTTPException(status_code=403, detail=f"unknown portal source: {requested}")

    # Workspace default
    candidate = (WORKSPACE / requested).resolve()
    workspace_resolved = WORKSPACE.resolve()
    user_files_resolved = USER_FILES.resolve()

    try:
        candidate.relative_to(workspace_resolved)
        return candidate
    except ValueError:
        pass

    try:
        candidate_uf = (USER_FILES / requested).resolve()
        candidate_uf.relative_to(user_files_resolved)
        return candidate_uf
    except ValueError:
        pass

    raise HTTPException(status_code=403, detail=f"path resolves outside sandbox: {requested}")


@router.get("/roots", dependencies=[Depends(requires_permission("read:files"))])
async def list_roots():
    """Return all filesystem roots File Explorer can browse.

    Always includes the workspace. Appends Portal shared sources that
    are currently mounted + CEIGAS-approved. Sidebar renders one entry
    per root, rooted at its prefix (e.g. "portal/documents").
    """
    roots = [
        {"id": "workspace", "prefix": "", "display_name": "Workspace", "kind": "workspace"},
    ]
    for r in _shared_roots():
        roots.append({
            "id": r["id"],
            "prefix": r["prefix"],
            "display_name": r["display_name"],
            "kind": "shared",
        })
    return {"roots": roots}


@router.get("/list", dependencies=[Depends(requires_permission("read:files"))])
async def list_files(path: str = "", offset: int = 0, limit: Optional[int] = None):
    """List directory contents. Returns files and directories with metadata.

    Empty path => workspace root. Paths beginning with "portal/<id>" are
    routed to the Portal shared source's mount (must be both active in
    Portal and mounted in the container).
    """
    source_kind = "workspace"
    source_display = "Workspace"
    if path and path.startswith("portal/"):
        source_kind = "folder"
        target = _validate_path(path)
        # Resolve "portal/<id>" alone to the mount itself
        if target.is_dir() is False and target.exists() is False:
            # path was "portal/<id>" exactly — resolve to its root
            source_id = path.split("/")[1]
            for root in _shared_roots():
                if root["id"] == source_id:
                    target = Path(root["mount_path"]).resolve()
                    source_display = root.get("display_name", source_id)
                    break
        else:
            # Find source display from prefix
            source_id = path.split("/")[1]
            for root in _shared_roots():
                if root["id"] == source_id:
                    source_display = root.get("display_name", source_id)
                    break
    else:
        target = _validate_path(path) if path else WORKSPACE.resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail="directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="path is not a directory")

    _emit("portal_access" if source_kind == "folder" else "file_list", {
        "operation": "list",
        "path": path or "/",
        "source_kind": source_kind,
        "source_display": source_display,
    })

    # Paged (RT-B11): `total` is every entry in the directory, and
    # `next_offset` fetches the rest. Only the page is stat'ed.
    names = sorted(target.iterdir())
    page, paging = _page(names, offset, limit)
    entries = []
    for item in page:
        try:
            stat = item.stat()
            entries.append({
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": stat.st_size if item.is_file() else 0,
                "modified": stat.st_mtime,
            })
        except (PermissionError, OSError):
            entries.append({"name": item.name, "type": "unreadable",
                            "size": 0, "modified": None})

    return {"path": path or "/", "entries": entries, **paging}


@router.get("/read", dependencies=[Depends(requires_permission("read:files"))])
async def read_file(path: str, encoding: Optional[str] = "utf-8",
                    offset: int = 0, length: Optional[int] = None):
    """Read file content. Returns text (utf-8) or base64 (binary).

    A file of ANY size is read in pages (it used to refuse past 10 MB): `offset` (bytes) and `length` (bytes, at most MAX_READ_BYTES
    per answer) pick the range; only that range is read from disk. Every answer
    carries the file's `size`, the `offset` and `length` actually read, and
    `next_offset` (None at the end) — `truncated: true` whenever more follows.
    A utf-8 page starts and ends on whole characters, so `next_offset` always
    continues cleanly. `size` is the whole file's size (it was the bytes
    returned; the two are the same for a whole read)."""
    target = _validate_path(path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="path is not a file")

    size = target.stat().st_size
    offset = max(0, int(offset or 0))
    want = MAX_READ_BYTES if length is None else max(0, min(int(length), MAX_READ_BYTES))
    with open(target, "rb") as fh:
        fh.seek(offset)
        raw = fh.read(want)

    _emit(
        "portal_access" if path.startswith("portal/") else "file_read",
        {
            "operation": "read",
            "path": path,
            "size": size,
            "offset": offset,
            "source_kind": "folder" if path.startswith("portal/") else "workspace",
        },
    )

    def _answer(enc: str, content: str, start: int, nbytes: int) -> dict:
        end = start + nbytes
        more = end < size
        return {"path": path, "encoding": enc, "content": content, "size": size,
                "offset": start, "length": nbytes,
                "next_offset": end if more else None, "truncated": more}

    if encoding == "binary" or encoding is None:
        return _answer("base64", base64.b64encode(raw).decode("ascii"), offset, len(raw))

    skip = _utf8_start(raw) if (offset and encoding.lower().replace("-", "") == "utf8") else 0
    body = raw[skip:]
    if offset + len(raw) < size and encoding.lower().replace("-", "") == "utf8":
        cut = _utf8_end(body)
        if cut > 0:  # a page shorter than one character keeps its bytes
            body = body[:cut]
    try:
        text = body.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return _answer("base64", base64.b64encode(raw).decode("ascii"), offset, len(raw))

    return _answer(encoding, text, offset + skip, len(body))


class WriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"


@router.post("/write", dependencies=[Depends(requires_permission("write:files"))])
async def write_file(req: WriteRequest):
    """Write content to a file. Creates parent directories."""
    target = _validate_path(req.path)

    # Only allow writes to workspace (user-files is read-only)
    workspace_resolved = WORKSPACE.resolve()
    try:
        target.relative_to(workspace_resolved)
    except ValueError:
        raise HTTPException(status_code=403, detail="writes only allowed to workspace")

    target.parent.mkdir(parents=True, exist_ok=True)

    if req.encoding == "base64":
        data = base64.b64decode(req.content)
    else:
        data = req.content.encode(req.encoding)

    target.write_bytes(data)
    return {"path": req.path, "bytes_written": len(data)}


@router.delete("/delete", dependencies=[Depends(requires_permission("write:files"))])
async def delete_file(path: str):
    """Delete a file. Directories not supported (safety)."""
    target = _validate_path(path)

    workspace_resolved = WORKSPACE.resolve()
    try:
        target.relative_to(workspace_resolved)
    except ValueError:
        raise HTTPException(status_code=403, detail="deletes only allowed in workspace")

    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="cannot delete directories via API")

    target.unlink()
    return {"path": path, "deleted": True}


@router.get("/index", dependencies=[Depends(requires_permission("read:files"))])
async def file_index(offset: int = 0, limit: Optional[int] = None):
    """Container file index — what entity has surfaced to dashboard.

    Paged (RT-B11): `total` is every file in the workspace and `next_offset`
    fetches the rest. The walk already visited every file (it kept walking
    after the first 500 and only stopped listing them), so the true count
    costs nothing more; only the page is stat'ed."""
    workspace_resolved = WORKSPACE.resolve()
    if not workspace_resolved.exists():
        return {"workspace": str(WORKSPACE), "files": [], "total": 0, "offset": 0,
                "limit": MAX_LIST_ENTRIES if limit is None else limit,
                "returned": 0, "next_offset": None, "truncated": False}

    found = sorted(p for p in workspace_resolved.rglob("*") if p.is_file())
    page, paging = _page(found, offset, limit)
    files = []
    for item in page:
        try:
            rel = item.relative_to(workspace_resolved)
            stat = item.stat()
            files.append({
                "path": str(rel),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        except (PermissionError, OSError, ValueError):
            files.append({"path": os.path.relpath(item, workspace_resolved),
                          "size": 0, "modified": None, "unreadable": True})

    return {"workspace": str(WORKSPACE), "files": files, **paging}
