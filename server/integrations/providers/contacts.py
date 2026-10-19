"""
Google Contacts provider — people.connections.list with syncToken.

Read-only on the user's personal contacts. Names, emails, phones,
organizations — no photos, no addresses (address book sensitivity).
"""

from __future__ import annotations

import logging
from typing import Any

from server.integrations.core import egress, signals, storage
from server.integrations.providers import _google_base as g

logger = logging.getLogger("privatae.pc.integrations.contacts")

PROVIDER_KEY = "contacts"
PEOPLE_API = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,metadata"


async def build_authorize_url(user_id: str, dashboard_origin: str) -> str | None:
    return await g.build_authorize_url(PROVIDER_KEY, user_id, dashboard_origin)


async def handle_callback(code: str, state: str, dashboard_origin: str) -> dict[str, Any]:
    return await g.handle_callback(PROVIDER_KEY, code, state, dashboard_origin)


async def revoke(connection_id: str) -> dict[str, Any]:
    return await g.revoke_google(connection_id)


async def run_sync_loop(connection_id: str, interval_s: int = 600) -> None:
    await g.run_sync_loop(connection_id, sync, interval_s, PROVIDER_KEY)


# ── Contacts-specific ─────────────────────────────────────

async def sync(connection_id: str) -> dict[str, Any]:
    access_token = await g.ensure_fresh_token(connection_id)
    if not access_token:
        return {"ok": False, "error": "no_access_token"}

    sync_row = await storage.get_sync(connection_id, "person") or {}
    sync_token = sync_row.get("cursor")

    params: dict[str, str] = {
        "personFields": PERSON_FIELDS,
        "pageSize": "200",
        "requestSyncToken": "true",
    }
    if sync_token:
        params["syncToken"] = sync_token

    try:
        async with egress.session() as s:
            async with s.get(
                f"{PEOPLE_API}/people/me/connections",
                headers=await g.bearer_headers(access_token),
                params=params,
                timeout=30,
            ) as r:
                if r.status == 410:
                    await storage.upsert_sync(connection_id, "person", "cursor", None,
                                        last_run_at=g.now_iso(),
                                        last_error="sync_token_invalidated")
                    return {"ok": True, "rebootstrapped": True}
                if r.status != 200:
                    return {"ok": False, "error": f"contacts http {r.status}"}
                j = await r.json()
    except Exception as e:
        logger.warning("contacts people.connections failed: %s", e)
        return {"ok": False, "error": "contacts_fetch_failed"}

    people = j.get("connections") or []
    for p in people:
        rid = p.get("resourceName")
        if not rid:
            continue
        names = p.get("names") or []
        emails = p.get("emailAddresses") or []
        phones = p.get("phoneNumbers") or []
        orgs = p.get("organizations") or []
        await storage.upsert_record({
            "id": f"contacts:{rid}",
            "connection_id": connection_id,
            "resource": "person",
            "canonical_model": "Contact",
            "data": {
                "resource_name": rid,
                "display_name": (names[0].get("displayName") if names else None),
                "given_name": (names[0].get("givenName") if names else None),
                "family_name": (names[0].get("familyName") if names else None),
                "email": (emails[0].get("value") if emails else None),
                "phone": (phones[0].get("value") if phones else None),
                "organization": (orgs[0].get("name") if orgs else None),
                "title": (orgs[0].get("title") if orgs else None),
            },
            "raw_json": None,
            "received_at": g.now_iso(),
        })

    next_token = j.get("nextSyncToken") or sync_token
    await storage.upsert_sync(connection_id, "person", "cursor", str(next_token) if next_token else None,
                        last_run_at=g.now_iso(), last_error=None)
    await storage.update_connection(connection_id, last_sync_at=g.now_iso(), last_error=None)

    if people:
        row = await storage.get_connection(connection_id) or {}
        account = (row.get("connection_config") or {}).get("account_email")
        signals.emit_portal_access(
            connection_id=connection_id,
            connection_kind="contacts",
            connection_provider="contacts",
            operation="read",
            source_display="Google Contacts",
            record_count=len(people),
            extra={"account_id": account} if account else None,
            content=f"{len(people)} contact update(s)",
        )

    return {"ok": True, "new": len(people), "cursor": next_token}
