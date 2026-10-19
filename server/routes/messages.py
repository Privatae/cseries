"""Host-to-host direct messages — real conversation transport.

A DM is a back-and-forth between two hosts. Each host keeps BOTH its sent
and received messages locally in inbox_messages, sharing a DETERMINISTIC
thread id derived from the two entity ids — so the same conversation groups
identically on both sides without any negotiation. Delivery rides the
cross-host bridge relay (op_inbox_send), PRESENTING the permission slip the
peer issued us.

What gates it: for message_kind='message' the slip is the sole authority — the
receiver refuses a cross-host message that arrives without a live one, and a
standing domain_policy allow does not substitute. domain_policy still gates the
bootstrap kinds (connection requests and grants), which cannot carry a slip yet
because the relationship does not exist. An earlier version of this docstring
said domain policy gates DMs; it does not, and the send path was written to
match the docstring rather than the receiver.

Routes:
  POST /api/messages/send            send a message to a host you can reach
  GET  /api/messages/conversations   your DM threads (peer + last + unread)
  GET  /api/messages/thread/{id}     the full back-and-forth in one thread
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

from server import brain_service
from server.applications.middleware import requires_permission

router = APIRouter(prefix="/api/messages", tags=["messages"])

# Fixed namespace so dm_thread_id is stable across hosts + restarts.
_DM_NS = uuid.UUID("6f1d3b2a-7c44-5e9a-b3d1-7a2c9e5f0a11")


def dm_thread_id(a: int, b: int) -> str:
    """The one thread two hosts share. Symmetric: pair(a,b) == pair(b,a)."""
    lo, hi = sorted((int(a), int(b)))
    return str(uuid.uuid5(_DM_NS, f"dm:{lo}:{hi}"))


_PEOPLE_NS = uuid.UUID("2b7f0d4e-9c31-5a6e-8f02-4d1e6c9a7b55")


def people_thread_id(a: str, b: str) -> str:
    """The one thread two people share. Symmetric: pair(a,b) == pair(b,a).

    Lives HERE, beside dm_thread_id, because both are pure thread-id derivation
    over a sorted pair and both are needed by the peer-lane delivery handler.
    It used to live in circles.py, which imports server.main at module scope —
    so reaching it from server/peer put a FastAPI app mount on the delivery
    path and, from anything importing circles first, a genuine circular import.
    circles re-exports it, so every existing caller is unchanged.
    """
    lo, hi = sorted((a.strip().lower(), b.strip().lower()))
    return str(uuid.uuid5(_PEOPLE_NS, f"people:{lo}:{hi}"))


def pair_thread_id(me: int, peer: int, from_user_email: str | None,
                   to_user_email: str | None) -> str:
    """The thread a message between this Host (`me`) and `peer` belongs to,
    from who is actually on it: person↔person when BOTH people are named
    (the People readers select on both), otherwise the Host pair. The ONE
    rule the receiver applies on both ways in (peer/handlers._thread_id_for
    on the direct lane, memory_handlers.handle_inbox_append on the relay),
    and the one each sender already follows, so an honest sender's thread id
    and the derived one are the same."""
    if from_user_email and to_user_email:
        return people_thread_id(from_user_email, to_user_email)
    return dm_thread_id(me, peer)


def _self() -> int:
    sid = brain_service._entity_id or 0
    if not sid:
        raise HTTPException(503, "host id unknown")
    return int(sid)


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool down")
    return pool


async def _bridge(op: str, params: dict) -> dict:
    from server.main import get_bridge_client
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(503, "bridge not connected")
    env = await bc.request_full(op, params)
    if not env.get("ok"):
        raise HTTPException(int(env.get("code") or 502), env.get("error") or f"{op} failed")
    return env.get("data") or {}


class SendBody(BaseModel):
    to_entity_id: int | None = None
    to_handle: str | None = None          # resolve to entity_id if id absent
    to_display_name: str | None = None    # peer's name, for the thread label
    body: str = Field(min_length=1)   # bounded by the receiver: CROSS_HOST_BODY_MAX
    topic_id: str | None = None           # "original" or a topic's id; omitted: the peer's latest


@router.post("/send", dependencies=[Depends(requires_permission("write:inbox"))])
async def send_message(body: SendBody) -> dict:
    """Thin adapter — the DM send logic (peer resolve + our-copy insert + bridge
    deliver) lives in send_message_service so the host_message tool shares it
    without faking an HTTP Request. Response shape unchanged."""
    me = _self()
    pool = _pool()
    try:
        return await send_message_service(
            pool, me,
            to_entity_id=body.to_entity_id, to_handle=body.to_handle,
            to_display_name=body.to_display_name, body=body.body,
            topic_id=body.topic_id)
    except MessageError as e:
        raise HTTPException(e.status, e.detail)


class OpenBody(BaseModel):
    to_entity_id: int | None = None
    to_handle: str | None = None
    to_display_name: str | None = None


@router.post("/open", dependencies=[Depends(requires_permission("read:inbox"))])
async def open_conversation(body: OpenBody) -> dict:
    """Resolve a contact to the DM thread to open (may have no messages yet).

    Thin adapter — resolution lives in open_conversation_service so the
    host_message tool shares it. Response shape unchanged."""
    me = _self()
    pool = _pool()
    try:
        return await open_conversation_service(
            pool, me,
            to_entity_id=body.to_entity_id, to_handle=body.to_handle,
            to_display_name=body.to_display_name)
    except MessageError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/conversations", dependencies=[Depends(requires_permission("read:inbox"))])
async def conversations() -> dict:
    """Thin adapter — the thread-list query lives in conversations_service so the
    host_message tool shares it. Response shape unchanged."""
    me = _self()
    pool = _pool()
    try:
        return await conversations_service(pool, me)
    except MessageError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/thread/{thread_id}", dependencies=[Depends(requires_permission("read:inbox"))])
async def thread(thread_id: str, limit: int | None = None, before: str | None = None,
                 topic_id: str | None = None) -> dict:
    """Thin adapter — the thread read + mark-read lives in thread_service so the
    host_message tool shares it. One page, newest first; `before` (a
    next_before this route returned) reads the page before it; `topic_id`
    reads one topic of a conversation with another Host."""
    me = _self()
    pool = _pool()
    try:
        return await thread_service(pool, me, thread_id, limit=limit, before=before,
                                    topic_id=topic_id)
    except MessageError as e:
        raise HTTPException(e.status, e.detail)


# ── DM SERVICE LAYER ──────────────────────────────────────────────────────
# The host-to-host DM logic, extracted from the routes so the host_message chat
# tool shares the EXACT path the /api/messages/* routes use — no _ShimRequest, no
# route handler called in-process. Each function holds the whole behavior (peer
# resolution, our-copy insert, bridge deliver, thread read+mark-read) and reads
# NOTHING from a Request. HTTP-ish failures raise MessageError(status, detail),
# which the thin routes map to HTTPException; everything else returns normally.
# These helpers live ABOVE the routes (no existing DM service module) and reuse
# the module-level dm_thread_id() so the thread id stays identical to the routes.


# The message is the person's own words, so the SENDER has no length policy of
# its own (the old Field(max_length=20000) answered a 422 nobody could
# read). The bound is the RECEIVER's: a Host takes
# at most memory_handlers.CROSS_HOST_BODY_MAX in one message from another host
# (the receiver protects itself; it is cross-tenant input). The
# send checks the same bound first, with the receiver's own words, so the
# person learns it before anything is written or travels — never cut.
def _dm_max_bytes() -> int:
    from server.bridge.memory_handlers import CROSS_HOST_BODY_MAX
    return CROSS_HOST_BODY_MAX


class MessageError(Exception):
    """A DM failure the caller surfaces. `status` is the HTTP code the /api/messages
    routes map it to; the host_message tool just reports `detail`. Same shape as
    nac_mgmt.MgmtError so both callers handle one error type."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def _bridge_svc(op: str, params: dict) -> dict:
    """Service-layer bridge call — identical to the route's _bridge but raises
    MessageError instead of HTTPException so the service stays Request/HTTP-free."""
    from server.main import get_bridge_client
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise MessageError(503, "bridge not connected")
    env = await bc.request_full(op, params)
    if not env.get("ok"):
        raise MessageError(int(env.get("code") or 502), env.get("error") or f"{op} failed")
    return env.get("data") or {}


async def _resolve_target_service(to_entity_id, to_handle, *, not_found_status: int) -> int:
    """Resolve a DM recipient to an entity id: explicit id wins, else look up the
    handle over the bridge. Raises MessageError(not_found_status) if neither resolves."""
    target = to_entity_id
    if target is None and to_handle:
        r = await _bridge_svc("synaptive.handle.lookup", {"handle": str(to_handle).strip()})
        target = r.get("entity_id")
    if not target:
        if not_found_status == 404:
            raise MessageError(404, "couldn't resolve that contact to a host")
        raise MessageError(400, "to_entity_id or a resolvable to_handle required")
    return int(target)


async def send_message_service(pool, entity_id: int, *, to_entity_id=None, to_handle=None,
                               to_display_name=None, body: str, topic_id=None) -> dict:
    """Send a DM to a host you can reach — the SINGLE source of truth for both
    POST /api/messages/send and the host_message tool (op='send'). Resolves the peer,
    keeps our own copy in the shared thread (so we see what we sent, marked read),
    then delivers over the bridge relay (their domain policy decides if it lands).
    Returns {ok, message_id, thread_id, delivered, error, topic}. Raises MessageError
    for missing/unresolvable recipient or self-send.

    `topic_id`: the topic of the
    conversation with that Host the message goes into, "original" or a topic's
    id, checked against the pair's own topics (_send_topic). Omitted, it is the
    topic of the newest message from that Host, so a reply lands where the
    message it answers is. It travels as the nested payload on both lanes and
    is stored on our own copy (topics.wire); the original conversation sends
    none, so an older Host reads every message exactly as before."""
    me = int(entity_id)
    from server.bridge.memory_handlers import cross_host_body_refusal
    too_big = cross_host_body_refusal(body or "")
    if too_big:
        raise MessageError(413, too_big)
    target = await _resolve_target_service(to_entity_id, to_handle, not_found_status=400)
    if target == me:
        raise MessageError(400, "use the brain chat to talk to yourself")
    from server.peer import topics as _topics
    topic, topic_title = await _send_topic(pool, me, target, topic_id)
    topic_wire = _topics.wire(topic, topic_title)

    thread_id = dm_thread_id(me, target)
    peer_name = (to_display_name or "").strip() or None

    # 1) Keep our own copy in the thread (so we see what we sent). Marked
    #    read — it's ours. peer_display_name = the recipient. The topic goes
    #    on it as it goes on the wire, so this Host files the message under
    #    the same topic the peer does.
    sent_payload = {"peer_display_name": peer_name, "direction": "sent", **topic_wire}
    msg_id = await pool.fetchval(
        """
        INSERT INTO inbox_messages
          (message_id, thread_id, from_address, to_address,
           from_entity_id, to_entity_id, subject, body, body_format,
           message_kind, payload, sender_verified, delivered_at, read_at)
        VALUES (gen_random_uuid(), $1::uuid, $2, $3, $4, $5, 'Message',
                $6, 'text/plain', 'message', $7::jsonb, TRUE, NOW(), NOW())
        RETURNING message_id
        """,
        thread_id, f"{me}@entity.localhost", f"{target}@entity.localhost",
        me, target, body, json.dumps(sent_payload),
    )

    # 2) Deliver to the peer over the bridge relay, PRESENTING the permission
    #    slip they issued us. thread_id is shared so it threads on both sides.
    #
    #    The slip is not optional for a host↔host message. The receiver treats it
    #    as the SOLE AUTHORITY for message_kind='message' and refuses anything
    #    without one — a standing domain_policy allow is explicitly not sufficient
    #    (server/bridge/memory_handlers.py, handle_inbox_append). This path used to
    #    relay with no slip at all, so every host↔host DM sent through
    #    /api/messages/send — or the host_message tool, which shares this service —
    #    was refused by the peer. Measured: 6/6 refused with a live slip
    #    sitting in held_slips the whole time, against 30/30 delivered on the
    #    Circles path, which does present it.
    #
    #    The bridge already supports this on inbox.send: the shared cross-host
    #    primitive reads params['slip'], verifies it (signature, grantee==sender,
    #    issuer==recipient, liveness, revocation) and attests authorized_via_slip
    #    into the delivered payload; with no slip it leaves the receiver to gate.
    #    So this was a sender-side omission, not a missing capability.
    #
    #    _held_slip_for stays the ONE implementation and stays in circles.py: it
    #    carries renew-on-use (rotate at half-life under the same contract_id), and
    #    a second copy would drift — a slip renewed on one send path but not the
    #    other surfaces as an intermittent refusal. Imported lazily, matching how
    #    this tree already crosses module lines, so import order is unaffected.
    #    (Where it LIVES is worth revisiting; that it is single is the property.)
    #
    #    Holding no slip is a legitimate state, not an error: bootstrap kinds and a
    #    peer we have no relationship with have none, and those still flow under
    #    domain_policy. Present what we hold; let the receiver decide.
    delivered = True
    deliver_error = None
    # ONE id for the RECIPIENT's row, carried down both transports so a delivery
    # that commits on the peer and then times out on the way back cannot land twice
    # (the direct lane raises on the readline, i.e. after the receiver has written;
    # handle_inbox_append dedupes on a supplied id and only on a supplied id).
    #
    # NOT msg_id. That is the id of OUR OWN copy, in our own pod's inbox — a
    # different message. Reusing it would let a future reader that dedupes across
    # both directions treat the pair as one and lose the sent side.
    deliver_id = str(uuid.uuid4())
    params = {
        "to_entity_id": target,
        "thread_id": thread_id,
        "message_id": deliver_id,
        "message_kind": "message",
        "subject": "Message",
        "body": body,
        "body_format": "text/plain",
    }
    if topic_wire:
        # The relay hands the nested payload to the receiver whole, less the
        # keys the bridge stamps (adapter.py op_inbox_send, _INBOX_SENDER_FIELDS
        # and _enrich_and_verify_cross_host), and handle_inbox_append checks it.
        params["payload"] = dict(topic_wire)
    try:
        from server.routes.circles import _held_slip_for
        held = await _held_slip_for(target)
        if held:
            params["slip"] = held
    except Exception as e:
        # Never fail a send on a slip-lookup problem: relay without it and let the
        # receiver refuse, which is exactly the behaviour that preceded this fix.
        log.warning("held slip lookup failed for peer %s (relaying without): %s", target, e)
    #    AND THE DIRECT LANE FIRST. The relay keeps its connections in an
    #    in-process dict per DaemonSet pod with no forwarding between them, so it
    #    only ever delivered within a node: measured, 8 hosts across 4
    #    nodes = 5 of 28 possible pairs, and one Host alone on its node could reach
    #    none of the other seven. The direct lane dials the peer's own service name
    #    and has no such affinity.
    #
    #    Only with a slip in hand: `message` is slip-only on the receiving side, so
    #    a slipless send has nothing to present and goes straight to the relay,
    #    where domain_policy still gates it. Same order as the Circles composer,
    #    through the same primitive.
    #
    #    No person fields: this service is the host_message TOOL's send path, so the
    #    author is the host itself, not an operator at a keyboard. A human-stamped
    #    send arrives through the Circles composer, which passes its verified
    #    X-Authentik-Email. Nothing here has one to assert.
    direct = None
    if params.get("slip"):
        try:
            from server.peer.deliver import deliver_message
            direct, direct_reason, _ = await deliver_message(
                me, target, params["slip"],
                subject=params["subject"], body=body,
                thread_id=thread_id, message_id=deliver_id,
                topic=topic_wire or None)
            if direct is None:
                log.info("direct lane did not carry %s -> %s (%s) — relaying",
                         me, target, direct_reason)
        except Exception as e:
            # Never fail a send on the lane itself being unusable; the relay is
            # the fallback and it is the behaviour that preceded this.
            log.warning("direct lane unusable for %s (%s) — relaying", target, type(e).__name__)
    if direct is None:
        try:
            await _bridge_svc("inbox.send", params)
        except MessageError as e:
            delivered = False
            deliver_error = e.detail if isinstance(e.detail, str) else "delivery failed"

    return {
        "ok": True, "message_id": str(msg_id), "thread_id": thread_id,
        "delivered": delivered, "error": deliver_error,
        "topic": {"id": topic, "title": topic_title},
    }


async def _send_topic(pool, me: int, peer: int, topic_id) -> tuple[str, str | None]:
    """(topic id, title) a host_message send to `peer` goes into
    (server/peer/topics.py):

      omitted or blank   the topic of the newest message FROM `peer` in this
                         pair's conversation, else the original conversation
      "original"         the original conversation
      a topic's id       a topic this pair's conversation has; the title is the
                         topic's own (topics.title_of), never the caller's

    A topic is only ever looked up over this pair's rows, so an id another
    pair uses is not one of this conversation's: MessageError(404). Anything
    that is not an id is MessageError(400)."""
    from server.peer import topics as _topics
    if topic_id is None or (isinstance(topic_id, str) and not topic_id.strip()):
        tid = await pool.fetchval(
            f"SELECT {_topics.topic_sql()} FROM inbox_messages" + _topics.PAIR_ROWS
            + " AND from_entity_id = $2"
            "  ORDER BY COALESCE(sent_at, delivered_at) DESC, message_id DESC LIMIT 1",
            int(me), int(peer)) or _topics.ORIGINAL
    else:
        tid = _topics.parse(topic_id) if isinstance(topic_id, str) else None
        if tid is None:
            raise MessageError(400, "topic_id is \"original\" or a topic's id")
    if tid == _topics.ORIGINAL:
        return _topics.ORIGINAL, _topics.ORIGINAL_TITLE
    exists, title = await _topics.title_of(pool, int(me), int(peer), tid)
    if not exists:
        raise MessageError(404, "there is no such topic in your conversation with this Host")
    return tid, title


async def open_conversation_service(pool, entity_id: int, *, to_entity_id=None,
                                    to_handle=None, to_display_name=None) -> dict:
    """Resolve a contact to the DM thread to open (may have no messages yet) — the
    SINGLE source of truth for POST /api/messages/open and the host_message tool
    (op='open'). Returns {thread_id, peer_id, peer_name}. Raises MessageError(404)
    if the contact can't be resolved to a host."""
    me = int(entity_id)
    target = await _resolve_target_service(to_entity_id, to_handle, not_found_status=404)
    return {
        "thread_id": dm_thread_id(me, target),
        "peer_id": target,
        "peer_name": to_display_name or f"Host {target}",
    }


async def conversations_service(pool, entity_id: int) -> dict:
    """List the host's DM threads (peer + last message + unread) — the SINGLE source
    of truth for GET /api/messages/conversations and the host_message tool
    (op='conversations'). Read-only. Returns {conversations: [...]}.

    A person's chat is listed only while the reading switch is on for it
    (server/circles_reading.py visible_sql): then its entry
    says kind 'person', names the person and the operator whose chat it is, and
    peer_id stays the Host the person is reached through. Every other entry is
    kind 'host'. A person's chat counts no unread here: whether its operator has
    read it is theirs to mark (circles_page_marks), not this Host's read_at."""
    me = int(entity_id)
    from server.circles_reading import visible_sql
    rows = await pool.fetch(
        f"""
        SELECT DISTINCT ON (thread_id)
               thread_id,
               CASE WHEN from_entity_id = $1 THEN to_entity_id ELSE from_entity_id END AS peer_id,
               CASE WHEN from_entity_id = $1
                    THEN payload->>'peer_display_name'
                    ELSE COALESCE(payload->>'from_display_name', payload->>'peer_display_name')
               END AS peer_name,
               (from_user_email IS NOT NULL AND to_user_email IS NOT NULL) AS people,
               CASE WHEN from_entity_id = $1 THEN to_user_email ELSE from_user_email END AS person,
               CASE WHEN from_entity_id = $1 THEN from_user_email ELSE to_user_email END AS operator,
               CASE WHEN from_entity_id = $1
                    THEN payload->>'peer_user_name'
                    ELSE COALESCE(payload->>'from_user_name', payload->>'peer_user_name')
               END AS person_name,
               body AS last_body, sent_at,
               (from_entity_id = $1) AS last_from_me,
               (SELECT COUNT(*) FROM inbox_messages u
                 WHERE u.thread_id = m.thread_id
                   AND u.to_entity_id = $1 AND u.read_at IS NULL
                   AND NOT (u.from_user_email IS NOT NULL AND u.to_user_email IS NOT NULL)) AS unread
          FROM inbox_messages m
         WHERE message_kind = 'message'
           AND (from_entity_id = $1 OR to_entity_id = $1)
           -- person ↔ person traffic is the People list, never the host's
           -- (this service backs the host_message tool), except one chat whose
           -- operator turned the reading switch on (circles_reading.visible_sql)
           AND {visible_sql("m", "$1")}
         ORDER BY thread_id, sent_at DESC
        """,
        me,
    )
    convos = []
    for r in rows:
        c = {
            "thread_id": str(r["thread_id"]),
            "kind": "host",
            "peer_id": r["peer_id"],
            "peer_name": r["peer_name"] or (f"Host {r['peer_id']}" if r["peer_id"] else "Unknown"),
            "last_body": (r["last_body"] or "")[:140],
            "last_from_me": r["last_from_me"],
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            "unread": int(r["unread"] or 0),
        }
        if r["people"]:
            c.update(kind="person", person=r["person"], operator=r["operator"],
                     peer_name=r["person_name"] or r["person"])
        convos.append(c)
    convos.sort(key=lambda c: c["sent_at"] or "", reverse=True)
    return {"conversations": convos}


# ── Thread paging ──────────────────────────────────────────────────────────
# A thread between two Hosts grows without bound; it is read a PAGE at a time,
# newest first, with a cursor that names one exact row: (sent_at, message_id).
# Two messages can share a timestamp, so a timestamp alone would skip or repeat
# rows at a page edge; message_id (the uuid primary key) breaks every tie.
# The cursor travels as "<iso timestamp>|<message_id>".
THREAD_PAGE = 100


def thread_cursor(ts, message_id) -> str:
    return f"{ts.isoformat()}|{message_id}"


def parse_thread_cursor(before):
    """(datetime, uuid) from a cursor, or None for no cursor. Raises
    MessageError(400) for one that is not ours."""
    if before in (None, ""):
        return None
    from datetime import datetime
    try:
        ts_s, mid_s = str(before).rsplit("|", 1)
        return datetime.fromisoformat(ts_s), uuid.UUID(mid_s)
    except (ValueError, TypeError):
        raise MessageError(400, "before must be a cursor this thread returned (next_before)")


# One page is bounded twice: by a row count,
# and by the bytes of its bodies, so a huge `limit` never loads the whole
# thread and a page is never cut by the transcript wall after it was read
# (which would leave next_before pointing past rows nobody saw). Neither is a
# ceiling on the THREAD — every row is reachable through next_before.
THREAD_PAGE_MAX = 500


def page_limit(limit, default: int = THREAD_PAGE) -> int:
    """A page size: the caller's, or the default, at most THREAD_PAGE_MAX."""
    try:
        n = int(limit) if limit not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, THREAD_PAGE_MAX))


def thread_page_budget() -> int:
    """The body bytes one page may carry: half of what one tool result carries
    for this call (tool_result_wall.read_page_cap) — JSON escapes non-ASCII
    text up to 2x for the scripts a message is written in — and never under
    16 KiB, so a single long message still comes back on its own page."""
    from mastercode.runtime.tool_result_wall import read_page_cap
    return max(16 * 1024, read_page_cap("bytes") // 2)


def sized_page_sql(cols: str, from_where: str, cur: int, lim: int, budget: int) -> str:
    """The newest-first page of `from_where` older than the cursor ($cur
    timestamptz, $cur+1 uuid), at most $lim rows and $budget body bytes — the
    first row always, so one message larger than the budget still advances.
    Each row carries `fetched`, how many rows the count bound allowed, so the
    caller can tell a page cut by bytes from the thread's start."""
    return f"""
        WITH page AS (
            SELECT {cols}
              {from_where}
               AND (${cur}::timestamptz IS NULL
                    OR (sent_at, message_id) < (${cur}::timestamptz, ${cur + 1}::uuid))
             ORDER BY sent_at DESC, message_id DESC
             LIMIT ${lim}::int
        ), sized AS (
            SELECT page.*,
                   row_number() OVER w AS rn,
                   sum(COALESCE(octet_length(body), 0)) OVER w AS running,
                   count(*) OVER () AS fetched
              FROM page
            WINDOW w AS (ORDER BY sent_at DESC, message_id DESC)
        )
        SELECT * FROM sized WHERE rn = 1 OR running <= ${budget}::bigint
         ORDER BY sent_at DESC, message_id DESC"""


def take_page(rows, n: int):
    """(rows newest-first, cut to n, and whether an older page exists): more
    when the count bound found an (n+1)th row, or the byte budget stopped the
    page before the rows the count bound allowed."""
    rows = list(rows)
    fetched = int(rows[0]["fetched"]) if rows else 0
    more = len(rows) > n or fetched > len(rows)
    return rows[:n], more


async def thread_service(pool, entity_id: int, thread_id: str, *,
                         limit=None, before=None, topic_id=None) -> dict:
    """One PAGE of the back-and-forth in one thread, newest page first, rows
    oldest-first inside it, + mark received messages read — the SINGLE source
    of truth for GET /api/messages/thread/{id} and the host_message tool
    (op='thread'). Returns {thread_id, messages, total, limit, next_before}:
    `total` is every message in the thread, and next_before is the cursor for
    the page before this one (None when this page reaches the start).

    Each message of a conversation with another Host names its topic
    (topic_id, topic_title; server/peer/topics.py), and `topic_id` reads one
    topic of it ("original" or a topic's id). A person's chat is read only
    while its reading switch is on (circles_reading.visible_sql); its messages
    name the people on them (from_person, to_person) and carry no topic, since
    a person is one continuous chat."""
    me = int(entity_id)
    try:
        uuid.UUID(str(thread_id))
    except (ValueError, TypeError):
        raise MessageError(400, "thread_id must be a uuid")
    from server.circles_reading import visible_sql
    from server.peer import topics as _topics
    topic = None
    if topic_id is not None and not (isinstance(topic_id, str) and not topic_id.strip()):
        topic = _topics.parse(topic_id) if isinstance(topic_id, str) else None
        if topic is None:
            raise MessageError(400, "topic_id is \"original\" or a topic's id")
    n = page_limit(limit)
    cur = parse_thread_cursor(before)
    where = f"""
         WHERE m.thread_id = $1::uuid AND m.message_kind = 'message'
           AND (m.from_entity_id = $2 OR m.to_entity_id = $2)
           AND {visible_sql("m", "$2")}"""
    args: list = [thread_id, me]
    if topic is not None:
        where += f" AND {_topics.topic_sql('m')} = $3"
        args.append(topic)
    k = len(args)
    total = await pool.fetchval(
        "SELECT count(*) FROM inbox_messages m" + where, *args)
    rows = await pool.fetch(
        sized_page_sql(
            f"""message_id, from_entity_id, to_entity_id, body, sent_at, read_at,
               from_user_email, to_user_email,
               payload->>'from_display_name' AS from_name,
               payload->>'peer_display_name' AS peer_name,
               payload->>'from_user_name' AS from_user_name,
               {_topics.topic_sql('m')} AS topic""",
            "FROM inbox_messages m" + where, cur=k + 1, lim=k + 3, budget=k + 4),
        *args, cur[0] if cur else None, cur[1] if cur else None, n + 1,
        thread_page_budget(),
    )
    rows, more = take_page(rows, n)
    rows.reverse()
    # Mark received messages in this thread read (never a person's rows: an
    # operator's own chat is marked by that operator, circles_page_marks).
    await pool.execute(
        "UPDATE inbox_messages SET read_at = NOW() "
        "WHERE thread_id = $1::uuid AND to_entity_id = $2 AND read_at IS NULL "
        "AND NOT (from_user_email IS NOT NULL AND to_user_email IS NOT NULL)",
        thread_id, me,
    )
    # Each topic's title, read over the pair it lives in (topics.list_topics:
    # the title on its earliest message that has one), once per peer on the page.
    titles: dict[int, dict] = {}
    messages = []
    for r in rows:
        m = {
            "message_id": str(r["message_id"]),
            "from_me": r["from_entity_id"] == me,
            "from_name": r["from_name"],
            "body": r["body"] or "",
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        }
        if r["from_user_email"] and r["to_user_email"]:
            m.update(from_name=r["from_user_name"] or r["from_user_email"],
                     from_person=r["from_user_email"], to_person=r["to_user_email"])
        else:
            peer = r["to_entity_id"] if r["from_entity_id"] == me else r["from_entity_id"]
            if peer is not None and int(peer) not in titles:
                titles[int(peer)] = {t["id"]: t["title"]
                                     for t in await _topics.list_topics(pool, me, int(peer))}
            m.update(topic_id=r["topic"],
                     topic_title=(titles.get(int(peer)) or {}).get(r["topic"])
                     if peer is not None else None)
        messages.append(m)
    out = {"thread_id": thread_id, "messages": messages, "total": int(total or 0),
           "limit": n,
           "next_before": (thread_cursor(rows[0]["sent_at"], rows[0]["message_id"])
                           if more and rows else None)}
    if topic is not None:
        out["topic_id"] = topic
    return out
