"""The page an outside agent's approval link opens: /agent-access/ask/<id>.

A page of the person's, behind their session like any page of the Host. It is
written from the stored ask, never from the agent: what the agent supplied
(its name, its note) is shown as a quotation.

  * kind 'grant' (the agent asked for something it may not do): "Just this
    once", "Always for this agent" or "No"; either yes takes the person's code.
  * kind 'machine' (the machine holds the op for the person's own approval):
    the Host's card for it, answered here as the chat's pop-up answers it
    (tickets.decide_ticket_service); or, when the machine waits on the
    person's approval device or its console, where to approve it. The Host
    never signs a machine's approval.

Also the person's list of connected agents for Settings, and its two revokes.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from server.agent_access import pages, person, store
from server.agent_access.oauth import _form, _html, _pool_eid

router = APIRouter()
log = logging.getLogger("cseries.agent_access")


def _gone(text: str) -> HTMLResponse:
    return _html(pages.message("This request is closed", text), 410)


async def _load(request: Request, ask_id: str):
    pool, eid = _pool_eid()
    if pool is None:
        return None, None, None, _html(pages.message("Your Host is starting",
                                                     "Try again in a few seconds."), 503)
    if not person.person_email(request):
        return None, None, None, _html(pages.message(
            "Sign in to your Host first",
            "Only you can answer an agent's request. Sign in, then open the link again.",
            error=True), 401)
    ask = await store.get_ask(pool, eid, ask_id)
    if ask is None:
        return None, None, None, _html(pages.message("No such request",
                                                     "This link names no request on this Host.",
                                                     error=True), 404)
    return pool, eid, ask, None


def _closed_words(ask: dict) -> str:
    if ask.get("connection_revoked_at") is not None:
        return "The agent that asked has been disconnected."
    state = ask.get("state")
    if state == "once":
        return "You allowed it once. The agent can now run it."
    if state == "always":
        return "You allowed it for this agent from now on."
    if state == "refused":
        return "You said no to it."
    if state == "spent":
        return "It was allowed once, and has run."
    return "It has expired. The agent can ask again."


async def _card(pool, eid: int, ask: dict) -> dict | None:
    """The open card a machine ask points at, or None."""
    waiting = ask.get("waiting") or {}
    if waiting.get("via") != "card" or not waiting.get("ticket_id"):
        return None
    from server.routes import tickets
    try:
        t = await tickets.get_ticket_service(pool, eid, int(waiting["ticket_id"]))
    except Exception:  # noqa: BLE001 — a card that cannot be read is shown as closed
        return None
    return t if t.get("status") == "open" else None


async def _render(request, pool, eid: int, ask: dict, error: str = "",
                  status: int = 200) -> HTMLResponse:
    import datetime as _dt
    expired = ask["expires_at"] <= _dt.datetime.now(_dt.timezone.utc)
    if ask.get("connection_revoked_at") is not None or expired:
        return _gone(_closed_words(ask))
    if ask["kind"] == store.GRANT_ASK:
        if ask["state"] != "waiting":
            return _gone(_closed_words(ask))
        from server.network import nac_approval
        token = await person.sign_form(pool, {"k": "ask", "id": ask["id"]})
        factors = await person.factors(pool, request)
        nonce = pages.passkey_nonce(factors)
        return _html(pages.grant_ask_page(client_name=ask["client_name"], server=ask["server"],
                                          op=ask["op"], args=ask.get("args") or [],
                                          note=ask.get("note") or "", form_token=token,
                                          error=error, reads=not nac_approval.op_writes(ask["op"]),
                                          factors=factors, nonce=nonce),
                     status, nonce)
    card = await _card(pool, eid, ask)
    if card is not None:
        token = await person.sign_form(pool, {"k": "card", "id": ask["id"],
                                              "ticket": int(card["id"])})
        return _html(pages.machine_ask_page(client_name=ask["client_name"], server=ask["server"],
                                            op=ask["op"], where="", form_token=token,
                                            card_words=str(card.get("sitrep") or ""),
                                            error=error), status)
    where = (ask.get("waiting") or {}).get("where") or (
        "Your Host passed it on to the machine. If the agent's next call still waits, approve it "
        "where the machine asks: on your approval device, or at the machine itself.")
    return _html(pages.machine_ask_page(client_name=ask["client_name"], server=ask["server"],
                                        op=ask["op"], where=where, error=error), status)


@router.get("/agent-access/ask/{ask_id}")
async def ask_page(ask_id: str, request: Request):
    pool, eid, ask, refusal = await _load(request, ask_id)
    if refusal is not None:
        return refusal
    return await _render(request, pool, eid, ask)


@router.post("/agent-access/ask/{ask_id}")
async def ask_answer(ask_id: str, request: Request):
    pool, eid, ask, refusal = await _load(request, ask_id)
    if refusal is not None:
        return refusal
    try:
        form = await _form(request)
    except store.AgentAccessError as err:
        return _html(pages.message("That answer could not be read", err.detail, error=True),
                     err.status)
    signed = await person.read_form(pool, form.get("form") or "")
    if not signed or signed.get("id") != ask["id"]:
        return _html(pages.message("This page has expired",
                                   "Open the agent's link again to answer it.", error=True), 400)
    decision = form.get("decision") or ""

    if signed.get("k") == "ask":
        if decision not in (store.ONCE, store.ALWAYS, store.REFUSE):
            return await _render(request, pool, eid, ask, "Choose one of the three answers.", 400)
        if decision != store.REFUSE:
            from server.network.nac_stepup import StepupLocked
            try:
                ok, why = await person.confirm(pool, request, form.get("code") or "")
            except StepupLocked as locked:
                ok, why = False, person.locked_words(locked)
            if not ok:
                return await _render(request, pool, eid, ask, why, 403)
        try:
            await store.decide_ask(pool, eid, ask["id"], decision)
        except store.AgentAccessError as err:
            return _gone(err.detail)
        log.info("agent ask %s answered: %s", ask["id"], decision)
        return _html(pages.message("Done", _closed_words({**ask, "state": decision})
                                   + " You can close this page."))

    if signed.get("k") == "card":
        from server.network import nac_ops
        from server.routes import tickets
        if decision not in (nac_ops.CHOICE_ONCE, nac_ops.CHOICE_NO):
            return await _render(request, pool, eid, ask, "Choose one of the two answers.", 400)
        try:
            await tickets.decide_ticket_service(pool, eid, int(signed["ticket"]), option_id=decision,
                                                actor="user")
        except tickets.TicketError as err:
            return _gone(err.detail)
        if decision == nac_ops.CHOICE_NO:
            return _html(pages.message("Done", "It will not run. You can close this page."))
        return _html(pages.message(
            "Approved here",
            f"{ask['server']} may still ask you to confirm on your approval device or at the "
            "machine itself; it runs once you have. You can close this page."))

    return _html(pages.message("This page has expired",
                               "Open the agent's link again to answer it.", error=True), 400)


# ── Settings: the person's connected agents ─────────────────────────────────

def _needs(permission: str):
    """The Settings app's own scopes (read:session / write:session), the
    ones the list this tab replaced took."""
    from server.applications.middleware import requires_permission
    return Depends(requires_permission(permission))


@router.get("/api/agent-access/connections", dependencies=[_needs("read:session")])
async def connections():
    pool, eid = _pool_eid()
    if pool is None:
        raise HTTPException(503, "host not ready")
    rows = await store.list_connections(pool, eid)
    for r in rows:
        r["redirect_hosts"] = sorted({u.split("/")[2] for u in r.pop("redirect_uris") or []
                                      if u.count("/") >= 2})
    return {"connections": rows}


@router.post("/api/agent-access/connections/{connection_id}/revoke",
             dependencies=[_needs("write:session")])
async def revoke_connection(connection_id: int):
    pool, eid = _pool_eid()
    if pool is None:
        raise HTTPException(503, "host not ready")
    if not await store.revoke_connection(pool, eid, connection_id):
        raise HTTPException(404, "no such connected agent")
    return {"ok": True}


@router.post("/api/agent-access/grants/{grant_id}/revoke", dependencies=[_needs("write:session")])
async def revoke_grant(grant_id: int):
    pool, eid = _pool_eid()
    if pool is None:
        raise HTTPException(503, "host not ready")
    if not await store.revoke_grant(pool, eid, grant_id):
        raise HTTPException(404, "no such permission")
    return {"ok": True}
