"""
Memory Engine — Dimension-tagged memory with personality-native retrieval.

Every piece of stored memory is scored across the 10 personality dimensions.
Retrieval uses the entity's personality weights as the query vector via pgvector,
so a high-warmth entity naturally surfaces emotionally relevant memories and
a high-depth entity naturally surfaces analytical ones.

The personality IS the retrieval function.
"""

import asyncio
import contextvars
import json
import logging
import time
import re
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

import os
import aiohttp
import asyncpg

log = logging.getLogger("cseries.mastercode.memory_engine")

# NO default embedding model. The active model is 100% config-driven
# (behavioral_params.embedding_model, written only by apply_embedding_switch;
# genesis seeds it at provision). A fallback literal here would embed against
# a model the entity's endpoint may not even serve — wrong-dim vectors, split-
# brained search (pins and fallbacks both break model switching).

# Multi-dim embedding columns — added by migration v044.
# Dim → DB column. Used by readers to pick the right pgvector column for
# the entity's currently-configured embedding model.
EMBEDDING_COLUMN_BY_DIM = {
    768:  "embedding_768",     # vector  (v044)
    1024: "embedding",         # vector  — default column, predates multi-dim (init)
    2048: "embedding_2048",    # halfvec (v049 column, halfvec+HNSW in v071)
    2560: "embedding_2560",    # halfvec — qwen3-embedding:4b (RunPod GPU) — v070
    4096: "embedding_4096",    # halfvec (v044 column; unindexed, >4000d HNSW cap)
}
# Every value MUST be a real entity_memory column (init-db.sql + migrations):
# 768 (v044), 1024 (init), 2048 (v049), 2560 (v070), 4096 (v044). Columns above
# 2000d are stored as HALFVEC so they can be HNSW-indexed — pgvector's HNSW caps
# `vector` at 2000d and `halfvec` at 4000d.
# Because casts and the `<=>` operator are type-specific, any SQL touching one of
# these columns MUST cast the embedding literal with embedding_cast_type(dim).

# Content columns whose pgvector type is HALFVEC (every >2000-dim column).
HALFVEC_DIMS = {2048, 2560, 4096}


def embedding_cast_type(dim: int) -> str:
    """SQL type to cast an embedding literal to for the content column of `dim`.
    halfvec for >2000d columns (HNSW-indexable to 4000d), vector otherwise.
    The `<=>` cosine operator and column assignment both require the literal's
    type to match the column type — never hardcode `::vector` for a content
    column whose dim might be >2000."""
    return f"halfvec({dim})" if dim in HALFVEC_DIMS else f"vector({dim})"

# Model → dim. Mirrors the catalog at /api/embeddings/catalog. New models
# must be registered here for the routing logic to know which column.
MODEL_DIMS = {
    "bge-m3": 1024,
    "bge-m3:latest": 1024,
    "mxbai-embed-large": 1024,
    "mxbai-embed-large:latest": 1024,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "snowflake-arctic-embed": 1024,
    "snowflake-arctic-embed:335m": 1024,
    "snowflake-arctic-embed:l": 1024,
    "qwen3-embedding:0.6b": 1024,
    "qwen3-embedding:4b": 2560,   # RunPod GPU / code index / onboarding model
    "qwen3-embedding": 4096,
    "qwen3-embedding:latest": 4096,
}


def embedding_column_for(model: str) -> str:
    """Return the pgvector column name that holds an embedding from `model`.
    Falls back to 'embedding' (1024d) when the model is unknown — same as
    the historical default. Prefer active_embedding_column, which reads the
    CONFIGURED dim and only consults this static hint as a last resort."""
    dim = MODEL_DIMS.get(model) or MODEL_DIMS.get(model.split(":", 1)[0]) or 1024
    return EMBEDDING_COLUMN_BY_DIM.get(dim, "embedding")


def model_dim(model: str) -> int:
    """Output dim for `model`. 1024 fallback."""
    return MODEL_DIMS.get(model) or MODEL_DIMS.get(model.split(":", 1)[0]) or 1024


# Dims pgvector can build an HNSW index for, by column type:
#   vector  columns (768/1024): vector_cosine_ops, HNSW cap 2000d
#   halfvec columns (2048/2560): halfvec_cosine_ops, HNSW cap 4000d
# 4096 is halfvec but exceeds the 4000d HNSW cap → unindexed (sequential scan).
# Its one reader in the repo is embed_backends.choose_bastion_embed_model
# (embed_backends.py:200), which prefers an indexable dim when picking which of
# a bastion's models to serve — an unindexable column is a sequential scan on
# every recall. It is NOT a read-path check: active_embedding_column routes on
# the configured dim and never consults this set. (It also used to gate
# ensure_embedding_index, which is gone — see the note below.)
_HNSW_INDEXABLE_DIMS = {768, 1024, 2048, 2560}


# ensure_embedding_index() USED TO LIVE HERE and built
# idx_entity_memory_<column>_hnsw lazily, the first time a dim column was
# used, "so its index exists without bloating fresh installs that never touch
# it". Removed because it was the last interpolated DDL
# in brain/, and because two measurements said it had nothing to do:
#
#   1. The set it could build is FINITE — EMBEDDING_COLUMN_BY_DIM intersected
#      with _HNSW_INDEXABLE_DIMS is exactly four indexes — and all four are
#      declared by a MIGRATION, so every host gets them by construction:
#      idx_entity_memory_embedding_hnsw (v151 — init-db.sql:233 declares it too,
#      but init-db runs only on an empty database, so it alone was not enough),
#      _768_ (v044:40, and init-db.sql — search the index NAME there, never a
#      line number: this one citation has been wrong THREE times, once pointing
#      into prose and twice because a comment added above it moved the
#      statement), _2048_ (v071:35), _2560_ (v070:34).
#   2. All four are PRESENT on every live host — 8 of 8 by NAME (pg_indexes;
#      one Host was up then and has since been scaled to 0, and
#      a name says nothing about shape — see the 768d note in init-db.sql). The
#      largest entity_memory in the fleet is 2,874 rows, so the "bloat" the
#      laziness avoided was an HNSW index over an empty or tiny column.
#
# It also had no callers anywhere in the repo — only its own definition and log
# strings matched a search. So this deletes a function that never ran, which
# could not have run usefully, and which would have been refused outright once
# the kernel's role is narrowed.


async def active_embedding_column(pool, entity_id) -> tuple[str, int]:
    """The pgvector content column + dim for the entity's CURRENT embedding
    model. The single router every content read/write site uses to pick which
    dim column to hit, so a non-1024 model stores into (and is searched in)
    the matching column instead of silently dropping its vector.

    Returns (column_name, dim). The column is always one of the
    EMBEDDING_COLUMN_BY_DIM whitelist values — safe to f-string into SQL.

    Fully config-driven: prefers the CONFIGURED embedding_dim (persisted by
    apply_embedding_switch alongside the model, so a catalog model the static
    MODEL_DIMS table has never heard of still routes right), then the model's
    known dim, then the 1024d base column — the schema's default column, only
    reachable for a legacy/unconfigured entity that also never embeds (no
    configured model → _get_memory_embedding returns None).
    """
    bp = await _load_behavioral_params(pool, entity_id)
    model = (bp.get("embedding_model") or "").strip()
    dim = None
    try:
        dim = int(bp.get("embedding_dim") or 0) or None
    except (TypeError, ValueError):
        dim = None
    if dim not in EMBEDDING_COLUMN_BY_DIM:
        dim = model_dim(model) if model else 1024
    return EMBEDDING_COLUMN_BY_DIM.get(dim, "embedding"), dim


async def _load_behavioral_params(pool, entity_id) -> dict:
    """Read + parse entity_registry.behavioral_params — the ONE config source
    for embedding model/provider/dim/base_url. {} when unreadable."""
    bp: dict = {}
    if pool is not None and entity_id is not None:
        try:
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id=$1",
                entity_id,
            )
            if row:
                bp = row["behavioral_params"] or {}
                if isinstance(bp, str):
                    try:
                        bp = json.loads(bp)
                    except Exception:
                        bp = {}
                if not isinstance(bp, dict):
                    bp = {}
        except Exception:
            # Fail open to {} for every existing caller, but MARK it: an
            # unreadable config is not an unconfigured host, and the recall
            # arm must not tell the user "nothing is broken" over a DB error.
            bp = {_BP_UNREADABLE_KEY: True}
    else:
        # No pool / entity yet (brain not initialised) is a NOT-READY state,
        # not a setup state — mark it the same way so a boot-time search says
        # "could not be read just now" rather than "not set up yet".
        bp = {_BP_UNREADABLE_KEY: True}
    return bp


# Marker key _load_behavioral_params sets when the read itself failed; popped
# by _entity_embedding_config into _embed_config_unreadable. Never persisted.
_BP_UNREADABLE_KEY = "__unreadable__"
_embed_config_unreadable: bool = False


def embed_config_unreadable() -> bool:
    """True when the most recent embedding-config read FAILED (as opposed to
    returned an unconfigured host). Readers check this before describing an
    empty model as a setup step."""
    return _embed_config_unreadable


# Entities we've already shouted about — warn once per process, not per turn.
_warned_unconfigured: set = set()


async def _entity_embedding_config(pool, entity_id: int) -> tuple[str, str]:
    """Read entity's chosen (provider, model) from behavioral_params — the ONE
    config source (UI/host-written via apply_embedding_switch; genesis seeds it
    at provision). NO fallback model: when unconfigured this returns model=""
    and embedding is disabled until the host/UI sets one — substituting a
    default literal would embed against a model the endpoint may not serve.

    Legacy 'local' entries in behavioral_params are auto-migrated to
    the platform backend on read.
    """
    bp = await _load_behavioral_params(pool, entity_id)
    # Distinguish "unreadable this call" from "unconfigured": _load_behavioral_
    # params fails open to {} and the two used to be indistinguishable, so a
    # configured host with a transient DB error read as "not set up yet" to
    # every caller of last_embed_model(). The marker is set by the loader; the
    # reader is embed_config_unreadable() (memory_retrieval consults it before
    # calling anything a setup problem).
    global _embed_config_unreadable
    _embed_config_unreadable = bool(bp.pop(_BP_UNREADABLE_KEY, False))
    model = (bp.get("embedding_model") or "").strip()
    if not model and not _embed_config_unreadable and entity_id not in _warned_unconfigured:
        # Once per entity, and only for a READ that succeeded and found no
        # model — an unreadable config is a different condition and must not
        # be logged as "unconfigured". Setup state, not a fault: the user
        # picks an endpoint (their machine via embedding_route use_bastion, or
        # a keyed provider in Orchestrator → Memory). No fallback model, ever.
        _warned_unconfigured.add(entity_id)
        log.warning(
            "entity %s has no embedding model set up yet "
            "(behavioral_params.embedding_model) — embeddings are off until the "
            "user picks an endpoint (their own machine or a keyed provider). "
            "Not a fault; no fallback model is ever substituted.", entity_id)
    # 'local' is no longer a supported provider. Auto-migrate to the platform backend.
    return ("platform", model)


async def _byo_embedding_config(pool, entity_id: int) -> dict:
    """Phase-3a (s2-embed-byo): read the entity's BYO embedding endpoint config
    from behavioral_params, if any. Four distinct returns so the caller can hold
    the config-driven-lane rule (no cross-lane fallback):
      - {}                                → NO BYO/bastion backend configured; the
                                            platform bridge endpoint is the chosen
                                            backend (the bridge lane is legitimate).
      - {"backend_down": True, "bastion"} → a BYO/bastion backend IS configured but
                                            currently has no live endpoint; the
                                            caller marks the lane down, NO bridge.
      - {"backend_down": True, "reason":
         "config-read-failed"}            → the config could not be READ, so the
                                            lane is UNKNOWN. Fails closed: unknown
                                            is treated as down, never as "no BYO".
      - {base_url, peer_forward?, cred_name?, dim?} → a usable BYO/bastion lane.
    A BYO endpoint lets the host embed against its OWN inference server
    (leak-guarded: embedding is a non-prompt role).

    Bastion lane (identity, not IP): when `embedding_bastion` is set, the
    persisted truth is the bastion's HOSTNAME and the endpoint is resolved LIVE
    from its latest heartbeat on every call — a re-enrolled bastion (new tunnel
    IP) self-heals instantly, and a stale pinned address can never strand
    embeds again. Resolution failing is LOUD."""
    if pool is None or entity_id is None:
        return {}
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id)
    except Exception as exc:
        # The lane is UNDETERMINED, not absent. `{}` here used to mean "no BYO
        # configured → the bridge is legitimate", so a transient pod-DB blip was
        # enough to route a bastion host's data to platform compute — the lane
        # rule failing OPEN. Report it as a down backend instead: every caller
        # already treats backend_down as "return None, never the bridge", and the
        # unembedded-backfill sweep re-embeds those rows once the DB answers.
        log.warning("embed lane config unreadable for entity %s (%s) — lane DOWN "
                    "this call (no bridge fallback)", entity_id, exc)
        return {"backend_down": True, "reason": "config-read-failed"}
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except Exception:
            bp = {}
    if not isinstance(bp, dict):
        return {}
    base_url = (bp.get("embedding_base_url") or "").strip()
    bastion = (bp.get("embedding_bastion") or "").strip()
    if bastion:
        # Identity is truth; the persisted base_url is only the address at
        # switch time. Same-package import, no heavy deps (embed_backends
        # imports nothing at module level).
        from mastercode.embed_backends import resolve_endpoint_for_hostname
        live = await resolve_endpoint_for_hostname(pool, entity_id, bastion)
        if live:
            base_url = live
        else:
            # No live address for the configured bastion (revoked / never
            # heartbeated). Do NOT fall back to the stale pinned address, and
            # do NOT let the caller drop to the bridge: the bastion is the
            # CONFIGURED backend, and config-driven lanes take NO cross-lane
            # fallback. Signal "configured but down" so
            # the caller marks the embed lane down and returns None — the
            # unembedded-backfill sweep re-embeds these rows once the bastion
            # returns. `{}` here would silently send the vector to the bridge,
            # which is the exact violation this branch exists to prevent.
            log.warning("embed bastion %r has no live endpoint — bastion lane DOWN "
                        "this call (no bridge fallback)", bastion)
            return {"backend_down": True, "bastion": bastion}
    if not base_url:
        return {}
    out = {"base_url": base_url}
    if bastion:
        # Bastion targets live on the host's WG overlay, which the app container
        # has NO kernel route to (the Hub's WireGuard is a userspace netstack;
        # the old kernel-routed wg-gw concentrator is retired). The
        # ONLY path is the Hub sidecar's host→peer forward (hub/peerfwd): a
        # pod-loopback CONNECT proxy whose reachable set is exactly the host's
        # own registered peers. Pure-BYO endpoints (no bastion) stay direct.
        out["peer_forward"] = (os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129"
    if bp.get("embedding_cred_name"):
        out["cred_name"] = bp["embedding_cred_name"]
    if bp.get("embedding_dim"):
        try:
            out["dim"] = int(bp["embedding_dim"])
        except (TypeError, ValueError):
            pass
    if bp.get("embedding_dimensions"):
        # Matryoshka truncation request (provider-lane models whose NATIVE dim
        # has no storage column — e.g. OpenAI's 1536/3072 embed at 1024 via the
        # API's `dimensions` param). Persisted at switch time only after a
        # probe PROVED the endpoint honors it, so production embeds and the
        # probe stay on one contract.
        try:
            out["dimensions"] = int(bp["embedding_dimensions"])
        except (TypeError, ValueError):
            pass
    return out


async def _post_json_via_peer_forward(fwd_addr: str, url: str, payload: dict,
                                      timeout_s: float = 60.0,
                                      connect_timeout_s: float = 10.0) -> tuple[int, dict]:
    """POST `payload` to `url` THROUGH the Hub's host→peer forward (hub/peerfwd,
    an HTTP CONNECT proxy on pod loopback). The app container has no kernel route
    to the WG overlay — this tunnel is the embed lane's only path to a bastion.
    Speaks minimal HTTP/1.1 over asyncio streams (no proxy support needed from
    aiohttp for plain-http CONNECT). Returns (status_code, parsed_json_or_{}).

    Two timeouts on purpose. `connect_timeout_s` bounds the CONNECT stage: the
    Hub's userspace WG netstack answers CONNECT to a peer that is OFF (no tunnel
    handshake) with silence, not a refusal, and the peer's TCP handshake over a
    live tunnel takes milliseconds — so a CONNECT that has not answered in 10s
    is a dead bastion, and waiting the full response timeout for it just makes
    every turn pay for an offline desktop (measured: 60s per call).
    `timeout_s` bounds the embed response itself, which legitimately runs long
    on a cold model load."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    target = u.netloc                      # e.g. [2001:db8::a:d]:11434
    fhost, fport = fwd_addr.rsplit(":", 1)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(fhost.strip("[]"), int(fport)), connect_timeout_s)
    try:
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), connect_timeout_s)
        if b" 200" not in status:
            raise ConnectionError(
                f"peer-forward refused CONNECT {target}: {status.decode(errors='replace').strip()}")
        while True:  # drain the CONNECT response headers
            line = await asyncio.wait_for(reader.readline(), connect_timeout_s)
            if line in (b"\r\n", b"\n", b""):
                break
        body = json.dumps(payload).encode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        req = (f"POST {path} HTTP/1.1\r\nHost: {target}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               f"Connection: close\r\n\r\n").encode() + body
        writer.write(req)
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout_s)
        parts = status_line.decode(errors="replace").split()
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        content_len = None
        chunked = False
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout_s)
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode(errors="replace").partition(":")
            k = k.strip().lower()
            if k == "content-length":
                try:
                    content_len = int(v.strip())
                except ValueError:
                    pass
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
        if chunked:
            # Ollama answers /v1/embeddings with Transfer-Encoding: chunked —
            # dechunk or the JSON parse sees hex size lines and fails (the
            # exact 'no vector returned' probe failure).
            raw = b""
            while True:
                size_line = await asyncio.wait_for(reader.readline(), timeout_s)
                try:
                    size = int(size_line.strip().split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    await asyncio.wait_for(reader.readline(), timeout_s)  # trailing CRLF
                    break
                raw += await asyncio.wait_for(reader.readexactly(size), timeout_s)
                await asyncio.wait_for(reader.readexactly(2), timeout_s)  # chunk CRLF
        elif content_len is not None:
            raw = await asyncio.wait_for(reader.readexactly(content_len), timeout_s)
        else:  # Connection: close → read to EOF
            raw = await asyncio.wait_for(reader.read(), timeout_s)
        try:
            return code, json.loads(raw) if raw else {}
        except Exception:
            return code, {}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _embed_via_byo(text: str, model: str, cfg: dict, *,
                         entity_id: int | None = None,
                         pool=None) -> Optional[list[float]]:
    """Embed `text` against the host's OWN BYO endpoint (cfg['base_url']). When a
    sealed token is registered (cfg['cred_name']) the call goes through the vault
    broker so the raw key never reaches this layer; otherwise an unauthenticated
    POST is made. Expects an OpenAI-style {"data":[{"embedding":[...]}]} or an
    Ollama-style {"embedding":[...]} response. Returns the vector or None."""
    base_url = cfg.get("base_url")
    if not base_url:
        return None
    # Reverse-relay sentinel (bastion-embed://<hostname>): the bastion advertised the
    # `embed-relay` capability, so the host reaches its embedder over the reverse relay
    # (the bastion opened a worker WS to us) instead of forward-dialing its tunnel IP —
    # which the entity pod has no route into. Short-circuit ABOVE the http route-suffix
    # logic below (the sentinel must NOT be fed to url.rstrip()/suffix-appending, which
    # would corrupt it) and before any aiohttp session opens: no HTTP client is ever
    # constructed for a relay endpoint. The model is passed through unchanged (the relay
    # carries no model of its own). Probe + real embed both take this one path.
    if base_url.startswith("bastion-embed://"):
        hostname = base_url[len("bastion-embed://"):].strip("/").strip()
        if not hostname:
            return None
        from server.network import embed_relay
        _pool = pool
        if _pool is None:
            try:
                from server import brain_service as _bs
                _pool = _bs.get_pool()
            except Exception:
                _pool = None
        if _pool is None:
            log.warning("BYO embed relay: no brain pool for %s", base_url)
            return None
        return await embed_relay.dispatch_embed(_pool, hostname, model, text)
    # Resolve the embeddings route. A bare host:port (what resolve_embed_endpoint
    # hands us for a bastion) has no path and 404s on Ollama, which serves
    # /v1/embeddings (OpenAI-compat) and /api/embed(dings) — NOT a top-level
    # /embeddings. Default to /v1/embeddings: it works for Ollama-compat AND real
    # OpenAI-style servers, and takes the same {model,input} body we send below.
    url = base_url.rstrip("/")
    low = url.lower()
    if low.endswith("/embeddings") or low.endswith("/embed"):
        pass  # caller already named a full embeddings route
    elif low.endswith("/v1"):
        url = url + "/embeddings"      # .../v1  -> .../v1/embeddings
    elif low.endswith("/api"):
        url = url + "/embed"           # .../api -> .../api/embed (ollama native)
    else:
        url = url + "/v1/embeddings"   # bare host -> OpenAI-compat route
    # Whole: the text reaching here is one embed window at most
    # (_embed_text_config_driven windows a longer one), never sliced with
    # nothing said.
    payload = {"model": model, "input": text}
    if cfg.get("dimensions"):
        # MRL truncation — only ever set for provider-lane endpoints that the
        # switch-time probe verified honor it (OpenAI-style `dimensions`).
        payload["dimensions"] = int(cfg["dimensions"])
    cred_name = cfg.get("cred_name")
    try:
        if cred_name:
            # Brokered: the sealed token is injected server-side; only the
            # response body comes back. entity_id/pool default to brain_service.
            from server import brain_service as _bs
            from server.vault import authz as _authz
            _pool = pool or _bs.get_pool()
            _eid = entity_id if entity_id is not None else getattr(_bs, "_entity_id", None)
            res = await _authz.use_credential(
                _pool, entity_id=_eid, name=cred_name, action="http_call",
                request={"url": url, "method": "POST", "json": payload,
                         "auth_scheme": "bearer"},
                actor=f"embed_byo:{_eid}")
            data = res.get("json") or {}
        elif cfg.get("peer_forward"):
            # Bastion lane: the target is on the WG overlay, reachable ONLY
            # through the Hub sidecar's host→peer forward. Same request, same
            # response shapes — just tunneled. probe_embedding_endpoint reuses
            # this function, so the switch-time probe exercises the identical
            # production path.
            status, data = await _post_json_via_peer_forward(
                cfg["peer_forward"], url, payload)
            if status != 200:
                log.warning("BYO embed via peer-forward HTTP %s for %s", status, url)
                return None
        else:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        log.warning("BYO embed HTTP %s for %s", resp.status, url)
                        return None
                    data = await resp.json()
    except Exception as exc:
        # Name the exception type: asyncio.TimeoutError stringifies to '' and
        # the line read "BYO embed failed url=...: " for a whole dead-bastion
        # incident, hiding that it was a timeout.
        log.warning("BYO embed failed url=%s: %s: %s", url, type(exc).__name__, exc)
        return None
    # OpenAI-style or Ollama-style response shapes.
    if isinstance(data, dict):
        if data.get("data") and isinstance(data["data"], list):
            vec = (data["data"][0] or {}).get("embedding")
            if vec:
                return list(vec)
        if data.get("embedding"):
            return list(data["embedding"])
        if data.get("embeddings") and isinstance(data["embeddings"], list):
            return list(data["embeddings"][0])
    return None


# One tiny deterministic embed is enough to prove an endpoint serves a model at a
# given dim. Not stored anywhere; short so a cold model load stays cheap.
_EMBED_PROBE_TEXT = "privatae embedding capability probe"


async def probe_embedding_endpoint(base_url: str, model: str,
                                   expected_dim: int | None = None,
                                   *, cred_name: str | None = None,
                                   peer_forward: str | None = None,
                                   dimensions: int | None = None,
                                   entity_id: int | None = None,
                                   pool=None) -> tuple[bool, str]:
    """Authoritatively check that the embedder at `base_url` actually SERVES
    `model` — by issuing ONE real embed over the exact direct (no-proxy) path
    production embeds use (`_embed_via_byo`) and requiring a vector back. When
    `expected_dim` is given the vector MUST match it (catches a model that answers
    at the wrong dimensionality). Returns (ok, reason).

    This is the hard gate behind pointing a host's embeds at a bastion/BYO
    endpoint: a host/agent may only assign a model the endpoint can actually serve
    at the right dim — never one that isn't pulled, nor one whose dim wouldn't fit
    the host's vector column. Reusing `_embed_via_byo` verbatim means the probe and
    the real embed share ONE code path (same auth, same proxy-bypass, same response
    shapes), so a passing probe guarantees the subsequent embeds work."""
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    if not base_url:
        return False, "no embedder endpoint to probe"
    if not model:
        return False, "no model to probe"
    cfg = {"base_url": base_url}
    if cred_name:
        cfg["cred_name"] = cred_name
    if dimensions:
        cfg["dimensions"] = int(dimensions)
    if peer_forward:
        # Bastion targets are overlay-only: the probe MUST ride the Hub's
        # host→peer forward exactly like production embeds, or a perfectly
        # healthy bastion probes as unreachable (no kernel route to the mesh).
        cfg["peer_forward"] = peer_forward
    try:
        vec = await _embed_via_byo(_EMBED_PROBE_TEXT, model, cfg,
                                   entity_id=entity_id, pool=pool)
    except Exception as exc:  # defensive — _embed_via_byo already swallows most
        return False, f"embedder probe errored: {exc}"
    if not vec:
        return False, (f"the endpoint does not serve '{model}' "
                       f"(no vector returned — is the model pulled on the bastion?)")
    if expected_dim and len(vec) != int(expected_dim):
        return False, (f"'{model}' returned dim {len(vec)}, expected {int(expected_dim)} "
                       f"— model/dim mismatch")
    return True, f"serves '{model}' at {len(vec)}d"


async def measure_embedding_dim(base_url: str, model: str,
                                *, cred_name: str | None = None,
                                dimensions: int | None = None,
                                entity_id: int | None = None,
                                pool=None) -> tuple[Optional[int], str]:
    """One real embed against `base_url` (same production path as
    probe_embedding_endpoint) that returns the MEASURED vector dim, or
    (None, reason). The provider-lane switch uses this to discover a model's
    native dim — and, with `dimensions`, to verify the endpoint honors MRL
    truncation — before committing a storage column."""
    cfg: dict = {"base_url": (base_url or "").strip()}
    if not cfg["base_url"] or not (model or "").strip():
        return None, "endpoint and model required"
    if cred_name:
        cfg["cred_name"] = cred_name
    if dimensions:
        cfg["dimensions"] = int(dimensions)
    try:
        vec = await _embed_via_byo(_EMBED_PROBE_TEXT, model.strip(), cfg,
                                   entity_id=entity_id, pool=pool)
    except Exception as exc:
        return None, f"embed probe errored: {exc}"
    if not vec:
        return None, f"the endpoint returned no vector for '{model}'"
    return len(vec), f"serves '{model}' at {len(vec)}d"


_last_bridge_embed_err = ""


async def _embed_via_bridge(text: str, model: str) -> Optional[list[float]]:
    """Route embed call through the bridge embeddings.embed op. mTLS-bound;
    only registered entities can call; not billed by the platform.
    Returns the vector list or None on failure."""
    try:
        from server import main as _server_main
        client = _server_main.get_bridge_client()
    except Exception:
        return None
    if client is None:
        return None
    try:
        envelope = await client.request_full(
            "embeddings.embed",
            # Whole: one embed window at most (_embed_text_config_driven
            # windows a longer text).
            {"model": model, "input": text},
        )
    except Exception as exc:
        global _last_bridge_embed_err
        _last_bridge_embed_err = str(exc)
        log.debug("bridge embed failed model=%s: %s", model, exc)
        return None
    if not envelope or not envelope.get("ok"):
        err = (envelope or {}).get("error") or "unknown"
        globals()["_last_bridge_embed_err"] = str(err)
        # debug, not warning: the health gate logs ONCE on state change
        # (this line used to fire per attempt — the circuit_open log spam).
        log.debug("bridge embed error model=%s: %s", model, err)
        return None
    data = envelope.get("data") or {}
    return data.get("vector")


# Observable counters for Pulse monitoring
embedding_failure_count = 0
unembedded_store_count = 0

# ── Embed-health gate ───────────────────────────────────────────────────
# When the embed lane is down, do NOT hammer it per memory op: turns fired
# query+store embeds × 3 retries each while the bridge circuit was open,
# spamming the log and burning the turn. One REAL attempt per cooldown
# window; if it fails, every embed in the window no-ops fast; state changes
# log ONCE; embedding_health() exposes the state so the dashboard / host
# turn can surface "memory degraded" instead of failing silently.
_EMBED_DOWN_COOLDOWN_S = 60.0
_embed_down_until = 0.0
_embed_down_reason = ""


def embedding_health() -> dict:
    """{down, reason} — down means embeds are skipped until the cooldown
    expires (then ONE real attempt re-probes the lane)."""
    return {"down": time.monotonic() < _embed_down_until,
            "reason": _embed_down_reason}


class RecallUnavailable(RuntimeError):
    """Memory search could NOT run (embed lane down / no model configured /
    vector store error) — as opposed to ran-and-found-nothing.

    The bridge-outage class: recall's embed failure used to collapse
    to None, which the recall tool rendered as an ok-status "No matching
    memories." — the host model then answered on a false premise ("I
    searched; there's nothing there") while the platform was actually
    degraded. That reads to the user as hallucinated tool calls. Callers of
    _query_rag catch THIS and surface an honest error instead (degrade,
    don't crash — the turn continues; the claim changes)."""


def _embed_mark_down(reason: str) -> None:
    global _embed_down_until, _embed_down_reason
    was_down = time.monotonic() < _embed_down_until
    _embed_down_until = time.monotonic() + _EMBED_DOWN_COOLDOWN_S
    _embed_down_reason = str(reason)[:300]
    if not was_down:  # state change only — never per-call spam
        log.error("EMBEDDINGS DOWN (%s) — skipping embeds for %.0fs; memories "
                  "written in this window will be backfilled by the unembedded "
                  "sweep once the lane recovers", _embed_down_reason,
                  _EMBED_DOWN_COOLDOWN_S)


def _embed_mark_up() -> None:
    global _embed_down_until, _embed_down_reason
    if time.monotonic() < _embed_down_until:
        log.warning("embeddings recovered (was: %s)", _embed_down_reason)
    _embed_down_until = 0.0
    _embed_down_reason = ""


# ── Memory-firing telemetry hook ──────────────────────────────────────
# server/main.py wires an async callback here at boot via set_retrieve_hook
# so the dashboard WebSocket lights up when memories are recalled. The caller
# already existed (main.py: "Could not wire memory-firing hook: ... has no
# attribute 'set_retrieve_hook'") but the setter was never defined here, so the
# wiring raised on every boot and recall events never reached the dashboard.
# Optional by design: when unset, fire_retrieve_hook is a no-op. Never raises
# into the recall hot path (best-effort, fire-and-forget).
_retrieve_hook = None


def set_retrieve_hook(fn) -> None:
    """Register an async callback fn(ids: list, source: str, context: dict) that
    fires whenever memories are recalled. Called once at boot from main.py."""
    global _retrieve_hook
    _retrieve_hook = fn


def fire_retrieve_hook(ids, source: str, context: dict | None = None) -> None:
    """Fire-and-forget the registered retrieval hook. Safe from any async
    context; never raises and never blocks the recall path. No-op if no hook
    is wired or there are no ids to report."""
    hook = _retrieve_hook
    if hook is None or not ids:
        return
    try:
        asyncio.get_running_loop().create_task(hook(list(ids), source, context or {}))
    except Exception:
        # No running loop, or scheduling failed — telemetry only; never let it
        # disturb retrieval.
        pass


async def _get_memory_embedding(text: str, retries: int = 3, *,
                                entity_id: int | None = None,
                                pool=None):
    """Get an embedding for a memory string.

    Returns the pgvector literal string `"[v0,v1,...]"` (or None on failure).

    When entity_id + pool aren't passed, defaults from brain_service:
      pool      = brain_service.get_pool()
      entity_id = brain_service._entity_id
    so legacy callers automatically route via the entity's configured
    embedding provider (the bridge or pod-local Ollama) instead of
    always hitting pod-local mxbai. Important on entities whose pod-
    local Ollama is broken — they switch via Settings → re-embed runs
    → from that point new writes also route via bridge.
    """
    if pool is None or entity_id is None:
        try:
            from server import brain_service as _bs
            if pool is None:
                pool = _bs.get_pool()
            if entity_id is None:
                entity_id = getattr(_bs, "_entity_id", None)
        except Exception:
            pass
    _, model = await _entity_embedding_config(pool, entity_id)
    global _last_embed_model
    _last_embed_model = model
    if not model:
        # Unconfigured — no model to embed with. _entity_embedding_config
        # already logged loudly (once). Never substitute a fallback model.
        return None

    # Embed lane known-down → no-op fast (no calls, no logs) until the
    # cooldown expires. One real attempt per window re-probes.
    if time.monotonic() < _embed_down_until:
        return None

    # Whole: it was cut at 2,000 characters
    # with nothing said, so the rest of a long memory could never be found by
    # meaning. _embed_text_config_driven embeds a longer text window by
    # window and combines the vectors into one.
    vec = await _embed_text_config_driven(
        text, model, entity_id=entity_id, pool=pool, retries=retries)
    if not vec:
        return None
    # Multi-dim: format and return the vector at whatever dim the model
    # produced. The caller routes it into the matching dim column via
    # active_embedding_column(). The old `len != 1024 → None` skip silently
    # dropped every non-1024 embedding.
    return "[" + ",".join(str(v) for v in vec) + "]"


async def _embed_text_config_driven(text: str, model: str, *,
                                    entity_id: int | None, pool,
                                    retries: int = 3,
                                    byo: dict | None = None):
    """Embed `text`, of ANY length, as one vector:
    a text longer than one embed window is embedded window by window and the
    vectors combined, weighted by length (embed_windows.embed_whole — the
    published method for inputs longer than the model takes). A text that
    fits one window is one call, unchanged. Never a cut text."""
    async def _one(part: str):
        return await _embed_one_config_driven(
            part, model, entity_id=entity_id, pool=pool, retries=retries, byo=byo)
    try:
        from mastercode import embed_windows as _ew
    except ImportError:
        _ew = None
    size = _ew.EMBED_INPUT_CHARS if _ew is not None else _EMBED_WINDOW_CHARS
    if len(text or "") <= size:
        return await _one(text)
    if _ew is not None:
        return await _ew.embed_whole(text, _one)
    return await _embed_windows_fallback(text, _one, size)


# The embed window when embed_windows is not importable — the same 8,000
# characters every embed call already sent. With embed_windows present, its
# embed_whole is used and this fallback is never reached.
_EMBED_WINDOW_CHARS = 8000


async def _embed_windows_fallback(text: str, embed_one, size: int):
    """The same method as embed_windows.embed_whole: window, embed each,
    combine weighted by window length, rescaled to the windows' mean norm. A
    window that fails fails the whole text (None)."""
    import math
    parts = [text[i:i + size] for i in range(0, len(text), size)]
    vecs, weights = [], []
    for part in parts:
        v = await embed_one(part)
        if not v:
            return None
        vecs.append([float(x) for x in v])
        weights.append(len(part))
    dim = len(vecs[0])
    total = float(sum(weights))
    acc = [sum(v[i] * w for v, w in zip(vecs, weights)) / total for i in range(dim)]
    mean_norm = sum(math.sqrt(sum(x * x for x in v)) for v in vecs) / len(vecs)
    norm = math.sqrt(sum(x * x for x in acc))
    return acc if norm == 0.0 else [x * (mean_norm / norm) for x in acc]


async def _embed_one_config_driven(text: str, model: str, *,
                                   entity_id: int | None, pool,
                                   retries: int = 3,
                                   byo: dict | None = None):
    """Embed one window of text on the entity's CONFIGURED backend and NOWHERE else — the
    embedding backend is 100% config-driven and takes NO cross-lane fallback
    Resolves the backend ONCE (`_byo_embedding_config`):

      - BYO/bastion endpoint configured & reachable → embed there, ONLY there.
        On failure mark the embed lane down and return None (the unembedded-
        backfill sweep recovers the rows when the lane returns). NEVER the bridge.
      - BYO/bastion configured but currently DOWN (no live endpoint) → mark the
        lane down, return None. NEVER the bridge.
      - No BYO/bastion configured at all → the platform bridge endpoint IS the
        chosen backend; the bridge lane is legitimate here.

    Returns the raw vector (list[float]) or None. Callers format as needed. The
    token (if any) is brokered through the vault — never held here. `byo` may be
    pre-resolved by a batch caller to avoid re-reading behavioral_params per row.
    """
    # UNCONFIGURED IS A SETUP STATE, NOT A FAILURE. An empty model must never
    # dial any endpoint and must never mark the lane down. Before this guard the
    # two context.py callers passed model='' straight through, the bridge was
    # dialed with no model, its circuit tripped, and every reader then reported
    # "embedding lane down (model=: circuit_open)" — a system-outage story on a
    # fresh host that simply had not chosen an embedding model yet (stranger
    # interview). There is no platform embedder; setup is the
    # user's. Guarding HERE covers every caller, present and future.
    # _entity_embedding_config already logged the unconfigured state once.
    if not (model or "").strip():
        return None
    # Embed lane known-down → no-op fast for EVERY caller until the cooldown
    # expires; one real attempt per window re-probes. This check used to live
    # only in _get_memory_embedding, so the direct callers (signal_retrieval.
    # embed_message on every turn, context/query embeds, the backfill batch)
    # still dialed a dead bastion right after the lane had been marked down
    # and each ate the full peer-forward timeout: two back-to-back 60s waits
    # inside one workbench turn.
    if time.monotonic() < _embed_down_until:
        return None
    if byo is None:
        byo = await _byo_embedding_config(pool, entity_id)
    if byo:
        # A BYO/bastion backend is the CHOSEN lane. The bridge is never a
        # fallback from here — that was the exact violation (bridge fired,
        # failed, tripped circuit breakers, spammed logs).
        if byo.get("backend_down"):
            # Name the actual condition: a bastion with no live address, or a
            # config read that failed (which can fire on a provider-lane host
            # with no bastion at all — never call that a bastion problem).
            if byo.get("reason") == "config-read-failed":
                _embed_mark_down(f"{model}: the embedding configuration could not be read")
            elif byo.get("bastion"):
                _embed_mark_down(f"{model}: the machine {byo['bastion']} serving embeddings "
                                 f"has no live endpoint")
            else:
                _embed_mark_down(f"{model}: the embedding endpoint has no live address")
            return None
        try:
            vec = await _embed_via_byo(text, model, byo,
                                       entity_id=entity_id, pool=pool)
            if vec:
                _embed_mark_up()
                return vec
            log.warning("BYO embed returned no vector (model=%s) — embed lane DOWN, "
                        "no bridge fallback", model)
        except Exception as e:
            log.warning("BYO embed raised (model=%s): %s — embed lane DOWN, "
                        "no bridge fallback", model, e)
        _embed_mark_down(f"{model}: the embedding endpoint returned no vector")
        return None

    # No BYO/bastion configured → the platform backend via bridge (mTLS, entity-registered,
    # not billed by the platform) is the chosen backend. Pod-local Ollama
    # removed. Retry on transient bridge errors only — Ollama-side
    # errors come back as an envelope error from the bridge, not an exception.
    # An OPEN CIRCUIT is definitive for this window: never retried (retrying a
    # breaker 3× with sleeps was pure waste + log spam).
    last_err = None
    for attempt in range(retries):
        try:
            vec = await _embed_via_bridge(text, model)
            if vec:
                _embed_mark_up()
                return vec
            last_err = _last_bridge_embed_err or "bridge returned no vector"
            if "circuit_open" in str(last_err):
                break
        except Exception as e:
            last_err = e
        if attempt < retries - 1:
            await asyncio.sleep(1)

    global embedding_failure_count
    embedding_failure_count += 1
    _embed_mark_down(f"{model}: {last_err}")
    return None


# Tracks the model used by the most recent _get_memory_embedding call.
# Writers that need to record the embedding_model column read this after
# calling _get_memory_embedding. Not thread-safe; safe within a single
# request because the entity runtime is single-threaded asyncio.
# Empty until the first resolve — there is no default model.
_last_embed_model: str = ""


def last_embed_model() -> str:
    """The model used by the most recent _get_memory_embedding call.
    Writers use this to populate entity_memory.embedding_model on insert."""
    return _last_embed_model


# The pre-fire items (verbatim contents) led into the memory block by the most
# recent build_memory_context(query=...) call, per entity. Read by
# prompt_schema._build_grouped to record a "memory_prefire" entry in the
# prompt_log blocks payload so the View
# Context panel can show exactly what prefired last turn. Same single-
# threaded-asyncio caveat as _last_embed_model.
def _memory_date(when) -> str:
    """YYYY-MM-DD from a datetime or an ISO string; '' when unknown."""
    if when is None:
        return ""
    if hasattr(when, "strftime"):
        try:
            return when.strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(when)
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else ""


# Rows the host itself wrote — its own past replies and the consolidated
# summaries of them. What they describe (a bench, a routing, an embedding
# state) was true when written and is a READ now, not a fact; the prompt
# marks them so the host re-reads the live tool instead of repeating them
# (undated snapshots from different dates otherwise disagree with each other).
OWN_WORDS_TYPES = ("turn", "summary")
# The standing list of a scoped turn holds this thread's own rows, rows written
# with no scope (global), and these types from anywhere: the persona's
# relationship notes, the constitution and the rules the user taught.
# `preference` is NOT here (what is relevant in one place is a leak in
# another): a preference rides only in its own scope, or everywhere
# when it is global — its scope is set by what it is about (memory_scope,
# metadata.about; memory_retrieval.EFFECTIVE_SCOPE).
THREAD_SET_ENTITY_WIDE_TYPES = ("relationship", "constitutional")
# Recalled types that may be volunteered to a turn whatever scope they were
# written in (the pre-fire's turn-scope filter lets them through).
SCOPE_EVERYWHERE_TYPES = ("relationship",)

# The write-tags of turns that run in a conversation without being part of it:
# a replay or A/B variant re-running a turn, and a fired wake-up's own reply
# (its report is what the thread shows). Their rows carry the conversation's
# descriptor, so every read of what the conversation SAID leaves them out.
# COALESCE so a row with no app_id is kept (NULL NOT IN (...) would drop it).
HIDDEN_LANE_APP_IDS = ("circles-replay", "circles-ab", "scheduled_task")
NOT_HIDDEN_LANE_ROW = ("COALESCE(metadata->>'app_id', '') NOT IN ("
                       + ", ".join(f"'{a}'" for a in HIDDEN_LANE_APP_IDS) + ")")


# The only RECALLED memory types the pre-fire ("Relevant Right Now") and the
# standing list may show. A preference changes what the host does next; the
# persona's relationship notes are who it is. Every other recalled type
# (facts, goals, decisions, topics, open threads, emotions of a moment,
# own-words turns and summaries, research receipts) is archive: `recall` still finds it, these
# two sections never volunteer it. Not governed here, and still rendered by
# build_memory_context: the rules the user taught (skill_memory, "Things You
# Were Taught") and the thread audit's open findings. Documents and cited
# facts are `recall`'s; the pre-fire does not render them. Measured over 10 days: 101 pre-fired
# turns carried 96 own-words rows, 91 goals and 75 facts averaging 57 days old,
# against 48 preferences — and a six-week-old "embeddings do not work" fact
# rode the very turn that asked whether embeddings work. `emotion` is out on
# the same evidence: of its recent rows, none was durable — they record one
# moment ("User was frustrated that the assistant forgot…").
PROMPT_MEMORY_TYPES = frozenset({"preference", "relationship"})
# Memory kind five, `cited_fact`: a finding promoted from a task's record. It
# is not pre-fired; `recall` finds it and renders it fenced as quoted data
# (memory_retrieval.format_recall_text -> task_record.cited_fact_text).


def is_person_pattern(hit: dict) -> bool:
    """A cross-session pattern about the person (memory_consolidation
    _extract_patterns): stored as a `summary` with no scope, but it describes
    the person across every thread, so it is not another thread's own words."""
    meta = hit.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = {}
    return isinstance(meta, dict) and meta.get("source") == "consolidation_pattern"


def format_memory_line(content: str, when=None, memory_type: str | None = None,
                       *, topic: str | None = None, row_id=None) -> str:
    """One prompt line for a memory row. Every row carries its DATE, and a
    row in the host's own words says so:
        - [2026-08-04] the fact
        - [2026-08-22, your own words then] what you replied that day
    A memory without a date is a claim with no provenance — the Host could not
    tell which of five contradicting snapshots was current. A row with a
    slot topic (memory_slots) names it, and a row's #id is given when known,
    so the Host can reuse the topic, or key or correct the row by id:
        - [2026-10-19, topic spelling, #41] the preference"""
    d = _memory_date(when)
    own = memory_type in OWN_WORDS_TYPES
    if d and own:
        head = f"{d}, your own words then"
    elif d:
        head = d
    elif own:
        head = "undated, your own words then"
    else:
        head = "undated"
    if topic:
        head += f", topic {topic}"
    if row_id is not None:
        head += f", #{row_id}"
    return f"\n- [{head}] {content}"


# How many live rows of one type the standing list reads before it picks the
# newest five: every row a Host realistically holds in one scope. Past it the
# count it reports says "or more".
STANDING_CANDIDATES = 200
STANDING_SHOWN = 5
TOPICS_LINE_MAX = 40


def omitted_line(n: int, label: str, how: str, *, at_least: bool = False) -> str:
    """The line a section ends with when its cap or the block's budget left
    live entries out: how many, and how to read them. Nothing left out, no
    line: an entry never leaves the prompt without the Host being told."""
    if n <= 0:
        return ""
    more = f"{n}{'+' if at_least else ''} more {label}"
    return f"\n- ({more} not shown here; {how})"

async def topics_line(pool, entity_id: int, source_context: Optional[dict] = None) -> str:
    """One line naming the slot topics in use per type (memory_slots) where
    this turn reads them: global slots and the turn's own scope's (a
    relationship note is read everywhere, so its slots are global). At most
    TOPICS_LINE_MAX, '' when there are none. Never raises."""
    from mastercode.memory_slots import slot_scope_for
    scopes = ["global"]
    if isinstance(source_context, dict) and source_context.get("kind"):
        here = slot_scope_for("preference", {"source_context": source_context})
        if here and here not in scopes:
            scopes.append(here)
    try:
        rows = await pool.fetch(
            "SELECT memory_type, slot_topic, count(*) AS n FROM entity_memory"
            " WHERE entity_id = $1 AND slot_topic IS NOT NULL AND status = 'active'"
            "   AND t_valid_until IS NULL AND memory_type IN ('preference', 'relationship')"
            "   AND slot_scope = ANY($3::text[])"
            " GROUP BY memory_type, slot_topic ORDER BY memory_type, max(created_at) DESC"
            " LIMIT $2", entity_id, TOPICS_LINE_MAX, scopes)
    except Exception as e:  # noqa: BLE001 — the block stands without it
        log.debug("topics line skipped: %s", e)
        return ""
    if not rows or not isinstance(rows, list):
        return ""
    by: dict[str, list[str]] = {}
    for r in rows:
        try:
            by.setdefault(r["memory_type"], []).append(r["slot_topic"])
        except Exception:  # noqa: BLE001
            return ""
    said = "; ".join(f"{t}: {', '.join(v)}" for t, v in by.items())
    return (f"\nTopics in use ({said}). A memory written with one of these topics, "
            "in the same place, replaces the entry there.")



_last_prefire_items: dict[int, list[str]] = {}
# The documents-store stash went with the documents pre-fire: the pre-fire reads memory only, and the
# user's documents are found with `recall`.


def last_prefire_items(entity_id: int) -> list[str]:
    """Pre-fired memory contents from the entity's latest memory-block build
    ([] when the last turn fired none / ran no query)."""
    return _last_prefire_items.get(entity_id) or []


def _hit_scope(hit: dict) -> dict | None:
    kind, ref = hit.get("sctx_kind"), hit.get("sctx_ref")
    if not kind:
        meta = hit.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        sc = (meta or {}).get("source_context") if isinstance(meta, dict) else None
        if isinstance(sc, dict):
            kind, ref = sc.get("kind"), sc.get("ref")
    return {"kind": str(kind), "ref": str(ref)} if kind else None


def derive_source_context(
    interaction_context: str | None,
    *,
    project_id: int | None = None,
    session_id: str | None = None,
    peer_ref: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """Map a turn's surface to a unified ``{kind, ref}`` provenance descriptor.

    Source-driven threading. Collapses the scattered scoping
    signals — the ``project_id`` column, ``metadata.app_id``, and the
    ``source_session`` fallback — into ONE descriptor the recall ladder
    matches on. The app the turn happens in decides what is first-class:
    workbench → the project, circles → the app, a host↔host message →
    the peer. This is source-driven, NOT a fixed taxonomy.

    It reproduces exactly the scope decisions session.py used to make from
    ``interaction_context`` (so existing surfaces are unchanged) and adds
    ``peer`` as a first-class kind the synaptive / external-host surface
    never had (its turns were previously reachable only via the
    source_session fallback — see synaptive_handler.py).

    kind ∈ {project, app, peer, thread, session}:
      - project : ref = project id (workbench chat)
      - app     : ref = app id, e.g. "circles" (circles / generic app chat)
      - peer    : ref = peer/host actor (synaptive, external host)
      - thread  : ref = circles_threads.id (a Circles self-thread the user
                  started)
      - session : ref = session id (voice / cron / bg with no durable thread)

    ``thread`` is its own kind, matched exactly, and deliberately NOT a
    suffix on ``app:circles``: the app branch of ``_turn_row_where`` also
    matches the legacy ``metadata.app_id`` tag, so a suffixed ref would leak
    every new thread's turns back into the original scope. The original
    ``app:circles`` scope is untouched — it is "the original conversation"
    and keeps every behaviour it has today.
    """
    if peer_ref:
        return {"kind": "peer", "ref": str(peer_ref)}
    if thread_id:
        return {"kind": "thread", "ref": str(thread_id)}
    ic = interaction_context or ""
    if project_id is not None:
        return {"kind": "project", "ref": str(project_id)}
    if ic.startswith("project:"):
        return {"kind": "project", "ref": ic.split(":", 1)[1]}
    if ic in ("", "chat", "circles"):
        return {"kind": "app", "ref": "circles"}
    return {"kind": "app", "ref": ic}


def derive_prompt_log_context(src_ctx: dict | None, app_id: str | None) -> dict | None:
    """Which THREAD a prompt_log row belongs to: the turn's recall descriptor,
    except that an `app` turn follows the app_id the CALLER declared rather
    than the one the session's interaction_context implies.

    Replay and the A/B fan-out (server/routes/circles.py:1775, :1834) send
    history_context="chat" with app_id 'circles-replay' / 'circles-ab' — they
    reconstruct the circles thread deliberately, so derive_source_context
    returns app:circles for both and their prompt_log rows landed on the self
    thread, where the panel described a replay the user never sent. Their turn
    ROWS were already separated by the app_id tag (_turn_row_where above).

    The app_id goes through derive_source_context rather than being wrapped in
    a descriptor here, so the writer collapses exactly what the reader
    collapses. Doing it by hand meant app_id="chat" stamped app:chat while
    every reader (routes/brain.py:456, and derive_source_context:1030 itself)
    reads "" | "chat" | "circles" as app:circles — a thread that could be
    written to and never read back. One rule, one function, no drift."""
    if not app_id or not isinstance(src_ctx, dict) or src_ctx.get("kind") != "app":
        return src_ctx
    cand = derive_source_context(str(app_id))
    # Only an app descriptor may override an app descriptor: an app_id shaped
    # like "project:7" would otherwise silently re-scope the row to a project.
    return cand if cand.get("kind") == "app" else src_ctx


def _turn_row_where(
    args: list,
    *,
    session_id: str,
    source_context: dict | None = None,
    project_id: int | None = None,
    app_id: str | None = None,
    before=None,
    live_only: bool = True,
    # A thread's own acts (Clear) take a fired wake-up's hidden reply rows
    # with it; every reader of the thread (recall, summary, counts) leaves
    # them out, because the runner's report already carries that reply.
    with_wakeup_rows: bool = False,
) -> list[str]:
    """The turn-row WHERE clause (entity filter + live-row filter + scope),
    factored out of recall_recent_turns
    so the thread consolidator's per-scope turn stats match the recall
    ladder's scoping EXACTLY — one scope truth, no drift. `args` carries
    $1=entity_id on entry; this appends and returns the clause list.

    `live_only=False` drops the t_valid_until filter. The TRANSCRIPT views
    use it — circles_threads.conversation_where and knowledge_toc.
    scope_episodes: a transcript shows what was said, and a superseded turn
    row is hidden from recall, not from the transcript (a long-lived Host
    holds many such rows). Recall keeps the filter."""
    where = [
        "entity_id = $1",
        "memory_type = 'turn'",
        "status = 'active'",
    ]
    if live_only:
        where.append("(t_valid_until IS NULL OR t_valid_until > NOW())")

    if before is not None:
        args.append(before)
        where.append(f"created_at < ${len(args)}")

    if source_context and source_context.get("kind"):
        # Source-driven threading: match the unified descriptor
        # exactly, then broaden to legacy rows not yet backfilled. The
        # `metadata->>'app_id' IS NULL` leak of the old app branch is gone
        # on purpose — an untagged turn no longer bleeds into every app's
        # recall; the backfill gives every existing turn a source_context.
        _kind = source_context.get("kind")
        _ref = str(source_context.get("ref") or "")
        if _kind == "project":
            try:
                args.append(int(_ref))
                where.append(f"project_id = ${len(args)}::int")
            except (ValueError, TypeError):
                args.append(session_id)
                where.append(f"source_session = ${len(args)}")
        elif _kind == "app":
            # The legacy `metadata.app_id` arm is for rows written BEFORE the
            # descriptor existed, and it must not reach a Circles thread.
            # A threaded send still declares app_id='circles' —
            # session.py:2608-2612 stamps _turn_tags with the app_id the
            # caller passed, and circles.py:1705 passes 'circles' whether or
            # not the body carried a thread_id — so without the exclusion
            # below every thread's turns answer the app:circles scope as well
            # as their own, which is exactly the leak to prevent (a suffixed
            # ref would leak every new thread's turns back into the original
            # scope). Measured by scripts/test_circles_threads.py.
            args.append(_ref)
            where.append("project_id IS NULL")
            where.append(
                f"((metadata->'source_context'->>'kind' = 'app' "
                f"  AND metadata->'source_context'->>'ref' = ${len(args)}) "
                f" OR (metadata->>'app_id' = ${len(args)} "
                f"     AND metadata->'source_context'->>'kind' "
                f"         IS DISTINCT FROM 'thread'))"
            )
        elif _kind == "peer":
            # First-class now: exact descriptor OR the legacy synaptive
            # source_session shape (`synaptive:<actor>:<hash>`).
            args.append(_ref)
            where.append(
                f"((metadata->'source_context'->>'kind' = 'peer' "
                f"  AND metadata->'source_context'->>'ref' = ${len(args)}) "
                f" OR source_session LIKE 'synaptive:' || ${len(args)} || ':%')"
            )
        elif _kind == "thread":
            # A Circles self-thread. Exact descriptor only — no
            # legacy OR clause, because a thread has no rows that predate the
            # kind. This branch lives here and only here so the consolidator's
            # per-scope stats and the recall ladder cannot disagree about
            # what a thread contains (the "two scope truths" risk).
            args.append(_ref)
            where.append("project_id IS NULL")
            where.append(
                f"(metadata->'source_context'->>'kind' = 'thread' "
                f" AND metadata->'source_context'->>'ref' = ${len(args)})"
            )
            # A fired wake-up thinks in its thread, so its
            # own reply row carries this descriptor on the hidden lane
            # (app_id 'scheduled_task'). The runner's report, which is in the
            # thread, already carries that reply: counting both would give
            # every wake-up answer twice in the thread's history. A replay or
            # A/B variant runs under the thread's descriptor too, so its rows
            # land here; they are a re-run, not the conversation, and the
            # transcript never shows them (circles_threads.NOT_REPLAY_ROW).
            if not with_wakeup_rows:
                where.append(NOT_HIDDEN_LANE_ROW)
        else:  # session (voice / cron / bg)
            # The DESCRIPTOR'S OWN ref wins when it has one. `session` is the
            # one kind whose ref IS a session id (derive_source_context's
            # contract, :1028-1034), so matching the caller's session_id here
            # and ignoring the ref answered "which turns are in THIS scope?"
            # with "the turns of whoever is asking" — a scope that returns []
            # for every caller but one. That is what `chat_threads
            # op='episodes'` on a kind='session' ref hit once the op started
            # routing through this clause (knowledge_toc.scope_episodes); the
            # op matched {kind, ref} exactly before the move, and
            # routes/brain.py:645-653 (the Memory app's episodes lane) still
            # does. The session_id fallback stays for the descriptor that
            # carries no ref, and for the two no-descriptor branches below.
            #
            # No in-tree caller is affected: derive_source_context never
            # returns kind='session' (:1045-1054 maps every surface to
            # project / app / peer / thread), so every descriptor that lands
            # here was built by hand at a boundary — the chat_threads
            # arguments — and there it is the ref the host asked about.
            args.append(_ref or session_id)
            where.append(f"source_session = ${len(args)}")
    elif project_id is not None:
        args.append(project_id)
        where.append(f"project_id = ${len(args)}::int")
    elif app_id is not None:
        # The app_id path is what the Circles UI has always shown for the
        # original conversation (circles_threads.legacy_conversation_where).
        # It is deliberately NARROWER than the descriptor path above: a
        # hidden-lane row (app_id 'scheduled_task' with an app:circles
        # descriptor) is recalled but never rendered. A thread's rows are
        # excluded here too — they carry app_id 'circles' as well.
        args.append(app_id)
        where.append("project_id IS NULL")
        where.append(
            f"(metadata->>'app_id' = ${len(args)} OR metadata->>'app_id' IS NULL)"
        )
        where.append(
            "metadata->'source_context'->>'kind' IS DISTINCT FROM 'thread'")
    else:
        # Fallback for callers without surface tags (voice, cron, bg).
        args.append(session_id)
        where.append(f"source_session = ${len(args)}")
    return where


async def count_scope_turns(
    pool,
    entity_id: int,
    session_id: str,
    *,
    source_context: dict,
    since=None,
) -> dict:
    """Turn count + total content chars for one thread scope (conversation-
    depth P2: the consolidator's trigger math). Uses the SAME scope clause
    as recall_recent_turns via _turn_row_where. `since` bounds to turns
    created at/after a timestamp (e.g. the last consolidation)."""
    args: list = [entity_id]
    where = _turn_row_where(args, session_id=session_id,
                            source_context=source_context)
    if since is not None:
        args.append(since)
        where.append(f"created_at >= ${len(args)}")
    try:
        row = await pool.fetchrow(
            f"SELECT count(*) AS n, COALESCE(sum(length(content)), 0) AS chars"
            f"  FROM entity_memory WHERE {' AND '.join(where)}",
            *args,
        )
        return {"count": int(row["n"]), "chars": int(row["chars"])}
    except Exception as e:
        log.warning("count_scope_turns failed (entity=%d): %s", entity_id, e)
        return {"count": 0, "chars": 0}


# ── Platform notices on a stored reply. A reply row keeps what the person SAW — the Host's words plus
# a platform notice (the budget stop line, the stall notice). Everything that
# reads a row as the Host's words — history, consolidation, extraction, the
# rolling summary — reads host_part() instead: the Host's own words, and the
# notice as a platform note kind.
_REPLY_SEP = "\n\n"
# The fixed stall texts main wrote into rows before this change (main's
# session.py, the stall notice). Matched EXACTLY — the whole row, or a row
# that ends with the separator and the text — so a Host that quotes the
# sentence mid-reply is never touched.
_OLD_STALL_TEXTS = (
    "I hit a snag — the model returned its reasoning but no actual reply, twice in a row. "
    "Nothing was lost and no work was undone.",
    "The open choice is in the question box above.",
    # the earlier form of the same notice, before its last sentence was
    # dropped
    "I hit a snag — the model returned its reasoning but no actual reply, twice in a row. "
    "Nothing was lost and no work was undone. Say \"continue\" and I'll pick up where I left off.",
)
# What the next turn's model is told about a platform notice on an earlier
# reply — the platform's voice, never a UI pointer.
PLATFORM_NOTES = {
    "budget": "[platform] That turn stopped when it reached its spend budget.",
    "stall": "[platform] That turn's model call returned no reply text.",
}


def host_part(content: str, md: dict | None) -> tuple[str, list[str]]:
    """(the Host's own words, platform notice kinds) of a stored turn row.
    model_content when the row carries it; else (PII mode, where only
    structural keys survive) the notice's length from the END — notices are
    platform text, untouched by the scrub; else an old row whose reply is, or
    ends with, one of main's fixed stall texts. Any other row: as stored."""
    md = md if isinstance(md, dict) else {}
    content = content or ""
    if (md.get("role") or "user") != "assistant":
        return content, []
    notes = [k for k in (md.get("platform_notice") or []) if isinstance(k, str)]
    mc = md.get("model_content")
    if isinstance(mc, str):
        return mc, notes
    nl = md.get("notice_len")
    if isinstance(nl, int) and 0 < nl <= len(content):
        return content[:len(content) - nl].rstrip(), notes
    for s in _OLD_STALL_TEXTS:
        if content == s:
            return "", ["stall"]
        if content.endswith(_REPLY_SEP + s):
            return content[:-len(_REPLY_SEP + s)].rstrip(), ["stall"]
    return content, notes


def history_messages(turns, *, drop_leading_notes: bool = True, tool_note=None) -> list[dict]:
    """Recalled turns as the model's messages: role + content only; a turn
    that carried a platform notice (its `platform_note`) is followed by a
    platform-voiced system note, and an assistant turn with no words of its
    own contributes only the note. History never STARTS on a note (a
    leading system message would be lifted into the top-level system prompt
    on the Anthropic wire).

    `tool_note` (search mode only, runtime/tool_core.NoteRenderer): a turn
    that called tools is followed by a note with their schemas, each schema
    once per window at its first appearance and never as the window's first
    message (tool_core.note_plan, the same plan the walk sizes). None (every
    groups-mode caller) emits exactly what it always did."""
    plan = None
    if tool_note is not None:
        from mastercode.runtime.tool_core import note_plan
        plan = note_plan(turns, tool_note)
    out: list[dict] = []
    for i, m in enumerate(turns or []):
        c = m.get("content") or ""
        if not (m.get("role") == "assistant" and not c.strip()):
            out.append({"role": m.get("role"), "content": c})
        for kind in (m.get("platform_note") or []):
            note = PLATFORM_NOTES.get(kind)
            if note:
                out.append({"role": "system", "content": note})
        if plan is not None and plan[i] is not None:
            out.append({"role": "system", "content": plan[i][0]})
    if drop_leading_notes:
        from mastercode.runtime.tool_core import is_note as _is_tool_note
        while out and out[0].get("role") == "system" and (
                out[0].get("content") in PLATFORM_NOTES.values() or _is_tool_note(out[0])):
            out.pop(0)
    return out


async def recall_recent_turns(
    pool,
    entity_id: int,
    session_id: str,
    *,
    limit: int = 30,
    exclude_content: str | None = None,
    project_id: int | None = None,
    app_id: str | None = None,
    source_context: dict | None = None,
    recency_window_hours: int = 0,  # historical default 2; see retraction below
    before=None,  # point-in-time cutoff: only turns created strictly before
                  # this timestamp (replay/A-B regenerate — the variant must
                  # see exactly the history the ORIGINAL answer saw).
    lane: str | None = None,  # an internal thread of a run of work: only the
                  # rows its own turns wrote (metadata.task_lane), never a
                  # sibling thread's (task_serial.CURRENT_WORK_LANE).
) -> list[dict]:
    """Pull chronological turn rows for the LLM's per-turn history view.

    Tag-scoped retrieval: context follows tags, not time. The earlier union design also pulled
    a 2-hour cross-session recency slice and that produced a planning
    leak — the first turn in a brand-new Workbench project
    pulled in Circles turns from minutes earlier about a totally
    unrelated topic, the model saw incoherent context and wrote its own
    plan to itself instead of answering.

    Retrieval rules now:

      1. **Same project** — when `project_id` is set, return turns
         tagged with this project_id. This is the strongest scope.
      2. **Same app + no project** — when `project_id` is None and
         `app_id` is set, return turns where project_id IS NULL and
         `metadata->>'app_id' = app_id` (matches the Circles self-
         thread pattern at routes/circles.py:218-221).
      3. **Same session fallback** — when neither tag is supplied,
         return turns from this session. (Voice / cron / background
         callers that pass session_id.)

    `recency_window_hours` defaults to 0 (off). Cross-surface time
    union is the wrong primitive: the user changes surfaces because
    the topic changed, and we want the model's context to reflect that.
    Set explicitly to 1+ ONLY if a caller has a real need to widen the
    net (e.g., a debug introspection tool). The chat hot path does
    NOT pass it.

    Args:
        entity_id: scope.
        session_id: the active session — used only as a fallback when
            no project_id and no app_id are supplied.
        limit: max rows to return.
        exclude_content: drop a row whose content matches this exactly
            (used to exclude the just-arrived user message that's
            already queued for write but should be appended explicitly).
        project_id: tag scope. Strongest.
        app_id: tag scope (used when project_id is None).
        recency_window_hours: legacy escape valve, default 0 (off).
    """
    args: list = [entity_id]
    where = _turn_row_where(
        args, session_id=session_id, source_context=source_context,
        project_id=project_id, app_id=app_id, before=before,
    )

    # Optional widening — off by default.
    if recency_window_hours and int(recency_window_hours) > 0:
        args.append(int(recency_window_hours))
        # OR'd into the existing tag scope so the user's "we were just
        # talking about" arc IS recoverable, but only when a caller
        # explicitly opts in.
        last_clause = where.pop()
        where.append(
            f"({last_clause} OR created_at > NOW() - (${len(args)} || ' hours')::interval)"
        )
    if lane:
        args.append(str(lane))
        where.append(f"metadata->>'task_lane' = ${len(args)}")

    args.append(limit)
    sql = f"""
        SELECT id, content, metadata, created_at, project_id, source_session
          FROM entity_memory
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
         LIMIT ${len(args)}
    """
    try:
        rows = await pool.fetch(sql, *args)
    except Exception as e:
        log.warning("recall_recent_turns failed (entity=%d session=%s project=%s): %s",
                    entity_id, session_id, project_id, e)
        return []

    out: list[dict] = []
    for r in reversed(rows):  # chronological
        content = r["content"] or ""
        if exclude_content is not None and content == exclude_content:
            continue
        md = r.get("metadata") or {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        role = md.get("role") or "user"
        # The Host's own words, and any platform notice as a note kind
        # (host_part).
        host, notes = host_part(content, md)
        # row_id: where the window's front is, so what fell out of it can be
        # read by id (evicted_record); history_messages never sends it.
        item = {"role": role, "content": host, "row_id": r["id"]}
        if notes:
            item["platform_note"] = notes
        # The tools the turn called, by target (search mode's history notes,
        # runtime/tool_core): read from the chips every turn row stores.
        # Only a search-mode history renders them.
        if role == "assistant" and isinstance(md.get("tool_calls"), list):
            from mastercode.runtime.tool_core import called_names
            called = called_names(md["tool_calls"])
            if called:
                item["tools_called"] = called
        if md.get("untrusted"):
            # Written in a turn that read untrusted content: the turn that
            # reads it back as history is marked (runtime/turn_taint).
            item["untrusted"] = md["untrusted"]
        out.append(item)
    return out


async def recall_tool_trail(
    pool,
    entity_id: int,
    session_id: str,
    *,
    source_context: dict | None = None,
    limit: int = 3,
    before=None,
) -> list[dict]:
    """The last `limit` ASSISTANT turns in this thread scope that made tool
    calls, chronological: [{created_at, tool_calls}] — the bounded per-call
    chips session.py persists on the turn row (metadata.tool_calls: tool,
    op/provider/harness, status, next_step, digest). Feeds
    runtime.tool_trail.render_tool_trail, the block that carries the host's own
    recent calls + still-pending next steps into the next turn (without it,
    tool results did not survive a turn boundary, and the host inferred a
    login had landed / quoted the wrong task id). Same scope clause as
    recall_recent_turns. Never raises — [] on any failure."""
    args: list = [entity_id]
    where = _turn_row_where(
        args, session_id=session_id, source_context=source_context, before=before,
    )
    where.append("metadata->>'role' = 'assistant'")
    # Order-independent predicates (Postgres may evaluate AND operands in any
    # order, and jsonb_array_length raises on a non-array): type check + a
    # jsonb value comparison that is safe for any type.
    where.append("jsonb_typeof(metadata->'tool_calls') = 'array'")
    where.append("metadata->'tool_calls' <> '[]'::jsonb")
    args.append(int(limit))
    sql = f"""
        SELECT created_at, metadata
          FROM entity_memory
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
         LIMIT ${len(args)}
    """
    try:
        rows = await pool.fetch(sql, *args)
    except Exception as e:
        log.warning("recall_tool_trail failed (entity=%d session=%s): %s",
                    entity_id, session_id, e)
        return []
    out: list[dict] = []
    for r in reversed(rows):  # chronological
        md = r.get("metadata") or {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        calls = md.get("tool_calls") if isinstance(md, dict) else None
        if isinstance(calls, list) and calls:
            item = {"created_at": r.get("created_at"), "tool_calls": calls}
            if md.get("untrusted"):
                item["untrusted"] = md["untrusted"]
            out.append(item)
    return out


async def write_turn_row(
    pool,
    entity_id: int,
    session_id: str,
    role: str,
    content: str,
    *,
    metadata_extra: dict | None = None,
    project_id: int | None = None,
    tags: dict | None = None,
    created_at: datetime | None = None,
) -> int | None:
    """Write a single primary-chat turn as memory_type='turn'.

    Called from session.py after each user message + each assistant response.
    Runs in the background (asyncio.create_task) so chat hot path never blocks
    on the embed call.

    `created_at` is WHEN THE TURN WAS SAID, and callers must pass it. Omitted,
    the column falls back to its now() default — which is the INSERT time, not
    the utterance time, and that is a reordering bug, not a rounding one: every
    caller here is a fire-and-forget task that first awaits a variable-latency
    embed (see _write_turn_row_landed). Two turns of one exchange race, and the
    shorter text embeds first, so a short reply is routinely stamped BEFORE the
    long message that prompted it. The chat surfaces sort strictly on this
    column (workbench-web chat-pane loadHistory, circles-web's twin), so the
    answer renders above the question. In PII mode the gap is far wider still: the drain worker
    lands the row an arbitrary time later.

    No LLM cost. Just embed + insert. Typed memories are written in the turn
    (close_turn, memory_curate), never derived from these rows afterwards.

    Per-app chat surfacing:
      `project_id` populates the entity_memory.project_id typed column.
      `tags` is merged into the existing `metadata` JSONB column with
      standard keys: app_id, originating_app, artifact_id, cc_thread_id.
      No schema change — both column targets already exist. Callers
      pass these from the request context (which app token, which
      project the user has active, which artifact is in focus).

    Returns the inserted row id, or None on failure (logged, non-raising —
    a missed turn row is recoverable from sess.messages and is far less
    damaging than dropping the user-facing response).

    PII mode: mode OFF, `_writer` below runs
    directly — byte-identical behavior. Mode ON, the CONTENT field goes
    through the in-memory pii_queue and lands TOKENIZED via the drain
    worker (scope = the conversation thread), AND the sibling metadata/tags
    are scrubbed to structural keys before submit (free-text display hints
    — attachment filenames, prompt previews, agent titles — are stripped,
    not tokenized; see pii_queue.scrub_metadata). Neither the content field
    nor its siblings persist raw user text in mode ON.
    """
    if not content or not content.strip():
        return None

    from mastercode import pii_queue as pii_guard
    if pii_guard.enabled():
        # The queue tokenizes the text arg ONLY — metadata/tags would land
        # raw in entity_memory.metadata.
        metadata_extra = pii_guard.scrub_metadata(metadata_extra)
        tags = pii_guard.scrub_metadata(tags)
    # A turn that read untrusted content writes its rows marked, here, while
    # the turn is still the caller (the queue's drain worker runs outside
    # it): a later turn that reads this row back as history is marked too
    # (runtime/turn_taint).
    from mastercode.runtime import turn_taint as _turn_taint
    metadata_extra = _turn_taint.stamp(metadata_extra) or None

    # Stamped HERE, not at insert time: this is the last point that still runs
    # synchronously with the caller, before the embed await and (in PII mode)
    # before the queue hop.
    said_at = created_at or datetime.now(timezone.utc)

    async def _writer(text: str):
        return await _write_turn_row_landed(
            pool, entity_id, session_id, role, text,
            metadata_extra=metadata_extra, project_id=project_id, tags=tags,
            created_at=said_at,
        )

    return await pii_guard.submit(
        content, f"thread:{session_id}", _writer, verify=False,
        pool=pool, entity_id=entity_id,
    )


async def _write_turn_row_landed(
    pool,
    entity_id: int,
    session_id: str,
    role: str,
    content: str,
    *,
    metadata_extra: dict | None = None,
    project_id: int | None = None,
    tags: dict | None = None,
    created_at: datetime | None = None,
) -> int | None:
    """Embed + insert one turn row. Called directly (PII mode off) or by the
    pii_queue drain worker with already-tokenized text (mode on) — so the
    embed below never sees raw text in PII mode either.

    `created_at` is written EXPLICITLY when the caller supplied one, so the row
    carries the utterance time rather than the post-embed insert time. None
    keeps the column's now() default.

    landed_at (v216) is when the row's words were last written: set here in
    the INSERT and again by every rewrite (_write_turn_row_by_token_landed),
    never earlier than created_at, so COALESCE(landed_at, created_at) is the
    later of the two on every row. created_at is when the words were SAID,
    which the chat orders by: a reply is stamped when it began to reach the
    person, and a turn that holds keeps that stamp when its final reply is
    written over the row, minutes or hours later. What landed since the
    person last looked is read from landed_at (projects._PAGE_MARKS_JOINS,
    through idx_entity_memory_project_replies); a column, so the projects
    list never fetches a reply's metadata to read it."""
    try:
        embedding_pgvec = await _get_memory_embedding(content)
    except Exception as e:
        log.warning("turn-row embed failed (entity=%d session=%s role=%s): %s",
                    entity_id, session_id, role, e)
        embedding_pgvec = None

    md = {
        "source_kind": "chat_turn",
        "role": role,
    }
    if metadata_extra:
        md.update(metadata_extra)
    if tags:
        # Standard keys: source_context ({kind,ref} — the scoping
        # descriptor recall matches on), app_id, originating_app,
        # artifact_id, cc_thread_id. Anything else passes through to JSONB.
        md.update({k: v for k, v in tags.items() if v is not None})

    try:
        if embedding_pgvec:
            # Multi-dim: route the content vector into the column matching the
            # entity's active embedding model's dim — the SAME routing every
            # other writer uses (_insert_new_memory, code_rag.embed_memory,
            # the JobRunner). Hardcoding the 1024d `embedding` column silently
            # dropped EVERY turn row whenever the entity ran a non-1024 model
            # (768 nomic / 4096 qwen3): the vector failed the column's dim check,
            # the INSERT raised, and the turn was lost. content_col is a
            # whitelisted name (injection-safe). Also stamp embedding_model so
            # the row records which model produced its vector (was left NULL).
            content_col, dim = await active_embedding_column(pool, entity_id)
            cast = embedding_cast_type(dim)
            row_id = await pool.fetchval(
                f"""
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata, {content_col}, embedding_model, project_id,
                   created_at, landed_at)
                VALUES
                  ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb, $5::{cast}, $6, $7,
                   COALESCE($8::timestamptz, now()), GREATEST(clock_timestamp(), $8::timestamptz))
                RETURNING id
                """,
                entity_id, content, session_id, json.dumps(md), embedding_pgvec,
                last_embed_model(), project_id, created_at,
            )
        else:
            global unembedded_store_count
            unembedded_store_count += 1
            row_id = await pool.fetchval(
                """
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata, project_id, created_at, landed_at)
                VALUES
                  ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb, $5,
                   COALESCE($6::timestamptz, now()), GREATEST(clock_timestamp(), $6::timestamptz))
                RETURNING id
                """,
                entity_id, content, session_id, json.dumps(md), project_id,
                created_at,
            )
    except Exception as e:
        log.warning("turn-row insert failed (entity=%d session=%s role=%s): %s",
                    entity_id, session_id, role, e)
        return None
    _src = md.get("source_context")
    if row_id and isinstance(_src, dict) and _src.get("kind") == "thread":
        await _archive_if_thread_deleted(pool, row_id, str(_src.get("ref") or ""))
    return row_id


# status_reason for every row a thread's deletion archived (circles_threads.
# delete_thread, and the late-row guard below).
async def write_turn_row_by_token(
    pool,
    entity_id: int,
    session_id: str,
    role: str,
    content: str,
    *,
    token: str,
    held: bool,
    row_id: int | None = None,
    project_id: int | None = None,
    tags: dict | None = None,
    created_at: datetime | None = None,
) -> int | None:
    """The ONE row of a turn that holds open.

    Written BEFORE the turn holds, with the reply as it stands (`held=True`),
    so a Stop, a crash or a restart during the hold cannot lose what the person
    already read; rewritten IN PLACE when the turn ends (`held=False`) with the
    final reply and tags — content, vector, metadata — so the thread keeps one
    row per turn, stamped when the reply began. The row is found by `row_id`
    when the first write returned it, else by the turn's token (PII mode: the
    queue returns no id). pii_queue is FIFO with one drain worker, so a rewrite
    submitted after the first write lands after it.

    A held row carries metadata.held = true; the turn consolidator leaves it
    alone while the turn may still rewrite it (turn_consolidator
    TURN_QUEUE_WHERE). Returns the row id, or None (logged, non-raising, as
    write_turn_row)."""
    if not content or not content.strip() or not token:
        return None
    from mastercode import pii_queue as pii_guard
    tags = dict(tags or {})
    tags["turn_token"] = token
    if held:
        tags["held"] = True
    if pii_guard.enabled():
        tags = pii_guard.scrub_metadata(tags)
    # The held row and its rewrite are marked like write_turn_row's.
    from mastercode.runtime import turn_taint as _turn_taint
    tags = _turn_taint.stamp(tags)
    said_at = created_at or datetime.now(timezone.utc)

    async def _writer(text: str):
        return await _write_turn_row_by_token_landed(
            pool, entity_id, session_id, role, text, token=token, held=held,
            row_id=row_id, project_id=project_id, tags=tags, created_at=said_at)

    return await pii_guard.submit(
        content, f"thread:{session_id}", _writer, verify=False,
        pool=pool, entity_id=entity_id,
    )


async def _write_turn_row_by_token_landed(
    pool, entity_id: int, session_id: str, role: str, content: str, *,
    token: str, held: bool, row_id: int | None, project_id: int | None,
    tags: dict, created_at: datetime,
) -> int | None:
    try:
        if row_id is None:
            row_id = await pool.fetchval(
                "SELECT id FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn' "
                "AND metadata->>'turn_token' = $2 AND metadata->>'role' = $3 "
                "ORDER BY id DESC LIMIT 1", entity_id, token, role)
    except Exception as e:  # noqa: BLE001
        log.warning("held turn row lookup failed (entity=%d token=%s): %s", entity_id, token, e)
        row_id = None
    if row_id is None:
        return await _write_turn_row_landed(
            pool, entity_id, session_id, role, content,
            project_id=project_id, tags=tags, created_at=created_at)
    try:
        embedding_pgvec = await _get_memory_embedding(content)
    except Exception as e:  # noqa: BLE001
        log.warning("held turn row embed failed (entity=%d row=%s): %s", entity_id, row_id, e)
        embedding_pgvec = None
    md = {"source_kind": "chat_turn", "role": role}
    md.update({k: v for k, v in (tags or {}).items() if v is not None})
    try:
        content_col, dim = await active_embedding_column(pool, entity_id)
        # Written again, so landed again: landed_at moves, created_at stays
        # when the reply began.
        if embedding_pgvec:
            cast = embedding_cast_type(dim)
            await pool.execute(
                f"UPDATE entity_memory SET content = $2, "
                f"metadata = (COALESCE(metadata, '{{}}'::jsonb) - 'held') || $3::jsonb, "
                f"{content_col} = $4::{cast}, embedding_model = $5, project_id = $6, "
                "landed_at = GREATEST(clock_timestamp(), created_at) "
                "WHERE id = $1",
                row_id, content, json.dumps(md), embedding_pgvec, last_embed_model(), project_id)
        else:
            # No vector for the new text: the old one would describe other
            # words, so it goes (the insert path leaves it NULL the same way).
            await pool.execute(
                f"UPDATE entity_memory SET content = $2, "
                f"metadata = (COALESCE(metadata, '{{}}'::jsonb) - 'held') || $3::jsonb, "
                f"{content_col} = NULL, project_id = $4, "
                "landed_at = GREATEST(clock_timestamp(), created_at) WHERE id = $1",
                row_id, content, json.dumps(md), project_id)
    except Exception as e:  # noqa: BLE001
        log.warning("held turn row rewrite failed (entity=%d row=%s): %s", entity_id, row_id, e)
        return None
    return row_id


THREAD_DELETED_REASON = "circles_thread_deleted"


async def _archive_if_thread_deleted(pool, row_id: int, thread_ref: str) -> None:
    """A turn row that lands in a thread the person DELETED is archived on
    arrival, done and never mined: a reply that finished, a stopped-turn note, a
    fire-and-forget user row can all land after the delete archived the rest.
    FOR SHARE waits out a delete in flight, so the row is either archived by
    it or by this. Never raises: the row is written either way."""
    try:
        _uuid.UUID(thread_ref)
    except (ValueError, TypeError):
        return
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                gone = await conn.fetchval(
                    "SELECT deleted_at IS NOT NULL FROM circles_threads "
                    " WHERE id = $1::uuid FOR SHARE", thread_ref)
                if gone:
                    await conn.execute(
                        "UPDATE entity_memory SET status = 'archived', status_reason = $2, "
                        "status_changed_at = NOW() "
                        "WHERE id = $1", row_id, THREAD_DELETED_REASON)
    except Exception as e:  # noqa: BLE001 — a missing table, a stand-in pool
        log.debug("late-row guard skipped (row=%s thread=%s): %s", row_id, thread_ref, e)


# Allowlist of governance floor event names. The event log is
# content-free BY CONSTRUCTION: a caller can only ever pick one of these
# fixed strings — adding a new event means extending this tuple.
GOVERNANCE_EVENT_TYPES = (
    "crisis_gate",
)


async def write_governance_event(
    pool,
    entity_id: int,
    session_id: str,
    event: str,
) -> int | None:
    """Governance floor evidence: one content-free
    row per floor event (e.g. "crisis_gate"), so the operator can PROVE the
    floor fired — counts + timestamps feed the 2027-07 CA report.

    content holds ONLY a small json {"event", "ts"} — never user text, never
    model output (logging content would violate the doctrine the floor
    implements). `event` is allowlisted (GOVERNANCE_EVENT_TYPES) so the
    content-free guarantee holds against FUTURE callers too: an event name
    is a fixed string, never a payload. No embedding: the row is an audit
    record, not a retrievable memory (NULL embedding also keeps it out of
    every similarity scan), and the write must not depend on the embed lane
    — the lexical crisis floor exists precisely for the embedder-down case.

    memory_type='governance_observation' requires migration v103.
    Returns the inserted row id, or None on failure (logged, non-raising —
    the referral/gate the event records must never fail with the counter).
    """
    if event not in GOVERNANCE_EVENT_TYPES:
        log.error("governance event %r not in GOVERNANCE_EVENT_TYPES — refused "
                  "(entity=%d): the event log stores fixed names, never payloads",
                  event, entity_id)
        return None
    content = json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    try:
        return await pool.fetchval(
            """
            INSERT INTO entity_memory
              (entity_id, content, memory_type, importance, status,
               source_session, metadata)
            VALUES
              ($1, $2, 'governance_observation', 1.0, 'active', $3, $4::jsonb)
            RETURNING id
            """,
            entity_id, content, session_id,
            json.dumps({"source_kind": "governance_floor"}),
        )
    except Exception as e:
        log.error("governance event insert failed (entity=%d event=%s): %s",
                  entity_id, event, e)
        return None


# The types a restatement may carry a row into (memory_consolidation,
# circles_threads): the kept types of migrations/v235_one_memory.sql that a
# conversation writes. turn, research, cited_fact and governance_observation
# have their own single writers.
MEMORY_TYPES = ("preference", "goal", "relationship", "summary", "constitutional")

# OPEN-LOOP TYPES — the memory types that describe a MOMENT in the work rather
# than a standing truth about the person. A goal, a decision and an open thread
# are all statements about where things stood when they were said; three weeks
# later the goal is met, the decision is superseded, the thread is closed, and
# the row still reads as current.
#
# Measured on one Host: 75 goals, 80 decisions and 43 unresolved rows older
# than a week were still active at importance up to 2, and one desktop-style
# goal had been served 110 times. The standing list's existing recency term is a 30-day half-life, which
# at importance 2 leaves a three-week-old row scoring above a fresh importance-1
# fact — so age was priced in, just far too cheaply for these three types.
#
# PREFERENCES ARE NOT HERE, deliberately. A preference is a standing fact about
# the person ("prefers the simplest solution first") and does not expire by
# sitting still; decaying it would make the host forget how someone likes to be
# worked with, which is the opposite of the fix. Same for fact / relationship /
# skill_memory / moral_precedent.
OPEN_LOOP_TYPES = ("goal",)  # decision and unresolved were retired by v235

# Half-life in DAYS for the open-loop decay, applied to the ranking score only.
# A week is the span over which this work's goals and decisions actually turn
# over, so a row from last week is worth about half a row from today and one
# from a month ago is worth about a sixteenth — enough to put a fresh fact above
# a stale importance-2 goal without erasing the old row, which stays in the
# corpus and stays findable by an explicit query.
OPEN_LOOP_HALF_LIFE_DAYS = 7.0

# Rows an extraction candidate may NEVER supersede. The consolidation
# classifier is offered the memories nearest a candidate and may close one of
# them bi-temporally; these five must never be in that set.
#
# Measured: `turn` was. A derived statement superseded the raw user
# message it had been derived FROM — t_valid_until closed the turn row, so the
# person's own words left recall_recent_turns and vanished from the thread. A
# turn row is the conversation log, not a belief about the world, and nothing
# derived from it may retire it.
#
# The rest by the same test — is this row a belief the extractor is entitled to
# revise?
#   summary        no writer now (the rolling summary is retired, v241)
#                  chain and versions it. Two writers on one chain is how a
#                  live row gets superseded twice.
#   constitutional prescriptive guardrail text, never user-derived.
#   governance_observation  an audit record of what happened. Rewriting the
#                  audit trail from conversation defeats the point of it.
#   skill_memory   what the person EXPLICITLY taught ("remember this: ..."),
#                  which outranks anything inferred. _merge_duplicates
#                  excludes these from automatic merging for the same
#                  reason; this closes the other door to the same rows.
#
# fact / preference / goal / emotion / topic / relationship / decision /
# unresolved stay supersedable — they ARE the extractor's own output and
# correcting them is the whole feature. research / knowledge / legacy stay
# supersedable too: ordinary content memories, of which consolidate_new is a
# legitimate editor.
NEVER_SUPERSEDED_TYPES = (
    "turn", "summary", "constitutional", "governance_observation",
)

# RULES ARE PREFERENCES.
# A rule the person taught ("Remember this:") and one the Host learned
# (<learned>, runtime/skill_events.py) are `preference` rows marked
# `metadata.rule_kind`, so they are scoped, listed
# and corrected the way every preference is (memory_scope, memory_curate).
# They were `skill_memory` rows; the boot pass moves them over
# (memory_consolidation.retype_rules; RULES_ROLLBACK_SQL there reverses every
# rule, moved or written as one), and until it has run a rule may still be
# one, so every rule reader takes both shapes (rule_row_sql).
#
# What a rule keeps from `skill_memory`: nothing AUTOMATIC retires, merges or
# decays it — not close_turn's near-duplicate supersede, not the consolidator's
# merge, decay or correction, not the Memory app's sweep of unretrieved rows
# (nothing marks a rule read). What it gives up, by design (allowed, in the
# open): the Host may replace or drop one with
# memory_curate when asked — a call the person sees in the thread.
RULE_KINDS = ("taught", "learned")

# The weight a taught rule is stored at ("Things You Were Taught" shows the top
# five rules by weight, newest first among equals). One writer stores every
# taught rule at it: MemoryEngine._store_teaching_memory.
TAUGHT_IMPORTANCE = 2.0


def rule_row_sql(alias: str = "") -> str:
    """SQL true for a rule row: a preference marked `rule_kind` (the
    skill_memory shape was retyped or retired by migrations/v235)."""
    a = f"{alias}." if alias else ""
    return f"({a}memory_type = 'preference' AND COALESCE({a}metadata ? 'rule_kind', false))"


# WHERE A RULE RIDES — the one definition (three separate paths once lost
# the same rule). A rule's REACH is:
#   - everywhere, when nobody has decided a place for it or it was decided
#     global;
#   - otherwise its decided place UNION every place the person TAUGHT it
#     ("Remember this:", first time or again: a held_in entry marked taught,
#     memory_retrieval.HELD_IN_TAUGHT). A decided place that no longer exists
#     (a deleted thread a narrowing named) still counts as decided: the rule
#     then rides only where it was taught (circles_threads._forget keeps such
#     a decision while a taught place is live).
# rule_reach is the Python form; rule_place_clause / rule_everywhere_sql
# render the same definition for a turn's read, and memory_retrieval.
# place_key_sql ('places' of a rule row) for every covers test. Every path
# that asks where a rule rides goes through one of them: the taught block,
# close_turn._held_row, close_turn._covers, tools._correction_scope and the
# replace's duplicate shortcut, memory_scope's report and list, retire-into-
# rule. scripts/test_memory_integrity_db.py checks the Python and the SQL
# forms agree.


def rule_taught_places(metadata) -> list[dict]:
    """The places the person taught a rule in: its held_in entries marked
    taught (and not provenance-only), each once."""
    md = metadata if isinstance(metadata, dict) else {}
    out: list[dict] = []
    for e in md.get("held_in") or []:
        if not (isinstance(e, dict) and e.get("taught") and "about" not in e
                and isinstance(e.get("kind"), str) and isinstance(e.get("ref"), str)):
            continue
        place = {"kind": e["kind"], "ref": e["ref"]}
        if place not in out:
            out.append(place)
    return out


def rule_reach(metadata) -> tuple[bool, list[dict]]:
    """(everywhere, places) — where a rule rides, by the one definition
    above. `places` is empty when `everywhere`."""
    md = metadata if isinstance(metadata, dict) else {}
    about = md.get("about")
    if not isinstance(about, dict) or about.get("kind") == "global":
        return True, []
    places: list[dict] = []
    if about.get("kind") and about.get("ref") is not None:
        places.append({"kind": str(about["kind"]), "ref": str(about["ref"])})
    for p in rule_taught_places(md):
        if p not in places:
            places.append(p)
    return False, places


def rule_everywhere_sql(alias: str = "") -> str:
    """SQL true for a row nobody has decided a place for, or decided global —
    a rule that rides everywhere (rule_reach) — what a rule reader with no
    turn scope may show without leaking a rule decided about one project, app
    or thread into every other."""
    a = f"{alias}." if alias else ""
    return (f"(NOT COALESCE({a}metadata ? 'about', false) "
            f"OR {a}metadata->'about'->>'kind' = 'global')")


def rule_place_clause(places, next_param: int, alias: str = "") -> tuple[str, list]:
    """(SQL, args) for where a rule rides — rule_reach rendered for a turn's
    read; every "Things You Were Taught" reader applies it. A rule nobody has decided about, or decided global,
    rides everywhere (rule_everywhere_sql); one decided about a project, an
    app or a thread rides in a turn there, and in every conversation the
    person TAUGHT it in (a taught held_in record, memory_retrieval.
    HELD_IN_TAUGHT: the first "Remember this:" and every repeat write one):
    a teaching is the person's own word, so where they taught it the rule
    rides, and the Host's narrowing holds everywhere else. A record the Host
    or another writer left by saying the words again never widens it.
    `places` is the turn's place
    ({kind, ref}), or a list of them — a project-scoped type's turn adds its
    projects, as the pre-fire's turn scope does (federated_recall). No place
    known: everywhere rules only. `next_param` is the first placeholder number
    the args take."""
    from mastercode.memory_retrieval import _said_anywhere
    seq = places if isinstance(places, (list, tuple)) else [places]
    a = f"{alias}." if alias else ""
    ors, args = [rule_everywhere_sql(alias)], []
    for sc in seq:
        sc = sc if isinstance(sc, dict) else {}
        kind = str(sc.get("kind") or "")
        if not kind:
            continue
        k = next_param + len(args)
        ors.append(f"({a}metadata->'about'->>'kind' = ${k} AND {a}metadata->'about'->>'ref' = ${k + 1})")
        ors.append(_said_anywhere(f"jsonb_build_object('kind', ${k}::text, 'ref', ${k + 1}::text)",
                                  a + "metadata", taught_only=True))
        args += [kind, str(sc.get("ref") or "")]
    return (ors[0] if len(ors) == 1 else "(" + " OR ".join(ors) + ")"), args


def typed_rule_clause(source_context, project_ids, next_param: int,
                      alias: str = "") -> tuple[str, list]:
    """(SQL, args) for the rules a turn reads in "Things You Were Taught".
    Every turn: where the rule rides (rule_place_clause). A project-scoped
    type's turn (`project_ids` not None) applies, besides, the one test its
    pre-fire applies to a plain preference (memory_retrieval.
    project_rows_sql): tagged with one of its projects, decided about one, or
    decided global — and rides at the turn's place or one of its projects. A
    rule nobody has decided about and nobody has tagged rides everywhere else
    and is not read there, as an untagged plain preference is not."""
    if project_ids is None:
        return rule_place_clause(source_context, next_param, alias)
    from mastercode.memory_retrieval import project_rows_sql
    places = [source_context] + [{"kind": "project", "ref": str(int(p))} for p in project_ids]
    place_sql, place_args = rule_place_clause(places, next_param + 1, alias)
    return (f"({project_rows_sql(next_param, alias)} AND {place_sql})",
            [[int(p) for p in project_ids]] + place_args)


def not_rule_sql(alias: str = "") -> str:
    """SQL true for every row but a rule-marked preference — the clause each
    automatic preference path adds (a NULL metadata is not a rule)."""
    a = f"{alias}." if alias else ""
    return f"NOT COALESCE({a}metadata ? 'rule_kind', false)"


def same_words_key(text) -> str:
    """The key two memory lines share when they say the same words: case and
    whitespace folded. The per-turn block's dedup (prefired_keys) has always
    keyed on it; every same-words check (memory_scope's list, recall, the
    Memory app) uses this one."""
    return " ".join(str(text or "").lower().split())


async def rules_holding(pool, entity_id: int) -> dict:
    """{same_words_key: the lowest live rule id} — a plain preference in
    those words is held by that rule. Rules marked rule_kind only: a stored
    skill left as skill_memory is not a rule (the label every surface uses)."""
    out: dict = {}
    for r in await pool.fetch(
            "SELECT id, content FROM entity_memory WHERE entity_id = $1 "
            "AND memory_type = 'preference' AND COALESCE(metadata ? 'rule_kind', false) "
            "AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW()) "
            "ORDER BY id", entity_id):
        out.setdefault(same_words_key(r["content"]), int(r["id"]))
    return out


def is_rule(memory_type: str | None, metadata) -> bool:
    """The Python side of rule_row_sql."""
    if memory_type == "skill_memory":
        return True
    if memory_type != "preference":
        return False
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:  # noqa: BLE001
            return False
    return isinstance(metadata, dict) and "rule_kind" in metadata

# Patterns that trigger teaching mode — content after trigger is stored as teaching memory
_TEACHING_PATTERNS = [
    (re.compile(r"^remember this:\s*", re.IGNORECASE), "remember this:"),
    (re.compile(r"^always remember:\s*", re.IGNORECASE), "always remember:"),
    (re.compile(r"^never forget:\s*", re.IGNORECASE), "never forget:"),
    (re.compile(r"^learn this:\s*", re.IGNORECASE), "learn this:"),
    (re.compile(r"^important:.*?remember\s*", re.IGNORECASE), "important: remember"),
    (re.compile(r"^teaching mode:\s*", re.IGNORECASE), "teaching mode:"),
]

# Signals that mark a memory as internal context (not for voice actor prompt)
CONTEXT_SIGNALS = [
    "s3 ", "s3.", "oracle", "endpoint", "architecture", "pipeline",
    "debug", "deploy", "codebook", "checkpoint", "schema", "migration",
    "server", "backend", "nginx", "uvicorn", "tmux", "pgvector",
    "asyncpg", "webhook", "api endpoint", "api call", "function call",
    "tool call", "grok api", "prompt engineer", "system prompt",
    "entity_registry", "entity_memory",
    "neurotransmitter", "nt level", "nt state", "perception bus",
    "signal extract", "context inject", "thinking mode",
]


def classify_visibility(memory_text: str) -> str:
    """Classify whether a memory is internal context or ordinary content.

    Returns 'context' for internal/system material, 'default' otherwise.
    The non-context value matches the column default (migrations/v166) so
    "topic" has one meaning on entity_memory — a conversation topic, which
    Circles threads record — and nothing reads this column by value."""
    lower = memory_text.lower()
    for signal in CONTEXT_SIGNALS:
        if signal in lower:
            return "context"
    return "default"

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "of", "and", "or",
    "but", "for", "not", "are", "was", "were", "be", "been", "has", "have",
    "had", "do", "does", "did", "will", "would", "could", "should", "can",
    "may", "might", "shall", "that", "this", "with", "from", "by", "as",
    "if", "so", "no", "yes", "its", "my", "your", "his", "her", "our",
    "their", "i", "me", "we", "you", "he", "she", "they", "them", "what",
    "who", "how", "when", "where", "why", "which", "all", "each", "any",
    "some", "about", "just", "like", "very", "really", "also", "then",
    "than", "more", "much", "up", "out", "into", "over", "am", "im",
    "dont", "cant", "wont", "ok", "okay", "yeah", "hey", "hi", "hello",
})


def _extract_keywords(text: str) -> list[str]:
    """Extract top 8 meaningful keywords from conversation text."""
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    seen = set()
    keywords = []
    for w in words:
        if w not in _STOP_WORDS and w not in seen:
            seen.add(w)
            keywords.append(w)
    # Sort by length (longer = more specific), take top 8
    keywords.sort(key=len, reverse=True)
    return keywords[:8]


def _avg_relevance(memories: list[dict]) -> float:
    """Average relevance score across memory list."""
    if not memories:
        return 0.0
    return sum(m.get("relevance", 0.0) for m in memories) / len(memories)


_SYS_NOUN_RE = re.compile(
    r"\b(harness(?:es)?|model(?: row)?|provider|api[ -]?key|key|token|endpoint|"
    r"pod|service|bench|orchestrator|operator|routing|role|bridge|sidecar|"
    r"cli|runner|worker|cluster|node|image|build lane|claude[- ]code|codex|"
    r"cursor|kimi|grok|opencode|antigravity|openrouter|anthropic|openai)\b",
    re.I,
)
_SYS_STATUS_RE = re.compile(
    r"(\b\d+\s*(?:runs?|attempts?|successful|succeeded|failed|failures?|ok|errors?)\b"
    r"|\b(?:runs?|attempts?)\s*[:=]?\s*\d+\b"
    r"|\b(?:not\s+)?(?:set\s*up|configured|connected|disconnected|installed|"
    r"signed[- ]in|logged[- ]in|authenticated|ready|available|unavailable|"
    r"reachable|unreachable|enabled|disabled)\b"
    r"|\bis\s+(?:up|down|dead|broken|healthy|stale)\b"
    r"|\b(?:returning|returned|returns|gave|threw)\s+(?:a\s+)?(?:[45]\d\d|error|timeout)\b"
    r"|\b[45]\d\d\b"
    r"|\btim(?:ed|es)?\s*out\b|\bnot\s+ready\s+within\b"
    r"|\bdefaults?\s+to\b|\bpinned\s+to\b|\bversion\s+v?\d|\bv\d+\.\d+\.\d+\b"
    r"|\b(?:set|routed|routing|pointed|pointing|switched|mapped)\s+to\b"
    r"|\bfails?\s+\d+\s*%|\b\d+\s*/\s*\d+\b|\bkey\s+(?:not\s+)?set\b|\bnot_set_up\b"
    r"|\blast\s+run\s+was\b|\baverag(?:e|ing)\s+\d+)",
    re.I,
)


def is_system_state_snapshot(content: str) -> bool:
    """True when a candidate memory is a status snapshot of the platform's
    own machinery (see the guard note above). Both a system noun and a
    status/count shape must match; either alone is not enough."""
    text = (content or "").strip()
    if not text:
        return False
    return bool(_SYS_NOUN_RE.search(text) and _SYS_STATUS_RE.search(text))


# ---- What a memory is ABOUT -----------------------------------
# Every schema that asks a model for a memory carries a required `about`:
# "person" (what the person said, wanted, decided, asked or felt, or who they
# are) or "machinery" (the state of a system). Machinery is read live and is
# never stored: memories about when something was not working poison the
# context. One Host held "Dispatch on this host currently fails" as a standing rule for seven weeks after the fix, recalled into about
# 40 prompts, and told the person dispatch was broken.
#
# The field is enforced in code, for every memory type. The regex guard above
# is only the fallback for a candidate with no `about` (an older model reply):
# measured on a hand-labelled sample it caught 4 of 55 state snapshots
# and flagged 141 rows that were kept, so it cannot carry the decision itself.
ABOUT_PERSON = "person"
ABOUT_MACHINERY = "machinery"


# The scope of the turn running in THIS task, set by runtime/session.py when it
# derives the turn's source_context: {"session_id", "scope"}. A row written
# during the turn — a curated memory, a learned rule, a retired skill — reads
# its conversation here first, because the newest turn row can be missing or
# another conversation's: a wake-up turn writes no user row, and in PII mode the
# user row lands later. A task started from the turn inherits a copy.
CURRENT_TURN_SCOPE: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_scope", default=None)
# ...and the turn itself, {"session_id", "task", "token"}: `token` is the id
# the turn has from its start (session.py's turn_token, kept on every row of
# the turn, its message row included, and through the PII queue's scrub), and
# `task` the write of its message row (it returns the row's id). What the turn
# learns (close_turn, memory_curate, memory_scope) names the turn in
# metadata.from_turn_tokens, and the row in metadata.from_turns when the write
# knows it, in its stamps too, as a rule taught in the message does
# (session._keep_taught), so it follows the message wherever the message goes
# (circles_threads.carry_learned) and leaves with it. In PII mode the row is
# queued and its id is never known to the write: the token still is.
CURRENT_TURN_ROW: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_row", default=None)
# How long a write in the turn waits for that row (seconds): the bound the
# move at the turn's end waits for it with (thread_choice._await_row).
TURN_ROW_WAIT = 30


async def turn_from_turns(session_id) -> list[int]:
    """[the running turn's message row id] for a write in that turn, waited
    for (bounded, TURN_ROW_WAIT); [] outside a turn of `session_id`, for a
    turn with no message row (a woken turn), or when the row is not written
    (PII mode queues it; a failed write). Never raises."""
    cur = CURRENT_TURN_ROW.get()
    if not (isinstance(cur, dict) and session_id is not None
            and str(cur.get("session_id")) == str(session_id)):
        return []
    task = cur.get("task")
    if task is None:
        return []
    try:
        rid = await asyncio.wait_for(asyncio.shield(task), timeout=TURN_ROW_WAIT)
    except asyncio.CancelledError:
        # The row's own write was cancelled: the write goes on without it. A
        # cancellation of this caller is its own, and goes on up.
        if task.done() and task.cancelled():
            return []
        raise
    except Exception as e:  # noqa: BLE001 — the write stands without its link
        log.info("turn row not known to the write (%s); stored without from_turns", e)
        return []
    try:
        return [int(rid)] if rid else []
    except (TypeError, ValueError):
        return []


def turn_tokens_now(session_id) -> list[str]:
    """[the running turn's token] for a write in that turn (CURRENT_TURN_ROW),
    known from the turn's start, so a queued write captures it before it is
    queued; [] outside a turn of `session_id`. Read in the turn's own task: a
    queued write lands in the drain worker, whose context is not the turn's."""
    cur = CURRENT_TURN_ROW.get()
    if not (isinstance(cur, dict) and session_id is not None
            and str(cur.get("session_id")) == str(session_id)):
        return []
    tok = cur.get("token")
    return [str(tok)] if tok else []


# ...and the write-tag it runs under, {"session_id", "tag"}: a replay, an A/B
# variant or a fired wake-up (HIDDEN_LANE_APP_IDS) runs in a conversation
# without being part of it, so the memory tools write nothing from it — the
# same rule close_turn keeps (runtime/session._CLOSE_TURN_NO_WRITES).
CURRENT_TURN_WRITE_TAG: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_write_tag", default=None)
# ...and whether the platform started it with no person in it yet,
# {"session_id", "woken"} (runtime/session.py: a woken turn, until the person's
# words are read into it). The build door reads it: a phase's AUTOMATIC repairs
# are spent only by such turns (tools.PHASE_AUTO_REPAIRS); a person who asks for
# another build gets it.
CURRENT_TURN_WOKEN: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_woken", default=None)


# What "Remember this:" kept word for word as this turn's message arrived,
# {"session_id", "texts"}: close_turn does not keep the same rule a second
# time as a preference in the same turn.
CURRENT_TURN_TAUGHT: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_taught", default=None)


# The room the turn running in THIS task is in, the shape escalate records on
# its ticket (`asked_in`: {app_id, project_id, thread_id, interaction_context},
# a fired wake-up mapped to the room it reports into). Set by
# runtime/session.py beside CURRENT_TURN_SCOPE. Whatever the platform files
# during the turn — a ticket (routes/tickets.create_ticket_service), a
# connector pairing (nac_connector.propose_connector) — reads it here, so its
# card comes up in that conversation and its answer starts a turn there. A
# task started from the turn inherits a copy; an HTTP request does not, so
# nothing an app files through POST /api/tickets is linked to a turn.
CURRENT_TURN_ROOM: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_room", default=None)


def current_turn_room() -> dict | None:
    """A copy of the running turn's room, or None outside a turn."""
    cur = CURRENT_TURN_ROOM.get()
    return dict(cur) if isinstance(cur, dict) and cur.get("app_id") else None


# The turn type the turn running in THIS task resolved to, {"session_id",
# "name"}: set by runtime/session.py once the type is known. Every memory row
# the turn writes is stamped with it (entity_memory.turn_type, migrations/v241)
# by the platform, never by the model.
CURRENT_TURN_TYPE: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "current_turn_type", default=None)


def turn_type_now(session_id=None) -> str | None:
    """The running turn's type name for stamping, or None outside a turn (or
    in another session's). 'untyped' for a turn with no type."""
    cur = CURRENT_TURN_TYPE.get()
    if not isinstance(cur, dict):
        return None
    if session_id is not None and str(cur.get("session_id")) != str(session_id):
        return None
    return str(cur.get("name") or "untyped")


def taught_this_turn(session_id) -> list[str]:
    cur = CURRENT_TURN_TAUGHT.get()
    if isinstance(cur, dict) and str(cur.get("session_id")) == str(session_id):
        return [str(t) for t in cur.get("texts") or []]
    return []


def hidden_lane_turn(session_id=None) -> str | None:
    """The write-tag of the running turn when it is a hidden lane's (its
    memory writes are refused), else None. Read from this task's context,
    whatever the caller's session id: an agent the turn dispatched runs under
    its own id and inherits the context, and is the same hidden lane. Every
    turn sets its own tag as it starts, so a real turn never reads another's.
    A finished variant's tag stays on its task (the /replay request's, say)
    until another turn there replaces it; only memory_curate and memory_scope
    read it, and only inside a turn."""
    cur = CURRENT_TURN_WRITE_TAG.get()
    if isinstance(cur, dict) and cur.get("tag") in HIDDEN_LANE_APP_IDS:
        return str(cur["tag"])
    return None


async def session_source_context(pool, entity_id: int, session_id) -> dict | None:
    """The scope descriptor ({kind, ref}) of a session's newest turn row, so a
    row written outside the turn writer — a derived fact, a learned rule, a
    curated memory — lands in the thread that produced it. Inside a running
    turn of that session, the turn's own scope (CURRENT_TURN_SCOPE). None for
    a sessionless caller or a session with no scoped turn."""
    if not session_id or pool is None:
        return None
    cur = CURRENT_TURN_SCOPE.get()
    if (isinstance(cur, dict) and str(cur.get("session_id")) == str(session_id)
            and isinstance(cur.get("scope"), dict) and cur["scope"].get("kind")):
        return cur["scope"]
    try:
        sc = await pool.fetchval(
            "SELECT metadata->'source_context' FROM entity_memory "
            "WHERE entity_id = $1 AND source_session = $2 "
            "AND memory_type = 'turn' "
            "AND (metadata->'source_context') IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            entity_id, str(session_id),
        )
    except Exception as e:  # noqa: BLE001 — an unscoped row beats no row
        log.warning("source_context lookup failed (session=%s): %s", session_id, e)
        return None
    if not sc:
        return None
    try:
        sc = json.loads(sc) if isinstance(sc, str) else sc
    except (TypeError, ValueError):
        return None
    return sc if isinstance(sc, dict) and sc.get("kind") else None


def is_machinery_memory(mem: dict) -> bool:
    """True when a candidate memory describes machinery state and must not be
    stored. `about` decides; without it, the regex guard decides."""
    mem = mem if isinstance(mem, dict) else {}
    about = str(mem.get("about") or "").strip().lower()
    if about == ABOUT_MACHINERY:
        return True
    if about == ABOUT_PERSON:
        return False
    return is_system_state_snapshot(str(mem.get("content") or ""))


# A dispatch receipt's giveaway phrases, used only when the envelope is present
# but truncated past the point where json.loads can see the "status" key (the
# document store caps content, so a long tool result arrives cut).
_DISPATCH_RECEIPT_RE = re.compile(
    r'"(?:task_id|agent_id|job_id)"\s*:|\brunning in the background\b'
    r'|\bdispatched\b', re.IGNORECASE)
# "{" then, within the first few keys, a "status" field — the envelope shape
# every host tool result is wrapped in.
_ENVELOPE_STATUS_RE = re.compile(r'^\s*\{[^{}]{0,400}?"status"\s*:', re.DOTALL)

# The status VALUES the host's own tool envelope uses (session.py reads
# tool_result["status"], defaulting to "unknown"; the task/dispatch lanes add
# the queued/running/completed set). A `status` key alone is far too common in
# the wild to be a signal — a saved REST response says {"status": "success"} or
# {"status": 200} — so the value has to be one of ours AND a companion key has
# to be present. Both conditions came from the same measurement: narrowing to
# either one alone still swallowed ordinary JSON documents.
_RECEIPT_STATUS_VALUES = frozenset({
    "ok", "error", "blocked", "denied", "refused", "dispatched", "queued",
    "running", "completed", "failed", "skipped", "pending", "unknown",
})
# Keys that only a RECEIPT carries alongside that status: what it dispatched,
# what it is waiting on, or the wrapped payload of a tool call.
_RECEIPT_COMPANION_KEYS = frozenset({
    "task_id", "agent_id", "job_id", "note", "result", "tool", "session_id",
    "error_detail", "next_step",
})
# A receipt that named no status at all still announces itself by its id.
_RECEIPT_ID_KEYS = frozenset({"task_id", "agent_id", "job_id"})


def is_tool_result_envelope(content: str) -> bool:
    """True when a stored document is a TOOL RESULT rather than a document.

    Measured: the pre-fire's documents lane was
    feeding the prompt rows like
    `{"status": "dispatched", "task_id": 343, "note": "Agent 343 is running in
    the background..."}` — four at a time, in the section headed "From Your
    Research & Documents". A dispatch receipt is a fact about the plumbing at
    one instant: stale the moment the agent finishes, displacing real research
    from a bounded section, and read back by the host as though the user had
    provided it.

    The test is the RECEIPT SHAPE, not the tool name and not a bare "status"
    key. A `status` key on its own is ordinary in stored JSON — a saved API
    response, a health payload the user is debugging — and excluding on it
    alone would quietly drop the user's own documents, which is the same class
    of bug in the other direction. So: one of OUR status values AND a companion
    key that only a receipt carries, or a bare task/agent/job id.

    Cheap and never raises: called per candidate row on the chat hot path, and
    it parses at most the leading bytes.
    """
    text = (content or "").lstrip()
    if not text.startswith("{"):
        return False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # Truncated or malformed — the document store caps content, so a long
        # receipt arrives cut mid-object and json.loads can never see its keys.
        # BOTH halves are still required: the envelope's status shape near the
        # front, and a dispatch marker.
        head = text[:400]
        return bool(_ENVELOPE_STATUS_RE.search(text)
                    and _DISPATCH_RECEIPT_RE.search(head))
    if not isinstance(obj, dict):
        return False
    keys = set(obj)
    status = obj.get("status")
    if (isinstance(status, str)
            and status.strip().lower() in _RECEIPT_STATUS_VALUES
            and (keys & _RECEIPT_COMPANION_KEYS)):
        return True
    return bool(keys & _RECEIPT_ID_KEYS)


def _detect_teaching(messages: list[dict]) -> list[dict]:
    """Detect teaching-mode trigger phrases in user messages.

    Returns list of dicts: {content, trigger, original_content}
    """
    teachings = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        for pattern, trigger_label in _TEACHING_PATTERNS:
            match = pattern.match(text)
            if match:
                teaching_content = text[match.end():].strip()
                if teaching_content:
                    teachings.append({
                        "content": teaching_content,
                        "trigger": trigger_label,
                        "original_content": text,
                    })
                break
    return teachings


class MemoryEngine:
    """
    Personality-native memory storage and retrieval.

    Stores facts/preferences/goals/emotions with 10-dimension vectors.
    Retrieves using entity personality weights as the query vector via pgvector.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._consolidator = None

    @property
    def consolidator(self):
        """The Mem0-style classifier extraction routes through.
        Lazy + function-local import — memory_consolidation imports from this
        module, so a top-level import here would be circular."""
        if self._consolidator is None:
            from mastercode.memory_consolidation import MemoryConsolidator
            self._consolidator = MemoryConsolidator(self.pool)
        return self._consolidator

    async def _store_teaching_memory(
        self,
        entity_id: int,
        teaching: dict,
        session_id: Optional[str] = None,
        source_context: Optional[dict] = None,
        from_turns: Optional[list] = None,
        turn_type: Optional[str] = None,
        turn_tokens: Optional[list] = None,
    ) -> Optional[dict]:
        """Store a teaching memory with max importance, personality-agnostic vector.

        `turn_type` is the name of the turn type the teaching turn ran under,
        stamped on the row (entity_memory.turn_type) by the platform. The rule
        arrives with no topic (the words are the person's, kept as they came);
        the Host keys it in its own turn (memory_curate op='key'), and a rule
        then outranks a plain preference in its slot (memory_slots).

        `source_context` is the scope the teaching was said in and `from_turns`
        the turn rows it came from (`turn_tokens` the same turns by their
        token, which is all a PII-mode write knows: CURRENT_TURN_ROW), stamped
        like every other derived row's, so deleting that thread
        (circles_threads.delete_thread) takes what was taught in it too, and a
        Split carries it with its messages.

        A taught rule is a preference marked rule_kind='taught' (RULE_KINDS),
        and this insert is the one way one is taught; "Remember
        this:" always leaves a rule holding the words. The conversation it is
        taught in is recorded as a TAUGHT place (held_in, `taught`), first
        time and every repeat alike: where the person taught a rule, it rides,
        whatever place the Host narrows it to (rule_place_clause). Only two
        kinds of row are looked at. A live rule in these words holds the
        repeat: this conversation is recorded on it as taught (note_held). Every live plain
        preference in these words that the rule is read everywhere of is
        retired into the rule that holds them, the fresh one or the one a
        repeat found (_retire_into_rule, stamped with this conversation, so
        deleting it brings them back): a teaching never narrows where the
        words are read. The
        rule is a fresh teaching: undecided, read everywhere, whatever those
        preferences were scoped to (carry_about=False; the Host narrows it
        with memory_scope), and it ranks in "Things You Were Taught" as the
        newest taught rule. A fact, a goal or any other row in the same words
        is left as it is, and the rule is stored beside it."""
        content = teaching["content"]
        # C9: a teaching memory is durable prompt-surfacing content derived from
        # conversation/model output (an untrusted-to-durable-prompt sink).
        # Screen it through the structural injection guard before storing so a
        # role-break / override / fake-system payload can't be parked as a
        # "taught fact" that resurfaces in a later system prompt. Strip-and-flag;
        # the neutralized text is what gets stored.
        _teach_flags: list[str] = []
        try:
            from mastercode.runtime.input_guard import sanitize as _ig_sanitize
            content, _teach_flags = _ig_sanitize(content)
            content = (content or "").strip()
        except Exception as _ig_err:
            log.warning("teaching memory input_guard sanitize failed: %s", _ig_err)
        if not content:
            log.warning("teaching memory empty after sanitize — not stored")
            return None
        keywords = _extract_keywords(content)
        metadata = {
            "rule_kind": "taught",
            "keywords": keywords,
            "trigger": teaching.get("trigger", ""),
            "skill_memory": True,
            "injection_flags": _teach_flags,  # C9: screening result (audit trail)
        }
        from mastercode.memory_consolidation import turn_marks
        turns = turn_marks({"from_turns": from_turns, "from_turn_tokens": turn_tokens})
        if source_context:
            metadata["source_context"] = source_context
        metadata.update(turns)
        # Where the person taught it, marked as a repeat teaching marks its own
        # place (note_held taught=True): if the Host later narrows the rule,
        # it still rides here (rule_place_clause, memory_retrieval.
        # HELD_IN_TAUGHT). No conversation to name: no place to keep.
        if isinstance(source_context, dict) and source_context.get("kind"):
            from datetime import datetime as _dt, timezone as _tz
            _taught_here = {"kind": str(source_context["kind"]),
                            "ref": str(source_context.get("ref") or ""),
                            "at": _dt.now(_tz.utc).isoformat(), "taught": True, **turns}
            metadata["held_in"] = [_taught_here]
        vec = [1.0] * 10  # Personality-agnostic — surfaces for any entity weight config

        try:
            # A LIVE rule in these words: an archived copy (a deleted
            # thread's) or a retired one must not stop it being taught again.
            held_rule = await self.pool.fetchval(
                f"SELECT id FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                f"AND {rule_row_sql()} AND status = 'active' "
                "AND (t_valid_until IS NULL OR t_valid_until > NOW()) ORDER BY id LIMIT 1",
                entity_id, content,
            )
            # Taught again here: the rule it repeats records this conversation,
            # so deleting the rule's own thread keeps it. Gone by now (a
            # thread's delete took it), the rule is stored here instead.
            from mastercode.memory_consolidation import scope_stamp
            stamp = scope_stamp(source_context, from_turns, turn_tokens)
            holder = None
            if held_rule is not None and await self.consolidator.note_held(
                    entity_id, int(held_rule), stamp, taught=True):
                holder = int(held_rule)
            # Every plain preference in these words is retired into the rule
            # that holds them (the one above, or the one stored below) where
            # that rule is read in all its places — a first teaching and a
            # repeat alike; a copy read where a narrowed rule is not stays.
            plain_ids = [int(r["id"]) for r in await self.pool.fetch(
                f"SELECT id FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                f"AND memory_type = 'preference' AND {not_rule_sql()} AND status = 'active' "
                "AND t_valid_until IS NULL ORDER BY id",
                entity_id, content)]
            if holder is not None:
                await self._retire_into_rule(entity_id, plain_ids, holder, stamp)
                return None

            from mastercode.memory_slots import slot_scope_for
            row = await self.pool.fetchrow("""
                INSERT INTO entity_memory (
                    entity_id, content, memory_type, source_session,
                    importance, confidence, visibility, slot_scope, turn_type, metadata
                ) VALUES ($1, $2, 'preference', $3, $4, 1.0, 'default', $5, $6, $7::jsonb)
                RETURNING id, content, memory_type, importance
            """,
                entity_id, content, session_id, TAUGHT_IMPORTANCE,
                slot_scope_for("preference", metadata), turn_type,
                json.dumps(metadata),
            )
            log.info("Stored teaching memory for entity %d: %s", entity_id, content[:80])
            if row:
                await self._retire_into_rule(entity_id, plain_ids, int(row["id"]), stamp)
                # The same rule, word for word, that a thread marked outdated:
                # this row replaces it, so deleting that thread cannot bring
                # the old copy back beside it.
                await self.consolidator.link_dropped(
                    entity_id, content, "preference", None, int(row["id"]), corrects=False,
                    stamp=stamp, rule=True)
            return dict(row) if row else None
        except Exception as e:
            log.error("Failed to store teaching memory: %s", e, exc_info=True)
            return None

    async def _retire_into_rule(self, entity_id: int, plain_ids: list[int], rule_id: int,
                                stamp: Optional[dict]) -> None:
        """Retire each plain preference into the taught rule that holds its
        words — only one the rule is read in every turn of, typed turns
        included (close_turn._covers, the test close_turn's own restatements
        take): a teaching never takes words from a place they were read. A
        rule nobody has decided about rides every normal turn, but a
        project-scoped type reads it only by its project tag, so a copy such
        a type reads gives the rule its place first (_carry_typed_place). A
        rule the Host narrowed leaves a copy read elsewhere live. Stamped with
        the teaching conversation (deleting it brings them back), and without
        carrying a narrower decision (_store_teaching_memory)."""
        from mastercode.runtime.close_turn import _covers
        for plain_id in plain_ids:
            if not await _covers(self.pool, entity_id, rule_id, plain_id):
                if not (await self._carry_typed_place(entity_id, rule_id, plain_id)
                        and await _covers(self.pool, entity_id, rule_id, plain_id)):
                    continue
            await self.consolidator._supersede(
                entity_id, plain_id, rule_id, superseded_in=stamp, carry_about=False)

    async def _carry_typed_place(self, entity_id: int, rule_id: int, plain_id: int) -> bool:
        """Give a rule nobody has decided about the place a project-scoped
        type reads a plain copy in, so retiring the copy into it takes nothing
        from that type (memory_engine.typed_rule_clause, the pre-fire's own
        project test): its project tag when the copy is read in one project
        and the rule has none, or the copy's decision when it is about the
        person (global: every normal turn reads the rule already, so it adds
        no reach there). Anything else — a decided rule, a rule tagged with
        another project, a copy read in two, another share scope — is carried
        nowhere, and the copy stays live. True when the rule changed."""
        from mastercode.memory_retrieval import place_key_sql
        r = await self.pool.fetchrow(
            f"""
            SELECT COALESCE(n.metadata ? 'about', false) AS decided, n.project_id AS tag,
                   n.memory_type = o.memory_type AS same_type,
                   n.metadata->'share_scope' IS NOT DISTINCT FROM (o.metadata->'share_scope') AS same_share,
                   ({place_key_sql('o')})->'project' AS project
              FROM entity_memory n, entity_memory o
             WHERE n.entity_id = $1 AND n.id = $2 AND o.entity_id = $1 AND o.id = $3
            """, entity_id, rule_id, plain_id)
        if r is None or r["decided"] or not r["same_share"]:
            return False
        project = r["project"]
        project = json.loads(project) if isinstance(project, str) else project
        if project == "any" and r["same_type"]:
            await self.consolidator._carry_about(entity_id, [plain_id], [rule_id])
            return True
        if isinstance(project, list) and len(project) == 1 and r["tag"] is None:
            res = await self.pool.execute(
                "UPDATE entity_memory SET project_id = $3, updated_at = NOW() "
                "WHERE id = $1 AND entity_id = $2 AND project_id IS NULL",
                rule_id, entity_id, int(project[0]))
            return not str(res).endswith(" 0")
        return False

    async def retrieve_teaching(
        self,
        entity_id: int,
        conversation_context: str,
        limit: int = 5,
        source_context: Optional[dict] = None,
        project_ids: Optional[list] = None,
    ) -> list[dict]:
        """Retrieve teaching memories by keyword match against conversation context.

        Unlike cosine retrieval, teaching memories are matched via keyword ILIKE
        to guarantee they surface when relevant keywords appear.

        `source_context` is the turn's place: a rule decided about a place
        rides only there (rule_place_clause), as in the per-turn block. None
        reads only the rules that ride everywhere. `project_ids`: a
        project-scoped type's projects (typed_rule_clause).
        """
        keywords = _extract_keywords(conversation_context)
        if not keywords:
            return []

        # Build OR conditions: match content or metadata keywords
        conditions = []
        params = [entity_id]
        for i, kw in enumerate(keywords[:5]):
            param_idx = len(params) + 1
            conditions.append(
                f"(content ILIKE '%' || ${param_idx} || '%' "
                f"OR metadata->>'keywords' ILIKE '%' || ${param_idx} || '%')"
            )
            params.append(kw)

        if not conditions:
            return []

        where_clause = " OR ".join(conditions)
        kw_place_sql, kw_place_args = typed_rule_clause(source_context, project_ids, len(params) + 1)
        params.extend(kw_place_args)
        params.append(limit)
        limit_idx = len(params)

        rows = await self.pool.fetch(f"""
            SELECT id, content, memory_type, importance, confidence,
                   access_count, created_at, visibility, metadata
            FROM entity_memory
            WHERE entity_id = $1
              AND {rule_row_sql()}
              AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
              AND ({where_clause})
              AND {kw_place_sql}
            ORDER BY importance DESC, created_at DESC
            LIMIT ${limit_idx}
        """, *params)

        # Update access counts
        if rows:
            ids = [r["id"] for r in rows]
            await self.pool.execute("""
                UPDATE entity_memory
                SET access_count = access_count + 1, last_accessed_at = NOW()
                WHERE id = ANY($1)
            """, ids)

        return [
            {
                "content": r["content"],
                "type": "skill_memory",
                "importance": r["importance"],
                "relevance": 1.0,  # Teaching memories always max relevant when matched
                "confidence": r["confidence"],
                "visibility": r["visibility"],
                "source": "skill_memory",
                "age": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
            }
            for r in rows
        ]

    async def retrieve(
        self,
        entity_id: int,
        personality_weights: dict,
        limit: int = 15,
        memory_types: Optional[list[str]] = None,
        min_importance: float = 0.5,
        visibility: Optional[str] = None,
        *,
        query_embedding: Optional[list[float]] = None,
        semantic_weight: float = 0.5,
        recency_half_life_days: float = 30.0,
        open_loop_half_life_days: float = OPEN_LOOP_HALF_LIFE_DAYS,
        include_staged: bool = False,
        mode: Optional[str] = None,
        consumer: Optional[object] = None,
        source_context: Optional[dict] = None,
        count_access: bool = True,
        exclude_rules: bool = False,
    ) -> list[dict]:
        """
        Retrieve memories with multi-factor scoring.

        exclude_rules: leave out rule-marked preferences
        (RULE_KINDS) — the standing preference list, whose rules ride in
        "Things You Were Taught" instead.

        Score per row =
            similarity * importance * tier_boost * recency_factor * open_loop_decay

        Similarity is SEMANTIC-ONLY: cosine sim of the
        content embedding vs query_embedding when one is given; neutral 1.0
        when not (pure importance/tier/recency ordering).
        `personality_weights` and `semantic_weight` are accepted and ignored
        for caller compatibility.
        - tier_boost: 1.5 for memory_tier=0 (identity), 1.2 for tier=1 (policy), else 1.0
        - recency_factor: exp(-age_days / recency_half_life_days)
        - open_loop_decay: a SECOND, steeper exp decay applied only to
          OPEN_LOOP_TYPES (see that constant). 1.0 for every other type.
        - importance: row's intrinsic weight (codebook ≥2.5, personal ~1.0, staged 0.3)

        Filters:
        - status = 'active'
        - importance >= min_importance
        - cache_state ∉ {'staged','expired'} unless include_staged=True
        - a RESOLVED goal/unresolved row is out (resolved_at IS NOT NULL); see
          the resolved-row filter below

        Callers that don't pass query_embedding get importance/tier/recency
        ordering.

        Args:
            entity_id, personality_weights, limit, memory_types, min_importance,
            visibility: as before.
            query_embedding: optional 1024D semantic query vector (e.g. mxbai
                embedding of the user message). If None, semantic term drops out.
            semantic_weight: blend factor in [0,1]; ignored if query_embedding is None.
            recency_half_life_days: temporal decay half-life. Larger = slower decay.
            include_staged: if True, untrusted cached world events surface too.

        Returns:
            List of memory dicts sorted by score DESC.
        """
        # Apply mode rules. Mode is surface-declared by the caller
        # (e.g., chat, orchestrator, research). It restricts what memories the
        # mode can see and which types it emphasizes.
        mode_rules = None
        if mode is not None:
            try:
                from mastercode.modes import rules_for as _rules_for
                mode_rules = _rules_for(mode)
            except Exception:
                mode_rules = None

        params: list = [entity_id, min_importance, limit]
        param_idx = 4

        # Semantic-only similarity. COALESCE so rows without a content
        # embedding score at neutral 0.5 instead of being filtered out.
        if query_embedding is not None:
            # Multi-dim: the content semantic term hits the dim column matching
            # the entity's active model. Whitelisted column name.
            content_col, dim = await active_embedding_column(self.pool, entity_id)
            cast = embedding_cast_type(dim)
            sem_lit = "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]"
            params.append(sem_lit)
            sem_idx = param_idx; param_idx += 1
            semantic_sim_expr = f"(1 - ({content_col} <=> ${sem_idx}::{cast}))"
            sim_blend_expr = f"COALESCE({semantic_sim_expr}, 0.5)"
        else:
            semantic_sim_expr = "NULL::float"
            sim_blend_expr = "1.0"  # pure importance/tier/recency ordering

        # Recency factor with half-life
        params.append(float(recency_half_life_days))
        rh_idx = param_idx; param_idx += 1
        recency_expr = (
            f"EXP(-EXTRACT(epoch FROM (NOW() - created_at)) / 86400.0 / ${rh_idx}::float)"
        )

        # OPEN-LOOP DECAY — a second, steeper decay on the types
        # that describe a moment rather than a standing truth. See
        # OPEN_LOOP_TYPES for what and why. Ranking only: the stored
        # `importance` column is never touched, so nothing here rewrites what
        # the extraction model judged the memory to be worth. A caller that
        # wants the old flat behaviour passes open_loop_half_life_days=0.
        params.append(float(open_loop_half_life_days))
        oh_idx = param_idx; param_idx += 1
        params.append(list(OPEN_LOOP_TYPES)); param_idx += 1
        ot_idx = param_idx - 1
        # LN(2) is not decoration: this is a literal HALF-life, so a row exactly
        # one half-life old scores 0.5. The recency term above is named the same
        # way but computes EXP(-age/T), which is a 1/e time constant (0.368 at
        # T) — left alone here because changing it would move every score in
        # every lane. Two different curves in one product is worth one line of
        # warning rather than a silent mismatch with the parameter's own name.
        open_loop_expr = (
            f"CASE WHEN ${oh_idx}::float > 0 AND memory_type = ANY(${ot_idx}) "
            f"THEN EXP(-LN(2) * EXTRACT(epoch FROM (NOW() - created_at)) / 86400.0 "
            f"/ ${oh_idx}::float) ELSE 1.0 END"
        )

        # Cache-state filter parameter
        params.append(bool(include_staged))
        inc_staged_idx = param_idx; param_idx += 1

        extra_filters = ""

        # Caller's memory_types intersects with mode's deny list (deny wins).
        effective_types = list(memory_types) if memory_types else None
        if mode_rules and mode_rules.type_deny:
            denied = set(mode_rules.type_deny)
            if effective_types:
                effective_types = [t for t in effective_types if t not in denied]
                if not effective_types:
                    # Every requested type is denied: that is nothing, not
                    # the unfiltered space an empty list would fall through to.
                    return []
            else:
                # No caller filter — exclude denied types from the whole space
                extra_filters += f" AND NOT (memory_type = ANY(${param_idx}))"
                params.append(list(denied)); param_idx += 1

        if effective_types:
            extra_filters += f" AND memory_type = ANY(${param_idx})"
            params.append(effective_types); param_idx += 1
        if exclude_rules:
            extra_filters += f" AND {not_rule_sql()}"
        # THE THREAD'S SET. With a scope, the standing list reads
        # this thread's rows plus the types that describe who the person is
        # everywhere (THREAD_SET_ENTITY_WIDE_TYPES). Without one — every caller
        # that has no turn behind it — it reads everything, as before.
        # Rows written OUTSIDE any thread (genesis facts, promoted documents,
        # pre-threading memories) were never another thread's, so they stay
        # entity-wide too — except own words, which a scope-less turn row is
        # not safe to show anywhere. A cross-session pattern about the person
        # (metadata.source='consolidation_pattern') is entity-wide by nature.
        if isinstance(source_context, dict) and source_context.get("kind"):
            # Scope = memory_retrieval.EFFECTIVE_SCOPE: a row the Host set to
            # be about something (metadata.about) is read by that, not by
            # where it was said.
            from mastercode.memory_retrieval import (
                scope_global_sql, scope_match_sql)
            extra_filters += (
                f" AND (memory_type = ANY(${param_idx}) OR "
                f"{scope_match_sql(param_idx + 1, param_idx + 2)} OR "
                f"metadata->>'source' = 'consolidation_pattern' OR "
                f"({scope_global_sql()} "
                f"AND memory_type NOT IN ('turn', 'summary')))")
            params.extend([list(THREAD_SET_ENTITY_WIDE_TYPES),
                           str(source_context.get("kind")),
                           str(source_context.get("ref"))])
            param_idx += 3
        if visibility:
            extra_filters += f" AND visibility = ${param_idx}"
            params.append(visibility); param_idx += 1

        # Mode tier filter: row's memory_tier (if set) must be in the allowed list.
        # Rows without memory_tier always pass — most legacy rows have no tier.
        if mode_rules and mode_rules.tier_filter:
            allowed_tiers = list(mode_rules.tier_filter)
            extra_filters += (
                f" AND (metadata->>'memory_tier' IS NULL "
                f"OR (metadata->>'memory_tier')::int = ANY(${param_idx}))"
            )
            params.append(allowed_tiers); param_idx += 1

        # Tier boost: identity (0) > policy (1) > default
        tier_expr = (
            "CASE "
            "WHEN (metadata->>'memory_tier')::int = 0 THEN 1.5 "
            "WHEN (metadata->>'memory_tier')::int = 1 THEN 1.2 "
            "ELSE 1.0 END"
        )

        # Mode emphasis: boost score for types the mode favors. Multiplicative,
        # small (1.3x) so it nudges ranking without dominating importance/tier.
        if mode_rules and mode_rules.type_emphasis:
            emph_list = list(mode_rules.type_emphasis)
            params.append(emph_list); param_idx += 1
            emph_idx = param_idx - 1
            emphasis_expr = f"(CASE WHEN memory_type = ANY(${emph_idx}) THEN 1.3 ELSE 1.0 END)"
        else:
            emphasis_expr = "1.0"

        score_expr = (
            f"({sim_blend_expr}) * importance * ({tier_expr}) * ({recency_expr}) "
            f"* ({emphasis_expr}) * ({open_loop_expr})"
        )

        # Cache-state filter: when not include_staged, exclude staged/expired
        cache_filter = (
            f"(${inc_staged_idx}::bool "
            "OR metadata->>'cache_state' IS NULL "
            "OR metadata->>'cache_state' NOT IN ('staged','expired'))"
        )

        sql = f"""
            SELECT
                id, content, memory_type, importance, confidence,
                access_count, created_at, visibility, metadata, slot_topic,
                {semantic_sim_expr}    AS semantic_sim,
                {tier_expr}            AS tier_boost,
                {recency_expr}         AS recency_factor,
                {open_loop_expr}       AS open_loop_decay,
                {score_expr}           AS score
            FROM entity_memory
            WHERE entity_id = $1
              AND importance >= $2
              AND status = 'active'
              -- A goal or open thread that has been ANSWERED leaves the
              -- standing list. resolved_at is the exit condition init-db.sql
              -- declares for exactly these two types ("allows goal/unresolved
              -- into prompt context only when resolved_at IS NULL"), and
              -- consolidation is what sets it. Without this clause the column
              -- is written and read by nobody, and a completed goal keeps
              -- being served as though it were still open.
              AND (resolved_at IS NULL
                   OR memory_type NOT IN ('goal', 'unresolved'))
              -- Bi-temporal live window (Ship C): exclude rows that have been
              -- superseded by a later memory. t_valid_until IS NULL means the
              -- row is currently authoritative; an explicit future timestamp
              -- means valid until then. Hard DELETEs are off the table now
              -- (consolidation supersedes instead) so this is the live filter.
              AND (t_valid_until IS NULL OR t_valid_until > NOW())
              AND {cache_filter}
              {extra_filters}
            ORDER BY {score_expr} DESC
            LIMIT $3
        """

        rows = await self.pool.fetch(sql, *params)

        # Bump access counts on what surfaced. Not for the standing list of a
        # prompt (count_access=False): being shown every turn is not being
        # used, and the count feeds _strengthen_accessed, which then raises
        # the importance that put the row in the list — the loop that kept
        # a stale "dispatch fails" rule in ~40 prompts.
        if rows and count_access:
            ids = [r["id"] for r in rows]
            await self.pool.execute(
                "UPDATE entity_memory SET access_count = access_count + 1, "
                "last_accessed_at = NOW() WHERE id = ANY($1)",
                ids,
            )

        results = []
        for r in rows:
            meta = r["metadata"] or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            entry = {
                "id": r["id"],
                "content": r["content"],
                "type": r["memory_type"],
                "topic": r["slot_topic"],
                "created_at": r["created_at"],
                "importance": r["importance"],
                # `relevance` = semantic similarity;
                # neutral 0.5 when retrieval ran without a query embedding.
                "relevance": round(float(r["semantic_sim"]), 4) if r["semantic_sim"] is not None else 0.5,
                "semantic_sim": round(float(r["semantic_sim"]), 4) if r["semantic_sim"] is not None else None,
                "tier_boost": round(float(r["tier_boost"]), 2),
                "recency_factor": round(float(r["recency_factor"]), 4),
                "open_loop_decay": round(float(r["open_loop_decay"]), 4),
                "score": round(float(r["score"]), 4) if r["score"] is not None else 0.0,
                "confidence": r["confidence"],
                "visibility": r["visibility"],
                "age": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
            }
            if meta.get("cache_state"):
                entry["cache_state"] = meta["cache_state"]
            if meta.get("memory_tier") is not None:
                entry["memory_tier"] = meta["memory_tier"]
            # Carry visibility_scope and the full metadata dict through so
            # downstream filters (visibility check, prompt builder, CEIGAS
            # hooks) can read what the row carries without another query.
            if meta.get("visibility_scope"):
                entry["visibility_scope"] = meta["visibility_scope"]
            entry["metadata"] = meta
            results.append(entry)

        # Visibility / scope filter against the declared consumer.
        # A memory whose visibility_scope.deny matches the consumer is dropped;
        # if visibility_scope.allow is set and doesn't match, also dropped.
        # Memories without visibility_scope pass through (allow-by-default).
        if consumer is not None:
            try:
                from mastercode.visibility import filter_rows
                results = filter_rows(results, consumer)
            except Exception as _vis_err:
                # FAIL CLOSED (C27): a consumer was explicitly declared, so the
                # caller asked for visibility enforcement. If the filter itself
                # cannot run, we must NOT surface unfiltered rows to that
                # consumer — drop the scoped rows (any row carrying a
                # visibility_scope) and keep only unscoped rows, which are
                # allow-by-default regardless. Previously this swallowed the
                # error and returned EVERY row unfiltered (fail open).
                log.warning("Visibility filter failed — failing closed, dropping scoped rows: %s", _vis_err)
                _safe = []
                for _r in results:
                    _meta = _r.get("metadata") or {}
                    _scope = _meta.get("visibility_scope") if isinstance(_meta, dict) else None
                    if not _scope:
                        _safe.append(_r)
                results = _safe

        return results

    async def retrieve_by_keywords(
        self,
        entity_id: int,
        keywords: list[str],
        limit: int = 10,
        min_importance: float = 0.1,
        visibility: Optional[str] = None,
        consumer: Optional[object] = None,
    ) -> list[dict]:
        """
        Retrieve memories by keyword text search (ILIKE fallback).

        Used when vector similarity fails to surface relevant memories.
        OR-combines up to 5 keywords against memory content.

        consumer (C10): when set, the returned rows are visibility-filtered
        against this consumer (e.g. "model:<id>") so a denied memory cannot
        leak via the keyword path. Rows carry metadata.visibility_scope for the
        filter; rows without a scope pass through (allow-by-default).
        """
        if not keywords:
            return []
        keywords = keywords[:5]

        # Build OR conditions for ILIKE
        conditions = []
        params = [entity_id, min_importance]
        for i, kw in enumerate(keywords):
            param_idx = len(params) + 1
            conditions.append(f"content ILIKE '%' || ${param_idx} || '%'")
            params.append(kw)

        where_clause = " OR ".join(conditions)

        vis_filter = ""
        if visibility:
            param_idx = len(params) + 1
            vis_filter = f" AND visibility = ${param_idx}"
            params.append(visibility)

        params.append(limit)
        limit_idx = len(params)

        rows = await self.pool.fetch(f"""
            SELECT
                id, content, memory_type, importance, confidence,
                access_count, created_at, visibility, metadata
            FROM entity_memory
            WHERE entity_id = $1
              AND importance >= $2
              AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
              AND ({where_clause})
              {vis_filter}
            ORDER BY importance DESC
            LIMIT ${limit_idx}
        """, *params)

        # Update access counts
        if rows:
            ids = [r["id"] for r in rows]
            await self.pool.execute("""
                UPDATE entity_memory
                SET access_count = access_count + 1, last_accessed_at = NOW()
                WHERE id = ANY($1)
            """, ids)

        out = []
        for r in rows:
            meta = r["metadata"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            meta = meta or {}
            out.append({
                "content": r["content"],
                "type": r["memory_type"],
                "importance": r["importance"],
                "relevance": 0.5,  # Fixed — no vector similarity for text search
                "confidence": r["confidence"],
                "visibility": r["visibility"],
                "source": "keyword",
                "metadata": meta,  # carry scope so filter_rows can enforce it
                "age": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
            })

        # C10/C27: visibility filter on the keyword path. Fail-closed per row.
        if consumer is not None:
            try:
                from mastercode.visibility import filter_rows
                out = filter_rows(out, consumer)
            except Exception as _vis_err:
                log.warning("Keyword visibility filter failed — failing closed, dropping scoped rows: %s", _vis_err)
                out = [m for m in out if not (m.get("metadata") or {}).get("visibility_scope")]

        return out

    async def get_memory_stats(self, entity_id: int) -> dict:
        """Get memory statistics for an entity."""
        rows = await self.pool.fetch("""
            SELECT memory_type, COUNT(*) as count,
                   AVG(importance) as avg_importance,
                   MAX(created_at) as latest
            FROM entity_memory
            WHERE entity_id = $1
            GROUP BY memory_type
            ORDER BY count DESC
        """, entity_id)

        total = sum(r["count"] for r in rows)
        return {
            "entity_id": entity_id,
            "total_memories": total,
            "by_type": {
                r["memory_type"]: {
                    "count": r["count"],
                    "avg_importance": round(float(r["avg_importance"]), 2),
                    "latest": r["latest"].isoformat() if r["latest"] else None,
                }
                for r in rows
            },
        }

    async def build_memory_context(
        self,
        entity_id: int,
        personality_weights: dict,
        max_tokens: int = 1500,
        conversation_context: str = "",
        nt_levels: dict = None,
        consumer: Optional[object] = None,
        query: str = "",
        context: Optional[dict] = None,
        source_context: Optional[dict] = None,
        prefire_sink: Optional[dict] = None,
    ) -> str:
        """
        Build a memory context block for the system prompt.

        prefire_sink: the caller's own dict; when given, the
        pre-fired items' metadata (id, type, scope) is left in it under
        "items". The caller owns it per turn — the per-entity stash above is
        shared across concurrent turns and cannot carry anything the audit
        acts on.

        source_context: the turn's scope descriptor — a project,
        a Circles thread, a peer or a session, one {kind, ref}. It scopes the
        standing list to this thread's set, selects the thread audit's
        findings, and labels each pre-fired item's scope in prefire_sink.
        Own-words rows never ride either section (PROMPT_MEMORY_TYPES).
        None (no scope known) is the call before, unchanged.

        context (turn types; runtime/turn_types.context_filter):
        what a TYPED turn may read. memory="none" renders no memory at all
        (the teaching section included — it is memory); memory="projects"
        keeps the per-turn pre-fire, filtered to the listed projects, reads
        the taught rules of those projects by the same test
        (typed_rule_clause), and drops the standing importance/recency list,
        which is cross-project by construction. The documents flag does not
        apply here: the pre-fire never reads the documents store.
        None is every turn before turn types existed, unchanged.

        v1.1: Uses timeline composite retrieval (semantic + temporal + emotional)
        when available, falls back to personality-only retrieval otherwise.

        consumer (C7): the prompt's declared consumer (e.g. "model:<id>"). When
        set, the fallback personality retrieval below runs visibility.filter_rows
        so a memory denied for this model is dropped from the live prompt. The
        live caller is runtime/prompt_schema._b_memory.

        query (per-turn pre-fire): the current user message. When
        non-empty, a hybrid recall of the memory store only
        (memory_retrieval.federated_recall, scope="memory", preferences and
        relationship notes) runs against it and the top hits render as a
        "### Relevant Right Now" section, ahead of the standing
        importance/tier/recency list, which no longer has to guess what this
        turn is about. Borrowed from TencentDB-Agent-Memory's auto-recall:
        recall per turn, not just on tool call. The memory block is PER_TURN
        (after the CACHE_BREAK), so per-turn content here does not disturb the
        cache-stable prefix. Hits already surfaced are deduped out of the
        grouped sections below.
        """
        # Conversation-depth: reset the per-entity pre-fire stash
        # up front so every exit path (timeline early-return included) leaves
        # an accurate record — prompt_schema reads it for the prompt_log
        # "memory_prefire" entry.
        _last_prefire_items[entity_id] = []

        _ctx_memory = str((context or {}).get("memory") or "all") if context else "all"
        if _ctx_memory == "none":
            # A type that reads no memory gets no memory block — not a
            # narrower one, none, teaching rows included.
            return ""

        # The standing list. Thread the consumer so visibility.filter_rows
        # runs on this live prompt path (C7).
        if _ctx_memory == "projects":
            # The standing list is importance/tier/recency over EVERYTHING;
            # a project-scoped type reads only what the pre-fire finds inside
            # its projects.
            memories = []
        else:
            # One read per type, so neither crowds the other out of the
            # section each renders (at most 5 apiece, below). A preference is
            # the user's rule whatever importance it carries — the extractor
            # scored it anywhere from 0 to 2 when it wrote it
            # (batch_consolidator) and the consolidator's decay only lowered
            # it (87 of one Host's 126 live preferences sat under the old 0.5
            # gate) — so preferences have no floor and rank alone
            # decides which ride. Relationship notes keep theirs.
            memories = []
            for _mtype in sorted(PROMPT_MEMORY_TYPES):
                # The scope's whole live set of the type (bounded), so the
                # section can show the NEWEST five and say how many it left
                # out (below), never drop the rest unannounced.
                memories += await self.retrieve(
                    entity_id, personality_weights, limit=STANDING_CANDIDATES,
                    memory_types=[_mtype], consumer=consumer,
                    source_context=source_context, count_access=False,
                    min_importance=0.0 if _mtype == "preference" else 0.5,
                    exclude_rules=True,
                )

        parts = ["## What You Remember"]
        char_budget = max_tokens * 4  # rough token-to-char ratio
        chars_used = len(parts[0])

        # --- The thread audit's open findings ---
        # What the background audit found wrong in THIS thread, first, so the
        # next reply can correct itself (mastercode.thread_audit). Empty when
        # nothing is open or no scope is known.
        if isinstance(source_context, dict) and source_context.get("kind"):
            try:
                from mastercode import thread_audit
                _audit_block = thread_audit.render_findings_block(
                    await thread_audit.open_findings(
                        self.pool, entity_id, source_context, thread_audit.SHOWN_MAX))
                if _audit_block:
                    parts.append(_audit_block)
                    chars_used += len(_audit_block)
            except Exception as _af_err:  # noqa: BLE001 — never a failed turn
                log.debug("thread audit block skipped: %s", _af_err)

        # --- Teaching memories (highest priority) ---
        # Always load teaching memories — use context for keyword filtering, or load all if no context
        ctx = conversation_context or ""
        # A project-scoped type reads the rules of its projects by the one
        # test its pre-fire applies to a plain preference (typed_rule_clause).
        _typed_projects = ([int(p) for p in ((context or {}).get("projects") or [])]
                           if _ctx_memory == "projects" else None)
        if ctx:
            teaching_mems = await self.retrieve_teaching(entity_id, ctx, limit=5,
                                                         source_context=source_context,
                                                         project_ids=_typed_projects)
        else:
            # No context yet (session boot) — load most recent teachings.
            # Live rows only, like every other skill_memory reader
            # (teaching_deduplicator, signal_retrieval, constitution/context):
            # this branch runs on EVERY turn (prompt_schema passes no
            # conversation_context), and without the filter an archived rule
            # stayed in the prompt as "Things You Were Taught" — a stale
            # "dispatch currently fails" row did, after a cleanup archived it.
            #
            # A rule is a preference marked rule_kind now (RULE_KINDS); both
            # shapes are read. Where it rides: a rule nobody has
            # decided about rides everywhere, as every rule always has; one
            # decided global rides everywhere; one decided about a project,
            # an app or a thread rides only in turns there (memory_scope;
            # rule_place_clause, which every taught reader applies).
            turn_place_sql, turn_place_args = typed_rule_clause(source_context, _typed_projects, 2)
            rows = await self.pool.fetch(f"""
                SELECT id, content, memory_type, importance, confidence,
                       access_count, created_at, visibility, metadata, slot_topic,
                       count(*) OVER () AS live_rules
                FROM entity_memory
                WHERE entity_id = $1 AND {rule_row_sql()}
                  AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND {turn_place_sql}
                ORDER BY importance DESC, created_at DESC, id DESC
                LIMIT 5
            """, entity_id, *turn_place_args)
            teaching_mems = [
                {"content": r["content"], "type": "skill_memory", "importance": r["importance"],
                 "relevance": 1.0, "confidence": r["confidence"], "visibility": r["visibility"],
                 "source": "skill_memory", "age": r["created_at"], "id": r["id"],
                 "topic": r["slot_topic"]}
                for r in rows
            ]
            _taught_live = int(rows[0]["live_rules"]) if rows else 0
        # Built here, before the pre-fire, and placed after it: its budget
        # comes first (highest priority), and the words it carries are known
        # before any preference section renders.
        taught_section, taught_keys = "", set()
        # Every row the block shows, by id: the turn records which memories
        # rode (prefire_sink["rode"]), so the owner can see it.
        _rode: list[dict] = []
        if teaching_mems:
            section = "\n### Things You Were Taught"
            section_len = len(section)
            _shown_taught = 0
            for item in teaching_mems[:5]:
                line = format_memory_line(item["content"], item.get("age"), "skill_memory",
                                          topic=item.get("topic"), row_id=item.get("id"))
                if section_len + len(line) + chars_used > char_budget:
                    break
                section += line
                section_len += len(line)
                _shown_taught += 1
                taught_keys.add(same_words_key(item.get("content")))
                if item.get("id") is not None:
                    _rode.append({"id": item["id"], "type": "rule", "section": "taught"})
            if not ctx:
                _left = omitted_line(max(_taught_live - _shown_taught, 0), "rules you were taught",
                                     "memory_scope kind='list' lists every one")
                section += _left
                section_len += len(_left)
            if section_len > len("\n### Things You Were Taught"):
                taught_section = section
                chars_used += section_len

        # --- Per-turn pre-fire recall (message-semantic) ---
        # The standing list above is importance/tier/recency-ordered — blind
        # to what THIS turn asks. With the live user message in hand, run THE
        # recall engine against it and lead the block with those hits.
        # It searches the memory store only: the user_context
        # document store (uploads and the research bucket the janitor keeps)
        # is no longer pushed here; `recall` finds it on request.
        # Degrades honestly: embed lane down ⇒ the lexical arm still answers;
        # no arm at all ⇒ no section, never an error. Visibility is enforced
        # with the same fail-closed posture as the keyword path (C10/C27).
        #
        # Wall-clock guard: a *hanging* embed
        # backend (the lane is a remote GPU over the bridge, 60s timeout ×
        # retries) must never stall a chat turn, so the whole federated
        # recall gets a hard 4s budget; on timeout the standing list
        # renders alone. 4s, not less: a cold circuit-breaker probe costs
        # up to ~3 attempts × 1s sleeps before the lexical-only path runs
        # (a gate failed at 1.5s), and a genuinely hung lane
        # is still bounded at 4s instead of a minute.
        # One copy of the same words per turn. A rule this turn's taught block
        # carries is the words: a plain preference in exactly those words
        # (kept live because the rule does not cover every place it is read,
        # _retire_into_rule) is skipped by every preference section below,
        # by the dedup they already share (prefired_keys, its normalisation).
        prefired_keys: set[str] = set(taught_keys)
        if query:
            prefired: list[dict] = []
            try:
                from mastercode.memory_retrieval import (
                    federated_recall, RELEVANCE_FLOOR,
                )
                # Preferences and relationship notes only: cited facts and
                # the documents store are found
                # with `recall`, which reaches every store.
                fed = await asyncio.wait_for(
                    federated_recall(self.pool, entity_id, query,
                                     scope="memory", limit=6, context=context,
                                     memory_types=sorted(PROMPT_MEMORY_TYPES),
                                     turn_scope=(source_context
                                                 if isinstance(source_context, dict)
                                                 else {}),
                                     scope_everywhere_types=list(SCOPE_EVERYWHERE_TYPES),
                                     # A rule rides in "Things You Were Taught"
                                     # below, chosen by weight: relevance does
                                     # not pick rules on this model
                                     # (measured),
                                     # and a second copy here would be the
                                     # same words twice. Left out IN the query,
                                     # so matching rules never use up its
                                     # limit before a preference is reached.
                                     exclude_rules=True,
                                     # The words of the rules the taught block
                                     # carries are dropped before the limit too,
                                     # so a copy never takes a slot.
                                     skip_words=set(taught_keys),
                                     # Two of the message's words, or the
                                     # row's topic, are a match: every word
                                     # of the message almost never is
                                     # (memory_retrieval._lexical_arm).
                                     any_words=True),
                    timeout=4.0)
                for h in (fed.get("memory") or {}).get("hits", []):
                    matched = h.get("matched") or []
                    if "lexical" not in matched and "graph" not in matched:
                        rel = h.get("relevance")
                        if rel is None or rel < RELEVANCE_FLOOR:
                            continue
                    if h.get("memory_type") not in PROMPT_MEMORY_TYPES:
                        continue
                    meta = h.get("metadata")
                    if isinstance(meta, str):
                        try:
                            h["metadata"] = json.loads(meta)
                        except Exception:
                            h["metadata"] = {}
                    prefired.append(h)
            except Exception as e:
                log.info("pre-fire recall skipped (standing list unaffected): %s", e)
            if prefired and consumer is not None:
                try:
                    from mastercode.visibility import filter_rows as _pf_filter
                    prefired = _pf_filter(prefired, consumer)
                except Exception as _vf:
                    log.warning("Pre-fire visibility filter failed — failing closed, dropping scoped rows: %s", _vf)
                    prefired = [
                        h for h in prefired
                        if not (h.get("metadata") or {}).get("visibility_scope")
                    ]
            if prefired:
                section = "\n### Relevant Right Now"
                section_len = len(section)
                _fired: list[str] = []
                _fired_rows: list[dict] = []
                _fired_meta: list[dict] = []
                # Own-words rows (turn/summary) never reach this loop: they are
                # not PROMPT_MEMORY_TYPES, so the thread-scope and newest-few
                # rules they needed here went with them.
                for h in prefired:
                    if len(_fired) >= 6:
                        break
                    content = (h.get("content") or "").strip()
                    if not content:
                        continue
                    if len(content) > 500:
                        content = content[:497] + "..."
                    line = format_memory_line(content, h.get("created_at"), h.get("memory_type"),
                                              topic=h.get("slot_topic"), row_id=h.get("id"))
                    if section_len + len(line) + chars_used > char_budget:
                        break
                    # Register the dedup key only once the line is actually
                    # in — a hit priced out of the budget must still render
                    # in its standing section, not vanish from the prompt.
                    prefired_keys.add(same_words_key(content))
                    section += line
                    section_len += len(line)
                    _fired.append(content)
                    _fired_rows.append(h)
                    _fired_meta.append({"id": h.get("id"), "type": h.get("memory_type"),
                                        "scope": _hit_scope(h),
                                        "pattern": is_person_pattern(h)})
                    if h.get("id") is not None:
                        _rode.append({"id": h.get("id"), "type": h.get("memory_type"),
                                      "section": "relevant"})
                if section_len > len("\n### Relevant Right Now"):
                    parts.append(section)
                    chars_used += section_len
                    # A row written in a turn that read untrusted content
                    # marks the turn it is shown to (runtime/turn_taint).
                    from mastercode.runtime import turn_taint as _tt_pf
                    _tt_pf.mark_rows(_fired_rows, "a memory recalled into this turn")
                    # Conversation-depth: record what actually prefired so the
                    # prompt_log blocks payload can carry it (View Context
                    # panel's last_prefired).
                    _last_prefire_items[entity_id] = _fired
                    if isinstance(prefire_sink, dict):
                        prefire_sink["items"] = _fired_meta
                    # Nothing is written back to a pre-fired row. Bumping
                    # access_count or refreshing last_accessed_at both feed
                    # _strengthen_accessed (5+ accesses with a fresh stamp →
                    # +importance every cycle), and importance is part of the
                    # recall score: a shown row would keep itself shown — the
                    # "recalled for weeks" loop this exists to break.
                    # prompt_log already records what fired.

        # Constitutional memories are injected separately in session assembly —
        # do NOT duplicate them here (was causing 3x constitutional injection).

        # "Things You Were Taught", built above before the pre-fire, takes
        # its place here.
        if taught_section:
            parts.append(taught_section)

        if not memories and len(parts) == 1:
            if isinstance(prefire_sink, dict):
                prefire_sink["rode"] = _rode
            return "" if not teaching_mems else "\n".join(parts)

        # Group by type for clean formatting
        by_type = {}
        for mem in memories:
            by_type.setdefault(mem["type"], []).append(mem)

        type_labels = {
            "preference": "Their Preferences",
            "relationship": "Relationship Notes",
        }

        for mem_type in ["preference", "relationship"]:
            items = by_type.get(mem_type, [])
            if not items:
                continue

            label = type_labels.get(mem_type, mem_type.title())
            section = f"\n### {label}"
            section_len = len(section)

            # Drop anything already led in "Relevant Right Now" BEFORE the
            # 5-per-type slice — a deduped item must not consume a standing
            # slot (don't pay for the same memory twice, don't starve the
            # section either).
            items = [
                it for it in items
                if same_words_key(it.get("content")) not in prefired_keys
            ]
            # Newest first within the type, ties to the newest row: of two
            # entries that disagree, the later statement leads.
            items.sort(key=lambda it: (str(it.get("age") or ""), int(it.get("id") or 0)),
                       reverse=True)
            shown = 0
            for item in items[:STANDING_SHOWN]:  # the newest five per type
                line = format_memory_line(item["content"], item.get("age"), item.get("type"),
                                          topic=item.get("topic"), row_id=item.get("id"))
                if section_len + len(line) + chars_used > char_budget:
                    break
                section += line
                section_len += len(line)
                shown += 1
                if item.get("id") is not None:
                    _rode.append({"id": item["id"], "type": mem_type, "section": "standing"})
            left = omitted_line(
                len(items) - shown,
                "preferences" if mem_type == "preference" else "relationship notes",
                "recall finds them by topic or words" + (
                    ", and memory_scope kind='list' lists every preference"
                    if mem_type == "preference" else ""),
                at_least=len(by_type.get(mem_type, [])) >= STANDING_CANDIDATES)
            if left:
                section += left
                section_len += len(left)

            if section_len > len(f"\n### {label}"):
                parts.append(section)
                chars_used += section_len

            if chars_used > char_budget:
                break

        if isinstance(prefire_sink, dict):
            prefire_sink["rode"] = _rode
        if len(parts) == 1:
            return ""
        # The topics already in use (memory_slots), so a close_turn
        # preference reuses one instead of coining a near twin: the newest
        # entry in a topic replaces the older there.
        # A project-typed turn reads its projects' rows only, so it is shown
        # no topic of anywhere else.
        if _ctx_memory != "projects":
            _topics = await topics_line(self.pool, entity_id, source_context)
            if _topics:
                parts.append(_topics)
        return "\n".join(parts)

    async def _build_teaching_block(self, entity_id: int, conversation_context: str,
                                    max_chars: int = 1600,
                                    source_context: Optional[dict] = None) -> str:
        """v1.1: Extract teaching memories as a standalone block. A rule
        decided about a place rides only in a turn there (rule_place_clause);
        with no `source_context`, only the rules that ride everywhere."""
        if conversation_context:
            teaching_mems = await self.retrieve_teaching(entity_id, conversation_context, limit=5,
                                                         source_context=source_context)
        else:
            block_place_sql, block_place_args = rule_place_clause(source_context, 2)
            rows = await self.pool.fetch(f"""
                SELECT content, importance, confidence
                FROM entity_memory
                WHERE entity_id = $1 AND {rule_row_sql()}
                  AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND {block_place_sql}
                ORDER BY importance DESC, created_at DESC
                LIMIT 5
            """, entity_id, *block_place_args)
            teaching_mems = [{"content": r["content"]} for r in rows]

        if not teaching_mems:
            return ""

        lines = ["### Things You Were Taught"]
        used = len(lines[0])
        for t in teaching_mems[:5]:
            line = f"- {t['content']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines) if len(lines) > 1 else ""

    async def forget(self, entity_id: int, memory_id: int) -> bool:
        """Delete a specific memory."""
        result = await self.pool.execute(
            "DELETE FROM entity_memory WHERE id = $1 AND entity_id = $2",
            memory_id, entity_id,
        )
        return "DELETE 1" in result

    async def decay_old_memories(
        self,
        entity_id: int,
        decay_factor: float = 0.95,
        min_importance: float = 0.1,
    ):
        """
        Apply importance decay to old memories.
        Frequently accessed memories resist decay.
        """
        await self.pool.execute(f"""
            UPDATE entity_memory
            SET importance = GREATEST($3, importance * $2),
                updated_at = NOW()
            WHERE entity_id = $1
              AND last_accessed_at < NOW() - INTERVAL '7 days'
              AND access_count < 3
              AND memory_type != 'skill_memory'
              AND {not_rule_sql()}
        """, entity_id, decay_factor, min_importance)

    # ----------------------------------------------------------------
    # Obsidian-style memory emit formats (from kepano/obsidian-skills
    # patterns; the Obsidian app itself is not a
    # dependency — these are pure data emit methods).
    # ----------------------------------------------------------------

    async def emit_moc(
        self,
        entity_id: int,
        topic: str,
        limit: int = 20,
        min_similarity: float = 0.5,
    ) -> str:
        """Emit a Map of Content note for a topic.

        Obsidian convention: a MOC is a curated index note linking
        related notes on a topic. For agent memory the curation is
        automatic — embed the topic, find memories with cosine
        similarity >= min_similarity, render as a markdown list with
        wikilinks (`[[mem-<id>]]`).

        Returns markdown suitable for ContextStore at
        `mocs/<topic-slug>.md` or for direct prompt injection. Empty
        string if no relevant memories.
        """
        emb = await _get_memory_embedding(topic)
        if not emb:
            return ""
        vec_str = "[" + ",".join(str(v) for v in emb) + "]"
        # Multi-dim: route to the column matching the entity's active model's
        # dim. Whitelisted name (injection-safe).
        content_col, dim = await active_embedding_column(self.pool, entity_id)
        cast = embedding_cast_type(dim)
        try:
            rows = await self.pool.fetch(f"""
                SELECT id, content, memory_type, importance,
                       1 - ({content_col} <=> $1::{cast}) AS similarity
                FROM entity_memory
                WHERE entity_id = $2
                  AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND {content_col} IS NOT NULL
                ORDER BY {content_col} <=> $1::{cast}
                LIMIT $3
            """, vec_str, entity_id, limit)
        except Exception as e:
            log.warning("emit_moc query failed for entity %d: %s", entity_id, e)
            return ""

        relevant = [r for r in rows if r["similarity"] >= min_similarity]
        if not relevant:
            return ""

        lines = [
            f"# MOC: {topic}",
            "",
            f"Auto-generated Map of Content. Memories ordered by similarity to **{topic}** (cosine ≥ {min_similarity}).",
            "",
        ]
        for r in relevant:
            snippet = (r["content"] or "")[:180].replace("\n", " ")
            lines.append(
                f"- [[mem-{r['id']}]] *({r['memory_type']}, sim={r['similarity']:.2f}, "
                f"imp={r['importance']:.1f})* — {snippet}"
            )
        return "\n".join(lines)

    async def emit_json_canvas(
        self,
        entity_id: int,
        topic: str | None = None,
        limit: int = 50,
        link_threshold: float = 0.75,
    ) -> dict:
        """Emit memory graph as JSON Canvas (https://jsoncanvas.org/).

        Returns a `{"nodes": [...], "edges": [...]}` dict conforming to
        the JSON Canvas spec. Each memory is a text node; edges link
        memory pairs whose embedding cosine similarity is at least
        `link_threshold`.

        If `topic` is given, the selection is restricted to memories
        nearest the topic embedding (sub-graph centered on the topic).
        Otherwise the top-`limit` memories by importance + recency are
        used.

        Edges are computed server-side via pgvector cosine — no client
        memory cost beyond the result set.
        """
        # Multi-dim: route to the column matching the entity's active model's
        # dim. Whitelisted name (injection-safe).
        content_col, dim = await active_embedding_column(self.pool, entity_id)
        cast = embedding_cast_type(dim)
        if topic:
            emb = await _get_memory_embedding(topic)
            if not emb:
                return {"nodes": [], "edges": []}
            vec_str = "[" + ",".join(str(v) for v in emb) + "]"
            row_sql = f"""
                SELECT id, content, memory_type
                FROM entity_memory
                WHERE entity_id = $1
                  AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND {content_col} IS NOT NULL
                ORDER BY {content_col} <=> $2::{cast}
                LIMIT $3
            """
            row_args = (entity_id, vec_str, limit)
        else:
            row_sql = f"""
                SELECT id, content, memory_type
                FROM entity_memory
                WHERE entity_id = $1
                  AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND {content_col} IS NOT NULL
                ORDER BY importance DESC, created_at DESC
                LIMIT $2
            """
            row_args = (entity_id, limit)

        try:
            rows = await self.pool.fetch(row_sql, *row_args)
        except Exception as e:
            log.warning("emit_json_canvas selection failed for entity %d: %s", entity_id, e)
            return {"nodes": [], "edges": []}

        if not rows:
            return {"nodes": [], "edges": []}

        ids = [r["id"] for r in rows]

        # Edges: pairwise cosine similarity over the selected set,
        # computed in PG to avoid pulling 1024-dim vectors client-side.
        try:
            edge_rows = await self.pool.fetch(f"""
                SELECT a.id AS from_id, b.id AS to_id,
                       1 - (a.{content_col} <=> b.{content_col}) AS sim
                FROM entity_memory a
                JOIN entity_memory b ON a.id < b.id
                WHERE a.entity_id = $1
                  AND b.entity_id = $1
                  AND a.id = ANY($2)
                  AND b.id = ANY($2)
                  AND a.{content_col} IS NOT NULL
                  AND b.{content_col} IS NOT NULL
                  AND 1 - (a.{content_col} <=> b.{content_col}) >= $3
            """, entity_id, ids, link_threshold)
        except Exception as e:
            log.warning("emit_json_canvas edge query failed for entity %d: %s", entity_id, e)
            edge_rows = []

        # Lay nodes out in a simple grid.
        import math
        cols = max(1, int(math.ceil(math.sqrt(len(rows)))))
        width, height = 240, 110
        gap_x, gap_y = 60, 60

        nodes = []
        for i, r in enumerate(rows):
            col = i % cols
            row = i // cols
            content = r["content"] or ""
            snippet = (content[:80] + "…") if len(content) > 80 else content
            nodes.append({
                "id": f"mem-{r['id']}",
                "type": "text",
                "x": col * (width + gap_x),
                "y": row * (height + gap_y),
                "width": width,
                "height": height,
                "text": f"**{r['memory_type']}** · #{r['id']}\n{snippet}",
            })

        edges = []
        for er in edge_rows:
            sim = float(er["sim"])
            edges.append({
                "id": f"edge-{er['from_id']}-{er['to_id']}",
                "fromNode": f"mem-{er['from_id']}",
                "toNode": f"mem-{er['to_id']}",
                "label": f"{sim:.2f}",
            })

        return {"nodes": nodes, "edges": edges}
