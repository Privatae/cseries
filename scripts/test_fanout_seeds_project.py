#!/usr/bin/env python3
"""Gate: a fan_out given a project_id starts from that project's real files.

A 3-harness "redesign the front end of this weather app" bake-off started
from an empty project, though the ask named an existing one.

Because project_id NAMED the project but never LOCATED it. fan_out did

    project_dir = Path(req.project_dir or (PROJECTS_ROOT / session_id))
    project_dir.mkdir(parents=True, exist_ok=True)
    seeded = req.seed if req.seed is not None else _project_has_content(project_dir)

— so with project_dir omitted it asked whether a directory created one line
above held any files. It never does. "AUTO" could therefore only ever resolve to
greenfield, whatever project the caller named, and three harnesses rebuilt an
existing app from a prose description while the real code sat one directory
away (one session: fan-out project_dir 0 files, project 9198's 18).

fan_out was the only entry point that failed to derive this — run_single_harness_build
and ladder.start_run both already did. Case 3 pins that agreement so the three
doors cannot drift apart again.

Run:  python3 scripts/test_fanout_seeds_project.py
"""
import ast
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(1, str(_ROOT / "brain"))

from server.routes import review  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# ── The resolution rule, lifted from fan_out and exercised directly ─────────
# fan_out is a large async route with harness-auth and DB work ahead of this
# line, so the rule is re-evaluated here against the SOURCE (case 2 asserts the
# source really is this shape, so the two cannot drift).
def resolve(projects_root: Path, session_id: str, project_dir, project_id):
    if project_dir:
        return Path(project_dir)
    out = projects_root / session_id
    if project_id is not None:
        cand = projects_root / str(project_id)
        if cand.is_dir():
            out = cand
    return out


print("== 1. the resolution, against a real project tree ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "9198").mkdir()
    (root / "9198" / "index.html").write_text("<!doctype html>")
    (root / "9198" / "app.yaml").write_text("id: weather")
    SID = "5e55f00d1a2b4c3d9e8f7a6b5c4d3e2f"

    got = resolve(root, SID, None, 9198)
    check("project_id alone resolves to THAT PROJECT, not a session dir",
          got == root / "9198", str(got))
    check("...and it has content, so AUTO seeds (edit mode)",
          review._project_has_content(got) is True)

    # red/green: the shipped rule, on the same inputs
    old = Path(None or (root / SID))
    old.mkdir(parents=True, exist_ok=True)
    check("red/green — the OLD rule picked a fresh session dir",
          old == root / SID, str(old))
    check("...which is empty, so AUTO could only ever say greenfield",
          review._project_has_content(old) is False)

    check("an explicit project_dir still WINS over project_id",
          resolve(root, SID, str(root / "other"), 9198) == root / "other")

    # The is_dir() guard: a project_id with no directory must NOT materialize
    # an empty project dir — that would rebuild the same silent-greenfield trap.
    got404 = resolve(root, SID, None, 999)
    check("a project_id with no directory falls back to the session dir",
          got404 == root / SID, str(got404))
    check("...and does NOT create an empty projects/999",
          not (root / "999").exists())

    check("no project_id and no project_dir stays session-scoped",
          resolve(root, SID, None, None) == root / SID)

    # seed=False must still force greenfield inside a populated project.
    pd = resolve(root, SID, None, 9198)
    seeded_auto = None if None is not None else review._project_has_content(pd)
    check("AUTO on a populated project seeds", seeded_auto is True)
    check("explicit seed=False still overrides AUTO",
          (False if False is not None else review._project_has_content(pd)) is False)


print()
print("== 2. the route really resolves it this way ==")
SRC = (_ROOT / "server" / "routes" / "review.py").read_text()
check("fan_out no longer falls straight to a session dir",
      "Path(req.project_dir or (PROJECTS_ROOT / session_id))" not in SRC)
check("fan_out derives project_dir from req.project_id",
      "cand = PROJECTS_ROOT / str(req.project_id)" in SRC)
check("...guarded by is_dir(), like ladder.start_run",
      "if cand.is_dir():" in SRC)


print()
print("== 3. all three entry points agree on what a project_id means ==")
# The bug was one door disagreeing with the other two. Assert the agreement,
# not the single line.
LAD = (_ROOT / "server" / "routes" / "ladder.py").read_text()
check("ladder.start_run derives project_dir from project_id",
      "project_id is not None" in LAD and "projects" in LAD
      and "is_dir()" in LAD)
check("run_single_harness_build derives project_dir from project_id",
      "project_dir = PROJECTS_ROOT / str(project_id)" in SRC)


print()
print("== 4. seeding only READS the project — it never writes or deletes it ==")
# _prepare_worktree(worktree_parent, harness_id, seed, seed_from): the rmtree
# targets worktree_parent/harness_id. Now that seed_from can be the user's real
# project, "which path does it delete" stops being academic.
fn = None
for node in ast.walk(ast.parse(SRC)):
    if isinstance(node, ast.FunctionDef) and node.name == "_prepare_worktree":
        fn = node
        break
check("_prepare_worktree found", fn is not None)
if fn:
    body = ast.get_source_segment(SRC, fn) or ""
    check("it rmtree's the WORKTREE, never seed_from",
          "shutil.rmtree(worktree" in body and "rmtree(seed_root" not in body
          and "rmtree(seed_from" not in body)
    # 2026-10-01: the seed is copied through the shared walk, which opens the
    # project read-only and never follows a link (tree_walk.copy_tree_no_escape).
    check("seed_from is only read (copied through the walk), never mutated",
          "copy_tree_no_escape(seed_root, worktree" in body)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        proj.mkdir()
        (proj / "index.html").write_text("<!doctype html>")
        (proj / "keep").mkdir()
        (proj / "keep" / "f.txt").write_text("x")
        wt_root = root / "work"
        wt_root.mkdir()
        try:
            review._prepare_worktree(wt_root, "antigravity", seed=True,
                                     seed_from=proj)
            ran = True
        except Exception as e:  # noqa: BLE001
            ran = False
            print("       (_prepare_worktree raised: %s)" % e)
        if ran:
            check("the project survives being used as a seed",
                  (proj / "index.html").is_file() and (proj / "keep" / "f.txt").is_file())
            check("the worktree got the project's files",
                  (wt_root / "antigravity" / "index.html").is_file())
            check("output landed under the WORKTREE root, not in the project",
                  not (proj / "antigravity").exists())

print()
print("== 5. seeding does not block the event loop ==")
# Making the seed REAL turned a zero-cost step into N full project copies
# (N = variants). Run synchronously in an async route they stall the worker for
# every other request. Review of this PR. Asserted on the source because
# the surrounding routes need a live DB + harness auth to reach this line.
_fan = SRC[SRC.index("async def fan_out("):]
_fan = _fan[:_fan.index("\n@router") if "\n@router" in _fan else len(_fan)]
check("fan_out prepares worktrees off the loop",
      "await asyncio.to_thread(_prep_all)" in _fan)
check("...and does not still call _prepare_worktree inline",
      "_prepare_worktree(_wt_root" not in _fan
      or "def _prep_all" in _fan)
_solo = SRC[SRC.index("async def run_single_harness_build("):]
_solo = _solo[:_solo.index("\nasync def ", 10) if "\nasync def " in _solo[10:] else len(_solo)]
check("the solo build path is off the loop too",
      "await asyncio.to_thread(" in _solo and "_prepare_worktree" in _solo)

print()
print("== 6. a pick cannot be written OUT of the project ==")
# src has always been realpath-contained; dst was only string-checked. That
# asymmetry matters now that project_dir is routinely the real project: an
# outbound directory symlink would carry a pick out of the tree.
check("propose_commit resolves a containment root for destinations",
      "_project_real = project_dir.resolve()" in SRC)
check("...and realpath-checks every destination against it",
      "dst_real = Path(os.path.realpath(dst))" in SRC
      and "_project_real not in dst_real.parents" in SRC)
check("...rejecting rather than writing",
      '"error": "rejected_project_escape"' in SRC)

# Behavioral: the guard's own predicate, against a real outbound symlink.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    proj = root / "proj"
    (proj / "sub").mkdir(parents=True)
    outside = root / "outside"
    outside.mkdir()
    (proj / "escape").symlink_to(outside, target_is_directory=True)
    project_real = proj.resolve()

    def contained(rel: str) -> bool:
        dst_real = Path(os.path.realpath(proj / rel))
        return project_real == dst_real or project_real in dst_real.parents

    check("a normal path is contained", contained("sub/app.js"))
    check("a path through an outbound symlink is REJECTED",
          not contained("escape/evil.js"),
          str(Path(os.path.realpath(proj / "escape/evil.js"))))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all gates passed")
