"""pgwire_relay — the memory-mirror lane.

A user's bastion holds a STREAMING REPLICA of this entity's database on their
own hardware: a local copy is their continuity. This
module is the pod half of the wire that carries it.

WHY A REWRITE AND NOT A PIPE
============================
The bastion can reach exactly ONE TCP port in this pod — the hub sidecar's
ingress splices the WireGuard tunnel to [::1]:8443 and nothing else
(hub/ingress/ingress.go, HUB_HOST_TARGET). So the replication stream arrives
as a WebSocket on that port, like every other bastion lane (/bastion-egress,
/bastion-mgmt, /bastion-harness), authenticated by the bastion's own bearer +
client cert.

The database it must reach is unix-socket-only with `trust` auth
(entrypoint-postgres.sh: `listen_addresses = ''`, initdb --auth=trust). There
is no network listener to authenticate to, which is precisely why that image
holds no secrets. A naive splice of an authenticated WebSocket onto that
socket would therefore hand the bastion UNAUTHENTICATED SUPERUSER SQL on the
entity's memory — a large escalation over what a bastion can do today, and it
would do it silently.

So this relay does not pipe the client's first message. It DISCARDS the
client's StartupMessage and writes its own:

    user=entity  database=<the pod's own>  replication=true  application_name=mirror:<name>

Two properties fall out, and both are the point:

  1. The lane is STRUCTURALLY incapable of carrying SQL. `replication=true`
     puts the backend in walsender mode, where the only commands the parser
     accepts are the replication commands (IDENTIFY_SYSTEM, BASE_BACKUP,
     START_REPLICATION, CREATE/DROP_REPLICATION_SLOT, TIMELINE_HISTORY).
     There is no SELECT, no COPY, no arbitrary query — enforced by the
     backend, not by anything here.
  2. Identity is KERNEL-ASSERTED. application_name is what
     pg_stat_replication reports, so /api/backup/mirror attributes a stream to
     a specific enrolled bastion and a compromised box cannot forge another
     one's name into the status the user reads.

Nothing about the client's startup is trusted or even parsed for content: it
is measured, dropped, and replaced. A wrong guess about its contents cannot
open a hole, because its contents never reach the backend.

The bytes AFTER the startup are spliced verbatim. That half is content-blind
on purpose — the same doctrine as the hub's ingress splice and the egress
relay: route bytes, never read them.

HOW A DEAD SESSION IS NOTICED — and what this module relies on rather than does
==============================================================================
Nothing here times out an idle stream, deliberately, because a healthy
replication connection IS idle most of the time: between writes the walsender
sends nothing for as long as the host is quiet. An inactivity timer here would
cut exactly the connections that are working.

Detection is therefore borrowed from the two ends, and both must stay in place:

  * BACKEND side — `wal_sender_timeout` (PostgreSQL default 60s) makes the
    walsender ping and terminate a peer that stops answering. This is what
    reaps the socket when a laptop is closed mid-stream, and it is why the
    socket half of the splice ends on its own.
  * WIRE side — the WebSocket ping/pong the ASGI server runs under the mTLS
    front door. A TCP connection that dies without a FIN (the common home-NAT
    case) is closed by that keepalive, which is what makes `receive_bytes`
    raise and end the session.

If either is ever turned off, this relay leaks a walsender and a socket per
abandoned mirror, and the slot they hold stays `active` so the reconciler will
not touch it. That is the failure to look for if mirror sessions accumulate.

FAILURE POSTURE
===============
Every path here degrades rather than crashing: a bad frame, an oversized
startup, a dead socket or a vanished bastion closes THIS stream and nothing
else. The relay never touches the brain pool, never writes, and holds no
state beyond a session counter.
"""

from __future__ import annotations

import asyncio
import logging
import struct

log = logging.getLogger("cseries.network.pgwire")

# ── Wire constants (PostgreSQL frontend/backend protocol, v3) ───────────────
_PROTO_V3 = 196608          # 0x00030000
_SSL_REQUEST = 80877103     # magic "version" for an SSLRequest
_GSSENC_REQUEST = 80877104  # ditto for GSSENCRequest
_CANCEL_REQUEST = 80877102  # ditto for CancelRequest

# A StartupMessage is a handful of short key/value pairs. Anything larger is
# either a broken client or someone trying to make us buffer. libpq's own
# limit for this message is 10000 bytes; match it rather than invent one.
_MAX_STARTUP_BYTES = 10000

# How many pre-startup negotiation messages we will answer before giving up.
# A real client sends at most GSSENCRequest then SSLRequest then the startup.
_MAX_NEGOTIATIONS = 2

# Concurrent mirror streams allowed per pod. `pg_basebackup -X stream` holds
# TWO walsender connections at once (the base backup and the parallel WAL
# stream); a steady-state standby holds one. Three covers a seed overlapping a
# reconnect. The cap matters because the DB container runs max_connections=16
# and the kernel's own pool needs its share — an unbounded lane would let a
# looping bastion starve the entity of its own database.
MAX_CONCURRENT_STREAMS = 3

_active = 0
_active_lock = asyncio.Lock()


class PgWireError(Exception):
    """Refusal to establish the lane. The message is operator-facing only."""


def build_startup(dbname: str, application_name: str) -> bytes:
    """The StartupMessage the KERNEL sends — never the client's.

    `replication=true` selects PHYSICAL walsender mode. Not 'database' (that
    is logical replication, which would put a normal SQL parser behind the
    connection and hand back exactly the surface this design removes).
    """
    params = [
        ("user", "entity"),
        ("database", dbname),
        ("replication", "true"),
        ("application_name", application_name),
    ]
    body = struct.pack("!I", _PROTO_V3)
    for key, value in params:
        body += key.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
    body += b"\x00"
    return struct.pack("!I", len(body) + 4) + body


def _msg_len(buf: bytes) -> int | None:
    """Length of the leading length-prefixed message, or None if incomplete."""
    if len(buf) < 4:
        return None
    (n,) = struct.unpack("!I", buf[:4])
    if n < 8 or n > _MAX_STARTUP_BYTES:
        raise PgWireError(f"implausible startup length {n}")
    return n if len(buf) >= n else None


class StartupRewriter:
    """Consumes client bytes until the client's startup is complete, then
    yields the kernel's own startup plus any pipelined remainder.

    WebSocket frames do not align with PostgreSQL message boundaries, so this
    is a byte-stream state machine, not a per-frame parser. Assuming one frame
    == one message is the classic way this kind of relay desyncs.
    """

    def __init__(self, dbname: str, application_name: str):
        self._dbname = dbname
        self._app = application_name
        self._buf = b""
        self._negotiations = 0
        self.done = False

    def feed(self, chunk: bytes) -> tuple[bytes, bytes]:
        """Absorb client bytes.

        Returns (to_client, to_backend). `to_client` carries an immediate
        negotiation reply ('N' — we never speak TLS or GSS inside a tunnel
        that is already encrypted end to end). `to_backend` is empty until the
        client's startup has been seen in full, at which point it is the
        KERNEL's startup followed by whatever the client had already
        pipelined behind its own.
        """
        if self.done:
            return b"", chunk
        self._buf += chunk
        to_client = b""
        while True:
            n = _msg_len(self._buf)
            if n is None:
                return to_client, b""
            (version,) = struct.unpack("!I", self._buf[4:8])
            if version in (_SSL_REQUEST, _GSSENC_REQUEST):
                self._negotiations += 1
                if self._negotiations > _MAX_NEGOTIATIONS:
                    raise PgWireError("too many pre-startup negotiations")
                # 'N' = not supported. The stream is already inside the
                # bastion's WireGuard tunnel and the pod's own loopback, so a
                # second TLS layer buys nothing and libpq falls through fine.
                to_client += b"N"
                self._buf = self._buf[n:]
                continue
            if version == _CANCEL_REQUEST:
                # A cancel is addressed to a backend by pid/secret the client
                # learned from a session we deliberately did not let it see.
                raise PgWireError("cancel requests are not carried on this lane")
            if version != _PROTO_V3:
                raise PgWireError(f"unsupported protocol version {version}")
            # A v3 StartupMessage. Everything it says — user, database,
            # options, replication, anything else — is DISCARDED here. This
            # is the wall: the client does not get to choose who it is.
            remainder = self._buf[n:]
            self._buf = b""
            self.done = True
            return to_client, build_startup(self._dbname, self._app) + remainder


async def relay(ws, dbname: str, application_name: str, socket_path: str) -> None:
    """Carry one mirror session between an authenticated bastion WebSocket and
    the entity's PostgreSQL unix socket.

    Returns when either side closes. Raises PgWireError only for a refusal
    before the backend connection is made; after that every end is a clean
    return, because a mirror going away is normal and must never be an
    incident.
    """
    global _active
    async with _active_lock:
        if _active >= MAX_CONCURRENT_STREAMS:
            raise PgWireError(
                f"{_active} mirror streams already open (cap {MAX_CONCURRENT_STREAMS})")
        _active += 1
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(socket_path), timeout=10)
        except Exception as e:  # noqa: BLE001
            raise PgWireError(f"database socket unreachable: {e}") from None

        rewriter = StartupRewriter(dbname, application_name)
        log.info("mirror stream open (%s)", application_name)

        async def client_to_backend() -> None:
            while True:
                chunk = await ws.receive_bytes()
                if not chunk:
                    continue
                to_client, to_backend = rewriter.feed(chunk)
                if to_client:
                    await ws.send_bytes(to_client)
                if to_backend:
                    writer.write(to_backend)
                    await writer.drain()

        async def backend_to_client() -> None:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                await ws.send_bytes(chunk)

        tasks = [asyncio.create_task(client_to_backend()),
                 asyncio.create_task(backend_to_client())]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                # A closed socket / gone bastion is the expected end of every
                # mirror session. Only a refusal is worth a log line above debug.
                if isinstance(exc, PgWireError):
                    log.info("mirror stream refused (%s): %s", application_name, exc)
                elif exc is not None:
                    log.debug("mirror stream ended (%s): %r", application_name, exc)
        finally:
            for t in tasks:
                t.cancel()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        log.info("mirror stream closed (%s)", application_name)
    finally:
        async with _active_lock:
            _active -= 1
