#!/usr/bin/env python3
"""Invariant: nac-egress / the user's outbound layer (P6, decision core).

The host's egress policy is DEFAULT-OPEN: no rule => the host reaches everything. The
user opts in to limit (a deny tightens; explicit allows + a `*` catch-all deny build an
allowlist). Rules are signed by the host; a tampered rule is SKIPPED — default-open means
a tamper can only widen, never cut the host off. (Where the allowed traffic then EXITS —
a user bastion — is the separate routing layer; this gate pins the decision core.)

Pure parts always run; the sign/verify part needs the pod DB (skip via SKIP_DB=1). Run:
  docker exec <c> python3 /app/scripts/check_nac_egress.py
"""
import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network.nac_egress import egress_decision, load_egress_rules, _host_matches, _is_platform_dest


def _pure():
    fails = []
    if egress_decision([], "api.x.ai").allowed is not True:
        fails.append("default must be OPEN (no rules => allowed)")
    deny = [{"pattern": "evil.example", "action": "deny", "priority": 100}]
    if egress_decision(deny, "evil.example").allowed is not False:
        fails.append("a deny rule must block its host")
    if egress_decision(deny, "api.x.ai").allowed is not True:
        fails.append("a deny rule must NOT touch other hosts (still open)")
    allowlist = [{"pattern": "api.x.ai", "action": "allow", "priority": 10},
                 {"pattern": "*", "action": "deny", "priority": 1000}]
    if egress_decision(allowlist, "api.x.ai").allowed is not True:
        fails.append("allowlist: the explicit allow must win (first match by priority)")
    if egress_decision(allowlist, "tracker.example").allowed is not False:
        fails.append("allowlist: the `*` catch-all must deny everything else")
    if not _host_matches("*.x.ai", "api.x.ai") or not _host_matches("*.x.ai", "x.ai"):
        fails.append("*.x.ai should match x.ai and its subdomains")
    if _host_matches("*.x.ai", "x.ai.evil.com"):
        fails.append("*.x.ai must NOT match x.ai.evil.com (suffix only)")
    # platform-owned destinations never ride the bastion (routing, not policy)
    os.environ["GITEA_URL"] = "https://forge.example"
    if not _is_platform_dest("forge.example") or not _is_platform_dest("FORGE.example"):
        fails.append("the platform's gitea host must be a platform destination (direct path)")
    if _is_platform_dest("api.x.ai") or _is_platform_dest("forge.example.evil.com") or _is_platform_dest(None):
        fails.append("only the platform's own hosts are platform destinations")
    os.environ["PLATFORM_DIRECT_HOSTS"] = "extra.example, "
    if not _is_platform_dest("extra.example"):
        fails.append("PLATFORM_DIRECT_HOSTS must extend the platform destination set")
    return fails


async def _db():
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
    from server.network import nac_stepup
    ids = []
    try:
        eid = (int(__import__("os").environ.get("ENTITY_ID") or 0) or int(await pool.fetchval("SELECT max(id) FROM entity_registry") or 1))  # the pod's own entity, never genesis 0
        terms = {"pattern": "blocked.example", "action": "deny", "priority": 50}
        key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
        rid = await pool.fetchval(
            "INSERT INTO nac_egress (pattern,action,priority,proposed_by,approved_at,"
            "approved_by,signing_key_id,signed_terms,signature) "
            "VALUES ($1,$2,$3,'host',now(),'inv',$4,$5::jsonb,$6) RETURNING id",
            terms["pattern"], terms["action"], terms["priority"], key_id, json.dumps(terms), sig)
        ids.append(rid)
        pub = await nac_stepup.entity_verify_pubkey(pool, eid)

        rules = await load_egress_rules(pool, pub)
        if not any(r["id"] == rid for r in rules):
            fails.append("a signed, approved rule should load + verify")
        elif egress_decision(rules, "blocked.example").allowed is not False:
            fails.append("the signed deny rule should block its host")

        # tamper: corrupt the signature -> the rule MUST be skipped (default-open preserved)
        await pool.execute("UPDATE nac_egress SET signature=$2 WHERE id=$1",
                           rid, base64.b64encode(b"\x00" * 64).decode())
        rules2 = await load_egress_rules(pool, pub)
        if any(r["id"] == rid for r in rules2):
            fails.append("a tampered rule must be SKIPPED (signature fails closed on the rule)")
        if egress_decision(rules2, "blocked.example").allowed is not True:
            fails.append("after tamper the host falls back to OPEN (a tamper never cuts it off)")

        # unsigned: an approved rule with NO signature is a direct-DB-write bypass of the
        # user-gate (which signs every rule it approves). Under an armed gate it MUST be
        # skipped — the host cannot set the USER's egress policy without the user's signature.
        urid = await pool.fetchval(
            "INSERT INTO nac_egress (pattern,action,priority,proposed_by,approved_at,approved_by) "
            "VALUES ('forged.example','deny',40,'host',now(),'inv') RETURNING id")
        ids.append(urid)
        rules3 = await load_egress_rules(pool, pub)
        if any(r["id"] == urid for r in rules3):
            fails.append("an approved-but-UNSIGNED rule must be SKIPPED under an armed gate "
                         "(no user signature => no authority)")
        if egress_decision(rules3, "forged.example").allowed is not True:
            fails.append("an unsigned forged deny must not take effect (stays OPEN)")

        if not fails:
            print("  [db] OK — signed rule verifies + enforces; tampered + unsigned rules are "
                  "skipped (fails open; the host can't forge the user's egress policy)")
    except Exception as e:
        fails.append(f"db: {e}")
    finally:
        for i in ids:
            await pool.execute("DELETE FROM nac_egress WHERE id=$1", i)
        await pool.close()
    return fails


async def _routing():
    """The fix itself, not just the helper: with an egress bastion CONNECTED, a request for
    the platform's gitea must take _direct_dispatch, and a third-party host must still go
    to the bastion relay. Stubs the relay and both dispatchers; no sockets."""
    from server.network import nac_egress, egress_relay
    fails = []
    calls = []

    class _Relay:
        def worker_count(self, token_id=None):
            return 1
        def has_bastion(self, token_id):
            return True
        async def dispatch(self, head, reader, writer, timeout=0, bastion_token_id=None):
            calls.append("bastion"); return True

    class _R:
        def __init__(self, data): self._d = data
        async def read(self, n): d, self._d = self._d, b""; return d
    class _W:
        def write(self, b): pass
        async def drain(self): pass
        def close(self): pass

    real_relay, real_direct = egress_relay.get_egress_relay, nac_egress.EgressProxy._direct_dispatch
    egress_relay.get_egress_relay = lambda: _Relay()
    async def _direct(self, head, reader, writer, method, target):
        calls.append("platform")
    nac_egress.EgressProxy._direct_dispatch = _direct
    try:
        os.environ["GITEA_URL"] = "https://forge.example"
        proxy = nac_egress.EgressProxy(pool=None, entity_id=0, verify_pubkey=None)
        proxy._rules_at = float("inf")  # no DB: keep the (empty, default-open) rules
        await proxy._handle(_R(b"CONNECT forge.example:443 HTTP/1.1\r\nHost: forge.example:443\r\n\r\n"), _W())
        await proxy._handle(_R(b"CONNECT api.x.ai:443 HTTP/1.1\r\nHost: api.x.ai:443\r\n\r\n"), _W())
    finally:
        egress_relay.get_egress_relay, nac_egress.EgressProxy._direct_dispatch = real_relay, real_direct
    if calls != ["platform", "bastion"]:
        fails.append(f"routing: gitea must take the platform path and third parties the bastion "
                     f"when a bastion is connected (got {calls})")
    return fails


def main():
    fails = _pure()
    fails += asyncio.run(_routing())
    fails += asyncio.run(_db())
    if fails:
        print("NAC-EGRESS CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("NAC-EGRESS INVARIANTS OK — default-open; deny tightens; allowlist via `*`; signed "
          "rules verify + enforce; a tampered rule is skipped (fails open, never cuts the host off); "
          "platform hosts (gitea) take the platform path, never the bastion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
