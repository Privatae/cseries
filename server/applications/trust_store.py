"""
Trust store — Ed25519 public keys the kernel will accept for bundle
signatures.

Each row binds a public key to a publisher name and the PROVENANCE classes
that key is permitted to sign for (renamed from `tier_allowed` to
end the collision with the billing tier). The C-Series key is seeded on first
boot from /etc/cseries/keys/cseries-apps.pub.raw and is permitted to sign
`native` + `standard` (first-party). Native is also enforced by the kernel's
hardcoded id set.

Community + custom keys are added later via register_key() — community via the
operator/admin registration path (server.applications.community_keys), custom
through the entity's own gateway after it generates its personal signing key.
The C-Series key MUST NOT be authorized for `community`: third-party code is
signed by a community-authorized key so its provenance is issued by the signer,
never self-declared.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import asyncpg

log = logging.getLogger("privatae.applications.trust_store")


CSERIES_PUBLIC_KEY_PATH = Path("/etc/cseries/keys/cseries-apps.pub.raw")
COMMUNITY_PUBLIC_KEY_PATH = Path("/etc/cseries/keys/community-apps.pub.raw")


# `application_signing_keys` and its partial index are declared in
# migrations/v030_app_runtime_tables.sql:41 and created at boot. This module
# no longer mirrors that CREATE at runtime; it writes rows.

@dataclass(frozen=True)
class SigningKey:
    fingerprint: str
    public_key: bytes
    publisher: str
    provenance_allowed: list[str]
    registered_at: datetime
    revoked_at: datetime | None


def _fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


async def seed_cseries_key(pool: asyncpg.Pool) -> None:
    """Insert the C-Series kernel-trusted key if it's not already there."""

    if not CSERIES_PUBLIC_KEY_PATH.exists():
        log.warning(
            "C-Series public key not found at %s — standard/premium "
            "installs from factory bundles will fail until this is fixed",
            CSERIES_PUBLIC_KEY_PATH,
        )
        return

    pub = CSERIES_PUBLIC_KEY_PATH.read_bytes()
    if len(pub) != 32:
        log.error(
            "C-Series public key is %d bytes, expected 32 (Ed25519 raw)",
            len(pub),
        )
        return

    fp = _fingerprint(pub)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT 1 FROM application_signing_keys WHERE fingerprint = $1",
            fp,
        )
        if existing:
            return
        await conn.execute(
            """
            INSERT INTO application_signing_keys
                (fingerprint, public_key, publisher, provenance_allowed)
            VALUES ($1, $2, 'C-Series', ARRAY['native','standard'])
            """,
            fp, pub,
        )
        log.info("Seeded C-Series signing key %s...", fp[:16])


async def seed_community_key(pool: asyncpg.Pool) -> None:
    """Seed the platform community publishing key, trusted for `community`
    provenance ONLY. The app store signs third-party published apps with the
    matching private key; without this the installer's
    provenance gate refuses every community bundle. The C-Series key is NEVER
    community-authorized — third-party code is vouched for by THIS key, so its
    provenance is issued by the signer, never self-declared (closes C-3)."""

    if not COMMUNITY_PUBLIC_KEY_PATH.exists():
        log.warning(
            "Community public key not found at %s — community app installs will "
            "be refused until this is present", COMMUNITY_PUBLIC_KEY_PATH,
        )
        return

    pub = COMMUNITY_PUBLIC_KEY_PATH.read_bytes()
    if len(pub) != 32:
        log.error("Community public key is %d bytes, expected 32 (Ed25519 raw)", len(pub))
        return

    fp = _fingerprint(pub)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT 1 FROM application_signing_keys WHERE fingerprint = $1", fp,
        )
        if existing:
            return
        await conn.execute(
            """
            INSERT INTO application_signing_keys
                (fingerprint, public_key, publisher, provenance_allowed)
            VALUES ($1, $2, 'Community', ARRAY['community'])
            """,
            fp, pub,
        )
        log.info("Seeded Community signing key %s...", fp[:16])


def _row_to_key(row) -> SigningKey:
    return SigningKey(
        fingerprint=row["fingerprint"],
        public_key=bytes(row["public_key"]),
        publisher=row["publisher"],
        provenance_allowed=list(row["provenance_allowed"]),
        registered_at=row["registered_at"],
        revoked_at=row["revoked_at"],
    )


async def list_active_keys(pool: asyncpg.Pool) -> list[SigningKey]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM application_signing_keys
             WHERE revoked_at IS NULL
             ORDER BY publisher, registered_at
            """
        )
        return [_row_to_key(r) for r in rows]


async def register_key(
    pool: asyncpg.Pool,
    *,
    public_key: bytes,
    publisher: str,
    provenance_allowed: Iterable[str],
) -> SigningKey:
    """Add a new signing key. Used for community + custom publishers.
    Idempotent on fingerprint."""
    if len(public_key) != 32:
        raise ValueError(f"public key must be 32 bytes, got {len(public_key)}")

    fp = _fingerprint(public_key)
    provs = list(provenance_allowed)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO application_signing_keys
                (fingerprint, public_key, publisher, provenance_allowed)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (fingerprint) DO UPDATE
                SET publisher          = EXCLUDED.publisher,
                    provenance_allowed = EXCLUDED.provenance_allowed,
                    revoked_at         = NULL
            RETURNING *
            """,
            fp, public_key, publisher, provs,
        )
        log.info("registered signing key %s for publisher %s (%s)",
                 fp[:16], publisher, provs)
        return _row_to_key(row)


async def revoke_key(pool: asyncpg.Pool, fingerprint: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE application_signing_keys SET revoked_at = now() "
            "WHERE fingerprint = $1 AND revoked_at IS NULL",
            fingerprint,
        )
        log.info("revoked signing key %s", fingerprint[:16])
