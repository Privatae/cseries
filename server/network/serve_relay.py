"""
The serve lane, Host side: a model server on a bastion's machine, reached with
no address to dial.

A C-Series Host pairs with bastions locally (its socket, its lane port, or a
connection it opens to the server), so there is no tunnel address to dial a
bastion's model server at: such a pairing's heartbeat carries no reported_ip
and llm_port 0. The bastion turns the direction round instead, as it does for
egress (client-go/servelane.go): it keeps worker WebSockets parked on this Host
at /bastion-serve, and the Host hands each one connection. The Host names a
SERVICE, never an address: its first frame on a worker is b"S" + {"service":
"llm"} (or "ollama"), and the bastion answers b"A" and carries the connection
to the server set or found on ITS OWN machine, or b"E" + a reason, having
dialled nothing. After b"A" the worker is a byte pipe, b"D" + bytes and b"E"
for the end, both ways, exactly the egress lane's framing (egress_relay.py).

Everything that reads a local model's endpoint here takes an http ORIGIN
(model_backends.list_local_models -> provider_gateway._call_local and the
rest), so a lane model gets one: a listener on this Host's loopback, one per
bastion and service, made the first time it is asked for. Each connection to
it is handed, byte for byte, to one parked worker of THAT bastion, never to
another machine's. The HTTP inside is the client's own; the bastion reads it
through its own scope filter (inferencefilter.go), so a pairing scoped to named
models is held to them there, where the grant is.

The listener is on 127.0.0.1 only, the address this Host's own loopback
services use (the egress gate's proxy, the forge relay). What reaches it is
what runs in this Host's network namespace, which already holds every
credential the model lane could be used with.

The pool is keyed by the bastion's token, and it is the same structure egress
uses (egress_relay.EgressRelay), one instance of its own: a worker serves one
job, then its WebSocket closes and the bastion parks a replacement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from server.network.egress_relay import EgressRelay, RelayWorker

log = logging.getLogger("cseries.serve")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s serve %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

# The services a Host asks the lane for: the bastion's whole list (servelane.go
# serveServices). "llm" and "ollama" carry chat and embeddings
# (model_backends, embed_backends), "pii" the sterilizer (pii_client).
SERVICES = ("llm", "ollama", "pii")

# How long a connection waits for a worker of its bastion to come free. The
# bastion parks a replacement as soon as one takes a job, so this is a bastion
# that is busy (16 streams in flight) or restarting, never the normal path.
DISPATCH_TIMEOUT_S = 20.0

# How long a bastion's listeners stay after its lane was last connected
# (ServeGateway.sweep): past a moment between two jobs, never past a minute.
LISTENER_GRACE_S = 60.0

# How long the bastion has to answer the service frame. It decides before it
# dials (servelane.go serveVerdict), so the answer is immediate.
ANSWER_TIMEOUT_S = 15.0


def _http_error(status: int, reason: str, message: str) -> bytes:
    """A complete HTTP/1.1 answer for a connection the lane could not carry,
    so the client reads a status and the reason instead of a closed socket."""
    body = json.dumps({"error": {"message": message, "type": "bastion_serve_lane"}}).encode()
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body


class ServeWorker(RelayWorker):
    """One worker the bastion parked on /bastion-serve = one connection carried.
    The idle wait, the drop while idle and the Stop are RelayWorker's own; what
    differs is the first exchange: the service frame and the bastion's answer,
    before any byte of the client's."""

    _log = log

    async def _relay(self, head: bytes, c_reader, c_writer):
        service = head.decode()
        await self.ws.send_bytes(b"S" + json.dumps({"service": service}).encode())
        try:
            first = await asyncio.wait_for(self.ws.receive_bytes(), ANSWER_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — a closed or silent worker
            log.info("serve worker %s: no answer to %s (%r)", self.bastion_token_id, service, e)
            await _answer(c_writer, 502, "Bad Gateway",
                          "the machine serving this model did not answer")
            return
        if first[:1] == b"E":
            why = first[1:].decode("utf-8", "replace")[:500]
            log.info("serve worker %s refused %s: %s", self.bastion_token_id, service, why)
            await _answer(c_writer, 502, "Bad Gateway",
                          f"the machine serving this model refused it: {why}")
            return
        if first[:1] != b"A":
            await _answer(c_writer, 502, "Bad Gateway",
                          "the machine serving this model answered something this Host does not read")
            return

        async def c_to_ws():
            try:
                while True:
                    data = await c_reader.read(65536)
                    if not data:
                        break
                    await self.ws.send_bytes(b"D" + data)
            except Exception:
                pass
            try:
                await self.ws.send_bytes(b"E")
            except Exception:
                pass

        async def ws_to_c():
            try:
                while True:
                    frame = await self.ws.receive_bytes()
                    if frame[:1] == b"D":
                        c_writer.write(frame[1:])
                        await c_writer.drain()
                    elif frame[:1] == b"E":
                        break
            except Exception:
                pass
            try:
                c_writer.close()
            except Exception:
                pass

        # The client's end is the bastion's 'E'; the bastion's end closes the
        # client. Either way round, a client that hangs up first ends c_to_ws,
        # and the bastion's half-close ends the rest.
        await asyncio.gather(c_to_ws(), ws_to_c())


async def _answer(c_writer, status: int, reason: str, message: str) -> None:
    try:
        c_writer.write(_http_error(status, reason, message))
        await c_writer.drain()
    except Exception:
        pass
    try:
        c_writer.close()
    except Exception:
        pass


_relay: Optional[EgressRelay] = None


def get_serve_relay() -> EgressRelay:
    """The pool of parked serve workers, keyed by bastion token."""
    global _relay
    if _relay is None:
        _relay = EgressRelay()
    return _relay


def connected(token_id) -> bool:
    """Is that bastion's serve lane connected right now (a worker parked, or
    one carrying a connection)?"""
    return _relay is not None and _relay.has_bastion(token_id)


class ServeGateway:
    """The loopback listeners: one per (bastion token, service), each handing
    every connection to that bastion's serve lane for that service."""

    def __init__(self, relay_getter=get_serve_relay, host: str = "127.0.0.1"):
        self._relay_getter = relay_getter
        self._host = host
        self._servers: dict[tuple[int, str], asyncio.base_events.Server] = {}
        # When each bastion's lane was last seen connected, so a listener
        # outlives a moment between two jobs (a worker gone, its replacement
        # not parked yet) and no more: sweep() closes the listeners of a
        # bastion not connected for LISTENER_GRACE_S (revoked, gone, off).
        self._seen: dict[int, float] = {}
        self._lock = asyncio.Lock()

    def sweep(self, now: Optional[float] = None) -> list[tuple[int, str]]:
        """Close the listeners of every bastion whose lane has not been
        connected for LISTENER_GRACE_S. Returns what it closed. Connections
        already accepted on a closed listener run to their end."""
        now = time.monotonic() if now is None else now
        relay = self._relay_getter()
        closed = []
        for key in list(self._servers):
            tid = key[0]
            if relay.has_bastion(tid):
                self._seen[tid] = now
                continue
            if now - self._seen.get(tid, now) >= LISTENER_GRACE_S:
                self._servers.pop(key).close()
                closed.append(key)
        for tid in {k[0] for k in closed} - {k[0] for k in self._servers}:
            self._seen.pop(tid, None)
        if closed:
            log.info("serve gateway: closed the listeners of %s (not connected for %ds)",
                     sorted(closed), int(LISTENER_GRACE_S))
        return closed

    async def endpoint(self, token_id: int, service: str) -> str:
        """The http origin a client dials for `service` on bastion `token_id`."""
        if service not in SERVICES:
            raise ValueError(f"not a service the serve lane carries: {service!r}")
        key = (int(token_id), service)
        async with self._lock:
            self._seen[key[0]] = time.monotonic()
            self.sweep()
            srv = self._servers.get(key)
            if srv is None or not srv.is_serving():
                srv = await asyncio.start_server(
                    lambda r, w, k=key: self._carry(k, r, w), self._host, 0)
                self._servers[key] = srv
        port = srv.sockets[0].getsockname()[1]
        return f"http://{self._host}:{port}"

    def owns(self, url: str) -> bool:
        """Is `url` the origin of one of this gateway's listeners? A caller that
        would send a bastion's traffic through the Hub's peer forward (the mesh
        path) dials a lane origin directly instead: it is this Host's own
        loopback, and the forward admits mesh peers only."""
        try:
            from urllib.parse import urlsplit
            u = urlsplit(url or "")
            if u.scheme != "http" or u.hostname != self._host or not u.port:
                return False
        except ValueError:
            return False
        for srv in self._servers.values():
            if srv.is_serving() and any(s.getsockname()[1] == u.port for s in srv.sockets):
                return True
        return False

    async def _carry(self, key, c_reader, c_writer):
        token_id, service = key
        relay = self._relay_getter()
        try:
            if not relay.has_bastion(token_id):
                await _answer(c_writer, 503, "Service Unavailable",
                              "the machine serving this model is not connected to this Host right now")
                return
            ok = await relay.dispatch(service.encode(), c_reader, c_writer, DISPATCH_TIMEOUT_S,
                                      bastion_token_id=token_id)
            if not ok:
                await _answer(c_writer, 503, "Service Unavailable",
                              "the machine serving this model is busy or not connected; try again")
        except Exception as e:  # noqa: BLE001 — one connection, never the listener
            log.info("serve gateway %s/%s: %r", token_id, service, e)
            try:
                c_writer.close()
            except Exception:
                pass

    async def close(self):
        for srv in list(self._servers.values()):
            srv.close()
        self._servers.clear()


_gateway: Optional[ServeGateway] = None


def get_serve_gateway() -> ServeGateway:
    global _gateway
    if _gateway is None:
        _gateway = ServeGateway()
    return _gateway


def is_lane_origin(url: str) -> bool:
    """Is `url` a serve-lane origin of this Host (ServeGateway.owns)?"""
    return _gateway is not None and _gateway.owns(url)


async def lane_endpoint(token_id, service: str) -> str | None:
    """The origin a local model on bastion `token_id` is reached at over its
    serve lane, or None when that lane is not connected now. What
    model_backends.list_local_models gives a model whose machine reported no
    address of its own, and embed_backends and pii_client give an embedder
    or the sterilizer on such a machine."""
    try:
        tid = int(token_id)
    except (TypeError, ValueError):
        return None
    gw = get_serve_gateway()
    if not connected(tid):
        gw.sweep()
        return None
    return await gw.endpoint(tid, service)
