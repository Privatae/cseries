"""
Per-app storage planes.

Three planes; each has a different lifecycle relative to install/uninstall:

  1. Bundle (read-only)        — /var/lib/cseries/apps/<id>/
                                  Extracted from the .cseries at install.
                                  Wiped on uninstall. Apps never write here.

  2. Config (small, structured) — PG table `app_config` keyed by
                                  (app_id, key). JSON values, max 64 KiB.
                                  Survives uninstall (orphaned, reattaches
                                  on reinstall). Apps reach it via the
                                  /api/applications/<id>/config endpoints,
                                  auto-scoped to the token's app_id.

  3. Workspace (large, free-form fs) — /var/lib/cseries/appdata/<id>/
                                  Only provisioned if the app's manifest
                                  declares storage.workspace: true.
                                  Survives uninstall.

A reinstall of the same app id reattaches both config and workspace.
DELETE /api/applications/<id>?purge=true removes both, in addition to
the bundle dir + registry row.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import asyncpg

log = logging.getLogger("privatae.applications.storage")

APPDATA_ROOT = Path("/var/lib/cseries/appdata")
# One config value is ONE jsonb value in Postgres, and this is Postgres's own
# limit on one (2^28 - 1 bytes: "total size of jsonb ... exceeds the maximum of
# 268435455 bytes"). It was 64 KiB, which refused a legitimate app's settings
# blob or long list and protected nothing: the route had already read the whole
# body. The kernel's memory is protected where it
# belongs, before the body is read (routes/applications.py CONFIG_BODY_MAX).
MAX_CONFIG_VALUE_BYTES = (1 << 28) - 1


class StorageError(ValueError):
    """Bad input to the storage API."""


# ── Config (PG) ────────────────────────────────────────────────────


async def get_config(
    pool: asyncpg.Pool, *, app_id: str, key: str
) -> object | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_config WHERE app_id = $1 AND key = $2",
            app_id, key,
        )
        if row is None:
            return None
        v = row["value"]
        return json.loads(v) if isinstance(v, str) else v


async def list_config(pool: asyncpg.Pool, *, app_id: str) -> dict:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value, updated_at FROM app_config "
            "WHERE app_id = $1 ORDER BY key",
            app_id,
        )
        out = {}
        for r in rows:
            v = r["value"]
            out[r["key"]] = json.loads(v) if isinstance(v, str) else v
        return out


async def put_config(
    pool: asyncpg.Pool, *, app_id: str, key: str, value: object,
) -> None:
    payload = json.dumps(value)
    if len(payload.encode("utf-8")) > MAX_CONFIG_VALUE_BYTES:
        raise StorageError(
            f"value too large (>{MAX_CONFIG_VALUE_BYTES} bytes, the database's own "
            "limit on one JSON value) — keep data this size in the app's files"
        )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_config (app_id, key, value, updated_at)
            VALUES ($1, $2, $3::jsonb, now())
            ON CONFLICT (app_id, key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            app_id, key, payload,
        )


async def delete_config(
    pool: asyncpg.Pool, *, app_id: str, key: str | None = None,
) -> int:
    """Remove a single key or, if key is None, every key for the app."""
    async with pool.acquire() as conn:
        if key is None:
            result = await conn.execute(
                "DELETE FROM app_config WHERE app_id = $1", app_id,
            )
        else:
            result = await conn.execute(
                "DELETE FROM app_config WHERE app_id = $1 AND key = $2",
                app_id, key,
            )
    # "DELETE N"
    parts = result.split()
    return int(parts[-1]) if len(parts) >= 2 and parts[-1].isdigit() else 0


# ── Workspace (filesystem) ─────────────────────────────────────────


def workspace_dir(app_id: str) -> Path:
    return APPDATA_ROOT / app_id


def ensure_workspace(app_id: str) -> Path:
    APPDATA_ROOT.mkdir(parents=True, exist_ok=True)
    d = workspace_dir(app_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def remove_workspace(app_id: str) -> bool:
    d = workspace_dir(app_id)
    if d.exists():
        shutil.rmtree(d)
        log.info("purged workspace for %s", app_id)
        return True
    return False


# ── Purge on uninstall ────────────────────────────────────────────


async def purge_app_data(pool: asyncpg.Pool, app_id: str) -> dict:
    """Hard-delete every survivable artifact for an app."""
    config_rows = await delete_config(pool, app_id=app_id)
    workspace_removed = remove_workspace(app_id)
    return {
        "config_rows_deleted": config_rows,
        "workspace_removed": workspace_removed,
    }
