"""Browser app: stay signed in, or don't — the user's switch over their profile.

Three routes over server/applications/browser_profile.py. The surface is
deliberately tiny, because there are exactly two honest things to expose: whether
the browser keeps the user signed in, and a way to stop. There is NO route that
returns the profile, and there must never be one — those bytes are bearer
credentials for the user's bank, and a read endpoint is somewhere to read them
from.

Gated on the vault permissions rather than a browser-specific scope: turning this
on means the host will seal a credential on the user's behalf, and signing out
revokes one. That is what write:vault already means.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import brain_service
from server.applications import browser_profile
from server.applications.middleware import requires_permission

router = APIRouter(prefix="/api/browser/profile", tags=["browser-profile"])


class EnabledBody(BaseModel):
    enabled: bool


def _pool_or_503():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool not ready")
    return pool


@router.get("", dependencies=[Depends(requires_permission("read:vault"))])
async def get_status() -> dict:
    """{enabled, kept} — whether keeping is on, and whether anything is kept.

    Never the contents. `kept` answers "am I still signed in to things", which is
    what the user is actually asking."""
    return await browser_profile.status(_pool_or_503(), brain_service._entity_id)


@router.put("", dependencies=[Depends(requires_permission("write:vault"))])
async def set_enabled(body: EnabledBody) -> dict:
    """Turn "stay signed in" on or off.

    Turning it OFF also forgets what is stored — a switch that leaves the sealed
    credential behind it is not an off switch."""
    pool = _pool_or_503()
    eid = brain_service._entity_id
    await browser_profile.set_enabled(pool, eid, bool(body.enabled))
    return await browser_profile.status(pool, eid)


@router.post("/sign-out", dependencies=[Depends(requires_permission("write:vault"))])
async def sign_out() -> dict:
    """Sign out everywhere: clear the live browser context, then revoke the sealed
    copy. Leaves the switch ON — "sign me out now" and "stop keeping me signed in"
    are different requests, and collapsing them would surprise the user who only
    asked for the first."""
    pool = _pool_or_503()
    eid = brain_service._entity_id
    if not await browser_profile.forget(pool, eid):
        raise HTTPException(
            503, "could not clear the live browser, so nothing was revoked — your "
                 "sign-ins are untouched. Try again in a moment.")
    return await browser_profile.status(pool, eid)
