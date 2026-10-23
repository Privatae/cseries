"""
Peer-transport op handlers. A handler runs ONLY after PeerServer has verified the
client cert (WHO = grantee) and the slip (WHAT = this host issued it, authorizes the
op's action, live + unrevoked). So the handler is the trusted, post-gate position —
this host is now the attestor (the bridge is not in this path).

Signature: async (peer_id: int, slip_info: dict, params: dict) -> dict
"""
from __future__ import annotations

import datetime as _dt

import asyncio
import logging

from server.bridge.memory_handlers import handle_inbox_append
# The inbox's OWN normalisation, not a second copy of it. Both ends of a
# host↔host message have to agree on what one mailbox address looks like, and a
# reimplementation here would be free to drift from the rule the column is
# actually written under (memory_handlers._clean_email).
from server.bridge.memory_handlers import _clean_email
from server.operator_identity import operator_allowlist as _operator_allowlist
from server.groups import lane as _group_lane
from server.peer import app_lane as _app_lane

log = logging.getLogger("peer.handlers")


#: What a DELIVERY may carry in its nested payload: NOTHING.
#:
#: Same reason as _ALLOWED_PAYLOAD_KEYS below, and it was missing here. inbox_request
#: has been allow-listed since the knock shipped (pinned by test_peer_lane_knock.py's
#: forged-key loop) while inbox_deliver splatted the caller's dict straight through —
#: so the op that carries an ACCEPTED relationship was the looser of the two.
#:
#: EMPTY rather than a short list. The first cut allowed
#: from_display_name / peer_display_name, reasoning that only the person keys name
#: somebody. That was wrong and _identify's own docstring below says why in advance:
#: those two ARE the name the reader sees. circles.py _list_entity_threads titles a
#: Hosts thread from COALESCE(from_display_name, peer_display_name), _people_thread
#: renders from_display_name as `via`, _entity_thread as peer_name, and
#: messages.py's conversations_service reads the same pair. A slip-holding peer
#: could have titled a thread "Privatae Support". The bridge never allowed it —
#: _enrich_and_verify_cross_host OVERWRITES from_display_name from entity_registry
#: — and three existing gates already pin the property for the sibling ops
#: (test_peer_lane_knock, test_peer_lane_grant, test_machine_sharing).
#:
#: So every name on a delivered message is now written BY US: the host's from
#: _identify (the receiver's own peer list), the person's from bound top-level
#: params. A future need adds a key deliberately, with a reader in mind.
#:
#: THE TOPIC, added that way: topic_id and
#: topic_title, and nothing else. Neither names anybody: no reader takes an
#: author, a sender or a thread's title from them (the readers above still read
#: only the keys this handler and _identify write). A topic is WHAT a message
#: is about inside the one conversation this pair shares, the same class of
#: text as the subject, and it is bounded the same way. The id cannot move a
#: message either: the thread below is still derived from who is on the
#: message, and every topic read takes the pair first (server/peer/topics.py).
#: Both keys are checked where every way in stores, handle_inbox_append
#: (topics.stored), because the relay passes the nested payload through.
#:
#: THE READING NOTICE: reading_notice, "on"
#: or "off", on the message that tells a person the other side's Host may now
#: read (or no longer reads) their chat. It names nobody either: the Host it
#: speaks of is the sending Host, which a reader names from _identify, never
#: from this key. Checked at storage too (circles_reading.stored): kept on a
#: person-to-person message only, and only as one of its two values.
_ALLOWED_DELIVER_PAYLOAD_KEYS: tuple[str, ...] = ("topic_id", "topic_title", "reading_notice")

#: A display name is shown, not matched, so it only needs to be bounded.
_MAX_USER_NAME = 120

#: The direct lane's answer to a person-to-person message whose person this Host
#: cannot place on the sending Host (inbox_deliver). Plain, and the same for every
#: such case, so it says nothing about which of our contacts exist.
PERSON_UNCHECKED_ERROR = ("this host cannot check that person on the sending host; "
                          "send it by the relay, which checks the person")


async def inbox_deliver(peer_id: int, slip_info: dict, params: dict) -> dict:
    """Deliver a host↔host message into this pod's local inbox. We have already
    verified the peer's slip authorizes `message` and was issued by us, so we hand
    that verification to handle_inbox_append in-process (verified_slip=), which
    admits it via the slip (the sole authority for host↔host messages). Same
    storage path the bridge relay uses — just reached directly.

    WHO VOUCHES FOR THE PERSON. A message may name the person
    who wrote it. On the bridge path the PLATFORM answered that question: its
    _person_on_host checked from_user_email against operator_entities in the
    platform DB. A pod cannot read that table, and the decision was that it should
    not have to — the host that owns the people vouches for them. So the two ends
    verify what each actually knows:

      * from_user_email  — the SENDER'S assertion about its OWN operator. We cannot
        check it against the platform. It is trustworthy exactly insofar as the
        sending host is, and mTLS already bound this connection to that host's cert
        (peer_server: peer_id comes from the client-cert CN, never from params).
        What we CAN check is our own contacts (_person_claim): a message to one of
        our people is taken only when our contact places them on this host, and
        is otherwise refused, so the sender's lane falls back to the relay.
      * to_user_email    — a person on THIS host, so WE are the authority and we
        check locally against our own operator allowlist. A message addressed to
        somebody who is not ours is refused rather than filed under a name we cannot
        account for, which is what the bridge did with the same case.

    The fail-closed direction is deliberate and it differs from is_operator's. That
    helper answers True when ENTITY_OPERATOR_EMAILS is unprovisioned, because a
    dashboard request has already passed the router and locking an operator out of
    their own pod would be worse (operator_identity.is_operator). Nothing stands in
    front of this lane, so "I cannot check" must not read as "yes" here.

    ONE KNOWN SOFTNESS, stated rather than left for a reader to find. The allowlist
    is an env var baked at pod render, where the bridge read operator_entities live.
    An operator added since the last reconcile has their inbound person-DMs refused
    here until the pod re-renders. It degrades softly rather than dropping mail —
    the refusal returns no data, the sender's direct lane falls back to the bridge,
    and the bridge's live check passes — so it is a lag, not a loss. Worth revisiting
    if pods start living long enough for it to bite; measured, all eight
    live pods carry the var.
    """
    to_user_email = _clean_email(params.get("to_user_email"))
    if to_user_email:
        allow = _operator_allowlist()
        if allow is None:
            log.warning("peer inbox_deliver from=%s refused: addressed to a person "
                        "(%s) but this pod has no operator allowlist to check against",
                        peer_id, to_user_email)
            return {"ok": False, "blocked": True,
                    "error": "cannot verify the addressee on this host"}
        if to_user_email not in allow:
            log.info("peer inbox_deliver from=%s refused: %s is not an operator here",
                     peer_id, to_user_email)
            return {"ok": False, "blocked": True,
                    "error": "that person is not on this host"}

    _sent_inner = params.get("payload")
    inner = {k: v for k, v in (_sent_inner if isinstance(_sent_inner, dict) else {}).items()
             if k in _ALLOWED_DELIVER_PAYLOAD_KEYS}
    # WHO THE SENDING HOST IS, from our own peer list — never from the sender.
    # Same call inbox_request makes, for the same reason, and it also restores what
    # the bridge leg injects (from_display_name / from_handle): without it a thread
    # that arrived direct titles "Host 9122" while the same peer's bridge-delivered
    # messages titled with its real name, in one inbox the readers cannot tell apart.
    #
    # ONE DIVERGENCE REMAINS AND IT IS IRREDUCIBLE HERE: the relay names a peer from
    # entity_registry.display_name, this names it from the handle its own bastion
    # announced. There is no way to read the registry from a pod, and putting the
    # name back on the message so the sender supplies it is precisely the hole the
    # empty allowlist above closes. A peer that has never been announced to us is
    # "Host <id>" — honest, and never an address.
    #
    # And that fallback is the ONLY way to reach it, so do not "improve" it with a
    # peer_host_name fallback: peer_host_name is NOT NULL in the schema and
    # list_peerings already derives peer_handle as
    # `peer_handle or peer_host_name.split(".")[0]`, so a MATCHING row always
    # yields a non-empty handle. "Host <id>" means there was no row at all, and
    # then there is no host name to fall back to either.
    inner.update(await _identify(peer_id))

    # The sender's claim about its own person, written by US into the nested payload
    # so the reader can never receive a key the sender placed there directly.
    # From the RAW param, not the cleaned one. Computing it after _clean_email made
    # a malformed address read as "no person was claimed", so the flag went ABSENT
    # and the sender diagnosed "old image" for what was actually its own address
    # failing our validation — two meanings for one absence. The two ends validate
    # differently (the sender puts the router's X-Authentik-Email on the wire as-is
    # and never runs _clean_email on it), so the divergence is real even though no
    # address the platform issues today can reach it.
    claimed_person = bool(params.get("from_user_email"))
    from_user_email = _clean_email(params.get("from_user_email"))
    # BLOCK. The person named here is this Host's word for its own operator, so
    # while the Host is blocked the claim counts only for a person our contacts
    # place on that Host; anything else is the Host's own message and refused
    # (contacts.block_refuses). Weighed on the claim as sent, before
    # _person_claim may drop it, so a message naming a blocked person is refused
    # whichever Host carries it. The sender's fallback is the relay, where the
    # platform checks the person itself.
    refusal = await _block_refusal(peer_id, from_user_email, host_vouches_for_person=True)
    if refusal is not None:
        return refusal
    # A PERSON WRITING TO ONE OF OUR PEOPLE is taken only when our contact places
    # that person on this Host (_person_claim "on_host"). The People chat is keyed
    # on the two addresses alone, so any other claim (no contact, a contact with
    # no Host recorded, a contact on another Host, an address we cannot use, or a
    # check that could not run) would let one Host write into our person's chat
    # with whoever it named. It is REFUSED here, before anything is stored, and
    # the sender's direct lane falls back to the relay (routes/circles.py
    # send_message, peer/deliver.py: a refusal returns no data), where the
    # platform checks the person itself: a genuine person still reaches the chat,
    # a spoof arrives without the person.
    standing, ours = ("unknown", None)
    if from_user_email:
        standing, ours = await _person_claim(from_user_email, peer_id)
    if claimed_person and to_user_email and standing != "on_host":
        log.info("peer inbox_deliver from=%s refused: a person-to-person message whose "
                 "person this host cannot place on the sender (%s)", peer_id, standing)
        return {"ok": False, "error": PERSON_UNCHECKED_ERROR}
    if from_user_email:
        # A person writing to our HOST (no addressee) keeps an unrefuted claim,
        # unnamed, as before: that message shows in this Host's conversation,
        # "via" this Host. A refuted one is this Host's own message, carrying the
        # address it named (circles_reading.CLAIMED_KEY), so the reader shows
        # "<Host> says this is from <address>".
        keep = standing in ("on_host", "unknown")
        if not keep:
            from server.circles_reading import CLAIMED_KEY
            inner[CLAIMED_KEY] = from_user_email
            from_user_email = None
        else:
            inner["from_user_email"] = from_user_email
            if to_user_email:
                # Checked against our contact NOW, and marked so, so the message
                # stays the person's if the contact is later removed
                # (circles_reading.CHECKED_BY_CONTACT).
                from server.circles_reading import CHECKED_KEY, CHECKED_BY_CONTACT
                inner[CHECKED_KEY] = CHECKED_BY_CONTACT
            name = params.get("from_user_name")
            if isinstance(name, str) and name.strip():
                inner["from_user_name"] = name.strip()[:_MAX_USER_NAME]
            elif ours:
                # The sender named a person but not their display name (the pod has
                # no users.name row for its own operator the way the bridge did).
                # Without a name the People SIDEBAR, not just the author line, falls
                # back to the raw address (_list_people_threads titles from
                # COALESCE(from_user_name, peer_user_name)), so one thread would
                # show a name for the messages that came by bridge and an address
                # for the ones that came direct.
                inner["from_user_name"] = ours[:_MAX_USER_NAME]

    payload = {
        "from_entity_id": int(peer_id),
        "from_address": f"{int(peer_id)}@entity.localhost",
        # Capped like every other piece of another tenant's free text on this lane
        # (_MAX_SUBJECT, same as the knock and the grant). Nothing downstream bounds
        # it — the only other limit is the 256KB frame — and the module comment below
        # says exactly that about the sibling ops. The one live caller now sends the
        # constant "Direct message", so this costs nothing and closes the asymmetry.
        "subject": (params.get("subject") or "(message)").strip()[:_MAX_SUBJECT] or "(message)",
        "body": params.get("body") or "",
        "body_format": params.get("body_format") or "text/markdown",
        "message_kind": "message",
        "payload": inner,
        # DERIVED, NOT ACCEPTED — the last field on this op where the receiver took
        # the sender's word for something it can compute itself, and the same family
        # as the two before it.
        #
        # Both thread ids are deterministic uuid5 over a SORTED pair (messages.
        # dm_thread_id over the two entity ids, people_thread_id over the two
        # addresses), so anyone who knows the pair can compute one — and a peer knows
        # them. Accepting it let a slip-holding peer B send dm_thread_id(A, C) and
        # land inside A's conversation with C: conversations_service does
        # SELECT DISTINCT ON (thread_id) and takes the peer from the NEWEST row, so
        # A's entry for C flips to B, and thread_service interleaves B's message into
        # the C thread. Both docstrings name themselves the source of truth for the
        # host_message tool, so that is the surface the MODEL reads. Circles is
        # unaffected — it groups by entity pair and email pair — which makes it
        # worse, not better: one row under B in Circles and inside C's thread in the
        # tool.
        #
        # Deriving costs nothing: both ends compute the same value from the same
        # inputs, which is what the relay's own comment already claims is happening
        # ("the pod derives it deterministically from the two ends"). A refuted person
        # claim collapses to the host form, which is exactly where that row files.
        # A topic in the payload lives INSIDE this thread and never chooses it.
        "thread_id": _thread_id_for(peer_id, from_user_email, to_user_email),
        "message_id": params.get("message_id"),
    }
    if from_user_email:
        payload["from_user_email"] = from_user_email
    if to_user_email:
        payload["to_user_email"] = to_user_email
    # The slip peer_server verified goes to the inbox gate IN-PROCESS, never as
    # payload fields a peer could set.
    result = await handle_inbox_append(payload, verified_slip=slip_info)
    if not result.get("ok"):
        log.info("peer inbox_deliver from=%s not stored: %s", peer_id, result.get("error"))
        return result
    # TELL THE SENDER WHAT WE ACTUALLY DID WITH THE PERSON. An image from before
    # this change reads neither top-level person field, so it stores the message
    # host-authored and still answers ok:True — and the sender, seeing success,
    # files ITS copy under People. One message, two different lists, and nothing
    # visible to say why. The sender reconciles on this flag; its absence is what
    # an older receiver looks like.
    # PRESENT-AND-FALSE vs ABSENT are different answers, and conflating them is what
    # made this a choice between two bad options. A receiver carrying this change
    # ALWAYS sets the key: True when it kept the person, False when it refuted a
    # person-to-Host claim or could not use the address (a person-to-person claim
    # it cannot place is refused above, never stored). Absence therefore means exactly one thing — an
    # image that never read the field. The sender needs the distinction because the
    # right response differs: an old image did not understand us (transient, worth
    # matching), a False understood us and did not keep the person (the sender keeps
    # its own copy as sent; routes/circles.py _try_direct_peer_send).
    if claimed_person:
        result = {**result, "person_stored": bool(from_user_email)}
    return result


#: The kinds a knock may carry. Deliberately tiny: these are the social-graph
#: entry points the inbox gate already admits without a slip because "the
#: relationship doesn't exist" yet (memory_handlers.py:412-440). Anything else —
#: above all `message` — must never arrive this way, because `message` is
#: slip-only BY DESIGN and this op has no slip. An unknown kind is refused rather
#: than forwarded; forwarding would let the caller pick which gate judges it.
#:
#: `machine_request`: one host asking another to let
#: it use a model on one of their machines. It belongs here for the same reason
#: endpoint_request does — it carries no credential and authorizes nothing; it
#: puts a card in front of a person who decides. It is gated HARDER than the
#: other kind, though, and the gate is below: an existing, accepted contact.
#: Asking a stranger for their GPU is not a first contact, and the knock's
#: standing allowance for strangers exists for first contact.
#:
#: `group_join` is a host asking to join a group circle
#: whose roster this host owns. It belongs here for the reason endpoint_request
#: does and NOT for the reason machine_request does: it carries no credential,
#: authorizes nothing, and puts a card in front of a person who decides. It is
#: deliberately NOT in _KINDS_NEEDING_A_CONTACT — asking to join a circle is a
#: FIRST contact, and requiring an accepted contact first would make the whole
#: op pointless, because an accepted contact can already be added by the owner
#: (routes/groups.py add_member). Its gate is _group_join_target instead, which
#: is stronger than the knock's standing allowance: the card is refused unless
#: it names a group THIS host owns.
REQUEST_KINDS = ("endpoint_request", "machine_request", "group_join")

#: Kinds that need an existing relationship, checked before the card is written.
_KINDS_NEEDING_A_CONTACT = ("machine_request",)

#: The ONLY inner-payload keys a knock may carry. Everything else is dropped.
#:
#: The bridge path strips the person fields for exactly this reason: "a host
#: could present itself as a named person in a connection request" (bridge
#: adapter.py:4921-4927). This lane spread the sender's payload verbatim and
#: reintroduced that on a new path — a known bug, repeated. The card reads its
#: name straight out of it (inbox-pane.tsx:244-247: from_display_name ??
#: display_name ?? from_handle ?? handle), and Accept then writes THAT handle
#: into `contacts` (unique on handle alone) and into the domain_policy allow
#: rule. So a sender who picks those keys picks the name the user sees, the
#: contact that gets written, and the handle that gets allowed.
#:
#: An allow-list, not a deny-list: a deny-list has to be updated every time a
#: renderer starts reading a new key, and it will not be.
#:
#: `models_asked` is a list of model names the sender HOPES to use. It
#: is safe to carry for the reason the two above are: it is not a name the card
#: reads an identity out of and not a handle Accept writes anywhere. It is still
#: the sender's word and nothing more — the scope that ends up in the signed
#: grant is whatever the RECEIVER picks off their own machine's heartbeat
#: (machine_offers.accept_inbound). Sanitised and bounded at
#: machine_offers.clean_models_asked, which is where every other field of an
#: offer is bounded too.
#: `group_id` is which group the sender is asking to join. It is
#: safe to carry for the reason `models_asked` is — it is not a name the card
#: reads an identity out of, and not a handle Accept writes anywhere — and it is
#: bounded harder than any other key here: _group_join_target refuses the whole
#: knock unless the value matches _GROUP_ID_RE *and* names a group this host
#: owns, so what reaches the card is a group id this host itself minted.
_ALLOWED_PAYLOAD_KEYS = ("via", "machine", "models_asked", "group_id")

#: The card's headline when the sender did not write one. Per kind, because the
#: inbox list shows the subject and "Connection request" over a group_join is
#: simply wrong — the user is being asked a different question.
_DEFAULT_SUBJECT = {
    "endpoint_request": "Connection request",
    "machine_request": "Machine request",
    "group_join": "Request to join a group",
}

#: Free text a requester may attach. Capped HERE, at the trust boundary, because
#: the sender is another tenant and nothing downstream bounds it.
_MAX_BODY = 500
_MAX_SUBJECT = 120

#: How long one unanswered knock holds a peer's slot. See _knock_slot_free: this
#: exists so an un-archived or aged-out card cannot lock a peer out permanently.
#: A timedelta, NOT the string "7 days": asyncpg binds `$n::interval` only from a
#: timedelta and raised DataError on the string, so the cap query always failed,
#: _knock_slot_free fell open, and the one-knock cap was never enforced — any
#: cert holder could flood an inbox with cards (measured, PG 18).
KNOCK_SLOT_WINDOW = _dt.timedelta(days=7)


async def inbox_request(peer_id: int, _slip_info, params: dict) -> dict:
    """A slipless KNOCK: one host asking another to connect.

    This is the op that closes the peer lane's BOOTSTRAP. Not its whole round
    trip: the grant that carries the slip back still goes by bridge
    (contacts.py accept path), as does the requester's reciprocal grant
    (memory_handlers.py). So in the measured topology the card lands and the user
    can accept, and the grant can still fail to arrive — the knock is one of three
    cross-pod messages, and this fixes one. Everything else on the lane
    is slip-gated, and a slip exists only after someone ACCEPTS a request — so
    without this, the one message that STARTS a relationship had to travel by
    bridge, which cannot route between its own DaemonSet pods
    (peer_server._SLIPLESS_OPS carries the measurement and the reasoning).

    This handler stands in a WEAKER position than the others and the code says so:
    there is no slip, so this host is NOT the attestor. It asserts only what mTLS
    proved — WHO — and hands the message to the SAME path the bridge uses, so
    nothing arrives by a route the bridge could not already have taken.
    Deliberately no slip_authorized and no sender_verified: those flags mean "a
    slip was checked", and none was.

    Do NOT read that as "domain_policy gates this". It does not: request kinds
    return True out of check_inbound_allowed before any table is read
    (domain_policy.py:176-177), on this lane and on the bridge alike. The controls
    that actually exist here are the kind allow-list above, the payload
    allow-list, and the per-peer live-request cap below.
    """
    inner = params.get("payload") or {}
    if not isinstance(inner, dict):
        inner = {}
    kind = (params.get("message_kind") or "endpoint_request").strip()
    if kind not in REQUEST_KINDS:
        log.info("peer inbox_request from=%s REFUSED: %r is not a request kind", peer_id, kind)
        return {"ok": False, "error": f"'{kind}' is not a request kind on this lane"}
    if kind in _KINDS_NEEDING_A_CONTACT and not await _connected_with(peer_id):
        log.info("peer inbox_request from=%s REFUSED: %s needs an accepted contact", peer_id, kind)
        return {"ok": False, "error": "we are not connected — send a connection request first, "
                                      "and ask about a machine once it is accepted"}
    # A group_join names a group, and the gate is that we OWN it. Run before
    # anything is written, like the contact precondition above: a card this host
    # cannot make sense of must never reach the user's inbox.
    join_group = None
    if kind == "group_join":
        join_group, refusal = await _group_join_target(peer_id, inner)
        if refusal is not None:
            return refusal
    # The sender's own words, cleaned at the boundary by the module that bounds
    # them. Done for EVERY kind that carries the key, not just machine_request:
    # the allow-list lets it through on an endpoint_request too, and an
    # unsanitised value would be stored on that card verbatim.
    if "models_asked" in inner:
        inner = {**inner, "models_asked": _sanitised_models_asked(inner.get("models_asked"))}
    payload = {
        # from_entity_id is the CERT's identity, never the caller's claim — stronger
        # attribution than the bridge path, which forwards an id asserted by the
        # sending pod.
        "from_entity_id": int(peer_id),
        "from_address": f"{int(peer_id)}@entity.localhost",
        "subject": (params.get("subject")
                    or _DEFAULT_SUBJECT.get(kind, "Connection request")).strip()[:_MAX_SUBJECT],
        "body": (params.get("body") or "").strip()[:_MAX_BODY],
        "body_format": "text/plain",
        "message_kind": kind,
        # CARRIED, and it is the whole of the send's idempotency. contacts.py mints one
        # id and sends it down BOTH transports; a read timeout after the receiver
        # committed then makes the bridge retry a no-op instead of a second card.
        # Dropping it here left that comment describing something the lane did not
        # do — handle_inbox_append only dedupes on a SUPPLIED id
        # (memory_handlers.py:442,453). inbox_deliver has always carried it (:36).
        "message_id": params.get("message_id"),
        # Allow-listed, and the sender's own `via` is KEPT (it is the provenance
        # the receiver's card will eventually explain itself with); `lane` records
        # the transport separately instead of overwriting it.
        "payload": {**{k: inner[k] for k in _ALLOWED_PAYLOAD_KEYS if k in inner},
                    **(await _identify(peer_id)),
                    "lane": "peer"},
    }
    # ONE LIVE KNOCK PER PEER, PER KIND.
    #
    # Nothing else limits this. There is no dedupe (handle_inbox_append conflicts
    # only on a message_id a knock need not supply), no throttle in the connection
    # handler, and every stored row fans out a dashboard push — so without this,
    # "any valid cert may append a request card" means "may append them without
    # limit", and a couple of hundred fills the inbox window. The per-target
    # serialization the bridge path has is also gone on this lane.
    #
    # The cap is one, not a rate: a second pending "X wants to connect" from the
    # same peer is not new information, it is the same question again. Answering
    # it — accept or archive — frees the slot immediately, so an honest retry
    # after a timeout still lands and a flood does not.
    # Serialized per peer: the cap is a count followed by an insert, and
    # PeerServer spawns a task per connection with no limit, so N simultaneous
    # knocks from one cert all counted zero and all inserted. One process per pod,
    # so a plain asyncio lock is the whole fix.
    async with _peer_lock(peer_id):
        res = await _append_knock(peer_id, kind, payload, group_id=join_group)
        # A machine_request has a ROW as well as a card, and the row is
        # what the user's answer acts on — a card with no row behind it is an
        # Approve button that does nothing.
        #
        # Written AFTER the card and INSIDE the same lock, both deliberately.
        # After, because _append_knock can still refuse (the slot is taken), and
        # a row written before that refusal is an orphan: open, cardless, and
        # holding the 409 that blocks the peer's next ask. Inside, because the
        # row's own unique index is the only thing stopping two simultaneous
        # asks, and the lock is what makes card and row agree.
        if kind == "machine_request" and res.get("ok") is not False:
            refusal = await _record_machine_offer(peer_id, params, inner)
            if refusal is not None:
                return refusal
        return res


_LOCKS: dict[int, "asyncio.Lock"] = {}


def _peer_lock(peer_id: int):
    return _LOCKS.setdefault(int(peer_id), asyncio.Lock())


#: The kinds an inbox.grant may carry — the answer to a request, and nothing
#: else. `endpoint_grant` is the one that closes first contact.
GRANT_KINDS = ("endpoint_grant",)

#: The ONLY inner-payload keys a grant may carry. `contact_slip` is the whole
#: point: it is the credential the requester will present on every later message
#: (memory_handlers stores it into held_slips keyed by the VERIFIED sender id).
#: `kind` rides so the receiver records the right sort of contact.
#:
#: Everything else is dropped, for the reason the knock's allow-list exists: the
#: card reads its name out of the payload, and Accept writes that name into
#: `contacts` and the domain_policy allow. A sender must never choose either.
_ALLOWED_GRANT_KEYS = ("contact_slip", "kind", "card")


async def inbox_grant(peer_id: int, _slip_info, params: dict) -> dict:
    """The ANSWER to a knock: "your request was accepted", carried here instead
    of by bridge.

    This is the other half of first contact. inbox.request made the knock
    deliverable; without this the acceptance still went by bridge, which holds
    its connections per DaemonSet pod with no forwarding between them — so the
    card landed, the user accepted, and the requester was never told. The accept
    path already knows: it sets grant_delivered=False and the UI shows it.

    WHY IT CANNOT USE inbox.deliver. That op is slip-gated and the slip must be
    issued BY the receiver (peer_server verifies expected_issuer=self). At grant
    time the accepter holds no slip from the requester — the requester only
    knocked, and a knock grants nothing. So the reply needs its own slipless op,
    which widens the slipless surface and therefore has to be gated harder than
    the knock is.

    AND IT IS, on a precondition the knock cannot have: WE MUST HAVE ASKED.
    request_peer_service records every outbound request as a `contacts` row with
    that peer_entity_id and status 'pending' (contacts.py). So a grant is
    admitted only from a peer we are actually waiting on. A stranger holding a
    valid CEIGAS cert has no pending row here and is refused outright — where the
    knock can only be capped, this one can be genuinely preconditioned.

    HOST GRANTS ONLY, deliberately. For kind='human' the accept path stamps
    from_user_email and THE BRIDGE VERIFIES that address is an operator of that
    host before relaying it. This lane proves which ENTITY is calling, from the
    client cert, and cannot attest which PERSON. Carrying a human grant here
    would assert something no one checked, so human grants stay on the bridge and
    human↔human first contact stays partitioned until the bridge can forward
    across its own pods. Saying that plainly is better than quietly widening it.
    """
    inner = params.get("payload") or {}
    if not isinstance(inner, dict):
        inner = {}
    kind = (params.get("message_kind") or "endpoint_grant").strip()
    if kind not in GRANT_KINDS:
        log.info("peer inbox_grant from=%s REFUSED: %r is not a grant kind", peer_id, kind)
        return {"ok": False, "error": f"'{kind}' is not a grant kind on this lane"}
    contact_kind = str(inner.get("kind") or "host").strip()
    if contact_kind != "host":
        log.info("peer inbox_grant from=%s REFUSED: kind=%r needs the bridge", peer_id, contact_kind)
        return {"ok": False,
                "error": "only host grants travel this lane; a person's grant needs the "
                         "bridge, which verifies the address belongs to that host"}
    payload = {
        # The CERT's identity, never the caller's claim — same as the knock.
        "from_entity_id": int(peer_id),
        "from_address": f"{int(peer_id)}@entity.localhost",
        "subject": (params.get("subject") or "Connection accepted").strip()[:_MAX_SUBJECT],
        "body": (params.get("body") or "").strip()[:_MAX_BODY],
        "body_format": "text/plain",
        "message_kind": kind,
        "message_id": params.get("message_id"),
        "payload": {**{k: inner[k] for k in _ALLOWED_GRANT_KEYS if k in inner},
                    **(await _identify(peer_id)),
                    "lane": "peer"},
    }
    # THE PRECONDITION IS CHECKED INSIDE THE LOCK. The check once sat above,
    # outside, so two grants racing from one peer both passed it and both
    # stored. The knock has always done check-and-append inside its lock; this
    # now matches, and the comment below about serializing is finally true of
    # the whole decision rather than only of the write.
    async with _peer_lock(peer_id):
        if not await _relationship_open_with(peer_id):
            log.info("peer inbox_grant from=%s REFUSED: no open request with them", peer_id)
            return {"ok": False,
                    "error": "no connection request is open with you"}
        # handle_inbox_append writes the carried slip into held_slips with an
        # upsert, and two grants racing from one peer would interleave.
        result = await handle_inbox_append(payload)
    if not result.get("ok"):
        log.info("peer inbox_grant from=%s not stored: %s", peer_id, result.get("error"))
    return result


async def contact_slip(peer_id: int, _slip_info, params: dict) -> dict:
    """The RECIPROCAL slip: the requester hands the host that accepted it a
    message slip back, so the accepter can message first as well as reply.

    Until this op existed it went by bridge only (memory_handlers._reciprocate
    -> inbox.send), and the relay reaches only entities on its own DaemonSet pod
    — so across nodes the accepter never got a slip, and its first message was
    refused as "host-to-host message requires a live permission slip". The
    connection looked broken from the side that said yes.

    SLIP-GATED on `message`, the slip WE issued the requester in our grant — so
    only a host we accepted can call it, and peer_server has verified that slip
    offline before this runs. What is checked HERE is the slip it CARRIES, by the
    shape check group_grant and app_grant make: it must be issued by the host
    presenting it, to us, for exactly dm:contact / message. So a peer cannot park
    a third host's slip in our held_slips under its own id, nor hand us a slip
    for anything wider than messaging it. Its signature is checked where it is
    PRESENTED — by the requester's own pod, against the bridge key.

    Identity comes from the cert and our own records (_identify), never from
    the caller: the stored contact's handle and name are not the sender's to set.
    """
    from server import brain_service
    carried = params.get("contact_slip")
    if not isinstance(carried, dict) or not isinstance(carried.get("terms"), dict):
        return {"ok": False, "error": "contact_slip missing or malformed"}
    terms = carried["terms"]
    try:
        issuer, grantee = int(terms.get("issuer", -1)), int(terms.get("grantee", -1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "contact_slip terms malformed"}
    if issuer != int(peer_id):
        return {"ok": False, "error": "contact_slip issuer is not the presenting host"}
    if grantee != int(brain_service._entity_id):
        return {"ok": False, "error": "contact_slip grantee is not this host"}
    if terms.get("resource") != "dm:contact" or list(terms.get("allowed_ops") or []) != ["message"]:
        return {"ok": False, "error": "contact_slip must be dm:contact for message only"}
    payload = {
        "from_entity_id": int(peer_id),
        "from_address": f"{int(peer_id)}@entity.localhost",
        "subject": "Connection confirmed",
        "body": "You can message this host now.",
        "body_format": "text/plain",
        "message_kind": "permission_grant",
        "message_id": params.get("message_id"),
        "payload": {"contact_slip": carried, **(await _identify(peer_id)), "lane": "peer",
                    # What the requester shares about itself on the accepted
                    # connection; server/hosts/peer_cards.py cleans and checks it.
                    **({"card": params["card"]} if isinstance(params.get("card"), dict) else {})},
    }
    # Serialized per peer with the grant path: both write held_slips for this
    # peer with an upsert.
    async with _peer_lock(peer_id):
        result = await handle_inbox_append(payload)
    if not result.get("ok"):
        log.info("peer contact_slip from=%s not stored: %s", peer_id, result.get("error"))
    return result


# How long an unanswered connection request keeps admitting grants from that
# peer. A request nobody has answered in a month is dead, and the row is what
# admits a SLIPLESS op, so it must not outlive the conversation it belongs to.
#
# It bounds a real hole rather than a theoretical one: the pending row is meant
# to close when the grant lands (upsert_contact_from_grant), and that can SKIP —
# a missing from_handle, or handle-case drift against the case-sensitive
# ux_contacts_handle index. A stranded row "keeps the peer admitted
# indefinitely". Now it keeps them admitted for thirty days.
_GRANT_WINDOW_DAYS = 30


async def _relationship_open_with(peer_id: int) -> bool:
    """True when a connection request with that peer is open and still recent.

    THE NAME AND THE CLAIM BOTH CHANGED HERE, because the old ones were wrong.
    This was `_we_asked_them`, and the commit message said a grant is "admitted
    only from a peer we are waiting on". The query never checked direction and
    could not: the schema that defines the column says so in as many words —
    migrations/v048_circles_contacts_policy.sql:20, "pending = we accepted an
    endpoint_request / sent one, grant not confirmed". So a peer whose knock WE
    accepted is admitted too, while our own reply is in flight
    (accept_request_service, server/routes/contacts.py:438).

    That is still sound, and it is worth saying WHY rather than quietly widening
    the words to fit: the property that makes a slipless grant safe is not who
    spoke first, it is that THE USER ACTED. Both writers of a pending row are a
    user's decision — one to ask, one to accept. A stranger holding a valid
    CEIGAS cert has no row at all, which is the case this exists to refuse.

    FAILS CLOSED, unlike the knock's slot check. A grant installs a credential —
    handle_inbox_append writes its contact_slip into held_slips — so a check that
    cannot run is not a reason to admit one. The knock's equivalent fails open
    because a knock confers nothing and refusing a first contact on a cold pool
    would be worse than allowing it; the asymmetry is deliberate.
    """
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        log.info("grant precondition unavailable (no pool) — refusing")
        return False
    try:
        n = await pool.fetchval(
            # COALESCE because the two writers disagree about last_contact_at:
            # the outbound request sets it, the accept path did not until this
            # change, and an ON CONFLICT re-pending never touches created_at. The
            # most recent evidence of activity is the honest clock.
            "SELECT count(*) FROM contacts "
            " WHERE peer_entity_id = $1 AND status = 'pending' "
            "   AND COALESCE(last_contact_at, created_at) > now() - ($2 || ' days')::interval",
            int(peer_id), str(_GRANT_WINDOW_DAYS))
    except Exception as e:  # pragma: no cover - defensive
        log.info("grant precondition check failed (%s) — refusing", type(e).__name__)
        return False
    return bool(n)


def _sanitised_models_asked(v):
    """The one allow-listed payload key a machine_request adds, cleaned by the
    module that owns its bounds. Never trusted for anything but display: the
    scope that reaches the signed grant is what the RECEIVER picks off their own
    machine's heartbeat."""
    from server.network import machine_offers
    return machine_offers.clean_models_asked(v)


async def _record_machine_offer(peer_id: int, params: dict, inner: dict):
    """Write the offer row a machine_request card will be answered from.
    Returns None on success, or the refusal dict to send back."""
    from server import brain_service
    from server.network import machine_offers
    pool = brain_service.get_pool()
    if pool is None:
        log.info("machine_request from=%s REFUSED: no pool", peer_id)
        return {"ok": False, "error": "this host is not ready"}
    ident = await _identify(peer_id)
    try:
        await machine_offers.record_inbound(
            pool, peer_id,
            purpose=(params.get("body") or ""),
            models_asked=inner.get("models_asked") or [],
            peer_handle=str(ident.get("from_handle") or ""),
            peer_display=str(ident.get("from_display_name") or ""))
    except machine_offers.OfferError as e:
        log.info("machine_request from=%s REFUSED: %s", peer_id, e.message)
        return {"ok": False, "error": e.message}
    except Exception as e:  # pragma: no cover - defensive
        log.warning("machine_request from=%s could not be recorded: %s", peer_id, type(e).__name__)
        return {"ok": False, "error": "could not record the request"}
    return None


async def _block_refusal(peer_id: int, person: str | None = None, *,
                         host_vouches_for_person: bool = False) -> dict | None:
    """The answer a block gives traffic from `peer_id` (written by `person`), or
    None when no block applies. The rule is contacts.block_refuses.

    FAILS CLOSED when the check cannot run, like the grant precondition: a
    blocked Host is not let in because a read failed, and the sender can try
    again. With no pool at all it returns None, because no caller can store or
    mint anything without one and each already refuses on its own."""
    from server import brain_service
    from server.routes.contacts import block_refuses, blocked_refusal
    pool = brain_service.get_pool()
    if pool is None:
        return None
    try:
        refused = await block_refuses(pool, int(peer_id), person,
                                      host_vouches_for_person=host_vouches_for_person)
    except Exception as e:
        log.info("block check from=%s failed (%s) — refusing for now", peer_id, type(e).__name__)
        return {"ok": False, "retryable": True,
                "error": "the sender could not be checked right now — try again"}
    if refused:
        log.info("peer from=%s refused: blocked", peer_id)
        return blocked_refusal()
    return None


async def _connected_with(peer_id: int) -> bool:
    """True when this host holds an ACTIVE contact with that peer.

    Stricter than _relationship_open_with on purpose: that one admits a pending
    relationship because it gates the message that COMPLETES one. This gates a
    request to use someone's hardware, which is not a first contact and has no
    reason to be reachable by a stranger holding a valid platform cert.

    FAILS CLOSED, for the same reason the grant precondition does: where the
    plain knock fails open (refusing a first contact on a cold pool would be
    worse than allowing it), nothing is lost by making this one wait — the
    asker can send it again, and until then the user is not shown a card from
    someone this host cannot confirm it knows."""
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        log.info("machine-request precondition unavailable (no pool) — refusing")
        return False
    try:
        n = await pool.fetchval(
            "SELECT count(*) FROM contacts WHERE peer_entity_id = $1 AND status = 'active'",
            int(peer_id))
    except Exception as e:  # pragma: no cover - defensive
        log.info("machine-request precondition failed (%s) — refusing", type(e).__name__)
        return False
    return bool(n)


async def _group_join_target(peer_id: int, inner: dict) -> tuple[str | None, dict | None]:
    """The gate on a `group_join` knock. Returns (group_id, refusal).

    A group_join names a group, and the ONLY groups it may name are the ones
    whose roster this host owns. That is what replaces the accepted-contact
    precondition the other gated kind uses: a stranger may knock, but only
    about a room this host is actually the door to.

    Refusing an unknown group id here also bounds the knock slot. The slot for
    this kind is per (peer, group) rather than per peer, because a host may
    legitimately want to join two different circles and one shared slot would
    make the second ask look like a duplicate of the first. Per-group is only
    safe BECAUSE of this check — without it the group id would be an attacker-
    chosen string and "one slot per group" would mean no slot at all.

    THREE OF THE FOUR REFUSALS ARE ONE SENTENCE. "no valid group id" is about
    the request. "no such group", "not ours" and "that group is closed" are all
    answered with the same words, because any wording that separated them would
    confirm to a caller that an id exists, or that this host is its owner — the
    thing a prober would enumerate for.

    A CURRENT MEMBER IS TOLD PLAINLY, and that is not an exception to the rule
    above: it is reachable only by a host already on the roster, which knows the
    group exists because it is in it. Silence there would leave a member
    re-asking for ever with no idea they were already in.

    FAILS CLOSED, like the machine-request precondition and for the same
    reason: nothing is lost by making the asker try again, and until then the
    user is not shown a card this host cannot make sense of.
    """
    from server import brain_service
    from server.groups import roster as R
    gid = inner.get("group_id")
    if not R.valid_group_id(gid):
        log.info("peer inbox_request from=%s REFUSED: group_join names no valid group", peer_id)
        return None, {"ok": False, "error": "a join request must name a group"}
    pool = brain_service.get_pool()
    if pool is None:
        log.info("group_join precondition unavailable (no pool) — refusing")
        return None, {"ok": False, "retryable": True, "error": "this host is not ready"}
    try:
        g = await R.get_group(pool, gid)
        if g is None or int(g["owner_entity_id"]) != int(brain_service._entity_id):
            # One sentence for "no such group" and "not ours", deliberately: the
            # difference between them tells a caller whether a group id exists on
            # this host, which is exactly what a prober would enumerate for.
            log.info("peer inbox_request from=%s REFUSED: group_join for %s which this host does not own",
                     peer_id, gid)
            return None, {"ok": False, "error": "this host does not host that group"}
        if g.get("revoked_at") is not None:
            # THE SAME SENTENCE, for the same reason. A distinct "that group is
            # closed" would confirm that the id exists AND that this host owns
            # it, which is exactly what the shared refusal above exists to hide.
            log.info("peer inbox_request from=%s REFUSED: group_join for %s which is closed",
                     peer_id, gid)
            return None, {"ok": False, "error": "this host does not host that group"}
        if await R.is_current_member(pool, gid, int(peer_id)):
            # This one CAN be specific, and the difference is worth stating: it
            # is reachable only by a host already on the roster, which knows the
            # group exists because it is in it. It leaks nothing it did not
            # already have, and silence here would be a member re-asking for
            # ever with no idea they were already in.
            return None, {"ok": False, "error": "you are already a member of that group"}
    except Exception as e:  # pragma: no cover - defensive
        log.info("group_join precondition failed (%s) — refusing", type(e).__name__)
        return None, {"ok": False, "retryable": True, "error": "could not check that group"}
    return gid, None


async def machine_token(peer_id: int, _slip_info, params: dict) -> dict:
    """Mint one bastion enrolment token for a peer who is approving our request
    to use their machine.

    SLIPLESS, and preconditioned rather than credentialed — the same shape
    inbox.grant uses, and for a sharper version of the same reason: the thing
    this op hands back IS a credential, so it cannot be gated by one the caller
    would have to already hold. The precondition is that THIS host has an open,
    recent machine request out to that peer, which only a user of this host can
    create. A stranger with a valid CEIGAS cert has no such row and gets
    nothing.

    Everything else about the bound — how often, how many, how long the row
    stays valid — lives in machine_offers.mint_for_peer with the reasoning.

    The response is the ONE place on this lane that carries a secret. It is
    never logged here and never stored on either side: the caller redeems it
    within seconds (machine_offers.accept_inbound) and the token is single-use
    with a 15-minute life of its own.
    """
    from server import brain_service
    from server.network import machine_offers
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "this host is not ready"}
    # A blocked Host is handed no credential, even against a request of ours.
    refusal = await _block_refusal(peer_id)
    if refusal is not None:
        return refusal
    try:
        return await machine_offers.mint_for_peer(pool, brain_service._entity_id, int(peer_id))
    except machine_offers.OfferError as e:
        log.info("peer machine.token from=%s REFUSED: %s", peer_id, e.message)
        return {"ok": False, "error": e.message}
    except Exception as e:  # pragma: no cover - defensive
        log.warning("peer machine.token from=%s failed: %s", peer_id, type(e).__name__)
        return {"ok": False, "error": "could not mint a token"}


async def _append_knock(peer_id: int, kind: str, payload: dict,
                        *, group_id: str | None = None) -> dict:
    if not await _knock_slot_free(peer_id, kind, group_id=group_id):
        log.info("peer inbox_request from=%s REFUSED: a %s is already waiting", peer_id, kind)
        return {"ok": False, "error": f"a {kind} from you is already waiting to be answered"}

    result = await handle_inbox_append(payload)
    if not result.get("ok"):
        log.info("peer inbox_request from=%s not stored: %s", peer_id, result.get("error"))
    return result


def _thread_id_for(peer_id: int, from_user_email: str | None,
                   to_user_email: str | None) -> str | None:
    """The thread this delivery belongs to, computed from who is actually on it.

    Person↔person when BOTH ends are named — matching the People readers, which
    select on `from_user_email IS NOT NULL AND to_user_email IS NOT NULL`. Otherwise
    the host pair, which is where every other row files. Returning None would be
    worse than any wrong id: handle_inbox_append COALESCEs a missing thread_id to a
    fresh uuid, so each message would land in a thread of its own.
    """
    try:
        from server import brain_service
        from server.routes.messages import pair_thread_id
        return pair_thread_id(int(brain_service._entity_id), int(peer_id),
                              from_user_email, to_user_email)
    except Exception as e:  # pragma: no cover - defensive, matches _identify
        log.info("thread id derivation unavailable (%s) — letting the inbox mint one",
                 type(e).__name__)
        return None


async def _person_claim(email: str, peer_id: int) -> tuple[str, str | None]:
    """Weigh the sending host's claim to be speaking for `email`, against the only
    thing this pod actually knows about that person. Returns (standing, our_name),
    standing one of

      "on_host"    our contact places this person on the sending Host
      "elsewhere"  our contact places them on a DIFFERENT Host: refuted
      "unknown"    no contact, a contact with no Host recorded, or the check
                   could not run: nothing refutes the claim and nothing verifies it

    inbox_deliver takes a person-to-person claim only "on_host" and refuses any
    other, so the sender falls back to the relay (the People chat is keyed on the
    addresses alone, so "unknown" there is how one Host could write into another
    person's chat); it keeps a person-to-Host claim unless "elsewhere".

    WHY THIS IS NOT JUST A NAME LOOKUP. The person claim is
    unverifiable by design — that decision stands. But
    "unverifiable" is not "unfalsifiable", and the difference is the whole of this
    function. The People views key on the ADDRESS alone and never on the sending
    host (_list_people_threads' inbound arm is `to_entity_id = $1 AND to_user_email
    = $2`, and the thread's identity is from_user_email), so a message claiming
    alice@ lands inside the genuine Alice thread whoever sent it.

    The first cut of this helper then looked the address up in OUR OWN contacts and
    rendered the name our user chose — turning an unverified assertion into a
    trusted one, and letting the sender pick which of our contacts to wear. The
    sidebar has no `via`, so the preview line was indistinguishable from real Alice.

    The row we were already fetching carries the refutation: contacts.peer_entity_id
    is the host WE believe that person is reached through. A mismatch is a LOCAL
    contradiction — detectable with this pod's own records — so acting on it does
    not put the platform back in the loop, and is the same shape as the to_user_email
    check above.

    On a refuted claim the message is still DELIVERED, just host-authored. That is
    the bridge's own behaviour for the equivalent failure ("a sender email that
    fails the check is DROPPED ... the message goes as host-authored, never as a
    stranger"), and it is the right default over refusing: a person legitimately
    moving hosts would otherwise have their mail bounce on a stale contact row.

    No contact at all → "unknown", no name. Nothing is contradicted, and nothing
    is verified either.

    FIRST CONTACT is not "unknown". A person can only write to a person once the
    connection is accepted (routes/circles.py refuses a People send whose contact
    names no Host), and accepting writes the Host into the contact on BOTH sides
    before the first message: the acceptor's from the request
    (contacts.accept_request_service), the requester's from the grant
    (memory_handlers.handle_inbox_append, upsert_contact_from_grant). Both travel
    by the relay, where the platform checks the person.
    """
    try:
        from server import brain_service
        from server.routes.contacts import find_human_contact
        pool = brain_service.get_pool()
        if pool is None:
            return "unknown", None
        row = await find_human_contact(pool, email)
        if row is None:
            return "unknown", None
        known_host = row["peer_entity_id"]
        if known_host is not None and int(known_host) != int(peer_id):
            log.warning("peer inbox_deliver from=%s claimed to speak for %s, whom "
                        "this host reaches through %s — person claim dropped, "
                        "message delivered as host-authored",
                        peer_id, email, known_host)
            return "elsewhere", None
        # TWO QUESTIONS, NOT ONE — and collapsing them once let a spoof through.
        # "Is the claim contradicted?" and "may we lend our name?" have different
        # answers on no evidence: not refuted ("unknown") but UNNAMED (nothing
        # corroborates it). The first cut tested only for contradiction, so a row with
        # peer_entity_id IS NULL fell through and returned the name — which is the
        # ordinary case, not an edge one: create_contact_service inserts
        # (handle, display_name, kind, notes, status) and its ON CONFLICT never sets
        # the column, so EVERY contact a user adds by hand has it NULL. Any
        # slip-holding peer could then send from_user_email=<a contact of theirs>
        # with no name and be rendered under the name their user chose.
        #
        # So the name needs POSITIVE agreement: we know which host that person is on,
        # and it is this one.
        if known_host is None:
            return "unknown", None
        # A contact the user BLOCKED still has a row and a name. Deliver, but do
        # not lend it our name: find_human_contact filters on kind, never status.
        if (row["status"] or "") == "blocked":
            return "on_host", None
        return "on_host", (row["display_name"] or "").strip() or None
    except Exception as e:  # pragma: no cover - defensive, matches _identify
        # Cannot check is not "refuted", and it is not "verified" either.
        log.info("person claim check unavailable (%s) — the claim is unverified",
                 type(e).__name__)
        return "unknown", None


async def _identify(peer_id: int) -> dict:
    """Name the knocker from the RECEIVER'S OWN records — never from the knock.

    The bridge leg injects verified from_display_name / from_handle /
    from_entity_uuid into the card's payload (adapter.py:4983-5002). This lane
    injected nothing, and the allow-list made that visible: the card fell through
    to `message.from` and read "9122@entity.localhost (a host) wants to connect".
    Worse, Accept posts whatever the card showed as the contact's HANDLE
    (inbox-pane.tsx:253-260), so accepting wrote an ADDRESS as a host handle —
    the exact invariant this branch's own gate pins ("a handle can no longer BE an
    address"), and a second contact row for the same peer once the reciprocal
    grant arrives by bridge carrying the real handle.

    The source is the receiver's peer list: its own bastions announced these
    hosts, which is the independent proof contacts.py already leans on for the
    outbound direction. Nothing here comes from the sender, so a knocker cannot
    choose the name on the card by choosing what to send — which is the property
    the payload allow-list exists to protect and this must not reopen.

    A peer we hold no record of gets "Host <id>" — honest, and still not an
    address. Whether such a knock should be refused outright is a product call and
    is deliberately NOT made here; a bare number is the minimum the user needs to
    decide on."""
    fallback = {"from_display_name": f"Host {int(peer_id)}"}
    try:
        from server import brain_service
        from server.network import peer_discovery
        pool = brain_service.get_pool()
        if pool is None:
            return fallback
        me = brain_service._entity_id
        # SCRUBBED, because these are bastion-supplied strings on their way into
        # storage. peer_handle is stored raw from the announcement (peer_discovery
        # writes `handle or None`), and this function now stamps it onto EVERY
        # delivered message rather than only a knock card. peer_discovery owns the
        # helper for exactly this — "no newlines or control characters to forge
        # structure with, and length-capped" — and applied it only on its
        # notification path, never on storage. React escapes these, so the harm is
        # not XSS: it is payload bloat and forged structure in the plain-text
        # surfaces (the host_message tool's output, log lines) _plain exists for.
        scrub = peer_discovery._plain
        for p in await peer_discovery.list_peerings(pool, me):
            if p.get("peer_entity_id") == int(peer_id):
                handle = scrub(p.get("peer_handle") or "")
                out = {"from_display_name": handle or f"Host {int(peer_id)}"}
                if handle:
                    out["from_handle"] = handle
                if p.get("introduced_by"):
                    out["shares_machine"] = scrub(p["introduced_by"])
                return out
    except Exception as e:  # pragma: no cover - defensive
        log.info("knock identity lookup unavailable (%s) — falling back", type(e).__name__)
    return fallback


async def _knock_slot_free(peer_id: int, kind: str, *, group_id: str | None = None) -> bool:
    """True when this peer has no UNANSWERED request of this kind already waiting.

    `group_id` NARROWS the slot to one group, and is passed only for a
    group_join. One host may reasonably want into two different circles, and a
    single slot per kind would make the second ask look like a repeat of the
    first — which the caller is told, so it would read as "already waiting" for
    a group nobody has been asked about. Per-group is safe here and ONLY here,
    because _group_join_target has already refused any group id this host does
    not own: the number of slots a peer can occupy is the number of groups this
    host chose to create, not a number the peer picks.

    Answered means archived — Accept and Deny both archive, so the slot reopens the
    moment the user decides.

    AND IT AGES OUT, because "archived" is not reliable enough to hold a peer out
    for ever. Accept archives only when it was given a message_id and does so
    best-effort (the chat tool treats it as optional), so an accepted request can
    leave its row live — and then the peer it was accepted FROM is told "already
    waiting to be answered" on its next knock. A card ignored long enough to fall
    out of the inbox list blocks the same way, with nothing on screen to explain
    it. A week is well past "the user is thinking about it" and well short of
    letting a flood back in: one per peer per week is not a flood.

    Fails OPEN: the append below will fail honestly if the pool is gone, and a
    counting query that cannot run is not a reason to refuse a first contact."""
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return True
    try:
        if group_id is not None:
            n = await pool.fetchval(
                "SELECT count(*) FROM inbox_messages "
                " WHERE from_entity_id = $1 AND message_kind = $2 AND archived_at IS NULL"
                "   AND sent_at > now() - $3::interval"
                "   AND payload->>'group_id' = $4",
                int(peer_id), kind, KNOCK_SLOT_WINDOW, group_id)
        else:
            n = await pool.fetchval(
                "SELECT count(*) FROM inbox_messages "
                " WHERE from_entity_id = $1 AND message_kind = $2 AND archived_at IS NULL"
                "   AND sent_at > now() - $3::interval",
                int(peer_id), kind, KNOCK_SLOT_WINDOW)
    except Exception as e:  # pragma: no cover - defensive
        log.info("knock cap check unavailable (%s) — allowing", type(e).__name__)
        return True
    return not n


# Registered into PeerServer(handlers=...). Op name -> handler.
PEER_HANDLERS = {
    "inbox.deliver": inbox_deliver,
    "inbox.request": inbox_request,
    "inbox.grant": inbox_grant,
    # The reciprocal slip after an accept, on the message slip the accepter
    # issued us. Slip-gated: _OP_ACTION["contact.slip"] = "message".
    "contact.slip": contact_slip,
    "machine.token": machine_token,
    # The lane carries apps.
    # Both slip-gated: app.grant on the message slip, app.deliver on app:<id>.
    "app.grant": _app_lane.app_grant,
    "app.deliver": _app_lane.app_deliver,
    # The lane carries GROUPS. All three are
    # slip-gated: group.grant on the message slip, because it is the INVITE and
    # the slip it would otherwise present is the one it carries; roster_fetch
    # and vote on group:<group_id> read off their own params.
    "group.grant": _group_lane.group_grant,
    "group.roster_fetch": _group_lane.group_roster_fetch,
    "group.vote": _group_lane.group_vote,
}
