"""The group lane — roster replication and vote carriage over the existing
direct host<->host transport.

Three ops, no new transport, no new port, and no change to verify_slip:

  group.grant         owner  -> member   the invite: hands a member the group
                                         slip and the group's identity. Rides
                                         the MESSAGE slip the member already
                                         issued the owner, i.e. an accepted
                                         contact — exactly app.grant's
                                         structure (server/peer/peer_server.py:43-46).
  group.roster_fetch  member -> owner    pull the roster, and register the
                                         member's verify key on the way.
  group.vote          member -> owner    submit one signed vote.

WHY A PULL. The lane's slip is issued BY THE RECEIVER and peer_server honours
only slips it issued itself (expected_issuer=self._me,
server/peer/peer_server.py:218). An owner-initiated roster PUSH would therefore
need a second credential, issued by every member to the owner, for the same
relationship. A pull needs only the one slip the owner already minted.
group.grant carries the current roster_epoch as a nudge, so a member learns
THAT it is behind without polling for it; the background poll is the repair
path, not the mechanism.

CLASSIFICATION. peer_server refuses an op that is neither in _OP_ACTION nor
_OP_ACTION_FROM_PARAMS nor _SLIPLESS_OPS (peer_server.py:170-171), and names
the reason: defaulting an unclassified op to "no slip needed" is how a gated op
silently becomes ungated. All three ops here are classified and none is
slipless — a group op always has a slip available, because the group slip is
issued before any group op is made.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
import uuid

from server.groups import roster as R
from server.groups import votes as V

log = logging.getLogger("privatae.groups.lane")

OP_GRANT = "group.grant"
#: What a member on an older image answers a new-shape group id with
#: (group_grant's own first refusal, unchanged since the lane shipped).
_OLD_IMAGE_ID_REFUSAL = "group_id missing or malformed"
OP_ROSTER_FETCH = "group.roster_fetch"
OP_VOTE = "group.vote"

#: The slip action a group op needs. One string per group, so a slip for group
#: X can never carry group Y — the property action_for_deliver was written for
#: (server/peer/app_lane.py:86-91).
def group_action(group_id: str) -> str:
    return f"group:{group_id}"


def action_for_group(params: dict) -> str | None:
    """The slip action group.roster_fetch / group.vote must be authorized for,
    read off the request. None when the params name no valid group id —
    peer_server then refuses the op as unknown rather than verifying against an
    empty action (peer_server.py:163-171)."""
    gid = (params or {}).get("group_id")
    return group_action(gid) if R.valid_group_id(gid) else None


#: One roster page. 500 is the page ceiling the peer app routes already use
#: (server/routes/peer_apps.py:220, `le=500`); a page is additionally sized
#: against the lane's frame ceiling before it is sent, because a frame past the
#: ceiling is refused with no response and looks exactly like a sleeping peer
#: (the check app_lane._send learned at app_lane.py:528-533).
ROSTER_PAGE = 500

#: The relationship term a group slip is issued for. Same as a contact's and an
#: app's (app_lane.GRANT_TERM_S) — a group membership is the same kind of
#: standing relationship.
GRANT_TERM_S = 90 * 86400

#: Background roster poll. A FLOOR ON STALENESS, not the mechanism: group.grant
#: carries the epoch and an on-demand pull is uncapped. Kubernetes informers
#: default their periodic resync to 10 minutes for the same watch-plus-repair
#: reason; this is the conservative half.
SYNC_INTERVAL_S = 300
#: The outbox tick. Matches the app lane's (app_lane.TICK_S).
TICK_S = 20
BATCH = 50
OUTBOX_TTL_DAYS = 7


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

async def group_grant(peer_id: int, _slip_info: dict, params: dict) -> dict:
    """An owner hands us the slip that lets us pull its group's roster and vote.

    peer_server has already verified the presented slip authorizes `message`
    and was issued by us — so this peer is a contact we accepted. What we check
    here is that the carried GROUP slip names the two of us the right way
    round, so a peer cannot park a third host's slip in our table under its own
    id; the same shape check app_grant makes (server/peer/app_lane.py:121-165).
    The slip itself is verified where it is PRESENTED — by the owner, against
    the bridge keys it pinned — never here.
    """
    gid = params.get("group_id")
    if not R.valid_group_id(gid):
        return {"ok": False, "error": "group_id missing or malformed"}
    slip = params.get("group_slip")
    if not isinstance(slip, dict) or not isinstance(slip.get("terms"), dict):
        return {"ok": False, "error": "group_slip missing or malformed"}
    terms = slip["terms"]
    from server import brain_service
    me = brain_service._entity_id
    try:
        issuer, grantee = int(terms.get("issuer", -1)), int(terms.get("grantee", -1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "group_slip terms malformed"}
    if issuer != int(peer_id):
        return {"ok": False, "error": "group_slip issuer is not the presenting host"}
    if grantee != int(me):
        return {"ok": False, "error": "group_slip grantee is not this host"}
    if group_action(gid) not in (terms.get("allowed_ops") or []):
        return {"ok": False, "error": f"group_slip does not allow {group_action(gid)}"}

    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "retryable": True, "error": "this host is not ready"}
    # A group id names its owner (roster._GROUP_ID_RE), and only that owner
    # may grant it. Without this, any accepted contact who knew an id (every
    # member does) could rename a group this host owns, push its roster_epoch
    # to the bigint ceiling so the next member change 500s before the
    # departing member's slip is revoked, repoint a member's group slip at
    # itself, or squat an id on a contact before the real owner's invite
    # arrived. The held-row check and the SQL guards below stay as a second
    # line: a stored row never changes owner.
    if R.group_id_owner(gid) != int(peer_id):
        log.info("group.grant from=%s group=%s refused: the id names another owner", peer_id, gid)
        return {"ok": False, "error": "that group id names a different owner"}
    try:
        held = await R.get_group(pool, gid)
    except Exception as e:
        log.warning("group.grant from=%s group=%s lookup failed: %s", peer_id, gid, type(e).__name__)
        return {"ok": False, "retryable": True, "error": "could not read the group"}
    if held is not None and int(held["owner_entity_id"]) != int(peer_id):
        log.info("group.grant from=%s group=%s refused: held for owner %s",
                 peer_id, gid, held["owner_entity_id"])
        return {"ok": False, "error": "that group is held for a different owner"}
    try:
        await store_group_slip(pool, gid, int(peer_id), slip)
        # Record the group itself so the sync loop has something to pull. The
        # roster arrives on the first fetch, not here: an invite carries a
        # credential and an identity, never a membership list the receiver
        # cannot verify the sender was entitled to send.
        await pool.execute(
            "INSERT INTO groups (group_id, owner_entity_id, name, description, roster_epoch) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (group_id) DO UPDATE "
            "   SET name = EXCLUDED.name, description = EXCLUDED.description, "
            "       roster_epoch = GREATEST(groups.roster_epoch, EXCLUDED.roster_epoch), "
            "       updated_at = now() "
            " WHERE groups.owner_entity_id = EXCLUDED.owner_entity_id",
            gid, int(peer_id), str(params.get("name") or "")[:R.MAX_NAME_LEN],
            str(params.get("description") or "")[:R.MAX_DESCRIPTION_LEN],
            max(1, int(params.get("roster_epoch") or 1)))
    except Exception as e:
        log.warning("group.grant from=%s group=%s store failed: %s", peer_id, gid, type(e).__name__)
        return {"ok": False, "retryable": True, "error": "could not store the group slip"}
    log.info("group.grant from=%s group=%s stored (contract %s)", peer_id, gid, slip.get("contract_id"))
    # Pull straight away rather than waiting up to SYNC_INTERVAL_S: the invite
    # is the moment the member most wants the roster. Best-effort — the loop is
    # the guarantee, this is only the latency.
    asyncio.create_task(_sync_one_quietly(pool, gid))
    return {"ok": True, "stored": True, "group_id": gid}


async def group_roster_fetch(peer_id: int, _slip_info: dict, params: dict) -> dict:
    """A member pulls our roster, and registers its verify key on the way.

    peer_server verified the slip's allowed_ops carries group:<group_id> for
    the group in these params (action_for_group) and that WE issued it. Here we
    check we actually own the group and the caller is a current member, pin the
    key they presented against the CERT's entity id, and answer with one page.

    The key pin is the load-bearing line: `peer_id` came from the mTLS client
    cert CN (server/peer/peer_server.py:123-125, :242), so the key is bound to
    a CEIGAS-attested identity and never to a field the caller chose.
    """
    gid = params.get("group_id")
    if not R.valid_group_id(gid):
        return {"ok": False, "error": "group_id missing or malformed"}
    from server import brain_service
    me = brain_service._entity_id
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "retryable": True, "error": "this host is not ready"}
    g = await R.get_group(pool, gid)
    if g is None or int(g["owner_entity_id"]) != int(me):
        return {"ok": False, "error": "this host does not own that group"}
    if not await R.is_current_member(pool, gid, int(peer_id)):
        return {"ok": False, "error": "not a current member of that group"}

    pinned = None
    raw = params.get("verify_pubkey")
    if isinstance(raw, str) and raw:
        import base64
        try:
            pub = base64.b64decode(raw)
        except Exception:
            pub = None
        if pub is not None:
            changed, reason = await R.pin_key(pool, gid, int(peer_id), pub)
            pinned = {"changed": changed, "reason": reason}

    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    rows = await R.roster(pool, gid, include_removed=True)
    page = [R.snapshot_member(r) for r in rows[offset:offset + ROSTER_PAGE]]
    extra = {}
    if offset == 0 and params.get("with_ballots") is True:
        # The open ballots ride on the first page only: a member cannot vote
        # on a ballot its own pod has never seen. Only when ASKED: a member on
        # an older image reads answers with a 64 KiB line limit, and ballots
        # added to a page it could read would stop it syncing at all.
        extra["ballots"] = await R.open_ballot_snapshots(pool, gid)
    return {
        **extra,
        "ok": True,
        "group_id": gid,
        "owner_entity_id": int(me),
        "name": g["name"],
        "description": g["description"],
        "roster_epoch": int(g["roster_epoch"]),
        "offset": offset,
        "total": len(rows),
        "members": page,
        "key": pinned,
    }


async def group_vote(peer_id: int, _slip_info: dict, params: dict) -> dict:
    """A member submits one signed vote to the group's owner.

    Two identities have to agree and both are checked: the CERT says who
    connected, and the SIGNED TERMS say who voted. A valid signature naming a
    different voter than the cert is refused rather than attributed to either —
    it is the one shape that would otherwise let a member relay someone else's
    vote and have it counted as arriving from them.
    """
    gid = params.get("group_id")
    ballot_id = params.get("ballot_id")
    if not R.valid_group_id(gid) or not R.valid_ballot_id(ballot_id):
        return {"ok": False, "error": "group_id or ballot_id missing or malformed"}
    vote = params.get("vote")
    if not isinstance(vote, dict) or not isinstance(vote.get("terms"), dict):
        return {"ok": False, "error": "vote missing or malformed"}
    try:
        if int(vote["terms"].get("voter", -1)) != int(peer_id):
            return {"ok": False, "error": "the signed vote names a different voter than the connection"}
    except (TypeError, ValueError):
        return {"ok": False, "error": "vote terms malformed"}

    from server import brain_service
    me = brain_service._entity_id
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "retryable": True, "error": "this host is not ready"}
    g = await R.get_group(pool, gid)
    if g is None or int(g["owner_entity_id"]) != int(me):
        return {"ok": False, "error": "this host does not own that group"}
    if not await R.is_current_member(pool, gid, int(peer_id)):
        return {"ok": False, "error": "not a current member of that group"}
    return await accept_vote(pool, gid, ballot_id, vote)


async def accept_vote(pool, group_id: str, ballot_id: str, vote: dict) -> dict:
    """Verify and store one vote. The SAME path the owner's own vote takes, so
    a host cannot admit its own vote under weaker checks than a peer's."""
    ballot = await R.get_ballot(pool, group_id, ballot_id)
    if ballot is None:
        return {"ok": False, "error": "no such ballot"}
    closes = ballot.get("closes_at")
    if closes is not None and _now() >= closes:
        return {"ok": False, "error": "that ballot is closed"}
    voter = int(vote["terms"]["voter"])
    row = await pool.fetchrow(
        "SELECT verify_pubkey FROM group_members WHERE group_id = $1 AND member_entity_id = $2",
        group_id, voter)
    pub = bytes(row["verify_pubkey"]) if row and row["verify_pubkey"] else None
    known = await R.held_seq(pool, group_id, voter)
    ok, reason, terms = V.verify_vote(
        vote, verify_pubkey=pub, group_id=group_id, ballot_id=ballot_id,
        choices=list(ballot.get("choices") or []), known_seq=known)

    if not ok and reason != V.NO_PINNED_KEY:
        # Every other failure is a refusal: a bad signature, a replayed seq, a
        # choice this ballot does not offer. Nothing is stored, because storing
        # a vote that failed verification is how an unverified row later reads
        # as an unpinned-key one.
        log.info("group %s ballot %s: vote from %s refused: %s", group_id, ballot_id, voter, reason)
        return {"ok": False, "error": reason}

    # signed_at must parse before anything is stored: the unpinned-key path
    # above returns before verify_vote reaches its own signed_at check, and a
    # value the column cannot hold is a final refusal, not a "try again".
    signed = R._snapshot_time((terms or {}).get("signed_at"))
    if signed is None or not (2020 <= signed.year <= 9998):
        log.info("group %s ballot %s: vote from %s refused: signed_at is not a timestamp",
                 group_id, ballot_id, voter)
        return {"ok": False, "error": "signed_at is not a timestamp this host can hold"}
    try:
        await R.store_vote(pool, group_id, ballot_id, vote,
                           verified=ok, verify_error=None if ok else reason)
    except Exception as e:
        log.warning("group %s: vote store failed: %s", group_id, type(e).__name__)
        return {"ok": False, "retryable": True, "error": "could not store the vote"}
    return {"ok": True, "stored": True, "verified": ok,
            "counted": ok, "reason": None if ok else reason,
            # STRUCTURAL, not prose. A caller must not have to match on the
            # reason text to learn that its vote landed but will not be
            # counted until it syncs — the same rule peer_server applies to
            # `retryable` (peer_server.py:190-195).
            "key_not_pinned": (not ok and reason == V.NO_PINNED_KEY),
            "group_id": group_id, "ballot_id": ballot_id, "seq": int(terms["seq"])}


# ═══════════════════════════════════════════════════════════════════════════
# SLIPS
# ═══════════════════════════════════════════════════════════════════════════

async def store_group_slip(pool, group_id: str, owner_id: int, slip: dict) -> None:
    terms = slip.get("terms") or {}
    await pool.execute(
        "INSERT INTO group_slips (group_id, owner_entity_id, slip, contract_id, expires_at, updated_at) "
        "VALUES ($1, $2, $3::jsonb, $4, $5::timestamptz, now()) "
        "ON CONFLICT (group_id) DO UPDATE "
        "  SET owner_entity_id = EXCLUDED.owner_entity_id, slip = EXCLUDED.slip, "
        "      contract_id = EXCLUDED.contract_id, expires_at = EXCLUDED.expires_at, "
        "      updated_at = now() "
        " WHERE group_slips.owner_entity_id = EXCLUDED.owner_entity_id",
        group_id, int(owner_id), json.dumps(slip), slip.get("contract_id"),
        _parse_dt(terms.get("expires_at")))


async def _bridge(op: str, params: dict) -> dict | None:
    try:
        from server.main import get_bridge_client
        c = get_bridge_client()
        return await c.request(op, params or {}) if c is not None else None
    except Exception as e:
        log.info("bridge %s failed: %s", op, type(e).__name__)
        return None


async def issue_group_slip(peer_id: int, group_id: str) -> dict | None:
    """Ask the bridge to sign a slip naming a member as grantee of
    group:<group_id> on this host. None if the bridge is not there — the caller
    queues the grant.

    `resource` and `allowed_ops` carry the same string on purpose: the resource
    IS the group, and the only op it opens is that group's. Nothing here needs
    a bridge change — allowed_ops is a free list and resource a free string
    (bridge/adapter.py:2352-2360).
    """
    data = await _bridge("contract.issue", {
        "grantee_entity_id": int(peer_id),
        "resource": group_action(group_id),
        "allowed_ops": [group_action(group_id)],
        "reasoning": f"member of group {group_id} on this host: may pull its roster and vote in it",
        "term_seconds": GRANT_TERM_S,
    })
    slip = (data or {}).get("slip")
    return slip if isinstance(slip, dict) else None


async def revoke_group_slips(pool, group_id: str, peer_id: int) -> int:
    """Revoke every contract this host issued that member for this group.

    Layer 2 of the three-layer removal. Layer 1 is the roster
    itself, which is immediate at every member that has pulled; this one is
    what stops a member whose replica is behind, because verify_slip refuses a
    revoked contract_id (server/peer/slip_verify.py:74-75) and peer_server
    fails CLOSED when its revocation view is stale (peer_server.py:199-208).
    """
    contract_ids = []
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT payload->'group_slip'->>'contract_id' AS cid FROM group_outbox "
            "WHERE group_id = $1 AND peer_entity_id = $2 AND op = $3",
            group_id, int(peer_id), OP_GRANT)
        contract_ids.extend(r["cid"] for r in rows)
    except Exception as e:
        log.warning("group %s: could not list group slips issued to %s: %s",
                    group_id, peer_id, type(e).__name__)
    # And the group-scoped APP slips. These are the ones that actually gate an
    # app delivery, so revoking only the group slip above would leave the thing
    # a removed member could still USE. They live on the app lane's outbox,
    # because that is the lane that minted them (app_lane._send), and they are
    # selected by the group id the row carries rather than by parsing the
    # slip's resource — the row is what the minting decision wrote down.
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT payload->'app_slip'->>'contract_id' AS cid FROM peer_app_outbox "
            "WHERE peer_entity_id = $1 AND op = $2 AND payload->>'group_id' = $3",
            int(peer_id), "app.grant", group_id)
        contract_ids.extend(r["cid"] for r in rows)
    except Exception as e:
        log.warning("group %s: could not list app slips issued to %s: %s",
                    group_id, peer_id, type(e).__name__)

    # Stamp the app-grant rows revoked in the same act. reconcile_group_grants
    # treats a revoked row as NO grant (app_lane._LIVE_GROUP_GRANT), so a
    # member removed and later re-admitted gets a fresh contract. Done whether
    # or not the bridge answers below: the roster already says they are out,
    # and a row left `delivered` would skip them for ever on re-admission —
    # trading a temporary failure for a permanent one.
    try:
        await pool.execute(
            "UPDATE peer_app_outbox SET status = 'revoked', "
            "       last_error = 'group membership revoked' "
            " WHERE peer_entity_id = $1 AND op = $2 AND payload->>'group_id' = $3 "
            "   AND status <> 'revoked'",
            int(peer_id), "app.grant", group_id)
    except Exception as e:
        log.warning("group %s: could not stamp app grant rows revoked for %s: %s",
                    group_id, peer_id, type(e).__name__)

    n = 0
    for cid in contract_ids:
        if not cid:
            continue
        if await _bridge("contract.revoke", {"contract_id": cid}) is not None:
            n += 1
        else:
            # Say it rather than swallow it: the roster check still holds at
            # every member that has pulled, but the member whose replica is
            # behind is exactly the case this layer exists for.
            log.warning("group %s: contract %s NOT revoked (bridge unavailable) — "
                        "removal rests on the roster until a retry succeeds", group_id, cid)
    return n


async def revoke_app_group_slips(pool, app_id: str) -> int:
    """Revoke every GROUP-scoped slip this host issued for one app.

    Called by registry.uninstall BEFORE it deletes that app's peer_app_outbox
    rows, because those rows are the only place a group grant's contract_id is
    written down. Delete them first and the contracts are stranded live with
    nothing able to enumerate them — they would then lapse on their own 90-day
    term instead of on the user's act of uninstalling.

    Per-contact slips are NOT revoked here. That is deliberate and pre-existing:
    uninstalling drops the app's rows, and a reinstall re-grants. What changes
    for a group slip is only that its id lives somewhere the uninstall erases.
    """
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT payload->'app_slip'->>'contract_id' AS cid FROM peer_app_outbox "
            # `payload->>'group_id' IS NOT NULL` rather than the jsonb `?`
            # key-exists operator: `?` reads as a placeholder to several
            # drivers and tools, and this is the only predicate that would
            # differ between them. Same result, no ambiguity.
            "WHERE app_id = $1 AND op = $2 AND payload->>'group_id' IS NOT NULL "
            "  AND status <> 'revoked'",
            app_id, "app.grant")
    except Exception as e:
        log.info("app %s: could not list group slips to revoke (%s)", app_id, type(e).__name__)
        return 0
    n = 0
    for r in rows:
        cid = r["cid"]
        if not cid:
            continue
        if await _bridge("contract.revoke", {"contract_id": cid}) is not None:
            n += 1
        else:
            # Named, not swallowed. The uninstall still proceeds — holding it
            # up on a bridge flap would leave the app installed — but this
            # contract now lapses on its term rather than on the user's act.
            log.warning("app %s: group contract %s NOT revoked (bridge unavailable) — "
                        "it now lapses on its term instead", app_id, cid)
    if n:
        log.info("app %s: revoked %d group-scoped slip(s) before uninstall", app_id, n)
    return n


async def group_slip_for(pool, group_id: str) -> tuple[dict | None, int | None]:
    """The live slip a group's owner issued us, renewed at half-life under the
    same contract — the rule app_lane.app_slip_for applies to an app slip
    (server/peer/app_lane.py:314-354). (slip, owner_entity_id)."""
    try:
        row = await pool.fetchrow(
            "SELECT slip, contract_id, owner_entity_id FROM group_slips WHERE group_id = $1",
            group_id)
    except Exception as e:
        log.warning("group slip lookup failed: %s", type(e).__name__)
        return None, None
    if not row:
        return None, None
    owner = int(row["owner_entity_id"])
    s = row["slip"]
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except Exception:
            return None, owner
    if not isinstance(s, dict):
        return None, owner
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
                await store_group_slip(pool, group_id, owner, fslip)
            except Exception:
                pass
            s = fslip
            exp = _parse_dt((fslip.get("terms") or {}).get("expires_at"))
    if exp is not None and exp <= now:
        return None, owner
    return s, owner


# ═══════════════════════════════════════════════════════════════════════════
# OUTBOX — the sender side. Same DOCTRINE as the app lane, and the same CODE:
# classify() and _backoff_s() are imported, not restated. A separate TABLE
# because peer_app_outbox requires an app_id that must name an installed app
# (app_lane._app_accepts_peers, app_lane.py:232-247) and a group op names none.
# ═══════════════════════════════════════════════════════════════════════════

async def enqueue(pool, group_id: str, peer_id: int, op: str, payload: dict,
                  message_id: uuid.UUID | None = None) -> dict:
    mid = message_id or uuid.uuid4()
    row = await pool.fetchrow(
        "INSERT INTO group_outbox (group_id, peer_entity_id, op, payload, message_id, expires_at) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, now() + ($6 || ' days')::interval) "
        "ON CONFLICT (group_id, peer_entity_id, message_id) DO NOTHING "
        "RETURNING id, status, message_id, attempts, expires_at",
        group_id, int(peer_id), op, json.dumps(payload or {}), mid, str(OUTBOX_TTL_DAYS))
    if row is None:
        row = await pool.fetchrow(
            "SELECT id, status, message_id, attempts, expires_at FROM group_outbox "
            "WHERE group_id = $1 AND peer_entity_id = $2 AND message_id = $3",
            group_id, int(peer_id), mid)
    out = dict(row)
    if hasattr(out.get("expires_at"), "isoformat"):
        out["expires_at"] = out["expires_at"].isoformat()   # when it is given up on, for the sender
    return out


async def _send(pool, row: dict) -> tuple[dict | None, BaseException | None]:
    """One attempt on the wire."""
    from server.peer.app_lane import LocalFailure, FinalLocal, message_slip_for
    from server.peer.peer_client import send_to_peer, encode_frame, MAX_FRAME_BYTES
    gid = row["group_id"]
    peer = int(row["peer_entity_id"])
    payload = row["payload"]
    try:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as e:
                raise FinalLocal(f"payload unreadable: {type(e).__name__}") from e
        if not isinstance(payload, dict):
            payload = {}
        if row["op"] == OP_GRANT:
            # The invite rides the MESSAGE slip the member issued us — the same
            # credential app.grant presents, for the same reason: the member
            # has issued us no group slip yet, because this is what delivers it.
            msg_slip = await message_slip_for(pool, peer)
            if msg_slip is None:
                raise LocalFailure("no message slip from this peer on file (not a contact yet, or a lookup failed)")
            gslip = payload.get("group_slip")
            if not isinstance(gslip, dict):
                gslip = await issue_group_slip(peer, gid)
                if gslip is None:
                    raise LocalFailure("bridge unavailable to issue the group slip")
                payload = {**payload, "group_slip": gslip}
                # Keep the issued slip on the row so a retry re-presents the
                # same contract instead of minting a new one per attempt.
                try:
                    await pool.execute(
                        "UPDATE group_outbox SET payload = $2::jsonb WHERE id = $1",
                        row["id"], json.dumps(payload))
                except Exception:
                    pass
            g = await R.get_group(pool, gid)
            params = {"group_id": gid, "group_slip": gslip,
                      "name": (g or {}).get("name") or "",
                      "description": (g or {}).get("description") or "",
                      "roster_epoch": int((g or {}).get("roster_epoch") or 1)}
            if len(encode_frame(OP_GRANT, msg_slip, params)) > MAX_FRAME_BYTES:
                raise FinalLocal("frame exceeds the lane's ceiling")
            return await send_to_peer(peer, OP_GRANT, msg_slip, params), None

        # group.vote — presented under the group slip the OWNER issued us.
        slip, owner = await group_slip_for(pool, gid)
        if slip is None:
            raise LocalFailure("no group slip from that owner on file (not invited yet, or it expired)")
        params = {"group_id": gid, "ballot_id": payload.get("ballot_id"),
                  "vote": payload.get("vote")}
        if len(encode_frame(OP_VOTE, slip, params)) > MAX_FRAME_BYTES:
            raise FinalLocal(f"frame exceeds the lane's ceiling ({MAX_FRAME_BYTES} bytes with slip and envelope)")
        return await send_to_peer(peer, OP_VOTE, slip, params), None
    except Exception as e:
        return None, e


async def attempt(pool, row: dict) -> dict:
    """Try one outbox row now and record the outcome. The DECISION is
    app_lane.classify — imported, so the group lane cannot drift from the app
    lane on what counts as a final refusal versus a retryable moment."""
    from server.peer.app_lane import classify, _backoff_s
    resp, exc = await _send(pool, row)
    status, reason = classify(resp, exc)
    if status == "refused" and row.get("op") == OP_GRANT \
            and _OLD_IMAGE_ID_REFUSAL in str(reason or ""):
        # A member still on an image from before group ids named their owner
        # reads a new id as malformed. That answer changes when the member's
        # Host is upgraded, so the invite waits in the outbox (it expires on
        # its own after OUTBOX_TTL_DAYS) instead of being refused for good.
        status = "queued"
    attempts = int(row.get("attempts") or 0) + 1
    try:
        if status == "delivered":
            await pool.execute(
                "UPDATE group_outbox SET status='delivered', attempts=$2, delivered_at=now(), "
                "last_error=NULL, response=$3::jsonb WHERE id=$1",
                row["id"], attempts, json.dumps((resp or {}).get("data") or {}))
        elif status == "refused":
            await pool.execute(
                "UPDATE group_outbox SET status='refused', attempts=$2, last_error=$3, "
                "response=$4::jsonb WHERE id=$1",
                row["id"], attempts, reason, json.dumps((resp or {}).get("data") or {}))
        else:
            await pool.execute(
                "UPDATE group_outbox SET status='queued', attempts=$2, last_error=$3, "
                "next_attempt_at = now() + ($4 || ' seconds')::interval WHERE id=$1",
                row["id"], attempts, reason, str(_backoff_s(attempts)))
    except Exception as e:
        # Bookkeeping failed AFTER the attempt: push next_attempt_at out so the
        # row is not re-selected on the very next tick with no backoff.
        log.warning("group outbox row %s: could not record %s (%s) — backing off",
                    row["id"], status, type(e).__name__)
        try:
            await pool.execute(
                "UPDATE group_outbox SET attempts=$2, last_error=$3, "
                "next_attempt_at = now() + ($4 || ' seconds')::interval WHERE id=$1",
                row["id"], attempts, f"bookkeeping_failed:{type(e).__name__}", str(_backoff_s(attempts)))
        except Exception:
            pass
    return {"id": row["id"], "status": status, "reason": reason, "attempts": attempts,
            "response": (resp or {}).get("data") if isinstance(resp, dict) else None}


async def tick(pool) -> int:
    expired = await pool.fetch(
        "UPDATE group_outbox SET status='expired', last_error='expired before delivery' "
        "WHERE status='queued' AND expires_at <= now() "
        "RETURNING id, group_id AS what, peer_entity_id, op, message_id")
    # The sender is told what was given up.
    from server.peer.app_lane import tell_sender_expired
    await tell_sender_expired("group", expired)
    rows = await pool.fetch(
        "SELECT id, group_id, peer_entity_id, op, payload, message_id, attempts "
        "FROM group_outbox WHERE status='queued' AND next_attempt_at <= now() "
        "ORDER BY next_attempt_at LIMIT $1", BATCH)
    n = 0
    for r in rows:
        try:
            await attempt(pool, dict(r))
        except Exception as e:
            log.warning("group outbox row %s attempt raised: %s", r["id"], type(e).__name__)
        n += 1
    return n


# ═══════════════════════════════════════════════════════════════════════════
# SYNC — the member side's repair path.
# ═══════════════════════════════════════════════════════════════════════════

async def sync_group(pool, group_id: str) -> dict:
    """Pull one group's roster from its owner, presenting the group slip and
    offering our own verify key so the owner can pin it.

    Pages until the owner reports no more, and applies the whole thing as ONE
    snapshot: a partial apply would stamp everyone the first page did not
    mention as removed (apply_roster_snapshot replaces rather than merges, and
    it has to — a merge would make a REMOVAL the one change replication could
    never carry).
    """
    from server.peer.peer_client import send_to_peer, PeerUnavailable
    from server.network import nac_stepup
    from server import brain_service
    import base64

    slip, owner = await group_slip_for(pool, group_id)
    if slip is None:
        return {"ok": False, "group_id": group_id,
                "error": "no live group slip from that owner (not invited yet, or it expired)"}
    me = brain_service._entity_id
    try:
        pub_b64 = base64.b64encode(await nac_stepup.entity_verify_pubkey(pool, int(me))).decode()
    except Exception as e:
        log.warning("group %s sync: could not read our own verify key: %s", group_id, type(e).__name__)
        pub_b64 = None

    members: list = []
    head: dict = {}
    offset = 0
    while True:
        params = {"group_id": group_id, "offset": offset}
        if pub_b64 and offset == 0:
            params["verify_pubkey"] = pub_b64
        if offset == 0:
            params["with_ballots"] = True
        try:
            resp = await send_to_peer(int(owner), OP_ROSTER_FETCH, slip, params)
        except PeerUnavailable as e:
            return {"ok": False, "group_id": group_id, "retryable": True,
                    "error": f"owner unreachable: {e}"}
        if not isinstance(resp, dict) or not resp.get("ok"):
            return {"ok": False, "group_id": group_id,
                    "error": (resp or {}).get("error") or "owner refused the roster fetch"}
        data = resp.get("data") or {}
        if offset == 0:
            head = data
        page = data.get("members") or []
        members.extend(page)
        total = int(data.get("total") or len(members))
        offset += len(page)
        if not page or offset >= total:
            break

    out = await R.apply_roster_snapshot(
        pool, group_id=group_id, owner_entity_id=int(owner),
        name=head.get("name") or "", description=head.get("description") or "",
        roster_epoch=int(head.get("roster_epoch") or 1), members=members)
    # An owner on an older image sends no `ballots` key; nothing is stored then.
    # A snapshot refused as stale is a stale answer, so its ballots are not
    # applied either.
    if out.get("applied") and isinstance(head.get("ballots"), list):
        out["ballots"] = await R.apply_ballot_snapshot(
            pool, group_id=group_id, owner_entity_id=int(owner), ballots=head["ballots"])
    return out


async def next_vote_seq(pool, group_id: str, voter: int) -> int:
    """The seq this host's next vote in this group must carry.

    held_seq reads group_votes, and only the OWNER writes there (accept_vote).
    On a member the votes it signed live in its own group_outbox, so without
    them every member vote was signed seq 1 and the owner refused every one
    after the first as a replay.
    """
    held = await R.held_seq(pool, group_id, voter)
    sent = await pool.fetchval(
        "SELECT max((payload->'vote'->'terms'->>'seq')::bigint) FROM group_outbox "
        "WHERE group_id = $1 AND op = $2 AND payload->'vote'->'terms'->>'voter' = $3",
        group_id, OP_VOTE, str(int(voter)))
    # The floor is the clock in microseconds. The owner never tells a member
    # the highest seq it holds, so a member whose pod lost its outbox (a
    # restore) would otherwise restart at 1 and have every vote refused as a
    # replay for good; and two votes cast at once read the same max. Ordering
    # is still seq alone — the clock only picks a number no earlier vote from
    # this host can be above (seq is BIGINT; microseconds fit for millennia).
    return max(int(held or 0) + 1, int(sent or 0) + 1, time.time_ns() // 1000)


async def _sync_one_quietly(pool, group_id: str) -> None:
    try:
        await sync_group(pool, group_id)
    except Exception as e:
        log.info("group %s opportunistic sync failed: %s", group_id, type(e).__name__)


async def sync_due(pool, entity_id: int) -> int:
    """One pass over every group this host is a member of but does not own."""
    rows = await pool.fetch(
        "SELECT g.group_id FROM groups g "
        " WHERE g.owner_entity_id <> $1 AND g.revoked_at IS NULL "
        "   AND EXISTS (SELECT 1 FROM group_slips s WHERE s.group_id = g.group_id)",
        int(entity_id))
    n = 0
    for r in rows:
        try:
            await sync_group(pool, r["group_id"])
        except Exception as e:
            log.info("group %s sync failed: %s", r["group_id"], type(e).__name__)
        n += 1
    return n


async def run_group_lane_loop(pool, entity_id: int) -> None:
    """Background driver: the outbox every TICK_S, the roster resync every
    SYNC_INTERVAL_S. Never raises; a tick that fails logs and waits."""
    log.info("group lane loop up (entity %s, outbox every %ss, roster resync every %ss)",
             entity_id, TICK_S, SYNC_INTERVAL_S)
    since_sync = SYNC_INTERVAL_S  # sync on the first pass, not SYNC_INTERVAL_S later
    while True:
        try:
            await tick(pool)
        except Exception as e:
            log.warning("group outbox tick failed: %s", type(e).__name__)
        if since_sync >= SYNC_INTERVAL_S:
            since_sync = 0
            try:
                await sync_due(pool, entity_id)
            except Exception as e:
                log.warning("group roster resync failed: %s", type(e).__name__)
            # A group app grant follows the ROSTER, not a snapshot of it: a
            # member who joined since the grant was made is granted here. Runs
            # on the same beat as the resync because that is when the roster
            # this host owns and the rosters it replicates are both current.
            try:
                from server.peer.app_lane import reconcile_group_grants
                await reconcile_group_grants(pool, entity_id)
            except Exception as e:
                log.warning("group app grant reconcile failed: %s", type(e).__name__)
            # And the STORE's replica of any roster that gates a circle app.
            # Same beat as the grant reconcile above and for the identical reason: this is the
            # moment the rosters this host owns are known to be current, so it
            # is the moment a stale copy anywhere else is worth correcting.
            # Its trigger is the roster EPOCH, not this timer — an unchanged
            # roster costs one query and sends nothing.
            try:
                from server.applications import appstore_circle
                await appstore_circle.reconcile_rosters(pool, entity_id)
            except Exception as e:
                log.warning("store circle roster reconcile failed: %s", type(e).__name__)
        await asyncio.sleep(TICK_S)
        since_sync += TICK_S
