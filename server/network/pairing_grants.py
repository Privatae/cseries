"""Pairing grants — the anchor host admits (or refuses) other hosts on its machine.

THE PROBLEM THIS CLOSES. Before anchoring, anyone who could write the bastion's
config dir and held a `skb_` token from their OWN host could pair that host to
the machine, and the box asked no existing host (the gateway's pair.go), so a
stranger got control too. On an anchored machine every
pairing after the anchor is PENDING — no tunnel scope, no worker lanes, no DCN
dir — until the anchor host's user approves it. This module is the host half:

  heartbeat  ─▶ mode / anchor / pairings / unapproved attempts are persisted
                (topology.upsert_heartbeat narrows them; record_and_notify here
                keeps the durable "have we asked the user" row and raises the
                inbox events)
  approve    ─▶ the user's step-up, then a PAIRING GRANT signed with this
                host's entity_sign key — the SAME key and canonical form the
                DCN store grants use (nac_stepup.sign_policy_terms) — pushed to
                the box over this host's own mgmt lane as `pairing_grant_sync`
  deny       ─▶ recorded here only. Nothing goes to the box: the pairing stays
                pending there and keeps being reported, which is the point —
                a refused attempt is still an attempt the user should see
  revoke     ─▶ signed `pairing_revoke` terms, `pairing_grant_drop` to the box;
                it tears the peer's lanes and WireGuard peer down at once

WHAT IS AND IS NOT TRUSTED. The heartbeat fields are the box's word about
itself and authorize nothing on this host. The grant is enforced by ONE party:
the machine it names, against the anchor key it pinned at `claim-anchor`. A
grant from any other key is refused there and reported. This host never
consults the anchor claim for anything but "is this the machine I anchored,
so may I approve on it".

CANONICAL BYTES. The box verifies the signature over the exact bytes it is
handed and parses the terms from them — it never re-canonicalizes (Python and
Go canonical JSON differ on unicode). So the terms travel as
base64(canonical JSON) beside base64(signature), and the canonical form is
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` —
nac_stepup._canonical, reused here rather than re-implemented. The fixed
vector in scripts/test_pairing_grants.py is what the Go side asserts against.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import re
from typing import Any, Optional

from server.deep_link import app_link

logger = logging.getLogger("privatae.network.pairing_grants")

# The op families a pairing grant may cap. A grant's `caps` is a CEILING for the
# subject host on that machine — which families its own user may later be
# granted within (design §3b). Deny beats allow at the box. The families
# (anchor.go allCaps): observe (read machine state), session (the S frame —
# terminal sessions), dcn (context and stores), harness (the H frame — coding
# jobs), egress (the reverse egress lane — the host's traffic routed out
# through the machine), inference (2026-09-10: the three inbound forwards the
# host dials on the machine — its ollama embedder, an OpenAI-compatible server,
# the PII sterilizer; before this they were the one lane every pairing got
# regardless of its ceiling), models (CHANGING which models are on the
# machine — pulling one onto the user's disk, dropping one this host put there;
# the gateway's modelswap.go). The anchor holds all of them; a member with no
# verified grant is default-deny; stepup_* is ungated; pairing_* is anchor-only.
# A bastion from before the build that carries the family
# ignores a cap it does not know, so granting inference to such a box
# changes nothing there.
#
# `models` is DELIBERATELY its own family rather than part of `inference`, and
# that is the whole governance point: USING a model that is already
# loaded and DECIDING what gets downloaded onto someone's disk are different
# permissions. Appending a family leaves every grant already given out exactly
# as wide as it was when the user gave it. The box enforces the same separation
# — anchor.go opFamily maps the "model:" op prefix to "models", and
# TestModelOpsRefusedToAMemberGrantedInference pins that a pairing holding
# inference is still refused every model:* op.
# `services` (bastion 0.34.0): a server the host built on the machine and leaves running
# (client-go/service.go) — its own family, because a grant to run a job there and clean up
# is not a grant to leave a server holding someone's repos. Appended, so every grant already
# given out is exactly as wide as it was.
CAPS = ("observe", "session", "dcn", "harness", "egress", "inference", "models", "services")

# A ceiling INSIDE the inference family. `caps` says WHICH LANES a host
# may reach on the machine; `models` says which models it may use once it is on
# the inference lane. Empty = unscoped, which is what every grant signed before
# this existed carries and what the anchor always holds — so adding the field
# widens nothing and narrows nothing that already went out.
#
# The case it exists for: someone serves a model
# on their own GPU and wants a friend to use it, and the friend must be able to
# say truthfully "the only thing we can do with this machine is use the qwen4
# model". `caps: ['inference']` alone could not say that — it opened every
# loaded model, the OpenAI-compatible server beside it, and the PII sterilizer.
#
# ONE RESERVED ENTRY. The sterilizer is a bearer-authed service, not a model, so
# no model name can name it and a scoped grant refuses it unless the list
# contains exactly `pii`. It cannot collide with a real model: ollama's names
# always carry a tag (`qwen3:4b`, `all-minilm:latest`), so a bare `pii` is never
# one. The box holds the matching half in client-go/modelscope.go.
PII_LANE_ENTRY = "pii"
# A model name as ollama and the OpenAI dialect write them: letters, digits and
# the punctuation a repo/tag carries. Checked because this string is signed into
# a grant the box then matches requests against — a name with a comma or a
# newline in it would not mean on the box what it read like on the card.
_MODEL_NAME_MAX = 128

# The states a pairing reports. Anything else is refused at the boundary.
PAIRING_STATES = ("active", "pending", "revoked")

MODES = ("local", "networked")

# An attempt that has sat unanswered this long is raised as an unapproved
# pairing attempt; the raise repeats at most once per day. Ten minutes is long
# enough for the ordinary case (the user pairs, then approves from the card
# they were just shown) and short enough that a stranger's attempt is on the
# record the same hour.
UNAPPROVED_AFTER = _dt.timedelta(minutes=10)
UNAPPROVED_REPEAT = _dt.timedelta(days=1)

# A pairing grant expires like a management grant does by default (v060: 30
# days); the anchor renews by approving again. Empty expiry is allowed by the
# contract and means "until revoked".
DEFAULT_TTL_DAYS = 30

# Mgmt op ids the box answers ONLY on the anchor host's connection.
OP_GRANT_SYNC = "pairing_grant_sync"
OP_GRANT_DROP = "pairing_grant_drop"
OP_LIST = "pairing_list"
# Named into the same prefix as the three above, and that is the gate:
# The gateway's handleMgmtConnFor refuses every "pairing_" op from a pairing
# that is not the anchor's before any handler runs, while an op id outside its
# family table falls through to ALLOW. Do not rename it out of the prefix.
OP_REDEEM = "pairing_redeem"
# The box calls the platform's enrolment ingress inside this op and waits on it
# (the gateway's pair.go: a 30s client), so the relay has to outlast that.
REDEEM_DISPATCH_TIMEOUT = 45.0

_CONTROL = {c: None for c in range(0x20)} | {0x7f: None}


def _plain(s: str, limit: int = 120) -> str:
    """A bastion-supplied string safe to drop into a plain-text line (same
    reason as peer_discovery._plain: a name is the box's word, up to 253
    chars, and a newline inside it forges structure in the one message whose
    job is to describe it)."""
    out = str(s or "").translate(_CONTROL).strip()
    return out[:limit] + ("…" if len(out) > limit else "")


# ── Tables ──
#
# All three tables this module reads and writes are declared, applied at boot by
# entrypoint.sh, and not mirrored here:
#
#   network_bastion_pairing  migrations/v133_pairing_grants.sql:62
#   nac_pairing_grants       migrations/v133_pairing_grants.sql:90
#     + its `models` column  migrations/v140_machine_offers.sql:51 — the model
#       scope on grants signed before v140 existed; '[]' is exactly what those
#       grants mean, unscoped, so the default rewrites nothing.
#   network_bastion_other    migrations/v136_bastion_second_bastion_detection.sql:80
#       Second-bastion findings (2026-09-12), keyed on the OS user rather than
#       the path: one user is one installation, and a path that moves underneath
#       it is that same installation getting further along.


# ── Narrowing at the trust boundary (pure; asserted by scripts/test_pairing_grants.py) ──

def narrow_mode(v: Any) -> Optional[str]:
    """'local' | 'networked' | None. None for anything else, including a bastion
    that sent no mode — that is "too old to say", never "local"."""
    s = str(v or "").strip().lower()
    return s if s in MODES else None


def narrow_anchor(v: Any) -> Optional[dict]:
    """The anchor claim, whitelisted to its six fields. None when absent or when
    the required identity (owner + anchor_host) is missing. `verified` is added
    from the signature check; the claim is stored either way and the app words
    an unverified one as such — a stored signature nobody checked would read as
    a checked one (the v132 owner_claim lesson)."""
    if not isinstance(v, dict):
        return None
    owner = str(v.get("owner") or "").strip()[:120]
    anchor_host = str(v.get("anchor_host") or "").strip()[:253]
    if not owner or not anchor_host:
        return None
    claim = {
        "owner": owner,
        "anchor_host": anchor_host,
        "anchor_handle": str(v.get("anchor_handle") or "").strip()[:64],
        "claimed_at": str(v.get("claimed_at") or "").strip()[:40],
        "public_key": str(v.get("public_key") or "").strip()[:120],
        "signature": str(v.get("signature") or "").strip()[:200],
    }
    claim["verified"] = anchor_claim_verifies(claim)
    return claim


def anchor_signing_input(claim: dict) -> bytes:
    """The bytes the anchor claim's signature covers: "anchor\\n<owner>\\n
    <anchor_host>\\n<claimed_at>" — client-go/anchor.go anchorSigningInput,
    kept in one function there and one here. The leading
    "anchor\\n" is domain separation from the owner claim ("owner\\n…", owner.go),
    so one signature can never be replayed as the other. An older unprefixed
    claim reads verified: false, never true — the safe direction."""
    return (f"anchor\n{claim.get('owner') or ''}\n{claim.get('anchor_host') or ''}\n"
            f"{claim.get('claimed_at') or ''}").encode()


def anchor_claim_verifies(claim: dict) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = base64.b64decode(claim.get("public_key") or "", validate=True)
        sig = base64.b64decode(claim.get("signature") or "", validate=True)
        if len(pub) != 32:
            return False
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, anchor_signing_input(claim))
        return True
    except Exception:
        return False


def _narrow_host_list(v: Any, *, with_state: bool) -> Optional[list]:
    if not isinstance(v, list):
        return None
    out = []
    for e in v[:64]:
        if not isinstance(e, dict):
            continue
        host_name = str(e.get("host_name") or "").strip()[:253]
        if not host_name:
            continue
        # `since` IS first_seen on the box (anchor.go: when the host was first
        # paired there, kept across restarts); either key is accepted.
        row = {
            "host_name": host_name,
            "handle": str(e.get("handle") or "").strip()[:64],
            "since": str(e.get("since") or e.get("first_seen") or "").strip()[:40],
        }
        if with_state:
            state = str(e.get("state") or "").strip().lower()
            if state not in PAIRING_STATES:
                continue
            row["state"] = state
        out.append(row)
    return out


def narrow_pairings(v: Any) -> Optional[list]:
    """[{host_name, handle, state, since}] with state in PAIRING_STATES. None
    when the box sent no list (older bastion) — kept distinct from [] all the
    way to the column, as peers is."""
    return _narrow_host_list(v, with_state=True)


def narrow_attempts(v: Any) -> Optional[list]:
    """[{host_name, handle, since}] — the pending pairings nobody approved."""
    return _narrow_host_list(v, with_state=False)


# The kinds otherbastions.go reports, worst first. A kind outside this set is
# dropped rather than stored: the UI and the host skill both branch on it, and an
# unknown value would render as an unstyled pill with no wording behind it.
OTHER_BASTION_KINDS = ("paired", "state", "binary")


def narrow_other_bastions(v: Any) -> Optional[list]:
    """[{kind, user, uid, state_dir|binary_path, pairings, last_change}] — other
    bastion installations the box found on its own machine (design 2026-09-09 §7
    step 2; bastion otherbastions.go).

    None when the box sent no key, and that distinction is doing MORE work here
    than it does for peers: the bastion omits this key entirely when it could not
    LOOK (not root, or not Linux), so None means "unknown", never "clean". A host
    that renders None as "no second bastion" would be reporting a clean machine
    off a box that never checked, which is the one failure this feature cannot
    afford. [] means it looked and found none.

    Paths are stored as reported and are only ever displayed. They name files in
    another OS user's home directory, so nothing downstream may open one."""
    if not isinstance(v, list):
        return None
    out, dropped = [], 0
    for e in v[:64]:
        if not isinstance(e, dict):
            dropped += 1
            continue
        kind = str(e.get("kind") or "").strip().lower()
        if kind not in OTHER_BASTION_KINDS:
            dropped += 1
            continue
        try:
            uid = int(e.get("uid"))
        except (TypeError, ValueError):
            # uid is the DEDUPE KEY (network_bastion_other is keyed on it), so a
            # finding whose uid will not parse cannot be tracked: two of them
            # would collide on one row and alternate, re-raising for ever. The
            # bastion always sends an int it read from passwd, so this is a
            # malformed payload rather than a real machine state.
            dropped += 1
            continue
        try:
            pairings = max(0, int(e.get("pairings") or 0))
        except (TypeError, ValueError):
            pairings = 0
        out.append({
            "kind": kind,
            "user": str(e.get("user") or "").strip()[:64],
            "uid": uid,
            "state_dir": str(e.get("state_dir") or "").strip()[:512],
            "binary_path": str(e.get("binary_path") or "").strip()[:512],
            "pairings": pairings,
            "last_change": str(e.get("last_change") or "").strip()[:40],
        })
    # A list where EVERYTHING was dropped is not "the machine is clean" — it is a
    # box saying something this pod cannot read, which is exactly the "unknown"
    # case. Returning [] there would store an all-clear AND mark every existing
    # finding gone, so a bastion newer than the pod (reporting a kind added
    # later) would silently retract real findings the moment it upgraded.
    if dropped and not out:
        logger.info("other_bastions: all %d entries unreadable — treating as 'did not look'", dropped)
        return None
    return out


# The states of the accounting a delta pass was computed against — bastion
# client-go/deltarun.go:37-43, kept as one set there and one here. A value
# outside this set means the beat is not describing a comparison this pod
# understands, and narrow_detect returns None rather than storing it: the whole
# point of the field is that "compared, found nothing" and "never compared" can
# never read the same, and an unknown state is the second of those.
DETECT_BASELINE_STATES = ("ok", "none", "edited", "not-administered",
                          "taken-under-older-rules")

# A count this side of absurd. The bastion sends what it counted on one machine;
# anything past this is a malformed payload, and clamping (rather than dropping
# the beat) keeps a real critical visible even in a payload with one bad field.
_DETECT_COUNT_MAX = 1_000_000


# The entry classes a bastion's accounting can hold (client-go deltaClasses).
DETECT_CLASSES = ("process", "scheduler", "listener", "identity")


def narrow_detect(v: Any) -> Optional[dict]:
    """The detection plane's summary line (bastion deltarun.go
    deltaSummary, sent as `findings` on the beat).

    WHAT THIS IS NOT: it is not the findings. The rows stay on the box, where
    `observe:delta` prints them, and they land in the machine store the host
    reads through `dcn` (built 2026-09-19: client-go/machinestore.go on the
    box, runtime/oversight_store.py here). This is the summary —
    the counts per level, plus the state of the accounting they were computed
    against — and it is the half a host can react to without pulling anything.

    None when the box sent no key (a bastion older than v0.30.0, or one whose
    first pass has not completed), and that distinction carries the same weight
    it does for other_bastions: None is "this box never said", never "this box
    is clean". A reader that renders None as "no findings" would report a quiet
    machine off a box that never looked.

    `baseline` is REQUIRED and must be a state this pod knows. A summary with
    all-zero counts is meaningless without it — `baseline: none` means nobody
    ever said what normal was on that machine, which is the opposite of an
    all-clear, and the two are one field apart. So a payload that cannot say
    which of those it is does not get stored as either.

    Pure, so scripts/test_pairing_grants.py can pin it."""
    if not isinstance(v, dict):
        return None
    baseline = str(v.get("baseline") or "").strip().lower()
    if baseline not in DETECT_BASELINE_STATES:
        return None

    def _count(key: str) -> int:
        try:
            return max(0, min(_DETECT_COUNT_MAX, int(v.get(key) or 0)))
        except (TypeError, ValueError):
            return 0

    return {
        "baseline": baseline,
        "baseline_taken_at": str(v.get("baseline_taken_at") or "").strip()[:40],
        # Which halves of the machine the accounting actually covers. Stored as
        # reported because the bastion names each half separately (a
        # baseline once said "all-processes" while the scheduler half was
        # recognised-only), and a host that states coverage it did not verify
        # would be overstating the reach of the whole plane.
        # The cap fits every line the bastion can build: four halves, the
        # longest being 98 characters. At 64 the identities half was cut off,
        # so the Host was told less than the box reported, about exactly the
        # half that says whether accounts are watched at all.
        "coverage": str(v.get("coverage") or "").strip()[:160],
        "at": str(v.get("at") or "").strip()[:40],
        "critical": _count("critical"),
        "high": _count("high"),
        "warn": _count("warn"),
        "notice": _count("notice"),
        "new_since": str(v.get("new_since") or "").strip()[:40],
        # Came and went during the pass: not findings, but a storm of them is
        # itself a signal (§5c), so the count rides along.
        "short_lived": _count("short_lived"),
        # How many events the deterministic noise filter took out. Carried so a
        # plane that has gone quiet because it is filtering everything is
        # distinguishable from a machine that is genuinely quiet.
        "filtered": _count("filtered"),
        "note": str(v.get("note") or "").strip()[:500],
        # The process walk did not happen this pass (bastion 0.31.1): the zero
        # counts mean "nothing looked". Its own key, because the reading below
        # is an allowlist on `baseline` and a failure carried only in `note`
        # still read as clean.
        "walk_failed": str(v.get("walk_failed") or "").strip()[:200],
        # The entry classes this pass did NOT compare (bastion: the listener or
        # identity reader could not read, or the accounting predates the
        # class). Zero counts then say nothing about those classes, so the
        # reading must not call the pass clean. Known class names only, in a
        # fixed order; anything else is dropped rather than echoed.
        "skipped": [k for k in DETECT_CLASSES
                    if isinstance(v.get("skipped"), list) and k in v.get("skipped")],
    }


# How many fingerprints ride one beat. The contract says up to 20 and the box
# sends the top of its active set; they are identifiers to ask the box about
# with `observe:alerts`, never the alert bodies, so the cap bounds the heartbeat
# rather than the information — the COUNTS are the part that has to be complete,
# and they are.
ALERT_FINGERPRINT_CAP = 20

# A count past this is a malformed payload rather than a machine in trouble.
# Same bound narrow_detect uses, for the same reason: one machine's own alert
# source cannot plausibly be firing a million rules.
_ALERT_COUNT_MAX = 1_000_000


# A FULL RFC3339 instant: date, `T`, time, and a zone. The zone is required and
# the time component is required, which `datetime.fromisoformat` on its own does
# not give — it happily accepts "2026-09-16" and "20260916" (review finding). A
# bare date arriving here would be shown to an operator as the moment a machine
# last read its alert source, and subtracted from a heartbeat timestamp as if it
# were an instant; at midnight-minus-one-second it would also read as most of a
# day of staleness. The bastion emits `Z`, so requiring the zone costs nothing
# real and closes the class.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def _rfc3339_or_none(v: Any) -> Optional[str]:
    """The timestamp as reported, or None when it is absent or not a full
    RFC3339 instant.

    PARSED AND SHAPE-CHECKED, not merely length-capped, and that is the
    difference from the `at`/`baseline_taken_at` handling in narrow_detect. Those
    ride along as decoration. These two carry the window an alert state covers —
    `since` is what a host would repeat as "firing for three hours", and `at`
    drives the staleness comparison on the read side — so a string that is not an
    instant must not reach either.
    """
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not _RFC3339_RE.match(s):
        return None
    iso = s[:-1] + "+00:00" if s[-1] in ("Z", "z") else s
    try:
        parsed = _dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return s[:40]


_RECORD_SHELF_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RECORD_SHELVES_MAX = 32


def narrow_record_dropped(v: Any) -> Optional[dict]:
    """Per-shelf holes in the machine's own record, as the box sends them
    (client-go/machinestore.go storeObserveDroppedSnapshot, agents.go inside
    the observe gate): `{"observe-host": 3, ...}` — shelf name -> results the
    box could not write. AN EMPTY OBJECT WHEN THE RECORD IS WHOLE, and the key
    absent only when the pairing is not entitled to observe.

    Three answers, and they must stay three:
      {}     the box said its record is whole (an empty
             object, or only zero counts)                -> stored as {}
      {...}  holes, per shelf                            -> stored, narrowed
      None   not a dict, not sent, OR any entry this
             host could not read                         -> NULL; COALESCE keeps
                                                             the last reading
    The first version collapsed {} into None, and the writer then could not
    tell "whole" from "no longer allowed to say".

    The counter covers the OBSERVE shelves only (every storeObserveDroppedAdd
    call is inside storeRecordObserve); the findings shelf re-offers a refused
    line on the next unsealed day rather than counting it. So "no holes" means
    "no observe result was lost", not "the whole record is complete".

    A shelf name outside [a-z0-9-], a count that is not a finite integer
    (including NaN, which int() would otherwise raise on and take the whole
    beat down), or a negative count makes the beat unreadable (None). Names
    are lowercased (newer boxes do too; an older box's collision
    keeps the larger count). At most 32 shelves, largest counts first.
    """
    if not isinstance(v, dict):
        return None
    out = {}
    # WHY A MAP CAN NARROW TO NOTHING matters, because `{}` is not passive: it
    # wins the COALESCE and clears stored holes. A box that sends {"x": 0} is
    # affirmatively saying nothing was lost — that is whole. A box that sends
    # {"BAD NAME": 5} or {"observe-host": NaN} said "I lost results" in a form
    # this host cannot read — and returning {} for that would overwrite real
    # holes with an all-clear, which is the B1 failure relocated to the trust
    # boundary. So an entry rejected for any reason OTHER than asserting zero
    # makes the whole beat "not reported" (None), and the COALESCE keeps the
    # last reading. Found in review, with the seven input classes enumerated.
    unreadable = False
    for k, n in v.items():
        name = str(k).strip().lower()
        if not _RECORD_SHELF_RE.match(name):
            unreadable = True
            continue
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            unreadable = True
            continue
        try:
            n = int(n)
        except (TypeError, ValueError, OverflowError):
            unreadable = True
            continue
        if n < 0:
            unreadable = True
            continue
        if n == 0:
            continue
        # A collision after lowercasing (only from an older box) keeps
        # the LARGER count: last-write-wins under-reported and depended on
        # dict order.
        out[name] = max(out.get(name, 0), min(n, 1_000_000_000))
    if unreadable:
        return None
    if len(out) > _RECORD_SHELVES_MAX:
        keep = sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))[:_RECORD_SHELVES_MAX]
        out = dict(keep)
    return out


def narrow_alerts(v: Any) -> Optional[dict]:
    """The alert summary a bastion folds onto its heartbeat.

    The contract, as the box sends it::

        {"firing": int, "critical": int, "warning": int,
         "since": RFC3339|null, "at": RFC3339, "fingerprints": [str, ...<=20]}

    WHAT THIS IS NOT: it is not the alerts. The rows stay on the box, where
    `observe:alerts` pulls them on demand — the same split narrow_detect makes,
    and the fingerprints here are identifiers to ask that op about rather than
    anything anyone can read.

    None when the key is ABSENT, and that carries the weight the whole column
    exists for: **an absent key means this machine has no alert source
    configured**, which is not "no alerts firing". A reader that renders None as
    a quiet machine is reporting calm off a box that was never looking.

    None ALSO for anything malformed OR INCOMPLETE, and deliberately the same
    None. A payload missing any of the three counts or `at`, whose counts are not
    non-negative integers, whose fingerprints are not a list of strings, or whose
    timestamps are not full RFC3339 instants is a box saying something this pod
    cannot read — which is the "did not look" case, not the "looked and found
    nothing" case. Storing a partially-repaired summary would put a zero in front
    of an operator for a machine whose real state is unknown. Note the contrast
    with narrow_detect, which CLAMPS its counts: there the `baseline` field
    independently states whether a comparison happened, so a bad count cannot on
    its own manufacture an all-clear. Here the counts ARE the state, so a count
    that cannot be trusted takes the whole summary with it.

    Pure, so scripts/test_alerts_summary.py can pin it.
    """
    if not isinstance(v, dict):
        return None

    # PRESENCE IS REQUIRED, not defaulted (review finding, and it was a real
    # hole). Defaulting a missing count to 0 meant `narrow_alerts({})` returned a
    # complete zero-filled summary — the ONLY all-clear this vocabulary has,
    # manufactured out of a payload that said nothing. Two live shapes reach it:
    # a future bastion that renames a field, and an error body like
    # `{"error": "alertmanager unreachable"}`, which is the box telling us it
    # could NOT read and would have been stored as "read it, all clear".
    #
    # So all three counts must be present ints, and `at` must be present and a
    # full RFC3339 instant. Together they are the proof that a read actually
    # happened: counts without a time cannot be placed, and a time without counts
    # says nothing. Anything short of both is the "did not look" case.
    counts = {}
    for key in ("firing", "critical", "warning"):
        raw = v.get(key)
        # bool is an int in Python and True would silently become 1. A boolean
        # where a count belongs is a payload from something that does not
        # implement this contract.
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        if raw < 0 or raw > _ALERT_COUNT_MAX:
            return None
        counts[key] = raw

    # `at` is REQUIRED. `since` stays nullable, because the contract makes it so:
    # nothing firing means nothing has a start time, and an Alertmanager can
    # report a firing rule with no startsAt.
    at = _rfc3339_or_none(v.get("at"))
    if at is None:
        return None
    since_raw = v.get("since")
    since = None
    if since_raw is not None and since_raw != "":
        since = _rfc3339_or_none(since_raw)
        if since is None:
            return None

    fps_raw = v.get("fingerprints")
    if fps_raw is None:
        fps_raw = []
    if not isinstance(fps_raw, list):
        return None
    # EVERY MEMBER IS VALIDATED BEFORE THE CAP IS APPLIED (review finding).
    # Capping first made the refusal depend on POSITION: a non-string at index 3
    # rejected the payload and the same non-string at index 25 was silently
    # dropped, so whether a malformed beat was caught turned on how many
    # fingerprints happened to precede it. A box sending the wrong type sends it
    # everywhere, and the check has to see the whole list to say so.
    for fp in fps_raw:
        if not isinstance(fp, str):
            return None
    fingerprints = []
    for fp in fps_raw[:ALERT_FINGERPRINT_CAP]:
        fp = fp.strip()[:128]
        if fp:
            fingerprints.append(fp)

    return {
        "firing": counts["firing"],
        "critical": counts["critical"],
        "warning": counts["warning"],
        # When the current alert state began. None with firing > 0 is legal —
        # an Alertmanager that reports no startsAt for a rule — and a reader
        # says "for an unknown period" rather than inventing one.
        "since": since,
        # When the box last READ its alert source. Distinct from last_seen_at:
        # a machine can be beating happily while the alert read inside it has
        # been failing for an hour, and comparing these two is exactly how the
        # read side derives `stale` (tools._alerts_read_lag_s). Required, because
        # counts this pod cannot place in time cannot be judged fresh.
        "at": at,
        "fingerprints": fingerprints,
    }


# ── host posture, as the box reports it on the beat (v153) ─────────────────
#
# The COMPACT subset the gateway folds onto every heartbeat
# (observe_ops.go `heartbeatPosture`), refreshed on the box
# every 5 minutes (observe_ops.go:795 `postureRefreshEvery`). `input` and `puv`
# are deliberately NOT on the beat — they stay on `observe:host` — so no rule on
# this side may read them.
#
# Field -> type, taken from the Go struct tags rather than from a sample beat:
#   ufw pubwall ssh22 f2b reboot   string
#   sec cpus diskpct                int
#   jrn                             int64  (-1 = the box could not read it)
#   load                            float64
_POSTURE_STR_FIELDS = ("ufw", "pubwall", "ssh22", "f2b", "reboot")
_POSTURE_INT_FIELDS = ("sec", "cpus", "diskpct", "jrn")
_POSTURE_STR_MAX = 64
# Absurd-value ceilings, the same shape narrow_detect clamps its counts with and
# for the same reason: one bad field must not take a whole posture read with it,
# because the fields are INDEPENDENT here. `sec` saying nonsense says nothing
# about whether `pubwall` is fail-open. (Contrast narrow_alerts, which refuses
# the whole payload — there the counts ARE the state, so one bad count makes the
# state unknowable.)
_POSTURE_SEC_MAX = 100_000        # pending security packages on one machine
_POSTURE_CPUS_MAX = 4_096
# Deliberately NOT 100. `diskpct` comes off `df -P /` on the box, and clamping an
# absurd value INTO the band a rule tests would be the narrower inventing the
# finding: 500 clamped to 100 reads as a full root filesystem. The ceiling is
# only here to bound the stored value, so it sits well outside every band.
_POSTURE_DISKPCT_MAX = 4_096
_POSTURE_JRN_MAX = 10 * 365 * 24 * 3600   # seconds since the last journal line
_POSTURE_LOAD_MAX = 100_000.0


def narrow_posture(v: Any) -> Optional[dict]:
    """The host-posture summary a bastion folds onto its heartbeat.

    None when the key is absent or unreadable, and — as everywhere else on this
    boundary — that is "nobody read this machine's posture", never "this
    machine's posture is fine". A bastion too old to send it, a non-Linux box
    (there is no posture key off Linux, observe_ops.go:810) and the first beat
    after start all land here.

    PER-FIELD, NOT ALL-OR-NOTHING, and the asymmetry with narrow_alerts is the
    point. There the three counts jointly ARE the state, so one bad count makes
    the whole reading unknowable and the payload is refused. Here each field is
    an independent probe of a different thing: a `sec` that arrives as a string
    tells you nothing about whether the firewall chain is fail-open, and
    dropping the beat over it would hide a real finding behind a cosmetic one.
    So a malformed field becomes None and the rules that read it SKIP — which is
    safe precisely because the ledger only ever writes findings and never
    all-clears, so a field nobody read produces no row rather than a green one.

    A payload with no readable field at all returns None: that is a box saying
    something this pod cannot read, which is the "did not look" case.

    Pure, so scripts/test_oversight_ledger.py can pin it."""
    if not isinstance(v, dict):
        return None
    out: dict = {}
    seen = 0
    for k in _POSTURE_STR_FIELDS:
        raw = v.get(k)
        if isinstance(raw, str) and raw.strip():
            out[k] = raw.strip()[:_POSTURE_STR_MAX]
            seen += 1
        else:
            out[k] = None
    _int_max = {"sec": _POSTURE_SEC_MAX, "cpus": _POSTURE_CPUS_MAX,
                "diskpct": _POSTURE_DISKPCT_MAX, "jrn": _POSTURE_JRN_MAX}
    for k in _POSTURE_INT_FIELDS:
        raw = v.get(k)
        # bool is an int in Python; True must never become a count of 1.
        if isinstance(raw, bool) or not isinstance(raw, int):
            out[k] = None
            continue
        # `jrn` is the ONE field with a meaningful negative: -1 is the box
        # saying it could not read a journal timestamp (observe_ops.go), which
        # is not zero seconds since the last line. Clamping it to 0 would report
        # a box with dead logging as one that just logged.
        lo = -1 if k == "jrn" else 0
        out[k] = max(lo, min(_int_max[k], raw))
        seen += 1
    load = v.get("load")
    if isinstance(load, bool) or not isinstance(load, (int, float)):
        out["load"] = None
    else:
        out["load"] = max(0.0, min(_POSTURE_LOAD_MAX, float(load)))
        seen += 1
    if not seen:
        logger.info("posture: no readable field on the beat — treating as 'did not look'")
        return None
    return out


def settle_attempts(stored: Optional[list], attempts: Optional[list],
                    pairings: Optional[list]) -> Optional[list]:
    """What the unapproved-attempts column should hold after this beat.

    The box sends an explicit [] once it enumerated and nothing qualifies, and
    omits the key only when enumeration failed — so a list
    replaces, None keeps what is stored. ON TOP of that: any stored attempt
    whose subject the same beat reports as active or revoked is dropped, so a
    box that predates the fix (it omitted the key on a quiet machine) cannot
    leave a permanent red pill after the pairing was settled. Pure; asserted
    by scripts/test_pairing_grants.py."""
    settled = {p.get("host_name") for p in (pairings or [])
               if isinstance(p, dict) and p.get("state") in ("active", "revoked")}
    base = attempts if attempts is not None else stored
    if base is None:
        return None
    return [a for a in base if isinstance(a, dict) and a.get("host_name") not in settled]


def _parse_since(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        v = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=_dt.timezone.utc)
    return v


# ── Terms + signing (the DCN store grant's signer, reused) ───────────────────

def _rfc3339(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def grant_terms(*, anchor: str, server: str, subject: str, subject_handle: str,
                caps: list[str], issued_at: str, expires: str = "",
                models: Optional[list[str]] = None) -> dict:
    """The pairing grant's terms, key for key as the contract states them. Keys
    are sorted at canonicalization, so insertion order here is for the reader.

    `models` is OMITTED when empty, never sent as []. The box reads the field's
    absence as "unscoped" (client-go/modelscope.go), and an empty list would
    canonicalize to different bytes for a grant that means exactly what every
    grant before `models` existed meant — the fixed vector in test_pairing_grants.py is
    the check that keeps those bytes stable."""
    terms = {
        "anchor": anchor,
        "caps": list(caps),
        "expires": expires or "",
        "issued_at": issued_at,
        "kind": "pairing_grant",
        "server": server,
        "subject": subject,
        "subject_handle": subject_handle or "",
    }
    if models:
        terms["models"] = list(models)
    return terms


def revoke_terms(*, server: str, anchor: str, subject: str, issued_at: str) -> dict:
    return {
        "anchor": anchor,
        "issued_at": issued_at,
        "kind": "pairing_revoke",
        "server": server,
        "subject": subject,
    }


def canonical(terms: dict) -> bytes:
    """nac_stepup._canonical — ONE canonical form for every signed record this
    host produces. Imported lazily because nac_stepup pulls in the pool-backed
    key store; the pure narrowing above must stay importable without it."""
    from server.network.nac_stepup import _canonical
    return _canonical(terms)


def normalize_caps(caps: Any) -> list[str]:
    """A subset of CAPS, deduplicated, in CAPS order (so two approvals of the
    same set canonicalize identically). Raises ValueError on an unknown cap —
    a typo must not silently grant a smaller ceiling than the user read."""
    if caps is None:
        return []
    if isinstance(caps, str):
        caps = [c for c in caps.replace(";", ",").split(",")]
    want = {str(c).strip().lower() for c in caps if str(c).strip()}
    bad = sorted(want - set(CAPS))
    if bad:
        raise ValueError(f"unknown cap(s) {bad}; caps are {list(CAPS)}")
    return [c for c in CAPS if c in want]


def normalize_models(models: Any) -> list[str]:
    """The model scope: deduplicated, lowercased, sorted, so two approvals of
    the same set canonicalize identically (the same discipline normalize_caps
    follows). None/empty → [] → an UNSCOPED grant.

    Unlike caps there is no vocabulary to check against — the names are whatever
    that machine serves, and this host does not get to decide what a model on
    someone else's box is called. What IS checked is the shape, because the
    string is signed into a grant the box matches live requests against: a name
    carrying a comma, a space or a control character would not mean on the box
    what it read like on the card the user approved."""
    if models is None:
        return []
    if isinstance(models, str):
        models = models.replace(";", ",").split(",")
    out: set[str] = set()
    for m in models:
        m = str(m).strip().lower()
        if not m:
            continue
        if len(m) > _MODEL_NAME_MAX:
            raise ValueError(f"model name is too long (max {_MODEL_NAME_MAX}): {m[:40]}…")
        if any(ch.isspace() for ch in m) or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in m):
            raise ValueError(f"model name must not contain spaces or control characters: {m[:40]!r}")
        if "," in m or '"' in m or "\\" in m:
            raise ValueError(f"model name must not contain a comma, quote or backslash: {m[:40]!r}")
        out.add(m)
    return sorted(out)


def describe_models(models: list[str]) -> str:
    """What the scope means, in a sentence a person reads on a card. The whole
    point of the feature is that this sentence is TRUE."""
    if not models:
        return "every model this machine serves"
    named = [m for m in models if m != PII_LANE_ENTRY]
    parts = []
    if named:
        parts.append(", ".join(named))
    if PII_LANE_ENTRY in models:
        parts.append("the PII sterilizer")
    return " and ".join(parts) if parts else "nothing"


def consent_terms(*, server: str, anchor: str, next_anchor: str, issued_at: str, expires: str) -> dict:
    """The current anchor's consent to a rotation: `subject` is the host_name
    that may claim the anchor next. The box burns it on claim-anchor --rotate
    and refuses one older than 30 days (anchor.go consentMaxAge)."""
    return {
        "anchor": anchor,
        "expires": expires,
        "issued_at": issued_at,
        "kind": "anchor_consent",
        "server": server,
        "subject": next_anchor,
    }


async def _verify_pubkey_b64(pool, eid: int) -> str:
    """This host's entity verify key, base64 raw — the TOFU pin the box takes
    on the FIRST pairing_grant_sync (a fresh anchor may hold no DCN grant yet,
    so no DCN pin exists). Sent on every sync; the box ignores it once pinned."""
    from server.network import nac_stepup
    return base64.b64encode(await nac_stepup.entity_verify_pubkey(pool, eid)).decode()


async def _sign(pool, eid: int, terms: dict) -> tuple[str, str]:
    """(terms_b64, signature_b64) under this host's entity_sign key. The seam
    the tests replace with a fixed key."""
    from server.network import nac_stepup
    _key_id, sig_b64 = await nac_stepup.sign_policy_terms(pool, eid, terms)
    return base64.b64encode(canonical(terms)).decode(), sig_b64


async def _verify_stepup(pool, code: str) -> bool:
    from server.network import nac_stepup
    return await nac_stepup.verify_stepup(pool, code)


async def _dispatch(pool, eid: int, server: str, op: str, args: list,
                    timeout: Optional[float] = None) -> dict:
    """Raw relay dispatch to the box — nac_dcn._dispatch_box, the same transport
    dcn_grant_sync rides (maps an unreachable box to {_offline: True} instead
    of raising)."""
    from server.network.nac_dcn import _dispatch_box
    return await _dispatch_box(pool, eid, server, op, args, timeout=timeout)


async def _own_host_names(eid: int) -> tuple:
    """EVERY host_name this host answers to, canonical first (one per
    server.host_names.HOST_SUFFIXES). Empty tuple when the handle cannot be
    resolved right now.

    Plural on purpose, and the plural is load-bearing. A box pins the name it
    was paired on (the gateway's config.go) and signs its anchor
    claim with that exact string (anchor.go); it then refuses any
    record naming a different anchor (anchor.go:311-312). So the name a box
    reports is the one IT was given, which after an address move is not the one
    this pod would mint today. Comparing against a single derived name would
    403 every already-anchored machine on the fleet.
    """
    from server.network import bastion_enroll
    handle = await bastion_enroll.resolve_self_handle(eid)
    return bastion_enroll._host_names(handle) if handle else ()


class PairingError(Exception):
    """An approve/deny/revoke failure the caller surfaces — `status` is the HTTP
    code the route maps it to; the host tools report `detail`."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── Heartbeat side: record + notify ──────────────────────────────────────────

async def _bastion_row(pool, server: str) -> Optional[dict]:
    try:
        r = await pool.fetchrow(
            "SELECT token_id, hostname, anchor, pairings, gateway_version "
            "FROM network_bastion_heartbeat "
            "WHERE hostname=$1 ORDER BY last_seen_at DESC LIMIT 1", server)
    except Exception:
        return None
    if r is None:
        return None
    from server.network.topology import _json_or
    return {"token_id": r["token_id"], "hostname": r["hostname"],
            "anchor": _json_or(r["anchor"], None), "pairings": _json_or(r["pairings"], None),
            # Read the way topology.py reads its newer columns — present or not,
            # never a KeyError. Absent reads as UNKNOWN, and unknown is refused
            # for a scoped grant (_refuse_scope_the_box_cannot_enforce), so a row
            # that arrives without it fails CLOSED rather than silently signing a
            # ceiling nobody will enforce.
            "gateway_version": (r["gateway_version"] if "gateway_version" in r.keys() else None)}


# The bastion build that ENFORCES a model scope. Below it, client-go's
# pairingTerms has no Models field, so json.Unmarshal silently drops the key —
# the signature still verifies, the grant still applies, and the friend's host
# gets every model on the machine plus the PII sterilizer while the user was
# shown "may use: qwen4-embed". A signed ceiling nobody enforces is worse than
# no ceiling, because it is the one the user believes.
MODELS_MIN_BASTION = (0, 29, 0)


def _version_tuple(v: Any) -> Optional[tuple]:
    """A dotted-decimal bastion version as a comparable tuple, or None when it
    is absent or not that shape. Compared numerically, never as strings: "0.9"
    sorts ABOVE "0.29" lexically, which would pass every box on an older line."""
    parts = str(v or "").strip().split(".")
    if not parts or not parts[0]:
        return None
    out = []
    for p in parts[:4]:
        if not p.isdigit():
            return None
        out.append(int(p))
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _refuse_scope_the_box_cannot_enforce(row: dict, models: list[str]) -> None:
    """A model scope only means something on a bastion that reads the field.
    Refuse rather than sign one a box will apply as 'everything' — and refuse an
    UNKNOWN version too, because 'we could not tell' is not 'new enough'."""
    if not models:
        return
    have = _version_tuple((row or {}).get("gateway_version"))
    want = ".".join(str(n) for n in MODELS_MIN_BASTION)
    if have is None:
        raise PairingError(409, f"'{row.get('hostname')}' has not reported a version this host can "
                                f"read, so it cannot be told to share only named models. Update the "
                                f"bastion to {want} or later, or approve without a model list — "
                                f"which shares EVERY model on that machine.")
    if have < MODELS_MIN_BASTION:
        raise PairingError(409, f"'{row.get('hostname')}' runs bastion {'.'.join(str(n) for n in have)}, "
                                f"which ignores a model list: it would serve every model on the machine "
                                f"while this host told you otherwise. Update it to {want} or later "
                                f"(`privatae-bastion update`), then share again.")


async def record_and_notify(pool, entity_id: int, token_id: int, bastion_label: str,
                            pairings: Optional[list], attempts: Optional[list],
                            *, is_anchor: bool = False) -> dict:
    """Keep the durable per-(machine, subject) row current and raise what the
    user has not been told: a NEW pending pairing once (payload.event =
    'pairing_pending'), and — on the ANCHOR host only — an attempt unanswered
    past UNAPPROVED_AFTER at most once per UNAPPROVED_REPEAT (payload.event =
    'pairing_unapproved'). `is_anchor` is the box's word from this beat's
    enforcing report (anchor.go: is_anchor on the anchor pairing's beat): the
    anchor's user is asked to approve; a member is told, once, that someone
    is asking to join and its admin decides. Also retries delivery of an
    approved grant the box has not confirmed while it still reports that
    subject pending — never one the box REFUSED. Returns counts. Best-effort:
    the liveness beat that carries this must never break on it."""
    out = {"pending_raised": 0, "unapproved_raised": 0, "resynced": 0}
    if pairings is None and attempts is None:
        return out
    now = _dt.datetime.now(_dt.timezone.utc)

    rows: dict[str, dict] = {}
    for p in pairings or []:
        rows[p["host_name"]] = dict(p)
    for a in attempts or []:
        # An attempt IS a pending pairing; the box may list it in one or both.
        rows.setdefault(a["host_name"], {**a, "state": "pending"})
    try:
        for subject, p in rows.items():
            await pool.execute(
                """
                INSERT INTO network_bastion_pairing (token_id, subject, subject_handle, state, since)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (token_id, subject) DO UPDATE SET
                    subject_handle = EXCLUDED.subject_handle,
                    state          = EXCLUDED.state,
                    since          = EXCLUDED.since,
                    last_seen_at   = now()
                """,
                token_id, subject, p.get("handle") or None, p.get("state") or None,
                p.get("since") or None)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing record failed for token %s: %r", token_id, e)
        return out

    pending = [s for s, p in rows.items() if p.get("state") == "pending"]
    if not pending:
        return out

    try:
        db = await pool.fetch(
            "SELECT subject, subject_handle, since, pending_notified_at, unapproved_raised_at, "
            "denied_at FROM network_bastion_pairing WHERE token_id=$1 AND subject = ANY($2::text[])",
            token_id, pending)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing notify scan failed for token %s: %r", token_id, e)
        return out

    # Approved but undelivered (box was offline at approval): retry now, while
    # the box still says pending. One dispatch per subject per beat. A grant
    # the box REFUSED (bad key, wrong server, expired) is not retried — the
    # same bytes would be refused again every beat; the card shows the detail
    # and a fresh approve replaces the row.
    if is_anchor:
        for r in db:
            try:
                g = await pool.fetchrow(
                    "SELECT id, terms_canonical, signature FROM nac_pairing_grants "
                    "WHERE server=$1 AND subject=$2 AND revoked_at IS NULL AND synced_at IS NULL "
                    "AND (sync_detail IS NULL OR sync_detail NOT LIKE 'refused:%') "
                    "AND (expires_at IS NULL OR expires_at > now()) ORDER BY id DESC LIMIT 1",
                    bastion_label, r["subject"])
            except Exception:
                g = None
            if g is not None:
                res = await _dispatch(pool, entity_id, bastion_label, OP_GRANT_SYNC,
                                      [g["terms_canonical"], g["signature"],
                                       await _verify_pubkey_b64(pool, entity_id)])
                await _record_sync(pool, g["id"], res)
                if res.get("exit") == 0:
                    out["resynced"] += 1

    machine = _plain(bastion_label or "one of your machines")
    for r in db:
        if r["denied_at"] is not None:
            continue  # the user said no; the card shows it, no more asking
        subject, handle = r["subject"], (r["subject_handle"] or r["subject"].split(".")[0])
        if r["pending_notified_at"] is None:
            # First sight — even of a pairing that is already old on the box:
            # ONE ask, and the daily unapproved clock starts from this beat, so
            # the user is never handed the ask and the reminder together.
            if await _raise_pending(pool, token_id, machine, subject, handle, is_anchor=is_anchor):
                out["pending_raised"] += 1
            continue
        if not is_anchor:
            continue  # a member was told once; its admin decides, no reminders here
        asked = r["pending_notified_at"]
        if now - asked < UNAPPROVED_AFTER:
            continue
        last = r["unapproved_raised_at"] or asked
        if now - last < UNAPPROVED_REPEAT:
            continue
        since = _parse_since(r["since"]) or asked
        if await _raise_unapproved(pool, token_id, machine, subject, handle, since):
            out["unapproved_raised"] += 1
    return out


# How far along a second bastion is, as an ordered ladder. A finding that climbs
# it is re-raised: "someone has a binary" and "someone is serving a host off your
# machine" are different events, and collapsing them would mean the anchor hears
# about the harmless one and never about the serious one.
_OTHER_RANK = {"binary": 0, "state": 1, "paired": 2}

# A standing `paired` finding repeats on the same clock as an unapproved pairing.
# Anything less serious is said once: a binary on disk does not warrant a daily
# interruption, and an alert that cries wolf is how the serious one gets ignored.
OTHER_BASTION_REPEAT = _dt.timedelta(days=1)


def _owns_machine(*, is_anchor: bool, mode: Optional[str]) -> bool:
    """Whether THIS host is the party to ask to settle a machine-level finding.

    The anchor, on a networked box — but `is_anchor` is absent whenever the box
    has no anchor at all (anchor.go reports it only under an anchor claim), and
    that is every local-mode box. A local bastion serves exactly one host and
    that host's user owns the machine, so telling them "the host that anchored
    this decides; nothing changes for you" would address them as a bystander on
    their own computer and suppress the reminder entirely.

    A box too old to report `mode` is NOT assumed local: the narrow reading is
    the safe one. Pure, so scripts/test_pairing_grants.py can pin it."""
    return bool(is_anchor) or str(mode or "").strip().lower() == "local"


async def record_other_bastions(pool, entity_id: int, token_id: int, bastion_label: str,
                                other_bastions: Optional[list], *,
                                is_anchor: bool = False, mode: Optional[str] = None) -> dict:
    """Keep the per-(machine, OS user) row current and tell the user what is new.

    The anchor host is alerted when a new bastion is installed on the machine.
    The bastion scans its own machine and reports what
    it found (otherbastions.go); this is the half that decides what counts as
    news and says it once.

    `other_bastions` is None when the box did NOT LOOK — not root, not Linux, or
    a bastion too old to have the scan. None is returned untouched: nothing is
    raised, and nothing already recorded is cleared. Only an explicit list (which
    may be empty) is evidence about the machine.

    WHO IS ASKED TO ACT. On a networked box, the anchor — same gate as
    record_and_notify, because the anchor is the party that governs the machine.
    But `is_anchor` is absent whenever the box has no anchor at all (anchor.go
    reports it only under an anchor claim), and that is EVERY local-mode box. A
    local bastion serves exactly one host, and that host's user is the owner of
    the machine — telling them "the host that anchored this decides; nothing
    changes for you" would be addressing them as a bystander on their own
    computer, and would suppress the reminder entirely. So local mode is treated
    as the responsible party too.

    Best-effort, like record_and_notify: the liveness beat that carries this must
    never break on it."""
    out = {"raised": 0, "escalated": 0, "cleared": 0}
    # The party this host should ask to act on the finding.
    owns_it = _owns_machine(is_anchor=is_anchor, mode=mode)
    if other_bastions is None:
        return out

    machine = _plain(bastion_label or "one of your machines")
    seen_uids = []
    try:
        for o in other_bastions:
            uid = int(o.get("uid", -1))
            seen_uids.append(uid)
            await pool.execute(
                """
                INSERT INTO network_bastion_other
                    (token_id, uid, user_name, kind, state_dir, binary_path, pairings)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (token_id, uid) DO UPDATE SET
                    user_name    = EXCLUDED.user_name,
                    kind         = EXCLUDED.kind,
                    state_dir    = EXCLUDED.state_dir,
                    binary_path  = EXCLUDED.binary_path,
                    pairings     = EXCLUDED.pairings,
                    last_seen_at = now(),
                    -- A finding that COMES BACK is a new event, and the memory of
                    -- having mentioned the old one must not silence it. Without
                    -- this reset: eve's bastion is found and raised, eve removes
                    -- it (gone_at set below), eve installs it again weeks later —
                    -- and because notified_kind still says 'state', the escalation
                    -- test reads "already told them" and nobody is told again.
                    -- Permanent silence for state/binary, and for 'paired' only
                    -- the anchor's daily reminder would ever fire while a member
                    -- host heard nothing at all. Re-installation is exactly the
                    -- event the alert exists for.
                    notified_kind = CASE WHEN network_bastion_other.gone_at IS NOT NULL
                                         THEN NULL ELSE network_bastion_other.notified_kind END,
                    notified_at   = CASE WHEN network_bastion_other.gone_at IS NOT NULL
                                         THEN NULL ELSE network_bastion_other.notified_at END,
                    raised_at     = CASE WHEN network_bastion_other.gone_at IS NOT NULL
                                         THEN NULL ELSE network_bastion_other.raised_at END,
                    gone_at      = NULL
                """,
                token_id, uid, o.get("user") or None, o.get("kind") or None,
                o.get("state_dir") or None, o.get("binary_path") or None,
                int(o.get("pairings") or 0))
    except Exception as e:  # pragma: no cover - defensive
        logger.info("other-bastion record failed for token %s: %r", token_id, e)
        return out

    # A finding the box no longer reports is marked gone rather than deleted: the
    # anchor asked someone to remove a second bastion and the record of it having
    # been there is the evidence that they did.
    try:
        await pool.execute(
            "UPDATE network_bastion_other SET gone_at = now() "
            "WHERE token_id = $1 AND gone_at IS NULL AND NOT (uid = ANY($2::int[]))",
            token_id, seen_uids)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("other-bastion clear failed for token %s: %r", token_id, e)

    if not other_bastions:
        return out

    try:
        db = await pool.fetch(
            "SELECT uid, user_name, kind, state_dir, binary_path, pairings, "
            "notified_kind, notified_at, raised_at FROM network_bastion_other "
            "WHERE token_id=$1 AND uid = ANY($2::int[])", token_id, seen_uids)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("other-bastion notify scan failed for token %s: %r", token_id, e)
        return out

    now = _dt.datetime.now(_dt.timezone.utc)
    for r in db:
        kind = r["kind"] or "binary"
        told = r["notified_kind"]
        if told is None:
            new_event = True          # never mentioned
        elif _OTHER_RANK.get(kind, 0) > _OTHER_RANK.get(told, 0):
            new_event = True          # it got further along — that is news
        else:
            new_event = False
        if new_event:
            if await _raise_other_bastion(pool, token_id, machine, r, kind,
                                          escalated=told is not None, is_anchor=owns_it):
                out["escalated" if told is not None else "raised"] += 1
            continue
        # Standing reminder, anchor only, and only while it is actually serving.
        # The standing reminder goes to whoever owns the machine (anchor, or
        # the single host of a local box), and only while one is actually serving.
        if not owns_it or kind != "paired":
            continue
        last = r["raised_at"] or r["notified_at"]
        if last is not None and now - last < OTHER_BASTION_REPEAT:
            continue
        if await _raise_other_bastion(pool, token_id, machine, r, kind,
                                      escalated=False, is_anchor=True, reminder=True):
            out["raised"] += 1
    return out


async def _raise_other_bastion(pool, token_id: int, machine: str, r, kind: str, *,
                               escalated: bool, is_anchor: bool, reminder: bool = False) -> bool:
    """One inbox row (and one host message) about a second bastion on `machine`.

    The inbox write gates the mark and the host message, for the same reason
    _raise_pending does it that way: marking first would lose the alert entirely
    if the inbox were down, and writing the host message first would repeat it on
    every beat until the inbox came back."""
    user = _plain(r["user_name"] or f"uid {r['uid']}")
    where = _plain(r["state_dir"] or r["binary_path"] or "", limit=200)
    n = int(r["pairings"] or 0)

    if kind == "paired":
        what = (f"{user} is running a second bastion on {machine}, and it is serving "
                f"{n} Host{'s' if n != 1 else ''} of its own.")
        why = ("Your machine is answering to a host you never approved. Nothing that "
               "bastion does shows up in your Network app, and its pairings are not "
               "governed by your anchor.")
    elif kind == "state":
        what = f"{user} has set up a second bastion on {machine}."
        why = ("It is not paired to any host yet, so it is serving nothing — but it is "
               "one command away from doing so.")
    else:
        what = f"{user} has installed a bastion binary on {machine}."
        why = "There is no configuration beside it yet, so nothing is running."

    if escalated:
        what = "Update — " + what[0].lower() + what[1:]
    elif reminder:
        what = "Still there — " + what[0].lower() + what[1:]

    if is_anchor:
        do = (f"You anchored {machine}, so this is yours to settle. Remove that "
              f"installation on the machine, or have {user} pair to the bastion you "
              f"already run there instead of standing up their own — one machine is "
              f"meant to run one bastion.")
    else:
        do = (f"The host that anchored {machine} decides what happens about it; "
              f"nothing changes for you.")

    where_line = f"\n\nFound at: {where}" if where else ""
    ok = await _inbox(
        f"Another bastion on {machine}",
        f"{what}\n\n{why}\n\n{do}{where_line}",
        {"app": "network", "lens": "bastions", "event": "other_bastion",
         "token_id": token_id, "machine": machine, "uid": int(r["uid"]),
         "user": r["user_name"], "kind": kind, "pairings": n,
         "path": r["state_dir"] or r["binary_path"], "is_anchor": is_anchor,
         "link": app_link("network")})
    if not ok:
        logger.info("other bastion: inbox refused uid %s on %s", r["uid"], machine)
        return False
    try:
        await pool.execute(
            "UPDATE network_bastion_other SET notified_kind = $3, notified_at = now(), "
            "raised_at = now() WHERE token_id=$1 AND uid=$2", token_id, int(r["uid"]), kind)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("other bastion: mark failed for uid %s: %r", r["uid"], e)
    try:
        if is_anchor:
            host_msg = (
                f"{what} {why} You anchor {machine}, so tell the user about this and say "
                f"plainly what it means: a second bastion on a machine they anchor is "
                f"outside their governance — its pairings do not appear in their Network "
                f"app and their anchor does not gate them. The fix is on the machine "
                f"itself (remove that installation, or have {user} pair to the existing "
                f"bastion). There is no tool here that removes it for them.")
        else:
            host_msg = (
                f"{what} The host that anchored {machine} decides what happens about it. "
                f"Mention it once if it comes up; there is nothing for this host to do.")
        await _host_message(host_msg, "other_bastion")
    except Exception as e:  # noqa: BLE001
        logger.info("other bastion: host message not written: %s", type(e).__name__)
    return True


async def _host_message(content: str, action: str) -> None:
    """The host's own message list (context store inbox/messages.json) — the one
    that lands in its system prompt as "Messages For You" on its next turn, so
    it ASKS the user instead of waiting to be asked. Same two-write shape as
    gitea._notify_host_of_grant."""
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return
    import uuid
    from mastercode.context_store import ContextStore
    await ContextStore(pool, brain_service._entity_id).append_to_list("inbox/messages.json", {
        "id": str(uuid.uuid4()),
        "content": content,
        "source": "network",
        "action": action,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "delivered_to_session": None,
        "read": False,
    }, max_items=50)


async def _inbox(subject: str, body: str, payload: dict) -> bool:
    """A durable inbox row for the user. handle_inbox_append RETURNS
    {"ok": False, ...} on failure rather than raising (memory_handlers.py), so
    the caller must read the result before marking anything notified."""
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        res = await handle_inbox_append({
            "subject": subject, "body": body, "body_format": "text/plain",
            "message_kind": "system_notification", "payload": payload,
        })
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing notify: inbox raised: %r", e)
        return False
    return bool(isinstance(res, dict) and res.get("ok"))


async def _raise_pending(pool, token_id: int, machine: str, subject: str, handle: str,
                         *, is_anchor: bool) -> bool:
    """The inbox row is written FIRST and gates the host message: the inbox
    write is what marks the row notified, so a host message written before a
    failed inbox write would repeat on every beat until the inbox came back."""
    e_handle, e_subject = _plain(handle), _plain(subject)
    if is_anchor:
        subj = f"Approve {e_handle} on {machine}?"
        body = (f"{e_handle} paired a Host to your machine {machine} and is waiting for your "
                f"approval. You anchored that machine: until you approve, that pairing gets "
                f"nothing — no tunnel, no lanes, no context.\n\n"
                f"Open Network → Bastions → {machine} to approve (with your code) or deny. "
                f"If you did not expect this, deny it: the attempt stays on the record.")
        host_msg = (
            f"A new host, {e_handle} ({e_subject}), has paired to your machine {machine} and is "
            f"waiting for your approval — you anchored that machine, so nothing opens for it "
            f"until you say so. Ask the user ONCE whether to admit it and with which caps "
            f"(observe, session, dcn, harness, egress). If yes: manage_pairing op='approve' "
            f"server='{machine}' subject='{e_subject}' caps=[...] with their 2FA code. If no: "
            f"manage_pairing op='deny' server='{machine}' subject='{e_subject}'. Until then it "
            f"stays pending and is reported on every check-in.")
    else:
        subj = f"A new host is asking to join {machine}"
        body = (f"{e_handle} is asking to join {machine}, a machine you share. Its admin (the "
                f"host that anchored it) decides; nothing changes for you until they approve.")
        host_msg = (
            f"A new host, {e_handle} ({e_subject}), is asking to join {machine}; its admin decides. "
            f"Mention it to the user once if it comes up — there is nothing for this host to approve.")
    ok = await _inbox(subj, body,
                      {"app": "network", "lens": "bastions", "event": "pairing_pending",
                       "token_id": token_id, "machine": machine, "subject": subject,
                       "handle": handle, "is_anchor": is_anchor, "link": app_link("network")})
    if not ok:
        logger.info("pairing pending: inbox refused %s on %s", subject, machine)
        return False
    try:
        await pool.execute(
            "UPDATE network_bastion_pairing SET pending_notified_at = now(), "
            "unapproved_raised_at = now() WHERE token_id=$1 AND subject=$2", token_id, subject)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing pending: mark failed for %s: %r", subject, e)
    try:
        await _host_message(host_msg, "pairing_pending")
    except Exception as e:  # noqa: BLE001
        logger.info("pairing pending: host message not written: %s", type(e).__name__)
    return True


async def _raise_unapproved(pool, token_id: int, machine: str, subject: str, handle: str,
                            since: _dt.datetime) -> bool:
    e_handle, e_subject = _plain(handle), _plain(subject)
    age_h = max(1, int((_dt.datetime.now(_dt.timezone.utc) - since).total_seconds() // 3600))
    ok = await _inbox(
        f"Unapproved pairing on {machine}: {e_handle}",
        (f"{e_handle} has been waiting to pair on {machine} for about {age_h} hour(s). "
         f"It holds nothing until you approve. Approve or deny it in Network → Bastions; "
         f"this reminder repeats once a day while it waits."),
        {"app": "network", "lens": "bastions", "event": "pairing_unapproved",
         "token_id": token_id, "machine": machine, "subject": subject, "handle": handle,
         "since": _rfc3339(since), "link": app_link("network")})
    if not ok:
        return False
    try:
        await pool.execute(
            "UPDATE network_bastion_pairing SET unapproved_raised_at = now() "
            "WHERE token_id=$1 AND subject=$2", token_id, subject)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing unapproved: mark failed for %s: %r", subject, e)
    try:
        await _host_message(
            f"Unapproved pairing attempt: {e_handle} ({e_subject}) has been pending on your "
            f"machine {machine} for about {age_h} hour(s) with no answer. Tell the user once "
            f"today; manage_pairing op='approve' or op='deny' settles it.",
            "pairing_unapproved")
    except Exception as e:  # noqa: BLE001
        logger.info("pairing unapproved: host message not written: %s", type(e).__name__)
    return True


async def _record_sync(pool, grant_id: int, res: dict) -> None:
    detail = (res.get("stderr") or res.get("stdout") or "").strip()[:300]
    try:
        if res.get("exit") == 0:
            await pool.execute(
                "UPDATE nac_pairing_grants SET synced_at=now(), sync_detail=NULL WHERE id=$1", grant_id)
        else:
            await pool.execute(
                "UPDATE nac_pairing_grants SET sync_detail=$2 WHERE id=$1",
                grant_id, ("offline: " if res.get("_offline") else "refused: ") + (detail or "no detail"))
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing sync record failed for grant %s: %r", grant_id, e)


# ── The three actions ────────────────────────────────────────────────────────

async def _anchored_here(pool, eid: int, server: str) -> dict:
    """The heartbeat row for `server`, refused unless this host is its anchor.
    The box enforces this too (it answers pairing_* only on the anchor's
    connection); checking here gives the user a sentence instead of exit 126."""
    row = await _bastion_row(pool, server)
    if row is None:
        raise PairingError(404, f"no bastion registered for server '{server}'")
    anchor = row.get("anchor") or {}
    if not anchor.get("anchor_host"):
        # No token named here: from bastion 0.23.0 claim-anchor takes none on a
        # box that already holds one pairing, and this string is only ever
        # reached for a machine that IS paired to this host.
        raise PairingError(409, f"'{server}' is not anchored — run `privatae-bastion claim-anchor` "
                                f"on that machine with machine authority first "
                                f"(`sudo -H` on Linux/macOS, an Administrator PowerShell on Windows)")
    own = await _own_host_names(eid)
    if not own:
        # Without this host's own name the anchor check cannot be made, and a
        # check that cannot be made is not passed. Fail closed.
        raise PairingError(409, "this host's own handle could not be resolved right now (bridge "
                                "down or no handle yet) — the anchor check needs it; try again")
    if anchor.get("anchor_host") not in own:
        raise PairingError(403, f"'{server}' is anchored to {anchor.get('anchor_host')}, not to "
                                f"this host — only its anchor may admit or remove pairings")
    return row


def _ttl_days(ttl_days: Any, default: int) -> int:
    """0 = no expiry (the schema's word), None = default, negative = 400."""
    if ttl_days is None or ttl_days == "":
        return default
    try:
        d = int(ttl_days)
    except (TypeError, ValueError):
        raise PairingError(400, "ttl_days must be an integer (0 = no expiry)")
    if d < 0:
        raise PairingError(400, "ttl_days cannot be negative (0 = no expiry)")
    return d


async def approve_pairing(pool, eid: int, server: str, subject: str, caps: Any,
                          *, stepup_code: Optional[str] = None, approved_by: Optional[str] = None,
                          ttl_days: Optional[int] = None, models: Any = None,
                          via: str = "unknown") -> dict:
    """Admit `subject` on `server` with the given caps ceiling. WIDENS, so it
    needs the user's step-up code, always. Signs the grant, stores it, pushes
    pairing_grant_sync over the mgmt lane; an offline box gets the grant on
    its next beat (record_and_notify retries while it reports pending).

    `via` names the surface for approved_by, by the same rule as the NAC
    approvals (nac_commit.approver_for): the Network app's route passes "ui" and
    records the user; the manage_pairing tool is the host and records "host",
    even with a verified code — the entity's own workload can compute one.

    `models` narrows the inference lane to named models. It is a
    NARROWING inside a widening, so it rides the same step-up rather than
    needing one of its own — and an approval that names models while granting
    no inference at all is refused below, because it would read on the card as
    a scope and mean nothing on the box."""
    if not pool:
        raise PairingError(503, "brain pool not ready")
    server = (server or "").strip()
    subject = (subject or "").strip()
    if not server or not subject:
        raise PairingError(400, "server and subject required")
    try:
        caps = normalize_caps(caps)
    except ValueError as e:
        raise PairingError(400, str(e))
    try:
        models = normalize_models(models)
    except ValueError as e:
        raise PairingError(400, str(e))
    if models and "inference" not in caps:
        raise PairingError(400, "models narrows the 'inference' lane, so it only means something "
                                "alongside it — approve with caps ['inference'] (plus any others), "
                                "or drop the model list")
    row = await _anchored_here(pool, eid, server)
    reported = {p.get("host_name"): p for p in (row.get("pairings") or [])}
    if subject not in reported:
        # The last beat may have listed it only under unapproved attempts; the
        # durable row remembers every subject the box ever reported.
        known = await pool.fetchrow(
            "SELECT subject_handle FROM network_bastion_pairing WHERE token_id=$1 AND subject=$2",
            row["token_id"], subject)
        if known is None:
            raise PairingError(404, f"'{subject}' has not paired on '{server}' — the box has not "
                                    f"reported it; nothing to approve")
        reported[subject] = {"host_name": subject, "handle": known["subject_handle"] or ""}
    if not await _verify_stepup(pool, str(stepup_code or "")):
        raise PairingError(403, "admitting a host on your machine widens access — it needs a "
                                "valid step-up code (enroll a second factor at /nac/stepup/enroll first)")
    from server.network.nac_commit import approver_for
    handle = str(reported[subject].get("handle") or subject.split(".")[0])
    return await sign_and_push_grant(pool, eid, server, subject, handle, caps, models,
                                     row=row, ttl_days=ttl_days,
                                     approved_by=approver_for(via, approved_by))


async def sign_and_push_grant(pool, eid: int, server: str, subject: str, handle: str,
                              caps: list[str], models: list[str], *, row: Optional[dict] = None,
                              ttl_days: Optional[int] = None,
                              approved_by: Optional[str] = None) -> dict:
    """Sign a pairing grant, store it, and push it to the box. Everything
    approve_pairing does AFTER it has established who the subject is and taken
    the user's step-up code.

    THERE IS NO STEP-UP CHECK IN HERE, and that is the point of the split rather
    than an omission. The code is BURN-ON-USE (nac_stepup.consume_counter: a
    counter at or below the last accepted one is refused), so a caller that
    verified it and then called a function that verifies it again would refuse
    its own approval — which is exactly what machine_offers.accept_inbound did
    before this split, after the other host had already minted and this box had
    already redeemed. Every caller is an entry point that takes the code exactly
    once; this is not reachable from a tool or a route.

    `handle` and `row` are passed IN for the second half of the same bug: a
    pairing redeemed seconds ago is in no heartbeat yet (the box beats on a
    60-second ticker and a redeem triggers no beat), so a caller that already
    knows the subject — the redeem answer names it — must not be made to look it
    up in a report that cannot contain it."""
    if row is None:
        row = await _anchored_here(pool, eid, server)
    _refuse_scope_the_box_cannot_enforce(row, models)
    # Sign with the name the BOX pinned, not the one this pod would mint today:
    # _anchored_here has already proved it is one of ours, and the box rejects a
    # record naming any other anchor (client-go/anchor.go:311-312).
    anchor = str((row.get("anchor") or {}).get("anchor_host") or "")
    now = _dt.datetime.now(_dt.timezone.utc)
    ttl = _ttl_days(ttl_days, DEFAULT_TTL_DAYS)
    expires_dt = now + _dt.timedelta(days=ttl) if ttl > 0 else None
    handle = handle or subject.split(".")[0]
    terms = grant_terms(anchor=anchor, server=server, subject=subject, subject_handle=handle,
                        caps=caps, issued_at=_rfc3339(now),
                        expires=_rfc3339(expires_dt) if expires_dt else "",
                        models=models)
    terms_b64, sig_b64 = await _sign(pool, eid, terms)
    # approved_by comes from the entry point (nac_commit.approver_for). A caller that
    # passes none is recorded as the host, never as the user.
    async with pool.acquire() as conn:
        # A re-approval supersedes: the box keeps one grant per subject, so the
        # host keeps one live row per (server, subject) too.
        await conn.execute(
            "UPDATE nac_pairing_grants SET revoked_at=now() WHERE server=$1 AND subject=$2 "
            "AND revoked_at IS NULL", server, subject)
        gid = await conn.fetchval(
            "INSERT INTO nac_pairing_grants (entity_id, server, subject, subject_handle, caps, "
            "models, terms_canonical, signature, issued_at, expires_at, approved_by, stepup_at) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10,$11,now()) RETURNING id",
            eid, server, subject, handle, json.dumps(caps), json.dumps(models), terms_b64, sig_b64,
            now, expires_dt, approved_by or "host")
        await conn.execute(
            "UPDATE network_bastion_pairing SET denied_at=NULL, denied_by=NULL "
            "WHERE token_id=$1 AND subject=$2", row["token_id"], subject)
    from server.network.nac_mgmt import audit_mgmt
    # Third arg: the verify key the box pins on its first sync from this anchor.
    res = await _dispatch(pool, eid, server, OP_GRANT_SYNC,
                          [terms_b64, sig_b64, await _verify_pubkey_b64(pool, eid)])
    await audit_mgmt(pool, eid, server, "pairing:grant_sync", exit_code=res.get("exit"), decision="ran")
    await _record_sync(pool, gid, res)
    delivered = res.get("exit") == 0
    return {"grant_id": gid, "approved": True, "server": server, "subject": subject,
            "subject_handle": handle, "caps": caps, "models": models,
            "may_use": describe_models(models) if "inference" in caps else None,
            "expires": terms["expires"],
            "stepped_up": True, "delivered": delivered,
            "state": "active" if delivered else ("offline" if res.get("_offline") else "refused"),
            "detail": None if delivered else (res.get("stderr") or res.get("stdout") or "").strip()[:300],
            "note": None if delivered else ("the machine is not connected; the grant is delivered "
                                            "on its next check-in" if res.get("_offline") else
                                            "the machine refused the grant — see detail")}


async def redeem_on_box(pool, eid: int, server: str, token: str) -> dict:
    """Hand a pairing token minted by ANOTHER host down to our own machine and
    have the box redeem it.

    WHAT THIS IS FOR. Until now a token could only be redeemed ON the machine —
    a terminal or the box's local setup page — so letting a friend's host use a
    model on your GPU meant someone physically at the box. This is the remote
    half: the machine's anchor host directs the redeem over the mgmt lane it
    already owns, and nobody touches the machine.

    WHAT IT IS NOT. It admits nobody. The box writes the pairing PENDING with no
    tunnel and no lanes (the gateway's pair.go), exactly as a redeem at the
    machine would on an anchored box. `approve_pairing` is still the decision,
    still with a step-up code, and still the only thing that opens a lane.

    Returns {subject, handle, state} — `subject` is the host_name the box
    redeemed, which is what approve_pairing then needs. Raises PairingError; an
    offline box is a 503 rather than a silent nothing, because unlike a grant
    sync this cannot be retried later from a stored row: the token is
    single-use and short-lived, and pretending otherwise would leave the user
    waiting on something that is already dead."""
    if not pool:
        raise PairingError(503, "brain pool not ready")
    server = (server or "").strip()
    token = (token or "").strip()
    if not server or not token:
        raise PairingError(400, "server and token required")
    # _anchored_here proves this host is the machine's anchor before the token
    # goes anywhere: the box would refuse a non-anchor anyway, but a refusal
    # that costs a single-use credential is a refusal that arrived too late.
    await _anchored_here(pool, eid, server)
    res = await _dispatch(pool, eid, server, OP_REDEEM, [token],
                          timeout=REDEEM_DISPATCH_TIMEOUT)
    from server.network.nac_mgmt import audit_mgmt
    # The AUDIT NEVER CARRIES THE TOKEN. audit_mgmt records the op, not its
    # args, and nothing below echoes one either — a single-use credential in an
    # audit row is a credential at rest for as long as the row lives.
    await audit_mgmt(pool, eid, server, "pairing:redeem", exit_code=res.get("exit"), decision="ran")
    if res.get("_offline"):
        raise PairingError(503, f"'{server}' is not connected right now. A pairing token is "
                                f"single-use and expires in minutes, so there is nothing to retry "
                                f"later — ask them to send the offer again once the machine is up.")
    if res.get("exit") != 0:
        detail = (res.get("stderr") or res.get("stdout") or "").strip()[:400]
        raise PairingError(400, f"the machine refused the redeem: {detail}" if detail
                                else "the machine refused the redeem")
    try:
        out = json.loads(res.get("stdout") or "{}")
    except Exception:
        raise PairingError(502, "the machine's answer to the redeem was unreadable")
    subject = str(out.get("subject") or "").strip()
    if not subject:
        raise PairingError(502, "the machine redeemed the token but named no host")
    return {"redeemed": True, "server": server, "subject": subject,
            "handle": str(out.get("handle") or ""),
            "state": str(out.get("pairing_state") or "pending"),
            "note": "pending — it holds no lanes until it is approved with a ceiling"}


async def deny_pairing(pool, eid: int, server: str, subject: str,
                       *, denied_by: Optional[str] = None, via: str = "unknown") -> dict:
    """Record a refusal. No op to the box: the pairing stays pending there and
    keeps being reported (the attempt is the signal). Tightens — free.

    Who refused is recorded by the same rule as an approval
    (nac_commit.approver_for): the Network app's route passes via="ui" and records
    the user; the manage_pairing tool is the host and records "host". Otherwise the
    audit trail would say the person refused something they never saw."""
    from server.network.nac_commit import approver_for
    denied_by = approver_for(via, denied_by)
    if not pool:
        raise PairingError(503, "brain pool not ready")
    server, subject = (server or "").strip(), (subject or "").strip()
    if not server or not subject:
        raise PairingError(400, "server and subject required")
    row = await _bastion_row(pool, server)
    if row is None:
        raise PairingError(404, f"no bastion registered for server '{server}'")
    await pool.execute(
        """
        INSERT INTO network_bastion_pairing (token_id, subject, state, denied_at, denied_by)
        VALUES ($1, $2, 'pending', now(), $3)
        ON CONFLICT (token_id, subject) DO UPDATE SET denied_at = now(), denied_by = EXCLUDED.denied_by
        """, row["token_id"], subject, denied_by)
    from server.network.nac_mgmt import audit_mgmt
    await audit_mgmt(pool, eid, server, "pairing:deny", exit_code=0, decision="ran")
    return {"denied": True, "server": server, "subject": subject,
            "note": "recorded here only — the box keeps reporting the pending pairing until it is "
                    "removed on the machine (privatae-bastion unpair) or approved"}


async def revoke_pairing(pool, eid: int, server: str, subject: str) -> dict:
    """Sign pairing_revoke terms and push pairing_grant_drop: the box tears the
    peer's lanes and WireGuard peer down at once. Tightens — no step-up."""
    if not pool:
        raise PairingError(503, "brain pool not ready")
    server, subject = (server or "").strip(), (subject or "").strip()
    if not server or not subject:
        raise PairingError(400, "server and subject required")
    row = await _anchored_here(pool, eid, server)
    g = await pool.fetchrow(
        "SELECT id FROM nac_pairing_grants WHERE server=$1 AND subject=$2 AND revoked_at IS NULL "
        "ORDER BY id DESC LIMIT 1", server, subject)
    # Sign with the name the BOX pinned, not the one this pod would mint today:
    # _anchored_here has already proved it is one of ours, and the box rejects a
    # record naming any other anchor (client-go/anchor.go:311-312).
    anchor = str((row.get("anchor") or {}).get("anchor_host") or "")
    terms = revoke_terms(server=server, anchor=anchor, subject=subject,
                         issued_at=_rfc3339(_dt.datetime.now(_dt.timezone.utc)))
    terms_b64, sig_b64 = await _sign(pool, eid, terms)
    from server.network.nac_mgmt import audit_mgmt
    res = await _dispatch(pool, eid, server, OP_GRANT_DROP, [subject, terms_b64, sig_b64])
    await audit_mgmt(pool, eid, server, "pairing:grant_drop", exit_code=res.get("exit"), decision="ran")
    if g is not None:
        # Revoked HERE regardless of delivery: a grant the host no longer stands
        # behind must not be re-synced by the heartbeat retry. If the box was
        # offline, its next beat reports the pairing state and the user sees it.
        await pool.execute(
            "UPDATE nac_pairing_grants SET revoked_at=now(), revoke_terms_canonical=$2, "
            "revoke_signature=$3 WHERE id=$1", g["id"], terms_b64, sig_b64)
    delivered = res.get("exit") == 0
    return {"revoked": True, "server": server, "subject": subject,
            "had_grant": g is not None, "delivered": delivered,
            "detail": None if delivered else (res.get("stderr") or res.get("stdout") or "").strip()[:300],
            "note": None if delivered else ("the machine is not connected; it keeps the pairing "
                                            "until it next answers — quarantine_bastion cuts the "
                                            "whole box now if that matters" if res.get("_offline")
                                            else "the machine refused the drop — see detail")}


CONSENT_MAX_DAYS = 30
OP_ANCHOR_CONSENT = "pairing_anchor_consent"


async def consent_anchor_rotation(pool, eid: int, server: str, next_anchor: str,
                                  *, stepup_code: Optional[str] = None,
                                  days: Optional[int] = None) -> dict:
    """Consent to another host taking the anchor on `server`. WIDENS (the next
    anchor gains the power to admit pairings there), so step-up always. The
    box stores the signed consent; root on the machine then runs
    `claim-anchor --rotate` with a token from `next_anchor`, which burns it.
    Single use, at most 30 days."""
    if not pool:
        raise PairingError(503, "brain pool not ready")
    server, next_anchor = (server or "").strip(), (next_anchor or "").strip()
    if not server or not next_anchor:
        raise PairingError(400, "server and next_anchor required")
    row = await _anchored_here(pool, eid, server)
    if not await _verify_stepup(pool, str(stepup_code or "")):
        raise PairingError(403, "handing the anchor to another host widens access — it needs a "
                                "valid step-up code")
    # Sign with the name the BOX pinned, not the one this pod would mint today:
    # _anchored_here has already proved it is one of ours, and the box rejects a
    # record naming any other anchor (client-go/anchor.go:311-312).
    anchor = str((row.get("anchor") or {}).get("anchor_host") or "")
    now = _dt.datetime.now(_dt.timezone.utc)
    d = min(_ttl_days(days, CONSENT_MAX_DAYS) or CONSENT_MAX_DAYS, CONSENT_MAX_DAYS)
    terms = consent_terms(server=server, anchor=anchor, next_anchor=next_anchor,
                          issued_at=_rfc3339(now), expires=_rfc3339(now + _dt.timedelta(days=max(1, d))))
    terms_b64, sig_b64 = await _sign(pool, eid, terms)
    from server.network.nac_mgmt import audit_mgmt
    res = await _dispatch(pool, eid, server, OP_ANCHOR_CONSENT, [terms_b64, sig_b64])
    await audit_mgmt(pool, eid, server, "pairing:anchor_consent", exit_code=res.get("exit"), decision="ran")
    delivered = res.get("exit") == 0
    return {"consented": delivered, "server": server, "next_anchor": next_anchor,
            "expires": terms["expires"], "stepped_up": True, "delivered": delivered,
            "detail": None if delivered else (res.get("stderr") or res.get("stdout") or "").strip()[:300],
            "note": (f"root on {server} may now run `privatae-bastion claim-anchor --rotate <token from "
                     f"{next_anchor}>`; the consent is single-use and expires {terms['expires']}")
                    if delivered else ("the machine is not connected — consent is not stored anywhere "
                                       "until it answers; try again when it is online"
                                       if res.get("_offline") else "the machine refused the consent — see detail")}


# ── Reads (the app + get_network) ─────────────────────────────────────────────

async def list_grants(pool, eid: int, server: Optional[str] = None) -> list[dict]:
    """The pairing grants this host signed, live and revoked, newest first."""
    try:
        rows = await pool.fetch(
            "SELECT id, server, subject, subject_handle, caps, models, issued_at, expires_at, "
            "revoked_at, approved_by, stepup_at, synced_at, sync_detail FROM nac_pairing_grants "
            "WHERE ($1::text IS NULL OR server=$1) ORDER BY id DESC LIMIT 500", server)
    except Exception as e:
        logger.info("list_grants unavailable: %s", e)
        return []
    from server.network.topology import _json_or, _iso
    return [{
        "id": r["id"], "server": r["server"], "subject": r["subject"],
        "subject_handle": r["subject_handle"], "caps": _json_or(r["caps"], []),
        # models + may_use: [] is UNSCOPED, and may_use says what that
        # means in a sentence so a reader never has to know that convention.
        "models": _json_or(r["models"], []),
        "may_use": (describe_models(_json_or(r["models"], []))
                    if "inference" in (_json_or(r["caps"], []) or []) else None),
        "issued_at": _iso(r["issued_at"]), "expires_at": _iso(r["expires_at"]),
        "revoked_at": _iso(r["revoked_at"]), "approved_by": r["approved_by"],
        "stepup_at": _iso(r["stepup_at"]), "synced_at": _iso(r["synced_at"]),
        "sync_detail": r["sync_detail"],
        "state": ("revoked" if r["revoked_at"] else
                  "expired" if (r["expires_at"] and r["expires_at"] < _dt.datetime.now(_dt.timezone.utc)) else
                  "active" if r["synced_at"] else "undelivered"),
    } for r in rows]


async def list_denials(pool, eid: int) -> dict[tuple[int, str], dict]:
    """(token_id, subject) → {denied_at, denied_by} for every denial on record."""
    try:
        rows = await pool.fetch(
            "SELECT token_id, subject, denied_at, denied_by FROM network_bastion_pairing "
            "WHERE denied_at IS NOT NULL")
    except Exception as e:
        logger.info("list_denials unavailable: %s", e)
        return {}
    from server.network.topology import _iso
    return {(r["token_id"], r["subject"]): {"denied_at": _iso(r["denied_at"]), "denied_by": r["denied_by"]}
            for r in rows}


def decorate_bastion(b: dict, grants: list[dict], denials: dict) -> dict:
    """Attach the grants this host holds for a bastion and the denial marks to
    its reported pairings, in place. Pure — asserted by the tests."""
    server = b.get("hostname")
    mine = [g for g in grants if g.get("server") == server]
    live = {g["subject"]: g for g in mine if g.get("state") in ("active", "undelivered")}
    for p in (b.get("pairings") or []):
        d = denials.get((b.get("id"), p.get("host_name")))
        if d:
            p["denied_at"] = d["denied_at"]
        g = live.get(p.get("host_name"))
        if g:
            p["grant_id"] = g["id"]
            p["caps"] = g.get("caps") or []
            p["grant_state"] = g.get("state")
    for a in (b.get("unapproved_pairing_attempts") or []):
        d = denials.get((b.get("id"), a.get("host_name")))
        if d:
            a["denied_at"] = d["denied_at"]
    b["pairing_grants"] = mine
    return b
