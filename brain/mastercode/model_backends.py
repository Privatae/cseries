"""
Model backends — the host-side LOCAL MODEL TABLE.

The mirror of embed_backends for INFERENCE models: a machine the user runs
(bastion) serves models over the host's private mesh; the host records what
each bastion reports (network_bastion_heartbeat.models, written by
topology.upsert_heartbeat) and reads it here. THE key behavior this enables:
a model selection DEFAULTS TO THE LOCAL LANE when the picked model id matches
a model served on the host's own network — the user picked the MODEL; if their
own hardware serves it, it runs there (free, private, unmetered).

This is the HOST side only (read + match). The bastion side — the gateway
advertising its served models in the heartbeat — landed in
agents.go:buildHeartbeatBody; a box with no model server reports [] and every
helper here degrades to "no local models" (never raises).

TWO SERVERS, TWO PORTS (2026-08-03). A bastion may run ollama and an
OpenAI-compatible server (llama.cpp llama-server, vLLM) side by side, on
different ports. They are advertised in SEPARATE heartbeat columns —
`models` (ollama, reached on its own port) and `llm_models` (OpenAI-compat,
reached on the port the bastion reports in `llm_port`) — because a merged
list forces one derived port onto both and silently dials the wrong server.
Endpoint derivation mirrors embed_backends.resolve_embed_endpoint: prefer an
explicitly-reported model_endpoint, else http://<reported_ip>:<port>. A
pairing that reports no address (every pairing of a standalone Host: its
socket, its lane port, a server it connects to) is reached over the bastion's
serve lane instead, at a loopback origin of this Host's
(server/network/serve_relay.py), while that lane is connected.

Every endpoint here is a bare ORIGIN (scheme://host:port, no path). The
caller appends the route — provider_gateway._call_local normalizes to
/v1/chat/completions, exactly as memory_engine does for /v1/embeddings.

Import-safe: no heavy deps at import time.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("privatae.model_backends")

# A bastion advertises this when it serves OpenAI-compatible inference over the
# mesh (llama.cpp llama-server, vLLM). Mirrors embed_backends.EMBED_CAPABILITY.
# Set by the bastion when its OpenAI-compatible server answers: the address
# PRIVATAE_LLM_ADDR names (client-go/env.go llmTarget), or, unset, one it found
# on the machine's loopback.
LLM_OPENAI_CAPABILITY = "llm-openai"
DEFAULT_OLLAMA_PORT = 11434
# Fallback only — the bastion reports its real forward port in `llm_port`.
# Must stay in lockstep with the gateway's serveLLMForward listener.
DEFAULT_LLM_PORT = 18080


def _norm_model(mid: str) -> str:
    """Comparison form of a model id: trimmed, lowered, ':latest' dropped —
    'qwen3' and 'qwen3:latest' are the same served model to ollama."""
    m = (mid or "").strip().lower()
    return m[:-7] if m.endswith(":latest") else m


def resolve_model_endpoint(bastion: dict, port: int = DEFAULT_OLLAMA_PORT) -> str | None:
    """The bastion's model-serving ORIGIN (scheme://host:port, NO path) reachable
    over the host's mesh, for a server listening on `port`.

    `port` is explicit because one bastion can serve two model servers on two
    ports; the caller knows which advertisement it is resolving. An explicitly
    reported model_endpoint always wins and is returned untouched.
    """
    if not isinstance(bastion, dict):
        return None
    ep = (bastion.get("model_endpoint") or "").strip()
    if ep:
        return ep
    ip = (bastion.get("reported_ip") or "").strip()
    if not ip:
        return None
    host = f"[{ip}]" if (":" in ip and not ip.startswith("[")) else ip  # bracket bare IPv6
    return f"http://{host}:{int(port)}"


def _model_list(raw) -> list[str]:
    """A heartbeat model column (jsonb list, or its string form) as clean ids."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        mid = str(m).strip() if not isinstance(m, dict) else str(m.get("id") or "").strip()
        if mid:
            out.append(mid)
    return out


async def _serve_lane_endpoint(token_id, service: str) -> str | None:
    """The loopback origin of bastion `token_id`'s serve lane for `service`
    ('llm' or 'ollama'), or None when the lane is not connected now or this
    process has no lane (a brain run on its own, a test)."""
    if token_id is None:
        return None
    try:
        from server.network import serve_relay
    except Exception as e:  # noqa: BLE001 — no Host server in this process
        log.debug("serve lane unavailable (%s)", e)
        return None
    try:
        return await serve_relay.lane_endpoint(token_id, service)
    except Exception as e:  # noqa: BLE001 — a listener that cannot be made is no endpoint
        log.info("serve lane endpoint for bastion %s/%s: %s", token_id, service, type(e).__name__)
        return None


# How long one look at a model server's published context size may take, per
# request (/props, then /v1/models). Both answer from memory on llama.cpp and
# vLLM; a server that is slower than this is not waited on, and the turn goes
# on with what it knew (the refusal still teaches the window).
WINDOW_PROBE_TIMEOUT_S = 3.0
# How long a looked-up window is trusted before the server is asked again: a
# person who restarts llama-server with a larger `-c` is believed within this.
# A look that found nothing is retried sooner.
WINDOW_PROBE_TTL_S = 300.0
WINDOW_PROBE_MISS_TTL_S = 60.0
_WINDOW_PROBES: dict[tuple[str, str], tuple[float, int | None]] = {}


def _server_origin(endpoint: str) -> str:
    """The server's origin from a local endpoint: what the resolver hands out
    (scheme://host:port), or an owner's URL ending in /v1 or a full route."""
    ep = (endpoint or "").strip().rstrip("/")
    low = ep.lower()
    for tail in ("/chat/completions", "/v1"):
        if low.endswith(tail):
            ep, low = ep[: -len(tail)], low[: -len(tail)]
    return ep.rstrip("/")


async def probe_server_window(endpoint: str, model: str, *,
                              timeout_s: float = WINDOW_PROBE_TIMEOUT_S) -> int | None:
    """The context size the model server at `endpoint` says one request to
    `model` may fill, read from the server itself, or None when it publishes
    none. llama.cpp's GET /props (default_generation_settings.n_ctx, the
    slot's size), then GET /v1/models (vLLM's max_model_len, llama.cpp's
    meta.n_ctx, and the other names context_budget.server_window_from_models
    reads). A scoped pairing's bastion refuses /props (inferencefilter.go
    serves only model lists and a few reads to one), so /v1/models is asked
    too. Remembered for the model (context_budget.learn_context_window), so
    every reader of the window sees it; cached here for WINDOW_PROBE_TTL_S.
    Never raises."""
    from mastercode import context_budget as _cb
    origin = _server_origin(endpoint)
    want = _norm_model(model)
    if not origin or not want:
        return None
    key = (origin, want)
    import time as _t
    now = _t.monotonic()
    hit = _WINDOW_PROBES.get(key)
    if hit is not None:
        at, win = hit
        if now - at < (WINDOW_PROBE_TTL_S if win else WINDOW_PROBE_MISS_TTL_S):
            return win
    win = None
    try:
        import aiohttp
        tmo = aiohttp.ClientTimeout(total=timeout_s, connect=timeout_s)
        async with aiohttp.ClientSession(timeout=tmo) as s:
            for path, read in (("/props", lambda d: _cb.server_window_from_props(d)),
                               ("/v1/models", lambda d: _cb.server_window_from_models(d, model))):
                try:
                    async with s.get(origin + path) as r:
                        if r.status != 200:
                            continue
                        doc = await r.json(content_type=None)
                except Exception as e:  # noqa: BLE001 — one path failing is not the other's
                    log.debug("window probe %s%s: %s", origin, path, type(e).__name__)
                    continue
                win = read(doc)
                if win:
                    break
    except Exception as e:  # noqa: BLE001
        log.debug("window probe %s failed: %s", origin, type(e).__name__)
    _WINDOW_PROBES[key] = (now, win)
    if win:
        _cb.learn_context_window("local", model, win)
        log.info("local model server %s: %s allows %d tokens per request", origin, model, win)
    return win


async def list_local_models(pool, entity_id: int, *, strict: bool = False,
                            token_ids: set[int] | None = None) -> list[dict]:
    """Every model served by this host's ONLINE bastions, then by the model
    servers the owner named by address (mastercode/owner_endpoints), with the
    serving machine and the endpoint of the server that actually answers for
    it: [{id, bastion, endpoint, kind}]. `kind` is 'ollama' or 'llm-openai'.

    Both advertisements are read: `models` (ollama, DEFAULT_OLLAMA_PORT) and
    `llm_models` (OpenAI-compatible, on the bastion-reported `llm_port`). Each
    model carries ITS OWN server's endpoint — that is the whole point of the
    split; a shared endpoint dials the wrong one.

    Best-effort: a missing table/column (pre-advertisement pods) returns [] —
    the UI shows its honest 'discovery pending' state, never an error.
    `strict` raises the read's failure instead: the turn-type wall reads
    "none" as "this call reaches no server", so it must not be told that
    when the truth is "could not read".

    `token_ids` narrows the bastions to those rows (mcp_client_tokens.id);
    None is every bastion. The owner's named servers are listed either way."""
    try:
        from server.network.topology import ONLINE_WINDOW_SECONDS
        rows = await pool.fetch(
            f"""
            SELECT t.id AS token_id, h.hostname, h.reported_ip, h.models,
                   COALESCE(h.llm_models, '[]'::jsonb) AS llm_models,
                   h.llm_port
              FROM mcp_client_tokens t
              JOIN network_bastion_heartbeat h ON h.token_id = t.id
             WHERE t.entity_id = $1
               AND t.operator_kind = 'bastion'
               AND t.revoked_at IS NULL
               AND h.last_seen_at > now() - INTERVAL '{int(ONLINE_WINDOW_SECONDS)} seconds'
            """,
            entity_id,
        )
    except Exception as e:
        if strict:
            raise
        log.debug("list_local_models: bastion table unavailable (%s)", e)
        rows = []   # the owner's own servers below are still listed
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        if token_ids is not None and r.get("token_id") not in token_ids:
            continue
        host = r["hostname"] or ""
        # Ollama FIRST so a box serving the same id on both servers keeps the
        # endpoint it resolved to before the split existed (no silent re-route).
        lanes = (
            (_model_list(r["models"]), DEFAULT_OLLAMA_PORT, "ollama", "ollama"),
            (_model_list(r["llm_models"]), r["llm_port"] or DEFAULT_LLM_PORT, "llm-openai", "llm"),
        )
        for ids, port, kind, service in lanes:
            if not ids:
                continue
            ep = resolve_model_endpoint({"reported_ip": r["reported_ip"]}, port)
            if not ep:
                # A pairing with no address of its own (every pairing of a
                # standalone Host: its socket, its lane port, or a server it
                # connects to) serves its models over the bastion's serve lane
                # (server/network/serve_relay.py), reached at a loopback origin
                # while that lane is connected.
                ep = await _serve_lane_endpoint(r.get("token_id"), service)
            if not ep:
                continue
            for mid in ids:
                key = (_norm_model(mid), host)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"id": mid, "bastion": host, "endpoint": ep, "kind": kind})
    # Model servers the owner named by address (mastercode/owner_endpoints),
    # after every bastion, so a bastion serving the same model is picked first.
    try:
        from mastercode import owner_endpoints
        for m in await owner_endpoints.models(pool, entity_id):
            key = (_norm_model(m["id"]), m["bastion"])
            if key not in seen:
                seen.add(key)
                out.append(m)
    except Exception as e:
        if strict:
            raise
        log.debug("owner model servers unavailable (%s)", e)
    return out


async def match_local_model(pool, entity_id: int, model_id: str,
                            allowed: set[str] | None = None) -> dict | None:
    """Does a machine on the host's own network serve `model_id` right now?
    Returns {id, bastion, endpoint, kind} for the first ONLINE server of that
    model, else None. ':latest' differences are tolerated both ways.

    `allowed` is the servers a typed turn lists, and the pick is the first
    match on one of them. None is any server (an untyped turn); a set holding
    '*' is any server too, because '*' is how a type reaches a server it
    cannot name."""
    want = _norm_model(model_id)
    if not want:
        return None
    _any = allowed is None or "*" in allowed
    for m in await list_local_models(pool, entity_id):
        if _norm_model(m["id"]) == want and (_any or m["bastion"] in allowed):
            return m
    return None


async def local_model_servers(pool, entity_id: int, model_id: str, *,
                              strict: bool = False) -> list[str]:
    """Every ONLINE server that serves `model_id` right now, by hostname. What
    a refusal names when none of them is a server the turn's type lists, and
    what the turn-type wall checks a local bench against (`strict`: a failed
    read raises rather than reading as none)."""
    want = _norm_model(model_id)
    if not want:
        return []
    return sorted({m["bastion"] for m in await list_local_models(pool, entity_id, strict=strict)
                   if _norm_model(m["id"]) == want})
