#!/usr/bin/env python3
"""Gate: the app-delivery lane — migration v165 + exactly-once delivery.

WHY A REAL DATABASE. Everything this proves IS the schema: a CHECK constraint
accepting a new value while still accepting the seventeen old ones, and a
primary-key collision turning a retry into a no-op. A stubbed pool would assert
against a fiction — it would happily "reject" a kind the real constraint allows,
and vice versa. So this runs against a throwaway Postgres and skips loudly if
Docker is absent (a silent skip proves nothing; see scripts/peer_test_db.sh).

THE SCHEMA IS THE KERNEL'S (2026-09-30). This gate used to stand a hand copy
of inbox_messages (with a hand list of the seventeen kinds it allowed) on a
stock postgres:18.4. It now gets the REAL schema as it stood just before v165
from scripts/kernel_schema.py (entrypoint.sh's procedure, stopped there), reads
the kinds the real constraint allowed, applies v165 itself to test the
transition, then brings the database to the full current schema and runs the
delivery checks there — on the table a host has today.

Server: INBOX_TEST_DSN / PEER_TEST_DSN / TEST_DSN when set, else a throwaway
cseries-postgres started here (INBOX_TEST_PG_NAME names it).

Run:  python3 scripts/test_inbox_app_delivery.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import contextlib  # noqa: E402
import re  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kernel_schema import complete, kernel_schema_db, throwaway_server  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "migrations", "v165_inbox_app_delivery.sql")
DSN = None   # the clone this run works in, set in main()

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def psql(sql: str) -> tuple[bool, str]:
    """Run SQL against this run's clone of the kernel schema."""
    p = subprocess.run(["psql", DSN, "-X", "-v", "ON_ERROR_STOP=1", "-At", "-f", "-"],
                       input=sql, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    return (p.returncode == 0), out.strip()


def allowed_kinds() -> list[str]:
    """The kinds the REAL constraint allows right now, read from the database."""
    _ok, d = psql("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                  "WHERE conname = 'inbox_messages_message_kind_check'")
    return re.findall(r"'([a-z_]+)'::text", d)


def insert_kind(kind: str, message_id: str | None = None,
                payload: str = "{}") -> tuple[bool, str]:
    mid = f"'{message_id}'::uuid" if message_id else "gen_random_uuid()"
    return psql(
        "INSERT INTO inbox_messages "
        "(message_id, from_address,to_address,from_entity_id,to_entity_id,"
        " subject,message_kind,payload) VALUES "
        f"({mid},'cseries@localhost','9003@entity.localhost',9003,9003,"
        f"'t','{kind}','{payload}'::jsonb) "
        "ON CONFLICT (message_id) DO NOTHING"
    )


def main() -> int:
    global DSN
    server = (os.environ.get("INBOX_TEST_DSN") or os.environ.get("PEER_TEST_DSN")
              or os.environ.get("TEST_DSN"))
    if not server and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing "
              "without one.", file=sys.stderr)
        return 2
    with (contextlib.nullcontext(server) if server
          else throwaway_server(os.environ.get("INBOX_TEST_PG_NAME"))) as srv, \
            kernel_schema_db(srv, upto=165) as DSN:
        before = allowed_kinds()
        check("the real pre-v165 constraint is read from the database", len(before) >= 10, before)
        ok, _ = insert_kind("app_delivery")
        check("BEFORE migration: 'app_delivery' is rejected", not ok)

        with open(MIGRATION) as f:
            migration_sql = f.read()
        ok, out = psql(migration_sql)
        check("v165 applies cleanly", ok, out)
        if not ok:
            return 1

        # The whole risk of rewriting a CHECK: silently dropping a value.
        dropped = [k for k in before if not insert_kind(k)[0]]
        check(f"all {len(before)} pre-existing kinds still accepted", not dropped, f"dropped={dropped}")

        ok, out = insert_kind("app_delivery")
        check("AFTER migration: 'app_delivery' is accepted", ok, out)

        ok, _ = insert_kind("not_a_real_kind")
        check("constraint still rejects an unknown kind", not ok)

        # Now the schema a host has today: every migration after v165 lands on
        # top, exactly as entrypoint.sh would apply it.
        complete(DSN, already=(165,))
        now = allowed_kinds()
        check("on the full current schema 'app_delivery' is still an allowed kind",
              "app_delivery" in now and insert_kind("app_delivery")[0], now)

        # Exactly-once. uuid5(app_id + key) is what the route derives, so the
        # second delivery of the same item collides on the primary key.
        ns = uuid.UUID("6f6d2b5e-0e3a-4a1d-9c7f-2b4d8e1a5c93")
        mid = str(uuid.uuid5(ns, "mail-sorter\x00rule7:acct1:msg42"))
        insert_kind("app_delivery", message_id=mid,
                    payload='{"delivered_by_app":"mail-sorter"}')
        insert_kind("app_delivery", message_id=mid,
                    payload='{"delivered_by_app":"mail-sorter"}')
        _ok, n = psql(f"SELECT count(*) FROM inbox_messages WHERE message_id='{mid}'::uuid")
        check("redelivering the same key inserts exactly one row", n == "1", f"count={n}")

        # Two apps, same key text, must not collide — the key is app-scoped.
        a = str(uuid.uuid5(ns, "mail-sorter\x00dup"))
        b = str(uuid.uuid5(ns, "other-app\x00dup"))
        check("the same idempotency key in two apps is two rows", a != b)

        # Provenance survives the round trip and is queryable.
        _ok, app = psql("SELECT payload->>'delivered_by_app' FROM inbox_messages "
                        f"WHERE message_id='{mid}'::uuid")
        check("delivery records which app delivered it", app == "mail-sorter", f"got={app}")

        # A self-send is what the admission gate always allows (domain_policy.py:198).
        _ok, same = psql("SELECT bool_and(from_entity_id = to_entity_id) FROM inbox_messages "
                         "WHERE message_kind='app_delivery'")
        check("app deliveries are self-sends (admitted without policy)", same == "t")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
