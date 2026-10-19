#!/usr/bin/env python3
"""Gate: the land's three-way merge has no size bound of its own.

A file both the run and the project changed is merged with `git merge-file`
whatever its size: the three sides are files on disk, git merges them in its
own process, and the Host streams the result into the project. Before this, a
side over 8 MiB (or a land whose merges passed 64 MiB) was a
`too_large_to_merge` conflict, and any conflict makes the whole land write
nothing. Driven on real seeded worktrees made by the real _prepare_worktree,
the same way scripts/test_parallel_builds.py drives the land.

Run: APP_ROOT=<repo> python3 scripts/test_land_merges_large_files_on_disk.py
"""
import gc
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILS: list = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


from server.routes import review as rv  # noqa: E402

MiB = 1024 * 1024


def _hwm_kib() -> int:
    for line in open("/proc/self/status"):
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return 0


def _reset_hwm() -> None:
    gc.collect()
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")


def _read(p: Path):
    return p.read_text() if p.is_file() else None


def _big_text(n_lines: int) -> bytes:
    return b"".join(b"line %07d of a large data file\n" % i for i in range(n_lines))


# About 12 MiB: over the old 8 MiB per side, and with the other big file over
# the old 64 MiB per land only when both are counted (see section 2).
N_LINES = 400_000

print("== 1. a both-changed text file over 8 MiB merges, and the rest of the land is written ==")
with tempfile.TemporaryDirectory() as td:
    saved_root, saved_tmp = rv.PROJECTS_ROOT, tempfile.tempdir
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        proj = rv.PROJECTS_ROOT / "5"
        (proj / "data").mkdir(parents=True)
        seed = _big_text(N_LINES)
        (proj / "data" / "big.csv").write_bytes(seed)
        (proj / "small.txt").write_text("a\nb\nc\n")
        wt = rv._prepare_worktree(Path(td) / "sessions" / "a", "codex", seed=True, seed_from=proj)
        check("the seed is a commit the land can read",
              "data/big.csv" in (rv._worktree_base_blobs(wt) or {}))
        # The run edits the LAST line and adds a file; the project (a parallel
        # phase) edits the FIRST line after the run was seeded.
        run_side = seed.replace(b"line %07d of" % (N_LINES - 1), b"RUN EDIT %07d of" % (N_LINES - 1))
        (wt / "data" / "big.csv").write_bytes(run_side)
        (wt / "new.txt").write_text("from the run\n")
        (wt / "small.txt").write_text("a\nb\nc\nd\n")
        proj_side = seed.replace(b"line 0000000 of", b"PROJECT EDIT 0000000 of", 1)
        (proj / "data" / "big.csv").write_bytes(proj_side)
        check("the both-changed file is over the old 8 MiB per-side bound",
              len(run_side) > 8 * MiB and len(proj_side) > 8 * MiB, (len(run_side), len(proj_side)))
        want = proj_side.replace(b"line %07d of" % (N_LINES - 1), b"RUN EDIT %07d of" % (N_LINES - 1))
        del seed, run_side, proj_side

        stage_parent = Path(td) / "tmp"
        stage_parent.mkdir()
        tempfile.tempdir = str(stage_parent)
        ch, sk, conf = [], [], []
        _reset_hwm()
        hwm0 = _hwm_kib()
        landed = rv._land_worktree_in_project(5, wt, ch, sk, None, conf, {"phase_id": 1})
        grew_mib = (_hwm_kib() - hwm0) / 1024
        tempfile.tempdir = saved_tmp
        check("1a. no conflict: the large both-changed file is merged, not refused",
              conf == [], conf)
        got = (proj / "data" / "big.csv").read_bytes() if (proj / "data" / "big.csv").is_file() else b""
        check("1b. the project file holds BOTH edits (the run's last line, the project's first)",
              got == want, (len(got), len(want), got[:40], got[-40:]))
        del got
        check("1c. the other files of the same land are written",
              _read(proj / "new.txt") == "from the run\n"
              and _read(proj / "small.txt") == "a\nb\nc\nd\n", (ch, landed))
        check("1d. the land reports every file it changed",
              sorted(ch) == ["data/big.csv", "new.txt", "small.txt"], ch)
        check("1e. the Host process never held the file: peak RSS grew under 8 MiB "
              "for a 12 MiB merge", grew_mib < 8, f"{grew_mib:.1f} MiB")
        check("1f. the merge's temp files are gone after the land",
              list(stage_parent.iterdir()) == [], list(stage_parent.iterdir()))
    finally:
        rv.PROJECTS_ROOT, tempfile.tempdir = saved_root, saved_tmp

print("== 2. several large merges in one land: no per-land total ==")
with tempfile.TemporaryDirectory() as td:
    saved_root = rv.PROJECTS_ROOT
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        proj = rv.PROJECTS_ROOT / "6"
        proj.mkdir(parents=True)
        seed = _big_text(N_LINES)
        names = [f"part{i}.csv" for i in range(6)]       # 6 x ~12 MiB, past the old 64 MiB
        for n in names:
            (proj / n).write_bytes(seed)
        wt = rv._prepare_worktree(Path(td) / "sessions" / "b", "codex", seed=True, seed_from=proj)
        for n in names:
            (wt / n).write_bytes(seed + b"run tail\n")
            (proj / n).write_bytes(b"project head\n" + seed)
        del seed
        conf = []
        rv._land_worktree_in_project(6, wt, [], [], None, conf, {"phase_id": 2})
        check("2a. six both-changed ~12 MiB files (over the old 64 MiB per land) all merge",
              conf == [] and all((proj / n).read_bytes().startswith(b"project head\n")
                                 and (proj / n).read_bytes().endswith(b"run tail\n") for n in names),
              conf)
    finally:
        rv.PROJECTS_ROOT = saved_root

print("== 3. genuine conflicts are unchanged; a merge git cannot run says why ==")
with tempfile.TemporaryDirectory() as td:
    saved_root = rv.PROJECTS_ROOT
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        proj = rv.PROJECTS_ROOT / "7"
        proj.mkdir(parents=True)
        seed = _big_text(N_LINES)
        (proj / "big.csv").write_bytes(seed)
        (proj / "bin.dat").write_bytes(b"a\0b")
        wt = rv._prepare_worktree(Path(td) / "sessions" / "c", "codex", seed=True, seed_from=proj)
        (wt / "big.csv").write_bytes(seed.replace(b"line 0000005 of", b"RUN 0000005 of"))
        (proj / "big.csv").write_bytes(seed.replace(b"line 0000005 of", b"PROJ 0000005 of"))
        (wt / "bin.dat").write_bytes(b"a\0run")
        (proj / "bin.dat").write_bytes(b"a\0proj")
        (wt / "other.txt").write_text("x\n")
        before = (proj / "big.csv").read_bytes()
        del seed
        conf, ch = [], []
        landed = rv._land_worktree_in_project(7, wt, ch, [], None, conf, {"phase_id": 3})
        kinds = {c["path"]: c["kind"] for c in conf}
        check("3a. overlapping edits to a large file are a both_changed conflict",
              kinds.get("big.csv") == "both_changed", conf)
        check("3b. a binary file changed on both sides is still a conflict",
              kinds.get("bin.dat") == "both_changed", conf)
        check("3c. ...and a conflicting land still writes nothing",
              landed == [] and ch == [] and not (proj / "other.txt").exists()
              and (proj / "big.csv").read_bytes() == before, (landed, ch))
        # A base blob git cannot read: not 'both changed the same lines'.
        (proj / "big.csv").write_bytes(b"x\n")
        (wt / "big.csv").write_bytes(b"y\n")
        try:
            w, m, c = rv._integrate_with_project(7, wt, ["big.csv"], {"big.csv": "f" * 40},
                                                 None, Path(td))
        except TypeError as e:          # a tree whose integrate takes no stage dir
            c = [{"error": repr(e)}]
        check("3d. a merge that cannot run is kind merge_failed with a reason, not both_changed",
              len(c) == 1 and c[0].get("kind") == "merge_failed" and c[0].get("reason"), c)
    finally:
        rv.PROJECTS_ROOT = saved_root

print("== 4. source pins ==")
src = (ROOT / "server/routes/review.py").read_text()
check("4a. no merge size bound remains (_MERGE_MAX_BYTES / _MERGE_TOTAL_MAX / too_large_to_merge)",
      "_MERGE_MAX_BYTES" not in src and "_MERGE_TOTAL_MAX" not in src
      and '"too_large_to_merge"' not in src)
check("4b. the merge writes git's output to a file, never capture_output",
      'stdout=fo, stderr=fe' in src and "def _merge3(ours: Path" in src)

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("ok: all checks passed")
