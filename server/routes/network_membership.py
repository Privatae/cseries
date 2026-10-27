"""Network membership routes for Settings -> Network.

  GET  /api/network/status  what the panel shows: member or not, the network's
                            id for this Host, the network, until when the
                            passport is valid, the connection, the last error,
                            and whether the network froze the Host (frozen,
                            frozen_note: what happened and what to do)
  POST /api/network/join    {code, host_name?}: enrol with a one-time code
  POST /api/network/leave   revoke the passport and delete it

The logic is server/bridge/membership.py. Joining and leaving are the owner's
acts: each takes the write scope, which only the Settings app declares, the
Settings app's own token, and a live owner session on the request (the
sign-in cookie, server/owner_login.py). A process inside the container holds
no session, so the Host cannot join or leave on its own, and neither scope
maps to any capability the Host's tools can hold (meta_tool_caps: an unmapped
scope is no capability).

One join is not made here: on a C-Series Cloud machine the owner's own press
of "Set up your machine", signed in at privatae.ai, sends a code with the
claim, and the Host joins with it once that owner is its owner
(membership.join_from_claim). That press is the owner's consent; the Host
never makes a code, and has no way to.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server.applications.middleware import requires_permission
from server.bridge import membership
from server.middleware.owner_session import live_session

router = APIRouter(prefix="/api/network", tags=["network"])

OWNER_APP_IDS = ("settings",)


async def owner_only(request: Request) -> None:
    tok = getattr(request.state, "app_token", None)
    if getattr(tok, "app_id", None) not in OWNER_APP_IDS:
        raise HTTPException(403, "only the owner can change network membership, "
                                 "in Settings -> Network")
    email, _token = await live_session(request.scope)
    if not email:
        raise HTTPException(403, "sign in as the owner to change network membership")


class JoinBody(BaseModel):
    code: str
    host_name: Optional[str] = None


def _fail(e: membership.MembershipError) -> HTTPException:
    return HTTPException(e.status, e.message)


@router.get("/status", dependencies=[Depends(requires_permission("read:network_membership"))])
async def status():
    return membership.current().status()


@router.post("/join", dependencies=[Depends(requires_permission("write:network_membership")),
                                    Depends(owner_only)])
async def join(body: JoinBody):
    name = (body.host_name or "").strip()[:120] or None
    try:
        return await membership.current().join(body.code, host_name=name)
    except membership.MembershipError as e:
        raise _fail(e) from None


@router.post("/leave", dependencies=[Depends(requires_permission("write:network_membership")),
                                     Depends(owner_only)])
async def leave():
    try:
        return await membership.current().leave()
    except membership.MembershipError as e:
        raise _fail(e) from None
