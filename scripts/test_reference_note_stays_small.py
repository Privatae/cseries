#!/usr/bin/env python3
"""A long list of reference files never stops a run from starting.

A module run's brief gains a note naming every project file it was given and
every one it was not (with why). The prompt reaches the CLI as ONE argv
string — a NUL or 128 KiB stops the run — and the ladder leaves 8 KiB for
this note. With a large list the note itself could stop the run. Past 4 KiB
the note now carries the counts and the path of an index file written into
the worktree beside the copies; the index holds the whole list.

Run: APP_ROOT=<repo> python3 scripts/test_reference_note_stays_small.py
"""
import os
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[ref-note] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    repo = Path(os.environ.get("APP_ROOT", "/app"))
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "brain"))
    import server.routes.review as review

    print("== a short list rides inline, whole ==")
    note = review._references_note(["docs/a.md"], [("docs/b.md", "it is a link")])
    check("both files are named in the note", "docs/a.md" in note and "docs/b.md" in note, note)

    print("== a long list never makes the brief too long to start ==")
    left = [(f"data/part-{i:05d}.csv", "it is a link, or no longer a file in the project")
            for i in range(3000)]
    copied = [f"docs/chapter-{i:04d}.md" for i in range(800)]
    note = review._references_note(copied, left)
    size = len(note.encode())
    check(f"the note stays within the ladder's 8 KiB for it ({size} bytes)", size <= 8192,
          str(size))
    check("...says how many were given and how many were not",
          "800" in note and "3000" in note, note[:300])
    idx = getattr(review, "_REF_INDEX", None)
    check("...and names the index file that holds the whole list",
          bool(idx) and f"{review._REF_DIR}/{idx}" in note, note[:400])

    print("== the index is written into the worktree, whole ==")
    tmp = Path(tempfile.mkdtemp(prefix="refnote-"))
    projects = tmp / "projects"
    (projects / "7" / "docs").mkdir(parents=True)
    (projects / "7" / "docs" / "brief.md").write_text("the brief\n")
    wt = tmp / "wt"
    (wt / ".git" / "info").mkdir(parents=True)
    saved = review.PROJECTS_ROOT
    review.PROJECTS_ROOT = projects
    try:
        rels = ["docs/brief.md"] + [f"gone/f{i}.txt" for i in range(500)]
        out_left: list = []
        got = review._place_references(wt, 7, rels, None, out_left)
    finally:
        review.PROJECTS_ROOT = saved
    check("the existing file is copied, the 500 missing ones are left out",
          got == ["docs/brief.md"] and len(out_left) == 500, (got, len(out_left)))
    ip = wt / review._REF_DIR / (idx or "_none_")
    text = ip.read_text() if ip.exists() else ""
    check("the index lists every copied and every left-out file, with why",
          "docs/brief.md" in text and all(f"gone/f{i}.txt" in text for i in range(500))
          and "no longer a file" in text, str(ip))
    exclude = (wt / ".git" / "info" / "exclude").read_text() \
        if (wt / ".git" / "info" / "exclude").exists() else ""
    check("...and it sits in the reference folder git never sees",
          f"/{review._REF_DIR}/" in exclude, exclude)

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
