#!/usr/bin/env python3
"""The detection plane's summary, on the host side.

Runs as pytest (`pytest scripts/test_detect_summary.py`) AND as a plain script
in the container-free gate lane (APP_ROOT=<repo> python3 scripts/…). No DB, no
network: every function under test is pure.

WHAT THIS FILE IS DEFENDING. The bastion has computed a detection pass on every
heartbeat since 0.30.0 — everything running and every standing job, compared
against the machine's own accounting, noise-filtered and levelled. It sends the
summary on each beat. Until v146 the host had no column for it, so the plane
could report a critical and the host would drop it on the floor.

Storing it is the easy half. The half that needs tests is the WORDING, because
SEVEN different states all produce zero findings and only one of them is an
all-clear:

    unreported     the box never reported a pass at all
    no-baseline    it ran, against no accounting — nobody said what normal is
    unusable       the box refuses to compare and returns before collecting
                   anything: the accounting predates the current fingerprint
                   rules, or the pass is running without machine authority and
                   cannot read what the accounting recorded
    untrusted      it DID compare, against an accounting that was changed after
                   it was taken. The counts are real but a FLOOR — editing the
                   accounting is how something is made not to appear
    unknown-state  it reported a state this host has no wording for, because it
                   is running a newer bastion than this pod
    stale          it is OFFLINE; its last pass describes the machine as it was
                   then, not now
    clean          it compared, and nothing differs      <-- the only all-clear

A host that reads the counts and says "no findings, your machine is clean" is
telling the user something nobody checked in six of those seven. That is the
failure this file exists to prevent, and it is the same quiet-versus-blind rule
the agent-observe plane already follows (_bastion_watch_state).

It was FOUR states when this file was written, and every one of the other three
was a live defect rather than a hypothetical:

  - `unknown-state` fell through an if/elif chain into the counts branch and
    returned `clean`, while the suite stayed green — and the vocabulary-drift
    test below actively walked a developer into it.
  - `stale` had no check at all, so an offline laptop's old clean pass read as
    its state now.
  - `untrusted` was folded into `unusable` and told the host "No comparison was
    made" — but `edited` is the one state that does NOT return early in
    deltaPass (client-go/deltarun.go:218-221 falls through where :193, :201 and
    :216 return), so it compares and produces real counts. A tampered
    accounting with live criticals behind it was reported as a machine nobody
    had looked at, and a later change to null the counts of non-comparing
    states stopped those criticals reaching the host at all.

So the tests below are written to fail on the SHAPE that allowed each: an
enumerated vocabulary rather than a hardcoded map, an explicit allowlist for
the states that genuinely compared, and counts keyed on whether a comparison
happened rather than on what the host decided to call it.

The matching half — the box computing the pass, the fingerprints, the noise
filter — is the gateway's client-go/delta_test.go.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from mastercode.runtime import tools  # noqa: E402
from mastercode.runtime.tools import (  # noqa: E402
    _bastion_detect_state, _bastion_summary)
from server.network import pairing_grants as pg  # noqa: E402

# Every state the bastion can report for the accounting it compared against —
# client-go/deltarun.go:37-43. Pinned here so a state added on the box without a
# word about it on this side fails loudly rather than being read as something
# it is not (it now reads as `unknown-state`, which is safe but says nothing).
BASTION_BASELINE_STATES = ("ok", "none", "edited", "not-administered",
                           "taken-under-older-rules")


def test_the_narrower_and_the_box_agree_on_the_state_vocabulary():
    assert set(pg.DETECT_BASELINE_STATES) == set(BASTION_BASELINE_STATES), (
        "deltarun.go and pairing_grants.DETECT_BASELINE_STATES have drifted; a "
        "state the box can send that this side does not know is dropped whole, "
        "and the beat reads as 'never reported'")


# The closed set of readings this host has words for. `clean` is the ONLY
# all-clear; everything else must be outside it.
HOST_STATES = ("unreported", "no-baseline", "unusable", "untrusted",
               "unknown-state", "stale", "clean", "findings", "partial")


def test_every_box_state_maps_to_a_host_state():
    # No state the bastion can send may fall through to a default. Each one gets
    # a deliberate reading, because each means something different to the user.
    want = {"ok": "clean", "none": "no-baseline", "edited": "untrusted",
            "not-administered": "unusable", "taken-under-older-rules": "unusable"}
    for baseline, expected in want.items():
        got, why = _bastion_detect_state({"online": True, "detect": {"baseline": baseline}})
        assert got == expected, (baseline, got, expected)
        assert isinstance(why, str) and why.strip(), \
            f"{baseline} owes the host a sentence"


def test_the_vocabulary_is_enumerated_not_hardcoded():
    # THE TEST THAT WAS MISSING, and its absence was a live defect: the previous
    # version of the check above iterated its OWN hardcoded 5-entry dict, so a
    # state added to DETECT_BASELINE_STATES with no host wording fell through to
    # the counts branch, returned `clean`, and the suite stayed green. The
    # vocabulary-drift test even instructs you to sync the constant, walking you
    # into it. So: iterate the REAL vocabulary and demand a known reading for
    # every member of it.
    for baseline in pg.DETECT_BASELINE_STATES:
        state, why = _bastion_detect_state({"online": True, "detect": {"baseline": baseline}})
        assert state in HOST_STATES, (baseline, state)
        assert isinstance(why, str) and why.strip(), baseline
        # Only `ok` may be the all-clear. `edited` compares too, but against a
        # file that was changed after it was taken, so its zero is not one.
        if baseline != "ok":
            assert state != "clean", (
                f"{baseline!r} reads as an all-clear; only a completed comparison may")
        # AND it must have wording of its OWN. Falling through to
        # `unknown-state` is safe but it tells the user "your bastion is newer
        # than this host", which is false for a state this host has in its own
        # vocabulary — so a state synced into the constant without a sentence
        # fails here rather than shipping a wrong explanation.
        assert state != "unknown-state", (
            f"{baseline!r} is in DETECT_BASELINE_STATES but _bastion_detect_state "
            f"has no wording for it, so it falls through to 'unknown-state' and "
            f"tells the user their bastion is newer than this host. Add its "
            f"reading — and its REMEDIATION, which differs per state.")


def test_an_unrecognised_state_is_never_an_all_clear():
    # A bastion NEWER than this pod, reporting something this host predates.
    # It must not read as a checked machine — the compare path is an allowlist
    # on `ok`, not a fall-through.
    for unknown in ("scheduler-half-unreadable", "some-future-state", "OK-ish", "x"):
        state, why = _bastion_detect_state({"online": True, "detect": {"baseline": unknown}})
        assert state == "unknown-state", (unknown, state)
        assert state not in ("clean",)
        assert "NOT a clean result" in why, why
    # and it stays out of the all-clear family even with zero counts, which is
    # exactly the shape that made the old code return clean.
    state, _ = _bastion_detect_state({"online": True, "detect": {
        "baseline": "some-future-state", "critical": 0, "high": 0, "warn": 0, "notice": 0}})
    assert state == "unknown-state", state


def test_an_offline_box_is_never_reported_in_the_present_tense():
    # A laptop compromised after its last clean pass, then taken offline, is the
    # case this exists for. _bastion_watch_state checks `online` first; this must
    # too, or a six-week-old clean pass reads as the machine's state now.
    state, why = _bastion_detect_state({"online": False, "detect": {
        "baseline": "ok", "critical": 0, "high": 0, "warn": 0, "notice": 0,
        "at": "2026-08-01T00:00:00Z"}})
    assert state == "stale", state
    assert "offline" in why and "not as it is now" in why, why
    assert "2026-08-01T00:00:00Z" in why, "a stale verdict owes the host its date"
    # An offline box's old CRITICAL must not read as live either.
    state, why = _bastion_detect_state({"online": False, "detect": {
        "baseline": "ok", "critical": 3, "at": "2026-08-01T00:00:00Z"}})
    assert state == "stale", state
    assert "found 3 difference(s)" in why, why
    # offline beats every other state, including the ones that never compared —
    # and for those the underlying reason is carried through instead of being
    # replaced (see test_stale_composes_the_underlying_reading...).
    state, why = _bastion_detect_state({"online": False, "detect": {"baseline": "none"}})
    assert state == "stale" and "nobody has taken an accounting" in why, why


def test_the_box_s_own_caveats_ride_along():
    # `note` carries the plane's standing limits on EVERY pass — the levels are
    # compiled-in rather than admin-signed, and listeners/identities are not
    # compared at all (client-go/delta.go:108). Without it `clean` overstates
    # the reach: a new listening port is not compared, and "found nothing that
    # differs" would be read as "nothing changed". Carried from the box so it
    # cannot go stale here.
    note = "listeners and identities are not in the baseline yet, so this delta cannot report them"
    _, why = _bastion_detect_state({"online": True, "detect": {"baseline": "ok", "note": note}})
    assert "found nothing that differs" in why
    assert note in why, "the clean sentence must not hide the plane's blind spots"
    # and on a findings pass too
    _, why = _bastion_detect_state({"online": True, "detect": {
        "baseline": "ok", "critical": 1, "note": note}})
    assert note in why


def test_not_administered_names_the_right_cause_and_fix():
    # The box sets this when the CURRENT pass is not running with machine
    # authority (client-go/deltarun.go:190-195), NOT when the accounting was
    # taken badly — a baseline cannot be taken without authority at all
    # (baseline.go:579-581 -> privilege.go:39-45). Wording it as a bad
    # accounting sent the user to retake it, which changes nothing. This is the
    # one of the three unusable states whose remediation differs.
    _, why = _bastion_detect_state({"online": True,
                                    "detect": {"baseline": "not-administered"}})
    assert "machine authority" in why, why
    assert "does NOT fix" in why, "it must say retaking the accounting is not the fix"
    # the two that ARE fixed by retaking say so, and do not claim authority
    _, edited = _bastion_detect_state({"online": True, "detect": {"baseline": "edited"}})
    assert "Retaking" in edited or "retaking" in edited
    _, older = _bastion_detect_state({"online": True,
                                      "detect": {"baseline": "taken-under-older-rules"}})
    assert "retaking" in older.lower()


def test_absent_is_not_clean():
    # The key is missing on a bastion older than 0.30.0 and on one whose first
    # pass has not finished. Neither is evidence about the machine.
    for b in ({}, {"detect": None}, {"detect": "nonsense"}, {"detect": []}):
        state, why = _bastion_detect_state(b)
        assert state == "unreported", (b, state)
        assert "NOT a clean result" in why, why


def test_seven_states_show_zero_findings_and_only_one_is_an_all_clear():
    # THE CENTRAL PROPERTY. Identical zero counts, seven different meanings.
    # It was FOUR when this file was written. Two more turned up
    # (an unrecognised state from a newer bastion; an offline box whose last
    # pass happened to be clean) and then the seventh: `edited`, which
    # DOES compare, so a zero against a tampered accounting is the weakest zero
    # of the lot rather than an all-clear.
    zero = {
        "unreported":    {"online": True},
        "no-baseline":   {"online": True, "detect": {"baseline": "none"}},
        "unusable":      {"online": True, "detect": {"baseline": "not-administered"}},
        "untrusted":     {"online": True, "detect": {"baseline": "edited", "critical": 0}},
        "unknown-state": {"online": True, "detect": {"baseline": "from-a-newer-box"}},
        "stale":         {"online": False, "detect": {"baseline": "ok", "critical": 0}},
        "clean":         {"online": True, "detect": {"baseline": "ok", "critical": 0,
                                                     "high": 0, "warn": 0, "notice": 0}},
        "partial":       {"online": True, "detect": {"baseline": "ok", "critical": 0,
                                                     "skipped": ["listener"]}},
    }
    seen = {}
    for expected, b in zero.items():
        state, why = _bastion_detect_state(b)
        assert state == expected, (expected, state)
        d = b.get("detect") if isinstance(b.get("detect"), dict) else {}
        assert sum(int(d.get(k) or 0) for k in ("critical", "high", "warn", "notice")) == 0
        seen[state] = why
    # Exactly one of the seven may be worded as a present-tense "found nothing
    # that differs" — the phrase a host will repeat as an all-clear.
    all_clear = [s for s, why in seen.items()
                 if "found nothing that differs" in why and "not as it is now" not in why]
    assert all_clear == ["clean"], all_clear
    # And each of the other six says, in words a host will repeat, that it is
    # not one. A host paraphrasing the reason must not be able to imply clean.
    disclaimers = ("NOT a clean result", "Not a clean result",
                   "No comparison was made", "not as it is now",
                   "not an all-clear", "standing condition")
    for state in ("unreported", "no-baseline", "unusable", "untrusted",
                  "unknown-state", "stale", "partial"):
        why = seen[state]
        assert any(x in why for x in disclaimers), (state, why)


def test_findings_are_counted_but_never_carried():
    state, why = _bastion_detect_state({"online": True, "detect": {
        "baseline": "ok", "critical": 1, "high": 2, "warn": 3, "notice": 4}})
    assert state == "findings"
    assert "10 difference(s)" in why, why
    for n, level in ((1, "critical"), (2, "high"), (3, "warn"), (4, "notice")):
        assert f"{n} {level}" in why, (n, level, why)
    # The rows stay on the box by design (the machine store is not built), and
    # the host is told where to look rather than handed a page of them.
    assert "observe:delta" in why, why


def test_a_single_notice_is_still_a_finding():
    # Levelling belongs to the box and its signed policy, not to this side. A
    # host that only surfaced criticals would silently redefine the threshold.
    state, _ = _bastion_detect_state({"online": True, "detect": {"baseline": "ok", "notice": 1}})
    assert state == "findings"


def test_get_network_carries_the_state_the_counts_and_the_reason():
    # The whitelist is what the host actually sees; a field absent from
    # _bastion_summary does not reach it, which was the original defect one
    # layer down (the column existed, the summary never left the database).
    out = _bastion_summary({
        "name": "n", "hostname": "box", "online": True, "platform": "linux",
        "detect": {"baseline": "ok", "critical": 2, "high": 0, "warn": 0,
                   "notice": 1, "coverage": "all-processes", "note": "n",
                   "at": "2026-09-15T15:00:00Z", "short_lived": 4, "filtered": 6},
    })
    assert out["detect_state"] == "findings"
    assert "3 difference(s)" in out["detect_reason"]
    assert out["detect"]["critical"] == 2 and out["detect"]["notice"] == 1
    assert out["detect"]["coverage"] == "all-processes"
    # counts are ints even when the beat omitted them
    assert out["detect"]["warn"] == 0

    # A bastion that never reported still gets the three fields, so a host never
    # has to infer meaning from a missing key.
    quiet = _bastion_summary({"name": "n", "hostname": "box", "online": True,
                              "platform": "linux"})
    assert quiet["detect"] is None
    assert quiet["detect_state"] == "unreported"
    assert "NOT a clean result" in quiet["detect_reason"]


def test_edited_compared_so_its_counts_are_real_and_it_is_never_an_all_clear():
    # `edited` is the ONE state that does not return early in deltaPass: it
    # sets the state, appends a note and falls through
    # (client-go/deltarun.go:218-221), so deltaCollectLive, deltaCompare, the
    # noise filter and the levelling all run — the other three return at :193,
    # :201 and :216. The box's own surface says the same: it hard errors on
    # `none` and prints nothing, while for `edited` it prints "WARNING … what
    # follows is measured against an edited file" AND THEN the findings
    # (:507-524).
    #
    # Reading it as "no comparison was made" and discarding the counts hid the
    # most adversarial case the plane has: someone edits baseline.json so their
    # own process is accounted for, the digest stops matching, the pass runs
    # anyway and reports real criticals, and the host said nobody had looked.
    assert "edited" in tools.DETECT_COMPARED_STATES
    assert tools._detect_compared("edited") is True

    b = {"name": "n", "hostname": "h", "online": True,
         "detect": {"baseline": "edited", "critical": 2, "high": 5, "notice": 1}}
    state, why = _bastion_detect_state(b)
    assert state == "untrusted", state
    assert "No comparison was made" not in why, \
        "edited DOES compare; claiming otherwise is what hid live criticals"
    # the numbers must reach the host
    out = _bastion_summary(b)
    assert out["detect"]["critical"] == 2 and out["detect"]["high"] == 5, out["detect"]
    # ...and be framed as a floor, because editing the accounting is how a
    # process is made not to appear.
    assert "MINIMUM" in why, why
    assert "observe:baseline --force" in why, "it owes the user the remediation"

    # ZERO differences against an edited file is the case that must not slip
    # through as clean — it is weaker evidence than a zero anywhere else.
    state, why = _bastion_detect_state({"online": True, "detect": {
        "baseline": "edited", "critical": 0, "high": 0, "warn": 0, "notice": 0}})
    assert state == "untrusted", state
    assert "found nothing that differs" not in why
    assert "not an all-clear" in why, why


def test_offline_counts_follow_the_comparison_not_the_label():
    # `stale` covers all five baseline states, so keying the counts on the HOST
    # state reintroduced the zero-counts defect for every offline box — which
    # for a laptop is most of the time. A closed laptop that never had an
    # accounting taken handed the host critical: 0.
    for baseline in ("none", "not-administered", "taken-under-older-rules",
                     "from-a-newer-box"):
        out = _bastion_summary({"name": "n", "hostname": "h", "online": False,
                                "detect": {"baseline": baseline, "critical": 3,
                                           "at": "2026-08-01T00:00:00Z"}})
        assert out["detect_state"] == "stale", (baseline, out["detect_state"])
        for k in ("critical", "high", "warn", "notice"):
            assert out["detect"][k] is None, (
                f"offline + {baseline}: {k} is {out['detect'][k]!r}; nothing was "
                f"ever compared, so there is no number to report")
    # the two that DID compare keep their numbers even offline — they were real,
    # and detect_reason is what puts them in the past tense.
    for baseline in ("ok", "edited"):
        out = _bastion_summary({"name": "n", "hostname": "h", "online": False,
                                "detect": {"baseline": baseline, "critical": 3,
                                           "at": "2026-08-01T00:00:00Z"}})
        assert out["detect_state"] == "stale"
        assert out["detect"]["critical"] == 3, (baseline, out["detect"])


def test_stale_composes_the_underlying_reading_and_only_tenses_observations():
    # A standing configuration gap does not become outdated by the box going
    # offline. "describes the machine as it was then, not as it is now" would
    # frame a fact that is still true and still actionable as possibly-stale.
    _, why = _bastion_detect_state({"online": False, "detect": {
        "baseline": "none", "at": "2026-08-01T00:00:00Z"}})
    assert "standing condition" in why, why
    assert "nobody has taken an accounting" in why, "the reason must survive composition"
    assert "as it was then" not in why, "a config gap is not an out-of-date observation"
    # A real observation DOES get the tense clause and the date.
    _, why = _bastion_detect_state({"online": False, "detect": {
        "baseline": "ok", "critical": 3, "at": "2026-08-01T00:00:00Z"}})
    assert "as it was then, not as it is now" in why, why
    assert "3 difference(s)" in why and "2026-08-01T00:00:00Z" in why


def test_offline_plus_an_unrecognised_state_claims_neither():
    # The offline branch had two compositions — "that was an
    # observation, here it is in the past tense" and "that is a standing
    # condition, not an out-of-date observation" — and an unrecognised state
    # fits neither: we cannot know whether a newer bastion's unknown state
    # compared anything. Asserting "nothing has been compared" immediately
    # before "nothing can be concluded from it" contradicted the same sentence.
    _, why = _bastion_detect_state({"online": False, "detect": {
        "baseline": "from-a-newer-box", "at": "2026-08-01T00:00:00Z"}})
    assert "cannot be determined from here" in why, why
    # it must claim NEITHER of the two things it does not know
    assert "Nothing has been compared" not in why, why
    assert "as it was then, not as it is now" not in why, why
    # and the underlying reading still survives composition
    assert "does not recognise" in why and "NOT a clean result" in why, why
    # the two states that DO fit a composition keep theirs
    _, obs = _bastion_detect_state({"online": False, "detect": {"baseline": "ok"}})
    assert "as it was then, not as it is now" in obs
    _, gap = _bastion_detect_state({"online": False, "detect": {"baseline": "none"}})
    assert "standing condition" in gap


def test_counts_are_null_not_zero_when_nothing_was_counted():
    # The STRUCTURAL half of the same property. A host tabulating bastion cards
    # reads a table faster than a sentence, and `critical: 0` for a machine
    # nobody compared is an all-clear in the column that matters. None is
    # unambiguous: no number, because nothing was counted.
    #
    # Found by review as a gap in this file, not in the code: the code nulls
    # them, and nothing here failed when that was reverted.
    for label, b in (
        ("no accounting", {"online": True, "detect": {"baseline": "none"}}),
        ("no machine authority", {"online": True, "detect": {"baseline": "not-administered"}}),
        ("older rules", {"online": True, "detect": {"baseline": "taken-under-older-rules"}}),
        ("state from a newer box", {"online": True, "detect": {"baseline": "from-the-future"}}),
        # bastion 0.31.1: the walk failed, so a compared state with zero counts
        # counted nothing — the newest member of this set.
        ("walk failed", {"online": True, "detect": {"baseline": "ok", "critical": 0,
                                                     "walk_failed": "read /proc: too many open files"}}),
        ("walk failed on an edited accounting", {"online": True, "detect": {
            "baseline": "edited", "critical": 0, "walk_failed": "ps sweep: exit 1"}}),
    ):
        out = _bastion_summary({"name": "n", "hostname": "h", **b})
        for k in ("critical", "high", "warn", "notice", "short_lived", "filtered"):
            assert out["detect"][k] is None, (
                f"{label}: detect[{k}] is {out['detect'][k]!r}, not None — a machine "
                f"nobody compared must not report a count")
        # the state itself still travels, so the host knows WHY there is no number
        assert out["detect"]["baseline"] == b["detect"]["baseline"]
        assert out["detect_state"] != "clean"

    # A box that DID compare reports real numbers, including honest zeros.
    clean = _bastion_summary({"name": "n", "hostname": "h", "online": True,
                              "detect": {"baseline": "ok", "critical": 0, "high": 0,
                                         "warn": 0, "notice": 0}})
    assert clean["detect_state"] == "clean"
    assert clean["detect"]["critical"] == 0, "a real comparison reports its zeros"

    # And an offline box keeps the numbers its last pass produced — they were
    # real; detect_reason is what puts them in the past tense.
    stale = _bastion_summary({"name": "n", "hostname": "h", "online": False,
                              "detect": {"baseline": "ok", "critical": 3,
                                         "at": "2026-08-01T00:00:00Z"}})
    assert stale["detect_state"] == "stale"
    assert stale["detect"]["critical"] == 3


def test_a_failed_walk_is_unusable_never_clean():
    """bastion 0.31.1: a pass whose process walk failed still reports
    baseline=ok with zero counts — the exact shape of clean — and says so in
    its own key. Read before the counts, as the unusable family (no
    comparison was made; retaking the accounting does not fix it)."""
    d = pg.narrow_detect({"baseline": "ok", "critical": 0, "high": 0, "warn": 0, "notice": 0,
                          "note": "the process walk failed this pass (read /proc: too many open files)",
                          "walk_failed": "read /proc: too many open files"})
    assert d["walk_failed"] == "read /proc: too many open files", d
    state, why = _bastion_detect_state({"online": True, "detect": d})
    assert state == "unusable", (state, why)
    assert "nothing was compared" in why and "too many open files" in why, why
    assert "Not a clean result" in why, why
    # The key is capped like its siblings, and absent-or-empty reads as before.
    assert len(pg.narrow_detect({"baseline": "ok", "walk_failed": "x" * 900})["walk_failed"]) == 200
    assert _bastion_detect_state({"online": True, "detect": pg.narrow_detect({"baseline": "ok"})})[0] == "clean"
    # Buried in `note` alone (a bastion before 0.31.1) it still reads clean —
    # that is the residual the key exists to close, stated so it is known.
    assert _bastion_detect_state({"online": True, "detect": pg.narrow_detect(
        {"baseline": "ok", "note": "the process walk failed this pass"})})[0] == "clean"
    # Paired with `edited` (a bad digest still runs the walk): unusable, not
    # untrusted — untrusted asserts a comparison ("reports no differences
    # against that file") that never ran.
    state, why = _bastion_detect_state({"online": True, "detect": pg.narrow_detect(
        {"baseline": "edited", "walk_failed": "ps sweep: exit 1"})})
    assert state == "unusable" and "nothing was compared" in why, (state, why)
    assert "no differences" not in why, why
    # The cause travels on the card beside the nulls, as `baseline` does.
    card = _bastion_summary({"name": "n", "hostname": "h", "online": True,
                             "detect": pg.narrow_detect({"baseline": "ok", "walk_failed": "read /proc: EMFILE"})})
    assert card["detect"]["walk_failed"] == "read /proc: EMFILE", card["detect"]
    assert card["detect"]["critical"] is None and card["detect_state"] == "unusable", card
    # A normal pass carries the key as None, never "" — a table reads None as
    # absent and "" as a cause.
    ok = _bastion_summary({"name": "n", "hostname": "h", "online": True,
                           "detect": pg.narrow_detect({"baseline": "ok", "critical": 1})})
    assert ok["detect"]["walk_failed"] is None and ok["detect"]["critical"] == 1, ok["detect"]
    # THE BINARY RUNG RUNS WITHOUT THE WALK. A failed-walk pass whose own
    # bastion binary was swapped carries that critical (client-go/deltarun.go
    # keeps the binary event on such a pass); nulling it behind "nothing
    # looked" would hide the one finding no flag may suppress.
    swapped = _bastion_summary({"name": "n", "hostname": "h", "online": True,
                                "detect": pg.narrow_detect({"baseline": "ok", "critical": 1,
                                                            "walk_failed": "read /proc: EMFILE"})})
    assert swapped["detect_state"] == "findings", swapped
    assert swapped["detect"]["critical"] == 1 and swapped["detect"]["walk_failed"] == "read /proc: EMFILE", swapped["detect"]
    assert "bastion binary" in swapped["detect_reason"] and "NOT compared" in swapped["detect_reason"], swapped["detect_reason"]
    # short_lived / filtered are walk-derived: nulls on a failed-walk pass even
    # when the binary rung's level counts are numbers.
    assert swapped["detect"]["short_lived"] is None and swapped["detect"]["filtered"] is None, swapped["detect"]
    # A swap with a version bump levels HIGH, not critical (client-go/delta.go):
    # the reading and the counts must agree on that shape too.
    bumped = _bastion_summary({"name": "n", "hostname": "h", "online": True,
                               "detect": pg.narrow_detect({"baseline": "ok", "high": 1,
                                                           "walk_failed": "read /proc: EMFILE"})})
    assert bumped["detect_state"] == "findings" and bumped["detect"]["high"] == 1, bumped
    # The branch is an allowlist on the compared states: a walk_failed key
    # beside a state that never walks keeps that state's own reading — with
    # counts as well as without, or a future state would be read as a real
    # binary finding this pod has no wording for.
    for counts in ({}, {"critical": 1}):
        assert _bastion_detect_state({"online": True, "detect": {"baseline": "none", "walk_failed": "x", **counts}})[0] == "no-baseline"
        assert _bastion_detect_state({"online": True, "detect": {"baseline": "from-the-future", "walk_failed": "x", **counts}})[0] == "unknown-state"
    # A raw dict with junk counts must null, not raise — on a never-compared
    # state, and on the one path that sums counts before the narrower's
    # coercion is guaranteed (a failed walk on a compared state).
    assert _bastion_summary({"name": "n", "hostname": "h", "online": True,
                             "detect": {"baseline": "none", "warn": "lots"}})["detect"]["warn"] is None
    junk = _bastion_summary({"name": "n", "hostname": "h", "online": True,
                             "detect": {"baseline": "ok", "walk_failed": "x", "warn": "lots"}})
    assert junk["detect_state"] == "unusable" and junk["detect"]["warn"] is None, junk
    # Whitespace is not a cause (the narrower strips; the reading must agree).
    assert _bastion_detect_state({"online": True, "detect": {"baseline": "ok", "walk_failed": "   "}})[0] == "clean"
    assert _bastion_summary({"name": "n", "hostname": "h", "online": True,
                             "detect": {"baseline": "ok", "critical": 0, "walk_failed": "   "}})["detect"]["critical"] == 0

def test_a_skipped_class_is_never_read_as_clean():
    """The bastion names the classes a pass did not compare. Zero counts then
    say nothing about them: a machine whose only finding is a listener read as
    clean on every pass where `ss` failed."""
    d = pg.narrow_detect({"baseline": "ok", "critical": 0, "skipped": ["listener", "identity"]})
    state, why = _bastion_detect_state({"online": True, "detect": d})
    assert state == "partial", (state, why)
    assert "listening ports" in why and "accounts" in why and "NOT compared" in why, why
    assert "not an all-clear" in why, why
    # With findings: still `findings`, and the counts are said to leave the class out.
    d = pg.narrow_detect({"baseline": "ok", "high": 1, "skipped": ["listener"]})
    state, why = _bastion_detect_state({"online": True, "detect": d})
    assert state == "findings" and "listening ports were NOT compared" in why, (state, why)
    # Against an edited accounting: still `untrusted`, and the class is named.
    d = pg.narrow_detect({"baseline": "edited", "skipped": ["identity"]})
    state, why = _bastion_detect_state({"online": True, "detect": d})
    assert state == "untrusted" and "accounts" in why and "NOT compared" in why, (state, why)
    # The counts the Host reads name the classes they leave out.
    counts = tools._detect_counts(pg.narrow_detect({"baseline": "ok", "skipped": ["listener"]}))
    assert counts["skipped"] == ["listener"] and counts["critical"] == 0, counts
    assert tools._detect_counts(pg.narrow_detect({"baseline": "ok"}))["skipped"] is None
    # Nothing skipped: clean, exactly as before.
    d = pg.narrow_detect({"baseline": "ok", "critical": 0})
    assert _bastion_detect_state({"online": True, "detect": d})[0] == "clean"

def test_a_skipped_class_is_worded_by_its_cause():
    """The coverage line says why a class was left out, and the remedy differs:
    only an accounting that predates the class is fixed by a retake."""
    four = "processes=all,schedulers=all,listeners=all,identities=login-shells"
    cases = {
        # the accounting predates both classes: retake adds them
        "processes=all,schedulers=all": ("Retaking", "accounting predates"),
        # the platform cannot read them: a retake changes nothing
        "processes=recognised-only,schedulers=recognised-only,listeners=unsupported,identities=unsupported":
            ("does not change that", "cannot read them"),
        # the platform CAN read them and the accounting missed them: fix, retake
        "processes=all,schedulers=all,listeners=unread,identities=unread":
            ("fix that access first", "--force"),
        # an empty value is read as not in the accounting, as the bastion reads it
        "processes=all,schedulers=all,listeners=,identities=": ("Retaking", "accounting predates"),
        # covered, and this pass could not read them: transient
        four: ("on this pass", "could not read them"),
    }
    for coverage, (must, also) in cases.items():
        d = pg.narrow_detect({"baseline": "ok", "coverage": coverage,
                              "skipped": ["listener", "identity"]})
        state, why = _bastion_detect_state({"online": True, "detect": d})
        assert state == "partial" and must in why and also in why, (coverage, why)
    # A retake is suggested ONLY where it helps.
    d = pg.narrow_detect({"baseline": "ok", "coverage": four, "skipped": ["listener"]})
    assert "--force" not in _bastion_detect_state({"online": True, "detect": d})[1]
    # And the counts name what a retake would add, only for that cause.
    c = tools._detect_counts(pg.narrow_detect({"baseline": "ok", "coverage": "processes=all,schedulers=all",
                                               "skipped": ["listener", "identity"]}))
    assert c["retake_adds"] == ["listener", "identity"], c
    c = tools._detect_counts(pg.narrow_detect({"baseline": "ok", "coverage": four, "skipped": ["listener"]}))
    assert c["retake_adds"] is None, c
    c = tools._detect_counts(pg.narrow_detect({"baseline": "ok", "skipped": ["listener", "identity"],
        "coverage": "processes=all,schedulers=all,listeners=unread,identities=unsupported"}))
    assert c["retake_adds"] == ["listener"], c


def test_the_reading_drops_a_class_name_it_does_not_know():
    """The narrower filters `skipped` on the way in, but the reading must not
    depend on that: a raw row with a stray class name reads the known ones and
    drops the rest, rather than raising inside get_network."""
    state, why = _bastion_detect_state({"online": True, "detect": {
        "baseline": "ok", "critical": 0, "skipped": ["listener", "rm -rf"]}})
    assert state == "partial" and "listening ports" in why and "rm -rf" not in why, (state, why)
    counts = tools._detect_counts({"baseline": "ok", "skipped": ["rm -rf"]})
    assert counts["skipped"] is None, counts
    # Only unknown names: nothing known was skipped, so the pass reads clean.
    state, _ = _bastion_detect_state({"online": True, "detect": {
        "baseline": "ok", "critical": 0, "skipped": ["rm -rf"]}})
    assert state == "clean", state


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
