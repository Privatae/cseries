"""Groups — the host- and app-facing surface for a roster and a signed vote.

  GET    /api/groups                        groups owned or joined      read:group
  POST   /api/groups                        create one (this host owns) write:group
  GET    /api/groups/{id}                   the group + its roster      read:group
  POST   /api/groups/{id}/members           add a member (owner only)   write:group
  DELETE /api/groups/{id}/members/{entity}  remove one  (owner only)    write:group
  POST   /api/groups/{id}/members/{entity}/role   set a role            write:group
  GET    /api/groups/join-requests          our own outbound asks       read:group
  POST   /api/groups/{id}/join-request      ask an owner to let us in   write:group
  GET    /api/groups/{id}/join-requests     unanswered asks to us       read:group
  POST   /api/groups/{id}/join-requests/{e}/accept  contact + roster    write:group
  POST   /api/groups/{id}/join-requests/{e}/deny    archive, silently   write:group
  GET    /api/groups/rendezvous             our interest tags           read:group
  POST   /api/groups/rendezvous             set them + list them        write:group
  DELETE /api/groups/rendezvous             withdraw one or all         write:group
  GET    /api/groups/rendezvous/introductions        what the batch sent read:group
  POST   /api/groups/rendezvous/introductions/{m}/accept  join it        write:group
  POST   /api/groups/{id}/sync              pull the roster now         write:group
  POST   /api/groups/{id}/app-grant         grant an app to the group   write:group
  POST   /api/groups/{id}/ballots           open a ballot               write:group
  GET    /api/groups/{id}/ballots           ballots                     read:group
  POST   /api/groups/{id}/ballots/{b}/vote  sign and submit our vote    write:group
  GET    /api/groups/{id}/ballots/{b}/tally recount from signatures     read:group

WHO OWNS WHAT is the only gate that matters here and it is not in this file:
every mutation goes through roster._require_owner, so a replica of someone
else's group — which lives in the same tables — can never be edited locally and
then silently reverted by the next pull.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission
from server.groups import join, lane, rendezvous, roster, votes

log = logging.getLogger("privatae.pc.groups")

router = APIRouter(prefix="/api/groups", tags=["groups"])


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "host not ready")
    return pool


def _me() -> int:
    return int(brain_service._entity_id)


def _refuse(e: roster.RosterError) -> HTTPException:
    return HTTPException(e.status, e.detail)


def _vote_advice(answer) -> dict:
    """Turn "stored, not counted" into something the member can act on.

    A vote cast before the group's owner has pinned this host's key is kept and
    left out of the count. Nothing about that is an error, so it must not be
    reported as one — but saying nothing at all is worse: the member sees a
    200, and their vote is not in the tally. The route says so and says what to
    do, structurally (`key_not_pinned`) and in one sentence.
    """
    if not isinstance(answer, dict) or not answer.get("key_not_pinned"):
        return {}
    return {"key_not_pinned": True, "counted": False,
            "advice": votes.NO_PINNED_KEY_ADVICE}


def _j(v):
    """Datetimes and bytes out of asyncpg, as JSON. verify_pubkey goes base64
    because it is key material on the wire, not text."""
    import base64
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    return v


def _row(d: dict) -> dict:
    return {k: _j(v) for k, v in dict(d).items()}


class CreateIn(BaseModel):
    name: str
    description: str = ""


class AddMemberIn(BaseModel):
    member_entity_id: int
    role: str = "member"
    display_name: str = ""


class RoleIn(BaseModel):
    role: str


class AppGrantIn(BaseModel):
    app_id: str


class BallotIn(BaseModel):
    ballot_id: str
    question: str
    choices: list
    closes_at: _dt.datetime
    target_kind: str = ""
    target_ref: str = ""


class VoteIn(BaseModel):
    choice: str


class JoinAskIn(BaseModel):
    owner_entity_id: int
    note: str = ""


class JoinAcceptIn(BaseModel):
    role: str = "member"


class InterestsIn(BaseModel):
    tags: list = []


# ── Groups ─────────────────────────────────────────────────────────────────

@router.get("", dependencies=[Depends(requires_permission("read:group"))])
async def list_groups():
    rows = await roster.list_groups(_pool(), _me())
    return {"groups": [_row(r) for r in rows]}


@router.post("", dependencies=[Depends(requires_permission("write:group"))])
async def create_group(body: CreateIn):
    try:
        g = await roster.create_group(_pool(), _me(), name=body.name,
                                      description=body.description)
    except roster.RosterError as e:
        raise _refuse(e)
    return {"group": _row(g)}


@router.get("/join-requests", dependencies=[Depends(requires_permission("read:group"))])
async def my_join_requests():
    """Join requests THIS host has sent, and whether each has landed us in the
    group yet.

    DECLARED BEFORE `/{group_id}` ON PURPOSE. FastAPI matches routes in
    declaration order, and `/{group_id}` would otherwise swallow this path and
    answer 400 "malformed group id" — a literal segment has to be registered
    ahead of the template that shadows it. Moving this below that route is a
    silent break, so it is stated here rather than left to be rediscovered.
    """
    return {"requests": [_row(r) for r in await join.my_requests(_pool())]}


# ── Rendezvous ─────────────────────────────────────────────────────────────
# Under /api/groups and not a prefix of
# its own, for two reasons that agree: rendezvous is how a member GETS INTO a
# group, so it is the same subject; and /api/groups is already classified in
# both kernel_ops._EXPOSED_PREFIXES and generate_capabilities._AREA_HOST_TOOLS,
# so these routes inherit that classification instead of opening a new area.
# A NEW area key missing from _AREA_HOST_TOOLS reads as unreviewed and stops the
# ship at check_host_parity — a failure recorded twice in
# generate_capabilities.py's own comments. Nothing here raises the parity floor.
#
# These paths sit above `/{group_id}` for the ordering reason stated on
# /join-requests: a literal segment must be registered ahead of the template
# that would otherwise swallow it.

@router.get("/rendezvous", dependencies=[Depends(requires_permission("read:group"))])
async def list_interests(include_withdrawn: bool = Query(False)):
    """This host's interest tags, and whether the platform has them."""
    rows = await rendezvous.interests(_pool(), include_withdrawn=include_withdrawn)
    return {"interests": [_row(r) for r in rows]}


@router.post("/rendezvous", dependencies=[Depends(requires_permission("write:group"))])
async def set_interests(body: InterestsIn):
    """Set the whole tag list, then list it with the platform.

    Store-then-publish, in that order and never the reverse: the tags are the
    user's whether or not the bridge is reachable, and a publish that failed
    must leave something for the next publish to send. The answer says which of
    the two happened — "saved but not listed yet" is a real state and reporting
    it as success would be a lie the user acts on.
    """
    pool = _pool()
    try:
        saved = await rendezvous.set_interests(pool, body.tags)
    except rendezvous.RendezvousError as e:
        raise HTTPException(e.status, e.detail)
    return {**saved, **(await rendezvous.publish(pool, _me()))}


@router.delete("/rendezvous", dependencies=[Depends(requires_permission("write:group"))])
async def withdraw_interest(tag: Optional[str] = Query(None)):
    """Withdraw one tag, or every tag when none is named."""
    pool = _pool()
    try:
        out = await rendezvous.withdraw(pool, tag)
        # The NORMALISED tag, never the caller's raw query string. Both ends
        # would otherwise be told about different tags: this host withdraws
        # "bonsai" and the platform is asked for "Bonsai", which it refuses —
        # correctly, since that end validates and never transforms.
        # withdraw_remote normalises defensively too; this is the call site
        # being honest rather than leaning on that.
        remote = await rendezvous.withdraw_remote(
            pool, _me(), None if tag is None else out["withdrawn"])
    except rendezvous.RendezvousError as e:
        raise HTTPException(e.status, e.detail)
    return {**out, **remote}


@router.get("/rendezvous/introductions",
            dependencies=[Depends(requires_permission("read:group"))])
async def list_introductions():
    """Introductions waiting in this host's inbox. `others` is a COUNT — there
    is no field naming them, by design."""
    return {"introductions": await rendezvous.introductions(_pool())}


@router.post("/rendezvous/introductions/{message_id}/accept",
             dependencies=[Depends(requires_permission("write:group"))])
async def accept_introduction(message_id: str):
    """Join the community group an introduction named. The user's act, never
    the batch's: the platform introduces and never enrols."""
    try:
        return await rendezvous.accept_intro(_pool(), message_id)
    except rendezvous.RendezvousError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/{group_id}", dependencies=[Depends(requires_permission("read:group"))])
async def get_group(group_id: str, include_removed: bool = Query(False)):
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    pool = _pool()
    g = await roster.get_group(pool, group_id)
    if g is None:
        raise HTTPException(404, "no such group")
    rows = await roster.roster(pool, group_id, include_removed=include_removed)
    return {"group": _row(g), "owned": int(g["owner_entity_id"]) == _me(),
            "members": [_row(r) for r in rows]}


# ── Roster (owner only) ────────────────────────────────────────────────────

@router.post("/{group_id}/members", dependencies=[Depends(requires_permission("write:group"))])
async def add_member(group_id: str, body: AddMemberIn):
    """Add a member and send them the invite.

    The invite (`group.grant`) rides the MESSAGE slip the member already issued
    this host, so the member must be an accepted contact. That bound is stated
    rather than hidden: you cannot add a stranger to a group circle — connect in
    Circles first. It is refused here, at the decision, rather than discovered
    later as an outbox row that can never be sent.
    """
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    pool = _pool()
    peer = int(body.member_entity_id)
    from server.peer import app_lane
    if not await app_lane.peer_is_contact(pool, peer):
        raise HTTPException(409, "that host is not an accepted contact — connect in Circles first")
    try:
        out = await roster.add_member(pool, _me(), group_id, peer,
                                      role=body.role, display_name=body.display_name)
    except roster.RosterError as e:
        raise _refuse(e)
    if out.get("added"):
        row = await lane.enqueue(pool, group_id, peer, lane.OP_GRANT, {})
        out["invite"] = await lane.attempt(pool, {**row, "group_id": group_id,
                                                  "peer_entity_id": peer,
                                                  "op": lane.OP_GRANT, "payload": {}})
    return out


@router.delete("/{group_id}/members/{entity_id}",
               dependencies=[Depends(requires_permission("write:group"))])
async def remove_member(group_id: str, entity_id: int):
    """Remove a member. Three layers bite, in this order:

      1. the ROSTER, immediately — every member that has pulled refuses the
         removed member's next group-scoped app delivery on a LOCAL check;
      2. the CONTRACT — the group slip is revoked, which is what stops a member
         whose replica is behind;
      3. EXPIRY, at 90 days, as the backstop.

    Layer 2 is best-effort against the bridge and says so when it fails, rather
    than reporting a removal as fully enforced when one of its layers did not
    run.
    """
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    pool = _pool()
    try:
        out = await roster.remove_member(pool, _me(), group_id, int(entity_id))
    except roster.RosterError as e:
        raise _refuse(e)
    out["contracts_revoked"] = await lane.revoke_group_slips(pool, group_id, int(entity_id))
    return out


@router.post("/{group_id}/members/{entity_id}/role",
             dependencies=[Depends(requires_permission("write:group"))])
async def set_role(group_id: str, entity_id: int, body: RoleIn):
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    try:
        return await roster.set_role(_pool(), _me(), group_id, int(entity_id), body.role)
    except roster.RosterError as e:
        raise _refuse(e)


@router.post("/{group_id}/sync", dependencies=[Depends(requires_permission("write:group"))])
async def sync(group_id: str):
    """Pull a group's roster from its owner NOW. The background loop does this
    every 300s; this is the on-demand path and it is deliberately uncapped —
    the interval bounds the BACKGROUND poll, never the user."""
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    try:
        return await lane.sync_group(_pool(), group_id)
    except roster.RosterError as e:
        raise _refuse(e)


# ── Join requests ──────────────────────────────────────────────────────────
# A host that is NOT yet a contact asking to be let in. server/groups/join.py
# carries the reasoning; these routes are the surface and nothing else.

@router.post("/{group_id}/join-request",
             dependencies=[Depends(requires_permission("write:group"))])
async def ask_to_join(group_id: str, body: JoinAskIn):
    """Ask a group's owner to let this host in.

    `owner_entity_id` comes from the CALLER because a host that is not in a
    group holds no row naming its owner — that is the situation this op exists
    for. Naming a group confers nothing; the owner's host decides whether the
    pair means anything at all.
    """
    try:
        return await join.ask_to_join(_pool(), group_id, body.owner_entity_id,
                                      note=body.note)
    except roster.RosterError as e:
        raise _refuse(e)


@router.get("/{group_id}/join-requests",
            dependencies=[Depends(requires_permission("read:group"))])
async def list_join_requests(group_id: str):
    """The unanswered requests to join a group this host owns."""
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    pool = _pool()
    g = await roster.get_group(pool, group_id)
    if g is None or int(g["owner_entity_id"]) != _me():
        # Same single sentence _group_join_target gives a stranger, for the same
        # reason: distinguishing "no such group" from "not yours" hands a caller
        # an enumeration oracle. An app holding read:group is not a stranger,
        # but it is not necessarily the user either.
        raise HTTPException(404, "this host does not host that group")
    return {"group_id": group_id, "requests": await join.pending(pool, group_id)}


@router.post("/{group_id}/join-requests/{entity_id}/accept",
             dependencies=[Depends(requires_permission("write:group"))])
async def accept_join(group_id: str, entity_id: int, body: JoinAcceptIn | None = None):
    """Accept: make the contact, then put them on the roster, then invite.

    The answer reports the three outcomes separately. A group grant that is
    QUEUED rather than delivered is the normal case, not a failure — the
    requester's reciprocal message slip has not arrived yet and the group
    outbox retries until it does — so it must not be reported as delivered and
    must not be reported as an error either.
    """
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    try:
        return await join.accept(_pool(), _me(), group_id, entity_id,
                                 role=(body.role if body else "member"))
    except roster.RosterError as e:
        raise _refuse(e)


@router.post("/{group_id}/join-requests/{entity_id}/deny",
             dependencies=[Depends(requires_permission("write:group"))])
async def deny_join(group_id: str, entity_id: int):
    """Deny: archive the card. Nothing is stored and nothing is sent back."""
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    try:
        return await join.deny(_pool(), _me(), group_id, entity_id)
    except roster.RosterError as e:
        raise _refuse(e)


# ── The group app grant ────────────────────────────────────────────────────

@router.post("/{group_id}/app-grant", dependencies=[Depends(requires_permission("write:group"))])
async def app_grant(group_id: str, body: AppGrantIn):
    """Grant an installed app to every current member of a group, and to every
    member who joins later.

    One decision instead of one per pair. The slip each member gets names the
    group in its (bridge-signed) resource, so the receiver re-checks membership
    at delivery time against its own roster replica — which is what makes a
    removal take effect without waiting for a CRL or an expiry.
    """
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    from server.peer import app_lane
    if not app_lane.valid_app_id(body.app_id):
        raise HTTPException(400, "malformed app_id")
    pool = _pool()
    g = await roster.get_group(pool, group_id)
    if g is None:
        raise HTTPException(404, "no such group")
    ok, why, transient = await app_lane._app_accepts_peers(pool, body.app_id)
    if not ok:
        raise HTTPException(503 if transient else 409, why)
    await pool.execute(
        "INSERT INTO group_app_grants (group_id, app_id) VALUES ($1, $2) "
        "ON CONFLICT (group_id, app_id) DO UPDATE SET revoked_at = NULL",
        group_id, body.app_id)
    return await app_lane.grant_group(pool, body.app_id, group_id, _me())


# ── Ballots and votes ──────────────────────────────────────────────────────

@router.post("/{group_id}/ballots", dependencies=[Depends(requires_permission("write:group"))])
async def create_ballot(group_id: str, body: BallotIn):
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    try:
        b = await roster.create_ballot(
            _pool(), _me(), group_id, ballot_id=body.ballot_id, question=body.question,
            choices=body.choices, closes_at=body.closes_at,
            target_kind=body.target_kind, target_ref=body.target_ref)
    except roster.RosterError as e:
        raise _refuse(e)
    return {"ballot": _row(b)}


@router.get("/{group_id}/ballots", dependencies=[Depends(requires_permission("read:group"))])
async def list_ballots(group_id: str, limit: int = Query(100, ge=1, le=500),
                       offset: int = Query(0, ge=0)):
    if not roster.valid_group_id(group_id):
        raise HTTPException(400, "malformed group id")
    # A page past the first `limit`.
    rows = await roster.list_ballots(_pool(), group_id, limit, offset)
    return {"group_id": group_id, "ballots": [_row(r) for r in rows], "offset": offset,
            "next_offset": (offset + len(rows)) if len(rows) == limit else None}


@router.post("/{group_id}/ballots/{ballot_id}/vote",
             dependencies=[Depends(requires_permission("write:group"))])
async def cast_vote(group_id: str, ballot_id: str, body: VoteIn):
    """Sign this host's vote and get it to the group's owner.

    A change of mind is a new call: the seq increments past the one already
    held, the owner's verifier requires strictly-greater, and the row is
    superseded in place. A REPLAY of an older signed vote is refused by the
    same rule.

    When this host IS the owner, the vote takes `accept_vote` — the exact path
    a peer's vote takes — rather than a local shortcut, so a host can never
    admit its own vote under weaker checks than a member's.
    """
    if not roster.valid_group_id(group_id) or not roster.valid_ballot_id(ballot_id):
        raise HTTPException(400, "malformed group or ballot id")
    pool = _pool()
    me = _me()
    g = await roster.get_group(pool, group_id)
    if g is None:
        raise HTTPException(404, "no such group")
    ballot = await roster.get_ballot(pool, group_id, ballot_id)
    if ballot is None:
        raise HTTPException(404, "no such ballot")
    if body.choice not in (ballot.get("choices") or []):
        raise HTTPException(400, "that is not one of this ballot's choices")
    if not await roster.is_current_member(pool, group_id, me):
        raise HTTPException(403, "this host is not a current member of that group")

    vote = await votes.sign_vote(pool, me, group_id=group_id, ballot_id=ballot_id,
                                 choice=body.choice,
                                 seq=await lane.next_vote_seq(pool, group_id, me))

    if int(g["owner_entity_id"]) == me:
        out = await lane.accept_vote(pool, group_id, ballot_id, vote)
        return {"group_id": group_id, "ballot_id": ballot_id, "owner": True,
                **out, **_vote_advice(out)}
    peer = int(g["owner_entity_id"])
    payload = {"ballot_id": ballot_id, "vote": vote}
    row = await lane.enqueue(pool, group_id, peer, lane.OP_VOTE, payload)
    out = await lane.attempt(pool, {**row, "group_id": group_id, "peer_entity_id": peer,
                                    "op": lane.OP_VOTE, "payload": payload})
    return {"group_id": group_id, "ballot_id": ballot_id, "owner": False,
            "seq": vote["terms"]["seq"], **out,
            # The owner's own answer rides in `response`; a vote it stored but
            # could not count has to reach the CALLER, not just the log.
            **_vote_advice(out.get("response") if isinstance(out.get("response"), dict) else None)}


@router.get("/{group_id}/ballots/{ballot_id}/tally",
            dependencies=[Depends(requires_permission("read:group"))])
async def tally(group_id: str, ballot_id: str):
    """Recount from the stored signatures. The result reports what it could NOT
    count — unverified rows, members with no pinned key, votes from former
    members — as loudly as what it could, because a tally that quietly omits
    them reads as complete when it is not."""
    if not roster.valid_group_id(group_id) or not roster.valid_ballot_id(ballot_id):
        raise HTTPException(400, "malformed group or ballot id")
    pool = _pool()
    g = await roster.get_group(pool, group_id)
    if g is not None and int(g["owner_entity_id"]) != _me():
        # A member holds the ballot but none of the votes — only the owner
        # stores them (lane.accept_vote) — so a count here would read as a
        # complete tally of zero. Say where the count lives instead.
        raise HTTPException(409, {
            "error": "votes are counted by the group's owner, not on this host",
            "owner_entity_id": int(g["owner_entity_id"]), "counted_here": False})
    try:
        return await roster.tally_ballot(pool, group_id, ballot_id)
    except roster.RosterError as e:
        raise _refuse(e)
