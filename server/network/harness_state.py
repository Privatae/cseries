"""Per-harness reconciler state: which harnesses the user has DISCONNECTED, and
which credential each connected harness is actually running on.

Two facts the credential reconcilers need and had nowhere to keep.

**disconnected** — the user revoked this harness's credential and meant it.
Both materializers are "never delete" by contract, so without an explicit
tombstone a revoke is undone within one interval:

  - `harness_vault.materialize_harness_cred` sees the still-present file with no
    vault row and takes the "seal-prime" branch, writing the revoked credential
    straight back into the vault;
  - `harness_vault.seal_sweep_all` does the same on its 600s timer;
  - `harness_keys.materialize_from_keys` finds `harness-<id>` absent and
    rebuilds the on-disk file from the user's CANONICAL provider key via the
    fallback ladder — so revoking a harness credential silently falls through
    to the user's OpenRouter/Moonshot key and the harness keeps running.

Revocation has to out-rank reconciliation or it is not revocation. Storing a
credential for the harness again is what clears the tombstone — reconnecting is
an explicit act, exactly like connecting was.

**credential** — WHICH Keys entry the on-disk credential was last written from.
`harness_keys` resolves this (the harness's own `harness-<id>` entry, or a
canonical provider key through the fallback ladder), logged it, and discarded
it — so nothing downstream could answer "which key is opencode running on?".
The picker needs it to offer a true model list, and the host needs it to explain
the configuration.

Stored in `entity_registry.behavioral_params` beside `credential_wiring` and
`coding_harnesses` — JSON, no schema change. Writes use `jsonb_set` on a single
key so a concurrent writer cannot lose a sibling key (a read-modify-write of the
whole blob can, and this file is written from background loops).

Best-effort throughout: never raises, a missing pool/ENTITY_ID degrades to "no
state", and every reader treats absence as UNKNOWN — never as disconnected.
Failing closed here would strand a working harness on a transient DB blip.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("privatae.pc.harness_state")

# behavioral_params keys.
_DISCONNECTED_KEY = "harness_disconnected"   # [harness_id, ...]
_CREDENTIAL_KEY = "harness_credential"       # {harness_id: {name, cred_type, source}}


def _resolve_pool_entity():
    """(pool, entity_id) for the in-pod brain, or (None, None). NEVER raises —
    mirrors the identical guard in harness_keys / harness_vault.

    brain_service.get_pool() returns Optional[asyncpg.Pool] (brain_service.py:134)
    and is None until initialize() runs, so the None check is a real state, not
    a defensive no-op."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("harness_state: pool unavailable: %s", e)
        return None, None
    if pool is None:
        return None, None
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        return None, None
    return pool, int(eid)


async def _read_params() -> dict:
    """behavioral_params as a dict ({} on any failure). Tolerates the column
    arriving as a decoded dict OR as a JSON string — both shapes are handled
    elsewhere in this codebase (see runtime/tools.py _exec_credential_wiring),
    so this does the same rather than assuming a codec is registered."""
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return {}
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_state: params read failed: %r", e)
        return {}
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp or "{}")
        except Exception:
            bp = {}
    return bp if isinstance(bp, dict) else {}


async def _write_key(key: str, value: Any) -> bool:
    """Set ONE behavioral_params key atomically. jsonb_set rather than a
    read-modify-write of the whole blob: this is called from background loops,
    and a RMW race here would silently drop credential_wiring or the user's
    coding_harnesses selection. The column is jsonb (verified on the live
    entity registry), so jsonb_set applies directly."""
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return False
    try:
        await pool.execute(
            "UPDATE entity_registry "
            "SET behavioral_params = jsonb_set("
            "    COALESCE(behavioral_params, '{}'::jsonb), $2::text[], $3::jsonb, true) "
            "WHERE id = $1",
            eid, [key], json.dumps(value))
        return True
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_state: write %s failed: %r", key, e)
        return False


async def _write_subkey(key: str, subkey: str, value: Any) -> bool:
    """Set behavioral_params[key][subkey] atomically, WITHOUT reading the parent
    object first — so two writers under different subkeys cannot drop each
    other's entry (the model-catalog race).

    Two nested jsonb_sets in one statement: the inner one makes sure the parent
    object exists (setting it to itself when it does, `{}` when it does not),
    because jsonb_set on a two-element path is a NO-OP when the parent is
    missing — it would silently write nothing on a fresh entity. The column is
    `behavioral_params JSONB` and NULLABLE with no default (init-db.sql:67),
    which is what the outer COALESCE covers."""
    pool, eid = _resolve_pool_entity()
    if pool is None:
        return False
    try:
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = jsonb_set("
            "    jsonb_set(COALESCE(behavioral_params, '{}'::jsonb), $2::text[],"
            "              COALESCE(behavioral_params -> $3, '{}'::jsonb), true),"
            "    $4::text[], $5::jsonb, true) "
            "WHERE id = $1",
            eid, [key], key, [key, subkey], json.dumps(value))
        return True
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_state: write %s.%s failed: %r", key, subkey, e)
        return False


# ── disconnected set ────────────────────────────────────────────────
async def disconnected_set() -> set:
    """Harness ids the user has explicitly disconnected. Empty on any failure —
    absence is 'not disconnected', never the reverse."""
    raw = (await _read_params()).get(_DISCONNECTED_KEY) or []
    if not isinstance(raw, list):
        return set()
    return {str(h).strip() for h in raw if str(h or "").strip()}


async def is_disconnected(provider: str) -> bool:
    """True only when this harness is positively marked disconnected. A DB
    failure reads as False: a transient blip must not strand a live harness."""
    p = (provider or "").strip()
    return bool(p) and p in (await disconnected_set())


async def mark_disconnected(provider: str) -> bool:
    """Tombstone this harness so no reconciler resurrects it."""
    p = (provider or "").strip()
    if not p:
        return False
    cur = await disconnected_set()
    if p in cur:
        return True
    return await _write_key(_DISCONNECTED_KEY, sorted(cur | {p}))


async def clear_disconnected(provider: str) -> bool:
    """Un-tombstone — the user stored a credential for this harness again."""
    p = (provider or "").strip()
    if not p:
        return False
    cur = await disconnected_set()
    if p not in cur:
        return True
    return await _write_key(_DISCONNECTED_KEY, sorted(cur - {p}))


# ── credential provenance ───────────────────────────────────────────
async def credential_records() -> dict:
    """{harness_id: {name, cred_type, source}} — what each harness's on-disk
    credential was last written from. {} on any failure."""
    raw = (await _read_params()).get(_CREDENTIAL_KEY) or {}
    return raw if isinstance(raw, dict) else {}


async def credential_record(provider: str) -> Optional[dict]:
    """One harness's provenance, or None when unknown. None is HONEST: a
    credential written by a path that predates this record, or by an in-pod
    vendor login, genuinely has no known Keys source."""
    p = (provider or "").strip()
    if not p:
        return None
    rec = (await credential_records()).get(p)
    return rec if isinstance(rec, dict) else None


async def record_credential(provider: str, *, name: str, cred_type: str,
                            source: str) -> bool:
    """Record which Keys entry this harness's on-disk credential came from.

    `source` names the RUNG, not the value:
      'harness-key'        — the harness's own `harness-<id>` Keys entry
      'provider-fallback'  — a provider key via harness_keys._fallback_providers
    `name` is the Keys logical name, so the host can name it back to the user.
    Never the secret — this record is metadata and must stay safe to display.
    """
    p = (provider or "").strip()
    if not p:
        return False
    recs = dict(await credential_records())
    recs[p] = {"name": name, "cred_type": cred_type, "source": source}
    return await _write_key(_CREDENTIAL_KEY, recs)


async def clear_credential(provider: str) -> bool:
    """Drop the provenance record (the credential is gone)."""
    p = (provider or "").strip()
    if not p:
        return False
    recs = dict(await credential_records())
    if p not in recs:
        return True
    recs.pop(p, None)
    return await _write_key(_CREDENTIAL_KEY, recs)


# ── known harness ids ───────────────────────────────────────────────
def known_harness_ids() -> frozenset:
    """Every harness id the platform knows, from the harness catalog itself
    (routes/harnesses.py:45 PROVIDERS) — derived, never a second hardcoded list
    that can drift from it. Empty set on import failure, which makes the callers
    below fail SAFE: no tombstone and no disconnect for an id nothing
    recognises, rather than acting on a guess."""
    try:
        from server.routes.harnesses import PROVIDERS
        return frozenset(PROVIDERS)
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_state: harness catalog unreadable: %r", e)
        return frozenset()


# ── Keys name -> harness id ─────────────────────────────────────────
def harness_for_keys_name(name: str) -> Optional[str]:
    """The harness a Keys logical name belongs to, or None.

    Recognises BOTH live conventions, because both are written by shipping code
    and a revoke must work whichever one the row uses:
      harness-<id>          harness_keys.keys_name
      harness:<id>:<slot>   harness_vault._vault_name
      harness:claude_oauth  nac_harness (the claude-sdk special case)

    Name-based by necessity: the vault has no owner column yet (review F4). When
    it gains one this should read that instead — the convention parsing is the
    stopgap, not the design.
    """
    n = (name or "").strip()
    if not n:
        return None
    hid: Optional[str] = None
    if n == "harness:claude_oauth":
        hid = "claude-sdk"
    elif n.startswith("harness-"):
        hid = n[len("harness-"):].strip() or None
    elif n.startswith("harness:"):
        hid = n[len("harness:"):].split(":", 1)[0].strip() or None
    if not hid:
        return None
    # Must name a REAL harness. Without this, a user credential that merely
    # starts with the prefix ('harness-notes') claims a harness id, and the
    # revoke path reports a disconnect that never happened while writing a junk
    # tombstone.
    return hid if hid in known_harness_ids() else None


# ── provider -> the harness login that fills its OAuth slot ─────────
#
# OAuth lights up once a harness has been authenticated through the host; there
# is no separate login service for it. The Keys app therefore has
# no Add button on a provider's OAuth slot — the HOST fills it, by running the
# vendor's OWN login — and this map is how the Keys UI learns which login fills
# which slot.
#
# Only an OAuth-shaped flow qualifies. A pasted API key is the user's own act and
# already has its own slot; counting it here would make "signed in" mean "typed
# something in" — which is why cursor, opencode and kimi (auth_type api_key) are
# absent, and why the map is re-checked against each harness's live auth_type
# below rather than trusted.
_OAUTH_LOGIN_PROVIDER: dict[str, str] = {
    "claude-sdk": "anthropic",
    "codex": "openai",
    "grok": "xai",
    # Added with the `google` registry row (2026-08-31). Antigravity's sign-in
    # IS a Google account sign-in, so it fills Google's slot exactly as Claude
    # Code fills Anthropic's.
    "antigravity": "google",
}


def oauth_login_slots() -> dict:
    """provider id -> {harness, harness_name, vault_name} for every provider
    whose OAuth slot the HOST fills by running the vendor's own login.

    Everything is cross-checked against a live source rather than asserted: the
    harness must still be in the catalog (routes/harnesses.py:45) AND still
    declare an OAuth auth_type, the provider must still be in the vault registry
    (vault/providers.py:54), and the vault name comes from the module that
    actually seals it (nac_harness.agent_creds_vault_name for claude-sdk,
    harness_vault.login_vault_name for the rest). An entry failing any check
    drops OUT — so a retired harness or a renamed slot yields no slot at all,
    never a slot nothing can fill.

    Returns {} on any import failure: no OAuth slots is the safe read, because a
    slot the host cannot fill is a control the user can only stare at.
    """
    try:
        from server.routes.harnesses import PROVIDERS as HARNESSES
        from server.routes.harness_vault import login_vault_name
        from server.vault.providers import PROVIDERS as REGISTRY
        from server.network import nac_harness
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_state: OAuth slot map unavailable: %r", e)
        return {}
    out: dict = {}
    for hid, provider in _OAUTH_LOGIN_PROVIDER.items():
        if hid not in HARNESSES or provider not in REGISTRY:
            continue
        spec = HARNESSES[hid]
        auth = str(spec["auth_type"]) if "auth_type" in spec else ""
        if not auth.startswith("oauth"):
            continue
        try:
            name = (nac_harness.agent_creds_vault_name() if hid == "claude-sdk"
                    else login_vault_name(hid))
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness_state: vault name for %s unavailable: %r", hid, e)
            continue
        if not name:
            continue
        out[provider] = {
            "harness": hid,
            "harness_name": str(spec["name"]) if "name" in spec else hid,
            "vault_name": name,
        }
    return out


async def set_wiring(harness: str, provider: Optional[str]) -> bool:
    """Store (or clear, provider=None) the user's provider choice for a
    harness: behavioral_params.credential_wiring['harness:<id>'] = provider.
    Storage only — WHICH providers a harness may use is enforced at the write
    route and again at every read (harness_keys._wired_provider refuses a
    provider outside the harness's allowed set), so a stale or hand-edited
    value can never widen anything. Same single-key jsonb_set discipline as
    the tombstone writes."""
    h = (harness or "").strip()
    if not h:
        return False
    wiring = (await _read_params()).get("credential_wiring") or {}
    if not isinstance(wiring, dict):
        wiring = {}
    key = f"harness:{h}"
    p = (provider or "").strip().lower()
    if p:
        wiring[key] = p
    else:
        wiring.pop(key, None)
    return await _write_key("credential_wiring", wiring)


# ── the default coding engine ───────────────────────────────────────
# Orchestrator → Coding Engines → the "Default" tick: the
# ONE engine a build runs on when nothing names one. Stored as its own
# behavioral_params key (not a flag inside coding_harnesses) so the read that
# every unnamed build makes is one key, and a per-engine override save can
# never drop it. Same single-key jsonb_set discipline as the tombstones.
_DEFAULT_ENGINE_KEY = "default_coding_harness"


async def default_coding_harness() -> Optional[str]:
    """The harness id ticked as the default engine, or None when no tick is
    set. None is honest — the resolver then refuses an unnamed build that has
    several engines to choose from, rather than guessing one."""
    raw = (await _read_params()).get(_DEFAULT_ENGINE_KEY)
    v = str(raw or "").strip()
    return v or None


async def set_default_coding_harness(harness: Optional[str]) -> bool:
    """Tick (or clear, harness=None) the default engine. Storage only — WHICH
    ids are real engines is enforced at the write route (HARNESS_APP_IDS) and
    the resolver re-checks connection at every build."""
    h = (harness or "").strip()
    return await _write_key(_DEFAULT_ENGINE_KEY, h or None)


# ── the vendor CLI's own model catalog, per harness ─────────────────
# Some harnesses can only answer "what models can you run" from INSIDE the pod,
# with the user's own account: `cursor-agent --list-models` builds a backend
# client and returns what that Cursor plan serves (specs.py has the read-off-
# the-bundle contract). That means there is nothing to mirror in the kernel the
# way codex's offline `debug models` table is mirrored — the honest source is
# per-entity, so it is cached here when a run has a pod awake anyway.
#
# Shape: {harness_id: {"models": [{id, label?, default?}], "read_at": iso,
#                      "release_tag": str|None}}
# An entry appears only after a real read; its ABSENCE is what makes the coding
# matrix say "not read yet" instead of inventing a list (which is exactly the
# bug this replaces — the matrix served OpenRouter slugs Cursor never had).
_MODEL_CATALOG_KEY = "harness_model_catalog"


async def model_catalogs() -> dict:
    """{harness_id: {models, read_at, release_tag}} — {} when nothing cached."""
    raw = (await _read_params()).get(_MODEL_CATALOG_KEY)
    return raw if isinstance(raw, dict) else {}


async def model_catalog(harness: str) -> Optional[dict]:
    return (await model_catalogs()).get(str(harness or "").strip()) or None


async def record_model_catalog(harness: str, models: list, *,
                               release_tag: Optional[str] = None) -> bool:
    """Cache what the harness's own CLI reported, under this harness's OWN slot.

    Storage only, and it will store an empty list if asked — but the CALLER
    should not ask. An empty catalog is AMBIGUOUS: measured 2026-08-21, a bogus
    Cursor key makes `cursor-agent --list-models` print the identical "No models
    available for this account." an empty plan prints, at exit 0. So
    review._maybe_cache_model_catalog declines to cache one and leaves the state
    at "not read yet", which the next run retries for free.

    PER-HARNESS write (2026-08-22). This used to read the whole map and write it
    back, which was safe only while cursor was the single reader. antigravity
    now reads its own catalog too, and two pods caching concurrently would each
    write back a map missing the other's entry — one silently dropped. That race
    is exactly the prerequisite review._CATALOG_STATUS_HARNESSES names for
    growing past one id, so it is paid here before the set grows."""
    h = str(harness or "").strip()
    if not h:
        return False
    import datetime
    rows = [m for m in (models or []) if isinstance(m, dict) and m.get("id")]
    entry = {
        "models": rows,
        "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "release_tag": release_tag,
    }
    return await _write_subkey(_MODEL_CATALOG_KEY, h, entry)


def key_state(*, present: bool, valid: bool, disconnected: bool) -> str:
    """The ONE precedence rule for a harness credential's display state:

        disconnected  — the user revoked it and meant it (tombstone). Beats
                        everything: a lingering file must not read as connected.
        not_set_up    — no credential; never had one (or it was fully removed).
        expired       — on disk but past its expiry; present yet unusable.
        connected     — present and valid.

    Pure so the matrix feed, the host tool and any future surface compute the
    same word from the same three facts — four states, four words, never
    blended (the not_set_up/disconnected blur is the F19/F26 confusion class).
    """
    if disconnected:
        return "disconnected"
    if not present:
        return "not_set_up"
    return "connected" if valid else "expired"


# ── the credential-lifecycle entry points ───────────────────────────
# EVERY writer of a harness credential goes through these two. They live here,
# not in the route handler, because the route is NOT the only writer: the
# `keys_manage` host tool calls the vault API directly. Putting the semantics in
# the route meant revoking through the agent skipped the disconnect entirely,
# and adding through the agent never cleared the tombstone — which could brick a
# harness that had a perfectly good new credential sitting in Keys. One entry
# point per direction, no second implementation to drift.
async def disconnect_for_credential(name: str, *,
                                    actor: Optional[str] = None) -> Optional[str]:
    """Called when a credential is REVOKED. When it belongs to a harness: remove
    the harness's runtime file(s), supersede its envelopes, and tombstone it.
    Returns the harness id, or None when the credential is not a harness's.
    Best-effort; never raises into the caller's revoke."""
    harness = harness_for_keys_name(name)
    if not harness:
        return None
    # Tombstone FIRST — a pod death between here and the unlink must fail toward
    # "disconnected with a lingering file", never a silent reconnect.
    await mark_disconnected(harness)
    await clear_credential(harness)
    if harness == "claude-sdk":
        # claude is deliberately absent from harness_vault._FILES (it has its
        # own reconciler), so its unlink lives with that reconciler
        # (nac_harness.revoke_agent_creds).
        try:
            from server.network import nac_harness
            await nac_harness.revoke_agent_creds(actor=actor)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("harness_state: claude disconnect failed: %r", e)
        return harness
    try:
        from server.routes import harness_vault
        await harness_vault.revoke_harness_cred(harness, actor=actor)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("harness_state: disconnect %s failed: %r", harness, e)
    return harness


async def reconnect_for_credential(name: str) -> Optional[str]:
    """Called when a credential is STORED. Clears the disconnect tombstone so
    the reconcilers pick the harness up again. Returns the harness id when a
    tombstone was actually lifted, else None.

    Deliberately NOT called from the reconcilers: they seal from the on-disk
    file, so clearing there would let a revoke be undone by the very
    resurrection the tombstone exists to prevent. Only a caller holding a NEW
    credential reconnects."""
    harness = harness_for_keys_name(name)
    if not harness or not await is_disconnected(harness):
        return None
    await clear_disconnected(harness)
    return harness
