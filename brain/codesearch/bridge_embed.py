"""
bridge_embed — the in-pod engine's embed + rerank on the CONFIGURED lane.

Code-search embeds ENTITY data inside the entity pod, so it takes the entity's
configured embedding backend like every other in-pod embedder — 100%
config-driven, NO cross-lane fallback: a BYO/bastion backend when the host
configured one (memory_engine._byo_embedding_config → _embed_via_byo, the Hub
peer-forward lane), the platform bridge (embeddings.embed) ONLY when no
BYO/bastion backend is set.

The MODEL and its SIZE are the Host's configured ones too — the same config
memory embeds with (entity_registry.behavioral_params: embedding_model and
embedding_dim, written by apply_embedding_switch). active_space() reads them
and names the code_chunks column that size lives in (store.COLUMN_BY_DIM,
v231), as memory_engine.active_embedding_column does for entity_memory. There
is no model or size of codesearch's own and no default: with no model set up
the index is keyword-only and says so ("embeddings not set up yet"), which is
a setup step, never an outage. Until 2026-10-16 this module embedded with
qwen3-embedding:4b into a 2560 column whatever the Host had configured, so a
Host whose embedder is nomic-embed-text (768) on its own machine could not
index code by meaning at all.

embeddings.embed is single-text, so a "batch" is just bounded-concurrent single
calls; true GPU batching is a deferred onboarding-time optimization.

RERANK OBEYS THE SAME LANE, and it matters more than embed does. `rerank` used
to skip lane resolution entirely and post straight to the bridge, so on a
BYO/bastion host the query embedding rode the private lane and then the rerank
step shipped the query PLUS up to CODESEARCH_RERANK_CANDIDATES chunks of raw
source (searcher.py hands it `h["content"]`) to platform compute — plaintext,
not vectors, which is strictly worse than the vector leak the no-cross-lane
rule exists to prevent. There is no BYO rerank to route to instead: bastions
advertise `embed` / `embed-relay` only (embed_backends), and the bridge op is a
TEI cross-encoder that REQUIRES the documents (op_embeddings_rerank rejects a
docs-less call 400), so query-only is not a shape it can take. A configured
BYO/bastion lane therefore means rerank returns None — search keeps its RRF
order (searcher.search guards on `scores is not None`), which costs ranking
quality, not function.

The lane must also fail CLOSED. When the lane cannot be determined at all —
brain pool not up, behavioral_params unreadable — this module used to assume
the bridge, so one transient pod-DB error was enough to route a BYO host's
content to platform compute. `_resolve_lane` now reports that as a down
backend, which every consumer here already treats as "return None, never the
bridge".
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from . import store

log = logging.getLogger("codesearch.bridge_embed")

_CONCURRENCY = int(os.environ.get("CODESEARCH_EMBED_CONCURRENCY", "8"))


def _client():
    """The in-pod bridge client ({op,params} over mTLS), or None if unavailable."""
    try:
        from server import main as _server_main
        return _server_main.get_bridge_client()
    except Exception:
        return None


# The lane could not be DETERMINED (brain pool not up, behavioral_params
# unreadable). Shaped as a "configured but down" backend on purpose: every
# consumer in this module already branches on `backend_down` → return None,
# never the bridge. Undetermined must fail CLOSED — the alternative is that one
# transient pod-DB error silently promotes the platform bridge to the lane for a
# host that configured a BYO/bastion backend, which is the leak with extra steps.
# Recovery is bounded: the searcher just degrades for that call, and the indexer
# skips null-vector chunks WITHOUT recording their file hash (store.upsert_chunks
# / indexer.index_project), so the next incremental pass re-indexes them.
_LANE_UNDETERMINED: dict = {"backend_down": True, "reason": "lane-undetermined"}


async def _resolve_lane() -> tuple[Optional[dict], object, object]:
    """Resolve the entity's configured embed lane ONCE for a batch. Returns
    (byo_config_or_None, pool, entity_id): a truthy dict = BYO/bastion is the
    chosen backend (possibly flagged backend_down); None = no BYO configured,
    the bridge lane is legitimate. A lane we could not read is NOT None — it is
    _LANE_UNDETERMINED, i.e. down."""
    try:
        from server import brain_service as _bs
        from mastercode.memory_engine import _byo_embedding_config
        pool = _bs.get_pool()
        eid = getattr(_bs, "_entity_id", None)
        if pool is None or eid is None:
            log.warning("embed lane undetermined (no brain pool/entity) — lane DOWN "
                        "this call, no bridge fallback")
            return _LANE_UNDETERMINED, None, None
        byo = await _byo_embedding_config(pool, eid)
        return (byo or None), pool, eid
    except Exception as e:
        log.warning("embed lane resolution failed (%s) — lane DOWN this call, "
                    "no bridge fallback", e)
        return _LANE_UNDETERMINED, None, None


# What a search or an index pass says when there is no space to embed into.
# The first two are memory's own sentences (memory_retrieval._embed_query):
# no model is a setup step, never an outage; a config that could not be read
# is not a setup step.
NOT_SET_UP = ("embeddings not set up yet — this host has no embedding model. Nothing "
              "is broken: one can be set up on a machine the user runs "
              "(embedding_route op=bastions) or with a provider they hold a key for "
              "(Orchestrator → Memory)")
CONFIG_UNREADABLE = ("the embedding configuration could not be read just now (a "
                     "transient error, not a setup problem)")


async def active_space() -> tuple[Optional[store.Space], str]:
    """The Host's configured embedding model and its size, as the code_chunks
    column they live in: (Space, "") — or (None, the sentence saying why
    there is none). Read from entity_registry.behavioral_params, the one
    config memory embeds with: embedding_model, and embedding_dim, which
    apply_embedding_switch persists beside every model it sets. A host set up
    before that has only the model, whose size memory's own table names
    (memory_engine.MODEL_DIMS) — never a guessed one. No model, no space."""
    try:
        from server import brain_service as _bs
        from mastercode import memory_engine as _me
        bp = await _me._load_behavioral_params(_bs.get_pool(), _bs._entity_id)
    except Exception as e:  # noqa: BLE001 — unreadable is not "not set up"
        log.warning("code search: embedding config unreadable (%s)", type(e).__name__)
        return None, CONFIG_UNREADABLE
    if bp.get(_me._BP_UNREADABLE_KEY):
        return None, CONFIG_UNREADABLE
    model = (bp.get("embedding_model") or "").strip()
    if not model:
        return None, NOT_SET_UP
    try:
        dim = int(bp.get("embedding_dim") or 0) or None
    except (TypeError, ValueError):
        dim = None
    if dim is None:
        dim = _me.MODEL_DIMS.get(model) or _me.MODEL_DIMS.get(model.split(":", 1)[0])
    if dim not in store.COLUMN_BY_DIM:
        return None, (f"the embedding model {model} has "
                      + (f"{dim}-dimension vectors, a size" if dim else "no size on record,")
                      + " the code index has no column for "
                      f"(sizes: {', '.join(str(d) for d in sorted(store.COLUMN_BY_DIM))})")
    return store.Space(model=model, dim=dim), ""


async def _embed_via_bridge_op(text: str, model: str) -> Optional[list[float]]:
    """One embedding via the bridge embeddings.embed op. None on failure."""
    client = _client()
    if client is None:
        return None
    try:
        env = await client.request_full(
            "embeddings.embed", {"model": model, "input": (text or "")[:8000]})
    except Exception as e:
        # The exception's type, never its text: an exception or an envelope
        # can carry the words that were sent (asyncpg quotes an argument it
        # cannot encode; a provider may echo its input), and this log is kept
        # by the pod.
        log.warning("bridge embed failed (%s)", type(e).__name__)
        return None
    if env and env.get("ok"):
        return (env.get("data") or {}).get("vector")
    # The envelope's `code` when it is a short identifier; never its free-text
    # `error`, for the same reason.
    log.warning("bridge embed not ok%s", _code_of(env))
    return None


_CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _code_of(env) -> str:
    """' (<code>)' for a not-ok envelope whose `code` is a short identifier,
    else ''. Anything else in `code` (a sentence, the input) is not logged."""
    code = (env or {}).get("code") if isinstance(env, dict) else None
    if isinstance(code, int) and not isinstance(code, bool):
        code = str(code)
    return f" ({code})" if isinstance(code, str) and _CODE_RE.fullmatch(code) else ""


async def _embed_one(text: str, byo: Optional[dict], pool, eid,
                     space: store.Space) -> Optional[list[float]]:
    """One embedding with the space's model on the resolved lane. BYO/bastion
    configured → ONLY that lane (down/failed = None, never the bridge);
    otherwise the bridge op. A vector of another size than the configured one
    is None: it has no column here, and the configured size is wrong or the
    endpoint serves another model."""
    if byo:
        if byo.get("backend_down"):
            return None
        from mastercode.memory_engine import _embed_via_byo
        try:
            vec = await _embed_via_byo((text or "")[:8000], space.model, byo,
                                       entity_id=eid, pool=pool)
        except Exception as e:
            log.warning("BYO embed failed (no bridge fallback) (%s)", type(e).__name__)
            return None
    else:
        vec = await _embed_via_bridge_op(text, space.model)
    if vec is not None and len(vec) != space.dim:
        log.warning("embed: %s returned a %d-dimension vector where %d is configured — "
                    "not stored", space.model, len(vec), space.dim)
        return None
    return vec


async def embed(text: str, *, space: Optional[store.Space] = None) -> Optional[list[float]]:
    """One embedding with the configured model on the entity's configured
    lane (`space` when the caller already resolved it: active_space). None on
    failure, or when no model is set up."""
    if space is None:
        space, _why = await active_space()
        if space is None:
            return None
    byo, pool, eid = await _resolve_lane()
    return await _embed_one(text, byo, pool, eid, space)


async def embed_many(texts: list[str], *,
                     space: Optional[store.Space] = None) -> list[Optional[list[float]]]:
    """Embed a batch with the configured model (`space`, or active_space) —
    the lane is resolved ONCE, then bounded-concurrent single-text calls.
    Order-preserving, per-item None on failure; all None with no model set up."""
    if not texts:
        return []
    if space is None:
        space, _why = await active_space()
        if space is None:
            return [None for _ in texts]
    byo, pool, eid = await _resolve_lane()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(t: str) -> Optional[list[float]]:
        async with sem:
            return await _embed_one(t, byo, pool, eid, space)

    return list(await asyncio.gather(*[_one(t) for t in texts]))


async def rerank(query: str, docs: list[str]) -> Optional[list[Optional[float]]]:
    """Cross-encoder rerank ON THE CONFIGURED LANE. Per-doc scores in INPUT order,
    None for an omitted doc; None for the WHOLE call on failure (caller keeps the
    RRF order). Matches op_embeddings_rerank: data.results = [{index, score}].

    `docs` is raw chunk CONTENT, so the lane rule binds harder here than it does
    for embed. BYO/bastion configured (or the lane unreadable) → None, full stop:
    there is no BYO rerank endpoint to route to, and the bridge is never a
    fallback from a configured lane. The bridge is used ONLY when no BYO/bastion
    backend is set, i.e. when platform compute IS the entity's chosen backend."""
    if not docs:
        return None
    byo, _pool, _eid = await _resolve_lane()
    if byo:
        # Includes _LANE_UNDETERMINED. No cross-lane fallback: sending these
        # docs to the bridge would put the host's source on platform compute in
        # plaintext. The caller keeps the RRF order.
        log.info("rerank skipped: BYO/bastion is the configured lane "
                 "(no bridge fallback) — keeping RRF order for %d docs", len(docs))
        return None
    client = _client()
    if client is None:
        return None
    try:
        env = await client.request_full("embeddings.rerank", {"query": query, "docs": docs})
    except Exception as e:
        log.debug("bridge rerank failed: %s", e)
        return None
    if not (env and env.get("ok")):
        log.debug("bridge rerank not-ok: %s", (env or {}).get("error"))
        return None
    scores: list[Optional[float]] = [None] * len(docs)
    for r in (env.get("data") or {}).get("results") or []:
        i, s = r.get("index"), r.get("score")
        if isinstance(i, int) and 0 <= i < len(docs) and s is not None:
            scores[i] = float(s)
    return scores
