"""The signing scheme: what bytes are signed, with which key, and how anyone
holding the roster recounts a tally.

WHICH KEY. The host's existing Ed25519 identity key, `entity_sign` in
kernel_secrets (server/applications/entity_signing.py:35, :53-92). No new key:
this is already the key the host signs its identity-bearing statements with —
NAC policy terms (nac_stepup.sign_policy_terms, nac_stepup.py:352-356),
pairing grants to a bastion (pairing_grants._sign, pairing_grants.py:724-729,
which says outright it is "the SAME key and canonical form the DCN store grants
use"), and the Hub PDP's arming key (hub_peers.arm_hub_pdp, hub_peers.py:112).
A group vote joins that list rather than starting a second identity.

WHICH BYTES. nac_stepup._canonical (nac_stepup.py:343-344) —
json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).
Imported, never re-implemented: Python and Go canonical JSON differ on unicode
and pairing_grants.py:35-42 already carries the scar from that. communityd
verifies the same bytes on the platform tier, so a second implementation here
would be a third chance to drift.

WHAT A SIGNATURE PROVES — read this before relying on it. It proves A HOST SAID
THIS. It does not prove a person did. nac_stepup's own header states the limit
for this same key: the in-pod Postgres is initdb'd with --auth=trust, so
anything running in the entity container can read entity_sign and sign what it
likes as itself (nac_stepup.py:15-21). There is no cryptography here that stops
a host lying about its own vote, and there was never going to be — the host is
the user's machine.

What it does buy, precisely:
  * RECOUNTABILITY. A member recomputes the tally from the roster and the
    stored rows. A tallier cannot invent a vote for a member it cannot sign
    for, and cannot silently drop one the member can present.
  * NON-REPUDIATION between members. "I never voted for that" is falsifiable.
  * ATTRIBUTION ACROSS HOSTS. A vote keeps its author through every store,
    forward and recount, with no trusted relay in the middle.

One host, one vote, and one member's host cannot forge another's. Sybil
resistance — one PERSON, one vote — is not a signature property and is never
claimed: on the community tier it rests on the platform's account gate, on this
tier on the creator choosing who is on the roster.
"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger("privatae.groups.votes")

#: Bumped only if the signed object's SHAPE changes. A verifier refuses a
#: version it does not know rather than verifying a subset of the fields.
VOTE_V = 1
VOTE_KIND = "group_vote"

#: THE reason for "this vote is well-formed, but no key is pinned for its voter
#: yet". A named constant because three places branch on it: the verifier emits
#: it, accept_vote treats it as the ONE failure that still stores, and the vote
#: route turns it into advice the member can act on. A magic string compared in
#: three files is one reword away from silently becoming a refusal.
NO_PINNED_KEY = "no pinned key for this voter"

#: What a member should DO about it. The roster pins a key on the member's own
#: roster pull, so the fix is a sync and then re-casting — not a retry of the
#: same bytes, which would be refused as a replayed seq.
NO_PINNED_KEY_ADVICE = (
    "stored but not counted: this group's owner has no verify key pinned for "
    "this host yet. Sync the roster (POST /api/groups/{id}/sync) and cast "
    "again — the re-cast carries a higher seq and supersedes this row.")

#: 16 random bytes hex — the bridge's own slip nonce width.
NONCE_BYTES = 16

#: A vote signed further than this into the future is clock nonsense, not a
#: vote. Ordering is `seq`, never the clock, because the clock is the signer's;
#: this bound only keeps an absurd timestamp out of the table.
MAX_FUTURE_SKEW_S = 24 * 3600

#: The choice vocabulary of a ballot. Bounds an untrusted field that reaches
#: SQL and becomes a tally key.
MIN_CHOICES = 2
MAX_CHOICES = 32
MAX_CHOICE_LEN = 64


def _canonical(obj: dict) -> bytes:
    """THE signed bytes. One import, one implementation — see the module
    header on why this is not written out again here."""
    from server.network.nac_stepup import _canonical as _c
    return _c(obj)


def new_nonce() -> str:
    return os.urandom(NONCE_BYTES).hex()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


def valid_choice(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= MAX_CHOICE_LEN and v.strip() == v


def valid_choices(v) -> bool:
    if not isinstance(v, list) or not (MIN_CHOICES <= len(v) <= MAX_CHOICES):
        return False
    return all(valid_choice(c) for c in v) and len(set(v)) == len(v)


def vote_terms(*, group_id: str, ballot_id: str, voter_entity_id: int,
               choice: str, seq: int, nonce: str, signed_at: str) -> dict:
    """The object the signature covers. EVERY field here is load-bearing:

      group + ballot  bind the vote to one question. Without both, a signature
                      lifted from group X's ballot counts in group Y's.
      voter           the member's entity id. The verifier checks it against
                      the roster row whose pinned key verified the signature,
                      so a valid signature for the wrong voter is refused
                      rather than silently re-attributed.
      choice          one of the ballot's declared choices.
      seq             monotonic per (group, voter): a higher seq supersedes, an
                      equal-or-lower one is a replay and is refused.
      nonce           16 random bytes, so two identical votes never share bytes.
      signed_at       advisory, bounded by MAX_FUTURE_SKEW_S.

    Key order is irrelevant — _canonical sorts — but the set is not: adding a
    field without bumping VOTE_V would make old and new signatures disagree.
    """
    return {
        "v": VOTE_V,
        "kind": VOTE_KIND,
        "group": str(group_id),
        "ballot": str(ballot_id),
        "voter": int(voter_entity_id),
        "choice": str(choice),
        "seq": int(seq),
        "nonce": str(nonce),
        "signed_at": str(signed_at),
    }


async def sign_vote(pool, entity_id: int, *, group_id: str, ballot_id: str,
                    choice: str, seq: int) -> dict:
    """Sign one vote as THIS host. Returns the wire object: the terms plus the
    base64 signature and the key id that made it.

    Signing goes through nac_stepup.sign_policy_terms (nac_stepup.py:352-356)
    rather than loading the key here, so there is exactly one place in the pod
    that turns entity_sign into a signature.
    """
    from server.network import nac_stepup
    terms = vote_terms(
        group_id=group_id, ballot_id=ballot_id, voter_entity_id=int(entity_id),
        choice=choice, seq=int(seq), nonce=new_nonce(), signed_at=_iso(_now()))
    key_id, sig_b64 = await nac_stepup.sign_policy_terms(pool, int(entity_id), terms)
    return {"terms": terms, "signature": sig_b64, "key_id": key_id}


def verify_vote(vote: dict, *, verify_pubkey: bytes | None,
                group_id: str, ballot_id: str, choices: list | None = None,
                known_seq: int | None = None) -> tuple[bool, str, dict]:
    """Verify one signed vote OFFLINE against a pinned key. (ok, reason, terms).

    Checks, in order: shape -> version -> the terms name THIS group and ballot
    -> choice is in the ballot's vocabulary -> a key is pinned for the voter ->
    signature valid under that pinned key -> seq is ahead of what we hold ->
    signed_at is not absurd.

    ORDER MATTERS. The signature is checked before seq and before the clock,
    because an unsigned or wrongly-signed vote must never reach a code path
    that updates state — and after the group/ballot binding, so a valid
    signature for a different question is refused as a binding failure with a
    reason that says so, rather than as a signature failure that reads like
    corruption.

    verify_pubkey None means the voter's key is not pinned yet. That is NOT a
    forgery and is reported as its own reason: the vote is stored unverified
    and left out of the count, and the tally says how many it left out.
    """
    if not isinstance(vote, dict):
        return False, "malformed vote", {}
    terms = vote.get("terms")
    if not isinstance(terms, dict):
        return False, "malformed vote", {}
    if terms.get("v") != VOTE_V or terms.get("kind") != VOTE_KIND:
        return False, f"unknown vote version/kind ({terms.get('v')!r}/{terms.get('kind')!r})", {}

    if str(terms.get("group")) != str(group_id):
        return False, "vote names a different group", terms
    if str(terms.get("ballot")) != str(ballot_id):
        return False, "vote names a different ballot", terms
    try:
        voter = int(terms["voter"])
        seq = int(terms["seq"])
    except (KeyError, TypeError, ValueError):
        return False, "voter/seq missing or not an integer", terms
    choice = terms.get("choice")
    if not valid_choice(choice):
        return False, "choice missing or malformed", terms
    if choices is not None and choice not in choices:
        return False, "choice is not one this ballot offers", terms
    if not isinstance(terms.get("nonce"), str) or not terms["nonce"]:
        return False, "nonce missing", terms

    if not verify_pubkey:
        # Not a forgery. The roster has no key for this member yet — they have
        # not pulled since joining. Stored, not counted, and counted as
        # uncounted by the tally.
        return False, NO_PINNED_KEY, terms

    try:
        sig = base64.b64decode(vote.get("signature") or "")
    except Exception:
        return False, "signature is not base64", terms
    try:
        Ed25519PublicKey.from_public_bytes(bytes(verify_pubkey)).verify(sig, _canonical(terms))
    except Exception:
        return False, "signature invalid (terms altered, or not signed by this member)", terms

    if known_seq is not None and seq <= int(known_seq):
        return False, f"seq {seq} is not ahead of the vote already held ({known_seq})", terms

    signed_at = terms.get("signed_at")
    try:
        when = _dt.datetime.fromisoformat(str(signed_at).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return False, "signed_at is not a timestamp", terms
    if when > _now() + _dt.timedelta(seconds=MAX_FUTURE_SKEW_S):
        return False, "signed_at is further in the future than any clock skew explains", terms

    return True, "valid", terms


def tally(roster: list, votes: list, *, choices: list) -> dict:
    """Recount a ballot from the roster and the stored rows.

    This is the whole point of signing. The number is not asserted by whoever
    stored the votes; it is recomputed from rows anyone holding the roster can
    re-verify. Callers that want to prove that independently re-run
    verify_vote over the stored signatures rather than trusting `verified`.

    roster: rows with member_entity_id, removed_at, verify_pubkey.
    votes:  rows with voter_entity_id, choice, verified.

    The result reports what it could NOT count as loudly as what it could —
    a tally that quietly omits members with no pinned key reads as complete
    when it is not.
    """
    current = {int(r["member_entity_id"]) for r in roster if r.get("removed_at") is None}
    unpinned = {int(r["member_entity_id"]) for r in roster
                if r.get("removed_at") is None and not r.get("verify_pubkey")}
    counts = {c: 0 for c in choices}
    counted = 0
    unverified = 0
    off_roster = 0
    for v in votes:
        voter = int(v["voter_entity_id"])
        if voter not in current:
            # A vote from someone who has since left. Kept and attributable,
            # never counted in a live tally.
            off_roster += 1
            continue
        if not v.get("verified"):
            unverified += 1
            continue
        c = v.get("choice")
        if c not in counts:
            # Verified against the member's key, but naming a choice this
            # ballot does not offer: the ballot changed under it. Not counted,
            # and not silently dropped either.
            unverified += 1
            continue
        counts[c] += 1
        counted += 1
    return {
        "counts": counts,
        "counted": counted,
        "roster_size": len(current),
        "turnout": counted / len(current) if current else 0.0,
        # The three ways a vote or a member does not reach the count. Present
        # on every tally, zero or not, so a reader never has to wonder whether
        # the field was omitted or the number was zero.
        "unverified": unverified,
        "members_without_pinned_key": len(unpinned),
        "votes_from_former_members": off_roster,
        "complete": unverified == 0 and not unpinned,
    }
