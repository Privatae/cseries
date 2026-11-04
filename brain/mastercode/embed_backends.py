"""
Embedding backends — the BASTION EMBED LANE.

A host's MEMORY/CONTEXT embeddings should compute on the host's OWN bastion (their GPU), keeping the host's OWN model — so qwen3@2560
stays qwen3@2560, served from the user's hardware. Never dumped, never migrated.

Two embedding domains, never sharing a backend:
  - PLATFORM MANAGED: a catalog model on the harness pool (everyday users) UNTIL
    a user allocates their own model through their bastion.
  - HOST (sovereign): the host points its embeds at ITS bastion's embedder via
    this lane. The host's model is UNCHANGED — only WHERE it embeds moves to the
    bastion, so existing (paid) vectors are never re-embedded.

This is the HOST side. It resolves the host's embed-capable bastion and wires the
existing BYO embedding path (server.routes.embedding.apply_embedding_switch
base_url -> memory_engine._embed_via_byo): embeds go DIRECT to the bastion's
embedder over the mesh — DATA-PLANE, never through the control relay (a dead/slow
embedder must never touch the control connection — the bridge circuit-breaker
lesson). When a sealed token is registered, _embed_via_byo routes through the
vault broker so the raw key never reaches this layer.

A PAIRING WITH NO ADDRESS (every pairing of a standalone Host: its socket,
its lane port, a server it connects to) has no tunnel address to dial, so
its embedder is reached over the bastion's serve lane
(server/network/serve_relay.py), at a loopback origin of this Host's, while
that lane is connected: the "ollama" service for a model in `models` (the
`embed` capability), the "llm" service (llama.cpp's llama-server, vLLM,
LM Studio, all OpenAI /v1/embeddings) for one in `llm_models`. The Host names
the service; the bastion dials only the server set on its own machine. A
bastion that reports an address is resolved exactly as before.

PAIRED, NOT IN THIS MODULE:
  - Bastion side (the gateway): advertise the 'embed' capability + report its
    embedder endpoint in the heartbeat. Until then resolve_embed_endpoint falls
    back to reported_ip:11434.
  - Pod -> bastion embedder reachability is VERIFIED on a real bastion before a
    host is wired live — never assumed.

Import-safe: no heavy deps at import time.
"""
from __future__ import annotations

import logging

log = logging.getLogger("privatae.embed_backends")

# A bastion advertises this when it can serve embeddings (runs a local embedder
# reachable over the host's mesh). Mirrors context_backends.CONTEXT_STORE_CAPABILITY.
EMBED_CAPABILITY = "embed"
# A bastion advertises this ADDITIONALLY when it runs the embed reverse-relay worker
# (opens worker WebSockets to the host on :8443/bastion-embed). When present,
# resolve_embed_endpoint emits a `bastion-embed://<hostname>` sentinel and the host
# reaches the embedder over the relay instead of forward-dialing its tunnel IP.
# Mirrors how EMBED_CAPABILITY already gates the picker (list_embed_bastions:78).
EMBED_RELAY_CAPABILITY = "embed-relay"
DEFAULT_OLLAMA_PORT = 11434


def resolve_embed_endpoint(bastion: dict) -> str | None:
    """The bastion's embedder URL reachable over the host's mesh. Prefers an
    explicitly-reported endpoint (the bastion-side advertises `embed_endpoint`).
    When the bastion advertises the `embed-relay` capability, returns a
    `bastion-embed://<hostname>` sentinel — the host reaches the embedder over the
    reverse relay (memory_engine._embed_via_byo detects the sentinel) instead of
    forward-dialing its tunnel IP. Else derives http://<reported_ip>:11434 (standard
    ollama). Returns None when the bastion is unaddressable (no endpoint, no relay,
    and no reported_ip)."""
    if not isinstance(bastion, dict):
        return None
    ep = (bastion.get("embed_endpoint") or "").strip()
    if ep:
        return ep
    # Capability-gated relay (option A): a bastion running the embed
    # worker reaches the host over the reverse relay, so the pod never dials its tunnel
    # IP. Version-independent — an old bastion never advertises it → unchanged http path.
    caps = bastion.get("capabilities") or []
    host_name = (bastion.get("hostname") or "").strip()
    if EMBED_RELAY_CAPABILITY in caps and host_name:
        return f"bastion-embed://{host_name}"
    ip = (bastion.get("reported_ip") or "").strip()
    if not ip:
        return None
    host = f"[{ip}]" if (":" in ip and not ip.startswith("[")) else ip  # bracket bare IPv6
    return f"http://{host}:{DEFAULT_OLLAMA_PORT}"


def _addressless(bastion: dict) -> bool:
    """A pairing that reports no address of its own: a standalone Host's
    (serve_relay.py). Its embedder rides the serve lane."""
    return not (bastion.get("reported_ip") or "").strip()


def lane_embed_service(bastion: dict, model: str | None = None) -> str | None:
    """Which serve-lane service carries an address-less bastion's embedder:
    "ollama" for a model it serves on ollama (the `embed` capability), "llm"
    for one in its OpenAI-compatible server's list, None when neither holds
    `model` (or, with no model named, neither holds an embedding model this
    host knows). Ollama first, as model_backends lists it."""
    from mastercode.model_backends import _norm_model as _norm
    caps = bastion.get("capabilities") or []
    ollama = [str(m) for m in (bastion.get("models") or [])]
    llm = [str(m) for m in (bastion.get("llm_models") or [])]
    if model:
        want = _norm(model)
        if EMBED_CAPABILITY in caps and any(_norm(m) == want for m in ollama):
            return "ollama"
        if any(_norm(m) == want for m in llm):
            return "llm"
        return None
    if EMBED_CAPABILITY in caps and known_embed_models(ollama):
        return "ollama"
    if known_embed_models(llm):
        return "llm"
    if EMBED_CAPABILITY in caps and any(embedding_named(m) for m in ollama):
        return "ollama"
    # Any model the machine's OpenAI-compatible server lists is a candidate
    # (embed_candidates): llama.cpp names a model by its file unless aliased,
    # so only a test embed can tell (choose_embed_model_by_probe).
    if llm:
        return "llm"
    return None


async def resolve_embed_endpoint_live(bastion: dict, model: str | None = None) -> str | None:
    """resolve_embed_endpoint, and for a bastion that reports no address, its
    serve-lane origin (lane_embed_service) while that lane is connected. A
    bastion with an address is resolved exactly as resolve_embed_endpoint
    does. `bastion["id"]` is its token (topology.list_bastions)."""
    ep = resolve_embed_endpoint(bastion)
    if ep or not isinstance(bastion, dict) or not _addressless(bastion):
        return ep
    service = lane_embed_service(bastion, model)
    if not service:
        return None
    from mastercode.model_backends import _serve_lane_endpoint
    return await _serve_lane_endpoint(bastion.get("id"), service)


def _embed_models_of(bastion: dict) -> list:
    """The models a bastion's embedder can be picked from: its ollama list,
    and for an address-less pairing its OpenAI-compatible server's list too
    (both reached over the serve lane)."""
    models = list(bastion.get("models") or [])
    if _addressless(bastion):
        models += [m for m in (bastion.get("llm_models") or []) if m not in models]
    return models


async def list_embed_bastions(pool, entity_id: int) -> list[dict]:
    """The host's ONLINE bastions that advertise the 'embed' capability AND have a
    resolvable embedder endpoint — the choosable targets to serve this host's
    embeddings. Returns [{hostname, name, handle, id, capabilities,
    embed_endpoint, last_seen_at}]. Best-effort: any failure returns [] so the
    picker degrades to 'no live embedder' rather than raising."""
    try:
        from server.network import topology
        rows = await topology.list_bastions(pool, entity_id)
    except Exception as e:
        log.debug("list_embed_bastions: topology unavailable (%s)", e)
        return []
    out = []
    for b in rows:
        if not b.get("online"):
            continue
        # An address-less pairing's OpenAI-compatible server serving an
        # embedding model is an embedder too (lane_embed_service), with no
        # `embed` capability: that capability is the bastion's ollama probe.
        if EMBED_CAPABILITY not in (b.get("capabilities") or []) and not (
                _addressless(b) and lane_embed_service(b) == "llm"):
            continue
        if not b.get("hostname"):
            continue  # un-addressable
        ep = await resolve_embed_endpoint_live(b)
        if not ep:
            continue  # online + capable but no reachable endpoint
        out.append({
            "hostname": b.get("hostname"),
            "name": b.get("name"),
            "handle": b.get("handle"),
            "id": b.get("id"),
            "capabilities": b.get("capabilities") or [],
            "models": _embed_models_of(b),
            "embed_endpoint": ep,
            "last_seen_at": b.get("last_seen_at"),
        })
    return out


async def resolve_endpoint_for_hostname(pool, entity_id: int, server: str,
                                        model: str | None = None) -> str | None:
    """Resolve a bastion's embedder endpoint LIVE from its latest heartbeat —
    the reader-side companion to validate_embed_bastion_target. The persisted
    embedding config stores the bastion IDENTITY (hostname), never a raw tunnel
    IP: tunnel addresses change on every re-enroll, and a pinned IP strands the
    host on a dead endpoint (the exact failure of 2026-07-09/10). Freshness is
    deliberately NOT required here: the tunnel IP only changes when the bastion
    re-enrolls, and a re-enroll writes a new heartbeat row immediately — so the
    latest row is always the right address, even mid heartbeat-gap. Revoked
    bastions never resolve (their token is gone). Returns None when unknown.

    A bastion whose latest beat reports no address is reached over its serve
    lane (resolve_embed_endpoint_live): the service is the one whose list
    holds `model` (the host's embedding model), and the origin exists only
    while that lane is connected, so a machine that went away is None here,
    and the caller marks the lane down."""
    server = (server or "").strip()
    if not server or pool is None or entity_id is None:
        return None
    try:
        row = await pool.fetchrow(
            """
            SELECT t.id AS token_id, h.reported_ip, h.capabilities, h.models,
                   h.llm_models
              FROM network_bastion_heartbeat h
              JOIN mcp_client_tokens t ON t.id = h.token_id
             WHERE h.hostname = $1
               AND t.entity_id = $2
               AND t.operator_kind = 'bastion'
               AND t.revoked_at IS NULL
             ORDER BY h.last_seen_at DESC NULLS LAST
             LIMIT 1
            """, server, entity_id)
    except Exception as e:
        log.warning("resolve_endpoint_for_hostname(%s): heartbeat lookup failed: %s", server, e)
        return None
    if not row:
        return None
    if row["reported_ip"]:
        return resolve_embed_endpoint({"reported_ip": str(row["reported_ip"])})
    return await resolve_embed_endpoint_live({
        "id": row.get("token_id"), "reported_ip": "",
        "capabilities": _json_list(row.get("capabilities")),
        "models": _json_list(row.get("models")),
        "llm_models": _json_list(row.get("llm_models")),
    }, model)


def _json_list(raw) -> list:
    """A heartbeat jsonb model column as clean ids (model_backends' reader)."""
    from mastercode.model_backends import _model_list
    return _model_list(raw)


async def validate_embed_bastion_target(pool, entity_id: int, server: str) -> tuple[bool, str, str | None]:
    """Is `server` (a hostname) an ONLINE, embed-capable, reachable bastion right
    now? Returns (ok, reason, embed_endpoint). Used before wiring so the host
    targets a real live embedder, not a typed hostname."""
    server = (server or "").strip()
    if not server:
        return False, "no server given", None
    for b in await list_embed_bastions(pool, entity_id):
        if b["hostname"] == server:
            return True, "online embed bastion", b["embed_endpoint"]
    # Distinguish 'exists but ineligible' from 'unknown' for a useful message.
    try:
        from server.network import topology
        allb = await topology.list_bastions(pool, entity_id)
    except Exception:
        allb = []
    m = next((b for b in allb if b.get("hostname") == server), None)
    if m is None:
        return False, f"'{server}' is not a known bastion of this host", None
    if not m.get("online"):
        return False, f"bastion '{server}' is offline (no recent heartbeat)", None
    if _addressless(m) and lane_embed_service(m):
        return False, (f"bastion '{server}' serves an embedding model on its own machine, but its "
                       "serve lane is not connected to this Host right now"), None
    if EMBED_CAPABILITY not in (m.get("capabilities") or []):
        return False, f"bastion '{server}' does not advertise the '{EMBED_CAPABILITY}' capability", None
    return False, f"bastion '{server}' reports no reachable embedder endpoint", None


def known_embed_models(models) -> list[tuple[str, int]]:
    """The subset of a bastion's served models this host can store vectors
    for: [(name, dim)] in the bastion's order. The `models` list is every
    ollama tag on the box — chat models included — so a caller that wants an
    EMBEDDING model must filter, and this is the one filter (MODEL_DIMS is the
    registry of dims the memory columns can hold)."""
    from mastercode.memory_engine import MODEL_DIMS
    out: list[tuple[str, int]] = []
    for m in models or []:
        name = str(m or "").strip()
        if not name:
            continue
        dim = MODEL_DIMS.get(name) or MODEL_DIMS.get(name.split(":", 1)[0])
        if dim:
            out.append((name, int(dim)))
    return out


# The parts of a model id that name an embedding family, read from the id's
# name before any ':' tag, split on / : _ . - and space ("some-embed.gguf",
# "nomic-embed-text", "bge-m3", "all-MiniLM-L6-v2"); any part starting "embed"
# counts too. Also the rule local_model_kind falls back to when a model's server
# cannot say whether it is an embedding model.
_EMBED_NAME_PARTS = frozenset({"embed", "embedding", "minilm", "bge", "e5", "gte", "paraphrase"})


def embedding_named(model_id) -> bool:
    """Does the model's id name an embedding model (_EMBED_NAME_PARTS)?"""
    import re as _re
    mid = str(model_id or "").strip().lower()
    if not mid:
        return False
    parts = set(_re.split(r"[/:_.\-\s]+", mid.split(":", 1)[0]))
    return bool(parts & _EMBED_NAME_PARTS) or any(p.startswith("embed") for p in parts)


def embed_candidates(bastion: dict) -> list[dict]:
    """The models on a bastion's machine that could be its embedder, in the
    order they are tried: [{model, dim, why}]. `dim` is the dim this host
    knows the model at (MODEL_DIMS), None for one it does not know, which the
    test embed measures.

      1. "known": models this host knows as embedders, best first
         (choose_bastion_embed_model's order: an indexable dim, then the
         largest, then the name);
      2. "named": models whose id names an embedding model (embedding_named),
         by name — llama.cpp's `-m some-embed.gguf` is listed as its file;
      3. "listed": every other model the machine's OpenAI-compatible server
         lists (an address-less pairing's llama.cpp, vLLM or LM Studio). A
         server that only chats refuses the embed (llama.cpp: "This server does
         not support embeddings. Start it with `--embeddings`"), so it is never
         chosen. Not ollama's other tags: those are its chat models, which
         ollama may still answer an embed for, and loading one to find out
         costs the machine a model's memory.

    The list only says what to TRY: choose_embed_model_by_probe decides, by a
    real embed against each in turn (decision: an endpoint is assigned only
    when a live probe embed proves it serves the model at the expected dim)."""
    if not isinstance(bastion, dict):
        return []
    caps = bastion.get("capabilities") or []
    ollama = [str(m) for m in (bastion.get("models") or [])] if EMBED_CAPABILITY in caps else []
    llm = [str(m) for m in (bastion.get("llm_models") or [])] if _addressless(bastion) else []
    pool_ids: list[str] = []
    for m in ollama + llm:
        if m.strip() and m not in pool_ids:
            pool_ids.append(m)
    from mastercode.memory_engine import _HNSW_INDEXABLE_DIMS
    known = known_embed_models(pool_ids)
    known.sort(key=lambda t: (t[1] not in _HNSW_INDEXABLE_DIMS, -t[1], t[0]))
    out = [{"model": n, "dim": d, "why": "known"} for n, d in known]
    seen = {n for n, _ in known}
    for m in pool_ids:
        if m not in seen and embedding_named(m):
            out.append({"model": m, "dim": None, "why": "named"})
            seen.add(m)
    for m in llm:
        if m not in seen and m.strip():
            out.append({"model": m, "dim": None, "why": "listed"})
            seen.add(m)
    return out


def peer_forward_for(endpoint: str | None, bastion: str | None) -> str | None:
    """The Hub's host→peer forward an embed to a bastion at `endpoint` rides,
    or None: a bastion at a mesh address is reached only through it; one at
    its serve-lane origin on this Host's loopback is dialled directly."""
    if not bastion or not endpoint:
        return None
    try:
        from server.network import serve_relay as _serve_relay
        if _serve_relay.is_lane_origin(endpoint):
            return None
    except Exception:  # noqa: BLE001 — no Host server in this process
        pass
    import os as _os
    return (_os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129"


async def choose_embed_model_by_probe(pool, entity_id: int, server: str,
                                      bastion: dict | None = None) -> dict:
    """Pick the embedding model for bastion `server` by TESTING the candidates
    its machine lists (embed_candidates, best first): one real embed against
    each, over the path its embeds would take, until one answers with a
    vector at a dim this host can store; a model this host knows must answer
    at the dim it is known at. The first that passes is the choice, so a
    known good model wins over one picked by its name, and either over one
    the server merely lists.

    Returns {model, dim, endpoint, why, tried} or {error, tried}; `tried` is
    every candidate with what its test embed said, the record of the choice."""
    from mastercode.memory_engine import EMBEDDING_COLUMN_BY_DIM, measure_embedding_dim
    if bastion is None:
        try:
            from server.network import topology
            rows = await topology.list_bastions(pool, entity_id)
        except Exception as e:  # noqa: BLE001
            return {"error": f"bastions unreadable: {e}", "tried": []}
        bastion = next((b for b in rows if (b.get("hostname") or "") == server), None)
    cands = embed_candidates(bastion or {})
    tried: list[dict] = []
    for c in cands:
        model = c["model"]
        ep = await resolve_endpoint_for_hostname(pool, entity_id, server, model)
        if not ep:
            tried.append({"model": model, "result": "no endpoint reachable for it now"})
            continue
        dim, why = await measure_embedding_dim(ep, model, peer_forward=peer_forward_for(ep, server))
        if dim is None:
            tried.append({"model": model, "result": f"no vector: {why}"})
            continue
        if c["dim"] and dim != c["dim"]:
            tried.append({"model": model, "result": f"answered at {dim}d, expected {c['dim']}d"})
            continue
        if dim not in EMBEDDING_COLUMN_BY_DIM:
            tried.append({"model": model, "result": (f"answered at {dim}d, which this host has no column "
                                                     f"for (it stores {sorted(EMBEDDING_COLUMN_BY_DIM)})")})
            continue
        tried.append({"model": model, "result": f"embeds at {dim}d"})
        log.info("embedding model for %s chosen by test embed: %s (%dd, %s) after %d tried",
                 server, model, dim, c["why"], len(tried))
        return {"model": model, "dim": dim, "endpoint": ep, "why": c["why"], "tried": tried}
    if not cands:
        return {"error": f"'{server}' lists no model that could be an embedder", "tried": tried}
    return {"error": (f"no model on '{server}' answered a test embed at a dim this host can store "
                      f"({'; '.join(t['model'] + ': ' + t['result'] for t in tried)})"),
            "tried": tried}


def choose_record(server: str, pick: dict) -> dict:
    """What is kept with the embedding config about how its model was chosen
    (embedding_chosen_by): by a test embed, on which machine, and what each
    candidate answered. Model ids and dims only."""
    from datetime import datetime, timezone
    return {"how": "test embed", "bastion": server, "model": pick.get("model"),
            "dim": pick.get("dim"), "picked_as": pick.get("why"),
            "tried": list(pick.get("tried") or []),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def choose_bastion_embed_model(models) -> tuple[str, int] | None:
    """Pick the embedding model to serve from a bastion's model list, or None.

    Preference, in order: a dim pgvector can index (an unindexable column is a
    sequential scan on every recall — memory_engine._HNSW_INDEXABLE_DIMS), then
    the largest dim, then name order so the choice is stable across beats.
    The probe in apply_embedding_switch still has the final word."""
    from mastercode.memory_engine import _HNSW_INDEXABLE_DIMS
    cands = known_embed_models(models)
    if not cands:
        return None
    cands.sort(key=lambda t: (t[1] not in _HNSW_INDEXABLE_DIMS, -t[1], t[0]))
    return cands[0]


async def _bastion_models(pool, entity_id: int, server: str) -> list:
    for b in await list_embed_bastions(pool, entity_id):
        if b.get("hostname") == server:
            return b.get("models") or []
    return []


async def set_embed_bastion(pool, entity_id: int, server: str,
                            cred_name: str | None = None,
                            model: str | None = None,
                            dim: int | None = None,
                            chosen_by: dict | None = None) -> dict:
    """Point this host's embeddings at its bastion `server`. Resolves the bastion's
    embedder endpoint and writes the BYO embedding config via apply_embedding_switch.

    Model selection:
      - `model` omitted, host already has a model → KEEP it (just move WHERE it
        embeds). No dim change, so with multi-dim storage nothing is re-embedded.
      - `model` omitted, host has NO model yet → pick the embedding model the
        bastion's machine serves by testing what it lists
        (choose_embed_model_by_probe), and keep the record of that choice. A
        fresh host should never have to name a model the bastion's own
        heartbeat already lists — the 2026-09-02 test host guessed one the box
        did not run.
      - `model` given → serve THAT model on the bastion (e.g. move bge-m3 → a
        qwen the box actually runs). apply_embedding_switch probe-gates it (the
        bastion must return a vector for that model at that dim) and, because
        storage is multi-dim, still never dumps: prior-model vectors stay in their
        own column and only the target dim column is (optionally) filled. The dim
        follows the model when not given explicitly.

    Either way this NEVER dumps existing vectors — switching is a pointer change.
    A refused probe names the embedding models the box DOES serve, so the next
    call needs no guess."""
    ok, reason, endpoint = await validate_embed_bastion_target(pool, entity_id, server)
    if not ok:
        return {"error": reason}
    from server.routes.embedding import current as _current, apply_embedding_switch
    served = await _bastion_models(pool, entity_id, server)
    served_known = known_embed_models(served)
    target_model = (model or "").strip()
    target_dim = dim
    chosen = False
    if not target_model:
        cur = await _current()
        target_model = cur.get("model")
        target_dim = cur.get("dim")
        if not target_model:
            pick = await choose_embed_model_by_probe(pool, entity_id, server)
            if pick.get("error"):
                return {"error": (f"this host has no embedding model yet and no model on '{server}' "
                                  f"embeds at a dim it can store: {pick['error']}"),
                        "served_embedding_models": [n for n, _ in served_known],
                        "tried": pick.get("tried") or []}
            target_model, target_dim = pick["model"], pick["dim"]
            chosen_by = choose_record(server, pick)
            chosen = True
        elif chosen_by is None:
            # The model is KEPT and only WHERE it embeds moves: the record of
            # how it was chosen still describes it, so it stays
            # (apply_embedding_switch clears the record when given none).
            try:
                from server.routes.embedding import _load_bp
                chosen_by = (await _load_bp(pool, entity_id)).get("embedding_chosen_by") or None
            except Exception:  # noqa: BLE001 — a lost record is not a failed move
                chosen_by = None
    kept_record = bool(chosen_by) and not chosen and not model
    # An address-less pairing can serve embedders on two servers (ollama and
    # its OpenAI-compatible one), each with its own lane origin, and the
    # endpoint above is the one picked with no model named. Once the model is
    # known, the origin is the one of the server that holds it; a model on
    # neither keeps the endpoint, and the probe refuses it.
    from mastercode.memory_engine import _is_serve_lane_origin
    if target_model and _is_serve_lane_origin(endpoint):
        by_model = await resolve_endpoint_for_hostname(pool, entity_id, server, target_model)
        if by_model:
            endpoint = by_model
    # `bastion=server` persists the bastion IDENTITY next to the endpoint: the
    # embed reader re-resolves the live endpoint from the heartbeat every call
    # (resolve_endpoint_for_hostname), so a re-enroll (new tunnel IP) self-heals
    # instead of stranding embeds on a dead pinned address.
    res = await apply_embedding_switch(
        pool, entity_id, target_model, provider="local",
        base_url=endpoint, dim=target_dim, cred_name=cred_name,
        bastion=server, chosen_by=chosen_by)
    if isinstance(res, dict) and res.get("error"):
        names = [n for n, _ in served_known]
        res["served_embedding_models"] = names
        res["error"] += (f" — embedding models '{server}' serves: {', '.join(names)}"
                         if names else f" — '{server}' serves no embedding model this host knows")
        return res
    if isinstance(res, dict):
        res.setdefault("bastion", server)
        res.setdefault("embed_endpoint", endpoint)
        res["served_embedding_models"] = [n for n, _ in served_known]
        if chosen_by:
            res["chosen_by"] = chosen_by
        res["note"] = (
            f"embeds now route to bastion '{server}' serving {target_model}"
            + (" (picked by a test embed of what the machine serves)"
               if (chosen_by and not kept_record) else "")
            if (model or chosen) else
            "model kept unchanged; embeds now route to the host's bastion"
        ) + " (no re-embed; existing vectors untouched)"
    return res


async def clear_embed_bastion(pool, entity_id: int) -> dict:
    """Stop using the bastion embedder; fall back to the platform-managed path
    for the host's CURRENT model (no re-embed — base_url is cleared, model kept)."""
    from server.routes.embedding import current as _current, apply_embedding_switch
    cur = await _current()
    model = cur.get("model")
    if not model:
        return {"error": "no current model"}
    return await apply_embedding_switch(pool, entity_id, model, provider="privatae")
