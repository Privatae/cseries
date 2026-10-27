"""Network member mode: the Host's passport and its client for the member door.

A Host joins the network with a one-time code from the owner's account page.
It makes its own key, sends a CSR to the enrolment door (enrol.privatae.ai,
public port 8643), and keeps what comes back under its state volume. From then
on it holds one mTLS connection to the member door (net.privatae.ai:8643),
trusting only the network's root certificate and checking the door's name.

These pin:

  16. the door's name is checked: a door presenting a valid chain for another
      name is refused, on the member connection and at enrolment;
  17. nothing can be pushed in: register_handler keeps nothing, and an inbound
      dispatch (or any request the door sends) is answered "unsupported" and
      never routed;
  18. the passport files are written atomically, key first and json last, and a
      half-written state reads as "not a member";
  19. a refusal naming revocation deletes the passport, and the status endpoint
      says why; the other refusal reasons are handled as agreed with the door;

  20. a frozen Host (the door saw its certificate used from two places and
      refuses it with reason "conflict"): the passport is deleted, the member
      connection is not dialled again, remote access's tunnel stops, Settings
      says what happened and what to do, and the rejoin with a new code takes
      the Host's previous host_uuid and entity id as they are;
  21. every heartbeat.ping and passport.renew carries this process's
      instance_id, kept only in memory; a certificate is renewed about daily,
      with jitter;

plus: the cloud client's TLS settings are unchanged by the new keyword
arguments, join dials the enrolment name on port 8643 with the root as its only
trust anchor, only the network's own ops ever leave the Host, the join and
leave routes are the owner's alone, and renewal swaps the key only after the
new certificate is on disk.

Every certificate here is made in the test; nothing reads a real key.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import io
import json
import logging
import os
import re
import ssl
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.bridge import bridge_client  # noqa: E402
from server.bridge import member_client as mc  # noqa: E402
from server.bridge import membership as ms  # noqa: E402
from server.bridge import passport as pp  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)

# What a Host really adds to a renewal's time; read before any test changes it.
_REAL_JITTER_S = getattr(ms, "RENEW_JITTER_S", 0.0)


@pytest.fixture(autouse=True)
def _renewals_are_due_at_renew_after(monkeypatch):
    """The renewal tests below pick their moment from the passport's renew_after.
    A real Host adds up to RENEW_JITTER_S to it (Membership.renew_due_at); the
    tests for that give their own jitter."""
    monkeypatch.setattr(ms, "RENEW_JITTER_S", 0.0, raising=False)


# ── test PKI ────────────────────────────────────────────────────────────────

def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test PKI"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _ca(cn: str, issuer=None, issuer_key=None, pathlen=None):
    key = ec.generate_private_key(ec.SECP256R1())
    b = (x509.CertificateBuilder()
         .subject_name(_name(cn))
         .issuer_name(issuer.subject if issuer is not None else _name(cn))
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(days=1))
         .not_valid_after(NOW + dt.timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=True, path_length=pathlen), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                      content_commitment=False, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False,
                                      encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    signer = issuer_key or key
    if issuer is not None:
        b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            issuer_key.public_key()), critical=False)
    return b.sign(signer, hashes.SHA256()), key


def _leaf(pub, issuer, issuer_key, *, cn, dns=(), uris=(), server=True, days=7):
    b = (x509.CertificateBuilder()
         .subject_name(_name(cn))
         .issuer_name(issuer.subject)
         .public_key(pub)
         .serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(minutes=5))
         .not_valid_after(NOW + dt.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage(
             [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),
             critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
             issuer_key.public_key()), critical=False))
    sans = [x509.DNSName(d) for d in dns] + [x509.UniformResourceIdentifier(u) for u in uris]
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


class PKI:
    """A root, an entity intermediate (signs the door's server certificates) and
    a members intermediate (signs passports), written under one directory."""

    def __init__(self, d: Path):
        self.dir = d
        d.mkdir(parents=True, exist_ok=True)
        self.root, self.root_key = _ca("Test Root")
        self.entity, self.entity_key = _ca("Test Entity Intermediate", self.root, self.root_key, 0)
        self.members, self.members_key = _ca("Test Members Intermediate", self.root, self.root_key, 0)
        self.root_path = d / "root.pem"
        self.root_path.write_bytes(_pem(self.root))
        self.servers = {}
        for name in ("net.privatae.ai", "other.privatae.ai", "enrol.privatae.ai"):
            k = ec.generate_private_key(ec.SECP256R1())
            dns = ["net.privatae.ai", "enrol.privatae.ai"] if name != "other.privatae.ai" else [name]
            c = _leaf(k.public_key(), self.entity, self.entity_key, cn=name, dns=dns)
            chain = d / f"{name}.chain.pem"
            key = d / f"{name}.key.pem"
            chain.write_bytes(_pem(c) + _pem(self.entity))
            key.write_bytes(_key_pem(k))
            self.servers[name] = (chain, key)

    def issue(self, csr_pem: str, host_uuid: str, days: int = 7):
        """What the door does with a CSR: only its public key is used."""
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        assert csr.is_signature_valid
        leaf = _leaf(csr.public_key(), self.members, self.members_key,
                     cn=f"member:{host_uuid}", server=False, days=days,
                     uris=[f"spiffe://net.privatae.ai/host/{host_uuid}",
                           "spiffe://net.privatae.ai/owner/acct-1",
                           "spiffe://net.privatae.ai/network/privatae"])
        return leaf

    def passport_answer(self, csr_pem: str, host_uuid: str, entity_id: int = 412,
                        identity: bool = True, days: int = 7) -> dict:
        leaf = self.issue(csr_pem, host_uuid, days=days)
        na = leaf.not_valid_after_utc
        data = {"cert_pem": _pem(leaf).decode(),
                "chain_pem": (_pem(leaf) + _pem(self.members)).decode(),
                "root_pem": _pem(self.root).decode(),
                "not_after": na.isoformat(),
                "renew_after": (na - dt.timedelta(days=6)).isoformat(),
                "network": "privatae", "door": "net.privatae.ai:8643"}
        if identity:
            data.update(host_uuid=host_uuid, entity_id=entity_id)
        return data


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI(tmp_path_factory.mktemp("pki"))


def _enrolled(pki: PKI, directory: Path, entity_id: int = 412, days: int = 7) -> pp.Passport:
    """A passport on disk, written the way enrolment writes one."""
    key = pp.new_key()
    host_uuid = str(uuid.uuid4())
    answer = pki.passport_answer(pp.csr_for(key), host_uuid, entity_id, days=days)
    info = pp.check_issued(answer, key, enrolment=True)
    return pp.save_enrolment(directory, key=key, cert_pem=answer["cert_pem"],
                             chain_pem=answer["chain_pem"], root_pem=pki.root_path.read_bytes(),
                             info=info)


class _Door:
    """A TLS listener on 127.0.0.1 standing in for the member or enrolment door."""

    def __init__(self, server, port):
        self.server, self.port = server, port

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


async def _door(pki: PKI, name: str, on_conn, *, client_cert: bool) -> _Door:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    chain, key = pki.servers[name]
    ctx.load_cert_chain(str(chain), str(key))
    if client_cert:
        ctx.load_verify_locations(str(pki.root_path))
        ctx.verify_mode = ssl.CERT_REQUIRED

    async def _handle(reader, writer):
        try:
            await on_conn(reader, writer)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=ctx)
    return _Door(server, server.sockets[0].getsockname()[1])


async def _wait_for(pred, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            return False
        await asyncio.sleep(0.02)
    return True


class _Writer:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


# ── 16. the door's name is checked ──────────────────────────────────────────

def test_16_a_door_with_another_name_is_refused(pki, tmp_path):
    p = _enrolled(pki, tmp_path / "passport")

    async def idle(reader, writer):
        await reader.read(1)

    async def run():
        other = await _door(pki, "other.privatae.ai", idle, client_cert=True)
        right = await _door(pki, "net.privatae.ai", idle, client_cert=True)
        try:
            bad = mc.MemberClient(p, host="127.0.0.1", port=other.port)
            ctx = bad._build_context()
            assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
            with pytest.raises(ConnectionError) as e:
                await bad._connect(ctx)
            assert "certificate verify failed" in str(e.value).lower()
            assert not bad.connected

            good = mc.MemberClient(p, host="127.0.0.1", port=right.port)
            await good._connect(good._build_context())
            assert good.connected
            await good.stop()
        finally:
            await other.close()
            await right.close()

    asyncio.run(run())


def test_16_enrolment_refuses_a_door_with_another_name(pki, tmp_path):
    seen = []

    async def answer(reader, writer):
        seen.append(await reader.readline())

    async def run():
        other = await _door(pki, "other.privatae.ai", answer, client_cert=False)
        try:
            with pytest.raises(ms.NetworkUnreachable):
                await ms.enrol("K7Q2-M9XD-4PWR", directory=tmp_path / "passport",
                               anchor=pki.root_path, host="127.0.0.1", port=other.port)
        finally:
            await other.close()

    asyncio.run(run())
    assert seen == [], "the code must not be sent to a door that did not prove its name"
    assert pp.load(tmp_path / "passport") is None


# ── 17. nothing can be pushed in ────────────────────────────────────────────

def test_17_register_handler_keeps_nothing_and_inbound_requests_are_unsupported(pki, tmp_path):
    p = _enrolled(pki, tmp_path / "passport")
    called = []

    async def run():
        c = mc.MemberClient(p)
        c.register_handler("screenshot", lambda payload: called.append(payload))
        c.register_handler("file.read", lambda payload: called.append(payload))
        assert c._handlers == {}
        assert c.status["handlers"] == []

        c._reader = asyncio.StreamReader()
        c._writer = _Writer()
        c._connected = True
        task = asyncio.create_task(c._reader_loop())
        c._reader.feed_data(json.dumps({"op": "device.dispatch", "params": {
            "action": "screenshot", "correlation_id": "c1", "payload": {}}}).encode() + b"\n")
        c._reader.feed_data(json.dumps({"op": "file.read", "id": 7,
                                        "params": {"path": "/etc/passwd"}}).encode() + b"\n")
        assert await _wait_for(lambda: c._writer.buf.count(b"\n") >= 2)
        lines = [json.loads(x) for x in c._writer.buf.splitlines()]
        assert lines[0] == {"ok": False, "error": "unsupported", "code": 400}
        assert lines[1] == {"ok": False, "error": "unsupported", "code": 400, "id": 7}

        # The base class's dispatch path is not a way in either.
        c._writer.buf = b""
        await c._handle_dispatch({"action": "screenshot", "correlation_id": "c2", "payload": {}})
        assert json.loads(c._writer.buf) == {"ok": False, "error": "unsupported", "code": 400}
        assert c._connected and not task.done()
        task.cancel()

    asyncio.run(run())
    assert called == []


# ── 18. atomic files; half-written is not a member ──────────────────────────

def test_18_files_modes_and_order(pki, tmp_path):
    d = tmp_path / "passport"
    p = _enrolled(pki, d)
    assert stat.S_IMODE(os.stat(d / "host.key").st_mode) == 0o600
    for f in ("host.crt", "chain.crt", "root.crt", "passport.json"):
        assert (d / f).is_file(), f
    got = pp.load(d)
    assert got is not None and got.entity_id == 412 and got.host_uuid == p.host_uuid
    meta = json.loads((d / "passport.json").read_text())
    assert set(meta) >= {"host_uuid", "entity_id", "network", "door", "not_after",
                         "renew_after", "enrolled_at"}
    # No temporary file is left beside them.
    assert sorted(x.name for x in d.iterdir()) == ["chain.crt", "host.crt", "host.key",
                                                   "passport.json", "root.crt"]


def test_18_a_crash_before_the_json_reads_as_not_a_member(pki, tmp_path, monkeypatch):
    d = tmp_path / "passport"
    real = pp._replace
    order = []

    def crash_at_root(src, dst):
        order.append(Path(dst).name)
        if Path(dst).name == "root.crt":
            raise OSError("power cut")
        return real(src, dst)

    monkeypatch.setattr(pp, "_replace", crash_at_root)
    with pytest.raises(OSError):
        _enrolled(pki, d)
    assert order[:3] == ["host.key", "chain.crt", "host.crt"], order
    assert pp.load(d) is None
    assert not list(d.glob(".*")), "a temporary file was left behind"


def test_18_json_without_a_cert_is_not_a_member(pki, tmp_path):
    d = tmp_path / "passport"
    _enrolled(pki, d)
    (d / "host.crt").unlink()
    (d / "chain.crt").unlink()
    assert pp.load(d) is None


def test_18_a_key_that_does_not_match_the_cert_is_not_a_member(pki, tmp_path):
    d = tmp_path / "passport"
    _enrolled(pki, d)
    (d / "host.key").write_bytes(_key_pem(ec.generate_private_key(ec.SECP256R1())))
    assert pp.load(d) is None


def test_18_a_failed_write_leaves_the_old_file_whole(pki, tmp_path, monkeypatch):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old_chain = (d / "chain.crt").read_bytes()

    def no_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(pp, "_fsync", no_fsync)
    with pytest.raises(OSError):
        pp._atomic_write(d / "chain.crt", b"-----BEGIN CERTIFICATE-----\npartial")
    assert (d / "chain.crt").read_bytes() == old_chain
    assert not list(d.glob(".*"))
    monkeypatch.undo()
    assert pp.load(d).serial == before.serial


def test_18_renewal_cut_short_rolls_forward_to_the_new_pair(pki, tmp_path):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    new_key = pp.new_key()
    pp.stage_next_key(d, new_key)
    answer = pki.passport_answer(pp.csr_for(new_key), before.host_uuid, identity=False)
    # Only the chain reached the disk before the crash.
    pp._atomic_write(d / "chain.crt", answer["chain_pem"].encode())
    after = pp.load(d)
    assert after is not None and after.serial != before.serial
    assert (d / "host.key").read_bytes() == pp.key_pem(new_key)
    assert not (d / "host.key.next").exists()
    assert (d / "host.crt").read_bytes() == answer["cert_pem"].encode()


@pytest.mark.parametrize("cut_at", ["chain.crt", "host.crt", "root.crt", "host.key",
                                    "passport.json"])
def test_18_a_renewal_cut_short_at_any_write_leaves_a_working_passport(pki, tmp_path,
                                                                        monkeypatch, cut_at):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    new_key = pp.new_key()
    pp.stage_next_key(d, new_key)
    answer = pki.passport_answer(pp.csr_for(new_key), before.host_uuid, identity=False)
    info = pp.check_issued(answer, new_key, enrolment=False)
    real = pp._replace

    def cut(src, dst):
        if Path(dst).name == cut_at:
            raise OSError("power cut")
        return real(src, dst)

    monkeypatch.setattr(pp, "_replace", cut)
    with pytest.raises(OSError):
        pp.save_renewal(d, cert_pem=answer["cert_pem"], chain_pem=answer["chain_pem"],
                        root_pem=pki.root_path.read_bytes(), info=info, previous=before)
    monkeypatch.undo()
    after = pp.load(d)
    assert after is not None, f"cut at {cut_at}: the Host lost its passport"
    new_serial = format(x509.load_pem_x509_certificate(answer["cert_pem"].encode()).serial_number,
                        "x")
    # Once the new certificate is anywhere on disk, it is the one in use.
    assert after.serial == (before.serial if cut_at == "chain.crt" else new_serial)
    assert after.host_uuid == before.host_uuid and after.entity_id == before.entity_id


# ── 19. a refusal naming revocation deletes the passport ────────────────────

def _refuse(reason: str, error: str):
    async def on_conn(reader, writer):
        writer.write(json.dumps({"ok": False, "error": error, "code": 403,
                                 "reason": reason}).encode() + b"\n")
        await writer.drain()
    return on_conn


@pytest.mark.parametrize("reason,deleted,shown", [
    ("revoked", True, "This Host was removed from the network on the account page."),
    ("conflict", True, "Another copy of this passport connected."),
    ("unknown", True, "The network does not know this Host."),
    ("membership", False, "membership not active"),
    ("mismatch", False, "The passport does not match its record."),
    ("refused", False, "Refused."),
])
def test_19_refusals(pki, tmp_path, monkeypatch, reason, deleted, shown):
    d = tmp_path / "passport"
    _enrolled(pki, d)
    error = shown if reason != "membership" else "this membership is not active"
    swaps = []

    async def run():
        door = await _door(pki, "net.privatae.ai", _refuse(reason, error), client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=door.port, set_client=swaps.append)
        monkeypatch.setattr(ms, "_current", m)
        try:
            await m.boot()
            assert isinstance(swaps[-1], mc.MemberClient)
            assert await _wait_for(lambda: m.reason == reason), m.status()
            if deleted:
                assert await _wait_for(lambda: not (d / "passport.json").exists())
            from server.routes import network_membership as routes
            st = await routes.status()
            assert st["reason"] == reason
            assert st["last_error"] == shown
            if deleted:
                assert st["member"] is False
                assert isinstance(swaps[-1], NoPlatform)
                left = sorted(x.name for x in d.iterdir())
                if reason == "conflict":
                    # A freeze keeps one file, the door's words for it, so a
                    # restarted Host still says it was stopped; nothing of
                    # the passport is in it.
                    assert left == [ms.FROZEN_FILE], left
                    assert json.loads((d / ms.FROZEN_FILE).read_text()) == {"why": error}
                else:
                    assert not left, left
            else:
                assert st["member"] is True
                assert pp.load(d) is not None
        finally:
            await m.shutdown()
            await door.close()

    asyncio.run(run())


def _accept_twice_then(after):
    """A door that answers the first connection's first two heartbeats (so the
    client has paused between beats at least once) and then does `after` on
    that connection and on every later one, with a heartbeat in flight."""
    conns = []

    async def on_conn(reader, writer):
        conns.append(1)
        if len(conns) == 1:
            for _ in range(2):
                req = json.loads(await reader.readline())
                writer.write(json.dumps({"ok": True, "data": {"ts": 1},
                                         "id": req["id"]}).encode() + b"\n")
                await writer.drain()
        await reader.readline()          # the next heartbeat, never answered
        await after(writer)

    return conns, on_conn


@pytest.mark.parametrize("reason", ["membership", "mismatch", "refused"])
def test_19_a_kept_refusal_waits_before_dialling_again(pki, tmp_path, monkeypatch, reason):
    """After a refusal that keeps the passport the client waits REFUSED_RETRY_S
    before the next dial, also when the refusal lands while a heartbeat is in
    flight on a client that has been connected before. It used to redial at
    once, hundreds of times a second, for as long as the door kept refusing."""
    from server.bridge import bridge_client as _bc
    monkeypatch.setattr(_bc, "HEARTBEAT_INTERVAL", 0.1)
    monkeypatch.setattr(mc, "REFUSED_RETRY_S", 0.5)
    d = tmp_path / "passport"
    _enrolled(pki, d)

    async def refuse(writer):
        writer.write(json.dumps({"ok": False, "error": "no", "code": 403,
                                 "reason": reason}).encode() + b"\n")
        await writer.drain()

    conns, on_conn = _accept_twice_then(refuse)

    async def run():
        door = await _door(pki, "net.privatae.ai", on_conn, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=door.port, set_client=lambda c: None)
        try:
            await m.boot()
            await asyncio.sleep(2.0)
        finally:
            await m.shutdown()
            await door.close()

    asyncio.run(run())
    # Two seconds with a half-second wait after every refusal.
    assert 2 <= len(conns) <= 5, f"{len(conns)} connections in 2 s"
    assert pp.load(d) is not None


def test_a_door_that_drops_every_connection_is_redialled_with_backoff(pki, tmp_path,
                                                                      monkeypatch):
    """A door that closes each connection without a word gets the reconnect
    backoff, not an immediate redial, once the client has been connected."""
    from server.bridge import bridge_client as _bc
    monkeypatch.setattr(_bc, "HEARTBEAT_INTERVAL", 0.1)
    d = tmp_path / "passport"
    _enrolled(pki, d)

    async def drop(writer):
        return None

    conns, on_conn = _accept_twice_then(drop)

    async def run():
        door = await _door(pki, "net.privatae.ai", on_conn, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=door.port, set_client=lambda c: None)
        try:
            await m.boot()
            await asyncio.sleep(2.0)
        finally:
            await m.shutdown()
            await door.close()

    asyncio.run(run())
    # RECONNECT_BASE is 3 s: the first connection and nothing else in 2 s.
    assert len(conns) == 1, f"{len(conns)} connections in 2 s"


def test_19_a_refusal_on_a_connection_made_with_an_older_cert_deletes_nothing(pki, tmp_path):
    d = tmp_path / "passport"
    p = _enrolled(pki, d)

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None)
        m.passport = p
        await m._on_refusal("conflict", "old serial", "deadbeef")
        assert pp.load(d) is not None and m.reason is None

    asyncio.run(run())


# ── the cloud client is unchanged ───────────────────────────────────────────

def test_cloud_bridge_client_defaults_are_unchanged(pki, tmp_path, monkeypatch):
    certs = tmp_path / "certs"
    certs.mkdir()
    k = ec.generate_private_key(ec.SECP256R1())
    (certs / "relay.crt").write_bytes(_pem(_leaf(k.public_key(), pki.entity, pki.entity_key,
                                                 cn="domain-1", server=False)))
    (certs / "relay.key").write_bytes(_key_pem(k))
    (certs / "ceigas_ca.crt").write_bytes(_pem(pki.root))
    monkeypatch.setattr(bridge_client, "CERT_DIR", certs)
    monkeypatch.setenv("HOST_IP", "2001:db8::9")

    ctx = bridge_client._build_ssl_context()
    assert ctx.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert [c["subject"] for c in ctx.get_ca_certs()] == [((("organizationName", "Test PKI"),),
                                                           (("commonName", "Test Root"),))]

    bc = bridge_client.BridgeClient()
    assert (bc.status["host"], bc.status["port"]) == (bridge_client.BRIDGE_HOST,
                                                      bridge_client.BRIDGE_PORT)
    calls, resolved = [], []

    async def fake_open(addr, port, **kw):
        calls.append((addr, port, kw))
        raise OSError("no bridge in this test")

    async def run():
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(host, port, **kw):
            resolved.append((host, port))
            return [(10, 1, 6, "", ("2001:db8::1", port, 0, 0))]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        with pytest.raises(ConnectionError):
            await bc._connect(ctx)

    asyncio.run(run())
    assert resolved == [(bridge_client.BRIDGE_HOST, bridge_client.BRIDGE_PORT)]
    assert [c[0] for c in calls] == ["2001:db8::9", "2001:db8::1"]
    for addr, port, kw in calls:
        assert port == bridge_client.BRIDGE_PORT
        assert kw["server_hostname"] == "bridge.services.ceigas.internal"
        assert kw["ssl"] is ctx
        assert kw["limit"] == 10 * 1024 * 1024


def test_bridge_client_takes_the_member_keyword_arguments(pki, tmp_path):
    p = _enrolled(pki, tmp_path / "passport")
    bc = bridge_client.BridgeClient(host="127.0.0.1", port=9, cert=p.chain_path, key=p.key_path,
                                    ca=p.root_path, server_hostname="net.privatae.ai",
                                    verify_hostname=True)
    ctx = bc._build_context()
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert (bc.status["host"], bc.status["port"]) == ("127.0.0.1", 9)
    m = mc.MemberClient(p)
    assert (m._host, m._port, m._server_hostname) == ("net.privatae.ai", 8643, "net.privatae.ai")
    assert m._verify_hostname is True and Path(m._ca) == p.root_path


def test_the_cloud_client_never_dials_without_a_bridge_host(pki, tmp_path, monkeypatch):
    """BRIDGE_HOST has no default, and with it unset the cloud client starts
    nothing, even with certificates on disk. The check reads the client's own
    host, so a client given one still dials it."""
    certs = tmp_path / "certs"
    certs.mkdir()
    k = ec.generate_private_key(ec.SECP256R1())
    (certs / "relay.crt").write_bytes(_pem(_leaf(k.public_key(), pki.entity, pki.entity_key,
                                                 cn="domain-1", server=False)))
    (certs / "relay.key").write_bytes(_key_pem(k))
    (certs / "ceigas_ca.crt").write_bytes(_pem(pki.root))
    monkeypatch.setattr(bridge_client, "CERT_DIR", certs)
    monkeypatch.setattr(bridge_client, "BRIDGE_HOST", "")
    monkeypatch.delenv("HOST_IP", raising=False)
    calls = []

    async def fake_open(addr, port, **kw):
        calls.append(addr)
        raise OSError("no bridge in this test")

    async def run():
        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        await asyncio.wait_for(bridge_client.BridgeClient().start(), timeout=2)
        assert calls == []
        named = bridge_client.BridgeClient(host="2001:db8::7")
        task = asyncio.create_task(named.start())
        try:
            assert await _wait_for(lambda: calls)
        finally:
            await named.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert calls[0] == "2001:db8::7"


# ── join dials enrol.privatae.ai:8643 ───────────────────────────────────────

class _EnrolDoor:
    """open_connection stand-in: records the dial and answers one enrol request
    the way the door does."""

    def __init__(self, pki: PKI, refuse: dict | None = None):
        self.pki, self.refuse = pki, refuse
        self.calls, self.requests = [], []
        self.host_uuid = str(uuid.uuid4())

    async def __call__(self, host, port, **kw):
        if host != ms.ENROL_HOST:
            raise OSError("no member door in this test")
        self.calls.append((host, port, kw))
        reader = asyncio.StreamReader()
        door = self

        class W(_Writer):
            def write(self, b):
                req = json.loads(b)
                door.requests.append(req)
                if door.refuse:
                    out = dict(door.refuse)
                else:
                    out = {"ok": True, "data": door.pki.passport_answer(
                        req["params"]["csr_pem"], door.host_uuid)}
                reader.feed_data(json.dumps(out).encode() + b"\n")
                reader.feed_eof()

        return reader, W()


def test_join_dials_the_enrolment_name_on_port_8643(pki, tmp_path, monkeypatch):
    door = _EnrolDoor(pki)
    monkeypatch.setattr(asyncio, "open_connection", door)
    d = tmp_path / "passport"
    swaps = []

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=swaps.append)
        try:
            st = await m.join("k7q2 m9xd-4pwr", host_name="Desk")
            assert st["member"] is True and st["entity_id"] == 412
            assert isinstance(swaps[-1], mc.MemberClient)
        finally:
            await m.shutdown()

    asyncio.run(run())
    assert len(door.calls) == 1
    host, port, kw = door.calls[0]
    assert (host, port) == ("enrol.privatae.ai", 8643)
    assert kw["server_hostname"] == "enrol.privatae.ai"
    ctx = kw["ssl"]
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert [c["subject"] for c in ctx.get_ca_certs()] == [((("organizationName", "Test PKI"),),
                                                           (("commonName", "Test Root"),))]
    req = door.requests[0]
    assert req["op"] == "passport.enrol"
    params = req["params"]
    assert params["code"] == "K7Q2M9XD4PWR"
    assert params["host_name"] == "Desk"
    assert params["client"]["product"] == "cseries"
    csr = x509.load_pem_x509_csr(params["csr_pem"].encode())
    assert len(csr.subject) == 0, "the CSR carries no name: the door names the Host"
    assert "entity_id" not in params
    p = pp.load(d)
    assert p is not None and p.host_uuid == door.host_uuid and p.entity_id == 412
    assert (d / "root.crt").read_bytes() == pki.root_path.read_bytes()


@pytest.mark.parametrize("error,code", [
    ("this code is not valid", 403),
    ("this membership already has 5 Hosts", 403),
    ("too many enrolment attempts from this address", 429),
])
def test_join_shows_the_doors_words(pki, tmp_path, monkeypatch, error, code):
    door = _EnrolDoor(pki, refuse={"ok": False, "error": error, "code": code})
    monkeypatch.setattr(asyncio, "open_connection", door)
    d = tmp_path / "passport"

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None)
        monkeypatch.setattr(ms, "_current", m)
        from fastapi import HTTPException
        from server.routes import network_membership as routes
        with pytest.raises(HTTPException) as e:
            await routes.join(routes.JoinBody(code="K7Q2-M9XD-4PWR"))
        assert e.value.detail == error
        assert e.value.status_code == code
        st = await routes.status()
        assert st["member"] is False and st["last_error"] == error

    asyncio.run(run())
    assert pp.load(d) is None


def test_join_refuses_a_malformed_code_without_dialling(pki, tmp_path, monkeypatch):
    door = _EnrolDoor(pki)
    monkeypatch.setattr(asyncio, "open_connection", door)

    async def run():
        m = ms.Membership(directory=tmp_path / "p", anchor=pki.root_path, set_client=lambda c: None)
        for bad in ("", "K7Q2-M9XD", "K7Q2-M9XD-4PWI", "K7Q2-M9XD-4PWR-0"):
            with pytest.raises(ms.BadCode):
                await m.join(bad)

    asyncio.run(run())
    assert door.calls == []


def test_join_says_so_when_the_passport_cannot_be_saved(pki, tmp_path, monkeypatch):
    """The door has used the code by the time the files are written. A disk
    that refuses them is the owner's to hear about in words, with what to do,
    and leaves no half passport behind."""
    door = _EnrolDoor(pki)
    monkeypatch.setattr(asyncio, "open_connection", door)
    d = tmp_path / "passport"
    real = pp._replace

    def full_disk(src, dst):
        if Path(dst).name == "passport.json":
            raise OSError(28, "No space left on device")
        return real(src, dst)

    monkeypatch.setattr(pp, "_replace", full_disk)

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None)
        with pytest.raises(ms.MembershipError) as e:
            await m.join("K7Q2-M9XD-4PWR")
        assert e.value.status == 500
        assert "account page" in e.value.message and "new code" in e.value.message
        assert m.status()["member"] is False and m.status()["last_error"] == e.value.message

    asyncio.run(run())
    monkeypatch.undo()
    assert pp.load(d) is None
    assert not [x.name for x in d.iterdir()], "a half-written passport was left behind"


def test_an_expired_passport_says_so_and_leave_does_not_call_it_valid(pki, tmp_path):
    d = tmp_path / "passport"
    p = _enrolled(pki, d)

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          clock=lambda: p.not_after + dt.timedelta(days=2))
        m.passport = p
        st = m.status()
        assert st["member"] is True and st["expired"] is True
        assert "expired" in (st["last_error"] or "") and "new code" in st["last_error"]
        out = await m.leave()
        assert out["member"] is False and out["revoked"] is False
        assert "stays valid" not in out["note"] and "expired" in out["note"]

        fresh = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                              clock=lambda: p.renew_after)
        fresh.passport = p
        assert fresh.status()["expired"] is False and fresh.status()["last_error"] is None

    asyncio.run(run())
    assert pp.load(d) is None


# ── only the network's own ops leave the Host ───────────────────────────────

def test_ops_the_network_does_not_offer_never_leave_the_host(pki, tmp_path):
    p = _enrolled(pki, tmp_path / "passport")

    async def run():
        c = mc.MemberClient(p)
        c._writer = _Writer()
        c._connected = True
        for op in ("memory.store", "embeddings.embed", "inference.execute", "entity.busy",
                   "synaptive.handle.register", "device.respond", "docker.build"):
            try:
                env = await asyncio.wait_for(
                    c.request_full(op, {"entity_id": 412, "text": "private"}), timeout=1)
            except asyncio.TimeoutError:
                env = {"ok": None, "error_kind": "sent and waited for an answer"}
            assert env["ok"] is False and env["error_kind"] == "infra", (op, env)
            assert c._writer.buf == b"", (op, c._writer.buf[:200])
        assert await asyncio.wait_for(c.request("memory.search", {"q": "x"}), timeout=1) is None
        assert c._writer.buf == b""
        assert {"heartbeat.ping", "passport.renew", "passport.revoke", "appstore.list",
                "community.post", "bundle.upload_part"} <= mc.NETWORK_OPS
        assert not any(op.startswith(("memory.", "device.", "docker.", "k8s.", "platform."))
                       for op in mc.NETWORK_OPS)

    asyncio.run(run())


# ── the routes are the owner's alone ────────────────────────────────────────

def _request(app_id=None):
    from starlette.requests import Request
    req = Request({"type": "http", "method": "POST", "path": "/api/network/join",
                   "headers": [(b"host", b"home.cseries.localhost")], "state": {}})
    req.state.app_token = type("T", (), {"app_id": app_id})() if app_id else None
    return req


def test_join_and_leave_are_the_owners_alone(monkeypatch):
    from fastapi import HTTPException
    from server.routes import network_membership as routes

    async def run():
        for app_id in (None, "host", "network"):
            with pytest.raises(HTTPException) as e:
                await routes.owner_only(_request(app_id))
            assert e.value.status_code == 403
        # The Settings app's token alone is not enough: the owner's session is.
        with pytest.raises(HTTPException) as e:
            await routes.owner_only(_request("settings"))
        assert e.value.status_code == 403

        async def signed_in(scope):
            return "owner@example.com", "tok"

        monkeypatch.setattr(routes, "live_session", signed_in)
        await routes.owner_only(_request("settings"))

    asyncio.run(run())
    from fastapi.routing import APIRoute
    deps = {r.path: [getattr(d.call, "_app_scope", d.call.__name__) for d in r.dependant.dependencies]
            for r in routes.router.routes if isinstance(r, APIRoute)}
    assert deps["/api/network/join"] == ["write:network_membership", "owner_only"]
    assert deps["/api/network/leave"] == ["write:network_membership", "owner_only"]
    assert deps["/api/network/status"] == ["read:network_membership"]


# ── boot ────────────────────────────────────────────────────────────────────

def test_boot_without_a_passport_is_standalone_as_before(pki, tmp_path):
    swaps = []

    async def run():
        m = ms.Membership(directory=tmp_path / "passport", anchor=pki.root_path,
                          set_client=swaps.append)
        client = await m.boot()
        assert isinstance(client, NoPlatform) and swaps == [client]
        assert m.client is None and m.status()["member"] is False
        await m.shutdown()

    asyncio.run(run())


# ── renewal ─────────────────────────────────────────────────────────────────

def test_renewal_end_to_end_reconnects_with_the_new_certificate(pki, tmp_path, monkeypatch):
    """Over real TLS: the door sees the first serial, answers passport.renew,
    and the next connection presents the new one."""
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    seen = []

    async def door(reader, writer):
        peer = writer.get_extra_info("peercert") or {}
        seen.append(str(peer.get("serialNumber", "")).lower().lstrip("0"))
        while True:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            if req["op"] == "heartbeat.ping":
                out = {"ok": True, "data": {"ts": 1, "entity_id": before.entity_id}}
            elif req["op"] == "passport.renew":
                out = {"ok": True, "data": pki.passport_answer(
                    req["params"]["csr_pem"], before.host_uuid, identity=False)}
            else:
                out = {"ok": False, "error": f"Unknown operation: {req['op']}", "code": 400}
            out["id"] = req.get("id")
            writer.write(json.dumps(out).encode() + b"\n")
            await writer.drain()

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(hours=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: m.client.connected and len(seen) == 1)
            m._clock = lambda: before.renew_after + dt.timedelta(minutes=1)
            assert await m.renew_if_due() == "renewed"
            assert await _wait_for(lambda: len(seen) == 2 and m.client.connected)
            return m.passport
        finally:
            await m.shutdown()
            await srv.close()

    after = asyncio.run(run())
    assert seen == [before.serial, after.serial] and after.serial != before.serial


class _StubClient:
    connected = True

    def __init__(self, answer):
        self.answer = answer
        self.sent = []
        self.reconnected = 0

    async def request_full(self, op, params=None):
        self.sent.append((op, params))
        return self.answer(op, params)

    async def reconnect(self):
        self.reconnected += 1

    async def stop(self):
        return None


def test_renewal_swaps_the_key_after_the_new_cert_is_written(pki, tmp_path):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old_key = (d / "host.key").read_bytes()

    def door(op, params):
        assert op == "passport.renew"
        # The new key is on disk, beside the old one, before the door answers.
        assert (d / "host.key.next").is_file() and (d / "host.key").read_bytes() == old_key
        return {"ok": True, "data": pki.passport_answer(params["csr_pem"], before.host_uuid,
                                                        identity=False)}

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(minutes=1))
        m.passport = before
        m.client = _StubClient(door)
        assert await m.renew_if_due() == "not due"
        assert m.client.sent == []
        m._clock = lambda: before.renew_after + dt.timedelta(minutes=1)
        assert await m.renew_if_due() == "renewed"
        assert m.client.reconnected == 1
        return m

    m = asyncio.run(run())
    after = pp.load(d)
    assert after.serial != before.serial and m.passport.serial == after.serial
    assert (d / "host.key").read_bytes() != old_key
    assert not (d / "host.key.next").exists()
    assert after.entity_id == before.entity_id and after.host_uuid == before.host_uuid
    assert after.renew_after == after.not_after - dt.timedelta(days=6)


def test_renewal_too_early_changes_nothing(pki, tmp_path):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old = {f: (d / f).read_bytes() for f in ("host.key", "host.crt", "chain.crt", "passport.json")}

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          clock=lambda: before.renew_after + dt.timedelta(minutes=1))
        m.passport = before
        m.client = _StubClient(lambda op, params: {"ok": False, "error": "too early to renew",
                                                   "code": 429})
        assert await m.renew_if_due() == "too early"
        assert m.client.reconnected == 0

    asyncio.run(run())
    assert {f: (d / f).read_bytes() for f in old} == old
    assert not (d / "host.key.next").exists()


def test_a_cert_for_another_key_is_never_written(pki, tmp_path):
    key = pp.new_key()
    other = pp.new_key()
    answer = pki.passport_answer(pp.csr_for(other), str(uuid.uuid4()))
    with pytest.raises(pp.PassportError):
        pp.check_issued(answer, key, enrolment=True)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert rsa_key  # the Host never makes one: new_key is EC P-256
    assert isinstance(pp.new_key(), ec.EllipticCurvePrivateKey)


# ── C8: a renewal waits as pending until the new passport connects ──────────

PASSPORT_FILES = ("host.key", "host.crt", "chain.crt", "root.crt", "passport.json")


def _files(d: Path) -> dict:
    return {f: (d / f).read_bytes() for f in PASSPORT_FILES}


def _serial_of(cert_pem: str) -> str:
    return format(x509.load_pem_x509_certificate(cert_pem.encode()).serial_number, "x")


def _conflict_frame() -> bytes:
    return json.dumps({"ok": False, "error": "another copy of this passport connected",
                       "code": 403, "reason": "conflict"}).encode() + b"\n"


class _C8Door:
    """A member door that keeps contract C8's serial rules.

    passport.renew is taken only from the current serial; it signs a
    certificate for the new key and keeps it as pending (a later renewal
    replaces it), and leaves current, previous and the rotation time alone.
    At connect: current is accepted; pending is promoted (previous = current,
    current = pending, rotated now) and accepted; previous is accepted for
    120 s after a rotation; anything else gets the C4 conflict frame and is
    stamped. `lose` says, one renewal at a time, what becomes of the answer:
    None sends it, "timeout" never sends it, "drop" closes the connection
    instead."""

    OVERLAP_S = 120.0

    def __init__(self, pki: PKI, passport: pp.Passport, lose=()):
        self.pki, self.host_uuid = pki, passport.host_uuid
        self.current, self.previous, self.pending = passport.serial, None, None
        self.rotated_at = None
        self.lose = list(lose)
        self.seen, self.renewed, self.conflicts = [], [], []
        self.on_accept = None

    def _admit(self, serial: str) -> bool:
        now = time.monotonic()
        if serial == self.current:
            return True
        if serial and serial == self.pending:
            self.previous, self.current, self.pending = self.current, serial, None
            self.rotated_at = now
            return True
        if (serial and serial == self.previous and self.rotated_at is not None
                and now - self.rotated_at < self.OVERLAP_S):
            return True
        self.conflicts.append(serial)
        return False

    async def __call__(self, reader, writer):
        peer = writer.get_extra_info("peercert") or {}
        serial = str(peer.get("serialNumber", "")).lower().lstrip("0")
        if not self._admit(serial):
            writer.write(_conflict_frame())
            await writer.drain()
            return
        self.seen.append(serial)
        if self.on_accept is not None:
            self.on_accept(serial)
        while True:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            op = req.get("op")
            if op == "heartbeat.ping":
                out = {"ok": True, "data": {"ts": 1}}
            elif op == "passport.renew":
                if serial != self.current:
                    writer.write(_conflict_frame())
                    await writer.drain()
                    return
                data = self.pki.passport_answer(req["params"]["csr_pem"], self.host_uuid,
                                                identity=False)
                self.pending = _serial_of(data["cert_pem"])
                self.renewed.append(self.pending)
                fate = self.lose.pop(0) if self.lose else None
                if fate == "timeout":
                    continue
                if fate == "drop":
                    return
                out = {"ok": True, "data": data}
            else:
                out = {"ok": False, "error": f"Unknown operation: {op}", "code": 400}
            out["id"] = req.get("id")
            writer.write(json.dumps(out).encode() + b"\n")
            await writer.drain()


@pytest.mark.parametrize("lost", ["timeout", "drop"])
def test_c8_a_lost_renew_answer_keeps_the_passport_and_the_next_check_renews_again(
        pki, tmp_path, monkeypatch, lost):
    """The door keeps a renewal as pending, so when its answer never arrives
    (the request times out, or the connection drops) the Host's passport is
    still the current one there. The files stay byte for byte as they were,
    the staged key goes, the Host stays connected or comes back with the same
    passport, and the next check renews again. The pending certificate whose
    answer was lost is replaced and never connects."""
    monkeypatch.setattr(bridge_client, "REQUEST_TIMEOUT", 0.5)
    monkeypatch.setattr(bridge_client, "RECONNECT_BASE", 0.1)
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old = _files(d)
    door = _C8Door(pki, before, lose=[lost])

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(hours=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: m.client.connected and door.seen == [before.serial])
            m._clock = lambda: before.renew_after + dt.timedelta(minutes=1)

            assert await m.renew_if_due() == "no answer"
            lost_pending = door.pending
            assert lost_pending and door.current == before.serial
            assert _files(d) == old
            assert not (d / "host.key.next").exists()
            assert m.passport.serial == before.serial
            st = m.status()
            assert st["member"] is True and st["reason"] is None and st["last_error"] is None
            assert "keeps its passport" in st["renew_note"], st["renew_note"]

            dials = 1 if lost == "timeout" else 2
            assert await _wait_for(lambda: m.client.connected and len(door.seen) == dials), door.seen
            assert door.seen == [before.serial] * dials

            assert await m.renew_if_due() == "renewed"
            after = m.passport
            assert await _wait_for(lambda: m.client.connected and door.seen[-1] == after.serial)
            assert door.renewed == [lost_pending, after.serial]
            assert door.current == after.serial and door.previous == before.serial
            assert door.pending is None and door.conflicts == []
            assert m.status()["renew_note"] is None
            return after
        finally:
            await m.shutdown()
            await srv.close()

    after = asyncio.run(run())
    assert pp.load(d).serial == after.serial != before.serial


@pytest.mark.parametrize("answer", [
    {"ok": False, "error": "timeout", "error_kind": "infra"},
    {"ok": False, "error": "member door connection closed", "error_kind": "infra"},
    {"ok": False, "error": "too early to renew", "code": 429},
])
def test_c8_after_a_lost_answer_or_a_429_the_next_try_is_the_hourly_check(
        pki, tmp_path, monkeypatch, answer):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old = _files(d)
    waits = []

    class _Stop(Exception):
        pass

    async def sleep(seconds):
        waits.append(seconds)
        raise _Stop

    monkeypatch.setattr(ms, "_sleep", sleep)

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          clock=lambda: before.renew_after + dt.timedelta(minutes=1))
        m.passport = before
        m.client = _StubClient(lambda op, params: dict(answer))
        with pytest.raises(_Stop):
            await m._renew_loop()
        return m

    m = asyncio.run(run())
    assert waits == [ms.RENEW_CHECK_S]
    assert [op for op, _ in m.client.sent] == ["passport.renew"]
    assert m.client.reconnected == 0 and m.passport is before
    assert _files(d) == old and not (d / "host.key.next").exists()


def test_c8_a_refusal_frame_without_an_id_fails_every_pending_request_as_a_refusal(
        pki, tmp_path):
    """A C4 frame carries no id: when the door refuses while requests are in
    flight it sends that one line and closes. Every pending request gets the
    refusal (ok false, the door's words, 403, the reason) instead of a bare
    "connection closed", so a renewal in flight reads as refused and the
    refusal alone decides what happens to the passport."""
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    text = "another copy of this passport connected"
    got = []

    async def door(reader, writer):
        req = json.loads(await reader.readline())
        writer.write(json.dumps({"ok": True, "data": {"ts": 1},
                                 "id": req["id"]}).encode() + b"\n")
        await writer.drain()
        while len(got) < 2:
            got.append(json.loads(await reader.readline())["op"])
        writer.write(json.dumps({"ok": False, "error": text, "code": 403,
                                 "reason": "conflict"}).encode() + b"\n")
        await writer.drain()

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(hours=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: m.client.connected and m.client._last_heartbeat)
            m._clock = lambda: before.renew_after + dt.timedelta(minutes=1)
            listing = asyncio.create_task(m.client.request_full("appstore.list", {}))
            outcome = await asyncio.wait_for(m.renew_if_due(), timeout=5)
            env = await asyncio.wait_for(listing, timeout=5)
            assert sorted(got) == ["appstore.list", "passport.renew"]
            assert outcome == "refused"
            assert {k: env.get(k) for k in ("ok", "error", "code", "reason")} == {
                "ok": False, "error": text, "code": 403, "reason": "conflict"}, env
            assert not (d / "host.key.next").exists()
            # The refusal, not the renewal, decides: conflict deletes the passport.
            assert await _wait_for(lambda: m.reason == "conflict"
                                   and not (d / "passport.json").exists())
        finally:
            await m.shutdown()
            await srv.close()

    asyncio.run(run())


def test_c8_boot_drops_a_key_staged_for_a_renewal_whose_answer_never_reached_the_disk(
        pki, tmp_path):
    """The Host stopped after sending passport.renew and before writing any
    answer: host.key.next is all that is left of it. The passport on disk is
    still current at the door, so boot drops the staged key, changes nothing
    else, and connects with the passport it has."""
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old = _files(d)
    pp.stage_next_key(d, pp.new_key())
    door = _C8Door(pki, before)

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(hours=1))
        try:
            await m.boot()
            assert not (d / "host.key.next").exists()
            assert _files(d) == old and m.passport.serial == before.serial
            assert await _wait_for(lambda: m.client.connected and door.seen == [before.serial])
            assert door.conflicts == []
        finally:
            await m.shutdown()
            await srv.close()

    asyncio.run(run())


def test_c8_an_answered_renew_is_written_before_the_next_dial_which_promotes_it(pki, tmp_path):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    old_key = (d / "host.key").read_bytes()
    door = _C8Door(pki, before)
    on_disk = []
    door.on_accept = lambda serial: on_disk.append(_serial_of((d / "host.crt").read_text()))

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(hours=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: m.client.connected and door.seen == [before.serial])
            m._clock = lambda: before.renew_after + dt.timedelta(minutes=1)
            assert await m.renew_if_due() == "renewed"
            after = m.passport
            assert after.serial != before.serial
            assert (d / "host.key").read_bytes() != old_key
            assert not (d / "host.key.next").exists()
            assert json.loads((d / "passport.json").read_text())["not_after"] == \
                after.not_after.isoformat()
            assert await _wait_for(lambda: m.client.connected and len(door.seen) == 2)
            assert door.seen == [before.serial, after.serial]
            # Each dial presented the certificate that was on disk when it was made.
            assert on_disk == door.seen
            assert door.current == after.serial and door.previous == before.serial
            assert door.pending is None and door.conflicts == []
        finally:
            await m.shutdown()
            await srv.close()

    asyncio.run(run())


def test_c8_a_full_disk_mid_renewal_then_a_lost_answer_never_costs_the_passport(
        pki, tmp_path, monkeypatch):
    """The disk fills while an answered renewal is written: chain.crt (what
    the Host presents) is new, host.crt cannot be written, and the new key is
    still staged as host.key.next. That staged key is then the only key for
    the certificate on disk. The Host goes on with the new pair (the door
    holds it as pending, and the next connection promotes it), and a later
    renewal whose answer is lost cannot take the key away and leave a
    certificate with no key, which reads as not a member."""
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    real_write = pp._atomic_write
    disk_full = {"on": True}

    def write(path, data, mode=0o644):
        if disk_full["on"] and Path(path).name == "host.crt":
            raise OSError(28, "No space left on device")
        return real_write(path, data, mode)

    monkeypatch.setattr(pp, "_atomic_write", write)
    lost = []

    def door(op, params):
        assert op == "passport.renew"
        if lost:
            return lost.pop(0)
        return {"ok": True, "data": pki.passport_answer(params["csr_pem"], before.host_uuid,
                                                        identity=False)}

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          clock=lambda: before.renew_after + dt.timedelta(minutes=1))
        m.passport = before
        m.client = _StubClient(door)
        first = await m.renew_if_due()
        disk_full["on"] = False
        lost.append({"ok": False, "error": "timeout", "error_kind": "infra"})
        second = await m.renew_if_due()
        return m, first, second

    m, first, second = asyncio.run(run())
    on_disk = pp.load(d)
    assert on_disk is not None, f"the Host lost its passport ({first}, then {second})"
    assert first == "renewed" and m.client.reconnected == 1
    assert on_disk.serial == m.passport.serial != before.serial
    assert second == "no answer" and not (d / "host.key.next").exists()


def test_a_staged_key_the_certificate_on_disk_needs_is_never_replaced(pki, tmp_path,
                                                                      monkeypatch):
    """A renewal's chain reached the disk but the key swap could not be made
    (load cannot finish it either): host.key.next is the only key for the
    certificate the Host presents. A new renewal must not stage over it; the
    next check finishes the swap once the disk allows it, and sends nothing
    until then."""
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    staged = pp.new_key()
    pp.stage_next_key(d, staged)
    answer = pki.passport_answer(pp.csr_for(staged), before.host_uuid, identity=False)
    pp._atomic_write(d / "chain.crt", answer["chain_pem"].encode())
    with pytest.raises(pp.PassportError):
        pp.stage_next_key(d, pp.new_key())
    assert (d / "host.key.next").read_bytes() == pp.key_pem(staged)

    real = pp._replace
    stuck = {"on": True}

    def replace(src, dst):
        if stuck["on"] and Path(dst).name == "host.key":
            raise OSError(5, "Input/output error")
        return real(src, dst)

    monkeypatch.setattr(pp, "_replace", replace)

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          clock=lambda: before.renew_after + dt.timedelta(minutes=1))
        m.passport = before
        m.client = _StubClient(lambda op, params: pytest.fail(f"{op} was sent"))
        assert await m.renew_if_due() == "failed"
        assert (d / "host.key.next").read_bytes() == pp.key_pem(staged)
        stuck["on"] = False
        assert await m.renew_if_due() == "renewed"
        assert m.client.sent == [] and m.client.reconnected == 1
        return m

    m = asyncio.run(run())
    assert m.passport.serial == _serial_of(answer["cert_pem"]) == pp.load(d).serial
    assert (d / "host.key").read_bytes() == pp.key_pem(staged)


# ── the op table matches the door's ─────────────────────────────────────────

def test_network_ops_are_the_doors_phase_1_table(pki, tmp_path):
    """The member door's MEMBER_OPS for phase 1 plus its three own ops, its
    two remote-access ops (privatae bridge/member_remote.py REMOTE_OPS) and
    the backup ops. Three App Store ops the door leaves out are answered here
    without a byte sent: appstore.yank_version (until the store checks that
    the caller published the app) and appstore.checkout.create and
    appstore.refund (the door holds no Stripe key in phase 1); so is
    backup.recovery_key, until the door answers it."""
    assert mc.NETWORK_OPS == frozenset({
        "heartbeat.ping", "passport.renew", "passport.revoke",
        "remote.enable", "remote.ticket",
        "appstore.list", "appstore.icon", "appstore.fetch_bundle", "appstore.purchase",
        "appstore.refund.request", "appstore.entitlement.check", "appstore.agreement",
        "appstore.publisher.my_apps", "appstore.publisher.sales",
        "appstore.publisher.set_price", "appstore.publisher.set_refund_policy",
        "appstore.publisher.refund_requests", "appstore.publish_bundle",
        "appstore.publish_requests", "appstore.circle_roster", "bundle.upload_part",
        "community.categories", "community.threads", "community.thread", "community.post",
        "community.vote", "community.flag", "community.search", "community.digest",
        "community.set_proposal_ref", "community.group.join",
        "rendezvous.list", "rendezvous.withdraw",
        "backup.upload.start", "backup.upload.part", "backup.upload.complete",
        "backup.upload.abort", "backup.put_url", "backup.list", "backup.get_url",
        "backup.drop", "backup.report",
    })
    p = _enrolled(pki, tmp_path / "passport")

    async def run():
        c = mc.MemberClient(p)
        c._writer = _Writer()
        c._connected = True
        for op in ("appstore.yank_version", "appstore.checkout.create", "appstore.refund",
                   "backup.recovery_key"):
            env = await asyncio.wait_for(c.request_full(op, {"app_id": "x"}), timeout=1)
            assert env["ok"] is False and env["error_kind"] == "infra", (op, env)
        assert c._writer.buf == b""

    asyncio.run(run())


def test_backup_ops_go_to_the_door(pki, tmp_path):
    """A member Host's backups travel through the door: each backup op the
    Host's backup code sends is written to the connection, not answered
    "not offered" here."""
    p = _enrolled(pki, tmp_path / "passport")
    ops = ("backup.upload.start", "backup.upload.part", "backup.upload.complete",
           "backup.upload.abort", "backup.put_url", "backup.list", "backup.get_url",
           "backup.drop", "backup.report")

    async def run():
        c = mc.MemberClient(p)
        c._writer = _Writer()
        c._connected = True
        for op in ops:
            c._writer.buf = b""
            try:
                await asyncio.wait_for(c.request_full(op, {"key": "k"}), timeout=0.3)
            except asyncio.TimeoutError:
                pass      # sent, and nothing answers in this test
            sent = [json.loads(x) for x in c._writer.buf.splitlines() if x.strip()]
            assert any(f.get("op") == op for f in sent), (op, c._writer.buf[:200])

    asyncio.run(run())


# ── where the owner gets a code ─────────────────────────────────────────────

ACCOUNT_NETWORK_URL = "https://privatae.ai/account/network"


def test_settings_network_names_the_account_page_where_codes_are_made(pki, tmp_path):
    """The account site serves the page that makes codes and lists Hosts at
    /account/network (its /network only redirects to the homepage), so every
    place Settings -> Network sends the owner names that address."""
    app_dir = Path(ROOT) / "system-apps" / "settings"
    html = (app_dir / "index.html").read_text()
    section = html[html.index('id="view-network"'):]
    section = section[:section.index("</section>")]
    assert f'href="{ACCOUNT_NETWORK_URL}"' in section
    assert 'target="_blank"' in section and 'rel="noopener noreferrer"' in section
    js = (app_dir / "app.js").read_text()
    prompt = js[js.index("function joinNetwork"):]
    prompt = prompt[:prompt.index("netSay(", prompt.index("if (!code)")) + 200]
    assert ACCOUNT_NETWORK_URL in prompt
    for text in (section, js, (app_dir / "app.yaml").read_text()):
        assert "privatae.ai/network" not in text

    assert ms.ACCOUNT_NETWORK_URL == ACCOUNT_NETWORK_URL
    d = tmp_path / "passport"
    p = _enrolled(pki, d)
    m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                      clock=lambda: p.not_after + dt.timedelta(hours=1))
    m.passport = p
    assert ACCOUNT_NETWORK_URL in m.status()["last_error"]


# ── Settings -> Network in a real browser ───────────────────────────────────

def test_settings_network_tab_in_a_browser(pki, tmp_path, monkeypatch):
    """The Settings page in headless Chromium over the real routes and the real
    Membership, with the enrolment door and the member door as TLS listeners on
    loopback: a refused code shows the door's words as text, a good code makes
    the Host a member and connects it, Leave revokes and deletes the passport.
    Only the owner's session is stood in for (owner_only is pinned above)."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not available")
    import contextlib
    import threading
    import socket

    import uvicorn
    import yaml
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    from server.applications.tokens import AppToken
    from server.routes import network_membership as routes

    app_dir = Path(ROOT) / "system-apps" / "settings"
    css_dir = Path(ROOT) / "dashboard" / "base" / "css"
    perms = list(yaml.safe_load((app_dir / "app.yaml").read_text())["permissions"])
    assert {"read:network_membership", "write:network_membership"} <= set(perms)
    d = tmp_path / "passport"
    seen = {"enrol": [], "door": []}
    host_uuid = str(uuid.uuid4())
    monkeypatch.setattr(ms, "_current", None)

    async def enrol_door(reader, writer):
        req = json.loads(await reader.readline())
        seen["enrol"].append(req["params"]["code"])
        code = req["params"]["code"]
        if code == "ZZZZZZZZZZZZ":
            out = {"ok": False, "error": "this code is not valid", "code": 403}
        elif code == "YYYYYYYYYYYY":
            out = {"ok": False, "error": "<img src=x onerror=window.__pwned=1>", "code": 403}
        else:
            out = {"ok": True, "data": pki.passport_answer(req["params"]["csr_pem"], host_uuid)}
        writer.write(json.dumps(out).encode() + b"\n")
        await writer.drain()

    async def member_door(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            seen["door"].append(req["op"])
            if req["op"] in ("heartbeat.ping", "passport.revoke"):
                out = {"ok": True, "data": {"ts": 1, "entity_id": 412}}
            else:
                out = {"ok": False, "error": f"Unknown operation: {req['op']}", "code": 400}
            out["id"] = req.get("id")
            writer.write(json.dumps(out).encode() + b"\n")
            await writer.drain()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        enrol = await _door(pki, "enrol.privatae.ai", enrol_door, client_cert=False)
        door = await _door(pki, "net.privatae.ai", member_door, client_cert=True)
        ms._current = ms.Membership(directory=d, anchor=pki.root_path,
                                    enrol_host="127.0.0.1", enrol_port=enrol.port,
                                    door_host="127.0.0.1", door_port=door.port)
        yield
        await ms._current.shutdown()
        await enrol.close()
        await door.close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def window_token(request: Request, call_next):
        # What the kernel's token middleware leaves for a Settings window.
        if request.url.path.startswith("/api/"):
            request.state.app_token = AppToken(app_id="settings", permissions=perms,
                                               window_id="test", iat=0)
        return await call_next(request)

    async def _owner_signed_in():
        return None

    app.dependency_overrides[routes.owner_only] = _owner_signed_in
    app.include_router(routes.router)

    @app.api_route("/api/{rest:path}", methods=["GET", "PUT", "POST", "DELETE"])
    def elsewhere(rest: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.get("/css/{name}")
    def css(name: str):
        target = (css_dir / name).resolve()
        if target.parent != css_dir.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/css")

    @app.get("/system-apps/settings/{rest:path}")
    def static(rest: str):
        target = (app_dir / (rest or "index.html")).resolve()
        if app_dir.resolve() not in target.parents:
            return Response(status_code=403)
        return FileResponse(str(target)) if target.is_file() else Response(status_code=404)

    # Loopback on purpose: the only client is the Chromium this test launches.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(300):
            if server.started:
                break
            import time as _t
            _t.sleep(0.05)
        assert server.started, "the test server did not start"
        expect = sync_api.expect
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"chromium unavailable: {e}")
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/system-apps/settings/index.html#view=network",
                          wait_until="domcontentloaded", timeout=15_000)
                state = page.locator("#net-state")
                expect(state).to_contain_text("not a member", timeout=10_000)
                expect(page.locator("#net-join")).to_be_visible()
                expect(page.locator("#net-leave-row")).to_be_hidden()
                # Where the code comes from, as a link that opens the account page.
                link = page.locator(f'#net-join a[href="{ACCOUNT_NETWORK_URL}"]')
                expect(link).to_be_visible()
                expect(link).to_have_attribute("target", "_blank")
                expect(link).to_have_text(ACCOUNT_NETWORK_URL)

                # The door's refusal, word for word.
                page.fill("#net-code", "zzzz-zzzz-zzzz")
                page.click("#net-join-btn")
                expect(page.locator("#net-message")).to_have_text("this code is not valid",
                                                                  timeout=10_000)
                # Markup in an answer is text.
                page.fill("#net-code", "YYYY-YYYY-YYYY")
                page.click("#net-join-btn")
                expect(page.locator("#net-message")).to_have_text(
                    "<img src=x onerror=window.__pwned=1>", timeout=10_000)
                assert page.evaluate("window.__pwned") is None
                expect(state).to_contain_text("not a member")
                assert pp.load(d) is None

                # A good code.
                page.fill("#net-code", "K7Q2-M9XD-4PWR")
                page.press("#net-code", "Enter")
                expect(state).to_contain_text("is a member", timeout=15_000)
                expect(state).to_contain_text("412")
                expect(state).to_contain_text("privatae")
                expect(page.locator("#net-join")).to_be_hidden()
                expect(page.locator("#net-leave-row")).to_be_visible()
                assert pp.load(d) is not None
                assert seen["enrol"] == ["ZZZZZZZZZZZZ", "YYYYYYYYYYYY", "K7Q2M9XD4PWR"]

                connection = state.locator(".about-row", has=page.locator(
                    ".about-row-label", has_text="Connection")).locator(".about-row-value")
                for _ in range(50):
                    page.click('.cseries-nav-btn[data-view="network"]')
                    if connection.inner_text() == "Connected":
                        break
                    page.wait_for_timeout(200)
                assert connection.inner_text() == "Connected"

                # Leave: revoked at the door, files gone.
                page.once("dialog", lambda dlg: dlg.accept())
                page.click("#net-leave-btn")
                expect(page.locator("#net-message")).to_have_text("This Host has left the network.",
                                                                  timeout=10_000)
                expect(state).to_contain_text("not a member")
                assert "passport.revoke" in seen["door"]
                assert pp.load(d) is None and not (d / "passport.json").exists()
                assert errors == [], errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=15)


# ── 21. instance_id: one per Host process, on heartbeat.ping and passport.renew ──

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def test_21_instance_id_is_32_lowercase_hex_and_the_same_for_the_whole_process(pki, tmp_path):
    first = mc.instance_id()
    assert _HEX32.match(first), first
    assert mc.instance_id() == first
    # Every client in the process names the same Host process.
    p = _enrolled(pki, tmp_path / "passport")
    assert mc.MemberClient(p).__class__ is mc.MemberClient and mc.instance_id() == first


def test_21_instance_id_is_new_after_a_restart():
    """A restart is a new process: run two, each importing the module fresh."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from server.bridge import member_client as m; print(m.instance_id())")
    ids = []
    for _ in range(2):
        out = subprocess.run([sys.executable, "-I", "-c", code, ROOT], capture_output=True,
                             text=True, timeout=120, check=True)
        ids.append(out.stdout.strip())
    assert all(_HEX32.match(i) for i in ids), ids
    assert ids[0] != ids[1], "a restarted Host must not repeat its instance id"


def test_21_only_heartbeat_ping_and_passport_renew_carry_it(pki, tmp_path, monkeypatch):
    sent = []

    async def record(self, op, params=None):
        sent.append((op, params))
        return {"ok": True, "data": {}}

    monkeypatch.setattr(bridge_client.BridgeClient, "request_full", record)
    p = _enrolled(pki, tmp_path / "passport")
    iid = mc.instance_id()

    async def run():
        c = mc.MemberClient(p)
        await c.request_full("heartbeat.ping", {})
        await c.request_full("heartbeat.ping", None)
        await c.request_full("passport.renew", {"csr_pem": "CSR"})
        # A caller cannot choose it.
        await c.request_full("passport.renew", {"csr_pem": "CSR", "instance_id": "f" * 32})
        await c.request_full("appstore.list", {"q": 1})
        await c.request_full("passport.revoke", {})
        # An op the network does not offer is answered here and never sent.
        assert (await c.request_full("memory.search", {}))["ok"] is False

    asyncio.run(run())
    assert sent == [("heartbeat.ping", {"instance_id": iid}),
                    ("heartbeat.ping", {"instance_id": iid}),
                    ("passport.renew", {"csr_pem": "CSR", "instance_id": iid}),
                    ("passport.renew", {"csr_pem": "CSR", "instance_id": iid}),
                    ("appstore.list", {"q": 1}),
                    ("passport.revoke", {})]


def test_21_the_same_instance_id_is_on_the_wire_before_and_after_a_renewal(pki, tmp_path,
                                                                           monkeypatch):
    """Over real TLS: the heartbeats on the first connection, the renewal, and
    the heartbeats on the connection made with the new certificate all carry
    one id."""
    monkeypatch.setattr(bridge_client, "HEARTBEAT_INTERVAL", 0.1)
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    got, conns = [], []

    async def door(reader, writer):
        conns.append(1)
        n = len(conns)
        while True:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            got.append((n, req["op"], req.get("params")))
            if req["op"] == "passport.renew":
                out = {"ok": True, "data": pki.passport_answer(
                    req["params"]["csr_pem"], before.host_uuid, identity=False)}
            else:
                out = {"ok": True, "data": {"ts": 1}}
            out["id"] = req.get("id")
            writer.write(json.dumps(out).encode() + b"\n")
            await writer.drain()

    def beats(n):
        return [p for c, op, p in got if c == n and op == "heartbeat.ping"]

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after - dt.timedelta(hours=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: len(beats(1)) >= 2)
            m._clock = lambda: before.renew_after + dt.timedelta(minutes=1)
            assert await m.renew_if_due() == "renewed"
            assert await _wait_for(lambda: len(beats(2)) >= 2)
        finally:
            await m.shutdown()
            await srv.close()

    asyncio.run(run())
    iid = mc.instance_id()
    renews = [p for _, op, p in got if op == "passport.renew"]
    assert len(renews) == 1 and renews[0]["instance_id"] == iid and "csr_pem" in renews[0]
    assert all(p == {"instance_id": iid} for n in (1, 2) for p in beats(n)), got


def test_21_instance_id_is_not_on_disk_in_logs_in_status_or_in_a_backup(pki, tmp_path, caplog):
    """A whole session (join's files, heartbeats, a renewal, a refusal, leave)
    against real TLS doors, with every log level on: the id is nowhere that
    outlives the process or leaves the Host."""
    from server import host_backup
    caplog.set_level(logging.DEBUG)
    iid = mc.instance_id().encode()
    apps_root = tmp_path / "var-lib-cseries"          # stands for the apps volume
    d = apps_root / "passport"
    before = _enrolled(pki, d)
    seen_ops = []

    async def door(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            seen_ops.append(req["op"])
            if req["op"] == "passport.renew":
                out = {"ok": True, "data": pki.passport_answer(
                    req["params"]["csr_pem"], before.host_uuid, identity=False)}
            else:
                out = {"ok": True, "data": {"ts": 1}}
            out["id"] = req.get("id")
            writer.write(json.dumps(out).encode() + b"\n")
            await writer.drain()

    shown = []

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after + dt.timedelta(minutes=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: m.client.connected and "heartbeat.ping" in seen_ops)
            assert await m.renew_if_due() == "renewed"
            assert await _wait_for(lambda: m.client.connected)
            shown.append(json.dumps(m.status()))
            shown.append(json.dumps(m.client.status, default=str))
            shown.append(json.dumps(m.remote.status(), default=str))
            # A refusal on the connection, then the owner leaving.
            await m._on_refusal("membership", "membership not active", m.passport.serial)
            shown.append(json.dumps(m.status()))
            assert (await m.leave())["member"] is False
        finally:
            await m.shutdown()
            await srv.close()

    asyncio.run(run())
    assert "passport.renew" in seen_ops and "passport.revoke" in seen_ops

    # Not in anything written under the Host's volume.
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert iid not in f.read_bytes(), f"the instance id is on disk in {f}"
    # Not in a log line, at any level.
    logged = caplog.text + "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert caplog.records and iid.decode() not in logged
    # Not in what Settings reads.
    assert all(iid.decode() not in text for text in shown), shown
    # Not in a backup of the volume: the backup writer's own walk of it, with the
    # Host's passport files in it (leave removed them, so make them again).
    _enrolled(pki, d)
    (apps_root / "remote-access.json").write_text('{"on": false}')
    buf, files = io.BytesIO(), []
    with tarfile.open(fileobj=buf, mode="w") as tout:
        notes = host_backup._add_apps(tout, str(apps_root), files)
    assert notes["files"] >= 6 and any(f["path"] == "apps/passport/host.key" for f in files), files
    assert iid not in buf.getvalue() + json.dumps(files).encode()


def test_21_the_id_is_named_in_no_other_module():
    """It is made and sent in member_client.py and nowhere else under server/,
    so nothing else can write it down."""
    found = sorted(str(f.relative_to(Path(ROOT))) for f in (Path(ROOT) / "server").rglob("*.py")
                   if "instance_id" in f.read_text(encoding="utf-8", errors="replace"))
    assert found == ["server/bridge/member_client.py"], found


# ── 21. renewal: about daily, with jitter ──────────────────────────────────

def test_21_a_passport_remembers_when_its_certificate_began(pki, tmp_path):
    d = tmp_path / "passport"
    p = _enrolled(pki, d)
    leaf = x509.load_pem_x509_certificates((d / "chain.crt").read_bytes())[0]
    assert p.not_before == leaf.not_valid_before_utc == pp.load(d).not_before
    # The door backdates a certificate five minutes, makes it seven days long,
    # and allows its renewal a day after issue.
    assert p.not_after - p.not_before == dt.timedelta(days=7, minutes=5)
    assert p.renew_after - p.not_before == dt.timedelta(days=1, minutes=5)


def test_21_renewal_is_due_a_day_after_the_certificate_began_plus_one_jitter(pki, tmp_path):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    draws = []

    def jitter():
        draws.append(1)
        return 5400.0

    def door(op, params):
        assert op == "passport.renew"
        return {"ok": True, "data": pki.passport_answer(params["csr_pem"], before.host_uuid,
                                                         identity=False)}

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          jitter=jitter)
        m.passport, m.client = before, _StubClient(door)
        due = m.renew_due_at(before)
        # A day old (24 h after issue, which is what the door takes), plus the jitter.
        assert due == before.renew_after + dt.timedelta(seconds=5400)
        assert dt.timedelta(hours=24) <= due - before.not_before <= dt.timedelta(hours=26, minutes=5)
        # One draw for the certificate, however often it is asked.
        m.renew_due_at(before)
        m.renew_due_at(before)
        assert len(draws) == 1
        m._clock = lambda: before.renew_after + dt.timedelta(hours=1)        # inside the jitter
        assert await m.renew_if_due() == "not due" and m.client.sent == []
        m._clock = lambda: due + dt.timedelta(minutes=1)
        assert await m.renew_if_due() == "renewed"
        # The new certificate gets a draw of its own.
        assert m.passport.serial != before.serial and len(draws) == 1
        m.renew_due_at(m.passport)
        assert len(draws) == 2

    asyncio.run(run())


def test_21_hosts_spread_their_renewals_over_the_jitter_window(pki, tmp_path, monkeypatch):
    """Two hundred Hosts with one and the same certificate (a fleet enrolled
    together) do not all come at once, and none comes before the door takes it."""
    monkeypatch.setattr(ms, "RENEW_JITTER_S", _REAL_JITTER_S)
    assert _REAL_JITTER_S == 2 * 3600.0 and ms.RENEW_AGE == dt.timedelta(hours=24)
    p = _enrolled(pki, tmp_path / "passport")

    async def run():
        out = []
        for _ in range(200):
            m = ms.Membership(directory=tmp_path / "passport", set_client=lambda c: None)
            out.append((m.renew_due_at(p) - p.renew_after).total_seconds())
        return out

    late = asyncio.run(run())
    assert min(late) >= 0 and max(late) <= _REAL_JITTER_S
    assert max(late) - min(late) > _REAL_JITTER_S / 2
    assert len(set(late)) > 150


def test_21_renewal_is_never_before_the_door_takes_it_and_never_later_than_the_age_rule(
        pki, tmp_path):
    p = _enrolled(pki, tmp_path / "passport")
    t0 = p.not_before

    async def due(**kw):
        m = ms.Membership(directory=tmp_path / "passport", set_client=lambda c: None,
                          jitter=lambda: 0.0)
        return m.renew_due_at(dataclasses.replace(p, **kw))

    async def run():
        # The door's renew_after is later than a day: the Host waits for it.
        assert await due(renew_after=t0 + dt.timedelta(days=3)) == t0 + dt.timedelta(days=3)
        # The door's renew_after is earlier than a day: a day old is the rule.
        assert await due(renew_after=t0) == t0 + dt.timedelta(hours=24)
        # No start time known (a passport built by hand): the door's word.
        assert await due(not_before=None, renew_after=t0 + dt.timedelta(hours=30)) \
            == t0 + dt.timedelta(hours=30)

    asyncio.run(run())


@pytest.mark.parametrize("left,connected,want", [
    (dt.timedelta(hours=23), True, ms.RENEW_CHECK_S),           # far off: the hourly look
    (dt.timedelta(minutes=10), True, 600.0),                    # soon: when it is due
    (dt.timedelta(seconds=5), True, 60.0),                      # not busier than this
    (dt.timedelta(minutes=-1), False, ms.RENEW_RETRY_S),        # due, door not reached: the retry
])
def test_21_the_renewal_loop_sleeps_until_the_renewal_is_due(pki, tmp_path, monkeypatch,
                                                            left, connected, want):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    waits = []

    class _Stop(Exception):
        pass

    async def sleep(seconds):
        waits.append(seconds)
        raise _Stop

    monkeypatch.setattr(ms, "_sleep", sleep)

    async def run():
        m = ms.Membership(directory=d, anchor=pki.root_path, set_client=lambda c: None,
                          jitter=lambda: 1800.0)
        m._clock = lambda: m.renew_due_at(before) - left
        m.passport = before
        m.client = _StubClient(lambda op, params: pytest.fail(f"{op} was sent"))
        m.client.connected = connected
        with pytest.raises(_Stop):
            await m._renew_loop()

    asyncio.run(run())
    assert waits == [want]
    assert ms.RENEW_MIN_WAIT_S == 60.0


# ── 20. a frozen Host ──────────────────────────────────────────────────────

# The door's words (privatae bridge/member_server.py ERR_FROZEN and
# REFUSED_BEFORE_REJOIN), both with reason "conflict".
DOOR_FROZEN = "your Host's network certificate was used from two places; it has been stopped"
DOOR_BEFORE_REJOIN = ("this passport was issued before its Host joined the network again; "
                      "it no longer connects")


def _frozen_frame(error: str = DOOR_FROZEN) -> bytes:
    return json.dumps({"ok": False, "error": error, "code": 403,
                       "reason": "conflict"}).encode() + b"\n"


def test_20_the_freeze_is_the_conflict_reason_and_deletes_the_passport():
    assert mc.FROZEN == "conflict" and "conflict" in mc.REFUSAL_REASONS
    assert "conflict" in ms.DELETE_ON
    assert "frozen" not in mc.REFUSAL_REASONS and "frozen" not in ms.DELETE_ON


def test_20_heartbeats_stay_thirty_seconds_apart():
    """The door's grace for two instances is three heartbeats (INSTANCE_GRACE,
    90 s): a Host that beat slower would be taken for two."""
    assert bridge_client.HEARTBEAT_INTERVAL == 30


@pytest.mark.parametrize("reason,redialled", [("conflict", False), ("refused", True)])
def test_20_a_frozen_host_is_not_dialled_again_where_a_refused_one_is(pki, tmp_path,
                                                                     monkeypatch, reason,
                                                                     redialled):
    """The same door and the same waits; only the reason differs. A refusal that
    keeps the passport is tried again after REFUSED_RETRY_S (here 0.3 s). A
    frozen Host's passport is gone and nothing dials again, however long we
    wait."""
    monkeypatch.setattr(bridge_client, "HEARTBEAT_INTERVAL", 0.1)
    monkeypatch.setattr(mc, "REFUSED_RETRY_S", 0.3)
    d = tmp_path / "passport"
    _enrolled(pki, d)
    swaps = []

    async def refuse(writer):
        writer.write(json.dumps({"ok": False, "error": DOOR_FROZEN, "code": 403,
                                 "reason": reason}).encode() + b"\n")
        await writer.drain()

    conns, on_conn = _accept_twice_then(refuse)

    async def run():
        door = await _door(pki, "net.privatae.ai", on_conn, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=door.port, set_client=swaps.append)
        try:
            await m.boot()
            await asyncio.sleep(2.0)
            return m
        finally:
            await m.shutdown()
            await door.close()

    m = asyncio.run(run())
    if redialled:
        assert len(conns) >= 3, f"{len(conns)} connections in 2 s"
        assert pp.load(d) is not None and m.frozen is False
    else:
        assert len(conns) == 1, f"{len(conns)} connections in 2 s"
        assert pp.load(d) is None and m.client is None and m.frozen is True
        assert isinstance(swaps[-1], NoPlatform)


def test_20_a_frozen_answer_to_a_renewal_ends_the_renewals_too(pki, tmp_path):
    d = tmp_path / "passport"
    before = _enrolled(pki, d)
    ops, conns = [], []

    async def door(reader, writer):
        conns.append(1)
        while True:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            ops.append(req["op"])
            if req["op"] == "passport.renew":
                writer.write(_frozen_frame())         # no id, then the door closes
                await writer.drain()
                return
            writer.write(json.dumps({"ok": True, "data": {}, "id": req.get("id")}).encode() + b"\n")
            await writer.drain()

    async def run():
        srv = await _door(pki, "net.privatae.ai", door, client_cert=True)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, set_client=lambda c: None,
                          clock=lambda: before.renew_after + dt.timedelta(minutes=1))
        try:
            await m.boot()
            assert await _wait_for(lambda: m.client is not None and m.client.connected)
            assert await m.renew_if_due() == "refused"
            assert await _wait_for(lambda: m.frozen)
            await asyncio.sleep(0.5)
            assert ops.count("passport.renew") == 1 and len(conns) == 1
            assert m.passport is None and m.client is None and pp.load(d) is None
            assert not (d / "host.key.next").exists()
            assert await m.renew_if_due() == "not a member"
            return m.status()
        finally:
            await m.shutdown()
            await srv.close()

    st = asyncio.run(run())
    assert st["frozen"] is True and st["reason"] == "conflict"


@pytest.mark.parametrize("door_words", [DOOR_FROZEN, DOOR_BEFORE_REJOIN])
def test_20_the_frozen_host_says_what_happened_and_what_to_do_until_a_rejoin(
        pki, tmp_path, monkeypatch, door_words):
    """The door's words are shown as they are, then the way back: a new join
    code from the account page, entered in Settings, Network. A code the door
    refuses changes nothing; the rejoin's answer names the Host's previous
    host_uuid and entity id, which the Host takes unchanged."""
    d = tmp_path / "passport"
    first = _enrolled(pki, d, entity_id=731)
    enrol_says = {"refuse": True}

    async def frozen_door(reader, writer):
        writer.write(_frozen_frame(door_words))
        await writer.drain()

    async def enrol_door(reader, writer):
        req = json.loads(await reader.readline())
        if enrol_says["refuse"]:
            out = {"ok": False, "error": "this code is not valid", "code": 403}
        else:
            # A rejoin is a normal enrolment; the Host it names is the same one.
            out = {"ok": True, "data": pki.passport_answer(
                req["params"]["csr_pem"], first.host_uuid, entity_id=first.entity_id)}
        writer.write(json.dumps(out).encode() + b"\n")
        await writer.drain()

    async def run():
        srv = await _door(pki, "net.privatae.ai", frozen_door, client_cert=True)
        enrol = await _door(pki, "enrol.privatae.ai", enrol_door, client_cert=False)
        m = ms.Membership(directory=d, anchor=pki.root_path, door_host="127.0.0.1",
                          door_port=srv.port, enrol_host="127.0.0.1", enrol_port=enrol.port,
                          set_client=lambda c: None)
        try:
            await m.boot()
            assert await _wait_for(lambda: m.frozen)
            from server.routes import network_membership as routes
            monkeypatch.setattr(ms, "_current", m)
            st = await routes.status()
            assert st["member"] is False and st["frozen"] is True and st["reason"] == "conflict"
            assert st["last_error"] == door_words
            note = st["frozen_note"]
            assert note.startswith(door_words + ". ")
            assert "new join code" in note and ACCOUNT_NETWORK_URL in note
            assert "Settings, Network" in note
            # A code the door refuses does not make it look any less frozen.
            with pytest.raises(ms.EnrolmentRefused):
                await m.join("K7Q2-M9XD-4PWR")
            st = m.status()
            assert st["frozen"] is True and st["last_error"] == "this code is not valid"
            assert st["frozen_note"] == note
            # The rejoin: the same Host, taken as it is.
            enrol_says["refuse"] = False
            srv.server.close()                      # the old door is no longer asked
            st = await m.join("K7Q2-M9XD-4PWR")
            assert st["member"] is True and st["frozen"] is False and st["frozen_note"] is None
            assert st["reason"] is None and st["last_error"] is None
            assert st["host_uuid"] == first.host_uuid and st["entity_id"] == 731
            assert pp.load(d).host_uuid == first.host_uuid and pp.load(d).entity_id == 731
        finally:
            await m.shutdown()
            await srv.close()
            await enrol.close()

    asyncio.run(run())


def test_20_a_frozen_refusal_without_words_still_says_what_to_do(pki, tmp_path):
    async def run():
        m = ms.Membership(directory=tmp_path / "passport", set_client=lambda c: None)
        m.passport = _enrolled(pki, tmp_path / "passport")
        await m._on_refusal("conflict", "", m.passport.serial)
        return m.status()

    st = asyncio.run(run())
    assert st["frozen"] is True and "used from two places" in st["frozen_note"]
    assert "new join code" in st["frozen_note"]


def test_20_a_frozen_host_still_says_so_after_a_restart(pki, tmp_path, monkeypatch):
    """The freeze deletes the passport, so a restarted Host has none. It still
    says it was stopped and what to do, in Settings, Network and Remote access,
    until a join succeeds; a refused code changes nothing. No door is dialled:
    the only address given is a closed port on this machine."""
    from server.network.remote_access import RemoteAccess
    d = tmp_path / "passport"
    nowhere = dict(door_host="127.0.0.1", door_port=1, enrol_host="127.0.0.1", enrol_port=1,
                   set_client=lambda c: None)

    def membership():
        m = ms.Membership(directory=d, **nowhere)
        m._remote = RemoteAccess(m, state_path=tmp_path / "remote.json")
        return m

    async def run():
        m = membership()
        m.passport = _enrolled(pki, d, entity_id=731)
        await m._on_refusal("conflict", DOOR_FROZEN, m.passport.serial)
        frozen = m.status()
        assert pp.load(d) is None and frozen["frozen"] is True
        # The restart: a new process, nothing in memory, no passport on disk.
        again = membership()
        try:
            await again.boot()
            st = again.status()
            assert st["member"] is False and st["frozen"] is True
            assert st["frozen_note"] == frozen["frozen_note"]
            assert st["frozen_note"].startswith(DOOR_FROZEN + ". ")
            assert again.remote.status()["frozen"] == frozen["frozen_note"]

            async def refused(code, **kw):
                raise ms.EnrolmentRefused("this code is not valid", 403)

            monkeypatch.setattr(ms, "enrol", refused)
            with pytest.raises(ms.EnrolmentRefused):
                await again.join("K7Q2-M9XD-4PWR")
            assert again.status()["frozen_note"] == frozen["frozen_note"]
            assert (d / ms.FROZEN_FILE).exists()

            async def rejoined(code, **kw):
                return _enrolled(pki, d, entity_id=731)

            monkeypatch.setattr(ms, "enrol", rejoined)
            st = await again.join("K7Q2-M9XD-4PWR")
            assert st["member"] is True and st["frozen"] is False
        finally:
            await again.shutdown()
        assert not (d / ms.FROZEN_FILE).exists()
        # And the restart after the rejoin is a member's, not a frozen Host's.
        third = membership()
        try:
            await third.boot()
            assert third.status()["frozen"] is False and third.status()["member"] is True
        finally:
            await third.shutdown()

    asyncio.run(run())


def test_21_the_jitter_never_takes_a_renewal_past_the_expiry(pki, tmp_path):
    """Whatever renew_after the door gave, the jitter only spreads renewals:
    a passport whose renew_after is an hour before its expiry is renewed
    before it expires, and an ordinary one keeps its whole draw."""
    d = tmp_path / "passport"
    ordinary = _enrolled(pki, d)
    late = dataclasses.replace(ordinary, renew_after=ordinary.not_after - dt.timedelta(hours=1))
    m = ms.Membership(directory=d, set_client=lambda c: None, jitter=lambda: 7200.0)
    due = m.renew_due_at(late)
    assert late.renew_after <= due < late.not_after
    assert m.renew_due_at(ordinary) == ordinary.renew_after + dt.timedelta(seconds=7200)


def test_20_settings_network_in_a_browser_says_the_host_is_frozen(pki, tmp_path, monkeypatch):
    """The Settings page in headless Chromium over the real routes and a real
    Membership the door has frozen: the Network panel says what happened and what
    to do, with the door's own words as text, and the join box is the way back."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not available")
    import contextlib
    import socket
    import threading

    import uvicorn
    import yaml
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    from server.applications.tokens import AppToken
    from server.routes import network_membership as routes

    app_dir = Path(ROOT) / "system-apps" / "settings"
    css_dir = Path(ROOT) / "dashboard" / "base" / "css"
    perms = list(yaml.safe_load((app_dir / "app.yaml").read_text())["permissions"])
    door_words = "<b>your Host's network certificate was used from two places</b>"
    monkeypatch.setattr(ms, "_current", None)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        m = ms.Membership(directory=tmp_path / "passport", anchor=pki.root_path)
        m.frozen, m._frozen_why = True, door_words
        m.reason, m.last_error = "conflict", door_words
        ms._current = m
        yield
        await m.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def window_token(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            request.state.app_token = AppToken(app_id="settings", permissions=perms,
                                               window_id="test", iat=0)
        return await call_next(request)

    async def _owner_signed_in():
        return None

    app.dependency_overrides[routes.owner_only] = _owner_signed_in
    app.include_router(routes.router)

    @app.api_route("/api/{rest:path}", methods=["GET", "PUT", "POST", "DELETE"])
    def elsewhere(rest: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.get("/css/{name}")
    def css(name: str):
        target = (css_dir / name).resolve()
        if target.parent != css_dir.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/css")

    @app.get("/system-apps/settings/{rest:path}")
    def static(rest: str):
        target = (app_dir / (rest or "index.html")).resolve()
        if app_dir.resolve() not in target.parents:
            return Response(status_code=403)
        return FileResponse(str(target)) if target.is_file() else Response(status_code=404)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(300):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "the test server did not start"
        expect = sync_api.expect
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"chromium unavailable: {e}")
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/system-apps/settings/index.html#view=network",
                          wait_until="domcontentloaded", timeout=15_000)
                state = page.locator("#net-state")
                expect(state).to_contain_text("This Host was stopped by the network",
                                              timeout=10_000)
                expect(state).to_contain_text("get a new join code from your account page")
                expect(state).to_contain_text(ACCOUNT_NETWORK_URL)
                expect(state).to_contain_text("enter it here, in Settings, Network")
                # The door's own words, as text, once.
                expect(state).to_contain_text(door_words)
                assert state.inner_text().count(door_words) == 1
                assert page.locator("#net-state b").count() == 0
                # The way back is on the page; there is nothing to leave.
                expect(page.locator("#net-join")).to_be_visible()
                expect(page.locator("#net-code")).to_be_visible()
                expect(page.locator("#net-leave-row")).to_be_hidden()
                assert errors == [], errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=15)
