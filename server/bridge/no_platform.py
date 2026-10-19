"""What a standalone Host has in place of the platform bridge.

Code throughout the Host reaches the platform through one client object
(server.main.get_bridge_client()). A C-Series Host has no platform, so that object
is a NoPlatform: the same interface as a BridgeClient that has never
connected. Every request answers the transport-failure envelope the callers
already handle ({"ok": False, "error_kind": "infra"}), nothing is ever sent
anywhere, and nothing can be pushed in.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Optional

from server.bridge.bridge_client import BridgeUnavailable

logger = logging.getLogger("cseries.no_platform")

_NOT_CONNECTED = {"ok": False, "error": "no platform: this Host runs standalone",
                  "error_kind": "infra"}


class NoPlatform:
    connected = False

    @property
    def status(self) -> dict:
        return {"connected": False, "reconnecting": False, "host": None, "port": None,
                "last_heartbeat": 0.0, "uptime": 0, "handlers": [], "standalone": True}

    def register_handler(self, action: str, fn) -> None:
        # Nothing can arrive, so nothing is kept.
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def request_full(self, op: str, params: dict = None) -> dict:
        logger.debug("no platform: %s not sent", op)
        return dict(_NOT_CONNECTED)

    async def request_stream(self, op: str, params: dict, on_delta) -> dict:
        return await self.request_full(op, params)

    async def request(self, op: str, params: dict = None, *,
                      raise_on_infra: bool = False) -> Optional[dict]:
        if raise_on_infra:
            raise BridgeUnavailable(op, _NOT_CONNECTED["error"])
        return None

    @contextlib.asynccontextmanager
    async def busy(self):
        yield
