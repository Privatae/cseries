#!/usr/bin/env python3
"""Group JOIN REQUESTS: a stranger's knock, and the two acts of an accept —
server/groups/join.py, server/peer/handlers.py,
migrations/v155_group_join_and_rendezvous.sql.

What is pinned here, and why each one:

  1. THE KIND EXISTS AT BOTH ENDS OF THE WIRE. `group_join` is an admitted
     request kind and `group_id` is an allow-listed payload key, because a
     knock whose kind is unknown is refused outright and a payload key that is
     not allow-listed is dropped — either one silently turns the whole feature
     into a card with no group on it.
  2. A STRANGER MAY ASK. `group_join` is deliberately NOT in
     _KINDS_NEEDING_A_CONTACT: an accepted contact can already be added by the
     owner, so requiring one first would make the op pointless. This is the
     one property most likely to be "tidied" into consistency with
     machine_request, so it is asserted as an absence.
  3. THE RECEIVER'S GATE, case by case. _group_join_target refuses a malformed
     id, a group this host does not own, a group that is closed, and a peer who
     is already a current member — and admits a group this host owns. THE
     REFUSAL SENTENCE FOR "no such group" AND FOR "not ours" IS ASSERTED
     BYTE-IDENTICAL: the difference between those two answers is exactly what a
     prober would enumerate group ids for, so the sameness is the security
     property and not a coincidence of phrasing.
  4. THE KNOCK SLOT NARROWS BY GROUP. _knock_slot_free adds a
     `payload->>'group_id'` predicate and binds the group when one is passed,
     and does not when it is not. Asserted on the SQL the function actually
     issues (the fake pool records statement and args), because "one slot per
     peer per kind" would make a second, legitimate ask for a DIFFERENT circle
     read back as "already waiting".
  5. DENY WRITES NOTHING BUT THE ARCHIVE. No contact, no roster row, no
     domain_policy allow, no outbox row — asserted over every statement the
     function issued, not over its return value. A denial that leaves a trace
     is a denial that answers a stranger.
  6. ACCEPT MAKES THE CONTACT FIRST, THE ROSTER ROW SECOND, AND ENQUEUES THE
     GRANT ONLY FOR A MEMBER WHO WAS NEWLY ADDED. The order is the whole design
     (the group grant rides a message slip only a contact relationship
     produces), so it is asserted from the recorded call SEQUENCE rather than
     from the answer. The normal-case shape is pinned with it:
     contact_grant_delivered False and an invite left `queued` — which is what
     the Circles card has to read as "invited, waiting for their host".
  7. THE MESSAGE_KIND VOCABULARY IS IN BOTH SCHEMAS. 'machine_request',
     'group_join' and 'rendezvous_intro' appear in the CHECK constraint in
     v155 AND in init-db.sql. A card whose kind the constraint refuses cannot
     be stored at all — the INSERT raises, the knock comes back refused — and
     a fresh host built from init-db.sql alone never runs the migration, so
     the two files drifting apart is a shipped feature that works nowhere new.
     Same shape as test_error_lane.py:85.
  8. "THEY SAID NO" AND "THEY WERE ASLEEP" ARE DIFFERENT STATES. ask_to_join
     records status='refused' when the far side ANSWERS {ok: False} and
     status='unreachable' when the transport fails. Conflating them costs the
     user the only thing this row exists to tell them.

Container-free: real functions against a recording fake pool. No DB, no
container, no network.

Run:  python3 scripts/test_group_join_requests.py
"""
import asyncio
import datetime as _dt
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


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class JoinPool:
    """A fake pool holding one group, its roster and the inbox cards knocking
    at it, and recording every statement with its arguments. It answers by
    matching on the SQL the production code actually writes, so a query that
    changes shape stops being answered instead of quietly passing — and the
    recorded arguments are what the SQL-shape checks below read."""

    def __init__(self, *, group_id, owner, members=(), cards=(), group=True,
                 revoked=False):
        self.group = {
            "group_id": group_id, "owner_entity_id": owner, "name": "builders",
            "description": "", "roster_epoch": 7, "synced_epoch": None,
            "synced_at": None, "created_at": _now(), "updated_at": _now(),
            "revoked_at": _now() if revoked else None,
        } if group else None
        self.members = {int(m["member_entity_id"]): dict(m) for m in members}
        self.cards = [dict(c) for c in cards]
        self.execs = []
        #: The ordered trace of the writes that MATTER — what check 6 reads.
        self.seq = []
        self.outbox = []
        self.joins = []
        #: What a knock-slot count returns. 0 = the slot is free.
        self.slot_count = 0

    def sql_seen(self, needle):
        return [(s, a) for s, a in self.execs if needle in " ".join(s.split())]

    async def fetchrow(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM groups WHERE group_id" in s:
            if self.group and a[0] == self.group["group_id"]:
                return dict(self.group)
            return None
        if "SELECT role, removed_at FROM group_members" in s:
            m = self.members.get(int(a[1]))
            return {"role": m["role"], "removed_at": m.get("removed_at")} if m else None
        if "INSERT INTO group_outbox" in s:
            self.seq.append("outbox")
            row = {"id": len(self.outbox) + 1, "status": "queued",
                   "message_id": a[4], "attempts": 0}
            self.outbox.append({**row, "group_id": a[0], "peer_entity_id": a[1],
                                "op": a[2]})
            return row
        return None

    async def fetchval(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "SELECT 1 FROM group_members" in s and "removed_at IS NULL" in s:
            m = self.members.get(int(a[1]))
            return 1 if (m and m.get("removed_at") is None) else None
        if "roster_epoch = roster_epoch + 1" in s:
            self.group["roster_epoch"] += 1
            return self.group["roster_epoch"]
        if "count(*) FROM inbox_messages" in s:
            return self.slot_count
        return None

    async def fetch(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM inbox_messages" in s:
            # Honour the real predicate: this kind, unarchived, this group.
            return [c for c in self.cards
                    if c.get("message_kind") == a[0]
                    and (c.get("payload") or {}).get("group_id") == a[1]]
        return []

    async def execute(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO group_members" in s:
            self.seq.append("roster")
            mid = int(a[1])
            cur = self.members.setdefault(mid, {"member_entity_id": mid})
            cur["role"] = a[2]
            cur["removed_at"] = None
            return "INSERT 0 1"
        if "INSERT INTO group_join_requests" in s:
            self.joins.append(a)
            return "INSERT 0 1"
        return "OK"


async def main() -> int:
    from server import brain_service
    from server.bridge import memory_handlers as MH
    from server.groups import join as J
    from server.groups import roster as R
    from server.peer import handlers as H
    from server.peer import peer_client as PC
    from server.routes import contacts as C

    GID = R.new_group_id(9003)
    OTHER_GID = R.new_group_id(9003)
    OWNER, ALICE, BOB = 9003, 9122, 9123
    brain_service._entity_id = OWNER

    # ── 1. the kind exists at both ends of the wire ─────────────────────────
    print("\n1. the kind is admitted, and its one key is allow-listed")
    check("group_join" in H.REQUEST_KINDS,
          "group_join is an admitted request kind — an unknown kind is refused outright",
          H.REQUEST_KINDS)
    check(J.KIND == "group_join",
          "and join.py names the same string, imported rather than typed twice", J.KIND)
    check("group_id" in H._ALLOWED_PAYLOAD_KEYS,
          "group_id survives the payload allow-list — everything else the sender writes is dropped",
          H._ALLOWED_PAYLOAD_KEYS)
    check(H._DEFAULT_SUBJECT.get("group_join") == "Request to join a group",
          "the card has its own headline — 'Connection request' over a join request is the wrong question",
          H._DEFAULT_SUBJECT.get("group_join"))

    # ── 2. a stranger may ask ───────────────────────────────────────────────
    print("\n2. a stranger may ask")
    check("group_join" not in H._KINDS_NEEDING_A_CONTACT,
          "group_join needs NO accepted contact — an accepted contact can already be added by the owner",
          H._KINDS_NEEDING_A_CONTACT)
    check("machine_request" in H._KINDS_NEEDING_A_CONTACT,
          "while machine_request still does, so the absence above is a decision and not an empty tuple",
          H._KINDS_NEEDING_A_CONTACT)

    # ── 3. the receiver's gate ──────────────────────────────────────────────
    print("\n3. the gate on the knock")
    owned = JoinPool(group_id=GID, owner=OWNER)
    brain_service._pool = owned

    gid_ok, refusal = await H._group_join_target(ALICE, {"group_id": GID})
    check(gid_ok == GID and refusal is None,
          "a group this host owns is admitted, and the gate returns its id", (gid_ok, refusal))

    for bad in ("nope", "", None, 7, GID.upper()):
        _g, ref = await H._group_join_target(ALICE, {"group_id": bad})
        check(_g is None and ref is not None and ref.get("ok") is False,
              f"a malformed group id is refused ({bad!r})", ref)
    _g, malformed = await H._group_join_target(ALICE, {})
    check(_g is None and malformed.get("error") == "a join request must name a group",
          "and a knock naming no group at all is refused the same way", malformed)

    # THE ENUMERATION ORACLE. These two must be the same sentence, byte for
    # byte: a caller who can tell "there is no such group" from "that group is
    # not ours" can enumerate which group ids exist on this host.
    _g, no_such = await H._group_join_target(ALICE, {"group_id": OTHER_GID})
    brain_service._pool = JoinPool(group_id=GID, owner=BOB)
    _g, not_ours = await H._group_join_target(ALICE, {"group_id": GID})
    check(no_such.get("error") == not_ours.get("error"),
          "'no such group' and 'not ours' are the SAME sentence — the enumeration-oracle property",
          (no_such.get("error"), not_ours.get("error")))
    check(no_such.get("error") == "this host does not host that group",
          "and that sentence names neither case", no_such.get("error"))

    # A CLOSED GROUP GETS THE SAME SENTENCE TOO. An earlier cut answered "that
    # group is closed", which is friendlier and wrong: it confirms both that the
    # id exists AND that this host owns it, which is the pair the shared
    # sentence above exists to withhold.
    brain_service._pool = JoinPool(group_id=GID, owner=OWNER, revoked=True)
    _g, closed = await H._group_join_target(ALICE, {"group_id": GID})
    check(_g is None and closed.get("error") == no_such.get("error"),
          "a CLOSED group gets the same sentence as an unknown one — saying "
          "'closed' would confirm the id exists and that we own it",
          closed)

    brain_service._pool = JoinPool(
        group_id=GID, owner=OWNER,
        members=[{"member_entity_id": ALICE, "role": "member", "removed_at": None}])
    _g, already = await H._group_join_target(ALICE, {"group_id": GID})
    check(_g is None and "already a member" in (already.get("error") or ""),
          "a peer who is already a current member is told plainly — that answer "
          "is reachable only by a host already on the roster, which knows the "
          "group exists because it is in it", already)

    brain_service._pool = None
    _g, cold = await H._group_join_target(ALICE, {"group_id": GID})
    check(_g is None and cold.get("retryable") is True,
          "the gate FAILS CLOSED on a cold pool, and says the refusal is retryable", cold)

    # ── 4. the knock slot narrows BY GROUP ──────────────────────────────────
    print("\n4. the knock slot is per (peer, group)")
    slots = JoinPool(group_id=GID, owner=OWNER)
    brain_service._pool = slots

    free = await H._knock_slot_free(ALICE, "group_join", group_id=GID)
    check(free, "an unoccupied slot is free")
    sql, args = slots.execs[-1]
    flat = " ".join(sql.split())
    check("payload->>'group_id'" in flat,
          "the count carries a payload->>'group_id' predicate when a group is named", flat)
    check(GID in args,
          "and BINDS the group, so the narrowing is a bound parameter and not a literal", args)

    slots.slot_count = 1
    taken = await H._knock_slot_free(ALICE, "group_join", group_id=GID)
    check(not taken, "a live card for that group holds the slot")

    slots.slot_count = 0
    await H._knock_slot_free(ALICE, "endpoint_request")
    sql2, args2 = slots.execs[-1]
    flat2 = " ".join(sql2.split())
    check("payload->>'group_id'" not in flat2,
          "with no group the predicate is ABSENT — the other kinds keep one slot per peer", flat2)
    check(len(args2) == 3 and GID not in args2,
          "and nothing binds a group", args2)

    # ── 5. deny writes nothing but the archive ──────────────────────────────
    print("\n5. deny")
    archived = []

    async def fake_archive(params):
        archived.append(params.get("message_id"))
        return {"ok": True}

    MH.handle_inbox_archive = fake_archive

    card = {"message_id": "11111111-1111-4111-8111-111111111111",
            "from_entity_id": ALICE, "subject": "Request to join a group",
            "body": "we met at the meetup", "sent_at": _now(),
            "message_kind": "group_join",
            "payload": {"group_id": GID, "from_display_name": "alice-host",
                        "from_handle": "alice-host", "shares_machine": "studio"}}

    dpool = JoinPool(group_id=GID, owner=OWNER, cards=[card])
    out = await J.deny(dpool, OWNER, GID, ALICE)
    check(out.get("denied") and out.get("archived"), "a denial archives the card", out)
    check(archived == [card["message_id"]],
          "through the inbox's own archive path, on the card's id", archived)
    for table in ("contacts", "group_members", "domain_policy", "group_outbox"):
        wrote = [s for s, _a in dpool.execs
                 if re.search(rf"(?i)(INSERT\s+INTO|UPDATE)\s+{table}\b", " ".join(s.split()))]
        check(not wrote, f"deny writes nothing to {table}", wrote)
    check(not dpool.seq, "and leaves no trace at all beyond the archive", dpool.seq)

    gone = await J.deny(JoinPool(group_id=GID, owner=OWNER), OWNER, GID, ALICE)
    check(gone.get("denied") and gone.get("archived") is False,
          "denying a card that is no longer there is idempotent, not an error", gone)

    # ── 6. accept: contact FIRST, roster SECOND, grant only for a new member ─
    print("\n6. accept")
    contact_calls = []

    async def fake_accept_request(pool, *, handle, message_id=None, display_name=None,
                                  from_entity_id=None, kind="host", **_kw):
        # The seam. Everything on the OTHER side of it — the bridge, the slip
        # issue, the grant delivery — is a network. What is under test is the
        # ORDER join.accept does its two acts in, so the call is recorded into
        # the same sequence the pool records its writes into.
        contact_calls.append({"handle": handle, "message_id": message_id,
                              "display_name": display_name,
                              "from_entity_id": from_entity_id, "kind": kind})
        pool.seq.append("contact")
        return {"ok": True, "contact_id": 42, "grant_delivered": False}

    C.accept_request_service = fake_accept_request

    apool = JoinPool(group_id=GID, owner=OWNER, cards=[card])
    brain_service._pool = apool
    acc = await J.accept(apool, OWNER, GID, ALICE)

    check(apool.seq == ["contact", "roster", "outbox"],
          "the contact is made BEFORE the roster row, and the grant is enqueued after both",
          apool.seq)
    check(contact_calls and contact_calls[0]["handle"] == "alice-host",
          "the contact is named from the handle the RECEIVER stamped on the card, never from the sender's words",
          contact_calls)
    check(contact_calls and contact_calls[0]["message_id"] == card["message_id"],
          "and carries the card's id, so accepting archives the card it answered", contact_calls)
    check(acc.get("added") is True and acc.get("member_entity_id") == ALICE,
          "the requester is on the roster", acc)
    check(acc.get("contact_grant_delivered") is False,
          "contact_grant_delivered is reported as FALSE rather than as success — their host has not answered yet",
          acc.get("contact_grant_delivered"))
    check((acc.get("invite") or {}).get("status") == "queued",
          "and the group invite is QUEUED, not delivered and not refused — the normal case the card must not read as an error",
          acc.get("invite"))
    # QUEUED BY THE LOCAL ARM of app_lane.classify, which is the arm that
    # retries. Stated exactly: in this container-free process the slip lookup
    # cannot even import its module (server.routes.circles pulls server.main,
    # which wants the container's dashboard directory), so what is proven here
    # is that a slip this side does not have yields `local_unavailable:` and
    # therefore a RETRY — not that a pod with an empty held_slips table takes
    # the same path. The real-Postgres half of that lives in
    # test_groups_readmission_pg.py's lane.
    check(((acc.get("invite") or {}).get("reason") or "").startswith("local_unavailable:"),
          "queued by classify's LOCAL arm — a slip this side does not hold is retried, never filed as the peer's no",
          (acc.get("invite") or {}).get("reason"))

    # A card for someone who is ALREADY a current member: the roster is
    # untouched and NO grant is enqueued. Re-inviting a member who never left
    # would re-mint a slip for a relationship that already has one.
    member_pool = JoinPool(
        group_id=GID, owner=OWNER, cards=[card],
        members=[{"member_entity_id": ALICE, "role": "member", "removed_at": None}])
    brain_service._pool = member_pool
    again = await J.accept(member_pool, OWNER, GID, ALICE)
    check(again.get("added") is False, "accepting a current member adds nothing", again)
    check("outbox" not in member_pool.seq,
          "and enqueues no group grant — the enqueue is gated on a NEW member", member_pool.seq)
    check("invite" not in again, "so the answer carries no invite to report", again)

    missing = JoinPool(group_id=GID, owner=OWNER)
    try:
        await J.accept(missing, OWNER, GID, BOB)
        check(False, "accepting with no card for that host is refused")
    except R.RosterError as e:
        check(e.status == 404, "accepting with no card for that host is refused", e.detail)

    try:
        await J.accept(JoinPool(group_id=GID, owner=BOB, cards=[card]), OWNER, GID, ALICE)
        check(False, "a group this host does not own cannot be accepted into")
    except R.RosterError as e:
        check(e.status == 403, "a group this host does not own cannot be accepted into", e.detail)

    # ── 6b. A LATE REFUSAL MUST NOT STRAND THE CARD ─────────────────────────
    # accept_request_service ARCHIVES the card before it returns
    # (server/routes/contacts.py:586-592, unconditional). So anything that can
    # refuse AFTER it leaves the contact made, the allow written, the card gone
    # and no roster row — and the user cannot re-accept, because there is no
    # card left to find. That is unrecoverable through the API.
    #
    # The stub used above models the contact seam but NOT the archive, which is
    # exactly why the ordering check could not see this. This one archives.
    print("\n6b. a late refusal must not strand the card")

    async def archiving_accept_request(pool, *, handle, message_id=None, **_kw):
        pool.seq.append("contact")
        for c in pool.cards:
            if str(c["message_id"]) == str(message_id):
                c["archived_at"] = _now()
        return {"ok": True, "contact_id": 42, "grant_delivered": False}

    C.accept_request_service = archiving_accept_request

    for bad_role, why in [("owner", "a role add_member will not assign"),
                          ("creator", "the one role that is never assignable")]:
        card2 = dict(card)
        spool = JoinPool(group_id=GID, owner=OWNER, cards=[card2])
        brain_service._pool = spool
        raised = None
        try:
            await J.accept(spool, OWNER, GID, ALICE, role=bad_role)
        except R.RosterError as e:
            raised = e
        check(raised is not None and raised.status == 400,
              f"{why} is refused with a 400", raised.detail if raised else "accepted")
        check("contact" not in spool.seq,
              f"...and NOTHING was written first — no contact for role={bad_role!r}",
              spool.seq)
        check(card2.get("archived_at") is None,
              f"...and the card SURVIVES, so the user can accept again "
              f"(role={bad_role!r})")

    # The same shape with a VALID role but a group that closes underneath it:
    # here the contact IS made and the card IS gone, so the refusal has to say
    # so and say how to finish, rather than reporting a bare 409.
    card3 = dict(card)
    cpool = JoinPool(group_id=GID, owner=OWNER, cards=[card3])
    brain_service._pool = cpool
    real_add = R.add_member

    async def closing_add_member(pool, entity_id, group_id, member, **kw):
        raise R.RosterError(409, "that group is closed")

    R.add_member = closing_add_member
    raised2 = None
    try:
        await J.accept(cpool, OWNER, GID, ALICE)
    except R.RosterError as e:
        raised2 = e
    R.add_member = real_add
    check(raised2 is not None and raised2.status == 409,
          "a refusal that lands AFTER the contact was made is a 409", raised2)
    _detail = (raised2.detail if raised2 else "")
    check("accepted contact" in _detail and "/members" in _detail,
          "...and it says the contact WAS made and names the route that finishes "
          "the job, instead of leaving the user at a dead end", _detail)

    C.accept_request_service = fake_accept_request

    # ── 7. the message_kind vocabulary is in BOTH schemas ───────────────────
    print("\n7. schema parity")
    NEW_KINDS = ("machine_request", "group_join", "rendezvous_intro")
    for rel in ("migrations/v155_group_join_and_rendezvous.sql", "init-db.sql"):
        text = _read(rel)
        m = re.search(r"message_kind\s+IN\s*\((.*?)\)\s*\)", text, re.DOTALL)
        if not m:
            check(False, f"the message_kind CHECK constraint is present in {rel}", "no CHECK found")
            continue
        body = m.group(1)
        for kind in NEW_KINDS:
            check(f"'{kind}'" in body,
                  f"{rel} admits '{kind}' in the message_kind CHECK")
    for rel in ("migrations/v155_group_join_and_rendezvous.sql",):
        text = _read(rel)
        for tbl in ("group_join_requests", "rendezvous_interests"):
            check(f"CREATE TABLE IF NOT EXISTS {tbl}" in text, f"v155 declares {tbl}")
        check(not re.search(r"(?mi)^\s*DROP\s+(TABLE|INDEX|COLUMN)\b", text),
              "and drops no table, index or column — the migration is forward-only")

    # ── 8. refused and unreachable are different states ─────────────────────
    print("\n8. 'they said no' is not 'they were asleep'")

    async def answered_no(_owner, _params):
        return {"ok": False, "error": "this host does not host that group"}

    async def no_route(_owner, _params):
        raise PC.PeerUnavailable("no route to that host")

    PC.knock_peer = answered_no
    rpool = JoinPool(group_id=GID, owner=OWNER, group=False)
    refused = await J.ask_to_join(rpool, OTHER_GID, BOB, note="we met at the meetup")
    check(refused.get("status") == "refused",
          "an ANSWER of {ok: False} is recorded as 'refused' — a fact about the question, not the transport",
          refused)
    check(rpool.joins and rpool.joins[0][2] == "refused",
          "and that is the status the row carries", rpool.joins)
    check(rpool.joins and rpool.joins[0][0] == OTHER_GID and rpool.joins[0][1] == BOB,
          "keyed by the group, against the owner the caller named", rpool.joins)

    PC.knock_peer = no_route
    upool = JoinPool(group_id=GID, owner=OWNER, group=False)
    unreachable = await J.ask_to_join(upool, OTHER_GID, BOB)
    check(unreachable.get("status") == "unreachable",
          "a transport failure is recorded as 'unreachable' — retryable, and a different thing entirely",
          unreachable)
    check(upool.joins and upool.joins[0][2] == "unreachable",
          "and that is the status the row carries", upool.joins)
    check(refused.get("status") != unreachable.get("status"),
          "the two states are never collapsed into one — the user can tell them apart")
    check((unreachable.get("error") or "").startswith("owner unreachable"),
          "the unreachable row says what failed", unreachable.get("error"))

    for bad_owner in (0, -1, "nobody", None):
        try:
            await J.ask_to_join(JoinPool(group_id=GID, owner=OWNER, group=False),
                                OTHER_GID, bad_owner)
            check(False, f"asking with a non-entity owner is refused ({bad_owner!r})")
        except R.RosterError as e:
            check(e.status == 400, f"asking with a non-entity owner is refused ({bad_owner!r})", e.detail)
    try:
        await J.ask_to_join(JoinPool(group_id=GID, owner=OWNER, group=False), "nope", BOB)
        check(False, "asking about a malformed group id is refused before anything leaves")
    except R.RosterError as e:
        check(e.status == 400, "asking about a malformed group id is refused before anything leaves", e.detail)

    long_note = "x" * (J.MAX_NOTE + 500)
    PC.knock_peer = answered_no
    npool = JoinPool(group_id=GID, owner=OWNER, group=False)
    capped = await J.ask_to_join(npool, OTHER_GID, BOB, note=long_note)
    check(len(capped.get("note") or "") == J.MAX_NOTE,
          "a note is bounded on the SENDING side too, so a caller is never silently truncated at the far end",
          len(capped.get("note") or ""))

    print(f"\n{CHECKS} checks, {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  FAILED: " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
