"""Training Memory's review screen accounts for every pair it was given.

Two ways a pair used to go missing between the classify run and the screen:
  - a batch row that errored or expired was dropped, and the screen never
    said so, so its total came out short of the pairs sent;
  - a feedback pair whose classifier type was null (the classifier prompt
    allows it) or outside correction/frustration/rejection was counted as
    pending but filtered out of every view, so it could never be acted on.

The route half (review() and the bulk action on a job directory) is kernel
code and lives in tests/test_training_memory_review_route.py. This file is
the screen half, a store app's test (scripts/gates-store-apps.txt): it
drives the REAL app (system-apps/training-memory) in Chromium against a
stubbed review route; it skips when Chromium is unavailable.
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


# ── the screen ───────────────────────────────────────────────────────────────

ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "training-memory"


def _review_payload():
    rows = [{**e, "correction_text": "no", "rejected_preview": "reply",
             "context": [], "session_label": "first"} for e in STATE.values()]
    counts = {"total": len(STATE), "pending": 4, "approved": 0, "edited": 0,
              "rejected": 0, "skipped_no_feedback": 1,
              "not_classified": SUMMARY["not_classified"],
              "not_classified_by_type": SUMMARY["not_classified_by_type"]}
    return {"rows": rows, "sessions": [{"id": "s1", "label": "first", "count": 5}],
            "counts": counts}


def _serve(route):
    path = route.request.url[len(ORIGIN):].split("?")[0]
    prefix = "/system-apps/training-memory/"
    if path.startswith(prefix):
        f = APP / path[len(prefix):]
        ctype = {".css": "text/css", ".js": "application/javascript",
                 ".html": "text/html"}.get(f.suffix, "application/octet-stream")
        if not f.is_file():
            return route.fulfill(status=404, body="")
        return route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
    if path == "/api/training-memory/jobs/job1/review":
        return _json(route, _review_payload())
    if path == "/api/training-memory/existing-roles":
        return _json(route, {"roles": []})
    return _json(route, {})


def _json(route, body):
    return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


@pytest.fixture()
def screen():
    playwright_api = pytest.importorskip(
        "playwright.sync_api", reason="playwright not available to drive the app JS")
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route(f"{ORIGIN}/**", _serve)
        pg.goto(f"{ORIGIN}/system-apps/training-memory/index.html")
        pg.wait_for_function("window.App && document.readyState === 'complete'")
        pg.errors = errors  # type: ignore[attr-defined]
        yield pg
        b.close()


def _open_review(pg):
    """Reach the review screen the way a finished classify run does: the
    run's event stream delivers ready_for_review, and the app loads the
    review route."""
    pg.evaluate("""() => {
        App.state.jobId = 'job1';
        window.EventSource = function () {
            const self = this;
            setTimeout(() => self.onmessage && self.onmessage({data: JSON.stringify(
                {phase: 'ready_for_review', progress: 1, done: true, counters: {}})}), 0);
            this.close = () => {};
        };
    }""")
    pg.route(f"{ORIGIN}/api/training-memory/jobs/job1/start",
             lambda r: _json(r, {"started": True}))
    pg.evaluate("() => { document.getElementById('confirm-btn').disabled = false;"
                " document.getElementById('confirm-btn').click(); }")
    pg.wait_for_function("document.querySelectorAll('#review-list [data-pair-id]').length > 0")


def test_screen_shows_every_pending_pair_and_the_not_classified_count(screen):
    _open_review(screen)
    # Lower the confidence filter to its floor: a pending pair whose
    # confidence was dropped to None must still be reachable.
    screen.evaluate("""() => { const s = document.getElementById('conf-slider');
        s.value = s.min; s.dispatchEvent(new Event('input')); }""")
    shown = screen.evaluate(
        "() => Array.from(document.querySelectorAll('#review-list [data-pair-id]'))"
        ".map(n => n.dataset.pairId).sort()")
    assert shown == ["s1:a", "s1:b", "s1:c", "s1:d"], shown
    headers = screen.evaluate(
        "() => Array.from(document.querySelectorAll('#review-list .tm-group-header'))"
        ".map(h => h.textContent)")
    assert "unclassified · 2 pairs" in headers, headers

    note = screen.locator("#not-classified-note")
    assert note.is_visible()
    assert " ".join(note.text_content().split()) == (
        "3 pairs could not be classified (errored/expired) and are not shown.")
    assert screen.errors == []


def _call(coro):
    # Playwright's sync API runs its event loop on this thread, so the real
    # async route runs on a worker thread with a loop of its own.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def test_approve_all_visible_leaves_pairs_the_search_hides(routes, screen):
    """Search narrows the list to one rule; "approve all visible" approves
    that rule and nothing the search hid. The app drives the REAL review and
    bulk routes on a job directory, so this covers both halves at once."""
    R, jd = routes
    state = {
        "s1:a": {**_entry("s1:a", is_fb=True, ctype="correction", status="pending"),
                 "rule_final": "Use tabs for indentation."},
        "s1:b": {**_entry("s1:b", is_fb=True, ctype="correction", status="pending"),
                 "rule_final": "Never push to main."},
        "s1:c": {**_entry("s1:c", is_fb=True, ctype=None, status="pending"),
                 "rule_final": "Answer in French."},
        "s1:e": _entry("s1:e", is_fb=False, ctype=None, status="skipped_no_feedback"),
    }
    (jd / "review_state.json").write_text(json.dumps(state))
    sent: list[dict] = []

    def review(route):
        return _json(route, _call(R.review("job1", show_all=True)))

    def bulk(route):
        payload = json.loads(route.request.post_data or "{}")
        sent.append(payload)
        return _json(route, _call(R.bulk_action("job1", R.BulkAction(**payload))))

    screen.route(f"{ORIGIN}/api/training-memory/jobs/job1/review?*", review)
    screen.route(f"{ORIGIN}/api/training-memory/jobs/job1/review/bulk", bulk)
    _open_review(screen)

    screen.fill("#search-input", "tabs")
    screen.wait_for_function(
        "document.querySelectorAll('#review-list [data-pair-id]').length === 1")
    screen.click("[data-bulk=approve_visible]")
    screen.wait_for_function(
        "document.querySelector('#review-list [data-pair-id]')?.dataset.status === 'approved'")

    assert _statuses(jd) == {"s1:a": "approved", "s1:b": "pending",
                             "s1:c": "pending", "s1:e": "skipped_no_feedback"}
    assert len(sent) == 1, sent
    assert sent[0]["pair_ids"] == ["s1:a"], sent[0]
    assert screen.errors == []
