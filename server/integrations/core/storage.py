"""
Integrations storage — connections + syncs + canonical records, the Host's own
OAuth clients and per-app grants, in the Host's own Postgres
(migrations/v201_integrations_store_in_host_postgres.sql).

Separate from credentials (those live in the vault). These tables hold the
lifecycle metadata + all canonical data models the rule engine and apps read.
Provider raw_json stays alongside the canonical columns.

Until 2026-10-03 this was a SQLite file, /app/integrations/integrations.db;
there is no need to keep a second database. Any rows still
in that file are copied here once at startup and the file is removed
(retire_sqlite_store).

Every function is async on the pod's asyncpg pool. Values keep the shapes they
had: timestamps are ISO-8601 strings, connection_config comes back a dict,
grant methods a list or None.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("privatae.pc.integrations.storage")

#: The SQLite file this store used to be. Read once at startup by
#: retire_sqlite_store and removed; nothing writes it any more.
LEGACY_DB_PATH = Path(os.environ.get("INTEGRATIONS_LEGACY_DB", "/app/integrations/integrations.db"))

#: A pool to use instead of the brain pool — set by DB gates only.
POOL = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pool():
    """The pod's Postgres pool. Raises when it is not up yet, rather than
    handing callers a None they would fail on further in."""
    if POOL is not None:
        return POOL
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        raise RuntimeError("integrations store: the Host's database is not ready yet")
    return pool


def _affected(tag: str) -> int:
    """Row count from an asyncpg command tag ('DELETE 1', 'UPDATE 0')."""
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, TypeError):
        return 0


#: Columns update_connection / upsert_sync may set. The column names go into
#: the statement text, so they come from here and never from a caller's keys.
_CONNECTION_FIELDS = ("provider_key", "user_id", "status", "credentials_ref",
                      "connection_config", "last_sync_at", "last_error")
_SYNC_FIELDS = ("last_run_at", "next_run_at", "last_error")


def _connection(row) -> dict[str, Any]:
    d = dict(row)
    cfg = d.get("connection_config")
    d["connection_config"] = json.loads(cfg) if isinstance(cfg, str) and cfg else (cfg or {})
    return d


# ── Connections ─────────────────────────────────────────────

async def insert_connection(row: dict[str, Any]) -> None:
    row.setdefault("created_at", _now_iso())
    row["updated_at"] = _now_iso()
    row.setdefault("connection_config", "{}")
    if isinstance(row["connection_config"], dict):
        row["connection_config"] = json.dumps(row["connection_config"])
    await _pool().execute(
        """INSERT INTO integration_connections
           (id, provider_key, user_id, status, credentials_ref,
            connection_config, created_at, updated_at, last_sync_at, last_error)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)""",
        row["id"], row["provider_key"], row["user_id"], row["status"],
        row.get("credentials_ref"), row["connection_config"], row["created_at"],
        row["updated_at"], row.get("last_sync_at"), row.get("last_error"))


async def update_connection(connection_id: str, **fields: Any) -> None:
    if not fields:
        return
    bad = sorted(set(fields) - set(_CONNECTION_FIELDS))
    if bad:
        raise ValueError(f"update_connection: unknown column(s) {bad}")
    if isinstance(fields.get("connection_config"), dict):
        fields["connection_config"] = json.dumps(fields["connection_config"])
    fields["updated_at"] = _now_iso()
    names = list(fields)
    sets = ", ".join(f"{k} = ${i}" + ("::jsonb" if k == "connection_config" else "")
                     for i, k in enumerate(names, start=2))
    await _pool().execute(
        f"UPDATE integration_connections SET {sets} WHERE id = $1",
        connection_id, *[fields[k] for k in names])


async def get_connection(connection_id: str) -> dict[str, Any] | None:
    row = await _pool().fetchrow(
        "SELECT * FROM integration_connections WHERE id = $1", connection_id)
    return _connection(row) if row else None


async def list_connections(user_id: str | None = None,
                           provider_key: str | None = None) -> list[dict[str, Any]]:
    where, params = [], []
    if user_id is not None:
        params.append(user_id)
        where.append(f"user_id = ${len(params)}")
    if provider_key is not None:
        params.append(provider_key)
        where.append(f"provider_key = ${len(params)}")
    q = "SELECT * FROM integration_connections"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at DESC"
    return [_connection(r) for r in await _pool().fetch(q, *params)]


async def delete_connection(connection_id: str) -> None:
    await _pool().execute("DELETE FROM integration_connections WHERE id = $1", connection_id)


# ── Syncs ───────────────────────────────────────────────────

async def upsert_sync(connection_id: str, resource: str, mode: str,
                      cursor: str | None = None, **fields: Any) -> None:
    """Insert the sync row, or update cursor + mode and only the fields given
    — a field not named keeps its value."""
    bad = sorted(set(fields) - set(_SYNC_FIELDS))
    if bad:
        raise ValueError(f"upsert_sync: unknown column(s) {bad}")
    cols = ["connection_id", "resource", "cursor", "mode", *_SYNC_FIELDS]
    vals = [connection_id, resource, cursor, mode, *[fields.get(k) for k in _SYNC_FIELDS]]
    sets = ["cursor = EXCLUDED.cursor", "mode = EXCLUDED.mode",
            *[f"{k} = EXCLUDED.{k}" for k in _SYNC_FIELDS if k in fields]]
    await _pool().execute(
        f"""INSERT INTO integration_syncs ({", ".join(cols)})
            VALUES ({", ".join(f"${i}" for i in range(1, len(cols) + 1))})
            ON CONFLICT (connection_id, resource) DO UPDATE SET {", ".join(sets)}""",
        *vals)


async def get_sync(connection_id: str, resource: str) -> dict[str, Any] | None:
    row = await _pool().fetchrow(
        "SELECT * FROM integration_syncs WHERE connection_id = $1 AND resource = $2",
        connection_id, resource)
    return dict(row) if row else None


async def list_syncs(connection_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in await _pool().fetch(
        "SELECT * FROM integration_syncs WHERE connection_id = $1", connection_id)]


# ── Records (canonical data + raw) ──────────────────────────

async def upsert_record(record: dict[str, Any]) -> None:
    """integration_records.id is the provider's native ID. Upsert by primary key."""
    data = record.get("data") or {}
    if isinstance(data, dict):
        data = json.dumps(data)
    raw = record.get("raw_json")
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw)
    await _pool().execute(
        """INSERT INTO integration_records (id, connection_id, resource, canonical_model,
                                            data, raw_json, received_at, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (id) DO UPDATE SET
               data        = EXCLUDED.data,
               raw_json    = EXCLUDED.raw_json,
               received_at = EXCLUDED.received_at""",
        record["id"], record["connection_id"], record["resource"],
        record.get("canonical_model"), data, raw, record.get("received_at"),
        record.get("created_at") or _now_iso())


async def list_records(connection_id: str, resource: str | None = None,
                       limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = [connection_id]
    q = "SELECT * FROM integration_records WHERE connection_id = $1"
    if resource:
        params.append(resource)
        q += f" AND resource = ${len(params)}"
    params.append(max(1, min(limit, 1000)))
    # NULLS LAST: a record with no received_at sorts after dated ones, as it
    # did in SQLite (where NULL is the smallest value).
    q += f" ORDER BY received_at DESC NULLS LAST LIMIT ${len(params)}"
    rows = [dict(r) for r in await _pool().fetch(q, *params)]
    for r in rows:
        try:
            r["data"] = json.loads(r["data"])
        except Exception:
            pass
    return rows


# ── OAuth clients (per provider family) ─────────────────────

async def put_oauth_client(family: str, credentials_ref: str,
                           label: str | None = None) -> None:
    """Record (or replace) the vault ref of this host's OAuth client for a family."""
    now = _now_iso()
    await _pool().execute(
        """INSERT INTO integration_oauth_clients (family, credentials_ref, label, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $4)
           ON CONFLICT (family) DO UPDATE SET
               credentials_ref = EXCLUDED.credentials_ref,
               label           = EXCLUDED.label,
               updated_at      = EXCLUDED.updated_at""",
        family, credentials_ref, label, now)


async def get_oauth_client(family: str) -> dict[str, Any] | None:
    row = await _pool().fetchrow(
        "SELECT * FROM integration_oauth_clients WHERE family = $1", family)
    return dict(row) if row else None


async def list_oauth_clients() -> list[dict[str, Any]]:
    return [dict(r) for r in await _pool().fetch(
        "SELECT * FROM integration_oauth_clients ORDER BY family")]


async def delete_oauth_client(family: str) -> str | None:
    """Forget a family's client. Returns the vault ref that was held, so the
    caller can revoke the sealed secret — this only drops the pointer."""
    return await _pool().fetchval(
        "DELETE FROM integration_oauth_clients WHERE family = $1 RETURNING credentials_ref",
        family)


# ── App grants on a connection ──────────────────────────────

#: Methods a grant may name. Anything outside this is a typo, and a typo that
#: silently widened a grant would be the worst possible failure here.
GRANT_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


async def grant_app(connection_id: str, app_id: str,
                    methods: list[str] | None = None) -> None:
    """Let one app use one connection. Re-granting replaces the method list.

    `methods=None` is every method; ["GET"] is read-only. Raises on an
    unrecognised method rather than storing it — a grant that looks narrower
    than it is would be worse than no narrowing at all.
    """
    # `[]` is REFUSED, not treated as "no narrowing". An empty list falls
    # through `if methods:` as falsy and would store NULL — full access — which
    # is the widest possible grant arriving by the narrowest-looking route. A UI
    # with an empty multi-select, or a caller that filtered its list down to
    # nothing, would silently hand over everything. None means all; say it.
    if methods is not None and not methods:
        raise ValueError(
            "methods=[] is ambiguous: pass None for every method, "
            f"or name them from {list(GRANT_METHODS)}")
    if methods:
        methods = [m.strip().upper() for m in methods]
        bad = [m for m in methods if m not in GRANT_METHODS]
        if bad:
            raise ValueError(f"unknown method(s) {bad}; expected {list(GRANT_METHODS)}")
    await _pool().execute(
        """INSERT INTO integration_grants (connection_id, app_id, methods, granted_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (connection_id, app_id) DO UPDATE SET
               methods    = EXCLUDED.methods,
               granted_at = EXCLUDED.granted_at""",
        connection_id, app_id, json.dumps(methods) if methods else None, _now_iso())


def _grant(row) -> dict[str, Any]:
    d = dict(row)
    if d.get("methods"):
        try:
            d["methods"] = json.loads(d["methods"])
        except Exception:
            # Unreadable JSON must NOT read as "no narrowing" — that would widen
            # the grant to every method. Fall back to the safest reading.
            d["methods"] = ["GET"]
    return d


async def get_grant(connection_id: str, app_id: str) -> dict[str, Any] | None:
    """The grant row, or None. None means REFUSE — never 'allow by default'."""
    row = await _pool().fetchrow(
        "SELECT * FROM integration_grants WHERE connection_id = $1 AND app_id = $2",
        connection_id, app_id)
    return _grant(row) if row else None


async def list_grants(connection_id: str | None = None,
                      app_id: str | None = None) -> list[dict[str, Any]]:
    where, params = [], []
    if connection_id is not None:
        params.append(connection_id)
        where.append(f"connection_id = ${len(params)}")
    if app_id is not None:
        params.append(app_id)
        where.append(f"app_id = ${len(params)}")
    q = "SELECT * FROM integration_grants"
    if where:
        q += " WHERE " + " AND ".join(where)
    return [_grant(r) for r in await _pool().fetch(q + " ORDER BY granted_at DESC", *params)]


async def revoke_app_grant(connection_id: str, app_id: str) -> bool:
    return _affected(await _pool().execute(
        "DELETE FROM integration_grants WHERE connection_id = $1 AND app_id = $2",
        connection_id, app_id)) > 0


# ── The SQLite file this store used to be ───────────────────

#: Old table → (new table, its key columns, every column). Copied in this
#: order so each foreign key finds its connection.
_LEGACY_TABLES = (
    ("connections", "integration_connections", ("id",),
     ("id", "provider_key", "user_id", "status", "credentials_ref", "connection_config",
      "created_at", "updated_at", "last_sync_at", "last_error")),
    ("syncs", "integration_syncs", ("connection_id", "resource"),
     ("connection_id", "resource", "cursor", "mode", "last_run_at", "next_run_at", "last_error")),
    ("records", "integration_records", ("id",),
     ("id", "connection_id", "resource", "canonical_model", "data", "raw_json",
      "received_at", "created_at")),
    ("oauth_clients", "integration_oauth_clients", ("family",),
     ("family", "credentials_ref", "label", "created_at", "updated_at")),
    ("integration_grants", "integration_grants", ("connection_id", "app_id"),
     ("connection_id", "app_id", "methods", "granted_at")),
)


def read_legacy(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Every row of an old store file, by old table name, read-only. A table
    the file never had reads as empty; a column it never had (methods, added
    2026-09-21) reads as None."""
    import sqlite3
    out: dict[str, list[dict[str, Any]]] = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        for old, _new, _key, cols in _LEGACY_TABLES:
            try:
                rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{old}"')]
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e):
                    raise
                rows = []
            out[old] = [{c: r.get(c) for c in cols} for r in rows]
    finally:
        conn.close()
    return out


async def copy_legacy_rows(db, rows: dict[str, list[dict[str, Any]]],
                           skip: tuple[str, ...] = ()) -> dict[str, int]:
    """Insert read_legacy's rows into the integration_* tables through `db`
    (a pool or a connection; the caller holds any transaction), leaving a row
    already present as it is. `skip` names old tables not to copy — a restore
    skips integration_grants, because the backup's grants are authorization
    the live Host may have revoked since. Returns the rows offered, by table."""
    counts: dict[str, int] = {}
    for old, new, key, cols in _LEGACY_TABLES:
        if old in skip or not rows.get(old):
            continue
        for r in rows[old]:
            if "connection_config" in r and not r["connection_config"]:
                r["connection_config"] = "{}"
        marks = ", ".join(f"${i}" + ("::jsonb" if c == "connection_config" else "")
                          for i, c in enumerate(cols, start=1))
        quoted = ", ".join(f'"{c}"' for c in cols)
        await db.executemany(
            f'INSERT INTO "{new}" ({quoted}) VALUES ({marks}) '
            f"ON CONFLICT ({', '.join(key)}) DO NOTHING",
            [tuple(r[c] for c in cols) for r in rows[old]])
        counts[old] = len(rows[old])
    return counts


def remove_legacy_file(path: Path) -> None:
    """The old file and its -wal/-shm. Raises if one cannot be removed."""
    for p in (Path(path), Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


async def _missing_rows(db, rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Rows of read_legacy's output with no row of the same key in Postgres,
    by old table. One query per table, so a large file is not a query per row."""
    missing: dict[str, int] = {}
    for old, new, key, _cols in _LEGACY_TABLES:
        if not rows.get(old):
            continue
        arrays = [[r[k] for r in rows[old]] for k in key]
        cols = ", ".join(f"k{i}" for i in range(len(key)))
        unnest = ", ".join(f"${i}::text[]" for i in range(1, len(key) + 1))
        match = " AND ".join(f't."{k}" = u.k{i}' for i, k in enumerate(key))
        have = await db.fetchval(
            f'SELECT count(*) FROM (SELECT DISTINCT * FROM unnest({unnest}) AS u0({cols})) u '
            f'WHERE EXISTS (SELECT 1 FROM "{new}" t WHERE {match})', *arrays)
        want = len({tuple(r[k] for k in key) for r in rows[old]})
        if have != want:
            missing[old] = want - have
    return missing


class _NotAllArrived(Exception):
    def __init__(self, missing: dict[str, int]):
        super().__init__(f"rows did not arrive: {missing}")
        self.missing = missing


async def retire_sqlite_store(pool=None, path: Path | None = None) -> dict[str, Any]:
    """Copy any rows still in the old SQLite file into Postgres, check every
    one arrived, then remove the file. A row already here is left as it is
    (the Postgres copy is the one written since). The copy, the check and a
    row in integration_store_retired commit together or not at all: any
    failure keeps the file, nothing half-lands, and the next start tries
    again. A file whose copy HAS committed (its removal failed) is removed on
    a later start without being read again, because by then the user may have
    revoked a grant or removed a connection it still holds. With no file this
    is one stat() and nothing else. Never raises."""
    path = Path(path or LEGACY_DB_PATH)
    if not path.exists():
        return {"retired": False, "reason": "no file"}
    try:
        pool = pool or _pool()
        if await pool.fetchval("SELECT 1 FROM integration_store_retired WHERE path = $1", str(path)):
            remove_legacy_file(path)
            logger.info("integrations store: %s was copied in before; removed it without reading it", path)
            return {"retired": True, "already": True}
        rows = read_legacy(path)
        counts = {old: len(rows[old]) for old, *_ in _LEGACY_TABLES}
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await copy_legacy_rows(conn, rows)
                    # Every row of the file must be here, by key, before the
                    # copy commits and the file goes.
                    missing = await _missing_rows(conn, rows)
                    if missing:
                        raise _NotAllArrived(missing)
                    await conn.execute(
                        "INSERT INTO integration_store_retired (path, retired_at, copied) "
                        "VALUES ($1, $2, $3::jsonb) ON CONFLICT (path) DO NOTHING",
                        str(path), _now_iso(), json.dumps(counts))
        except _NotAllArrived as e:
            logger.warning("integrations store: %s rows of %s did not arrive — keeping %s",
                           e.missing, counts, path)
            return {"retired": False, "copied": counts, "missing": e.missing}
        remove_legacy_file(path)
        logger.info("integrations store: copied %s from %s into Postgres and removed it", counts, path)
        return {"retired": True, "copied": counts}
    except Exception as e:  # noqa: BLE001 — the file stays; the next start tries again
        logger.warning("integrations store: retiring %s failed (%s) — file kept", path, e)
        return {"retired": False, "error": str(e)[:300]}
