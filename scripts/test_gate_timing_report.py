#!/usr/bin/env python3
"""Gate: every pre-merge run records what each gate cost, starts the gates
that took longest last time first, and ends with how it could have been
faster, on a pass and on a fail.

Measured on main (447 gates, 8 slots): 825 s wall, of which one
gate took 548 s while all gate time over 8 slots was 346 s, and tests/ ran
222 s on its own afterwards. Nothing recorded a gate's time, so nothing could
say so. This drives the real scripts/pre_merge_gates.sh and
scripts/gate_timing.py on a fake repo of tiny gates (each logs when it
starts), and the report on known durations:
  1. with a recorded history, a gate with no recorded time starts first, then
     the rest longest first, whatever the list order
  2. the run records every gate's time for the next run, keeps the time of a
     gate that did not run, and writes <tree>.timings.tsv for a clean tree
  3. with no history the list order stands and the report says there is
     nothing to compare against
  4. a failing run still prints the report, before its FAILED banner; so does
     a run that stops early on the gates covering the change
  5. the report's numbers on known durations: busy share, the floor and which
     bound sets it, the gate the run waited on alone, the serial lanes, the
     slowest list, and a gate markedly slower than last time
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PMG = os.path.join(ROOT, "scripts", "pre_merge_gates.sh")
FAILS = []
HEAD = "How this run could have been faster"


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=g@example.invalid", "-c", "user.name=g", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


GATE = '''import os, sys, time
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("{name}\\n")
time.sleep({sleep})
sys.exit({rc})
'''


def make_repo(td, gates, fail=()):
    """gates: (name, seconds) in LIST order. Returns the repo path."""
    repo = os.path.join(td, "repo")
    os.makedirs(os.path.join(repo, "scripts", "gates-container-free.d"))
    shutil.copy(os.path.join(ROOT, "scripts", "hang_watch.py"), os.path.join(repo, "scripts"))
    with open(os.path.join(repo, "scripts", "smoke_test.sh"), "w") as smoke:
        for name, sleep in gates:
            with open(os.path.join(repo, "scripts", name), "w") as f:
                f.write(GATE.format(name=name, sleep=sleep, rc=1 if name in fail else 0))
            smoke.write(f'python3 "$SMOKE_REPO/scripts/{name}"\n')
    open(os.path.join(repo, "scripts", "gates-container-free.d", ".keep"), "w").close()
    subprocess.run(["git", "init", "-q", repo], check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fake gates")
    return repo


def run(repo, results, log, jobs=1):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRE_MERGE_", "GATE_RESULTS"))}
    env.update(PRE_MERGE_JOBS=str(jobs), PRE_MERGE_PYFLAKES="true", GATE_RESULTS_DIR=results, FAKE_LOG=log)
    r = subprocess.run(["bash", PMG, repo], capture_output=True, text=True, env=env, timeout=300)
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
    started = open(log).read().split() if os.path.exists(log) else []
    return r.returncode, out, started


def history(path):
    rows = {}
    if os.path.exists(path):
        for line in open(path).read().splitlines():
            if not line.startswith("#"):
                f = line.split("\t")
                rows[f[0]] = float(f[1])
    return rows


# pre_merge_gates.sh builds its list in name order, so the names put the list in
# exactly the reverse of the recorded durations.
LIST = [("test_fake_a_short.py", 0.1), ("test_fake_b_mid.py", 0.3), ("test_fake_c_long.py", 0.6),
        ("test_fake_d_new.py", 0.2)]

# ── 1-2: a recorded history decides the order; the run records its own ───────
with tempfile.TemporaryDirectory() as td:
    repo, results, log = make_repo(td, LIST), os.path.join(td, "results"), os.path.join(td, "log")
    os.makedirs(results)
    with open(os.path.join(results, "timings-latest.tsv"), "w") as f:
        f.write("# seeded\ntest_fake_a_short.py\t1.0\t\t-\ntest_fake_b_mid.py\t5.0\t\t-\n"
                "test_fake_c_long.py\t9.0\t\t-\ntest_gone.py\t42.0\t\t-\n")
    rc, out, started = run(repo, results, log)
    check("the run passes", rc == 0, out[-600:])
    check("1: no recorded time first, then longest first (the list order was the reverse)",
          started == ["test_fake_d_new.py", "test_fake_c_long.py", "test_fake_b_mid.py", "test_fake_a_short.py"],
          f"{started}")
    hist = history(os.path.join(results, "timings-latest.tsv"))
    check("2: every gate's time is recorded for the next run",
          set(n for n, _ in LIST) <= set(hist) and 0.5 < hist["test_fake_c_long.py"] < 5, f"{hist}")
    check("2: a gate that did not run keeps its last time", hist.get("test_gone.py") == 42.0, f"{hist}")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    check("2: a clean tree gets <tree>.timings.tsv", os.path.exists(os.path.join(results, f"{tree}.timings.tsv")))
    check("2: the report runs and compares with the last run",
          HEAD in out and "slower than the last recorded run" in out.lower(), out[-800:])
    check("2: ...and names the gate that finished last (on one slot, the one started last)",
          re.search(r"Finished last: test_fake_a_short\.py", out) is not None, out[-800:])

# ── 3: no history: list order, and the report says so ───────────────────────
with tempfile.TemporaryDirectory() as td:
    repo, results, log = make_repo(td, LIST), os.path.join(td, "results"), os.path.join(td, "log")
    rc, out, started = run(repo, results, log)
    check("3: with no history the list order stands", started == [n for n, _ in LIST], f"{started}")
    check("3: the report says there is nothing to compare against", "No previous run recorded" in out, out[-600:])

# ── 4: a failing run, and one that stops early, still report ─────────────────
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, LIST, fail=("test_fake_b_mid.py",))
    results, log = os.path.join(td, "results"), os.path.join(td, "log")
    rc, out, _ = run(repo, results, log, jobs=2)
    at, banner = out.find(HEAD), out.find("PRE-MERGE GATES FAILED")
    check("4: a failing run exits non-zero", rc != 0)
    check("4: ...and prints the report before its FAILED banner", 0 <= at < banner, f"report@{at} banner@{banner}")

with tempfile.TemporaryDirectory() as td:
    repo, results, log = make_repo(td, LIST), os.path.join(td, "results"), os.path.join(td, "log")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    with open(os.path.join(repo, "scripts", "test_fake_broken.py"), "w") as f:   # untracked: the change
        f.write(GATE.format(name="test_fake_broken.py", sleep=0, rc=1))
    with open(os.path.join(repo, "scripts", "gates-container-free.d", "broken.txt"), "w") as f:
        f.write("test_fake_broken.py\n")
    rc, out, _ = run(repo, results, log, jobs=2)
    at, banner = out.find(HEAD), out.find("PRE-MERGE GATES FAILED EARLY")
    check("4: a run that stops early on the changed gate still prints the report first",
          rc != 0 and 0 <= at < banner, f"rc={rc} report@{at} banner@{banner}")

# ── 5: the report's numbers on known durations ───────────────────────────────
sys.path.insert(0, os.path.join(ROOT, "scripts"))
try:
    import gate_timing as gt
except ImportError as e:
    gt = None
    check("5: scripts/gate_timing.py imports", False, str(e))
if gt:
    T = 1000.0
    rows = [  # two slots: a fills one for 10 s; b then c fill the other for 8 s
        {"name": "a.py", "start": T, "end": T + 10, "dur": 10.0, "rc": "0", "kind": "gate", "cpu": 7.0},
        {"name": "b.py", "start": T, "end": T + 4, "dur": 4.0, "rc": "0", "kind": "gate", "cpu": 1.0},
        {"name": "c.py", "start": T + 4, "end": T + 8, "dur": 4.0, "rc": "0", "kind": "gate", "cpu": 1.0},
        {"name": "tests/", "start": T + 10, "end": T + 13, "dur": 3.0, "rc": "0", "kind": "lane", "cpu": None},
    ]
    text = "\n".join(gt.report(rows, {"a.py": (4.0, None, ""), "b.py": (3.9, None, "")}, 2, T, T + 15, "/r"))
    check("5: wall, gate phase and the serial lane",
          "Wall 15s. Gates 10s (3 gates, 2 at a time), then one at a time: tests/ 3.0s." in text, text)
    check("5: gate time, CPU, and how busy the slots were (18 of 20 slot-seconds)",
          "Gate time 18s (CPU 9.0s). The 2 slots were busy 90% of the gate phase." in text, text)
    check("5: the floor is the longest gate, named, against all gate time over the slots",
          "Floor for the gate phase: 10s (the longest gate, a.py; all gate time ÷ 2 is 9.0s). It took 10s, 0.0s over."
          in text, text)
    check("5: the gate the run waited on alone (2 s after c ended)",
          "Finished last: a.py (10s), started at +0.0s, ran 2.0s with no other gate running." in text, text)
    check("5: the lane that ran after the gates, and what running it beside them could save",
          "tests/ could end the run up to 3.0s sooner (the gate phase left 2.0s of slot time idle)." in text, text)
    check("5: slowest first, with its share of all time (10 of 21 s)",
          re.search(r"Slowest 10.*\n\s+10\.0s\s+47\.6%\s+a\.py\n", text) is not None, text)
    check("5: a gate over 50% and 5 s slower is named; one only 0.1 s slower is not",
          "a.py 4.0s → 10s" in text and "b.py" not in text.split("Slower than")[-1], text)
    one = "\n".join(gt.report(rows[:3], {}, 1, T, T + 15, ""))
    check("5: on one slot the floor is all gate time, said so",
          "Floor for the gate phase: 18s (all gate time ÷ 1; the longest gate is 10s)." in one, one)
    check("5: order: unknown first in the order given, then longest first",
          gt.order({"x": (1.0, None, ""), "y": (9.0, None, "")}, ["x", "u1", "y", "u2"]) == ["u1", "u2", "y", "x"])

    beside = rows[:3] + [dict(rows[3], start=T, end=T + 12, dur=12.0, kind="beside")]
    text = "\n".join(gt.report(beside, {}, 2, T, T + 13, ""))
    check("6: a lane run beside the gates is said beside them, with its own time",
          "Gates 10s (3 gates, 2 at a time), beside them: tests/ 12s." in text, text)
    check("6: ...never as one at a time, nor as advice to run it beside them",
          "one at a time" not in text and "Run beside the gates instead" not in text, text)

# ── 6: tests/ runs beside the gates; its row ends when ITS pytest ended ───────
# In one CI run pytest took 311 s, but the row ended when the lane was
# collected after the last gate, so the report read "then one at a time:
# tests/ 1102s". Here the lane takes ~0.5 s and a gate holds the run 4 s.
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [("test_fake_slow.py", 4.0)])
    shutil.copy(os.path.join(ROOT, "scripts", "pytest_supervisor.py"), os.path.join(repo, "scripts"))
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "test_quick.py"), "w") as f:
        f.write("def test_quick():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a tests/ lane")
    results, log = os.path.join(td, "results"), os.path.join(td, "log")
    rc, out, _ = run(repo, results, log, jobs=2)
    hist = history(os.path.join(results, "timings-latest.tsv"))
    check("6: the run passes", rc == 0 and "OK    tests/" in out, out[-800:])
    check("6: tests/ is recorded at its own duration, not the gate it waited on (4 s)",
          0 < hist.get("tests/", 99) < 3, f"{hist}")
    check("6: the report says tests/ ran beside the gates, not after them",
          "beside them: tests/" in out and "one at a time: tests/" not in out, out[-900:])

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS: every run records its gates' times, starts the longest first, and says what held it up")
