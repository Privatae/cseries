"""
Decision substrate routes (Ship 4d-1).

User > Host (option-builder) > User decides. Long-running project plans hit
ambiguities the host can't reason through confidently → host POSTs a decision
proposal → an inbox_messages row of kind='decision' lands → user reads the
inbox → user resolves → next-turn message dispatches back to the host and
execution resumes.

Surface choices intentionally minimal:
  - Decisions are an INBOX KIND, not a separate app. Inbox is already the
    cross-entity surface: same shape, same notification
    pipeline, same address book.
  - Resolution dispatches a synthetic user turn back through brain_service
    so the host treats the chosen option as plain conversational input. No
    new "decision_made" event type — the host's own session loop handles it.

Future ships (NOT in 4d-1):
  - 4d-2: pattern accrual — embed prompts, cluster decisions, expose
    pattern_id on new proposals so the host can pre-fill an "auto" choice.
  - 4d-3: auto-decision flow — when pattern confidence is high enough, host
    writes decided_by='auto' or 'pattern:<id>' AND fires a decision_review
    inbox message so the user can still confirm / refactor / rollback /
    update the codebook. Most-scrutinized data on the platform.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.pc.decisions")

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


# ── Payloads ──────────────────────────────────────────────────────────


class DecisionOption(BaseModel):
    """One option the host is offering. tradeoffs is free-form — anything
    that helps the user pick (cost, irreversibility flag, dependency
    summary, etc.). Renderer just shows label + rationale + tradeoffs."""
    key: str
    label: str
    rationale: Optional[str] = None
    tradeoffs: Optional[dict] = None


class ProposeRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    options: list[DecisionOption] = Field(..., min_length=2)
    context: Optional[dict] = None
    session_id: Optional[str] = None
    cross_entity_id: Optional[int] = None
    # Ship 4d-3: when set, the route auto-resolves at write time if the
    # pattern cluster is large enough AND unanimous. Set to None (default)
    # to never auto-resolve; set to N (e.g. 3) to allow auto when at least
    # N prior decisions all chose the same key for a semantically-similar
    # prompt. Similarity floor is _AUTO_SIM_THRESHOLD (env-tuneable).
    auto_if_unanimous_cluster_min: Optional[int] = None


class ResolveRequest(BaseModel):
    chosen_key: str = Field(..., min_length=1)
    chosen_rationale: Optional[str] = None
    # `decided_by` defaults to 'user' here. 'auto'/'pattern:*' is the
    # province of /propose_auto (Ship 4d-3).


class ReviewRequest(BaseModel):
    """Ship 4d-3: user verdict on an auto-resolved decision."""
    verdict: str = Field(..., pattern="^(confirmed|refactor|rollback|codebook_update)$")
    notes: Optional[str] = None
    # 'codebook_update' is still accepted because the decisions table's
    # CHECK constraint still lists it; it records the verdict and notes
    # only. The codebook_params/propose follow-up it once pointed at was
    # removed with the codebook engine (2026-10-17).


# ── Helpers ───────────────────────────────────────────────────────────


def _self_entity_id() -> Optional[int]:
    """The entity_id this container is bound to. Decisions are pod-local —
    every row is scoped to this container's own entity."""
    try:
        from server.synaptive_handler import _entity_id
        return _entity_id()
    except Exception as e:
        logger.warning("decisions: cannot resolve container entity_id: %s", e)
        return None


async def _write_inbox_for_decision(
    pool, *, entity_id: int, decision_id: int,
    prompt: str, options: list[dict], context: Optional[dict],
    cross_entity_id: Optional[int],
) -> Optional[str]:
    """Insert the corresponding inbox_messages row. Returns message_id."""
    # Subject: first line of prompt, truncated.
    subj = (prompt.strip().splitlines() or [""])[0][:120]
    if not subj:
        subj = "Decision needed"

    # Body: human-readable rendering. The dashboard renderer can also use
    # payload.options to render an interactive submit-button widget; this
    # body is the plaintext/markdown fallback.
    body_lines = [prompt.strip(), "", "**Options:**"]
    for opt in options:
        line = f"- `{opt['key']}` — {opt.get('label', '')}"
        if opt.get("rationale"):
            line += f": {opt['rationale']}"
        body_lines.append(line)
    body = "\n".join(body_lines)

    payload = {
        "decision_id": decision_id,
        "prompt": prompt,
        "options": options,
        "context": context or {},
        "renderer": "decision_proposal_v1",
    }
    if cross_entity_id is None:
        # The deep-link contract (server/deep_link.py): Circles, on this
        # decision. Only for a decision this host holds — the id is this
        # host's, and means nothing on another's.
        from server.deep_link import app_link
        payload["link"] = app_link("circles", f"decision:{decision_id}")

    # Self-addressed: inbox lives on this entity's pod and the user reads
    # from this entity's dashboard. Cross-host routing piggybacks the
    # cross_entity_id field — actual dispatch to the other entity's inbox
    # is out of 4d-1 scope (depends on inbox cross-entity flow maturity).
    from_address = f"entity:{entity_id}@host"
    to_address = f"entity:{entity_id}@user" if cross_entity_id is None else f"entity:{cross_entity_id}@user"
    to_entity = entity_id if cross_entity_id is None else cross_entity_id

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO inbox_messages
              (from_address, to_address,
               from_entity_id, to_entity_id,
               subject, body, body_format, message_kind, payload, delivered_at)
            VALUES
              ($1, $2, $3, $4, $5, $6, 'text/markdown', 'decision', $7::jsonb, NOW())
            RETURNING message_id::text
            """,
            from_address, to_address, entity_id, to_entity,
            subj, body, json.dumps(payload),
        )
        return row["message_id"] if row else None
    except Exception as e:
        logger.warning("decisions: inbox write failed for decision %d: %s", decision_id, e)
        return None


# ── Routes ────────────────────────────────────────────────────────────


# Ship 4d-2: similarity threshold for inheriting a sibling decision's
# pattern_id on propose. 0.80 is conservative — false-positive clustering
# would lump unrelated decisions together and mislead 4d-3 auto-confidence.
# Tuneable via env in case future operating experience suggests otherwise.
import os as _os_4d2
_PATTERN_SIM_THRESHOLD = float(_os_4d2.environ.get("DECISION_PATTERN_SIM_THRESHOLD", "0.80"))


async def _find_pattern_neighbor(pool, entity_id: int, prompt_embedding_str: str) -> Optional[dict]:
    """Find the nearest prior DECIDED decision for this entity. Returns
    None when there are no decided prior rows or the nearest is below the
    similarity threshold.

    Returns:
        {id, pattern_id, chosen_key, similarity, cluster_size}
        cluster_size = count of decided rows that share this pattern_id.
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT id,
                   COALESCE(pattern_id, id) AS pattern_id,
                   chosen_key,
                   1 - (prompt_embedding <=> $2::vector) AS similarity
              FROM decisions
             WHERE entity_id = $1
               AND decided_at IS NOT NULL
               AND prompt_embedding IS NOT NULL
             ORDER BY prompt_embedding <=> $2::vector
             LIMIT 1
            """,
            entity_id, prompt_embedding_str,
        )
    except Exception as e:
        logger.warning("decisions.propose: pattern neighbor lookup failed: %s", e)
        return None
    if not row or row["similarity"] is None:
        return None
    sim = float(row["similarity"])
    if sim < _PATTERN_SIM_THRESHOLD:
        return None
    # Count members in this cluster (decided rows with same pattern_id).
    cluster_size = await pool.fetchval(
        """
        SELECT COUNT(*) FROM decisions
         WHERE entity_id = $1
           AND decided_at IS NOT NULL
           AND COALESCE(pattern_id, id) = $2
        """,
        entity_id, row["pattern_id"],
    ) or 1
    return {
        "id": row["id"],
        "pattern_id": row["pattern_id"],
        "chosen_key": row["chosen_key"],
        "similarity": sim,
        "cluster_size": int(cluster_size),
    }


# Ship 4d-3: auto-pick threshold + minimum-unanimous-cluster-size knobs.
# Decoupled from the propose-side _PATTERN_SIM_THRESHOLD because auto-pick
# is a stricter promise — wrong auto-pick wastes a turn and trains a bad
# pattern. Defaults: sim ≥ 0.92, cluster ≥ 3 unanimous picks.
_AUTO_SIM_THRESHOLD = float(_os_4d2.environ.get(
    "DECISION_AUTO_SIM_THRESHOLD", "0.92",
))
_AUTO_MIN_CLUSTER = int(_os_4d2.environ.get(
    "DECISION_AUTO_MIN_CLUSTER", "3",
))


async def _cluster_chosen_key_distribution(pool, entity_id: int, pattern_id: int) -> dict[str, int]:
    """Return {chosen_key: count} across all decided rows in this cluster.
    A unanimous cluster has exactly one entry. Used to gate auto-pick —
    we refuse to auto-resolve when the cluster has historically been
    ambiguous (the user has picked different keys for similar prompts)."""
    rows = await pool.fetch(
        """
        SELECT chosen_key, COUNT(*) AS n
          FROM decisions
         WHERE entity_id = $1
           AND COALESCE(pattern_id, id) = $2
           AND decided_at IS NOT NULL
           AND chosen_key IS NOT NULL
         GROUP BY chosen_key
        """,
        entity_id, pattern_id,
    )
    return {r["chosen_key"]: int(r["n"]) for r in rows}


async def _write_decision_review_inbox(
    pool, *, entity_id: int, decision_id: int,
    prompt: str, options: list[dict], chosen_key: str, decided_by: str,
    pattern_id: int, similarity: float, cluster_size: int,
) -> Optional[str]:
    """Insert kind='decision_review' inbox row for an auto-resolved
    decision. Carries everything the user needs to confirm / refactor /
    rollback / codebook_update without leaving inbox."""
    subj = (prompt.strip().splitlines() or [""])[0][:120]
    if not subj:
        subj = "Auto-decision review"
    body_lines = [
        f"Auto-picked **`{chosen_key}`** based on {cluster_size} similar prior "
        f"decisions (similarity {similarity:.2f}).",
        "",
        f"> {prompt.strip()}",
        "",
        "**Options were:**",
    ]
    for opt in options:
        marker = "✓" if opt.get("key") == chosen_key else " "
        line = f"- [{marker}] `{opt['key']}` — {opt.get('label', '')}"
        if opt.get("rationale"):
            line += f": {opt['rationale']}"
        body_lines.append(line)
    body = "\n".join(body_lines)

    payload = {
        "decision_id": decision_id,
        "prompt": prompt,
        "options": options,
        "chosen_key": chosen_key,
        "decided_by": decided_by,
        "pattern_id": pattern_id,
        "similarity": round(similarity, 4),
        "cluster_size": cluster_size,
        "renderer": "decision_review_v1",
    }

    from_address = f"entity:{entity_id}@host"
    to_address = f"entity:{entity_id}@user"

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO inbox_messages
              (from_address, to_address,
               from_entity_id, to_entity_id,
               subject, body, body_format, message_kind, payload, delivered_at)
            VALUES
              ($1, $2, $3, $3, $4, $5, 'text/markdown',
               'decision_review', $6::jsonb, NOW())
            RETURNING message_id::text
            """,
            from_address, to_address, entity_id,
            subj, body, json.dumps(payload),
        )
        return row["message_id"] if row else None
    except Exception as e:
        logger.warning("decisions: decision_review inbox write failed for %d: %s", decision_id, e)
        return None


@router.post("/propose", dependencies=[Depends(requires_permission("write:decision"))])
async def propose_decision(req: ProposeRequest):
    """Host emits a decision proposal. Writes the decisions row and the
    corresponding inbox_messages row (kind='decision'). Returns the new
    decision_id so the caller can reference it in subsequent telemetry.

    The host SHOULD end its turn after this — the user is now in the
    loop. The response below is just a receipt.

    Ship 4d-2: also embeds the prompt + finds the nearest prior DECIDED
    sibling. If sim > threshold, inherits that sibling's pattern_id so
    future 4d-3 auto-decision logic can read cluster history. The
    response carries the prior-match info so the host can mention it in
    its own framing of the proposal (e.g., "I asked something similar
    before, and you chose X").
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="brain pool not ready")

    entity_id = _self_entity_id()
    if entity_id is None:
        raise HTTPException(status_code=500, detail="this container has no host id bound")

    opts_serialized = [o.model_dump() for o in req.options]

    # Ship 4d-2: embed the prompt. Embedding failure is non-fatal — the
    # row still gets written (host needs the decision_id back) but it
    # won't participate in pattern lookup until a future re-embed pass.
    prompt_embedding_str: Optional[str] = None
    try:
        from mastercode.memory_engine import _get_memory_embedding
        prompt_embedding_str = await _get_memory_embedding(req.prompt)
    except Exception as e:
        logger.warning("decisions.propose: prompt embed failed (non-fatal): %s", e)

    pattern_match: Optional[dict] = None
    if prompt_embedding_str:
        pattern_match = await _find_pattern_neighbor(pool, entity_id, prompt_embedding_str)

    inherited_pattern_id = pattern_match["pattern_id"] if pattern_match else None

    # Ship 4d-3: gate auto-resolve on cluster signal + unanimity.
    # - caller must opt in via auto_if_unanimous_cluster_min
    # - cluster_size >= that floor (default 3 per _AUTO_MIN_CLUSTER)
    # - similarity >= _AUTO_SIM_THRESHOLD (0.92 default)
    # - cluster's chosen_key distribution has exactly one entry (unanimous)
    auto_pick: Optional[dict] = None
    if (
        pattern_match
        and req.auto_if_unanimous_cluster_min is not None
        and pattern_match["similarity"] >= _AUTO_SIM_THRESHOLD
        and pattern_match["cluster_size"] >= max(
            int(req.auto_if_unanimous_cluster_min), _AUTO_MIN_CLUSTER,
        )
    ):
        dist = await _cluster_chosen_key_distribution(
            pool, entity_id, pattern_match["pattern_id"],
        )
        if len(dist) == 1:
            auto_key = next(iter(dist))
            valid_keys = {o.get("key") for o in opts_serialized}
            if auto_key in valid_keys:
                auto_pick = {
                    "chosen_key": auto_key,
                    "decided_by": f"pattern:{pattern_match['pattern_id']}",
                    "pattern_id": pattern_match["pattern_id"],
                    "similarity": pattern_match["similarity"],
                    "cluster_size": pattern_match["cluster_size"],
                }

    try:
        if auto_pick and prompt_embedding_str:
            # Auto-resolved branch: write decided_at + chosen_key + pattern decided_by.
            row = await pool.fetchrow(
                """
                INSERT INTO decisions
                  (entity_id, session_id, prompt, options, context,
                   cross_entity_id, prompt_embedding, pattern_id,
                   chosen_key, decided_at, decided_by)
                VALUES
                  ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7::vector, $8,
                   $9, NOW(), $10)
                RETURNING id, proposed_at, decided_at
                """,
                entity_id, req.session_id, req.prompt,
                json.dumps(opts_serialized),
                json.dumps(req.context or {}),
                req.cross_entity_id,
                prompt_embedding_str,
                inherited_pattern_id,
                auto_pick["chosen_key"],
                auto_pick["decided_by"],
            )
        elif prompt_embedding_str:
            row = await pool.fetchrow(
                """
                INSERT INTO decisions
                  (entity_id, session_id, prompt, options, context,
                   cross_entity_id, prompt_embedding, pattern_id)
                VALUES
                  ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7::vector, $8)
                RETURNING id, proposed_at
                """,
                entity_id, req.session_id, req.prompt,
                json.dumps(opts_serialized),
                json.dumps(req.context or {}),
                req.cross_entity_id,
                prompt_embedding_str,
                inherited_pattern_id,
            )
        else:
            row = await pool.fetchrow(
                """
                INSERT INTO decisions
                  (entity_id, session_id, prompt, options, context, cross_entity_id)
                VALUES
                  ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
                RETURNING id, proposed_at
                """,
                entity_id, req.session_id, req.prompt,
                json.dumps(opts_serialized),
                json.dumps(req.context or {}),
                req.cross_entity_id,
            )
    except Exception as e:
        logger.exception("decisions.propose insert failed: %s", e)
        raise HTTPException(status_code=500, detail=f"decisions insert failed: {e}")

    decision_id = row["id"]

    if auto_pick:
        message_id = await _write_decision_review_inbox(
            pool, entity_id=entity_id, decision_id=decision_id,
            prompt=req.prompt, options=opts_serialized,
            chosen_key=auto_pick["chosen_key"],
            decided_by=auto_pick["decided_by"],
            pattern_id=auto_pick["pattern_id"],
            similarity=auto_pick["similarity"],
            cluster_size=auto_pick["cluster_size"],
        )
    else:
        message_id = await _write_inbox_for_decision(
            pool,
            entity_id=entity_id,
            decision_id=decision_id,
            prompt=req.prompt,
            options=opts_serialized,
            context=req.context,
            cross_entity_id=req.cross_entity_id,
        )

    logger.info(
        "decisions.propose: id=%d entity=%d session=%s inbox=%s pattern=%s",
        decision_id, entity_id, req.session_id, message_id,
        inherited_pattern_id,
    )

    response = {
        "decision_id": decision_id,
        "proposed_at": row["proposed_at"].isoformat() if row["proposed_at"] else None,
        "inbox_message_id": message_id,
        "status": "auto_resolved" if auto_pick else "pending",
        "pattern_id": inherited_pattern_id,
    }
    if auto_pick:
        response["auto_pick"] = {
            "chosen_key": auto_pick["chosen_key"],
            "decided_by": auto_pick["decided_by"],
            "similarity": round(auto_pick["similarity"], 4),
            "cluster_size": auto_pick["cluster_size"],
        }
    if pattern_match:
        response["prior_decision"] = {
            "id": pattern_match["id"],
            "chosen_key": pattern_match["chosen_key"],
            "similarity": round(pattern_match["similarity"], 4),
            "cluster_size": pattern_match["cluster_size"],
        }
    return response


@router.post("/{decision_id}/resolve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def resolve_decision(decision_id: int, req: ResolveRequest):
    """User picks an option. Writes chosen_key + decided_at on the row,
    marks the inbox message as read, and dispatches a synthetic user turn
    back to the host so execution resumes from the conversational layer.

    The dispatched turn is plain natural language — the host treats it
    as ordinary user input. No new event type, no protocol change beyond
    the inbox kind extension.

    Thin adapter: pool/entity binding + body parse here; all the
    business logic + invariants live in
    decisions_service.resolve_decision_service.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="brain pool not ready")

    entity_id = _self_entity_id()
    if entity_id is None:
        raise HTTPException(status_code=500, detail="this container has no host id bound")

    from server import decisions_service
    try:
        return await decisions_service.resolve_decision_service(
            pool,
            entity_id,
            decision_id,
            req.chosen_key,
            req.chosen_rationale,
        )
    except decisions_service.DecisionResolveError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/{decision_id}/review",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def review_decision(decision_id: int, req: ReviewRequest):
    """Ship 4d-3: user verdict on an auto-resolved decision.

    Four verdicts:
      confirmed       — auto-pick was right. Strengthens the pattern.
      refactor        — auto-pick was workable but not the best fit;
                        suggests pattern should be split or refined.
      rollback        — auto-pick was wrong. Undo whatever the host
                        did with it; supersede the row so it doesn't
                        train future picks.
      codebook_update — legacy verdict, recorded with its notes only;
                        nothing acts on it.

    For 'rollback', t_valid_until is stamped so retrieval skips the row
    (matches Ship C supersession semantics). The pattern_id chain is
    preserved so the audit trail stays intact.

    Marks the kind='decision_review' inbox row as read regardless of
    verdict. Returns the updated decision shape for the renderer.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="brain pool not ready")

    entity_id = _self_entity_id()
    if entity_id is None:
        raise HTTPException(status_code=500, detail="this container has no host id bound")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, decided_at, decided_by, reviewed_at, chosen_key,
                       pattern_id, prompt
                  FROM decisions
                 WHERE id = $1 AND entity_id = $2
                 FOR UPDATE
                """,
                decision_id, entity_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="decision not found")
            if row["decided_at"] is None:
                raise HTTPException(
                    status_code=409,
                    detail="decision is still pending — use /resolve instead of /review",
                )
            if row["reviewed_at"] is not None:
                raise HTTPException(
                    status_code=409, detail="decision already reviewed",
                )
            if row["decided_by"] == "user":
                # User-resolved rows don't need review — they ARE the verdict.
                raise HTTPException(
                    status_code=409,
                    detail="decision was user-resolved; review is only for auto-picks",
                )

            await conn.execute(
                """
                UPDATE decisions
                   SET reviewed_at = NOW(),
                       review_verdict = $2,
                       review_notes = $3
                 WHERE id = $1
                """,
                decision_id, req.verdict, req.notes,
            )

            # Mark the decision_review inbox row read.
            await conn.execute(
                """
                UPDATE inbox_messages
                   SET read_at = NOW()
                 WHERE message_kind = 'decision_review'
                   AND to_entity_id = $1
                   AND (payload->>'decision_id')::bigint = $2
                   AND read_at IS NULL
                """,
                entity_id, decision_id,
            )

    logger.info(
        "decisions.review: id=%d entity=%d verdict=%s pattern=%s",
        decision_id, entity_id, req.verdict, row["pattern_id"],
    )

    return {
        "decision_id": decision_id,
        "verdict": req.verdict,
        "reviewed_at": "just-now",
        "pattern_id": row["pattern_id"],
        "chosen_key": row["chosen_key"],
    }


@router.get("/{decision_id}", dependencies=[Depends(requires_permission("read:decision"))])
async def get_decision(decision_id: int):
    """Full state of a single decision — for the inbox renderer + audit
    surfaces. No filtering; user can see everything on their own pod."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="brain pool not ready")

    entity_id = _self_entity_id()
    row = await pool.fetchrow(
        """
        SELECT id, entity_id, session_id, proposed_at, prompt, options,
               context, decided_at, chosen_key, chosen_rationale, decided_by,
               outcome, reviewed_at, review_verdict, review_notes,
               pattern_id, supersedes_decision_id, cross_entity_id
          FROM decisions
         WHERE id = $1 AND entity_id = $2
        """,
        decision_id, entity_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="decision not found")

    out = dict(row)
    for k in ("options", "context", "outcome"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                pass
    for k in ("proposed_at", "decided_at", "reviewed_at"):
        v = out.get(k)
        if v is not None and hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


@router.get("/pending/list",
            dependencies=[Depends(requires_permission("read:decision"))])
async def list_pending(limit: int = 50):
    """Decisions awaiting a user pick. Host can call this on cold-start
    to refresh its 'still-waiting' awareness. Inbox renderer can also
    call it to scope the decision feed."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="brain pool not ready")
    entity_id = _self_entity_id()
    limit = max(1, min(limit, 200))
    rows = await pool.fetch(
        """
        SELECT id, session_id, proposed_at, prompt, options
          FROM decisions
         WHERE entity_id = $1
           AND decided_at IS NULL
         ORDER BY proposed_at DESC
         LIMIT $2
        """,
        entity_id, limit,
    )
    out = []
    for r in rows:
        opts = r["options"]
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except Exception:
                opts = []
        out.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "proposed_at": r["proposed_at"].isoformat() if r["proposed_at"] else None,
            "prompt": r["prompt"],
            "options": opts,
        })
    return {"pending": out, "count": len(out)}
