"""Settings, Git server: connect the Host to a git server the person runs.

  GET    /api/git-server   what is connected, its last check, or the forge the
                           install set
  POST   /api/git-server   {url, username, token, person_username?}: check the
                           server and connect it
  DELETE /api/git-server   disconnect it

The logic is server/git_server.py. Connecting and disconnecting are the
owner's acts, gated the way renaming the Host and joining the network are
(routes/host_name.owner_only): the write scope, which only Settings declares,
Settings' own token, and a live owner sign-in on the request. A process inside
the container holds no session, and write:git_server maps to no capability the
Host's tools can hold, so the Host cannot point itself at another server.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import brain_service
from server import git_server
from server.applications.middleware import requires_permission
from server.middleware.owner_session import live_session

router = APIRouter(prefix="/api/git-server", tags=["git-server"])

# Settings, which is served from its own origin (see routes/host_name.py for
# why only such an app may hold an owner-only write).
OWNER_APP_IDS = ("settings",)


async def owner_only(request: Request) -> None:
    tok = getattr(request.state, "app_token", None)
    if getattr(tok, "app_id", None) not in OWNER_APP_IDS:
        raise HTTPException(403, "only the owner can connect a git server, in Settings")
    email, _token = await live_session(request.scope)
    if not email:
        raise HTTPException(403, "sign in as the owner to connect a git server")


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "the Host's database is not up yet")
    return pool


class ConnectBody(BaseModel):
    url: str
    username: str
    token: str
    person_username: Optional[str] = None


@router.get("", dependencies=[Depends(requires_permission("read:git_server"))])
async def status():
    return await git_server.status(_pool(), int(brain_service._entity_id))


@router.post("", dependencies=[Depends(requires_permission("write:git_server")),
                               Depends(owner_only)])
async def connect(body: ConnectBody):
    try:
        return await git_server.connect(
            _pool(), int(brain_service._entity_id), url=body.url, username=body.username,
            token=body.token, person_username=body.person_username)
    except git_server.GitServerError as e:
        raise HTTPException(e.status, e.message) from None


@router.delete("", dependencies=[Depends(requires_permission("write:git_server")),
                                 Depends(owner_only)])
async def disconnect():
    return await git_server.disconnect(_pool(), int(brain_service._entity_id))
