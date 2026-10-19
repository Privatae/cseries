"""Join requests — how a host that is NOT yet a contact gets into a group.

The bound this module removes: `group.grant` rides the MESSAGE slip the invitee already issued
the owner, so until now the only way onto a roster was for the OWNER to add an
already-accepted contact. "Connect in Circles first, then ask to be added" is
two conversations for one intention, and the first one has nothing to say.

THE SHAPE. A join request is a KNOCK — the existing slipless `inbox.request`
(server/peer/peer_server.py:135), carrying `message_kind='group_join'` and the
group id in its payload. Nothing new on the wire and no new op:

  * the knock authorizes nothing. It puts a card in front of a person, which is
    the entire reason inbox.request is allowed to be slipless in the first place
    (peer_server.py:71-134 carries that reasoning in full).
  * the receiver's gate is `handlers._group_join_target`: the card is refused
    unless it names a group THIS host owns. A stranger cannot use it to probe
    for group ids, and cannot occupy more knock slots than this host has groups.
  * ACCEPT is two acts in one, in this order and for a reason stated below.
  * DENY stores nothing beyond archiving the card. A denial is silent, exactly
    as `contacts.deny_request_service` is silent — a refusal that answers back
    tells a stranger their id was seen.

WHY ACCEPT MAKES THE CONTACT FIRST. `group.grant` needs the message slip the
requester issued US, and a requester who knocked has issued nothing: the slip
travels on the reciprocal grant their host sends after ours lands
(server/bridge/memory_handlers.py:545-584). So the contact is made first, and
the group grant is ENQUEUED rather than required to succeed now. The group
lane's outbox is exactly the right mechanism for the gap: `_send` raises
`LocalFailure("no message slip from this peer on file")`, `app_lane.classify`
calls that retryable, and the row is retried every TICK_S until the reciprocal
lands (server/groups/lane.py:546-574, :589-626). Nothing here waits, polls or
sleeps — the outbox already owns this problem.

NEVER A SLIP FROM AN UNACCEPTED HOST. Stated because it is the property most
easily lost here: a join request puts no group slip, no app slip and no contact
slip in this pod. The requester's card is a row in `inbox_messages` and nothing
else until the user accepts. All this module can do before an accept is read.
"""
from __future__ import annotations

import logging
import uuid

from server.groups import lane
from server.groups import roster as R

log = logging.getLogger("privatae.groups.join")

#: The knock's message_kind. Declared here beside the code that reads it, and
#: in handlers.REQUEST_KINDS beside the code that admits it — one string, two
#: ends of the same wire, so it is imported rather than typed twice.
KIND = "group_join"

#: A note the asker may attach ("we met at the meetup"). Bounded on the SENDING
#: side too, not only at the receiver: a caller should be refused for a 10 KB
#: note rather than have it silently truncated at the far end into something
#: they did not write. The receiver's own cap is handlers._MAX_BODY.
MAX_NOTE = 500


# ── The asker's side ───────────────────────────────────────────────────────

async def ask_to_join(pool, group_id: str, owner_entity_id: int, *,
                      note: str = "") -> dict:
    """Knock on a group owner's door.

    `owner_entity_id` is a PARAMETER and not a lookup, because the whole point
    of this op is that we are not in the group: there is no local row to read an
    owner off. The asker learned the pair from somewhere out of band — a
    rendezvous introduction, a link, a person — and that is fine, because naming
    a group confers nothing. The owner's host is the one that decides whether
    the pair means anything (`handlers._group_join_target`).
    """
    if not R.valid_group_id(group_id):
        raise R.RosterError(400, "malformed group id")
    try:
        owner = int(owner_entity_id)
    except (TypeError, ValueError):
        raise R.RosterError(400, "owner_entity_id must be a host id")
    if owner <= 0:
        raise R.RosterError(400, "owner_entity_id must be a host id")

    existing = await R.get_group(pool, group_id)
    if existing is not None and int(existing["owner_entity_id"]) == owner:
        # We already hold this group — either we own it or we are a member with
        # a replica. Either way there is nothing to ask for, and the refusal
        # says which so the caller is not left guessing.
        from server import brain_service
        if int(existing["owner_entity_id"]) == int(brain_service._entity_id):
            raise R.RosterError(409, "this host owns that group")
        if await R.is_current_member(pool, group_id, int(brain_service._entity_id)):
            raise R.RosterError(409, "this host is already a member of that group")

    note = (note or "").strip()[:MAX_NOTE]
    from server.peer.peer_client import knock_peer, PeerUnavailable
    params = {
        "message_kind": KIND,
        "subject": "Request to join a group",
        "body": note,
        "payload": {"group_id": group_id},
    }
    status, err = "sent", None
    try:
        resp = await knock_peer(owner, params)
        if not resp.get("ok"):
            # The owner's host ANSWERED. Recorded as its own state rather than
            # as a transport failure: "that host does not host that group" is a
            # fact the user should see, not something to retry into.
            status, err = "refused", str(resp.get("error") or "the owner's host refused")
    except PeerUnavailable as e:
        status, err = "unreachable", f"owner unreachable: {e}"
    except Exception as e:  # pragma: no cover - defensive
        status, err = "unreachable", f"could not reach the owner ({type(e).__name__})"

    await pool.execute(
        "INSERT INTO group_join_requests (group_id, owner_entity_id, status, last_error, note) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (group_id) DO UPDATE "
        "   SET owner_entity_id = EXCLUDED.owner_entity_id, status = EXCLUDED.status, "
        "       last_error = EXCLUDED.last_error, note = EXCLUDED.note, "
        "       asked_at = now(), updated_at = now()",
        group_id, owner, status, err, note)
    log.info("group %s: join request to entity %s -> %s", group_id, owner, status)
    return {"group_id": group_id, "owner_entity_id": owner, "status": status,
            "error": err, "note": note}


async def my_requests(pool) -> list[dict]:
    """Join requests THIS host has sent. An accepted one stops being a request
    and becomes a group: the row is left in place and the caller can see it is
    satisfied by the group turning up in `list_groups`."""
    rows = await pool.fetch(
        "SELECT r.group_id, r.owner_entity_id, r.status, r.last_error, r.note, "
        "       r.asked_at, r.updated_at, "
        "       (g.group_id IS NOT NULL) AS joined "
        "  FROM group_join_requests r "
        "  LEFT JOIN groups g ON g.group_id = r.group_id "
        " ORDER BY r.asked_at DESC")
    return [dict(r) for r in rows]


# ── The owner's side ───────────────────────────────────────────────────────

async def pending(pool, group_id: str) -> list[dict]:
    """The unanswered join requests for one group this host owns.

    Read straight off `inbox_messages`, because that IS where the card lives —
    there is no second table shadowing it. A shadow table would be a second
    place for "answered" to be true, and the two would disagree the first time
    an accept archived one and not the other.
    """
    rows = await pool.fetch(
        "SELECT message_id, from_entity_id, subject, body, payload, sent_at "
        "  FROM inbox_messages "
        " WHERE message_kind = $1 AND archived_at IS NULL "
        "   AND payload->>'group_id' = $2 "
        " ORDER BY sent_at DESC", KIND, group_id)
    out = []
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            import json
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        p = p if isinstance(p, dict) else {}
        out.append({
            "message_id": str(r["message_id"]),
            "from_entity_id": r["from_entity_id"],
            # The names the RECEIVER stamped on the card (handlers._identify),
            # never anything the sender chose. Passed straight through to the
            # contact the accept writes, so the group_join accept and the
            # endpoint_request accept name the same peer the same way.
            "display_name": p.get("from_display_name"),
            "handle": p.get("from_handle"),
            "shares_machine": p.get("shares_machine"),
            "note": r["body"] or "",
            "asked_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        })
    return out


async def _card(pool, group_id: str, requester: int) -> dict | None:
    for c in await pending(pool, group_id):
        if int(c["from_entity_id"] or -1) == int(requester):
            return c
    return None


async def accept(pool, me: int, group_id: str, requester: int, *,
                 role: str = "member") -> dict:
    """Accept a join request: make the contact, then put them on the roster.

    ORDER IS THE WHOLE DESIGN. The contact comes first because the group grant
    rides the message slip that only a contact relationship produces; the roster
    row comes second because it is the thing `group.roster_fetch` and
    `app.deliver` gate on and it must not exist for a host we have not accepted.

    NEITHER STEP IS ALLOWED TO SUCCEED ALONE AND SILENTLY. If the contact
    accept refuses, nothing is added and the card stays. If the contact was made
    but its grant did not land, the roster row IS still written and the group
    grant is queued — that is not a half-success, it is the normal case: the
    requester's reciprocal slip has not arrived yet and the outbox exists to
    wait for it. The answer says which of those happened rather than reporting
    a queued grant as a delivered one.

    EVERYTHING THAT CAN REFUSE IS CHECKED BEFORE THE FIRST WRITE, and that is
    not tidiness — it is the only thing standing between a failed accept and an
    unrecoverable one. `accept_request_service` ARCHIVES THE CARD before it
    returns (server/routes/contacts.py:586-592, unconditional). So a refusal
    raised AFTER it — a role `add_member` will not assign, a group closed
    between this function's ownership check and add_member's own — leaves the
    contact made, the allow written, the card gone and no roster row, and the
    user cannot re-accept because `_card` has nothing left to find. The role is
    therefore validated here, against add_member's own vocabulary, before
    anything is written.

    A refusal that still gets past all of that is caught and re-raised saying
    what DID happen and how to finish it by hand, because at that point the
    requester is a contact and the ordinary add-member route works on them.
    """
    g = await R._require_owner(pool, group_id, me)
    requester = int(requester)
    if requester == int(me):
        raise R.RosterError(400, "that is this host")
    # add_member checks this too, but it checks it AFTER the card is gone. Its
    # own vocabulary is imported rather than restated so the two cannot drift
    # into disagreeing about which roles exist.
    if role not in R.ASSIGNABLE_ROLES:
        raise R.RosterError(400, f"role must be one of {', '.join(R.ASSIGNABLE_ROLES)}")
    card = await _card(pool, group_id, requester)
    if card is None:
        raise R.RosterError(404, "no unanswered join request from that host for this group")

    from server.routes import contacts as C
    # The handle the CARD shows, which is the handle the Circles Accept button
    # posts for an endpoint_request (inbox-pane.tsx renders
    # from_display_name ?? display_name ?? from_handle ?? handle and Accept
    # sends it). Matched deliberately: a different convention here would write a
    # SECOND contacts row for a peer the other path had already named, which is
    # the duplicate-row defect handlers._identify's docstring records.
    handle = card.get("handle") or card.get("display_name") or f"Host {requester}"
    try:
        accepted = await C.accept_request_service(
            pool, handle=handle, message_id=card["message_id"],
            display_name=card.get("display_name"), from_entity_id=requester,
            kind="host")
    except C.ContactsError as e:
        raise R.RosterError(e.status, e.detail)

    try:
        added = await R.add_member(pool, me, group_id, requester, role=role,
                                   display_name=card.get("display_name") or "")
    except R.RosterError as e:
        # The card is already archived and the contact is already made. Saying
        # only "that group is closed" here would be true and useless: it hides
        # that half the act landed and leaves the user with no way back, because
        # re-accepting needs a card that no longer exists. The requester IS a
        # contact now, so the ordinary add-member route will finish the job —
        # say so.
        log.warning("group %s: join accept from %s made the contact but could not "
                    "add the member: %s", group_id, requester, e.detail)
        raise R.RosterError(
            409, f"{e.detail} — but {requester} is now an accepted contact and the "
                 f"request has been answered, so add them with "
                 f"POST /api/groups/{group_id}/members once that is resolved")
    out = {"group_id": group_id, "member_entity_id": requester,
           "contact_id": accepted.get("contact_id"),
           "contact_grant_delivered": bool(accepted.get("grant_delivered")),
           **added}
    if added.get("added"):
        # A DETERMINISTIC message_id, so two accepts racing on one card produce
        # ONE invite. Both can pass `_card` before either archives, and
        # add_member's INSERT ... ON CONFLICT DO UPDATE answers "added" to both,
        # so without this each would mint its own contract and send its own
        # grant. enqueue dedupes on (group_id, peer_entity_id, message_id)
        # (server/groups/lane.py:514-528) — it just has to be GIVEN the id
        # rather than minting a fresh uuid4 per call.
        #
        # The roster_epoch is in the name on purpose: a re-admission bumps it
        # (roster._bump_epoch), so a member removed and added back gets a fresh
        # id and a fresh contract instead of colliding with the row of the grant
        # that was revoked when they left — the defect _LIVE_GROUP_GRANT exists
        # to stop, one layer up.
        mid = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"group-join-grant:{group_id}:{requester}:{added.get('roster_epoch')}")
        row = await lane.enqueue(pool, group_id, requester, lane.OP_GRANT, {},
                                 message_id=mid)
        out["invite"] = await lane.attempt(
            pool, {**row, "group_id": group_id, "peer_entity_id": requester,
                   "op": lane.OP_GRANT, "payload": {}})
    log.info("group %s: join request from %s accepted (epoch %s)",
             group_id, requester, out.get("roster_epoch"))
    return out


async def deny(pool, me: int, group_id: str, requester: int) -> dict:
    """Deny a join request. Archive the card and do nothing else.

    No contact, no roster row, no reply — the same silence
    `contacts.deny_request_service` keeps, and for a sharper reason here: a
    reply would confirm to a stranger that the group id they named is real and
    that this host is its owner, which is precisely what
    `_group_join_target`'s single refusal sentence exists to hide.

    Idempotent. Denying twice archives an already-archived card and says so
    rather than raising, because the card may also have aged out of the list.
    """
    await R._require_owner(pool, group_id, me)
    card = await _card(pool, group_id, int(requester))
    if card is None:
        return {"group_id": group_id, "member_entity_id": int(requester),
                "denied": True, "archived": False,
                "reason": "no unanswered join request from that host for this group"}
    archived = False
    try:
        from server.bridge.memory_handlers import handle_inbox_archive
        await handle_inbox_archive({"message_id": card["message_id"]})
        archived = True
    except Exception as e:
        log.warning("group %s: could not archive denied join request: %s",
                    group_id, type(e).__name__)
    return {"group_id": group_id, "member_entity_id": int(requester),
            "denied": True, "archived": archived}
