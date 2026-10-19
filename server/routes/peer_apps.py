"""Peer app lane — the routes an installed app calls to talk to itself on a
peer host.

The app never sees a cert or a slip. It sees:

  GET  /api/peer/app/peers            who granted us, whom we granted   read:peer_app
  POST /api/peer/app/grant            let peer P's copy of this app call ours   write:peer_app
  POST /api/peer/app/send             one message to peer P's copy of this app  write:peer_app
  GET  /api/peer/app/outbox           our sends and their status         read:peer_app
  GET  /api/peer/app/inbox            what peers sent us (unacked first) read:peer_app
  POST /api/peer/app/inbox/ack        mark rows handled                  read:peer_app

WHICH APP is never a parameter: it is the app_id bound into the window token
(middleware.ApplicationTokenMiddleware), so app A cannot read app B's inbox or
send as it. Every query below is scoped by that id.

WHICH PEER is bounded by the user: /grant refuses a peer who is not an
accepted contact (contacts.status = 'active'), and /send refuses a peer whose
host has not granted this app. Both are the user's decisions, made elsewhere
(Circles accept; the peer's own app calling /grant). Nothing here widens them.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission
from server.peer import app_lane

log = logging.getLogger("privatae.pc.peer_apps")

router = APIRouter(prefix="/api/peer/app", tags=["peer-app"])


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "host not ready")
    return pool


#: The app_id kernel_ops mints when the HOST calls a route (kernel_ops.py:269).
#: The one identity allowed to act on behalf of a named app below.
HOST_TOKEN_APP_ID = "host"


async def _app_id(request: Request, on_behalf_of: Optional[str] = None,
                  need: str = "read:peer_app") -> str:
    """WHICH app this call is about, and whether it may be driven this way.

    An app's window token names the app, full stop: an `app_id` it sends is
    ignored, never honoured, so app A can never read or send as app B.

    The HOST's kernel_op token names no app (app_id 'host' — an id no
    manifest may carry, manifest.RESERVED_APP_IDS), so a host call must say
    which installed app it is driving, and that app must have DECLARED the
    half being driven: `need` is read:peer_app for the four reads and
    write:peer_app for grant and send. The route's own requires_permission
    cannot check this — kernel_op mints exactly the route's scope, so the
    host always carries it; the consent that matters is the driven app's, at
    install. Without this a receive-only app could be made to grant and send.
    """
    tok = getattr(request.state, "app_token", None)
    app_id = getattr(tok, "app_id", None)
    if not app_lane.valid_app_id(app_id):
        raise HTTPException(401, "application token required")
    if app_id != HOST_TOKEN_APP_ID:
        return app_id
    if not on_behalf_of or not app_lane.valid_app_id(on_behalf_of):
        raise HTTPException(400, "a host call names the app it drives: app_id=<installed app id>")
    if on_behalf_of == HOST_TOKEN_APP_ID:
        raise HTTPException(400, "'host' is not an app")
    try:
        from server.applications import registry
        app = await registry.get(_pool(), on_behalf_of)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("peer app host call: registry lookup for %s failed: %s", on_behalf_of, type(e).__name__)
        raise HTTPException(503, "could not check the app")
    if app is None or getattr(app, "status", "active") != "active":
        raise HTTPException(409, f"app '{on_behalf_of}' is not installed on this host")
    declared = getattr(app, "permissions", None) or []
    if need not in declared:
        raise HTTPException(409, f"app '{on_behalf_of}' did not declare {need}; the host may not drive that half for it")
    return on_behalf_of


class GrantIn(BaseModel):
    peer_entity_id: int
    # Host calls only (kernel_op): the installed app being driven. Ignored
    # for an app's own token.
    app_id: Optional[str] = None


class SendIn(BaseModel):
    app_id: Optional[str] = None
    peer_entity_id: int
    action: str
    payload: dict = {}
    # Optional caller-supplied idempotency key; a repeat with the same id
    # returns the existing row instead of sending twice.
    message_id: Optional[str] = None
    # Try the wire now and wait for the answer (default). False = queue only;
    # the outbox loop delivers within a tick.
    wait: bool = True


class AckIn(BaseModel):
    ids: list[int]
    app_id: Optional[str] = None


def _row(r) -> dict:
    d = dict(r)
    for k in ("message_id",):
        if k in d and d[k] is not None:
            d[k] = str(d[k])
    # asyncpg hands jsonb back as text unless a codec is set on the pool.
    for k in ("payload", "response"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    for k in ("created_at", "delivered_at", "next_attempt_at", "expires_at",
              "received_at", "acked_at", "updated_at"):
        if k in d and d[k] is not None:
            d[k] = d[k].isoformat()
    return d


@router.get("/peers", dependencies=[Depends(requires_permission("read:peer_app"))])
async def peers(request: Request, app_id: Optional[str] = Query(None)):
    app_id = await _app_id(request, app_id)
    pool = _pool()
    granted_to_us = await pool.fetch(
        "SELECT peer_entity_id, contract_id, expires_at, updated_at FROM peer_app_slips "
        "WHERE app_id = $1 ORDER BY updated_at DESC", app_id)
    granted_by_us = await pool.fetch(
        "SELECT DISTINCT ON (peer_entity_id) peer_entity_id, status, delivered_at, created_at "
        "FROM peer_app_outbox WHERE app_id = $1 AND op = $2 "
        "ORDER BY peer_entity_id, created_at DESC", app_id, app_lane.OP_GRANT)
    return {"app_id": app_id,
            "granted_to_us": [_row(r) for r in granted_to_us],
            "granted_by_us": [_row(r) for r in granted_by_us]}


@router.post("/grant", dependencies=[Depends(requires_permission("write:peer_app"))])
async def grant(body: GrantIn, request: Request):
    """Issue peer P's copy of this app a slip to call ours, and deliver it.

    The peer must be an accepted contact — the user's decision, not the app's.
    The slip is minted by the bridge at send time (issue_app_slip) and travels
    on app.grant presenting the MESSAGE slip P gave us; if P is asleep the
    row waits in the outbox and the loop delivers it later. If P has not
    INSTALLED this app yet, P answers "not installed" and that is final: the
    row reads refused, and the app calls /grant again once P has it.
    """
    app_id = await _app_id(request, body.app_id, need="write:peer_app")
    pool = _pool()
    peer = int(body.peer_entity_id)
    if peer == int(brain_service._entity_id):
        raise HTTPException(400, "cannot grant this host to itself")
    if not await app_lane.peer_is_contact(pool, peer):
        raise HTTPException(409, "that host is not an accepted contact — connect in Circles first")
    row = await app_lane.enqueue(pool, app_id, peer, app_lane.OP_GRANT, "", {})
    out = await app_lane.attempt(pool, {**row, "app_id": app_id, "peer_entity_id": peer,
                                        "op": app_lane.OP_GRANT, "action": "", "payload": {}})
    return {"message_id": str(row["message_id"]), **out}


@router.post("/send", dependencies=[Depends(requires_permission("write:peer_app"))])
async def send(body: SendIn, request: Request):
    """Send one message to peer P's copy of this app.

    Refused up front when P has not granted this app (no peer_app_slips row):
    the fix is on P's side (their app calls /grant), and queueing a send that
    can never be presented would only hide that. Otherwise the row is written
    first, then tried once if `wait`; the loop takes it from there.
    """
    app_id = await _app_id(request, body.app_id, need="write:peer_app")
    pool = _pool()
    peer = int(body.peer_entity_id)
    if not app_lane.valid_action(body.action):
        raise HTTPException(400, "action must be a short token: [a-z0-9][a-z0-9_.-]{0,63}")
    if not app_lane._payload_ok(body.payload):
        raise HTTPException(413, f"payload must be an object under {app_lane.MAX_PAYLOAD_BYTES} bytes")
    if await app_lane.app_slip_for(pool, peer, app_id) is None:
        raise HTTPException(409, "that host has not granted this app — their copy must call /grant first")
    mid = None
    if body.message_id:
        try:
            mid = uuid.UUID(body.message_id)
        except ValueError:
            raise HTTPException(400, "message_id must be a UUID")
    row = await app_lane.enqueue(pool, app_id, peer, app_lane.OP_DELIVER, body.action, body.payload, mid)
    if row.get("status") != "queued":
        # A repeat of a message_id this app already sent to this peer: report
        # the row as it is, never as freshly queued.
        return {"message_id": str(row["message_id"]), "status": row.get("status"),
                "queued": False, "existing": True}
    if not body.wait:
        return {"message_id": str(row["message_id"]), "status": "queued", "queued": True}
    out = await app_lane.attempt(pool, {**row, "app_id": app_id, "peer_entity_id": peer,
                                        "op": app_lane.OP_DELIVER, "action": body.action,
                                        "payload": body.payload})
    return {"message_id": str(row["message_id"]), **out}


@router.get("/outbox", dependencies=[Depends(requires_permission("read:peer_app"))])
async def outbox(request: Request,
                 status: Optional[str] = Query(None),
                 limit: int = Query(100, ge=1, le=500),
                 app_id: Optional[str] = Query(None)):
    app_id = await _app_id(request, app_id)
    pool = _pool()
    if status:
        rows = await pool.fetch(
            "SELECT id, peer_entity_id, op, action, message_id, status, attempts, last_error, "
            "response, created_at, delivered_at, next_attempt_at, expires_at FROM peer_app_outbox "
            "WHERE app_id = $1 AND status = $2 ORDER BY id DESC LIMIT $3", app_id, status, limit)
    else:
        rows = await pool.fetch(
            "SELECT id, peer_entity_id, op, action, message_id, status, attempts, last_error, "
            "response, created_at, delivered_at, next_attempt_at, expires_at FROM peer_app_outbox "
            "WHERE app_id = $1 ORDER BY id DESC LIMIT $2", app_id, limit)
    return {"app_id": app_id, "items": [_row(r) for r in rows]}


@router.get("/inbox", dependencies=[Depends(requires_permission("read:peer_app"))])
async def inbox(request: Request,
                since_id: int = Query(0, ge=0),
                unacked: bool = Query(True),
                limit: int = Query(100, ge=1, le=500),
                app_id: Optional[str] = Query(None)):
    app_id = await _app_id(request, app_id)
    pool = _pool()
    rows = await pool.fetch(
        "SELECT id, peer_entity_id, action, payload, message_id, received_at, acked_at "
        "FROM peer_app_inbox WHERE app_id = $1 AND id > $2 "
        "AND ($3::bool = FALSE OR acked_at IS NULL) ORDER BY id LIMIT $4",
        app_id, since_id, unacked, limit)
    return {"app_id": app_id, "items": [_row(r) for r in rows]}


@router.post("/inbox/ack", dependencies=[Depends(requires_permission("read:peer_app"))])
async def ack(body: AckIn, request: Request):
    app_id = await _app_id(request, body.app_id)
    pool = _pool()
    ids = [int(i) for i in body.ids][:500]
    if not ids:
        return {"acked": 0}
    n = await pool.fetchval(
        "WITH u AS (UPDATE peer_app_inbox SET acked_at = now() "
        "WHERE app_id = $1 AND id = ANY($2::bigint[]) AND acked_at IS NULL RETURNING id) "
        "SELECT count(*) FROM u", app_id, ids)
    return {"acked": int(n or 0)}
