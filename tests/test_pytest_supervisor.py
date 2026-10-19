"""scripts/pytest_supervisor.py ends every run with a verdict: the hangs a CPU
reading cannot see (a poller, a leaked polling thread, a C call holding the
GIL, a module that hangs while it is imported) are each caught, their stacks
reach the output whatever pytest captures, and a test that works is left be.
The cases are the review's probes of the earlier, in-process detector."""
from __future__ import annotations

import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SUPERVISOR = HERE.parent / "scripts" / "pytest_supervisor.py"

CASES = {
    "poller": ({"test_poll.py": "import time\ndef test_poll_hang():\n    while True:\n        time.sleep(0.01)\n"
                                "def test_after():\n    pass\n"}, []),
    "leaked_poller": ({"test_leak.py": (
        "import threading, time\n"
        "def test_a_leaks_a_poller():\n"
        "    def spin():\n        while True:\n            time.sleep(0.05)\n"
        "    threading.Thread(target=spin, daemon=True).start()\n"
        "def test_b_hangs_idle():\n    time.sleep(10_000)\n"
        "def test_c_after():\n    pass\n")}, []),
    "idle": ({"test_cases.py": (
        "import subprocess, time\n"
        "def test_idle_sleep():\n    time.sleep(10_000)\n"
        "def test_idle_child():\n    subprocess.run(['sleep', '10000'])\n"
        "def test_after():\n    assert True\n")}, []),
    "c_call": ({"test_c.py": (
        "import re\n"
        "def test_c_runaway():\n    re.match(r'(a+)+$', 'a' * 64 + 'b')\n"
        "def test_next():\n    pass\n")}, []),
    "c_block": ({"test_block.py": (
        "def test_one_c_call():\n    sum(range(10 ** 12))\n"
        "def test_next():\n    pass\n")}, []),
    "import": ({"test_import_hang.py": "import time\ntime.sleep(10_000)\ndef test_x():\n    pass\n"}, []),
    "working": ({"test_work.py": (
        "import subprocess, sys, time\n"
        "def test_busy_child_for_2_s():\n"
        "    subprocess.run([sys.executable, '-c', 'import time\\nt = time.time()\\n"
        "while time.time() - t < 2: pass'], check=True)\n"
        "def test_sleeps_2_s():\n    time.sleep(2)\n")}, []),
}


def run_case(name: str, tmp: Path) -> tuple[int, str]:
    d = tmp / name
    d.mkdir()
    shutil.copy(HERE / "conftest.py", d / "conftest.py")
    files, _ = CASES[name]
    for fn, src in files.items():
        (d / fn).write_text(src)
    # Short limits so the probes end in seconds; the lane's own are 300/300/900.
    r = subprocess.run([sys.executable, str(SUPERVISOR), "--test-s", "3", "--collect-s", "5",
                        "--grace-s", "5", "--backstop-s", "120", "--",
                        sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", str(d)],
                       capture_output=True, text=True, cwd=d, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_every_probe_ends_with_a_verdict(tmp_path):
    probes = [n for n in CASES if n != "working"]
    with ThreadPoolExecutor(len(probes)) as pool:
        got = dict(zip(probes, pool.map(lambda n: run_case(n, tmp_path), probes)))

    rc, out = got["poller"]
    assert rc == 1 and "hang: test_poll.py::test_poll_hang ran over 3 s and was failed" in out, out
    assert "1 failed, 1 passed" in out and 'test_poll.py", line 4 in test_poll_hang' in out, out  # its stack

    rc, out = got["leaked_poller"]
    assert rc == 1 and "hang: test_leak.py::test_b_hangs_idle ran over 3 s" in out, out
    assert "1 failed, 2 passed" in out, out

    rc, out = got["idle"]
    assert rc == 1 and "test_cases.py::test_idle_sleep ran over" in out and \
        "test_cases.py::test_idle_child ran over" in out and "2 failed, 1 passed" in out, out

    # A catastrophic regex holds the GIL (it froze the in-process watchdog),
    # but the regex engine checks for signals: the test is failed and the run
    # goes on.
    rc, out = got["c_call"]
    assert rc == 1 and "hang: test_c.py::test_c_runaway ran over 3 s and was failed" in out, out
    assert "1 failed, 1 passed" in out and 'test_c.py", line 3 in test_c_runaway' in out, out

    # One C call that never checks for signals cannot be failed from inside:
    # its stack is still written (faulthandler needs no GIL) and the run is
    # stopped with that verdict.
    rc, out = got["c_block"]
    assert rc == 124, out
    assert "hang: test_block.py::test_one_c_call was failed after 3 s and did not stop" in out, out
    assert 'test_block.py", line 2 in test_one_c_call' in out, out

    rc, out = got["import"]
    assert rc == 124 and "hang while collecting" in out and "test_import_hang.py\", line 2" in out, out


def test_slow_but_working_tests_pass(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    shutil.copy(HERE / "conftest.py", d / "conftest.py")
    (d / "test_work.py").write_text(CASES["working"][0]["test_work.py"])
    r = subprocess.run([sys.executable, str(SUPERVISOR), "--test-s", "5", "--",
                        sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", str(d)],
                       capture_output=True, text=True, cwd=d, timeout=300)
    assert r.returncode == 0 and "2 passed" in r.stdout and "pytest_supervisor ==" not in r.stdout, r.stdout


def test_zero_turns_the_per_test_limit_off(tmp_path):
    d = tmp_path / "z"
    d.mkdir()
    shutil.copy(HERE / "conftest.py", d / "conftest.py")
    (d / "test_ok.py").write_text("def test_ok():\n    pass\n")
    r = subprocess.run([sys.executable, str(SUPERVISOR), "--test-s", "0", "--",
                        sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", str(d)],
                       capture_output=True, text=True, cwd=d, timeout=300)
    assert r.returncode == 0 and "per-test hang limit off" in r.stdout and "1 passed" in r.stdout, r.stdout


def test_a_sigterm_ends_the_run_in_order_and_leaves_nothing(tmp_path):
    """SIGTERM to the supervisor stops the run it started (a separate session,
    which the signal would not reach) the orderly way: the running test's
    teardown runs and pytest prints its interrupt summary. Real signals, so the
    whole thing runs in a private PID namespace: the kill is sent from a shell
    inside it, and nothing of the host's is signalled."""
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not installed")
    d = tmp_path / "t"
    d.mkdir()
    shutil.copy(HERE / "conftest.py", d / "conftest.py")
    (d / "test_stuck.py").write_text(
        "import pathlib, pytest, time\n"
        "@pytest.fixture\n"
        "def mark():\n"
        "    pathlib.Path('started').write_text('1')\n"
        "    yield\n"
        "    pathlib.Path('torn-down').write_text('1')\n"
        "def test_stuck(mark):\n    time.sleep(10_000)\n")
    script = (
        f'{sys.executable} {SUPERVISOR} --test-s 0 -- {sys.executable} -m pytest -q --color=no '
        f'-p no:cacheprovider test_stuck.py > out.txt 2>&1 & sup=$!\n'
        # Until the test is running (never a fixed sleep), then ask it to stop.
        'for i in $(seq 1 600); do [ -e started ] && break; sleep 0.1; done\n'
        'kill -TERM $sup; wait $sup; echo "rc=$?" > rc.txt\n'
        'echo "left=$(ps -e -o args= | grep -c "[p]ytest -q")" >> rc.txt\n')
    r = subprocess.run(["unshare", "--pid", "--fork", "--mount-proc", "sh", "-c", script],
                       capture_output=True, text=True, cwd=d, timeout=120)
    if not (d / "rc.txt").exists():
        pytest.skip(f"no private PID namespace here: {r.stderr.strip()[:200]}")
    rc = (d / "rc.txt").read_text()
    out = (d / "out.txt").read_text()
    assert "rc=143" in rc and "left=0" in rc, (rc, out)
    assert (d / "torn-down").exists(), out
    assert "KeyboardInterrupt" in out and "was sent SIGTERM" in out, out
