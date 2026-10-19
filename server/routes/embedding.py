"""
Embedding — entity-side surface for the platform embedding service.

ONE source of truth: behavioral_params.embedding_model (+ embedding_provider).
That is the field the memory engine actually embeds with
(memory_engine._entity_embedding_config). Setting it goes through
apply_embedding_switch(), which persists the field AND enqueues the re-embed
job — so a model change actually takes effect. The Privatae App's routing
surface reads the same field via resolve_role_model("embedding"), so the UI
shows + sets exactly what embeds (no decorative picker, no backend-name leak).

Providers are reported in the user-facing taxonomy (local | privatae). The
platform backend (a GPU host / runpod / ollama) is an implementation detail and
always reads as 'privatae' (platform-served, billed to the subscription).

Permissions: the reads (/current, /catalog) require read:inference_catalog and
the write (/switch) requires write:inference_routing — the same scopes the
model-routing apps already declare. Callers: the Privatae + OpenRouter Direct
pickers (all three routes), and the Memory app / oracle (/switch only). The
Skills app's copy of the panel was removed 2026-10-05 (Skills 0.2.0). apply_embedding_switch() and the by-model coverage route invoke
current()/catalog() as plain functions, so the FastAPI dependency does not fire
for those in-process calls.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.embedding")
router = APIRouter(prefix="/api/embedding", tags=["embedding"])


# 'privatae' is a LANE name (managed vs the user's own 'local' bastion), not a
# model. There is NO default model/dim: the active model is 100% config-driven
# (behavioral_params, written here by apply_embedding_switch; genesis seeds it
# at provision). Unconfigured → model=None and embedding stays off until the
# UI/host sets one — a fallback literal would name a model the entity's
# endpoint may not serve.
DEFAULT_PROVIDER = "privatae"   # entity pods run no local Ollama


def _normalize_provider(p: str | None) -> str:
    """User-facing provider for embeddings. 'local' (the user's own machine)
    and registry provider ids (openai, together, … — the provider-key lane,
    2026-08-10) pass through; any platform backend name (a GPU host, runpod,
    ollama) is an implementation detail → 'privatae'."""
    v = (p or "").strip().lower()
    if v == "local":
        return "local"
    from server.vault.providers import PROVIDERS
    return v if v in PROVIDERS else "privatae"


async def _load_bp(pool, entity_id) -> dict:
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id,
    )
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except Exception:
            bp = {}
    return bp if isinstance(bp, dict) else {}


@router.get("/current", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def current():
    """Return the entity's currently-configured embedding provider + model + dim
    — the SAME field the engine embeds with. When the host is serving its own
    embeddings on a bastion (provider 'local'), also returns the bastion IDENTITY
    (`bastion`, the hostname — the persisted truth) and embedding_base_url
    resolved LIVE from that bastion's latest heartbeat, so what a UI shows is
    what the engine will actually dial. The Network app's 'serving your
    embeddings' badge matches on `bastion`, not on a brittle URL compare."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    if pool is None or entity_id is None:
        return {"provider": None, "model": None, "dim": None, "bastion": None,
                "embedding_base_url": None, "configured": False}
    bp = await _load_bp(pool, entity_id)
    model = (bp.get("embedding_model") or "").strip() or None
    if not model:
        # Truthfully unconfigured — never report a made-up default model.
        return {"provider": None, "model": None, "dim": None, "bastion": None,
                "embedding_base_url": None, "configured": False}
    provider = _normalize_provider(bp.get("embedding_provider") or DEFAULT_PROVIDER)
    from mastercode.memory_engine import model_dim
    bastion = (bp.get("embedding_bastion") or "").strip() or None
    base_url = bp.get("embedding_base_url") or None
    if bastion:
        # Identity is truth; the endpoint is derived. A re-enrolled bastion
        # (new tunnel IP) resolves to its NEW address here — never the pin.
        from mastercode.embed_backends import resolve_endpoint_for_hostname
        live = await resolve_endpoint_for_hostname(pool, entity_id, bastion)
        if live:
            base_url = live
    dim = bp.get("embedding_dim") or model_dim(model)
    return {"provider": provider, "model": model, "dim": dim, "bastion": bastion,
            "embedding_base_url": base_url, "configured": True}


async def apply_embedding_switch(pool, entity_id, model: str,
                                 provider: str = DEFAULT_PROVIDER,
                                 base_url: str | None = None,
                                 dim: int | None = None,
                                 cred_name: str | None = None,
                                 bastion: str | None = None,
                                 request_dimensions: int | None = None) -> dict:
    """Persist the entity's embedding model (the field the engine reads). The ONE
    write path for embedding selection — used by POST /switch and by the unified
    routing surface (inference.set_role_model role='embedding'). Returns the switch
    result, or {'error': ...} on a bad/unknown model.

    Switching NEVER re-embeds and NEVER dumps: storage is multi-dim (a column per
    dim), so this only re-points which column the engine reads/writes. The result's
    `rows_missing_active_dim` reports how many active rows lack a vector in the new
    model's dim column, so a caller can CHOOSE to fill it with an explicit embed_jobs
    run — this function enqueues nothing.

    Phase-3a (s2-embed-byo): when `base_url` is given, this is a BRING-YOUR-OWN
    embedding endpoint. The catalog gate is RELAXED (the model lives on the host's
    own server, not the platform catalog) — but the dim MUST still resolve to a
    real entity_memory column (768/1024/2048/2560/4096) so the vector has a home.
    The BYO base_url + optional sealed-credential pointer are persisted alongside
    the model; the engine reads them via _byo_embedding_config. embedding is a
    non-prompt role, so a BYO endpoint here is leak-safe.

    `bastion` (with base_url): the bastion lane. Persists the bastion's HOSTNAME
    as the identity the engine re-resolves the live endpoint from on every embed
    (resolve_endpoint_for_hostname) — base_url is just the address at switch
    time. Without `bastion`, base_url is a pure-BYO fixed endpoint.
    """
    model = (model or "").strip()
    provider = _normalize_provider(provider)
    base_url = (base_url or "").strip() or None
    if not model:
        return {"error": "model required"}
    if pool is None or entity_id is None:
        return {"error": "brain pool unavailable"}

    from mastercode.memory_engine import (
        EMBEDDING_COLUMN_BY_DIM, model_dim,
        probe_embedding_endpoint,
    )

    if base_url is not None:
        # BYO path: relax the platform-catalog gate. Resolve dim from the caller
        # (explicit), else the model's known dim, else default 1024 — but it MUST
        # map to a real content column or there's nowhere to store the vector.
        if not (base_url.startswith("http://") or base_url.startswith("https://")
                or base_url.startswith("bastion-embed://")):
            return {"error": f"embedding base_url must be an http(s) or bastion-embed:// URL, got {base_url!r}"}
        target_dim = int(dim) if dim else model_dim(model)
        if target_dim not in EMBEDDING_COLUMN_BY_DIM:
            return {"error": (f"BYO embedding dim {target_dim} has no storage column "
                              f"(supported: {sorted(EMBEDDING_COLUMN_BY_DIM)})")}
        target_column = EMBEDDING_COLUMN_BY_DIM[target_dim]

        # HARD GATE: a host/agent may only assign a model the
        # endpoint can actually SERVE. Probe it with the target model over the same
        # path real embeds use; reject if it can't serve at the right dim. Without
        # this, an agent could point a host at a bastion that doesn't run the
        # model — silently embedding at the wrong model/dim and split-braining
        # memory search. (Managed/catalog models take base_url=None and are gated
        # by the catalog check below, not this probe.) Bastion targets are
        # overlay-only → the probe rides the Hub's host→peer forward, exactly
        # like the production embeds it stands in for.
        peer_forward = None
        if bastion:
            import os as _os
            peer_forward = (_os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129"
        ok, why = await probe_embedding_endpoint(
            base_url, model, target_dim,
            cred_name=cred_name, peer_forward=peer_forward,
            dimensions=request_dimensions,
            entity_id=entity_id, pool=pool)
        if not ok:
            return {"error": f"endpoint {base_url} can't serve embedding model "
                             f"'{model}': {why}"}
    else:
        # Validate against the catalog so a typo can't silently embed at the wrong dim.
        cat = await catalog()
        valid = [m for m in (cat.get("embedding_models") or []) if m.get("id") == model]
        if not valid:
            return {"error": f"model {model!r} not in embedding catalog"}
        target = valid[0]
        # The catalog is the authority on the model's dim — never guess. A
        # wrong dim silently routes vectors into the wrong column.
        target_dim = target.get("output_dim") or model_dim(model)
        if target_dim not in EMBEDDING_COLUMN_BY_DIM:
            return {"error": (f"catalog model {model!r} reports dim {target_dim}, "
                              f"which has no storage column "
                              f"(supported: {sorted(EMBEDDING_COLUMN_BY_DIM)})")}
        target_column = EMBEDDING_COLUMN_BY_DIM[target_dim]

    # Persist into behavioral_params (read-modify-write — shared JSONB).
    bp = await _load_bp(pool, entity_id)
    bp["embedding_provider"] = provider
    bp["embedding_model"] = model
    # ALWAYS persist the dim next to the model: the reader
    # (memory_engine.active_embedding_column) prefers this configured dim, so a
    # catalog model the static MODEL_DIMS table has never heard of still routes
    # into the right column. Config-driven end to end.
    bp["embedding_dim"] = target_dim
    if base_url is not None:
        bp["embedding_base_url"] = base_url
        # Bastion lane: persist the IDENTITY (hostname) as the source of truth.
        # base_url above is only the address at switch time — the embed reader
        # re-resolves the live endpoint from this identity every call, so a
        # re-enroll (new tunnel IP) self-heals instead of stranding embeds on a
        # dead pinned address. A pure-BYO endpoint (no bastion) keeps base_url
        # as its fixed, user-owned URL — identity cleared.
        if bastion:
            bp["embedding_bastion"] = bastion
        else:
            bp.pop("embedding_bastion", None)
        if cred_name:
            bp["embedding_cred_name"] = cred_name
        else:
            bp.pop("embedding_cred_name", None)
        # MRL truncation request — persisted ONLY when the probe above proved
        # the endpoint honors it, so every later production embed sends the
        # same `dimensions` the probe verified.
        if request_dimensions:
            bp["embedding_dimensions"] = int(request_dimensions)
        else:
            bp.pop("embedding_dimensions", None)
    else:
        # Switching back to a platform/catalog model clears any BYO endpoint.
        bp.pop("embedding_base_url", None)
        bp.pop("embedding_bastion", None)
        bp.pop("embedding_cred_name", None)
        bp.pop("embedding_dimensions", None)
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params=$1::jsonb WHERE id=$2",
        json.dumps(bp), entity_id,
    )

    # A switch NEVER re-embeds and NEVER dumps. Storage is multi-dim: each model's
    # vectors live in their own dim column (768/1024/2048/2560/4096), so selecting a
    # model is a pointer change — the active-column router (memory_engine) reads only
    # the current model's column, and every prior-model vector stays put (switching
    # BACK to a model already embedded is instant, zero work). We only MEASURE the
    # coverage gap — active rows that have no vector in the TARGET dim column yet — and
    # report it so a caller can CHOOSE to fill it with an explicit embed_jobs run. We
    # do not enqueue anything here. (Switching must not re-embed; never dump.)
    rows_missing = 0
    if target_column in EMBEDDING_COLUMN_BY_DIM.values():
        rows_missing = await pool.fetchval(f"""
            SELECT COUNT(*) FROM entity_memory
             WHERE entity_id = $1 AND status = 'active' AND {target_column} IS NULL
        """, entity_id) or 0

    return {
        "provider": provider,
        "model": model,
        "dim": target_dim,
        "target_column": target_column,
        "queued_job_id": None,                 # a switch never queues a re-embed
        "rows_missing_active_dim": int(rows_missing),
    }


@router.post("/switch", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def switch_model(payload: dict):
    """Switch the entity's embedding model (+ provider). Re-points the active dim
    column only — never re-embeds, never dumps existing vectors.
    Body: {model: <id from catalog>, provider?: 'privatae'|'local'}.
    BYO (Phase-3a): {model, base_url, dim?, cred_name?} embeds against the host's
    OWN endpoint — the catalog gate is relaxed (see apply_embedding_switch)."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    _dim = payload.get("dim")
    return await apply_embedding_switch(
        pool, entity_id,
        model=(payload.get("model") or ""),
        provider=(payload.get("provider") or DEFAULT_PROVIDER),
        base_url=payload.get("base_url"),
        dim=int(_dim) if _dim else None,
        cred_name=payload.get("cred_name"),
    )


@router.get("/catalog", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def catalog():
    """Return the platform's embedding catalog (provider reported user-facing).
    Asks the bridge for the platform side; returns [] when the bridge is down.
    """
    from server import main as _server_main
    client = _server_main.get_bridge_client()
    remote_models = []
    if client is not None:
        try:
            envelope = await client.request_full("embeddings.catalog", {})
            if envelope and envelope.get("ok"):
                data = envelope.get("data") or {}
                for m in data.get("remote") or []:
                    remote_models.append({
                        "id": m.get("id"),
                        "name": m.get("name"),
                        "provider": "privatae",   # platform-served, whatever the backend
                        "output_dim": m.get("output_dim"),
                        "parameter_size": m.get("parameter_size"),
                        "market_microdollars_per_M": m.get("market_microdollars_per_M"),
                        "charge_microdollars_per_M": m.get("charge_microdollars_per_M"),
                        "discount_pct": m.get("discount_pct"),
                    })
        except Exception as exc:
            logger.warning("bridge embeddings.catalog failed: %s", exc)

    # No pod-local Ollama — the platform catalog is the only path.
    return {"embedding_models": remote_models}


# ── Bastion embed lane ───────────────────────────────────────────────
# A host can serve its OWN embeddings on its OWN bastion (their GPU), keeping its
# OWN model. The platform-managed catalog (above) is the default until a host
# allocates a model through its bastion via these routes. Switching to/from a
# bastion KEEPS the current model — no re-embed, existing (paid) vectors untouched.

@router.get("/bastions", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def embed_bastions():
    """The host's ONLINE, embed-capable bastions — the choosable targets to serve
    this host's embeddings on its own hardware. Gated by read:inference_catalog
    (NOT read:topology): this is the embedding picker's backend list — the same
    surface that reads /current + /catalog — so every model-routing app that
    already carries read:inference_catalog (Privatae, OpenRouter Direct, Skills,
    plus Network + Context) can offer the bastion lane without a topology grant."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    if pool is None or entity_id is None:
        return {"bastions": []}
    from mastercode import embed_backends
    return {"bastions": await embed_backends.list_embed_bastions(pool, entity_id)}


@router.post("/use-bastion", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def use_bastion(payload: dict):
    """Route this host's embeddings to its bastion `server` (hostname).
    Body: {server, model?, dim?, cred_name?} — `model` omitted keeps the current
    model (lane move only); given, serves THAT model on the bastion (probe-gated:
    the box must actually run it). Either way NOTHING re-embeds or dumps —
    switching is a pointer change over multi-dim storage."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    from mastercode import embed_backends
    p = payload or {}
    _dim = p.get("dim")
    return await embed_backends.set_embed_bastion(
        pool, entity_id, p.get("server", ""),
        cred_name=p.get("cred_name"),
        model=(p.get("model") or "").strip() or None,
        dim=int(_dim) if _dim else None)


@router.post("/use-managed", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def use_managed():
    """Fall back to the platform-managed embedder for the host's CURRENT model
    (clears the bastion endpoint; no re-embed)."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    from mastercode import embed_backends
    return await embed_backends.clear_embed_bastion(pool, entity_id)


# ── Provider embed lane ──────────────────────────────────────────────
# Embeddings on a PROVIDER the user already keys in the Keys app — the
# provider's OpenAI-wire POST /embeddings under the vault-brokered key.
# Replaces the platform-managed "Privatae Endpoint" pick in the Orchestrator
# (the bridge catalog serves zero models; several keyed providers serve real
# embedding models). Rides the EXISTING BYO lane end to end: base_url = the
# provider registry base, cred_name = the resolved Keys entry (the raw key
# never reaches the embed layer; the brokered call honors the governed egress
# proxy), dim measured by a real probe embed, switch gated on it. Models whose
# native dim has no storage column are stored via the API's MRL `dimensions`
# param — only when a probe PROVES the endpoint honors it.

# Best-fit first: try the largest column dim below the model's native dim.
_MRL_CANDIDATE_DIMS = (2048, 1024, 768)


def _looks_like_embedding_model(row: dict) -> bool:
    """Embedding rows in a provider's /models: an explicit type (Together
    marks type='embedding'), else an id that names embedding ('embed' covers
    text-embedding-3-*, qwen3-embedding, bge/gte 'embedding' variants…). A
    heuristic ONLY for narrowing the picker list — the probe is the gate."""
    t = str(row.get("type") or "").lower()
    if t:
        return t in ("embedding", "embeddings")
    return "embed" in str(row.get("id") or "").lower()


@router.get("/provider-models/{provider}", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def embed_provider_models(provider: str):
    """Embedding-capable models `provider` serves under the user's key, from
    its own /models (shared _direct_models cache). Empty + error when there is
    no key or the catalog is unreachable — shown honestly, never invented."""
    from server.vault.providers import PROVIDERS
    p = (provider or "").strip().lower()
    spec = PROVIDERS.get(p)
    if not spec or not spec.get("embeddings"):
        return {"provider": p, "models": [],
                "error": f"{p or '(none)'} does not serve embeddings"}
    from server.routes.inference import _direct_models
    rows, err = await _direct_models(p)
    models = [r for r in rows if isinstance(r, dict) and _looks_like_embedding_model(r)]
    out: dict = {"provider": p, "models": models}
    if err:
        out["error"] = err
    return out


@router.post("/use-provider", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def use_provider(payload: dict):
    """Route this host's embeddings to a keyed provider's /embeddings.
    Body: {provider, model}. The switch is measured, never assumed: one real
    probe embed discovers the model's dim; a native dim with no storage column
    falls back to MRL truncation (the API's `dimensions` param) at the best
    fitting column dim, and only a dim the endpoint PROVABLY serves is
    persisted. Nothing re-embeds, nothing is dumped — pointer change over
    multi-dim storage, same as every other embedding switch."""
    from mastercode.memory_engine import (
        EMBEDDING_COLUMN_BY_DIM, measure_embedding_dim)
    from server.vault.providers import PROVIDERS, resolve
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    p = (payload.get("provider") or "").strip().lower()
    model = (payload.get("model") or "").strip()
    spec = PROVIDERS.get(p)
    if not spec or not spec.get("embeddings"):
        return {"error": f"{p or '(none)'} does not serve embeddings"}
    if not model:
        return {"error": "model required"}
    if pool is None or entity_id is None:
        return {"error": "brain pool unavailable"}
    cred = None
    try:
        cred = await resolve(pool, entity_id, p, ("api_key",),
                             actor="embedding-use-provider")
    except Exception:
        cred = None
    if not cred or not cred.secret:
        return {"error": f"no {p} key in the Keys app"}
    base_url = spec["base_url"].rstrip("/")

    native, why = await measure_embedding_dim(
        base_url, model, cred_name=cred.name, entity_id=entity_id, pool=pool)
    if native is None:
        return {"error": f"{p} can't embed '{model}': {why}"}
    dim, request_dimensions = native, None
    if native not in EMBEDDING_COLUMN_BY_DIM:
        for cand in _MRL_CANDIDATE_DIMS:
            if cand >= native:
                continue
            got, _ = await measure_embedding_dim(
                base_url, model, cred_name=cred.name, dimensions=cand,
                entity_id=entity_id, pool=pool)
            if got == cand:
                dim, request_dimensions = cand, cand
                break
        else:
            return {"error": (
                f"'{model}' embeds at {native}d — no storage column for that "
                f"dim (supported: {sorted(EMBEDDING_COLUMN_BY_DIM)}) and the "
                f"endpoint doesn't honor MRL truncation")}
    return await apply_embedding_switch(
        pool, entity_id, model=model, provider=p, base_url=base_url,
        dim=dim, cred_name=cred.name, request_dimensions=request_dimensions)
