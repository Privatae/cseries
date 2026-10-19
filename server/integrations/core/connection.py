"""
Connection lifecycle — create, refresh, revoke, health-check.

Pure framework logic. Provider-specific code (OAuth token exchange,
Plaid Link, etc.) lives in providers/<key>.py and is invoked through
small hook functions; this module knows nothing about any specific API.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from server.integrations.core import credentials, manifests, storage, signals
from server.integrations.core.models import Connection, ConnectionStatus, Provider

logger = logging.getLogger("privatae.pc.integrations.connection")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# create_connection removed 2026-07-25. It was DEAD POST-CUTOVER with zero
# callers, and its own docstring was a standing warning: re-wiring it would
# recreate the plaintext connections store — and regenerate the plaintext master
# key — that the boot cutover deliberately unlinks. A comment saying "never call
# this" is weaker than the function not existing. Live code uses
# create_connection_vault() below, which seals into the host-owned server/vault.


# get_credentials + update_credentials removed 2026-07-25. Both were
# DEAD POST-CUTOVER sync wrappers over the LEGACY plaintext NaCl store with
# ZERO callers anywhere in the tree. Live code uses the vault-first async
# pair below: get_credentials_resolve() and update_credentials_vault().
#
# NOTE: this does NOT remove every legacy write. create_connection_vault and
# update_credentials_vault below still fall back to credentials.put() when the
# vault is unavailable, deliberately, so a host that hasn't been cut over keeps
# working. What's gone is the set of entry points that reached the legacy store
# UNCONDITIONALLY, with no vault attempt first.


async def set_status(connection_id: str, status: ConnectionStatus,
               last_error: str | None = None) -> None:
    fields: dict[str, Any] = {"status": status.value}
    if last_error is not None:
        fields["last_error"] = last_error[:500]
    await storage.update_connection(connection_id, **fields)

    # Audit the transition
    row = await storage.get_connection(connection_id)
    if row:
        provider = manifests.get(row["provider_key"])
        if provider:
            signals.emit_portal_access(
                connection_id=connection_id,
                connection_kind=provider.connection_kind,
                connection_provider=provider.connection_provider or provider.key,
                operation=status.value,
                source_display=provider.display_name,
                content=f"{provider.display_name} status → {status.value}",
            )


# revoke() removed 2026-07-25. DEAD POST-CUTOVER sync revoke with ZERO callers;
# live code uses revoke_vault() below, which revokes the vault envelope AND the
# legacy blob. set_status() (above) stays — it is live from the provider
# refresh paths (_google_base.py) and from revoke_vault().


# ──────────────────────────────────────────────────────────────────────────────
# server.vault path (Phase 5, 2026-06-25) — NON-DESTRUCTIVE, ADDITIVE.
#
# These async variants are the ONLY credential path in live use: they seal NEW
# connection secrets into the host-owned server/vault and RESOLVE vault-first
# then legacy. The sync legacy trio (get_credentials/update_credentials/revoke)
# was deleted 2026-07-25 — it had no callers. Only create_connection above still
# touches the LEGACY NaCl store, and it is deliberately kept dead (see its
# docstring). Nothing here deletes legacy bytes; resolution still falls back for
# everything not yet migrated.
# ──────────────────────────────────────────────────────────────────────────────

def _vault_cred_type(provider) -> str:
    """Map a provider's auth mode to a vault cred_type label (metadata only; the
    payload is a JSON token blob either way)."""
    try:
        mode = provider.auth_mode.value if provider else "oauth2"
    except Exception:
        mode = "oauth2"
    return {
        "oauth2": "connection_oauth",
        "api_key": "connection_api_key",
        "basic": "connection_basic",
        "hosted_link": "connection_hosted_link",
        "none": "connection_opaque",
    }.get(mode, "connection_opaque")


async def create_connection_vault(
    provider_key: str,
    user_id: str,
    credentials_payload: dict[str, Any] | None = None,
    connection_config: dict[str, Any] | None = None,
    status: ConnectionStatus = ConnectionStatus.INITIATED,
) -> Connection:
    """Create a connection row, sealing any credentials into the host-owned
    server/vault (X25519, broker-gated) instead of the legacy NaCl store. The row
    keeps the same opaque `credentials_ref` shape, so list/get/revoke are
    unchanged. If the vault is unavailable, FALLS BACK to the legacy sync put()
    (non-destructive: a connection is never lost because the vault isn't ready)."""
    provider = manifests.get(provider_key)
    if not provider:
        raise ValueError(f"unknown provider: {provider_key}")

    ref = None
    if credentials_payload:
        cred_type = _vault_cred_type(provider)
        ref = await credentials.put_vault(
            credentials_payload, cred_type=cred_type,
            actor=f"integrations:{provider_key}",
        )
        if ref is None:
            # Vault not available — legacy fallback keeps the connection working.
            logger.warning(
                "vault unavailable for %s; sealing connection secret into legacy store",
                provider_key,
            )
            ref = credentials.put(credentials_payload)

    conn_id = f"conn_{uuid.uuid4().hex[:16]}"
    row = {
        "id": conn_id,
        "provider_key": provider_key,
        "user_id": str(user_id),
        "status": status.value,
        "credentials_ref": ref,
        "connection_config": connection_config or {},
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }
    await storage.insert_connection(row)

    signals.emit_portal_access(
        connection_id=conn_id,
        connection_kind=provider.connection_kind,
        connection_provider=provider.connection_provider or provider.key,
        operation="auth",
        source_display=provider.display_name,
        content=f"connected {provider.display_name}",
    )

    return Connection(
        id=conn_id,
        provider_key=provider_key,
        user_id=str(user_id),
        status=status,
        credentials_ref=ref,
        connection_config=connection_config or {},
        created_at=_now(),
        updated_at=_now(),
    )


async def get_credentials_resolve(connection_id: str) -> dict[str, Any] | None:
    """Resolve a connection's credentials VAULT-FIRST, then the legacy store. Use
    this from async flows; the sync get_credentials() (legacy-only) stays as-is for
    sync call sites until they migrate."""
    row = await storage.get_connection(connection_id)
    if not row or not row.get("credentials_ref"):
        return None
    return await credentials.resolve(row["credentials_ref"])


async def update_credentials_vault(connection_id: str, payload: dict[str, Any]) -> bool:
    """Async vault-first credential rotation (e.g. after an OAuth refresh). Re-seals
    the new payload into the host-owned vault under the row's EXISTING credentials_ref
    (store_secret supersedes the prior envelope). Also mirrors into the legacy NaCl
    store ONLY IF a legacy .bin for that ref still exists (pre-cutover) — once the
    boot cutover has unlinked the legacy files this mirror is a silent no-op, so no
    live path recreates plaintext after cutover. If the row has no ref yet, seals a
    fresh one (vault-first, legacy fallback). Mirrors update_credentials()'s contract."""
    row = await storage.get_connection(connection_id)
    if not row:
        return False
    ref = row.get("credentials_ref")
    if ref:
        # Re-seal into the vault under the same ref (supersedes the prior envelope).
        sealed = await credentials.put_vault(
            payload, ref=ref, actor=f"integrations:rotate:{row['provider_key']}",
        )
        # Legacy mirror ONLY while a legacy .bin still exists (pre-cutover). After the
        # boot cutover unlinks the vault dir this update() finds no file and returns
        # False without writing — it can NEVER recreate the plaintext store.
        if credentials._legacy_blob_exists(ref):
            credentials.update(ref, payload)
        if sealed is not None:
            return True
        # Vault unavailable AND no legacy blob: last-resort legacy write so a refresh
        # is never silently lost on a host that hasn't been cut over yet.
        return credentials.update(ref, payload)
    # No existing ref — create one (vault-first, legacy fallback) and attach it.
    new_ref = await credentials.put_vault(
        payload, actor=f"integrations:rotate:{row['provider_key']}",
    )
    if new_ref is None:
        new_ref = credentials.put(payload)
    await storage.update_connection(connection_id, credentials_ref=new_ref)
    return True


async def ensure_fresh_token(connection_id: str) -> str | None:
    """A valid access_token for this connection, refreshing + re-sealing if the
    one on file has expired. None when there is nothing usable.

    THE one implementation. It lived in providers/_google_base.py, which made it
    reachable only by importing a provider-private module — fine for the Google
    sync loops that were its only callers, wrong for the broker, which is
    provider-agnostic by design. `_google_base.ensure_fresh_token` now delegates
    here, so both paths refresh, persist and flag reauth identically.

    Nothing about the body was ever Google-specific: the family and token
    endpoint are read off the manifest. The Google defaults stay only as
    fallbacks for a manifest that omits them.

    A connection with no refresh_token is marked NEEDS_REAUTH rather than
    retried — that is the browser-token-flow shape, and no amount of retrying
    produces a refresh token that was never issued.
    """
    from server.integrations.core import oauth

    tok = await get_credentials_resolve(connection_id)
    if not tok:
        return None

    # An API-KEY connection has nothing to refresh, and must not be judged by
    # the OAuth clock. token_expired() returns True for any blob without
    # expires_in/_received_at — which is every api_key blob (connect_with_apikey
    # stores {"api_key": ...}) — so falling through would find no refresh_token
    # and mark a perfectly healthy connection NEEDS_REAUTH. Latent while every
    # shipped manifest is oauth2; reachable the moment one is not, and the
    # broker is what makes this function reachable for non-Google providers.
    if tok.get("api_key") and not tok.get("access_token"):
        return tok["api_key"]

    if not oauth.token_expired(tok):
        return tok.get("access_token")

    row = await storage.get_connection(connection_id)
    if not row:
        return None
    p = manifests.get(row["provider_key"])
    if not p:
        return None

    refresh = tok.get("refresh_token")
    if not refresh:
        await set_status(connection_id, ConnectionStatus.NEEDS_REAUTH,
                   last_error="no refresh_token on file")
        return None

    try:
        new_tok = await oauth.refresh_token(
            family=(p.auth or {}).get("family", "google"),
            token_endpoint=(p.auth or {}).get(
                "token_endpoint", "https://oauth2.googleapis.com/token"),
            refresh_token_val=refresh,
        )
        await update_credentials_vault(connection_id, new_tok)
        return new_tok.get("access_token")
    except Exception as e:
        await set_status(connection_id, ConnectionStatus.NEEDS_REAUTH,
                   last_error=str(e)[:200])
        return None


async def revoke_vault(connection_id: str) -> bool:
    """Async vault-first revoke. Revokes the host-owned vault envelope for
    "conn:<ref>" AND deletes the legacy NaCl blob (if any), then marks the connection
    REVOKED and clears its credentials_ref — keeping the row for audit history.
    Mirrors revoke()'s audit signal (via set_status -> emit_portal_access)."""
    row = await storage.get_connection(connection_id)
    if not row:
        return False
    ref = row.get("credentials_ref")
    if ref:
        # Tear down the vault envelope first (the post-cutover authority)…
        await credentials.revoke_vault(ref)
        # …then the legacy blob, if a pre-cutover host still has one.
        credentials.delete(ref)
    await set_status(connection_id, ConnectionStatus.REVOKED)
    await storage.update_connection(connection_id, credentials_ref=None)
    return True


async def migrate_connection_to_vault(connection_id: str) -> bool:
    """One-time, idempotent: re-seal ONE connection's legacy secret into the
    host-owned vault, keeping the legacy copy as fallback. Returns True if the
    secret now lives in the vault. Safe to call repeatedly."""
    row = await storage.get_connection(connection_id)
    if not row or not row.get("credentials_ref"):
        return False
    provider = manifests.get(row["provider_key"])
    return await credentials.migrate_ref_to_vault(
        row["credentials_ref"], cred_type=_vault_cred_type(provider),
        actor=f"integrations:migrate:{row['provider_key']}",
    )


async def migrate_all_to_vault(user_id: str | None = None) -> dict[str, int]:
    """Idempotent bulk migration: re-seal every connection's legacy secret into the
    host-owned vault (legacy copies LEFT in place as fallback). Returns
    {migrated, skipped, total}. Re-running is a no-op for already-migrated refs."""
    rows = await storage.list_connections(user_id=str(user_id) if user_id is not None else None)
    migrated = skipped = 0
    for r in rows:
        if not r.get("credentials_ref"):
            skipped += 1
            continue
        ok = await migrate_connection_to_vault(r["id"])
        if ok:
            migrated += 1
        else:
            skipped += 1
    return {"migrated": migrated, "skipped": skipped, "total": len(rows)}


async def list_for_user(user_id: str) -> list[dict[str, Any]]:
    """Sanitized view for UI — never exposes credentials_ref."""
    rows = await storage.list_connections(user_id=str(user_id))
    out = []
    for r in rows:
        provider = manifests.get(r["provider_key"])
        out.append({
            "id": r["id"],
            "provider_key": r["provider_key"],
            "provider_name": provider.display_name if provider else r["provider_key"],
            "categories": provider.categories if provider else [],
            "connection_kind": provider.connection_kind if provider else "",
            "status": r["status"],
            "connection_config": r.get("connection_config", {}),
            "created_at": r.get("created_at"),
            "last_sync_at": r.get("last_sync_at"),
            "last_error": r.get("last_error"),
        })
    return out
