"""
Credential vault — identities and their keypairs.

Every vault credential is OWNED by exactly one identity:
  - host:<entity_id>  — the host's own X25519 keypair lives in-pod (kernel_secrets);
                        only the host can open its own envelopes.
  - user:<user_id>    — the user's PUBLIC key is registered here; the PRIVATE key
                        lives only in the user's mesh (absolute sovereignty), so the
                        platform/host can seal TO the user but can never open.
  - platform          — platform-owned; private key held platform-side only.

host != user is enforced structurally: a host envelope is sealed to the host key,
a user envelope to the user key; no identity can open an envelope not sealed to it.

The host private key never leaves this pod (kernel_secrets, --auth=trust local PG),
so host-owned envelopes are unreadable off-host even by the platform that stores them.
"""
from __future__ import annotations

import asyncpg

# _SECRET_SCHEMA no longer CREATEs kernel_secrets — migrations/v147 does, at
# boot. What is left in it is the guarded REVOKE that keeps the host's key
# drawer out of the recall role's reach, and executing it here is still worth
# the round trip: this is one of the four call sites that reach the drawer, and
# the revoke is a no-op once converged (see tokens.py's ACL guard).
from server.applications.tokens import _SECRET_SCHEMA
from server.vault import crypto

_HOST_VAULT_KEY_ID = "vault_host_x25519"

# Registered recipient public keys for non-local identities (users / platform).
# We store ONLY public keys here — private keys for these identities live in
# their own space and are supplied to open_envelope() there, never on the
# platform. The table is declared in init-db.sql:1334; this module used to
# create it on first use; that is done at boot now.


def host_identity(entity_id: int) -> str:
    return f"host:{entity_id}"


def user_identity(user_id: str) -> str:
    return f"user:{user_id}"


PLATFORM_IDENTITY = "platform"


async def ensure_host_keypair(pool: asyncpg.Pool) -> tuple[bytes, bytes]:
    """Generate-or-fetch THIS host's vault X25519 keypair. Returns (priv_pem, pub_raw).

    The private key lives only in this pod's kernel_secrets — the platform never
    sees it, so host-owned envelopes are unreadable off-host."""
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow(
            "SELECT secret FROM kernel_secrets WHERE id=$1", _HOST_VAULT_KEY_ID
        )
        if row is None:
            priv_pem, _ = crypto.gen_identity_keypair()
            await conn.execute(
                "INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2) "
                "ON CONFLICT (id) DO NOTHING",
                _HOST_VAULT_KEY_ID, priv_pem,
            )
            row = await conn.fetchrow(
                "SELECT secret FROM kernel_secrets WHERE id=$1", _HOST_VAULT_KEY_ID
            )
        priv_pem = bytes(row["secret"])
    return priv_pem, crypto.pub_from_priv(priv_pem)


async def host_pubkey(pool: asyncpg.Pool) -> bytes:
    _, pub = await ensure_host_keypair(pool)
    return pub


async def host_privkey(pool: asyncpg.Pool) -> bytes:
    priv, _ = await ensure_host_keypair(pool)
    return priv


async def register_pubkey(pool: asyncpg.Pool, identity: str, pubkey: bytes) -> None:
    """Register a recipient public key for a non-local identity (a user or the
    platform). The matching private key NEVER lives here."""
    if len(pubkey) != 32:
        raise ValueError("X25519 public key must be 32 raw bytes")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_identity_pubkey (identity, pubkey) VALUES ($1,$2) "
            "ON CONFLICT (identity) DO UPDATE SET pubkey = EXCLUDED.pubkey",
            identity, pubkey,
        )


async def pubkey_for(pool: asyncpg.Pool, identity: str) -> bytes:
    """The X25519 public key to seal TO for an identity.

    host:<this entity> resolves to the local host key; everything else is looked
    up in vault_identity_pubkey (must have been registered first)."""
    if identity.startswith("host:"):
        return await host_pubkey(pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT pubkey FROM vault_identity_pubkey WHERE identity=$1", identity
        )
    if row is None:
        raise KeyError(f"no registered public key for identity {identity!r}")
    return bytes(row["pubkey"])
