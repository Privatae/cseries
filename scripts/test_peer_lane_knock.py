#!/usr/bin/env python3
"""The slipless knock on the peer lane — the properties that must never regress.

Container-free on purpose: these are pure classification and gating decisions, so
they run on EVERY merge rather than skipping without a Postgres.

Background. The peer lane (`:8444`, mTLS, CEIGAS CA) is slip-gated end to end,
and a slip only exists once someone ACCEPTS a connection request. So the one
message that STARTS a relationship cannot travel on it and would have to go by
bridge, which keeps its connections in an in-process dict per DaemonSet pod
with no forwarding between them: a request between two Hosts connected to
different pods is answered `503 "entity N not connected"`, so no first message
and no slip can ever exist. `inbox.request` closes that
bootstrap, and every assertion below guards a way it could go wrong.

What is pinned:

  1. `message` can NEVER arrive slipless. The slip is the sole authority for
     host↔host messaging (memory_handlers.py:399-408); a knock able to carry
     message_kind='message' would route around it entirely. This is the finding
     this file exists for.
  2. The op is CLASSIFIED — registered as a handler AND in the slipless set. A
     registered-but-unclassified op must be refused, never defaulted to "no slip
     needed": defaulting is the bastion's own opFamily bug, where an unknown name
     fell through to ALLOW.
  3. The slipless handler never claims attestation. `slip_authorized` and
     `sender_verified` mean "a slip was checked"; none was, so setting either
     would forge the exact proof the inbox gate looks for.
  4. Identity comes from the CERT, never the caller's claim.
  5. Another tenant's free text is bounded at the trust boundary.
  6. The SENDER cannot choose the name the user sees. The card renders from the
     inner payload and Accept writes that handle into contacts AND into the
     domain_policy allow rule, so spreading a sender's payload let them pick the
     identity on the card. The bridge strips those fields for that reason; this
     lane allow-lists instead.
  7. A kind the table would reject is never advertised (contact_request is absent
     from the inbox_messages CHECK, so offering it guaranteed a failed INSERT).
  8. A REFUSED handler is not reported as success — and the refusal is marked as
     the receiver's, so the caller does not retry the same message down another
     transport into the same gate.
  9. One live knock per peer, per kind: nothing else limits this lane, so without
     a cap "any valid cert may append a card" means "without limit". Answering
     reopens the slot; an unavailable pool fails OPEN.

NOT pinned here, and worth knowing: domain_policy does NOT gate request kinds —
check_inbound_allowed returns True for them before reading any table
(domain_policy.py:176-177), on this lane and the bridge alike. Earlier comments in
this feature claimed otherwise and were wrong.

Run:  APP_ROOT=<repo> python3 scripts/test_peer_lane_knock.py
"""
from __future__ import annotations

import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS: list[str] = []


def check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    from server.peer import handlers as H
    from server.peer import peer_server as PS

    captured: list[dict] = []

    async def fake_append(payload):
        captured.append(payload)
        return {"ok": True, "message_id": "m1"}

    # handle_inbox_append (memory_handlers.py:327) is the real gate; stubbed here so
    # this stays container-free. What is under test is what the handler HANDS it.
    H.handle_inbox_append = fake_append

    print("\n── 1. a knock can never carry a `message` ──")
    res = asyncio.run(H.inbox_request(9122,None, {"message_kind": "message", "body": "hi"}))
    check("message_kind='message' is REFUSED on the slipless op",
          res.get("ok") is False and "not a request kind" in (res.get("error") or ""), res)
    check("...and nothing reached the inbox at all", not captured, captured)

    for bad in ("system_notification", "endpoint_grant", "permission_grant", "anything"):
        r = asyncio.run(H.inbox_request(9122,None, {"message_kind": bad}))
        check(f"...and so is {bad!r}", r.get("ok") is False, r)
    check("still nothing reached the inbox", not captured, captured)

    print("\n── 2. the op is classified, and nothing can default open ──")
    check("inbox.request is registered as a handler", "inbox.request" in H.PEER_HANDLERS)
    check("inbox.request is in the slipless set", "inbox.request" in PS._SLIPLESS_OPS)
    check("inbox.deliver is NOT slipless", "inbox.deliver" not in PS._SLIPLESS_OPS)
    check("inbox.deliver still maps to the 'message' slip action",
          PS._OP_ACTION.get("inbox.deliver") == "message")
    # Three classes: slipless, a fixed slip action, or a slip
    # action derived from the request (app.deliver -> app:<app_id>). An op in
    # none of them is refused by _serve as unknown.
    derived = getattr(PS, "_OP_ACTION_FROM_PARAMS", {})
    unclassified = [op for op in H.PEER_HANDLERS
                    if op not in PS._SLIPLESS_OPS and op not in PS._OP_ACTION
                    and op not in derived]
    check("every registered op is classified (an unclassified one is refused)",
          not unclassified, unclassified)
    gated = set(PS._OP_ACTION) | set(derived)
    overlap = set(PS._SLIPLESS_OPS) & gated
    check("no op is both slipless and slip-gated", not overlap, overlap)
    check("no op has both a fixed and a derived slip action",
          not (set(PS._OP_ACTION) & set(derived)), set(PS._OP_ACTION) & set(derived))

    print("\n── 3. the knock never forges attestation ──")
    captured.clear()
    res = asyncio.run(H.inbox_request(9122,None, {"body": "we share a machine"}))
    check("a plain knock is accepted", res.get("ok") is True, res)
    p = captured[0] if captured else {}
    check("slip_authorized is NOT set — no slip was checked",
          "slip_authorized" not in p, sorted(p.keys()))
    check("sender_verified is NOT set — nothing beyond the cert was verified",
          "sender_verified" not in p, sorted(p.keys()))
    check("authorized_via_slip is NOT set in the body payload",
          "authorized_via_slip" not in (p.get("payload") or {}), p.get("payload"))
    check("it defaults to endpoint_request",
          p.get("message_kind") == "endpoint_request", p.get("message_kind"))

    print("\n── 4. identity is the CERT's, not the caller's claim ──")
    captured.clear()
    asyncio.run(H.inbox_request(9122,None, {
        "body": "x", "from_entity_id": 999, "payload": {"from_entity_id": 999}}))
    p = captured[0]
    check("from_entity_id is the peer_id the server read off the cert",
          p.get("from_entity_id") == 9122, p.get("from_entity_id"))
    check("from_address is derived from that id, not supplied",
          p.get("from_address") == "9122@entity.localhost", p.get("from_address"))

    print("\n── 5. another tenant's free text is bounded here ──")
    captured.clear()
    asyncio.run(H.inbox_request(9122,None, {"body": "b" * 5000, "subject": "s" * 5000}))
    p = captured[0]
    check("body is capped at the boundary",
          len(p.get("body", "")) == H._MAX_BODY, len(p.get("body", "")))
    check("subject is capped at the boundary",
          len(p.get("subject", "")) == H._MAX_SUBJECT, len(p.get("subject", "")))

    print("\n── 6. the sender cannot choose the name the user sees ──")
    # The card reads its name straight out of the inner payload
    # (inbox-pane.tsx:244-247), and Accept writes THAT handle into contacts and
    # into the domain_policy allow rule. The bridge path strips these, because a
    # host could present itself as a named person in a connection request.
    # Spreading the payload verbatim repeats that bug on a new lane.
    captured.clear()
    asyncio.run(H.inbox_request(9122,None, {"body": "x", "payload": {
        "from_display_name": "Alex Rivera", "display_name": "Alex Rivera",
        "from_handle": "alex", "handle": "alex", "from_user_email": "alex@example.com",
        "from_user_name": "Alex", "kind": "human", "authorized_via_slip": True,
        "from_entity_id": 999, "via": "bastion_peer", "machine": "DESKTOP-EXAMPLE1"}}))
    inner = captured[0].get("payload") or {}
    # The invariant is that the sender's VALUE never survives — not that the key is
    # absent. from_display_name/from_handle ARE present now, written by _identify
    # from the receiver's own peer list (section 11). Asserting absence would fail
    # on the very fix that closed the hole, so assert what actually matters.
    forged_values = ("Alex Rivera", "alex", "alex@example.com", "Alex", "human")
    for k, v in inner.items():
        check(f"no sender-chosen value survived in {k!r}",
              str(v) not in forged_values, {k: v})
    for forged in ("display_name", "handle", "from_user_email", "from_user_name",
                   "kind", "authorized_via_slip", "from_entity_id"):
        check(f"payload key {forged!r} is dropped outright", forged not in inner, inner)
    check("from_display_name is OURS, not theirs",
          inner.get("from_display_name") != "Alex Rivera", inner)
    check("the sender's own `via` is kept (it is provenance, not identity)",
          inner.get("via") == "bastion_peer", inner)
    check("the transport is recorded separately, not over the top of it",
          inner.get("lane") == "peer", inner)

    print("\n── 7. a kind the table rejects is not advertised ──")
    # contact_request is absent from the inbox_messages CHECK (init-db.sql:1882-1889),
    # so advertising it guaranteed an INSERT failure — reported to the sender as
    # success, because the wrapper returned ok:True whatever the handler said.
    check("contact_request is NOT offered", "contact_request" not in H.REQUEST_KINDS,
          H.REQUEST_KINDS)
    r = asyncio.run(H.inbox_request(9122,None, {"message_kind": "contact_request"}))
    check("...and is refused if asked for", r.get("ok") is False, r)

    print("\n── 8. a refused handler is not reported as success ──")
    async def refusing_append(payload):
        return {"ok": False, "blocked": True, "error": "blocked by domain policy"}
    H.handle_inbox_append = refusing_append
    srv = PS.PeerServer(trust=None, handlers=H.PEER_HANDLERS, my_entity_id=9003)
    out = asyncio.run(srv._serve(9122, {"op": "inbox.request", "slip": None,
                                       "params": {"body": "x"}}))
    check("_serve reports the refusal as a FAILURE",
          out.get("ok") is False, out)
    check("...and marks it as the RECEIVER's answer, so the caller does not retry "
          "the same message into the same gate", out.get("receiver") is True, out)
    H.handle_inbox_append = fake_append

    print("\n── 9. one live knock per peer, per kind ──")
    class _Pool:
        def __init__(self, n): self.n = n
        async def fetchval(self, *a): return self.n
    import server.brain_service as BS
    real_get_pool = BS.get_pool
    BS.get_pool = lambda: _Pool(1)          # one already waiting
    r = asyncio.run(H.inbox_request(9122,None, {"body": "again"}))
    check("a second unanswered knock from the same peer is refused",
          r.get("ok") is False and "already waiting" in (r.get("error") or ""), r)
    BS.get_pool = lambda: _Pool(0)          # the user answered it
    captured.clear()
    r = asyncio.run(H.inbox_request(9122,None, {"body": "after answering"}))
    check("...and the slot reopens once it is answered", r.get("ok") is True, r)
    BS.get_pool = lambda: None              # pool unavailable
    r = asyncio.run(H.inbox_request(9122,None, {"body": "degraded"}))
    check("...and the cap fails OPEN when it cannot be checked", r.get("ok") is True, r)
    BS.get_pool = real_get_pool

    print("\n── 10. the minted message_id is carried ──")
    # contacts.py mints one id for BOTH transports so a timeout-then-bridge retry
    # dedupes. The handler dropped it, which made that comment describe something
    # the lane did not do — handle_inbox_append only dedupes on a supplied id.
    captured.clear()
    asyncio.run(H.inbox_request(9122,None, {"body": "x", "message_id": "abc-123"}))
    check("message_id reaches the inbox append",
          captured[0].get("message_id") == "abc-123", captured[0].get("message_id"))

    print("\n── 11. the card is named from OUR records, not the knock ──")
    import server.network.peer_discovery as PD
    real_list = PD.list_peerings

    async def fake_peerings(pool, eid):
        return [{"peer_entity_id": 9122, "peer_handle": "8abb770bbfde71d0",
                 "introduced_by": "DESKTOP-EXAMPLE1", "current": True}]
    PD.list_peerings = fake_peerings

    class _P:
        async def fetchval(self, *a): return 0
    import server.brain_service as BS
    real_pool, BS.get_pool = BS.get_pool, lambda: _P()

    captured.clear()
    asyncio.run(H.inbox_request(9122,None, {"body": "x", "payload": {
        "from_display_name": "Alex Rivera", "from_handle": "alex"}}))
    inner = captured[0]["payload"]
    check("the name comes from OUR peer list, not the sender's payload",
          inner.get("from_display_name") == "8abb770bbfde71d0", inner)
    check("the handle likewise — so Accept cannot write the sender's choice",
          inner.get("from_handle") == "8abb770bbfde71d0", inner)
    check("...and it is NOT an address (the invariant the route gate pins)",
          "@" not in str(inner.get("from_handle", "")), inner)
    check("the shared machine is named from our own record",
          inner.get("shares_machine") == "DESKTOP-EXAMPLE1", inner)

    async def no_peerings(pool, eid):
        return []
    PD.list_peerings = no_peerings
    captured.clear()
    asyncio.run(H.inbox_request(9122,None, {"body": "x", "payload": {
        "from_display_name": "Alex Rivera", "from_handle": "alex"}}))
    inner = captured[0]["payload"]
    check("an UNKNOWN knocker gets a bare id, never the name it asked for",
          inner.get("from_display_name") == "Host 9122" and "from_handle" not in inner, inner)
    PD.list_peerings = real_list
    BS.get_pool = real_pool

    print("\n── 12. only the kind that has a sender ──")
    check("permission_request is no longer advertised",
          "permission_request" not in H.REQUEST_KINDS, H.REQUEST_KINDS)
    # EXACT, not a membership check, and that is the point: this is a slipless
    # allow-list, so the test has to fail when a kind is ADDED, not only when
    # one is removed. machine_request is one host
    # asking another for a model on its machine. It belongs here for the same
    # reason endpoint_request does (it carries no credential and confers nothing
    # until a person answers), and it is gated harder than this one: on an
    # existing accepted contact, because asking a stranger for their hardware is
    # not a first contact and does not need the stranger allowance.
    # group_join is a host asking to be let into a group circle whose
    # roster this host owns. It belongs here for the reason endpoint_request
    # does and NOT for the reason machine_request does — it is a FIRST contact,
    # so it is deliberately absent from _KINDS_NEEDING_A_CONTACT below.
    # Requiring a contact first would make the op pointless, because an accepted
    # contact can already be added by the owner. Its gate is stronger than the
    # stranger allowance instead: _group_join_target refuses any card that does
    # not name a group THIS host owns, which is checked in
    # scripts/test_group_join_requests.py.
    check("the slipless request kinds are exactly the three we mean to serve",
          H.REQUEST_KINDS == ("endpoint_request", "machine_request", "group_join"),
          H.REQUEST_KINDS)
    check("...and only the machine ask demands a relationship first",
          H._KINDS_NEEDING_A_CONTACT == ("machine_request",), H._KINDS_NEEDING_A_CONTACT)

    print("\n── 13. the SLIP-GATED path refuses honestly too ──")
    async def refusing(peer_id, info, params):
        return {"ok": False, "error": "nope"}
    srv2 = PS.PeerServer(trust=None, handlers={"inbox.deliver": refusing}, my_entity_id=9003)
    out = asyncio.run(srv2._serve(9122, {"op": "inbox.deliver", "slip": None, "params": {}}))
    check("a slip-gated op with no slip is still refused before the handler",
          out.get("ok") is False and out.get("error") == "slip required", out)

    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
