#!/usr/bin/env python3
"""The database half of scripts/kernel_schema.py, on a real cseries-postgres.

  E. the entity id lands where entrypoint.sh sets it — right after init-db.sql,
     BEFORE the migrations — so rows a migration seeds for the entity carry the
     Host's id: v019/v037/v038/v054 seed entity_domain_access from
     entity_registry. Measured 2026-10-01: a fresh build seeds 11 rows; on an
     entity-1 clone renumbered to 3 afterwards (what this module used to do)
     all 11 stay under entity 1 while entity_registry says 3 — no foreign key
     notices (review of preview-animated at fa0dccbe). The id-0 'Platform' row
     v094/v105 add is the platform's, not the entity's;
  U. upto=165 is the schema a Host had before v165: no v165 in the ledger, and
     the inbox kind check does not allow 'app_delivery' — read from the
     database, not from the file; complete() then runs entrypoint.sh's section
     over it as an upgrading Host's boot does, and the ledger reaches the
     tree's last migration with the kind allowed;
  S. a server keeps only this tree's templates: another tree's template and a
     build whose process is gone are dropped; this tree's templates and a build
     still in progress are not.

Needs a test server: PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres
(scripts/peer_test_db.sh). Exits 2 and says so without one.

Run:  python3 scripts/test_kernel_schema_db.py
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
import kernel_schema as ks  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


def q(dsn: str, sql: str) -> str:
    r = subprocess.run(["psql", dsn, "-X", "-Atc", sql], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def main(server: str) -> None:
    print("E. the entity id is set before the migrations seed")
    for eid in (3, 1):
        with ks.kernel_schema_db(server, entity_id=eid) as dsn:
            row = q(dsn, "SELECT string_agg(id || '/' || coalesce(privatae_entity_id::text, '-'), ',') "
                         "FROM entity_registry WHERE id <> 0")
            seeded = q(dsn, "SELECT coalesce(string_agg(DISTINCT entity_id::text, ','), '') FROM entity_domain_access")
            n = int(q(dsn, "SELECT count(*) FROM entity_domain_access"))
            check(f"entity {eid}: the entity's row carries id {eid}", row == f"{eid}/{eid}", row)
            check(f"entity {eid}: the {n} entity_domain_access rows migrations seeded carry entity {eid}",
                  n > 0 and seeded == str(eid), f"entity ids on seeded rows: {seeded!r}")

    print("U. upto=165 is the schema before v165")
    with ks.kernel_schema_db(server, upto=165) as dsn:
        top = int(q(dsn, "SELECT max(version) FROM schema_migrations"))
        kinds = q(dsn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                       "WHERE conname = 'inbox_messages_message_kind_check'")
        check("no v165 or later in the ledger", top < 165, top)
        check("the kind check does not allow 'app_delivery' yet", "message" in kinds and "app_delivery" not in kinds,
              kinds[:200])
        before = int(q(dsn, "SELECT count(*) FROM schema_migrations"))
        gained = ks.complete(dsn)
        last = max(v for v, _ in ks.migrations(ROOT))
        after = q(dsn, "SELECT count(*) || '/' || max(version) FROM schema_migrations")
        kinds = q(dsn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                       "WHERE conname = 'inbox_messages_message_kind_check'")
        check(f"complete() brings it to the tree's last migration (v{last}), every version once",
              gained > 0 and after == f"{before + gained}/{last}", (before, gained, after))
        check("...and the kind check then allows 'app_delivery'", "app_delivery" in kinds, kinds[:200])

    print("S. a server keeps only this tree's templates")
    mine = ks.template_name(ROOT, 1)
    other = "kernel_tmpl_0000000000000000_e1"
    gone = subprocess.Popen(["true"])
    gone.wait()
    dead_build = f"kernel_build_{gone.pid}_deadbeef"
    live_build = f"kernel_build_{os.getpid()}_cafef00d"
    for name in (other, dead_build, live_build):
        q(server, f'CREATE DATABASE "{name}"')
    q(server, f'ALTER DATABASE "{other}" WITH ALLOW_CONNECTIONS false IS_TEMPLATE true')
    try:
        dropped = ks.drop_stale(server, ROOT)
        left = set(q(server, "SELECT string_agg(datname, ' ') FROM pg_database").split())
        check("another tree's template is dropped", other in dropped and other not in left, dropped)
        check("a build whose process is gone is dropped", dead_build in dropped and dead_build not in left, dropped)
        check("a build still in progress is kept", live_build in left)
        check("this tree's templates are kept", mine in left and ks.template_name(ROOT, 3) in left,
              sorted(n for n in left if n.startswith("kernel_")))
    finally:
        for name in (other, dead_build, live_build):
            subprocess.run(["psql", server, "-X", "-qc", f'ALTER DATABASE "{name}" WITH IS_TEMPLATE false'],
                           capture_output=True)
            subprocess.run(["psql", server, "-X", "-qc", f'DROP DATABASE IF EXISTS "{name}"'], capture_output=True)


SERVER = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing without one.")
    sys.exit(2)
with (contextlib.nullcontext(SERVER) if SERVER else ks.throwaway_server()) as _srv:
    main(_srv)
print()
if fails:
    print(f"FAIL — {fails} check(s)")
    sys.exit(1)
print("PASS kernel_schema_db")
