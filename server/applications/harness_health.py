"""
Background harness-credential health loop (proactive expiry notifications).

A DETERMINISTIC periodic check — NO inference, exactly like update_check /
turn_consolidator — so it respects the no-autonomous-loops rule (the forbidden
thing is an LLM loop hunting for work; this just reads credential files + pushes
an inbox notice). Every few hours, for each installed harness it reads validity +
expiry and pushes a "reconnect X" inbox notification when a credential is EXPIRED
or expiring within the warning window — proactively, BEFORE the user's next
dispatch fails (the reactive pre-dispatch + on-failure notices live in review.py).

Suppression: notify once per (harness, expires_at) per process so the sweep
doesn't spam; a fresh expiry timestamp (after reconnect) re-arms it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.bridge.bridge_client import BridgeClient

log = logging.getLogger("privatae.applications.harness_health")

STARTUP_DELAY_SECONDS = 90            # let the bridge connect first
INTERVAL_SECONDS = 6 * 60 * 60        # every 6h
WARN_WINDOW_MS = 3 * 24 * 60 * 60 * 1000   # warn up to 3 days before expiry

# (harness_id, expires_at_ms) tuples already alerted this process lifetime.
_notified: set[tuple[str, int]] = set()


async def _check_once() -> None:
    from server.routes.harnesses import (
        PROVIDERS, _creds_status, notify_harness_auth, _app_is_installed,
    )
    now_ms = int(time.time() * 1000)
    for pid, p in PROVIDERS.items():
        gate = p.get("present_if_app_installed")
        if gate and not await _app_is_installed(gate):
            continue
        st = _creds_status(p)
        if not st["present"]:
            continue  # not connected — nothing to warn about
        exp = st["expires_at"]
        expired = not st["valid"]
        expiring = (exp is not None and not expired and (exp - now_ms) <= WARN_WINDOW_MS)
        if not (expired or expiring):
            continue
        key = (pid, int(exp or 0))
        if key in _notified:
            continue
        _notified.add(key)
        await notify_harness_auth(pid, "expired" if expired else "expiring soon")
        log.info("harness_health: notified %s (%s, expires_at=%s)",
                 pid, "expired" if expired else "expiring", exp)


async def run_loop(bridge_client: "BridgeClient | None" = None) -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await _check_once()
        except Exception as exc:
            log.warning("harness_health check failed: %s", exc)
        await asyncio.sleep(INTERVAL_SECONDS)
