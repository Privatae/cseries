"""
Circles routes — facade for the Circles app.

This module is the cseries-side endpoint surface that the Circles app calls.
Under the hood, calls fan out to:

  - Local PG: synaptive identity (who am I), domain_policy (who can call me)
  - Bridge → the platform database's circles_ceigas: address book, threads, messages, slips
  - Bridge → entity-to-entity mTLS (when the protocol lands): direct DM
    delivery between two cseries containers over the IPv6 DMZ.

v0.1 scope (this image bump):
  - GET  /api/circles/conversations
  - GET  /api/circles/thread/{kind}/{id}
  - POST /api/circles/messages

Empty/empty-list returns are intentional when the bridge or backing store
isn't reachable — the UI degrades to "no conversations yet" rather than
erroring out. Real wiring lands in the next pass alongside the
container-to-container mTLS protocol.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from server import brain_service, main as server_main, secret_cut

from server.applications.middleware import requires_permission

# Circles threads. The
# rows, the descriptor and the two user acts live in the brain module; this
# router is their door. Aliased short because every self-thread path below
# touches it.
from mastercode import circles_threads as _ct


log = logging.getLogger("privatae.pc.circles")

router = APIRouter(prefix="/api/circles", tags=["circles"])

# SCOPE GATING. Every route below carries read:circles or
# write:circles. Until now this router was the largest ungated surface in the
# kernel: 14 routes, zero `requires_permission`, while system-apps/circles/
# app.yaml declared read:circles + write:circles and nothing ever checked them.
# CEIGAS did not cover it either — "/api/circles" is absent from
# ROUTE_DOMAIN_MAP (server/middleware/ceigas_gate.py:25), so the crossing check
# finds no route_match and calls straight through.
#
# What that exposed: an app with provenance `custom` is neither `community` nor
# in SDK_MANAGED_APPS, so `isolated` is false (applications/proxy.py) and it is
# framed on the SHELL origin. A bare fetch('/api/circles/conversations') from
# there returned the user's entire chat history, and POST /messages sent as
# them — with the app having declared no circles scope at all.
#
# Safe to gate because the token is already there: app-runner.js installs a
# contentWindow fetch shim on every same-origin app, stamping
# X-Application-Token on its requests, and circles-web reaches the API
# exclusively through `fetch` (no EventSource anywhere in circles-web/src —
# the reattach stream at lib/api.ts:openTurnStream is a plain fetch), so every
# call already carries the app's manifest scopes.
#
# READ vs WRITE follows what the route DOES to the conversation, not the HTTP
# verb: /messages, /nudge and /turns/{id}/cancel are POSTs that write or act,
# and PUT /thread-context sets the budget.
# GET /turns/{id}/stream is a read even though it tails a live turn.
_READ = [Depends(requires_permission("read:circles"))]
_WRITE = [Depends(requires_permission("write:circles"))]


# ---- Audit log ----

async def _audit(
    *,
    request_id: str,
    entity_id: int,
    session_id: str | None,
    target_kind: str,
    phase: str,
    status_: str,
    text_preview: str | None = None,
    error_class: str | None = None,
    error_detail: str | None = None,
    elapsed_ms: int | None = None,
    app_id: str | None = "circles",
    cc_session_id: str | None = None,
    originating_app: str | None = "circles",
) -> None:
    """Write one row to chat_events. Best-effort: an audit-log failure
    must not block the chat path. Schema landed via migration v002, then
    extended in v032 with app_id/cc_session_id/originating_app for the
    unified chat log + SDK resume tracking."""
    pool = brain_service.get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO chat_events (
                    request_id, entity_id, session_id, target_kind,
                    phase, status, text_preview, error_class, error_detail,
                    elapsed_ms, app_id, cc_session_id, originating_app
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                          $11, $12, $13)
                """,
                uuid.UUID(request_id),
                entity_id,
                uuid.UUID(session_id) if session_id else None,
                target_kind,
                phase,
                status_,
                text_preview,
                error_class,
                error_detail,
                elapsed_ms,
                app_id,
                cc_session_id,
                originating_app,
            )
    except Exception as exc:  # noqa: BLE001 - audit must never break chat
        log.warning("chat_events audit write failed: %s", exc)


def _err_envelope(request_id: str, code: str, message: str, http_code: int) -> HTTPException:
    """Structured error response. The Circles UI surfaces `message` as a
    toast and keeps the user's draft so the message isn't lost."""
    return HTTPException(
        status_code=http_code,
        detail={"ok": False, "error": {"code": code, "message": message, "request_id": request_id}},
    )


# ---- Helpers ----

def _entity_id() -> int | None:
    try:
        return brain_service._entity_id
    except Exception:
        return None


async def _entity_display_name() -> str:
    """Return this entity's human-readable display name. Used as the
    `author_name` on assistant messages and as the sidebar self-pane
    label, so Circles renders 'Ada' not 'Entity'."""
    from server.host_display import display_name_or_host, host_label
    pool = brain_service.get_pool()
    eid = _entity_id()
    if pool is None or eid is None:
        return host_label(eid)
    try:
        row = await pool.fetchrow(
            "SELECT display_name FROM entity_registry WHERE id = $1", eid,
        )
        if row:
            return display_name_or_host(row["display_name"], eid)
    except Exception:
        pass
    return host_label(eid)


async def _bridge_call(op: str, params: dict[str, Any] | None = None) -> dict | None:
    """Send an op to the bridge. Returns the data dict, or None on any
    failure. Callers must handle None as "treat as empty" — never raise."""
    try:
        client = server_main.get_bridge_client()
        if client is None:
            return None
        return await client.request(op, params or {})
    except Exception as e:
        log.warning("bridge call %s failed: %s", op, e)
        return None


async def _try_direct_peer_send(sender_id: int, target_id: int, text: str,
                                slip: dict, *, thread_id: str | None = None,
                                from_user_email: str | None = None,
                                from_user_name: str | None = None,
                                to_user_email: str | None = None,
                                own_payload: dict | None = None,
                                message_id: str | None = None,
                                topic: dict | None = None,
                                ) -> tuple[dict | None, str]:
    """Workstream B: deliver a host↔host DM over the DIRECT peer lane (:8444) instead
    of the bridge relay. Connect to the peer, present the slip; the peer verifies it
    OFFLINE and stores the message. On success we also write our own outbound copy
    locally (the bridge path used to do this for us).

    Returns `(data, reason)`. `data` is None to signal "fall back to the bridge
    relay" (peer unavailable / on an older image / slip refused → the relay may
    still deliver to an accepted contact).

    `reason` is why, and it is the point of the tuple. Both lanes can fail for
    unrelated causes, and until this existed a host↔host message that died did so
    with one 502 that named only the relay — the direct lane's refusal was an INFO
    line somewhere above it, if it was anywhere. The reason travels back to the
    caller so the error envelope carries it, and the same string is logged at
    WARNING here: one line names which lane refused and why.

    THE PERSON FIELDS (the host vouches for its own people). A send that
    names a human used to be barred from this lane, because
    the platform was the thing that verified the name and the platform is not in
    this path. It now travels here: we assert our own operator's address, the
    receiver checks its own, and the mTLS client cert binds the assertion to this
    host. `from_user_email` must already be a verified operator of THIS host —
    callers take it from the router-injected header, never from the request body.

    The payload is built to match op_circles_send_entity_dm field for field
    (adapter.py:5148-5186) rather than approximately. The two lanes write into the
    same inbox and are read back by queries that cannot tell them apart, so any
    field only one lane sets is a difference that surfaces later as a thread that
    groups on one path and not the other — thread_id and body_format were exactly
    that until this change.
    """
    # The wire and the diagnosis live in ONE place now — server/peer/deliver.py —
    # because messages.send_message_service needs exactly the same call for exactly
    # the same reason, and a second copy would drift the moment one lane gained a
    # field. What stays HERE is what is genuinely this caller's: the sender's own
    # outbound copy, and what to do when the far side did not keep the person.
    from server.peer.deliver import deliver_message, DM_SUBJECT
    subject = DM_SUBJECT
    data, reason, person_stored = await deliver_message(
        sender_id, int(target_id), slip,
        subject=subject, body=text,
        thread_id=thread_id, message_id=message_id,
        from_user_email=from_user_email, from_user_name=from_user_name,
        to_user_email=to_user_email, topic=topic)
    if data is None:
        return None, reason

    # WHAT THE RECEIVER ACTUALLY STORED. A pod on an older image reads
    # neither top-level person field, so it files the message host-authored and still
    # answers ok — and we would file OUR copy under People. One message in two
    # different lists. Falling back to the relay cannot repair it either: the message
    # is already stored there and message_id dedupes the second delivery, so the row
    # would stay host-authored while our copy stayed a person's. So match what
    # happened instead: drop the person from our copy too. Both sides then agree,
    # honestly, as host mail, and new traffic files correctly the moment that pod
    # rolls. The flag is set only by a receiver carrying this change.
    if from_user_email and person_stored is None:
        # ABSENT, not false: an older image never read the field, so it
        # filed the message host-authored while we would file ours under People. It
        # DOES store the thread_id we sent, so demoting our copy leaves both sides
        # on the same thread as well as the same list. Transient — it self-corrects
        # for new traffic the moment that pod rolls.
        log.info("peer %s stored this without the person (older image) — filing our "
                 "copy as host-authored so both sides agree", target_id)
        from_user_email = None
        from_user_name = None
        to_user_email = None
        own_payload = None
    elif from_user_email and person_stored is False:
        # PRESENT AND FALSE: the peer understood us and refuted the claim from its
        # own address book. We do NOT demote, for two reasons.
        #
        # It would cross an operator-privacy boundary in the wrong direction:
        # _list_people_threads / _read_people_thread are scoped to the operator who
        # is a party to the thread, while _list_entity_threads / _read_entity_thread
        # are host-wide. Demoting would move this operator's own sent message out of
        # their private thread and into one every operator of this host can read —
        # their view of their own message changed by the RECIPIENT's stale contacts.
        #
        # And the asymmetry is honest. We sent as a person and that is true; they
        # could not corroborate it and that is also true. Two hosts holding different
        # evidence about one claim recording different things is the correct
        # outcome, not a bug to paper over. Logged so it is visible.
        log.info("peer %s refuted the person on this message from its own records — "
                 "keeping our copy as sent; the two sides disagree, on purpose",
                 target_id)

    # Our own outbound copy so the thread shows both sides (the relay path did this).
    # Mirrors adapter.py:5168-5186 — same keys, same nested peer_entity_id, and the
    # person fields carried through so the sender's own People list files this send
    # beside the replies to it. Without from_user_email here the reader's is_self
    # test (circles.py _people_thread: r["from_user_email"] == my_email) is never
    # true for our own messages and a person↔person thread shows one side only.
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        # `own_payload` is the sender's own note-to-self about this send, and it
        # stays OUT of the wire payload above on purpose — the peer has no use for
        # our name for them, so it should not travel. peer_user_name is the
        # load-bearing one: _list_people_threads reads payload->>'peer_user_name'
        # off OUR OWN outbound rows to title a People thread, so dropping it makes
        # the sidebar fall back to the raw address.
        # The topic goes on our copy as it went on the wire, so this Host lists
        # the message under the same topic the peer files it under.
        own_inner: dict[str, Any] = {"peer_entity_id": int(target_id),
                                     **(own_payload or {}), **(topic or {})}
        if from_user_name:
            own_inner["from_user_name"] = from_user_name
        own_copy: dict[str, Any] = {
            "direction": "outbound", "outbound_to_entity_id": int(target_id),
            "from_entity_id": int(sender_id), "subject": subject, "body": text,
            "body_format": "text/plain",
            "message_kind": "message",
            "sender_verified": True,
            "thread_id": thread_id,
            "payload": own_inner,
        }
        if from_user_email:
            own_copy["from_user_email"] = from_user_email
        if to_user_email:
            own_copy["to_user_email"] = to_user_email
        await handle_inbox_append(own_copy)
    except Exception as oe:
        log.warning("outbound copy write failed after direct send (non-fatal): %s", oe)
    return data, "ok"


# ---- Self-thread (talking to your own entity) ----
#
# The "Your entity" pane in Circles is THE conversation with your synaptive
# entity. No sessions: read and write go directly against entity_memory
# turn rows tagged with app_id='circles' and project_id IS NULL.
#
# This path once used `_get_or_create_self_session`
# + entity_sessions.messages JSONB as the chat-log source of truth. That had
# two structural problems:
#   1. The session row was shared with whatever other surface happened to
#      have `channel='chat'`, producing cross-wired rows (e.g. a Circles
#      session ending up with `interaction_context='project:181'` because
#      the Workbench was using the same channel pool).
#   2. The system_prompt cached on the row froze at session-creation time,
#      so post-deploy prompt updates never reached the model on this
#      surface — the model kept reading a pre-deploy prompt.
# Both gone now: chat context is built per-request from the entity row,
# turn rows are the only durable state, and the read/write is tag-filtered.


def _normalize_turn_rows(rows: list[dict], entity_name: str) -> list[dict]:
    """Convert entity_memory turn rows into the UI's message shape:
    { author_name, author_kind, body, created_at, tool_calls?, attachments? }.

    The row carries `metadata.role` ('user' / 'assistant'). Skip rows
    with no usable content or unknown roles. tool_calls (assistant rows)
    and attachments (user rows) are persisted by the runtime so chips
    survive reload.
    """
    out: list[dict] = []
    for r in rows:
        md = r.get("metadata") or {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        role = md.get("role")
        if role not in ("user", "assistant"):
            continue
        body = (r.get("content") or "").strip()
        if not body:
            continue
        created = r.get("created_at")
        msg = {
            # The turn-row id: the message's key on screen, and what a topic
            # marker's Split here names (from_turn_id).
            "id": r.get("id"),
            "author_name": "You" if role == "user" else entity_name,
            "author_kind": "self" if role == "user" else "entity",
            "body": body,
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        }
        # The live turn that wrote this row (session.py `_turn_tags`); the UI
        # swaps its streamed copy of the turn for this row by it.
        if md.get("turn_token"):
            msg["turn_token"] = md["turn_token"]
        # Words the person sent INTO a running turn, saved when it read them
        # (session.py's drain): the UI swaps their echo for this row.
        if role == "user" and md.get("nudge") is True:
            msg["nudge"] = True
        if isinstance(md.get("tool_calls"), list) and md["tool_calls"]:
            msg["tool_calls"] = md["tool_calls"]
        if isinstance(md.get("attachments"), list) and md["attachments"]:
            msg["attachments"] = md["attachments"]
        # A message of a person's chat given to this turn (Ask Ivy):
        # {kind, ref, message_id}, recorded by session.py from the
        # turn's frame. The quote itself is in the row's words.
        # A list when several messages went on as one turn (turn_registry).
        if role == "user" and isinstance(md.get("carry"), (dict, list)) and md["carry"]:
            msg["carry"] = md["carry"]
        # Per-turn metrics: duration/tokens/model stamped by the
        # runtime at turn-write. Assistant rows only — user rows have no run.
        if role == "assistant" and md.get("gen_ms") is not None:
            msg["metrics"] = {
                "gen_ms": md.get("gen_ms"),
                "tokens_total": md.get("tokens_total"),
                "tokens_in": md.get("tokens_in"),
                "tokens_out": md.get("tokens_out"),
                # Cached-prefix split — present on turns run
                # since the cache-telemetry instrumentation; older rows
                # simply omit it and the UI degrades to in/out only.
                "tokens_cached": md.get("tokens_cached"),
                "tokens_cache_write": md.get("tokens_cache_write"),
                "model": md.get("model"),
                # Provenance: the provider that answered and the
                # failover record, printed under the turn — never hover-only.
                "provider": md.get("provider"),
                "failed_over": md.get("failed_over"),
                # Price: list-price estimate + basis word,
                # stamped by session.py. Older rows omit both.
                "est_cost_usd": md.get("est_cost_usd"),
                "price_basis": md.get("price_basis"),
                "context_ledger": md.get("context_ledger"),
                # The Host's close_turn signal (the Review button).
                "close_turn": md.get("close_turn"),
            }
        # Agent-delivered turn (deliver_agent_result stamps origin='agent'):
        # surface the attribution so the UI can mark it as an agent's result,
        # not the host just talking.
        if md.get("origin") == "agent":
            msg["agent"] = {
                "task_id": md.get("task_id"),
                "title": md.get("agent_title"),
                "status": md.get("agent_status"),
                "cost_usd": md.get("agent_cost_usd"),
            }
        out.append(msg)
    return out


async def _read_self_thread(limit: int = 200,
                            thread_id: str | None = None) -> list[dict]:
    """Read recent Circles self-thread turn rows from entity_memory.

    There is no original conversation: the legacy ids
    (absent, null, "original", the client's historic "self") read as an
    empty page, so a row left in the legacy app:circles scope is shown
    nowhere.

    A thread is scoped by its descriptor, matched exactly
    (no app_id OR), so a thread's turns never bleed into the original
    conversation and vice versa. Like the original conversation, a thread's
    transcript shows superseded turn rows that recall hides
    (conversation_where, live_only=False); the scope itself is the same
    descriptor recall and the consolidator match on.

    Returns chronological (oldest-first) so the UI renders top-down.
    """
    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None or _ct.is_legacy(thread_id):
        return []
    try:
        # Exactly the rows this conversation RENDERS — one helper for the
        # read, the list's count, Split and the marker anchor
        # (circles_threads.shown_where: the conversation clause plus the
        # exclusion of the hidden lanes, older replay and A/B rows among them;
        # its docstrings record how the row set differs from recall's and why).
        args: list = [eid]
        where = _ct.shown_where(args, thread_id)

        args.append(limit)
        rows = await pool.fetch(
            f"SELECT id, content, metadata, created_at FROM entity_memory"
            f" WHERE {' AND '.join(where)}"
            f" ORDER BY created_at DESC LIMIT ${len(args)}",
            *args,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("self-thread read failed for entity %s: %s", eid, exc)
        return []
    entity_name = await _entity_display_name()
    # Walk reverse for chronological order.
    return _normalize_turn_rows(list(reversed([dict(r) for r in rows])), entity_name)


# _send_self_message is removed.
# Self-thread send is now streaming-only via brain_service.send_message,
# routed inline in the POST /messages handler below. The legacy JSON
# request/response path is gone; clients must parse the AI SDK Data
# Stream Protocol.


# ---- Per-thread model selection ---------------------------------------
#
# The in-chat ModelPicker is the ONLY source of a per-turn model pick. The
# client sends `model` exactly when the picker shows a pick and omits it when
# the picker shows "Default model" (circles-web/src/lib/api.ts: "only sent
# when picked so default routing is untouched") — so no `model` on a send
# means the Orchestrator's chat route runs, full stop.
#
# The pick was once ALSO persisted under
# `circles_thread_models[<thread_key>] = {provider, model_id}` and silently
# re-applied on every later send that carried no pick. Nothing ever read it
# back into the picker (the comment here claimed it did), so the screen said
# "Default model" while the turn ran on a pick made weeks earlier — a Host
# chatted on claude-haiku-4.5 (stored under the dead 'privatae' lane) while
# the Orchestrator said kimi/k3, so chat was not honouring the Orchestrator
# settings. The model pick is no longer persisted;
# a stale one is scrubbed the next time the thread sends. The entry itself
# stays — the per-thread context_budget (conversation-depth) lives on it and
# is still forwarded to the runtime on thread_override.
#
# Picks are guarded through provider_allowed_for_role, the single source of
# truth — any Keys-registry provider or 'local' passes for chat; SDK-lane
# aliases (subscription/claude-sdk) and unknown providers are refused
# (the SDK is not a production option).

def _thread_key(target_kind: str, target_id: Any) -> str:
    """Stable per-thread key for the model-pick / budget map.

    The original Circles conversation keeps the bare 'self' key it has always
    had — that is the key context_budget._circles_entry_value:261 reads back
    for an app:circles descriptor, and changing it would strand every budget
    and allowance already set. A Circles thread the user started keys by its
    own id ("thread:<uuid>"), the same shape the generic store's scope key
    takes (context_budget.read_thread_budget:301 builds "<kind>:<ref>").
    """
    if target_kind == "self":
        if _ct.is_legacy(None if target_id is None else str(target_id)):
            return "self"
        # Canonical, so the key the panel reads is the key the send path
        # wrote whatever spelling the path param used.
        return f"thread:{_ct.canonical_id(str(target_id))}"
    return f"{target_kind}:{target_id}"


def _normalize_thread_override(model: Any) -> dict | None:
    """Coerce a client `model` field into a {provider, model_id} override, or
    None. Accepts either a dict {provider, model_id|model} or a bare model-id
    string (provider then defaults to 'openrouter' — legacy picker compat;
    normalize_provider coerces a Claude-shaped id to 'anthropic' downstream).
    Returns None for empty/unusable input."""
    if not model:
        return None
    if isinstance(model, str):
        mid = model.strip()
        if not mid:
            return None
        return {"provider": "openrouter", "model_id": mid}
    if isinstance(model, dict):
        provider = (model.get("provider") or "").strip() or "openrouter"
        mid = (model.get("model_id") or model.get("model") or "").strip()
        if not mid:
            return None
        out = {"provider": provider, "model_id": mid}
        if model.get("reasoning_inline") is True:
            # the picked model writes its reasoning inline, closed by
            # </think> (inference_config._route)
            out["reasoning_inline"] = True
        return out
    return None


def _chat_override_allowed(override: dict | None) -> bool:
    """True iff this override may serve the `chat` role. Provider model:
    any Keys-registry provider or 'local' passes; the removed
    SDK lane's aliases normalize empty and are refused, as is any unknown
    provider id."""
    if not override:
        return False
    try:
        from mastercode.inference_config import provider_allowed_for_role
    except Exception:
        # Fail closed: if the guard can't be imported, refuse the override.
        return False
    return provider_allowed_for_role((override.get("provider") or "").strip(), "chat")


async def _thread_entry(thread_key: str) -> dict | None:
    """The thread's `circles_thread_models` entry as stored, or None. Read by
    the send path (for context_budget + stale-pick detection) and by
    thread-context. Best-effort: an unreadable row reads as no entry."""
    pool = brain_service.get_pool()
    eid = _entity_id()
    if pool is None or eid is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid
        )
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            bp = json.loads(bp)
        if not isinstance(bp, dict):
            return None
        entry = (bp.get("circles_thread_models") or {}).get(thread_key)
        return entry if isinstance(entry, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("read thread entry failed (entity %s, %s): %s", eid, thread_key, exc)
    return None


async def _scrub_thread_model(thread_key: str) -> None:
    """Drop an older persisted model pick (provider/model_id) from the
    thread's entry, keeping context_budget. Best-effort, never blocks a send.
    Called only when the send path has seen such a pick on the entry."""
    pool = brain_service.get_pool()
    eid = _entity_id()
    if pool is None or eid is None:
        return
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid
        )
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            bp = json.loads(bp)
        if not isinstance(bp, dict):
            return
        thread_models = dict(bp.get("circles_thread_models") or {})
        entry = dict(thread_models.get(thread_key) or {})
        if not ("model_id" in entry or "provider" in entry):
            return
        entry.pop("model_id", None)
        entry.pop("provider", None)
        if entry:
            thread_models[thread_key] = entry
        else:
            thread_models.pop(thread_key, None)
        bp["circles_thread_models"] = thread_models
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params=$2::jsonb WHERE id=$1",
            eid, json.dumps(bp),
        )
        log.info("circles: scrubbed stale per-thread model pick (entity %s, %s)", eid, thread_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("scrub thread model failed (entity %s, %s): %s", eid, thread_key, exc)


async def _thread_budget_override(thread_key: str) -> dict | None:
    """{context_budget, turn_allowance_usd} from the thread's entry, or None.

    These are the only persisted per-thread fields the runtime still takes on
    thread_override. Both ride together because the runtime reads them at one
    site (session.py, where the chat route resolves): a send that forwarded
    the depth dial but not the spend allowance would silently drop the
    person's budget for that turn and run to the backstop instead."""
    entry = await _thread_entry(thread_key)
    if not entry:
        return None
    out: dict = {}
    if entry.get("context_budget"):
        out["context_budget"] = entry["context_budget"]
    if entry.get("turn_allowance_usd"):
        out["turn_allowance_usd"] = entry["turn_allowance_usd"]
    return out or None


# ---- Conversations list ----

def _parse_dt(v):
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(v)
    except Exception:
        return None


async def _store_held_slip(peer_entity_id: int, slip: dict) -> None:
    """Upsert a (renewed) slip into held_slips, keyed by peer. expires_at is bound as
    a datetime (asyncpg rejects a str for a timestamptz param)."""
    pool = brain_service.get_pool()
    if pool is None:
        return
    terms = slip.get("terms") or {}
    try:
        await pool.execute(
            "INSERT INTO held_slips (peer_entity_id, slip, contract_id, expires_at, updated_at) "
            "VALUES ($1, $2::jsonb, $3, $4::timestamptz, now()) "
            "ON CONFLICT (peer_entity_id) DO UPDATE "
            "  SET slip=EXCLUDED.slip, contract_id=EXCLUDED.contract_id, "
            "      expires_at=EXCLUDED.expires_at, updated_at=now()",
            int(peer_entity_id), json.dumps(slip), slip.get("contract_id"),
            _parse_dt(terms.get("expires_at")),
        )
    except Exception as e:
        log.warning("held_slip store (renew) failed (non-fatal): %s", e)


async def _held_slip_for(peer_id) -> dict | None:
    """The live permission slip a peer issued us, to present when messaging them.
    RENEW-ON-USE: if the held slip is past ~50% of its TTL (or expired), transparently
    renew it under the same contract_id (contract.renew) before presenting — the
    renewal holds while the relationship's term is live and unrevoked. Returns None if
    there's no live slip (the send then falls back to domain_policy for a contact)."""
    pool = brain_service.get_pool()
    if pool is None:
        return None
    try:
        peer = int(peer_id)
    except (TypeError, ValueError):
        return None
    try:
        row = await pool.fetchrow(
            "SELECT slip, contract_id FROM held_slips WHERE peer_entity_id = $1", peer)
    except Exception as e:
        log.warning("held_slip lookup failed (non-fatal): %s", e)
        return None
    if not row:
        return None
    s = row["slip"]
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except Exception:
            return None
    if not isinstance(s, dict):
        return None
    terms = s.get("terms") or {}
    cid = row["contract_id"] or s.get("contract_id")
    now = _dt.datetime.now(_dt.timezone.utc)
    exp = _parse_dt(terms.get("expires_at"))
    iss = _parse_dt(terms.get("issued_at"))
    # Renew when past the half-life (SPIFFE-style rotate-at-half-life), or already expired.
    needs_renew = (
        exp is None
        or (iss is not None and now >= iss + (exp - iss) / 2)
        or (iss is None and now >= exp)
    )
    if needs_renew and cid:
        try:
            fresh = await _bridge_call("contract.renew", {"contract_id": cid})
            fslip = (fresh or {}).get("slip")
            if isinstance(fslip, dict):
                await _store_held_slip(peer, fslip)
                s = fslip
                exp = _parse_dt((fslip.get("terms") or {}).get("expires_at"))
        except Exception as e:
            log.info("slip renew-on-use failed for peer %s (using current if live): %s", peer, e)
    # Present only a still-live slip.
    if exp is not None and exp <= now:
        return None
    return s


async def _list_entity_threads(eid: int) -> list[dict]:
    """Host↔host DM threads, read POD-LOCAL from inbox_messages (no bridge round-
    trip — the inbox lives in this pod). One row per peer, latest message first.

    Each item's `display_name` is the PEER'S OWN chosen name (their host display
    name / alias), sourced the same way messages.conversations_service derives
    `peer_name`: from the message payload — `peer_display_name` on our outbound
    copies, `from_display_name` on the peer's inbound. Falls back to "Host {peer}"
    when no name was ever carried. No cross-tenant leakage: this is only the name
    the peer attached to messages they exchanged with us."""
    pool = brain_service.get_pool()
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            """
            SELECT DISTINCT ON (peer) peer, body, ts, peer_name FROM (
              SELECT CASE WHEN from_entity_id = $1 THEN to_entity_id
                          ELSE from_entity_id END AS peer,
                     body, COALESCE(sent_at, delivered_at) AS ts,
                     CASE WHEN from_entity_id = $1
                          THEN payload->>'peer_display_name'
                          ELSE COALESCE(payload->>'from_display_name',
                                        payload->>'peer_display_name')
                     END AS peer_name
                FROM inbox_messages
               WHERE message_kind = 'message'
                 AND from_entity_id IS NOT NULL AND to_entity_id IS NOT NULL
                 AND from_entity_id <> to_entity_id
                 AND (from_entity_id = $1 OR to_entity_id = $1)
                 AND archived_at IS NULL
                 -- person ↔ person traffic belongs to the People list
                 AND NOT (from_user_email IS NOT NULL AND to_user_email IS NOT NULL)
            ) t
            ORDER BY peer, ts DESC
            """,
            int(eid),
        )
    except Exception as e:
        log.warning("entity-thread list failed (non-fatal): %s", e)
        return []
    items = []
    for r in rows:
        if r["peer"] is None:
            continue
        items.append({
            "id": str(r["peer"]),
            "kind": "entity",
            "display_name": r["peer_name"] or f"Host {r['peer']}",
            "preview": (r["body"] or "")[:80],
            "unread_count": 0,
        })
    return items


# ---- People (person ↔ person, through their hosts) ----
#
# Circles Hosts | People: two lists so nobody mistakes a
# person for a host. A People thread is every 'message' row where BOTH ends are
# people (from_user_email AND to_user_email set); the hosts on the row are only
# the endpoints that carried it. The thread is keyed by the OTHER person's email
# — the identity that stays with the person, not with whichever host they run
# today — and the deterministic thread id below is the same on both sides.

# Derivation moved to server/routes/messages.py, beside dm_thread_id: the peer-lane
# delivery handler needs it, and reaching it through this module put server.main's
# app mount on the delivery path. Re-exported so every caller here is unchanged.
from server.routes.messages import people_thread_id  # noqa: E402,F401


async def _list_people_threads(eid: int, my_email: str) -> list[dict]:
    """Person ↔ person threads, read POD-LOCAL, for ONE person: the operator
    making the request (`my_email`, the verified X-Authentik-Email). A host may
    have several operators; each sees only the threads they are a party to.
    One row per peer person (their email), latest message first. The peer's
    name is what the bridge stamped on their messages (from_user_name) or what
    we recorded when we sent (peer_user_name); falls back to the address."""
    pool = brain_service.get_pool()
    if pool is None or not my_email:
        return []
    # Only rows whose person this Host can stand behind (circles_reading.
    # person_checked_sql), so a message one Host filed under another person's
    # address never opens or previews a chat with them.
    from server.circles_reading import person_checked_sql
    try:
        rows = await pool.fetch(
            f"""
            SELECT DISTINCT ON (peer) peer, body, ts, peer_name FROM (
              SELECT CASE WHEN from_user_email = $2 THEN to_user_email
                          ELSE from_user_email END AS peer,
                     body, COALESCE(sent_at, delivered_at) AS ts,
                     CASE WHEN from_user_email = $2
                          THEN payload->>'peer_user_name'
                          ELSE COALESCE(payload->>'from_user_name',
                                        payload->>'peer_user_name')
                     END AS peer_name
                FROM inbox_messages
               WHERE message_kind = 'message'
                 AND from_user_email IS NOT NULL AND to_user_email IS NOT NULL
                 AND from_user_email <> to_user_email
                 AND ((from_entity_id = $1 AND from_user_email = $2)
                   OR (to_entity_id = $1 AND to_user_email = $2))
                 AND archived_at IS NULL
                 AND {person_checked_sql("inbox_messages", "$1")}
            ) t
            ORDER BY peer, ts DESC
            """,
            int(eid), my_email,
        )
    except Exception as e:
        log.warning("people-thread list failed (non-fatal): %s", e)
        return []
    items = []
    for r in rows:
        if not r["peer"]:
            continue
        items.append({
            "id": str(r["peer"]),
            "kind": "dm",
            "display_name": r["peer_name"] or str(r["peer"]),
            "preview": (r["body"] or "")[:80],
            "unread_count": 0,
        })
    return items


async def _list_people_contacts_without_thread(eid: int, have: set[str]) -> list[dict]:
    """Human contacts the user can message but has no thread with yet, so the
    People list is never empty while there is someone to talk to. A pending
    contact (request sent, not yet accepted) is shown as such."""
    pool = brain_service.get_pool()
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            "SELECT handle, display_name, status FROM contacts "
            "WHERE kind = 'human' AND status <> 'blocked' ORDER BY COALESCE(display_name, handle)")
    except Exception as e:
        log.warning("people contacts list failed (non-fatal): %s", e)
        return []
    out = []
    for r in rows:
        addr = (r["handle"] or "").strip().lower()
        if not addr or addr in have:
            continue
        out.append({
            "id": addr,
            "kind": "dm",
            "display_name": r["display_name"] or addr,
            "preview": "",
            "unread_count": 0,
            "pending": r["status"] == "pending",
        })
    return out


async def _list_bastion_peers_without_thread(eid: int, have: set[str]) -> list[dict]:
    """Hosts a bastion of ours says it ALSO serves, that we have no thread with
    yet — so the channel to a peer is open the moment they become one, which is
    the point of discovery: the Circles channel must always be open for bastion
    peers.

    Why this rather than auto-sending a greeting, which is what the design first
    proposed: a host↔host DM is slip-gated. The receiver treats a bridge-signed
    permission slip as the SOLE authority for message_kind='message' and refuses
    without one (messages.py:227-236 — measured 6/6 refused). A peer we just met
    holds no slip, so an automatic greeting would land in OUR thread and bounce
    off theirs, leaving a conversation that looks two-sided and is not. Listing
    the peering opens the place to talk without sending anything and without
    widening any authorization — which discovery must never do.

    A DORMANT peering (the bastion stopped announcing it) is not listed: it is
    history, and belongs in Network rather than at the top of Circles. A peering
    whose entity id has not resolved yet cannot be opened, so it is not listed
    either — it appears on the beat that resolves it. list_peerings
    (peer_discovery.py:242-277) returns peer_entity_id (None until resolved) and
    `current`, which is exactly what this filters on.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return []
    try:
        from server.network import peer_discovery
        peerings = await peer_discovery.list_peerings(pool, eid)
    except Exception as e:
        log.warning("bastion peer list failed (non-fatal): %s", e)
        return []
    # Who we have already sent a connection request to. Without this the Peers
    # row offers "connect" for ever, including to a host the user asked ten
    # minutes ago — the request path records the pending contact precisely so
    # this list can say which state it is in. Keyed on peer_entity_id, never on
    # handle: the handle comes from the machine and the machine can change it.
    pending_ids: set[str] = set()
    try:
        rows = await pool.fetch(
            "SELECT peer_entity_id FROM contacts "
            "WHERE kind = 'host' AND status = 'pending' AND peer_entity_id IS NOT NULL")
        pending_ids = {str(r["peer_entity_id"]) for r in rows}
    except Exception as e:
        log.warning("pending peer contacts unreadable (non-fatal): %s", e)

    out, seen = [], set()
    for p in peerings:
        pid = p.get("peer_entity_id")
        if not p.get("current") or pid is None:
            continue
        key = str(pid)
        if key in have or key in seen:
            continue
        seen.add(key)
        machine = p.get("introduced_by") or ""
        where = (f"shares your machine {machine}" if machine
                 else "shares one of your machines")
        out.append({
            "id": key,
            "kind": "entity",
            "display_name": p.get("peer_handle") or p.get("peer_host_name") or f"Host {pid}",
            # Nothing has been exchanged yet; say WHY they are in the list
            # rather than showing an empty preview that reads like a lost
            # message. Once a request is out, say THAT instead — the useful
            # fact is no longer how they got here, it is that we are waiting.
            "preview": (f"{where} · request sent" if key in pending_ids else where),
            "unread_count": 0,
            "peer": True,
            "request_sent": key in pending_ids,
        })
    return out


async def _read_people_thread(peer_email: str, my_email: str, *, before=None,
                              limit=None) -> dict:
    """A person ↔ person thread, read POD-LOCAL: every message between THIS
    operator (`my_email`) and `peer_email` (inbound from them + our own outbound
    copies), oldest-first, newest-200 window. Scoped to the requesting person,
    so two operators of one host never see each other's People threads.
    Author is always a PERSON here; `via` names the host that carried the
    message so the reader still sees the endpoint.

    Only rows whose person this Host can stand behind count
    (circles_reading.person_checked_sql, the predicate the Host's own readers
    use): a row another Host filed under `peer_email` before the direct lane
    checked that claim is not shown as theirs. It stays stored."""
    from server.circles_reading import person_checked_sql
    from server.routes.messages import (page_limit, parse_thread_cursor,
                                        sized_page_sql, thread_page_budget)
    pool = brain_service.get_pool()
    eid = _entity_id()
    peer = (peer_email or "").strip().lower()
    if pool is None or eid is None or not peer or not my_email:
        return {"messages": [], "members": [], "policy": {}}
    n = page_limit(limit, _THREAD_PAGE)
    cur = parse_thread_cursor(before)
    self_name = await _entity_display_name()
    where = f"""
                 WHERE message_kind = 'message' AND archived_at IS NULL
                   AND from_user_email IS NOT NULL AND to_user_email IS NOT NULL
                   AND ((from_entity_id = $1 AND from_user_email = $3 AND to_user_email = $2)
                     OR (to_entity_id = $1 AND to_user_email = $3 AND from_user_email = $2))
                   AND {person_checked_sql("inbox_messages", "$1")}"""
    try:
        total = await pool.fetchval(
            "SELECT count(*) FROM inbox_messages" + where, int(eid), peer, my_email)
        rows = await pool.fetch(
            sized_page_sql(
                """message_id, from_entity_id, from_user_email, body, payload,
                   sent_at, COALESCE(sent_at, delivered_at) AS ts""",
                "FROM inbox_messages" + where, cur=4, lim=6, budget=7),
            int(eid), peer, my_email, cur[0] if cur else None, cur[1] if cur else None, n + 1,
            thread_page_budget(),
        )
    except Exception as e:
        log.warning("people-thread read failed (non-fatal): %s", e)
        return {"messages": [], "members": [], "policy": {}}
    rows, next_before = _page_rows(rows, n)
    messages = []
    for r in rows:
        is_self = r["from_user_email"] == my_email
        pl = r["payload"] or {}
        if isinstance(pl, str):
            try:
                pl = json.loads(pl)
            except Exception:
                pl = {}
        if is_self:
            author = pl.get("from_user_name") or r["from_user_email"] or "You"
            via = self_name
        else:
            author = pl.get("from_user_name") or r["from_user_email"] or peer
            via = pl.get("from_display_name") or f"Host {r['from_entity_id']}"
        m = {
            "id": str(r["message_id"]),
            "author_name": author,
            "author_kind": "self" if is_self else "person",
            "via": via,
            "body": r["body"] or "",
            "created_at": r["ts"].isoformat() if r["ts"] else None,
        }
        # A reading notice: "on" or "off", so a view draws it
        # as a notice rather than as words someone wrote. Stored only on a
        # person-to-person message and only with these values
        # (circles_reading.stored).
        if pl.get("reading_notice") in ("on", "off"):
            m["reading_notice"] = pl["reading_notice"]
        messages.append(m)
    # The reading switch: whether this operator has let
    # their Host read this chat (server/circles_reading.py). Read on its own,
    # so a failure here never hides the messages.
    try:
        from server import circles_reading as _reading
        host_reads = await _reading.is_on(pool, my_email, peer)
    except Exception as e:  # noqa: BLE001 — said in the log, shown as off
        log.warning("reading switch unreadable (shown off): %s", e)
        host_reads = False
    return {"messages": messages, "members": [],
            "policy": {"people": True, "host_reads": host_reads},
            "total": int(total or 0), "limit": n, "next_before": next_before}


# One page of a host↔host or person↔person thread. A page, not a
# window: older pages are read with `before` (the next_before a page returned),
# so no message of a long-running thread is out of reach.
_THREAD_PAGE = 200


def _page_rows(rows, n: int):
    """A sized page fetched newest-first (messages.sized_page_sql): keep at
    most n rows, put them oldest-first, and name the cursor of the oldest row
    RETURNED — so the next page starts exactly where this one stopped, whether
    the row count or the byte budget stopped it."""
    from server.routes.messages import take_page, thread_cursor
    rows, more = take_page(rows, n)
    rows.reverse()
    nxt = thread_cursor(rows[0]["sent_at"], rows[0]["message_id"]) if more and rows else None
    return rows, nxt


async def _read_entity_thread(peer_id: int, *, before=None, limit=None,
                              topic: str | None = None) -> dict:
    """A host↔host DM thread, read POD-LOCAL: messages between self and peer
    (inbound from the peer + our own outbound copies), one page of the newest
    `limit` (default _THREAD_PAGE) older than `before`, oldest-first, with the
    thread's true `total` and `next_before` for the page before it.

    `topic` (server/peer/topics.py) reads one topic of the
    conversation: "original" or a topic's id, already parsed by the caller
    (topics.parse). None reads the whole conversation, every topic, as before
    topics. Each message names the topic it is in, `topic_id`. A topic other
    than the original with no message here is a 404 (MessageError): a topic
    exists once a message carries it."""
    from server.circles_reading import CLAIMED_KEY, claim_address
    from server.peer import topics as _topics
    from server.routes.messages import (MessageError, page_limit, parse_thread_cursor,
                                        sized_page_sql, thread_page_budget)
    pool = brain_service.get_pool()
    eid = _entity_id()
    if pool is None or eid is None:
        return {"messages": [], "members": [], "policy": {}}
    n = page_limit(limit, _THREAD_PAGE)
    cur = parse_thread_cursor(before)
    self_name = await _entity_display_name()
    # The pair's rows (topics.PAIR_ROWS, the clause this read always used),
    # then the topic inside them. It is bound last in each query so no other
    # parameter moves: $3 in the count, $7 in the page (whose cursor, limit
    # and budget are $3 to $6).
    count_where = where = _topics.PAIR_ROWS
    args_topic: list = []
    if topic is not None:
        count_where += f" AND {_topics.topic_sql()} = $3"
        where += f" AND {_topics.topic_sql()} = $7"
        args_topic = [topic]
    try:
        total = await pool.fetchval(
            "SELECT count(*) FROM inbox_messages" + count_where,
            int(eid), int(peer_id), *args_topic)
        # The NEWEST page, re-ordered oldest-first for display. The previous
        # `ORDER BY ts ASC LIMIT 200` took the OLDEST 200, so a thread past the
        # window showed months-old messages and HID the live conversation —
        # the same window bug brain.py's chat_history fixed. Now older pages
        # are reachable too: the cursor (sent_at, message_id) names one row.
        rows = await pool.fetch(
            sized_page_sql(
                f"""message_id, from_entity_id, from_user_email, body, payload,
                   sent_at, COALESCE(sent_at, delivered_at) AS ts,
                   {_topics.topic_sql()} AS topic""",
                "FROM inbox_messages" + where, cur=3, lim=5, budget=6),
            int(eid), int(peer_id), cur[0] if cur else None, cur[1] if cur else None, n + 1,
            thread_page_budget(), *args_topic,
        )
    except Exception as e:
        log.warning("entity-thread read failed (non-fatal): %s", e)
        return {"messages": [], "members": [], "policy": {}}
    if topic is not None and topic != _topics.ORIGINAL and not total:
        raise MessageError(404, "There is no such topic with this Host.")
    rows, next_before = _page_rows(rows, n)
    messages = []
    for r in rows:
        is_self = r["from_entity_id"] == eid
        pl = r["payload"] or {}
        if isinstance(pl, str):
            try:
                pl = json.loads(pl)
            except Exception:
                pl = {}
        peer_name = pl.get("from_display_name") or f"Host {peer_id}"
        host_name = self_name if is_self else peer_name
        # A person typed it (from_user_email) → show the person, "via" the host
        # that carried it. Otherwise the host itself is the author.
        person = pl.get("from_user_name") or r["from_user_email"]
        if r["from_user_email"]:
            author_name, author_kind, via = person, ("self" if is_self else "person"), host_name
        else:
            author_name, author_kind, via = host_name, ("self" if is_self else "entity"), None
        m = {
            # Stable identity for the UI's React keys — without it every
            # thread reload remounts the whole conversation DOM.
            "id": str(r["message_id"]),
            "author_name": author_name,
            "author_kind": author_kind,
            "via": via,
            "body": r["body"] or "",
            "created_at": r["ts"].isoformat() if r["ts"] else None,
            "topic_id": r["topic"],
        }
        # The Host named a person this Host could not verify, so the message
        # is the Host's own (peer/handlers.inbox_deliver): say whom it named,
        # "<Host> says this is from <address>". Only ever on a Host's message.
        claimed = claim_address(pl.get(CLAIMED_KEY))
        if not r["from_user_email"] and not is_self and claimed:
            m["claimed_person"] = claimed
        messages.append(m)
    out = {"messages": messages, "members": [], "policy": {},
           "total": int(total or 0), "limit": n, "next_before": next_before}
    if topic is not None:
        out["topic_id"] = topic
    return out


@router.get("/conversations", dependencies=_READ)
async def list_conversations(request: Request) -> dict:
    """Return the full sidebar payload: self pin, DMs, entity DMs, and
    circles. Each section may be empty; the UI renders empty states."""
    from server.operator_identity import request_email
    eid = _entity_id()
    if eid is None:
        return {"self": None, "people": [], "entities": [], "circles": []}
    my_email = request_email(request.headers)

    # Self pin — the entity's actual display name (e.g. "Ada") so the
    # sidebar and the thread header don't say "Your entity".
    self_pane = {
        "id": "self",
        "kind": "self",
        "display_name": await _entity_display_name(),
        "unread_count": 0,
    }

    # Two lists, read POD-LOCAL from inbox_messages (Hosts | People):
    #   entities — host ↔ host and person → host traffic, one row per peer host
    #   people   — person ↔ person traffic, one row per peer person (email),
    #              plus human contacts with no thread yet so there is always
    #              someone to pick. (Replaces the never-wired `dms` section.)
    entity_items = await _list_entity_threads(eid)
    # A bastion peer is a host we share a machine with. The channel to them is
    # open from the moment they become a peer, before anything has been said —
    # so they join the Hosts list the same way a human contact with no thread
    # joins People, just above.
    entity_items += await _list_bastion_peers_without_thread(
        eid, {i["id"] for i in entity_items})
    people_items = await _list_people_threads(eid, my_email)
    people_items += await _list_people_contacts_without_thread(
        eid, {p["id"] for p in people_items})

    # The deck: a blocked person, and a Host blocked directly
    # or through its person, leave the list, and the rest are paired, each
    # person with their Host and each Host with its people
    # (server/circles_deck.py pair). A read that fails leaves the two lists
    # empty, as each list's own read does on a database error above: a list
    # that could not check a block must not show what it would hide.
    pool = brain_service.get_pool()
    if pool is not None:
        from server import circles_deck
        try:
            people_items, entity_items = await circles_deck.pair(pool, people_items, entity_items)
        except Exception as e:  # noqa: BLE001 — the list answers; the two sections say nothing
            log.warning("deck pairing failed (people and Hosts left out): %s", e)
            people_items, entity_items = [], []

    # Circles (groups) use the op the bridge ACTUALLY registers —
    # circles.list (adapter op_circles_list, returns {"circles":[rows]}).
    # The previous op name circles.list_circles never existed; the
    # section rendered empty forever.
    circles_data = await _bridge_call("circles.list", {"entity_id": eid}) or {}
    circle_items = [
        {
            "id": c.get("id"),
            "display_name": c.get("circle_name") or c.get("name") or f"circle {c.get('id')}",
            "unread_count": 0,
        }
        for c in (circles_data.get("circles") or circles_data.get("items") or [])
    ]

    # Unread: the other side's messages after the
    # conversation's seen mark, in `unread`, the field circles-web reads, and
    # in `unread_count`, which every item above set to a constant 0. Read for
    # each kind where its messages are (server/circles_marks.py): peer Hosts
    # and people from this Host's inbox_messages, one query each; rooms
    # through the bridge, one call per room, all within
    # circles_marks.ROOM_UNREAD_DEADLINE. A peer or a person with no message
    # to this Host has 0; a room the bridge did not answer for in time says
    # unread_unknown instead of a count, in both fields (its unread_count is
    # null, not the constant 0 above).
    if pool is not None:
        from server import circles_marks
        for items, counts, absent in (
                (entity_items, await circles_marks.entity_unread(pool, eid),
                 {"unread": 0, "unread_count": 0}),
                (people_items, await circles_marks.people_unread(pool, eid, my_email),
                 {"unread": 0, "unread_count": 0}),
                (circle_items, await circles_marks.circle_unread(
                    pool, eid, [c["id"] for c in circle_items], _bridge_call),
                 {"unread_unknown": True, "unread_count": None})):
            for item in items:
                key = str(item.get("id") or "")
                fields = counts.get(key.lower() if items is people_items else key)
                item.update(fields or absent)

    out = {
        "self": self_pane,
        "people": people_items,
        "entities": entity_items,
        "circles": circle_items,
    }
    # The groups this Host owns or is in (the groups service's list), for a
    # caller that may read groups: GET /api/groups needs read:group, and this
    # list needs only read:circles, so a caller without read:group gets null
    # rather than another route's data.
    if pool is not None and _token_holds(request, "read:group"):
        from server import circles_deck
        try:
            out["groups"] = await circles_deck.groups(pool, eid)
        except Exception as e:  # noqa: BLE001 — the list answers without them
            log.warning("deck groups failed (non-fatal): %s", e)
            out["groups"] = None
    else:
        out["groups"] = None
    return out


def _root_person(request: Request) -> bool:
    """Whether this request is the person's (runtime/root_turn.person_request:
    the Circles or Workbench app token). write:circles alone is not: another
    app may hold it, and the root thread takes only the person's words."""
    from mastercode.runtime.root_turn import person_request
    return person_request(request)


def _token_holds(request: Request, scope: str) -> bool:
    """Whether the app token on this request declared `scope` (the check
    requires_permission makes, asked rather than enforced)."""
    tok = getattr(getattr(request, "state", None), "app_token", None)
    return tok is not None and scope in (getattr(tok, "permissions", None) or ())


# ---- The deck's door and what waits on you ----
#
# Each carries the scopes of what it reads, as /bastions does: the door is
# inbox cards (read:inbox), the machines Hosts were found on (read:topology)
# and the groups a knock names (read:group); waiting adds the decisions and
# support drafts (read:tickets) to the door's count.
_DOOR_SCOPES = _READ + [Depends(requires_permission(s))
                        for s in ("read:inbox", "read:topology", "read:group")]


@router.get("/door", dependencies=_DOOR_SCOPES)
async def circles_door(request: Request) -> dict:
    """At the door, for the operator asking: connection requests from people
    and Hosts, knocks to join a group this Host owns, introductions from
    shared interests, machine requests, and Hosts newly found on your
    machines, newest first, each with the call its Accept and its Not now
    make (server/circles_deck.py door)."""
    from server import circles_deck
    from server.operator_identity import request_email
    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None:
        return {"items": [], "count": 0}
    try:
        items = await circles_deck.door(pool, eid, request_email(request.headers))
    except Exception as e:  # noqa: BLE001 — said, not guessed
        log.warning("door read failed: %s", e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The door could not be read right now.") from None
    return {"items": items, "count": len(items)}


@router.get("/waiting",
            dependencies=_DOOR_SCOPES + [Depends(requires_permission("read:tickets"))])
async def circles_waiting(request: Request) -> dict:
    """Waiting on you: every open decision with the room it was asked in
    (thread, original, project or none), the support drafts waiting for
    Send, and the door's count (server/circles_deck.py waiting)."""
    from server import circles_deck
    from server.operator_identity import request_email
    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None:
        return {"decisions": [], "support_drafts": [], "door": 0, "count": 0}
    try:
        return await circles_deck.waiting(pool, eid, request_email(request.headers))
    except Exception as e:  # noqa: BLE001 — said, not guessed
        log.warning("waiting read failed: %s", e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "What is waiting on you could not be read right now.") from None


# ---- Circles threads ----
#
# A thread is a scope, not a session: an id that every turn written inside it
# is stamped with. The original conversation is not a row — it answers to the
# sentinel id "original" everywhere on this surface and keeps every behaviour
# it has today.

def _threads_pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Brain not ready")
    return pool


async def _thread_id_from_body(body: dict, eid: int, request_id: str,
                               *, require_open: bool = True) -> str | None:
    """The `thread_id` a self-chat body names, normalised to a uuid string or
    None for the original conversation (absent, null, "original", or the
    client's historic "self").

    None rather than the sentinel, because the sentinel has no row: the
    descriptor for the original conversation is app:circles, and everything
    downstream reads None as "no thread". A uuid must belong to this entity
    and — on a send — still be open; a turn written into someone else's or a
    closed thread is a row nothing would ever read back. 400 either way, in
    the route's envelope shape.
    """
    raw = (body or {}).get("thread_id")
    if _ct.is_legacy(raw):
        return None
    # thread_exists_open, not get_thread: this runs on every send, and
    # get_thread's turn_count is a full count over the thread's rows that no
    # send has ever read.
    thread = await _ct.thread_exists_open(brain_service.get_pool(), eid, raw)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {
                "code": "unknown_thread",
                "message": "That conversation doesn't exist.",
                "request_id": request_id}},
        )
    if require_open and thread.get("closed_at"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {
                "code": "thread_closed",
                "message": "This conversation is closed. Reopen it or start a "
                           "new one to keep talking.",
                "request_id": request_id}},
        )
    # Return the ROW's canonical id, not the client's raw string.
    # _ct.is_uuid accepts uppercase/hyphenless forms and Postgres canonicalises
    # on ::uuid inside get_thread's `id = $1::uuid` lookup, so a non-canonical
    # `raw` (e.g. uppercase) still matches the row here but would become the
    # descriptor ref if returned verbatim — and every read keyed on
    # str(row["id"]) (memory_engine._turn_row_where's thread branch) would
    # then never see turns written under that non-canonical ref.
    return str(thread["id"])


@router.get("/bastions",
            dependencies=_READ + [Depends(requires_permission("read:topology"))])
async def list_bastion_directory() -> dict:
    """The bastions folder: every machine this Host is paired to, who anchors
    it, and the other Hosts that machine announced. Narrowed to exactly that (server/network/bastion_directory.py
    says why), and read:topology as well as read:circles, because it names
    this Host's machines the same way the Network app does."""
    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None:
        return {"machines": []}
    from server.network import bastion_directory, peer_discovery, topology
    bastions = await topology.list_bastions(pool, eid)
    peerings = await peer_discovery.list_peerings(pool, eid)
    return {"machines": bastion_directory.build_directory(bastions, peerings)}


@router.get("/threads", dependencies=_READ)
async def list_circles_threads() -> dict:
    """Every Circles thread: the original conversation first, then the rows
    newest-first. Each carries its turn count and its page marks: seen_at,
    last_activity_at, last_landed_at, unseen_landed_at, waiting_on_you
    (circles_threads.list_threads)."""
    return {"threads": await _ct.list_threads(_threads_pool(), _entity_id() or 0)}


@router.get("/last-open", dependencies=_READ)
async def circles_last_open() -> dict:
    """The thread Circles reopens on (server/ui_last_open.py): {ref} is
    'original', a thread id, or null when nothing is stored or the thread
    stored was deleted since."""
    from server import ui_last_open
    try:
        return await ui_last_open.read(_threads_pool(), _entity_id() or 0, "circles")
    except ui_last_open.LastOpenError as e:
        raise HTTPException(e.status, e.detail) from None


@router.put("/last-open", dependencies=_WRITE)
async def circles_set_last_open(request: Request) -> dict:
    """Record the thread now open in Circles. Body: {ref: 'original' | thread id}."""
    from server import ui_last_open
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        return await ui_last_open.write(_threads_pool(), "circles", (body or {}).get("ref"))
    except ui_last_open.LastOpenError as e:
        raise HTTPException(e.status, e.detail) from None


# ---- One conversation ----
#
# Circles opens on a blank page and the person scrolls up for what came
# before: one feed over every topic of the conversation (the original
# conversation and every live thread, the root thread included), newest at
# the bottom. Each row names its topic, so the page draws a one-line marker
# where the topic changes. Which topic a message goes to is the Host's pick
# (thread_pick.pick_topic), made on the send (`pick_topic: true`) or, while a
# turn runs, through POST /topic first, so words for the running topic go
# into that turn and words for another topic start their own turn at once.

_FEED_PAGE = 50


def _feed_where(args: list) -> list[str]:
    """The rows the feed shows: what each topic's own transcript shows
    (circles_threads.shown_where), across all of them at once. Topics only:
    there is no original conversation, so a row left in
    the legacy app:circles scope is shown nowhere. `args` carries $1 =
    entity id."""
    threads = (
        "entity_id = $1 AND memory_type = 'turn' AND status = 'active' AND project_id IS NULL"
        " AND metadata->'source_context'->>'kind' = 'thread'"
        " AND metadata->'source_context'->>'ref' IN"
        "     (SELECT id::text FROM circles_threads WHERE entity_id = $1 AND deleted_at IS NULL)")
    return [f"({threads})", _ct.NOT_REPLAY_ROW]


async def read_feed(pool, eid: int, *, before=None, before_id: int | None = None,
                    limit: int = _FEED_PAGE) -> dict:
    """One page of the feed, oldest first, each message with its topic
    ({id: thread id, title, root}) and a topic marker flagged.
    `next` is the cursor for the page before it, None at the start."""
    from server.connectors import thread_pick as _tp
    args: list = [eid]
    where = _feed_where(args)
    if before is not None:
        args.append(before)
        b = len(args)
        args.append(int(before_id) if before_id is not None else 2**62)
        where.append(f"(created_at, id) < (${b}, ${len(args)})")
    args.append(int(limit) + 1)
    rows = await pool.fetch(
        f"SELECT id, content, metadata, created_at FROM entity_memory WHERE {' AND '.join(where)}"
        f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)}", *args)
    more = len(rows) > limit
    rows = [dict(r) for r in rows[:limit]]
    titles = {str(r["id"]): (r["title"], r["builtin_type"]) for r in await pool.fetch(
        "SELECT id, title, builtin_type FROM circles_threads WHERE entity_id = $1 AND deleted_at IS NULL", eid)}
    topic_of: dict = {}
    for r in rows:
        md = _tp._md(r.get("metadata"))
        sc = md.get("source_context") or {}
        ref = str(sc.get("ref") or "")
        title, builtin = titles.get(ref, ("", None))
        topic = {"id": ref, "title": title or "Untitled topic", "root": builtin == "root"}
        topic_of[r["id"]] = (topic, md.get("source_kind") == _tp.TOPIC_MARKER_KIND)
    msgs = _normalize_turn_rows(list(reversed(rows)), await _entity_display_name())
    for m in msgs:
        topic, marker = topic_of.get(m.get("id"), ({}, False))
        m["feed_topic"] = topic
        if marker:
            m["topic_marker"] = True
    oldest = rows[-1] if rows else None
    nxt = ({"before": oldest["created_at"].isoformat(), "before_id": oldest["id"]}
           if more and oldest else None)
    return {"messages": msgs, "next": nxt}


@router.get("/feed", dependencies=_READ)
async def circles_feed(before: str | None = None, before_id: int | None = None) -> dict:
    """The one conversation, a page at a time, newest page first (read_feed).
    `before`/`before_id`: the cursor a page answered as `next`."""
    when = None
    if before:
        try:
            when = _dt.datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "before: an ISO time") from None
    return await read_feed(_threads_pool(), _entity_id() or 0, before=when, before_id=before_id)


# ---- Parallel work from the chat ----
#
# The internal threads the Host runs on its own (task_record op='run' with
# items, runtime/task_serial._drive_work): what the activity panel lists, each
# thread with its item's goal, status and the step it is on. They work in
# their run's project, so none is a topic of this conversation and none is
# ever offered to the topic pick (thread_pick.topic_scores reads no project's
# rows); what reaches the conversation is each thread's one finish or blocker
# line (task_serial.post_item_line).


async def read_work(pool, eid: int) -> dict:
    """Every internal thread of a running run of work, oldest run first."""
    rows = await pool.fetch(
        "SELECT r.id AS run_id, r.project_id, r.threads, i.id, i.thread_no, i.goal, i.status, i.step,"
        "       i.updated_at"
        "  FROM task_runs r JOIN task_work_items i ON i.run_id = r.id"
        " WHERE r.entity_id = $1 AND r.kind = 'work' AND r.status = 'running'"
        " ORDER BY r.id, i.thread_no", eid)
    return {"threads": [{
        "run_id": r["run_id"], "project_id": r["project_id"], "item_id": r["id"],
        "thread": r["thread_no"] + 1, "of": r["threads"], "goal": r["goal"], "status": r["status"],
        "step": r["step"], "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        # The registry lane its turns run on (session._wake_host_turn lane=).
        "lane": f"task-{r['run_id']}-t{r['thread_no']}"} for r in rows]}


@router.get("/work", dependencies=_READ)
async def circles_work() -> dict:
    """The internal threads running now (read_work)."""
    try:
        return await read_work(_threads_pool(), _entity_id() or 0)
    except Exception as e:  # noqa: BLE001 — a kernel before v226: nothing runs
        log.debug("work threads not read: %s", e)
        return {"threads": []}


async def _topic_for(eid: int, current: str | None, text: str, known: bool = True):
    """The Host's topic pick for a Circles message (thread_pick.pick_topic).
    Never fails a send: anything that goes wrong leaves the message in the
    topic the person was last in. A closed topic the message stays in is
    opened again (the person wrote in it)."""
    from server.connectors import thread_pick as _tp
    pool = brain_service.get_pool()
    try:
        landing = await _tp.pick_topic(pool, eid, text, current_thread_id=current,
                                       current_known=known)
    except Exception as e:  # noqa: BLE001 — the message stays where it was
        log.warning("circles topic pick failed (message stays): %s", e)
        landing = _tp.TopicLanding(thread_id=current, title="", failure=str(e)[:200])
    if landing.thread_id and not landing.picked and not landing.root:
        try:
            await pool.execute(
                "UPDATE circles_threads SET closed_at = NULL WHERE id = $1::uuid AND entity_id = $2"
                " AND closed_at IS NOT NULL AND deleted_at IS NULL", landing.thread_id, eid)
        except Exception as e:  # noqa: BLE001
            log.warning("reopening the topic failed: %s", e)
    return landing


def _with_choice(frame: dict | None, landing) -> dict | None:
    """The turn's frame with the topics the Host may still move the message
    to in its turn (runtime/thread_choice), when the placement left any."""
    if not getattr(landing, "choice", None) or not landing.thread_id:
        return frame
    from mastercode.runtime.thread_choice import frame_value
    return {**(frame or {}), "thread_choice": frame_value(
        surface="circles", candidates=list(landing.choice),
        landed={"kind": "thread", "ref": landing.thread_id,
                "title": landing.title or "a new topic", "provisional": bool(landing.new)})}


def _topic_known(body: dict) -> bool:
    """Whether the page names a topic the person was in this visit: a blank
    page after login names none, and its first message is then no
    follow-on of anything (thread_pick.pick_topic current_known)."""
    return str(body.get("thread_id") or "").strip() != ""


def _topic_json(landing) -> dict:
    # thread_id None when no topic could be picked or made: the page then
    # sends with no thread and the send route lands it (never "original").
    return {"thread_id": landing.thread_id, "title": landing.title,
            "new": landing.new, "moved": landing.moved, "marker": landing.marker,
            "picked": landing.picked, "root": landing.root, "follow_on": landing.follow_on,
            "related": list(landing.related)}


@router.post("/topic", dependencies=_WRITE)
async def circles_topic(request: Request) -> dict:
    """Pick the topic for a message the person is about to send while a turn
    runs: {body, thread_id (the topic they were last in)} -> where it goes
    (_topic_json). The page then puts the words into the running turn when it
    is that topic's, or sends them as their own turn when it is another's."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    text = str((body or {}).get("body") or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body: the message")
    eid = _entity_id() or 0
    current = await _thread_id_from_body(body or {}, eid, str(uuid.uuid4()), require_open=False)
    return _topic_json(await _topic_for(eid, current, text, _topic_known(body or {})))


@router.post("/threads", dependencies=_WRITE, status_code=status.HTTP_201_CREATED)
async def create_circles_thread(request: Request) -> dict:
    """Start a thread. Body: {title?}. A new thread's scope has zero rows —
    no history and no summary — which is what makes its first turn the cheap
    one."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body or {}).get("title")
    title = str(title).strip() if title else None
    # A title the person typed is cut of its secret shapes before the thread
    # row keeps it, as the turn cuts a message (server/secret_cut.py): the
    # thread list shows it and a split writes it into a memory row.
    if title is not None:
        title = secret_cut.redact_words(title, door="circles thread title", log=log)
    thread = await _ct.create_thread(_threads_pool(), _entity_id() or 0,
                                     title=title)
    return {"thread": thread}


@router.patch("/threads/{thread_id}", dependencies=_WRITE)
async def patch_circles_thread(thread_id: str, request: Request) -> dict:
    """Rename and/or close a thread. Body: {title?, closed?}. Absent fields
    are left alone, so the rename and the close controls never clobber each
    other."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    title = body.get("title")
    if title is not None:
        # Cut as a new thread's title is (create_circles_thread, above).
        title = secret_cut.redact_words(str(title).strip(), door="circles thread title", log=log)
    closed = body.get("closed")
    if closed is not None:
        closed = bool(closed)
    try:
        thread = await _ct.patch_thread(_threads_pool(), _entity_id() or 0,
                                        thread_id, title=title, closed=closed)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found") from None
    return {"thread": thread}


@router.delete("/threads/{thread_id}", dependencies=_WRITE)
async def delete_circles_thread(thread_id: str) -> dict:
    """Delete a thread: its messages and everything the Host learned in it
    leave the Host's memory (archived, not erased), what it retired elsewhere
    comes back, the wake-ups set in it are cancelled, and it leaves the list
    (circles_threads.delete_thread). A reply still being written in it is
    stopped first. A row that lands afterwards anyway — a reply that
    finished, a stopped-turn note — is archived on arrival and never mined,
    and an agent's result still addressed to the thread lands in the
    original conversation. The original conversation cannot be deleted —
    clear it instead."""
    from server.turn_registry import compose_thread_key, get_turn_registry
    eid = _entity_id() or 0
    pool = _threads_pool()
    tid = _ct.canonical_id(thread_id)
    if tid is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "the original conversation cannot be deleted; clear it instead")
    reg = get_turn_registry()
    running = reg.active_for(compose_thread_key(eid, "circles", None, thread_id=tid))
    if running is not None:
        await reg.cancel(running.turn_id)
    try:
        out = await _ct.delete_thread(pool, eid, tid)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found") from None
    # A wake-up of this thread that is firing now runs in its own turn lane
    # (offline_task_runner.wakeup_thread_key), which the key above does not
    # reach. Its task is cancelled, so its turn stops as well.
    from mastercode.brain.offline_task_runner import wakeup_thread_key
    for wid in out.get("cancelled_ids") or []:
        firing = reg.active_for(wakeup_thread_key(eid, wid))
        if firing is not None:
            await reg.cancel(firing.turn_id)
    return {"ok": True, "deleted": out}


@router.get("/threads/{thread_id}/wakeups", dependencies=_READ)
async def circles_thread_wakeups(thread_id: str) -> dict:
    """The wake-ups set in this thread that have not finished — what deleting
    it cancels, so the confirm dialog can name them."""
    return {"wakeups": await _ct.wakeups_set_in(_threads_pool(), _entity_id() or 0,
                                                thread_id)}


@router.get("/threads/{thread_id}/markers", dependencies=_READ)
async def circles_thread_markers(thread_id: str) -> dict:
    """The topic flips in this thread, oldest first — the system's
    suggestion of where a boundary is. Splitting on one is the user's act,
    never automatic."""
    return {"markers": await _ct.markers(_threads_pool(), _entity_id() or 0,
                                         thread_id)}


@router.post("/threads/{thread_id}/split", dependencies=_WRITE,
             status_code=status.HTTP_201_CREATED)
async def split_circles_thread(thread_id: str, request: Request) -> dict:
    """Split this thread at a turn: every turn from `from_turn_id` onward is
    re-tagged into a new thread. Body: {from_turn_id:int, title?}.

    A retag, never a delete — a marker turn stays behind so the move does not
    read as a disappearance."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        from_turn_id = int(body.get("from_turn_id"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "from_turn_id must be a turn id") from None
    title = body.get("title")
    title = str(title).strip() if title else None
    # Cut as a new thread's title is (create_circles_thread, above): the split
    # also writes the title into the marker row it leaves in entity_memory.
    if title is not None:
        title = secret_cut.redact_words(title, door="circles thread title", log=log)
    try:
        thread = await _ct.split_thread(_threads_pool(), _entity_id() or 0,
                                        thread_id, from_turn_id=from_turn_id,
                                        title=title)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    return {"thread": thread}


# ---- Thread view ----

@router.get("/thread/{kind}/{thread_id}", dependencies=_READ)
async def get_thread(kind: str, thread_id: str, request: Request) -> dict:
    """Fetch a thread's messages + metadata. `kind` is one of
    self|dm|entity|circle and disambiguates the backend lookup."""
    if kind not in {"self", "dm", "entity", "circle"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown thread kind")

    if kind == "self":
        # Self-thread: the same conversation Cortex shows. We read from
        # the entity's active channel='chat' session so Circles and Cortex
        # are two surfaces on one continuous thread.
        # `thread_model` is gone from this payload: no client ever
        # read it (circles-web + the legacy dashboard, grepped) and
        # no per-thread model pick is persisted any more — the picker's
        # state is the pick. See the per-thread model selection block above.
        #
        # The path id IS the Circles thread here; a legacy id ("original",
        # "self") names no thread any more and reads as an empty page.
        return {
            "messages": await _read_self_thread(thread_id=thread_id),
            "members": [],
            "policy": {"private": True},
            "thread_id": None if _ct.is_legacy(thread_id) else thread_id,
        }

    if kind == "circle":
        # Use the op the bridge actually registers: circles.messages
        # (adapter op_circles_messages, params {circle_id, limit},
        # returns {"messages":[rows DESC]}). The previous op name
        # circle_read_thread never existed in the adapter.
        # circle_id MUST go over as an int — the path param is a string
        # and asyncpg refuses str against the INT column, which the
        # bridge surfaces as a 500 we'd swallow into an empty thread.
        try:
            circle_id = int(thread_id)
        except ValueError:
            return {"messages": [], "members": [], "policy": {}}
        # One PAGE, newest first: the
        # bridge pages circles.messages with an opaque `before` cursor and
        # answers total / limit / next_before. With no `before` it is the
        # newest page, exactly as before — and an older bridge, which sends
        # none of the new keys, still works: what came back is shown with no
        # way to ask for more.
        params: dict[str, Any] = {"circle_id": circle_id, "limit": _THREAD_PAGE}
        before = request.query_params.get("before")
        if before:
            # A paged read must say why it failed: a cursor the bridge did not
            # issue is its 400 with a plain sentence, not an empty thread.
            params["before"] = before
            client = server_main.get_bridge_client()
            env = (await client.request_full("circles.messages", params)
                   if client is not None else {"ok": False, "error_kind": "infra",
                                                "error": "bridge not connected"})
            if not env.get("ok"):
                code = env.get("status") or (502 if env.get("error_kind") == "infra" else 400)
                raise HTTPException(int(code), env.get("error") or "could not read the earlier messages")
            data = env.get("data") or {}
        else:
            data = await _bridge_call("circles.messages", params)
        if not data:
            return {"messages": [], "members": [], "policy": {}}
        eid = _entity_id()
        self_name = await _entity_display_name()
        messages = []
        for m in reversed(data.get("messages") or []):
            sender = m.get("sender_entity_id")
            is_self = eid is not None and sender == eid
            row = {
                "author_name": self_name if is_self else (m.get("sender_name") or f"Host {sender}"),
                "author_kind": "self" if is_self else "entity",
                "body": m.get("content") or "",
                "created_at": m.get("created_at"),
            }
            if m.get("id") is not None:
                # The bridge row's own id: a stable key, so an earlier page
                # put above is never a duplicate of a row already shown.
                row["id"] = f"circle-{m['id']}"
            messages.append(row)
        out = {"messages": messages, "members": data.get("members", []),
               "policy": data.get("policy", {})}
        if "next_before" in data:
            out["total"] = data.get("total")
            out["next_before"] = data.get("next_before")
        return out

    from server.routes.messages import MessageError
    if kind == "entity":
        # Host↔host DM thread — read pod-local from inbox_messages, a page at
        # a time (`before` = the next_before a page returned). `topic` reads
        # one topic of it: "original" or a topic's id; without
        # it, the whole conversation, as before topics.
        topic = None
        if "topic" in request.query_params:
            from server.peer import topics as _topics
            topic = _topics.parse(request.query_params.get("topic"))
            if topic is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "topic is \"original\" or a topic's id")
        try:
            return await _read_entity_thread(
                int(thread_id), before=request.query_params.get("before"),
                limit=request.query_params.get("limit"), topic=topic)
        except MessageError as e:
            raise HTTPException(e.status, e.detail)
        except (TypeError, ValueError):
            return {"messages": [], "members": [], "policy": {}}

    # 'dm' — a person ↔ person thread, keyed by the other person's email, scoped
    # to the person asking. Read pod-local like the host threads (the old bridge
    # op never existed).
    from server.operator_identity import request_email
    try:
        return await _read_people_thread(
            thread_id, request_email(request.headers),
            before=request.query_params.get("before"), limit=request.query_params.get("limit"))
    except MessageError as e:
        raise HTTPException(e.status, e.detail)


# ---- Seen marks and the page ----
#
# The marks live in
# circles_page_marks (v219), keyed by this route family's own {kind}/{id}; the
# reads are server/circles_marks.py and circles_threads.list_threads.

@router.put("/thread/{kind}/{thread_id}/seen", dependencies=_WRITE)
async def mark_circles_seen(kind: str, thread_id: str, request: Request) -> dict:
    """The person has this conversation open. Returns {kind, id, seen_at}, the
    mark as stored: it never moves back (circles_marks.mark_seen). With a body
    {turn_token} on one of your Host's pages, the reply of that turn is also
    marked as watched, so its row never lights the page's tab whenever it is
    written. The body is optional, as PUT /api/projects/{id}/seen's is."""
    from server import circles_marks
    from server.operator_identity import request_email
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no body is a plain mark
        body = None
    token = body.get("turn_token") if isinstance(body, dict) else None
    if token is not None and not isinstance(token, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "turn_token is not a turn token")
    # `?topic=` marks one topic of a conversation with another Host:
    # "original" or a topic's id. Without it the mark is the whole
    # conversation's, as before topics.
    topic = request.query_params.get("topic") if "topic" in request.query_params else None
    try:
        return await circles_marks.mark_seen(
            _threads_pool(), _entity_id() or 0, kind, thread_id,
            reader=request_email(request.headers), turn_token=token, topic=topic)
    except circles_marks.MarksError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/thread/{kind}/{thread_id}/topics", dependencies=_READ)
async def list_circles_topics(kind: str, thread_id: str) -> dict:
    """The topics of this Host's conversation with another Host
    (server/peer/topics.py list_topics): the original conversation
    first, then each topic by last activity, with its title, who started it,
    its counts and its own unread. Only a conversation with another Host has
    topics: your own Host's pages are its threads (GET /threads), and a
    person is one continuous chat."""
    if kind != "entity":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only a conversation with another Host has topics; your own Host has "
            "threads, and a person is one continuous chat.")
    try:
        peer = int(thread_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a Host's id is a number") from None
    eid = _entity_id()
    if peer <= 0 or eid is None or peer == int(eid):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that is not another Host")
    from server.peer import topics as _topics
    try:
        items = await _topics.list_topics(_threads_pool(), int(eid), peer)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — said, not guessed
        log.warning("topics read failed for peer %s: %s", peer, e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The topics could not be read right now.") from None
    return {"kind": "entity", "id": str(peer), "topics": items}


@router.put("/thread/{kind}/{thread_id}/reading", dependencies=_WRITE)
async def set_circles_reading(kind: str, thread_id: str, request: Request) -> dict:
    """The reading switch on a person's card
    (server/circles_reading.py). Body {on: true|false}. While it is on, this
    Host's own readers (the host_message tool) include the operator's chat with
    this one person; for everyone else nothing changes.

    Turning it on or off writes a notice into the chat on both sides: ours
    ("You let <Host> read this chat.") and the other person's version, sent
    to them as a message from the operator. The Host's name is its own
    display name.

    ON tells the other person FIRST and turns on only once they have been
    told: if the notice cannot be delivered, nothing changes (502). OFF turns
    off first, then tells them as well as it can (`delivered` says).
    Setting it to what it already is changes nothing and sends nothing."""
    from server import circles_reading as _reading
    from server.operator_identity import request_email
    from server.routes.contacts import clean_email, find_human_contact
    request_id = str(uuid.uuid4())
    if kind != "dm":
        raise _err_envelope(request_id, "not_a_person",
                            "Only a person's chat has a reading switch.",
                            status.HTTP_400_BAD_REQUEST)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — said below
        body = None
    on = body.get("on") if isinstance(body, dict) else None
    if not isinstance(on, bool):
        raise _err_envelope(request_id, "bad_body", "Send {\"on\": true} or {\"on\": false}.",
                            status.HTTP_400_BAD_REQUEST)
    operator = request_email(request.headers)
    if not operator:
        raise _err_envelope(request_id, "no_identity",
                            "No verified identity on this request. Reload and sign in again.",
                            status.HTTP_403_FORBIDDEN)
    person = clean_email(thread_id)
    if person is None:
        raise _err_envelope(request_id, "bad_person", "A person's chat is named by their email.",
                            status.HTTP_400_BAD_REQUEST)
    pool = _threads_pool()
    eid = _entity_id()
    if eid is None:
        raise _err_envelope(request_id, "not_ready", "Your Host is starting. Try again in a moment.",
                            status.HTTP_503_SERVICE_UNAVAILABLE)
    contact = await find_human_contact(pool, person)
    if contact is None:
        raise _err_envelope(request_id, "no_contact", "This person is not in your contacts.",
                            status.HTTP_404_NOT_FOUND)
    if await _reading.is_on(pool, operator, person) == on:
        return {"kind": "dm", "id": person, "on": on, "changed": False}
    peer_host = contact["peer_entity_id"]
    name = contact["display_name"] or person
    if on and peer_host is None:
        raise _err_envelope(request_id, "not_connected",
                            f"You're not connected with {name} yet, so they can't be told.",
                            status.HTTP_409_CONFLICT)
    if on and contact["status"] == "blocked":
        raise _err_envelope(request_id, "blocked", "You've blocked this person.",
                            status.HTTP_409_CONFLICT)
    host_name = await _entity_display_name()
    ours, theirs = _reading.notice_texts(host_name, on)
    value = "on" if on else "off"
    delivered, lane = False, "not_attempted"
    if on:
        delivered, lane = await _deliver_reading_notice(
            int(eid), int(peer_host), operator, person, theirs, value)
        if not delivered:
            log.warning("reading switch not turned on: %s could not be told (direct lane %s) "
                        "[request_id=%s]", person, lane, request_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"ok": False, "error": {
                    "code": "not_told",
                    "message": f"{name} couldn't be told, so {host_name} still can't read "
                               "this chat. Try again in a moment.",
                    "request_id": request_id, "direct_lane": lane}})
        try:
            await _keep_reading_notice(int(eid), int(peer_host), operator, person, ours, value, name)
        except HTTPException:
            # They were told "on" and the switch stays off: tell them "off" as
            # well as we can, so nobody is left believing the chat is read.
            _o, theirs_off = _reading.notice_texts(host_name, False)
            try:
                await _deliver_reading_notice(
                    int(eid), int(peer_host), operator, person, theirs_off, "off")
            except Exception as e:  # noqa: BLE001 — the 503 below is what the person sees
                log.warning("reading switch: the take-back notice to %s failed: %s "
                            "[request_id=%s]", person, e, request_id)
            raise
        await _reading.set_on(pool, operator, person, True)
    else:
        await _reading.set_on(pool, operator, person, False)
        if peer_host is not None:
            await _keep_reading_notice(int(eid), int(peer_host), operator, person, ours, value, name)
            if contact["status"] != "blocked":
                delivered, lane = await _deliver_reading_notice(
                    int(eid), int(peer_host), operator, person, theirs, value)
    return {"kind": "dm", "id": person, "on": on, "changed": True,
            "notice": ours, "delivered": delivered}


async def _deliver_reading_notice(eid: int, peer_host: int, operator: str, person: str,
                                  text: str, value: str) -> tuple[bool, str]:
    """Send the other person their reading notice, as a message from
    `operator` in their chat, without writing our own copy (ours says it
    differently; _keep_reading_notice). The direct lane first with the slip
    their Host issued us, then the relay's inbox.send, which carries the
    people and the nested payload (bridge adapter.py _INBOX_SENDER_FIELDS) and
    writes no copy for the sender. One message id on both, so a direct send
    that landed and then timed out is not stored twice. Returns (delivered,
    the direct lane's reason)."""
    from server.circles_reading import NOTICE_KEY
    from server.peer.deliver import DM_SUBJECT, deliver_message
    thread_id = people_thread_id(operator, person)
    message_id = str(uuid.uuid4())
    notice = {NOTICE_KEY: value}
    held = await _held_slip_for(peer_host)
    lane = "not_attempted:no_slip_held"
    if held:
        try:
            data, lane, _person = await deliver_message(
                eid, peer_host, held, subject=DM_SUBJECT, body=text,
                thread_id=thread_id, message_id=message_id,
                from_user_email=operator, to_user_email=person, notice=notice)
            if data is not None:
                return True, "ok"
        except Exception as e:  # noqa: BLE001 — the relay is the fallback
            lane = f"error:{type(e).__name__}"
    params = {"to_entity_id": peer_host, "thread_id": thread_id, "message_id": message_id,
              "message_kind": "message", "subject": DM_SUBJECT, "body": text,
              "body_format": "text/plain", "from_user_email": operator,
              "to_user_email": person, "payload": notice}
    if held:
        params["slip"] = held
    data = await _bridge_call("inbox.send", params)
    return bool(data) and data.get("ok") is not False, lane


async def _keep_reading_notice(eid: int, peer_host: int, operator: str, person: str,
                               text: str, value: str, peer_name: str) -> None:
    """Our side's reading notice, stored as our own copy of a message in the
    chat: the row shape _try_direct_peer_send writes, so _read_people_thread
    reads it beside everything else in the chat."""
    from server.bridge.memory_handlers import handle_inbox_append
    from server.circles_reading import NOTICE_KEY
    from server.peer.deliver import DM_SUBJECT
    r = await handle_inbox_append({
        "direction": "outbound", "outbound_to_entity_id": peer_host,
        "from_entity_id": eid, "subject": DM_SUBJECT, "body": text,
        "body_format": "text/plain", "message_kind": "message", "sender_verified": True,
        "thread_id": people_thread_id(operator, person),
        "payload": {"peer_entity_id": peer_host, "peer_user_name": peer_name, NOTICE_KEY: value},
        "from_user_email": operator, "to_user_email": person,
    })
    if not r.get("ok"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The notice could not be written. Try again in a moment.")


# read:inference_log as well as read:circles, as the Workbench's page route
# (routes/projects.py get_project_page) asks for it: the page carries what the
# conversation cost. Circles holds both (system-apps/circles/app.yaml).
@router.get("/thread/{kind}/{thread_id}/page",
            dependencies=_READ + [Depends(requires_permission("read:inference_log"))])
async def get_circles_page(kind: str, thread_id: str) -> dict:
    """What the book shows at the top of one of your Host's pages: where the
    work stands, the conversation's running totals, the seen mark and what is
    waiting on the person (circles_marks.page_data). Only your own Host's
    pages have these: a peer Host's, a person's or a room's conversation runs
    no turn of this Host, so it has no totals and no "where this stands"."""
    if kind not in {"self", "dm", "entity", "circle"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown thread kind")
    if kind != "self":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only a page with your own host has a running count and \"where this "
            f"stands\". A {kind} conversation runs on the other host.")
    from server import circles_marks
    try:
        return await circles_marks.page_data(_threads_pool(), _entity_id() or 0, thread_id)
    except circles_marks.MarksError as e:
        raise HTTPException(e.status, e.detail) from None


# ---- Thread context (conversation-depth) ----
#
# The View Context / budget-dial surface for Circles threads. The budget is
# co-located with the per-thread model pick (circles_thread_models[<key>])
# — same store, same lifecycle; the send path forwards it to the runtime on
# thread_override and the runtime falls back to reading it here.

def _thread_src_ctx(kind: str, thread_id: str) -> dict | None:
    """Map a circles thread to the turn path's source_context {kind, ref}.
    For kind='self' the id is the Circles thread: the original conversation
    ("original", "self", or nothing) is app:circles — its interaction_context
    is 'chat' — and a thread the user started is thread:<uuid>.
    entity/dm threads are peer:<id>. Circle threads are bridge-owned (no
    local turn rows) → None, and the payload degrades to window/budget.

    A peer descriptor is returned for dm/entity even though NOTHING writes a
    peer-stamped prompt_log row — derive_source_context's `peer_ref`
    (memory_engine.py:1018) has no call sites, and a peer turn is ingested
    from the other host rather than built here. So the thread-context panel's
    per-turn blocks and pre-fire are permanently empty for these threads, by
    design and not as a gap waiting to fill; build_thread_context reports that
    as prefire_state="not_built_here" rather than a bare empty list.

    The descriptor is NOT downgraded to None to signal that: the payload's
    other three sections — budget, rolling summary, history preview — are
    scoped by this same descriptor and DO have peer data to find
    (memory_engine._turn_row_where:1083 matches peer turn rows and the legacy
    `synaptive:<actor>:%` source_session shape). Passing None would drop them
    onto the session_id fallback (_turn_row_where:1104) and break three
    working sections to annotate a fourth."""
    if kind == "self":
        # Through circles_threads.descriptor_for → derive_source_context, so
        # this reader collapses a thread id exactly the way the turn writer
        # does.
        return _ct.descriptor_for(thread_id)
    if kind in ("dm", "entity"):
        return {"kind": "peer", "ref": str(thread_id)}
    return None


_UNSET = object()  # "this field was not in the request" — distinct from null


async def _persist_thread_budget(thread_key: str, budget=_UNSET,
                                 allowance=_UNSET) -> None:
    """Persist (or clear, None) the per-thread context budget and per-turn
    spend allowance, merged into the thread's circles_thread_models entry so a
    model switch, a budget change and an allowance change never clobber each
    other.

    Both fields default to the _UNSET sentinel rather than to None because
    None is a REAL value here — it means "clear this one". A caller that sends
    only an allowance must leave the depth dial exactly as it was, and a None
    default could not express the difference."""
    pool = brain_service.get_pool()
    eid = _entity_id()
    if pool is None or eid is None:
        return
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid
        )
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            bp = json.loads(bp)
        if not isinstance(bp, dict):
            bp = {}
        thread_models = dict(bp.get("circles_thread_models") or {})
        entry = dict(thread_models.get(thread_key) or {})
        if budget is not _UNSET:
            if budget is None:
                entry.pop("context_budget", None)
            else:
                entry["context_budget"] = budget
        if allowance is not _UNSET:
            if allowance is None:
                entry.pop("turn_allowance_usd", None)
            else:
                entry["turn_allowance_usd"] = float(allowance)
        if entry:
            thread_models[thread_key] = entry
        else:
            thread_models.pop(thread_key, None)
        bp["circles_thread_models"] = thread_models
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params=$2::jsonb WHERE id=$1",
            eid, json.dumps(bp),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("persist thread budget failed (entity %s, %s): %s", eid, thread_key, exc)


@router.get("/thread-context/{kind}/{thread_id}", dependencies=_READ)
async def get_thread_context(kind: str, thread_id: str) -> dict:
    """What the next turn on this thread will fire with: window, budget,
    prompt-block composition, history preview, rolling-summary state, and
    last turn's prefired memories (same payload as the workbench's
    /api/brain/thread-context)."""
    if kind not in {"self", "dm", "entity", "circle"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown thread kind")
    if kind == "self" and _ct.is_legacy(thread_id):
        # There is no original conversation: the legacy
        # id names no topic, so it has no context to show, and the app:circles
        # rows it used to map to are never read back here.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such topic")
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "pool not ready")
    from mastercode.context_budget import build_thread_context
    # No per-thread model pick is persisted any more (see the per-thread
    # model selection block above): the window is the Orchestrator's chat route; the picker's
    # live pick, when any, is resolved client-side from the catalog
    # (circles-web conversation-depth.tsx). Only the thread's budget rides.
    tk = _thread_key(kind, thread_id)
    payload = await build_thread_context(
        pool, _entity_id() or 0, _thread_src_ctx(kind, thread_id),
        thread_override=await _thread_budget_override(tk),
        # This IS the circles surface, so legacy circles_thread_models
        # entries for a dm/entity thread are the person's own setting and
        # are shown. Nothing else passes this, so the turn path can never
        # pick one up for a same-ref peer on another lane
        # (context_budget._circles_entry_value).
        surface="circles",
    )
    if kind == "self":
        # The knowledge table of contents rides the same payload the
        # View Context panel already renders. knowledge_toc is built beside
        # this change; the import and the call are both wrapped so a missing
        # module, or a failure inside it, degrades to toc=null instead of
        # turning the whole context panel into a 500.
        toc = None
        try:
            from mastercode.knowledge_toc import build_toc
            toc = await build_toc(pool, _entity_id() or 0, thread_id, session_id=None)
        except ImportError:
            log.debug("knowledge_toc not available; thread-context toc omitted")
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge toc build failed (thread %s): %s",
                        thread_id, exc)
        payload["toc"] = toc
    return payload


@router.put("/thread-context/{kind}/{thread_id}", dependencies=_WRITE)
async def put_thread_context(kind: str, thread_id: str, request: Request) -> dict:
    """Set (or clear with null) the per-thread context budget
    ({"budget": ...}) and the per-turn spend allowance
    ({"turn_allowance_usd": ...}).

    Keyed on PRESENCE, matching /api/brain/thread-context: a body carrying
    only one field leaves the other untouched, and null clears. The panel
    saves the two dials independently, so a value-keyed write would have each
    save wipe the other."""
    if kind not in {"self", "dm", "entity", "circle"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown thread kind")
    # ONLY THE SELF-THREAD RUNS A TURN ON THIS HOST, so only the self-thread
    # has anything for these two dials to govern. `send_message` (:1407) takes
    # the local streaming path for target_kind == "self" and nothing else;
    # dm / entity / circle are routed to the other side, where that host's own
    # settings apply. Until now a PUT on those kinds returned {"ok": true} and
    # wrote circles_thread_models["dm:<id>"], which no reader ever consulted —
    # the person set a budget, was told it saved, and it governed nothing.
    # Refusing in plain words is the honest answer; entries already written
    # that way are still read (context_budget._circles_entry_value) rather
    # than being stranded.
    if kind != "self":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only this thread with your own host can carry these settings. "
            f"A {kind} conversation runs on the other host, so its context "
            "depth and per-turn budget are set there, not here.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    budget = body.get("budget", None)
    if budget is not None:
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "budget must be an int or null") from None
        if budget <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "budget must be positive")
    allowance = body.get("turn_allowance_usd", None)
    if allowance is not None:
        try:
            allowance = float(allowance)
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "turn_allowance_usd must be a number or null") from None
        # Zero or less cannot pay for the first model call, so it can only
        # mean "refuse to answer" — never what a budget means. See the twin
        # check in routes/brain.py.
        if allowance <= 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "turn_allowance_usd must be positive (send null to clear it)")
    tk = _thread_key(kind, thread_id)
    await _persist_thread_budget(
        tk,
        budget if "budget" in body else _UNSET,
        allowance if "turn_allowance_usd" in body else _UNSET,
    )
    return {"ok": True, "thread": tk, "budget": budget,
            "turn_allowance_usd": allowance}


# ---- Send message ----

@router.post("/messages", dependencies=_WRITE)
async def send_message(request: Request) -> dict:
    """Persist + deliver a message. For self/entity-to-self the message
    is local; for DMs/entity/circle the bridge routes (and once the
    container-to-container mTLS protocol lands, entity-to-entity hops
    direct over the DMZ instead of round-tripping the bridge).

    Every attempt gets a UUID `request_id` for tracing in chat_events.
    Errors return a structured envelope:
        4xx/5xx, body = {"ok": false,
                         "error": {"code", "message", "request_id"}}
    Success: 200, body = {"ok": true, "session_id", "messages", "request_id"}.
    """
    request_id = str(uuid.uuid4())

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "bad_json", "message": "Body must be JSON.", "request_id": request_id}},
        ) from None

    target_kind = body.get("target_kind")
    target_id = body.get("target_id")
    text = (body.get("body") or "").strip()
    # The sender's id for the words (self only): the page's id for words it
    # nudged and kept a copy of in PII mode, sent as the next message (its
    # deliverKept). A second send under it delivers nothing new: it follows
    # the turn that has the words (brain_service.send_message). An opaque
    # tag the page makes up, never its words.
    from mastercode.runtime.session import nudge_id_ok
    _send_client_id = body.get("client_id")
    if _send_client_id is not None and not nudge_id_ok(_send_client_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "bad_client_id",
                                           "message": "client_id: 1-64 of A-Z a-z 0-9 _ -",
                                           "request_id": request_id}},
        )

    # Optional per-thread model pick. May be a
    # {provider, model_id} dict or a bare model-id string. Threaded into the chat
    # resolution for THIS turn AND persisted so the thread keeps the choice.
    # Any Keys-registry provider or 'local' passes provider_allowed_for_role for
    # chat; SDK-lane aliases and unknown providers are refused.
    _override = _normalize_thread_override(body.get("model"))
    _override_refused = bool(_override) and not _chat_override_allowed(_override)

    # Attachments: Workbench-format content blocks, checked by
    # _check_attachments (no count cap of its own; one
    # file's size is the Host's one bound for a file it holds).
    attachments = body.get("attachments") or []
    _check_attachments(attachments, request_id)

    # The FRAME (host prompt map phase 4): what the front end knows about
    # this turn — the pills the composer attached from an app (the Artifacts
    # window's tune-mode references) and the open view. Validated to those
    # keys (runtime.frame.validate_frame); honoured on the self-thread only,
    # where the host answers.
    from mastercode.runtime.frame import validate_frame
    frame = validate_frame(body.get("frame"))

    # ASK IVY (server/circles_reading.py): one message of a
    # person's chat, handed to this turn of your own Host. The server reads
    # it itself below; the body names it only by {kind, ref, message_id}.
    carry_raw = body.get("carry")
    if not text and attachments:
        # Attachment-only send: give the turn row + model a stub body so
        # the write path (which skips empty content) keeps the turn.
        text = "(sent an attachment)" if len(attachments) == 1 else f"(sent {len(attachments)} attachments)"
    # A carried message is something to send on its own: the quote is the
    # turn's words then (it is prepended below, on your own Host's page only).
    if not text and not (carry_raw is not None and target_kind == "self"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "empty_body", "message": "Empty message body.", "request_id": request_id}},
        )
    if target_kind not in {"self", "dm", "entity", "circle"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "unknown_target_kind", "message": f"Unknown target kind: {target_kind!r}.", "request_id": request_id}},
        )
    # Topics are a conversation with another Host's alone:
    # your own Host's pages are its threads (`thread_id`), a person is one
    # continuous chat, and a room is the platform's.
    if target_kind != "entity" and (body.get("topic_id") is not None
                                    or body.get("new_topic") is not None):
        raise _err_envelope(
            request_id, "topic_not_here",
            "Topics are for a conversation with another Host. Your own Host has "
            "threads, and a person is one continuous chat.",
            status.HTTP_400_BAD_REQUEST)

    if carry_raw is not None and target_kind != "self":
        raise _err_envelope(
            request_id, "carry_not_here",
            "A message from a person's chat is given to your own Host only. "
            "Nothing is sent to anyone else.",
            status.HTTP_400_BAD_REQUEST)

    eid = _entity_id() or 0
    await _audit(
        request_id=request_id, entity_id=eid, session_id=None, target_kind=target_kind,
        phase="received", status_="ok", text_preview=text[:120],
    )

    if target_kind == "self":
        # Self-thread chats
        # always stream now via the AI SDK Data Stream Protocol. The
        # legacy request/response JSON path (_send_self_message) was
        # removed; the Accept fork is gone. Every client must parse
        # f:/0:/9:/a:/d:/e:/3: frames. Workbench + Circles clients
        # already do. The smoke test + A/B benchmark were updated in
        # this same ship.
        from fastapi.responses import StreamingResponse

        # The per-thread EFFORT pick (beside the model picker), for THIS turn
        # only, like the model pick: never persisted, sent while the picker
        # shows it. Checked before anything of the turn happens, against the
        # model the turn runs on (the pick when it stands, else the chat
        # route), by the Orchestrator's own rule (inference.
        # check_thread_effort): a level the wire would drop is refused with
        # the reason instead of the turn running at another one.
        _effort_pick = str(body.get("effort") or "").strip()
        if _effort_pick:
            from server.routes.inference import check_thread_effort
            _ov = _override if (_override and not _override_refused) else {}
            _eff_err = await check_thread_effort(
                _effort_pick, _ov.get("provider"), _ov.get("model_id"))
            if _eff_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"ok": False, "error": {"code": "effort_refused", "message": _eff_err,
                                                   "request_id": request_id}},
                )

        # The model for THIS turn is exactly what the picker sent.
        # An explicit pick that passes the guard is used for this turn only;
        # a refused pick (an SDK-lane alias or unknown provider — the only
        # refusals post-cut) is ignored — the Orchestrator's chat route runs
        # AND the refusal is surfaced via an x-thread-model-refused header so
        # the picker can show a clean message. No pick → the Orchestrator's
        # chat route (inference_config.resolve_role_route precedence 2+),
        # never a pick remembered from an earlier turn. The thread's
        # persisted context_budget still rides on thread_override; a stale
        # older model pick found on the entry is scrubbed here.
        #
        # The carried message (Ask Ivy), read by the server, only from a chat
        # the operator asking is a party to; refused before anything of the
        # turn happens. Nothing here sends anything to the other side.
        _carry_quote = _carry_meta = None
        if carry_raw is not None:
            _carry_quote, _carry_meta = await _carry_for_turn(carry_raw, eid, request, request_id)

        # WHICH THREAD THIS TURN BELONGS TO. `thread_id` rides the body the
        # same way the FRAME field does — one body, one parse, one forward.
        # One conversation: `pick_topic` asks the Host which topic
        # the message belongs to (thread_pick.pick_topic); the thread the body
        # names is only the topic the person was last in, so a closed one is
        # no refusal. `topic_picked`: the page asked first (POST /topic, a
        # turn was running) and names the topic it was given. Either way the
        # turn's frame names the topic and the related ones the search found,
        # and the turn-type scorer does not move it again. Never with a
        # carried message or a turn-type pick: those say where they go.
        _one_conv = (carry_raw is None and not body.get("turn_type_id")
                     and (body.get("pick_topic") is True or body.get("topic_picked") is True))
        _thread_id = await _thread_id_from_body(body, eid, request_id,
                                                require_open=not _one_conv)
        _topic = None
        if _one_conv and body.get("pick_topic") is True:
            _topic = await _topic_for(eid, _thread_id, text, _topic_known(body))
            _thread_id = _topic.thread_id
        elif _one_conv:
            from server.connectors import thread_pick as _tp1
            from mastercode.runtime.root_turn import is_root_thread as _is_root
            try:
                if _thread_id and await _is_root(brain_service.get_pool(), eid, _thread_id):
                    raise LookupError("the root thread reads no other topic")
                _topic = _tp1.TopicLanding(
                    thread_id=_thread_id, title="", picked=True,
                    related=await _tp1.related_topics(brain_service.get_pool(), eid, text,
                                                      thread_id=_thread_id),
                    # The topics POST /topic left for the Host's pick in its
                    # turn, kept on the new topic's marker.
                    choice=tuple(await _tp1.pending_choice(brain_service.get_pool(), eid, _thread_id)))
            except Exception as _rel_err:  # noqa: BLE001 — the turn runs without them
                log.warning("related topics not read (non-fatal): %s", _rel_err)
        if _topic is not None and not _topic.root:
            frame = {**(frame or {}), "topic": {
                "title": _topic.title, "new": _topic.new, "marker": _topic.marker,
                "related": list(_topic.related)}}
            frame = _with_choice(frame, _topic)

        # Turn types:
        # WHERE this message lands is decided here, before the turn, in
        # software — a dropdown pick (`turn_type_id`), else the thread's
        # binding, else the scorer over the active types, else it stays. A
        # message that lands in another type's thread reports it in the
        # response headers so the view can follow. A failure holds
        # the message where it is; a send never fails for the classifier.
        _landed_type = None
        try:
            from mastercode.runtime.turn_types import resolve_thread_for_message as _tt_resolve
            # The Host already picked this message's topic: it stays there.
            _landing = {} if (_topic is not None and _topic.picked) else await _tt_resolve(
                brain_service.get_pool(), eid, _thread_id, text,
                selected_type_id=(str(body.get("turn_type_id")).strip()
                                  if body.get("turn_type_id") else None))
            if _landing.get("moved") and _landing.get("thread_id"):
                _thread_id = _landing["thread_id"]
            _landed_type = _landing.get("type")
        except Exception as _tt_err:  # noqa: BLE001
            log.warning("turn type landing failed (message held): %s", _tt_err)

        # Every message lands in a topic: there is no original conversation.
        # One that names none by now (a blank page's
        # send with no pick, a carried message, a turn-type pick with no
        # thread bound, a pick that failed) is picked as any message is, from
        # the person's own words (the carried quote is added below), else
        # starts a topic. x-landed-thread then names it and the page follows.
        if not _thread_id:
            _topic = await _topic_for(eid, None, text, False)
            _thread_id = _topic.thread_id or await _ct.home_thread(
                brain_service.get_pool(), eid, None, title=text)
            if _topic.picked and not _topic.root:
                frame = {**(frame or {}), "topic": {
                    "title": _topic.title, "new": _topic.new, "marker": _topic.marker,
                    "related": list(_topic.related)}}
                frame = _with_choice(frame, _topic)

        # The quote goes into the turn's words AFTER the landing: where the
        # message lands is the person's call (their pick, else their words),
        # never the carried text's, which is someone else's. The turn's user
        # row keeps the words with the quote, so a later turn still has it,
        # and records the carry itself from the frame (session.py `carry`).
        if _carry_quote is not None:
            text = _carry_quote + (f"\n\n{text}" if text else "")
            frame = {**(frame or {}), "carry": _carry_meta}

        tk = _thread_key("self", _thread_id)
        _entry = await _thread_entry(tk)
        if _entry and ("model_id" in _entry or "provider" in _entry):
            await _scrub_thread_model(tk)
        _effective_override: dict | None = None
        if _override and not _override_refused:
            _effective_override = dict(_override)
        if _entry and _entry.get("context_budget"):
            _effective_override = {**(_effective_override or {}),
                                   "context_budget": _entry["context_budget"]}
        # The per-turn spend allowance rides the same forward as the depth
        # dial. session.py reads thread_override first and only falls back to
        # the DB (context_budget.read_thread_allowance), so a send that left
        # this out would still work — it would just re-read the same row. It
        # is forwarded anyway so the send path and the runtime never disagree
        # about what the person set for THIS turn.
        if _entry and _entry.get("turn_allowance_usd"):
            _effective_override = {**(_effective_override or {}),
                                   "turn_allowance_usd": _entry["turn_allowance_usd"]}
        if _effort_pick:
            _effective_override = {**(_effective_override or {}), "effort": _effort_pick}

        _hdrs = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-data-stream": "v1",
        }
        if _override_refused:
            _hdrs["x-thread-model-refused"] = "byo_not_allowed_for_chat"
        # Where the message landed — the client compares it with the thread
        # it sent into and moves the view when they differ. Always
        # sent for a self send, so "stayed" and "moved" read the same way.
        _hdrs["x-landed-thread"] = str(_thread_id or "")
        # The Host's topic pick (one conversation): the line the feed shows
        # for a switch, and the topic's title, URL-quoted (a header carries
        # latin-1 only and a title is anything the Host wrote).
        if _topic is not None and _topic.picked:
            from urllib.parse import quote as _q
            _hdrs["x-topic-title"] = _q(_topic.title or "", safe="")[:600]
            _hdrs["x-topic-marker"] = _q(_topic.marker or "", safe="")[:800]
            _hdrs["x-topic-new"] = "1" if _topic.new else "0"
        # The server's clock at landing: every row of the landed thread saved
        # at or after it is THIS turn — how the view tells this turn from an
        # earlier message with the same text.
        from datetime import datetime as _dt, timezone as _tz
        _hdrs["x-landed-at"] = _dt.now(_tz.utc).isoformat()
        if _landed_type:
            _hdrs["x-landed-type"] = str(_landed_type.get("id") or "")
            # ASCII-safe and one line: a header cannot carry every name a
            # person may type, and a CR/LF in it would be an invalid header.
            import re as _re
            _hdrs["x-landed-type-name"] = _re.sub(
                r"[\x00-\x1f\x7f]+", " ",
                str(_landed_type.get("name") or "").encode("ascii", "replace").decode("ascii"),
            ).strip()[:120]

        # A file dropped into the chat is also KEPT on this Host, the same as
        # an upload (files a person uploads to a Host belong on that Host and
        # are processed there): stored,
        # read by the host's own readers, embedded, so a later turn can open
        # it again. In the background — the turn never waits on it, and a
        # failed store never fails the send.
        if attachments:
            _keep_attachments_on_host(eid, attachments)

        return StreamingResponse(
            brain_service.send_message(
                entity_id=eid,
                message=text,
                attachments=attachments or None,
                app_id="circles",
                accept="text/x-vercel-ai-data-stream",
                thread_override=_effective_override,
                frame=frame,
                # None for the original conversation — app_id='circles' alone
                # already scopes it, and a sentinel would be stamped onto the
                # turn rows as a thread ref that has no row.
                thread_id=_thread_id,
                **({"client_id": _send_client_id} if _send_client_id else {}),
                # Whether the caller is the person (the Circles or Workbench
                # app token): the root thread takes only their words.
                from_person=_root_person(request),
            ),
            media_type="text/event-stream",
            headers=_hdrs,
        )

    # Why the DIRECT peer lane (:8444) did or didn't carry this message. Ends up
    # in the failure envelope so a dead host↔host send names the lane that refused
    # it, not just "the bridge". See _try_direct_peer_send.
    #
    # Bound HERE, above every fork, because the failure path below is SHARED by
    # all of them. A placeholder set inside one branch leaves the name unbound on
    # the others and Python raises UnboundLocalError on the shared `if not data:`
    # — turning this route's 502 envelope into a 500 with no request_id and no
    # audit row. That is precisely what an earlier revision of this change did to
    # target_kind='circle'; the runtime half of
    # scripts/test_circles_direct_lane_reason.py now drives every kind through
    # the failure path so it cannot come back.
    direct_lane = "not_attempted"
    # The topic an 'entity' send went into, for the response: {id, title}.
    sent_topic: dict | None = None

    if target_kind == "circle":
        # A circle message is fanned out by the bridge; there is no direct
        # host↔host lane to attempt.
        direct_lane = "not_attempted:circle"
        # The op the bridge actually registers (adapter op_circles_message,
        # params {circle_id, content}). circle_send_message never existed.
        # int coercion: asyncpg binds strictly against the INT column.
        try:
            circle_target = int(target_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"ok": False, "error": {"code": "bad_target", "message": f"Bad circle id: {target_id!r}.", "request_id": request_id}},
            )
        data = await _bridge_call("circles.message", {
            "circle_id": circle_target,
            "content": text,
        })
    else:
        # WHO IS TYPING (Hosts | People). Everything sent from this
        # composer is written by the PERSON at the keyboard, never by the host —
        # so the message carries their verified email (the router-injected
        # X-Authentik-Email; server/operator_identity.py). The receiving side
        # files a person → host message under Hosts › Peers with the person
        # named "via" this host, and a person → person message under People.
        # The bridge only relays the email after checking it is an operator of
        # THIS host, which is why a human-stamped send never takes the direct
        # peer lane: that lane verifies the host, not the person.
        from server.operator_identity import request_email
        from server.routes.contacts import find_human_contact
        from server.routes.messages import dm_thread_id
        sender_email = request_email(request.headers)
        data = None
        # ONE message id, carried down BOTH transports. send_to_peer raises
        # PeerUnavailable on the READLINE (peer_client.py), i.e. after the receiver
        # has already committed the row — so a timed-out-but-delivered direct send
        # falls through to the bridge and would land a second copy. Minting the id
        # HERE, above the fork, is what lets handle_inbox_append dedupe it: it
        # dedupes on a supplied id and only on a supplied id. Same shape
        # contacts.request_peer_service already uses, for the same reason.
        #
        # The bridge half of this needs op_circles_send_entity_dm to forward
        # message_id into its recip_payload — it does not today, so the id is sent
        # and ignored until that ships; the direct-lane retry case it covers
        # is live now.
        message_id = str(uuid.uuid4())
        # Everything past here goes to ANOTHER host, whose inbox takes at most
        # CROSS_HOST_BODY_MAX in one message and refuses more in words.
        # Said here, before it travels, in the receiver's words.
        from server.bridge.memory_handlers import cross_host_body_refusal
        _too_big = cross_host_body_refusal(text)
        if _too_big:
            raise _err_envelope(request_id, "message_too_large", _too_big,
                                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if target_kind == "dm":
            # A person, keyed by email. Their HOST is on the contact row (v123),
            # written when the connection was accepted either way round.
            pool = brain_service.get_pool()
            contact = await find_human_contact(pool, str(target_id)) if pool is not None else None
            if contact is None or contact["peer_entity_id"] is None:
                raise _err_envelope(
                    request_id, "not_connected",
                    "You're not connected with this person yet — send them a "
                    "connection request from Contacts and wait for them to accept.",
                    status.HTTP_409_CONFLICT,
                )
            if contact["status"] == "blocked":
                raise _err_envelope(
                    request_id, "blocked", "You've blocked this person.",
                    status.HTTP_409_CONFLICT,
                )
            if not sender_email:
                raise _err_envelope(
                    request_id, "no_identity",
                    "No verified identity on this request — reload and sign in again.",
                    status.HTTP_403_FORBIDDEN,
                )
            peer_host = int(contact["peer_entity_id"])
            call_params = {
                "target_id": peer_host,
                "to_user_email": str(target_id).strip().lower(),
                "from_user_email": sender_email,
                "thread_id": people_thread_id(sender_email, str(target_id)),
                "message_id": message_id,
                "body": text,
                "payload": {"peer_user_name": contact["display_name"]},
            }
            held = await _held_slip_for(peer_host)
            if held:
                call_params["slip"] = held
                # The direct lane now carries the person too. The old
                # comment here said it could not, because that lane verifies the
                # HOST and this send names a human — true while the PLATFORM was
                # the authority on who a host's people are. That authority now
                # sits with the host: we assert our own operator
                # (sender_email is the router-injected, already-verified header),
                # the receiver checks its own, and neither needs the bridge.
                #
                # No from_user_name on this lane, deliberately. The bridge got the
                # sender's display name from users.name in the platform DB
                # (adapter.py:4920-4922) and the pod has no such row for its own
                # operator — find_human_contact returns the PEER's contact row, so
                # its display_name is the recipient's name, not ours. Sending it
                # would label our message with their name. The reader already
                # degrades correctly: author falls back to from_user_email
                # (circles.py _people_thread), so the person is still named.
                data, direct_lane = await _try_direct_peer_send(
                    eid, peer_host, text, held,
                    thread_id=call_params["thread_id"],
                    from_user_email=sender_email,
                    to_user_email=call_params["to_user_email"],
                    own_payload=call_params["payload"],
                    message_id=message_id)
            else:
                direct_lane = "not_attempted:no_slip_held"
            if data is None:
                data = await _bridge_call("circles.send_dm", call_params)
        else:
            try:
                peer_host_id = int(target_id)
            except (TypeError, ValueError):
                raise _err_envelope(
                    request_id, "bad_target", f"Bad host id: {target_id!r}.",
                    status.HTTP_400_BAD_REQUEST,
                ) from None
            # WHICH TOPIC (server/peer/topics.py). It travels in
            # the nested payload on both lanes, beside a thread id that stays
            # the pair's: the receiver derives that from who is on the message
            # and files the topic inside it. The original conversation sends no
            # payload at all, exactly as before topics.
            sent_topic = await _topic_for_send(body, eid, peer_host_id, request_id)
            from server.peer import topics as _topics
            topic_wire = _topics.wire(sent_topic["id"], sent_topic["title"])
            call_params = {
                "target_id": peer_host_id,
                "body": text,
                "thread_id": dm_thread_id(eid, peer_host_id),
                "message_id": message_id,
            }
            if topic_wire:
                # The relay copies this to the receiver and to our own copy
                # (bridge adapter.py op_circles_send_entity_dm), and removes
                # only the keys the bridge stamps (_BRIDGE_STAMPED_INNER).
                call_params["payload"] = dict(topic_wire)
            if sender_email:
                call_params["from_user_email"] = sender_email
            # Present the slip the peer issued us (if any) so the receiver honors
            # it cryptographically instead of relying on its domain_policy
            # allow-list. No slip → the send still works for an accepted contact
            # via domain_policy.
            held = await _held_slip_for(target_id)
            if held:
                call_params["slip"] = held
                # Workstream B: the DIRECT peer lane (:8444) — the peer verifies
                # the slip offline, no bridge in the data path. None → bridge relay.
                #
                # The `not sender_email` guard that used to stand here is
                # gone. It kept every human-authored send on the bridge because the
                # platform was the authority on whether sender_email really is one
                # of our operators. That authority now sits with the host, so
                # the name now travels with the send and the
                # receiver takes it as OUR claim about OUR person — which is what
                # the mTLS cert already makes it.
                data, direct_lane = await _try_direct_peer_send(
                    eid, int(target_id), text, held,
                    thread_id=call_params["thread_id"],
                    from_user_email=sender_email or None,
                    message_id=message_id, topic=topic_wire or None)
            else:
                direct_lane = "not_attempted:no_slip_held"
            if data is None:
                data = await _bridge_call("circles.send_entity_dm", call_params)
    if not data:
        # BOTH lanes are now accounted for in one line and in the envelope: the
        # direct lane's reason (or why it wasn't tried) beside the relay's failure.
        # Without this the relay's 502 was the only visible fact and the direct
        # lane's refusal was invisible, which is what made a dead host↔host
        # message un-diagnosable from the outside.
        log.warning("host↔host send failed (entity %s → %s, kind=%s): relay "
                    "unavailable, direct lane %s [request_id=%s]",
                    eid, target_id, target_kind, direct_lane, request_id)
        await _audit(
            request_id=request_id, entity_id=eid, session_id=None, target_kind=target_kind,
            phase="returned", status_="error",
            error_class="bridge_delivery_unavailable",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"ok": False, "error": {
                "code": "bridge_delivery_unavailable",
                "message": "Bridge couldn't deliver this message. Try again in a moment.",
                "request_id": request_id,
                "direct_lane": direct_lane,
            }},
        )

    await _audit(
        request_id=request_id, entity_id=eid, session_id=None, target_kind=target_kind,
        phase="returned", status_="ok",
    )
    out = {"ok": True, "request_id": request_id, **data}
    if sent_topic is not None:
        out["topic"] = sent_topic
    return out


async def _carry_for_turn(raw, eid: int, request: Request, request_id: str) -> tuple[str, dict]:
    """(the quote for the turn, the carry its user row records) for a send to
    your own Host that carries a message of a person's chat (Ask Ivy;
    server/circles_reading.py). The message is read here, by id, from the
    chat between the operator asking and the person `ref` names, and nowhere
    else; anything else is the error envelope (400 malformed, 403 no
    identity, 404 not in your chat with that person)."""
    from server import circles_reading as _reading
    from server.operator_identity import request_email
    from server.routes.contacts import find_human_contact
    pool = brain_service.get_pool()
    if pool is None:
        raise _err_envelope(request_id, "not_ready", "Your Host is starting. Try again in a moment.",
                            status.HTTP_503_SERVICE_UNAVAILABLE)
    operator = request_email(request.headers)
    try:
        carry = _reading.parse_carry(raw)
        row = await _reading.carried_message(pool, eid, operator, carry)
    except _reading.CarryError as e:
        raise _err_envelope(request_id, e.code, e.detail, e.status) from None
    if row["from_user_email"] == operator:
        who = f"your person ({operator})"
    else:
        contact = await find_human_contact(pool, carry["ref"])
        name = (contact["display_name"] if contact is not None else None) or ""
        who = f"{name} ({carry['ref']})" if name.strip() else carry["ref"]
    return _reading.fence(who, row["ts"], row["body"]), carry


async def _topic_for_send(body: dict, eid: int, peer: int, request_id: str) -> dict:
    """The topic a send to another Host goes into
    (server/peer/topics.py), as {id, title}:

      neither key             the original conversation
      topic_id "original"     the original conversation
      topic_id "<uuid>"       a topic this conversation already has; the title
                              is the topic's own, never the caller's
      new_topic "<title>"     a new topic, the "+": its id is minted here, and
                              the topic exists once this message is stored

    Raises the error envelope: 400 for both keys at once, an id that is not
    one, or a new title with nothing in it; 404 for a topic this conversation
    does not have (a topic of another Host's conversation is not one)."""
    from server.peer import topics as _topics
    raw_id, raw_new = body.get("topic_id"), body.get("new_topic")
    if raw_id is not None and raw_new is not None:
        raise _err_envelope(request_id, "bad_topic",
                            "Name a topic or start a new one, not both.",
                            status.HTTP_400_BAD_REQUEST)
    if raw_new is not None:
        title = _topics.clean_title(raw_new)
        if title is None:
            raise _err_envelope(request_id, "bad_topic", "A new topic needs a title.",
                                status.HTTP_400_BAD_REQUEST)
        return {"id": str(uuid.uuid4()), "title": title}
    tid = _topics.parse(raw_id) if raw_id is None or isinstance(raw_id, str) else None
    if tid is None:
        raise _err_envelope(request_id, "bad_topic",
                            "topic_id is \"original\" or a topic's id.",
                            status.HTTP_400_BAD_REQUEST)
    if tid == _topics.ORIGINAL:
        return {"id": _topics.ORIGINAL, "title": _topics.ORIGINAL_TITLE}
    pool = brain_service.get_pool()
    if pool is None:
        raise _err_envelope(request_id, "not_ready", "Your Host is starting. Try again in a moment.",
                            status.HTTP_503_SERVICE_UNAVAILABLE)
    exists, title = await _topics.title_of(pool, eid, peer, tid)
    if not exists:
        raise _err_envelope(request_id, "no_such_topic", "There is no such topic with this Host.",
                            status.HTTP_404_NOT_FOUND)
    return {"id": tid, "title": title}


@router.post("/nudge", dependencies=_WRITE)
async def nudge(request: Request) -> dict:
    """Real mid-stream influence, NOT Claude's /btw
    side-chat pattern.

    Body: {session_id, text, client_id?} — client_id is the sender's id for
    the words (1-64 of A-Z a-z 0-9 _ -), echoed on the turn's nudge_read
    receipt and the saved row.

    Behavior:
      - Writes nudge text into a per-session pending-nudge queue
        (mastercode.runtime.session._pending_nudges).
      - Returns immediately. The running tool loop checks the queue
        at the next round boundary; if non-empty it:
          * appends each nudge as a user-role priority message
          * resets tool_round counter so the entity gets a fresh
            budget to respond to the new direction
          * skips any pending tool-result handling — the nudge IS
            the new context
      - Background work (build, dispatched agents) continues
        independently — only the entity's CURRENT tool loop sees
        the nudge.

    A turn that has STOPPED READING (session.turn_stopped_reading — it
    released its holds at its exit) never drains its queue
    again, so the text starts a turn of its own in that turn's room once
    it is done (`own_turn: true`), or the route answers 409 and the client
    sends it as a message. Returns 400 on missing session_id or empty text.

    The server owns the words from here: they are written to
    the Host's database (pending_words) BEFORE this answers, and from then
    on a turn reads them, or they start the thread's next turn when the
    turn ends without reading them, or — after a restart — when the Host
    is back. The page keeps no copy to send again; it reads what is still
    held (GET /api/circles/pending) to show it. A write that fails answers
    503 and the page keeps its words. Observed before this change: words
    typed here vanished when the turn ended, because the only copy left was
    the page's, which a reload loses. In PII mode nothing is written: the
    answer says `held: false`, the turn still gets the words, and the page
    keeps its copy until the turn's nudge_read receipt, sending it as the
    next message when none comes.
    """
    request_id = str(uuid.uuid4())
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "bad_json", "message": "Body must be JSON.", "request_id": request_id}},
        ) from None
    session_id = (body.get("session_id") or "").strip()
    text = (body.get("text") or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "session_id_required", "message": "session_id required.", "request_id": request_id}},
        )
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "empty_text", "message": "Empty nudge text.", "request_id": request_id}},
        )
    # Push into the session's pending-nudge queue. Import here (not at
    # module load) because session.py is heavy.
    from mastercode.runtime.session import (
        answer_after_turn, nudge_id_ok, nudge_words, push_nudge, turn_stopped_reading)
    from mastercode import pending_words as _pw
    from server.turn_registry import get_turn_registry
    # The sender's id for these words (optional), as the Workbench's nudge
    # route takes it (routes/brain.py brain_turn_nudge): the turn's nudge_read
    # receipt and the saved row carry it (session.push_nudge), so the client
    # matches its copy by it, never by the words' text, which the platform may
    # have cut (secret shapes, the frame's 500 characters).
    client_id = body.get("client_id") if isinstance(body, dict) else None
    if client_id is not None and not nudge_id_ok(client_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "bad_client_id",
                                           "message": "client_id: 1-64 of A-Z a-z 0-9 _ -",
                                           "request_id": request_id}},
        )
    # Sent by the Circles page, as its send route names it (app_id="circles"):
    # the id says the words were delivered for Circles only
    # (pending_words.SentId).
    if client_id is not None:
        client_id = _pw.sent_ids(client_id, "circles")
    eid = _entity_id() or 0
    # Held before anything else answers: the words as the turn reads them
    # (secret shapes cut, session.nudge_words), in the room of the turn they
    # were typed into, so a turn of their own can run there if that turn
    # never reads them (end_turn_input) or the Host restarts first.
    _rec = get_turn_registry().by_session(session_id)
    # A running root turn takes only the person's own words
    # (runtime/root_turn.words_refusal): this route's caller must be the
    # Circles or Workbench app. Refused before anything is held, so no
    # pending_words row carries words the root thread will not take.
    if _rec is not None and isinstance(_rec.room, dict) and _rec.room.get("thread_id"):
        from mastercode.runtime import root_turn as _rt
        _why = await _rt.words_refusal(brain_service.get_pool(), eid or _rec.entity_id,
                                       _rec.room["thread_id"],
                                       person=_rt.person_request(request))
        if _why:
            await _audit(
                request_id=request_id, entity_id=eid, session_id=session_id,
                target_kind="self", phase="nudge_queued", status_="error",
                text_preview=text[:120],
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"ok": False, "error": {"code": "root_thread_person_only",
                                               "message": _why, "request_id": request_id}},
            )
    _room = (dict(_rec.room) if _rec is not None and isinstance(_rec.room, dict)
             else {"app_id": "circles", "project_id": None, "thread_id": None, "history_context": None})
    _key = None
    try:
        _key = await _pw.hold(
            brain_service.get_pool(), eid, session_id, nudge_words(text), client_id=client_id,
            room=_room, thread_key=(_rec.thread_key if _rec is not None else None))
    except Exception as e:  # noqa: BLE001
        log.error("nudge: the words could not be held (session %s): %s", session_id, e)
        await _audit(
            request_id=request_id, entity_id=eid, session_id=session_id,
            target_kind="self", phase="nudge_queued", status_="error",
            text_preview=text[:120], error_class="pending_words", error_detail=str(e)[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"ok": False, "error": {
                "code": "not_held",
                "message": "Your words could not be saved on your Host; send them again as a message.",
                "request_id": request_id}},
        ) from None
    # A turn that has stopped reading (it released its holds at
    # its exit) never drains its queue again: the words start a turn of their
    # own in the room that turn ran in, once it is done — the way an answer
    # to a card does after the asking turn ended (answer_after_turn). The
    # room is the one the turn registry recorded for that session. That turn
    # carries the person's words WHOLE: the client, which holds them until a
    # read receipt that never comes, sends the same text as its next message,
    # and the registry reattaches it to this turn only if it is the same text
    # (a cut one was a second, duplicate turn). A nudge into a
    # running turn is carried whole too: it was
    # cut to 2,000 characters with nothing said, and the person's words are
    # the input. The bound on them is the turn's context window, which the
    # turn's own history fitting already handles.
    own_turn = None
    if turn_stopped_reading(session_id):
        if (_rec is not None and isinstance(_rec.room, dict)
                and (not eid or str(_rec.entity_id) == str(eid))):
            # The server's words (above): if they land in another running
            # turn as a nudge, that turn sends them on if it never reads
            # them, and the row holding them goes with them.
            own_turn = await answer_after_turn(_rec.entity_id, _rec.room, text,
                                               asked_session=session_id, max_chars=None,
                                               **({"pending_key": _key} if _key else {}))
            if not own_turn:
                # Refused: the page keeps the words, so nothing holds them here.
                if _key:
                    try:
                        await _pw.settle(brain_service.get_pool(), _key)
                    except Exception as e:  # noqa: BLE001
                        log.error("nudge: a refused nudge's row was not removed: %s", e)
                await _audit(
                    request_id=request_id, entity_id=eid, session_id=session_id,
                    target_kind="self", phase="nudge_queued", status_="error",
                    text_preview=text[:120],
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"ok": False, "error": {
                        "code": "turn_stopped_reading",
                        "message": "The turn has stopped reading and the message could "
                                   "not start a turn of its own; send it as a message.",
                        "request_id": request_id}},
                )
    if own_turn is None:
        if _key:
            # The server's to deliver from here: read by the turn, or sent on
            # as the thread's next turn when it ends without reading them
            # (session.end_turn_input), with the row that holds them.
            push_nudge(session_id, text, client_id=client_id, pending_key=_key)
        else:
            # Nothing was written (PII mode: raw text at rest is what that
            # mode forbids), so a restart would lose words the server kept
            # in memory. The answer says so (`held: false`) and the page
            # keeps its copy: the turn's nudge_read receipt settles it, and
            # with none the page sends it as its next message, the way it
            # does when the write fails (503). So the server never sends
            # these on itself (keeps_copy), or they would go twice.
            push_nudge(session_id, text, client_id=client_id, keeps_copy=True)

    await _audit(
        request_id=request_id, entity_id=eid, session_id=session_id,
        target_kind="self", phase="nudge_queued", status_="ok",
        text_preview=text[:120],
    )

    out = {"ok": True, "session_id": session_id,
           "queued_chars": len(text), "request_id": request_id,
           # The server holds the words (pending_words): the page keeps no
           # copy. False where nothing is written (PII mode): the page keeps
           # its copy and sends it again if the turn never reads it.
           "held": bool(_key)}
    if own_turn:
        out["own_turn"] = True
    return out


@router.get("/pending", dependencies=_READ)
async def pending(thread: str | None = None) -> dict:
    """The words the server holds for a page of your Host (pending_words): sent
    while a turn ran, and not yet read by a turn. The page shows them as sent
    and waiting, after a reload too; they leave this list when a turn has
    them. `thread`: "original" or a thread's uuid, as /turns/active takes it
    (absent: the original conversation)."""
    from mastercode import pending_words as _pw
    from server.turn_registry import compose_thread_key
    eid = _entity_id()
    pool = brain_service.get_pool()
    if not eid or pool is None:
        return {"ok": True, "words": []}
    key = compose_thread_key(eid, "circles", None, thread_id=_ct.canonical_id(thread))
    return {"ok": True, "words": await _pw.held_for(pool, eid, key)}


# ---- Detached turns: see, reattach to, and stop the running turn ----
#
# A turn started on a phone died in the UI on screen-off,
# kept running invisibly, and retries stacked duplicate turns. The turn now
# runs DETACHED in server/turn_registry.py; these routes are the visibility
# surface — the standing rule is NO hidden processes: whatever runs must be
# listable, watchable, and stoppable from the client that started it.

@router.get("/turns/active", dependencies=_READ)
async def turn_active(thread: str | None = None) -> dict:
    """The running (or just-finished, within TTL) turn on the self thread.
    The client calls this on open/wake; a non-null turn means: don't show a
    dead chat — reattach to /turns/{id}/stream and render live.

    `thread` is the Circles thread the client is looking at ("original" or a
    uuid; absent means the original conversation). It goes into the turn-lock
    key so a client sitting on thread B finds B's running turn and not A's —
    the send path composes the same key, and the two must not diverge or
    reattach silently stops finding the turn. `turns` stays entity-wide: the
    list is "what is running for you", across every thread."""
    from server.turn_registry import compose_thread_key, get_turn_registry
    eid = _entity_id()
    reg = get_turn_registry()
    # canonical_id, not the raw parameter: the send path composes this key
    # from the thread ROW's id (_thread_id_from_body), and compose_thread_key
    # is a byte compare. A client that held an uppercase or hyphenless id
    # would look in a lane nothing ever ran in — finding no turn to reattach
    # to and stranding the budget key under the canonical one.
    rec = reg.active_for(compose_thread_key(
        eid, "circles", None, thread_id=_ct.canonical_id(thread)))
    return {"ok": True, "turn": rec.describe() if rec else None,
            "turns": reg.list_turns(eid)}


@router.get("/turns/{turn_id}/stream", dependencies=_READ)
async def turn_stream(turn_id: str, from_seq: int = 0):
    """Reattach: replay the turn's buffered frames from from_seq, then follow
    live. Same wire format as POST /messages — the client reuses its parser.
    Works on finished turns too (pure replay) until the record's TTL. A
    `replayed` frame marks where the replay ends (TurnRegistry.tail
    mark_replayed): Circles says the Host is writing only for words that
    arrive after it."""
    from fastapi.responses import StreamingResponse
    from server.brain_service import _never_silent
    from server.turn_registry import get_turn_registry
    rec = get_turn_registry().get(turn_id)
    if rec is None:
        raise HTTPException(404, "unknown turn (finished more than ~15min ago?)")
    return StreamingResponse(
        _never_silent(get_turn_registry().tail(
            rec, mode="reattached", from_seq=max(0, from_seq), mark_replayed=True)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-data-stream": "v1",
        },
    )


@router.post("/turns/{turn_id}/cancel", dependencies=_WRITE)
async def turn_cancel(turn_id: str) -> dict:
    """The stop button. Cancels the detached turn task; the teardown chain
    (registry → inner generator finally → runtime task → engine) takes the
    SDK subprocess down with it and the wire says so on every attached tail."""
    from server.turn_registry import get_turn_registry
    out = await get_turn_registry().cancel(turn_id)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error", "unknown turn"))
    return out


# ---- Delete / clear a conversation (soft, no hard data loss) ----

@router.delete("/thread/{kind}/{ref}", dependencies=_WRITE)
async def delete_thread(kind: str, ref: str, request: Request) -> dict:
    """Soft-delete (clear) a conversation. No hard data loss:

      - kind='self'   — clear ONE Circles thread by flipping its turn rows
                        from status='active' to status='archived' (so
                        _read_self_thread, which only reads 'active', stops
                        surfacing them). Rows stay in entity_memory. `ref` is
                        the thread: "original" (or "self", or nothing) clears
                        the original conversation exactly as before, a uuid
                        clears that thread and nothing else.
      - kind='entity' — archive the host↔host DM thread with this peer by setting
                        inbox_messages.archived_at (the same column
                        _list_entity_threads / _read_entity_thread filter on).
                        Rows stay in the inbox, just hidden from Circles.

      - kind='dm'     — archive the person ↔ person thread with this email the
                        same way (rows where both ends are people and the other
                        end is `ref`).

      - kind='circle' — a circle lives on the platform, not in this pod: the
                        bridge's circles.leave archives it for everyone when
                        this Host owns it, and otherwise takes this Host out
                        of it. Both are the platform's own status columns
                        (circles.status, circle_whitelist.status), so nothing
                        is erased.

    Returns {ok, cleared} with the row count.
    """
    request_id = str(uuid.uuid4())
    if kind not in {"self", "entity", "dm", "circle"}:
        raise _err_envelope(
            request_id, "unsupported_kind",
            f"Can't clear a {kind!r} conversation from here.",
            status.HTTP_400_BAD_REQUEST,
        )

    if kind == "circle":
        try:
            circle_id = int(ref)
        except (TypeError, ValueError):
            raise _err_envelope(
                request_id, "bad_circle", f"Bad circle id: {ref!r}.",
                status.HTTP_400_BAD_REQUEST,
            ) from None
        # The full envelope, not _bridge_call's None-on-anything: a refusal, a
        # platform without circles.leave and a transport failure need
        # different words, and only the last is worth trying again.
        client = server_main.get_bridge_client()
        env = None
        if client is not None:
            try:
                env = await client.request_full("circles.leave", {"circle_id": circle_id})
            except Exception as e:  # noqa: BLE001
                log.warning("bridge call circles.leave failed: %s", e)
        if isinstance(env, dict) and env.get("ok") and env.get("data"):
            return {"ok": True, "cleared": 1, "circle": env["data"]}
        err = str((env or {}).get("error") or "")
        if (env or {}).get("code") == 403:
            words, code = ("You're not in this circle any more, or it was already "
                           "removed."), status.HTTP_404_NOT_FOUND
        elif err.startswith("Unknown operation"):
            words, code = ("The platform can't remove circles yet; it needs its "
                           "update first."), status.HTTP_501_NOT_IMPLEMENTED
        else:
            words, code = ("Couldn't reach the platform to remove this circle. "
                           "Try again in a moment."), status.HTTP_502_BAD_GATEWAY
        raise _err_envelope(request_id, "leave_failed", words, code)

    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None:
        raise HTTPException(503, "Brain not ready")

    if kind == "self":
        try:
            # Clear archives what the conversation shows
            # (circles_threads.conversation_where) plus, on a thread, the
            # replay/A-B rows written inside it — the read hides those but
            # they belong to the thread being cleared. On the original
            # conversation the app_id term already leaves them out, as it
            # always did. The thread row itself stays; clearing is not
            # closing.
            _a: list = [eid]
            _w = _ct.conversation_where(_a, ref)
            rows = await pool.fetch(
                f"UPDATE entity_memory"
                f"   SET status = 'archived',"
                f"       status_reason = 'circles_thread_cleared'"
                f" WHERE {' AND '.join(_w)} RETURNING id",
                *_a,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("self-thread clear failed (entity %s): %s", eid, exc)
            raise _err_envelope(
                request_id, "clear_failed",
                "Couldn't clear this conversation. Try again in a moment.",
                status.HTTP_502_BAD_GATEWAY,
            ) from None
        cleared = len(rows)
    elif kind == "dm":
        from server.operator_identity import request_email
        peer_email = (ref or "").strip().lower()
        my_email = request_email(request.headers)
        if "@" not in peer_email or not my_email:
            raise _err_envelope(
                request_id, "bad_peer", f"Bad person: {ref!r}.",
                status.HTTP_400_BAD_REQUEST,
            )
        try:
            # Only the requesting person's side of the thread — another
            # operator of this host keeps theirs.
            rows = await pool.fetch(
                """
                UPDATE inbox_messages
                   SET archived_at = now()
                 WHERE message_kind = 'message'
                   AND archived_at IS NULL
                   AND from_user_email IS NOT NULL AND to_user_email IS NOT NULL
                   AND ((from_entity_id = $1 AND from_user_email = $3 AND to_user_email = $2)
                     OR (to_entity_id = $1 AND to_user_email = $3 AND from_user_email = $2))
                RETURNING message_id
                """,
                eid, peer_email, my_email,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("people-thread clear failed (entity %s, peer %s): %s", eid, peer_email, exc)
            raise _err_envelope(
                request_id, "clear_failed",
                "Couldn't clear this conversation. Try again in a moment.",
                status.HTTP_502_BAD_GATEWAY,
            ) from None
        cleared = len(rows)
    else:  # entity
        try:
            peer = int(ref)
        except (TypeError, ValueError):
            raise _err_envelope(
                request_id, "bad_peer", f"Bad peer id: {ref!r}.",
                status.HTTP_400_BAD_REQUEST,
            ) from None
        # A LIVE bastion peering keeps its channel open. The
        # Hosts list is derived from messages and filters `archived_at IS NULL`
        # (init-db.sql:1815 declares the column; circles.py:607 does the
        # filtering), so archiving would hide a Host you are still sharing a
        # machine with — and that peering is precisely the fact this feature
        # exists to keep in front of the user. Refused while the peering is
        # current; once the bastion stops announcing it, or the bastion is
        # revoked, it clears normally. Refused rather than silently no-op'd: a
        # Clear that reports success and changes nothing is worse than one that
        # says why.
        try:
            from server.network import peer_discovery
            live_peer = next(
                (p for p in await peer_discovery.list_peerings(pool, eid)
                 if p.get("peer_entity_id") == peer and p.get("current")), None)
        except Exception as exc:  # noqa: BLE001 - never block a clear on this
            log.info("peering check skipped for peer %s: %s", peer, exc)
            live_peer = None
        # A PEERING MAY HOLD OPEN AN EMPTY CHANNEL. IT MAY NEVER LOCK A REAL
        # CONVERSATION.
        #
        # peer_entity_id is resolved from a handle the BASTION supplies
        # (peer_discovery.record_and_notify -> _resolve_target_service), so the
        # machine chooses which entity id lands here. Without this check, a
        # compromised or hostile box announces a "peer" whose handle resolves to
        # someone the user actually talks to, and the user can no longer clear
        # that conversation — a machine-controlled lock on a user action, in the
        # one surface built to reassure them.
        #
        # The rule that keeps that intent and removes the lock: refuse only
        # when there is nothing to clear. A brand-new peer has no history, so the
        # channel stays open exactly as designed; a thread with real messages in
        # it always clears, and the peering simply re-appears afterwards through
        # _list_bastion_peers_without_thread (which lists any current peering we
        # have no un-archived thread with). Nothing is lost either way.
        if live_peer is not None:
            try:
                exchanged = await pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM inbox_messages
                         WHERE message_kind = 'message'
                           AND archived_at IS NULL
                           AND from_entity_id IS NOT NULL AND to_entity_id IS NOT NULL
                           AND ((from_entity_id = $1 AND to_entity_id = $2)
                             OR (from_entity_id = $2 AND to_entity_id = $1)))
                    """,
                    eid, peer,
                )
            except Exception as exc:  # noqa: BLE001 - never block a clear on this
                log.info("peer history check failed for %s: %s", peer, exc)
                exchanged = True  # cannot prove it is empty -> let the clear run
            if exchanged:
                live_peer = None
        if live_peer is not None:
            machine = live_peer.get("introduced_by") or "one of your machines"
            raise _err_envelope(
                request_id, "peer_channel_open",
                f"This Host shares your machine {machine}, so its channel stays "
                f"open. Revoke that bastion in Network to end the peering, then "
                f"clear the conversation.",
                status.HTTP_409_CONFLICT,
            )
        try:
            rows = await pool.fetch(
                """
                UPDATE inbox_messages
                   SET archived_at = now()
                 WHERE message_kind = 'message'
                   AND archived_at IS NULL
                   AND from_entity_id IS NOT NULL AND to_entity_id IS NOT NULL
                   AND ((from_entity_id = $1 AND to_entity_id = $2)
                     OR (from_entity_id = $2 AND to_entity_id = $1))
                   AND NOT (from_user_email IS NOT NULL AND to_user_email IS NOT NULL)
                RETURNING message_id
                """,
                eid, peer,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("entity-thread clear failed (entity %s, peer %s): %s", eid, peer, exc)
            raise _err_envelope(
                request_id, "clear_failed",
                "Couldn't clear this conversation. Try again in a moment.",
                status.HTTP_502_BAD_GATEWAY,
            ) from None
        cleared = len(rows)

    await _audit(
        request_id=request_id, entity_id=eid, session_id=None,
        target_kind=kind, phase="thread_cleared", status_="ok",
    )
    return {"ok": True, "cleared": cleared, "request_id": request_id}


def _attachment_name(att: dict, i: int) -> str:
    """The file's own name when the composer sent one; otherwise a stable
    stand-in with the extension its media type implies."""
    import mimetypes
    name = str((att or {}).get("name") or "").replace("/", "_").replace("\\", "_").strip()[:200]
    if name:
        return name
    media = (((att or {}).get("content_block") or {}).get("source") or {}).get("media_type") or ""
    return f"attachment-{i + 1}{mimetypes.guess_extension(media) or ''}"


def _distinct_names(names: list) -> list:
    """Same-named files in ONE message (two pasted "image.png") get distinct
    stored names — "image.png", "image (2).png" — so neither hides the other."""
    seen: dict = {}
    out = []
    for n in names:
        key = n.lower()
        if key not in seen:
            seen[key] = 1
            out.append(n)
            continue
        stem, dot, ext = n.rpartition(".")
        if not dot or not stem:
            stem, ext, dot = n, "", ""
        k = seen[key]
        while True:
            k += 1
            cand = f"{stem} ({k}){dot}{ext}"
            if cand.lower() not in seen:
                break
        seen[key] = k
        seen[cand.lower()] = 1
        out.append(cand)
    return out


def _check_attachments(attachments, request_id: str) -> None:
    """Refuse a malformed or oversize chat attachment; anything else passes.

    Workbench-format content blocks — [{name?, content_block: {type:
    image|document, source: {type: base64, media_type, data}}}].

    The route once had its own "20 files, 30 MB
    each, mirroring what Claude accepts" — unverified, and turns go to other
    providers too. How many files a message may carry is now the ROUTED
    PROVIDER's call: its refusal comes back as the turn's error
    (provider_gateway: "<provider> <status>: <body>"). One file's size is the
    Host's one bound for a file it holds (context.MAX_FILE_UPLOAD_BYTES), the
    same number that decides whether it can be kept, so a file is never taken
    here and then refused by the keep; past it the answer is a 413 that says
    why."""
    from server.routes.context import MAX_FILE_UPLOAD_BYTES, upload_too_large_message
    if not attachments:
        return
    if not isinstance(attachments, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"ok": False, "error": {"code": "bad_attachments", "message": "Attachments must be a list.", "request_id": request_id}},
        )
    for att in attachments:
        blk = (att or {}).get("content_block") or {}
        if blk.get("type") not in ("image", "document"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"ok": False, "error": {"code": "bad_attachments", "message": "Attachment content_block.type must be image or document.", "request_id": request_id}},
            )
        size = len(((blk.get("source") or {}).get("data")) or "") * 3 // 4
        if size > MAX_FILE_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"ok": False, "error": {"code": "attachment_too_large", "message": upload_too_large_message(size), "request_id": request_id}},
            )


def _keep_attachments_on_host(eid: int, attachments: list) -> None:
    """Schedule storing each chat attachment through the upload lane
    (context._ingest_binary_file: stored, read, embedded). A chat file is its
    OWN copy: supersede=False, so it never replaces an upload that happens to
    share its name, and same-named files in one message are stored under
    distinct names. Files over the upload cap are not kept, and say so. Marks
    each attachment kept_on_host (with the name it is stored under) or
    keep_note (why not), for the turn's frame. Fire-and-forget: every failure
    is logged, none reaches the chat."""
    import asyncio
    import base64
    import uuid
    from server.routes.context import MAX_FILE_UPLOAD_BYTES
    pool = brain_service.get_pool()
    if pool is None or not eid:
        return
    items = []
    for i, att in enumerate(attachments):
        if not isinstance(att, dict):
            continue
        src = ((att.get("content_block") or {}).get("source") or {})
        if src.get("type") != "base64" or not src.get("data"):
            continue
        items.append((att, _attachment_name(att, i), src))
    names = _distinct_names([n for _, n, _ in items])
    jobs = []
    for (att, _, src), name in zip(items, names):
        att["name"] = name
        size = len(src["data"]) * 3 // 4
        if size > MAX_FILE_UPLOAD_BYTES:
            att["kept_on_host"] = False
            att["keep_note"] = (f"about {size >> 20} MB, over the "
                                f"{MAX_FILE_UPLOAD_BYTES >> 20} MB a file kept on the Host may be")
            continue
        att["kept_on_host"] = True
        jobs.append((name, src.get("media_type") or "", src["data"]))
    if not jobs:
        return

    async def _store():
        from server.routes.context import _ingest_binary_file
        for name, media, data in jobs:
            try:
                raw = base64.b64decode(data, validate=False)
                await _ingest_binary_file(pool, eid, raw, name, media, [],
                                          uuid.uuid4().hex, supersede=False)
            except Exception as e:  # noqa: BLE001 — never the chat's failure
                log.warning("circles: keeping attachment %r on the host failed: %s", name, e)

    task = asyncio.get_running_loop().create_task(_store())
    _KEEP_TASKS.add(task)                   # held until done: an unreferenced task can be collected
    task.add_done_callback(_KEEP_TASKS.discard)


_KEEP_TASKS: set = set()
