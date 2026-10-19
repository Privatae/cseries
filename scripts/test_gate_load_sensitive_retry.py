#!/usr/bin/env python3
"""Gate: a load-sensitive gate that fails inside the parallel pool runs once more
alone, and the run says so; nothing else is ever retried.

Measured: test_harness_requested_step_window.py and
test_harness_grok_command_matrix.py each failed one full pre-merge run on a tree
that did not touch them, and passed alone on the same tree. Both decide by the
wall-clock or CPU timing of real processes, which the full pool can starve. This
drives the real scripts/pre_merge_gates.sh on a fake repo of tiny gates:
  1. a listed gate that fails in the pool and passes alone: the run passes, the
     retry starts after every pool gate has finished, and both the gate's line
     and the PASSED banner say it needed the retry
  2. a listed gate that fails alone too: the run fails and names it
  3. a gate that is NOT listed and fails once: never retried, the run fails
  4. the timing report and history read the retry's second row: the report names
     it among what ran one at a time, labelled, and the history keeps the pool
     time the next run's order is for
  5. a listed gate in the FIRST stage (it failed here last time) gets the same
     retry before the early stop decides, so the run goes on to the rest
  6. the Telegram and Slack connector example gates are on the list:
     each, failing once in the pool beside an
     ordinary gate, gets its retry alone, and the run passes and names both
  7. a failed gate whose check is followed by more output than the report's
     tail still shows that check, and its error line (in one CI run
     the report of test_project_list_speed_pg.py was psql notices only)
  8. the sidecar workspace-routes gate is on the list: failing once in the
     pool, it gets its retry alone,
     and the run passes and names it
  9. the book of pages' browser gate is on the list: failing once in the
     pool, it gets its retry alone,
     and the run passes and names it
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PMG = os.path.join(ROOT, "scripts", "pre_merge_gates.sh")
FAILS = []
LISTED = "test_harness_requested_step_window.py"
LISTED_2 = "test_harness_grok_command_matrix.py"


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=g@example.invalid", "-c", "user.name=g", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


# mode: ok | always | once (fails on its first run, passes after; a marker file
# beside the log remembers the first run)
GATE = '''import os, sys, time
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write("{name}\\n")
with open(log + ".events", "a") as f:
    f.write("start:{name}\\n")
time.sleep({sleep})
with open(log + ".events", "a") as f:
    f.write("end:{name}\\n")
mode = "{mode}"
if mode == "always":
    print("FAIL  pretend timing row")
    sys.exit(1)
if mode == "once":
    marker = log + ".{name}.ran"
    if not os.path.exists(marker):
        open(marker, "w").close()
        print("FAIL  pretend timing row")
        sys.exit(1)
if mode == "noisy":
    print("  FAIL  the check that broke  [its detail]")
    print("kernel_schema.SchemaError: the error that broke it")
    for i in range(40):
        print("NOTICE:  relation r%d already exists, skipping" % i)
    sys.exit(1)
sys.exit(0)
'''


def make_repo(td, gates):
    """gates: (name, seconds, mode). Returns the repo path."""
    repo = os.path.join(td, "repo")
    os.makedirs(os.path.join(repo, "scripts", "gates-container-free.d"))
    shutil.copy(os.path.join(ROOT, "scripts", "hang_watch.py"), os.path.join(repo, "scripts"))
    with open(os.path.join(repo, "scripts", "smoke_test.sh"), "w") as smoke:
        for name, sleep, mode in gates:
            with open(os.path.join(repo, "scripts", name), "w") as f:
                f.write(GATE.format(name=name, sleep=sleep, mode=mode))
            smoke.write(f'python3 "$SMOKE_REPO/scripts/{name}"\n')
    open(os.path.join(repo, "scripts", "gates-container-free.d", ".keep"), "w").close()
    subprocess.run(["git", "init", "-q", repo], check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fake gates")
    return repo


def run(repo, td, jobs=2):
    log, results = os.path.join(td, "log"), os.path.join(td, "results")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRE_MERGE_", "GATE_RESULTS"))}
    env.update(PRE_MERGE_JOBS=str(jobs), PRE_MERGE_PYFLAKES="true", GATE_RESULTS_DIR=results, FAKE_LOG=log)
    r = subprocess.run(["bash", PMG, repo], capture_output=True, text=True, env=env, timeout=300)
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
    started = open(log).read().split() if os.path.exists(log) else []
    ev = open(log + ".events").read().split() if os.path.exists(log + ".events") else []
    return r.returncode, out, started, ev


# ── 1: listed, fails in the pool, passes alone ──────────────────────────────
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [(LISTED, 0.1, "once"), ("test_fake_a.py", 0.6, "ok"), ("test_fake_b.py", 0.6, "ok")])
    rc, out, started, ev = run(repo, td)
    check("1: the run passes", rc == 0, out[-900:])
    check("1: the listed gate ran twice", started.count(LISTED) == 2, f"{started}")
    second = [i for i, e in enumerate(ev) if e == f"start:{LISTED}"][1:2]
    ends = [i for i, e in enumerate(ev) if e.startswith("end:") and e != f"end:{LISTED}"]
    check("1: its retry started only after every other gate had finished (alone, after the pool)",
          bool(second) and ends and second[0] > max(ends), f"{ev}")
    check("1: its line says it failed in the pool and passed alone",
          re.search(rf"OK\s+{re.escape(LISTED)}.*\n\s+failed in the parallel pool \(exit 1\), passed alone", out)
          is not None, out[-900:])
    check("1: the PASSED banner names it",
          re.search(rf"passed alone after failing in the pool: {re.escape(LISTED)}", out) is not None, out[-600:])

# ── 2: listed, fails alone too ───────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [(LISTED_2, 0.1, "always"), ("test_fake_a.py", 0.1, "ok")])
    rc, out, started, ev = run(repo, td)
    check("2: a listed gate that fails alone too fails the run", rc != 0, out[-600:])
    check("2: ...it was retried exactly once", started.count(LISTED_2) == 2, f"{started}")
    check("2: ...and is named in the failures", re.search(rf"-\s+{re.escape(LISTED_2)}", out) is not None,
          out[-600:])

# ── 3: not listed, fails once: never retried ─────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [("test_fake_flaky.py", 0.1, "once"), ("test_fake_a.py", 0.1, "ok")])
    rc, out, started, ev = run(repo, td)
    check("3: a gate that is not listed is not retried", started.count("test_fake_flaky.py") == 1, f"{started}")
    check("3: ...and its failure fails the run", rc != 0, out[-600:])

# ── 4: the timing report and history read the retry's row ───────────────────
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gate_timing as gt  # noqa: E402

T = 1000.0
rows = [
    {"name": "a.py", "start": T, "end": T + 10, "dur": 10.0, "rc": "0", "kind": "gate", "cpu": 7.0},
    {"name": LISTED, "start": T, "end": T + 9, "dur": 9.0, "rc": "1", "kind": "gate", "cpu": 5.0},
    {"name": LISTED, "start": T + 10, "end": T + 16, "dur": 6.0, "rc": "0", "kind": "alone-retry", "cpu": 4.0},
    {"name": "tests/", "start": T + 16, "end": T + 19, "dur": 3.0, "rc": "0", "kind": "lane", "cpu": None},
]
text = "\n".join(gt.report(rows, {LISTED: (8.0, None, "")}, 2, T, T + 20, "/r"))
check("4: the report names the retry among what ran one at a time, labelled",
      f"then one at a time: {LISTED} (retried alone) 6.0s, tests/ 3.0s." in text, text)
check("4: ...and never advises running the retry beside the gates",
      "Run beside the gates instead of after them, tests/ could" in text, text)
check("4: its two rows are told apart in the slowest list",
      re.search(rf"6\.0s\s+\S+%\s+{re.escape(LISTED)} \(retried alone\)", text) is not None, text)
with tempfile.TemporaryDirectory() as td:
    gt.record(rows, td, "")
    hist = gt.read_history(os.path.join(td, "timings-latest.tsv"))
    check("4: the history keeps the pool time (9 s), not the retry's (6 s)",
          hist.get(LISTED, (None,))[0] == 9.0, f"{hist}")

# ── 5: a listed gate in the first stage gets the retry before the early stop ─
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [(LISTED, 0.1, "once"), ("test_fake_a.py", 0.1, "ok")])
    results = os.path.join(td, "results")
    os.makedirs(results)
    key = hashlib.sha1((os.path.realpath(repo) + "\n").encode()).hexdigest()[:16]
    with open(os.path.join(results, f"last-failed-{key}"), "w") as f:
        f.write(LISTED + "\n")
    rc, out, started, ev = run(repo, td)
    check("5: it ran in the first stage", "first: 1 gates" in out, out[:600])
    check("5: the run passes, so the early stop did not fire on the pool failure", rc == 0, out[-900:])
    check("5: ...the listed gate ran twice, and the rest ran", started.count(LISTED) == 2
          and "test_fake_a.py" in started, f"{started}")

# ── 6: the connector example gates are listed ────────────────────────────────
CONNECTOR_EXAMPLES = ("test_connector_telegram_example.py", "test_connector_slack_example.py")
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [(g, 0.1, "once") for g in CONNECTOR_EXAMPLES] + [("test_fake_a.py", 0.1, "ok")])
    rc, out, started, ev = run(repo, td)
    check("6: the connector example gates failing once in the pool: the run passes", rc == 0, out[-900:])
    for g in CONNECTOR_EXAMPLES:
        check(f"6: ...{g} ran twice, and the PASSED banner names it",
              started.count(g) == 2
              and re.search(rf"passed alone after failing in the pool: [^\n]*{re.escape(g)}", out) is not None,
              f"{started} {out[-600:]}")

# ── 7: a failed check stays readable behind later output ────────────────────
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [("test_fake_noisy.py", 0.1, "noisy"), ("test_fake_a.py", 0.1, "ok")])
    rc, out, started, ev = run(repo, td)
    report = out[out.find("FAIL  test_fake_noisy.py"):] if "FAIL  test_fake_noisy.py" in out else ""
    check("7: the noisy gate fails the run", rc != 0, out[-600:])
    check("7: ...its report shows the failed check, though 40 lines followed it",
          "FAIL  the check that broke  [its detail]" in report, report[:900])
    check("7: ...and the error line", "kernel_schema.SchemaError: the error that broke it" in report, report[:900])
    check("7: ...and still its last lines", "relation r39 already exists" in report, report[-600:])
    check("7: ...each shown once", report.count("the check that broke") == 1, report[:900])

# ── 8: the sidecar workspace-routes gate is listed ───────────────────────────
SIDECAR_ROUTES = "test_sidecar_workspace_routes_harness_only.py"
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [(SIDECAR_ROUTES, 0.1, "once"), ("test_fake_a.py", 0.1, "ok")])
    rc, out, started, ev = run(repo, td)
    check("8: the sidecar routes gate failing once in the pool: the run passes", rc == 0, out[-900:])
    check(f"8: ...{SIDECAR_ROUTES} ran twice, and the PASSED banner names it",
          started.count(SIDECAR_ROUTES) == 2
          and re.search(rf"passed alone after failing in the pool: [^\n]*{re.escape(SIDECAR_ROUTES)}", out) is not None,
          f"{started} {out[-600:]}")

# ── 9: the book of pages' browser gates are listed ──────────────────────────
# Three gates: the book's scenarios, its Index, its colours and
# Open (one process spent over the 180 s CPU budget).
BOOK_GATES = ("test_workbench_book.py", "test_workbench_book_index.py", "test_workbench_book_colours.py")
with tempfile.TemporaryDirectory() as td:
    repo = make_repo(td, [(g, 0.1, "once") for g in BOOK_GATES] + [("test_fake_a.py", 0.1, "ok")])
    rc, out, started, ev = run(repo, td)
    check("9: the book gates failing once in the pool: the run passes", rc == 0, out[-900:])
    for g in BOOK_GATES:
        check(f"9: ...{g} ran twice, and the PASSED banner names it",
              started.count(g) == 2
              and re.search(rf"passed alone after failing in the pool: [^\n]*{re.escape(g)}", out) is not None,
              f"{started} {out[-600:]}")

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS: a load-sensitive gate gets one retry alone, said so; nothing else is retried")
