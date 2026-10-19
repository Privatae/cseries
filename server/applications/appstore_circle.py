"""Store audience `circle` — the Host half: telling the platform who is on the
roster that gates a circle app.

The shape is `audience='circle'`, `app_store_circle_members`, a catalog filter
and a bundle-route membership gate. The missing piece, and where it comes
from: *"the membership source that the catalog filter and the
bundle-route gate read is `group_members` on the creator's host, keyed by the
group id the store app is published to. Nothing else has to be invented for it."*

THE ASYMMETRY THAT DEFINES THIS MODULE. The store is platform-side. The roster
of a group circle is HOST-side and the platform holds nothing for it — that is
v154's first design decision and it is not being walked back here. So the
platform cannot look the roster up, and the creator's host has to say.

WHY A PUSH AND NOT A PULL. A pull would mean the platform holding a credential
against every publisher's host and knowing when to spend it: a standing reach
into a pod, for a fact the pod can just send. The push costs nothing new — it
rides the beat the group lane already wakes on (server/groups/lane.py:743-757),
which is precisely the moment this host's own rosters are known to be current.

WHAT THE SIGNATURE IS FOR, stated because this is where someone will reach for
it: TAMPER-EVIDENCE AT REST, not authority. The snapshot travels host -> bridge
-> store, and the signature is what keeps the row attributable to the host that
made it after the bridge has relayed it. The AUTHORITY is the bridge's
cert-derived identity: `entity_id` is derived from the client certificate by
bridge_server and handed to every op, and no op takes an acting entity from
params (the bridge adapter states this as the
property the whole scheme rests on). The signature does not authenticate the
caller — the certificate did that, one hop earlier.

WHAT TRAVELS IS A LIST OF ENTITY IDS. Not display names, not verify keys, not
roles, not `added_at`. The store's only question is "may this entity fetch this
bundle", so a member id is the complete answer and everything else would be
roster detail leaving a pod for no purpose. `roster.snapshot_member` exists for
the peer lane, where a member genuinely needs the key and the role; this path
deliberately does not reuse it.
"""
from __future__ import annotations

import datetime as _dt
import logging

from server.groups import roster as R

log = logging.getLogger("privatae.applications.appstore_circle")

#: Bumped only if the signed object's SHAPE changes. A verifier refuses a
#: version it does not know rather than verifying a subset of the fields.
ROSTER_V = 1
ROSTER_KIND = "appstore_circle_roster"

#: The audience value this module exists for. One string, declared beside the
#: code that acts on it.
AUDIENCE = "circle"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def roster_terms(*, app_id: str, group_id: str, publisher_entity_id: int,
                 epoch: int, members: list, signed_at: str) -> dict:
    """The object the signature covers. Every field is load-bearing:

      app + group  bind the snapshot to one app AND one group. Without both, a
                   signature lifted from app X's snapshot would apply to app Y,
                   which is how one publisher's roster becomes another's.
      publisher    checked at the platform against the CERT-derived caller, so a
                   valid signature naming a different publisher is refused
                   rather than silently re-attributed.
      epoch        the group's roster_epoch. Monotonic, so an older snapshot
                   arriving late can never walk the replica backwards and
                   re-admit someone the newer one removed.
      members      SORTED AND DEDUPLICATED entity ids, so one roster is one
                   signature.
      signed_at    advisory, bounded at the far end by clock skew.

    DEDUPLICATION IS PART OF THE CANONICAL FORM, NOT OF THE CALLER. Both callers
    here happen to pass distinct ids — `group_members` is keyed
    (group_id, member_entity_id) — so a duplicate cannot arise today, and that
    is exactly why it has to be handled here rather than relied on there: the
    platform's verifier rebuilds these bytes from the members it was sent, and
    if one end deduplicates while the other does not, a snapshot carrying a
    repeated id produces bytes the verifier can never reproduce and a perfectly
    valid roster is refused as tampered. The platform side normalises
    identically and both repos freeze the same byte vector in their gates.

    Key order is irrelevant — the canonical form sorts — but the SET is not:
    adding a field without bumping ROSTER_V would make old and new signatures
    disagree.
    """
    return {
        "v": ROSTER_V,
        "kind": ROSTER_KIND,
        "app": str(app_id),
        "group": str(group_id),
        "publisher": int(publisher_entity_id),
        "epoch": int(epoch),
        "members": sorted({int(m) for m in members}),
        "signed_at": str(signed_at),
    }


async def check_publishable(pool, group_id: str) -> dict:
    """May this host gate an app on `group_id`? Raises if not. Writes nothing.

    SPLIT FROM record_publication ON PURPOSE. The two happen at different
    moments and must: the CHECK runs before the bundle is uploaded, so a
    publisher learns their group is wrong without paying for the round trip;
    the RECORD runs after the store has accepted the submission, because a
    publish is a REVIEW REQUEST and not a listing. A row written at check time
    for a submission the operator later rejects is a row reconcile_rosters
    retries on every beat for ever, against an app the store answers 404 for.
    """
    if not R.valid_group_id(group_id):
        raise R.RosterError(400, "a circle app must name a group this host owns")
    g = await R.get_group(pool, group_id)
    from server import brain_service
    if g is None or int(g["owner_entity_id"]) != int(brain_service._entity_id):
        # A publisher may only gate an app on a roster it OWNS. Gating on a
        # replica would be gating on a list this host cannot change and does not
        # decide, and the platform would then be told about it by the wrong host.
        raise R.RosterError(403, "this host does not own that group — a circle app is "
                                 "gated on a roster its publisher holds")
    if g.get("revoked_at") is not None:
        raise R.RosterError(409, "that group is closed — a closed roster would gate the "
                                 "app on a list that can never change again")
    return {"group_id": group_id, "name": g["name"]}


async def record_publication(pool, app_id: str, group_id: str) -> dict:
    """Record that this host published `app_id` gated on `group_id`.

    Called AFTER the store accepted the submission — see check_publishable for
    why the two halves are separated. Re-checks ownership rather than trusting
    that the caller ran the check: this is the function that creates a standing
    obligation, so it is the one that has to be sure.
    """
    if not app_id:
        raise R.RosterError(400, "a circle publication needs an app id")
    await check_publishable(pool, group_id)
    await pool.execute(
        "INSERT INTO app_store_circle_publications (app_id, group_id) VALUES ($1, $2) "
        "ON CONFLICT (app_id) DO UPDATE "
        # A re-publish to a DIFFERENT group resets the posted epoch: the two
        # rosters have unrelated epoch sequences, so keeping the old number
        # would make the new group's epoch 1 look older than what we hold and
        # the first snapshot would never be sent.
        "   SET group_id = EXCLUDED.group_id, updated_at = now(), "
        "       posted_epoch = CASE WHEN app_store_circle_publications.group_id "
        "                            = EXCLUDED.group_id "
        "                           THEN app_store_circle_publications.posted_epoch "
        "                           ELSE NULL END",
        app_id, group_id)
    return {"app_id": app_id, "group_id": group_id}


async def publications(pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT p.app_id, p.group_id, p.posted_epoch, p.posted_at, p.last_error, "
        "       g.roster_epoch "
        "  FROM app_store_circle_publications p "
        "  LEFT JOIN groups g ON g.group_id = p.group_id "
        " ORDER BY p.app_id")
    return [dict(r) for r in rows]


async def _bridge(op: str, params: dict) -> dict | None:
    """Same shape as lane._bridge (server/groups/lane.py:321-328)."""
    try:
        from server.main import get_bridge_client
        c = get_bridge_client()
        return await c.request(op, params or {}) if c is not None else None
    except Exception as e:
        log.info("bridge %s failed: %s", op, type(e).__name__)
        return None


async def post_roster(pool, entity_id: int, app_id: str, group_id: str) -> dict:
    """Sign one app's roster snapshot and post it to the store.

    CURRENT MEMBERS ONLY. `roster.roster()` without `include_removed` is the
    right call and the difference matters: a tally needs removed rows so it can
    attribute an old vote, but the store's question is present-tense and a
    removed member appearing in this list is exactly the access this whole
    feature exists to cut off.
    """
    import base64
    from server.network import nac_stepup

    g = await R.get_group(pool, group_id)
    if g is None or int(g["owner_entity_id"]) != int(entity_id):
        return {"ok": False, "app_id": app_id,
                "error": "this host does not own that group"}
    rows = await R.roster(pool, group_id)
    members = [int(r["member_entity_id"]) for r in rows]
    epoch = int(g["roster_epoch"])
    terms = roster_terms(app_id=app_id, group_id=group_id,
                         publisher_entity_id=int(entity_id), epoch=epoch,
                         members=members, signed_at=_now().isoformat())
    key_id, sig_b64 = await nac_stepup.sign_policy_terms(pool, int(entity_id), terms)
    try:
        pub_b64 = base64.b64encode(
            await nac_stepup.entity_verify_pubkey(pool, int(entity_id))).decode()
    except Exception as e:
        log.warning("circle roster: could not read this host's verify key: %s",
                    type(e).__name__)
        pub_b64 = None

    data = await _bridge("appstore.circle_roster", {
        "app_id": app_id, "group_id": group_id, "roster_epoch": epoch,
        "members": terms["members"], "terms": terms,
        "signature": sig_b64, "key_id": key_id, "verify_pubkey": pub_b64,
    })
    if data is None:
        err = "bridge unavailable"
        await pool.execute(
            "UPDATE app_store_circle_publications SET last_error = $2, updated_at = now() "
            " WHERE app_id = $1", app_id, err)
        # NOT a raise. A store replica that is one epoch behind is a real state
        # and the reconciler's whole job is to catch up from it; failing loudly
        # here would turn a transient bridge flap into a failed roster edit.
        return {"ok": False, "app_id": app_id, "epoch": epoch, "error": err}

    # posted_epoch is the epoch the PLATFORM acknowledged. Reading it back out
    # of the answer rather than assuming ours landed is what makes a rejected
    # snapshot visible as "still behind" instead of recorded as applied and
    # never retried.
    #
    # THE PRECISE SHAPE, because it depends on another repo and the dependency
    # should be stated rather than discovered: the controller answers 200 with a
    # roster_epoch for exactly one refusal — a stale epoch — and RAISES for every
    # other (a refused key, a group that does not match the one recorded at
    # publish, an app that is not a circle app). Those arrive here as `data is
    # None` and are handled above, leaving posted_epoch untouched, which is
    # right: a refused key must keep retrying.
    #
    # If the controller ever starts answering 200-with-an-epoch for a refusal
    # that is NOT staleness, this would record that epoch and stop retrying a
    # snapshot the store never applied. Nothing on this side can pin that; the
    # `applied` flag is checked as well as the epoch precisely so the failure
    # needs BOTH to go wrong rather than one.
    applied = data.get("roster_epoch")
    try:
        applied = int(applied)
    except (TypeError, ValueError):
        applied = epoch if data.get("applied") else None
    await pool.execute(
        "UPDATE app_store_circle_publications "
        "   SET posted_epoch = COALESCE($2, posted_epoch), posted_at = now(), "
        "       last_error = $3, updated_at = now() "
        " WHERE app_id = $1",
        app_id, applied, None if data.get("applied") else str(data.get("error") or "")[:500])
    log.info("circle roster: app %s group %s epoch %s -> platform epoch %s (%d member(s))",
             app_id, group_id, epoch, applied, len(members))
    return {"ok": True, "app_id": app_id, "group_id": group_id, "epoch": epoch,
            "platform_epoch": applied, "members": len(members),
            "applied": bool(data.get("applied"))}


async def reconcile_rosters(pool, entity_id: int) -> int:
    """Post a fresh snapshot for every circle app whose roster has moved.

    THE EPOCH IS THE TRIGGER, not a timer and not a hook on the roster edit. A
    hook would miss every change made while the bridge was down; a timer would
    re-post an unchanged roster for ever. Comparing the group's current
    `roster_epoch` against the epoch the platform acknowledged catches both: a
    change that has not landed yet is still pending on the next pass, and one
    that has landed costs nothing.

    This is what makes "a removed member loses catalog visibility and bundle
    fetch on the next roster sync" true rather than aspirational — removal bumps
    the epoch (`roster._bump_epoch`, server/groups/roster.py:187-190), the epoch
    no longer matches, and the next pass sends the shorter list.

    Never raises. A single bad app must not stall the others, the rule the group
    lane's own reconciler follows.
    """
    try:
        rows = await pool.fetch(
            "SELECT p.app_id, p.group_id, p.posted_epoch, g.roster_epoch "
            "  FROM app_store_circle_publications p "
            "  JOIN groups g ON g.group_id = p.group_id "
            " WHERE g.owner_entity_id = $1 AND g.revoked_at IS NULL "
            "   AND (p.posted_epoch IS NULL OR p.posted_epoch < g.roster_epoch)",
            int(entity_id))
    except Exception as e:
        log.info("circle roster reconcile: could not list publications (%s)",
                 type(e).__name__)
        return 0
    n = 0
    for r in rows:
        try:
            await post_roster(pool, int(entity_id), r["app_id"], r["group_id"])
            n += 1
        except Exception as e:
            log.warning("circle roster: app %s post raised: %s",
                        r["app_id"], type(e).__name__)
    return n
