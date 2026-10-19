"""Contact cards for other Hosts — server/hosts/card.py.

  GET  /api/hosts/{host_id}/card   one card
  POST /api/hosts/cards            {"ids": [..]} -> {"cards": {id: card}}

Read-only, from this Host's own records; read:address_book, the permission
the Contacts pane already reads under.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission
from server.hosts.card import MAX_IDS, host_cards

router = APIRouter(prefix="/api/hosts", tags=["hosts"])


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "host not ready")
    return pool


class CardsIn(BaseModel):
    ids: list[int]


@router.post("/cards", dependencies=[Depends(requires_permission("read:address_book"))])
async def cards(body: CardsIn) -> dict:
    if len(body.ids) > MAX_IDS:
        raise HTTPException(400, f"at most {MAX_IDS} hosts per request")
    got = await host_cards(_pool(), int(brain_service._entity_id), body.ids)
    return {"cards": {str(k): v for k, v in got.items()}}


@router.get("/{host_id}/card", dependencies=[Depends(requires_permission("read:address_book"))])
async def card(host_id: int) -> dict:
    got = await host_cards(_pool(), int(brain_service._entity_id), [host_id])
    if host_id not in got:
        raise HTTPException(404, "no card: that is this host itself, or not a host id")
    return got[host_id]
