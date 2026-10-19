#!/usr/bin/env python3
"""Invariant: host-parity ratchet (Phase 1 of software-for-the-AI, 2026-08-11).

docs/capabilities.json carries the generated host-parity ledger: how much of
the app-facing route surface the host can reach without a human clicking.
docs/parity-baseline.json is the committed floor. This gate enforces the
ratchet: the two gap numbers — routes in UNREACHABLE areas and routes in
UNREVIEWED areas — may only go DOWN, ship over ship.

Improving the numbers requires lowering the baseline in the same change
(deliberate, reviewed); regressing them fails the ship. A missing baseline
fails too — a ratchet that skips when its floor is absent is not a ratchet.

Run in-pod: docker exec <c> python3 /app/scripts/check_host_parity.py
"""
import json
import os
import sys

DOCS = os.environ.get("CSERIES_DOCS", "/app/docs")

try:
    caps = json.load(open(os.path.join(DOCS, "capabilities.json")))
except Exception as e:
    print(f"FAIL: cannot read capabilities.json: {e}")
    sys.exit(1)
try:
    base = json.load(open(os.path.join(DOCS, "parity-baseline.json")))
except Exception as e:
    print(f"FAIL: cannot read parity-baseline.json (the ratchet floor must ship): {e}")
    sys.exit(1)

counts = (caps.get("host_parity") or {}).get("counts") or {}
fails = []
improved = []
for key in ("routes_unreachable", "routes_unreviewed"):
    now = counts.get(key)
    floor = base.get(key)
    if now is None or floor is None:
        fails.append(f"{key} missing (ledger={now!r}, baseline={floor!r})")
        continue
    if now > floor:
        fails.append(f"{key} regressed: {now} > baseline {floor}")
    elif now < floor:
        improved.append(f"{key}: {now} < baseline {floor}")

if fails:
    for f in fails:
        print(f"FAIL: {f}")
    print("      fix the regression, or (if a NEW route area is genuinely "
          "host-covered) update _AREA_HOST_TOOLS in generate_capabilities.py")
    sys.exit(1)

for i in improved:
    print(f"NOTE: improved — {i}. Lower docs/parity-baseline.json in the next change.")
print(f"OK: parity ratchet holds (unreachable={counts.get('routes_unreachable')}, "
      f"unreviewed={counts.get('routes_unreviewed')})")
