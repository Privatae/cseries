"""
Credentials vault — encrypted at rest, opaque IDs everywhere.

Master key lives at /app/secrets/integrations-master.key, 0600, generated
on first boot. Per-credentials envelope: NaCl SecretBox with a random
24-byte nonce. Rotation = generate new master, re-encrypt all rows,
atomic swap.

No raw tokens in logs. No raw tokens in signals. Routes see credentials_ref
(an opaque string); vault only resolves it when the proxy needs to make
a call.

Migration note: when the Phase 9 bootstrap binary + native installer
land, this switches to the OS keychain (Windows Credential Manager,
macOS Keychain, libsecret). The API — get/put/delete — stays the same.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("privatae.pc.integrations.credentials")

SECRETS_DIR = Path("/app/secrets")
MASTER_KEY_PATH = SECRETS_DIR / "integrations-master.key"
VAULT_DIR = Path("/app/secrets/vault")


def _ensure_master_key() -> bytes:
    """DEAD/HAZARD POST-CUTOVER (2026-06-25): generates the PLAINTEXT 32-byte master
    key at /app/secrets/integrations-master.key — the exact security hole the boot
    cutover DELETES. Reached only by the legacy sync put/get/update below, none of
    which has a live caller anymore. DO NOT call from any live path: re-running this
    would re-create the plaintext master key the cutover removed. Kept only because
    the dead legacy functions reference it.

    Generate master key on first use. 32 bytes for SecretBox."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if MASTER_KEY_PATH.exists():
        raw = MASTER_KEY_PATH.read_bytes()
        if len(raw) != 32:
            raise RuntimeError("integrations master key is corrupted (wrong length)")
        return raw
    key = secrets.token_bytes(32)
    # Atomic write with restrictive perms
    tmp = MASTER_KEY_PATH.with_suffix(".tmp")
    with tmp.open("wb") as f:
        os.chmod(tmp, 0o600)
        f.write(key)
    tmp.replace(MASTER_KEY_PATH)
    os.chmod(MASTER_KEY_PATH, 0o600)
    logger.info("integrations master key generated at %s", MASTER_KEY_PATH)
    return key


def _nacl():
    # Lazy import so the container still boots without PyNaCl installed.
    from nacl.secret import SecretBox
    from nacl.utils import random as nacl_random
    return SecretBox, nacl_random


def put(payload: dict[str, Any]) -> str:
    """Encrypt + store a credentials blob. Returns opaque ref id."""
    SecretBox, nacl_random = _nacl()
    key = _ensure_master_key()
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(VAULT_DIR, 0o700)

    ref = f"cred_{uuid.uuid4().hex[:16]}"
    nonce = nacl_random(SecretBox.NONCE_SIZE)
    box = SecretBox(key)
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    ciphertext = box.encrypt(plaintext, nonce).ciphertext  # nonce separated below

    path = VAULT_DIR / f"{ref}.bin"
    with path.open("wb") as f:
        f.write(nonce)
        f.write(ciphertext)
    os.chmod(path, 0o600)
    return ref


def get(ref: str) -> dict[str, Any] | None:
    """Load + decrypt by ref. Returns None if missing or corrupted."""
    if not ref or "/" in ref or ".." in ref:
        return None
    path = VAULT_DIR / f"{ref}.bin"
    if not path.exists():
        return None
    try:
        SecretBox, _ = _nacl()
        key = _ensure_master_key()
        raw = path.read_bytes()
        nonce_size = SecretBox.NONCE_SIZE
        nonce, ciphertext = raw[:nonce_size], raw[nonce_size:]
        box = SecretBox(key)
        plaintext = box.decrypt(ciphertext, nonce)
        return json.loads(plaintext)
    except Exception as e:
        logger.warning("credentials load failed for %s: %s", ref, e)
        return None


def update(ref: str, payload: dict[str, Any]) -> bool:
    """Overwrite existing credentials at the same ref."""
    if not ref or "/" in ref or ".." in ref:
        return False
    existing = VAULT_DIR / f"{ref}.bin"
    if not existing.exists():
        return False
    SecretBox, nacl_random = _nacl()
    key = _ensure_master_key()
    nonce = nacl_random(SecretBox.NONCE_SIZE)
    box = SecretBox(key)
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    ciphertext = box.encrypt(plaintext, nonce).ciphertext
    tmp = existing.with_suffix(".tmp")
    with tmp.open("wb") as f:
        f.write(nonce)
        f.write(ciphertext)
    os.chmod(tmp, 0o600)
    tmp.replace(existing)
    return True


def delete(ref: str) -> bool:
    if not ref or "/" in ref or ".." in ref:
        return False
    path = VAULT_DIR / f"{ref}.bin"
    if not path.exists():
        return False
    path.unlink()
    return True


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Safe representation for logs. Never show token values."""
    safe_keys = {
        "access_token", "refresh_token", "id_token", "client_secret",
        "api_key", "password", "secret",
    }
    out = {}
    for k, v in payload.items():
        if k in safe_keys:
            out[k] = f"<{len(v)}-char redacted>" if isinstance(v, str) else "<redacted>"
        else:
            out[k] = v
    return out


# ──────────────────────────────────────────────────────────────────────────────
# server.vault BRIDGE (Phase 5, 2026-06-25) — NON-DESTRUCTIVE.
#
# The block above is the LEGACY connections store: a NaCl SecretBox whose master
# key lives in plaintext beside the data (no per-credential identity, no authz on
# resolve). Phase 5 moves NEW connection secrets into server/vault (X25519 sealed
# envelopes, host-owned, broker-gated) while keeping the legacy store fully intact
# as a READ FALLBACK. Nothing here deletes legacy bytes or the plaintext master key
# — that destructive cutover is a deliberate, separately-flagged follow-up.
#
# Resolution order for any ref is ALWAYS vault-first, then the legacy store, so an
# unmigrated secret keeps working and a migrated one is served from the vault.
#
# The vault is async + needs an asyncpg pool + ENTITY_ID; this legacy module is
# sync. These helpers are async and resolve the pool/entity themselves, so the
# integration framework can adopt them incrementally without rewriting sync call
# sites (which still hit put()/get() above unchanged).
# ──────────────────────────────────────────────────────────────────────────────

# Logical-name namespace for a connection secret inside the host-owned vault. We
# reuse the existing opaque `cred_<hex>` ref AS the logical name so no connections
# row needs a schema change — the row keeps storing the same ref string.
_VAULT_NAME_PREFIX = "conn:"


def _vault_name(ref: str) -> str:
    return f"{_VAULT_NAME_PREFIX}{ref}"


def _legacy_blob_exists(ref: str) -> bool:
    """True iff a legacy NaCl .bin still exists on disk for this ref. After the boot
    cutover unlinks the vault dir this is False for every ref, so the legacy-mirror
    writes in connection.update_credentials_vault become no-ops — nothing can
    recreate the plaintext store post-cutover."""
    if not ref or "/" in ref or ".." in ref:
        return False
    return (VAULT_DIR / f"{ref}.bin").exists()


async def revoke_vault(ref: str, *, actor: str | None = None) -> bool:
    """Revoke the host-owned vault envelope for "conn:<ref>". Returns True if a vault
    envelope was revoked, False if the vault is unavailable or no envelope existed.
    Does NOT touch the legacy blob — connection.revoke_vault() deletes that
    separately so revoke works whether or not the cutover has run."""
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return False
    try:
        from server.vault import api as vault_api
        return await vault_api.revoke_secret(
            pool, entity_id=eid, name=_vault_name(ref),
            actor=actor or "integrations:connection",
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("vault revoke failed for %s: %s", ref, e)
        return False


def _resolve_pool_entity():
    """Return (pool, entity_id) for the in-pod host vault, or (None, None) if the
    brain pool / ENTITY_ID isn't available (then callers fall back to the legacy
    store). Never raises — a missing pool must degrade to the legacy path, not
    break credential resolution."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("vault pool unavailable, using legacy store: %s", e)
        return None, None
    if pool is None:
        return None, None
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        return None, None
    return pool, int(eid)


async def put_vault(payload: dict[str, Any], *, cred_type: str = "connection_oauth",
                    ref: str | None = None, actor: str | None = None) -> str | None:
    """Seal a connection credentials blob into the host-owned vault and return its
    ref (reusing the legacy opaque-ref shape). Returns None if the vault is
    unavailable — the caller then falls back to the legacy sync put(). The payload
    is JSON-serialized and sealed to THIS host's X25519 key; the platform stores
    ciphertext only."""
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return None
    ref = ref or f"cred_{uuid.uuid4().hex[:16]}"
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    from server.vault import api as vault_api
    await vault_api.store_secret(
        pool, entity_id=eid, name=_vault_name(ref), cred_type=cred_type,
        plaintext=plaintext, actor=actor or "integrations:connection",
    )
    return ref


async def get_vault(ref: str) -> dict[str, Any] | None:
    """Open a connection secret from the host-owned vault by ref. Returns None if
    the vault is unavailable OR there is no active vault envelope for this ref (the
    caller then tries the legacy store)."""
    if not ref:
        return None
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return None
    try:
        from server.vault import api as vault_api
        raw = await vault_api.open_secret(
            pool, entity_id=eid, name=_vault_name(ref),
            actor="integrations:connection",
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("vault get failed for %s (falling back to legacy): %s", ref, e)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("vault payload for %s is not JSON: %s", ref, e)
        return None


async def resolve(ref: str) -> dict[str, Any] | None:
    """Vault-FIRST, legacy-FALLBACK resolution of a connection secret. This is the
    non-destructive read path: a migrated secret is served from the vault; an
    unmigrated one is still served from the legacy NaCl store. NEVER deletes the
    legacy copy."""
    if not ref:
        return None
    via_vault = await get_vault(ref)
    if via_vault is not None:
        return via_vault
    # Legacy fallback — the sync NaCl store. Kept readable until a deliberate cutover.
    return get(ref)


async def migrate_ref_to_vault(ref: str, *, cred_type: str = "connection_oauth",
                               actor: str | None = None) -> bool:
    """Idempotently re-seal a single legacy-store secret into the host-owned vault.
    Reads the legacy NaCl bytes, seals them into the vault under the SAME ref, and
    LEAVES the legacy bytes in place (non-destructive). Returns True if the secret
    now exists in the vault (already-present or freshly migrated), False if the
    secret can't be read or the vault is unavailable.

    Idempotent: if a vault envelope for this ref already exists, this is a no-op
    success — it does not overwrite/supersede a migrated value with stale legacy
    bytes."""
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return False
    # Already migrated? Don't clobber the live vault value with legacy bytes.
    if await get_vault(ref) is not None:
        return True
    legacy = get(ref)
    if legacy is None:
        return False
    new_ref = await put_vault(legacy, cred_type=cred_type, ref=ref, actor=actor)
    return new_ref is not None


# ──────────────────────────────────────────────────────────────────────────────
# DESTRUCTIVE BOOT-TIME CUTOVER (2026-06-25) — VERIFY-BEFORE-DELETE.
#
# This is the deliberate, one-way removal of the legacy plaintext connections store:
# the NaCl-sealed `*.bin` blobs under /app/secrets/vault and the PLAINTEXT 32-byte
# master key at /app/secrets/integrations-master.key (the security hole). It runs
# once per boot from the server lifespan AFTER the asyncpg pool + host vault are up.
#
# SAFETY CONTRACT (load-bearing — a host with real legacy creds must NEVER lose them):
#   1. migrate  : idempotently re-seal EVERY connection's legacy secret into the vault.
#   2. VERIFY   : for every connection row WITH a credentials_ref, assert get_vault(ref)
#                 actually resolves a secret FROM the vault. Collect any that fail.
#   3. DELETE   : unlink the legacy blobs + master key ONLY IF verify had ZERO failures
#                 AND migrate reported no skipped-rows-that-had-a-ref (skip-with-data).
#   4. DEGRADE  : on ANY failure / exception — DO NOT delete. Log a LOUD warning naming
#                 the unmigrated refs and return. Re-running next boot is safe + idempotent.
#
# Empty legacy store (no rows, no files) ⇒ clean no-op (nothing migrated, nothing to
# delete). The function never raises out — it catches and degrades.
# ──────────────────────────────────────────────────────────────────────────────

# Master key path is module-level MASTER_KEY_PATH; vault blob dir is VAULT_DIR.

async def cutover_legacy_cred_store(pool, entity_id: int) -> dict[str, Any]:
    """One-way: migrate→verify→delete the legacy plaintext connections store. Returns a
    status dict {ran, deleted, migrated, verified, failed_refs, reason}. NEVER raises:
    any error degrades to a no-delete warning + safe re-run next boot."""
    from server.integrations.core import connection as _conn
    from server.integrations.core import storage

    status: dict[str, Any] = {
        "ran": True, "deleted": False, "migrated": 0, "verified": 0,
        "failed_refs": [], "reason": "",
    }

    # ── 1. MIGRATE (idempotent re-seal of every connection's legacy secret) ──────
    try:
        mig = await _conn.migrate_all_to_vault()
    except Exception as e:
        logger.warning(
            "CRED CUTOVER ABORTED — migrate_all_to_vault raised (%s); NOT deleting "
            "any legacy bytes; will retry next boot (idempotent)", e,
        )
        status["reason"] = f"migrate_raised:{e}"
        return status
    status["migrated"] = mig.get("migrated", 0)

    # ── 2. VERIFY — every ref'd row must now resolve FROM THE VAULT ──────────────
    # Also independently detect skipped-rows-that-HAD-a-ref (migrate counts both
    # ref-less rows and failed migrations as "skipped"; only a skipped row WITH a
    # credentials_ref is a real failure that must block deletion).
    try:
        rows = await storage.list_connections()
    except Exception as e:
        logger.warning(
            "CRED CUTOVER ABORTED — could not enumerate connections to verify (%s); "
            "NOT deleting any legacy bytes; will retry next boot", e,
        )
        status["reason"] = f"list_raised:{e}"
        return status

    failed_refs: list[str] = []
    verified = 0
    for r in rows:
        ref = r.get("credentials_ref")
        if not ref:
            continue  # ref-less row carries no secret; nothing to verify or lose
        try:
            present = await get_vault(ref) is not None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("cutover verify: get_vault(%s) raised: %s", ref, e)
            present = False
        if present:
            verified += 1
        else:
            failed_refs.append(ref)
    status["verified"] = verified
    status["failed_refs"] = failed_refs

    # ── DEGRADE-NOT-BREAK: any ref that did NOT verify blocks all deletion ───────
    if failed_refs:
        logger.warning(
            "CRED CUTOVER HELD — %d connection secret(s) did NOT verify in the vault "
            "(refs: %s). NOT deleting the legacy store or master key; the legacy "
            "fallback keeps these working. Re-running next boot is safe + idempotent.",
            len(failed_refs), failed_refs,
        )
        status["reason"] = "verify_failed"
        return status

    # ── HELD while the old integrations SQLite file is still there ─────────────
    # The connections verified above are the Postgres ones (v201). A file that
    # storage.retire_sqlite_store kept (a copy that failed) still holds
    # connections that are not in that list, and their secrets may be only in
    # the legacy blobs: deleting those now would lose them for good.
    if storage.LEGACY_DB_PATH.exists():
        logger.warning(
            "CRED CUTOVER HELD — the old integrations store %s is still there, so its "
            "connections are not all in Postgres yet; NOT deleting the legacy store or "
            "master key. Re-running next boot is safe + idempotent.", storage.LEGACY_DB_PATH,
        )
        status["reason"] = "integrations_store_not_retired"
        return status

    # ── 3. DELETE — only reached when verify had ZERO failures ───────────────────
    # (Every ref'd row resolves from the vault; ref-less rows hold no secret. The
    # legacy bytes are now provably redundant.) Unlink blobs + the plaintext master.
    removed = 0
    try:
        if VAULT_DIR.exists():
            for p in VAULT_DIR.glob("*.bin"):
                try:
                    p.unlink()
                    removed += 1
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("cutover: could not unlink legacy blob %s: %s", p, e)
        if MASTER_KEY_PATH.exists():
            MASTER_KEY_PATH.unlink()
            removed += 1
    except Exception as e:  # pragma: no cover - defensive
        # Partial delete is still SAFE: every secret is verified-present in the vault,
        # and resolve() is vault-first, so a leftover blob is dead weight, not a risk.
        logger.warning(
            "CRED CUTOVER partial — verified %d secret(s) in the vault and deleted "
            "%d legacy artifact(s), but hit an error removing the rest (%s). The "
            "vault is authoritative; leftover legacy bytes are inert. Re-run clears them.",
            verified, removed, e,
        )
        status["deleted"] = removed > 0
        status["reason"] = f"delete_partial:{e}"
        return status

    status["deleted"] = True
    if removed:
        logger.warning(
            "CRED CUTOVER COMPLETE — %d connection secret(s) verified in the host vault; "
            "DELETED %d legacy artifact(s) including the plaintext master key. The "
            "plaintext connections store is gone; the vault is now the sole authority.",
            verified, removed,
        )
    else:
        logger.info(
            "CRED CUTOVER no-op — legacy plaintext connections store already empty "
            "(0 blobs, no master key); nothing to migrate or delete.",
        )
    return status
