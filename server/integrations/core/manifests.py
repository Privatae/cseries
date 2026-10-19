"""
Manifest loader — parses YAML provider definitions into Provider objects.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from server.integrations.core.models import Provider, ResourceSpec, RateLimit

logger = logging.getLogger("privatae.pc.integrations.manifests")

MANIFESTS_DIR = Path("/app/server/integrations/manifests")

_cache: dict[str, Provider] = {}
_cache_mtime: float = 0.0


def _manifest_to_provider(data: dict[str, Any]) -> Provider:
    if not isinstance(data, dict):
        raise ValueError("manifest must be a YAML mapping")
    if not data.get("key"):
        raise ValueError("manifest missing 'key'")

    resources = []
    for r in data.get("resources", []) or []:
        resources.append(
            ResourceSpec(
                name=r.get("name", ""),
                canonical_model=r.get("canonical_model"),
                sync=r.get("sync", {}) or {},
                signal=r.get("signal", {}) or {},
            )
        )

    rl = None
    proxy = data.get("proxy") or {}
    if "rate_limit" in proxy:
        rl_cfg = proxy["rate_limit"] or {}
        rl = RateLimit(
            requests_per_minute=int(rl_cfg.get("requests_per_minute", 60)),
            concurrent=int(rl_cfg.get("concurrent", 4)),
        )

    return Provider(
        key=data["key"],
        display_name=data.get("display_name", data["key"]),
        categories=list(data.get("categories") or []),
        connection_kind=data.get("connection_kind") or data["key"],
        connection_provider=data.get("connection_provider") or "",
        auth=data.get("auth") or {},
        proxy=proxy,
        verification=data.get("verification") or {},
        resources=resources,
        webhooks=data.get("webhooks") or {},
        post_connection=list(data.get("post_connection") or []),
        reauth_triggers=list(data.get("reauth_triggers") or []),
        rate_limit=rl,
        realtime=data.get("realtime") or {},
    )


def _dir_mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    latest = 0.0
    for p in path.glob("*.yaml"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return latest


def load_all(force: bool = False) -> dict[str, Provider]:
    """Load all provider manifests. Cached; re-scans when any YAML changes."""
    global _cache, _cache_mtime
    current = _dir_mtime(MANIFESTS_DIR)
    if not force and _cache and current == _cache_mtime:
        return _cache
    providers: dict[str, Provider] = {}
    if not MANIFESTS_DIR.exists():
        _cache = providers
        _cache_mtime = current
        return providers
    for path in sorted(MANIFESTS_DIR.glob("*.yaml")):
        try:
            with path.open() as f:
                data = yaml.safe_load(f)
            provider = _manifest_to_provider(data)
            providers[provider.key] = provider
        except Exception as e:
            logger.warning("skipping manifest %s: %s", path.name, e)
    _cache = providers
    _cache_mtime = current
    return providers


def get(key: str) -> Provider | None:
    return load_all().get(key)


def list_keys() -> list[str]:
    return sorted(load_all().keys())
