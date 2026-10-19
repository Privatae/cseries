"""The contact card for ANOTHER Host — one resolver behind every surface that
names one: anywhere another host is named in the dashboard, it shows as a
contact instead of a string of letters and numbers.

EVERYTHING HERE COMES FROM THIS HOST'S OWN RECORDS. Contacts it keeps,
peerings its own bastions announced, slips it holds, rosters it holds, and the
names on messages that Host sent it. Nothing asks the platform who a Host is:
a card must never be a way to learn a stranger's identity.

"Host", never "entity", in anything that reaches a screen.
"""
from __future__ import annotations

import logging

log = logging.getLogger("privatae.hosts.card")

#: How the relationship reads on the card, in the order it is decided.
RELATIONSHIPS = {
    "contact": "Contact",
    "pending": "Connection pending",
    "blocked": "Blocked",
    "shares_machine": "Shares a machine with you",
    "known": "Not connected",
}

MAX_IDS = 500


def _address(peer_host_name: str | None, *handles: str | None) -> str | None:
    """Another Host's address: the full name a peering carries, or None. A
    handle alone names no address: another Host's zone is not this one's, so
    a name built from it in this Host's zone would point at nothing."""
    return peer_host_name or None


async def host_cards(pool, me: int, ids) -> dict[int, dict]:
    """One card per Host id in `ids` (this Host's own id is left out)."""
    want = []
    for i in ids or []:
        try:
            n = int(i)
        except (TypeError, ValueError):
            continue
        if n > 0 and n != int(me) and n not in want:
            want.append(n)
    want = want[:MAX_IDS]
    if not want:
        return {}

    async def rows(sql, *a):
        try:
            return [dict(r) for r in await pool.fetch(sql, *a)]
        except Exception as e:           # one missing store never blanks the card
            log.info("host card: a store was unreadable (%s)", type(e).__name__)
            return []

    contacts = {r["peer_entity_id"]: r for r in await rows(
        "SELECT DISTINCT ON (peer_entity_id) peer_entity_id, handle, display_name, status "
        "  FROM contacts WHERE kind = 'host' AND peer_entity_id = ANY($1::int[]) "
        " ORDER BY peer_entity_id, (status = 'active') DESC, last_contact_at DESC NULLS LAST",
        want)}
    said = {r["from_entity_id"]: r for r in await rows(
        "SELECT DISTINCT ON (from_entity_id) from_entity_id, "
        "       payload->>'from_display_name' AS name, payload->>'from_handle' AS handle "
        "  FROM inbox_messages "
        " WHERE from_entity_id = ANY($1::int[]) AND from_user_email IS NULL "
        "   AND (payload ? 'from_display_name' OR payload ? 'from_handle') "
        " ORDER BY from_entity_id, sent_at DESC NULLS LAST",
        want)}
    slips = {r["peer_entity_id"]: r for r in await rows(
        "SELECT peer_entity_id, (expires_at IS NULL OR expires_at > now()) AS live "
        "  FROM held_slips WHERE peer_entity_id = ANY($1::int[])", want)}
    rostered = {r["member_entity_id"]: r for r in await rows(
        "SELECT DISTINCT ON (member_entity_id) member_entity_id, display_name "
        "  FROM group_members WHERE member_entity_id = ANY($1::int[]) "
        "   AND removed_at IS NULL AND COALESCE(display_name, '') <> '' "
        " ORDER BY member_entity_id, added_at DESC",
        want)}
    shared = {r["peer_entity_id"]: r for r in await rows(
        "SELECT peer_entity_id, owner_name, phone, phone_verified_at FROM peer_cards "
        " WHERE peer_entity_id = ANY($1::int[])", want)}
    try:
        from server.hosts.peer_cards import reverify_stale_numbers
        await reverify_stale_numbers(pool, shared)
    except Exception as e:
        log.info("host card: number re-check skipped (%s)", type(e).__name__)
    mine = None
    try:
        from server.hosts.self_card import own_card
        mine = await own_card()
    except Exception as e:
        log.info("host card: own card unavailable (%s)", type(e).__name__)
    my_hosts = set((mine or {}).get("account_hosts") or [])
    peerings: dict[int, dict] = {}
    try:
        from server.network.peer_discovery import list_peerings
        for p in await list_peerings(pool, int(me)):
            pid = p.get("peer_entity_id")
            if pid in want and (pid not in peerings or p.get("current")):
                peerings[pid] = p
    except Exception as e:
        log.info("host card: peerings unreadable (%s)", type(e).__name__)

    out = {}
    for n in want:
        c, s, p, g = contacts.get(n), said.get(n), peerings.get(n), rostered.get(n)
        name = ((c or {}).get("display_name") or (s or {}).get("name")
                or (g or {}).get("display_name") or (p or {}).get("peer_handle")
                or (c or {}).get("handle") or f"Host #{n}")
        if c and c.get("status") in ("active", "pending", "blocked"):
            rel = "contact" if c["status"] == "active" else c["status"]
        elif p:
            rel = "shares_machine"
        else:
            rel = "known"
        slip = slips.get(n)
        card = shared.get(n) or {}
        if n in my_hosts:
            # One of the user's OWN hosts (same account, per the platform —
            # host.self_card, self-scoped). Its user is the user.
            owner = (mine or {}).get("owner_name")
            belongs = "My host"
        else:
            owner = card.get("owner_name")
            belongs = f"{owner}'s host" if owner else None
        out[n] = {
            "host_id": n,
            "number": f"Host #{n}",
            "classification": "Host",
            "display_name": name,
            "address": _address((p or {}).get("peer_host_name"), (s or {}).get("handle"),
                                (c or {}).get("handle"), (p or {}).get("peer_handle")),
            # Whose host it is ("My host" / "Alex's host") when known; the
            # connection state is its own line.
            "relationship": rel,
            "relationship_label": belongs or RELATIONSHIPS[rel],
            "mine": n in my_hosts,
            "connection_label": RELATIONSHIPS[rel],
            "shares_machine": bool(p and p.get("current")),
            "can_message": bool(slip and slip.get("live")),
            # What that Host shared on an accepted connection, the number only once
            # the platform confirmed it is
            # that Host's. Empty, never guessed, when it has shared nothing.
            "contact_number": card.get("phone") or None,
            "user": owner or None,
            "sources": sorted(k for k, v in (("contact", c), ("messages", s), ("peering", p),
                                             ("roster", g), ("slip", slip), ("shared", card),
                                             ("account", n in my_hosts)) if v),
        }
    return out
