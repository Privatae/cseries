"""The reference relay for remote access: where browsers meet Hosts' tunnels.

This is the relay's whole behaviour in one process: the tests run it on the
machine's loopback, and the network's edge relay is built from it. No Host
runs it. A Host owner who wants a relay of their own could, on any server
with a public address.

Two listeners:

  tunnels   TLS 1.3, mutual: a client certificate must chain to the CA given
            (on the network, the members intermediate: every certificate that
            completes the handshake is a passport). GET remote_protocol.
            TUNNEL_PATH upgrades to a WebSocket only with a ticket
            (TICKET_HEADER) that verifies, has not expired and names the
            serial of the certificate it came in on. A refusal is an HTTP
            answer with the reason in X-Remote-Refusal. The newest tunnel for
            an address wins; the one it replaces gets goaway "replaced".
  browsers  plain TCP carrying the browser's TLS (on an edge, the front
            door's layer-4 route hands these over, behind a PROXY v2 header
            when expect_proxy is set). The relay reads the ClientHello's
            server name and nothing else, finds the tunnel of the address it
            names (remote_protocol.address_id_of), opens a stream there and
            splices bytes it cannot read. It never holds a key for the
            browser's TLS: that ends on the Host's machine.

What it keeps: in memory only, per tunnel the ticket's address, serial,
expiry and limits; per stream the counts. Its log line for a stream holds the
address ID, the browser's /24 (IPv6: /48), the bytes each way and how long it
lasted, never a full address.

Limits, each refusing the connection that would pass it (never slowing the
others): tunnels in all, streams in all, per browser source (/32, IPv6 /64)
connections open at once and a token bucket of new ones, and per tunnel the
ticket's streams and its token bucket of new streams. Every live tunnel's
ticket is checked each CHECK_INTERVAL_S; one past its expiry gets goaway
"ticket_expired" and is closed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import json
import logging
import ssl
import struct
import time
from typing import Callable, Mapping, Optional

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from server.network import remote_protocol as rp

log = logging.getLogger("cseries.remote_relay")

HEARTBEAT_S = 20.0
CHECK_INTERVAL_S = 60.0
PEEK_TIMEOUT_S = 5.0
# How long a goodbye (goaway, then the close) may take before the connection
# is cut. A Host that stopped reading leaves aiohttp's close waiting in
# drain(), which has no timeout of its own.
GOAWAY_TIMEOUT_S = 5.0
# A ClientHello with post-quantum key shares is about 1.8 KB today; the cap
# leaves room for larger ones and still bounds what an idle peer can make the
# relay hold before it has named anyone.
MAX_CLIENT_HELLO = 16 * 1024
REFUSAL_HEADER = "X-Remote-Refusal"


@dataclasses.dataclass
class RelayLimits:
    tunnels: int = 20000
    streams: int = 16384
    per_source_open: int = 256
    per_source_rate: float = 20.0     # new browser connections a second ...
    per_source_burst: int = 100       # ... after a burst of this many (the cloud box's door.nft meter)


class TokenBucket:
    def __init__(self, rate: float, burst: float, now: float):
        self.rate, self.capacity, self.tokens, self.last = rate, burst, burst, now

    def take(self, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def source_key(addr: str) -> str:
    ip = ipaddress.ip_address(addr)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def log_source(addr: str) -> str:
    ip = ipaddress.ip_address(addr)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ipaddress.ip_network(f"{ip}/{24 if ip.version == 4 else 48}", strict=False))


# ── the ClientHello's server name ───────────────────────────────────────────

class _Incomplete(Exception):
    pass


def parse_sni(data: bytes) -> Optional[str]:
    """The host_name in a TLS ClientHello at the start of `data`. Raises
    _Incomplete while more bytes could still complete it; None when it is not
    a ClientHello or names no host."""
    def need(n: int) -> None:
        if len(data) < n:
            raise _Incomplete()

    need(5)
    if data[0] != 0x16:
        return None
    (rec_len,) = struct.unpack("!H", data[3:5])
    need(9)
    if data[5] != 0x01:
        return None
    hs_len = int.from_bytes(data[6:9], "big")
    end = 9 + hs_len
    if hs_len > rec_len - 4:
        return None      # a ClientHello split over records: not routed
    need(end)
    p = 9 + 2 + 32
    if p + 1 > end:
        return None
    p += 1 + data[p]                                   # session id
    if p + 2 > end:
        return None
    p += 2 + struct.unpack("!H", data[p:p + 2])[0]     # cipher suites
    if p + 1 > end:
        return None
    p += 1 + data[p]                                   # compression methods
    if p + 2 > end:
        return None
    ext_end = p + 2 + struct.unpack("!H", data[p:p + 2])[0]
    p += 2
    if ext_end > end:
        return None
    while p + 4 <= ext_end:
        etype, elen = struct.unpack("!HH", data[p:p + 4])
        p += 4
        if p + elen > ext_end:
            return None
        if etype == 0:
            q = p + 2
            while q + 3 <= p + elen:
                ntype = data[q]
                (nlen,) = struct.unpack("!H", data[q + 1:q + 3])
                q += 3
                if q + nlen > p + elen:
                    return None
                if ntype == 0:
                    try:
                        return data[q:q + nlen].decode("ascii").lower()
                    except UnicodeDecodeError:
                        return None
                q += nlen
            return None
        p += elen
    return None


async def peek_sni(reader: asyncio.StreamReader, timeout: float = PEEK_TIMEOUT_S
                   ) -> tuple[bytes, Optional[str]]:
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < MAX_CLIENT_HELLO:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(4096), left)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
        try:
            return buf, parse_sni(buf)
        except _Incomplete:
            continue
    return buf, None


# ── one Host's tunnel ───────────────────────────────────────────────────────

class _Tunnel:
    def __init__(self, relay: "Relay", ws: web.WebSocketResponse, ticket: dict, serial: int,
                 transport: Optional[asyncio.BaseTransport] = None):
        self.relay = relay
        self.ws = ws
        self.transport = transport
        self.address_id: str = ticket["address_id"]
        self.serial = serial
        self.exp: float = float(ticket["exp"])
        self.limits: rp.Limits = ticket["limits"]
        self.streams: dict[int, rp.MuxStream] = {}
        self._next_sid = 1
        self._sender = rp.Sender()
        self._opens = TokenBucket(self.limits.open_rate, self.limits.open_burst,
                                  time.monotonic())
        self.closing = False

    async def send(self, frame: bytes) -> None:
        if self.ws.closed:
            raise ConnectionResetError("the tunnel is closed")
        tap = self.relay.tap
        if tap is not None and frame[0] == rp.DATA:
            tap("to_host", frame[rp.HEADER.size:])
        # Shielded and in order (rp.Sender): a stream's half cancelled by an
        # RST while it waits in drain must not cancel aiohttp's drain future
        # under every later send on this tunnel.
        await self._sender.send(lambda: self.ws.send_bytes(frame))

    async def send_text(self, obj: dict) -> None:
        if self.ws.closed:
            return
        text = json.dumps(obj, separators=(",", ":"))
        await self._sender.send(lambda: self.ws.send_str(text))

    async def goaway(self, reason: str, reconnect_after_s: float = 0.0) -> None:
        """Tell the Host why and close, within the relay's goaway timeout;
        past it, or if the goodbye fails, the connection is cut.

        Run it as a task of its own (Relay._say_goaway), never awaited where one
        tunnel's goodbye could hold up anything else. A Host that stopped
        reading leaves the close waiting in aiohttp's drain(). And the writers
        of one connection share a single drain future, so cancelling this
        tunnel's stream tasks (drop_streams) cancels it under every other
        waiter: the goodbye then raises CancelledError in whatever task
        awaits it, though nothing cancelled that task."""
        self.closing = True
        await self._bounded(self._goodbye(reason, reconnect_after_s))

    async def close(self) -> None:
        """Close without a goaway, bounded the same way."""
        self.closing = True
        await self._bounded(self.ws.close())

    async def _bounded(self, step) -> None:
        done = False
        try:
            await asyncio.wait_for(step, self.relay.goaway_timeout)
            done = True
        except (Exception, asyncio.CancelledError):
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise           # this task itself was cancelled: cut, and go on up
        finally:
            if not done:
                self._cut()

    async def _goodbye(self, reason: str, reconnect_after_s: float) -> None:
        try:
            await self.send_text({"type": "goaway", "reason": reason,
                                  "reconnect_after_s": reconnect_after_s})
        except Exception:
            pass
        await self.ws.close()

    def _cut(self) -> None:
        """Drop the connection now, unsent bytes and all. A close() would wait
        to flush a buffer the Host is never going to read."""
        if self.transport is not None:
            self.transport.abort()

    def open_allowed(self) -> bool:
        return (not self.closing and len(self.streams) < self.limits.streams
                and self._opens.take(time.monotonic()))

    def new_stream(self) -> rp.MuxStream:
        sid = self._next_sid
        self._next_sid += 1
        stream = rp.MuxStream(sid, self.send, window=self.limits.window)
        self.streams[sid] = stream
        return stream

    async def serve(self) -> None:
        async for msg in self.ws:
            if msg.type.name == "BINARY":
                ftype, sid, payload = rp.decode(msg.data)
                if ftype == rp.OPEN:
                    raise rp.ProtocolError("a Host may not open a stream")
                stream = self.streams.get(sid)
                if stream is None:
                    continue
                if ftype == rp.DATA:
                    if self.relay.tap is not None:
                        self.relay.tap("to_browser", payload)
                    stream.feed(payload)
                elif ftype == rp.WINDOW:
                    stream.grant(rp.parse_window(payload))
                elif ftype == rp.FIN:
                    stream.peer_fin()
                elif ftype == rp.RST:
                    stream.peer_reset()
                    self.streams.pop(sid, None)
            elif msg.type.name == "TEXT":
                await self._control(msg.data)
            else:
                break

    async def _control(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            raise rp.ProtocolError("a control message that is not JSON") from None
        if not isinstance(msg, dict) or msg.get("type") != "ticket":
            return
        try:
            t = self.relay.check_ticket(msg.get("ticket"), self.serial)
        except rp.TicketError as exc:
            await self.send_text({"type": "ticket_refused", "reason": exc.reason})
            return
        if t["address_id"] != self.address_id:
            await self.send_text({"type": "ticket_refused", "reason": "ticket_mismatch"})
            return
        # The window was agreed when the tunnel came up (ready), and the Host
        # holds every stream to it: a stream opened with a new one would
        # overrun the Host's credit and close the tunnel. The ticket's other
        # limits apply now, its window from the next tunnel.
        self.exp = float(t["exp"])
        self.limits = dataclasses.replace(t["limits"], window=self.limits.window)
        await self.send_text({"type": "ticket_ok", "exp": self.exp})

    def drop_streams(self) -> None:
        for stream in list(self.streams.values()):
            stream.peer_reset()
        self.streams.clear()


# ── the relay ───────────────────────────────────────────────────────────────

class Relay:
    # What each accepted tunnel is made as. A relay that adds its own per-tunnel
    # accounting (the network's edge) subclasses _Tunnel and names it here.
    tunnel_class: type[_Tunnel] = _Tunnel

    def __init__(self, *, zone: str, ticket_keys: Mapping[str, Ed25519PublicKey],
                 tunnel_ssl: ssl.SSLContext, expect_proxy: bool = False,
                 heartbeat: float = HEARTBEAT_S, check_interval: float = CHECK_INTERVAL_S,
                 goaway_timeout: float = GOAWAY_TIMEOUT_S,
                 limits: Optional[RelayLimits] = None,
                 tap: Optional[Callable[[str, bytes], None]] = None,
                 clock: Callable[[], float] = time.time):
        if tunnel_ssl.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("the tunnel listener must require a client certificate")
        self.zone = zone.lower()
        self._keys = dict(ticket_keys)
        self._tunnel_ssl = tunnel_ssl
        self._expect_proxy = expect_proxy
        self._heartbeat = heartbeat
        self._check_interval = check_interval
        self.goaway_timeout = goaway_timeout
        self.limits = limits or RelayLimits()
        self.tap = tap
        self._clock = clock
        self.tunnels: dict[str, _Tunnel] = {}
        self.deny: set[str] = set()
        self._streams_open = 0
        self._per_source: dict[str, int] = {}
        self._source_buckets: dict[str, TokenBucket] = {}
        self._runner: Optional[web.AppRunner] = None
        self._browser_server: Optional[asyncio.base_events.Server] = None
        self._checker: Optional[asyncio.Task] = None
        self._browser_tasks: set[asyncio.Task] = set()
        self._goodbyes: set[asyncio.Task] = set()
        self.tunnel_port = 0
        self.browser_port = 0

    def _say_goaway(self, tunnel: _Tunnel, reason: str, reconnect_after_s: float) -> None:
        """A tunnel's goodbye, as a task of its own (see _Tunnel.goaway): the
        expiry check, a newer tunnel's handler and stop() never wait on it."""
        tunnel.closing = True
        task = asyncio.ensure_future(tunnel.goaway(reason, reconnect_after_s))
        self._goodbyes.add(task)
        task.add_done_callback(self._goodbyes.discard)

    # ── tickets ──

    def check_ticket(self, token, serial: int) -> dict:
        t = rp.verify_ticket(token, self._keys, now=self._clock())
        if not rp.ticket_matches(t, serial):
            raise rp.TicketError("ticket_mismatch", "the ticket is for another passport")
        if t["zone"] != self.zone:
            raise rp.TicketError("ticket_mismatch", "the ticket is for another zone")
        return t

    # ── lifecycle ──

    async def start(self, host: str = "127.0.0.1", tunnel_port: int = 0,
                    browser_port: int = 0) -> None:
        app = web.Application()
        app.router.add_get(rp.TUNNEL_PATH, self._tunnel)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, tunnel_port, ssl_context=self._tunnel_ssl,
                           reuse_address=True)
        await site.start()
        self.tunnel_port = site._server.sockets[0].getsockname()[1]
        self._browser_server = await asyncio.start_server(self._browser, host, browser_port,
                                                          reuse_address=True)
        self.browser_port = self._browser_server.sockets[0].getsockname()[1]
        self._checker = asyncio.ensure_future(self._check_expiry())

    async def stop(self) -> None:
        if self._checker is not None:
            self._checker.cancel()
        if self._browser_server is not None:
            self._browser_server.close()
        for t in list(self.tunnels.values()):
            t.drop_streams()
            if not t.closing:
                self._say_goaway(t, "draining", 5.0)
        for task in list(self._browser_tasks):
            task.cancel()
        await asyncio.gather(*self._browser_tasks, return_exceptions=True)
        # Each is bounded by goaway_timeout, and they run side by side.
        await asyncio.gather(*self._goodbyes, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()

    async def _check_expiry(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            # A source's bucket is kept only while it has a connection open or
            # has not yet refilled, so the table holds recent sources only.
            mono = time.monotonic()
            for k, b in list(self._source_buckets.items()):
                if k not in self._per_source and mono - b.last > b.capacity / b.rate:
                    del self._source_buckets[k]
            now = self._clock()
            for t in list(self.tunnels.values()):
                if t.closing:
                    continue
                if t.exp <= now or t.address_id in self.deny:
                    reason = "ticket_expired" if t.exp <= now else "not_entitled"
                    log.info("remote relay: closing %s (%s)", t.address_id, reason)
                    t.drop_streams()
                    # Never awaited here: one Host that stopped reading would
                    # stall every other tunnel's expiry and the deny list.
                    self._say_goaway(t, reason, 0.0)

    # ── tunnels ──

    @staticmethod
    def _refuse(status: int, reason: str) -> web.Response:
        """A refused upgrade, and the end of its connection: a refused peer
        has no reason to keep its TLS connection, and behind a PROXY header
        (an edge) the connection's source is read once, for its first
        request, so a second request on it would come from nowhere."""
        resp = web.json_response({"error": "refused", "reason": reason}, status=status,
                                 headers={REFUSAL_HEADER: reason})
        resp.force_close()
        return resp

    async def _tunnel(self, request: web.Request) -> web.StreamResponse:
        sslobj = request.transport.get_extra_info("ssl_object") if request.transport else None
        der = sslobj.getpeercert(binary_form=True) if sslobj is not None else None
        if not der:
            return self._refuse(401, "no_certificate")
        serial = x509.load_der_x509_certificate(der).serial_number
        try:
            ticket = self.check_ticket(request.headers.get(rp.TICKET_HEADER), serial)
        except rp.TicketError as exc:
            return self._refuse(403, exc.reason)
        aid = ticket["address_id"]
        if aid in self.deny:
            return self._refuse(403, "not_entitled")
        if aid not in self.tunnels and len(self.tunnels) >= self.limits.tunnels:
            return self._refuse(503, "full")
        ws = web.WebSocketResponse(protocols=(rp.PROTOCOL,), heartbeat=self._heartbeat,
                                   max_msg_size=rp.WS_MAX_MSG_SIZE, compress=False)
        if ws.can_prepare(request).protocol != rp.PROTOCOL:
            return self._refuse(400, "protocol")
        await ws.prepare(request)
        tunnel = self.tunnel_class(self, ws, ticket, serial, request.transport)
        old = self.tunnels.get(aid)
        self.tunnels[aid] = tunnel
        try:
            if old is not None:
                old.drop_streams()
                # Not awaited: the old connection is usually a dead one (the
                # Host restarted), and its goodbye must not hold up this
                # tunnel's ready, or take this handler down with it.
                self._say_goaway(old, "replaced", 0.0)
            await tunnel.send_text({"type": "ready", "address_id": aid, "exp": tunnel.exp,
                                    "limits": dataclasses.asdict(tunnel.limits)})
            log.info("remote relay: tunnel up for %s", aid)
            await tunnel.serve()
        except rp.ProtocolError as exc:
            log.warning("remote relay: %s broke the protocol (%s)", aid, exc)
        finally:
            if self.tunnels.get(aid) is tunnel:
                del self.tunnels[aid]
            tunnel.drop_streams()
            if not ws.closed:
                await tunnel.close()
        return ws

    # ── browsers ──

    async def _browser(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._browser_tasks.add(task)
        key = None
        stream = None
        tunnel = None
        src = "0.0.0.0"
        started = time.monotonic()
        try:
            peer = writer.get_extra_info("peername")
            if self._expect_proxy:
                try:
                    src, sport = await rp.read_proxy_v2(reader)
                except rp.ProtocolError:
                    return
            else:
                src, sport = peer[0], peer[1]
            key = source_key(src)
            now = time.monotonic()
            bucket = self._source_buckets.get(key)
            if bucket is None:
                bucket = self._source_buckets[key] = TokenBucket(
                    self.limits.per_source_rate, self.limits.per_source_burst, now)
            if (self._per_source.get(key, 0) >= self.limits.per_source_open
                    or not bucket.take(now)):
                key = None
                return
            self._per_source[key] = self._per_source.get(key, 0) + 1
            buf, sni = await peek_sni(reader)
            aid = rp.address_id_of(sni, self.zone) if sni else None
            tunnel = self.tunnels.get(aid) if aid else None
            if (tunnel is None or self._streams_open >= self.limits.streams
                    or not tunnel.open_allowed()):
                return
            stream = tunnel.new_stream()
            stream.attach(reader, writer)
            self._streams_open += 1
            await tunnel.send(rp.encode(rp.OPEN, stream.sid, rp.open_payload(src, sport, sni)))
            await stream.send_first(buf)
            await stream.run()
        except (OSError, ConnectionError, rp.ProtocolError):
            if stream is not None:
                await stream.reset()
        except asyncio.CancelledError:
            # The writers of one tunnel's WebSocket share aiohttp's single drain
            # future, so a cancel elsewhere on it (drop_streams, another
            # stream's RST) can end the OPEN or the first bytes here in
            # CancelledError though nothing cancelled this task. The Host was
            # sent OPEN and holds the stream half open: it gets RST. A cancel
            # of this task itself (stop()) goes on up, after the finally.
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise
            if stream is not None:
                await stream.reset()
        finally:
            if stream is not None:
                # Done whatever ended it, so whoever waits on stream.done (the
                # edge's accounting) is never left waiting for good.
                stream.finish()
                self._streams_open -= 1
                if tunnel is not None:
                    tunnel.streams.pop(stream.sid, None)
                log.info("remote relay: stream %s from %s, %d bytes from the browser, "
                         "%d to it, %.1f s",
                         tunnel.address_id if tunnel else "-", log_source(src),
                         stream.bytes_out, stream.bytes_in, time.monotonic() - started)
            if key is not None:
                left = self._per_source.get(key, 1) - 1
                if left > 0:
                    self._per_source[key] = left
                else:
                    self._per_source.pop(key, None)
            try:
                writer.close()
            except Exception:
                pass
            self._browser_tasks.discard(task)


def server_context(*, cert: str, key: str, client_ca: str) -> ssl.SSLContext:
    """The tunnel listener's TLS: TLS 1.3, its own certificate, and a client
    certificate required, chaining to `client_ca` alone.

    `client_ca` is the CA that issues passports, and the whole trust anchor:
    on the network that is the members intermediate, never the root. The
    network is root -> members intermediate -> passports and root -> entity
    intermediate -> everything else (Host pods, the edges' own certificates),
    so trusting the root would let any of those complete the handshake.
    OpenSSL accepts a chain that ends at a CA that is not self-signed only with
    X509_V_FLAG_PARTIAL_CHAIN; without it every passport under the
    intermediate is refused. Give `client_ca` the issuing CA alone: with the
    flag, any certificate in that file is an anchor, a root included."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ctx.load_verify_locations(cafile=client_ca)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
