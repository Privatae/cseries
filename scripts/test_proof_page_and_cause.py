#!/usr/bin/env python3
"""The host can see WHY its app restarted (entity 9128).

The pod was replaced at 14:05 by a repair build the proof loop opened
(factory_build task 74, after app_verify task 73 failed). Asked "why did it
restart 33 minutes ago", the host had `last_exit` — which says the process
ended, never that the PLATFORM ended it. entity_tasks knew, and no tool
showed it. app_status now carries `platform_actions`: the last five builds,
ships and proofs that touched this app, newest first, with what the platform
decided.

WHAT IS DELIBERATELY NOT HERE. This file used to also gate a "does the page
load" check in app_verify: fetch the entrypoint, regex out its first script,
resolve it like a browser, judge the response. Three false passes turned up,
each one layer deeper — the kernel's JSON 404 at the
root, the ?v= buster on classic scripts, and finally the SDK tag the kernel
injects FIRST in <head>, which loads fine and hides everything after it. A
simulator of the thing that defines correctness cannot converge; the browser's
behaviour is the spec. The right structure is to load the built app in the
real browser sidecar and read what a browser reads. That is a separate build,
and it is not a regex.

Run: APP_ROOT=<repo> python3 scripts/test_proof_page_and_cause.py
"""
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[cause] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


tools_src = (_ROOT / "brain/mastercode/runtime/tools.py").read_text()
verify_src = (_ROOT / "brain/mastercode/runtime/app_verify.py").read_text()

# ── 0. the simulator is GONE ───────────────────────────────────────────────
check("no page-load regex simulator lives in app_verify",
      "_page_loads" not in verify_src and "_ASSET_REF" not in verify_src)

# ── 1. the query keys on where each writer actually records the app ───────
# The first cut matched result.origin.app_id, which on a factory_build row is
# the calling SURFACE ("workbench") — so it returned no builds at all, and the
# commit's own motivating row (the repair build that replaced the pod) was the
# one it could not return. A string-match test is what let that through.
_sql = re.search(r"SELECT task_id, task_type.*?LIMIT 5", tools_src, re.S)
_pred = _sql.group(0) if _sql else ""
check("app_status reports the platform actions that touched this app",
      '"platform_actions"' in tools_src)
for pred in ("result->>'app_id' = $2",
             "result->'auto_ship'->>'app_id' = $2",
             "result->'origin'->'repair'->>'app_id' = $2"):
    check(f"the query matches on {pred}", pred in _pred, _pred[:200])
check("...and no longer on origin.app_id, which is the calling SURFACE",
      "result->'origin'->>'app_id'" not in _pred)
check("...newest first and capped", "ORDER BY task_id DESC LIMIT 5" in _pred)
check("...carrying what the platform DECIDED, not just that something ran",
      "result->>'summary' AS summary" in _pred)
check("...and a failed read never breaks a status call",
      'result["platform_actions"] = []' in tools_src)


# ── 2. EXECUTED against the row shapes the three writers produce ──────────
def _matches(result: dict, app_id: str) -> bool:
    """The query's WHERE, evaluated the way postgres would on a row."""
    def _g(*path):
        cur = result
        for k in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur
    return any([
        _g("app_id") == app_id,
        _g("auto_ship", "app_id") == app_id,
        _g("origin", "repair", "app_id") == app_id,
    ])


_PROOF_ROW = {"app_id": "trading-bot", "reason": "ship"}
_SHIP_BUILD_ROW = {"project_id": 1, "harness": "kimi",
                   "origin": {"app_id": "workbench", "project_id": 1},
                   "auto_ship": {"shipped": True, "app_id": "trading-bot"}}
_REPAIR_BUILD_ROW = {"project_id": 1,
                     "origin": {"app_id": "workbench",
                                "repair": {"app_id": "trading-bot",
                                           "round": 1}}}
_OTHER_APP_ROW = {"app_id": "some-other-app"}
check("EXECUTED: a proof row matches", _matches(_PROOF_ROW, "trading-bot"))
check("EXECUTED: the plan-end SHIP build matches",
      _matches(_SHIP_BUILD_ROW, "trading-bot"))
check("EXECUTED: the REPAIR build that replaced the pod matches",
      _matches(_REPAIR_BUILD_ROW, "trading-bot"))
check("EXECUTED: another app's proof does not match",
      not _matches(_OTHER_APP_ROW, "trading-bot"))
check("EXECUTED: app_status('workbench') does not sweep every build",
      not _matches(_SHIP_BUILD_ROW, "workbench")
      and not _matches(_REPAIR_BUILD_ROW, "workbench"))

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[cause] ALL OK")
