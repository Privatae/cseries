"""A stand-in for the network's member door, on the test machine's side of the
tap, for the cloud VM test's member phase:

  python3 member_door.py <tap address> <dir> <code> <log> <address id> <relay address>

<dir> holds root.pem and root.key (a test root the Host is told to trust,
CSERIES_NETWORK_ROOT_CERT), door.pem and door.key (the door's certificate
under it, for enrol.privatae.ai and net.privatae.ai) and ticket.key (the
Ed25519 key remote-access tickets are signed with; the relay, relay.py, holds
its public half, ticket.pub). It listens on <tap address>:8643, the port both
names use, and speaks the doors' line protocol (one JSON object a line,
server/bridge/membership.py enrol and server/bridge/member_client.py):

  * passport.enrol, with no client certificate: the one-time <code> (once)
    gets a passport for the CSR, signed by the test root, valid 7 days, with
    an invented Host id and entity id; any other code, or the same one again,
    is refused as the real door refuses it;
  * every other op needs a client certificate the test root issued (a
    passport): heartbeat.ping is answered ok, and remote.enable and
    remote.ticket as the network's member door answers them (privatae
    bridge/member_remote.py, its tickets bridge/remote_ticket.py); anything
    else "unsupported".

remote.enable {acme_account_uri, reissue_acme_dns?} turns remote access on for
the passport's Host: the account must be one at Let's Encrypt (the real door's
default account prefix), and here only the one run-vm-test.sh invents for the
Host (TEST_ACCOUNT_URI; the real door takes any), and the Host's address ID is
<address id> (the real
door draws one once and keeps it). The answer is the real door's while the
platform's DNS automation makes the Host's records: dns "wanted", with no
acme-dns credential. The credential comes with the first answer after the
records exist, and with it the certificate job would ask Let's Encrypt for a
certificate, which needs real DNS and is not made in this test (the box's
remote certificate is a stand-in, as its own is).

remote.ticket {} answers with a ticket for the passport the connection came in
on (its serial, without the pad), the address ID and the zone, one relay,
tunnel.remote.privatae.ai, at <relay address>, the door's default limits and
no bandwidth ceiling, an hour long. A Host that has not turned remote access
on gets 409, as from the real door. The tickets are signed with the Host's own
remote_protocol.sign_ticket, which the network's door must match byte for
byte: at its start this door signs the pinned vector (remote_ticket_vector.json,
privatae bridge/tests/fixtures/remote_ticket_vector.json) and stops if the
bytes differ.

Each op is logged with whether a passport came with it, so the test can see
the Host joined, connected with its passport, and asked for remote access.
Every value here is invented for the test.
"""

import datetime as dt
import json
import re
import os
import socket
import ssl
import sys
import threading
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))
from server.network import remote_protocol as rp  # noqa: E402

ADDR, DIR, CODE, LOG, REMOTE_AID, RELAY_ADDR = sys.argv[1:7]
PORT = 8643
_lock = threading.Lock()
_used = set()

# The real door's settings (privatae bridge/remote_ticket.py RemoteConfig and
# its defaults), with this test's key id.
KID = "vmtest-1"
ZONE = "remote.privatae.ai"
TICKET_LIFETIME_S = 3600
LIMITS = {"streams": 512, "open_rate": 100, "open_burst": 200, "window": 256 * 1024, "bps": 10 ** 11}
ACME_ACCOUNT_PREFIX = "https://acme-v02.api.letsencrypt.org/acme/acct/"
# The invented account run-vm-test.sh writes for the Host (its acme account
# fixture). The real door takes any account at the CA; this stand-in takes only
# this one, so a Host that stopped reading the fixture, and asked the real CA
# for an account instead, fails the remote leg rather than passing it.
TEST_ACCOUNT_URI = ACME_ACCOUNT_PREFIX + "900000001"
_ACCOUNT_URI_RE = re.compile(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,=%-]+")
ERR_NOT_ENABLED = "remote access is not turned on for this Host; ask remote.enable first"
_remote = {}    # host uuid -> the ACME account URL it turned remote access on with


def note(line):
    with _lock, open(LOG, "a") as f:
        f.write(line + "\n")


def canonical(code):
    return "".join(c for c in str(code).upper() if c.isalnum())


with open(os.path.join(DIR, "root.pem"), "rb") as f:
    ROOT_PEM = f.read()
ROOT = x509.load_pem_x509_certificate(ROOT_PEM)
with open(os.path.join(DIR, "root.key"), "rb") as f:
    ROOT_KEY = serialization.load_pem_private_key(f.read(), password=None)


def issue(csr_pem, host_uuid):
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host_uuid)]))
            .issuer_name(ROOT.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(
                f"urn:privatae:host:{host_uuid}")]), critical=False)
            .sign(ROOT_KEY, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def check_vector():
    """The pinned ticket, signed here as this door signs: the same bytes or
    no door."""
    with open(os.path.join(HERE, "remote_ticket_vector.json")) as f:
        v = json.load(f)
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v["ed25519_seed_hex"]))
    if rp.sign_ticket(key, v["payload"]) != v["ticket"]:
        sys.exit("member_door: remote_protocol.sign_ticket no longer makes the pinned ticket")


with open(os.path.join(DIR, "ticket.key"), "rb") as f:
    TICKET_KEY = serialization.load_pem_private_key(f.read(), password=None)


def names(aid):
    zone = f"{aid}.{ZONE}"
    return {"zone": ZONE, "host_zone": zone, "wildcard": f"*.{zone}",
            "challenge": f"_acme-challenge.{zone}", "tunnel": rp.tunnel_name(ZONE)}


def remote_enable(out, params, host_uuid):
    uri = params.get("acme_account_uri")
    if (not isinstance(uri, str) or len(uri) > 512 or not _ACCOUNT_URI_RE.fullmatch(uri) or ";" in uri
            or not uri.startswith(ACME_ACCOUNT_PREFIX) or len(uri) == len(ACME_ACCOUNT_PREFIX)):
        note("remote.enable refused: account")
        return {**out, "ok": False, "error": "acme_account_uri is not an account at the CA this zone allows",
                "code": 400}
    if uri != TEST_ACCOUNT_URI:
        note("remote.enable refused: not the test's own ACME account")
        return {**out, "ok": False, "error": "acme_account_uri is not the test's own account", "code": 400}
    if not isinstance(params.get("reissue_acme_dns", False), bool):
        return {**out, "ok": False, "error": "reissue_acme_dns must be true or false", "code": 400}
    with _lock:
        first = host_uuid not in _remote
        _remote[host_uuid] = uri
    note(f"remote.enable ok address_id={REMOTE_AID} first={first} dns=wanted")
    return {**out, "ok": True, "data": {"address_id": REMOTE_AID, "names": names(REMOTE_AID), "dns": "wanted",
                                        "acme_account_uri": uri, "acme_dns_delivered": False}}


def remote_ticket(out, host_uuid, serial):
    with _lock:
        enabled = host_uuid in _remote
    if not enabled:
        note("remote.ticket refused: not enabled")
        return {**out, "ok": False, "error": ERR_NOT_ENABLED, "code": 409}
    iat = int(dt.datetime.now(dt.timezone.utc).timestamp())
    relays = [{"name": rp.tunnel_name(ZONE), "addrs": [RELAY_ADDR]}]
    payload = {"v": 1, "kid": KID, "host_uuid": host_uuid, "serial": format(serial, "x"),
               "address_id": REMOTE_AID, "zone": ZONE, "iat": iat, "exp": iat + TICKET_LIFETIME_S,
               "relays": relays, "limits": dict(LIMITS)}
    note(f"remote.ticket ok address_id={REMOTE_AID}")
    return {**out, "ok": True, "data": {"ticket": rp.sign_ticket(TICKET_KEY, payload), "exp": payload["exp"],
                                        "address_id": REMOTE_AID, "zone": ZONE, "relays": relays,
                                        "limits": dict(LIMITS)}}


def answer(msg, passport):
    op = msg.get("op")
    params = msg.get("params") or {}
    out = {"id": msg["id"]} if "id" in msg else {}
    if op == "passport.enrol":
        code = canonical(params.get("code"))
        if code != canonical(CODE) or code in _used:
            note(f"enrol refused code_ok={code == canonical(CODE)}")
            return {**out, "ok": False, "error": "This code is not valid or was already used.", "code": 403}
        _used.add(code)
        host_uuid = str(uuid.uuid4())
        cert = issue(str(params.get("csr_pem") or ""), host_uuid)
        note(f"enrol ok host_name={params.get('host_name')!r}")
        return {**out, "ok": True, "data": {
            "cert_pem": cert, "chain_pem": cert + ROOT_PEM.decode(), "root_pem": ROOT_PEM.decode(),
            "host_uuid": host_uuid, "entity_id": 4242, "network": "privatae", "door": "net.privatae.ai:8643"}}
    if passport is None:
        note(f"op {op} refused: no passport")
        return {**out, "ok": False, "error": "a passport is needed", "code": 401}
    note(f"op {op} passport=yes")
    if op == "heartbeat.ping":
        return {**out, "ok": True, "data": {}}
    if op in ("remote.enable", "remote.ticket") and not isinstance(msg.get("params", {}), dict):
        return {**out, "ok": False, "error": f"params for {op} must be an object", "code": 400}
    host_uuid = passport.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    if op == "remote.enable":
        return remote_enable(out, params, host_uuid)
    if op == "remote.ticket":
        return remote_ticket(out, host_uuid, passport.serial_number)
    return {**out, "ok": False, "error": "unsupported", "code": 400}


def serve(conn):
    try:
        der = conn.getpeercert(binary_form=True)
        passport = x509.load_der_x509_certificate(der) if der else None
        f = conn.makefile("rwb")
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                break
            if not isinstance(msg, dict):
                break
            f.write(json.dumps(answer(msg, passport)).encode() + b"\n")
            f.flush()
    except (OSError, ssl.SSLError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


check_vector()
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version = ssl.TLSVersion.TLSv1_2
ctx.load_cert_chain(os.path.join(DIR, "door.pem"), os.path.join(DIR, "door.key"))
ctx.load_verify_locations(cafile=os.path.join(DIR, "root.pem"))
ctx.verify_mode = ssl.CERT_OPTIONAL
srv = socket.create_server((ADDR, PORT))
note(f"listening {ADDR}:{PORT}")
while True:
    raw, _peer = srv.accept()
    try:
        conn = ctx.wrap_socket(raw, server_side=True)
    except (OSError, ssl.SSLError) as e:
        note(f"handshake failed: {type(e).__name__}")
        raw.close()
        continue
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
