#!/usr/bin/env python3
# gate-scope: whole-tree — every gate script's scope line, and the runner's selection over all of them
"""Gate: a whole-tree gate runs on every pull request, whatever the change touched.

A pull request runs the gates whose source names a changed path, file name or
module (scripts/pre_merge_gates.sh, PRE_MERGE_AFFECTED_ONLY). A gate whose
subject is the whole tree or a whole table (every tracked file, every /api
route, every tool, every migration, every app manifest) finds what it checks
by walking, globbing or importing, so it names none of the files that can turn
it red, and that match never picked it. Measured: a pull request added two /api
routes with no route_policy.py entry and merged green, because
test_api_route_guards.py names neither route file; another put a private
document's name in a comment and merged green, because
test_public_source_clean.py names no file at all. Each was found later, at the
full gate or at the publish. Such a gate now says so on a line of its own
source (`# gate-scope: whole-tree — ...`) and the runner always selects it.

Pins:
  1. on a stub repo, a pull request whose change no gate names runs the
     whole-tree gate and only it; a failing whole-tree gate fails the run; a
     scope line that is plainly meant as one but malformed (indented, another
     case, spacing or dash) still selects its gate and is named, never dropped;
     a gate that imports a changed module (`from lib import (beta)`) or a
     helper that declares it reads the changed path (`# gate-reads: data/`)
     covers the change; PRE_MERGE_SELECT_ONLY runs nothing and is never a pass;
  2. on this tree, a planted change to a file no gate names selects every
     gate that declares itself whole-tree, and nothing else;
  3. the gates that must be whole-tree declare it (WHOLE_TREE_REQUIRED below,
     each with what it reads), the helpers that must declare what they read
     do (READS_REQUIRED), and no scope or reads line in the tree is malformed
     or names a path that does not exist.

Container-free: APP_ROOT=<repo> python3 scripts/test_whole_tree_gates.py
Runs no gate of the tree: the real tree's selection is read with
PRE_MERGE_SELECT_ONLY=1, which says what a run would run and runs nothing.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

APP_ROOT = os.environ.get("APP_ROOT", "/app")
SCRIPTS = os.path.join(APP_ROOT, "scripts")
RUNNER = os.path.join(SCRIPTS, "pre_merge_gates.sh")
# The runner's own test for a scope line, read the same way.
SCOPE = re.compile(r"^# gate-scope: whole-tree(\s|$)", re.M)
# What the runner takes as meant to be a scope line, malformed or not, and the
# form a reads line must have (scripts/pre_merge_gates.sh, SCOPE_ANY_RE and the
# gate-reads grep).
SCOPE_ANY = re.compile(r"^[ \t]*#+[ \t]*gate[-_ \t]*scope[ \t]*:", re.M | re.I)
READS = re.compile(r"^# gate-reads: (\S+)", re.M)
READS_ANY = re.compile(r"^[ \t]*#+[ \t]*gate[-_ \t]*reads[ \t]*:", re.M | re.I)
FAILS = []

# Gates that must stay whole-tree, and what each reads that no changed file
# name would lead the runner to. Dropping one's scope line fails here.
WHOLE_TREE_REQUIRED = {
    "test_public_source_clean.py": "every tracked file and bundle member",
    "test_api_route_guards.py": "every /api route in the built app",
    "test_connector_gates.py": "every route of the built app",
    "test_gate_coverage.py": "every gate script and gate list",
    "test_whole_tree_gates.py": "every gate script's scope line",
    "check_tool_lanes.py": "every tool in every registry",
    "check_capability_coverage.py": "every schema'd tool",
    "test_migrations_guarded.py": "every migration",
    "test_migration_versions.py": "every migration",
    "test_platform_texts_ask_nothing.py": "every platform text",
    "test_tools_split.py": "every runtime/tools_*.py",
    "test_no_runtime_ddl.py": "every .py file",
    "test_bundle_freshness.py": "every app's sources against its bundle",
}
# Helpers the gates import that read a whole path, and the path each declares
# (`# gate-reads: <path>`), so a change there runs the gates that use them.
READS_REQUIRED = {
    "kernel_schema.py": ("migrations/", "init-db.sql", "entrypoint.sh"),
}


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[-900:]}]"))
    if not cond:
        FAILS.append(label)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True).stdout


# The outer run's settings, which a stub run must not inherit.
_OUTER = ("PRE_MERGE_", "HANG_WATCH_LOG", "GATE_RESULTS_DIR")
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith(_OUTER)}
SELECTS = "PRE_MERGE_SELECT_ONLY" in open(RUNNER, encoding="utf-8").read()


def run(repo, results, **env):
    e = {**BASE_ENV, "GATE_RESULTS_DIR": results, "PRE_MERGE_JOBS": "2", "PRE_MERGE_PYFLAKES": "true", **env}
    p = subprocess.run(["bash", os.path.join(repo, "scripts", "pre_merge_gates.sh"), repo],
                       capture_output=True, text=True, env=e, timeout=600)
    return p.returncode, p.stdout, p.stderr


def selected(out):
    return {ln[len("would run: "):].strip() for ln in out.splitlines() if ln.startswith("would run: ")}


with tempfile.TemporaryDirectory() as root:
    print("\n1. on a stub repo, a change no gate names still runs the whole-tree gate")
    repo = os.path.join(root, "stub")
    marks = os.path.join(root, "marks")
    results = os.path.join(root, "results-stub")
    for d in (os.path.join(repo, "scripts", "gates-container-free.d"), os.path.join(repo, "lib"), marks, results):
        os.makedirs(d)
    shutil.copy(RUNNER, os.path.join(repo, "scripts", "pre_merge_gates.sh"))
    shutil.copy(os.path.join(SCRIPTS, "hang_watch.py"), os.path.join(repo, "scripts", "hang_watch.py"))
    mark = 'import os, sys\nopen(os.path.join(os.environ["MARKS"], "{0}"), "w").close()\n'
    gates = {
        # W reads every file under lib/ and names none of them.
        "test_w.py": "#!/usr/bin/env python3\n# gate-scope: whole-tree — every file under lib/\n"
                     + mark.format("w-ran")
                     + 'sys.exit(1 if os.environ.get("W_FAILS") else 0)\n',
        # N names lib/alpha.py; Z names nothing and says nothing.
        "test_n.py": "# reads lib/alpha.py\n" + mark.format("n-ran"),
        "test_z.py": mark.format("z-ran"),
        # Y, D and E mean a scope line and get it wrong: indented, no spaces,
        # another case and dash. Each still runs as whole-tree, and is named.
        "test_y.py": "if True:\n    # gate-scope: whole-tree — not at the start of its line\n    pass\n"
                     + mark.format("y-ran"),
        "test_d.py": "#gate-scope:whole-tree\n" + mark.format("d-ran"),
        "test_e.py": "# Gate-Scope: Whole-Tree \u2013 another dash\n" + mark.format("e-ran"),
        # P only talks about its scope, in prose: not a scope line.
        "test_p.py": "# the gate scope is one file, said in prose\n" + mark.format("p-ran"),
        # H uses a helper that reads data/; I and J import lib/beta.py without
        # naming its path, file name or dotted module; V imports another name.
        "test_h.py": "from helper_h import X\n" + mark.format("h-ran"),
        "test_i.py": "from lib import (\n    gamma,\n    beta,\n)\n" + mark.format("i-ran"),
        "test_j.py": "from lib import gamma, beta as b  # one line\n" + mark.format("j-ran"),
        "test_v.py": "from lib import beta_two\nbeta = 1\n" + mark.format("v-ran"),
    }
    MALFORMED = ("test_y.py", "test_d.py", "test_e.py")
    files = {"scripts/smoke_test.sh": "".join(f'python3 "$SMOKE_REPO/scripts/{g}"\n' for g in gates),
             "lib/alpha.py": "x = 1\n", "scripts/gates-container-free.d/.keep": "",
             "scripts/helper_h.py": "# gate-reads: data/ — every file under data/\nX = 1\n"}
    files.update({f"scripts/{g}": t for g, t in gates.items()})
    for rel, text in files.items():
        with open(os.path.join(repo, rel), "w", encoding="utf-8") as f:
            f.write(text)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    with open(os.path.join(repo, "lib", "planted.txt"), "w") as f:
        f.write("no gate names this file\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a change no gate names")

    def ran():
        return sorted(os.listdir(marks))

    def clear():
        for m in os.listdir(marks):
            os.remove(os.path.join(marks, m))

    rc, out, err = run(repo, results, MARKS=marks, PRE_MERGE_AFFECTED_ONLY="1")
    check("the run passes", rc == 0 and "PRE-MERGE GATES PASSED" in out, (rc, out, err))
    check("the whole-tree gate ran", "w-ran" in ran(), (ran(), out))
    check("…and so did the gates whose scope line is malformed (Y indented, D unspaced, E another case and dash)",
          {"y-ran", "d-ran", "e-ran"} <= set(ran()), (ran(), out))
    check("…and no gate that does not cover the change (N names another file, Z nothing, P only prose,"
          " H, I, J, V import what did not change)",
          ran() == ["d-ran", "e-ran", "w-ran", "y-ran"], (ran(), out))
    check("the run names each malformed scope line", all(f"scripts/{g} has a gate-scope line" in err
                                                         for g in MALFORMED), err)
    check("the run says four of the gates it ran are whole-tree", "4 of them whole-tree" in out, out)
    clear()
    rc, out, err = run(repo, results, MARKS=marks, PRE_MERGE_AFFECTED_ONLY="1", W_FAILS="1")
    check("a failing whole-tree gate fails the pull request's run",
          rc != 0 and "FAIL  test_w.py" in out + err, (rc, out, err))
    clear()
    for f in os.listdir(results):
        os.remove(os.path.join(results, f))
    rc, out, err = run(repo, results, MARKS=marks, PRE_MERGE_AFFECTED_ONLY="1", PRE_MERGE_SELECT_ONLY="1")
    check("PRE_MERGE_SELECT_ONLY names what would run: the whole-tree gates only",
          selected(out) == {"test_w.py", *MALFORMED}, (rc, out, err))
    check("…runs nothing", ran() == [], ran())
    check("…and is never a pass: it exits 2, as a run that tested nothing",
          rc == 2 and "no gate ran" in err, (rc, err))

    print("\n1b. a gate that imports a changed module, or uses a helper that reads the changed path, covers it")
    os.makedirs(os.path.join(repo, "data"))
    for rel, text in {"data/v2.sql": "select 1;\n", "lib/beta.py": "y = 2\n"}.items():
        with open(os.path.join(repo, rel), "w", encoding="utf-8") as f:
            f.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a migration and a module no gate names")
    rc, out, err = run(repo, results, MARKS=marks, PRE_MERGE_AFFECTED_ONLY="1", PRE_MERGE_SELECT_ONLY="1")
    sel = selected(out)
    check("H runs: its helper declares `# gate-reads: data/`, and data/v2.sql changed", "test_h.py" in sel, sorted(sel))
    check("I runs: `from lib import (... beta ...)` over several lines imports lib/beta.py", "test_i.py" in sel,
          sorted(sel))
    check("J runs: `from lib import gamma, beta as b` imports lib/beta.py", "test_j.py" in sel, sorted(sel))
    check("…and V (`from lib import beta_two`), N, Z and P do not",
          sel == {"test_w.py", *MALFORMED, "test_h.py", "test_i.py", "test_j.py"}, sorted(sel))

    print("\n2. on this tree, a planted change no gate names selects every whole-tree gate")
    tree = os.path.join(root, "tree")
    results2 = os.path.join(root, "results-tree")
    os.makedirs(os.path.join(tree, "scripts"))
    os.makedirs(results2)
    # What the runner reads to build its gate list: the gate scripts, smoke's
    # invocations, the shared and store lists, the timeouts. Nothing it runs.
    for name in os.listdir(SCRIPTS):
        src = os.path.join(SCRIPTS, name)
        if os.path.isfile(src) and name.endswith((".py", ".sh", ".txt")):
            shutil.copy(src, os.path.join(tree, "scripts", name))
        elif os.path.isdir(src) and name.startswith("gates-") and name.endswith(".d"):
            shutil.copytree(src, os.path.join(tree, "scripts", name))
    git(tree, "init", "-q")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "base")
    git(tree, "update-ref", "refs/remotes/origin/main", "HEAD")
    planted = f"planted-{os.getpid()}/nothing-names-this-{os.urandom(6).hex()}.txt"
    os.makedirs(os.path.join(tree, os.path.dirname(planted)))
    with open(os.path.join(tree, planted), "w") as f:
        f.write("a change to a file no gate names\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "plant")

    if not SELECTS:
        check("the runner can say what it would run (PRE_MERGE_SELECT_ONLY)", False,
              "scripts/pre_merge_gates.sh has no PRE_MERGE_SELECT_ONLY")
        every, picked = set(), set()
    else:
        rc, out, err = run(tree, results2, PRE_MERGE_FULL="1", PRE_MERGE_SELECT_ONLY="1")
        every = selected(out)
        check("the runner lists this tree's gates", rc == 2 and len(every) > 50, (rc, len(every), err))
        rc, out, err = run(tree, results2, PRE_MERGE_AFFECTED_ONLY="1", PRE_MERGE_SELECT_ONLY="1")
        picked = selected(out)
        check("the runner says what the planted change would run", rc == 2 and "affected-only" in out,
              (rc, out[-900:], err))

    declared = set()
    for g in every:
        with open(os.path.join(SCRIPTS, g), encoding="utf-8", errors="replace") as f:
            if SCOPE.search(f.read()):
                declared.add(g)
    print(f"  ({len(declared)} of {len(every)} gates declare themselves whole-tree;"
          f" the planted change selects {len(picked)})")
    check("every whole-tree gate is selected", declared <= picked, sorted(declared - picked))
    check("…and nothing else: the planted change names no other gate", picked <= declared,
          sorted(picked - declared))

    print("\n3. the gates that must be whole-tree say so")
    for g, what in sorted(WHOLE_TREE_REQUIRED.items()):
        path = os.path.join(SCRIPTS, g)
        has = os.path.isfile(path) and bool(SCOPE.search(open(path, encoding="utf-8").read()))
        check(f"{g} ({what}) declares itself whole-tree", has)
        check("…and the planted change selects it", g in picked, sorted(picked)[:20])
    for h, paths in sorted(READS_REQUIRED.items()):
        p = os.path.join(SCRIPTS, h)
        got = set(READS.findall(open(p, encoding="utf-8").read())) if os.path.isfile(p) else set()
        check(f"{h} declares that it reads {', '.join(paths)}", set(paths) <= got, sorted(got))
    bad, missing = [], []
    for name in sorted(os.listdir(SCRIPTS)):
        p = os.path.join(SCRIPTS, name)
        if not (name.endswith(".py") and os.path.isfile(p)):
            continue
        text = open(p, encoding="utf-8", errors="replace").read()
        for i, ln in enumerate(text.splitlines(), 1):
            if SCOPE_ANY.match(ln) and not SCOPE.match(ln):
                bad.append(f"scripts/{name}:{i}: {ln.strip()[:80]}")
            if READS_ANY.match(ln) and not READS.match(ln):
                bad.append(f"scripts/{name}:{i}: {ln.strip()[:80]}")
        for path in READS.findall(text):
            if not os.path.exists(os.path.join(APP_ROOT, path)):
                missing.append(f"scripts/{name}: {path}")
    check("no scope or reads line in scripts/ is malformed (each would run, or match, nothing it should)",
          not bad, bad)
    check("every path a `# gate-reads:` line names exists (a misspelt one matches no change)",
          not missing, missing)

print()
if FAILS:
    print(f"FAIL — {len(FAILS)} failure(s)")
    sys.exit(1)
print("PASS — 0 failure(s)")
