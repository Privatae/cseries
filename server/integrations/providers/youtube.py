"""
YouTube provider — activities.list on user's subscription feed.

Not a cursor sync — YouTube doesn't offer one for activities. We poll
with `publishedAfter` set to the last seen publishedAt timestamp. First
run stamps "now" so we don't backfill the user's entire history.

Use case: the entity can summarize what videos dropped into the user's
subs since yesterday. Useful for news and transcript
workflows. Body (transcript) is fetched on-demand by the apps, not here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from server.integrations.core import egress, signals, storage
from server.integrations.providers import _google_base as g

logger = logging.getLogger("privatae.pc.integrations.youtube")

PROVIDER_KEY = "youtube"
YT_API = "https://youtube.googleapis.com/youtube/v3"


async def build_authorize_url(user_id: str, dashboard_origin: str) -> str | None:
    return await g.build_authorize_url(PROVIDER_KEY, user_id, dashboard_origin)


async def handle_callback(code: str, state: str, dashboard_origin: str) -> dict[str, Any]:
    return await g.handle_callback(
        PROVIDER_KEY, code, state, dashboard_origin,
        after_exchange=_prime_cursor,
    )


async def revoke(connection_id: str) -> dict[str, Any]:
    return await g.revoke_google(connection_id)


async def run_sync_loop(connection_id: str, interval_s: int = 600) -> None:
    await g.run_sync_loop(connection_id, sync, interval_s, PROVIDER_KEY)


# ── YouTube-specific ───────────────────────────────────────

async def _prime_cursor(connection_id: str, tok: dict[str, Any]) -> None:
    """Stamp now() so the first real poll doesn't replay the user's whole feed."""
    await storage.upsert_sync(
        connection_id=connection_id, resource="subscription_activity",
        mode="incremental_poll",
        cursor=datetime.now(timezone.utc).isoformat(),
    )


async def sync(connection_id: str) -> dict[str, Any]:
    access_token = await g.ensure_fresh_token(connection_id)
    if not access_token:
        return {"ok": False, "error": "no_access_token"}

    sync_row = await storage.get_sync(connection_id, "subscription_activity") or {}
    published_after = sync_row.get("cursor")
    if not published_after:
        await _prime_cursor(connection_id, {})
        return {"ok": True, "new": 0, "primed": True}

    try:
        async with egress.session() as s:
            async with s.get(
                f"{YT_API}/activities",
                headers=await g.bearer_headers(access_token),
                params={
                    "part": "snippet,contentDetails",
                    "home": "true",
                    "maxResults": "50",
                    "publishedAfter": published_after,
                },
                timeout=30,
            ) as r:
                if r.status != 200:
                    return {"ok": False, "error": f"youtube http {r.status}"}
                j = await r.json()
    except Exception as e:
        logger.warning("youtube activities.list failed: %s", e)
        return {"ok": False, "error": "youtube_fetch_failed"}

    items = j.get("items") or []
    newest = published_after
    for it in items:
        snip = it.get("snippet") or {}
        content = it.get("contentDetails") or {}
        upload = content.get("upload") or {}
        video_id = (
            upload.get("videoId")
            or (content.get("playlistItem") or {}).get("resourceId", {}).get("videoId")
        )
        if not video_id:
            continue
        published_at = snip.get("publishedAt") or ""
        if published_at > newest:
            newest = published_at
        await storage.upsert_record({
            "id": f"youtube:{video_id}:{it.get('id') or published_at}",
            "connection_id": connection_id,
            "resource": "subscription_activity",
            "canonical_model": "Video",
            "data": {
                "video_id": video_id,
                "title": snip.get("title"),
                "channel_title": snip.get("channelTitle"),
                "channel_id": snip.get("channelId"),
                "published_at": published_at,
                "activity_type": snip.get("type"),
                "description": (snip.get("description") or "")[:280],
            },
            "raw_json": None,
            "received_at": g.now_iso(),
        })

    await storage.upsert_sync(connection_id, "subscription_activity", "incremental_poll",
                        cursor=str(newest),
                        last_run_at=g.now_iso(), last_error=None)
    await storage.update_connection(connection_id, last_sync_at=g.now_iso(), last_error=None)

    if items:
        row = await storage.get_connection(connection_id) or {}
        account = (row.get("connection_config") or {}).get("account_email")
        signals.emit_portal_access(
            connection_id=connection_id,
            connection_kind="video",
            connection_provider="youtube",
            operation="read",
            source_display="YouTube",
            record_count=len(items),
            extra={"account_id": account} if account else None,
            content=f"{len(items)} YouTube subscription item(s)",
        )

    return {"ok": True, "new": len(items), "cursor": newest}
