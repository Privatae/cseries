"""Keys-app credential ingest for coding harnesses (canvas pivot, 2026-07).

The platform ships NO third-party login flow. Instead the user brings their own
credential — an API key OR an OAuth token they minted themselves — and stores it
in the **Keys** app (server/vault, sealed X25519), naming it by CONVENTION:

    harness-<id>        e.g.  harness-codex, harness-cursor, harness-opencode

At boot (and on a periodic loop) this module reads each conventioned Keys secret
and materializes it VERBATIM into the harness's expected on-disk creds file on
the shared creds PVC — the same file the Lane-A harness pod reads at run time
(system-apps/_harness_runtime/harness_core.py reads spec.creds_path). The harness
runtime's creds_shape parsing is unchanged; only the INGEST source moves from the
(removed) Connections connect-flow to the Keys app.

This REPLACES server/routes/harness_vault.py's connect-flow seal/materialize as
the source of truth. It runs AFTER the legacy materialize in the lifespan so the
Keys secret is authoritative when both are present. harness_vault is retired with
the rest of the connect flow.

SCOPE: codex, cursor, grok, opencode, kimi, antigravity — the file-based
harnesses.
claude-sdk is deliberately NOT here: its credential is dual-shaped (an
ANTHROPIC_API_KEY env vs an OAuth .credentials.json), and post-2026-08-04 it
is a user-installed CLI (the shipped host SDK engine is retired), so there is
no platform slice left to wire it through.

SAFETY (mirrors the legacy materialize contract): guarded on (pool, ENTITY_ID);
NEVER raises out of the lifespan; NEVER deletes a file; writes atomically at 0600;
a missing Keys secret is a no-op (the harness is simply not connected yet).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("privatae.pc.harness_keys")

# The file-based harnesses. claude-sdk is deliberately not a materialize
# target: its credential is dual-shaped (an ANTHROPIC_API_KEY env vs an OAuth
# .credentials.json), and with the host SDK engine retired (2026-08-04) it is
# a user-installed CLI, not a shipped lane.
# kimi's provider_json contract (bare key or JSON spec, read RAW by
# harness_core.py) is exactly the verbatim materialize this module does.
KEYS_HARNESS_PROVIDERS = ("codex", "cursor", "grok", "opencode", "kimi",
                          "antigravity")

# Conventioned Keys secret name → the harness's primary on-disk creds file.
# Paths mirror the harness runtime's spec.creds_path (the file each binary reads).
_PRIMARY_CREDS: dict[str, str] = {
    "codex":    "/home/privatae/.codex/credentials.json",
    "cursor":   "/home/privatae/.cursor/api_key",
    "grok":     "/home/privatae/.grok/auth.json",
    "opencode": "/home/privatae/.opencode/api_key",
    "kimi":     "/home/privatae/.kimi-code/provider.json",
    # antigravity: the Keys paste is the API-KEY lane (a Gemini API key). The
    # vendor-login lane writes its own file beside it
    # (antigravity-oauth-token, via rotated_creds) and is never pasted.
    "antigravity": "/home/privatae/.antigravity/api_key",
}


def keys_name(provider: str) -> str:
    """The conventioned Keys-app secret name for a harness. The host tells the
    user this exact name; this reads it back. One convention, no stored mapping."""
    return f"harness-{provider}"


# ── Provider-key fallback (reworked to the provider model) ──────────
# `harness-<id>` always wins. When it is ABSENT, a harness may run on one of
# the user's PROVIDER keys instead — but only the provider(s) that actually
# belong to it:
#
#   - A FIRST-PARTY harness (Kimi Code) runs on its own provider's key and
#     NOTHING else. With no Kimi key it is simply not connected — it must
#     never silently borrow a generic key. The old list here put openrouter
#     second, which manufactured a "kimi" credential out of a user's OpenRouter
#     key, lit the Keys chip for a provider the user does not have, and billed Kimi
#     Code's runs through a reseller's fee.
#   - The GENERIC harness (opencode) has no vendor of its own; the model says
#     it may run on any generic provider (registry kind flag — derived via
#     providers.generic_providers(), never a second hand-kept list), but its
#     wrapper today only speaks OpenRouter (specs.py _opencode_env sets one
#     env var; normalize_model force-prefixes openrouter/), so its allowed
#     set is openrouter alone — widening it is the unlock tracked in the
#     review (F12/F13), not something this list can grant.
#
# The vendor-ACCOUNT CLIs (codex, cursor, grok) are deliberately
# ABSENT and must stay absent: their creds are account artifacts (device-code
# grants, vendor tokens). No API key can drive them, so a fallback would only
# manufacture a credential that fails at run time.
def _fallback_providers(provider: str) -> tuple:
    if provider == "kimi":
        return ("kimi",)
    if provider == "opencode":
        return ("openrouter",)   # wrapper limitation, see above
    return ()


def harness_band(provider: str) -> str:
    """Which band of the provider model a harness sits in — derived from
    the fallback rule + the registry's kind flags, never a third list.

      'generic'     — the harness has no vendor of its own and runs on the
                      generic providers (opencode).
      'first_party' — everything else: bound to its own vendor, whether by
                      provider key (kimi) or vendor account (codex, cursor,
                      grok, claude)."""
    allowed = _fallback_providers(provider)
    if not allowed:
        return "first_party"
    try:
        from server.vault.providers import generic_providers
        gen = set(generic_providers())
        return "generic" if all(p in gen for p in allowed) else "first_party"
    except Exception:  # pragma: no cover - defensive
        return "first_party"


async def _wired_provider(pool, eid: int, provider: str) -> str:
    """The user's stored provider CHOICE for a generic harness —
    behavioral_params.credential_wiring['harness:<id>'], where the stored value
    names a registry provider. Returns '' when unset, unreadable, or naming a
    provider this harness may not use: wiring can REORDER within the allowed
    set, never widen it — a kimi wiring naming openrouter must stay refused,
    or the manufactured-credential path comes back with extra steps."""
    if pool is None or not eid:
        return ""
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            bp = json.loads(bp or "{}")
        wired = ""
        if isinstance(bp, dict):
            wiring = bp.get("credential_wiring") or {}
            if isinstance(wiring, dict):
                wired = (wiring.get(f"harness:{provider}") or "").strip().lower()
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_keys: wiring read for %s failed: %r", provider, e)
        return ""
    return wired if wired in _fallback_providers(provider) else ""


# The ONE canonical provider each harness's bare-key file means. A bare secret
# carries no endpoint, so the runtime auto-wraps it to that harness's own
# default base_url — a bare key synthesized from any OTHER provider would point
# a working key at the wrong endpoint and 401 at run time. Keep in step with
# the runtime's wrap destination (harness_core.py bare_key_base_url).
_BARE_KEY_CANONICAL = {
    "opencode": "openrouter",   # specs.py OPENCODE — OpenRouter key in api_key
    # kimi: harness_core resolves a bare key's base PER KEY — Moonshot platform
    # keys wrap to api.moonshot.ai, Kimi-for-Coding subscription keys to the
    # coding base (spec.bare_key_alt_base_urls, probed; 2026-08-10). The bare
    # synthesis below stays sound for BOTH key classes.
    "kimi":     "kimi",
}

# Harnesses whose creds file is a JSON provider spec, so a non-bare canonical
# key can still be synthesized into a dialable endpoint.
_PROVIDER_JSON_HARNESSES = ("kimi",)


def _synthesize_creds(provider: str, canonical: str, secret: str) -> Optional[bytes]:
    """The on-disk bytes for `provider` built from `canonical`'s API key, or
    None when the pair has no sound synthesis.

    NOTE (deviation from the design's build-item-5 sketch, deliberate): there
    is no `anthropic` preset. The provider registry marks anthropic as
    `wire: anthropic` (Messages) while the provider_json contract here is
    OpenAI-wire — synthesizing an anthropic spec would write a credential the
    runtime cannot dial."""
    if not secret:
        return None
    # Refuse any pairing the fallback rule does not allow — belt and braces
    # with _fallback_providers, so a future caller cannot resurrect the
    # "build kimi's credential out of an OpenRouter key" path this function
    # used to permit.
    if canonical not in _fallback_providers(provider):
        return None
    if provider in _BARE_KEY_CANONICAL and canonical == _BARE_KEY_CANONICAL[provider]:
        # Bare key: what opencode's api_key file and kimi's auto-wrap path
        # each expect. Gated on the harness's OWN bare
        # provider — kimi wraps bare keys to Moonshot, so an OpenRouter key
        # must NOT take this path; it falls through to a spec below.
        return secret.encode("utf-8")
    if provider not in _PROVIDER_JSON_HARNESSES:
        return None
    try:
        from server.vault.providers import PROVIDERS
    except Exception:  # pragma: no cover - defensive
        return None
    spec = PROVIDERS.get(canonical) or {}
    if spec.get("wire") != "openai" or not spec.get("base_url"):
        return None
    out = {"base_url": spec["base_url"], "api_key": secret}
    if provider == "kimi":
        # kimi's binary picks its wire from the provider entry's `type`, and
        # its default is Moonshot's own protocol — an endpoint synthesized
        # from an OpenAI-wire registry row must say so explicitly.
        out["type"] = "openai"
    return json.dumps(out).encode("utf-8")


async def _fallback_bytes(pool, eid: int, provider: str) -> tuple[Optional[bytes], str]:
    """(bytes, canonical-provider) synthesized from the user's canonical
    credentials for a harness whose own Keys entry is absent. ('', '') when
    nothing applies. NEVER raises.

    The user's wiring choice (behavioral_params.credential_wiring
    ['harness:<id>']) is tried FIRST when it names an allowed provider — the
    generic-harness provider picker writes it, and honoring it here is what
    makes the matrix's 'runs on' column the truth rather than a request. It
    reorders within _fallback_providers; it can never widen the set."""
    order = _fallback_providers(provider)
    wired = await _wired_provider(pool, eid, provider)
    if wired and wired in order:
        order = (wired,) + tuple(p for p in order if p != wired)
    for canonical in order:
        try:
            from server.vault.providers import resolve
            rc = await resolve(pool, eid, canonical, ("api_key",),
                               actor=f"harness_keys:{provider}:{eid}",
                               env_fallback=False)
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness_keys: canonical %s lookup for %s failed: %r",
                     canonical, provider, e)
            continue
        if not rc or not rc.secret:
            continue
        raw = _synthesize_creds(provider, canonical, rc.secret)
        if raw:
            return raw, canonical
    return None, ""


def _resolve_pool_entity():
    """(pool, entity_id) for the in-pod host vault, or (None, None) if the brain
    pool / ENTITY_ID isn't available. NEVER raises — a missing pool degrades to a
    no-op instead of breaking the lifespan (mirrors harness_vault)."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("harness_keys: pool unavailable: %s", e)
        return None, None
    if pool is None:
        return None, None
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        return None, None
    return pool, int(eid)


def _atomic_write(path: Path, raw: bytes) -> None:
    """Write a single creds file atomically (0600, parents mkdir'd)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".keys.tmp")
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _current_bytes(path: Path) -> bytes | None:
    try:
        if path.exists() and path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None


async def materialize_from_keys(provider: str, *, force: bool = False) -> str:
    """Materialize ONE harness's credential from its conventioned Keys secret into
    the harness's on-disk creds file. Keys is authoritative: if the secret is
    present and the on-disk file is absent OR differs, the file is (re)written.
    NEVER deletes; NEVER raises. Returns a per-provider status string.

    `force=True` skips the don't-clobber-a-valid-file short-circuit below —
    used by the wiring write route so a provider change takes effect NOW
    instead of at the next time the file happens to be invalid. Still never
    deletes; a force with nothing resolvable leaves the file untouched."""
    if provider not in _PRIMARY_CREDS:
        return "unknown_provider"
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return "no_pool"
    # The user revoked this harness and meant it. Without this check the
    # canonical fallback below rebuilds the harness from their OpenRouter/
    # Moonshot provider key, so revoking a harness credential disconnected
    # nothing — the harness just moved onto a different key of theirs.
    try:
        from server.network import harness_state
        if await harness_state.is_disconnected(provider):
            return "disconnected"
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_keys: disconnect check for %s failed (continuing): %r",
                 provider, e)
    try:
        from server.vault import api as vault_api
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_keys: vault api import failed for %s: %r", provider, e)
        return f"vault_import_error"

    # Don't clobber a live on-disk credential: if the harness's current creds
    # file is present AND valid (e.g. an in-pod OAuth refresh wrote a FRESHER
    # token than the Keys copy), leave it. Only (re)materialize from Keys when
    # the file is absent or invalid/expired — so the 300s loop can't overwrite a
    # refreshed grok grant with the stale original the user first pasted.
    # `force` (the wiring write) skips this so a provider change applies now.
    if not force:
        try:
            from server.routes.harnesses import harness_validity
            st = harness_validity(provider)
            if st.get("present") and st.get("valid"):
                return "present_valid"
        except Exception:
            pass  # validity check is best-effort; fall through to Keys materialize

    try:
        raw = await vault_api.open_secret(
            pool, entity_id=eid, name=keys_name(provider),
            actor=f"harness_keys:{provider}:{eid}")
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_keys: vault open %s (%s) failed: %r",
                 provider, keys_name(provider), e)
        return "vault_open_error"

    fallback_from = ""
    if not raw:
        # `harness-<id>` is absent — try the user's canonical provider
        # credential before giving up (design item 5). Vendor CLIs have no
        # entry in the fallback map and still land on "absent" here.
        raw, fallback_from = await _fallback_bytes(pool, eid, provider)
        if not raw:
            return "absent"  # nothing in Keys this harness can run on

    path = Path(_PRIMARY_CREDS[provider])
    if _current_bytes(path) == raw:
        if force:
            # A wiring write is a CONNECT, and a connected engine must be
            # runnable, which the file alone is not. Returning here skipped the
            # provisioning below, so an engine whose key had not changed was
            # never put on Entity.spec.harnesses: a test Host kept its
            # key on its volume across a re-created Entity, every seed answered
            # "unchanged", and every build failed "no harness-opencode
            # Deployment" (measured on two runs). The operator's upsert is
            # idempotent, so asserting again costs one no-op PATCH.
            await _provision_harness(provider)
        return "unchanged"

    # Don't clobber a ROTATED grant with the original paste. The guard above
    # only protects a file that is still VALID, which is the easy half; the
    # dangerous half is a file that has EXPIRED. Platform-side refresh rotates
    # the refresh token (harnesses.refresh_harness_cred) and re-seals the vault,
    # but it does not write back to the Keys secret — so the Keys copy still
    # holds the token the user first pasted, long since consumed. A pod that
    # boots after sitting scaled down past the grant's life comes up expired,
    # and without this the 300s loop would restore that dead copy over the live
    # one, and the seal sweep would then seal it — losing the grant entirely
    # (the 2026-07-02 invalid_grant class, review.py:1299).
    #
    # Compare expiries rather than presence: the user pasting a genuinely FRESH
    # credential must still win, and it does — its expiry is newer.
    if not force:
        try:
            from server.routes.harnesses import expiry_ms_from_text
            keys_exp = expiry_ms_from_text(
                raw.decode() if isinstance(raw, bytes) else str(raw))
            disk_raw = _current_bytes(path)
            disk_exp = expiry_ms_from_text(
                disk_raw.decode() if isinstance(disk_raw, bytes) else str(disk_raw or ""))
            if (keys_exp is not None and disk_exp is not None
                    and disk_exp > keys_exp):
                return "present_newer"
        except Exception:
            pass  # unparseable either side: fall through to the write

    try:
        _atomic_write(path, raw)
        log.info("harness_keys: %s materialized from Keys (%s) -> %s",
                 provider, fallback_from or keys_name(provider), path)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("harness_keys: write %s -> %s failed: %r", provider, path, e)
        return "write_failed"
    # WHICH credential this harness is now running on. Resolved above and, until
    # 2026-08-06, only logged — so nothing downstream (the picker,
    # harness_connections, the host) could answer "which key is opencode running
    # on?". Recorded AFTER the write lands, never before: on write_failed the
    # record would otherwise claim a source for a file that isn't there, and an
    # honest "unknown" beats a confident wrong answer. Metadata only — the Keys
    # NAME and the rung, never the secret.
    try:
        from server.network import harness_state
        await harness_state.record_credential(
            provider,
            name=fallback_from or keys_name(provider),
            cred_type="api_key" if fallback_from else "harness",
            source="provider-fallback" if fallback_from else "harness-key")
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_keys: provenance record for %s failed (non-fatal): %r",
                 provider, e)
    await _provision_harness(provider)
    # Distinct status so a sweep log says WHICH credential is running the
    # harness — a fallback is a different operational fact from the user's
    # own harness-<id> entry.
    return f"materialized_from:{fallback_from}" if fallback_from else "materialized"


async def connected_coding_harnesses() -> list[str]:
    """The key-connected coding engines this Host holds a credential file for
    and has not disconnected: what harness_sync re-asserts at boot.

    Presence of the file is the test, the same fact the materialize above
    writes and `_creds_present` reads. A disconnected harness is left out: its
    tombstone means no reconciler may bring it back (harness_state)."""
    from server.network import harness_state
    out: list[str] = []
    for provider, creds in sorted(_PRIMARY_CREDS.items()):
        if _current_bytes(Path(creds)) is None:
            continue
        try:
            if await harness_state.is_disconnected(provider):
                continue
        except Exception:  # noqa: BLE001 — is_disconnected already fails open
            pass
        out.append(provider)
    return out


async def _provision_harness(provider: str) -> bool:
    """Connect-time harness provisioning (ported from the connect flow,
    2026-07-18): a credential that just landed should make the harness
    RUNNABLE — fire the sanctioned spec.harnesses writer (bridge op
    workspace.set_harness → operator reconciles the Deployment@0 + Service).
    Idempotent on re-materialize; best-effort so a bridge hiccup never breaks
    the materialize itself, and a send the bridge was not up for is asserted
    again at boot (harness_sync.run_once, the coding-engine lane).
    Deprovision-on-revoke has no trigger here (this module never deletes) — it
    rides the wiring slice. Returns whether the request was sent."""
    try:
        from server.applications.installer import _sync_app_harness
        return await _sync_app_harness(provider, True)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("harness_keys: %s harness provisioning sync failed "
                    "(non-fatal): %s", provider, e)
        return False


async def materialize_all_from_keys() -> dict:
    """Materialize ALL file-based harnesses from their conventioned Keys secrets.
    Wired into the server lifespan AFTER the legacy connect-flow materialize (so
    Keys wins). Each provider is isolated; NEVER raises."""
    out: dict = {}
    for provider in KEYS_HARNESS_PROVIDERS:
        try:
            out[provider] = await materialize_from_keys(provider)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("harness_keys: %s raised: %r", provider, e)
            out[provider] = f"error:{e!r}"[:60]
    return out


async def run_keys_materialize_loop(interval_s: int = 300) -> None:
    """Background loop: re-materialize from Keys every interval so a credential
    the user ADDS or UPDATES mid-session propagates to the harness within one
    interval, without a pod restart. Best-effort; NEVER raises."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            res = await materialize_all_from_keys()
            changed = {k: v for k, v in res.items()
                       if v in ("materialized", "write_failed")}
            if changed:
                log.info("harness_keys refresh: %s", changed)
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness_keys materialize loop error: %r", e)
