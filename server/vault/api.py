"""
Credential vault — the high-level API the rest of the platform calls.

HOST-OWNED (the common case for network / bastion / WG / mTLS creds): the host
stores a secret sealed to itself and reads it back in-pod. The platform stores
only ciphertext + the host's public key, so it cannot read these.
    await store_secret(pool, entity_id=..., name="wg:bastion", cred_type="wg_key", plaintext=...)
    await open_secret(pool, entity_id=..., name="wg:bastion") -> bytes | None

USER-OWNED (absolute sovereignty): the host can SEAL a secret TO a user (the user
reads it in their own mesh with their private key) but can NEVER reopen it.
    await seal_for_user(pool, user_id=..., name=..., cred_type=..., plaintext=..., actor=...)

Plus list / revoke / add an opt-in break-glass recipient.

This is the in-process API. The slip-gated cross-identity USE path (one identity
using another's credential) lives in server/vault/authz.py; the host opening its
OWN credential needs no slip — it owns the key.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

from server.vault import crypto, identity, store

# An MCP server's sign-in (server/mcp_oauth.py): a name under this prefix holds
# the person's token for that one server. It reads as absent and cannot be
# written unless the caller passes sign_in=True, which only three do: the
# sign-in code itself, the MCP client's own call (vault/authz.py, action
# mcp_call) and the person's step-up reveal (routes/vault.py). Every other
# reader takes a name from configuration, a plan or a tool argument, so a
# check in each of them would be one missed reader away from sending the token
# somewhere else, and a write would let a Host point the client at an account
# of someone else's choosing.
SIGN_IN_PREFIX = "mcp-oauth"

# The token of the git server the owner connected in Settings
# (server/git_server.TOKEN_NAME) is held the same way, for the same reason. It
# signs the Host in to that one server as the account the owner's connect
# checked: a broker use would send it to whatever URL the call names, and a
# write would have the Host sign in at its next start as an account that check
# never saw. Only server/git_server.py passes sign_in=True for it.
GIT_SERVER_TOKEN_NAME = "git-server"


def is_git_server_token(name: Any) -> bool:
    return str(name or "").strip().lower() == GIT_SERVER_TOKEN_NAME


def is_sign_in_name(name: Any) -> bool:
    n = str(name or "").strip().lower()
    return n.startswith(SIGN_IN_PREFIX) or n == GIT_SERVER_TOKEN_NAME


def held_name_words(name: Any) -> dict:
    """The words a refusal uses for a name is_sign_in_name holds: what it is,
    the only code that writes and uses it, and where the person changes it."""
    if is_git_server_token(name):
        return {"what": "the token of the git server connected in Settings",
                "writer": "Settings, Git server", "user": "the Host's own git clients",
                "where": "connect or disconnect the server in Settings, Git server"}
    return {"what": "an MCP server's sign-in", "writer": "the sign-in",
            "user": "its own connection",
            "where": "sign in or out on the server's card in the Network app"}


class SignInName(ValueError):
    """A write to an MCP server's sign-in from anywhere but the sign-in."""


async def store_secret(
    pool: asyncpg.Pool, *, entity_id: int, name: str, cred_type: str,
    plaintext: bytes, actor: Optional[str] = None,
    extra_recipients: Optional[list[bytes]] = None,
    if_absent: bool = False, sign_in: bool = False,
) -> str:
    """Store a host-owned secret, sealed to THIS host (plus any extra recipient
    public keys, e.g. an opt-in break-glass key). Returns the envelope id.
    `if_absent`: raise store.NameTaken instead of superseding an existing
    active key of that name (checked inside the write transaction).
    `sign_in`: the caller is the MCP sign-in (SIGN_IN_PREFIX above), or
    server/git_server.py writing GIT_SERVER_TOKEN_NAME."""
    signed_in_name = is_sign_in_name(name)
    if signed_in_name and not sign_in:
        w = held_name_words(name)
        raise SignInName(f"{name!r} is {w['what']}; only {w['writer']} writes it")
    owner = identity.host_identity(entity_id)
    recipients = [await identity.host_pubkey(pool)]
    if extra_recipients:
        recipients += list(extra_recipients)
    envelope_id = await store.put(
        pool, owner_identity=owner, logical_name=name, cred_type=cred_type,
        plaintext=plaintext, recipient_pubkeys=recipients, actor=actor or owner,
        if_absent=if_absent,
    )
    # Committed (store.put's transaction has closed): a scheduled task waiting
    # for this key wakes. The NAME only — the event's declared keys are all
    # emit() keeps, so the value cannot reach a row or the model. A sign-in's
    # token is rewritten at every refresh and no task waits on it.
    if not signed_in_name:
        from server import host_events
        await host_events.emit(pool, entity_id, "key_added", {"name": name})
    return envelope_id


async def open_secret(
    pool: asyncpg.Pool, *, entity_id: int, name: str, actor: Optional[str] = None,
    include_deactivated: bool = False, sign_in: bool = False,
) -> Optional[bytes]:
    """Open a host-owned secret in-pod with the host's private key. Returns None
    if there is no active credential of that name. A DEACTIVATED credential
    reads as absent (resolvers, the materializer, and every consumer see 'no
    key') unless include_deactivated — only the TOTP-gated reveal path passes
    that, because the user still owns the value while it is switched off. An
    MCP server's sign-in reads as absent too, unless `sign_in` (SIGN_IN_PREFIX
    above), and so does the git server's token (GIT_SERVER_TOKEN_NAME)."""
    if is_sign_in_name(name) and not sign_in:
        return None
    owner = identity.host_identity(entity_id)
    env = await store.get_envelope(
        pool, owner_identity=owner, logical_name=name, actor=actor or owner,
        include_deactivated=include_deactivated,
    )
    if env is None:
        return None
    return crypto.open_envelope(env, await identity.host_privkey(pool))


async def seal_for_user(
    pool: asyncpg.Pool, *, user_id: str, name: str, cred_type: str,
    plaintext: bytes, actor: str,
) -> str:
    """Seal a secret TO a user (sovereign): only the user's private key — held in
    their mesh, never here — can open it. The host/platform cannot reopen it."""
    owner = identity.user_identity(user_id)
    pub = await identity.pubkey_for(pool, owner)  # user's registered public key
    return await store.put(
        pool, owner_identity=owner, logical_name=name, cred_type=cred_type,
        plaintext=plaintext, recipient_pubkeys=[pub], actor=actor,
    )


async def list_secrets(pool: asyncpg.Pool, *, entity_id: int) -> list[dict[str, Any]]:
    """Metadata of the host's active credentials (no secret bytes)."""
    return await store.list_names(pool, owner_identity=identity.host_identity(entity_id))


async def revoke_secret(
    pool: asyncpg.Pool, *, entity_id: int, name: str, actor: Optional[str] = None,
) -> bool:
    owner = identity.host_identity(entity_id)
    return await store.revoke(pool, owner_identity=owner, logical_name=name, actor=actor or owner)


async def set_secret_deactivated(
    pool: asyncpg.Pool, *, entity_id: int, name: str, deactivated: bool,
    actor: Optional[str] = None,
) -> bool:
    """The temporary off-switch (vs revoke's permanent supersede): the value
    stays sealed, every resolver reads the credential as absent while off, and
    re-activation restores service without re-issuing."""
    owner = identity.host_identity(entity_id)
    return await store.set_deactivated(
        pool, owner_identity=owner, logical_name=name,
        deactivated=deactivated, actor=actor or owner)


async def add_break_glass(
    pool: asyncpg.Pool, *, entity_id: int, name: str, recipient_pubkey: bytes,
    actor: Optional[str] = None,
) -> bool:
    """Add an opt-in, user-controlled break-glass recipient to a host-owned secret:
    re-wraps the DEK to recipient_pubkey (no re-encryption of the payload). The host
    must currently own the secret (its key recovers the DEK)."""
    if is_sign_in_name(name):
        # A recipient added to a sign-in could open the token from any copy
        # of the envelope (a backup), outside the sign-in.
        raise SignInName(f"{name!r} is {held_name_words(name)['what']}; it takes no other recipient")
    owner = identity.host_identity(entity_id)
    env = await store.get_envelope(pool, owner_identity=owner, logical_name=name, actor=actor or owner)
    if env is None:
        raise KeyError(name)
    crypto.add_recipient(env, await identity.host_privkey(pool), recipient_pubkey)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE vault_envelope SET envelope=$3 "
                "WHERE owner_identity=$1 AND logical_name=$2 AND superseded_at IS NULL",
                owner, name, json.dumps(env),
            )
            # The change gets its own audit row, so a recipient added to a key
            # reads as a change in vault_audit, not as the get_envelope read
            # above.
            await store._audit(conn, actor or owner, "add_recipient", owner, name,
                               {"recipients": len(env.get("recipients") or [])})
    return True
