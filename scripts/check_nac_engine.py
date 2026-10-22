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
       * unsigned approved rule     -> fails its check ("unsigned") -> FAILS CLOSED
       * priority columns swapped   -> order still follows the SIGNED priority
       * no entity id (unarmed)     -> signed snapshots still load + enforce (genesis)

  2. KEY + NOTICE (stand-in pool, no DB) — a key unreadable at boot enforces no signed
     rule, the next reload re-arms once it reads; a lock is told to the Host's inbox once
     per lock, under an id a restart in the same lock reuses.

  3. DB BINDING (skippable via SKIP_DB=1) — read-only: connect to the in-pod database,
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

    # ── unsigned approved rule: approve signs in the same write, so a row with no signature
    #    is one whose signature was stripped. It fails its check; it is never read from its
    #    editable columns. ──
    for nac_u in (NACController(verify_pubkey=_PUB), NACController(verify_pubkey=None)):
        nac_u._ingest(_PROFILES, [{"id": 30, "name": "inv-unsigned", "priority": 5,
                                   "match": {}, "profile_id": 1,
                                   "signed_terms": None, "signature": None}])
        armed = "armed" if nac_u._verify_pubkey else "unarmed"
        if nac_u._tamper is not True:
            fails.append(f"unsigned ({armed}): an approved rule with no signature must trip tamper")
        if nac_u._verdicts.get(30) != {"integrity": "failed", "reason": "unsigned"}:
            fails.append(f"unsigned ({armed}): verdict should be failed/unsigned, got "
                         f"{nac_u._verdicts.get(30)!r}")
        if permits(nac_u, {"posture": "healthy"}) is not False:
            fails.append(f"unsigned ({armed}): a catch-all allow with no signature must not PERMIT")

    # ── column priority swapped: enforcement follows the SIGNED priority ──
    # Signed: quarantine-all at 10 ahead of allow-all at 20. Then the editable columns are
    # swapped so the allow claims 1 and the quarantine 99. First match must still be the
    # quarantine, and nothing is flagged: the signed terms are intact.
    quar = _signed_row(40, "inv-shut", 10, {}, "quarantine", [])
    allow = _signed_row(41, "inv-open", 20, {}, "allow", [{"direction": "inbound", "lane": "mcp"}])
    quar["priority"], allow["priority"] = 99, 1
    nac_p = NACController(verify_pubkey=_PUB)
    nac_p._ingest(_PROFILES, [allow, quar])   # handed over in the swapped column order
    if nac_p._tamper:
        fails.append("priority: a column-only swap must not flag tamper (signed terms intact)")
    if [r["id"] for r in nac_p._rules] != [40, 41]:
        fails.append(f"priority: rules must be ordered by SIGNED priority, got "
                     f"{[r['id'] for r in nac_p._rules]}")
    if permits(nac_p, {"posture": "healthy"}) is not False:
        fails.append("priority: swapping the priority COLUMNS reordered enforcement (allow won)")

    # ── unarmed by design (genesis / no entity id): signed snapshots load + enforce ──
    nac_b = NACController(verify_pubkey=None)
    nac_b._ingest(_PROFILES, armed_policy)
    if nac_b._tamper:
        fails.append("unarmed: should not flag tamper without a pinned key (engine must work pre-pin)")
    if permits(nac_b, {"posture": "degraded"}) is not False:
        fails.append("unarmed: degraded posture should still QUARANTINE")

    return fails


# ── the async half: the key retry and the lock notice, against a stand-in pool ──

class _Pool:
    """The two reads NACController._load makes. Rows are handed back as they are set."""
    def __init__(self, policy):
        self.policy = policy

    async def fetch(self, query, *args):
        if "FROM nac_profiles" in query:
            return list(_PROFILES)
        if "FROM nac_policy" in query:
            return list(self.policy)
        raise AssertionError(f"unexpected query: {query}")


def _engine(pool, eid, notify):
    """An engine as server.main builds one, with its notices sent to `notify`."""
    nac = NACController(pool=pool, entity_id=eid)
    nac._notify = notify
    return nac


def lock_tests() -> list[str]:
    from server.network import nac_stepup
    fails: list[str] = []
    key = {"readable": False}

    async def _key(pool, eid):
        if not key["readable"]:
            raise RuntimeError("signing key unavailable")
        return _PUB

    notices: list[dict] = []

    async def _notify(n):
        notices.append(n)
        return {"ok": True}

    def permits(nac, ep):
        prof, _why = nac.decide(ep)
        return nac.permits_inbound(prof, lane="mcp")

    healthy_allow = _signed_row(20, "inv-healthy", 20, {"posture": ["healthy"]}, "allow",
                                [{"direction": "inbound", "lane": "mcp"}])
    real_key = nac_stepup.entity_verify_pubkey
    nac_stepup.entity_verify_pubkey = _key
    try:
        async def run():
            # ── key unreadable at boot: no signed rule is enforced unchecked ──
            pool = _Pool([healthy_allow])
            nac = _engine(pool, 7, _notify)
            await nac.attach_pool(pool)
            if nac._verify_pubkey is not None:
                fails.append("key: an unreadable key must leave the engine unarmed")
            if nac._tamper is not True:
                fails.append("key: a signed rule the Host cannot check must trip the lock")
            if nac._verdicts.get(20) != {"integrity": "failed", "reason": "key_unreadable"}:
                fails.append(f"key: verdict should be failed/key_unreadable, got "
                             f"{nac._verdicts.get(20)!r}")
            if permits(nac, {"posture": "healthy"}) is not False:
                fails.append("key: a signed allow was enforced UNCHECKED while the key was unreadable")
            if len(notices) != 1 or "signing key" not in notices[0]["body"]:
                fails.append(f"key: one notice saying the signing key could not be read, got "
                             f"{[n['body'][:60] for n in notices]}")
            # ── the next reload re-arms once the key reads, and the lock lifts ──
            key["readable"] = True
            await nac._load(force=True)
            if nac._verify_pubkey != _PUB:
                fails.append("key: the reload did not re-arm once the key became readable")
            if nac._tamper or nac._verdicts.get(20) != {"integrity": "ok"}:
                fails.append(f"key: after re-arm the rule should verify ok, got tamper="
                             f"{nac._tamper} {nac._verdicts.get(20)!r}")
            if permits(nac, {"posture": "healthy"}) is not True:
                fails.append("key: after re-arm the healthy machine should be admitted again")

            # ── tamper: exactly one notice per lock, however many reloads ──
            notices.clear()
            bad = dict(healthy_allow)
            bad["signed_terms"] = dict(bad["signed_terms"], permits=[{"direction": "inbound",
                                                                      "lane": "any"}])
            pool.policy = [bad]
            for _ in range(4):
                await nac._load(force=True)
            if nac._tamper is not True:
                fails.append("notice: the tampered rule did not lock the gate")
            if len(notices) != 1:
                fails.append(f"notice: one lock must be told once, got {len(notices)} notices")
            elif not (notices[0]["message_kind"] == "system_notification"
                      and "refusing every machine you paired" in notices[0]["body"]
                      and "Network app" in notices[0]["body"]
                      and "no longer matches what you approved" in notices[0]["body"]):
                fails.append(f"notice: wrong wording or kind: {notices[0]!r}")
            first_id = notices[0]["message_id"] if notices else None
            # A restart in the same state names the same notice, so the inbox keeps one.
            again = _engine(pool, 7, _notify)
            await again.attach_pool(pool)
            if len(notices) != 2 or notices[-1]["message_id"] != first_id:
                fails.append("notice: a restart in the same lock must reuse the notice id "
                             "(the inbox drops the copy)")
            # A different breakage is a new lock and is told.
            unsigned = dict(healthy_allow, signed_terms=None, signature=None)
            pool.policy = [unsigned]
            await nac._load(force=True)
            await nac._load(force=True)
            if len(notices) != 3 or notices[-1]["message_id"] == first_id:
                fails.append(f"notice: a new breakage must be told once with a new id, got "
                             f"{len(notices)} notices")
            # Fixed, then broken again: told again.
            pool.policy = [healthy_allow]
            await nac._load(force=True)
            if nac._tamper:
                fails.append("notice: the restored rule should lift the lock")
            pool.policy = [bad]
            await nac._load(force=True)
            if len(notices) != 4:
                fails.append(f"notice: a lock after the gate recovered must be told again, got "
                             f"{len(notices)} notices")
            # A notice the inbox refuses is retried on the next reload, not marked told.
            refused: list[dict] = []

            async def _refuse(n):
                refused.append(n)
                return {"ok": False, "error": "pool not ready", "retryable": True}
            nac3 = _engine(pool, 7, _refuse)
            await nac3.attach_pool(pool)
            await nac3._load(force=True)
            if len(refused) != 2:
                fails.append(f"notice: a refused notice must be retried, got {len(refused)} tries")
            # Genesis stays unarmed by design: no key read, signed rows enforced.
            key["readable"] = False
            g = _engine(_Pool([healthy_allow]), 0, _notify)
            await g.attach_pool(g.pool)
            if g._tamper or permits(g, {"posture": "healthy"}) is not True:
                fails.append("genesis: entity 0 must stay unarmed and enforce its signed rows")
        asyncio.run(run())
    finally:
        nac_stepup.entity_verify_pubkey = real_key
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
    fails += lock_tests()
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
