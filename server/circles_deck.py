"""Circles' deck: the server half of the cards.

Three reads, each only adding to what the server already answers:

  pairing   on GET /api/circles/conversations. A person carries the Host they
            are reached through (their contact's peer_entity_id, v123) and the
            contact you hold for that Host; a peer Host carries the people on
            the same list who are reached through it. A blocked person, and a
            Host blocked directly or through its person, leave the list
            (contacts.blocked_sets, the set form of contacts.block_refuses).
  door      GET /api/circles/door. The four knocks the Inbox draws buttons for
            (endpoint_request, group_join, rendezvous_intro, machine_request)
            and the Hosts newly found on your machines (peer_discovery's
            "New peer" notice), each with the call its Accept and its Not now
            make today.
  waiting   GET /api/circles/waiting. Every open decision with the room it was
            asked in, the support drafts waiting for Send, and the door's
            count.

Everything here is read-only; the calls the door names are the routes that
already answer each knock.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("privatae.circles_deck")

#: The knocks the Inbox pane answers with buttons (circles-web
#: inbox-pane.tsx, the four widgets), in the order the door lists kinds.
KNOCK_KINDS = ("endpoint_request", "group_join", "rendezvous_intro", "machine_request")
#: A Host newly found on one of your machines: peer_discovery.record_and_notify
#: writes a system_notification whose payload kind is this.
NEW_PEER = "new_peer"
FOUND_HOST = "found_host"
DOOR_KINDS = KNOCK_KINDS + (FOUND_HOST,)


def _payload(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _call(method: str, path: str, body: dict | None = None) -> dict:
    out = {"method": method, "path": path}
    if body is not None:
        out["body"] = body
    return out


def _archive(message_id: str) -> dict:
    """Not now for a card no other route answers: the archive the Inbox pane
    calls (inbox-pane.tsx archive, POST /api/inbox/archive/{id}). It tells
    nobody."""
    return _call("POST", f"/api/inbox/archive/{message_id}")


# ── Pairing ─────────────────────────────────────────────────────────────────

async def pair(pool, people_items: list[dict], entity_items: list[dict]) -> tuple[list, list]:
    """The people and peer Hosts of the conversations list, less the blocked,
    each paired with the other. Returns new lists; the items are updated in
    place.

    On a person (`id` is their email):
      contact_id      their human contact, or None
      host_entity_id  the Host that contact is reached through, or None
                      (no contact, or none recorded yet)
      host_contact    {id, status} of the host contact you hold for that Host,
                      or None: a person whose Host you are not connected to
      host_blocked    the block rule's answer for that Host (blocked directly,
                      or through a blocked person on it). The person stays:
                      blocking a Host does not block the people on it.
    On a peer Host (`id` is its entity id):
      people          [{id, display_name}] of the people on this list reached
                      through it
      host_contact    {id, status} as above

    Raises when a read fails; the caller decides what the list shows then."""
    from server.routes.contacts import blocked_sets, clean_email
    hosts_blocked, people_blocked = await blocked_sets(pool)

    def addr(item) -> str:
        raw = str(item.get("id") or "")
        return clean_email(raw) or raw.strip().lower()

    people = [p for p in people_items if addr(p) not in people_blocked]
    entities = [e for e in entity_items if _int(e.get("id")) not in hosts_blocked]

    emails = sorted({addr(p) for p in people if addr(p)})
    human = {}
    if emails:
        for r in await pool.fetch(
                "SELECT DISTINCT ON (lower(handle)) id, lower(handle) AS email, peer_entity_id"
                "  FROM contacts WHERE kind = 'human' AND lower(handle) = ANY($1::text[])"
                " ORDER BY lower(handle), id", emails):
            human[r["email"]] = r
    host_ids = {r["peer_entity_id"] for r in human.values() if r["peer_entity_id"] is not None}
    host_ids |= {i for i in (_int(e.get("id")) for e in entities) if i is not None}
    host_contact = {}
    if host_ids:
        # A Host can have two host rows (a request's "host <id>" placeholder
        # beside the real handle, contacts.upsert_contact_from_grant). The
        # blocked one first, so Unblock names the row that holds the block,
        # then the active one.
        for r in await pool.fetch(
                "SELECT DISTINCT ON (peer_entity_id) id, peer_entity_id, status"
                "  FROM contacts WHERE kind = 'host' AND peer_entity_id = ANY($1::int[])"
                " ORDER BY peer_entity_id, (status = 'blocked') DESC, (status = 'active') DESC, id",
                sorted(host_ids)):
            host_contact[int(r["peer_entity_id"])] = {"id": r["id"], "status": r["status"]}

    on_host: dict[int, list[dict]] = {}
    for p in people:
        c = human.get(addr(p))
        hid = c["peer_entity_id"] if c is not None else None
        p["contact_id"] = c["id"] if c is not None else None
        p["host_entity_id"] = hid
        p["host_contact"] = host_contact.get(hid) if hid is not None else None
        p["host_blocked"] = hid is not None and hid in hosts_blocked
        if hid is not None:
            on_host.setdefault(int(hid), []).append(
                {"id": p.get("id"), "display_name": p.get("display_name")})
    for e in entities:
        hid = _int(e.get("id"))
        e["people"] = on_host.get(hid, []) if hid is not None else []
        e["host_contact"] = host_contact.get(hid) if hid is not None else None
    return people, entities


# ── Groups ──────────────────────────────────────────────────────────────────

async def groups(pool, entity_id: int) -> list[dict]:
    """The groups this Host owns or is a current member of, as the groups
    service lists them (roster.list_groups, the read GET /api/groups makes),
    each with `id` and `kind` as the list's other items carry them."""
    from server.groups import roster
    out = []
    for g in await roster.list_groups(pool, entity_id):
        out.append({"id": g["group_id"], "kind": "group", "display_name": g["name"], **g})
    return out


# ── The door ────────────────────────────────────────────────────────────────

_DOOR_SQL = """
SELECT message_id, message_kind, from_address, from_entity_id, from_user_email,
       subject, body, payload, COALESCE(sent_at, delivered_at) AS at
  FROM inbox_messages
 WHERE to_entity_id = $1 AND archived_at IS NULL
   AND (message_kind = ANY($2::text[])
        OR (message_kind = 'system_notification' AND payload->>'kind' = $4))
   -- A knock addressed to one person is that operator's: another operator's
   -- Accept would answer it in their own name (accept_request_service sends
   -- the accepting operator's email back).
   -- Compared lowercased on both sides, so an operator never loses their own
   -- knock to the case a sender wrote the address in.
   AND (to_user_email IS NULL OR lower(to_user_email) = lower($3))
 ORDER BY at DESC, message_id
"""


async def door(pool, entity_id: int, reader: str) -> list[dict]:
    """What is at the door for the operator `reader`, newest first. One item
    per card:

      {kind, id, at, from: {entity_id, name, email, person}, subject, note,
       accept: {method, path, body?} | None, not_now: [{method, path, body?}, ...],
       ...what the kind's card shows}

    `not_now` is a LIST of calls on every kind, every one to be made, so a
    client reads one shape. Most kinds have one; a found Host has one archive
    per "New peer" notice of that Host; a Host's own connection request that
    hides its found card also archives that Host's notices; a machine request
    with no open offer archives every request row of that Host.

    `accept` is None when the card cannot be answered as it stands (a knock
    naming no group, a machine request whose offer is no longer open); Not now
    is then the archive. A blocked sender's card is not listed, by the rule
    admission uses today. A found Host is listed while the peering is current
    and resolved and no host contact for it exists (Connect writes a pending
    one, contacts.request_peer_service), so either answer takes it away. It is
    one card per Host, from the newest notice, however many of your machines
    reported it (peer_discovery writes a notice per machine and Host). And it
    is not listed beside that Host's own connection request: the request is
    the stronger card, and accepting it writes the host contact that clears
    both. A machine request is one card per Host too, from the newest row: a
    second row can land once the first is past the peer lane's one-week slot
    (peer/handlers._knock_slot_free), and the offer insert then refuses
    without taking the card back. Its offer's decline archives every request
    row of that Host (machine_offers._archive_cards).

    The reads do not grow with the number of cards: the groups the group
    knocks name, and the open offers behind the machine requests, are read
    once each for the whole door.

    Raises when a read fails."""
    from server.groups import roster
    from server.network import peer_discovery
    from server.routes.contacts import blocked_sets, clean_email

    hosts_blocked, people_blocked = await blocked_sets(pool)
    rows = await pool.fetch(_DOOR_SQL, int(entity_id), list(KNOCK_KINDS), reader or "", NEW_PEER)

    # One read each for what the group knocks and machine requests name.
    gids = sorted({g for g in (_payload(r["payload"]).get("group_id") for r in rows
                               if r["message_kind"] == "group_join")
                   if roster.valid_group_id(g)})
    groups_named = {}
    if gids:
        groups_named = {g["group_id"]: g for g in await pool.fetch(
            "SELECT group_id, owner_entity_id, name FROM groups WHERE group_id = ANY($1::text[])", gids)}
    asking = sorted({int(r["from_entity_id"]) for r in rows
                     if r["message_kind"] == "machine_request" and r["from_entity_id"] is not None})
    open_offers = {}
    if asking:
        # At most one open inbound offer per Host (ux_machine_offers_open,
        # migrations/v140_machine_offers.sql:79); newest first all the same.
        open_offers = {int(o["peer_entity_id"]): o for o in await pool.fetch(
            "SELECT DISTINCT ON (peer_entity_id) id, peer_entity_id, purpose, models_asked "
            "  FROM network_machine_offers "
            " WHERE direction = 'in' AND state = 'open' AND peer_entity_id = ANY($1::int[]) "
            " ORDER BY peer_entity_id, id DESC", asking)}

    peerings: dict | None = None
    held: set[int] | None = None
    found: dict[int, dict] = {}            # a Host's one found card, the newest notice
    notices: dict[int, list[str]] = {}     # every notice naming that Host
    host_asks: dict[int, list[dict]] = {}  # Hosts whose own request is on the door
    machine: dict[int, dict] = {}          # a Host's one machine card, the newest row
    machine_rows: dict[int, list[str]] = {}
    out = []
    for r in rows:
        kind = r["message_kind"]
        mid = str(r["message_id"])
        p = _payload(r["payload"])
        person = clean_email(r["from_user_email"])
        from_eid = r["from_entity_id"]
        base = {"id": mid, "at": r["at"], "subject": r["subject"],
                "note": r["body"] or ""}

        if kind == "system_notification":
            if peerings is None:
                peerings = {(x["token_id"], x["peer_host_name"]): x
                            for x in await peer_discovery.list_peerings(pool, entity_id)}
                held = {int(x["peer_entity_id"]) for x in await pool.fetch(
                    "SELECT DISTINCT peer_entity_id FROM contacts "
                    " WHERE kind = 'host' AND peer_entity_id IS NOT NULL")}
            peering = peerings.get((_int(p.get("token_id")), p.get("peer_host_name")))
            pid = _int(peering.get("peer_entity_id")) if peering is not None else None
            if pid is not None:
                notices.setdefault(pid, []).append(mid)
            if peering is None or not peering.get("current"):
                continue
            if pid is None or pid == int(entity_id) or pid in held or pid in hosts_blocked:
                continue
            if pid in found:
                continue
            name = peering.get("peer_handle") or peering.get("peer_host_name") or f"Host {pid}"
            found[pid] = {**base, "kind": FOUND_HOST,
                          "from": {"entity_id": pid, "name": name, "email": None, "person": False},
                          "machine": peering.get("introduced_by"),
                          "peer_host_name": peering.get("peer_host_name"),
                          "accept": _call("POST", "/api/contacts/request-peer",
                                          {"peer_entity_id": pid}),
                          "not_now": None}
            out.append(found[pid])
            continue

        if kind == "rendezvous_intro":
            gid = _int(p.get("group_id"))
            # No sender: the card names a group and a count, and nobody
            # (rendezvous.introductions).
            out.append({**base, "kind": kind, "from": None,
                        "group": {"id": gid, "slug": p.get("group_slug"),
                                  "title": p.get("group_title")},
                        "tag": p.get("tag"), "others": p.get("others"),
                        "accept": (_call("POST", f"/api/groups/rendezvous/introductions/{mid}/accept")
                                   if gid is not None and gid > 0 else None),
                        "not_now": [_archive(mid)]})
            continue

        # The three knocks another Host sends. A person's request is refused
        # by the person's own block; a Host's by the Host's (block_refuses on
        # the relay, where the person on the row stands as the platform
        # checked it).
        if person and person in people_blocked:
            continue
        if not person and from_eid is not None and int(from_eid) in hosts_blocked:
            continue

        if kind == "endpoint_request":
            # The card's own reading (inbox-pane.tsx EndpointRequestWidget):
            # a person is the verified column, never the payload; the Host's
            # name and handle are the fields the receiver stamped.
            host_handle = str(p.get("from_handle") or p.get("handle") or r["from_address"] or "")
            host_name = str(p.get("from_display_name") or p.get("display_name") or host_handle)
            name = str(p.get("from_user_name") or person) if person else host_name
            eid = from_eid if from_eid is not None else _int(p.get("from_entity_id"))
            card = {**base, "kind": kind,
                        "from": {"entity_id": eid, "name": name, "email": person,
                                 "person": bool(person), "host_name": host_name},
                        "accept": _call("POST", "/api/contacts/accept-request", {
                            "message_id": mid, "handle": person or host_handle,
                            "display_name": name, "from_entity_id": eid,
                            "kind": "human" if person else "host",
                            "from_user_email": person}),
                    "not_now": [_archive(mid)]}
            if not person and eid is not None:
                host_asks.setdefault(int(eid), []).append(card)
            out.append(card)
            continue

        who = str(p.get("from_display_name") or p.get("from_handle") or r["from_address"] or "A host")
        sender = {"entity_id": from_eid, "name": who, "email": None, "person": False}

        if kind == "group_join":
            gid = p.get("group_id") if isinstance(p.get("group_id"), str) else None
            g = groups_named.get(gid) if gid else None
            owned = g is not None and int(g["owner_entity_id"]) == int(entity_id)
            answerable = owned and from_eid is not None
            out.append({**base, "kind": kind, "from": sender,
                        "shares_machine": p.get("shares_machine") if isinstance(p.get("shares_machine"), str) else None,
                        "group": {"id": gid, "name": g["name"] if owned else None},
                        "accept": (_call("POST", f"/api/groups/{gid}/join-requests/{int(from_eid)}/accept",
                                         {"role": "member"}) if answerable else None),
                        "not_now": [_call("POST", f"/api/groups/{gid}/join-requests/{int(from_eid)}/deny")
                                    if answerable else _archive(mid)]})
            continue

        # machine_request. The card's answer is the open offer row its arrival
        # wrote (machine_offers); accepting needs the person's choice of
        # machine and models and their step-up code, so those are named, not
        # filled.
        if from_eid is not None:
            machine_rows.setdefault(int(from_eid), []).append(mid)
            if int(from_eid) in machine:
                continue
        offer = open_offers.get(int(from_eid)) if from_eid is not None else None
        models = (_payload_list(offer["models_asked"]) if offer is not None
                  else _payload_list(p.get("models_asked")))
        # Not now with an open offer is its decline, which archives every
        # request row of that Host (machine_offers._archive_cards); without
        # one it is filled after the loop, an archive per row of that Host.
        card = {**base, "kind": kind, "from": sender,
                "purpose": offer["purpose"] if offer is not None else None,
                "models_asked": models,
                "offer_id": offer["id"] if offer is not None else None,
                "accept": ({**_call("POST", "/api/network/bastions/machine-offers/accept",
                                    {"offer_id": offer["id"]}),
                            "needs": ["server", "models", "stepup_code"]}
                           if offer is not None else None),
                "not_now": ([_call("POST", "/api/network/bastions/machine-offers/decline",
                                   {"offer_id": offer["id"]})]
                            if offer is not None else None)}
        if from_eid is not None:
            machine[int(from_eid)] = card
        out.append(card)

    for pid, card in found.items():
        card["not_now"] = [_archive(m) for m in notices.get(pid) or [card["id"]]]
    for hid, card in machine.items():
        if card["not_now"] is None:
            card["not_now"] = [_archive(m) for m in machine_rows.get(hid) or [card["id"]]]
    for item in out:
        if item["kind"] == "machine_request" and item["not_now"] is None:
            item["not_now"] = [_archive(item["id"])]
    hidden = {pid for pid in host_asks if pid in found}
    for pid in hidden:
        for card in host_asks[pid]:
            card["not_now"] = card["not_now"] + [_archive(m) for m in notices.get(pid, [])]
    if hidden:
        out = [i for i in out
               if not (i["kind"] == FOUND_HOST and i["from"]["entity_id"] in hidden)]
    return out


# ── Waiting on you ──────────────────────────────────────────────────────────

def room_of(asked_in) -> dict:
    """The room a decision was asked in, as the thread list and the tickets
    route key it: a thread by its id; a project as a project id with no
    thread (tickets._room_filter); anything else none. A decision asked in
    the old original conversation has no room: there is no such page any
    more."""
    a = asked_in if isinstance(asked_in, dict) else {}
    tid = str(a.get("thread_id") or "").strip().lower()
    pid = "" if a.get("project_id") is None else str(a.get("project_id")).strip()
    if tid and tid != "original":
        return {"kind": "thread", "id": tid}
    if pid:
        return {"kind": "project", "id": pid}
    return {"kind": "none", "id": None}


async def waiting(pool, entity_id: int, reader: str) -> dict:
    """What is waiting on you: every open decision (tickets.ticket_summary,
    the Host's own list row, with its room), the support drafts waiting for
    Send, and the door's count. A thread room carries the thread's title, or
    gone=True when it is deleted or not this Host's; a project room the
    project's name, or gone=True."""
    from server.routes.support import _revision
    from server.routes.tickets import ticket_summary

    rows = await pool.fetch(
        "SELECT id, status, source, priority, opened_at, sitrep, options, metadata->'asked_in' AS asked_in"
        "  FROM entity_tickets WHERE entity_id = $1 AND status = 'open'"
        " ORDER BY priority DESC, opened_at DESC, id DESC", int(entity_id))
    decisions = []
    threads, projects = set(), set()
    for r in rows:
        d = dict(r)
        d["options"] = _payload_list(d.get("options"))
        if hasattr(d.get("opened_at"), "isoformat"):
            d["opened_at"] = d["opened_at"].isoformat()
        room = room_of(_payload(r["asked_in"]))
        if room["kind"] == "thread":
            threads.add(room["id"])
        elif room["kind"] == "project" and room["id"].isdigit():
            projects.add(int(room["id"]))
        decisions.append({**ticket_summary(d), "room": room})

    titles: dict[str, dict] = {}
    if threads:
        for t in await pool.fetch(
                "SELECT lower(id::text) AS id, title, closed_at FROM circles_threads "
                " WHERE entity_id = $1 AND deleted_at IS NULL AND lower(id::text) = ANY($2::text[])",
                int(entity_id), sorted(threads)):
            titles[t["id"]] = {"title": t["title"], "closed": t["closed_at"] is not None}
    names: dict[str, str] = {}
    if projects:
        for pr in await pool.fetch(
                "SELECT id, name FROM projects WHERE entity_id = $1 AND id = ANY($2::int[])",
                int(entity_id), sorted(projects)):
            names[str(pr["id"])] = pr["name"]
    for d in decisions:
        room = d["room"]
        if room["kind"] == "thread":
            t = titles.get(room["id"])
            room.update(t if t else {"title": None, "gone": True})
        elif room["kind"] == "project":
            n = names.get(room["id"])
            room.update({"name": n} if n is not None else {"name": None, "gone": True})

    drafts = []
    for s in await pool.fetch(
            "SELECT id, title, category, severity, project_id, project_name, opened_at, updated_at,"
            "       jsonb_array_length(shared_context) AS shared_items, metadata"
            "  FROM support_tickets WHERE entity_id = $1 AND status = 'draft'"
            " ORDER BY updated_at DESC, id DESC", int(entity_id)):
        drafts.append({"id": s["id"], "title": s["title"], "category": s["category"],
                       "severity": s["severity"], "project_id": s["project_id"],
                       "project_name": s["project_name"], "opened_at": s["opened_at"],
                       "updated_at": s["updated_at"], "shared_items": s["shared_items"],
                       "revision": _revision(s["metadata"]),
                       # The link that opens it today (circles-web app-view.ts).
                       "link": f"support:{s['id']}"})

    at_door = len(await door(pool, entity_id, reader))
    return {"decisions": decisions, "support_drafts": drafts, "door": at_door,
            "count": len(decisions) + len(drafts) + at_door}


def _payload_list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []
