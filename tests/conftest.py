"""This file does two things.

1. A Chromium-driven test SKIPS on a machine with no browser, and FAILS in the
   gate lane, where a browser is expected.

   scripts/pre_merge_gates.sh runs this suite with CSERIES_REQUIRE_CHROMIUM=1. A
   skip there prints the same green summary as a pass, so a lane whose browser
   went missing would keep reporting the desktop render check, the shell's bar
   hand-over and the app windows as proved while proving none of them. Here a
   skip whose reason is the browser (the `chromium unavailable` /
   `playwright not available` skips these files raise) becomes a failure under
   that flag, whether it happened at collection (pytest.importorskip at module
   level) or inside a test or fixture.

2. What scripts/pytest_supervisor.py needs from inside a test run.

   pre_merge_gates.sh runs this lane under that supervisor, a separate process
   that judges hangs on its own clock (see its docstring). From inside, this
   plugin only:

     - writes each step to the progress file the supervisor reads
       (CSERIES_TEST_PROGRESS): "collect", "collected", "start <test>",
       "end <test>", "done", each stamped with the system's monotonic clock;
     - lets faulthandler write every thread's stack to the supervisor's file
       (CSERIES_TEST_STACKS) on SIGUSR1. faulthandler writes from its own signal
       handler, without the GIL, straight to that file, so neither a C call that
       holds the GIL nor pytest's output capture can keep the stacks from it;
     - turns SIGTERM into the interrupt a Ctrl-C raises, so a run the supervisor
       stops ends its session in order (teardown, summary) instead of dying mid-test;
     - fails the running test on SIGUSR2, with the supervisor's reason.

   Without the supervisor (the variables unset) it does nothing.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import time

import pytest

_BROWSER_SKIPS = ("chromium unavailable", "playwright not available")


def _required() -> bool:
    return os.environ.get("CSERIES_REQUIRE_CHROMIUM") == "1"


def _reason(rep) -> str:
    lr = rep.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2])
    return str(lr or "")


def _fail_browser_skip(rep) -> None:
    if not (_required() and rep.skipped):
        return
    reason = _reason(rep)
    if any(s in reason for s in _BROWSER_SKIPS):
        rep.outcome = "failed"
        rep.longrepr = ("Chromium is required in this lane (CSERIES_REQUIRE_CHROMIUM=1), "
                        f"but the test skipped: {reason}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    _fail_browser_skip(outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    _fail_browser_skip(outcome.get_result())

_PROGRESS = os.environ.get("CSERIES_TEST_PROGRESS")
_STACKS = os.environ.get("CSERIES_TEST_STACKS")
_state: dict = {"test": None, "fd": None, "stacks": None}


def _note(msg: str) -> None:
    if _state["fd"] is not None:
        os.write(_state["fd"], f"{time.monotonic():.3f} {msg}\n".encode(errors="replace"))


def _fail_running_test(signum, frame):
    if _state["test"]:
        pytest.fail(f"hang: {_state['test']} ran past the supervisor's per-test limit "
                    "(its stacks are in the supervisor's report)", pytrace=False)


def _stop_session(signum, frame):
    raise KeyboardInterrupt(f"stopped by {signal.Signals(signum).name}")


def pytest_configure(config):
    if not _PROGRESS:
        return
    _state["fd"] = os.open(_PROGRESS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if _STACKS:
        _state["stacks"] = open(_STACKS, "a")            # held open: faulthandler writes to its fd
        faulthandler.register(signal.SIGUSR1, file=_state["stacks"], all_threads=True)
    signal.signal(signal.SIGUSR2, _fail_running_test)
    signal.signal(signal.SIGTERM, _stop_session)
    _note("collect")


def pytest_collection_finish(session):
    _note("collected")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _state["test"] = item.nodeid
    _note(f"start {item.nodeid}")
    try:
        return (yield)
    finally:
        _state["test"] = None
        _note(f"end {item.nodeid}")


def pytest_unconfigure(config):
    _note("done")
