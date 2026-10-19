"""
CEIGAS app routes.

The Security system app (system-apps/security) calls these endpoints to
render Permissions and the governance part of History.

Data sources:
  - Policy: /app/ceigas/policy.yaml (local materialized view)
  - Signals: local cache at /app/ceigas/signals.cache.jsonl (materialized
    from bridge forwards; authoritative copy lives in the platform's
    domain_signal_log).

The incident lane (GET /incidents, POST /incidents/{id}/grant and /deny,
POST /policy/revoke) was removed on 2026-10-01. Its only producer was the
Portal's add-folder route, which nothing called; no live Host held an
incident or a granted rule; and on every Host the three writes were always
refused at the CEIGAS gate (core.governance is read-only to a Host), each
refusal costing trust.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from server.applications.middleware import requires_permission

from server.ceigas import policy as policy_store

logger = logging.getLogger("privatae.pc.routes.ceigas")

router = APIRouter(prefix="/api/ceigas", tags=["ceigas"])

SIGNALS_CACHE = Path("/app/ceigas/signals.cache.jsonl")


def _load_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        logger.warning("failed to read %s: %s", path, e)
        return []
    if limit is not None and limit > 0:
        out = out[-limit:]
    # Newest first
    out.reverse()
    return out


@router.get("/policy", dependencies=[Depends(requires_permission("read:policy"))])
async def get_policy():
    """Return the current materialized policy YAML as JSON."""
    return policy_store.load_policy()


@router.get("/signals", dependencies=[Depends(requires_permission("read:signals"))])
async def get_signals(limit: int = 200, signal_type: str | None = None):
    """Return recent CEIGAS signals. Local cache — truth lives on the platform."""
    signals = _load_jsonl(SIGNALS_CACHE, limit=max(1, min(limit, 1000)))
    if signal_type:
        signals = [s for s in signals if s.get("signal_type") == signal_type]
    return {"signals": signals, "count": len(signals)}
