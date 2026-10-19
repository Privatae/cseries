#!/usr/bin/env python3
"""/launch's withheld token meets the retired scopes.

/launch builds its withheld isolated-app token object by hand (not through
tokens.mint), so it must drop retired permissions the same way mint does.

(The CEIGAS audit-attribution half of this gate went with the audit row it
attributed, 2026-10-17: the HTTP gate now checks only the kill switch.)

Source read; no pod, no DB, no network.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.applications.scopes import RETIRED_PERMISSIONS   # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


# /launch's withheld token object filters retired permissions like mint.
src = open(os.path.join(ROOT, "server/routes/applications.py")).read()
block = src[src.index("if withhold:"):src.index("else:", src.index("if withhold:"))]
check("the withheld token object filters RETIRED_PERMISSIONS",
      "RETIRED_PERMISSIONS" in block and re.search(r"not in RETIRED_PERMISSIONS", block) is not None, block[:300])
check("RETIRED_PERMISSIONS still holds the two retired scopes",
      {"write:incident_decision", "write:policy_proposals"} <= set(RETIRED_PERMISSIONS))

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("PASS — /launch never reports a retired scope")
