#!/usr/bin/env python3
"""A run's variants are reachable, and what a run lands is what it says.

Found while adding modules and versions, each in code the one build door had
just leaned on:

  1. A compare's later runs are `<harness>-sN`. review.compare listed them
     with a preview_url and told the host to adopt them by that id, but the
     preview, pick and adopt routes gated on the bare harness set — every one
     404'd. _session_entrypoints walked harnesses, not runs, too.
  2. The land step (build → project dir) followed symlinks; adopt skips them.
     A seeded worktree keeps the project's own links as links, so a link in
     the project could make the land read outside the run.
  3. `changed_files` was every file in the worktree — seeded ones included —
     and rode the commit message and the delivery as "changed".
  4. The learned-preference text was prepended after the build contracts, so
     it sat AHEAD of them while the comment said after.

Review of the fix (Opus, 2026-09-22) added: changed_files measured by the
land itself (exact for greenfield, non-ASCII, deletions and failed runs); the
diff parser decodes git's quoted names; a pick records the base harness and
refuses a run the session never had; the per-harness ranking folds variants.

Container-free: temp dirs, the stub build engine (FACTORY_ENGINE=stub +
CSERIES_SMOKE=1), no pod.

Run: APP_ROOT=<repo> python3 scripts/test_run_landing_truth.py
"""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from fastapi import HTTPException  # noqa: E402
from server.routes import review as rv  # noqa: E402
from server.routes import _preference_profile as pp  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


print("== 1. variant ids reach preview, adopt and the entry map ==")
for vid, want in (("codex", True), ("codex-s2", True), ("codex-s16", True),
                  ("codex-s1", False), ("codex-s17", False), ("nope-s2", False),
                  ("codex-s2x", False), ("../codex-s2", False), ("codex-s2\n", False),
                  ("codex-s02", False)):
    check(f"_is_variant_id({vid!r}) is {want}", rv._is_variant_id(vid) is want)

with tempfile.TemporaryDirectory() as td:
    saved = (rv.SESSIONS_ROOT, rv.PROJECTS_ROOT)
    try:
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        rv.PROJECTS_ROOT = Path(td) / "projects"
        sd = rv.SESSIONS_ROOT / "s1"
        for v in ("codex", "codex-s2"):
            (sd / "work" / v).mkdir(parents=True)
            (sd / "work" / v / "index.html").write_text(f"<p>{v}</p>")
        manifest = {"session_id": "s1", "worktree_root": str(sd / "work"),
                    "harnesses": ["codex"],
                    "variants": [{"vid": "codex", "harness": "codex"},
                                 {"vid": "codex-s2", "harness": "codex"}]}
        (sd / "manifest.json").write_text(json.dumps(manifest))
        try:
            root = rv._harness_root("s1", "codex-s2")
            check("preview resolves the second variant's worktree", root.name == "codex-s2", root)
        except HTTPException as e:
            check("preview resolves the second variant's worktree", False, e.detail)
        try:
            src = rv._adopt_src("s1", "codex-s2", manifest)
            check("adopt resolves the second variant's files", src.name == "codex-s2", src)
        except HTTPException as e:
            check("adopt resolves the second variant's files", False, e.detail)
        ents = rv._session_entrypoints(manifest)
        check("the entry map covers every run, not every harness",
              ents == {"codex": True, "codex-s2": True}, ents)
        for bad in ("codex-s1", "rm-s2"):
            try:
                rv._harness_root("s1", bad)
                check(f"preview refuses {bad!r}", False, "accepted")
            except HTTPException as e:
                check(f"preview refuses {bad!r}", e.status_code == 404, e.status_code)
    finally:
        rv.SESSIONS_ROOT, rv.PROJECTS_ROOT = saved


print("== 2. the land step never follows a link ==")
with tempfile.TemporaryDirectory() as td:
    saved = rv.PROJECTS_ROOT
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        (rv.PROJECTS_ROOT / "7").mkdir(parents=True)
        secret = Path(td) / "outside-secret.txt"
        secret.write_text("TOKEN=hunter2")
        wt = Path(td) / "wt"
        (wt / "sub").mkdir(parents=True)
        (wt / "index.html").write_text("<p>ok</p>")
        (wt / "leak.txt").symlink_to(secret)
        (wt / "linked-dir").symlink_to(Path(td))
        landed = rv._land_worktree_in_project(7, wt)
        proj = rv.PROJECTS_ROOT / "7"
        check("the real file lands", (proj / "index.html").read_text() == "<p>ok</p>", landed)
        check("a linked file is not landed", "leak.txt" not in landed
              and not (proj / "leak.txt").exists(), landed)
        check("a linked directory is not walked", not any(p.startswith("linked-dir") for p in landed), landed)
        rv._snapshot_to_project(7, wt, ["leak.txt"])
        check("_snapshot_to_project refuses a linked source even when named",
              not (proj / "leak.txt").exists())
    finally:
        rv.PROJECTS_ROOT = saved


print("== 2c. a land never writes into an ARTIFACT's .history/ ==")
# An artifact's .history/ is its revision store and trial mark, written only
# by the artifact ops (artifacts._reject_unsafe_path). A build run against an
# artifact's id lands in the same directory; the land refuses it the same way
# (review of the desktop render check, 2026-10-01). An ordinary project's
# .history/ is just a folder.
with tempfile.TemporaryDirectory() as td:
    saved = rv.PROJECTS_ROOT
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        art = rv.PROJECTS_ROOT / "8"
        (art / ".history").mkdir(parents=True)
        (art / ".history" / "log.jsonl").write_text('{"rev": "r1"}\n')
        wt = Path(td) / "wt8"
        (wt / ".history").mkdir(parents=True)
        (wt / "index.html").write_text("<p>new</p>")
        (wt / ".history" / "log.jsonl").write_text('{"rev": "forged"}\n')
        (wt / ".history" / "design-trial").write_text("1 forged")
        landed = rv._land_worktree_in_project(8, wt)
        check("an artifact's own file lands", (art / "index.html").read_text() == "<p>new</p>", landed)
        check("...its .history/ is NOT written by the land",
              (art / ".history" / "log.jsonl").read_text() == '{"rev": "r1"}\n'
              and not (art / ".history" / "design-trial").exists()
              and not any(p.startswith(".history") for p in landed), landed)
        code = rv.PROJECTS_ROOT / "9"
        code.mkdir(parents=True)
        wt9 = Path(td) / "wt9"
        (wt9 / ".history").mkdir(parents=True)
        (wt9 / ".history" / "notes.md").write_text("an editor's local history")
        rv._land_worktree_in_project(9, wt9)
        check("...while an ordinary project's .history/ lands like any folder",
              (code / ".history" / "notes.md").is_file())
    finally:
        rv.PROJECTS_ROOT = saved


print("== 3-4. a stub build: prompt order, and changed_files is the diff ==")
captured: dict = {}
_orig_stub = rv._stub_harness_build


def _capture_stub(worktree, prompt):
    captured["prompt"] = prompt
    out = _orig_stub(worktree, prompt)
    (worktree / "new-feature.js").write_text("export const x = 1;\n")
    return out


async def _target(h, model=None):
    return "claude-code", None, None


async def _no_settings():
    return {}

with tempfile.TemporaryDirectory() as td:
    saved = (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
             pp.summarize_for_prompt, rv.resolve_build_target, rv._coding_harness_settings,
             os.environ.get("FACTORY_ENGINE"), os.environ.get("CSERIES_SMOKE"))
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        proj = rv.PROJECTS_ROOT / "9"
        proj.mkdir(parents=True)
        (proj / "README.md").write_text("seeded, untouched\n")
        (proj / "notes.txt").write_text("seeded, untouched too\n")
        os.environ["FACTORY_ENGINE"], os.environ["CSERIES_SMOKE"] = "stub", "1"
        rv._stub_harness_build = _capture_stub
        pp.summarize_for_prompt = lambda pid: "LEARNED-PREFS-MARKER"
        rv.resolve_build_target = _target
        rv._coding_harness_settings = _no_settings
        out = run(rv.run_single_harness_build(project_id=9, prompt="TASK-MARKER build it",
                                              harness="claude-code"))
        p = captured.get("prompt", "")
        i_contract = p.find("PLATFORM DEPLOYMENT CONTRACT")
        i_learned = p.find("LEARNED-PREFS-MARKER")
        i_task = p.find("TASK-MARKER")
        check("the build ran on the stub", out.get("ok") is True, out.get("error"))
        check("contracts, then learned preferences, then the task",
              -1 < i_contract < i_learned < i_task, (i_contract, i_learned, i_task))
        # The run view shows the manifest as "what the host sent the harness";
        # it must be the whole string the harness got, not the task alone.
        _mf = [json.loads(m.read_text()) for m in rv.SESSIONS_ROOT.glob("*/manifest.json")]
        _sent = [(m.get("sent_prompt") or {}).get("claude-code") for m in _mf]
        check("the manifest records exactly what the harness was sent",
              len(_mf) == 1 and _sent[0] == p and p, (len(_mf), len(_sent[0] or ""), len(p)))
        check("...while 'prompt' stays the task as asked",
              _mf and _mf[0].get("prompt") == "TASK-MARKER build it", _mf and _mf[0].get("prompt"))
        cf = out.get("changed_files") or []
        check("changed_files names what the run wrote",
              "new-feature.js" in cf and "index.html" in cf, cf)
        check("...and not the seeded files it never touched",
              "README.md" not in cf and "notes.txt" not in cf, cf)
    finally:
        (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, rv._stub_harness_build,
         pp.summarize_for_prompt, rv.resolve_build_target, rv._coding_harness_settings,
         fe, sm) = saved
        for k, v in (("FACTORY_ENGINE", fe), ("CSERIES_SMOKE", sm)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

print("== 5. changed_files is exact: greenfield, deletions, failed runs ==")
with tempfile.TemporaryDirectory() as td:
    saved = (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, pp.summarize_for_prompt,
             rv.resolve_build_target, rv._coding_harness_settings, rv._stub_harness_build,
             os.environ.get("FACTORY_ENGINE"), os.environ.get("CSERIES_SMOKE"))
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        (rv.PROJECTS_ROOT / "11").mkdir(parents=True)
        os.environ["FACTORY_ENGINE"], os.environ["CSERIES_SMOKE"] = "stub", "1"
        pp.summarize_for_prompt = lambda pid: ""
        rv.resolve_build_target = _target
        rv._coding_harness_settings = _no_settings
        rv._stub_harness_build = _orig_stub
        out = run(rv.run_single_harness_build(project_id=11, prompt="greenfield",
                                              harness="claude-code"))
        cf = out.get("changed_files") or []
        check("a GREENFIELD build lists what it wrote (no diff is involved)",
              out.get("ok") is True and "index.html" in cf and "app.yaml" in cf, (out.get("error"), cf))

        (rv.PROJECTS_ROOT / "12").mkdir(parents=True)
        (rv.PROJECTS_ROOT / "12" / "gone.txt").write_text("the run deletes me\n")

        def _deleting_stub(worktree, prompt):
            r = _orig_stub(worktree, prompt)
            (worktree / "gone.txt").unlink()
            (worktree / "página.html").write_text("<p>hola</p>")
            return r
        rv._stub_harness_build = _deleting_stub
        out = run(rv.run_single_harness_build(project_id=12, prompt="seeded", harness="claude-code"))
        cf = out.get("changed_files") or []
        check("a non-ASCII file the run wrote is listed", "página.html" in cf, cf)
        check("a file the run deleted is not claimed as landed (the land is additive)",
              "gone.txt" not in cf and (rv.PROJECTS_ROOT / "12" / "gone.txt").exists(), cf)

        def _failing_stub(worktree, prompt):
            (worktree / "partial.js").write_text("half")
            return {"ok": False, "harness": "stub", "exit_code": 1, "error": "boom"}
        rv._stub_harness_build = _failing_stub
        out = run(rv.run_single_harness_build(project_id=12, prompt="fails", harness="claude-code"))
        check("a failed run reports no changed files", out.get("ok") is False
              and out.get("changed_files") == [], out.get("changed_files"))
    finally:
        (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, pp.summarize_for_prompt,
         rv.resolve_build_target, rv._coding_harness_settings, rv._stub_harness_build,
         fe, sm) = saved
        for k, v in (("FACTORY_ENGINE", fe), ("CSERIES_SMOKE", sm)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


print("== 6. the diff parser keeps names git quotes ==")
with tempfile.TemporaryDirectory() as td:
    g = ["git", "-C", td, "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(g[:3] + ["init", "-q"], check=True)
    for n in ("gone.txt", "old.js", "same.js"):
        Path(td, n).write_text(n)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "base"], check=True)
    Path(td, "gone.txt").unlink()
    Path(td, "old.js").rename(Path(td, "renamed.js"))
    for n in ("café.js", 'q"uote.js', "my file.js", "new.js"):
        Path(td, n).write_text(n)
    Path(td, "img.png").write_bytes(bytes(range(256)))
    subprocess.run(g + ["add", "-A"], check=True)
    diff = subprocess.run(g + ["diff", "--cached", "-M"], capture_output=True, text=True).stdout
    keys = set(rv._parse_diff_files(diff))
    want = {"café.js", 'q"uote.js', "my file.js", "new.js", "img.png", "renamed.js", "gone.txt"}
    check("every changed path is keyed, quoted names included", want <= keys, sorted(keys))
    check("...and nothing it did not change", "same.js" not in keys, sorted(keys))


print("== 7. a pick records the base harness, and only a run of the session ==")
with tempfile.TemporaryDirectory() as td:
    saved = (rv.SESSIONS_ROOT,)
    import server.routes._preference_profile as _ppm
    _saved_up = _ppm.update_profiles
    seen_profile: list = []
    _ppm.update_profiles = lambda pid, rec: seen_profile.append(rec)
    try:
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        sd = rv.SESSIONS_ROOT / "p1"
        sd.mkdir(parents=True)
        (sd / "manifest.json").write_text(json.dumps({
            "session_id": "p1", "project_id": 5, "harnesses": ["codex"],
            "variants": [{"vid": "codex", "harness": "codex"},
                         {"vid": "codex-s2", "harness": "codex"}]}))
        out = run(rv.pick_harness_service(None, session_id="p1", harness="codex-s2"))
        hp = json.loads((sd / "human_pick.json").read_text())
        check("the pick keeps the variant it chose", hp.get("variant") == "codex-s2", hp)
        check("...and records the BASE harness for the taste log and profile",
              hp.get("harness") == "codex" and seen_profile and seen_profile[-1]["harness"] == "codex",
              (hp, seen_profile))
        try:
            run(rv.pick_harness_service(None, session_id="p1", harness="codex-s3"))
            check("a variant this session never ran is refused", False, "accepted")
        except HTTPException as e:
            check("a variant this session never ran is refused", e.status_code == 400, e.detail)
    finally:
        rv.SESSIONS_ROOT = saved[0]
        _ppm.update_profiles = _saved_up

print("== 7b. picks: element picks validated and folded; pre-variant sessions ==")
with tempfile.TemporaryDirectory() as td:
    import server.routes._preference_profile as _ppm2
    _saved_up2, _saved_root = _ppm2.update_profiles, rv.SESSIONS_ROOT
    _ppm2.update_profiles = lambda pid, rec: None
    try:
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        sd = rv.SESSIONS_ROOT / "old1"
        sd.mkdir(parents=True)
        (sd / "manifest.json").write_text(json.dumps({
            "session_id": "old1", "harnesses": ["codex", "kimi"], "samples": {"codex": 2}}))
        check("a pre-variant session lists its sampled runs",
              rv._session_run_ids(json.loads((sd / "manifest.json").read_text()))
              == ["codex", "codex-s2", "kimi"])
        run(rv.pick_harness_service(None, session_id="old1", harness="codex-s2",
                                    element_picks={"header": "kimi", "css": "codex-s2"}))
        hp = json.loads((sd / "human_pick.json").read_text())
        check("element picks are recorded by base harness",
              hp.get("element_picks") == {"header": "kimi", "css": "codex"}, hp)
        try:
            run(rv.pick_harness_service(None, session_id="old1", harness="codex",
                                        element_picks={"css": "grok"}))
            check("an element pick naming a run the session never had is refused", False, "accepted")
        except HTTPException as e:
            check("an element pick naming a run the session never had is refused",
                  e.status_code == 400, e.detail)
    finally:
        _ppm2.update_profiles, rv.SESSIONS_ROOT = _saved_up2, _saved_root


print("== 2b. the land never writes through a project-side link ==")
with tempfile.TemporaryDirectory() as td:
    saved = rv.PROJECTS_ROOT
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        proj = rv.PROJECTS_ROOT / "8"
        proj.mkdir(parents=True)
        (proj / "v1.css").write_text("old")
        (proj / "latest.css").symlink_to(proj / "v1.css")
        wt = Path(td) / "wt8"
        wt.mkdir()
        (wt / "latest.css").write_text("new")
        changed: list = []
        rv._land_worktree_in_project(8, wt, changed)
        check("the link's target is not overwritten under another name",
              (proj / "v1.css").read_text() == "old" and "latest.css" not in changed,
              ((proj / "v1.css").read_text(), changed))
        (proj / "static" / "assets").mkdir(parents=True)
        (proj / "assets").symlink_to(proj / "static" / "assets")
        (wt / "assets").mkdir()
        (wt / "assets" / "b.css").write_text("b")
        landed8 = rv._land_worktree_in_project(8, wt)
        check("a file behind a project-side linked dir is neither written nor counted as landed",
              not (proj / "static" / "assets" / "b.css").exists()
              and "assets/b.css" not in landed8, landed8)
    finally:
        rv.PROJECTS_ROOT = saved


print("== 8. the acceptance ranking is per harness, variants folded ==")
from server.routes import compute as _cp  # noqa: E402
ranked = _cp._rank_from(
    [{"harness": "codex", "runs": 1, "accepted": 0},
     {"harness": "codex-s2", "runs": 1, "accepted": 1}],
    [{"session_id": "p1", "harness": "codex", "variant": "codex-s2"}], None)
by = {r["harness"]: r for r in ranked}
check("one row per harness: codex-s2's runs fold into codex",
      set(by) == {"codex"} and by["codex"]["runs"] == 2 and by["codex"]["accepted"] == 1
      and by["codex"]["base_picks"] == 1, ranked)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
