"""Domain policy — who can reach this container, decided by this container.

Backs the Policy rail tab in Circles (previously a dead button). The read/write:domain_policy scopes existed in
applications/scopes.py since v0.1 with nothing behind them; migration v048
adds the pod-local table and this router exposes it.

Model: one '*' row is the default posture; per-peer rows override it.
Enforcement happens pod-side in the inbox append path
(server/bridge/memory_handlers.handle_inbox_append) — cross-entity messages
from a denied peer are rejected at this pod's boundary. Platform
notifications (cseries@localhost) and self-sends always pass.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.domain_policy")

router = APIRouter(prefix="/api/domain-policy", tags=["domain-policy"])

POSTURES = {
    # allow_inbox / allow_dm / allow_circle_invite for the '*' row
    "open":     (True, True, True),
    "contacts": (False, False, False),   # '*' denies; per-contact rows allow
    "closed":   (False, False, False),
}


class RuleCreate(BaseModel):
    peer: str = Field(min_length=1, max_length=200)
    allow_inbox: bool = True
    allow_dm: bool = True
    allow_circle_invite: bool = True
    note: str | None = Field(None, max_length=500)


class PostureUpdate(BaseModel):
    posture: str  # open|contacts|closed


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


def _row_dict(r) -> dict:
    return {
        "id": r["id"],
        "peer": r["peer"],
        "allow_inbox": r["allow_inbox"],
        "allow_dm": r["allow_dm"],
        "allow_circle_invite": r["allow_circle_invite"],
        "note": r["note"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


async def _posture_of(pool) -> str:
    """Derive the named posture from the '*' row + a marker in its note."""
    row = await pool.fetchrow("SELECT note, allow_inbox FROM domain_policy WHERE peer = '*'")
    if row is None:
        return "open"
    note = row["note"] or ""
    for name in POSTURES:
        if note.startswith(f"posture:{name}"):
            return name
    return "open" if row["allow_inbox"] else "closed"


@router.get("", dependencies=[Depends(requires_permission("read:domain_policy"))])
async def get_policy() -> dict:
    pool = _pool()
    rows = await pool.fetch(
        """
        SELECT id, peer, allow_inbox, allow_dm, allow_circle_invite, note, created_at
          FROM domain_policy
         ORDER BY (peer = '*') DESC, peer
        """,
    )
    return {
        "ok": True,
        "posture": await _posture_of(pool),
        "rules": [_row_dict(r) for r in rows if r["peer"] != "*"],
    }


@router.put("/posture", dependencies=[Depends(requires_permission("write:domain_policy"))])
async def set_posture(body: PostureUpdate) -> dict:
    if body.posture not in POSTURES:
        raise HTTPException(400, f"posture must be one of {sorted(POSTURES)}")
    allow_inbox, allow_dm, allow_ci = POSTURES[body.posture]
    pool = _pool()
    await pool.execute(
        """
        INSERT INTO domain_policy (peer, allow_inbox, allow_dm, allow_circle_invite, note, updated_at)
        VALUES ('*', $1, $2, $3, $4, now())
        ON CONFLICT (peer) DO UPDATE
           SET allow_inbox = EXCLUDED.allow_inbox,
               allow_dm = EXCLUDED.allow_dm,
               allow_circle_invite = EXCLUDED.allow_circle_invite,
               note = EXCLUDED.note,
               updated_at = now()
        """,
        allow_inbox, allow_dm, allow_ci, f"posture:{body.posture}",
    )
    return {"ok": True, "posture": body.posture}


@router.post("/rules", dependencies=[Depends(requires_permission("write:domain_policy"))])
async def add_rule(body: RuleCreate) -> dict:
    peer = body.peer.strip()
    if peer == "*":
        raise HTTPException(400, "set the default posture via PUT /posture")
    pool = _pool()
    row = await pool.fetchrow(
        """
        INSERT INTO domain_policy (peer, allow_inbox, allow_dm, allow_circle_invite, note)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (peer) DO UPDATE
           SET allow_inbox = EXCLUDED.allow_inbox,
               allow_dm = EXCLUDED.allow_dm,
               allow_circle_invite = EXCLUDED.allow_circle_invite,
               note = EXCLUDED.note,
               updated_at = now()
        RETURNING id, peer, allow_inbox, allow_dm, allow_circle_invite, note, created_at
        """,
        peer, body.allow_inbox, body.allow_dm, body.allow_circle_invite, body.note,
    )
    return {"ok": True, "rule": _row_dict(row)}


@router.delete("/rules/{rule_id}", dependencies=[Depends(requires_permission("write:domain_policy"))])
async def delete_rule(rule_id: int) -> dict:
    pool = _pool()
    peer = await pool.fetchval(
        "DELETE FROM domain_policy WHERE id = $1 AND peer != '*' RETURNING peer", rule_id,
    )
    if not peer:
        raise HTTPException(404, "rule not found")
    return {"ok": True, "deleted": rule_id}


# Connection/permission REQUESTS are the entry point to the social graph —
# they must reach you even under a 'contacts only' posture, or you could
# never receive a first request to accept. They land as pending for review;
# they are not messages. (Grants are replies you only get after you asked.)
#
# `group_join` (2026-09-17) is here for exactly the reason the paragraph above
# gives, and the feature does not work without it. A join request is a STRANGER
# asking to be let into a group circle — that is the whole point of the op, and
# the reason it is deliberately absent from handlers._KINDS_NEEDING_A_CONTACT.
# Under a `*` rule with allow_inbox = FALSE it would otherwise fall through to
# the per-peer rule (none), then the active-contact check (not a contact, by
# definition), then that default, and be denied. The card would never reach the
# user and the owner would never learn they had been asked.
#
# It is safe on the same terms as the kinds beside it, and its bound is
# NARROWER than any of theirs: handlers._group_join_target refuses the knock
# unless it names a group THIS host owns, so unlike a bare connection request it
# cannot even be aimed at a host that has nothing to say yes to.
#
# `rendezvous_intro` is the platform's batched introduction. It usually arrives
# from a `cseries@` sender and returns True two checks earlier, but whether it
# does depends on how the bridge addresses its own push — another repo's
# decision. Naming the kind here makes the Host's admission the Host's own
# business. It carries a group id and a count, confers nothing, and the member
# joins only when the user says yes.
#
# NOT `machine_request`, deliberately. It requires an accepted contact before
# its card is ever written (handlers._KINDS_NEEDING_A_CONTACT), so it is not a
# first contact and has no claim on the stranger allowance these kinds get.
_REQUEST_KINDS = {"contact_request", "endpoint_request", "permission_request",
                  "endpoint_grant", "permission_grant",
                  "group_join", "rendezvous_intro"}


async def check_inbound_allowed(pool, from_address: str | None, from_entity_id: int | None,
                                self_entity_id: int | None, message_kind: str | None = None) -> bool:
    """Enforcement helper used by the inbox append path. Returns True when
    a message from `from_address` may land in this pod's inbox.

    Always allowed: platform senders (cseries@...), self-sends, messages with
    no foreign entity attribution (local app writes), and connection/
    permission requests + grants (the social-graph entry point).
    """
    addr = (from_address or "").strip().lower()
    if not addr or addr.startswith("cseries@"):
        return True
    if from_entity_id is not None and self_entity_id is not None and from_entity_id == self_entity_id:
        return True
    if from_entity_id is None and "@" not in addr:
        return True
    if message_kind in _REQUEST_KINDS:
        return True
    try:
        # Per-peer rule first (exact address, then handle local-part).
        local = addr.split("@", 1)[0]
        row = await pool.fetchrow(
            """
            SELECT allow_inbox FROM domain_policy
             WHERE lower(peer) = $1 OR lower(peer) = $2
             ORDER BY (lower(peer) = $1) DESC
             LIMIT 1
            """,
            addr, local,
        )
        if row is not None:
            return bool(row["allow_inbox"])
        # No explicit rule: an active (non-blocked) contact passes — this
        # is what makes the 'contacts only' posture mean contacts, not
        # 'peers with a hand-written rule'.
        is_contact = await pool.fetchval(
            """
            SELECT 1 FROM contacts
             WHERE (lower(handle) = $1 OR lower(handle) = $2)
               AND status = 'active'
             LIMIT 1
            """,
            addr, local,
        )
        if is_contact:
            return True
        default = await pool.fetchrow("SELECT allow_inbox FROM domain_policy WHERE peer = '*'")
        if default is not None:
            return bool(default["allow_inbox"])
        # Tables exist, no rule, no contact, no '*' default row: open posture (no policy authored).
        return True
    except Exception as exc:  # noqa: BLE001
        # C14 fail-closed: everything reaching this try is a cross-host, non-request message (all
        # the always-allow cases — platform sender, self-send, no-foreign-attribution, request/grant
        # kinds — returned True above before the DB touch). So a DB error here is deciding admission
        # for a FOREIGN message, and the only safe-by-default answer is DENY.
        #
        # The ONE exception is the genuine pre-migration window: the domain_policy / contacts tables
        # don't exist yet (Postgres 42P01 UndefinedTable). Before the table exists there is no policy
        # to enforce, so we preserve the old open posture for that case ONLY. Any other error
        # (connection drop, query error, type error, the table present but unreadable) fails CLOSED.
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate == "42P01":  # undefined_table — pre-migration; no policy exists yet
            logger.debug("domain_policy check skipped (table not migrated yet): %s", exc)
            return True
        logger.warning("domain_policy check failed — failing closed (deny inbound): %s", exc)
        return False
