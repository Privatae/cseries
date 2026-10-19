"""runtime/oversight_store.py — folding a machine's OWN record into the ledger.

WHAT THIS ADDS TO THE LEDGER. oversight.py folds four things, and all four are
COUNTS off the heartbeat: how many findings at each level (v146 `detect`), how
many alerts (v150), host posture, offline. So the ledger could say "three high
findings on <machine>" and could not say which three — and the same new
executable appearing on three machines looked like three unrelated numbers
rather than one incident.

The bastion now keeps the findings themselves in a journal on its own disk
(privatae-bastion client-go/machinestore.go), and the host reads it through the
governed DCN range-read. This turns those counts into ROWS, with the evidence
the box computed and a fingerprint that is stable across machines.

── WHAT THE JOURNAL IS, AND WHY THE PARSING IS SHAPED THIS WAY ──────────────

It is a journal of TRANSITIONS, not a log of passes: the box writes a line when
a finding opens, changes materially, or closes, because a finding is pinned
until a human resolves it and the pass runs every 60 seconds. So folding it is a
REPLAY, not a snapshot read — the state of a finding is the last transition
that mentioned it, and lines must be applied in file order.

Five transitions, and the two easy ones to get wrong are the last two:

    opened      first time the box reported this fingerprint
    changed     same fingerprint, materially different (level, tier, reason)
    closed      the box stopped reporting it
    carried     still open when a new day-file was started. NOT a new finding —
                the box writes one per open finding at each UTC rollover so each
                day-file is self-contained, since retention may already have
                swept the file holding the `opened` line.
    day_capped  the day-file hit its ceiling and was sealed. NOT a finding at
                all; it means the record for that day is INCOMPLETE, which is
                itself worth a row, because the alternative is the host reading
                a truncated day as a quiet one.

`closed` IS NOT `resolved`, and this module must never convert one into the
other. The box says so in every closed line's note, and oversight.py (the "WHAT IS NEVER AUTO-RESOLVED" block) makes
the same distinction on its side: the overwhelmingly common reason a finding
stops being reported is that the machine stopped reporting, or that somebody
retook the accounting. A `closed` line lets a row STOP BEING RE-ASSERTED; what
actually resolves a row is the resolve path in oversight.py, which is narrow on
purpose.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("cseries.runtime.oversight_store")

# The transitions the box writes (client-go/machinestore.go). Listed rather than
# accepted open-endedly: a transition this host does not know about must be
# reported as unread, not silently skipped, or a future box version could write
# findings into a host that quietly drops them.
T_OPENED = "opened"
T_CHANGED = "changed"
T_CLOSED = "closed"
# day_open is ONE snapshot line at the top of each day-file naming what was
# already open, so the file is self-contained for a reader whose window starts
# inside it. It replaced a per-finding `carried` line, which could fill a day
# on its own and lock the shelf — see the box's machinestore.go.
T_DAY_OPEN = "day_open"
T_CAPPED = "day_capped"
KNOWN_TRANSITIONS = (T_OPENED, T_CHANGED, T_CLOSED, T_DAY_OPEN, T_CAPPED)

# The box's levels are the ledger's severities, one for one
# (client-go/delta.go's deltaNotice/Warn/High/Critical vs oversight.SEVERITIES).
# Mapped explicitly anyway: an identity mapping written down is a decision, and
# an unknown level must land somewhere defined rather than propagate a string
# the severity CHECK will reject at write time.
LEVELS = {"notice": "notice", "warn": "warn", "high": "high", "critical": "critical"}
LEVEL_FALLBACK = "warn"

# One journal line's evidence, after which it is dropped for a marker. The
# ledger row caps its own evidence at 8 KB (oversight._EVIDENCE_MAX); this is
# the line-level guard so a pathological line cannot dominate a batch.
_LINE_MAX = 64_000


def parse_journal(data: bytes) -> tuple[list[dict], int, list[str]]:
    """Parse a range of journal BYTES into lines.

    Returns (lines, consumed_bytes, problems).

    BYTES, NOT A DECODED STRING, AND THAT IS NOT A STYLE CHOICE. The box returns
    the range base64-encoded (client-go/dcn.go:655 `content_b64`), so the real
    bytes are available and the cursor is a byte offset into the file on the
    box. An earlier version worked on text and counted
    `len(line.encode('utf-8','replace'))`, which is wrong for any byte the
    decode replaced: each invalid byte arrives as U+FFFD and re-encodes to
    three, so the cursor ran AHEAD of the file and every subsequent line in that
    day was read at the wrong offset. Measured at +4 bytes on a line with two
    invalid bytes in it. Counting the real bytes makes the class disappear.

    THE TRAILING PARTIAL LINE IS NEVER CONSUMED. The box appends to this file
    while the host reads it, so a range-read routinely ends mid-record.
    `consumed` is the offset of the byte after the last COMPLETE line, and that
    is what the cursor must advance to — counting a partial line as read drops
    whatever the rest of it said, permanently and silently, which is the one
    failure this whole store exists to prevent.

    A line that is not valid JSON is reported as a problem and SKIPPED, not
    fatal: one corrupt line must not stop the fold of the thousand after it.
    """
    lines: list[dict] = []
    problems: list[str] = []
    consumed = 0
    if not data:
        return lines, consumed, problems

    for raw in data.split(b"\n")[:-1]:
        consumed += len(raw) + 1
        s = raw.strip()
        if not s:
            continue
        if len(s) > _LINE_MAX:
            problems.append(f"a journal line of {len(s)} bytes was skipped as oversized")
            continue
        try:
            obj = json.loads(s.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            problems.append("a journal line was not valid JSON and was skipped")
            continue
        if isinstance(obj, dict):
            lines.append(obj)
        else:
            problems.append("a journal line was not an object and was skipped")
    return lines, consumed, problems


def replay(lines: list[dict], machine: str) -> tuple[dict, list[str], bool]:
    """Replay a journal in file order into the open set it describes.

    Returns (asserted_by_fp, problems, incomplete).

    `asserted_by_fp` maps fingerprint -> the last line that ASSERTED it, and it
    contains ONLY fingerprints an `opened` or `changed` line named in this
    window. A `day_open` snapshot is RECOGNISED and then IGNORED here: it never
    puts a fingerprint into the result, and it carries nothing the ledger needs.
    (An earlier version kept a set of what the snapshot listed, "so a later
    close still removes it" — but nothing ever read that set, so the sentence
    described an effect the code did not have. Deleted rather than kept as an
    honest-sounding no-op.)

    THAT EXCLUSION IS WHAT STOPS THE LEDGER RESURRECTING RESOLVED WORK. The
    snapshot is written once per UTC day for everything still open on the box.
    If it produced findings, then every rollover would re-assert a finding an
    operator had already resolved here: the upsert clears `resolved_at`,
    merge_findings counts it in `new_by_severity`, and notify_new_findings sends
    an inbox row saying it is new. Daily, for ever, until somebody retook the
    accounting on the machine. The snapshot exists so a HUMAN reading a
    truncated day-file knows what was open; the ledger already knows, because
    the ledger is the durable state.

    `incomplete` is True when any day in this range was SEALED at its ceiling.
    The record for that day is partial, and a host reading a truncated day as a
    quiet one is exactly the misreading the box writes that marker to prevent.
    """
    asserted: dict[str, dict] = {}
    problems: list[str] = []
    incomplete = False

    for ln in lines:
        t = str(ln.get("transition") or "").strip()
        if t not in KNOWN_TRANSITIONS:
            # Not skipped silently: a newer box writing a transition this host
            # does not understand is a real condition, and the host must say it
            # cannot read part of the record rather than act as though it did.
            problems.append(f"unknown transition {t!r} from {machine} — this host "
                            "cannot read part of that machine's record")
            continue
        if t == T_CAPPED:
            incomplete = True
            continue
        if t == T_DAY_OPEN:
            continue
        ev = ln.get("event")
        if not isinstance(ev, dict):
            problems.append(f"a {t} line from {machine} carried no event")
            continue
        fp = str(ev.get("fp") or "").strip()
        if not fp:
            problems.append(f"a {t} line from {machine} carried no fingerprint")
            continue
        if t == T_CLOSED:
            asserted.pop(fp, None)
            continue
        # opened / changed both assert the finding is open NOW. The last one
        # wins, which is why order matters and why the caller must never sort
        # these.
        asserted[fp] = ln
    return asserted, problems, incomplete


def line_to_finding(ln: dict, machine: str, finding_factory) -> dict | None:
    """One journal line -> one ledger finding, via oversight._finding.

    The factory is passed in rather than imported so this module stays free of a
    circular import and so a test can drive the mapping without the ledger.
    """
    ev = ln.get("event")
    if not isinstance(ev, dict):
        return None
    fp = str(ev.get("fp") or "").strip()
    if not fp:
        return None
    kind = str(ev.get("kind") or "").strip() or "unknown"
    level = str(ev.get("level") or "").strip().lower()
    severity = LEVELS.get(level)
    if severity is None:
        # An unrecognised level is NOT dropped and NOT guessed downward. It
        # lands at the fallback and says so in the evidence, because a finding
        # the host cannot grade is still a finding, and grading it `notice`
        # because the string was unfamiliar is how a critical goes quiet.
        severity = LEVEL_FALLBACK

    entry = ev.get("entry") if isinstance(ev.get("entry"), dict) else {}
    exe = str(entry.get("exe") or "").strip()
    unit = str(entry.get("unit") or "").strip()
    user = str(entry.get("user") or "").strip()

    title = _title(kind, exe or unit or fp, machine)
    evidence = {
        "fp": fp,
        "kind": kind,
        "level": level or "unknown",
        "tier": str(ev.get("tier") or "").strip(),
        "reason": str(ev.get("reason") or "").strip(),
        "exe": exe,
        "unit": unit,
        "user": user,
        "exe_digest": str(entry.get("exe_digest") or "").strip(),
        "argv_shape": str(entry.get("argv_shape") or "").strip(),
        # first_seen is the BOX's first sighting, which is not when the host
        # first read it. Both matter and they are different numbers: the ledger
        # row's own first_seen is the host's, and this is the machine's.
        "machine_first_seen": str(ev.get("first_seen") or "").strip(),
        "transition": str(ln.get("transition") or "").strip(),
        "baseline": str(ln.get("baseline") or "").strip(),
        "reported_at": str(ln.get("at") or "").strip(),
        "bastion_version": str(ln.get("version") or "").strip(),
        "box_evidence": ev.get("evidence") if isinstance(ev.get("evidence"), list) else [],
    }
    if severity == LEVEL_FALLBACK and level not in LEVELS:
        evidence["level_unrecognised"] = (
            f"the box graded this {level!r}, which this host does not know; "
            f"filed at {LEVEL_FALLBACK} rather than dropped or downgraded")
    return finding_factory(
        machine=machine,
        kind=f"detect:{kind}",
        fingerprint=fp,
        severity=severity,
        source="store",
        title=title,
        recommendation=_recommendation(kind, exe or unit or fp),
        evidence=evidence,
    )


_TITLES = {
    "proc_new": "a program that was not in the accounting is running on {m}",
    "proc_changed": "the program at a known post on {m} has been replaced",
    "proc_gone": "a program the accounting expects is not running on {m}",
    "scheduler_new": "a scheduled job that was not in the accounting exists on {m}",
    "scheduler_changed": "a scheduled job on {m} now runs something else",
    "scheduler_gone": "a scheduled job the accounting expects is gone from {m}",
    "binary_digest_changed": "the bastion's own binary on {m} has changed",
}


def _title(kind: str, what: str, machine: str) -> str:
    base = _TITLES.get(kind)
    if base is None:
        return f"{kind} on {machine}: {what}"
    return base.format(m=machine) + (f": {what}" if what else "")


_RECOMMENDATIONS = {
    "proc_new": "Identify {w}. If it belongs, retake the accounting so it stops "
                "being reported; if it does not, this is the finding to act on.",
    "proc_changed": "A binary at a known post was replaced. Confirm it was an "
                    "update you made before retaking the accounting.",
    "binary_digest_changed": "The bastion binary changed without a version change. "
                             "Treat as critical until you can account for it.",
    "scheduler_changed": "A scheduled job now runs a different command. Read the "
                         "job on the machine before retaking the accounting.",
}


def _recommendation(kind: str, what: str) -> str:
    base = _RECOMMENDATIONS.get(kind)
    if base is None:
        return ("Read the finding on the machine, then either act on it or retake "
                "the accounting so it stops being reported.")
    return base.format(w=what)


def findings_from_journal(data: bytes, machine: str, finding_factory
                          ) -> tuple[list[dict], int, list[str], bool]:
    """The whole pure path: journal BYTES -> ledger findings.

    Returns (findings, consumed_bytes, problems, incomplete). `consumed_bytes`
    is what the cursor advances by — never past the last complete line.
    """
    lines, consumed, problems = parse_journal(data)
    asserted, replay_problems, incomplete = replay(lines, machine)
    problems.extend(replay_problems)

    findings = []
    # Sorted by fingerprint so a batch is deterministic; the REPLAY above is
    # what depends on file order, and it has already run.
    for fp in sorted(asserted):
        f = line_to_finding(asserted[fp], machine, finding_factory)
        if f is not None:
            findings.append(f)
    return findings, consumed, problems, incomplete


# ── the read side ───────────────────────────────────────────────────────────
#
# Everything above is pure. Everything below talks to the DB and to the box, and
# is kept apart from it so the rules that cost a finding can be tested without
# either.

# One ingest pass reads at most this much from one store. The journal is quiet
# on a healthy machine — zero bytes — so this bounds the ABNORMAL case: a box
# that churned all night, or a store being read for the first time with 30 days
# of retained history behind it. Four range-reads at the DCN's 512 KiB ceiling.
# A pass that hits the bound stops cleanly and the next one resumes from the
# cursor, so nothing is skipped; it just takes more passes.
MAX_BYTES_PER_STORE = 4 * 512 * 1024

# And at most this many day-files, oldest first. A first read of a store with a
# month of history would otherwise walk all thirty in one pass.
MAX_DAYS_PER_PASS = 4

# HOW OFTEN THE STORES ARE READ, in ingest passes. The ingest runs every 5
# minutes (oversight.OVERSIGHT_RECURRENCE) and every run_dcn_op writes an
# nac_mgmt_audit row, so listing every store on every pass costs 288 audit rows
# per store per day even on a machine where nothing happens — turning the user's
# own command-accounting log into mostly robot polling. Every third pass is 15
# minutes, which is well inside the cadence the plan sets for acting on a
# finding, and cuts that to 96.
#
# THE WAKE IS THE REASON THIS IS SAFE, and it is safe only because the wake and
# a hand-run pass `force=True` and bypass this gate entirely. The first version
# defined `force` and never passed it, so two wakes in three skipped the store
# read and the detail behind a brand-new critical waited up to fifteen minutes —
# the exact thing the wake exists to prevent. The gate applies to the SCHEDULED
# pass alone (oversight._exec_oversight_ingest reads the `scheduled` marker the
# seeded task carries).
STORE_POLL_EVERY_N_PASSES = 3

FINDINGS_SHELF = "findings"

# The box's seal marker suffix (client-go/machinestore.go storeSealSuffix): a
# day-file that hit its ceiling. Its presence means that day's record is
# INCOMPLETE, not that it is quiet.
SEAL_SUFFIX = ".capped"

# One range-read. The DCN clamps to this server-side (nac_dcn.DCN_MAX_RANGE_BYTES
# default 512 KiB); naming it here keeps the "is this window full" test honest
# rather than comparing against a number the server may have shrunk.
RANGE_BYTES = 512 * 1024


async def machine_record_stores(pool, entity_id: int) -> list[dict]:
    """Every granted store holding a machine's own record.

    ONLY purpose='machine_record'. The ingest must never walk a folder the user
    added — the marker is a column precisely so this cannot become a guess about
    a path (v162).
    """
    rows = await pool.fetch(
        "SELECT id, server, root FROM dcn_stores "
        "WHERE entity_id = $1 AND purpose = 'machine_record' "
        "AND approved_at IS NOT NULL AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now()) ORDER BY id",
        entity_id)
    return [{"store_id": r["id"], "server": r["server"], "root": r["root"]} for r in rows]


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_of(name: str) -> str:
    """The UTC day a journal filename is about, or '' if it is not one."""
    base = name.rsplit("/", 1)[-1]
    if not base.endswith(".jsonl"):
        return ""
    day = base[: -len(".jsonl")]
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return ""
    return day if day.replace("-", "").isdigit() else ""


async def _cursors(pool, store_id: int, shelf: str) -> dict:
    rows = await pool.fetch(
        "SELECT day, byte_offset, sealed FROM oversight_store_cursor "
        "WHERE store_id = $1 AND shelf = $2", store_id, shelf)
    return {r["day"]: {"offset": r["byte_offset"], "sealed": r["sealed"]} for r in rows}


async def commit_cursors(pool, pending) -> int:
    """Persist the cursor advances of a pass, AFTER its findings are written.

    Returns how many landed. A failure here is safe in the direction that
    matters: the bytes are re-read next pass and the findings re-derived, which
    costs a duplicate fold and loses nothing.
    """
    done = 0
    for store_id, shelf, day, offset, sealed in pending:
        try:
            await _save_cursor(pool, store_id, shelf, day, offset, sealed)
            done += 1
        except Exception as e:  # noqa: BLE001
            log.info("oversight store: cursor %s/%s/%s not saved (%s)",
                     store_id, shelf, day, e)
    return done


async def _save_cursor(pool, store_id: int, shelf: str, day: str,
                       offset: int, sealed: bool) -> None:
    await pool.execute(
        "INSERT INTO oversight_store_cursor (store_id, shelf, day, byte_offset, "
        "sealed, last_read_at) VALUES ($1,$2,$3,$4,$5, now()) "
        "ON CONFLICT (store_id, shelf, day) DO UPDATE SET "
        "byte_offset = EXCLUDED.byte_offset, sealed = EXCLUDED.sealed, "
        "last_read_at = now()",
        store_id, shelf, day, offset, sealed)


async def _due_this_pass(pool, entity_id: int) -> bool:
    """Whether this pass reads the stores at all (STORE_POLL_EVERY_N_PASSES).

    Derived from the CLOCK, not a counter, so it needs no state and cannot drift
    or be reset by a restart: the ingest fires on a 5-minute calendar, so the
    minute-of-hour divided by 5 gives a stable pass index.
    """
    minute = datetime.now(timezone.utc).minute
    return (minute // 5) % STORE_POLL_EVERY_N_PASSES == 0


async def pull_store_findings(pool, entity_id: int, finding_factory,
                              force: bool = False, bastions=None) -> dict:
    """Read every machine-record store's journal since its cursor and fold it.

    Returns {findings, problems, stores, bytes, incomplete_days}.

    THE CURSOR IS ONLY ADVANCED BY WHAT WAS ACTUALLY PARSED. Not by the size of
    the range asked for, and not to the end of the file — `consumed` stops at
    the last COMPLETE line. A box appending while this reads is the normal case,
    not an edge, and advancing past a partial line drops the rest of it for
    good.

    EVERY NON-OK STATE IS A PROBLEM, NEVER AN EMPTY RESULT. run_dcn_op returns
    typed states (nac_dcn.py:_typed) — offline, refused, gone. Folding any of
    them as "no new findings" would let an unreachable or revoked machine read
    exactly like a quiet one, which is the whole failure mode the detection
    plane is built to avoid. The cursor is NOT advanced on any of them.
    """
    from server.network.nac_dcn import run_dcn_op

    # pending_cursors: nothing is committed here. The caller
    # writes the ledger FIRST and only then calls commit_cursors, because a
    # cursor advanced before the upsert means a transient DB failure loses those
    # bytes for good — apply_findings swallows per-row errors into a
    # `write_failed` count, and nothing ever re-reads a passed cursor.
    out = {"findings": [], "problems": [], "stores": 0, "bytes": 0,
           "incomplete_days": 0, "pending_cursors": [], "skipped": "",
           # machine -> the accounting's verification state, for the result
           "accounting": {}}
    if not force and not await _due_this_pass(pool, entity_id):
        out["skipped"] = "not this pass"
        return out
    try:
        stores = await machine_record_stores(pool, entity_id)
    except Exception as e:  # noqa: BLE001
        # The column or table is absent (a pod that has not applied v162).
        # Report it as unmeasured; never fold into nothing and call it zero.
        log.info("oversight store: cannot list machine-record stores (%s)", e)
        out["problems"].append(f"machine-record stores unreadable: {str(e)[:160]}")
        return out

    # hostname -> the fingerprint bound to that bastion's row at redeem, which
    # is what the accounting's signer certificate must carry. None for a row
    # that was paired bearer-only; the verifier treats that as untestable, not
    # as passed.
    bound_fp, beat_taken = {}, {}
    for b in (bastions or []):
        if isinstance(b, dict) and b.get("hostname"):
            bound_fp[str(b["hostname"])] = b.get("hardware_fingerprint")
            # The BEAT's word on which accounting the box is comparing against
            # (detect.baseline_taken_at, narrowed by pairing_grants.narrow_detect).
            # This is the freshness check the signature cannot make: a validly
            # signed accounting from last month restores cleanly, and a mirror
            # that failed to update verifies happily — but the beat says what
            # the box is actually using, and a mismatch is a finding.
            d = b.get("detect") if isinstance(b.get("detect"), dict) else {}
            beat_taken[str(b["hostname"])] = str(d.get("baseline_taken_at") or "").strip()

    out["_host_ca_raised"] = False
    for st in stores:
        out["stores"] += 1
        machine = st["server"] or f"store:{st['store_id']}"
        # TWO try blocks, not one. With a single block a raise inside the
        # accounting verifier skipped the journal fold for that machine — one
        # crafted baseline.json silenced a machine's entire detection journal
        # on the host, every pass, and the ledger said only "could not be
        # read". The verifier and the journal fold are separate; neither may
        # take the other down. Same principle run_oversight_ingest applies one level up.
        try:
            await _verify_accounting(pool, entity_id, st, machine, finding_factory,
                                     run_dcn_op, out, bound_fp.get(machine),
                                     beat_taken.get(machine, ""))
        except Exception as e:  # noqa: BLE001
            log.info("oversight store: %s accounting check failed (%s)", machine, e)
            out["problems"].append(f"{machine}: accounting check failed: {str(e)[:160]}")
            out["findings"].append(_accounting_finding(
                machine, {"state": "unreadable",
                          "reason": f"the accounting check raised: {str(e)[:200]}"},
                finding_factory))
        try:
            await _pull_one(pool, entity_id, st, machine, finding_factory,
                            run_dcn_op, out)
        except Exception as e:  # noqa: BLE001
            # One unreachable machine must not stop the fold of the others.
            log.info("oversight store: %s failed (%s)", machine, e)
            out["problems"].append(f"{machine}: {str(e)[:160]}")
            out["findings"].append(
                unreadable_finding(machine, str(e)[:200], finding_factory))
    # Bookkeeping for this pass only; not part of the result.
    out.pop("_host_ca_raised", None)
    return out



def unreadable_finding(machine: str, reason: str, finding_factory) -> dict:
    """A ledger ROW saying this machine's record could not be read.

    `store_problems` alone, returned in the op result, is consumed by nothing —
    notify_new_findings keys only on new_by_severity — so the one place a human
    looks could not tell "nothing new on that machine" from "nobody could read
    that machine". The heartbeat folds already turn the same condition into a
    `detect-state` row; this is the same move, one shelf over.

    ONE ROW PER MACHINE, keyed on the machine alone, so a box that has been
    unreachable for a week is one pinned row with a moving last_seen rather than
    a pile.
    """
    return finding_factory(
        machine=machine,
        kind="store-state",
        fingerprint="unreadable",
        severity="high",
        source="store",
        title=f"this machine's own findings record could not be read on {machine}",
        recommendation=(
            "The detection counts on the heartbeat still apply; what is missing is "
            "WHICH findings they are. Check the machine is online and that its "
            "machine-record store is still granted — a revoked store reads exactly "
            "like a quiet one from here, which is why this row exists."),
        evidence={"reason": reason[:400]})


# The accounting is small — entries are a few hundred bytes each and a busy
# box has a few hundred — so one governed range-read covers it. Larger than this
# is read as "could not be verified", never partially.
ACCOUNTING_MAX_BYTES = 512 * 1024


# Which verdict states are the MACHINE's problem, and at what severity. Absent
# from this table = not a finding about the machine at all (no_ca is the host's;
# absent/verified are not findings).
_ACCOUNTING_SEVERITY = {
    "unsigned": "high", "wrong_alg": "high", "bad_cert": "high", "not_our_ca": "high",
    "cert_not_valid": "high", "wrong_machine": "high", "bad_signature": "high",
    "unreadable": "high", "stale_mirror": "high",
    # A bearer-only pairing has no bound fingerprint, by the user's own choice
    # (bastion_enroll: "full access is the user's sovereign choice"). The
    # accounting cannot be tied to the machine, and that is worth a line — not
    # a permanent HIGH row punishing a choice the platform said was theirs.
    "no_fingerprint": "notice",
}


def _instants_differ(a: str, b: str) -> bool:
    """Whether two timestamps name different INSTANTS. Parsed, not compared as
    strings: the beat and the mirror are byte-identical today only because
    both read one Go field formatted once, and the day either side emits
    `+00:00` or nanoseconds for the same moment, string equality would file a
    high `stale_mirror` row against every machine in the fleet. Either side
    unparseable -> fall back to the strings, since that is all there is."""
    from server.network.bastion_baseline import _parse_rfc3339
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    ta, tb = _parse_rfc3339(a, assume_utc=True), _parse_rfc3339(b, assume_utc=True)
    if ta is not None and tb is not None:
        return ta != tb
    return a != b


def _accounting_finding(machine: str, verdict: dict, finding_factory) -> dict:
    """ONE pinned row per machine: its accounting is not this machine's word.

    Keyed on the machine ALONE — the fingerprint is the constant "unverified",
    exactly like unreadable_finding's "unreadable" — so a machine that flips
    between bad_signature and wrong_machine and no_fingerprint is one row with
    a moving last_seen and the current state in its evidence. The first version
    keyed on the STATE under a docstring saying this, and one unhappy machine
    could accumulate eight permanent rows, each counted new, each notified.
    """
    state = str(verdict.get("state") or "unverified")
    return finding_factory(
        machine=machine,
        kind="accounting-state",
        fingerprint="unverified",
        severity=_ACCOUNTING_SEVERITY.get(state, "high"),
        source="store",
        title=f"the accounting on {machine} is not verified: {state}",
        recommendation=(
            "Every finding from this machine is measured against this accounting, "
            "and the host cannot confirm the machine's own key signed it. Read the "
            "reason, then retake the accounting on the machine as an administrator "
            "(`privatae-bastion observe:baseline --force`) if it is yours."),
        evidence={k: verdict.get(k) for k in
                  ("state", "reason", "signed_by", "taken_at", "fingerprint")})


def _host_ca_finding(reason: str, finding_factory) -> dict:
    """The host has no CA to check accountings against. A fact about THIS pod,
    filed once under the host's own name, never against a machine."""
    return finding_factory(
        machine="this host",
        kind="accounting-ca",
        fingerprint="missing",
        severity="high",
        source="store",
        title="this host cannot verify any machine's accounting: no platform CA on the pod",
        recommendation=("The platform CA is expected at the path in the reason. Until it is "
                        "present, no accounting from any machine can be verified — the "
                        "machines are not at fault."),
        evidence={"reason": reason[:400]})


async def _verify_accounting(pool, entity_id, st, machine, finding_factory,
                             run_dcn_op, out, expected_fp, beat_taken_at=""):
    """Read baseline.json from the store and judge its signature.

    Missing is NOT a finding: a box that has never taken an accounting reports
    `baseline: none` on the beat and the heartbeat fold already raises that.
    What this raises is an accounting that EXISTS and is not the machine's word
    — unsigned on a box that could sign, signed by a certificate this CA never
    issued, signed for a different machine, or edited after signing — and, as
    a separate fact about the HOST, a pod with no CA to check against.

    SELF-CONTAINED. An earlier version left "could not stat/read baseline.json"
    as a dict entry and relied on _pull_one's list call failing "moments later"
    to raise a row — which is true only when BOTH fail. Every non-verified
    outcome here raises its own row now, and the order this runs in relative to
    the journal fold no longer matters.
    """
    from server.network.bastion_baseline import read_ca, verify_signed_baseline
    store_id = st["store_id"]
    stat = await run_dcn_op(pool, entity_id, store_id, "stat", {"path": "baseline.json"})
    if stat.get("state") == "gone":
        out["accounting"][machine] = "absent"
        return
    if stat.get("state") != "ok":
        verdict = {"state": "unreadable",
                   "reason": f"baseline.json could not be stat'ed ({stat.get('state')}: "
                             f"{stat.get('reason') or stat.get('detail') or 'no detail'})"}
        out["accounting"][machine] = "unreadable"
        out["findings"].append(_accounting_finding(machine, verdict, finding_factory))
        return
    size = int(stat.get("size_bytes") or 0)
    if size > ACCOUNTING_MAX_BYTES:
        verdict = {"state": "unreadable",
                   "reason": f"the accounting is {size} bytes, over the {ACCOUNTING_MAX_BYTES} "
                             "the host will read in one range"}
        out["accounting"][machine] = "unreadable"
        out["findings"].append(_accounting_finding(machine, verdict, finding_factory))
        return
    res = await run_dcn_op(pool, entity_id, store_id, "read_range",
                           {"path": "baseline.json", "start_byte": 0,
                            "end_byte": max(size, 1)})
    if res.get("state") != "ok" or res.get("content_b64") is None:
        verdict = {"state": "unreadable",
                   "reason": f"baseline.json could not be read ({res.get('state')}: "
                             f"{res.get('reason') or res.get('detail') or 'no content_b64'})"}
        out["accounting"][machine] = "unreadable"
        out["findings"].append(_accounting_finding(machine, verdict, finding_factory))
        return
    try:
        raw = base64.b64decode(res["content_b64"], validate=False)
    except (ValueError, TypeError):
        raw = b""
    verdict = verify_signed_baseline(raw, ca_pem=read_ca(), expected_fingerprint=expected_fp)
    out["accounting"][machine] = verdict["state"]
    if verdict["state"] == "no_ca":
        # Once per pass, not once per machine: the same missing file.
        if not out.get("_host_ca_raised"):
            out["_host_ca_raised"] = True
            out["findings"].append(_host_ca_finding(verdict["reason"], finding_factory))
        return
    if not verdict["ok"]:
        out["findings"].append(_accounting_finding(machine, verdict, finding_factory))
        return
    # VERIFIED — now, is it the accounting the box is actually using? The beat
    # carries the taken_at of the accounting the box compares against. If the
    # mirror the host just verified is a DIFFERENT accounting, then either the
    # mirror failed to update (stale but validly signed) or an older accounting
    # was restored under the box (rollback, validly signed). The signature
    # cannot tell either apart from the truth; the beat can.
    if _instants_differ(beat_taken_at, verdict.get("taken_at")):
        stale = {"state": "stale_mirror", "signed_by": verdict.get("signed_by"),
                 "taken_at": verdict.get("taken_at"), "fingerprint": verdict.get("fingerprint"),
                 "reason": (f"the mirrored accounting was taken {verdict['taken_at']} but the box "
                            f"reports comparing against one taken {beat_taken_at}; the mirror "
                            "is stale or an older accounting was restored on the machine")}
        out["accounting"][machine] = "stale_mirror"
        out["findings"].append(_accounting_finding(machine, stale, finding_factory))


async def _pull_one(pool, entity_id, st, machine, finding_factory, run_dcn_op, out):
    store_id = st["store_id"]
    listing = await run_dcn_op(pool, entity_id, store_id, "list",
                               {"prefix": FINDINGS_SHELF, "limit": 200})
    state = listing.get("state")
    if state != "ok":
        # offline / refused / gone — all of them mean THIS HOST DID NOT READ,
        # which is not the same as "nothing to read".
        why = (f"{state}: {listing.get('reason') or listing.get('detail') or 'no detail'}")
        out["problems"].append(f"{machine}: the machine record could not be listed ({why})")
        out["findings"].append(unreadable_finding(machine, why, finding_factory))
        return

    # `files`, the key the box's list handler actually sets (client-go/dcn.go:553).
    # Never `entries`, a key nothing sends — reading keys the peer never writes
    # is a bug class of its own.
    entries = listing.get("files") or []
    days, sealed_days = {}, set()
    for e in entries:
        name = (e.get("path") or e.get("name") or "") if isinstance(e, dict) else str(e)
        # size_bytes, NOT size. The box's list entry is `Size int64
        # json:"size_bytes"` (client-go/dcn.go:517). Reading `size` gave 0 for
        # every file, which meant a finished day never registered as finished
        # and was re-walked on every pass — burning a MAX_DAYS_PER_PASS slot
        # each time, so on a store with four retained days TODAY's file was
        # never reached at all, indefinitely, with no problem reported.
        size = int((e.get("size_bytes") if isinstance(e, dict) else 0) or 0)
        if name.endswith(".jsonl" + SEAL_SUFFIX):
            d = _day_of(name[: -len(SEAL_SUFFIX)])
            if d:
                sealed_days.add(d)
            continue
        day = _day_of(name)
        if day:
            days[day] = {"path": name, "size": size}

    cursors = await _cursors(pool, store_id, FINDINGS_SHELF)
    budget = MAX_BYTES_PER_STORE
    read_days = 0

    for day in sorted(days):
        if read_days >= MAX_DAYS_PER_PASS or budget <= 0:
            break
        cur = cursors.get(day) or {"offset": 0, "sealed": False}
        if cur["sealed"]:
            continue
        size = days[day]["size"]
        offset = int(cur["offset"])
        if size and offset >= size:
            # Finished. Seal it so it stops being considered at all, rather
            # than being re-listed and re-skipped for the next 30 days.
            if day != _utc_today():
                out["pending_cursors"].append(
                    (store_id, FINDINGS_SHELF, day, offset, True))
            continue
        read_days += 1
        path = days[day]["path"]
        day_incomplete = False

        while budget > 0:
            want = min(RANGE_BYTES, budget)
            res = await run_dcn_op(pool, entity_id, store_id, "read_range",
                                   {"path": path, "start_byte": offset,
                                    "end_byte": offset + want})
            if res.get("state") != "ok":
                why = (f"{res.get('state')}: "
                       f"{res.get('reason') or res.get('detail') or 'no detail'}")
                out["problems"].append(f"{machine}: {path} could not be read ({why})")
                out["findings"].append(unreadable_finding(machine, why, finding_factory))
                return
            # content_b64, NOT content. `content` is the CABINET envelope
            # (nac_dcn._run_cabinet_op); a machine store's box handler returns
            # base64 (client-go/dcn.go:655) and run_dcn_op passes the box's
            # payload through verbatim. Reading the wrong key folded every
            # journal to zero findings, recorded no problem, and never moved the
            # cursor — the exact "an unreachable machine must never read like a
            # quiet one" failure this module says it exists to prevent. It
            # survived the suite because the test's own fake invented `content`.
            b64 = res.get("content_b64")
            if b64 is None:
                why = (f"{path} came back without content_b64 — this host cannot "
                       "read that box's reply shape")
                out["problems"].append(f"{machine}: {why}")
                out["findings"].append(unreadable_finding(machine, why, finding_factory))
                return
            try:
                chunk = base64.b64decode(b64, validate=False)
            except (ValueError, TypeError) as e:
                why = f"{path} content was not base64 ({e})"
                out["problems"].append(f"{machine}: {why}")
                out["findings"].append(unreadable_finding(machine, why, finding_factory))
                return
            if not chunk:
                break
            findings, consumed, problems, incomplete = findings_from_journal(
                chunk, machine, finding_factory)
            out["findings"].extend(findings)
            out["problems"].extend(f"{machine}: {p}" for p in problems)
            day_incomplete = day_incomplete or incomplete or day in sealed_days
            if consumed <= 0:
                if len(chunk) >= want and want >= RANGE_BYTES:
                    # A FULL WINDOW WITH NO NEWLINE IN IT. The line is longer
                    # than the window, so waiting cannot help: every future pass
                    # re-reads the same bytes, never advances, and keeps burning
                    # a day slot for ever. Say so, seal the day, and RAISE A ROW
                    # — "the rest of that day is not being read" is exactly the
                    # kind of thing a problems string nobody consumes would hide.
                    #
                    # `want >= RANGE_BYTES` is the P3 guard: near the end of the
                    # budget `want` shrinks, an ordinary line fills it, and
                    # without this the day was sealed and its remainder lost.
                    why = (f"{path} has a line longer than the {want}-byte read "
                           f"window at byte {offset}; the rest of that day is not "
                           "being read")
                    out["problems"].append(f"{machine}: {why}")
                    out["findings"].append(unreadable_finding(machine, why, finding_factory))
                    out["pending_cursors"].append(
                        (store_id, FINDINGS_SHELF, day, offset, True))
                else:
                    # A short window with no complete line: the box is mid-write.
                    # The next pass picks it up once the newline lands.
                    out["problems"].append(
                        f"{machine}: {path} has an unterminated line at byte {offset}")
                break
            offset += consumed
            budget -= consumed
            out["bytes"] += consumed
            out["pending_cursors"].append(
                (store_id, FINDINGS_SHELF, day, offset,
                 bool(day in sealed_days and size and offset >= size)))
            got = len(chunk)
            if consumed < got:
                # The tail of what we read was a partial line; stop here and
                # resume from the cursor next pass.
                break
            if got < want:
                # A SHORT READ IS EOF. Without this the loop went round once
                # more and spent a second governed range-read and a second audit
                # row on every day of every store, on every pass.
                break

        # ONE DAY, ONE COUNT. This used to be incremented inside the read loop,
        # so a sealed day read in four chunks reported four incomplete days.
        if day_incomplete:
            out["incomplete_days"] += 1
