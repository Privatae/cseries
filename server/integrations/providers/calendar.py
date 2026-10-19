"""
Google Calendar provider — events.list with syncToken cursor.

Incremental sync: `events.list(syncToken=...)` returns only events that
changed since the last token. First call uses no syncToken and stores
the returned `nextSyncToken`. Later calls ride that token.
"""

from __future__ import annotations

import logging
from typing import Any

from server.integrations.core import egress, signals, storage
from server.integrations.providers import _google_base as g

logger = logging.getLogger("privatae.pc.integrations.calendar")

PROVIDER_KEY = "calendar"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"


async def build_authorize_url(user_id: str, dashboard_origin: str) -> str | None:
    return await g.build_authorize_url(PROVIDER_KEY, user_id, dashboard_origin)


async def handle_callback(code: str, state: str, dashboard_origin: str) -> dict[str, Any]:
    return await g.handle_callback(PROVIDER_KEY, code, state, dashboard_origin)


async def revoke(connection_id: str) -> dict[str, Any]:
    return await g.revoke_google(connection_id)


async def run_sync_loop(connection_id: str, interval_s: int = 90) -> None:
    await g.run_sync_loop(connection_id, sync, interval_s, PROVIDER_KEY)


# ── Calendar-specific ─────────────────────────────────────

async def sync(connection_id: str) -> dict[str, Any]:
    access_token = await g.ensure_fresh_token(connection_id)
    if not access_token:
        return {"ok": False, "error": "no_access_token"}

    sync_row = await storage.get_sync(connection_id, "event") or {}
    sync_token = sync_row.get("cursor")

    params: dict[str, str] = {"maxResults": "100", "singleEvents": "true"}
    if sync_token:
        params["syncToken"] = sync_token
    else:
        # First run — start "now" rather than backfill history
        from datetime import datetime, timezone
        params["timeMin"] = datetime.now(timezone.utc).isoformat()

    try:
        async with egress.session() as s:
            async with s.get(
                f"{CALENDAR_API}/calendars/primary/events",
                headers=await g.bearer_headers(access_token),
                params=params,
                timeout=30,
            ) as r:
                if r.status == 410:
                    # syncToken invalidated — re-bootstrap
                    await storage.upsert_sync(connection_id, "event", "cursor", None,
                                        last_run_at=g.now_iso(),
                                        last_error="sync_token_invalidated")
                    return {"ok": True, "rebootstrapped": True}
                if r.status != 200:
                    return {"ok": False, "error": f"calendar http {r.status}"}
                j = await r.json()
    except Exception as e:
        logger.warning("calendar events.list failed: %s", e)
        return {"ok": False, "error": "calendar_fetch_failed"}

    events = j.get("items") or []
    for e in events:
        eid = e.get("id")
        if not eid:
            continue
        start = (e.get("start") or {})
        end = (e.get("end") or {})
        await storage.upsert_record({
            "id": f"calendar:{eid}",
            "connection_id": connection_id,
            "resource": "event",
            "canonical_model": "CalendarEvent",
            "data": {
                "calendar_id": eid,
                "summary": e.get("summary"),
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "location": e.get("location"),
                "attendee_count": len(e.get("attendees") or []),
                "status": e.get("status"),
            },
            "raw_json": None,
            "received_at": g.now_iso(),
        })

    next_token = j.get("nextSyncToken") or sync_token
    await storage.upsert_sync(connection_id, "event", "cursor", str(next_token) if next_token else None,
                        last_run_at=g.now_iso(), last_error=None)
    await storage.update_connection(connection_id, last_sync_at=g.now_iso(), last_error=None)

    if events:
        row = await storage.get_connection(connection_id) or {}
        account = (row.get("connection_config") or {}).get("account_email")
        signals.emit_portal_access(
            connection_id=connection_id,
            connection_kind="calendar",
            connection_provider="calendar",
            operation="read",
            source_display="Google Calendar",
            record_count=len(events),
            extra={"account_id": account} if account else None,
            content=f"{len(events)} calendar update(s)",
        )

    return {"ok": True, "new": len(events), "cursor": next_token}
