#!/usr/bin/env python3
"""Gate: a cold open of a harness-backed app performs ONE wake, not one per request.

Origin — measured on one Host 2026-08-21, the first live open of the (since
removed) VS Code editor. An app's page loads its assets in parallel and every
one is proxied through `ensure_awake`. A cold wake takes tens of seconds, so each
of those requests saw "not awake" and fired its own harness.wake. The operator's
access log recorded FOUR concurrent `POST /harness/wake` for a single page load.

Two invariants, both asserted below:
  1. A concurrent cold open performs exactly ONE wake, and every caller gets the
     same upstream.
  2. A failed wake releases the lock, so the next open can still work.

Run from the repo root:  python3 scripts/test_harness_wake_single_flight.py
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("APP_ROOT") or str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS += 1


LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

from server.applications import harness_backend as hb  # noqa: E402

# Any harness-backed app id that is not the Browser: the Browser's profile
# restore rides its cold wake and is not what this gate measures.
APP = "harness-app"
HARNESS = "browser"


def _reset():
    hb._state.pop(APP, None)
    hb._wake_locks.pop(APP, None)


run = LOOP.run_until_complete

# ── 1. the cold open ────────────────────────────────────────────────────────
print("== 1. a concurrent cold open (the entity-9003 failure) ==")


async def _cold_open():
    _reset()
    wakes = []

    async def fake_wake(harness_id, creds=None):
        wakes.append(creds)
        await asyncio.sleep(0.20)          # a real cold wake is tens of seconds
        return {"ok": True, "url": f"http://harness-{harness_id}.host-ns.svc.cluster.local:8830"}

    hb._wake = fake_wake
    # 12 proxied asset requests land while the wake is still in flight.
    results = await asyncio.gather(*[hb.ensure_awake(APP, HARNESS) for _ in range(12)])
    check("12 concurrent requests → exactly ONE harness.wake", len(wakes) == 1, f"{len(wakes)} wakes")
    check("every caller got the same upstream", len(set(results)) == 1 and bool(results[0]), str(set(results)))


run(_cold_open())


# ── 2. a failed wake must not leave the lock held ───────────────────────────
print("== 2. a failed wake releases the lock (the next open can still work) ==")


async def _failed_wake():
    _reset()
    calls = []

    async def bad_wake(harness_id, creds=None):
        calls.append(1)
        if len(calls) == 1:
            return {"ok": False, "error": "bridge not connected"}
        return {"ok": True, "url": f"http://harness-{harness_id}.host-ns.svc.cluster.local:8830"}

    hb._wake = bad_wake
    first = await hb.ensure_awake(APP, HARNESS)
    check("a failed wake returns None", first is None)
    check("the lock is not held afterwards", not hb._wake_locks[APP].locked())
    second = await hb.ensure_awake(APP, HARNESS)
    check("the next open succeeds", bool(second))


run(_failed_wake())

print()
print("ALL PASS" if FAILS == 0 else f"{FAILS} FAILURE(S)")
sys.exit(1 if FAILS else 0)
