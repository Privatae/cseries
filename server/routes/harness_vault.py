"""Harness-credential VAULT BACKING for the NON-Claude harnesses (Phase 5+, 2026-06-25).

Mirrors the Claude harness-OAuth vault path (server/network/nac_harness.py:
seal_agent_creds / materialize_agent_creds, vault name "harness:claude_oauth") for the
other coding harnesses authed through Connections: codex, cursor, grok.

The on-disk cred files connections.py writes (and the harness login subprocesses write)
are PLAINTEXT on a PVC subPath. These helpers SEAL those bytes into the host-owned
server/vault (X25519, in-pod only) so a restart can re-materialize the runtime file, and
MATERIALIZE them at boot. Read-the-file-then-seal — robust to HOW the file was written
(connections._write_creds_atomic OR a login subprocess writing its own file).

SCOPE: codex/cursor/grok ONLY. The Claude path (claude-sdk) is already vault-backed
by nac_harness and is deliberately NOT handled here (no double-seal).

SAFETY (load-bearing — mirrors the Claude materialize contract):
  - seal is BEST-EFFORT + guarded on (pool, ENTITY_ID): a vault-down / pre-pool pod still
    completes auth/login. seal NEVER raises out of the auth/login flow and NEVER fails it.
  - materialize NEVER deletes a file, NEVER overwrites a PRESENT file (present plaintext is
    authoritative; if present + vault missing it SEAL-PRIMES, like the Claude materialize),
    and NEVER raises.
  - a missing on-disk file is a no-op for seal (nothing to seal).

Per-harness file map: some harnesses persist MORE THAN ONE cred file. Each file is sealed
under its OWN sub-name (f"harness:{provider}:{slot}") so materialize can restore each
independently.
  codex   : credentials.json   (the app derives ~/.codex/auth.json itself — do NOT seal the
                                 derived file; the source-of-truth is credentials.json)
  cursor  : api_key
  grok    : auth.json          (the grok login subprocess writes ~/.grok/auth.json)
            api_key            (a console.x.ai deployment key the grok app ALSO reads — sealed
                                 if present so a paste survives a restart)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import weakref
from pathlib import Path
from typing import Optional

log = logging.getLogger("privatae.pc.connections.vault")

# The file-based NON-Claude harnesses. claude-sdk is excluded by construction.
HARNESS_PROVIDERS = ("codex", "cursor", "grok", "opencode", "kimi",
                     "antigravity")

# Per-provider on-disk cred files to seal/materialize. Each entry is
# (slot_name, absolute_path). Multiple files per provider seal under distinct
# sub-names. These mirror the paths connections.PROVIDERS[creds_path] + the
# extra files the harness main.py reads (grok api_key).
_FILES: dict[str, list[tuple[str, str]]] = {
    "codex":   [("credentials", "/home/privatae/.codex/credentials.json")],
    "cursor":  [("api_key", "/home/privatae/.cursor/api_key")],
    "grok":    [("auth", "/home/privatae/.grok/auth.json"),
                ("api_key", "/home/privatae/.grok/api_key")],
    "opencode": [("api_key", "/home/privatae/.opencode/api_key")],
    # kimi: provider_json shape (harness_core.py) — a bare vendor key OR a
    # JSON provider spec {base_url, model?, api_key?, type?} in one file.
    "kimi":    [("provider", "/home/privatae/.kimi-code/provider.json")],
    # antigravity: grok's dual shape — the vendor's own session token (written
    # by its login in the pod, carried back as rotated_creds) + the Keys-app
    # paste (a Gemini API key).
    "antigravity": [("token", "/home/privatae/.antigravity/antigravity-oauth-token"),
                    ("api_key", "/home/privatae/.antigravity/api_key")],
}


def _vault_name(provider: str, slot: str) -> str:
    return f"harness:{provider}:{slot}"


# The ONE slot per harness that the VENDOR'S OWN LOGIN writes — the artifact
# proving this harness was authenticated THROUGH the host, as opposed to handed
# a key someone pasted. grok's `api_key` slot is deliberately absent: that is a
# console.x.ai deployment key the user pastes, and counting it as a login would
# light a provider's OAuth slot on the strength of a paste. claude-sdk is not
# here by construction (nac_harness owns it, no double-seal) — its name comes
# from nac_harness.agent_creds_vault_name().
#
# antigravity has the same dual shape as grok and the same answer: `token` is
# the vendor token its GOOGLE sign-in writes (specs.py::_agy_login_env — "A
# Google sign-in: never GEMINI_API_KEY here"), while `api_key` is the Gemini
# API key someone pastes. Only the token is a sign-in.
_LOGIN_SLOT: dict[str, str] = {"codex": "credentials", "grok": "auth",
                               "antigravity": "token"}


def login_vault_name(provider: str) -> Optional[str]:
    """The vault name holding `provider`'s host-run login artifact, or None when
    the harness has no login flow (its credential is a paste). Cross-checked
    against _FILES' own slot names, so it can never name a slot this module does
    not actually seal."""
    p = (provider or "").strip()
    slot = _LOGIN_SLOT.get(p)
    if not slot or slot not in {s for s, _ in _FILES.get(p, [])}:
        return None
    return _vault_name(p, slot)


def _cred_type(provider: str) -> str:
    return f"harness_{provider}"


def _resolve_pool_entity():
    """(pool, entity_id) for the in-pod host vault, or (None, None) if the brain pool /
    ENTITY_ID isn't available. NEVER raises — mirrors
    server/integrations/core/credentials.py::_resolve_pool_entity so a missing pool degrades
    instead of breaking the auth/login flow."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("harness vault: pool unavailable: %s", e)
        return None, None
    if pool is None:
        return None, None
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        return None, None
    return pool, int(eid)


# One seal of a harness at a time. A seal reads the file, then awaits the vault
# write; two seals of the same harness interleaved can commit out of order. The
# 600 s sweep, the grok auto-refresh and a login's immediate seal
# (review._seal_login_grant) all call seal_harness_cred, and a sweep that read
# the file just before a login persisted a new grant, and was still inside its
# write, committed AFTER the login's seal — the vault kept the old grant until
# the next sweep. Serialised, the later seal always reads the newer file.
# Keyed by event loop as well: one loop in the server, but each gate's
# asyncio.run is its own, and a lock waited on in one loop cannot be in another.
_SEAL_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _seal_lock(provider: str) -> asyncio.Lock:
    per_loop = _SEAL_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(provider, asyncio.Lock())


async def seal_harness_cred(provider: str, *, actor: Optional[str] = None) -> bool:
    """BEST-EFFORT: read each of the provider's on-disk cred files and seal its bytes into
    the host-owned vault under f"harness:{provider}:{slot}". Read-the-file-then-seal so it
    is robust to HOW the file was written. Absent file → that slot is skipped. Guarded on
    (pool, ENTITY_ID); NEVER raises and NEVER fails the calling auth/login flow on a vault
    miss. Returns True if at least one slot was sealed. One at a time per harness
    (_seal_lock)."""
    if provider not in _FILES:
        return False
    async with _seal_lock(provider):
        return await _seal_harness_cred_now(provider, actor=actor)


async def _seal_harness_cred_now(provider: str, *, actor: Optional[str] = None) -> bool:
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return False  # vault unavailable — auth/login still completes from the on-disk file
    sealed = False
    try:
        from server.vault import api as vault_api
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness vault: api import failed for %s: %r", provider, e)
        return False
    for slot, path_str in _FILES[provider]:
        try:
            raw = _read_cred_bytes(Path(path_str))
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness vault: read %s (%s) failed: %r", provider, slot, e)
            continue
        if not raw:
            continue  # absent / empty → no-op for this slot
        try:
            await vault_api.store_secret(
                pool, entity_id=eid, name=_vault_name(provider, slot),
                cred_type=_cred_type(provider), plaintext=raw,
                actor=actor or f"harness:{provider}:{eid}",
            )
            sealed = True
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness vault: seal %s (%s) failed: %r", provider, slot, e)
            continue
    return sealed


def _read_cred_bytes(path: Path) -> bytes | None:
    """Read a cred file's bytes. Handles the file shape (the live case for every harness).
    For a DIRECTORY-shaped store (a vendor CLI that writes opaque files under its own
    dir), serialize the dir's regular-file children into one JSON envelope so the
    whole credential set seals + re-materializes as a unit. Returns None if absent/empty."""
    try:
        if not path.exists():
            return None
        if path.is_dir():
            blob: dict[str, str] = {}
            import base64
            for child in sorted(path.iterdir()):
                try:
                    if child.is_file() and child.stat().st_size > 0:
                        blob[child.name] = base64.b64encode(child.read_bytes()).decode("ascii")
                except OSError:
                    continue
            if not blob:
                return None
            return json.dumps({"__dir_b64__": blob}).encode("utf-8")
        data = path.read_bytes()
        return data if data.strip() else None
    except OSError:
        return None


def _materialize_one_file(path: Path, raw: bytes) -> None:
    """Atomically write a single cred file (0600, parents mkdir'd) from sealed bytes. If the
    sealed payload is the dir-envelope shape, re-expand the directory children instead."""
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        obj = None
    if isinstance(obj, dict) and "__dir_b64__" in obj:
        import base64
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        for name, b64 in (obj["__dir_b64__"] or {}).items():
            # Defensive: never write outside the dir (no path separators in child names).
            if "/" in name or name in ("", ".", ".."):
                continue
            child = path / name
            tmp = child.with_suffix(child.suffix + ".materialize.tmp")
            tmp.write_bytes(base64.b64decode(b64))
            os.chmod(tmp, 0o600)
            tmp.replace(child)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".materialize.tmp")
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


async def materialize_harness_cred(provider: str) -> dict:
    """BOOT-TIME materialize for ONE provider. For each of its cred files:
      - file ABSENT + vault HAS it   → write the file from the vault envelope (0600, atomic).
      - file PRESENT + vault MISSING → seal-prime the vault from the live plaintext.
      - file PRESENT + vault present → nothing to do.
      - neither                      → absent (harness simply not connected yet).
    NEVER deletes a file, NEVER overwrites a PRESENT file, NEVER raises. Returns a per-slot
    status dict for logging. Guarded on (pool, ENTITY_ID) by the caller and again here."""
    out: dict = {"provider": provider, "slots": {}}
    if provider not in _FILES:
        out["error"] = "unknown_provider"
        return out
    pool, eid = _resolve_pool_entity()
    if pool is None:
        out["error"] = "no_pool"
        return out
    # A disconnected harness is never rebuilt. Without this the seal-prime
    # branch below turns a revoke into a no-op: the file is still on disk and
    # the envelope is gone, which is precisely the shape it re-seals.
    try:
        from server.network import harness_state
        if await harness_state.is_disconnected(provider):
            out["error"] = "disconnected"
            return out
    except Exception as e:  # pragma: no cover - defensive
        log.info("materialize: disconnect check for %s failed (continuing): %r",
                 provider, e)
    try:
        from server.vault import api as vault_api
    except Exception as e:  # pragma: no cover - defensive
        out["error"] = f"vault_import:{e!r}"
        return out

    for slot, path_str in _FILES[provider]:
        path = Path(path_str)
        try:
            have_file = path.exists() and (
                path.is_dir() or path.stat().st_size > 0
            )
        except OSError:
            have_file = False

        vault_raw: bytes | None = None
        try:
            vault_raw = await vault_api.open_secret(
                pool, entity_id=eid, name=_vault_name(provider, slot),
                actor=f"harness:{provider}:{eid}")
        except Exception as e:  # pragma: no cover - defensive
            log.info("materialize: vault open %s (%s) failed: %r", provider, slot, e)
            vault_raw = None

        if not have_file and vault_raw:
            # Rebuild the runtime file the harness reads from the vault.
            try:
                _materialize_one_file(path, vault_raw)
                log.info("harness %s (%s) materialized from vault -> %s",
                         provider, slot, path)
                out["slots"][slot] = "materialized"
            except Exception as e:  # pragma: no cover - defensive
                log.warning("materialize: write %s (%s) failed: %r", provider, slot, e)
                out["slots"][slot] = "materialize_failed"
            continue

        if have_file and not vault_raw:
            # Prime the vault from the live plaintext (PRESENT file is authoritative — never
            # overwritten). Mirrors the Claude materialize "sealed" branch.
            try:
                raw = _read_cred_bytes(path)
                if raw:
                    await vault_api.store_secret(
                        pool, entity_id=eid, name=_vault_name(provider, slot),
                        cred_type=_cred_type(provider), plaintext=raw,
                        actor=f"harness:boot:{provider}:{eid}")
                    log.info("harness %s (%s) sealed into vault from on-disk plaintext "
                             "(boot prime)", provider, slot)
                    out["slots"][slot] = "sealed"
                else:
                    out["slots"][slot] = "absent"
            except Exception as e:  # pragma: no cover - defensive
                log.warning("materialize: boot seal %s (%s) failed: %r", provider, slot, e)
                out["slots"][slot] = "seal_failed"
            continue

        out["slots"][slot] = "present" if have_file else "absent"
    return out


async def materialize_all_harness_creds() -> dict:
    """BOOT-TIME materialize for ALL four non-Claude harnesses. Wired into the server
    lifespan right after the Claude materialize. NEVER raises — each provider is isolated;
    one provider's failure can't block the others or the lifespan."""
    results: dict = {}
    for provider in HARNESS_PROVIDERS:
        try:
            results[provider] = await materialize_harness_cred(provider)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("materialize_all: %s raised: %r", provider, e)
            results[provider] = {"provider": provider, "error": repr(e)}
    return results


# Last-sealed content hash per (provider, slot) — the sweep re-seals only on
# actual change. Per-process; after a restart the first sweep re-seals once
# (a content no-op: store_secret supersedes) and re-seeds the record.
_SEALED_HASH: dict[tuple, str] = {}


async def seal_sweep_all() -> dict:
    """Reconcile on-disk harness cred files INTO the vault whenever their
    content changes — regardless of WHO wrote them (login subprocess, gate
    auto-refresh, rotation write-back, a hand edit). Exists because the
    connect-flow seal RACED the grok login's file write (2026-07-02): the vault
    captured the pre-login bytes, the fresh grant lived only in the file, and
    the next pod roll re-materialized the DEAD grant over it — burning the
    user's login. A content-hash sweep has no timing dependence: drift
    reconciles within one interval. Best-effort; never raises."""
    import hashlib
    out: dict = {}
    try:
        from server.network import harness_state
        skip = await harness_state.disconnected_set()
    except Exception as e:  # pragma: no cover - defensive
        log.info("seal sweep: disconnect read failed (sweeping all): %r", e)
        skip = set()
    for provider in HARNESS_PROVIDERS:
        # A revoked harness whose file lingers (unlink failed, or something
        # rewrote it) must NOT be re-sealed — that is the 600s path that used to
        # undo a revoke within one interval.
        if provider in skip:
            out[provider] = "disconnected"
            continue
        for slot, path_str in _FILES.get(provider, []):
            try:
                raw = _read_cred_bytes(Path(path_str))
            except Exception:
                continue
            if not raw:
                continue
            h = hashlib.sha256(raw).hexdigest()
            if _SEALED_HASH.get((provider, slot)) == h:
                continue
            try:
                ok = await seal_harness_cred(provider, actor=f"harness:seal-sweep:{provider}")
                if ok:
                    _SEALED_HASH[(provider, slot)] = h
                    out[f"{provider}:{slot}"] = "sealed"
            except Exception as e:  # pragma: no cover - defensive
                log.info("seal sweep: %s (%s) failed: %r", provider, slot, e)
    return out


async def run_seal_sweep_loop(interval_s: int = 600) -> None:
    """Background loop: seal_sweep_all every interval. Started from the server
    lifespan next to the boot materialize."""
    while True:
        try:
            res = await seal_sweep_all()
            if res:
                log.info("harness seal sweep: %s", res)
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness seal sweep loop error: %r", e)
        await asyncio.sleep(interval_s)


async def revoke_harness_cred(provider: str, *, actor: Optional[str] = None) -> bool:
    """DISCONNECT a harness: supersede its vault envelope(s), REMOVE its on-disk
    cred file(s), and tombstone it so no reconciler puts it back.

    All three halves are required, and until 2026-08-06 only the first was here.
    This function's one caller (connections.auth_revoke) owned the file unlink
    and was deleted with the connect flow, so revoking became a no-op the
    reconcilers actively undid:

      - the file survived, so the harness kept running on the revoked credential;
      - boot materialize saw file-present + vault-empty and RE-SEALED it
        (materialize_harness_cred's seal-prime branch);
      - the 600s seal sweep did the same;
      - and harness_keys' canonical fallback would rebuild the file from the
        user's provider key even after a successful unlink.

    The tombstone is what makes revocation out-rank reconciliation. It clears
    when the user stores a credential for this harness again.

    Unlinking is the ONE place in this module that removes a cred file — every
    materialize path is still strictly never-delete. BEST-EFFORT; NEVER raises.
    Returns True if anything was actually revoked or removed."""
    if provider not in _FILES:
        return False
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return False
    try:
        from server.vault import api as vault_api
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness vault: api import failed for revoke %s: %r", provider, e)
        return False

    # Tombstone FIRST: if the unlink below succeeds but this pod dies before the
    # mark lands, the next boot's fallback would rebuild the harness. Marking
    # first makes the worst case "disconnected but the file lingers" — which the
    # next reconciler pass then skips — rather than a silent reconnect.
    # A tombstone that could not be written stops the revoke with nothing removed:
    # removing the file and the Vault copy without it is undone by the next
    # reconcile, and the caller would report a disconnect that did not happen.
    # disconnect_for_credential, the one caller, writes it first and raises; this
    # holds the same rule for any caller that reaches here directly.
    try:
        from server.network import harness_state
        marked = await harness_state.mark_disconnected(provider)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("harness vault: tombstone for %s failed, nothing revoked: %r", provider, e)
        return False
    if not marked:
        log.warning("harness vault: tombstone for %s not written, nothing revoked", provider)
        return False
    try:
        await harness_state.clear_credential(provider)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("harness vault: clearing the credential record of %s failed: %r",
                    provider, e)

    revoked_any = False
    for slot, path_str in _FILES[provider]:
        try:
            # revoke_secret (vault/api.py:80) returns True only when an ACTIVE
            # envelope was superseded — a missing one is not an error here.
            if await vault_api.revoke_secret(
                pool, entity_id=eid, name=_vault_name(provider, slot),
                actor=actor or f"harness:{provider}:{eid}"):
                revoked_any = True
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness vault: revoke %s (%s) failed: %r", provider, slot, e)
        # Remove the runtime file whether or not an envelope existed: a file
        # with no envelope is exactly the orphan case (a credential the deleted
        # connect flow left behind), and leaving it is what kept the harness
        # running after a revoke.
        try:
            p = Path(path_str)
            if p.exists() and p.is_file():
                p.unlink()
                log.info("harness %s (%s) cred file removed on revoke -> %s",
                         provider, slot, path_str)
                revoked_any = True
            # Drop the sweep's content hash so a later reconnect re-seals
            # instead of being skipped as "already sealed".
            _SEALED_HASH.pop((provider, slot), None)
        except OSError as e:  # pragma: no cover - defensive
            log.warning("harness vault: unlink %s (%s) failed: %r",
                        provider, slot, e)
    return revoked_any
