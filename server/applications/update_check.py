"""
Background app-update-check loop.

Once a day (and once at startup, after a brief delay so the bridge is
warm), this task:

  1. Asks the store for the public catalog via ``appstore.list``.
  2. Reads the local registry's installed apps.
  3. For every app where ``installed_version != latest_version`` AND the
     entity has NOT already been notified about this specific
     installed→latest pair, writes a ``system_notification`` inbox
     message via ``inbox.send`` through the bridge.

Notification suppression: we only message ONCE per (app_id, installed,
available) tuple, so the user isn't spammed every day for an update they
chose to defer. The tuple is keyed in a small ``update_notifications``
table; clearing the row (e.g. on actual install) lets a future newer
version fire again.

"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from server import brain_service
from server.applications import registry

if TYPE_CHECKING:
    from server.bridge.bridge_client import BridgeClient


log = logging.getLogger("privatae.applications.update_check")

STARTUP_DELAY_SECONDS = 60       # let the bridge connect, factory install, etc.
INTERVAL_SECONDS = 24 * 60 * 60  # daily


async def _already_notified(pool, app_id: str, installed: str, available: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM update_notifications "
            " WHERE app_id=$1 AND installed_version=$2 AND available_version=$3",
            app_id, installed, available,
        )
    return bool(row)


async def _mark_notified(pool, app_id: str, installed: str, available: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO update_notifications (app_id, installed_version, available_version)
               VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""",
            app_id, installed, available,
        )


async def _check_once(bc: "BridgeClient | None") -> dict:
    """One pass: fetch catalog, diff with installed, emit notifications."""
    pool = brain_service.get_pool()
    if pool is None:
        log.debug("update-check: brain pool not ready yet")
        return {"status": "pool_not_ready"}
    if bc is None or not getattr(bc, "_connected", False):
        log.debug("update-check: bridge not connected yet")
        return {"status": "bridge_not_ready"}

    # Fetch catalog: the network's list with the free public catalog's apps
    # added (applications/public_store.py). The notification below still
    # needs the bridge, which is why the check above stays on it.
    from server.applications.public_store import store_client
    try:
        envelope = await store_client(bc).request_full("appstore.list", {})
    except Exception as exc:
        log.warning("update-check: appstore.list failed: %s", exc)
        return {"status": "store_unreachable", "error": str(exc)}
    if not envelope.get("ok"):
        log.warning("update-check: appstore.list returned error: %s",
                    envelope.get("error"))
        return {"status": "store_error", "error": envelope.get("error")}
    catalog = (envelope.get("data") or {}).get("apps", [])
    latest_by_id = {a["app_id"]: a.get("latest_version") for a in catalog
                    if a.get("latest_version")}

    # Read installed.
    installed = await registry.list_active(pool)
    notified_count = 0
    skipped_already = 0
    up_to_date = 0
    not_in_catalog = 0

    entity_id = brain_service._entity_id

    for app in installed:
        latest = latest_by_id.get(app.id)
        if latest is None:
            not_in_catalog += 1
            continue
        if latest == app.version:
            up_to_date += 1
            continue
        # Drift detected.
        if await _already_notified(pool, app.id, app.version, latest):
            skipped_already += 1
            continue

        await _send_notification(bc, entity_id, app.id, app.name or app.id,
                                 app.version, latest)
        await _mark_notified(pool, app.id, app.version, latest)
        notified_count += 1

    summary = {
        "status": "ok",
        "installed_count": len(installed),
        "catalog_count": len(latest_by_id),
        "notified": notified_count,
        "skipped_already_notified": skipped_already,
        "up_to_date": up_to_date,
        "not_in_catalog": not_in_catalog,
    }
    log.info("update-check pass: %s", summary)
    return summary


async def _send_notification(
    bc: "BridgeClient",
    entity_id: int,
    app_id: str,
    app_name: str,
    installed: str,
    available: str,
) -> None:
    """Drop a system_notification email about the available update."""
    subject = f"{app_name} v{available} is available"
    body = (
        f"# {app_name} v{available} is available\n\n"
        f"You're currently on **v{installed}**. The App Store has **v{available}**.\n\n"
        f"Open the **App Store** and click **Update** on {app_name} to install it.\n\n"
        f"_This message fires once per available version, not daily._"
    )
    params = {
        "to_entity_id": entity_id,
        "from_address": "cseries@localhost",
        "subject": subject,
        "body": body,
        "body_format": "text/markdown",
        "message_kind": "system_notification",
        "payload": {
            "event_type": "app_update_available",
            "app_id": app_id,
            "installed_version": installed,
            "available_version": available,
        },
    }
    try:
        envelope = await bc.request_full("inbox.send", params)
    except Exception as exc:
        log.warning("update-check: inbox.send failed for %s: %s", app_id, exc)
        return
    if not envelope.get("ok"):
        log.warning("update-check: inbox.send envelope error for %s: %s",
                    app_id, envelope.get("error"))


async def run_loop(bc: "BridgeClient | None") -> None:
    """Long-running task. Yields immediately for STARTUP_DELAY then runs
    a check every INTERVAL_SECONDS forever."""
    try:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    while True:
        try:
            await _check_once(bc)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("update-check pass crashed; will retry next interval")
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
