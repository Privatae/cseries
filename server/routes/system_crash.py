"""/api/system/crash — the Host's view of its own crash artifacts.

Reached by the Host through `kernel_op` (brain/mastercode/runtime/kernel_ops.py),
which is why this is a kernel route and not a hand-written meta-tool: the generic
bridge already mints a scoped app token and re-checks the scope against the
entity's CEIGAS bits, so a new capability costs one prefix in _EXPOSED_PREFIXES
instead of a new tool in four registries.

SCOPES ARE DELIBERATELY THE ORDINARY FILE ONES. `read:files` (bit 1) and
`write:files` (bit 4) are already in ENTITY_SCOPE_TO_BITS — a crash dump sitting
in the entity's own volume is the entity's own file, and inventing a
`read:crash` scope would have meant extending the entity vocabulary for
something the vocabulary already covers.

METADATA ONLY, AND THAT IS STRUCTURAL. GET returns name/size/mtime/component.
There is no read/download/tail route here and there must never be one: the dump
is a full process memory image, the platform can never look at it, and a route
that returned bytes would put it on the wire under a platform-minted token. If
the user wants the contents examined, their Host reads the file in-pod — that
read never crosses the bridge. scripts/smoke_test.sh gates on the absence of a
content-read function in server/crash.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from server import crash
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.pc.system_crash")

router = APIRouter(prefix="/api/system/crash", tags=["system"])


@router.get("", dependencies=[Depends(requires_permission("read:files"))])
async def list_crash_artifacts() -> dict:
    """Every crash dump in this pod, newest first. Names and sizes only."""
    dumps = crash.list_dumps()
    return {
        "dumps": dumps,
        "count": len(dumps),
        "total_bytes": sum(d["bytes"] for d in dumps),
        "retain_max_bytes": crash.RETAIN_MAX_BYTES,
        # Said in the payload, not just in a doc, because the Host renders this
        # to the user and the user's first question is "who else has seen it".
        "note": (
            "These are yours. The platform never reads, backs up or ships a "
            "crash dump — it is excluded from the backup by name and by path. "
            "Keep one for your own troubleshooting or delete it here."
        ),
    }


@router.delete("", dependencies=[Depends(requires_permission("write:files"))])
async def delete_crash_artifact(request: Request) -> dict:
    """Delete one dump by name. The user's call, made through their Host."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    name = str((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    if not crash.delete(name):
        # Same answer for "not a dump name", "traversal attempt" and "gone":
        # the caller learns nothing about the filesystem from a failed delete.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such crash artifact: {name}")
    remaining = crash.list_dumps()
    return {"deleted": name, "remaining": len(remaining)}
