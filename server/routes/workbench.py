"""
Workbench — the project's Code Approvals.

The Workbench is the Host's coding surface. A project mounts one Code
Repository, and the changes the Host wants to make there are its Code
Approvals — pull requests on the forge, shown in plain English
(routes/code_approvals.py). This module reads them for the project that is
open: the list, one in full, its diff; it records "ask for changes"; and it
writes the chat line once one is merged.

It never merges. A button a person clicks merges as that person (only an
auto-merge would be the Host), and the only login this pod
holds on the forge is the Host's own. So Approve opens a window on the
platform's own site, which merges with the person's forge account, or, on a
git server the person connected in Settings, the pull request's own page
there, where they merge as themselves; the route that merged here with the
Host's login is gone. The Host merges only under Auto-Approve
(project_git.merge_project_pr).

Scopes reuse the existing review surface: read:review for reads,
write:review for what the person asks for.

Fails SOFT (degrade, don't crash): with no forge login every read returns
{connected: false} so the app shows that state instead of erroring.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from server.applications.middleware import requires_permission
# Shared gitea client (also used by routes/gitea.py — one implementation).
from server.routes.gitea_client import gitea_creds as _gitea_creds

logger = logging.getLogger("privatae.pc.workbench")

router = APIRouter(prefix="/api/workbench", tags=["workbench"])

_READ = Depends(requires_permission("read:review"))
_WRITE = Depends(requires_permission("write:review"))


def _ctx():
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "the Host's database is not ready")
    return pool, int(brain_service._entity_id)


async def _call(coro):
    """A forge or project refusal as the HTTP answer it names."""
    from server.routes.project_forge import ForgeError
    try:
        return await coro
    except ForgeError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/gitea/status", dependencies=[_READ])
async def gitea_status():
    """Is a gitea credential present? The app keys its connect-state on this."""
    cred = _gitea_creds()
    if not cred:
        return {"connected": False, "reason": "no git server connected (Settings, Git server)"}
    user, _token, host = cred
    return {"connected": True, "user": user, "host": host}


@router.get("/approvals", dependencies=[_READ])
async def list_approvals(project_id: int = Query(..., ge=1)):
    """The open project's Code Approvals: every open one, then the most
    recently closed, each in plain words."""
    from server.routes import code_approvals as _ca
    from server.routes import project_forge as _pf
    pool, eid = _ctx()
    # A project whose code lives on the person's machine is reached through
    # the Host whether or not the platform forge is connected.
    forge = await _call(_pf.project_forge(pool, project_id))
    if not forge.on_machine and not _gitea_creds():
        return {"connected": False, "approvals": []}
    return {"connected": True, **await _call(_ca.list_approvals(pool, eid, project_id))}


@router.get("/approvals/{number}", dependencies=[_READ])
async def get_approval(number: int, project_id: int = Query(..., ge=1)):
    """One Code Approval in full, and where the person approves it."""
    from server.routes import code_approvals as _ca
    pool, eid = _ctx()
    return await _call(_ca.get_approval(pool, eid, project_id, number))


@router.get("/approvals/{number}/diff", dependencies=[_READ])
async def approval_diff(number: int, project_id: int = Query(..., ge=1)):
    """The full change, for anyone who wants to read the code (the forge
    serves it at .../pulls/<n>.diff), from wherever the project's code lives."""
    from server.routes import code_approvals as _ca
    from server.routes.gitea_client import split_repo
    pool, eid = _ctx()

    async def _go():
        async with _ca._forge(pool, eid, project_id) as (_f, request, repo, _s):
            owner, name = split_repo(repo)
            return await request("GET", f"/api/v1/repos/{owner}/{name}/pulls/{int(number)}.diff",
                                 raw_text=True)
    return {"number": number, "diff": await _call(_go())}


class ChangesReq(BaseModel):
    project_id: int
    note: str = ""


@router.post("/approvals/{number}/changes", dependencies=[_WRITE])
async def ask_for_changes(number: int, body: ChangesReq):
    """The person asks for changes instead of approving. Returns the message
    the Workbench sends to the Host in the chat."""
    from server.routes import code_approvals as _ca
    pool, eid = _ctx()
    return await _call(_ca.ask_for_changes(pool, eid, body.project_id, number, body.note))


class AnnounceReq(BaseModel):
    project_id: int


@router.post("/approvals/{number}/announce", dependencies=[_WRITE])
async def announce(number: int, body: AnnounceReq):
    """Write the chat line for a merged Code Approval, once, from the forge's
    own merge record. The Workbench calls it when the approve window closes
    and when the list shows a merged one not yet announced; a change that is
    not merged writes nothing."""
    from server.routes import code_approvals as _ca
    pool, eid = _ctx()
    return await _call(_ca.announce_merge(pool, eid, body.project_id, number))
