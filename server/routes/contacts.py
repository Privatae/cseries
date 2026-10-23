"""Contacts — pod-local address book for the Circles app.

Backs the Contacts rail tab (previously a dead button).
The table is pod-local (migration v048): each entity owns its own address
book; the platform never holds it. Handles are synaptive handles for hosts,
or inbox addresses (someone@user.example) for humans.

Accepting an inbound `endpoint_request` inbox message creates a contact here
and attempts an `endpoint_grant` reply. The reply is best-effort: the bridge
refuses cross-entity inbox sends today, so until that platform op lands the
contact is recorded locally with status 'pending' and the UI says so honestly.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from server import brain_service
from server.applications.middleware import requires_any_permission, requires_permission

logger = logging.getLogger("privatae.contacts")

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


class ContactCreate(BaseModel):
    handle: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(None, max_length=200)
    kind: str = "host"                 # host|human
    notes: str | None = Field(None, max_length=2000)
    status: str = "active"             # active|pending|blocked


class ContactUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=2000)
    status: str | None = None          # active|pending|blocked


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


_ROW_COLS = ("id, handle, display_name, kind, notes, status, "
             "last_contact_at, created_at, peer_entity_id")


def _row_dict(r) -> dict:
    return {
        "id": r["id"],
        "handle": r["handle"],
        "display_name": r["display_name"],
        "kind": r["kind"],
        "notes": r["notes"],
        "status": r["status"],
        "last_contact_at": r["last_contact_at"].isoformat() if r["last_contact_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        # The host this contact is reached through (v123). A host contact's own
        # id; a person's host. NULL until an exchange has carried it.
        "peer_entity_id": r["peer_entity_id"],
    }


def clean_email(value) -> str | None:
    """A person's address, normalised the way inbox_messages stores it
    (lowercase, one mailbox) — or None when it is not one."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or "@" not in v or " " in v or len(v) > 320:
        return None
    local, _, domain = v.rpartition("@")
    if not local or "." not in domain:
        return None
    return v


@router.get("/list", dependencies=[Depends(requires_permission("read:address_book"))])
async def list_contacts(
    status: str | None = Query(None),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    pool = _pool()
    try:
        return await list_contacts_service(pool, status=status, q=q, limit=limit, offset=offset)
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


@router.post("", dependencies=[Depends(requires_permission("write:address_book"))])
async def create_contact(body: ContactCreate) -> dict:
    pool = _pool()
    try:
        return await create_contact_service(
            pool, handle=body.handle, display_name=body.display_name,
            kind=body.kind, notes=body.notes, status=body.status,
        )
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


@router.patch("/{contact_id}", dependencies=[Depends(requires_permission("write:address_book"))])
async def update_contact(contact_id: int, body: ContactUpdate) -> dict:
    pool = _pool()
    # _provided omitted → the service uses "non-None means set" (the exact
    # behavior of the old route: it iterated non-None attrs, so a contact
    # field can't be nulled, only changed).
    try:
        return await update_contact_service(
            pool, contact_id=contact_id, display_name=body.display_name,
            notes=body.notes, status=body.status,
        )
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


@router.delete("/{contact_id}", dependencies=[Depends(requires_permission("write:address_book"))])
async def delete_contact(contact_id: int) -> dict:
    pool = _pool()
    try:
        return await delete_contact_service(pool, contact_id=contact_id)
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/accept-request", dependencies=[Depends(requires_permission("write:address_book"))])
async def accept_request(body: dict, request: Request) -> dict:
    """Accept an inbound endpoint_request inbox message: record the sender
    as a contact and attempt an endpoint_grant reply.

    Body: {message_id, handle, display_name?, from_entity_id?, kind?,
    from_user_email?}. kind='human' + from_user_email (both from the request's
    payload, stamped by the bridge) make it a person: the contact is keyed by
    their email and the grant we return names OUR person (the verified
    X-Authentik-Email on this request). The grant reply is best-effort — if
    the requester id is unknown or delivery fails the contact stays 'pending'
    and `grant_delivered` is false. The UI surfaces that state.
    """
    pool = _pool()
    from server.operator_identity import request_email
    try:
        return await accept_request_service(
            pool,
            handle=(body.get("handle") or ""),
            message_id=body.get("message_id"),
            display_name=body.get("display_name"),
            from_entity_id=body.get("from_entity_id"),
            kind=(body.get("kind") or "host"),
            from_user_email=body.get("from_user_email"),
            my_email=request_email(request.headers),
        )
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    note: str | None = Field(None, max_length=500)


@router.post("/request-by-email", dependencies=[Depends(requires_permission("write:address_book"))])
async def request_by_email(body: EmailRequest, request: Request) -> dict:
    """Connect with a PERSON by their account email (Circles: Connect, under your cards). The
    request is delivered to the host that person runs; nothing about whether
    the address exists comes back — the answer is their acceptance or silence."""
    pool = _pool()
    from server.operator_identity import request_email
    try:
        return await request_by_email_service(
            pool, email=body.email, note=body.note,
            my_email=request_email(request.headers))
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


# ── Bridge helper + phone-alias contact requests ──────────────────────

async def _bridge_op(op: str, params: dict) -> dict:
    from server.main import get_bridge_client
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(503, "bridge not connected")
    envelope = await bc.request_full(op, params)
    if not envelope.get("ok"):
        raise HTTPException(int(envelope.get("code") or 502),
                            envelope.get("error") or f"{op} failed")
    return envelope.get("data") or {}


class PhoneRequest(BaseModel):
    alias: str
    note: str | None = Field(None, max_length=500)


@router.post("/request-by-phone", dependencies=[Depends(requires_permission("write:address_book"))])
async def request_by_phone(body: PhoneRequest) -> dict:
    pool = _pool()
    try:
        return await request_by_phone_service(pool, alias=body.alias, note=body.note)
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


class PeerRequest(BaseModel):
    peer_entity_id: int
    note: str | None = Field(None, max_length=500)


@router.post("/request-peer",
             dependencies=[Depends(requires_any_permission("request:peer", "write:address_book"))])
async def request_peer(body: PeerRequest) -> dict:
    """Connect with a host discovered through a shared machine (Circles: at the door; Network: Request connection).

    The gap this closes: peer discovery puts a host in the Peers list, and there
    was nothing to click. The only two senders were by phone alias and by a
    person's account email — and the email one refuses your own address, which is
    exactly what both sides are when one person's two hosts share a machine. So a
    discovered peer was visible and unreachable."""
    pool = _pool()
    try:
        return await request_peer_service(pool, peer_entity_id=body.peer_entity_id, note=body.note)
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)


# ── Contacts service (single source of truth for the routes + the chat tool) ──
#
# Each function below holds the EXACT route logic + every invariant (kind/status
# validation, the accept-request domain_policy allow + best-effort grant + slip,
# the phone identity-resolve → endpoint_request send). They read NOTHING from a
# Request. HTTP-ish failures raise ContactsError(status, detail); the routes map
# that to HTTPException, the chat tool maps it to an error envelope. The bridge
# helper (_bridge_op) still raises HTTPException for transport/bridge failures —
# that propagates unchanged through the service in both call paths.


class ContactsError(Exception):
    """HTTP-ish failure inside a contacts service function."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def list_contacts_service(pool, *, status: str | None = None,
                                q: str | None = None, limit: int = 200,
                                offset: int = 0) -> dict:
    clauses, params = [], []
    if status:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        clauses.append(f"(handle ILIKE ${len(params)} OR display_name ILIKE ${len(params)})")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await pool.fetch(
        f"""
        SELECT {_ROW_COLS}
          FROM contacts {where}
         ORDER BY COALESCE(display_name, handle), id
         LIMIT {int(limit)} OFFSET {max(0, int(offset or 0))}
        """,
        *params,
    )
    contacts = [_row_dict(r) for r in rows]
    # A Host blocked through a blocked person keeps its own status, because that
    # block is derived (see block_refuses). Name the people it comes from, so the
    # Host's row does not read as open while its messages are refused.
    if any(c["kind"] == "host" and c["peer_entity_id"] is not None for c in contacts):
        through = {r["peer_entity_id"]: list(r["names"]) for r in await pool.fetch(
            "SELECT peer_entity_id, array_agg(COALESCE(display_name, handle) ORDER BY id) AS names"
            "  FROM contacts WHERE kind = 'human' AND status = 'blocked'"
            "   AND peer_entity_id IS NOT NULL GROUP BY peer_entity_id")}
        for c in contacts:
            if c["kind"] == "host":
                c["blocked_through"] = through.get(c["peer_entity_id"], [])
    # A page past the first `limit`.
    off = max(0, int(offset or 0))
    return {"ok": True, "count": len(rows), "contacts": contacts,
            "offset": off, "next_offset": (off + len(rows)) if len(rows) == int(limit) else None}


async def find_human_contact(pool, email: str):
    """The human contact for `email` (handle = the person's account email), or
    None. The People send path resolves the person's HOST from it
    (peer_entity_id) — that is where the message is delivered and whose slip we
    present. Returns the raw row (peer_entity_id, display_name, status)."""
    addr = clean_email(email)
    if not addr:
        return None
    return await pool.fetchrow(
        f"SELECT {_ROW_COLS} FROM contacts WHERE kind = 'human' AND lower(handle) = $1 LIMIT 1",
        addr,
    )


# ── Block ─────────────────────────────────────────────────────────────
#
# WHAT BLOCK MEANS. A Host can be the assistant for every
# person at a company, so blocking the Host must not silence those people,
# while blocking a person must not leave them a way round through their Host:
#   * a blocked HOST: what the Host itself sends is refused; a message from a
#     person on it still arrives.
#   * a blocked PERSON: their messages are refused from any Host, and the Host
#     they are reached through is blocked as well.
#
# The Host block is DERIVED, never stored. A Host is blocked when its own
# contact is blocked, or when a blocked person contact is reached through it
# (contacts.peer_entity_id, v123). So unblocking the person lifts the Host block
# that came from them, a Host blocked directly stays blocked, and two blocked
# people on one Host keep it blocked until both are lifted, with no second
# record to fall out of step. Any writer of contacts.status gets this, so every
# client's Block does.
#
# The permission slip is left alone on purpose. A person's messages travel on
# their Host's slip (circles.py, the People send path), so revoking it to block
# the Host would silence the person too. Both blocks are enforced here, at this
# Host's own admission.

#: The words a blocked sender gets back: the answer a domain-policy deny already
#: gives (memory_handlers.handle_inbox_append), so a block says nothing to the
#: sender that a closed posture would not.
BLOCKED_ERROR = "blocked by domain policy"


def blocked_refusal() -> dict:
    return {"ok": False, "blocked": True, "error": BLOCKED_ERROR}


_BLOCK_SQL = """
SELECT
  EXISTS (SELECT 1 FROM contacts
           WHERE status = 'blocked' AND peer_entity_id = $1)            AS host_blocked,
  EXISTS (SELECT 1 FROM contacts
           WHERE status = 'blocked' AND kind = 'human'
             AND lower(handle) = $2)                                    AS person_blocked,
  EXISTS (SELECT 1 FROM contacts
           WHERE status <> 'blocked' AND kind = 'human'
             AND lower(handle) = $2 AND peer_entity_id = $1)            AS person_on_host
"""


async def block_refuses(pool, host_id: int, person: str | None = None, *,
                        host_vouches_for_person: bool = False) -> bool:
    """True when a block on this Host refuses traffic from Host `host_id`
    written by `person` (None: the Host itself wrote it).

    `host_vouches_for_person`: on the direct lane the only evidence for the
    person is the sending Host's own word (peer/handlers.inbox_deliver), and a
    blocked Host could name anyone. There the person counts only when OUR
    contacts place them on that Host; any other claim is the Host's own message.
    On the relay the platform has already checked that the person runs that
    Host before it carried the message (bridge adapter
    _enrich_and_verify_cross_host), so the person stands as given.

    A person whose Host is unknown (peer_entity_id NULL, as for a person added
    by hand) is refused by their address on every Host, and no Host is blocked
    with them until an exchange records the one they use.

    Raises when the read fails. Every caller refuses then: a check that could
    not run is not a reason to let a blocked sender in.
    """
    addr = clean_email(person)
    row = await pool.fetchrow(_BLOCK_SQL, int(host_id), addr)
    if row is None:
        return False
    if addr and row["person_blocked"]:
        return True
    if not row["host_blocked"]:
        return False
    if not addr:
        return True
    return host_vouches_for_person and not row["person_on_host"]


# The two sets block_refuses reads a row at a time, read whole: every Host id
# _BLOCK_SQL's host_blocked is true for, and every address its person_blocked
# is true for. Same tables, same predicates.
_BLOCKED_SETS_SQL = """
SELECT
  ARRAY(SELECT DISTINCT peer_entity_id FROM contacts
         WHERE status = 'blocked' AND peer_entity_id IS NOT NULL)   AS hosts,
  ARRAY(SELECT DISTINCT lower(handle) FROM contacts
         WHERE status = 'blocked' AND kind = 'human')               AS people
"""


async def blocked_sets(pool) -> tuple[set[int], set[str]]:
    """The block rule above, for a list rather than one message: (hosts,
    people), where

      * Host `h` is in `hosts` exactly when block_refuses(pool, h) is True:
        its own contact is blocked, or a blocked person is reached through it;
      * address `a` is in `people` exactly when the person is blocked, which is
        when block_refuses(pool, <any Host>, a) is True whatever Host carries it.

    One query, so a list of a few hundred contacts costs one read instead of
    one per row. Circles' deck uses it to leave a blocked person, and a Host
    blocked either way, off the cards (server/circles_deck.py).
    scripts/test_circles_deck_pg.py holds it to block_refuses for every Host
    and person it seeds.

    Raises when the read fails, as block_refuses does: a list that could not
    check a block must not show what the block would hide."""
    row = await pool.fetchrow(_BLOCKED_SETS_SQL)
    if row is None:
        return set(), set()
    return ({int(h) for h in (row["hosts"] or [])},
            {str(p) for p in (row["people"] or []) if p})


#: FIRST ENTITY WINS ON A HANDLE.
#:
#: `contacts` is UNIQUE on `handle` alone (ux_contacts_handle,
#: migrations/v048_circles_contacts_policy.sql:28) and both upsert paths below
#: used to write `peer_entity_id = EXCLUDED.peer_entity_id` into the ON CONFLICT
#: branch. So a second entity arriving under a handle this host already holds
#: RE-POINTED the existing contact at itself.
#:
#: That is reachable, and the sharper of the two paths is the grant one: for a
#: host grant the handle is read straight off the sender's payload
#: (`body_payload.get("from_handle")`, server/bridge/memory_handlers.py), and an
#: `endpoint_grant` is admitted by domain_policy._REQUEST_KINDS without any
#: relationship precondition on the bridge leg. A host that sends one naming
#: someone else's handle took over that contact: `peer_entity_id` is what the
#: People send path resolves a person's HOST from (find_human_contact's
#: docstring says so), so messages meant for the first host would have gone to
#: the second.
#:
#: The rule is the one this codebase already applies to a key that must not
#: change under a stable identity — roster.pin_key's first-key-wins, and
#: hub_peers.arm_hub_pdp treating a changed key as an alarm rather than an
#: update. A handle that already names a different entity is refused and
#: reported; it is never overwritten.
#:
#: ENFORCED IN THE SQL AS WELL AS HERE, and the SQL is the part that matters:
#: the predicate below is a check-then-write and two concurrent grants would
#: both pass it, so every ON CONFLICT DO UPDATE that touches peer_entity_id
#: carries `WHERE contacts.peer_entity_id IS NULL OR contacts.peer_entity_id =
#: EXCLUDED.peer_entity_id` and the database is the arbiter — the same shape
#: roster.store_vote uses for its seq.
_HANDLE_CLAIM_GUARD = (
    " WHERE contacts.peer_entity_id IS NULL "
    "    OR contacts.peer_entity_id = EXCLUDED.peer_entity_id"
)


async def handle_claimed_by(pool, handle: str, peer_entity_id) -> int | None:
    """The entity that already holds `handle`, when it is NOT `peer_entity_id`.

    None means the handle is free, or already this entity's — either way the
    upsert may proceed. Returns the OTHER entity's id so the caller can say who
    it is rather than refusing anonymously.

    Fails CLOSED is not the right posture here and it is deliberately not taken:
    a lookup that raises returns None and the SQL guard still refuses the
    re-point. Refusing on a transient read error would break first contact for
    a handle nobody holds.
    """
    if not handle or peer_entity_id is None:
        return None
    try:
        held = await pool.fetchval(
            "SELECT peer_entity_id FROM contacts WHERE handle = $1", handle)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("handle-claim lookup failed (%s) — the SQL guard still holds", exc)
        return None
    if held is None:
        return None
    return None if int(held) == int(peer_entity_id) else int(held)


async def upsert_contact_from_grant(pool, *, handle: str, kind: str,
                                    display_name: str | None, peer_entity_id: int) -> None:
    """The endpoint_grant receiver's half of a connection: the OTHER side accepted
    us, so record them as an active contact with the host they are reached
    through. Never downgrades: a blocked contact stays blocked, a name the user
    typed is kept over the one the grant carried."""
    if kind not in ("host", "human"):
        kind = "host"
    handle = (handle or "").strip()
    if kind == "human":
        handle = clean_email(handle) or ""
    if not handle:
        return
    # FIRST ENTITY WINS. This is the path where the handle is the SENDER'S
    # WORD — for a host grant it is read straight off body_payload.from_handle
    # (server/bridge/memory_handlers.py) — so it is the one a host could use to
    # take over a contact it does not own. Refused and reported, never
    # overwritten. Best-effort by contract (the caller's own comment says this
    # never fails a delivery), so this returns rather than raising.
    claimed = await handle_claimed_by(pool, handle, peer_entity_id)
    if claimed is not None:
        logger.warning(
            "contact grant from entity %s claims handle %r, which already names "
            "entity %s — REFUSED, not re-pointed", peer_entity_id, handle, claimed)
        return
    await pool.execute(
        """
        INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id, last_contact_at)
        VALUES ($1, $2, $3, 'active', $4, now())
        ON CONFLICT (handle) DO UPDATE
           SET display_name   = COALESCE(contacts.display_name, EXCLUDED.display_name),
               kind           = EXCLUDED.kind,
               status         = CASE WHEN contacts.status = 'blocked' THEN 'blocked' ELSE 'active' END,
               peer_entity_id = EXCLUDED.peer_entity_id,
               last_contact_at = now()
        """ + _HANDLE_CLAIM_GUARD,
        handle, display_name, kind, int(peer_entity_id),
    )
    if kind == "host":
        # The real handle is recorded now; drop the placeholder a request left
        # ("host <id>", request_by_phone_service / request_peer_service) so the
        # People and Contacts lists do not carry a stale pending twin.
        await pool.execute(
            "DELETE FROM contacts WHERE kind = 'host' AND status = 'pending' "
            "AND peer_entity_id = $1 AND handle <> $2 AND handle = 'host ' || $1::text",
            int(peer_entity_id), handle)


async def create_contact_service(pool, *, handle: str, display_name: str | None = None,
                                 kind: str = "host", notes: str | None = None,
                                 status: str = "active") -> dict:
    if kind not in ("host", "human"):
        raise ContactsError(400, "kind must be host or human")
    if status not in ("active", "pending", "blocked"):
        raise ContactsError(400, "status must be active, pending, or blocked")
    handle = (handle or "").strip()
    if not handle:
        raise ContactsError(400, "handle required")
    if kind == "human":
        # A person is addressed by their account email — the identity Circles
        # follows across hosts. Normalise so the People thread key matches.
        addr = clean_email(handle)
        if not addr:
            raise ContactsError(400, "a human contact's handle is their email address")
        handle = addr
    row = await pool.fetchrow(
        f"""
        INSERT INTO contacts (handle, display_name, kind, notes, status)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (handle) DO UPDATE
           SET display_name = COALESCE(EXCLUDED.display_name, contacts.display_name),
               notes        = COALESCE(EXCLUDED.notes, contacts.notes),
               status       = EXCLUDED.status
        RETURNING {_ROW_COLS}
        """,
        handle, display_name, kind, notes, status,
    )
    return {"ok": True, "contact": _row_dict(row)}


async def update_contact_service(pool, *, contact_id: int, display_name: str | None = None,
                                 notes: str | None = None, status: str | None = None,
                                 _provided: set | None = None) -> dict:
    """Update a contact. `_provided` names the fields the caller explicitly set;
    when None we fall back to \"non-None means set\" — the exact shape the PATCH
    route had (it iterated non-None attrs, so a field can be changed but not
    nulled).

    status='blocked' is the whole of Block: admission reads it (block_refuses),
    so nothing else has to be written here and no client can do half of it."""
    if status is not None and status not in ("active", "pending", "blocked"):
        raise ContactsError(400, "status must be active, pending, or blocked")
    sets, params = [], []
    fields = {"display_name": display_name, "notes": notes, "status": status}
    for col in ("display_name", "notes", "status"):
        is_set = (col in _provided) if _provided is not None else (fields[col] is not None)
        if is_set:
            params.append(fields[col])
            sets.append(f"{col} = ${len(params)}")
    if not sets:
        raise ContactsError(400, "nothing to update")
    params.append(contact_id)
    row = await pool.fetchrow(
        f"""
        UPDATE contacts SET {", ".join(sets)}
         WHERE id = ${len(params)}
        RETURNING {_ROW_COLS}
        """,
        *params,
    )
    if not row:
        raise ContactsError(404, "contact not found")
    return {"ok": True, "contact": _row_dict(row)}


async def delete_contact_service(pool, *, contact_id: int) -> dict:
    deleted = await pool.fetchval(
        "DELETE FROM contacts WHERE id = $1 RETURNING id", contact_id,
    )
    if not deleted:
        raise ContactsError(404, "contact not found")
    return {"ok": True, "deleted": contact_id}


class _GrantRefused(Exception):
    """The requester's host answered "no" to our grant. Internal to the accept
    path: it unwinds to "grant_delivered False" without a bridge retry, and is
    never raised past this module — accepting is still a success locally, the
    contact is recorded, and the UI surfaces that the reply did not land."""


async def accept_request_service(pool, *, handle: str, message_id=None,
                                 display_name: str | None = None,
                                 from_entity_id=None, kind: str = "host",
                                 from_user_email: str | None = None,
                                 my_email: str | None = None) -> dict:
    """Accept an inbound endpoint_request: record the sender as a contact, write
    the per-peer domain_policy allow (so 'contacts only' admits them), and best-
    effort deliver the endpoint_grant reply (with a nested contact_slip). When
    the requester id is unknown or delivery fails the contact stays 'pending' and
    grant_delivered is false — the UI surfaces that state.

    A request from a PERSON (kind='human', from_user_email stamped by the bridge)
    becomes a human contact keyed by their email, reached through the host the
    request came from (peer_entity_id). The grant we send back carries OUR
    person's email (my_email) and kind, so the requester records us the same way."""
    handle = (handle or "").strip()
    if kind not in ("host", "human"):
        kind = "host"
    if kind == "human":
        # The person's email comes from the request row's VERIFIED column
        # (set only from the bridge-checked value), never from the caller's
        # body or the message payload — a host could otherwise put any name
        # in its request and be accepted as that person. No verified person
        # on the row → it is a host's request.
        verified = None
        if message_id:
            try:
                verified = await pool.fetchval(
                    "SELECT from_user_email FROM inbox_messages "
                    "WHERE message_id = $1::uuid AND message_kind = 'endpoint_request'",
                    str(message_id))
            except Exception as exc:
                logger.warning("accept: verified-sender lookup failed: %s", exc)
        verified = clean_email(verified)
        if not verified:
            raise ContactsError(400, "this request did not come from a verified person — "
                                     "accept it as a host, or decline it")
        handle = verified
    if not handle:
        raise ContactsError(400, "handle required")

    # FIRST ENTITY WINS, checked before the write so the refusal can name the
    # entity that holds the handle. The SQL guard below is the real enforcement;
    # this is what turns it from "the row silently did not update" into a
    # sentence the user can act on.
    claimed = await handle_claimed_by(pool, handle, from_entity_id)
    if claimed is not None:
        raise ContactsError(
            409,
            f"the handle {handle!r} already names Host #{claimed} — accepting this "
            f"would re-point that contact at Host #{from_entity_id}. A handle belongs "
            f"to the first host that claimed it; rename or remove the existing contact "
            f"first if this is a genuine change")

    row = await pool.fetchrow(
        f"""
        -- last_contact_at is set here as well as on the outbound path
        -- (request_peer_service). It is not decoration: the peer lane's grant
        -- precondition (server/peer/handlers.py _relationship_open_with) admits
        -- a slipless grant only while a pending row is RECENT, and an ON CONFLICT
        -- re-pending never touches created_at — so without this, re-accepting an
        -- old contact would produce a fresh decision wearing a stale clock and
        -- the grant would be refused.
        INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id, last_contact_at)
        VALUES ($1, $2, $3, 'pending', $4, now())
        ON CONFLICT (handle) DO UPDATE
           SET status = 'pending',
               kind = EXCLUDED.kind,
               peer_entity_id = COALESCE(EXCLUDED.peer_entity_id, contacts.peer_entity_id),
               last_contact_at = now()
        {_HANDLE_CLAIM_GUARD}
        RETURNING {_ROW_COLS}
        """,
        handle, display_name, kind,
        int(from_entity_id) if from_entity_id is not None else None,
    )
    if row is None:
        # The guard in the ON CONFLICT branch refused the update, so nothing was
        # returned. Only reachable when another caller claimed the handle between
        # the check above and this write — which is exactly why the guard is in
        # the SQL and not only in Python. Same sentence, so a race and a plain
        # conflict read identically.
        raced = await handle_claimed_by(pool, handle, from_entity_id)
        raise ContactsError(
            409,
            f"the handle {handle!r} already names Host #{raced} — accepting this "
            f"would re-point that contact at Host #{from_entity_id}. A handle belongs "
            f"to the first host that claimed it; rename or remove the existing contact "
            f"first if this is a genuine change")

    # Accepting means this peer may reach us — write the per-peer allow
    # rule so the 'contacts only' posture actually admits them. The policy
    # matches the SENDING HOST's address (domain_policy.check_inbound_allowed
    # compares from_address / its local part), so for a person the rule is
    # written for the host they are reached through, not their email.
    policy_peer = handle
    if kind == "human" and from_entity_id is not None:
        policy_peer = f"{int(from_entity_id)}@entity.localhost"
    await pool.execute(
        """
        INSERT INTO domain_policy (peer, allow_inbox, allow_dm, allow_circle_invite, note)
        VALUES ($1, TRUE, TRUE, TRUE, 'accepted connection request')
        ON CONFLICT (peer) DO UPDATE
           SET allow_inbox = TRUE, allow_dm = TRUE, allow_circle_invite = TRUE,
               updated_at = now()
        """,
        policy_peer,
    )

    # Deliver the endpoint_grant reply to the requester so they can record
    # us as a contact too. Cross-entity inbox delivery is now wired (the
    # bridge relays + stamps our verified identity). Best-effort: if the
    # requester's id is unknown or delivery fails, the contact is still
    # recorded locally and the UI shows grant_delivered=false.
    grant_delivered = False
    if from_entity_id is not None:
        # Issue a permission slip granting the new contact the right to message us,
        # and deliver it INSIDE the grant payload (as data — `contact_slip`), so the
        # requester can store + present it on future DMs. It rides nested, NOT as the
        # top-level `slip` of this send: top-level `slip` means \"authorize THIS send\",
        # and a slip we issued names US as issuer, so the bridge would refuse it for
        # our own outbound grant. The slip is the cryptographic, revocable contact
        # proof on top of the domain_policy allow we just wrote.
        contact_slip = None
        try:
            issued = await _bridge_op("contract.issue", {
                "grantee_entity_id": int(from_entity_id),
                "resource": "dm:contact",
                "allowed_ops": ["message"],
                "reasoning": "accepted contact may send direct messages",
                # The relationship TERM (90d default) — the slip itself is short-lived
                # (1h) and renewed on use under the same contract_id until the term ends
                # or we revoke. Revoke cuts it off; the contact sets the term.
                "term_seconds": 90 * 86400,
            })
            contact_slip = (issued or {}).get("slip")
        except Exception as e:
            logger.warning("contact slip issue failed (non-fatal): %s", e)
        try:
            grant_payload: dict = {"contact_slip": contact_slip} if contact_slip else {}
            # Tell the requester WHO accepted, in the same shape their request
            # carried: a person's grant names the person (the bridge verifies
            # my_email is an operator of this host before it relays it).
            if kind == "human":
                grant_payload["kind"] = "human"
            else:
                # Our owner's name and our number, to a host we just ACCEPTED
                # (name and number are shared on accept).
                try:
                    from server.hosts.self_card import shareable_card
                    mine = await shareable_card()
                    if mine:
                        grant_payload["card"] = mine
                except Exception as e:
                    logger.info("own card unavailable for the grant (%s)", type(e).__name__)
            send_params: dict = {
                "to_entity_id": int(from_entity_id),
                "message_kind": "endpoint_grant",
                "subject": "Connection accepted",
                "body": "Your connection request was accepted.",
                "body_format": "text/plain",
                "payload": grant_payload,
            }
            if kind == "human" and clean_email(my_email):
                send_params["from_user_email"] = clean_email(my_email)
            # DIRECT LANE FIRST for a HOST grant, bridge as fallback — the same
            # order the request itself uses (request_peer_service) and Circles
            # uses for a DM. This is the half that was still missing: the knock
            # became deliverable, and the ANSWER to it did not, so first contact
            # landed, the user accepted, and the requester was never told. The
            # bridge holds its connections per DaemonSet pod with no forwarding
            # between them (two Hosts on different bridge pods got
            # "entity N not connected").
            #
            # HUMAN GRANTS STAY ON THE BRIDGE. For kind='human' the bridge
            # verifies from_user_email is an operator of this host before it
            # relays; the peer lane proves which ENTITY is calling and cannot
            # attest which PERSON. Routing a human grant here would assert
            # something nobody checked.
            sent_direct = False
            if kind != "human":
                try:
                    from server.peer.peer_client import grant_peer, PeerUnavailable
                except Exception as e:
                    grant_peer = None
                    logger.info("peer lane module unavailable (%s) — bridge only", type(e).__name__)

                    class PeerUnavailable(Exception):
                        pass
                try:
                    if grant_peer is None:
                        raise PeerUnavailable("peer lane module not present")
                    resp = await grant_peer(int(from_entity_id), send_params)
                    if resp.get("ok"):
                        sent_direct = True
                    elif resp.get("receiver"):
                        # The far side ANSWERED no — most often "no request of
                        # ours is pending with you", which the bridge would hand
                        # to the same gate for the same answer. Not a fallback,
                        # so do not retry; the caller sees grant_delivered=False
                        # and the reason is logged.
                        logger.info("peer grant to %s refused by the receiver: %s",
                                    from_entity_id, resp.get("error"))
                        sent_direct = None  # answered, and the answer was no
                    else:
                        logger.info("peer grant to %s refused at the lane (%s) — bridge fallback",
                                    from_entity_id, resp.get("error"))
                except PeerUnavailable as e:
                    logger.info("peer lane unavailable for %s (%s) — bridge fallback", from_entity_id, e)
                except Exception as e:
                    logger.info("peer lane unusable (%s) — bridge fallback", type(e).__name__)
            if sent_direct is None:
                # Answered no. Leave grant_delivered False and stop — retrying
                # the bridge asks the same gate the same question.
                raise _GrantRefused()
            if not sent_direct:
                await _bridge_op("inbox.send", send_params)
            grant_delivered = True
            # Now that they're connected, mark the contact active.
            await pool.execute(
                "UPDATE contacts SET status='active' WHERE id=$1", row["id"])
        except _GrantRefused:
            # Already logged with the receiver's own words; not a delivery
            # failure, an authoritative refusal.
            pass
        except Exception as e:
            logger.warning("endpoint_grant delivery failed (non-fatal): %s", e)

    # Archive the request message so it leaves the inbox queue.
    if message_id:
        try:
            from server.bridge.memory_handlers import handle_inbox_archive
            await handle_inbox_archive({"message_id": str(message_id)})
        except Exception:
            pass
    elif from_entity_id is not None:
        # No card id (the chat tool treats it as optional): accepting still
        # ANSWERS that host's connection requests, so archive its open ones.
        # A live card holds the peer's knock slot (peer/handlers._knock_slot_free)
        # for 7 days, and a host we just accepted would otherwise be told
        # "already waiting" on its next request.
        try:
            await pool.execute(
                "UPDATE inbox_messages SET archived_at = now() "
                " WHERE from_entity_id = $1 AND message_kind = 'endpoint_request' "
                # The HOST's own requests only: a card from a PERSON on that host
                # is a different question, and archiving it would drop a request
                # the user never saw.
                "   AND from_user_email IS NULL AND archived_at IS NULL", int(from_entity_id))
        except Exception as exc:
            logger.warning("archiving %s's answered request cards failed (non-fatal): %s",
                           from_entity_id, exc)

    return {"ok": True, "contact_id": row["id"], "grant_delivered": grant_delivered}


async def deny_request_service(pool, *, handle: str | None = None,
                               message_id=None) -> dict:
    """Deny an inbound endpoint_request: do NOT create a contact or write any
    domain_policy allow (the default-deny posture already refuses them). Just
    archive the request so it leaves the inbox queue. No reply is sent — a
    denial is silent by design. Idempotent: returns {ok, denied, archived}."""
    archived = False
    if message_id:
        try:
            from server.bridge.memory_handlers import handle_inbox_archive
            await handle_inbox_archive({"message_id": str(message_id)})
            archived = True
        except Exception as e:
            logger.warning("deny archive failed (non-fatal): %s", e)
    return {"ok": True, "denied": True, "archived": archived}


class _KnockRefused(Exception):
    """The FAR SIDE answered no. Distinct from every other failure here, and the
    distinction decides whether a retry is a fallback or the same question twice."""


async def _knock_first(target_entity_id: int, req: dict, *, what: str) -> dict | None:
    """Try the DIRECT peer lane for a slipless knock; None means fall back to the
    relay. Raises _KnockRefused when the receiver itself said no.

    ONE implementation for every first-contact path. The relay holds its
    connections in an in-process dict per DaemonSet pod with no forwarding between
    them, so a bridge-only knock reached a peer only when both pods happened to
    share a node — two Hosts on different bridge pods got "entity N not
    connected" (503), with zero held slips estate-wide because the
    request that issues one could never arrive.

    The peer lane is reached by service DNS (entity-<id>.entity-<id>.svc…), stable
    across pod-IP churn, and its mTLS proves WHO on the far side. Falling back
    rather than failing keeps this working against a peer whose pod predates
    inbox.request: PeerUnavailable is transport-only by contract, and an
    authoritative refusal comes back as {ok: False} instead.
    """
    # The import gets its OWN try. Inside the one below, a failed import would
    # make evaluating `except PeerUnavailable` raise UnboundLocalError — which
    # propagates past `except Exception` too, so the "older image" tolerance this
    # was written for would have produced a 500 and no fallback at all. Measured.
    try:
        from server.peer.peer_client import knock_peer, PeerUnavailable
    except Exception as e:
        logger.info("peer lane module unavailable (%s) — bridge only for %s",
                    type(e).__name__, what)
        return None
    try:
        resp = await knock_peer(int(target_entity_id), req)
    except PeerUnavailable as e:
        logger.info("peer lane unavailable for %s (%s) — bridge fallback", target_entity_id, e)
        return None
    except Exception as e:
        logger.info("peer lane unusable (%s) — bridge fallback", type(e).__name__)
        return None
    if resp.get("ok"):
        return resp.get("data") or {}
    if resp.get("receiver"):
        # Raised OUTSIDE any try the caller wraps this in, so nothing can swallow
        # an authoritative refusal and turn it into a silent bridge retry — which
        # is exactly what happened once, and the route gate caught it. The bridge
        # would hand the same message to the same pod and the same gate.
        raise _KnockRefused(resp.get("error") or "the peer refused the request")
    # A lane-level refusal — an older pod that does not know inbox.request, a
    # malformed frame, no usable cert CN. The receiver stored nothing, so the
    # bridge is a real second chance.
    logger.info("%s to %s refused at the lane (%s) — bridge fallback",
                what, target_entity_id, resp.get("error"))
    return None


async def request_by_phone_service(pool, *, alias: str, note: str | None = None) -> dict:
    """Resolve a phone number to a host's IDENTITY and send it a connection
    request. We learn who the number belongs to (display name) but not how
    to reach them — sending the request is the consented first contact; if
    they accept, each side then shares its routing handle. The request binds
    to the resolved crypto identity (entity_uuid), never the alias string."""
    resolved = await _bridge_op("phone.alias.lookup", {"alias": alias})
    if not resolved.get("found"):
        raise ContactsError(404, "no host is using that number")
    target_id = resolved["entity_id"]
    target_name = resolved.get("display_name") or "a host"

    # endpoint_request is the existing connection-request kind the receiver's
    # inbox + accept flow already understand. The bridge stamps OUR verified
    # identity + routing handle so they can connect back on accept.
    note = (note or "").strip()
    req = {
        "to_entity_id": int(target_id),
        "message_kind": "endpoint_request",
        "subject": "Connection request",
        "body": note or "Would like to connect.",
        "body_format": "text/plain",
        "payload": {
            "target_entity_uuid": resolved.get("entity_uuid"),
            "via": "phone_alias",
        },
    }
    # THE DIRECT LANE FIRST, same order and same shape as
    # request_peer_service — a knock is slipless by design, so knock_peer is the
    # call, and the relay stays the fallback. Without this a first contact by phone
    # number only landed when both pods happened to share a node: the relay keeps
    # its connections in an in-process dict per DaemonSet pod, measured at 5 of 28
    # possible host pairs.
    #
    # Resolution stays PLATFORM-SIDE and that is correct: phone.alias.lookup already
    # returns the entity id to this pod, so knocking directly reveals nothing the
    # pod was not already told. (This is exactly what request_by_email cannot do —
    # see its own note.)
    try:
        data = await _knock_first(int(target_id), req, what="phone-alias request")
    except _KnockRefused as e:
        # The far side ANSWERED. 409 for the same reason request_peer_service uses
        # it: the commonest answer is "one is already waiting", which is a conflict,
        # and it is the peer's HOST answering, not the person.
        raise ContactsError(409, f"{target_name}'s host did not accept the request: {e}")
    if data is None:
        # None means FALL BACK — peer unreachable, an image without the lane, or a
        # lane-level refusal where the receiver stored nothing. Leaving this out was
        # a regression that turned an older-image peer into a 500 and sent the
        # request nowhere, on the very path this change exists to make reliable.
        data = await _bridge_op("inbox.send", req)
    # RECORD THAT WE ASKED, as request_peer_service does. A grant is admitted
    # only from a host we are waiting on (the peer lane's inbox.grant and, since
    # the contact card, the relay too — memory_handlers._grant_admissible), and
    # without this row a phone request's own answer was refused ("no connection
    # request is open with you"). "host <id>" is the placeholder handle; the
    # grant replaces it with the real one (upsert_contact_from_grant).
    await pool.execute(
        """
        INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id)
        VALUES ($1, $2, 'host', 'pending', $3)
        ON CONFLICT (handle) DO UPDATE
           SET status = CASE WHEN contacts.status = 'blocked' THEN 'blocked'
                             WHEN contacts.status = 'active'  THEN 'active'
                             ELSE 'pending' END,
               peer_entity_id = COALESCE(contacts.peer_entity_id, EXCLUDED.peer_entity_id),
               last_contact_at = now()
         WHERE contacts.kind = 'host'
        """,
        f"host {int(target_id)}", resolved.get("display_name"), int(target_id),
    )
    return {"ok": True, "sent_to": target_name, "message_id": data.get("message_id")}


async def request_peer_service(pool, *, peer_entity_id: int, note: str | None = None) -> dict:
    """Send a connection request to a host we share a MACHINE with.

    THE PEER LIST IS THE AUTHORIZATION, and that is the whole security of this.
    A bare entity id from the caller must never be enough to send an
    endpoint_request, or this becomes a way to knock on any host on the platform
    by guessing numbers. So the id is checked against the peerings this host's
    own bastions reported: a machine we are paired to told us it also serves that
    host, which is precisely the consent peer discovery carries (pairing IS the
    consent for the announcement).

    A DORMANT peering does not qualify. The machine stopped saying it serves
    them, so the shared-machine fact that justified the request is no longer
    true; list_peerings returns `current` for exactly this. Nor does a peering
    the machine reports as pending or revoked, or names with no state or an
    unknown one: only an active pairing is a Host the machine serves.
    list_peerings' `requestable` is both conditions, and requestable_peering
    is the lookup.

    The announcement still authorizes nothing beyond this knock. The receiver
    treats the endpoint_request the way it treats one arriving by phone alias,
    and a cross-host MESSAGE still needs the slip their accept issues."""
    try:
        eid = int(peer_entity_id)
    except (TypeError, ValueError):
        raise ContactsError(400, "peer_entity_id must be a number")

    from server import brain_service
    from server.network import peer_discovery
    me = brain_service._entity_id
    if eid == me:
        raise ContactsError(400, "that is this host")

    match = peer_discovery.requestable_peering(
        await peer_discovery.list_peerings(pool, me), eid)
    if match is None:
        raise ContactsError(
            404, "that host is not on a machine you share — a connection request may only be "
                 "sent to a host one of your machines currently reports serving")

    peer_handle = match.get("peer_handle") or f"host {eid}"
    machine = match.get("introduced_by") or ""
    # Capped HERE, not only on the Pydantic model: the host tool calls this
    # service directly (contacts_manage op='request_peer') and never passes
    # through PeerRequest, so a route-only cap is no cap at all for that caller.
    note = (note or "").strip()[:500]
    body = note or (f"We share the machine {machine}. Would like to connect."
                    if machine else "We share a machine. Would like to connect.")
    # `via` is provenance for the receiver's card, so it can eventually say WHY a
    # request arrived. It is SENDER-ASSERTED and nothing reads it yet: a receiver
    # must never treat it as proof that a machine is shared, only as the sender's
    # claim about how they found you. The proof a receiver holds is its own peer
    # list, which it can check independently.
    # ONE message id, carried down BOTH transports. peer_client raises
    # PeerUnavailable on a read timeout, which can fire after the receiver has
    # already committed — the bridge retry would then be a second card with no way
    # to dedupe it. handle_inbox_append conflicts on message_id, so minting it here
    # makes the retry idempotent instead of duplicating.
    import uuid as _uuid
    req = {
        "to_entity_id": eid,
        "message_kind": "endpoint_request",
        "subject": "Connection request",
        "body": body,
        "body_format": "text/plain",
        "message_id": str(_uuid.uuid4()),
        "payload": {"via": "bastion_peer", "machine": machine or None},
    }
    # DIRECT LANE FIRST, bridge as fallback — the same order Circles uses for a DM
    # (circles._try_direct_peer_send). This is the half that was missing: the
    # request went bridge-only, and the bridge holds its connections per DaemonSet
    # pod with no forwarding between them, so a peer on a different bridge pod
    # answered "entity N not connected" (503) and first contact was impossible.
    # Two Hosts on different bridge pods could not connect; zero held slips
    # estate-wide, because the request that issues one could never arrive.
    #
    # The peer lane is reached by service DNS (entity-<id>.entity-<id>.svc…), which
    # is stable across pod-IP churn, and its mTLS proves WHO on the far side.
    # Falling back rather than failing keeps this working against a peer whose pod
    # predates inbox.request (PeerUnavailable is transport-only by contract,
    # peer_client.py:35-37; an authoritative refusal comes back as {ok: False}).
    try:
        data = await _knock_first(eid, req, what="peer request")
    except _KnockRefused as e:
        # 409, not 502: the far side ANSWERED, and the commonest answer is "one is
        # already waiting" — a conflict, not a bad gateway. And it is the peer's
        # host that answered, so do not word every one of these as a refusal by
        # the person: a cold pod saying "pool not ready" is not the person saying no.
        raise ContactsError(409, f"{peer_handle}'s host did not accept the request: {e}")
    if data is None:
        data = await _bridge_op("inbox.send", req)
    # Record who we are waiting on, so the Peers row can say "request sent"
    # instead of offering to send another.
    #
    # THE kind IS NEVER OVERWRITTEN, and that is a security property, not
    # politeness. ux_contacts_handle is unique on handle ALONE, and this handle
    # comes from a peering whose handle the BASTION refreshes on every beat while
    # its entity id is resolved only once (peer_discovery). A machine announcing
    # a stable host_name can therefore swap the handle underneath a peering that
    # is already resolved and current. Before the charset narrowing in
    # topology._narrow_handle it could swap in an email address — and this
    # statement would then have flipped the user's HUMAN contact of that name to
    # kind='host', removing them from Circles › People (circles.py filters on
    # kind='human') with nothing to show what happened.
    #
    # Two independent fixes, because either alone leaves the class open: a handle
    # can no longer BE an address, and this can no longer change what a contact
    # is. The `WHERE contacts.kind = 'host'` makes a collision with a human
    # contact a no-op rather than a rewrite.
    #
    # peer_entity_id (v123_contacts_peer_entity.sql:17, nullable INTEGER) is
    # recorded so the Peers row can find this row without matching on a handle
    # the machine controls.
    await pool.execute(
        """
        INSERT INTO contacts (handle, kind, status, peer_entity_id)
        VALUES ($1, 'host', 'pending', $2)
        ON CONFLICT (handle) DO UPDATE
           SET status = CASE WHEN contacts.status = 'blocked' THEN 'blocked'
                             WHEN contacts.status = 'active'  THEN 'active'
                             ELSE 'pending' END,
               peer_entity_id = COALESCE(contacts.peer_entity_id, EXCLUDED.peer_entity_id)
         WHERE contacts.kind = 'host'
        """,
        peer_handle, eid,
    )
    return {"ok": True, "sent_to": peer_handle, "machine": machine or None,
            "message_id": data.get("message_id")}


async def request_by_email_service(pool, *, email: str, note: str | None = None,
                                   my_email: str | None = None) -> dict:
    """Send a PERSON a connection request by their account email (Circles ›
    People). The bridge resolves the email to the host that person
    runs and delivers an endpoint_request there stamped with OUR person's
    verified email; if no such account exists the bridge drops it silently and
    the answer here is the same — the address book never becomes a way to test
    whether someone has an account. Records a pending human contact locally so
    the People list shows who we are waiting on."""
    addr = clean_email(email)
    if not addr:
        raise ContactsError(400, "that is not an email address")
    me = clean_email(my_email)
    if not me:
        raise ContactsError(403, "no verified operator identity on this request")
    if addr == me:
        raise ContactsError(400, "that is your own address")
    note = (note or "").strip()
    await _bridge_op("contacts.request_by_email", {
        "to_user_email": addr,
        "from_user_email": me,
        "body": note or "Would like to connect.",
    })
    await pool.execute(
        """
        INSERT INTO contacts (handle, kind, status)
        VALUES ($1, 'human', 'pending')
        ON CONFLICT (handle) DO UPDATE
           SET kind = 'human',
               status = CASE WHEN contacts.status = 'blocked' THEN 'blocked' ELSE contacts.status END
        """,
        addr,
    )
    return {"ok": True, "sent_to": addr}


# ── NEW route: deny an inbound connection request ─────────────────────
# (accept-request's sibling; thin adapter over deny_request_service)
@router.post("/deny-request", dependencies=[Depends(requires_permission("write:address_book"))])
async def deny_request(body: dict) -> dict:
    """Deny an inbound endpoint_request: archive it, create no contact and no
    allow rule (default-deny already refuses them). Body: {message_id, handle?}."""
    pool = _pool()
    try:
        return await deny_request_service(
            pool, handle=body.get("handle"), message_id=body.get("message_id"),
        )
    except ContactsError as e:
        raise HTTPException(e.status, e.detail)

