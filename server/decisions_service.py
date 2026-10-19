"""
Decision-substrate SERVICE layer (extracted from server/routes/decisions.py).

Holds the resolve-decision business logic so BOTH the FastAPI route
(/api/decisions/{id}/resolve) and the chat meta-tool (decision_resolve)
call the same code path instead of the tool shimming the route.

The service:
  - takes (pool, entity_id, decision_id, chosen_key, chosen_rationale) and
    reads NOTHING from a Request;
  - holds every invariant the route held — entity-scoping (WHERE
    entity_id = $entity_id), the load+lock double-resolve guard, chosen_key
    validation, inbox read-marking, the cseries_proposal bridge relay, and
    the synthetic-user-turn dispatch;
  - raises DecisionResolveError(status, detail) for HTTP-ish failures and
    RETURNS the normal result dict on success.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from server import brain_service

logger = logging.getLogger("privatae.pc.decisions")


class DecisionResolveError(Exception):
    """HTTP-ish failure raised by the resolve service. The route maps
    .status/.detail onto HTTPException; the tool surfaces .detail."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


# The forwarded turns being followed to their end (a task nobody holds can be
# collected mid-turn).
_FOLLOWING: set = set()


async def forward_decision_turn(entity_id: int, session_id: str, composed: str,
                                decision_id: int) -> bool:
    """Start the Host's turn on a decided decision and follow it to its end,
    detached. True when the turn started. send_message is a stream (an async
    generator), not a coroutine: awaiting it raised every time, so a decision
    was never forwarded. Its first frame is read (a refusal is a 3: frame)
    and the rest followed, as session.answer_after_turn does. The turn lands
    in a topic: there is no original conversation."""
    try:
        agen = brain_service.send_message(
            entity_id=entity_id,
            session_id=session_id,
            message=composed,
        )
        first = await agen.__anext__()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "decisions.resolve: forward-turn dispatch failed (decision=%d session=%s): %s",
            decision_id, session_id, e,
        )
        return False
    if str(first or "").startswith("3:"):
        logger.warning("decisions.resolve: forward-turn refused (decision=%d): %s",
                       decision_id, str(first)[:200])
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _follow():
        try:
            async for _frame in agen:
                pass
        except Exception as e:  # noqa: BLE001
            logger.debug("decisions.resolve: following the turn ended early: %s", e)
    task = asyncio.ensure_future(_follow())
    _FOLLOWING.add(task)
    task.add_done_callback(_FOLLOWING.discard)
    return True


async def resolve_decision_service(
    pool,
    entity_id: int,
    decision_id: int,
    chosen_key: str,
    chosen_rationale: Optional[str] = None,
) -> dict:
    """User picks an option. Writes chosen_key + decided_at on the row,
    marks the inbox message as read, and dispatches a synthetic user turn
    back to the host so execution resumes from the conversational layer.

    The dispatched turn is plain natural language — the host treats it
    as ordinary user input. No new event type, no protocol change beyond
    the inbox kind extension.

    Scoped to `entity_id` (the caller resolves the pod's own entity via
    _self_entity_id): a host can only resolve its own queue.
    """
    # Load + lock to prevent double-resolve race.
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, entity_id, session_id, prompt, options, context, decided_at
                  FROM decisions
                 WHERE id = $1 AND entity_id = $2
                 FOR UPDATE
                """,
                decision_id, entity_id,
            )
            if not row:
                raise DecisionResolveError(404, "decision not found")
            if row["decided_at"] is not None:
                raise DecisionResolveError(409, "decision already resolved")

            opts = row["options"]
            if isinstance(opts, str):
                opts = json.loads(opts)
            valid_keys = {o.get("key") for o in opts}
            if chosen_key not in valid_keys:
                raise DecisionResolveError(
                    400,
                    f"chosen_key '{chosen_key}' not in options {sorted(valid_keys)}",
                )

            # Ship 4d-2: if pattern_id is NULL when this row resolves, it
            # BECOMES the exemplar of its (new) cluster — pattern_id = id.
            # Later propose calls with semantically similar prompts will
            # inherit this id via _find_pattern_neighbor.
            await conn.execute(
                """
                UPDATE decisions
                   SET chosen_key       = $2,
                       chosen_rationale = $3,
                       decided_at       = NOW(),
                       decided_by       = 'user',
                       pattern_id       = COALESCE(pattern_id, id)
                 WHERE id = $1
                """,
                decision_id, chosen_key, chosen_rationale,
            )

            # Mark the corresponding inbox message read. Match on payload.
            await conn.execute(
                """
                UPDATE inbox_messages
                   SET read_at = NOW()
                 WHERE message_kind = 'decision'
                   AND to_entity_id = $1
                   AND (payload->>'decision_id')::bigint = $2
                   AND read_at IS NULL
                """,
                entity_id, decision_id,
            )

    session_id = row["session_id"]
    chosen_label = next(
        (o.get("label") for o in opts if o.get("key") == chosen_key),
        chosen_key,
    )
    # Synthetic user turn — natural language so the host treats it as
    # ordinary input. Includes the original prompt for context in case
    # the session has scrolled far past the propose call.
    composed = (
        f"Decision on \"{row['prompt'].strip()[:200]}\": "
        f"I chose `{chosen_key}` ({chosen_label})."
    )
    if chosen_rationale:
        composed += f"\n\nMy reasoning: {chosen_rationale}"

    dispatched = False
    response = None
    if session_id:
        dispatched = await forward_decision_turn(entity_id, session_id, composed, decision_id)

    logger.info(
        "decisions.resolve: id=%d entity=%d chose=%s dispatched=%s",
        decision_id, entity_id, chosen_key, dispatched,
    )

    # The decision's context, parsed once for the post-commit handlers below.
    ctx_post = row["context"]
    if isinstance(ctx_post, str):
        try:
            ctx_post = json.loads(ctx_post)
        except Exception:
            ctx_post = {}

    # C-Series-proposal handler — the decision-ticket rail's deterministic
    # side. When this decision wraps a platform-change proposal, relay the
    # verdict to the platform via the bridge (platform.proposal_resolve →
    # controller). Deliberately code-driven, not model-driven: the state
    # change must not depend on the host acting on the synthetic turn.
    # Post-transaction: the local resolve is committed either way; a bridge
    # failure is surfaced so the UI can show the platform didn't hear it.
    proposal_resolve: Optional[dict] = None
    if (isinstance(ctx_post, dict) and ctx_post.get("cseries_proposal")
            and chosen_key in ("approve", "reject")):
        sp = ctx_post["cseries_proposal"]
        proposal_id = str(sp.get("proposal_id") or "")
        if proposal_id:
            verdict = "approved" if chosen_key == "approve" else "rejected"
            try:
                from server.main import get_bridge_client
                bc = get_bridge_client()
                if bc is None or not getattr(bc, "_connected", False):
                    raise RuntimeError("bridge not connected")
                envelope = await bc.request_full("platform.proposal_resolve", {
                    "proposal_id": proposal_id,
                    "verdict": verdict,
                    "note": chosen_rationale or "",
                    "decided_via": f"decision:{decision_id}",
                })
                if envelope.get("ok"):
                    proposal_resolve = envelope.get("data") or {"status": verdict}
                else:
                    proposal_resolve = {"error": envelope.get("error") or "resolve failed"}
            except Exception as e:
                logger.warning(
                    "decisions.resolve: proposal_resolve relay failed (decision=%d): %s",
                    decision_id, e,
                )
                proposal_resolve = {"error": str(e)}
            logger.info(
                "decisions.resolve: cseries proposal %s -> %s (decision=%d, relay=%s)",
                proposal_id, verdict, decision_id,
                "ok" if proposal_resolve and not proposal_resolve.get("error") else "FAILED",
            )

    return {
        "decision_id": decision_id,
        "chosen_key": chosen_key,
        "dispatched": dispatched,
        "session_id": session_id,
        "host_response": response if isinstance(response, dict) else None,
        "proposal_resolve": proposal_resolve,
    }
