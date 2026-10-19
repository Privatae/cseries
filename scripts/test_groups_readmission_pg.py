#!/usr/bin/env python3
"""Gate — the group re-admission predicate, against a REAL Postgres.

scripts/test_groups_roster_votes.py pins the DECISION logic with a recording
fake pool, which is right for the logic and not sufficient for this one thing:
the re-admission fix IS a SQL predicate. `_LIVE_GROUP_GRANT` turns on
`status <> 'revoked'`, a `payload->>'group_id'` json extraction and an
`interval` comparison, and the revoke half is an `UPDATE ... WHERE
payload->>'group_id' = $3`. A fake pool can only ever prove that my Python
re-implementation of that predicate agrees with my Python re-implementation of
it — which is exactly the shape of test this repo already warns about.

So this drives the REAL functions — app_lane.enqueue, lane.revoke_group_slips,
app_lane.reconcile_group_grants — against a real engine, over the real
migrations, and asserts the behaviour the defect produced:

    grant -> remove -> re-add  must enqueue a FRESH row,
    and the old row must read `revoked`.

Before the fix, the third step enqueued nothing: the member's slip had been
revoked but the stale `delivered` row still satisfied a
`NOT EXISTS (app_id, peer_entity_id, op)` match, so a re-admitted member was
skipped for ever.

SKIPS CLEANLY when TEST_DATABASE_URL is unset — the same pattern the Go side
uses (community/forum/postgres_test.go) — so it is safe in the container-free
lane and does the real work wherever a scratch database exists.

    TEST_DATABASE_URL=postgres://user:pw@host:port/db python3 scripts/test_groups_readmission_pg.py

It builds its own schema (`grouptest_<pid>`), does everything inside it, and
drops it at the end, so it never touches anything the database already holds
and is safe to re-run.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import uuid

REPO = pathlib.Path(os.environ.get("APP_ROOT") or
                    pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(REPO), str(REPO / "brain")]

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


GID = "grp-3-" + uuid.uuid4().hex
OWNER, ALICE, BOB = 3, 122, 123
APP = "veterans"
SCHEMA = f"grouptest_{os.getpid()}"


async def main() -> int:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        print("SKIP: TEST_DATABASE_URL unset — this gate needs a scratch Postgres.")
        print("      The fake-pool half runs in scripts/test_groups_roster_votes.py.")
        return 0

    import asyncpg
    from server.groups import lane as LANE
    from server.peer import app_lane as AL

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'CREATE SCHEMA "{SCHEMA}"')
        await conn.execute(f'SET search_path TO "{SCHEMA}"')

        # The REAL migrations, not a hand-written approximation of them. v153
        # brings peer_app_outbox (the table the predicate reads) and v154 the
        # group tables; if either drifts from what the code expects, this gate
        # is where that shows up.
        for name in ("v153_peer_app_lane.sql", "v154_groups_roster_votes.sql"):
            sql = (REPO / "migrations" / name).read_text()
            await conn.execute(sql)
        check(True, f"v153 + v154 applied into schema {SCHEMA}")

        # Every connection in the pool must land in the scratch schema, so the
        # real functions' unqualified table names resolve there. Set as a
        # CONNECTION PARAMETER rather than a `SET` in an init hook: the
        # parameter is applied by the server at startup for every connection
        # the pool opens, including ones opened later, with nothing to run
        # first and nothing to forget.
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=2,
            server_settings={"search_path": SCHEMA})

        # A group this host owns, with two members.
        await pool.execute(
            "INSERT INTO groups (group_id, owner_entity_id, name) VALUES ($1, $2, $3)",
            GID, OWNER, "builders")
        for m in (ALICE, BOB):
            await pool.execute(
                "INSERT INTO group_members (group_id, member_entity_id, role) "
                "VALUES ($1, $2, 'member')", GID, m)
        await pool.execute(
            "INSERT INTO group_app_grants (group_id, app_id) VALUES ($1, $2)", GID, APP)

        # ── 1. grant ────────────────────────────────────────────────────────
        n = await AL.reconcile_group_grants(pool, OWNER)
        check(n == 2, "the reconciler grants both members on the first pass", n)
        rows = await pool.fetch(
            "SELECT peer_entity_id, status, payload->>'group_id' AS gid "
            "FROM peer_app_outbox WHERE app_id = $1 ORDER BY peer_entity_id", APP)
        check([r["peer_entity_id"] for r in rows] == [ALICE, BOB],
              "one row per member", [dict(r) for r in rows])
        check(all(r["gid"] == GID for r in rows),
              "each row carries the group id, through a REAL jsonb round-trip")

        # A second pass must add nothing — the rows are live.
        check(await AL.reconcile_group_grants(pool, OWNER) == 0,
              "a second pass adds nothing while the grants are live")

        # Pretend the grants were delivered with a minted contract, as _send does.
        for m in (ALICE, BOB):
            await pool.execute(
                "UPDATE peer_app_outbox SET status='delivered', "
                "payload = payload || $3::jsonb "
                "WHERE app_id = $1 AND peer_entity_id = $2",
                APP, m, json.dumps({"app_slip": {"contract_id": f"slip-{m}"}}))
        check(await AL.reconcile_group_grants(pool, OWNER) == 0,
              "and nothing once they are delivered")

        # ── 2. remove ───────────────────────────────────────────────────────
        revoked: list = []

        async def fake_bridge(op, params):
            revoked.append((op, params.get("contract_id")))
            return {"ok": True}

        real_bridge, LANE._bridge = LANE._bridge, fake_bridge
        try:
            await pool.execute(
                "UPDATE group_members SET removed_at = now() "
                "WHERE group_id = $1 AND member_entity_id = $2", GID, ALICE)
            await LANE.revoke_group_slips(pool, GID, ALICE)
        finally:
            LANE._bridge = real_bridge

        check(("contract.revoke", f"slip-{ALICE}") in revoked,
              "removal revokes the removed member's group-scoped app contract", revoked)
        check(not any(c == f"slip-{BOB}" for _o, c in revoked),
              "and leaves the other member's contract alone", revoked)

        st = await pool.fetchrow(
            "SELECT status, last_error FROM peer_app_outbox "
            "WHERE app_id = $1 AND peer_entity_id = $2", APP, ALICE)
        check(st["status"] == "revoked",
              "the REAL UPDATE stamps that member's grant row revoked", dict(st))
        other = await pool.fetchval(
            "SELECT status FROM peer_app_outbox WHERE app_id = $1 AND peer_entity_id = $2",
            APP, BOB)
        check(other == "delivered", "and does not touch the other member's row", other)

        # ── 3. re-add ───────────────────────────────────────────────────────
        await pool.execute(
            "UPDATE group_members SET removed_at = NULL "
            "WHERE group_id = $1 AND member_entity_id = $2", GID, ALICE)
        n = await AL.reconcile_group_grants(pool, OWNER)
        check(n == 1, "a RE-ADMITTED member is granted again — the defect this fixes", n)

        fresh = await pool.fetch(
            "SELECT status, payload->>'group_id' AS gid, payload->'app_slip'->>'contract_id' AS cid "
            "FROM peer_app_outbox WHERE app_id = $1 AND peer_entity_id = $2 "
            "ORDER BY id", APP, ALICE)
        check(len(fresh) == 2, "the old row is kept and a NEW one is enqueued beside it",
              [dict(r) for r in fresh])
        check(fresh[0]["status"] == "revoked" and fresh[0]["cid"] == f"slip-{ALICE}",
              "the old row still reads revoked, with the contract that was revoked",
              dict(fresh[0]))
        check(fresh[1]["status"] == "queued" and fresh[1]["cid"] is None,
              "and the new row is queued with NO contract yet — _send mints a fresh one",
              dict(fresh[1]))
        check(fresh[1]["gid"] == GID,
              "the new row names the group, so the re-issued slip is group-scoped again")

        check(await AL.reconcile_group_grants(pool, OWNER) == 0,
              "and the pass after that is quiet again")

        # ── 4. the AGE arm of the same predicate ────────────────────────────
        # The other half of "live", exercised through real interval arithmetic
        # rather than a Python timestamp comparison.
        await pool.execute(
            "UPDATE peer_app_outbox SET status='delivered', "
            "created_at = now() - ($2 || ' seconds')::interval "
            "WHERE app_id = $1 AND status = 'queued'", APP, str(AL.GRANT_TERM_S + 3600))
        n = await AL.reconcile_group_grants(pool, OWNER)
        check(n == 1, "a grant older than the slip's term is re-issued, not left lapsed", n)

        # ── 5. group scoping ────────────────────────────────────────────────
        # Two groups granting the SAME app to the SAME peer are two grants.
        gid2 = "grp-3-" + uuid.uuid4().hex
        await pool.execute(
            "INSERT INTO groups (group_id, owner_entity_id, name) VALUES ($1, $2, $3)",
            gid2, OWNER, "second")
        await pool.execute(
            "INSERT INTO group_members (group_id, member_entity_id, role) "
            "VALUES ($1, $2, 'member')", gid2, ALICE)
        await pool.execute(
            "INSERT INTO group_app_grants (group_id, app_id) VALUES ($1, $2)", gid2, APP)
        n = await AL.reconcile_group_grants(pool, OWNER)
        check(n == 1,
              "a second group granting the same app to the same peer is its OWN grant", n)
        g2rows = await pool.fetchval(
            "SELECT count(*) FROM peer_app_outbox WHERE app_id = $1 "
            "AND peer_entity_id = $2 AND payload->>'group_id' = $3", APP, ALICE, gid2)
        check(g2rows == 1, "recorded against the second group, not the first", g2rows)

        await pool.close()
    finally:
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        finally:
            await conn.close()

    print(f"\n{CHECKS} checks, {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  FAILED: " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
