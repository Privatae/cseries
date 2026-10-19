#!/usr/bin/env python3
"""Invariant: nac-pdp-bound-default-preserve (P0).

The host-local NAC PDP enforces what was approved and nothing silently regresses
(per the invariant-ratchet rule). Ported host-local from the reverted platform check
— it now exercises the in-pod NACController (server/network/nac_controller.py), not a
fleet forwarder, and writes NOTHING to the live policy tables.

Two parts:

  1. PURE-LOGIC matrix (always runs; zero DB impact) — synthetic, ephemerally-signed
     policy fed straight through NACController._ingest:
       * healthy posture          -> matched allow  -> PERMIT
       * degraded / unknown        -> matched quarantine -> DENY  (CoA on posture drop)
       * no policy authored        -> PERMIT  (default-preserve: today's behavior)
       * signed snapshot tampered  -> signature breaks -> mesh FAILS CLOSED (DENY)
       * unsigned approved rule     -> loads + decides via its profile (bootstrap)
       * no pinned key (unarmed)    -> snapshots still load + enforce (engine on pre-P4)

  2. DB BINDING (skippable via SKIP_DB=1) — read-only: connect to the in-pod database,
     attach_pool, status() — proves v058 applied and the PDP binds + loads on a real pod.

Non-zero exit on any failure. Run in-pod: `docker exec <c> python3 /app/scripts/check_nac_engine.py`
"""
import asyncio
import base64
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# In-pod, /app is the package root (smoke runs this via `docker exec … /app/scripts`).
# For a local repo run, point CSERIES_ROOT at the checkout. One import path, no fallback.
sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network.nac_controller import NACController, _canonical

# ── ephemeral host signing key (no production key touched) ──
_priv = Ed25519PrivateKey.generate()
_PUB = _priv.public_key().public_bytes_raw()


def _sign(terms: dict) -> str:
    return base64.b64encode(_priv.sign(_canonical(terms))).decode()


_PROFILES = [
    {"id": 1, "name": "inv-full", "enforcement": "allow", "is_default": False,
     "permits": [{"direction": "inbound", "target": "self-host", "lane": "mcp", "ports": "8443"}]},
    {"id": 2, "name": "inv-quarantine", "enforcement": "quarantine", "is_default": False,
     "permits": []},
]


def _signed_row(rid, name, prio, match, enforcement, permits):
    terms = {"name": name, "priority": prio, "match": match,
             "enforcement": enforcement, "permits": permits}
    return {"id": rid, "name": name, "priority": prio, "match": match,
            "profile_id": (1 if enforcement == "allow" else 2),
            "signed_terms": terms, "signature": _sign(terms)}


def pure_tests() -> list[str]:
    fails: list[str] = []
    armed_policy = [
        _signed_row(10, "inv-bad-posture", 10, {"posture": ["unknown", "degraded"]}, "quarantine", []),
        _signed_row(20, "inv-healthy", 20, {"posture": ["healthy"]}, "allow",
                    [{"direction": "inbound", "lane": "mcp"}]),
    ]

    def permits(nac, ep):
        prof, _why = nac.decide(ep)
        return nac.permits_inbound(prof, lane="mcp")

    # ── armed matrix ──
    nac = NACController(verify_pubkey=_PUB)
    nac._ingest(_PROFILES, armed_policy)
    if nac._tamper:
        fails.append("armed: false tamper flag on valid signatures")
    if permits(nac, {"posture": "healthy"}) is not True:
        fails.append("armed: healthy posture should PERMIT (got deny)")
    if permits(nac, {"posture": "degraded"}) is not False:
        fails.append("armed: degraded posture should QUARANTINE (CoA) — got permit")
    if permits(nac, {"posture": "unknown"}) is not False:
        fails.append("armed: unknown posture should QUARANTINE (CoA) — got permit")

    # ── default-preserve: no policy -> permit ──
    nac_np = NACController(verify_pubkey=_PUB)
    nac_np._ingest(_PROFILES, [])
    prof, _ = nac_np.decide({"posture": "unknown"})
    if prof is not None:
        fails.append("default-preserve: no-policy host should have NO governing profile")
    if nac_np.permits_inbound(prof) is not True:
        fails.append("default-preserve: no-policy host should PERMIT (got deny)")

    # ── tamper: mutate signed_terms after signing -> fail closed ──
    nac_t = NACController(verify_pubkey=_PUB)
    bad = dict(armed_policy[0])
    bad_terms = dict(bad["signed_terms"]); bad_terms["enforcement"] = "allow"
    bad["signed_terms"] = bad_terms  # signature still over the ORIGINAL quarantine terms
    nac_t._ingest(_PROFILES, [bad, armed_policy[1]])
    if nac_t._tamper is not True:
        fails.append("tamper: a mutated signed snapshot was NOT detected")
    if permits(nac_t, {"posture": "healthy"}) is not False:
        fails.append("tamper: a tampered mesh must fail CLOSED (got permit)")

    # ── unsigned approved rule (pre-P4 bootstrap) loads + decides via its profile ──
    nac_u = NACController(verify_pubkey=_PUB)
    nac_u._ingest(_PROFILES, [{"id": 30, "name": "inv-unsigned", "priority": 5,
                               "match": {}, "profile_id": 1,
                               "signed_terms": None, "signature": None}])
    if nac_u._tamper:
        fails.append("unsigned: an approved-but-unsigned rule wrongly flagged tamper")
    if permits(nac_u, {"posture": "healthy"}) is not True:
        fails.append("unsigned: catch-all allow rule should PERMIT (got deny)")

    # ── unarmed bootstrap: no pinned key -> snapshots still load + enforce ──
    nac_b = NACController(verify_pubkey=None)
    nac_b._ingest(_PROFILES, armed_policy)
    if nac_b._tamper:
        fails.append("unarmed: should not flag tamper without a pinned key (engine must work pre-pin)")
    if permits(nac_b, {"posture": "degraded"}) is not False:
        fails.append("unarmed: degraded posture should still QUARANTINE")

    return fails


async def db_tests() -> list[str]:
    fails: list[str] = []
    if os.environ.get("SKIP_DB") == "1":
        print("  [db] SKIPPED (SKIP_DB=1)")
        return fails
    try:
        import asyncpg
    except ImportError:
        print("  [db] SKIPPED (asyncpg not importable)")
        return fails
    try:
        conn = await asyncpg.connect(host="/var/run/postgresql", user="entity", database="hostmem")
    except Exception as e:
        print(f"  [db] SKIPPED (database not reachable: {e})")
        return fails
    try:
        nac = NACController(pool=conn, entity_id=int(os.environ.get("ENTITY_ID", "0")))
        bound = await nac.attach_pool(conn)         # raises if v058 tables are absent
        st = await nac.status()
        if not bound or not st["bound"]:
            fails.append("db: NAC PDP failed to bind to the in-pod pool")
        print(f"  [db] OK — bound, {st['rules']} live rule(s), "
              f"default_preserve={st['default_preserve']}, armed={st['armed']}")
    except Exception as e:
        fails.append(f"db: attach_pool/status raised (v058 not applied?): {e}")
    finally:
        await conn.close()
    return fails


def main() -> int:
    fails = pure_tests()
    fails += asyncio.run(db_tests())
    if fails:
        print("NAC-ENGINE CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("NAC-ENGINE INVARIANTS OK — bound + default-preserve intact, CoA holds "
          "(posture drop quarantines), tamper fails closed, unarmed bootstrap works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
