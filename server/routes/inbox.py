"""Inbox — pod-local inbox_messages surface for the Circles app.

The inbox table is dual-written from the platform's memory service (PME) via bridge
handlers (see server.bridge.memory_handlers.handle_inbox_*). This
router exposes those handlers over HTTP so the in-pod Circles app
can read its own queue without round-tripping the bridge.

Read paths reuse the bridge handler verbatim — the response shape
matches what the app already expects (count + messages list).
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pydantic import BaseModel, Field

from server.applications.manifest import RESERVED_APP_IDS
from server.applications.middleware import requires_permission
from server.peer import app_lane
from server.bridge.memory_handlers import (
    handle_inbox_list,
    handle_inbox_read,
    handle_inbox_archive,
    handle_inbox_unread_count,
    handle_inbox_append,
    # The handler's OWN entity resolver, imported rather than re-derived: a
    # delivery must be written under the same id the admission gate judges it
    # by. See the note at its call site in deliver_message.
    _entity_id,
)


class InboxMessageCreate(BaseModel):
    subject: str
    body: str
    # MIME-style, matching the inbox_messages_body_format_check constraint
    # (text/markdown | text/plain | text/html). The old default "text"
    # violated the CHECK — every POST /send 502'd at the insert.
    body_format: str = "text/markdown"
    # Must be in the inbox_messages_message_kind_check vocabulary
    # (message | permission_* | endpoint_* | software_* | contract_* |
    # system_notification | decision | decision_review). The old default
    # "note" and the documented note|update|grant|contract|order set were
    # never valid — same fate as body_format above.
    message_kind: str = "message"
    from_address: str | None = None    # defaults to entity self
    to_address: str | None = None      # defaults to entity self
    thread_id: str | None = None       # ties messages into a thread

logger = logging.getLogger("privatae.inbox")

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


def _unwrap(envelope: dict) -> dict:
    """Convert a LOCAL handler {'ok': bool, 'error': str} envelope into an HTTP
    response, or raise with a status code matching the error SEMANTICS. These
    handlers (server.bridge.memory_handlers) run in-pod, so a client-input
    error is a 4xx — not a 502 (which implies an upstream gateway failure and
    is what a malformed message_id used to return)."""
    if not envelope.get("ok", False):
        err = envelope.get("error") or "unknown error"
        e = err.lower()
        # A handler that says the pod is not ready yet (no pool, no host id)
        # marks the envelope `retryable` — matched on the flag, not the words,
        # so rewording a message cannot turn a 503 into a 502 again.
        if envelope.get("retryable"):
            code = 503
        elif any(s in e for s in ("required", "invalid", "must be", "malformed")):
            code = 400
        elif "not found" in e or "no such" in e:
            code = 404
        elif "pool" in e or "not ready" in e or "host id" in e:
            code = 503
        else:
            code = 502
        raise HTTPException(status_code=code, detail=err)
    return envelope


@router.get("/list", dependencies=[Depends(requires_permission("read:inbox"))])
async def list_messages(
    unread: int = Query(0),
    kind: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    include_archived: int = Query(0),
    offset: int = Query(0, ge=0),
) -> dict:
    """Inbox list. Always returns the {ok, count, messages} list shape so
    the app can render rows. `unread=1` filters to unread messages and
    surfaces the true unread total under `unread` (for the rail badge);
    `kind=<message_kind>` filters to a single stored kind.

    The underlying `handle_inbox_list` handler returns all active messages
    (newest first, capped at `limit`); the unread/kind filters are applied
    here so we don't have to change the shared bridge handler."""
    payload = {
        "limit": limit,
        "offset": offset,          # page past the first `limit` (RT-B21)
        "include_archived": bool(include_archived),
    }
    result = _unwrap(await handle_inbox_list(payload))
    messages = result.get("messages") or []

    if unread:
        messages = [m for m in messages if not m.get("read_at")]
    if kind:
        messages = [m for m in messages if m.get("kind") == kind]

    result["messages"] = messages
    result["count"] = len(messages)

    if unread:
        # The list is capped by `limit`, so a filtered length undercounts.
        # Surface the real unread total for the rail badge.
        unread_env = _unwrap(await handle_inbox_unread_count({}))
        result["unread"] = unread_env.get("unread", len(messages))

    return result


@router.get("/unread-count", dependencies=[Depends(requires_permission("read:inbox"))])
async def unread_count() -> dict:
    return _unwrap(await handle_inbox_unread_count({}))


@router.get("/read/{message_id}", dependencies=[Depends(requires_permission("read:inbox"))])
async def read_message(message_id: str) -> dict:
    return _unwrap(await handle_inbox_read({"message_id": message_id}))


@router.post("/archive/{message_id}", dependencies=[Depends(requires_permission("write:inbox"))])
async def archive_message(message_id: str) -> dict:
    return _unwrap(await handle_inbox_archive({"message_id": message_id}))


@router.post("/send", dependencies=[Depends(requires_permission("write:inbox"))])
async def send_message(body: InboxMessageCreate) -> dict:
    """User-composed inbox message. Thin HTTP wrapper over the bridge
    `handle_inbox_append` handler."""
    return _unwrap(await handle_inbox_append({
        k: v for k, v in body.model_dump().items() if v is not None
    }))


#: Namespace for the deterministic delivery UUID. A fixed, arbitrary v4 that
#: exists only so uuid5 has a stable root — never a secret, never rotated
#: (rotating it would make every past delivery redeliverable).
_DELIVERY_NS = uuid.UUID("6f6d2b5e-0e3a-4a1d-9c7f-2b4d8e1a5c93")


class InboxDeliverRequest(BaseModel):
    subject: str = Field(min_length=1)
    body: str = ""
    #: Constrained here rather than left to the database: the column's CHECK
    #: would reject anything else, and a constraint violation surfaces through
    #: _unwrap as a 502 — an upstream-gateway error for what is plainly a bad
    #: request. Pinning the literals returns a 422 that names the field.
    body_format: Literal["text/markdown", "text/plain", "text/html"] = "text/markdown"
    #: The caller's OWN stable name for this item. Same key = same row, forever.
    #: A mail-routing app derives one per (rule, account, message); anything stable
    #: works. Scoped to the calling app, so two apps cannot collide.
    idempotency_key: str = Field(min_length=1, max_length=200)
    #: Structured context for the host to act on — the matched rule, the
    #: addressing headers, whatever the app wants to hand over. Reserved keys
    #: are overwritten server-side.
    payload: dict | None = None
    thread_id: str | None = None


@router.post("/deliver", dependencies=[Depends(requires_permission("write:inbox_delivery"))])
async def deliver_message(body: InboxDeliverRequest, request: Request) -> dict:
    """An installed app delivers one item into the host's inbox, exactly once.

    This is the contract an app needs to hand the host something it selected on
    the user's behalf — a routing rule's match, a watcher's hit — without being
    able to lie about where it came from or to double-deliver on a retry.

    WHICH APP is never a parameter: it is read off the window token the
    middleware bound (same rule as the peer app lane, peer_apps.py:53-80), so
    app A cannot deliver as app B. The host's own `host` token is refused —
    the host already has inbox_send and does not need to launder a delivery
    through an app identity.

    EXACTLY ONCE is a property of the row, not of the caller's care: the
    message_id is uuid5(app_id + idempotency_key), so a retry collides with
    itself on the primary key and inserts nothing. `duplicate: true` says that
    happened, which is what lets an app keep an honest activity log instead of
    guessing whether its retry landed.
    """
    tok = getattr(request.state, "app_token", None)
    app_id = getattr(tok, "app_id", None)
    if not app_lane.valid_app_id(app_id):
        raise HTTPException(401, "application token required")
    if app_id in RESERVED_APP_IDS:
        raise HTTPException(403, "delivery is for installed apps; use /api/inbox/send")

    # NESTED, never merged. The app's content goes under `app` and nothing it
    # sends can set a top-level payload key.
    #
    # Merging it was a privilege escalation, found in review. `payload` is not
    # inert storage — handle_inbox_append reads keys back out of it and turns
    # them into rows (`contact_slip` -> held_slips + a contacts row with
    # status='active'; `from_display_name` -> the dashboard broadcast). An app
    # holding only write:inbox_delivery could therefore plant a contact under a
    # handle and display name of its choosing, which is exactly what the scope's
    # own rationale promises is impossible. The harvest is now gated on kind at
    # the handler too; this is the second line, and the one that holds if
    # another reserved key is added later.
    payload = {
        # Stamped, never accepted: these name the deliverer and the dedupe key,
        # and an app supplying its own would claim provenance it cannot prove.
        "delivered_by_app": app_id,
        "idempotency_key": body.idempotency_key,
        "app": dict(body.payload or {}),
    }

    message_id = uuid.uuid5(_DELIVERY_NS, f"{app_id}\x00{body.idempotency_key}")

    # The SAME resolver the handler uses. It reads brain_service._entity_id
    # (memory_handlers.py:93), which main.py sets from the relay config when one
    # exists — so reading ENTITY_ID here could disagree with the id the row is
    # written under. If those diverge, `cross_host` goes true, the self-send
    # always-allow (domain_policy.py:198) is missed, and a closed '*' policy
    # silently drops the user's own app's deliveries: the precise failure the
    # explicit from_entity_id below exists to prevent.
    entity_id = _entity_id()
    if not entity_id:
        raise HTTPException(503, "host not ready")

    result = _unwrap(await handle_inbox_append({
        "subject": body.subject,
        "body": body.body,
        "body_format": body.body_format,
        "message_kind": "app_delivery",
        "message_id": str(message_id),
        "payload": payload,
        # BOTH of these are load-bearing; neither can be left to the handler's
        # defaults.
        #
        # from_address: the default is "cseries@localhost", the PLATFORM sender.
        # A delivery inheriting it would render as a platform notification —
        # precisely the impersonation this route's narrow scope exists to make
        # impossible. It names the app instead.
        #
        # from_entity_id: admission re-reads the RAW payload
        # (memory_handlers.py, `from_eid = payload.get("from_entity_id")`), not
        # the value the handler resolves for the row. Omitting it means
        # check_inbound_allowed sees None, misses the self-send always-allow
        # (domain_policy.py:198), and falls through to the peer rules — where a
        # user running a closed '*' default would have their own app's
        # deliveries silently dropped. Saying it explicitly makes the self-send
        # true at the gate as well as in the row.
        "from_address": f"app:{app_id}@host",
        "from_entity_id": entity_id,
        **({"thread_id": body.thread_id} if body.thread_id else {}),
    }))
    logger.info("inbox delivery app=%s key=%s duplicate=%s",
                app_id, body.idempotency_key, result.get("duplicate"))
    return result
