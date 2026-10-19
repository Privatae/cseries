"""
Gmail provider — historyId incremental sync on top of _google_base.

All OAuth plumbing, token refresh, and revoke live in the shared base.
This module owns Gmail-specific concerns only:
  - cursor priming (latest historyId on new connection)
  - per-tick sync (history.list since cursor, fetch new message metadata)
  - signal emission (portal_access with record_count)

Bodies are never stored — only From/To/Subject/Date plus a short snippet.
The entity reads this mailbox on behalf of the user; entities don't need
full-body access to narrate "3 new Gmail messages since morning."
"""

from __future__ import annotations

import logging
from typing import Any

from server.integrations.core import egress, signals, storage
from server.integrations.providers import _google_base as g

logger = logging.getLogger("privatae.pc.integrations.gmail")

PROVIDER_KEY = "gmail"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


# ── Public provider surface ────────────────────────────────

async def build_authorize_url(user_id: str, dashboard_origin: str) -> str | None:
    return await g.build_authorize_url(PROVIDER_KEY, user_id, dashboard_origin)


async def handle_callback(code: str, state: str, dashboard_origin: str) -> dict[str, Any]:
    res = await g.handle_callback(
        PROVIDER_KEY, code, state, dashboard_origin,
        after_exchange=_prime_cursor,
    )
    if res.get("ok") and res.get("connection_id"):
        await _ensure_default_channel(res["connection_id"])
    return res


async def _ensure_default_channel(connection_id: str) -> None:
    """Reach-out's default channel: once Gmail is connected, the host can reach its person through
    it — their own mailbox, to their own address. Run here, at the moment of
    consent, and nowhere else; on a reconnect it moves this mailbox's channels
    onto the new connection rather than building a second one
    (server/reachout/defaults.py says why).

    Never allowed to fail the connection itself — and never silent either: when
    the channel cannot be built the user reads why in their inbox, because a
    doorbell that quietly does not exist is worse than none."""
    try:
        from server import brain_service
        from server.reachout.defaults import ensure_gmail_channel
        pool = brain_service.get_pool()
        if pool is None:
            # No database, so no inbox to say it in either.
            logger.warning("gmail: no database yet — no reach-out channel for %s",
                           connection_id)
            return
        await ensure_gmail_channel(pool, int(brain_service._entity_id), connection_id)
    except Exception as e:  # noqa: BLE001 — the mailbox is connected either way
        logger.warning("gmail: default reach-out channel not built: %s", e)
        await _say_the_channel_failed(e, connection_id)


async def _say_the_channel_failed(exc: Exception, connection_id: str) -> None:
    """One inbox line saying the mailbox is connected and the channel is not —
    with the advice that can actually work. When the CONNECTION is what was
    refused (no account address recorded, not active), the host's own build
    would hit the same refusal, so the only fix is reconnecting; otherwise the
    host can build the channel on request."""
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        from server.deep_link import app_link
        from server.reachout.transports import TransportUnavailable, gmail_connection
        try:
            await gmail_connection(connection_id)
            advice = ("Your mailbox itself is connected. Ask your Host to set up "
                      "email reach-out and it will build the channel.")
        except TransportUnavailable:
            advice = ("The connection itself cannot carry email, so connect Gmail "
                      "again; the channel is built when that works.")
        await handle_inbox_append({
            "subject": "Gmail is connected, but your Host cannot email you yet",
            "body": f"Setting up the email channel failed: {str(exc)[:300]}. {advice}",
            "message_kind": "system_notification",
            "payload": {"event": "reachout_channel_failed",
                        "link": app_link("circles")},
        })
    except Exception as e:  # noqa: BLE001 — nothing further to fall back to
        logger.warning("gmail: could not tell the user the channel failed: %s", e)


async def revoke(connection_id: str) -> dict[str, Any]:
    return await g.revoke_google(connection_id)


async def run_sync_loop(connection_id: str, interval_s: int = 45) -> None:
    await g.run_sync_loop(connection_id, sync, interval_s, PROVIDER_KEY)


# ── Gmail-specific: cursor priming ─────────────────────────

async def _prime_cursor(connection_id: str, tok: dict[str, Any]) -> None:
    """Gmail history.list needs a starting historyId — use the current
    latest so the first poll sees only genuinely new messages."""
    access_token = tok.get("access_token")
    if not access_token:
        return
    hid = await _fetch_latest_history_id(access_token)
    if hid:
        await storage.upsert_sync(
            connection_id=connection_id, resource="message",
            mode="cursor", cursor=str(hid),
        )


async def _fetch_latest_history_id(access_token: str) -> str | None:
    try:
        async with egress.session() as s:
            async with s.get(
                f"{GMAIL_API}/users/me/profile",
                headers=await g.bearer_headers(access_token),
                timeout=15,
            ) as r:
                if r.status != 200:
                    return None
                j = await r.json()
                return j.get("historyId")
    except Exception:
        return None


# ── Gmail-specific: per-tick sync ──────────────────────────

async def sync(connection_id: str) -> dict[str, Any]:
    """Poll for new messages since the stored cursor. Emit one signal per
    poll tick (not one per message) — aggregated by record_count."""
    access_token = await g.ensure_fresh_token(connection_id)
    if not access_token:
        return {"ok": False, "error": "no_access_token"}

    sync_row = await storage.get_sync(connection_id, "message") or {}
    cursor = sync_row.get("cursor")
    if not cursor:
        hid = await _fetch_latest_history_id(access_token)
        if hid:
            await storage.upsert_sync(connection_id, "message", "cursor", str(hid),
                                last_run_at=g.now_iso())
        return {"ok": True, "new": 0, "primed": bool(hid)}

    hist = await _fetch_history_since(access_token, cursor)
    if hist is None:
        await storage.upsert_sync(connection_id, "message", "cursor", cursor,
                            last_run_at=g.now_iso(), last_error="history fetch failed")
        return {"ok": False, "error": "history_fetch_failed"}
    if hist.get("_needs_rebootstrap"):
        hid = await _fetch_latest_history_id(access_token)
        await storage.upsert_sync(connection_id, "message", "cursor", str(hid or cursor),
                            last_run_at=g.now_iso(), last_error="cursor_expired_rebootstrap")
        return {"ok": True, "rebootstrapped": True}

    new_msg_ids: list[str] = []
    for h in hist.get("history", []) or []:
        for added in h.get("messagesAdded", []) or []:
            mid = (added.get("message") or {}).get("id")
            if mid:
                new_msg_ids.append(mid)
    seen: set[str] = set()
    new_msg_ids = [m for m in new_msg_ids if not (m in seen or seen.add(m))]

    for mid in new_msg_ids:
        meta = await _fetch_message_meta(access_token, mid)
        if not meta:
            continue
        headers = _headers_dict(meta)
        await storage.upsert_record({
            "id": f"gmail:{meta.get('id')}",
            "connection_id": connection_id,
            "resource": "message",
            "canonical_model": "Email",
            "data": {
                "gmail_id": meta.get("id"),
                "thread_id": meta.get("threadId"),
                "snippet": (meta.get("snippet") or "")[:280],
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "label_ids": meta.get("labelIds") or [],
            },
            "raw_json": None,
            "received_at": g.now_iso(),
        })

    new_cursor = hist.get("historyId") or cursor
    await storage.upsert_sync(connection_id, "message", "cursor", str(new_cursor),
                        last_run_at=g.now_iso(), last_error=None)
    await storage.update_connection(connection_id, last_sync_at=g.now_iso(), last_error=None)

    if new_msg_ids:
        row = await storage.get_connection(connection_id) or {}
        account = (row.get("connection_config") or {}).get("account_email")
        signals.emit_portal_access(
            connection_id=connection_id,
            connection_kind="email",
            connection_provider="gmail",
            operation="read",
            source_display="Gmail",
            record_count=len(new_msg_ids),
            extra={"account_id": account} if account else None,
            content=f"{len(new_msg_ids)} new Gmail message(s)",
        )

    return {"ok": True, "new": len(new_msg_ids), "cursor": new_cursor}


async def _fetch_history_since(access_token: str, start_history_id: str) -> dict[str, Any] | None:
    try:
        async with egress.session() as s:
            async with s.get(
                f"{GMAIL_API}/users/me/history",
                headers=await g.bearer_headers(access_token),
                params={
                    "startHistoryId": start_history_id,
                    "historyTypes": "messageAdded",
                    "maxResults": "100",
                },
                timeout=30,
            ) as r:
                if r.status == 404:
                    return {"_needs_rebootstrap": True}
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        logger.warning("gmail history.list failed: %s", e)
        return None


async def _fetch_message_meta(access_token: str, msg_id: str) -> dict[str, Any] | None:
    try:
        async with egress.session() as s:
            async with s.get(
                f"{GMAIL_API}/users/me/messages/{msg_id}",
                headers=await g.bearer_headers(access_token),
                params={"format": "metadata",
                        "metadataHeaders": "From,To,Subject,Date"},
                timeout=20,
            ) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        return None


def _headers_dict(msg: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in (msg.get("payload") or {}).get("headers", []) or []:
        name = h.get("name", "").lower()
        if name in ("from", "to", "subject", "date"):
            out[name] = h.get("value", "")
    return out
