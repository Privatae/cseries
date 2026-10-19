#!/usr/bin/env python3
"""The scoped diff reviewer — the gate on brain/mastercode/runtime/diff_review.py
and its four wiring points (2026-09-19).

Origin: project 229 shipped through eight phases with no review of any kind;
the only "reviewer" in the tree was review.compare, a per-file tiebreaker for
bake-offs, and asking for a review fired another harness rebuild. This module
is one scoped model call over a landed diff with a typed verdict.

Pins:
  1. the diff splitter and the chunk planner (per-file packing under the call
     cap; an oversized file is truncated and NAMED);
  2. the verdict parser: tolerant of prose around the JSON, derives ok from
     the findings (ok=true with a blocking list is false), None on junk;
  3. review_diff's envelope rules: an empty diff, a model error, an
     unparseable answer and a raised call are all ran=False with the reason —
     never ok=True; a multi-part diff merges (ok = every part ok, score = min);
  4. the prompt the reviewer sees carries the brief and the diff (no
     acceptance criteria), and NOTHING of the host (no identity, no memory, no tools);
  5. review_landed_run writes review/<harness>.json beside the diff and returns
     the envelope even with no pool and no route;
  6. the delivery line and the bench line state the verdict from the envelope;
  7. a blocking verdict never changes a phase's outcome (a build's outcome is
     whether it landed); the verdict is stated as the reviewer's findings;
  8. the wiring is present in source: the build lands, then reviews; the build
     envelope, the delivery record and the delivery prose carry `review`.

Pure: no DB, no network, no container. APP_ROOT=<repo> python3 scripts/test_diff_review.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))

from mastercode.runtime import diff_review as dr  # noqa: E402

FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS += 1


DIFF = (
    "diff --git a/app.js b/app.js\n"
    "--- a/app.js\n+++ b/app.js\n@@ -1,2 +1,3 @@\n-const x = 1;\n+const x = 2;\n+export const y = x;\n"
    "diff --git a/index.html b/index.html\n"
    "--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-<h1>old</h1>\n+<h1>new</h1>\n"
)
BRIEF = "Bump x and export y; keep the heading."


def _fake(reply: str | None = None, *, error: str | None = None, raise_exc: bool = False,
          replies: list | None = None):
    """A scripted model_fn: records every message list it was sent."""
    calls: list[dict] = []
    queue = list(replies or [])

    async def fn(messages, **kw):
        calls.append({"messages": messages, "kw": kw})
        if raise_exc:
            raise RuntimeError("boom")
        if error:
            return {"error": error, "content": ""}
        if queue:
            return {"content": queue.pop(0)}
        return {"content": reply or ""}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def main() -> int:
    # ── 1. splitter + planner ────────────────────────────────────────────
    files = dr.split_diff_files(DIFF)
    check("splitter keys chunks by new path, in order",
          list(files) == ["app.js", "index.html"] and files["app.js"].startswith("diff --git a/app.js"))
    check("splitter drops a preamble and ignores a headerless blob",
          dr.split_diff_files("garbage\n" + DIFF) == files and dr.split_diff_files("no diff here") == {})
    spaced = "diff --git a/my file.js b/my file.js\n--- a/my file.js\n+++ b/my file.js\n@@ -1 +1 @@\n-a\n+b\n"
    check("a path with a space is keyed whole, not dropped",
          list(dr.split_diff_files(spaced)) == ["my file.js"])
    # The server's own splitter (review.compare) had the same defect; pin the
    # fixed function by executing just its source, so this stays container-free.
    import ast as _ast
    _rsrc = (ROOT / "server/routes/review.py").read_text()
    _tree = _ast.parse(_rsrc)
    _fn = next(n for n in _tree.body if isinstance(n, _ast.FunctionDef) and n.name == "_parse_diff_files")
    _ns: dict = {}
    exec(compile(_ast.Module(body=[_fn], type_ignores=[]), "review._parse_diff_files", "exec"), _ns)
    check("review._parse_diff_files keys a spaced path the same way",
          list(_ns["_parse_diff_files"](spaced)) == ["my file.js"]
          and list(_ns["_parse_diff_files"](DIFF)) == ["app.js", "index.html"])
    calls, trunc = dr.plan_chunks(files, max_call_chars=10_000)
    check("small diff is one call, nothing truncated", len(calls) == 1 and len(calls[0]) == 2 and trunc == [])
    calls, trunc = dr.plan_chunks(files, max_call_chars=len(files["app.js"]) + 5)
    # index.html's chunk is the smaller of the two, so it packs first.
    check("diff over the call cap splits by file, smaller source first",
          len(calls) == 2 and [c[0][0] for c in calls] == ["index.html", "app.js"])
    big = {"bundle.js": "x" * 500, "a.py": "y" * 10}
    calls, trunc = dr.plan_chunks(big, max_call_chars=10_000, max_file_chars=100)
    _bundle = dict(calls[0])["bundle.js"]
    check("an oversized file is truncated with a marker and NAMED",
          trunc == ["bundle.js"] and "truncated: 400 more characters of bundle.js" in _bundle
          and len(_bundle) < 300)

    # ── 2. parser ─────────────────────────────────────────────────────────
    v = dr.parse_verdict('Sure! ```json\n{"ok": true, "score": 88, "summary": "fine", "blocking": [], '
                         '"non_blocking": [{"file": "app.js", "line": 2, "issue": "magic number", "fix": "name it"}]}\n```')
    check("parser reads JSON wrapped in prose and a fence",
          v and v["ok"] is True and v["score"] == 88 and v["non_blocking"][0]["line"] == 2)
    v = dr.parse_verdict('{"ok": true, "score": 90, "summary": "s", "blocking": [{"file": "app.js", "line": 1, "issue": "y is undefined at import time", "fix": "define y"}]}')
    check("ok=true with a blocking finding is DERIVED false", v and v["ok"] is False and len(v["blocking"]) == 1)
    v = dr.parse_verdict('{"ok": false, "score": "bad", "blocking": [{"issue": ""}, "junk", {"file": "a", "issue": "real"}]}')
    check("junk findings and a junk score are dropped, real ones kept",
          v and v["score"] is None and len(v["blocking"]) == 1 and v["blocking"][0]["issue"] == "real")
    check("no object / no ok key → None",
          dr.parse_verdict("nothing here") is None and dr.parse_verdict('{"summary": "x"}') is None
          and dr.parse_verdict("") is None)
    check("trailing-comma JSON is repaired once",
          (dr.parse_verdict('{"ok": true, "score": 70, "summary": "fine", "blocking": [],}') or {}).get("ok") is True)
    # A string "false" is not a pass, a
    # non-boolean ok is not an answer, and a pass without a summary is not
    # evidence of a review.
    v = dr.parse_verdict('{"ok": "false", "score": 50, "summary": "meh", "blocking": []}')
    check("ok as the string \"false\" reads as NOT ok (bool(\"false\") trap)", v and v["ok"] is False)
    check("ok as a non-boolean word is no answer", dr.parse_verdict('{"ok": "yes", "summary": "x"}') is None)
    check("a bare pass with no summary is not a verdict",
          dr.parse_verdict('{"ok": true}') is None and dr.parse_verdict('{"ok": true, "score": 90, "summary": ""}') is None)
    check("a block with no summary still counts (the findings are the evidence)",
          (dr.parse_verdict('{"ok": false, "blocking": [{"file": "a", "issue": "x"}]}') or {}).get("ok") is False)

    # ── 3. review_diff envelope rules ────────────────────────────────────
    env = asyncio.run(dr.review_diff(diff="", brief=BRIEF, model_fn=_fake("{}")))
    check("empty diff → ran=False with reason, never ok",
          env["ran"] is False and env["ok"] is None and "empty diff" in env["reason"] and env["calls"] == 0)
    env = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_fake(error="no model configured")))
    check("model error → ran=False carrying the gateway's error",
          env["ran"] is False and env["ok"] is None and "no model configured" in env["reason"])
    env = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_fake("I think it looks fine.")))
    check("unparseable answer → ran=False 'unparseable', never a pass",
          env["ran"] is False and env["ok"] is None and "unparseable" in env["reason"])
    env = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_fake(raise_exc=True)))
    check("a raised call is caught → ran=False with the exception",
          env["ran"] is False and "boom" in env["reason"])
    good = '{"ok": true, "score": 85, "summary": "does the brief", "blocking": [], "non_blocking": []}'
    fn = _fake(good)
    env = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=fn))
    check("clean diff → ran, ok, score, files_reviewed, one call",
          env["ran"] and env["ok"] is True and env["score"] == 85
          and sorted(env["files_reviewed"]) == ["app.js", "index.html"] and env["calls"] == 1)
    blocked = '{"ok": false, "score": 40, "summary": "y undefined", "blocking": [{"file": "app.js", "line": 3, "issue": "y exported before x assigned", "fix": "reorder"}], "non_blocking": []}'
    env2 = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_fake(replies=[good, blocked]),
                                      max_call_chars=len(files["app.js"]) + 5))
    check("multi-part: ok is every part ok, score is the min, findings concatenate, calls counted",
          env2["ran"] and env2["ok"] is False and env2["score"] == 40 and len(env2["blocking"]) == 1
          and env2["calls"] == 2 and env2["files_reviewed"] == ["index.html", "app.js"])
    env3 = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_fake(replies=[good, "??"]),
                                      max_call_chars=len(files["app.js"]) + 5))
    check("multi-part with one failed part → ran=False, the answered part's files kept, reason names the part",
          env3["ran"] is False and env3["ok"] is None and env3["files_reviewed"] == ["index.html"]
          and "part 2/2" in env3["reason"] and env3.get("raw_tail") == "??")
    # A call cap and a per-call timeout.
    env4 = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_fake(good),
                                      max_call_chars=len(files["app.js"]) + 5, max_calls=1))
    check("calls beyond the cap are not made: files_skipped named, summary stays the model's, still ran",
          env4["ran"] and env4["ok"] is True and env4["calls"] == 1 and env4["files_skipped"] == ["app.js"]
          and env4["files_reviewed"] == ["index.html"] and env4["summary"] == "does the brief"
          and env4["files_total"] == 2)
    _cov = dr.review_facts({"review": env4})
    check("coverage is stated as the platform's own line, not inside the reviewer's summary",
          "Coverage: 1 of 2 files reviewed; NOT reviewed (past the call cap): app.js." in _cov
          and "Reviewer's summary: does the brief" in _cov)
    # Packing order: source first, smaller first; generated last.
    _ordered = {"package-lock.json": "L" * 50, "src/big.py": "B" * 40, "src/small.py": "S" * 10, "dist/app.min.js": "M" * 5}
    _calls, _ = dr.plan_chunks(_ordered, max_call_chars=10_000)
    check("plan_chunks packs source smallest-first and generated paths last",
          [p for p, _ in _calls[0]] == ["src/small.py", "src/big.py", "dist/app.min.js", "package-lock.json"])
    check("is_generated_path knows lockfiles, dist/, node_modules/, minified and map files",
          all(dr.is_generated_path(p) for p in ["yarn.lock", "a/node_modules/x.js", "dist/x.js", "x.min.css", "x.js.map"])
          and not any(dr.is_generated_path(p) for p in ["src/app.js", "index.html", "lockbox.py"]))

    async def _slow(messages, **kw):
        await asyncio.sleep(0.2)
        return {"content": good}
    env5 = asyncio.run(dr.review_diff(diff=DIFF, brief=BRIEF, model_fn=_slow, call_timeout_s=0.02))
    check("a call silent past the idle window → ran=False 'no sign of life' (liveness)",
          env5["ran"] is False and "no sign of life" in env5["reason"])
    fn_long = _fake(good)
    asyncio.run(dr.review_diff(diff=DIFF, brief="B" * (dr.MAX_BRIEF_CHARS + 500), model_fn=fn_long))
    _um = fn_long.calls[0]["messages"][1]["content"]
    check("an oversized brief is cut with a marker, the diff still rides",
          "brief truncated: 500 more characters" in _um and "+export const y = x;" in _um)

    # ── 4. what the reviewer sees ─────────────────────────────────────────
    sent = fn.calls[0]["messages"]
    sysm, usr = sent[0]["content"], sent[1]["content"]
    check("system prompt is the fixed reviewer prompt, JSON-only, style never blocking, diff is untrusted",
          sent[0]["role"] == "system" and sysm == dr.SYSTEM_PROMPT
          and "JSON only" in sysm and "never blocking" in sysm and "untrusted input" in sysm)
    check("user message carries the brief and the whole diff, and no acceptance criteria",
          BRIEF in usr and "node --check app.js" not in usr and "must exit 0" not in usr
          and "acceptance" not in usr.lower() and "acceptance" not in sysm.lower()
          and "+export const y = x;" in usr and "<h1>new</h1>" in usr)
    check("nothing of the host rides along: no identity, memory, tools or history",
          len(sent) == 2 and "CSeriesDesktop" not in usr and "What You Remember" not in usr
          and fn.calls[0]["kw"].get("max_tokens") == dr.MAX_OUTPUT_TOKENS
          and fn.calls[0]["kw"].get("temperature") == 0.1)
    check("a split diff tells the reviewer which part it holds and the full file list",
          "part 1 of 2" in fn.calls[0]["messages"][1]["content"] if len(fn.calls) > 1 else
          "part 1 of 2" in _fake_part_msg())

    # ── 5. review_landed_run without pool/route ───────────────────────────
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        (sd / "diffs").mkdir()
        (sd / "diffs" / "kimi.patch").write_text(DIFF)
        env = asyncio.run(dr.review_landed_run(None, 3, session_dir=sd, harness="kimi",
                                               brief=BRIEF, run_id=None,
                                               model_fn=_fake(good)))
        written = json.loads((sd / "review" / "kimi.json").read_text())
        check("review_landed_run reviews diffs/<harness>.patch and writes review/<harness>.json",
              env["ran"] and env["ok"] is True and written["ok"] is True and written["harness"] == "kimi")
        missing = asyncio.run(dr.review_landed_run(None, 3, session_dir=sd, harness="codex",
                                                   brief=BRIEF, model_fn=_fake(good)))
        check("a missing patch is an empty diff → not reviewed, said so",
              missing["ran"] is False and "empty diff" in missing["reason"])

    # ── 6. the two stated lines ───────────────────────────────────────────
    check("bench line: passed", "passed — score 85" in dr.activity_line(env))
    check("bench line: blocked", "BLOCKED — 1 blocking" in dr.activity_line(env2))
    check("bench line: not run", "not run (" in dr.activity_line(env3))
    facts_ok = dr.review_facts({"review": env})
    facts_bad = dr.review_facts({"review": env2})
    facts_none = dr.review_facts({"review": env3})
    check("delivery line: PASSED names score and summary",
          "PASSED, score 85" in facts_ok and "does the brief" in facts_ok)
    check("delivery line: a blocking verdict lists file:line and says they are the reviewer's findings",
          facts_bad.startswith("Code review: 1 blocking finding(s), 0 non-blocking.")
          and "app.js:3" in facts_bad
          and facts_bad.rstrip().endswith("These are the reviewer's findings; report them as the reviewer's.")
          and "BLOCKED" not in facts_bad and "'review'" not in facts_bad)
    check("delivery line: not run says so with the reason",
          facts_none.startswith("Code review: not run — ") and "part 2/2" in facts_none)
    check("no verdict on the record → no line", dr.review_facts({}) == "" and dr.review_facts({"review": None}) == "")

    # ── 7. a verdict never changes the outcome ───────────────────────────
    import mastercode.runtime.tools as _tools
    from mastercode.runtime.tools import _PHASE_DONE, _PHASE_FAILED
    check("tools has no phase outcome over the verdict (a landed build is complete whatever the reviewer said)",
          not hasattr(_tools, "_phase_outcome") and _PHASE_DONE == "complete" and _PHASE_FAILED == "failed")
    _run_src = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
    _run_src = _run_src[_run_src.index("async def _run_phase_build("):]
    _run_src = _run_src[:_run_src.index("\nasync def ", 10)]
    check("the phase's status is landed -> complete, failed harness -> failed, with the reviewer's verdict ignored",
          "phase_final = _PHASE_DONE if ok else _PHASE_FAILED" in _run_src
          and "_PHASE_REVIEW" not in _run_src.split("phase_final = _PHASE_DONE if ok")[1].split("_checks = ")[0])

    # ── 8. wiring present in source ──────────────────────────────────────
    review_py = (ROOT / "server/routes/review.py").read_text()
    land = review_py[review_py.index("async def run_single_harness_build("):]
    land = land[:land.index("\n# ── Routes")]
    check("the build reviews AFTER the diff is persisted and BEFORE it returns",
          land.index(".patch\").write_text(_diff") < land.index("review_landed_run(")
          < land.index('"review": result.get("review")'))
    check("the review runs only for a landed success",
          "if ok and landed and session_dir is not None:" in land)
    check("session JSON carries review/<harness>.json", '"review": reviews,' in review_py)
    # The run's return used to leave the acceptance envelope out, so no phase
    # outcome ever saw a verdict. Pin the key.
    _ret = land[land.rindex("    return {"):]
    check("run_single_harness_build RETURNS the acceptance envelope",
          '"acceptance": result.get("acceptance"),' in _ret and '"review": result.get("review"),' in _ret)
    tools_py = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
    check("the build envelope carries the verdict", '"review": out.get("review")' in tools_py)
    check("the verdict feeds the check summaries and the review hold, not the outcome",
          "_pipeline.diff_review_verdict(_rev_env)" in tools_py
          and '_pipeline.decide(ok, _pipe, _verdicts)["checks"]' in tools_py)
    # The delivery record is built from entity_tasks.result, which
    # only _factory_build_task_finish's merge writes — the verdict must be on it.
    _finish = tools_py[tools_py.index("async def _factory_build_task_finish("):]
    _finish = _finish[:_finish.index("\nasync def ", 10)]
    check("the task-row merge carries the verdict (what the delivery record is built from)",
          '"review": res.get("review")' in _finish and '"acceptance": res.get("acceptance")' in _finish)
    check("the 'review' chain-stop texts for a blocked landing are gone",
          "reviewer BLOCKED it with" not in tools_py and "_acc_passed and _rev_blocked" not in tools_py)
    disp = (ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
    check("the delivery record carries the verdict", '"review": (res.get("review")' in disp)
    sess = (ROOT / "brain/mastercode/runtime/session.py").read_text()
    check("the delivery states the verdict right after the proof line",
          sess.index("_facts += _acceptance_facts(record)") < sess.index("review_facts as _review_facts"))
    from mastercode.runtime.session import _acceptance_facts
    _rec = {"phase_status": "complete", "acceptance_declared": True,
            "acceptance": {"ran": True, "ok": True, "results": [{"ok": True}, {"ok": True}]}, "review": env2}
    _line = _acceptance_facts(_rec)
    check("checks line for a landed phase the reviewer found problems in: checks passed, information only",
          "2 of 2 passed" in _line and "information only" in _line and "reviewer" not in _line)
    _rec2 = {"phase_status": "complete", "acceptance_declared": True,
             "acceptance": {"ran": False, "ok": None, "reason": "/check HTTP 404", "results": []}}
    check("checks line when the checks could not run says so as information",
          "did not run (/check HTTP 404)" in _acceptance_facts(_rec2))

    print(f"\n{'ALL PASS' if not FAILS else str(FAILS) + ' FAIL(S)'}")
    return 1 if FAILS else 0


def _fake_part_msg() -> str:
    files = dr.split_diff_files(DIFF)
    calls, _ = dr.plan_chunks(files, max_call_chars=len(files["app.js"]) + 5)
    return dr._user_message(BRIEF, calls[0], 0, len(calls), list(files))


if __name__ == "__main__":
    sys.exit(main())
