"""Reach-out — the kernel routes behind the Circles pane.

Thin wrappers over server/reachout/service.py, which is the same module the
`reachout` host tool calls. One set of rules, two callers: the tool and the
pane can therefore never disagree about what a channel is allowed to be.

The pane itself (Circles → Settings → "How your Host reaches you", design
decision 4) is NOT in this PR — these are the routes it will call, and they
are useful on their own to anyone reading a host's reach-out state.

Scope split, matching the rest of the kernel: reading the configuration and
the log is `read:reachout`; deleting a channel or a trigger, and firing a test,
are `write:reachout`. Creating is deliberately absent — building a channel is a
conversation with the host (design: "a settings surface is never a config form
for what the host does"), so it happens through the tool.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from server import brain_service
from server.applications.middleware import requires_permission
from server.reachout import service

logger = logging.getLogger("privatae.reachout.routes")

router = APIRouter(prefix="/api/reachout", tags=["reachout"])


def _ctx():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "pool not ready")
    eid = brain_service._entity_id
    if not eid:
        raise HTTPException(503, "host id unknown")
    return pool, int(eid)


@router.get("", dependencies=[Depends(requires_permission("read:reachout"))])
async def get_reachout(limit: int = Query(20, ge=1, le=200),
                       offset: int = Query(0, ge=0)) -> dict:
    """Channels, triggers and the recent log — everything the pane renders.
    `limit`/`offset` page the log; recent_next_offset reads on."""
    pool, eid = _ctx()
    return {"ok": True, **(await service.list_state(pool, eid, log_limit=limit,
                                                    log_offset=offset))}


@router.delete("/channels/{channel_id}",
               dependencies=[Depends(requires_permission("write:reachout"))])
async def delete_channel(channel_id: int) -> dict:
    """Delete a channel and, by cascade, its triggers.

    actor='user': this route IS the user's hands, so it removes a row the host
    created as readily as one the user did. The reverse asymmetry — the host
    refusing to delete the user's row — lives in the service and applies to the
    tool.
    """
    pool, eid = _ctx()
    try:
        return {"ok": True, **(await service.remove_channel(pool, eid, channel_id,
                                                            actor="user"))}
    except service.ReachoutError as e:
        raise HTTPException(404, str(e))


@router.delete("/triggers/{trigger_id}",
               dependencies=[Depends(requires_permission("write:reachout"))])
async def delete_trigger(trigger_id: int) -> dict:
    pool, eid = _ctx()
    try:
        return {"ok": True, **(await service.remove_trigger(pool, eid, trigger_id,
                                                            actor="user"))}
    except service.ReachoutError as e:
        raise HTTPException(404, str(e))


@router.post("/channels/{channel_id}/test",
             dependencies=[Depends(requires_permission("write:reachout"))])
async def test_channel(channel_id: int) -> dict:
    """Fire one channel once with a synthetic message, and say what happened."""
    pool, eid = _ctx()
    try:
        res = await service.test_fire(pool, eid, channel_id)
    except service.ReachoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": res.get("status") == "sent", **res}
