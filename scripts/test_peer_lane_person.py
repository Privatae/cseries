#!/usr/bin/env python3
"""The PERSON on a direct-lane delivery — who may name one, and who checks.

Container-free, like its sibling test_peer_lane_knock.py: these are pure gating
decisions on what `inbox.deliver` accepts, so they run on every merge.

Background. A host↔host message may name the human who wrote it. While
the bridge carried these messages the PLATFORM answered for that human — it
checked `from_user_email` against `operator_entities` and dropped a claim that
did not hold (adapter.py _enrich_and_verify_cross_host). That check is the
reason the direct lane refused human-authored sends at all, and the reason
host↔host messaging was stuck on a relay that cannot route between its own
DaemonSet pods (measured: one Host on bridge ::250, its peer on ::23f,
`503 "entity 9122 not connected"`).

The rule: the host vouches for its own people. So the
question moves, and each end now answers only the half it can actually know:

  * the SENDER asserts its own operator. We cannot check it and do not pretend
    to — the assertion is worth what the sending host is worth, and mTLS already
    bound this connection to that host's cert.
  * the RECEIVER checks the addressee against ITS OWN operator allowlist,
    because that person is ours.

What is pinned here:

  1. A message addressed to somebody who is not an operator of this host is
     REFUSED, not filed under a name we cannot account for.
  2. "I cannot check" is not "yes". On an unprovisioned pod the delivery is
     refused. This deliberately differs from is_operator(), which answers True
     when unprovisioned because a dashboard request has already passed the
     router and locking an operator out of their own pod would be worse
     (operator_identity.py). Nothing stands in front of the peer lane.
  3. The SENDER cannot choose the name the reader sees. circles.py renders
     payload->>'from_user_name' as a message's author, so a key the sender
     controls is a name the reader believes. inbox_request has been allow-listed
     against exactly this since the knock shipped; inbox_deliver was not, and
     this is the half that closes it.
  4. A person's address is normalised the way the COLUMN is written, not by a
     second copy of the rule that is free to drift.
  5. A host-authored message (no person named) is unaffected — it still
     delivers, because most traffic on this lane has no human on it.
  6. A person claim our OWN records refute is dropped, and the message still
     delivers host-authored. The assertion is unverifiable by design; it is not
     unfalsifiable, and contacts.peer_entity_id is the local evidence. Without
     this, looking the address up in our own address book to name the sender
     turned an unverified claim into a trusted one — the sender got to pick
     which of the receiver's contacts to wear.
  7. Another tenant's SUBJECT and display name are bounded here, at the trust
     boundary, because nothing downstream bounds them. The BODY deliberately is
     not: this is a direct message and the knock's 500-char cap would be absurd
     on one, so the 256KB frame is the only limit and that is the intent.

Run:  APP_ROOT=<repo> python3 scripts/test_peer_lane_person.py
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


PEER = 9122
SLIP = {"terms": {"resource": "peer:inbox"}}


def main() -> int:
    from server.peer import handlers as H

    captured: list[dict] = []

    captured_kw: list = []

    async def fake_append(payload, **kw):
        captured.append(payload)
        captured_kw.append(kw)
        return {"ok": True, "message_id": "m1"}

    # The real gate (memory_handlers.py) is stubbed so this stays container-free.
    # What is under test is what the handler HANDS it.
    H.handle_inbox_append = fake_append

    # Block is SQL over contacts, and the pool here is a stub, so the block
    # check is taken out of the way. It is pinned against a real database, on
    # this op and every other way in, by test_circles_block_pg.py.
    async def _no_block(*_a, **_k):
        return None

    H._block_refusal = _no_block

    def deliver(params, *, operators="me@example.com"):
        captured.clear()
        if operators is None:
            os.environ.pop("ENTITY_OPERATOR_EMAILS", None)
        else:
            os.environ["ENTITY_OPERATOR_EMAILS"] = operators
        return asyncio.run(H.inbox_deliver(PEER, SLIP, params))

    print("\n── 1. the addressee is checked HERE, against our own operators ──")
    r = deliver({"body": "hi", "to_user_email": "stranger@example.com"})
    check("a message for somebody who is not an operator here is refused",
          r.get("ok") is False and r.get("blocked") is True, r)
    check("...and nothing reached the inbox", not captured, captured)

    r = deliver({"body": "hi", "to_user_email": "me@example.com"})
    check("a message for one of our own people is delivered", r.get("ok") is True, r)
    check("...and it is stored with the addressee on it",
          len(captured) == 1 and captured[0].get("to_user_email") == "me@example.com",
          captured)

    r = deliver({"body": "hi", "to_user_email": "  ME@Example.com "})
    check("the addressee is matched case- and whitespace-insensitively, the way "
          "the column is written", r.get("ok") is True, r)

    print("\n── 2. 'I cannot check' is not 'yes' ──")
    r = deliver({"body": "hi", "to_user_email": "me@example.com"}, operators=None)
    check("an unprovisioned pod REFUSES a message that names an addressee",
          r.get("ok") is False and r.get("blocked") is True, r)
    check("...and nothing reached the inbox", not captured, captured)
    # The contrast that makes the direction deliberate rather than accidental.
    from server import operator_identity as OI
    os.environ.pop("ENTITY_OPERATOR_EMAILS", None)
    check("...while is_operator() still answers True unprovisioned — the two "
          "fail in OPPOSITE directions on purpose, because a router stands in "
          "front of that one and nothing stands in front of this one",
          OI.is_operator("anyone@example.com") is True)
    check("operator_allowlist() reports the unknown as None, which is what lets "
          "this caller pick its own direction", OI.operator_allowlist() is None)

    print("\n── 3. a host-authored message is unaffected ──")
    r = deliver({"body": "hi"})
    check("no person named → delivered", r.get("ok") is True, r)
    check("...with no person fields invented",
          len(captured) == 1
          and "to_user_email" not in captured[0]
          and "from_user_email" not in captured[0], captured)
    r = deliver({"body": "hi"}, operators=None)
    check("...and an unprovisioned pod still takes it (the refusal in 2 is "
          "scoped to messages that NAME somebody, not to the whole lane)",
          r.get("ok") is True, r)

    print("\n── 4. the sender cannot choose a name PER MESSAGE ──")
    # The first cut of this change allowed from_display_name / peer_display_name
    # through, on the reasoning that only the person keys name somebody. That was
    # wrong: those two are what titles a thread. circles.py _list_entity_threads
    # COALESCEs them for the Hosts list, _people_thread renders from_display_name
    # as `via`, _entity_thread as peer_name, and messages.py reads the same pair.
    # A slip-holding peer could have titled a thread "Privatae Support". This loop
    # is the regression guard, so the key list below must stay a superset of every
    # payload key any reader renders.
    #
    # "per message" is the honest claim, not "any name": _identify sources the host
    # name from peer_handle, which a bastion announced, so it is still peer-
    # INFLUENCED one step removed. That is materially weaker — it needs a shared
    # machine rather than a slip, it is per-peer and stable rather than per-message,
    # and the same string already shows in Circles > Peers and Network, so it cannot
    # be aimed at one thread. Taking the name off the message instead would be
    # strictly worse. Stated so the stronger claim never becomes load-bearing.
    r = deliver({"body": "hi", "payload": {
        "from_user_name": "Alex Rivera", "from_user_email": "alex@example.com",
        "to_user_email": "me@example.com", "display_name": "Privatae",
        "handle": "privatae", "authorized_via_slip": False,
        "from_display_name": "Privatae Support",
        "peer_display_name": "Privatae Support",
        "from_handle": "privatae", "peer_user_name": "Privatae"}})
    inner = captured[0].get("payload") if captured else {}
    for forged in ("from_user_name", "from_user_email", "to_user_email",
                   "display_name", "handle", "peer_display_name",
                   "peer_user_name"):
        check(f"a {forged!r} planted in the nested payload is dropped",
              forged not in inner, inner)
    check("from_display_name — which TITLES the thread — is never the sender's",
          inner.get("from_display_name") != "Privatae Support", inner)
    check("from_handle is never the sender's either",
          inner.get("from_handle") != "privatae", inner)
    # The allow-list carries a topic,
    # its id and its title, and nothing else. Neither is a key a reader takes a
    # name from, and the thread stays derived (5d). Pinned as exactly those two
    # so a third key is again a deliberate change with a reader in mind.
    # Also the reading notice's one key, "on" or "off",
    # which names nobody and is checked again at storage (circles_reading.stored).
    check("the only keys a foreign sender may place in the nested payload are the topic's two "
          "and the reading notice's",
          tuple(H._ALLOWED_DELIVER_PAYLOAD_KEYS) == ("topic_id", "topic_title", "reading_notice"),
          H._ALLOWED_DELIVER_PAYLOAD_KEYS)
    check("...and nothing else a foreign sender puts there survives: what is left is ours "
          "(_identify's name, handle and shared machine)",
          set(inner) <= {"from_display_name", "from_handle", "shares_machine"}, inner)
    # The slip verification never rides as payload flags a peer could set — it goes to the inbox gate
    # in-process (verified_slip=), and the gate writes authorized_via_slip itself.
    check("...and the attestation is never the caller's: the verified slip goes to the gate in-process",
          "authorized_via_slip" not in inner and (captured_kw[-1:] or [{}])[0].get("verified_slip") is SLIP,
          (inner, captured_kw[-1:]))
    check("a payload-planted addressee does NOT reach the top-level column "
          "either — the gate in 1 keys on the param, so a nested one must not "
          "sneak past it", "to_user_email" not in captured[0], captured)

    print("\n── 4b. the sending HOST is named from OUR records ──")
    # _identify is the mechanism inbox_request already uses; its docstring argues
    # that a knocker must not choose the name on the card "and this must not
    # reopen". inbox_deliver reopened it. Without this the direct lane also wrote
    # no host name at all, so one peer's thread titled "Host 9122" for messages that
    # came direct and with its real handle for the ones that came by bridge.
    r = deliver({"body": "hi"})
    inner = captured[0].get("payload") if captured else {}
    check("a delivery carries a from_display_name we wrote",
          bool(inner.get("from_display_name")), inner)
    check("...and with no peer record it is an honest 'Host <id>', never an address",
          inner.get("from_display_name") == f"Host {PEER}", inner)

    print("\n── 5. the author travels as a TOP-LEVEL claim, written by us ──")
    r = deliver({"body": "hi", "from_user_email": "Alice@Example.com ",
                 "from_user_name": "Alice"})
    top = captured[0] if captured else {}
    inner = top.get("payload") or {}
    check("from_user_email is stored, normalised",
          top.get("from_user_email") == "alice@example.com", top)
    check("...and mirrored into the payload the reader renders from",
          inner.get("from_user_email") == "alice@example.com"
          and inner.get("from_user_name") == "Alice", inner)
    check("the sender id still comes from the CERT, never the params",
          top.get("from_entity_id") == PEER, top)

    r = deliver({"body": "hi", "from_user_email": "not an address",
                 "from_user_name": "Nobody"})
    top = captured[0] if captured else {}
    check("a malformed author address is dropped rather than stored half-formed",
          "from_user_email" not in top, top)
    check("...and its name goes with it, so no unattributed name is rendered",
          "from_user_name" not in (top.get("payload") or {}), top)

    r = deliver({"body": "hi", "from_user_email": "alice@example.com",
                 "from_user_name": "A" * 500})
    inner = captured[0].get("payload") if captured else {}
    check("another tenant's display name is bounded at the trust boundary",
          len(inner.get("from_user_name", "")) <= H._MAX_USER_NAME, inner)

    print("\n── 5b. our own records can REFUTE a person claim, and then do ──")
    # The People views key on the ADDRESS alone — _list_people_threads' inbound arm
    # is `to_entity_id = $1 AND to_user_email = $2` and the thread's identity is
    # from_user_email, with no tie to the sending host. So a slip-holding peer that
    # claims alice@ lands inside the genuine Alice thread. The claim is unverifiable
    # by design but it is not UNFALSIFIABLE: contacts.peer_entity_id
    # says which host we believe that person is reached through, and it is on the row
    # we already fetch. Discarding that field renders our own name for whoever asked.
    # _person_claim resolves the pool and the lookup through their real modules,
    # so stubbing those two is enough to drive every branch without a container.
    import server.brain_service as _bs
    import server.routes.contacts as _contacts
    _bs.get_pool = lambda: object()

    def _with_contact(row):
        """What THIS host's address book says about the person, or None."""
        _contacts.find_human_contact = lambda pool, email: asyncio.sleep(0, row)

    # (a) our records place this person on ANOTHER host -> claim dropped, message kept
    _with_contact({"peer_entity_id": 999, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    top = captured[0] if captured else {}
    check("a peer speaking for somebody our records place on ANOTHER host has the "
          "claim dropped", "from_user_email" not in top, top)
    check("...but the message is still DELIVERED, host-authored — the bridge's own "
          "behaviour, and a person who legitimately moved host must not bounce",
          r.get("ok") is True, r)
    check("...and no name of ours is lent to it",
          "from_user_name" not in (top.get("payload") or {}), top)

    # (b) our records agree -> the claim stands and takes OUR name for them
    _with_contact({"peer_entity_id": PEER, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    top = captured[0] if captured else {}
    check("our records AGREE -> the claim stands",
          top.get("from_user_email") == "alice@example.com", top)
    check("...and it is named from OUR contact row",
          (top.get("payload") or {}).get("from_user_name") == "Alice", top)

    # (c) a name the sender supplied is not overwritten by ours
    r = deliver({"body": "hi", "from_user_email": "alice@example.com",
                 "from_user_name": "Sent Name"})
    check("a name the sender DID supply still wins",
          (captured[0].get("payload") or {}).get("from_user_name") == "Sent Name",
          captured)

    # (d) blocked contact -> not named by us. inbox_deliver now refuses a blocked
    # person before _person_claim is asked (test_circles_block_pg.py C1-C4); with
    # the block check out of the way, as here, the claim still lends no name.
    _with_contact({"peer_entity_id": PEER, "display_name": "Alice",
                   "status": "blocked"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    check("a BLOCKED contact is not named by us — "
          "find_human_contact filters on kind, never status",
          r.get("ok") is True
          and "from_user_name" not in (captured[0].get("payload") or {}), captured)

    # (e) no contact at all -> nothing is contradicted; address, no name
    _with_contact(None)
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    top = captured[0] if captured else {}
    check("no contact -> nothing to contradict, claim kept",
          top.get("from_user_email") == "alice@example.com", top)
    check("...and no name invented",
          "from_user_name" not in (top.get("payload") or {}), top)

    # (f) we hold a contact but no host for them. TWO questions, not one: nothing
    # refutes the claim (keep it) and nothing corroborates it (do NOT lend our
    # name). Collapsing those is how R1 survived a round — the first cut tested
    # only for contradiction, so this row fell through and returned "Alice".
    # It is the ORDINARY case, not an edge one: create_contact_service inserts
    # (handle, display_name, kind, notes, status) and its ON CONFLICT never sets
    # peer_entity_id, so every contact a user adds by hand has it NULL.
    _with_contact({"peer_entity_id": None, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    check("a contact with no host recorded cannot refute anything",
          captured[0].get("from_user_email") == "alice@example.com", captured)
    check("...and cannot CORROBORATE anything either — no name is lent to a peer "
          "just because we happen to know that address",
          "from_user_name" not in (captured[0].get("payload") or {}), captured)

    print("\n── 5c. another tenant's subject is bounded like its body ──")
    _with_contact(None)
    r = deliver({"body": "hi", "subject": "S" * 4000})
    check("subject is capped at _MAX_SUBJECT, not at the 256KB frame",
          len(captured[0].get("subject", "")) <= H._MAX_SUBJECT, len(captured[0].get("subject", "")))
    r = deliver({"body": "hi", "subject": "   "})
    check("...and a whitespace-only subject still yields a usable one",
          captured[0].get("subject") == "(message)", captured)

    print("\n── 5d. the thread is DERIVED, so a peer cannot join another one ──")
    # The last field where the receiver took the sender's word for something it can
    # compute. dm_thread_id is uuid5 over a SORTED entity pair and people_thread_id
    # over a sorted address pair, so any peer can compute either. Accepting it let a
    # slip-holding B send dm_thread_id(A, C) and land inside A's conversation with C:
    # conversations_service does SELECT DISTINCT ON (thread_id) and takes the peer
    # from the NEWEST row, so A's entry for C flips to B. Both that and thread_service
    # name themselves the source of truth for the host_message tool — the surface the
    # MODEL reads.
    from server.routes.messages import dm_thread_id
    from server.routes.messages import people_thread_id
    import server.brain_service as _bs2
    _bs2._entity_id = 9003

    _with_contact(None)
    hijack = dm_thread_id(9003, 4242)         # A<->C, a thread B has no part in
    r = deliver({"body": "hi", "thread_id": hijack})
    check("a thread_id the sender chose is NOT stored",
          captured[0].get("thread_id") != hijack, captured[0].get("thread_id"))
    check("...it is the host pair WE compute for this peer",
          captured[0].get("thread_id") == dm_thread_id(9003, PEER), captured)

    # person<->person: both ends named -> the People pair, matching the readers,
    # which select on both columns being NOT NULL.
    _with_contact({"peer_entity_id": PEER, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "thread_id": hijack,
                 "from_user_email": "alice@example.com",
                 "to_user_email": "me@example.com"})
    check("a person<->person delivery derives the PEOPLE thread",
          captured[0].get("thread_id")
          == people_thread_id("alice@example.com", "me@example.com"), captured)

    # a REFUTED person claim to one of our people is refused (section 5g); one to
    # this Host itself collapses to the host form, which is where that row files.
    _with_contact({"peer_entity_id": 999, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "thread_id": hijack, "from_user_email": "alice@example.com"})
    check("a refuted person claim to this Host collapses to the HOST thread, matching "
          "where the row actually files",
          captured and captured[0].get("thread_id") == dm_thread_id(9003, PEER), captured)

    print("\n── 5e. bastion-supplied names are scrubbed on the way into storage ──")
    # _identify now stamps peer_handle onto EVERY delivered message, not just a
    # knock card. peer_discovery stores it raw from the announcement and owns _plain
    # for exactly this, but applied it only on its notification path.
    async def _peer_row(pool, me):
        return [{"peer_entity_id": PEER,
                 "peer_handle": "evil\nSubject: forged\x07" + "x" * 400,
                 "introduced_by": "box\nline"}]
    import server.network.peer_discovery as _pd
    _pd.list_peerings = _peer_row
    _with_contact(None)
    r = deliver({"body": "hi"})
    nm = (captured[0].get("payload") or {}).get("from_display_name", "")
    check("no newline or control character survives into the stored host name",
          "\n" not in nm and "\x07" not in nm, repr(nm))
    check("...and it is length-capped, not a 400-char payload",
          len(nm) <= 121, len(nm))
    check("the machine name is scrubbed too",
          "\n" not in (captured[0].get("payload") or {}).get("shares_machine", ""),
          captured)

    print("\n── 5f. the receiver ALWAYS answers the person question ──")
    # present-and-false vs absent are different answers. A receiver carrying this
    # change always sets the key; absence means an image that never read the field.
    # The sender's response differs per case, so conflating them forces a choice
    # between a privacy regression and a split thread.
    _with_contact({"peer_entity_id": PEER, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    check("claim kept -> person_stored True", r.get("person_stored") is True, r)

    _with_contact({"peer_entity_id": 999, "display_name": "Alice",
                   "status": "active"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    check("claim REFUTED -> person_stored False, not absent — 'I understood you "
          "and disagreed' must not read as 'I am an old image'",
          r.get("person_stored") is False, r)

    _with_contact(None)
    r = deliver({"body": "hi"})
    check("no person claimed -> the key is absent, because there was no question",
          "person_stored" not in r, r)

    # The fourth state. claimed_person is computed from the RAW param, so a claim
    # we cannot USE still counts as a claim made. Computing it after _clean_email
    # made this read as "no claim", the flag went absent, and the sender diagnosed
    # "old image" for its own address failing OUR validation — two meanings for one
    # absence, which is the exact conflation 5f exists to prevent. The two ends
    # validate differently (the sender puts the router header on the wire as-is and
    # never runs _clean_email on it), so the divergence is real.
    r = deliver({"body": "hi", "from_user_email": "alex@localhost"})
    check("a MALFORMED claim answers False, not absent — we were asked and could "
          "not use it, which is not the same as never being asked",
          r.get("person_stored") is False, r)
    check("...and no half-formed identity is stored",
          "from_user_email" not in captured[0], captured)

    print("\n── 5g. a claim to write to one of our PEOPLE must be verified ──")
    # The People chat is keyed on the two
    # addresses alone, so a person-to-person claim our contacts do not place on the
    # sending Host would let any slip-holding Host write into our person's chat with
    # whoever it named. Such a claim is REFUSED,
    # before anything is stored, so the sender's lane falls back to the relay,
    # where the platform checks the person. Tagged [refusal]: on a54f1d414 these
    # were delivered as the Host's own message instead.
    import server.circles_reading as _cr
    CLAIMED_KEY = getattr(_cr, "CLAIMED_KEY", "claimed_from_user_email")
    for label, row in (("no contact for that person", None),
                       ("a contact with no Host recorded",
                        {"peer_entity_id": None, "display_name": "Alice", "status": "active"}),
                       ("a contact on ANOTHER Host",
                        {"peer_entity_id": 999, "display_name": "Alice", "status": "active"})):
        _with_contact(row)
        r = deliver({"body": "hi", "from_user_email": "alice@example.com",
                     "from_user_name": "Alice", "to_user_email": "me@example.com"})
        check(f"[refusal] {label}: a message to our person naming alice@ is REFUSED with a plain "
              "reason, and nothing is stored",
              r.get("ok") is False and not captured
              and "relay" in (r.get("error") or "") and "person_stored" not in r, (r, captured))
    _with_contact(None)
    r = deliver({"body": "hi", "from_user_email": "alex@localhost", "to_user_email": "me@example.com"})
    check("[refusal] an address we cannot use, to one of our people, is refused too, nothing stored",
          r.get("ok") is False and not captured, (r, captured))
    r = deliver({"body": "hi", "from_user_email": "alice@example.com"})
    check("[guard] a person writing to this HOST with no contact is unchanged: delivered, the "
          "person kept unnamed, in our conversation with that Host",
          r.get("ok") is True and captured and captured[0].get("from_user_email") == "alice@example.com"
          and captured[0].get("thread_id") == dm_thread_id(9003, PEER)
          and "from_user_name" not in (captured[0].get("payload") or {}), (r, captured))

    _with_contact({"peer_entity_id": PEER, "display_name": "Alice", "status": "active"})
    r = deliver({"body": "hi", "from_user_email": "alice@example.com",
                 "to_user_email": "me@example.com"})
    top = captured[0] if captured else {}
    check("[guard] our contact places alice@ on THIS Host: the claim stands, in the People chat, "
          "with no claimed-address key",
          top.get("from_user_email") == "alice@example.com"
          and top.get("thread_id") == people_thread_id("alice@example.com", "me@example.com")
          and CLAIMED_KEY not in (top.get("payload") or {}) and r.get("person_stored") is True, top)

    _with_contact(None)
    r = deliver({"body": "hi", "payload": {CLAIMED_KEY: "someone@example.com",
                                           "person_checked_by": "relay"}})
    inner = captured[0].get("payload") if captured else {}
    check("[guard] a sender cannot plant the claimed address or the relay's mark itself",
          CLAIMED_KEY not in inner and "person_checked_by" not in inner, inner)

    print("\n── 6. the two ends share ONE rule for what an address is ──")
    import server.bridge.memory_handlers as MH
    check("handlers reuses the inbox's own _clean_email rather than a second copy",
          H._clean_email is MH._clean_email)

    print()
    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("peer-lane person checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
