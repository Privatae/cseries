"""
PeerClient — dial a peer host's `:8444` directly and present a bridge-signed slip.

Addressing is DETERMINISTIC from the peer's entity_id: each entity namespace has a
stable ClusterIP Service `entity-<id>`, so the peer is
`entity-<id>.entity-<id>.svc.cluster.local:8444` — stable across pod-IP churn, no
address baked into the grant, no resolve op. We learn the peer's entity_id as the
slip's `issuer` (the peer issued us the slip).

We present our own relay cert (CN=domain-<us>) as the mTLS client cert (WHO) and the
held slip (WHAT). The peer verifies both locally. We validate the peer's server cert
chains to the CEIGAS CA; hostname check is off because we dial by the service DNS
name, not the cert SAN (the handle). (Pinning the peer's cert to its entity_id is a
hardening follow-up — k8s service routing already guarantees entity-<id> → that pod.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl

log = logging.getLogger("peer.client")

PEER_PORT = int(os.environ.get("PEER_PORT", "8444"))
CLIENT_CERT = os.environ.get("CEIGAS_RELAY_CERT", "/etc/privatae/certs/relay.crt")
CLIENT_KEY = os.environ.get("CEIGAS_RELAY_KEY", "/etc/privatae/certs/relay.key")
CA_BUNDLE = os.environ.get("MTLS_CA_BUNDLE", "/etc/privatae/certs/ceigas_ca.crt")
PEER_DNS_SUFFIX = os.environ.get("PEER_DNS_SUFFIX", "svc.cluster.local")

_client_ctx: ssl.SSLContext | None = None


class PeerUnavailable(Exception):
    """The direct peer lane could not be used — caller should fall back to the
    bridge relay. Never raised for a slip refusal (that's an authoritative no)."""


#: The receiver's frame ceiling (peer_server._MAX_REQ). Held here too so a
#: sender can refuse a frame that could never be read, instead of retrying it.
MAX_FRAME_BYTES = 256 * 1024

#: The ceiling on one ANSWER line. asyncio's StreamReader default is 64 KiB, and
#: a full roster page (lane.ROSTER_PAGE members) measures about 75 KB before
#: display names, so a group past ~430 members could never be synced: readline
#: overran, the sync read it as the owner being unreachable, and retried for
#: ever. peer_server raised its own read limit for the same reason.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Connections this Host holds open to ONE peer at once. The receiver serves at
#: most this many per sending identity and closes the rest unanswered
#: (peer_server._MAX_CONNS_PER_PEER), so a burst waits HERE for a free
#: connection — the way a browser queues requests past its per-host limit —
#: instead of being closed and handed to the relay, which reaches only peers
#: on the same node.
MAX_CONNS_PER_PEER = 6

_SLOTS: dict = {}       # peer id -> [event loop, Semaphore, callers using it]
# bound at import: tests swap this module's `asyncio` for a narrow fake
_Semaphore, _running_loop, _SlotTimeout = asyncio.Semaphore, asyncio.get_running_loop, asyncio.TimeoutError


async def _take_slot(peer_entity_id, timeout: float):
    """Wait (up to `timeout`) for one of the MAX_CONNS_PER_PEER connections to
    this peer; returns the entry to give back with _give_slot."""
    loop = _running_loop()
    ent = _SLOTS.get(peer_entity_id)
    if ent is None or ent[0] is not loop:
        ent = _SLOTS[peer_entity_id] = [loop, _Semaphore(MAX_CONNS_PER_PEER), 0]
    ent[2] += 1
    try:
        await asyncio.wait_for(ent[1].acquire(), timeout)
    except BaseException:
        _drop_user(peer_entity_id, ent)
        raise
    return ent


def _give_slot(peer_entity_id, ent) -> None:
    ent[1].release()
    _drop_user(peer_entity_id, ent)


def _drop_user(peer_entity_id, ent) -> None:
    ent[2] -= 1
    if ent[2] <= 0 and _SLOTS.get(peer_entity_id) is ent:
        _SLOTS.pop(peer_entity_id, None)


def encode_frame(op: str, slip: dict | None, params: dict) -> bytes:
    """THE wire encoding of one request — compact separators, one line.

    One function because two callers measure it: send_to_peer writes it, and
    app_lane sizes an outbox row against MAX_FRAME_BYTES before queueing. The
    first cut serialized here with json's default ", " / ": " separators while
    the size check used compact ones, so a payload that passed the check grew
    ~20% on the wire and was refused by the receiver."""
    return (json.dumps({"op": op, "slip": slip, "params": params},
                       separators=(",", ":")) + "\n").encode()


def _peer_host(entity_id: int) -> str:
    return f"entity-{int(entity_id)}.entity-{int(entity_id)}.{PEER_DNS_SUFFIX}"


def _ctx() -> ssl.SSLContext:
    global _client_ctx
    if _client_ctx is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CLIENT_CERT, CLIENT_KEY)
        ctx.load_verify_locations(CA_BUNDLE)
        ctx.check_hostname = False          # dialed by svc DNS, not the cert SAN
        ctx.verify_mode = ssl.CERT_REQUIRED  # peer must present a valid CEIGAS cert
        _client_ctx = ctx
    return _client_ctx


async def knock_peer(peer_entity_id: int, params: dict, *, timeout: float = 10.0) -> dict:
    """Send a SLIPLESS request (`inbox.request`) — the op that starts a relationship.

    A thin wrapper over send_to_peer with slip=None, and it exists as its own name
    so a slipless call is never something a caller reaches by accident: passing
    None where a slip belongs should look deliberate at the call site. The receiver
    classifies the op (peer_server._SLIPLESS_OPS) and its own domain_policy decides
    admission; this side proves only WHO, via the client cert.

    Same contract as send_to_peer: PeerUnavailable on transport failure so the
    caller can fall back to the bridge; a refusal comes back as {ok: False}.
    """
    return await send_to_peer(peer_entity_id, "inbox.request", None, params, timeout=timeout)


async def grant_peer(peer_entity_id: int, params: dict, *, timeout: float = 10.0) -> dict:
    """Send a SLIPLESS GRANT (`inbox.grant`) — the answer that completes a
    relationship, and the other half of what knock_peer starts.

    Its own name for the same reason knock_peer has one: passing None where a
    slip belongs should look deliberate at the call site, never reached by
    accident. The receiver's precondition does the gating (it must be waiting on
    a request from us); this side proves only WHO, via the client cert.

    Same contract as send_to_peer: PeerUnavailable on transport failure so the
    caller can fall back to the bridge; a refusal comes back as {ok: False}.
    """
    return await send_to_peer(peer_entity_id, "inbox.grant", None, params, timeout=timeout)


async def send_to_peer(peer_entity_id: int, op: str, slip: dict | None, params: dict,
                       *, timeout: float = 10.0) -> dict:
    """Direct call to a peer's :8444. Returns the peer's response dict
    ({ok, data|error}). Raises PeerUnavailable on a transport failure (connect/
    TLS/timeout) so the caller can fall back to the bridge relay. A slip refusal is
    returned as {ok: False, error: ...}, NOT raised (it's an authoritative answer)."""
    host = _peer_host(peer_entity_id)
    payload = encode_frame(op, slip, params)
    try:
        slot = await _take_slot(peer_entity_id, timeout)
    except _SlotTimeout as e:
        raise PeerUnavailable(f"no free connection to {host} within {timeout:g} s") from e
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, PEER_PORT, ssl=_ctx(),
                                        server_hostname=host, limit=MAX_RESPONSE_BYTES),
                timeout)
        except Exception as e:
            raise PeerUnavailable(f"connect {host}:{PEER_PORT} failed: {type(e).__name__}: {e}") from e
        try:
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout)
            line = await asyncio.wait_for(reader.readline(), timeout)
        except Exception as e:
            raise PeerUnavailable(f"io to {host} failed: {type(e).__name__}: {e}") from e
        finally:
            try:
                writer.close()
            except Exception:
                pass
    finally:
        _give_slot(peer_entity_id, slot)
    if not line:
        raise PeerUnavailable(f"empty response from {host}")
    try:
        return json.loads(line)
    except Exception as e:
        raise PeerUnavailable(f"bad response from {host}: {e}") from e
