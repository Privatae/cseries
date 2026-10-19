"""Invariant 19 extension to behavioral_corrections.

Final substrate-table closure of Invariant 19 (codebook tampering
detection). entity_registry.directive_hash, codebook_registry.codebook_digest
and codebook_params.row_digest are the other legs. This module is the last
one — behavioral_corrections.row_digest.

behavioral_corrections has 6 runtime INSERT sites (worker.py × 4,
teaching_deduplicator.py × 1, memory_handlers.py × 1). All re-route
through apply_behavioral_correction() so the digest is written
atomically with the INSERT. The accompanying UPDATE sites
(escalation, soft-undo) touch lifecycle-mutable fields that are
deliberately excluded from the digest field set, so they don't need
re-routing — their UPDATEs leave the digest intact.

Digested fields (immutable semantics):
  entity_id, role, correction, trigger_context, source_message,
  feedback_type, confidence, origin, codebook_id, created_at

NOT digested (lifecycle-mutable, modified by escalation / soft-undo /
embedder backfill):
  severity, times_violated, active, updated_at, trigger_embedding, id

Field set MUST match v041_behavioral_corrections_digest.sql byte-
for-byte. Changes here without updating the SQL backfill will cause
every load to record a mismatch on already-stored rows.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional


logger = logging.getLogger("cseries.mastercode.behavioral_correction_digest")


# The two canonical-form helpers below came from runtime/codebook_digest.py and
# runtime/codebook_param_digest.py, deleted with the codebook engine. They are
# kept verbatim here because every stored behavioral_corrections digest was
# computed with them.

def _pg_jsonb_canonical(obj: Any) -> str:
    """Match Postgres jsonb_build_object()::text byte-for-byte.

    Postgres jsonb stores object keys in **length-then-lexical** order
    (shorter keys first; ties broken lexically), and renders the text
    cast with `KEY: VALUE` separator (note the space after the colon)
    and `, ` between members. Nested jsonb objects follow the same
    rule recursively. Arrays preserve insertion order.

    Without matching this exact form, the SHA256 digest produced in
    Python diverges from the one Postgres backfilled via
    encode(digest(jsonb_build_object(...)::text, 'sha256'), 'hex')
    and every codebook would be flagged as tampered on first load.
    """
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, (int, float)):
        # Postgres renders integers without decimal point, floats with.
        if isinstance(obj, bool):  # bool is int subclass; already handled
            return "true" if obj else "false"
        # json.dumps handles both shapes identically to Postgres for
        # the numeric ranges we care about (small ints + sub-1.0 floats).
        return json.dumps(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, dict):
        # Postgres ordering: by key length asc, then lexical.
        items = sorted(obj.items(), key=lambda kv: (len(kv[0]), kv[0]))
        parts = [
            f"{json.dumps(k, ensure_ascii=False)}: {_pg_jsonb_canonical(v)}"
            for k, v in items
        ]
        return "{" + ", ".join(parts) + "}"
    if isinstance(obj, (list, tuple)):
        parts = [_pg_jsonb_canonical(v) for v in obj]
        return "[" + ", ".join(parts) + "]"
    # Fallback — coerce to string.
    return json.dumps(str(obj))


def _iso_timestamp(v: Any, *, legacy_offset: bool = False) -> Optional[str]:
    """Timestamptz → the string form these digests hash.

    The digested text is `jsonb_build_object(...)::text`, and **jsonb renders a
    timestamptz in full ISO 8601 with a `+00:00` offset** —
    `2026-05-15T04:05:05.950471+00:00`. Python's tz-aware
    `datetime.isoformat()` already matches it byte for byte, so nothing is
    trimmed. Return None for NULL so jsonb null and Python None align.

    TWO differences from a bare `datetime.isoformat()` matter, and both were
    getting rows rejected as tampered on a real Host (measured):

      1. **Offset.** jsonb keeps `+00:00`. The superseded code truncated it to
         `+00` on the belief that "Postgres uses +00" — true of
         `timestamptz::text`, false of the jsonb cast these digests hash. The
         comment was right about the wrong cast. This rejected EVERY row
         digested by the v040/v041 SQL backfills: 1391 of 1391
         behavioral_corrections rows, while a fresh SQL recomputation
         reproduced every stored digest exactly.
      2. **Fractional seconds.** Postgres TRIMS trailing zeros from the
         microsecond field (`...07.38858+00:00`, five digits, for a stored
         .388580) and omits the fraction entirely at .000000. Python's
         isoformat always emits six digits. That is a ~10% chance per row by
         digit distribution, and it accounted for the 132 of 1391 rows still
         rejected after (1) alone was fixed.

    `legacy_offset=True` BYTE-REPRODUCES the superseded helper, so digests
    written by the old Python path still validate. That means NEITHER of the
    two corrections above: no fraction trimming and a truncated `+00`, because
    that is precisely what the old code emitted. Applying the trimming here too
    (as the first cut of this fix did) reproduces a string that never existed —
    trimmed fraction plus `+00` — and rejects every legacy row whose microsecond
    happens to end in a zero, roughly one in ten. See validate_correction_row.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v
    iso = v.isoformat()
    if legacy_offset:
        # main's _iso_timestamp, verbatim: isoformat() untouched, `+00:00`
        # truncated to `+00`. Any divergence here silently un-validates real
        # rows, so this stays a copy rather than a variation on the live path.
        return iso[:-3] if iso.endswith("+00:00") else iso
    # Match Postgres' fractional-second rendering: trailing zeros trimmed, and
    # no fraction at all when it would be empty. isoformat() already omits the
    # fraction when microsecond == 0, so only the non-zero case needs work.
    if "." in iso:
        head, _, frac_and_offset = iso.partition(".")
        # The offset (if any) starts at the first '+' or '-' after the fraction.
        cut = len(frac_and_offset)
        for i, ch in enumerate(frac_and_offset):
            if ch in "+-":
                cut = i
                break
        frac, offset = frac_and_offset[:cut], frac_and_offset[cut:]
        frac = frac.rstrip("0")
        iso = f"{head}.{frac}{offset}" if frac else f"{head}{offset}"
    return iso



def compute_correction_digest(row: dict, *, legacy_offset: bool = False) -> str:
    """SHA256 over canonical jsonb shape of a behavioral_corrections row.

    Byte-identical to v041's backfill — same field set, same Postgres
    jsonb length-then-lexical key order, same NULL handling.

    `legacy_offset=True` reproduces the superseded truncated-timezone form;
    see _iso_timestamp and validate_correction_row."""
    payload = {
        "entity_id":       row.get("entity_id"),
        "role":            row.get("role"),
        "correction":      row.get("correction"),
        "trigger_context": row.get("trigger_context"),
        "source_message":  row.get("source_message"),
        "feedback_type":   row.get("feedback_type"),
        "confidence":      row.get("confidence"),
        "origin":          row.get("origin"),
        "codebook_id":     row.get("codebook_id"),
        "created_at":      _iso_timestamp(row.get("created_at"),
                                          legacy_offset=legacy_offset),
    }
    canonical = _pg_jsonb_canonical(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def apply_behavioral_correction(
    conn_or_pool,
    *,
    entity_id: int,
    correction: str,
    role: Optional[str] = None,
    trigger_context: str = "",
    source_message: str = "",
    feedback_type: Optional[str] = None,
    confidence: float = 0.5,
    severity: str = "gentle",
    origin: Optional[str] = None,
    codebook_id: Optional[int] = None,
    trigger_embedding: Optional[Any] = None,
    active: bool = True,
    times_violated: int = 0,
) -> dict:
    """Single chokepoint for behavioral_corrections INSERTs.

    INSERTs the row + writes its digest in two statements; safe to
    call against either a pool or an already-acquired connection
    (passes through to whichever exposes execute() / fetchrow()).

    Returns {id, digest, correction}. Caller may also pass `trigger_embedding`
    as a vector when available — it's not in the digest field set so
    a downstream embedder backfill (writing `trigger_embedding` after
    insertion) doesn't invalidate the digest.
    """
    # asyncpg's vector codec needs the embedding passed as the raw
    # type asyncpg expects — most callers already serialize via the
    # `vector` cast. Forward as-is; the INSERT statement below handles
    # the cast when embedding is provided vs omitted. The column + cast
    # are routed by the literal's dim (correction_embeddings) —
    # an unroutable dim stores no vector rather than mis-embedding.
    if trigger_embedding is not None:
        from mastercode.correction_embeddings import (
            trigger_cast_type, trigger_column_for)
        _col, _dim = trigger_column_for(trigger_embedding)
        if _col is None:
            trigger_embedding = None
    if trigger_embedding is not None:
        row = await conn_or_pool.fetchrow(
            f"""
            INSERT INTO behavioral_corrections
              (entity_id, role, correction, trigger_context, source_message,
               feedback_type, confidence, severity, {_col},
               active, times_violated, origin, codebook_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::{trigger_cast_type(_dim)},
                    $10, $11, $12, $13)
            RETURNING id, entity_id, role, correction, trigger_context,
                      source_message, feedback_type, confidence, origin,
                      codebook_id, created_at
            """,
            entity_id, role, correction, trigger_context, source_message,
            feedback_type, confidence, severity, trigger_embedding,
            active, times_violated, origin, codebook_id,
        )
    else:
        row = await conn_or_pool.fetchrow(
            """
            INSERT INTO behavioral_corrections
              (entity_id, role, correction, trigger_context, source_message,
               feedback_type, confidence, severity,
               active, times_violated, origin, codebook_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12)
            RETURNING id, entity_id, role, correction, trigger_context,
                      source_message, feedback_type, confidence, origin,
                      codebook_id, created_at
            """,
            entity_id, role, correction, trigger_context, source_message,
            feedback_type, confidence, severity,
            active, times_violated, origin, codebook_id,
        )

    row_dict = dict(row)
    digest = compute_correction_digest(row_dict)

    await conn_or_pool.execute(
        "UPDATE behavioral_corrections SET row_digest = $1 WHERE id = $2",
        digest, row["id"],
    )

    return {
        "id":         int(row["id"]),
        "digest":     digest,
        "correction": correction,
    }


def validate_correction_row(row: dict) -> tuple[bool, str]:
    """Returns (ok, reason). Called at read time in
    codebook_engine.load()'s rule-fetch loop. Mismatch → caller drops
    the row from cb._rules + emits an audit row.

    Legacy rows (row_digest IS NULL) return (True, "missing") so the
    caller can log a 'missing' audit row but still load them — same
    legacy semantic as the other digest legs.

    TWO canonical forms are accepted. The digested text is
    jsonb_build_object(...)::text, in which a timestamptz renders with a
    `+00:00` offset; the old _iso_timestamp truncated that to `+00`, so every
    row digested by the v041 SQL backfill failed this check while every row
    digested by the Python write path passed. Measured on one Host:
    1391 of 1391 rows carried the SQL form and were being DROPPED on every
    turn — the host's entire learned rule set, discarded as tampered while a
    fresh SQL recomputation reproduced every stored digest exactly.

    Accepting both forms does not weaken tamper detection: they are SHA256 over
    the same field set and the same content, so changing any digested field
    still fails both. Storage is never rewritten here, so no migration is
    needed on any entity; new writes use the jsonb form only."""
    stored = (row.get("row_digest") or "").strip().lower()
    if not stored:
        return (True, "missing")
    if compute_correction_digest(row) == stored:
        return (True, "match")
    if compute_correction_digest(row, legacy_offset=True) == stored:
        return (True, "match-legacy-offset")
    return (False, "mismatch")


async def audit_correction(
    pool,
    entity_id: Optional[int],
    *,
    row_id: Optional[int],
    correction_preview: Optional[str],
    codebook_id: Optional[int],
    status: str,
    detail: str,
    extra: Optional[dict] = None,
) -> None:
    """One log line per Invariant-19 behavioral_correction decision (the
    entity_behavioral_log row it wrote went with the table).

    status values:
      "allowed"  — match (heartbeat)
      "denied"   — mismatch, row dropped from cb._rules
      "missing"  — no stored digest; legacy row, backfill needed
      "applied"  — apply_behavioral_correction emitted this row when
                   it successfully wrote a new digest
    """
    args_payload: dict = {
        "detail":              detail,
        "row_id":              row_id,
        "codebook_id":         codebook_id,
        "correction_preview":  (correction_preview or "")[:120],
    }
    if extra:
        args_payload.update(extra)
    # The audit row this wrote went with entity_behavioral_log
    # (migrations/v237_dead_tables.sql); the decision is logged instead.
    log_fn = logger.warning if status == "denied" else logger.info
    log_fn("invariant19:rule %s host=%s row=%s: %s %s", status, entity_id, row_id, detail,
           json.dumps(args_payload, default=str)[:300])
