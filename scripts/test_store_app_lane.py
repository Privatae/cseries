#!/usr/bin/env python3
"""Gate: a store app's gates run on their own lane, never in the kernel's.

A CI run once took 19 min: the Telegram store app's 9-minute gate was
picked as covering a kernel change, failed in the pool and ran 9 more alone.
scripts/gates-store-apps.txt now names each gate whose subject is a store app.
This drives the real scripts on a stub repo (two store apps, alpha and beta,
each with a gate; a kernel gate; a tests/ file of each kind) and pins:
  A. pre_merge_gates.sh never runs a registry gate, on a PR (affected-only,
     with the change naming both store gates) or in the full ship run, even
     one smoke invokes or gates-container-free.d lists; its pytest run leaves
     out a registry tests/ file and runs the rest
  B. store_app_gates.sh --changed-from <ref> runs exactly the gates of the apps
     the tree changed (system-apps/<id>/, the gate file, a path its line
     names) and nothing, exit 0, when no store app changed; --app and --all
  C. test_gate_coverage.py counts a registry line as a gate's registration and
     refuses a gate also listed in gates-container-free.d/ or invoked by smoke,
     and an image-baked app named as a store app
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[-900:]}]"))
    if not cond:
        FAILS.append(label)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout


GATE = '''import os
# reads {reads}
open(os.path.join(os.environ["MARKS"], "{mark}"), "w").close()
'''
TEST = '''import os
def test_it():
    open(os.path.join(os.environ["MARKS"], "{mark}"), "w").close()
'''
SMOKE = ('python3 "$SMOKE_REPO/scripts/test_k.py"\n'
         'GATE_DIR="$SMOKE_REPO/scripts/gates-container-free.d"\n'
         'STORE="$SMOKE_REPO/scripts/gates-store-apps.txt"\n')
REGISTRY = ("# store apps' gates\n"
            "test_sa_alpha.py alpha\n"
            "test_sa_beta.py  beta  lib/beta_kit.py\n"
            "tests/test_sa_tm.py alpha\n")
BASE_ENV = {k: v for k, v in os.environ.items()
            if not k.startswith(("PRE_MERGE_", "HANG_WATCH_LOG", "GATE_RESULTS_DIR"))}


def write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


with tempfile.TemporaryDirectory() as root:
    repo, marks, results = (os.path.join(root, d) for d in ("repo", "marks", "results"))
    os.makedirs(marks)
    os.makedirs(os.path.join(repo, "scripts", "gates-infra-only.d"))
    for s in ("pre_merge_gates.sh", "hang_watch.py", "pytest_supervisor.py", "test_gate_coverage.py",
              "store_app_gates.sh"):
        if os.path.exists(os.path.join(SCRIPTS, s)):
            os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
            shutil.copy(os.path.join(SCRIPTS, s), os.path.join(repo, "scripts", s))
    files = {
        "scripts/smoke_test.sh": SMOKE + 'python3 "$SMOKE_REPO/scripts/test_sa_alpha.py"\n',
        "scripts/gates-container-free.d/test_sa_beta.txt": "test_sa_beta.py\n",
        "scripts/gates-store-apps.txt": REGISTRY,
        "scripts/image_baked_apps.txt": "kernelapp\n",
        "scripts/gate-timeouts.txt": "# the stub's lane, measured 1 s\ntests/ 120\n",
        "scripts/test_k.py": GATE.format(reads="lib/kernel.py", mark="k-ran"),
        "scripts/test_sa_alpha.py": GATE.format(reads="system-apps/alpha/app.yaml", mark="alpha-ran"),
        "scripts/test_sa_beta.py": GATE.format(reads="lib/beta_kit.py", mark="beta-ran"),
        "tests/test_kernel_lane.py": TEST.format(mark="kt-ran"),
        "tests/test_sa_tm.py": TEST.format(mark="st-ran"),
        "system-apps/alpha/app.yaml": "id: alpha\n", "system-apps/beta/app.yaml": "id: beta\n",
        "system-apps/kernelapp/app.yaml": "id: kernelapp\n",
        "lib/kernel.py": "k = 1\n", "lib/beta_kit.py": "b = 1\n",
    }
    for rel, text in files.items():
        write(repo, rel, text)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    def ran():
        return sorted(os.listdir(marks))

    def clear():
        for m in os.listdir(marks):
            os.remove(os.path.join(marks, m))

    def run(argv, **env):
        clear()
        e = {**BASE_ENV, "GATE_RESULTS_DIR": results, "MARKS": marks, "PRE_MERGE_JOBS": "2",
             "PRE_MERGE_PYFLAKES": "true", **env}
        p = subprocess.run(argv, capture_output=True, text=True, env=e, timeout=300)
        return p.returncode, p.stdout + p.stderr

    pmg = ["bash", os.path.join(repo, "scripts", "pre_merge_gates.sh"), repo]
    sag = ["bash", os.path.join(repo, "scripts", "store_app_gates.sh"), "--repo", repo]

    print("\nA. the kernel lane never runs a store app's gate")
    write(repo, "system-apps/alpha/app.yaml", "id: alpha\nversion: 2\n")
    write(repo, "lib/beta_kit.py", "b = 2\n")
    git(repo, "commit", "-qam", "change both store apps")
    rc, out = run(pmg, PRE_MERGE_AFFECTED_ONLY="1")
    check("a PR run (affected-only) passes", rc == 0 and "PRE-MERGE GATES PASSED" in out, (rc, out))
    check("…and runs neither store gate, though the change names both and smoke and "
          "gates-container-free.d list them", "alpha-ran" not in ran() and "beta-ran" not in ran(), ran())
    rc, out = run(pmg, PRE_MERGE_FULL="1")
    check("the full (ship) run passes and runs the kernel gate",
          rc == 0 and "k-ran" in ran(), (rc, ran(), out))
    check("…and no store gate", "alpha-ran" not in ran() and "beta-ran" not in ran(), ran())
    check("…its pytest runs the kernel's tests/ file", "kt-ran" in ran(), (ran(), out))
    check("…and leaves out the store app's tests/ file", "st-ran" not in ran(), ran())
    # A malformed registry line drops nothing else: a bare
    # `tests/` or a directory under it would have been --ignore'd, the whole
    # tests/ lane with it; an indented comment or a trailing one is no gate.
    write(repo, "scripts/gates-store-apps.txt", REGISTRY
          + "tests/ alpha\ntests/sub/ alpha\n   # test_k.py alpha\ntest_k.py# alpha\ntest_k.py\n\n")
    rc, out = run(pmg, PRE_MERGE_FULL="1")
    check("a malformed registry line drops neither the tests/ lane nor a kernel gate",
          rc == 0 and "kt-ran" in ran() and "k-ran" in ran() and "st-ran" not in ran(),
          (rc, ran(), out))
    write(repo, "scripts/gates-store-apps.txt", REGISTRY)

    print("\nB. store_app_gates.sh runs the gates of the apps a change touched")
    os.remove(os.path.join(repo, "scripts/gates-container-free.d/test_sa_beta.txt"))
    write(repo, "scripts/smoke_test.sh", SMOKE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "store gates on the registry only")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("no store app changed: exit 0, nothing ran, and it says so",
          rc == 0 and ran() == [] and "nothing to run" in out, (rc, ran(), out))
    write(repo, "lib/kernel.py", "k = 2\n")
    git(repo, "commit", "-qam", "a kernel change")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("a kernel-only change runs no store gate", rc == 0 and ran() == [], (rc, ran(), out))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "system-apps/alpha/app.yaml", "id: alpha\nversion: 3\n")
    git(repo, "commit", "-qam", "change alpha")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("alpha changed: exactly alpha's gate and alpha's tests/ file ran",
          rc == 0 and ran() == ["alpha-ran", "st-ran"], (rc, ran(), out))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "lib/beta_kit.py", "b = 3\n")
    git(repo, "commit", "-qam", "change the path beta's line names")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("a path named on beta's line changed: exactly beta's gate ran",
          rc == 0 and ran() == ["beta-ran"], (rc, ran(), out))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "scripts/test_sa_beta.py", GATE.format(reads="lib/beta_kit.py", mark="beta-ran") + "# v2\n")
    git(repo, "commit", "-qam", "change beta's gate itself")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("the gate file itself changed: exactly that gate ran", rc == 0 and ran() == ["beta-ran"],
          (rc, ran(), out))
    # A scripts/ helper a store gate imports (the connector gates import
    # gate_loopback.py and connector_gate_kit.py) selects that gate; a helper
    # only kernel gates import selects none.
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "scripts/sa_helper.py", "h = 1\n")
    write(repo, "scripts/k_helper.py", "h = 1\n")
    write(repo, "scripts/test_sa_alpha.py",
          "import sa_helper  # noqa: F401\n" + GATE.format(reads="system-apps/alpha/app.yaml", mark="alpha-ran"))
    write(repo, "scripts/test_k.py", "import k_helper  # noqa: F401\n" + GATE.format(reads="lib/kernel.py", mark="k-ran"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "gates import helpers")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "scripts/sa_helper.py", "h = 2\n")
    git(repo, "commit", "-qam", "change the helper alpha's gate imports")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("a scripts/ helper a store gate imports changed: exactly that gate ran",
          rc == 0 and ran() == ["alpha-ran"], (rc, ran(), out))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "scripts/k_helper.py", "h = 2\n")
    git(repo, "commit", "-qam", "change a helper only a kernel gate imports")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("a helper only a kernel gate imports changed: no store gate ran", rc == 0 and ran() == [],
          (rc, ran(), out))
    # A helper the gate reaches through another helper (connector_gate_kit ->
    # kernel_schema), and one it imports as scripts.<mod>, select it too.
    write(repo, "scripts/sa_deep.py", "d = 1\n")
    write(repo, "scripts/sa_pkg.py", "p = 1\n")
    write(repo, "scripts/sa_helper.py", "import sa_deep  # noqa: F401\nh = 2\n")
    write(repo, "scripts/test_sa_beta.py",
          "try:\n    from scripts.sa_pkg import p  # noqa: F401\nexcept ImportError:\n    pass\n" + GATE.format(reads="lib/beta_kit.py", mark="beta-ran"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a helper imports a helper; beta imports scripts.sa_pkg")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "scripts/sa_deep.py", "d = 2\n")
    git(repo, "commit", "-qam", "change the helper alpha's helper imports")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("a helper a store gate reaches through another helper changed: that gate ran",
          rc == 0 and ran() == ["alpha-ran"], (rc, ran(), out))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "scripts/sa_pkg.py", "p = 2\n")
    git(repo, "commit", "-qam", "change the helper beta imports as scripts.sa_pkg")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("a helper a store gate imports as scripts.<mod> changed: that gate ran",
          rc == 0 and ran() == ["beta-ran"], (rc, ran(), out))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "tests/conftest.py", "# changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change the conftest.py pytest loads for a tests/ store gate")
    rc, out = run(sag + ["--changed-from", "origin/main"])
    check("the conftest.py of a tests/ store gate changed: that gate ran",
          rc == 0 and ran() == ["st-ran"], (rc, ran(), out))
    rc, out = run(sag + ["--app", "beta"])
    check("--app beta runs beta's gate only", rc == 0 and ran() == ["beta-ran"], (rc, ran(), out))
    rc, out = run(sag + ["--all"])
    check("--all runs every store gate", rc == 0 and ran() == ["alpha-ran", "beta-ran", "st-ran"],
          (rc, ran(), out))
    write(repo, "scripts/test_sa_beta.py", "import sys\nsys.exit(1)\n")
    rc, out = run(sag + ["--app", "beta"])
    check("a failing store gate fails the run", rc != 0 and "test_sa_beta.py" in out, (rc, out))
    git(repo, "checkout", "-q", "--", "scripts/test_sa_beta.py")

    print("\nC. the coverage gate takes the registry, and refuses a double listing")

    def coverage():
        p = subprocess.run([sys.executable, os.path.join(repo, "scripts", "test_gate_coverage.py")],
                           capture_output=True, text=True, env={**BASE_ENV, "APP_ROOT": repo}, timeout=120)
        return p.returncode, p.stdout + p.stderr

    rc, out = coverage()
    check("a store gate registered only in gates-store-apps.txt is accounted for", rc == 0, out)
    write(repo, "scripts/gates-container-free.d/test_sa_beta.txt", "test_sa_beta.py\n")
    rc, out = coverage()
    check("…also listed in gates-container-free.d: refused, naming it",
          rc != 0 and "in a kernel lane too" in out and "test_sa_beta.py" in out.split("kernel lane too")[-1],
          out)
    os.remove(os.path.join(repo, "scripts/gates-container-free.d/test_sa_beta.txt"))
    write(repo, "scripts/smoke_test.sh", SMOKE + 'python3 "$SMOKE_REPO/scripts/test_sa_alpha.py"\n')
    rc, out = coverage()
    check("…also invoked by smoke: refused, naming it",
          rc != 0 and "test_sa_alpha.py" in out.split("kernel lane too")[-1], out)
    write(repo, "scripts/smoke_test.sh", SMOKE)
    write(repo, "scripts/gates-store-apps.txt", REGISTRY.replace("test_sa_alpha.py alpha", "test_sa_alpha.py kernelapp"))
    rc, out = coverage()
    check("…an image-baked app named as a store app: refused", rc != 0 and "image-baked" in out, out)

print()
if FAILS:
    print(f"FAIL — {len(FAILS)} failure(s)")
    sys.exit(1)
print("PASS — 0 failure(s)")
