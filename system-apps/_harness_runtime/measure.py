"""Per-run resource measurement, read from this container's own cgroup.

The run result used to report wall-clock time only, and billing priced that
wall clock times the vCPUs the pod was allowed, whatever the run actually used.
With one run per container, the container's cgroup IS the run, so two cgroup v2 files give the
run's real numbers:

  memory.peak   the container's peak memory over its whole life, in bytes
  memory.stat   `anon` + `shmem`, sampled through the run (peak_anon_bytes)
  cpu.stat      `usage_usec`, CPU time used by every process in the container

memory.peak counts page cache. A run that writes and reads a lot of files
(an `npm install`) fills the cache, and the kernel takes that back whenever
something else needs it. Measured: one such run peaked at 606.8 MiB
in memory.peak against 186.1 MiB of anon memory. So the final frame also
carries peak_anon_bytes, the highest anon + shmem seen while the run was live:
the memory the run held that the kernel could not simply drop. shmem is
in because tmpfs and shared memory cannot be dropped without swap. The kernel
keeps no peak for it, so a background thread reads memory.stat every
SAMPLE_INTERVAL_S and keeps the highest value.

Inside a container on cgroup v2, /sys/fs/cgroup is that container's own
cgroup (cgroup namespace), so no path lookup is needed. In the shared
per-engine Deployment, which can hold several runs at once, the same numbers
cover the whole container; the `measure_scope` field says "container" so a
reader never mistakes them for one run's share.

A value that cannot be read is None (JSON null), never 0 and never estimated:
an unreadable file (cgroup v1, Docker Desktop, a seccomp or mount that hides
it) must not look like a run that used nothing.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from typing import Optional

log = logging.getLogger("harness_measure")

# The override exists for tests only; a real container reads its own cgroup.
CGROUP_DIR = os.environ.get("CSERIES_HARNESS_CGROUP_DIR", "/sys/fs/cgroup")

MEASURE_SCOPE = "container"

# How often the anon sampler reads memory.stat. A spike shorter than this can
# be missed; reading the file costs microseconds.
SAMPLE_INTERVAL_S = 0.25


def _read(name: str) -> Optional[str]:
    try:
        with open(os.path.join(CGROUP_DIR, name), encoding="ascii") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.debug("cgroup %s unreadable: %s", name, exc)
        return None


def cpu_usage_usec() -> Optional[int]:
    """`usage_usec` from cpu.stat, or None when it cannot be read."""
    text = _read("cpu.stat")
    if text is None:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec":
            try:
                v = int(parts[1])
            except ValueError:
                log.debug("cpu.stat usage_usec unparsable: %r", parts[1])
                return None
            return v if v >= 0 else None
    log.debug("cpu.stat has no usage_usec line")
    return None


def memory_peak() -> Optional[int]:
    """memory.peak in bytes, or None when it cannot be read."""
    text = _read("memory.peak")
    if text is None:
        return None
    try:
        v = int(text.strip())
    except ValueError:
        log.debug("memory.peak unparsable: %r", text[:40])
        return None
    return v if v >= 0 else None


def memory_anon() -> Optional[int]:
    """`anon` + `shmem` from memory.stat in bytes, or None when the file or
    either line cannot be read."""
    text = _read("memory.stat")
    if text is None:
        return None
    vals = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("anon", "shmem"):
            try:
                vals[parts[0]] = int(parts[1])
            except ValueError:
                log.debug("memory.stat %s unparsable: %r", parts[0], parts[1])
                return None
    if "anon" not in vals or "shmem" not in vals:
        log.debug("memory.stat has no anon or shmem line")
        return None
    v = vals["anon"] + vals["shmem"]
    return v if v >= 0 else None


def _sample_loop(ref, stop: threading.Event) -> None:
    # Holds only a weak reference, so a run that never reaches its final
    # frame (the client went away and the generator was dropped) does not
    # keep its sampler alive: the measure is collected, `stop` is set by its
    # finalizer, and this returns.
    while not stop.wait(SAMPLE_INTERVAL_S):
        m = ref()
        if m is None:
            return
        m._sample()
        del m


class RunMeasure:
    """Start a measurement at construction; `fields()` reads the end values.

    Built once per run, before the first attempt, so a retried run is measured
    across its attempts the same way `elapsed_seconds` is. Construction also
    starts the anon sampler; `fields()` stops it."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.cpu0 = cpu_usage_usec()
        self._lock = threading.Lock()
        self._peak_anon: Optional[int] = None
        self._stop = threading.Event()
        self._sample()
        weakref.finalize(self, self._stop.set)
        self._thread: Optional[threading.Thread] = threading.Thread(
            target=_sample_loop, args=(weakref.ref(self), self._stop),
            name="harness-anon-sampler", daemon=True)
        try:
            self._thread.start()
        except RuntimeError as exc:
            # No thread to spare: the first and last reads still count.
            log.debug("anon sampler not started: %s", exc)
            self._thread = None

    def _sample(self) -> None:
        v = memory_anon()
        if v is None:
            return
        with self._lock:
            if self._peak_anon is None or v > self._peak_anon:
                self._peak_anon = v

    def fields(self) -> dict:
        self._stop.set()
        self._sample()
        cpu1 = cpu_usage_usec()
        if self.cpu0 is None or cpu1 is None or cpu1 < self.cpu0:
            cpu_s = None
        else:
            cpu_s = round((cpu1 - self.cpu0) / 1e6, 3)
        with self._lock:
            peak_anon = self._peak_anon
        return {
            "peak_mem_bytes": memory_peak(),
            "peak_anon_bytes": peak_anon,
            "cpu_s": cpu_s,
            "wall_s": round(time.time() - self.t0, 2),
            "measure_scope": MEASURE_SCOPE,
        }
