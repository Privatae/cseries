"""
Entity state route.

Surfaces the deterministic entity activity (from the signal rule engine)
to the dashboard and any other consumer that needs to render the
presence line.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends
from server.applications.middleware import requires_permission

from server.engine import rules as rules_engine

logger = logging.getLogger("privatae.pc.routes.entity")

router = APIRouter(prefix="/api/entity", tags=["entity"])

RELAY_CONFIG = Path("/etc/privatae/certs/relay.json")


def _entity_name() -> str:
    """Resolve the entity's display name (ex: 'Ada Lovelace')."""
    from server.host_display import display_name_or_host
    host_id = os.environ.get("ENTITY_ID")
    if RELAY_CONFIG.exists():
        try:
            cfg = json.loads(RELAY_CONFIG.read_text())
            return display_name_or_host(cfg.get("entity_display_name"), host_id)
        except Exception:
            pass
    return display_name_or_host(None, host_id)


async def host_name() -> str:
    """The Host's name as it is shown: the name chosen for it in
    entity_registry (set at first boot, renamed through POST /api/system/name)
    when that row holds one, else what _entity_name finds. The row comes first
    because a rename writes only the row; a standalone Host has no relay.json
    at all, and on a platform Host the file would keep showing the name the
    Host was provisioned with."""
    from server import brain_service
    from server.host_display import is_placeholder_name
    try:
        pool = brain_service.get_pool()
        if pool is not None:
            name = await pool.fetchval(
                "SELECT display_name FROM entity_registry WHERE id = $1",
                brain_service._entity_id)
            if not is_placeholder_name(name):
                return str(name).strip()
    except Exception:  # noqa: BLE001 — the file's answer is still a name
        pass
    return _entity_name()


@router.get("/state", dependencies=[Depends(requires_permission("read:session"))])
async def entity_state():
    """Current entity state — deterministic, derived from recent signals.

    Zero model tokens. The sentence is a template fill over matched
    signals; C-Series's design principle is software-first, inference-last.
    """
    name = await host_name()
    state = rules_engine.evaluate(entity_name=name)
    state["entity_name"] = name
    return state


@router.get("/signals", dependencies=[Depends(requires_permission("read:signals"))])
async def entity_signals(
    limit: int = 100,
    type: str | None = None,
    since_s: int | None = None,
):
    """Unified signal feed. Merges local signals (emitted by the container's
    own routes, job scheduler, code exec, etc.) with the CEIGAS signal cache
    (populated by the bridge via the tunnel — domain_signal_log mirror).

    Newest first. Accepts optional type filter + window in seconds.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    from datetime import datetime as _dt

    paths = [
        _Path("/app/engine/local-signals.jsonl"),
        _Path("/app/ceigas/signals.cache.jsonl"),
    ]

    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        try:
            lines = p.read_text().splitlines()[-2000:]
        except Exception:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                s = _json.loads(raw)
            except Exception:
                continue
            s["_source"] = p.name
            rows.append(s)

    # Filter
    if type:
        rows = [r for r in rows if r.get("signal_type") == type]
    if since_s:
        cutoff = _time.time() - max(1, int(since_s))
        def _ts(r):
            v = r.get("created_at") or r.get("ts")
            if not v:
                return 0
            try:
                return _dt.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0
        rows = [r for r in rows if _ts(r) >= cutoff]

    # Sort newest first
    def _key(r):
        v = r.get("created_at") or r.get("ts") or ""
        return str(v)
    rows.sort(key=_key, reverse=True)

    # Truncate
    rows = rows[:max(1, min(int(limit), 500))]

    return {
        "signals": rows,
        "count": len(rows),
    }
