#!/usr/bin/env python3
"""Run a pytest command and always end with a verdict, even when a test hangs.

    python3 scripts/pytest_supervisor.py [--test-s N] [--collect-s N] [--backstop-s N] \\
        -- python3 -m pytest tests/ -q -p no:cacheprovider

The supervisor is a separate process, so nothing the tests do can freeze it:
not a poller that keeps the process busy, not a thread an earlier test leaked,
not one C call that holds the GIL. tests/conftest.py writes each step to a
progress file (collecting, start <test>, end <test>), and the supervisor reads
it and judges by the clock on its side:

  - a test still running after --test-s seconds is a HANG. Every thread's stack
    is written (faulthandler, which needs no GIL) to a file the supervisor
    prints, and the test is failed (SIGUSR2); the run goes on. If the test
    does not end within --grace-s of being failed (it is inside a call that
    never returns to Python), the run is stopped there;
  - collecting for over --collect-s seconds is a hang while collecting: the
    stacks are written and the run is stopped;
  - --backstop-s is the run's own limit, a hang BACKSTOP, not a speed budget:
    several times the slowest run measured on a loaded box.

These are generous by design (the slowest test measured under load is a few
seconds; see the defaults) and only end a run that is not progressing. Exit
status: pytest's own, or 124 when the supervisor stopped the run. Every
verdict and stack is printed on the supervisor's stdout, which pytest's
capture never sees. CSERIES_TEST_HANG_S=0 turns the per-test limit off (the
collection limit and the backstop still apply).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time

# Measured 2026-10-01 on the build machine: the whole tests/ lane took 38.6 s alone and
# 82-84 s as one of three full gate runs at once beside 16 busy loops (load
# 71). The slowest single tests (the Playwright ones) took a few seconds.
TEST_S = 300.0        # one test
COLLECT_S = 300.0     # importing and collecting every test file
BACKSTOP_S = 900.0    # the whole run: over ten times its slowest loaded run
GRACE_S = 30.0        # a failed test's time to stop


def _read_progress(path: str, pos: int) -> tuple[list[tuple[float, str]], int]:
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
    except FileNotFoundError:
        return [], pos
    end = data.rfind(b"\n") + 1
    out = []
    for line in data[:end].decode(errors="replace").splitlines():
        stamp, _, msg = line.partition(" ")
        try:
            out.append((float(stamp), msg))
        except ValueError:
            continue
    return out, pos + end


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM first: tests/conftest.py turns it into the same interrupt a
    Ctrl-C raises, so pytest ends its session in order (teardown runs, the
    summary prints). SIGKILL only for a run still alive 10 s later, i.e. one
    inside a call that never returns to Python."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def supervise(cmd: list[str], test_s: float, collect_s: float, backstop_s: float,
              grace_s: float = GRACE_S, poll: float = 0.5) -> int:
    work = tempfile.mkdtemp(prefix="pytest-supervisor-")
    progress, stacks = os.path.join(work, "progress"), os.path.join(work, "stacks")
    env = {**os.environ, "CSERIES_TEST_PROGRESS": progress, "CSERIES_TEST_STACKS": stacks}
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, env=env, start_new_session=True)
    # The run lives in its own session, so a SIGTERM/SIGINT aimed at this
    # process would not reach it and would leave it running: they are taken
    # here, and the run is stopped the same orderly way as any other stop.
    asked: list[int] = []
    old = {s: signal.signal(s, lambda n, f: asked.append(n)) for s in (signal.SIGTERM, signal.SIGINT)}
    pos, phase, since, test = 0, "collect", t0, None      # starting up counts as collecting
    told: tuple[str, float] | None = None       # (test, when) a test was failed for hanging
    verdicts: list[str] = []
    stopped = None
    try:
        while True:
            try:
                rc = proc.wait(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                pass
            if asked:
                stopped = f"stopped: the supervisor was sent {signal.Signals(asked[0]).name}; the run was stopped"
                _stop(proc)
                rc = 128 + asked[0]
                break
            lines, pos = _read_progress(progress, pos)
            for stamp, msg in lines:
                kind, _, arg = msg.partition(" ")
                if kind == "start":
                    phase, since, test = "test", stamp, arg
                elif kind == "end":
                    if told and told[0] == arg:
                        told = None
                    phase, since, test = "between", stamp, None
                elif kind in ("collect", "collected", "done"):
                    phase, since, test = kind, stamp, None
            now = time.monotonic()
            if now - t0 > backstop_s:
                stopped = f"backstop: the run went past {backstop_s:.0f} s (phase: {phase}" + \
                          (f", in {test}" if test else "") + ")"
            elif told and now - told[1] > grace_s:
                stopped = (f"hang: {told[0]} was failed after {test_s:.0f} s and did not stop within "
                           f"{grace_s:.0f} s (inside a call that never returns to Python); the run was stopped")
            elif phase == "collect" and now - since > collect_s:
                stopped = f"hang while collecting: no test file finished importing within {collect_s:.0f} s"
            elif phase == "test" and test_s > 0 and not told and now - since > test_s:
                verdicts.append(f"hang: {test} ran over {test_s:.0f} s and was failed; its stacks are below")
                os.kill(proc.pid, signal.SIGUSR1)            # faulthandler: every thread's stack
                time.sleep(0.2)
                os.kill(proc.pid, signal.SIGUSR2)            # conftest: fail the running test
                told = (test, now)
            if stopped:
                try:
                    os.kill(proc.pid, signal.SIGUSR1)
                    time.sleep(0.5)
                except ProcessLookupError:
                    pass
                _stop(proc)
                rc = 124
                break
    finally:
        if proc.poll() is None:
            _stop(proc)
        for sg, h in old.items():
            signal.signal(sg, h)
    if stopped:
        verdicts.append(stopped)
    if verdicts:
        print("\n== pytest_supervisor ==")
        for v in verdicts:
            print(v)
        try:
            dump = open(stacks, errors="replace").read().strip()
        except FileNotFoundError:
            dump = ""
        if dump:
            print("-- stacks (faulthandler) --")
            print(dump[-20000:])
        sys.stdout.flush()
    for p in (progress, stacks):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    os.rmdir(work)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    env_test = os.environ.get("CSERIES_TEST_HANG_S")
    ap.add_argument("--test-s", type=float, default=float(env_test) if env_test else TEST_S)
    ap.add_argument("--collect-s", type=float, default=COLLECT_S)
    ap.add_argument("--backstop-s", type=float, default=BACKSTOP_S)
    ap.add_argument("--grace-s", type=float, default=GRACE_S)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")
    if a.test_s <= 0:
        print("pytest_supervisor: per-test hang limit off (CSERIES_TEST_HANG_S=0); "
              f"the {a.collect_s:.0f} s collection limit and {a.backstop_s:.0f} s backstop still apply")
    return supervise(cmd, a.test_s, a.collect_s, a.backstop_s, a.grace_s)


if __name__ == "__main__":
    sys.exit(main())
