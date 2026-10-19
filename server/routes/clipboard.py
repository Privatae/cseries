"""
Clipboard routes — read and write system clipboard.

CEIGAS domain: device.filesystem (clipboard is device data).
"""

import logging

from fastapi import APIRouter, HTTPException, Depends
from server.applications.middleware import requires_permission
from pydantic import BaseModel

from server.capabilities import clipboard as _clipboard
read_clipboard = _clipboard.read_clipboard
write_clipboard = _clipboard.write_clipboard

logger = logging.getLogger("privatae.pc.routes.clipboard")

router = APIRouter(prefix="/api/clipboard", tags=["clipboard"])


class ClipboardWriteRequest(BaseModel):
    content: str


@router.get("/read", dependencies=[Depends(requires_permission("read:clipboard"))])
async def clipboard_read():
    """Read current clipboard contents."""
    try:
        result = read_clipboard()
    except Exception as e:
        logger.error("Clipboard read route error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))

    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result.get("error", "clipboard read failed"))

    return result


@router.post("/write", dependencies=[Depends(requires_permission("write:clipboard"))])
async def clipboard_write(req: ClipboardWriteRequest):
    """Write content to system clipboard."""
    try:
        result = write_clipboard(content=req.content)
    except Exception as e:
        logger.error("Clipboard write route error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))

    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result.get("error", "clipboard write failed"))

    return result
