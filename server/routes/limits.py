"""The person's limits (runtime/user_limits.py), for the Settings app's Limits
page.

GET lists every limit: what it limits, the value in force, who set it, the
default, what happens when it binds and when it last did. PUT sets one.

Only the person writes a limit. The scope (write:session) is one a community
app may declare (build_contract.COMMUNITY_ALLOWED_SCOPES), so it is not what
keeps an app or the Host out: the caller's app id is. A PUT is taken only from
the person's own surfaces (Settings, and the Circles / Workbench chats, the
same set routes/tickets.PERSON_SURFACES trusts to decide a user_only card).
The Host's kernel_op cannot reach /api/limits at all (kernel_ops
_EXPOSED_PREFIXES does not list it), and its `limits` tool only reads and
files a card.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from server import brain_service
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.routes.limits")

router = APIRouter(prefix="/api/limits", tags=["limits"])

# routes/tickets.PERSON_SURFACES plus the Settings app, where the Limits page is.
PERSON_WRITERS = frozenset({"settings", "circles", "workbench"})


def _person(request: Request) -> bool:
    tok = getattr(request.state, "app_token", None)
    return str(getattr(tok, "app_id", "") or "") in PERSON_WRITERS


def _limits():
    from mastercode.runtime import user_limits
    return user_limits


@router.get("", dependencies=[Depends(requires_permission("read:session"))])
async def list_limits():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    ul = _limits()
    eid = brain_service._entity_id
    bp = await ul.read_bp(pool, eid)
    return {"limits": ul.status(bp, await ul.last_hits(pool, eid)),
            "groups": [{"id": g, "label": label} for g, label in ul.GROUPS]}


@router.put("/{name}", dependencies=[Depends(requires_permission("write:session"))])
async def set_limit(name: str, request: Request):
    """Body {"value": n} sets the limit (0 = no limit); {"value": null} puts
    the default back."""
    if not _person(request):
        raise HTTPException(403, "only the person changes a limit, from Settings; "
                                 "the Host can propose one with limits(op='propose')")
    ul = _limits()
    if name not in ul.LIMITS:
        raise HTTPException(404, f"no limit named {name!r}")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "body must be JSON: {\"value\": <number or null>}")
    if not isinstance(body, dict) or "value" not in body:
        raise HTTPException(400, "body must be {\"value\": <number or null>}")
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    res = await ul.write(pool, brain_service._entity_id, name, body["value"])
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "not a valid value")
    return res
