"""Rendezvous — the Host half of batched introductions.

WHAT LEAVES THE POD IS A LIST OF STRINGS AND NOTHING ELSE. Not memory, not a
profile, not an embedding, not a summary of any of them. This module is the
whole of what rendezvous ever sends off the host, which is why it is short and
why `rendezvous_interests` has no free-text column: a field whose far side
nobody can see is a field that eventually gets filled with something that should
not have left.

THREE THINGS HAPPEN HERE:

  1. the user's tags are normalised and stored (`set_interests`, `withdraw`);
  2. the whole current list is SIGNED and posted (`publish`), idempotently;
  3. an introduction that arrived in the inbox is accepted (`accept_intro`),
     which is the only place this host joins anything.

NOTHING HERE MATCHES ANYBODY. The batch runs on the platform, once an hour, in
communityd — the only singleton in the estate that can safely hold it
(community/deploy/20-communityd.yaml:18-21 declares replicas: 1 with
strategy Recreate, and says why).

NORMALISATION IS AT THE WRITE AND TARGETS THE FAR SIDE'S RULE. A tag becomes a
community group slug, so the set of characters it may contain is not a choice
made here: it is exactly what communityd's `validSlug` admits
(communityd's forum/groups.go — a-z, 0-9, '-', '_',
length 1..64). Applying the far side's rule at the near end means a tag can
never be listed successfully and then refused at group-creation time, which is
the failure nobody would be able to see from either end.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re

log = logging.getLogger("privatae.groups.rendezvous")

#: Bumped only if the signed object's SHAPE changes. A verifier refuses a
#: version it does not know rather than verifying a subset of the fields — the
#: rule votes.VOTE_V states for the same reason.
LISTING_V = 1
LISTING_KIND = "rendezvous_listing"

#: communityd's own slug bound (community/forum/groups.go:349). Stated as a
#: constant rather than inlined so the one place it could drift is visible.
MAX_TAG_LEN = 64

#: 16 random bytes hex — the bridge's own slip nonce width
#: (bridge/adapter.py:2359), and votes.NONCE_BYTES for the same reason.
NONCE_BYTES = 16

#: Anything that is not a slug character becomes a separator. A run of them
#: collapses to ONE '-', so "home  lab" and "home-lab" are the same tag rather
#: than two that look identical in a list.
_SEP_RE = re.compile(r"[^a-z0-9_]+")
_VALID_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")


class RendezvousError(Exception):
    """A refusal the route surfaces, same shape as roster.RosterError
    (server/groups/roster.py:52-60)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def normalise(tag) -> str | None:
    """One tag, normalised, or None if nothing usable is left.

    IDEMPOTENT BY CONSTRUCTION — normalise(normalise(x)) == normalise(x) — and
    the gate asserts it. That property is what makes it safe to store normalised
    and re-validate on the platform rather than re-transform there: the two ends
    agree on the bytes, so a tag cannot match differently than it reads.
    """
    if not isinstance(tag, str):
        return None
    t = _SEP_RE.sub("-", tag.strip().lower()).strip("-")
    if not t or len(t) > MAX_TAG_LEN:
        return None
    return t if _VALID_RE.match(t) else None


def normalise_all(tags) -> list[str]:
    """A list of tags, normalised, deduplicated, SORTED.

    Sorted here and not only in the signed terms, because the stored order and
    the signed order must be the same order — one set is one signature, and a
    caller that re-posted the same interests in a different order must not
    produce different bytes.
    """
    if not isinstance(tags, (list, tuple)):
        return []
    out = set()
    for t in tags:
        n = normalise(t)
        if n:
            out.add(n)
    return sorted(out)


# ── The host's own list ────────────────────────────────────────────────────

async def interests(pool, *, include_withdrawn: bool = False) -> list[dict]:
    if include_withdrawn:
        rows = await pool.fetch(
            "SELECT tag, listed_at, last_error, created_at, withdrawn_at "
            "FROM rendezvous_interests ORDER BY tag")
    else:
        rows = await pool.fetch(
            "SELECT tag, listed_at, last_error, created_at, withdrawn_at "
            "FROM rendezvous_interests WHERE withdrawn_at IS NULL ORDER BY tag")
    return [dict(r) for r in rows]


async def live_tags(pool) -> list[str]:
    rows = await pool.fetch(
        "SELECT tag FROM rendezvous_interests WHERE withdrawn_at IS NULL ORDER BY tag")
    return [r["tag"] for r in rows]


async def set_interests(pool, tags) -> dict:
    """Make this host's live tag list equal to `tags`. REPLACE, not merge.

    The same replace-not-merge rule `roster.apply_roster_snapshot` records
    (server/groups/roster.py:317-330) and for the same reason: under a merge,
    REMOVAL is the one change that can never travel, and here removal is the
    change that matters most — a withdrawn interest that keeps matching is the
    feature failing in the direction the user actually cares about.

    A tag removed from the list is stamped `withdrawn_at`, never deleted, so
    re-adding it later is one row changing state rather than a row appearing
    with a fresh `created_at` that misreports when the user first said it.

    Returns the stored set AND anything that normalised to nothing, because a
    call that succeeds while quietly discarding one of the user's interests is
    the shape they cannot see.
    """
    if not isinstance(tags, (list, tuple)):
        raise RendezvousError(400, "tags must be a list")
    wanted = normalise_all(tags)
    if tags and not wanted:
        raise RendezvousError(
            400, "no usable tags: a tag is 1-64 characters of a-z, 0-9, '-' or '_' "
                 "after normalisation, because it becomes a community group slug")
    # WHAT WAS DROPPED IS REPORTED, not silently swallowed. A 70-character tag
    # or one made entirely of punctuation normalises to nothing, and when even
    # one other tag survives the call succeeds — so without this the user is
    # told "listed" and one of their interests simply is not there, discoverable
    # only by diffing the returned list against what they typed.
    dropped = [str(t)[:120] for t in tags if normalise(t) is None]

    # ONE statement, not one per tag. The count is uncapped by design — a
    # person's interests are theirs — so the bound that protects the pod has to
    # be on the WORK, not on the user: unnest() makes a hundred tags one round
    # trip instead of a hundred.
    await pool.execute(
        "INSERT INTO rendezvous_interests (tag) SELECT unnest($1::text[]) "
        "ON CONFLICT (tag) DO UPDATE SET withdrawn_at = NULL",
        wanted)
    await pool.execute(
        "UPDATE rendezvous_interests SET withdrawn_at = now() "
        " WHERE withdrawn_at IS NULL AND NOT (tag = ANY($1::text[]))",
        wanted)
    return {"tags": wanted, "dropped": dropped}


async def withdraw(pool, tag: str | None = None) -> dict:
    """Withdraw one tag, or every tag when `tag` is None.

    Withdrawing everything is its own call and not "post an empty list",
    because the two read completely differently at a call site and only one of
    them looks deliberate — the reason `knock_peer` exists as its own name
    beside `send_to_peer` (server/peer/peer_client.py:74-86).
    """
    if tag is None:
        n = await pool.execute(
            "UPDATE rendezvous_interests SET withdrawn_at = now() WHERE withdrawn_at IS NULL")
        return {"withdrawn": "all", "rows": n}
    t = normalise(tag)
    if not t:
        raise RendezvousError(400, "malformed tag")
    await pool.execute(
        "UPDATE rendezvous_interests SET withdrawn_at = now() "
        " WHERE tag = $1 AND withdrawn_at IS NULL", t)
    return {"withdrawn": t}


# ── Signing and posting ────────────────────────────────────────────────────

def listing_terms(*, entity_id: int, tags: list, seq: int, nonce: str,
                  signed_at: str) -> dict:
    """The object the signature covers. Every field is load-bearing:

      entity     whose list this is. Checked against the CERT-derived id at the
                 bridge, so a signature naming a different entity than the
                 connection is refused rather than silently re-attributed —
                 the property votes.vote_terms states for `voter`.
      tags       SORTED, so one set is one signature.
      seq        monotonic per entity: the platform refuses a listing at or
                 below the seq it holds, so an old signed list cannot be
                 replayed to resurrect a withdrawn tag.
      nonce      16 random bytes, so two identical listings never share bytes.
      signed_at  advisory, bounded by the platform at 24h future skew.

    Key order is irrelevant — _canonical sorts — but the SET is not: adding a
    field without bumping LISTING_V would make old and new signatures disagree.
    """
    return {
        "v": LISTING_V,
        "kind": LISTING_KIND,
        "entity": int(entity_id),
        "tags": list(tags),
        "seq": int(seq),
        "nonce": str(nonce),
        "signed_at": str(signed_at),
    }


async def _next_seq(pool) -> int:
    """Bump and return the listing counter. NEVER reset — a reset would make
    every previously-signed listing replayable."""
    return await pool.fetchval(
        "INSERT INTO rendezvous_state (id, seq) VALUES (1, 1) "
        "ON CONFLICT (id) DO UPDATE SET seq = rendezvous_state.seq + 1, "
        "                               updated_at = now() "
        "RETURNING seq")


async def sign_listing(pool, entity_id: int, tags: list) -> dict:
    """Sign this host's whole current tag list. Returns the wire object.

    Signing goes through nac_stepup.sign_policy_terms (nac_stepup.py:352-356)
    rather than loading the key here, so there is exactly one place in the pod
    that turns entity_sign into a signature — the rule votes.sign_vote follows.
    """
    import os
    from server.network import nac_stepup
    terms = listing_terms(
        entity_id=int(entity_id), tags=list(tags), seq=await _next_seq(pool),
        nonce=os.urandom(NONCE_BYTES).hex(),
        signed_at=_now().astimezone(_dt.timezone.utc).isoformat())
    key_id, sig_b64 = await nac_stepup.sign_policy_terms(pool, int(entity_id), terms)
    return {"terms": terms, "signature": sig_b64, "key_id": key_id}


async def _bridge(op: str, params: dict) -> dict | None:
    """Same shape as lane._bridge (server/groups/lane.py:321-328) — one place
    per module that knows the bridge may simply not be there."""
    try:
        from server.main import get_bridge_client
        c = get_bridge_client()
        return await c.request(op, params or {}) if c is not None else None
    except Exception as e:
        log.info("bridge %s failed: %s", op, type(e).__name__)
        return None


async def publish(pool, entity_id: int) -> dict:
    """Sign and post the WHOLE current list. Idempotent and total.

    Not a delta. The platform makes its stored set equal to what this carries,
    so a tag withdrawn here disappears there on the next post without a second
    message having to arrive and be believed.

    A bridge that is not there is reported, never swallowed: the tags are stored
    either way, and the user needs to know the difference between "listed" and
    "saved, not yet listed".
    """
    import base64
    from server.network import nac_stepup
    tags = await live_tags(pool)
    listing = await sign_listing(pool, int(entity_id), tags)
    try:
        pub_b64 = base64.b64encode(
            await nac_stepup.entity_verify_pubkey(pool, int(entity_id))).decode()
    except Exception as e:
        log.warning("rendezvous: could not read this host's verify key: %s", type(e).__name__)
        pub_b64 = None
    # verify_pubkey rides so the platform can pin it FIRST-KEY-WINS on the first
    # post, the way a member's key is pinned on its first roster pull
    # (server/groups/lane.py:194-204). Without it the first listing would arrive
    # unverifiable and have to be stored uncounted, which for a listing means
    # not matching at all.
    data = await _bridge("rendezvous.list", {**listing, "verify_pubkey": pub_b64})
    if data is None:
        err = "the bridge is not reachable — your tags are saved but not listed yet"
        await pool.execute(
            "UPDATE rendezvous_interests SET last_error = $1 WHERE withdrawn_at IS NULL", err)
        return {"ok": False, "listed": False, "tags": tags, "error": err}
    await pool.execute(
        "UPDATE rendezvous_interests SET listed_at = now(), last_error = NULL "
        " WHERE withdrawn_at IS NULL")
    log.info("rendezvous: listed %d tag(s) at seq %s", len(tags), listing["terms"]["seq"])
    return {"ok": True, "listed": True, "tags": tags,
            "seq": listing["terms"]["seq"], "platform": data}


async def withdraw_remote(pool, entity_id: int, tag: str | None) -> dict:
    """Tell the platform to drop one tag, or all of them.

    Signed like a listing, for the same reason: the platform must be able to
    hold an attributable record of a withdrawal after the bridge has relayed it,
    and a withdrawal that cannot be attributed is one that cannot be audited.

    NORMALISES ITS OWN INPUT, like every other entry point in this module. The
    first cut took the caller's word for it and the route handed it the raw
    query string, so `DELETE /rendezvous?tag=Bonsai` withdrew "bonsai" locally
    and asked the platform to withdraw "Bonsai" — which the platform refuses
    with a 400, correctly, because that end VALIDATES and never transforms.
    Local state and platform state then disagreed with nothing on
    screen to say so, which is the worst shape a withdrawal can fail in.

    The rule is one line and it is the whole fix: every function here that
    accepts a tag from outside normalises it, so no caller can hold it wrong.
    """
    if tag is not None:
        t = normalise(tag)
        if not t:
            raise RendezvousError(400, "malformed tag")
        tag = t
    listing = await sign_listing(pool, int(entity_id), [] if tag is None else [tag])
    data = await _bridge("rendezvous.withdraw",
                         {**listing, "tag": tag, "all": tag is None})
    return {"ok": data is not None, "tag": tag, "all": tag is None, "platform": data}


# ── An introduction that arrived ───────────────────────────────────────────

async def introductions(pool) -> list[dict]:
    """The rendezvous introductions waiting in this host's inbox.

    Read straight off `inbox_messages`, because that IS where the card lives —
    there is no second table shadowing it, for the reason join.pending states.
    """
    import json
    rows = await pool.fetch(
        "SELECT message_id, subject, body, payload, sent_at FROM inbox_messages "
        " WHERE message_kind = 'rendezvous_intro' AND archived_at IS NULL "
        " ORDER BY sent_at DESC")
    out = []
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        p = p if isinstance(p, dict) else {}
        out.append({
            "message_id": str(r["message_id"]),
            # The numeric id is what community.group.join takes
            # (bridge/adapter.py:9020 `_community_group_id`); the slug and title
            # are what a person reads. Both ride, and the id is the one the
            # code acts on.
            "group_id": p.get("group_id"),
            "group_slug": p.get("group_slug"),
            "group_title": p.get("group_title"),
            "tag": p.get("tag"),
            # A COUNT. There is no field here for who the others are and there
            # must never be one: an introduction that named them would make
            # listing a tag a way to enumerate who else listed it.
            "others": p.get("others"),
            "arrived_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        })
    return out


async def accept_intro(pool, message_id: str) -> dict:
    """Join the community group an introduction named. THE USER'S ACT.

    This is the only place rendezvous joins anything. The batch introduces; it
    never enrols. Joining goes through `community.group.join`, which already
    exists and is open to members (bridge/adapter.py:9005) — no new op and no
    new authority, because a community group's membership has always been the
    member's own decision.

    The group id comes off the STORED CARD, never off the caller: a caller that
    could pass an id could join this host to any community group by naming it,
    which is a different act than accepting an introduction.

    THE VERIFY KEY RIDES ALONG. `community.group.join` pins it at join time and
    the key is optional there "only so a host can take a seat before it has
    one" — but a host that joins without one cannot vote, and the tally reports
    it as a roster row in that state rather than counting round it
    (bridge/adapter.py:9005-9019). This host has the key already, so there is no
    reason to arrive in that state and one extra field avoids it.
    """
    import base64
    import json
    # `archived_at IS NULL` is load-bearing, not tidiness: accepting archives
    # the card, so without it an introduction could be accepted again and again,
    # each one a fresh community.group.join for a group this host is already in.
    # It is also the only thing here that makes "an introduction is answered
    # once" true — the UNIQUE (entity, group_id) that guarantees it on the
    # platform side bounds the BATCH, not this call.
    row = await pool.fetchrow(
        "SELECT payload FROM inbox_messages "
        " WHERE message_id = $1::uuid AND message_kind = 'rendezvous_intro' "
        "   AND archived_at IS NULL",
        str(message_id))
    if row is None:
        raise RendezvousError(404, "no unanswered introduction with that id")
    p = row["payload"]
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    p = p if isinstance(p, dict) else {}
    try:
        gid = int(p.get("group_id"))
    except (TypeError, ValueError):
        gid = 0
    if gid <= 0:
        raise RendezvousError(409, "that introduction names no group")
    slug = p.get("group_slug") or str(gid)
    params: dict = {"group_id": gid}
    try:
        from server import brain_service
        from server.network import nac_stepup
        params["verify_pubkey"] = base64.b64encode(
            await nac_stepup.entity_verify_pubkey(
                pool, int(brain_service._entity_id))).decode()
    except Exception as e:
        # Joinable without it — the far side says so explicitly — so this is a
        # log line and not a refusal. What it costs is the ability to vote until
        # the key is pinned, which the tally reports out loud.
        log.warning("rendezvous: joining %s without a verify key: %s",
                    slug, type(e).__name__)
    data = await _bridge("community.group.join", params)
    if data is None:
        raise RendezvousError(503, "the bridge is not reachable — try again")
    try:
        from server.bridge.memory_handlers import handle_inbox_archive
        await handle_inbox_archive({"message_id": str(message_id)})
    except Exception as e:
        # The join LANDED. Failing the call over bookkeeping would invite a
        # retry that joins twice, so the archive failure is reported and the
        # join is not unwound.
        log.warning("rendezvous: joined %s but could not archive the card: %s",
                    slug, type(e).__name__)
    log.info("rendezvous: joined community group %s", slug)
    return {"ok": True, "joined": slug, "platform": data}
