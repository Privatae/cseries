"""
Google Drive provider — Changes API (pageToken cursor).

On every poll tick, we call `changes.list(pageToken=<cursor>)` which
returns every file change since the last token. We keep metadata only
(id, name, mime, modifiedTime, owners) — contents never leave Google.
"""

from __future__ import annotations

import logging
from typing import Any

from server.integrations.core import egress, signals, storage
from server.integrations.providers import _google_base as g

logger = logging.getLogger("privatae.pc.integrations.drive")

PROVIDER_KEY = "drive"
DRIVE_API = "https://www.googleapis.com/drive/v3"


async def build_authorize_url(user_id: str, dashboard_origin: str) -> str | None:
    return await g.build_authorize_url(PROVIDER_KEY, user_id, dashboard_origin)


async def handle_callback(code: str, state: str, dashboard_origin: str) -> dict[str, Any]:
    return await g.handle_callback(
        PROVIDER_KEY, code, state, dashboard_origin,
        after_exchange=_prime_cursor,
    )


async def revoke(connection_id: str) -> dict[str, Any]:
    return await g.revoke_google(connection_id)


async def run_sync_loop(connection_id: str, interval_s: int = 60) -> None:
    await g.run_sync_loop(connection_id, sync, interval_s, PROVIDER_KEY)


# ── Drive-specific ─────────────────────────────────────────

async def _prime_cursor(connection_id: str, tok: dict[str, Any]) -> None:
    access_token = tok.get("access_token")
    if not access_token:
        return
    try:
        async with egress.session() as s:
            async with s.get(
                f"{DRIVE_API}/changes/startPageToken",
                headers=await g.bearer_headers(access_token),
                timeout=15,
            ) as r:
                if r.status != 200:
                    return
                j = await r.json()
                token = j.get("startPageToken")
    except Exception:
        return
    if token:
        await storage.upsert_sync(connection_id, "file", "cursor", str(token))


async def sync(connection_id: str) -> dict[str, Any]:
    access_token = await g.ensure_fresh_token(connection_id)
    if not access_token:
        return {"ok": False, "error": "no_access_token"}

    sync_row = await storage.get_sync(connection_id, "file") or {}
    page_token = sync_row.get("cursor")
    if not page_token:
        await _prime_cursor(connection_id, {"access_token": access_token})
        return {"ok": True, "new": 0, "primed": True}

    new_changes: list[dict[str, Any]] = []
    next_token = page_token
    try:
        async with egress.session() as s:
            while True:
                async with s.get(
                    f"{DRIVE_API}/changes",
                    headers=await g.bearer_headers(access_token),
                    params={
                        "pageToken": next_token,
                        "pageSize": "100",
                        "fields": "nextPageToken,newStartPageToken,"
                                  "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners(emailAddress)))",
                    },
                    timeout=30,
                ) as r:
                    if r.status != 200:
                        await storage.upsert_sync(connection_id, "file", "cursor", page_token,
                                            last_run_at=g.now_iso(),
                                            last_error=f"changes.list {r.status}")
                        return {"ok": False, "error": f"drive http {r.status}"}
                    j = await r.json()
                for ch in j.get("changes") or []:
                    new_changes.append(ch)
                if j.get("nextPageToken"):
                    next_token = j["nextPageToken"]
                    continue
                next_token = j.get("newStartPageToken") or next_token
                break
    except Exception as e:
        logger.warning("drive changes.list failed: %s", e)
        return {"ok": False, "error": "drive_fetch_failed"}

    for ch in new_changes:
        if ch.get("removed"):
            continue
        f = ch.get("file") or {}
        fid = f.get("id")
        if not fid:
            continue
        await storage.upsert_record({
            "id": f"drive:{fid}",
            "connection_id": connection_id,
            "resource": "file",
            "canonical_model": "File",
            "data": {
                "drive_id": fid,
                "name": f.get("name"),
                "mime_type": f.get("mimeType"),
                "modified_time": f.get("modifiedTime"),
                "owners": [o.get("emailAddress") for o in (f.get("owners") or []) if o.get("emailAddress")],
            },
            "raw_json": None,
            "received_at": g.now_iso(),
        })

    await storage.upsert_sync(connection_id, "file", "cursor", str(next_token),
                        last_run_at=g.now_iso(), last_error=None)
    await storage.update_connection(connection_id, last_sync_at=g.now_iso(), last_error=None)

    if new_changes:
        row = await storage.get_connection(connection_id) or {}
        account = (row.get("connection_config") or {}).get("account_email")
        signals.emit_portal_access(
            connection_id=connection_id,
            connection_kind="files",
            connection_provider="drive",
            operation="read",
            source_display="Google Drive",
            record_count=len(new_changes),
            extra={"account_id": account} if account else None,
            content=f"{len(new_changes)} Drive change(s)",
        )

    return {"ok": True, "new": len(new_changes), "cursor": next_token}
