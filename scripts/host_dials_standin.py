"""The stand-ins scripts/test_host_dials.py and scripts/e2e_network_add_server.py
share: a stub pool (the Host CA, the bastion rows, the tickets) and a bastion
that speaks the pairing window's wire contract (the bastion's
docs/host-connects.md, pair_window.go serveWindow) as the TLS client on each
connection the Host opens to it. Not a gate itself (no test_ prefix)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from server.network import bastion_enroll, host_ca, host_dials, local_bastion


# ── a stub pool: the CA, the bastion rows, the tickets ──────────────────────

class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *a):
        if sql.lstrip().startswith("INSERT INTO kernel_secrets"):
            self.pool.secrets.setdefault(a[0], a[1])

    async def fetchrow(self, sql, *a):
        if "FROM kernel_secrets" in sql:
            v = self.pool.secrets.get(a[0])
            return None if v is None else {"secret": v}
        raise AssertionError(sql)


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class Pool:
    def __init__(self):
        self.secrets = {}
        self.rows = []          # INSERTed mcp_client_tokens: dict
        self.tickets = {}       # id -> metadata
        self.executed = []

    def acquire(self):
        return _Acq(self)

    async def fetchrow(self, sql, *a):
        if "INSERT INTO mcp_client_tokens" in sql:
            self.rows.append({"id": len(self.rows) + 1, "entity_id": a[0], "name": a[1],
                              "notes": json.loads(a[5]), "fp": a[6], "revoked": False})
            return {"id": len(self.rows)}
        if "notes->>'cert_serial' = $2" in sql:
            for r in self.rows:
                if r["notes"].get("cert_serial") == a[1] and not r["revoked"]:
                    return {"id": r["id"], "name": r["name"]}
            return None
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if "UPDATE mcp_client_tokens SET revoked_at" in sql:
            out = []
            for r in self.rows:
                if (r["notes"].get("transport") == a[1] and r["notes"].get("door") == a[2]
                        and r["id"] != a[3] and not r["revoked"]):
                    r["revoked"] = True
                    out.append({"id": r["id"]})
            return out
        if "notes->>'transport' = $2" in sql:
            return [{"id": r["id"], "door": r["notes"].get("door"), "serial": r["notes"].get("cert_serial")}
                    for r in self.rows if r["notes"].get("transport") == a[1] and not r["revoked"]]
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        if "UPDATE mcp_client_tokens SET revoked_at" in sql:
            for r in self.rows:
                if r["id"] == a[0]:
                    r["revoked"] = True
            return "UPDATE 1"
        if "UPDATE entity_tickets SET metadata = jsonb_set" in sql:
            md = self.tickets.get(a[0])
            if md is not None and a[2] in md:
                md[a[2]]["code"] = None
            return "UPDATE 1"
        raise AssertionError(sql)


async def _handle(entity_id):
    return "home"
bastion_enroll.resolve_self_handle = _handle

CODE = "S12AW-KP3HC-FJTG7-HAS9C"
NORM = "S12AWKP3HCFJTG7HAS9C"
FP = "abcdefghijklmnopqrstuvwxyz012345"


def make_csr():
    key = ec.generate_private_key(ec.SECP256R1())
    b = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "operator")])).add_extension(
        x509.SubjectAlternativeName([x509.UniformResourceIdentifier(host_ca.FP_SAN_PREFIX + FP)]), critical=False)
    csr = b.sign(key, hashes.SHA256())
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


async def client_tls(writer, ctx, sni):
    """TLS as the CLIENT on a connection this side accepted: the bastion is
    the TLS client on the connections the Host opens to it."""
    loop = asyncio.get_running_loop()
    proto = writer._protocol
    await writer.drain()
    nt = await loop.start_tls(writer._transport, proto, ctx, server_side=False, server_hostname=sni)
    writer._transport = nt
    proto._replace_transport(nt)


async def _http(reader, writer, path, body):
    raw = json.dumps(body).encode()
    writer.write(f"POST {path} HTTP/1.1\r\nHost: host.cseries.internal\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    n = 0
    for ln in head.split(b"\r\n")[1:]:
        k, _, v = ln.partition(b":")
        if k.strip().lower() == b"content-length":
            n = int(v.strip())
        if k.strip().lower() == b"connection" and v.strip().lower() == b"close":
            raise AssertionError("the Host closed the kept-alive pairing connection")
    return status, json.loads(await reader.readexactly(n) or b"{}")


class StandinBastion:
    """The bastion's side of a pairing window (pair_window.go serveWindow), as a
    TLS client on each connection the Host opens to it."""

    def __init__(self, code=NORM, *, offer=(host_dials.LANE_ALPN, host_dials.PAIR_ALPN),
                 bad_proof=False, close_after_hello=False, done=None, hang_before_done=False,
                 refuse_before_redeem=None):
        self.code, self.offer, self.bad_proof = code, offer, bad_proof
        self.hang_before_done = hang_before_done
        self.close_after_hello, self.done = close_after_hello, done or {"v": 1, "state": "active"}
        # pair_window.go windowCommit refusing before the redeem (this Host is
        # already paired here): done carries the refusal, with no redeem.
        self.refuse_before_redeem = refuse_before_redeem
        self.seen: dict = {}
        self.finished = asyncio.Event()

    async def serve(self, reader, writer):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE         # the CA comes from the chain, then pinned
            ctx.set_alpn_protocols(list(self.offer))
            await client_tls(writer, ctx, local_bastion.TCP_SERVER_NAME)
            so = writer.get_extra_info("ssl_object")
            self.seen["alpn"] = so.selected_alpn_protocol()
            chain = so.get_unverified_chain()
            self.seen["chain_len"] = len(chain)
            leaf_der, ca_der = chain[0], (chain[1] if len(chain) > 1 else b"")
            self.seen["ca_der"] = ca_der
            leaf = hashlib.sha256(leaf_der).digest()
            ca = hashlib.sha256(ca_der).digest()
            ca_cert = x509.load_der_x509_certificate(ca_der)
            x509.load_der_x509_certificate(leaf_der).verify_directly_issued_by(ca_cert)
            self.seen["leaf_names"] = x509.load_der_x509_certificate(leaf_der).extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            nb = os.urandom(32)
            st, hello = await _http(reader, writer, "/v1/pair/hello", {"v": 1, "nonce": nb.hex()})
            self.seen["hello"] = (st, hello)
            nh = bytes.fromhex(hello["nonce"])
            want = host_dials.proof(self.code, "host", leaf, ca, nb, nh)
            self.seen["host_proved"] = hmac.compare_digest(want, hello.get("proof", ""))
            if self.close_after_hello or not self.seen["host_proved"]:
                writer.close()
                return
            if self.refuse_before_redeem:
                st, _ = await _http(reader, writer, "/v1/pair/done",
                                    {"v": 1, "state": "refused", "error": self.refuse_before_redeem})
                self.seen["done_status"] = st
                writer.close()
                return
            key, csr = make_csr()
            bp = host_dials.proof("0" * 20 if self.bad_proof else self.code, "bastion", leaf, ca, nb, nh)
            st, rr = await _http(reader, writer, "/v1/pair/redeem", {
                "v": 1, "proof": bp, "wg_public_key": "x", "operator_csr": csr,
                "hostname": "far-server", "platform": "linux", "handle": hello["host_handle"]})
            self.seen["redeem"] = (st, rr)
            if st != 200:
                writer.close()
                return
            self.seen["op_key"] = key
            if self.hang_before_done:
                await reader.read()               # until the Host gives up
                return
            st, _ = await _http(reader, writer, "/v1/pair/done", self.done)
            self.seen["done_status"] = st
            writer.close()
        except Exception as e:  # noqa: BLE001
            self.seen["error"] = repr(e)
        finally:
            self.finished.set()


