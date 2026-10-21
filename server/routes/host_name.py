"""Naming the Host.

  POST /api/system/name   {name}: set the name the Host is called by

A new Host is called "C-Series" (server/local_identity.py) and nothing else
renames it, so this is the one place the owner chooses a name. The name lives
in entity_registry.display_name for this Host's own row, which every reader
already takes it from: the Host's identity prompt (synaptive_handler), the
taskbar and Settings (/api/system/status), the presence line
(/api/entity/state) and Circles.

Renaming is the owner's act, gated the way joining the network is
(routes/network_membership.owner_only): the request must carry Settings'
own token, the write scope, and a live owner sign-in. A process inside the
container holds no session, and write:host_name maps to no capability its
tools can hold.

Only an app served from its own origin may be on NAME_APP_IDS. A page on the
desktop's origin (a Host-built app, say) can launch any same-origin app with
no token and be handed that app's token, and the owner's sign-in cookie rides
along with its requests (routes/applications.launch_app). If a same-origin
app were allowed here, the Host could rename itself by building an app that
does it the next time the owner opens it. Settings is served from its own
origin, so only its own code ever holds its token. Getting Started is not, so
its Name step opens Settings at About instead of saving a name itself.
"""

from __future__ import annotations

import logging
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission
from server.middleware.owner_session import live_session

log = logging.getLogger("cseries.host_name")

router = APIRouter(prefix="/api/system", tags=["system"])

# The apps whose token may rename the Host: Settings, where the name is shown.
# Each must be served from its own origin (proxy.is_isolated), which
# scripts/test_host_name_route.py checks; see the module docstring for why.
# Kept apart from network_membership.OWNER_APP_IDS so either list can change
# without the other.
NAME_APP_IDS = ("settings",)

MAX_NAME_CHARS = 40


async def owner_only(request: Request) -> None:
    tok = getattr(request.state, "app_token", None)
    if getattr(tok, "app_id", None) not in NAME_APP_IDS:
        raise HTTPException(403, "only the owner can rename the Host, in Settings")
    email, _token = await live_session(request.scope)
    if not email:
        raise HTTPException(403, "sign in as the owner to rename the Host")


def clean_name(raw: object) -> str:
    """The name as stored: trimmed, 1 to MAX_NAME_CHARS characters, with no
    control or format characters (a newline in a name would end up inside the
    Host's identity prompt). Raises HTTPException(400) otherwise."""
    if not isinstance(raw, str):
        raise HTTPException(400, "name must be text")
    name = raw.strip()
    if not name:
        raise HTTPException(400, "give the Host a name")
    if len(name) > MAX_NAME_CHARS:
        raise HTTPException(400, f"a name is at most {MAX_NAME_CHARS} characters")
    # Cs: a lone surrogate, which a JSON body can carry ("\ud800") but which
    # is not text and cannot be stored as UTF-8 (the write would fail, a 500).
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in name):
        raise HTTPException(400, "a name cannot contain control characters")
    return name


class NameBody(BaseModel):
    name: str


@router.post("/name", dependencies=[Depends(requires_permission("write:host_name")),
                                    Depends(owner_only)])
async def set_name(body: NameBody):
    name = clean_name(body.name)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "the Host's database is not up yet")
    eid = brain_service._entity_id
    status = await pool.execute(
        "UPDATE entity_registry SET display_name = $1 WHERE id = $2", name, eid)
    if status != "UPDATE 1":
        raise HTTPException(404, "this Host has no identity row to rename")
    log.info("host %s renamed", eid)
    return {"ok": True, "name": name}
