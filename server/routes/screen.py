"""
Screen Capture routes — capture the user's screen.

CEIGAS domain: device.filesystem (reading screen data).
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from server.applications.middleware import requires_permission
from pydantic import BaseModel

from server.capabilities import screen_capture as _sc
capture_screen = _sc.capture_screen

logger = logging.getLogger("privatae.pc.routes.screen")

router = APIRouter(prefix="/api/screen", tags=["screen"])


class RegionModel(BaseModel):
    left: int = 0
    top: int = 0
    width: int = 1920
    height: int = 1080


class CaptureRequest(BaseModel):
    quality: int = 60
    monitor: int = 0
    region: Optional[RegionModel] = None


@router.post("/capture", dependencies=[Depends(requires_permission("read:screen"))])
async def capture(req: CaptureRequest):
    """Capture a screenshot. Returns base64 JPEG."""
    try:
        region_dict = req.region.model_dump() if req.region else None
        result = capture_screen(
            quality=req.quality,
            monitor=req.monitor,
            region=region_dict,
        )
    except Exception as e:
        logger.error("Screen capture route error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))

    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result.get("error", "capture failed"))

    return result
