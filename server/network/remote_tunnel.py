"""The Host's side of remote access: one outbound tunnel to each relay.

A member Host keeps a tunnel open to every relay its ticket names (one on each
of the network's edges), so whichever edge the network's DNS points browsers
at, the Host is already there. Nothing on the machine listens on the internet:
the tunnel is a connection the Host made, out through the owner's router like
any other.

Each tunnel is TLS 1.3 with the network passport as the client certificate
(server/bridge/passport.py) and the relay checked against the network root for
tunnel.<zone>, then a WebSocket (server/network/remote_protocol.py has the
frames). The relay opens a stream for each browser that asks for one of this
Host's names. The Host connects it to its own door, the TLS front end on this
machine that holds the Host's certificate and asks the Host about every
request (as the cloud box's Caddy does: vm/cloud/overlay/etc/caddy/
Caddyfile.in), and writes a PROXY v2 header first so the door and the Host see
the browser's address. The door is never the Host's loopback: a loopback peer
is the machine's own and passes the owner-session gate without a session
(server/middleware/owner_session.py, OwnerSessionGate), which no browser on
the internet may.

The tunnel never carries anything out. The Host cannot open a stream; it only
answers the relay's, and only for names under its own <address_id>.<zone>.

Keepalive and reconnects: a WebSocket ping every HEARTBEAT_S both ways, and a
tunnel whose pong does not come within half of that is closed, so a home
router that dropped the connection's mapping, a changed address or a sleeping
laptop is noticed within HEARTBEAT_S * 1.5. Then the Host dials again after
remote_protocol's delays: a few seconds at random, then full-jitter
exponential backoff to a minute. The relay's goaway names how long to spread
the next try over; a refusal the Host cannot fix by retrying waits
REFUSED_RETRY_S. An expired or refused ticket asks the ticket source for a
fresh one before the next try.

Nothing starts this yet. Its two inputs come from outside: the ticket source
(the member door's remote.ticket) and the door on the machine, which is given
as an address. The tests drive it against the reference relay
(server/network/remote_relay.py).
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import ipaddress
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import aiohttp

from server.network import remote_protocol as rp

log = logging.getLogger("cseries.remote_tunnel")

HEARTBEAT_S = 20.0
CONNECT_TIMEOUT_S = 15.0
READY_TIMEOUT_S = 10.0
DOOR_CONNECT_TIMEOUT_S = 5.0
# The ticket lives an hour; a fresh one goes up the open tunnel every 20
# minutes, so a member door that cannot answer for 40 minutes costs nothing.
TICKET_REFRESH_S = 1200.0

# The relay's refusals (the X-Remote-Refusal header on a refused upgrade, or
# a goaway's reason) that a fresh ticket can fix, and those it cannot.
TICKET_REASONS = frozenset({"ticket_invalid", "ticket_expired", "ticket_mismatch"})
FINAL_REASONS = frozenset({"not_entitled", "revoked"})
REFUSAL_HEADER = "X-Remote-Refusal"

# fresh=True asks for a ticket newer than any handed out before.
TicketSource = Callable[[bool], Awaitable[str]]


@dataclass(frozen=True)
class RelayAddress:
    host: str                 # what is dialled: an address or a name
    port: int = 443
    label: str = ""           # for status and logs ("vin", "hil")


@dataclass
class Delays:
    """remote_protocol's reconnect policy; the tests shrink it."""
    first: tuple[float, float] = rp.FIRST_DELAY_S
    cap: float = rp.BACKOFF_CAP_S
    stable: float = rp.STABLE_S
    refused: float = rp.REFUSED_RETRY_S


@dataclass
class LinkStatus:
    relay: str
    state: str = "starting"   # starting, connecting, up, waiting, refused, stopped
    since: float = field(default_factory=time.time)
    connected_at: Optional[float] = None
    last_error: Optional[str] = None
    last_refusal: Optional[str] = None
    streams: int = 0
    attempts: int = 0
    connects: int = 0


def door_refusal(host: str) -> Optional[str]:
    """Why the tunnel may not hand a browser's bytes to `host`, or None.

    A connection from the Host's own loopback passes the owner-session gate
    without a session (server/middleware/owner_session.py, OwnerSessionGate:
    LOOPBACK_HOSTS), so a door reached over loopback would let every browser
    on the internet in as the machine itself. All of loopback is refused, not
    only 127.0.0.1: a connection to any of 127/8 leaves from 127.0.0.1. So is
    the unspecified address, which the kernel dials as this machine. A name
    is not judged here (None): the client judges the connection it made to
    it, both ends, before anything is written (_Link._run_stream)."""
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return f"{host} is loopback"
    if ip.is_unspecified:
        return f"{host} is the unspecified address"
    return None


class Refused(Exception):
    def __init__(self, reason: str, status: int = 0, retry_after: Optional[float] = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retry_after = retry_after


class TunnelClient:
    def __init__(self, *, address_id: str, zone: str, relays: list[RelayAddress],
                 ssl_context: ssl.SSLContext, tickets: TicketSource,
                 door: tuple[str, int], heartbeat: float = HEARTBEAT_S,
                 ticket_refresh: float = TICKET_REFRESH_S,
                 delays: Optional[Delays] = None, rng: Optional[random.Random] = None):
        if not rp.valid_address_id(address_id):
            raise ValueError(f"{address_id!r} is not an address ID")
        if not rp.valid_dns_name(zone):
            raise ValueError(f"{zone!r} is not a zone")
        if not relays:
            raise ValueError("no relay to dial")
        if ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname:
            raise ValueError("the relay must be verified: CERT_REQUIRED and check_hostname")
        why = door_refusal(door[0])
        if why:
            raise ValueError(f"the door must not be this machine's loopback: {why}")
        self.address_id = address_id
        self.zone = zone.lower()
        self._suffix = f".{address_id}.{self.zone}"
        self._ctx = ssl_context
        self._tickets = tickets
        self._door = door
        self._heartbeat = heartbeat
        self._ticket_refresh = ticket_refresh
        self._delays = delays or Delays()
        self._rng = rng or random.SystemRandom()
        self._stopping = asyncio.Event()
        self._links = [_Link(self, r) for r in relays]
        self._tasks: list[asyncio.Task] = []

    # ── the owner's view (Settings, later) ──

    def status(self) -> list[dict]:
        return [dict(vars(link.status)) for link in self._links]

    # ── running ──

    async def run(self) -> None:
        self._tasks = [asyncio.ensure_future(link.run()) for link in self._links]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            for t in self._tasks:
                t.cancel()

    def start(self) -> asyncio.Task:
        return asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self._stopping.set()
        for link in self._links:
            await link.close()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def owns(self, sni: str) -> bool:
        return rp.address_id_of(sni, self.zone) == self.address_id

    async def _sleep(self, seconds: float) -> bool:
        """Wait, or return True at once if the client is stopping."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False


class _Link:
    """One relay: dial, serve, and dial again."""

    def __init__(self, client: TunnelClient, relay: RelayAddress):
        self.c = client
        self.relay = relay
        self.status = LinkStatus(relay=relay.label or f"{relay.host}:{relay.port}")
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._streams: dict[int, rp.MuxStream] = {}
        self._stream_tasks: set[asyncio.Task] = set()
        self._sender = rp.Sender()
        self._need_fresh = False
        self._goaway_after: Optional[float] = None
        self._goaway_reason: Optional[str] = None
        self._limits = rp.Limits()
        # The limits of each refreshed ticket sent and not yet answered; the
        # relay answers them in order (ticket_ok or ticket_refused).
        self._pending: collections.deque = collections.deque()
        self._up_at: Optional[float] = None

    def _set(self, state: str, error: Optional[str] = None) -> None:
        self.status.state, self.status.since = state, time.time()
        if error is not None:
            self.status.last_error = error

    async def close(self) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()

    async def run(self) -> None:
        c = self.c
        attempt = 0
        while not c._stopping.is_set():
            self.status.attempts += 1
            self._set("connecting")
            self._goaway_after = None
            self._goaway_reason = None
            self._up_at = None
            delay: Optional[float] = None
            try:
                await self._session()
            except Refused as r:
                self.status.last_refusal = r.reason
                if r.reason in FINAL_REASONS or r.status == 401:
                    self._set("refused", f"the relay refused this Host: {r.reason}")
                    delay = c._delays.refused
                else:
                    self._set("waiting", f"the relay refused the tunnel: {r.reason}")
                    if r.reason in TICKET_REASONS:
                        self._need_fresh = True
                    if r.retry_after is not None:
                        # At least what the relay asked, but never longer than
                        # a refusal's wait (one header cannot park the Host for
                        # good), and spread like any other retry, so the Hosts
                        # a full relay turned away together do not all come
                        # back in the same second.
                        delay = (min(r.retry_after, c._delays.refused)
                                 + rp.backoff_delay(attempt, c._rng, c._delays.cap))
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise       # stop(), or the client's own task cancelled
                # Not a cancel of this task: the writers of one WebSocket share
                # aiohttp's single drain future, and a cancel elsewhere (a
                # stream's halves cancelled by the relay's RST) cancels it under
                # every later send, an RST or a pong among them. This tunnel is
                # over; the link must not be, or the Host is unreachable until
                # it restarts.
                self._set("waiting", "a send on the tunnel was cut")
            except Exception as exc:  # network, TLS, the relay going away
                self._set("waiting", f"{exc.__class__.__name__}: {exc}")
            finally:
                await self._drop_streams()
            if c._stopping.is_set():
                break
            if self._up_at is not None and time.monotonic() - self._up_at >= c._delays.stable:
                attempt = 0
            if delay is None and self._goaway_reason in FINAL_REASONS:
                self._set("refused", f"the relay closed the tunnel: {self._goaway_reason}")
                delay = c._delays.refused
            if delay is None and self._goaway_reason == "replaced":
                # Another connection with this Host's passport took the
                # address. A restarted Host never sees this (the goaway goes to
                # its old, dead connection), so it means a second machine holds
                # the same passport, a restored copy say. Dialling straight
                # back would take turns with it for ever; this waits as long as
                # a refusal does and says why.
                self._set("refused", "another connection for this Host replaced this one: "
                                     "is a copy of this Host running on another machine?")
                delay = c._delays.refused
            if delay is None:
                if self._goaway_after is not None:
                    delay = c._rng.uniform(0.0, self._goaway_after)
                elif attempt == 0:
                    delay = c._rng.uniform(*c._delays.first)
                else:
                    delay = rp.backoff_delay(attempt, c._rng, c._delays.cap)
            attempt += 1
            if self.status.state in ("up", "connecting"):
                self._set("waiting")
            if await c._sleep(delay):
                break
        self._set("stopped")

    def _url(self) -> str:
        h = self.relay.host
        host = f"[{h}]" if ":" in h else h
        return f"https://{host}:{self.relay.port}{rp.TUNNEL_PATH}"

    async def _session(self) -> None:
        """One tunnel, from the dial to its close. self._up_at is the
        monotonic time it came up, if it did."""
        c = self.c
        fresh, self._need_fresh = self._need_fresh, False
        self._pending.clear()
        ticket = await c._tickets(fresh)
        agreed = rp.ticket_limits(ticket)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S,
                                        connect=CONNECT_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            try:
                ws = await http.ws_connect(
                    self._url(), protocols=(rp.PROTOCOL,), heartbeat=c._heartbeat,
                    ssl=c._ctx, server_hostname=rp.tunnel_name(c.zone),
                    headers={rp.TICKET_HEADER: ticket}, max_msg_size=rp.WS_MAX_MSG_SIZE,
                    compress=0, autoclose=True, autoping=True)
            except aiohttp.WSServerHandshakeError as exc:
                headers = exc.headers or {}
                reason = headers.get(REFUSAL_HEADER) or f"http_{exc.status}"
                retry = headers.get("Retry-After")
                raise Refused(reason, exc.status,
                              float(retry) if retry and retry.isdigit() else None) from exc
            # Its own sender: a send a cancelled caller left on the last
            # tunnel, still waiting on that connection's drain, holds up
            # nothing on this one.
            self._sender = rp.Sender()
            self._ws = ws
            try:
                if ws.protocol != rp.PROTOCOL:
                    raise rp.ProtocolError(f"the relay chose subprotocol {ws.protocol!r}")
                ready = await asyncio.wait_for(ws.receive(), READY_TIMEOUT_S)
                if ready.type != aiohttp.WSMsgType.TEXT:
                    raise rp.ProtocolError("the relay did not say ready")
                msg = json.loads(ready.data)
                if msg.get("type") != "ready" or msg.get("address_id") != c.address_id:
                    raise rp.ProtocolError("the relay's ready is not for this Host")
                try:
                    offered = rp.Limits.from_dict(msg.get("limits"))
                except rp.TicketError as exc:
                    raise rp.ProtocolError(str(exc)) from exc
                # The ticket is what the member door agreed to. The relay may
                # hold this tunnel to less, never to more: a relay that claims
                # a larger window has its streams closed for overrunning the
                # ticket's, and one that claims more streams gets RST.
                self._limits = offered.narrower(agreed)
                self._up_at = time.monotonic()
                self.status.connected_at = time.time()
                self.status.connects += 1
                self.status.last_error = None
                self._set("up")
                log.info("remote access: tunnel up to %s", self.status.relay)
                refresher = asyncio.ensure_future(self._refresh_tickets())
                try:
                    await self._serve(ws)
                finally:
                    refresher.cancel()
            finally:
                self._ws = None
                if not ws.closed:
                    await ws.close()

    async def _refresh_tickets(self) -> None:
        while True:
            await asyncio.sleep(self.c._ticket_refresh)
            try:
                ticket = await self.c._tickets(True)
            except Exception as exc:
                # The ticket in use is still good until it expires; the next
                # refresh tries again.
                log.warning("remote access: no fresh ticket (%s)", exc)
                continue
            self._pending.append(rp.ticket_limits(ticket))
            try:
                await self._send_text({"type": "ticket", "ticket": ticket})
            except asyncio.CancelledError:
                # The shared drain future (see run): the ticket was written,
                # only the wait for the drain was cut. Only a cancel of this
                # task (the tunnel closing) ends the refreshes.
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise

    async def _send(self, frame: bytes) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionResetError("the tunnel is closed")
        # Shielded and in order (rp.Sender): a stream's half cancelled by the
        # relay's RST while it waits in drain must not cancel aiohttp's drain
        # future under every later send on this tunnel, its pongs included.
        await self._sender.send(lambda: ws.send_bytes(frame))

    async def _send_text(self, obj: dict) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        text = json.dumps(obj, separators=(",", ":"))
        await self._sender.send(lambda: ws.send_str(text))

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self._frame(*rp.decode(msg.data))
            elif msg.type == aiohttp.WSMsgType.TEXT:
                self._control(msg.data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    def _control(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            raise rp.ProtocolError("a control message that is not JSON") from None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "goaway":
            reason = str(msg.get("reason") or "")
            self.status.last_refusal = reason or None
            self._goaway_reason = reason or None
            after = msg.get("reconnect_after_s")
            if isinstance(after, (int, float)) and not isinstance(after, bool) and 0 <= after <= 3600:
                self._goaway_after = float(after)
            if reason in TICKET_REASONS:
                self._need_fresh = True
            log.info("remote access: %s says goaway (%s)", self.status.relay, reason)
        elif kind in ("ticket_ok", "ticket_refused") and self._pending:
            limits = self._pending.popleft()
            if kind == "ticket_ok":
                # The relay holds this tunnel to the new ticket from now on, so
                # the Host does too: its own ticket's limits, never the relay's
                # word. The window stays the one agreed at ready, on both
                # sides, until the next tunnel.
                self._limits = dataclasses.replace(limits, window=self._limits.window)
        # Anything newer: nothing to do.

    async def _frame(self, ftype: int, sid: int, payload: bytes) -> None:
        if ftype == rp.OPEN:
            await self._open(sid, payload)
            return
        stream = self._streams.get(sid)
        if stream is None:
            return   # a stream already over: its RST and the peer's frame crossed
        if ftype == rp.DATA:
            stream.feed(payload)
        elif ftype == rp.WINDOW:
            stream.grant(rp.parse_window(payload))
        elif ftype == rp.FIN:
            stream.peer_fin()
        elif ftype == rp.RST:
            stream.peer_reset()
            self._streams.pop(sid, None)

    async def _open(self, sid: int, payload: bytes) -> None:
        info = rp.parse_open(payload)
        if sid in self._streams:
            raise rp.ProtocolError(f"stream {sid} opened twice")
        if not self.c.owns(info["sni"]) or len(self._streams) >= self._limits.streams:
            # Not this Host's name, or more browsers than the ticket allows: the
            # relay should never ask, and the answer is the same either way.
            await self._send(rp.encode(rp.RST, sid))
            return
        stream = rp.MuxStream(sid, self._send, window=self._limits.window)
        self._streams[sid] = stream
        self.status.streams = len(self._streams)
        task = asyncio.ensure_future(self._run_stream(stream, info))
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    async def _run_stream(self, stream: rp.MuxStream, info: dict) -> None:
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(*self.c._door), DOOR_CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError) as exc:
                log.warning("remote access: the door at %s:%s did not answer (%s)",
                            self.c._door[0], self.c._door[1], exc.__class__.__name__)
                await stream.reset()
                return
            if stream.was_reset:
                writer.close()
                return
            # The connection that was made, judged at both ends before a byte
            # goes on it: a door given by a name can resolve to loopback, and a
            # connection over loopback reaches the Host as the machine itself.
            peer = writer.get_extra_info("peername")
            local = writer.get_extra_info("sockname")
            why = ((door_refusal(peer[0]) if peer else "no peer address")
                   or (door_refusal(local[0]) if local else "no local address"))
            if why:
                log.error("remote access: refused the door at %s:%s (%s); no browser is "
                          "let in through this machine's loopback",
                          self.c._door[0], self.c._door[1], why)
                writer.transport.abort()
                await stream.reset()
                return
            writer.write(rp.proxy_v2_header((info["src"], info["sport"]), (peer[0], peer[1])))
            stream.attach(reader, writer)
            await stream.run()
        finally:
            self._streams.pop(stream.sid, None)
            self.status.streams = len(self._streams)

    async def _drop_streams(self) -> None:
        for stream in list(self._streams.values()):
            stream.peer_reset()
        self._streams.clear()
        self.status.streams = 0
        tasks = list(self._stream_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def client_context(*, passport_cert: str, passport_key: str, network_root: str) -> ssl.SSLContext:
    """The TLS context a tunnel dials with: the passport as the client
    certificate (the leaf followed by its intermediate, as chain.crt holds
    them), the network root as the only trust anchor, TLS 1.3, and the name
    checked (tunnel.<zone>, given at the dial)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(cafile=network_root)
    ctx.load_cert_chain(certfile=passport_cert, keyfile=passport_key)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
