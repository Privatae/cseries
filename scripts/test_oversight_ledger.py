#!/usr/bin/env python3
"""The oversight ledger: the folds, the pin, the resolve, and the wake.

Runs as pytest
(`pytest scripts/test_oversight_ledger.py`) AND as a plain script in the
container-free gate lane (APP_ROOT=<repo> python3 scripts/…). No DB, no
network: every rule is a pure function over a heartbeat fixture, and the two
halves that touch a pool drive the real code against a recording stub.

WHAT THIS FILE IS DEFENDING, in four parts.

1. THE FOLDS. Four planes — alerts, detection, host posture, offline — become
   rows. The failure mode each fold has is the fleet's standing one: a machine
   that was never looked at reading as a machine that was found well. The
   ledger's answer to that is structural rather than verbal — it writes
   FINDINGS ONLY, never an all-clear — so the tests here assert the silence as
   hard as the rows: a box with no posture key produces NO row, and a box with
   posture produces exactly the rows its values earn.

2. THE PIN. A finding is one row with a moving last_seen, never a pile. The
   second pass over the same fact updates. `first_seen` never moves — it is the
   only thing in this whole lane that a heartbeat cannot tell you. And severity
   MAY RISE, NEVER FALL inside a pinned row: a firewall reported high yesterday
   and warn today is still the same finding and the worse number is the true
   one.

3. THE RESOLVE. Almost nothing auto-resolves, and that is the point: a machine
   that stops reporting a problem has usually stopped reporting. The one
   exception is tested on all three of its conditions, because dropping any of
   them turns "we lost sight of it" into "it was fixed".

4. THE WAKE. Exactly once per 0->critical edge, never on critical->critical,
   and debounced per entity. A wake that fired on every beat of an ongoing
   incident would run the ingest sixty times a minute across a fleet.

THE PORT. Every host-posture threshold comes from an existing fleet monitor's
rules, and the test for each one names the rule it came from. That is not
decoration: the rule this codebase holds is that numbers are researched or
ported, never invented, and a port whose provenance lives only in a comment is
one refactor away from being an invention. The two the monitor rules whose inputs
are NOT on the heartbeat (`puv`, `input`) are asserted absent, so a later reader
cannot add a rule that silently never fires.

MUTATION RESULTS (each shown red before this file was finished, see the PR):
  fold      _posture_pubwall `startswith("accept")` -> `startswith("drop")`
  pin       merge_findings `worst(...)` -> `d["severity"]`
  edge      rising_critical_edge `before == 0` -> `True`
  debounce  _wake_debounce_ok early return -> `True`
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from mastercode.runtime import direct_ops as dops          # noqa: E402
from mastercode.runtime import meta_tool_caps as caps      # noqa: E402
from mastercode.runtime import oversight as ov             # noqa: E402
from mastercode import public_tool_registry as reg         # noqa: E402
from server.network import pairing_grants as pg            # noqa: E402
from server.network import topology                        # noqa: E402



def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# The contract the bastion's compact posture builder implements
# (the gateway's client-go/observe_ops.go:126-139). Written out once so every
# fixture below draws its field names from the same place.
POSTURE_KEYS = ("ufw", "pubwall", "ssh22", "f2b", "sec", "load", "cpus",
                "reboot", "diskpct", "jrn")

# A machine with nothing wrong with it. Every posture test starts from this and
# changes ONE field, so a rule that fires on a healthy box is caught by the
# neighbouring test rather than by inspection.
CLEAN_POSTURE = {"ufw": "active", "pubwall": "drop+tdrop", "ssh22": "scoped",
                 "f2b": "active", "sec": 0, "load": 0.4, "cpus": 8,
                 "reboot": "no", "diskpct": 31, "jrn": 12}

ALERTS_OK = {"firing": 2, "critical": 1, "warning": 1,
             "since": "2026-09-16T08:00:00Z", "at": "2026-09-16T12:00:00Z",
             "fingerprints": ["fpA", "fpB"]}
SEEN = "2026-09-16T12:00:30+00:00"


def bastion(**kw) -> dict:
    """An ONLINE bastion row in the shape topology._bastion_row produces."""
    b = {"id": 7, "name": "box-1", "hostname": "box-1", "online": True,
         "last_seen_at": SEEN, "alerts": None, "detect": None, "posture": None}
    b.update(kw)
    return b


def keys_of(findings) -> set:
    return {f["key"] for f in findings}


def by_kind(findings, kind) -> list:
    return [f for f in findings if f["kind"] == kind]


# ── 1. the folds ────────────────────────────────────────────────────────────

def test_a_machine_with_nothing_wrong_produces_no_rows():
    # THE STRUCTURAL ALL-CLEAR, and it is the property every other test here
    # leans on. The ledger holds findings only, so "no row" is the only thing
    # it ever says about a healthy machine — which is what makes it safe for a
    # box that was never READ to also produce no row. If this ever starts
    # emitting a row for a clean machine, the two become indistinguishable and
    # the whole design argument goes with it.
    found = ov.derive_findings([bastion(posture=dict(CLEAN_POSTURE))])
    assert found == [], f"a clean machine produced rows: {keys_of(found)}"


def test_a_machine_that_reported_no_posture_produces_no_posture_row():
    # NULL posture = an older bastion, a non-Linux box, or the first beat after
    # a restart (client-go/observe_ops.go:786-795). Not "its posture is fine",
    # and not a finding either — there is nothing to find.
    assert by_kind(ov.derive_findings([bastion(posture=None)]), "posture") == []


def test_alerts_fold_one_row_per_fingerprint_at_the_machine_severity():
    found = by_kind(ov.derive_findings([bastion(alerts=dict(ALERTS_OK))]), "alert")
    assert keys_of(found) == {"box-1|alert|fpA", "box-1|alert|fpB"}
    # critical > 0 on the machine, so every fingerprint from it is filed
    # critical: which fingerprint is the critical one is NOT on the beat
    # (narrow_alerts carries three counts and up to twenty identifiers, and no
    # mapping between them). Over-stating some rows is the only safe direction.
    assert {f["severity"] for f in found} == {"critical"}
    assert {f["source"] for f in found} == {"alert"}


def test_alert_severity_follows_the_counts_and_never_reaches_below_notice():
    assert ov._alert_severity({"critical": 1, "warning": 0}) == "critical"
    assert ov._alert_severity({"critical": 0, "warning": 3}) == "warn"
    # Firing with neither count set: a real Alertmanager state (rules with no
    # severity label), so it is filed at the bottom rather than dropped.
    assert ov._alert_severity({"critical": 0, "warning": 0}) == "notice"


def test_a_stale_alert_source_is_its_own_high_finding():
    # The box is heartbeating and its alert source stopped answering: an
    # alerting OUTAGE on a live machine, which is a different fact from
    # anything the alerts themselves say. tools.ALERTS_STALE_AFTER_S is 15 min;
    # this read is four hours behind its beat.
    stale = dict(ALERTS_OK, at="2026-09-16T08:00:00Z")
    found = ov.derive_findings([bastion(alerts=stale)])
    src = by_kind(found, "alert-source")
    assert [f["key"] for f in src] == ["box-1|alert-source|stale"]
    assert src[0]["severity"] == "high"
    assert "stale" in src[0]["title"]


def test_an_unconfigured_alert_plane_produces_nothing_not_a_quiet_row():
    # Today this is most of the fleet. A row saying anything at all here would
    # be the host reporting on a machine nobody is watching.
    found = ov.derive_findings([bastion(alerts=None)])
    assert by_kind(found, "alert") == [] and by_kind(found, "alert-source") == []


def test_detect_counts_fold_one_row_per_level_and_skip_zeros():
    d = {"baseline": "ok", "at": "2026-09-16T12:00:00Z",
         "critical": 2, "high": 0, "warn": 1, "notice": 0}
    found = by_kind(ov.derive_findings([bastion(detect=d)]), "detect")
    assert keys_of(found) == {"box-1|detect|critical", "box-1|detect|warn"}
    assert {f["key"]: f["severity"] for f in found} == {
        "box-1|detect|critical": "critical", "box-1|detect|warn": "warn"}


def test_a_detect_state_that_compared_nothing_is_a_high_finding():
    # The seven states are default-skills/network.md:76-117. `no-baseline`
    # means nobody ever said what normal looks like on that machine — the
    # question has not been asked, which is the opposite of an all-clear.
    found = by_kind(ov.derive_findings([bastion(detect={"baseline": "none"})]),
                    "detect-state")
    assert [f["key"] for f in found] == ["box-1|detect-state|no-baseline"]
    assert found[0]["severity"] == "high"


def test_clean_findings_and_unreported_get_no_state_row():
    # clean/findings: the plane did its job (counts fold separately).
    # unreported: every bastion older than the plane, which today is most of
    # the fleet — a row per box would be a ledger nobody can act on.
    clean = {"baseline": "ok", "at": "2026-09-16T12:00:00Z",
             "critical": 0, "high": 0, "warn": 0, "notice": 0}
    findings = {"baseline": "ok", "at": "2026-09-16T12:00:00Z", "critical": 1}
    # partial: compared what it could, a class left out; the reading says so.
    partial = {"baseline": "ok", "at": "2026-09-16T12:00:00Z", "critical": 0,
               "skipped": ["listener"]}
    for d in (clean, findings, partial, None):
        assert by_kind(ov.derive_findings([bastion(detect=d)]), "detect-state") == [], d
    assert ov.DETECT_STATES_WITHOUT_FINDING == ("clean", "findings", "unreported", "partial")


def test_an_offline_machine_is_one_warn_finding():
    found = by_kind(ov.derive_findings([bastion(online=False)]), "offline")
    assert [f["key"] for f in found] == ["box-1|offline|"]
    assert found[0]["severity"] == "warn"


def test_the_machine_name_is_never_blank():
    # Two unrelated machines sharing a key would merge two machines' history
    # into one row. hostname, then the pairing name, then the token id.
    assert ov.machine_of({"hostname": "h", "name": "n", "id": 3}) == "h"
    assert ov.machine_of({"hostname": "  ", "name": "n", "id": 3}) == "n"
    assert ov.machine_of({"id": 3}) == "token:3"


# ── 1b. the the monitor port, rule by rule, each with its line ──────────────────

def _posture_findings(**override):
    p = dict(CLEAN_POSTURE)
    p.update(override)
    return {f["key"].split("|")[-1]: f
            for f in by_kind(ov.derive_findings([bastion(posture=p)]), "posture")}


def test_journald_threshold_is_the_monitors_1800s():
    # fleet monitor rule journald — `if jrn > 1800` -> high.
    assert "journald" not in _posture_findings(jrn=1800), "1800 is not > 1800"
    hit = _posture_findings(jrn=1801)["journald"]
    assert hit["severity"] == "high"
    assert hit["evidence"]["threshold_s"] == 1800
    assert hit["evidence"]["ported_from"] == "fleet monitor rule: journald"


def test_fail2ban_rule_is_gated_on_ssh22_exactly_as_monitor_gates_it():
    # fires only when 22 is NOT scoped. Measured:
    # without the gate it fired five mediums no action could clear,
    # because on a box whose perimeter allow-lists 22 no packet reaches sshd.
    assert "f2b" not in _posture_findings(f2b="inactive", ssh22="scoped")
    hit = _posture_findings(f2b="inactive", ssh22="unscoped")["f2b"]
    assert hit["severity"] == "warn", "the monitor's medium maps to warn"
    assert hit["evidence"]["ported_from"] == "fleet monitor rule: fail2ban"
    # `unknown` is not `scoped`: the monitor's gate is `!= "scoped"`, so a rule it
    # could not read still raises. Reading the gate as `== "unscoped"` would
    # make an unreadable rule silently safe.
    assert "f2b" in _posture_findings(f2b="inactive", ssh22="unknown")
    # And a field the box did not report cannot fire it at all.
    assert "f2b" not in _posture_findings(f2b=None, ssh22="unscoped")


def test_pubwall_failopen_is_the_accept_policy_and_only_that():
    # `startswith("accept")` -> high. `accept+tdrop` is
    # the fail-open shape: the chain survives only on its trailing drop rule.
    for value in ("accept", "accept+tdrop"):
        hit = _posture_findings(pubwall=value)["pubwall-failopen"]
        assert hit["severity"] == "high", value
        assert hit["evidence"]["ported_from"] == "fleet monitor rule: public wall fail-open"
    for value in ("drop", "drop+tdrop", "absent"):
        assert "pubwall-failopen" not in _posture_findings(pubwall=value), value


def test_security_backlog_is_any_pending_package():
    # `int(sec) > 0` -> medium.
    assert "sec-updates" not in _posture_findings(sec=0)
    hit = _posture_findings(sec=1)["sec-updates"]
    assert hit["severity"] == "warn" and hit["evidence"]["sec"] == 1
    assert hit["evidence"]["ported_from"] == "fleet monitor rule: security updates"


def test_pending_reboot_is_a_notice():
    # low, "kernel/libc update pending activation".
    assert "reboot" not in _posture_findings(reboot="no")
    hit = _posture_findings(reboot="yes")["reboot"]
    assert hit["severity"] == "notice", "the monitor's low maps to notice"
    assert hit["evidence"]["ported_from"] == "fleet monitor rule: reboot required"


def test_load_is_per_core_with_the_monitors_flat_fallback():
    # Per-core: `load1 >= cpus` is a full run queue. The
    # flat `> 8` survives ONLY where the core count is unknown, which is the
    # half easiest to drop in a port — and dropping it would silence every box
    # whose bastion is too old to report `cpus`.
    assert "load" not in _posture_findings(load=7.9, cpus=8)
    assert _posture_findings(load=8.0, cpus=8)["load"]["severity"] == "warn"
    # 80-core box at load 12.98: the monitor's own example of what the flat
    # threshold got wrong for 44 days.
    assert "load" not in _posture_findings(load=12.98, cpus=80)
    # cpus unknown -> the flat threshold, boundary included.
    assert "load" not in _posture_findings(load=8.0, cpus=0)
    assert _posture_findings(load=8.1, cpus=0)["load"]["severity"] == "warn"
    assert _posture_findings(load=8.1, cpus=None)["load"]["severity"] == "warn"


def test_disk_bands_are_the_monitors_90_and_80():
    # >=90 critical (a full volume evicts workloads),
    # >=80 high.
    assert "disk" not in _posture_findings(diskpct=79)
    assert _posture_findings(diskpct=80)["disk"]["severity"] == "high"
    assert _posture_findings(diskpct=89)["disk"]["severity"] == "high"
    assert _posture_findings(diskpct=90)["disk"]["severity"] == "critical"
    assert _posture_findings(diskpct=99)["disk"]["evidence"]["ported_from"] == \
        "fleet monitor rule: disk bands"


def test_the_two_unportable_rules_are_absent_and_say_why():
    # `puv` and `input` are NOT on the heartbeat — observe_ops.go:126-127 says
    # "input and puv stay on observe:host". A rule reading a field that never
    # arrives never fires, and one that silently never fires is worse than one
    # that is absent. So: no rule may name them, and the file must say so.
    ids = {rule_id for rule_id, _, _ in ov.POSTURE_RULES}
    assert "puv" not in ids and "input" not in ids
    src = _read("brain/mastercode/runtime/oversight.py")
    assert "observe_ops.go:126-127" in src or "stay on observe:host" in src, \
        "the reason the two the monitor rules are not ported is not written down"
    # And nothing folds a field the narrower does not carry.
    assert not re.search(r'p\.get\("(puv|input)"\)', src)


def test_monitor_severities_map_without_promotion_or_demotion():
    assert ov.MONITOR_SEVERITY == {"critical": "critical", "high": "high",
                                    "medium": "warn", "low": "notice"}
    assert ov.SEVERITIES == ("notice", "warn", "high", "critical")


# ── 1c. the narrower ────────────────────────────────────────────────────────

def test_narrow_posture_carries_the_whole_contract():
    got = pg.narrow_posture(dict(CLEAN_POSTURE))
    assert set(got) == set(POSTURE_KEYS)
    assert got["pubwall"] == "drop+tdrop" and got["cpus"] == 8


def test_narrow_posture_drops_a_bad_field_and_keeps_the_rest():
    # PER-FIELD, unlike narrow_alerts. The fields are independent probes: a
    # `sec` that arrives as a string says nothing about whether the firewall is
    # fail-open, and dropping the beat over it would hide a real finding behind
    # a cosmetic one.
    got = pg.narrow_posture(dict(CLEAN_POSTURE, sec="lots"))
    assert got["sec"] is None and got["pubwall"] == "drop+tdrop"
    # And a dropped field cannot fire its own rule.
    assert "sec-updates" not in _posture_findings(sec="lots")


def test_narrow_posture_refuses_a_payload_with_nothing_readable():
    for bad in (None, [], "posture", {}, {"unknown": 1}, {"ufw": 5}):
        assert pg.narrow_posture(bad) is None, bad


def test_narrow_posture_keeps_journalds_minus_one():
    # -1 is the box saying it could NOT read a journal timestamp
    # (observe_ops.go). Clamping it to 0 would report a box with dead logging
    # as one that just logged — and 0 <= 1800, so the rule would go quiet.
    assert pg.narrow_posture(dict(CLEAN_POSTURE, jrn=-1))["jrn"] == -1
    assert "journald" not in _posture_findings(jrn=-1)


def test_narrow_posture_never_clamps_a_value_into_a_band():
    # Clamping diskpct to 100 would turn a nonsense 500 into a critical the
    # narrower invented. The ceiling sits well outside every band.
    assert pg.narrow_posture(dict(CLEAN_POSTURE, diskpct=500))["diskpct"] == 500
    assert pg.narrow_posture(dict(CLEAN_POSTURE, sec=True))["sec"] is None, \
        "bool is an int in Python; True must not become a count of 1"


# ── 1d. the beat actually carries it ────────────────────────────────────────

class Pool:
    """Enough asyncpg surface for upsert_heartbeat, recording every statement.

    Everything after the INSERT in upsert_heartbeat is individually
    try/excepted, so a pool that answers nothing else exercises the write.
    """
    def __init__(self, prior=None, returning=None):
        self.sql: list = []
        self.prior = prior
        self.returning = returning or {"token_id": 7, "last_seen_at": None,
                                       "detect": None, "alerts": None}

    async def execute(self, q, *a):
        self.sql.append((q, a))

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "FROM mcp_client_tokens" in q and "operator_kind = 'bastion'" in q:
            return 1
        return None

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "SELECT detect, alerts FROM network_bastion_heartbeat" in q:
            return self.prior
        if "INSERT INTO network_bastion_heartbeat" in q:
            return self.returning
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
    return {"token_id": 7, "hostname": "box-1", "platform": "linux", **extra}


def test_a_heartbeat_with_posture_reaches_the_row():
    # THE DEFECT CLASS, driven end to end: the writer takes a fixed column list
    # and drops what it does not name — how `detect` was lost before v146 and
    # `alerts` before v150. Grepping the source would pass with the narrower
    # deleted, so this reads the statement AND its arguments.
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 3, _beat(posture=dict(CLEAN_POSTURE)))) is not None
    q, args = pool.insert()
    assert "posture" in q, "`posture` is not in the INSERT at all"
    assert "posture         = COALESCE(EXCLUDED.posture" in q, \
        "an update beat must not lose the column the insert path stores"
    stored = [a for a in args if isinstance(a, str) and '"pubwall"' in a]
    assert stored, f"the narrowed posture never reached the arguments: {args!r}"
    assert json.loads(stored[0])["diskpct"] == 31


def test_a_beat_with_no_posture_stores_nothing_and_retracts_nothing():
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 3, _beat())) is not None
    q, args = pool.insert()
    # The column must be THERE and the value absent — asserting only the
    # absence passes vacuously once the column is dropped from the writer.
    assert "posture" in q
    assert not [a for a in args if isinstance(a, str) and '"pubwall"' in a]
    assert "COALESCE(EXCLUDED.posture" in q, \
        "a bastion restart carries no posture; a plain overwrite would read as " \
        "a machine that stopped being looked at"


# ── 2. the pin ──────────────────────────────────────────────────────────────

def _existing(key, severity, ident=1, resolved_at=None):
    return {"id": ident, "key": key, "severity": severity, "resolved_at": resolved_at}


def test_a_second_pass_over_the_same_fact_updates_and_never_piles():
    derived = ov.derive_findings([bastion(posture=dict(CLEAN_POSTURE, diskpct=95))])
    key = "box-1|posture|disk"
    first = ov.merge_findings([], derived, {})
    assert [d["key"] for d in first["insert"]] == [key] and first["update"] == []
    assert first["new"] == 1 and first["updated"] == 0
    second = ov.merge_findings([_existing(key, "critical")], derived, {})
    assert second["insert"] == [] and [u["key"] for u in second["update"]] == [key]
    assert second["new"] == 0 and second["updated"] == 1


def test_first_seen_is_not_in_the_update_statement():
    # The pin, at the only place it can actually be broken. Asserted against
    # the SQL because merge_findings cannot express "do not touch this column".
    sql = ov._UPSERT_SQL
    body = sql.split("DO UPDATE SET", 1)[1]
    assert "first_seen" not in body.replace("first_seen is NOT", ""), \
        "first_seen is being written on update — the ledger's whole value"
    assert "last_seen = now()" in body


def test_severity_may_rise():
    derived = ov.derive_findings([bastion(posture=dict(CLEAN_POSTURE, diskpct=95))])
    merged = ov.merge_findings([_existing("box-1|posture|disk", "high")], derived, {})
    assert merged["update"][0]["severity"] == "critical"


def test_severity_never_falls_inside_a_pinned_row():
    # 85% today, 95% yesterday. The finding is the same finding and the worse
    # number is the true one: an operator who saw `critical` and comes back to
    # `high` reads it as improvement that did not happen.
    derived = ov.derive_findings([bastion(posture=dict(CLEAN_POSTURE, diskpct=85))])
    assert derived[0]["severity"] == "high"
    merged = ov.merge_findings([_existing("box-1|posture|disk", "critical")], derived, {})
    assert merged["update"][0]["severity"] == "critical"
    # And the SQL says it too, so a race between the wake and the 5-minute op
    # cannot lower what the other raised.
    assert "array_position" in ov._UPSERT_SQL and "ELSE oversight_findings.severity" in ov._UPSERT_SQL


def test_worst_prefers_a_known_severity_over_an_unknown_one():
    assert ov.worst("warn", "critical") == "critical"
    assert ov.worst("critical", "warn") == "critical"
    assert ov.worst("nonsense", "notice") == "notice"


def test_a_key_not_seen_this_pass_stays_open():
    # The machine may simply be offline. Closing the row then would turn "we
    # lost sight of this" into "this was fixed".
    merged = ov.merge_findings([_existing("box-1|posture|disk", "high")], [], {})
    assert merged["resolve"] == [] and merged["resolved"] == 0


def test_a_resolved_finding_that_recurs_reopens_the_same_row_and_counts_as_new():
    derived = ov.derive_findings([bastion(posture=dict(CLEAN_POSTURE, diskpct=95))])
    merged = ov.merge_findings(
        [_existing("box-1|posture|disk", "critical", resolved_at="2026-09-15T00:00:00Z")],
        derived, {})
    assert merged["update"][0]["reopened"] is True
    assert merged["new"] == 1, "a recurrence that notified nobody is silence about a recurrence"
    assert "resolved_at = NULL" in ov._UPSERT_SQL
    assert merged["new_keys"] == ["box-1|posture|disk"]


# ── 3. the resolve ──────────────────────────────────────────────────────────

def test_an_alert_the_still_watching_machine_no_longer_names_is_resolved():
    b = bastion(alerts=dict(ALERTS_OK, fingerprints=["fpA"]))
    merged = ov.merge_findings(
        [_existing("box-1|alert|fpA", "critical", 11),
         _existing("box-1|alert|fpGONE", "critical", 12)],
        ov.derive_findings([b]), ov.alert_resolve_scope([b]))
    assert merged["resolve"] == [12] and merged["resolved"] == 1


def test_nothing_resolves_on_an_offline_machine():
    b = bastion(online=False, alerts=dict(ALERTS_OK, fingerprints=[]))
    merged = ov.merge_findings([_existing("box-1|alert|fpGONE", "critical", 12)],
                               ov.derive_findings([b]), ov.alert_resolve_scope([b]))
    assert merged["resolve"] == [], "an offline box's silence is our blindness, not its recovery"


def test_nothing_resolves_when_the_alert_source_is_not_answering():
    # `stale` and `unconfigured` both mean an absent fingerprint proves nothing.
    for alerts in (dict(ALERTS_OK, at="2026-09-16T08:00:00Z", fingerprints=[]), None):
        b = bastion(alerts=alerts)
        scope = ov.alert_resolve_scope([b])
        merged = ov.merge_findings([_existing("box-1|alert|fpGONE", "critical", 12)],
                                   ov.derive_findings([b]), scope)
        assert merged["resolve"] == [], alerts
    assert ov.ALERTS_READING_STATES == ("quiet", "firing", "critical")


def test_only_alert_findings_auto_resolve():
    # A posture finding on a machine whose alert source is answering must not
    # ride the alert rule: a firewall is not fixed by an alert clearing.
    b = bastion(alerts=dict(ALERTS_OK, fingerprints=[]))
    merged = ov.merge_findings([_existing("box-1|posture|pubwall-failopen", "high", 9)],
                               ov.derive_findings([b]), ov.alert_resolve_scope([b]))
    assert merged["resolve"] == []


def test_an_already_resolved_row_is_not_resolved_twice():
    b = bastion(alerts=dict(ALERTS_OK, fingerprints=[]))
    merged = ov.merge_findings(
        [_existing("box-1|alert|fpGONE", "critical", 12, resolved_at="2026-09-15T00:00:00Z")],
        ov.derive_findings([b]), ov.alert_resolve_scope([b]))
    assert merged["resolve"] == []


# ── 4. the wake ─────────────────────────────────────────────────────────────

def test_the_edge_fires_on_zero_to_critical_on_either_plane():
    for plane in ("detect", "alerts"):
        assert topology.rising_critical_edge({plane: {"critical": 0}},
                                             {plane: {"critical": 1}}), plane
        # Never reported -> critical is a rising edge too. The collapse is only
        # ever in the direction of firing more, never of calling something calm.
        assert topology.rising_critical_edge({}, {plane: {"critical": 1}}), plane


def test_the_edge_does_not_fire_on_critical_to_critical():
    # The whole reason the edge exists. A wake on every beat of an ongoing
    # incident is the ingest running once a minute per machine, for ever.
    assert not topology.rising_critical_edge({"alerts": {"critical": 2}},
                                             {"alerts": {"critical": 3}})
    assert not topology.rising_critical_edge({"alerts": {"critical": 1}},
                                             {"alerts": {"critical": 1}})


def test_the_edge_does_not_fire_on_a_falling_or_flat_beat():
    assert not topology.rising_critical_edge({"alerts": {"critical": 2}},
                                             {"alerts": {"critical": 0}})
    assert not topology.rising_critical_edge(None, None)
    assert not topology.rising_critical_edge({}, {"alerts": {"critical": 0}})


def test_the_edge_reads_the_post_coalesce_row_so_an_omitted_key_is_not_an_edge():
    # The beat's RETURNING is post-COALESCE, so a bastion that omits `alerts`
    # compares the KEPT value against itself. Reading the omission as a drop to
    # zero would make the next beat look like a fresh rise, every time.
    prior = {"alerts": {"critical": 1}}
    current = {"alerts": {"critical": 1}}          # COALESCE kept it
    assert not topology.rising_critical_edge(prior, current)


def test_a_malformed_critical_count_is_read_as_no_criticals_not_as_an_edge():
    for bad in (True, -1, "2", None, {}):
        assert topology._critical_count({"critical": bad}) == 0, bad


def test_the_debounce_admits_one_wake_per_window_per_entity():
    topology._oversight_wake_last.clear()
    assert topology._wake_debounce_ok(3, 1000.0) is True
    assert topology._wake_debounce_ok(3, 1000.0 + 59.0) is False
    # Another entity is not throttled by this one's incident.
    assert topology._wake_debounce_ok(4, 1000.0 + 1.0) is True
    # Past the window, the same entity may wake again.
    assert topology._wake_debounce_ok(3, 1000.0 + topology.OVERSIGHT_WAKE_DEBOUNCE_S) is True
    topology._oversight_wake_last.clear()


def test_the_debounce_window_is_the_heartbeat_period():
    # Not a number picked for its own sake: one wake per beat at most.
    assert topology.OVERSIGHT_WAKE_DEBOUNCE_S == 60


def test_the_wake_fires_exactly_once_per_edge_and_is_never_awaited():
    topology._oversight_wake_last.clear()
    calls = []

    async def _fake(pool, entity_id):
        calls.append(entity_id)
        return {}

    real = topology._oversight_wake
    topology._oversight_wake = _fake
    try:
        async def _drive():
            low = {"alerts": {"critical": 0}}
            high = {"alerts": {"critical": 1}}
            higher = {"alerts": {"critical": 5}}
            fired = [topology._maybe_wake_oversight(None, 3, low, high),
                     topology._maybe_wake_oversight(None, 3, high, higher)]
            await asyncio.sleep(0)
            return fired
        fired = _run(_drive())
    finally:
        topology._oversight_wake = real
        topology._oversight_wake_last.clear()
    assert fired == [True, False], "the second beat was still critical, not a new edge"
    assert calls == [3], f"the wake ran {len(calls)} times for one edge"


def test_the_wake_goes_through_run_direct_op_and_not_the_tool_function():
    # The fitness gate and the CEIGAS caps gate both live on that path
    # (offline_task_runner._run_op says so). Calling _exec_oversight_ingest
    # directly would skip both, which is the shortcut this asserts against.
    src = _read("server/network/topology.py")
    assert "run_direct_op(\"oversight_ingest\"" in src
    assert "_exec_oversight_ingest" not in src


def test_the_wake_notifies_at_critical_only():
    src = _read("server/network/topology.py")
    assert 'min_severity="critical"' in src, \
        "the wake must not send the high-severity notification; that is the " \
        "scheduled pass's"


# ── 5. the notification ─────────────────────────────────────────────────────

class InboxPool:
    def __init__(self):
        self.rows = []


def _notify(result, min_severity):
    sent = []

    async def _append(payload):
        sent.append(payload)
        return {"ok": True}

    import server.bridge.memory_handlers as mh
    real = mh.handle_inbox_append
    mh.handle_inbox_append = _append
    try:
        out = _run(ov.notify_new_findings(None, 3, result, min_severity=min_severity))
    finally:
        mh.handle_inbox_append = real
    return out, sent


def test_one_inbox_row_for_new_criticals_naming_identifiers_and_a_link():
    out, sent = _notify({"new_by_severity": {"critical": 2, "high": 1},
                         "new_keys": ["box-1|alert|fpA", "box-1|posture|disk"]},
                        "high")
    assert out["sent"] is True and len(sent) == 1
    row = sent[0]
    assert row["message_kind"] == "system_notification"
    assert row["payload"]["event"] == ov.EVENT_CRITICAL == "oversight_critical"
    assert row["payload"]["count"] == 2
    assert row["payload"]["keys"] == ["box-1|alert|fpA", "box-1|posture|disk"]
    # The link OPENS THE APP (the deep-link contract), relative to the host;
    # reach-out makes it absolute when it leaves the host.
    assert row["payload"]["link"] == "/?open=oversight&view=findings"
    # IDENTIFIERS AND A LINK, NEVER CONTENT. reach-out dispatches
    # this over email, so a machine's alert bodies in here would post a
    # customer's infrastructure detail through a mail provider.
    assert "evidence" not in row["payload"]
    assert "fail-open" not in row["body"] and "load1" not in row["body"]


def test_a_critical_beats_a_high_in_the_same_pass():
    out, sent = _notify({"new_by_severity": {"critical": 1, "high": 4},
                         "new_keys": ["k"]}, "high")
    assert sent[0]["payload"]["event"] == ov.EVENT_CRITICAL


def test_the_scheduled_pass_reports_a_high_and_the_wake_does_not():
    result = {"new_by_severity": {"critical": 0, "high": 3}, "new_keys": ["k"]}
    out, sent = _notify(result, "high")
    assert sent and sent[0]["payload"]["event"] == ov.EVENT_HIGH == "oversight_high"
    out, sent = _notify(result, "critical")
    assert out["sent"] is False and sent == []


def test_nothing_is_said_when_nothing_became_true():
    # A standing op that fires every five minutes and speaks every time is
    # noise, and noise is how a user learns to stop reading.
    out, sent = _notify({"new_by_severity": {s: 0 for s in ov.SEVERITIES}}, "high")
    assert out["sent"] is False and sent == []
    # Still-open findings are not new findings.
    out, sent = _notify({"new_by_severity": {"critical": 0},
                         "by_severity": {"critical": 9}}, "high")
    assert out["sent"] is False and sent == []


def test_the_ingest_itself_writes_no_inbox_row():
    src = _read("brain/mastercode/runtime/oversight.py")
    body = src.split("async def run_oversight_ingest", 1)[1].split("\nasync def ", 1)[0]
    assert "handle_inbox_append" not in body, \
        "the fold must be runnable twice without a message each time"


# ── 6. the standing task ────────────────────────────────────────────────────

class SeedPool:
    """Counts what exists; records what gets appended."""
    def __init__(self):
        self.count = 0
        self.appends = []

    async def fetchval(self, q, *a):
        if "FROM calendar_events" in q:
            return self.count
        return None


def _seed(pool):
    import server.bridge.memory_handlers as mh
    real = mh.handle_calendar_append

    async def _append(payload):
        pool.appends.append(payload)
        pool.count += 1
        return {"ok": True, "event_id": "e1", "event_at": payload["event_at"]}

    mh.handle_calendar_append = _append
    try:
        return _run(ov.ensure_oversight_ingest_task(pool, 3))
    finally:
        mh.handle_calendar_append = real


def test_the_standing_task_is_seeded_once_and_is_idempotent_across_two_boots():
    pool = SeedPool()
    first = _seed(pool)
    assert first["seeded"] is True and len(pool.appends) == 1
    second = _seed(pool)
    assert second["seeded"] is False and len(pool.appends) == 1, \
        "a second boot wrote a second standing task"
    assert "already present" in second["reason"]


def test_the_seeded_task_is_the_op_lane_every_five_minutes():
    pool = SeedPool()
    _seed(pool)
    row = pool.appends[0]
    assert row["event_type"] == "scheduled_task"
    # args carries the `scheduled` marker: it is what tells the
    # store-read gate this is the 5-minute pass rather than a wake or a
    # hand-run, both of which read the machine records every time. A
    # payload.prompt here would still be a model turn; the marker is not one.
    assert row["payload"]["op"] == {"tool": "oversight_ingest", "args": {"scheduled": True}}, \
        "the op lane is payload.op — a payload.prompt here would be a model turn"
    assert row["recurrence"] == "every:300"
    # The runner's own grammar, so the row is not merely well-intentioned.
    from mastercode.brain.offline_task_runner import _recurrence_seconds
    assert _recurrence_seconds(row["recurrence"]) == 300


def test_the_fifteen_minute_model_turn_is_not_seeded():
    # A scheduled MODEL turn every fifteen minutes is a spend decision after a
    # measured day. Nothing may create it yet, and the check is structural: no seeder in the tree
    # writes a prompt-lane task for this lane.
    src = _read("brain/mastercode/runtime/oversight.py") + _read("server/main.py")
    assert '"prompt"' not in _read("brain/mastercode/runtime/oversight.py")
    assert "every:900" not in src


# ── 7. registration, in every place the gate requires ───────────────────────

def test_oversight_ingest_is_registered_everywhere():
    assert "oversight_ingest" in dops.DIRECT_OP_TOOLS, "not a direct op"
    assert dops.is_unattended_op_tool("oversight_ingest"), "not schedulable"
    assert "oversight_ingest" in reg.PUBLIC_TOOL_SCHEMAS
    assert reg.PUBLIC_TOOL_SCHEMAS["oversight_ingest"]["function"]["name"] == "oversight_ingest"
    assert "oversight_ingest" in reg.PUBLIC_META_TOOL_NAMES, \
        "the executor-set entry is the line three tools shipped without in 2026-09"
    assert "oversight_ingest" in caps.META_TOOL_CAPS, "default-deny at call"
    # A pure-read bit set would make check_tool_lanes demand a read lane for a
    # tool that writes rows.
    assert caps.META_TOOL_CAPS["oversight_ingest"]["required_bits"] & ~(1 | 2), \
        "a write bit is missing — this tool writes the ledger"
    src = _read("brain/mastercode/runtime/tools.py")
    assert 'elif name == "oversight_ingest":' in src
    assert "from mastercode.runtime.oversight import _exec_oversight_ingest" in src
    from mastercode.runtime.tool_lanes import lane_for
    assert lane_for("oversight_ingest", reg.PUBLIC_TOOL_SCHEMAS["oversight_ingest"]) == "operate"


def test_wake_is_an_internal_arg_and_stays_off_the_wire():
    # The executor accepts `wake`; the schema does not advertise it, because
    # only the heartbeat passes it and it SUPPRESSES a notification — a knob a
    # host could reach for and be quietly worse off. Safe to omit because args
    # are not filtered against the schema: _execute_meta_tool reads the schema
    # only for the surface gate and hands `args` through untouched.
    params = reg.PUBLIC_TOOL_SCHEMAS["oversight_ingest"]["function"]["parameters"]
    assert params.get("properties") == {}, \
        "an internal-only flag is being advertised to the host"
    tools_src = _read("brain/mastercode/runtime/tools.py")
    gate = tools_src.split("async def _execute_meta_tool", 1)[1].split("_authorize_meta_tool", 1)[0]
    assert "_find_tool_schema" in gate and "_surface_allows" in gate, \
        "the schema's only use before dispatch has changed — re-check whether " \
        "an unadvertised arg still reaches the executor"
    # And the executor still honours it.
    src = _read("brain/mastercode/runtime/oversight.py")
    assert 'a.get("wake")' in src


def test_the_skill_tells_the_host_what_the_ledger_will_not_say():
    skill = _read("default-skills/network.md")
    assert "oversight_ingest" in skill.split("---", 2)[1], \
        "the tool is not in the skill's allowed-tools"
    section = skill[skill.index("## oversight_ingest"):]
    assert "never writes an all-clear" in section
    assert "never closes a finding because the machine went quiet" in section
    assert "ported" in section


def test_the_scopes_exist_and_stay_out_of_the_community_allowlist():
    from server.applications import scopes as sc
    from server.applications.build_contract import COMMUNITY_ALLOWED_SCOPES
    text = _read("server/applications/scopes.py")
    for scope in ("read:oversight", "write:oversight"):
        assert f'"{scope}"' in text, scope
        assert scope not in COMMUNITY_ALLOWED_SCOPES, \
            f"{scope} names a customer's machines — an untrusted app must not be " \
            f"able to request it"
    assert hasattr(sc, "__file__")


def test_the_routes_are_gated_and_the_vocabulary_matches_the_ledger():
    from server.routes import oversight as routes
    src = _read("server/routes/oversight.py")
    assert routes.SEVERITIES == ov.SEVERITIES, \
        "the route's mirrored vocabulary has drifted from the ledger's"
    # EVERY route is gated, read by GET and write by POST, read off the router
    # itself rather than counted in the source: requires_permission tags its
    # dependency with `_app_scope` (applications/middleware.py), so a route
    # added without a gate — or with the wrong one — fails here by name.
    seen = {}
    for r in routes.router.routes:
        scopes = [getattr(d.call, "_app_scope", None) for d in r.dependant.dependencies]
        scopes = [s for s in scopes if s]
        want = "write:oversight" if "POST" in r.methods else "read:oversight"
        assert scopes == [want], f"{sorted(r.methods)} {r.path} is gated by {scopes}, want [{want!r}]"
        seen[r.path] = want
    assert seen == {
        "/api/oversight/findings": "read:oversight",
        "/api/oversight/findings/{finding_id}": "read:oversight",
        "/api/oversight/findings/{finding_id}/resolve": "write:oversight",
        "/api/oversight/machines": "read:oversight",
    }, f"the route set changed: {seen}"
    assert src.count('requires_permission("write:oversight")') == 1
    # No create, no edit: every row is folded, and a second writer would be a
    # second unaudited source for the same table.
    assert "INSERT INTO oversight_findings" not in src
    assert "DELETE FROM" not in src
    main = _read("server/main.py")
    assert "app.include_router(oversight_routes.router)" in main


def test_the_migration_declares_the_table_and_the_runtime_does_not():
    # DDL placement: migrations only, and every
    # statement of the permitted shapes.
    sql = _read("migrations/v156_oversight_findings.sql")
    assert "CREATE TABLE IF NOT EXISTS oversight_findings" in sql
    assert "ADD COLUMN IF NOT EXISTS posture JSONB" in sql
    assert "CREATE INDEX IF NOT EXISTS oversight_findings_open_severity_idx" in sql
    assert "ON oversight_findings (resolved_at, severity)" in sql
    for forbidden in ("DROP ", "SET NOT NULL", "ALTER COLUMN"):
        assert forbidden not in sql, f"{forbidden} is forbidden (ON_ERROR_STOP=1 aborts boot)"
    # The guard, and the COMMENTs inside it — v150's header records the boot
    # this exact mistake cost on a real cseries-postgres:smoke.
    assert "to_regclass('public.network_bastion_heartbeat') IS NULL" in sql
    guarded = sql.split("DO $$", 1)[1]
    assert "COMMENT ON COLUMN network_bastion_heartbeat.posture" in guarded
    for rel in ("brain/mastercode/runtime/oversight.py", "server/routes/oversight.py"):
        assert "CREATE TABLE" not in _read(rel), rel

def test_an_accounting_that_predates_a_class_is_one_warn_row():
    """Only the cause a retake fixes raises a row; a platform that cannot read
    the class, or a reader that failed this pass, raises none."""
    predates = {"baseline": "ok", "critical": 0, "coverage": "processes=all,schedulers=all",
                "skipped": ["listener", "identity"]}
    rows = by_kind(ov.derive_findings([bastion(detect=predates)]), "detect-coverage")
    assert [f["key"] for f in rows] == ["box-1|detect-coverage|retake"], rows
    assert rows[0]["severity"] == "warn"
    # The row says what to do, in the words the reading uses.
    row = rows[0]
    blob = json.dumps(row, ensure_ascii=False)
    assert "observe:baseline --force" in blob, blob
    assert "listening ports" in blob and "accounts" in blob, blob
    # An accounting taken while a reader failed on a platform that CAN read the
    # class (`=unread`) is fixed by a retake too, so it raises the same row.
    missed = {"baseline": "ok", "critical": 0, "skipped": ["listener"],
              "coverage": "processes=all,schedulers=all,listeners=unread,identities=login-shells"}
    rows = by_kind(ov.derive_findings([bastion(detect=missed)]), "detect-coverage")
    assert [f["key"] for f in rows] == ["box-1|detect-coverage|retake"], rows
    for coverage in ("processes=all,schedulers=all,listeners=unsupported,identities=unsupported",
                     "processes=all,schedulers=all,listeners=all,identities=login-shells"):
        d = {"baseline": "ok", "critical": 0, "coverage": coverage, "skipped": ["listener"]}
        assert by_kind(ov.derive_findings([bastion(detect=d)]), "detect-coverage") == [], coverage


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
