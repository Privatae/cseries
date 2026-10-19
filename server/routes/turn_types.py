"""Turn types — the read surface the Circles dropdown uses.

One route: the entity's ACTIVE types with the thread each opens. Nothing
here creates, activates or widens a type — the `turn_type` tool and its
platform-filed card are the only writers, and only the
ticket-decide route sets status. A Host with no types answers an empty
list, and the dropdown shows nothing.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from server import brain_service
from server.applications.middleware import requires_permission

log = logging.getLogger("cseries.routes.turn_types")

router = APIRouter(prefix="/api/turn-types", tags=["turn-types"])

_READ = [Depends(requires_permission("read:circles"))]


def _entity_id() -> int | None:
    return getattr(brain_service, "_entity_id", None)


@router.get("", dependencies=_READ)
async def list_turn_types() -> dict:
    """The active types, name order: {id, name, thread_id, skill, tool_groups,
    keyword_count}. Keywords themselves are not returned — the list is for
    a picker, and a type's vocabulary is not something to page through."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Brain not ready")
    eid = _entity_id() or 0
    from mastercode.runtime.turn_types import active_types
    types = await active_types(pool, eid)
    return {"types": [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "thread_id": t.get("thread_id"),
            "skill": t.get("skill"),
            "tool_groups": list(t.get("tool_groups") or []),
            "keyword_count": len(t.get("keywords") or []),
        }
        for t in types
    ]}
