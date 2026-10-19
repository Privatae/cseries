"""
Per-container entity signing key — what the Software Factory uses to sign
apps the user's entity builds inside its own workspace.

Lifecycle:
  - First call to `ensure_entity_signing_key()` generates a fresh Ed25519
    keypair, stores the private PEM in `kernel_secrets` with id='entity_sign',
    and registers the public bytes in `application_signing_keys` as
    publisher='Entity-<entity_id>' with tier_allowed=['custom'].
  - Every subsequent call returns the persisted key.
  - The factory's POST /api/applications/build endpoint uses this key
    via `sign_with_entity_key()` to sign the project bundle.

This key never leaves the container. It cannot sign for tier=native or
=standard (the trust store enforces tier_allowed). A compromise of the
container compromises this key, but the blast radius is exactly that
container — other containers have their own, the Privatae key is
elsewhere.
"""

from __future__ import annotations

import logging

import asyncpg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.applications import bundles, trust_store
from server.applications.tokens import _SECRET_SCHEMA

log = logging.getLogger("privatae.applications.entity_signing")


KEY_ID = "entity_sign"


def _generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_pem, public_raw_32_bytes)."""
    priv = Ed25519PrivateKey.generate()
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


async def ensure_entity_signing_key(pool: asyncpg.Pool, entity_id: int) -> bytes:
    """Generate-or-fetch the entity signing key. Returns the private PEM.

    On first call:
        1. CREATE TABLE IF NOT EXISTS kernel_secrets (shared with tokens.py).
        2. Generate Ed25519, store private PEM in kernel_secrets.
        3. Register public bytes in trust store with publisher='Entity-<id>',
           tier_allowed=['custom'].

    On subsequent calls: just fetch and return.
    """
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow(
            "SELECT secret FROM kernel_secrets WHERE id = $1", KEY_ID,
        )
        if row is not None:
            return bytes(row["secret"])

        priv_pem, pub_raw = _generate_keypair()
        await conn.execute(
            "INSERT INTO kernel_secrets (id, secret) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            KEY_ID, priv_pem,
        )
        row = await conn.fetchrow(
            "SELECT secret FROM kernel_secrets WHERE id = $1", KEY_ID,
        )

    # Register the public side in the trust store. Done outside the txn
    # so a partial failure here doesn't roll back the key write — we'd
    # rather have a half-registered key than no key.
    await trust_store.register_key(
        pool,
        public_key=pub_raw,
        publisher=f"Entity-{entity_id}",
        provenance_allowed=["custom"],
    )
    log.info("entity signing key seeded for entity %d", entity_id)
    return bytes(row["secret"])


async def sign_with_entity_key(
    pool: asyncpg.Pool, entity_id: int, unsigned_bundle: bytes,
) -> bytes:
    """Convenience wrapper: load entity key, sign the bundle."""
    priv_pem = await ensure_entity_signing_key(pool, entity_id)
    return bundles.sign(unsigned_bundle, priv_pem)
