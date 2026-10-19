"""What another Host told us about itself when a connection was accepted — its
owner's name and its phone number, shared on accept.

TRUST, field by field:
  * owner_name — the Host's own statement about its own person. That is
    authority the Host holds (the HOST vouches for its own people), and it
    is only ever taken from a Host we have ACCEPTED or that accepted us —
    never from a bare connection request. Bounded and cleaned, because it is
    another tenant's free text on our screen.
  * phone — checked against the platform before it is kept: the number must
    resolve (phone.alias.lookup) to the Host that sent it. So no Host can put
    somebody else's number on its card. An unverifiable number is dropped.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("privatae.hosts.peer_cards")

MAX_NAME = 80
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_PHONE = re.compile(r"^\+?[0-9][0-9\s\-.()]{5,20}$")


def clean_name(v) -> str | None:
    """Another tenant's text on our screen: drop control and FORMAT characters
    (zero-width, bidi overrides), turn every Unicode space into a plain one and
    collapse runs, so a name cannot pad itself into looking like "My host"."""
    if not isinstance(v, str):
        return None
    import unicodedata
    out = []
    for ch in v:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Co", "Cs"):
            continue
        out.append(" " if cat.startswith("Z") else ch)
    v = re.sub(r" +", " ", "".join(out)).strip()
    return v[:MAX_NAME] or None


async def _phone_belongs_to(phone: str, peer_id: int) -> str | None:
    """The canonical number when the platform says it is `peer_id`'s, else None."""
    if not isinstance(phone, str) or not _PHONE.match(phone.strip()):
        return None
    try:
        from server.routes.contacts import _bridge_op
        got = await _bridge_op("phone.alias.lookup", {"alias": phone.strip()})
    except Exception as e:
        log.info("peer card: phone check unavailable (%s) — number not kept", type(e).__name__)
        return None
    if isinstance(got, dict) and got.get("found") and int(got.get("entity_id") or 0) == int(peer_id):
        return got.get("alias") or phone.strip()
    log.info("peer card from %s: number does not belong to it — not kept", peer_id)
    return None


async def store_peer_card(pool, peer_id: int, card) -> dict | None:
    """Keep what `peer_id` shared. Returns what was stored, or None."""
    if not isinstance(card, dict):
        return None
    name = clean_name(card.get("owner_name"))
    offered = card.get("phone")
    phone = await _phone_belongs_to(offered, peer_id) if offered else None
    # THE NUMBER'S THREE CASES. Verified: replace. Offered but NOT verified (a
    # bridge hiccup, or a number that is not theirs): keep whatever verified
    # number we already hold — one failed check must not wipe a good one.
    # Not offered at all: the host stopped sharing a number, so clear it.
    keep_old = bool(offered) and phone is None
    if not (name or phone or not offered):
        return None
    try:
        await pool.execute(
            "INSERT INTO peer_cards (peer_entity_id, owner_name, phone, phone_verified_at, updated_at) "
            "VALUES ($1, $2, $3, CASE WHEN $3::text IS NULL THEN NULL ELSE now() END, now()) "
            "ON CONFLICT (peer_entity_id) DO UPDATE "
            "   SET owner_name = COALESCE(EXCLUDED.owner_name, peer_cards.owner_name), "
            "       phone = CASE WHEN $4 THEN peer_cards.phone ELSE EXCLUDED.phone END, "
            "       phone_verified_at = CASE WHEN $4 THEN peer_cards.phone_verified_at "
            "                                ELSE EXCLUDED.phone_verified_at END, "
            "       updated_at = now()",
            int(peer_id), name, phone, keep_old)
    except Exception as e:
        log.warning("peer card store for %s failed (non-fatal): %s", peer_id, type(e).__name__)
        return None
    return {"owner_name": name, "phone": phone}


#: A stored number is re-checked once it is older than this: a host may drop or
#: change its number, and the card must not keep vouching for the old one.
REVERIFY_AFTER_DAYS = 7
REVERIFY_MAX_PER_READ = 20


#: The whole re-check never holds a card read longer than this: the lookups run
#: concurrently, and whatever has not answered is simply re-checked next read.
REVERIFY_DEADLINE_S = 3.0


async def reverify_stale_numbers(pool, rows: dict) -> None:
    """Re-check up to REVERIFY_MAX_PER_READ stale numbers among `rows`
    ({peer_id: {phone, phone_verified_at}}), in place, concurrently and within
    REVERIFY_DEADLINE_S — a card read must not wait on a slow bridge. A number
    the platform no longer gives that host is cleared; a failed or unfinished
    lookup leaves it as it was."""
    import asyncio
    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=REVERIFY_AFTER_DAYS)
    stale = [(pid, r) for pid, r in rows.items()
             if r.get("phone") and not (r.get("phone_verified_at") and r["phone_verified_at"] > cutoff)]
    stale = stale[:REVERIFY_MAX_PER_READ]
    if not stale:
        return
    tasks = [asyncio.ensure_future(_recheck_one(pool, pid, r)) for pid, r in stale]
    done, pending = await asyncio.wait(tasks, timeout=REVERIFY_DEADLINE_S)
    for t in pending:
        t.cancel()


async def _recheck_one(pool, peer_id, r) -> None:
    try:
        from server.routes.contacts import _bridge_op
        got = await _bridge_op("phone.alias.lookup", {"alias": r["phone"]})
    except Exception:
        return
    still = isinstance(got, dict) and got.get("found") and int(got.get("entity_id") or 0) == int(peer_id)
    try:
        if still:
            await pool.execute("UPDATE peer_cards SET phone_verified_at = now() "
                               "WHERE peer_entity_id = $1", int(peer_id))
        else:
            await pool.execute("UPDATE peer_cards SET phone = NULL, phone_verified_at = NULL "
                               "WHERE peer_entity_id = $1", int(peer_id))
            r["phone"] = None
    except Exception as e:
        log.info("peer card re-check for %s not recorded (%s)", peer_id, type(e).__name__)
