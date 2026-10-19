"""
Community board — bridge pass-through to communityd (the members' discussion
board).

Same shape as billing.py: nothing is computed or cached locally, every call
relays the bridge's community.* ops and the platform service is the source of
truth. The bridge pins the author identity (entity id + display name) — this
route never sends or trusts an author field.

Write idempotency: each create call sends a caller-stable `op_id` (the app
generates one per user action and retries with the SAME id), so a retry after
a timeout can never double-post. Votes converge by construction.

Degradation (rule 7): bridge not connected, or communityd down behind it,
surfaces as 503 — never a cached board, never a fake empty one.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.community")

router = APIRouter(prefix="/api/community", tags=["community"])

_READ = Depends(requires_permission("read:community"))
_WRITE = Depends(requires_permission("write:community"))


def _bridge():
    from server.main import get_bridge_client
    return get_bridge_client()


async def _op(op: str, params: dict) -> dict:
    bc = _bridge()
    if bc is None or not bc.connected:
        raise HTTPException(503, "bridge not connected — community board unavailable")
    env = await bc.request_full(op, params)
    if not env.get("ok"):
        # The bridge relays communityd's own status (404/409/400) in `code`;
        # anything else is the fail-closed 503.
        raise HTTPException(int(env.get("code") or 503),
                            env.get("error") or "community board unavailable")
    return env.get("data") or {}


# ---- reads ----

@router.get("/categories", dependencies=[_READ])
async def categories():
    """The board's categories, in display order."""
    return await _op("community.categories", {})


@router.get("/threads", dependencies=[_READ])
async def threads(
    category: str = Query("", max_length=64),
    sort: str = Query("hot", pattern="^(hot|new|top)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List threads — hot (log-votes decayed by age), new, or top."""
    return await _op("community.threads", {
        "category": category, "sort": sort, "limit": limit, "offset": offset,
    })


@router.get("/threads/{thread_id}", dependencies=[_READ])
async def thread(thread_id: int):
    """One thread + its full post list (parent-linked; client builds the tree)."""
    return await _op("community.thread", {"id": thread_id})


@router.get("/search", dependencies=[_READ])
async def search(q: str = Query(..., min_length=1, max_length=200),
                 limit: int = Query(50, ge=1, le=200)):
    """Substring search over thread titles + bodies."""
    return await _op("community.search", {"q": q, "limit": limit})


# ---- writes ----

class NewThread(BaseModel):
    op_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50_000)


class NewReply(BaseModel):
    op_id: str = Field(min_length=1, max_length=128)
    thread_id: int
    parent_id: int | None = None
    body: str = Field(min_length=1, max_length=50_000)


class VoteReq(BaseModel):
    target_kind: str = Field(pattern="^(thread|post)$")
    target_id: int
    up: bool = True


class FlagReq(BaseModel):
    target_kind: str = Field(pattern="^(thread|post)$")
    target_id: int
    reason: str = Field(default="", max_length=2000)


@router.post("/threads", dependencies=[_WRITE])
async def create_thread(req: NewThread):
    """Start a thread. Author is bridge-pinned; op_id makes retries safe."""
    return await _op("community.post", req.model_dump())


@router.post("/posts", dependencies=[_WRITE])
async def create_reply(req: NewReply):
    """Reply to a thread (parent_id nests under another reply)."""
    return await _op("community.post", req.model_dump())


@router.post("/vote", dependencies=[_WRITE])
async def vote(req: VoteReq):
    """Set (up=true) or remove (up=false) this member's single vote."""
    return await _op("community.vote", req.model_dump())


@router.post("/flag", dependencies=[_WRITE])
async def flag(req: FlagReq):
    """Report a thread or post to the human moderation queue."""
    return await _op("community.flag", req.model_dump())


# ---- phase 3: the ladder + digests ----

@router.get("/digest", dependencies=[_READ])
async def digest(
    since: str = Query("", max_length=40),
    topics: str = Query("", max_length=500),
    categories: str = Query("", max_length=200),
    limit: int = Query(50, ge=1, le=200),
):
    """Threads with activity since a cutoff (default 24h), filtered by
    comma-separated topic substrings / category slugs — the host digest
    loops read this and summarize into the inbox."""
    return await _op("community.digest", {
        "since": since,
        "topics": [t for t in (s.strip() for s in topics.split(",")) if t],
        "categories": [c for c in (s.strip() for s in categories.split(",")) if c],
        "limit": limit,
    })


@router.post("/threads/{thread_id}/propose", dependencies=[_WRITE])
async def propose(thread_id: int, done_looks_like: str = Query("")):
    """The ladder: escalate a board thread into the governed proposal queue.

    The member's HOST authors a feature-request file in the set format and
    hands it to the platform (bridge op `proposal.file`), which lands it as a
    `propose/entity-<id>-thread-<id>` branch of the proposals repo with a
    same-repo PR — no fork anywhere. The per-host fork used to put a public
    copy of the whole proposals repo under the host's own user account, which
    is what this replaces.

    Nothing about WHO or WHERE comes from this pod: the bridge stamps the
    entity id from the entity's certificate and builds the branch, and the
    platform derives the commit author from that id. The HOST is the author
    and is named on the first line of the PR body.

    The thread's votes are snapshotted into the front matter (popularity ->
    intake -> Proposal.popularity) and the thread is stamped with the PR
    number (set-once, service-side). Votes route attention; approval stays
    human. `done_looks_like` is optional — absent it, the set format's three
    fields derive from the thread's own title and body."""
    from mastercode.runtime.capability_propose import (
        ProposalCredentialUnavailable, propose_feature_request,
    )

    data = await _op("community.thread", {"id": thread_id})
    th = data.get("thread") or {}
    if th.get("proposal_ref"):
        return {"proposal_ref": th["proposal_ref"], "already_proposed": True}
    if th.get("status") == "locked":
        raise HTTPException(409, "thread locked")

    try:
        result = await propose_feature_request(
            gitea_user=os.environ.get("GITEA_USERNAME", ""),
            title=str(th.get("title") or ""),
            body=str(th.get("body") or ""),
            thread_id=thread_id,
            popularity=int(th.get("votes") or 0),
            done_looks_like=done_looks_like,
        )
    except ProposalCredentialUnavailable as e:
        # The platform holds no proposals credential. A configuration fact,
        # not a blip — say so, rather than let the host retry forever.
        raise HTTPException(503, f"proposal filing is not configured: {e}")
    except (RuntimeError, ValueError) as e:
        raise HTTPException(502, f"proposal PR failed: {e}")

    stamp_error = None
    try:
        await _op("community.set_proposal_ref",
                  {"thread_id": thread_id, "proposal_ref": result["pr"]})
    except HTTPException as e:
        # The PR exists — that is the important half. Surface the stamp miss
        # honestly instead of failing the whole action.
        stamp_error = e.detail
        logger.warning("propose: PR #%s opened but stamp failed: %s", result["pr"], e.detail)

    out = {"proposal_ref": result["pr"], "url": result.get("url")}
    if stamp_error:
        out["stamp_error"] = stamp_error
    return out
