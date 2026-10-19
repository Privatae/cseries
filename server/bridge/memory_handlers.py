"""Pod-side handlers for inbound bridge memory.* dispatches.

The bridge (Ship D ops in bridge/adapter.py) sends `device.dispatch` envelopes
down the existing mTLS socket with action memory.config.push (and the
PME→EME and calendar/inbox actions below). The pod's bridge_client routes
each to one of the handlers below.

Each handler returns a dict (NOT a {"ok": ..., "data": ...} envelope — the
client wraps it). On error, return {"ok": False, "error": "..."}.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import uuid

from server import brain_service

logger = logging.getLogger("privatae.bridge.memory_handlers")

# Strong references to fire-and-forget tasks spawned by handlers (the event
# loop keeps only weak ones); each task discards itself when done.
_background_tasks: set = set()


def _parse_ts(value):
    """Accept either a datetime.datetime, an ISO 8601 string, or None.
    asyncpg's timestamptz binding requires datetime; the bridge JSON wire
    serializes datetimes as ISO strings, so callers from the platform's memory
    service arrive here with strings. Returns None for falsy input — handlers
    use COALESCE($N::timestamptz, NOW()) so None defaults to NOW()."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, str):
        try:
            # python 3.11+ accepts "...Z" via fromisoformat
            return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clean_email(value) -> str | None:
    """A person's address as the inbox stores it: lowercased, whitespace
    stripped, must look like one mailbox. Anything else is None — a message
    never carries a half-formed identity. (Circles Hosts | People.)"""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or "@" not in v or " " in v or len(v) > 320:
        return None
    local, _, domain = v.rpartition("@")
    if not local or "." not in domain:
        return None
    return v


def _parse_uuid(value):
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# Fields the bridge is permitted to push down. Any other field_path is
# rejected — defends against a bridge bug pushing into unrelated tables.
_ALLOWED_FIELD_PREFIXES = (
    "behavioral_params.",
    "entity.behavioral_params.",
)

# Governance flags a host MUST NOT be able to clear via its own config-push.
# The sub-key (after the behavioral_params. prefix) is matched; pushes targeting
# one are rejected rather than silently no-opped, so the attempt is visible.
#
# Empty now. It held use_unified_brain, use_v4_brain and
# watchdog_enabled, which locked the UnifiedBrain semantic veto and its
# watchdog. Those modules were deleted as unreachable, so nothing read the three
# keys any more and locking them protected nothing. The denylist itself stays
# because it is the place a genuinely governance-gating flag has to be named:
# add the flag here in the same change that adds the code reading it.
#
# web_search (web_research.platform_search_config): the backend
# every one of the Host's searches goes to. A push could point them all at a
# backend of the pusher's choosing, so no push sets it; it is written where the
# Host's settings are written directly. Whoever later decides who may set it
# decides this lock in the same change.
_GOVERNANCE_LOCKED_SUBKEYS: frozenset[str] = frozenset({"web_search"})


def _entity_id() -> int:
    return brain_service._entity_id or int(os.environ.get("ENTITY_ID", "0"))


async def handle_memory_config_push(payload: dict) -> dict:
    """Apply a config delta from the bridge to local entity_registry.

    Payload shape (matches bridge's op_memory_config_push):
      {field_path: str, value: <jsonable>, sequence: int}

    field_path is dotted; only the behavioral_params.* subtree is writable.
    The sequence is monotonic — we skip the apply if we've already seen a
    sequence >= the incoming one (idempotency under burst).
    """
    field_path = payload.get("field_path") or ""
    value = payload.get("value")
    sequence = int(payload.get("sequence", 0))

    if not field_path:
        return {"ok": False, "error": "field_path required"}
    if not any(field_path.startswith(p) for p in _ALLOWED_FIELD_PREFIXES):
        return {"ok": False, "error": f"field_path not allowed: {field_path}"}

    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}

    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    # Strip the "behavioral_params." prefix (or "entity.behavioral_params.")
    # to get the JSONB sub-key.
    if field_path.startswith("entity.behavioral_params."):
        sub_key = field_path[len("entity.behavioral_params."):]
    else:
        sub_key = field_path[len("behavioral_params."):]

    if not sub_key:
        return {"ok": False, "error": "field_path has no sub-key"}

    # Governance denylist: a locked key is never set through this lane.
    # Reject (don't silently no-op) so the attempt is visible.
    if sub_key in _GOVERNANCE_LOCKED_SUBKEYS:
        logger.warning(
            "Rejected config.push to governance-locked field: entity=%d field=%s",
            entity_id, field_path,
        )
        return {"ok": False, "error": f"field_path is governance-locked: {field_path}"}

    try:
        async with pool.acquire() as conn:
            # Idempotency: if local config_versions already has >= sequence
            # for this field, skip the apply (we've seen it).
            existing_seq = await conn.fetchval(
                """
                SELECT sequence FROM config_versions
                WHERE entity_id = $1 AND field_path = $2
                """,
                entity_id, field_path,
            )
            if existing_seq is not None and existing_seq >= sequence:
                return {
                    "ok": True,
                    "applied": False,
                    "reason": "stale_or_duplicate",
                    "local_sequence": int(existing_seq),
                }

            # Build a JSONB patch — top-level merge of {sub_key: value}.
            # entity_registry.behavioral_params is JSONB; the || operator
            # does a shallow merge so we only overwrite the one key.
            patch = json.dumps({sub_key: value})
            await conn.execute(
                """
                UPDATE entity_registry
                SET behavioral_params = COALESCE(behavioral_params, '{}'::jsonb) || $2::jsonb
                WHERE id = $1
                """,
                entity_id, patch,
            )

            await conn.execute(
                """
                INSERT INTO config_versions (entity_id, field_path, sequence, updated_by)
                VALUES ($1, $2, $3, 'bridge.push')
                ON CONFLICT (entity_id, field_path) DO UPDATE
                  SET sequence   = EXCLUDED.sequence,
                      updated_at = NOW(),
                      updated_by = EXCLUDED.updated_by
                """,
                entity_id, field_path, sequence,
            )
    except Exception as exc:
        logger.exception("memory.config.push apply failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    logger.info(
        "Applied bridge config.push: entity=%d field=%s sequence=%d",
        entity_id, field_path, sequence,
    )
    return {"ok": True, "applied": True, "sequence": sequence, "field_path": field_path}


async def handle_calendar_append(payload: dict) -> dict:
    """Insert a row into the pod's local calendar_events (Ship E PME→EME).

    Bridge `op_calendar_log` dispatches this when an entity-pod hook writes
    a timeline event. Payload shape mirrors the bridge op's params.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}

    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    event_type = (payload.get("event_type") or "").strip()
    summary = (payload.get("summary") or "").strip()
    status = payload.get("status", "completed")
    if not event_type or not summary:
        return {"ok": False, "error": "event_type and summary required"}
    if status not in ("scheduled", "running", "completed", "failed", "cancelled"):
        return {"ok": False, "error": f"invalid status: {status!r}"}

    body_payload = payload.get("payload") or {}
    if not isinstance(body_payload, dict):
        return {"ok": False, "error": "payload must be an object"}

    # Ship E alignment: if the caller (the platform memory service's dual-write) supplies an
    # event_id, use it so the row has the same UUID in PME and EME.
    # ON CONFLICT lets the same dual-write retry idempotently.
    supplied_event_id = _parse_uuid(payload.get("event_id"))
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO calendar_events
              (event_id,
               entity_id, event_type, event_at, summary, payload,
               source, status, cost_usd, tokens_in, tokens_out, model_id,
               related_message_id, related_artifact_uri, related_project_id,
               start_time, end_time, title, all_day, category, created_by,
               recurrence)
            VALUES
              (COALESCE($15::uuid, gen_random_uuid()),
               $1, $2::text, COALESCE($3::timestamptz, NOW()),
               $4::text, $5::jsonb,
               $6::text, $7::text, $8::numeric, $9::int, $10::int, $11::text,
               $12::uuid, $13::text, $14::int,
               COALESCE($3::timestamptz, NOW()), COALESCE($3::timestamptz, NOW()),
               $4::varchar, FALSE, $2::varchar, COALESCE($6::varchar, 'entity'),
               $16::text)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id, event_at, status
            """,
            int(entity_id), event_type, _parse_ts(payload.get("event_at")),
            summary, json.dumps(body_payload),
            payload.get("source", "entity"), status,
            payload.get("cost_usd"), payload.get("tokens_in"),
            payload.get("tokens_out"), payload.get("model_id"),
            _parse_uuid(payload.get("related_message_id")),
            payload.get("related_artifact_uri"),
            payload.get("related_project_id"),
            supplied_event_id,
            # Recurrence (RRULE-lite: daily/weekly/hourly/monthly/every:<seconds>) —
            # the scheduled-task runner re-arms a recurring 'scheduled' event to its next occurrence
            # after it fires. NULL for one-shot events (the existing default).
            (payload.get("recurrence") or None),
        )
        if row is None and supplied_event_id is not None:
            # ON CONFLICT — row already exists; treat as success (idempotent).
            row = await pool.fetchrow(
                "SELECT event_id, event_at, status FROM calendar_events WHERE event_id=$1",
                supplied_event_id,
            )
    except Exception as exc:
        logger.exception("calendar.append insert failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "event_id": str(row["event_id"]),
        "event_at": row["event_at"].isoformat(),
        "status": row["status"],
    }


# What another Host may put in this one's inbox in ONE message. A body from
# another host is cross-tenant input: it is stored in this Host's own database
# and returned whole whenever its Host or its person reads the thread
# (messages.thread_service, the Circles thread views). The sender side's own
# length policy is removed (the person's words are theirs), which is only safe
# while the RECEIVER protects itself — so it does, here.
#
# The number is this Host's own direct lane: server/peer/peer_server.py takes a
# request of at most _MAX_REQ (256 KiB), and 32 KiB of that is left for the
# envelope (thread, ids, the permission slip), so a message under this bound
# crosses EITHER lane — the direct dial and the bridge relay — the same way.
# For scale: Slack refuses a message past 40,000 characters, a Matrix event is
# at most 64 KiB, a Telegram message 4,096 characters; 224 KiB is ~5x Slack's
# whole message and about 57k tokens of a reader's window, far above a real
# host-to-host message, which is a request, a report or a reply — a document
# goes as a shared file, not a message. Local writers (the Host's own
# notifications, its outbound copies) are not cross-host and are not bounded.
CROSS_HOST_BODY_MAX = 224 * 1024   # utf-8 bytes of one body; gate-checked vs _MAX_REQ


def cross_host_body_refusal(body: str) -> str | None:
    """The plain sentence a cross-host message past CROSS_HOST_BODY_MAX is
    refused with, or None when it fits. One wording for both ends: the sender
    learns it before the message travels, the receiver says it if one arrives."""
    n = len((body or "").encode("utf-8"))
    if n <= CROSS_HOST_BODY_MAX:
        return None
    return (f"this message is {n // 1024} KB; a host accepts at most "
            f"{CROSS_HOST_BODY_MAX // 1024} KB in one message from another host — "
            f"send it in parts, or share it as a file")


# ── The permission slip, checked HERE ────────────────────────────────────────
# A slip-gated delivery used to be admitted on three flags in the payload —
# slip_authorized, sender_verified and the nested authorized_via_slip — that the
# bridge set after verifying the slip. On the relay the payload is the SENDING
# Host's own params passed through, so a peer could set the flags itself and be
# admitted with no slip at all (seen live; the bridge checks its own half).
# Neither side alone may be fooled, so this Host never reads those flags: it
# admits on a slip only when it checked the slip itself —
#   * the direct lane (server/peer): peer_server verified the presented slip
#     offline before the handler ran, and hands its result in-process as
#     `verified_slip` — a keyword argument, never a payload field;
#   * the relay: the presented slip travels with the message (`slip`), and this
#     Host verifies it offline exactly as the direct lane does — pinned bridge
#     signing keys, the synced revocation list, issued by THIS Host to THIS
#     sender for `message`, unexpired.
_SLIP_TRUST = None
_SLIP_TRUST_AT = 0.0
# The fallback trust is refreshed on the peer lane's own cadence
# (server/main.py: PeerTrust.run_periodic(120)), so a slip revoked after it
# first became ready is refused within one interval, not honoured until
# restart or expiry.
_TRUST_REFRESH_S = 120.0


async def _trust_bridge_call(op, params):
    """PeerTrust's bridge call: the data dict, or None on any failure."""
    try:
        from server import main as _main
        c = _main.get_bridge_client()
        return await c.request(op, params or {}) if c is not None else None
    except Exception:
        return None


async def _slip_trust():
    """The pinned keys + revocation view: the peer lane's own PeerTrust when it
    runs, else one kept here, refreshed before first use. None when it cannot
    be made ready (no keys, revocation list never synced) — fail closed."""
    global _SLIP_TRUST, _SLIP_TRUST_AT
    import time as _time
    trust = None
    try:
        from server import main as _main
        trust = getattr(_main, "_peer_trust_ref", None)
    except Exception:
        trust = None
    fallback = trust is None
    if fallback:
        if _SLIP_TRUST is None:
            from server.peer.peer_trust import PeerTrust
            _SLIP_TRUST = PeerTrust(_trust_bridge_call)
        trust = _SLIP_TRUST
    # The peer lane's own trust refreshes itself every 120 s (run_periodic);
    # the fallback has no task, so it refreshes here on the same cadence.
    due = fallback and (_time.monotonic() - _SLIP_TRUST_AT >= _TRUST_REFRESH_S)
    if not getattr(trust, "ready", False) or due:
        try:
            await trust.refresh()
        except Exception as e:
            logger.warning("slip trust refresh failed: %s", e)
        if fallback:
            _SLIP_TRUST_AT = _time.monotonic()
    return trust if getattr(trust, "ready", False) else None


async def _slip_admits(payload: dict, from_eid, entity_id, verified_slip) -> tuple[bool, str]:
    """(this Host checked a slip that lets `from_eid` message it, why not)."""
    if verified_slip is not None:
        # The direct lane's own verification (peer_server.verify_slip with
        # expected_issuer = this Host, presenter = the cert's entity).
        if not isinstance(verified_slip, dict):
            return False, "malformed slip"
        ops = verified_slip.get("allowed_ops")
        try:
            ok = (isinstance(ops, list) and "message" in ops
                  and int(verified_slip.get("grantee")) == int(from_eid)
                  and int(verified_slip.get("issuer")) == int(entity_id))
        except (TypeError, ValueError):
            ok = False
        return ok, ("verified on the direct lane" if ok else "the verified slip is not for this sender")
    slip = payload.get("slip")
    if not isinstance(slip, dict) or not slip:
        return False, "no slip"
    trust = await _slip_trust()
    if trust is None:
        return False, "unchecked"
    from server.peer.slip_verify import slip_key_id, verify_slip
    # Shape first, with the same helper the direct lane uses: a slip that is
    # not one is refused in words, never raised on.
    # verify_slip re-checks every field it uses.
    key_id = slip_key_id(slip)
    if key_id is None:
        return False, "malformed slip"
    try:
        await trust.ensure_key(key_id)
    except Exception:
        pass
    ok, reason, _info = verify_slip(
        slip, from_eid, "message", trusted_keys=trust.keys, revoked_ids=trust.revoked,
        expected_issuer=entity_id)
    return ok, reason


async def handle_relay_inbox_append(payload: dict) -> dict:
    """The bridge's `inbox.append` (registered in server/main.py): a message the
    relay carried. The same storage as every other way in, told that it came by
    the relay, so a person the platform checked is marked as checked
    (circles_reading.CHECKED_KEY). In-process callers use handle_inbox_append
    and are never taken for the relay."""
    return await handle_inbox_append(payload, via_relay=True)


async def handle_inbox_append(payload: dict, *, verified_slip: dict | None = None,
                              via_relay: bool = False) -> dict:
    """Insert a row into the pod's local inbox_messages (Ship E PME→EME).

    Bridge `op_inbox_send` dispatches this for self-notifications (update
    checks, daily summaries). Cross-entity messaging stays bridge-side
    until that pattern gets its own op.

    `via_relay`: the bridge delivered it (handle_relay_inbox_append).
    `verified_slip`: the direct lane delivered it, with the slip it checked
    (peer/handlers.inbox_deliver).
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}

    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    subject = (payload.get("subject") or "").strip()
    if not subject:
        return {"ok": False, "error": "subject required"}
    body = payload.get("body") or ""
    body_format = payload.get("body_format") or "text/markdown"
    message_kind = payload.get("message_kind") or "system_notification"
    body_payload = payload.get("payload") or {}
    if not isinstance(body_payload, dict):
        return {"ok": False, "error": "payload must be an object"}

    to_address = payload.get("to_address") or f"{entity_id}@entity.localhost"
    from_address = payload.get("from_address") or "cseries@localhost"
    # WHO WROTE IT, as distinct from WHICH HOST CARRIED IT (Circles Hosts |
    # People). from_entity_id / to_entity_id are the routing
    # endpoints — always hosts. from_user_email / to_user_email name the PEOPLE
    # on either end when a person, not a host, authored or is addressed:
    #   * person → person   both set   → the People list
    #   * person → host     from set   → the Hosts › Peers list, author shown
    #                                    as the person "via" their host
    #   * host   → anyone   from unset → the Hosts › Peers list (as today)
    # A cross-host sender's email is only trusted because the bridge verified
    # it is an operator of the sending host before relaying (adapter.py
    # _enrich_and_verify_cross_host); a pod cannot claim a stranger's name.
    from_user_email = _clean_email(payload.get("from_user_email"))
    to_user_email = _clean_email(payload.get("to_user_email"))
    # The table requires from_entity_id OR from_user_email
    # (inbox_messages_from_required). Self-notifications and local
    # user-composed sends carry neither — attribute them to this entity.
    # Cross-entity deliveries supply the sender's id explicitly.
    from_entity_id = payload.get("from_entity_id")
    if from_entity_id is None and not from_user_email:
        from_entity_id = entity_id

    # Outbound copy: the bridge (circles.send_entity_dm) writes the sender's OWN
    # copy of a DM it just sent, so the thread shows both sides. Stored from=self →
    # to=peer, and NOT subject to inbound domain policy (it's our own send).
    #
    # `direction` is a CLAIM in the payload, and on the relay lane the payload
    # is the sending Host's own params passed through (the bridge's inbox.send),
    # so a peer could label its message "outbound" to skip the size bound and
    # the admission gate below (seen live). A message counts as outbound only
    # when it is THIS Host's own write: from_entity_id is this Host, or absent
    # (the local write path).
    # From any other Host it is inbound, whatever it claims — bounded and
    # admitted like every other message. The bridge's genuine outbound copy
    # carries this Host's own id as from_entity_id, so real copies still land.
    claimed_outbound = payload.get("direction") == "outbound"
    try:
        _claim_from = payload.get("from_entity_id")
        _from_is_self = _claim_from is None or int(_claim_from) == int(entity_id)
    except (TypeError, ValueError):
        _from_is_self = False
    outbound = claimed_outbound and _from_is_self
    if claimed_outbound and not outbound:
        logger.warning("inbox append: a message from host %r claimed to be this host's "
                       "outbound copy — treated as inbound (bounded and admitted)",
                       payload.get("from_entity_id"))
    to_eid = entity_id
    if outbound and payload.get("outbound_to_entity_id") is not None:
        try:
            to_eid = int(payload["outbound_to_entity_id"])
            to_address = f"{to_eid}@entity.localhost"
        except (TypeError, ValueError):
            outbound = False

    # Did this arrive from ANOTHER host? Computed once, here, because the
    # broadcast below needs the same answer and is outside the admission block.
    # Read off the RAW payload exactly as admission does — a caller that names
    # no from_entity_id is local, whatever the row is later stamped with.
    try:
        _raw_from = payload.get("from_entity_id")
        _is_cross_host = _raw_from is not None and int(_raw_from) != int(entity_id)
    except (TypeError, ValueError):
        _is_cross_host = False

    # A message from ANOTHER host is bounded before anything else is done with
    # it: refused in words, nothing stored, no broadcast.
    if _is_cross_host and not outbound:
        _too_big = cross_host_body_refusal(body if isinstance(body, str) else str(body))
        if _too_big:
            logger.info("inbox append refused: cross-host body over %d bytes from=%s",
                        CROSS_HOST_BODY_MAX, from_address)
            return {"ok": False, "too_large": True, "error": _too_big}

    # Admission. Our own outbound copies skip the gate entirely.
    has_live_slip = False
    if not outbound:
        from_eid = payload.get("from_entity_id")
        try:
            cross_host = from_eid is not None and int(from_eid) != int(entity_id)
        except (TypeError, ValueError):
            cross_host = False
        # A permission slip THIS Host checked: never the payload's own
        # slip_authorized / sender_verified / authorized_via_slip, which a peer
        # can set. Only a cross-host sender needs one.
        has_live_slip, slip_why = False, "no slip"
        if cross_host:
            # BLOCK, before anything else is weighed: a live slip outranks a
            # domain-policy deny below, and Block deliberately leaves the slip
            # alone (contacts.py, the Block section). Every kind, so a blocked
            # sender's requests and grants are refused as well as its messages.
            # The person here is the platform-checked one (relay) or one the
            # direct lane has already weighed (peer/handlers.inbox_deliver).
            from server.routes.contacts import block_refuses, blocked_refusal
            try:
                refused = await block_refuses(pool, int(from_eid), from_user_email)
            except Exception as exc:
                logger.warning("inbox held: block check failed from=%s: %s",
                               from_address, type(exc).__name__)
                return {"ok": False, "retryable": True,
                        "error": "the sender could not be checked right now — try again"}
            if refused:
                logger.info("inbox refused: blocked sender from=%s", from_address)
                return blocked_refusal()
            has_live_slip, slip_why = await _slip_admits(payload, from_eid, entity_id,
                                                         verified_slip)
            if slip_why == "unchecked" and message_kind == "message":
                logger.info("inbox held: cannot check a slip right now from=%s", from_address)
                return {"ok": False, "retryable": True,
                        "error": "the permission slip could not be checked right now "
                                 "(signing keys or revocation list not synced) — try again"}
        if cross_host and message_kind == "message":
            # THE SLIP IS THE SOLE AUTHORITY for host↔host messaging. An ordinary
            # cross-host message is admitted ONLY by a live, bridge-attested slip — a
            # standing domain_policy allow is NOT sufficient. So revoking the slip
            # (renewal then refused → the held slip expires) is the one thing that cuts
            # off messaging. domain_policy is subordinated to the slip for messages.
            if not has_live_slip:
                logger.info("inbox blocked: host→host message without a live slip from=%s (%s)",
                            from_address, slip_why)
                return {"ok": False, "blocked": True,
                        "error": "host-to-host message requires a live permission slip"}
            logger.info("inbox admitted via slip (sole authority): from=%s", from_address)
        else:
            # Bootstrap (contact/permission/endpoint requests + grants), platform
            # notifications, and human-addressed mail can't carry a slip yet (the
            # relationship doesn't exist) — they flow under the pod's domain_policy
            # posture, which establishes the relationship. A valid slip may also admit
            # a gated non-message kind.
            try:
                from server.routes.domain_policy import check_inbound_allowed
                allowed = await check_inbound_allowed(
                    pool, from_address, from_eid, entity_id, message_kind)
            except Exception as exc:
                # C14 caller fail-closed: if the admission check itself errors, default to ALLOW
                # only for the cases that must always reach the user (the social-graph entry
                # points — contact/permission/endpoint requests + grants — and platform/self/
                # human mail, which carry no foreign cross-host attribution). A FOREIGN
                # (cross-host) NON-request message whose policy check errored is denied: we do not
                # admit a stranger's message just because the gate failed.
                _request_kinds = {"contact_request", "endpoint_request", "permission_request",
                                  "endpoint_grant", "permission_grant"}
                _req = message_kind in _request_kinds
                if cross_host and not _req:
                    logger.warning(
                        "domain policy check errored on cross-host %s — failing closed (deny): %s",
                        message_kind, exc)
                    allowed = False
                else:
                    allowed = True
            if not allowed and not has_live_slip:
                logger.info("inbox append blocked by domain policy: from=%s", from_address)
                return {"ok": False, "blocked": True, "error": "blocked by domain policy"}

    # What the stored row says about a slip is what THIS Host checked, never the
    # sender's word; the presented slip itself is not kept.
    body_payload = {k: v for k, v in body_payload.items() if k != "authorized_via_slip"}
    if has_live_slip:
        body_payload["authorized_via_slip"] = True
    # A TOPIC is checked HERE, where both ways in
    # store: the direct lane allow-lists its two keys, and the relay hands the
    # sender's nested payload over whole. Kept only on a Host-to-Host
    # conversation message; a person-to-person message is one continuous chat.
    from server.peer import topics as _topics
    body_payload = _topics.stored(
        body_payload,
        carries_topic=(message_kind == "message" and not (from_user_email and to_user_email)))
    # A READING NOTICE: the other person turned their Host's
    # reading of this chat on or off. Kept the same way, here, on a
    # person-to-person message only and only as "on" or "off".
    from server import circles_reading as _reading
    body_payload = _reading.stored(
        body_payload,
        people=(message_kind == "message" and bool(from_user_email and to_user_email)))
    # WHO WROTE IT, as this Host can stand behind it (circles_reading.stamped):
    # a person's message the relay carried is marked as checked by the relay,
    # and only the direct lane may say which address it could not verify.
    body_payload = _reading.stamped(
        body_payload,
        relay_checked=(via_relay and _is_cross_host and not outbound
                       and message_kind == "message" and bool(from_user_email and to_user_email)),
        direct_lane=verified_slip is not None)

    supplied_message_id = _parse_uuid(payload.get("message_id"))
    supplied_thread_id = _parse_uuid(payload.get("thread_id"))
    # A MESSAGE FROM ANOTHER HOST FILES UNDER THE THREAD WE DERIVE, never the
    # one it names. The direct lane always derived it (peer/handlers.inbox_
    # deliver, whose comment says why: a slip-holding peer naming the thread
    # of A's conversation with C landed inside it, in the reader the host_
    # message tool and so the model uses). The relay did not: the bridge
    # forwards the sender's thread_id (adapter.py _INBOX_SENDER_FIELDS and
    # op_circles_send_entity_dm), and this took it. Derived from the same
    # fields on both lanes: the sending Host as the bridge or the mTLS cert
    # stamped it, and the people the bridge or the direct lane kept.
    if _is_cross_host and not outbound and message_kind == "message":
        from server.routes.messages import pair_thread_id
        supplied_thread_id = _parse_uuid(pair_thread_id(
            int(entity_id), int(payload.get("from_entity_id")), from_user_email, to_user_email))
    # Whether this call ADDED a row or hit an existing one. The INSERT has always
    # been idempotent on a supplied message_id, but the caller could not tell the
    # two apart — both returned {"ok": True, "message_id": ...}. An app retrying a
    # delivery needs the difference to keep an honest activity log, so say it.
    duplicate = False
    try:
        msg_id = await pool.fetchval(
            """
            INSERT INTO inbox_messages
              (message_id, thread_id,
               from_address, to_address, from_entity_id, to_entity_id,
               subject, body, body_format, message_kind, payload, delivered_at,
               from_user_email, to_user_email)
            VALUES
              (COALESCE($10::uuid, gen_random_uuid()),
               COALESCE($11::uuid, gen_random_uuid()),
               $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW(),
               $12, $13)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING message_id
            """,
            from_address, to_address,
            from_entity_id,
            to_eid,
            subject, body, body_format, message_kind,
            json.dumps(body_payload),
            supplied_message_id, supplied_thread_id,
            from_user_email, to_user_email,
        )
        if msg_id is None and supplied_message_id is not None:
            # ON CONFLICT — already inserted under same UUID (idempotent dual-write).
            msg_id = supplied_message_id
            duplicate = True
    except Exception as exc:
        logger.exception("inbox.append insert failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    # Live push: tell the open dashboard a message arrived so the thread
    # updates instantly (no poll). Best-effort — never fatal.
    #
    # NOT on a duplicate. A retrying caller (an app redelivering the same routed
    # item, the bridge replaying a dual-write) inserted nothing, so there is no
    # arrival to announce — and a ping per retry is a notification the user did
    # not earn. The row already exists and the dashboard already has it.
    try:
        if not duplicate:
            from server.main import broadcast
            await broadcast({
                "type": "inbox.message",
                "message_id": str(msg_id),
                "thread_id": str(supplied_thread_id) if supplied_thread_id else None,
                "message_kind": message_kind,
                "from_entity_id": from_entity_id,
                # CROSS-HOST ONLY. from_display_name titles the thread in the
                # UI and is a RECEIVER-stamped field: the bridge overwrites it
                # from entity_registry on the way in (peer/handlers.py:42), and
                # test_peer_lane_person pins that it is "never the sender's".
                # Reading it off a LOCAL write is therefore never right — there
                # is no peer to name — and it let a caller that passed a payload
                # through verbatim choose the name shown for its own message.
                # The slip/contact harvest below is gated on kind; this is the
                # same class of read and needs its own answer, because a future
                # local caller must not be able to reintroduce the spoof.
                "from_display_name": (
                    (body_payload or {}).get("from_display_name")
                    if _is_cross_host else None),
                "from_user_email": from_user_email,
                "to_user_email": to_user_email,
                # Which topic of the conversation it landed in (checked above),
                # so a view can light that topic's tab.
                "topic_id": (body_payload or {}).get("topic_id"),
                "direction": "outbound" if outbound else "inbound",
                "subject": subject,
                "preview": (body or "")[:140],
                "sent_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            })
    except Exception:
        pass

    # A GRANT may carry a slip the sender issued us — store it keyed by the
    # sender so we can present it when we message them back. Best-effort: never
    # fail the delivery over slip bookkeeping.
    #
    # GATED ON KIND, which it once was not. This block harvests
    # caller-controlled keys out of `payload` and turns them into rows: a
    # held_slips row, and a contacts row via upsert_contact_from_grant (which
    # forces status='active'). It ran for ANY message_kind whose payload merely
    # happened to carry a `contact_slip` dict, and `from_entity_id` is set for
    # every local self-send — so any caller that passed a payload through
    # verbatim handed the sender the ability to plant a contact with a chosen
    # handle and display name in the user's People list. Found reviewing the new
    # app-delivery route, which did exactly that pass-through; the route is
    # narrowed too, but the authority belongs HERE, where every caller passes.
    #
    # Only a grant issues a slip: endpoint_grant (contacts.py:607) and the
    # reciprocal permission_grant (:573 below). The peer lane already holds this
    # line for its own inner payloads — peer/handlers.py:447 _ALLOWED_GRANT_KEYS.
    contact_slip = body_payload.get("contact_slip")
    if (isinstance(contact_slip, dict) and from_entity_id
            and message_kind in ("endpoint_grant", "permission_grant")
            and await _grant_admissible(pool, message_kind, from_entity_id,
                                        from_user_email=from_user_email,
                                        human=body_payload.get("kind") == "human")):
        try:
            terms = contact_slip.get("terms") or {}
            await pool.execute(
                """
                INSERT INTO held_slips (peer_entity_id, slip, contract_id, expires_at, updated_at)
                VALUES ($1, $2::jsonb, $3, $4::timestamptz, now())
                ON CONFLICT (peer_entity_id) DO UPDATE
                  SET slip = EXCLUDED.slip,
                      contract_id = EXCLUDED.contract_id,
                      expires_at = EXCLUDED.expires_at,
                      updated_at = now()
                """,
                int(from_entity_id), json.dumps(contact_slip),
                contact_slip.get("contract_id"),
                # asyncpg binds a timestamptz param from a datetime, NOT a str —
                # the slip's expires_at is an ISO string, so parse it (a bare
                # string silently DataError'd the whole insert, caught
                # by the live 3↔102 round-trip).
                _dt.datetime.fromisoformat(terms["expires_at"]) if terms.get("expires_at") else None,
            )
        except Exception as exc:
            logger.warning("held_slip store failed (non-fatal): %s", exc)
        # The grant also tells us WHO accepted — record them as a contact so the
        # requester's side has the same row the acceptor's side wrote (until now
        # only the acceptor got a contact; the requester held a slip and nothing
        # else, so its People/Peers send path had no handle and no peer id to
        # find the slip by). A person's grant (from_user_email stamped by the
        # bridge) becomes a human contact keyed by their email; a host's grant a
        # host contact keyed by its handle. Best-effort, never fails delivery.
        # upsert_contact_from_grant: server/routes/contacts.py:266 (v123).
        try:
            grant_kind = "human" if (
                body_payload.get("kind") == "human" or from_user_email) else "host"
            grant_handle = from_user_email if grant_kind == "human" else (
                body_payload.get("from_handle") or None)
            grant_name = (body_payload.get("from_user_name")
                          if grant_kind == "human" else body_payload.get("from_display_name"))
            if grant_handle:
                from server.routes.contacts import upsert_contact_from_grant
                await upsert_contact_from_grant(
                    pool, handle=str(grant_handle), kind=grant_kind,
                    display_name=grant_name, peer_entity_id=int(from_entity_id))
        except Exception as exc:
            logger.warning("contact upsert from grant failed (non-fatal): %s", exc)
        # WHAT THAT HOST SHARED ABOUT ITSELF — its owner's name and its number —
        # on this accepted connection. Host grants only, and
        # only past the same admission the slip passed; the number is kept only
        # if the platform says it is that host's (server/hosts/peer_cards.py).
        if isinstance(body_payload.get("card"), dict) and not (
                body_payload.get("kind") == "human" or from_user_email):
            try:
                from server.hosts.peer_cards import store_peer_card
                await store_peer_card(pool, int(from_entity_id), body_payload["card"])
            except Exception as exc:
                logger.warning("peer card store from grant failed (non-fatal): %s", exc)
        # RECIPROCATE. The acceptor issued US a slip; until now nothing issued
        # them one back, so the acceptor's first reply was refused by our pod
        # ("host-to-host message requires a live permission slip") and the
        # connection looked broken from their side.
        # Issue a matching dm:contact slip to them and deliver it as a
        # permission_grant — the same contact_slip path stores it in their
        # held_slips. Only an endpoint_grant triggers this, so it cannot loop.
        if message_kind == "endpoint_grant" and not outbound:
            grantee = int(from_entity_id)

            # After the ack, not before it: two bridge round-trips must not
            # hold up the grant's delivery receipt (pinned so the loop's weak
            # reference cannot drop the task).
            import asyncio as _aio
            t = _aio.get_event_loop().create_task(reciprocate_grant(grantee))
            _background_tasks.add(t)
            t.add_done_callback(_background_tasks.discard)

    return {"ok": True, "message_id": str(msg_id), "duplicate": duplicate}


async def _grant_admissible(pool, message_kind: str, from_entity_id, *,
                            from_user_email: str | None = None, human: bool = False) -> bool:
    """May this grant's carried slip be kept, its sender made a contact, and
    its card stored — and, for an endpoint_grant, OUR card sent back?

    ONLY FROM A RELATIONSHIP THE USER STARTED OR ACCEPTED, on EVERY transport.
      * endpoint_grant — the answer to OUR request. There must be a request of
        ours waiting on that host: a pending (or already active) contacts row
        with its id, or for a PERSON's grant a pending human contact keyed by
        their bridge-verified email. The peer lane's inbox.grant always checked
        this (handlers._relationship_open_with); the relay did not, so any cert
        holder could send an endpoint_grant, be made an active contact, have
        its card stored, and be sent our owner's name and number. Checked
        BEFORE upsert_contact_from_grant, which forces 'active'.
      * permission_grant — the reciprocal slip, owed only by a host we already
        hold as a contact.
    A blocked contact is not a relationship. A failed check keeps nothing.
    """
    try:
        if message_kind == "permission_grant":
            known = await pool.fetchval(
                "SELECT 1 FROM contacts WHERE peer_entity_id = $1 "
                "AND status IN ('pending', 'active') LIMIT 1", int(from_entity_id))
        elif human or from_user_email:
            known = await pool.fetchval(
                "SELECT 1 FROM contacts WHERE kind = 'human' "
                "AND lower(handle) = lower($1) AND status IN ('pending', 'active') LIMIT 1",
                str(from_user_email)) if from_user_email else None
        else:
            known = await pool.fetchval(
                # kind='host': a PERSON contact also records the host they are
                # reached through, and must not stand in for asking the host.
                "SELECT 1 FROM contacts WHERE kind = 'host' AND peer_entity_id = $1 "
                "AND (status = 'active' OR (status = 'pending' "
                "     AND COALESCE(last_contact_at, created_at) > now() - interval '30 days')) "
                "LIMIT 1", int(from_entity_id))
    except Exception as exc:
        logger.warning("%s from %s: relationship check failed (%s) — not kept",
                       message_kind, from_entity_id, type(exc).__name__)
        return False
    if not known:
        logger.info("%s from %s ignored: no request or contact of ours with them",
                    message_kind, from_entity_id)
    return bool(known)


async def reciprocate_grant(grantee: int) -> str | None:
    """Issue the host that accepted us a dm:contact message slip BACK, and hand it
    over — so it can message first, not only reply. Returns the lane that
    delivered it ("peer" / "relay"), or None.

    The acceptor issued US a slip; until this existed nothing issued them one
    back, so the acceptor's first message was refused by our pod ("host-to-host
    message requires a live permission slip") and the connection looked broken
    from their side.

    ONE message_id for both transports. send_to_peer raises on the READLINE, so a
    delivery can commit on the peer and still time out coming back; the relay
    would then land a second "Connection confirmed" row. handle_inbox_append
    dedupes on a SUPPLIED id, and the relay passes it through (adapter
    op_inbox_send copies params), so the second copy is a no-op.
    """
    try:
        from server.routes.contacts import _bridge_op
        issued = await _bridge_op("contract.issue", {
            "grantee_entity_id": int(grantee),
            "resource": "dm:contact",
            "allowed_ops": ["message"],
            "reasoning": "reciprocal: accepted contact may send direct messages",
            "term_seconds": 90 * 86400,
        })
        back = (issued or {}).get("slip")
        if not isinstance(back, dict):
            return None
        message_id = str(uuid.uuid4())
        # Our owner's name and number, to the host that accepted us.
        mine = None
        try:
            from server.hosts.self_card import shareable_card
            mine = await shareable_card()
        except Exception as exc:
            logger.info("own card unavailable for the reciprocal slip (%s)", type(exc).__name__)
        # THE DIRECT LANE FIRST: the relay reaches only entities on its
        # own bridge pod, so across nodes the accepter never received this. The
        # relay stays the fallback for a peer on an older image or out of reach —
        # but not after the peer ANSWERED no.
        if await _reciprocate_direct(int(grantee), back, message_id=message_id, card=mine):
            return "peer"
        await _bridge_op("inbox.send", {
            "to_entity_id": int(grantee),
            "message_kind": "permission_grant",
            "message_id": message_id,
            "subject": "Connection confirmed",
            "body": "You can message this host now.",
            "body_format": "text/plain",
            "payload": {"contact_slip": back, **({"card": mine} if mine else {})},
        })
        return "relay"
    except Exception as exc:
        logger.warning("reciprocal slip after grant failed (non-fatal): %s", exc)
        return None


async def _reciprocate_direct(grantee: int, back: dict, *, message_id: str | None = None,
                              card: dict | None = None) -> bool:
    """Deliver the reciprocal slip over the peer lane. True when the peer took
    it OR refused it (the answer is final either way); False to fall back to
    the relay (no slip held, peer unreachable, or an image without the op)."""
    try:
        from server.routes.circles import _held_slip_for
        from server.peer.peer_client import send_to_peer, PeerUnavailable
    except Exception:
        return False
    held = await _held_slip_for(grantee)
    if not held:
        return False
    try:
        resp = await send_to_peer(int(grantee), "contact.slip", held,
                                  {"contact_slip": back, "message_id": message_id,
                                   **({"card": card} if card else {})})
    except PeerUnavailable as exc:
        logger.info("reciprocal slip to %s: peer lane unavailable (%s) — relay", grantee, exc)
        return False
    except Exception as exc:
        logger.info("reciprocal slip to %s: peer lane unusable (%s) — relay",
                    grantee, type(exc).__name__)
        return False
    if isinstance(resp, dict) and resp.get("ok"):
        return True
    if isinstance(resp, dict) and resp.get("receiver"):
        logger.info("reciprocal slip to %s refused by the receiver: %s",
                    grantee, resp.get("error"))
        return True
    # A lane-level refusal: most often "unknown op" from an image without
    # contact.slip. The relay can still deliver it.
    logger.info("reciprocal slip to %s refused at the lane (%s) — relay",
                grantee, (resp or {}).get("error"))
    return False


async def handle_context_fulfill(payload: dict) -> dict:
    """Fulfil a slip-authorized context fetch (the host↔host contract layer).

    The bridge's op_context_fetch gate has ALREADY verified the grantee holds a
    valid, unexpired, unrevoked slip naming exactly this `resource` on this host (the
    no-overshare gate). We return ONLY that resource's data — scoped strictly to the
    one resource, never adjacent memory (defense in depth: even if the gate were
    bypassed, the query is bound to the named thread). `resource` is a memory-thread
    ref (source_context); we return that thread's live entries, bounded. The data
    stays home — we hand back only what the contract exposed."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    resource = (payload.get("resource") or "").strip()
    action = (payload.get("action") or "read").strip()
    if not resource:
        return {"ok": False, "error": "resource required"}
    if action != "read":
        return {"ok": False, "error": f"action '{action}' not supported on context endpoints"}
    # A blocked Host reads nothing here (contacts.block_refuses). `grantee` is
    # the fetching Host as the bridge's cert check resolved it (op_context_fetch).
    grantee = payload.get("grantee")
    if grantee is not None:
        from server.routes.contacts import block_refuses, blocked_refusal
        try:
            if await block_refuses(pool, int(grantee)):
                logger.info("context.fulfill refused: blocked grantee=%s", grantee)
                return blocked_refusal()
        except Exception as exc:
            logger.warning("context.fulfill held: block check failed (%s)", type(exc).__name__)
            return {"ok": False, "retryable": True,
                    "error": "the requester could not be checked right now — try again"}
    # resource is a thread ref: "thread:<ref>" or "<ref>". Return ONLY that thread.
    ref = resource.split("thread:", 1)[-1].strip()
    try:
        rows = await pool.fetch(
            "SELECT content, memory_type FROM entity_memory "
            "WHERE entity_id=$1 AND status='active' "
            "AND (t_valid_until IS NULL OR t_valid_until > now()) "
            "AND metadata->'source_context'->>'ref' = $2 "
            "ORDER BY importance DESC NULLS LAST LIMIT 50",
            entity_id, ref)
    except Exception as exc:
        logger.exception("context.fulfill query failed: %s", exc)
        return {"ok": False, "error": "fulfilment query failed"}
    entries = [{"content": r["content"], "type": r["memory_type"]} for r in rows]
    logger.info("context.fulfill: resource=%s grantee=%s -> %d entries",
                resource, payload.get("grantee"), len(entries))
    return {"ok": True, "resource": resource, "entries": entries, "count": len(entries)}


# ── PME→EME handlers ───────────────────────────────────────────────────


async def handle_chat_events_append(payload: dict) -> dict:
    """Insert a row into pod-local chat_events.

    Ship E PME→EME table 4/6. Phase/status entries from the request-
    pipeline lifecycle; debugging + observability.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}

    request_id = _parse_uuid(payload.get("request_id"))
    if request_id is None:
        return {"ok": False, "error": "request_id (uuid) required"}

    phase = (payload.get("phase") or "").strip()
    status = (payload.get("status") or "").strip()
    if not phase or not status:
        return {"ok": False, "error": "phase and status required"}

    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO chat_events
              (request_id, entity_id, session_id, target_kind, phase, status,
               text_preview, error_class, error_detail, elapsed_ms,
               app_id, cc_session_id, originating_app)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13)
            RETURNING id, created_at
            """,
            request_id, int(entity_id),
            _parse_uuid(payload.get("session_id")),
            payload.get("target_kind"),
            phase, status,
            payload.get("text_preview"),
            payload.get("error_class"),
            payload.get("error_detail"),
            payload.get("elapsed_ms"),
            payload.get("app_id"),
            payload.get("cc_session_id"),
            payload.get("originating_app"),
        )
    except Exception as exc:
        logger.exception("chat_events.append insert failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "id": row["id"],
        "created_at": row["created_at"].isoformat(),
    }


async def handle_behavioral_corrections_append(payload: dict) -> dict:
    """Insert a row into pod-local behavioral_corrections.

    Ship E PME→EME table 5/6. Learned corrections from feedback —
    entity-private memory about what to avoid.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}

    correction = (payload.get("correction") or "").strip()
    if not correction:
        return {"ok": False, "error": "correction required"}

    feedback_type = payload.get("feedback_type") or "correction"
    severity = payload.get("severity") or "gentle"

    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    try:
        # Invariant 19 chokepoint.
        import sys as _sys
        _sys.path.insert(0, "/app/brain")
        from mastercode.runtime.behavioral_correction_digest import apply_behavioral_correction
        result = await apply_behavioral_correction(
            pool,
            entity_id=int(entity_id),
            correction=correction,
            trigger_context=payload.get("trigger_context") or "",
            source_message=payload.get("source_message") or "",
            feedback_type=feedback_type,
            confidence=payload.get("confidence", 0.5),
            severity=severity,
            role=payload.get("role"),
            origin=payload.get("origin"),
        )
        # Re-fetch created_at since the wrapper doesn't surface it.
        created_row = await pool.fetchrow(
            "SELECT created_at FROM behavioral_corrections WHERE id = $1",
            result["id"],
        )
    except Exception as exc:
        logger.exception("behavioral.append insert failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "id": result["id"],
        "created_at": created_row["created_at"].isoformat(),
    }


async def handle_calendar_list(payload: dict) -> dict:
    """Return a paginated slice of pod-local calendar_events.

    Ship E HALF 2 read-flip: the platform's memory service dispatches this so the user's
    /api/calendar/list view comes straight from EME instead of PME. Filters
    mirror the existing list_events signature.

    Payload:
      since (ISO), until (ISO), event_type, status, limit, offset,
      order ('desc', the default: newest first; 'asc': soonest first)
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}

    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    valid_status = {"scheduled", "running", "completed", "failed", "cancelled"}
    status = payload.get("status")
    if status is not None and status not in valid_status:
        return {"ok": False, "error": f"invalid status: {status!r}"}
    # Soonest first is what "what is coming up" needs (the Calendar app's
    # Upcoming, Circles' Coming up): newest first from `since` returns the
    # farthest event first. The SQL gets one of two fixed words, never the
    # caller's text.
    order = payload.get("order") or "desc"
    if order not in ("asc", "desc"):
        return {"ok": False, "error": f"invalid order: {order!r}"}
    direction = "ASC" if order == "asc" else "DESC"

    clauses = ["entity_id = $1"]
    params: list = [int(entity_id)]
    since = _parse_ts(payload.get("since"))
    until = _parse_ts(payload.get("until"))
    if since is not None:
        params.append(since)
        clauses.append(f"event_at >= ${len(params)}::timestamptz")
    if until is not None:
        params.append(until)
        clauses.append(f"event_at <= ${len(params)}::timestamptz")
    event_type = payload.get("event_type")
    if event_type:
        params.append(event_type)
        clauses.append(f"event_type = ${len(params)}")
    if status:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    limit = max(1, min(int(payload.get("limit", 100)), 500))
    params.append(limit)
    # A page past the first `limit`.
    offset = max(0, int(payload.get("offset", 0) or 0))
    params.append(offset)

    # `id` breaks ties between rows at the same moment, so a page never
    # repeats or skips a row the page before it ended on. `recurrence` lets a
    # list row say a task repeats without reading the row in full.
    sql = (
        "SELECT event_id, entity_id, event_type, event_at, summary, status, "
        "       source, cost_usd, tokens_in, tokens_out, model_id, "
        "       related_message_id, related_artifact_uri, related_project_id, "
        "       cancelled_reason, recurrence "
        "  FROM calendar_events WHERE " + " AND ".join(clauses) +
        f" ORDER BY event_at {direction}, id {direction}"
        f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    )
    try:
        rows = await pool.fetch(sql, *params)
    except Exception as exc:
        logger.exception("calendar.list failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    def _ser(r):
        d = dict(r)
        if d.get("event_id") is not None:
            d["event_id"] = str(d["event_id"])
        if d.get("event_at") is not None:
            d["event_at"] = d["event_at"].isoformat()
        if d.get("related_message_id") is not None:
            d["related_message_id"] = str(d["related_message_id"])
        if d.get("cost_usd") is not None:
            d["cost_usd"] = float(d["cost_usd"])
        d.update(_held_fields(d.get("status"), d.get("cancelled_reason")))
        return d

    return {
        "ok": True,
        "entity_id": int(entity_id),
        "count": len(rows),
        "events": [_ser(r) for r in rows],
        "offset": offset,
        "next_offset": (offset + len(rows)) if len(rows) == limit else None,
    }


async def handle_calendar_today(payload: dict) -> dict:
    """Today's calendar events (UTC) from pod-local EME."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    try:
        rows = await pool.fetch(
            "SELECT event_id, event_type, event_at, summary, status, cost_usd, "
            "       cancelled_reason "
            "  FROM calendar_events "
            " WHERE entity_id = $1 "
            "   AND event_at::date = (NOW() AT TIME ZONE 'UTC')::date "
            " ORDER BY event_at DESC LIMIT 200",
            int(entity_id),
        )
    except Exception as exc:
        logger.exception("calendar.today failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "entity_id": int(entity_id),
        "count": len(rows),
        "events": [
            {
                "event_id":  str(r["event_id"]),
                "event_type": r["event_type"],
                "event_at":  r["event_at"].isoformat(),
                "summary":   r["summary"],
                "status":    r["status"],
                "cost_usd":  float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                "cancelled_reason": r["cancelled_reason"],
                **_held_fields(r["status"], r["cancelled_reason"]),
            }
            for r in rows
        ],
    }


async def handle_inbox_list(payload: dict) -> dict:
    """Pod-local inbox_messages list, scoped to this entity (as recipient)."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    limit = max(1, min(int(payload.get("limit", 50)), 200))
    # A page, never a wall: `offset` reaches the
    # rows past the first page.
    offset = max(0, int(payload.get("offset", 0) or 0))
    include_archived = bool(payload.get("include_archived", False))

    query = """
        SELECT message_id, thread_id, from_address, to_address,
               subject, body_format, message_kind,
               sent_at, delivered_at, read_at, archived_at,
               from_entity_id, to_entity_id, from_user_email,
               payload->>'decision_id' AS decision_id
          FROM inbox_messages
         WHERE to_entity_id = $1
    """
    if not include_archived:
        query += " AND archived_at IS NULL"
    query += " ORDER BY sent_at DESC LIMIT $2 OFFSET $3"
    try:
        rows = await pool.fetch(query, int(entity_id), limit, offset)
    except Exception as exc:
        logger.exception("inbox.list failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    def _ser(r):
        return {
            "message_id":     str(r["message_id"]),
            "thread_id":      str(r["thread_id"]),
            "from":           r["from_address"],
            "to":             r["to_address"],
            "subject":        r["subject"],
            "body_format":    r["body_format"],
            "kind":           r["message_kind"],
            "sent_at":        r["sent_at"].isoformat() if r["sent_at"] else None,
            "delivered_at":   r["delivered_at"].isoformat() if r["delivered_at"] else None,
            "read_at":        r["read_at"].isoformat() if r["read_at"] else None,
            "archived_at":    r["archived_at"].isoformat() if r["archived_at"] else None,
            "from_entity_id": r["from_entity_id"],
            "to_entity_id":   r["to_entity_id"],
            # The VERIFIED person behind the message (column, set only from the
            # bridge-checked value) — the UI decides "a person asked" from this,
            # never from anything the sender put in the payload.
            "from_user_email": r["from_user_email"],
            # A decision's own id, so a link to it (`decision:<id>`, Circles)
            # can find its message in the list. The list carries no payload;
            # this is the one identifier from it a reader needs.
            "decision_id":    (int(r.get("decision_id")) if str(r.get("decision_id") or "").isdigit() else None),
        }

    return {
        "ok": True,
        "entity_id": int(entity_id),
        "count":     len(rows),
        "messages":  [_ser(r) for r in rows],
        "offset":    offset,
        # A full page may have more behind it; a short one is the end.
        "next_offset": (offset + len(rows)) if len(rows) == limit else None,
    }


async def handle_inbox_unread_count(payload: dict) -> dict:
    """Count of unread inbox_messages for this entity."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    try:
        n = await pool.fetchval(
            "SELECT count(*) FROM inbox_messages "
            "WHERE to_entity_id = $1 AND read_at IS NULL AND archived_at IS NULL",
            int(entity_id),
        )
    except Exception as exc:
        logger.exception("inbox.unread_count failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "entity_id": int(entity_id), "unread": int(n or 0)}


# ── Ship E read-flip batch 3: by-row endpoints ─────────────────────────


def _held_fields(status, reason) -> dict:
    """{"held": True, "state": ...} for a request kept for the person's next
    message (status 'cancelled', reason held_for_person); {} for anything else."""
    from mastercode.circles_threads import HELD_FOR_PERSON_REASON
    if status == "cancelled" and reason == HELD_FOR_PERSON_REASON:
        return {"held": True,
                "state": "held: runs with the person's next message in its thread"}
    return {}


async def handle_calendar_read(payload: dict) -> dict:
    """Single calendar event by id. The caller (the platform's memory service) has already
    used PME as a routing index to pick this pod. We just return the
    row from EME."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    event_id = _parse_uuid(payload.get("event_id"))
    if event_id is None:
        return {"ok": False, "error": "event_id (uuid) required"}
    row = await pool.fetchrow(
        "SELECT * FROM calendar_events WHERE event_id=$1 AND entity_id=$2",
        event_id, int(entity_id),
    )
    if row is None:
        return {"ok": False, "error": "not found"}
    p = row["payload"]
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    return {
        "ok": True,
        "event_id":             str(row["event_id"]),
        "entity_id":            row["entity_id"],
        "event_type":           row["event_type"],
        "event_at":             row["event_at"].isoformat(),
        "summary":              row["summary"],
        "status":               row["status"],
        "source":               row["source"],
        "recurrence":           row["recurrence"],
        "payload":              p,
        "cost_usd":             float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        "tokens_in":            row["tokens_in"],
        "tokens_out":           row["tokens_out"],
        "model_id":             row["model_id"],
        "related_message_id":   str(row["related_message_id"]) if row["related_message_id"] else None,
        "related_artifact_uri": row["related_artifact_uri"],
        "related_project_id":   row["related_project_id"],
        # Why a cancelled row was cancelled. A request the identity hold is
        # keeping for the person's next message is stored 'cancelled' with the
        # reason held_for_person; `held` and `state` say so in plain words, so
        # neither the Host nor the calendar pane reads it as a failure.
        "cancelled_reason":     row.get("cancelled_reason"),
        **_held_fields(row["status"], row.get("cancelled_reason")),
    }


async def handle_calendar_runs(payload: dict) -> dict:
    """Execution history for a scheduled_task calendar event.

    The offline_task_runner appends one calendar_event_runs row per run.
    That child table keys on the integer SERIAL PK (calendar_events.id),
    NOT the UUID event_id the UI carries — so we resolve the UUID to the
    integer id (entity-scoped) first, then return the runs newest-first.

    `limit` (with `offset`) returns one page and says where the next one
    starts, with the count of every run: an hourly task gathers about 720
    runs a month, and the Calendar app shows the newest page with a way to
    older ones. Without `limit` every run comes back, as it always has (the
    Host's calendar_read op='runs').
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    event_id = _parse_uuid(payload.get("event_id"))
    if event_id is None:
        return {"ok": False, "error": "event_id (uuid) required"}
    try:
        limit = payload.get("limit")
        limit = None if limit is None else max(1, min(int(limit), 500))
        offset = max(0, int(payload.get("offset", 0) or 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid limit or offset"}
    parent = await pool.fetchrow(
        "SELECT id FROM calendar_events WHERE event_id=$1 AND entity_id=$2",
        event_id, int(entity_id),
    )
    if parent is None:
        return {"ok": False, "error": "not found"}
    args: list = [parent["id"], int(entity_id)]
    # `id` keeps two runs stamped the same moment in one order across pages.
    sql = ("SELECT ran_at, ok, output_excerpt, error "
           "  FROM calendar_event_runs "
           " WHERE event_id=$1 AND entity_id=$2 "
           " ORDER BY ran_at DESC, id DESC")
    if limit is not None:
        args += [limit, offset]
        sql += " LIMIT $3 OFFSET $4"
    elif offset:
        args.append(offset)
        sql += " OFFSET $3"
    try:
        rows = await pool.fetch(sql, *args)
        total = None
        if limit is not None:
            total = await pool.fetchval(
                "SELECT count(*) FROM calendar_event_runs WHERE event_id=$1 AND entity_id=$2",
                parent["id"], int(entity_id),
            )
    except Exception as exc:
        logger.exception("calendar.runs failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    runs = [
        {
            "ran_at": r["ran_at"].isoformat() if r["ran_at"] is not None else None,
            "ok": bool(r["ok"]),
            "output_excerpt": r["output_excerpt"],
            "error": r["error"],
        }
        for r in rows
    ]
    out = {
        "ok": True,
        "event_id": str(event_id),
        "count": len(runs),
        "runs": runs,
    }
    if limit is not None:
        total = int(total or 0)
        out.update({
            "total": total,
            "offset": offset,
            "next_offset": (offset + len(runs)) if offset + len(runs) < total else None,
        })
    return out


async def handle_calendar_cancel(payload: dict) -> dict:
    """Cancel a scheduled/running event. State machine enforced at pod."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    event_id = _parse_uuid(payload.get("event_id"))
    if event_id is None:
        return {"ok": False, "error": "event_id (uuid) required"}
    reason = payload.get("reason")
    row = await pool.fetchrow(
        "SELECT status FROM calendar_events WHERE event_id=$1 AND entity_id=$2",
        event_id, int(entity_id),
    )
    if row is None:
        return {"ok": False, "error": "not found"}
    if row["status"] not in ("scheduled", "running"):
        return {"ok": False, "error": f"cannot cancel from status={row['status']}"}
    await pool.execute(
        "UPDATE calendar_events SET status='cancelled', cancelled_reason=$1, "
        "       updated_at=NOW() WHERE event_id=$2 AND entity_id=$3",
        reason, event_id, int(entity_id),
    )
    return {"ok": True, "event_id": str(event_id), "status": "cancelled"}


async def handle_calendar_complete(payload: dict) -> dict:
    """Mark an event completed, merge `result` into payload jsonb."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    event_id = _parse_uuid(payload.get("event_id"))
    if event_id is None:
        return {"ok": False, "error": "event_id (uuid) required"}
    result_payload = payload.get("result")

    row = await pool.fetchrow(
        "SELECT payload FROM calendar_events WHERE event_id=$1 AND entity_id=$2",
        event_id, int(entity_id),
    )
    if row is None:
        return {"ok": False, "error": "not found"}
    p = row["payload"] or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    merged = dict(p) if isinstance(p, dict) else {}
    if isinstance(result_payload, dict):
        merged["result"] = result_payload
    await pool.execute(
        "UPDATE calendar_events SET status='completed', payload=$1::jsonb, "
        "       updated_at=NOW() WHERE event_id=$2 AND entity_id=$3",
        json.dumps(merged), event_id, int(entity_id),
    )
    return {"ok": True, "event_id": str(event_id), "status": "completed"}


async def handle_inbox_read(payload: dict) -> dict:
    """Fetch a single inbox message, mark it read."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    message_id = _parse_uuid(payload.get("message_id"))
    if message_id is None:
        return {"ok": False, "error": "message_id (uuid) required"}
    row = await pool.fetchrow(
        """
        UPDATE inbox_messages
           SET read_at = COALESCE(read_at, NOW())
         WHERE message_id = $1 AND to_entity_id = $2
        RETURNING message_id, thread_id, from_address, to_address,
                  subject, body, body_format, message_kind, payload,
                  sent_at, delivered_at, read_at, archived_at,
                  from_entity_id, to_entity_id, from_user_email
        """,
        message_id, int(entity_id),
    )
    if row is None:
        return {"ok": False, "error": "not found"}
    p = row["payload"]
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    return {
        "ok": True,
        "message_id":     str(row["message_id"]),
        "thread_id":      str(row["thread_id"]),
        "from":           row["from_address"],
        "to":             row["to_address"],
        "subject":        row["subject"],
        "body":           row["body"],
        "body_format":    row["body_format"],
        "kind":           row["message_kind"],
        "payload":        p,
        "sent_at":        row["sent_at"].isoformat() if row["sent_at"] else None,
        "delivered_at":   row["delivered_at"].isoformat() if row["delivered_at"] else None,
        "read_at":        row["read_at"].isoformat() if row["read_at"] else None,
        "archived_at":    row["archived_at"].isoformat() if row["archived_at"] else None,
        "from_entity_id": row["from_entity_id"],
        "to_entity_id":   row["to_entity_id"],
        "from_user_email": row["from_user_email"],
    }


async def handle_inbox_archive(payload: dict) -> dict:
    """Set archived_at for a single inbox message."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}
    message_id = _parse_uuid(payload.get("message_id"))
    if message_id is None:
        return {"ok": False, "error": "message_id (uuid) required"}
    result = await pool.execute(
        "UPDATE inbox_messages SET archived_at = COALESCE(archived_at, NOW()) "
        "WHERE message_id=$1 AND to_entity_id=$2",
        message_id, int(entity_id),
    )
    affected = int(result.split()[-1]) if result else 0
    if not affected:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "message_id": str(message_id), "archived": True}


async def handle_calendar_aggregate_day(payload: dict) -> dict:
    """Aggregate calendar_events counts for a single UTC day.

    Used by the platform memory service's daily-report endpoint. The endpoint then
    composes a system_notification inbox message from these metrics +
    dispatches inbox.append (which has its own dual-write).
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "error": "pool not ready", "retryable": True}
    entity_id = _entity_id()
    if not entity_id:
        return {"ok": False, "error": "host id unknown", "retryable": True}

    date_arg = payload.get("date")  # ISO date or None → today
    if date_arg:
        # A date object: asyncpg binds `$n::date` only from a datetime.date and
        # raised DataError on the ISO string, so a summary for any given day
        # failed (found in the same sweep as the vote bind).
        try:
            day = _dt.date.fromisoformat(str(date_arg)[:10])
        except ValueError:
            return {"ok": False, "error": "date must be YYYY-MM-DD"}
        where_date = "event_at::date = $2::date"
        params = [int(entity_id), day]
    else:
        where_date = "event_at::date = (NOW() AT TIME ZONE 'UTC')::date"
        params = [int(entity_id)]

    try:
        agg = await pool.fetchrow(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE event_type='pr_shipped')         AS prs,
              COUNT(*) FILTER (WHERE event_type='artifact_created')   AS artifacts,
              COUNT(*) FILTER (WHERE event_type='app_built')          AS apps_built,
              COUNT(*) FILTER (WHERE event_type='app_installed')      AS apps_installed,
              COUNT(*) FILTER (WHERE event_type='research_task')      AS research,
              COUNT(*) FILTER (WHERE event_type='permission_granted') AS perms,
              COALESCE(SUM(cost_usd) FILTER (WHERE event_type='inference_run'), 0)::float8 AS cost_usd,
              COALESCE(SUM(tokens_in)  FILTER (WHERE event_type='inference_run'), 0)::int  AS t_in,
              COALESCE(SUM(tokens_out) FILTER (WHERE event_type='inference_run'), 0)::int  AS t_out,
              COUNT(*) AS total_events
              FROM calendar_events
             WHERE entity_id = $1 AND {where_date}
            """,
            *params,
        )
    except Exception as exc:
        logger.exception("calendar.aggregate_day failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "entity_id":    int(entity_id),
        "date":         date_arg or "today",
        "metrics": {
            "prs":            int(agg["prs"]),
            "artifacts":      int(agg["artifacts"]),
            "apps_built":     int(agg["apps_built"]),
            "apps_installed": int(agg["apps_installed"]),
            "research":       int(agg["research"]),
            "perms":          int(agg["perms"]),
            "cost_usd":       float(agg["cost_usd"]),
            "t_in":           int(agg["t_in"]),
            "t_out":          int(agg["t_out"]),
            "total_events":   int(agg["total_events"]),
        },
    }
