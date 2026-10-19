"""This Host's platform grant (infra.codebase), mirrored from the platform.

The authority is the CENTRAL entity_domain_access row: the bridge refuses every
op that acts on the platform without it (the bridge adapter's
_refuse_platform_op). Everything in this pod that decides what a
Host is SHOWN reads the pod's own row instead — the tool list and the
inventory (runtime/tools.py), the per-call capability check
(runtime/meta_tool_caps.py) and the prompt (runtime/prompt_builder.py). Until
2026-10-04 every pod seeded that row as admin for itself, so every Host was
listed the platform's tools.

This loop keeps the pod row a copy of the central one: ask the bridge for this
Host's own level (platform.grant), then write it, or delete the row when the
platform says there is none. Only a definite answer changes anything — an old
bridge that does not know the op, a bridge outage or a refused read leave the
row as it is, because the bridge enforces the central grant either way and an
outage is not a revoke.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("privatae.platform_grant_sync")

STARTUP_DELAY_SECONDS = 30
INTERVAL_SECONDS = 300
_LEVELS = ("read", "read_write", "execute", "admin")
NOT_CONNECTED = "unchanged: bridge not connected"


async def sync_once(pool, bridge_client, entity_id: int) -> str:
    """One mirror pass. Returns what it did, for the log."""
    if bridge_client is None or not getattr(bridge_client, "_connected", False):
        return NOT_CONNECTED
    env = await bridge_client.request_full("platform.grant", {})
    data = env.get("data") if isinstance(env, dict) else None
    if not (isinstance(env, dict) and env.get("ok") and isinstance(data, dict) and "level" in data):
        err = env.get("error") if isinstance(env, dict) else env
        return f"unchanged: {str(err)[:160]}"
    level = data.get("level")
    async with pool.acquire() as conn:
        if level in _LEVELS:
            await conn.execute(
                """INSERT INTO entity_domain_access
                       (entity_id, domain_id, access_level, granted_by, grant_reason)
                   VALUES ($1, 'infra.codebase', $2, 'platform', 'mirror of the platform grant')
                   ON CONFLICT (entity_id, domain_id) DO UPDATE
                     SET access_level = EXCLUDED.access_level,
                         granted_by = EXCLUDED.granted_by,
                         grant_reason = EXCLUDED.grant_reason,
                         updated_at = NOW()
                   WHERE entity_domain_access.access_level <> EXCLUDED.access_level""",
                int(entity_id), level)
            return f"mirrored: {level}"
        status = await conn.execute(
            "DELETE FROM entity_domain_access WHERE entity_id = $1 AND domain_id = 'infra.codebase'",
            int(entity_id))
        return f"none on the platform: {status}"


async def run_loop(pool, bridge_client, entity_id: int) -> None:
    """Background task spawned from lifespan. Best-effort — never fatal."""
    try:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    last = None
    while True:
        outcome = None
        try:
            outcome = await sync_once(pool, bridge_client, entity_id)
            if outcome != last:
                log.info("platform grant (entity %d): %s", entity_id, outcome)
                last = outcome
        except Exception as e:
            log.warning("platform grant sync error: %s", e)
        # Not connected yet (a pod booting while the bridge is slow to answer):
        # look again soon rather than a full interval later. The check is local,
        # so nothing is sent; any answer from the bridge goes back to the interval.
        try:
            await asyncio.sleep(STARTUP_DELAY_SECONDS if outcome == NOT_CONNECTED
                                else INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
