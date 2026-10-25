"""
Tickets routes — entity-private escalation queue.

A ticket is the entity asking the handler to decide something it
won't decide on its own: a quote, a confidentiality call, an unfamiliar
inbound from a circle member, an email needing a human response, etc.

Lifecycle:
  1. Source emits a ticket. Sources today: `circle_dispatch` (entity
     escalated mid-conversation), `email`, `slack`, `manual` (handler
     created it themselves), `runtime` (catch-all for entity-internal
     escalations).
  2. Ticket lands in `entity_tickets` with a snapshot sitrep + options.
     Circles UI badges the originating circle (or the global queue).
  3. Handler picks an option. If `and_similar=true`, the ticket spawns
     a `decision_patterns` row so matching future requests auto-resolve.

Tickets live in the container's local PG. They are private to this
entity; the platform never sees ticket bodies.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from server import brain_service
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.pc.tickets")

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


VALID_SOURCES = {"circle_dispatch", "email", "slack", "manual", "runtime"}
VALID_STATUSES = {"open", "decided", "auto_resolved", "expired", "dismissed"}


# REMOVED — _close_linked_claude_session. Deciding or dismissing a
# ticket used to DELETE /system-apps/claude-code/sessions/by-id for a
# `claude_session_id` in the ticket's metadata. Nothing ever wrote that key,
# and the claude-code app no longer holds sessions: the route that started
# them (an agent run inside the Host's container) is retired.


def _entity_id() -> int | None:
    try:
        return brain_service._entity_id
    except Exception:
        return None


def _row_to_dict(row) -> dict:
    """Coerce asyncpg Record + jsonb columns into JSON-safe dicts. Postgres
    sometimes hands back jsonb as a string; normalize so callers don't have
    to guess."""
    out = dict(row)
    for k in ("requester", "options", "context", "decision", "metadata"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                pass
    for k in ("opened_at", "decided_at", "expires_at"):
        v = out.get(k)
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    if "ticket_uuid" in out and out["ticket_uuid"] is not None:
        out["ticket_uuid"] = str(out["ticket_uuid"])
    return out


# ---- List + filter ----

@router.get("", dependencies=[Depends(requires_permission("read:tickets"))])
async def list_tickets(
    status_filter: str | None = Query(None, alias="status"),
    circle_id: int | None = None,
    source: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    asked_in: str | None = None,
) -> dict:
    # Thin adapter: list/filter logic + entity-scoping lives in the service so the
    # `tickets` host tool shares it without faking an HTTP Request.
    # `asked_in`: one conversation's tickets — thread:<uuid>,
    # thread:original or project:<id> — the chat raises each open one as its
    # card (app-web-kit/ask.ts askFromTicket).
    pool = brain_service.get_pool()
    eid = _entity_id()
    try:
        return await list_tickets_service(
            pool, eid,
            status_filter=status_filter, circle_id=circle_id,
            source=source, limit=limit, offset=offset, asked_in=asked_in,
        )
    except TicketError as e:
        raise HTTPException(e.status, e.detail)


# ---- Badge counts for the sidebar ----

@router.get("/badge-counts", dependencies=[Depends(requires_permission("read:tickets"))])
async def badge_counts() -> dict:
    """Open ticket counts keyed by circle_id (and 'global' for nulls).
    The Circles UI uses this to draw badges next to each circle and a
    grand total on the Reviews tab."""
    pool = brain_service.get_pool()
    eid = _entity_id()
    try:
        return await badge_counts_service(pool, eid)
    except TicketError as e:
        raise HTTPException(e.status, e.detail)


# ---- Single ticket ----

@router.get("/{ticket_id}", dependencies=[Depends(requires_permission("read:tickets"))])
async def get_ticket(ticket_id: int) -> dict:
    pool = brain_service.get_pool()
    eid = _entity_id()
    try:
        return await get_ticket_service(pool, eid, ticket_id)
    except TicketError as e:
        raise HTTPException(e.status, e.detail)


# ---- Create ticket ----

@router.post("", dependencies=[Depends(requires_permission("write:tickets"))])
async def create_ticket(request: Request) -> dict:
    """Create a ticket. Body schema:
        source         required, one of VALID_SOURCES
        sitrep         required, plain-English dense brief
        options        required, list of {id, label, consequence,
                          recommend?, pattern_template?}
        circle_id      optional
        source_ref     optional (email-id, slack-ts, etc.)
        requester      optional jsonb (kind/handle/display_name)
        context        optional jsonb (pulled memory, attachments,
                          requirements package)
        priority       optional 0..100, default 50
        expires_at     optional ISO timestamp
        metadata       optional jsonb
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    eid = _entity_id()
    if eid is None:
        raise HTTPException(503, "host not initialized")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None

    # A caller-supplied metadata block may not claim the locks the platform's
    # own tickets carry (see _strip_platform_locks). Stripped, not refused.
    metadata = _strip_platform_locks(body.get("metadata") or {})
    try:
        return await create_ticket_service(
            pool, eid,
            source=body.get("source"), sitrep=body.get("sitrep") or "",
            options=body.get("options") or [],
            circle_id=body.get("circle_id"), source_ref=body.get("source_ref"),
            requester=body.get("requester") or {}, context=body.get("context") or {},
            priority=body.get("priority", 50), expires_at=body.get("expires_at"),
            metadata=metadata,
        )
    except TicketError as e:
        raise HTTPException(e.status, e.detail)


# The apps a PERSON decides tickets in: the Tickets pane (Circles) and the
# ask card over either chat (Circles, Workbench). A `user_only` ticket — the
# Host's own route change, a phase gate, a pipeline proposal — is the user's
# signature, so only these apps' tokens decide or dismiss it as the user.
# Any other holder of write:tickets (a community app declares it,
# build_contract.COMMUNITY_ALLOWED_SCOPES; the claude-code sidecar only reads)
# is an app, and the service refuses it on a user_only ticket as it refuses
# the Host.
PERSON_SURFACES = frozenset({"circles", "workbench"})


def _route_actor(request: Request) -> str:
    tok = getattr(request.state, "app_token", None)
    app_id = str(getattr(tok, "app_id", "") or "")
    return "user" if app_id in PERSON_SURFACES else f"app:{app_id or 'unknown'}"


# ---- Decide ----

@router.post("/{ticket_id}/decide", dependencies=[Depends(requires_permission("write:tickets"))])
async def decide_ticket(ticket_id: int, request: Request) -> dict:
    """Apply handler decision. Body schema:
        option_id              required, must match one of ticket.options[].id
        and_similar            bool, default false. If true → crystallize
                                into a decision_patterns row that auto-
                                resolves future matching tickets.
        note                   optional free text recorded on the decision
        pattern_action_class   optional override for the pattern taxonomy
                                slot (falls back to option.pattern_template
                                .action_class, else 'default')
        pattern_conditions     optional override for what conditions the
                                pattern matches (falls back to option's
                                pattern_template.conditions)
        stepup_code            the code from the user's authenticator, on a
                                pipeline proposal whose 'apply' adds a check
                                that runs outside the platform, and on a board
                                post card's 'Post it'
    """
    pool = brain_service.get_pool()
    eid = _entity_id()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    try:
        return await decide_ticket_service(
            pool, eid, ticket_id,
            option_id=body.get("option_id"),
            and_similar=bool(body.get("and_similar")),
            note=body.get("note", ""),
            pattern_action_class=body.get("pattern_action_class"),
            pattern_conditions=body.get("pattern_conditions"),
            seen_event_max=body.get("seen_event_max"),
            stepup_code=body.get("stepup_code"),
            actor=_route_actor(request),
        )
    except TicketError as e:
        raise HTTPException(e.status, e.detail)


# ---- Dismiss (close without a decision; doesn't create a pattern) ----

@router.post("/{ticket_id}/dismiss", dependencies=[Depends(requires_permission("write:tickets"))])
async def dismiss_ticket(ticket_id: int, request: Request) -> dict:
    """Close a ticket without picking an option. Useful when the request
    is no longer relevant or the handler wants to ignore it."""
    pool = brain_service.get_pool()
    eid = _entity_id()
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return await dismiss_ticket_service(
            pool, eid, ticket_id, note=(body or {}).get("note", ""),
            actor=_route_actor(request),
        )
    except TicketError as e:
        raise HTTPException(e.status, e.detail)


# ---- Decision-pattern listing (handler can see / pause / retire patterns) ----

@router.get("/_/patterns", dependencies=[Depends(requires_permission("read:tickets"))])
async def list_patterns(
    status_filter: str | None = Query(None, alias="status"),
    circle_id: int | None = None,
) -> dict:
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    eid = _entity_id()
    sql = [
        "SELECT id, pattern_uuid::text AS pattern_uuid, circle_id, action_class,",
        "       conditions, decision_template, status, observations,",
        "       approved_at, last_used_at, source_ticket_id",
        "FROM decision_patterns",
        "WHERE entity_id = $1",
    ]
    params: list[Any] = [eid]
    if status_filter:
        params.append(status_filter)
        sql.append(f"AND status = ${len(params)}")
    if circle_id is not None:
        params.append(circle_id)
        sql.append(f"AND circle_id = ${len(params)}")
    sql.append("ORDER BY observations DESC, last_used_at DESC NULLS LAST")
    rows = await pool.fetch(" ".join(sql), *params)

    def _norm(r):
        d = dict(r)
        for k in ("conditions", "decision_template"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
        for k in ("approved_at", "last_used_at"):
            v = d.get(k)
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        return d

    return {"patterns": [_norm(r) for r in rows]}


@router.post("/_/patterns/{pattern_id}/status", dependencies=[Depends(requires_permission("write:tickets"))])
async def set_pattern_status(pattern_id: int, request: Request) -> dict:
    """Pause / resume / retire a learned pattern. The entity stops
    auto-resolving against paused or retired patterns."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    eid = _entity_id()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    new_status = body.get("status")
    if new_status not in {"active", "paused", "retired"}:
        raise HTTPException(400, "status must be active|paused|retired")
    result = await pool.execute(
        "UPDATE decision_patterns SET status = $1 WHERE id = $2 AND entity_id = $3",
        new_status, pattern_id, eid,
    )
    if not result.endswith(" 1"):
        raise HTTPException(404, "pattern not found")
    return {"pattern_id": pattern_id, "status": new_status}


# ── Service layer ────────────────────────────────────────────────────
# The triage/escalation-queue logic extracted out of the routes so BOTH the
# /api/tickets routes AND the `tickets` host tool share ONE path with the SAME
# entity-scoping invariant (every read/write is `entity_id = $eid`, the host only
# ever touches its OWN queue). Reads NOTHING from a Request; raises
# TicketError(status, detail) for HTTP-ish failures, returns plain dicts otherwise.
# The route maps TicketError -> HTTPException; the tool reports .detail. Response
# shapes are UNCHANGED from the original inline routes. Placed ABOVE the routes;
# reuses the existing module-level _row_to_dict / VALID_SOURCES / VALID_STATUSES
# helpers.


class TicketError(Exception):
    """A ticket-queue failure the caller surfaces. `status` is the HTTP code the
    route maps it to; the `tickets` host tool just reports `detail`."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def list_tickets_service(
    pool, eid: int | None = None, *,
    status_filter: str | None = None,
    circle_id: int | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    asked_in: str | None = None,
) -> dict:
    """List/filter the host's escalation tickets — the source of truth for both
    GET /api/tickets and the `tickets` tool op='list'. Entity-scoped: only this
    entity's rows. Validates status/source against the allowed sets. Returns
    {\"tickets\": [...]}. No eid (entity not initialized) -> empty list, not an error.

    `asked_in` narrows to one conversation's tickets by the room recorded on
    them (metadata.asked_in, _room_filter): thread:<uuid>, thread:original
    (the original Circles conversation) or project:<id>."""
    if pool is None:
        raise TicketError(503, "brain pool down")
    if eid is None:
        return {"tickets": []}
    if status_filter and status_filter not in VALID_STATUSES:
        raise TicketError(400, f"status must be one of {sorted(VALID_STATUSES)}")

    sql = [
        "SELECT id, ticket_uuid, source, source_ref, circle_id, requester,",
        "       sitrep, options, context, status, priority,",
        "       decision, decided_at, pattern_id, opened_at, expires_at, metadata",
        "FROM entity_tickets",
        "WHERE entity_id = $1",
    ]
    params: list[Any] = [eid]
    if status_filter:
        params.append(status_filter)
        sql.append(f"AND status = ${len(params)}")
    if circle_id is not None:
        params.append(circle_id)
        sql.append(f"AND circle_id = ${len(params)}")
    if source:
        if source not in VALID_SOURCES:
            raise TicketError(400, f"source must be one of {sorted(VALID_SOURCES)}")
        params.append(source)
        sql.append(f"AND source = ${len(params)}")
    if asked_in:
        sql.append("AND " + _room_filter(asked_in, params))
    sql.append("ORDER BY priority DESC, opened_at DESC")
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 50
    if lim < 1:
        lim = 1
    if lim > 200:
        lim = 200
    sql.append(f"LIMIT {lim}")
    # A page past the first `limit`.
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0
    sql.append(f"OFFSET {off}")

    rows = await pool.fetch(" ".join(sql), *params)
    return {"tickets": [_row_to_dict(r) for r in rows], "offset": off,
            "next_offset": (off + len(rows)) if len(rows) == lim else None}


# How much of a sitrep one row of the Host's `tickets op='list'` carries. The
# list is for finding a ticket; op='get' reads one in full. Measured
# on one Host: 39 open decisions came back whole on every list call, 205,519
# characters of sitreps, 76k-253k characters a call, while the Host was only
# looking for a support draft that this list never contains.
SITREP_PREVIEW = 160


def ticket_summary(d: dict) -> dict:
    """One decision ticket as a list row: who/what/when, the first
    SITREP_PREVIEW characters of the sitrep (whitespace folded) and each
    option's id and label. Everything else stays on op='get'."""
    sitrep = str(d.get("sitrep") or "")
    folded = " ".join(sitrep.split())
    preview = folded if len(folded) <= SITREP_PREVIEW else folded[:SITREP_PREVIEW - 1].rstrip() + "…"
    options = []
    for o in d.get("options") or []:
        if isinstance(o, dict):
            options.append({"id": o.get("id"), "label": str(o.get("label") or "")[:SITREP_PREVIEW]})
    return {
        "id": d.get("id"), "status": d.get("status"), "source": d.get("source"),
        "priority": d.get("priority"), "opened_at": d.get("opened_at"),
        "sitrep": preview, "sitrep_chars": len(sitrep), "options": options,
    }


async def badge_counts_service(pool, eid: int | None = None) -> dict:
    """Open-ticket counts keyed by circle_id (and 'global' for nulls) plus a grand
    total — the source of truth for GET /api/tickets/badge-counts and the `tickets`
    tool op='badges'. Entity-scoped. No eid -> empty/zero, not an error."""
    if pool is None:
        raise TicketError(503, "brain pool down")
    if eid is None:
        return {"by_circle": {}, "total": 0}
    rows = await pool.fetch(
        """SELECT circle_id, COUNT(*)::int AS n
           FROM entity_tickets
           WHERE entity_id = $1 AND status = 'open'
           GROUP BY circle_id""",
        eid,
    )
    by_circle: dict[str, int] = {}
    total = 0
    for r in rows:
        key = "global" if r["circle_id"] is None else str(r["circle_id"])
        by_circle[key] = r["n"]
        total += r["n"]
    return {"by_circle": by_circle, "total": total}


async def get_ticket_service(pool, eid: int | None, ticket_id: int) -> dict:
    """Fetch ONE ticket by id — the source of truth for GET /api/tickets/{id} and
    the `tickets` tool op='get'. Entity-scoped: a ticket belonging to another
    entity reads as 404 (the WHERE clause never matches). Returns the row dict;
    raises TicketError(404) when not found."""
    if pool is None:
        raise TicketError(503, "brain pool down")
    row = await pool.fetchrow(
        """SELECT id, ticket_uuid, source, source_ref, circle_id, requester,
                  sitrep, options, context, status, priority,
                  decision, decided_by, decided_at, pattern_id,
                  opened_at, expires_at, metadata
           FROM entity_tickets WHERE id = $1 AND entity_id = $2""",
        ticket_id, eid,
    )
    if not row:
        raise TicketError(404, "ticket not found")
    return _row_to_dict(row)


async def create_ticket_service(
    pool, eid: int | None, *, source: str, sitrep: str, options: list,
    circle_id=None, source_ref=None, requester: dict | None = None,
    context: dict | None = None, priority: int = 50, expires_at=None,
    metadata: dict | None = None,
) -> dict:
    """Open a ticket — the source of truth for POST /api/tickets and for the
    host's own in-process callers (a route-change proposal from
    runtime/tools._host_route_write lands here). Same
    validation as the route; raises TicketError(400/503)."""
    if pool is None:
        raise TicketError(503, "brain pool down")
    if eid is None:
        raise TicketError(503, "host not initialized")
    if source not in VALID_SOURCES:
        raise TicketError(400, f"source must be one of {sorted(VALID_SOURCES)}")
    sitrep = (sitrep or "").strip()
    if not sitrep:
        raise TicketError(400, "sitrep required")
    if not isinstance(options, list) or len(options) == 0:
        raise TicketError(400, "options must be a non-empty list")
    for i, o in enumerate(options):
        if not isinstance(o, dict) or not o.get("id") or not o.get("label"):
            raise TicketError(400, f"option[{i}] needs at minimum {{id, label}}")
    await refuse_bad_asks(pool, eid, options=options, metadata=metadata)
    # Linked to the turn it was filed in — except another Host's support card
    # and an imported backlog ticket, which belong to no conversation here
    # however they arrived (a pull or an import run from a turn).
    if not ((isinstance(metadata, dict) and metadata.get("kind") == SUPPORT_KIND)
            or (requester or {}).get("kind") == "import"):
        metadata = with_turn_room(metadata)
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        raise TicketError(400, "priority must be 0..100")
    if not (0 <= priority <= 100):
        raise TicketError(400, "priority must be 0..100")
    row = await pool.fetchrow(
        """INSERT INTO entity_tickets
              (entity_id, source, source_ref, circle_id, requester, sitrep,
               options, context, priority, expires_at, metadata)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb,
                   $9, $10, $11::jsonb)
           RETURNING id, ticket_uuid, opened_at""",
        eid, source, source_ref, circle_id,
        json.dumps(requester or {}), sitrep, json.dumps(options),
        json.dumps(context or {}), priority, expires_at, json.dumps(metadata or {}),
    )
    log.info("ticket created id=%d source=%s circle=%s priority=%d",
             row["id"], source, circle_id, priority)
    return {
        "id": row["id"],
        "ticket_uuid": str(row["ticket_uuid"]),
        "opened_at": row["opened_at"].isoformat() if row["opened_at"] else None,
    }


async def _apply_route_change_proposal(proposal, *, option_id: str, ticket_id: int) -> dict:
    """The deterministic half of a host's route proposal (runtime/route_change):
    on 'apply', write the proposed route through the SAME writer
    the Orchestrator uses (inference.apply_role_route), stamped
    set_by='decision' with this ticket's id so the ledger row says the user
    approved it — which is what lets the host revert to it for free later.
    Any other option writes nothing. Never raises: a refused write (the
    provider lost its key since the proposal, say) comes back as
    {applied: False, error} and the ticket stays decided either way."""
    if option_id != "apply":
        return {"applied": False}
    route = (proposal or {}).get("route") if isinstance(proposal, dict) else None
    if not isinstance(route, dict) or not (route.get("role") and route.get("provider")
                                           and route.get("model")):
        return {"applied": False, "error": "proposal carries no complete route"}
    try:
        from server.routes import inference as inf
        body = inf.SetRoleModel(
            role=str(route["role"]), provider=str(route["provider"]), model=str(route["model"]),
            effort=(route.get("effort") or None),
            fallback_provider=(route.get("fallback_provider") or None),
            fallback_model=(route.get("fallback_model") or None))
        await inf.apply_role_route(body, set_by="decision", decision_id=int(ticket_id))
        log.info("ticket %d: route applied on the user's apply — %s -> %s/%s",
                 ticket_id, route["role"], route["provider"], route["model"])
        return {"applied": True}
    except Exception as e:  # noqa: BLE001
        detail = getattr(e, "detail", None) or str(e)
        log.warning("ticket %d: route apply refused: %s", ticket_id, detail)
        return {"applied": False, "error": str(detail)[:300]}


async def _apply_pipeline_proposal(pool, eid: int | None, md: dict, *, option_id: str,
                                   ticket_id: int, stepped_up: bool = False) -> dict:
    """The user's 'apply' on a pipeline proposal: write the proposed pipeline
    as projects.config.pipeline, beside `pipeline_decision` = this ticket, so
    the setting names the approval that made it. Re-validated here (the
    validator may have tightened since the draft), and COMPARE-AND-SET
    against the setting the draft was made from: if the project's pipeline
    changed in between, nothing is written and the result says so — an
    approval of one change never lands on top of another. Any option but
    'apply' writes nothing. Never raises.

    Checks that run outside the platform ride `pipeline_approval`: the rows
    the user's step-up covered, signed with the entity key. `stepped_up`
    (decide_ticket_service verified and burned the code) → every such row in
    the new setting is signed afresh. Without it, the approval on file must
    already cover every such row — then it is kept as it is — or nothing is
    written. A setting with none of them drops the approval."""
    if option_id != "apply":
        return {"applied": False}
    prop = md.get("pipeline_proposal") if isinstance(md.get("pipeline_proposal"), dict) else {}
    try:
        project_id = int(prop.get("project_id"))
    except (TypeError, ValueError):
        return {"applied": False, "error": "proposal names no project"}
    try:
        from mastercode.runtime import pipeline as _pl
        norm, errors = _pl.validate(prop.get("pipeline"))
        if norm is None:
            return {"applied": False,
                    "error": "the proposed pipeline no longer validates: " + "; ".join(errors)}
        base = prop.get("base")
        approval_sql, approval_arg = "", None
        if _pl.widen_rows(norm):
            # Who typed the code: this ticket, or — a change that only keeps
            # or removes approved rows — the ticket the approval came from.
            stepped_on = int(ticket_id)
            if not stepped_up:
                cfg = await pool.fetchval(
                    "SELECT config FROM projects WHERE id = $1 AND entity_id = $2",
                    project_id, eid)
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                if _pl.widens(norm, await _pl.load_approved(pool, eid, project_id, cfg)):
                    return {"applied": False, "error": (
                        "this pipeline has a check that runs outside the platform and your "
                        "authenticator code did not approve it — nothing was written; ask "
                        "the Host to propose it again")}
                stepped_on = int((cfg.get("pipeline_approval") or {}).get("ticket_id") or 0)
            # Signed over EXACTLY the rows the new setting keeps: a row this
            # change removes loses its approval with it, so adding it back
            # later takes a fresh code (as a removed egress allow would).
            from server.network import nac_stepup
            terms = _pl.widen_terms(project_id, norm)
            key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
            approval_sql = " || jsonb_build_object('pipeline_approval', $6::jsonb)"
            approval_arg = json.dumps({"terms": terms, "key_id": key_id, "signature": sig,
                                       "ticket_id": stepped_on, "applied_by": int(ticket_id)})
        else:
            approval_sql = " - 'pipeline_approval'"
        # Where the code lives changed: a standing review hold names a PR
        # number on the OLD forge, which means nothing (or another PR) on the
        # new one. It keeps holding, but forgets the number, so it re-targets
        # the open build PR where the code now lives (tools._active_review_hold).
        before = (_pl.validate(base)[0] if base is not None else None) or _pl.default_pipeline()
        if (norm.get("source") or {}) != (before.get("source") or {}):
            approval_sql += (" || CASE WHEN config ? 'review_hold' THEN jsonb_build_object("
                             "'review_hold', (config->'review_hold') || '{\"pr\": null}'::jsonb) "
                             "ELSE '{}'::jsonb END")
        result = await pool.execute(
            "UPDATE projects SET config = (jsonb_set(jsonb_set("
            "  COALESCE(config, '{}'::jsonb), '{pipeline}', $3::jsonb, true),"
            "  '{pipeline_decision}', to_jsonb($4::int), true)" + approval_sql + "),"
            "  updated_at = now() "
            "WHERE id = $1 AND entity_id = $2 "
            # JSON null reads as "no setting" (resolve() treats it so; the
            # propose side stored base=None for it) — NULLIF makes the
            # compare agree, or such a project could never be applied to.
            "AND NULLIF(config -> 'pipeline', 'null'::jsonb) IS NOT DISTINCT FROM $5::jsonb",
            project_id, eid, json.dumps(norm), int(ticket_id),
            json.dumps(base) if base is not None else None,
            *([approval_arg] if approval_arg is not None else []))
        if isinstance(result, str) and result.endswith(" 1"):
            log.info("ticket %d: pipeline applied on the user's apply — project %d%s",
                     ticket_id, project_id, " (stepped up)" if stepped_up else "")
            return {"applied": True, "project_id": project_id,
                    "stepped_up": stepped_up}
        return {"applied": False, "error": (
            "the project's pipeline changed after this was proposed (or the project is "
            "gone) — nothing was written; ask the Host to propose again")}
    except Exception as e:  # noqa: BLE001
        log.warning("ticket %d: pipeline apply failed: %s", ticket_id, e)
        return {"applied": False, "error": str(e)[:300]}


async def decide_ticket_service(
    pool, eid: int | None, ticket_id: int, *,
    option_id: str | None = None,
    and_similar: bool = False,
    note: str = "",
    pattern_action_class: str | None = None,
    pattern_conditions: Any = None,
    actor: str = "user",
    seen_event_max: Any = None,
    stepup_code: str | None = None,
    deliver_answer: bool = True,
) -> dict:
    """Apply a handler decision to an OPEN ticket — the source of truth for
    POST /api/tickets/{id}/decide and the `tickets` tool op='decide'. Entity-scoped
    throughout. Validates the chosen option against the ticket's own options;
    when and_similar=True, crystallizes a decision_patterns row that auto-resolves
    future matching tickets (the SAME upsert as the original route). Raises
    TicketError for missing input
    (400), unknown ticket (404), non-open status (409), or an option_id not in the
    ticket (400). Returns {ticket_id, decision, pattern_id}.

    `actor` is WHO is deciding: 'user' from the route (the person, in
    Circles), 'host' from the `tickets` tool. A ticket whose metadata says
    `user_only` — the host's own route-change proposal — is refused to the
    host (403): the host cannot answer its own question and have the ledger
    call it the user's approval. A ticket
    marked `no_pattern` never crystallises: and_similar is dropped, so no
    decision_patterns row can ever auto-resolve a route proposal.

    `seen_event_max` (a support card): the platform event mark
    the pane RENDERED. The collector refreshes an open support card while it
    sits on screen (a new message from the user); the decide is applied
    only if the card's mark is still the one the person read, else 409 and
    the pane reloads the card. Without it a stale screen would send a mark
    the person never saw and the platform would take that message in unread.
    Absent → no check (the host's own tools,
    older bundles).

    `stepup_code`: a pipeline proposal whose 'apply' adds a check
    that runs outside the platform, and a board post card's 'Post it', need a
    code from the user's authenticator.
    It is checked and burned here, BEFORE the status flips, so a wrong or
    missing code leaves the card open to try again; only the route passes
    one, and the ticket is user_only, so the host can never supply it.

    `deliver_answer` False: the PLATFORM is closing a card the person already
    answered somewhere else (resolve_phase_gate_tickets — the phase was
    approved in the Workbench, or on another card for the same phase). The
    ticket is decided and its apply runs, but no answer goes to the Host:
    the person said nothing on this card, and a turn started for it is one
    nobody asked for."""
    if pool is None:
        raise TicketError(503, "brain pool down")
    if not option_id:
        raise TicketError(400, "option_id required")

    ticket = await pool.fetchrow(
        """SELECT id, circle_id, status, options, metadata
           FROM entity_tickets WHERE id = $1 AND entity_id = $2""",
        ticket_id, eid,
    )
    if not ticket:
        raise TicketError(404, "ticket not found")
    if ticket["status"] != "open":
        raise TicketError(409, f"ticket status is {ticket['status']!r}, can't decide")

    md = ticket["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    if not isinstance(md, dict):
        md = {}
    if md.get("user_only") and actor != "user":
        raise TicketError(403, "this ticket is the user's to decide — it was filed by the host "
                               "and stays open until the user answers it in Circles")

    options = ticket["options"]
    if isinstance(options, str):
        options = json.loads(options)
    chosen = next((o for o in options if o.get("id") == option_id), None)
    if not chosen:
        raise TicketError(400, f"option_id {option_id!r} not in ticket options")

    # A messaging connector's pairing card: 'Let this account in' takes the
    # authenticator code, checked and burned before anything changes, and
    # approves in one transaction; "Don't pair" is free. nac_connector owns the
    # whole decision, closing the card itself, and honours it only for the ticket
    # the grant names. user_only is checked above.
    # It does not run the generic path below: the card is already closed by
    # the time decide_from_ticket returns (its compare-and-set would 409), and
    # a pairing card never crystallises a pattern. The person's answer reaches
    # the Host in the conversation that started the pairing (the grant's
    # asked_in), told by nac_connector's approve/decline themselves — the one
    # call that changes the grant — so the Network app's buttons tell it too,
    # and exactly once whichever surface answered. Not here as
    # well: that would be a second turn for one answer. A refused decide (a
    # wrong authenticator code, a replaced pairing) raises and tells nothing.
    if md.get("kind") == CONNECTOR_PAIRING_KIND:
        from server.network import nac_connector
        try:
            return await nac_connector.decide_from_ticket(
                pool, eid, ticket_id, md, option_id=option_id,
                stepup_code=stepup_code, actor=actor, note=note or "")
        except nac_connector.ConnectorError as e:
            raise TicketError(e.status, e.detail) from None

    # A card that asks for a credential never crystallises a pattern: a
    # pattern would auto-resolve the NEXT such ask as "done" with nothing
    # sealed (escalation.escalate skips patterns for the same reason).
    # A support card (another Host's ticket) is dropped by KIND as well as by
    # the no_pattern lock: the route strips caller locks, so a card that
    # reached the table through POST /api/tickets carries no lock, and a
    # pattern must never answer another Host's ticket on the operator's
    # behalf (measured in a container exercise:
    # pattern_id=1 on a route-made support card).
    and_similar = (bool(and_similar) and not md.get("no_pattern")
                   and md.get("kind") != SUPPORT_KIND
                   and not has_credential_ask(options))
    pattern_id: int | None = None
    # A support ticket's 'ask' / 'decline' IS the note the other Host's user
    # reads; without one there is nothing to send, so the card stays open
    # (refused here, before the status flips — a decided card with nothing
    # delivered would be the person's answer lost).
    if md.get("kind") == SUPPORT_KIND and option_id in SUPPORT_NOTE_REQUIRED \
            and not (note or "").strip():
        raise TicketError(400, f"'{option_id}' needs a note — it is what the user reads")
    # 'Tell them it's fixed' with no landed fix waiting to be told (the fix
    # was attempted again, or the user was already told): refused here, for
    # the same reason — the card stays open rather than closing on nothing.
    if md.get("kind") == SUPPORT_KIND and option_id == "fixed":
        from mastercode.runtime import support_collect as _sc
        try:
            _pid = int(md.get("platform_ticket_id"))
        except (TypeError, ValueError):
            _pid = None
        if await _sc._landed_fix(pool, eid, _pid) is None:
            raise TicketError(409, "there is no landed fix for this ticket that the user has "
                                   "not been told about — nothing was sent")

    # A pipeline widen: the step-up an egress
    # allow carries. The flag lives inside pipeline_proposal, which only the
    # platform's own `pipeline` tool writes (the POST route strips it).
    stepped_up = False
    if md.get("kind") == PIPELINE_PROPOSAL_KIND and option_id == "apply" \
            and (md.get("pipeline_proposal") or {}).get("requires_stepup"):
        from server.network import nac_stepup
        if actor != "user" or not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
            raise TicketError(403, "applying this adds a check that runs outside the platform, "
                                   "so it needs a current code from your authenticator (the "
                                   "second factor enrolled in Network). Nothing was changed — "
                                   "enter a fresh code and apply again.")
        stepped_up = True

    # A board post the Host wrote (runtime/board_post_approval): 'Post it'
    # puts text every member of the network reads, under this Host's name, so
    # it takes the person's second factor on the card, exactly as a pipeline
    # widen's 'apply' does: the code from their authenticator, checked and
    # burned here BEFORE the status flips, so a missing, wrong, stale or
    # replayed code leaves the card open and sends nothing. Only the route
    # passes a code, and the card is user_only, so the Host never supplies one.
    if md.get("kind") == BOARD_POST_PROPOSAL_KIND and option_id == BOARD_POST_STEPUP_OPTION:
        from server.network import nac_stepup
        if actor != "user" or not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
            raise TicketError(403, BOARD_POST_STEPUP_REFUSED)

    decision = {
        "option_id": option_id,
        "option_label": chosen.get("label", ""),
        "and_similar": and_similar,
        "note": note or "",
    }
    # Compare-and-set FIRST: only the ONE decide that flips 'open' to
    # 'decided' proceeds to anything that writes — the pattern upsert below
    # and the route apply further down. The status check above is a read;
    # two concurrent decides (a double-click on 'apply') both pass it, and
    # without this predicate both would apply a route proposal — a duplicate
    # ledger row — and the loser could still
    # leave a pattern upsert behind. The loser gets the same 409 a late
    # decide gets, and writes nothing.
    seen = None
    if md.get("kind") == SUPPORT_KIND and seen_event_max is not None:
        try:
            seen = int(seen_event_max)
        except (TypeError, ValueError):
            raise TicketError(400, "seen_event_max must be an integer")
    if seen is None:
        result = await pool.execute(
            """UPDATE entity_tickets
                  SET status = 'decided',
                      decision = $1::jsonb,
                      decided_by = $2,
                      decided_at = NOW()
                WHERE id = $3 AND status = 'open'""",
            json.dumps(decision), eid, ticket_id,
        )
    else:
        # The flip happens only if the card still shows what the person read
        # (the collector's refresh moves platform_event_max and the sitrep
        # together) — the mark the verdict then carries is exactly theirs.
        result = await pool.execute(
            """UPDATE entity_tickets
                  SET status = 'decided',
                      decision = $1::jsonb,
                      decided_by = $2,
                      decided_at = NOW()
                WHERE id = $3 AND status = 'open'
                  AND COALESCE((metadata->>'platform_event_max')::int, 0) = $4""",
            json.dumps(decision), eid, ticket_id, seen,
        )
    if isinstance(result, str) and not result.endswith(" 1"):
        if seen is not None:
            still = await pool.fetchval(
                "SELECT status FROM entity_tickets WHERE id = $1 AND entity_id = $2", ticket_id, eid)
            if still == "open":
                raise TicketError(409, "the user wrote since you opened this card — it has been "
                                       "refreshed; read it again and decide")
            raise TicketError(409, f"ticket status is {still!r} — it was decided by another request")
        raise TicketError(409, "ticket was decided by another request, can't decide")

    if and_similar:
        template = chosen.get("pattern_template") or {}
        action_class = (
            pattern_action_class
            or template.get("action_class")
            or "default"
        )
        conditions = pattern_conditions or template.get("conditions") or {}
        decision_template = {
            "option_id": option_id,
            "option_label": chosen.get("label", ""),
            "response_template": chosen.get("consequence", ""),
        }
        pat = await pool.fetchrow(
            """INSERT INTO decision_patterns
                  (entity_id, circle_id, action_class, conditions,
                   decision_template, source_ticket_id, approved_by)
               VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)
               ON CONFLICT (entity_id, circle_id, action_class) DO UPDATE
                   SET conditions        = EXCLUDED.conditions,
                       decision_template = EXCLUDED.decision_template,
                       source_ticket_id  = EXCLUDED.source_ticket_id,
                       observations      = decision_patterns.observations + 1,
                       last_used_at      = NOW(),
                       status            = 'active'
               RETURNING id""",
            eid,
            ticket["circle_id"],
            action_class,
            json.dumps(conditions),
            json.dumps(decision_template),
            ticket_id,
            eid,
        )
        pattern_id = pat["id"]
        await pool.execute(
            "UPDATE entity_tickets SET pattern_id = $1 WHERE id = $2",
            pattern_id, ticket_id,
        )

    log.info(
        "ticket %d decided option=%s and_similar=%s pattern_id=%s",
        ticket_id, option_id, and_similar, pattern_id,
    )
    out = {
        "ticket_id": ticket_id,
        "decision": decision,
        "pattern_id": pattern_id,
    }
    # A route-change proposal: the platform applies the route on the user's
    # 'apply' — after the ticket is marked decided, so a retry cannot apply
    # twice (a decided ticket is refused above), and never by handing the
    # answer to the model and hoping it sets the route (it would re-propose).
    if md.get("route_change_proposal"):
        out["route_apply"] = await _apply_route_change_proposal(
            md["route_change_proposal"], option_id=option_id, ticket_id=ticket_id)
    # A phase gate: the user's 'approve' on the ticket IS the
    # signature the Workbench approve route writes — same column, same
    # COALESCE, after the ticket is decided so a retry cannot re-sign. Any
    # other option writes nothing; the phase stays gated and the note says why.
    if md.get("kind") == PHASE_GATE_KIND:
        out["phase_approve"] = await _apply_phase_gate_decision(
            pool, eid, md, option_id=option_id, ticket_id=ticket_id)
    # A pipeline proposal: the user's 'apply' writes the setting,
    # after the decide for the same reason as the two above.
    if md.get("kind") == PIPELINE_PROPOSAL_KIND:
        out["pipeline_apply"] = await _apply_pipeline_proposal(
            pool, eid, md, option_id=option_id, ticket_id=ticket_id, stepped_up=stepped_up)
        # On the ticket, so the outcome is never only in this response: the
        # card's click reports "I chose Apply" either way, and the Host's
        # pipeline(op='status') reads the result back from here.
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('pipeline_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["pipeline_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: pipeline apply result not recorded: %s", ticket_id, e)
    # A turn type card: the person's click is the
    # ONLY writer of a type's status and profile — after the decide, for the
    # same reason as the three above, and recorded on the ticket.
    if md.get("kind") == TURN_TYPE_PROPOSAL_KIND:
        from mastercode.runtime.turn_type_approval import apply_proposal as _tt_apply
        out["turn_type_apply"] = await _tt_apply(
            pool, eid, md, option_id=option_id, ticket_id=ticket_id)
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('turn_type_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["turn_type_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: turn type apply result not recorded: %s", ticket_id, e)
    # A live context card the Host built: the person's click on
    # the rendered preview is the ONLY thing that makes it live — after the
    # decide, like the turn type card above, and recorded on the ticket.
    if md.get("kind") == LIVE_CONTEXT_PROPOSAL_KIND:
        from mastercode.live_context_host import apply_proposal as _lc_apply
        out["live_context_apply"] = await _lc_apply(
            pool, eid, md, option_id=option_id, ticket_id=ticket_id)
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('live_context_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["live_context_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: live context apply result not recorded: %s", ticket_id, e)
    # A limit the Host asked to change (runtime/user_limits): the person's
    # click is the only writer, after the decide like the cards above, and the
    # outcome is recorded on the ticket for the Host's limits(op='status').
    if md.get("kind") == LIMIT_CHANGE_PROPOSAL_KIND:
        from mastercode.runtime.user_limits import apply_proposal as _lim_apply
        try:
            out["limit_change_apply"] = await _lim_apply(
                pool, eid, md, option_id=option_id, ticket_id=ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: limit change not applied: %s", ticket_id, e)
            out["limit_change_apply"] = {"applied": False, "why": str(e)[:300]}
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('limit_change_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["limit_change_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001
            log.warning("ticket %d: limit change result not recorded: %s", ticket_id, e)
    # A recurring task the Host asked to start (runtime/scheduled_approval):
    # the person's click sets it, after the decide like the cards above.
    if md.get("kind") == RECURRING_TASK_PROPOSAL_KIND:
        from mastercode.runtime.scheduled_approval import apply_proposal as _rt_apply
        try:
            out["recurring_task_apply"] = await _rt_apply(
                pool, eid, md, option_id=option_id, ticket_id=ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: recurring task not set: %s", ticket_id, e)
            out["recurring_task_apply"] = {"applied": False, "why": str(e)[:300]}
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('recurring_task_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["recurring_task_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001
            log.warning("ticket %d: recurring task result not recorded: %s", ticket_id, e)
    # A runbook trigger the Host asked to add (runtime/scheduled_approval):
    # the person's click adds it.
    if md.get("kind") == RUNBOOK_TRIGGER_PROPOSAL_KIND:
        from mastercode.runtime.scheduled_approval import apply_trigger as _rb_apply
        try:
            out["runbook_trigger_apply"] = await _rb_apply(
                pool, eid, md, option_id=option_id, ticket_id=ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: runbook trigger not added: %s", ticket_id, e)
            out["runbook_trigger_apply"] = {"applied": False, "why": str(e)[:300]}
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('runbook_trigger_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["runbook_trigger_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001
            log.warning("ticket %d: runbook trigger result not recorded: %s", ticket_id, e)
    # A board post the Host wrote (runtime/board_post_approval): the person's
    # 'post' sends the request stored on the card, after the decide like the
    # cards above, so a double click cannot post twice.
    if md.get("kind") == BOARD_POST_PROPOSAL_KIND:
        from mastercode.runtime.board_post_approval import apply_proposal as _bp_apply
        try:
            out["board_post_apply"] = await _bp_apply(
                pool, eid, md, option_id=option_id, ticket_id=ticket_id)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: board post not sent: %s", ticket_id, e)
            out["board_post_apply"] = {"posted": False, "why": str(e)[:300]}
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('board_post_apply', $1::jsonb) WHERE id = $2",
                json.dumps(out["board_post_apply"]), ticket_id)
        except Exception as e:  # noqa: BLE001
            log.warning("ticket %d: board post result not recorded: %s", ticket_id, e)
    # A support ticket another Host sent the platform: the operator's fix
    # / ask / decline is applied by the platform — 'fix' also opens the
    # build phase the answer rides on.
    # After the decide, for the same reason as the two above; the module
    # never raises, so a bridge outage leaves the card decided with the
    # outcome on it rather than the person's answer lost in a 500.
    if md.get("kind") == SUPPORT_KIND:
        from mastercode.runtime import support_collect as _sc
        out["support"] = await _sc.verdict(
            pool, eid, ticket_id=ticket_id, md=md, option_id=option_id, note=note or "")
    # The approval card for an op (rung 7): the click records the choice and
    # files the ask on the user's device so the tap is waiting there; nothing
    # runs from here — the Host's next op_apply waits on the device and
    # dispatches. After the decide for the same reason as the others; the
    # module never raises. The outcome goes on the ticket so the Host's next
    # call and the Tickets pane both read it.
    if md.get("kind") == OP_ASK_KIND:
        from server.network import nac_ops
        try:
            out["op_ask"] = await nac_ops.card_decided(
                pool, eid, ticket_id=ticket_id, md=md, option_id=option_id, note=note or "")
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: op card decide not applied: %s", ticket_id, e)
            out["op_ask"] = {"choice": option_id, "filed": False, "why": str(e)[:300]}
        try:
            await pool.execute(
                "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('op_ask_result', $1::jsonb) WHERE id = $2",
                json.dumps(out["op_ask"]), ticket_id)
        except Exception as e:  # noqa: BLE001
            log.warning("ticket %d: op card result not recorded: %s", ticket_id, e)
    # The turn that asked is still running: the person's answer
    # goes into it, whichever surface they answered on, in the ask card's own
    # words (app-web-kit/ask.ts answerText) — the Host is holding for it
    # (session.hold_for) and goes on from it. The Workbench card, which has no
    # nudge lane of its own, and the Tickets pane reach the turn this way;
    # Circles also nudges the same words, and the turn reads them once
    # (session.py: an answer to a ticket is injected once per turn).
    # The turn that asked has ENDED: the answer
    # starts a turn in the room it asked in, as the person's message — one
    # server path for every surface (the Tickets pane had none). The ask cards
    # follow that turn instead of sending the answer again (answer_turn_started).
    # Not when the platform closes a card the person answered elsewhere
    # (deliver_answer False, see the docstring).
    if deliver_answer:
        out.update(await _answer_to_turn(eid, ticket_id, md, chosen, option_id, note, actor))
    # A scheduled task waiting on this ticket wakes now (server/host_events.py);
    # after everything the decide applies, so the woken turn reads the outcome.
    # Whoever decided it — a platform-made close too: the ticket IS decided.
    from server import host_events
    await host_events.emit(pool, eid, "ticket_decided",
                           {"ticket_id": ticket_id, "status": "decided", "option_id": option_id})
    return out


async def _hold_answer(eid, session_id: str, room, text: str):
    """Write the answer for the running turn `session_id` asked in to
    pending_words; its key, or None (no live turn there, no pool, PII mode,
    or the write failed: then it is held in memory only, as before)."""
    from mastercode import pending_words as _pw
    from mastercode.runtime.session import nudge_words, turn_is_live
    from server import brain_service
    from server.turn_registry import compose_thread_key, get_turn_registry
    if eid is None or not turn_is_live(session_id):
        return None
    rec = get_turn_registry().by_session(session_id)
    where = (dict(rec.room) if rec is not None and isinstance(rec.room, dict)
             else dict(room) if isinstance(room, dict) else None)
    key = (rec.thread_key if rec is not None
           else compose_thread_key(eid, where.get("app_id"), where.get("project_id"),
                                   thread_id=where.get("thread_id"),
                                   history_context=where.get("history_context"))
           if where else None)
    try:
        return await _pw.hold(brain_service.get_pool(), eid, session_id, nudge_words(text),
                              room=where, thread_key=key)
    except Exception as e:  # noqa: BLE001 — the answer still goes in, held in memory
        log.error("ticket answer: not held in the database (session %s): %s", session_id, e)
        return None


async def _settle_answer(key) -> None:
    from mastercode import pending_words as _pw
    from server import brain_service
    try:
        await _pw.settle(brain_service.get_pool(), key)
    except Exception as e:  # noqa: BLE001
        log.error("ticket answer: its row was not removed: %s", e)


async def _answer_to_turn(eid, ticket_id: int, md: dict, chosen: dict, option_id: str,
                          note: str, actor: str) -> dict:
    """The person's answer to a decided ticket, delivered to the Host (see the
    comment above the call in decide_ticket_service): into the running turn
    that asked, else as a new turn in the room it asked in. Returns the keys
    the ask cards read ({answered_into_turn, answer_turn_started}), empty when
    there is nobody to tell. Never raises: the decide already stands."""
    out: dict = {}
    _asked = md.get("asked_in_session")
    _room = md.get("asked_in") if isinstance(md.get("asked_in"), dict) else None
    if actor == "user" and ((isinstance(_asked, str) and _asked) or _room):
        try:
            from mastercode.runtime.session import (
                answer_after_turn, answer_into_turn)
            _n = (note or "").strip()
            # Whole: the person's note is never
            # cut on its way into the turn.
            _answer = (f"Ticket #{ticket_id} — I chose “{chosen.get('label') or option_id}”"
                       + (f": {_n}" if _n else "."))
            # Held in the Host's database before the turn has it, as words
            # typed into a running turn are (pending_words): a restart
            # before a turn reads it still delivers it, and the row goes
            # once a turn has it.
            _key = (await _hold_answer(eid, _asked, _room, _answer)
                    if isinstance(_asked, str) and _asked else None)
            out["answered_into_turn"] = bool(
                isinstance(_asked, str) and _asked
                and answer_into_turn(_asked, ticket_id, _answer,
                                     **({"pending_key": _key} if _key else {})))
            if not out["answered_into_turn"] and _room and eid is not None:
                out["answer_turn_started"] = await answer_after_turn(
                    eid, _room, _answer,
                    asked_session=_asked if isinstance(_asked, str) else None,
                    **({"pending_key": _key} if _key else {}))
            if _key and not out.get("answered_into_turn") and not out.get("answer_turn_started"):
                # Delivered nowhere: the answer stays on the ticket, and no
                # row may send it later on its own.
                await _settle_answer(_key)
        except Exception as e:  # noqa: BLE001 — the decide already stands
            log.warning("ticket %d: answer not delivered into its turn: %s", ticket_id, e)
    return out


async def dismiss_ticket_service(
    pool, eid: int | None, ticket_id: int, *, note: str = "",
    actor: str = "user",
) -> dict:
    """Close an OPEN ticket without picking an option — the source of truth for
    POST /api/tickets/{id}/dismiss and the `tickets` tool op='dismiss'. Doesn't
    create a pattern. Entity-scoped: the UPDATE only matches this entity's own open
    ticket; anything else (missing / already closed / not yours) raises
    TicketError(404). Returns {ticket_id, status: 'dismissed'}.

    `actor` as in decide_ticket_service: a `user_only` ticket (the host's own
    route-change proposal) is the user's to close as well as to answer — the
    host may not withdraw its ask before the user has seen it (403)."""
    if pool is None:
        raise TicketError(503, "brain pool down")
    note = note or ""
    # Pull metadata BEFORE the update: it decides who may close this ticket.
    pre = await pool.fetchrow(
        "SELECT metadata FROM entity_tickets WHERE id = $1 AND entity_id = $2 AND status = 'open'",
        ticket_id, eid,
    )
    if pre and actor != "user":
        md = pre["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        if isinstance(md, dict) and md.get("user_only"):
            raise TicketError(403, "this ticket is the user's to close — it was filed by the host "
                                   "and stays open until the user answers it in Circles")
    if pre:
        md = pre["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        if isinstance(md, dict) and md.get("kind") == SUPPORT_KIND:
            # Another Host's user is waiting on this answer. A dismissed card
            # would strand their ticket (decided_at set, nothing sent); the
            # honest close is 'decline' with the note they read.
            raise TicketError(400, "a support ticket is answered, never dismissed — pick "
                                   "'decline' and write the note the user reads")
    result = await pool.execute(
        """UPDATE entity_tickets
              SET status = 'dismissed',
                  decision = jsonb_build_object('note', $1::text, 'dismissed', TRUE),
                  decided_by = $2,
                  decided_at = NOW()
            WHERE id = $3 AND entity_id = $4 AND status = 'open'""",
        note, eid, ticket_id, eid,
    )
    if not result.endswith(" 1"):
        raise TicketError(404, "ticket not found or already closed")
    # A connector pairing card dismissed is a pairing declined: the draft must
    # not wait on a card nobody can see any more. Only the draft the grant
    # itself names this ticket on is touched.
    if pre and isinstance(md, dict) and md.get("kind") == CONNECTOR_PAIRING_KIND:
        try:
            from server.network import nac_connector
            gid = int((md.get("connector_pairing") or {}).get("grant_id"))
            named = await pool.fetchval(
                "SELECT ticket_id FROM nac_connector_grants WHERE id=$1", gid)
            if named is not None and int(named) == int(ticket_id):
                await nac_connector.decline_connector(pool, gid, actor="user", eid=eid,
                                                      note=note or "")
        except Exception as e:  # noqa: BLE001 — the card is closed; Network shows the draft
            log.info("ticket %d: connector draft not declined on dismiss: %r", ticket_id, e)
    from server import host_events
    await host_events.emit(pool, eid, "ticket_decided",
                           {"ticket_id": ticket_id, "status": "dismissed"})
    return {"ticket_id": ticket_id, "status": "dismissed"}


# ---- The join between a ticket and a Workbench phase ----
#
# One ticket system, two shapes:
# something a PERSON must do is a ticket here, in Circles; something a BUILD
# must do is a project phase, in the Workbench. The two are joined through the
# ticket's metadata — the same slot that already carries a linked session id —
# in both directions:
#
#   ticket -> phase   metadata {project_id, phase_id}: when that phase lands
#                     'complete' the ticket is decided with option 'built';
#                     a 'failed' or 'review' landing is recorded on the ticket
#                     (metadata.phase_last) and it stays open.
#   phase -> ticket   a phase with gate='user' that the plan is waiting on
#                     raises ONE ticket (metadata.kind='phase_gate'); the
#                     user's 'approve' on it writes approved_at, exactly what
#                     the Workbench approve route writes. user_only + no_pattern
#                     for the same reason the route-change proposal carries
#                     them: the host may not approve its own gate, and no
#                     pattern may ever auto-approve one.
#
# A 'hands' ticket — one option, usually 'done' — is not a new kind; it is a
# ticket whose one option may carry `credential_request` {name, cred_type,
# purpose}. The Circles card then offers the same sealed "Store in Keys" input
# the composer has; the kernel never sees the bytes and never validates more
# than the request's shape here.

PHASE_GATE_KIND = "phase_gate"
SUPPORT_KIND = "support"   # runtime/support_collect.SUPPORT_KIND — the operator's queue
# The host's proposed build pipeline: the
# user's 'apply' writes projects.config.pipeline (_apply_pipeline_proposal).
PIPELINE_PROPOSAL_KIND = "pipeline_proposal"   # = runtime/tools.PIPELINE_PROPOSAL_KIND
# A turn type draft / change / retire card: the
# person's click applies it through runtime/turn_type_approval.apply_proposal.
TURN_TYPE_PROPOSAL_KIND = "turn_type_proposal"   # = runtime/turn_type_approval.PROPOSAL_KIND
LIVE_CONTEXT_PROPOSAL_KIND = "live_context_proposal"   # = mastercode/live_context_host.PROPOSAL_KIND
# The approval card for an op the Host wrote: the pod files it from nac_ops;
# the user's click records the choice on the run and files the ask on their
# approval device (nac_ops.card_decided).
OP_ASK_KIND = "op_ask"   # = server/network/nac_ops.ASK_KIND
# A messaging connector's pairing card: the
# person lets the reported account in with their authenticator code
# (nac_connector.decide_from_ticket). Only the pod's own /api/connector/pair
# files one.
CONNECTOR_PAIRING_KIND = "connector_pairing"   # = server/network/nac_connector.CONNECTOR_PAIRING_KIND
# A change to one of the person's limits the Host proposed (runtime/user_limits):
# the person's 'apply' writes the setting; only the Host's `limits` tool files one.
LIMIT_CHANGE_PROPOSAL_KIND = "limit_change_proposal"   # = runtime/user_limits.PROPOSAL_KIND
# A recurring task that runs a model, which the Host may not start by itself
# (runtime/scheduled_approval): the person's 'apply' sets it.
RECURRING_TASK_PROPOSAL_KIND = "recurring_task_proposal"   # = runtime/scheduled_approval.PROPOSAL_KIND
RUNBOOK_TRIGGER_PROPOSAL_KIND = "runbook_trigger_proposal"  # = runtime/scheduled_approval.TRIGGER_KIND
# A post on the members' board the Host wrote (runtime/board_post_approval):
# the person's 'post' sends the exact request stored on the card.
BOARD_POST_PROPOSAL_KIND = "board_post_proposal"  # = runtime/board_post_approval.PROPOSAL_KIND
# Its 'Post it' takes the authenticator code on the card (decide_ticket_service).
BOARD_POST_STEPUP_OPTION = "post"  # = runtime/board_post_approval.OPT_POST
BOARD_POST_STEPUP_REFUSED = (
    "Posting on the members' board needs a current code from your authenticator (the "
    "second factor enrolled in Network). Nothing was posted: enter a fresh code on the "
    "card and press Post it again.")
SUPPORT_NOTE_REQUIRED = ("reply", "ask", "decline")   # = support_collect.NOTE_REQUIRED (gate-pinned)
BUILT_OPTION_ID = "built"
_CRED_TYPES = ("api_key", "opaque")
_CRED_NAME_MAX = 120


def _strip_platform_locks(metadata: Any) -> dict:
    """The metadata keys a caller of POST /api/tickets may not set, because
    each one makes the platform ACT on the answer: `user_only` and
    `no_pattern` ("the platform filed this and applies its answer"),
    `route_change_proposal` (the Orchestrator applies the route),
    `kind=phase_gate` (the user's 'approve' writes approved_at on a phase —
    approval authority stays with write:projects and the platform's own gate
    ticket, never with any write:tickets holder naming a phase),
    `kind=support` (the platform answers another Host's ticket on the decide)
    and `pipeline_proposal` / `kind=pipeline_proposal` (the user's 'apply'
    writes a project's build pipeline — only the platform's own `pipeline`
    tool, which writes the sitrep from the validated document, raises one),
    `turn_type_proposal` / `kind=turn_type_proposal` (the user's click
    activates, changes or retires a turn type — only the platform's own
    `turn_type` tool, which writes the card from the validated profile,
    raises one), `live_context_proposal` / `kind=live_context_proposal` (the
    user's click puts a Host-built card into every matching prompt — only the
    pod's own card sync, which renders the preview from the validated card,
    raises one), and `op_ask` / `kind=op_ask` (the user's click files an ask on their
    approval device for a run on one of their machines — only the pod's own
    op door, which writes the sitrep from the manifest and the dry run,
    raises one), and `asked_in_session` / `asked_in` (deciding the ticket
    speaks into that running turn, or starts a turn with the answer in that
    room — only the platform's own `escalate`, from the turn itself, writes
    them). Anything else passes through untouched."""
    if not isinstance(metadata, dict):
        return {}
    out = {k: v for k, v in metadata.items()
           if k not in ("user_only", "no_pattern", "route_change_proposal",
                        "op_ask", "op_ask_result", "pipeline_proposal",
                        "turn_type_proposal", "turn_type_apply",
                        "live_context_proposal", "live_context_apply",
                        "asked_in_session", "asked_in", "connector_pairing",
                        "limit_change", "limit_change_apply",
                        "recurring_task", "recurring_task_apply",
                        "runbook_trigger", "runbook_trigger_apply",
                        "board_post", "board_post_then", "board_post_apply")}
    if out.get("kind") in (PHASE_GATE_KIND, SUPPORT_KIND, OP_ASK_KIND, PIPELINE_PROPOSAL_KIND,
                           TURN_TYPE_PROPOSAL_KIND, LIVE_CONTEXT_PROPOSAL_KIND,
                           CONNECTOR_PAIRING_KIND, LIMIT_CHANGE_PROPOSAL_KIND,
                           RECURRING_TASK_PROPOSAL_KIND, RUNBOOK_TRIGGER_PROPOSAL_KIND,
                           BOARD_POST_PROPOSAL_KIND):
        # kind=support: the platform applies the answer on another Host's
        # ticket (a verdict, or the person's 'Tell them it's fixed') — only the operator's own
        # collect may raise one.
        out.pop("kind")
    return out


def with_turn_room(metadata: Any) -> dict:
    """`metadata` with `asked_in` set to the room of the turn running in this
    task (memory_engine.current_turn_room), when it names none yet. Every
    ticket the platform files during a turn — a route change, a pipeline or
    turn type card, an op's approval, anything a tool files — is then linked
    to the conversation it was filed in: the chat lists that conversation's
    open tickets (list_tickets_service asked_in=) and raises each as its card,
    and deciding it starts the answer's turn there (decide_ticket_service).
    Outside a turn (an HTTP request, the connector pod's /pair, a background
    collector) there is no room and nothing changes; the route strips a
    caller's own asked_in before it gets here (_strip_platform_locks)."""
    md = dict(metadata) if isinstance(metadata, dict) else {}
    if isinstance(md.get("asked_in"), dict):
        return md
    try:
        from mastercode.memory_engine import current_turn_room
        room = current_turn_room()
    except Exception:  # noqa: BLE001 — no runtime in this process: no room
        room = None
    if room:
        md["asked_in"] = room
    return md


# The rooms a chat asks for its open tickets by (list_tickets_service): a
# Circles thread uuid, the original Circles conversation, or a Workbench
# project.
_ROOM_THREAD_RE = re.compile(r"^thread:([0-9A-Za-z-]{1,64})$")
_ROOM_PROJECT_RE = re.compile(r"^project:([0-9]{1,18})$")
ORIGINAL_ROOM = "thread:original"
# Your Host's one conversation: every
# topic of it at once, the original conversation and every Circles thread of
# the entity, deleted ones too (a ticket outlives its thread). Circles' cards
# read this, so a question the Host asked on any topic comes up whatever topic
# the person last wrote to.
CONVERSATION_ROOM = "thread:*"


def _room_filter(asked_in: str, params: list) -> str:
    """The SQL condition for one room, appending its parameter to `params`.
    Raises TicketError(400) on anything else. The original conversation is
    the Circles room with neither a thread nor a project (escalate records
    app_id 'circles', thread_id null, project_id null there)."""
    a = (asked_in or "").strip()
    room = "metadata->'asked_in'"
    if a == CONVERSATION_ROOM:
        # $1 is the entity in every caller (list_tickets).
        return (f"(({room}->>'app_id' = 'circles' AND COALESCE({room}->>'thread_id', '') = '' "
                f"AND COALESCE({room}->>'project_id', '') = '') "
                f"OR lower(COALESCE({room}->>'thread_id', '')) IN "
                f"(SELECT id::text FROM circles_threads WHERE entity_id = $1))")
    if a == ORIGINAL_ROOM:
        return (f"({room}->>'app_id' = 'circles' AND COALESCE({room}->>'thread_id', '') = '' "
                f"AND COALESCE({room}->>'project_id', '') = '')")
    m = _ROOM_THREAD_RE.match(a)
    if m:
        params.append(m.group(1).lower())
        return f"lower({room}->>'thread_id') = ${len(params)}"
    m = _ROOM_PROJECT_RE.match(a)
    if m:
        params.append(str(int(m.group(1))))
        return (f"({room}->>'project_id' = ${len(params)} "
                f"AND COALESCE({room}->>'thread_id', '') = '')")
    raise TicketError(400, "asked_in must be thread:<id>, thread:original, thread:* or project:<id>")


def _bad_phase_link(metadata: Any) -> str:
    """'' when metadata's phase link (if any) is well-formed, else why not.
    `project_id` and `phase_id` must be integers when present: the join looks
    them up as text against the ticket rows, but a writer that stores '5.0'
    or 'p-7' would name a phase that can never land."""
    if not isinstance(metadata, dict):
        return ""
    for key in ("project_id", "phase_id"):
        if key in metadata and (isinstance(metadata[key], bool)
                                or not isinstance(metadata[key], int)):
            return f"metadata.{key} must be an integer"
    return ""


def has_credential_ask(options: Any) -> bool:
    """True when any option asks the user to seal a credential."""
    return isinstance(options, list) and any(
        isinstance(o, dict) and "credential_request" in o for o in options)


async def _active_vault_names(pool, eid: int) -> set[str]:
    """The names this Host's vault holds today (active and deactivated alike —
    a deactivated key is still stored and re-enableable). Metadata only."""
    from server.vault import api as vault_api
    rows = await vault_api.list_secrets(pool, entity_id=int(eid))
    return {str(r.get("logical_name")) for r in rows if r.get("logical_name")}


async def refuse_bad_asks(pool, eid: int | None, *, options: list, metadata: Any) -> None:
    """The one place a ticket's asks are checked, for every writer (the route,
    the host's `escalate`, the platform's own gate). Raises TicketError.

    A credential ask must be well-formed (shape) and must NOT name a key the
    vault already holds: the Keys store supersedes on name, so a ticket that
    asked for "your GitHub token" under the name of an existing provider key
    would have the user's paste silently replace that key. Replacing a key is
    the user's own act in the Keys app; a ticket may only ask for a NEW name.
    The vault is asked once, only when an ask is present; an unanswerable
    vault refuses the ask (fail closed) rather than letting it through.
    A phase link, when present, must be integers."""
    asks = [(i, o["credential_request"]) for i, o in enumerate(options)
            if isinstance(o, dict) and "credential_request" in o]
    for i, req in asks:
        why = _bad_credential_request(req)
        if why:
            raise TicketError(400, f"option[{i}].credential_request {why}")
    if asks:
        try:
            names = await _active_vault_names(pool, eid)
        except Exception as e:  # noqa: BLE001
            raise TicketError(503, f"could not check the vault for that name: {e}") from None
        for i, req in asks:
            if req["name"] in names:
                raise TicketError(
                    400, f"option[{i}].credential_request names an existing key "
                         f"{req['name']!r} — ask under a new name; replacing a key is "
                         "the user's own act in Keys")
    why = _bad_phase_link(metadata)
    if why:
        raise TicketError(400, why)


def _bad_credential_request(req: Any) -> str:
    """'' when `req` is a well-formed credential ask, else why it is not.
    Shape only: {name (str, 1..120, no whitespace-only), cred_type (api_key |
    opaque), purpose? (str)}. The value is never part of a ticket."""
    if not isinstance(req, dict):
        return "must be an object {name, cred_type, purpose?}"
    name = req.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > _CRED_NAME_MAX:
        return f"needs a name (1..{_CRED_NAME_MAX} chars)"
    if req.get("cred_type") not in _CRED_TYPES:
        return f"cred_type must be one of {list(_CRED_TYPES)}"
    purpose = req.get("purpose", "")
    if purpose is not None and not isinstance(purpose, str):
        return "purpose must be a string"
    if "value" in req or "value_b64" in req:
        return "must not carry a value — the user seals it in Keys, the ticket only names it"
    return ""


async def raise_phase_gate_ticket(pool, eid: int | None, *, project_id: int,
                                  phase_id: int) -> dict:
    """Open the ONE ticket that asks the user to approve a gated phase, or
    return the one already open. Idempotent on (kind, phase_id) among OPEN
    tickets: a phase the plan keeps asking about (every build-door refusal,
    every chain pass) must not pile up cards. Returns {ticket_id, created}.
    Never raises into the build path — a missing phase is {ticket_id: None}."""
    if pool is None or eid is None:
        return {"ticket_id": None, "created": False}
    phase_key = str(int(phase_id))
    existing = await pool.fetchrow(
        """SELECT id FROM entity_tickets
            WHERE entity_id = $1 AND status = 'open'
              AND metadata->>'kind' = $2 AND metadata->>'phase_id' = $3""",
        eid, PHASE_GATE_KIND, phase_key)
    if existing:
        return {"ticket_id": existing["id"], "created": False}
    ph = await pool.fetchrow(
        """SELECT ph.id, ph.phase_number, ph.title, ph.spec, ph.description,
                  ph.gate, ph.approved_at, ph.updated_at, p.name AS project_name
             FROM project_phases ph JOIN projects p ON p.id = ph.project_id
            WHERE ph.id = $1 AND ph.project_id = $2""", int(phase_id), int(project_id))
    if ph is None or ph["gate"] != "user" or ph["approved_at"]:
        return {"ticket_id": None, "created": False}
    # SENT BACK, unchanged since: the user answered this phase's last card with
    # 'send back' and nothing on the phase has moved after that answer. Asking
    # again would be the same card with the note thrown away. The note is the
    # answer; the caller relays it to the host. A rewrite of the phase
    # (set_phase_spec bumps updated_at) is what earns a new card.
    back = await pool.fetchrow(
        """SELECT id, decided_at, decision->>'note' AS note FROM entity_tickets
            WHERE entity_id = $1 AND status = 'decided'
              AND metadata->>'kind' = $2 AND metadata->>'phase_id' = $3
              AND decision->>'option_id' = 'send_back'
            ORDER BY decided_at DESC LIMIT 1""",
        eid, PHASE_GATE_KIND, phase_key)
    if back and ph["updated_at"] and back["decided_at"] and back["decided_at"] >= ph["updated_at"]:
        return {"ticket_id": back["id"], "created": False, "sent_back": back["note"] or ""}
    # The WHOLE brief: the person approves what
    # this card shows, so it is never cut. sitrep is a TEXT column and the
    # Tickets pane renders it whole.
    brief = (ph["spec"] or ph["description"] or "").strip()
    sitrep =(f"Phase {ph['phase_number']} of {ph['project_name']!s} — {ph['title']} — is "
              "gated on your approval and is next to build. Approving it here or in the "
              "Workbench starts it."
              + (f"\n\nThe brief:\n{brief}" if brief else ""))
    options = [
        # The click IS the go-ahead: approving starts the phase
        # when its turn has come (tools.start_approved_phase) — the card used
        # to tell the person to "say build", a word typed to make the Host act.
        {"id": "approve", "label": "Approve — build it",
         "consequence": "The phase starts building now if what it waits for has "
                        "landed, otherwise as soon as it does.",
         "recommend": True},
        {"id": "send_back", "label": "Not yet — send it back",
         "consequence": "The phase stays gated and your note reaches your Host; "
                        "you are not asked again until the phase changes."},
    ]
    try:
        created = await create_ticket_service(
            pool, eid, source="runtime", sitrep=sitrep, options=options,
            requester={"kind": "workbench", "display_name": str(ph["project_name"])},
            context={"project_id": int(project_id), "phase_id": int(phase_id),
                     "phase_number": int(ph["phase_number"]), "title": ph["title"]},
            priority=60,
            metadata={"kind": PHASE_GATE_KIND, "project_id": int(project_id),
                      "phase_id": int(phase_id), "user_only": True, "no_pattern": True},
        )
    except Exception as e:  # noqa: BLE001
        # Two callers at once (two idle explanations after parallel phases
        # land) both passed the read above; the partial unique index (v177)
        # lets exactly one insert land. The loser adopts the winner's ticket.
        if type(e).__name__ != "UniqueViolationError":
            raise
        again = await pool.fetchrow(
            """SELECT id FROM entity_tickets
                WHERE entity_id = $1 AND status = 'open'
                  AND metadata->>'kind' = $2 AND metadata->>'phase_id' = $3""",
            eid, PHASE_GATE_KIND, phase_key)
        return {"ticket_id": again["id"] if again else None, "created": False}
    log.info("phase gate ticket %s opened for phase %s (project %s)",
             created["id"], phase_id, project_id)
    return {"ticket_id": created["id"], "created": True}


async def _apply_phase_gate_decision(pool, eid: int | None, md: dict, *, option_id: str,
                                     ticket_id: int) -> dict:
    """The deterministic half of a phase-gate ticket: 'approve' writes
    approved_at on the phase named in the ticket's metadata, the same way
    projects.approve_phase does (COALESCE — a phase approved meanwhile stays
    approved at its earlier time). Any other option writes nothing. Never
    raises; the ticket is already decided either way."""
    if option_id != "approve":
        return {"applied": False}
    try:
        phase_id = int(md.get("phase_id"))
        project_id = int(md.get("project_id"))
    except (TypeError, ValueError):
        return {"applied": False, "error": "ticket names no phase"}
    try:
        res = await pool.execute(
            """UPDATE project_phases
                  SET approved_at = COALESCE(approved_at, now()), updated_at = now()
                WHERE id = $1 AND project_id = $2 AND gate = 'user'""",
            phase_id, project_id)
        applied = isinstance(res, str) and res.endswith(" 1")
        if applied:
            log.info("ticket %d: phase %d approved by the user's decision", ticket_id, phase_id)
    except Exception as e:  # noqa: BLE001
        log.warning("ticket %d: phase approve refused: %s", ticket_id, e)
        return {"applied": False, "error": str(e)[:300]}
    # The approval is the go-ahead: the phase starts when its turn has come,
    # with nothing more to type (tools.start_approved_phase).
    started = {"started": False}
    if applied:
        try:
            from mastercode.runtime.tools import start_approved_phase
            started = await start_approved_phase(pool, eid, project_id, phase_id)
        except Exception as e:  # noqa: BLE001 — the approval already stands
            started = {"started": False, "why": str(e)[:300]}
    # Any sibling card for the same phase (a race opened two before v177, or
    # an old row) is answered by this one so nothing stays open for a phase
    # the user has already approved.
    siblings = await resolve_phase_gate_tickets(
        pool, eid, phase_id=phase_id, note=f"approved on ticket {ticket_id}",
        exclude_ticket_id=ticket_id)
    return {"applied": applied, "phase_id": phase_id, "also_decided": siblings,
            "build": started}


async def resolve_phase_gate_tickets(pool, eid: int | None, *, phase_id: int,
                                     note: str = "approved in the Workbench",
                                     exclude_ticket_id: int | None = None) -> list:
    """The phase was approved somewhere else (the Workbench approve route, or
    another card for the same phase): decide its open gate ticket(s) with
    'approve' so no card dangles. Returns the ticket ids decided. Never
    raises.

    The person did not answer these cards, so nothing is said to the Host
    for them (deliver_answer=False): the approve route has already started
    the build, and a card approved by the person has delivered its own
    answer. Delivering here started a Host turn nobody asked for."""
    if pool is None or eid is None:
        return []
    done: list = []
    try:
        rows = await pool.fetch(
            """SELECT id FROM entity_tickets
                WHERE entity_id = $1 AND status = 'open'
                  AND metadata->>'kind' = $2 AND metadata->>'phase_id' = $3""",
            eid, PHASE_GATE_KIND, str(int(phase_id)))
        for r in rows:
            if exclude_ticket_id is not None and r["id"] == exclude_ticket_id:
                continue
            try:
                await decide_ticket_service(pool, eid, r["id"], option_id="approve",
                                            note=note, actor="user", deliver_answer=False)
                done.append(r["id"])
            except TicketError:
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_phase_gate_tickets(phase %s): %s", phase_id, e)
    return done


async def on_phase_outcome(pool, eid: int | None, *, phase_id: int, status: str,
                           note: str = "") -> dict:
    """A phase landed. Every OPEN ticket joined to it (metadata.phase_id, and
    not itself the gate ticket) hears about it:

      complete -> decided with option 'built' (added to the ticket's options
                  if the writer did not include one; a decide needs an option
                  the ticket carries), the note = what landed.
      failed / review / anything else -> recorded as metadata.phase_last
                  {status, note, at}; the ticket stays open — the person still
                  has something to do, now with the outcome on the card.

    Returns {decided: [ids], noted: [ids]}. Never raises into the build path."""
    out = {"decided": [], "noted": []}
    if pool is None or eid is None:
        return out
    try:
        rows = await pool.fetch(
            """SELECT id, options, metadata FROM entity_tickets
                WHERE entity_id = $1 AND status = 'open'
                  AND metadata->>'phase_id' = $2
                  AND COALESCE(metadata->>'kind', '') <> $3""",
            eid, str(int(phase_id)), PHASE_GATE_KIND)
    except Exception as e:  # noqa: BLE001
        log.warning("on_phase_outcome(phase %s): lookup failed: %s", phase_id, e)
        return out
    for r in rows:
        tid = r["id"]
        try:
            if status == "complete":
                opts = r["options"]
                if isinstance(opts, str):
                    opts = json.loads(opts)
                if not any(isinstance(o, dict) and o.get("id") == BUILT_OPTION_ID for o in opts):
                    opts = list(opts) + [{"id": BUILT_OPTION_ID, "label": "Built",
                                          "consequence": "The linked phase landed complete."}]
                    await pool.execute(
                        "UPDATE entity_tickets SET options = $1::jsonb WHERE id = $2 AND status = 'open'",
                        json.dumps(opts), tid)
                await decide_ticket_service(
                    pool, eid, tid, option_id=BUILT_OPTION_ID,
                    note=(note or "the linked phase landed complete"), actor="host")
                out["decided"].append(tid)
            else:
                last = {"status": status, "note": (note or "")[:2000],
                        "at": datetime.now(timezone.utc).isoformat()}
                await pool.execute(
                    """UPDATE entity_tickets
                          SET metadata = COALESCE(metadata, '{}'::jsonb)
                                         || jsonb_build_object('phase_last', $1::jsonb)
                        WHERE id = $2 AND status = 'open'""",
                    json.dumps(last), tid)
                out["noted"].append(tid)
        except TicketError as e:
            log.info("on_phase_outcome(phase %s): ticket %s: %s", phase_id, tid, e.detail)
        except Exception as e:  # noqa: BLE001
            log.warning("on_phase_outcome(phase %s): ticket %s: %s", phase_id, tid, e)
    # A support card was DECIDED 'fix' when its phase opened, so the loop
    # above never sees it (it reads open tickets). A complete landing makes
    # that ticket ASK the person whether to tell the user it is fixed
    # (support_collect.support_landed); only their click sends it.
    # Never raises.
    if status == "complete":
        try:
            from mastercode.runtime import support_collect as _sc
            out["support_landed"] = await _sc.support_landed(pool, eid, phase_id=phase_id)
        except Exception as e:  # noqa: BLE001
            log.warning("on_phase_outcome(phase %s): support_landed: %s", phase_id, e)
    return out

