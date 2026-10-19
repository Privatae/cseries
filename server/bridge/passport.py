"""The Host's network passport: the files that make it a member of the network.

A Host joins the network with a one-time code from its owner's account page
(server/bridge/membership.py). What it keeps lives under the state volume,
which compose mounts at /var/lib/cseries and which outlives the container:

    passport/host.key       the Host's private key, 0600; made here, never sent
    passport/host.crt       the passport certificate the network issued for it
    passport/chain.crt      that certificate followed by the issuing intermediate
    passport/root.crt       the root the network's doors are verified against
    passport/passport.json  host_uuid, entity_id, network, door, not_after,
                            renew_after, enrolled_at

Every file is written atomically (a temporary file in the same directory,
fsync, rename, fsync of the directory), key first, then the certificates,
then passport.json. passport.json is written last and removed first, so it is
the mark that the rest is complete: without it, or without a certificate and
a key that belong together, the Host is not a member.

A renewal makes a new key, and the old one must stay usable until the new
certificate is on disk, so the new key waits as host.key.next. The order is
host.key.next, chain.crt, host.crt, root.crt, then host.key.next renamed over
host.key (the old key is gone from that moment), then passport.json. If the
Host stops part way, load() finishes the swap: the key that matches the
newest certificate is the one kept.

No network and no database here.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger("cseries.passport")

PASSPORT_DIR = Path(os.environ.get("CSERIES_PASSPORT_DIR", "/var/lib/cseries/passport"))

KEY = "host.key"
NEXT_KEY = "host.key.next"
CERT = "host.crt"
CHAIN = "chain.crt"
ROOT = "root.crt"
META = "passport.json"

# A renewal is accepted from six days before expiry: the door issues seven-day
# passports and renew_after is one day after issue.
RENEW_BEFORE_EXPIRY = dt.timedelta(days=6)

# Enrolment codes: 12 characters of Crockford's base32 (no I, L, O or U),
# shown as XXXX-XXXX-XXXX. The door compares a hash of the canonical form.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_RE = re.compile(r"^[" + CODE_ALPHABET + r"]{12}$")

# Seams for the tests that cut a write short.
_replace = os.replace
_fsync = os.fsync


class PassportError(ValueError):
    """The network's answer cannot be used as a passport for this key."""


@dataclass(frozen=True)
class Passport:
    dir: Path
    host_uuid: str
    entity_id: int
    network: str
    door: str
    not_after: dt.datetime
    renew_after: dt.datetime
    enrolled_at: Optional[str]
    serial: str                      # lower-case hex, no leading zeros

    @property
    def key_path(self) -> Path:
        return self.dir / KEY

    @property
    def cert_path(self) -> Path:
        return self.dir / CERT

    @property
    def chain_path(self) -> Path:
        return self.dir / CHAIN

    @property
    def root_path(self) -> Path:
        return self.dir / ROOT


# ── codes ───────────────────────────────────────────────────────────────────

def canonical_code(code: str) -> str:
    """Upper case with every hyphen and space removed: what the door hashes."""
    return re.sub(r"[\s\-]", "", str(code or "")).upper()


def valid_code(canonical: str) -> bool:
    return bool(_CODE_RE.match(canonical))


# ── keys and requests ───────────────────────────────────────────────────────

def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def csr_for(key) -> str:
    """A CSR with an empty subject and no extensions. The door takes only the
    public key from it; the name, SANs, serial and lifetime are its own."""
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([]))
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def serial_hex(cert: x509.Certificate) -> str:
    return format(cert.serial_number, "x")


def _public_der(obj) -> bytes:
    return obj.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def _when(value) -> dt.datetime:
    t = dt.datetime.fromisoformat(str(value))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def check_issued(data: dict, key, *, enrolment: bool) -> dict:
    """What passport.json will hold, from a passport.enrol or passport.renew
    answer, after checking the certificate is for `key` and the chain starts
    with it. Raises PassportError; nothing is written."""
    if not isinstance(data, dict):
        raise PassportError("the answer is not an object")
    try:
        cert = x509.load_pem_x509_certificate(str(data["cert_pem"]).encode())
        chain = x509.load_pem_x509_certificates(str(data["chain_pem"]).encode())
    except (KeyError, ValueError) as e:
        raise PassportError(f"the certificate could not be read: {e}") from None
    if _public_der(cert) != _public_der(key):
        raise PassportError("the certificate is for another key")
    if not chain or chain[0] != cert:
        raise PassportError("the chain does not start with the certificate")
    info = {
        "network": str(data.get("network") or "privatae"),
        "door": str(data.get("door") or "net.privatae.ai:8643"),
        "not_after": cert.not_valid_after_utc.isoformat(),
    }
    try:
        info["renew_after"] = _when(data["renew_after"]).isoformat()
    except (KeyError, ValueError):
        info["renew_after"] = (cert.not_valid_after_utc - RENEW_BEFORE_EXPIRY).isoformat()
    if enrolment:
        try:
            info["host_uuid"] = str(data["host_uuid"])
            info["entity_id"] = int(data["entity_id"])
        except (KeyError, TypeError, ValueError):
            raise PassportError("the answer names no Host") from None
    return info


# ── files ───────────────────────────────────────────────────────────────────

def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """`path` holds either its old bytes or all of `data`, never part of it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            _fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _load_key(path: Path):
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None


def load(directory: Optional[Path] = None) -> Optional[Passport]:
    """The passport on disk, or None when this Host is not a member."""
    d = Path(directory or PASSPORT_DIR)
    try:
        meta = json.loads((d / META).read_text())
        chain = x509.load_pem_x509_certificates((d / CHAIN).read_bytes())
        host_crt = (d / CERT).read_bytes()
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not chain:
        return None
    leaf = chain[0]
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM)

    # The key that belongs to the newest certificate. host.key.next matching
    # it means a renewal stopped before the swap: finish it.
    current = _load_key(d / KEY)
    if current is None or _public_der(current) != _public_der(leaf):
        staged = _load_key(d / NEXT_KEY)
        if staged is None or _public_der(staged) != _public_der(leaf):
            logger.warning("passport: no key matches the certificate in %s; not a member", d)
            return None
        # host.crt is a copy of the leaf for reading; chain.crt and the key are
        # what the Host presents. A full disk that stops the copy must not
        # stop the swap, or the staged key stays the only key for chain.crt.
        if host_crt != leaf_pem:
            try:
                _atomic_write(d / CERT, leaf_pem)
            except OSError as e:
                logger.warning("passport: host.crt not rewritten (%s); the next load tries again", e)
        try:
            _replace(d / NEXT_KEY, d / KEY)
            _fsync_dir(d)
        except OSError as e:
            logger.warning("passport: could not finish an interrupted renewal: %s", e)
            return None
        logger.info("passport: finished an interrupted renewal (serial %s)", serial_hex(leaf))
    elif host_crt != leaf_pem:
        try:
            _atomic_write(d / CERT, leaf_pem)
        except OSError:
            pass

    not_after = leaf.not_valid_after_utc
    try:
        renew_after = _when(meta["renew_after"])
        if _when(meta.get("not_after")) != not_after:
            # passport.json is from before the last renewal: the certificate wins.
            renew_after = not_after - RENEW_BEFORE_EXPIRY
        return Passport(dir=d, host_uuid=str(meta["host_uuid"]), entity_id=int(meta["entity_id"]),
                        network=str(meta.get("network") or "privatae"),
                        door=str(meta.get("door") or "net.privatae.ai:8643"),
                        not_after=not_after, renew_after=renew_after,
                        enrolled_at=meta.get("enrolled_at"), serial=serial_hex(leaf))
    except (KeyError, TypeError, ValueError):
        return None


def _write_meta(d: Path, info: dict, enrolled_at: Optional[str]) -> None:
    meta = {k: info[k] for k in ("host_uuid", "entity_id", "network", "door",
                                 "not_after", "renew_after")}
    meta["enrolled_at"] = enrolled_at
    _atomic_write(d / META, (json.dumps(meta, indent=2) + "\n").encode())


def save_enrolment(directory: Optional[Path], *, key, cert_pem: str, chain_pem: str,
                   root_pem: bytes, info: dict) -> Passport:
    """A new membership's files. Any earlier passport.json goes first, so a
    stop part way can never pair old details with new certificates."""
    d = Path(directory or PASSPORT_DIR)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    _unlink(d / META)
    _unlink(d / NEXT_KEY)
    _fsync_dir(d)
    _atomic_write(d / KEY, key_pem(key), mode=0o600)
    _atomic_write(d / CHAIN, chain_pem.encode())
    _atomic_write(d / CERT, cert_pem.encode())
    _atomic_write(d / ROOT, root_pem)
    _write_meta(d, info, dt.datetime.now(dt.timezone.utc).isoformat())
    p = load(d)
    if p is None:
        raise PassportError("the passport did not read back")
    return p


def staged_key_in_use(directory: Optional[Path]) -> bool:
    """Whether host.key.next is the key for the certificate chain.crt holds: a
    renewal reached the disk and its key swap did not. That staged key is then
    the only key for what the Host presents."""
    d = Path(directory or PASSPORT_DIR)
    staged = _load_key(d / NEXT_KEY)
    if staged is None:
        return False
    try:
        chain = x509.load_pem_x509_certificates((d / CHAIN).read_bytes())
    except (OSError, ValueError):
        return False
    return bool(chain) and _public_der(staged) == _public_der(chain[0])


def stage_next_key(directory: Optional[Path], key) -> None:
    """Stage a renewal's key. Refuses to replace a staged key that the
    certificate on disk needs (staged_key_in_use)."""
    if staged_key_in_use(directory):
        raise PassportError("an earlier renewal's key is still staged for the "
                            "certificate on disk")
    _atomic_write(Path(directory or PASSPORT_DIR) / NEXT_KEY, key_pem(key), mode=0o600)


def discard_next_key(directory: Optional[Path]) -> None:
    _unlink(Path(directory or PASSPORT_DIR) / NEXT_KEY)


def save_renewal(directory: Optional[Path], *, cert_pem: str, chain_pem: str,
                 root_pem: bytes, info: dict, previous: Passport) -> Passport:
    """A renewed certificate for the key staged by stage_next_key. The old key
    is deleted only once the new certificate is on disk."""
    d = Path(directory or PASSPORT_DIR)
    _atomic_write(d / CHAIN, chain_pem.encode())
    _atomic_write(d / CERT, cert_pem.encode())
    _atomic_write(d / ROOT, root_pem)
    _replace(d / NEXT_KEY, d / KEY)
    _fsync_dir(d)
    merged = dict(info, host_uuid=previous.host_uuid, entity_id=previous.entity_id)
    _write_meta(d, merged, previous.enrolled_at)
    p = load(d)
    if p is None:
        raise PassportError("the renewed passport did not read back")
    return p


def delete(directory: Optional[Path] = None) -> None:
    """Remove the passport: passport.json first, so a stop part way reads as
    not a member."""
    d = Path(directory or PASSPORT_DIR)
    _unlink(d / META)
    _fsync_dir(d)
    for name in (KEY, NEXT_KEY, CERT, CHAIN, ROOT):
        _unlink(d / name)
    if d.is_dir():
        for leftover in d.glob(".*.tmp"):
            _unlink(leftover)
    _fsync_dir(d)
