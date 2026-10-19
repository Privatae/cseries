"""server.loop_splay — break up the boot herd of periodic background workers.

Every periodic worker in this pod is `asyncio.create_task`'d from the same
lifespan block within a few milliseconds, and then runs on a fixed interval
forever. Nothing ever pulls them apart, so their wakeups stay correlated for the
life of the pod: the two 60s loops fire together on every single cycle, and at
boot the work-first ones all hit the database in the same instant.

Measured on one Host (2026-08-31): four background loops sharing one asyncpg
pool held the pool at 3-4 connections indefinitely, each backend re-touched just
inside the 60s idle-prune window so it never aged out. That is bounded and it is
not a leak — but it is bounded by luck rather than by design, and the same
alignment is what makes a boot storm: after a node roll, every pod runs its
whole worker set against a database that is itself still warming up.

The fix is one random delay, once, before a loop's first pass. Cadence is
untouched — a 60s loop is still a 60s loop, it just no longer starts on the same
tick as its neighbours. Random rather than a fixed per-loop offset on purpose:
a fixed table decorrelates the loops inside ONE pod while leaving every pod on a
node aligned with every other, which is the bigger herd.

Bounded on purpose too. The point is to stop simultaneity, not to spread work
across minutes — a few seconds is enough to decorrelate a handful of loops, and
capping it means no worker's first pass is ever delayed by more than one cycle
of the shortest loop in the set. Without the cap, the 300s backup poll would
have its first pass pushed out to anywhere in [300, 600)s.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

log = logging.getLogger("privatae.loop_splay")

# Upper bound on the initial delay. 15s is the shortest periodic worker in the
# pod (hub_peers), so the fastest loop is fully decorrelated across its own
# cycle and no slower loop is set back by more than that.
SPLAY_MAX_S = float(os.environ.get("LOOP_SPLAY_MAX_S", "15"))


async def splay(interval_s: float, name: str) -> None:
    """Sleep a random slice of one interval before a periodic loop's first pass.

    Call it immediately before `while True:`, never inside the loop — this is a
    one-time phase offset, and re-rolling it every iteration would turn a fixed
    cadence into a random one and make the worker's timing unreproducible.

    Set LOOP_SPLAY_MAX_S=0 to disable (tests that assert on timing, or anyone
    bisecting a scheduling problem who wants the old lockstep back).
    """
    ceiling = min(float(interval_s), SPLAY_MAX_S)
    if ceiling <= 0:
        return
    delay = random.uniform(0, ceiling)
    log.debug("%s: splaying first pass by %.1fs", name, delay)
    await asyncio.sleep(delay)
