"""Sharing a machine's models with another host — the ask, and the answer.

Someone serves a model on their own GPU and wants a friend or
a teammate to use it. Until now the only way there was a pairing token redeemed
ON the machine — a terminal, or the box's local setup page — so "share my GPU"
meant "let someone touch my box, or go and touch it myself". And the smallest
thing the anchor could then grant was the whole `inference` family: every loaded
model, the OpenAI-compatible server beside it, and the PII sterilizer.

The hard stop: SYSTEM RESOURCES ONLY, NOT DATA. No default rights
to a folder, an index or a context store. The friend's host must be able to say,
truthfully, "the only thing we can do with this machine is use the qwen4 model."

WHAT TRAVELS, AND WHAT DOES NOT — the part worth reading before changing this.

A pairing token is a single-use machine credential: whoever holds it can enrol a
machine as a bastion of the host that minted it. So the question "which lane may
it ride" is not a detail, and the answer came out of the code rather than out of
a preference:

  * THE BRIDGE RELAY SEES PLAINTEXT. The cross-host relay reads and REWRITES the
    message body on the way through — it stamps the verified sender into the
    nested payload and attests the slip into it (bridge adapter.py
    _enrich_and_verify_cross_host), then serialises the whole thing onto the
    recipient pod's socket. Every one of those steps needs cleartext. There is
    no envelope the platform cannot open.
  * AND IT DROPS. That same delivery resolves the recipient through an
    in-process, per-pod writer table, so a host that landed on a different
    bridge pod is simply "not connected". A credential must not ride a
    lane that silently drops.
  * THE DIRECT PEER LANE HAS NEITHER PROBLEM. server/peer dials the peer pod's
    own :8444 mTLS listener by service DNS — no bridge in the data path, and
    nothing per-pod about the addressing.

So this module uses the direct lane and NEVER falls back to the relay for
anything carrying a token.

AND THE TOKEN IS NOT IN THE OFFER. A pairing token lives 15 minutes
(bastion_enroll.PAIRING_TTL_SECONDS), so one minted when the offer is SENT is
dead long before a human gets to it — the feature would work only if the
approver happened to be at the screen. Instead the offer is a question with no
credential in it at all, and the token is fetched at the moment of approval:

  1. the asking host writes an OUTBOUND offer and knocks (peer op inbox.request,
     kind 'machine_request'). It carries who, what for, and which model they
     hope to use. It carries no secret and authorizes nothing.
  2. the owning host stores it and shows a card.
  3. the owner approves: picks the machine, the ceiling, and the exact models.
  4. only then does the owner's host call back over the same lane (peer op
     machine.token) and the asking host mints — because it still holds the open
     offer it sent, which is the authorization for minting.
  5. the owner's host hands the token straight to its own bastion
     (pairing_grants.redeem_on_box), then approves the pending pairing with the
     ceiling and the model scope (pairing_grants.approve_pairing).

The token exists for seconds, is never stored, never sits in an inbox row, and
never waits on a person. A decline writes nothing anywhere near the machine.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("privatae.network.machine_offers")

#: The knock kind. Registered in peer/handlers.REQUEST_KINDS.
OFFER_KIND = "machine_request"

#: The peer op that mints, called by the owner at approval time.
OP_TOKEN = "machine.token"

#: States an offer row moves through. 'open' is the only actionable one, and
#: on an OUTBOUND row it is also a standing permission for that peer to make
#: this host mint one enrolment token — so 'spent' exists to end that the
#: moment it is used (mint_for_peer claims the row and mints in one statement).
OFFER_STATES = ("open", "spent", "accepted", "declined", "withdrawn")

#: An open outbound offer is a standing permission to mint, so it ages out even
#: if nobody ever answers. A week is well past "they are thinking about it" and
#: well short of leaving a minting right lying around for a quarter.
OFFER_WINDOW_DAYS = 7

#: Free text the asker attaches. Capped HERE, at the trust boundary: the sender
#: is another tenant and nothing downstream bounds it.
MAX_PURPOSE = 400
MAX_MODELS_ASKED = 8

# network_machine_offers is declared in migrations/v140_machine_offers.sql:54,
# and ux_machine_offers_open — one OPEN offer per peer per direction, which is
# the whole of the inbound flood control and of the outbound "one standing
# minting right" rule — at v140_machine_offers.sql:79. Both are applied at boot
# by entrypoint.sh. The defensive mirror this module carried is gone;
# v140's own header always named it as a mirror.


class OfferError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ── narrowing at the boundary (pure) ────────────────────────────────────────

def clean_purpose(v: Any) -> str:
    """One line of the asker's own words, control characters removed. It is
    shown on a card next to an Approve button, so a newline in it would forge
    structure in the one sentence whose job is to say what they want."""
    s = "".join(ch for ch in str(v or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F).strip()
    return s[:MAX_PURPOSE]


def clean_models_asked(v: Any) -> list[str]:
    """The models the asker HOPES to use. Their words, never trusted: the scope
    that ends up in the signed grant is whatever the owner picks from their own
    machine's heartbeat. This is a hint on a card, and it is bounded and
    sanitised like one."""
    if isinstance(v, str):
        v = v.replace(";", ",").split(",")
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for m in v:
        m = "".join(ch for ch in str(m or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F).strip()
        if m:
            out.append(m[:128])
        if len(out) >= MAX_MODELS_ASKED:
            break
    return out


def _rfc3339(t) -> Optional[str]:
    return t.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if t else None


def _row(r) -> dict:
    def _j(v, d):
        if v is None:
            return d
        return json.loads(v) if isinstance(v, str) else v
    return {
        "id": r["id"], "direction": r["direction"], "peer_entity_id": r["peer_entity_id"],
        "peer_handle": r["peer_handle"], "peer_display": r["peer_display"],
        "purpose": r["purpose"], "models_asked": _j(r["models_asked"], []),
        "state": r["state"], "server": r["server"], "subject": r["subject"],
        "granted_caps": _j(r["granted_caps"], None), "granted_models": _j(r["granted_models"], None),
        "created_at": _rfc3339(r["created_at"]), "decided_at": _rfc3339(r["decided_at"]),
        "decided_by": r["decided_by"], "detail": r["detail"],
    }


# ── inbound: someone asks to use one of OUR machines ────────────────────────

async def record_inbound(pool, peer_entity_id: int, *, purpose: str, models_asked: list[str],
                         peer_handle: str = "", peer_display: str = "") -> dict:
    """Store an incoming ask. Called from the peer lane's knock handler, which
    has already proved WHO is calling from the client cert — nothing here may be
    taken from the caller's own claim about itself except the two free-text
    fields, which are sanitised above.

    A second ask from a peer who already has one open is refused rather than
    stacked: it is the same question again, and answering the first one frees
    the slot immediately."""
    try:
        rid = await pool.fetchval(
            "INSERT INTO network_machine_offers "
            "  (direction, peer_entity_id, peer_handle, peer_display, purpose, models_asked) "
            "VALUES ('in', $1, $2, $3, $4, $5::jsonb) RETURNING id",
            int(peer_entity_id), peer_handle or None, peer_display or None,
            clean_purpose(purpose), json.dumps(clean_models_asked(models_asked)))
    except Exception as e:
        if "ux_machine_offers_open" in str(e):
            raise OfferError(409, "a machine request from you is already waiting to be answered")
        raise
    return {"offer_id": rid}


async def list_offers(pool, *, direction: Optional[str] = None,
                      state: Optional[str] = "open", limit: int = 50) -> list[dict]:
    """The offers on this host. Default: everything still open, both ways."""
    rows = await pool.fetch(
        "SELECT * FROM network_machine_offers "
        " WHERE ($1::text IS NULL OR direction = $1) "
        "   AND ($2::text IS NULL OR state = $2) "
        " ORDER BY id DESC LIMIT $3", direction, state, max(1, min(int(limit or 50), 200)))
    return [_row(r) for r in rows]


async def _open_offer(pool, offer_id: int, direction: str) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM network_machine_offers WHERE id=$1 AND direction=$2", int(offer_id), direction)
    if row is None:
        raise OfferError(404, f"no {'incoming' if direction == 'in' else 'outgoing'} machine offer {offer_id}")
    if row["state"] != "open":
        raise OfferError(409, f"that offer is already {row['state']}")
    return dict(row)


async def _archive_cards(pool, peer_entity_id: int, offer_id: int) -> None:
    """Archive the machine_request card(s) this peer's ask put in the inbox.

    The answer IS the archive, on every path that answers. The card and the
    offer row are two records of one question (peer handlers
    _record_machine_offer writes the row as the card lands), and the peer lane
    admits ONE unanswered knock of a kind per peer per week by counting
    UNARCHIVED cards (handlers._knock_slot_free) — not by reading this table.
    So an accept or decline that left the card live kept the asker's slot taken
    for up to a week: their next ask came back "a machine_request from you is
    already waiting to be answered" for a question this host had already
    answered. Until now only the Circles card archived itself, with a second
    call after the answer; the route and the Host's own manage_pairing tool
    never did. Measured 2026-09-23 on the operational test lane.

    Bounded by the answer's own clock: only cards that landed before the offer
    was decided (`decided_at`, written by the UPDATE just before this and read
    from the same database clock that stamps a card's `sent_at`). A fresh ask
    from the same peer that lands between the two statements — possible only
    when the old card had already aged out of the slot — keeps its card.

    Best-effort: the answer has already been recorded and, on accept, the
    grant is already live, so a failure here is logged and never turns a
    completed answer into an error."""
    try:
        await pool.execute(
            "UPDATE inbox_messages SET archived_at = now() "
            " WHERE message_kind = $2 AND from_entity_id = $1 AND archived_at IS NULL "
            "   AND sent_at <= (SELECT decided_at FROM network_machine_offers WHERE id = $3)",
            int(peer_entity_id), OFFER_KIND, int(offer_id))
    except Exception as e:
        logger.warning("machine offer from %s answered but its card was not archived: %s",
                       peer_entity_id, type(e).__name__)


async def decline_inbound(pool, offer_id: int, *, decided_by: Optional[str] = None,
                          via: str = "unknown") -> dict:
    """Refuse an ask. NOTHING is written on the machine — no token is fetched,
    no redeem happens, the box never hears of it. That is the whole difference
    between this and approving, and it is why the redeem lives on the accept
    path rather than on arrival. The card is archived with the answer
    (_archive_cards).

    Who declined is recorded by the same rule as an accept (nac_commit.approver_for):
    the card's route passes via="ui" and records the user; the manage_pairing tool
    records "host"."""
    from server.network.nac_commit import approver_for
    decided_by = approver_for(via, decided_by)
    row = await _open_offer(pool, offer_id, "in")
    await pool.execute(
        "UPDATE network_machine_offers SET state='declined', decided_at=now(), decided_by=$2, "
        "updated_at=now() WHERE id=$1", row["id"], decided_by)
    await _archive_cards(pool, int(row["peer_entity_id"]), int(row["id"]))
    return {"declined": True, "offer_id": row["id"], "peer_entity_id": row["peer_entity_id"],
            "note": "nothing was written on the machine — the box was never asked"}


async def accept_inbound(pool, eid: int, offer_id: int, *, server: str, caps: Any, models: Any,
                         stepup_code: str, ttl_days: Optional[int] = None,
                         decided_by: Optional[str] = None, via: str = "unknown") -> dict:
    """Approve an ask, end to end, in the order that keeps the credential's life
    as short as it can be: fetch a token from the asking host, hand it straight
    to our own machine, then sign the ceiling.

    Who decided is recorded by the NAC rule (nac_commit.approver_for): the card's
    route passes via="ui" and records the user; the manage_pairing tool is the
    host and records "host", on the offer row and on the grant alike.

    The step-up is checked by approve_pairing, which is the call that actually
    widens anything — but it runs LAST, and a token has already been spent by
    then. So the code is checked HERE too, before anything goes over the wire:
    a wrong 2FA code must not cost the other side a single-use credential.
    """
    from server.network import pairing_grants
    from server.network.nac_commit import approver_for
    decided_by = approver_for(via, decided_by)
    row = await _open_offer(pool, offer_id, "in")
    server = (server or "").strip()
    if not server:
        raise OfferError(400, "which machine? name one of your anchored machines "
                              "(get_network → bastions[] where you are the anchor)")
    try:
        caps = pairing_grants.normalize_caps(caps or ["inference"])
        models = pairing_grants.normalize_models(models)
    except ValueError as e:
        raise OfferError(400, str(e))
    if "inference" not in caps:
        raise OfferError(400, "sharing a model means caps ['inference'] — that is the lane the "
                              "models live on. Use manage_pairing directly to grant anything else.")
    if not models:
        raise OfferError(400, "name the models to share. An approval with no model list is an "
                              "approval to use EVERY model on that machine, which is not what "
                              "this card asked for — pass them explicitly, or approve the pairing "
                              "with manage_pairing if that is really what you mean.")
    # EVERY FREE CHECK FIRST, and the 2FA code last of the checks — it is
    # BURN-ON-USE (nac_stepup.consume_counter), so a code spent proving
    # something this host could have read for itself is a code the user has to
    # wait 30 seconds to replace. Are we the machine's anchor, and will that
    # machine actually honour a model list? Both are reads of what it already
    # told us.
    bastion = await pairing_grants._anchored_here(pool, eid, server)
    pairing_grants._refuse_scope_the_box_cannot_enforce(bastion, models)
    # Now the code. It is checked HERE and nowhere else in this flow: spending
    # it before anyone is asked to mint is what stops a typo from costing the
    # other host a single-use credential, and sign_and_push_grant deliberately
    # does not check it again (it cannot — the counter is already burned).
    if not await pairing_grants._verify_stepup(pool, str(stepup_code or "")):
        raise OfferError(403, "sharing a machine widens access — it needs a valid step-up code "
                              "(enroll a second factor at /nac/stepup/enroll first)")

    token = await request_token_from_peer(int(row["peer_entity_id"]))
    try:
        redeemed = await pairing_grants.redeem_on_box(pool, eid, server, token)
    finally:
        # Dropped as soon as it has been used. It bounds one frame, not the
        # whole call stack — redeem_on_box's own local and the dispatch args
        # still exist on a propagating traceback — so this is a narrowing, not
        # a guarantee that the token cannot appear in a log.
        del token
    subject = redeemed["subject"]
    # The subject and its handle come from the REDEEM ANSWER, not from a
    # heartbeat: the box beats on a 60-second ticker and a redeem triggers no
    # beat, so the pairing this just created is in no report yet.
    granted = await pairing_grants.sign_and_push_grant(
        pool, eid, server, subject, redeemed.get("handle") or "", caps, models,
        row=bastion, ttl_days=ttl_days, approved_by=decided_by)
    # `AND state='open'` so a decline that landed while the network calls were
    # in flight is not overwritten. The grant is live either way — both presses
    # were the user's — but the row must record what actually happened.
    await pool.execute(
        "UPDATE network_machine_offers SET state='accepted', decided_at=now(), decided_by=$2, "
        "server=$3, subject=$4, granted_caps=$5::jsonb, granted_models=$6::jsonb, updated_at=now() "
        "WHERE id=$1 AND state='open'", row["id"], decided_by, server, subject,
        json.dumps(caps), json.dumps(models))
    await _archive_cards(pool, int(row["peer_entity_id"]), int(row["id"]))
    await _tell_them(int(row["peer_entity_id"]), server, models, granted.get("delivered"))
    return {"accepted": True, "offer_id": row["id"], "server": server, "subject": subject,
            "caps": caps, "models": models,
            "may_use": pairing_grants.describe_models(models),
            "delivered": granted.get("delivered"), "expires": granted.get("expires"),
            "note": granted.get("note")}


async def _tell_them(peer_entity_id: int, server: str, models: list[str], delivered) -> None:
    """Say what was shared, in words, on the ordinary message path. Best-effort
    and deliberately NOT on the credential lane: this carries no secret, so the
    relay fallback inside send_message_service is fine here and nowhere else in
    this module."""
    try:
        from server import brain_service
        from server.routes.messages import send_message_service
        pool = brain_service.get_pool()
        if pool is None:
            return
        body = (f"Your host can now use {', '.join(models)} on my machine {server}. "
                f"It is on the inference lane only — no folders, no context, no other models."
                + ("" if delivered else " The machine is offline right now; it takes effect on its next check-in."))
        await send_message_service(pool, brain_service._entity_id,
                                   to_entity_id=int(peer_entity_id), body=body)
    except Exception as e:  # pragma: no cover - best effort
        logger.info("machine offer: could not tell peer %s (%s)", peer_entity_id, type(e).__name__)


# ── outbound: we ask to use SOMEONE ELSE'S machine ──────────────────────────

async def send_offer(pool, eid: int, peer_entity_id: int, *, purpose: str,
                     models_asked: Any = None) -> dict:
    """Ask a peer host to let this host use a model on one of their machines.

    Writes the OUTBOUND row FIRST and only then knocks, because that row is what
    later authorizes us to mint a token for them (mint_for_peer): if the knock
    landed and the row did not, their approval would arrive at a host that
    refuses to complete it."""
    from server.peer import peer_client
    purpose = clean_purpose(purpose)
    asked = clean_models_asked(models_asked)
    peer_entity_id = int(peer_entity_id)
    if peer_entity_id == int(eid):
        raise OfferError(400, "that is this host")
    try:
        rid = await pool.fetchval(
            "INSERT INTO network_machine_offers (direction, peer_entity_id, purpose, models_asked) "
            "VALUES ('out', $1, $2, $3::jsonb) RETURNING id",
            peer_entity_id, purpose, json.dumps(asked))
    except Exception as e:
        if "ux_machine_offers_open" in str(e):
            raise OfferError(409, "you already have an open request with that host — withdraw it "
                                  "first, or wait for them to answer")
        raise
    try:
        res = await peer_client.knock_peer(peer_entity_id, {
            "message_kind": OFFER_KIND,
            "subject": "Use a model on your machine",
            "body": purpose or "We'd like to use a model on one of your machines.",
            "payload": {"models_asked": asked},
        })
    except Exception as e:
        await pool.execute("UPDATE network_machine_offers SET state='withdrawn', detail=$2, "
                           "updated_at=now() WHERE id=$1", rid, f"could not reach them: {e}"[:300])
        raise OfferError(503, f"could not reach that host directly: {e}")
    if not res.get("ok"):
        await pool.execute("UPDATE network_machine_offers SET state='withdrawn', detail=$2, "
                           "updated_at=now() WHERE id=$1", rid, str(res.get("error"))[:300])
        raise OfferError(409, f"they refused the request: {res.get('error')}")
    return {"sent": True, "offer_id": rid, "peer_entity_id": peer_entity_id,
            "models_asked": asked,
            "note": "they decide. If they approve, their machine enrols to this host by itself — "
                    "nobody types anything on it."}


async def withdraw_offer(pool, offer_id: int) -> dict:
    """Take back an ask. This is also how a standing minting right is closed:
    while the row is open, that peer may ask this host to mint a pairing token."""
    row = await _open_offer(pool, offer_id, "out")
    await pool.execute("UPDATE network_machine_offers SET state='withdrawn', decided_at=now(), "
                       "updated_at=now() WHERE id=$1", row["id"])
    return {"withdrawn": True, "offer_id": row["id"]}


async def mint_for_peer(pool, eid: int, peer_entity_id: int) -> dict:
    """Mint a pairing token FOR a peer who is about to approve our ask.

    THE OPEN OUTBOUND OFFER IS THE AUTHORIZATION, and it is a precondition
    rather than a credential — the same shape peer/handlers.inbox_grant uses,
    for the same reason: the thing being asked for cannot itself be presented.
    A peer with no open offer from us is refused outright, so a stranger holding
    a valid platform cert gets nothing. What makes the precondition meaningful
    is that only a user of THIS host can create that row.

    Bounded three ways: the row ages out (OFFER_WINDOW_DAYS), a burst is refused
    (TOKEN_MIN_INTERVAL_S), and a slow drip is refused too (MAX_TOKENS_PER_OFFER).
    Without those, "they may ask us to mint" would mean "they may mint at will".

    The token itself is returned and NOT stored, NOT logged and NOT audited — it
    enrols a machine to this host, and a single-use credential in a log is a
    credential at rest for as long as the log lives."""
    from server.network import bastion_enroll
    # ONE ASK, ONE TOKEN — claimed in a single statement, not read-then-written.
    #
    # The first cut counted tokens in Python between a SELECT and an UPDATE, and
    # PeerServer serves every connection as its own task with no lock, so N
    # simultaneous calls all read zero, all passed, and all minted. A cap you
    # can step around by arriving twice at once is not a cap.
    #
    # So the OFFER ITSELF is the permit and claiming it is the mint: this
    # UPDATE moves the row out of 'open' and hands back its id, and exactly one
    # caller can win that. It also collapses three bounds into one fact —
    # there is no burst to rate-limit and no allowance to exhaust, because
    # after this the row is spent and the answer is the same 403 a stranger
    # gets. A redeem that then fails needs a fresh ask, which is honest: the
    # user on this side has to decide again.
    row = await pool.fetchrow(
        "UPDATE network_machine_offers SET state='spent', tokens_issued=tokens_issued+1, "
        "       last_token_at=now(), updated_at=now() "
        " WHERE id = (SELECT id FROM network_machine_offers "
        "              WHERE direction='out' AND peer_entity_id=$1 AND state='open' "
        "                AND created_at > now() - ($2 || ' days')::interval "
        "              ORDER BY id DESC LIMIT 1 FOR UPDATE SKIP LOCKED) "
        " RETURNING id", int(peer_entity_id), str(OFFER_WINDOW_DAYS))
    if row is None:
        raise OfferError(403, "this host has no open machine request with you")
    # The token carries WHERE to redeem: this host's own handle, resolved the
    # same way the setup card resolves it (bastion_enroll.resolve_self_handle).
    # mint_pairing_token raises a clean 409 on a host that has none yet.
    handle = await bastion_enroll.resolve_self_handle(int(eid))
    try:
        minted = await bastion_enroll.mint_pairing_token(
            pool, entity_id=int(eid), handle=handle,
            name=f"shared machine (offer {row['id']})")
    except Exception:
        # The permit was spent on a mint that did not happen. Put it back, or
        # the user's ask is dead with nothing to show for it.
        await pool.execute("UPDATE network_machine_offers SET state='open', "
                           "tokens_issued=tokens_issued-1, updated_at=now() "
                           "WHERE id=$1 AND state='spent'", row["id"])
        raise
    logger.info("machine offer %s: its one enrolment token issued to peer %s", row["id"], peer_entity_id)
    return {"token": minted["token"], "expires_in_s": minted["expires_in_s"]}


async def request_token_from_peer(peer_entity_id: int, *, timeout: float = 20.0) -> str:
    """Ask the asking host to mint us one enrolment token, over the DIRECT peer
    lane. There is deliberately NO bridge fallback: the answer carries a
    single-use machine credential, and the relay path reads and rewrites message
    bodies in cleartext (see this module's header). If the direct lane is down,
    the honest outcome is that the approval does not happen yet."""
    from server.peer import peer_client
    try:
        res = await peer_client.send_to_peer(peer_entity_id, OP_TOKEN, None, {}, timeout=timeout)
    except peer_client.PeerUnavailable as e:
        raise OfferError(503, f"could not reach that host directly, and an enrolment token is "
                              f"never sent through the platform relay: {e}")
    if not res.get("ok"):
        raise OfferError(403, f"they did not mint a token: {res.get('error')}")
    token = str(((res.get("data") or {}).get("token") or "")).strip()
    if not token:
        raise OfferError(502, "they answered without a token")
    return token
