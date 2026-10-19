"""The Host's own database, for apps.

  POST /api/world/sql       {sql, params?, timeout_s?}   run SQL, one transaction
  GET  /api/world/describe  ?name=                        what is in `world`
  GET  /api/world/rows      ?name=&limit=&offset=         a table's rows, read-only
  GET  /api/world/history                                 the snapshots
  POST /api/world/restore   {snapshot}                    put `world` back

Every one runs through mastercode/world_db on the host_world lane: the same
role, purpose check, snapshots and limits as the Host's `data` tool. One scope,
use:world. It is not in build_contract.COMMUNITY_ALLOWED_SCOPES: this is the
Host's own data, for the Host's own apps and the Memory app's Data tab. For a
Host-built (`custom`) app a scope is a declaration, not a wall (routes/
applications.py on same-origin frames), which is acceptable because the app is
the Host's own.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from server.applications.middleware import requires_permission

router = APIRouter(prefix="/api/world", tags=["world"])
_SCOPE = "use:world"


class WorldSql(BaseModel):
    sql: str = Field(..., min_length=1, max_length=200_000)
    params: list[str] = Field(default_factory=list)
    timeout_s: int | None = Field(None, ge=1, le=900)


class WorldRestore(BaseModel):
    snapshot: str = Field(..., min_length=1, max_length=64)


@router.post("/sql", dependencies=[Depends(requires_permission(_SCOPE))])
async def world_sql(body: WorldSql) -> dict:
    from mastercode import world_db
    return await world_db.run_sql(body.sql, list(body.params), body.timeout_s)


@router.get("/describe", dependencies=[Depends(requires_permission(_SCOPE))])
async def world_describe(name: str | None = Query(None, max_length=200)) -> dict:
    from mastercode import world_db
    return await world_db.describe(name)


@router.get("/rows", dependencies=[Depends(requires_permission(_SCOPE))])
async def world_rows(name: str = Query(..., min_length=1, max_length=200),
                     limit: int = Query(100, ge=1, le=500),
                     offset: int = Query(0, ge=0)) -> dict:
    from mastercode import world_db
    return await world_db.rows(name, limit, offset)


@router.get("/history", dependencies=[Depends(requires_permission(_SCOPE))])
async def world_history() -> dict:
    from mastercode import world_db
    return world_db.history()


@router.post("/restore", dependencies=[Depends(requires_permission(_SCOPE))])
async def world_restore(body: WorldRestore) -> dict:
    from mastercode import world_db
    from server import brain_service
    if brain_service.get_world_pool() is None:
        return world_db._unavailable()
    return await asyncio.to_thread(world_db.restore, body.snapshot, brain_service.world_dsn())
