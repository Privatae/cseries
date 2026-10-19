"""
The keys the Host's backups are encrypted to — registration and storage.

THE RULE: the platform holds a backup OR a key, never both.
Every backup (server/host_backup.py) is encrypted to EXACTLY these keys, whose
private halves the platform never sees, and to nothing else that outlives the
run:

  kind "host-recovery"  REQUIRED. The Host's recovery key, made in the owner's
                        browser on the account page; its private half was shown
                        to the owner once and never left the browser. The
                        platform relays only the public half (bridge op
                        backup.recovery_key). This Host PINS the first one it
                        is sent (trust on first use); a different one is only
                        adopted when the owner says so, with their second factor
                        (pin_recovery_key, adopt_recovery_key). Without one, no
                        backup is written.
  kind "bastion"        optional. A key one of the owner's bastions made and
                        keeps (`privatae-bastion backup key init`); label = that
                        bastion. Registered through the walled, step-up
                        POST /api/backup/key/register.

Never the Host's own vault key, never a platform key. One active row per
(kind, label), and one active host-recovery key. A replaced row is KEPT: backups
taken while it was active are still encrypted to it.

An older "user" kind (a key made inside this pod, or registered by hand) is
retired by migration v193: it is no longer a recipient and cannot be
registered. The seal/open helpers below stay for the sealed file such a key
came with; no Host had one when this changed.

Schema: migrations/v106_backup_user_key.sql, v193_backup_recipients.sql.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets
from datetime import datetime
from typing import Any, Optional

import asyncpg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from server.vault import crypto

# scrypt work factors. n=2^16, r=8, p=2 is ~0.17 s and ~64 MiB peak on this
# hardware (measured). OWASP's first-choice profile (n=2^17, r=8,
# p=1) does comparable work in ~0.21 s but peaks at ~128 MiB, and a transient
# 128 MiB allocation inside an entity pod is a real OOM risk next to Postgres.
# Recorded per row so raising them later cannot orphan an existing envelope.
KDF: dict[str, Any] = {"algo": "scrypt", "n": 1 << 16, "r": 8, "p": 2, "dklen": 32}

SOURCE_PASSPHRASE = "passphrase"
SOURCE_RECOVERY = "recovery_code"

# Crockford-ish alphabet: no I, L, O, U — a recovery code gets read aloud and
# typed by hand, and those four are where transcription errors come from.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# `backup_user_key` (+ its unique active index) and `backup_key_platform_state`
# are declared in migrations/v106_backup_user_key.sql:33 and :70, and created at
# boot. This module no longer mirrors those CREATEs at runtime.


def fingerprint(pub_raw: bytes) -> str:
    """Short, stable fingerprint of a recipient public key.

    Deliberately the SAME construction as `fp()` in
    ops/backup-custody/backup-entity-keys.py (sha256, first 16 hex chars), so
    the string the user sees in the Backup tab is the string the operator sees
    in `backup-entity-keys.py status`. Two different fingerprints of one key is
    how a user gets told their key is not in a set that in fact contains it."""
    return hashlib.sha256(pub_raw).hexdigest()[:16]


def new_recovery_code() -> str:
    """A one-time recovery code: 20 chars ~ 100 bits, grouped for reading."""
    raw = "".join(_secrets.choice(_CODE_ALPHABET) for _ in range(20))
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def normalize_recovery_code(code: str) -> str:
    """Accept what a human types: any case, spaces or dashes anywhere, and the
    four ambiguous letters mapped onto the characters they get mistaken for.
    Applied on BOTH sides (seal and open), so the mapping cannot desync."""
    s = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return s.translate(str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"}))


def derive_recipient(secret: str, salt: bytes,
                     params: Optional[dict] = None) -> tuple[bytes, bytes]:
    """An X25519 recipient derived from a user secret. Returns (priv_pem, pub_raw).

    The passphrase is not the key; it derives one. Recording n/r/p per row means
    a later increase in the work factor cannot orphan envelopes sealed under the
    old one — the stored row says which numbers to use."""
    k = dict(KDF)
    k.update(params or {})
    if k.get("algo") != "scrypt":
        raise ValueError(f"unsupported kdf algo {k.get('algo')!r}")
    raw = Scrypt(salt=salt, length=int(k["dklen"]),
                 n=int(k["n"]), r=int(k["r"]), p=int(k["p"])).derive(secret.encode("utf-8"))
    priv = X25519PrivateKey.from_private_bytes(raw)
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return pem, pub


def seal_private_key(priv_pem: bytes, passphrase: str,
                     recovery_code: str) -> dict[str, Any]:
    """Seal a backup private key to the passphrase AND the recovery code.

    Multi-recipient is free in this envelope format, so two independent ways in
    cost one extra stanza. This mirrors the bastion lane's two kdf_source values
    (`local_backup_envelope`), for the same reason: a user who forgets the
    passphrase but kept the printed code is not locked out of their own data,
    and neither route involves the platform.

    Returns the sealed FILE structure — envelope plus the salts and work factors
    needed to open it. This is what the user downloads and keeps; it is also
    what is stored, and it is safe at rest because opening it needs something
    that was never stored."""
    salts = {SOURCE_PASSPHRASE: os.urandom(16), SOURCE_RECOVERY: os.urandom(16)}
    _, pass_pub = derive_recipient(passphrase, salts[SOURCE_PASSPHRASE])
    _, rec_pub = derive_recipient(normalize_recovery_code(recovery_code),
                                  salts[SOURCE_RECOVERY])
    return {
        "v": 1,
        "kind": "privatae-backup-key",
        "kdf": dict(KDF),
        "salts": {k: base64.b64encode(v).decode("ascii") for k, v in salts.items()},
        # Recipient ORDER is not recoverable from the envelope (age-style
        # stanzas carry no identifier), so record which source is which. An
        # opener still tries every stanza; this is for humans and for the
        # unwrap tool's error messages.
        "sources": [SOURCE_PASSPHRASE, SOURCE_RECOVERY],
        "envelope": crypto.seal(priv_pem, [pass_pub, rec_pub]),
    }


def open_sealed(sealed: dict[str, Any], secret: str,
                source: str = SOURCE_PASSPHRASE) -> bytes:
    """Recover the backup private key PEM from a sealed file. The inverse of
    `seal_private_key`, and the exact operation
    ops/backup-custody/backup-key-unwrap.py performs offline."""
    known = sealed.get("sources") or [SOURCE_PASSPHRASE, SOURCE_RECOVERY]
    if source not in known:
        raise ValueError(f"{source!r} is not a source of this sealed file")
    salt_b64 = (sealed.get("salts") or {}).get(source)
    if not salt_b64:
        raise ValueError(f"sealed file carries no {source} salt")
    if source == SOURCE_RECOVERY:
        secret = normalize_recovery_code(secret)
    priv_pem, _ = derive_recipient(secret, base64.b64decode(salt_b64), sealed.get("kdf"))
    return crypto.open_envelope(sealed["envelope"], priv_pem)


# ---- storage ----------------------------------------------------------------

KIND_HOST_RECOVERY = "host-recovery"
KIND_BASTION = "bastion"
# The kinds a backup is encrypted to. THE RULE: nothing else, ever.
RECIPIENT_KINDS = (KIND_HOST_RECOVERY, KIND_BASTION)
# The kind a person can register from Settings. The recovery key arrives from
# the account page instead (pin_recovery_key in server/host_backup.py).
REGISTERABLE_KINDS = (KIND_BASTION,)
ORIGINS = ("in-pod", "self-generated", "owner-browser")
MAX_LABEL = 80

_COLS = "id, identity, kind, label, pubkey, sealed_priv, kdf, origin, created_at"


def _row(row) -> dict[str, Any]:
    out = dict(row)
    for k in ("sealed_priv", "kdf"):
        if isinstance(out.get(k), str):
            out[k] = json.loads(out[k])
    out["pubkey"] = bytes(out["pubkey"])
    return out


async def get_active(pool: asyncpg.Pool, ident: str, kind: str = KIND_HOST_RECOVERY,
                     label: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The active key of one kind (and label, when given), newest first."""
    if label is None:
        row = await pool.fetchrow(
            f"SELECT {_COLS} FROM backup_user_key WHERE identity=$1 AND kind=$2 "
            "AND superseded_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 1", ident, kind)
    else:
        row = await pool.fetchrow(
            f"SELECT {_COLS} FROM backup_user_key WHERE identity=$1 AND kind=$2 AND label=$3 "
            "AND superseded_at IS NULL", ident, kind, label)
    return None if row is None else _row(row)


async def list_active(pool: asyncpg.Pool, ident: str) -> list[dict[str, Any]]:
    """Every active key a backup is encrypted to, in a stable order. Only the
    RECIPIENT_KINDS, whatever else the table holds."""
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM backup_user_key WHERE identity=$1 AND superseded_at IS NULL "
        "AND kind = ANY($2::text[]) ORDER BY kind, label, id", ident, list(RECIPIENT_KINDS))
    return [_row(r) for r in rows]


async def list_all(pool: asyncpg.Pool, ident: str, kind: str) -> list[dict[str, Any]]:
    """Every key of one kind, active or replaced, newest first: the replaced
    ones still open the backups taken while they were active."""
    rows = await pool.fetch(
        f"SELECT {_COLS}, superseded_at FROM backup_user_key WHERE identity=$1 AND kind=$2 "
        "ORDER BY created_at DESC, id DESC", ident, kind)
    return [_row(r) for r in rows]


async def put_active(
    pool: asyncpg.Pool, *, ident: str, pub_raw: bytes,
    sealed: Optional[dict[str, Any]], origin: str,
    kind: str = KIND_BASTION, label: str = "",
) -> None:
    """Record an active key for (kind, label), superseding any previous one.

    Superseded rows are KEPT. A backup taken while an older key was active is
    encrypted to that older key; deleting the row would hide which key opens
    it."""
    if len(pub_raw) != 32:
        raise ValueError("X25519 public key must be 32 raw bytes")
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}")
    if kind not in RECIPIENT_KINDS:
        raise ValueError(f"{kind!r} is not a kind a backup may be encrypted to")
    label = (label or "").strip()
    if len(label) > MAX_LABEL:
        raise ValueError(f"label is longer than {MAX_LABEL} characters")
    async with pool.acquire() as conn:
        async with conn.transaction():
            # One active recovery key per Host, whatever its label; any other
            # kind, one per (kind, label).
            await conn.execute(
                "UPDATE backup_user_key SET superseded_at=now() "
                "WHERE identity=$1 AND kind=$2 AND ($4 OR label=$3) AND superseded_at IS NULL",
                ident, kind, label, kind == KIND_HOST_RECOVERY)
            await conn.execute(
                "INSERT INTO backup_user_key (identity, kind, label, pubkey, sealed_priv, kdf, origin) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                ident, kind, label, pub_raw,
                json.dumps(sealed) if sealed else None,
                json.dumps(sealed["kdf"]) if sealed else None,
                origin,
            )


async def retire_recovery(pool: asyncpg.Pool, ident: str) -> bool:
    """Stop encrypting new backups to this Host's recovery key, whatever its
    label (one is active at most). The row is kept: backups taken while it was
    active still open with it (host_backup.pin_recovery_key)."""
    res = await pool.execute(
        "UPDATE backup_user_key SET superseded_at=now() "
        "WHERE identity=$1 AND kind=$2 AND superseded_at IS NULL", ident, KIND_HOST_RECOVERY)
    return not str(res).endswith(" 0")


async def retire_registered_before(pool: asyncpg.Pool, ident: str,
                                   when: datetime) -> list[dict[str, Any]]:
    """Stop encrypting new backups to every key registered before `when`, the
    moment this Host's current owner took over (bridge backup.recovery_key
    owner_since, the last completed transfer). A key registered before then
    is a previous owner's: their recovery key, and their bastions' keys, which
    only this Host holds, so nothing on the platform can retire them. Rows are
    kept: backups taken before still open with them. Returns who was retired,
    as {kind, label, fingerprint}."""
    rows = await pool.fetch(
        "UPDATE backup_user_key SET superseded_at=now() "
        "WHERE identity=$1 AND superseded_at IS NULL AND kind = ANY($2::text[]) AND created_at < $3 "
        "RETURNING kind, label, pubkey", ident, list(RECIPIENT_KINDS), when)
    return [{"kind": r["kind"], "label": r["label"], "fingerprint": fingerprint(bytes(r["pubkey"]))}
            for r in rows]


async def retire(pool: asyncpg.Pool, ident: str, kind: str, label: str) -> bool:
    """Stop encrypting new backups to (kind, label). The row is kept."""
    res = await pool.execute(
        "UPDATE backup_user_key SET superseded_at=now() "
        "WHERE identity=$1 AND kind=$2 AND label=$3 AND superseded_at IS NULL",
        ident, kind, label)
    return res.endswith(" 1")
