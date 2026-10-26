"""Remote access: what the Host's tunnel and the relay say to each other.

A person reaches their own Host from a browser anywhere through a relay on the
network's edge, without the machine listening on the internet. The Host dials
out to the relay and keeps the connection open (server/network/remote_tunnel.py).
A browser connects to the relay at <handle>.<address_id>.<zone>; the relay reads
only the name in the TLS ClientHello and passes the still-encrypted bytes down
that Host's tunnel, where the machine's own front door ends the TLS with a key
that never leaves it. The relay's side is server/network/remote_relay.py.

The tunnel, end to end:

  * TCP to the relay, TLS 1.3, mutual: the Host's client certificate is its
    network passport (server/bridge/passport.py) and the relay's certificate is
    checked against the network root for the name tunnel.<zone>.
  * GET TUNNEL_PATH, upgraded to a WebSocket with the subprotocol PROTOCOL,
    carrying the Host's remote-access ticket in TICKET_HEADER (below).
  * Every binary WebSocket message is one frame: a type byte, a 4-byte
    big-endian stream id, then the payload (HEADER, encode, decode).

      OPEN    relay -> Host  a browser connected; JSON {src, sport, sni}
      DATA    both           at most MAX_DATA bytes of that connection
      WINDOW  both           4-byte credit: the sender may send that many more
                             DATA bytes on the stream
      FIN     both           the sender has no more bytes (a half close); once
                             per stream, and no DATA after it
      RST     both           the stream is over, without the rest of its bytes

    Only the relay opens streams, so nothing the Host sends can make the relay
    connect anywhere: the tunnel carries browsers in and nothing out.
    Each side may have at most `window` bytes in flight per stream (256 KiB to
    start); credit comes back as the receiver writes the bytes out, so a slow
    browser or a slow door holds up its own stream and nothing else, and
    neither side buffers more than a window per stream.

  * Text messages are JSON control: the relay's {"type": "ready"}, the Host's
    fresh {"type": "ticket"} and the relay's {"type": "ticket_ok"}, and the
    relay's {"type": "goaway", "reason", "reconnect_after_s"}.

The ticket is what the network's member door hands a member Host for remote
access: `v1.<payload>.<signature>`, both base64url without padding, the
signature Ed25519 over TICKET_CONTEXT + the payload's base64url text. The
payload names the passport's certificate serial, so a ticket is no use to
anyone without that passport's private key, and the address the Host may
serve. The relay checks it with public keys alone and needs no database.

No network here: frames, names, tickets, the PROXY header, delays, and the
stream that both sides splice through.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import random
import re
import struct
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PROTOCOL = "cseries-remote.v1"
TUNNEL_PATH = "/v1/tunnel"
TICKET_HEADER = "X-Remote-Ticket"
DEFAULT_ZONE = "remote.privatae.ai"
# The relay's own name in every zone; the Host checks the relay's certificate
# for it whatever address the relay was dialled on.
TUNNEL_LABEL = "tunnel"

# ── frames ──────────────────────────────────────────────────────────────────

OPEN, DATA, WINDOW, FIN, RST = 1, 2, 3, 4, 5
FRAME_TYPES = frozenset({OPEN, DATA, WINDOW, FIN, RST})
HEADER = struct.Struct("!BI")
MAX_DATA = 32 * 1024
MAX_OPEN_PAYLOAD = 1024
INITIAL_WINDOW = 256 * 1024
MAX_STREAM_ID = 2 ** 32 - 1
# The largest WebSocket message either side sends: one DATA frame. aiohttp
# refuses a message whose size reaches its max_msg_size (close 1009), so the
# limit each side sets is one more.
MAX_MESSAGE = HEADER.size + MAX_DATA
WS_MAX_MSG_SIZE = MAX_MESSAGE + 1


class ProtocolError(ValueError):
    """The peer sent something this protocol does not allow. The tunnel that
    carried it is closed: a peer that breaks the framing once cannot be
    trusted to have kept to it anywhere else."""


def encode(ftype: int, sid: int, payload: bytes = b"") -> bytes:
    if ftype not in FRAME_TYPES:
        raise ProtocolError(f"unknown frame type {ftype}")
    if not 1 <= sid <= MAX_STREAM_ID:
        raise ProtocolError(f"stream id {sid} out of range")
    if ftype == DATA and not 0 < len(payload) <= MAX_DATA:
        raise ProtocolError(f"DATA of {len(payload)} bytes")
    return HEADER.pack(ftype, sid) + payload


def decode(msg: bytes) -> tuple[int, int, bytes]:
    """(type, stream id, payload) of one binary message, each part checked."""
    if len(msg) < HEADER.size:
        raise ProtocolError(f"a frame of {len(msg)} bytes is shorter than its header")
    ftype, sid = HEADER.unpack_from(msg)
    payload = bytes(msg[HEADER.size:])
    if ftype not in FRAME_TYPES:
        raise ProtocolError(f"unknown frame type {ftype}")
    if sid == 0:
        raise ProtocolError("stream id 0")
    if ftype == DATA and not 0 < len(payload) <= MAX_DATA:
        raise ProtocolError(f"DATA of {len(payload)} bytes")
    if ftype == OPEN and not 0 < len(payload) <= MAX_OPEN_PAYLOAD:
        raise ProtocolError(f"OPEN of {len(payload)} bytes")
    if ftype == WINDOW and len(payload) != 4:
        raise ProtocolError(f"WINDOW of {len(payload)} bytes")
    if ftype in (FIN, RST) and len(payload) > 1:
        raise ProtocolError(f"{'FIN' if ftype == FIN else 'RST'} of {len(payload)} bytes")
    return ftype, sid, payload


def window_payload(n: int) -> bytes:
    if not 0 < n <= 2 ** 31 - 1:
        raise ProtocolError(f"WINDOW credit {n}")
    return struct.pack("!I", n)


def parse_window(payload: bytes) -> int:
    (n,) = struct.unpack("!I", payload)
    if not 0 < n <= 2 ** 31 - 1:
        raise ProtocolError(f"WINDOW credit {n}")
    return n


def open_payload(src: str, sport: int, sni: str) -> bytes:
    return json.dumps({"src": src, "sport": sport, "sni": sni},
                      separators=(",", ":")).encode()


def parse_open(payload: bytes) -> dict:
    """The browser's address and the name it asked for, each checked."""
    try:
        d = json.loads(payload)
    except ValueError as exc:
        raise ProtocolError("OPEN is not JSON") from exc
    if not isinstance(d, dict):
        raise ProtocolError("OPEN is not an object")
    try:
        src = str(ipaddress.ip_address(d.get("src")))
    except ValueError as exc:
        raise ProtocolError("OPEN names no browser address") from exc
    sport = d.get("sport")
    if not isinstance(sport, int) or isinstance(sport, bool) or not 0 <= sport <= 65535:
        raise ProtocolError("OPEN names no browser port")
    sni = d.get("sni")
    if not isinstance(sni, str) or not valid_dns_name(sni):
        raise ProtocolError("OPEN names no server name")
    return {"src": src, "sport": sport, "sni": sni.lower()}


# ── names ───────────────────────────────────────────────────────────────────

_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# The same address IDs as a cloud machine's (vm/cloud/.../cseries_cloud.py):
# six characters of a-z and 2-9 without l, o, 0 or 1, a letter first, a digit
# somewhere.
ADDRESS_ID_RE = re.compile(r"[a-km-np-z][a-km-np-z2-9]{5}")
_DIGIT_RE = re.compile(r"[2-9]")


def valid_dns_name(name: str) -> bool:
    if not isinstance(name, str) or not 0 < len(name) <= 253:
        return False
    return all(_LABEL_RE.fullmatch(label) for label in name.lower().split("."))


def valid_address_id(value) -> bool:
    return (isinstance(value, str) and bool(ADDRESS_ID_RE.fullmatch(value))
            and bool(_DIGIT_RE.search(value)))


def address_id_of(sni: str, zone: str) -> Optional[str]:
    """The address ID a browser's name belongs to, or None. Only a name
    exactly one label under <address_id>.<zone> is a Host's: its desktop
    (<handle>.<address_id>.<zone>) or one of its apps'
    (apps-<key>-<handle>.<address_id>.<zone>). The relay routes by the address
    ID alone; which label above it is the Host's own business."""
    if not isinstance(sni, str) or not valid_dns_name(sni):
        return None
    name, zone = sni.lower(), zone.lower()
    suffix = "." + zone
    if not name.endswith(suffix):
        return None
    labels = name[: -len(suffix)].split(".")
    if len(labels) != 2 or not valid_address_id(labels[1]):
        return None
    return labels[1]


def tunnel_name(zone: str) -> str:
    return f"{TUNNEL_LABEL}.{zone}"


# ── tickets ─────────────────────────────────────────────────────────────────

TICKET_CONTEXT = b"cseries-remote-ticket-v1."
TICKET_VERSION = 1
# The member door issues tickets for an hour;
# the relay refuses anything claiming longer, so a leaked signing mistake
# cannot mint a ticket that outlives a revocation by days.
MAX_TICKET_LIFETIME_S = 2 * 3600
# A ticket stamped this far in the future is refused: the issuer's clock and
# the relay's may disagree by a little, never by more.
CLOCK_SKEW_S = 300
_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SERIAL_RE = re.compile(r"[0-9a-f]{1,40}")
_MAX_TICKET_CHARS = 4096


@dataclass(frozen=True)
class Limits:
    """What one Host's tunnel may use on a relay. The ticket carries them, so
    the network can raise one member's without deploying the relay; a value
    outside BOUNDS is refused rather than clamped, as a sign the ticket is not
    what the door meant."""
    streams: int = 512             # browser connections open at once
    open_rate: float = 100.0       # new browser connections a second ...
    open_burst: int = 200          # ... after a burst of this many
    window: int = INITIAL_WINDOW   # bytes in flight per stream, each way
    bps: int = 12_500_000          # bytes a second each way (100 Mbit/s); not metered yet

    BOUNDS = {
        "streams": (1, 65536), "open_rate": (1, 100000), "open_burst": (1, 1000000),
        "window": (MAX_DATA, 16 * 1024 * 1024), "bps": (125_000, 10 ** 11),
    }

    @classmethod
    def from_dict(cls, d) -> "Limits":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise TicketError("ticket_invalid", "limits is not an object")
        values = {}
        for key, (lo, hi) in cls.BOUNDS.items():
            if key not in d:
                continue
            v = d[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise TicketError("ticket_invalid", f"limits.{key} is outside {lo}..{hi}")
            values[key] = float(v) if key == "open_rate" else int(v)
        return cls(**values)

    def narrower(self, other: "Limits") -> "Limits":
        """Each limit the smaller of the two."""
        return Limits(**{key: min(getattr(self, key), getattr(other, key))
                         for key in self.BOUNDS})


class TicketError(ValueError):
    """A ticket the relay will not take. `reason` is what the relay tells the
    Host: ticket_invalid, ticket_expired or ticket_mismatch."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_ticket(key: Ed25519PrivateKey, payload: Mapping) -> str:
    """The issuer's half, used by the member door and by the tests."""
    body = _b64e(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode())
    sig = key.sign(TICKET_CONTEXT + body.encode("ascii"))
    return f"v1.{body}.{_b64e(sig)}"


def verify_ticket(token: str, keys: Mapping[str, Ed25519PublicKey], *,
                  now: Optional[float] = None) -> dict:
    """The payload of a ticket signed by one of `keys` (by kid), unexpired and
    well formed, with its limits parsed into payload["limits"]. Raises
    TicketError otherwise. The caller still has to check the serial against
    the certificate the tunnel came in on (ticket_matches)."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or len(token) > _MAX_TICKET_CHARS:
        raise TicketError("ticket_invalid", "no ticket")
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise TicketError("ticket_invalid", "not a v1 ticket")
    try:
        raw = _b64d(parts[1])
        sig = _b64d(parts[2])
        payload = json.loads(raw)
    except ValueError as exc:
        raise TicketError("ticket_invalid", "the ticket does not decode") from exc
    if not isinstance(payload, dict):
        raise TicketError("ticket_invalid", "the ticket is not an object")
    kid = payload.get("kid")
    if not isinstance(kid, str) or not _KID_RE.fullmatch(kid) or kid not in keys:
        raise TicketError("ticket_invalid", "the ticket is signed by no key this relay holds")
    try:
        keys[kid].verify(sig, TICKET_CONTEXT + parts[1].encode("ascii"))
    except InvalidSignature as exc:
        raise TicketError("ticket_invalid", "the ticket's signature does not verify") from exc
    if payload.get("v") != TICKET_VERSION:
        raise TicketError("ticket_invalid", "unknown ticket version")
    for name in ("iat", "exp"):
        v = payload.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TicketError("ticket_invalid", f"the ticket has no {name}")
    iat, exp = float(payload["iat"]), float(payload["exp"])
    if iat > now + CLOCK_SKEW_S or exp <= iat or exp - iat > MAX_TICKET_LIFETIME_S:
        raise TicketError("ticket_invalid", "the ticket's times are not ones this relay accepts")
    if exp <= now:
        raise TicketError("ticket_expired", "the ticket has expired")
    if not valid_address_id(payload.get("address_id")):
        raise TicketError("ticket_invalid", "the ticket names no address")
    zone = payload.get("zone")
    if not isinstance(zone, str) or not valid_dns_name(zone):
        raise TicketError("ticket_invalid", "the ticket names no zone")
    serial = payload.get("serial")
    if not isinstance(serial, str) or not _SERIAL_RE.fullmatch(serial):
        raise TicketError("ticket_invalid", "the ticket names no passport serial")
    host_uuid = payload.get("host_uuid")
    if not isinstance(host_uuid, str) or not 0 < len(host_uuid) <= 64:
        raise TicketError("ticket_invalid", "the ticket names no Host")
    payload["limits"] = Limits.from_dict(payload.get("limits"))
    payload["zone"] = zone.lower()
    return payload


def ticket_limits(token) -> Limits:
    """The limits a ticket names, read without checking its signature; the
    defaults when it names none or cannot be read. For the Host, which reads
    the ticket its own member door handed it to know what the door agreed
    to: the relay's ready repeats the limits, and a relay's word must never
    raise them (each stream is a connection to the door, a file descriptor in
    the Host's own process). The relay checks the signature (verify_ticket)."""
    try:
        payload = json.loads(_b64d(token.strip().split(".")[1]))
        return Limits.from_dict(payload.get("limits"))
    except (AttributeError, IndexError, TypeError, ValueError):
        return Limits()


def ticket_matches(payload: dict, cert_serial: int) -> bool:
    """Whether the ticket was issued for the passport the tunnel presented."""
    return payload.get("serial") == format(cert_serial, "x")


# ── the PROXY protocol v2 header the Host's door reads ──────────────────────
# haproxy's proxy-protocol.txt, section 2.2. The door (Caddy's proxy_protocol
# listener wrapper) believes it only from the tunnel's own address, and then
# sees the browser's address as the client's, so the Host's per-client limits
# count browsers, not the tunnel.

PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"


def proxy_v2_header(src: tuple[str, int], dst: tuple[str, int]) -> bytes:
    s, d = ipaddress.ip_address(src[0]), ipaddress.ip_address(dst[0])
    if s.version == 6 and s.ipv4_mapped:
        s = s.ipv4_mapped
    if d.version == 6 and d.ipv4_mapped:
        d = d.ipv4_mapped
    if s.version == 4 and d.version == 4:
        family = 0x11
    else:
        family = 0x21
        if s.version == 4:
            s = ipaddress.IPv6Address(f"::ffff:{s}")
        if d.version == 4:
            d = ipaddress.IPv6Address(f"::ffff:{d}")
    block = s.packed + d.packed + struct.pack("!HH", src[1], dst[1])
    return PROXY_V2_SIGNATURE + bytes([0x21, family]) + struct.pack("!H", len(block)) + block


async def read_proxy_v2(reader: asyncio.StreamReader, timeout: float = 5.0) -> tuple[str, int]:
    """The source address from a PROXY v2 header at the start of `reader`.
    Raises ProtocolError for anything else; the caller drops the connection."""
    try:
        head = await asyncio.wait_for(reader.readexactly(16), timeout)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ProtocolError("no PROXY header") from exc
    if head[:12] != PROXY_V2_SIGNATURE or head[12] != 0x21:
        raise ProtocolError("not a PROXY v2 PROXY command")
    (length,) = struct.unpack("!H", head[14:16])
    if length > 512:
        raise ProtocolError("PROXY header too long")
    try:
        block = await asyncio.wait_for(reader.readexactly(length), timeout)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ProtocolError("short PROXY header") from exc
    if head[13] == 0x11 and length >= 12:
        return str(ipaddress.IPv4Address(block[0:4])), struct.unpack("!H", block[8:10])[0]
    if head[13] == 0x21 and length >= 36:
        addr = ipaddress.IPv6Address(block[0:16])
        return str(addr.ipv4_mapped or addr), struct.unpack("!H", block[32:34])[0]
    raise ProtocolError("PROXY header for neither TCP over IPv4 nor IPv6")


# ── reconnecting ────────────────────────────────────────────────────────────
# After a tunnel drops, the first try waits a few seconds at random, so the
# Hosts a relay restart dropped do not all come back in the same second; then
# full-jitter exponential backoff (each wait uniform between 0 and the capped
# doubling). A tunnel that stays up STABLE_S starts the count again.

FIRST_DELAY_S = (0.5, 5.0)
BACKOFF_CAP_S = 60.0
STABLE_S = 60.0
# A refusal the Host cannot fix by trying again (its membership does not
# include remote access, or the passport was revoked) waits as long as the
# member client does after a refusal (server/bridge/member_client.py,
# REFUSED_RETRY_S).
REFUSED_RETRY_S = 900.0


def first_delay(rng: random.Random) -> float:
    return rng.uniform(*FIRST_DELAY_S)


def backoff_delay(attempt: int, rng: random.Random, cap: float = BACKOFF_CAP_S) -> float:
    return rng.uniform(0.0, min(cap, 2.0 ** max(0, min(attempt, 30))))


# ── one spliced connection ──────────────────────────────────────────────────

SendFrame = Callable[[bytes], Awaitable[None]]


class Sender:
    """Every send on one WebSocket, in the order asked, each in a task of its
    own that no caller's cancel reaches.

    aiohttp's writers of one WebSocket share a single drain future, and its
    drain helper awaits it unshielded. A task cancelled while it waits there
    cancels the future itself, which stays cancelled until the socket drains:
    every later send that reaches aiohttp's flush mark then ends in
    CancelledError, aiohttp's own pong inside receive() among them. One
    stream's RST, cancelling a half that waited in drain under a peer that
    reads slowly, took the whole tunnel down. Here a cancel ends only the
    caller's wait: the frame it asked for still goes out, in order, and only
    these tasks, which nothing cancels, ever wait in drain. A cancel that
    reaches one anyway (the connection being torn down under it) ends its
    send in ConnectionResetError, never in a CancelledError that looks like a
    cancel of the caller.

    Backpressure is unchanged: a caller still waits for its own send, drain
    and all. Whatever a cancelled caller leaves is one frame, sent or failed
    with the connection."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def send(self, write: Callable[[], Awaitable[None]]) -> None:
        """Run write() (one aiohttp send) after every send asked before it."""
        task = asyncio.ensure_future(self._run(write))
        self._tasks.add(task)
        task.add_done_callback(self._done)
        await asyncio.shield(task)

    async def _run(self, write: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            try:
                await write()
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise
                raise ConnectionResetError("a send on the tunnel was cut") from None

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()    # a cancelled caller's send that failed: seen here


class MuxStream:
    """One browser connection inside the tunnel, on either side: the relay's
    end splices it to the browser's socket, the Host's end to its door's.

    Bytes the peer sends wait in a queue for the socket. They can never be
    more than `window`: the peer may send only what it has credit for, and
    credit goes back only once bytes are written out and drained, a half
    window at a time. A peer that sends beyond its credit has broken the
    protocol (ProtocolError, raised from feed)."""

    def __init__(self, sid: int, send: SendFrame,
                 reader: Optional[asyncio.StreamReader] = None,
                 writer: Optional[asyncio.StreamWriter] = None,
                 window: int = INITIAL_WINDOW):
        self.sid = sid
        self._send = send
        # The Host's end has no socket yet when OPEN arrives: the relay sends
        # the browser's first bytes straight after it, and they wait here
        # (within the window) until the door's connection is attached.
        self._reader = reader
        self._writer = writer
        self.window = window
        self._recv_credit = window      # what the peer may still send us
        self._send_credit = window      # what we may still send the peer
        self._credit = asyncio.Event()
        self._credit.set()
        self._queue: asyncio.Queue = asyncio.Queue()
        self.queued = 0
        self.max_queued = 0
        self.bytes_in = 0                # from the peer, out to the socket
        self.bytes_out = 0               # from the socket, out to the peer
        self._reset = False
        self._peer_fin = False
        self._tasks: list[asyncio.Task] = []
        self.done = asyncio.Event()

    # ── what the tunnel's reader hands this stream ──

    def feed(self, payload: bytes) -> None:
        if self._peer_fin:
            raise ProtocolError(f"stream {self.sid}: DATA after the peer's FIN")
        if len(payload) > self._recv_credit:
            raise ProtocolError(f"stream {self.sid}: {len(payload)} bytes beyond its "
                                f"credit of {self._recv_credit}")
        self._recv_credit -= len(payload)
        self.queued += len(payload)
        self.max_queued = max(self.max_queued, self.queued)
        self._queue.put_nowait(payload)

    def grant(self, n: int) -> None:
        if self._send_credit + n > 2 ** 31 - 1:
            raise ProtocolError(f"stream {self.sid}: credit overflow")
        self._send_credit += n
        self._credit.set()

    def peer_fin(self) -> None:
        # Once: the first FIN ends the writer (_to_socket), so nothing would
        # ever read a second one, and a peer repeating it would grow this
        # queue, outside every window, for as long as the stream stays open.
        if self._peer_fin:
            raise ProtocolError(f"stream {self.sid}: a second FIN")
        self._peer_fin = True
        self._queue.put_nowait(None)

    def peer_reset(self) -> None:
        self._reset = True
        self._abort()
        self.done.set()

    @property
    def was_reset(self) -> bool:
        return self._reset

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer

    async def send_first(self, data: bytes) -> None:
        """Bytes already read from the socket (the relay's peek at the
        ClientHello), sent before the socket is spliced, out of the same
        credit."""
        for i in range(0, len(data), MAX_DATA):
            chunk = data[i:i + MAX_DATA]
            if len(chunk) > self._send_credit:
                raise ProtocolError("first bytes beyond the stream's window")
            self._send_credit -= len(chunk)
            self.bytes_out += len(chunk)
            await self._send(encode(DATA, self.sid, chunk))

    async def reset(self) -> None:
        """End the stream from this side: RST to the peer, the socket closed."""
        await self._send_rst()
        self.finish()

    def finish(self) -> None:
        """End the stream here without a frame: both halves cancelled, the
        socket closed, done set. Calling it again does nothing more."""
        self._abort()
        self.done.set()

    # ── running ──

    async def run(self) -> None:
        if self._reset:
            return
        to_sock = asyncio.ensure_future(self._to_socket())
        from_sock = asyncio.ensure_future(self._from_socket())
        self._tasks = [to_sock, from_sock]
        try:
            # Each half may end on its own (a FIN one way is a half close), so
            # this waits for both, and stops at the first that fails.
            pending = set(self._tasks)
            while pending:
                done, pending = await asyncio.wait(pending,
                                                   return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.cancelled():
                        if self._reset:
                            return          # the peer's RST (peer_reset)
                        # Cancelled though no RST came: a send in it waited on
                        # the drain future that every writer of one WebSocket
                        # shares, and a cancel elsewhere cancelled it. That
                        # half can carry nothing more, so the stream is over,
                        # and the peer is told.
                        raise ConnectionResetError(f"stream {self.sid}: a send was cut")
                    if t.exception() is not None:
                        raise t.exception()
        except (OSError, ConnectionError, asyncio.IncompleteReadError):
            await self._send_rst()
        except asyncio.CancelledError:
            # The peer's RST cancels the two halves (peer_reset); a cancel of
            # this task itself is the tunnel going away, and goes on up.
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise
        finally:
            self.finish()

    async def _send_rst(self) -> None:
        if self._reset:
            return
        self._reset = True
        try:
            await self._send(encode(RST, self.sid))
        except Exception:
            pass
        except asyncio.CancelledError:
            # The shared drain future again (see run): aiohttp writes the frame
            # before it waits for the drain, so only the wait was cut. A cancel
            # of this task itself goes on up.
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise

    def _abort(self) -> None:
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass

    async def _to_socket(self) -> None:
        owed = 0
        while True:
            item = await self._queue.get()
            if item is None:
                if self._writer.can_write_eof():
                    try:
                        self._writer.write_eof()
                    except OSError:
                        pass
                return
            self._writer.write(item)
            await self._writer.drain()
            self.queued -= len(item)
            self.bytes_in += len(item)
            owed += len(item)
            if owed >= self.window // 2:
                self._recv_credit += owed
                await self._send(encode(WINDOW, self.sid, window_payload(owed)))
                owed = 0

    async def _from_socket(self) -> None:
        while True:
            while self._send_credit <= 0:
                self._credit.clear()
                await self._credit.wait()
            data = await self._reader.read(min(MAX_DATA, self._send_credit))
            if not data:
                await self._send(encode(FIN, self.sid))
                return
            self._send_credit -= len(data)
            self.bytes_out += len(data)
            await self._send(encode(DATA, self.sid, data))
