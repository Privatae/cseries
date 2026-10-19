#!/usr/bin/env python3
"""Run a command and stop it only if it hangs or runs away, never for being slow.

    python3 scripts/hang_watch.py --cpu-budget 180 [--idle 120] [--backstop-s 1800] \
        [--log FILE] -- CMD...

A tight wall-clock limit is a performance budget: on a loaded box a gate that
is only slow runs out of it (the gates in pre_merge_gates.sh had 180 s each).
So the command's whole process tree is watched instead, and it is stopped when:

  - it HANGS idle: nothing in it spends any CPU for --idle seconds. A live
    process on a busy machine still gets a CPU many times a second;
  - it RUNS AWAY: it has spent more than --cpu-budget seconds of CPU;
  - it outlives --backstop-s: a hang BACKSTOP for what spends a little CPU
    and never ends (a poller waiting on a condition that never comes), set
    several times above the slowest gate measured on a loaded box. It is not
    a speed budget.

On any of them every thread's stack is written into the command's own output
first (PYTHONFAULTHANDLER is set for it, and it is sent SIGABRT, which
faulthandler answers without needing the GIL; that one process's core limit
is set to 0 first, so it writes no core file).
Then its process group gets SIGTERM, SIGKILL 10 s later, the reason is printed
on stderr as "hang_watch: stopped ... — <why>", and the exit status is 124 (as
timeout(1)'s). Otherwise the command's own exit status is returned.

SIGINT and SIGTERM sent to hang_watch are passed on to the command's process
group (it runs in a group of its own, which they would not reach), so a gate
run that is cancelled stops what it started instead of leaving it running. If
the group is still there 10 s later it is stopped as above, and the exit
status is 128 plus the signal's number.

--log FILE appends "<cpu s> <longest idle s> <wall s>" when the command ends,
to size the three limits from measurements.
"""
from __future__ import annotations

import argparse
import os
import resource
import signal
import subprocess
import sys
import time

_TICK = os.sysconf("SC_CLK_TCK")


def _fields(path: str) -> list[str]:
    with open(path) as f:
        return f.read().rsplit(")", 1)[1].split()   # [1] ppid, [11] utime, [12] stime, [13] cutime, [14] cstime


def descendants(pid: int) -> list[int]:
    """Every live process below `pid` (/proc/<pid>/task/<tid>/children)."""
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        try:
            tids = os.listdir(f"/proc/{p}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                with open(f"/proc/{p}/task/{tid}/children") as f:
                    kids = [int(k) for k in f.read().split()]
            except OSError:
                continue
            out += kids
            todo += kids
    return out


def tree_cpu(pid: int, skip_tid: int | None = None) -> float:
    """CPU seconds spent so far by `pid`'s threads (but `skip_tid`), the
    children it has reaped, and every live process below it."""
    ticks = 0
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        tids = []
    for tid in tids:
        if skip_tid is not None and int(tid) == skip_tid:
            continue
        try:
            f = _fields(f"/proc/{pid}/task/{tid}/stat")
            ticks += int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            pass
    for p, cols in [(pid, (13, 14))] + [(k, (11, 12, 13, 14)) for k in descendants(pid)]:
        try:
            f = _fields(f"/proc/{p}/stat")
            ticks += sum(int(f[c]) for c in cols)
        except (OSError, IndexError, ValueError):
            pass
    return ticks / _TICK


def _stop(proc: subprocess.Popen) -> None:
    try:
        # SIGABRT writes its stacks, not a core file: the core limit is set to
        # 0 on that one process, at that moment, never for the gate's run (a
        # gate checks the core limits its own entrypoints set).
        resource.prlimit(proc.pid, resource.RLIMIT_CORE, (0, 0))
        os.kill(proc.pid, signal.SIGABRT)          # faulthandler: every thread's stack, then exit
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def watch(cmd: list[str], cpu_budget: float, idle: float, backstop: float = 1800.0,
          poll: float = 1.0, log: str | None = None) -> int:
    proc = subprocess.Popen(cmd, start_new_session=True,  # its own group: stopped whole
                            env={**os.environ, "PYTHONFAULTHANDLER": "1"})
    t0 = time.monotonic()
    last, idle_since, longest = 0.0, t0, 0.0
    why = None
    got: list[tuple[int, float]] = []          # (signal, when) hang_watch was asked to stop

    def forward(signum, frame):
        got.append((signum, time.monotonic()))
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            pass
    old = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _watch_loop(proc, cmd, cpu_budget, idle, backstop, poll, log, t0, last, idle_since, longest, why, got)
    finally:
        for sg, h in old.items():
            signal.signal(sg, h)


def _watch_loop(proc, cmd, cpu_budget, idle, backstop, poll, log, t0, last, idle_since, longest, why, got) -> int:
    while True:
        try:
            rc = proc.wait(timeout=poll)
            if got:
                rc = 128 + got[0][0]
            break
        except subprocess.TimeoutExpired:
            pass
        if got:
            if time.monotonic() - got[0][1] >= 10:
                _stop(proc)                     # it ignored the signal: the usual escalation
                rc = 128 + got[0][0]
                break
            continue
        now = tree_cpu(proc.pid)
        if now != last:
            last, idle_since = now, time.monotonic()
        longest = max(longest, time.monotonic() - idle_since)
        if time.monotonic() - idle_since >= idle:
            why = f"hang: its processes spent no CPU for {idle:.0f} s"
        elif now > cpu_budget:
            why = f"runaway: it spent over {cpu_budget:.0f} s of CPU"
        elif time.monotonic() - t0 > backstop:
            why = f"backstop: it ran past {backstop:.0f} s without ending"
        if why:
            sys.stderr.write(f"\nhang_watch: stopped {' '.join(cmd)[:200]} — {why}\n")
            sys.stderr.flush()
            _stop(proc)
            rc = 124
            break
    if log:
        with open(log, "a") as f:
            f.write(f"{last:.1f} {longest:.1f} {time.monotonic() - t0:.1f}\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cpu-budget", type=float, required=True, help="CPU seconds before it is a runaway")
    ap.add_argument("--idle", type=float, default=float(os.environ.get("HANG_WATCH_IDLE_S") or 120),
                    help="seconds without any CPU before it is a hang (default 120)")
    ap.add_argument("--backstop-s", type=float, default=float(os.environ.get("HANG_WATCH_BACKSTOP_S") or 1800),
                    help="seconds before a command that never ends is stopped (default 1800)")
    ap.add_argument("--log", default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")
    return watch(cmd, a.cpu_budget, a.idle, a.backstop_s, log=a.log)


if __name__ == "__main__":
    sys.exit(main())
