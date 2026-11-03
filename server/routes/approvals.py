"""The passkey half of an approval pop-up (server/approval_passkeys.py).

  GET  /api/approvals/passkey          can a passkey approve here? and where
                                       one is set up
  POST /api/approvals/passkey/ticket   a challenge for answering one card
                                       {ticket_id, option_id, choices?}
  POST /api/approvals/passkey/request  a challenge for one request a card
                                       sends to a route of its own
                                       {path, body}

The purpose is computed here from what the server holds (the ticket as it is
now, the request's own body), exactly as the route that acts computes it, so
the browser chooses nothing it is approving. Only the person's own chat
(routes/tickets.PERSON_SURFACES) asks, with the owner's live session on this
address. Walled from the Host (kernel_ops._WALLED_PATHS): its tools never ask
for an approval, and could not answer one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from server import approval_passkeys, brain_service
from server.applications.middleware import requires_permission
from server.middleware.owner_session import live_session, request_host

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# The requests a card sends to a route of its own that a passkey can approve,
# each route binding approval_passkeys.purpose_for_request itself.
REQUEST_PATHS = frozenset({"/api/network/bastions/machine-offers/accept"})


def _no_store(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _person(request: Request) -> None:
    from server.routes.tickets import PERSON_SURFACES
    tok = getattr(request.state, "app_token", None)
    if str(getattr(tok, "app_id", "") or "") not in PERSON_SURFACES:
        raise HTTPException(403, "only your own chat asks for a passkey approval")
    email, _token = await live_session(request.scope)
    if not email:
        raise HTTPException(401, "Sign in first.")


def _setup_url() -> str:
    from server.home_origin import HOME
    return HOME.origin + "/passkey"


def _refused(e: approval_passkeys.ApprovalPasskeyError) -> JSONResponse:
    return _no_store(JSONResponse({"detail": e.detail, "no_passkey": e.no_passkey},
                                  status_code=e.status))


@router.get("/passkey", dependencies=[Depends(requires_permission("write:tickets"))])
async def passkey_status(request: Request):
    """{passkey: whether a passkey approves on this address, code: whether
    an authenticator code is set up, setup_url: where a passkey is added}."""
    await _person(request)
    pool = brain_service.get_pool()
    from server.network import nac_stepup
    try:
        code = await nac_stepup.is_enrolled(pool) if pool is not None else False
    except Exception:  # noqa: BLE001
        code = False
    return _no_store(JSONResponse({
        "passkey": await approval_passkeys.available(pool, request_host(request.scope)),
        "code": bool(code), "setup_url": _setup_url()}))


@router.post("/passkey/ticket", dependencies=[Depends(requires_permission("write:tickets"))])
async def ticket_challenge(request: Request):
    await _person(request)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    try:
        ticket_id = int(body.get("ticket_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "ticket_id required") from None
    from server.routes.tickets import _entity_id
    row = await pool.fetchrow(
        "SELECT status, sitrep, options, metadata FROM entity_tickets WHERE id=$1 AND entity_id=$2",
        ticket_id, _entity_id())
    if row is None:
        raise HTTPException(404, "ticket not found")
    if row["status"] != "open":
        raise HTTPException(409, f"ticket status is {row['status']!r}, can't decide")
    md = row["metadata"]
    if isinstance(md, str):
        import json
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    purpose = approval_passkeys.ticket_purpose(
        ticket_id, row["sitrep"], row["options"], md, str(body.get("option_id") or ""),
        body.get("choices"), await approval_passkeys.acted_on(pool, md))
    if purpose is None:
        raise HTTPException(400, "that option is not on this card")
    try:
        return _no_store(JSONResponse(await approval_passkeys.options(
            pool, request_host(request.scope), purpose)))
    except approval_passkeys.ApprovalPasskeyError as e:
        return _refused(e)


@router.post("/passkey/request", dependencies=[Depends(requires_permission("write:tickets"))])
async def request_challenge(request: Request):
    await _person(request)
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    path = body.get("path") if isinstance(body, dict) else None
    if path not in REQUEST_PATHS:
        raise HTTPException(400, "a passkey does not approve that request")
    purpose = approval_passkeys.purpose_for_request("POST", path, body.get("body"))
    if purpose is None:
        raise HTTPException(400, "the request's body must be a JSON object")
    try:
        return _no_store(JSONResponse(await approval_passkeys.options(
            pool, request_host(request.scope), purpose)))
    except approval_passkeys.ApprovalPasskeyError as e:
        return _refused(e)
