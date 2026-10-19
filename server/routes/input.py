"""
Input routes — mouse and keyboard actions.

CEIGAS domain: container.relay (controlling the machine).
"""

import logging
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Depends
from server.applications.middleware import requires_permission
from pydantic import BaseModel, field_validator

from server.capabilities import mouse_keyboard as _mkb
execute_action = _mkb.execute_action
ALLOWED_ACTIONS = getattr(_mkb, "ALLOWED_ACTIONS", {"move", "click", "type", "key", "scroll", "drag"})

logger = logging.getLogger("privatae.pc.routes.input")

router = APIRouter(prefix="/api/input", tags=["input"])


class ActionRequest(BaseModel):
    action: str
    params: Dict[str, Any] = {}

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ALLOWED_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(sorted(ALLOWED_ACTIONS))}")
        return v


@router.post("/action", dependencies=[Depends(requires_permission("write:input"))])
async def perform_action(req: ActionRequest):
    """Execute a mouse/keyboard action (click, move, type, key, scroll, drag)."""
    try:
        result = execute_action(action=req.action, params=req.params)
    except Exception as e:
        logger.error("Input action route error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error", "action failed"))

    return result
