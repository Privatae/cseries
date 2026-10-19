"""
Memory visibility / scope rules — CEIGAS-shaped gate at the prompt boundary.

Each `entity_memory` row optionally carries `metadata.visibility_scope` with
the shape:

    {
        "deny":  ["model:grok*", "external_api:*"],   # opt-out routes
        "allow": ["model:haiku*"]                      # if set, ONLY these allowed
    }

When a prompt is being assembled, the caller declares the consumer context
(typically `model:<id>`, optionally also `external_api:<svc>`, `tool:<name>`,
`network:<dest>` etc.). `is_allowed()` decides whether each retrieved memory
is allowed to surface for that consumer.

Defaults:
- A memory with NO visibility_scope is allowed for every consumer
  (allow-by-default; opt-out via deny list).
- `deny` wins over `allow` when both match — explicit denial is final.

Pattern syntax:
- Exact:           `model:grok-4-1-fast`
- Trailing wild:   `model:grok*`     (matches anything starting with `model:grok`)
- Whole-kind:      `model:*`          (matches any consumer of kind `model`)
- Universal:       `*`                 (matches any consumer)

This module is deliberately small and dependency-free: pure-Python, fast,
called per memory row at retrieval time. CEIGAS can wrap or re-use this
function later without changes to the storage layer.
"""
from __future__ import annotations

from typing import Iterable, Optional, Union

ConsumerSpec = Union[str, Iterable[str]]


def _match_pattern(pattern: str, consumer: str) -> bool:
    """Whether `pattern` matches `consumer`. Both are `kind:name` strings.

    Rules:
      - "*"          matches anything
      - "kind:*"     matches anything in that kind
      - "kind:foo*"  matches "kind:foobar", "kind:foo", etc.
      - exact match  matches itself
    """
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return consumer.startswith(prefix)
    return pattern == consumer


def _normalize_consumer(spec: Optional[ConsumerSpec]) -> list[str]:
    if not spec:
        return []
    if isinstance(spec, str):
        return [spec]
    return [s for s in spec if s]


def is_allowed(
    visibility_scope: Optional[dict],
    consumer: Optional[ConsumerSpec],
) -> bool:
    """
    Decide whether a memory with the given scope should surface for the given
    consumer (or list of consumers — denial against ANY is denial overall).

    Returns True if no scope is set, no consumer is declared, or the rules
    permit the consumer. Returns False if a deny pattern matches any consumer,
    or if `allow` is set and no allow pattern matches any consumer.
    """
    consumers = _normalize_consumer(consumer)
    if not visibility_scope or not consumers:
        return True

    deny = visibility_scope.get("deny") or []
    allow = visibility_scope.get("allow")

    # A malformed scope must NOT be silently treated as permissive. A `deny`/
    # `allow` that isn't a list of string patterns is unevaluable; raise so the
    # caller (filter_rows) fails closed and drops the row instead of iterating a
    # string char-by-char and matching nothing. (C27 fail-closed contract.)
    if not isinstance(deny, (list, tuple)) or any(not isinstance(p, str) for p in deny):
        raise ValueError("visibility_scope.deny must be a list of string patterns")
    if allow is not None and (
        not isinstance(allow, (list, tuple)) or any(not isinstance(p, str) for p in allow)
    ):
        raise ValueError("visibility_scope.allow must be a list of string patterns")

    # Deny is final: if ANY listed pattern matches ANY consumer, block.
    for c in consumers:
        for pat in deny:
            if _match_pattern(pat, c):
                return False

    # If allow is set (truthy list), require at least one allow pattern to
    # match at least one consumer.
    if allow:
        for c in consumers:
            for pat in allow:
                if _match_pattern(pat, c):
                    return True
        return False  # allow set but no match

    # No allow restriction → permit by default
    return True


def filter_rows(
    rows: list[dict],
    consumer: Optional[ConsumerSpec],
) -> list[dict]:
    """
    Filter a list of retrieved memory rows by visibility against `consumer`.
    Each row is a dict with `metadata` carrying the optional `visibility_scope`.

    Rows without metadata or without visibility_scope pass through.

    FAIL CLOSED (C27): a row whose scope evaluation RAISES (malformed
    visibility_scope — e.g. `deny` is a string, a non-str pattern) is DROPPED,
    not surfaced. A scope we cannot evaluate is treated as "do not surface to
    this consumer" rather than waved through. The drop is per-row, so one bad
    row never takes out the whole context.
    """
    if not consumer:
        return rows
    out = []
    for r in rows:
        try:
            meta = r.get("metadata") or {}
            scope = meta.get("visibility_scope") if isinstance(meta, dict) else None
            if is_allowed(scope, consumer):
                out.append(r)
        except Exception:
            # Unevaluable scope → drop the row (fail closed). Intentionally no
            # re-raise: a single malformed row must not surface and must not
            # abort filtering of the rest.
            continue
    return out
