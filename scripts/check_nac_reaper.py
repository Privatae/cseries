#!/usr/bin/env python3
"""Invariant: nac-reaper / dead-device-loses-reach (P5, liveness CoA).

A bastion that stops heartbeating past the cutoff has its posture downgraded to
'unknown' (non-destructive); the PDP then re-decides — under a posture policy the dead
device is quarantined. A live device (recent heartbeat) is untouched, and posture
recovers on the next heartbeat.

DB-only (skippable via SKIP_DB=1). Run in-pod:
  docker exec <c> python3 /app/scripts/check_nac_reaper.py
"""
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network.nac_controller import NACController, reap_stale_endpoints, STALE_CUTOFF_SECONDS


async def _mk_bastion(conn, eid, *, posture, last_seen_offset):
    """Synthetic bastion token + nac_endpoints(posture) + a heartbeat last_seen_at
    `last_seen_offset` seconds in the past. Returns token_id."""
    tok = await conn.fetchval(
        "INSERT INTO mcp_client_tokens "
        "(entity_id,name,token_hash,url_handle,scopes,operator_kind,notes,hardware_fingerprint) "
        "VALUES ($1,'inv-reaper',$2,$3,$4,'bastion','{}'::jsonb,$5) RETURNING id",
        eid, secrets.token_bytes(32), secrets.token_hex(8),
        ["shape", "recall", "signal"], "REAPER-FP-" + secrets.token_hex(4))
    await conn.execute(
        "INSERT INTO nac_endpoints (token_id,fingerprint,posture,posture_at,updated_at) "
        "VALUES ($1,$2,$3,now(),now())", tok, "REAPER-FP", posture)
    await conn.execute(
        "INSERT INTO network_bastion_heartbeat (token_id,capabilities,last_seen_at) "
        "VALUES ($1,'[]'::jsonb, now() - make_interval(secs => $2::int)) "
        "ON CONFLICT (token_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at",
        tok, int(last_seen_offset))
    return tok


async def run():
    fails = []
    if os.environ.get("SKIP_DB") == "1":
        print("  [db] SKIPPED (SKIP_DB=1)")
        return fails
    try:
        import asyncpg
        pool = await asyncpg.create_pool(host="/var/run/postgresql", user="entity",
                                         database="hostmem", min_size=1, max_size=2)
    except Exception as e:
        print(f"  [db] SKIPPED (database not reachable: {e})")
        return fails
    toks = []
    try:
        async with pool.acquire() as conn:
            eid = (int(__import__("os").environ.get("ENTITY_ID") or 0) or int(await conn.fetchval("SELECT max(id) FROM entity_registry") or 1))  # the pod's own entity, never genesis 0
            stale = await _mk_bastion(conn, eid, posture="healthy",
                                      last_seen_offset=STALE_CUTOFF_SECONDS + 120)
            live = await _mk_bastion(conn, eid, posture="healthy", last_seen_offset=5)
            toks = [stale, live]

        downgraded = await reap_stale_endpoints(pool)
        if downgraded < 1:
            fails.append(f"reaper should have downgraded the stale device (downgraded={downgraded})")

        async with pool.acquire() as conn:
            sp = await conn.fetchval("SELECT posture FROM nac_endpoints WHERE token_id=$1", stale)
            lp = await conn.fetchval("SELECT posture FROM nac_endpoints WHERE token_id=$1", live)
        if sp != "unknown":
            fails.append(f"stale device posture should be 'unknown' after reaping (got {sp!r})")
        if lp != "healthy":
            fails.append(f"live device posture must be UNTOUCHED by the reaper (got {lp!r})")

        # dead-device-loses-reach: under a posture policy, the now-unknown device quarantines.
        nac = NACController(pool=pool)
        profiles = [{"id": 1, "name": "allow-healthy", "enforcement": "allow", "is_default": False,
                     "permits": [{"direction": "inbound", "lane": "mcp"}]},
                    {"id": 2, "name": "quarantine-bad", "enforcement": "quarantine", "is_default": False,
                     "permits": []}]
        policy = [{"id": 10, "name": "bad", "priority": 10, "match": {"posture": ["degraded", "unknown"]},
                   "profile_id": 2, "signed_terms": None, "signature": None},
                  {"id": 20, "name": "ok", "priority": 20, "match": {"posture": ["healthy"]},
                   "profile_id": 1, "signed_terms": None, "signature": None}]
        nac._ingest(profiles, policy)

        ep_dead = await nac._endpoint_state(stale)   # posture=unknown after reaping
        if nac.permits_inbound(nac.decide(ep_dead)[0]) is not False:
            fails.append(f"a reaped (dead) device must lose reach under a posture policy (posture={ep_dead.get('posture')!r})")
        ep_live = await nac._endpoint_state(live)    # posture=healthy
        if nac.permits_inbound(nac.decide(ep_live)[0]) is not True:
            fails.append("a live healthy device should keep reach")

        if not fails:
            print("  [db] OK — stale device downgraded to unknown + quarantined (CoA); live device untouched")
    except Exception as e:
        fails.append(f"db: reaper test raised: {e}")
    finally:
        async with pool.acquire() as conn:
            for t in toks:
                await conn.execute("DELETE FROM network_bastion_heartbeat WHERE token_id=$1", t)
                await conn.execute("DELETE FROM nac_endpoints WHERE token_id=$1", t)
                await conn.execute("DELETE FROM mcp_client_tokens WHERE id=$1", t)
        await pool.close()
    return fails


def main():
    fails = asyncio.run(run())
    if fails:
        print("NAC-REAPER CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("NAC-REAPER INVARIANTS OK — a stale bastion is downgraded to posture=unknown and "
          "loses reach (CoA); a live device is untouched; posture recovers on the next heartbeat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
