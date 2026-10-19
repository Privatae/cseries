"""connector_gate_kit.log_error_detail: a failed "the logs hold no error" check
must carry the error's own line on the one line the gate report keeps.

A CI run failed test_connector_discord_example.py's check M with
only "Exception in thread Thread-2 (work_forever):" in the report; the line
naming the error was printed further down and dropped by the report.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import connector_gate_kit as K  # noqa: E402

LOG = """[discord-connector] connecting
[discord-connector] listening
Exception in thread Thread-2 (work_forever):
Traceback (most recent call last):
  File "/usr/lib/python3.12/threading.py", line 1075, in _bootstrap_inner
    self.run()
  File "/app/bin/run.py", line 88, in work_forever
    raise ConnectionResetError("the gateway closed the socket")
ConnectionResetError: the gateway closed the socket
[discord-connector] connecting"""


def _bad(log: str) -> int:
    return next(i for i, ln in enumerate(log.splitlines()) if ln.startswith("Exception in thread"))


def test_the_first_line_names_the_error():
    detail = K.log_error_detail(LOG, _bad(LOG))
    first = detail.splitlines()[0]
    assert first.startswith("Exception in thread Thread-2 (work_forever):")
    assert "ConnectionResetError: the gateway closed the socket" in first


def test_the_lines_before_it_follow_on_the_same_line():
    first = K.log_error_detail(LOG, _bad(LOG)).splitlines()[0]
    assert "(before it: [discord-connector] connecting ⏎ [discord-connector] listening)" in first


def test_a_match_on_the_first_line_has_no_before_part():
    detail = K.log_error_detail("Traceback (most recent call last):\nValueError: x", 0)
    assert detail == "Traceback (most recent call last): ⏎ ValueError: x"


# A real thread traceback as Python 3.14 prints it (the pods' runtime,
# sidecars/app-runtime-python/Dockerfile): the threading frames and their caret
# lines put the cause 27 lines below the match, past the 14-line window.
DEEP = """[discord-connector] listening
Exception in thread Thread-2 (work_forever):
Traceback (most recent call last):
  File "/usr/lib/python3.14/threading.py", line 1082, in _bootstrap_inner
    self._context.run(self.run)
    ~~~~~~~~~~~~~~~~~^^^^^^^^^^
  File "/usr/lib/python3.14/threading.py", line 1024, in run
    self._target(*self._args, **self._kwargs)
    ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/connector.py", line 10, in work_forever
    asyncio.run(main())
    ~~~~~~~~~~~^^^^^^^^
  File "/usr/lib/python3.14/asyncio/runners.py", line 204, in run
    return runner.run(main)
           ~~~~~~~~~~^^^^^^
  File "/usr/lib/python3.14/asyncio/runners.py", line 127, in run
    return self._loop.run_until_complete(task)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/usr/lib/python3.14/asyncio/base_events.py", line 719, in run_until_complete
    return future.result()
           ~~~~~~~~~~~~~^^
  File "/app/connector.py", line 8, in main
    await session()
  File "/app/connector.py", line 6, in session
    await connect()
  File "/app/connector.py", line 4, in connect
    raise ConnectionResetError("the gateway closed the socket")
ConnectionResetError: the gateway closed the socket
[discord-connector] connecting"""


def test_a_deep_traceback_still_names_its_cause_second():
    detail = K.log_error_detail(DEEP, _bad(DEEP))
    assert detail.startswith("Exception in thread Thread-2 (work_forever): ⏎ "
                             "ConnectionResetError: the gateway closed the socket ⏎ Traceback")
    assert detail.count("ConnectionResetError: the gateway") == 1


def test_a_match_on_the_traceback_line_names_its_cause_too():
    i = DEEP.splitlines().index("Traceback (most recent call last):")
    assert K.log_error_detail(DEEP, i).startswith(
        "Traceback (most recent call last): ⏎ ConnectionResetError: the gateway closed the socket")


def test_a_line_that_is_no_traceback_is_left_as_it_was():
    log = "a\nERROR: the token was refused\n  more\nb"
    assert K.log_error_detail(log, 1) == "ERROR: the token was refused ⏎ more ⏎ b   (before it: a)"


def test_the_check_prints_it_within_its_limit():
    # check() keeps 1500 characters; a traceback's cause must land well inside.
    detail = K.log_error_detail(LOG, _bad(LOG))
    assert detail.index("ConnectionResetError:") < 1500
