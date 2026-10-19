"""
Media embedding — the entity-side surface for the `media_embedding` role.

The exact shape of server/routes/embedding.py, one role over: ONE source
of truth in behavioral_params (media_embedding_model + provider + dim + wire),
written only by apply_media_embedding_switch, read only by
media_engine.media_config. Setting it is probe-gated against the user's own
endpoint; there is no catalog, no default, and no platform media embedder.

WHY A SEPARATE SURFACE rather than role='media_embedding' inside embedding.py:
the text switch resolves a dim and stops. A media switch has to resolve a dim
AND a request shape AND prove the model's text tower lands in the same space
(media_engine.probe_media_endpoint) — three measured facts, not one. Folding
that into apply_embedding_switch would put a media-only branch through every
text call site for no gain.

Permissions mirror embedding.py exactly: reads need read:inference_catalog,
writes need write:inference_routing.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.media_embedding")
router = APIRouter(prefix="/api/media-embedding", tags=["media-embedding"])


async def _load_bp(pool, entity_id) -> dict:
    """behavioral_params as a dict. The column is JSONB and NULLABLE with no
    default (init-db.sql:67), so a fresh entity reads None — which the `or {}`
    covers. Duplicated from embedding.py rather than imported so this module
    has no import-time dependency on the text surface."""
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id)
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except Exception:
            bp = {}
    return bp if isinstance(bp, dict) else {}


@router.get("/current",
            dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def current():
    """What this host embeds pictures with, or configured=false.

    configured=false means NOT SET UP YET — never an outage, never a failure.
    A picture on an unconfigured host is stored and findable by its
    filename; it is simply not searchable by what is in it.
    """
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    if pool is None:
        return {"provider": None, "model": None, "dim": None, "wire": None,
                "bastion": None, "media_base_url": None, "configured": False}
    from mastercode.media_engine import media_config
    cfg = await media_config(pool, entity_id)
    if not cfg:
        return {"provider": None, "model": None, "dim": None, "wire": None,
                "bastion": None, "media_base_url": None, "configured": False}
    return {"provider": cfg.get("provider"), "model": cfg["model"],
            "dim": cfg["dim"], "wire": cfg["wire"], "bastion": cfg.get("bastion"),
            "media_base_url": cfg["base_url"], "configured": True}


@router.get("/dims",
            dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def dims():
    """Which vector widths this host can STORE a media vector at, and which one
    is active. A model whose output width has no column here cannot be assigned
    unless the endpoint honours truncation down to one that does."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    from mastercode.media_engine import (
        MEDIA_COLUMN_BY_DIM, MEDIA_MRL_CANDIDATE_DIMS, active_media_column)
    col, dim = (None, None)
    if pool is not None:
        col, dim = await active_media_column(pool, entity_id)
    return {
        "storable_dims": sorted(MEDIA_COLUMN_BY_DIM),
        "columns": {str(d): c for d, c in sorted(MEDIA_COLUMN_BY_DIM.items())},
        "truncation_targets": MEDIA_MRL_CANDIDATE_DIMS,
        "active_column": col,
        "active_dim": dim,
    }


async def apply_media_embedding_switch(
        pool, entity_id, model: str, base_url: str,
        provider: str | None = None,
        cred_name: str | None = None,
        bastion: str | None = None) -> dict:
    """THE write path for media embedding selection. Probes, then persists.

    Nothing is persisted that was not measured. The probe embeds a real picture
    and then the same model's text tower, and only a (wire, dim) pair the
    endpoint actually served at is written. A model whose native width has no
    storage column is retried with truncation, largest candidate first — and
    only a truncated width the endpoint PROVES it serves is taken.

    Switching never re-embeds and never dumps: media storage is one column per
    width, so this is a pointer change. `rows_missing_media` reports how many
    pictures have no vector in the new column so the caller can CHOOSE to fill
    it — this function enqueues nothing, exactly like the text switch.
    """
    from mastercode.media_engine import (
        MEDIA_COLUMN_BY_DIM, MEDIA_MRL_CANDIDATE_DIMS, probe_media_endpoint)

    model = (model or "").strip()
    base_url = (base_url or "").strip()
    if not model:
        return {"error": "model required"}
    if not base_url:
        return {"error": "base_url required — there is no platform media embedder"}
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return {"error": f"media base_url must be an http(s) URL, got {base_url!r}"}
    if pool is None or entity_id is None:
        return {"error": "brain pool unavailable"}

    peer_forward = None
    if bastion:
        import os as _os
        peer_forward = (_os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129"

    wire, dim, why = await probe_media_endpoint(
        base_url, model, cred_name=cred_name, peer_forward=peer_forward,
        entity_id=entity_id, pool=pool)
    if not wire:
        return {"error": f"endpoint {base_url} can't serve media model '{model}': {why}"}

    request_dimensions = None
    if dim not in MEDIA_COLUMN_BY_DIM:
        # Native width has no home. Ask the endpoint to truncate, largest
        # candidate first, and believe only what it actually returns.
        native = dim
        for cand in MEDIA_MRL_CANDIDATE_DIMS:
            if cand >= native:
                continue
            w2, d2, _ = await probe_media_endpoint(
                base_url, model, cred_name=cred_name, peer_forward=peer_forward,
                dimensions=cand, entity_id=entity_id, pool=pool)
            if w2 and d2 == cand:
                wire, dim, request_dimensions = w2, cand, cand
                break
        else:
            return {"error": (
                f"'{model}' embeds pictures at {native}d — no storage column for "
                f"that width (have {sorted(MEDIA_COLUMN_BY_DIM)}) and the endpoint "
                f"does not honour truncation")}

    target_column = MEDIA_COLUMN_BY_DIM[dim]

    # Read-modify-write of the shared JSONB, matching embedding.py:240-243. The
    # media keys are disjoint from the text ones, so the two lanes never fight
    # over a value — but they do share the column, so this inherits the same
    # last-writer-wins window the text switch has. Narrowing that is a change to
    # both lanes (harness_state.py:120 shows the jsonb_set form) and not this
    # ticket's to make unilaterally.
    bp = await _load_bp(pool, entity_id)
    bp["media_embedding_model"] = model
    bp["media_embedding_base_url"] = base_url
    bp["media_embedding_dim"] = dim
    bp["media_embedding_wire"] = wire
    if provider:
        bp["media_embedding_provider"] = provider
    else:
        bp.pop("media_embedding_provider", None)
    if bastion:
        bp["media_embedding_bastion"] = bastion
    else:
        bp.pop("media_embedding_bastion", None)
    if cred_name:
        bp["media_embedding_cred_name"] = cred_name
    else:
        bp.pop("media_embedding_cred_name", None)
    if request_dimensions:
        bp["media_embedding_dimensions"] = int(request_dimensions)
    else:
        bp.pop("media_embedding_dimensions", None)
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params=$1::jsonb WHERE id=$2",
        json.dumps(bp), entity_id)

    # How many stored pictures have no vector in the new column. Counted the
    # same way the media fill job selects, so the number a caller sees is the
    # number that job would do.
    from server.routes.context import user_context_exists
    from mastercode.media_engine import media_stale_sql
    rows_missing = 0
    if await user_context_exists(pool):
        # The SAME predicate the fill uses — this comment claims the number is
        # what that job would do, so it has to be. Keyed on the column alone it
        # reported 0 right after a same-width model switch while the fill had
        # every row still to do.
        rows_missing = await pool.fetchval(f"""
            SELECT COUNT(*) FROM user_context
             WHERE entity_id = $1
               AND metadata->>'kind' = 'file'
               AND metadata->>'mime' LIKE 'image/%'
               AND status != 'dismissed'
               AND {media_stale_sql(target_column, '$2')}
        """, entity_id, model) or 0

    return {"provider": provider, "model": model, "dim": dim, "wire": wire,
            "target_column": target_column, "bastion": bastion,
            "queued_job_id": None,
            "rows_missing_media": int(rows_missing),
            "probe": why}


@router.post("/switch",
             dependencies=[Depends(requires_permission("write:inference_routing"))])
async def switch_model(payload: dict):
    """Point this host's picture embedding at an endpoint the user provides.
    Body: {model, base_url, provider?, cred_name?, bastion?}. Probe-gated."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    return await apply_media_embedding_switch(
        pool, entity_id,
        model=(payload.get("model") or ""),
        base_url=(payload.get("base_url") or ""),
        provider=(payload.get("provider") or None),
        cred_name=payload.get("cred_name"),
        bastion=payload.get("bastion"))


@router.post("/use-provider",
             dependencies=[Depends(requires_permission("write:inference_routing"))])
async def use_provider(payload: dict):
    """Point picture embedding at a keyed provider's endpoint.
    Body: {provider, model}. The key is brokered from the Keys app and never
    reaches the embed layer; the wire shape and width are both measured."""
    from server.vault.providers import PROVIDERS, resolve
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    p = (payload.get("provider") or "").strip().lower()
    model = (payload.get("model") or "").strip()
    spec = PROVIDERS.get(p)
    # Same shape as the text lane (embedding.py:436): a provider that serves no
    # embeddings at all is refused by NAME rather than after a probe round-trip,
    # so the message says what is wrong instead of "the endpoint did not answer".
    if not spec or not spec.get("embeddings"):
        return {"error": f"{p or '(none)'} does not serve embeddings"}
    if not model:
        return {"error": "model required"}
    if pool is None:
        return {"error": "brain pool unavailable"}
    try:
        cred = await resolve(pool, entity_id, p, ("api_key",),
                             actor="media-embedding-use-provider")
    except Exception:
        cred = None
    if not cred or not cred.secret:
        return {"error": f"no {p} key in the Keys app"}
    base_url = (spec.get("base_url") or "").rstrip("/")
    if not base_url:
        return {"error": f"{p} has no base_url in the provider registry"}
    return await apply_media_embedding_switch(
        pool, entity_id, model=model, base_url=base_url,
        provider=p, cred_name=cred.name)


@router.post("/clear",
             dependencies=[Depends(requires_permission("write:inference_routing"))])
async def clear():
    """Unset media embedding. Existing vectors are LEFT IN PLACE — clearing the
    route is a pointer change, and re-pointing at the same model later finds its
    column already filled. Never dumps (feedback: never migrate or discard
    embeddings the user paid for)."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    if pool is None:
        return {"error": "brain pool unavailable"}
    bp = await _load_bp(pool, entity_id)
    for k in ("media_embedding_model", "media_embedding_base_url",
              "media_embedding_dim", "media_embedding_wire",
              "media_embedding_provider", "media_embedding_bastion",
              "media_embedding_cred_name", "media_embedding_dimensions"):
        bp.pop(k, None)
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params=$1::jsonb WHERE id=$2",
        json.dumps(bp), entity_id)
    return {"configured": False, "vectors_kept": True}
