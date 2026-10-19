#!/usr/bin/env python3
"""Folding a machine's own findings journal into the ledger.

Runs as pytest (`pytest scripts/test_oversight_store.py`) AND as a plain script
(`APP_ROOT=<repo> python3 scripts/test_oversight_store.py`). No DB, no network:
everything under test is pure, which is why it was written that way.

WHAT THIS FILE IS DEFENDING. The bastion keeps its findings in an append-only
journal of TRANSITIONS — a line when a finding opens, changes, or closes —
because the detection pass runs every 60 seconds and a finding stays pinned
until a human resolves it. The host reads that file through a governed DCN
range-read, which means two things are true at once and both are hazards:

  * THE FILE IS BEING WRITTEN WHILE IT IS READ. A range routinely ends mid-line.
    Counting a partial line as read drops the rest of it permanently and
    silently — the exact failure the store exists to prevent.
  * THE RANGE IS A WINDOW, NOT THE WHOLE FILE. A finding's opening line may have
    been swept by retention long ago, which is why the box re-asserts open
    findings as `carried` at each UTC rollover, and why this fold must treat a
    `carried` line as authoritative rather than as an orphan.

The third hazard is quieter: a SEALED day. When a day-file hits its ceiling the
box stops writing and says so in band. A host that folds that day like any other
reads a truncated record as a complete one, which is the reassuring direction.
"""
import base64
import json
import os
import re
import sys

# Same two entries scripts/test_oversight_ledger.py:59 puts on the path: the
# repo root AND brain/, because brain/mastercode/runtime/__init__ imports its
# siblings as `mastercode.*` and fails on a bare repo-root path.
_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from mastercode.runtime.oversight_store import (  # noqa: E402
    findings_from_journal, machine_record_stores, parse_journal, replay,
)

# THE LITERAL, NOT THE MODULE'S CONSTANT. Importing LEVEL_FALLBACK and asserting
# severity == LEVEL_FALLBACK made the test agree with whatever the module said —
# setting the fallback to "notice" then made it assert the exact behaviour its
# own docstring forbids, and it passed. A test that imports the value it is
# checking is not checking anything.
EXPECTED_FALLBACK = "warn"

MACHINE = "box-a"


def _finding(machine, kind, fingerprint, severity, source, title, recommendation,
             evidence) -> dict:
    """oversight._finding's shape, without importing the ledger."""
    return {"key": f"{machine}|{kind}|{fingerprint}", "kind": kind, "machine": machine,
            "severity": severity, "source": source, "title": title,
            "recommendation": recommendation, "evidence": evidence}


def line(transition, fp, level="high", kind="proc_new", **extra):
    """One journal line in the shape client-go/machinestore.go writes."""
    ev = {"kind": kind, "level": level, "tier": "new", "fp": fp,
          "entry": {"fp": fp, "kind": "process", "exe": f"/usr/local/bin/{fp}",
                    "exe_digest": f"sha256:{fp}", "user": "root",
                    "argv_shape": f"{fp} --serve"},
          "first_seen": "2026-09-19T01:00:00Z"}
    out = {"at": "2026-09-19T10:00:00Z", "transition": transition, "machine": MACHINE,
           "version": "0.31.0", "baseline": "base-a", "event": ev}
    out.update(extra)
    return json.dumps(out)


def journal(*lines, complete=True):
    """The journal as BYTES, which is what the box's file holds."""
    text = "".join(l + "\n" for l in lines)
    if not complete and text:
        text = text[:-1]  # lop the final newline: a partial trailing line
    return text.encode("utf-8")


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
        return 0
    print(f"  FAIL  {name}{(': ' + detail) if detail else ''}")
    return 1


# ── the partial-line rule ───────────────────────────────────────────────────

def test_a_partial_trailing_line_is_never_consumed():
    whole = journal(line("opened", "aaa"), line("opened", "bbb"))
    cut = whole[:-20]  # mid-record, as a live range-read routinely lands
    lines, consumed, problems = parse_journal(cut)
    assert len(lines) == 1, f"a partial line was parsed as whole: {lines}"
    assert not problems, problems
    # The cursor must stop at the byte after the LAST COMPLETE line, so the next
    # read starts at the beginning of the partial one and loses nothing.
    first_len = len((line("opened", "aaa") + "\n").encode("utf-8"))
    assert consumed == first_len, f"consumed {consumed}, want {first_len}"


def test_the_offset_is_bytes_not_characters():
    """A multi-byte character must not desynchronise the cursor.

    The content arrives already decoded, so measuring len() of the string would
    under-count every non-ASCII byte and the next read would start mid-record —
    and then every line after it in the file is garbage, for ever.
    """
    ev = json.loads(line("opened", "aaa"))
    ev["event"]["entry"]["exe"] = "/usr/local/bin/naïve–dash"  # 2- and 3-byte runes
    # ensure_ascii=False, because Go's encoding/json does NOT escape non-ASCII —
    # it writes raw UTF-8 — so a real journal line genuinely carries these bytes.
    # Python's default would escape them to \uXXXX and this test would measure
    # an all-ASCII string, which is what the guard below caught the first time.
    text = json.dumps(ev, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    _, consumed, _ = parse_journal(data)
    assert consumed == len(data), (
        f"consumed {consumed} but the file holds {len(data)} bytes")
    assert len(data) != len(text), "the fixture has no multi-byte character in it"


# ── the replay ──────────────────────────────────────────────────────────────

def test_a_closed_finding_is_absent_from_the_open_set():
    lines, _, _ = parse_journal(journal(
        line("opened", "aaa"), line("opened", "bbb"), line("closed", "aaa")))
    open_by_fp, problems, incomplete = replay(lines, MACHINE)
    assert set(open_by_fp) == {"bbb"}, f"open set is {sorted(open_by_fp)}"
    assert not problems and not incomplete


def test_the_last_transition_wins_and_order_is_respected():
    """The replay's answer is the state at the END of the range.

    TWO ASSERTIONS WITH NO CLOSE BETWEEN THEM, which is what makes this able to
    fail. The first version of this test went opened -> closed -> opened, and a
    mutation that kept the FIRST line instead of the last survived it: the close
    emptied the slot in between, so either rule produced the same answer. An
    escalation is the real case anyway — a notice that became a critical must
    be read as a critical.
    """
    lines, _, _ = parse_journal(journal(
        line("opened", "aaa", level="notice"),
        line("changed", "aaa", level="critical")))
    open_by_fp, _, _ = replay(lines, MACHINE)
    assert open_by_fp["aaa"]["event"]["level"] == "critical", (
        f"the fold kept {open_by_fp['aaa']['event']['level']} — an escalation was lost")

    # And a reopen after a close still lands open.
    lines, _, _ = parse_journal(journal(
        line("opened", "aaa"), line("closed", "aaa"), line("opened", "aaa", level="critical")))
    open_by_fp, _, _ = replay(lines, MACHINE)
    assert "aaa" in open_by_fp, "a reopened finding was left closed"


def test_a_day_open_snapshot_never_produces_a_finding():
    """THE RESURRECTION GUARD.

    The box writes one day_open line per UTC day naming everything still open.
    If that produced ledger findings, every rollover would re-assert a finding
    the operator had already RESOLVED here: the upsert clears resolved_at,
    merge_findings counts it new, and an inbox row says so. Daily, for ever.
    The snapshot is for a human reading a truncated day-file; the ledger already
    holds the durable state.
    """
    snap = json.dumps({"at": "2026-09-19T00:00:01Z", "transition": "day_open",
                       "machine": MACHINE, "version": "0.31.0",
                       "open_count": 2, "open_fps": ["old-one", "old-two"]})
    findings, _, problems, _ = findings_from_journal(journal(snap), MACHINE, _finding)
    assert findings == [], f"a snapshot manufactured {len(findings)} finding(s)"
    assert not problems, f"a snapshot was treated as a problem: {problems}"


def test_a_snapshot_is_recognised_rather_than_unknown():
    """The distinguishing check: an UNRECOGNISED transition is a problem, so a
    day_open line that produced none proves the vocabulary knows it. (Asserting
    `asserted == {}` would be true whether or not day_open is handled at all.)"""
    snap = json.dumps({"at": "2026-09-19T00:00:01Z", "transition": "day_open",
                       "machine": MACHINE, "open_count": 1, "open_fps": ["old-one"]})
    lines, _, _ = parse_journal(journal(snap))
    asserted, problems, _ = replay(lines, MACHINE)
    assert asserted == {}
    assert problems == [], f"day_open was treated as unknown: {problems}"
    # And the SAME line with a made-up name IS a problem — so the assertion
    # above can fail.
    bogus = snap.replace('"day_open"', '"day_something"')
    lines, _, _ = parse_journal(journal(bogus))
    _, problems, _ = replay(lines, MACHINE)
    assert problems, "the fixture's control case did not fire, so this test proves nothing"


def test_a_sealed_day_is_reported_incomplete_and_is_not_a_finding():
    text = journal(line("opened", "aaa"),
                   json.dumps({"at": "2026-09-19T11:00:00Z", "transition": "day_capped",
                               "machine": MACHINE, "version": "0.31.0",
                               "note": "this day-file reached its ceiling"}))
    lines, _, _ = parse_journal(text)
    open_by_fp, _, incomplete = replay(lines, MACHINE)
    assert incomplete, "a sealed day read as a complete one"
    assert set(open_by_fp) == {"aaa"}, "the cap notice became a finding"


def test_an_unknown_transition_is_reported_not_silently_dropped():
    """A newer box writing a transition this host cannot read is a REAL state.

    Skipping it silently would let a future box version write findings into a
    host that drops them without anybody learning.
    """
    text = journal(line("quarantined", "aaa"))
    lines, _, _ = parse_journal(text)
    _, problems, _ = replay(lines, MACHINE)
    assert any("quarantined" in p for p in problems), f"problems were {problems}"


# ── grading ─────────────────────────────────────────────────────────────────

def test_an_unrecognised_level_is_not_dropped_and_not_downgraded():
    """Grading an unfamiliar level `notice` is how a critical goes quiet."""
    findings, _, _, _ = findings_from_journal(
        journal(line("opened", "aaa", level="catastrophic")), MACHINE, _finding)
    assert len(findings) == 1, findings
    f = findings[0]
    assert f["severity"] == EXPECTED_FALLBACK, f"graded {f['severity']}"
    assert "level_unrecognised" in f["evidence"], "the fallback was silent"
    assert "catastrophic" in f["evidence"]["level_unrecognised"]


def test_levels_map_one_for_one():
    for level in ("notice", "warn", "high", "critical"):
        findings, _, _, _ = findings_from_journal(
            journal(line("opened", "x", level=level)), MACHINE, _finding)
        assert findings[0]["severity"] == level, f"{level} graded {findings[0]['severity']}"
        assert "level_unrecognised" not in findings[0]["evidence"]


def test_a_finding_carries_the_boxs_evidence_and_both_first_seens():
    findings, _, _, _ = findings_from_journal(
        journal(line("opened", "aaa")), MACHINE, _finding)
    ev = findings[0]["evidence"]
    assert ev["exe"] == "/usr/local/bin/aaa", ev
    assert ev["exe_digest"] == "sha256:aaa", ev
    # The BOX's first sighting, which is not the host's — the ledger row keeps
    # its own. Both numbers matter and they are different.
    assert ev["machine_first_seen"] == "2026-09-19T01:00:00Z", ev
    assert findings[0]["source"] == "store", "provenance must be distinguishable"
    assert findings[0]["kind"] == "detect:proc_new"


# ── robustness ──────────────────────────────────────────────────────────────

def test_one_corrupt_line_does_not_stop_the_fold():
    text = (line("opened", "aaa").encode() + b"\n" + b"{not json at all\n"
            + line("opened", "bbb").encode() + b"\n")
    findings, consumed, problems, _ = findings_from_journal(text, MACHINE, _finding)
    assert len(findings) == 2, f"a corrupt line cost {2 - len(findings)} good ones"
    assert problems, "a corrupt line was swallowed without a word"
    assert consumed == len(text), "the corrupt line was not consumed"


def test_a_line_with_no_fingerprint_is_a_problem_not_a_finding():
    bad = json.dumps({"at": "2026-09-19T10:00:00Z", "transition": "opened",
                      "machine": MACHINE, "event": {"kind": "proc_new", "level": "high"}})
    findings, _, problems, _ = findings_from_journal(journal(bad), MACHINE, _finding)
    assert not findings, findings
    assert any("fingerprint" in p for p in problems), problems


def test_an_empty_range_folds_to_nothing_without_complaint():
    findings, consumed, problems, incomplete = findings_from_journal("", MACHINE, _finding)
    assert findings == [] and consumed == 0 and problems == [] and not incomplete




# ── the read side ───────────────────────────────────────────────────────────
#
# Driven against a fake pool and a fake run_dcn_op, because the rules that cost
# a finding live in the CURSOR arithmetic and in what happens on a non-ok state.

class FakePool:
    """Enough asyncpg surface for the cursor reads and writes."""

    def __init__(self, stores):
        self.stores = stores
        self.cursors = {}

    async def fetch(self, sql, *args):
        if "dcn_stores" in sql:
            # THE FAKE APPLIES THE FILTERS THE SQL ASKS FOR, and only those.
            # Returning every row unconditionally made two of this module's own
            # stated properties untestable: deleting `purpose = 'machine_record'`
            # (the ingest then trawls every folder the user granted) and
            # deleting the grant-state clause (it reads revoked stores) both
            # survived the suite. A fake that ignores the query cannot test the
            # query.
            rows = list(self.stores)
            if "purpose = 'machine_record'" in sql:
                rows = [r for r in rows if r.get("purpose") == "machine_record"]
            if "approved_at IS NOT NULL" in sql:
                rows = [r for r in rows if r.get("approved")]
            if "revoked_at IS NULL" in sql:
                rows = [r for r in rows if not r.get("revoked")]
            if "expires_at > now()" in sql:
                rows = [r for r in rows if not r.get("expired")]
            if "ORDER BY id" in sql:
                rows = sorted(rows, key=lambda r: r["store_id"])
            return [{"id": r["store_id"], "server": r["server"], "root": r["root"]}
                    for r in rows]
        if "oversight_store_cursor" in sql:
            sid, shelf = args[0], args[1]
            return [{"day": d, "byte_offset": v["offset"], "sealed": v["sealed"]}
                    for (s, sh, d), v in self.cursors.items() if s == sid and sh == shelf]
        raise AssertionError(f"unexpected fetch: {sql[:60]}")

    async def execute(self, sql, *args):
        assert "oversight_store_cursor" in sql, sql[:60]
        sid, shelf, day, off, sealed = args
        self.cursors[(sid, shelf, day)] = {"offset": off, "sealed": sealed}


def fake_dcn(files, fail_on=None, fail_state="offline"):
    """files: {path: content_bytes}. fail_on: a path whose read returns fail_state."""
    calls = []

    async def run(pool, eid, store_id, op, args=None):
        args = args or {}
        calls.append((op, dict(args)))
        if op == "list":
            # size_bytes and content_b64 are the BOX's shape
            # (client-go/dcn.go:517 and :655). The first version of this fake
            # invented `size` and `content`, which are the CABINET envelope — so
            # all twenty tests passed against a wire shape the box does not
            # speak, and the real fold returned zero findings for ever.
            # `files`, not `entries` — client-go/dcn.go:553. The read_range fake
            # had been corrected to the box's shape and this one had
            # not, so the live arm of the list op was exercised by nothing.
            return {"state": "ok", "store_id": store_id, "op": op,
                    "files": [{"path": p, "size_bytes": len(c)}
                              for p, c in files.items()], "count": len(files)}
        if op == "stat":
            path = args["path"]
            if path not in files:
                return {"state": "gone", "store_id": store_id, "op": op, "path": path}
            return {"state": "ok", "store_id": store_id, "op": op, "path": path,
                    "size_bytes": len(files[path])}
        if op == "read_range":
            path = args["path"]
            if fail_on is not None and path == fail_on:
                return {"state": fail_state, "store_id": store_id, "op": op,
                        "detail": "the box did not answer"}
            body = files[path]
            start, end = int(args["start_byte"]), int(args["end_byte"])
            return {"state": "ok", "store_id": store_id, "op": op, "path": path,
                    "start_byte": start, "size_bytes": len(body),
                    "content_b64": base64.b64encode(body[start:end]).decode()}
        raise AssertionError(f"unexpected op {op}")

    run.calls = calls
    return run


def _pull_out(pool, dcn):
    """Run _pull_one and return the out dict it filled."""
    import asyncio

    from mastercode.runtime import oversight_store as osx
    out = {"findings": [], "problems": [], "stores": 1, "bytes": 0,
           "incomplete_days": 0, "pending_cursors": [], "skipped": "", "accounting": {}}
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            osx._pull_one(pool, 3, pool.stores[0], pool.stores[0]["server"],
                          _finding, dcn, out))
        # The caller commits cursors AFTER the ledger write; do the same here so
        # these tests measure the committed state rather than the intent.
        loop.run_until_complete(osx.commit_cursors(pool, out["pending_cursors"]))
    finally:
        loop.close()
    return out


STORE = [{"store_id": 7, "server": "box-a",
          "root": "/var/lib/privatae-bastion/store",
          "purpose": "machine_record", "approved": True, "revoked": False}]


def test_the_cursor_stops_at_the_last_complete_line():
    """A box appending while the host reads is the NORMAL case."""
    whole = journal(line("opened", "aaa"), line("opened", "bbb"))
    partial = whole[:-15]  # the file as it stands mid-write
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-19.jsonl": partial})
    out = _pull_out(pool, dcn)
    assert len(out["findings"]) == 1, f"folded {len(out['findings'])}, want 1"
    saved = pool.cursors[(7, "findings", "2026-09-19")]["offset"]
    complete_len = len((line("opened", "aaa") + "\n").encode())
    assert saved == complete_len, (
        f"cursor at {saved}, want {complete_len} — advancing past a partial line "
        "drops the rest of it for good")


def test_a_second_pass_resumes_and_does_not_refold():
    pool = FakePool(STORE)
    whole = journal(line("opened", "aaa"), line("opened", "bbb"))
    first = whole[:-15]
    out1 = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": first}))
    assert len(out1["findings"]) == 1
    # The box finished the line; the host reads again from its cursor.
    out2 = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": whole}))
    assert len(out2["findings"]) == 1, (
        f"the second pass folded {len(out2['findings'])} — it refolded the first line")
    assert out2["findings"][0]["evidence"]["fp"] == "bbb"


def test_an_offline_machine_is_a_problem_not_an_empty_result():
    """The failure the whole detection plane exists to avoid.

    An unreachable machine must never fold like a quiet one, and its cursor must
    not move — otherwise the bytes it could not read are skipped for ever.
    """
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa"))},
                   fail_on="findings/2026-09-19.jsonl", fail_state="offline")
    out = _pull_out(pool, dcn)
    # It produces exactly ONE row, and that row is the "could not read" marker —
    # never a journal finding, because nothing was read.
    assert all(f["kind"] == "store-state" for f in out["findings"]), (
        f"an offline read produced journal findings: {out['findings']}")
    assert out["problems"], "an offline machine folded silently as quiet"
    assert "offline" in out["problems"][0]
    assert (7, "findings", "2026-09-19") not in pool.cursors, (
        "the cursor advanced past bytes that were never read")


def test_a_revoked_store_is_a_problem_not_an_empty_result():
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa"))},
                   fail_on="findings/2026-09-19.jsonl", fail_state="refused")
    out = _pull_out(pool, dcn)
    assert out["problems"] and "refused" in out["problems"][0], out["problems"]
    # A refused read yields the "could not read" row and no journal findings.
    assert all(f["kind"] == "store-state" for f in out["findings"]), out["findings"]
    assert (7, "findings", "2026-09-19") not in pool.cursors, (
        "a refused read advanced the cursor")


def test_a_sealed_day_is_counted_incomplete():
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa")),
                    "findings/2026-09-19.jsonl.capped": b""})
    out = _pull_out(pool, dcn)
    assert out["incomplete_days"] == 1, (
        "a day the box sealed at its ceiling read as a complete one")


def test_only_journal_filenames_are_read():
    """A stray file in the shelf must not be parsed as a journal."""
    pool = FakePool(STORE)
    # THE LAST ONE IS WHAT MAKES THIS ABLE TO FAIL. README and notes.txt are
    # rejected by the date-shape check alone, so removing the .jsonl check left
    # this test green. A DATE-SHAPED name with no extension is the case only the
    # extension check catches — and it is a realistic stray: a directory, or a
    # half-written file beside the real one.
    dcn = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa")),
                    "findings/README": b"not a journal at all",
                    "findings/notes.txt": b"{}",
                    "findings/2026-09-17": b"not a journal either"})
    out = _pull_out(pool, dcn)
    read = [a["path"] for op, a in dcn.calls if op == "read_range"]
    assert read == ["findings/2026-09-19.jsonl"], f"it read {read}"
    assert not out["problems"], out["problems"]


def test_days_are_folded_oldest_first():
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-18.jsonl": journal(line("opened", "old")),
                    "findings/2026-09-19.jsonl": journal(line("opened", "new"))})
    _pull_out(pool, dcn)
    read = [a["path"] for op, a in dcn.calls if op == "read_range"]
    assert read[0] == "findings/2026-09-18.jsonl", (
        f"folded out of order: {read} — a journal replayed backwards inverts "
        "every open/close pair")




# ── which stores are read at all ────────────────────────────────────────────

def _stores(pool):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(machine_record_stores(pool, 3))
    finally:
        loop.close()


def test_only_machine_record_stores_are_read():
    """The ingest must NEVER walk a folder the user added.

    v162 makes this a column rather than a path heuristic precisely so it cannot
    become a guess — and the guess would fail in the direction where the host
    reads someone's documents looking for findings.
    """
    pool = FakePool([
        dict(STORE[0]),
        {"store_id": 8, "server": "laptop", "root": "/home/alex/Documents",
         "purpose": None, "approved": True, "revoked": False},
    ])
    got = [s["store_id"] for s in _stores(pool)]
    assert got == [7], f"the ingest would read {got} — 8 is the user's Documents"


def test_a_revoked_or_unapproved_store_is_not_read():
    """A revoke must actually stop the reading."""
    pool = FakePool([
        {"store_id": 9, "server": "old-box", "root": "/x",
         "purpose": "machine_record", "approved": True, "revoked": True},
        {"store_id": 10, "server": "draft-box", "root": "/y",
         "purpose": "machine_record", "approved": False, "revoked": False},
    ])
    got = [s["store_id"] for s in _stores(pool)]
    assert got == [], f"the ingest would read revoked/unapproved stores {got}"


# ── bounds ──────────────────────────────────────────────────────────────────

def test_the_byte_budget_bounds_one_pass():
    """Without the budget a first read of a busy store is unbounded.

    THE FIXTURE MUST REACH THE BUDGET CODE. The read loop stops after ONE window
    whenever that window ends mid-line (`consumed < got`), so with ordinary
    fixtures only a single 512 KiB read was ever issued and `budget -= consumed`
    was unreachable with data remaining — deleting it passed. Measured: exactly
    one window was asked. Here every line is a fixed length and the
    window is set to a whole multiple of it, so windows end on newlines, the
    loop continues, and the budget is what stops it.
    """
    from mastercode.runtime import oversight_store as osx
    one = line("opened", "f00000").encode() + b"\n"
    L = len(one)
    lines_ = [line("opened", f"f{i:05d}").encode() + b"\n" for i in range(4000)]
    assert all(len(x) == L for x in lines_), "fixture lines are not fixed-length"
    big = b"".join(lines_)

    orig_range, orig_budget = osx.RANGE_BYTES, osx.MAX_BYTES_PER_STORE
    try:
        osx.RANGE_BYTES = L * 100          # exactly 100 lines per window
        osx.MAX_BYTES_PER_STORE = L * 350  # 3.5 windows: the budget, not EOF, must stop it
        pool = FakePool(STORE)
        dcn = fake_dcn({"findings/2026-09-19.jsonl": big})
        out = _pull_out(pool, dcn)
        windows = [a for op, a in dcn.calls if op == "read_range"]
        assert len(windows) >= 3, f"only {len(windows)} window(s) asked — the budget code was never reached"
        assert out["bytes"] <= L * 350, f"read {out['bytes']} bytes past the budget"
        assert out["bytes"] >= L * 300, f"read only {out['bytes']} — stopped long before the budget"
        assert pool.cursors[(7, "findings", "2026-09-19")]["offset"] == out["bytes"]
        # Nothing was lost: the next pass resumes exactly where this one stopped.
        out2 = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": big}))
        assert out2["bytes"] > 0 and out2["findings"][0]["evidence"]["fp"] == f"f{out['bytes'] // L:05d}"
    finally:
        osx.RANGE_BYTES, osx.MAX_BYTES_PER_STORE = orig_range, orig_budget


def test_at_most_four_days_are_walked_in_one_pass():
    from mastercode.runtime import oversight_store as osx
    pool = FakePool(STORE)
    files = {f"findings/2026-09-{d:02d}.jsonl": journal(line("opened", f"d{d}"))
             for d in range(10, 20)}
    dcn = fake_dcn(files)
    _pull_out(pool, dcn)
    days_read = {a["path"] for op, a in dcn.calls if op == "read_range"}
    # THE LITERAL. Reading the bound from the module under test made this the
    # same tautology as the level-fallback test: set it to 400 and the assertion
    # became 10 <= 400 and passed. Four is the number the plan derives; if it
    # changes, this line changes with it, on purpose.
    assert len(days_read) == 4, f"walked {len(days_read)} days, want exactly 4 of 10"


def test_a_line_longer_than_the_window_does_not_stall_the_day_forever():
    """A newline-free window cannot be waited out: the line exceeds the window,
    so every future pass re-reads the same bytes and burns a day slot for ever."""
    from mastercode.runtime import oversight_store as osx
    pool = FakePool(STORE)
    monster = b"x" * (osx.RANGE_BYTES + 1000)  # no newline anywhere
    out = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": monster}))
    assert any("longer than" in p for p in out["problems"]), out["problems"]
    assert pool.cursors[(7, "findings", "2026-09-19")]["sealed"] is True, (
        "the stuck day was not sealed and will burn a slot on every pass")


def test_an_oversized_line_is_skipped_not_folded():
    from mastercode.runtime import oversight_store as osx
    huge = json.dumps({"at": "x", "transition": "opened", "machine": MACHINE,
                       "event": {"kind": "proc_new", "level": "high", "fp": "big",
                                 "entry": {"exe": "/x" * osx._LINE_MAX}}})
    findings, _, problems, _ = findings_from_journal(journal(huge), MACHINE, _finding)
    assert findings == [], "an oversized line was folded"
    assert any("oversized" in p for p in problems), problems


def test_parse_problems_reach_the_caller():
    """A corrupt line must be REPORTED, not dropped on the floor."""
    text = journal(line("opened", "aaa")) + b"{not json\n"
    _, _, problems, _ = findings_from_journal(text, MACHINE, _finding)
    assert problems, "a corrupt line produced no problem"


# ── the wire shape ──────────────────────────────────────────────────────────

def test_a_reply_without_content_b64_is_a_problem_not_silence():
    """The cabinet envelope arriving from a machine store must be LOUD.

    Reading `content` instead of `content_b64` folded every journal to zero
    findings with no problem and no cursor movement — a critical vanishing in
    total silence. If the key is ever missing again, it must say so.
    """
    pool = FakePool(STORE)

    async def cabinet_shaped(pool_, eid, store_id, op, args=None):
        if op == "list":
            return {"state": "ok", "files": [
                {"path": "findings/2026-09-19.jsonl", "size_bytes": 100}]}
        return {"state": "ok", "path": args["path"], "start_byte": 0,
                "content": "pretending to be a cabinet"}

    out = _pull_out(pool, cabinet_shaped)
    assert out["findings"], "a wrong reply shape produced no finding and no row"
    assert any("content_b64" in p for p in out["problems"]), out["problems"]


def test_an_unreadable_record_produces_a_ledger_row():
    """store_problems is a dict key nobody reads. A human looks at rows."""
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa"))},
                   fail_on="findings/2026-09-19.jsonl", fail_state="offline")
    out = _pull_out(pool, dcn)
    rows = [f for f in out["findings"] if f["kind"] == "store-state"]
    assert len(rows) == 1, f"an unreadable record produced {len(rows)} rows"
    assert rows[0]["severity"] == "high"
    assert rows[0]["machine"] == "box-a"


def test_the_cursor_counts_real_bytes_not_re_encoded_ones():
    """A byte the decode replaced re-encodes to three, running the cursor AHEAD
    of the file — after which every later line in that day is read at the wrong
    offset. Working on real bytes is what removes the class."""
    bad = line("opened", "aaa").encode() + b"\n"
    # Two invalid UTF-8 bytes spliced into an otherwise fine line's tail.
    corrupt = bad[:-1] + b"\xff\xfe" + b"\n"
    lines, consumed, _ = parse_journal(corrupt)
    assert consumed == len(corrupt), (
        f"consumed {consumed} of {len(corrupt)} real bytes — the cursor desynced")




# ── budget, seal and force ──────────────────────────────────────────────────

def test_a_budget_shortened_window_never_seals_the_day():
    """Near the end of the budget `want` shrinks; an ORDINARY line then fills
    the whole window with no newline in it, and the seal — meant for a line
    longer than a full 512 KiB window — fired and lost the rest of the day."""
    from mastercode.runtime import oversight_store as osx
    one = line("opened", "f00000").encode() + b"\n"
    L = len(one)
    big = b"".join(line("opened", f"f{i:05d}").encode() + b"\n" for i in range(50))
    orig_range, orig_budget = osx.RANGE_BYTES, osx.MAX_BYTES_PER_STORE
    try:
        osx.RANGE_BYTES = L * 10
        osx.MAX_BYTES_PER_STORE = L * 10 + (L // 2)  # the second window is half a line
        pool = FakePool(STORE)
        out = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": big}))
        cur = pool.cursors[(7, "findings", "2026-09-19")]
        assert cur["sealed"] is False, "an ordinary line in a budget-shortened window sealed the day"
        assert not any("longer than" in p for p in out["problems"]), out["problems"]
    finally:
        osx.RANGE_BYTES, osx.MAX_BYTES_PER_STORE = orig_range, orig_budget


def test_todays_file_is_never_sealed_as_finished():
    """Dropping the today-guard seals today's file the moment the host catches up
    mid-day, and every finding written for the rest of the day is lost."""
    from mastercode.runtime import oversight_store as osx
    today = osx._utc_today()
    pool = FakePool(STORE)
    content = journal(line("opened", "aaa"))
    _pull_out(pool, fake_dcn({f"findings/{today}.jsonl": content}))
    # Caught up: offset == size. A second pass must NOT seal today.
    _pull_out(pool, fake_dcn({f"findings/{today}.jsonl": content}))
    cur = pool.cursors[(7, "findings", today)]
    assert cur["sealed"] is False, "today's file was sealed while the box is still writing it"
    # …whereas a finished YESTERDAY is sealed on the second pass.
    pool2 = FakePool(STORE)
    _pull_out(pool2, fake_dcn({"findings/2020-01-01.jsonl": content}))
    _pull_out(pool2, fake_dcn({"findings/2020-01-01.jsonl": content}))
    assert pool2.cursors[(7, "findings", "2020-01-01")]["sealed"] is True


def test_an_oversized_line_raises_a_row_not_just_a_string():
    """'The rest of that day is not being read' must be a ROW."""
    from mastercode.runtime import oversight_store as osx
    pool = FakePool(STORE)
    monster = b"x" * (osx.RANGE_BYTES + 1000)
    out = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": monster}))
    rows = [f for f in out["findings"] if f["kind"] == "store-state"]
    assert rows, "the seal path recorded only a problems string"


def test_an_unreadable_machine_is_one_pinned_row_not_a_pile():
    """Two different reasons on two passes must land on ONE key."""
    pool = FakePool(STORE)
    f = journal(line("opened", "aaa"))
    o1 = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": f},
                                  fail_on="findings/2026-09-19.jsonl", fail_state="offline"))
    o2 = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": f},
                                  fail_on="findings/2026-09-19.jsonl", fail_state="refused"))
    k1 = [x["key"] for x in o1["findings"] if x["kind"] == "store-state"]
    k2 = [x["key"] for x in o2["findings"] if x["kind"] == "store-state"]
    assert k1 == k2 and len(k1) == 1, f"different reasons produced different keys: {k1} vs {k2}"


def test_parse_problems_reach_the_pull_result():
    """Through _pull_one, not the pure function: the plumbing is what was untested."""
    pool = FakePool(STORE)
    text = journal(line("opened", "aaa")) + b"{not json\n"
    out = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": text}))
    assert any("not valid JSON" in p for p in out["problems"]), out["problems"]


def _pull_store(pool, force):
    import asyncio

    from mastercode.runtime import oversight_store as osx
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(osx.pull_store_findings(pool, 3, _finding, force=force))
    finally:
        loop.close()


def test_the_poll_gate_never_blocks_a_forced_read():
    """The wake and a hand-run pass force=True; the gate must not apply."""
    from mastercode.runtime import oversight_store as osx
    orig = osx.STORE_POLL_EVERY_N_PASSES
    osx.STORE_POLL_EVERY_N_PASSES = 1000  # the scheduled pass would almost never read
    try:
        pool = FakePool(STORE)
        # No run_dcn_op is reachable from here (it imports the real one), so use
        # the skip marker as the observable: a forced pull must not be skipped.
        out = _pull_store(pool, force=True)
        assert out["skipped"] == "", "a forced read was gated by the poll clock"
    finally:
        osx.STORE_POLL_EVERY_N_PASSES = orig


def test_the_poll_gate_cannot_silently_never_fire():
    """The arithmetic: with N=3 exactly four of the twelve 5-minute buckets read.
    A gate that fires for zero buckets is the failure named in the code."""
    from datetime import datetime, timezone
    from unittest import mock

    from mastercode.runtime import oversight_store as osx
    import asyncio
    fired = 0
    for minute in range(0, 60, 5):
        fake_now = datetime(2026, 9, 19, 10, minute, tzinfo=timezone.utc)
        with mock.patch.object(osx, "datetime") as dt:
            dt.now.return_value = fake_now
            loop = asyncio.new_event_loop()
            try:
                if loop.run_until_complete(osx._due_this_pass(None, 3)):
                    fired += 1
            finally:
                loop.close()
    assert fired == 4, f"the gate fires for {fired} of 12 buckets, want 4"


def test_the_scheduled_pass_is_gated_and_the_wake_is_not():
    """Through _exec_oversight_ingest's own rule, without the pool: the marker
    decides. A seeded row from before the marker existed carries {} and reads
    every pass — the safe direction."""
    rule = lambda a: bool(a.get("wake")) or not bool(a.get("scheduled"))  # noqa: E731
    assert rule({"wake": True}) is True
    assert rule({}) is True, "a pre-marker seeded row must read every pass"
    assert rule({"scheduled": True}) is False, "the scheduled pass must be gated"
    assert rule({"scheduled": True, "wake": True}) is True
    # And the rule in the source is the rule tested here.
    src = open(os.path.join(_ROOT, "brain", "mastercode", "runtime", "oversight.py"),
               encoding="utf-8").read()
    assert 'force_store = bool(a.get("wake")) or not bool(a.get("scheduled"))' in src, (
        "the force rule in oversight.py no longer matches the one this test pins")
    assert '"args": {"scheduled": True}' in src, "the seeded task no longer carries the marker"

def test_run_oversight_ingest_carries_force_and_the_store_findings_to_the_plan():
    """THE SEAM. The worst defects in this lane lived on
    run_oversight_ingest — its wiring, a dropped `force` — and no test held
    it. The two mutations that restore those bugs (drop
    `force=force_store`; delete `derived.extend(pulled["findings"])`) both
    passed 40/40, because the only test here stubbed the pull with no findings
    and never recorded `force`. This one records both and follows the finding
    through to the plan apply_findings is handed.
    """
    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight as ov
    from mastercode.runtime import oversight_store as osx
    from server.network import topology

    seen_force = []
    sentinel = _finding("box-a", "detect:proc_new", "SENTINEL", "critical",
                        "store", "sentinel", "", {"fp": "SENTINEL"})

    async def fake_pull(pool, eid, factory, force=False, **kw):
        seen_force.append(force)
        return {"findings": [sentinel], "problems": [], "stores": 1, "bytes": 1,
                "incomplete_days": 0, "pending_cursors": [], "skipped": ""}

    async def no_bastions(pool, eid):
        return []

    class LedgerPool:
        async def fetch(self, sql, *a):
            return []

    plans = []

    async def fake_apply(pool, plan):
        plans.append(plan)
        return {"written": len(plan.get("new_keys", [])), "failed": 0}

    def run(force_store):
        with mock.patch.object(osx, "pull_store_findings", fake_pull), \
             mock.patch.object(topology, "list_bastions", no_bastions), \
             mock.patch.object(ov, "apply_findings", fake_apply):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    ov.run_oversight_ingest(LedgerPool(), 3, force_store=force_store))
            finally:
                loop.close()

    res = run(force_store=True)
    assert seen_force == [True], f"force_store=True reached the pull as {seen_force}"
    assert sentinel["key"] in plans[-1]["new_keys"], (
        f"the store finding never reached the plan: {plans[-1]}")
    assert res["store_findings"] == 1
    run(force_store=False)
    assert seen_force == [True, False], f"force_store=False reached the pull as {seen_force[-1]}"


def test_cursors_are_not_committed_when_any_row_failed_to_write():
    """apply_findings swallows per-row failures into a count; committing the
    cursors past a row that did not land loses that finding for good."""
    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight as ov
    from mastercode.runtime import oversight_store as osx
    from server.network import topology

    pending = [(7, "findings", "2026-09-19", 401, False)]

    async def fake_pull(pool, eid, factory, force=False, **kw):
        return {"findings": [], "problems": [], "stores": 1, "bytes": 401,
                "incomplete_days": 0, "pending_cursors": list(pending), "skipped": ""}

    async def no_bastions(pool, eid):
        return []

    class LedgerPool:
        async def fetch(self, sql, *a):
            return []

    committed = []

    async def record_commit(pool, pend):
        committed.append(list(pend))
        return len(pend)

    def run(failed):
        async def fake_apply(pool, plan):
            return {"written": 0, "failed": failed}
        committed.clear()
        with mock.patch.object(osx, "pull_store_findings", fake_pull), \
             mock.patch.object(osx, "commit_cursors", record_commit), \
             mock.patch.object(topology, "list_bastions", no_bastions), \
             mock.patch.object(ov, "apply_findings", fake_apply):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(ov.run_oversight_ingest(LedgerPool(), 3))
            finally:
                loop.close()

    run(failed=1)
    assert committed == [], f"cursors were committed past a failed row: {committed}"
    run(failed=0)
    assert committed == [pending], f"a clean write did not commit the cursors: {committed}"


def test_a_sealed_day_is_never_read_again():
    """v162's own comment: 'A sealed day whose offset reached EOF is never read
    again.' Without the sealed check it is re-read on every pass for 30 days.

    SEALED MID-FILE, NOT AT EOF. A day sealed after being fully read is also
    caught by the finished-day skip (offset >= size), so a test using that
    shape cannot tell the two checks apart — the mutation removing the sealed
    check survived it. The oversized-line seal leaves offset < size, and only
    the sealed check stops that day being re-read.
    """
    from mastercode.runtime import oversight_store as osx
    pool = FakePool(STORE)
    monster = b"x" * (osx.RANGE_BYTES + 1000)
    files = {"findings/2020-01-01.jsonl": monster}
    _pull_out(pool, fake_dcn(files))
    cur = pool.cursors[(7, "findings", "2020-01-01")]
    assert cur["sealed"] is True and cur["offset"] < len(monster), cur
    dcn2 = fake_dcn(files)
    _pull_out(pool, dcn2)
    reads = [a["path"] for op, a in dcn2.calls if op == "read_range"]
    assert reads == [], f"a sealed day was read again: {reads}"


def test_an_in_band_day_capped_line_counts_the_day_incomplete():
    """The .capped MARKER half was tested; the in-band day_capped LINE reaching
    incomplete_days through _pull_one was not."""
    pool = FakePool(STORE)
    capped = json.dumps({"at": "2026-09-19T11:00:00Z", "transition": "day_capped",
                         "machine": MACHINE, "note": "ceiling"})
    out = _pull_out(pool, fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa"), capped)}))
    assert out["incomplete_days"] == 1, out


def test_a_store_whose_read_raises_still_gets_a_row_and_the_others_still_fold():
    """The outer except: one machine blowing up must not stop the others, and
    must itself leave a store-state row rather than only a string."""
    import asyncio

    from mastercode.runtime import oversight_store as osx
    two = [dict(STORE[0]),
           {"store_id": 8, "server": "second-box", "root": "/x",
            "purpose": "machine_record", "approved": True, "revoked": False}]
    pool = FakePool(two)
    good = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "bbb"))})

    async def exploding(pool_, eid, store_id, op, args=None):
        if store_id == 7:
            raise RuntimeError("the lane fell over")
        return await good(pool_, eid, store_id, op, args)

    orig = osx.run_dcn_op if hasattr(osx, "run_dcn_op") else None
    import server.network.nac_dcn as nd
    real = nd.run_dcn_op
    nd.run_dcn_op = exploding
    try:
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(osx.pull_store_findings(pool, 3, _finding, force=True))
        finally:
            loop.close()
    finally:
        nd.run_dcn_op = real
    rows = {f["machine"]: f["kind"] for f in out["findings"]}
    assert rows.get("box-a") == "store-state", f"the exploding machine left no row: {rows}"
    assert any(f["evidence"].get("fp") == "bbb" for f in out["findings"]), (
        f"the healthy machine did not fold: {rows}")


def test_an_expired_grant_is_not_read():
    pool = FakePool([{"store_id": 11, "server": "stale-box", "root": "/z",
                      "purpose": "machine_record", "approved": True, "revoked": False,
                      "expired": True}])
    assert _stores(pool) == [], "an expired grant would still be read"


def test_stores_are_walked_in_id_order():
    pool = FakePool([
        {"store_id": 30, "server": "c", "root": "/c", "purpose": "machine_record",
         "approved": True, "revoked": False},
        {"store_id": 10, "server": "a", "root": "/a", "purpose": "machine_record",
         "approved": True, "revoked": False},
    ])
    assert [s["store_id"] for s in _stores(pool)] == [10, 30]


def test_the_list_asks_for_the_whole_shelf():
    """A month of retained day-files is under 200; a limit of 1 would hide all
    but one and the fold would silently walk a single day."""
    pool = FakePool(STORE)
    dcn = fake_dcn({"findings/2026-09-19.jsonl": journal(line("opened", "aaa"))})
    _pull_out(pool, dcn)
    lists = [a for op, a in dcn.calls if op == "list"]
    assert lists and lists[0].get("limit") == 200, lists




# ── the accounting's signature, read from the store ─────────────────────────

FIXTURE_DIR = os.path.join(_ROOT, "tests", "fixtures", "signed-baseline")


def _fixture_files():
    with open(os.path.join(FIXTURE_DIR, "baseline.json"), "rb") as f:
        raw = f.read()
    with open(os.path.join(FIXTURE_DIR, "ca.pem"), "rb") as f:
        ca = f.read()
    with open(os.path.join(FIXTURE_DIR, "fingerprint"), encoding="utf-8") as f:
        fp = f.read().strip()
    return raw, ca, fp


def _pull_with_accounting(files, ca, bastions):
    """pull_store_findings with the box replaced by fake_dcn and the CA by the
    fixture's — the whole path from list/stat/read_range to the verdict."""
    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight_store as osx
    from server.network import bastion_baseline as bb
    import server.network.nac_dcn as nd
    pool = FakePool(STORE)
    real = nd.run_dcn_op
    nd.run_dcn_op = fake_dcn(files)
    try:
        with mock.patch.object(bb, "read_ca", lambda: ca):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    osx.pull_store_findings(pool, 3, _finding, force=True, bastions=bastions))
            finally:
                loop.close()
    finally:
        nd.run_dcn_op = real


def _acct_rows(out):
    return [f for f in out["findings"] if f["kind"] == "accounting-state"]


def test_a_go_signed_accounting_in_the_store_verifies_and_raises_no_row():
    raw, ca, fp = _fixture_files()
    out = _pull_with_accounting(
        {"baseline.json": raw, "findings/2026-09-19.jsonl": journal(line("opened", "aaa"))},
        ca, [{"hostname": "box-a", "hardware_fingerprint": fp}])
    assert out["accounting"].get("box-a") == "verified", out["accounting"]
    assert _acct_rows(out) == [], _acct_rows(out)
    # And the findings still folded beside it.
    assert any(f["evidence"].get("fp") == "aaa" for f in out["findings"])


def test_an_absent_accounting_is_not_a_finding():
    """A box that never took one says `baseline: none` on the beat, and the
    heartbeat fold already raises that. Raising it here too would be two rows
    for one fact."""
    raw, ca, fp = _fixture_files()
    out = _pull_with_accounting(
        {"findings/2026-09-19.jsonl": journal(line("opened", "aaa"))},
        ca, [{"hostname": "box-a", "hardware_fingerprint": fp}])
    assert out["accounting"].get("box-a") == "absent", out["accounting"]
    assert _acct_rows(out) == []


def test_an_edited_accounting_raises_one_pinned_row():
    raw, ca, fp = _fixture_files()
    doc = json.loads(raw)
    doc["entries"][0]["exe"] = "/usr/sbin/sshd-but-different"
    edited = json.dumps(doc).encode()
    bastions = [{"hostname": "box-a", "hardware_fingerprint": fp}]
    out = _pull_with_accounting({"baseline.json": edited}, ca, bastions)
    rows = _acct_rows(out)
    assert len(rows) == 1 and rows[0]["severity"] == "high", rows
    assert rows[0]["evidence"]["state"] == "bad_signature", rows[0]["evidence"]
    assert "content has changed" in rows[0]["evidence"]["reason"]
    # Same key on the next pass: one pinned row, not a pile.
    out2 = _pull_with_accounting({"baseline.json": edited}, ca, bastions)
    assert _acct_rows(out2)[0]["key"] == rows[0]["key"]


def test_an_accounting_signed_for_another_machine_raises_a_row():
    raw, ca, _ = _fixture_files()
    out = _pull_with_accounting(
        {"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": "not-this-box"}])
    rows = _acct_rows(out)
    assert rows and rows[0]["evidence"]["state"] == "wrong_machine", rows


def test_a_bearer_only_row_cannot_vouch_for_its_accounting():
    """No bound fingerprint on the row -> the certificate cannot be tied to the
    machine. That is reported, never passed."""
    raw, ca, _ = _fixture_files()
    out = _pull_with_accounting(
        {"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": None}])
    rows = _acct_rows(out)
    assert rows and rows[0]["evidence"]["state"] == "no_fingerprint", rows


def test_an_oversized_accounting_is_reported_not_partially_read():
    from mastercode.runtime import oversight_store as osx
    _, ca, fp = _fixture_files()
    bastions = [{"hostname": "box-a", "hardware_fingerprint": fp}]
    huge = b"{" + b" " * (osx.ACCOUNTING_MAX_BYTES + 10) + b"}"
    out = _pull_with_accounting({"baseline.json": huge}, ca, bastions)
    rows = _acct_rows(out)
    assert rows and rows[0]["evidence"]["state"] == "unreadable", rows
    # THE KEY IS THE STATE, NEVER THE REASON. The reason carries the size, so a
    # file that grows between passes has a different reason each time — keyed
    # on it, one condition becomes a new row every pass. Same state, two
    # reasons, one key. (The first version of this check used one reason
    # twice, so a reason-keyed mutation matched itself and survived.)
    bigger = b"{" + b" " * (osx.ACCOUNTING_MAX_BYTES + 5000) + b"}"
    out2 = _pull_with_accounting({"baseline.json": bigger}, ca, bastions)
    r1, r2 = rows[0], _acct_rows(out2)[0]
    assert r1["evidence"]["reason"] != r2["evidence"]["reason"], "the fixture's two reasons are identical"
    assert r1["key"] == r2["key"], f"one condition produced two keys: {r1['key']} / {r2['key']}"


def test_the_verdict_reaches_the_ingest_result():
    """run_oversight_ingest passes the bastion rows through and surfaces the
    per-machine state, so 'verified' and 'nobody checked' cannot look alike."""
    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight as ov
    from mastercode.runtime import oversight_store as osx
    from server.network import topology

    seen = {}

    async def fake_pull(pool, eid, factory, force=False, bastions=None):
        seen["bastions"] = bastions
        return {"findings": [], "problems": [], "stores": 1, "bytes": 0,
                "incomplete_days": 0, "pending_cursors": [], "skipped": "",
                "accounting": {"box-a": "verified"}}

    async def one_bastion(pool, eid):
        return [{"hostname": "box-a", "hardware_fingerprint": "fp"}]

    class LedgerPool:
        async def fetch(self, sql, *a):
            return []

    async def fake_apply(pool, plan):
        return {"written": 0, "failed": 0}

    with mock.patch.object(osx, "pull_store_findings", fake_pull), \
         mock.patch.object(topology, "list_bastions", one_bastion), \
         mock.patch.object(ov, "apply_findings", fake_apply):
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(ov.run_oversight_ingest(LedgerPool(), 3))
        finally:
            loop.close()
    assert seen["bastions"] and seen["bastions"][0]["hostname"] == "box-a", seen
    assert res["store_accounting"] == {"box-a": "verified"}, res




# ── the accounting row and the host's own CA ────────────────────────────────

def test_one_machine_one_accounting_row_across_states():
    """bad_signature then wrong_machine then no_fingerprint: ONE key."""
    raw, ca, fp = _fixture_files()
    doc = json.loads(raw); doc["unreadable"] += 1
    edited = json.dumps(doc).encode()
    k1 = _acct_rows(_pull_with_accounting({"baseline.json": edited}, ca,
                    [{"hostname": "box-a", "hardware_fingerprint": fp}]))[0]
    k2 = _acct_rows(_pull_with_accounting({"baseline.json": raw}, ca,
                    [{"hostname": "box-a", "hardware_fingerprint": "other"}]))[0]
    k3 = _acct_rows(_pull_with_accounting({"baseline.json": raw}, ca,
                    [{"hostname": "box-a", "hardware_fingerprint": None}]))[0]
    assert k1["key"] == k2["key"] == k3["key"], (k1["key"], k2["key"], k3["key"])
    assert {k1["evidence"]["state"], k2["evidence"]["state"], k3["evidence"]["state"]} == \
        {"bad_signature", "wrong_machine", "no_fingerprint"}


def test_a_bearer_only_row_is_a_notice_not_a_high():
    """The user chose bearer-only. A permanent HIGH row for that choice is the
    platform punishing what it said was theirs."""
    raw, ca, _ = _fixture_files()
    row = _acct_rows(_pull_with_accounting({"baseline.json": raw}, ca,
                     [{"hostname": "box-a", "hardware_fingerprint": None}]))[0]
    assert row["severity"] == "notice", row["severity"]


def test_a_missing_host_ca_is_one_row_under_the_host_not_per_machine():
    """Two machines, no CA on the pod: one row, filed against the host."""
    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight_store as osx
    from server.network import bastion_baseline as bb
    import server.network.nac_dcn as nd
    raw, _, fp = _fixture_files()
    two = [dict(STORE[0]),
           {"store_id": 8, "server": "second-box", "root": "/x",
            "purpose": "machine_record", "approved": True, "revoked": False}]
    pool = FakePool(two)
    real = nd.run_dcn_op
    nd.run_dcn_op = fake_dcn({"baseline.json": raw})
    try:
        with mock.patch.object(bb, "read_ca", lambda: b""):
            loop = asyncio.new_event_loop()
            try:
                out = loop.run_until_complete(osx.pull_store_findings(
                    pool, 3, _finding, force=True,
                    bastions=[{"hostname": "box-a", "hardware_fingerprint": fp},
                              {"hostname": "second-box", "hardware_fingerprint": fp}]))
            finally:
                loop.close()
    finally:
        nd.run_dcn_op = real
    ca_rows = [f for f in out["findings"] if f["kind"] == "accounting-ca"]
    assert len(ca_rows) == 1, f"{len(ca_rows)} host-CA rows for one missing file"
    assert ca_rows[0]["machine"] == "this host"
    assert _acct_rows(out) == [], "a missing HOST file was filed against a machine"


def test_a_failed_stat_raises_the_accounting_row_on_its_own():
    """The missed-row path: stat fails but the shelf listing works."""
    raw, ca, fp = _fixture_files()
    files = {"baseline.json": raw, "findings/2026-09-19.jsonl": journal(line("opened", "aaa"))}

    base = fake_dcn(files)

    async def stat_fails(pool, eid, store_id, op, args=None):
        if op == "stat":
            return {"state": "offline", "detail": "no answer"}
        return await base(pool, eid, store_id, op, args)

    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight_store as osx
    from server.network import bastion_baseline as bb
    import server.network.nac_dcn as nd
    pool = FakePool(STORE)
    real = nd.run_dcn_op
    nd.run_dcn_op = stat_fails
    try:
        with mock.patch.object(bb, "read_ca", lambda: ca):
            loop = asyncio.new_event_loop()
            try:
                out = loop.run_until_complete(osx.pull_store_findings(
                    pool, 3, _finding, force=True,
                    bastions=[{"hostname": "box-a", "hardware_fingerprint": fp}]))
            finally:
                loop.close()
    finally:
        nd.run_dcn_op = real
    rows = _acct_rows(out)
    assert rows and rows[0]["evidence"]["state"] == "unreadable", rows
    assert any(f["evidence"].get("fp") == "aaa" for f in out["findings"]), "the journal did not fold"


def test_a_raise_in_the_verifier_does_not_take_the_journal_down():
    """One crafted baseline.json must not silence a machine's whole journal."""
    raw, ca, fp = _fixture_files()
    files = {"baseline.json": raw, "findings/2026-09-19.jsonl": journal(line("opened", "aaa"))}
    import asyncio
    from unittest import mock

    from mastercode.runtime import oversight_store as osx
    import server.network.nac_dcn as nd

    async def boom(*a, **k):
        raise RuntimeError("verifier exploded")

    pool = FakePool(STORE)
    real = nd.run_dcn_op
    nd.run_dcn_op = fake_dcn(files)
    try:
        with mock.patch.object(osx, "_verify_accounting", boom):
            loop = asyncio.new_event_loop()
            try:
                out = loop.run_until_complete(osx.pull_store_findings(
                    pool, 3, _finding, force=True,
                    bastions=[{"hostname": "box-a", "hardware_fingerprint": fp}]))
            finally:
                loop.close()
    finally:
        nd.run_dcn_op = real
    assert any(f["evidence"].get("fp") == "aaa" for f in out["findings"]), (
        "a raise in the verifier silenced the journal")
    assert _acct_rows(out) and _acct_rows(out)[0]["evidence"]["state"] == "unreadable"


def test_the_accounting_size_cap_is_the_literal():
    from mastercode.runtime import oversight_store as osx
    assert osx.ACCOUNTING_MAX_BYTES == 512 * 1024, (
        "the cap changed; the range-read ceiling it must not exceed is 512 KiB")




def test_a_verified_mirror_that_is_not_the_accounting_in_use_is_a_finding():
    """The freshness check the signature cannot make: the beat says which
    accounting the box is comparing against. A verified mirror with a different
    taken_at is stale, or a rollback — both validly signed, both findings."""
    raw, ca, fp = _fixture_files()
    taken = json.loads(raw)["taken_at"]
    # Same taken_at on the beat: no finding.
    out = _pull_with_accounting({"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": fp,
          "detect": {"baseline_taken_at": taken}}])
    assert _acct_rows(out) == [], _acct_rows(out)
    assert out["accounting"]["box-a"] == "verified"
    # A different one: stale_mirror, high, one row keyed like every other.
    out = _pull_with_accounting({"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": fp,
          "detect": {"baseline_taken_at": "2026-09-01T00:00:00Z"}}])
    rows = _acct_rows(out)
    assert rows and rows[0]["evidence"]["state"] == "stale_mirror", rows
    assert rows[0]["severity"] == "high"
    assert "2026-09-01" in rows[0]["evidence"]["reason"] and taken in rows[0]["evidence"]["reason"]
    # No beat value (older bastion, or no pass yet): not a finding.
    out = _pull_with_accounting({"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": fp, "detect": {}}])
    assert _acct_rows(out) == []




def test_stale_mirror_compares_instants_not_strings():
    """The beat and the mirror are byte-identical today only because both read
    one Go field formatted once. The same instant with an offset, or with
    nanoseconds, is NOT stale."""
    raw, ca, fp = _fixture_files()
    taken = json.loads(raw)["taken_at"]                  # 2026-09-19T10:00:00Z
    same_offset = taken.replace("Z", "+00:00")
    same_nanos = taken.replace("Z", ".000000000Z")
    for form in (same_offset, same_nanos):
        out = _pull_with_accounting({"baseline.json": raw}, ca,
            [{"hostname": "box-a", "hardware_fingerprint": fp,
              "detect": {"baseline_taken_at": form}}])
        assert _acct_rows(out) == [], f"{form!r} read as stale: {_acct_rows(out)}"
    # A genuinely different instant one second later IS stale.
    out = _pull_with_accounting({"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": fp,
          "detect": {"baseline_taken_at": taken.replace(":00Z", ":01Z")}}])
    assert _acct_rows(out) and _acct_rows(out)[0]["evidence"]["state"] == "stale_mirror"


def test_the_pass_bookkeeping_flag_is_not_in_the_result():
    raw, ca, fp = _fixture_files()
    out = _pull_with_accounting({"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": fp}])
    assert "_host_ca_raised" not in out, sorted(out)




def test_stale_mirror_tolerates_a_naive_beat_timestamp():
    """A naive beat value naming the same instant is not stale — the instant
    compare must not fall back to string equality for the input class it
    exists to protect against."""
    raw, ca, fp = _fixture_files()
    taken = json.loads(raw)["taken_at"]
    out = _pull_with_accounting({"baseline.json": raw}, ca,
        [{"hostname": "box-a", "hardware_fingerprint": fp,
          "detect": {"baseline_taken_at": taken.rstrip("Z")}}])
    assert _acct_rows(out) == [], _acct_rows(out)


def _tests():
    """Collected INSIDE main, not at import, AND RECONCILED AGAINST THE SOURCE.

    A module-level list is built where it is written, so any test defined below
    it is silently never run. Moving the runner to the end fixes it once; the
    next `cat >>` puts it back. This file has now lost tests to that twice — the
    second time it was 31 defined and 21 running, and the ten missing were
    precisely the ones written for the blocking findings, so the suite reported
    green for a fold that could not work at all.

    So the count is RECONCILED with the source text. A test that exists and is
    not collected is a hard failure, not a quieter green.
    """
    found = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        src = open(__file__, encoding="utf-8").read()
    except OSError:
        return found
    declared = len(re.findall(r"^def (test_\w+)", src, re.M))
    if declared != len(found):
        raise SystemExit(
            f"{declared} tests are defined in this file and {len(found)} were "
            f"collected. {declared - len(found)} are defined BELOW the runner and "
            "would never run. Move the runner block to the end of the file.")
    return found


def main() -> int:
    print("oversight store fold")
    failed = 0
    TESTS = _tests()
    for t in TESTS:
        try:
            t()
            failed += check(t.__name__, True)
        except AssertionError as e:
            failed += check(t.__name__, False, str(e))
        except Exception as e:  # noqa: BLE001
            failed += check(t.__name__, False, f"{type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
