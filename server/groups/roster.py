"""The roster: create a group, add and remove members, set roles, keep epochs,
and hold the replica of someone else's group.

THE OWNER'S COPY AND A MEMBER'S REPLICA ARE THE SAME TABLES. A `groups` row
whose owner_entity_id is this host is authoritative; any other row is a replica
refreshed by `group.roster_fetch` and never written by a local route. Every
function below that MUTATES a roster asserts ownership first (`_require_owner`)
— that assertion is the only thing keeping a member's replica read-only, so it
is one function and every writer goes through it.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import uuid

log = logging.getLogger("privatae.groups.roster")

#: THE CREATOR-HELD TIER'S ROLES, and they are deliberately NOT communityd's.
#: communityd has `member | moderator` only, with no admin — a community group
#: is on the PLATFORM's surface, so a creator who could name admins there would
#: be handing out platform moderation by starting a group, and moderator is
#: assigned only from the operator side.
#:
#: Here the opposite holds and these four are right: a group circle is one
#: person's room. The roles are labels its owner gives, and they confer nothing
#: by themselves — every mutation below goes through _require_owner, so `admin`
#: is a name, not a capability. Do not "reconcile" the two vocabularies; the
#: asymmetry is the decision.
ROLES = ("creator", "admin", "moderator", "member")
#: Roles a member may be SET to. `creator` is not assignable — it is where the
#: group came from, and a group with two creators (or none) has no owner.
ASSIGNABLE_ROLES = ("admin", "moderator", "member")

MAX_NAME_LEN = 120
MAX_DESCRIPTION_LEN = 2000

#: A group id is 'grp-<owner entity id>-<uuid4 hex>', minted by the creator's
#: host: globally unique with no registry and no allocation round-trip, the
#: same reason the bridge mints contract_id as "slip-" + uuid4().hex
#: (bridge/adapter.py:2419). The OWNER IS IN THE ID so a member can refuse a
#: group grant from anyone but the owner it names: with a bare uuid the first
#: host to grant an id to a member kept it, so any member of a group could
#: squat that group's id on a contact before the real invite arrived.
#: The controller's GROUP_REF_RE (appstore service.py) accepts this shape.
_GROUP_ID_RE = re.compile(r"^grp-([1-9][0-9]{0,9})-[0-9a-f]{32}$")
#: A ballot id is the caller's own stable token — it is how a retry is
#: idempotent — so it comes off the wire and is bounded like every other
#: untrusted token on the lane (app_lane._ACTION_RE's shape).
_BALLOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class RosterError(Exception):
    """A refusal the route surfaces. `status` is the HTTP code, `detail` the
    sentence — the shape pairing_grants.PairingError uses
    (server/network/pairing_grants.py:768-774)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def new_group_id(owner_entity_id: int) -> str:
    return f"grp-{int(owner_entity_id)}-{uuid.uuid4().hex}"


def group_id_owner(v) -> int | None:
    """The owner a group id names, or None when it is not a group id."""
    m = _GROUP_ID_RE.match(v) if isinstance(v, str) else None
    return int(m.group(1)) if m else None


def valid_group_id(v) -> bool:
    return isinstance(v, str) and bool(_GROUP_ID_RE.match(v))


def valid_ballot_id(v) -> bool:
    return isinstance(v, str) and bool(_BALLOT_ID_RE.match(v))


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ── Reads ──────────────────────────────────────────────────────────────────

async def get_group(pool, group_id: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT group_id, owner_entity_id, name, description, roster_epoch, "
        "synced_epoch, synced_at, created_at, updated_at, revoked_at "
        "FROM groups WHERE group_id = $1", group_id)
    return dict(row) if row else None


async def list_groups(pool, entity_id: int) -> list[dict]:
    """Groups this host owns, plus groups it is a current member of. One query
    rather than two lists the caller has to merge, because the surface is "my
    groups" and ownership is a property of a group, not a separate collection."""
    rows = await pool.fetch(
        "SELECT g.group_id, g.owner_entity_id, g.name, g.description, "
        "       g.roster_epoch, g.synced_epoch, g.synced_at, g.revoked_at, "
        "       (g.owner_entity_id = $1) AS owned, "
        "       (SELECT count(*) FROM group_members m "
        "         WHERE m.group_id = g.group_id AND m.removed_at IS NULL) AS members "
        "  FROM groups g "
        " WHERE g.owner_entity_id = $1 "
        "    OR EXISTS (SELECT 1 FROM group_members m "
        "                WHERE m.group_id = g.group_id "
        "                  AND m.member_entity_id = $1 AND m.removed_at IS NULL) "
        " ORDER BY g.created_at DESC", int(entity_id))
    return [dict(r) for r in rows]


async def roster(pool, group_id: str, *, include_removed: bool = False) -> list[dict]:
    """The roster rows. include_removed is for a TALLY over a closed ballot:
    a vote from someone who has since left is still attributable, and a tally
    that cannot see the removed row cannot say so."""
    if include_removed:
        rows = await pool.fetch(
            "SELECT member_entity_id, role, verify_pubkey, key_pinned_at, "
            "       display_name, added_at, removed_at "
            "FROM group_members WHERE group_id = $1 ORDER BY member_entity_id", group_id)
    else:
        rows = await pool.fetch(
            "SELECT member_entity_id, role, verify_pubkey, key_pinned_at, "
            "       display_name, added_at, removed_at "
            "FROM group_members WHERE group_id = $1 AND removed_at IS NULL "
            "ORDER BY member_entity_id", group_id)
    return [dict(r) for r in rows]


async def is_current_member(pool, group_id: str, entity_id: int) -> bool:
    """THE membership predicate. Fails CLOSED: a lookup that raises is not a
    yes. Used at app.deliver time to gate a group-scoped slip, where a false
    positive admits a removed member (server/peer/app_lane.py)."""
    try:
        return bool(await pool.fetchval(
            "SELECT 1 FROM group_members WHERE group_id = $1 AND member_entity_id = $2 "
            "AND removed_at IS NULL", group_id, int(entity_id)))
    except Exception as e:
        log.warning("group %s membership check failed: %s", group_id, type(e).__name__)
        return False


async def member_role(pool, group_id: str, entity_id: int) -> str | None:
    return await pool.fetchval(
        "SELECT role FROM group_members WHERE group_id = $1 AND member_entity_id = $2 "
        "AND removed_at IS NULL", group_id, int(entity_id))


# ── Ownership ──────────────────────────────────────────────────────────────

async def _require_owner(pool, group_id: str, entity_id: int) -> dict:
    """The one gate every mutation passes. A replica of someone else's group
    lives in the same tables, so without this a local route would happily edit
    a roster this host does not own and then have it silently reverted by the
    next pull — the worst shape of a write, one that looks like it worked."""
    g = await get_group(pool, group_id)
    if g is None:
        raise RosterError(404, "no such group")
    if int(g["owner_entity_id"]) != int(entity_id):
        raise RosterError(403, "this host does not own that group — its creator's host holds the roster")
    if g.get("revoked_at") is not None:
        raise RosterError(409, "that group is closed")
    return g


# ── Writes (owner only) ────────────────────────────────────────────────────

async def create_group(pool, entity_id: int, *, name: str, description: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise RosterError(400, "a group needs a name")
    gid = new_group_id(entity_id)
    now = _now()
    await pool.execute(
        "INSERT INTO groups (group_id, owner_entity_id, name, description, "
        "roster_epoch, created_at, updated_at) VALUES ($1, $2, $3, $4, 1, $5, $5)",
        gid, int(entity_id), name[:MAX_NAME_LEN],
        (description or "")[:MAX_DESCRIPTION_LEN], now)
    # The creator is on their own roster from the first moment, with their own
    # key already pinned. Otherwise the creator would be the one member unable
    # to vote in their own group until they pulled a roster from themselves.
    await pool.execute(
        "INSERT INTO group_members (group_id, member_entity_id, role, added_at) "
        "VALUES ($1, $2, 'creator', $3) ON CONFLICT (group_id, member_entity_id) DO NOTHING",
        gid, int(entity_id), now)
    await pin_own_key(pool, gid, int(entity_id))
    log.info("group %s created by entity %s", gid, entity_id)
    return await get_group(pool, gid)


async def _bump_epoch(pool, group_id: str) -> int:
    return await pool.fetchval(
        "UPDATE groups SET roster_epoch = roster_epoch + 1, updated_at = now() "
        "WHERE group_id = $1 RETURNING roster_epoch", group_id)


async def add_member(pool, entity_id: int, group_id: str, member: int, *,
                     role: str = "member", display_name: str = "") -> dict:
    """Add (or re-admit) a member. Idempotent: adding a current member returns
    the roster unchanged rather than bumping the epoch, so a retried call does
    not make every replica re-pull for nothing."""
    await _require_owner(pool, group_id, entity_id)
    if int(member) == int(entity_id):
        raise RosterError(400, "the creator is already on the roster")
    if role not in ASSIGNABLE_ROLES:
        raise RosterError(400, f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
    existing = await pool.fetchrow(
        "SELECT role, removed_at FROM group_members WHERE group_id = $1 AND member_entity_id = $2",
        group_id, int(member))
    if existing is not None and existing["removed_at"] is None:
        return {"group_id": group_id, "member_entity_id": int(member),
                "role": existing["role"], "added": False,
                "roster_epoch": (await get_group(pool, group_id))["roster_epoch"]}
    # A re-admit clears removed_at but NOT verify_pubkey: the key is pinned
    # first-key-wins and their old votes stay verifiable under it. A member who
    # genuinely rotated re-pins by presenting the same key they now hold, which
    # is refused, and that refusal is the signal — not something to paper over
    # by clearing the pin on the way back in.
    await pool.execute(
        "INSERT INTO group_members (group_id, member_entity_id, role, display_name, added_at) "
        "VALUES ($1, $2, $3, $4, now()) "
        "ON CONFLICT (group_id, member_entity_id) DO UPDATE "
        "   SET removed_at = NULL, role = EXCLUDED.role, added_at = now(), "
        "       display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), group_members.display_name)",
        group_id, int(member), role, (display_name or "")[:MAX_NAME_LEN])
    epoch = await _bump_epoch(pool, group_id)
    log.info("group %s: entity %s added as %s (epoch %s)", group_id, member, role, epoch)
    return {"group_id": group_id, "member_entity_id": int(member), "role": role,
            "added": True, "roster_epoch": epoch}


async def remove_member(pool, entity_id: int, group_id: str, member: int) -> dict:
    """Stamp a member removed. NEVER a DELETE — their votes must stay
    attributable, and a tally over a closed ballot has to be able to say who
    was on the roster when it closed."""
    await _require_owner(pool, group_id, entity_id)
    if int(member) == int(entity_id):
        raise RosterError(400, "the creator cannot be removed from their own group")
    n = await pool.execute(
        "UPDATE group_members SET removed_at = now() "
        "WHERE group_id = $1 AND member_entity_id = $2 AND removed_at IS NULL",
        group_id, int(member))
    if n == "UPDATE 0":
        raise RosterError(404, "that host is not a current member")
    epoch = await _bump_epoch(pool, group_id)
    log.info("group %s: entity %s removed (epoch %s)", group_id, member, epoch)
    return {"group_id": group_id, "member_entity_id": int(member),
            "removed": True, "roster_epoch": epoch}


async def set_role(pool, entity_id: int, group_id: str, member: int, role: str) -> dict:
    await _require_owner(pool, group_id, entity_id)
    if role not in ASSIGNABLE_ROLES:
        raise RosterError(400, f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
    if int(member) == int(entity_id):
        raise RosterError(400, "the creator's role is not assignable")
    n = await pool.execute(
        "UPDATE group_members SET role = $3 "
        "WHERE group_id = $1 AND member_entity_id = $2 AND removed_at IS NULL",
        group_id, int(member), role)
    if n == "UPDATE 0":
        raise RosterError(404, "that host is not a current member")
    epoch = await _bump_epoch(pool, group_id)
    return {"group_id": group_id, "member_entity_id": int(member), "role": role,
            "roster_epoch": epoch}


# ── Key pinning ────────────────────────────────────────────────────────────

async def pin_key(pool, group_id: str, member: int, pubkey: bytes) -> tuple[bool, str]:
    """Pin a member's verify key. FIRST KEY WINS.

    (changed, reason). A different key arriving later is REFUSED and reported,
    never overwritten — arm_hub_pdp treats exactly this as an alarm because the
    key derives from the write-once entity_sign row and does not legitimately
    change (server/network/hub_peers.py:120-135). Overwriting would let anyone
    who could reach this path retro-invalidate every vote that member has cast.

    The CALLER is responsible for having established `member` from a certificate
    and not from a request body. On the peer lane that is peer_server's cert CN
    (server/peer/peer_server.py:123-125, :242).
    """
    if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != 32:
        return False, "a verify key is 32 raw bytes"
    row = await pool.fetchrow(
        "SELECT verify_pubkey FROM group_members WHERE group_id = $1 AND member_entity_id = $2",
        group_id, int(member))
    if row is None:
        return False, "not a member of that group"
    have = row["verify_pubkey"]
    if have:
        if bytes(have) == bytes(pubkey):
            return False, "already pinned"
        log.warning("group %s: entity %s presented a DIFFERENT verify key — refused",
                    group_id, member)
        return False, ("a different verify key is already pinned for this member; "
                       "a key change is a re-join, never an overwrite")
    await pool.execute(
        "UPDATE group_members SET verify_pubkey = $3, key_pinned_at = now() "
        "WHERE group_id = $1 AND member_entity_id = $2 AND verify_pubkey IS NULL",
        group_id, int(member), bytes(pubkey))
    return True, "pinned"


async def pin_own_key(pool, group_id: str, entity_id: int) -> bool:
    """Pin THIS host's own key on a roster it holds. Same first-key-wins rule;
    the key comes from the pod's own kernel_secrets, so there is no identity to
    establish."""
    try:
        from server.network import nac_stepup
        pub = await nac_stepup.entity_verify_pubkey(pool, int(entity_id))
    except Exception as e:
        log.warning("group %s: could not read this host's verify key: %s", group_id, type(e).__name__)
        return False
    ok, _reason = await pin_key(pool, group_id, int(entity_id), pub)
    return ok


# ── Replica (a group someone else owns) ────────────────────────────────────

async def apply_roster_snapshot(pool, *, group_id: str, owner_entity_id: int,
                                name: str, description: str, roster_epoch: int,
                                members: list) -> dict:
    """Write a roster snapshot pulled from its owner.

    REPLACE, not merge. The owner's snapshot is the whole truth about its own
    roster, so a member that is present locally and absent from the snapshot is
    stamped removed rather than kept — a merge would make a removal the one
    change replication could never carry, which is the change that matters most
    (it is what cuts off a removed member's group-scoped app slip).

    Only applied when the snapshot's epoch is at least the one already stored:
    an out-of-order answer from a retry must never walk a replica backwards.
    """
    existing = await get_group(pool, group_id)
    if existing is not None and int(existing["owner_entity_id"]) != int(owner_entity_id):
        raise RosterError(409, "a group with that id is already held for a different owner")
    if existing is not None and existing.get("synced_epoch") is not None \
            and int(roster_epoch) < int(existing["synced_epoch"]):
        return {"group_id": group_id, "applied": False,
                "reason": "snapshot is older than the roster already held",
                "roster_epoch": existing["synced_epoch"]}

    await pool.execute(
        "INSERT INTO groups (group_id, owner_entity_id, name, description, roster_epoch, "
        "synced_epoch, synced_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $5, now(), now()) "
        "ON CONFLICT (group_id) DO UPDATE "
        "   SET name = EXCLUDED.name, description = EXCLUDED.description, "
        "       roster_epoch = EXCLUDED.roster_epoch, synced_epoch = EXCLUDED.synced_epoch, "
        "       synced_at = now(), updated_at = now()",
        group_id, int(owner_entity_id), (name or "")[:MAX_NAME_LEN],
        (description or "")[:MAX_DESCRIPTION_LEN], int(roster_epoch))

    seen = []
    for m in members or []:
        try:
            mid = int(m["member_entity_id"])
        except (KeyError, TypeError, ValueError):
            continue
        role = m.get("role") if m.get("role") in ROLES else "member"
        pub = m.get("verify_pubkey")
        if isinstance(pub, str):
            import base64
            try:
                pub = base64.b64decode(pub)
            except Exception:
                pub = None
        if not (isinstance(pub, (bytes, bytearray)) and len(pub) == 32):
            pub = None
        # A datetime, not the snapshot's ISO string (asyncpg binds $6::timestamptz
        # only from a datetime): with the string, the first REMOVED member in a
        # roster raised mid-loop, so the rest of the roster, the removal stamp
        # and the ballots never applied — once an owner removed anyone, no member
        # could receive a new ballot.
        removed = _snapshot_time(m.get("removed_at"))
        seen.append(mid)
        await pool.execute(
            "INSERT INTO group_members (group_id, member_entity_id, role, verify_pubkey, "
            "display_name, removed_at) VALUES ($1, $2, $3, $4, $5, $6::timestamptz) "
            "ON CONFLICT (group_id, member_entity_id) DO UPDATE "
            # The owner's key for a member is authoritative ON A REPLICA: this
            # host never established that member's identity itself, so it has
            # nothing better to keep. COALESCE, not overwrite-with-NULL, so a
            # snapshot taken before the member pulled does not blank a key a
            # later snapshot already carried.
            "   SET role = EXCLUDED.role, "
            "       verify_pubkey = COALESCE(EXCLUDED.verify_pubkey, group_members.verify_pubkey), "
            "       display_name = EXCLUDED.display_name, removed_at = EXCLUDED.removed_at",
            group_id, mid, role, pub, (m.get("display_name") or "")[:MAX_NAME_LEN],
            removed)
    # Anyone the snapshot did not mention is no longer on that roster.
    await pool.execute(
        "UPDATE group_members SET removed_at = now() "
        "WHERE group_id = $1 AND removed_at IS NULL AND NOT (member_entity_id = ANY($2::int[]))",
        group_id, seen)
    log.info("group %s: roster replica applied at epoch %s (%d members)",
             group_id, roster_epoch, len(seen))
    return {"group_id": group_id, "applied": True, "roster_epoch": int(roster_epoch),
            "members": len(seen)}


def snapshot_member(row: dict) -> dict:
    """One roster row as it travels. verify_pubkey goes base64 because the
    frame is JSON (peer_client.encode_frame, server/peer/peer_client.py:45-54)."""
    import base64
    pub = row.get("verify_pubkey")
    return {
        "member_entity_id": int(row["member_entity_id"]),
        "role": row.get("role") or "member",
        "display_name": row.get("display_name") or "",
        "verify_pubkey": base64.b64encode(bytes(pub)).decode() if pub else None,
        "removed_at": row["removed_at"].isoformat() if row.get("removed_at") else None,
    }


# ── Ballots ────────────────────────────────────────────────────────────────

async def create_ballot(pool, entity_id: int, group_id: str, *, ballot_id: str,
                        question: str, choices: list, closes_at, target_kind: str = "",
                        target_ref: str = "") -> dict:
    from server.groups import votes as V
    await _require_owner(pool, group_id, entity_id)
    if not valid_ballot_id(ballot_id):
        raise RosterError(400, "ballot_id must be a short token: [a-z0-9][a-z0-9._-]{0,63}")
    if not (question or "").strip():
        raise RosterError(400, "a ballot needs a question")
    if not V.valid_choices(choices):
        raise RosterError(400, f"choices must be {V.MIN_CHOICES}-{V.MAX_CHOICES} distinct short strings")
    if closes_at is None:
        raise RosterError(400, "closes_at is required — a ballot with no close has no tally")
    now = _now()
    if closes_at <= now:
        raise RosterError(400, "closes_at is in the past")
    # A ballot must not outlive the relationship term that carries its votes:
    # the bridge caps a contact term at 366 days (_CONTACT_TERM_MAX,
    # bridge/adapter.py:2316), and a group slip is issued on that term.
    if closes_at > now + _dt.timedelta(days=366):
        raise RosterError(400, "closes_at is further out than the 366-day term a group slip can hold")
    await pool.execute(
        "INSERT INTO group_ballots (group_id, ballot_id, question, choices, target_kind, "
        "target_ref, created_by, opens_at, closes_at) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9) "
        "ON CONFLICT (group_id, ballot_id) DO NOTHING",
        group_id, ballot_id, question.strip(), json.dumps(list(choices)),
        target_kind[:64], target_ref[:256], int(entity_id), now, closes_at)
    return await get_ballot(pool, group_id, ballot_id)


async def get_ballot(pool, group_id: str, ballot_id: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT group_id, ballot_id, question, choices, target_kind, target_ref, "
        "created_by, opens_at, closes_at, closed_at, created_at "
        "FROM group_ballots WHERE group_id = $1 AND ballot_id = $2", group_id, ballot_id)
    if row is None:
        return None
    d = dict(row)
    if isinstance(d.get("choices"), str):
        try:
            d["choices"] = json.loads(d["choices"])
        except ValueError:
            d["choices"] = []
    return d


async def list_ballots(pool, group_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    rows = await pool.fetch(
        "SELECT group_id, ballot_id, question, choices, target_kind, target_ref, "
        "created_by, opens_at, closes_at, closed_at, created_at "
        "FROM group_ballots WHERE group_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        group_id, limit, max(0, int(offset or 0)))
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("choices"), str):
            try:
                d["choices"] = json.loads(d["choices"])
            except ValueError:
                d["choices"] = []
        out.append(d)
    return out


#: The byte budget for the ballots one roster fetch carries. A member needs a
#: ballot's row in its own pod to vote on it (routes/groups.py cast_vote looks
#: it up locally), and the roster fetch is the call a member already makes to
#: its owner, so the open ballots ride on it. Newest first; a ballot that does
#: not fit the remaining budget is skipped rather than making the whole answer
#: unreadable (peer_client.MAX_RESPONSE_BYTES is the ceiling).
BALLOT_SNAPSHOT_BYTES = 1024 * 1024


async def open_ballot_snapshots(pool, group_id: str) -> list[dict]:
    """The ballots a member can still vote on, as they travel. Nothing sets
    closed_at today, so closes_at alone decides; both are checked so a future
    early close stops travelling without a change here."""
    rows = await pool.fetch(
        "SELECT ballot_id, question, choices, target_kind, target_ref, created_by, "
        "opens_at, closes_at, closed_at "
        "FROM group_ballots WHERE group_id = $1 AND closed_at IS NULL AND closes_at > now() "
        "ORDER BY created_at DESC",
        group_id)
    out, used = [], 0
    for r in rows:
        d = dict(r)
        choices = d.get("choices")
        if isinstance(choices, str):
            try:
                choices = json.loads(choices)
            except ValueError:
                continue
        snap = {
            "ballot_id": d["ballot_id"],
            "question": d.get("question") or "",
            "choices": list(choices or []),
            "target_kind": d.get("target_kind") or "",
            "target_ref": d.get("target_ref") or "",
            "opens_at": d["opens_at"].isoformat() if d.get("opens_at") else None,
            "closes_at": d["closes_at"].isoformat(),
        }
        size = len(json.dumps(snap, separators=(",", ":")))
        if used + size > BALLOT_SNAPSHOT_BYTES:
            # continue, not break: one oversized question must not hold back
            # every smaller, older ballot behind it.
            continue
        used += size
        out.append(snap)
    return out


def _snapshot_time(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        t = _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


async def apply_ballot_snapshot(pool, *, group_id: str, owner_entity_id: int,
                                ballots: list) -> int:
    """Store the owner's open ballots on this member's replica.

    The owner is the only host that can create a ballot in its group
    (create_ballot -> _require_owner), so a replica row is the owner's row and
    the owner's copy wins on conflict. The caller has already checked, through
    apply_roster_snapshot, that this group is held for this owner. Every field
    is re-validated here because it arrived over the wire: a ballot the owner's
    own create path would have refused is skipped, not stored.
    """
    from server.groups import votes as V
    n = 0
    for b in ballots or []:
        if not isinstance(b, dict):
            continue
        bid, question, choices = b.get("ballot_id"), b.get("question"), b.get("choices")
        closes = _snapshot_time(b.get("closes_at"))
        if not valid_ballot_id(bid) or not isinstance(question, str) or not question.strip() \
                or not V.valid_choices(choices) or closes is None:
            continue
        opens = _snapshot_time(b.get("opens_at")) or _now()
        await pool.execute(
            "INSERT INTO group_ballots (group_id, ballot_id, question, choices, target_kind, "
            "target_ref, created_by, opens_at, closes_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9) "
            "ON CONFLICT (group_id, ballot_id) DO UPDATE "
            "   SET question = EXCLUDED.question, choices = EXCLUDED.choices, "
            "       target_kind = EXCLUDED.target_kind, target_ref = EXCLUDED.target_ref, "
            "       opens_at = EXCLUDED.opens_at, closes_at = EXCLUDED.closes_at",
            group_id, bid, question.strip(), json.dumps(list(choices)),
            str(b.get("target_kind") or "")[:64], str(b.get("target_ref") or "")[:256],
            int(owner_entity_id), opens, closes)
        n += 1
    return n


# ── Votes ──────────────────────────────────────────────────────────────────

async def held_seq(pool, group_id: str, voter: int) -> int | None:
    """The highest seq this voter has used ANYWHERE in this group.

    SCOPE IS PER (group, voter), NOT per ballot — the rule communityd already
    applies, so a member's counter means the same thing whichever tier stores
    the vote. Per-ballot would make seq mean "how many times I changed my mind
    on this one question", which stops being a useful anti-replay counter as
    soon as a member votes on several ballots: each would separately accept a
    seq of 1. One counter per member per group is simpler to reason about and
    strictly harder to replay.

    NULL when this voter has never voted in this group.
    """
    return await pool.fetchval(
        "SELECT max(seq) FROM group_votes WHERE group_id = $1 AND voter_entity_id = $2",
        group_id, int(voter))


async def store_vote(pool, group_id: str, ballot_id: str, vote: dict, *,
                     verified: bool, verify_error: str | None) -> None:
    """Write (or supersede) one vote row. Guarded on seq IN SQL as well as in
    the caller: two concurrent submissions from the same member would otherwise
    both read the same held_seq and the later-committing one could be the older
    vote. The WHERE clause makes the database the arbiter."""
    terms = vote["terms"]
    sig = vote.get("signature") or ""
    import base64
    try:
        raw_sig = base64.b64decode(sig)
    except Exception:
        raw_sig = b""
    # A DATETIME, not the terms' ISO string: asyncpg binds `$n::timestamptz`
    # only from a datetime and raised DataError on the string, so once no
    # vote was ever stored — a member's or the owner's own — and every voter
    # saw "could not store the vote" (seen in a live end-to-end test).
    # accept_vote refuses an unparseable signed_at before this.
    when = _snapshot_time(terms.get("signed_at"))
    if when is None:
        raise ValueError("signed_at is not a timestamp")
    await pool.execute(
        "INSERT INTO group_votes (group_id, ballot_id, voter_entity_id, choice, seq, "
        "nonce, signed_at, signature, key_id, verified, verify_error) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz, $8, $9, $10, $11) "
        "ON CONFLICT (group_id, ballot_id, voter_entity_id) DO UPDATE "
        "   SET choice = EXCLUDED.choice, seq = EXCLUDED.seq, nonce = EXCLUDED.nonce, "
        "       signed_at = EXCLUDED.signed_at, signature = EXCLUDED.signature, "
        "       key_id = EXCLUDED.key_id, verified = EXCLUDED.verified, "
        "       verify_error = EXCLUDED.verify_error, received_at = now() "
        " WHERE EXCLUDED.seq > group_votes.seq",
        group_id, ballot_id, int(terms["voter"]), terms["choice"], int(terms["seq"]),
        terms["nonce"], when, raw_sig, vote.get("key_id") or "entity_sign",
        bool(verified), verify_error)


async def ballot_votes(pool, group_id: str, ballot_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT voter_entity_id, choice, seq, nonce, signed_at, signature, key_id, "
        "verified, verify_error, received_at "
        "FROM group_votes WHERE group_id = $1 AND ballot_id = $2 ORDER BY voter_entity_id",
        group_id, ballot_id)
    return [dict(r) for r in rows]


async def tally_ballot(pool, group_id: str, ballot_id: str) -> dict:
    """The recount. Uses the roster INCLUDING removed members, because a tally
    has to be able to report a vote from someone who has since left rather than
    quietly not see it."""
    from server.groups import votes as V
    ballot = await get_ballot(pool, group_id, ballot_id)
    if ballot is None:
        raise RosterError(404, "no such ballot")
    rows = await roster(pool, group_id, include_removed=True)
    vs = await ballot_votes(pool, group_id, ballot_id)
    out = V.tally(rows, vs, choices=list(ballot.get("choices") or []))
    out["group_id"] = group_id
    out["ballot_id"] = ballot_id
    out["question"] = ballot["question"]
    out["closes_at"] = ballot["closes_at"].isoformat() if ballot.get("closes_at") else None
    return out
