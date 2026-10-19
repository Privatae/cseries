"""Invariant 9 — 10D personality validation at every write.

The CEIGAS invariants declare exactly 10 named personality dimensions on
every entity (warmth, directness, humor, formality, curiosity,
assertiveness, patience, vulnerability, energy, depth).
`validate_personality_weights` at ceigas_invariants.validate_entity_genesis
alone is never called from the sites that actually write
entity_registry.current_weights / seed_weights. The registered writer sites are:

  1. server/routes/genesis.py     — bootstrap upsert
  2. brain/mastercode/codebook.py — enrollment INSERT
  3. brain/mastercode/codebook.py — auto-reset UPDATE
  4. server/routes/brain.py       — codebook recommendation apply

(A 5th site — runtime/session.py update_entity_weights, the "drift
weight UPDATE" — was deleted: zero callers; stored
personality is a CONSTITUTION and changes ONLY via the governed
recommendation-apply path, site 4.)

This module gives all sites a single chokepoint. The chokepoint is
FAIL-CLOSED on writer identity too: a Host's weights once
drifted with NO audit attribution — an unregistered /
legacy writer bypassed the shape gate entirely. Every registered site
now calls guard_personality_write() before its INSERT/UPDATE; a write
whose `source` is not in REGISTERED_WRITER_SOURCES is DENIED with an
audit row invariant9:personality status=denied, exactly like a shape
violation. validate_weights_shape() remains available for pure shape
checks (recommendation producer, tests).

The Invariant 9s live in ceigas_invariants.INVARIANTS — same source
of truth the meta-tool gate uses for Invariant 1. Changing the dimension list
in ONE place propagates here.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("cseries.mastercode.personality_validate")


# ── Registered writer sites (fail-closed) ───────────────
#
# Every sanctioned writer of entity_registry.current_weights /
# seed_weights / envelope_bounds passes one of these `source` tags (an
# optional ":<detail>" suffix is tolerated). guard_personality_write()
# denies anything else. Each source is bound to the
# COLUMNS it may write — "envelope_bounds ONLY" is enforced, not a
# comment. Adding a writer (or widening its columns) is a deliberate
# act: change the call site AND this map in the same change.
#
# Retired tags: "routes.brain.codebook_apply", "codebook.enroll",
# "codebook._reset_dimensions" and "routes.brain.envelope_update"
# (the envelope and its auto-reset went with the simulated psyche:
# the sliders are the person's and nothing moves them).
REGISTERED_WRITER_SOURCES: dict = {
    # bootstrap upsert
    "routes.genesis._seed_codebook": frozenset({"seed_weights", "current_weights"}),
    # the user's direct sliders (Circles personality UI)
    "routes.brain.user_weights_set": frozenset({"seed_weights", "current_weights"}),
}


def _normalize_writer_source(source: Any) -> str:
    """Strip the optional ':<detail>' suffix off a writer source tag."""
    if not isinstance(source, str):
        return ""
    return source.split(":", 1)[0]


def is_registered_writer(source: Any) -> bool:
    return _normalize_writer_source(source) in REGISTERED_WRITER_SOURCES


async def guard_personality_write(
    pool,
    entity_id: Optional[int],
    *,
    source: str,
    column: str = "current_weights",
    weights: Any = None,
    detail: str = "",
) -> tuple[bool, list[str]]:
    """Fail-closed pre-write gate every registered writer site calls.

    Checks, in order:
      1. `source` is a registered writer tag (REGISTERED_WRITER_SOURCES,
         ':<detail>' suffix tolerated). Unregistered → DENY + audit.
      2. `column` is one the source is registered for —
         e.g. routes.brain.envelope_update may write envelope_bounds
         and nothing else. Off-map column → DENY + audit.
      3. `weights` matches the Invariant 9 10D shape. Violation →
         DENY + audit.

    Returns (ok, violations). ok=False means the caller MUST NOT perform
    the write; the denial is logged. (The per-write audit rows went with
    entity_behavioral_log: the only writers left are genesis and
    the person's own slider.)
    """
    if not is_registered_writer(source):
        vios = [
            f"unregistered writer source {source!r} — not in "
            f"REGISTERED_WRITER_SOURCES; write denied (fail closed)"
        ]
        logger.error(
            "invariant9:personality DENIED unregistered writer source=%r column=%s entity=%s",
            source, column, entity_id,
        )
        return False, vios

    allowed_columns = REGISTERED_WRITER_SOURCES[_normalize_writer_source(source)]
    if column not in allowed_columns:
        vios = [
            f"writer source {source!r} is not registered for column "
            f"{column!r} (allowed: {sorted(allowed_columns)}); write denied "
            f"(fail closed)"
        ]
        logger.error(
            "invariant9:personality DENIED off-map column source=%r column=%s entity=%s",
            source, column, entity_id,
        )
        return False, vios

    ok, vios = validate_weights_shape(weights, label=column)
    if not ok:
        return False, vios

    return True, []


def validate_weights_shape(weights: Any, *, label: str = "weights") -> tuple[bool, list[str]]:
    """Invariant 9 check on a personality weights dict.

    Returns (ok, violations). Violations is a list of human-readable
    strings; caller decides whether to deny-write, log-and-continue,
    or surface to the operator. `label` names the value in those
    strings — the guard passes the column (an envelope_bounds denial
    would otherwise audit in "weights" vocabulary, and the audit row is
    the post-incident artifact).

    Failures:
      - value is not a dict
      - wrong number of dimensions
      - missing or extra dimension names
      - any value non-numeric (bool is NOT numeric here — JSON true
        used to slip through because Python bool subclasses int)
      - any value outside [0.0, 3.0] (allowed range — values
        beyond 3.0 imply a tampered or corrupted write)
    """
    violations: list[str] = []

    if isinstance(weights, str):
        try:
            weights = json.loads(weights)
        except (json.JSONDecodeError, ValueError):
            return False, [f"{label} is malformed JSON string: {weights[:80]!r}"]

    if not isinstance(weights, dict):
        return False, [f"{label} must be dict, got {type(weights).__name__}"]

    try:
        from mastercode.ceigas_invariants import INVARIANTS
    except Exception as e:
        return False, [f"could not load invariants: {e}"]

    expected_count = INVARIANTS["personality_dimensions"]
    expected_dims = set(INVARIANTS["dimension_names"])
    actual_dims = set(weights.keys())

    if len(weights) != expected_count:
        violations.append(
            f"{label} has {len(weights)} dimensions, "
            f"the architecture spec requires exactly {expected_count} (Invariant 9)"
        )

    missing = expected_dims - actual_dims
    extra = actual_dims - expected_dims
    if missing:
        violations.append(f"missing spec-required dimensions: {sorted(missing)} (Invariant 9)")
    if extra:
        violations.append(f"unknown dimensions not in the spec: {sorted(extra)} (Invariant 9)")

    for dim, val in weights.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            violations.append(
                f"dimension {dim!r}: value must be numeric, got {type(val).__name__}"
            )
            continue
        if val < 0.0 or val > 3.0:
            violations.append(
                f"dimension {dim!r}: value {val} outside the allowed range [0.0, 3.0]"
            )

    return len(violations) == 0, violations


