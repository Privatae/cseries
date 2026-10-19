"""
Portal routes — /api/portal/*

List the Portal's sources, read one, and remove one. Adding a folder source
was removed on 2026-10-01 with the CEIGAS incident lane it fed (see
server/portal/sources.py): nothing called it, and no live Host held a source,
an incident or a granted rule. Deleting stays so a legacy entry can go.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Depends
from server.applications.middleware import requires_permission

from server.portal import sources as sources_mod

logger = logging.getLogger("privatae.pc.routes.portal")

router = APIRouter(prefix="/api/portal", tags=["portal"])


@router.get("/sources", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def list_sources():
    """List all Portal sources (pending + active + revoked)."""
    return {"sources": sources_mod.list_sources()}


@router.delete("/sources/{source_id}", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def delete_source(source_id: str):
    """Remove a Portal source entry. The user removes the compose.yml
    bind-mount themselves."""
    existing = next((s for s in sources_mod.list_sources() if s["id"] == source_id), None)
    if not existing:
        raise HTTPException(status_code=404, detail="source not found")

    sources_mod.delete(source_id)

    try:
        from server.engine import rules as rules_engine
        rules_engine.emit_local_signal("portal_source_revoked", {
            "source_id": source_id,
            "source_display": existing.get("display_name", source_id),
            "source_kind": existing.get("type", "folder"),
        }, content=f"revoked source '{existing.get('display_name', source_id)}'")
    except Exception as e:
        logger.debug("portal_source_revoked emit failed: %s", e)

    return {"deleted": source_id}


@router.get("/sources/{source_id}", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def get_source(source_id: str):
    src = next((s for s in sources_mod.list_sources() if s["id"] == source_id), None)
    if not src:
        raise HTTPException(status_code=404, detail="source not found")
    return src
