"""Training Memory's classify phase reaches review on the user's own key.

A change that retired the platform-billed classify lane also,
with its dead helpers, deleted _persist_results, which the surviving BYOK lane
still calls once the batch ends. From then on every classify run raised
NameError at that line, AFTER the user's Anthropic batch had been submitted and
paid for, so no job could reach review. pyflakes found it.

This drives the real run_classify_phase through the real submit / poll / fetch
helpers. Only the HTTP session and the DB pool are fakes; nothing leaves the
process.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

import brain.training_memory.worker as W  # noqa: E402


def _pair(i: int, text: str) -> dict:
    return {"pair_id": f"s1:u{i}", "session_id": "s1",
            "rejected": {"text": f"entity reply {i}", "tools": [], "len": 14},
            "correction": {"text": text, "len": len(text), "kind": "correction"}}


def _result(i: int, verdict: dict) -> dict:
    return {"custom_id": f"p{i:05d}",
            "result": {"type": "succeeded",
                       "message": {"content": [{"type": "text",
                                                "text": json.dumps(verdict)}]}}}


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status, self._data, self._text = status, data, text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._data

    async def text(self):
        return self._text


class _Session:
    """aiohttp.ClientSession-shaped: one batch submitted, ended on first poll."""
    calls: list = []

    def __init__(self, body: str):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _Session.calls.append(("POST", url, headers.get("x-api-key")))
        return _Resp(data={"id": "msgbatch_1"})

    def get(self, url, headers=None):
        _Session.calls.append(("GET", url, headers.get("x-api-key")))
        if url.endswith("/results"):
            return _Resp(text=self._body)
        return _Resp(data={"processing_status": "ended",
                           "request_counts": {"succeeded": 2, "errored": 0},
                           "results_url": url + "/results"})


class _Pool:
    async def fetch(self, *a):
        return []


def test_byok_classify_reaches_ready_for_review(tmp_path, monkeypatch):
    pairs = [_pair(0, "stop adding trailing summaries"), _pair(1, "thanks, perfect")]
    (tmp_path / "pairs.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pairs))
    body = "\n".join(json.dumps(r) for r in (
        _result(0, {"is_feedback": True, "type": "correction", "confidence": 0.9,
                    "rule": "Do not add trailing summaries.", "codebook_signals": {}}),
        _result(1, {"is_feedback": False, "type": None, "confidence": 0.95,
                    "rule": None, "codebook_signals": {}}),
    )) + "\n"

    _Session.calls = []
    monkeypatch.setattr(W, "aiohttp", types.SimpleNamespace(
        ClientSession=lambda: _Session(body)))
    monkeypatch.setattr(W.brain_service, "get_pool", lambda: _Pool())

    async def run():
        return [e async for e in W.run_classify_phase(
            tmp_path, {"billing": "byo", "byo_key": "sk-ant-test",
                       "entity_id": 9003, "role": "coding"})]

    events = asyncio.run(run())

    seeding = [e for e in events if e["phase"] == "seeding_review"]
    assert seeding and seeding[0]["counters"]["yes_pairs"] == 1, events
    last = events[-1]
    assert last["phase"] == "ready_for_review", events
    assert last["counters"]["yes_pending_review"] == 1, last
    # The not-feedback pair is counted too: the review screen's "no" tally and
    # its show-all view read the skipped entries from review_state.json.
    assert last["counters"]["no_pairs"] == 1, last
    assert last["counters"]["total_classified"] == 2, last

    # Both classifier rows are written; the feedback one is seeded for review.
    rows = [json.loads(l) for l in (tmp_path / "classified.jsonl").read_text().splitlines()]
    assert [r["pair_id"] for r in rows] == ["s1:u0", "s1:u1"]
    assert rows[0]["classifier"]["rule"] == "Do not add trailing summaries."
    state = json.loads((tmp_path / "review_state.json").read_text())
    assert state["s1:u0"]["status"] == "pending"
    assert state["s1:u0"]["rule_final"] == "Do not add trailing summaries."
    assert state["s1:u1"]["status"] == "skipped_no_feedback"

    # The user's key went to Anthropic, and only there.
    assert _Session.calls and all(c[2] == "sk-ant-test" for c in _Session.calls)
    assert all(c[1].startswith(W.ANTHROPIC_API) for c in _Session.calls)


def test_a_non_object_reply_does_not_fail_the_batch(tmp_path, monkeypatch):
    """A reply that parses as JSON but is not an object (`true`, a list) used
    to raise AttributeError when seeding read it with .get, after the batch
    had been paid for. It is kept as a parse error and seeded as skipped."""
    pairs = [_pair(0, "stop adding trailing summaries"), _pair(1, "ok")]
    (tmp_path / "pairs.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pairs))
    bare = {"custom_id": "p00001",
            "result": {"type": "succeeded",
                       "message": {"content": [{"type": "text", "text": "[1, 2]"}]}}}
    body = "\n".join(json.dumps(r) for r in (
        _result(0, {"is_feedback": True, "type": "correction", "confidence": 0.9,
                    "rule": "Do not add trailing summaries.", "codebook_signals": {}}),
        bare,
    )) + "\n"

    _Session.calls = []
    monkeypatch.setattr(W, "aiohttp", types.SimpleNamespace(
        ClientSession=lambda: _Session(body)))
    monkeypatch.setattr(W.brain_service, "get_pool", lambda: _Pool())

    async def run():
        return [e async for e in W.run_classify_phase(
            tmp_path, {"billing": "byo", "byo_key": "sk-ant-test",
                       "entity_id": 9003, "role": "coding"})]

    events = asyncio.run(run())
    assert events[-1]["phase"] == "ready_for_review", events
    state = json.loads((tmp_path / "review_state.json").read_text())
    assert state["s1:u0"]["status"] == "pending"
    assert state["s1:u1"]["status"] == "skipped_no_feedback"
    assert state["s1:u1"]["rule_final"] == ""
    # The progress counter agrees with the review screen's "no" tally, which
    # counts skipped_no_feedback by status (routes/training_memory.py).
    assert events[-1]["counters"]["no_pairs"] == 1, events[-1]


def test_an_object_reply_with_wrong_field_types_does_not_fail_the_batch(tmp_path, monkeypatch):
    """An object reply whose rule is not a string raised AttributeError at
    seeding's .strip(), losing the paid-for batch; a confidence that is not a
    number reached the review screen, whose toFixed() throws on it. Both are
    dropped to None and the pair still reaches review."""
    pairs = [_pair(0, "stop adding trailing summaries"), _pair(1, "no, not like that")]
    (tmp_path / "pairs.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pairs))
    body = "\n".join(json.dumps(r) for r in (
        _result(0, {"is_feedback": True, "type": "correction", "confidence": "high",
                    "rule": "Do not add trailing summaries.", "codebook_signals": {}}),
        _result(1, {"is_feedback": True, "type": ["correction"], "confidence": True,
                    "rule": 5, "codebook_signals": {}}),
    )) + "\n"

    _Session.calls = []
    monkeypatch.setattr(W, "aiohttp", types.SimpleNamespace(
        ClientSession=lambda: _Session(body)))
    monkeypatch.setattr(W.brain_service, "get_pool", lambda: _Pool())

    async def run():
        return [e async for e in W.run_classify_phase(
            tmp_path, {"billing": "byo", "byo_key": "sk-ant-test",
                       "entity_id": 9003, "role": "coding"})]

    events = asyncio.run(run())
    assert events[-1]["phase"] == "ready_for_review", events
    state = json.loads((tmp_path / "review_state.json").read_text())
    assert state["s1:u0"]["classifier_confidence"] is None
    assert state["s1:u0"]["classifier_type"] == "correction"
    assert state["s1:u0"]["rule_final"] == "Do not add trailing summaries."
    assert state["s1:u1"]["rule_final"] == ""
    assert state["s1:u1"]["classifier_type"] is None
    assert state["s1:u1"]["classifier_confidence"] is None


def test_pairs_the_batch_did_not_classify_are_counted_and_recorded(tmp_path, monkeypatch):
    """A batch row that errored, expired or was canceled used to be dropped
    without a trace: the review total came out smaller than the pair count
    and nothing said why. Each one is counted by result type, a pair the
    results never mention is counted as missing, and the counts are written
    where the review route reads them."""
    pairs = [_pair(i, f"no, not like that {i}") for i in range(5)]
    (tmp_path / "pairs.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pairs))
    body = "\n".join(json.dumps(r) for r in (
        _result(0, {"is_feedback": True, "type": "correction", "confidence": 0.9,
                    "rule": "Do not add trailing summaries.", "codebook_signals": {}}),
        {"custom_id": "p00001", "result": {"type": "errored",
                                          "error": {"type": "overloaded_error"}}},
        {"custom_id": "p00002", "result": {"type": "expired"}},
        {"custom_id": "p00003", "result": {"type": "errored",
                                          "error": {"type": "invalid_request"}}},
        # p00004 never appears in the results.
    )) + "\n"

    _Session.calls = []
    monkeypatch.setattr(W, "aiohttp", types.SimpleNamespace(
        ClientSession=lambda: _Session(body)))
    monkeypatch.setattr(W.brain_service, "get_pool", lambda: _Pool())

    async def run():
        return [e async for e in W.run_classify_phase(
            tmp_path, {"billing": "byo", "byo_key": "sk-ant-test",
                       "entity_id": 9003, "role": "coding"})]

    events = asyncio.run(run())
    last = events[-1]
    assert last["phase"] == "ready_for_review", events
    c = last["counters"]
    assert c["total_classified"] == 1, c
    assert c["not_classified"] == 4, c
    assert c["not_classified_errored"] == 2, c
    assert c["not_classified_expired"] == 1, c
    assert c["not_classified_missing"] == 1, c
    assert c["total_classified"] + c["not_classified"] == len(pairs)

    summary = json.loads((tmp_path / W.CLASSIFY_SUMMARY).read_text())
    assert summary == {"pairs": 5, "classified": 1, "not_classified": 4,
                       "not_classified_by_type": {"errored": 2, "expired": 1,
                                                  "missing": 1}}, summary
