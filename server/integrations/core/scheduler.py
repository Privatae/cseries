"""
Resume background sync loops for ACTIVE connections on app startup.

Each provider module that exports `run_sync_loop(connection_id)` gets one
task per active connection. Tasks are cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from server.integrations.core import storage

logger = logging.getLogger("privatae.pc.integrations.scheduler")

_running_tasks: dict[str, asyncio.Task] = {}


def _provider_module(key: str):
    try:
        return importlib.import_module(f"server.integrations.providers.{key}")
    except ModuleNotFoundError:
        return None


async def start_all() -> int:
    """Walk the connections table. For every ACTIVE connection whose
    provider module exports run_sync_loop, create a background task.
    Returns count of tasks started."""
    started = 0
    for row in await storage.list_connections():
        if row["status"] != "active":
            continue
        if row["id"] in _running_tasks:
            continue
        module = _provider_module(row["provider_key"])
        if not module or not hasattr(module, "run_sync_loop"):
            continue
        task = asyncio.create_task(module.run_sync_loop(row["id"]))
        _running_tasks[row["id"]] = task
        started += 1
        logger.info("sync loop resumed: provider=%s connection=%s",
                    row["provider_key"], row["id"])
    return started


async def stop_all() -> None:
    for connection_id, task in list(_running_tasks.items()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        _running_tasks.pop(connection_id, None)
    logger.info("all integration sync loops stopped")
