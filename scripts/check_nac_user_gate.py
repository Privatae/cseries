#!/usr/bin/env python3
"""Invariant: nac-user-gate (P4, step-up + signing).

Committing an ALLOW policy requires the host-local TOTP step-up; the approved terms are
signed with the host's entity key, which ARMS the PDP's fail-closed verify.

Pure (always):
  * TOTP: the current code verifies; a wrong / malformed code does not.

DB (skippable via SKIP_DB=1):
  * verify_stepup is FALSE before a factor is enrolled (so an allow cannot commit), and a
    wrong code never verifies.
  * sign_policy_terms + a PDP armed with the host's pubkey: a host-signed approved policy
    loads + verifies (not tamper); mutating signed_terms after approval -> tamper -> deny.

Run in-pod: docker exec <c> python3 /app/scripts/check_nac_user_gate.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network import nac_stepup
from server.network.nac_controller import NACController


def pure_tests():
    fails = []
    secret = nac_stepup._b32encode(b"0123456789012345")
    code = nac_stepup.current_code(secret)
    if not nac_stepup.verify_totp(secret, code):
        fails.append("the current TOTP code must verify")
    if nac_stepup.verify_totp(secret, "000000" if code != "000000" else "111111"):
        fails.append("a wrong TOTP code must NOT verify")
    if nac_stepup.verify_totp(secret, "12345") or nac_stepup.verify_totp(secret, "abcdef"):
        fails.append("a malformed TOTP code must NOT verify")
    # Burn-on-use needs the window a code matched: the current code names the
    # current counter; a code from the previous window names that one (skew).
    at = 1_700_000_000.0
    now_c = int(at // nac_stepup._TOTP_PERIOD)
    if nac_stepup.matched_counter(secret, nac_stepup.current_code(secret, at=at), at=at) != now_c:
        fails.append("matched_counter must name the current window for the current code")
    prev = nac_stepup.current_code(secret, at=at - nac_stepup._TOTP_PERIOD)
    if nac_stepup.matched_counter(secret, prev, at=at) != now_c - 1:
        fails.append("matched_counter must name the previous window for last window's code")
    if nac_stepup.matched_counter(secret, "000000" if prev != "000000" else "111111", at=at) is not None:
        fails.append("matched_counter must be None for a wrong code")
    # Purpose strings must equal the gateway's stepup.go byte for byte (same
    # fixtures as its TestStepupPurposeStrings).
    g = {"server": "box1", "root": "/data", "mask": ["search", "read"], "excludes": [".ssh", ".env"],
         "sweep": "hourly", "sensitive": False, "ttl_days": 365}
    if nac_stepup.purpose_for_store_terms(g) != "dcn_store_grant|box1|/data|read,search|.env,.ssh|hourly|false|ttl:365":
        fails.append(f"store purpose drifted from the box: {nac_stepup.purpose_for_store_terms(g)!r}")
    p = {"store_id": 5, "plan_id": 9, "root": "/data", "archive": "Archive",
         "moves": [{"from": "Downloads/a.exe", "to": "Archive/a.exe"}, {"from": "b", "to": "c/b"}]}
    want = "dcn_move_plan|5|9|/data|Archive|f4790ec430c605a396065aab9b9c2e52e60b875c4c4060a0136210833b6e7315"
    if nac_stepup.purpose_for_plan_terms(p) != want:
        fails.append(f"plan purpose drifted from the box: {nac_stepup.purpose_for_plan_terms(p)!r}")
    # Delimiter collisions are refused, never folded (a pod signing excludes
    # [".env,.ssh"] under an approval read as two patterns).
    for bad in ({**g, "excludes": [".env,.ssh"]}, {**g, "root": "/da|ta"}, {**g, "mask": ["re|ad"]}):
        try:
            nac_stepup.purpose_for_store_terms(bad)
            fails.append(f"reserved character must be refused: {bad}")
        except ValueError:
            pass
    try:
        nac_stepup.purpose_for_plan_terms({"store_id": 1, "plan_id": 1, "root": "/d", "archive": "A",
                                           "moves": [{"from": "a\tb", "to": "c"}]})
        fails.append("a move path with a tab must be refused")
    except ValueError:
        pass
    # The details the box shows must carry exactly the purpose's fields.
    d = nac_stepup.store_request_details(g, display_name="Docs", note="why")
    if d["kind"] != "dcn_store_grant" or d["sweep"] != "hourly" or d["sensitive"] is not False \
            or d["mask"] != ["search", "read"] or d["display_name"] != "Docs" or d["ttl_days"] != 365:
        fails.append(f"store_request_details wrong: {d}")
    pd = nac_stepup.plan_request_details(p, title="Cleanup")
    if pd["moves"] != [{"from": "Downloads/a.exe", "to": "Archive/a.exe"}, {"from": "b", "to": "c/b"}] \
            or pd["plan_id"] != 9 or pd["title"] != "Cleanup":
        fails.append(f"plan_request_details wrong: {pd}")
    # No pool / no box → not enrolled (the host's factor applies) — never 'approved'.
    r = asyncio.run(nac_stepup.box_confirmation(None, 1, server="box1", purpose=want, details=pd))
    if r.get("state") != "not_enrolled":
        fails.append(f"box_confirmation without a pool must be not_enrolled: {r}")
    # An enrolled but unreachable box is OFFLINE — never a fallback to the host's factor.
    async def offline_case():
        orig_enrolled, orig_dispatch = nac_stepup.box_stepup_enrolled, None
        from server.network import nac_dcn
        orig_dispatch = nac_dcn._dispatch_box
        async def enrolled(pool, server): return True
        async def dispatch(pool, eid, server, op, args, token_id=None): return {"_offline": True, "stderr": "no worker"}
        nac_stepup.box_stepup_enrolled, nac_dcn._dispatch_box = enrolled, dispatch
        try:
            return await nac_stepup.box_confirmation(object(), 1, server="box1", purpose=want, details=pd)
        finally:
            nac_stepup.box_stepup_enrolled, nac_dcn._dispatch_box = orig_enrolled, orig_dispatch
    r = asyncio.run(offline_case())
    if r.get("state") != "offline":
        fails.append(f"an enrolled, unreachable box must be 'offline', never a host fallback: {r}")
    return fails


async def db_tests():
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
    try:
        eid = (int(__import__("os").environ.get("ENTITY_ID") or 0) or int(await pool.fetchval("SELECT max(id) FROM entity_registry") or 1))  # the pod's own entity, never genesis 0

        # ── step-up gate: the security property is "no allow without an enrolled factor" ──
        secret = await nac_stepup.ensure_stepup_secret(pool)
        good = nac_stepup.current_code(secret)
        enrolled = await nac_stepup.is_enrolled(pool)
        if enrolled:
            if not await nac_stepup.verify_stepup(pool, good):
                fails.append("enrolled host: a valid step-up code should verify")
            if await nac_stepup.verify_stepup(pool, "000000" if good != "000000" else "111111"):
                fails.append("a wrong step-up code must not verify")
        else:
            if await nac_stepup.verify_stepup(pool, good):
                fails.append("un-enrolled host: verify_stepup must be FALSE even for a valid code "
                             "(an allow cannot commit without an armed second factor)")

        # ── burn-on-use: a counter is accepted once, and only if newer ──
        saved = await pool.fetchval("SELECT secret FROM kernel_secrets WHERE id=$1",
                                    nac_stepup._TOTP_LAST_COUNTER_ID)
        try:
            await pool.execute("DELETE FROM kernel_secrets WHERE id=$1", nac_stepup._TOTP_LAST_COUNTER_ID)
            if not await nac_stepup.consume_counter(pool, 1000):
                fails.append("first use of a counter must be accepted")
            if await nac_stepup.consume_counter(pool, 1000):
                fails.append("REPLAY: the same counter must NOT be accepted twice")
            if await nac_stepup.consume_counter(pool, 999):
                fails.append("an older counter than the last accepted must NOT be accepted")
            if not await nac_stepup.consume_counter(pool, 1001):
                fails.append("a newer counter must be accepted")
            if enrolled:
                # end to end: the same real code cannot approve twice
                good2 = nac_stepup.current_code(secret)
                first = await nac_stepup.verify_stepup(pool, good2)
                second = await nac_stepup.verify_stepup(pool, good2)
                if not first or second:
                    fails.append(f"enrolled host: a code must verify once then burn (first={first}, second={second})")
        finally:
            if saved is None:
                await pool.execute("DELETE FROM kernel_secrets WHERE id=$1", nac_stepup._TOTP_LAST_COUNTER_ID)
            else:
                await pool.execute("UPDATE kernel_secrets SET secret=$2 WHERE id=$1",
                                   nac_stepup._TOTP_LAST_COUNTER_ID, saved)

        # ── signing arms the PDP verify; a tamper after approval fails closed ──
        terms = {"name": "ug-test", "priority": 50, "match": {"posture": ["healthy"]},
                 "enforcement": "allow", "permits": [{"direction": "inbound", "lane": "mcp"}]}
        _key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
        pub = await nac_stepup.entity_verify_pubkey(pool, eid)
        profiles = [{"id": 1, "name": "ug-test", "enforcement": "allow", "is_default": False,
                     "permits": terms["permits"]}]
        good_row = {"id": 99, "name": "ug-test", "priority": 50, "match": terms["match"],
                    "profile_id": 1, "signed_terms": terms, "signature": sig}

        nac = NACController(verify_pubkey=pub)
        nac._ingest(profiles, [good_row])
        if nac._tamper:
            fails.append("a correctly host-signed policy must verify (false tamper flag)")
        if nac.permits_inbound(nac.decide({"posture": "healthy"})[0]) is not True:
            fails.append("a verified allow policy should permit a healthy device")

        bad_terms = dict(terms); bad_terms["enforcement"] = "quarantine"  # mutate after signing
        bad_row = dict(good_row); bad_row["signed_terms"] = bad_terms
        nac2 = NACController(verify_pubkey=pub)
        nac2._ingest(profiles, [bad_row])
        if not nac2._tamper:
            fails.append("a host-signed policy mutated after approval must be detected as tamper")
        if nac2.permits_inbound(nac2.decide({"posture": "healthy"})[0]) is not False:
            fails.append("a tampered policy must fail closed (deny)")

        if not fails:
            print(f"  [db] OK — step-up gate holds (enrolled={enrolled}); host-signed policy "
                  f"verifies; tamper after approval fails closed")
    except Exception as e:
        fails.append(f"db: user-gate test raised: {e}")
    finally:
        await pool.close()
    return fails


def main():
    fails = pure_tests()
    fails += asyncio.run(db_tests())
    if fails:
        print("NAC-USER-GATE CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("NAC-USER-GATE INVARIANTS OK — TOTP step-up verifies, an allow can't commit "
          "without an enrolled factor, host-signed policy verifies, tamper fails closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
