"""Phone-number alias routes — your host's human-friendly identifier.

A phone alias is a user-chosen, phone-number-style handle for THIS host,
used so other hosts can address you by a friendly number instead of a
random handle. It is NOT telephony and NOT public DNS — it lives in the
platform handle-registry (kind='phone') and resolves identity-only.

These routes proxy the caller-scoped bridge ops:
  GET    /api/phone-alias         → phone.alias.get
  PUT    /api/phone-alias         → phone.alias.set
  DELETE /api/phone-alias         → phone.alias.remove
  POST   /api/phone-alias/lookup  → phone.alias.lookup (identity-only)

The bridge fixes entity_id from the mTLS cert — a host only ever sets or
reads its OWN alias.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from server.applications.middleware import requires_permission

router = APIRouter(prefix="/api/phone-alias", tags=["phone-alias"])


async def _bridge(op: str, params: dict) -> dict:
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"bridge: {e}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "bridge not connected")
    try:
        envelope = await bc.request_full(op, params)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"bridge unreachable: {e}")
    if not envelope.get("ok"):
        raise HTTPException(int(envelope.get("code") or 502),
                            envelope.get("error") or "alias op failed")
    return envelope.get("data") or {}


@router.get("", dependencies=[Depends(requires_permission("read:address_book"))])
async def get_alias() -> dict:
    return await _bridge("phone.alias.get", {})


class AliasBody(BaseModel):
    alias: str


@router.put("", dependencies=[Depends(requires_permission("write:address_book"))])
async def set_alias(body: AliasBody) -> dict:
    return await _bridge("phone.alias.set", {"alias": body.alias})


@router.delete("", dependencies=[Depends(requires_permission("write:address_book"))])
async def remove_alias() -> dict:
    return await _bridge("phone.alias.remove", {})


@router.post("/lookup", dependencies=[Depends(requires_permission("read:address_book"))])
async def lookup_alias(body: AliasBody) -> dict:
    """Resolve a phone number → host identity (display name + uuid). Returns
    no network endpoint — you learn WHO it is, then send a contact request."""
    return await _bridge("phone.alias.lookup", {"alias": body.alias})
