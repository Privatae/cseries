"""
Credential vault — the identity-scoped envelope store.

Each credential is an ENVELOPE (server/vault/crypto.py) sealed to exactly one
OWNING identity (server/vault/identity.py). The store keeps ciphertext + public
keys only; it cannot read any secret it holds. Every read/write is audited.

`vault_envelope` + `vault_audit` are declared in init-db.sql (:1317, :1340);
`deactivated_at` arrives with migrations/v147_band_c_kernel_vault_apps.sql.
This module used to ensure them on first use — that is done at boot now,
because a credential read should not need the privilege to create a table.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

from server.vault import crypto

log = logging.getLogger("privatae.vault.store")


async def _audit(
    conn, actor: str, op: str, owner_identity: Optional[str],
    logical_name: Optional[str], detail: Optional[dict] = None,
) -> None:
    await conn.execute(
        "INSERT INTO vault_audit (actor, op, owner_identity, logical_name, detail) "
        "VALUES ($1,$2,$3,$4,$5)",
        actor, op, owner_identity, logical_name, json.dumps(detail or {}),
    )


class NameTaken(Exception):
    """put(if_absent=True) found an active credential under that name."""


async def put(
    pool: asyncpg.Pool, *, owner_identity: str, logical_name: str, cred_type: str,
    plaintext: bytes, recipient_pubkeys: list[bytes], actor: str,
    if_absent: bool = False,
) -> str:
    """Seal `plaintext` to the owner (plus any extra recipients, e.g. break-glass)
    and store it, superseding any existing active credential of the same
    (owner_identity, logical_name). Returns the new envelope id. The plaintext is
    NEVER persisted in the clear.

    `if_absent`: refuse (NameTaken) instead of superseding when an active
    credential of that name exists (the ticket card's credential ask). The
    rule is enforced by the partial unique index on active names
    (idx_vault_envelope_active): with if_absent the supersede is SKIPPED, so
    the INSERT itself collides with any active row of that name — including
    one committed by a concurrent plain put — and the collision is the
    refusal. A check-then-write here could not say that (a concurrent
    supersede+insert slipped between the check and the write). A DEACTIVATED
    key of that name refuses too — it is still stored and re-enableable in
    Keys, so it is still "a key of that name"; the ticket-side ask check
    (routes/tickets.py refuse_bad_asks) counts deactivated names the same
    way, so both ends agree."""
    envelope = crypto.seal(plaintext, recipient_pubkeys)
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not if_absent:
                await conn.execute(
                    "UPDATE vault_envelope SET superseded_at = now() "
                    "WHERE owner_identity=$1 AND logical_name=$2 AND superseded_at IS NULL",
                    owner_identity, logical_name,
                )
            try:
                row = await conn.fetchrow(
                    "INSERT INTO vault_envelope (owner_identity, logical_name, cred_type, envelope) "
                    "VALUES ($1,$2,$3,$4) RETURNING id",
                    owner_identity, logical_name, cred_type, json.dumps(envelope),
                )
            except asyncpg.UniqueViolationError:
                if if_absent:
                    raise NameTaken(logical_name) from None
                raise
            await _audit(
                conn, actor, "put", owner_identity, logical_name,
                {"cred_type": cred_type, "recipients": len(recipient_pubkeys)},
            )
    return str(row["id"])


async def cred_type_of(
    pool: asyncpg.Pool, *, owner_identity: str, logical_name: str,
) -> Optional[str]:
    """The stored cred_type for (owner, name), or None if there is no active
    credential of that name.

    Metadata only — no envelope, no audit row, because this answers "what KIND
    of thing is this" rather than "give me the thing". The broker uses it to
    decide where a value is allowed to go (server/vault/authz.py), and that
    decision must not depend on the caller telling the truth about it.
    A deactivated credential reads as absent here, like everywhere else.
    """
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT cred_type FROM vault_envelope "
            "WHERE owner_identity=$1 AND logical_name=$2 "
            "AND superseded_at IS NULL AND deactivated_at IS NULL",
            owner_identity, logical_name,
        )


async def get_envelope(
    pool: asyncpg.Pool, *, owner_identity: str, logical_name: str, actor: str,
    include_deactivated: bool = False,
) -> Optional[dict[str, Any]]:
    """Return the sealed envelope dict for (owner, name), or None. This returns
    CIPHERTEXT — opening it requires the owner's private key (see authz.resolve).
    A deactivated credential reads as ABSENT (the off-switch) unless
    include_deactivated — only the user-facing reveal path passes that."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT envelope FROM vault_envelope "
            "WHERE owner_identity=$1 AND logical_name=$2 AND superseded_at IS NULL"
            + ("" if include_deactivated else " AND deactivated_at IS NULL"),
            owner_identity, logical_name,
        )
        await _audit(conn, actor, "get_envelope", owner_identity, logical_name, None)
    if row is None:
        return None
    env = row["envelope"]
    return env if isinstance(env, dict) else json.loads(env)


async def list_names(
    pool: asyncpg.Pool, *, owner_identity: str,
) -> list[dict[str, Any]]:
    """List the active credential names owned by an identity (metadata only).
    Deactivated rows are INCLUDED, flagged via deactivated_at — a deactivated
    credential is still stored, and the UI must be able to show and re-enable
    it. Consumers wanting only usable credentials filter on deactivated_at."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT logical_name, cred_type, created_at, deactivated_at FROM vault_envelope "
            "WHERE owner_identity=$1 AND superseded_at IS NULL ORDER BY logical_name",
            owner_identity,
        )
    return [dict(r) for r in rows]


async def set_deactivated(
    pool: asyncpg.Pool, *, owner_identity: str, logical_name: str,
    deactivated: bool, actor: str,
) -> bool:
    """Deactivate (or re-activate) the active credential of (owner, name).
    Returns True if one was updated. Deactivation keeps the envelope sealed —
    the value survives and re-activation restores service with no re-issue —
    but every resolution path reads it as absent."""
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE vault_envelope SET deactivated_at = " +
            ("now()" if deactivated else "NULL") +
            " WHERE owner_identity=$1 AND logical_name=$2 AND superseded_at IS NULL",
            owner_identity, logical_name,
        )
        await _audit(conn, actor, "deactivate" if deactivated else "activate",
                     owner_identity, logical_name, None)
    return res.endswith(" 1")


async def revoke(
    pool: asyncpg.Pool, *, owner_identity: str, logical_name: str, actor: str,
) -> bool:
    """Supersede (revoke) the active credential of (owner, name). Returns True if
    one was active. The envelope row is kept superseded for audit, not deleted."""
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE vault_envelope SET superseded_at = now() "
            "WHERE owner_identity=$1 AND logical_name=$2 AND superseded_at IS NULL",
            owner_identity, logical_name,
        )
        await _audit(conn, actor, "revoke", owner_identity, logical_name, None)
    return res.endswith(" 1")
