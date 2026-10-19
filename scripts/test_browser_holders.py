#!/usr/bin/env python3
"""The browser scales to zero when nobody is holding it (2026-09-07).

The browser is a spike: it should not run all the time, and it scales to
zero with inactivity.

Two separate defects kept a 1Gi / 2-vCPU Chromium up indefinitely, and this
file pins both fixes:

  1. OWNERSHIP GUESSING. browser_agents._release used to release only when
     THAT run performed the wake. A run finding the browser already awake left
     it running while still refreshing the operator's sweep boundary on its way
     past, so once the original owner vanished nothing would ever release it
     and the 900s sweep could never fire. Replaced by a holder count: the last
     one out releases, whoever woke it.

  2. AN OPEN SOCKET TREATED AS USE. The lease re-stamps last-wake while a WS is
     live, which is right for an editor and wrong for a browser tab. Measured
     on one Host: 5.67 h continuous, and 24.83 metered browser-hours on
     2026-09-06 alone from two concurrent sessions. The lease now stops after
     HARNESS_LEASE_IDLE_S of client silence and lets the sweep reclaim the pod.

Container-free, like test_app_rt_lease: harness_backend's module-level imports
are stdlib-only by design, so this imports it directly and never touches k8s.
Run: python3 scripts/test_browser_holders.py
"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.applications import harness_backend as hb  # noqa: E402

APP = "browser"
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def reset(*, ws: int = 0, runs: int = 0, url: str | None = "svc:8830"):
    """A fresh state row, with release_and_bill stubbed so nothing dials out."""
    hb._state.clear()
    st = hb._st(APP, APP)
    st.update({"ws": ws, "runs": runs, "url": url})
    return st


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _fire_delayed_release(st):
    """Run the task session_closed scheduled, skipping its grace sleep.

    The grace is _RELEASE_GRACE_S of real time; the property under test is the
    POST-sleep re-check, so the sleep is stubbed rather than waited out."""
    real_sleep = asyncio.sleep

    async def _no_sleep(_s, *a, **kw):
        return await real_sleep(0)

    asyncio.sleep = _no_sleep          # type: ignore[assignment]
    try:
        task = st.get("release_task")
        if task is not None:
            await task
    finally:
        asyncio.sleep = real_sleep     # type: ignore[assignment]


def main() -> int:
    released: list[tuple[str, str]] = []

    async def _fake_release_and_bill(app_id, harness_id):
        released.append((app_id, harness_id))

    hb._release_and_bill = _fake_release_and_bill  # type: ignore[assignment]

    print("── holders decide, not ownership ──")

    # THE REGRESSION: awake, nobody holding. One run joins and leaves -> asleep.
    reset()
    released.clear()
    hb.hold_run(APP, APP)
    run(hb.release_run(APP, APP))
    check("a run that finds the browser awake with no holder releases it",
          released == [(APP, APP)], str(released))

    # Two concurrent runs: the first out must NOT tear it down under the second.
    reset()
    released.clear()
    hb.hold_run(APP, APP)
    hb.hold_run(APP, APP)
    run(hb.release_run(APP, APP))
    first_out = list(released)
    run(hb.release_run(APP, APP))
    check("the first of two runs to finish does not release it", first_out == [])
    check("the last of two runs to finish does release it",
          released == [(APP, APP)], str(released))

    # A viewport outranks a run: the user's open tab is never torn down by a run.
    reset(ws=1)
    released.clear()
    hb.hold_run(APP, APP)
    run(hb.release_run(APP, APP))
    check("a run leaving does not release while a viewport holds it",
          released == [], str(released))

    # Double-release must not make a live holder look like none.
    reset()
    released.clear()
    hb.hold_run(APP, APP)
    run(hb.release_run(APP, APP))
    run(hb.release_run(APP, APP))           # spurious second leave
    hb.hold_run(APP, APP)                   # a NEW run arrives
    check("a double release cannot drive the count negative",
          hb._state[APP]["runs"] == 1, str(hb._state[APP]["runs"]))

    # release_now is what the ship proof calls; it must respect run holders too.
    reset(runs=1)
    released.clear()
    run(hb.release_now(APP, APP))
    check("release_now refuses while a run holds it", released == [], str(released))

    print("── a holder is never torn down under ──")

    # H1: closing a SPECTATOR tab must not sleep the pod under an active run.
    # session_closed schedules a task, so this has to run inside a loop — the
    # same way the proxy calls it, from an async finally.
    async def _spectator_leaves_mid_run():
        reset(ws=1, runs=1)
        released.clear()
        hb.session_closed(APP, APP)                 # the watcher closes the tab
        st = hb._state[APP]
        first = (st["ws"] == 0 and released == [])
        await _fire_delayed_release(st)             # the grace expires
        return first, list(released)

    first_ok, after_grace = run(_spectator_leaves_mid_run())
    check("closing the last viewport does not release while a run holds it",
          first_ok)
    check("the DELAYED release also refuses while a run holds it",
          after_grace == [], str(after_grace))

    # ...and once the run leaves, it does sleep.
    released.clear()
    run(hb.release_run(APP, APP))
    check("the run leaving afterwards is what sleeps it",
          released == [(APP, APP)], str(released))

    print("── an open socket is not use ──")

    # Never-stamped is not idle: a session must not be reaped before its first frame.
    reset()
    check("a session with no activity yet is not idle", not hb._idle_past_limit(APP))

    # Fresh activity holds it.
    hb.touch_activity(APP)
    check("a session that just sent something is not idle", not hb._idle_past_limit(APP))

    # Silence past the limit releases the hold.
    hb._state[APP]["last_activity"] = time.monotonic() - (hb.HARNESS_LEASE_IDLE_S + 1)
    check("a session silent past the limit IS idle", hb._idle_past_limit(APP))

    # A RUN is an unconditional holder: it drives the browser over HTTP and
    # sends no client frames, so the idle rule must never apply while one is
    # in flight (otherwise the sweep reaps a long run mid-flight).
    reset(runs=1)
    hb._state[APP]["last_activity"] = time.monotonic() - (hb.HARNESS_LEASE_IDLE_S + 1)
    check("a silent RUN is still a holder (idle applies to viewports only)",
          int(hb._state[APP]["runs"]) > 0)

    # Scoped to the browser: an editor keeps the old socket-is-holder rule,
    # because losing unflushed work to the sweep is the regression the lease
    # was built to stop.
    hb._state.clear()
    st = hb._st("vscode", "vscode")
    st["last_activity"] = time.monotonic() - (hb.HARNESS_LEASE_IDLE_S + 1)
    check("an equally silent EDITOR is never idle-reaped",
          not hb._idle_past_limit("vscode"))

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
