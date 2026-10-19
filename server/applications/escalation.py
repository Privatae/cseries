"""
Escalation helper — entity-side `escalate()` that writes a ticket
directly to the local PG without going through the HTTP route.

Used by:
  * The entity runtime when it decides mid-response that it can't answer
    on its own authority (sales quote, confidential disclosure, anything
    outside the active share scope).
  * Inbound adapters (email/slack/circle dispatch handler in the
    container) that need to file a ticket before invoking the runtime,
    or instead of invoking it.

The function is a thin wrapper over an INSERT plus a pattern-match
check: if an active `decision_patterns` row matches the request, we
DON'T create a ticket — we record the auto-resolution and return
`{"auto_resolved": pattern_id, "decision": …}` so the caller can apply
the decision template without paging the handler.

This is where "less and less human-in-the-loop over time" actually
shows up.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from server import brain_service

log = logging.getLogger("privatae.pc.escalation")

VALID_SOURCES = {"circle_dispatch", "email", "slack", "manual", "runtime"}


# ---- Pattern matching ----

def _conditions_match(conditions: dict, context: dict) -> bool:
    """Conservative condition matcher.

    A pattern's `conditions` is a free-form dict the handler approved at
    crystallization time. We support a small set of predicates so the
    entity can grade incoming requests without an LLM round-trip:

      * exact match on top-level keys ("requester_kind": "prospect")
      * `<field>_max` numeric ceiling — the SUFFIX is "_max", read it as
        "value of <field> may be at most this". So to cap amount_usd at
        5000, write {"amount_usd_max": 5000}, NOT {"amount_max_usd": 5000}.
      * `<field>_min` numeric floor — same convention.
      * `scope_overlap_required`: list[str] — context.share_scope must
        cover every tag listed
      * `requires_keys`: list[str] — context must have non-empty values
        for each key

    Anything not listed above is treated as exact-equal on a top-level
    context key. Strict by default — a pattern only fires when every
    condition passes; ambiguous requests fall through to a fresh ticket.
    """
    if not isinstance(conditions, dict):
        return False
    for key, want in conditions.items():
        if key == "scope_overlap_required":
            ctx_scope = set(context.get("share_scope") or [])
            need = set(want or [])
            if not need.issubset(ctx_scope):
                return False
            continue
        if key == "requires_keys":
            for k in (want or []):
                if not context.get(k):
                    return False
            continue
        if key.endswith("_max"):
            base = key[: -len("_max")]
            v = context.get(base)
            try:
                if v is None or float(v) > float(want):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if key.endswith("_min"):
            base = key[: -len("_min")]
            v = context.get(base)
            try:
                if v is None or float(v) < float(want):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        # Default: exact-equal on top-level context key
        if context.get(key) != want:
            return False
    return True


async def _find_matching_pattern(
    pool, entity_id: int, circle_id: int | None,
    action_class: str, context: dict,
):
    """Return (pattern_row, decision_template) if an active pattern
    matches, else (None, None). Circle-scoped patterns beat global ones
    when both match."""
    rows = await pool.fetch(
        """SELECT id, circle_id, conditions, decision_template, observations
           FROM decision_patterns
           WHERE entity_id = $1 AND status = 'active'
             AND action_class = $2
             AND (circle_id = $3 OR circle_id IS NULL)
           ORDER BY (circle_id IS NULL) ASC, observations DESC""",
        entity_id, action_class, circle_id,
    )
    for row in rows:
        cond = row["conditions"]
        if isinstance(cond, str):
            try:
                cond = json.loads(cond)
            except Exception:
                cond = {}
        if _conditions_match(cond or {}, context):
            dt = row["decision_template"]
            if isinstance(dt, str):
                try:
                    dt = json.loads(dt)
                except Exception:
                    dt = {}
            return dict(row), dt
    return None, None


# ---- Public API ----

async def escalate(
    *,
    source: str,
    sitrep: str,
    options: list[dict],
    action_class: str | None = None,
    circle_id: int | None = None,
    requester: dict | None = None,
    context: dict | None = None,
    priority: int = 50,
    source_ref: str | None = None,
    expires_at: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a ticket — or auto-resolve via a learned pattern.

    Returns one of two shapes:

      Ticket created:
        { "ticket_id": int, "ticket_uuid": str, "status": "open",
          "auto_resolved": False }

      Auto-resolved by a decision pattern:
        { "ticket_id": int, "status": "auto_resolved",
          "auto_resolved": True, "pattern_id": int,
          "decision_template": {option_id, response_template, ...},
          "observations": int }

    The caller (runtime / dispatcher) should consume `decision_template`
    when `auto_resolved` is true and proceed without paging the handler.
    The auto_resolved ticket is logged so the handler can see what their
    entity decided on their behalf.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}")
    if not sitrep:
        raise ValueError("sitrep required")
    if not options:
        raise ValueError("options required")

    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    if pool is None or eid is None:
        raise RuntimeError("brain pool / host not initialized")

    requester = requester or {}
    context = context or {}
    metadata = metadata or {}

    # The same ask checks the route applies (2026-09-24): a credential ask
    # must be well-formed and must not name a key the vault already holds; a
    # phase link must be integers and must name a phase of THIS entity's own
    # project — a ticket joined to someone else's phase would be decided by a
    # landing the user never saw. TicketError -> ValueError, the error type
    # this function has always raised to its callers.
    from server.routes.tickets import TicketError, refuse_bad_asks
    try:
        await refuse_bad_asks(pool, eid, options=options, metadata=metadata)
    except TicketError as e:
        raise ValueError(e.detail) from None
    if metadata.get("phase_id") is not None:
        owned = await pool.fetchval(
            """SELECT 1 FROM project_phases ph JOIN projects p ON p.id = ph.project_id
                WHERE ph.id = $1 AND p.id = $2 AND p.entity_id = $3""",
            int(metadata["phase_id"]), int(metadata.get("project_id") or 0), eid)
        if not owned:
            raise ValueError("metadata.phase_id/project_id do not name a phase of one of "
                             "this host's own projects")

    # Pattern check — only if the caller named an action_class. The
    # entity classifies its escalations into action_class slots when it
    # creates them; that's how the handler's approved patterns line up.
    pattern_row = None
    pattern_template = None
    # A credential ask is never auto-resolved: a pattern's "done" would hand
    # the host a decision with nothing sealed in Keys. The user answers every
    # such card themselves (and decide never crystallises one, tickets.py).
    from server.routes.tickets import has_credential_ask
    if action_class and not has_credential_ask(options):
        pattern_row, pattern_template = await _find_matching_pattern(
            pool, eid, circle_id, action_class, context,
        )

    if pattern_row and pattern_template:
        # Auto-resolve. Record the ticket as `auto_resolved` so the
        # handler can audit what their entity decided. No human page.
        now = datetime.now(timezone.utc)
        row = await pool.fetchrow(
            """INSERT INTO entity_tickets
                  (entity_id, source, source_ref, circle_id, requester,
                   sitrep, options, context, status, priority,
                   decision, decided_by, decided_at, pattern_id,
                   expires_at, metadata)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb,
                       $8::jsonb, 'auto_resolved', $9, $10::jsonb,
                       $11, $12, $13, $14, $15::jsonb)
               RETURNING id, ticket_uuid""",
            eid, source, source_ref, circle_id,
            json.dumps(requester), sitrep, json.dumps(options),
            json.dumps(context), priority,
            json.dumps({
                "option_id": pattern_template.get("option_id"),
                "and_similar": True,
                "auto": True,
                "pattern_id": pattern_row["id"],
                "note": "auto-resolved by pattern",
            }),
            eid, now, pattern_row["id"], expires_at,
            json.dumps(metadata),
        )
        # Bump the pattern's observation counter
        await pool.execute(
            """UPDATE decision_patterns
                  SET observations = observations + 1,
                      last_used_at = NOW()
                WHERE id = $1""",
            pattern_row["id"],
        )
        log.info(
            "escalate auto-resolved entity=%d action=%s pattern=%d ticket=%d",
            eid, action_class, pattern_row["id"], row["id"],
        )
        return {
            "ticket_id": row["id"],
            "ticket_uuid": str(row["ticket_uuid"]),
            "status": "auto_resolved",
            "auto_resolved": True,
            "pattern_id": pattern_row["id"],
            "decision_template": pattern_template,
            "observations": int(pattern_row["observations"]) + 1,
        }

    # No matching pattern → page the handler.
    row = await pool.fetchrow(
        """INSERT INTO entity_tickets
              (entity_id, source, source_ref, circle_id, requester,
               sitrep, options, context, priority, expires_at, metadata)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb,
                   $9, $10, $11::jsonb)
           RETURNING id, ticket_uuid""",
        eid, source, source_ref, circle_id,
        json.dumps(requester), sitrep, json.dumps(options),
        json.dumps(context), priority, expires_at, json.dumps(metadata),
    )
    log.info(
        "escalate created ticket entity=%d source=%s action=%s ticket=%d",
        eid, source, action_class, row["id"],
    )
    return {
        "ticket_id": row["id"],
        "ticket_uuid": str(row["ticket_uuid"]),
        "status": "open",
        "auto_resolved": False,
    }
