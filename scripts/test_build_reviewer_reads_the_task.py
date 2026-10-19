#!/usr/bin/env python3
"""The build's reviewer is handed the TASK, not the prompt the harness got.

Found on one Host. The
reviewer scored a correct card-game build 8/100 because "the brief asked to
rebuild a combined notes app". That text came from the LEARNED PREFERENCES
block, not from the task. run_single_harness_build passed `brief=prompt` to
diff_review.review_landed_run after `prompt` had become the composed text:
the SDK contract (about 13.4K characters), the deployment contract, the
learned preferences, the desktop brief and the harness notes, with the task
last. The reviewer caps the brief at MAX_BRIEF_CHARS (16,000), so the task
itself could be cut off the end.

What this pins: the brief the reviewer receives is exactly the task the build
was asked for, and none of the contracts, preferences or harness notes are in
it, while the harness itself still gets all of them.

Container-free: temp dirs, the stub build engine (FACTORY_ENGINE=stub +
CSERIES_SMOKE=1), a stubbed reviewer, no pod, no model.

Run: APP_ROOT=<repo> python3 scripts/test_build_reviewer_reads_the_task.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
from pathlib import Path

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.routes import review as rv  # noqa: E402
from server.routes import _preference_profile as pp  # noqa: E402
from mastercode.runtime import diff_review as dr  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


TASK = "TASK-MARKER build a card game with drag and drop tiles"
LEARNED = "LEARNED-PREFS-MARKER rebuild a combined notes app"
NOTE = "HARNESS-NOTE-MARKER use plain file writes"

sent: dict = {}
reviewed: dict = {}
_orig_stub = rv._stub_harness_build


def _capture_stub(worktree, prompt):
    sent["prompt"] = prompt
    return _orig_stub(worktree, prompt)


async def _fake_review(pool, entity_id, *, session_dir, harness, brief, run_id=None, model_fn=None):
    reviewed["brief"] = brief
    reviewed["harness"] = harness
    return {"ran": True, "ok": True, "score": 90, "summary": "stub", "blocking": [],
            "non_blocking": [], "harness": harness, "run_id": run_id}


async def _target(h, model=None):
    return "claude-code", None, None


async def _no_settings():
    return {}


print("== the reviewer gets the task; the harness gets the whole prompt ==")
with tempfile.TemporaryDirectory() as td:
    saved = (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
             pp.summarize_for_prompt, rv.resolve_build_target, rv._coding_harness_settings,
             dr.review_landed_run, dict(rv.HARNESS_PROMPT_NOTES),
             os.environ.get("FACTORY_ENGINE"), os.environ.get("CSERIES_SMOKE"))
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        (rv.PROJECTS_ROOT / "9423").mkdir(parents=True)
        os.environ["FACTORY_ENGINE"], os.environ["CSERIES_SMOKE"] = "stub", "1"
        rv._stub_harness_build = _capture_stub
        pp.summarize_for_prompt = lambda pid: LEARNED
        rv.resolve_build_target = _target
        rv._coding_harness_settings = _no_settings
        rv.HARNESS_PROMPT_NOTES["claude-code"] = NOTE
        dr.review_landed_run = _fake_review
        out = run(rv.run_single_harness_build(project_id=9423, prompt=TASK,
                                              harness="claude-code",
                                              billing_key="factory-build-9434"))
        p = sent.get("prompt", "")
        b = reviewed.get("brief")
        check("the build ran on the stub and landed", out.get("ok") is True, out.get("error"))
        check("the reviewer ran on this run", reviewed.get("harness") == "claude-code"
              and (out.get("review") or {}).get("score") == 90, (reviewed, out.get("review")))
        check("the harness got the contracts, the preferences, the note and the task",
              "PLATFORM DEPLOYMENT CONTRACT" in p and LEARNED in p and NOTE in p and TASK in p,
              len(p))
        check("the reviewer's brief is exactly the task", b == TASK, (b or "")[:200])
        check("...with no learned preferences in it", LEARNED not in (b or ""), (b or "")[:200])
        check("...no contract in it", "CONTRACT" not in (b or ""), (b or "")[:200])
        check("...and no harness note in it", NOTE not in (b or ""), (b or "")[:200])
    finally:
        (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
         pp.summarize_for_prompt, rv.resolve_build_target, rv._coding_harness_settings,
         dr.review_landed_run, notes, fe, sm) = saved
        rv.HARNESS_PROMPT_NOTES.clear()
        rv.HARNESS_PROMPT_NOTES.update(notes)
        for k, v in (("FACTORY_ENGINE", fe), ("CSERIES_SMOKE", sm)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
