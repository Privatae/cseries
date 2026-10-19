#!/usr/bin/env python3
"""
Gate: the integrations store runs on the Host's own Postgres (migrations/v201).

2026-10-03: one database per Host, no second one.
server/integrations/core/storage.py used to keep connected accounts, sync
cursors, synced records, the Host's OAuth clients and per-app grants in a SQLite
file (/app/integrations/integrations.db). It now keeps them in the
integration_* tables. This proves the store against a real Postgres carrying
init-db.sql + migrations, the way smoke runs it in the built image:

  C1  a connection round-trips: config back as a dict, timestamps as strings
  C2  update_connection sets what it is given; a column it does not know is refused
  C3  list_connections filters by user and provider, newest first
  S1  upsert_sync inserts, then updates cursor + mode and only the fields named
  R1  upsert_record replaces by id; list_records is newest first with an
      undated record last, parses data, and clamps the limit
  O1  an OAuth client is replaced, never duplicated; delete returns its ref
  G1  grants — moved here from test_integration_broker.py, unchanged in what
      they pin: absence refuses, a grant needs a real connection, per app and
      per connection, deleting the connection takes its grants, narrowing in
      place, unknown and empty method lists refused, an unreadable method list
      reads as read-only
  M1  retire_sqlite_store: an old-shape file (from before grants had `methods`)
      is copied in, counted, and removed with its -wal/-shm; a row already
      here is kept as it is; a file whose rows cannot all arrive is KEPT; no
      file is a no-op
  M2  a file whose copy committed but whose removal failed is removed at the
      next start without being read again: a grant revoked and a connection
      removed in between stay gone
  K1  the legacy credential cutover deletes nothing while the old file is
      still there (its connections are not all in Postgres yet)
  U1  registry.uninstall finishes on a one-connection pool and removes the
      app's grant: the grant cleanup runs after its own connection is released

Run: SMOKE_PG_HOST=<host or socket dir> [SMOKE_PG_PORT/USER/PASSWORD/DB] \\
     python3 scripts/test_integrations_store_pg.py
In the image: python3 /app/scripts/test_integrations_store_pg.py
Prints OK <n> checks on success (smoke reads the last line).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

FAILS: list = []
PASSED = 0


def check(label, cond, detail=""):
    global PASSED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if cond:
        PASSED += 1
    else:
        FAILS.append(label)


P = "gate-int-"         # every id and family this gate writes starts with this


def conn_row(cid: str, *, user="3", provider="gmail", created=None, cfg=None) -> dict:
    r = {"id": P + cid, "provider_key": provider, "user_id": user, "status": "active",
         "credentials_ref": f"cred_{cid}", "last_sync_at": None, "last_error": None}
    if created:
        r["created_at"] = created
    if cfg is not None:
        r["connection_config"] = cfg
    return r


async def raises(coro, exc=Exception) -> bool:
    try:
        await coro
    except exc:
        return True
    return False


async def db() -> None:
    import asyncpg
    from server.integrations.core import storage

    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=1, max_size=4)

    async def wipe():
        # Grants, syncs and records go with their connection (ON DELETE CASCADE).
        await pool.execute("DELETE FROM integration_connections WHERE id LIKE $1", P + "%")
        await pool.execute("DELETE FROM integration_oauth_clients WHERE family LIKE $1", P + "%")
        # The record of a file this gate retired (only its own temp folders).
        await pool.execute("DELETE FROM integration_store_retired WHERE path LIKE $1",
                           "%/integrations-retire-%")

    saved = storage.POOL
    storage.POOL = pool
    await wipe()
    try:
        # ── C: connections ────────────────────────────────────────────
        await storage.insert_connection(conn_row("c1", created="2026-10-01T00:00:00+00:00",
                                                 cfg={"account_email": "a@example.com"}))
        c1 = await storage.get_connection(P + "c1")
        check("C1: a connection round-trips with its config as a dict",
              c1 is not None and c1["connection_config"] == {"account_email": "a@example.com"}
              and c1["status"] == "active" and c1["credentials_ref"] == "cred_c1", c1)
        check("C1: timestamps come back as the strings written",
              c1 and c1["created_at"] == "2026-10-01T00:00:00+00:00"
              and isinstance(c1["updated_at"], str), c1)
        check("C1: an absent connection is None", await storage.get_connection(P + "nope") is None)

        await storage.update_connection(P + "c1", status="needs_reauth", last_error="expired",
                                        connection_config=json.dumps({"x": 1}))
        c1 = await storage.get_connection(P + "c1")
        check("C2: update_connection sets the fields it is given",
              c1["status"] == "needs_reauth" and c1["last_error"] == "expired"
              and c1["connection_config"] == {"x": 1}, c1)
        await storage.update_connection(P + "c1", connection_config={"y": 2})
        check("C2: a config given as a dict is stored as one",
              (await storage.get_connection(P + "c1"))["connection_config"] == {"y": 2})
        check("C2: a column update_connection does not know is refused",
              await raises(storage.update_connection(P + "c1", id="other"), ValueError))

        await storage.insert_connection(conn_row("c2", user="9", provider="calendar",
                                                 created="2026-10-02T00:00:00+00:00"))
        await storage.insert_connection(conn_row("c3", provider="gmail",
                                                 created="2026-10-03T00:00:00+00:00"))
        mine = [r["id"] for r in await storage.list_connections(user_id="3") if r["id"].startswith(P)]
        check("C3: list_connections filters by user, newest first",
              mine == [P + "c3", P + "c1"], mine)
        gm = [r["id"] for r in await storage.list_connections(provider_key="gmail")
              if r["id"].startswith(P)]
        check("C3: ... and by provider", gm == [P + "c3", P + "c1"], gm)
        both = [r["id"] for r in await storage.list_connections(user_id="9", provider_key="calendar")
                if r["id"].startswith(P)]
        check("C3: ... and by both", both == [P + "c2"], both)

        # ── S: syncs ──────────────────────────────────────────────────
        await storage.upsert_sync(P + "c1", "message", "cursor", "100",
                                  last_run_at="t1", next_run_at="n1")
        s = await storage.get_sync(P + "c1", "message")
        check("S1: upsert_sync inserts the row", s and s["cursor"] == "100" and s["mode"] == "cursor"
              and s["last_run_at"] == "t1" and s["next_run_at"] == "n1", s)
        await storage.upsert_sync(P + "c1", "message", "incremental_poll", "200", last_run_at="t2")
        s = await storage.get_sync(P + "c1", "message")
        check("S1: a second upsert updates cursor, mode and only the fields named",
              s["cursor"] == "200" and s["mode"] == "incremental_poll"
              and s["last_run_at"] == "t2" and s["next_run_at"] == "n1", s)
        check("S1: list_syncs reads the connection's rows",
              [x["resource"] for x in await storage.list_syncs(P + "c1")] == ["message"])
        check("S1: a field upsert_sync does not know is refused",
              await raises(storage.upsert_sync(P + "c1", "message", "cursor", bogus=1), ValueError))

        # ── R: records ────────────────────────────────────────────────
        await storage.upsert_record({"id": P + "r1", "connection_id": P + "c1", "resource": "message",
                                     "data": {"subject": "old"}, "received_at": "2026-10-01T00:00:00Z"})
        await storage.upsert_record({"id": P + "r1", "connection_id": P + "c1", "resource": "message",
                                     "data": {"subject": "new"}, "raw_json": {"k": 1},
                                     "received_at": "2026-10-02T00:00:00Z"})
        await storage.upsert_record({"id": P + "r2", "connection_id": P + "c1", "resource": "message",
                                     "data": {"subject": "undated"}})
        await storage.upsert_record({"id": P + "r3", "connection_id": P + "c1", "resource": "event",
                                     "data": {"subject": "e"}, "received_at": "2026-10-03T00:00:00Z"})
        recs = await storage.list_records(P + "c1")
        check("R1: upsert_record replaces by id, and list_records parses data",
              [r["id"] for r in recs] == [P + "r3", P + "r1", P + "r2"]
              and recs[1]["data"] == {"subject": "new"} and json.loads(recs[1]["raw_json"]) == {"k": 1},
              [(r["id"], r["data"]) for r in recs])
        check("R1: newest first, an undated record last (as SQLite ordered it)",
              recs[-1]["id"] == P + "r2", [r["id"] for r in recs])
        check("R1: list_records filters by resource and clamps the limit",
              [r["id"] for r in await storage.list_records(P + "c1", "message", limit=0)] == [P + "r1"])

        # ── O: OAuth clients ──────────────────────────────────────────
        fam = P + "google"
        await storage.put_oauth_client(fam, "cred_abc", label="my project")
        row = await storage.get_oauth_client(fam)
        check("O1: a registered client round-trips", row and row["credentials_ref"] == "cred_abc"
              and row["label"] == "my project", row)
        await storage.put_oauth_client(fam, "cred_def", label="rotated")
        rows = [r for r in await storage.list_oauth_clients() if r["family"] == fam]
        check("O1: re-registering replaces rather than duplicating",
              len(rows) == 1 and rows[0]["credentials_ref"] == "cred_def", rows)
        check("O1: delete returns the ref it held", await storage.delete_oauth_client(fam) == "cred_def")
        check("O1: delete of an absent family is None", await storage.delete_oauth_client(fam) is None)

        # ── G: grants ─────────────────────────────────────────────────
        CONN, APP, OTHER = P + "c1", "mail-sorter", "weather-app"
        check("G1: no grant -> None (the broker's refuse condition)",
              await storage.get_grant(CONN, APP) is None)
        check("G1: a grant against a connection that does not exist cannot be written",
              await raises(storage.grant_app(P + "does-not-exist", APP)))
        await storage.grant_app(CONN, APP)
        g = await storage.get_grant(CONN, APP)
        check("G1: after grant_app the grant exists with full scope",
              g is not None and g["methods"] is None, g)
        check("G1: a grant is per app", await storage.get_grant(CONN, OTHER) is None)
        check("G1: a grant is per connection", await storage.get_grant(P + "c3", APP) is None)
        await storage.grant_app(P + "c3", APP)
        await storage.delete_connection(P + "c3")
        check("G1: deleting a connection takes its grants with it",
              await storage.get_grant(P + "c3", APP) is None
              and await storage.list_grants(connection_id=P + "c3") == [])
        await storage.grant_app(CONN, APP, ["GET"])
        check("G1: re-granting narrows in place rather than duplicating",
              (await storage.get_grant(CONN, APP))["methods"] == ["GET"]
              and len(await storage.list_grants(connection_id=CONN)) == 1)
        check("G1: an unknown method is refused",
              await raises(storage.grant_app(CONN, APP, ["TRACE"]), ValueError))
        check("G1: ... and did not overwrite the good grant",
              (await storage.get_grant(CONN, APP))["methods"] == ["GET"])
        await storage.grant_app(CONN, APP, ["get", "post"])
        check("G1: methods are normalised to upper case",
              (await storage.get_grant(CONN, APP))["methods"] == ["GET", "POST"])
        check("G1: methods=[] is refused, not read as 'grant everything'",
              await raises(storage.grant_app(CONN, APP, []), ValueError))
        check("G1: list_grants by app finds it",
              [r["connection_id"] for r in await storage.list_grants(app_id=APP)
               if r["connection_id"].startswith(P)] == [CONN])
        await pool.execute("UPDATE integration_grants SET methods = 'not json' "
                           "WHERE connection_id = $1 AND app_id = $2", CONN, APP)
        check("G1: an unreadable method list reads as read-only, never as every method",
              (await storage.get_grant(CONN, APP))["methods"] == ["GET"])
        check("G1: revoke removes it", await storage.revoke_app_grant(CONN, APP) is True
              and await storage.get_grant(CONN, APP) is None)
        check("G1: revoking a grant that is not there is False, not an error",
              await storage.revoke_app_grant(CONN, APP) is False)

        # ── M: the old SQLite file ────────────────────────────────────
        await wipe()
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="integrations-retire-"))
        old = tmp / "integrations.db"

        def make_old(path: pathlib.Path, *, orphan_grant: bool = False) -> None:
            # The shape before 2026-09-21: integration_grants had no `methods`.
            c = sqlite3.connect(path)
            c.executescript("""
                CREATE TABLE connections (id TEXT PRIMARY KEY, provider_key TEXT NOT NULL,
                    user_id TEXT NOT NULL, status TEXT NOT NULL, credentials_ref TEXT,
                    connection_config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, last_sync_at TEXT, last_error TEXT);
                CREATE TABLE syncs (connection_id TEXT NOT NULL, resource TEXT NOT NULL,
                    cursor TEXT, mode TEXT NOT NULL, last_run_at TEXT, next_run_at TEXT,
                    last_error TEXT, PRIMARY KEY (connection_id, resource));
                CREATE TABLE records (id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    resource TEXT NOT NULL, canonical_model TEXT, data TEXT NOT NULL,
                    raw_json TEXT, received_at TEXT, created_at TEXT NOT NULL);
                CREATE TABLE oauth_clients (family TEXT PRIMARY KEY, credentials_ref TEXT NOT NULL,
                    label TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE integration_grants (connection_id TEXT NOT NULL, app_id TEXT NOT NULL,
                    resources TEXT, granted_at TEXT NOT NULL, PRIMARY KEY (connection_id, app_id));
            """)
            c.execute("INSERT INTO connections VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (P + "m1", "gmail", "3", "active", "cred_m1", '{"account_email": "m@example.com"}',
                       "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", None, None))
            c.execute("INSERT INTO connections VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (P + "m2", "drive", "3", "active", "cred_m2", "",
                       "2026-09-02T00:00:00+00:00", "2026-09-02T00:00:00+00:00", None, None))
            c.execute("INSERT INTO syncs VALUES (?,?,?,?,?,?,?)",
                      (P + "m1", "message", "77", "cursor", "t", None, None))
            c.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                      (P + "mr1", P + "m1", "message", "Email", '{"subject": "kept"}', None,
                       "2026-09-03T00:00:00Z", "2026-09-03T00:00:00Z"))
            c.execute("INSERT INTO oauth_clients VALUES (?,?,?,?,?)",
                      (P + "fam", "cred_fam", "mine", "2026-09-01", "2026-09-01"))
            c.execute("INSERT INTO integration_grants VALUES (?,?,?,?)",
                      (P + "m1", "mail-sorter", None, "2026-09-04"))
            if orphan_grant:
                c.execute("INSERT INTO integration_grants VALUES (?,?,?,?)",
                          (P + "never-connected", "mail-sorter", None, "2026-09-04"))
            c.commit()
            c.close()
            for suffix in ("-wal", "-shm"):
                pathlib.Path(f"{path}{suffix}").write_bytes(b"")

        make_old(old)
        r = await storage.retire_sqlite_store(pool, old)
        check("M1: the old file's rows are copied and counted",
              r.get("retired") and r.get("copied") == {"connections": 2, "syncs": 1, "records": 1,
                                                       "oauth_clients": 1, "integration_grants": 1}, r)
        check("M1: ... and arrive as the store reads them",
              (await storage.get_connection(P + "m1"))["connection_config"] == {"account_email": "m@example.com"}
              and (await storage.get_connection(P + "m2"))["connection_config"] == {}
              and (await storage.get_sync(P + "m1", "message"))["cursor"] == "77"
              and (await storage.list_records(P + "m1"))[0]["data"] == {"subject": "kept"}
              and (await storage.get_oauth_client(P + "fam"))["credentials_ref"] == "cred_fam"
              and (await storage.get_grant(P + "m1", "mail-sorter"))["methods"] is None)
        check("M1: the file and its -wal/-shm are removed",
              not any((tmp / n).exists() for n in ("integrations.db", "integrations.db-wal",
                                                   "integrations.db-shm")))

        await storage.update_connection(P + "m1", status="revoked")
        other = tmp / "other" / "integrations.db"
        other.parent.mkdir()
        make_old(other)
        r = await storage.retire_sqlite_store(pool, other)
        check("M1: a row already here is kept as it is (the Postgres copy is the newer one)",
              r.get("retired") and (await storage.get_connection(P + "m1"))["status"] == "revoked", r)

        # M2: the copy committed but removing the file failed. Before the next
        # start the user revokes the app's grant and removes a connection; the
        # file still holds both. They must stay gone.
        await wipe()
        make_old(old)
        real_remove = storage.remove_legacy_file

        def refuse(path):
            raise PermissionError(f"cannot remove {path}")
        storage.remove_legacy_file = refuse
        try:
            r = await storage.retire_sqlite_store(pool, old)
        finally:
            storage.remove_legacy_file = real_remove
        check("M2: a copy whose file could not be removed keeps the file",
              not r.get("retired") and old.exists()
              and await storage.get_grant(P + "m1", "mail-sorter") is not None, r)
        await storage.revoke_app_grant(P + "m1", "mail-sorter")
        await storage.delete_connection(P + "m2")
        r = await storage.retire_sqlite_store(pool, old)
        check("M2: the next start removes that file without reading it again",
              r.get("retired") and r.get("already") and not old.exists(), r)
        check("M2: ... so a grant revoked since stays revoked, and a connection removed since stays removed",
              await storage.get_grant(P + "m1", "mail-sorter") is None
              and await storage.get_connection(P + "m2") is None)

        # K1: the credential cutover (credentials.cutover_legacy_cred_store)
        # verifies against the Postgres connections. While the old file is
        # still here its connections are not all in Postgres, and their
        # secrets may be only in the legacy blobs: nothing is deleted.
        from server.integrations.core import credentials as creds
        await wipe()
        make_old(old)
        legacy = tmp / "secrets"
        (legacy / "vault").mkdir(parents=True)
        (legacy / "vault" / "cred_m1.bin").write_bytes(b"sealed")
        (legacy / "integrations-master.key").write_bytes(b"k")
        saved_paths = (creds.VAULT_DIR, creds.MASTER_KEY_PATH, storage.LEGACY_DB_PATH)
        creds.VAULT_DIR, creds.MASTER_KEY_PATH = legacy / "vault", legacy / "integrations-master.key"
        storage.LEGACY_DB_PATH = old
        try:
            cut = await creds.cutover_legacy_cred_store(pool, 3)
        finally:
            creds.VAULT_DIR, creds.MASTER_KEY_PATH, storage.LEGACY_DB_PATH = saved_paths
        check("K1: while the old integrations file is still here the cutover deletes no legacy secret",
              (legacy / "vault" / "cred_m1.bin").exists() and (legacy / "integrations-master.key").exists()
              and not cut.get("deleted") and cut.get("reason") == "integrations_store_not_retired", cut)
        old.unlink()
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{old}{suffix}").unlink(missing_ok=True)

        await wipe()
        make_old(old, orphan_grant=True)
        r = await storage.retire_sqlite_store(pool, old)
        check("M1: a file whose rows cannot all arrive is KEPT, and nothing half-lands",
              not r.get("retired") and old.exists()
              and await storage.get_connection(P + "m1") is None, r)
        old.unlink()
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{old}{suffix}").unlink(missing_ok=True)

        r = await storage.retire_sqlite_store(pool, old)
        check("M1: with no file it does nothing", r == {"retired": False, "reason": "no file"}, r)

        # ── U1: uninstall on a one-connection pool ────────────────────
        from server.applications import registry
        uapp = P + "uninstall-app"
        await storage.insert_connection(conn_row("u1"))
        await pool.execute(
            "INSERT INTO installed_applications (id, name, version, provenance, entrypoint) "
            "VALUES ($1, 'Gate Uninstall', '0.1.0', 'custom', 'index.html') "
            "ON CONFLICT (id) DO NOTHING", uapp)
        await storage.grant_app(P + "u1", uapp)
        one = await asyncpg.create_pool(
            host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
            port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
            user=os.environ.get("SMOKE_PG_USER", "entity"),
            password=os.environ.get("SMOKE_PG_PASSWORD") or None,
            database=os.environ.get("SMOKE_PG_DB", "hostmem"),
            min_size=1, max_size=1)
        storage.POOL = one
        try:
            await asyncio.wait_for(registry.uninstall(one, uapp), timeout=10)
            finished = True
        except asyncio.TimeoutError:
            finished = False
        finally:
            storage.POOL = pool
            await one.close()
        check("U1: uninstall finishes when the pool has one connection (the grant "
              "cleanup never waits on a second while holding the first)", finished)
        check("U1: ... and the app's grant is gone",
              finished and await storage.list_grants(app_id=uapp) == [])
        await pool.execute("DELETE FROM installed_applications WHERE id = $1", uapp)
    finally:
        await wipe()
        storage.POOL = saved
        await pool.close()


def main() -> int:
    asyncio.run(db())
    if FAILS:
        print(f"FAIL {len(FAILS)} of {len(FAILS) + PASSED}: " + "; ".join(FAILS))
        return 1
    print(f"OK {PASSED} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
