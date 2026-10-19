"""The "vendor refused this CLI version" signal, the Host's half (2026-10-06).

A Host may send {harness, cli_version, min_version_named, hour} and nothing else,
matched from the vendor CLI's own refusal error, at most once an hour per
(harness, cli_version), on the heartbeat it already sends. These pin:

  * the grok matcher needs BOTH fragments of xAI's recorded refusal ("426 Upgrade
    Required" and "update to version X or later"), reads X, and matches nothing
    for a harness with no matcher;
  * detect() reads only the CLI's error messages, only on a failed run, and its
    record is exactly {harness, cli_version, min_version_named}, the version read
    the way the auto-update job reads it;
  * a record with any key beyond the four, or a value outside its shape, is
    dropped whole (content-free: no run id, prompt, path, Host id or minute can
    ride along);
  * a pair goes out at most once an hour, the marks survive a restart, and the
    heartbeat carries the records only when there are some.

The real run (a fake grok CLI through the watched runner, which sends real
signals) is scripts/test_harness_vendor_refusal_run.py, in a private PID
namespace.
"""
from __future__ import annotations

import asyncio
import calendar
import inspect
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "system-apps"))

from _harness_runtime import vendor_refusal as vr  # noqa: E402
from server import harness_refusals as hr  # noqa: E402
from server.bridge import bridge_client  # noqa: E402

# The text recorded from the refused run .
# The record elides what sat between the two fragments; so does this.
XAI_426 = "426 Upgrade Required ... update to version 1.0.13 or later"
T0 = calendar.timegm((2026, 10, 7, 5, 10, 0))   # 2026-10-07 05:10 UTC


# ── the matcher (harness pod) ───────────────────────────────────────────────

def test_grok_refusal_is_matched_and_names_the_minimum():
    assert vr.match("grok", [XAI_426]) == "1.0.13"
    assert vr.match("grok", ["Error: " + XAI_426.upper()]) == "1.0.13"


@pytest.mark.parametrize("text", [
    "426 Upgrade Required",                                   # no version named
    "please update to version 1.0.13 or later",               # no 426
    "HTTP 426 ... update to version 1.0.13 or later",         # not the status line
    "4260 Upgrade Required ... update to version 1.0.13 or later",
    "426 Upgrade Required ... update to version 1.0 or later",  # not a version
    "Not signed in",
    "",
])
def test_anything_short_of_both_fragments_is_not_a_refusal(text):
    assert vr.match("grok", [text]) is None


def test_a_harness_with_no_matcher_never_matches():
    for hid in ("codex", "cursor", "opencode", "kimi", "antigravity", "claude-code"):
        assert vr.match(hid, [XAI_426]) is None, hid


def _spec(hid="grok"):
    vr._version_cache.clear()
    return types.SimpleNamespace(id=hid, binary=Path(f"/nonexistent/{hid}"))


def _fake_version(monkeypatch, out="grok 0.2.112 (0123abcd)\n", err="", exc=None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if exc:
            raise exc
        return types.SimpleNamespace(stdout=out, stderr=err, returncode=0)
    monkeypatch.setattr(vr.subprocess, "run", run)
    return calls


def test_detect_returns_exactly_the_pod_record(monkeypatch):
    calls = _fake_version(monkeypatch)
    spec = _spec()
    rec = vr.detect(spec, ok=False, errors=["Not signed in", XAI_426])
    assert rec == {"harness": "grok", "cli_version": "0.2.112", "min_version_named": "1.0.13"}
    assert calls == [[str(spec.binary), "--version"]]


def test_detect_parses_the_version_as_the_autobump_probe_does():
    # grok 1.0.46 prints "grok 1.0.46 (2765805b9442)" (measured on the shipped image).
    assert vr.parse_version("grok 1.0.46 (2765805b9442)\n") == "1.0.46"
    assert vr.parse_version("2026.10.01-e373342") == "2026.10.01-e373342"
    assert vr.parse_version("codex-cli 0.160.1") == "0.160.1"


def test_detect_needs_a_failed_run_a_matcher_and_a_version(monkeypatch):
    _fake_version(monkeypatch)
    assert vr.detect(_spec(), ok=True, errors=[XAI_426]) is None
    assert vr.detect(_spec("codex"), ok=False, errors=[XAI_426]) is None
    assert vr.detect(_spec(), ok=False, errors=["boom"]) is None
    _fake_version(monkeypatch, out="no version here")
    assert vr.detect(_spec(), ok=False, errors=[XAI_426]) is None
    _fake_version(monkeypatch, exc=OSError("gone"))
    assert vr.detect(_spec(), ok=False, errors=[XAI_426]) is None


def test_detect_never_raises(monkeypatch):
    _fake_version(monkeypatch)
    assert vr.detect(object(), ok=False, errors=[XAI_426]) is None


# ── the record (Host) ───────────────────────────────────────────────────────

GOOD = {"harness": "grok", "cli_version": "0.2.112", "min_version_named": "1.0.13",
        "hour": "2026-10-07T05"}


def test_clean_admits_the_four_keys():
    assert hr.clean(dict(GOOD)) == GOOD
    assert hr.clean({**GOOD, "min_version_named": None}) == {**GOOD, "min_version_named": None}
    assert hr.clean({**GOOD, "cli_version": "2026.10.01-e373342"})["cli_version"] == "2026.10.01-e373342"


@pytest.mark.parametrize("extra", ["run_id", "prompt", "output", "path", "user_id", "host_id",
                                   "entity_id", "ip", "ts", "minute"])
def test_any_fifth_key_drops_the_whole_record(extra):
    assert hr.clean({**GOOD, extra: "x"}) is None


@pytest.mark.parametrize("key,bad", [
    ("harness", "bash"), ("harness", None), ("harness", "grok "),
    ("cli_version", "0.2.112 entity-1"), ("cli_version", "/home/u/proj"),
    ("cli_version", "x" * 80), ("cli_version", 112),
    ("min_version_named", "1.0.13 or later"), ("min_version_named", {"v": "1"}),
    ("hour", "2026-10-07T05:10"), ("hour", "2026-10-07"), ("hour", 1759813800),
])
def test_a_value_outside_its_shape_drops_the_whole_record(key, bad):
    assert hr.clean({**GOOD, key: bad}) is None
    assert hr.clean({k: v for k, v in GOOD.items() if k != key}) is None


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "SENT_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(hr, "_sent", None)
    monkeypatch.setattr(hr, "_pending", {})
    return tmp_path


POD = {"harness": "grok", "cli_version": "0.2.112", "min_version_named": "1.0.13"}


def test_a_run_result_queues_one_record_for_the_harness_that_ran(fresh):
    assert hr.note("grok", {"ok": False, "vendor_refusal": dict(POD)}, now=T0)
    assert hr.take_due(now=T0 + 5) == [GOOD]
    assert hr.take_due(now=T0 + 35) == [], "a record goes out once"


@pytest.mark.parametrize("run_out", [
    {"ok": False},
    {"ok": False, "vendor_refusal": {**POD, "harness": "codex"}},      # not the harness that ran
    {"ok": False, "vendor_refusal": {**POD, "transcript": "secret"}},  # more than the pod's part
    {"ok": False, "vendor_refusal": {**POD, "hour": "2026-10-07T05"}},  # the pod sets no hour
    {"ok": False, "vendor_refusal": {**POD, "cli_version": "../../etc"}},
    {"ok": False, "vendor_refusal": "grok refused"},
    None,
])
def test_anything_else_in_a_run_result_queues_nothing(fresh, run_out):
    assert hr.note("grok", run_out, now=T0) is False
    assert hr.take_due(now=T0) == []


def test_a_pair_goes_out_at_most_once_an_hour(fresh):
    sent = []
    for minute in range(0, 50, 5):                       # a refusal on every run
        hr.note("grok", {"vendor_refusal": dict(POD)}, now=T0 + minute * 60)
        sent += hr.take_due(now=T0 + minute * 60 + 30)
    assert sent == [GOOD], sent
    # the next hour, the next refusal goes out again, stamped with that hour
    hr.note("grok", {"vendor_refusal": dict(POD)}, now=T0 + 3600)
    assert hr.take_due(now=T0 + 3600 + 30) == [{**GOOD, "hour": "2026-10-07T06"}]


def test_a_refusal_just_before_the_hour_turns_still_goes_out_once(fresh):
    late = calendar.timegm((2026, 10, 7, 5, 59, 50))
    hr.note("grok", {"vendor_refusal": dict(POD)}, now=late)
    first = hr.take_due(now=late + 30)                   # leaves at 06:00:20
    hr.note("grok", {"vendor_refusal": dict(POD)}, now=late + 300)
    assert first == [{**GOOD, "hour": "2026-10-07T06"}]
    assert hr.take_due(now=late + 330) == [], "a second send inside 06:00-07:00"


def test_two_versions_are_two_pairs(fresh):
    hr.note("grok", {"vendor_refusal": dict(POD)}, now=T0)
    hr.note("grok", {"vendor_refusal": {**POD, "cli_version": "1.0.46"}}, now=T0)
    got = hr.take_due(now=T0)
    assert sorted(r["cli_version"] for r in got) == ["0.2.112", "1.0.46"]


def test_the_marks_survive_a_restart_inside_the_hour(fresh, monkeypatch):
    hr.note("grok", {"vendor_refusal": dict(POD)}, now=T0)
    assert hr.take_due(now=T0) == [GOOD]
    saved = json.loads((fresh / "sent.json").read_text())
    assert saved == {"grok|0.2.112": "2026-10-07T05"}, "the marks hold no more than the pair and hour"
    monkeypatch.setattr(hr, "_sent", None)               # a new process
    monkeypatch.setattr(hr, "_pending", {})
    assert hr.note("grok", {"vendor_refusal": dict(POD)}, now=T0 + 600) is False
    assert hr.take_due(now=T0 + 630) == []


def test_note_and_take_due_never_raise(fresh, monkeypatch):
    monkeypatch.setattr(hr, "clean", lambda rec: (_ for _ in ()).throw(RuntimeError("x")))
    assert hr.note("grok", {"vendor_refusal": dict(POD)}, now=T0) is False
    monkeypatch.setattr(hr, "_pending", {("grok", "0.2.112"): dict(POD)})
    assert hr.take_due(now=T0) == []


# ── the heartbeat ───────────────────────────────────────────────────────────

def _one_beat(monkeypatch):
    sent = []

    class _Client(bridge_client.BridgeClient):
        def __init__(self):
            self._running = True
            self._connected = True

        async def request(self, op, params=None, **kw):
            sent.append((op, params))
            self._running = False
            return {"ok": True}

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(bridge_client.asyncio, "sleep", _no_sleep)
    asyncio.run(_Client()._heartbeat_loop())
    return sent


def test_the_heartbeat_carries_due_records_once(fresh, monkeypatch):
    hr.note("grok", {"vendor_refusal": dict(POD)})
    sent = _one_beat(monkeypatch)
    assert len(sent) == 1 and sent[0][0] == "heartbeat.ping"
    recs = sent[0][1]["harness_refusals"]
    assert len(recs) == 1 and set(recs[0]) == set(hr.KEYS)
    assert {k: recs[0][k] for k in POD} == POD
    again = _one_beat(monkeypatch)
    assert "harness_refusals" not in again[0][1], "the next beat does not carry it again"


def test_a_heartbeat_with_nothing_due_is_unchanged(fresh, monkeypatch):
    params = _one_beat(monkeypatch)[0][1]
    assert "harness_refusals" not in params and "hub_key" in params


# ── the dispatcher ──────────────────────────────────────────────────────────

def test_every_finished_pod_run_offers_its_result_to_the_signal():
    """_run_on_pod notes the run's result right after it persists rotated
    credentials: the one place every run's result passes (stream, buffered,
    failed) with the harness that was dispatched."""
    from server.routes import review
    src = inspect.getsource(review._run_on_pod)
    persist = src.index("await _persist_rotated_creds(harness_id, run_out)")
    note = src.index("_refusals.note(harness_id, run_out)")
    assert persist < note < src.index("_maybe_cache_model_catalog(http, base, harness_id)")
