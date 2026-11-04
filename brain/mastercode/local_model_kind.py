"""Which of the models a person's machines serve are embedding models.

A model server lists every model it has: ollama every tag it ever pulled,
llama.cpp's router every model it was given, embedders beside the chat models.
An embedding model answers no chat, so it must never be offered as one: not in
the Orchestrator's list of models on your machines, not as the model chat runs
on when nobody chose one, not as a route the person saves.

The answer comes from asking the server (as the embedding assignment does:
a live probe decides, the advertised list does not):

  - ollama states it: POST /api/show answers each model's `capabilities`
    ("completion" for a model that chats, "embedding" for one that embeds).
    It reads the model's file only; nothing is loaded.
  - an OpenAI-compatible server (llama.cpp, vLLM, LM Studio) states nothing,
    so a model is tested with one real embed (memory_engine.
    measure_embedding_dim, the path the Host's embeds take). Such a server
    embeds only with a model it serves for embeddings (llama.cpp refuses
    "start it with --embeddings"; vLLM serves a model for generation or for
    pooling), so a vector back is an embedding model. Only models the server
    lists whose id names an embedding family, or that the memory store knows
    as one, are tested: testing every model would make a router load each
    chat model into memory to find out.

When the server cannot be asked (offline, an ollama too old to say), the name
decides, as it did before: embed_backends.embedding_named or a model the memory
store knows as an embedder.

Answers are kept per server and model for an hour (a model's kind does not
change while it is listed), a failed ask for five minutes.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("privatae.local_model_kind")

ANSWER_TTL_S = 3600.0
UNKNOWN_TTL_S = 300.0
# How long a list waits for asks still running; the rest finish in the
# background and the next read has them (the Orchestrator reads the list again
# every 5-30 s while it is open).
LIST_WAIT_S = 4.0

# (endpoint, model) -> ("embedding" | "chat" | None, expires_at)
_VERDICTS: dict[tuple[str, str], tuple[str | None, float]] = {}
_INFLIGHT: dict[tuple[str, str], asyncio.Task] = {}
# A test sets this to a stand-in for _ask; None is the real one.
ASK_OVERRIDE = None


def _asking() -> bool:
    """Servers are asked only inside a running Host (its brain pool is up), as
    inference_config.anthropic_model_efforts_warm reaches the network: a test
    or a script importing this never dials anything unless it stands in an
    asker of its own."""
    if ASK_OVERRIDE is not None:
        return True
    try:
        from server import brain_service
        return brain_service.get_pool() is not None
    except Exception:  # noqa: BLE001
        return False


def _key(m: dict) -> tuple[str, str]:
    return (str(m.get("endpoint") or ""), str(m.get("id") or "").strip())


def named_embedder(model_id) -> bool:
    """The name rule: the id names an embedding family, or the memory store
    knows the model as an embedder."""
    try:
        from mastercode.embed_backends import embedding_named, known_embed_models
        return bool(embedding_named(model_id) or known_embed_models([model_id]))
    except Exception:  # noqa: BLE001
        return False


def _peer_forward(m: dict) -> str | None:
    """The Hub forward a request to this model's server rides: a bastion at a
    mesh address, never a serve-lane origin or a server the owner named by
    address (owner_endpoints labels those by their host:port)."""
    ep = str(m.get("endpoint") or "")
    try:
        from mastercode import owner_endpoints
        if (m.get("bastion") or "") == owner_endpoints.label(ep):
            return None
    except Exception:  # noqa: BLE001
        pass
    try:
        from mastercode.embed_backends import peer_forward_for
        return peer_forward_for(ep, m.get("bastion"))
    except Exception:  # noqa: BLE001
        return None


async def _post_json(url: str, payload: dict, peer: str | None,
                     timeout_s: float = 10.0) -> tuple[int, dict]:
    if peer:
        from mastercode.memory_engine import _post_json_via_peer_forward
        return await _post_json_via_peer_forward(peer, url, payload, timeout_s=timeout_s,
                                                 connect_timeout_s=min(5.0, timeout_s))
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = {}
            return resp.status, data if isinstance(data, dict) else {}


def verdict_from_capabilities(caps) -> str | None:
    """ollama's /api/show `capabilities` -> 'chat' | 'embedding' | None."""
    if not isinstance(caps, list):
        return None
    c = {str(x).strip().lower() for x in caps}
    if "completion" in c:
        return "chat"
    if "embedding" in c:
        return "embedding"
    return None


async def _ask(m: dict) -> str | None:
    """Ask the server what `m` is; None when it cannot say."""
    ep = str(m.get("endpoint") or "").rstrip("/")
    mid = str(m.get("id") or "").strip()
    if not ep or not mid:
        return None
    peer = _peer_forward(m)
    if m.get("kind") == "ollama":
        try:
            status, data = await _post_json(ep + "/api/show", {"model": mid}, peer)
        except Exception as e:  # noqa: BLE001
            log.info("local model kind: %s on %s not asked (%s)", mid, ep, type(e).__name__)
            return None
        if status != 200:
            return None
        return verdict_from_capabilities(data.get("capabilities"))
    if not named_embedder(mid):
        return "chat"
    try:
        from mastercode.memory_engine import measure_embedding_dim
        dim, _why = await measure_embedding_dim(ep, mid, peer_forward=peer)
    except Exception as e:  # noqa: BLE001
        log.info("local model kind: test embed of %s on %s failed (%s)", mid, ep, type(e).__name__)
        return None
    return "embedding" if dim else None


async def _ask_and_keep(m: dict) -> str | None:
    k = _key(m)
    try:
        v = await (ASK_OVERRIDE or _ask)(m)
    except Exception:  # noqa: BLE001
        v = None
    _VERDICTS[k] = (v, time.monotonic() + (ANSWER_TTL_S if v else UNKNOWN_TTL_S))
    return v


def cached_verdict(m: dict) -> str | None:
    got = _VERDICTS.get(_key(m))
    if got and got[1] > time.monotonic():
        return got[0]
    return None


def is_embedding(m: dict, verdict: str | None) -> bool:
    """The answer for one model: the server's when it gave one, else the name."""
    if verdict == "embedding":
        return True
    if verdict == "chat":
        return False
    return named_embedder(m.get("id"))


async def classify(models: list[dict], *, wait_s: float = LIST_WAIT_S) -> dict[tuple[str, str], bool]:
    """{(endpoint, model): is an embedding model} for every row of
    model_backends.list_local_models. Asks still running after `wait_s` go on
    in the background; their rows read by the name meanwhile."""
    now = time.monotonic()
    pending: list[asyncio.Task] = []
    for m in (models or []) if _asking() else []:
        k = _key(m)
        got = _VERDICTS.get(k)
        if got and got[1] > now:
            continue
        t = _INFLIGHT.get(k)
        if t is None or t.done():
            t = asyncio.ensure_future(_ask_and_keep(dict(m)))
            _INFLIGHT[k] = t
            t.add_done_callback(lambda _t, _k=k: _INFLIGHT.pop(_k, None))
        pending.append(t)
    if pending and wait_s > 0:
        await asyncio.wait([asyncio.shield(t) for t in pending], timeout=wait_s)
    return {_key(m): is_embedding(m, cached_verdict(m)) for m in models or []}


async def chat_models(models: list[dict], *, wait_s: float = LIST_WAIT_S) -> list[dict]:
    """The rows that are not embedding models, in their order."""
    kinds = await classify(models, wait_s=wait_s)
    return [m for m in models or [] if not kinds.get(_key(m), False)]


def _reset_for_tests() -> None:
    _VERDICTS.clear()
    _INFLIGHT.clear()
