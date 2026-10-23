#!/usr/bin/env python3
"""Bastion auto-update's SQL against a REAL Postgres (the decision logic is
pinned container-free by test_bastion_autoupdate.py; this is the half that is
only true against a database).

Applies migrations/v176_bastion_autoupdate.sql to a throwaway database (twice,
for idempotence), then drives the module's own statements: the settings row,
the per-version watch of which machines ring 0 told (and whether each was
beating) and which it saw on the version by heartbeat however they got there
(first write wins; never another Host's machine; never a revoked one), the
join that feeds the verdict, the state row the Network app reads, and the
once-per-version stall alert.

Run (skips without a database, and a skip proves nothing):

    eval "$(scripts/peer_test_db.sh)"      # exports PEER_TEST_DSN
    python3 scripts/test_bastion_autoupdate_sql.py
    scripts/peer_test_db.sh --stop
"""
from __future__ import annotations

import asyncio
import os
import sys

REPO = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

DSN = os.environ.get("PEER_TEST_DSN")
FAILED = 0


def check(label, cond, extra=""):
    global FAILED
    if cond:
        print(f"ok    {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label} {extra}")


# The real mcp_client_tokens requires a hash and a handle, each unique among
# live tokens; every bastion token here gets its own.
TOKEN_SQL = ("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind, "
             "revoked_at) VALUES ($1, $2, 'box', $3, $4, 'bastion', $5)")
BEAT_SQL = ("INSERT INTO network_bastion_heartbeat (token_id, hostname, gateway_version, update_to_version, "
            "last_seen_at) VALUES ($1, $2, $3, $4, now() - ($5::text)::interval)")


async def token(c, tid, entity=9003, revoked=False):
    await c.execute(TOKEN_SQL, tid, entity, tid.to_bytes(4, "big"),
                    f"box-{tid}", __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                    if revoked else None)


async def main(dsn) -> int:
    import asyncpg
    # The KERNEL's schema, on a fresh clone (scripts/kernel_schema.py): the
    # bastion tables are the ones the migrations build, never a copy.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    from server.network import bastion_autoupdate as bau
    from server.network import nac_commit

    mig = open(os.path.join(REPO, "migrations", "v176_bastion_autoupdate.sql")).read()
    async with pool.acquire() as c:
        # v176 is already in the schema; applied twice MORE, on top of every
        # migration after it, it must still be a no-op.
        await c.execute(mig)
        await c.execute(mig)
        for tid, entity, revoked in ((7, 9003, False), (8, 9003, False), (9, 9003, True), (10, 9003, False),
                                     (11, 99, False), (12, 9003, False)):
            await token(c, tid, entity, revoked)
        for row in ((7, "box-7", "0.33.0", None, "0"), (8, "box-8", "0.32.2", "0.33.0", "2 hours"),
                    (9, "box-9", "0.33.0", None, "0"), (10, "box-10", "0.33.0", None, "0"),
                    (11, "other-host", "0.33.0", None, "0"), (12, "long-dead", "0.33.0", None, "3 days")):
            await c.execute(BEAT_SQL, *row)
    check("the migration applies twice", True)

    check("no settings row reads as on", await bau.is_enabled(pool) is True)
    await bau.set_enabled(pool, False)
    check("off is stored", await bau.is_enabled(pool) is False)
    await bau.set_enabled(pool, True)
    check("on again is stored (upsert)", await bau.is_enabled(pool) is True)

    async def fake_update(pool_, eid, version, token_ids=None):
        return {"directed": [{"token_id": 7}, {"token_id": 8}, {"token_id": 9}], "directed_count": 3}

    async def online_first(pool_, eid):
        return {7: True, 8: False, 9: True}

    async def online_second(pool_, eid):
        return {7: False, 8: True, 9: True}

    q = "SELECT * FROM bastion_autoupdate_watch WHERE version='0.33.0'"
    saved = (nac_commit.update_bastions, bau._online_now)
    try:
        nac_commit.update_bastions, bau._online_now = fake_update, online_first
        await bau._direct(pool, 9003, "0.33.0", ring0=True)
        first = {r["token_id"]: (r["online_at_told"], r["told_at"]) for r in await pool.fetch(q)}
        nac_commit.update_bastions, bau._online_now = fake_update, online_second
        await bau._direct(pool, 9003, "0.33.0", ring0=True)
        second = {r["token_id"]: (r["online_at_told"], r["told_at"]) for r in await pool.fetch(q)}
    finally:
        nac_commit.update_bastions, bau._online_now = saved
    check("each told machine is recorded with whether it was beating",
          {k: v[0] for k, v in first.items()} == {7: True, 8: False, 9: True}, str(first))
    check("the first record wins — a later pass does not move told_at or the flag",
          first == second, f"{first} vs {second}")

    opened = await pool.fetchval("SELECT now() - interval '1 day'")
    await bau._observe(pool, 9003, "0.33.0", opened)
    took1 = {r["token_id"]: r["took_at"] for r in await pool.fetch(q)}
    check("observe marks every machine of this Host on the version, told or not "
          "(box-10 got there by a manual update)",
          took1.get(7) is not None and took1.get(10) is not None and took1.get(8) is None, str(took1))
    check("observe never records another Host's machine", 11 not in took1, str(took1))
    check("observe skips a revoked machine", took1.get(9) is None, str(took1))
    check("observe skips a machine that last reported the version days ago and is silent "
          "(a re-published old version must not count the dead as having taken it)",
          12 not in took1, str(took1))
    await bau._observe(pool, 9003, "0.33.0", opened)
    took2 = {r["token_id"]: r["took_at"] for r in await pool.fetch(q)}
    check("the first sighting wins", took1[7] == took2[7] and took1[10] == took2[10])
    # A machine that took it by hand and wedged 40 minutes ago, while passes were
    # skipped: it beat after the canary opened, so it still counts (round-3 note).
    async with pool.acquire() as c:
        await token(c, 13)
        await c.execute(BEAT_SQL, 13, "wedged-by-hand", "0.33.0", None, "40 minutes")
    await bau._observe(pool, 9003, "0.33.0", opened)
    took3 = {r["token_id"]: r["took_at"] for r in await pool.fetch(q)}
    check("a machine that beat on the version after the canary opened counts, even if silent now",
          took3.get(13) is not None, str(took3))
    await pool.execute("DELETE FROM bastion_autoupdate_watch WHERE token_id = 13")
    await bau._observe(pool, 9003, "0.33.0", None)
    took4 = {r["token_id"]: r["took_at"] for r in await pool.fetch(q)}
    check("with no opening time, only the last 15 minutes count", 13 not in took4, str(took4))
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = 13")
    rows = await bau._watched_rows(pool, "0.33.0")
    ids = sorted(r["token_id"] for r in rows)
    check("the verdict's rows drop a revoked machine", ids == [7, 8, 10], str(ids))
    r8 = next(r for r in rows if r["token_id"] == 8)
    check("the rows carry the pending directive and the last beat",
          r8["update_to_version"] == "0.33.0" and r8["last_seen_at"] is not None and r8["hostname"] == "box-8")

    await bau._record_state(pool, "0.33.0", 0, "canary")
    await bau._record_state(pool, "0.33.0", 0, "general", verdict="pass", reason="1 on it after 24h")
    st = await bau.status(pool)
    lt = st["latest"] or {}
    check("status reads the newest state row",
          lt.get("version") == "0.33.0" and lt.get("stage") == "general"
          and lt.get("verdict") == "pass" and lt.get("reason") == "1 on it after 24h", str(st))
    check("status carries the switch and the rule's numbers",
          st["enabled"] is True and st["soak_hours"] == 24 and st["silent_minutes"] == 15, str(st))
    await bau._record_state(pool, "0.33.0", 0, "general")
    lt = (await bau.status(pool))["latest"]
    check("a later plain pass keeps the recorded verdict", lt["verdict"] == "pass", str(lt))

    await bau._record_state(pool, "0.33.1", 0, "canary")
    got = []
    from server.network import pairing_grants

    async def fake_inbox(subject, body, payload):
        got.append(subject)
        return True
    saved_inbox, pairing_grants._inbox = pairing_grants._inbox, fake_inbox
    try:
        await bau._alert_once(pool, "0.33.1", "stalled", "none of your machines has taken it")
        await bau._alert_once(pool, "0.33.1", "stalled", "none of your machines has taken it")
        await bau._alert_once(pool, "0.33.1", "waiting", "box-8 went silent before taking it")
        await bau._alert_once(pool, "0.33.1", "waiting", "box-8 went silent before taking it")
    finally:
        pairing_grants._inbox = saved_inbox
    check("each kind is told exactly once per version, and a stall does not swallow a wait",
          len(got) == 2, str(got))

    await pool.close()
    print("PASS" if not FAILED else f"{FAILED} FAILED")
    return 1 if FAILED else 0


if not DSN:
    print('SKIP — no PEER_TEST_DSN. eval "$(scripts/peer_test_db.sh)" first.')
    sys.exit(2)
from kernel_schema import kernel_schema_db  # noqa: E402
with kernel_schema_db(DSN, entity_id=9003) as _db:
    sys.exit(asyncio.run(main(_db)))
