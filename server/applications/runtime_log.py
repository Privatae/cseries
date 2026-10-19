"""runtime_log — the log file of an app that runs in its OWN runtime pod.

The in-container supervisor redirects a sidecar's stdout+stderr to
<workspace>/logs/sidecar.log (supervisor.py, 2026-09-03). A pod-backed app
(manifest.runs_in_pod) has no child here to redirect, so until 2026-09-04
its log_tail was "" by construction and the host was blind to a failed boot
(one app: the bootstrap died at a setup step and the
only record sat on the pod's PVC).

The toolchain image's wrapper (sidecars/app-runtime-python/run.py) now
POSTs what the command prints to routes/applications.py:app_runtime_log,
and this module writes it to THE SAME FILE. One path, one reader
(supervisor.tail_text via system.app_status), both placements.

Also recorded: the child's exit, on the app-runtime lane's state
(harness_backend._state[app_id]["last_exit"]), so status can say the
process died even while the pod's probe kept it Ready — a UI server that
answers the probe over a failed bootstrap is exactly the shape that hid
that failure.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("privatae.applications.runtime_log")


def runtime_log_path(app_id: str) -> Path:
    from server.applications import storage
    from server.applications.supervisor import LOG_FILENAME, LOG_SUBDIR
    return storage.workspace_dir(app_id) / LOG_SUBDIR / LOG_FILENAME


def _pod_generation(app_id: str, pod: str | None) -> bool:
    """Track WHICH pod is talking. The wrapper tags every POST with its pod
    name (HOSTNAME); the first POST from a new pod name clears the previous
    generation's exit and numbers, so a re-ship's proof never reads the old
    pod's crash. Returns False when the POST is
    from an OLDER generation than the one already seen — its exit is
    ignored — True otherwise."""
    if not pod:
        return True
    try:
        from server.applications import harness_backend as _hb
        st = _hb._state.get(app_id)
        if st is None:
            st = _hb._st(app_id, _hb._APP_RT_MARK)
        cur = st.get("runtime_pod")
        if cur == pod:
            return True
        prev = st.get("runtime_pods_seen") or []
        if pod in prev:
            return False            # a straggler from a pod already superseded
        st["runtime_pod"] = pod
        st["runtime_pods_seen"] = (prev + [cur] if cur else prev)[-8:]
        st.pop("last_exit", None)
        st.pop("runtime_stats", None)
        return True
    except Exception:
        return True


def append_runtime_log(app_id: str, lines: list[str], *,
                       exit_code: int | None = None, pod: str | None = None) -> int:
    """Append `lines` to the app's log file (rotating at the supervisor's
    size), stamp a non-None exit on the lane state. Returns lines written.
    Fail-soft: a full or unwritable data dir logs a warning and returns 0 —
    the pod's POST must never 500 because the kernel's disk is tight."""
    from server.applications.supervisor import LOG_ROTATE_BYTES
    if not _pod_generation(app_id, pod):
        exit_code = None            # an old pod's last word is not this pod's
    path = runtime_log_path(app_id)
    written = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_ROTATE_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except FileNotFoundError:
            pass
        if lines:
            with open(path, "a", encoding="utf-8", errors="replace") as fh:
                for ln in lines:
                    fh.write(ln.rstrip("\n") + "\n")
                    written += 1
    except OSError as e:
        log.warning("runtime log for %s not writable (%s) — %d line(s) dropped",
                    app_id, e, len(lines))
    # A signal exit is a STOP, not a crash: the operator's scale-to-0 (release,
    # idle sweep) SIGTERMs the pod, the wrapper forwards it and reports -15
    # (SIGKILL → -9, an interactive SIGINT → -2; 143/137/130 when a shell in
    # the middle re-encodes it). Stamping those would make app_status say "the
    # app's process EXITED" about every sleeping window app until its next
    # wake. The supervisor makes
    # the same distinction for in-container sidecars with its `stopping` flag.
    if exit_code in (-15, -9, -2, 143, 137, 130):
        exit_code = None
    if exit_code is not None:
        try:
            from server.applications import harness_backend as _hb
            # _state is the lane's per-app dict (harness_backend.py:70); the
            # app-rt entries carry harness == _APP_RT_MARK ("app-rt", :359).
            st = _hb._state.get(app_id)
            if st is None:
                st = _hb._st(app_id, _hb._APP_RT_MARK)
            st["last_exit"] = {"code": int(exit_code), "at": time.time()}
        except Exception as e:  # noqa: BLE001 — telemetry, never the write
            log.debug("runtime log exit stamp for %s failed: %s", app_id, e)
    return written


def record_stats(app_id: str, stats: dict) -> None:
    """The pod command's own numbers, pushed with each log batch by the
    wrapper: {pid, mem_bytes} (+ whatever else it learns to sample). Kept on
    the lane state so the machine panel (routes/system.py) and app_status can
    show a pod app's pid and memory the way they show an in-container
    sidecar's (a pod app showed neither)."""
    if not isinstance(stats, dict):
        return
    try:
        from server.applications import harness_backend as _hb
        st = _hb._state.get(app_id)
        if st is None:
            st = _hb._st(app_id, _hb._APP_RT_MARK)
        keep = {}
        for k in ("pid", "mem_bytes"):
            v = stats.get(k)
            if isinstance(v, int) and v >= 0:
                keep[k] = v
        keep["at"] = time.time()
        st["runtime_stats"] = keep
    except Exception as e:  # noqa: BLE001 — telemetry
        log.debug("runtime stats for %s not recorded: %s", app_id, e)


def last_stats(app_id: str) -> dict | None:
    """{pid, mem_bytes, at} last pushed by the pod, or None."""
    try:
        from server.applications import harness_backend as _hb
        st = _hb._state.get(app_id) or {}
        s = st.get("runtime_stats")
        return dict(s) if isinstance(s, dict) else None
    except Exception:
        return None


def last_exit(app_id: str) -> dict | None:
    """{code, at} of the pod command's last recorded exit, or None."""
    try:
        from server.applications import harness_backend as _hb
        st = _hb._state.get(app_id) or {}
        ex = st.get("last_exit")
        return dict(ex) if isinstance(ex, dict) else None
    except Exception:
        return None


def clear_last_exit(app_id: str) -> None:
    """A fresh pod address means a fresh process — forget the old exit."""
    try:
        from server.applications import harness_backend as _hb
        st = _hb._state.get(app_id)
        if st is not None:
            st.pop("last_exit", None)
    except Exception:
        pass
