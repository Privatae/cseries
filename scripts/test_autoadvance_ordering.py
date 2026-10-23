#!/usr/bin/env python3
"""Gate for the approved-plan build chain ORDER.

ORDER. Auto-advance fired from inside the phase-complete block, which put
it two steps before sync_project_repo and three before _deliver. The next build
opened about 150 ms after the previous run's row was written — while that
phase's work was still being committed and before the user had been told
anything. _run_phase_build starts with prepare_workspace(),
which fast-forwards the project directory, so the new phase's prep raced the
finishing phase's commit on the SAME directory.

Run: APP_ROOT=<repo> python3 scripts/test_autoadvance_ordering.py
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

check("the chain is split into a decide half and a dispatch half",
      "_autoadvance_pick_next" in FUNCS and "_autoadvance_start_phase" in FUNCS,
      f"{sorted(k for k in FUNCS if 'autoadvance' in k)}")
check("the old single-shot entry point is gone",
      "_autoadvance_next_phase" not in FUNCS)


# ── C5: ordering inside _run_phase_build ────────────────────────────────────
def _call_order(fn, names):
    """Line number of the first call to each name, in source order."""
    found = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            ident = getattr(f, "id", None) or getattr(f, "attr", None)
            if ident in names and ident not in found:
                found[ident] = n.lineno
    return found


rpb = FUNCS["_run_phase_build"]
order = _call_order(rpb, {
    "_factory_build_task_finish", "_autoadvance_pick_next",
    "sync_project_repo", "_deliver", "_autoadvance_start_phase",
})
for name in ("_factory_build_task_finish", "_autoadvance_pick_next",
             "sync_project_repo", "_deliver", "_autoadvance_start_phase"):
    check(f"_run_phase_build still calls {name}", name in order)

if len(order) == 5:
    check("the row is finalized before the chain is even considered",
          order["_factory_build_task_finish"] < order["_autoadvance_pick_next"])
    # The DECISION must precede _deliver: _chain_stop writes its reason onto the
    # finishing task, and that delivery is what carries it to the user.
    check("the next phase is DECIDED before the user is told (so a stop reason "
          "rides that delivery)",
          order["_autoadvance_pick_next"] < order["_deliver"],
          f"pick={order['_autoadvance_pick_next']} deliver={order['_deliver']}")
    # The regression itself.
    check("the next phase is STARTED only after the commit",
          order["sync_project_repo"] < order["_autoadvance_start_phase"],
          f"sync={order['sync_project_repo']} start={order['_autoadvance_start_phase']}")
    check("the next phase is STARTED only after the user is told",
          order["_deliver"] < order["_autoadvance_start_phase"],
          f"deliver={order['_deliver']} start={order['_autoadvance_start_phase']}")
    check("red/green — start is genuinely last of the five",
          order["_autoadvance_start_phase"] == max(order.values()))


# ── the dispatch half keeps the last-instant halt re-read ───────────────────
start_src = ast.get_source_segment(src, FUNCS["_autoadvance_start_phase"]) or ""
check("the halt flag is still re-read at the last instant before the row exists",
      "build_chain_halt" in start_src)
check("the dispatch half is fail-soft",
      "auto-advance start failed" in start_src)
check("the chain depth still increments on the started phase",
      "chain_depth=chain_depth + 1" in start_src)


# ── retry-once-then-stop fires ONLY inside a chain ─────────────────────────
# The retry exists because an autonomous chain has nobody watching it. A build
# the USER started by hand is the opposite case — they are sitting there, and
# spending a second harness run for them without asking is their cost decision.
# chain_depth is the discriminator and it is exact: it DEFAULTS to 0 and both
# user-initiated call sites take that default; only _autoadvance_start_phase
# passes chain_depth + 1. Demonstrated by the smoke
# factory-build gate, whose zero-files scenario is a hand-started build: it
# spent an unrequested run and raced the gate's own assertion.
check("the retry decision half exists", "_phase_retry_pick" in FUNCS)
if "_phase_retry_pick" in FUNCS:
    retry_src = ast.get_source_segment(src, FUNCS["_phase_retry_pick"]) or ""
    check("the retry takes chain_depth", "chain_depth" in retry_src)
    check("a hand-started build (chain_depth < 1) is NOT retried",
          "chain_depth < 1" in retry_src)
    # The attempt count reads the phase's failed rows through one helper
    # (_phase_failed_builds, shared with the automatic repairs), so the order
    # is checked at its call.
    _i_depth = retry_src.find("chain_depth < 1")
    _i_halt = retry_src.find("build_chain_halt")
    _i_count = retry_src.find("_phase_failed_builds(")
    check("...and that check comes FIRST, before the halt read and the "
          "attempt count — a hand-started build must cost no queries and "
          "write no chain-stop note",
          -1 < _i_depth < _i_halt < _i_count,
          f"depth={_i_depth} halt={_i_halt} count={_i_count}")
    _helper = (ast.get_source_segment(src, FUNCS["_phase_failed_builds"]) or ""
               if "_phase_failed_builds" in FUNCS else "")
    check("the retry budget counts FAILED builds, not all builds "
          "(a phase that built clean once then failed has failed ONCE)",
          "status='failed'" in _helper)
    check("_run_phase_build passes its own chain_depth into the decision",
          "chain_depth=chain_depth, finished_task_id=task_id" in src)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
