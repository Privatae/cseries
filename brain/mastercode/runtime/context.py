"""
runtime/context.py — Context management utilities.

Contains functions for:
- Getting active session messages (used by brain loop for chat awareness)

These are stateless utility functions that operate on message arrays
and DB pools. They do not depend on EntityRuntime state.

REMOVED 2026-09-15 — the mid-turn "brain context synthesis" lane and
everything only it used: _brain_synthesize_context (a second inference on the
chat model, invisible to the context dial, that paraphrased raw tool results),
_compress_to_working_memory (deleted every tool_call/tool message from the
in-flight list and replaced them with that paraphrase, which also invalidated
the Anthropic cache prefix mid-turn), _check_context_pressure and
_estimate_token_count (its trigger, measured against a hardcoded 180000
regardless of the model's real window), and the two error-envelope helpers
that fed it. Overflow and runaway loops are covered by the per-round pre-send
trim (session.py _call_with_overflow_retry, now budget-aware with hysteresis),
the dedup cache and the dead-tool ledger (runtime/tool_failures.note_round).
Historical inference rows still carry source_module='brain_synthesis'; its
activity label stays in server/routes/inference.py so those rows keep reading
in plain language.
"""

import json
import logging
import re

log = logging.getLogger("cseries.mastercode.entity_runtime")

_SENTENCE_END_RE = re.compile(r'(?<=[.!?])\s')


# ---------------------------------------------------------------
# Shared context: brain loop reads active chat session
# ---------------------------------------------------------------

async def get_active_session_messages(
    pool,
    entity_id: int,
    max_messages: int = 10,
) -> list[dict]:
    """Return recent messages from the most recent active session for this entity.

    Used by brain_loop to gain awareness of what the entity is currently
    discussing in chat, enabling shared context between autonomous cognition
    and interactive conversation.
    """
    try:
        row = await pool.fetchrow(
            "SELECT messages FROM entity_sessions "
            "WHERE entity_id = $1 AND status IN ('active', 'idle') "
            "ORDER BY last_activity_at DESC NULLS LAST LIMIT 1",
            entity_id,
        )
        if not row or not row["messages"]:
            return []
        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)
        if not isinstance(messages, list):
            return []
        # Return last N messages (user + assistant only, skip system)
        chat_msgs = [
            m for m in messages
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        return chat_msgs[-max_messages:]
    except Exception as e:
        log.debug("get_active_session_messages failed (non-fatal): %s", e)
        return []
