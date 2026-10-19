"""
Memory scope — the audience-aware filter that closes the privacy loop for
"Entity as Endpoint".

The handler always sees the entity's full memory corpus. Every other
audience (a circle member, an external API caller, an email correspondent)
sees only the memories whose share-scope tags overlap with the audience's
active envelope.

Two pieces:

  * `Audience` — a small dataclass naming who's asking. Most callers
    construct it once at request entry; downstream code reads it.

  * `filter_clause(audience)` — returns a SQL `(fragment, params)` pair
    that callers compose into their `entity_memory` queries. Handler
    audiences get an always-true fragment; non-handler audiences get
    "the memory's metadata.share_scope must overlap with the allowed
    tags".

Memory tagging convention:

  Each memory row carries `metadata->'share_scope'` — a JSON array of
  free-form scope tags ("product_specs", "pricing_book", "personal", …).
  Memories WITHOUT a `share_scope` array are treated as **handler-only**:
  they only surface for the handler audience. This is the safe default —
  legacy memories stay private until explicitly tagged.

Wire-up plan:

  Phase 1 (this chunk): the helper module exists. `brain_service.search_memory`
  accepts an optional `audience` kwarg. The Circles route + circle
  dispatch handlers will start passing it.

  Phase 2 (next): plumb `audience` through `BotExecutor` context so the
  public `memory` tool the LLM uses applies the same filter inside a
  circle conversation. Until that lands, the LLM still sees the full
  corpus in circles — but external `search_memory` API calls already
  honor scope, and that's the larger leak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Audience:
    """Who's asking. Construct at request entry and thread through."""

    kind: str = "handler"
    # 'handler'  — the entity's own user (full visibility)
    # 'circle'   — a circle member is asking, filter by their share_scope
    # 'public'   — an unauthenticated public caller (most restrictive)
    # 'system'   — internal call from the entity's own runtime; same as
    #              handler for now, override later if we want stricter
    #              boundaries on background loops.

    caller_entity_id: int | None = None
    circle_id: int | None = None
    allowed_scopes: list[str] = field(default_factory=list)

    @property
    def is_handler(self) -> bool:
        return self.kind in ("handler", "system")


def filter_clause(
    audience: Audience,
    next_param_index: int,
) -> tuple[str, list]:
    """Build a SQL filter fragment + params for `entity_memory` queries.

    Returns `(sql_fragment, params)`. The fragment is intended to be
    appended to an existing WHERE clause:

        sql = "SELECT ... FROM entity_memory WHERE entity_id = $1"
        clause, extra = filter_clause(audience, next_param_index=2)
        if clause:
            sql += " AND " + clause
        rows = await pool.fetch(sql, entity_id, *extra)

    For handler audiences the function returns `("", [])` — no filter,
    everything visible.
    """
    if audience.is_handler:
        return "", []

    scopes = [s for s in (audience.allowed_scopes or []) if s]
    if not scopes:
        # Non-handler audience with no granted scopes sees nothing.
        return "FALSE", []

    # `metadata->'share_scope'` is expected to be a JSON array of strings.
    # `?|` returns true when any key in the right-hand text[] matches a
    # top-level key of the JSONB — for arrays that means "any element of
    # the right list appears in the array". This is the overlap predicate.
    placeholder = f"${next_param_index}"
    fragment = (
        f"(metadata ? 'share_scope' AND "
        f" jsonb_typeof(metadata->'share_scope') = 'array' AND "
        f" (SELECT bool_or(tag = ANY({placeholder}::text[])) "
        f"   FROM jsonb_array_elements_text(metadata->'share_scope') AS tag))"
    )
    return fragment, [list(scopes)]


def for_circle(
    caller_entity_id: int,
    circle_id: int,
    allowed_scopes: Iterable[str],
) -> Audience:
    """Convenience constructor for the circle-dispatch case."""
    return Audience(
        kind="circle",
        caller_entity_id=caller_entity_id,
        circle_id=circle_id,
        allowed_scopes=list(allowed_scopes or []),
    )


def for_handler() -> Audience:
    return Audience(kind="handler")
