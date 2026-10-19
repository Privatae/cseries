#!/usr/bin/env python3
"""Gate: an open app window holds its runtime pod.

An app whose server side runs in its own pod (manifest.runs_in_pod) had its
lease re-stamped only by the window's own requests, so a window someone was
only WATCHING had its pod swept 900 s after the last request. Now the launch
route opens a window hold (harness_backend.app_window_opened) that re-stamps
the lease every HARNESS_LEASE_HEARTBEAT_S, the way an open editor socket
does, and it ends on liveness:
  - the last window's close releases (after the grace); a close while
    another window is open releases nothing;
  - a one-shot caller's close (window_id None, the delivery proof) never
    takes a person's open window away;
  - with no desktop tab connected to the kernel at all, the hold ends.

Real harness_backend with the operator call, the clock and the release
scripted; no pod, no bridge. Run: APP_ROOT=<repo> python3 scripts/test_app_window_holds_pod.py
"""
import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
MAIN = types.ModuleType("server.main")
MAIN._ws_connections = {"a desktop tab"}
sys.modules["server.main"] = MAIN

FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"[window-hold] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


from server.applications import harness_backend as HB  # noqa: E402
import time as _time  # noqa: E402

CLOCK = [1000.0]
HB.time = types.SimpleNamespace(monotonic=lambda: CLOCK[0], time=_time.time)

if not hasattr(HB, "app_window_opened"):
    check("harness_backend has a window hold (app_window_opened)", False)
    print(f"[window-hold] {len(FAILS)} FAILED")
    sys.exit(1)

stamps: list[str] = []
released: list[str] = []


class _Proxy:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_d):
        await asyncio.sleep(0)


async def _wake_app(app_id):
    stamps.append(app_id)
    return {"ok": True, "url": "http://[fd00::9]:8080"}


async def _release(app_id):
    released.append(app_id)


HB.asyncio = _Proxy()
HB._wake_app = _wake_app
HB._release_and_bill_app = _release


async def spin(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def main():
    st = HB._st("dash", HB._APP_RT_MARK)
    st["url"] = "http://[fd00::9]:8080"

    HB.app_window_opened("dash", "w1")
    await spin()
    check("an open window re-stamps its pod's lease with no request from it",
          len(stamps) >= 3 and set(stamps) == {"dash"}, repr(stamps[:5]))

    HB.app_window_opened("dash", "w2")
    HB.app_window_closed("dash", "w1")
    await spin()
    check("closing one of two windows releases nothing and keeps the hold",
          released == [] and st.get("release_task") is None
          and not st["window_task"].done(), repr(released))

    HB.app_window_closed("dash", None)          # the delivery proof is done
    await spin()
    check("a one-shot caller's close never takes a person's open window away",
          released == [] and set(st.get("windows") or {}) == {"w2"}, repr(st.get("windows")))

    HB.app_window_closed("dash", "w2")
    await spin()
    check("the last window's close ends the hold and releases after the grace",
          released == ["dash"] and st.get("window_task") is None, repr(released))

    n = len(stamps)
    await spin()
    check("...and nothing re-stamps it after that", len(stamps) == n)

    # A page RELOAD (repro: window_reload_leak.py) sends
    # no close, and desktop-restore relaunches the window with a NEW id. The
    # person then closes that window. The old id's client never beats again,
    # so once WINDOW_STALE_S has passed nothing holds the pod.
    released.clear()
    st["url"] = "http://[fd00::9]:8080"
    st["windows"] = {}
    HB.app_window_opened("dash", "w-before-reload")
    await spin(4)
    CLOCK[0] += 30
    HB.app_window_opened("dash", "w-after-reload")        # no close for the old id
    await spin(4)
    HB.app_window_closed("dash", "w-after-reload")        # the person closes the window
    await spin(4)
    check("a reload's old window id still holds the pod only while it could be alive",
          released == [] and set(st.get("windows") or {}) == {"w-before-reload"},
          repr((released, st.get("windows"))))
    CLOCK[0] += HB.WINDOW_STALE_S + 1
    n = len(stamps)
    await spin(40)
    check("...once its client has not beaten for WINDOW_STALE_S the hold ends and the "
          "pod is released", released == ["dash"] and not st.get("windows"),
          repr((released, st.get("windows"))))
    check("...and the beat stops re-stamping it", len(stamps) - n <= 1, repr(len(stamps) - n))

    # A window whose client keeps beating is held however long it is open.
    released.clear()
    st["url"] = "http://[fd00::9]:8080"
    HB.app_window_opened("dash", "w-watched")
    for _ in range(10):                                   # ~an hour of watching
        CLOCK[0] += HB.WINDOW_BEAT_S
        check_beat = HB.app_window_beat("dash", "w-watched")
        await spin(4)
    check("a watched window that keeps beating keeps its pod for as long as it is open",
          check_beat and released == [] and set(st.get("windows") or {}) == {"w-watched"},
          repr((released, st.get("windows"))))
    HB.app_window_closed("dash", "w-watched")
    await spin()
    check("...and its close releases it", released == ["dash"], repr(released))

    # A beat already in flight when the window's close landed does not hold
    # the closed id again (window_sim.py "in-flight beat after close").
    st3 = HB._st("dash3", HB._APP_RT_MARK)
    st3["url"] = "http://[fd00::9]:8082"
    HB.app_window_opened("dash3", "w-c")
    HB.app_window_closed("dash3", "w-c")
    check("a beat that arrives after its window closed does not hold it again",
          HB.app_window_beat("dash3", "w-c") is False and not st3.get("windows"),
          repr(st3.get("windows")))
    await spin()

    # A beat for a window the kernel does not know (it restarted) holds it again.
    st2 = HB._st("dash2", HB._APP_RT_MARK)
    st2["url"] = "http://[fd00::9]:8081"
    check("a beat for an unknown window (a kernel restart) holds it again",
          HB.app_window_beat("dash2", "w-z") and set(st2.get("windows") or {}) == {"w-z"})
    HB.app_window_closed("dash2", "w-z")
    await spin()

    # A tab closed without its windows' close calls: no desktop tab is
    # connected, so nothing is left holding the pod.
    released.clear()
    st["url"] = "http://[fd00::9]:8080"
    HB.app_window_opened("dash", "w3")
    await spin(4)
    MAIN._ws_connections = set()
    await spin()
    check("with no desktop tab connected the hold ends and the pod is released",
          released == ["dash"] and not st.get("windows"), repr((released, st.get("windows"))))


asyncio.new_event_loop().run_until_complete(main())

# The shell's half: it beats for every window the launch answer says is held,
# stops on the window's close, and closes every held window on pagehide.
js = (ROOT / "dashboard/base/js/app-runner.js").read_text()
check("the shell beats for each held window and closes them all on pagehide",
      "holdWindow(appId, appWindowId, launchData.window_beat_s)" in js
      and "'window-beat'" in js and "addEventListener('pagehide'" in js
      and "releaseWindow(appWindowId)" in js)
routes = (ROOT / "server/routes/applications.py").read_text()
check("the launch answer carries window_beat_s and the beat route exists",
      '"window_beat_s": window_beat_s' in routes and '"/{app_id}/window-beat"' in routes)
if FAILS:
    print(f"[window-hold] {len(FAILS)} FAILED")
    sys.exit(1)
print("[window-hold] all checks passed")
