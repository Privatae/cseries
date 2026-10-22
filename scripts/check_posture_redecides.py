#!/usr/bin/env python3
"""Invariant: posture-drop-redecides (P2, device_health self-report).

The bastion self-reports device_health on its heartbeat; the host writes it (plus a
server/workstation classification and the CSR-bound fingerprint) into nac_endpoints,
and the PDP re-decides — a posture DROP flips an otherwise-permitted device to quarantine.

Pure (always):
  * _norm_posture validates healthy/degraded/unknown, rejects junk -> None
  * _classify maps platform/hostname -> server | workstation | unknown

DB (skippable via SKIP_DB=1):
  * record_endpoint_posture(device_health="healthy") -> nac_endpoints.posture='healthy'
    and the PDP (with a posture policy) PERMITS;
  * a later record_endpoint_posture(device_health="degraded") flips posture and the SAME
    PDP QUARANTINES (re-decided from the heartbeat, no policy change).

Run in-pod: docker exec <c> python3 /app/scripts/check_posture_redecides.py
"""
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network.nac_controller import (
    NACController, record_endpoint_posture, _norm_posture, _classify,
)


def _signed_policy(rows):
    """(verify key, rows) — each rule signed with an ephemeral stand-in for the Host's key,
    because the engine enforces only signed terms (an approved rule with no signature fails
    its check and locks every machine out)."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from server.network.nac_controller import _canonical
    key = Ed25519PrivateKey.generate()
    out = []
    for r, enforcement, permits in rows:
        terms = {"name": r["name"], "priority": r["priority"], "match": r["match"],
                 "enforcement": enforcement, "permits": permits}
        out.append(dict(r, signed_terms=terms,
                        signature=base64.b64encode(key.sign(_canonical(terms))).decode()))
    return key.public_key().public_bytes_raw(), out


def pure_tests():
    fails = []
    if _norm_posture("healthy") != "healthy" or _norm_posture("DEGRADED") != "degraded":
        fails.append("_norm_posture should accept + lowercase known postures")
    if _norm_posture("frobnicated") is not None or _norm_posture(None) is not None:
        fails.append("_norm_posture should reject unknown values / None -> None")
    if _classify("mac-laptop", "darwin") != "workstation":
        fails.append("_classify: darwin -> workstation")
    if _classify("prod-web-server", "linux") != "server":
        fails.append("_classify: linux server-ish hostname -> server")
    if _classify("", "linux") != "server":
        fails.append("_classify: bare linux -> server (default)")
    return fails


async def db_tests():
    fails = []
    if os.environ.get("SKIP_DB") == "1":
        print("  [db] SKIPPED (SKIP_DB=1)")
        return fails
    try:
        import asyncpg
        conn = await asyncpg.connect(host="/var/run/postgresql", user="entity", database="hostmem")
    except Exception as e:
        print(f"  [db] SKIPPED (database not reachable: {e})")
        return fails
    eid = await conn.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
    if eid is None:
        print("  [db] SKIPPED (no entity in registry)")
        await conn.close()
        return fails

    # A PDP loaded (in-memory, no DB-policy pollution) with a posture-conditioned policy:
    # bad posture -> quarantine, healthy -> allow.
    profiles = [
        {"id": 1, "name": "allow-healthy", "enforcement": "allow", "is_default": False,
         "permits": [{"direction": "inbound", "lane": "mcp"}]},
        {"id": 2, "name": "quarantine-bad", "enforcement": "quarantine", "is_default": False,
         "permits": []},
    ]
    pub, policy = _signed_policy([
        ({"id": 10, "name": "bad-posture", "priority": 10, "match": {"posture": ["degraded", "unknown"]},
          "profile_id": 2}, "quarantine", []),
        ({"id": 20, "name": "healthy", "priority": 20, "match": {"posture": ["healthy"]},
          "profile_id": 1}, "allow", [{"direction": "inbound", "lane": "mcp"}]),
    ])
    nac = NACController(pool=conn, verify_pubkey=pub)
    nac._ingest(profiles, policy)

    tok = None
    try:
        tok = await conn.fetchval(
            "INSERT INTO mcp_client_tokens "
            "(entity_id, name, token_hash, url_handle, scopes, operator_kind, notes, hardware_fingerprint) "
            "VALUES ($1,'inv-posture-bastion',$2,$3,$4,'bastion','{}'::jsonb,'POSTURE-FP') RETURNING id",
            eid, secrets.token_bytes(32), secrets.token_hex(8), ["shape", "recall", "signal"])

        async def decide_now():
            ep = await nac._endpoint_state(tok)
            prof, _why = nac.decide(ep)
            return ep.get("posture"), nac.permits_inbound(prof, lane="mcp")

        # healthy self-report -> PERMIT
        await record_endpoint_posture(conn, token_id=tok, device_health="healthy",
                                      tunnel_status="up", hostname="box", platform="linux")
        p1, allow1 = await decide_now()
        if p1 != "healthy" or allow1 is not True:
            fails.append(f"healthy report should permit (posture={p1!r}, allowed={allow1})")

        # posture DROP -> the SAME PDP re-decides to QUARANTINE
        await record_endpoint_posture(conn, token_id=tok, device_health="degraded",
                                      tunnel_status="up", hostname="box", platform="linux")
        p2, allow2 = await decide_now()
        if p2 != "degraded" or allow2 is not False:
            fails.append(f"degraded report should quarantine (posture={p2!r}, allowed={allow2})")

        # classification + fingerprint were recorded from the heartbeat / token
        row = await conn.fetchrow(
            "SELECT classification, fingerprint FROM nac_endpoints WHERE token_id=$1", tok)
        if row["classification"] != "server" or row["fingerprint"] != "POSTURE-FP":
            fails.append(f"endpoint state mis-recorded: {dict(row)}")

        # NAC agent: the host's verdict carries its reason; a beat
        # without one keeps the last reason instead of blanking it.
        await record_endpoint_posture(conn, token_id=tok, device_health="degraded",
                                      tunnel_status="up", hostname="box", platform="linux",
                                      detail="drift: store 1: granted here, not held on the box")
        why = await conn.fetchval("SELECT last_decision FROM nac_endpoints WHERE token_id=$1", tok)
        if not (why or "").startswith("drift: store 1"):
            fails.append(f"detail must land in last_decision: {why!r}")
        await record_endpoint_posture(conn, token_id=tok, device_health="degraded",
                                      tunnel_status="up", hostname="box", platform="linux")
        why2 = await conn.fetchval("SELECT last_decision FROM nac_endpoints WHERE token_id=$1", tok)
        if why2 != why:
            fails.append(f"a beat without detail must keep the last reason: {why2!r}")
        if not fails:
            print("  [db] OK — heartbeat posture healthy->PERMIT, drop to degraded->QUARANTINE (re-decided)")
    except Exception as e:
        fails.append(f"db: posture-redecides test raised: {e}")
    finally:
        if tok is not None:
            await conn.execute("DELETE FROM nac_endpoints WHERE token_id=$1", tok)
            await conn.execute("DELETE FROM mcp_client_tokens WHERE id=$1", tok)
        await conn.close()
    return fails


def main():
    fails = pure_tests()
    fails += asyncio.run(db_tests())
    if fails:
        print("POSTURE-REDECIDES CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("POSTURE-REDECIDES INVARIANTS OK — heartbeat self-report writes posture to "
          "nac_endpoints, classification + CSR-bound fp recorded, and a posture drop "
          "re-decides the PDP to quarantine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
