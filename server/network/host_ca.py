"""The Host's own certificate authority, for a Host with no platform.

A Host on the platform has its bastion's operator certificate signed by the
platform CA (the bridge's bastion.enroll). A standalone Host has no platform,
so it signs with a CA of its own. That CA is made once, the first time it is
needed, and kept where the Host keeps its other keys: kernel_secrets, beside
the entity signing key (server/applications/entity_signing.py) and the vault
identity key (server/vault/identity.py). Nothing in this module writes it
anywhere else, and no function here returns it.

It signs exactly two kinds of certificate:
  - a bastion's operator certificate, from the CSR the bastion sent at redeem
    (sign_operator_csr). The request is checked the way the platform checks
    it: subject CN=operator and nothing else, at most the device fingerprint
    as URI SANs (ours, and the same fingerprint under one other name). The
    certificate is built from those checked values, never
    copied from the request, so a bastion cannot ask for another identity.
  - the Host's own server certificates for its lanes and its enroll port
    (lane_server_ssl_context, enroll_server_ssl_context), each on a fresh key
    made in memory at every start.

The key is ECDSA P-256: the bastion's own operator key is P-256
(client-go/pair.go genOperatorCSR), its baseline signature check requires an
EC key (client-go/baselinesign.go), Go's crypto/tls and x509 verify P-256
chains on every version, and so does the OpenSSL behind Python's ssl.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import secrets
import ssl
import tempfile
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from server.applications.tokens import _SECRET_SCHEMA

# kernel_secrets id. The value is the CA's private key (PKCS#8 PEM) followed
# by its certificate (PEM), in one row so the two can never disagree.
KEY_ID = "host_ca"

OPERATOR_CN = "operator"           # client-go/pair.go; main._enforce_bastion_binding
FP_SAN_PREFIX = "urn:privatae:hwfp:"  # bastion_enroll._FP_SAN_PREFIX
# The bastion names its fingerprint under two URNs, the platform's first and
# this one second (client-go/pair.go fpURIs), so one CSR serves both. Any
# urn:<name>:hwfp:<fp> may ride beside ours if it names the same fingerprint;
# the certificate carries ours only.
_ANY_FP_URI = re.compile(r"urn:[a-z][a-z0-9-]{0,31}:hwfp:([A-Za-z0-9_-]{16,128})")
_ONE_CSR_BLOCK = re.compile(r"\s*-----BEGIN CERTIFICATE REQUEST-----\r?\n"
                            r"[A-Za-z0-9+/=\r\n]+"
                            r"-----END CERTIFICATE REQUEST-----\s*")
_EXTENSION_REQUEST_OID = "1.2.840.113549.1.9.14"

# The bastion pins this CA at pairing and refuses any other later, so it has
# to outlive every pairing made under it.
CA_DAYS = 20 * 365
# The platform issues operator certificates for a year (bridge _GATEWAY_CERT_DAYS).
OPERATOR_DAYS = 365
# The lane certificate is re-made at every start and renewed while running.
LANE_DAYS = 30
LANE_RENEW_BEFORE = datetime.timedelta(days=10)


class CSRRefused(ValueError):
    """The request is not a bastion's own operator CSR."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _key_usage(*, sign: bool, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=sign, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=ca,
        crl_sign=ca, encipher_only=False, decipher_only=False)


def _new_ca() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "C-Series Host"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Host CA " + secrets.token_hex(4)),
    ])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(sign=False, ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
            + cert.public_bytes(serialization.Encoding.PEM))


async def _stored(pool) -> bytes:
    """Generate-or-fetch the CA row. Write-once, the shape of
    hub_identity.ensure_hub_wg_key: two first callers can both generate, the
    INSERT keeps the first, and both return what was stored."""
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id = $1", KEY_ID)
        if row is None:
            await conn.execute(
                "INSERT INTO kernel_secrets (id, secret) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                KEY_ID, _new_ca())
            row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id = $1", KEY_ID)
    return bytes(row["secret"])


async def _load(pool) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    raw = await _stored(pool)
    cut = raw.index(b"-----BEGIN CERTIFICATE-----")
    key = serialization.load_pem_private_key(raw[:cut], password=None)
    cert = x509.load_pem_x509_certificate(raw[cut:])
    return key, cert


async def ensure(pool) -> None:
    """Make the CA if this Host has none yet (first boot)."""
    await _stored(pool)


async def ca_cert_pem(pool) -> str:
    """The CA certificate, PEM: what a bastion pins and what the lane trusts."""
    _, cert = await _load(pool)
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def ca_sha256(ca_pem: str) -> str:
    """The SHA-256 of a CA certificate's DER, in lowercase hex: what a pairing
    code carries (bastion_enroll.pairing_code), so a bastion can take the CA
    from the enroll port's chain and refuse any other."""
    der = x509.load_pem_x509_certificate(ca_pem.encode()).public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def checked_operator_csr(csr_pem: str) -> tuple[x509.CertificateSigningRequest, Optional[str]]:
    """(the CSR, its fingerprint or None) when this is a bastion's own operator
    CSR; raises CSRRefused naming why not. The same checks the platform makes
    before it signs (bridge _checked_operator_csr)."""
    csr_pem = csr_pem or ""
    if csr_pem.count("-----BEGIN") != 1 or not _ONE_CSR_BLOCK.fullmatch(csr_pem):
        raise CSRRefused("operator_csr must be exactly one CERTIFICATE REQUEST block")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except Exception:
        raise CSRRefused("operator_csr is not a certificate request") from None
    try:
        signed = csr.is_signature_valid
    except Exception:
        signed = False
    if not signed:
        raise CSRRefused("operator_csr is not signed by its own key")
    try:
        subject = list(csr.subject)
        subject_ok = (len(subject) == 1 and subject[0].oid == NameOID.COMMON_NAME
                      and subject[0].value == OPERATOR_CN)
    except Exception:
        subject_ok = False
    if not subject_ok:
        raise CSRRefused(f"operator_csr subject must be CN={OPERATOR_CN} and nothing else")
    try:
        attributes = list(csr.attributes)
        extensions = list(csr.extensions)
    except Exception:
        raise CSRRefused("operator_csr has an attribute that cannot be read") from None
    if len(attributes) > 1 or any(a.oid.dotted_string != _EXTENSION_REQUEST_OID
                                  for a in attributes):
        raise CSRRefused("operator_csr may carry one extension request and nothing else")
    fp = None
    for ext in extensions:
        if not isinstance(ext.value, x509.SubjectAlternativeName):
            raise CSRRefused("operator_csr asks for an extension this Host does not sign "
                             f"({ext.oid.dotted_string})")
        names = list(ext.value)
        found = [_ANY_FP_URI.fullmatch(n.value) if isinstance(n, x509.UniformResourceIdentifier)
                 else None for n in names]
        if len(names) > 2 or not all(found):
            raise CSRRefused("operator_csr may name only its device fingerprint "
                             f"({FP_SAN_PREFIX}<fp>)")
        ours = [n.value[len(FP_SAN_PREFIX):] for n in names if n.value.startswith(FP_SAN_PREFIX)]
        if names and len(ours) != 1:
            raise CSRRefused(f"operator_csr must name its device fingerprint once as {FP_SAN_PREFIX}<fp>")
        if len({m.group(1) for m in found}) > 1:
            raise CSRRefused("operator_csr names two different device fingerprints")
        if ours:
            fp = ours[0]
    pub = csr.public_key()
    ec_ok = (isinstance(pub, ec.EllipticCurvePublicKey)
             and isinstance(pub.curve, (ec.SECP256R1, ec.SECP384R1)))
    if not ec_ok and not isinstance(pub, ed25519.Ed25519PublicKey):
        raise CSRRefused("operator_csr key must be ECDSA P-256/P-384 or Ed25519")
    return csr, fp


async def sign_operator_csr(pool, csr_pem: str) -> tuple[str, str]:
    """(operator certificate PEM, serial hex) for a checked operator CSR.

    The certificate is built from the checked values only: CN=operator, the
    fingerprint SAN when the request had one, client auth and nothing else."""
    csr, fp = checked_operator_csr(csr_pem)
    key, ca = await _load(pool)
    now = _now()
    b = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, OPERATOR_CN)]))
        .issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=OPERATOR_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(sign=True, ca=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                       critical=False)
    )
    if fp:
        b = b.add_extension(x509.SubjectAlternativeName(
            [x509.UniformResourceIdentifier(FP_SAN_PREFIX + fp)]), critical=False)
    cert = b.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM).decode(), format(cert.serial_number, "x")


async def issue_lane_server_cert(pool, names: list[str]) -> tuple[bytes, bytes, datetime.datetime]:
    """(cert PEM, key PEM, not_after) for the lane's server side, on a key made
    here and never stored. `names` are the DNS names a bastion may check."""
    if not names:
        raise ValueError("the lane certificate needs at least one name")
    from server.middleware.localhost_only import ROUTER_CN
    if any(n in (ROUTER_CN, OPERATOR_CN) for n in names):
        # Either name is an identity the kernel trusts on a client cert.
        raise ValueError("the lane certificate may not carry the router's or a bastion's name")
    ca_key, ca = await _load(pool)
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    not_after = now + datetime.timedelta(days=LANE_DAYS)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(sign=True, ca=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM), key_pem, not_after


def load_server_chain(ctx: ssl.SSLContext, cert_pem: bytes, key_pem: bytes) -> None:
    """Put a server cert and key into `ctx`. `cert_pem` is the leaf, followed
    by any certificates the chain carries after it.

    Python's ssl reads a key only from a file, and the container's seccomp
    profile refuses memfd_create, so the pair goes through a 0600 file in a
    0700 directory under /tmp (a tmpfs in compose.yaml) and is removed as soon
    as OpenSSL has read it. This is the lane's own leaf key, re-made at every
    start; the CA key never comes here."""
    d = tempfile.mkdtemp(prefix="lane-")
    path = os.path.join(d, "chain.pem")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(cert_pem + key_pem)
        ctx.load_cert_chain(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        os.rmdir(d)


async def lane_server_ssl_context(pool, names: list[str]) -> tuple[ssl.SSLContext, datetime.datetime]:
    """(server context, when its certificate ends) for a lane, on lane.sock or
    the lane port: a certificate from this CA, and client certificates
    verified against this CA and nothing else (no system roots, no platform
    CA)."""
    cert_pem, key_pem, not_after = await issue_lane_server_cert(pool, names)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    load_server_chain(ctx, cert_pem, key_pem)
    ctx.load_verify_locations(cadata=await ca_cert_pem(pool))
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx, not_after


async def enroll_server_ssl_context(pool, names: list[str]) -> tuple[ssl.SSLContext, datetime.datetime]:
    """(server context, when its certificate ends) for the enroll port: a
    certificate from this CA, which the bastion verifies against the CA it was
    handed with the token. No client certificate: the bastion has none yet,
    and the pairing token is what authenticates the redeem.

    The chain carries the CA after the leaf. A bastion paired with a pairing
    code holds only the CA's SHA-256 (bastion_enroll.pairing_code), takes the
    CA from this chain and refuses one whose SHA-256 differs, so the person
    has no certificate file to save. The lanes send the leaf alone: by then
    the bastion has the CA pinned."""
    cert_pem, key_pem, not_after = await issue_lane_server_cert(pool, names)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    load_server_chain(ctx, cert_pem + (await ca_cert_pem(pool)).encode(), key_pem)
    return ctx, not_after


async def renew_lane_cert(pool, ctx: ssl.SSLContext, names: list[str], *,
                          chain_ca: bool = False) -> datetime.datetime:
    """Replace the lane certificate in a live context. New handshakes get the
    new one; connections already open keep theirs. `chain_ca` is the enroll
    port's: its chain carries the CA after the leaf (enroll_server_ssl_context)."""
    cert_pem, key_pem, not_after = await issue_lane_server_cert(pool, names)
    if chain_ca:
        cert_pem += (await ca_cert_pem(pool)).encode()
    load_server_chain(ctx, cert_pem, key_pem)
    return not_after
