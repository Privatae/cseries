#!/usr/bin/env python3
"""
A module run is told about EVERY project file its brief names: the ones
copied for it and, by name and with why, the ones that were not.

The copies into _ref_project/ were bounded at 2 MB a file and 8 MB in all,
and discovery stopped at 20,000 files, all silently (an INFO log line): the
harness's note listed only what was copied, so a named data file was left out
without the harness or the user hearing of it, which is the failure the
feature was built to stop (codex "reconstructed" a missing research.md). The
copies ride the same clone-in upload as the seeded worktree, which has no size
bound. What is still protected is the Host's disk (harness_input.disk_floor),
and a file left out for it is named.

  1. a named 3 MB data file is copied, byte for byte
  2. three named 3 MB files (9 MB, past the old 8 MB total) are all copied
  3. a named file past the first 20,000 in its folder is still found
  4. a named file that is not copied (a link) is reported with why
  5. the harness's note names every file not copied, with why, and says it
     has not read them; a run with nothing left out reads as before
  6. the run terminal and the activity feed get the same, for the build and
     for both compare waves, and the build's result carries it

Temp dirs, no pod, no DB. Run:
APP_ROOT=<repo> python3 scripts/test_module_build_names_reference_files_not_copied.py
"""

import os
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[ref-left-out] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    import server.routes.review as rv

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        projects = base / "projects"
        proj = projects / "301"
        for d in ("api", "data", "bulk"):
            (proj / d).mkdir(parents=True)
        blobs = {f"data/part{i}.csv": os.urandom(3_000_000) for i in range(3)}
        for rel, b in blobs.items():
            (proj / rel).write_bytes(b)
        (base / "outside.txt").write_text("not the project's")
        (proj / "data" / "linked.csv").symlink_to(base / "outside.txt")
        for i in range(20_050):
            (proj / "bulk" / f"f{i:05d}.txt").write_bytes(b"")
        (proj / "bulk" / "zz-last.txt").write_text("the one the brief names")
        rv.PROJECTS_ROOT = projects

        wt = rv._prepare_worktree(base / "work", "codex", seed=False, seed_from=proj / "api")

        # 1-2. Large files are copied.
        try:
            got = rv._place_references(wt, 301, ["data/part0.csv"], "api")
        except TypeError as e:
            got = [f"TypeError: {e}"]
        check("1. a named 3 MB data file is copied, byte for byte",
              got == ["data/part0.csv"]
              and (wt / "_ref_project/data/part0.csv").read_bytes() == blobs["data/part0.csv"],
              str(got))
        wt2 = rv._prepare_worktree(base / "work", "kimi", seed=False, seed_from=proj / "api")
        got2 = rv._place_references(wt2, 301, list(blobs), "api")
        check("2. three named 3 MB files (9 MB) are all copied",
              got2 == list(blobs), str(got2))

        # 3. The scan does not stop at 20,000 files.
        found = rv.brief_reference_paths("see bulk/zz-last.txt", "api", 301)
        check("3. a named file past the first 20,000 in its folder is found",
              found == ["bulk/zz-last.txt"], str(found))

        # 4. A file not copied is reported with why.
        wt3 = rv._prepare_worktree(base / "work", "grok", seed=False, seed_from=proj / "api")
        left: list = []
        try:
            got3 = rv._place_references(wt3, 301, ["data/part1.csv", "data/linked.csv"],
                                        "api", left)
        except TypeError as e:
            got3, left = [f"TypeError: {e}"], []
        check("4. a named file that is not copied (a link) is reported with why",
              got3 == ["data/part1.csv"] and len(left) == 1
              and left[0][0] == "data/linked.csv" and "link" in left[0][1],
              f"{got3} {left}")

        # 5. The note to the harness.
        try:
            note = rv._references_note(["data/part1.csv"], [("data/linked.csv", "it is a link"),
                                                            ("data/huge.bin", "copying it would "
                                                             "leave too little disk")])
        except TypeError as e:
            note = f"TypeError: {e}"
        check("5a. the harness's note names every file not copied, with why",
              "_ref_project/data/part1.csv" in note and "data/linked.csv (it is a link)" in note
              and "data/huge.bin (copying it would leave too little disk)" in note
              and "NOT" in note and "not read them" in note, note)
        check("5b. nothing left out reads as before",
              rv._references_note(["data/part1.csv"])
              == ("Project files your brief names from outside this module are copied into "
                  "_ref_project/ for you to read; they are reference only, and nothing written "
                  "under _ref_project/ is kept: _ref_project/data/part1.csv.\n\n")
              and rv._references_note([]) == "")
        try:
            only_left = rv._references_note([], [("data/linked.csv", "it is a link")])
        except TypeError as e:
            only_left = f"TypeError: {e}"
        check("5c. a brief whose named files were all left out still says so",
              "data/linked.csv (it is a link)" in only_left, only_left)

    # 6. Wiring: the build, the compare and its later waves all say it.
    src = Path(app_root, "server/routes/review.py").read_text()
    single = src.split("async def run_single_harness_build(")[1].split("\n# ── Routes")[0]
    route = src.split("async def fan_out(")[1].split("\nasync def ")[0]
    bg = src.split("async def _run_fan_out_background(")[1].split("\nasync def ")[0]
    check("6a. build: the note carries what was left out; the activity feed, the run "
          "terminal and the result say it",
          "prompt = _references_note(_ref_copied, _ref_left) + prompt" in single
          and "await _act(_references_left_out_line(_ref_left))" in single
          and '"state": "references_left_out"' in single
          and 'result["references_left_out"]' in single)
    check("6b. compare: the shared note carries it, and each variant's terminal says its own",
          "prompt=_references_note(_ref_copied, _ref_left) + req.prompt" in route
          and '"state": "references_left_out"' in route)
    check("6c. a later compare wave says it too",
          "_place_references(out[vid], project_id, references or [], None, left)" in bg
          and '"state": "references_left_out"' in bg)

    if FAILS:
        print(f"[ref-left-out] {len(FAILS)} FAILED")
        return 1
    print("[ref-left-out] all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
