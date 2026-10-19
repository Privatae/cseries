"""scripts/hang_watch.py: a gate is stopped when it hangs or runs away, never
for being slow, and the process tree is what counts."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HW = Path(__file__).resolve().parents[1] / "scripts" / "hang_watch.py"
PY = sys.executable


def watch(*args: str, code: str) -> tuple[int, str, float]:
    t0 = time.monotonic()
    r = subprocess.run([PY, str(HW), *args, "--", PY, "-c", code], capture_output=True, text=True)
    return r.returncode, r.stderr, time.monotonic() - t0


def test_its_own_exit_status_when_it_ends():
    rc, err, _ = watch("--cpu-budget", "60", "--idle", "30", code="import sys; sys.exit(3)")
    assert rc == 3 and "hang_watch" not in err


def test_a_process_that_spends_no_cpu_is_a_hang():
    rc, err, took = watch("--cpu-budget", "60", "--idle", "2", code="import time; time.sleep(600)")
    assert rc == 124 and "hang: its processes spent no CPU for 2 s" in err and took < 60


def test_a_process_that_spends_too_much_cpu_is_a_runaway():
    rc, err, took = watch("--cpu-budget", "2", "--idle", "60", code="while True: pass")
    assert rc == 124 and "runaway: it spent over 2 s of CPU" in err and took < 60


def test_slow_but_working_is_never_stopped():
    # Longer than --idle by the wall clock, spending CPU all along.
    rc, err, took = watch("--cpu-budget", "60", "--idle", "1", code=(
        "import time\nend = time.monotonic() + 3\nwhile time.monotonic() < end: pass"))
    assert rc == 0 and took >= 3 and "hang_watch" not in err


def test_a_child_doing_the_work_counts_as_progress():
    # The watched process only waits; its child spends the CPU.
    rc, err, _ = watch("--cpu-budget", "60", "--idle", "1", code=(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'import time\\nend = time.monotonic() + 3\\n"
        "while time.monotonic() < end: pass'], check=True)"))
    assert rc == 0 and "hang_watch" not in err


def test_a_hung_child_stops_the_whole_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    rc, err, _ = watch("--cpu-budget", "60", "--idle", "2", code=(
        "import subprocess, sys\n"
        f"p = subprocess.Popen(['sleep', '600']); open({str(pidfile)!r}, 'w').write(str(p.pid)); p.wait()"))
    pid = int(pidfile.read_text())
    gone = not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] == "Z"
    assert rc == 124 and "hang:" in err and gone


def test_a_poller_that_never_ends_meets_the_backstop_with_its_stack():
    # Spends a little CPU forever, so neither the idle rule nor the CPU budget
    # sees it; the backstop does, and its stack is in the output.
    rc, err, took = watch("--cpu-budget", "600", "--idle", "60", "--backstop-s", "3",
                          code="import time\nwhile True:\n    time.sleep(0.01)\n")
    assert rc == 124 and "backstop: it ran past 3 s without ending" in err and took < 60
    assert 'File "<string>", line 3 in <module>' in err, err


def test_a_c_call_holding_the_gil_still_leaves_its_stack():
    rc, err, _ = watch("--cpu-budget", "2", "--idle", "60", code="sum(range(10 ** 12))\n")
    assert rc == 124 and "runaway: it spent over 2 s of CPU" in err
    assert 'File "<string>", line 1 in <module>' in err, err


def test_an_idle_hang_leaves_its_stack():
    rc, err, _ = watch("--cpu-budget", "60", "--idle", "2", code="import time\ntime.sleep(600)\n")
    assert rc == 124 and 'File "<string>", line 2 in <module>' in err, err


def test_sigterm_and_sigint_reach_the_command_and_leave_nothing():
    """Real signals, so the whole case runs in a private PID namespace: the
    signals are sent from a shell inside it and nothing of the host's is hit."""
    import shutil
    import pytest
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not installed")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        script = (
            'for sig in TERM INT; do\n'
            f'  rm -f {d}/up; {PY} {HW} --cpu-budget 600 --idle 600 -- {PY} -c '
            f'"import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: (open(\'{d}/got\',\'a\').write(\'T\'), sys.exit(0))); '
            f'signal.signal(signal.SIGINT, lambda *a: (open(\'{d}/got\',\'a\').write(\'I\'), sys.exit(0))); '
            f'open(\'{d}/up\',\'w\').write(\'1\'); time.sleep(600)" & hw=$!\n'
            f'  for i in $(seq 1 300); do [ -e {d}/up ] && break; sleep 0.1; done\n'
            '  kill -$sig $hw; wait $hw; echo "$sig rc=$?" >> ' + f'{d}/rc\n'
            'done\n'
            f'echo "left=$(ps -e -o args= | grep -c "[t]ime.sleep(600)")" >> {d}/rc\n')
        subprocess.run(["unshare", "--pid", "--fork", "--mount-proc", "sh", "-c", script],
                       capture_output=True, text=True, timeout=120)
        rc = Path(d, "rc")
        if not rc.exists():
            pytest.skip("no private PID namespace here")
        got = rc.read_text()
        assert "TERM rc=143" in got and "INT rc=130" in got and "left=0" in got, got
        assert Path(d, "got").read_text() == "TI"
