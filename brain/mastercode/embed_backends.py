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
        if EMBED_CAPABILITY not in (b.get("capabilities") or []):
            continue
        if not b.get("hostname"):
            continue  # un-addressable
        ep = resolve_embed_endpoint(b)
        if not ep:
            continue  # online + capable but no reachable endpoint
        out.append({
            "hostname": b.get("hostname"),
            "name": b.get("name"),
            "handle": b.get("handle"),
            "id": b.get("id"),
            "capabilities": b.get("capabilities") or [],
            "models": b.get("models") or [],
            "embed_endpoint": ep,
            "last_seen_at": b.get("last_seen_at"),
        })
    return out


async def resolve_endpoint_for_hostname(pool, entity_id: int, server: str) -> str | None:
    """Resolve a bastion's embedder endpoint LIVE from its latest heartbeat —
    the reader-side companion to validate_embed_bastion_target. The persisted
    embedding config stores the bastion IDENTITY (hostname), never a raw tunnel
    IP: tunnel addresses change on every re-enroll, and a pinned IP strands the
    host on a dead endpoint (the exact failure of 2026-07-09/10). Freshness is
    deliberately NOT required here: the tunnel IP only changes when the bastion
    re-enrolls, and a re-enroll writes a new heartbeat row immediately — so the
    latest row is always the right address, even mid heartbeat-gap. Revoked
    bastions never resolve (their token is gone). Returns None when unknown."""
    server = (server or "").strip()
    if not server or pool is None or entity_id is None:
        return None
    try:
        row = await pool.fetchrow(
            """
            SELECT h.reported_ip
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
    if not row or not row["reported_ip"]:
        return None
    return resolve_embed_endpoint({"reported_ip": str(row["reported_ip"])})


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
                            dim: int | None = None) -> dict:
    """Point this host's embeddings at its bastion `server`. Resolves the bastion's
    embedder endpoint and writes the BYO embedding config via apply_embedding_switch.

    Model selection:
      - `model` omitted, host already has a model → KEEP it (just move WHERE it
        embeds). No dim change, so with multi-dim storage nothing is re-embedded.
      - `model` omitted, host has NO model yet → pick the embedding model the
        bastion serves (choose_bastion_embed_model). A fresh host should never
        have to name a model the bastion's own heartbeat already lists — the
        2026-09-02 test host guessed one the box did not run.
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
            pick = choose_bastion_embed_model(served)
            if not pick:
                return {"error": (f"this host has no embedding model yet and '{server}' serves no "
                                  f"embedding model it can store (served: "
                                  f"{', '.join(str(m) for m in served) or 'nothing'})"),
                        "served_embedding_models": [n for n, _ in served_known]}
            target_model, target_dim = pick
            chosen = True
    # `bastion=server` persists the bastion IDENTITY next to the endpoint: the
    # embed reader re-resolves the live endpoint from the heartbeat every call
    # (resolve_endpoint_for_hostname), so a re-enroll (new tunnel IP) self-heals
    # instead of stranding embeds on a dead pinned address.
    res = await apply_embedding_switch(
        pool, entity_id, target_model, provider="local",
        base_url=endpoint, dim=target_dim, cred_name=cred_name,
        bastion=server)
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
        res["note"] = (
            f"embeds now route to bastion '{server}' serving {target_model}"
            + (" (picked from what the machine serves)" if chosen else "")
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
