"""Remote access, the Host's side: turning it on, and the tunnel that keeps it.

The rig, all on this machine with real TLS and invented values:

  * a network PKI: a root, a members intermediate that signs the Host's
    passport (a real passport directory, server/bridge/passport.py), and the
    member door's and the relays' certificates;
  * a stub member door that speaks the real member-door frames (NDJSON over
    mutual TLS, {"ok", "data" | "error", "code", "id"}) and answers
    remote.enable and remote.ticket in the shapes privatae
    bridge/member_remote.py answers them, with tickets signed the way
    bridge/remote_ticket.py signs them (one relay, tunnel.<zone>, with one
    address per edge);
  * a stub ACME server (RFC 8555 newNonce and newAccount) that checks each
    JWS signature against the key it carries;
  * two reference relays (server/network/remote_relay.py), standing in for
    the two edges;
  * a pretend door on the machine, which reads the PROXY v2 header, ends the
    browser's TLS with the remote address's certificate and passes the
    request on, with the browser's address in X-Forwarded-For, to a pretend
    Host that writes down what it was sent;
  * a pretend browser.

The Host side under test is the real one: Membership with its member client,
and RemoteAccess (server/network/remote_access.py) with its ACME account
(remote_acme.py) and the tunnel (remote_tunnel.py).

Run: python3 -m pytest -q tests/test_remote_access_host_side.py
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import random
import secrets
import socket
import ssl
import stat
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import home_origin  # noqa: E402
from server.bridge import membership as ms  # noqa: E402
from server.bridge import passport as pp  # noqa: E402
from server.network import remote_access as ra  # noqa: E402
from server.network import remote_acme  # noqa: E402
from server.network import remote_protocol as rp  # noqa: E402
from server.network import remote_relay as relay_mod  # noqa: E402
from server.network import remote_tunnel as tun  # noqa: E402

ZONE = "remote.example.test"
AID = "k7x2m9"
KID = "door-test"
# Two edges' addresses as the door's ticket names them (documentation
# addresses); the rig dials each at its own relay on 127.0.0.1.
EDGE_A, EDGE_B = "192.0.2.10", "192.0.2.11"
ACCOUNT_PREFIX = "https://acme-v02.api.letsencrypt.org/acme/acct/"
NOW = dt.datetime.now(dt.timezone.utc)
FAST = tun.Delays(first=(0.01, 0.03), cap=0.2, stable=60.0, refused=0.3)

# The door's own words (privatae bridge/member_remote.py, member_server.py).
ERR_OFF = "remote access is not offered on this door yet"
ERR_NOT_ENTITLED = "remote access is not part of this membership"
ERR_NOT_ENABLED = "remote access is not turned on for this Host; ask remote.enable first"
ERR_MEMBERSHIP = "membership not active"


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def until(cond, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ── certificates ────────────────────────────────────────────────────────────

def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _ca(cn, issuer=None, issuer_key=None):
    key = ec.generate_private_key(ec.SECP256R1())
    b = (x509.CertificateBuilder().subject_name(_name(cn))
         .issuer_name(issuer.subject if issuer is not None else _name(cn))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None if issuer is None else 0), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                      content_commitment=False, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False,
                                      encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if issuer is not None:
        b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                            critical=False)
    return b.sign(issuer_key or key, hashes.SHA256()), key


def _leaf(pub, issuer, issuer_key, *, dns=(), uris=(), server=True, days=7):
    sans = [x509.DNSName(d) for d in dns] + [x509.UniformResourceIdentifier(u) for u in uris]
    b = (x509.CertificateBuilder().subject_name(_name("leaf")).issuer_name(issuer.subject)
         .public_key(pub).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(minutes=5)).not_valid_after(NOW + dt.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server
                                               else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                        critical=False))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


class PKI:
    def __init__(self, d: Path):
        d.mkdir(parents=True, exist_ok=True)
        self.dir = d
        self.root, self.root_key = _ca("Test Network Root")
        self.members, self.members_key = _ca("Test Members Intermediate", self.root, self.root_key)
        self.root_path = d / "root.pem"
        self.root_path.write_bytes(_pem(self.root))
        self.members_path = d / "members.pem"
        self.members_path.write_bytes(_pem(self.members))
        self.door_chain, self.door_key = self._server("net.privatae.ai", ["net.privatae.ai"], self.root,
                                                      self.root_key)
        self.relay_chain, self.relay_key = self._server("relay", [rp.tunnel_name(ZONE)], self.root,
                                                        self.root_key)
        # The public CA stand-in, and the remote address's certificate, which
        # only the door on the machine holds.
        self.web, web_key = _ca("Test Web PKI")
        self.web_path = d / "web.pem"
        self.web_path.write_bytes(_pem(self.web))
        self.site_chain, self.site_key = self._server("site", [f"*.{AID}.{ZONE}"], self.web, web_key)
        self.ticket_key = Ed25519PrivateKey.generate()

    def _server(self, name, dns, issuer, issuer_key):
        k = ec.generate_private_key(ec.SECP256R1())
        c = _leaf(k.public_key(), issuer, issuer_key, dns=dns)
        chain, key = self.dir / f"{name}.chain.pem", self.dir / f"{name}.key.pem"
        chain.write_bytes(_pem(c))
        key.write_bytes(_key_pem(k))
        return str(chain), str(key)

    def passport(self, directory: Path, host_uuid="0b5e4c2a-0000-4000-8000-00000000a001") -> pp.Passport:
        key = pp.new_key()
        leaf = _leaf(key.public_key(), self.members, self.members_key, server=False,
                     uris=[f"spiffe://net.privatae.ai/host/{host_uuid}",
                           "spiffe://net.privatae.ai/owner/acct-test",
                           "spiffe://net.privatae.ai/network/privatae"])
        na = leaf.not_valid_after_utc
        answer = {"cert_pem": _pem(leaf).decode(), "chain_pem": (_pem(leaf) + _pem(self.members)).decode(),
                  "root_pem": _pem(self.root).decode(), "not_after": na.isoformat(),
                  "renew_after": (na - dt.timedelta(days=6)).isoformat(), "network": "privatae",
                  "door": "net.privatae.ai:8643", "host_uuid": host_uuid, "entity_id": 412}
        info = pp.check_issued(answer, key, enrolment=True)
        return pp.save_enrolment(directory, key=key, cert_pem=answer["cert_pem"], chain_pem=answer["chain_pem"],
                                 root_pem=self.root_path.read_bytes(), info=info)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI(tmp_path_factory.mktemp("pki"))


# ── the stub ACME server ────────────────────────────────────────────────────

def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class StubAcme:
    """newNonce and newAccount (RFC 8555 7.2, 7.3). Each JWS is checked: ES256,
    the jwk it carries, a nonce this server gave, the URL it was posted to, and
    the signature over protected.payload with that key. A known key gets its
    account back (200); a new one a new account (201)."""

    def __init__(self):
        self.nonces: set[str] = set()
        self.accounts: dict[str, str] = {}
        self.posts: list[dict] = []
        self.refuse = False

    async def start(self):
        app = web.Application()
        app.router.add_get("/dir", self._dir)
        app.router.add_route("HEAD", "/nonce", self._nonce)
        app.router.add_post("/acct", self._acct)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()

    def _fresh(self):
        n = secrets.token_urlsafe(16)
        self.nonces.add(n)
        return n

    async def _dir(self, request):
        return web.json_response({"newNonce": self.base + "/nonce", "newAccount": self.base + "/acct"})

    async def _nonce(self, request):
        return web.Response(status=200, headers={"Replay-Nonce": self._fresh()})

    async def _acct(self, request):
        jws = json.loads(await request.text())
        prot = json.loads(_b64d(jws["protected"]))
        payload = json.loads(_b64d(jws["payload"]))
        self.posts.append({"protected": prot, "payload": payload})
        assert prot["alg"] == "ES256" and prot["url"] == self.base + "/acct"
        assert prot["nonce"] in self.nonces
        self.nonces.discard(prot["nonce"])
        jwk = prot["jwk"]
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_b64d(jwk["x"]), "big"),
                                            int.from_bytes(_b64d(jwk["y"]), "big"),
                                            ec.SECP256R1()).public_key()
        sig = _b64d(jws["signature"])
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pub.verify(der, f"{jws['protected']}.{jws['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
        if self.refuse:
            return web.json_response({"type": "urn:ietf:params:acme:error:rejectedIdentifier",
                                      "detail": "refused by the test"}, status=403)
        thumb = jwk["x"] + jwk["y"]
        known = thumb in self.accounts
        if not known:
            self.accounts[thumb] = f"{ACCOUNT_PREFIX}{1000 + len(self.accounts)}"
        uri = self.accounts[thumb]
        return web.json_response({"status": "valid", "orders": uri + "/orders"},
                                 status=200 if known else 201,
                                 headers={"Location": uri, "Replay-Nonce": self._fresh()})


# ── the stub member door ────────────────────────────────────────────────────

class StubDoor:
    """The member door's remote.enable and remote.ticket, in the shapes and
    with the words of privatae bridge/member_remote.py. Its DNS automation
    finishes after `dns_after` calls of remote.enable; the first answer after
    that carries the acme-dns credential, once."""

    def __init__(self, pki: PKI, relay_addrs=(EDGE_A, EDGE_B)):
        self.pki = pki
        self.relay_addrs = list(relay_addrs)
        self.offered = True
        self.entitled = True
        self.active = True
        self.dns_after = 1
        self.lifetime = 3600
        self.row = None
        self.calls: list[tuple[str, dict]] = []
        self.issued: list[dict] = []
        self.password = "invented-acme-dns-key-" + secrets.token_hex(12)
        self.server = None
        self.port = 0

    async def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.pki.door_chain, self.pki.door_key)
        ctx.load_verify_locations(str(self.pki.root_path))
        ctx.verify_mode = ssl.CERT_REQUIRED
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", 0, ssl=ctx, limit=1 << 20)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def ops(self, op):
        return [p for o, p in self.calls if o == op]

    async def _conn(self, reader, writer):
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        serial = x509.load_der_x509_certificate(der).serial_number
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                op, params, mid = msg.get("op"), msg.get("params") or {}, msg.get("id")
                self.calls.append((op, params))
                data, err, code = self.handle(op, params, serial)
                frame = ({"ok": True, "data": data} if err is None else
                         {"ok": False, "error": err, "code": code})
                if mid is not None:
                    frame["id"] = mid
                writer.write(json.dumps(frame).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, ssl.SSLError, ValueError):
            pass
        finally:
            writer.close()

    def _names(self):
        z = f"{AID}.{ZONE}"
        return {"zone": ZONE, "host_zone": z, "wildcard": f"*.{z}", "challenge": f"_acme-challenge.{z}",
                "tunnel": f"tunnel.{ZONE}"}

    def _may(self):
        if not self.active:
            return ERR_MEMBERSHIP
        if not self.entitled:
            return ERR_NOT_ENTITLED
        return None

    def handle(self, op, params, serial):
        if op == "heartbeat.ping":
            return {}, None, None
        if op == "remote.enable":
            return self.enable(params)
        if op == "remote.ticket":
            return self.ticket(serial)
        return {}, f"Unknown operation: {op}", 400

    def enable(self, params):
        if not self.offered:
            return {}, ERR_OFF, 503
        uri = params.get("acme_account_uri")
        if not isinstance(uri, str) or not uri.startswith(ACCOUNT_PREFIX) or len(uri) <= len(ACCOUNT_PREFIX):
            return {}, "acme_account_uri is not an account at the CA this zone allows", 400
        reissue = params.get("reissue_acme_dns", False)
        if not isinstance(reissue, bool):
            return {}, "reissue_acme_dns must be true or false", 400
        error = self._may()
        if error:
            return {}, error, 403
        r = self.row
        if r is None:
            r = self.row = {"address_id": AID, "uri": uri, "dns": "wanted", "password": None,
                            "delivered": None, "polls": 0, "issued": 0}
        elif r["uri"] != uri or reissue or r["dns"] in ("failed", "removed"):
            r.update(uri=uri, dns="wanted", polls=0)
            if reissue:
                r["delivered"] = None
        if r["dns"] == "wanted":
            r["polls"] += 1
            if r["polls"] > self.dns_after:
                r["dns"] = "ready"
                if r["delivered"] is None:
                    r["issued"] += 1
                    r["password"] = self.password if r["issued"] == 1 else f"{self.password}-{r['issued']}"
        credential = None
        if r["dns"] == "ready" and r["password"] is not None:
            credential = {"api_base": "https://acme.example.test", "username": "8d3f2c1a-user",
                          "password": r["password"], "subdomain": "4a9e7b2c-sub",
                          "fulldomain": "4a9e7b2c-sub.acme.example.test"}
            r["password"], r["delivered"] = None, time.time()
        out = {"address_id": AID, "names": self._names(), "dns": r["dns"], "acme_account_uri": r["uri"],
               "acme_dns_delivered": r["delivered"] is not None}
        if credential is not None:
            out["acme_dns"] = credential
        return out, None, 200

    def ticket(self, serial):
        if not self.offered:
            return {}, ERR_OFF, 503
        error = self._may()
        if error:
            return {}, error, 403
        if self.row is None:
            return {}, ERR_NOT_ENABLED, 409
        iat = int(time.time())
        relays = [{"name": f"tunnel.{ZONE}", "addrs": list(self.relay_addrs)}]
        limits = {"streams": 512, "open_rate": 100, "open_burst": 200, "window": 256 * 1024, "bps": 10 ** 11}
        payload = {"v": 1, "kid": KID, "host_uuid": "0b5e4c2a-0000-4000-8000-00000000a001",
                   "serial": format(serial, "x"), "address_id": AID, "zone": ZONE, "iat": iat,
                   "exp": iat + self.lifetime, "relays": relays, "limits": limits}
        self.issued.append(payload)
        return {"ticket": rp.sign_ticket(self.pki.ticket_key, payload), "exp": payload["exp"],
                "address_id": AID, "zone": ZONE, "relays": relays, "limits": limits}, None, 200


# ── the machine: a pretend door and a pretend Host ──────────────────────────

async def _recv_exact(loop, sock, n):
    buf = b""
    while len(buf) < n:
        chunk = await loop.sock_recv(sock, n - len(buf))
        if not chunk:
            raise asyncio.IncompleteReadError(buf, n)
        buf += chunk
    return buf


class PretendHost:
    """Answers every request with what it was sent: its peer, Host and
    X-Forwarded-For."""

    def __init__(self):
        self.seen: list[dict] = []

    async def start(self):
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _conn(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            hd = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            seen = {"peer": writer.get_extra_info("peername")[0], "line": lines[0], "host": hd.get("host"),
                    "xff": hd.get("x-forwarded-for")}
            self.seen.append(seen)
            body = json.dumps(seen).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()


class PretendDoor:
    """The machine's door, as the tunnel meets it: the PROXY v2 header, then
    the browser's TLS ended with the remote address's certificate, then the
    request passed to the Host with the browser's address in
    X-Forwarded-For."""

    def __init__(self, pki: PKI, host: PretendHost):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        self.ctx.load_cert_chain(pki.site_chain, pki.site_key)
        self.host = host
        self.sources: list[tuple[str, int]] = []
        self._tasks: set = set()

    async def start(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self._accepter = asyncio.ensure_future(self._accept())
        return self

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

    async def _one(self, conn):
        loop = asyncio.get_running_loop()
        conn.setblocking(False)
        try:
            head = await _recv_exact(loop, conn, 16)
            block = await _recv_exact(loop, conn, int.from_bytes(head[14:16], "big"))
            r = asyncio.StreamReader()
            r.feed_data(head + block)
            r.feed_eof()
            src = await rp.read_proxy_v2(r)
            self.sources.append(src)
            reader = asyncio.StreamReader()
            proto = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_accepted_socket(lambda: proto, conn, ssl=self.ctx)
            writer = asyncio.StreamWriter(transport, proto, reader, loop)
        except (OSError, ssl.SSLError, rp.ProtocolError, asyncio.IncompleteReadError):
            conn.close()
            return
        try:
            req = await reader.readuntil(b"\r\n\r\n")
            first, _, rest = req.partition(b"\r\n")
            hr, hw = await asyncio.open_connection("127.0.0.1", self.host.port)
            hw.write(first + b"\r\n" + f"X-Forwarded-For: {src[0]}\r\n".encode() + rest)
            await hw.drain()
            writer.write(await hr.read())
            await writer.drain()
            hw.close()
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()


async def browse(pki: PKI, relay, name=f"home.{AID}.{ZONE}"):
    """GET / from the browser, through `relay`: the JSON the Host answered."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(str(pki.web_path))
    reader, writer = await asyncio.wait_for(asyncio.open_connection(
        "127.0.0.1", relay.browser_port, ssl=ctx, server_hostname=name), 10)
    local = writer.get_extra_info("sockname")
    writer.write(f"GET / HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head[:80]
    return json.loads(body), local


# ── the whole rig ───────────────────────────────────────────────────────────

class Rig:
    def __init__(self, pki: PKI, tmp: Path, monkeypatch, *, refused_retry=30.0, ticket_refresh=1200.0):
        self.pki, self.tmp = pki, tmp
        self.acme_dir = tmp / "remote-acme"
        self.state_path = tmp / "remote-access.json"
        self.status_path = tmp / "cert-status.json"
        monkeypatch.setenv(ra.ACME_DIR_ENV, str(self.acme_dir))
        monkeypatch.setenv(ra.CERT_STATUS_ENV, str(self.status_path))
        monkeypatch.setenv(home_origin.REMOTE_STATE_ENV, str(self.state_path))
        monkeypatch.delenv(home_origin.REMOTE_URL_ENV, raising=False)
        # The rig's door is on 127.0.0.1, the one address every test machine
        # has. The tunnel refuses a loopback door (scripts/
        # test_remote_door_loopback.py proves that guard); this lets that one
        # address through for the rig and keeps the rest of it.
        real = tun.door_refusal
        monkeypatch.setattr(tun, "door_refusal", lambda h: None if h == "127.0.0.1" else real(h))
        self.refused_retry = refused_retry
        self.ticket_refresh = ticket_refresh
        self.relays: dict[str, relay_mod.Relay] = {}

    async def start(self):
        self.acme = await StubAcme().start()
        self.door = await StubDoor(self.pki).start()
        self.host = await PretendHost().start()
        self.machine_door = await PretendDoor(self.pki, self.host).start()
        for addr in (EDGE_A, EDGE_B):
            r = relay_mod.Relay(zone=ZONE, ticket_keys={KID: self.pki.ticket_key.public_key()},
                                tunnel_ssl=relay_mod.server_context(cert=self.pki.relay_chain,
                                                                    key=self.pki.relay_key,
                                                                    client_ca=str(self.pki.members_path)),
                                heartbeat=0.5, check_interval=0.1)
            await r.start(host="127.0.0.1")
            self.relays[addr] = r
        self.passport = self.pki.passport(self.tmp / "passport")
        self.m = ms.Membership(directory=self.tmp / "passport", anchor=self.pki.root_path,
                               door_host="127.0.0.1", door_port=self.door.port)
        self.m._remote = ra.RemoteAccess(
            self.m, state_path=self.state_path, acme_directory=self.acme.base + "/dir",
            acme_allow_http=True, dial=lambda a: ("127.0.0.1", self.relays[a].tunnel_port),
            door=lambda: ("127.0.0.1", self.machine_door.port),
            tunnel_kw=dict(heartbeat=0.5, delays=FAST, rng=random.Random(7),
                           ticket_refresh=self.ticket_refresh),
            enable_poll=0.1, refused_retry=self.refused_retry, check_every=0.1,
            ticket_reuse=min(0.3, self.ticket_refresh / 2))
        await self.m.boot()
        await until(lambda: self.m.client is not None and self.m.client.connected, what="member door")
        return self

    @property
    def remote(self) -> ra.RemoteAccess:
        return self.m.remote

    async def wait_links(self, n=2, timeout=10.0):
        def up():
            t = self.remote.tunnel
            return (t is not None and sum(1 for s in t.status() if s["state"] == "up") == n
                    and all(AID in r.tunnels for r in self.relays.values()))
        await until(up, timeout, "every link up")

    async def close(self):
        try:
            await self.m.shutdown()
        finally:
            for r in self.relays.values():
                await r.stop()
            await self.machine_door.stop()
            await self.host.stop()
            await self.door.stop()
            await self.acme.stop()
            home_origin.set_remote(None)


@pytest.fixture(autouse=True)
def _no_remote_left_behind():
    yield
    home_origin.set_remote(None)


# ── the tests ───────────────────────────────────────────────────────────────

def test_turn_on_reaches_the_host_from_a_browser_through_each_relay(pki, tmp_path, monkeypatch):
    """Turn on, end to end: the Host's own ACME account, remote.enable until
    the DNS is ready, the credential and cloud.json where the certificate job
    reads them, the address set, one link per relay address in the ticket,
    and a browser through each relay reaching the Host through the door, with
    its own address in the PROXY header and in X-Forwarded-For."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            st = await rig.remote.turn_on()
            assert st["on"] is True and st["address_id"] == AID
            await until(lambda: rig.remote.state.dns == "ready" and rig.remote.state.credential_stored,
                        what="DNS ready")
            # The account: one newAccount, no contact, terms agreed, its URL
            # what remote.enable carried, kept in lego's layout.
            assert len(rig.acme.posts) == 1
            assert rig.acme.posts[0]["payload"] == {"termsOfServiceAgreed": True}
            enables = rig.door.ops("remote.enable")
            assert len(enables) >= 2 and enables[0]["acme_account_uri"].startswith(ACCOUNT_PREFIX)
            assert all("reissue_acme_dns" not in p for p in enables)
            acct, key = remote_acme.account_paths(rig.acme_dir / "acme-account", rig.acme.base + "/dir")
            doc = json.loads(acct.read_text())
            assert doc["email"] == remote_acme.ACCOUNT_LABEL
            assert doc["registration"]["uri"] == enables[0]["acme_account_uri"]
            assert doc["registration"]["body"]["status"] == "valid"
            assert b"BEGIN EC PRIVATE KEY" in key.read_bytes()
            assert stat.S_IMODE(key.stat().st_mode) == 0o600
            # What the certificate job reads (the cloud box's own files).
            creds = json.loads((rig.acme_dir / "acme-dns.json").read_text())
            assert creds[f"{AID}.{ZONE}"]["password"] == rig.door.password
            assert stat.S_IMODE((rig.acme_dir / "acme-dns.json").stat().st_mode) == 0o600
            assert json.loads((rig.acme_dir / "acme-dns-api.json").read_text()) == {
                "api_base": "https://acme.example.test"}
            cloud = json.loads((rig.acme_dir / "cloud.json").read_text())
            assert cloud["address_id"] == AID and cloud["zone"] == ZONE
            assert cloud["name"] == home_origin.HOME.handle
            # The address: set now, and kept for the next start.
            want = f"https://{home_origin.HOME.handle}.{AID}.{ZONE}"
            assert home_origin.REMOTE is not None and home_origin.REMOTE.origin == want
            assert json.loads(rig.state_path.read_text())["remote_url"] == want
            assert home_origin._load_remote().origin == want

            await rig.wait_links(2)
            links = rig.remote.status()["links"]
            assert sorted(link["relay"] for link in links) == [EDGE_A, EDGE_B]
            assert all(link["state"] == "up" for link in links)
            for addr, relay in rig.relays.items():
                seen, local = await browse(pki, relay)
                assert seen["host"] == f"home.{AID}.{ZONE}" and seen["line"] == "GET / HTTP/1.1"
                assert seen["xff"] == local[0], (addr, seen)
                assert rig.machine_door.sources[-1] == (local[0], local[1])
            assert len(rig.host.seen) == 2
        finally:
            await rig.close()
    run(go())


def test_one_link_per_relay_address_and_only_bare_ip_literals():
    """The door's ticket carries one relay, tunnel.<zone>, with one address
    per edge, each a bare IP literal (privatae bridge/remote_ticket.py
    RemoteConfig.relays): each address is a link, once. A ticket naming a
    relay any other way (a name, brackets, a port, a zone index, spaces) is
    refused whole, never partly dialled."""
    relays = [{"name": f"tunnel.{ZONE}", "addrs": [EDGE_A, EDGE_B, EDGE_A, "2001:DB8::7"]}]
    assert ra.relay_hosts(relays) == (EDGE_A, EDGE_B, "2001:db8::7")
    for bad in ("edge.example.test", "[2001:db8::8]", f"{EDGE_A}:443", "fe80::1%eth0", f" {EDGE_A}",
                "", "0.0.0.0", "::", "224.0.0.1", 5, None):
        with pytest.raises(ra.BadRelays):
            ra.relay_hosts([{"name": f"tunnel.{ZONE}", "addrs": [EDGE_A, bad]}])
    for bad in (None, [], "junk", ["junk"], [{"addrs": []}], [{"addrs": "no"}]):
        with pytest.raises(ra.BadRelays):
            ra.relay_hosts(bad)


def test_a_ticket_naming_a_relay_by_name_starts_no_tunnel(pki, tmp_path, monkeypatch):
    """The whole ticket is refused: no link is dialled, Settings says why."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            rig.door.dns_after = 0
            rig.door.relay_addrs = [EDGE_A, "edge-b.example.test"]
            await rig.remote.turn_on()
            await until(lambda: "not an IP literal" in (rig.remote.status()["problem"] or ""),
                        what="the refusal to be shown")
            await asyncio.sleep(0.5)
            assert rig.remote.tunnel is None
            assert all(AID not in r.tunnels for r in rig.relays.values())
        finally:
            await rig.close()
    run(go())


@pytest.mark.parametrize("case", ["not_entitled", "membership", "off"])
def test_remote_enable_refused_turns_nothing_on(pki, tmp_path, monkeypatch, case):
    """remote.enable refused: 403 with the door's words when the membership
    carries no remote access or is not active, 503 when the door offers none.
    Nothing is turned on, nothing is written for the certificate job, and no
    address is set."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            if case == "not_entitled":
                rig.door.entitled = False
            elif case == "membership":
                rig.door.active = False
            else:
                rig.door.offered = False
            with pytest.raises(ra.RemoteError) as e:
                await rig.remote.turn_on()
            if case == "off":
                assert e.value.status == 503 and "does not offer remote access" in e.value.message
            else:
                assert e.value.status == 403
                assert e.value.message == (ERR_NOT_ENTITLED if case == "not_entitled" else ERR_MEMBERSHIP)
            assert rig.remote.state.on is False and rig.remote.tunnel is None
            assert home_origin.REMOTE is None
            assert not (rig.acme_dir / "acme-dns.json").exists()
            assert not (rig.acme_dir / "cloud.json").exists()
            assert not rig.door.ops("remote.ticket")
        finally:
            await rig.close()
    run(go())


@pytest.mark.parametrize("case", ["not_entitled", "off"])
def test_remote_ticket_refused_starts_no_tunnel(pki, tmp_path, monkeypatch, case):
    """remote.ticket refused (403 not entitled, 503 off) after the address
    exists: no tunnel starts, the refusal is shown, and the Host does not ask
    again before the refusal's wait is over."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch, refused_retry=30.0).start()
        try:
            rig.door.dns_after = 0
            orig = rig.door.ticket

            def refuse(serial):
                if case == "off":
                    return {}, ERR_OFF, 503
                return {}, ERR_NOT_ENTITLED, 403
            rig.door.ticket = refuse
            await rig.remote.turn_on()
            await until(lambda: rig.remote.refusal is not None, what="the refusal")
            assert rig.remote.refusal.code == (503 if case == "off" else 403)
            asked = len(rig.door.ops("remote.ticket"))
            assert asked >= 1
            await asyncio.sleep(1.0)
            assert rig.remote.tunnel is None
            assert all(AID not in r.tunnels for r in rig.relays.values())
            assert len(rig.door.ops("remote.ticket")) == asked
            st = rig.remote.status()
            assert st["refused"] and st["links"] == [] and not st["running"]
            assert st["problem"]
            rig.door.ticket = orig
        finally:
            await rig.close()
    run(go())


def test_tunnel_stops_when_a_lapsed_membership_is_refused_a_ticket(pki, tmp_path, monkeypatch):
    """Up through both relays; then the membership lapses and the next
    ticket refresh is refused (403, "membership not active"): the tunnel
    stops on both relays, Settings says why, and nothing dials again until
    the refusal's wait (REFUSED_RETRY_S, here 30 s) is over."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch, refused_retry=30.0, ticket_refresh=0.5).start()
        try:
            rig.door.dns_after = 0
            await rig.remote.turn_on()
            await rig.wait_links(2)
            rig.door.active = False
            await until(lambda: rig.remote.tunnel is None, what="the tunnel to stop")
            await until(lambda: all(AID not in r.tunnels for r in rig.relays.values()),
                        what="both relays to drop the tunnel")
            st = rig.remote.status()
            assert st["refused"] == ERR_MEMBERSHIP and st["links"] == [] and st["running"] is False
            assert "membership not active" in (st["problem"] or "")
            asked = len(rig.door.ops("remote.ticket"))
            await asyncio.sleep(1.5)
            assert len(rig.door.ops("remote.ticket")) == asked
            assert all(AID not in r.tunnels for r in rig.relays.values())
            assert rig.remote.state.on is True       # the owner's choice stands
        finally:
            await rig.close()
    run(go())


def test_tickets_are_refreshed_over_the_open_tunnel(pki, tmp_path, monkeypatch):
    """Each link sends a fresh ticket up its open tunnel on its timer and the
    relay takes it (its expiry moves on); the two links share one fetch, so
    the door is asked about once a round, not twice."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch, ticket_refresh=0.6).start()
        try:
            rig.door.dns_after = 0
            await rig.remote.turn_on()
            await rig.wait_links(2)
            first = {a: r.tunnels[AID].exp for a, r in rig.relays.items()}
            before = len(rig.door.issued)
            await asyncio.sleep(1.1)              # tickets of a later second
            await until(lambda: all(r.tunnels.get(AID) is not None and r.tunnels[AID].exp > first[a]
                                    for a, r in rig.relays.items()), what="the relays to take a fresh ticket")
            assert len(rig.door.issued) > before
            # Two links, ~2 rounds: shared fetches keep it near one a round.
            rounds = len(rig.door.issued) - before
            assert rounds <= 3, rounds
            # The same tunnels: a refresh never redials.
            assert all(s["connects"] == 1 for s in rig.remote.tunnel.status())
        finally:
            await rig.close()
    run(go())


def test_the_acme_dns_credential_is_never_logged_or_shown(pki, tmp_path, monkeypatch, caplog):
    """The credential is in acme-dns.json (0600) and nowhere else: not in any
    log record at any level, not in Settings' status, not in the Host's own
    state file."""
    caplog.set_level(logging.DEBUG)

    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            await rig.remote.turn_on()
            await until(lambda: rig.remote.state.credential_stored, what="the credential")
            await rig.wait_links(2)
            from server.routes import remote_access as routes
            st = rig.remote.status()
            return rig.door.password, json.dumps(st) + routes.summary(st), rig.state_path.read_text(), \
                (rig.acme_dir / "acme-dns.json").read_text()
        finally:
            await rig.close()
    password, shown, state, creds = run(go())
    assert password in creds
    assert password not in shown and password not in state
    assert caplog.records, "nothing was logged at all; the check would prove nothing"
    for rec in caplog.records:
        text = rec.getMessage() + (rec.exc_text or "") + repr(rec.args)
        assert password not in text, rec.name


def test_a_lost_credential_is_asked_for_again(pki, tmp_path, monkeypatch):
    """The credential comes once. A Host whose copy is gone asks remote.enable
    with reissue_acme_dns, and keeps the new one."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            rig.door.dns_after = 0
            await rig.remote.turn_on()
            await until(lambda: rig.remote.state.credential_stored, what="the credential")
            (rig.acme_dir / "acme-dns.json").unlink()
            rig.remote.state.credential_stored = False
            rig.remote.wake()
            await until(lambda: any(p.get("reissue_acme_dns") for p in rig.door.ops("remote.enable")),
                        what="a reissue")
            await until(lambda: (rig.acme_dir / "acme-dns.json").exists(), what="the new credential")
            creds = json.loads((rig.acme_dir / "acme-dns.json").read_text())
            assert creds[f"{AID}.{ZONE}"]["password"] == f"{rig.door.password}-2"
        finally:
            await rig.close()
    run(go())


def test_passport_renewal_rebuilds_the_tunnel_with_the_new_certificate(pki, tmp_path, monkeypatch):
    """A renewed passport: the tunnel starts again with the new certificate,
    and its tickets are for the new serial (the relay refuses a ticket for
    any other passport)."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            rig.door.dns_after = 0
            await rig.remote.turn_on()
            await rig.wait_links(2)
            old_serial = rig.m.passport.serial
            assert all(format(r.tunnels[AID].serial, "x") == old_serial for r in rig.relays.values())
            renewed = pki.passport(tmp_path / "passport-renewed")
            rig.m.passport = renewed
            # The member client dials again with the new certificate first,
            # as Membership.renew_if_due does, then remote access hears of it.
            rig.m.client._cert = renewed.chain_path
            rig.m.client._key = renewed.key_path
            await rig.m.client.reconnect()
            rig.remote.passport_renewed()
            # Well inside NOT_CONNECTED_S: the new connection wakes it.
            await until(lambda: rig.remote.tunnel is not None and rig.remote._tunnel_serial == renewed.serial,
                        timeout=ra.NOT_CONNECTED_S / 2, what="a tunnel on the new passport")
            await until(lambda: all(r.tunnels.get(AID) is not None
                                    and format(r.tunnels[AID].serial, "x") == renewed.serial
                                    for r in rig.relays.values()), what="both relays on the new serial")
            assert rig.door.issued[-1]["serial"] == renewed.serial
        finally:
            await rig.close()
    run(go())


def test_turning_off_stops_the_tunnel_and_clears_the_address(pki, tmp_path, monkeypatch):
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            rig.door.dns_after = 0
            await rig.remote.turn_on()
            await rig.wait_links(2)
            st = await rig.remote.turn_off()
            assert st["on"] is False and st["running"] is False and st["address"] is None
            assert rig.remote.tunnel is None and home_origin.REMOTE is None
            await until(lambda: all(AID not in r.tunnels for r in rig.relays.values()),
                        what="the relays to drop the tunnel")
            assert json.loads(rig.state_path.read_text())["on"] is False
            assert home_origin._load_remote() is None
        finally:
            await rig.close()
    run(go())


def test_the_remote_address_can_be_set_while_the_host_runs():
    """home_origin.set_remote changes every reader at once: the origins, the
    app zones and the app-origin parser."""
    from server import host_names
    from server.applications import app_origin
    try:
        home_origin.set_remote(f"https://{home_origin.HOME.handle}.{AID}.{ZONE}")
        assert home_origin.is_remote_host(f"home.{AID}.{ZONE}")
        assert f".{AID}.{ZONE}" in host_names.APP_ZONES
        assert app_origin.classify(f"apps-{'a' * 20}-{home_origin.HOME.handle}.{AID}.{ZONE}").kind == app_origin.APP
        assert f"{AID}.{ZONE}" in app_origin.frame_ancestors_for(home_origin.HOME.handle)
        with pytest.raises(ValueError):
            home_origin.set_remote("http://home.k7x2m9.remote.example.test")
    finally:
        home_origin.set_remote(None)
    assert home_origin.REMOTE is None and host_names.APP_ZONES == (home_origin.HOME.zone,)
    assert not home_origin.is_remote_host(f"home.{AID}.{ZONE}")


def test_without_a_door_on_this_install_it_cannot_be_turned_on(tmp_path, monkeypatch):
    """An install from before the door (no CSERIES_REMOTE_ACME_DIR or
    CSERIES_REMOTE_DOOR) says so instead of turning anything on."""
    monkeypatch.delenv(ra.ACME_DIR_ENV, raising=False)
    monkeypatch.delenv(ra.DOOR_ENV, raising=False)

    class M:
        passport = client = None
    r = ra.RemoteAccess(M(), state_path=tmp_path / "s.json")
    assert "no door for remote access" in r.available()
    with pytest.raises(ra.RemoteError) as e:
        run(r.turn_on())
    assert e.value.status == 409
    monkeypatch.setenv(ra.ACME_DIR_ENV, str(tmp_path / "acme"))
    monkeypatch.setenv(ra.DOOR_ENV, "127.0.0.1:8443")
    assert "cannot be the door" in r.available()


def _route_word(ip: str) -> str:
    """An address as /proc/net/route prints it: its four network-order bytes
    read as one native-order word."""
    return "%08X" % int.from_bytes(socket.inet_aton(ip), sys.byteorder)


def _routes(tmp_path, *rows) -> Path:
    """A /proc/net/route of (iface, dest, gateway, mask) rows, each up."""
    out = ["Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"]
    for iface, dest, gw, mask in rows:
        flags = 0x3 if gw != "0.0.0.0" else 0x1
        out.append(f"{iface}\t{_route_word(dest)}\t{_route_word(gw)}\t{flags:04X}\t0\t0\t0\t"
                   f"{_route_word(mask)}\t0\t0\t0")
    p = tmp_path / "route"
    p.write_text("\n".join(out) + "\n")
    return p


# The Host's routes in the guest with and without the remote network
# (cseries-remote.network 10.89.79.0/29 beside its database's and the apps').
ON_REMOTE = (("eth0", "0.0.0.0", "10.89.0.1", "0.0.0.0"), ("eth0", "10.89.0.0", "0.0.0.0", "255.255.255.0"),
             ("eth1", "10.89.78.0", "0.0.0.0", "255.255.255.0"), ("eth2", "10.89.79.0", "0.0.0.0", "255.255.255.248"))
OFF_REMOTE = ON_REMOTE[:3]


def test_settings_says_when_the_host_is_not_on_its_doors_network(tmp_path, monkeypatch):
    """A Host whose unit did not take the remote drop-in (a key Quadlet
    refuses, a section systemd ignores) still runs on its old networks after
    the guest's restart. With a door configured, Settings says the Host is not
    on the door's network, and stays quiet once it is."""
    monkeypatch.setenv(ra.DOOR_ENV, "10.89.79.3:8443")
    monkeypatch.setattr(ra, "_current", None)
    monkeypatch.setattr(ra, "ROUTES_PATH", _routes(tmp_path, *OFF_REMOTE))
    st = ra.status()
    assert st["door"] == "10.89.79.3:8443"
    assert st["problem"] and "not on its door's network" in st["problem"] and "10.89.79.3" in st["problem"]
    # Settings says so with remote access off as well as on.
    from server.routes import remote_access as routes
    assert "not on its door's network" in routes.summary(st)
    assert "not on its door's network" in routes.summary({**st, "on": True})
    # The default route reaches every address; it is not the door's network.
    assert ra.door_network_problem(("10.89.79.3", 8443), _routes(tmp_path, *OFF_REMOTE))

    monkeypatch.setattr(ra, "ROUTES_PATH", _routes(tmp_path, *ON_REMOTE))
    assert ra.status()["problem"] is None

    class M:
        passport = client = None
    r = ra.RemoteAccess(M(), state_path=tmp_path / "s.json")
    monkeypatch.setattr(ra, "ROUTES_PATH", _routes(tmp_path, *OFF_REMOTE))
    assert "not on its door's network" in (r.status()["problem"] or "")
    monkeypatch.setattr(ra, "ROUTES_PATH", _routes(tmp_path, *ON_REMOTE))
    assert r.status()["problem"] is None
    # No door configured: nothing to be on.
    monkeypatch.delenv(ra.DOOR_ENV)
    monkeypatch.setattr(ra, "ROUTES_PATH", _routes(tmp_path, *OFF_REMOTE))
    assert ra.status()["problem"] is None and r.status()["problem"] is None
    # Routes that cannot be read say nothing rather than raise an alarm.
    assert ra.door_network_problem(("10.89.79.3", 8443), tmp_path / "missing") is None


def _door_cert():
    import importlib.machinery
    import importlib.util
    path = str(Path(__file__).resolve().parents[1] / "sidecars/remote-door/door-cert")
    loader = importlib.machinery.SourceFileLoader("door_cert_under_test", path)
    spec = importlib.util.spec_from_loader("door_cert_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_the_certificate_job_uses_the_hosts_account_and_reports_for_settings(pki, tmp_path, monkeypatch):
    """door-cert copies the Host's ACME account into lego's --path (so lego
    registers no other account than the one the CAA names), waits quietly
    without the Host's files, and writes what Settings shows: the
    certificate's expiry and names, and the error."""
    dc = _door_cert()
    etc, state, tls, status = (tmp_path / n for n in ("etc", "state", "tls", "status"))
    directory = "https://acme-v02.api.letsencrypt.org/directory"
    acct, key = remote_acme.account_paths(etc / "acme-account", directory)
    acct.parent.mkdir(parents=True)
    key.parent.mkdir(parents=True)
    acct.write_text('{"email": "info@privatae.ai", "registration": {"body": {"status": "valid"}, '
                    '"uri": "https://acme-v02.api.letsencrypt.org/acme/acct/1000"}}')
    key.write_bytes(b"-----BEGIN EC PRIVATE KEY-----\ninvented\n-----END EC PRIVATE KEY-----\n")
    (etc / "acme-account" / "accounts" / "evil").symlink_to("/etc")
    assert dc.seed_account(str(etc), str(state)) is True
    lego = state / "lego" / "accounts" / "acme-v02.api.letsencrypt.org" / "info@privatae.ai"
    assert (lego / "account.json").read_text() == acct.read_text()
    assert stat.S_IMODE((lego / "keys" / "info@privatae.ai.key").stat().st_mode) == 0o600
    assert not (state / "lego" / "accounts" / "evil").exists()
    assert dc.ready(str(etc)) is False
    # The installed certificate, as cseries-cert leaves it.
    tls.mkdir()
    (tls / "cert.pem").write_bytes(Path(pki.site_chain).read_bytes())
    dc.write_status(False, "lego exited 1; the certificate Caddy has stays in place", 1234567890,
                    status_dir=str(status), tls=str(tls), now=1234560000)
    doc = json.loads((status / "status.json").read_text())
    assert doc["names"] == [f"*.{AID}.{ZONE}"] and doc["not_after"].endswith("Z")
    assert doc["error"].startswith("lego exited 1") and doc["ok"] is False
    monkeypatch.setenv(ra.CERT_STATUS_ENV, str(status / "status.json"))
    cert = ra.certificate_status()
    assert cert["not_after"] == doc["not_after"] and cert["error"] == doc["error"]
    from server.routes import remote_access as routes
    line = routes.summary({"on": True, "certificate": cert, "links": []})
    assert "Certificate valid until" in line and "lego exited 1" in line


def test_settings_turns_it_on_and_shows_each_link_and_turns_it_off(pki, tmp_path, monkeypatch):
    """Settings -> Remote access, as the signed-in owner on the desktop's own
    address: the Turn on button, its refusal in the door's words, the status
    with each relay's link, and Turn off. Not on any other address, and not
    without a session."""
    import httpx
    from fastapi import FastAPI
    from server.routes import remote_access as routes

    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        signed_in = {"yes": True}

        async def session(scope):
            return ("owner@example.test", "tok") if signed_in["yes"] else (None, None)
        monkeypatch.setattr(routes, "live_session", session)
        monkeypatch.setattr(ms, "_current", rig.m)
        monkeypatch.setattr(routes.owner_passkeys, "list_passkeys", lambda pool: asyncio.sleep(0, []))
        monkeypatch.setattr(routes.owner_login, "_pool", lambda: object())
        # Turn on asks for a passkey here and a fresh check with it
        # (test_turning_it_on_needs_the_check_adding_a_phone_needs).
        monkeypatch.setattr(routes.owner_passkeys, "passkey_count",
                            lambda pool, origin=None: asyncio.sleep(0, 1))
        routes.owner_passkeys._note_step_up("tok", routes.owner_passkeys.PASSKEY)
        app = FastAPI()
        app.include_router(routes.router)
        desk = home_origin.HOME.host
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=f"http://{desk}") as c:
                page = await c.get("/remote")
                assert page.status_code == 200 and 'id="remote-on"' in page.text
                assert (await c.post("/api/owner/remote/enable", headers={"host": f"home.{AID}.{ZONE}"})
                        ).status_code == 404
                signed_in["yes"] = False
                assert (await c.post("/api/owner/remote/enable")).status_code == 401
                signed_in["yes"] = True
                rig.door.entitled = False
                r = await c.post("/api/owner/remote/enable")
                assert r.status_code == 403 and r.json()["error"] == ERR_NOT_ENTITLED
                rig.door.entitled = True
                r = await c.post("/api/owner/remote/enable")
                assert r.status_code == 200 and r.json()["on"] is True, r.text
                await rig.wait_links(2)
                st = (await c.get("/api/owner/remote/status")).json()
                assert "Links to the network: 2 of 2 up" in st["summary"], st["summary"]
                assert f"https://home.{AID}.{ZONE}" in st["summary"]
                assert rig.door.password not in json.dumps(st)
                page = await c.get("/remote")
                assert 'id="remote-off"' in page.text and f"home.{AID}.{ZONE}" in page.text
                assert 'id="remote-status"' in page.text
                r = await c.post("/api/owner/remote/disable")
                assert r.status_code == 200 and r.json()["on"] is False
                assert 'id="remote-on"' in (await c.get("/remote")).text
        finally:
            await rig.close()
    run(go())


@pytest.mark.parametrize("case", ["other_address", "no_address"])
def test_a_ticket_the_door_cannot_match_is_asked_for_at_the_supervisors_pace(pki, tmp_path, monkeypatch, case):
    """remote.enable says the DNS is ready but remote.ticket answers for
    another address, or 409 (no address): the Host asks remote.enable again,
    at the supervisor's own pace. It never wakes itself into asking again at
    once, which would ask the door twice a round trip and use up the Host's
    requests on the member connection."""
    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        try:
            rig.door.dns_after = 0
            rig.remote._enable_poll = 5.0
            real = rig.door.ticket

            def ticket(serial):
                if case == "no_address":
                    return {}, ERR_NOT_ENABLED, 409
                data, err, code = real(serial)
                payload = dict(rig.door.issued[-1], address_id="k7x2m8")
                data["ticket"] = rp.sign_ticket(rig.pki.ticket_key, payload)
                return data, err, code
            rig.door.ticket = ticket
            await rig.remote.turn_on()
            await until(lambda: rig.door.ops("remote.ticket"), what="a ticket asked for")
            await asyncio.sleep(1.0)
            enables, tickets = len(rig.door.ops("remote.enable")), len(rig.door.ops("remote.ticket"))
            assert enables <= 3 and tickets <= 2, (enables, tickets)
            assert rig.remote.tunnel is None
        finally:
            await rig.close()
    run(go())


def test_turning_it_on_needs_the_check_adding_a_phone_needs(pki, tmp_path, monkeypatch):
    """Turn on puts the Host on a public address, so a live session is not
    enough: as for Add a phone, a passkey on this computer and a check with
    it (or a recovery code) in the last five minutes. Without the check the
    answer asks for one (step_up), and nothing is asked of the network.
    Turn off needs the session only."""
    import httpx
    from fastapi import FastAPI
    from server import owner_passkeys
    from server.routes import remote_access as routes

    async def go():
        rig = await Rig(pki, tmp_path, monkeypatch).start()
        passkeys = {"n": 0}

        async def session(scope):
            return "owner@example.test", "tok-turn-on"
        monkeypatch.setattr(routes, "live_session", session)
        monkeypatch.setattr(ms, "_current", rig.m)
        monkeypatch.setattr(routes.owner_login, "_pool", lambda: object())
        monkeypatch.setattr(owner_passkeys, "passkey_count", lambda pool, origin=None: asyncio.sleep(0, passkeys["n"]))
        app = FastAPI()
        app.include_router(routes.router)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=f"http://{home_origin.HOME.host}") as c:
                r = await c.post("/api/owner/remote/enable")
                assert r.status_code == 403 and "passkey" in r.json()["error"], r.text
                passkeys["n"] = 1
                r = await c.post("/api/owner/remote/enable")
                assert r.status_code == 403 and r.json().get("step_up") is True, r.text
                # A password sign-in's check is not one: only a passkey or a
                # recovery code is, as for Add a phone.
                owner_passkeys._note_step_up("tok-turn-on", owner_passkeys.PASSWORD)
                r = await c.post("/api/owner/remote/enable")
                assert r.status_code == 403 and r.json().get("step_up") is True, r.text
                assert rig.door.ops("remote.enable") == []
                assert rig.remote.state.on is False
                owner_passkeys._note_step_up("tok-turn-on", owner_passkeys.PASSKEY)
                r = await c.post("/api/owner/remote/enable")
                assert r.status_code == 200 and r.json()["on"] is True, r.text
                owner_passkeys._step_ups.pop(routes.owner_login._token_hash("tok-turn-on"), None)
                r = await c.post("/api/owner/remote/disable")
                assert r.status_code == 200 and r.json()["on"] is False, r.text
        finally:
            await rig.close()
    run(go())


@pytest.mark.parametrize("lego", ["4.29.0", "4.30.1", "5.0.0", "unreadable", "missing"])
def test_the_certificate_job_refuses_a_lego_of_another_series(tmp_path, monkeypatch, lego):
    """door-cert writes the Host's account in lego 4.29's own layout, so a
    lego of any other series (or one whose version cannot be read) is refused
    before anything is copied for it or asked of the CA, and Settings shows
    why. The 4.29 series runs."""
    etc, state = tmp_path / "etc", tmp_path / "state"
    monkeypatch.setenv("CSERIES_CERT_ETC", str(etc))
    monkeypatch.setenv("CSERIES_CERT_STATE", str(state))
    binary = tmp_path / "bin" / "lego"
    if lego != "missing":
        binary.parent.mkdir()
        out = "lego: unknown" if lego == "unreadable" else f"lego version {lego} linux/amd64"
        binary.write_text(f"#!/bin/sh\necho '{out}'\n")
        binary.chmod(0o755)
    monkeypatch.setenv("CSERIES_LEGO", str(binary))
    dc = _door_cert()
    etc.mkdir()
    for name in ("cloud.json", "acme-dns.json", "acme-dns-api.json"):
        (etc / name).write_text("{}\n")
    directory = "https://acme-v02.api.letsencrypt.org/directory"
    acct, key = remote_acme.account_paths(etc / "acme-account", directory)
    acct.parent.mkdir(parents=True)
    key.parent.mkdir(parents=True)
    acct.write_text('{"email": "info@privatae.ai", "registration": {"uri": "https://acme-v02.api.letsencrypt.org/acme/acct/1000"}}')
    key.write_bytes(b"-----BEGIN EC PRIVATE KEY-----\ninvented\n-----END EC PRIVATE KEY-----\n")
    ran = []

    class Cert:
        def main(self):
            ran.append(True)
            return 0
    ok, error = dc.run_once(Cert())
    if lego == "4.29.0":
        assert ok and error is None and ran
        assert (state / "lego" / "accounts").is_dir()
        return
    assert not ok and not ran, (ok, error)
    assert not (state / "lego").exists()
    assert "not 4.29.x" in error and "no account is written" in error, error
    if lego in ("4.30.1", "5.0.0"):
        assert f"lego is {lego.rsplit('.', 1)[0]}," in error, error
    status = tmp_path / "status"
    dc.write_status(ok, error, None, status_dir=str(status), tls=str(tmp_path / "tls"))
    monkeypatch.setenv(ra.CERT_STATUS_ENV, str(status / "status.json"))
    from server.routes import remote_access as routes
    line = routes.summary({"on": True, "certificate": ra.certificate_status(), "links": []})
    assert "Certificate problem" in line and "not 4.29.x" in line, line
