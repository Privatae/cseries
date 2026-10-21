#!/usr/bin/env python3
"""A standalone Host pairs with the bastion on its own machine, under its own CA.

What this gate holds (server/network/host_ca.py, local_bastion.py,
bastion_enroll.redeem_local):

  1. The Host CA: made once (two first callers store and return ONE), ECDSA
     P-256, a CA that may sign only end certificates. An operator certificate
     is built from the checked CSR: CN=operator, the device fingerprint, client
     auth. A CSR naming any other identity (the router's CN), or asking for
     more than its fingerprint, or not signed by its own key, is refused.
  2. The Host CA key is never written outside the vault: across first boot,
     a redeem, the lane certificate and a lane handshake, the only write that
     carries it is the one kernel_secrets INSERT. No file opened for writing
     holds it, the lane's leaf-key file is gone once loaded, and no answer
     carries it.
  3. enroll.sock serves the redeem, same body as today, on the design's path,
     today's Host route and pair.go's path. The answer is the unix shape:
     operator_cert, ca_cert, bearer, transport "unix", lane_socket, runs_dir,
     host_handle (and today's operator_crt, ca_crt, host_name), both sockets
     inside one directory, host_name the same single name the lane
     certificate carries, and no relay or tunnel field. The cert verifies against the
     CA. A spent token and an unknown token are refused 401 and mint nothing;
     a refused CSR or a wrong handle is refused before the token is spent; a
     signing failure gives the token back. Other paths 404, other methods 405.
  4. lane.sock is the :8443 door's own handler under the Host CA: a bastion on
     its operator cert, checking the lane's name like the Go client
     (ServerName = host_name), reaches the kernel on loopback with
     X-Client-Cert-CN: operator and the front-door proof. A client cert from
     any other CA, even with CN=operator, and no client cert at all, are
     refused at the handshake and nothing reaches the kernel.
  5. Behind the lane, the kernel is unchanged: what the door forwarded, handed
     to the REAL server.main app, gets the REAL /bastion-harness handler.
     Without a bearer, or with a wrong one, it closes 4401; with the bearer
     from the redeem the worker is authed and parked. A dashboard route on
     the same cert is refused as a cross-tenant cert, and a non-bastion
     WebSocket is closed before its handler.
  6. With a platform (a bridge that is not NoPlatform), nothing changes: the
     supervisor does nothing (no CA row, no socket), the handle comes from the
     bridge, and the pairing command is today's.
  7. The supervisor end to end on a standalone Host: first boot makes the CA
     even with no shared directory; with one, both sockets come up 0660 in the
     directory's group, a redeem works through them, and they are removed on
     shutdown. A stale socket is replaced; a live one, or a non-socket, is not.
  8. The coder client's helper: open_run_connection / run_connector reach a
     run's socket in runs/ by run id or by the run-up answer's control_socket,
     refuse a run id or path that could name anything else, and refuse a
     socket the bastion (root) does not own.
  9. The enroll and lane ports, for a bastion outside the Host's VM. The
     enroll port's certificate verifies against the Host CA alone under
     host.cseries.internal, and under no other name or CA. POST
     /v1/local/redeem answers transport "tcp" and the lane's address, and
     notes the certificate's serial on the bastion's row; a reused, wrong or
     expired token is refused 401 and mints nothing; no other path is
     served. On the lane port the redeemed certificate reaches the kernel as
     operator with the front door's proof; a certificate from another CA, one
     the Host CA signed that no redeem issued, none at all, and the redeemed
     one once its bastion is revoked, reach nothing; a lane already open when
     the Host revokes its bastion (nac_commit.revoke_bastion) is closed by the
     revoke. The supervisor serves
     the ports with or without the shared directory, never with a platform.
 10. `python3 -m cli.cseries bastion pair-token` prints exactly {"token",
     "host_ca_pem", "enroll"}: a token the Host minted for its own handle,
     its CA, and https://127.0.0.1:8810/v1/local/redeem (or the port
     CSERIES_ENROLL_PORT names).

Stub pool and stub upstream; real TLS on real unix sockets in a temporary
directory and on loopback ports; no DB, no container, no network.

    python3 scripts/test_host_owns_bastion.py
"""
import asyncio
import atexit
import base64
import datetime
import hashlib
import json
import logging
import os
import pathlib
import shutil
import socket
import ssl
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

def _socket_tmp() -> pathlib.Path:
    """The test's folder: in TMPDIR when the deepest socket it binds there
    (shared/runs/<run id>.sock, a real AF_UNIX address, at most 107 bytes)
    fits, else in /tmp. The pre-merge runner's TMPDIR is a scratch folder
    of its own, deep enough that the run socket did not fit, so this gate
    failed there and passed when run by hand."""
    deepest = len("/host-owns-bastion-XXXXXXXX/shared/runs/run-0001abcd.sock")
    for base in (tempfile.gettempdir(), "/tmp"):
        if len(os.path.abspath(base)) + deepest <= 107 and os.path.isdir(base):
            return pathlib.Path(tempfile.mkdtemp(prefix="host-owns-bastion-", dir=base))
    raise SystemExit("FAIL: no temp folder short enough for a unix socket "
                     f"(TMPDIR={tempfile.gettempdir()!r})")


TMP = _socket_tmp()
# Removed however the run ends, an import failure included.
atexit.register(shutil.rmtree, TMP, True)
SHARED = TMP / "shared"
SHARED.mkdir()
os.chmod(SHARED, 0o1770)
(SHARED / "runs").mkdir()
os.chmod(SHARED / "runs", 0o750)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


UP_PORT = _free_port()
# The router's client-cert name: unset in a standalone Host, set here so the
# checks that no CSR or lane certificate may take it have a name to refuse.
ROUTER = "router.example.test"
os.environ.update({
    "PRIVATAE_HOST_DIR": str(SHARED),
    "DASHBOARD_ROUTER_CN": ROUTER,
    # The lane door forwards here: a stub kernel that records what arrives.
    "FRONTDOOR_UPSTREAM_HOST": "127.0.0.1", "FRONTDOOR_UPSTREAM_PORT": str(UP_PORT),
})
for _v in ("ENTITY_HANDLE", "HANDLE", "CSERIES_URL"):
    os.environ.pop(_v, None)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from server.bridge.no_platform import NoPlatform  # noqa: E402
from server.home_origin import HOME  # noqa: E402
from server.network import bastion_enroll, host_ca, local_bastion  # noqa: E402

# The supervisor's two ports, on loopback and free here: their defaults are
# every address of the Host container's own network namespace.
local_bastion.TCP_BIND = "127.0.0.1"
local_bastion.ENROLL_PORT, local_bastion.LANE_PORT = _free_port(), _free_port()

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


# ── stub pool ────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        await asyncio.sleep(0)
        self.pool.writes.append((sql, args))
        if sql.lstrip().startswith("INSERT INTO kernel_secrets"):
            self.pool.secrets.setdefault(args[0], args[1])

    async def fetchrow(self, sql, *args):
        await asyncio.sleep(0)
        if "FROM kernel_secrets" in sql:
            v = self.pool.secrets.get(args[0])
            return None if v is None else {"secret": v}
        raise AssertionError(sql)


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class FakePool:
    """kernel_secrets, bastion_pairing_tokens and mcp_client_tokens, as state."""

    def __init__(self):
        self.secrets = {}
        self.tokens = {}       # secret_hash -> {"name", "used", "expired"}
        self.bearers = []      # INSERTed mcp_client_tokens rows
        self.revoked = set()   # ids of revoked mcp_client_tokens rows
        self.writes = []       # every (sql, args) that writes

    def acquire(self):
        return _Acquire(self)

    async def close(self):
        return None

    async def execute(self, sql, *args):
        self.writes.append((sql, args))
        if sql.lstrip().startswith("INSERT INTO bastion_pairing_tokens"):
            self.tokens[args[0]] = {"name": args[1], "used": False, "expired": False}
            return "INSERT 0 1"
        if "SET used_at = NULL" in sql:
            t = self.tokens.get(args[0])
            if t:
                t["used"] = False
                return "UPDATE 1"
            return "UPDATE 0"
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "UPDATE bastion_pairing_tokens SET used_at = now()" in sql:
            self.writes.append((sql, args))
            # Each refusal only when the query says so, so the SQL is what is tested.
            t = self.tokens.get(args[0])
            if (t is None or (t["used"] and "used_at IS NULL" in sql)
                    or (t["expired"] and "expires_at > now()" in sql)):
                return None
            t["used"] = True
            return {"name": t["name"]}
        if "INSERT INTO mcp_client_tokens" in sql:
            self.writes.append((sql, args))
            self.bearers.append(args)
            return {"id": len(self.bearers)}
        if "notes->>'cert_serial' = $2" in sql:
            for i, row in enumerate(self.bearers, 1):
                if (row[0] == args[0] and json.loads(row[5]).get("cert_serial") == args[1]
                        and not (i in self.revoked and "revoked_at IS NULL" in sql)):
                    return {"id": i, "name": row[1]}
            return None
        if "FROM mcp_client_tokens WHERE token_hash" in sql:
            for i, row in enumerate(self.bearers, 1):
                if row[2] == args[0]:
                    return {"id": i, "op_uuid": json.loads(row[5]).get("operator_uuid"),
                            "hardware_fingerprint": row[6]}
            return None
        raise AssertionError(sql)


class FakeBridge:
    """A platform Host's client: a BridgeClient's status has no "standalone"."""
    _connected = True

    def __init__(self):
        self.ops = []

    @property
    def status(self):
        return {"connected": True}

    async def request_full(self, op, params=None):
        self.ops.append(op)
        if op == "synaptive.handle.self":
            return {"ok": True, "data": {"handle": "platformhandle"}}
        return {"ok": False, "error": "not in this test", "code": 502}


def as_standalone():
    local_bastion._bridge_client = lambda: NoPlatform()


def as_platform(bridge):
    local_bastion._bridge_client = lambda: bridge


# ── what a bastion makes (client-go/pair.go genOperatorCSR) ─────────────────

FP = "abcdefghijklmnopqrstuvwxyz012345"


# The bastion names its fingerprint twice (client-go/pair.go fpURIs): first
# under the platform's name, then under this Host's. A stand-in for the first.
OTHER_FP_PREFIX = "urn:platform:hwfp:"


def make_csr(cn="operator", fp=FP, extra_san=None, other_fp=None):
    key = ec.generate_private_key(ec.SECP256R1())
    names = ([x509.UniformResourceIdentifier(OTHER_FP_PREFIX + (other_fp or fp)),
              x509.UniformResourceIdentifier(host_ca.FP_SAN_PREFIX + fp)] if fp else [])
    if extra_san:
        names.append(extra_san)
    b = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
    if names:
        b = b.add_extension(x509.SubjectAlternativeName(names), critical=False)
    csr = b.sign(key, hashes.SHA256())
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def _only_other_name_csr() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "operator")]))
        .add_extension(x509.SubjectAlternativeName(
            [x509.UniformResourceIdentifier(OTHER_FP_PREFIX + FP)]), critical=False)
        .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def write_pair(stem, cert_pem, key):
    c, k = TMP / f"{stem}.crt", TMP / f"{stem}.key"
    c.write_bytes(cert_pem.encode() if isinstance(cert_pem, str) else cert_pem)
    k.write_bytes(key_pem(key))
    return str(c), str(k)


def other_ca_operator_cert():
    """CN=operator, signed by a CA that is not this Host's (a platform's, say)."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "some other CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "operator")]))
        .issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), key


async def mint(pool) -> str:
    """A pairing token through the real mint; returns its secret."""
    out = await bastion_enroll.mint_pairing_token(pool, entity_id=1, handle=HOME.handle, name="vps")
    raw = out["token"][len("skb_"):]
    raw += "=" * (-len(raw) % 4)
    handle, _, secret = base64.urlsafe_b64decode(raw).decode().partition(".")
    assert handle == HOME.handle
    return secret, out


# ── raw HTTP over a unix socket ──────────────────────────────────────────────

async def http_unix(path, method, target, body=None, *, ssl_ctx=None, server_hostname=None,
                    raw_body=None, headers=None):
    reader, writer = await asyncio.open_unix_connection(path, ssl=ssl_ctx,
                                                        server_hostname=server_hostname)
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
    hdrs = {"Host": server_hostname or "localhost", "Content-Length": str(len(data)),
            "Content-Type": "application/json", "Connection": "close", **(headers or {})}
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
    writer.write(head.encode() + b"\r\n" + data)
    await writer.drain()
    resp = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    status = int(resp.split(b" ", 2)[1])
    payload = resp.split(b"\r\n\r\n", 1)[1]
    return status, (json.loads(payload) if payload else None)


async def http_tcp(port, method, target, body=None, *, ssl_ctx, server_hostname,
                   headers=None):
    """One request to 127.0.0.1:<port> over TLS; (status, JSON body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ssl_ctx,
                                                   server_hostname=server_hostname)
    data = json.dumps(body).encode() if body is not None else b""
    hdrs = {"Host": server_hostname, "Content-Length": str(len(data)),
            "Content-Type": "application/json", "Connection": "close", **(headers or {})}
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
    writer.write(head.encode() + b"\r\n" + data)
    await writer.drain()
    resp = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    status = int(resp.split(b" ", 2)[1])
    payload = resp.split(b"\r\n\r\n", 1)[1]
    return status, (json.loads(payload) if payload else None)


def client_ctx(ca_pem, cert_key=None):
    """What the bastion's Go client does on the ports: the Host CA and no
    system roots, the name checked; its operator certificate when it has one."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca_pem)
    if cert_key:
        ctx.load_cert_chain(*cert_key)
    return ctx


# ── 1. the CA ────────────────────────────────────────────────────────────────

async def part_ca():
    print("== 1. the Host CA")
    pool = FakePool()
    await asyncio.gather(host_ca.ensure(pool), host_ca.ensure(pool), host_ca.ca_cert_pem(pool))
    a = await host_ca.ca_cert_pem(pool)
    b = await host_ca.ca_cert_pem(pool)
    check("two first callers racing store ONE CA, and every caller gets it",
          len(pool.secrets) == 1 and a == b, list(pool.secrets))
    ca = x509.load_pem_x509_certificate(a.encode())
    check("the CA key is ECDSA P-256",
          isinstance(ca.public_key(), ec.EllipticCurvePublicKey)
          and ca.public_key().curve.name == "secp256r1", ca.public_key())
    bc = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    check("it is a CA that may sign end certificates only (path length 0)",
          bc.ca and bc.path_length == 0, bc)

    _key, csr = make_csr()
    cert_pem, serial = await host_ca.sign_operator_csr(pool, csr)
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    try:
        cert.verify_directly_issued_by(ca)
        issued = True
    except Exception as e:
        issued = e
    check("the operator cert is issued by the Host CA", issued is True, issued)
    check("its subject is CN=operator and nothing else",
          cert.subject.rfc4514_string() == "CN=operator", cert.subject)
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    check("it carries the device fingerprint the CSR named",
          sans.get_values_for_type(x509.UniformResourceIdentifier) == [host_ca.FP_SAN_PREFIX + FP], sans)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    check("it is good for client auth only", list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH], eku)
    check("the serial returned is the cert's", serial == format(cert.serial_number, "x"), serial)

    _k, only_ours = make_csr()
    csr_obj = x509.load_pem_x509_csr(only_ours.encode())
    check("the CSR here is the bastion's: the fingerprint under two names, ours second",
          csr_obj.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
          .get_values_for_type(x509.UniformResourceIdentifier)
          == [OTHER_FP_PREFIX + FP, host_ca.FP_SAN_PREFIX + FP])
    for label, pem in (
        ("a CSR naming two different fingerprints", make_csr(other_fp="z" * 32)[1]),
        ("a CSR naming its fingerprint only under another name",
         _only_other_name_csr()),
        ("a CSR asking for the router's CN", make_csr(cn=ROUTER)[1]),
        ("a CSR asking for a DNS name beside its fingerprint",
         make_csr(extra_san=x509.DNSName("home.cseries.localhost"))[1]),
        ("two CSRs in one block", make_csr()[1] + make_csr()[1]),
        ("something that is not a CSR", "-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n"
                                        "-----END CERTIFICATE REQUEST-----"),
    ):
        try:
            await host_ca.sign_operator_csr(pool, pem)
            check(f"{label}: refused", False, "signed")
        except host_ca.CSRRefused:
            check(f"{label}: refused", True)
    # A CSR whose signature does not match its content.
    _k, good = make_csr()
    der = bytearray(x509.load_pem_x509_csr(good.encode()).public_bytes(serialization.Encoding.DER))
    der[-5] ^= 0x01
    tampered = ("-----BEGIN CERTIFICATE REQUEST-----\n"
                + base64.encodebytes(bytes(der)).decode()
                + "-----END CERTIFICATE REQUEST-----\n")
    try:
        await host_ca.sign_operator_csr(pool, tampered)
        check("a CSR not signed by its own key: refused", False, "signed")
    except host_ca.CSRRefused:
        check("a CSR not signed by its own key: refused", True)
    try:
        await host_ca.issue_lane_server_cert(pool, [ROUTER])
        check("the lane cert can never carry the router's name", False, "issued")
    except ValueError:
        check("the lane cert can never carry the router's name", True)


# ── 2 + 3 + 4. one standalone Host, its sockets, a bastion ──────────────────

class Upstream:
    """The stub kernel the lane door forwards to: records each request head."""

    def __init__(self):
        self.heads = []

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head)
            for ln in head.split(b"\r\n"):
                if ln.lower().startswith(b"content-length:"):
                    await reader.readexactly(int(ln.split(b":", 1)[1]))
            body = b'{"kernel":"reached"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                         b"Connection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()


def header(head: bytes, name: bytes):
    vals = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n")[1:]
            if b":" in ln and ln.split(b":", 1)[0].strip().lower() == name]
    return vals


async def part_sockets():
    print("== 2-5. enroll.sock, lane.sock and the kernel behind them (standalone)")
    as_standalone()
    pool = FakePool()
    up = Upstream()
    up_srv = await asyncio.start_server(up.handle, "127.0.0.1", UP_PORT)

    # 2. Watch every write while the Host makes its CA, serves a redeem,
    #    makes its lane cert and serves a lane handshake.
    opened_for_write = []
    chains_loaded = []
    watching = [True]

    def audit(event, args):
        if not watching[0] or event != "open":
            return
        path, mode, flags = (list(args) + [None, None, None])[:3]
        w = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
            (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if w:
            opened_for_write.append(str(path))
    sys.addaudithook(audit)
    real_load = host_ca.load_server_chain

    def recording_load(ctx, cert_pem, key_pem_):
        chains_loaded.append(cert_pem + key_pem_)
        return real_load(ctx, cert_pem, key_pem_)
    host_ca.load_server_chain = recording_load

    names = await local_bastion.lane_names(1)
    ctx, _until = await host_ca.lane_server_ssl_context(pool, names)
    enroll = await local_bastion.start_enroll_server(local_bastion.enroll_socket(),
                                                     pool_getter=lambda: pool)
    lane = await local_bastion.start_lane_server(local_bastion.lane_socket(), ctx)
    answers = []
    try:
        # ── 3. redeem over enroll.sock
        secret, minted = await mint(pool)
        check("standalone: the pairing command redeems on the Host's socket",
              f"--redeem-url unix://{local_bastion.enroll_socket()} " in minted["run"]["pair"],
              minted["run"]["pair"])
        bkey, csr = make_csr()
        body = {"secret": secret, "wg_public_key": "x" * 44, "operator_csr": csr,
                "hostname": "vps-1", "platform": "linux", "handle": HOME.handle}
        status, out = await http_unix(local_bastion.enroll_socket(), "POST",
                                      "/bastion/enroll/redeem", body)
        answers.append(json.dumps(out or {}))
        check("redeem on enroll.sock answers 200", status == 200, (status, out))
        out = out or {}
        want = {"operator_cert", "ca_cert", "bearer", "transport", "lane_socket", "runs_dir",
                "host_handle"}
        check("the answer has the design's unix fields", want <= set(out), sorted(out))
        check("transport is unix, the sockets are the shared directory's, the handle is this Host's",
              out.get("transport") == "unix"
              and out.get("lane_socket") == str(SHARED / "lane.sock")
              and out.get("runs_dir") == str(SHARED / "runs")
              and out.get("host_handle") == HOME.handle, out)
        shared = os.path.dirname(local_bastion.enroll_socket())
        check("lane_socket and runs_dir are inside the enroll socket's directory",
              os.path.dirname(out.get("lane_socket", "")) == shared
              and os.path.dirname(out.get("runs_dir", "")) == shared, (shared, out.get("runs_dir")))
        check("ONE name: the answer's host_name is local_bastion.host_name() and the lane "
              "certificate's first DNS name",
              out.get("host_name") == await local_bastion.host_name(1) == names[0],
              (out.get("host_name"), names))
        check("no relay endpoint and no tunnel address in it",
              not ({"endpoint", "endpoints", "endpoint_pubkey", "bastion_tunnel_ip",
                    "host_tunnel_ip"} & set(out)), sorted(out))
        check("today's field names ride along for pair.go (operator_crt, ca_crt, host_name)",
              out.get("operator_crt") == out.get("operator_cert")
              and out.get("ca_crt") == out.get("ca_cert")
              and out.get("host_name") == names[0], out.get("host_name"))
        op_cert = x509.load_pem_x509_certificate(out["operator_cert"].encode())
        ca_cert = x509.load_pem_x509_certificate(out["ca_cert"].encode())
        try:
            op_cert.verify_directly_issued_by(ca_cert)
            ok = True
        except Exception as e:
            ok = e
        check("the operator cert verifies against the CA in the answer", ok is True, ok)
        row = pool.bearers[0] if pool.bearers else None
        check("one bastion bearer row, fingerprint bound from the CSR, transport noted",
              len(pool.bearers) == 1 and row[6] == FP
              and json.loads(row[5]).get("transport") == "unix"
              and row[2] == hashlib.sha256(out["bearer"].encode()).digest(), row)

        status, again = await http_unix(local_bastion.enroll_socket(), "POST",
                                        "/bastion/enroll/redeem", body)
        check("the spent token is refused 401 and mints nothing",
              status == 401 and len(pool.bearers) == 1, (status, again))
        status, unknown = await http_unix(local_bastion.enroll_socket(), "POST",
                                          "/bastion/enroll/redeem", {**body, "secret": "never-minted"})
        check("an unknown token is refused 401 and mints nothing",
              status == 401 and len(pool.bearers) == 1, (status, unknown))
        check("a refusal answers `error`, which pair.go prints",
              bool((unknown or {}).get("error")), unknown)

        secret2, _ = await mint(pool)
        h2 = hashlib.sha256(secret2.encode()).digest()
        status, _o = await http_unix(local_bastion.enroll_socket(), "POST", "/bastion/enroll/redeem",
                                     {**body, "secret": secret2,
                                      "operator_csr": make_csr(cn=ROUTER)[1]})
        check("a CSR the Host will not sign is refused 400 and the token is not spent",
              status == 400 and not pool.tokens[h2]["used"], (status, pool.tokens[h2]))
        status, _o = await http_unix(local_bastion.enroll_socket(), "POST", "/bastion/enroll/redeem",
                                     {**body, "secret": secret2, "handle": "someoneelse"})
        check("a token for another Host is refused 409 and the token is not spent",
              status == 409 and not pool.tokens[h2]["used"], (status, pool.tokens[h2]))

        real_sign = host_ca.sign_operator_csr

        async def broken_sign(*a, **k):
            raise RuntimeError("signing broke")
        host_ca.sign_operator_csr = broken_sign
        try:
            status, _o = await http_unix(local_bastion.enroll_socket(), "POST",
                                         "/bastion/enroll/redeem", {**body, "secret": secret2})
        finally:
            host_ca.sign_operator_csr = real_sign
        check("a signing failure answers 500 and gives the token back",
              status == 500 and not pool.tokens[h2]["used"], (status, pool.tokens[h2]))
        for path in ("/api/network/bastions/redeem", f"/v1/bastions/{HOME.handle}/redeem"):
            s3, _ = await mint(pool)
            status, o3 = await http_unix(local_bastion.enroll_socket(), "POST", path,
                                         {**body, "secret": s3, "operator_csr": make_csr()[1]})
            answers.append(json.dumps(o3 or {}))
            check(f"the redeem is served on {path} too", status == 200, (status, o3))
        status, _o = await http_unix(local_bastion.enroll_socket(), "POST", "/api/system/health", body)
        check("any other path on enroll.sock is 404", status == 404, status)
        status, _o = await http_unix(local_bastion.enroll_socket(), "GET", "/bastion/enroll/redeem")
        check("GET on the redeem path is 405", status == 405, status)
        status, _o = await http_unix(local_bastion.enroll_socket(), "POST", "/bastion/enroll/redeem",
                                     raw_body=b"x" * 10, headers={"Content-Length": str(10 ** 6)})
        check("an oversized body is refused 413 before it is read", status == 413, status)

        # ── 4. the lane
        op_crt, op_key = write_pair("bastion", out["operator_cert"], bkey)
        bastion_ctx = ssl.create_default_context(cadata=out["ca_cert"])  # verifies the name too
        bastion_ctx.load_cert_chain(op_crt, op_key)
        status, got = await http_unix(local_bastion.lane_socket(), "POST",
                                      "/api/network/bastions/heartbeat", {"beat": 1},
                                      ssl_ctx=bastion_ctx, server_hostname=out["host_name"],
                                      headers={"X-Client-Cert-CN": ROUTER})
        check("a bastion on its operator cert, checking the lane's name, reaches the kernel",
              status == 200 and got == {"kernel": "reached"}, (status, got))
        head = up.heads[-1] if up.heads else b""
        from server.middleware import router_identity
        check("...with X-Client-Cert-CN: operator, its own forged CN stripped",
              header(head, b"x-client-cert-cn") == [b"operator"], header(head, b"x-client-cert-cn"))
        check("...and the front door's proof",
              header(head, router_identity.PROOF_HEADER) == [router_identity.proof()], head[:200])
        lane_head = head

        n = len(up.heads)
        other_crt, other_key = write_pair("other", *other_ca_operator_cert())
        wrong = ssl.create_default_context(cadata=out["ca_cert"])
        wrong.load_cert_chain(other_crt, other_key)
        try:
            await http_unix(local_bastion.lane_socket(), "GET", "/api/system/health",
                            ssl_ctx=wrong, server_hostname=out["host_name"])
            refused = False
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError, ValueError):
            refused = True
        check("a CN=operator cert from another CA is refused at the handshake, nothing reaches the kernel",
              refused and len(up.heads) == n, (refused, len(up.heads) - n))
        bare = ssl.create_default_context(cadata=out["ca_cert"])
        try:
            await http_unix(local_bastion.lane_socket(), "GET", "/api/system/health",
                            ssl_ctx=bare, server_hostname=out["host_name"])
            refused = False
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError, ValueError):
            refused = True
        check("no client cert: refused at the handshake, nothing reaches the kernel",
              refused and len(up.heads) == n, (refused, len(up.heads) - n))

        st_e, st_l = os.stat(local_bastion.enroll_socket()), os.stat(local_bastion.lane_socket())
        check("both sockets are 0660 in the shared directory's group",
              stat.S_IMODE(st_e.st_mode) == 0o660 and stat.S_IMODE(st_l.st_mode) == 0o660
              and st_e.st_gid == st_l.st_gid == os.stat(SHARED).st_gid,
              (oct(st_e.st_mode), oct(st_l.st_mode)))
    finally:
        watching[0] = False
        host_ca.load_server_chain = real_load
        enroll.close()
        lane.close()
        up_srv.close()

    # ── 2. where the CA key went
    raw = pool.secrets[host_ca.KEY_ID]
    ca_key = serialization.load_pem_private_key(raw[:raw.index(b"-----BEGIN CERTIFICATE-----")], None)
    der = ca_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    scalar = ca_key.private_numbers().private_value.to_bytes(32, "big")
    body_b64 = "".join(raw[:raw.index(b"-----BEGIN CERTIFICATE-----")].decode()
                       .split("-----")[2].split())

    def carries(blob: bytes) -> bool:
        flat = blob.replace(b"\n", b"").replace(b"\r", b"")
        return der in blob or scalar in blob or body_b64.encode() in flat

    carrying = [sql.split()[0:3] for sql, args in pool.writes
                if any(isinstance(a, (bytes, bytearray)) and carries(bytes(a)) for a in args)]
    check("the CA key's only write is the one kernel_secrets INSERT",
          carrying == [["INSERT", "INTO", "kernel_secrets"]], carrying)
    # Not counted: compiled imports, an fd re-opened by number (its path was
    # counted when it was opened), and this test's own scratch files (the
    # bastion's cert and key it writes for its client), which the next check
    # but one still scans for the CA key.
    files = [p for p in opened_for_write
             if not p.endswith(".pyc") and "__pycache__" not in p and not p.isdigit()
             and not p.startswith(str(TMP) + os.sep)]
    check("the only file opened for writing is the lane's own chain file",
          files and all(os.path.basename(p) == "chain.pem" for p in files), files)
    check("...which holds the lane's leaf key, never the CA key",
          chains_loaded and not any(carries(c) for c in chains_loaded), len(chains_loaded))
    check("...and is gone once loaded", not any(os.path.exists(p) for p in files), files)
    still = [str(p) for p in list(TMP.rglob("*")) + list(pathlib.Path(tempfile.gettempdir()).glob("lane-*"))
             if p.is_file() and carries(p.read_bytes())]
    check("no file left in the temp areas holds it", not still, still)
    check("no redeem answer carries it", not any(carries(a.encode()) for a in answers))
    return pool, out, bkey, lane_head


# ── 5. the real kernel behind the lane ───────────────────────────────────────

def _import_real_main():
    # server.main mounts /app/dashboard/* at import, which exists only in the
    # image; nothing here serves those files, so their directory check is off.
    import starlette.staticfiles as sf
    real_init = sf.StaticFiles.__init__

    def init(self, *a, **k):
        k["check_dir"] = False
        real_init(self, *a, **k)
    sf.StaticFiles.__init__ = init
    try:
        import server.main as M
    finally:
        sf.StaticFiles.__init__ = real_init
    return M


def _scope_from_head(head: bytes, kind: str, path: str):
    """The ASGI scope uvicorn makes from what the door forwarded: the same
    headers, from the loopback address the door connects from."""
    lines = head.split(b"\r\n")
    hdrs = []
    for ln in lines[1:]:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            k = k.strip().lower()
            if k in (b"content-length", b"content-type", b"connection"):
                continue
            hdrs.append((k, v.strip()))
    scope = {"type": kind, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": hdrs, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8800),
             "scheme": "ws" if kind == "websocket" else "http", "root_path": "",
             "http_version": "1.1", "asgi": {"version": "3.0"}, "state": {}}
    if kind == "websocket":
        scope["headers"] = hdrs + [(b"upgrade", b"websocket"), (b"connection", b"upgrade"),
                                   (b"sec-websocket-version", b"13"),
                                   (b"sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ==")]
        scope["subprotocols"] = []
    else:
        scope["method"] = "GET"
    return scope


async def _ws(app, scope, hello, linger=0.5):
    """Run one WebSocket through the app: send `hello` once accepted, then
    wait; returns (close code or None, accepted)."""
    inbox = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    sent = []

    async def receive():
        return await inbox.get()

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "websocket.accept" and hello is not None:
            await inbox.put({"type": "websocket.receive", "text": hello})

    task = asyncio.ensure_future(app(scope, receive, send))
    await asyncio.sleep(linger)
    closed = [m for m in sent if m["type"] == "websocket.close"]
    await inbox.put({"type": "websocket.disconnect", "code": 1000})
    try:
        await asyncio.wait_for(task, 5)
    except Exception:
        pass
    return (closed[0].get("code") if closed else None,
            any(m["type"] == "websocket.accept" for m in sent))


async def _http(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


async def part_kernel(pool, out, lane_head):
    print("== 5. the real kernel behind the lane: bearer checks unchanged")
    M = _import_real_main()
    from server import brain_service
    from server.network.nac_harness import get_harness_relay
    brain_service._pool = pool
    app = M.app

    scope = lambda kind, path: _scope_from_head(lane_head, kind, path)  # noqa: E731
    code, accepted = await _ws(app, scope("websocket", "/bastion-harness"), "hello")
    check("/bastion-harness over the lane without a bearer: closed 4401", code == 4401, (code, accepted))
    code, _a = await _ws(app, scope("websocket", "/bastion-harness"), "BEARER mcp_not-the-bearer")
    check("/bastion-harness over the lane with a wrong bearer: closed 4401", code == 4401, code)
    relay = get_harness_relay()
    code, _a = await _ws(app, scope("websocket", "/bastion-harness"), "BEARER " + out["bearer"])
    parked = any(relay._workers.get(k) is not None for k in relay._idle)
    check("/bastion-harness over the lane with the redeemed bearer: authed and parked as a worker",
          code is None and parked and (1, "harness") in relay._idle, (code, list(relay._idle)))
    code, accepted = await _ws(app, scope("websocket", "/ws"), None)
    check("a non-bastion WebSocket on the operator cert is closed before its handler",
          code == 1008 and not accepted, (code, accepted))
    status, body = await _http(app, scope("http", "/api/network/overview"))
    check("a dashboard route on the operator cert is refused as a cross-tenant cert",
          status == 403 and b"cross-tenant" in body, (status, body[:120]))
    brain_service._pool = None


# ── 6. with a platform ───────────────────────────────────────────────────────

async def part_platform():
    print("== 6. a Host with a platform: nothing changes")
    bridge = FakeBridge()
    as_platform(bridge)
    # bastion_enroll._bridge_op asks server.main for the client, as in a pod.
    M = sys.modules.get("server.main") or _import_real_main()
    saved_ref = M._bridge_client_ref
    M._bridge_client_ref = bridge
    for p in (local_bastion.enroll_socket(), local_bastion.lane_socket()):
        if os.path.exists(p):
            os.unlink(p)
    pool = FakePool()
    real_pool = local_bastion._control_pool
    local_bastion._control_pool = lambda: pool
    try:
        check("standalone() is False with a bridge that is not NoPlatform", not local_bastion.standalone())
        await asyncio.wait_for(local_bastion.run_supervised(1), 2)
        check("the supervisor returns at once and says so",
              local_bastion.STATE == local_bastion.TCP_STATE == "platform",
              (local_bastion.STATE, local_bastion.TCP_STATE))
        check("no enroll or lane port is listening",
              not _listening(local_bastion.ENROLL_PORT) and not _listening(local_bastion.LANE_PORT))
        check("no Host CA is made", host_ca.KEY_ID not in pool.secrets, list(pool.secrets))
        check("no socket is made",
              not os.path.exists(local_bastion.enroll_socket())
              and not os.path.exists(local_bastion.lane_socket()))
        bastion_enroll._self_handle_cache.clear()
        h = await bastion_enroll.resolve_self_handle(1)
        check("the handle comes from the bridge, not the Host's settings",
              h == "platformhandle" and "synaptive.handle.self" in bridge.ops, (h, bridge.ops))
        out = await bastion_enroll.mint_pairing_token(pool, entity_id=1, handle="platformhandle",
                                                      name="m")
        check("the pairing command is today's", out["run"]["pair"] ==
              f"privatae-bastion pair {out['token']}", out["run"]["pair"])
    finally:
        M._bridge_client_ref = saved_ref
        local_bastion._control_pool = real_pool
        bastion_enroll._self_handle_cache.clear()


# ── 6b. a Host that joined the network ───────────────────────────────────────

async def part_member():
    print("== 6b. a Host that joined the network (the member door) pairs its bastion locally")
    from server.bridge.member_client import NETWORK_OPS, MemberClient
    member = MemberClient.__new__(MemberClient)  # the type is what is asked; no connection
    local_bastion._bridge_client = lambda: member
    bastion_enroll._self_handle_cache.clear()
    pool = FakePool()
    try:
        check("the member door carries no bastion or handle op",
              not any(op.startswith(("bastion.", "synaptive.")) for op in NETWORK_OPS))
        check("standalone() is True on a member Host", local_bastion.standalone())
        check("its handle is its own setting", await bastion_enroll.resolve_self_handle(1) == HOME.handle)
        out = await bastion_enroll.mint_pairing_token(pool, entity_id=1, handle=HOME.handle, name="m")
        check("its pairing command redeems on the Host, not the platform",
              "--redeem-url " in out["run"]["pair"], out["run"]["pair"])
    finally:
        bastion_enroll._self_handle_cache.clear()
        as_standalone()


# ── 7. the supervisor on a standalone Host ───────────────────────────────────

async def part_supervisor():
    print("== 7. the supervisor on a standalone Host")
    as_standalone()
    check("standalone: the handle is the Host's own setting (CSERIES_URL's first label)",
          await bastion_enroll.resolve_self_handle(1) == HOME.handle)
    pool = FakePool()
    real_pool = local_bastion._control_pool
    local_bastion._control_pool = lambda: pool
    real_dir = os.environ["PRIVATAE_HOST_DIR"]
    try:
        os.environ["PRIVATAE_HOST_DIR"] = str(TMP / "not-there")
        t = asyncio.ensure_future(local_bastion.run_supervised(1))
        await asyncio.sleep(0.3)
        for _ in range(100):
            if local_bastion.TCP_STATE == "listening":
                break
            await asyncio.sleep(0.05)
        check("no shared directory: the Host waits for it and says so",
              local_bastion.STATE == "no-shared-dir", local_bastion.STATE)
        check("...and its first boot made the CA anyway", host_ca.KEY_ID in pool.secrets)
        check("...and the enroll and lane ports do not wait for it",
              local_bastion.TCP_STATE == "listening" and _listening(local_bastion.ENROLL_PORT)
              and _listening(local_bastion.LANE_PORT), local_bastion.TCP_STATE)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        os.environ["PRIVATAE_HOST_DIR"] = real_dir

        # A stale socket from an earlier start is replaced.
        s = socket.socket(socket.AF_UNIX)
        s.bind(local_bastion.enroll_socket())
        s.close()
        t = asyncio.ensure_future(local_bastion.run_supervised(1))
        for _ in range(100):
            if local_bastion.STATE == "listening":
                break
            await asyncio.sleep(0.05)
        check("with the directory: both sockets listening (a stale one replaced)",
              local_bastion.STATE == "listening"
              and stat.S_ISSOCK(os.lstat(local_bastion.enroll_socket()).st_mode)
              and stat.S_ISSOCK(os.lstat(local_bastion.lane_socket()).st_mode), local_bastion.STATE)
        secret, _ = await mint(pool)
        status, out = await http_unix(local_bastion.enroll_socket(), "POST", "/bastion/enroll/redeem",
                                      {"secret": secret, "operator_csr": make_csr()[1],
                                       "handle": HOME.handle})
        check("a redeem through the supervisor's socket works", status == 200
              and out.get("transport") == "unix", (status, out))
        for _ in range(100):
            if local_bastion.TCP_STATE == "listening":
                break
            await asyncio.sleep(0.05)
        secret, _ = await mint(pool)
        status, out = await http_tcp(local_bastion.ENROLL_PORT, "POST",
                                     local_bastion.TCP_REDEEM_PATH,
                                     {"secret": secret, "operator_csr": make_csr()[1],
                                      "handle": HOME.handle},
                                     ssl_ctx=client_ctx(await host_ca.ca_cert_pem(pool)),
                                     server_hostname=local_bastion.TCP_SERVER_NAME)
        check("...and so does one through the supervisor's enroll port", status == 200
              and out.get("transport") == "tcp", (status, out))
        try:
            local_bastion._take_socket_path(local_bastion.enroll_socket())
            check("a socket another server answers on is not taken over", False, "taken")
        except RuntimeError:
            check("a socket another server answers on is not taken over", True)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        check("on shutdown both sockets are removed",
              not os.path.exists(local_bastion.enroll_socket())
              and not os.path.exists(local_bastion.lane_socket()))
        check("...and both ports are closed",
              not _listening(local_bastion.ENROLL_PORT) and not _listening(local_bastion.LANE_PORT))
        plain = SHARED / "lane.sock"
        plain.write_text("not a socket")
        try:
            local_bastion._take_socket_path(str(plain))
            check("a path that is not a socket is never removed", False, "removed")
        except RuntimeError:
            check("a path that is not a socket is never removed", plain.exists())
        plain.unlink()
    finally:
        os.environ["PRIVATAE_HOST_DIR"] = real_dir
        local_bastion._control_pool = real_pool


# ── 8. the coder client's helper ─────────────────────────────────────────────

async def part_runs():
    print("== 8. the run-socket helper the coder client calls")
    from aiohttp import ClientSession, web
    rid = "run-0001abcd"
    path = local_bastion.run_socket_path(rid)
    check("run_socket_path is <runs_dir>/<run_id>.sock",
          path == os.path.join(local_bastion.runs_dir(), rid + ".sock"), path)
    for bad in ("../../lane", "short", "UPPERCASE-RUN-ID", "run-0001abcd/../x", "-leading-dash1"):
        try:
            local_bastion.run_socket_path(bad)
            check(f"run id {bad!r} refused", False, "accepted")
        except ValueError:
            check(f"run id {bad!r} refused", True)
    check("a run-up answer's control_socket names the same socket",
          local_bastion.run_socket_path(path) == path, path)
    for bad in (local_bastion.lane_socket(), str(TMP / "elsewhere" / (rid + ".sock")),
                os.path.join(local_bastion.runs_dir(), "sub", rid + ".sock"),
                os.path.join(local_bastion.runs_dir(), "..", rid + ".sock"),
                os.path.join(local_bastion.runs_dir(), rid)):
        try:
            local_bastion.run_socket_path(bad)
            check(f"control_socket {bad!r} refused", False, "accepted")
        except ValueError:
            check(f"control_socket {bad!r} refused", True)
    try:
        await local_bastion.open_run_connection("run-notthere1")
        check("a run with no socket: FileNotFoundError", False)
    except FileNotFoundError:
        check("a run with no socket: FileNotFoundError", True)

    async def health(_req):
        return web.json_response({"ok": True})
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, path)
    await site.start()
    own = local_bastion.RUN_SOCKET_OWNER_UID
    local_bastion.RUN_SOCKET_OWNER_UID = os.getuid()   # the bastion is root; this test may not be
    try:
        reader, writer = await local_bastion.open_run_connection(rid)
        writer.write(b"GET /health HTTP/1.1\r\nHost: run\r\nConnection: close\r\n\r\n")
        await writer.drain()
        resp = await reader.read()
        writer.close()
        check("open_run_connection reaches the run's control socket", b'"ok": true' in resp, resp[:80])
        reader, writer = await local_bastion.open_run_connection(path)
        writer.write(b"GET /health HTTP/1.1\r\nHost: run\r\nConnection: close\r\n\r\n")
        await writer.drain()
        resp = await reader.read()
        writer.close()
        check("...and so does the control_socket path from a run-up answer", b'"ok": true' in resp,
              resp[:80])
        async with ClientSession(connector=local_bastion.run_connector(rid)) as s:
            async with s.get("http://run/health") as r:
                got = await r.json()
        check("run_connector serves an aiohttp session on it", got == {"ok": True}, got)
        local_bastion.RUN_SOCKET_OWNER_UID = os.getuid() + 1
        try:
            await local_bastion.open_run_connection(rid)
            check("a run socket the bastion does not own is refused", False)
        except PermissionError:
            check("a run socket the bastion does not own is refused", True)
    finally:
        local_bastion.RUN_SOCKET_OWNER_UID = own
        await runner.cleanup()


# ── 9. the enroll and lane ports ─────────────────────────────────────────────

async def part_tcp():
    print("== 9. the enroll and lane ports (a bastion outside the Host's VM)")
    as_standalone()
    pool = FakePool()
    up = Upstream()
    up_srv = await asyncio.start_server(up.handle, "127.0.0.1", UP_PORT)
    enroll_port, lane_port = _free_port(), _free_port()
    os.environ["CSERIES_LANE_PORT"] = str(lane_port)
    name = "host.cseries.internal"  # the design's name, written out
    check("the ports' certificate name is the design's", local_bastion.TCP_SERVER_NAME == name,
          local_bastion.TCP_SERVER_NAME)
    enroll_ctx, _u = await host_ca.enroll_server_ssl_context(pool, [name])
    lane_ctx, _u = await host_ca.lane_server_ssl_context(pool, [name])
    enroll = await local_bastion.start_tcp_enroll_server(enroll_ctx, pool_getter=lambda: pool,
                                                         host="127.0.0.1", port=enroll_port)
    lane = await local_bastion.start_tcp_lane_server(lane_ctx, pool_getter=lambda: pool,
                                                     host="127.0.0.1", port=lane_port)
    ca_pem = await host_ca.ca_cert_pem(pool)
    try:
        # The enroll port's certificate, as the bastion checks it.
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", enroll_port, ssl=client_ctx(ca_pem), server_hostname=name)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
        leaf = x509.load_der_x509_certificate(der)
        ca = x509.load_pem_x509_certificate(ca_pem.encode())
        try:
            leaf.verify_directly_issued_by(ca)
            issued = True
        except Exception as e:
            issued = e
        sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        check(f"the enroll port's certificate verifies against the Host CA alone under {name}",
              issued is True and sans.get_values_for_type(x509.DNSName) == [name], (issued, sans))
        for label, ctx, sni in (("under another name", client_ctx(ca_pem), "home.cseries.localhost"),
                                ("against another CA", client_ctx(other_ca_operator_cert_ca()), name)):
            try:
                r, w = await asyncio.open_connection("127.0.0.1", enroll_port, ssl=ctx,
                                                     server_hostname=sni)
                w.close()
                check(f"...and does not verify {label}", False, "verified")
            except ssl.SSLCertVerificationError:
                check(f"...and does not verify {label}", True)

        # The redeem on the enroll port.
        secret, _minted = await mint(pool)
        bkey, csr = make_csr()
        body = {"secret": secret, "wg_public_key": "x" * 44, "operator_csr": csr,
                "hostname": "laptop", "platform": "darwin", "handle": HOME.handle}
        status, out = await http_tcp(enroll_port, "POST", local_bastion.TCP_REDEEM_PATH, body,
                                     ssl_ctx=client_ctx(ca_pem), server_hostname=name)
        check("POST /v1/local/redeem on the enroll port answers 200", status == 200, (status, out))
        out = out or {}
        check("the answer is transport tcp with the lane's address on the machine, no socket paths",
              out.get("transport") == "tcp" and out.get("lane_address") == f"127.0.0.1:{lane_port}"
              and not ({"lane_socket", "runs_dir"} & set(out))
              and {"operator_cert", "ca_cert", "bearer", "host_handle", "host_name"} <= set(out),
              sorted(out))
        row = pool.bearers[-1] if pool.bearers else None
        op_cert = x509.load_pem_x509_certificate(out["operator_cert"].encode())
        check("the bastion's row notes transport tcp and the operator certificate's serial",
              row is not None and json.loads(row[5]).get("transport") == "tcp"
              and json.loads(row[5]).get("cert_serial") == format(op_cert.serial_number, "x"),
              row and row[5])
        n_rows = len(pool.bearers)
        redeemed_id = n_rows  # FakePool row ids count from 1

        secret_exp, _m = await mint(pool)
        pool.tokens[hashlib.sha256(secret_exp.encode()).digest()]["expired"] = True
        for label, sec in (("a reused token", secret), ("a wrong token", "never-minted"),
                           ("an expired token", secret_exp)):
            status, refusal = await http_tcp(enroll_port, "POST", local_bastion.TCP_REDEEM_PATH,
                                             {**body, "secret": sec, "operator_csr": make_csr()[1]},
                                             ssl_ctx=client_ctx(ca_pem), server_hostname=name)
            check(f"{label} is refused 401 on the enroll port and mints nothing",
                  status == 401 and len(pool.bearers) == n_rows and (refusal or {}).get("error"),
                  (status, refusal))
        status, _o = await http_tcp(enroll_port, "POST", "/bastion/enroll/redeem", body,
                                    ssl_ctx=client_ctx(ca_pem), server_hostname=name)
        check("the enroll port serves /v1/local/redeem and no other path", status == 404, status)

        # The lane port.
        op = write_pair("tcp-bastion", out["operator_cert"], bkey)
        n = len(up.heads)
        status, got = await http_tcp(lane_port, "POST", "/api/network/bastions/heartbeat",
                                     {"beat": 1}, ssl_ctx=client_ctx(ca_pem, op),
                                     server_hostname=name,
                                     headers={"X-Client-Cert-CN": ROUTER})
        head = up.heads[-1] if len(up.heads) > n else b""
        from server.middleware import router_identity
        check("the redeemed certificate reaches the kernel through the lane port, as operator, "
              "with the front door's proof",
              status == 200 and got == {"kernel": "reached"}
              and header(head, b"x-client-cert-cn") == [b"operator"]
              and header(head, router_identity.PROOF_HEADER) == [router_identity.proof()],
              (status, got, head[:160]))

        async def lane_refuses(cert_key) -> bool:
            n = len(up.heads)
            try:
                await http_tcp(lane_port, "GET", "/api/system/health",
                               ssl_ctx=client_ctx(ca_pem, cert_key), server_hostname=name)
                reached = len(up.heads) > n
            except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError,
                    ValueError):
                reached = len(up.heads) > n
            return not reached

        check("a CN=operator certificate from another CA: refused, nothing reaches the kernel",
              await lane_refuses(write_pair("tcp-other", *other_ca_operator_cert())))
        stray_key, stray_csr = make_csr()
        stray_cert, _serial = await host_ca.sign_operator_csr(pool, stray_csr)
        check("a certificate under the Host CA that no redeem issued: refused, nothing reaches "
              "the kernel", await lane_refuses(write_pair("tcp-stray", stray_cert, stray_key)))
        check("no client certificate: refused, nothing reaches the kernel",
              await lane_refuses(None))
        # A lane already open when the bastion is revoked: the Host's own revoke
        # (nac_commit.revoke_bastion, every revoke path) closes it, as the
        # platform's revoke drops the bastion's WireGuard peer.
        held_r, held_w = await asyncio.open_connection(
            "127.0.0.1", lane_port, ssl=client_ctx(ca_pem, op), server_hostname=name)
        await asyncio.sleep(0.3)  # the door has looked the certificate up
        from server.network import bastion_enroll as be, nac_commit, topology
        real_rev, real_drop = topology.revoke_bastion, be.drop_concentrator_peer

        async def fake_revoke(pool_, eid, token_id):
            pool.revoked.add(token_id)
            return {"id": token_id, "handle": "x", "wg_bastion_pubkey": None, "ok": True}

        async def no_peer(_pub):
            return None
        topology.revoke_bastion, be.drop_concentrator_peer = fake_revoke, no_peer
        try:
            await nac_commit.revoke_bastion(pool, 1, redeemed_id)
        finally:
            topology.revoke_bastion, be.drop_concentrator_peer = real_rev, real_drop
        try:
            got = await asyncio.wait_for(held_r.read(1), 3)
            closed = got == b""
        except (asyncio.TimeoutError, ConnectionError, ssl.SSLError):
            closed = False
        held_w.close()
        check("a lane connection open when its bastion is revoked is closed by the revoke",
              closed, "still open after 3s")
        check("the redeemed certificate once its bastion is revoked: refused, nothing reaches "
              "the kernel", await lane_refuses(op))
        pool.revoked.clear()
        check("...and the check is the row, not the handshake: unrevoked, it passes again",
              not await lane_refuses(op))
    finally:
        os.environ.pop("CSERIES_LANE_PORT", None)
        enroll.close()
        lane.close()
        up_srv.close()


def other_ca_operator_cert_ca() -> str:
    """A CA certificate that is not this Host's."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "not this Host's CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


# ── 10. bastion pair-token ───────────────────────────────────────────────────

async def part_pair_token():
    print("== 10. python3 -m cli.cseries bastion pair-token")
    import contextlib
    import io
    import asyncpg
    import cli.cseries as cseries_cli
    pool = FakePool()

    async def fake_create_pool(dsn, **kw):
        return pool
    real = asyncpg.create_pool
    asyncpg.create_pool = fake_create_pool

    def run(argv):
        buf = io.StringIO()
        saved = sys.argv
        sys.argv = ["cseries", *argv]
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = cseries_cli.main()
        except SystemExit as e:  # argparse refusing the command line
            rc = e.code
        finally:
            sys.argv = saved
        return rc, buf.getvalue()
    try:
        os.environ.pop("CSERIES_ENROLL_PORT", None)
        rc, printed = await asyncio.to_thread(run, ["bastion", "pair-token"])
        try:
            got = json.loads(printed)
        except ValueError:
            got = None
        check("it prints one JSON object with exactly token, host_ca_pem and enroll",
              rc == 0 and isinstance(got, dict) and set(got) == {"token", "host_ca_pem", "enroll"},
              (rc, printed[:200]))
        got = got or {}
        check("enroll is the contract's URL", got.get("enroll") ==
              "https://127.0.0.1:8810/v1/local/redeem", got.get("enroll"))
        check("host_ca_pem is this Host's CA", got.get("host_ca_pem") == await host_ca.ca_cert_pem(pool))
        raw = (got.get("token") or "skb_")[len("skb_"):]
        raw += "=" * (-len(raw) % 4)
        handle, _, secret = base64.urlsafe_b64decode(raw).decode().partition(".")
        check("token is a pairing token the Host minted (15 minutes, single use) for its own handle",
              handle == HOME.handle
              and pool.tokens.get(hashlib.sha256(secret.encode()).digest(), {}).get("used") is False,
              (handle, len(pool.tokens)))
        os.environ["CSERIES_ENROLL_PORT"] = "18810"
        rc, printed = await asyncio.to_thread(run, ["bastion", "pair-token"])
        try:
            enroll = json.loads(printed).get("enroll")
        except ValueError:
            enroll = None
        check("enroll follows the port compose.yaml publishes it on",
              enroll == "https://127.0.0.1:18810/v1/local/redeem", (rc, printed[:200]))
    finally:
        os.environ.pop("CSERIES_ENROLL_PORT", None)
        asyncpg.create_pool = real


async def main():
    # A part that raises is one failed check, and the parts after it still run.
    async def guarded(fn, *a):
        try:
            return await fn(*a)
        except Exception as e:
            check(f"{fn.__name__} ran to the end", False, f"{type(e).__name__}: {e}")
            return None

    await guarded(part_ca)
    got = await guarded(part_sockets)
    if got:
        pool, out, _bkey, lane_head = got
        await guarded(part_kernel, pool, out, lane_head)
    for fn in (part_platform, part_member, part_supervisor, part_runs, part_tcp, part_pair_token):
        await guarded(fn)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    if FAILED:
        print(f"\nFAIL: {len(FAILED)} host-owns-bastion check(s) failed")
        sys.exit(1)
    print("\nOK: host-owns-bastion gates pass")
