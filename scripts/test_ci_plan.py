#!/usr/bin/env python3
# gate-scope: whole-tree — what each CI job reads: Dockerfile.linux, smoke_test.sh, tests/, packages/cseries, check.yml
"""Gate: a pull request runs the CI jobs its change can affect, and no others.

Measured on the forge (2026-10-28, the last 15 cseries PR runs): every PR ran
every job whatever it changed; two docs/wiki files took the same image build,
CVE scan, 5-minute smoke and 6-minute tests/ lane as a kernel change, and with
one two-slot runner four PRs pushed together took about 35 minutes each.
scripts/ci_plan.py now says which of build, scan, smoke, installer and the
tests/ lane a change needs, from what each one reads; check.yml and
pre_merge_gates.sh act on it.

Pins:
  1. on this tree, invented changes select exactly these jobs: a docs-only
     change none of them reads (none), a wiki page the image bakes (the image
     jobs and tests/), one Python module (the image jobs and tests/), the
     Dockerfile, a store app, the installer, vm/, a gate script, the workflow
     file (every job), a forced run, and no merge base (every job);
  2. on a stub tree, the derivation itself: COPY sources, a smoke-only path, a
     tree gate smoke runs with only APP_ROOT (the gates job's, not smoke's)
     and one smoke runs with its own setup (smoke's), a word list, a sys.path
     line and a pytest scratch path are not reads, a pathlib chain and a join
     are, an installer test reaching outside its package;
  3. the wiring: check.yml's current job makes the plan and every gated step
     runs unless the plan said "false" (fails open), each job not needed says
     why, a full run reaches the gates job, and pre_merge_gates.sh skips
     tests/ only on an affected-only run whose plan said it is not needed.

Container-free: APP_ROOT=<repo> python3 scripts/test_ci_plan.py
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

APP_ROOT = os.environ.get("APP_ROOT", "/app")
SCRIPTS = os.path.join(APP_ROOT, "scripts")
PLAN = os.path.join(SCRIPTS, "ci_plan.py")
WORKFLOW = os.path.join(APP_ROOT, ".gitea", "workflows", "check.yml")
FAILS = []
JOBS = ("build", "scan", "smoke", "installer", "tests")


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f"\n         {detail}"))
    if not ok:
        FAILS.append(name)


if not os.path.isfile(PLAN):
    check("scripts/ci_plan.py exists (it says which CI jobs a change needs)", False, f"no {PLAN}")
    print(f"\nFAILED: {len(FAILS)}")
    sys.exit(1)
spec = importlib.util.spec_from_file_location("ci_plan", PLAN)
ci_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_plan)


def runs(repo, changed, full=""):
    return {j for j, (r, _) in ci_plan.plan(repo, changed, full).items() if r}


def expect(repo, name, changed, want, full=""):
    got = runs(repo, changed, full)
    why = {j: w for j, (_, w) in ci_plan.plan(repo, changed, full).items()}
    check(f"{name}: runs {', '.join(sorted(want)) or 'none of them'}", got == set(want),
          f"ran {sorted(got)}, wanted {sorted(want)}; " + "; ".join(f"{j}: {why[j]}" for j in sorted(got ^ set(want))))


print("1. on this tree, a change selects the jobs that read it")
ALL = set(JOBS)
IMAGE = {"build", "scan", "smoke", "tests"}
expect(APP_ROOT, "docs-only (docs/wiki/sources.yaml, which no image or job reads)",
       ["docs/wiki/sources.yaml"], set())
expect(APP_ROOT, "docs-only (a docs/ page outside the wiki)", ["docs/run-only-what-changed-probe.md"], set())
expect(APP_ROOT, "a wiki page (Dockerfile.linux COPYs docs/wiki/pages/ and bakes it)",
       ["docs/wiki/pages/browser-app.md"], IMAGE)
expect(APP_ROOT, "one Python module in the image (server/routes/agents.py)", ["server/routes/agents.py"], IMAGE)
expect(APP_ROOT, "the Dockerfile", ["Dockerfile.linux"], IMAGE)
# browser is a store app (scripts/gates-store-apps.txt); smoke reads system-apps/
# from the tree, so it runs; the image does not COPY it, so the scan does not.
expect(APP_ROOT, "a store app (system-apps/browser/app.yaml)", ["system-apps/browser/app.yaml"],
       {"build", "smoke", "tests"})
expect(APP_ROOT, "the installer (packages/cseries/bin/cseries.js)", ["packages/cseries/bin/cseries.js"],
       {"installer"})
expect(APP_ROOT, "vm/ (the installer's tests read vm/wsl/rootfs)", ["vm/wsl/rootfs/etc/hostname"], {"installer"})
# tests/ names scripts/ as a directory (tests/test_gate_list_readers_agree.py), so
# a gate change runs the lane; nothing else reads a gate file.
expect(APP_ROOT, "a gate script (scripts/test_workbench_book.py)", ["scripts/test_workbench_book.py"], {"tests"})
expect(APP_ROOT, "the workflow file", [".gitea/workflows/check.yml"], ALL)
expect(APP_ROOT, "the plan itself", ["scripts/ci_plan.py"], ALL)
expect(APP_ROOT, "a docs-only change on a forced full run", ["docs/wiki/sources.yaml"], ALL, full="ci-full label")
expect(APP_ROOT, "no merge base", None, ALL)
expect(APP_ROOT, "no changed file", [], ALL)
w = ci_plan.plan(APP_ROOT, ["docs/wiki/sources.yaml"])
check("a job not needed says why", all(why for _, why in w.values()), str(w))


print("\n2. on a stub tree, what counts as a read")


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *args], check=True, capture_output=True)


STUB = {
    "Dockerfile.linux": "FROM python:3 AS b\nCOPY --from=b /x /x\nCOPY app/ \\\n    /app/app/\n"
                        "COPY conf.txt /app/conf.txt\nADD https://example.invalid/f /f\n",
    "app/main.py": "x = 1\n",
    "conf.txt": "a\n",
    "scripts/smoke_test.sh": 'SMOKE_REPO=x\nAPP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_tree.py"\n'
                             'DB_LANE=1 python3 "$SMOKE_REPO/scripts/test_live.py"\n'
                             'cp "$SMOKE_REPO/smokedata/seed.sql" /tmp/x\n'
                             'for g in a; do python3 "$SMOKE_REPO/scripts/$g"; done\n',
    "scripts/test_tree.py": 'open("treeonly/x.txt")\n',
    "scripts/test_live.py": "import live_helper\n",
    "scripts/live_helper.py": 'P = "livedata/rows.csv"\n',
    "tests/test_a.py": 'from pathlib import Path\nimport sys\nROOT = Path(__file__).parents[1]\n'
                       'sys.path.insert(0, str(ROOT / "sysdir"))\nW = ["find", "words", "docs"]\n'
                       'F = ROOT / "fixtures" / "one.json"\nJ = __import__("os").path.join(ROOT, "joined", "two")\n'
                       'def test_x(tmp_path):\n    r = tmp_path / "repo"\n    (r / "scratchdir").mkdir()\n',
    "fixtures/one.json": "{}\n", "fixtures/other.json": "{}\n", "joined/two/x": "", "sysdir/m.py": "",
    "scratchdir/x": "", "docs/x.md": "", "treeonly/x.txt": "", "livedata/rows.csv": "",
    "smokedata/seed.sql": "", "vmx/guest.conf": "",
    # A module imported through a sys.path entry, and a scripts/ helper whose
    # import a change deletes (the stub's tree no longer has scripts/gone.py).
    "tests/test_b.py": 'import sys\nfrom pathlib import Path\nR = Path(__file__).parents[1]\n'
                       'sys.path.insert(0, str(R / "vmy/lib"))\nimport guestmod\nimport gone\nimport emptymod\n',
    "vmy/lib/guestmod.py": "x = 1\n", "vmy/lib/unrelated.py": "", "scripts/emptymod.py": "",
    "packages/cseries/test/t.test.js": "const p = require('path');\n"
                                       "const G = p.join(__dirname, '..', '..', '..', 'vmx', 'guest.conf');\n",
}
with tempfile.TemporaryDirectory() as root:
    for rel, text in STUB.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "stub")
    expect(root, "a COPY source dir (app/, a continued line)", ["app/main.py"], IMAGE)
    expect(root, "a COPY source file", ["conf.txt"], IMAGE)
    expect(root, "a path only smoke names", ["smokedata/seed.sql"], {"build", "smoke"})
    expect(root, "what a tree gate smoke runs with only APP_ROOT reads: the gates job's", ["treeonly/x.txt"], set())
    expect(root, "that tree gate itself: the gates job's", ["scripts/test_tree.py"], set())
    expect(root, "what a script smoke runs with its own setup reads, through an import",
           ["livedata/rows.csv"], {"build", "smoke"})
    expect(root, "a pathlib chain names one file", ["fixtures/one.json"], {"tests"})
    expect(root, "...and not its neighbour", ["fixtures/other.json"], set())
    expect(root, "a join names its directory", ["joined/two/x"], {"tests"})
    expect(root, "a word list is not a read", ["docs/x.md"], set())
    expect(root, "a sys.path entry is not a read of the whole directory", ["sysdir/m.py"], set())
    expect(root, "a module tests/ imports through a sys.path entry", ["vmy/lib/guestmod.py"], {"tests"})
    expect(root, "...and not that directory's other files", ["vmy/lib/unrelated.py"], set())
    expect(root, "a deleted scripts/ module tests/ still imports", ["scripts/gone.py"], {"tests"})
    expect(root, "an empty scripts/ module tests/ imports", ["scripts/emptymod.py"], {"tests"})
    expect(root, "a pytest scratch path is not the tree", ["scratchdir/x"], set())
    expect(root, "the installer's test reaching outside its package", ["vmx/guest.conf"], {"installer"})
    expect(root, "a tests/ file", ["tests/test_a.py"], {"tests"})

    print("\n   pre_merge_gates.sh asks the plan about tests/ only on an affected-only run")
    for rel in ("pre_merge_gates.sh", "ci_plan.py", "hang_watch.py"):
        shutil.copy(os.path.join(SCRIPTS, rel), os.path.join(root, "scripts", rel))
    os.makedirs(os.path.join(root, "scripts", "gates-container-free.d"), exist_ok=True)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "runner")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    with open(os.path.join(root, "docs", "x.md"), "a") as f:
        f.write("a docs change\n")
    git(root, "commit", "-q", "-am", "docs")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRE_MERGE_", "GATE_RESULTS_DIR"))}
    env.update(GATE_RESULTS_DIR=os.path.join(root, ".results"), PRE_MERGE_SELECT_ONLY="1", PRE_MERGE_PYFLAKES="true")

    def lane(**extra):
        p = subprocess.run(["bash", os.path.join(root, "scripts", "pre_merge_gates.sh"), root],
                           capture_output=True, text=True, env={**env, **extra}, timeout=120)
        return p.stdout + p.stderr

    out = lane(PRE_MERGE_AFFECTED_ONLY="1")
    check("an affected-only run of a docs change says tests/ is not run, and why",
          "tests/ not run: no changed file is in tests/" in out, out[-600:])
    out = lane()
    check("a run that is not affected-only (a ship) never asks", "tests/ not run" not in out, out[-600:])
    out = lane(PRE_MERGE_AFFECTED_ONLY="1", PRE_MERGE_FULL="1")
    check("a full run never asks", "tests/ not run" not in out, out[-600:])


print("\n3. the wiring in check.yml and pre_merge_gates.sh")
wf = open(WORKFLOW, encoding="utf-8").read()
check("the current job makes the plan into its outputs",
      re.search(r"python3 scripts/ci_plan\.py --changed-from origin/main .*--github-output", wf, re.S) is not None)
for j in ("build", "scan", "smoke", "installer"):
    check(f"current exports {j} and {j}_why",
          f"{j}: ${{{{ steps.plan.outputs.{j} }}}}" in wf and f"{j}_why: ${{{{ steps.plan.outputs.{j}_why }}}}" in wf)
    m = re.search(rf"\n  {j}:\n(.*?)(?=\n  [a-z-]+:\n|\Z)", wf, re.S)
    body = m.group(1) if m else ""
    steps = re.split(r"\n      - ", body.split("    steps:", 1)[-1])[1:]
    ifs = [re.search(r"\n        if: (.*)", "\n" + s_) for s_ in steps]
    first = ifs[0].group(1).strip() if ifs and ifs[0] else ""
    rest = [x.group(1).strip() if x else "" for x in ifs[1:]]
    check(f"{j}: its first step says why when it is not needed",
          first == f"needs.current.outputs.{j} == 'false'" and f"needs.current.outputs.{j}_why" in steps[0], first)
    check(f"{j}: every other step runs unless the plan said false (a missing plan runs it)",
          bool(rest) and all(r == f"needs.current.outputs.{j} != 'false'" for r in rest), str(rest))
check("a full run reaches the gates job as PRE_MERGE_FULL=1",
      re.search(r"needs\.current\.outputs\.full.*?export PRE_MERGE_FULL=1", wf, re.S) is not None)
check("the full run is asked for by the ci-full label or [ci-full] in the title",
      '"ci-full" in labels' in wf and '"[ci-full]" in' in wf)
runner = open(os.path.join(SCRIPTS, "pre_merge_gates.sh"), encoding="utf-8").read()
check("pre_merge_gates.sh starts tests/ only when the plan did not say it is not needed",
      re.search(r'if \[\[ -z "\$STORE_MODE" && -z "\$TESTS_SKIP_WHY"', runner) is not None)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} — {'; '.join(FAILS)}")
    sys.exit(1)
print("all gates passed")
