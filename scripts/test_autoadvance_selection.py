#!/usr/bin/env python3
"""Gate: the approved-plan build chain moves FORWARD.

Measured on one project. Selection was
`status <> 'complete' ORDER BY phase_number ASC LIMIT 1` — the lowest incomplete
phase in the whole project, ignoring where the user actually was. The user asked
to build phase 6; it succeeded; the chain then ran phase 3, then phase 4 —
backwards into leftovers of a restructure that APPENDED phases 3-11
while leaving 1-2 in place, so phase 1 and 3 are both "Scaffold & manifest" and
2 and 4 are both "Implement: Rebuild...".

Rebuilding ground already covered is worse than stopping, and picking which
stale phase to skip is a judgement the host should put to the user rather than
make silently.

Run: APP_ROOT=<repo> python3 scripts/test_autoadvance_selection.py
"""
import ast
import os
import sys
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
_TOOLS = _ROOT / "brain" / "mastercode" / "runtime" / "tools.py"

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


src = tools_source(_ROOT)
tree = ast.parse(src)
FUNCS = {n.name: n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

# ── C11: the chain moves FORWARD — now through the dependency graph ─────────
# The selection is a scheduler (_schedule_phases): a
# finishing phase starts only the phases that DEPEND on it. For a plan with no
# dependencies marked, that is the next phase in its module — the same forward
# step C11 pinned — and a phase that could build but does not follow from the
# finished one is named in the stop reason, never started. The graph itself is
# exercised functionally in scripts/test_parallel_builds.py.
pick_src = ast.get_source_segment(src, FUNCS["_autoadvance_pick_next"]) or ""
sched_src = ast.get_source_segment(src, FUNCS["_schedule_phases"]) or ""
deps_src = ast.get_source_segment(src, FUNCS["_phase_deps"]) or ""

check("the chain selects through the scheduler, bounded to what the finished "
      "phase unlocks",
      "_schedule_phases(rows, unlocked_by=finished_phase_id)" in pick_src)
check("...and the scheduler only considers phases that depend on it",
      "int(unlocked_by) not in deps" in sched_src)
check("an unmarked phase waits for the one before it in its module",
      "r[\"phase_number\"] < ph[\"phase_number\"]" in deps_src
      and "_row_get(r, \"module_id\") == _row_get(ph, \"module_id\")" in deps_src)
check("reaching the end with phases that could build but do not follow STOPS "
      "and says so", "_chain_stop" in pick_src and "not follow" in pick_src)
check("...and the message hands the choice to the user in the question box rather than silently rewind",
      "whether to build or drop them is the user's" in pick_src and "question box (escalate" in pick_src)
check("red/green — the unbounded 'lowest incomplete phase' pick is gone",
      "ORDER BY phase_number ASC LIMIT 1" not in pick_src)

# ── v124: gates are columns, the build's outcome is whether it landed ─────
# project 1 (2026-09-03): phase 3's brief said "approved by the
# user BEFORE this phase builds" and the chain built it; phase 2's "verify
# backtesting runs end-to-end" was a script the harness wrote and nobody ran.
held_src = ast.get_source_segment(src, FUNCS["_held_text"]) or ""
check("the chain stops IN FRONT of a gated, unapproved phase and says so",
      "_phase_awaits_approval(ph)" in sched_src
      and "waiting for the user's approval" in held_src)
check("the chain's SELECT reads the gate columns it decides on",
      "gate, approved_at" in src[src.index("_PHASE_COLS = ("):src.index("_PHASE_COLS = (") + 300]
      and "_PHASE_COLS" in pick_src)

run_src = ast.get_source_segment(src, FUNCS["_run_phase_build"]) or ""
# A build's outcome is whether it LANDED its files: the
# harness failing is 'failed', landing is 'complete'. The listed checks still
# run and are reported, and never decide the status; decide() only yields the
# per-check summaries. (scripts/test_pipeline.py pins the decide() table.)
check("the phase's status is landed -> complete, harness failed -> failed",
      "phase_final = _PHASE_DONE if ok else _PHASE_FAILED" in run_src
      and "_set_phase_status(pool, phase_id, phase_final)" in run_src
      and 'phase_final = _decision["status"]' not in run_src
      and "_PHASE_REVIEW" not in run_src)
check("auto-advance is decided on a COMPLETE phase",
      "if proven:" in run_src and "proven = phase_final == _PHASE_DONE" in run_src
      and "_autoadvance_pick_next(" in run_src)
check("a failed phase records why the chain ended (the seeded retry, or the stop)",
      "_phase_retry_pick(" in run_src and "elif phase_final == _PHASE_FAILED" in run_src)
check("the project completes when no phase is open — no 'review' count holds it",
      "_PHASE_REVIEW_COUNT_SQL" not in run_src and "in_review" not in run_src
      and "if not remaining:" in run_src and "SET status='complete'" in run_src)
check("the declared acceptance is still handed to the pool run, which never fails the build on it",
      "acceptance=acceptance_cmds" in run_src and "acceptance_fails_build" not in run_src)

mod_src = src
check("a legacy 'review' row is not OPEN work (never re-picked, never rewound into)",
      "_PHASE_OPEN_SQL = (f\"status NOT IN ('{_PHASE_DONE}','{_PHASE_ARCHIVED}',\"" in mod_src
      and "f\"'{_PHASE_REVIEW}')\")" in mod_src)

# review.py: the checks run on the POD, in the run's worktree, before release.
_REVIEW = _ROOT / "server" / "routes" / "review.py"
rsrc = _REVIEW.read_text()
rtree = ast.parse(rsrc)
RFUNCS = {n.name: n for n in ast.walk(rtree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
disp_src = ast.get_source_segment(rsrc, RFUNCS["_dispatch_to_pod_harness"]) or ""
# The container worktree is the SESSION-scoped directory the run executed in
# (_pod_dir_name); this check used to pin the variant-only path, which after
# that change was a directory that did not exist — every phase with declared
# checks lost its verdict to "project_dir missing". The release is the
# holder-count one, still after acceptance.
check("acceptance runs after the harness, before release, in the container worktree",
      "_run_acceptance(" in disp_src
      and 'in_container_dir = f"/var/lib/cseries/projects/{_pod_dir_name(session_dir, worktree)}"' in disp_src
      and disp_src.index("_run_acceptance(") < disp_src.index("_release_harness_when_last("))
acc_src = ast.get_source_segment(rsrc, RFUNCS["_run_acceptance"]) or ""
check("an older harness image (404 on /check) is UNPROVEN, never a pass",
      "r.status == 404" in acc_src and '"ran": False' in acc_src
      and "harness image predates /check" in acc_src)
check("a pass requires EVERY check ok",
      'all(bool(x.get("ok")) for x in results)' in acc_src)


print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
