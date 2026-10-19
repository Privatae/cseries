"""runtime/oversight.py — the host's findings ledger and its standing op.

WHY A LEDGER AND NOT A FEED. Every machine the host manages already reports
what is wrong with it — the detection plane's counts (v146), its alert source's
counts (v150), and its own host posture (v153) all ride the heartbeat.
All three are PRESENT TENSE: a beat says what is true now and the next beat
overwrites it. So the host can answer "is anything wrong" and cannot answer the
question an operator actually asks, which is "how long has this been wrong, and
is it the same thing I saw yesterday". `first_seen` is the entire difference,
and it is the one field no heartbeat can carry.

    A pinned finding is ONE ROW with a moving `last_seen`, never a pile.

WHAT FOLDS IN, AND WITH NO MODEL ANYWHERE (no model in the sensing path).
Five folds, all deterministic. The first four read
summaries the beat already carries:

  a. ALERTS     one finding per fingerprint in the machine's alert summary, plus
                one when the alert source itself has gone stale.
  b. DETECT     one finding per (machine, level) with a non-zero count, plus one
                when the detection plane is in a state that is neither a clean
                result nor a reported difference.
  c. POSTURE    the HOST_PROBE threshold rules from the operator's fleet
                collector (`build()`), ported verbatim with the line each came
                from. NO NUMBER IN THIS FILE IS NEW: every threshold is the
                collector's, and the two rules whose
                inputs are not on the heartbeat are NOT ported rather than
                re-derived (see POSTURE_RULES).
  d. OFFLINE    a bastion past ONLINE_WINDOW_SECONDS.

The fifth reads the machines' own records rather than the beat:

  e. STORE      one finding per line of each box's findings journal, read
                through the governed DCN range-read since a byte cursor
                (runtime/oversight_store.py). This is the only fold
                that produces a row PER FINDING with the box's evidence; the
                other four produce rows per (machine, level) from counts. It is
                additive — a store that is unreachable or revoked contributes
                problems and no findings, and the beat-derived rows are written
                either way.

WHAT IS NEVER WRITTEN: an all-clear. The ledger holds findings only. That is
what makes "nobody read this machine's posture" safe to represent as silence —
a field the box did not report produces no row, and there is no green row it
could be mistaken for. Everywhere the fleet's quiet-versus-blind problem is
handled in WORDS (_bastion_alerts_state, _bastion_detect_state), it is handled
here by the shape.

WHAT IS NEVER AUTO-RESOLVED: almost everything. A key not seen this pass stays
open, because the overwhelmingly common reason a machine stops reporting a
problem is that the machine stopped reporting. Closing the row then would turn
"we lost sight of this" into "this was fixed". The single exception is stated at
`alert_resolve_scope` and it is narrow: an alert fingerprint absent from the
CURRENT summary of an ONLINE machine whose alert source is answering.

HOW IT RUNS. As a standing op on the calendar's op lane
(offline_task_runner._run_op -> direct_ops.run_direct_op), every 5 minutes, no
model and no tokens, seeded at boot by `ensure_oversight_ingest_task` the way
the weekly changelog is (runtime/host_changelog.py:489). Plus the wake:
topology._maybe_wake_oversight fires the same op through the same path the
instant a beat takes a machine from no criticals to at least one.

WHAT IS DELIBERATELY NOT HERE. A 15-minute scheduled MODEL turn — the one
that would correlate across machines and write recommendations. That is a
spend decision the operator makes after a measured day, and the
`correlation` source value in the migration is reserved for it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("cseries.runtime.oversight")

OVERSIGHT_TOOL = "oversight_ingest"
OVERSIGHT_TASK_TITLE = "Oversight — fold what the machines report into the ledger"
# Every 5 minutes, as the plan specifies. The runner's recurrence grammar is
# RRULE-lite and `every:<seconds>` is its escape hatch
# (entity_clock._recurrence_seconds).
OVERSIGHT_RECURRENCE = "every:300"
OVERSIGHT_PERIOD_S = 300

# inbox_messages.payload.event — the two names the reach-out lane will
# subscribe to. They are the contract with that build, so they are constants
# here rather than literals at the write site.
EVENT_CRITICAL = "oversight_critical"
EVENT_HIGH = "oversight_high"

# Worst last. Used for every "may rise, never fall" comparison, and mirrored in
# the SQL upsert so a race between the wake and the 5-minute op cannot lower a
# severity either.
SEVERITIES = ("notice", "warn", "high", "critical")

# The fleet collector speaks critical/high/medium/low; this ledger speaks
# critical/high/warn/notice. The mapping is stated once, here, so a ported rule
# can keep the collector's own severity word in its citation and still land in this
# vocabulary. It is a rename, not a re-judgement: nothing is promoted or
# demoted on the way across.
MONITOR_SEVERITY = {"critical": "critical", "high": "high",
                     "medium": "warn", "low": "notice"}

# Detect states that are NOT worth a finding of their own. `clean` and
# `findings` are the two readings where the plane did its job (the counts fold
# separately); `unreported` is every bastion that predates the plane or has not
# finished its first pass, which today is most of the fleet — a finding per box
# would be a ledger full of rows nobody can act on except by updating bastions,
# which the Network app already says. Every other state — no-baseline, unusable,
# untrusted, stale, unknown-state — means nothing was compared and somebody has
# to do something about it (default-skills/network.md:76-117).
# `partial` compared what it could and found nothing there, with a class left
# out: a reader that failed one pass, or an accounting that predates the class.
# The reading names it and says what fixes it; a pinned high row for every pass
# where `ss` failed once would be a ledger nobody can act on.
DETECT_STATES_WITHOUT_FINDING = ("clean", "findings", "unreported", "partial")

# Alert-plane states in which the box is genuinely reading its source right now.
# Only these make an absent fingerprint mean "gone" rather than "unseen".
ALERTS_READING_STATES = ("quiet", "firing", "critical")

_KEY_SEP = "|"
_EVIDENCE_MAX = 8_000          # one row's evidence, serialized
_INBOX_IDENT_MAX = 12          # identifiers named in one notification


def severity_rank(s: str) -> int:
    try:
        return SEVERITIES.index(str(s or "").strip().lower())
    except ValueError:
        return -1


def worst(a: str, b: str) -> str:
    """The higher of two severities. Unknown values lose to known ones."""
    return a if severity_rank(a) >= severity_rank(b) else b


def finding_key(machine: str, kind: str, fingerprint: str = "") -> str:
    """(machine, kind, fingerprint), joined — the ledger's UNIQUE column.

    The table has no entity column and needs none: this is the entity's own pod
    database, one entity per pod, so "unique per entity" is unique per table.

    The machine goes FIRST because every human read of this ledger is
    machine-first, and because the alert auto-resolve scans by
    `<machine>|alert|` prefix — a key ordered the other way would make that a
    scan of the whole table.
    """
    return _KEY_SEP.join((str(machine or "?"), str(kind), str(fingerprint or "")))


def machine_of(bastion: dict) -> str:
    """The name this ledger files a machine under.

    hostname first (what the box calls itself, and what every other operator
    tool prints), then the pairing's name, then the token id — never blank, or
    two unrelated machines would share a key.
    """
    b = bastion or {}
    for candidate in (b.get("hostname"), b.get("name")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:200]
    return f"token:{b.get('id')}"


# ── (c) the posture rules, ported from the fleet collector ──────────────────
#
# Source: the operator's fleet collector, function `build()`. It asks these
# questions over SSH about the fleet's own machines; the bastion answers the
# same questions about the user's, on its own box, and folds the answers onto
# the beat (the gateway's client-go/observe_ops.go `heartbeatPosture`).
# Same questions, same thresholds, a different transport — so the rules are
# PORTED rather than re-derived, and each carries the line it came from.
#
# TWO MONITOR RULES ARE NOT PORTED, and the reason is the transport, not
# judgement:
#   - `puv` (post-upgrade verify, collect.py:1192-1197) and `input` (the
#     iptables INPUT policy) are NOT on the heartbeat. observe_ops.go:126-127
#     says so in as many words: "input and puv stay on observe:host". A rule
#     reading a field that never arrives is a rule that never fires, and one
#     that silently never fires is worse than one that is absent.
#   - the collector's f2b rule additionally excludes one host by name. That is
#     an exclusion for the operator's own fleet; the machines here are the user's, so it does not travel.
#
# Each rule: a stable id (it becomes the key's fingerprint, so renaming one
# orphans its history), the severity in THIS vocabulary, and the citation.

def _posture_journald(p: dict):
    # collect.py:1187-1191 — `if jrn > 1800` -> high. "journald died silently
    # and the box ran 24h with no logs" (collect.py:750).
    # 1800 s = 30 min = two of the collector's passes.
    jrn = p.get("jrn")
    if not isinstance(jrn, int) or jrn <= 1800:
        return None
    return (MONITOR_SEVERITY["high"],
            f"journald has been silent for {jrn // 60} min",
            "No journal line since then — logging is dead while the box runs. "
            "`systemctl restart systemd-journald` on the machine, then find what "
            "killed it.",
            {"jrn_s": jrn, "threshold_s": 1800})


def _posture_f2b(p: dict):
    # collect.py:1209-1213 — fail2ban not active AND tcp/22 not scoped -> medium.
    # The ssh22 gate is the whole rule: on a box whose perimeter allow-lists 22
    # to known addresses no attacker packet reaches sshd, so fail2ban has
    # nothing to read. Measured across the fleet: without the gate this fired
    # five mediums that no action could clear.
    f2b, ssh22 = p.get("f2b"), p.get("ssh22")
    if f2b is None or f2b == "active":
        return None
    if ssh22 == "scoped":
        return None
    return (MONITOR_SEVERITY["medium"],
            "fail2ban is not active and tcp/22 is not source-restricted",
            f"state={f2b} · tcp/22 is {ssh22 or 'unknown'} on this machine — sshd "
            "is reachable beyond any allowlist, so a brute-force source has "
            "something to talk to. Either scope the rule or start fail2ban.",
            {"f2b": f2b, "ssh22": ssh22})


def _posture_pubwall(p: dict):
    # collect.py:1223-1231 — pubwall policy startswith "accept" -> high. A chain
    # whose POLICY is accept survives only on its trailing drop rule; lose that
    # one rule and the box is open to the internet with no signal.
    pw = p.get("pubwall")
    if not isinstance(pw, str) or not pw.startswith("accept"):
        return None
    return (MONITOR_SEVERITY["high"],
            "the pubwall input chain is fail-open",
            f"policy={pw} — the chain relies entirely on its trailing drop rule; "
            "if that rule is lost the machine accepts everything. Flip the chain "
            "to `policy drop`, keeping the trailing drop.",
            {"pubwall": pw})


def _posture_sec(p: dict):
    # collect.py:1232-1233 — `int(sec) > 0` -> medium, "apt security backlog".
    sec = p.get("sec")
    if not isinstance(sec, int) or sec <= 0:
        return None
    return (MONITOR_SEVERITY["medium"],
            f"{sec} pending security update(s)",
            "apt security backlog on this machine.",
            {"sec": sec})


def _posture_reboot(p: dict):
    # collect.py:1236-1237 — reboot == "yes" -> low.
    if p.get("reboot") != "yes":
        return None
    return (MONITOR_SEVERITY["low"],
            "a reboot is pending",
            "kernel/libc update pending activation.",
            {"reboot": "yes"})


def _posture_load(p: dict):
    # collect.py:1243-1252 — per-core, and the fallback matters. cpus > 0:
    # `load1 >= cpus` (a full run queue, the saturation line every per-core
    # check is built around). cpus absent or 0: the old flat `load1 > 8`. Both
    # -> medium. The flat form "fired permanently on the big boxes and stayed
    # silent on the small ones", which is why the per-core form exists — but it
    # is still the right answer when the core count is unknown.
    load1, cpus = p.get("load"), p.get("cpus")
    if not isinstance(load1, (int, float)):
        return None
    if isinstance(cpus, int) and cpus > 0:
        if load1 < cpus:
            return None
        detail = f"load1={load1} on {cpus} cores ({load1 / cpus:.0%} of one-per-core)"
    else:
        if load1 <= 8:
            return None
        detail = f"load1={load1} (core count unknown)"
    return (MONITOR_SEVERITY["medium"], "high load", detail,
            {"load1": load1, "cpus": cpus})


def _posture_disk(p: dict):
    # collect.py:1492-1502 — diskpct >= 90 -> critical ("a full volume evicts
    # workloads; this once took billingd/communityd/authentik down"),
    # >= 80 -> high ("approaching the level where writes start failing").
    pct = p.get("diskpct")
    if not isinstance(pct, int):
        return None
    if pct >= 90:
        return (MONITOR_SEVERITY["critical"],
                f"root filesystem {pct}% full",
                "a full volume evicts workloads.", {"diskpct": pct, "band": 90})
    if pct >= 80:
        return (MONITOR_SEVERITY["high"],
                f"root filesystem {pct}% full",
                "approaching the level where writes start failing.",
                {"diskpct": pct, "band": 80})
    return None


# id -> (function, the fleet monitor rule it was ported from). The citation is
# carried in the DATA, not only in a comment, so it lands in `evidence` on every
# row and an operator reading the ledger can see where the number came from
# without opening this file.
POSTURE_RULES = (
    ("journald", _posture_journald, "fleet monitor rule: journald"),
    ("f2b", _posture_f2b, "fleet monitor rule: fail2ban"),
    ("pubwall-failopen", _posture_pubwall, "fleet monitor rule: public wall fail-open"),
    ("sec-updates", _posture_sec, "fleet monitor rule: security updates"),
    ("reboot", _posture_reboot, "fleet monitor rule: reboot required"),
    ("load", _posture_load, "fleet monitor rule: load per core"),
    ("disk", _posture_disk, "fleet monitor rule: disk bands"),
)


# ── the folds ───────────────────────────────────────────────────────────────

def _finding(machine, kind, fingerprint, severity, source, title, recommendation,
             evidence) -> dict:
    ev = dict(evidence or {})
    ev["title"] = title
    blob = json.dumps(ev, default=str, ensure_ascii=False)
    if len(blob) > _EVIDENCE_MAX:
        ev = {"title": title, "truncated": True}
    return {
        "key": finding_key(machine, kind, fingerprint),
        "kind": kind,
        "machine": machine,
        "severity": severity,
        "source": source,
        "title": title,
        "recommendation": recommendation,
        "evidence": ev,
    }


def _alert_severity(counts: dict) -> str:
    """The severity a machine's alert fingerprints are filed at.

    PER MACHINE, NOT PER FINGERPRINT, because per-fingerprint severity is not on
    the beat: the summary carries three COUNTS and up to twenty identifiers, and
    which identifier is the critical one is not in it (narrow_alerts, and the
    v150 header). So every fingerprint from a machine with a critical firing is
    filed critical. That over-states some rows and under-states none, which is
    the only direction that is safe here — and `observe:alerts` on the box is
    what turns an identifier into the alert it names.
    """
    if int(counts.get("critical") or 0) > 0:
        return "critical"
    if int(counts.get("warning") or 0) > 0:
        return "warn"
    # Firing, but the box classified none of them critical or warning. A real
    # state (an Alertmanager whose rules carry no severity label) and not an
    # error, so it is filed at the bottom of the vocabulary rather than dropped.
    return "notice"


def _fold_alerts(b: dict, machine: str, alerts_state: str, alerts_reason: str,
                 counts, out: list) -> None:
    if alerts_state == "stale":
        # The box is heartbeating and its alert source has stopped answering.
        # This is an ALERTING OUTAGE on that machine, not its alert state, and
        # `high` is where it belongs: nobody is watching a live machine.
        out.append(_finding(
            machine, "alert-source", "stale", "high", "alert",
            "alert source stale",
            "The machine is still heartbeating but stopped reading its alert "
            "source. Treat this as an alerting outage on that box: check the "
            "alert endpoint named in its /etc/privatae-bastion/observe-endpoints.json.",
            {"alerts_state": alerts_state, "alerts_reason": alerts_reason,
             "alerts": counts}))
    if not isinstance(counts, dict):
        return
    sev = _alert_severity(counts)
    for fp in (counts.get("fingerprints") or []):
        out.append(_finding(
            machine, "alert", fp, sev, "alert",
            f"alert {fp} firing",
            f"`observe:alerts` on {machine} lists the alert this fingerprint "
            "names; the alert rows stay on the box.",
            {"alerts_state": alerts_state, "alerts": counts, "fingerprint": fp}))


def _fold_detect(b: dict, machine: str, detect_state: str, detect_reason: str,
                 counts, out: list) -> None:
    if detect_state not in DETECT_STATES_WITHOUT_FINDING:
        out.append(_finding(
            machine, "detect-state", detect_state, "high", "heartbeat",
            f"detection plane state: {detect_state}",
            "Nothing on this machine is being compared against a known-good "
            "accounting. `detect_reason` carries the remediation and it differs "
            "between the states — read it rather than assuming.",
            {"detect_state": detect_state, "detect_reason": detect_reason,
             "detect": counts}))
    if not isinstance(counts, dict):
        # COUNTS ARE None UNLESS A COMPARISON HAPPENED (_detect_counts). There
        # is nothing to fold, and nothing that could be read as a zero.
        return
    # A CLASS THE ACCOUNTING PREDATES is the one cause of a skipped class that
    # somebody can fix, and it does not fix itself: every Linux machine whose
    # accounting was taken before listeners and accounts were compared reads
    # `partial` until it is retaken. One warn row per machine says so. A class
    # the platform cannot read, or a reader that failed this pass, raises
    # nothing — the first cannot be fixed here and the second clears itself.
    retake = counts.get("retake_adds") or []
    if retake:
        from mastercode.runtime.tools import _DETECT_CLASS_WORDS
        names = " and ".join(_DETECT_CLASS_WORDS.get(c, c) for c in retake)
        out.append(_finding(
            machine, "detect-coverage", "retake", "warn", "heartbeat",
            f"accounting does not hold {names}",
            f"The accounting on {machine} does not hold {names}: it was taken before "
            "they were compared, or while the box could not read them. The detection "
            "plane is not watching them there. Retake it on the machine "
            "(`observe:baseline --force`); it prints anything it still cannot read.",
            {"retake_adds": retake, "detect": counts}))
    for level in SEVERITIES:
        n = counts.get(level)
        if not isinstance(n, int) or n <= 0:
            continue
        out.append(_finding(
            machine, "detect", level, level, "heartbeat",
            f"{n} {level} difference(s) against this machine's accounting",
            f"`observe:delta` on {machine} lists them; the findings themselves "
            "stay on the box.",
            {"detect": counts, "level": level, "count": n,
             "baseline": counts.get("baseline")}))


def _fold_posture(b: dict, machine: str, out: list) -> None:
    p = b.get("posture")
    if not isinstance(p, dict):
        # NULL = this box never reported a posture. No row, and therefore no
        # green row: the ledger's silence is the only honest thing to say.
        return
    for rule_id, fn, citation in POSTURE_RULES:
        hit = fn(p)
        if hit is None:
            continue
        severity, title, recommendation, ev = hit
        ev = dict(ev)
        ev["ported_from"] = citation
        out.append(_finding(machine, "posture", rule_id, severity, "heartbeat",
                            title, recommendation, ev))


def _fold_record_holes(b: dict, machine: str, out: list) -> None:
    """One row per machine whose own record has holes (v163).

    The box counts, per shelf, every OBSERVE result it could not write into
    its machine store — a full disk, a sealed day-file — and sends the counts
    on the beat so a record with holes is never mistaken for a quiet machine. The
    host had no reader for it. This is the reader: ONE pinned row per machine, keyed on the
    machine alone, the shelves and their counts in the evidence, so a machine
    losing results for a week is one row with a moving last_seen rather than
    a pile. HIGH, because the record is the accounting half of AAA and a hole
    in it is a hole in what the host can ever know about that machine.

    `{}` means whole (the box sends an empty object when nothing was lost) and
    None means not reported; neither produces a row — and no green row either
    could be mistaken for, per this file's own rule. The counter covers the
    observe shelves only; a refused findings line is re-offered the next
    unsealed day rather than counted.
    """
    holes = b.get("record_dropped")
    if not isinstance(holes, dict) or not holes:
        return
    total = sum(int(n) for n in holes.values() if isinstance(n, int))
    shelves = ", ".join(f"{k} ({n})" for k, n in sorted(holes.items()))
    out.append(_finding(
        machine, "record-holes", "", "high", "heartbeat",
        f"{machine}'s own record has holes: {total} result(s) could not be written",
        "The bastion could not write these results into its machine store. The "
        "usual causes are a full disk under the state dir or a day-file that hit "
        "its ceiling; `observe:disk` and the store's `.capped` markers say which. "
        "Until it is fixed, the record of what was seen on this machine is "
        "incomplete and every finding from it should be read with that in mind.",
        {"record_dropped": holes, "shelves": shelves}))


def _fold_write_gate(b: dict, machine: str, out: list) -> None:
    """The box's own notice of the management lane (bastion 0.33.0,
    quarantine.go): every refusal it recorded, and any pairing whose write
    lanes it closed.

    One row per REFUSAL, keyed on its time + pairing + op, so a refusal the
    host already read is the same row with a moving last_seen, never a pile;
    `notice`, because a refusal is the machine working (the ask was wrong, not
    the box) — the posture rules already say so. One row per QUARANTINE, keyed
    on the pairing, `warn`: five refused writes in ten minutes is the pattern
    this exists to make visible (a host probing after a refusal), and the row
    resolves itself when the beat stops naming the hold. A box that gates
    writes and can take no approval is `warn` too: every write on it is
    refused until the person pins a device or enrols a factor there.
    """
    e = b.get("enforcing")
    if not isinstance(e, dict):
        return
    ring = e.get("refusals")
    if isinstance(ring, list):
        for r in ring[-20:]:
            if not isinstance(r, dict):
                continue
            fp = hashlib.sha256(
                f"{r.get('ts')}|{r.get('pairing')}|{r.get('op')}".encode()).hexdigest()[:16]
            out.append(_finding(
                machine, "refusal", fp, "notice", "heartbeat",
                f"{machine} refused {r.get('op') or 'an op'} from {r.get('pairing') or 'an unnamed pairing'}",
                "The machine said no, and it was right to: the ask carried no approval, named a "
                "capability the pairing does not hold, or a lane the person switched off. Read the "
                "reason; do not try another machine, lane or tool. If the words say an approval "
                "was missing, ask the user for it.",
                {"ts": r.get("ts"), "pairing": r.get("pairing"), "op": r.get("op"),
                 "why": str(r.get("why") or "")[:400], "write": bool(r.get("write"))}))
    held = e.get("quarantined")
    if isinstance(held, list):
        q = e.get("quarantine") if isinstance(e.get("quarantine"), dict) else {}
        for pairing in held:
            info = q.get(pairing) if isinstance(q.get(pairing), dict) else {}
            out.append(_finding(
                machine, "quarantine", str(pairing), "warn", "heartbeat",
                f"{machine} has closed {pairing}'s write lanes until {info.get('until') or 'later'}",
                "Five refused writes from this pairing inside ten minutes. Reads still answer. "
                "Stop asking that machine for writes until the hold lifts; the person can lift it "
                "early at the machine (privatae-bastion quarantine clear <host>).",
                {"pairing": pairing, "until": info.get("until"), "strikes": info.get("strikes")}))
    if e.get("write_gate") is True and e.get("second_factor") is not True:
        out.append(_finding(
            machine, "write-gate", "no-way", "warn", "heartbeat",
            f"{machine} gates writes and can take no approval",
            "Every write on this machine is refused until the user pins their approval device on "
            "it (privatae-bastion device-init on their own machine, then sudo -H privatae-bastion "
            "pin-device <key> on this one) or enrols a factor there (privatae-bastion "
            "stepup-enroll). Reads still answer.",
            {"write_gate": True, "device": e.get("device") is True, "stepup": e.get("stepup") is True}))


def _fold_offline(b: dict, machine: str, out: list) -> None:
    if b.get("online"):
        return
    out.append(_finding(
        machine, "offline", "", "warn", "heartbeat",
        "this machine has stopped beating",
        f"No heartbeat within the online window; nothing on {machine} is being "
        "read right now, and every other finding on it describes the machine as "
        "it was at its last beat.",
        {"last_seen_at": b.get("last_seen_at"),
         "bastion_version": b.get("bastion_version")}))


def derive_findings(bastions: list) -> list:
    """Every finding this pass, from the summaries the beats already carry.

    PURE — no pool, no clock, no I/O — so scripts/test_oversight_ledger.py can
    put a heartbeat fixture in and read the rules out. That is the whole reason
    the fold and the write are two functions.
    """
    from mastercode.runtime.tools import (
        _alerts_counts, _bastion_alerts_state, _bastion_detect_state, _detect_counts)
    out: list = []
    for b in (bastions or []):
        if not isinstance(b, dict):
            continue
        machine = machine_of(b)
        alerts_state, alerts_reason = _bastion_alerts_state(b)
        detect_state, detect_reason = _bastion_detect_state(b)
        _fold_alerts(b, machine, alerts_state, alerts_reason, _alerts_counts(b.get("alerts")), out)
        _fold_detect(b, machine, detect_state, detect_reason, _detect_counts(b.get("detect")), out)
        _fold_posture(b, machine, out)
        _fold_record_holes(b, machine, out)
        _fold_write_gate(b, machine, out)
        _fold_offline(b, machine, out)
    return out


def alert_resolve_scope(bastions: list) -> dict:
    """machine -> the alert keys its CURRENT summary still names.

    THE ONLY AUTO-RESOLVE IN THIS FILE, and the conditions are all three
    necessary. The machine must be ONLINE (an offline box is not reporting, and
    absence there is our blindness, not its recovery) and its alert plane must
    be in a state where it is actually reading its source right now
    (ALERTS_READING_STATES — `unconfigured` means nobody is watching and `stale`
    means the source stopped answering, and in both an absent fingerprint means
    nothing at all). Only then does "this fingerprint is no longer in the set"
    mean the alert cleared.

    A machine not in this mapping has NO alert finding resolved, which is the
    safe default and the reason this returns a mapping rather than a flag.
    """
    from mastercode.runtime.tools import _alerts_counts, _bastion_alerts_state
    scope: dict = {}
    for b in (bastions or []):
        if not isinstance(b, dict) or not b.get("online"):
            continue
        state, _ = _bastion_alerts_state(b)
        if state not in ALERTS_READING_STATES:
            continue
        counts = _alerts_counts(b.get("alerts")) or {}
        machine = machine_of(b)
        scope[machine] = {finding_key(machine, "alert", fp)
                          for fp in (counts.get("fingerprints") or [])}
    return scope


# ── the merge: what to insert, what to update, what to close ────────────────

def merge_findings(existing: list, derived: list, resolve_scope: dict) -> dict:
    """The write plan for one pass. PURE, and it is where the pin lives.

    `existing` is every row already in the ledger (id, key, severity,
    resolved_at). Rules, each of which has a test:

      - a key seen for the first time is an INSERT, with first_seen = now;
      - a key seen again is an UPDATE: last_seen moves, evidence is replaced,
        and SEVERITY MAY RISE, NEVER FALL inside a pinned row. A firewall that
        was high yesterday and is reported at warn today is still the same
        finding, and the operator's memory of it is the worse number;
      - a key that WAS resolved and is derived again REOPENS (resolved_at back
        to NULL) and counts as new — it is newly true, and a notification that
        stayed silent because the row existed once would be silence about a
        recurrence;
      - a key NOT derived this pass stays exactly as it is. No auto-resolve,
        except:
      - an open `<machine>|alert|<fp>` key whose machine is in `resolve_scope`
        and whose fingerprint the current summary no longer names is RESOLVED.
    """
    by_key = {}
    for row in (existing or []):
        r = row if isinstance(row, dict) else dict(row)
        by_key[r.get("key")] = r

    inserts, updates = [], []
    new_by_severity = {s: 0 for s in SEVERITIES}
    seen_keys = set()
    for d in (derived or []):
        key = d["key"]
        seen_keys.add(key)
        prior = by_key.get(key)
        if prior is None:
            inserts.append(d)
            new_by_severity[d["severity"]] = new_by_severity.get(d["severity"], 0) + 1
            continue
        merged = dict(d)
        merged["severity"] = worst(str(prior.get("severity") or ""), d["severity"])
        merged["id"] = prior.get("id")
        reopened = prior.get("resolved_at") is not None
        merged["reopened"] = reopened
        updates.append(merged)
        if reopened:
            new_by_severity[merged["severity"]] = new_by_severity.get(merged["severity"], 0) + 1

    resolve_ids = []
    for key, r in by_key.items():
        if r.get("resolved_at") is not None or key in seen_keys:
            continue
        parts = str(key).split(_KEY_SEP)
        if len(parts) < 2 or parts[1] != "alert":
            continue
        still_named = resolve_scope.get(parts[0])
        if still_named is None:
            # The machine is offline, unconfigured, or its alert source has
            # stopped answering. Absence proves nothing here.
            continue
        if key not in still_named:
            resolve_ids.append(r.get("id"))

    open_by_severity = {s: 0 for s in SEVERITIES}
    for d in inserts:
        open_by_severity[d["severity"]] = open_by_severity.get(d["severity"], 0) + 1
    for u in updates:
        open_by_severity[u["severity"]] = open_by_severity.get(u["severity"], 0) + 1

    return {
        "insert": inserts,
        "update": updates,
        "resolve": [i for i in resolve_ids if i is not None],
        "new": len(inserts) + sum(1 for u in updates if u.get("reopened")),
        "updated": sum(1 for u in updates if not u.get("reopened")),
        "resolved": len([i for i in resolve_ids if i is not None]),
        # The identifiers of what became true this pass, worst first. The
        # notification names these and nothing else — a key is (machine, kind,
        # fingerprint) and carries no content.
        "new_keys": [d["key"] for d in sorted(
            inserts + [u for u in updates if u.get("reopened")],
            key=lambda d: -severity_rank(d["severity"]))],
        # What is open NOW, by severity — the census an operator reads.
        "by_severity": open_by_severity,
        # What became true THIS pass. The notifier keys on this and only this,
        # which is what makes a notification impossible to repeat for a finding
        # that is merely still open.
        "new_by_severity": new_by_severity,
    }


# ── the write ───────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO oversight_findings
    (key, severity, source, machine, evidence, recommendation, first_seen, last_seen)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, now(), now())
ON CONFLICT (key) DO UPDATE SET
    -- MAY RISE, NEVER FALL — asserted here as well as in merge_findings, and
    -- deliberately not only there. The wake and the 5-minute op can run
    -- concurrently; the Python merge reads the ledger before it writes, so two
    -- passes racing could each compute from the same pre-race row and the later
    -- write would lower what the earlier one raised. This CASE is what actually
    -- holds under that, and merge_findings is what the suite pins.
    severity = CASE WHEN array_position(ARRAY['notice','warn','high','critical']::text[],
                                        EXCLUDED.severity)
                      > array_position(ARRAY['notice','warn','high','critical']::text[],
                                        oversight_findings.severity)
                    THEN EXCLUDED.severity ELSE oversight_findings.severity END,
    source = EXCLUDED.source,
    machine = EXCLUDED.machine,
    evidence = EXCLUDED.evidence,
    recommendation = EXCLUDED.recommendation,
    last_seen = now(),
    -- first_seen is NOT in this SET list. It is the whole point of the table.
    resolved_at = NULL
"""


async def apply_findings(pool, plan: dict) -> dict:
    """Write one merge plan. Every statement is individually guarded: a single
    malformed finding must not cost the pass the other forty."""
    written = 0
    failed = 0
    for row in list(plan.get("insert") or []) + list(plan.get("update") or []):
        try:
            await pool.execute(
                _UPSERT_SQL, row["key"], row["severity"], row["source"],
                row.get("machine"),
                json.dumps(row.get("evidence") or {}, default=str, ensure_ascii=False),
                row.get("recommendation"),
            )
            written += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.warning("oversight: finding write failed (%s): %s", row.get("key"), e)
    ids = [i for i in (plan.get("resolve") or []) if i is not None]
    if ids:
        try:
            await pool.execute(
                "UPDATE oversight_findings SET resolved_at = now() "
                "WHERE id = ANY($1::bigint[]) AND resolved_at IS NULL", ids,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("oversight: resolve failed: %s", e)
    return {"written": written, "failed": failed}


async def run_oversight_ingest(pool, entity_id: int, *, force_store: bool = False) -> dict:
    """One pass: read every bastion's latest beat, fold, merge, write.

    WRITES NOTHING TO THE INBOX. Saying something is a separate decision with a
    separate audience, and it lives in `notify_new_findings` — so the op can be
    run by hand, or twice, without a message each time.
    """
    from server.network.topology import list_bastions
    from mastercode.runtime.oversight_store import pull_store_findings
    bastions = await list_bastions(pool, entity_id)
    derived = derive_findings(bastions)
    scope = alert_resolve_scope(bastions)
    try:
        existing = await pool.fetch(
            "SELECT id, key, severity, resolved_at FROM oversight_findings")
    except Exception as e:  # noqa: BLE001
        # The table is absent (a pod that has not applied v153). Report it as
        # the unmeasured state it is; do not fold into nothing and call it zero.
        log.info("oversight: ledger unreadable (%s)", e)
        return {"status": "unavailable", "reason": str(e)[:200],
                "machines": len(bastions or [])}
    # ── the machines' own records ───────────────────────────────────────────
    #
    # The four folds above read the heartbeat, and the heartbeat carries COUNTS.
    # This reads the findings THEMSELVES out of each box's journal through the
    # governed DCN range-read, so the ledger holds one row per finding with the
    # evidence the box computed, rather than one row per (machine, level).
    #
    # IT IS ADDITIVE AND IT NEVER BLOCKS THE BEAT FOLD. A store that is
    # unreachable, revoked, or on a pod without v162 contributes problems and no
    # findings; the heartbeat-derived rows are written either way. Degrading to
    # the counts is the right failure — degrading to nothing is not.
    store_problems: list[str] = []
    try:
        pulled = await pull_store_findings(pool, entity_id, _finding, force=force_store,
                                           bastions=bastions)
        derived.extend(pulled["findings"])
        store_problems = pulled["problems"]
    except Exception as e:  # noqa: BLE001
        log.info("oversight: the machine-record fold failed (%s)", e)
        store_problems = [f"the machine records could not be read: {str(e)[:160]}"]
        pulled = {"stores": 0, "bytes": 0, "findings": [], "incomplete_days": 0,
                  "pending_cursors": [], "skipped": "", "accounting": {}}

    plan = merge_findings(existing, derived, scope)
    write = await apply_findings(pool, plan)
    # THE CURSORS MOVE ONLY NOW, after the rows are on disk. Advancing them
    # inside the read loop meant a transient upsert failure — which
    # apply_findings swallows into `write_failed` — lost those findings
    # permanently, because nothing ever re-reads a passed cursor.
    #
    # AND ONLY IF EVERY ROW LANDED. apply_findings swallows per-row failures
    # into `write_failed`; committing the cursors past a row that did not write
    # loses that finding for good. Holding the cursors costs one duplicate fold
    # next pass, which is the right side of the trade.
    if pulled.get("pending_cursors") and not write["failed"]:
        from mastercode.runtime.oversight_store import commit_cursors
        await commit_cursors(pool, pulled["pending_cursors"])
    return {
        "machines": len(bastions or []),
        "derived": len(derived),
        # The store fold's own numbers, reported separately so "no new findings"
        # and "no machine record was readable" can never look the same.
        "stores_read": pulled["stores"],
        "store_bytes": pulled["bytes"],
        "store_findings": len(pulled["findings"]),
        "store_incomplete_days": pulled["incomplete_days"],
        "store_problems": store_problems[:_INBOX_IDENT_MAX],
        "store_skipped": pulled.get("skipped", ""),
        "store_accounting": pulled.get("accounting", {}),
        "new": plan["new"],
        "updated": plan["updated"],
        "resolved": plan["resolved"],
        "by_severity": plan["by_severity"],
        "new_by_severity": plan["new_by_severity"],
        "new_keys": plan["new_keys"][:_INBOX_IDENT_MAX],
        "written": write["written"],
        "write_failed": write["failed"],
    }


# ── the one thing it says, and where ────────────────────────────────────────

async def notify_new_findings(pool, entity_id: int, result: dict, *,
                              min_severity: str = "high") -> dict:
    """At most ONE inbox row per pass, naming identifiers and a link.

    NEVER CONTENT. The row carries what was found and where to look at it; the
    evidence stays in the ledger. This is the event the reach-out lane
    subscribes to, and a reach-out channel is email — putting a machine's alert
    bodies in it would send a customer's infrastructure detail out through a
    mail provider, which is the one thing this whole lane is shaped to avoid.

    `min_severity` is the floor the CALLER is entitled to speak at. The wake
    passes "critical" (high is the scheduled op's to report — a high is not
    worth waking someone for, and the 5-minute op carries it within one cycle).
    The scheduled op passes "high", and still prefers the critical event when
    this pass produced both, so a critical found by the schedule — because the
    wake was debounced, or because the machine came back online — is never
    reported as merely high.
    """
    new_by_sev = (result or {}).get("new_by_severity") or {}
    n_crit = int(new_by_sev.get("critical") or 0)
    n_high = int(new_by_sev.get("high") or 0)
    if n_crit > 0:
        event, severity, count = EVENT_CRITICAL, "critical", n_crit
    elif severity_rank(min_severity) <= severity_rank("high") and n_high > 0:
        event, severity, count = EVENT_HIGH, "high", n_high
    else:
        return {"sent": False, "reason": "no new finding at or above the floor"}

    idents = [k for k in ((result or {}).get("new_keys") or [])][:_INBOX_IDENT_MAX]
    subject = (f"{count} new {severity} finding{'s' if count != 1 else ''} "
               f"on your machines")
    # The deep-link contract (server/deep_link.py): the link names the app and
    # its view, so it opens the Oversight app on its findings, worst first. A
    # reach-out makes it absolute against the host's own address
    # (reachout/service.doorbell_link) because it is read outside the host.
    from server.deep_link import app_link
    link = app_link("oversight", "findings")
    body_lines = [
        f"The oversight ledger recorded {count} new {severity} "
        f"finding{'s' if count != 1 else ''} this pass.",
        "",
        f"Open them in the Oversight app: {link}",
    ]
    if idents:
        body_lines += ["", "Identifiers:"] + [f"- `{k}`" for k in idents]
    try:
        from server.bridge.memory_handlers import handle_inbox_append  # memory_handlers.py:328
        env = await handle_inbox_append({
            "subject": subject,
            "body": "\n".join(body_lines),
            "body_format": "text/markdown",
            "message_kind": "system_notification",
            "payload": {
                "event": event,
                "severity": severity,
                "count": count,
                "keys": idents,
                "link": link,
            },
        })
    except Exception as e:  # noqa: BLE001
        log.warning("oversight: notification failed: %s", e)
        return {"sent": False, "error": str(e)[:200]}
    return {"sent": bool((env or {}).get("ok")), "event": event,
            "severity": severity, "count": count}


# ── the registry op ─────────────────────────────────────────────────────────

async def _exec_oversight_ingest(args: dict) -> dict:
    """Tool executor (runtime/tools.py dispatch + the op lane via run_direct_op).

    `wake=true` marks the call as the heartbeat's (topology._oversight_wake),
    which owns its own notification and reports criticals only. Every other
    caller — the 5-minute standing op, and a host running it by hand — gets the
    scheduled behaviour: notify at high and above.
    """
    a = args or {}
    try:
        from server import brain_service
        pool = brain_service.get_pool()          # brain_service.py:149
        if pool is None:
            return {"status": "error", "result": "oversight_ingest failed: pool unavailable"}
        entity_id = brain_service._entity_id     # brain_service.py:27
        # The store read is throttled ONLY on the scheduled pass. A wake exists
        # to fetch the detail behind a new critical the instant the beat reports
        # it, and a hand-run is somebody asking now — gating either by the poll
        # clock defeats the reason it ran. Rows seeded before the `scheduled`
        # marker existed carry `args: {}`, read as hand-runs, and so read the
        # stores every pass: the old audit volume, never a skipped wake.
        #
        # THE GATE IS OPT-IN ON DATA A USER CAN AUTHOR. A scheduled row created
        # by anything other than ensure_oversight_ingest_task — the Agents app
        # after cancelling the seeded one, POST /api/calendar — carries no
        # marker and reads every pass. Safe direction, and known: making the
        # gate a property of the LANE would mean the runner stamping the arg on
        # its own call (offline_task_runner._run_op), which is a runner change
        # and not made here.
        force_store = bool(a.get("wake")) or not bool(a.get("scheduled"))
        res = await run_oversight_ingest(pool, entity_id, force_store=force_store)
        if res.get("status") == "unavailable":
            return {"status": "error",
                    "result": f"oversight_ingest could not read the ledger: {res.get('reason')}"}
        if not a.get("wake"):
            res["notified"] = await notify_new_findings(pool, entity_id, res,
                                                        min_severity="high")
        return {"status": "ok", "result": res}
    except Exception as e:  # noqa: BLE001
        return {"status": "error",
                "result": f"oversight_ingest failed: {getattr(e, 'detail', None) or e}"}


# ── the platform-default standing task ──────────────────────────────────────

async def ensure_oversight_ingest_task(pool, entity_id: int) -> dict:
    """Seed the 5-minute oversight ingest for this host, once.

    Idempotent on the op payload, exactly like the weekly changelog
    (host_changelog.ensure_weekly_changelog_task:489-530): if ANY
    calendar_events row for this entity carries payload.op.tool =
    oversight_ingest — in ANY status, cancelled included — nothing is written. A
    user who stopped it from the Agents app has made a decision and a reboot
    must not undo it.

    First fire is one period out, and the runner re-arms it from there.
    """
    out = {"seeded": False}
    try:
        existing = await pool.fetchval(
            """
            SELECT count(*) FROM calendar_events
             WHERE entity_id = $1 AND event_type = 'scheduled_task'
               AND payload->'op'->>'tool' = $2
            """,
            entity_id, OVERSIGHT_TOOL,
        )
        if int(existing or 0) > 0:
            out["reason"] = "already present (any status — a cancel is respected)"
            return out
        from server.bridge.memory_handlers import handle_calendar_append  # memory_handlers.py:224
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=OVERSIGHT_PERIOD_S)
        env = await handle_calendar_append({
            "event_type": "scheduled_task",
            "summary": OVERSIGHT_TASK_TITLE,
            "event_at": fire_at.isoformat(),
            "status": "scheduled",
            "source": "system",
            "recurrence": OVERSIGHT_RECURRENCE,
            "payload": {"op": {"tool": OVERSIGHT_TOOL, "args": {"scheduled": True}},
                        "seeded_by": "platform_default"},
        })
        out["seeded"] = bool((env or {}).get("ok"))
        out["event_id"] = (env or {}).get("event_id")
        out["fire_at"] = (env or {}).get("event_at")
        if not out["seeded"]:
            out["reason"] = (env or {}).get("error") or "calendar append failed"
    except Exception as e:  # noqa: BLE001
        out["reason"] = str(e)[:200]
        log.warning("oversight: standing task seed failed for entity %s: %s", entity_id, e)
    return out
