"""The peer lane carries APPS.

Every op on the direct host<->host lane was host-level: a knock, its grant, a
direct message, a machine token. An app installed on two hosts had no way to
reach itself on the other one. This module adds the one op family that closes
that, on the lane's existing terms, so the app never touches a cert or a slip:

  app.grant    A -> B   "my app <id> may be called by your app <id>". Carries a
                        slip A issued (bridge-signed) naming B as grantee with
                        allowed_ops [app:<id>]. Gated on the MESSAGE slip B
                        already issued A — i.e. B accepted A as a contact.
  app.deliver  B -> A   one message from B's app to A's app. Gated on that app
                        slip: the action verified is app:<id> for the app named
                        in the params, so a slip for app X can never carry app Y.

WHO is still the client cert (peer_server), WHAT is still a slip this host
issued. The app on each side sees only /api/peer/app (server/routes/peer_apps.py).

STORE-AND-FORWARD, BOTH ENDS. The lane is one request/response per connection
and PeerUnavailable is the common case (the peer is a person's pod; it is
often asleep). So a send is written to peer_app_outbox BEFORE the first attempt
and a loop retries it with backoff until it expires; and a delivery is written
to peer_app_inbox BEFORE any attempt to hand it to a live app process, so an
app that is not running — or is static and has no process at all — still gets
everything. Live hand-off (POST /peer/<action> on the app's own port) is an
optimisation for apps that want a synchronous answer; it is never the record.

A REFUSAL IS FINAL, a transport failure is not. peer_server marks a receiver's
own no with receiver:true (and a slip refusal with "slip refused"); both are
authoritative and the row goes to `refused` on the spot — retrying a refused
send down the same gate is the mistake deliver.py's comment already names.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
import uuid
from typing import Any

log = logging.getLogger("peer.app_lane")

OP_DELIVER = "app.deliver"
OP_GRANT = "app.grant"

#: The slip action an app op needs. One string per app, so allowed_ops on a
#: contract names exactly which app it opens.
def app_action(app_id: str) -> str:
    return f"app:{app_id}"


#: A GROUP-scoped app grant. The slip's `resource` carries the group binding; `allowed_ops` is
#: unchanged, so verify_slip and the bridge need no change at all — `resource`
#: is a free string the bridge signs and carries through, and verify_slip returns it in
#: `info` (server/peer/slip_verify.py:80-87). The binding is therefore inside
#: the bridge-signed terms and cannot be edited by the presenter.
#:
#:   per-contact grant   resource = "app:<app_id>"
#:   group grant         resource = "group:<group_id>:app:<app_id>"
def group_app_resource(group_id: str, app_id: str) -> str:
    return f"group:{group_id}:app:{app_id}"


def group_from_resource(resource) -> str | None:
    """The group a slip's resource names, or None for a per-contact slip.

    Deliberately strict: anything that is not exactly the group-scoped shape
    reads as "no group", which means the caller falls back to the per-contact
    rules rather than to an empty group id that would match no roster and
    silently refuse every delivery.
    """
    if not isinstance(resource, str) or not resource.startswith("group:"):
        return None
    rest = resource[len("group:"):]
    gid, sep, tail = rest.partition(":app:")
    if not sep or not gid or not tail:
        return None
    from server.groups.roster import valid_group_id
    return gid if valid_group_id(gid) else None


#: app ids are manifest ids (registry.py); the same charset the install path
#: accepts. Checked here too because an app_id reaches SQL and a URL on the
#: receiving side and comes off the wire from another tenant.
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
#: A wire action is a short token the app defines (probe, msg, vouch, ...).
_ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

#: peer_server refuses a frame over 256KB; leave room for the envelope.
MAX_PAYLOAD_BYTES = 200 * 1024
#: The largest live answer an app process may hand back through the lane.
MAX_RESPONSE_BYTES = 64 * 1024

#: Retry schedule for the outbox: 30s, 1m, 2m, 4m ... capped at an hour. A
#: sleeping pod wakes in minutes, not seconds, so a tight loop buys nothing.
RETRY_BASE_S = 30
RETRY_MAX_S = 3600
TICK_S = 20
BATCH = 50
#: How long a send waits for its peer before it is given up on.
OUTBOX_TTL_DAYS = 7
#: The relationship term an app slip is issued for — same as a contact's.
GRANT_TERM_S = 90 * 86400


def valid_app_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_APP_ID_RE.match(v))


def valid_action(v: Any) -> bool:
    return isinstance(v, str) and bool(_ACTION_RE.match(v))


def action_for_deliver(params: dict) -> str | None:
    """The slip action app.deliver must be authorized for, read off the request.
    None when the params name no valid app — peer_server then refuses the op
    as unknown rather than verifying against an empty action."""
    app_id = (params or {}).get("app_id")
    return app_action(app_id) if valid_app_id(app_id) else None


def _payload_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        return len(json.dumps(payload, separators=(",", ":"))) <= MAX_PAYLOAD_BYTES
    except (TypeError, ValueError):
        return False


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_dt(v):
    if not v:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# RECEIVER — handlers registered into PeerServer (post-gate position).
# ═══════════════════════════════════════════════════════════════════════════

async def app_grant(peer_id: int, _slip_info: dict, params: dict) -> dict:
    """A peer hands us a slip that lets OUR app <id> call THEIR app <id>.

    peer_server has already verified the presented slip authorizes `message`
    and was issued by us — so this peer is a contact we accepted. What we do
    with the carried slip is store it; it is verified where it is presented
    (by the peer, against the bridge keys it pinned). We check only the shape
    and that its terms name the two of us the right way round, so a peer cannot
    park a third host's slip in our table under its own id.
    """
    app_id = params.get("app_id")
    if not valid_app_id(app_id):
        return {"ok": False, "error": "app_id missing or malformed"}
    slip = params.get("app_slip")
    if not isinstance(slip, dict) or not isinstance(slip.get("terms"), dict):
        return {"ok": False, "error": "app_slip missing or malformed"}
    terms = slip["terms"]
    from server import brain_service
    me = brain_service._entity_id
    try:
        issuer, grantee = int(terms.get("issuer", -1)), int(terms.get("grantee", -1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "app_slip terms malformed"}
    if issuer != int(peer_id):
        return {"ok": False, "error": "app_slip issuer is not the presenting host"}
    if grantee != int(me):
        return {"ok": False, "error": "app_slip grantee is not this host"}
    if app_action(app_id) not in (terms.get("allowed_ops") or []):
        return {"ok": False, "error": f"app_slip does not allow {app_action(app_id)}"}
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "retryable": True, "error": "this host is not ready"}
    # A blocked Host's own grants are refused (contacts.block_refuses). A
    # group-scoped one is the group's traffic and is left to its roster, but
    # only from a Host the roster holds: these terms are the sender's own word
    # (nothing here checks the carried slip's signature), so naming a group is
    # not by itself a way round the block. is_current_member fails closed.
    named_group = group_from_resource(terms.get("resource"))
    from server.groups.roster import is_current_member
    if named_group is None or not await is_current_member(pool, named_group, int(peer_id)):
        from server.peer.handlers import _block_refusal
        refusal = await _block_refusal(peer_id)
        if refusal is not None:
            return refusal
    # Same bound as app_deliver: a slip is only worth holding for an app that
    # is installed here and asked for peer traffic. Without this an accepted
    # contact could grow the table by one row per distinct app_id string.
    ok, why, transient = await _app_accepts_peers(pool, app_id)
    if not ok:
        return {"ok": False, "retryable": transient, "error": why}
    try:
        await store_app_slip(pool, int(peer_id), app_id, slip)
    except Exception as e:
        log.warning("peer app.grant from=%s app=%s store failed: %s", peer_id, app_id, type(e).__name__)
        return {"ok": False, "retryable": True, "error": "could not store the slip"}
    log.info("peer app.grant from=%s app=%s stored (contract %s)", peer_id, app_id, slip.get("contract_id"))
    return {"ok": True, "stored": True, "app_id": app_id}


async def app_deliver(peer_id: int, slip_info: dict, params: dict) -> dict:
    """One message from the peer's app into ours.

    peer_server verified the slip's allowed_ops carries app:<app_id> for the
    app_id in these params (action_for_deliver) — so the sender holds a grant
    for THIS app, issued by us. Here we check the app is installed and asked to
    be reachable, store the row (idempotent on the sender's message_id), and,
    if the app has a live process, hand it over for a synchronous answer.
    """
    app_id = params.get("app_id")
    action = params.get("action")
    if not valid_app_id(app_id) or not valid_action(action):
        return {"ok": False, "error": "app_id or action missing or malformed"}
    # Belt and braces: the server derived the verified action from the same
    # params, but a handler must not depend on the wiring above it.
    if isinstance(slip_info, dict) and app_action(app_id) not in (slip_info.get("allowed_ops") or []):
        return {"ok": False, "error": "slip does not cover this app"}
    payload = params.get("payload") or {}
    if not _payload_ok(payload):
        return {"ok": False, "error": f"payload must be an object under {MAX_PAYLOAD_BYTES} bytes"}
    try:
        message_id = uuid.UUID(str(params.get("message_id")))
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_id must be a UUID"}

    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "retryable": True, "error": "this host is not ready"}

    group_id = group_from_resource((slip_info or {}).get("resource")
                                   if isinstance(slip_info, dict) else None)
    # A blocked Host's own deliveries are refused (contacts.block_refuses),
    # before it learns anything about the app. Under a group-scoped slip the
    # delivery is the group's traffic, which its roster governs (below).
    if group_id is None:
        from server.peer.handlers import _block_refusal
        refusal = await _block_refusal(peer_id)
        if refusal is not None:
            return refusal

    # The app must be installed here AND have declared it wants peer traffic.
    # A slip proves the USER granted the peer; the scope proves the APP asked
    # to receive — without it rows would land in a table nothing reads.
    ok, why, transient = await _app_accepts_peers(pool, app_id)
    if not ok:
        return {"ok": False, "retryable": transient, "error": why}

    # A GROUP-scoped slip carries a second condition the slip alone cannot
    # express: the sender must still be on the roster. This is the point of
    # replicating the roster to every member — the check is LOCAL, so a removal
    # bites here without a bridge call and while the group's owner is asleep.
    #
    # It is layer 1 of three. Layer 2 is contract.revoke plus the
    # CRL, which is what stops a member whose replica is behind; layer 3 is the
    # 90-day term. Layer 1 alone is not sufficient and is not claimed to be.
    if group_id is not None:
        from server.groups.roster import is_current_member
        if not await is_current_member(pool, group_id, int(peer_id)):
            log.info("peer app.deliver from=%s app=%s REFUSED: not a current member of %s",
                     peer_id, app_id, group_id)
            return {"ok": False, "error": f"no longer a member of group {group_id}"}

    try:
        row_id = await pool.fetchval(
            "INSERT INTO peer_app_inbox (app_id, peer_entity_id, action, payload, message_id) "
            "VALUES ($1, $2, $3, $4::jsonb, $5) "
            "ON CONFLICT (app_id, peer_entity_id, message_id) DO NOTHING RETURNING id",
            app_id, int(peer_id), action, json.dumps(payload), message_id)
    except Exception as e:
        log.warning("peer app.deliver from=%s app=%s store failed: %s", peer_id, app_id, type(e).__name__)
        return {"ok": False, "retryable": True, "error": "could not store the delivery"}
    if row_id is None:
        # The outbox retried a send we already hold. Idempotent: same answer.
        return {"ok": True, "stored": True, "duplicate": True, "message_id": str(message_id)}

    out: dict = {"ok": True, "stored": True, "duplicate": False, "message_id": str(message_id)}
    live = await _hand_to_live_process(app_id, action, int(peer_id), str(message_id), payload)
    if live is not None:
        out["forwarded"] = True
        out["response"] = live
        try:
            await pool.execute("UPDATE peer_app_inbox SET acked_at = now() WHERE id = $1", row_id)
        except Exception:
            pass
    else:
        out["forwarded"] = False
    return out


async def _app_accepts_peers(pool, app_id: str) -> tuple[bool, str, bool]:
    """(ok, reason, transient). ok when app_id is installed, active, and
    declared read:peer_app — the one predicate both receiver ops gate on.
    `transient` is True only for "could not check" (our lookup failed); the
    other two reasons are this host's answer about the app and are final."""
    try:
        from server.applications import registry
        app = await registry.get(pool, app_id)
    except Exception as e:
        log.warning("peer lane: app=%s registry lookup failed: %s", app_id, type(e).__name__)
        return False, "could not check the app", True
    if app is None or getattr(app, "status", "active") != "active":
        return False, f"app '{app_id}' is not installed on this host", False
    if "read:peer_app" not in (getattr(app, "permissions", None) or []):
        return False, f"app '{app_id}' does not accept peer traffic here", False
    return True, "", False


async def _hand_to_live_process(app_id: str, action: str, peer_id: int,
                                message_id: str, payload: dict) -> dict | None:
    """POST the delivery to the app's own process if one is running. Returns
    the app's JSON answer (bounded) on a 2xx, None otherwise — and None is not
    an error: the row is already stored and the app will read it."""
    try:
        from server.applications.supervisor import get_supervisor
        sup = get_supervisor()
        rt = sup.get(app_id) if sup is not None else None
        if rt is None or not rt.is_alive():
            return None
        base = rt.base_url.rstrip("/")
    except Exception:
        return None
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10, sock_connect=3)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{base}/peer/{action}",
                              json={"peer_entity_id": peer_id, "message_id": message_id,
                                    "action": action, "payload": payload},
                              headers={"X-Peer-Entity-Id": str(peer_id)}) as r:
                if r.status < 200 or r.status >= 300:
                    return None
                raw = await r.content.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    log.info("app %s live answer over %d bytes — dropped", app_id, MAX_RESPONSE_BYTES)
                    return None
                try:
                    data = json.loads(raw or b"null")
                except Exception:
                    return None
                return data if isinstance(data, dict) else {"result": data}
    except Exception as e:
        log.info("app %s live hand-off failed (%s) — stored only", app_id, type(e).__name__)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# SLIPS — the app-scoped credential, held and renewed like a contact slip.
# ═══════════════════════════════════════════════════════════════════════════

async def store_app_slip(pool, peer_id: int, app_id: str, slip: dict) -> None:
    terms = slip.get("terms") or {}
    await pool.execute(
        "INSERT INTO peer_app_slips (peer_entity_id, app_id, slip, contract_id, expires_at, updated_at) "
        "VALUES ($1, $2, $3::jsonb, $4, $5::timestamptz, now()) "
        "ON CONFLICT (peer_entity_id, app_id) DO UPDATE "
        "  SET slip = EXCLUDED.slip, contract_id = EXCLUDED.contract_id, "
        "      expires_at = EXCLUDED.expires_at, updated_at = now()",
        int(peer_id), app_id, json.dumps(slip), slip.get("contract_id"),
        _parse_dt(terms.get("expires_at")))


async def _bridge(op: str, params: dict) -> dict | None:
    try:
        from server.main import get_bridge_client
        c = get_bridge_client()
        return await c.request(op, params or {}) if c is not None else None
    except Exception as e:
        log.info("bridge %s failed: %s", op, type(e).__name__)
        return None


async def app_slip_for(pool, peer_id: int, app_id: str) -> dict | None:
    """The live app slip a peer issued us, renewed at half-life under the same
    contract (the rule circles._held_slip_for applies to a contact slip). None
    when we hold none or it is dead."""
    try:
        row = await pool.fetchrow(
            "SELECT slip, contract_id FROM peer_app_slips WHERE peer_entity_id = $1 AND app_id = $2",
            int(peer_id), app_id)
    except Exception as e:
        log.warning("app slip lookup failed: %s", type(e).__name__)
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
    now = _now()
    exp = _parse_dt(terms.get("expires_at"))
    iss = _parse_dt(terms.get("issued_at"))
    needs_renew = (exp is None or (iss is not None and now >= iss + (exp - iss) / 2)
                   or (iss is None and now >= exp))
    if needs_renew and cid:
        fresh = await _bridge("contract.renew", {"contract_id": cid})
        fslip = (fresh or {}).get("slip")
        if isinstance(fslip, dict):
            try:
                await store_app_slip(pool, int(peer_id), app_id, fslip)
            except Exception:
                pass
            s = fslip
            exp = _parse_dt((fslip.get("terms") or {}).get("expires_at"))
    if exp is not None and exp <= now:
        return None
    return s


async def issue_app_slip(peer_id: int, app_id: str, group_id: str | None = None) -> dict | None:
    """Ask the bridge to sign a slip naming the peer as grantee of app:<id> on
    this host. None if the bridge is not there — the caller queues the grant.

    With `group_id`, the RESOURCE names the group as well, which binds the
    grant to a membership the receiver re-checks at delivery time. allowed_ops
    is identical either way, so verify_slip cannot tell the two apart and does
    not need to: the group condition is enforced where the roster is, not in
    the slip verifier.
    """
    resource = group_app_resource(group_id, app_id) if group_id else app_action(app_id)
    reasoning = (f"members of group {group_id} may reach app {app_id} on this host"
                 if group_id else
                 f"app {app_id} on this host accepts calls from the same app on the peer")
    data = await _bridge("contract.issue", {
        "grantee_entity_id": int(peer_id),
        "resource": resource,
        "allowed_ops": [app_action(app_id)],
        "reasoning": reasoning,
        "term_seconds": GRANT_TERM_S,
    })
    slip = (data or {}).get("slip")
    return slip if isinstance(slip, dict) else None


async def message_slip_for(pool, peer_id: int) -> dict | None:
    """The contact (message) slip the peer issued us — what app.grant presents.
    circles owns the renew-on-use rule for it; reuse, never copy."""
    try:
        from server.routes.circles import _held_slip_for
        return await _held_slip_for(int(peer_id))
    except Exception as e:
        log.info("message slip lookup for %s failed: %s", peer_id, type(e).__name__)
        return None


async def peer_is_contact(pool, peer_id: int) -> bool:
    """An app may only be pointed at a peer the USER already accepted. Same
    predicate the machine-request gate uses; fails closed."""
    try:
        from server.peer.handlers import _connected_with
        return await _connected_with(int(peer_id))
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# OUTBOX — the sender side.
# ═══════════════════════════════════════════════════════════════════════════

async def enqueue(pool, app_id: str, peer_id: int, op: str, action: str,
                  payload: dict, message_id: uuid.UUID | None = None) -> dict:
    mid = message_id or uuid.uuid4()
    row = await pool.fetchrow(
        "INSERT INTO peer_app_outbox (app_id, peer_entity_id, op, action, payload, message_id, expires_at) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, now() + ($7 || ' days')::interval) "
        "ON CONFLICT (app_id, peer_entity_id, message_id) DO NOTHING "
        "RETURNING id, status, message_id, attempts, expires_at",
        app_id, int(peer_id), op, action or "", json.dumps(payload or {}), mid, str(OUTBOX_TTL_DAYS))
    if row is None:
        # Idempotency is per (app, peer): the existing row is THIS app's, to
        # THIS peer, never another fan-out target's or another app's.
        row = await pool.fetchrow(
            "SELECT id, status, message_id, attempts, expires_at FROM peer_app_outbox "
            "WHERE app_id = $1 AND peer_entity_id = $2 AND message_id = $3",
            app_id, int(peer_id), mid)
    out = dict(row)
    if hasattr(out.get("expires_at"), "isoformat"):
        out["expires_at"] = out["expires_at"].isoformat()   # when it is given up on, for the sender
    return out


async def grant_group(pool, app_id: str, group_id: str, me: int) -> dict:
    """Grant this app to EVERY current member of a group — one user decision
    instead of one per pair.

    What the group removes is the N² of DECISIONS and the per-pair contact
    prerequisite, not the credentials on the wire: a slip binds one grantee
    (verify_slip refuses a presenter who is not the grantee,
    server/peer/slip_verify.py:62-63) and is honoured only by its issuer
    (server/peer/peer_server.py:218), so there is still one slip per ordered
    pair. Making one slip presentable by any roster member would mean changing
    the signed terms and relaxing the presenter check in a primitive already
    load-bearing for DMs, machine tokens and the app lane. Not taken here.

    Each member is enqueued, never sent inline: a roster is many peers and most
    of them are asleep. `reconcile_group_grants` then catches members who join
    later, so the grant follows the roster instead of being a snapshot of it.
    """
    from server.groups.roster import roster as _roster
    rows = await _roster(pool, group_id)
    queued, skipped = [], []
    for r in rows:
        peer = int(r["member_entity_id"])
        if peer == int(me):
            continue
        try:
            row = await enqueue(pool, app_id, peer, OP_GRANT, "", {"group_id": group_id})
            queued.append({"peer_entity_id": peer, "message_id": str(row["message_id"]),
                           "status": row.get("status")})
        except Exception as e:
            log.warning("group %s: could not queue an app grant to %s: %s",
                        group_id, peer, type(e).__name__)
            skipped.append({"peer_entity_id": peer, "error": type(e).__name__})
    return {"group_id": group_id, "app_id": app_id,
            "queued": queued, "skipped": skipped, "members": len(rows)}


#: A group grant is LIVE while it is neither revoked nor past the term of the
#: slip it minted. Both halves are load-bearing:
#:
#:   revoked — removing a member revokes their group-scoped app slip
#:     (server/groups/lane.py revoke_group_slips), and the outbox row is
#:     stamped in the same act. Without this a member removed and then
#:     RE-ADDED never got the app again: the slip was gone, but the old
#:     delivered row still matched, so the reconciler saw a grant that no
#:     longer existed and skipped them for ever.
#:   aged    — the slip was issued for GRANT_TERM_S, and a renewal is capped to
#:     the relationship term (bridge/adapter.py:2352-2360 mints `expires`
#:     clamped to term_expires_at). The peer renews inside the term by itself
#:     (app_slip_for); at the term's end the GRANTER must issue a fresh one,
#:     and the row's created_at is when we issued.
#:
#: Scoped to the GROUP too: two groups granting the same app to the same peer
#: are two grants, each revocable on its own, not one shared row.
_LIVE_GROUP_GRANT = (
    "SELECT 1 FROM peer_app_outbox o "
    " WHERE o.app_id = $3 AND o.peer_entity_id = m.member_entity_id "
    "   AND o.op = $4 AND o.payload->>'group_id' = $1 "
    "   AND o.status <> 'revoked' "
    "   AND o.created_at > now() - ($5 || ' seconds')::interval")


async def reconcile_group_grants(pool, me: int) -> int:
    """Grant every live group app to any current member who does not hold a
    LIVE grant.

    This is what makes a group grant follow the roster: a member who joins on
    Tuesday gets Monday's grant without the user doing anything, and a member
    who is removed and later re-admitted gets a FRESH contract instead of being
    skipped for ever by the row of a revoked one.

    "Live" is _LIVE_GROUP_GRANT above — not merely "a row exists".
    """
    try:
        pairs = await pool.fetch(
            "SELECT g.group_id, g.app_id FROM group_app_grants g "
            " WHERE g.revoked_at IS NULL")
    except Exception as e:
        log.info("group app grant reconcile skipped: %s", type(e).__name__)
        return 0
    n = 0
    for p in pairs:
        gid, app_id = p["group_id"], p["app_id"]
        try:
            rows = await pool.fetch(
                "SELECT m.member_entity_id FROM group_members m "
                " WHERE m.group_id = $1 AND m.removed_at IS NULL "
                "   AND m.member_entity_id <> $2 "
                f"   AND NOT EXISTS ({_LIVE_GROUP_GRANT})",
                gid, int(me), app_id, OP_GRANT, str(GRANT_TERM_S))
        except Exception as e:
            log.warning("group %s: grant reconcile query failed: %s", gid, type(e).__name__)
            continue
        for r in rows:
            try:
                await enqueue(pool, app_id, int(r["member_entity_id"]), OP_GRANT, "",
                              {"group_id": gid})
                n += 1
            except Exception as e:
                log.warning("group %s: could not queue a grant to %s: %s",
                            gid, r["member_entity_id"], type(e).__name__)
    if n:
        log.info("group app grants: queued %d new member grant(s)", n)
    return n


def _backoff_s(attempts: int) -> int:
    return min(RETRY_BASE_S * (2 ** max(0, attempts - 1)), RETRY_MAX_S)


class LocalFailure(Exception):
    """This side could not even attempt the send — no slip on file yet, bridge
    down for a renew or an issue, pool hiccup. Nothing the peer said. RETRY."""


class FinalLocal(Exception):
    """This send can never succeed as written (a frame the receiver cannot
    read). FINAL, and the reason names it — never disguised as the peer's no."""


#: Receiver answers that describe the RECEIVER'S moment, not this message:
#: a pod that just started (CRL unsynced, pool not up), a store that failed.
#: The same send later can land, so these are retried, not filed as refused.
#: A receiver on this image says so STRUCTURALLY (`retryable: true` on the
#: envelope, peer_server) and that flag is read first; these substrings are
#: the fallback for a receiver on an image from before the flag existed.
_TRANSIENT_RECEIVER = ("not ready", "could not store", "could not check",
                       "handler error", "revocation view not fresh")


def classify(resp: dict | None, exc: BaseException | None) -> tuple[str, str]:
    """(status, reason) for one attempt's outcome — THE decision the loop makes.

      FinalLocal (frame too large)                    -> refused   (final)
      any other exception (transport, LocalFailure)   -> queued    (retry)
      receiver transient ("not ready", CRL unsynced…) -> queued    (retry)
      receiver said no (receiver:true)                -> refused   (final)
      lane refused the slip ("slip refused: …")       -> refused   (final)
      anything else (unknown op on an older image…)   -> queued    (retry)
      ok                                              -> delivered

    The first cut filed EVERY non-ok answer as refused, including this side's
    own "no slip on file" and a peer's "not ready" — a pool hiccup during a
    retry became the peer's permanent no.
    """
    if isinstance(exc, FinalLocal):
        return "refused", f"local:{str(exc)[:290]}"
    if isinstance(exc, LocalFailure):
        return "queued", f"local_unavailable:{str(exc)[:280]}"
    if exc is not None:
        return "queued", f"transport_unavailable:{type(exc).__name__}"
    if not isinstance(resp, dict):
        return "queued", "bad_response"
    if resp.get("ok"):
        return "delivered", "ok"
    err = str(resp.get("error") or "refused")
    low = err.lower()
    if "retryable" in resp:
        # A receiver on this image answered structurally; its word is final
        # either way, and prose never overrides it.
        if resp.get("retryable") is True:
            return "queued", f"peer_transient:{err[:280]}"
    elif any(t in low for t in _TRANSIENT_RECEIVER):
        # An older image sent no flag: fall back to its prose.
        return "queued", f"peer_transient:{err[:280]}"
    if resp.get("receiver"):
        return "refused", err[:300]
    if low.startswith("slip refused:"):
        return "refused", err[:300]
    return "queued", f"lane:{err[:290]}"


async def _send(row: dict) -> tuple[dict | None, BaseException | None]:
    """One attempt on the wire. Returns (response, exception)."""
    from server import brain_service
    from server.peer.peer_client import send_to_peer, encode_frame, MAX_FRAME_BYTES
    pool = brain_service.get_pool()
    peer = int(row["peer_entity_id"])
    app_id = row["app_id"]
    payload = row["payload"]
    try:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as e:
                # Cannot happen off a jsonb column; if it ever does, the row
                # can never be sent as written and must not spin the loop.
                raise FinalLocal(f"payload unreadable: {type(e).__name__}") from e
        if not isinstance(payload, dict):
            payload = {}
        if pool is None:
            raise LocalFailure("no pool")
        if row["op"] == OP_GRANT:
            slip = await message_slip_for(pool, peer)
            if slip is None:
                raise LocalFailure("no message slip from this peer on file (not a contact yet, or a lookup failed)")
            app_slip = payload.get("app_slip")
            # A group grant carries its group id on the row, so a retry mints
            # the SAME group-scoped resource rather than silently falling back
            # to a per-contact slip that would skip the membership check.
            grant_group = payload.get("group_id")
            if not isinstance(app_slip, dict):
                app_slip = await issue_app_slip(peer, app_id, grant_group)
                if app_slip is None:
                    raise LocalFailure("bridge unavailable to issue the app slip")
                # Keep the issued slip on the row so a retry re-presents the
                # same contract instead of minting a new one per attempt. Merge
                # into the payload rather than replacing it: replacing dropped
                # group_id, and the retry then re-minted an ungrouped slip.
                payload = {**payload, "app_slip": app_slip}
                try:
                    await pool.execute(
                        "UPDATE peer_app_outbox SET payload = $2::jsonb WHERE id = $1",
                        row["id"], json.dumps(payload))
                except Exception:
                    pass
            params = {"app_id": app_id, "app_slip": app_slip}
            if len(encode_frame(OP_GRANT, slip, params)) > MAX_FRAME_BYTES:
                raise FinalLocal("frame exceeds the lane's ceiling")
            resp = await send_to_peer(peer, OP_GRANT, slip, params)
            return resp, None
        slip = await app_slip_for(pool, peer, app_id)
        if slip is None:
            raise LocalFailure("no app slip from this peer on file (they have not granted this app, or a lookup failed)")
        params = {"app_id": app_id, "action": row["action"], "payload": payload,
                  "message_id": str(row["message_id"])}
        # Size the REAL frame — slip and envelope included — against what the
        # receiver's reader can hold. A frame past it is refused by the
        # receiver without a response, which looks exactly like a sleeping
        # peer and would be retried for seven days.
        if len(encode_frame(OP_DELIVER, slip, params)) > MAX_FRAME_BYTES:
            raise FinalLocal(f"frame exceeds the lane's ceiling ({MAX_FRAME_BYTES} bytes with slip and envelope)")
        resp = await send_to_peer(peer, OP_DELIVER, slip, params)
        return resp, None
    except Exception as e:
        return None, e


async def attempt(pool, row: dict) -> dict:
    """Try one outbox row now and record the outcome. Returns the updated fields."""
    resp, exc = await _send(row)
    status, reason = classify(resp, exc)
    attempts = int(row.get("attempts") or 0) + 1
    try:
        if status == "delivered":
            await pool.execute(
                "UPDATE peer_app_outbox SET status='delivered', attempts=$2, delivered_at=now(), "
                "last_error=NULL, response=$3::jsonb WHERE id=$1",
                row["id"], attempts, json.dumps((resp or {}).get("data") or {}))
        elif status == "refused":
            await pool.execute(
                "UPDATE peer_app_outbox SET status='refused', attempts=$2, last_error=$3, "
                "response=$4::jsonb WHERE id=$1",
                row["id"], attempts, reason, json.dumps((resp or {}).get("data") or {}))
        else:
            await pool.execute(
                "UPDATE peer_app_outbox SET status='queued', attempts=$2, last_error=$3, "
                "next_attempt_at = now() + ($4 || ' seconds')::interval WHERE id=$1",
                row["id"], attempts, reason, str(_backoff_s(attempts)))
    except Exception as e:
        # Bookkeeping failed AFTER the attempt. Whatever happened on the wire,
        # the row must not be re-selected on the very next tick with no
        # backoff — push next_attempt_at out, best-effort, and say why.
        log.warning("outbox row %s: could not record %s (%s) — backing off", row["id"], status, type(e).__name__)
        try:
            await pool.execute(
                "UPDATE peer_app_outbox SET attempts=$2, last_error=$3, "
                "next_attempt_at = now() + ($4 || ' seconds')::interval WHERE id=$1",
                row["id"], attempts, f"bookkeeping_failed:{type(e).__name__}", str(_backoff_s(attempts)))
        except Exception:
            pass
    return {"id": row["id"], "status": status, "reason": reason, "attempts": attempts,
            "response": (resp or {}).get("data") if isinstance(resp, dict) else None}


async def tell_sender_expired(lane: str, rows) -> None:
    """Undelivered sends reached OUTBOX_TTL_DAYS: tell the sender (this
    Host's inbox), naming what and to whom, instead of letting the row turn
    'expired' where nobody looks. Never raises."""
    rows = [dict(r) for r in (rows or [])]
    if not rows:
        return
    what = ", ".join(sorted({f"{r.get('what') or r.get('op')} to Host {r.get('peer_entity_id')}"
                             for r in rows}))
    body = (f"{len(rows)} {lane} message(s) were not delivered within {OUTBOX_TTL_DAYS} days "
            f"and were given up: {what}. The other Host was not reachable that whole time; "
            "send again once it is back.")
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        await handle_inbox_append({
            "subject": f"{len(rows)} {lane} message(s) not delivered",
            "body": body, "message_kind": "system_notification",
            "payload": {"type": "outbox_expired", "lane": lane,
                        "ttl_days": OUTBOX_TTL_DAYS,
                        "rows": [{k: (str(v) if k == "message_id" else v)
                                  for k, v in r.items()} for r in rows]}})
    except Exception as e:  # noqa: BLE001 — the expiry itself already happened
        log.warning("could not tell the sender about %d expired %s message(s): %s",
                    len(rows), lane, e)


async def tick(pool) -> int:
    """One pass over due rows. Returns how many were attempted."""
    expired = await pool.fetch(
        "UPDATE peer_app_outbox SET status='expired', last_error='expired before delivery' "
        "WHERE status='queued' AND expires_at <= now() "
        "RETURNING id, app_id AS what, peer_entity_id, op, message_id")
    await tell_sender_expired("app", expired)
    rows = await pool.fetch(
        "SELECT id, app_id, peer_entity_id, op, action, payload, message_id, attempts "
        "FROM peer_app_outbox WHERE status='queued' AND next_attempt_at <= now() "
        "ORDER BY next_attempt_at LIMIT $1", BATCH)
    n = 0
    for r in rows:
        try:
            await attempt(pool, dict(r))
        except Exception as e:
            log.warning("outbox row %s attempt raised: %s", r["id"], type(e).__name__)
        n += 1
    return n


async def run_peer_app_outbox_loop(pool, entity_id: int) -> None:
    """Background driver. Never raises; a tick that fails logs and waits."""
    log.info("peer app outbox loop up (entity %s, every %ss)", entity_id, TICK_S)
    while True:
        try:
            await tick(pool)
        except Exception as e:
            log.warning("peer app outbox tick failed: %s", type(e).__name__)
        await asyncio.sleep(TICK_S)
