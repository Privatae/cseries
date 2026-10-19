#!/usr/bin/env python3
"""The Host Manager tile publisher retries until a publish LANDS.

After the grid shipped, the desktop snapshot did not appear on it.
`host_desktop_snapshots` was empty because nothing wrote it. The publisher that
fixes that has one non-obvious failure mode, and this file pins it.

THE BUG THIS EXISTS FOR (found in review, before it shipped). The boot
task is scheduled from the lifespan (`server/main.py`) well BEFORE the
BridgeClient is constructed and started, so the first attempt always finds
`get_bridge_client()` is None and fails. The first draft flipped its "first
run" flag whether or not the publish had landed. For a host WITH a desktop that
self-healed on the next tick — the content hash differed from the recorded
None, so it republished. For a host on the DEFAULT WALLPAPER it did not: the
hash is None, the recorded value is None, "unchanged" is true forever, and the
row was never written for the life of the process.

So the bug was invisible on exactly the hosts that had nothing to show, which
is the population it mattered for. Hence: the gate is on SUCCESS
(`published_once`), never on having attempted.

Container-free: the pool, the pointer, the render and the bridge are all
stubbed. What is under test is the retry DECISION, not artifact storage or
transport.

Run:  python3 scripts/test_desktop_snapshot_publish.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps")]

failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


def _run_ticks(ap, *, has_desktop: bool, bridge_up_after: int, ticks: int):
    """Drive the publisher loop with a stubbed world.

    `bridge_up_after` is how many attempts fail before the bridge answers —
    modelling the real lifespan ordering, where the task starts before the
    client exists. Returns (reasons that LANDED, sleeps taken).
    """
    landed: list[str] = []
    attempts = {"n": 0}

    async def fake_publish(pool, *, reason="change"):
        attempts["n"] += 1
        if attempts["n"] <= bridge_up_after:
            return False                      # bridge not up yet
        landed.append(reason)
        return True

    async def fake_hash(pool):
        return "deadbeef" if has_desktop else None

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)
        if len(slept) >= ticks:
            raise asyncio.CancelledError      # stop the loop deterministically

    orig = (ap.publish_desktop_snapshot, ap._desktop_content_hash,
            ap.brain_service.get_pool, asyncio.sleep)
    ap.publish_desktop_snapshot = fake_publish
    ap._desktop_content_hash = fake_hash
    ap.brain_service.get_pool = lambda: object()
    asyncio.sleep = fake_sleep
    try:
        try:
            asyncio.run(ap.publish_desktop_snapshot_at_boot())
        except asyncio.CancelledError:
            pass
    finally:
        (ap.publish_desktop_snapshot, ap._desktop_content_hash,
         ap.brain_service.get_pool, asyncio.sleep) = orig
    return landed, slept


import server.routes.appearance as ap   # noqa: E402

# ── THE REGRESSION. Default wallpaper + bridge down on the first attempt. ──
# The old code published zero times, forever.
landed, slept = _run_ticks(ap, has_desktop=False, bridge_up_after=1, ticks=4)
check(len(landed) >= 1,
      "a DEFAULT-WALLPAPER host still publishes after the first attempt fails "
      f"(landed={landed})")
check(bool(landed) and landed[0] == "boot",
      "and it is still labelled 'boot' — the boot publish is the one that has "
      f"not happened yet (landed={landed})")

# It must RETRY fast while nothing has landed, not sit out a 5-minute interval.
check(bool(slept) and slept[0] < 60,
      f"retries fast until the first publish lands (first sleep={slept[:1]})")
check(any(s >= 300 for s in slept),
      f"settles to the 5-minute interval after it lands (sleeps={slept})")

# ── A host WITH a desktop: same recovery, and no republish while unchanged. ──
landed2, _ = _run_ticks(ap, has_desktop=True, bridge_up_after=1, ticks=5)
check(len(landed2) == 1,
      "a host WITH a desktop publishes once and then stays quiet while the "
      f"content hash is unchanged (landed={landed2})")

# ── Bridge up immediately: publishes on the very first tick. ──
landed3, slept3 = _run_ticks(ap, has_desktop=True, bridge_up_after=0, ticks=3)
check(landed3[:1] == ["boot"],
      f"publishes on the first tick when the bridge is already up (landed={landed3})")
check(bool(slept3) and slept3[0] >= 300,
      f"and goes straight to the long interval (sleeps={slept3})")

# ── The loop must not swallow cancellation (shutdown must actually stop it). ──
src = inspect.getsource(ap.publish_desktop_snapshot_at_boot)
check("CancelledError" in src and "raise" in src,
      "re-raises CancelledError so the shutdown cancel actually stops the task")

print("\n".join(notes))
if failures:
    print("\nFAILURES:")
    for f in failures:
        print("  FAIL  " + f)
    sys.exit(1)
print(f"\nPASS — {len(notes)} checks")
