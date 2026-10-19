"""The connector route family: /api/connector/*.

A connector app runs in its own process-form runtime pod and reaches its Host
on :8800 with no app window and so no app token. It authenticates with the
connector capability (server/network/nac_connector.py), sent as two headers:

    X-CSeries-App-Id:          the connector app's id
    X-CSeries-Connector-Cap:   the capability, from env CSERIES_CONNECTOR_CAP

EVERY route here depends on require_connector_cap(...). That dependency is the
boundary, and the only one: the four gate lists that let this family past the
HTTP layer (localhost_only._SELF_AUTH_PREFIXES, ceigas_gate.BYPASS_PREFIXES,
the app-token middleware's _ALLOWLIST_PREFIXES and route_policy's own-auth
entries) let a request reach THIS check, nothing more.
scripts/test_connector_grants.py walks the app's route table and fails for a
/api/connector/ route without the dependency, and posts to every one of them
with no capability.

Stages: a capability minted when a pairing started opens
POST /pair and POST /platform, and only while that app has an open pairing
('pairing'): the connector needs the platform (Telegram getUpdates) to receive
the code it reports. Once the person approves the account, the same capability
opens the message routes ('active'), which start the person's turns: those
are approval-only. The grant is read from the database on every request.

The message routes (4.2-4.5) are server/connectors/turns.py behind this file:
POST /messages takes one message and streams NDJSON parts, POST /messages/sent
records what the connector sent, GET /turns/{id} reattaches. WebSocket
/gateway is Discord's gateway, held by the kernel (connectors/gateway.py).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketException
from fastapi.responses import StreamingResponse
from starlette.requests import HTTPConnection

from server import brain_service
from server.connectors import gateway as gw
from server.connectors import turns as ct
from server.network import nac_connector as nc

log = logging.getLogger("privatae.routes.connector")

router = APIRouter(prefix="/api/connector", tags=["connector"])

PREFIX = "/api/connector/"
_BODY_MAX = 16 * 1024

# The attribute the dependency carries, so a gate can see from the route table
# alone that a route has it.
STAGES_ATTR = "_connector_stages"


def require_connector_cap(*stages: str):
    """FastAPI dependency: the caller is a connector app holding a live
    capability, and its app is at one of `stages` right now ('pairing',
    'matched', 'active'). Returns the nac_connector.ConnectorCaller (also on
    request.state.connector). 403 without a capability that matches, or at the
    wrong stage; 503 when the grant cannot be read — a failed read refuses."""
    known = (nc.STAGE_PAIRING, nc.STAGE_MATCHED, nc.STAGE_ACTIVE)
    if not stages or any(s not in known for s in stages):
        raise ValueError(f"connector stages must be among {known}, got {stages!r}")
    wanted = frozenset(stages)

    def _refuse(request: HTTPConnection, status: int, detail: str, headers=None):
        # A WebSocket is refused before it is accepted: closed with 1008 (a
        # refusal) or 1013 (try again later), which the server answers as a
        # refused handshake. An HTTPException raised for a socket is never
        # sent to it.
        if request.scope.get("type") == "websocket":
            return WebSocketException(1013 if status == 503 else 1008, detail[:120])
        return HTTPException(status, detail, headers=headers)

    async def _dep(request: HTTPConnection) -> nc.ConnectorCaller:
        app_id = (request.headers.get(nc.APP_ID_HEADER) or "").strip()
        cap = (request.headers.get(nc.CAP_HEADER) or "").strip()
        if not app_id or not cap:
            raise _refuse(request, 403, "connector capability required")
        try:
            caller = await nc.check_connector_cap(brain_service.get_pool(), app_id, cap)
        except Exception as e:  # noqa: BLE001 — a failed read refuses
            log.warning("connector %s: grant read failed, refusing: %r", app_id, e)
            raise _refuse(request, 503, "your Host could not read this connector's grant, so it "
                                        "refused the request; try again shortly",
                          headers={"Retry-After": "5"}) from None
        if caller is None:
            raise _refuse(request, 403, "connector capability required")
        if not (caller.stages & wanted):
            raise _refuse(request, 403, "this connector is not paired" if nc.STAGE_ACTIVE in wanted
                          else "no pairing is open for this connector")
        request.state.connector = caller
        return caller

    setattr(_dep, STAGES_ATTR, wanted)
    return _dep


async def _body(request: Request, max_bytes: int = _BODY_MAX) -> dict:
    """The JSON body, read under `max_bytes`: a larger one is refused before
    it is read whole."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(413, "body too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > max_bytes:
            raise HTTPException(413, "body too large")
    raw = bytes(buf)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body


# ── pairing (4.9 step 2) ─────────────────────────────────────────────────

@router.post("/pair")
async def connector_pair(
        request: Request,
        caller: nc.ConnectorCaller = Depends(require_connector_cap(nc.STAGE_PAIRING))):
    """The connector reports the one-time code an account sent its bot, with
    that account's id and display name: {code, account_id, display_name}. A
    matching code binds the account to the draft and files the person's
    approval card; it does not let the account in."""
    body = await _body(request)
    try:
        return await nc.submit_code(brain_service.get_pool(), app_id=caller.app_id,
                                    code=body.get("code"), account_id=body.get("account_id"),
                                    display_name=body.get("display_name"))
    except nc.ConnectorError as e:
        raise HTTPException(e.status, e.detail) from None


# ── the platform call, through the vault broker (4.1) ────────────────────

# A platform call with files (a picture the reply carries) is base64 in JSON:
# the files' bound (nac_connector.PLATFORM_FILES_TOTAL_MAX) at 4/3, plus the
# form fields. Without files the ordinary bound applies.
_PLATFORM_BODY_MAX = (nc.PLATFORM_FILES_TOTAL_MAX * 4) // 3 + 64 * 1024

# Platform calls one connector app may have in flight at once. A connector
# long-polls with one and sends a reply's parts with a few; past this it is a
# loop, and each call can hold up to _PLATFORM_BODY_MAX of the kernel's
# memory. A cap that protects the Host, never one on the person's use.
PLATFORM_INFLIGHT_MAX = 8
_platform_inflight: dict[str, int] = {}


@router.post("/platform")
async def connector_platform(
        request: Request,
        caller: nc.ConnectorCaller = Depends(
            require_connector_cap(nc.STAGE_PAIRING, nc.STAGE_MATCHED, nc.STAGE_ACTIVE))):
    """One call to the platform's API with the bot token the grant names,
    injected by the broker: {method, path, query?, json? | form?, files?,
    timeout?, use?}. The host is the platform's API host and the token is the
    grant's (before approval, the open pairing's); the connector chooses
    neither.

    `files` makes it a multipart/form-data upload, built by the kernel:
    [{field, filename, data_b64, mime?}] beside `form`'s text fields (Telegram
    sendPhoto: field "photo"; sendDocument: "document").

    Open at the pairing stage too: a connector whose bot
    token lives only in the vault needs getUpdates to receive the pairing code
    it reports, and after the code matched, while the person has not approved
    yet, to say the code was accepted and keep reading. What the platform call reaches is the platform and nothing
    else, so this opens no turn and no part of the Host.

    Counted before the body is read (PLATFORM_INFLIGHT_MAX): an upload can
    hold up to _PLATFORM_BODY_MAX of the kernel's memory, so a connector
    caught in a retry loop is refused before it is buffered."""
    held = _platform_inflight.get(caller.app_id, 0)
    if held >= PLATFORM_INFLIGHT_MAX:
        raise HTTPException(429, f"{PLATFORM_INFLIGHT_MAX} platform calls are already in "
                                 "flight for this connector; wait for one to finish")
    _platform_inflight[caller.app_id] = held + 1
    try:
        body = await _body(request, _PLATFORM_BODY_MAX)
        # Measured as the bytes it is: json.dumps escapes every non-ASCII
        # character to six, which turned a full Cyrillic message into a 413.
        # surrogatepass: a lone "\ud800" escape in the body is a JSON string
        # Python reads, and a strict encode of it raised (a 500, not a size).
        if body.get("files") is None and len(json.dumps(body, ensure_ascii=False).encode(
                "utf-8", "surrogatepass")) > _BODY_MAX:
            raise HTTPException(413, "body too large")
        try:
            return await nc.platform_call(brain_service.get_pool(), caller, body)
        except nc.ConnectorError as e:
            raise HTTPException(e.status, e.detail) from None
    finally:
        left = _platform_inflight.get(caller.app_id, 1) - 1
        if left > 0:
            _platform_inflight[caller.app_id] = left
        else:
            _platform_inflight.pop(caller.app_id, None)


# ── the Discord gateway, held by the Host ────────────────────────────────

# A revoke closes the app's gateway socket at once, through the same hook that
# cancels its turns.
nc.register_turn_canceller(gw.close_for_revoke)

# The stages /platform is open at, and so the gateway: a Discord connector
# receives the pairing code over this socket.
_GATEWAY_STAGES = (nc.STAGE_PAIRING, nc.STAGE_MATCHED, nc.STAGE_ACTIVE)


def _gateway_recheck(websocket: WebSocket):
    """The handshake's capability check, run again on the same headers: the
    caller while the capability passes at the gateway's stages, else None. A
    failed read raises (gateway.relay closes the socket on it)."""
    app_id = (websocket.headers.get(nc.APP_ID_HEADER) or "").strip()
    cap = (websocket.headers.get(nc.CAP_HEADER) or "").strip()
    wanted = frozenset(_GATEWAY_STAGES)

    async def recheck():
        again = await nc.check_connector_cap(brain_service.get_pool(), app_id, cap)
        if again is None or not (again.stages & wanted):
            return None
        return again
    return recheck


@router.websocket("/gateway")
async def connector_gateway(
        websocket: WebSocket,
        caller: nc.ConnectorCaller = Depends(require_connector_cap(*_GATEWAY_STAGES))):
    """The Discord Gateway WebSocket, opened by the kernel for the connector
    (server/connectors/gateway.py): the app's text frames go on to Discord
    with the grant's bot token put into Identify and Resume (op 2 and 6,
    d.token), and Discord's come back. ?url= names the resume_gateway_url
    READY gave; any host but discord.gg or a subdomain of it is refused.
    Open at the stages /platform is: a Discord connector receives the pairing
    code over this socket. One per app; a revoke closes it, and so does the
    capability no longer passing that check (read again on a clock and before
    every frame the token goes into: gateway.CAP_RECHECK_S)."""
    if caller.platform != "discord":
        raise WebSocketException(1008, "the gateway is Discord's; this connector is not")
    pool = brain_service.get_pool()
    if pool is None:
        raise WebSocketException(1013, "brain pool not ready")
    try:
        await gw.relay(websocket, caller, resume_url=websocket.query_params.get("url"),
                       pool=pool, recheck=_gateway_recheck(websocket))
    except gw.Refused as e:
        raise WebSocketException(e.code, e.reason[:120]) from None


# ── the message routes (4.2-4.5) ─────────────────────────────────────────
#
# Approved grants only. The caller (nac_connector.ConnectorCaller) carries the
# grant id, the platform and the account id from the grant's SIGNED terms.

_MESSAGE_BODY_MAX = 64 * 1024
_NDJSON = "application/x-ndjson"
_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# Revoking a grant cancels its connector's turn in flight, and ends the
# streams of every reply of that grant with nothing more.
nc.register_turn_canceller(ct.cancel_for_revoke)


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool not ready", headers={"Retry-After": "5"})
    return pool


@router.post("/messages")
async def connector_messages(
        request: Request,
        caller: nc.ConnectorCaller = Depends(require_connector_cap(nc.STAGE_ACTIVE))):
    """One message from the paired account:
    {chat_id, message_id, text, account_id, display_name?, reply_to_message_id?,
    native_thread_id?, origin?}. `origin` says someone else wrote the text (a
    forward, or a message sent through a bot): {kind: forward|via_bot, source:
    user|bot|chat|channel|hidden, name?, id?}; the kernel validates it, labels
    the message's header with it and keeps it on the person's row
    (connectors/turns.py parse_origin).

    Refused (403) before anything is written unless account_id is the
    grant's account. Then the route finds the thread (a command, a reply to a
    mapped message, a mapped platform thread, else the Host's pick), writes
    the message to the map and starts the person's own chat turn there.

    Streams NDJSON (application/x-ndjson), one object per line:
      {"type":"thread","kind","ref","title","changed"}
      {"type":"turn","turn_id","state":"started"}        at once
      {"type":"alive"}                                    while waiting
      {"type":"text"|"image"|"link","seq",...}            when the turn ends, and
                                                          a batch earlier when it
                                                          starts holding on a card
      {"type":"done"}
    A message into a thread whose turn is already running answers
    {"type":"turn","turn_id":<running>,"state":"nudged","mine":bool[,"note"]}
    and done: the reply comes once, on the stream of the turn's own start."""
    body = await _body(request, _MESSAGE_BODY_MAX)
    try:
        msg = ct.parse_message(caller, body)
    except ct.Refused as e:
        raise HTTPException(e.status, e.detail) from None
    pool = _pool()
    return StreamingResponse(ct.stream_message(pool, caller, msg), media_type=_NDJSON,
                             headers=_STREAM_HEADERS)


_SENT_MAX = 200


@router.post("/messages/sent")
async def connector_messages_sent(
        request: Request,
        caller: nc.ConnectorCaller = Depends(require_connector_cap(nc.STAGE_ACTIVE))):
    """What the connector sent for a reply: {turn_id, chat_id, sent:[{seq,
    message_id}], native_thread_id?}. Each message is written to the map in
    the reply's thread, so a reply to it lands there. A message already
    written is not written twice, and a part reported sent is left out of a
    reattach. Only this connector's own replies; refused after revoke."""
    body = await _body(request)
    turn_id = str(body.get("turn_id") or "").strip()
    if not turn_id or len(turn_id) > 64:
        raise HTTPException(400, "turn_id is required")
    try:
        chat_id = ct._id(body.get("chat_id"), "chat_id")
        native = ct._id(body.get("native_thread_id"), "native_thread_id", False)
    except ct.Refused as e:
        raise HTTPException(e.status, e.detail) from None
    sent = body.get("sent")
    if not isinstance(sent, list) or not sent or len(sent) > _SENT_MAX:
        raise HTTPException(400, f"sent must be a list of 1 to {_SENT_MAX} {{seq, message_id}}")
    pairs: list[tuple[int, str]] = []
    for item in sent:
        if not isinstance(item, dict) or isinstance(item.get("seq"), bool) \
                or not isinstance(item.get("seq"), int) or item["seq"] < 0:
            raise HTTPException(400, "each sent item is {seq: a part's seq, message_id}")
        try:
            pairs.append((item["seq"], ct._id(item.get("message_id"), "message_id")))
        except ct.Refused as e:
            raise HTTPException(e.status, e.detail) from None
    pool = _pool()
    eid = ct._eid()
    reply = ct.reply_for(caller, turn_id)
    if reply is not None:
        # A turn the reply followed answers for it (turns._followed_turn):
        # its sends are the reply's, which a reattach reads by its own id.
        turn_id = reply.reply_id
        home = (reply.kind, reply.ref, reply.chat_id)
    else:
        found = await ct.mm.turn_home(pool, eid, caller.app_id, caller.grant_id, turn_id)
        if found is None:
            raise HTTPException(404, "no reply of this connector has that turn_id")
        home = found[:3]
    if chat_id != home[2]:
        raise HTTPException(400, "that reply was for another chat")
    if not home[0]:
        return {"turn_id": turn_id, "recorded": 0}
    n = await ct.mm.record_sent(pool, eid, app_id=caller.app_id, grant_id=caller.grant_id,
                                platform=caller.platform,
                                chat_id=chat_id, turn_id=turn_id, kind=home[0], ref=home[1],
                                sent=pairs, native_thread_id=native)
    return {"turn_id": turn_id, "recorded": n}


@router.get("/turns/{turn_id}")
async def connector_turn(
        turn_id: str,
        caller: nc.ConnectorCaller = Depends(require_connector_cap(nc.STAGE_ACTIVE))):
    """Reattach to a reply of this connector's: the same NDJSON from the
    start (the thread, the turn, its parts without those already reported
    sent, done) while the turn registry keeps it. Past that window
    (DONE_TTL_S): {"type":"turn","state":"gone"} and a link to the thread,
    where the reply is. Only this connector's own replies; refused after
    revoke."""
    pool = _pool()
    reply = ct.reply_for(caller, turn_id)
    if reply is not None:
        return StreamingResponse(ct.stream_reply(pool, reply, attached=True), media_type=_NDJSON,
                                 headers=_STREAM_HEADERS)
    lines = await ct.gone_lines(pool, caller, turn_id)
    if lines is None:
        raise HTTPException(404, "no reply of this connector has that turn_id")

    async def _gone():
        for s in lines:
            yield s
    return StreamingResponse(_gone(), media_type=_NDJSON, headers=_STREAM_HEADERS)
