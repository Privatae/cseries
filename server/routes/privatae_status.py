"""
Privatae platform status — transparency endpoint for the Privatae app.

Surfaces: relay connection health, inference usage, active provider/model,
recent requests, cert identity. Everything the user needs to trust the
connection between their C-Series and the platform.
"""

import json
import logging
import os
import ssl
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends
from server.applications.middleware import requires_permission

from server import brain_service

logger = logging.getLogger("privatae.pc.status")

router = APIRouter(prefix="/api/privatae", tags=["privatae"])


def _get_bridge_status() -> dict:
    """Pull connection state from the bridge client."""
    try:
        from server.main import get_bridge_client
        client = get_bridge_client()
        if client is None:
            return {"connected": False, "reconnecting": False, "host": None, "port": None}
        return client.status
    except Exception as e:
        logger.debug("Bridge status check failed: %s", e)
        return {"connected": False, "reconnecting": False, "host": None, "port": None}


def _get_cert_cn() -> str | None:
    """Read the entity cert CN for display."""
    cert_paths = [
        "/etc/ceigas/certs/entity.crt",
        "/etc/privatae/certs/entity.crt",
    ]
    for p in cert_paths:
        if os.path.exists(p):
            try:
                cert = ssl.PEM_cert_to_DER_cert(open(p).read())
                import subprocess
                out = subprocess.check_output(
                    ["openssl", "x509", "-in", p, "-noout", "-subject"],
                    text=True, stderr=subprocess.DEVNULL,
                )
                cn = out.strip().split("CN = ")[-1] if "CN = " in out else None
                return cn
            except Exception:
                pass
    return None


@router.get("/status", dependencies=[Depends(requires_permission("read:system_status"))])
async def privatae_status():
    """Full platform status snapshot for the Privatae transparency app."""
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id

    relay = _get_bridge_status()
    cert_cn = _get_cert_cn()

    # Model routing — all roles
    routing = {}
    fallback_provider = "unknown"
    if pool and entity_id:
        try:
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id
            )
            if row and row["behavioral_params"]:
                bp = row["behavioral_params"]
                if isinstance(bp, str):
                    bp = json.loads(bp)
                routing = bp.get("model_routing", {})
                fallback_provider = bp.get("inference_provider", "not set")
        except Exception:
            pass

    # Usage: the UI reads spend from the billing ledger; the per-dispatch
    # table this block summed (dispatch_log) had no writer and went 2026-10-17.
    tokens_today = 0
    requests_today = 0
    avg_latency = None
    recent: list = []

    # Per-model rates are surfaced honestly by the model picker from the live
    # catalogs (OpenRouter pricing for text, embedding pricing.py charge) — not a
    # hardcoded table. The old cost_map here was stale: its short keys never
    # matched the full OpenRouter slugs stored in model_routing, so it always fell
    # through to a single default. The cost the UI shows comes from the billing
    # ledger (actual spend), not an estimate.
    cost_per_1m = None

    return {
        "relay": relay,
        "cert_cn": cert_cn,
        "routing": routing,
        "fallback_provider": fallback_provider,
        "usage": {
            "tokens_today": tokens_today,
            "requests_today": requests_today,
            "avg_latency_ms": avg_latency,
            "cost_per_1m": cost_per_1m,
        },
        "recent_requests": recent,
        "entity_id": entity_id,
    }
