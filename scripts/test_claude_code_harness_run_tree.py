#!/usr/bin/env python3
"""claude-code harness: an idle stop kills THAT RUN's processes, and nothing else.

/run/stream stops a run that has gone quiet (no total cap).
The SDK's own close terminates the CLI alone, so a wedged tool — the case the
stop fires for — outlived the run. _stop_run_tree now freezes
and kills the run's CLI and everything under it first. One pod can hold several
runs, so "the run's" is decided by a per-run marker in the CLI's environment
(CSERIES_HARNESS_RUN) — never by cwd, which the default project_dir shares with
the server's own login and version-check children, and which the CLI can
change itself.

This file sends REAL signals, so it runs only inside a private PID namespace
(the test_harness_stop.py pattern) and FAILS LOUDLY (exit 2) where one cannot
be made — it never runs on the host. The real-signal check also routes every
signal through a fence built from /proc by this test, never by the code under
test.

  APP_ROOT=<repo> python3 scripts/test_claude_code_harness_run_tree.py
"""
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
PASSES = 0
FAILS: list = []


def check(name, ok, detail=""):
    global PASSES
    if ok:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail != "" else ""))


def _load():
    sys.path.insert(0, os.path.join(_ROOT, "cli"))
    sys.path.insert(0, os.path.join(_ROOT, "system-apps"))
    spec = importlib.util.spec_from_file_location(
        "cch_main", os.path.join(_ROOT, "system-apps/claude-code-harness/bin/main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_targets(CCH):
    """The target set, from fake pids — nothing is signalled."""
    me = 500
    ppids = {1: 0, me: 1,
             600: me, 601: 600, 602: 601,      # this run: CLI, tool, tool's child
             603: 600,                          # setsid'd by the tool: parent link kept
             700: me, 701: 700,                 # ANOTHER run's CLI + tool, same server
             650: me,                           # a login / version check: no marker
             800: 1, 801: 800}                  # not the server's at all
    mark = {600: "run-a", 601: "run-a", 700: "run-b", 800: "run-a"}   # 602 dropped it
    got = CCH._run_tree_targets(ppids, lambda p: mark.get(p), me, "run-a")
    # MUTATION: root at every child of the server (drop the marker test).
    # RESULT: red — 650, 700, 701 included.
    check("this run's CLI and every descendant (setsid'd, and one that dropped the marker)",
          got == {600, 601, 602, 603}, sorted(got))
    check("another run's CLI on the same server is not a target",
          not (got & {700, 701}), sorted(got))
    check("a login or version check under the server (no marker) is not a target",
          650 not in got, sorted(got))
    check("a process outside the server with the same marker is not a target",
          not (got & {800, 801}), sorted(got))
    # A child whose marker variable is present but EMPTY reads as "", so an
    # empty run marker would match it. MUTATION: drop the `if not marker`
    # guard.  RESULT: red — 650 targeted.
    check("an EMPTY marker targets nothing, not a child with an empty variable",
          CCH._run_tree_targets({1: 0, me: 1, 650: me}, lambda p: "", me, "") == set())
    check("no CLI with that marker: nothing to stop",
          CCH._run_tree_targets(ppids, lambda p: mark.get(p), me, "run-none") == set())


def test_orphans(CCH):
    """The run's processes already under pid 1 (the CLI exited first — the
    SDK's close waits for it): found by marker, inside the harness container
    only, and behind the second guard. Recorded signals."""
    me = os.getpid()
    ppids = {1: 0, me: 1, 900: 1, 901: 900, 902: 1, 950: 1}
    mark = {900: "run-a", 902: "run-b"}
    check("outside a harness container the orphans are not looked for",
          CCH._run_tree_targets(ppids, lambda p: mark.get(p), me, "run-a", orphans=False) == set())
    check("inside one, this run's orphan and its child are targets; another run's is not",
          CCH._run_tree_targets(ppids, lambda p: mark.get(p), me, "run-a", orphans=True) == {900, 901})
    for recheck, want in ((True, {900, 901}), (False, set())):
        log: list = []
        CCH._stop_run_tree("run-a", signal_fn=lambda p, s: log.append((p, s)),
                           ppids_fn=lambda: ppids, marker_fn=lambda p: mark.get(p),
                           in_container_fn=lambda: True, recheck_fn=lambda: recheck)
        # MUTATION: drop recheck_fn from the signal guard.  RESULT: red.
        check(f"orphans are signalled only when the separate container recheck "
              f"also holds (recheck={recheck})", {p for p, _ in log} == want, log)
    # The FIRST guard, through _stop_run_tree itself: outside a harness
    # container orphans are never looked for, even with the recheck answering
    # true. MUTATION: `orphans = True` in _stop_run_tree.  RESULT: red.
    log = []
    CCH._stop_run_tree("run-a", signal_fn=lambda p, s: log.append((p, s)),
                       ppids_fn=lambda: ppids, marker_fn=lambda p: mark.get(p),
                       in_container_fn=lambda: False, recheck_fn=lambda: True)
    check("outside a harness container _stop_run_tree signals no orphan, "
          "whatever the recheck says", log == [], log)


def test_second_guard(CCH):
    """With the target set forced wrong, only the server's own descendants are
    signalled. Recorded signals."""
    me = os.getpid()
    ppids = {1: 0, me: 1, 901: me, 50: 1}
    real = CCH._run_tree_targets
    CCH._run_tree_targets = lambda *a, **k: {901, 50, 1}
    log: list = []
    try:
        CCH._stop_run_tree("run-x", signal_fn=lambda p, s: log.append((p, s)),
                           ppids_fn=lambda: ppids, marker_fn=lambda p: None)
    finally:
        CCH._run_tree_targets = real
    hit = {p for p, _ in log}
    # MUTATION: remove the _descends_from check.  RESULT: red — 50 and 1 hit.
    check("a wrong target set cannot reach past the server's own tree", hit == {901}, sorted(hit))
    check("frozen before it is killed",
          log == [(901, signal.SIGSTOP), (901, signal.SIGKILL)], log)


def _children_from_proc(parent: int) -> list[int]:
    out = []
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            raw = Path(f"/proc/{e}/stat").read_text()
            if int(raw[raw.rindex(")") + 2:].split()[1]) == parent:
                out.append(int(e))
        except (OSError, ValueError, IndexError):
            continue
    return out


def test_real(CCH):
    """REAL SIGNALS, inside the private PID namespace only. Under this process:
    run A (a CLI stand-in that chdir'd away, with a tool and a setsid'd
    grandchild), run B, and a login-like child with no marker in run A's own
    directory. Stopping A leaves B and the login running."""
    a_dir, b_dir = tempfile.mkdtemp(prefix="run-a-"), tempfile.mkdtemp(prefix="run-b-")
    env = {k: v for k, v in os.environ.items() if k != CCH._RUN_MARKER_ENV}
    run_a = subprocess.Popen(["sh", "-c", "cd /; sleep 300 & setsid sleep 300 & wait"],
                             cwd=a_dir, env={**env, CCH._RUN_MARKER_ENV: "run-a"})
    run_b = subprocess.Popen(["sleep", "300"], cwd=b_dir, env={**env, CCH._RUN_MARKER_ENV: "run-b"})
    login = subprocess.Popen(["sleep", "300"], cwd=a_dir, env=env)
    try:
        deadline = time.time() + 5
        grand: list[int] = []
        while time.time() < deadline:
            grand = _children_from_proc(run_a.pid)
            if len(grand) >= 2:
                break
            time.sleep(0.02)
        check("run A's CLI stand-in really changed its own directory",
              os.readlink(f"/proc/{run_a.pid}/cwd") == "/", os.readlink(f"/proc/{run_a.pid}/cwd"))
        check("the real /proc environ read finds each marker, with no capabilities",
              CCH._marker_of(run_a.pid) == "run-a" and CCH._marker_of(run_b.pid) == "run-b"
              and CCH._marker_of(login.pid) is None)
        allowed = {run_a.pid, *grand}
        refused: list[int] = []

        def fenced(pid, sig):
            if pid not in allowed:
                refused.append(pid)
                raise PermissionError(f"test fence: {pid} is not run A's")
            os.kill(pid, sig)

        stopped = set(CCH._stop_run_tree("run-a", signal_fn=fenced))
        check("run A's CLI, its tool and its setsid'd child are stopped",
              len(grand) >= 2 and allowed <= stopped, (sorted(stopped), sorted(allowed)))
        check("nothing outside run A was asked to be signalled", not refused, refused)
        run_a.wait(timeout=5)
        time.sleep(0.2)
        check("run A is gone", run_a.poll() is not None
              and not any(Path(f"/proc/{g}").exists() and
                          Path(f"/proc/{g}/stat").read_text().split()[2] != "Z" for g in grand))
        check("run B, on the same server, is still running", run_b.poll() is None)
        check("the unmarked login in run A's own directory is still running", login.poll() is None)
    finally:
        for p in (run_a, run_b, login):
            if p.poll() is None:
                p.kill()
                p.wait()


def _cap_eff() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("CapEff:"):
            return int(line.split()[1], 16)
    return -1


def _in_private_pid_namespace() -> bool:
    try:
        return Path("/proc/1/comm").read_text().strip() == "sh" and os.getppid() == 1 \
            and len([e for e in os.listdir("/proc") if e.isdigit()]) < 50
    except OSError:
        return False


def main() -> int:
    if not _in_private_pid_namespace():
        cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
               "sh", "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}"; echo "rc=$?"']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                               env={**os.environ, "APP_ROOT": _ROOT})
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"SKIP-LOUD: no private PID namespace here ({e!r}) — this gate proves nothing "
                  "without one and will not run on the host.", file=sys.stderr)
            return 2
        import re as _re
        m = _re.search(r"rc=(\d+)\s*\Z", r.stdout)
        if not m:
            print("SKIP-LOUD: no private PID namespace here — this gate proves nothing without "
                  "one and will not run on the host.\n" + (r.stdout + r.stderr)[-600:], file=sys.stderr)
            return 2
        print(r.stdout[:m.start()].rstrip())
        return 0 if m.group(1) == "0" else 1
    # The pod drops ALL capabilities (the operator's securityContext), and a
    # process can read /proc/<pid>/environ of its own children only when theirs
    # are no wider than its own. Mapped root in the namespace holds every
    # capability, which would hide a permission failure — so the checks run
    # with all of them dropped, as in the pod.
    if _cap_eff() != 0:
        if os.environ.get("_CCH_RUN_TREE_CAPLESS"):
            print("FAIL: could not drop capabilities", file=sys.stderr)
            return 1
        os.environ["_CCH_RUN_TREE_CAPLESS"] = "1"
        os.execvp("setpriv", ["setpriv", "--no-new-privs", "--inh-caps=-all",
                              "--bounding-set=-all", "--", sys.executable,
                              os.path.abspath(__file__)])
    print("claude-code harness — an idle stop kills that run's processes, and nothing else "
          "(inside a private PID namespace, no capabilities)")
    check("no effective capabilities, as in the pod", _cap_eff() == 0)
    CCH = _load()
    for t in (test_targets, test_orphans, test_second_guard, test_real):
        t(CCH)
    print(f"\n{PASSES}/{PASSES + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
