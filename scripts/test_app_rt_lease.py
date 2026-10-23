#!/usr/bin/env python3
"""The app-runtime lane's traffic-lease + self-healing — review-3 B1, pinned.

The lane's whole liveness story is three behaviors with no test until now:
  * the TRAFFIC is the lease — a request against a cached wake older than
    the heartbeat interval fires an idempotent warm re-stamp (without it,
    a window open past the 900s sweep grace was reaped mid-use);
  * a failed re-stamp drops to the COLD path instead of serving the dead
    cached IP forever;
  * invalidate/forget clear lane state so reaps and reinstalls self-heal;
  * a meterless release (a sweep-reaped window) bills NOTHING.

Container-free: imports harness_backend directly (its module-level imports
are stdlib-only, by design), stubs the bridge via monkeypatching _bridge.

Run: python3 scripts/test_app_rt_lease.py
"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.applications import harness_backend as hb  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("OK    " if cond else "FAIL  ") + name
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeBridge:
    def __init__(self, ok=True, url="http://[fd00::1]:8850"):
        self.ok, self.url = ok, url
        self.calls = []
        self._connected = True

    async def request_full(self, op, params):
        self.calls.append((op, dict(params)))
        if not self.ok:
            return {"ok": False, "error": "operator says no"}
        return {"ok": True, "data": {"url": self.url, "ready": True}}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _with_bridge(bc):
    hb._bridge = lambda: bc
    return bc


APP = "lease-test-app"


def reset():
    hb._state.pop(APP, None)


# ── cold wake stamps ────────────────────────────────────────────────
reset()
bc = _with_bridge(FakeBridge())
host = run(hb.ensure_awake_app(APP))
check("cold wake returns the pod host", host == "[fd00::1]:8850", str(host))
st = hb._state[APP]
check("cold wake records the stamp", (st.get("stamped_at") or 0) > 0)
check("exactly one wake op sent",
      len(bc.calls) == 1 and bc.calls[0][1] == {"app_rt": APP}, str(bc.calls))

# ── young cache: no re-stamp ────────────────────────────────────────
bc.calls.clear()
host = run(hb.ensure_awake_app(APP))
check("young cache serves without a wake call", bool(host) and not bc.calls)

# ── aged cache: warm re-stamp fires (the traffic IS the lease) ──────
st["stamped_at"] = time.monotonic() - hb.HARNESS_LEASE_HEARTBEAT_S - 1
bc.calls.clear()
host = run(hb.ensure_awake_app(APP))
check("aged cache fires exactly one warm re-stamp", len(bc.calls) == 1)
check("stamp refreshed", time.monotonic() - st["stamped_at"] < 5)
check("still serves the host", host == "[fd00::1]:8850")

# ── failed re-stamp drops to the cold path, not a dead IP ───────────
st["stamped_at"] = time.monotonic() - hb.HARNESS_LEASE_HEARTBEAT_S - 1
flaky_bridge = FakeBridge()
calls = {"n": 0}


async def flaky(op, params):
    calls["n"] += 1
    if calls["n"] == 1:
        return {"ok": False, "error": "pod swept"}
    return {"ok": True, "data": {"url": "http://[fd00::2]:8850", "ready": True}}


flaky_bridge.request_full = flaky
_with_bridge(flaky_bridge)
host = run(hb.ensure_awake_app(APP))
check("failed re-stamp falls through to a cold wake (2 ops)", calls["n"] == 2)
check("cold wake re-established a FRESH pod address",
      host == "[fd00::2]:8850", str(host))

# ── invalidate + re-wake (the proxy's 502 lane) ─────────────────────
hb.invalidate_app_url(APP)
check("invalidate clears the cached url", hb._state[APP].get("url") is None)
_with_bridge(FakeBridge(url="http://[fd00::3]:8850"))
host = run(hb.ensure_awake_app(APP))
check("post-invalidate wake returns the new pod", host == "[fd00::3]:8850")

# ── forget_app drops everything ─────────────────────────────────────
hb.forget_app(APP)
check("forget_app removes the state entry", APP not in hb._state)

# ── cold wakes are single-flight, and a failed one backs off ────────
# With the pod not yet Ready, every asset request of the open window used to
# fire its own cold wake (dozens of 504s a minute). One wake in flight per
# app; a failed wake is remembered.
reset()
hb._wake_locks.pop(APP, None)
slow_calls = {"n": 0}


class SlowFailingBridge(FakeBridge):
    async def request_full(self, op, params):
        slow_calls["n"] += 1
        await asyncio.sleep(0.2)
        return {"ok": False, "error": "operator /harness/wake 504: still starting"}


_with_bridge(SlowFailingBridge())


async def _storm():
    hosts = await asyncio.gather(*(hb.ensure_awake_app(APP) for _ in range(12)))
    return hosts


hosts = run(_storm())
check("12 concurrent requests during a slow failing wake send ONE wake op",
      slow_calls["n"] == 1, f"ops={slow_calls['n']}")
check("...and all of them answer 'not up' (None), none hang", all(h is None for h in hosts))
host = run(hb.ensure_awake_app(APP))
check("a request inside the backoff window sends NO wake op",
      slow_calls["n"] == 1 and host is None, f"ops={slow_calls['n']}")
hb._state[APP]["cold_failed_at"] = time.monotonic() - hb.APP_RT_WAKE_BACKOFF_S - 1
_with_bridge(FakeBridge(url="http://[fd00::8]:8850"))
host = run(hb.ensure_awake_app(APP))
check("after the backoff the next request wakes again and gets the pod",
      host == "[fd00::8]:8850" and hb._state[APP].get("cold_failed_at") is None, str(host))

# ── a RESIDENT hold survives a wake that has not answered yet ───────
# A wake can 504 at 60s while the pod is still installing itself. If the hold
# is only taken after a good wake it is never taken, and the operator's idle
# sweep reaps the pod 15 minutes later. The hold must be independent of the
# wake result and keep re-waking until the pod answers.
reset()
attempts = {"n": 0}


class SlowBoot(FakeBridge):
    async def request_full(self, op, params):
        self.calls.append((op, dict(params)))
        attempts["n"] += 1
        if attempts["n"] < 3:
            return {"ok": False, "error": "operator /harness/wake 504: not Ready within 60s"}
        return {"ok": True, "data": {"url": "http://[fd00::7]:8850", "ready": True}}


slow = _with_bridge(SlowBoot())
orig_retry = hb.APP_RT_RESIDENT_RETRY_S
hb.APP_RT_RESIDENT_RETRY_S = 0.05


async def _resident_slow_boot():
    hb.app_hold(APP)                       # the supervisor holds BEFORE the wake
    host = await hb.ensure_awake_app(APP)  # the first wake fails (still starting)
    check("first wake of a slow boot fails honestly", host is None)
    st = hb._state[APP]
    check("...but the resident hold is in place and its task alive",
          st.get("resident") is True and st.get("lease_task") is not None
          and not st["lease_task"].done())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if hb._state[APP].get("url"):
            break
    return hb._state[APP].get("url")


url = run(_resident_slow_boot())
check("the hold keeps re-waking and picks up the pod when it answers",
      url == "http://[fd00::7]:8850", f"url={url} attempts={attempts['n']}")
check("app_runtime_state reports running once the re-wake lands",
      hb.app_runtime_state(APP)["running"] is True)
check("the wake ops were re-sent, not given up on", attempts["n"] >= 3, str(attempts))
run(hb.app_release(APP))
check("app_release ends the hold", hb._state[APP].get("resident") is False
      and (hb._state[APP].get("lease_task") is None))
hb.APP_RT_RESIDENT_RETRY_S = orig_retry

# ── a re-install recycles the pod, and keeps a resident hold ────────
# "Install as my app" wrote a new version and the new descriptor, but the
# RUNNING pod kept serving the old source — a pod fetches its source once, at
# creation. recycle_app drops it so the next wake fetches the new source; the
# resident switch survives.
reset()
hb._wake_locks.pop(APP, None)
rec = _with_bridge(FakeBridge())


async def _reinstall_while_running():
    await hb.ensure_awake_app(APP)
    hb.app_hold(APP)                       # a resident app the user switched on
    st = hb._state[APP]
    check("precondition: a live wake with a resident hold",
          bool(st.get("url")) and st.get("resident") is True)
    rec.calls.clear()
    did = await hb.recycle_app(APP)        # <- what the installer now calls
    check("recycle_app reports it dropped a live wake", did is True)
    check("...released the pod (harness.release sent)",
          any(op == "harness.release" for op, _p in rec.calls), str(rec.calls))
    check("...cleared the cached url so the next wake is cold", st.get("url") is None)
    check("...and KEPT the resident hold (the user's switch survives a re-install)",
          st.get("resident") is True and st.get("lease_task") is not None)
    _with_bridge(FakeBridge(url="http://[fd00::a]:8850"))
    host = await hb.ensure_awake_app(APP)
    check("the next wake brings up the replacement pod",
          host == "[fd00::a]:8850", str(host))
    hb._stop_lease(APP)


run(_reinstall_while_running())
reset()
check("recycle_app on an app with no live wake is a no-op",
      run(hb.recycle_app(APP)) is False)

# ── the release respects the meter contract ─────────────────────────
reset()
bc3 = _with_bridge(FakeBridge())
run(hb.ensure_awake_app(APP))
charges = []


async def fake_charge(**kw):
    charges.append(kw)
    return ("run", 1)


import server.routes.compute as compute  # noqa: E402
orig = compute.charge_harness_compute
compute.charge_harness_compute = fake_charge
try:
    async def release_env(op, params):
        if op == "harness.release":
            return {"ok": True, "data": {"ready_duration_s": 42.0}}
        return {"ok": True, "data": {}}
    bc3.request_full = release_env
    run(hb._release_and_bill_app(APP))
    check("release bills the metered window with the app-rt prefix",
          bool(charges) and charges[0]["session_id"] == f"app-rt:{APP}"
          and charges[0]["compute_s"] == 42.0, str(charges))
    charges.clear()

    async def release_none(op, params):
        return {"ok": True, "data": {"ready_duration_s": None}}
    bc3.request_full = release_none
    run(hb._release_and_bill_app(APP))
    check("a meterless release (sweep-reaped window) bills NOTHING",
          not charges, str(charges))
finally:
    compute.charge_harness_compute = orig

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all green")
