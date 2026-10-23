#!/usr/bin/env python3
"""Gate: service-slot REACH GRANTS (bastion 0.36.0 client-go/service_reach.go) — the Host half.

What this pins, pure Python (fake DB, real Ed25519 signatures, no server):

  1. The purpose the person approves is the box's string, byte for byte
     (client-go TestReachPurposeMatchesTheHost pins the same one), and the details
     carry exactly the fields the box rebuilds it from.
  2. A draft's SUBJECT is the name the box reported for this Host's pairing
     (enforcing.pairing_host) — never guessed; no report, no draft.
  3. Approval: on-machine approval rides in the signed terms; otherwise the Host's
     step-up code is required; a pending machine approval is stored and returned.
     The signed terms carry kind/server/slot/subject/expiry/ttl.
  4. The verified set checks signature AND kind AND server; the heartbeat reply's
     service_grants and drift use it; the push sends the exact canonical bytes.
  5. Wiring: heartbeat reply + truncation keep service_grants; routes exist with
     the Network app's permissions; the migration's constraints; the box ops are
     parked as pending.

Run: python3 scripts/test_service_reach.py
"""
import asyncio
import base64
import datetime as dt
import json
import os
import sys

ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from server.network import nac_stepup, service_reach  # noqa: E402

# ── 1. purpose + details ─────────────────────────────────────────────────────
terms = {"server": "box1", "slot": "forge", "subject": "h.example", "ttl_days": 30}
check("purpose is the box's string",
      service_reach.purpose_for_reach_terms(terms) == "service_reach_grant|box1|forge|h.example|ttl:30",
      service_reach.purpose_for_reach_terms(terms))
d = service_reach.reach_request_details(terms)
check("details carry the kind and exactly the purpose fields",
      d["kind"] == "service_reach_grant" and (d["server"], d["slot"], d["subject"], d["ttl_days"])
      == ("box1", "forge", "h.example", 30))
try:
    service_reach.purpose_for_reach_terms({**terms, "subject": "a|b"})
    check("a reserved character is refused", False)
except ValueError:
    check("a reserved character is refused", True)

# ── 1b. mirrors (bastion 0.38.0): the purpose binds them, pinned on both sides ──
mt = {**terms, "mirrors": [{"url": "https://github.com/psf/requests.git", "repo": "mirrors/requests"},
                           {"url": "https://github.com/pallets/flask.git", "repo": "mirrors/flask"}]}
check("with mirrors: the purpose is the box's string (sorted, appended)",
      service_reach.purpose_for_reach_terms(mt) ==
      "service_reach_grant|box1|forge|h.example|ttl:30|mirrors:"
      "https://github.com/pallets/flask.git>mirrors/flask,https://github.com/psf/requests.git>mirrors/requests",
      service_reach.purpose_for_reach_terms(mt))
check("the details carry the mirror list the person reads",
      service_reach.reach_request_details(mt).get("mirrors") == mt["mirrors"]
      and "mirrors" not in service_reach.reach_request_details(terms))
for label, bad in {"http": {"url": "http://github.com/a/b", "repo": "m/b"},
                   "userinfo": {"url": "https://u:p@github.com/a/b", "repo": "m/b"},
                   "query": {"url": "https://github.com/a/b?x=1", "repo": "m/b"},
                   "no owner": {"url": "https://github.com/a/b", "repo": "flask"},
                   "comma": {"url": "https://github.com/a/b,c", "repo": "m/b"},
                   "file": {"url": "file:///srv/x", "repo": "m/b"}}.items():
    check(f"a mirror source is refused: {label}", service_reach.mirror_source_refusal(bad) is not None)
for label, bad in {"empty userinfo": {"url": "https://@github.com/a/b", "repo": "m/b"},
                   "space": {"url": "https://github.com/a/b c", "repo": "m/b"},
                   "unicode": {"url": "https://g\u00edthub.com/a/b", "repo": "m/b"},
                   "rtl override": {"url": "https://github.com/a/\u202eb", "repo": "m/b"},
                   "tab in repo": {"url": "https://github.com/a/b", "repo": "m/\tb"}}.items():
    check(f"a mirror source is refused like the box does: {label}",
          service_reach.mirror_source_refusal(bad) is not None)
# The SAME vectors the box runs (client-go testdata/mirror_url_vectors.json, copied):
# both sides accept exactly the same mirror URLs.
_vec = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mirror_url_vectors.json")))
_bad = [v["url"] for v in _vec
        if (service_reach.mirror_source_refusal({"url": v["url"], "repo": "m/b"}) is None) != v["ok"]]
check(f"the box's {len(_vec)} mirror URL vectors give the same verdicts here", not _bad, repr(_bad))
dd = service_reach.normalize_mirrors([{"url": "https://github.com/a/b.git", "repo": "m/b"}] * 3)
check("mirrors are de-duplicated", len(dd) == 1)
for label, lst in {"same repo": [{"url": "https://github.com/a/b.git", "repo": "m/b"},
                                 {"url": "https://github.com/x/b.git", "repo": "m/b"}],
                   "same repo, other case": [{"url": "https://github.com/a/b.git", "repo": "m/b"},
                                             {"url": "https://github.com/x/b.git", "repo": "M/B"}]}.items():
    try:
        service_reach.normalize_mirrors(lst)
        check(f"two sources into one repo are refused ({label})", False)
    except service_reach.ReachError as e:
        check(f"two sources into one repo are refused ({label})", e.status == 400)
check("the seen-list fingerprint ignores order",
      service_reach.mirrors_digest(mt["mirrors"]) == service_reach.mirrors_digest(list(reversed(mt["mirrors"])))
      and service_reach.mirrors_digest(mt["mirrors"]) != service_reach.mirrors_digest(mt["mirrors"][:1]))
check("the mirror wait grows with the list and covers a fetch plus a push each",
      service_reach.mirror_wait_s(3) - service_reach.mirror_wait_s(2) >= 2 * 600
      and service_reach.mirror_wait_s(0) == service_reach.mirror_wait_s(1))

# ── fakes ────────────────────────────────────────────────────────────────────
priv = Ed25519PrivateKey.generate()
pub_raw = priv.public_key().public_bytes_raw()


class Conn:
    def __init__(self, db):
        self.db = db

    def transaction(self):
        class T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False
        return T()

    async def fetchrow(self, q, *a):
        return await self.db.fetchrow(q, *a)

    async def execute(self, q, *a):
        return await self.db.execute(q, *a)


class DB:
    def __init__(self, pairing_host="h.example"):
        self.pairing_host = pairing_host
        self.rows = {}
        self.next_id = 1

    def acquire(self):
        db = self

        class A:
            async def __aenter__(s):
                return Conn(db)

            async def __aexit__(s, *a):
                return False
        return A()

    async def fetchval(self, q, *a):
        if "enforcing FROM network_bastion_heartbeat" in q:
            return None if self.pairing_host is None else json.dumps({"pairing_host": self.pairing_host})
        if "INSERT INTO service_reach_grants" in q:
            gid = self.next_id
            self.next_id += 1
            self.rows[gid] = {"id": gid, "entity_id": a[0], "server": a[1], "slot": a[2], "subject": a[3],
                              "note": a[4], "proposed_by": a[5], "mirrors": a[6] if len(a) > 6 else "[]",
                              "status": "draft", "approved_at": None,
                              "revoked_at": None, "box_request": None, "signed_terms": None,
                              "signature": None, "expires_at": None}
            return gid
        return None

    async def fetchrow(self, q, *a):
        if "FROM service_reach_grants WHERE entity_id=$1 AND server=$2" in q:
            for r in self.rows.values():
                if (r["entity_id"], r["server"], r["slot"], r["subject"]) == a and r["revoked_at"] is None:
                    return r
            return None
        if "FROM service_reach_grants WHERE id=$1" in q:
            r = self.rows.get(a[0])
            return r if r and r["entity_id"] == a[1] and r["revoked_at"] is None else None
        return None

    async def fetch(self, q, *a):
        if "FROM service_reach_grants WHERE entity_id=$1 ORDER BY id DESC" in q:
            keys = ("id", "server", "slot", "subject", "status", "note", "proposed_by", "mirrors",
                    "approved_at", "expires_at", "revoked_at", "created_at")
            return [{**{k: r.get(k) for k in keys}, "pending_on_box": r["box_request"] is not None}
                    for r in self.rows.values() if r["entity_id"] == a[0]]
        if "FROM service_reach_grants" in q and "approved_at IS NOT NULL" in q:
            return [{"id": r["id"], "signed_terms": r["signed_terms"], "signature": r["signature"]}
                    for r in self.rows.values() if r["approved_at"] and not r["revoked_at"]]
        return []

    async def execute(self, q, *a):
        if "SET approved_at=now()" in q:
            r = self.rows[a[0]]
            r.update(approved_at=dt.datetime.now(dt.timezone.utc), approved_by=a[1], signed_terms=a[3],
                     signature=a[4], expires_at=a[5], status="granted", box_request=None)
        elif "SET box_request=$2" in q:
            self.rows[a[0]]["box_request"] = a[1]
        elif "SET mirrors=$2::jsonb, box_request=NULL" in q:
            r = self.rows[a[0]]
            if r["approved_at"] is not None or r["revoked_at"] is not None:
                return "UPDATE 0"
            r.update(mirrors=a[1], box_request=None)
            return "UPDATE 1"
        elif "SET box_request=NULL" in q:
            self.rows[a[0]]["box_request"] = None


async def _sign(pool, eid, t):
    return "entity_sign", base64.b64encode(priv.sign(nac_stepup._canonical(t))).decode()


async def _pub(pool, eid):
    return pub_raw


box_state = {"state": "not_enrolled"}
stepup_ok = {"ok": True}


async def _box_conf(pool, eid, **k):
    box_state["seen"] = k
    return dict(box_state)


async def _verify(pool, code):
    return stepup_ok["ok"] and code == "123456"


nac_stepup.sign_policy_terms = _sign
nac_stepup.entity_verify_pubkey = _pub
nac_stepup.box_confirmation = _box_conf
nac_stepup.verify_stepup = _verify


async def main():
    # ── 2. the subject is what the box reported ─────────────────────────────
    db = DB(pairing_host=None)
    try:
        await service_reach.propose_reach(db, 9003, "box1", "forge")
        check("no pairing_host report → no draft", False)
    except service_reach.ReachError as e:
        check("no pairing_host report → no draft", e.status == 409 and "0.36.0" in e.detail)
    db = DB(pairing_host="h.example")
    p = await service_reach.propose_reach(db, 9003, "box1", "forge", note="n")
    check("the draft's subject is the box-reported pairing name",
          p["subject"] == "h.example" and p["status"] == "draft")
    again = await service_reach.propose_reach(db, 9003, "box1", "forge")
    check("drafting twice returns the same live grant", again["existing"] and again["grant_id"] == p["grant_id"])
    try:
        await service_reach.propose_reach(db, 9003, "box1", "../x")
        check("a bad slot name is refused", False)
    except service_reach.ReachError as e:
        check("a bad slot name is refused", e.status == 400)

    # mirrors on a draft, and signed on approval
    dbm = DB()
    pm = await service_reach.propose_reach(dbm, 9003, "box1", "wiki", mirrors=mt["mirrors"])
    stored = json.loads(dbm.rows[pm["grant_id"]]["mirrors"])
    check("a draft stores its mirror list", len(stored) == 2 and stored[0]["repo"] in ("mirrors/requests", "mirrors/flask"))
    try:
        await service_reach.propose_reach(dbm, 9003, "box1", "wiki2", mirrors=[{"url": "http://x/y", "repo": "m/y"}])
        check("a bad mirror source refuses the draft", False)
    except service_reach.ReachError as e:
        check("a bad mirror source refuses the draft", e.status == 400)
    # The draft's list: None keeps it, a new list replaces it (and drops any ask filed
    # on the machine for the old one), [] clears it.
    dbm.rows[pm["grant_id"]]["box_request"] = json.dumps({"request": "r-old"})
    keep = await service_reach.propose_reach(dbm, 9003, "box1", "wiki")
    check("re-drafting without mirrors keeps the draft's list and says what it holds",
          len(json.loads(dbm.rows[pm["grant_id"]]["mirrors"])) == 2 and len(keep["mirrors"]) == 2
          and dbm.rows[pm["grant_id"]]["box_request"] is not None)
    one = await service_reach.propose_reach(dbm, 9003, "box1", "wiki", mirrors=mt["mirrors"][:1])
    check("a new list replaces a draft's and drops the ask filed for the old list",
          len(json.loads(dbm.rows[pm["grant_id"]]["mirrors"])) == 1 and len(one["mirrors"]) == 1
          and dbm.rows[pm["grant_id"]]["box_request"] is None)
    await service_reach.propose_reach(dbm, 9003, "box1", "wiki", mirrors=[])
    check("an explicit empty list clears a draft's mirrors", json.loads(dbm.rows[pm["grant_id"]]["mirrors"]) == [])
    await service_reach.propose_reach(dbm, 9003, "box1", "wiki", mirrors=mt["mirrors"])

    box_state.clear()
    box_state.update({"state": "not_enrolled"})
    seen = service_reach.mirrors_digest(mt["mirrors"])
    for label, dg in {"no fingerprint": None, "a stale fingerprint": service_reach.mirrors_digest(mt["mirrors"][:1])}.items():
        try:
            await service_reach.approve_reach(dbm, 9003, pm["grant_id"], stepup_code="123456", ttl_days=30,
                                              seen_mirrors_digest=dg)
            check(f"approving with the Host's code needs the list the person saw ({label})", False)
        except service_reach.ReachError as e:
            check(f"approving with the Host's code needs the list the person saw ({label})",
                  e.status == 409 and dbm.rows[pm["grant_id"]]["approved_at"] is None)
    await service_reach.approve_reach(dbm, 9003, pm["grant_id"], stepup_code="123456", ttl_days=30,
                                      seen_mirrors_digest=seen)
    stm = json.loads(dbm.rows[pm["grant_id"]]["signed_terms"])
    check("approval signs the mirror list into the terms", len(stm.get("mirrors") or []) == 2)
    late = await service_reach.propose_reach(dbm, 9003, "box1", "wiki", mirrors=mt["mirrors"][:1])
    check("an approved grant is not edited, and the reply says so instead of echoing the new list",
          len(late["mirrors"]) == 2 and "mirrors_unchanged" in late
          and len(json.loads(dbm.rows[pm["grant_id"]]["signed_terms"])["mirrors"]) == 2)
    # An approval that commits between the draft check and the write: the stale read
    # still says "draft", and the write must not land on the approved row.
    dr = DB()
    pr = await service_reach.propose_reach(dr, 3, "box1", "race", mirrors=mt["mirrors"])
    dr.rows[pr["grant_id"]]["approved_at"] = dt.datetime.now(dt.timezone.utc)  # status still "draft"
    raced = await service_reach.propose_reach(dr, 3, "box1", "race", mirrors=mt["mirrors"][:1])
    check("a re-draft racing an approval leaves the approved row's list alone and says so",
          len(json.loads(dr.rows[pr["grant_id"]]["mirrors"])) == 2 and "mirrors_unchanged" in raced
          and len(raced["mirrors"]) == 2)
    check("a sync's wait covers one queued behind another (twice the list's work)",
          service_reach.mirror_wait_s(2) >= 60 + 2 * 2 * (20 + 60 + 1200 + 15))
    listed = {g["id"]: g for g in await service_reach.list_reach(dbm, 9003)}
    check("the list view carries the fingerprint the page sends back",
          listed[pm["grant_id"]]["mirrors_digest"] == seen)

    # ── 3. approval ─────────────────────────────────────────────────────────
    gid = p["grant_id"]
    try:
        await service_reach.approve_reach(db, 9003, gid, stepup_code="000000")
        check("a wrong step-up code refuses", False)
    except service_reach.ReachError as e:
        check("a wrong step-up code refuses", e.status == 403)
    box_state.clear()
    box_state.update({"state": "pending", "request": {"request": "r-1"}})
    pend = await service_reach.approve_reach(db, 9003, gid)
    check("a machine-held factor: pending, stored, no signature",
          pend["pending_on_box"] and db.rows[gid]["box_request"] and db.rows[gid]["signature"] is None)
    seen = box_state.get("seen") or {}
    check("the machine is asked with the box's purpose and details",
          seen.get("purpose") == "service_reach_grant|box1|forge|h.example|ttl:365"
          and (seen.get("details") or {}).get("kind") == "service_reach_grant")
    box_state.clear()
    box_state.update({"state": "approved", "approval": "APPROVAL-1"})
    ok = await service_reach.approve_reach(db, 9003, gid)
    st = json.loads(db.rows[gid]["signed_terms"])
    check("approved on the machine: its approval rides in the signed terms",
          ok["approved"] and ok["approved_where"] == "box" and st.get("approval") == "APPROVAL-1")
    check("the signed terms are the reach grant's",
          st["kind"] == "service_reach_grant" and (st["server"], st["slot"], st["subject"]) ==
          ("box1", "forge", "h.example") and st["ttl_days"] == 365 and st.get("expires"))
    # host-code path on a second grant
    db2 = DB()
    g2 = (await service_reach.propose_reach(db2, 9003, "box1", "wiki"))["grant_id"]
    box_state.clear()
    box_state.update({"state": "not_enrolled"})
    r2 = await service_reach.approve_reach(db2, 9003, g2, stepup_code="123456", ttl_days=30)
    st2 = json.loads(db2.rows[g2]["signed_terms"])
    check("no machine factor: the Host's code approves, no approval field",
          r2["approved_where"] == "host" and "approval" not in st2 and st2["ttl_days"] == 30)

    # ── 4. verified set, drift, push ────────────────────────────────────────
    exp = await service_reach.expected_reach_set(db, 9003, "box1")
    check("the box's expected set lists the verified grant",
          exp == [{"grant_id": gid, "signature": db.rows[gid]["signature"]}], str(exp))
    check("another machine's set does not list it", await service_reach.expected_reach_set(db, 9003, "box2") == [])
    db.rows[gid]["signed_terms"] = json.dumps({**st, "slot": "other"})  # tampered
    check("a tampered row is not in the set", await service_reach.expected_reach_set(db, 9003, "box1") == [])
    db.rows[gid]["signed_terms"] = json.dumps(st)
    forged = {**st, "kind": "dcn_store_grant"}
    db.rows[gid]["signed_terms"] = json.dumps(forged)
    db.rows[gid]["signature"] = base64.b64encode(priv.sign(nac_stepup._canonical(forged))).decode()
    check("a validly signed record of another kind is not a reach grant",
          await service_reach.expected_reach_set(db, 9003, "box1") == [])
    db.rows[gid]["signed_terms"] = json.dumps(st)
    db.rows[gid]["signature"] = base64.b64encode(priv.sign(nac_stepup._canonical(st))).decode()
    exp = await service_reach.expected_reach_set(db, 9003, "box1")
    sig = exp[0]["signature"]
    check("drift: nothing held → re-push", service_reach.reach_drift_ids(exp, {"service_grants": []}) == [gid])
    check("drift: held with the same signature → nothing",
          service_reach.reach_drift_ids(exp, {"service_grants": [{"grant_id": gid, "signature": sig}]}) == [])
    check("drift: held stale → re-push",
          service_reach.reach_drift_ids(exp, {"service_grants": [{"grant_id": gid, "signature": "old"}]}) == [gid])
    check("drift: unknown host state → no action", service_reach.reach_drift_ids(None, {}) == [])
    check("drift: a grant the box dropped because its server stopped is not re-pushed",
          service_reach.reach_drift_ids(exp, {"service_grants": [], "service_grants_dropped": [gid]}) == [])

    sent = {}

    async def _dispatch(pool, eid, server, op, args, token_id=None, timeout=None):
        sent.update(server=server, op=op, args=args)
        return {"exit": 0, "stdout": "{}", "stderr": ""}

    async def _audit(*a, **k):
        return None
    from server.network import nac_dcn, nac_mgmt
    nac_dcn._dispatch_box = _dispatch
    nac_mgmt.audit_mgmt = _audit
    res = await service_reach.sync_reach_grant(db, 9003, gid)
    payload = json.loads(base64.b64decode(sent["args"][0]))
    check("the push is service_grant_sync to the grant's machine",
          res["state"] == "ok" and sent["op"] == "service_grant_sync" and sent["server"] == "box1")
    check("the push carries the EXACT canonical bytes that were signed, and the key",
          base64.b64decode(payload["terms_canonical"]) == nac_stepup._canonical(st)
          and payload["signature"] == sig and base64.b64decode(payload["verify_pubkey"]) == pub_raw
          and payload["grant_id"] == gid)
    await service_reach.drop_reach_grant(db, 9003, gid, "box1")
    check("a revoke pushes service_grant_drop with the id", sent["op"] == "service_grant_drop" and sent["args"] == [str(gid)])


asyncio.run(main())

# ── 4b. device-first confirmation (review B1) ────────────────────────────────
from server.network import nac_approval  # noqa: E402

calls = []


async def _gate_dev(pool, server, token_id=None):
    return {"write_gate": True, "device": True, "device_keys": ["k"], "stepup": False,
            "second_factor": True, "seen": True}


async def _find(pool, eid, keys):
    return {"hostname": "laptop", "token_id": 9}


async def _file(pool, eid, device, details, server):
    calls.append(("file", details.get("kind"), server))
    return {"state": "pending", "id": "req-7"}


async def _status(pool, eid, device, rid):
    calls.append(("status", rid))
    return {"state": "approved", "approval": "DEVICE-APPROVAL"}

nac_approval.box_gate = _gate_dev
nac_approval.find_device = _find
nac_approval._device_file = _file
nac_approval._device_status = _status


async def device_flow():
    t = {"server": "box1", "slot": "forge", "subject": "h.example", "ttl_days": 30}
    first = await nac_approval.grant_confirmation(
        None, 9003, server="box1", purpose=service_reach.purpose_for_reach_terms(t),
        details=service_reach.reach_request_details(t))
    second = await nac_approval.grant_confirmation(
        None, 9003, server="box1", purpose=service_reach.purpose_for_reach_terms(t),
        details=service_reach.reach_request_details(t), existing=first["request"])
    t2 = {**t, "ttl_days": 31}
    n_before = len(calls)
    await nac_approval.grant_confirmation(
        None, 9003, server="box1", purpose=service_reach.purpose_for_reach_terms(t2),
        details=service_reach.reach_request_details(t2), existing=first["request"])
    refiled = calls[n_before:] and calls[n_before][0] == "file"

    async def _no_device(pool, eid, keys):
        return None
    nac_approval.find_device = _no_device
    unseen = await nac_approval.grant_confirmation(
        None, 9003, server="box1", purpose="p", details={"kind": "service_reach_grant", "server": "box1"})
    return first, second, unseen, refiled

first, second, unseen, refiled = asyncio.run(device_flow())
check("a device-approving machine: the ask is filed ON THE DEVICE, pending",
      first["state"] == "pending" and first["request"]["via"] == "device"
      and calls[0] == ("file", "service_reach_grant", "box1") and "device" in first.get("next", ""))
check("the next call polls the SAME ask and returns the device's approval",
      second["state"] == "approved" and second["approval"] == "DEVICE-APPROVAL" and calls[1] == ("status", "req-7"))
check("a device ask filed for other terms is not re-used: a new ask is filed", bool(refiled))
check("a machine whose device this Host never saw: refused, never signed blind",
      unseen["state"] == "offline" and "approval device" in unseen["detail"])

# ── 5. wiring ────────────────────────────────────────────────────────────────
src = lambda p: open(os.path.join(ROOT, p)).read()  # noqa: E731
topo = src("server/network/topology.py")
check("the heartbeat reply carries service_grants, omitted when unknown",
      'result["service_grants"] = {"grants": expected_reach}' in topo and "if expected_reach is not None:" in topo)
check("a truncated enforcing report keeps service_grants, the dropped list and pairing_host",
      '"service_grants": enforcing.get("service_grants") or []' in topo and '"pairing_host": enforcing.get("pairing_host")' in topo
      and '"service_grants_dropped": enforcing.get("service_grants_dropped") or []' in topo)
check("the heartbeat revokes rows the box dropped because their server stopped",
      "await revoke_dropped_by_stop(pool, entity_id" in topo)
routes = src("server/routes/network.py")
check("routes: list (read:topology), propose/revoke (write:network), approve (resolve:decision)",
      '"/nac/services/reach", dependencies=[Depends(requires_permission("read:topology"))]' in routes
      and '"/nac/services/reach/propose", dependencies=[Depends(requires_permission("write:network"))]' in routes
      and '"/nac/services/reach/{grant_id}/approve",\n             dependencies=[Depends(requires_permission("resolve:decision"))]' in routes)
mig = src("migrations/v180_service_reach_grants.sql")
check("migration: one live grant per (machine, server, host), slot and status constrained",
      "uq_service_reach_live" in mig and "WHERE revoked_at IS NULL" in mig
      and "service_reach_slot" in mig and "service_reach_status" in mig)
from server.network import nac_dcn as _nd  # noqa: E402
check("the reach-grant box ops are known, from the generated list (bastion 0.36.0)",
      {"service_grant_sync", "service_grant_drop"} <= _nd._GATEWAY_BOX_OPS)
harness = src("server/network/nac_harness.py")
check("a successful service up drafts this Host's own reach grant",
      'result["reach_grant"] = await service_reach.propose_reach(' in harness)
check("reach AND store grants confirm device-first",
      "nac_approval.grant_confirmation(" in src("server/network/service_reach.py")
      and "nac_approval.grant_confirmation(" in src("server/network/nac_commit.py"))
check("machine-side approvals are finished for a device-approving box too",
      'enforcing.get("device") is True' in topo)
check("approve locks its row (no double signing)", "FOR UPDATE" in src("server/network/service_reach.py"))
check("stopping a server revokes this Host's reach rows for it; the draft is token-scoped",
      "revoke_reach_for_server(" in harness and "token_id=token_id" in harness)
app = src("system-apps/network/app.js")
check("the Network app lists reach grants and approves/revokes them",
      "/api/network/nac/services/reach" in app and "approveReach" in app and "revokeReach" in app)

print(f"\nservice reach grants: {'PASS' if not failures else 'FAIL'} ({len(failures)} failing)")
sys.exit(1 if failures else 0)
