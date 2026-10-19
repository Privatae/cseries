#!/usr/bin/env python3
"""Groups: a roster and a signed vote.

What is pinned here, and why each one:

  1. THE SIGNATURE IS REAL AND IT BINDS EVERY FIELD. A vote is signed through
     the production path (votes.sign_vote -> nac_stepup.sign_policy_terms ->
     the real entity_sign key material and the real canonical form) and then
     verified through the production verifier. The test supplies the KEY, never
     the signature and never the canonical bytes — a test that hands the
     verifier bytes it built itself cannot test the signer. Every signed field
     is then mutated in turn and every mutation must fail.
  2. A VOTE IS BOUND TO ONE QUESTION. Terms naming a different group or ballot
     are refused as a binding failure, so a signature lifted from group X's
     ballot cannot count in group Y's.
  3. A REPLAY IS REFUSED, A CHANGE OF MIND IS NOT. seq must be strictly ahead.
  4. AN UNPINNED KEY IS NOT A FORGERY. It is its own reason, the vote is not
     counted, and the tally SAYS how many it could not count.
  5. THE LANE OPS ARE CLASSIFIED. Each is in _OP_ACTION or
     _OP_ACTION_FROM_PARAMS and none is slipless, so none can reach a handler
     ungated; and the params-derived action is group:<id> read off the request,
     so a slip for group X cannot carry group Y.
  6. THE GROUP APP GRANT. A group-scoped slip's resource parses back to its
     group, app.deliver refuses a sender who is `removed_at` in the local
     roster and admits one who is not, and the fan-out queues one row per
     CURRENT member and none for a removed one or for this host itself.
  7. THE ROSTER REPLICA REPLACES, IT DOES NOT MERGE. A member the owner's
     snapshot omits is stamped removed — otherwise a removal would be the one
     change replication could never carry, which is the change that matters.
  8. FIRST KEY WINS. A different key presented later is refused, not
     overwritten.
  9. The scopes exist, carry the bits the design states, community apps may
     declare them, and the migration declares every table the code writes to.
 10. RE-ADMISSION WORKS. A grant counts only while it is LIVE — not revoked,
     not past its slip's term, and scoped to this group. The first cut matched
     any (app, peer, op) row, so a member removed and added back was skipped
     for ever: the slip was revoked and the stale delivered row still matched.
     Asserted on the real SQL predicate, not only on the outcome.
 11. UNINSTALL ORDER. An app's group slips are revoked BEFORE the rows that
     carry their contract ids are deleted; reversed, the contracts strand live.
 12. seq SPANS THE GROUP, not one ballot — the scope communityd uses, so a
     member's counter means one thing whichever tier stores the vote.
 13. THE CANONICAL ESCAPE TABLE, case by case, against what CPython produces —
     the same table communityd's Go encoder is held to, covering exactly the
     characters the two languages disagree about.
 14. A vote stored but NOT counted says so structurally and says what to do.

Container-free: real functions against a recording fake pool. No DB, no
container, no network.

Run:  python3 scripts/test_groups_roster_votes.py
"""
import asyncio
import base64
import datetime as _dt
import json
import os
import re
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


class RosterPool:
    """A fake pool holding one group's roster, ballot and votes in dicts, and
    recording every statement. It answers by matching on the SQL the production
    code actually writes, so a query that changes shape stops being answered
    instead of silently passing."""

    def __init__(self, *, group_id, owner, members, ballot=None):
        self.group = {"group_id": group_id, "owner_entity_id": owner,
                      "name": "builders", "description": "", "roster_epoch": 7,
                      "synced_epoch": None, "synced_at": None,
                      "created_at": _now(), "updated_at": _now(), "revoked_at": None}
        # member_entity_id -> dict
        self.members = {int(m["member_entity_id"]): dict(m) for m in members}
        self.ballot = ballot
        self.votes = {}
        self.execs = []
        self.outbox = []

    # -- helpers ------------------------------------------------------------
    def _member_rows(self, include_removed=True):
        rows = [dict(m) for m in self.members.values()]
        if not include_removed:
            rows = [r for r in rows if r.get("removed_at") is None]
        return sorted(rows, key=lambda r: r["member_entity_id"])

    # -- pool surface -------------------------------------------------------
    async def fetchrow(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM groups WHERE group_id" in s:
            return dict(self.group) if a[0] == self.group["group_id"] else None
        if "SELECT verify_pubkey FROM group_members" in s:
            m = self.members.get(int(a[1]))
            return {"verify_pubkey": m.get("verify_pubkey")} if m else None
        if "SELECT role, removed_at FROM group_members" in s:
            m = self.members.get(int(a[1]))
            return {"role": m["role"], "removed_at": m.get("removed_at")} if m else None
        if "FROM group_ballots WHERE group_id" in s:
            return dict(self.ballot) if self.ballot else None
        if "INSERT INTO group_outbox" in s:
            row = {"id": len(self.outbox) + 1, "status": "queued",
                   "message_id": a[4], "attempts": 0,
                   "group_id": a[0], "peer_entity_id": a[1], "op": a[2]}
            self.outbox.append(row)
            return row
        if "INSERT INTO peer_app_outbox" in s:
            row = {"id": len(self.outbox) + 1, "status": "queued",
                   "message_id": a[5], "attempts": 0,
                   "app_id": a[0], "peer_entity_id": a[1], "op": a[2]}
            self.outbox.append(row)
            return row
        return None

    async def fetchval(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "SELECT 1 FROM group_members" in s and "removed_at IS NULL" in s:
            m = self.members.get(int(a[1]))
            return 1 if (m and m.get("removed_at") is None) else None
        if "SELECT max(seq) FROM group_votes" in s:
            # Per (group, voter), across every ballot — the scope communityd
            # uses. Matched on the REAL statement, so reverting to a per-ballot
            # query stops being answered here rather than quietly passing.
            seqs = [v["seq"] for v in self.votes.values()
                    if v["voter_entity_id"] == int(a[1])]
            return max(seqs) if seqs else None
        if "SELECT role FROM group_members" in s:
            m = self.members.get(int(a[1]))
            return m["role"] if (m and m.get("removed_at") is None) else None
        if "roster_epoch = roster_epoch + 1" in s:
            self.group["roster_epoch"] += 1
            return self.group["roster_epoch"]
        return None

    async def fetch(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM group_members WHERE group_id" in s:
            return self._member_rows(include_removed="removed_at IS NULL" not in s)
        if "FROM group_votes WHERE group_id" in s:
            # Scoped to the ballot, like the real query: with a per-(group,
            # voter) seq a member has rows on several ballots at once, and a
            # tally that saw all of them would count the wrong question.
            return [dict(v) for k, v in self.votes.items() if k[0] == a[1]]
        return []

    async def execute(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO group_votes" in s:
            # Keyed as the real PK is — (group, ballot, voter) — so the
            # per-ballot row and the per-(group, voter) counter are modelled as
            # the two different things they are.
            key = (a[1], int(a[2]))
            voter = int(a[2])
            prev = self.votes.get(key)
            new = {"voter_entity_id": voter, "choice": a[3], "seq": int(a[4]),
                   "nonce": a[5], "signed_at": a[6], "signature": a[7],
                   "key_id": a[8], "verified": bool(a[9]), "verify_error": a[10],
                   "received_at": _now()}
            # The production statement guards the upsert on EXCLUDED.seq >
            # group_votes.seq; the fake has to honour that or the seq test
            # would pass here and fail in Postgres.
            if prev is None or new["seq"] > prev["seq"]:
                self.votes[key] = new
            return "INSERT 0 1"
        if "UPDATE group_members SET verify_pubkey" in s:
            m = self.members.get(int(a[1]))
            if m is not None and not m.get("verify_pubkey"):
                m["verify_pubkey"] = a[2]
                m["key_pinned_at"] = _now()
            return "UPDATE 1"
        if "UPDATE group_members SET removed_at = now()" in s and "NOT (member_entity_id = ANY" in s:
            keep = set(int(x) for x in (a[1] or []))
            n = 0
            for mid, m in self.members.items():
                if mid not in keep and m.get("removed_at") is None:
                    m["removed_at"] = _now()
                    n += 1
            return f"UPDATE {n}"
        if "UPDATE group_members SET removed_at = now()" in s:
            m = self.members.get(int(a[1]))
            if m and m.get("removed_at") is None:
                m["removed_at"] = _now()
                return "UPDATE 1"
            return "UPDATE 0"
        if "INSERT INTO group_members" in s:
            mid = int(a[1])
            cur = self.members.setdefault(mid, {"member_entity_id": mid})
            cur["role"] = a[2]
            if "verify_pubkey" in s:
                cur.setdefault("verify_pubkey", a[3] if len(a) > 3 else None)
                cur["removed_at"] = a[5] if len(a) > 5 else None
            else:
                cur["removed_at"] = None
            cur.setdefault("verify_pubkey", None)
            cur.setdefault("added_at", _now())
            return "INSERT 0 1"
        if "INSERT INTO groups" in s:
            self.group["roster_epoch"] = int(a[4]) if len(a) > 4 else self.group["roster_epoch"]
            if len(a) > 4:
                self.group["synced_epoch"] = int(a[4])
        return "OK"


def _make_key():
    """A real Ed25519 private key PEM, the shape ensure_entity_signing_key
    returns. Supplying the KEY is the only thing this test stubs about signing:
    the canonical bytes and the signature itself are produced by the production
    path."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(encoding=serialization.Encoding.PEM,
                          format=serialization.PrivateFormat.PKCS8,
                          encryption_algorithm=serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)
    return pem, pub


async def main() -> int:
    from server import brain_service
    from server.groups import lane as LANE
    from server.groups import roster as R
    from server.groups import votes as V
    from server.network import nac_stepup
    from server.peer import app_lane as AL
    from server.peer import handlers as H
    from server.peer import peer_server as PS

    GID = R.new_group_id(3)
    OWNER, ALICE, BOB, CAROL = 3, 122, 123, 124
    brain_service._entity_id = OWNER

    # Real keys. Each entity signs with its own; the roster pins the public
    # half, which is exactly what makes a tally recountable.
    keys = {e: _make_key() for e in (OWNER, ALICE, BOB, CAROL)}

    async def fake_ensure(pool, entity_id):
        return keys[int(entity_id)][0]

    nac_stepup.ensure_entity_signing_key = fake_ensure

    # ── 1. the signature is real, and it binds every field ──────────────────
    print("\n1. the signed vote")
    ballot = {"group_id": GID, "ballot_id": "name-it", "question": "Which name?",
              "choices": ["helios", "vela"], "target_kind": "", "target_ref": "",
              "created_by": OWNER, "opens_at": _now(),
              "closes_at": _now() + _dt.timedelta(days=7), "closed_at": None,
              "created_at": _now()}
    members = [
        {"member_entity_id": OWNER, "role": "creator", "verify_pubkey": keys[OWNER][1],
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": _now()},
        {"member_entity_id": ALICE, "role": "member", "verify_pubkey": keys[ALICE][1],
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": _now()},
        {"member_entity_id": BOB, "role": "moderator", "verify_pubkey": keys[BOB][1],
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": _now()},
        # Carol joined but has not pulled yet, so her key is not pinned.
        {"member_entity_id": CAROL, "role": "member", "verify_pubkey": None,
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": None},
    ]
    pool = RosterPool(group_id=GID, owner=OWNER, members=members, ballot=ballot)
    brain_service._pool = pool

    vote = await V.sign_vote(pool, ALICE, group_id=GID, ballot_id="name-it",
                             choice="helios", seq=1)
    check(set(vote) == {"terms", "signature", "key_id"}, "a signed vote is terms + signature + key_id", sorted(vote))
    check(vote["key_id"] == "entity_sign",
          "signed with entity_sign — the host's existing identity key, not a new one", vote["key_id"])
    t = vote["terms"]
    check(t["v"] == 1 and t["kind"] == "group_vote", "terms carry a version and a kind")
    check(set(t) == {"v", "kind", "group", "ballot", "voter", "choice", "seq", "nonce", "signed_at"},
          "every field the design names is signed, and nothing else is", sorted(t))
    check(re.fullmatch(r"[0-9a-f]{32}", t["nonce"]) is not None,
          "the nonce is 16 random bytes hex — the bridge's own slip nonce width", t["nonce"])

    ok, why, _ = V.verify_vote(vote, verify_pubkey=keys[ALICE][1], group_id=GID,
                               ballot_id="name-it", choices=ballot["choices"])
    check(ok, "the production verifier accepts what the production signer produced", why)

    # The canonical bytes ARE nac_stepup's, not a second implementation.
    check(V._canonical({"b": 1, "a": "x"}) == nac_stepup._canonical({"b": 1, "a": "x"}),
          "the canonical form is nac_stepup's, imported rather than re-implemented")
    check(V._canonical({"a": "é"}) == b'{"a":"\\u00e9"}',
          "canonical bytes are ASCII-escaped, sorted and compact — what communityd must match",
          V._canonical({"a": "é"}))

    # THE CROSS-LANGUAGE VECTOR. communityd verifies these same bytes in Go,
    # and Python and Go canonical JSON differ on unicode — the failure
    # pairing_grants.py:35-42 already records. Both sides assert this one fixed
    # vector byte-for-byte, so a drift in either implementation fails a gate
    # instead of silently rejecting every vote that crosses the tier.
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    VEC_TERMS = {"v": 1, "kind": "group_vote", "group": "42", "ballot": "7",
                 "voter": 122, "choice": "yes", "seq": 3,
                 "nonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                 "signed_at": "2026-09-17T12:00:00+00:00"}
    VEC_CANONICAL = (b'{"ballot":"7","choice":"yes","group":"42","kind":"group_vote",'
                     b'"nonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","seq":3,'
                     b'"signed_at":"2026-09-17T12:00:00+00:00","v":1,"voter":122}')
    VEC_PUB_B64 = "ebVWLo/mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ="
    VEC_SIG_B64 = ("/DFnd8jVWpC/6ky5fE2n3pktHmH1uFZIVKRIuQO6vWmRXvnb5vw1uPa4LmOA116/"
                   "vyvM02bTpyyttj36Qwx+Ag==")
    vk = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    vpub = vk.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
    check(base64.b64encode(vpub).decode() == VEC_PUB_B64,
          "the vector's key is the one communityd's Go test uses", base64.b64encode(vpub).decode())
    check(V._canonical(VEC_TERMS) == VEC_CANONICAL,
          "the canonical bytes match communityd's, byte for byte", V._canonical(VEC_TERMS))
    check(base64.b64encode(vk.sign(V._canonical(VEC_TERMS))).decode() == VEC_SIG_B64,
          "and so does the signature over them")
    check(V.verify_vote({"terms": VEC_TERMS, "signature": VEC_SIG_B64, "key_id": "entity_sign"},
                        verify_pubkey=vpub, group_id="42", ballot_id="7",
                        choices=["yes", "no"])[0],
          "the production verifier accepts a vote signed on the Go side")
    # THE ESCAPE TABLE, case by case. communityd's Go string encoder carries the
    # identical table, because these are exactly the characters Go's
    # encoding/json and CPython's json disagree about: Go escapes <, > and & by
    # default, and neither language's default agrees with ensure_ascii=True on
    # astral characters. Each expectation is what CPython produces, because
    # CPython is what signs on this tier.
    for _raw, _want in (
            ("\u00e9",        b'{"s":"\\u00e9"}'),
            ("\U0001F600",    b'{"s":"\\ud83d\\ude00"}'),
            ("\u2028",        b'{"s":"\\u2028"}'),
            ("\t",            b'{"s":"\\t"}'),
            ("\x01",          b'{"s":"\\u0001"}'),
            ("\x7f",          b'{"s":"\\u007f"}'),
            ("\b",            b'{"s":"\\b"}'),
            ("\f",            b'{"s":"\\f"}'),
            ("<&>",            b'{"s":"<&>"}'),
    ):
        _got = V._canonical({"s": _raw})
        check(_got == _want, f"canonical escaping of {_raw!r} matches CPython exactly", _got)

    # Mutate each signed field in turn. Every one must break the signature or
    # the binding; none may quietly pass.
    for field, bad in (("group", R.new_group_id(3)), ("ballot", "other"),
                       ("choice", "vela"), ("voter", BOB), ("seq", 99),
                       ("nonce", "0" * 32), ("signed_at", _now().isoformat()),
                       ("v", 2), ("kind", "not_a_vote")):
        tampered = {**vote, "terms": {**t, field: bad}}
        ok2, why2, _ = V.verify_vote(tampered, verify_pubkey=keys[ALICE][1], group_id=GID,
                                     ballot_id="name-it", choices=ballot["choices"])
        check(not ok2, f"tampering with '{field}' is refused ({why2})")

    # A valid signature from the WRONG key is refused.
    ok3, why3, _ = V.verify_vote(vote, verify_pubkey=keys[BOB][1], group_id=GID,
                                 ballot_id="name-it", choices=ballot["choices"])
    check(not ok3 and "signature invalid" in why3, "a vote checked against another member's key is refused", why3)

    # ── 2. a vote is bound to ONE question ──────────────────────────────────
    print("\n2. binding")
    ok4, why4, _ = V.verify_vote(vote, verify_pubkey=keys[ALICE][1], group_id=R.new_group_id(3),
                                 ballot_id="name-it", choices=ballot["choices"])
    check(not ok4 and "different group" in why4,
          "a signature lifted into another GROUP is refused as a binding failure", why4)
    ok5, why5, _ = V.verify_vote(vote, verify_pubkey=keys[ALICE][1], group_id=GID,
                                 ballot_id="other-ballot", choices=ballot["choices"])
    check(not ok5 and "different ballot" in why5,
          "and into another BALLOT of the same group", why5)
    ok6, why6, _ = V.verify_vote(vote, verify_pubkey=keys[ALICE][1], group_id=GID,
                                 ballot_id="name-it", choices=["vela", "orion"])
    check(not ok6 and "not one this ballot offers" in why6,
          "a choice the ballot does not offer is refused", why6)

    # ── 3. replay vs a change of mind ───────────────────────────────────────
    print("\n3. seq")
    r = await LANE.accept_vote(pool, GID, "name-it", vote)
    check(r.get("ok") and r.get("verified") and r.get("counted"),
          "a well-formed vote from a pinned member is stored and counted", r)
    check(pool.votes[("name-it", ALICE)]["seq"] == 1 and pool.votes[("name-it", ALICE)]["choice"] == "helios",
          "and the row is what she signed", pool.votes.get(("name-it", ALICE)))

    replay = await LANE.accept_vote(pool, GID, "name-it", vote)
    check(not replay.get("ok") and "not ahead" in (replay.get("error") or ""),
          "the SAME signed vote replayed is refused — seq must be strictly ahead", replay)

    changed = await V.sign_vote(pool, ALICE, group_id=GID, ballot_id="name-it",
                                choice="vela", seq=2)
    r2 = await LANE.accept_vote(pool, GID, "name-it", changed)
    check(r2.get("ok") and pool.votes[("name-it", ALICE)]["choice"] == "vela" and pool.votes[("name-it", ALICE)]["seq"] == 2,
          "a higher seq supersedes in place — a change of mind is one row, not two", pool.votes.get(("name-it", ALICE)))

    stale = await V.sign_vote(pool, ALICE, group_id=GID, ballot_id="name-it",
                              choice="helios", seq=1)
    r3 = await LANE.accept_vote(pool, GID, "name-it", stale)
    check(not r3.get("ok") and pool.votes[("name-it", ALICE)]["choice"] == "vela",
          "a freshly-signed vote at an OLD seq is refused too — the guard is seq, not the bytes", r3)

    # ── 4. an unpinned key is not a forgery ─────────────────────────────────
    print("\n4. the unpinned member")
    carol = await V.sign_vote(pool, CAROL, group_id=GID, ballot_id="name-it",
                              choice="helios", seq=1)
    rc = await LANE.accept_vote(pool, GID, "name-it", carol)
    check(rc.get("ok") and rc.get("stored") and not rc.get("verified"),
          "a vote from a member with no pinned key is STORED and not counted", rc)
    check("no pinned key" in (rc.get("reason") or ""),
          "and the reason says so rather than reading as a bad signature", rc.get("reason"))

    forged = {**carol, "signature": base64.b64encode(b"\x00" * 64).decode()}
    forged["terms"] = {**carol["terms"], "voter": BOB, "seq": 5}
    rf = await LANE.accept_vote(pool, GID, "name-it", forged)
    check(not rf.get("ok") and ("name-it", BOB) not in pool.votes,
          "a forged signature for a PINNED member is refused and nothing is stored", rf)

    bobv = await V.sign_vote(pool, BOB, group_id=GID, ballot_id="name-it",
                             choice="vela", seq=1)
    await LANE.accept_vote(pool, GID, "name-it", bobv)
    tal = await R.tally_ballot(pool, GID, "name-it")
    check(tal["counts"] == {"helios": 0, "vela": 2}, "the tally counts only verified votes", tal["counts"])
    check(tal["members_without_pinned_key"] == 1,
          "and reports the member it could not count", tal)
    check(tal["unverified"] == 1, "the stored-but-unverified row is reported, not dropped", tal)
    check(tal["complete"] is False,
          "a tally with an uncounted member is never reported complete", tal["complete"])
    check(tal["roster_size"] == 4 and abs(tal["turnout"] - 0.5) < 1e-9,
          "turnout is against the CURRENT roster", tal)

    # ── 5. the lane ops are classified ──────────────────────────────────────
    print("\n5. lane classification")
    check(PS._OP_ACTION.get("group.grant") == "message",
          "group.grant rides the message slip — it is the invite, and carries the slip it would present")
    for op in ("group.roster_fetch", "group.vote"):
        check(op in PS._OP_ACTION_FROM_PARAMS, f"{op}'s action is derived per request")
        check(op not in PS._SLIPLESS_OPS, f"{op} is not slipless")
        check(op in H.PEER_HANDLERS, f"{op} has a registered handler")
    check("group.grant" in H.PEER_HANDLERS and "group.grant" not in PS._SLIPLESS_OPS,
          "group.grant is registered and not slipless")
    check(LANE.action_for_group({"group_id": GID}) == f"group:{GID}",
          "the derived action is group:<group_id>, read off the request")
    check(LANE.action_for_group({"group_id": "nope"}) is None, "a malformed group id yields no action")
    check(LANE.action_for_group({}) is None, "missing group_id yields no action")

    class Trust:
        ready = True
        keys = {}
        revoked = set()

        async def ensure_key(self, k):
            return False

    reached = []

    async def spy(peer_id, slip_info, params):
        reached.append(params)
        return {"ok": True}

    srv = PS.PeerServer(trust=Trust(), handlers={"group.vote": spy}, my_entity_id=OWNER)
    res = await srv._serve(ALICE, {"op": "group.vote", "slip": {"x": 1}, "params": {"group_id": "bad"}})
    check(not res.get("ok") and "unknown op" in (res.get("error") or ""),
          "the server refuses a group op naming no valid group, as an unknown op", res)
    check(not reached, "and it never reaches the handler")

    # ── 6. the group app grant ──────────────────────────────────────────────
    print("\n6. the group app grant")
    res_str = AL.group_app_resource(GID, "veterans")
    check(res_str == f"group:{GID}:app:veterans", "a group grant's resource names the group AND the app", res_str)
    check(AL.group_from_resource(res_str) == GID, "and parses back to the group")
    check(AL.group_from_resource("app:veterans") is None,
          "a per-contact resource carries no group, so the membership gate does not engage")
    for bad in ("group::app:x", f"group:{GID}", "group:nope:app:x", "", None, 7):
        check(AL.group_from_resource(bad) is None, f"a malformed group resource yields no group ({bad!r})")

    class AppPool(RosterPool):
        async def fetchval(self, sql, *a):
            s = " ".join(sql.split())
            if "INSERT INTO peer_app_inbox" in s:
                self.execs.append((sql, a))
                return 1
            return await super().fetchval(sql, *a)

    apool = AppPool(group_id=GID, owner=OWNER, members=members, ballot=ballot)
    brain_service._pool = apool

    class FakeApp:
        permissions = ["read:peer_app", "write:peer_app"]
        status = "active"

    from server.applications import registry as REG

    async def fake_get(p, app_id):
        return FakeApp() if app_id == "veterans" else None

    REG.get = fake_get

    async def no_live(*a, **k):
        return None

    AL._hand_to_live_process = no_live

    import uuid as _uuid
    params = {"app_id": "veterans", "action": "probe", "payload": {},
              "message_id": str(_uuid.uuid4())}
    info = {"allowed_ops": ["app:veterans"], "resource": res_str}
    ok_del = await AL.app_deliver(ALICE, info, params)
    check(ok_del.get("ok"), "a group-scoped delivery from a CURRENT member is admitted", ok_del)

    apool.members[ALICE]["removed_at"] = _now()
    params2 = {**params, "message_id": str(_uuid.uuid4())}
    no_del = await AL.app_deliver(ALICE, info, params2)
    check(not no_del.get("ok") and "no longer a member" in (no_del.get("error") or ""),
          "and refused the moment she is removed_at in the LOCAL roster — no bridge call, no CRL wait",
          no_del)
    check(not no_del.get("retryable"),
          "the refusal is final, so a sender's outbox does not retry it for seven days")

    # The same delivery under a PER-CONTACT slip still works: removing someone
    # from a group must not cut a grant that was never group-scoped.
    plain = await AL.app_deliver(ALICE, {"allowed_ops": ["app:veterans"], "resource": "app:veterans"},
                                 {**params, "message_id": str(_uuid.uuid4())})
    check(plain.get("ok"), "a per-contact grant is untouched by group membership", plain)

    apool.members[ALICE]["removed_at"] = None
    apool.members[CAROL]["removed_at"] = _now()
    apool.outbox.clear()
    fan = await AL.grant_group(apool, "veterans", GID, OWNER)
    peers = sorted(q["peer_entity_id"] for q in fan["queued"])
    check(peers == [ALICE, BOB],
          "the fan-out queues one grant per CURRENT member — not the removed one, not this host", peers)
    check(all((r.get("op") == AL.OP_GRANT) for r in apool.outbox),
          "every queued row is an app.grant")
    payloads = [a for s, a in apool.execs if "INSERT INTO peer_app_outbox" in " ".join(s.split())]
    check(payloads and all(json.loads(p[4]).get("group_id") == GID for p in payloads),
          "and each row carries the group id, so a retry re-mints the same group-scoped slip")

    # Removal revokes BOTH slip families. Revoking only the group slip would
    # leave the group-scoped APP slip — the one that actually gates a delivery
    # — live for a member whose replica is behind, which is the exact case
    # layer 2 exists for.
    revoked = []

    async def fake_bridge(op, params):
        revoked.append((op, params.get("contract_id")))
        return {"ok": True}

    LANE._bridge = fake_bridge

    class RevokePool(RosterPool):
        async def fetch(self, sql, *a):
            s = " ".join(sql.split())
            if "FROM group_outbox" in s:
                return [{"cid": "slip-group-1"}]
            if "FROM peer_app_outbox" in s:
                return [{"cid": "slip-app-1"}, {"cid": None}]
            return await super().fetch(sql, *a)

    n = await LANE.revoke_group_slips(
        RevokePool(group_id=GID, owner=OWNER, members=members), GID, ALICE)
    cids = sorted(c for _o, c in revoked)
    check(n == 2 and cids == ["slip-app-1", "slip-group-1"],
          "removal revokes the group slip AND the group-scoped app slip", (n, cids))
    check(all(o == "contract.revoke" for o, _c in revoked),
          "through contract.revoke, so the CRL carries it to every verifier")

    # ── 6c. re-admission gets a FRESH grant ─────────────────────────────────
    print("\n6c. re-admission")
    # The first cut matched any (app, peer, op) row, so the DELIVERED row of a
    # revoked contract made re-admission a PERMANENT exclusion: the slip was
    # gone and the reconciler believed it was still there. The liveness
    # predicate is the fix, so it is asserted on the real SQL and not only on
    # the outcome.
    live = AL._LIVE_GROUP_GRANT
    check("o.status <> 'revoked'" in live, "a revoked grant row is not a live grant")
    check("o.payload->>'group_id' = $1" in live,
          "the match is scoped to the GROUP, so two groups are two grants")
    check("o.created_at > now() - ($5 || ' seconds')::interval" in live,
          "and a grant past the slip's term is not live either")

    class ReadmitPool(RosterPool):
        """Answers the real reconcile query by EVALUATING its predicate against
        modelled rows, rather than pattern-matching an expected answer."""

        def __init__(self, rows, **kw):
            super().__init__(**kw)
            self.app_rows = rows

        async def fetch(self, sql, *a):
            s = " ".join(sql.split())
            if "FROM group_app_grants" in s:
                return [{"group_id": GID, "app_id": "veterans"}]
            if "FROM group_members m" in s and "NOT EXISTS" in s:
                out = []
                for m in self._member_rows(include_removed=False):
                    mid = m["member_entity_id"]
                    if mid == int(a[1]):
                        continue
                    if not any(r["peer_entity_id"] == mid and r["group_id"] == a[0]
                               and r["status"] != "revoked" and r["fresh"]
                               for r in self.app_rows):
                        out.append({"member_entity_id": mid})
                return out
            return await super().fetch(sql, *a)

    def _r(peer, status="delivered", fresh=True):
        return {"peer_entity_id": peer, "group_id": GID, "status": status, "fresh": fresh}

    held_all = ReadmitPool([_r(ALICE), _r(BOB), _r(CAROL)],
                           group_id=GID, owner=OWNER, members=members)
    check(await AL.reconcile_group_grants(held_all, OWNER) == 0,
          "with every member holding a live grant, the reconciler queues nothing")

    revoked_row = ReadmitPool([_r(ALICE, status="revoked"), _r(BOB), _r(CAROL)],
                              group_id=GID, owner=OWNER, members=members)
    check(await AL.reconcile_group_grants(revoked_row, OWNER) == 1,
          "a re-admitted member whose grant was REVOKED is granted again")
    fresh_payload = [json.loads(a[4]) for s, a in revoked_row.execs
                     if "INSERT INTO peer_app_outbox" in " ".join(s.split())]
    check(fresh_payload and fresh_payload[0].get("group_id") == GID,
          "and the new row names the group, so the re-issued slip is group-scoped again",
          fresh_payload)

    aged = ReadmitPool([_r(ALICE, fresh=False), _r(BOB), _r(CAROL)],
                       group_id=GID, owner=OWNER, members=members)
    check(await AL.reconcile_group_grants(aged, OWNER) == 1,
          "a grant past the slip's term is re-issued rather than left lapsed")

    # The other half: removal must STAMP the row, or the predicate above has
    # nothing to see.
    class StampPool(RosterPool):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.stamped = []

        async def fetch(self, sql, *a):
            s = " ".join(sql.split())
            if "FROM group_outbox" in s or "FROM peer_app_outbox" in s:
                return []
            return await super().fetch(sql, *a)

        async def execute(self, sql, *a):
            if "UPDATE peer_app_outbox SET status = 'revoked'" in " ".join(sql.split()):
                self.stamped.append(a)
                return "UPDATE 1"
            return await super().execute(sql, *a)

    sp = StampPool(group_id=GID, owner=OWNER, members=members)
    await LANE.revoke_group_slips(sp, GID, ALICE)
    check(sp.stamped and sp.stamped[0][0] == ALICE and sp.stamped[0][2] == GID,
          "removing a member stamps their group app grant rows revoked", sp.stamped)

    # UNINSTALL ORDER. registry.uninstall deletes an app's peer_app_outbox
    # rows, and those rows are the only place a group grant's contract_id is
    # written down — so revocation has to happen BEFORE the delete, or the
    # contracts are stranded live with nothing able to name them.
    reg_src = open(os.path.join(_ROOT, "server", "applications", "registry.py"),
                   encoding="utf-8").read()
    i_revoke = reg_src.find("revoke_app_group_slips")
    i_delete = reg_src.find('for t in ("peer_app_slips", "peer_app_outbox")')
    check(i_revoke != -1 and i_delete != -1 and i_revoke < i_delete,
          "uninstall revokes an app's group slips BEFORE deleting the rows that name them",
          (i_revoke, i_delete))

    revoked.clear()

    class UninstallPool(RosterPool):
        async def fetch(self, sql, *a):
            if "FROM peer_app_outbox" in " ".join(sql.split()):
                return [{"cid": "slip-app-9"}]
            return await super().fetch(sql, *a)

    n_un = await LANE.revoke_app_group_slips(
        UninstallPool(group_id=GID, owner=OWNER, members=members), "veterans")
    check(n_un == 1 and ("contract.revoke", "slip-app-9") in revoked,
          "and that revocation reaches contract.revoke", revoked)

    # ── 6d. seq is per (group, voter), across ballots ───────────────────────
    print("\n6d. seq scope")
    b2 = {**ballot, "ballot_id": "second"}

    class TwoBallotPool(RosterPool):
        async def fetchrow(self, sql, *a):
            if "FROM group_ballots WHERE group_id" in " ".join(sql.split()):
                return dict(ballot) if a[1] == "name-it" else dict(b2)
            return await super().fetchrow(sql, *a)

    tb = TwoBallotPool(group_id=GID, owner=OWNER, members=members, ballot=ballot)
    v1 = await V.sign_vote(tb, ALICE, group_id=GID, ballot_id="name-it", choice="helios", seq=1)
    check((await LANE.accept_vote(tb, GID, "name-it", v1)).get("ok"), "ballot A accepts seq 1")
    check(await R.held_seq(tb, GID, ALICE) == 1, "the group-wide counter reads 1")

    v_bad = await V.sign_vote(tb, ALICE, group_id=GID, ballot_id="second", choice="helios", seq=1)
    r_bad = await LANE.accept_vote(tb, GID, "second", v_bad)
    check(not r_bad.get("ok") and "not ahead" in (r_bad.get("error") or ""),
          "a SECOND ballot will not take seq 1 again — the counter spans the group", r_bad)

    v2 = await V.sign_vote(tb, ALICE, group_id=GID, ballot_id="second", choice="vela", seq=2)
    check((await LANE.accept_vote(tb, GID, "second", v2)).get("ok"), "ballot B takes seq 2")
    v3 = await V.sign_vote(tb, ALICE, group_id=GID, ballot_id="name-it", choice="vela", seq=3)
    check((await LANE.accept_vote(tb, GID, "name-it", v3)).get("ok"),
          "and re-casting on ballot A at seq 3 supersedes")
    check(tb.votes[("name-it", ALICE)]["choice"] == "vela"
          and tb.votes[("second", ALICE)]["choice"] == "vela",
          "both ballots keep their own row", {k: v["choice"] for k, v in tb.votes.items()})

    # ── 8b. the member is TOLD when a vote is not counted ───────────────────
    print("\n8b. stored-not-counted is surfaced")
    from server.routes import groups as GR
    check(GR._vote_advice({"key_not_pinned": True}).get("advice") == V.NO_PINNED_KEY_ADVICE,
          "a vote stored without a pinned key comes back with advice, not a bare 200")
    check(GR._vote_advice({"key_not_pinned": True}).get("counted") is False,
          "and says plainly that it was not counted")
    check(GR._vote_advice({"ok": True, "verified": True}) == {}, "a counted vote carries no advice")
    check(GR._vote_advice(None) == {}, "and neither does a send that never reached the owner")
    check("sync" in V.NO_PINNED_KEY_ADVICE and "again" in V.NO_PINNED_KEY_ADVICE,
          "the advice names the fix — sync, then cast again")
    carol2 = await V.sign_vote(pool, CAROL, group_id=GID, ballot_id="name-it",
                               choice="vela", seq=99)
    rc2 = await LANE.accept_vote(pool, GID, "name-it", carol2)
    check(rc2.get("key_not_pinned") is True,
          "accept_vote reports it STRUCTURALLY, so no caller matches on prose", rc2)

    # THE REMOTE SEAM. When the voter is not the owner, that answer has to
    # survive peer_server's envelope and the outbox's bookkeeping to reach the
    # route. Exercised through the real `attempt`, with only the wire stubbed —
    # asserting _vote_advice in isolation would not have caught a break here.
    async def fake_send(pool_, row_):
        # peer_server wraps a handler's result as {"ok": True, "data": ...}
        # (peer_server.py:196), and attempt() keeps `data` as `response`.
        return {"ok": True, "data": {"ok": True, "stored": True, "verified": False,
                                     "key_not_pinned": True,
                                     "reason": V.NO_PINNED_KEY}}, None

    _real_send = LANE._send
    LANE._send = fake_send
    try:
        seam = RosterPool(group_id=GID, owner=ALICE, members=members, ballot=ballot)
        out_remote = await LANE.attempt(seam, {"id": 1, "group_id": GID,
                                               "peer_entity_id": ALICE,
                                               "op": LANE.OP_VOTE, "payload": {},
                                               "attempts": 0})
    finally:
        LANE._send = _real_send
    check(out_remote.get("status") == "delivered",
          "a vote the owner stored-but-did-not-count still DELIVERED", out_remote.get("status"))
    check(isinstance(out_remote.get("response"), dict)
          and out_remote["response"].get("key_not_pinned") is True,
          "and the owner's answer survives the envelope and the outbox", out_remote.get("response"))
    check(GR._vote_advice(out_remote.get("response")).get("advice") == V.NO_PINNED_KEY_ADVICE,
          "so the route turns the REMOTE answer into the same advice as a local one")

    # ── 7. the replica replaces, it does not merge ──────────────────────────
    print("\n7. roster replication")
    rpool = RosterPool(group_id=GID, owner=ALICE, members=[
        {"member_entity_id": ALICE, "role": "creator", "verify_pubkey": keys[ALICE][1],
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": _now()},
        {"member_entity_id": BOB, "role": "member", "verify_pubkey": keys[BOB][1],
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": _now()},
        {"member_entity_id": OWNER, "role": "member", "verify_pubkey": keys[OWNER][1],
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": _now()},
    ])
    rpool.group["synced_epoch"] = 7
    snap = [R.snapshot_member(m) for m in rpool._member_rows()
            if m["member_entity_id"] != BOB]
    out = await R.apply_roster_snapshot(rpool, group_id=GID, owner_entity_id=ALICE,
                                        name="builders", description="",
                                        roster_epoch=9, members=snap)
    check(out.get("applied"), "a newer snapshot applies", out)
    check(rpool.members[BOB].get("removed_at") is not None,
          "a member the snapshot OMITS is stamped removed — a merge would drop the one change that matters")
    check(rpool.members[OWNER].get("removed_at") is None, "a member it mentions stays")

    older = await R.apply_roster_snapshot(rpool, group_id=GID, owner_entity_id=ALICE,
                                          name="builders", description="",
                                          roster_epoch=8, members=snap)
    check(not older.get("applied") and "older" in (older.get("reason") or ""),
          "an out-of-order answer never walks a replica backwards", older)

    try:
        await R.apply_roster_snapshot(rpool, group_id=GID, owner_entity_id=999,
                                      name="x", description="", roster_epoch=99, members=[])
        check(False, "a snapshot claiming a DIFFERENT owner for a held group is refused")
    except R.RosterError as e:
        check(e.status == 409, "a snapshot claiming a DIFFERENT owner for a held group is refused", e.detail)

    check(R.snapshot_member({"member_entity_id": 1, "role": "member", "display_name": "",
                             "verify_pubkey": keys[ALICE][1], "removed_at": None})["verify_pubkey"]
          == base64.b64encode(keys[ALICE][1]).decode(),
          "a key travels base64 — the frame is JSON")

    # ── 8. first key wins ───────────────────────────────────────────────────
    print("\n8. key pinning")
    kpool = RosterPool(group_id=GID, owner=OWNER, members=[
        {"member_entity_id": CAROL, "role": "member", "verify_pubkey": None,
         "display_name": "", "added_at": _now(), "removed_at": None, "key_pinned_at": None}])
    changed1, why1 = await R.pin_key(kpool, GID, CAROL, keys[CAROL][1])
    check(changed1 and why1 == "pinned", "an unpinned member's key is pinned", why1)
    changed2, why2 = await R.pin_key(kpool, GID, CAROL, keys[CAROL][1])
    check(not changed2 and why2 == "already pinned", "the same key again is a no-op", why2)
    changed3, why3 = await R.pin_key(kpool, GID, CAROL, keys[BOB][1])
    check(not changed3 and "never an overwrite" in why3,
          "a DIFFERENT key is refused, not overwritten — otherwise every past vote could be retro-invalidated",
          why3)
    check(bytes(kpool.members[CAROL]["verify_pubkey"]) == keys[CAROL][1],
          "and the original pin still stands")
    bad_len, why4 = await R.pin_key(kpool, GID, CAROL, b"short")
    check(not bad_len and "32 raw bytes" in why4, "a key that is not 32 bytes is refused", why4)
    nomember, why5 = await R.pin_key(kpool, GID, 777, keys[BOB][1])
    check(not nomember and "not a member" in why5, "a non-member cannot pin a key", why5)

    # ── 9. wiring ───────────────────────────────────────────────────────────
    print("\n9. wiring")
    from server.applications import build_contract as BC
    from server.applications import scopes as SC
    from brain.mastercode.runtime import kernel_ops as KO
    from brain.mastercode.runtime import meta_tool_caps as MTC

    for s in ("read:group", "write:group"):
        check(s in SC.PERMISSIONS, f"{s} is in the declarable permission catalog")
        check(SC.validate([s]) == [s], f"{s} passes scope validation")
        check(s in BC.COMMUNITY_ALLOWED_SCOPES, f"a community app may declare {s}")
    check(MTC.ENTITY_SCOPE_TO_BITS.get("read:group") == 2,
          "read:group sits at READ_MEMORY, beside read:circles and read:peer_app",
          MTC.ENTITY_SCOPE_TO_BITS.get("read:group"))
    check(MTC.ENTITY_SCOPE_TO_BITS.get("write:group") == 8,
          "write:group sits at WRITE_MEMORY, beside write:circles and write:peer_app",
          MTC.ENTITY_SCOPE_TO_BITS.get("write:group"))
    check("/api/groups" in KO._EXPOSED_PREFIXES,
          "the /api/groups area is kernel_op-exposed, so the host can reach its own groups")
    check(not any(p.startswith("/api/groups") for p in KO._WALLED_PATHS),
          "and nothing under it is walled — no route here returns credential bytes")

    gen = open(os.path.join(_ROOT, "scripts", "generate_capabilities.py"), encoding="utf-8").read()
    check('"/api/groups": []' in gen,
          "the parity generator classifies the area, so it never reads as unreviewed")

    mig = open(os.path.join(_ROOT, "migrations", "v154_groups_roster_votes.sql"), encoding="utf-8").read()
    for tbl in ("groups", "group_members", "group_ballots", "group_votes",
                "group_slips", "group_outbox", "group_app_grants"):
        check(f"CREATE TABLE IF NOT EXISTS {tbl}" in mig, f"v154 declares {tbl}")
    # A real DROP statement, not the word in prose. Matched at the start of a
    # statement so "silently drop one the member can present" in a comment does
    # not read as destructive DDL.
    check(not re.search(r"(?mi)^\s*DROP\s+(TABLE|INDEX|COLUMN|CONSTRAINT)\b", mig),
          "and drops nothing — the migration is forward-only")
    check("circle_whitelist" in mig and "NOT MIGRATED" in mig,
          "and states that existing platform-held circle rosters are not migrated")

    # Every table the code writes to must be declared. Derived from the source,
    # not a hand-kept list, so a table added later without DDL fails here.
    src = "\n".join(
        open(os.path.join(_ROOT, p), encoding="utf-8").read()
        for p in ("server/groups/roster.py", "server/groups/lane.py"))
    written = set(re.findall(r"(?:INSERT INTO|UPDATE)\s+(group[a-z_]*)", src))
    missing = sorted(t for t in written if f"CREATE TABLE IF NOT EXISTS {t}" not in mig)
    check(not missing, "every group table the code writes to is declared in v154", missing)

    print(f"\n{CHECKS} checks, {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  FAILED: " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
