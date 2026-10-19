"""Calendar — pod-local calendar_events surface for the Calendar app.

The calendar table is dual-written from the platform's memory service via bridge
handlers (see server.bridge.memory_handlers.handle_calendar_*). This
router exposes those handlers over HTTP so the in-pod apps (the Calendar
app, Circles, the desktop's dock badge) can read the Host's own timeline
without round-tripping the bridge.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from fastapi import Body
from pydantic import BaseModel

from server.applications.middleware import requires_permission
from server.bridge.memory_handlers import (
    handle_calendar_list,
    handle_calendar_today,
    handle_calendar_read,
    handle_calendar_runs,
    handle_calendar_cancel,
    handle_calendar_complete,
    handle_calendar_append,
)


class CalendarEventCreate(BaseModel):
    event_type: str
    summary: str
    event_at: str | None = None       # ISO; defaults to now if omitted
    status: str = "scheduled"          # scheduled|running|completed|failed|cancelled
    payload: dict | None = None        # arbitrary structured payload
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model_id: str | None = None
    source: str = "user"               # 'user' for hand-composed events

logger = logging.getLogger("privatae.calendar")

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


def _unwrap(envelope: dict) -> dict:
    """Map a LOCAL handler {'ok': bool, 'error': str} envelope to an HTTP
    response with a status code matching the error semantics — a malformed
    event_id is a 400, not a 502 (these handlers are in-pod, not a gateway)."""
    if not envelope.get("ok", False):
        err = envelope.get("error") or "unknown error"
        e = err.lower()
        # A handler that says the pod is not ready yet (no pool, no host id)
        # marks the envelope `retryable` — matched on the flag, not the words,
        # so rewording a message cannot turn a 503 into a 502 again.
        if envelope.get("retryable"):
            code = 503
        elif any(s in e for s in ("required", "invalid", "must be", "malformed")):
            code = 400
        elif "not found" in e or "no such" in e:
            code = 404
        elif "pool" in e or "not ready" in e or "host id" in e:
            code = 503
        else:
            code = 502
        raise HTTPException(status_code=code, detail=err)
    return envelope


@router.get("/list", dependencies=[Depends(requires_permission("read:calendar"))])
async def list_events(
    since: str | None = Query(None),
    until: str | None = Query(None),
    event_type: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    type: str | None = Query(None),  # circles app sends `type` as a synonym
    offset: int = Query(0, ge=0),
    # 'asc' reads soonest first (what is coming up); newest first by default.
    order: Literal["asc", "desc"] = Query("desc"),
) -> dict:
    payload = {
        "since": since,
        "until": until,
        "event_type": event_type or type,
        "status": status,
        "limit": limit,
        "offset": offset,
        "order": order,
    }
    return _unwrap(await handle_calendar_list({k: v for k, v in payload.items() if v is not None}))


@router.get("/today", dependencies=[Depends(requires_permission("read:calendar"))])
async def today() -> dict:
    return _unwrap(await handle_calendar_today({}))


@router.get("/read/{event_id}", dependencies=[Depends(requires_permission("read:calendar"))])
async def read_event(event_id: str) -> dict:
    return _unwrap(await handle_calendar_read({"event_id": event_id}))


@router.get("/events/{event_id}/runs", dependencies=[Depends(requires_permission("read:calendar"))])
async def event_runs(
    event_id: str,
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Execution history (calendar_event_runs) for a scheduled_task event,
    newest-first. event_id is the UUID the calendar UI carries; the handler
    resolves it to the integer PK the run-record table keys on, entity-scoped.
    With `limit`, one page of it, the count of every run and where the next
    page starts; without, every run."""
    payload: dict = {"event_id": event_id, "offset": offset}
    if limit is not None:
        payload["limit"] = limit
    return _unwrap(await handle_calendar_runs(payload))


@router.post("/cancel/{event_id}", dependencies=[Depends(requires_permission("write:calendar"))])
async def cancel_event(event_id: str, reason: str | None = Query(None)) -> dict:
    return _unwrap(await handle_calendar_cancel({"event_id": event_id, "reason": reason}))


@router.post("/complete/{event_id}", dependencies=[Depends(requires_permission("write:calendar"))])
async def complete_event(event_id: str) -> dict:
    return _unwrap(await handle_calendar_complete({"event_id": event_id}))


@router.post("", dependencies=[Depends(requires_permission("write:calendar"))])
async def create_event(body: CalendarEventCreate) -> dict:
    """User-composed calendar event. Thin HTTP wrapper over the bridge
    `handle_calendar_append` handler that the platform's dispatch
    path already uses. Lets the Circles app's compose modal write directly
    to the pod's calendar_events table without round-tripping the bridge."""
    return _unwrap(await handle_calendar_append({
        k: v for k, v in body.model_dump().items() if v is not None
    }))
