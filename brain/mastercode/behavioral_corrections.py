"""
Behavioral Corrections — the read-only loader behind the training_memory tool.

Corrections are user-validated behavioral rules stored in the
behavioral_corrections table (Training Memory writes them). This module only
reads them for the tool, so the Host can say what it has learned; it injects
nothing. The live prompt path is constitution/context.py: hard rules by spike
keyword and role, rules of any severity at similarity >= 0.78, at most 3 per
turn. build_corrections_prompt below has no caller (prompt_schema records the
block's removal).
"""

import json
import logging

log = logging.getLogger("cseries.behavioral_corrections")


async def load_corrections(
    pool, entity_id: int, limit: int = 10,
    conversation_context: str = "", role: str | None = None,
) -> list[str]:
    """Load active behavioral corrections for this entity, for the
    training_memory tool to show. Nothing here reaches the system prompt
    (constitution/context.py is that path).

    Hard rules: the ones with no role (behavioral_corrections.role NULL)
    always, and a role's own when `role` names it. Soft rules: matched on
    whole words of the context (soft_keywords) against trigger_context,
    scoped by role the same way.
    """
    if not pool:
        return []

    # C8: full digest field set (+ row_digest, id) so each row read on the LIVE
    # prompt path is tamper-checked before injection. MUST match the field set
    # in runtime/behavioral_correction_digest.compute_correction_digest.
    _DIGEST_COLS = (
        "entity_id, role, correction, trigger_context, source_message, "
        "feedback_type, confidence, origin, codebook_id, created_at, "
        "row_digest, id"
    )

    rules = []
    try:
        # Hard rules: global (role IS NULL) always fire; role-scoped fire
        # only when their role matches the current session's role.
        if role:
            hard_rows = await pool.fetch(f"""
                SELECT {_DIGEST_COLS} FROM behavioral_corrections
                WHERE entity_id = $1 AND active = true AND severity = 'hard'
                  AND (role IS NULL OR role = $3)
                ORDER BY times_violated DESC
                LIMIT $2
            """, entity_id, limit, role)
        else:
            hard_rows = await pool.fetch(f"""
                SELECT {_DIGEST_COLS} FROM behavioral_corrections
                WHERE entity_id = $1 AND active = true AND severity = 'hard'
                  AND role IS NULL
                ORDER BY times_violated DESC
                LIMIT $2
            """, entity_id, limit)
        for r in hard_rows:
            if not await _correction_row_ok(pool, r):
                continue  # tamper mismatch — dropped + audited
            rules.append(r["correction"])

        # Keyword-matched soft rules (if we have conversation context).
        # Whole words of four letters or more: a substring match on the
        # first ten words let 'i' or 'a' match nearly every trigger_context
        # Role-scoped the way the hard rules are.
        if conversation_context and len(rules) < limit:
            remaining = limit - len(rules)
            keywords = soft_keywords(conversation_context)
            if keywords:
                conditions = " OR ".join(
                    f"trigger_context ~* ('\\m' || ${i+4} || '\\M')"
                    for i in range(len(keywords))
                )
                params = [entity_id, remaining, role] + keywords
                soft_rows = await pool.fetch(f"""
                    SELECT {_DIGEST_COLS} FROM behavioral_corrections
                    WHERE entity_id = $1 AND active = true AND severity != 'hard'
                      AND (role IS NULL OR role = $3)
                      AND ({conditions})
                    ORDER BY times_violated DESC
                    LIMIT $2
                """, *params)
                for r in soft_rows:
                    if not await _correction_row_ok(pool, r):
                        continue  # tamper mismatch — dropped + audited
                    if r["correction"] not in rules:
                        rules.append(r["correction"])

    except Exception as e:
        log.debug("Corrections load failed (table may not exist): %s", e)

    return rules


def soft_keywords(text: str, limit: int = 10) -> list[str]:
    """The words a soft rule's trigger_context is matched on: distinct
    lower-case words of letters and digits, four characters or more, the
    first `limit` of them. Only [a-z0-9] reach the regex, so none needs
    escaping."""
    import re
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) >= 4 and w not in out:
            out.append(w)
            if len(out) >= limit:
                break
    return out


async def _correction_row_ok(pool, row) -> bool:
    """Tamper-check a behavioral_corrections row on the live load path (C8).

    Routes the row through validate_correction_row; a digest MISMATCH drops the
    row (returns False) + emits an audit row. Legacy/un-backfilled rows
    (row_digest NULL, incl. globally-scoped codebook_id NULL rows) validate as
    'missing' → kept, matching codebook_engine.load semantics. A validator
    error fails closed (drop). Mirrors codebook_core._correction_row_ok."""
    try:
        from mastercode.runtime.behavioral_correction_digest import (
            validate_correction_row, audit_correction,
        )
    except Exception:
        return True  # digest substrate absent in this image — keep legacy behavior
    try:
        row_dict = dict(row)
        ok, reason = validate_correction_row(row_dict)
        if not ok:
            try:
                await audit_correction(
                    pool, row_dict.get("entity_id"),
                    row_id=row_dict.get("id"),
                    correction_preview=row_dict.get("correction"),
                    codebook_id=row_dict.get("codebook_id"),
                    status="denied",
                    detail=f"behavioral_corrections digest {reason} on live load — dropped",
                )
            except Exception as _ae:
                log.warning("correction tamper audit failed (non-fatal): %s", _ae)
            log.warning(
                "behavioral_corrections row id=%s digest %s on live load — DROPPED",
                row_dict.get("id"), reason,
            )
            return False
        return True
    except Exception as e:
        log.warning("correction digest validation errored — dropping row (fail closed): %s", e)
        return False


def build_corrections_prompt(corrections: list[str]) -> str:
    """Format corrections as a system prompt block."""
    if not corrections:
        return ""
    lines = ["## Behavioral Rules (from past feedback — follow these strictly)"]
    for c in corrections:
        lines.append(f"- {c}")
    return "\n".join(lines)
