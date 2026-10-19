"""
Portal — source registry.

A "source" is a user-controlled data input the entity was given access to;
the only type is a filesystem folder on the host machine, bind-mounted into
the container.

Registry file: /app/portal/sources.yaml (persistent volume)

Schema:
  version: 1
  sources:
    - id: documents
      type: folder
      display_name: "Documents"
      host_path: "C:\\Users\\alex\\Documents"
      mount_path: "/app/portal/documents"
      status: pending_mount | pending_grant | active | revoked
      added_at: <iso>

Read-only here, plus removing an entry. Adding a folder source (and the
CEIGAS incident it proposed, which the user approved in the Security app)
was removed on 2026-10-01: nothing called it, and no live Host held a source,
an incident or a granted rule. A legacy entry can still be listed (brain/
mastercode/dcn.py reads list_sources) and deleted.
"""

from __future__ import annotations

import logging
import os
import yaml
from pathlib import Path
from typing import Any

logger = logging.getLogger("privatae.pc.portal.sources")

PORTAL_DIR = Path("/app/portal")
SOURCES_PATH = PORTAL_DIR / "sources.yaml"


def _ensure_dir() -> None:
    PORTAL_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict[str, Any]:
    _ensure_dir()
    if not SOURCES_PATH.exists():
        default = {"version": 1, "sources": []}
        save(default)
        return default
    try:
        with SOURCES_PATH.open() as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("sources.yaml must be a mapping")
        data.setdefault("version", 1)
        data.setdefault("sources", [])
        return data
    except Exception as e:
        logger.error("sources load failed, resetting: %s", e)
        default = {"version": 1, "sources": []}
        save(default)
        return default


def save(data: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = SOURCES_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, SOURCES_PATH)


def list_sources() -> list[dict[str, Any]]:
    data = load()
    out = []
    for s in data.get("sources", []):
        # Runtime augmentation: whether the mount is actually present
        if s.get("type") == "folder":
            mount = s.get("mount_path")
            s = dict(s)
            s["mounted"] = bool(mount and Path(mount).is_dir())
        out.append(s)
    return out


def delete(source_id: str) -> bool:
    data = load()
    before = len(data["sources"])
    data["sources"] = [s for s in data["sources"] if s["id"] != source_id]
    if len(data["sources"]) == before:
        return False
    save(data)
    return True
