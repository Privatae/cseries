#!/usr/bin/env python3
"""Gate — the autofill grant table, against a REAL Postgres.

tests/test_browser_autofill.py pins the DECISION logic with a fake pool,
which is right for the logic and worthless for the SQL: a stubbed
database only ever proves the stub agrees with itself. The grant is a
uniqueness constraint, a partial index and an idempotent insert, and
every one of those is a property of Postgres, not of Python.

So this drives the ACTUAL server/browser_autofill.py from the tree under
test against the table the kernel builds: a fresh clone of the KERNEL's
schema (scripts/kernel_schema.py — entrypoint.sh's procedure, every
migration; browser_form_grant is v141's), made for this run and dropped
after it. The test server is TEST_DSN / PEER_TEST_DSN, else a throwaway
cseries-postgres started through docker.

It used to run this module inside a live Host's pod and drive that
Host's own embedded Postgres through DATABASE_URL or the pod socket:
inserting and deleting rows under a fixed entity id, and dropping a
scratch table, in a live Host's database. A gate never writes to a database
it did not create. With no
test server and no docker it cannot run: by hand that is a skip (exit 2);
in a gate lane (GATE_LANE set) it is a failure (exit 1).

Run:
    python3 scripts/test_autofill_grant_pg.py            # throwaway cseries-postgres
    TEST_DSN=postgresql://… python3 scripts/test_autofill_grant_pg.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

# A host no real site can be, and an entity id no Host has.
HOST = "gate-test.invalid"
EID = 424242

fails: list[str] = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("   [" + str(detail) + "]") if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def main(dsn: str) -> int:
    import asyncpg
    from server import browser_autofill as mod

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        # The module carries no DDL: the table is created by
        # migrations/v141_band_b_app_and_autofill.sql at entrypoint, before
        # the server starts. So the check here is that the kernel's schema
        # has the shape the module's SQL expects.
        print("[1] the migration left the table the module writes to")
        async with pool.acquire() as c:
            reg = await c.fetchval(
                "SELECT to_regclass('public.browser_form_grant')::text")
            cols = await c.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='browser_form_grant' ORDER BY ordinal_position")
        check("table exists (created by v141, not by the module)",
              reg == "browser_form_grant", reg)
        names = [r["column_name"] for r in cols]
        check("columns are what the module writes",
              {"id", "entity_id", "host", "granted_at", "granted_by",
               "revoked_at", "revoked_by", "last_used_at"}.issubset(set(names)),
              names)

        print("[2] deny by default")
        check("no grant before one is made",
              await mod.is_granted(pool, entity_id=EID, host=HOST) is False)

        print("[3] grant, and grant again")
        g1 = await mod.grant(pool, entity_id=EID, host=HOST, actor="gate")
        check("granting reports granted", g1["granted"] is True)
        check("first grant is not flagged 'already'", g1["already"] is False)
        check("is_granted now true",
              await mod.is_granted(pool, entity_id=EID, host=HOST) is True)

        g2 = await mod.grant(pool, entity_id=EID, host=HOST, actor="gate-again")
        check("re-granting is idempotent, not a second row", g2["already"] is True)
        check("re-grant keeps the ORIGINAL grantor",
              g2["granted_by"] == "gate", g2["granted_by"])
        async with pool.acquire() as c:
            live = await c.fetchval(
                "SELECT count(*) FROM browser_form_grant "
                "WHERE entity_id=$1 AND host=$2 AND revoked_at IS NULL", EID, HOST)
        check("exactly ONE live row (the partial unique index holds)", live == 1, live)

        print("[4] the unique index really is partial")
        # A revoked row must not block a fresh grant, or a user could never
        # re-allow a site they had turned off.
        await mod.revoke(pool, entity_id=EID, host=HOST, actor="gate")
        check("revoked -> is_granted false",
              await mod.is_granted(pool, entity_id=EID, host=HOST) is False)
        g3 = await mod.grant(pool, entity_id=EID, host=HOST, actor="gate-3")
        check("a site can be re-granted after a revoke", g3["granted"] is True)
        check("re-grant after revoke is a NEW grant", g3["already"] is False)
        async with pool.acquire() as c:
            total = await c.fetchval(
                "SELECT count(*) FROM browser_form_grant WHERE entity_id=$1", EID)
            live = await c.fetchval(
                "SELECT count(*) FROM browser_form_grant "
                "WHERE entity_id=$1 AND revoked_at IS NULL", EID)
        check("the revoked row SURVIVES as audit history", total == 2, total)
        check("but only one is live", live == 1, live)

        print("[5] revoking something that was never granted")
        r = await mod.revoke(pool, entity_id=EID, host="never-granted.invalid",
                             actor="gate")
        check("revoking a non-grant reports False, does not raise", r is False)

        print("[6] grants are per-entity")
        check("another entity does not see this grant",
              await mod.is_granted(pool, entity_id=EID + 1, host=HOST) is False)

        print("[7] last_used_at starts empty and is set by _touch")
        rows = await mod.list_grants(pool, entity_id=EID)
        check("list_grants returns the live grant only", len(rows) == 1, rows)
        check("last_used_at is null before any fill",
              rows[0]["last_used_at"] is None, rows[0])
        await mod._touch(pool, entity_id=EID, host=HOST)
        rows = await mod.list_grants(pool, entity_id=EID)
        check("last_used_at is set after a touch",
              rows[0]["last_used_at"] is not None, rows[0])

        print("[8] a revoked grant is invisible to list_grants")
        await mod.revoke(pool, entity_id=EID, host=HOST, actor="gate")
        check("list_grants is empty once revoked",
              await mod.list_grants(pool, entity_id=EID) == [])

        print("[9] a bad host never reaches the database")
        try:
            await mod.grant(pool, entity_id=EID, host="not a host", actor="gate")
            check("a malformed host is refused", False, "no exception raised")
        except mod.AutofillError:
            check("a malformed host is refused", True)
    finally:
        await pool.close()

    print()
    print("ALL GATES PASSED" if not fails else "FAILED: " + repr(fails))
    return 1 if fails else 0


def _no_database() -> int:
    """No database this gate may write to: a failure in a gate lane, a skip
    by hand."""
    lane = os.environ.get("GATE_LANE")
    if lane:
        print(f"FAIL — the {lane} lane gave no test server (TEST_DSN/PEER_TEST_DSN) and docker is "
              "unavailable; this gate never writes to a database it did not create.")
        return 1
    print("SKIP — no test server (TEST_DSN/PEER_TEST_DSN) and docker unavailable — this gate needs "
          "a database it creates to prove the SQL, and a skip proves nothing.")
    return 2


if __name__ == "__main__":
    from kernel_schema import kernel_schema_db, throwaway_server

    SERVER = os.environ.get("TEST_DSN") or os.environ.get("PEER_TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit(_no_database())
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv) as _db:
        print("# driving server/browser_autofill.py against a fresh clone of the kernel's schema\n")
        _rc = asyncio.run(main(_db))
    sys.exit(_rc)
