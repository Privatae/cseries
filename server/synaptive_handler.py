"""
Synaptive — relay handler for action="synaptive_shape".

Validates the bearer against mcp_client_tokens (container is the sole
validator; edge does not see tokens), loads this Host's name, master
directive and current_weights, and returns what an outside chat needs to
answer as this Host on this turn: who it is, and the rules that apply.

Returned shape:
  {
    "ok": True,
    "display_name": str,       # the Host's name ("Host #N" when unset)
    "directive": str,          # the master directive (may be empty)
    "base_fragment": str,      # tendencies rendered from current_weights
    "turn_directive": str,     # the rules that apply to this message (may be empty)
  }
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from server import brain_service, secret_cut

logger = logging.getLogger("privatae.pc.synaptive_handler")


def _entity_id() -> int:
    return int(os.environ.get("ENTITY_ID", "0"))


def _err(status: str, msg: str) -> dict:
    return {"ok": False, "status": status, "error": msg}


async def _validate_bearer(pool, bearer: str) -> dict | None:
    """Resolve a bearer string to an active mcp_client_tokens row.

    Returns None for any failure mode. Internally distinguishes the failure
    classes at INFO level so telemetry can answer "is this client sending
    garbage" vs "is this token revoked" — clients still see only None /
    HTTP 401 (don't leak details).
    """
    if not bearer or not isinstance(bearer, str):
        logger.info("bearer rejected: missing or non-string")
        return None
    if bearer.startswith("Bearer "):
        bearer = bearer[7:].strip()
    if not bearer.startswith("mcp_") or len(bearer) < 16:
        # Prefix + length are public guarantees of the mint surface; failing
        # this means the caller sent a garbage string.
        logger.info("bearer rejected: bad format (len=%d)", len(bearer))
        return None
    digest = hashlib.sha256(bearer.encode()).digest()
    row = await pool.fetchrow(
        """
        SELECT id, entity_id, scopes, hardware_fingerprint, operator_kind,
               revoked_at, expires_at
          FROM mcp_client_tokens
         WHERE token_hash = $1
        """,
        digest,
    )
    if not row:
        logger.info("bearer rejected: not in token table")
        return None
    if row["revoked_at"] is not None:
        logger.info("bearer rejected: revoked at %s (token id=%d)",
                    row["revoked_at"], row["id"])
        return None
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
        logger.info("bearer rejected: expired at %s (token id=%d)",
                    row["expires_at"], row["id"])
        return None
    await pool.execute(
        "UPDATE mcp_client_tokens SET last_used_at = now() WHERE id = $1",
        row["id"],
    )
    return {
        "id":                   row["id"],
        "entity_id":            row["entity_id"],
        "scopes":               list(row["scopes"]),
        "hardware_fingerprint": row["hardware_fingerprint"],   # NULL for pre-gateway tokens
        "operator_kind":        row["operator_kind"],
    }


async def _load_identity(pool, entity_id: int) -> dict | None:
    """The Host's name, master directive, stored directive hash and
    current_weights (the person's sliders), or None when the row is gone."""
    row = await pool.fetchrow(
        """
        SELECT display_name, master_directive, current_weights, directive_hash
          FROM entity_registry WHERE id = $1
        """,
        entity_id,
    )
    if not row:
        return None
    raw = row["current_weights"]
    if isinstance(raw, str):
        try:
            weights = json.loads(raw)
        except Exception:
            weights = {}
    else:
        weights = raw or {}
    return {
        "display_name": row["display_name"],
        "master_directive": (row["master_directive"] or "").strip(),
        "directive_hash": (row["directive_hash"] or "").strip().lower(),
        "weights": weights if isinstance(weights, dict) else {},
    }


def _base_fragment(weights: dict) -> str:
    try:
        from mastercode.runtime.prompt_builder import _weights_to_tendencies
    except Exception as e:
        logger.warning("Tendencies builder unavailable: %s", e)
        return ""
    lines = _weights_to_tendencies(weights or {}) or []
    return "\n".join(lines)


async def _ceigas_gate(entity_id: int, token_id: int) -> tuple[bool, str]:
    """The operator kill switch on the synaptive doors (shape, recall,
    identity, signal), the `external.mcp -> synaptive.shape` crossing. The
    caller is the bearer token each handler validates first.

    shape() is an UNTRUSTED external-MCP surface, so this gate FAILS CLOSED: if
    the switch cannot be checked, the crossing is DENIED."""
    try:
        from ceigas_kill_switch import KILL_SWITCH_REASON, kill_switch_refuses
        if kill_switch_refuses("external.mcp", "synaptive.shape", "invoke"):
            return False, KILL_SWITCH_REASON
        return True, "authorized"
    except Exception as e:
        logger.exception("CEIGAS kill-switch check raised — DENY (fail-closed)")
        return False, f"ceigas gate error: {e}"


async def _record_observability(
    pool,
    entity_id: int,
    token_id: int,
    message: str,
    directive: str,
    elapsed_ms: int,
) -> None:
    """Best-effort cost-ledger row; a failure never blocks the caller's
    response."""

    async def _cost_log():
        try:
            # $0 row keeps the inference cost ledger complete. model_id is the
            # model that actually embedded this turn (config-driven) — 'none'
            # when nothing embedded (the rules then come by importance).
            from mastercode.memory_engine import last_embed_model
            await pool.execute(
                """INSERT INTO inference_cost_log
                       (entity_id, source_module, model_id, provider, purpose,
                        tokens_in, tokens_out, est_cost_usd, latency_ms, metadata)
                   VALUES ($1, 'synaptive_handler', $5, 'local',
                           'shape', $2, 0, 0.0, $3, $4::jsonb)""",
                entity_id,
                len(message),  # crude tokens_in proxy until we measure for real
                elapsed_ms,
                json.dumps({"token_id": token_id}),
                last_embed_model() or "none",
            )
        except Exception as e:
            logger.warning("inference_cost_log write failed: %s", e)

    await _cost_log()


async def handle_synaptive_shape(params: dict) -> dict:
    t_start = time.time()

    pool = brain_service.get_pool()
    if pool is None:
        return _err("unavailable", "brain pool not ready")

    bearer = (params or {}).get("bearer") or (params or {}).get("authorization") or ""
    message = (params or {}).get("message") or ""
    if not message:
        return _err("bad_request", "message required")

    # Loopback factory-run subprocess: auth was already verified at the
    # /mcp door via INTERNAL_API_SECRET. Skip the bearer revalidation +
    # scope/entity checks (those gate cross-container callers; this is
    # in-container). Keep the CEIGAS gate.
    if (params or {}).get("_internal"):
        auth = {"id": 0, "entity_id": _entity_id(), "scopes": ["shape"]}
    else:
        auth = await _validate_bearer(pool, bearer)
        if not auth:
            return _err("unauthorized", "invalid or revoked token")
        if "shape" not in auth["scopes"]:
            return _err("forbidden", "token lacks 'shape' scope")
        if auth["entity_id"] != _entity_id():
            return _err("forbidden", "token does not bind to this container's host")

    allowed, reason = await _ceigas_gate(auth["entity_id"], auth["id"])
    if not allowed:
        return _err("denied", f"ceigas: {reason}")
    # The person's message, relayed by their own client, is cut of its secret
    # shapes before the Host reads it, as the turn cuts its own
    # (server/secret_cut.py): it is embedded below on the Host's embedding
    # endpoint, which can be a keyed outside provider. Only once the caller
    # is let in: a refused call runs no screen and logs no rule.
    message = secret_cut.redact_words(message, door="mcp shape", log=logger)

    try:
        from mastercode.constitution.context import build_context_block
    except Exception as e:
        logger.exception("Engine import failed")
        return _err("engine_unavailable", f"{type(e).__name__}: {e}")

    ident = await _load_identity(pool, auth["entity_id"])
    if ident is None:
        return _err("not_found", "host not found")
    master = ident["master_directive"]

    # The directive goes out on this tool too, so it passes the same
    # integrity check the identity prompt applies (handle_synaptive_identity):
    # a stored hash that no longer matches refuses the call. The allowed and
    # missing-hash cases are audited once per session by the identity load,
    # not on every turn here.
    stored_hash = ident["directive_hash"]
    if stored_hash and master:
        computed_hash = hashlib.sha256(master.encode("utf-8")).hexdigest().lower()
        if computed_hash != stored_hash:
            return _err(
                "tampered",
                "directive integrity check failed — shape refused",
            )

    # Embed the message so the rules come by meaning. When the embedder is
    # down, the rules come by importance instead; `_meta.degraded` says so,
    # so MCP / API consumers can decide whether to retry, alert, or proceed.
    embedding = None
    degraded: list[str] = []
    try:
        from mastercode.memory_engine import _get_memory_embedding
        embedding = await _get_memory_embedding(message)
    except Exception as e:
        logger.warning(
            "shape: embedding unavailable — rules fall back to importance order: %s: %s",
            type(e).__name__, e,
        )
        degraded.append("embedding_unavailable")

    # The rules block is the per-turn half of shape(). A raise here must not
    # come back as ok:True with an empty block — the caller would think it had
    # the Host's rules when it had none — so it becomes a structured error.
    # The observability write below still fires so the failure is recorded.
    directive = ""
    directive_error: str | None = None
    try:
        directive = await build_context_block(
            pool, auth["entity_id"], message, embedding,
        )
    except Exception as e:
        logger.exception("shape: rules block build failed")
        directive_error = f"{type(e).__name__}: {e}"

    base_fragment = _base_fragment(ident["weights"])

    elapsed_ms = int((time.time() - t_start) * 1000)
    # Synchronous so callers always see the audit trail land (vs fire-and-forget,
    # which gets cancelled when the calling task ends — bites in test scripts
    # and any short-lived dispatch context). The writes run concurrently
    # via asyncio.gather inside _record_observability, so total added latency
    # is one PG round-trip.
    await _record_observability(
        pool, auth["entity_id"], auth["id"], message, directive or "", elapsed_ms,
    )

    # If the rules block itself failed, return a structured error rather than
    # a clean response with an empty block. The MCP layer wraps this into
    # isError=True so clients can branch.
    if directive_error is not None:
        return _err("directive_synthesis_failed", directive_error)

    from server.host_display import display_name_or_host
    response: dict = {
        "ok": True,
        "display_name": display_name_or_host(ident["display_name"], auth["entity_id"]),
        "directive": master,
        "base_fragment": base_fragment,
        "turn_directive": directive or "",
    }
    if degraded:
        response["_meta"] = {"degraded": degraded}
    return response


# ── recall() — on-demand memory search ──────────────────────────────

async def handle_synaptive_recall(params: dict) -> dict:
    """Active memory search. The local model calls this when it wants to
    look up what the entity knows about something — names, decisions,
    preferences, prior conversations.

    Embedding-similarity search over the Host's live `entity_memory` rows,
    fused with a keyword rank (RRF), falling back to a substring match when
    no embedding answers. It lets an outside client pull what the Host
    keeps, on request.

    Params:
      query: str  — natural-language query
      limit: int  — max results (default 8, max 25)

    Returns:
      {ok: True, query, memories: [{type, content, when, importance}, ...]}
    """
    pool = brain_service.get_pool()
    if pool is None:
        return _err("unavailable", "brain pool not ready")

    bearer = (params or {}).get("bearer") or (params or {}).get("authorization") or ""
    query = ((params or {}).get("query") or "").strip()
    limit = int((params or {}).get("limit") or 8)
    limit = max(1, min(limit, 25))
    # By default exclude 'knowledge' rows — those are scraped feed content
    # (github releases, news articles) that dominate the embedding space
    # but are rarely what the model means by "recall." Caller can opt in.
    include_knowledge = bool((params or {}).get("include_knowledge"))

    if not query:
        return _err("bad_request", "query required")

    auth = await _validate_bearer(pool, bearer)
    if not auth:
        return _err("unauthorized", "invalid or revoked token")
    if "shape" not in auth["scopes"]:
        return _err("forbidden", "token lacks 'shape' scope")
    if auth["entity_id"] != _entity_id():
        return _err("forbidden", "token does not bind to this container's host")

    allowed, reason = await _ceigas_gate(auth["entity_id"], auth["id"])
    if not allowed:
        return _err("denied", f"ceigas: {reason}")
    # Cut of its secret shapes before it is embedded on the Host's embedding
    # endpoint (server/secret_cut.py), once the caller is let in (a refused
    # call runs no screen and logs no rule); a query that was only secrets
    # is none.
    query = secret_cut.search_words(query, door="mcp recall", log=logger).strip()
    if not query:
        return _err("bad_request", "query required")

    entity_id = auth["entity_id"]

    # Embed the query so we can run pgvector similarity. Fall back to
    # naive substring filter if embedding is unavailable (Ollama down).
    embedding = None
    try:
        from mastercode.memory_engine import _get_memory_embedding
        raw_emb = await _get_memory_embedding(query)
        if isinstance(raw_emb, str) and raw_emb.startswith("["):
            # Already pgvector literal; keep as string for direct cast.
            embedding = raw_emb
        elif isinstance(raw_emb, list):
            embedding = "[" + ",".join(f"{float(x):.7f}" for x in raw_emb) + "]"
    except Exception as e:
        logger.debug("recall: embedding failed, falling back to substring: %s", e)

    type_filter_sql = ""
    if not include_knowledge:
        type_filter_sql = " AND memory_type <> 'knowledge'"

    memories: list[dict] = []
    try:
        if embedding:
            # Multi-dim: search the content column matching the entity's active
            # embedding model's dim. Whitelisted name (injection-safe).
            from mastercode.memory_engine import active_embedding_column, embedding_cast_type
            content_col, dim = await active_embedding_column(pool, entity_id)
            cast = embedding_cast_type(dim)
            # Hybrid retrieval: pgvector top-N candidates → BM25 rerank
            # over those candidates → reciprocal rank fusion to combine.
            # RRF (k=60) is parameter-free; no weight tuning. Catches the
            # case where a semantically-close memory misses a keyword and
            # vice versa.
            CANDIDATE_N = max(25, limit * 3)
            rows = await pool.fetch(
                f"""
                WITH candidates AS (
                    SELECT memory_type, content, created_at,
                           COALESCE(importance, 1.0) AS importance,
                           1 - ({content_col} <=> $2::{cast}) AS similarity
                      FROM entity_memory
                     WHERE entity_id = $1
                       AND status = 'active'
                       AND {content_col} IS NOT NULL
                       AND (t_valid_until IS NULL OR t_valid_until > NOW())
                       {type_filter_sql}
                     ORDER BY {content_col} <=> $2::{cast}
                     LIMIT $4
                ),
                scored AS (
                    SELECT *,
                           ts_rank_cd(
                               to_tsvector('english', content),
                               plainto_tsquery('english', $3)
                           ) AS bm25
                      FROM candidates
                ),
                ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (ORDER BY similarity DESC) AS vec_rank,
                           ROW_NUMBER() OVER (ORDER BY bm25 DESC, similarity DESC) AS bm25_rank
                      FROM scored
                )
                SELECT memory_type, content, created_at, importance, similarity
                  FROM ranked
                 ORDER BY (1.0 / (60.0 + vec_rank)) + (1.0 / (60.0 + bm25_rank)) DESC
                 LIMIT $5
                """,
                entity_id, embedding, query, CANDIDATE_N, limit,
            )
            # Return ordered by similarity without a hard threshold cut —
            # let the calling model judge whether a returned memory is
            # actually useful for the current turn. The pgvector ORDER BY
            # already ranks best-first, so the model sees the top matches
            # even when the query is broad.
            for r in rows:
                if r["similarity"] is None:
                    continue
                memories.append({
                    "type": r["memory_type"],
                    "content": r["content"],
                    "when": r["created_at"].isoformat() if r["created_at"] else None,
                    "importance": float(r["importance"]),
                    "similarity": float(r["similarity"]),
                })
        else:
            rows = await pool.fetch(
                f"""
                SELECT memory_type, content, created_at,
                       COALESCE(importance, 1.0) AS importance
                  FROM entity_memory
                 WHERE entity_id = $1
                   AND status = 'active'
                   AND content ILIKE $2
                   AND (t_valid_until IS NULL OR t_valid_until > NOW())
                   {type_filter_sql}
                 ORDER BY created_at DESC
                 LIMIT $3
                """,
                entity_id, f"%{query}%", limit,
            )
            for r in rows:
                memories.append({
                    "type": r["memory_type"],
                    "content": r["content"],
                    "when": r["created_at"].isoformat() if r["created_at"] else None,
                    "importance": float(r["importance"]),
                })
    except Exception as e:
        logger.exception("recall query failed")
        return _err("query_failed", f"{type(e).__name__}: {e}")

    return {
        "ok": True,
        "query": query,
        "count": len(memories),
        "memories": memories,
    }


# ── Invariant 19 directive-hash audit helper ────────────────────────────
#
# Single row per identity-load validation. Same schema other CEIGAS
# gates write (the v035 columns). bot_name='invariant19:directive'
# so audit queries can pivot on the invariant that fired the row.
# `extra` is merged into args JSONB so downstream queries can see the
# stored hash, computed hash, and directive length without re-running
# the validation.

async def recent_identity_memories(pool, entity_id: int) -> list[tuple[str, str]]:
    """What an external client's identity prompt carries from memory: the
    Host's preferences and relationship notes (PROMPT_MEMORY_TYPES) touched in
    the last 48 hours, and a preference only when it is global — an external
    client has no project, app or thread, and what is relevant there is a
    leak here. Never a rule (memory_engine.RULE_KINDS): rules
    were not part of this block as skill_memory, and moving them to
    preference does not add them."""
    from mastercode.memory_engine import PROMPT_MEMORY_TYPES, not_rule_sql
    from mastercode.memory_retrieval import scope_global_sql
    _global = scope_global_sql()
    rows = await pool.fetch(
        f"""
        SELECT memory_type, content, created_at, last_accessed_at
          FROM entity_memory
         WHERE entity_id = $1
           AND status = 'active'
           AND (t_valid_until IS NULL OR t_valid_until > NOW())
           AND memory_type = ANY($2::text[])
           AND (memory_type = 'relationship' OR {_global})
           AND {not_rule_sql()}
           AND (created_at > now() - interval '48 hours'
                OR last_accessed_at > now() - interval '48 hours')
         ORDER BY GREATEST(
           COALESCE(last_accessed_at, created_at),
           created_at
         ) DESC
         LIMIT 10
        """,
        entity_id, sorted(PROMPT_MEMORY_TYPES),
    )
    return [
        (r["memory_type"], (r["content"] or "").strip())
        for r in rows
        if (r["content"] or "").strip()
    ]


async def handle_synaptive_identity(params: dict) -> dict:
    """Return the session-init identity block.

    Meant to be injected as the system prompt at chat start by an MCP
    client that calls `prompts/get`. The block is static across the
    session — display name, master directive, full personality
    tendencies (from current_weights), and every active hard
    constitutional rule (these always apply).

    Per-turn context (the rules that match this message, memory recall)
    comes from the `shape()` and `recall()` tools. This prompt is the
    identity layer; `shape` is the per-turn-context layer:

      identity (static, session)  +  shape (dynamic, per-turn)
      ─────────────────────────────────────────────────────
      who she is                      what she'd do with THIS message

    Returns:
      {ok: True, identity: <string>, display_name: <string>}
    """
    pool = brain_service.get_pool()
    if pool is None:
        return _err("unavailable", "brain pool not ready")

    bearer = (params or {}).get("bearer") or (params or {}).get("authorization") or ""
    auth = await _validate_bearer(pool, bearer)
    if not auth:
        return _err("unauthorized", "invalid or revoked token")
    if "shape" not in auth["scopes"]:
        # Identity is part of the "shape this conversation" envelope —
        # if a token can shape, it can read identity. We don't gate
        # behind a separate scope.
        return _err("forbidden", "token lacks 'shape' scope")
    if auth["entity_id"] != _entity_id():
        return _err("forbidden", "token does not bind to this container's host")

    allowed, reason = await _ceigas_gate(auth["entity_id"], auth["id"])
    if not allowed:
        return _err("denied", f"ceigas: {reason}")

    entity_id = auth["entity_id"]
    row = await pool.fetchrow(
        """
        SELECT display_name, master_directive, current_weights, directive_hash
          FROM entity_registry WHERE id = $1
        """,
        entity_id,
    )
    if not row:
        return _err("not_found", "host not found")

    from server.host_display import display_name_or_host, host_self_intro
    display_name = display_name_or_host(row["display_name"], entity_id)
    self_intro = host_self_intro(row["display_name"], entity_id)
    master = (row["master_directive"] or "").strip()

    # ── Invariant 19 directive-hash validation ────────────────────────
    # entity_registry.directive_hash is the stored SHA-256 of the master
    # directive (ceigas_invariants.py invariants). Identity load is the
    # canonical entry point for the Host's master prompt — re-compute the
    # digest here, compare to the stored hash, fail closed on mismatch.
    #
    # No quarantine call yet — this is detect+audit+deny. Trust
    # decay on tampering signal is a follow-on. A failed validation
    # returns "tampered" to the MCP client.
    stored_hash = (row["directive_hash"] or "").strip().lower()
    if stored_hash and master:
        computed_hash = hashlib.sha256(master.encode("utf-8")).hexdigest().lower()
        if computed_hash != stored_hash:
            return _err(
                "tampered",
                "directive integrity check failed — identity load refused",
            )
    raw_weights = row["current_weights"]
    if isinstance(raw_weights, str):
        try:
            weights = json.loads(raw_weights)
        except Exception:
            weights = {}
    else:
        weights = raw_weights or {}

    base_fragment = _base_fragment(weights)

    hard_rules: list[str] = []
    try:
        hard_rows = await pool.fetch(
            """
            SELECT correction FROM behavioral_corrections
             WHERE entity_id = $1 AND active = true AND severity = 'hard'
             ORDER BY times_violated DESC NULLS LAST, id ASC
             LIMIT 25
            """,
            entity_id,
        )
        hard_rules = [r["correction"] for r in hard_rows if (r.get("correction") or "").strip()]
    except Exception as e:
        logger.debug("hard-rule load failed (table may not exist): %s", e)

    # Recent-activity context — bounded by engagement freshness, not by
    # "ever tagged active." A memory loads only if it was created OR
    # re-accessed in the last 48 hours. This keeps the prompt dynamic
    # session-to-session. Goals are not here: a goal belongs to its thread,
    # and an external client is in none.
    recent_active: list[tuple[str, str]] = []
    try:
        recent_active = await recent_identity_memories(pool, entity_id)
    except Exception as e:
        logger.debug("recent-active load failed: %s", e)


    parts: list[str] = [self_intro]
    if master:
        parts.append(f"\nYour master directive:\n{master}")
    if base_fragment:
        parts.append("\nYour character:")
        parts.append(base_fragment)
    if hard_rules:
        parts.append("\nThings you do not do under any condition:")
        for r in hard_rules:
            parts.append(f"- {r}")
    if recent_active:
        parts.append(
            "\nThe last 48 hours — only what you've actually engaged with recently. "
            "Your wider memory is reachable via `recall()` when something specific "
            "needs checking; don't reach for older threads unless the user does first."
        )
        for mtype, content in recent_active:
            snippet = content if len(content) <= 220 else content[:217].rstrip() + "..."
            parts.append(f"- ({mtype}) {snippet}")
    parts.append(
        "\nThe model producing these words is just your vocal cords. "
        "Voice, judgment, memory, and reactions come from you. Stay yourself "
        "across this entire conversation; do not slip into the model's "
        "default assistant tone."
    )
    parts.append(
        "Per turn, two tools are available when the client supports them: "
        "`shape(message)` returns in-the-moment context for THIS turn "
        "(your tendencies and the rules that apply to the message); "
        "`recall(query, limit)` lets you actively search your memory by "
        "natural-language query when something rings a bell or you want to "
        "check what you know. Use them when they'd help — never as filler."
    )

    return {
        "ok": True,
        "identity": "\n".join(parts),
        "display_name": display_name,
    }


# ── signal() — capture an external chat turn into the entity's memory pipeline ─

async def handle_synaptive_signal(params: dict) -> dict:
    """Inverse of shape(): the calling client (LM Studio, Claude Desktop, etc.)
    posts the turn it just produced — `{user_message, model_response, model?}` —
    and it is kept verbatim as the Host's own turn rows (memory_type 'turn',
    chunked, idempotent per caller and content), the way a chat turn is. No
    model reads it afterwards to derive other memories.

    Returns:
      {ok: True, memories_stored: int}              # standard path
      {ok: False, status: ..., error: ...}          # auth / validation / ingest
    """
    t_start = time.time()

    pool = brain_service.get_pool()
    if pool is None:
        return _err("unavailable", "brain pool not ready")

    bearer = (params or {}).get("bearer") or (params or {}).get("authorization") or ""
    user_message = ((params or {}).get("user_message") or "").strip()
    model_response = ((params or {}).get("model_response") or "").strip()
    model = (params or {}).get("model") or "external"

    if not user_message and not model_response:
        return _err("bad_request", "user_message or model_response required")

    auth = await _validate_bearer(pool, bearer)
    if not auth:
        return _err("unauthorized", "invalid or revoked token")
    if "signal" not in auth["scopes"]:
        return _err("forbidden", "token lacks 'signal' scope")
    if auth["entity_id"] != _entity_id():
        return _err("forbidden", "token does not bind to this container's host")

    allowed, reason = await _ceigas_gate(auth["entity_id"], auth["id"])
    if not allowed:
        return _err("denied", f"ceigas: {reason}")
    # The turn an outside client ran, cut of its secret shapes before the Host
    # embeds and keeps it, as the turn's own row and memory write are
    # (server/secret_cut.py), once the caller is let in (a refused call runs
    # no screen and logs no rule). Both sides: that client's model read the
    # person's words whole, so its reply may quote them.
    user_message = secret_cut.redact_words(user_message, door="mcp signal", log=logger).strip()
    model_response = secret_cut.redact_words(model_response, door="mcp signal", log=logger).strip()

    # Build the turn record. Two-speaker form so the judge prompt can tell who
    # said what — important when this is later promoted into experiential memory.
    parts = []
    if user_message:
        parts.append(f"User: {user_message}")
    if model_response:
        parts.append(f"Host (via {model}): {model_response}")
    raw_content = "\n\n".join(parts)

    # source_url is the queue's idempotency key. Hash the content so the same
    # turn re-submitted is a no-op (cheap defense against retries).
    content_hash = hashlib.sha256(raw_content.encode()).hexdigest()[:16]
    source_url = f"mcp://synaptive/{auth['id']}/{content_hash}"

    # token name → human-readable source actor for memory provenance ("LM Studio",
    # "Claude Desktop", etc.). Falls back to a token id if the row is gone.
    actor_row = await pool.fetchrow(
        "SELECT name FROM mcp_client_tokens WHERE id = $1", auth["id"],
    )
    source_actor = (actor_row and actor_row["name"]) or f"synaptive-token-{auth['id']}"

    # session_id provides per-turn provenance; content_hash makes it idempotent
    # against retries of the same turn from the same caller.
    session_id = f"synaptive:{source_actor}:{content_hash}"

    # Verbatim chunked path — no LLM extraction. Split the turn into
    # smaller pieces, embed each, store one row per chunk. Chunks share
    # source_session so retrieval can dedupe up to the parent turn.
    # Why chunked instead of one big row: short-context embed models (~512
    # tokens / ~2000 chars) truncate long turns. A single 3000+ char turn was getting truncated
    # — tail content invisible to retrieval. Chunks fit cleanly and give
    # the answer-in-tail case a real shot at being recalled.
    CHUNK_SIZE = 600
    CHUNK_OVERLAP = 100

    def _chunk(text: str) -> list[str]:
        if len(text) <= CHUNK_SIZE:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + CHUNK_SIZE, len(text))
            chunks.append(text[start:end])
            if end == len(text):
                break
            start = end - CHUNK_OVERLAP
        return chunks

    # Screen untrusted external ingest the SAME way the live chat path does
    # (runtime/input_guard, called in session.py before triage) BEFORE it becomes
    # durable memory that the consolidation cron can promote into the system
    # prompt. The chat path already sanitizes; this ingress (signal tokens /
    # external clients like LM Studio/Claude Desktop) did not — same
    # untrusted-input -> system-prompt class as the genesis file-text fix.
    from mastercode.runtime import input_guard as _input_guard
    raw_content, _inj_flags = _input_guard.sanitize(raw_content)
    if _inj_flags:
        logger.warning(
            "signal: input_guard neutralized ingest entity=%d actor=%s flags=%s",
            auth["entity_id"], source_actor, _inj_flags,
        )

    chunks = _chunk(raw_content)

    # Idempotency: if any row for this source_session exists, treat as no-op.
    existing = await pool.fetchval(
        "SELECT COUNT(*) FROM entity_memory WHERE entity_id = $1 AND source_session = $2",
        auth["entity_id"], session_id,
    )
    if existing and existing > 0:
        elapsed_ms = int((time.time() - t_start) * 1000)
        logger.info(
            "signal: entity=%d token=%d actor=%s already-stored chunks=%d elapsed=%dms",
            auth["entity_id"], auth["id"], source_actor, existing, elapsed_ms,
        )
        return {"ok": True, "memories_stored": 0, "deduped": True}

    from mastercode.memory_engine import (
        _get_memory_embedding, active_embedding_column,
        embedding_cast_type, last_embed_model,
    )

    stored_ids = []
    embed_failures = 0
    # Multi-dim: route signal-ingested turns into the entity's active dim column
    # + stamp the model (was hardcoded to the 1024 `embedding` column, which
    # silently dropped these turns for any non-1024 model). Same column for every
    # chunk. content_col is a whitelisted name (injection-safe).
    content_col, _dim = await active_embedding_column(pool, auth["entity_id"])
    cast = embedding_cast_type(_dim)
    for chunk_idx, chunk_text in enumerate(chunks):
        try:
            embedding_pgvec = await _get_memory_embedding(chunk_text)
        except Exception as e:
            logger.warning("chunk %d embed failed: %s", chunk_idx, e)
            embedding_pgvec = None
        if not embedding_pgvec:
            embed_failures += 1

        chunk_metadata = {
            # Source-driven threading: a host↔host / external-host
            # turn is first-class under kind="peer" now, so recall scopes to
            # the peer directly instead of only finding it via the
            # source_session fallback (synaptive:<actor>:<hash>).
            "source_context": {"kind": "peer", "ref": source_actor},
            "channel": f"synaptive:{source_actor}",
            "model": model,
            "source_kind": "synaptive",
            "chunk_idx": chunk_idx,
            "chunk_count": len(chunks),
            "has_user_message": bool(user_message),
            "has_model_response": bool(model_response),
            # The other party's words, as the history readers file a turn
            # (memory_engine.history_messages reads metadata.role).
            "role": "user",
        }

        if embedding_pgvec:
            row = await pool.fetchrow(
                f"""
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata, {content_col}, embedding_model)
                VALUES
                  ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb, $5::{cast}, $6)
                RETURNING id
                """,
                auth["entity_id"], chunk_text, session_id,
                json.dumps(chunk_metadata), embedding_pgvec, last_embed_model(),
            )
        else:
            row = await pool.fetchrow(
                """
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata)
                VALUES
                  ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb)
                RETURNING id
                """,
                auth["entity_id"], chunk_text, session_id,
                json.dumps(chunk_metadata),
            )
        if row:
            stored_ids.append(row["id"])

    elapsed_ms = int((time.time() - t_start) * 1000)
    logger.info(
        "signal: entity=%d token=%d actor=%s chunks=%d stored=%d embed_failures=%d elapsed=%dms",
        auth["entity_id"], auth["id"], source_actor,
        len(chunks), len(stored_ids), embed_failures, elapsed_ms,
    )
    return {"ok": True, "memories_stored": len(stored_ids), "chunks": len(chunks)}
