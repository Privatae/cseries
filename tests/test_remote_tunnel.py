"""Remote access: the Host's outbound tunnel and the reference relay.

Everything runs on this machine's loopback with real TLS: a network root that
signs the relay's certificate and a passport, a separate CA standing in for
the public one that signs the Host's own certificate, a door that ends the
browser's TLS on the "machine", and a browser. What is checked is what
remote access promises:

  * a browser reaches the Host's door through the relay and the tunnel, the
    door sees the browser's address in the PROXY header, and the relay never
    sees a byte of plaintext;
  * flow control holds: neither side queues more than a window per stream;
  * only a passport with a ticket for its own serial gets a tunnel, and a
    ticket's expiry closes it;
  * a Host that tries to open a stream (to reach anything through the
    relay) loses its tunnel;
  * names outside a Host's address are not routed, by the relay or the Host;
  * per-tunnel stream limits refuse the extra connection only;
  * a dead tunnel is noticed by the heartbeat and dialled again, a relay
    restart is survived, and the newest tunnel for an address wins;
  * with the members intermediate as the only anchor, a passport under it
    gets a tunnel and a certificate under the other intermediate does not;
  * a send cut under a stream or a tunnel (aiohttp's shared drain future)
    ends that stream with an RST, or that tunnel only, never the link;
  * an RST that cancels a send waiting in drain leaves the drain future
    alone, so the tunnel's later sends (ticket_ok, pongs) still go out.

Run: python3 -m pytest -q tests/test_remote_tunnel.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import os
import random
import socket
import ssl
import sys
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.network import remote_protocol as rp  # noqa: E402
from server.network import remote_relay as relay_mod  # noqa: E402
from server.network import remote_tunnel as tun  # noqa: E402

ZONE = "remote.example.test"
AID = "k7x2m9"
OTHER_AID = "q4w8z2"
HOST_NAME = f"home.{AID}.{ZONE}"
KID = "t1"
MARKER = b"plaintext-marker-only-the-door-and-browser-see"

FAST = tun.Delays(first=(0.01, 0.03), cap=0.2, stable=60.0, refused=0.3)


# ── certificates ────────────────────────────────────────────────────────────

def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, *, dns=(), uri=None, server=True):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(d) for d in dns]
    if uri:
        sans.append(x509.UniformResourceIdentifier(uri))
    b = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(ca_cert.subject)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(minutes=5))
         .not_valid_after(now + dt.timedelta(days=1))
         .add_extension(x509.SubjectAlternativeName(sans), critical=True)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage(
             [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),
             critical=False))
    return key, b.sign(ca_key, hashes.SHA256())


def _write(d: Path, name: str, key, cert) -> tuple[str, str]:
    kp, cp = d / f"{name}.key", d / f"{name}.crt"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cp), str(kp)


class PKI:
    def __init__(self, d: Path):
        self.dir = d
        root_key, root = _ca("test network root")
        self.root_pem = str(d / "root.crt")
        Path(self.root_pem).write_bytes(root.public_bytes(serialization.Encoding.PEM))
        k, c = _leaf(root_key, root, dns=[rp.tunnel_name(ZONE)])
        self.relay_cert, self.relay_key = _write(d, "relay", k, c)
        self.passports = {}
        for label in ("a", "b"):
            k, c = _leaf(root_key, root, uri=f"urn:test:host:{label}", server=False)
            self.passports[label] = (*_write(d, f"passport-{label}", k, c), c.serial_number)
        # Someone else's CA: a "passport" from it must not get a tunnel.
        other_key, other = _ca("not the network")
        k, c = _leaf(other_key, other, uri="urn:test:host:x", server=False)
        self.stranger = (*_write(d, "stranger", k, c), c.serial_number)
        # The public CA stand-in and the Host's own certificate, which only the
        # door holds.
        web_key, web = _ca("test web pki")
        self.web_ca = str(d / "web-ca.crt")
        Path(self.web_ca).write_bytes(web.public_bytes(serialization.Encoding.PEM))
        k, c = _leaf(web_key, web, dns=[f"*.{AID}.{ZONE}"])
        self.door_cert, self.door_key = _write(d, "door", k, c)
        self.ticket_key = Ed25519PrivateKey.generate()

    def relay_ssl(self) -> ssl.SSLContext:
        return relay_mod.server_context(cert=self.relay_cert, key=self.relay_key,
                                        client_ca=self.root_pem)

    def client_ssl(self, who="a") -> ssl.SSLContext:
        cert, key, _serial = self.passports[who] if who in self.passports else self.stranger
        return tun.client_context(passport_cert=cert, passport_key=key,
                                  network_root=self.root_pem)

    def serial(self, who="a") -> int:
        return self.passports[who][2] if who in self.passports else self.stranger[2]

    def ticket(self, who="a", *, aid=AID, life=3600.0, serial=None, limits=None, now=None):
        now = time.time() if now is None else now
        payload = {"v": 1, "kid": KID, "host_uuid": f"host-{who}",
                   "serial": format(self.serial(who) if serial is None else serial, "x"),
                   "address_id": aid, "zone": ZONE, "iat": now, "exp": now + life}
        if limits is not None:
            payload["limits"] = limits
        return rp.sign_ticket(self.ticket_key, payload)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI(tmp_path_factory.mktemp("pki"))


@pytest.fixture
def loopback_door(monkeypatch):
    """The rig's door listens on 127.0.0.1, the one address every test
    machine has. The tunnel refuses a loopback door (a connection from the
    Host's own loopback passes its owner-session gate without a session), so
    this lets that one address through for the rig and keeps the rest of the
    guard. The guard itself runs without it
    (test_the_door_is_never_the_hosts_loopback)."""
    real = getattr(tun, "door_refusal", None)
    if real is not None:
        monkeypatch.setattr(tun, "door_refusal",
                            lambda host: None if host == "127.0.0.1" else real(host))


# ── the door on the "machine" and a browser ─────────────────────────────────

async def _recv_exact(loop, sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = await loop.sock_recv(sock, n - len(buf))
        if not chunk:
            raise asyncio.IncompleteReadError(buf, n)
        buf += chunk
    return buf


async def read_proxy_v2_sock(loop, sock: socket.socket) -> tuple[str, int]:
    """The PROXY v2 header read off the raw socket, exactly its own bytes, then
    parsed by remote_protocol's reader: whatever came after it (the browser's
    ClientHello) stays in the socket for the TLS layer."""
    head = await _recv_exact(loop, sock, 16)
    length = int.from_bytes(head[14:16], "big")
    ok = head[:12] == rp.PROXY_V2_SIGNATURE and length <= 512
    block = await _recv_exact(loop, sock, length) if ok else b""
    r = asyncio.StreamReader()
    r.feed_data(head + block)
    r.feed_eof()
    return await rp.read_proxy_v2(r)


class Door:
    """Reads the PROXY header, ends the browser's TLS with the Host's own
    certificate, then answers: a line `hello` gets `src=<addr> sport=<port>`;
    anything else is echoed until the browser closes.

    The header is read off the raw socket and only then does TLS start on it.
    A StreamReader reads ahead, and on Python 3.12 StreamWriter.start_tls does
    not hand the TLS layer bytes already in the reader, so a ClientHello that
    arrived with the header was lost and the browser hung."""

    def __init__(self, pki: PKI):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        self.ctx.load_cert_chain(pki.door_cert, pki.door_key)
        self.seen: list[tuple[str, int]] = []
        self.sock = None
        self.port = 0
        self._accepter = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(512)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self._accepter = asyncio.ensure_future(self._accept())

    async def stop(self):
        self._accepter.cancel()
        await asyncio.gather(self._accepter, return_exceptions=True)
        self.sock.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self):
        loop = asyncio.get_running_loop()
        while True:
            conn, _ = await loop.sock_accept(self.sock)
            t = asyncio.ensure_future(self._one(conn))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _one(self, conn: socket.socket):
        loop = asyncio.get_running_loop()
        conn.setblocking(False)
        try:
            src = await read_proxy_v2_sock(loop, conn)
            self.seen.append(src)
            reader = asyncio.StreamReader()
            proto = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_accepted_socket(lambda: proto, conn, ssl=self.ctx)
            writer = asyncio.StreamWriter(transport, proto, reader, loop)
        except (OSError, ssl.SSLError, rp.ProtocolError, asyncio.IncompleteReadError):
            conn.close()
            return
        await self._handle(src, reader, writer)

    async def _handle(self, src, reader, writer):
        try:
            first = await reader.readline()
            if first == b"hello\n":
                writer.write(f"src={src[0]} sport={src[1]}\n".encode())
                await writer.drain()
                # The browser hangs up first, so the door's port keeps no
                # TIME_WAIT that the CI firewall would refuse a later
                # connection on (scripts/gate_loopback.py).
                while await reader.read(65536):
                    pass
            else:
                writer.write(first)
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
        except (OSError, ssl.SSLError, rp.ProtocolError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


def browser_ctx(pki: PKI) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(pki.web_ca)
    return ctx


async def browse(pki, relay, name=HOST_NAME, line=b"hello\n", timeout=5.0):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(
        "127.0.0.1", relay.browser_port, ssl=browser_ctx(pki), server_hostname=name), timeout)
    local = writer.get_extra_info("sockname")
    writer.write(line)
    await writer.drain()
    answer = await asyncio.wait_for(reader.readline(), timeout)
    writer.close()
    return answer, local


class Rig:
    def __init__(self, pki: PKI, relay_cls=None, **relay_kw):
        self.pki = pki
        self.relay_cls = relay_cls or relay_mod.Relay
        self.taps: list[tuple[str, bytes]] = []
        self.relay_kw = relay_kw
        self.relay = None
        self.door = Door(pki)
        self.clients: list[tun.TunnelClient] = []
        self.tasks = []
        self.ticket_calls: list[bool] = []

    async def start_relay(self, tunnel_port=0, browser_port=0):
        kw = dict(zone=ZONE, ticket_keys={KID: self.pki.ticket_key.public_key()},
                  tunnel_ssl=self.pki.relay_ssl(), heartbeat=0.5, check_interval=0.1,
                  tap=lambda d, b: self.taps.append((d, b)))
        kw.update(self.relay_kw)
        self.relay = self.relay_cls(**kw)
        await self.relay.start(tunnel_port=tunnel_port, browser_port=browser_port)
        return self.relay

    async def start(self):
        await self.door.start()
        await self.start_relay()
        return self

    def client(self, who="a", *, tickets=None, port=None, heartbeat=0.5, aid=AID, door=None,
               **kw):
        async def default_tickets(fresh):
            self.ticket_calls.append(fresh)
            return self.pki.ticket(who, aid=aid)
        c = tun.TunnelClient(
            address_id=aid, zone=ZONE,
            relays=[tun.RelayAddress("127.0.0.1", port or self.relay.tunnel_port, "test")],
            ssl_context=self.pki.client_ssl(who), tickets=tickets or default_tickets,
            door=door or ("127.0.0.1", self.door.port), heartbeat=heartbeat, delays=FAST,
            rng=random.Random(7), **kw)
        self.clients.append(c)
        self.tasks.append(c.start())
        return c

    async def wait_up(self, client, timeout=5.0, aid=AID):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = client.status()[0]
            if st["state"] == "up" and aid in self.relay.tunnels:
                return st
            await asyncio.sleep(0.02)
        raise AssertionError(f"tunnel not up: {client.status()}")

    async def close(self):
        for c in self.clients:
            await c.stop()
        if self.relay is not None:
            await self.relay.stop()
        await self.door.stop()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


# ── frames, names, tickets, delays ──────────────────────────────────────────

def test_frames_round_trip_and_bad_ones_are_refused():
    assert rp.decode(rp.encode(rp.DATA, 7, b"x" * rp.MAX_DATA)) == (rp.DATA, 7, b"x" * rp.MAX_DATA)
    assert rp.decode(rp.encode(rp.WINDOW, 1, rp.window_payload(5))) == (rp.WINDOW, 1, b"\0\0\0\5")
    opened = rp.parse_open(rp.open_payload("2001:db8::1", 443, HOST_NAME.upper()))
    assert opened == {"src": "2001:db8::1", "sport": 443, "sni": HOST_NAME}
    for bad in (b"", b"\x02\0\0\0", rp.HEADER.pack(9, 1), rp.HEADER.pack(rp.DATA, 0) + b"x",
                rp.HEADER.pack(rp.DATA, 1), rp.HEADER.pack(rp.DATA, 1) + b"x" * (rp.MAX_DATA + 1),
                rp.HEADER.pack(rp.WINDOW, 1) + b"\0\0", rp.HEADER.pack(rp.RST, 1) + b"ab"):
        with pytest.raises(rp.ProtocolError):
            rp.decode(bad)
    for bad in (b"[]", b'{"src":"x","sport":1,"sni":"a.b"}', b'{"src":"::1","sport":-1,"sni":"a.b"}',
                b'{"src":"::1","sport":1,"sni":"bad name"}', b"not json"):
        with pytest.raises(rp.ProtocolError):
            rp.parse_open(bad)
    with pytest.raises(rp.ProtocolError):
        rp.parse_window(b"\0\0\0\0")


def test_only_a_name_one_label_under_an_address_routes():
    assert rp.address_id_of(HOST_NAME, ZONE) == AID
    assert rp.address_id_of(f"apps-0a1b2c-home.{AID}.{ZONE}".upper(), ZONE) == AID
    for name in (f"{AID}.{ZONE}", f"x.y.{AID}.{ZONE}", f"home.{AID}.other.test", ZONE,
                 f"home.k7x2mo.{ZONE}", f"home.kkkkkk.{ZONE}", f"home.{AID}.{ZONE}.evil.test",
                 rp.tunnel_name(ZONE), "", "a..b"):
        assert rp.address_id_of(name, ZONE) is None, name


def test_tickets_verify_only_when_signed_fresh_and_well_formed(pki):
    keys = {KID: pki.ticket_key.public_key()}
    now = time.time()
    t = rp.verify_ticket(pki.ticket(now=now), keys, now=now)
    assert t["address_id"] == AID and t["limits"] == rp.Limits()
    assert rp.ticket_matches(t, pki.serial("a")) and not rp.ticket_matches(t, pki.serial("b"))
    assert rp.verify_ticket(pki.ticket(limits={"streams": 3}), keys)["limits"].streams == 3

    def reason(token, **kw):
        with pytest.raises(rp.TicketError) as e:
            rp.verify_ticket(token, kw.get("keys", keys), now=kw.get("now", now))
        return e.value.reason

    assert reason(pki.ticket(now=now - 4000)) == "ticket_expired"
    assert reason(pki.ticket(now=now, life=3 * 3600)) == "ticket_invalid"      # too long
    assert reason(pki.ticket(now=now + 3600)) == "ticket_invalid"               # from the future
    assert reason(pki.ticket(limits={"streams": 0})) == "ticket_invalid"
    assert reason(pki.ticket(aid="kkkkkk")) == "ticket_invalid"
    assert reason(pki.ticket(), keys={"t2": Ed25519PrivateKey.generate().public_key()}) == "ticket_invalid"
    assert reason(pki.ticket(), keys={KID: Ed25519PrivateKey.generate().public_key()}) == "ticket_invalid"
    v, body, sig = pki.ticket().split(".")
    forged = rp._b64e(rp._b64d(body).replace(AID.encode(), OTHER_AID.encode()))
    assert reason(f"{v}.{forged}.{sig}") == "ticket_invalid"
    assert reason("v2.a.b") == "ticket_invalid" and reason(None) == "ticket_invalid"


def test_reconnect_delays_stay_in_bounds():
    rng = random.Random(1)
    firsts = [rp.first_delay(rng) for _ in range(500)]
    assert min(firsts) >= 0.5 and max(firsts) <= 5.0
    for attempt in range(40):
        d = [rp.backoff_delay(attempt, rng) for _ in range(200)]
        assert 0.0 <= min(d) and max(d) <= min(60.0, 2.0 ** attempt)
    assert max(rp.backoff_delay(50, rng) for _ in range(2000)) > 30.0   # it does reach the cap


def test_proxy_header_round_trips_for_both_families():
    async def main():
        for src in (("203.0.113.9", 50123), ("2001:db8::7", 443), ("::ffff:198.51.100.2", 9)):
            r = asyncio.StreamReader()
            r.feed_data(rp.proxy_v2_header(src, ("127.0.0.1", 8443)) + b"rest")
            got = await rp.read_proxy_v2(r)
            ip = ipaddress.ip_address(src[0])
            want = str(getattr(ip, "ipv4_mapped", None) or ip)
            assert got == (want, src[1])
            assert await r.read(4) == b"rest"
        r = asyncio.StreamReader()
        r.feed_data(b"GET / HTTP/1.1\r\n\r\n")
        r.feed_eof()
        with pytest.raises(rp.ProtocolError):
            await rp.read_proxy_v2(r)
    run(main())


def test_the_relay_reads_the_name_from_a_real_client_hello():
    async def main():
        got = {}

        async def grab(reader, writer):
            got["buf"], got["sni"] = await relay_mod.peek_sni(reader, timeout=2)
            writer.close()
        srv = await asyncio.start_server(grab, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        ctx = ssl.create_default_context()
        try:
            await asyncio.wait_for(asyncio.open_connection(
                "127.0.0.1", port, ssl=ctx, server_hostname=HOST_NAME), 2)
        except (OSError, ssl.SSLError, asyncio.TimeoutError):
            pass
        srv.close()
        assert got["sni"] == HOST_NAME
        assert relay_mod.parse_sni(b"GET / HTTP/1.1\r\n") is None
        with pytest.raises(relay_mod._Incomplete):
            relay_mod.parse_sni(got["buf"][:40])
    run(main())


# ── end to end ──────────────────────────────────────────────────────────────

def test_a_browser_reaches_the_door_and_the_relay_sees_no_plaintext(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        try:
            c = rig.client()
            await rig.wait_up(c)
            answer, local = await browse(pki, rig.relay)
            # The door saw the browser's own address and port, from the header
            # the Host wrote: the Host's limits count browsers, not the tunnel.
            assert answer == f"src=127.0.0.1 sport={local[1]}\n".encode()
            assert rig.door.seen[-1] == ("127.0.0.1", local[1])

            # 2 MiB each way through one stream: everything arrives, and no
            # side ever queued more than one window for it.
            payload = MARKER + os.urandom(2 * 1024 * 1024)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", rig.relay.browser_port, ssl=browser_ctx(pki),
                server_hostname=HOST_NAME)
            writer.write(b"echo\n")

            async def send():
                for i in range(0, len(payload), 65536):
                    writer.write(payload[i:i + 65536])
                    await writer.drain()
            sender = asyncio.ensure_future(send())
            echoed = await asyncio.wait_for(reader.readexactly(5 + len(payload)), 20)
            await sender
            writer.close()
            assert echoed == b"echo\n" + payload

            seen = b"".join(b for _d, b in rig.taps)
            assert len(seen) > len(payload)
            assert MARKER not in seen and b"hello" not in seen and b"src=127.0.0.1" not in seen
            assert {d for d, _b in rig.taps} == {"to_host", "to_browser"}
        finally:
            await rig.close()
    run(main())


def test_flow_control_bounds_what_either_side_queues(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        queued = []
        orig_feed = rp.MuxStream.feed

        def feed(self, payload):
            orig_feed(self, payload)
            queued.append(self.queued)
        rp.MuxStream.feed = feed
        try:
            c = rig.client()
            await rig.wait_up(c)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", rig.relay.browser_port, ssl=browser_ctx(pki),
                server_hostname=HOST_NAME)
            writer.write(b"echo\n" + os.urandom(3 * 1024 * 1024))
            # The browser reads nothing for a while: the door's echo backs up
            # into the relay's queue, which stops at one window.
            await asyncio.sleep(1.0)
            assert queued and max(queued) <= rp.INITIAL_WINDOW
            await asyncio.wait_for(reader.readexactly(5 + 3 * 1024 * 1024), 20)
            writer.close()
        finally:
            rp.MuxStream.feed = orig_feed
            await rig.close()
    run(main())


def test_a_peer_sending_beyond_its_credit_is_refused():
    async def main():
        async def send(_frame):
            pass
        s = rp.MuxStream(1, send, window=rp.MAX_DATA)
        s.feed(b"x" * rp.MAX_DATA)
        with pytest.raises(rp.ProtocolError):
            s.feed(b"y")
    run(main())


def test_a_peer_that_half_closes_twice_or_sends_after_its_fin_is_refused():
    """FIN says the sender has no more bytes. A second FIN, or DATA after
    one, breaks the protocol: each FIN costs the receiver a queue entry that
    nothing reads once the first has ended the stream's writer, so a peer
    repeating it could grow the other side's memory for as long as the
    browser's connection stays open."""
    async def main():
        async def send(_frame):
            pass
        s = rp.MuxStream(1, send)
        s.peer_fin()
        with pytest.raises(rp.ProtocolError):
            s.peer_fin()
        t = rp.MuxStream(2, send)
        t.peer_fin()
        with pytest.raises(rp.ProtocolError):
            t.feed(b"x")
    run(main())


def test_a_host_repeating_fin_loses_its_tunnel(pki, loopback_door):
    """A Host that sends FIN again and again on a stream a browser keeps open
    has its tunnel closed at the second, and the relay holds nothing for it.
    The edge relay is this relay (vendored byte for byte), shared by every
    member Host, and its memory budget counts stream windows only."""
    import aiohttp

    async def main():
        # A heartbeat longer than the test, so only the FINs can end the
        # tunnel (this raw Host stops reading, and would miss its pongs).
        rig = await Rig(pki, heartbeat=60.0).start()
        try:
            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(
                    f"https://127.0.0.1:{rig.relay.tunnel_port}{rp.TUNNEL_PATH}",
                    protocols=(rp.PROTOCOL,), ssl=pki.client_ssl("a"),
                    server_hostname=rp.tunnel_name(ZONE),
                    headers={rp.TICKET_HEADER: pki.ticket("a")})
                ready = await ws.receive()
                assert '"ready"' in ready.data and AID in rig.relay.tunnels
                # A browser connects and stays: the relay sends OPEN.
                reader, writer = await asyncio.open_connection("127.0.0.1",
                                                               rig.relay.browser_port)
                writer.write(_client_hello(HOST_NAME))
                await writer.drain()
                sid = None
                while sid is None:
                    msg = await asyncio.wait_for(ws.receive(), 5)
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        ftype, s, _p = rp.decode(msg.data)
                        if ftype == rp.OPEN:
                            sid = s
                tunnel = rig.relay.tunnels[AID]
                for _ in range(2000):
                    try:
                        await ws.send_bytes(rp.encode(rp.FIN, sid))
                    except ConnectionError:
                        break       # the relay has closed the tunnel
                deadline = time.monotonic() + 3
                while AID in rig.relay.tunnels and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                held = [st._queue.qsize() for st in tunnel.streams.values()]
                writer.close()
                assert AID not in rig.relay.tunnels, f"tunnel still up, queues {held}"
        finally:
            await rig.close()
    run(main())


def test_a_host_that_half_closes_once_keeps_its_tunnel(pki, loopback_door):
    """One FIN from the Host, then the frames a half-closed stream still
    legitimately carries (credit for what the browser sends, and the RST that
    ends it), leave the tunnel up and end only that stream. The browser's own
    data keeps flowing to the Host after the Host's FIN."""
    import aiohttp

    async def main():
        rig = await Rig(pki, heartbeat=60.0).start()
        try:
            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(
                    f"https://127.0.0.1:{rig.relay.tunnel_port}{rp.TUNNEL_PATH}",
                    protocols=(rp.PROTOCOL,), ssl=pki.client_ssl("a"),
                    server_hostname=rp.tunnel_name(ZONE),
                    headers={rp.TICKET_HEADER: pki.ticket("a")})
                await ws.receive()
                reader, writer = await asyncio.open_connection("127.0.0.1",
                                                               rig.relay.browser_port)
                writer.write(_client_hello(HOST_NAME))
                await writer.drain()
                sid = None
                while sid is None:
                    msg = await asyncio.wait_for(ws.receive(), 5)
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        ftype, s, _p = rp.decode(msg.data)
                        if ftype == rp.OPEN:
                            sid = s
                tunnel = rig.relay.tunnels[AID]
                await ws.send_bytes(rp.encode(rp.FIN, sid))
                await ws.send_bytes(rp.encode(rp.WINDOW, sid, rp.window_payload(4096)))
                await ws.send_bytes(rp.encode(rp.WINDOW, sid, rp.window_payload(4096)))
                writer.write(b"more from the browser")      # still flows to the Host
                await writer.drain()
                await asyncio.sleep(0.5)
                assert AID in rig.relay.tunnels and sid in tunnel.streams
                await ws.send_bytes(rp.encode(rp.RST, sid))
                deadline = time.monotonic() + 3
                while sid in tunnel.streams and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                writer.close()
                assert AID in rig.relay.tunnels and sid not in tunnel.streams
        finally:
            await rig.close()
    run(main())


async def _talk(reader, writer, mine: bytes, after_theirs: bool) -> bytes:
    """One end of a stream's socket: sends `mine` then half-closes, and reads
    until the other end half-closes. With `after_theirs` it sends only once
    the other end's half close has come, so the other end is the first to FIN."""
    async def pour():
        for i in range(0, len(mine), 16384):
            writer.write(mine[i:i + 16384])
            await writer.drain()
        writer.write_eof()
    if after_theirs:
        got = await reader.read()
        await pour()
        return got
    pouring = asyncio.ensure_future(pour())
    got = await reader.read()
    await pouring
    return got


def test_a_half_close_in_either_direction_is_not_a_second_fin():
    """The one-FIN rule must not close a stream on a legitimate half close: one
    side FINs and the other keeps sending, with credit (WINDOW) still flowing
    to the side that has FINed. Two MuxStreams, each on a real socket, joined
    the way a tunnel joins them (a frame sent is decoded and handed to the
    other as remote_relay.serve and remote_tunnel._frame hand it); a
    ProtocolError there is what closes a tunnel, so it is recorded. Both ways:
    the browser's end FINs first, then the door's."""
    window = 64 * 1024

    async def case(browser_first: bool):
        errors: list[str] = []
        ends: dict[str, rp.MuxStream] = {}

        def link(to):
            async def send(frame):
                ftype, _sid, payload = rp.decode(frame)
                peer = ends[to]
                try:
                    if ftype == rp.DATA:
                        peer.feed(payload)
                    elif ftype == rp.WINDOW:
                        peer.grant(rp.parse_window(payload))
                    elif ftype == rp.FIN:
                        peer.peer_fin()
                    elif ftype == rp.RST:
                        peer.peer_reset()
                except rp.ProtocolError as exc:
                    errors.append(str(exc))
            return send

        browser_sock, a_sock = socket.socketpair()
        door_sock, b_sock = socket.socketpair()
        b_r, b_w = await asyncio.open_connection(sock=browser_sock)
        a_r, a_w = await asyncio.open_connection(sock=a_sock)
        d_r, d_w = await asyncio.open_connection(sock=door_sock)
        e_r, e_w = await asyncio.open_connection(sock=b_sock)
        ends["a"] = rp.MuxStream(1, link("b"), a_r, a_w, window=window)
        ends["b"] = rp.MuxStream(1, link("a"), e_r, e_w, window=window)
        # Five windows each way, so credit goes back and forth after the FIN.
        up, down = os.urandom(5 * window), os.urandom(5 * window)
        got_door, got_browser = await asyncio.wait_for(_both(
            ends, _talk(d_r, d_w, down, after_theirs=browser_first),
            _talk(b_r, b_w, up, after_theirs=not browser_first)), 20)
        assert errors == []
        assert got_door == up and got_browser == down
        assert not ends["a"].was_reset and not ends["b"].was_reset
        assert ends["a"].done.is_set() and ends["b"].done.is_set()

    async def main():
        await case(browser_first=True)
        await case(browser_first=False)
    run(main())


async def _both(ends, door_talk, browser_talk):
    """Both ends' talk and both streams' run, together; (door, browser) got."""
    door, browser, _a, _b = await asyncio.gather(door_talk, browser_talk,
                                                 ends["a"].run(), ends["b"].run())
    return door, browser


def test_only_a_passport_with_its_own_ticket_gets_a_tunnel(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        try:
            # A ticket for another passport's serial: refused as a mismatch,
            # and the Host asks for a fresh ticket before the next try.
            calls = []

            async def wrong(fresh):
                calls.append(fresh)
                return pki.ticket("b")
            c = rig.client("a", tickets=wrong)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(calls) < 3:
                await asyncio.sleep(0.02)
            st = c.status()[0]
            assert st["last_refusal"] == "ticket_mismatch" and st["connects"] == 0
            assert calls[0] is False and all(calls[1:])
            assert AID not in rig.relay.tunnels
            await c.stop()

            # A certificate from outside the network: no TLS handshake at all.
            stranger = rig.client("stranger", tickets=lambda f: _ready(pki.ticket("a")))
            await asyncio.sleep(0.5)
            st = stranger.status()[0]
            assert st["connects"] == 0 and st["attempts"] >= 2 and st["last_error"]
            assert AID not in rig.relay.tunnels
            await stranger.stop()

            # A relay whose certificate is for another name: the Host will not
            # talk to it, whatever address it was dialled on.
            bad = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            bad.load_verify_locations(pki.root_pem)
            bad.load_cert_chain(pki.passports["a"][0], pki.passports["a"][1])
            other_zone = tun.TunnelClient(
                address_id=AID, zone="elsewhere.example.test",
                relays=[tun.RelayAddress("127.0.0.1", rig.relay.tunnel_port)], ssl_context=bad,
                tickets=lambda f: _ready(pki.ticket("a")), door=("127.0.0.1", rig.door.port),
                delays=FAST)
            task = other_zone.start()
            await asyncio.sleep(0.4)
            assert other_zone.status()[0]["connects"] == 0
            assert "certificate" in (other_zone.status()[0]["last_error"] or "").lower()
            await other_zone.stop()
            task.cancel()

            # The right passport and its own ticket: up.
            good = rig.client("a")
            await rig.wait_up(good)
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await rig.close()
    run(main())


async def _ready(value):
    return value


def test_an_expired_ticket_closes_the_tunnel_and_a_fresh_one_reopens_it(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        try:
            calls = []

            async def short(fresh):
                calls.append(fresh)
                return pki.ticket("a", life=0.6)
            c = rig.client(tickets=short)
            await rig.wait_up(c)
            first = rig.relay.tunnels[AID]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and c.status()[0]["connects"] < 2:
                await asyncio.sleep(0.02)
            assert c.status()[0]["connects"] >= 2
            assert first.ws.closed and c.status()[0]["last_refusal"] == "ticket_expired"
            assert True in calls[1:]          # the reopen asked for a fresh ticket
        finally:
            await rig.close()
    run(main())


def test_a_refreshed_ticket_keeps_the_tunnel_open(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        try:
            async def short(fresh):
                return pki.ticket("a", life=1.0)
            c = rig.client(tickets=short, ticket_refresh=0.3)
            await rig.wait_up(c)
            await asyncio.sleep(2.0)
            assert c.status()[0]["connects"] == 1
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await rig.close()
    run(main())


def test_names_outside_a_live_address_are_dropped(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        try:
            c = rig.client()
            await rig.wait_up(c)
            for name in (f"home.{OTHER_AID}.{ZONE}", f"home.{AID}.other.example.test",
                         rp.tunnel_name(ZONE)):
                with pytest.raises((ssl.SSLError, OSError, asyncio.IncompleteReadError,
                                    asyncio.TimeoutError)):
                    await browse(pki, rig.relay, name=name, timeout=2)
            assert not rig.taps            # nothing reached any tunnel
        finally:
            await rig.close()
    run(main())


def test_the_host_refuses_a_stream_for_a_name_that_is_not_its_own(pki, loopback_door):
    """A relay that asks for someone else's name gets RST, and the door is
    never dialled. Driven by a relay that misbehaves on purpose."""
    from aiohttp import web

    async def main():
        door = Door(pki)
        await door.start()
        got = []

        async def handler(request):
            ws = web.WebSocketResponse(protocols=(rp.PROTOCOL,))
            await ws.prepare(request)
            await ws.send_str('{"type":"ready","address_id":"%s"}' % AID)
            await ws.send_bytes(rp.encode(rp.OPEN, 1, rp.open_payload(
                "198.51.100.4", 4000, f"home.{OTHER_AID}.{ZONE}")))
            async for msg in ws:
                got.append(rp.decode(msg.data))
                break
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get(rp.TUNNEL_PATH, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=pki.relay_ssl())
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        c = tun.TunnelClient(address_id=AID, zone=ZONE, relays=[tun.RelayAddress("127.0.0.1", port)],
                             ssl_context=pki.client_ssl("a"), tickets=lambda f: _ready("x"),
                             door=("127.0.0.1", door.port), delays=FAST)
        task = c.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not got:
            await asyncio.sleep(0.02)
        await c.stop()
        task.cancel()
        await runner.cleanup()
        await door.stop()
        assert got and got[0][0] == rp.RST and got[0][1] == 1
        assert door.seen == []
    run(main())


def test_a_host_that_breaks_the_protocol_loses_its_tunnel(pki):
    """The tunnel carries browsers in and nothing out: a Host that sends OPEN
    (asking the relay to connect somewhere) has broken the protocol, and the
    relay closes that tunnel. Driven by a Host that misbehaves on purpose,
    with a real passport and ticket. DATA beyond a stream's credit raises the
    same ProtocolError (test_a_peer_sending_beyond_its_credit_is_refused),
    which takes the same path to the close."""
    import aiohttp

    async def raw_tunnel(http, rig):
        ws = await http.ws_connect(
            f"https://127.0.0.1:{rig.relay.tunnel_port}{rp.TUNNEL_PATH}",
            protocols=(rp.PROTOCOL,), ssl=pki.client_ssl("a"),
            server_hostname=rp.tunnel_name(ZONE), headers={rp.TICKET_HEADER: pki.ticket("a")})
        ready = await ws.receive()
        assert '"ready"' in ready.data and AID in rig.relay.tunnels
        return ws

    async def closed(ws):
        msg = await asyncio.wait_for(ws.receive(), 5)
        return msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)

    async def main():
        rig = await Rig(pki).start()
        try:
            async with aiohttp.ClientSession() as http:
                ws = await raw_tunnel(http, rig)
                await ws.send_bytes(rp.encode(rp.OPEN, 1, rp.open_payload(
                    "192.0.2.1", 25, f"home.{AID}.{ZONE}")))
                assert await closed(ws)
                assert AID not in rig.relay.tunnels
        finally:
            await rig.close()
    run(main())


def test_a_tunnels_stream_limit_refuses_only_the_extra_connection(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        try:
            async def two(fresh):
                return pki.ticket("a", limits={"streams": 2})
            c = rig.client(tickets=two)
            await rig.wait_up(c)
            held = []
            for _ in range(2):
                r, w = await asyncio.open_connection(
                    "127.0.0.1", rig.relay.browser_port, ssl=browser_ctx(pki),
                    server_hostname=HOST_NAME)
                w.write(b"echo\n")
                assert await asyncio.wait_for(r.readline(), 5) == b"echo\n"
                held.append((r, w))
            with pytest.raises((ssl.SSLError, OSError, asyncio.IncompleteReadError,
                                asyncio.TimeoutError)):
                await browse(pki, rig.relay, timeout=2)
            # The relay itself refused it: it opened two streams, not three
            # (the Host would also refuse a third, as a second line).
            assert rig.relay.tunnels[AID]._next_sid == 3
            for r, w in held:                      # the two still work
                w.write(b"more\n")
                assert await asyncio.wait_for(r.readline(), 5) == b"more\n"
                w.close()
        finally:
            await rig.close()
    run(main())


def test_per_source_limits_refuse_a_flood_from_one_address(pki, loopback_door):
    async def main():
        rig = await Rig(pki, limits=relay_mod.RelayLimits(per_source_rate=1.0,
                                                          per_source_burst=3)).start()
        try:
            c = rig.client()
            await rig.wait_up(c)
            ok = refused = 0
            for _ in range(6):
                try:
                    answer, _ = await browse(pki, rig.relay, timeout=2)
                    ok += answer.startswith(b"src=")
                except (ssl.SSLError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                    refused += 1
            assert ok == 3 and refused == 3
        finally:
            await rig.close()
    run(main())


class FreezableProxy:
    """TCP between the Host and the relay. freeze() stops carrying bytes on
    every connection open now, without closing them: what a home router that
    forgot a connection's mapping looks like from both ends. New
    connections are carried as usual."""

    def __init__(self, target_port):
        self.target_port = target_port      # may change: the next connection goes there
        self.frozen: set = set()
        self.conns: list = []
        self.writers: list = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def freeze(self):
        self.frozen.update(id(c) for c in self.conns)

    async def _handle(self, cr, cw):
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            # Nothing behind it right now. A reset, not a close: the side that
            # closes first keeps a TIME_WAIT on its port (scripts/gate_loopback.py).
            cw.transport.abort()
            return
        self.writers += [cw, uw]
        token = object()
        self.conns.append(token)

        async def pump(r, w):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    if id(token) in self.frozen:
                        continue
                    w.write(data)
                    await w.drain()
            except OSError:
                pass
        await asyncio.gather(pump(cr, uw), pump(ur, cw))

    async def stop(self):
        """Close the listener and cut every connection it carries, so a frozen
        one does not leave either end waiting out its close timeout."""
        self.server.close()
        for w in self.writers:
            w.transport.abort()


def test_a_dead_tunnel_is_noticed_by_the_heartbeat_and_dialled_again(pki, loopback_door):
    async def main():
        rig = await Rig(pki).start()
        proxy = FreezableProxy(rig.relay.tunnel_port)
        await proxy.start()
        try:
            c = rig.client(port=proxy.port, heartbeat=0.3)
            await rig.wait_up(c)
            proxy.freeze()
            t0 = time.monotonic()
            deadline = t0 + 5
            while time.monotonic() < deadline and c.status()[0]["connects"] < 2:
                await asyncio.sleep(0.02)
            assert c.status()[0]["connects"] == 2
            # A pong is due within half a heartbeat; the bound is loose for a
            # loaded runner. Without the heartbeat the frozen tunnel would
            # stay "up" for good: TCP alone never notices.
            assert time.monotonic() - t0 < 3.0
            await rig.wait_up(c)
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await proxy.stop()
            await rig.close()
    run(main())


def test_a_relay_restart_is_survived(pki, loopback_door):
    """The relay comes back behind the same address the Host dials (here a
    forwarder, as the edge's front door is), on ports of its own. No port is
    bound twice: in the parallel gate lane another process could take a
    freed port in between, and the old relay's TIME_WAITs on it would refuse
    the Host's next dial (scripts/gate_loopback.py)."""
    async def main():
        rig = await Rig(pki).start()
        front = FreezableProxy(rig.relay.tunnel_port)
        await front.start()
        try:
            c = rig.client(port=front.port)
            await rig.wait_up(c)
            await rig.relay.stop()
            await asyncio.sleep(0.2)
            assert c.status()[0]["state"] in ("waiting", "connecting")
            await rig.start_relay()
            front.target_port = rig.relay.tunnel_port
            await rig.wait_up(c)
            assert c.status()[0]["connects"] >= 2
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await front.stop()
            await rig.close()
    run(main())


def test_the_newest_tunnel_for_an_address_wins_and_the_old_one_stays_off(pki, loopback_door):
    """A second connection with the same passport takes the address. The one
    it replaced does not dial straight back (the two would take turns for
    ever): it waits as long as a refusal and says why."""
    async def main():
        rig = await Rig(pki).start()
        try:
            first = rig.client()
            await rig.wait_up(first)
            t1 = rig.relay.tunnels[AID]
            second = rig.client()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and rig.relay.tunnels.get(AID) is t1:
                await asyncio.sleep(0.02)
            await rig.wait_up(second)
            assert t1.ws.closed
            await asyncio.sleep(0.15)          # under FAST.refused: the first is still waiting
            st = first.status()[0]
            assert st["last_refusal"] == "replaced" and st["state"] == "refused"
            assert "another machine" in st["last_error"]
            assert st["connects"] == 1 and second.status()[0]["connects"] == 1
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await rig.close()
    run(main())


def test_a_client_that_would_not_verify_the_relay_cannot_be_made():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # A door that is not loopback, so the refusal is the relay check's.
    with pytest.raises(ValueError, match="must be verified"):
        tun.TunnelClient(address_id=AID, zone=ZONE, relays=[tun.RelayAddress("127.0.0.1")],
                         ssl_context=ctx, tickets=lambda f: _ready("x"),
                         door=("192.0.2.1", 1))


# ── a door on loopback, a relay that lies, a Host that stops reading ───────

def test_the_door_is_never_the_hosts_loopback(pki):
    """A connection from the Host's own loopback passes the owner-session gate
    without a session (server/middleware/owner_session.py, OwnerSessionGate),
    so the tunnel must never hand a browser's bytes to a loopback socket.
    Every loopback or unspecified door is refused when the client is made,
    and a door given by a name is judged by the connection it made, before a
    byte goes on it: this door gets a connection and nothing on it."""
    for host in ("127.0.0.1", "127.3.4.5", "::1", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1",
                 "0.0.0.0", "::"):
        with pytest.raises(ValueError, match="loopback|unspecified"):
            tun.TunnelClient(address_id=AID, zone=ZONE, relays=[tun.RelayAddress("192.0.2.1")],
                             ssl_context=pki.client_ssl("a"), tickets=lambda f: _ready("x"),
                             door=(host, 8443))

    async def main():
        got: list[bytes] = []

        async def door(reader, writer):
            data = b""
            try:
                while chunk := await reader.read(65536):
                    data += chunk
            except OSError:
                pass
            got.append(data)
            writer.close()
        srv = await asyncio.start_server(door, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        rig = await Rig(pki).start()
        try:
            # "127.1" is a name to the client (no address literal) that the
            # resolver turns into 127.0.0.1, as "localhost" would.
            c = rig.client(door=("127.1", port))
            await rig.wait_up(c)
            with pytest.raises((ssl.SSLError, OSError, asyncio.IncompleteReadError,
                                asyncio.TimeoutError)):
                await browse(pki, rig.relay, timeout=2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not got:
                await asyncio.sleep(0.02)
            assert got == [b""]
        finally:
            await rig.close()
            srv.close()
    run(main())


def test_the_relay_cannot_raise_the_hosts_limits_above_its_ticket(pki, loopback_door):
    """The relay's ready names the limits, but a relay is not who decides
    them: each stream is a connection to the door, a file descriptor in the
    Host's own process. A relay that claims more than the ticket gives gets
    the ticket's: the third stream of a two-stream ticket is refused, and the
    door is dialled twice.

    The fake relay serves one tunnel and keeps it open, reading, until the
    Host stops. If it closed after the RST, the Host would dial again within
    FAST's 10-30 ms and get the three OPENs again: under load, before the door
    has recorded its two connections, the test then saw a second tunnel's RST
    (resets == [3, 3]) and up to four door connections."""
    from aiohttp import web

    async def main():
        door = Door(pki)
        await door.start()
        resets: list[int] = []
        tunnels = 0

        async def handler(request):
            nonlocal tunnels
            tunnels += 1
            ws = web.WebSocketResponse(protocols=(rp.PROTOCOL,))
            await ws.prepare(request)
            await ws.send_str(json.dumps({"type": "ready", "address_id": AID, "limits": {
                "streams": 65536, "window": 16 * 1024 * 1024}}))
            for sid in (1, 2, 3):
                await ws.send_bytes(rp.encode(rp.OPEN, sid, rp.open_payload(
                    "198.51.100.4", 4000 + sid, HOST_NAME)))
            # Read until the Host hangs up (c.stop() below), so its tunnel is
            # never dropped under it and it has no reason to dial again.
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    ftype, sid, _ = rp.decode(msg.data)
                    if ftype == rp.RST:
                        resets.append(sid)
            return ws
        app = web.Application()
        app.router.add_get(rp.TUNNEL_PATH, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=pki.relay_ssl())
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        c = tun.TunnelClient(
            address_id=AID, zone=ZONE, relays=[tun.RelayAddress("127.0.0.1", port)],
            ssl_context=pki.client_ssl("a"),
            tickets=lambda f: _ready(pki.ticket("a", limits={"streams": 2})),
            door=("127.0.0.1", door.port), delays=FAST)
        task = c.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (not resets or len(door.seen) < 2):
                await asyncio.sleep(0.02)
            # A little longer, so a third stream let through late would show.
            await asyncio.sleep(0.2)
            assert tunnels == 1
            assert resets == [3]
            assert len(door.seen) == 2
        finally:
            await c.stop()
            task.cancel()
            await runner.cleanup()
            await door.stop()
    run(main())


def _client_hello(name: str) -> bytes:
    """The first flight of a real TLS client for `name`, and nothing after."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return outgoing.read()


async def _stalled_tunnel(http, rig, who, aid, streams=8, bulk=True):
    """A Host with a real passport and ticket that stops reading its tunnel
    after ready (a laptop asleep behind a NAT that still holds the mapping,
    or a Host doing it on purpose), and browsers that send it more than the
    connection holds, until the relay's writes to it stop completing: aiohttp
    then waits in drain(), which has no timeout. Small socket buffers keep
    that to a few windows."""
    import socket as socket_mod
    ws = await http.ws_connect(
        f"https://127.0.0.1:{rig.relay.tunnel_port}{rp.TUNNEL_PATH}",
        protocols=(rp.PROTOCOL,), ssl=rig.pki.client_ssl(who),
        server_hostname=rp.tunnel_name(ZONE),
        headers={rp.TICKET_HEADER: rig.pki.ticket(who, aid=aid)})
    assert json.loads((await ws.receive()).data)["type"] == "ready"
    # aiohttp's client reads the socket into its own queue whether or not
    # anyone receives; a Host that is not reading stops at the socket.
    ws._writer.transport.pause_reading()
    relay_ws = rig.relay.tunnels[aid].ws
    for transport, opt in ((relay_ws._writer.transport, socket_mod.SO_SNDBUF),
                           (ws._writer.transport, socket_mod.SO_RCVBUF)):
        transport.get_extra_info("socket").setsockopt(socket_mod.SOL_SOCKET, opt, 4096)
    browsers = []
    for _ in range(streams):
        _r, w = await asyncio.open_connection("127.0.0.1", rig.relay.browser_port)
        w.write(_client_hello(f"home.{aid}.{ZONE}")
                + (os.urandom(rp.INITIAL_WINDOW) if bulk else b""))
        browsers.append(w)
    if not bulk:
        return ws, browsers
    # The flag aiohttp's drain() waits on: from here the relay's writes to
    # this tunnel do not complete.
    protocol = relay_ws._payload_writer._protocol
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not protocol._paused:
        await asyncio.sleep(0.02)
    assert protocol._paused, "the relay's writes to the tunnel still complete"
    return ws, browsers


def _cut(ws, browsers):
    for w in browsers:
        w.transport.abort()
    ws._writer.transport.abort()


def test_a_new_tunnel_is_ready_at_once_while_the_one_it_replaces_is_stalled(pki, loopback_door):
    """A Host that restarted dials in while its old connection still looks
    alive to the relay, with the relay's writes to it stuck (a laptop that
    slept in the middle of a download). The relay's goodbye to the old one
    must neither hold up the new one's ready nor take the new one's handler
    down with it. aiohttp's writers of one connection share a single drain
    future, and cancelling the old tunnel's stream tasks cancels it under
    every other waiter, the goodbye included."""
    import aiohttp

    async def main():
        rig = await Rig(pki, heartbeat=30.0).start()
        try:
            async with aiohttp.ClientSession() as http:
                old, browsers = await _stalled_tunnel(http, rig, "a", AID)
                try:
                    new = rig.client()
                    await rig.wait_up(new, timeout=3.0)
                    # Up on its first dial: the relay's handler for it did not
                    # die in the old one's goodbye.
                    st = new.status()[0]
                    assert st["attempts"] == 1 and st["connects"] == 1, st
                finally:
                    _cut(old, browsers)
        finally:
            await rig.close()
    run(main())


def test_a_stalled_tunnel_does_not_hold_up_the_kill_switch_for_another(pki, loopback_door):
    """The relay checks every tunnel's ticket and the deny list in one loop
    for the whole relay. A Host whose tunnel the relay can no longer write to
    must not stall that loop: no other tunnel's expiry or kill switch would
    happen. Both Hosts are denied, the stalled one first in the loop; the
    other is still cut off at once."""
    import aiohttp

    async def main():
        rig = await Rig(pki, heartbeat=30.0).start()
        try:
            async with aiohttp.ClientSession() as http:
                stuck, browsers = await _stalled_tunnel(http, rig, "b", OTHER_AID)
                try:
                    c = rig.client()
                    await rig.wait_up(c)
                    other = rig.relay.tunnels[AID]
                    assert list(rig.relay.tunnels) == [OTHER_AID, AID]
                    rig.relay.deny.update({OTHER_AID, AID})
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and not other.ws.closed:
                        await asyncio.sleep(0.02)
                    assert other.ws.closed
                finally:
                    _cut(stuck, browsers)
        finally:
            await rig.close()
    run(main())


def test_a_goodbye_that_never_finishes_holds_up_nothing_and_is_cut(pki, loopback_door,
                                                                   monkeypatch):
    """Whatever a goodbye waits on, nothing else in the relay waits on it: not
    the expiry and deny-list check, not a newer tunnel's handler. After
    goaway_timeout its connection is cut. Here every goodbye hangs for good,
    so each of the two is seen on its own (the stalled-socket tests above
    drive the real aiohttp path)."""
    async def hang(self, reason, reconnect_after_s):
        await asyncio.Event().wait()
    monkeypatch.setattr(relay_mod._Tunnel, "_goodbye", hang)

    async def main():
        rig = await Rig(pki, goaway_timeout=2.0).start()
        try:
            b = rig.client("b", aid=OTHER_AID)
            await rig.wait_up(b, aid=OTHER_AID)
            first = rig.client()
            await rig.wait_up(first)
            tb, ta = rig.relay.tunnels[OTHER_AID], rig.relay.tunnels[AID]
            assert list(rig.relay.tunnels) == [OTHER_AID, AID]

            # The deny list: b's goodbye hangs, and a's still starts at once.
            rig.relay.deny.update({OTHER_AID, AID})
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0 and not ta.closing:
                await asyncio.sleep(0.02)
            assert ta.closing and time.monotonic() - t0 < 1.0
            # ... and both are cut once goaway_timeout has passed.
            while time.monotonic() - t0 < 4.0 and not (tb.transport.is_closing()
                                                       and ta.transport.is_closing()):
                await asyncio.sleep(0.02)
            assert tb.transport.is_closing() and ta.transport.is_closing()
            rig.relay.deny.clear()
            await first.stop()
            await b.stop()

            # A newer tunnel for an address: up at once, the old goodbye hanging.
            old = rig.client()
            await rig.wait_up(old)
            t_old = rig.relay.tunnels[AID]
            new = rig.client()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.5 and not (
                    rig.relay.tunnels.get(AID) not in (None, t_old)
                    and new.status()[0]["state"] == "up"):
                await asyncio.sleep(0.02)
            assert new.status()[0]["state"] == "up" and time.monotonic() - t0 < 1.5
        finally:
            await rig.close()
    run(main())


def test_a_relays_retry_after_is_bounded_and_spread(pki):
    """A relay that turns the Host away with Retry-After: the Host waits at
    least that, but never longer than a refusal's wait (one header cannot
    park it for good), and not to the same second as every other Host the
    full relay turned away."""
    from aiohttp import web

    async def main():
        async def handler(request):
            return web.Response(status=503, headers={tun.REFUSAL_HEADER: "full",
                                                     "Retry-After": "100000"})
        app = web.Application()
        app.router.add_get(rp.TUNNEL_PATH, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=pki.relay_ssl())
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        c = tun.TunnelClient(
            address_id=AID, zone=ZONE, relays=[tun.RelayAddress("127.0.0.1", port)],
            ssl_context=pki.client_ssl("a"), tickets=lambda f: _ready(pki.ticket("a")),
            door=("192.0.2.1", 8443), delays=FAST, rng=random.Random(3))
        waits: list[float] = []

        async def record(seconds):
            waits.append(seconds)
            return len(waits) >= 3
        c._sleep = record
        try:
            await asyncio.wait_for(c.run(), 10)
        finally:
            await runner.cleanup()
        assert c.status()[0]["last_refusal"] == "full"
        assert all(FAST.refused <= w <= FAST.refused + FAST.cap for w in waits), waits
        assert len(set(waits)) == 3, waits
    run(main())


def test_a_refreshed_ticket_is_held_to_this_tunnels_passport_and_address(pki):
    """A ticket sent up an open tunnel must be for the passport the tunnel
    came in on and the address it holds. Otherwise a Host whose membership
    lapsed could keep its tunnel with another member's ticket."""
    import aiohttp

    async def main():
        rig = await Rig(pki).start()
        try:
            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(
                    f"https://127.0.0.1:{rig.relay.tunnel_port}{rp.TUNNEL_PATH}",
                    protocols=(rp.PROTOCOL,), ssl=pki.client_ssl("a"),
                    server_hostname=rp.tunnel_name(ZONE),
                    headers={rp.TICKET_HEADER: pki.ticket("a")})
                assert json.loads((await ws.receive()).data)["type"] == "ready"
                exp = rig.relay.tunnels[AID].exp

                async def answer(ticket):
                    await ws.send_str(json.dumps({"type": "ticket", "ticket": ticket}))
                    return json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
                for ticket in (pki.ticket("b", life=7000), pki.ticket("a", aid=OTHER_AID,
                                                                     life=7000)):
                    assert await answer(ticket) == {"type": "ticket_refused",
                                                    "reason": "ticket_mismatch"}
                    assert rig.relay.tunnels[AID].exp == exp
                assert (await answer(pki.ticket("a", life=7000)))["type"] == "ticket_ok"
                assert rig.relay.tunnels[AID].exp > exp
                await ws.close()
        finally:
            await rig.close()
    run(main())


def test_a_refreshed_tickets_stream_limit_applies_on_both_sides(pki, loopback_door):
    """The relay applies a refreshed ticket's stream limit at once. The Host
    must too, once the relay has taken the ticket, or every stream above the
    old limit is opened by the relay and refused by the Host until the
    tunnel next reconnects."""
    async def main():
        rig = await Rig(pki).start()
        try:
            calls = []

            async def tickets(fresh):
                calls.append(fresh)
                return pki.ticket("a", limits={"streams": 1 if len(calls) == 1 else 2})
            c = rig.client(tickets=tickets, ticket_refresh=0.2)
            await rig.wait_up(c)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and rig.relay.tunnels[AID].limits.streams != 2:
                await asyncio.sleep(0.02)
            assert rig.relay.tunnels[AID].limits.streams == 2
            await asyncio.sleep(0.1)          # the relay's ticket_ok has reached the Host
            held = []
            for _ in range(2):
                r, w = await asyncio.open_connection(
                    "127.0.0.1", rig.relay.browser_port, ssl=browser_ctx(pki),
                    server_hostname=HOST_NAME)
                w.write(b"echo\n")
                assert await asyncio.wait_for(r.readline(), 5) == b"echo\n"
                held.append(w)
            for w in held:
                w.close()
            assert c.status()[0]["connects"] == 1
        finally:
            await rig.close()
    run(main())


def test_a_refreshed_ticket_with_a_larger_window_does_not_break_the_tunnel(pki, loopback_door):
    """The window is agreed when the tunnel comes up, and the Host holds every
    stream to it. A refreshed ticket that raises it must not reach the
    relay's next stream before it reaches the Host's: that stream would
    overrun the Host's credit and close the whole tunnel."""
    async def main():
        rig = await Rig(pki).start()
        try:
            calls = []

            async def tickets(fresh):
                calls.append(fresh)
                big = {"window": 4 * 1024 * 1024} if len(calls) > 1 else None
                return pki.ticket("a", limits=big)
            c = rig.client(tickets=tickets, ticket_refresh=0.2)
            await rig.wait_up(c)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(calls) < 2:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)          # the relay has taken the refresh
            payload = os.urandom(4 * 1024 * 1024)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", rig.relay.browser_port, ssl=browser_ctx(pki),
                server_hostname=HOST_NAME)
            writer.write(b"echo\n" + payload)
            echoed = await asyncio.wait_for(reader.readexactly(5 + len(payload)), 20)
            writer.close()
            assert echoed == b"echo\n" + payload
            assert c.status()[0]["connects"] == 1
        finally:
            await rig.close()
    run(main())


# ── review fixes: trust anchor, tunnel class, cut sends, refusals ───────────

def _intermediate(root_key, root, cn: str):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(root.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(root_key, hashes.SHA256()))
    return key, cert


def _write_chain(d: Path, name: str, key, leaf, issuer) -> tuple[str, str]:
    """The leaf followed by its intermediate, as a passport's chain.crt holds them."""
    cp, kp = _write(d, name, key, leaf)
    Path(cp).write_bytes(leaf.public_bytes(serialization.Encoding.PEM)
                         + issuer.public_bytes(serialization.Encoding.PEM))
    return cp, kp


def test_only_a_passport_under_the_members_intermediate_gets_a_tunnel(pki, loopback_door,
                                                                       tmp_path):
    """The network's shape: root -> members intermediate -> passports, and
    root -> entity intermediate -> everything else, the relay's own
    certificate among them. With the members intermediate as the relay's only
    trust anchor, a passport gets a tunnel and a certificate under the other
    intermediate gets no handshake."""
    root_key, root = _ca("test network root")
    root_pem = tmp_path / "root.crt"
    root_pem.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    members_key, members = _intermediate(root_key, root, "test members intermediate")
    members_pem = tmp_path / "members.crt"
    members_pem.write_bytes(members.public_bytes(serialization.Encoding.PEM))
    entity_key, entity = _intermediate(root_key, root, "test entity intermediate")
    k, c = _leaf(entity_key, entity, dns=[rp.tunnel_name(ZONE)])
    relay_cert, relay_key = _write_chain(tmp_path, "relay", k, c, entity)
    k, c = _leaf(members_key, members, uri="urn:test:host:m", server=False)
    passport, passport_key = _write_chain(tmp_path, "passport", k, c, members)
    passport_serial = c.serial_number
    k, c = _leaf(entity_key, entity, uri="urn:test:pod:e", server=False)
    other, other_key = _write_chain(tmp_path, "other", k, c, entity)
    other_serial = c.serial_number

    async def main():
        rig = Rig(pki, tunnel_ssl=relay_mod.server_context(
            cert=relay_cert, key=relay_key, client_ca=str(members_pem)))
        await rig.start()
        try:
            def client(cert, key, serial):
                c = tun.TunnelClient(
                    address_id=AID, zone=ZONE,
                    relays=[tun.RelayAddress("127.0.0.1", rig.relay.tunnel_port, "test")],
                    ssl_context=tun.client_context(passport_cert=cert, passport_key=key,
                                                   network_root=str(root_pem)),
                    tickets=lambda f: _ready(pki.ticket("a", serial=serial)),
                    door=("127.0.0.1", rig.door.port), heartbeat=0.5, delays=FAST,
                    rng=random.Random(7))
                rig.clients.append(c)
                rig.tasks.append(c.start())
                return c

            # Under the entity intermediate, with a ticket for its own serial:
            # the root signed it, but the relay trusts the members CA alone.
            pod = client(other, other_key, other_serial)
            await asyncio.sleep(0.5)
            st = pod.status()[0]
            assert st["connects"] == 0 and st["attempts"] >= 2 and st["last_error"]
            assert AID not in rig.relay.tunnels
            await pod.stop()

            good = client(passport, passport_key, passport_serial)
            await rig.wait_up(good)
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await rig.close()
    run(main())


def test_a_relay_makes_its_tunnels_from_its_tunnel_class(pki, loopback_door):
    """The edge adds its accounting by subclassing the tunnel, named on its
    relay class, without touching remote_relay's module."""
    made = []

    class CountingTunnel(relay_mod._Tunnel):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    class CountingRelay(relay_mod.Relay):
        tunnel_class = CountingTunnel

    async def main():
        rig = await Rig(pki, relay_cls=CountingRelay).start()
        try:
            c = rig.client()
            await rig.wait_up(c)
            assert isinstance(rig.relay.tunnels[AID], CountingTunnel) and len(made) == 1
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await rig.close()
    run(main())
    assert relay_mod.Relay.tunnel_class is relay_mod._Tunnel


def test_a_browser_whose_open_is_cut_ends_its_stream_and_the_host_gets_rst(
        pki, loopback_door, monkeypatch):
    """The writers of one tunnel share aiohttp's drain future, so a cancel
    elsewhere can end the OPEN in CancelledError though nothing cancelled the
    browser's task. Its stream is still done (the edge's accounting waits on
    that), and the Host, which was sent OPEN, gets RST and lets go."""
    real_send = relay_mod._Tunnel.send
    real_new = relay_mod._Tunnel.new_stream
    streams = []
    cut = {"left": 1}

    async def send(self, frame):
        await real_send(self, frame)      # aiohttp writes the frame, then drains
        if frame[0] == rp.OPEN and cut["left"]:
            cut["left"] -= 1
            raise asyncio.CancelledError()

    def new_stream(self):
        s = real_new(self)
        streams.append(s)
        return s
    monkeypatch.setattr(relay_mod._Tunnel, "send", send)
    monkeypatch.setattr(relay_mod._Tunnel, "new_stream", new_stream)

    async def main():
        rig = await Rig(pki).start()
        try:
            c = rig.client()
            await rig.wait_up(c)
            with pytest.raises((OSError, ssl.SSLError, asyncio.IncompleteReadError,
                                asyncio.TimeoutError)):
                await browse(pki, rig.relay, timeout=2.0)
            assert len(streams) == 1
            await asyncio.wait_for(streams[0].done.wait(), 2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and c.status()[0]["streams"]:
                await asyncio.sleep(0.02)
            assert c.status()[0]["streams"] == 0, "the Host still holds the stream it was OPENed"
            assert rig.relay._streams_open == 0 and not rig.relay.tunnels[AID].streams
            # The tunnel carries the next browser as before.
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
            assert c.status()[0]["connects"] == 1
        finally:
            await rig.close()
    run(main())


def test_a_send_cut_under_a_stream_ends_it_with_rst():
    """A half whose send is cancelled under it (the shared drain future, not
    the peer's RST) can carry nothing more: the stream ends, and the peer is
    told, instead of the other half waiting for good."""
    async def main():
        sent = []

        async def send(frame):
            sent.append(frame[0])
            if frame[0] == rp.DATA:
                raise asyncio.CancelledError()
        a, b = socket.socketpair()
        try:
            reader, writer = await asyncio.open_connection(sock=a)
            s = rp.MuxStream(1, send)
            s.attach(reader, writer)
            b.sendall(b"from the browser")
            await asyncio.wait_for(s.run(), 2)
            assert s.done.is_set() and sent == [rp.DATA, rp.RST]
        finally:
            b.close()
    run(main())


def test_a_cut_send_on_the_hosts_tunnel_ends_that_tunnel_not_the_link(pki, loopback_door,
                                                                       monkeypatch):
    """A cancel that reaches the Host's tunnel though nothing cancelled the
    link (an RST or a pong whose drain was cut under it) ends that tunnel
    only: the link dials again, so the Host stays reachable."""
    real_serve = tun._Link._serve
    cut = {"left": 1}

    async def serve(self, ws):
        if cut["left"]:
            cut["left"] -= 1
            await asyncio.sleep(0.1)
            raise asyncio.CancelledError()
        await real_serve(self, ws)
    monkeypatch.setattr(tun._Link, "_serve", serve)

    async def main():
        rig = await Rig(pki).start()
        try:
            c = rig.client()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and c.status()[0]["connects"] < 2:
                await asyncio.sleep(0.02)
            assert c.status()[0]["connects"] == 2, c.status()
            await rig.wait_up(c)
            assert (await browse(pki, rig.relay))[0].startswith(b"src=")
        finally:
            await rig.close()
    run(main())


def test_a_refused_upgrade_ends_its_connection(pki, loopback_door):
    """A refused peer keeps no TLS connection to the relay: behind an edge's
    PROXY header, a second request on it would come from no source."""
    async def main():
        rig = await Rig(pki).start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", rig.relay.tunnel_port, ssl=pki.client_ssl("a"),
                server_hostname=rp.tunnel_name(ZONE))
            writer.write(f"GET {rp.TUNNEL_PATH} HTTP/1.1\r\nHost: {rp.tunnel_name(ZONE)}\r\n"
                         f"{rp.TICKET_HEADER}: v1.not.a-ticket\r\n\r\n".encode())
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(), 3)
            writer.close()
            head = reply.split(b"\r\n\r\n", 1)[0].lower()
            assert head.startswith(b"http/1.1 403")
            assert b"x-remote-refusal: ticket_invalid" in head
            assert b"connection: close" in head
        finally:
            await rig.close()
    run(main())


def test_a_cut_ticket_send_does_not_end_the_refreshes(pki, loopback_door, monkeypatch):
    """A refreshed ticket whose send is cut (the shared drain future) was
    still written; the next refresh goes up as before, so the tunnel does not
    run out of ticket an hour later."""
    real = tun._Link._send_text
    cut = {"left": 1}

    async def send_text(self, obj):
        await real(self, obj)
        if obj.get("type") == "ticket" and cut["left"]:
            cut["left"] -= 1
            raise asyncio.CancelledError()
    monkeypatch.setattr(tun._Link, "_send_text", send_text)

    async def main():
        rig = await Rig(pki).start()
        try:
            c = rig.client(ticket_refresh=0.1)
            await rig.wait_up(c)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and rig.ticket_calls.count(True) < 3:
                await asyncio.sleep(0.02)
            assert rig.ticket_calls.count(True) >= 3, rig.ticket_calls
            assert c.status()[0]["connects"] == 1
        finally:
            await rig.close()
    run(main())


def test_an_rst_mid_drain_does_not_take_the_tunnel_down(pki, loopback_door):
    """aiohttp's drain helper awaits one future that every writer of a
    WebSocket shares, unshielded, so cancelling a task that waits there
    cancels the future itself, and it stays cancelled until the socket
    drains. A Host that reads slowly, then resets a stream whose half waits
    in drain, used to leave every later send that reaches aiohttp's flush
    mark ending in CancelledError: the next ticket_ok took the Host's whole
    tunnel down. The RSTs end their own streams only; the drain future is
    left alone, the refreshed ticket is taken, and the tunnel stays."""
    import aiohttp

    async def main():
        rig = await Rig(pki, heartbeat=30.0).start()
        try:
            async with aiohttp.ClientSession() as http:
                # Every browser sends its ClientHello alone, and the bulk only
                # once its stream runs: what then waits in drain is a stream's
                # half, never a browser's OPEN.
                host, browsers = await _stalled_tunnel(http, rig, "a", AID, bulk=False)
                try:
                    tunnel = rig.relay.tunnels[AID]
                    protocol = tunnel.ws._payload_writer._protocol
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not (
                            len(tunnel.streams) == len(browsers)
                            and all(s._tasks for s in tunnel.streams.values())):
                        await asyncio.sleep(0.02)
                    assert len(tunnel.streams) == len(browsers)
                    for w in browsers:
                        w.write(os.urandom(rp.INITIAL_WINDOW))
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not (
                            protocol._paused and protocol._drain_waiter is not None):
                        await asyncio.sleep(0.02)
                    # The first pauses come and go while the Host's kernel
                    # still takes bytes; then the relay's writes stop for good.
                    await asyncio.sleep(0.3)
                    waiter = protocol._drain_waiter
                    assert waiter is not None and not waiter.done(), "nothing waits in drain"
                    # The Host's door resets every stream, among them the one
                    # whose send waits in drain.
                    for sid in sorted(tunnel.streams):
                        await host.send_bytes(rp.encode(rp.RST, sid))
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and tunnel.streams:
                        await asyncio.sleep(0.02)
                    assert not tunnel.streams
                    await asyncio.sleep(0.1)
                    now = protocol._drain_waiter
                    assert now is None or not now.cancelled(), \
                        "the RST cancelled the shared drain future"
                    # The next frame is the one that reaches aiohttp's flush
                    # mark, as one does every 64 KiB the relay writes.
                    tunnel.ws._writer._output_size = tunnel.ws._writer._limit
                    fresh = pki.ticket("a", life=1800.0)
                    await host.send_str(json.dumps({"type": "ticket", "ticket": fresh}))
                    await asyncio.sleep(0.3)
                    assert rig.relay.tunnels.get(AID) is tunnel and not tunnel.ws.closed, \
                        "a control frame after the RST took the tunnel down"
                    assert tunnel.exp == rp.verify_ticket(
                        fresh, {KID: pki.ticket_key.public_key()})["exp"]
                finally:
                    _cut(host, browsers)
        finally:
            await rig.close()
    run(main())


class _DrainingWs:
    """A WebSocket whose sends wait in drain the way aiohttp's do: on one
    future shared by every writer, awaited unshielded, and kept (cancelled or
    not) until the socket drains."""
    closed = False

    def __init__(self):
        self.sent = []
        self.waiter = None
        self.paused = True

    async def _write(self, kind, data):
        self.sent.append((kind, data))
        if self.paused:
            if self.waiter is None:
                self.waiter = asyncio.get_running_loop().create_future()
            await self.waiter

    async def send_bytes(self, data):
        await self._write("bytes", data)

    async def send_str(self, data):
        await self._write("str", data)

    def drained(self):
        self.paused = False
        waiter, self.waiter = self.waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


def test_a_cancelled_send_on_the_hosts_tunnel_leaves_the_drain_alone():
    """The Host's side of the same: a stream's half cancelled by the relay's
    RST while it waits in drain must not cancel the drain future under the
    tunnel's later sends (a refreshed ticket, aiohttp's pongs). They go out
    after it, in order. A cancel that reaches a send through the connection
    (torn down under it) ends that send in ConnectionResetError, never in a
    CancelledError that reads as a cancel of the caller."""
    async def main():
        link = tun._Link(None, tun.RelayAddress("relay.example.test"))
        ws = link._ws = _DrainingWs()
        half = asyncio.ensure_future(link._send(rp.encode(rp.DATA, 1, b"x")))
        await asyncio.sleep(0.05)
        assert ws.waiter is not None and not half.done()
        half.cancel()
        await asyncio.gather(half, return_exceptions=True)
        assert half.cancelled()
        assert not ws.waiter.cancelled(), "the cancel reached the shared drain future"
        ticket = asyncio.ensure_future(link._send_text({"type": "ticket", "ticket": "t"}))
        await asyncio.sleep(0.05)
        assert not ticket.done(), "a send after the cancel ended at once"
        ws.drained()
        await asyncio.wait_for(ticket, 2)
        assert [k for k, _ in ws.sent] == ["bytes", "str"]

        ws.paused = True
        cut = asyncio.ensure_future(link._send(rp.encode(rp.DATA, 2, b"y")))
        await asyncio.sleep(0.05)
        ws.waiter.cancel()          # the connection torn down under the send
        with pytest.raises(ConnectionResetError):
            await asyncio.wait_for(cut, 2)
    run(main())
