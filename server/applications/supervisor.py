"""
Process supervisor for sidecar apps.

When the dashboard launches an app whose manifest declares
`runtime.kind == process`, the supervisor:

  1. Picks a free localhost port.
  2. Spawns the manifest's `command` with env:
        <port_env>          = the picked port
        PRIVATAE_APP_ID        = the app id
        CSERIES_KERNEL_URL    = the kernel loopback base (server/loopback.py)

     A sidecar holds NO standing credential. This block used to promise a
     `CSERIES_APP_TOKEN` env var carrying "the per-window token" — nothing has
     ever injected it (see the env dict built below), and the false promise
     propagated into the generated capabilities doc. Authentication is
     PER REQUEST instead: the browser's window token rides in on
     X-Application-Token (not a hop-by-hop header, so the proxy forwards it),
     and the sidecar re-presents that same header on its calls back to
     CSERIES_KERNEL_URL. See the RunningSidecar docstring below.
  3. Polls `ready_path` until the sidecar reports healthy or
     `ready_timeout_seconds` elapses (kill the process on timeout).
  4. Records the (window_id -> RunningSidecar) mapping so the reverse
     proxy + close handler can find it.

Termination:
  - terminate(window_id): SIGTERM, wait up to 5s, SIGKILL on hang
  - kill_all_for_app(app_id): same, all windows of that app
  - The supervisor surveys at intervals; dead processes have their
    proxy mounts torn down

Crash handling: a sidecar that exits before its window closes is
flagged; the next request through the proxy returns 503. The window
itself stays open (the dashboard surface decides whether to reload).

This supervisor lives entirely in-process. There's no separate
supervisor daemon yet — same Python event loop as the kernel API.
For a single user, this is fine. Multi-process / per-user
isolation is not built here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import aiohttp

from server.applications.hardening import build_preexec
from server.applications.manifest import AppManifest
from server.applications.registry import InstalledApp
from server.applications.storage import workspace_dir as _workspace_dir
# Where installed apps live — the PodRuntime view names the same dir the
# in-entity variant records, so callers see one shape.
_INSTALL_ROOT = Path("/var/lib/cseries/apps")   # == installer.INSTALL_ROOT
# In-pod dial target for sidecars calling back into the kernel — 127.0.0.1
# doesn't connect in k8s pods (the kernel's :: bind is v6-only there).
from server.loopback import KERNEL_LOOPBACK_BASE

log = logging.getLogger("privatae.applications.supervisor")

# Env the supervisor hands an in-container process app, and where its output
# goes. CSERIES_DATA_DIR so an app never has to guess
# the path it may write; CSERIES_LOG_FILE so what it prints survives — the
# bootstrap that killed one Host's app printed its failure into a pipe
# nobody read, and the only trace left was a one-line phase file the app
# happened to write itself.
ENV_DATA_DIR = "CSERIES_DATA_DIR"
ENV_LOG_FILE = "CSERIES_LOG_FILE"
LOG_SUBDIR = "logs"
LOG_FILENAME = "sidecar.log"
# Rotation happens at SPAWN (a file past this size is moved to .1 before the
# next run opens it). Within one run the file grows with the process: no
# per-process rlimit applies to sidecars (hardening.py), so a tight print loop
# is bounded only by the data volume until the app restarts.
LOG_ROTATE_BYTES = 5 * 1024 * 1024
TAIL_BYTES = 4096

# Kernel env an in-container app never inherits. CSERIES_SIDECAR_ROLE=harness is
# what makes cli/cseries_sidecar.py serve /workspace/load, /workspace/export,
# /session/export and /run/progress; it belongs to harness images only, and a
# process started here runs against the kernel's own /var/lib/cseries/projects.
# The Host image does not set it; this keeps a future render that did from
# reaching the apps.
_NOT_INHERITED_ENV = frozenset({"CSERIES_SIDECAR_ROLE"})


def _child_env(overrides: dict[str, str]) -> dict[str, str]:
    """The env for an in-container app: the kernel's own, minus
    _NOT_INHERITED_ENV, plus `overrides`."""
    env = {k: v for k, v in os.environ.items() if k not in _NOT_INHERITED_ENV}
    env.update(overrides)
    return env


def _open_log(path: Path):
    """The file the sidecar's stdout+stderr are redirected to. Fail-soft: an
    unwritable data dir returns None and the caller falls back to pipes, so a
    storage problem can never stop an app from starting."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_ROTATE_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except FileNotFoundError:
            pass
        return open(path, "ab")
    except OSError as e:
        log.warning("sidecar log %s not writable (%s) — falling back to pipes", path, e)
        return None


def tail_text(path: Path | None, n_bytes: int = TAIL_BYTES) -> str:
    """The last `n_bytes` of a text file, decoded leniently; '' when absent.
    Shared by the ready-wait failure, the crash event and system.app_status."""
    if path is None:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if size > n_bytes and "\n" in text:
            text = text.split("\n", 1)[1]  # drop the partial first line
        return text
    except OSError:
        return ""


@dataclass
class RunningSidecar:
    window_id: str
    app_id: str
    port: int
    process: asyncio.subprocess.Process
    started_at: float
    install_dir: Path
    # Set by _terminate before it signals, so the reaper can tell an exit WE
    # asked for from one the sidecar had on its own. Without it every
    # deliberate stop looks like a crash: _terminate sends SIGTERM, a sidecar
    # with no SIGTERM handler dies at rc=-15, and _reap's `rc not in (0, None)`
    # test fires the "exited unexpectedly" inbox notice. That was already true
    # of closing the last window by hand; it became routine when idle-sandbox
    # reclamation started calling close_app on a schedule.
    stopping: bool = False
    # A resident sidecar (manifest runtime.lifecycle=resident, or started by
    # start_app) survives the last window close and is restarted on a crash.
    # Window-scoped is the default: last close terminates.
    resident: bool = False
    # Where this process's stdout+stderr land. None only when the app's data
    # dir could not be opened for writing, in which case the pipes are used as
    # before. A PodRuntime has no such file — its output is the pod's.
    log_file: Path | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None


@dataclass
class PodRuntime:
    """The RunningSidecar-shaped view of a process app that runs in its OWN
    runtime pod (manifest.runs_in_pod — Form 2). Same attributes every
    caller of the supervisor reads (base_url, pid, is_alive, resident,
    port, started_at, install_dir), so the launcher, the runtime switch,
    system.restart_app, the process sweep and the machine panel keep
    working unchanged — and answer TRUTHFULLY: pid is None (no child in
    this container), is_alive asks the app-runtime lane whether this kernel
    holds a live wake for the pod.

    Origin: a Python app that needed apt + a C toolchain +
    python 3.11 at first boot could run nowhere on the platform: the entity container is uid 1000 with no compiler and only the
    kernel's own python, and the one runtime pod form was a scratch Go
    helper. User process apps now get a toolchain pod; this is the seam
    that keeps the supervisor's callers honest about it."""
    app_id: str
    upstream: str            # host:port of the pod from the last wake
    install_dir: Path
    started_at: float
    resident: bool = False
    window_id: str = ""
    process: None = None
    stopping: bool = False

    @property
    def port(self) -> int:
        try:
            return int(self.upstream.rsplit(":", 1)[-1])
        except (ValueError, AttributeError):
            return 0

    @property
    def base_url(self) -> str:
        return f"http://{self.upstream}"

    @property
    def pid(self) -> int | None:
        return None

    def is_alive(self) -> bool:
        from server.applications import harness_backend as _hb
        return bool(_hb.app_runtime_state(self.app_id).get("running"))


class SupervisorError(RuntimeError):
    """Spawn / wait_ready / terminate failed in a way the caller should see.

    `status` is set where the caller asked for something that cannot be done
    as asked (404 an app that is not installed, 409 one that is not in a
    state to start) — tool_call_ledger.refused_if_caller files those as the
    caller's outcome. A spawn or readiness failure carries none: the
    platform's."""

    def __init__(self, msg: str = "", *, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _pick_port() -> int:
    """Bind a temp socket to port 0, read the assigned port, close.
    Briefly racy with another listener — sidecars retry once on
    bind-failure, so we accept the window."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Supervisor:
    """One sidecar per app, refcounted by the windows referencing it.

    A request through /system-apps/<id>/... goes to that app's single
    sidecar. The X-Application-Token header rides through the proxy
    so the sidecar can attribute requests to a specific window.

    First window open spawns. Last window close terminates — for the default
    `runtime.lifecycle: window`. A RESIDENT sidecar (`lifecycle: resident`, or
    one started with start_app) is not window-refcounted: it starts at kernel
    boot (ensure_resident_apps), stays up with no window open, is restarted on
    a crash (bounded), and stops only when the user or the host says so
    (stop_app / the runtime route / installed_applications.resident_enabled).
    """

    # Crash auto-restart for resident sidecars, Kubernetes' CrashLoopBackOff
    # shape: the first restart after RESIDENT_RESTART_DELAY_S, each further
    # crash in a row doubles the wait up to RESIDENT_RESTART_MAX_DELAY_S, and a
    # run that stays up RESIDENT_STABLE_S resets the count (the kubelet's 10 s
    # doubling to 5 min, reset after 10 min of running). It NEVER gives up: an
    # always-on app (a bot, a poller) that hit a transient cause at boot — a DB
    # not ready, a network blip — used to be stopped for good after its 4th
    # crash in 5 minutes. Only the user or the Host stops it
    # (stop_app). Every exit is surfaced to the inbox by _reap, with when the
    # next restart comes.
    RESIDENT_RESTART_DELAY_S = 10.0
    RESIDENT_RESTART_MAX_DELAY_S = 300.0
    RESIDENT_STABLE_S = 600.0

    def __init__(self) -> None:
        self._by_app: dict[str, RunningSidecar] = {}
        self._refs: dict[str, set[str]] = {}  # app_id -> set of window_ids
        self._lock = asyncio.Lock()
        # Set by ensure_resident_apps (boot) so a crash restart can reload the
        # app's registry row + manifest without a caller in the loop.
        self._pool = None
        self._crashes: dict[str, list[float]] = {}  # app_id -> crash times, this run of crashes
        # app_id -> {state, crashes_in_a_row, delay_s, since, message}: what
        # the crash restart is doing now (restart_status).
        self._backoff: dict[str, dict] = {}
        # A pending crash-restart per app, so stop_app can cancel it: without
        # this a restart scheduled a moment before the user said stop fired
        # after the stop and brought the app back (caught by the gate).
        # _stopped is the belt to that brace — the flag a
        # restart re-checks after its sleep.
        self._restart_tasks: dict[str, asyncio.Task] = {}
        self._stopped: set[str] = set()

    # ── Pod-backed process apps (manifest.runs_in_pod) ───────────────────
    #
    # The supervisor is the ONE door every caller uses for a process app's
    # lifecycle, so placement is answered here and nowhere else: an app whose
    # manifest names a toolchain image never becomes a child of this
    # container — its lifecycle calls are routed to the app-runtime lane
    # (harness_backend.ensure_awake_app / app_hold / app_release), and its
    # state is read back from that lane. The in-entity path below it is
    # unchanged for image-baked first-party apps.

    @staticmethod
    def _pod_backed(manifest: AppManifest) -> bool:
        from server.applications.manifest import runs_in_pod
        return runs_in_pod(manifest)

    @staticmethod
    def _pod_view(app_id: str, install_dir: Path | None = None) -> PodRuntime | None:
        """The PodRuntime for an app the lane currently holds awake, or None."""
        from server.applications import harness_backend as _hb
        st = _hb.app_runtime_state(app_id)
        if not st.get("running"):
            return None
        lane = _hb._state.get(app_id) or {}
        return PodRuntime(
            app_id=app_id, upstream=st["url"] or "",
            install_dir=install_dir or (_INSTALL_ROOT / app_id),
            started_at=float(lane.get("started_wall") or time.time()),
            resident=bool(lane.get("resident")),
        )

    async def _pod_open(self, installed: InstalledApp, manifest: AppManifest,
                        *, resident: bool) -> PodRuntime:
        from server.applications import harness_backend as _hb
        if resident:
            # Hold BEFORE the wake. A resident app is switched on by the
            # user and only the user switches it off; a wake that outruns
            # its wait (a first boot installing its own toolchain) or fails
            # outright must leave the hold in place so the lane keeps
            # re-waking until the pod answers. Before this the hold was
            # taken only after a successful wake: one Host's Python app
            # wake 504'd at 60s, nothing held the pod, and the operator's
            # idle sweep reaped it 15 minutes later.
            _hb.app_hold(manifest.id)
        host = await _hb.ensure_awake_app(manifest.id)
        if host is None:
            raise SupervisorError(
                f"runtime pod for {manifest.id} did not wake"
                + (" yet — it is resident, so the lane keeps re-waking it "
                   "until it answers" if resident else "")
                + " — see the kernel log for the operator's reason "
                  "(descriptor / budget / image / still starting)")
        lane = _hb._state.get(manifest.id) or {}
        lane.setdefault("started_wall", time.time())
        view = self._pod_view(manifest.id, Path(installed.source_dir) if installed.source_dir else None)
        assert view is not None
        return view

    def get(self, app_id: str) -> RunningSidecar | PodRuntime | None:
        local = self._by_app.get(app_id)
        if local is not None:
            return local
        return self._pod_view(app_id)

    def list_running(self) -> list[RunningSidecar | PodRuntime]:
        from server.applications import harness_backend as _hb
        out: list[RunningSidecar | PodRuntime] = list(self._by_app.values())
        for app_id, st in list(_hb._state.items()):
            if st.get("harness") == _hb._APP_RT_MARK and st.get("url") \
                    and app_id not in self._by_app:
                view = self._pod_view(app_id)
                if view is not None:
                    out.append(view)
        return out

    async def open_window(
        self,
        *,
        installed: InstalledApp,
        manifest: AppManifest,
        window_id: str,
        kernel_url: str = KERNEL_LOOPBACK_BASE,
    ) -> RunningSidecar:
        """Acquire a sidecar reference for `window_id`. Spawns the
        sidecar if this is the app's first window; otherwise increments
        the refcount and returns the existing one."""
        if manifest.runtime.kind != "process":
            raise SupervisorError(
                f"app {manifest.id} is not a process runtime "
                f"(kind={manifest.runtime.kind})"
            )
        if self._pod_backed(manifest):
            # Window refs are the app-runtime lane's business for pod apps:
            # the close hook (harness_backend.app_window_closed) releases
            # after the grace, and a resident hold survives the close.
            return await self._pod_open(
                installed, manifest, resident=self._wants_resident(installed, manifest))

        async with self._lock:
            existing = self._by_app.get(manifest.id)
            if existing is not None and existing.is_alive():
                self._refs.setdefault(manifest.id, set()).add(window_id)
                log.info(
                    "window %s attached to existing sidecar %s :%d",
                    window_id, manifest.id, existing.port,
                )
                return existing

        running = await self._spawn(installed, manifest, kernel_url=kernel_url)
        running.resident = self._wants_resident(installed, manifest)

        async with self._lock:
            if running.resident:
                # Opening a resident app's window is a start: re-arm crash
                # restarts a plain `stop` (not `disable`) had stood down.
                self._stopped.discard(manifest.id)
            self._by_app[manifest.id] = running
            self._refs.setdefault(manifest.id, set()).add(window_id)

        asyncio.create_task(self._reap(running))
        return running

    @staticmethod
    def _wants_resident(installed: InstalledApp, manifest: AppManifest) -> bool:
        """The manifest asks for resident AND the user has not switched it off.
        installed.resident_enabled: True/None = on (None = the manifest's
        word stands), False = the user turned it off."""
        if manifest.runtime.kind != "process":
            return False
        if manifest.runtime.lifecycle != "resident":
            return False
        return installed.resident_enabled is not False

    async def start_app(
        self,
        app_id: str,
        *,
        pool,
        kernel_url: str = KERNEL_LOOPBACK_BASE,
        resident: bool | None = None,
        _rearm: bool = True,
    ) -> RunningSidecar:
        """Start an app's sidecar with NO window holding it. This is the
        resident lifecycle's door (boot, the runtime route, the host's
        system.start_app, a scheduled task) and the second half of a restart.

        `resident=None` takes the manifest's word (filtered by the user's
        switch); True/False force it — restart_app passes the running
        sidecar's own flag so a window-scoped app stays window-scoped.
        Idempotent: an app already alive is returned as is.

        `_rearm` (internal): an explicit start clears the app's stopped mark
        so crash restarts are armed again. The crash-restart path passes
        False — it must NOT clear a stop the user issued while it was
        spawning; it re-checks the mark afterwards and stands down."""
        from server.applications import registry as _registry
        from server.applications import manifest as _manifest_mod

        installed = await _registry.get(pool, app_id)
        if installed is None:
            raise SupervisorError(f"not installed: {app_id}", status=404)
        if installed.status != "active":
            raise SupervisorError(f"app not active: {installed.status}", status=409)
        if not installed.source_dir:
            raise SupervisorError(f"install dir for {app_id} unknown")
        manifest = _manifest_mod.load(Path(installed.source_dir) / "app.yaml")
        if manifest.runtime.kind != "process":
            raise SupervisorError(
                f"app {app_id} is not a process runtime (kind={manifest.runtime.kind})",
                status=409,
            )
        want_resident = (self._wants_resident(installed, manifest)
                         if resident is None else bool(resident))
        if self._pod_backed(manifest):
            if _rearm:
                self._stopped.discard(app_id)
            return await self._pod_open(installed, manifest, resident=want_resident)

        async with self._lock:
            if _rearm:
                self._stopped.discard(app_id)  # an explicit start re-arms crash restarts
            existing = self._by_app.get(app_id)
            if existing is not None and existing.is_alive():
                if want_resident:
                    existing.resident = True
                return existing

        running = await self._spawn(installed, manifest, kernel_url=kernel_url)
        running.resident = want_resident
        async with self._lock:
            self._by_app[app_id] = running
            self._refs.setdefault(app_id, set())
        asyncio.create_task(self._reap(running))
        log.info("sidecar %s started with no window (resident=%s)", app_id, want_resident)
        return running

    async def stop_app(self, app_id: str) -> bool:
        """Stop an app's sidecar whether or not it is resident — the user's or
        the host's explicit gesture. Same lock discipline as close_app, plus:
        a crash-restart already scheduled for it is cancelled, and the app is
        marked stopped so a restart that slips past the cancel stands down."""
        self._crashes.pop(app_id, None)
        self._backoff.pop(app_id, None)
        self._stopped.add(app_id)
        if app_id not in self._by_app:
            # Not a child of this container: a pod-backed app (or nothing
            # running at all). The lane answers honestly either way.
            from server.applications import harness_backend as _hb
            return await _hb.app_release(app_id)
        # Deliberately NOT cancelling a pending _restart_resident task: a
        # cancel that lands while it is inside _spawn/_wait_ready would raise
        # CancelledError past the point where the child exists but before it
        # is recorded in _by_app — an untracked process nothing can stop, and a
        # later start would run a second copy. The restart
        # path re-checks _stopped before AND after its start and terminates
        # what it just started if the user said stop meanwhile.
        return await self.close_app(app_id)

    async def restart_app(self, app_id: str, *, pool) -> RunningSidecar:
        """Stop and start, preserving what the app WAS: a window-scoped
        sidecar keeps its open windows' references (so the eventual last
        close still terminates it), a resident one stays resident."""
        async with self._lock:
            prev = self._by_app.get(app_id)
            prev_refs = set(self._refs.get(app_id) or ())
            was_resident = bool(prev.resident) if prev is not None else None
        if prev is None:
            pod = self._pod_view(app_id)
            if pod is not None:
                was_resident = pod.resident
        await self.close_app(app_id)
        running = await self.start_app(app_id, pool=pool, resident=was_resident)
        if prev_refs:
            async with self._lock:
                self._refs.setdefault(app_id, set()).update(prev_refs)
        return running

    async def ensure_resident_apps(self, pool) -> dict:
        """Kernel boot: start every installed process app whose manifest says
        `lifecycle: resident` and whose user switch is not off. Fail-soft per
        app — one broken daemon must never block the others or the boot.
        Returns {started: [...], skipped: [...], failed: {id: reason}}."""
        from server.applications import registry as _registry
        from server.applications import manifest as _manifest_mod

        self._pool = pool
        out: dict = {"started": [], "skipped": [], "failed": {}}
        try:
            apps = await _registry.list_active(pool)
        except Exception as e:  # noqa: BLE001 - boot must not die on a registry read
            log.warning("resident sweep: registry unreadable: %s", e)
            return out
        for installed in apps:
            if not installed.source_dir:
                continue
            try:
                manifest = _manifest_mod.load(Path(installed.source_dir) / "app.yaml")
            except Exception:  # noqa: BLE001 - a bad manifest is that app's problem
                continue
            if manifest.runtime.kind != "process" or manifest.runtime.lifecycle != "resident":
                continue
            if installed.resident_enabled is False:
                out["skipped"].append(installed.id)
                continue
            try:
                await self.start_app(installed.id, pool=pool, resident=True)
                out["started"].append(installed.id)
            except Exception as e:  # noqa: BLE001 - see docstring
                out["failed"][installed.id] = str(e)
                log.warning("resident app %s failed to start at boot: %s", installed.id, e)
        if out["started"] or out["failed"]:
            log.info("resident apps at boot: %s", out)
        return out

    def restart_delay(self, crashes_in_a_row: int) -> float:
        """The wait before restarting after this many crashes in a row."""
        return float(min(self.RESIDENT_RESTART_DELAY_S * (2 ** min(max(0, int(crashes_in_a_row) - 1), 32)),
                         max(self.RESIDENT_RESTART_DELAY_S, self.RESIDENT_RESTART_MAX_DELAY_S)))

    def restart_status(self, app_id: str) -> dict | None:
        """What the crash restart of a resident app is doing: {state:
        'restarting', crashes_in_a_row, delay_s, since, message}, or None
        when it has not crashed (or was stopped)."""
        st = self._backoff.get(app_id)
        return dict(st) if st else None

    def _note_crash(self, app_id: str) -> float:
        """Count a crash and return the wait before the restart. A crash
        long after the previous one (the app ran RESIDENT_STABLE_S past its
        restart) starts the count again."""
        now = time.time()
        hist = self._crashes.get(app_id, [])
        prev_delay = float((self._backoff.get(app_id) or {}).get("delay_s") or 0.0)
        if hist and now - hist[-1] < self.RESIDENT_STABLE_S + prev_delay:
            hist = hist + [now]
        else:
            hist = [now]
        self._crashes[app_id] = hist
        delay = self.restart_delay(len(hist))
        self._backoff[app_id] = {
            "state": "restarting", "crashes_in_a_row": len(hist), "delay_s": delay,
            "since": now,
            "message": (f"crashed {len(hist)} time(s) in a row — restarting, backing off "
                        f"{delay:.0f}s"),
        }
        return delay

    async def _restart_resident(self, app_id: str, delay: float | None = None) -> None:
        """After a resident sidecar crashed: restart it after the backoff
        wait (_note_crash), however many times it has crashed."""
        if delay is None:
            delay = self._note_crash(app_id)
        n = len(self._crashes.get(app_id, []))
        if self._pool is None:
            log.warning("resident sidecar %s crashed but the supervisor has no pool to "
                        "reload it with — start it from the runtime route", app_id)
            return
        log.warning("resident sidecar %s crashed (%d in a row) — restarting, backing off %.0fs",
                    app_id, n, delay)
        await asyncio.sleep(delay)
        if app_id in self._stopped:
            log.info("resident sidecar %s: restart stood down — stopped on request meanwhile", app_id)
            return
        try:
            await self.start_app(app_id, pool=self._pool, resident=True, _rearm=False)
        except Exception as e:  # noqa: BLE001 - a failed restart is logged, never raised
            # A restart that could not start is one more crash in a row: back
            # off and try again, never stop here (that was a silent give-up).
            if app_id in self._stopped:
                return
            try:
                nxt = self._note_crash(app_id)
            except Exception:  # noqa: BLE001
                nxt = float(max(self.RESIDENT_RESTART_DELAY_S, self.RESIDENT_RESTART_MAX_DELAY_S))
            log.warning("resident sidecar %s restart failed: %s — trying again in %.0fs",
                        app_id, e, nxt)
            self._restart_tasks[app_id] = asyncio.create_task(self._restart_resident(app_id, nxt))
            return
        if app_id in self._stopped:
            # The user said stop while we were spawning. The child is tracked
            # (start_app recorded it), so this is an ordinary stop, not a leak.
            log.info("resident sidecar %s: stopped on request during restart — terminating", app_id)
            await self.close_app(app_id)
            return
        if app_id in self._backoff:
            self._backoff[app_id]["state"] = "restarted"
            self._backoff[app_id]["message"] = (
                f"restarted after {n} crash(es) in a row; the count resets once it stays up "
                f"{self.RESIDENT_STABLE_S:.0f}s")
        log.info("resident sidecar %s restarted after crash (%d in a row)", app_id, n)

    async def _spawn(
        self,
        installed: InstalledApp,
        manifest: AppManifest,
        *,
        kernel_url: str,
    ) -> RunningSidecar:
        install_dir = Path(installed.source_dir) if installed.source_dir else None
        if install_dir is None or not install_dir.exists():
            raise SupervisorError(
                f"install dir for {manifest.id} missing: {install_dir}"
            )

        port = _pick_port()
        # The app's writable data dir and its log file. This is
        # the IN-CONTAINER path — an app that runs_in_pod never reaches here —
        # but an image-baked process app still needs somewhere to write and a
        # place its output survives. One Host's app bootstrap failure left no
        # trace anywhere: the app spawned it without capturing output and the
        # supervisor held the sidecar's own output in a pipe nobody read.
        data_dir = _workspace_dir(manifest.id)
        log_path = data_dir / LOG_SUBDIR / LOG_FILENAME
        env = _child_env({
            manifest.runtime.port_env: str(port),
            "PRIVATAE_APP_ID": manifest.id,
            "CSERIES_KERNEL_URL": kernel_url,
            ENV_DATA_DIR: str(data_dir),
            ENV_LOG_FILE: str(log_path),
        })

        argv = [
            os.path.expandvars(arg) if "$" in arg else arg
            for arg in manifest.runtime.command
        ]

        log.info(
            "spawning sidecar %s on :%d, argv=%s",
            manifest.id, port, " ".join(shlex.quote(a) for a in argv),
        )

        log_fh = _open_log(log_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(install_dir),
                env=env,
                stdout=log_fh if log_fh is not None else asyncio.subprocess.PIPE,
                stderr=log_fh if log_fh is not None else asyncio.subprocess.PIPE,
                preexec_fn=build_preexec(),
            )
        except (OSError, FileNotFoundError) as e:
            raise SupervisorError(f"spawn failed: {e}") from None
        finally:
            if log_fh is not None:
                log_fh.close()  # the child holds its own descriptor

        log.info("spawned %s pid=%d", manifest.id, proc.pid)

        running = RunningSidecar(
            window_id="",                  # sidecar is per-app, not per-window
            app_id=manifest.id,
            port=port,
            process=proc,
            started_at=time.time(),
            install_dir=install_dir,
            log_file=log_path if log_fh is not None else None,
        )

        try:
            await self._wait_ready(running, manifest.runtime.ready_path,
                                   manifest.runtime.ready_timeout_seconds)
        except SupervisorError:
            await self._terminate(running)
            raise

        return running

    async def _wait_ready(
        self,
        running: RunningSidecar,
        ready_path: str,
        timeout_seconds: int,
    ) -> None:
        url = running.base_url.rstrip("/") + "/" + ready_path.lstrip("/")
        deadline = time.time() + max(1, timeout_seconds)

        async with aiohttp.ClientSession() as session:
            while time.time() < deadline:
                # Probe the process for exit. asyncio's `returncode`
                # attribute only updates after `wait()` is called, so
                # we use a tight wait_for to detect death without
                # waiting forever.
                try:
                    await asyncio.wait_for(running.process.wait(), timeout=0.01)
                except asyncio.TimeoutError:
                    pass
                if not running.is_alive():
                    err = ""
                    if getattr(running, "log_file", None) is not None:
                        err = tail_text(running.log_file)
                    else:
                        try:
                            err = (await running.process.stderr.read(4096)).decode(
                                errors="replace"
                            )
                        except Exception:
                            pass
                    log.warning(
                        "sidecar %s exited rc=%s, stderr=%r",
                        running.app_id, running.process.returncode, err[:1024],
                    )
                    raise SupervisorError(
                        f"sidecar exited before ready "
                        f"(code={running.process.returncode}, stderr={err[:512]!r})"
                    )

                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if r.status < 500:
                            log.info(
                                "sidecar %s healthy on :%d after %.2fs",
                                running.app_id, running.port,
                                time.time() - running.started_at,
                            )
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    pass

                await asyncio.sleep(0.2)

        raise SupervisorError(
            f"sidecar {running.app_id} did not become ready in {timeout_seconds}s"
        )

    async def close_window(self, app_id: str, window_id: str) -> None:
        """Release one window's reference. Terminates the sidecar when
        the last window closes."""
        terminate: RunningSidecar | None = None
        async with self._lock:
            refs = self._refs.get(app_id)
            if refs is None:
                return
            refs.discard(window_id)
            if not refs:
                cur = self._by_app.get(app_id)
                if cur is not None and cur.resident:
                    # Resident: the last window closing is not a stop signal.
                    log.info("last window of resident sidecar %s closed; it stays up", app_id)
                    return
                self._refs.pop(app_id, None)
                terminate = self._by_app.pop(app_id, None)
        if terminate is not None:
            await self._terminate(terminate)

    async def close_app(self, app_id: str) -> bool:
        """Release EVERY window on one app and stop its sidecar. True if it was
        running and is now gone.

        `close_window` is the user gesture — one window at a time, refcounted.
        This is the machine one: "this app's sandbox has been idle for twenty
        minutes, reclaim it" (server/process_awareness.py). Same shape as
        shutdown_all, scoped to a single app.

        The reason it is a method rather than the caller looping close_window
        over a copy of `_refs`: that loop reads private state, and it drops the
        lock between windows, so a window opening mid-close leaves a live ref
        pointing at a sidecar the caller believes it closed. Popping both maps
        under one lock cannot interleave. A window opened AFTER the pop spawns a
        fresh sidecar, which is the correct outcome — the user reopened the app.
        """
        async with self._lock:
            self._refs.pop(app_id, None)
            running = self._by_app.pop(app_id, None)
        if running is None:
            # A pod-backed app the lane holds awake: release it (idle reclaim
            # / restart). Nothing held → False, same as before.
            from server.applications import harness_backend as _hb
            return await _hb.app_release(app_id)
        await self._terminate(running)
        return True

    async def shutdown_all(self) -> None:
        async with self._lock:
            running = list(self._by_app.values())
            self._by_app.clear()
            self._refs.clear()
        for r in running:
            await self._terminate(r)

    async def _terminate(self, running: RunningSidecar) -> None:
        if not running.is_alive():
            return
        proc = running.process
        # Claim the exit BEFORE signalling. _reap is already awaiting this
        # process on its own task, so the window between the signal and the
        # flag would be enough for it to read the death as a crash.
        running.stopping = True
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning(
                "sidecar %s did not exit on SIGTERM, sending SIGKILL", running.app_id
            )
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

        log.info("sidecar %s terminated (rc=%s)", running.app_id, proc.returncode)

    async def _reap(self, running: RunningSidecar) -> None:
        """Wait on the process; when it exits, drop it from the maps."""
        try:
            await running.process.wait()
        finally:
            async with self._lock:
                cur = self._by_app.get(running.app_id)
                if cur is running:
                    self._by_app.pop(running.app_id, None)
                    self._refs.pop(running.app_id, None)
            if running.stopping:
                # We asked for this one. A sidecar without a SIGTERM handler
                # exits at rc=-15, which is indistinguishable from a crash by
                # returncode alone — and telling the user their app "exited
                # unexpectedly" every time they closed its last window, or
                # every time an idle sandbox was reclaimed, would teach them to
                # ignore the notice that matters.
                log.info("sidecar %s stopped on request (rc=%s)",
                         running.app_id, running.process.returncode)
            elif running.process.returncode not in (0, None):
                # Structured log so log scrapers + the entity's
                # own system.app_status query can surface the crash.
                log.warning(
                    "sidecar %s exited unexpectedly (rc=%s) — "
                    "entity-side recovery via default-skills/system.md SOP",
                    running.app_id, running.process.returncode,
                )
                await self._after_crash(running)

    async def _after_crash(self, running: RunningSidecar) -> None:
        """Surface the exit and, for an always-on app, schedule its restart.
        Nothing here may stop the restart silently: an error while counting
        the crash or writing the notice still restarts the app (at the
        backoff cap) and is itself reported (an OverflowError at crash
        1,025 used to end the restarts unseen)."""
        app_id = running.app_id
        restart_in = None
        if running.resident and app_id not in self._stopped:
            try:
                restart_in = self._note_crash(app_id)
            except Exception as e:  # noqa: BLE001 — never lose the restart
                log.exception("resident sidecar %s: could not count the crash", app_id)
                restart_in = float(max(self.RESIDENT_RESTART_DELAY_S,
                                       self.RESIDENT_RESTART_MAX_DELAY_S))
                await self._surface_problem(
                    app_id, f"Counting the crash of `{app_id}` failed ({e!r}); it is "
                            f"restarted in {restart_in:.0f} s anyway.")
        try:
            await self._surface_exit(running, restart_in_s=restart_in)
        except Exception:  # noqa: BLE001 — the notice failing must not stop the restart
            log.exception("resident sidecar %s: the exit notice could not be written", app_id)
        if restart_in is not None and app_id not in self._stopped:
            try:
                self._restart_tasks[app_id] = asyncio.create_task(
                    self._restart_resident(app_id, restart_in))
            except Exception as e:  # noqa: BLE001
                log.exception("resident sidecar %s: the restart could not be scheduled", app_id)
                await self._surface_problem(
                    app_id, f"`{app_id}` crashed and its restart could not be scheduled "
                            f"({e!r}). Start it again from its window or the runtime switch.")

    async def _surface_problem(self, app_id: str, body: str) -> None:
        try:
            from server.bridge.memory_handlers import handle_inbox_append
            await handle_inbox_append({"subject": f"{app_id}: restart problem", "body": body,
                                       "message_kind": "system_notification",
                                       "payload": {"type": "sidecar_restart_problem",
                                                   "app_id": app_id}})
        except Exception:  # noqa: BLE001 — the log line above is the record
            log.warning("could not surface a restart problem for %s", app_id)

    async def _surface_exit(self, running: RunningSidecar, *,
                            restart_in_s: float | None = None) -> None:
        """Push the crash into the entity's inbox as a system_notification.

        THE EVENT WAS DOCUMENTED BUT NEVER EMITTED. default-skills/system.md has
        described a `{type: "sidecar_exit", app_id, exit_code, last_logs}` inbox
        message for a long time, and the SOP built on it was live — but the only
        writer was a comment here saying auto-push was deferred because
        `channel_messages` has no writers. That named the wrong table:
        `inbox_messages` HAS a writer (server/bridge/memory_handlers.py
        handle_inbox_append, message_kind='system_notification'), and it is the
        surface the user actually reads. So the SOP's event is real from here.

        CRASH METADATA ONLY. If the exit left a core dump, the event carries its
        name, size and time — never a byte of it. server/crash.py does the
        relocation into the crash dir first, so the path in the event is the one
        the Host will see from /api/system/crash rather than wherever the kernel
        happened to drop it (core_pattern is a bare relative name, so that is
        the crashing process's cwd).

        Fail-soft throughout: a supervisor reaping a dead sidecar must never
        raise. A missed notification is a degraded experience; an exception here
        would leave the reap half-done.
        """
        rc = running.process.returncode
        dump = None
        try:
            from server import crash
            moved = await crash.sweep(component=running.app_id)
            if moved:
                newest = max(moved, key=lambda m: m["bytes"])
                dump = {
                    "crash_file": f"{crash.CRASH_DIRNAME}/{newest['name']}",
                    "bytes": newest["bytes"],
                    "mtime": newest["mtime"],
                }
        except Exception as e:  # noqa: BLE001 — see docstring: never raise here
            log.warning("crash relocate after %s exit failed: %s", running.app_id, e)

        # rc < 0 is -N for "killed by signal N" (asyncio convention), which is
        # the case that produces a dump; a positive rc is a plain exit status.
        signal_no = -rc if isinstance(rc, int) and rc < 0 else None
        event = {
            "type": "sidecar_exit",
            "app_id": running.app_id,
            "exit_code": rc,
            "signal": signal_no,
        }
        if dump:
            event.update(dump)
        # The field default-skills/system.md documents and that nothing
        # once emitted: the last lines the process printed, now that
        # they land in a file.
        last = tail_text(getattr(running, "log_file", None))
        if last:
            event["last_logs"] = last
            event["log_file"] = str(running.log_file)

        body = f"`{running.app_id}` exited unexpectedly (exit code {rc})."
        if restart_in_s is not None:
            n = len(self._crashes.get(running.app_id, []))
            event["restart_in_s"] = restart_in_s
            event["crashes_in_a_row"] = n
            body += (f" It is always on, so it is restarted in {restart_in_s:.0f} s "
                     f"(crash {n} in a row; each crash in a row doubles the wait, up to "
                     f"{self.RESIDENT_RESTART_MAX_DELAY_S / 60:.0f} minutes). Stop it to "
                     "stop the restarts.")
        if last:
            body += "\n\nLast lines it printed:\n```\n" + last.rstrip() + "\n```"
        if dump:
            body += (
                f"\n\nIt left a crash dump: `{dump['crash_file']}`, "
                f"{dump['bytes'] / (1 << 20):.0f} MB. That file is yours — the "
                "platform never reads it, backs it up or ships it. Keep it if "
                "you want to look into the crash, or delete it."
            )
        try:
            from server.bridge.memory_handlers import handle_inbox_append
            await handle_inbox_append({
                "subject": f"{running.app_id} crashed",
                "body": body,
                "message_kind": "system_notification",
                "payload": event,
            })
        except Exception as e:  # noqa: BLE001 — see docstring: never raise here
            log.warning("could not surface %s exit to the inbox: %s", running.app_id, e)


# Module-level supervisor instance, wired by main.py at startup.
_SUPERVISOR: Supervisor | None = None


def set_supervisor(s: Supervisor) -> None:
    global _SUPERVISOR
    _SUPERVISOR = s


def get_supervisor() -> Supervisor | None:
    return _SUPERVISOR
