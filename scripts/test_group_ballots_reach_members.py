#!/usr/bin/env python3
"""A member can vote on its owner's ballot, more than once, in a big group.

THE BUG (measured by the end-to-end peer-group test, entity 9163 owner, 164
member): the owner opened a ballot, 164 synced, and 164's vote answered 404
"no such ballot". cast_vote looks the ballot up in the MEMBER's own pod, and
nothing ever put it there — group.roster_fetch carried members only.

Two more defects on the same path, found reading it:
  * a member's vote seq came from group_votes, which only the OWNER writes, so
    every member vote was signed seq 1 and the owner refused each one after the
    first as a replay — a change of mind, or a vote on a second ballot;
  * the peer client read answers with asyncio's 64 KiB line limit, and a full
    500-member roster page is ~75 KB, so a big group could never sync.

Pinned here, through the real functions:
  1. the owner's roster fetch carries its open ballots, and not a closed one;
  2. a member's sync stores them, and the member's vote route then finds one;
  3. an owner on an older image (no `ballots` key) stores nothing and breaks
     nothing; a stale snapshot's ballots are not applied; a malformed ballot is
     skipped;
  4. a member's second vote in a group is signed past its first, and a pod
     that lost its outbox still signs past what the owner holds;
  5. group.grant is refused from anyone but the owner the group id names,
     so a member cannot squat an id on a contact or take over a held group;
  6. a member's tally route refuses instead of reporting a complete zero;
  7. send_to_peer reads a 1 MB answer line.

Container-free: fake pools that answer the SQL the code writes. No DB, no pod.
Run: APP_ROOT=<repo> python3 scripts/test_group_ballots_reach_members.py
"""
import asyncio
import datetime as _dt
import json
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


class Pool:
    """One host's group tables in dicts, answering on the statements the
    production code writes — a statement that changes shape stops being
    answered here instead of quietly passing."""

    def __init__(self):
        self.groups, self.members, self.ballots, self.votes, self.outbox = {}, {}, {}, [], []

    async def fetchrow(self, sql, *a):
        s = " ".join(sql.split())
        if "FROM groups WHERE group_id" in s:
            g = self.groups.get(a[0])
            return dict(g) if g else None
        if "SELECT verify_pubkey FROM group_members" in s:
            m = self.members.get((a[0], int(a[1])))
            return {"verify_pubkey": m.get("verify_pubkey")} if m else None
        if "FROM group_ballots WHERE group_id = $1 AND ballot_id = $2" in s:
            b = self.ballots.get((a[0], a[1]))
            return dict(b) if b else None
        return None

    async def fetchval(self, sql, *a):
        s = " ".join(sql.split())
        if "SELECT 1 FROM group_members" in s and "removed_at IS NULL" in s:
            m = self.members.get((a[0], int(a[1])))
            return 1 if (m and m.get("removed_at") is None) else None
        if "SELECT max(seq) FROM group_votes" in s:
            seqs = [v["seq"] for v in self.votes if v["group_id"] == a[0] and v["voter"] == int(a[1])]
            return max(seqs) if seqs else None
        if "FROM group_outbox" in s and "payload->'vote'->'terms'->>'seq'" in s:
            seqs = [int(r["payload"]["vote"]["terms"]["seq"]) for r in self.outbox
                    if r["group_id"] == a[0] and r["op"] == a[1]
                    and str(r["payload"]["vote"]["terms"]["voter"]) == a[2]]
            return max(seqs) if seqs else None
        return None

    async def fetch(self, sql, *a):
        s = " ".join(sql.split())
        if "FROM group_members WHERE group_id = $1" in s:
            return [dict(m) for (g, _), m in sorted(self.members.items()) if g == a[0]]
        if "FROM group_ballots WHERE group_id = $1 AND closed_at IS NULL AND closes_at > now()" in s:
            rows = [b for (g, _), b in self.ballots.items()
                    if g == a[0] and b["closed_at"] is None and b["closes_at"] > _now()]
            return sorted(rows, key=lambda b: b["created_at"], reverse=True)
        return []

    async def execute(self, sql, *a):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO groups"):
            self.groups[a[0]] = {"group_id": a[0], "owner_entity_id": a[1], "name": a[2],
                                 "description": a[3], "roster_epoch": a[4],
                                 "synced_epoch": a[4], "revoked_at": None}
        elif s.startswith("INSERT INTO group_members"):
            self.members[(a[0], int(a[1]))] = {"member_entity_id": int(a[1]), "role": a[2],
                                               "verify_pubkey": a[3], "display_name": a[4],
                                               "removed_at": a[5], "key_pinned_at": None,
                                               "added_at": _now()}
        elif s.startswith("UPDATE group_members SET removed_at = now()"):
            for (g, mid), m in self.members.items():
                if g == a[0] and mid not in a[1] and m.get("removed_at") is None:
                    m["removed_at"] = _now()
        elif s.startswith("INSERT INTO group_ballots"):
            key = (a[0], a[1])
            prev = self.ballots.get(key, {})
            self.ballots[key] = {"group_id": a[0], "ballot_id": a[1], "question": a[2],
                                 "choices": a[3], "target_kind": a[4], "target_ref": a[5],
                                 "created_by": a[6], "opens_at": a[7], "closes_at": a[8],
                                 "closed_at": prev.get("closed_at"),
                                 "created_at": prev.get("created_at", _now())}
        return "OK"


GID = "grp-163-" + "0" * 32
OWNER, MEMBER = 163, 164


def owner_pool():
    p = Pool()
    p.groups[GID] = {"group_id": GID, "owner_entity_id": OWNER, "name": "ballot-test",
                     "description": "", "roster_epoch": 3, "synced_epoch": None, "revoked_at": None}
    for mid, role in ((OWNER, "creator"), (MEMBER, "member")):
        p.members[(GID, mid)] = {"member_entity_id": mid, "role": role, "verify_pubkey": None,
                                 "display_name": "", "removed_at": None, "key_pinned_at": None,
                                 "added_at": _now()}
    t = _now()
    p.ballots[(GID, "open-one")] = {
        "group_id": GID, "ballot_id": "open-one", "question": "Ship it?",
        "choices": json.dumps(["yes", "no"]), "target_kind": "", "target_ref": "",
        "created_by": OWNER, "opens_at": t, "closes_at": t + _dt.timedelta(days=1),
        "closed_at": None, "created_at": t}
    p.ballots[(GID, "long-gone")] = {
        "group_id": GID, "ballot_id": "long-gone", "question": "Old?",
        "choices": json.dumps(["a", "b"]), "target_kind": "", "target_ref": "",
        "created_by": OWNER, "opens_at": t - _dt.timedelta(days=9),
        "closes_at": t - _dt.timedelta(days=2), "closed_at": None,
        "created_at": t - _dt.timedelta(days=9)}
    return p


async def main() -> int:
    from server import brain_service
    from server.groups import lane as LANE
    from server.groups import roster as R
    from server.network import nac_stepup
    from server.peer import peer_client as PC

    OWN = owner_pool()
    MEM = Pool()
    strip_ballots = {"on": False}

    async def fake_send(peer, op, slip, params, **kw):
        # The member dials the owner: run the OWNER's real handler as the owner.
        assert op == LANE.OP_ROSTER_FETCH and peer == OWNER
        was = brain_service._entity_id, brain_service.get_pool
        brain_service._entity_id, brain_service.get_pool = OWNER, (lambda: OWN)
        try:
            data = await LANE.group_roster_fetch(MEMBER, {}, params)
        finally:
            brain_service._entity_id, brain_service.get_pool = was
        if strip_ballots["on"]:
            data.pop("ballots", None)
        return {"ok": True, "data": data}

    async def fake_slip_for(pool, gid):
        return {"terms": {}}, OWNER

    async def no_key(pool, eid):
        raise RuntimeError("no key in this test")

    real_send = PC.send_to_peer
    PC.send_to_peer = fake_send
    LANE.group_slip_for = fake_slip_for
    nac_stepup.entity_verify_pubkey = no_key
    brain_service._entity_id = MEMBER

    print("1. the owner's roster fetch carries its open ballots")
    brain_service._entity_id, brain_service.get_pool = OWNER, (lambda: OWN)
    head = await LANE.group_roster_fetch(MEMBER, {}, {"group_id": GID, "offset": 0,
                                                      "with_ballots": True})
    later = await LANE.group_roster_fetch(MEMBER, {}, {"group_id": GID, "offset": 1,
                                                       "with_ballots": True})
    unasked = await LANE.group_roster_fetch(MEMBER, {}, {"group_id": GID, "offset": 0})
    brain_service._entity_id = MEMBER
    ids = [b["ballot_id"] for b in head.get("ballots") or []]
    check(ids == ["open-one"], "first page carries the open ballot and not the closed one", ids)
    check(head["ballots"][0]["choices"] == ["yes", "no"], "choices travel as a list", head.get("ballots"))
    check("ballots" not in later, "a later page does not repeat them", later.keys())
    check("ballots" not in unasked,
          "a fetch that did not ask (an older member, 64 KiB reader) gets none", unasked.keys())
    t = _now()
    OWN.ballots[(GID, "huge")] = dict(OWN.ballots[(GID, "open-one")], ballot_id="huge",
                                      question="q" * (R.BALLOT_SNAPSHOT_BYTES + 1),
                                      created_at=t + _dt.timedelta(seconds=1))
    brain_service._entity_id = OWNER
    snaps = [b["ballot_id"] for b in await R.open_ballot_snapshots(OWN, GID)]
    brain_service._entity_id = MEMBER
    del OWN.ballots[(GID, "huge")]
    check(snaps == ["open-one"], "an oversized newest ballot is skipped, the older one still travels",
          snaps)

    print("2. a member's sync stores them, and its vote route finds one")
    check(await R.get_ballot(MEM, GID, "open-one") is None, "before sync the member has no ballot (the 404)")
    out = await LANE.sync_group(MEM, GID)
    check(out.get("applied") and out.get("ballots") == 1, "sync applies roster and one ballot", out)
    b = await R.get_ballot(MEM, GID, "open-one")
    check(b is not None and b["choices"] == ["yes", "no"] and b["created_by"] == OWNER,
          "the member's replica holds the owner's ballot", b)
    check(await R.get_ballot(MEM, GID, "long-gone") is None, "the closed ballot did not travel")

    print("3. older owner, stale snapshot, malformed ballot")
    MEM2 = Pool()
    strip_ballots["on"] = True
    out2 = await LANE.sync_group(MEM2, GID)
    strip_ballots["on"] = False
    check(out2.get("applied") and "ballots" not in out2 and not MEM2.ballots,
          "an owner that sends no ballots key: roster applies, nothing else stored", out2)
    MEM.groups[GID]["synced_epoch"] = 99
    OWN.ballots[(GID, "newer")] = dict(OWN.ballots[(GID, "open-one")], ballot_id="newer")
    out3 = await LANE.sync_group(MEM, GID)
    check(out3.get("applied") is False and (GID, "newer") not in MEM.ballots,
          "a snapshot older than the replica applies no ballots either", out3)
    n = await R.apply_ballot_snapshot(MEM2, group_id=GID, owner_entity_id=OWNER, ballots=[
        {"ballot_id": "Bad Id", "question": "q", "choices": ["a", "b"], "closes_at": _now().isoformat()},
        {"ballot_id": "one-choice", "question": "q", "choices": ["a"], "closes_at": _now().isoformat()},
        {"ballot_id": "no-close", "question": "q", "choices": ["a", "b"]},
        {"ballot_id": "blank", "question": "  ", "choices": ["a", "b"], "closes_at": _now().isoformat()},
        "not-a-dict",
        {"ballot_id": "fine", "question": "q", "choices": ["a", "b"], "closes_at": _now().isoformat()}])
    check(n == 1 and list(MEM2.ballots) == [(GID, "fine")], "only the well-formed ballot is stored",
          list(MEM2.ballots))

    print("4. a member's second vote is signed past its first")
    import time
    before = time.time_ns() // 1000
    s1 = await LANE.next_vote_seq(MEM, GID, MEMBER)
    MEM.outbox.append({"group_id": GID, "op": LANE.OP_VOTE,
                       "payload": {"ballot_id": "open-one",
                                   "vote": {"terms": {"voter": MEMBER, "seq": s1}}}})
    s2 = await LANE.next_vote_seq(MEM, GID, MEMBER)
    check(s1 >= before and s2 > s1, "the second vote is signed past the first (was 1 then 1)", (s1, s2))
    far = s2 + 10**9
    MEM.outbox.append({"group_id": GID, "op": LANE.OP_VOTE,
                       "payload": {"vote": {"terms": {"voter": MEMBER, "seq": far}}}})
    check(await LANE.next_vote_seq(MEM, GID, MEMBER) == far + 1,
          "a sent seq ahead of the clock is still passed")
    MEM.outbox.append({"group_id": "other", "op": LANE.OP_VOTE,
                       "payload": {"vote": {"terms": {"voter": MEMBER, "seq": far * 2}}}})
    check(await LANE.next_vote_seq(MEM, GID, MEMBER) == far + 1, "another group's votes do not move it")
    OWN.votes.append({"group_id": GID, "voter": OWNER, "seq": far * 3})
    check(await LANE.next_vote_seq(OWN, GID, OWNER) == far * 3 + 1,
          "the owner still counts from group_votes")
    restored = Pool()      # a member pod restored with no outbox, owner holding seq 7
    check(await LANE.next_vote_seq(restored, GID, MEMBER) > 7,
          "a pod that lost its outbox still signs past the small seqs the owner holds")
    import inspect
    from server.routes import groups as G
    src = inspect.getsource(G.cast_vote)
    check("next_vote_seq" in src and "held_seq" not in src, "cast_vote signs with next_vote_seq")

    print("5. a group id belongs to one owner: group.grant cannot take one over")
    ATTACKER = 999
    grant = {"group_id": GID, "name": "pwned", "roster_epoch": 9223372036854775807,
             "group_slip": {"terms": {"issuer": ATTACKER, "grantee": None,
                                      "allowed_ops": [LANE.group_action(GID)]}}}
    for who, pool, label in ((OWNER, OWN, "the owner's own group"),
                             (MEMBER, MEM, "a member's replica")):
        grant["group_slip"]["terms"]["grantee"] = who
        brain_service._entity_id, brain_service.get_pool = who, (lambda p=pool: p)
        before = dict(pool.groups[GID])
        r = await LANE.group_grant(ATTACKER, {}, grant)
        check(r.get("ok") is False and "different owner" in (r.get("error") or ""),
              f"a stranger's grant is refused on {label}", r)
        check(pool.groups[GID] == before, f"...and {label} is untouched")
    # The squat: a member of G grants G to a host that holds NOTHING for it
    # yet. The id names G's owner, so the first grant no longer wins.
    fresh = Pool()
    grant["group_slip"]["terms"]["grantee"] = MEMBER
    brain_service._entity_id, brain_service.get_pool = MEMBER, (lambda: fresh)
    r = await LANE.group_grant(ATTACKER, {}, grant)
    check(r.get("ok") is False and not fresh.groups,
          "a grant from anyone but the owner the id names stores nothing on a fresh host", r)
    check(R.new_group_id(OWNER).startswith(f"grp-{OWNER}-")
          and R.group_id_owner(R.new_group_id(OWNER)) == OWNER
          and R.group_id_owner("grp-" + "0" * 32) is None
          and R.group_id_owner("grp-0-" + "0" * 32) is None,
          "a new group id names its owner, and only that shape is a group id")
    src = inspect.getsource(LANE)
    check("WHERE groups.owner_entity_id = EXCLUDED.owner_entity_id" in src
          and "WHERE group_slips.owner_entity_id = EXCLUDED.owner_entity_id" in src,
          "both upserts carry the owner guard in SQL too")
    brain_service._entity_id, brain_service.get_pool = MEMBER, (lambda: MEM)

    # An owner inviting a member still on the OLD image gets "group_id missing
    # or malformed" back for a new-shape id. That changes when the member
    # upgrades, so the invite waits in the outbox instead of being refused.
    class Rec:
        def __init__(self):
            self.sql = []

        async def execute(self, sql, *a):
            self.sql.append(" ".join(sql.split()))
            return "UPDATE 1"

    real_lane_send = LANE._send
    for op, err, want in ((LANE.OP_GRANT, "group_id missing or malformed", "status='queued'"),
                          (LANE.OP_GRANT, "group_slip grantee is not this host", "status='refused'"),
                          (LANE.OP_VOTE, "group_id missing or malformed", "status='refused'")):
        async def fake_lane_send(pool, row, _e=err):
            return {"ok": False, "receiver": True, "retryable": False, "error": _e}, None
        LANE._send = fake_lane_send
        rec = Rec()
        await LANE.attempt(rec, {"id": 1, "op": op, "attempts": 0})
        check(any(want in q for q in rec.sql),
              f"{op} answered {err!r} is recorded {want.split('=')[1]}", rec.sql)
    LANE._send = real_lane_send

    print("6. a member's tally says where the count lives")
    G._pool = lambda: MEM
    try:
        await G.tally(GID, "open-one")
        check(False, "a member's tally is refused, not a complete count of zero")
    except Exception as e:  # noqa: BLE001
        d = getattr(e, "detail", None)
        check(getattr(e, "status_code", None) == 409 and isinstance(d, dict)
              and d.get("owner_entity_id") == OWNER,
              "a member's tally is refused, not a complete count of zero", repr(e))

    print("7. the peer client reads an answer past 64 KiB")
    big = json.dumps({"ok": True, "data": {"pad": "x" * 1_000_000}}) + "\n"

    async def serve(reader, writer):
        await reader.readline()
        writer.write(big.encode())
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    real_open = asyncio.open_connection

    async def plain_open(host, p, ssl=None, server_hostname=None, **kw):
        return await real_open("127.0.0.1", port, **kw)

    # The real send_to_peer, with only the TLS socket swapped for a plain one:
    # the limit it passes to open_connection is what is under test.
    PC.asyncio = types.SimpleNamespace(open_connection=plain_open, wait_for=asyncio.wait_for)
    PC._ctx = lambda: None
    try:
        r = await real_send(MEMBER, "group.roster_fetch", None, {})
        check(len(r["data"]["pad"]) == 1_000_000, "a 1 MB answer line is read whole")
    except Exception as e:  # noqa: BLE001
        check(False, "a 1 MB answer line is read whole", f"{type(e).__name__}: {e}")
    srv.close()

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ok  ballots reach members, member seqs advance, big answers are readable")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
