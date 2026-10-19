#!/usr/bin/env python3
"""End to end: a live-fleet heartbeat must land posture='healthy', not 'degraded'.

check_box_ops_sync.py asserts the same thing at the unit level and runs anywhere.
This drives the whole path — upsert_heartbeat -> grant_drift -> unknown_box_ops ->
record_endpoint_posture -> the nac_endpoints row — against a real Postgres,
because `posture` in that row IS the defect: 26 of one Host's 39 endpoints and
one Host's only one were sitting at 'degraded', and posture is
what the PDP matches on (nac_controller._match / min_posture :250).

A stub pool would assert my reading of the upsert rather than the upsert, which
is the same reason test_heartbeat_anchor_clears.py insists on a real database.

Run:  eval "$(scripts/peer_test_db.sh)" && python3 scripts/test_box_ops_not_degraded.py
"""
import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

from check_box_ops_sync import LIVE_FLEET_OPS  # noqa: E402  (one copy of the measured list)

TOKEN = 90021
ENTITY = 1
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


async def main(dsn):

    import asyncpg
    from server.network import nac_dcn, topology

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    # The tables are the KERNEL's (see _database below): no DDL here.
    for q in ("DELETE FROM nac_endpoints WHERE token_id = $1",
              "DELETE FROM network_bastion_heartbeat WHERE token_id = $1"):
        await pool.execute(q, TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    # url_handle is NOT NULL and uniquely indexed among live rows, so it is derived from
    # TOKEN rather than hardcoded: two gates sharing a literal would collide on that
    # index in a pod where both have run.
    await pool.execute(
        "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
        "VALUES ($1, $2, 'gate', '\\x00'::bytea, $3, 'bastion')",
        TOKEN, ENTITY, f"box-ops-gate-{TOKEN}")

    # The box holds exactly the one grant this host issued: nothing here is a
    # grant fault, so the ONLY thing that could degrade it is the op vocabulary.
    granted = [{"store_id": 1, "signature": "s1"}]
    real_expected = nac_dcn.expected_box_grants

    async def fake_expected(pool_, eid_, hostname_):
        return granted

    async def beat(mgmt_ops):
        nac_dcn.expected_box_grants = fake_expected
        try:
            got = await topology.upsert_heartbeat(pool, ENTITY, {
                "token_id": TOKEN, "hostname": "box", "platform": "linux",
                "tunnel_status": "up", "device_health": "healthy",
                "enforcing": {"grants": granted, "mgmt_ops": mgmt_ops}})
            if got is None:
                raise SystemExit("upsert_heartbeat refused the gate's own token — fixture wrong")
        finally:
            nac_dcn.expected_box_grants = real_expected
        return await pool.fetchrow(
            "SELECT posture, last_decision FROM nac_endpoints WHERE token_id = $1", TOKEN)

    # 1. THE DEFECT. The vocabulary a real bastion reported while being degraded.
    r = await beat(LIVE_FLEET_OPS)
    check(r["posture"] == "healthy",
          "a live-fleet heartbeat lands HEALTHY (26 of entity-9003's endpoints were degraded)",
          f"got {r['posture']!r} / {(r['last_decision'] or '')[:100]!r}")
    check("does not know" not in (r["last_decision"] or ""),
          "and nothing is reported as unknown", f"got {r['last_decision']!r}")

    # 2. A bastion AHEAD of this host: informational, still healthy.
    r = await beat(LIVE_FLEET_OPS + ["op_from_a_future_bastion"])
    check(r["posture"] == "healthy",
          "a box advertising an op this host never heard of stays HEALTHY",
          f"got {r['posture']!r}")
    check("op_from_a_future_bastion" in (r["last_decision"] or "")
          and "not a fault" in (r["last_decision"] or ""),
          "the unfamiliar op is surfaced, marked as not a fault",
          f"got {r['last_decision']!r}")

    # 3. Real grant drift still degrades — the thing that must NOT be weakened.
    nac_dcn.expected_box_grants = fake_expected
    try:
        await topology.upsert_heartbeat(pool, ENTITY, {
            "token_id": TOKEN, "hostname": "box", "platform": "linux",
            "tunnel_status": "up", "device_health": "healthy",
            "enforcing": {"grants": [{"store_id": 1, "signature": "STALE"}],
                          "mgmt_ops": LIVE_FLEET_OPS}})
    finally:
        nac_dcn.expected_box_grants = real_expected
    r = await pool.fetchrow(
        "SELECT posture, last_decision FROM nac_endpoints WHERE token_id = $1", TOKEN)
    check(r["posture"] == "degraded",
          "a box holding a STALE copy of a grant is still degraded",
          f"got {r['posture']!r}")
    check("stale copy" in (r["last_decision"] or ""),
          "and still says why", f"got {r['last_decision']!r}")

    for q in ("DELETE FROM nac_endpoints WHERE token_id = $1",
              "DELETE FROM network_bastion_heartbeat WHERE token_id = $1"):
        await pool.execute(q, TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    await pool.close()




def _database():
    """Where this gate runs: only a database it may write to, because it
    DELETEs and INSERTs rows under fixed ids.
      - Out of pod (PEER_TEST_DSN / TEST_DSN, a test SERVER): a fresh clone of
        the KERNEL's schema (scripts/kernel_schema.py, the schema entrypoint.sh
        builds), made for this run and dropped after it.
      - In smoke (docker exec): the database smoke made for this run, named by
        scripts/smoke_test.sh in GATE_SCRATCH_DSN (its own throwaway
        container's).
    Nothing else. It used to fall back to the pod socket whenever
    /var/run/postgresql existed, and inside a Host's pod that is the Host's own
    database. Never a hand copy either: one let this gate pass out of pod and
    fail in the pod on a release (url_handle NOT NULL)."""
    import contextlib
    server = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if server:
        sys.path.insert(0, os.path.join(_ROOT, "scripts"))
        from kernel_schema import kernel_schema_db
        return kernel_schema_db(server, entity_id=ENTITY)
    scratch = os.environ.get("GATE_SCRATCH_DSN")
    if scratch:
        return contextlib.nullcontext(scratch)
    return None


def _no_database(skip_code: int) -> int:
    """No database this gate may write to. In a gate lane (GATE_LANE, which
    smoke sets) that is a FAILURE, never a quiet skip; by hand it is a skip."""
    lane = os.environ.get("GATE_LANE")
    if lane:
        print(f"FAIL — the {lane} lane named no database this gate may write to "
              "(GATE_SCRATCH_DSN, or a test server in PEER_TEST_DSN/TEST_DSN). It never "
              "picks one itself: a Host's pod socket is that Host's own database.")
        return 1
    print("SKIP — no test database (it writes rows, so it never picks one itself). "
          f"Start one:  eval \"$(bash {os.path.join(_ROOT, 'scripts/peer_test_db.sh')})\"")
    return skip_code


_ctx = _database()
if _ctx is None:
    sys.exit(_no_database(2))
with _ctx as _dsn:
    asyncio.run(main(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"box ops end to end: {CHECKS}/{CHECKS} checks passed")
sys.exit(0)
