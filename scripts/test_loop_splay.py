#!/usr/bin/env python3
"""The pod's periodic workers must not all wake on the same tick.

WHAT THIS PROTECTS (built 2026-09-01, out of the entity-9128 memory audit).

Every periodic worker is `asyncio.create_task`'d from one lifespan block within
a few milliseconds and then runs on a fixed interval forever. Nothing pulls them
apart, so the correlation is permanent: the two 60s workers (mirror reconcile,
NAC reaper) fire on the same tick for the life of the pod, and the work-first
ones all hit the database in the same instant at boot.

Measured on one Host (2026-08-31): four loops sharing one asyncpg pool held it
at 3-4 connections indefinitely, each backend re-touched just inside the 60s
idle-prune window so it never aged out. Bounded, and not a leak — but bounded by
luck rather than design. The same alignment is what makes a boot storm after a
node roll, when every pod runs its whole worker set against a database that is
itself still warming up.

THE INVARIANTS.

  1. Every periodic worker calls splay() before its loop. A new worker that
     opts out is how the convention rots — one exempt loop puts the herd back.
  2. splay() is called ONCE, before `while True`, never inside it. Inside, it
     stops being a phase offset and becomes a random cadence: the worker's
     timing is no longer reproducible and its interval no longer means what its
     constant says.
  3. The delay is BOUNDED and never exceeds one interval. Without the cap the
     300s backup poll would have its first pass pushed into [300, 600)s —
     doubling a latency nobody asked to change. The point is to stop
     simultaneity, not to spread work across minutes.
  4. It is random, and it is disable-able. A fixed per-loop offset would
     decorrelate the loops inside ONE pod while leaving every pod on a node
     aligned with every other, which is the bigger herd; and anyone bisecting a
     scheduling problem needs the old lockstep back without editing five files.

WHAT THIS DOES NOT CATCH. It reads the loop sources for the call and its
position; it does not run the workers. A refactor that keeps the call but moves
the loop out from under it would pass.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_loop_splay.py
"""
import asyncio
import importlib
import os
import re
import sys
import time

FAILED = 0

# (module path, the coroutine that owns the loop, the interval it must splay on)
PERIODIC_WORKERS = (
    ("server/network/hub_peers.py", "reconcile_hub_peers_loop", "RECONCILE_INTERVAL_S"),
    ("server/network/mirror.py", "reconcile_mirror_loop", "RECONCILE_INTERVAL_S"),
    ("server/network/nac_controller.py", "run_reaper_loop", "interval_seconds"),
    ("server/backup_ship.py", "run_backup_ship_loop", "SHIP_POLL_S"),
    ("server/process_awareness.py", "run_sampler_loop", "SAMPLE_INTERVAL_S"),
)


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _body(src: str, fn: str) -> str:
    """The source of one top-level async def, up to the next top-level block."""
    m = re.search(rf"^async def {re.escape(fn)}\(", src, re.M)
    if not m:
        return ""
    rest = src[m.start():]
    nxt = re.search(r"^(?:async def |def |class )", rest[1:], re.M)
    return rest[: nxt.start() + 1] if nxt else rest


async def _elapsed(fn, interval: float) -> float:
    """The delay `fn` WOULD sleep — read by intercepting asyncio.sleep, not by
    waiting it out. This gate used to measure real wall clock around the real
    await: six 1s splays, five 2s splays and one 15s-capped splay, 17s of a
    108s pre-merge run spent asleep to learn a number the splay function had
    already chosen. The property under test is the VALUE of the delay
    (bounded by the interval, capped, random, zero when disabled), and that
    value is exactly what the sleep is asked for. Any extra real sleep the
    function did would show up here as a longer recorded delay, so nothing
    the old measurement could catch is lost."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _record(delay, *a, **k):
        slept.append(float(delay))

    asyncio.sleep = _record          # ls resolves `asyncio.sleep` at call time
    try:
        await fn(interval, "gate")
    finally:
        asyncio.sleep = real_sleep
    return sum(slept)


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("loop splay — the pod's periodic workers must not move in lockstep")

    import server.loop_splay as ls  # noqa: E402

    # ── 1 + 2. Every worker splays, once, before its loop ───────────────
    for path, fn, interval in PERIODIC_WORKERS:
        src = open(os.path.join(repo, path), encoding="utf-8").read()
        body = _body(src, fn)
        check(f"{fn} found in {path}", bool(body))
        if not body:
            continue

        check(f"{fn} splays on its own interval ({interval})",
              f"splay({interval}" in body,
              "A periodic worker that skips the offset re-forms the herd on its\n"
              "own. Splaying on a DIFFERENT number than the loop sleeps on lets\n"
              "the offset exceed a cycle, which is what the cap exists to stop.")

        call, loop = body.find("await splay("), body.find("while True:")
        check(f"{fn} splays BEFORE `while True`, not inside it",
              call != -1 and loop != -1 and call < loop,
              "Inside the loop this is no longer a phase offset — it is a random\n"
              "cadence, and the worker's interval stops meaning what its\n"
              "constant says.")
        check(f"{fn} splays exactly once",
              body.count("await splay(") == 1,
              f"Found {body.count('await splay(')} calls.")

    # ── 3. Bounded ──────────────────────────────────────────────────────
    for _ in range(6):
        d = asyncio.run(_elapsed(ls.splay, 1.0))
        check("a 1s-interval worker is never delayed past its own interval",
              d <= 1.05, f"delayed {d:.2f}s")

    check("the cap is well under the slowest worker's interval",
          ls.SPLAY_MAX_S < 300,
          "SHIP_POLL_S is 300s. An uncapped splay would push its first pass\n"
          "into [300, 600)s — doubling a latency nobody asked to change.")

    d = asyncio.run(_elapsed(ls.splay, 300.0))
    check("a 300s-interval worker is clamped to the cap",
          d <= ls.SPLAY_MAX_S + 0.05,
          f"delayed {d:.2f}s against a {ls.SPLAY_MAX_S}s cap")

    # ── 4. Random, and disable-able ─────────────────────────────────────
    delays = {round(asyncio.run(_elapsed(ls.splay, 2.0)), 3) for _ in range(5)}
    check("the offset is random, not a fixed constant",
          len(delays) > 1,
          "A fixed per-loop offset decorrelates the loops inside ONE pod while\n"
          "leaving every pod on a node aligned with every other — and that is\n"
          "the bigger herd.")

    os.environ["LOOP_SPLAY_MAX_S"] = "0"
    try:
        importlib.reload(ls)
        d = asyncio.run(_elapsed(ls.splay, 60.0))
        check("LOOP_SPLAY_MAX_S=0 disables it entirely", d < 0.05,
              f"still slept {d:.2f}s. Anyone bisecting a scheduling problem "
              "needs the old lockstep back without editing five files.")
    finally:
        del os.environ["LOOP_SPLAY_MAX_S"]
        importlib.reload(ls)
    check("the cap is restored after the disable check",
          ls.SPLAY_MAX_S > 0, f"SPLAY_MAX_S={ls.SPLAY_MAX_S}")

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
