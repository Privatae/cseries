#!/usr/bin/env python3
"""Gate: pre_merge_gates.sh runs the gates that cover the change first, and
stops there when one of them fails.

Measured: one PR's full run (~15 min) was started six times and one
counted; two of those runs failed on gates that read the changed files, and
the failures were printed only at the end. This drives the real script on a
small stub repo (three gates, a smoke_test.sh that names them, an origin/main
to diff against) and pins:
  1. a failing gate that names a changed file stops the run early: the gates
     that do not cover the change never run, the failure is printed the moment
     it happens and again in the list, and the run exits non-zero
  2. the failure is remembered for this tree and runs first next time
  3. PRE_MERGE_FULL=1 runs everything regardless
  4. a clean pass on a clean tree records the code's tree hash (what
     scripts/gate_pass_recorded.sh reads); a pass on a dirty tree does not
  5. a commit made while the run is going records no pass: the gates that ran
     before it tested other code
  6. a renamed file and a non-ASCII file name still put the gates that name
     them first (`git diff --name-only` lists a rename by its new path only,
     and C-quotes a non-ASCII path)

The stub runs are this gate's own: the PRE_MERGE_* settings of the run that
started it (PRE_MERGE_FULL=1 above all) are not passed down to them.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

APP_ROOT = os.environ.get("APP_ROOT", "/app")
SCRIPT = os.path.join(APP_ROOT, "scripts", "pre_merge_gates.sh")
HANG_WATCH = os.path.join(APP_ROOT, "scripts", "hang_watch.py")
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[-600:]}]"))
    if not cond:
        FAILS.append(label)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout


GATE_A = '''import os, sys, time
src = open(os.path.join(os.environ["APP_ROOT"], "lib/alpha.py")).read()
open(os.path.join(os.environ["MARKS"], "a-ran"), "w").close()
if os.environ.get("A_WAITS_FOR_LANE"):
    # Case 8 asks whether the tests/ lane runs BESIDE the gates. Failing at once
    # raced the lane's own start: on the CI runner, 24 gates at a time, A could
    # fail before pytest had begun. A waits for the
    # lane's start mark first (bounded), so the case tests the runner, not load.
    for _ in range(100):
        if os.path.exists(os.path.join(os.environ["MARKS"], "t-start")):
            break
        time.sleep(0.1)
sys.exit(1 if "BROKEN" in src else 0)
'''
GATE_B = 'print("b fine")\n'
GATE_C = '''import os, time
marks = os.environ["MARKS"]
if os.environ.get("WAIT_PROCEED"):
    open(os.path.join(marks, "c-waiting"), "w").close()
    for _ in range(1200):
        if os.path.exists(os.path.join(marks, "proceed")):
            break
        time.sleep(0.05)
open(os.path.join(marks, "c-ran"), "w").close()
print("c fine")
'''
# Case 6: D reads lib/beta.py by path, E names a non-ASCII file.
GATE_D = '''import os, sys
sys.exit(0 if os.path.isfile(os.path.join(os.environ["APP_ROOT"], "lib/beta.py")) else 1)
'''
GATE_E = '''import os, sys
src = open(os.path.join(os.environ["APP_ROOT"], "lib", "caf\\u00e9.py"), encoding="utf-8").read()
sys.exit(1 if "BROKEN" in src else 0)  # reads café.py
'''

# The outer run's settings, which a stub run must not inherit.
_OUTER = ("PRE_MERGE_", "HANG_WATCH_LOG", "GATE_RESULTS_DIR")
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith(_OUTER)}

with tempfile.TemporaryDirectory() as root:
    repo = os.path.join(root, "repo")
    marks = os.path.join(root, "marks")
    results = os.path.join(root, "results")
    for d in (os.path.join(repo, "scripts", "gates-container-free.d"), os.path.join(repo, "lib"), marks):
        os.makedirs(d)
    shutil.copy(SCRIPT, os.path.join(repo, "scripts", "pre_merge_gates.sh"))
    shutil.copy(HANG_WATCH, os.path.join(repo, "scripts", "hang_watch.py"))
    files = {
        "scripts/smoke_test.sh": "".join(f'python3 "$SMOKE_REPO/scripts/{g}"\n'
                                         for g in ("test_a.py", "test_b.py", "test_c.py")),
        "scripts/test_a.py": GATE_A, "scripts/test_b.py": GATE_B, "scripts/test_c.py": GATE_C,
        "lib/alpha.py": "x = 1\n", "lib/other.py": "y = 2\n",
        "scripts/gates-container-free.d/.keep": "",
    }
    for rel, text in files.items():
        with open(os.path.join(repo, rel), "w") as f:
            f.write(text)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    def start(at=None, **env):
        at = at or repo
        for m in os.listdir(marks):
            os.remove(os.path.join(marks, m))
        e = {**BASE_ENV, "GATE_RESULTS_DIR": results, "MARKS": marks, "PRE_MERGE_JOBS": "2",
             "PRE_MERGE_PYFLAKES": "true", **env}
        return subprocess.Popen(["bash", os.path.join(at, "scripts", "pre_merge_gates.sh"), at],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)

    def run(at=None, **env):
        p = start(at, **env)
        out, err = p.communicate(timeout=300)
        return p.returncode, out, err

    # The change: alpha breaks; gate A reads it, B and C do not.
    with open(os.path.join(repo, "lib/alpha.py"), "w") as f:
        f.write("x = 1  # BROKEN\n")
    git(repo, "commit", "-qam", "break alpha")

    print("\n1. a failing gate that covers the change stops the run early")
    rc, out, err = run()
    check("non-zero exit", rc != 0, (rc, out))
    check("says it stopped early, naming the gate",
          "FAILED EARLY" in out and "- test_a.py" in out, out)
    check("only the gate covering the change ran first", "first: 1 gates" in out, out)
    check("a gate that does not cover the change never ran",
          not os.path.exists(os.path.join(marks, "c-ran")), os.listdir(marks))
    check("the failure was also printed the moment it happened (stderr)",
          "FAIL  test_a.py" in err, err)

    print("\n2. the failure runs first next time")
    lf = [f for f in os.listdir(results) if f.startswith("last-failed-")]
    check("remembered for this tree", lf and "test_a.py" in open(os.path.join(results, lf[0])).read(),
          (lf, os.listdir(results)))

    print("\n3. PRE_MERGE_FULL=1 runs everything")
    rc, out, err = run(PRE_MERGE_FULL="1")
    check("every gate ran, and the run still fails",
          rc != 0 and os.path.exists(os.path.join(marks, "c-ran")) and "PRE-MERGE GATES FAILED" in out,
          (rc, out))

    print("\n4. a clean pass records the code; a dirty tree does not")
    with open(os.path.join(repo, "lib/alpha.py"), "w") as f:
        f.write("x = 1\n")
    with open(os.path.join(repo, "lib/other.py"), "w") as f:
        f.write("y = 3\n")
    rc, out, err = run()
    check("last time's failure ran first and passed",
          "first: 1 gates" in out and "all 1 passed" in out, out)
    check("a pass on a dirty tree passes", rc == 0 and "PRE-MERGE GATES PASSED" in out, (rc, out))
    check("…and records nothing", not any(f.endswith(".pass") for f in os.listdir(results)),
          os.listdir(results))
    git(repo, "commit", "-qam", "fix alpha")
    rc, out, err = run()
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    check("a clean pass", rc == 0 and "PRE-MERGE GATES PASSED" in out, (rc, out))
    check("…records the code's tree hash", os.path.isfile(os.path.join(results, tree + ".pass")),
          os.listdir(results))
    check("…and every gate ran", os.path.exists(os.path.join(marks, "c-ran")), os.listdir(marks))

    print("\n5. a commit made during the run records no pass")
    with open(os.path.join(repo, "lib/other.py"), "w") as f:
        f.write("y = 4\n")
    git(repo, "commit", "-qam", "new code, not yet run")
    started_on = git(repo, "rev-parse", "HEAD^{tree}").strip()
    p = start(WAIT_PROCEED="1")
    for _ in range(1200):  # A has read alpha and C is holding the run open
        if all(os.path.exists(os.path.join(marks, m)) for m in ("a-ran", "c-waiting")):
            break
        time.sleep(0.05)
    with open(os.path.join(repo, "lib/alpha.py"), "w") as f:
        f.write("x = 1  # BROKEN\n")
    git(repo, "commit", "-qam", "broken, committed while the run was going")
    committed = git(repo, "rev-parse", "HEAD^{tree}").strip()
    open(os.path.join(marks, "proceed"), "w").close()
    out, err = p.communicate(timeout=300)
    check("the run itself passed (A read alpha before the commit)",
          p.returncode == 0 and "PRE-MERGE GATES PASSED" in out, (p.returncode, out))
    check("…and no pass is recorded for the committed code, which A would fail",
          not os.path.isfile(os.path.join(results, committed + ".pass")), os.listdir(results))
    check("…nor for the code the run started on, which is no longer the tree",
          not os.path.isfile(os.path.join(results, started_on + ".pass")), os.listdir(results))

    print("\n6. a rename and a non-ASCII name still put the gates that name them first")
    repo2 = os.path.join(root, "repo2")
    os.makedirs(os.path.join(repo2, "scripts", "gates-container-free.d"))
    os.makedirs(os.path.join(repo2, "lib"))
    shutil.copy(SCRIPT, os.path.join(repo2, "scripts", "pre_merge_gates.sh"))
    shutil.copy(HANG_WATCH, os.path.join(repo2, "scripts", "hang_watch.py"))
    files2 = {
        "scripts/smoke_test.sh": "".join(f'python3 "$SMOKE_REPO/scripts/{g}"\n'
                                         for g in ("test_c.py", "test_d.py", "test_e.py")),
        "scripts/test_c.py": GATE_C, "scripts/test_d.py": GATE_D, "scripts/test_e.py": GATE_E,
        "lib/beta.py": "b = 1\n", "lib/café.py": "c = 1\n",
        "scripts/gates-container-free.d/.keep": "",
    }
    for rel, text in files2.items():
        with open(os.path.join(repo2, rel), "w", encoding="utf-8") as f:
            f.write(text)
    git(repo2, "init", "-q")
    git(repo2, "add", "-A")
    git(repo2, "commit", "-q", "-m", "base")
    git(repo2, "update-ref", "refs/remotes/origin/main", "HEAD")
    # git's defaults, whatever this box's config says: rename detection on,
    # non-ASCII paths quoted.
    pinned = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "diff.renames",
              "GIT_CONFIG_VALUE_0": "true", "GIT_CONFIG_KEY_1": "core.quotePath",
              "GIT_CONFIG_VALUE_1": "true"}
    git(repo2, "mv", "lib/beta.py", "lib/gamma.py")
    git(repo2, "commit", "-qm", "rename beta, which D reads")
    rc, out, err = run(repo2, **pinned)
    check("the gate reading the OLD path ran first and stopped the run",
          rc != 0 and "FAILED EARLY" in out and "- test_d.py" in out, (rc, out))
    check("…before the gate that does not cover the change",
          not os.path.exists(os.path.join(marks, "c-ran")), os.listdir(marks))
    git(repo2, "mv", "lib/gamma.py", "lib/beta.py")
    with open(os.path.join(repo2, "lib/café.py"), "w", encoding="utf-8") as f:
        f.write("c = 1  # BROKEN\n")
    git(repo2, "commit", "-qam", "beta back; break café")
    rc, out, err = run(repo2, **pinned)
    check("the gate naming café.py ran first and stopped the run",
          rc != 0 and "FAILED EARLY" in out and "- test_e.py" in out, (rc, out))
    check("…before the gate that does not cover the change",
          not os.path.exists(os.path.join(marks, "c-ran")), os.listdir(marks))

    print("\n7. PRE_MERGE_AFFECTED_ONLY=1 (a pull request) runs only what covers the change")
    # On the first repo, from a clean state with no failure remembered: alpha
    # changes, A reads it, B and C do not.
    for f in os.listdir(results):
        os.remove(os.path.join(results, f))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    with open(os.path.join(repo, "lib/alpha.py"), "w") as f:
        f.write("x = 2\n")
    git(repo, "commit", "-qam", "change alpha, which A reads")
    rc, out, err = run(PRE_MERGE_AFFECTED_ONLY="1")
    check("passes, saying it ran the affected gates only",
          rc == 0 and "PRE-MERGE GATES PASSED — 1 gates" in out and "affected-only" in out, (rc, out))
    check("…the gate covering the change ran", os.path.exists(os.path.join(marks, "a-ran")),
          os.listdir(marks))
    check("…a gate that does not cover it never ran", not os.path.exists(os.path.join(marks, "c-ran")),
          os.listdir(marks))
    check("…and no pass is recorded (a .pass names a FULL run; smoke's skip relies on it)",
          not any(f.endswith(".pass") for f in os.listdir(results)), os.listdir(results))
    rc, out, err = run(PRE_MERGE_AFFECTED_ONLY="1", PRE_MERGE_FULL="1")
    check("PRE_MERGE_FULL=1 still runs everything", rc == 0 and os.path.exists(os.path.join(marks, "c-ran")),
          (rc, out))
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    rc, out, err = run(PRE_MERGE_AFFECTED_ONLY="1")
    check("with no origin/main to diff against, every gate runs",
          rc == 0 and os.path.exists(os.path.join(marks, "c-ran")) and "no origin/main" in out, (rc, out))

    print("\n8. tests/ runs BESIDE the gates, and an early stop stops it")
    # The lane writes t-start, sleeps LANE_SLEEP, writes t-end. Gate F passes only
    # if t-start appears while F is running, i.e. tests/ started before the gates
    # finished (it used to start after the last one).
    shutil.copy(os.path.join(APP_ROOT, "scripts", "pytest_supervisor.py"),
                os.path.join(repo, "scripts", "pytest_supervisor.py"))
    os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
    with open(os.path.join(repo, "tests", "test_lane.py"), "w") as f:
        f.write("import os, time\n"
                "def test_lane():\n"
                "    m = os.environ['MARKS']\n"
                "    open(os.path.join(m, 't-start'), 'w').close()\n"
                "    time.sleep(float(os.environ.get('LANE_SLEEP', '1')))\n"
                "    open(os.path.join(m, 't-end'), 'w').close()\n")
    with open(os.path.join(repo, "scripts", "test_f.py"), "w") as f:
        f.write("import os, sys, time\n"
                "m = os.environ['MARKS']\n"
                "for _ in range(300):\n"
                "    if os.path.exists(os.path.join(m, 't-start')):\n"
                "        sys.exit(0)\n"
                "    time.sleep(0.1)\n"
                "sys.exit(1)\n")
    with open(os.path.join(repo, "scripts", "smoke_test.sh"), "a") as f:
        f.write('python3 "$SMOKE_REPO/scripts/test_f.py"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a tests/ lane and gate F")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    rc, out, err = run(PRE_MERGE_FULL="1")
    check("the lane started while gate F was still running (F saw t-start) and both passed",
          rc == 0 and "OK    test_f.py" in out and "OK    tests/" in out, (rc, out[-1500:]))
    with open(os.path.join(repo, "lib/alpha.py"), "w") as f:
        f.write("x = 1  # BROKEN\n")
    git(repo, "commit", "-qam", "break alpha again")
    t = time.time()
    rc, out, err = run(LANE_SLEEP="12", A_WAITS_FOR_LANE="1")
    took = time.time() - t
    time.sleep(14)
    check("a covering gate failing stops the run early, without waiting for tests/",
          rc != 0 and "FAILED EARLY" in out and took < 12, (rc, round(took, 1)))
    check("…the lane had started", os.path.exists(os.path.join(marks, "t-start")), os.listdir(marks))
    check("…and was stopped with the run (it never wrote t-end)",
          not os.path.exists(os.path.join(marks, "t-end")), os.listdir(marks))

    print("\n9. a full pool WAITS for a gate; it does not spin")
    # bash keeps a reaped job's status, so `wait -n` on a list still holding a
    # finished gate returns at once and the pool loop spun on `jobs` until the
    # next gate ended. BASH_ENV counts every `wait` the script makes (wrapping
    # `jobs` instead would change it: `$(jobs)` reads the job table only as the
    # builtin). One slot, four 1 s gates: a waiting pool calls it a few times.
    for i in range(4):
        with open(os.path.join(repo, "scripts", f"test_s{i}.py"), "w") as f:
            f.write("import time\ntime.sleep(1)\n")
    with open(os.path.join(repo, "scripts", "smoke_test.sh"), "a") as f:
        f.write("".join(f'python3 "$SMOKE_REPO/scripts/test_s{i}.py"\n' for i in range(4)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "four sleeping gates")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    counter = os.path.join(root, "jobs-calls")
    bash_env = os.path.join(root, "count-jobs.sh")
    with open(bash_env, "w") as f:
        f.write(f'wait() {{ echo x >>"{counter}"; builtin wait "$@"; }}\n')
    rc, out, err = run(PRE_MERGE_FULL="1", PRE_MERGE_JOBS="1", LANE_SLEEP="0", BASH_ENV=bash_env)
    calls = sum(1 for _ in open(counter)) if os.path.exists(counter) else 0
    check("every sleeping gate ran", all(f"OK    test_s{i}.py" in out for i in range(4)), out[-1500:])
    check("the pool called `wait` a few times per gate, not in a spin", 0 < calls < 60, calls)

print()
if FAILS:
    print(f"FAIL — {len(FAILS)} failure(s)")
    sys.exit(1)
print("PASS — 0 failure(s)")
