#!/usr/bin/env python3
"""Concurrent fan-outs share one harness pod, so they share one release.

Measured on one Host 2026-09-21. Four review.fan_out sessions all named
`codex`. There is one harness Deployment per (entity, harness), so all four
landed on the SAME pod, and each group's finally sent its own release. At
21:58:59 the first one out scaled the Deployment to 0:

  * three runs died mid-body ("TransferEncodingError: Not enough data to
    satisfy transfer length header"), and their collect-out then failed
    against a pod that no longer existed — so _one_run's "the build was not
    re-run and its partial work is kept" kept NOTHING, because the worktree
    lives on the pod's emptyDir and went with it;
  * and the window was billed four times. release() deliberately does not
    clear the metering stamps, so a second release with no wake between
    re-bills the same window — harness_compute_log rows 853-856 charged
    960.4 s for a pod that was Ready 252.9 s.

What this pins:
  * the LAST holder out releases, not the first: a group finishing while
    another still holds the pod sends NO release;
  * exactly ONE release per pod window, whatever the group count — the
    billing property, since one release is one metered window;
  * holders never go negative, so a double-release cannot make a live holder
    look like none;
  * a group that raises still leaves the holders (its finally runs), and does
    not stop the last one out from releasing;
  * independent harnesses do not share a count (codex releasing must not
    depend on claude-code);
  * _harness_group is actually wired to it and no longer sends a bare release.

Container-free: _harness_bridge is stubbed with a counter, so no bridge, no
operator, no pod.

Run: APP_ROOT=<repo> python3 scripts/test_fanout_shared_pod_release.py
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.routes import review as rv  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Bridge:
    """Counts the releases that actually reach the operator, per harness."""

    def __init__(self):
        self.releases = []

    async def __call__(self, op, harness_id, **params):
        if op == "harness.release":
            self.releases.append(harness_id)
        return {"ok": True, "data": {"ready_duration_s": 252.9, "scaled_to": 0}}

    def count(self, harness_id):
        return self.releases.count(harness_id)


def _fresh():
    """A stubbed bridge and an empty holder table — the state a cold kernel
    starts from, which is also what a mid-batch restart resets to."""
    rv._harness_holders.clear()
    bridge = _Bridge()
    rv._harness_bridge = bridge
    return bridge


_ORIGINAL_BRIDGE = rv._harness_bridge

print("== the incident: two groups on one pod, the first one out finishes ==")
bridge = _fresh()


async def _two_groups_first_out_holds_fire():
    """Group A and group B both hold codex; A finishes while B is still
    running. A must NOT release — that is the exact tear-down that killed
    three runs mid-stream."""
    rv._hold_harness("codex")          # group A joins
    rv._hold_harness("codex")          # group B joins
    sent_a = await rv._release_harness_when_last("codex")   # A finishes first
    return sent_a


sent_a = run(_two_groups_first_out_holds_fire())
check("the first group out sends no release", sent_a is None, sent_a)
check("...so the pod is still up for the group still running",
      bridge.count("codex") == 0, bridge.releases)

sent_b = run(rv._release_harness_when_last("codex"))
check("the last group out sends the release", sent_b is not None and sent_b.get("ok"))
check("exactly ONE release for the window = ONE metered charge",
      bridge.count("codex") == 1, bridge.releases)

print("== the measured shape: four concurrent sessions, one pod ==")
bridge = _fresh()


async def _four_concurrent_groups():
    """The 2026-09-21 dispatch. Four groups, staggered finishes, one pod."""
    for _ in range(4):
        rv._hold_harness("codex")

    async def _finish(delay):
        await asyncio.sleep(delay)
        return await rv._release_harness_when_last("codex")

    return await asyncio.gather(*(_finish(d) for d in (0.00, 0.01, 0.02, 0.03)))


sent = run(_four_concurrent_groups())
check("four groups produce exactly one release (was four)",
      bridge.count("codex") == 1, bridge.releases)
check("only the last group sent it",
      [s is not None for s in sent] == [False, False, False, True],
      [s is not None for s in sent])
check("the holder entry is dropped, so the next batch starts cold",
      "codex" not in rv._harness_holders, rv._harness_holders)

print("== a group that raises still leaves the holders ==")
bridge = _fresh()


async def _group_raises_midway():
    """Group A dies inside its try; its finally still decrements. B must
    still be able to release — a crashed peer must not strand the pod."""
    rv._hold_harness("codex")
    rv._hold_harness("codex")
    try:
        raise RuntimeError("stream died after 6 frames")
    except RuntimeError:
        pass
    finally:
        await rv._release_harness_when_last("codex")   # A's finally
    return await rv._release_harness_when_last("codex")  # B finishes normally


sent = run(_group_raises_midway())
check("the crashed group released nothing but left the count",
      bridge.count("codex") == 1, bridge.releases)
check("the surviving group still sent the one release", sent is not None)

print("== holders never go negative ==")
bridge = _fresh()
run(rv._release_harness_when_last("codex"))   # release with nobody holding
check("a release with no holder still goes out (single-group path unchanged)",
      bridge.count("codex") == 1, bridge.releases)
check("the count floors at 0 rather than going negative",
      rv._harness_holders.get("codex", 0) == 0, rv._harness_holders)
rv._hold_harness("codex")
check("...so the next holder joins at 1, not at 0 or -1",
      rv._harness_holders["codex"] == 1, rv._harness_holders)
run(rv._release_harness_when_last("codex"))
check("and that holder's release lands", bridge.count("codex") == 2, bridge.releases)

print("== harnesses do not share a count ==")
bridge = _fresh()


async def _two_harnesses():
    rv._hold_harness("codex")
    rv._hold_harness("claude-code")
    await rv._release_harness_when_last("codex")
    return rv._harness_holders.get("claude-code")


held = run(_two_harnesses())
check("codex released on its own last holder", bridge.count("codex") == 1)
check("claude-code is untouched and still held", held == 1, held)
check("claude-code was not released by codex's finish",
      bridge.count("claude-code") == 0, bridge.releases)

print("== a lane that never held: releases only when no build holds (review B3) ==")
# The vendor-login routes and the catalog read wake a pod and let go without
# joining the holders (a login ending in done:false leaves the pod up on
# purpose, and would pin the count forever). Their release used to be the same
# bare replicas:0 — a login finishing while a build streamed through the pod
# took the build down. Now: held -> skipped; unheld -> the old behaviour.
bridge = _fresh()


async def _login_while_build_holds():
    rv._hold_harness("codex")                       # a build is on the pod
    skipped = await rv._release_unless_held("codex", release_in_background=True)
    releases_after_skip = bridge.count("codex")     # measured BEFORE the build lets go
    after_build = await rv._release_harness_when_last("codex")
    return skipped, releases_after_skip, after_build


skipped, releases_after_skip, after_build = run(_login_while_build_holds())
check("a login's release while a build holds the pod is SKIPPED", skipped is None)
check("...so the pod stayed up for the build", releases_after_skip == 0, releases_after_skip)
check("...and the build's own last-out release closes the window",
      after_build is not None and bridge.count("codex") == 1, bridge.releases)
check("holders are unchanged by the skipped release",
      "codex" not in rv._harness_holders, rv._harness_holders)

bridge = _fresh()
sent = run(rv._release_unless_held("codex", release_in_background=True))
check("with nobody holding, the login's release goes out as before",
      sent is not None and bridge.count("codex") == 1, bridge.releases)

print("== every release lane is wired (review B3) ==")
rv._harness_bridge = _ORIGINAL_BRIDGE
review_src = (ROOT / "server/routes/review.py").read_text()
check("_harness_group releases through the holder count, shielded like the single build (re-review NB6)",
      "rel = await asyncio.shield(_release_harness_when_last(h))" in review_src)
# A deferred release must not make the single build FREE: the batch lane
# discards the release envelope and meters each run by its own wall clock,
# so a build under a shared window meters the same way (re-review NB1).
check("a deferred single-build release meters the run by its own wall clock, from after the wake",
      "compute_s = round(time.monotonic() - run_t0, 2)" in review_src
      and "run_t0 = time.monotonic()" in review_src)
check("_harness_group no longer sends a bare per-batch release",
      'rel = await _harness_bridge("harness.release", h)' not in review_src)
check("the group joins the holders before its first wake",
      "_hold_harness(h)" in review_src)
check("the single-build lane HOLDS and releases when last (was a bare release)",
      "_hold_harness(harness_id)" in review_src
      and "rel = await asyncio.shield(_release_harness_when_last(harness_id))" in review_src
      and 'rel = await asyncio.shield(_harness_bridge("harness.release", harness_id))'
      not in review_src)
check("the login routes release only when unheld",
      review_src.count("_release_unless_held(harness,") == 3,
      review_src.count("_release_unless_held(harness,"))
check("the catalog read releases only when unheld",
      "asyncio.shield(_release_unless_held(harness_id))" in review_src)
check("no bare harness.release is left outside the two helpers",
      review_src.count('_harness_bridge("harness.release"') == 2,
      review_src.count('_harness_bridge("harness.release"'))

print()
if FAILURES:
    print(f"FAIL {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all green")
