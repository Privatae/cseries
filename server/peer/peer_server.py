"""
PeerServer — the in-pod `:8444` mTLS terminator for direct host↔host transport.

Per connection: terminate mTLS (CERT_REQUIRED, CEIGAS CA), read the peer's
client-cert CN → peer entity_id (WHO), read one line-framed JSON request
`{op, slip, params}`, OFFLINE-verify the slip (WHAT) with the pinned trust state
(presenter==grantee, issuer==this host, not expired/revoked, op∈allowed_ops),
then dispatch to the registered handler. One request/response per connection
(simple + safe; the inbox op is a single round-trip). No bridge in the data path.

Distinct from the dashboard `:8443` terminator (mtls_frontdoor.py): that one is
owner/router-only and proxies HTTP; this is the cross-tenant peer lane and is
slip-gated. `:8444` is intentionally NOT in the cross-tenant ingress-deny.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl

from server.groups.lane import action_for_group
from server.peer.app_lane import action_for_deliver
from server.peer.slip_verify import slip_key_id, verify_slip

log = logging.getLogger("peer.server")

PEER_PORT = int(os.environ.get("PEER_PORT", "8444"))
BIND_HOST = os.environ.get("PEER_BIND_HOST", "::")
SERVER_CERT = os.environ.get("MTLS_SERVER_CERT", "/etc/privatae/certs/server.crt")
SERVER_KEY = os.environ.get("MTLS_SERVER_KEY", "/etc/privatae/certs/server.key")
CA_BUNDLE = os.environ.get("MTLS_CA_BUNDLE", "/etc/privatae/certs/ceigas_ca.crt")

_MAX_REQ = 256 * 1024
#: How long one request line may take to arrive — and so how long the rest of
#: an over-long frame is read and thrown away after its refusal.
_FRAME_TIMEOUT_S = 15.0
#: Connections served at once, counted per verified
#: peer identity (the client certificate's entity), never per address.
#: Our own sender (peer_client.send_to_peer) opens ONE connection per message
#: and holds at most peer_client.MAX_CONNS_PER_PEER to one peer at once — a
#: burst queues at the sender — so a well-behaved peer is never over this.
#: Per peer: 6, the per-host connection cap browsers ship for exactly this
#: shape — one request per connection, no pipelining (Chromium's
#: kMaxSocketsPerGroup). In all: 100, the floor RFC 9113 §6.5.2 recommends for
#: concurrent requests to one server. Over either: closed unanswered — the
#: sender reads a transport failure (PeerUnavailable), so an outbox row is
#: retried and a direct message goes to the relay.
#: Equal to peer_client.MAX_CONNS_PER_PEER (a test holds the two together).
_MAX_CONNS_PER_PEER = 6
_MAX_CONNS = 100


async def _finish_refusal(reader, writer, deadline: "float | None" = None) -> None:
    """After refusing a frame that is still arriving (past the line limit),
    make sure the refusal is what the peer reads.

    Closing a socket with unread input makes the kernel send a reset, and a
    reset throws away the answer still in flight — so a 2 MiB frame, or a slow
    sender past the limit, saw "connection reset" instead of "frame too
    large". So: half-close the write side where the transport can (TLS
    cannot), then read and discard what the peer still sends, a chunk at a
    time (memory stays one chunk), until it finishes or the frame timeout
    runs out; then the caller closes."""
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except (AttributeError, OSError, RuntimeError, NotImplementedError):
        pass
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + _FRAME_TIMEOUT_S
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return
        try:
            chunk = await asyncio.wait_for(reader.read(64 * 1024), timeout=left)
        except (asyncio.TimeoutError, OSError, ConnectionError):
            return
        if not chunk:
            return
_CN_RE = re.compile(r"domain-(\d+)-")

# Map a wire op to the slip ACTION it requires. A messaging slip is issued with
# allowed_ops=["message"]; the wire op that carries it is "inbox.deliver". The slip
# authorizes the semantic action, not the transport verb.
_OP_ACTION = {
    "inbox.deliver": "message",
    # app.grant hands a peer an APP slip; it rides on the message slip that
    # peer already holds from us, i.e. an accepted contact (app_lane.py).
    "app.grant": "message",
    # group.grant is the INVITE: it hands a member the GROUP slip, so the slip
    # it would otherwise present is the one it is carrying. Same structure and
    # same resolution as app.grant — it rides the message slip the member
    # already issued us, i.e. an accepted contact. The bound this imposes is
    # real and deliberate: you cannot invite a stranger to a group circle.
    "group.grant": "message",
    # contact.slip hands the accepter a slip BACK; it rides the message slip the
    # accepter issued in its grant, so only a host that was accepted can send one.
    "contact.slip": "message",
}

# Ops whose slip ACTION depends on the request: app.deliver is authorized by
# app:<app_id> for the app named in its params, so a slip for app X can never
# carry app Y. The function returns None when the params name no valid app,
# and None is refused as an unknown op below — never verified as "no action".
_OP_ACTION_FROM_PARAMS = {
    "app.deliver": action_for_deliver,
    # The group ops are authorized by group:<group_id> for the group named in
    # their params, so a slip for group X can never carry group Y — the same
    # property action_for_deliver gives the app lane. action_for_group returns
    # None when the params name no valid group id, and None is refused as an
    # unknown op below rather than verified as "no action".
    "group.roster_fetch": action_for_group,
    "group.vote": action_for_group,
}

# Ops that carry NO slip, because a slip is the thing they exist to ask for.
#
# THE BOOTSTRAP PROBLEM. This lane is slip-gated end to end, and a slip is issued
# only when someone ACCEPTS a connection request. So the one message that starts a
# relationship could never travel on the lane built for the relationship: it had to
# go via the bridge, and the bridge keeps its connections in an in-process dict per
# DaemonSet pod with no forwarding between pods (measured: two Hosts on two
# different bridge pods got "entity <id> not connected", 503). Two hosts
# could talk only if they happened to land on the same bridge pod. Consistent with
# the estate: zero host↔host messages and zero held slips existed.
#
# WHY A SLIPLESS OP IS SAFE HERE — narrow on purpose:
#   * WHO is already proven. mTLS CERT_REQUIRED against the CEIGAS CA, peer_id from
#     the client cert CN. A caller cannot assert an identity it holds no cert for.
#     The header above states the split: cert = WHO, slip = WHAT. A knock needs WHO.
#   * WHAT is nothing. An endpoint_request authorizes no reach, no data, no
#     capability. It puts a card in an inbox; the RECEIVER decides.
#   * The receiver's path is unchanged. The handler calls the same
#     handle_inbox_append the bridge path calls, so this adds a TRANSPORT, not an
#     exemption: nothing reaches the inbox by a route the bridge could not already
#     have taken.
#
# AND A CORRECTION, because an earlier version of this comment claimed the wrong
# thing and the claim spread: domain_policy does NOT gate request kinds.
# check_inbound_allowed returns True for them before touching any table
# (domain_policy.py:176-177), so a `closed` posture, a per-peer deny rule and a
# `blocked` contact are ALL bypassed for a knock. That is true of the bridge path
# too — it is not a regression — but "domain_policy still gates it" was false and
# must not be leaned on. The real bound is narrower and worth stating plainly:
# anyone holding a valid CEIGAS cert can append a request card to any pod, and the
# only control is the USER'S accept decision. What makes that tolerable is that a
# request confers nothing until accepted; what makes it bearable is the per-peer
# cap in handlers.py, because without one "can append a card" means "can append
# cards without limit".
#
# WHAT THIS DOES NOT CLOSE, stated rather than left for a reviewer: the TLS context
# loads the CA bundle with CERT_REQUIRED and sets NO CRL flags, and PeerTrust
# tracks revoked CONTRACT ids (slips), not revoked certs. A peer whose cert was
# revoked can still complete this handshake. For inbox.deliver that is covered —
# the slip is the authority and slips are revocable. For a slipless op the cert is
# the sole authority, so the exposure is real, and bounded to exactly what the op
# can do: put a request card in an inbox that domain_policy still gates. That is
# unrevoked spam, not escalation. Closing it means CRL checking at the TLS layer,
# which is a separate change and belongs with every other lane sharing this CA.
# inbox.grant is the ANSWER to a knock, and it cannot be
# slip-gated for the same reason the knock cannot: the slip it would need is the
# one it is carrying. Its gate is a precondition instead of a credential — the
# receiver must hold an OPEN, RECENT connection request with that peer
# (handlers._relationship_open_with), which is strictly stronger than the knock's
# per-peer cap: a stranger has no pending row and is refused outright. Note the
# row is written both when we ASK and when we ACCEPT — the schema defines it that
# way (v048:20) — so the property is "the user acted", not "we spoke first";
# handlers.py carries the full reasoning. It fails CLOSED, because unlike a knock
# a grant installs a credential (its contact_slip lands in held_slips).
# machine.token is the THIRD op that cannot be
# slip-gated, and the reason is the sharpest of the three: what it returns IS a
# credential — a single-use bastion enrolment token — so a slip the caller would
# have to hold first is exactly what does not exist yet. Its gate is a
# precondition, like inbox.grant's and stronger: THIS host must have an open,
# recent machine request out to that peer (handlers.machine_token →
# machine_offers.mint_for_peer), a row only a user of this host can create. A
# stranger with a valid CEIGAS cert has no such row and is refused outright.
# Rate and count bounds sit with the row, because "they may ask us to mint"
# without them would mean "they may mint at will".
_SLIPLESS_OPS = {"inbox.request", "inbox.grant", "machine.token"}


def _cn_to_entity_id(cn: str | None):
    m = _CN_RE.search(cn or "")
    return int(m.group(1)) if m else None


def _extract_cn(peercert) -> str | None:
    if not peercert:
        return None
    for rdn in peercert.get("subject", ()):
        for k, v in rdn:
            if k == "commonName":
                return v
    return None


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(SERVER_CERT, SERVER_KEY)
    ctx.load_verify_locations(CA_BUNDLE)
    ctx.verify_mode = ssl.CERT_REQUIRED  # only a valid CEIGAS cert can even connect
    return ctx


class PeerServer:
    def __init__(self, *, trust, handlers: dict, my_entity_id: int):
        self._trust = trust              # PeerTrust
        self._handlers = handlers        # {op: async (peer_id, slip_info, params) -> dict}
        self._me = int(my_entity_id)
        self._server = None
        self._open: dict = {}           # peer identity -> connections being served now
        self._open_total = 0

    async def _serve(self, peer_id, req: dict) -> dict:
        if peer_id is None:
            return {"ok": False, "error": "no peer identity (client cert CN)"}
        # A request that is not one is ANSWERED, never raised on: a non-dict
        # body or an unhashable op used to raise here and close the
        # connection unanswered.
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            return {"ok": False, "error": "malformed request"}
        op = req.get("op")
        slip = req.get("slip")
        params = req.get("params") or {}
        handler = self._handlers.get(op)
        slipless = op in _SLIPLESS_OPS
        action = _OP_ACTION.get(op)
        if action is None and op in _OP_ACTION_FROM_PARAMS:
            action = _OP_ACTION_FROM_PARAMS[op](params if isinstance(params, dict) else {})
        # An op is dispatchable only if it is registered AND classified: it either
        # maps to a slip action or is explicitly slipless. A handler that is neither
        # is a wiring mistake, and defaulting such an op to "no slip needed" is how a
        # gated op would silently become ungated — the shape of the bastion's own
        # opFamily bug, where an unknown name fell through to ALLOW.
        if handler is None or (action is None and not slipless):
            return {"ok": False, "error": f"unknown op '{op}'"}
        if slipless:
            # No slip to verify, and nothing below this point applies: the trust
            # state exists to judge slips. WHO came from the client cert in _handle.
            try:
                data = await handler(peer_id, None, params)
            except Exception as e:
                log.warning("peer handler %s raised: %s", op, e)
                return {"ok": False, "error": "handler error", "retryable": True}
            # A handler that REFUSED must not come back as success. The wrapper
            # used to return ok:True whatever the handler said, so a receiver-side
            # failure — pool not ready, INSERT rejected, blank subject — reached
            # the sender as "delivered": the contact was upserted pending, the
            # Peers row stopped offering to send, and the user was told the
            # request went out. `receiver: True` marks the refusal as the FAR
            # side's authoritative answer, so the caller knows not to retry the
            # same message down another transport into the same gate.
            if isinstance(data, dict) and data.get("ok") is False:
                # `retryable` rides through STRUCTURED: a handler that refused
                # because of its own moment (pool not up, a store that failed)
                # says so with a flag, so a sender's queue never has to parse
                # this host's prose — which a later image may reword.
                return {"ok": False, "receiver": True,
                        "retryable": bool(data.get("retryable")),
                        "error": data.get("error") or "receiver refused", "data": data}
            return {"ok": True, "data": data}
        if not isinstance(slip, dict):
            return {"ok": False, "error": "slip required"}
        # FAIL CLOSED on a stale revocation view: if the trust state hasn't synced the
        # CRL at least once (cold start / bridge flap), an empty _revoked set is NOT a
        # proof of "nothing revoked" — a slip revoked while we were down (still within
        # TTL) would slip through. Refuse delivery until the revocation view is fresh.
        if not self._trust.ready:
            log.info("peer %s op=%s REFUSED: revocation view not fresh (CRL unsynced)", peer_id, op)
            # retryable: this is the receiver's MOMENT (cold start, bridge flap),
            # not its answer about the request — a sender's queue should try again.
            return {"ok": False, "retryable": True,
                    "error": "slip refused: revocation view not fresh (CRL unsynced)"}
        # Shape first, with the relay's own helper: a slip that is not one is
        # refused in words — it used to raise here (terms a string, key_id a
        # list) and the connection closed with no answer.
        key_id = slip_key_id(slip)
        if key_id is None:
            log.info("peer %s op=%s REFUSED: malformed slip", peer_id, op)
            return {"ok": False, "error": "slip refused: malformed slip"}
        # Pin the slip's signing key if we haven't seen it (rotation / cold start).
        await self._trust.ensure_key(key_id)
        # WHO = peer cert (presenter must be the grantee); WHAT = the slip authorizes
        # this op's ACTION and the slip was issued BY this host (we only honor our
        # own grants).
        ok, reason, info = verify_slip(
            slip, peer_id, action,
            trusted_keys=self._trust.keys, revoked_ids=self._trust.revoked,
            expected_issuer=self._me)
        if not ok:
            log.info("peer %s op=%s REFUSED: %s", peer_id, op, reason)
            return {"ok": False, "error": f"slip refused: {reason}"}
        try:
            data = await handler(peer_id, info, params)
        except Exception as e:
            log.warning("peer handler %s raised: %s", op, e)
            return {"ok": False, "error": "handler error", "retryable": True}
        # Same rule as the slipless path above, applied here too. This one was
        # pre-existing: a refused inbox.deliver came back as ok:True, and circles'
        # _try_direct_peer_send treats that as delivered and writes the sender's
        # outbound copy — a thread that looks two-sided and is not. Found while
        # fixing the same bug one path over; fixing only the new path would have
        # left the older, busier one wrong.
        if isinstance(data, dict) and data.get("ok") is False:
            return {"ok": False, "receiver": True,
                    "retryable": bool(data.get("retryable")),
                    "error": data.get("error") or "receiver refused", "data": data}
        return {"ok": True, "data": data}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # One frame — read, answered, and the rest of a refused one drained —
        # gets _FRAME_TIMEOUT_S in TOTAL, never 15 s to read plus 15 more.
        deadline = asyncio.get_running_loop().time() + _FRAME_TIMEOUT_S
        ssl_obj = writer.get_extra_info("ssl_object")
        peer_id = _cn_to_entity_id(_extract_cn(ssl_obj.getpeercert() if ssl_obj else None))
        # Connections at once, per peer identity and in all. Over either
        # cap the connection is closed UNANSWERED: the sender reads a transport
        # failure, never a final refusal (an outbox row is retried, a direct
        # message relayed). Our own sender never goes past the per-peer cap.
        if (self._open_total >= _MAX_CONNS
                or self._open.get(peer_id, 0) >= _MAX_CONNS_PER_PEER):
            log.info("peer connection over the cap (peer=%s, open=%s/%s): closed for the relay",
                     peer_id, self._open.get(peer_id, 0), self._open_total)
            try:
                writer.close()
            except Exception:
                pass
            return
        self._open[peer_id] = self._open.get(peer_id, 0) + 1
        self._open_total += 1
        try:
            await self._handle_frame(reader, writer, peer_id, deadline)
        finally:
            self._open_total -= 1
            left = self._open.get(peer_id, 1) - 1
            if left > 0:
                self._open[peer_id] = left
            else:
                self._open.pop(peer_id, None)

    async def _handle_frame(self, reader, writer, peer_id, deadline):
        try:
            unread = False
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
            except ValueError:
                # Past the reader's line limit readline() RAISES (a ValueError
                # around LimitOverrunError); it used to be swallowed below and
                # the connection closed unanswered.
                line, unread = None, True
                resp = {"ok": False, "error": (
                    f"frame too large: a request is one line of at most {_MAX_REQ} bytes")}
            except asyncio.TimeoutError:
                line = None
                resp = {"ok": False, "error": (
                    f"request not completed within {_FRAME_TIMEOUT_S:g} s")}
            if line is None:
                pass
            elif not line or len(line) > _MAX_REQ:
                resp = {"ok": False, "error": "empty or oversized request"}
            else:
                try:
                    req = json.loads(line)
                except Exception:
                    resp = {"ok": False, "error": "malformed JSON"}
                else:
                    resp = await self._serve(peer_id, req)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
            if unread:
                await _finish_refusal(reader, writer, deadline)
        except Exception as e:
            log.debug("peer connection error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def start(self):
        ctx = _build_ssl_context()
        # limit= is the StreamReader line ceiling. asyncio's default is 64 KiB,
        # and readline() RAISES past it — the handler above then swallows the
        # error and closes with no response, which the client reads as
        # PeerUnavailable and retries for ever. _MAX_REQ is the frame bound
        # this lane actually enforces (checked after the read), so the reader
        # must be able to hold one such frame. Reproduced: a 70 KB frame
        # produced "chunk is longer than limit".
        self._server = await asyncio.start_server(
            self._handle, BIND_HOST, PEER_PORT, ssl=ctx, limit=_MAX_REQ + 4096,
            # a TLS handshake gets the frame timeout too, not asyncio's 60 s
            ssl_handshake_timeout=_FRAME_TIMEOUT_S)
        socks = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("peer transport listening on %s (slip-gated, entity_id=%s)", socks, self._me)
        return self._server
