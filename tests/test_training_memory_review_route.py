"""Training Memory's review and bulk routes account for every pair (kernel).

server/routes/training_memory.py is kernel code (server/main.py mounts it), so
these run in the kernel's tests/ lane. The Training Memory app's own screen,
which drives the real system-apps/training-memory in Chromium, is a store app's
test: tests/test_training_memory_review_reachable.py, on the store lane
(scripts/gates-store-apps.txt). Split out of that file so a
kernel change to these routes is still tested on every PR and at ship.

Two ways a pair used to go missing between the classify run and the screen:
  - a batch row that errored or expired was dropped, and the screen never
    said so, so its total came out short of the pairs sent;
  - a bulk action applied to whatever a filter matched on the server, not to
    the pairs the screen showed.
Nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]


def _entry(pid, *, is_fb, ctype, status, conf=0.9, kind="interrupt"):
    return {"pair_id": pid, "session_id": "s1", "kind": kind,
            "classifier_is_feedback": is_fb, "classifier_type": ctype,
            "classifier_confidence": conf, "rule_original": "Be brief.",
            "rule_final": "Be brief.", "role": "coding", "severity": "gentle",
            "status": status, "would_escalate_id": None}


STATE = {
    "s1:a": _entry("s1:a", is_fb=True, ctype="correction", status="pending"),
    "s1:b": _entry("s1:b", is_fb=True, ctype=None, status="pending"),
    "s1:c": _entry("s1:c", is_fb=True, ctype="praise", status="pending"),
    # The confidence _verdict_shape drops to None for a wrong-typed reply.
    "s1:d": _entry("s1:d", is_fb=True, ctype="frustration", status="pending", conf=None),
    "s1:e": _entry("s1:e", is_fb=False, ctype=None, status="skipped_no_feedback"),
}
SUMMARY = {"pairs": 8, "classified": 5, "not_classified": 3,
           "not_classified_by_type": {"errored": 2, "expired": 1}}


@pytest.fixture()
def routes(tmp_path, monkeypatch):
    os.environ.setdefault("TM_JOBS_ROOT", str(tmp_path / "tm-jobs-import"))
    from server.routes import training_memory as R
    from brain.training_memory.worker import CLASSIFY_SUMMARY
    monkeypatch.setattr(R, "JOBS_ROOT", tmp_path)
    jd = tmp_path / "job1"
    jd.mkdir()
    (jd / "review_state.json").write_text(json.dumps(STATE))
    (jd / CLASSIFY_SUMMARY).write_text(json.dumps(SUMMARY))
    return R, jd


def _statuses(jd):
    return {pid: e["status"] for pid, e in
            json.loads((jd / "review_state.json").read_text()).items()}


def test_review_route_serves_the_not_classified_count(routes):
    R, _ = routes
    data = asyncio.run(R.review("job1", show_all=True))
    assert data["counts"]["not_classified"] == 3, data["counts"]
    assert data["counts"]["not_classified_by_type"] == {"errored": 2, "expired": 1}


def test_review_route_reads_a_job_with_no_summary(routes):
    R, jd = routes
    from brain.training_memory.worker import CLASSIFY_SUMMARY
    (jd / CLASSIFY_SUMMARY).unlink()
    data = asyncio.run(R.review("job1", show_all=True))
    assert data["counts"]["not_classified"] == 0
    assert data["counts"]["not_classified_by_type"] == {}




def test_bulk_acts_on_exactly_the_pairs_it_is_sent(routes):
    """A bulk action applies to the pair_ids the screen sends and to no
    other pair, so a pair hidden by the search (or any filter) is untouched.
    Approve still moves only pending pairs."""
    R, jd = routes
    body = R.BulkAction(action="approve_visible", pair_ids=["s1:a", "s1:c", "s1:e"])
    out = asyncio.run(R.bulk_action("job1", body))
    assert out["matched"] == 3, out
    assert _statuses(jd) == {
        "s1:a": "approved", "s1:b": "pending", "s1:c": "approved",
        "s1:d": "pending", "s1:e": "skipped_no_feedback"}

    rj = R.BulkAction(action="reject_visible", pair_ids=["s1:b", "s1:gone"])
    out = asyncio.run(R.bulk_action("job1", rj))
    assert out["matched"] == 1, out
    assert _statuses(jd) == {
        "s1:a": "approved", "s1:b": "rejected", "s1:c": "approved",
        "s1:d": "pending", "s1:e": "skipped_no_feedback"}


def test_bulk_refuses_a_request_without_pair_ids(routes):
    """A filter-only request (the old screen's shape) is refused rather than
    widened to every pair the filter happens to match on the server."""
    R, jd = routes
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        R.BulkAction(action="approve_visible", filter={})
    assert _statuses(jd)["s1:a"] == "pending"
