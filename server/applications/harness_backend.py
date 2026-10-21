"""
Harness-backed app lifecycle — an INTERACTIVE harness (the Browser) runs on the
harness pool and is woken on demand when the user opens it and released shortly
after they close it, keyed on the app's WebSocket connection.

The app's WS stays open for the whole time the window is open (cseries's proxy
imposes no idle timeout, only a 30s upstream heartbeat), and it drops when the
window closes — so the WS lifetime is the "app is open" signal, bounded for the
browser by HARNESS_LEASE_IDLE_S below. That makes the lifecycle seamless and hits
the build criteria:

  * maximum availability — awake the whole time the window is open (no mid-thought vanish);
  * minimal idle memory   — released a short grace after the last WS closes;
  * minimal idle billing   — billed for WS-connected (tab-open) time only, on the
                             harness compute lane (charge_harness_compute).

The kernel proxy (server/applications/proxy.py) calls in for any app whose manifest
declares `runtime.harness: <id>`:
  - HTTP request -> ensure_awake()   (the app's first load wakes it)
  - WS open      -> session_opened()  (a live session; keep awake)
  - WS close     -> session_closed()  (release after a grace; a page refresh that
                    reopens the WS within the grace cancels the release, no churn)
The operator idle-sweep is the ultimate backstop for any leak. Fail-soft throughout.
"""

import asyncio
import json
import time
import logging
import os
import re
import secrets as _secrets
import sys

log = logging.getLogger("cseries.harness_backend")

# Grace after the LAST app WS closes before releasing — absorbs page refreshes and
# brief reconnects without a release+re-wake churn.
_RELEASE_GRACE_S = int(os.environ.get("HARNESS_RELEASE_GRACE_S", "20"))

# ── The harness lease — ONE rule for every path that holds a harness awake ───
#
# The operator's idle-sweep reaps any harness whose `privatae.ai/last-wake`
# annotation is older than its grace window (HARNESS_IDLE_GRACE_S, 900s default
# — the operator's idle-sweep handler) and it reads
# NOTHING else. Not a live WebSocket, not an in-flight /run. So "someone is
# still using this" has to be said in the only language the sweep speaks: a
# re-stamp of that annotation, which the idempotent `harness.wake` op performs.
#
# Every holder heartbeats on THIS cadence for as long as it holds. 300s against
# a 900s grace means two consecutive heartbeats can fail and a live harness
# still is not reaped; a third miss means nothing has held it for 15 minutes,
# which is the sweep doing its actual job. Keep the 3:1 ratio if either number
# moves — above grace/2 that margin is gone.
#
# server/routes/review.py imports this for its mid-run keepalive, so the two
# paths cannot drift apart (before this they were an independent 300s literal
# there and nothing at all here).
HARNESS_LEASE_HEARTBEAT_S = int(os.environ.get("HARNESS_LEASE_HEARTBEAT_S", "300"))
# A RESIDENT process app that holds no live wake yet is re-woken (see
# app_hold): first after APP_RT_RESIDENT_RETRY_S, then twice as long after each
# wake that fails again, up to APP_RT_RESIDENT_RETRY_MAX_S, and back to the
# first wait as soon as one lands — Kubernetes' CrashLoopBackOff shape (10 s
# doubling to a 5 min cap). It never gives up: the user switched the app on,
# and only the user (or the Host) switches it off. The fixed 60 s it replaces
# re-woke a pod that could not start every minute, forever.
# The wake itself blocks for the descriptor's wakeTimeoutS, so these are gaps
# BETWEEN attempts.
APP_RT_RESIDENT_RETRY_S = int(os.environ.get("APP_RT_RESIDENT_RETRY_S", "60"))
APP_RT_RESIDENT_RETRY_MAX_S = int(os.environ.get("APP_RT_RESIDENT_RETRY_MAX_S", "300"))


def resident_retry_wait(failures: int) -> float:
    """The wait before the next re-wake of a resident app after `failures`
    consecutive failed wakes (0 = none yet)."""
    return float(min(APP_RT_RESIDENT_RETRY_S * (2 ** min(max(0, int(failures) - 1), 32)),
                     max(APP_RT_RESIDENT_RETRY_S, APP_RT_RESIDENT_RETRY_MAX_S)))
# After a cold wake FAILED (pod still starting, budget, descriptor), requests
# for that app answer "starting" without another operator wait for this long.
# Bounded below the resident retry so a pod that became Ready is picked up by
# the next request within seconds, not minutes.
APP_RT_WAKE_BACKOFF_S = int(os.environ.get("APP_RT_WAKE_BACKOFF_S", "15"))
# An OPEN SOCKET IS NOT USE. The lease below re-stamps last-wake while a WS is
# live, which was right for the editor it was built for (losing unflushed work to
# the sweep was the regression it stopped) and wrong for the browser: a tab left open
# held a 1Gi / 2-vCPU Chromium indefinitely. Measured on one Host —
# 5.67 h continuous, and 24.83 metered browser-hours billed on 09-06 alone from
# two concurrent sessions, 58% of all compute spend from 1.2% of runs.
#
# So for the browser the lease stops after this much silence FROM THE CLIENT,
# and the operator's own 900s sweep then reclaims the pod — no new teardown
# path, just stop holding it. Worst case from last interaction to scaled-to-zero
# is this + the operator grace (~30 min at the defaults), and the next request
# wakes it cold again (the stale-upstream path already handles that).
#
# Scoped by HARNESS_LEASE_IDLE_APPS; any other WS app keeps the socket-is-holder rule.
HARNESS_LEASE_IDLE_S = int(os.environ.get("HARNESS_LEASE_IDLE_S", "900"))
_IDLE_REAPED_APPS = frozenset(
    a.strip()
    for a in (os.environ.get("HARNESS_LEASE_IDLE_APPS") or "browser").split(",")
    if a.strip())

# app_id -> {harness, url, ws (live WS count), runs (one-shot holders),
#            last_activity (monotonic, client->upstream traffic),
#            release_task, lease_task}
_state: dict[str, dict] = {}

# app_id -> the single-flight wake lock (see ensure_awake). Created lazily on the
# running loop, never at import: one asyncio.Lock per app for the process's life.
_wake_locks: dict[str, "asyncio.Lock"] = {}
# app_id -> the image ref the operator's last wake of that harness reported
# (controller wake() answers `image` with the harness container's image). Kept
# across releases: it is what the pod RUNS, which changes only when the
# operator re-renders the Deployment. The desktop render check keys "this
# browser has no render-check route" on it (desktop_render.sidecar_outdated),
# so a new browser image is noticed without waking anything on a clock.
_last_image: dict[str, str] = {}


def known_image(app_id: str) -> str | None:
    """The image the last wake of this app's harness reported, or None (no
    wake since this kernel started, or an operator that does not say)."""
    return _last_image.get(app_id)


def _entity_broker_addr() -> str:
    """The entity pod's own address a workload on the harness pool dials.
    StatefulSet pod DNS: <pod>.<svc>.<ns>.svc — pod/svc/ns all = entity-<id>. The
    kernel serves on 8800 (same port the kubelet health-probes).

    On a machine whose local runtime starts the containers, they dial this
    Host on its fixed address on the runtime's network instead
    (CSERIES_RUNTIME_KERNEL_ADDR): the cluster name means nothing there."""
    from server.applications.local_runtime import kernel_addr
    local = kernel_addr()
    if local:
        return local
    eid = os.environ.get("ENTITY_ID", "").strip()
    if not eid:
        return ""
    return f"entity-{eid}-0.entity-{eid}.entity-{eid}.svc.cluster.local:8800"


def kernel_routable_base() -> str:
    """This kernel's own base URL as a workload on the harness pool dials it —
    the same address the app-runtime pods use (CSERIES_KERNEL_URL in
    sidecars/app-runtime-python). "" when ENTITY_ID is
    unset (a dev tree), which every caller must treat as "not reachable"
    rather than fall back to loopback: loopback from another pod is that
    pod, not this kernel."""
    addr = _entity_broker_addr()
    return f"http://{addr}" if addr else ""


def _bridge():
    """Where this lane's container operations go: the machine's local runtime
    when it has one, the bridge otherwise (local_runtime.runtime_client)."""
    try:
        from server.applications.local_runtime import runtime_client
        return runtime_client()
    except Exception:
        return None


def _st(app_id: str, harness_id: str) -> dict:
    return _state.setdefault(
        app_id, {"harness": harness_id, "url": None, "ws": 0, "runs": 0,
                 "last_activity": None,
                 "release_task": None, "lease_task": None})


def awake_harnesses() -> list[dict]:
    """Every HARNESS pod this kernel currently holds awake — [{app_id,
    harness_id}] — for the machine panel.

    A woken harness is a whole pod on the harness pool with its own memory
    ceiling, and it is memory this host controls: the user opened the browser or
    started a coding run, and the plan is charged for it while it is up. It was
    absent from "your machine" entirely, which is how a 1Gi pod could be awake
    on a full plan with nothing on screen saying so.

    Source is this lane's OWN state, the same place app_runtime_state reads:
    an entry with a live url is a wake this kernel is holding. Nothing is
    inferred from a manifest or a file — a harness that was never opened does
    not appear."""
    return [{"app_id": app_id, "harness_id": st.get("harness")}
            for app_id, st in _state.items()
            if st.get("harness") and st.get("harness") != _APP_RT_MARK
            and st.get("url")]


async def _wake(harness_id: str, creds: dict | None = None) -> dict:
    bc = _bridge()
    if bc is None or not getattr(bc, "_connected", False):
        return {"ok": False, "error": "bridge not connected"}
    params: dict = {"harness": harness_id}
    if creds:
        # Forwarded through the bridge to the operator, which writes them into the
        # per-harness creds Secret BEFORE scale-up (read once by the creds-init).
        params["creds"] = creds
    try:
        env = await bc.request_full("harness.wake", params)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not env.get("ok"):
        # `code` is the refusal's own status (the operator's through the
        # bridge, the local runtime's): wake_refused reads it.
        return {"ok": False, "error": env.get("error") or "wake failed", "code": env.get("code")}
    # This lane holds the pod now (cold open or a lease re-stamp): a release
    # retry still waiting on it must abandon, not scale it to 0 under us.
    from server.bridge.bridge_client import note_harness_wake
    note_harness_wake(params)
    return {"ok": True, **(env.get("data") or {})}


def _start_lease(app_id: str, harness_id: str) -> None:
    """Hold this harness's lease open while a live session exists.

    An open WebSocket means nothing to the operator's sweep — it reads only the
    last-wake annotation (see HARNESS_LEASE_HEARTBEAT_S). Before this, an app
    left open past the 900s grace had its pod scaled to 0 underneath the user
    mid-session.

    Self-limiting on purpose: it re-stamps ONLY while a WS is live and returns
    otherwise, so a bare page load (ensure_awake with no live session) never
    holds a pod open — the sweep still reclaims that, which is its actual job.
    Idempotent; a second WS on the same app joins the running heartbeat.
    """
    st = _st(app_id, harness_id)
    task = st.get("lease_task")
    if task and not task.done():
        return

    async def _beat() -> None:
        while True:
            await asyncio.sleep(HARNESS_LEASE_HEARTBEAT_S)
            cur = _state.get(app_id) or {}
            if not cur.get("url"):
                return
            runs = int(cur.get("runs") or 0)
            if runs <= 0 and int(cur.get("ws") or 0) <= 0:
                return  # nobody is holding it any more — let the release/sweep have it
            if runs <= 0 and _idle_past_limit(app_id):
                # An open socket that has sent nothing for HARNESS_LEASE_IDLE_S
                # is a tab, not a user. SKIP the re-stamp so the sweep can
                # reclaim the pod — but keep BEATING, because the client may
                # come back inside the window between the last stamp and the
                # reap. Returning outright left a user who came back driving a
                # browser that nothing re-stamped, so the sweep killed it
                # mid-use; continuing means the stamp resumes the moment the
                # client sends anything.
                continue
            # A RUN is an UNCONDITIONAL holder: it drives the browser over HTTP
            # and sends no client frames at all, so the idle test above must
            # never apply to it. The runs count is kernel-local and the
            # operator's sweep reads only the annotation, so the lease is the
            # only way a run becomes visible to it — without this a run longer
            # than the grace was reaped mid-flight.
            # No creds and no vcpus: the operator treats a warm re-wake as a pure
            # annotation re-stamp — it does not touch the creds Secret (a cold
            # wake is what writes that) and does not patch the pod template, so
            # nothing restarts. Same call fan_out makes every 5min.
            res = await _wake(harness_id)
            if not res.get("ok"):
                # Best-effort by design: one missed beat costs margin, not the
                # pod — two more are due before the grace expires.
                log.warning("harness %s lease heartbeat failed (app %s): %s",
                            harness_id, app_id, res.get("error"))

    st["lease_task"] = asyncio.create_task(_beat())


def touch_activity(app_id: str) -> None:
    """Record client->upstream traffic as USE.

    Stamped on the client->upstream direction only, never upstream->client: a
    browser streams frames at the viewport whether or not anyone is watching,
    so counting the downstream direction would make every tab permanently
    "active" and defeat the whole idle rule.
    """
    st = _state.get(app_id)
    if st is not None:
        st["last_activity"] = time.monotonic()


def _idle_past_limit(app_id: str) -> bool:
    """True when this app is idle-reaped AND its client has been silent past
    the limit. An app with no activity stamp yet is NOT idle — it has only
    just opened, and treating "never stamped" as idle would reap a session
    before its first frame."""
    if app_id not in _IDLE_REAPED_APPS:
        return False
    last = (_state.get(app_id) or {}).get("last_activity")
    if last is None:
        return False
    return (time.monotonic() - last) > HARNESS_LEASE_IDLE_S


def hold_run(app_id: str, harness_id: str) -> int:
    """Join the one-shot holders. Returns the new count.

    THE FIX for a browser left awake with no owner. `_release(woke)` in
    browser_agents released only when THAT run performed the wake, so a run
    which found the browser already awake left it running ("browser left as
    found") — and any run finding it awake refreshed the sweep boundary on its
    way past. Once the original owner vanished (crashed before its finally, or
    a viewport whose ws never reached 0) nothing was left that would ever
    release it, and the 900s sweep could not fire because every passer-by kept
    re-stamping it.

    Counting holders instead of guessing ownership makes the LAST one out
    release it, whoever woke it. It also self-heals across a kernel restart:
    the count resets to 0 while the pod is up, so the next run joins at 1 and
    releases at 0.
    """
    # _st, not _state.get: a run may join before anything has woken the
    # harness, and a hold that silently did nothing would never be released.
    st = _st(app_id, harness_id)
    st["runs"] = int(st.get("runs") or 0) + 1
    # Claim the lease exactly as a live viewport does, so the sweep can see
    # that somebody is holding this pod. Best-effort: _start_lease schedules a
    # task and so needs a RUNNING loop. Every production caller is async, but
    # hold_run itself is sync, and a sync caller must get its hold counted
    # rather than an exception — the count is the correctness property here,
    # the lease only makes the hold visible to the operator's sweep.
    try:
        _start_lease(app_id, harness_id)
    except RuntimeError as e:
        log.info("hold_run %s/%s: lease not started (%s) — the hold still "
                 "counts; a run outliving the sweep grace could be reaped",
                 app_id, harness_id, e)
    return st["runs"]


async def release_run(app_id: str, harness_id: str) -> None:
    """Leave the one-shot holders; the last one out releases the harness.
    Never goes negative — a double-release must not make a live holder look
    like none."""
    st = _state.get(app_id)
    if st is None:
        return
    st["runs"] = max(0, int(st.get("runs") or 0) - 1)
    if st["runs"] > 0:
        return
    await release_now(app_id, harness_id)


def _stop_lease(app_id: str) -> None:
    """Drop the lease heartbeat — the harness is released or going away."""
    st = _state.get(app_id) or {}
    task = st.get("lease_task")
    st["lease_task"] = None
    if task and not task.done():
        task.cancel()


async def release_now(app_id: str, harness_id: str) -> None:
    """Release a harness this caller woke for a ONE-SHOT job, immediately.

    The WS lane releases on WS close after a grace (session_closed); the
    ship proof (app_verify._browser_verify) has no socket to close. Without
    this the browser stayed awake until the operator's idle sweep — a 1Gi
    Chromium nobody was using — and then the cached upstream went STALE:
    ensure_awake returns st["url"] with no liveness check, so every later
    proof dialled a dead Service, got a connection error, and reported "not
    checked" for the life of the kernel process. The gate silently reverted
    to the unchecked behaviour after one sweep cycle.

    Same tear-down session_closed's delayed release performs, minus the
    grace: bill and release, drop the cached url so the next wake is cold,
    cancel any pending delayed release so it cannot fire on the next lease.
    Best-effort — a failed release is logged; the next ensure_awake still
    goes cold because the url is cleared regardless.
    """
    st = _state.get(app_id)
    if not st:
        return
    # NEVER under a live session. The Browser APP the user opens and the
    # ship proof share this slot — both are app_id "browser" on harness
    # "browser" — so a proof releasing here while the user's viewport WS was
    # open scaled the pod to 0 under them and left the ws count pointing at
    # a pod that no longer existed. session_closed
    # refuses to release while ws > 0; so does this.
    if int(st.get("ws") or 0) > 0:
        log.info("release_now %s/%s skipped — %d live session(s) hold it",
                 app_id, harness_id, int(st.get("ws") or 0))
        return
    # Same reasoning for one-shot runs: a second run holding the browser must
    # not have it torn down by the first one finishing.
    if int(st.get("runs") or 0) > 0:
        log.info("release_now %s/%s skipped — %d run(s) hold it",
                 app_id, harness_id, int(st.get("runs") or 0))
        return
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
    st["release_task"] = None
    st["lingering"] = False
    try:
        await _release_and_bill(app_id, harness_id)
    except Exception as e:  # noqa: BLE001 — see docstring
        log.warning("release_now %s/%s failed (url cleared anyway): %s",
                    app_id, harness_id, e)
    st["url"] = None


# ── LINGER: a one-shot caller keeps what it woke for a short idle window ─────
#
# release_now is the ONE release rule for one-shot callers (the delivery
# proof, check_page, the desktop render check — all through
# app_verify.browser_sidecar_post). A desktop being designed is written
# several times in a row, and each write to the worn desktop is checked in
# the browser; releasing after every check made each one pay a cold start.
# So a caller may LINGER instead: the same release, idle_s later, cancelled
# by the next one-shot that arrives in the window (it takes the hold over —
# lingering() tells it the hold is a one-shot's, not someone's session).
# Released early, at once, when a wake the PLAN refuses needs the room
# (release_lingering, called from ensure_awake). Never under a live session
# or run: release_now's own guards still apply when the window ends.
def linger(app_id: str, harness_id: str, idle_s: float) -> None:
    st = _state.get(app_id)
    if not st or not st.get("url"):
        return
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
    st["lingering"] = True

    async def _later():
        try:
            await asyncio.sleep(idle_s)
        except asyncio.CancelledError:
            return
        cur = _state.get(app_id)
        if not cur or not cur.get("lingering"):
            return
        cur["release_task"] = None      # this task: release_now must not cancel it
        await release_now(app_id, harness_id)

    st["release_task"] = asyncio.create_task(_later())


def lingering(app_id: str) -> bool:
    """Awake only because a one-shot caller is lingering on it."""
    st = _state.get(app_id) or {}
    return bool(st.get("lingering") and st.get("url")
                and int(st.get("ws") or 0) == 0 and int(st.get("runs") or 0) == 0)


async def release_lingering(*, except_app: str | None = None) -> list[str]:
    """Release, now, every harness held only by a linger — the plan needs the
    room. Returns the app ids released."""
    freed: list[str] = []
    for aid, st in list(_state.items()):
        if aid == except_app or not lingering(aid):
            continue
        await release_now(aid, st.get("harness") or aid)
        freed.append(aid)
    return freed


def forget_upstream(app_id: str) -> None:
    """Drop a cached upstream that just refused a connection, so the next
    ensure_awake goes COLD instead of returning the dead Service forever —
    the app-rt lane's re-stamp/clear-on-502 shape, for the
    harness lane's one-shot callers."""
    st = _state.get(app_id)
    if st:
        st["url"] = None


# The bridge prices vCPUs in 1..8 (adapter.op_harness_compute_charge,
# _VCPUS_MAX) and 400s outside that — and a 400 is not retried, so an
# out-of-range value would turn a billable release into an UNBILLED one.
# Clamp rather than drop: a pod that somehow rendered above 8 is billed
# at 8, never at nothing.
_VCPUS_BILL_MAX = 8


def _vcpus_of(value) -> int | None:
    """The operator's `vcpus` on a release, as the charge wants it: a whole
    number of vCPUs in 1.._VCPUS_BILL_MAX, or None when it reported none
    (the bridge then prices at 1). Never raises — a malformed value must
    not turn a billable release into an unbilled one."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    return min(n, _VCPUS_BILL_MAX)


#: The app whose profile the kernel keeps. One constant so the wake and release
#: hooks below cannot drift onto different ids.
_BROWSER_PROFILE_APP_ID = "browser"


async def _release_and_bill(app_id: str, harness_id: str) -> None:
    # BROWSER PROFILE: collect the login BEFORE anything is torn down. After the
    # release the pod is gone and its context with it, so this is the only moment
    # the state still exists. Opt-in, fail-soft, and it deliberately refuses to
    # seal an EMPTY state — an idle reap of a browser nobody signed into must not
    # overwrite a good profile with nothing.
    if app_id == _BROWSER_PROFILE_APP_ID:
        try:
            from server import brain_service
            from server.applications import browser_profile
            await browser_profile.save(brain_service.get_pool(),
                                       brain_service._entity_id)
        except Exception as e:  # noqa: BLE001
            log.info("browser profile save skipped: %r", e)
    # Stop claiming the lease BEFORE releasing: a beat that lands between the
    # release and the next tick would re-wake the pod we just paid to shut down.
    _stop_lease(app_id)
    bc = _bridge()
    if bc is None or not getattr(bc, "_connected", False):
        return

    async def _bill(env: dict) -> None:
        if not env.get("ok"):
            log.warning("harness %s release failed (app %s): %s",
                        harness_id, app_id, env.get("error"))
            return
        data = env.get("data") or {}
        dur = data.get("ready_duration_s")
        if data.get("billing") == "none":
            # The owner's own machine ran it (the local runtime): nothing is
            # billed, and nothing is left unbilled.
            log.info("harness %s released (app %s) after %.0fs on this machine", harness_id,
                     app_id, dur or 0)
            return
        # What the pod RAN AT. The operator reports the vCPU limit of the
        # window it just closed (the per-run bump annotation, else the limit
        # rendered into the Deployment); the bridge prices compute_s × vcpus.
        # Dropping it here billed every browser session at 1 vCPU
        # whatever the pod was actually allowed — the browser harness renders
        # at 2, so that would be half its compute unbilled.
        vcpus = _vcpus_of(data.get("vcpus"))
        if dur and dur > 0:
            # Same metered path as every other harness (harness.compute_charge).
            # run_id: the operator's window_id for a window its sweep already
            # closed out, so its own recovery charge and this one land on one
            # ledger row. None while the window is still open.
            from server.routes.compute import charge_harness_compute
            await charge_harness_compute(
                session_id=f"vscode:{app_id}", harness=harness_id,
                compute_s=float(dur), vcpus=vcpus,
                run_id=data.get("window_id"))
        log.info("harness %s released (app %s); billed %.0fs at %s vCPU",
                 harness_id, app_id, dur or 0, vcpus or 1)

    try:
        # Survives an operator roll (bridge_client.request_release_retrying —
        # the measured window and policy are documented there). Detached:
        # release_now runs inside a ship proof and this is also the path a
        # user's stop takes, so one attempt inline and the rest off the
        # request; the bill follows the release that lands, exactly once.
        from server.bridge.bridge_client import request_release_retrying
        env = await request_release_retrying(
            bc, {"harness": harness_id}, detach=True, on_result=_bill)
        if not env.get("retrying"):
            await _bill(env)
    except Exception as e:
        log.warning("harness %s release/bill failed: %s", harness_id, e)


async def ensure_awake(app_id: str, harness_id: str) -> str | None:
    """Wake the harness if needed; return its upstream host:port (the Service), or
    None if it couldn't be woken. Cancels any pending release — the user is back.

    SINGLE-FLIGHT. Opening a harness-backed app is not one request: its
    page pulls assets in parallel, and each one is proxied through here. A cold wake
    takes tens of seconds, so without a lock every one of those requests saw "not
    awake" and fired its own harness.wake — measured on one Host: FOUR concurrent
    wakes for one page load. The lock collapses a cold open to ONE wake; everyone
    else waits and gets the same upstream."""
    st = _st(app_id, harness_id)
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
        st["release_task"] = None
    if st.get("url"):
        return upstream_host(app_id)
    lock = _wake_locks.get(app_id)
    if lock is None:
        lock = _wake_locks[app_id] = asyncio.Lock()
    async with lock:
        # Re-check inside the lock: while we queued, the winner may have finished
        # the wake (the common case for every request after the first).
        if st.get("url"):
            return upstream_host(app_id)
        creds: dict | None = None
        # v127: every harness carries its NAC egress subject token, so the pod
        # can authenticate to this kernel's egress app lane and be routed by the
        # user's signed rules — that is what lets "the Browser goes out MY machine"
        # apply to a pod on another node. The operator merges wake-creds into the
        # per-harness Secret and references this key from the pod's env
        # (NAC_EGRESS_SUBJECT_TOKEN); it cannot compute the value itself, because the
        # HMAC secret lives in this entity's own database. Fail-soft: without it the
        # sidecar falls back to the node proxy and says so, which is the pre-v127
        # behaviour rather than a broken pod.
        try:
            from server import brain_service
            _p = brain_service.get_pool()      # Optional[asyncpg.Pool] — None before boot
            if _p is None:
                raise RuntimeError("brain pool not ready")
            creds = dict(creds or {})
            creds["egress-subject-token"] = await egress_subject_token(
                _p, f"harness:{harness_id}")
        except Exception as e:  # noqa: BLE001
            log.info("harness %s: no egress subject token (%r) — the pod will use "
                     "the node proxy and pinned egress rules will not apply", harness_id, e)
        res = await _wake(harness_id, creds=creds or None)
        if (not res.get("ok") and _OVER_PLAN_RE.search(str(res.get("error") or ""))
                and await release_lingering(except_app=app_id)):
            # The plan is full and part of it is a one-shot's linger (a
            # browser kept warm between desktop checks): that room is the
            # person's, so it goes first, and the wake is asked once more.
            log.info("harness %s: plan full — released lingering holds, retrying", harness_id)
            res = await _wake(harness_id, creds=creds or None)
        if not res.get("ok") or not res.get("url"):
            log.warning("harness %s wake failed: %s", harness_id, res.get("error"))
            # Kept for a caller that has to tell a person WHY (wake_refusal):
            # "no browser" because the plan is full reads very differently
            # from "no browser" because something broke.
            _note_wake_failure(st, res)
            return None
        _note_wake_failure(st, None)
        st["url"] = res["url"]
        if isinstance(res.get("image"), str) and res["image"]:
            _last_image[app_id] = res["image"]
        # BROWSER PROFILE: this was a COLD wake, so the sidecar came
        # up with a clean context. If the user keeps a profile, hand it over now —
        # before they are looking at anything, because restoring replaces the
        # context and takes the open page with it. Opt-in and fail-soft: a host
        # that never turned it on does nothing here.
        if app_id == _BROWSER_PROFILE_APP_ID:
            try:
                from server import brain_service
                from server.applications import browser_profile
                # The kept profile is the user's sign-ins, a vault key. A
                # turn whose type does not list it wakes a clean browser; the
                # next release cannot overwrite the kept copy with that clean
                # context (browser_profile.save refuses one it did not restore).
                from mastercode.runtime.turn_types import CURRENT_TURN_TYPE
                _tt = CURRENT_TURN_TYPE.get()
                if _tt is not None and (
                        _tt.get("status") != "active"
                        or browser_profile.VAULT_NAME not in (_tt.get("keys") or [])):
                    log.debug("browser profile restore skipped: turn type %r does not "
                              "hold %s", _tt.get("name"), browser_profile.VAULT_NAME)
                else:
                    await browser_profile.restore(brain_service.get_pool(),
                                                  brain_service._entity_id)
            except Exception as e:  # noqa: BLE001
                log.info("browser profile restore skipped: %r", e)
    return upstream_host(app_id)


# The operator's plan gate answers a wake it cannot fit with HTTP 402 and
# numbers only (controller features/harness/service.py _refuse_if_over_plan:
# needs_mi, headroom_mi, upgrade_to, pool_add_mi); the bridge relays it as
# "operator /harness/wake 402: {json}" — the same shape
# routes/review._plain_run_failure reads for a coding harness.
_OVER_PLAN_RE = re.compile(r"/harness/wake 402: (\{.*\})\s*$", re.S)
_OVER_PLAN_RE_ANY = re.compile(r"\b402\b.*capability_over_plan", re.S)


def _note_wake_failure(st: dict, res: dict | None) -> None:
    """Keep the last failed wake's error and status on the lane's state, or
    clear them (res None) when a wake lands."""
    if res is None:
        st.pop("wake_error", None)
        st.pop("wake_code", None)
        return
    st["wake_error"] = str(res.get("error") or "")[:2000]
    st["wake_code"] = res.get("code")


def _over_plan(app_id: str) -> dict | None:
    """The 402 payload of the last wake's refusal, when the plan or this
    machine had no room ({error: capability_over_plan, needs_mi, ...})."""
    err = (_state.get(app_id) or {}).get("wake_error") or ""
    m = _OVER_PLAN_RE.search(err)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    d = d.get("detail") if isinstance(d, dict) and isinstance(d.get("detail"), dict) else d
    if not isinstance(d, dict) or d.get("error") != "capability_over_plan":
        return None
    return d


def wake_refusal(app_id: str) -> dict | None:
    """Why the last wake of this app's harness failed, when the plan or this
    machine had no room: {needs_mi, headroom_mi, upgrade_to, pool_add_mi,
    scope}; scope "machine" is this machine's local runtime, anything else a
    plan. None when the last wake did not fail that way (or never failed)."""
    d = _over_plan(app_id)
    if d is None:
        return None
    return {k: d.get(k) for k in ("needs_mi", "headroom_mi", "upgrade_to", "pool_add_mi", "scope")}


# Refusals whose text the local runtime writes for the owner to read: a short
# disk (507: the floor, and each app's data, largest first) and a machine that
# started or runs too many containers (429). The bridge relays an operator's
# refusal as "operator /<path> <code>: <body>", which is not written for a
# person, so only the runtime's own text is passed on.
_PEOPLE_CODES = frozenset({429, 507})

# Control characters, and the ones that reorder text: the same set Settings'
# oneLine() replaces before it shows what the Host says.
_UNSHOWABLE_RE = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_REFUSAL_MAX = 1000


def one_line(text: str, limit: int = _REFUSAL_MAX) -> str:
    """Text made one line and plain: every control or reordering character a
    space, runs of spaces one, at most `limit` characters. The refusal's text
    can carry app ids, and is shown as text only."""
    s = re.sub(r" {2,}", " ", _UNSHOWABLE_RE.sub(" ", str(text or ""))).strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def wake_refused(app_id: str) -> dict | None:
    """Why the last wake of this app's container was refused, in words the
    owner and the Host can act on: {"code": 402|429|507, "message": one line}.
    402 is a plan or this machine with no room for it (the sentence is
    composed here from the numbers, as installer.AppRuntimeOverPlan does);
    429 and 507 carry the local runtime's own text. None when the last wake
    did not fail that way (or never failed): still starting, or broken."""
    st = _state.get(app_id) or {}
    err = str(st.get("wake_error") or "")
    if not err:
        return None
    d = _over_plan(app_id)
    if d is not None:
        need, free = d.get("needs_mi"), d.get("headroom_mi")
        where = "this machine" if d.get("scope") == "machine" else "this plan"
        if d.get("reason") == "pressure":
            text = (f"{app_id} was not started: {where}'s memory is under pressure right now"
                    + (f" and it needs {need} MiB" if need is not None else ""))
        else:
            text = (f"{where} has no room for {app_id} right now"
                    + (f": it needs {need} MiB and {free} MiB are free" if need is not None else ""))
        if d.get("upgrade_to"):
            text += f" (the {d['upgrade_to']} plan has room for it)"
        return {"code": 402, "message": one_line(text)}
    try:
        code = int(st.get("wake_code"))
    except (TypeError, ValueError):
        return None
    if code in _PEOPLE_CODES and not err.startswith("operator /"):
        return {"code": code, "message": one_line(err)}
    return None


def wake_refused_words(app_id: str) -> str:
    """": <why>" for a caller's sentence about a wake that did not land, or
    "" when the last wake was not refused that way (wake_refused)."""
    why = wake_refused(app_id)
    return f": {why['message']}" if why else ""


def upstream_host(app_id: str) -> str | None:
    """The harness Service host:port from the last wake (e.g.
    harness-browser.<ns>.svc.cluster.local:<port>), or None if not awake."""
    url = (_state.get(app_id) or {}).get("url")
    if not url:
        return None
    return url.split("://", 1)[-1].rstrip("/")


# ── App runtime (helper) lane ───────────────────────────────────────────────
#
# Same 0<->1 machinery as the harness lane, addressed by `app_rt` instead of
# `harness` at the bridge. Deliberately simpler: no wake-creds (the helper
# takes none), no WS lease (no WS surface — the open-window lifecycle holds
# it: wake on first helper request, release on window close with the same
# grace, operator idle-sweep as the backstop). Billing rides the same
# metered path with an `app-rt:` session prefix.

_APP_RT_MARK = "app-rt"

# The open-time descriptor repair hook. REGISTERED by
# installer.py at import time rather than imported here: the two modules
# already import each other function-locally in opposite directions, and a
# registration is the shape that can never become a cycle. None = repair
# unavailable (a bare test import), which degrades to a single wake attempt.
_descriptor_resync = None


def set_descriptor_resync(fn) -> None:
    global _descriptor_resync
    _descriptor_resync = fn


async def _wake_app(app_id: str, creds: dict | None = None) -> dict:
    """Wake (or re-stamp) an app's runtime pod. `creds` go with the wake for the
    operator to write into the app's creds Secret (`app-rt-<id>-creds`) — on a
    COLD wake only, answering `creds_applied: true|false`.

    A messaging connector's pending capability rides here (nac_connector):
    while one is pending it is sent with
    every wake, cold or warm, and the kernel switches to it only when the
    operator answers creds_applied: true. An absent field is not true (an
    operator without that change), so it stays pending. Nothing is minted here:
    the resident heartbeat calls this every few minutes, and a capability is
    minted only when a pairing starts.

    Wakes that carry a capability run one at a time per app
    (nac_connector.creds_wake_lock), each reading the pending value under the
    lock: the heartbeat, the lease re-stamp and a delivery's cold wake can
    otherwise be in flight together, and with two different values in flight
    the operator could write them in either order."""
    bc = _bridge()
    if bc is None or not getattr(bc, "_connected", False):
        return {"ok": False, "error": "bridge not connected"}
    try:
        from server.network import nac_connector
        carries = nac_connector.pending_creds(app_id) is not None
    except Exception as e:  # noqa: BLE001 — a wake never fails on the connector lane
        log.warning("app-rt %s: connector creds unavailable: %r", app_id, e)
        nac_connector, carries = None, False
    if not carries:
        return await _wake_app_once(bc, app_id, creds, None)
    async with nac_connector.creds_wake_lock(app_id):
        return await _wake_app_once(bc, app_id, creds, nac_connector)


async def _wake_app_once(bc, app_id: str, creds: dict | None, nac_connector) -> dict:
    """One harness.wake for an app (see _wake_app). With `nac_connector`, the
    pending capability is read HERE, under its lock, and its answer handled
    before the lock is let go."""
    params: dict = {"app_rt": app_id}
    sent_cap = None
    pending = None
    if nac_connector is not None:
        try:
            pending = nac_connector.pending_creds(app_id)
        except Exception as e:  # noqa: BLE001 — a wake never fails on the connector lane
            log.warning("app-rt %s: connector creds unavailable: %r", app_id, e)
    if pending is not None:
        creds = {**(creds or {}), **pending[0]}
        sent_cap = pending[1]
    if creds:
        params["creds"] = creds
    try:
        env = await bc.request_full("harness.wake", params)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not env.get("ok"):
        return {"ok": False, "error": env.get("error") or "wake failed", "code": env.get("code")}
    # Same as _wake: a pending release retry for this pod must yield to us.
    from server.bridge.bridge_client import note_harness_wake
    note_harness_wake({"app_rt": app_id})
    data = env.get("data") or {}
    if sent_cap is not None:
        try:
            await nac_connector.on_wake_answer(app_id, sent_cap, data.get("creds_applied"))
        except Exception as e:  # noqa: BLE001 — stays pending; the next cold wake carries it
            log.warning("app-rt %s: connector capability not switched: %r", app_id, e)
    return {"ok": True, **data}


async def ensure_awake_app(app_id: str) -> str | None:
    """Wake the app's runtime pod if needed; return its upstream host:port
    (the POD IP from the operator's wake — no Service exists),
    or None. Cancels a pending release — the user is back.

    REQUEST-TIME LEASE: the app lane has no WS to heartbeat
    from, so the traffic itself is the holder — when the cached wake is
    older than the heartbeat interval, this fires an idempotent warm wake
    (a pure last-wake annotation re-stamp operator-side, nothing restarts)
    before serving. Without this, a window open past the 900s sweep grace
    had its pod reaped mid-use: every later fetch dialed the dead pod IP.

    Open-time descriptor repair: a wake refused with the
    operator's no-descriptor hint triggers ONE descriptor re-sync (the
    install-time write is best-effort, and for a helper app the server half
    is its whole function) and ONE retry."""
    st = _st(app_id, _APP_RT_MARK)
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
        st["release_task"] = None
    from server.applications import local_runtime
    if local_runtime.exporting_app_data(app_id):
        # Paused while a backup copies its data: the cached address below
        # would be dialled straight into a frozen process, and the request
        # would wait until the copy ends. Not a failed wake (no backoff, the
        # address kept): the caller answers still starting (proxy._not_awake).
        return None
    if st.get("url"):
        now = time.monotonic()
        if now - (st.get("stamped_at") or 0) > HARNESS_LEASE_HEARTBEAT_S:
            res = await _wake_app(app_id)
            if res.get("ok"):
                st["stamped_at"] = now
                if res.get("url"):
                    st["url"] = res["url"]  # pod may have moved across a reap
            else:
                # The pod may be gone (swept, or the operator restarted).
                # Drop the cached url so the cold path below re-establishes
                # it instead of serving 502s forever.
                log.warning("app-rt %s lease re-stamp failed (%s) — "
                            "re-waking cold", app_id, res.get("error"))
                st["url"] = None
        if st.get("url"):
            return upstream_host(app_id)
    # COLD PATH — single-flight per app, like ensure_awake for harnesses.
    # MEASURED (one Host): with the pod not yet Ready,
    # every asset request of the app window fired its OWN cold wake, each
    # blocking the operator for the descriptor's wait — dozens of 504s a
    # minute for one open window. One wake in flight per app; the rest wait
    # on it and share the answer. A failed cold wake is then remembered for
    # APP_RT_WAKE_BACKOFF_S so the next request answers "starting" at once
    # instead of queueing another wait (the resident hold, when on, keeps
    # re-waking on its own cadence).
    lock = _wake_locks.get(app_id)
    if lock is None:
        lock = _wake_locks[app_id] = asyncio.Lock()
    async with lock:
        st = _st(app_id, _APP_RT_MARK)
        if st.get("url"):
            return upstream_host(app_id)          # a waiter's wake landed
        failed_at = st.get("cold_failed_at")
        if failed_at and time.monotonic() - failed_at < APP_RT_WAKE_BACKOFF_S:
            return None
        res = await _wake_app(app_id)
        if not res.get("ok") and "appRuntimes" in str(res.get("error") or ""):
            if _descriptor_resync is not None:
                try:
                    sent = await _descriptor_resync(app_id, True)
                    log.info("app-rt %s: descriptor re-sync at open (sent=%s) — "
                             "retrying", app_id, sent)
                except Exception as e:
                    log.warning("app-rt %s: open-time descriptor re-sync failed: %s",
                                app_id, e)
            res = await _wake_app(app_id)
        if (not res.get("ok") and _OVER_PLAN_RE_ANY.search(str(res.get("error") or ""))
                and await release_lingering(except_app=app_id)):
            log.info("app-rt %s: plan full — released lingering holds, retrying", app_id)
            res = await _wake_app(app_id)
        if not res.get("ok") or not res.get("url"):
            log.warning("app-rt %s wake failed: %s", app_id, res.get("error"))
            st["cold_failed_at"] = time.monotonic()
            # Kept, as the harness lane keeps it, for whoever asked for the
            # wake to say why (wake_refused); read through the backoff above.
            _note_wake_failure(st, res)
            return None
        _note_wake_failure(st, None)
        st["url"] = res["url"]
        st["stamped_at"] = time.monotonic()
        st["cold_failed_at"] = None
        # A fresh pod is a fresh process: the exit the previous pod's wrapper
        # reported (runtime_log.append_runtime_log) belongs to it, not this one.
        st.pop("last_exit", None)
        return upstream_host(app_id)


def forget_app(app_id: str) -> None:
    """Drop ALL cached lane state for an app: called on
    uninstall so a reinstall — possibly with a different runtime kind —
    never inherits a stale mark/url, and a pending release task dies with
    the state it would have mutated."""
    st = _state.pop(app_id, None)
    if st:
        for key in ("release_task", "lease_task"):
            t = st.get(key)
            if t and not t.done():
                t.cancel()


async def ensure_awake_app_fresh(app_id: str) -> str | None:
    """ensure_awake_app, with the pod's address confirmed by the operator NOW
    rather than taken from a wake up to HARNESS_LEASE_HEARTBEAT_S old. For a
    caller that sends a credential to that address (an app's own MCP server:
    the app's token, the Host's images; mcp_connections._use_app): a pod reaped
    since the last wake gives its IP back, and another pod could hold it by
    the time the cached address is dialled. Ages the cached stamp so
    ensure_awake_app takes its warm path, a pure re-stamp that returns the
    pod's current address (nothing restarts)."""
    st = _state.get(app_id)
    if st and st.get("harness") == _APP_RT_MARK:
        # Older than any heartbeat, whatever the monotonic clock reads.
        st["stamped_at"] = float("-inf")
    return await ensure_awake_app(app_id)


def invalidate_app_url(app_id: str) -> None:
    """Drop the cached upstream after a connect failure: a
    reaped/moved pod must cost one failed request + a re-wake, never a
    permanently broken window."""
    st = _state.get(app_id)
    if st and st.get("harness") == _APP_RT_MARK:
        st["url"] = None


async def _release_and_bill_app(app_id: str) -> None:
    bc = _bridge()
    if bc is None or not getattr(bc, "_connected", False):
        return

    async def _bill(env: dict) -> None:
        if not env.get("ok"):
            log.warning("app-rt %s release failed: %s", app_id, env.get("error"))
            return
        data = env.get("data") or {}
        dur = data.get("ready_duration_s")
        if data.get("billing") == "none":
            log.info("app-rt %s released after %.0fs on this machine", app_id, dur or 0)
            return
        # What the pod RAN AT: an app may declare a CPU ceiling above 1
        # (manifest resources.vcpus), and the operator reports the
        # ceiling of the window it closed, as it does for a harness. Dropping
        # it would bill a 4-vCPU render window at 1.
        vcpus = _vcpus_of(data.get("vcpus"))
        if dur and dur > 0:
            from server.routes.compute import charge_harness_compute
            # window_id, when the operator reports one, is the idempotency key
            # for a window its own sweep already closed out. The
            # operator bills such a window itself when no caller ever returns —
            # it has to, because the reason to return lives in `_state`, which
            # this process loses on every restart. Passing the key back means
            # the two charges are one row, not two. None = the window is still
            # open, only we can bill it, and charge_harness_compute mints its
            # own key exactly as before.
            await charge_harness_compute(
                session_id=f"app-rt:{app_id}", harness=_APP_RT_MARK,
                compute_s=float(dur), vcpus=vcpus, run_id=data.get("window_id"))
        log.info("app-rt %s released; billed %.0fs at %s vCPU", app_id, dur or 0, vcpus or 1)

    try:
        # Survives an operator roll (bridge_client.request_release_retrying).
        # Detached: app_release is the user's stop and recycle_app the
        # re-install click — neither may sit out the roll's ~45 s window.
        from server.bridge.bridge_client import request_release_retrying
        env = await request_release_retrying(
            bc, {"app_rt": app_id}, detach=True, on_result=_bill)
        if not env.get("retrying"):
            await _bill(env)
    except Exception as e:
        log.warning("app-rt %s release/bill failed: %s", app_id, e)


def kernel_url_for_pods() -> str:
    """The URL an app's OWN runtime pod dials to reach this kernel — the entity
    pod's StatefulSet DNS name on :8800 (kernel_routable_base). Empty when
    ENTITY_ID is unset (a bare
    dev process has no pod to be reached from)."""
    addr = _entity_broker_addr()
    return f"http://{addr}" if addr else ""


async def app_source_token(pool, app_id: str) -> str:
    """The capability an app's runtime pod presents to pull its install dir
    (routes/applications.py:app_source). DERIVED, never stored: an HMAC over
    the app id under the kernel's app-token secret, so the descriptor write
    and the verify side compute the same value with no row between them. A
    leaked token buys exactly one thing — a copy of that app's own source,
    which the user built and can read in the Workbench anyway."""
    import hashlib
    import hmac
    from server.applications import tokens
    secret = await tokens.get_secret(pool)
    return hmac.new(secret, f"app-rt-source:{app_id}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


async def check_app_source_token(pool, app_id: str, presented: str) -> bool:
    if not presented or not isinstance(presented, str):
        return False
    want = await app_source_token(pool, app_id)
    return _secrets.compare_digest(want.encode("utf-8"), presented.encode("utf-8"))


async def egress_subject_token(pool, subject: str) -> str:
    """The capability an off-pod caller presents to the NAC egress APP LANE
    (server/network/nac_egress.py, :13129) in order to egress AS that subject —
    'app:<app_id>' or 'harness:<harness_id>'.

    Same derivation shape as app_source_token — an HMAC under the kernel's app-token
    secret (tokens.get_secret), computed identically on both sides with no row in
    between — but under a DIFFERENT DOMAIN PREFIX, and that separation is the point
    rather than tidiness. A leaked source token buys a copy of an app's own source,
    which the user built and can read in the Workbench anyway. A leaked egress token
    buys the use of the USER'S OWN HOME ADDRESS for that subject's traffic — a
    different order of loss — so the two must never be replayable for each other."""
    import hashlib
    import hmac
    from server.applications import tokens
    secret = await tokens.get_secret(pool)
    return hmac.new(secret, f"nac-egress-subject:{subject}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


async def check_egress_subject_token(pool, subject: str, presented: str) -> bool:
    if not subject or not presented or not isinstance(presented, str):
        return False
    want = await egress_subject_token(pool, subject)
    return _secrets.compare_digest(want.encode("utf-8"), presented.encode("utf-8"))


def app_runtime_state(app_id: str) -> dict:
    """TRUTH about an app's own runtime pod, from the lane's own state:
    {running, url, since}. `running` is True only while this kernel holds a
    live wake for the pod (a cached pod URL that the traffic-lease keeps
    fresh); `url` is that pod's upstream or None. Nothing is inferred from a
    manifest or a file on disk — a landed app that was never opened reports
    running=False, which is what the build/delivery envelope must carry so
    the host can never call a static page a live app (seen)."""
    st = _state.get(app_id) or {}
    if st.get("harness") != _APP_RT_MARK or not st.get("url"):
        return {"running": False, "url": None, "since": None}
    return {"running": True, "url": upstream_host(app_id),
            "since": st.get("stamped_at")}


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def app_hold(app_id: str) -> None:
    """A RESIDENT process app is a holder with no window: heartbeat its lease
    on the same cadence a live WS session uses (HARNESS_LEASE_HEARTBEAT_S against the
    operator's 900s sweep grace) until app_release stops it. The traffic-lease
    in ensure_awake_app covers an app someone is USING; this covers one the
    user switched on and walked away from — a bot, a poller, a trading loop."""
    st = _st(app_id, _APP_RT_MARK)
    st["resident"] = True
    task = st.get("lease_task")
    if task and not task.done():
        return

    async def _beat() -> None:
        while True:
            cur = _state.get(app_id) or {}
            # Two cadences. Holding a LIVE wake: the lease heartbeat, which
            # is a pure re-stamp against the sweep. Holding NO wake yet (the
            # wake outran its wait on a slow first boot, the bridge was down
            # at kernel boot, the operator 504'd): re-wake with a doubling
            # wait (resident_retry_wait) until the pod answers. The resident
            # flag is the only thing that ends this — app_release clears it.
            if cur.get("url"):
                wait = float(HARNESS_LEASE_HEARTBEAT_S)
            else:
                wait = resident_retry_wait(int(cur.get("rewake_failures") or 0))
                cur["rewake_next_s"] = wait
            await asyncio.sleep(wait)
            cur = _state.get(app_id) or {}
            if not cur.get("resident"):
                return
            had_url = bool(cur.get("url"))
            res = await _wake_app(app_id)
            if res.get("ok") and (had_url or res.get("url")):
                cur["stamped_at"] = time.monotonic()
                cur["rewake_failures"] = 0
                cur.pop("rewake_next_s", None)
                if res.get("url"):
                    cur["url"] = res["url"]
                    cur.setdefault("started_wall", time.time())
                    if not had_url:
                        log.info("app-rt %s resident: pod is up at %s",
                                 app_id, res["url"])
            else:
                if not had_url:
                    cur["rewake_failures"] = int(cur.get("rewake_failures") or 0) + 1
                log.warning("app-rt %s resident %s failed: %s%s",
                            app_id, "heartbeat" if had_url else "re-wake",
                            res.get("error"),
                            "" if had_url else
                            f" — next try in {resident_retry_wait(cur['rewake_failures']):.0f}s")

    st["lease_task"] = asyncio.create_task(_beat())


async def app_release(app_id: str) -> bool:
    """Stop an app's runtime pod NOW — the user's or the host's explicit
    stop (resident or not). Drops the hold, cancels any pending graceful
    release, clears the cached url, then releases + bills. True if the lane
    held a live wake for it."""
    st = _state.get(app_id)
    if not st or st.get("harness") != _APP_RT_MARK:
        return False
    st["resident"] = False
    _stop_lease(app_id)
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
        st["release_task"] = None
    had = bool(st.get("url"))
    st["url"] = None
    # A deliberate stop ends the process we were tracking: its exit and its
    # last pushed numbers belong to the pod that is going away.
    st.pop("last_exit", None)
    st.pop("runtime_stats", None)
    await _release_and_bill_app(app_id)
    return had


async def recycle_app(app_id: str) -> bool:
    """The app's SOURCE on disk changed (a re-install) — drop the running pod
    so the next wake creates one that fetches it. True if a live wake was held.

    A runtime pod pulls the app's install dir ONCE, in its init container, at
    creation (sidecars/app-runtime-python/fetch.py). So a re-install reaches a
    RUNNING pod only if that pod is replaced. MEASURED
    (one Host): the user pressed "Install as my app", the kernel installed
    v0.4.0 and updated the descriptor, and the pod kept serving v0.3.0 source
    with no error anywhere — the operator's template converge covers a version
    CHANGE; this covers the same-version re-ship it cannot see.

    The resident hold is deliberately KEPT: a resident app the user switched
    on stays on across a re-install, and the hold's next beat brings the fresh
    pod up. A window-scoped app comes back on its next request."""
    st = _state.get(app_id)
    if not st or st.get("harness") != _APP_RT_MARK or not st.get("url"):
        return False
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
        st["release_task"] = None
    st["url"] = None                    # the new pod gets a new address
    st["cold_failed_at"] = None         # do not inherit a pre-install backoff
    st.pop("started_wall", None)
    # The old pod's exit, numbers and identity die with it: a re-ship's proof
    # must never read the previous generation's crash.
    st.pop("last_exit", None)
    st.pop("runtime_stats", None)
    st.pop("runtime_pod", None)
    await _release_and_bill_app(app_id)
    log.info("app-rt %s: pod released for a re-install — the next wake fetches "
             "the new source (resident=%s)", app_id, bool(st.get("resident")))
    return True


def _shell_connected() -> bool | None:
    """Is any desktop tab connected to this kernel (server.main's /ws
    sockets)? None when that cannot be known (no server.main: a test, a
    tool)."""
    m = sys.modules.get("server.main")
    conns = getattr(m, "_ws_connections", None) if m is not None else None
    if conns is None:
        return None
    return len(conns) > 0


# Each open window of an app-runtime app says it is still there: the shell
# posts a beat for it every WINDOW_BEAT_S (dashboard/base/js/app-runner.js,
# POST /api/applications/{id}/window-beat). A window whose beats stop for
# WINDOW_STALE_S is gone — the page was reloaded, the tab closed, the browser
# crashed — and no longer holds the pod. Three beats of slack covers a
# background tab's throttled timers (once a minute at most).
WINDOW_BEAT_S = int(os.environ.get("APP_RT_WINDOW_BEAT_S", "60"))
WINDOW_STALE_S = int(os.environ.get("APP_RT_WINDOW_STALE_S", str(3 * WINDOW_BEAT_S)))


def _live_windows(st: dict) -> dict:
    """The windows of this app whose client beat within WINDOW_STALE_S;
    the others are dropped from the state."""
    wins = st.get("windows")
    if not isinstance(wins, dict):
        wins = st["windows"] = {}
    now = time.monotonic()
    for wid in [w for w, seen in wins.items() if now - seen > WINDOW_STALE_S]:
        wins.pop(wid, None)
    return wins


def app_window_opened(app_id: str, window_id: str) -> None:
    """An app-runtime app's window opened (routes/applications.py launch).

    AN OPEN WINDOW HOLDS THE POD, the way an open harness socket holds its
    harness (_start_lease). The lease used to be re-stamped only by the
    window's own requests (ensure_awake_app), so a window someone was only
    WATCHING — a dashboard, a chart, a log tail drawn once — had its pod
    swept 900 s after the last request. Now a
    beat re-stamps it every HARNESS_LEASE_HEARTBEAT_S while a window is open.

    It ends on liveness, never on a clock: the window's close
    (app_window_closed); its client's own beats stopping (app_window_beat,
    WINDOW_STALE_S) — a reload, a closed tab or a crashed browser sends no
    close, and each launch mints a new window id, so without the per-window
    beat the old id held the pod forever; and no
    desktop tab connected to this kernel at all (_shell_connected)."""
    st = _st(app_id, _APP_RT_MARK)
    if st.get("harness") != _APP_RT_MARK:
        return
    _live_windows(st)[str(window_id)] = time.monotonic()
    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()                     # re-opened inside the grace
        st["release_task"] = None
    task = st.get("window_task")
    if task and not task.done():
        return

    async def _beat() -> None:
        while True:
            await asyncio.sleep(HARNESS_LEASE_HEARTBEAT_S)
            cur = _state.get(app_id) or {}
            if not cur.get("windows"):
                return
            if _shell_connected() is False:
                log.info("app-rt %s: %d window(s) open but no desktop tab is connected — "
                         "the window hold ends", app_id, len(cur.get("windows") or ()))
                cur["windows"] = {}
                cur["window_task"] = None
                app_window_closed(app_id)
                return
            if not _live_windows(cur):
                log.info("app-rt %s: no open window has beaten for %ss — the window hold "
                         "ends", app_id, WINDOW_STALE_S)
                cur["window_task"] = None
                app_window_closed(app_id)
                return
            if cur.get("resident") or not cur.get("url"):
                continue                # the resident hold beats; nothing awake yet
            res = await _wake_app(app_id)
            if res.get("ok"):
                cur["stamped_at"] = time.monotonic()
                if res.get("url"):
                    cur["url"] = res["url"]
            else:
                log.warning("app-rt %s window hold re-stamp failed: %s", app_id,
                            res.get("error"))

    try:
        st["window_task"] = asyncio.get_running_loop().create_task(_beat())
    except RuntimeError:
        st["window_task"] = None


def app_window_beat(app_id: str, window_id: str) -> bool:
    """A window's client says it is still open (the shell, every
    WINDOW_BEAT_S). A window this kernel does not know (it restarted, or the
    beat outlived a stale drop) is held again. True when it is held."""
    st = _state.get(app_id)
    if st is not None and st.get("harness") != _APP_RT_MARK:
        return False
    if st is not None and str(window_id) in (st.get("closed_windows") or ()):
        return False                    # a beat already in flight when its close landed
    if st is None or str(window_id) not in (st.get("windows") or {}):
        app_window_opened(app_id, window_id)
        return True
    _live_windows(st)[str(window_id)] = time.monotonic()
    return True


def app_window_closed(app_id: str, window_id: str | None = None) -> None:
    """The app's window closed (routes/applications.py close hook), or a
    one-shot caller that woke it is done (window_id None: the delivery
    proof). While another window of the app is still open nothing is
    released. Otherwise schedule the release after the same grace the WS lane
    uses — a re-open within the grace cancels it, so a refresh never churns
    the pod. The operator idle-sweep remains the backstop for a close that
    never fires."""
    st = _state.get(app_id)
    if not st or st.get("harness") != _APP_RT_MARK:
        return
    wins = _live_windows(st)
    if window_id is not None:
        wins.pop(str(window_id), None)
        # A beat that was already on its way must not hold this id again.
        closed = st.setdefault("closed_windows", [])
        closed.append(str(window_id))
        del closed[:-64]
    if wins:
        return                          # another live window still holds it
    wt = st.get("window_task")
    if wt and not wt.done() and wt is not _current_task():
        wt.cancel()
    st["window_task"] = None
    if not st.get("url"):
        return
    if st.get("resident"):
        # A resident process app: the window closing is not a stop signal —
        # the user switched it on and only the user (or the host) switches
        # it off (app_release). Same rule as Supervisor.close_window for the
        # in-entity variant.
        return

    async def _delayed_release():
        try:
            await asyncio.sleep(_RELEASE_GRACE_S)
        except asyncio.CancelledError:
            return
        # Post-sleep re-check (mirrors the harness variant): a
        # re-open that landed during the sleep repopulated state via
        # ensure_awake_app's cancel — but a cancel that races the release
        # await must not leave a released pod's url cached. Clear the url
        # FIRST, then release: the worst race is now a spurious re-wake,
        # never a dead cached IP.
        cur = _state.get(app_id)
        if cur:
            if cur.get("resident"):
                return  # switched on during the grace — it stays up
            if _live_windows(cur):
                return  # a window opened during the grace — it holds the pod
            cur["url"] = None
            cur["release_task"] = None
        await _release_and_bill_app(app_id)

    rt = st.get("release_task")
    if rt and not rt.done():
        rt.cancel()
    st["release_task"] = asyncio.create_task(_delayed_release())


async def session_opened(app_id: str, harness_id: str) -> str | None:
    """A live app WS opened — ensure awake and count it. Returns the upstream
    host:port to proxy to, or None if the wake failed (nothing is counted then)."""
    host = await ensure_awake(app_id, harness_id)
    if host is None:
        return None
    st = _st(app_id, harness_id)
    st["ws"] = st.get("ws", 0) + 1
    # A live session is a holder — start claiming the lease so the sweep
    # cannot reap the pod out from under an open tab.
    _start_lease(app_id, harness_id)
    return host


def session_closed(app_id: str, harness_id: str) -> None:
    """An app WS closed. When the last one closes, schedule a release after the
    grace window (a WS reopened within the grace cancels it — refresh/reconnect)."""
    st = _state.get(app_id)
    if not st:
        return
    st["ws"] = max(0, st.get("ws", 0) - 1)
    if st["ws"] > 0:
        return

    async def _delayed_release():
        try:
            await asyncio.sleep(_RELEASE_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _state.get(app_id) or {}
        if cur.get("ws", 0) > 0:
            return  # a new session opened during the grace
        if int(cur.get("runs") or 0) > 0:
            # A run is still holding it. The runs guard went on release_now
            # only, so closing a SPECTATOR tab while an agent run was working
            # scaled the pod to 0 under that run once the grace expired — the
            # same failure this change removes, from the other direction. The
            # run's own release_run is what sleeps it when it finishes.
            return
        await _release_and_bill(app_id, harness_id)
        cur = _state.get(app_id)
        if cur:
            cur["url"] = None
            cur["release_task"] = None

    st["release_task"] = asyncio.create_task(_delayed_release())
