#!/usr/bin/env python3
"""Gate: smoke does not re-run the container-free gates on code that already
passed them, and never skips them on code that did not.

Measured: smoke's serial pass over gates-container-free.d was about
37 of a 43-minute smoke, every gate a repeat of
the pre_merge_gates.sh run on the same tree minutes before, and a red gates job
still started a full smoke. This drives the real scripts/gate_pass_recorded.sh
on throwaway git repos and pins:
  1. no record -> not passed (smoke runs the gates)
  2. pre_merge_gates.sh's <tree>.pass for the HEAD tree -> passed, says so
  3. that same record with a modified OR an untracked file -> not passed: the
     tree hash no longer names the code on disk
  4. a record for another tree -> not passed
  5. CI's SMOKE_SHARED_GATES_PASSED_TREE for the HEAD tree -> passed; for
     another tree -> not passed
  6. wiring: smoke asks gate_pass_recorded.sh before its serial pass and keeps
     the pass for every other case; pre_merge_gates.sh writes the record where
     the reader looks. (The repo has no CI workflow yet; when one is added, it
     must run the gates whenever it runs smoke before it hands smoke the tree.)
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "gate_pass_recorded.sh")
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


def ask(repo, results, **env):
    e = {k: v for k, v in os.environ.items() if k not in ("SMOKE_SHARED_GATES_PASSED_TREE", "GATE_RESULTS_DIR")}
    e["GATE_RESULTS_DIR"] = results
    e.update(env)
    r = subprocess.run(["bash", SCRIPT, repo], capture_output=True, text=True, env=e)
    return r.returncode, r.stdout.strip()


with tempfile.TemporaryDirectory() as td:
    repo, results = os.path.join(td, "repo"), os.path.join(td, "results")
    os.makedirs(results)
    subprocess.run(["git", "init", "-q", repo], check=True)
    git(repo, "config", "user.email", "gate@example.invalid")
    git(repo, "config", "user.name", "gate")
    with open(os.path.join(repo, "a.py"), "w") as f:
        f.write("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "one")
    tree = git(repo, "rev-parse", "HEAD^{tree}")

    rc, out = ask(repo, results)
    check("1: no record -> the gates run", rc == 1, f"rc={rc} {out}")

    with open(os.path.join(results, tree + ".pass"), "w") as f:
        f.write("2026-10-06T12:00:00Z\n")
    rc, out = ask(repo, results)
    check("2: pre_merge_gates.sh's record for this tree -> not re-run, and says which",
          rc == 0 and "pre_merge_gates.sh passed tree" in out and tree[:12] in out, f"rc={rc} {out}")

    with open(os.path.join(repo, "a.py"), "a") as f:
        f.write("y = 2\n")
    rc, out = ask(repo, results)
    check("3: a modified file -> the gates run (the hash no longer names the code)", rc == 1, f"rc={rc} {out}")
    git(repo, "checkout", "-q", "--", "a.py")
    with open(os.path.join(repo, "new.py"), "w") as f:
        f.write("z = 3\n")
    rc, out = ask(repo, results)
    check("3: an untracked file -> the gates run", rc == 1, f"rc={rc} {out}")
    os.remove(os.path.join(repo, "new.py"))
    rc, out = ask(repo, results)
    check("3: clean again -> the record counts again", rc == 0, f"rc={rc} {out}")

    os.remove(os.path.join(results, tree + ".pass"))
    with open(os.path.join(results, "0" * 40 + ".pass"), "w") as f:
        f.write("2026-10-06T12:00:00Z\n")
    rc, out = ask(repo, results)
    check("4: a record for another tree -> the gates run", rc == 1, f"rc={rc} {out}")

    rc, out = ask(repo, results, SMOKE_SHARED_GATES_PASSED_TREE=tree)
    check("5: CI's gates job passed this tree -> not re-run, and says so",
          rc == 0 and "gates job passed tree" in out, f"rc={rc} {out}")
    # That job runs only the gates covering the change (PRE_MERGE_AFFECTED_ONLY),
    # so the line smoke prints may not read as the whole list having passed.
    check("5: …and says the job ran the gates covering the change, the whole list at ship",
          "gates covering the change" in out and "whole list runs at ship" in out, out)
    rc, out = ask(repo, results, SMOKE_SHARED_GATES_PASSED_TREE="f" * 40)
    check("5: CI names another tree -> the gates run", rc == 1, f"rc={rc} {out}")
    rc, out = ask(os.path.join(td, "not-a-repo"), results, SMOKE_SHARED_GATES_PASSED_TREE=tree)
    check("5: not a git checkout -> the gates run", rc == 1, f"rc={rc} {out}")

smoke = open(os.path.join(ROOT, "scripts", "smoke_test.sh"), encoding="utf-8").read()
ask_at = smoke.find('scripts/gate_pass_recorded.sh" "$SMOKE_REPO"')
loop_at = smoke.find('yellow "Running container-free gates from gates-container-free.d…"')
check("6: smoke asks before its serial pass, and keeps the pass for every other case",
      0 < ask_at < loop_at and "elif [[ -d \"$GATE_DIR\" ]]; then" in smoke[ask_at:loop_at], f"ask@{ask_at} loop@{loop_at}")

pmg = open(os.path.join(ROOT, "scripts", "pre_merge_gates.sh"), encoding="utf-8").read()
reader = open(SCRIPT, encoding="utf-8").read()
# The default nests ${...} expansions, so take everything up to the closing }".
_default = re.search(r'^RESULTS_DIR="\$\{GATE_RESULTS_DIR:-(.+)\}"$', pmg, re.M)
check("6: pre_merge_gates.sh writes the record the reader reads (same default dir, <tree>.pass)",
      _default is not None
      and '"$RESULTS_DIR/$START_TREE.pass"' in pmg
      and '"${GATE_RESULTS_DIR:-%s}/$tree.pass"' % _default.group(1) in reader,
      _default.group(1) if _default else "no RESULTS_DIR default in pre_merge_gates.sh")

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS: smoke re-runs no gate that already passed on this code")
