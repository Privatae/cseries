#!/usr/bin/env python3
"""Pairing grants (host side) — signer vector, heartbeat narrowing, and the
approve / deny / revoke paths with a stubbed mgmt dispatch.

Runs as pytest (`pytest scripts/test_pairing_grants.py`) AND as a plain script in
the container-free gate lane (APP_ROOT=<repo> python3 scripts/test_pairing_grants.py).
No DB, no network: the pool is a recording stub, the relay is a recording stub,
the entity key is a FIXED ed25519 seed so the signature bytes are pinned.

THE VECTOR (the gateway's Go side asserts the same bytes). Key seed = bytes
0x01..0x20.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
# This Host's one zone is read when server.home_origin is first imported, so it
# is fixed here, before any server import, whatever the runner's environment says.
os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402

from server.network import pairing_grants as pg  # noqa: E402

SEED = bytes(range(1, 33))
KEY = Ed25519PrivateKey.from_private_bytes(SEED)
PUB_B64 = "ebVWLo/mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ="

# The names inside the fixed vectors below (ANCHOR, SUBJECT and the consent
# subject) are opaque strings the signature covers. They are not this Host's
# zone, and changing them changes the signed bytes the Go side asserts too.
ANCHOR = "entity-1.zone.example.test"
SERVER = "box-a"
SUBJECT = "a1b2c3d4e5f60718.zone.example.test"
HANDLE = "a1b2c3d4e5f60718"
# This Host's own zone, for every name that is not part of a pinned vector.
ZONE = ".cseries.localhost"

# Fixed vector — grant
GRANT_CANON = ('{"anchor":"entity-1.zone.example.test","caps":["observe","dcn"],'
               '"expires":"2026-10-08T12:00:00Z","issued_at":"2026-09-08T12:00:00Z",'
               '"kind":"pairing_grant","server":"box-a",'
               '"subject":"a1b2c3d4e5f60718.zone.example.test","subject_handle":"a1b2c3d4e5f60718"}')
GRANT_SIG = "QqJi7fuZ/HHn5MIkFHOSYGV09UiQvpzlsFROgJUbZkiE9VaLgHZ1kp41V0ETMTdffv1SRw2U5K/ndyP60rreBQ=="
# Fixed vector — revoke
REVOKE_CANON = ('{"anchor":"entity-1.zone.example.test","issued_at":"2026-09-08T12:30:00Z",'
                '"kind":"pairing_revoke","server":"box-a","subject":"a1b2c3d4e5f60718.zone.example.test"}')
REVOKE_SIG = "oebrR3DHKZNsbmC4GwQAWQKGEh+gAYAbgQLkka7T5fHytvxHme9MqdexzMC9xR8zncWJKc3xBfe/rVFcN56UCg=="
# Fixed vector — anchor claim signing input "anchor\n<owner>\n<anchor_host>\n<claimed_at>"
# (client-go/anchor.go anchorSigningInput in the gateway; the
# "anchor\n" prefix is domain separation from the owner claim)
ANCHOR_SIG = "mA5SLsUlkDZICilD91XZ+V0uCz9ObLgRfFDAIzIjejlMuvObMORYqxtMRUf/KMEbRC3tK8PmNyoz4ij5WH3eDg=="
# The pre-0.16 unprefixed input "<owner>\n<anchor_host>\n<claimed_at>" under the same key —
# must read verified: False now, never true.
ANCHOR_SIG_UNPREFIXED = "oJMPkR7NBQf5o82hSEIbre9kkDOW2ZGLve83Ajhlepu+OZvs4BN+R9I12vZ8rvzzHTZzXuKwMc9NoPuwIWHxDw=="
# Fixed vector — owner claim signing input "owner\n<owner>\n<claimed_at>" (owner.go ownerSigningInput)
OWNER_SIG = "L4h2MFftlgeeYZRshTQdZlhLHSsxeI1EZcMerK3X+Slk0TfM4MoNpLWsChl7PCwUiB5HIcxKrkVEX9CqTLwKBw=="
# Fixed vector — anchor consent
CONSENT_CANON = ('{"anchor":"entity-1.zone.example.test","expires":"2026-10-08T13:00:00Z",'
                 '"issued_at":"2026-09-08T13:00:00Z","kind":"anchor_consent","server":"box-a",'
                 '"subject":"b0b1b2b3b4b5b6b7.zone.example.test"}')
CONSENT_SIG = "LwARKaQZA1GAqP7enozCv6ErgoQfwmytpiuGTvn+H+5A5U4Y7TEl6jmV5FMEictmK/U5zJacHowx54kbIAkrAg=="


# ── 1. signer / canonicalization ─────────────────────────────────────────────

def test_grant_terms_canonical_and_signature_are_the_fixed_vector():
    terms = pg.grant_terms(anchor=ANCHOR, server=SERVER, subject=SUBJECT, subject_handle=HANDLE,
                           caps=["observe", "dcn"], issued_at="2026-09-08T12:00:00Z",
                           expires="2026-10-08T12:00:00Z")
    canon = pg.canonical(terms)
    assert canon.decode() == GRANT_CANON
    assert base64.b64encode(KEY.sign(canon)).decode() == GRANT_SIG
    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB_B64)).verify(base64.b64decode(GRANT_SIG), canon)


def test_revoke_terms_canonical_and_signature_are_the_fixed_vector():
    terms = pg.revoke_terms(server=SERVER, anchor=ANCHOR, subject=SUBJECT, issued_at="2026-09-08T12:30:00Z")
    canon = pg.canonical(terms)
    assert canon.decode() == REVOKE_CANON
    assert base64.b64encode(KEY.sign(canon)).decode() == REVOKE_SIG


def test_canonical_is_the_dcn_store_grant_canonical():
    # ONE canonical form for every signed record: sorted keys, no spaces, ascii.
    from server.network.nac_stepup import _canonical
    t = {"z": 1, "a": ["é", 2], "m": {"k": "v"}}
    assert pg.canonical(t) == _canonical(t) == b'{"a":["\\u00e9",2],"m":{"k":"v"},"z":1}'


def test_caps_normalize_to_a_fixed_order_and_refuse_unknowns():
    assert pg.normalize_caps(["dcn", "observe", "dcn"]) == ["observe", "dcn"]
    assert pg.normalize_caps("harness, session") == ["session", "harness"]
    # egress is the reverse egress lane; inference is the three
    # inbound forwards (ollama, llm, pii) the host dials on the box; models
    # is CHANGING what is on the box — pulling a model onto the user's
    # disk, dropping one this host put there.
    assert pg.CAPS == ("observe", "session", "dcn", "harness", "egress", "inference", "models", "services")
    assert pg.normalize_caps(["inference", "observe"]) == ["observe", "inference"]
    # models is its own family: granting inference must never imply it, or every
    # grant already given out would silently widen.
    assert pg.normalize_caps(["inference"]) == ["inference"]
    assert pg.normalize_caps(["models", "observe"]) == ["observe", "models"]
    assert pg.normalize_caps(["egress", "observe"]) == ["observe", "egress"]
    assert pg.normalize_caps(None) == []
    try:
        pg.normalize_caps(["root"])
        assert False, "unknown cap accepted"
    except ValueError as e:
        assert "root" in str(e)


def test_anchor_claim_verifies_under_the_documented_input():
    claim = {"owner": "Alex", "anchor_host": ANCHOR, "anchor_handle": "entity-1",
             "claimed_at": "2026-09-08T11:00:00Z", "public_key": PUB_B64, "signature": ANCHOR_SIG}
    assert pg.anchor_signing_input(claim) == b"anchor\nAlex\nentity-1.zone.example.test\n2026-09-08T11:00:00Z"
    assert pg.anchor_claim_verifies(claim) is True
    # an older bastion's unprefixed signature is "(unverified)", never valid
    assert pg.narrow_anchor({**claim, "signature": ANCHOR_SIG_UNPREFIXED})["verified"] is False
    assert pg.anchor_claim_verifies({**claim, "owner": "Mallory"}) is False
    assert pg.narrow_anchor(claim)["verified"] is True
    assert pg.narrow_anchor({**claim, "signature": "AAAA"})["verified"] is False


def test_owner_claim_verifies_under_either_input_but_never_the_anchor_one():
    from server.network.topology import _owner_claim_verifies
    claim = {"owner": "Alex", "claimed_at": "2026-09-08T11:00:00Z", "public_key": PUB_B64, "signature": OWNER_SIG}
    assert _owner_claim_verifies(claim) is True
    # An ANCHOR claim's signature is never an owner claim. That is the direction
    # the "owner\n" / "anchor\n" tags exist for, and it stays closed.
    assert _owner_claim_verifies({**claim, "signature": ANCHOR_SIG}) is False
    # The pre-0.16 unprefixed input still verifies: a box serves the signature
    # stored in owner.json and does not re-sign when it upgrades, so refusing it
    # would mark every claim made before 0.16 unverified the day the fleet moves.
    old = base64.b64encode(KEY.sign(b"Alex\n2026-09-08T11:00:00Z")).decode()
    assert _owner_claim_verifies({**claim, "signature": old}) is True
    # Accepted, not waved through: the legacy bytes still bind the name and time.
    assert _owner_claim_verifies({**claim, "signature": old, "owner": "Someone Else"}) is False


def test_consent_terms_canonical_and_signature_are_the_fixed_vector():
    terms = pg.consent_terms(server=SERVER, anchor=ANCHOR, next_anchor="b0b1b2b3b4b5b6b7.zone.example.test",
                             issued_at="2026-09-08T13:00:00Z", expires="2026-10-08T13:00:00Z")
    canon = pg.canonical(terms)
    assert canon.decode() == CONSENT_CANON
    assert base64.b64encode(KEY.sign(canon)).decode() == CONSENT_SIG


# ── 2. heartbeat narrowing ───────────────────────────────────────────────────

def test_narrowing_keeps_absent_distinct_from_empty_and_drops_junk():
    assert pg.narrow_mode(None) is None and pg.narrow_mode("NETWORKED") == "networked"
    assert pg.narrow_mode("root") is None
    assert pg.narrow_pairings(None) is None and pg.narrow_pairings([]) == []
    ps = pg.narrow_pairings([
        {"host_name": SUBJECT, "handle": HANDLE, "state": "PENDING", "since": "2026-09-08T12:00:00Z"},
        {"host_name": "", "state": "active"},                  # no name → dropped
        {"host_name": "x" + ZONE, "state": "owned"},  # unknown state → dropped
        "junk",
    ])
    assert ps == [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": "2026-09-08T12:00:00Z"}]
    at = pg.narrow_attempts([{"host_name": SUBJECT, "handle": HANDLE, "since": "", "extra": 1}])
    assert at == [{"host_name": SUBJECT, "handle": HANDLE, "since": ""}]
    # the box's `first_seen` IS since (anchor.go); either key lands in `since`
    assert pg.narrow_pairings([{"host_name": SUBJECT, "state": "active", "first_seen": "2026-09-01T00:00:00Z"}])[0]["since"] == "2026-09-01T00:00:00Z"


def test_unapproved_attempts_clear_when_settled_and_replace_on_explicit_empty():
    a, b = {"host_name": "a" + ZONE}, {"host_name": "b" + ZONE}
    stored = [a, b]
    # an explicit [] from the box replaces (it enumerated; nothing qualifies)
    assert pg.settle_attempts(stored, [], None) == []
    # absent key keeps what is stored…
    assert pg.settle_attempts(stored, None, [{"host_name": "a" + ZONE, "state": "pending"}]) == [a, b]
    # …minus any subject the beat now reports active or revoked (older box, no [])
    assert pg.settle_attempts(stored, None, [{"host_name": "a" + ZONE, "state": "active"}]) == [b]
    assert pg.settle_attempts(stored, None, [{"host_name": "a" + ZONE, "state": "revoked"},
                                              {"host_name": "b" + ZONE, "state": "active"}]) == []
    # a reported list is filtered the same way (defensive)
    assert pg.settle_attempts(None, [a, b], [{"host_name": "b" + ZONE, "state": "active"}]) == [a]
    # nothing stored, nothing sent → still nothing (NULL stays NULL)
    assert pg.settle_attempts(None, None, [{"host_name": "a" + ZONE, "state": "active"}]) is None
    assert pg.narrow_anchor({"owner": "Alex"}) is None   # no anchor_host → not a claim


def test_other_bastions_keep_did_not_look_apart_from_clean():
    # The distinction the whole feature rests on: None means the box could not
    # LOOK (not root, not Linux, too old), [] means it looked and found none. A
    # reader that collapses them reports a clean machine off a box that never
    # checked — the one failure second-bastion detection cannot afford.
    assert pg.narrow_other_bastions(None) is None
    assert pg.narrow_other_bastions("nonsense") is None
    assert pg.narrow_other_bastions([]) == []


def test_other_bastions_drop_unknown_kinds_and_clamp_junk():
    # kind drives both the UI pill and the host's wording, so an unknown value
    # would render as an unstyled pill with no sentence behind it.
    got = pg.narrow_other_bastions([{
        "kind": "PAIRED", "user": "eve", "uid": 1000, "pairings": 3,
        "state_dir": "/home/eve/.config/cseries-bastion",
        "last_change": "2026-09-12T05:00:00Z", "extra": "ignored",
    }])
    assert got == [{"kind": "paired", "user": "eve", "uid": 1000,
                    "state_dir": "/home/eve/.config/cseries-bastion", "binary_path": "",
                    "pairings": 3, "last_change": "2026-09-12T05:00:00Z"}], got
    # unreadable entries are dropped from a list that still has a readable one
    mixed = pg.narrow_other_bastions([
        {"kind": "root", "user": "eve", "uid": 1000},          # unknown kind
        {"user": "eve", "uid": 1001},                          # no kind
        "junk",
        {"kind": "binary", "user": "x", "uid": "abc"},         # unparseable uid
        {"kind": "state", "user": "amy", "uid": 1002},         # the good one
    ])
    assert mixed is not None and [m["user"] for m in mixed] == ["amy"], mixed
    assert pg.narrow_other_bastions([
        {"kind": "binary", "user": "x", "uid": 5, "pairings": "many"}])[0]["pairings"] == 0
    # a long path is truncated, never dropped
    long_path = "/home/eve/" + ("a" * 900)
    assert len(pg.narrow_other_bastions([
        {"kind": "state", "user": "eve", "uid": 1, "state_dir": long_path}])[0]["state_dir"]) == 512
    # the list is capped
    many = [{"kind": "binary", "user": f"u{i}", "uid": i} for i in range(200)]
    assert len(pg.narrow_other_bastions(many)) == 64


def test_detect_summary_requires_a_baseline_state_it_knows():
    # `baseline` is the field that separates "compared, found nothing" from
    # "nobody ever said what normal was here", and all-zero counts are
    # meaningless without it. A payload that cannot say which one it is must not
    # be stored as either — otherwise a box with no accounting at all lands in
    # the column looking exactly like a clean machine.
    assert pg.narrow_detect(None) is None
    assert pg.narrow_detect("nonsense") is None
    assert pg.narrow_detect({}) is None                            # no baseline
    assert pg.narrow_detect({"critical": 5}) is None               # counts, no baseline
    assert pg.narrow_detect({"baseline": "clean"}) is None          # not a state we know
    # every state the bastion can report (deltarun.go:37-43) survives
    for state in ("ok", "none", "edited", "not-administered", "taken-under-older-rules"):
        got = pg.narrow_detect({"baseline": state})
        assert got is not None and got["baseline"] == state, (state, got)
    # and it is case-folded, like narrow_mode
    assert pg.narrow_detect({"baseline": "OK"})["baseline"] == "ok"


def test_detect_summary_whitelists_fields_and_clamps_counts():
    got = pg.narrow_detect({
        "baseline": "ok", "baseline_taken_at": "2026-09-15T00:00:00Z",
        "coverage": "all-processes", "at": "2026-09-15T14:00:00Z",
        "critical": 1, "high": 2, "warn": 3, "notice": 4,
        "new_since": "2026-09-14T00:00:00Z", "short_lived": 7, "filtered": 9,
        "note": "one note", "secret": "dropped",
    })
    assert got == {
        "baseline": "ok", "baseline_taken_at": "2026-09-15T00:00:00Z",
        "coverage": "all-processes", "at": "2026-09-15T14:00:00Z",
        "critical": 1, "high": 2, "warn": 3, "notice": 4,
        "new_since": "2026-09-14T00:00:00Z", "short_lived": 7, "filtered": 9,
        "note": "one note",
        # bastion 0.31.1: the failed-walk key, empty when the walk happened
        "walk_failed": "",
        # the classes a pass did not compare; none here
        "skipped": [],
    }, got
    # a junk count does not take the whole beat down: a real critical stays
    # visible even when some other field is malformed.
    salvaged = pg.narrow_detect({"baseline": "ok", "critical": 2, "warn": "lots"})
    assert salvaged["critical"] == 2 and salvaged["warn"] == 0, salvaged
    # negatives and absurd values are clamped rather than stored
    assert pg.narrow_detect({"baseline": "ok", "high": -5})["high"] == 0
    assert pg.narrow_detect({"baseline": "ok", "high": 10**9})["high"] == 1_000_000
    # a long note is truncated, never dropped
    assert len(pg.narrow_detect({"baseline": "ok", "note": "x" * 2000})["note"]) == 500
    assert len(pg.narrow_detect({"baseline": "ok", "walk_failed": "x" * 900})["walk_failed"]) == 200


def test_detect_keeps_every_coverage_line_the_bastion_can_build_whole():
    """client-go baselineCoverage builds four halves. Every combination must
    reach the Host intact; the old 64-character cap cut the identities half
    off every four-half line."""
    import itertools
    for p, s, lst, ids in itertools.product(("all", "recognised-only"), ("all", "recognised-only"),
                                            ("all", "unsupported"), ("login-shells", "unsupported")):
        line = f"processes={p},schedulers={s},listeners={lst},identities={ids}"
        got = pg.narrow_detect({"baseline": "ok", "coverage": line})["coverage"]
        assert got == line, (line, got)
    # Still bounded: a hostile box cannot store an unbounded string.
    assert len(pg.narrow_detect({"baseline": "ok", "coverage": "x" * 5000})["coverage"]) == 160


def test_detect_skipped_keeps_known_classes_only_in_a_fixed_order():
    got = pg.narrow_detect({"baseline": "ok",
                            "skipped": ["identity", "rm -rf", "listener", 7, "identity"]})
    assert got["skipped"] == ["listener", "identity"], got["skipped"]
    assert pg.narrow_detect({"baseline": "ok", "skipped": "listener"})["skipped"] == []
    assert pg.narrow_detect({"baseline": "ok"})["skipped"] == []


def test_detect_absent_stays_distinct_from_a_clean_pass():
    # The same quiet-versus-blind rule other_bastions follows. None is "this box
    # never reported a pass"; a summary with zero counts is a box that compared
    # and found nothing. A surface that renders the first as the second reports a
    # clean machine off a box that never looked.
    assert pg.narrow_detect(None) is None
    clean = pg.narrow_detect({"baseline": "ok", "critical": 0, "high": 0,
                              "warn": 0, "notice": 0})
    assert clean is not None and clean["baseline"] == "ok"
    assert all(clean[k] == 0 for k in ("critical", "high", "warn", "notice"))
    # and a box with NO accounting is not a clean box, even though its counts
    # are also all zero — `baseline` is the only thing telling them apart.
    blind = pg.narrow_detect({"baseline": "none"})
    assert blind["baseline"] == "none" and blind["critical"] == 0
    assert clean != blind


def test_other_bastions_all_unreadable_is_unknown_not_clean():
    # A list where EVERY entry was dropped is a box saying something
    # this pod cannot read — "did not look", not "clean". Returning [] there
    # would store an all-clear AND mark every existing finding gone, so a bastion
    # NEWER than the pod (reporting a kind added later) would silently retract
    # real findings the moment it upgraded.
    assert pg.narrow_other_bastions([{"kind": "some-future-kind", "user": "e", "uid": 1}]) is None
    assert pg.narrow_other_bastions([{"user": "eve", "uid": 1000}]) is None       # no kind
    assert pg.narrow_other_bastions(["junk", 7]) is None
    # uid is the DEDUPE KEY (network_bastion_other is keyed on it), so
    # a finding whose uid will not parse cannot be tracked — two of them would
    # collide on one row and alternate, re-raising for ever.
    assert pg.narrow_other_bastions([{"kind": "binary", "user": "x", "uid": "abc"}]) is None
    # and a genuinely empty list is still "looked, and the machine is clean"
    assert pg.narrow_other_bastions([]) == []


def test_peer_handle_must_be_a_handle_not_an_address():
    # The narrowing half. peer_handle is refreshed on EVERY beat while
    # peer_entity_id resolves once, so a machine announcing a stable host_name can
    # swap the handle underneath a live peering. Unrestricted it could be an email
    # address — and `contacts` is unique on handle alone, so anything writing a
    # contact keyed on it would be addressing the user's HUMAN contact of that
    # name. topology._narrow_handle:40-48 restricts it to one DNS label.
    from server.network.topology import _narrow_handle
    assert _narrow_handle("8abb770bbfde71d0") == "8abb770bbfde71d0"
    assert _narrow_handle("host-01") == "host-01"
    assert _narrow_handle("alice@example.com") == ""
    assert _narrow_handle("a.b.c") == ""
    assert _narrow_handle("has space") == ""
    assert _narrow_handle("-leadinghyphen") == ""
    assert _narrow_handle("x" * 200) == ""
    assert _narrow_handle(None) == ""


def test_local_mode_box_is_asked_to_act_even_with_no_anchor():
    # is_anchor is absent whenever the box has no anchor, which is
    # EVERY local-mode box. Without this the single host of a local machine — its
    # owner — would be told "the host that anchored this decides; nothing changes
    # for you" about their own computer, and would get no reminder at all.
    assert pg._owns_machine(is_anchor=True, mode="networked") is True
    assert pg._owns_machine(is_anchor=False, mode="local") is True
    assert pg._owns_machine(is_anchor=False, mode="LOCAL") is True
    assert pg._owns_machine(is_anchor=False, mode="networked") is False
    # mode absent (a bastion too old to say) is NOT assumed local: the narrow
    # reading is the safe one, and record_and_notify already treats a member host
    # as told-once rather than asked.
    assert pg._owns_machine(is_anchor=False, mode=None) is False


def test_other_bastion_rank_orders_binary_below_paired():
    # The ladder decides what counts as news: a finding that climbs it is
    # re-raised, so "someone has a binary" never masks "someone is serving a
    # host off your machine".
    assert pg._OTHER_RANK["binary"] < pg._OTHER_RANK["state"] < pg._OTHER_RANK["paired"]
    assert set(pg._OTHER_RANK) == set(pg.OTHER_BASTION_KINDS)


# ── 3. approve / deny / revoke with a stubbed relay ──────────────────────────

class Pool:
    """Enough asyncpg surface for the service: records every statement, answers
    the few SELECTs the paths need, and tracks the grant rows it was handed."""
    def __init__(self, *, anchor_host=ANCHOR, pairings=None, live_grant=False,
                 attempts=None, stored_subject=False):
        self.sql: list[tuple[str, tuple]] = []
        self.anchor_host = anchor_host
        self.pairings = pairings if pairings is not None else [
            {"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": ""}]
        self.live_grant = live_grant
        # The heartbeat row's unapproved_pairing_attempts (None = column NULL).
        self.attempts = attempts
        # Whether the durable network_bastion_pairing row knows SUBJECT: it keeps
        # every subject a box ever reported, including ones it no longer does.
        self.stored_subject = stored_subject
        self.next_id = 41

    async def execute(self, q, *a):
        self.sql.append((q, a))

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "FROM network_bastion_heartbeat" in q:
            return {"token_id": 7, "hostname": SERVER,
                    "anchor": json.dumps({"anchor_host": self.anchor_host, "owner": "Alex"}) if self.anchor_host else None,
                    "pairings": json.dumps(self.pairings),
                    "unapproved_pairing_attempts":
                        json.dumps(self.attempts) if self.attempts is not None else None}
        if "FROM nac_pairing_grants" in q:
            return {"id": 40} if self.live_grant else None
        if "FROM network_bastion_pairing" in q:
            return {"subject_handle": HANDLE} if self.stored_subject else None
        return None

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "INSERT INTO nac_pairing_grants" in q:
            self.next_id += 1
            return self.next_id
        return None

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        return []

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Cx()


class Relay:
    def __init__(self, exit_code=0, offline=False):
        self.calls: list[tuple[str, str, list]] = []
        self.exit_code, self.offline = exit_code, offline

    async def __call__(self, pool, eid, server, op, args):
        self.calls.append((server, op, list(args)))
        if self.offline:
            return {"exit": None, "stdout": "", "stderr": "no worker", "_offline": True}
        return {"exit": self.exit_code, "stdout": "", "stderr": "" if self.exit_code == 0 else "bad key"}


async def _fixed_sign(pool, eid, terms):
    canon = pg.canonical(terms)
    return base64.b64encode(canon).decode(), base64.b64encode(KEY.sign(canon)).decode()


def _wire(monkey, relay, *, stepup_ok=True, own=ANCHOR):
    async def _stepup(pool, code):
        return stepup_ok and code == "123456"

    # _own_host_names is PLURAL: every name this host answers to, canonical
    # first. Tests pass a single string (or None) for readability; normalise.
    own_names = () if own is None else (own if isinstance(own, tuple) else (own,))

    async def _own(eid):
        return own_names

    async def _audit(pool, eid, server, op, *, exit_code, decision):
        pass

    async def _vk(pool, eid):
        return PUB_B64
    monkey.setattr(pg, "_verify_pubkey_b64", _vk)
    monkey.setattr(pg, "_sign", _fixed_sign)
    monkey.setattr(pg, "_verify_stepup", _stepup)
    monkey.setattr(pg, "_own_host_names", _own)
    monkey.setattr(pg, "_dispatch", relay)
    import server.network.nac_mgmt as nm
    monkey.setattr(nm, "audit_mgmt", _audit)


def _run(coro):
    return asyncio.run(coro)


def test_approve_signs_stores_and_pushes_grant_sync(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay)
    pool = Pool()
    res = _run(pg.approve_pairing(pool, 9003, SERVER, SUBJECT, ["dcn", "observe"], stepup_code="123456"))
    assert res["approved"] and res["delivered"] and res["state"] == "active"
    assert res["caps"] == ["observe", "dcn"] and res["subject_handle"] == HANDLE
    # one dispatch: [terms_b64, signature_b64, verify_pubkey_b64] — the third is the
    # TOFU pin the box takes on its first sync (the box's pairing_ops.go)
    assert len(relay.calls) == 1
    server, op, args = relay.calls[0]
    assert (server, op) == (SERVER, pg.OP_GRANT_SYNC) and len(args) == 3 and args[2] == PUB_B64
    terms = json.loads(base64.b64decode(args[0]))
    assert terms["kind"] == "pairing_grant" and terms["anchor"] == ANCHOR and terms["caps"] == ["observe", "dcn"]
    assert terms["subject"] == SUBJECT and terms["server"] == SERVER and terms["expires"]
    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB_B64)).verify(
        base64.b64decode(args[1]), base64.b64decode(args[0]))
    # stored: prior live rows superseded, new row inserted, denial cleared, sync recorded
    joined = "\n".join(q for q, _ in pool.sql)
    assert "UPDATE nac_pairing_grants SET revoked_at=now() WHERE server=$1 AND subject=$2" in joined
    assert "INSERT INTO nac_pairing_grants" in joined
    assert "SET denied_at=NULL" in joined
    assert "SET synced_at=now()" in joined


def test_approve_refuses_without_a_valid_stepup_and_sends_nothing(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay)
    try:
        _run(pg.approve_pairing(Pool(), 9003, SERVER, SUBJECT, ["observe"], stepup_code="000000"))
        assert False, "approved without step-up"
    except pg.PairingError as e:
        assert e.status == 403
    assert relay.calls == []


def test_approve_refuses_on_a_machine_anchored_elsewhere_or_unanchored(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay, own="other" + ZONE)
    try:
        _run(pg.approve_pairing(Pool(), 9003, SERVER, SUBJECT, [], stepup_code="123456"))
        assert False
    except pg.PairingError as e:
        assert e.status == 403 and "anchored to" in e.detail
    _wire(monkeypatch, relay)
    try:
        _run(pg.approve_pairing(Pool(anchor_host=None), 9003, SERVER, SUBJECT, [], stepup_code="123456"))
        assert False
    except pg.PairingError as e:
        assert e.status == 409 and "not anchored" in e.detail
    # own handle unresolvable → the anchor check cannot be made → 409, never proceed
    _wire(monkeypatch, relay, own=None)
    try:
        _run(pg.approve_pairing(Pool(), 9003, SERVER, SUBJECT, [], stepup_code="123456"))
        assert False
    except pg.PairingError as e:
        assert e.status == 409 and "handle" in e.detail
    assert relay.calls == []


def test_ttl_days_zero_means_no_expiry_and_negative_is_refused(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay)
    res = _run(pg.approve_pairing(Pool(), 9003, SERVER, SUBJECT, ["observe"], stepup_code="123456", ttl_days=0))
    terms = json.loads(base64.b64decode(relay.calls[0][2][0]))
    assert res["expires"] == "" and terms["expires"] == ""
    try:
        _run(pg.approve_pairing(Pool(), 9003, SERVER, SUBJECT, ["observe"], stepup_code="123456", ttl_days=-1))
        assert False
    except pg.PairingError as e:
        assert e.status == 400
    assert len(relay.calls) == 1


def test_approve_refuses_a_subject_the_box_never_reported(monkeypatch):
    _wire(monkeypatch, Relay())
    try:
        _run(pg.approve_pairing(Pool(pairings=[]), 9003, SERVER, SUBJECT, [], stepup_code="123456"))
        assert False
    except pg.PairingError as e:
        assert e.status == 404


def test_approve_refuses_a_subject_only_an_old_stored_row_remembers(monkeypatch):
    # The box no longer reports the subject — not as a pairing, not as an
    # attempt — but the durable row still remembers it from an earlier beat.
    # That is history, not a pairing: approving it would sign a live grant for
    # a host the machine has stopped holding.
    relay = Relay()
    _wire(monkeypatch, relay)
    pool = Pool(pairings=[{"host_name": "other.zone.example.test", "handle": "other",
                           "state": "active", "since": ""}],
                attempts=[], stored_subject=True)
    try:
        _run(pg.approve_pairing(pool, 3, SERVER, SUBJECT, ["observe"], stepup_code="123456"))
        assert False, "approved a subject the box does not report now"
    except pg.PairingError as e:
        assert e.status == 404, e.detail
    assert relay.calls == [], "nothing may reach the box for a stale subject"
    joined = "\n".join(q for q, _ in pool.sql)
    assert "INSERT INTO nac_pairing_grants" not in joined, "a grant row was written"


def test_approve_accepts_a_subject_the_box_lists_only_as_an_unapproved_attempt(monkeypatch):
    # The other half of "reported now": the latest beat names the subject only
    # under unapproved attempts, which is still the box reporting it.
    relay = Relay()
    _wire(monkeypatch, relay)
    pool = Pool(pairings=[], attempts=[{"host_name": SUBJECT, "handle": HANDLE, "since": ""}])
    res = _run(pg.approve_pairing(pool, 3, SERVER, SUBJECT, ["observe"], stepup_code="123456"))
    assert res["approved"] and res["subject_handle"] == HANDLE, res
    assert len(relay.calls) == 1


def test_approve_on_an_offline_box_keeps_the_grant_undelivered(monkeypatch):
    _wire(monkeypatch, Relay(offline=True))
    pool = Pool()
    res = _run(pg.approve_pairing(pool, 9003, SERVER, SUBJECT, ["session"], stepup_code="123456"))
    assert res["approved"] and res["delivered"] is False and res["state"] == "offline"
    assert "next check-in" in res["note"]
    joined = "\n".join(q for q, _ in pool.sql)
    assert "SET sync_detail=$2" in joined and "SET synced_at=now()" not in joined


def test_deny_records_here_and_sends_nothing(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay)
    pool = Pool()
    # via="ui" as the Network app's route passes it: only that surface is recorded as
    # the person (nac_commit.approver_for); the host tool's refusals record "host".
    res = _run(pg.deny_pairing(pool, 9003, SERVER, SUBJECT, denied_by="alex", via="ui"))
    assert res["denied"] and relay.calls == []
    q, a = next((q, a) for q, a in pool.sql if "INSERT INTO network_bastion_pairing" in q)
    assert "denied_at" in q and a[:2] == (7, SUBJECT) and a[2] == "alex"


def test_revoke_signs_revoke_terms_pushes_drop_and_needs_no_stepup(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay, stepup_ok=False)
    pool = Pool(live_grant=True)
    res = _run(pg.revoke_pairing(pool, 9003, SERVER, SUBJECT))
    assert res["revoked"] and res["delivered"] and res["had_grant"]
    server, op, args = relay.calls[0]
    assert (server, op) == (SERVER, pg.OP_GRANT_DROP)
    assert args[0] == SUBJECT and len(args) == 3
    terms = json.loads(base64.b64decode(args[1]))
    assert terms == {"anchor": ANCHOR, "issued_at": terms["issued_at"], "kind": "pairing_revoke",
                     "server": SERVER, "subject": SUBJECT}
    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB_B64)).verify(
        base64.b64decode(args[2]), base64.b64decode(args[1]))
    joined = "\n".join(q for q, _ in pool.sql)
    assert "SET revoked_at=now(), revoke_terms_canonical=$2" in joined


def test_revoke_marks_revoked_here_even_when_the_box_is_offline(monkeypatch):
    _wire(monkeypatch, Relay(offline=True))
    pool = Pool(live_grant=True)
    res = _run(pg.revoke_pairing(pool, 9003, SERVER, SUBJECT))
    assert res["revoked"] and res["delivered"] is False and "quarantine_bastion" in res["note"]
    assert any("SET revoked_at=now(), revoke_terms_canonical" in q for q, _ in pool.sql)


def test_consent_signs_and_pushes_anchor_consent_with_stepup(monkeypatch):
    relay = Relay()
    _wire(monkeypatch, relay)
    try:
        _run(pg.consent_anchor_rotation(Pool(), 9003, SERVER, "b" + ZONE, stepup_code="000000"))
        assert False, "consented without step-up"
    except pg.PairingError as e:
        assert e.status == 403 and relay.calls == []
    res = _run(pg.consent_anchor_rotation(Pool(), 9003, SERVER, "b" + ZONE, stepup_code="123456", days=90))
    assert res["consented"] and res["delivered"]
    server, op, args = relay.calls[0]
    assert (server, op) == (SERVER, pg.OP_ANCHOR_CONSENT) and len(args) == 2
    terms = json.loads(base64.b64decode(args[0]))
    assert terms["kind"] == "anchor_consent" and terms["subject"] == "b" + ZONE and terms["anchor"] == ANCHOR
    issued = dt.datetime.fromisoformat(terms["issued_at"].replace("Z", "+00:00"))
    expires = dt.datetime.fromisoformat(terms["expires"].replace("Z", "+00:00"))
    assert (expires - issued).days <= pg.CONSENT_MAX_DAYS   # 90 was asked; the box refuses > 30


# ── 4. heartbeat ingest: the record + notify path ────────────────────────────

class NotifyPool(Pool):
    """Rows for the pending scan, so the notify decisions can be asserted."""
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        if "FROM network_bastion_pairing" in q:
            return self.rows
        return []


def test_ingest_asks_once_then_raises_unapproved_daily(monkeypatch):
    sent: list = []

    async def _inbox(subject, body, payload):
        sent.append(payload)
        return True

    async def _msg(content, action):
        sent.append({"host_message": action, "content": content})
    monkeypatch.setattr(pg, "_inbox", _inbox)
    monkeypatch.setattr(pg, "_host_message", _msg)
    monkeypatch.setattr(pg, "_dispatch", Relay())
    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(days=3)).isoformat()   # already OLD on the box at first sight
    pairings = [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": old},
                {"host_name": "b" + ZONE, "handle": "b", "state": "active", "since": ""}]

    def row(**kw):
        base = {"subject": SUBJECT, "subject_handle": HANDLE, "since": old,
                "pending_notified_at": None, "unapproved_raised_at": None, "denied_at": None}
        return [{**base, **kw}]

    # first sight of an already-old pending pairing → ONE ask, no reminder with it,
    # and the daily clock starts from this beat (note 7)
    pool = NotifyPool(row())
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=True))
    assert out["pending_raised"] == 1 and out["unapproved_raised"] == 0
    assert [p.get("event") for p in sent if "event" in p] == ["pairing_pending"]
    assert any("SET pending_notified_at = now(), unapproved_raised_at = now()" in q for q, _ in pool.sql)
    # the ask names the approve op on the anchor; inbox row lands BEFORE the host message
    assert sent[0].get("event") == "pairing_pending" and sent[1].get("host_message") == "pairing_pending"
    assert "manage_pairing op='approve'" in sent[1]["content"]
    # asked 3h ago, clock started then → still silent (once per DAY, not per 10 min)
    sent.clear()
    pool = NotifyPool(row(pending_notified_at=now - dt.timedelta(hours=3),
                          unapproved_raised_at=now - dt.timedelta(hours=3)))
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=True))
    assert out["unapproved_raised"] == 0 and sent == []
    # asked 25h ago, raised then → unapproved raised once
    pool = NotifyPool(row(pending_notified_at=now - dt.timedelta(hours=25),
                          unapproved_raised_at=now - dt.timedelta(hours=25)))
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=True))
    assert out["unapproved_raised"] == 1
    assert [p.get("event") for p in sent if "event" in p] == ["pairing_unapproved"]
    # raised an hour ago → silent
    sent.clear()
    pool = NotifyPool(row(pending_notified_at=now - dt.timedelta(days=2),
                          unapproved_raised_at=now - dt.timedelta(hours=1)))
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=True))
    assert out["unapproved_raised"] == 0 and sent == []
    # denied → silent forever
    pool = NotifyPool(row(denied_at=now))
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=True))
    assert out["pending_raised"] == 0 and sent == []
    # an older bastion (no fields) is a no-op, not a retraction
    pool = NotifyPool([])
    assert _run(pg.record_and_notify(pool, 9003, 7, SERVER, None, None)) == {"pending_raised": 0, "unapproved_raised": 0, "resynced": 0}
    assert pool.sql == []


def test_ingest_on_a_member_host_informs_once_and_never_asks_to_approve(monkeypatch):
    sent: list = []

    async def _inbox(subject, body, payload):
        sent.append({"inbox_subject": subject, **payload})
        return True

    async def _msg(content, action):
        sent.append({"host_message": action, "content": content})
    monkeypatch.setattr(pg, "_inbox", _inbox)
    monkeypatch.setattr(pg, "_host_message", _msg)
    relay = Relay()
    monkeypatch.setattr(pg, "_dispatch", relay)
    now = dt.datetime.now(dt.timezone.utc)
    pairings = [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": ""}]
    pool = NotifyPool([{"subject": SUBJECT, "subject_handle": HANDLE, "since": "",
                        "pending_notified_at": None, "unapproved_raised_at": None, "denied_at": None}])
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=False))
    assert out["pending_raised"] == 1
    assert sent[0]["inbox_subject"].startswith("A new host is asking to join") and sent[0]["is_anchor"] is False
    assert "manage_pairing op='approve'" not in sent[1]["content"] and "admin decides" in sent[1]["content"]
    # no daily reminder for a member, and no grant retry (a member holds none)
    sent.clear()
    pool = NotifyPool([{"subject": SUBJECT, "subject_handle": HANDLE, "since": "",
                        "pending_notified_at": now - dt.timedelta(days=5),
                        "unapproved_raised_at": now - dt.timedelta(days=5), "denied_at": None}])
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER, pairings, None, is_anchor=False))
    assert out == {"pending_raised": 0, "unapproved_raised": 0, "resynced": 0} and sent == []
    assert relay.calls == []


def test_ingest_failed_inbox_write_leaves_no_host_message_and_no_mark(monkeypatch):
    sent: list = []

    async def _inbox(subject, body, payload):
        return False

    async def _msg(content, action):
        sent.append(action)
    monkeypatch.setattr(pg, "_inbox", _inbox)
    monkeypatch.setattr(pg, "_host_message", _msg)
    monkeypatch.setattr(pg, "_dispatch", Relay())
    pool = NotifyPool([{"subject": SUBJECT, "subject_handle": HANDLE, "since": "",
                        "pending_notified_at": None, "unapproved_raised_at": None, "denied_at": None}])
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER,
                                    [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": ""}],
                                    None, is_anchor=True))
    assert out["pending_raised"] == 0 and sent == []
    assert not any("SET pending_notified_at" in q for q, _ in pool.sql)


def test_ingest_retries_an_undelivered_grant_while_the_box_reports_pending(monkeypatch):
    relay = Relay()
    monkeypatch.setattr(pg, "_dispatch", relay)

    async def _vk(pool, eid):
        return PUB_B64
    monkeypatch.setattr(pg, "_verify_pubkey_b64", _vk)

    async def _inbox(subject, body, payload):
        return True

    async def _msg(content, action):
        pass
    monkeypatch.setattr(pg, "_inbox", _inbox)
    monkeypatch.setattr(pg, "_host_message", _msg)

    class P(NotifyPool):
        async def fetchrow(self, q, *a):
            self.sql.append((q, a))
            if "FROM nac_pairing_grants" in q and "synced_at IS NULL" in q:
                # the retry query must exclude a grant the box REFUSED — the
                # same bytes would be refused every beat (note 6)
                assert "NOT LIKE 'refused:%'" in q
                return {"id": 9, "terms_canonical": "dGVybXM=", "signature": "c2ln"}
            return None
    now = dt.datetime.now(dt.timezone.utc)
    pool = P([{"subject": SUBJECT, "subject_handle": HANDLE, "since": "",
               "pending_notified_at": now, "unapproved_raised_at": now, "denied_at": None}])
    out = _run(pg.record_and_notify(pool, 9003, 7, SERVER,
                                    [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": ""}], None,
                                    is_anchor=True))
    assert out["resynced"] == 1
    assert relay.calls == [(SERVER, pg.OP_GRANT_SYNC, ["dGVybXM=", "c2ln", PUB_B64])]
    assert any("SET synced_at=now()" in q for q, _ in pool.sql)


def test_decorate_bastion_marks_denials_and_live_grants():
    b = {"id": 7, "hostname": SERVER,
         "pairings": [{"host_name": SUBJECT, "handle": HANDLE, "state": "active"},
                      {"host_name": "b" + ZONE, "handle": "b", "state": "pending"}],
         "unapproved_pairing_attempts": [{"host_name": "b" + ZONE, "handle": "b"}]}
    grants = [{"id": 1, "server": SERVER, "subject": SUBJECT, "caps": ["observe"], "state": "active"},
              {"id": 2, "server": "elsewhere", "subject": SUBJECT, "caps": ["dcn"], "state": "active"},
              {"id": 3, "server": SERVER, "subject": SUBJECT, "caps": ["dcn"], "state": "revoked"}]
    pg.decorate_bastion(b, grants, {(7, "b" + ZONE): {"denied_at": "t", "denied_by": "u"}})
    assert b["pairings"][0]["grant_id"] == 1 and b["pairings"][0]["caps"] == ["observe"]
    assert b["pairings"][1]["denied_at"] == "t" and "grant_id" not in b["pairings"][1]
    assert b["unapproved_pairing_attempts"][0]["denied_at"] == "t"
    assert [g["id"] for g in b["pairing_grants"]] == [1, 3]


# ── script entry (the gate lane has no pytest) ──────────────────────────────

class _Monkey:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def test_host_names_is_the_one_home_zone_and_frame_ancestors_admit_it_on_its_scheme_and_port():
    """A C-Series Host has ONE zone, the one CSERIES_URL names (server/home_origin.py),
    and server/host_names.py is where it is written down. If a caller hardcodes
    a suffix, or a second zone creeps back into the tuple, this is the gate that
    says so."""
    from server import host_names as hn
    assert hn.HOST_SUFFIXES == (ZONE,), hn.HOST_SUFFIXES
    assert hn.host_names("host-a") == ("host-a" + ZONE,)
    assert hn.host_name("host-a") == "host-a" + ZONE == hn.host_names("host-a")[0]
    assert hn.host_names("host-a") == tuple("host-a" + s for s in hn.HOST_SUFFIXES)
    # frame-ancestors has to admit the zone, on the desktop's scheme AND port: a
    # source with no port matches only the scheme's default one, so a bare
    # "http://*.cseries.localhost" would blank every origin-isolated app on :8800.
    assert hn.frame_ancestors() == "http://*.cseries.localhost:8800", hn.frame_ancestors()

    # And the defect that motivated the module: a frame-ancestors zone written
    # as a literal instead of derived. Found in TWO files at once
    # (server/main.py, server/applications/proxy.py); on an address the literal
    # did not name, every origin-isolated app renders as a blank frame with no
    # server-side error at all. Zero-noise ratchet: derived is the only form.
    # CALL the real producers, do not just read them as text. The text ratchet
    # below cannot catch an undefined name, and that is exactly what once
    # happened: proxy.py used host_names.frame_ancestors() without importing
    # host_names, so every origin-isolated app's HTML response raised NameError
    # while ast.parse and all 28 tests stayed green.
    from server.applications import proxy as _proxy
    _csp = _proxy._app_csp(["self"], None)
    assert "frame-ancestors http://*.cseries.localhost:8800" in _csp, _csp

    import re as _re
    bad = []
    for base, _dirs, files in os.walk(os.path.join(_ROOT, "server")):
        for f in files:
            if not f.endswith(".py"):
                continue
            fp = os.path.join(base, f)
            for i, line in enumerate(open(fp, encoding="utf-8", errors="replace"), 1):
                if _re.search(r"frame-ancestors\s+https?://", line):
                    bad.append(f"{os.path.relpath(fp, _ROOT)}:{i}")
    assert not bad, ("frame-ancestors names a zone literally; use "
                     f"server.host_names.frame_ancestors(): {bad}")


def test_an_anchor_in_our_own_zone_but_not_ours_is_refused(monkeypatch):
    """Sharing a zone with this Host is not being this Host: a box anchored to
    another Host's name in the same zone is refused, and nothing is sent."""
    relay = Relay()
    _wire(monkeypatch, relay, own=("entity-1" + ZONE,))
    try:
        _run(pg.approve_pairing(Pool(anchor_host="attacker" + ZONE), 9003, SERVER,
                                SUBJECT, ["observe"], stepup_code="123456"))
        assert False, "approved on a machine anchored to someone else"
    except pg.PairingError as e:
        assert e.status == 403 and "anchored to" in e.detail
    assert relay.calls == []


def main() -> int:
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        mk = _Monkey()
        try:
            fn(mk) if "monkeypatch" in inspect.signature(fn).parameters else fn()
            print("OK   " + name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name} -- {type(e).__name__}: {e}")
        finally:
            mk.undo()
    print()
    print(f"FAILURES: {failed} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
