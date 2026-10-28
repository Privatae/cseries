"""Host discovery — a machine introduces the hosts it serves.

One bastion daemon serves many hosts, each pairing isolated from the others by
design. That makes the box the ONLY party that can know the set: every pairing
mints a fresh WireGuard keypair and a fresh per-pairing device fingerprint, so
two pairings of one machine are unlinkable from outside. The platform cannot
derive peers and must not be able to — correlating them platform-side would mean
silently linking a person's work host to their personal one.

So discovery is the machine ANNOUNCING, on the heartbeat it already sends
(`peers` on the body, narrowed in topology.upsert_heartbeat). It is
automatic and symmetric — pairing IS the consent, no approval step. Pair a
bastion to a second host and both hosts learn of each other on the next beat.
That is what lets an IT team's hosts assemble themselves with no steps, and it
is equally how you learn that someone paired a host to a machine you own. It
only works as awareness if it is loud, which is why it reaches the inbox rather
than sitting in a lens.

WHAT A PEER IS HERE: a claim by this bastion that it also serves that host.
It is NOT proof, and it grants NOTHING — no reach, no permission slip, no
credential use. Authorization is untouched: a secured channel still requires
its bridge-signed slip. This module records an introduction; it never widens.

"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("privatae.network.peer_discovery")

# The most distinct peers one machine may ever introduce. The heartbeat is
# already narrowed to 64 entries per beat (topology.upsert_heartbeat), but the
# dedupe is keyed on (token_id, peer_host_name) and rows are deliberately never
# deleted — "a machine I used to share with Acme" is the awareness this feature
# exists to give. Those two together mean a box that rotates 64 FRESH names on
# every beat grows this table without bound, costs one bridge handle lookup per
# new name, and raises one inbox message per new name. Each of those is the
# feature working exactly as designed, which is what makes the ceiling the right
# place to stop it: past this many, the machine is not introducing peers.
_MAX_PEERS_PER_TOKEN = 256

# The introduction is written as PLAIN TEXT, and the reason is the surface it
# lands on: Circles draws `body` as a text node, at the door (circles-web/src/
# cards/deck.tsx DoorCard) and in full (cards/inbox-notes.tsx NoteSheet, a
# whitespace-pre-wrap block), and neither uses a markdown renderer or
# dangerouslySetInnerHTML. So markdown syntax would show as literal asterisks,
# and markdown-escaping a bastion-supplied name would show as literal
# backslashes — `desktop\-01` instead of desktop-01, in the one message this
# feature exists to deliver.
#
# What DOES need guarding on a plain-text surface is not markup, it is
# STRUCTURE: the name is the bastion's word, up to 253 characters, so a newline
# inside it lets a machine forge extra lines in a message about itself. That is
# what _plain strips. If this body ever moves to a markdown renderer, escaping
# becomes necessary again — body_format says text/plain precisely so that change
# is visible when it happens.
_CONTROL = {c: None for c in range(0x20)} | {0x7f: None}


def _plain(s: str, limit: int = 120) -> str:
    """A bastion-supplied string, safe to drop into a plain-text line: no
    newlines or control characters to forge structure with, and length-capped."""
    out = str(s or "").translate(_CONTROL).strip()
    return out[:limit] + ("…" if len(out) > limit else "")

# network_bastion_peer is declared in migrations/v132_bastion_peers.sql:69,
# peer_entity_id included. This module used to mirror that CREATE plus an
# ADD COLUMN defensively on the heartbeat path; that is gone, so the table
# arrives at boot like every other. Reads and writes below are unchanged.


async def record_and_notify(
    pool,
    token_id: int,
    bastion_label: str,
    peers: Optional[list[dict[str, Any]]],
) -> int:
    """Record this beat's announced peer set and tell the user about any peer
    they have not been told about. Returns the number of introductions raised.

    `peers` is None when the bastion said nothing about peers (an older client):
    that is NOT an empty set, and it must leave the recorded peering alone. An
    explicit [] means the box now serves only this host — the rows stay, they
    simply stop being refreshed and go dormant.

    Best-effort throughout: discovery is awareness, and a failure here must
    never break the liveness beat that carries it.
    """
    if peers is None:
        return 0

    now_names = [p["host_name"] for p in peers if p.get("host_name")]

    # Ceiling on DISTINCT peers per machine. Checked before the inserts, because
    # the whole point is to not let the table grow. A box at the ceiling can
    # still refresh peers it has already introduced (those rows exist, so the
    # ON CONFLICT path just moves last_seen_at) — it simply cannot mint new
    # names forever. Logged at warning: a machine that hits this is either
    # broken or hostile, and either way the user's operator should see it.
    try:
        known = await pool.fetchval(
            "SELECT count(*) FROM network_bastion_peer WHERE token_id = $1", token_id)
    except Exception:  # pragma: no cover - defensive
        known = 0
    if (known or 0) >= _MAX_PEERS_PER_TOKEN:
        existing = set()
        try:
            rows = await pool.fetch(
                "SELECT peer_host_name FROM network_bastion_peer WHERE token_id = $1 "
                "  AND peer_host_name = ANY($2::text[])",
                token_id, now_names)
            existing = {r["peer_host_name"] for r in rows}
        except Exception:  # pragma: no cover - defensive
            existing = set()
        dropped = [n for n in now_names if n not in existing]
        if dropped:
            logger.warning(
                "peer ceiling hit for token %s (%d known, max %d) — ignoring %d new "
                "name(s); refreshing known peers only", token_id, known,
                _MAX_PEERS_PER_TOKEN, len(dropped))
        peers = [p for p in peers if p.get("host_name") in existing]
        now_names = [n for n in now_names if n in existing]
        if not now_names:
            return 0

    try:
        for p in peers:
            host_name = p.get("host_name")
            if not host_name:
                continue
            await pool.execute(
                """
                INSERT INTO network_bastion_peer
                    (token_id, peer_host_name, peer_handle, peer_since)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (token_id, peer_host_name) DO UPDATE SET
                    peer_handle  = EXCLUDED.peer_handle,
                    peer_since   = EXCLUDED.peer_since,
                    last_seen_at = now()
                """,
                token_id, host_name, p.get("handle") or None, p.get("since") or None,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("peer record failed for token %s: %r", token_id, e)
        return 0

    if not now_names:
        return 0

    # Resolve each peer's ENTITY ID once and keep it. Circles keys a host thread
    # by entity id (_list_entity_threads), so a peering without one can be listed
    # but never opened. Deliberately non-fatal and deliberately retried on later
    # beats: the peer may not have finished provisioning, or the bridge may be
    # down, and a peering we cannot resolve today is still one the user should
    # see. _resolve_target_service(None, handle, not_found_status=404) does the
    # bridge handle lookup and RAISES MessageError when it cannot resolve
    # (messages.py:178-189) — it never returns a falsy id.
    try:
        unresolved = await pool.fetch(
            "SELECT peer_host_name, peer_handle FROM network_bastion_peer "
            "WHERE token_id = $1 AND peer_entity_id IS NULL "
            "  AND peer_host_name = ANY($2::text[])",
            token_id, now_names,
        )
    except Exception:  # pragma: no cover - defensive
        unresolved = []
    for u in unresolved:
        handle = u["peer_handle"] or (u["peer_host_name"] or "").split(".")[0]
        if not handle:
            continue
        try:
            from server.routes.messages import _resolve_target_service
            eid = await _resolve_target_service(None, handle, not_found_status=404)
        except Exception as e:
            logger.info("peer %s unresolved for now (%s) — retried next beat",
                        u["peer_host_name"], e)
            continue
        try:
            await pool.execute(
                "UPDATE network_bastion_peer SET peer_entity_id = $3 "
                "WHERE token_id = $1 AND peer_host_name = $2",
                token_id, u["peer_host_name"], int(eid),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.info("peer id store failed for %s: %r", u["peer_host_name"], e)

    # Anything still unannounced is the introduction we owe the user. Read it
    # back from the table rather than diffing in memory: the table is what
    # dedupes across beats, restarts, and two pods racing the same heartbeat.
    try:
        fresh = await pool.fetch(
            """
            SELECT peer_host_name, peer_handle
              FROM network_bastion_peer
             WHERE token_id = $1 AND notified_at IS NULL
               AND peer_host_name = ANY($2::text[])
             ORDER BY peer_host_name
            """,
            token_id, now_names,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("peer notify scan failed for token %s: %r", token_id, e)
        return 0
    if not fresh:
        return 0

    try:
        from server.bridge.memory_handlers import handle_inbox_append
    except Exception as e:  # pragma: no cover - defensive
        logger.info("peer notify: inbox unavailable: %r", e)
        return 0

    machine = bastion_label or "one of your machines"
    raised = 0
    for r in fresh:
        peer = r["peer_host_name"]
        handle = r["peer_handle"] or peer.split(".")[0]
        # Every interpolated value below is the BASTION's word — the machine
        # chooses its own hostname and the peer names it announces, up to 253
        # chars each. Unescaped, a machine could render its own link or heading
        # inside the one message whose job is to tell the user how to revoke it.
        e_handle, e_peer, e_machine = _plain(handle), _plain(peer), _plain(machine)
        body = (
            f"{e_handle} is on a machine you own.\n\n"
            f"Your bastion on {e_machine} reports that it also serves the Host "
            f"{e_peer}. You are peers: the same machine works for both of you.\n\n"
            "This is the machine's word, not proof. It grants nothing — no reach "
            "into your Host, no permission slip, no access to your credentials. "
            "Anything that crosses between you still needs your explicit grant.\n\n"
            "If you did not expect this, someone paired a Host to that machine. "
            f"Open Network → Bastions and revoke {e_machine} to end it.\n"
        )
        # handle_inbox_append RETURNS {"ok": False, "error": ...} on failure — it
        # does not raise (memory_handlers.py:334-350). Marking notified_at on a
        # bare await would silently swallow the one message this whole feature
        # exists to deliver, and the dedupe would ensure it never retried.
        try:
            res = await handle_inbox_append({
                "subject": f"New peer: {e_handle}",
                "body": body,
                # text/plain, and it matters: the Inbox renders the body as a
                # text node, so markdown here would show as literal syntax.
                "body_format": "text/plain",
                "message_kind": "system_notification",
                "payload": {
                    "app": "network", "lens": "topology", "kind": "new_peer",
                    "token_id": token_id, "machine": machine,
                    "peer_host_name": peer, "peer_handle": handle,
                },
            })
        except Exception as e:  # pragma: no cover - defensive
            logger.info("peer notify: push raised for %s: %r", peer, e)
            continue
        if not (isinstance(res, dict) and res.get("ok")):
            # Left unnotified on purpose: the next beat retries it.
            logger.info("peer notify: inbox refused %s: %s", peer,
                        (res or {}).get("error") if isinstance(res, dict) else res)
            continue
        try:
            await pool.execute(
                "UPDATE network_bastion_peer SET notified_at = now() "
                "WHERE token_id = $1 AND peer_host_name = $2",
                token_id, peer,
            )
        except Exception as e:  # pragma: no cover - defensive
            # Told but not recorded: the next beat will tell them again. Noisy
            # beats being preferable to a silent one is the deliberate choice.
            logger.info("peer notify: mark failed for %s: %r", peer, e)
        raised += 1
    if raised:
        logger.info("peer discovery: %d introduction(s) via token %s (%s)",
                    raised, token_id, machine)
    return raised


async def list_peerings(pool, entity_id: int) -> list[dict]:
    """Every peering this host has been told about, with the machine that
    introduced it, whether the claim is still being made, the pairing state the
    machine reports for it, and whether a connection request may go to it
    (`requestable`). Dormant rows are kept and labelled, never dropped.

    `requestable` reads the latest beat, and also what the box confirmed to
    this Host after that beat: an anchor that has just approved a pairing does
    not wait up to a minute for the box to say so again, and one that has just
    removed a pairing does not keep sending requests through it."""
    try:
        rows = await pool.fetch(
            """
            SELECT p.token_id, p.peer_host_name, p.peer_handle, p.peer_since,
                   p.peer_entity_id,
                   p.first_seen_at, p.last_seen_at, p.notified_at,
                   t.name AS bastion_name, h.hostname AS bastion_hostname,
                   -- CURRENT means the box is STILL making the claim, and the
                   -- authority for that is the latest heartbeat's own peer
                   -- list, not the clock. The time window alone made an
                   -- unpairing take a full day to show: the box stops naming
                   -- the peer, but last_seen_at on the row simply stops moving.
                   -- Now an explicit empty announcement (the client sends [] —
                   -- peers.go, and agents.go tests `peers != nil`) retracts on
                   -- the very next beat, which is when the user did it.
                   -- h.peers IS NULL keeps the old behaviour for a bastion that
                   -- never reports peers at all, so absence still cannot retract.
                   (p.last_seen_at > now() - INTERVAL '1 day'
                    AND (h.peers IS NULL
                         OR EXISTS (SELECT 1
                                      FROM jsonb_array_elements(h.peers) e
                                     WHERE e->>'host_name' = p.peer_host_name)))
                   AS current,
                   -- The pairing state the box reports for this Host on that
                   -- same latest beat (topology.upsert_heartbeat keeps only
                   -- active, pending and revoked). NULL when the beat does not
                   -- name the Host, names it with no state, or the box never
                   -- reported peers. A box naming the Host more than once gets
                   -- the least usable answer it gave: revoked over pending over
                   -- active, and active only when every entry says active.
                   -- COALESCE matters: bool_and skips NULLs, so an entry with
                   -- no state would otherwise not count against active.
                   (SELECT CASE
                             WHEN bool_or(e->>'state' = 'revoked') THEN 'revoked'
                             WHEN bool_or(e->>'state' = 'pending') THEN 'pending'
                             WHEN bool_and(COALESCE(e->>'state', '') = 'active') THEN 'active'
                           END
                      FROM jsonb_array_elements(h.peers) e
                     WHERE e->>'host_name' = p.peer_host_name)
                   AS reported_state,
                   -- What the box told THIS Host since that beat, when this
                   -- Host is the machine's anchor. The beat is up to a minute
                   -- old (the box beats on a 60-second ticker, and neither an
                   -- approval nor a revoke triggers one), so on its own it
                   -- refuses a pairing the anchor has just approved and still
                   -- serves one the anchor has just removed.
                   --
                   -- confirmed_at: the box accepted this Host's pairing grant
                   -- for that Host on that machine AFTER the beat. synced_at is
                   -- written only when pairing_grant_sync exits 0
                   -- (pairing_grants._record_sync), and the box answers 0 only
                   -- after it has written the pairing active (the gateway's
                   -- pairing_ops.go handlePairingGrantSync). A grant the box
                   -- has not confirmed, one revoked or superseded, one expired,
                   -- or one for another machine or Host is not counted.
                   (SELECT max(g.synced_at)
                      FROM nac_pairing_grants g
                     WHERE g.entity_id = t.entity_id
                       AND g.server = h.hostname
                       AND g.subject = p.peer_host_name
                       AND g.revoked_at IS NULL
                       AND g.synced_at IS NOT NULL
                       AND (g.expires_at IS NULL OR g.expires_at > now())
                       AND g.synced_at > h.last_seen_at)
                   AS confirmed_at,
                   -- withdrawn_at: this Host revoked its grant for that Host on
                   -- that machine AFTER the beat (a revoke signs revoke terms;
                   -- a re-approval superseding a grant does not).
                   (SELECT max(g.revoked_at)
                      FROM nac_pairing_grants g
                     WHERE g.entity_id = t.entity_id
                       AND g.server = h.hostname
                       AND g.subject = p.peer_host_name
                       AND g.revoke_terms_canonical IS NOT NULL
                       AND g.revoked_at > h.last_seen_at)
                   AS withdrawn_at,
                   -- approval: this Host's live approval of that Host on that
                   -- machine that the box has not confirmed: 'refused' when the
                   -- box refused it (the heartbeat retry never resends those
                   -- bytes, pairing_grants.record_and_notify), else 'unconfirmed'
                   -- (offline at approval; delivered on a later beat). NULL when
                   -- there is none. It lifts nothing; it only lets a refusal say
                   -- what is true.
                   (SELECT CASE WHEN g.sync_detail LIKE 'refused:%' THEN 'refused'
                                ELSE 'unconfirmed' END
                      FROM nac_pairing_grants g
                     WHERE g.entity_id = t.entity_id
                       AND g.server = h.hostname
                       AND g.subject = p.peer_host_name
                       AND g.revoked_at IS NULL
                       AND g.synced_at IS NULL
                       AND (g.expires_at IS NULL OR g.expires_at > now())
                     ORDER BY g.id DESC LIMIT 1)
                   AS approval
              FROM network_bastion_peer p
              JOIN mcp_client_tokens t ON t.id = p.token_id
              LEFT JOIN network_bastion_heartbeat h ON h.token_id = p.token_id
             WHERE t.entity_id = $1
               AND t.operator_kind = 'bastion'
               AND t.revoked_at IS NULL
             ORDER BY p.last_seen_at DESC, p.peer_host_name
            """,
            entity_id,
        )
    except Exception as e:
        logger.info("list_peerings unavailable: %s", e)
        return []
    return [_peering(r) for r in rows]


def _iso(v) -> Optional[str]:
    return v.isoformat() if v else None


def _peering(r) -> dict:
    current = bool(r["current"])
    state = r["reported_state"]
    # Read with .get: a row without them (a stub of this query) reads as "the
    # box confirmed nothing since its beat", which lifts nothing.
    confirmed, withdrawn = r.get("confirmed_at"), r.get("withdrawn_at")
    # The newest word wins. A revoke this Host made after the beat outranks the
    # beat's "active" and any confirmation older than the revoke. A
    # confirmation lifts only "pending": the box refuses a grant for a revoked
    # pairing (handlePairingGrantSync), so a confirmation never stands against
    # "revoked", and a missing or unknown state is never read as active.
    if withdrawn is not None and (confirmed is None or withdrawn >= confirmed):
        active = False
    elif state == "active":
        active = True
    else:
        active = state == "pending" and confirmed is not None
    return {
        "token_id": r["token_id"],
        "peer_host_name": r["peer_host_name"],
        "peer_handle": r["peer_handle"] or r["peer_host_name"].split(".")[0],
        "peer_entity_id": r["peer_entity_id"],
        "peer_since": r["peer_since"],
        "first_seen_at": _iso(r["first_seen_at"]),
        "last_seen_at": _iso(r["last_seen_at"]),
        "introduced_by": r["bastion_hostname"] or r["bastion_name"],
        "current": current,
        # "active", "pending", "revoked", or None when the machine does not
        # report one for this Host now. The latest beat's word, unchanged.
        "state": state,
        # When the box confirmed this Host's approval of that pairing after its
        # latest beat, else None. Present only on the anchor that approved it.
        "confirmed_at": _iso(confirmed),
        # When this Host removed that pairing after the box's latest beat.
        "withdrawn_at": _iso(withdrawn),
        # This Host's approval the box has not confirmed: "unconfirmed",
        # "refused", or None. For the refusal sentence only.
        "approval": r.get("approval"),
        # Whether a connection request may go to this Host through this
        # peering: the machine still names it AND the pairing is active, by
        # the latest beat or by the box's own confirmation of an approval made
        # since. A pending Host has not been admitted to the machine and a
        # revoked one was removed from it, so neither is a Host the machine
        # serves. A missing or unknown state is treated the same way, because
        # the request is the one thing a peering authorizes.
        "requestable": current and active,
    }


def requestable_peering(peerings: list[dict], peer_entity_id) -> Optional[dict]:
    """The peering through which a connection request may go to that Host
    (list_peerings' `requestable`), or None. The request route and the Circles
    archive guard both look a Host up this way, so they cannot disagree."""
    return next((p for p in peerings or []
                 if p.get("requestable") and p.get("peer_entity_id") == peer_entity_id), None)


def request_refusal(peerings: list[dict], peer_entity_id) -> tuple[str, str]:
    """Why a connection request cannot go to that Host now: (reason, sentence).

    For a Host with no requestable peering (requestable_peering returned None).
    reason is one of "pending", "approval_refused", "withdrawn", "revoked",
    "no_state", "dormant" or "not_shared"; the sentence is what the person
    reads. With several peerings, the one closest to working speaks: a pairing
    about to be confirmed says so rather than hiding behind one that is gone.

    Every sentence says only what this Host knows. A pending pairing is not
    "about to work": nobody may have approved it, and an approval the box
    refused is never resent, so no sentence promises a time."""
    mine = [p for p in peerings or [] if p.get("peer_entity_id") == peer_entity_id]
    live = [p for p in mine if p.get("current")]

    def machine(p: dict) -> str:
        return _plain(p.get("introduced_by") or "") or "the machine you share with that host"

    pending = [p for p in live if p.get("state") == "pending" and not p.get("withdrawn_at")]
    for p in pending:
        if p.get("approval") == "unconfirmed":
            return "pending", (
                f"you approved that host's pairing on {machine(p)}, but the machine hasn't "
                "confirmed the approval yet, so a connection request can't go to it through "
                "that machine. It can as soon as the machine confirms it.")
    for p in pending:
        if p.get("approval") != "refused":
            return "pending", (
                f"{machine(p)} reports that host's pairing as waiting for approval, so a "
                "connection request can't go to it through that machine yet. Once the Host "
                "that anchors the machine approves it, the machine reports it active on its "
                "next check-in.")
    for p in pending:
        return "approval_refused", (
            f"{machine(p)} refused your approval of that host's pairing, so a connection "
            "request can't go to it through that machine. Approving it again sends the "
            "machine a new grant.")
    for p in live:
        if p.get("withdrawn_at"):
            return "withdrawn", (
                f"you revoked that host's pairing on {machine(p)}, so a connection "
                "request can't go to it through that machine.")
    for p in live:
        if p.get("state") == "revoked":
            return "revoked", (
                f"{machine(p)} reports that host's pairing as removed, so a connection "
                "request can't go to it through that machine.")
    if live:
        return "no_state", (
            f"{machine(live[0])} names that host but doesn't report its pairing as active, "
            "so a connection request can't go to it through that machine.")
    if mine:
        return "dormant", (
            f"that host is no longer on a machine you share — {machine(mine[0])} stopped "
            "reporting it.")
    return "not_shared", (
        "that host is not on a machine you share — a connection request may only be sent "
        "to a host one of your machines currently reports serving")
