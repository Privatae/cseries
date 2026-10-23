#!/usr/bin/env python3
"""The alert plane's summary, on the host side (v150).

Runs as pytest (`pytest scripts/test_alerts_summary.py`) AND as a plain script
in the container-free gate lane (APP_ROOT=<repo> python3 scripts/…). No DB, no
network: the narrower and the reading are pure, and the storage half drives the
real upsert_heartbeat against a recording pool.

WHAT THIS FILE IS DEFENDING. A bastion reads the alert source on its own box and
folds a summary onto every heartbeat under the key `alerts`. The host's heartbeat
writer takes a FIXED column list, so anything not named in it is dropped — the
exact defect v146 fixed for the detection plane, one plane over. Storing it is
the easy half.

The half that needs tests is the READING, and here the blind case is not an edge
case, it is the DEFAULT: every bastion in the fleet omits the key until an admin
places an endpoints file on that machine. So the failure this file exists to
prevent is a host looking at twenty machines nobody has ever configured an alert
source on and reporting twenty quiet machines.

    unconfigured   no summary ever arrived (no alert source on that box, or a
                   bastion too old to have the ops), or one arrived that this
                   host cannot read. NOBODY IS WATCHING.
    quiet          the box read its alert source and nothing is firing  <-- the
                   only all-clear
    firing         something is firing, none of it critical
    critical       at least one critical is firing
    stale          a summary arrived and it is OLD relative to the beat that
                   carried it: the box is heartbeating and its alert source has
                   stopped answering

`stale` closes the longest-lived hole on this plane.
By contract the box keeps its last good summary and folds it onto every beat, so
an Alertmanager that dies leaves a machine reporting `quiet` with a FROZEN `at`,
for ever, while it heartbeats perfectly. Nothing here compared `at` to anything,
so that read as the only all-clear the vocabulary has — the "zero findings,
nobody looked" failure arriving through the one door left open.

Two properties of that comparison are pinned below because both are easy to get
wrong: it is measured against the BEAT (`last_seen_at`), never this pod's clock,
so skew on a customer's machine cannot manufacture staleness; and the boundary is
tested on both sides, because a threshold nobody tested is a threshold nobody
chose.

The shapes below are chosen from what actually went wrong when this reading
was first written: it was rewritten three times and every defect was one
sentence: a state that produces zero findings being worded as a clean machine.
One was an unrecognised state falling through an if/elif chain into the
counts branch and returning `clean` WHILE THE SUITE STAYED GREEN. So the tests
here are written against the SHAPE that allowed it — an enumerated vocabulary
rather than literals in the assertions, and a fall-through that must land on
`unconfigured` and never on calm.

The matching half — the box reading Alertmanager and building the summary —
is the gateway's own suite.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from mastercode.runtime import tools  # noqa: E402
from mastercode.runtime.tools import (  # noqa: E402
    _alerts_counts, _bastion_alerts_state, _bastion_summary)
from server.network import pairing_grants as pg  # noqa: E402
from server.network import topology  # noqa: E402

# The contract the bastion builder is implementing, written out once so every
# test below draws its field names from the same place. Anything not in here is
# not part of the beat.
CONTRACT_KEYS = ("firing", "critical", "warning", "since", "at", "fingerprints")

OK = {"firing": 3, "critical": 1, "warning": 2,
      "since": "2026-09-16T08:00:00Z", "at": "2026-09-16T12:00:00Z",
      "fingerprints": ["abc123", "def456"]}

# The beat that carried OK, close behind its read. Staleness is measured between
# these two and never against this pod's clock, so the fixtures are fixed
# instants and the suite cannot go red in six months.
SEEN = "2026-09-16T12:00:30+00:00"


def _with(**kw):
    """A COMPLETE valid payload with one field replaced.

    Every malformed-input case is built through this. Presence of
    the three counts and `at` is required, so spoiling a partial payload would
    assert nothing: the None would come from the missing field rather than from
    the bad one the case is named after.
    """
    return {**OK, **kw}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1. the narrower: what may be stored ─────────────────────────────────────

def test_the_contract_is_stored_field_for_field():
    out = pg.narrow_alerts(OK)
    assert out is not None
    assert set(out) == set(CONTRACT_KEYS), (
        "narrow_alerts stores a field set that is not the contract; a key the "
        "box sends and this side drops is the v146 defect all over again")
    assert out["firing"] == 3 and out["critical"] == 1 and out["warning"] == 2
    assert out["since"] == "2026-09-16T08:00:00Z"
    assert out["at"] == "2026-09-16T12:00:00Z"
    assert out["fingerprints"] == ["abc123", "def456"]


def test_an_absent_key_is_none_and_that_is_the_whole_point():
    # Key ABSENT = this bastion has no alert source configured. It is NOT zero
    # alerts, and None is the only value that cannot be mistaken for one.
    assert pg.narrow_alerts(None) is None
    assert pg.narrow_alerts("nonsense") is None
    assert pg.narrow_alerts([1, 2]) is None
    assert pg.narrow_alerts(7) is None


def test_an_empty_or_unrecognised_payload_is_never_a_zero_filled_all_clear():
    # THIS ASSERTION WAS INVERTED BEFORE REVIEW, and the old behaviour was the
    # hole: missing counts defaulted to 0, so `{}` produced a complete summary
    # reading `quiet` — the only all-clear the vocabulary has, manufactured from
    # a payload that said nothing at all.
    #
    # Two shapes that reach it in real life, neither hypothetical: a future
    # bastion that renames a field (every count then reads as absent), and an
    # error body, which is the box saying it could NOT read its alert source and
    # would have been stored as "read it, all clear" — the precise inversion of
    # the truth, on the field an operator trusts most.
    for payload in ({},
                    {"error": "alertmanager unreachable"},
                    {"status": "error", "at": "2026-09-16T12:00:00Z"},
                    {"alerts_firing": 3, "alerts_critical": 1},   # renamed fields
                    {"firing": 0},                                # partial counts
                    {"firing": 0, "critical": 0},
                    {"firing": 0, "critical": 0, "warning": 0}):  # no `at`
        assert pg.narrow_alerts(payload) is None, payload
        # and it must not become quiet on the read side either
        state, _ = _bastion_alerts_state({"online": True, "alerts": payload})
        assert state == "unconfigured", (payload, state)


def test_presence_of_all_three_counts_and_at_is_what_proves_a_read_happened():
    # Counts with no time cannot be placed (and cannot be judged stale); a time
    # with no counts says nothing. Both together are the proof.
    full = {"firing": 0, "critical": 0, "warning": 0, "at": "2026-09-16T12:00:00Z"}
    assert pg.narrow_alerts(full) is not None, \
        "a complete zero read IS a real all-clear and must still be stored"
    for drop in ("firing", "critical", "warning", "at"):
        partial = {k: v for k, v in full.items() if k != drop}
        assert pg.narrow_alerts(partial) is None, drop
    # `since` stays nullable — nothing firing means nothing has a start time.
    assert pg.narrow_alerts({**full, "since": None}) is not None


def test_malformed_is_none_and_never_a_repaired_zero():
    # EVERY one of these is a box saying something this pod cannot read. The
    # tempting fix — drop the bad field, keep the rest — puts a zero in front of
    # an operator for a machine whose real state is unknown.
    #
    # Each case is built on a COMPLETE valid payload with exactly one field
    # spoiled. Spoiling a partial payload would pass for the wrong reason now
    # that presence is required — the assertion would be satisfied by the
    # missing field rather than by the bad one.
    bad = [
        _with(firing=-1),                      # negative count
        _with(firing="3"),                     # a string where a count belongs
        _with(firing=1.5),                     # a float
        _with(firing=True),                    # bool is an int in Python
        _with(critical=None),                  # null where a count belongs
        _with(critical={}),                    # a dict where a count belongs
        _with(firing=10 ** 9),                 # absurd, so malformed
        _with(fingerprints="abc"),             # a string is iterable and is not a list
        _with(fingerprints={"a": 1}),
        _with(fingerprints=[1, 2]),            # non-string members
        _with(fingerprints=["ok", None]),
        _with(at="not-a-time"),
        _with(since="yesterday"),
        _with(at=1758024000),                  # an epoch int, not RFC3339
        _with(at={"x": 1}),                    # a dict, which used to reach prose
    ]
    for payload in bad:
        assert pg.narrow_alerts(payload) is None, payload
    # the base itself must be valid, or every line above proves nothing
    assert pg.narrow_alerts(_with()) is not None


def test_a_timestamp_must_be_a_full_rfc3339_instant():
    # `datetime.fromisoformat` alone accepts a bare date and a compact date, and
    # both used to pass here. `at` is shown to an operator as
    # the moment a machine last read its alert source AND is subtracted from a
    # heartbeat time, so a date is not a lenient parse, it is a wrong sentence
    # and a wrong duration.
    for good in ("2026-09-16T12:00:00Z", "2026-09-16T12:00:00+02:00",
                 "2026-09-16T12:00:00.123456Z", "2026-09-16t12:00:00z"):
        assert pg.narrow_alerts(_with(at=good)) is not None, good
    for bad in ("2026-09-16",            # a bare date — fromisoformat accepts it
                "20260916",              # compact date — so does this
                "2026-09-16T12:00:00",   # no zone
                "12:00:00Z",             # no date
                "2026-13-99T99:99:99Z",  # shaped right, not a real instant
                ""):
        assert pg.narrow_alerts(_with(at=bad)) is None, bad
    # nullable: nothing firing means nothing has a start time
    out = pg.narrow_alerts(_with(since=None))
    assert out is not None and out["since"] is None


def test_the_two_rfc3339_readers_agree():
    # The rule is stated twice on purpose — brain/ does not import server/ — so
    # the same vector runs through both and they cannot drift apart. A stricter
    # writer with a laxer reader would let a bad value through on the path that
    # actually formats prose.
    for s in ("2026-09-16T12:00:00Z", "2026-09-16T12:00:00+02:00",
              "2026-09-16", "20260916", "2026-09-16T12:00:00", "nope", ""):
        wrote = pg._rfc3339_or_none(s) is not None
        reads = tools._rfc3339(s) is not None
        assert wrote == reads, (s, wrote, reads)


def test_fingerprints_are_validated_before_the_cap_not_after():
    # The cap bounds the HEARTBEAT, never the information: the fingerprints are
    # identifiers to ask the box about with `observe:alerts`, and the counts are
    # the part that has to be complete.
    out = pg.narrow_alerts(_with(firing=50, fingerprints=[f"fp{i}" for i in range(50)]))
    assert out is not None
    assert len(out["fingerprints"]) == pg.ALERT_FINGERPRINT_CAP == 20
    assert out["firing"] == 50, "the cap must not touch the counts"

    # THE POSITION BUG: capping before validating made the
    # refusal depend on WHERE the bad member sat. A box that sends the wrong type
    # sends it everywhere, so index 25 must refuse exactly as index 3 does.
    for idx in (0, 3, 19, 20, 25, 49):
        fps = [f"fp{i}" for i in range(50)]
        fps[idx] = 12345
        assert pg.narrow_alerts(_with(fingerprints=fps)) is None, (
            f"a non-string fingerprint at index {idx} was accepted; whether a "
            f"malformed beat is caught must not depend on how many good "
            f"fingerprints happen to precede it")


# ── 2. storage: the column list actually carries it ─────────────────────────

class Pool:
    """Enough asyncpg surface for upsert_heartbeat, recording every statement.

    Everything after the INSERT in upsert_heartbeat is individually try/excepted
    (pairing records, peer discovery, posture), so a pool that answers nothing
    else is enough to exercise the write itself.
    """
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        self.sql.append((q, a))

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "FROM mcp_client_tokens" in q and "operator_kind = 'bastion'" in q:
            return 1                      # yes, token 7 is this entity's bastion
        return None

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "INSERT INTO network_bastion_heartbeat" in q:
            return {"token_id": 7, "last_seen_at": None}
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

    def insert(self):
        for q, a in self.sql:
            if "INSERT INTO network_bastion_heartbeat" in q:
                return q, a
        raise AssertionError("upsert_heartbeat never wrote the heartbeat row")


def _beat(**extra):
    return {"token_id": 7, "hostname": "box", "platform": "linux", **extra}


def test_a_heartbeat_with_alerts_reaches_the_row():
    # THE DEFECT CLASS, driven end to end: the writer takes a fixed column list
    # and drops what it does not name. Grepping the source would pass with the
    # narrower deleted, so this reads the statement AND its arguments.
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 9003, _beat(alerts=dict(OK)))) is not None
    q, args = pool.insert()
    assert " alerts," in q or "alerts," in q.split("(", 1)[1], \
        "`alerts` is not in the INSERT column list — the beat is dropped on the floor"
    assert "alerts          = COALESCE(EXCLUDED.alerts" in q, \
        "an update beat must not lose the column the insert path stores"
    stored = [a for a in args if isinstance(a, str) and '"firing"' in a]
    assert stored, f"the narrowed summary never reached the arguments: {args!r}"
    got = json.loads(stored[0])
    assert got["firing"] == 3 and got["critical"] == 1
    assert got["fingerprints"] == ["abc123", "def456"]


def test_a_malformed_summary_is_stored_as_null():
    # NULL, not a repaired zero. A box saying something unreadable is the
    # "nobody looked" case, and it must arrive at the reader as one.
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 9003, _beat(alerts={"firing": -4}))) is not None
    q, args = pool.insert()
    # The column must be THERE and the value absent. Asserting only the absence
    # passes vacuously when the whole column has been dropped from the writer,
    # which is the very defect the sibling test above exists for — measured: it
    # stayed green under that mutation until this line was added.
    assert "alerts" in q, "the column is not in the statement at all"
    assert not [a for a in args if isinstance(a, str) and '"firing"' in a], \
        "a malformed summary was repaired into a stored row"


def test_a_beat_with_no_alerts_key_stores_nothing_and_retracts_nothing():
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 9003, _beat())) is not None
    q, args = pool.insert()
    assert not [a for a in args if isinstance(a, str) and '"firing"' in a]
    # COALESCE, so a bastion that cannot report does not wipe one that could.
    assert "COALESCE(EXCLUDED.alerts, network_bastion_heartbeat.alerts)" in q


class Row(dict):
    """asyncpg Record enough: subscript + .keys()."""


def test_the_stored_column_is_selected_and_read_back():
    # A column written and never selected is the same defect wearing the other
    # hat. _bastion_row is what list_bastions maps every row through.
    src = topology.list_bastions.__code__.co_consts
    assert any(isinstance(c, str) and "h.alerts" in c for c in src), \
        "list_bastions does not SELECT h.alerts, so nothing can ever read it"
    base = {k: None for k in (
        "id", "name", "url_handle", "scopes", "hardware_fingerprint",
        "machine_anchor_id", "notes", "created_at", "last_used_at", "hostname",
        "gateway_version", "platform", "tunnel_status", "reported_ip",
        "wg_pubkey", "capabilities", "models", "last_seen_at", "freeagent_ok",
        "freeagent_stores", "enforcing", "nac_posture", "nac_decision",
        "schedulers", "scheduler_scan", "peers", "owner_claim", "prep_tools",
        "prep_scan", "mode", "anchor", "pairings",
        "unapproved_pairing_attempts", "other_bastions",
        "other_bastions_scanned_at", "detect", "online")}
    row = topology._bastion_row(Row({**base, "alerts": json.dumps(OK)}))
    assert row["alerts"]["critical"] == 1
    # and a row from a database that predates v150 reads as never-reported
    assert topology._bastion_row(Row(dict(base)))["alerts"] is None


# ── 3. the reading: four states, and only one is calm ───────────────────────

def _b(online=True, last_seen=SEEN, **alerts):
    """A bastion row as _bastion_row builds it, with a COMPLETE alert summary.

    Every reading test goes through this, so `at` and `last_seen_at` are always
    present and close together — a fixture missing either would read as
    unconfigured or as never-stale and the test would pass for the wrong reason.
    """
    a = None if alerts.get("alerts") is False else {**OK, **alerts}
    return {"name": "n", "hostname": "box", "platform": "linux",
            "online": online, "last_seen_at": last_seen,
            **({"alerts": a} if a is not None else {})}


def test_the_vocabulary_is_enumerated_not_hardcoded():
    # THE DRIFT TEST. Its predecessor on the detect plane was the thing that
    # walked a developer into the round-1 defect: it iterated its OWN hardcoded
    # dict, so a state added to the real vocabulary with no wording fell through
    # into the counts branch, returned an all-clear, and the suite stayed green.
    # So: iterate the REAL constant, and demand of every member of it that it be
    # reachable, worded, and correctly placed on the calm/not-calm line.
    assert tools.ALERTS_STATES == ("unconfigured", "quiet", "firing", "critical",
                                   "stale"), (
        "ALERTS_STATES changed. A new state is not a rename — every reader "
        "below, the get_network tool description and default-skills/network.md "
        "all enumerate these, and a state with no wording reaches the user as "
        "silence. Add its sentence and its paragraph, then update this pin.")
    reachable = {
        "unconfigured": _b(alerts=False),
        "quiet":        _b(firing=0, critical=0, warning=0),
        "firing":       _b(firing=2, critical=0, warning=2),
        "critical":     _b(firing=2, critical=1, warning=1),
        # the read stopped advancing while the beat kept arriving
        "stale":        _b(firing=0, critical=0, warning=0,
                           at="2026-09-16T10:00:00Z"),
    }
    assert set(reachable) == set(tools.ALERTS_STATES), (
        "a state in ALERTS_STATES that no beat can produce, or a beat producing "
        "a state that is not in it")
    for expected, b in reachable.items():
        state, why = _bastion_alerts_state(b)
        assert state == expected, (expected, state)
        assert isinstance(why, str) and why.strip(), f"{expected} owes the host a sentence"
        assert state in tools.ALERTS_STATES


def test_only_quiet_may_be_worded_as_calm():
    # THE CENTRAL PROPERTY. Five states, and the phrase a host will repeat as an
    # all-clear may appear in exactly one of them.
    seen = {}
    for b in (_b(alerts=False),
              _b(firing=0, critical=0, warning=0),
              _b(firing=2, critical=0, warning=2),
              _b(firing=2, critical=2, warning=0),
              _b(firing=0, critical=0, warning=0, at="2026-09-16T10:00:00Z")):
        state, why = _bastion_alerts_state(b)
        seen[state] = why
    assert set(seen) == set(tools.ALERTS_STATES), seen.keys()
    calm = [s for s, why in seen.items() if "no alerts are firing" in why]
    assert calm == ["quiet"], (
        f"{calm} — the calm phrase appears outside `quiet`. It must not appear "
        f"even inside a negation: a host paraphrasing 'this does NOT mean no "
        f"alerts are firing' is one dropped word from an all-clear. Word the "
        f"other states without the phrase at all.")
    # and `unconfigured` must say, in words a host will repeat, that it is not
    # one — a paraphrase of its reason must not be able to imply a quiet machine.
    assert "does NOT mean the machine is quiet" in seen["unconfigured"], seen["unconfigured"]
    assert "nobody is watching" in seen["unconfigured"].lower(), seen["unconfigured"]
    # `stale` carries its own disclaimer: it is an alerting OUTAGE, not calm.
    assert "NOT the machine now" in seen["stale"], seen["stale"]
    assert "stopped reading its alert source" in seen["stale"], seen["stale"]


def test_a_frozen_read_goes_stale_and_never_stays_quiet():
    # THE LONGEST-LIVED FAILURE MODE on this plane. By contract the box keeps its last good summary and folds it onto
    # every beat, so an Alertmanager that dies leaves the machine reporting
    # `quiet` with a FROZEN `at`, for ever, while it heartbeats perfectly.
    # Nothing compared `at` to anything, so that read as the only all-clear the
    # vocabulary has.
    state, why = _bastion_alerts_state(
        _b(firing=0, critical=0, warning=0, at="2026-09-16T10:00:00Z"))
    assert state == "stale", state
    assert state != "quiet"
    assert "2026-09-16T10:00:00Z" in why, "it owes the host the moment it stopped"
    assert "no alerts firing" in why, "it owes the host the last reading it managed"
    # A frozen CRITICAL is stale too — the reading is old either way, and saying
    # so is not the same as retracting it.
    state, why = _bastion_alerts_state(
        _b(firing=2, critical=1, at="2026-09-16T10:00:00Z"))
    assert state == "stale", state
    assert "CRITICAL" in why, "the last reading it managed must still be named"


def test_the_staleness_boundary_is_tested_both_sides():
    # ALERTS_STALE_AFTER_S is derived, not picked (see its comment): three missed
    # refreshes of the sibling refresher the plan models this on. The boundary is
    # pinned both sides so a change to the constant is a deliberate act.
    import datetime as dt
    seen = dt.datetime(2026, 9, 16, 12, 0, 0, tzinfo=dt.timezone.utc)

    def at_lag(seconds):
        return (seen - dt.timedelta(seconds=seconds)).isoformat()

    inside = tools.ALERTS_STALE_AFTER_S - 1
    outside = tools.ALERTS_STALE_AFTER_S + 1
    state, _ = _bastion_alerts_state(
        _b(firing=0, critical=0, warning=0, last_seen=seen.isoformat(),
           at=at_lag(inside)))
    assert state == "quiet", f"a lag of {inside}s is within the window"
    state, _ = _bastion_alerts_state(
        _b(firing=0, critical=0, warning=0, last_seen=seen.isoformat(),
           at=at_lag(outside)))
    assert state == "stale", f"a lag of {outside}s is past the window"
    # exactly at the ceiling is NOT stale — the comparison is strictly greater
    state, _ = _bastion_alerts_state(
        _b(firing=0, critical=0, warning=0, last_seen=seen.isoformat(),
           at=at_lag(tools.ALERTS_STALE_AFTER_S)))
    assert state == "quiet"


def test_staleness_is_measured_against_the_beat_not_our_clock():
    # Both values come off the SAME box, so the comparison asks "how far behind
    # the beat is the read". Measuring `at` against this pod's clock would
    # instead measure skew between a customer's machine and ours and would
    # manufacture staleness on any box whose clock runs behind — something
    # nobody can fix from here, reported as broken.
    #
    # These fixtures are years in the past. Against a wall clock every one of
    # them is enormously stale; against its own beat none of them is.
    state, _ = _bastion_alerts_state(
        _b(firing=0, critical=0, warning=0,
           at="2020-01-01T00:00:00Z", last_seen="2020-01-01T00:00:20+00:00"))
    assert state == "quiet", (
        "staleness is being measured against wall-clock time, so every box with "
        "a slow clock — or every old fixture — reads as broken")
    # and a box whose read is stamped slightly AFTER its beat (clocks disagree
    # by a second) is not stale either; negative lag never trips the ceiling.
    state, _ = _bastion_alerts_state(
        _b(firing=0, critical=0, warning=0,
           at="2026-09-16T12:00:05Z", last_seen="2026-09-16T12:00:00+00:00"))
    assert state == "quiet", state
    # no beat time at all -> the question cannot be asked, and absence of an
    # answer must not be reported as freshness OR as staleness
    assert tools._alerts_read_lag_s({"last_seen_at": None}, {"at": OK["at"]}) is None
    assert tools._alerts_read_lag_s({"last_seen_at": SEEN}, {"at": None}) is None


def test_unconfigured_is_never_quiet_however_it_arrives():
    # Absent key, null column, and a malformed payload that slipped past a
    # narrower all land on the same reading, because they are the same fact
    # about the user: nobody can tell you this machine's alert state.
    for alerts in (None, "nonsense", [], {"firing": -1}, {"firing": "3"},
                   {"firing": True}, {"firing": 0, "fingerprints": "abc"},
                   # a stored row with no `at`: it could never be judged fresh,
                   # so it is not a reading
                   {"firing": 0, "critical": 0, "warning": 0},
                   # `at` the wrong TYPE — this used to be interpolated straight
                   # into the sentence an operator reads
                   {**OK, "at": {"x": 1}},
                   {**OK, "at": ["2026-09-16T12:00:00Z"]},
                   {**OK, "at": "2026-09-16"},
                   {**OK, "since": 12345}):
        b = {"name": "n", "hostname": "box", "online": True,
             "last_seen_at": SEEN, "alerts": alerts}
        state, why = _bastion_alerts_state(b)
        assert state == "unconfigured", (alerts, state)
        assert state != "quiet"
        assert "no alerts are firing" not in why, why
        # nothing unreadable reaches the prose
        assert "{" not in why and "[" not in why, why


def test_the_reading_guards_its_own_inputs_and_that_branch_is_reachable():
    # REVIEW FINDING: this fall-through used to be dead, because _alerts_counts
    # rejected everything before it. A documented safety property with no test
    # behind it is worse than none — it reads as defended and is not. The guard
    # now lives in _alerts_reading itself, so a caller passing a dict it did not
    # validate hits a real branch.
    for c in ({"critical": 0, "firing": 0, "warning": None},
              {"critical": "x", "firing": 0, "warning": 0},
              {"critical": 0, "firing": -1, "warning": 0},
              {"critical": True, "firing": 0, "warning": 0},
              {}):
        state, why = tools._alerts_reading(c)
        assert state == "unconfigured", (c, state)
        assert "cannot read" in why, why
        assert "no alerts are firing" not in why, why
    # and a valid one still reads normally through the same door
    state, _ = tools._alerts_reading(
        {"critical": 0, "firing": 0, "warning": 0, "at": None,
         "since": None, "fingerprints": []})
    assert state == "quiet"


def test_a_critical_is_read_first_so_incoherent_counts_can_never_read_quiet():
    # The counts come off an untrusted machine. If `critical` and `firing` ever
    # disagree, the ORDER of the branches is what decides which way the mistake
    # goes — and it must go towards the alarm. This is the original defect shape
    # exactly: a payload that produces a zero in the field a lazy branch reads.
    state, why = _bastion_alerts_state(_b(firing=0, critical=3, warning=0))
    assert state == "critical", state
    assert "3 CRITICAL" in why, why


def test_counts_are_none_not_zero_when_nothing_was_read():
    # The structural version of the same mistake, and a table is read faster and
    # more wrongly than a sentence: a column of zeros for machines nobody asked.
    assert _alerts_counts(None) is None
    assert _alerts_counts({"firing": -2}) is None
    out = _bastion_summary(_b(alerts=False))
    assert out["alerts"] is None
    assert out["alerts_state"] == "unconfigured"
    # A box that DID read reports its honest zero.
    q = _bastion_summary(_b(firing=0, critical=0, warning=0))
    assert q["alerts_state"] == "quiet"
    assert q["alerts"]["firing"] == 0, "a real read reports its zeros"


def test_get_network_carries_the_state_the_counts_and_the_reason():
    # The whitelist is what the host actually sees; a field absent from
    # _bastion_summary does not reach it, which was the original defect one
    # layer down (the column existed, the summary never left the database).
    out = _bastion_summary(_b())
    assert out["alerts_state"] == "critical"
    assert out["alerts"]["firing"] == 3 and out["alerts"]["warning"] == 2
    assert out["alerts"]["fingerprints"] == ["abc123", "def456"]
    assert "2026-09-16T08:00:00Z" in out["alerts_reason"], \
        "a firing state owes the host the time it started"
    assert "observe:alerts" in out["alerts_reason"], \
        "the rows stay on the box; the host must be told where to look"


def test_an_offline_box_is_never_reported_in_the_present_tense():
    # Its alert source is not being read at all. A machine that went down
    # mid-incident had real criticals — do not drop them, do not repeat them as
    # current. Same composition rule the detect plane's `stale` follows.
    state, why = _bastion_alerts_state(
        _b(online=False, firing=2, critical=1, warning=1,
           at="2026-08-01T00:00:00Z", last_seen="2026-08-01T00:00:20+00:00"))
    assert state == "critical", "an offline box's criticals are not retracted"
    assert "offline" in why and "not as it is now" in why, why
    assert "2026-08-01T00:00:00Z" in why, "a stale reading owes the host its date"
    # An offline QUIET box must not read as a present-tense all-clear either —
    # this is the exact shape that made an offline detect pass report `clean`.
    state, why = _bastion_alerts_state(
        _b(online=False, firing=0, critical=0, warning=0,
           at="2026-08-01T00:00:00Z", last_seen="2026-08-01T00:00:20+00:00"))
    assert state == "quiet"
    assert "not as it is now" in why, why
    # and an offline box that never reported is still unconfigured
    state, _ = _bastion_alerts_state(_b(online=False, alerts=False))
    assert state == "unconfigured", state
    # A box whose alert source died BEFORE the box did is stale AND offline —
    # the state survives the offline composition rather than being replaced.
    state, why = _bastion_alerts_state(
        _b(online=False, firing=0, critical=0, warning=0,
           at="2026-08-01T00:00:00Z", last_seen="2026-08-01T06:00:00+00:00"))
    assert state == "stale", state
    assert "offline" in why and "not as it is now" in why, why


def test_the_reason_never_carries_an_alert_body():
    # Fingerprints are identifiers to ask the box about. The alerts themselves —
    # labels, annotations, the text someone wrote in a rule — stay on the
    # machine, the same way findings do.
    out = _bastion_summary(_b(firing=1, critical=1, warning=0,
                              fingerprints=["fp1"],
                              labels={"alertname": "KubeMemoryOvercommit"}))
    assert "KubeMemoryOvercommit" not in out["alerts_reason"]
    assert "labels" not in (out["alerts"] or {})


# ── 4. the op vocabulary ────────────────────────────────────────────────────

def test_the_three_observe_ops_are_grantable():
    from server.network import nac_dcn, nac_mgmt
    ops = ("observe:alerts", "observe:metrics", "observe:logs")
    for op in ops:
        assert op in nac_dcn.KNOWN_BOX_OPS, (
            f"{op} is not in the host's box-op vocabulary, so a bastion that "
            f"answers it is reported as holding an op this host cannot name")
        assert op.startswith("observe:")
        # The bastion answers them (0.31.x), so they now live in the GENERATED
        # block — the one gen_box_ops.py writes from the Go compiler — and the
        # pending set no longer carries them. A name in both would mean the
        # pending set was never cleaned up after the regeneration.
        assert op in nac_dcn._GATEWAY_BOX_OPS, (
            f"{op} is missing from the generated block — re-run "
            f"scripts/gen_box_ops.py --src <bastion checkout> --write")
        assert op not in nac_dcn._PENDING_BOX_OPS, (
            f"{op} is still parked as pending although the bastion answers it")
    # THE HOST AUTHORIZES BY NAME, NOT BY PREFIX. On the box a cap maps to op
    # families by prefix; on this side mgmt_decision checks the op name against
    # each signed grant's list. So a grant signed before these ops existed —
    # one Host's fleet-wide observe grant is exactly that — does NOT cover them,
    # and saying otherwise (as the pending-set comment once did) sends a user
    # looking for alerts that the host was never allowed to read.
    older = [{"server": "*", "ops": ["uptime", "observe:host", "observe:disk",
                                     "observe:net", "observe:k8s"], "deny": []}]
    for op in ops:
        assert nac_mgmt.mgmt_decision(older, 3, "box-1", op) is False, (
            f"a grant that never named {op} authorized it")
    named = [{"server": "*", "ops": list(ops), "deny": []}]
    for op in ops:
        assert nac_mgmt.mgmt_decision(named, 3, "box-1", op) is True, (
            f"a grant naming {op} did not authorize it")


def test_the_skill_documents_every_state_it_can_show():
    # Vocabulary drift the other way: a state the code can return that the skill
    # has no paragraph for reaches the user as silence.
    skill = open(os.path.join(_ROOT, "default-skills", "network.md"), encoding="utf-8").read()
    # SCOPED TO THE ALERTS SECTION, not the whole file. `stale` is a state name
    # on BOTH planes — the detect section has explained its own `stale` since
    # v146 — so a whole-file search would report the alerts vocabulary as
    # documented on the strength of a paragraph about something else. That is the
    # same vacuous-pass shape the storage test had, one file over.
    assert "**Alerts (" in skill, "the Alerts section is gone from the skill"
    alerts_section = skill[skill.index("**Alerts ("):]
    # And it demands the BULLET, not a mention. Measured while writing this:
    # scoping to the section alone still passed with the `stale` bullet deleted,
    # because the section's closing sentence cross-references "the way `stale`
    # works for detection". A state has to be DEFINED here, not alluded to.
    for state in tools.ALERTS_STATES:
        assert f"- `{state}` —" in alerts_section, (
            f"default-skills/network.md's Alerts section has no definition for "
            f"`{state}` — a state the code can return with no paragraph behind "
            f"it reaches the user as silence")
    for op in ("observe:alerts", "observe:metrics", "observe:logs"):
        assert op in skill, f"the skill does not tell the host that {op} exists"
    # the fixed name tables — the box owns the query, the host may only name it
    for name in ("up", "node_cpu", "node_mem", "node_disk", "pod_restarts", "pod_oom",
                 "kernel", "sshd", "bastion", "k8s-system"):
        assert f"`{name}`" in skill, f"the skill does not list the name {name}"


def _main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} — {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
