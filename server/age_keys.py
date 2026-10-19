"""
age keys, in age's own text encoding: the one definition in this tree.

age names an X25519 key pair in bech32 (BIP-173):
  recipient (public):  bech32("age", 32 raw bytes)              -> "age1..."
  identity (private):  bech32("age-secret-key-", 32 raw bytes)  -> "AGE-SECRET-KEY-1..."
                       (upper-cased, as age-keygen writes it)

The vault's keys (server/vault/crypto.py) and the user's backup key are X25519
too, so the same 32 bytes are an age key with no conversion beyond this text
form. The Host's backups (server/host_backup.py), its exports
(server/host_export.py) and the bastion catalog seal (server/network/nac_dcn.py)
all name keys through here. Tests prove every function round-trips through the
real `age` binary (scripts/test_host_backup.py).
"""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
RECIPIENT_HRP = "age"
IDENTITY_HRP = "age-secret-key-"


def _polymod(values) -> int:
    gen = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _to5(data: bytes) -> list[int]:
    acc = bits = 0
    out = []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def _from5(data: list[int]) -> bytes:
    acc = bits = 0
    out = bytearray()
    for v in data:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xff)
    if bits >= 5 or (acc & ((1 << bits) - 1)):
        raise ValueError("invalid bech32 padding")
    return bytes(out)


def _encode(hrp: str, raw: bytes) -> str:
    data = _to5(raw)
    poly = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def _decode(hrp: str, text: str) -> bytes:
    s = (text or "").strip().lower()
    if not s.startswith(hrp + "1"):
        raise ValueError(f"not an age {hrp!r} string")
    body = s[len(hrp) + 1:]
    if len(body) < 7 or any(c not in _CHARSET for c in body):
        raise ValueError("invalid bech32 characters")
    values = [_CHARSET.index(c) for c in body]
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("bech32 checksum does not match")
    raw = _from5(values[:-6])
    if len(raw) != 32:
        raise ValueError("an age key is 32 bytes")
    return raw


def recipient(pub32: bytes) -> str:
    """The age recipient ("age1...") for a raw X25519 public key."""
    if len(pub32) != 32:
        raise ValueError("X25519 public key must be 32 bytes")
    return _encode(RECIPIENT_HRP, pub32)


def recipient_bytes(text: str) -> bytes:
    """The raw public key of an "age1..." recipient; raises on anything else."""
    return _decode(RECIPIENT_HRP, text)


def valid_recipient(text: str) -> bool:
    try:
        recipient_bytes(text)
        return True
    except ValueError:
        return False


def identity(priv32: bytes) -> str:
    """The age identity ("AGE-SECRET-KEY-1...") for a raw X25519 private key."""
    if len(priv32) != 32:
        raise ValueError("X25519 private key must be 32 bytes")
    return _encode(IDENTITY_HRP, priv32).upper()


def identity_bytes(text: str) -> bytes:
    return _decode(IDENTITY_HRP, text)


def recipient_of_identity(text: str) -> str:
    """The recipient that an identity opens."""
    priv = X25519PrivateKey.from_private_bytes(identity_bytes(text))
    return recipient(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def new_pair() -> tuple[str, str]:
    """A fresh key pair as (identity, recipient)."""
    priv = X25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    return identity(raw), recipient(pub)


def identity_from_pem(priv_pem: bytes) -> str:
    """The age identity of a vault key held as PKCS8 PEM (the Host's own
    `vault_host_x25519`, server/vault/identity.py)."""
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    if not isinstance(priv, X25519PrivateKey):
        raise TypeError("not an X25519 private key")
    return identity(priv.private_bytes(serialization.Encoding.Raw,
                                       serialization.PrivateFormat.Raw,
                                       serialization.NoEncryption()))
