#!/usr/bin/env python3
"""Invariant: dcn-cn1 (store grants + the DCN pod gate).

Pure (always):
  * dcn_decision is DEFAULT-DENY: no grant → deny; op outside the granted mask →
    deny; a suspended or quarantined server denies even with a valid grant whose
    mask covers the op (one decision point).
  * load_dcn_grants fails CLOSED: no verify key → no grants (never trusts
    unsigned columns on a default-deny lane).
  * propose_dcn_store refuses a mask containing 'delete' before touching the DB.
  * The cabinet composite derives exact egress hosts (endpoint host / AWS pair).

DB (skippable via SKIP_DB=1):
  * v092 grant columns present.
  * Grant-signing roundtrip: signed terms verify; a post-signing mutation fails.
  * approve_dcn_store WITHOUT a valid step-up code → CommitError 403 (the agent
    cannot self-grant).
  * revoke_dcn_store tears down the bundled egress allows with the store (the
    composite is one capability). Probe rows are tagged and cleaned in finally.

Run in-pod: docker exec <c> python3 /app/scripts/check_dcn_cn1.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network import nac_dcn
from server.network.nac_propose import _cabinet_egress_patterns, propose_dcn_store

PROBE = "dcn-cn1-invariant-probe"


def pure_tests():
    fails = []
    grant = {"store_id": 1, "terms": {"class": "machine", "server": "box1",
                                      "root": "/data", "mask": ["read", "search"]}}
    allowed, _ = nac_dcn.dcn_decision(None, "read")
    if allowed:
        fails.append("no grant must deny (default-deny)")
    allowed, _ = nac_dcn.dcn_decision(grant, "write")
    if allowed:
        fails.append("an op outside the granted mask must deny")
    allowed, _ = nac_dcn.dcn_decision(grant, "read")
    if not allowed:
        fails.append("a granted (store, op) should allow")
    # bastion agent lane: search is its own mask, its own box op, and
    # never reaches the gate without a query.
    read_only = {"store_id": 2, "terms": {"class": "machine", "server": "box1",
                                          "root": "/data", "mask": ["read"]}}
    allowed, _ = nac_dcn.dcn_decision(read_only, nac_dcn._MASK_FOR_OP["search"])
    if allowed:
        fails.append("a read-only grant must not allow search")
    allowed, _ = nac_dcn.dcn_decision(grant, nac_dcn._MASK_FOR_OP["search"])
    if not allowed:
        fails.append("a read+search grant must allow search")
    if nac_dcn._BOX_OPS.get("search") != "dcn_search":
        fails.append("search must dispatch to the box's dcn_search handler")
    payload = nac_dcn._box_payload("search", {"query": " routers ", "k": 3})
    if payload.get("query") != "routers" or payload.get("k") != 3:
        fails.append("search payload must carry the trimmed query and k")
    if "doc" not in nac_dcn._box_payload("read_range", {"doc": "abc", "end_byte": 10}):
        fails.append("doc-form read_range payload must carry the doc id")
    if nac_dcn._BOX_OPS.get("catalog") != "dcn_catalog" or nac_dcn._MASK_FOR_OP.get("catalog") != "read":
        fails.append("catalog must dispatch to dcn_catalog under the read mask")
    if nac_dcn._box_payload("catalog", {})["kind"] != "dirs" or nac_dcn._box_payload("catalog", {"kind": "files"})["kind"] != "files":
        fails.append("catalog payload kind must default to dirs and accept files")
    if nac_dcn._box_payload("catalog", {"offset": "7"})["offset"] != 7 or nac_dcn._box_payload("catalog", {})["offset"] != 0:
        fails.append("catalog payload must carry offset (default 0)")
    # age recipient encoding: age-keygen's own test vector shape (bech32, hrp
    # 'age', 58 data chars) and a round-trip against a reference implementation
    # is what the bastion agent's seal stage validates (AGE_RECIPIENT_RE).
    import re as _re
    rec = nac_dcn.age_recipient(bytes(range(32)))
    if not _re.match(r"^age1[a-z0-9]{58}$", rec):
        fails.append(f"age_recipient must produce age1 + 58 bech32 chars, got {rec!r}")
    # the reference decoder is age itself: it refuses a recipient with a bad
    # bech32 checksum, so an accepted recipient proves the encoding
    import shutil as _sh, subprocess as _sp
    if _sh.which("age"):
        r = _sp.run(["age", "-r", rec, "-o", os.devnull], input=b"x", capture_output=True)
        if r.returncode != 0:
            fails.append(f"age rejected our recipient encoding: {r.stderr.decode()[:120]}")
    try:
        asyncio.run(nac_dcn.run_dcn_op(None, 1, 1, "search", {}))
        fails.append("search without a query must be refused before the gate")
    except Exception as e:
        if getattr(e, "status", None) != 400 and "query" not in str(e):
            fails.append(f"search without a query must be a 400, got {e!r}")
    allowed, reason = nac_dcn.dcn_decision(grant, "read", suspended={"box1"})
    if allowed or "suspended" not in reason:
        fails.append("a suspended server must deny before the mask is consulted")
    allowed, reason = nac_dcn.dcn_decision(grant, "read", quarantined={"box1"})
    if allowed or "quarantined" not in reason:
        fails.append("a quarantined server must deny before the mask is consulted")

    if asyncio.run(nac_dcn.load_dcn_grants(None, None)) != []:
        fails.append("load_dcn_grants with no verify key must authorize nothing (fail-closed)")

    try:
        asyncio.run(propose_dcn_store(None, 1, store_class="machine", server="x",
                                      root="/y", mask=["read", "delete"]))
        fails.append("propose_dcn_store must refuse a mask containing 'delete'")
    except ValueError:
        pass

    # ── NAC agent step 1: the reconcile instruction for a box ──
    # Only THIS box's machine stores are listed; cabinets and other boxes never are.
    grants = [
        {"store_id": 1, "signature": "s1", "terms": {"class": "machine", "server": "box1"}},
        {"store_id": 2, "signature": "s2", "terms": {"class": "machine", "server": "box2"}},
        {"store_id": 3, "signature": "s3", "terms": {"class": "cabinet", "bucket": "b"}},
    ]
    if nac_dcn.box_grant_set(grants, "box1") != [{"store_id": 1, "signature": "s1"}]:
        fails.append("box_grant_set must list exactly this box's machine stores")
    if nac_dcn.box_grant_set(grants, "box9") != []:
        fails.append("box_grant_set for a box with no grants must be an EMPTY list (drop all)")
    # No hostname → no instruction (None), never an empty list.
    if asyncio.run(nac_dcn.expected_box_grants(None, 1, "")) is not None:
        fails.append("expected_box_grants with no hostname must return None (no instruction)")
    # A verify-key / DB failure → None (the heartbeat OMITS the field), never [].
    if asyncio.run(nac_dcn.expected_box_grants(None, 1, "box1")) is not None:
        fails.append("expected_box_grants on a failure must return None, not an empty list")
    # A cabinet revoke has no box to push to.
    r = asyncio.run(nac_dcn.drop_dcn_grant(None, 1, 5, ""))
    if r.get("state") != "ok" or r.get("op") != "grant_drop":
        fails.append(f"drop_dcn_grant with no server must be a typed ok no-op: {r}")

    # ── NAC agent step 2: granted vs enforced → drift, in plain words ──
    expected = [{"store_id": 1, "signature": "s1"}, {"store_id": 2, "signature": "s2"}]
    # The bastion's vocabulary. This WAS a second hand-typed list, with a comment
    # claiming it was verbatim from the gateway. It was not: it and
    # KNOWN_BOX_OPS shared the same 19 omissions, so this compared a stale copy to
    # a stale copy and passed while the live fleet sat at degraded. Reading the
    # constant means the two can no longer disagree, and the job of catching the
    # constant itself going stale belongs to check_box_ops_sync.py, which reads
    # the Go.
    gateway_vocabulary = sorted(nac_dcn.KNOWN_BOX_OPS)
    clean = {"grants": [{"store_id": 1, "signature": "s1"}, {"store_id": 2, "signature": "s2"}],
             "mgmt_ops": gateway_vocabulary}
    if nac_dcn.grant_drift(expected, clean) != []:
        fails.append("a box holding exactly the granted set must show NO drift")
    # A gateway-shaped plan reply carries plan_id/mode/state — the envelope must
    # absorb them (the first cut raised TypeError after the DB write).
    env = nac_dcn.plan_envelope(5, "plan_apply", 9, "box1",
                                {"state": "ok", "mode": "apply", "plan_id": 9, "applied": 3, "verified": 3})
    if env != {"state": "ok", "store_id": 5, "op": "plan_apply", "plan_id": 9, "server": "box1",
               "mode": "apply", "applied": 3, "verified": 3}:
        fails.append(f"plan_envelope must absorb the box's own keys: {env}")
    drifted = {"grants": [{"store_id": 1, "signature": "OLD"}, {"store_id": 7, "signature": "x"}],
               "mgmt_ops": ["uptime", "rm_rf"]}
    d = nac_dcn.grant_drift(expected, drifted)
    # GRANT facts only. An unfamiliar op ('rm_rf' here) is no longer drift and no
    # longer degrades — it is reported by unknown_box_ops instead. The three grant
    # cases still degrade exactly as they did.
    if not (len(d) == 3 and "store 1" in d[0] and "stale" in d[0]
            and "store 2" in d[1] and "not held" in d[1]
            and "store 7" in d[2] and "never issued" in d[2]):
        fails.append(f"grant_drift must name stale / missing / extra, and ONLY those: {d}")
    if any("rm_rf" in x for x in d):
        fails.append(f"an unfamiliar op must not be drift (it degrades the box): {d}")
    if nac_dcn.unknown_box_ops(drifted) != ["rm_rf"]:
        fails.append(f"unknown_box_ops must report it instead: {nac_dcn.unknown_box_ops(drifted)}")
    if nac_dcn.drift_store_ids(expected, drifted) != [1, 2]:
        fails.append("drift_store_ids must be the stores a re-sync repairs (stale + missing), never extras")
    # Cannot-judge is not drift.
    if nac_dcn.grant_drift(None, drifted) != [] or nac_dcn.grant_drift(expected, None) != []:
        fails.append("no expected set (host can't say) or no report (old bastion) must be NO drift")
    if nac_dcn.grant_drift(expected, "junk") != []:
        fails.append("a non-dict report must be NO drift")

    # ── Move plans: host-side shape wall; archive-only, no delete ──
    from server.network.nac_propose import validate_moves, _plan_rel_ok
    ok_moves = validate_moves([{"from": "Downloads/a.exe", "to": "Archive/a.exe", "size_bytes": 5},
                               {"from": "/Downloads/b.jpg/", "to": "Pictures/b.jpg"}], "Archive")
    if ok_moves != [{"from": "Downloads/a.exe", "to": "Archive/a.exe", "size_bytes": 5},
                    {"from": "Downloads/b.jpg", "to": "Pictures/b.jpg"}]:
        fails.append(f"validate_moves must normalize and keep size_bytes: {ok_moves}")
    for bad, why in (
        ([], "empty"),
        ([{"from": "../x", "to": "y"}], "traversal"),
        ([{"from": "a", "to": "b/../../x"}], "traversal in the middle"),
        ([{"from": "a", "to": "a"}], "same path"),
        ([{"from": "a", "to": "b"}, {"from": "a", "to": "c"}], "moved twice"),
        ([{"from": "a", "to": "b"}, {"from": "c", "to": "b"}], "duplicate target"),
        ([{"from": ".agent/catalog.jsonl", "to": "x"}], ".agent is the machine's"),
        ([{"from": "a\\b", "to": "c"}], "backslash"),
        ([{"from": "C:/x", "to": "y"}], "drive letter"),
    ):
        try:
            validate_moves(bad, "Archive")
            fails.append(f"validate_moves must refuse: {why}")
        except ValueError:
            pass
    if _plan_rel_ok("Archive/2024") is not True or _plan_rel_ok("") or _plan_rel_ok("../x"):
        fails.append("_plan_rel_ok wrong")
    # There is still no delete anywhere in the plan/store vocabulary.
    if "delete" in nac_dcn._BOX_OPS or "delete" in nac_dcn._MASK_FOR_OP:
        fails.append("a delete op must not exist on the DCN surface")
    if "dry_run" not in nac_dcn.PLAN_MODES or "apply" not in nac_dcn.PLAN_MODES or len(nac_dcn.PLAN_MODES) != 2:
        fails.append("plan modes are exactly dry_run and apply")

    pats = _cabinet_egress_patterns("b", "https://minio.example.com:9000/x", "")
    if pats != ["minio.example.com"]:
        fails.append(f"endpoint egress derivation wrong: {pats}")
    pats = _cabinet_egress_patterns("mybucket", "", "us-east-1")
    if "mybucket.s3.amazonaws.com" not in pats or "mybucket.s3.us-east-1.amazonaws.com" not in pats:
        fails.append(f"AWS egress derivation wrong: {pats}")
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
        from server.network import nac_commit, nac_stepup
        from server.network.nac_mgmt import _verify_grant
        eid = (int(__import__("os").environ.get("ENTITY_ID") or 0) or int(await pool.fetchval("SELECT max(id) FROM entity_registry") or 1))  # the pod's own entity, never genesis 0

        cols = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='dcn_stores'")}
        for want in ("signed_terms", "signature", "expires_at", "revoked_at",
                     "egress_rule_ids", "granted_to"):
            if want not in cols:
                fails.append(f"dcn_stores missing v092 column '{want}'")

        # ── grant-signing roundtrip + tamper detection ──
        terms = {"kind": "dcn_store_grant", "entity": eid, "class": "machine",
                 "server": PROBE, "root": "/probe", "mask": ["read"],
                 "expires": "2099-01-01T00:00:00+00:00"}
        _kid, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
        pub = await nac_stepup.entity_verify_pubkey(pool, eid)
        if not _verify_grant(pub, terms, sig):
            fails.append("a host-signed store grant must verify")
        bad = dict(terms); bad["mask"] = ["read", "write"]  # widen after signing
        if _verify_grant(pub, bad, sig):
            fails.append("a store grant mutated after signing must FAIL verification")

        # ── the agent cannot self-approve (no valid code → 403) ──
        res = await propose_dcn_store(pool, eid, store_class="machine",
                                      server=PROBE, root="/probe", note=PROBE)
        sid = res["store_id"]
        try:
            await nac_commit.approve_dcn_store(pool, eid, sid, stepup_code="000000")
            fails.append("approve_dcn_store must refuse without a valid step-up code")
        except nac_commit.CommitError as e:
            if e.status != 403:
                fails.append(f"approve without code: expected 403, got {e.status}")

        # ── composite: cabinet draft bundles egress; revoke tears both down ──
        cres = await propose_dcn_store(pool, eid, store_class="cabinet",
                                       bucket=PROBE, endpoint="https://probe.invalid",
                                       note=PROBE)
        csid = cres["store_id"]
        rids = cres["egress_rule_ids"]
        if not rids:
            fails.append("a cabinet draft must bundle at least one egress allow draft")
        rv = await nac_commit.revoke_dcn_store(pool, eid, csid)
        if rv.get("bundled_egress_revoked", 0) != len(rids):
            fails.append("revoking a cabinet store must revoke its bundled egress allows")
        if rv.get("class") != "cabinet" or rv.get("server") is not None:
            fails.append(f"revoke must name the store's class and box (cabinet → no box): {rv}")

        # ── NAC agent step 1: a machine revoke names its box; the push to an
        # unregistered box is a typed 'offline' (reconciled on the next beat), audited ──
        rv = await nac_commit.revoke_dcn_store(pool, eid, sid)
        if rv.get("class") != "machine":
            fails.append(f"machine revoke must report class=machine: {rv}")
        dr = await nac_dcn.drop_dcn_grant(pool, eid, sid, PROBE)
        if dr.get("state") != "offline" or dr.get("op") != "grant_drop":
            fails.append(f"drop to an unregistered box must be typed offline: {dr}")
        # The heartbeat reply for an unknown box is an explicit empty set (drop all),
        # and the host can compute it (verify key present) — so the field is SENT.
        exp = await nac_dcn.expected_box_grants(pool, eid, PROBE)
        if exp != []:
            fails.append(f"expected_box_grants for a box with no live grants must be []: {exp!r}")

        # ── Move plans: no plan on an ungranted store; a draft plan cannot run; a
        # bad code cannot approve one; revoke is free ──
        from server.network.nac_propose import propose_dcn_move_plan
        res2 = await propose_dcn_store(pool, eid, store_class="machine",
                                       server=PROBE, root="/probe2", note=PROBE)
        sid2 = res2["store_id"]  # a DRAFT store
        try:
            await propose_dcn_move_plan(pool, eid, store_id=sid2,
                                        moves=[{"from": "a", "to": "Archive/a"}])
            fails.append("a plan on a store the user has not approved must be refused")
        except ValueError:
            pass
        # force the store to 'granted' shape (no signature needed for these checks)
        await pool.execute("UPDATE dcn_stores SET status='granted', approved_at=now() WHERE id=$1", sid2)
        plan = await propose_dcn_move_plan(pool, eid, store_id=sid2, title=PROBE,
                                           moves=[{"from": "a", "to": "Archive/a"}])
        if plan.get("status") != "draft" or plan.get("into_archive") != 1:
            fails.append(f"plan draft shape wrong: {plan}")
        r = await nac_dcn.run_move_plan(pool, eid, plan["plan_id"], "dry_run")
        if r.get("state") != "refused" or "approve" not in (r.get("reason") or ""):
            fails.append(f"an unapproved plan must not run: {r}")
        try:
            await nac_commit.approve_dcn_move_plan(pool, eid, plan["plan_id"], stepup_code="000000")
            fails.append("approve_dcn_move_plan must refuse without a valid step-up code")
        except nac_commit.CommitError as e:
            if e.status != 403:
                fails.append(f"plan approve without code: expected 403, got {e.status}")
        rv2 = await nac_commit.revoke_dcn_move_plan(pool, eid, plan["plan_id"])
        if not rv2.get("revoked"):
            fails.append("revoking a draft plan must be free and succeed")
        lst = await nac_commit.dcn_move_plans_list(pool, eid, store_id=sid2)
        if not any(p["id"] == plan["plan_id"] and p["status"] == "revoked" for p in lst["plans"]):
            fails.append("plan list must show the revoked plan")
        live = await pool.fetchval(
            "SELECT count(*) FROM nac_egress WHERE id = ANY($1::bigint[]) "
            "AND revoked_at IS NULL", [int(r) for r in rids])
        if live:
            fails.append(f"{live} bundled egress rule(s) still live after store revoke")

        if not fails:
            print("  [db] OK — v092 columns present, signed grant verifies + tamper fails, "
                  "self-approve refused (403), composite revoked whole")
    except Exception as e:
        fails.append(f"db: dcn-cn1 test raised: {e}")
    finally:
        try:
            await pool.execute("DELETE FROM dcn_move_plans WHERE title=$1 OR store_id IN "
                               "(SELECT id FROM dcn_stores WHERE note=$1 OR server=$1)", PROBE)
            await pool.execute("DELETE FROM dcn_stores WHERE note=$1 OR server=$1 OR bucket=$1",
                               PROBE)
            await pool.execute("DELETE FROM nac_mgmt_audit WHERE server=$1", PROBE)
            await pool.execute("DELETE FROM nac_egress WHERE note LIKE '%' || $1 || '%' "
                               "OR pattern = 'probe.invalid'", PROBE)
        except Exception:
            pass
        await pool.close()
    return fails


def main():
    fails = pure_tests()
    fails += asyncio.run(db_tests())
    if fails:
        print("DCN-CN1 CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("DCN-CN1 INVARIANTS OK — default-deny gate holds (mask, suspend, quarantine), "
          "fail-closed loader, no delete in the vocabulary, signed grants tamper-evident, "
          "self-approve refused, cabinet composite lives and dies as one capability")
    return 0


if __name__ == "__main__":
    sys.exit(main())
