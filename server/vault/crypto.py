"""
Credential vault — envelope crypto (in-house, age-style sealed envelopes).

A credential is sealed into an ENVELOPE: the plaintext is encrypted once with a
random per-secret data key (DEK, AES-256-GCM), and that DEK is wrapped to one or
more recipient identities' X25519 public keys. Only a holder of a recipient's
X25519 private key can unwrap the DEK and decrypt. The store keeps ciphertext +
public keys only, so whoever holds the store cannot read the secret.

This mirrors age's "file key wrapped per recipient":
  - multi-recipient is free (owner + an opt-in, user-controlled break-glass key);
  - KEK rotation = re-wrap the DEK to a new key WITHOUT re-encrypting the payload.

Wire format (envelope dict — JSON/JSONB-safe, binary fields base64):
  {
    "v": 1,
    "payload":   {"nonce": b64, "ct": b64},          # AES-256-GCM(DEK, plaintext)
    "recipients":[{"epk": b64, "nonce": b64, "wrapped": b64}, ...]
  }                                                   # per recipient: X25519 eph pub + AES-256-GCM(KEK, DEK)

Primitives (all from `cryptography`, available in-pod and platform-side):
  X25519 ECDH -> HKDF-SHA256 -> AES-256-GCM. No custom crypto.
"""
from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_HKDF_INFO = b"privatae-vault-v1 dek-wrap"
_DEK_BYTES = 32  # AES-256


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def gen_identity_keypair() -> tuple[bytes, bytes]:
    """A vault identity keypair (X25519). Returns (private_pem_pkcs8, public_raw_32)."""
    priv = X25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_pem, pub_raw


def pub_from_priv(priv_pem: bytes) -> bytes:
    """Raw 32-byte X25519 public key for a PKCS8 PEM private key."""
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    if not isinstance(priv, X25519PrivateKey):
        raise TypeError("not an X25519 private key")
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _wrap_dek(dek: bytes, recipient_pub_raw: bytes) -> dict[str, str]:
    """Wrap the DEK to one recipient X25519 public key (ephemeral-static ECDH)."""
    recipient_pub = X25519PublicKey.from_public_bytes(recipient_pub_raw)
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(recipient_pub)
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared)
    nonce = os.urandom(12)
    wrapped = AESGCM(kek).encrypt(nonce, dek, None)
    epk = eph.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {"epk": _b64(epk), "nonce": _b64(nonce), "wrapped": _b64(wrapped)}


def _unwrap_dek(stanza: dict[str, str], priv: X25519PrivateKey) -> bytes | None:
    """Try to recover the DEK from one recipient stanza with our private key.
    Returns None if this stanza isn't for us (GCM auth fails) or is malformed."""
    try:
        epk = X25519PublicKey.from_public_bytes(_unb64(stanza["epk"]))
        shared = priv.exchange(epk)
        kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared)
        return AESGCM(kek).decrypt(_unb64(stanza["nonce"]), _unb64(stanza["wrapped"]), None)
    except Exception:
        return None


def seal(plaintext: bytes, recipient_pubkeys: list[bytes]) -> dict[str, Any]:
    """Seal plaintext to one or more recipient X25519 public keys (raw 32 bytes each)."""
    if not recipient_pubkeys:
        raise ValueError("seal: at least one recipient public key is required")
    dek = os.urandom(_DEK_BYTES)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext, None)
    return {
        "v": 1,
        "payload": {"nonce": _b64(nonce), "ct": _b64(ct)},
        "recipients": [_wrap_dek(dek, pk) for pk in recipient_pubkeys],
    }


def open_envelope(envelope: dict[str, Any], recipient_priv_pem: bytes) -> bytes:
    """Open an envelope with a recipient's X25519 private key (PKCS8 PEM).
    Raises ValueError if none of the recipient stanzas were sealed to this key."""
    priv = serialization.load_pem_private_key(recipient_priv_pem, password=None)
    if not isinstance(priv, X25519PrivateKey):
        raise TypeError("recipient key is not X25519")
    dek = None
    for stanza in envelope.get("recipients", []):
        dek = _unwrap_dek(stanza, priv)
        if dek is not None:
            break
    if dek is None:
        raise ValueError("no recipient stanza could be unwrapped with this key")
    p = envelope["payload"]
    return AESGCM(dek).decrypt(_unb64(p["nonce"]), _unb64(p["ct"]), None)


def add_recipient(
    envelope: dict[str, Any], holder_priv_pem: bytes, new_recipient_pub_raw: bytes
) -> dict[str, Any]:
    """Add a recipient (e.g. an opt-in break-glass key) WITHOUT re-encrypting the
    payload. The holder must already be a recipient (to recover the DEK first)."""
    priv = serialization.load_pem_private_key(holder_priv_pem, password=None)
    if not isinstance(priv, X25519PrivateKey):
        raise TypeError("holder key is not X25519")
    dek = None
    for stanza in envelope.get("recipients", []):
        dek = _unwrap_dek(stanza, priv)
        if dek is not None:
            break
    if dek is None:
        raise ValueError("holder key cannot unwrap this envelope")
    envelope.setdefault("recipients", []).append(_wrap_dek(dek, new_recipient_pub_raw))
    return envelope
