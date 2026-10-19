#!/usr/bin/env python3
"""Gate — store audience `circle`, the Host half.

Container-free: a recording fake pool, no DB, no network, no docker. It drives
the REAL functions in server/applications/appstore_circle.py and reads what they
produced; it never supplies the value it then asserts on.

The platform holds nothing for a group circle, so the store's replica of who may
fetch a circle app is only ever as good as what the creator's Host sends. Every
property below is about that one sentence.

  1. THE SNAPSHOT CARRIES CURRENT MEMBERS ONLY. A removed member must not appear.
     This is the whole feature working or not working: `roster.roster()` with
     include_removed=True would put a removed member back in the store's replica
     and hand them the bundle they were removed to stop fetching. The two calls
     differ by one keyword and a tally legitimately needs the other one, so this
     is asserted rather than assumed.
  2. THE SIGNED TERMS BIND APP AND GROUP BOTH. Without both, a signature lifted
     from app X's snapshot applies to app Y — one publisher's roster becoming
     another's.
  3. MEMBERS ARE SORTED IN THE SIGNED TERMS. One roster is one signature.
  4. THE SNAPSHOT IS SIGNED AND CHECKED THROUGH THE PRODUCTION PATH — real
     Ed25519 material, the real nac_stepup canonical form, the real signer — and
     then EVERY signed field is mutated in turn and every mutation must fail. A
     test that hands a verifier bytes it built itself cannot test the signer.
  5. ONLY ENTITY IDS TRAVEL. No display names, no verify keys, no roles. The
     store's question is "may this entity fetch this bundle" and a member id is
     the complete answer; anything else is roster detail leaving a pod for no
     reason. Asserted on the key set of a member entry, structurally.
  6. A PUBLISHER MAY ONLY GATE AN APP ON A ROSTER IT OWNS. record_publication
     refuses a group this host merely replicates — gating on a replica would
     mean gating on a list this host neither decides nor can change.
  7. posted_epoch IS WHAT THE PLATFORM ACKNOWLEDGED, NOT WHAT WE SENT. A
     snapshot the platform rejected must stay pending. Recording the send
     instead would make the next pass see "already posted" and the removal it
     carried would never arrive — and removal is the change that matters.
  8. THE EPOCH IS THE TRIGGER. reconcile_rosters selects apps whose group's
     roster_epoch has moved past the acknowledged one, and nothing else: a hook
     on the roster edit would miss every change made while the bridge was down,
     and a plain timer would re-post an unchanged roster for ever.
  9. THE TOOL SCHEMA ADMITS 'circle' AND DOCUMENTS group_ref, and the executor
     refuses circle without a group. A schema and an executor that disagree is a
     tool the model can call and the code always rejects.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


GID = "grp-9122-" + "a" * 32
OTHER_GID = "grp-9122-" + "b" * 32


class CirclePool:
    """Holds one group, its roster and the publication rows, answering on the
    SQL the production code actually writes — so a query that changes shape
    stops being answered rather than silently passing."""

    def __init__(self, *, owner=9122, members=None, epoch=7, pubs=None,
                 group_id=GID):
        self.group = {"group_id": group_id, "owner_entity_id": owner,
                      "name": "builders", "description": "", "roster_epoch": epoch,
                      "synced_epoch": None, "synced_at": None,
                      "created_at": _now(), "updated_at": _now(), "revoked_at": None}
        self.members = list(members or [])
        self.pubs = dict(pubs or {})
        self.execs = []

    async def fetchrow(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM groups WHERE group_id" in s:
            return dict(self.group) if a[0] == self.group["group_id"] else None
        return None

    async def fetch(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM group_members WHERE group_id" in s:
            rows = [dict(m) for m in self.members]
            if "removed_at IS NULL" in s:
                rows = [r for r in rows if r.get("removed_at") is None]
            return sorted(rows, key=lambda r: r["member_entity_id"])
        if "FROM app_store_circle_publications p" in s and "JOIN groups g" in s:
            out = []
            for app_id, p in self.pubs.items():
                if p["group_id"] != self.group["group_id"]:
                    continue
                if self.group["owner_entity_id"] != int(a[0]):
                    continue
                pe = p.get("posted_epoch")
                if pe is None or int(pe) < int(self.group["roster_epoch"]):
                    out.append({"app_id": app_id, "group_id": p["group_id"],
                                "posted_epoch": pe,
                                "roster_epoch": self.group["roster_epoch"]})
            return out
        return []

    async def fetchval(self, sql, *a):
        self.execs.append((sql, a))
        return None

    async def execute(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO app_store_circle_publications" in s:
            app_id, gid = a[0], a[1]
            prev = self.pubs.get(app_id)
            keep = prev.get("posted_epoch") if (prev and prev["group_id"] == gid) else None
            self.pubs[app_id] = {"group_id": gid, "posted_epoch": keep}
            return "INSERT 0 1"
        if "UPDATE app_store_circle_publications" in s and "posted_epoch" in s:
            p = self.pubs.get(a[0])
            if p is not None and a[1] is not None:
                p["posted_epoch"] = a[1]
            return "UPDATE 1"
        return "UPDATE 0"


async def main() -> int:
    from server.applications import appstore_circle as AC
    from server.groups.roster import RosterError
    import server.brain_service as _bs
    _bs._entity_id = 9122

    # ── 1, 3, 5. What the snapshot carries ────────────────────────────────
    print("\n-- the snapshot --")
    members = [
        {"member_entity_id": 9122, "role": "creator", "verify_pubkey": b"k" * 32,
         "display_name": "me", "added_at": _now(), "removed_at": None},
        {"member_entity_id": 301, "role": "member", "verify_pubkey": b"j" * 32,
         "display_name": "alice", "added_at": _now(), "removed_at": None},
        {"member_entity_id": 209, "role": "member", "verify_pubkey": None,
         "display_name": "bob", "added_at": _now(), "removed_at": None},
        # REMOVED. This one must never reach the store.
        {"member_entity_id": 404, "role": "member", "verify_pubkey": b"z" * 32,
         "display_name": "gone", "added_at": _now(), "removed_at": _now()},
    ]

    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    import server.network.nac_stepup as _ns
    from server.network import nac_stepup
    # Supply ONLY the key material; the terms, the canonical form and the
    # signature all come from production code.
    _ns.ensure_entity_signing_key = lambda pool, eid: asyncio.sleep(0, result=priv_pem)

    posted = {}

    async def _fake_bridge(op, params):
        posted["op"] = op
        posted["params"] = params
        return {"applied": True, "roster_epoch": params.get("roster_epoch")}

    AC._bridge = _fake_bridge

    pool = CirclePool(members=members, epoch=7, pubs={"com.x.app": {"group_id": GID,
                                                                   "posted_epoch": None}})
    out = await AC.post_roster(pool, 9122, "com.x.app", GID)
    check(out["ok"], "a snapshot for a group this host owns is posted", out)
    sent = posted["params"]
    check(posted["op"] == "appstore.circle_roster",
          "it goes out on the circle-roster op", posted["op"])
    check(sent["members"] == [209, 301, 9122],
          "CURRENT members only, sorted — the removed member is absent",
          sent["members"])
    check(404 not in sent["members"],
          "a removed member never reaches the store's replica")
    terms = sent["terms"]
    check(terms["app"] == "com.x.app" and terms["group"] == GID,
          "the signed terms bind BOTH the app and the group")
    check(terms["publisher"] == 9122, "and name the publisher")
    check(terms["epoch"] == 7, "and carry the group's roster_epoch", terms["epoch"])
    check(terms["members"] == sorted(terms["members"]),
          "members are sorted in the signed terms — one roster, one signature")
    check(all(isinstance(m, int) for m in terms["members"]),
          "members are bare entity ids")
    check(set(terms) == {"v", "kind", "app", "group", "publisher", "epoch",
                         "members", "signed_at"},
          "the signed terms carry EXACTLY the designed field set — no names, no "
          "keys, no roles leave the pod",
          sorted(set(terms) ^ {"v", "kind", "app", "group", "publisher", "epoch",
                               "members", "signed_at"}))

    # ── 3b. THE FROZEN BYTE VECTOR, shared with the platform ──────────────
    # The literal below is byte-identical to EXPECTED_TERMS in
    # the platform's bridge/test_appstore_circle_audience.py. Two canonical-JSON
    # implementations in two languages-worth of repos is exactly the drift
    # pairing_grants.py:35-42 already carries the scar from, so BOTH ends freeze
    # the SAME literal and either one moving turns its own gate red.
    #
    # Duplicate members are in the vector on purpose: a repeated id must not
    # change the bytes. If one end deduplicates and the other does not, a
    # perfectly valid snapshot produces bytes the verifier cannot reproduce and
    # is refused as tampered — which is what a shared vector is for.
    print("\n-- the frozen canonical vector --")
    EXPECTED_TERMS = (
        b'{"app":"widgets--team","epoch":4,"group":"grp-abababababababababababab'
        b'abababab","kind":"appstore_circle_roster","members":[9,12,9003],'
        b'"publisher":9003,"signed_at":"2026-09-17T12:00:00+00:00","v":1}'
    )
    vector = AC.roster_terms(
        app_id="widgets--team", group_id="grp-" + "ab" * 16,
        publisher_entity_id=9003, epoch=4, members=[9, 9003, 12],
        signed_at="2026-09-17T12:00:00+00:00")
    check(nac_stepup._canonical(vector) == EXPECTED_TERMS,
          "the canonical bytes for the fixed vector match the frozen literal — "
          "implementations cannot drift",
          nac_stepup._canonical(vector))
    dup = AC.roster_terms(
        app_id="widgets--team", group_id="grp-" + "ab" * 16,
        publisher_entity_id=9003, epoch=4, members=[9, 9003, 12, 9, 9003],
        signed_at="2026-09-17T12:00:00+00:00")
    check(nac_stepup._canonical(dup) == EXPECTED_TERMS,
          "a repeated member id does NOT change the signed bytes — order and "
          "duplicates are normalised inside the canonical form, not by callers")

    # ── 4. The signature, through the production path ─────────────────────
    print("\n-- the signature --")
    sig = base64.b64decode(sent["signature"])
    ok = True
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(
            sig, nac_stepup._canonical(terms))
    except Exception:
        ok = False
    check(ok, "a snapshot signed by the production path verifies under the real key")

    for field, bad in [("v", 2), ("kind", "other"), ("app", "com.y.app"),
                       ("group", OTHER_GID), ("publisher", 999), ("epoch", 8),
                       ("members", [9122, 209, 301, 404]),
                       ("signed_at", _now().isoformat())]:
        mutated = {**terms, field: bad}
        if mutated[field] == terms[field]:
            continue
        bad_ok = True
        try:
            Ed25519PublicKey.from_public_bytes(pub_raw).verify(
                sig, nac_stepup._canonical(mutated))
        except Exception:
            bad_ok = False
        check(not bad_ok, f"mutating the signed field {field!r} breaks the signature")

    # ── 6. Only a roster this host owns ───────────────────────────────────
    print("\n-- ownership --")
    pool2 = CirclePool(members=members, owner=999)   # someone else's group
    refused = None
    try:
        await AC.record_publication(pool2, "com.x.app", GID)
    except RosterError as e:
        refused = e
    check(refused is not None and refused.status == 403,
          "publishing a circle app gated on a group this host does not own is refused",
          refused.detail if refused else "accepted")

    pool3 = CirclePool(members=members, owner=9122)
    rec = await AC.record_publication(pool3, "com.x.app", GID)
    check(rec["group_id"] == GID, "a group this host owns is accepted")

    refused2 = None
    try:
        await AC.record_publication(pool3, "com.x.app", "not-a-group-id")
    except RosterError as e:
        refused2 = e
    check(refused2 is not None and refused2.status == 400,
          "a malformed group id is refused before anything is stored")

    # check_publishable WRITES NOTHING. It runs before the bundle is uploaded;
    # record_publication runs after the store has ACCEPTED the submission,
    # because a publish is a review request and not a listing. A row written at
    # check time for a submission the operator later rejects is a row
    # reconcile_rosters retries on every beat for ever, against an app the store
    # answers 404 for.
    poolc = CirclePool(members=members, owner=9122)
    await AC.check_publishable(poolc, GID)
    writes = [e for e in poolc.execs
              if "INSERT INTO" in " ".join(e[0].split())
              or "UPDATE app_store_circle_publications" in " ".join(e[0].split())]
    check(not writes, "check_publishable writes nothing", writes)
    check(not poolc.pubs, "and records no publication")

    closed = CirclePool(members=members, owner=9122)
    closed.group["revoked_at"] = _now()
    refused3 = None
    try:
        await AC.check_publishable(closed, GID)
    except RosterError as e:
        refused3 = e
    check(refused3 is not None and refused3.status == 409,
          "a CLOSED group is refused — its roster can never change again, so "
          "gating an app on it would freeze who may install it for ever",
          refused3.detail if refused3 else "accepted")

    refused4 = None
    try:
        await AC.record_publication(pool3, "", GID)
    except RosterError as e:
        refused4 = e
    check(refused4 is not None and refused4.status == 400,
          "an empty app id is refused — _declared_app_id_in_zip returns '' for a "
          "bundle it cannot read, and a row keyed on that can never be satisfied")

    unposted = await AC.post_roster(pool3, 9122, "com.x.app", OTHER_GID)
    check(not unposted["ok"],
          "post_roster refuses a group this host does not hold", unposted)

    # ── 7. posted_epoch tracks the PLATFORM's answer ──────────────────────
    print("\n-- the acknowledged epoch --")

    async def _reject(op, params):
        # The platform refusing the snapshot — a stale epoch, a refused key.
        return {"applied": False, "error": "snapshot is older than the roster held",
                "roster_epoch": 9}

    AC._bridge = _reject
    pool4 = CirclePool(members=members, epoch=7,
                       pubs={"com.x.app": {"group_id": GID, "posted_epoch": None}})
    await AC.post_roster(pool4, 9122, "com.x.app", GID)
    check(pool4.pubs["com.x.app"]["posted_epoch"] == 9,
          "a REJECTED snapshot records the platform's epoch, not ours — so the "
          "next pass still sees itself as behind",
          pool4.pubs["com.x.app"]["posted_epoch"])

    async def _down(op, params):
        return None

    AC._bridge = _down
    pool5 = CirclePool(members=members, epoch=7,
                       pubs={"com.x.app": {"group_id": GID, "posted_epoch": 6}})
    down = await AC.post_roster(pool5, 9122, "com.x.app", GID)
    check(not down["ok"] and down.get("error"),
          "a bridge that is not there is reported, not swallowed", down)
    check(pool5.pubs["com.x.app"]["posted_epoch"] == 6,
          "and it does NOT advance the acknowledged epoch",
          pool5.pubs["com.x.app"]["posted_epoch"])

    # ── 8. The epoch is the trigger ───────────────────────────────────────
    print("\n-- the reconciler --")
    AC._bridge = _fake_bridge
    posted.clear()
    caught = CirclePool(members=members, epoch=7,
                        pubs={"com.x.app": {"group_id": GID, "posted_epoch": 7}})
    n = await AC.reconcile_rosters(caught, 9122)
    check(n == 0, "an app whose roster has NOT moved sends nothing", n)
    check("op" not in posted, "and nothing reaches the bridge")

    behind = CirclePool(members=members, epoch=8,
                        pubs={"com.x.app": {"group_id": GID, "posted_epoch": 7}})
    n = await AC.reconcile_rosters(behind, 9122)
    check(n == 1, "an app whose roster HAS moved is re-sent", n)
    check(posted.get("params", {}).get("roster_epoch") == 8,
          "at the new epoch", posted.get("params", {}).get("roster_epoch"))

    never = CirclePool(members=members, epoch=1,
                       pubs={"com.x.app": {"group_id": GID, "posted_epoch": None}})
    check(await AC.reconcile_rosters(never, 9122) == 1,
          "an app that has never been posted is sent even at epoch 1")

    # A re-publish to a DIFFERENT group must clear the acknowledged epoch, or
    # the new group's epoch 1 looks older than what we hold and the first
    # snapshot is never sent.
    moved = CirclePool(members=members, owner=9122,
                       pubs={"com.x.app": {"group_id": OTHER_GID, "posted_epoch": 12}})
    await AC.record_publication(moved, "com.x.app", GID)
    check(moved.pubs["com.x.app"]["posted_epoch"] is None,
          "re-publishing to a DIFFERENT group resets the acknowledged epoch",
          moved.pubs["com.x.app"]["posted_epoch"])
    same = CirclePool(members=members, owner=9122,
                      pubs={"com.x.app": {"group_id": GID, "posted_epoch": 12}})
    await AC.record_publication(same, "com.x.app", GID)
    check(same.pubs["com.x.app"]["posted_epoch"] == 12,
          "re-publishing to the SAME group keeps it")

    # ── 9. Tool schema and executor agree ─────────────────────────────────
    print("\n-- the publish_app tool --")
    from mastercode.public_tool_registry import _FINAL_HOST_TOOL_SCHEMAS as S
    props = S["publish_app"]["function"]["parameters"]["properties"]
    check("circle" in props["audience"]["enum"],
          "the tool schema offers audience='circle'", props["audience"]["enum"])
    check("group_ref" in props, "and declares group_ref")
    # The doctrine is NOT in the schema, and that is deliberate: the wire budget
    # is a hard ratchet (test_registry_slim.WIRE_MAX, whose own header says the
    # honest fix for an overrun is the phase-6 fetch-on-demand lane and not a
    # bigger number), and its documented remedy is the skill file. So the
    # invariant to hold is that the guidance EXISTS somewhere the model reads —
    # a schema trimmed to fit with the explanation dropped on the floor would
    # leave a required field the model is never told how to fill.
    check("Skill 'apps'" in props["group_ref"]["description"],
          "the trimmed schema points at the skill that carries the doctrine",
          props["group_ref"]["description"])
    apps_md = open(os.path.join(_ROOT, "default-skills", "apps.md")).read()
    check("group_ref" in apps_md and "'circle'" in apps_md,
          "and default-skills/apps.md actually carries it")
    check("THIS host owns" in apps_md and "is refused" in apps_md,
          "including the bound that decides which groups are eligible")

    src = open(os.path.join(_ROOT, "brain", "mastercode", "runtime", "tools.py")).read()
    check('"public", "internal", "circle"' in src,
          "the executor admits the same three values the schema offers")
    check("audience == \"circle\" and not group_ref" in src,
          "and refuses circle without a group BEFORE the bundle is uploaded")

    # ── 10. `distribution` reaches the store app ──────────────────────────
    # The badge that tells a publisher (and a member) an app is private to a
    # group reads `distribution`, which the CONTROLLER puts on the catalog row.
    # This host does not add it — it must not DROP it either, and the only thing
    # standing between those two is that store_catalog spreads the upstream row
    # with `**entry` instead of allow-listing fields. Converting that to an
    # explicit list is a one-line refactor that would silently un-badge every
    # circle app, with nothing failing anywhere.
    print("\n-- distribution survives the host's catalog route --")
    apps_src = open(os.path.join(_ROOT, "server", "routes", "applications.py")).read()
    cat = apps_src[apps_src.index("async def store_catalog("):]
    cat = cat[:cat.index("# Image-baked apps WITH a runtime")]
    check("**entry" in cat,
          "store_catalog spreads the upstream row rather than allow-listing "
          "fields, so `distribution` reaches the store app unchanged")
    check("\"distribution\"" not in cat,
          "and this host does not synthesise it — 045 made audience/public "
          "DERIVED, so distribution is the store's to state, not ours to guess")

    store_js = open(os.path.join(_ROOT, "system-apps", "app-store", "app.js")).read()
    check("a.distribution === 'circle'" in store_js,
          "the store app badges on `distribution`, not on the derived "
          "`audience` or `public` columns")
    check("badge-circle" in open(os.path.join(
              _ROOT, "system-apps", "app-store", "style.css")).read(),
          "and the badge has a style of its own rather than reusing the "
          "retirement red, which would read as a warning about the app")

    print(f"\n{CHECKS} checks, {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  FAILED: " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
