#!/usr/bin/env python3
"""Gate for the system-state-snapshot guard in memory extraction.

Facts like "claude-code: 12 runs, 0 successful" / "key not set up" showed up
in every response and were wrong — status snapshots of
the platform's own machinery stored as durable facts, re-read as current weeks
later. memory_engine.is_system_state_snapshot is the deterministic guard the
extractor applies to fact/topic/unresolved candidates (decisions, preferences
and goals ABOUT a system still pass).

Run from the repo root:  PYTHONPATH=.:brain python3 scripts/test_memory_system_state_filter.py
"""
import os
import sys
from pathlib import Path

_root = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, "brain"))

from mastercode.memory_engine import is_system_state_snapshot  # noqa: E402

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS += 1


print("== snapshots that must be DROPPED (status of the machinery, stored as if durable) ==")
DROP = [
    "claude-code harness on the (default) model row has 12 runs, 0 successful, 12 failed, averaging 140s before dying; last run was 2026-01-05 09:30 UTC",
    "Harness failures are caused by operator's /harness/wake endpoint returning 504 with 'harness-claude-code not Ready within 60s' — a pod readiness timeout issue, not an auth, model, or task problem",
    "On this bench, claude-code with no model pinned fails 100% (12/12, harness wake 504). Pinned to a named model it is 40/40. Always pin the model when dispatching a build to claude-code.",
    "harness_connections snapshot: claude-code (Anthropic, first_party, key not_set_up, action: 'Connect Claude Code' login)",
    "The kimi harness is connected and ready; codex is not set up",
    "All orchestrator roles are now set to claude-sdk / claude-opus-5",
    "The OpenRouter key is not configured on this host",
    "C-Series is at version v1.2.3; the operator defaults to the kimi provider",
    "The embeddings service timed out; the bridge returned a 502",
]
for t in DROP:
    check(f"drop: {t[:70]}…", is_system_state_snapshot(t) is True)

print("== memories that must be KEPT (the user, their life, their decisions) ==")
KEEP = [
    "User's cousin is visiting next week",
    "User prefers tabular numerals in finance dashboards",
    "User wants most recent projects at the top of the Workbench rail",
    "User decided to build the finance app on the claude-code harness rather than kimi",
    "User is frustrated when the host restates the harness's report after a build",
    "User is testing the system's recall and embeddings capabilities; recall works, embeddings do not",
    "User asked for a full fresh run on Claude Code with no fan-out",
    "The garden planner (project 9207) is a planting calendar, not a shop",
    "User's train arrives at 7:15 and they want the weather app to show Springfield by default",
    "",
]
for t in KEEP:
    check(f"keep: {t[:70] or '(empty)'}", is_system_state_snapshot(t) is False)

print()
print("FAIL" if FAILS else "PASS", f"({FAILS} failing checks)")
sys.exit(1 if FAILS else 0)
