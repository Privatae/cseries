#!/usr/bin/env python3
"""ci_plan.py: which of the forge's pull-request jobs a change needs, and why.

WHY (measured on the forge, 2026-10-28, the last 15 cseries PR runs): every
PR ran every job, whatever it changed. A run of two docs/wiki files took the
same image build, CVE scan, 5-minute smoke and 6-minute tests/ lane as a
kernel change. One runner with two slots serves every cseries job, so four PRs
pushed together queued about 70 job-minutes behind each other and each took
about 35 minutes, though one alone takes about 7.

WHAT IT DECIDES. The jobs in .gitea/workflows/check.yml whose work depends on
which files changed, each from the files that job reads:

  smoke      the files in the image (the COPY and ADD sources of
             Dockerfile.linux, the build check.yml runs), and every repo path
             scripts/smoke_test.sh names itself (its DB image, seccomp profile,
             helpers, system-apps/) or through the scripts it runs with the
             live smoke setup. The scripts smoke runs straight off the tree
             (`python3 "$SMOKE_REPO/scripts/X.py"` with only APP_ROOT or
             CSERIES_ROOT set) are not its inputs: they are the gates
             pre_merge_gates.sh derives from that same line (DERIVED there), and
             the gates job (or the store-apps job, for a store app's gate) runs
             each one whenever the change covers it.
  scan       the files in the image, and trivy's own config files.
  build      when smoke or scan runs: both need the image.
  installer  packages/cseries/, and every repo path its sources name
             (its tests read vm/).
  tests      the tests/ lane of the gates job (pre_merge_gates.sh asks this
             script when PRE_MERGE_AFFECTED_ONLY is set): tests/ itself, pytest's
             config files, the lane's runner and limit, the files in the image
             (the code the tests import), the scripts/ modules tests/ imports
             (directly or through each other), and every repo path any of
             those sources names in a string, or in a gate-reads line. A tests/
             file with a whole-tree gate-scope line runs the lane on every
             change.

The gates job's own gates (pre_merge_gates.sh, PRE_MERGE_AFFECTED_ONLY) and the
store-apps job (store_app_gates.sh --changed-from) already pick what covers the
change, so they always run and choose for themselves.

"Names a repo path": a quoted string whose first component is a top-level
entry of the tree ("dashboard/base", 'vm'; never an absolute path such as
"/app/docs", which in the image is a COPY line's and anywhere else not the
tree's), a pathlib chain of such strings (ROOT / "docs" / "wiki" is
docs/wiki), or a recursive glob ("**/*.md", rglob("*.yaml")), which matches
by file name anywhere. A directory named is everything under it. This over-
selects on purpose: a job that runs when it was not needed costs minutes; a
job skipped that the change could break is a check lost.

EVERY JOB RUNS when the run is forced (the PR carries the ci-full label or
"[ci-full]" in its title: check.yml's current job), when there is no merge
base to diff against, when no changed file is found, or when the change
touches .gitea/ or this script, since those decide what every job does.

USAGE
  ci_plan.py --changed-from origin/main [--repo R] [--full WHY] [--github-output F]
  ci_plan.py --changed-list F [...]        F: changed paths, NUL- or newline-separated
  ci_plan.py --lane tests --changed-list F prints why; exit 0 = run, 1 = not needed
  ci_plan.py --inputs JOB                  every input JOB is derived to read
Exit 0 with one line per job otherwise; any error exits 2 (callers then run all).
"""
import argparse
import ast
import fnmatch
import os
import re
import subprocess
import sys

IMAGE_DOCKERFILE = "Dockerfile.linux"   # check.yml's build job builds this one
SMOKE = "scripts/smoke_test.sh"
INSTALLER = "packages/cseries/"
TESTS = "tests/"
# pytest's rootdir and inifile discovery reads these at the root.
PYTEST_CONFIGS = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
# The tests/ lane's runner and its limit (pre_merge_gates.sh, the tests/ lane).
TESTS_LANE = ("scripts/pre_merge_gates.sh", "scripts/pytest_supervisor.py", "scripts/gate-timeouts.txt")
# trivy's default config file names; trivy.yaml's own ignorefile is read too.
TRIVY_CONFIGS = ("trivy.yaml", "trivy.yml", ".trivyignore", ".trivyignore.yaml")
# A change here changes what every job does or how it is chosen.
DECIDES_ALL = (".gitea/", "scripts/ci_plan.py")
JOBS = ("build", "scan", "smoke", "installer", "tests")
# Tree-run scripts smoke runs with only these set run as the gates job runs them.
TREE_ENV = {"APP_ROOT", "CSERIES_ROOT"}

STRING = re.compile(r"""(?P<q>["'])(?P<s>[A-Za-z0-9_.@+*/-]{1,200})(?P=q)""")
# The next piece of a path: pathlib (`"docs" / "wiki"`, `Path("docs") / "wiki"`)
# or the next argument of a join (`os.path.join(ROOT, "docs", "wiki")`).
CHAIN = re.compile(r"""\)?\s*/\s*(?P<q>["'])(?P<s>[A-Za-z0-9_.@+*-]{1,100})(?P=q)""")
JOIN_ARG = re.compile(r"""\s*,\s*(?P<q>["'])(?P<s>[A-Za-z0-9_.@+*-]{1,100})(?P=q)""")
JOIN_CALL = re.compile(r"""(?:join|joinpath|Path|PurePath|resolve)\([^()"']*$""")
# Where a one-word string ("docs", 'vm') is a path: a pathlib operand
# (ROOT / "docs", "docs" / x), a call's argument (Path("docs"), listdir("vm"),
# join(ROOT, "docs")), or a constant (DOCS = "docs"). Anywhere else (a word
# list, a dict key or value, a comparison) it is a word.
WORD_PATH_BEFORE = re.compile(r"""(?:/|(?<![=!<>])=)\s*$""")
WORD_IN_CALL = re.compile(r"""[\w.\]]\(\s*$|[\w.\]]\([^()\[\]{}]*,\s*$""")
WORD_PATH_AFTER = re.compile(r"""^\)?\s*/""")
# pytest's scratch fixtures: a path under one of these is not in the tree.
SCRATCH_NAMES = ("tmp_path", "tmpdir")
SCRATCH_FROM = re.compile(r"""^\s*(\w+)\s*=\s*(?:Path\()?\s*(?:tmp_path\b|tmpdir\b|tmp_path_factory\.|tempfile\.mkdtemp\()"""
                          r"""|with\s+tempfile\.TemporaryDirectory\([^)]*\)\s+as\s+(\w+)""", re.M)
RECURSIVE = re.compile(r"""(?:(?P<recv>\w+)?\s*\.rglob\(\s*|["']\*\*/)(?P<q>["']?)(?P<s>[A-Za-z0-9_.*?\[\]-]+)""")
# NAME = <...> "path" ...: a name bound to a path this source names.
BOUND = re.compile(r"""^\s*(\w+)\s*=\s*[^\n=]*["'][A-Za-z0-9_.-]""", re.M)
WHOLE_TREE = re.compile(r"^# gate-scope: whole-tree(\s|$)", re.M)
READS_LINE = re.compile(r"^# gate-reads: (\S+)", re.M)


class Inputs:
    """Paths a job reads: (path, is_dir, why); a name pattern matches anywhere."""

    def __init__(self):
        self.paths = {}
        self.names = {}

    def add(self, path, why, is_dir=None, repo=None):
        path = path.strip()
        while path.startswith("./"):
            path = path[2:]
        if not path or path in (".", "/"):
            return
        if is_dir is None:
            full = os.path.join(repo, path.rstrip("/")) if repo else ""
            # A path that is not there now is read both ways: the file, or
            # everything under it.
            is_dir = path.endswith("/") or (bool(full) and not os.path.isfile(full))
        key = path.rstrip("/")
        if key not in self.paths or (is_dir and not self.paths[key][0]):
            self.paths[key] = (is_dir, why)

    def add_name(self, pattern, why):
        self.names.setdefault(pattern, why)

    def update(self, other):
        for k, v in other.paths.items():
            if k not in self.paths or (v[0] and not self.paths[k][0]):
                self.paths[k] = v
        for k, v in other.names.items():
            self.names.setdefault(k, v)

    def match(self, f):
        if f in self.paths:
            return self.paths[f][1]
        parts = f.split("/")
        for i in range(len(parts) - 1, 0, -1):
            d = "/".join(parts[:i])
            if d in self.paths and self.paths[d][0]:
                return self.paths[d][1]
        for k, (is_dir, why) in self.paths.items():
            if any(c in k for c in "*?[") and (fnmatch.fnmatch(f, k) or
                                               (is_dir and fnmatch.fnmatch(f, k + "/*"))):
                return why
        base = parts[-1]
        for pat, why in self.names.items():
            if fnmatch.fnmatch(base, pat):
                return why
        return None


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True).stdout


def toplevel(repo):
    out = git(repo, "ls-files", "-z").decode("utf-8", "surrogateescape")
    return {p.split("/", 1)[0] for p in out.split("\0") if p}


def read(repo, rel):
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def logical_lines(text):
    """Dockerfile lines with their continuations joined, and each one's line number."""
    buf, start = "", 0
    for n, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
            if line.lstrip().startswith("#"):
                continue
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        buf += line
        yield start, buf
        buf = ""
    if buf:
        yield start, buf


def image_inputs(repo):
    inp = Inputs()
    inp.add(IMAGE_DOCKERFILE, f"{IMAGE_DOCKERFILE} itself", is_dir=False)
    inp.add(".dockerignore", "the build context's .dockerignore", is_dir=False)
    text = read(repo, IMAGE_DOCKERFILE)
    if not text:
        raise RuntimeError(f"no {IMAGE_DOCKERFILE} under {repo}")
    for n, line in logical_lines(text):
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        args = words[1:]
        if any(a.startswith("--from") for a in args):
            continue   # from another stage, not the context
        args = [a for a in args if not a.startswith("--")]
        if args and args[0].startswith("["):   # JSON form
            try:
                import json
                args = json.loads(" ".join(args))
            except ValueError:
                pass
        for src in args[:-1]:
            if re.match(r"^[a-z]+://|^git@", src):
                continue   # fetched, not from the tree
            inp.add(src, f"in the image ({IMAGE_DOCKERFILE}:{n} {words[0]} {src})", repo=repo)
    return inp


def literal_paths(text, top, syspath=False):
    """Repo paths a source names: strings, pathlib chains, recursive globs.
    With syspath, the paths its sys.path lines name, and nothing else."""
    paths, names = set(), set()
    # sys.path entries are where imports come from; the imports themselves are
    # followed (scripts_imports, against each such directory), so the directory
    # is not read as a whole.
    text = "\n".join(line for line in text.splitlines() if ("sys.path" in line) == syspath)
    # A path under pytest's or tempfile's scratch directory is not in the tree:
    # tmp_path / "scripts", and r / "scripts" after r = tmp_path / "repo".
    scratch = set(SCRATCH_NAMES)
    for m in SCRATCH_FROM.finditer(text):
        scratch.add(m.group(1) or m.group(2))
    scratch_root = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(scratch))) + r")\s*/\s*$")
    for m in STRING.finditer(text):
        s = m.group("s")
        end = m.end()
        line_start = text.rfind("\n", 0, m.start()) + 1
        in_join = bool(JOIN_CALL.search(text, line_start, m.start()))
        while True:   # ROOT / "docs" / "wiki" and join(ROOT, "docs", "wiki") -> docs/wiki
            c = CHAIN.match(text, end) or (JOIN_ARG.match(text, end) if in_join else None)
            if not c:
                break
            s = s.rstrip("/") + "/" + c.group("s")
            end = c.end()
        if scratch_root.search(text[max(line_start, m.start() - 120):m.start()].rsplit("(", 1)[-1]):
            continue
        if "/" not in s and "." not in s:
            before = text[line_start:m.start()]
            # A call's argument (Path("docs"), join(ROOT, "docs")), never a
            # tuple, list or set of words (in ("units", "docs")).
            if not (WORD_PATH_AFTER.match(text[end:end + 4]) or WORD_PATH_BEFORE.search(before)
                    or WORD_IN_CALL.search(before)):
                continue
        # An absolute path ("/app/docs") is not the tree's: the image's own
        # paths are its COPY lines, read above.
        while s.startswith("./"):
            s = s[2:]
        first = s.split("/", 1)[0]
        if first in top:
            paths.add(s)
    # A recursive glob from a directory this source names (DASHBOARD.rglob,
    # DASHBOARD = ROOT / "dashboard") stays in it, which the name covers; one
    # from anywhere else (ROOT.rglob) can reach any file of that name.
    bound = {b for b in BOUND.findall(text)}
    for m in RECURSIVE.finditer(text):
        pat = m.group("s")
        if m.group("recv") and m.group("recv") in bound:
            continue
        if pat and pat not in ("*", "**"):
            names.add(pat)
    return paths, names


def scripts_imports(repo, rel, text, top=()):
    """The modules one Python file imports (import X / from scripts.X) from
    scripts/ or from a repo directory it puts on sys.path: X.py there, or the
    package X/ (ending in /). A module that is not there (deleted, or never
    one) is named all the same: a change deleting it is a change to it."""
    mods = set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return mods
    roots = ["scripts"] + sorted(p.rstrip("/") for p in literal_paths(text, top, syspath=True)[0]
                                 if os.path.isdir(os.path.join(repo, p)))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module] + [node.module + "." + a.name for a in node.names]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "import_module":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        for name in names:
            name = name[len("scripts."):] if name.startswith("scripts.") else name
            head = name.split(".", 1)[0]
            if not head or head in getattr(sys, "stdlib_module_names", ()):
                continue
            for root in roots:
                if os.path.isdir(os.path.join(repo, root, head)):
                    mods.add(f"{root}/{head}/")
                else:
                    mods.add(f"{root}/{head}.py")
    return mods


def lane_inputs(repo, top, seeds, why, follow_scripts=True, files_only=()):
    """What the given sources read: each seed, its scripts/ imports (closure),
    every repo path they name, and their gate-reads lines. A seed in files_only
    counts as itself and its imports, not what it names."""
    inp = Inputs()
    todo, seen, whole = list(seeds), set(), []
    files_only = set(files_only)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        if rel.endswith("/"):   # an imported package: everything under it
            inp.add(rel, f"{why} ({rel}, an imported package)", is_dir=True)
            continue
        # Named before it is read: an imported module that is gone or empty is
        # still an input (a change that deleted or emptied it breaks the import).
        inp.add(rel, f"{why} ({rel})", is_dir=False)
        text = read(repo, rel)
        if not text:
            continue
        if WHOLE_TREE.search(text):
            whole.append(rel)
        if follow_scripts and rel.endswith(".py"):
            imports = scripts_imports(repo, rel, text, top)
            todo.extend(imports - seen)
            if rel in files_only:
                files_only.update(imports)
        if rel in files_only:
            continue
        paths, names = literal_paths(text, top)
        for p in paths:
            inp.add(p, f"{why}: {rel} names {p}", repo=repo)
        for n in names:
            inp.add_name(n, f"{why}: {rel} globs {n} anywhere")
        for p in READS_LINE.findall(text):
            inp.add(p, f"{why}: {rel} reads {p} (its gate-reads line)", repo=repo)
    return inp, whole


def py_files(repo, inp):
    """The tracked .py files under the given inputs' paths."""
    out = git(repo, "ls-files", "-z", "--", "*.py").decode("utf-8", "surrogateescape")
    return [p for p in out.split("\0") if p and inp.match(p)]


def smoke_inputs(repo, top):
    text = read(repo, SMOKE)
    if not text:
        raise RuntimeError(f"no {SMOKE} under {repo}")
    inp = Inputs()
    inp.add(SMOKE, "smoke_test.sh itself", is_dir=False)
    named, driven = set(), set()
    for line in text.splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if not code:
            continue
        for m in re.finditer(r'\$\{?SMOKE_REPO\}?/([A-Za-z0-9_./-]+)(\$)?', code):
            path, var = m.group(1), m.group(2)
            if var:
                path = path.rsplit("/", 1)[0] if "/" in path.rstrip("/") else ""
                if not path or path.rstrip("/") == "scripts":
                    continue   # "$SMOKE_REPO/scripts/$_g": the container-free pass, skipped in CI
            run = re.search(r'python3 "\$SMOKE_REPO/scripts/([a-z_0-9]+\.py)"', code)
            if run and path == f"scripts/{run.group(1)}":
                env = set(re.findall(r'([A-Z_][A-Z0-9_]*)=', code[:run.start()].rsplit("$(", 1)[-1]))
                if env <= TREE_ENV:
                    continue   # a tree gate: the gates job runs it when the change covers it
                driven.add(path)
            named.add(path)
        for m in re.finditer(r'\)/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)', code):
            if m.group(1).split("/", 1)[0] in top:
                named.add(m.group(1))
    for p in sorted(named):
        inp.add(p, f"read by smoke_test.sh ({p})", repo=repo)
    # What the helpers and the smoke-driven scripts read in turn. A driven
    # script that is whole-tree runs in the gates job on every pull request
    # whatever it reads, so here it counts as itself and its imports: smoke's
    # run of it differs only in smoke's own setup, which is smoke's input above.
    helpers = [p for p in named if p.endswith((".sh", ".py")) and os.path.isfile(os.path.join(repo, p))]
    whole = [p for p in driven if WHOLE_TREE.search(read(repo, p))]
    sub, _ = lane_inputs(repo, top, helpers, "read by what smoke_test.sh runs", files_only=whole)
    inp.update(sub)
    return inp


def installer_inputs(repo, top):
    inp = Inputs()
    inp.add(INSTALLER, "the installer's package (packages/cseries/)", is_dir=True)
    out = git(repo, "ls-files", "-z", "--", INSTALLER).decode("utf-8", "surrogateescape")
    for rel in out.split("\0"):
        # Its code, not package.json: that names the package's own files.
        if rel.endswith((".js", ".mjs", ".cjs")) and "/node_modules/" not in rel:
            paths, _ = literal_paths(read(repo, rel), top)
            for p in paths:
                if p != "packages" and not p.startswith(INSTALLER):
                    inp.add(p, f"read by the installer's tests: {rel} names {p}", repo=repo)
    return inp


def tests_inputs(repo, top, image):
    inp = Inputs()
    inp.add(TESTS, "the tests/ lane's own files", is_dir=True)
    for c in PYTEST_CONFIGS:
        inp.add(c, f"pytest's config ({c})", is_dir=False)
    for c in TESTS_LANE:
        inp.add(c, f"what runs the tests/ lane ({c})", is_dir=False)
    inp.update(image)
    out = git(repo, "ls-files", "-z", "--", TESTS).decode("utf-8", "surrogateescape")
    seeds = [p for p in out.split("\0") if p.endswith(".py")]
    sub, whole = lane_inputs(repo, top, seeds, "read by tests/")
    inp.update(sub)
    # The code the tests import lives in the image; what it names, it may read.
    # Not the image's scripts/: those are gates, and the scripts/ modules the
    # tests do import are followed above.
    code, _ = lane_inputs(repo, top, [p for p in py_files(repo, image) if not p.startswith("scripts/")],
                          "read by code the tests import", follow_scripts=False)
    inp.update(code)
    return inp, [w for w in whole if w.startswith(TESTS)]


def trivy_inputs(repo, image):
    inp = Inputs()
    inp.update(image)
    for c in TRIVY_CONFIGS:
        inp.add(c, f"trivy's config ({c})", is_dir=False)
    for m in re.finditer(r"^\s*ignorefile:\s*(\S+)", read(repo, "trivy.yaml"), re.M):
        inp.add(m.group(1), f"trivy's ignore file (trivy.yaml names {m.group(1)})", is_dir=False)
    return inp


def changed_from(repo, ref):
    try:
        base = git(repo, "merge-base", "HEAD", ref).decode().strip()
    except subprocess.CalledProcessError:
        return None
    out = git(repo, "diff", "-z", "--name-only", "--no-renames", base)
    out += git(repo, "ls-files", "-z", "-o", "--exclude-standard")
    return sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p})


def first_match(changed, inp):
    for f in changed:
        why = inp.match(f)
        if why:
            return f, why
    return None


_DERIVED = {}


def derive(repo):
    """Every job's inputs, read once per tree and process."""
    if repo not in _DERIVED:
        top = toplevel(repo)
        image = image_inputs(repo)
        smoke = smoke_inputs(repo, top)
        smoke.update(image)
        _DERIVED[repo] = {"image": image, "smoke": smoke, "scan": trivy_inputs(repo, image),
                          "installer": installer_inputs(repo, top), "tests": tests_inputs(repo, top, image)}
    return _DERIVED[repo]


def plan(repo, changed, full="", jobs=JOBS):
    """{job: (runs, why)} for the jobs asked, with every other job's verdict too."""
    if full:
        return {j: (True, f"forced full run ({full})") for j in JOBS}
    if changed is None:
        return {j: (True, "no merge base with main, so nothing is known to be unaffected") for j in JOBS}
    if not changed:
        return {j: (True, "no changed file found, so nothing is known to be unaffected") for j in JOBS}
    for f in changed:
        for d in DECIDES_ALL:
            if f == d.rstrip("/") or f.startswith(d if d.endswith("/") else d + "\0"):
                return {j: (True, f"{f} decides what every job does") for j in JOBS}
    d = derive(repo)
    out = {}
    want = set(jobs)
    if want & {"smoke", "build"}:
        hit = first_match(changed, d["smoke"])
        out["smoke"] = (True, f"{hit[0]}: {hit[1]}") if hit else (
            False, "no changed file is in the image or read by smoke_test.sh")
    if want & {"scan", "build"}:
        hit = first_match(changed, d["scan"])
        out["scan"] = (True, f"{hit[0]}: {hit[1]}") if hit else (
            False, "no changed file is in the image or is trivy's config")
    if "build" in want:
        need = [j for j in ("smoke", "scan") if out[j][0]]
        out["build"] = (True, f"{' and '.join(need)} need the image") if need else (
            False, "neither smoke nor the CVE scan needs the image: no changed file is in it")
    if "installer" in want:
        hit = first_match(changed, d["installer"])
        out["installer"] = (True, f"{hit[0]}: {hit[1]}") if hit else (
            False, "no changed file is in packages/cseries/ or read by its tests")
    if "tests" in want:
        tinp, whole = d["tests"]
        if whole:
            out["tests"] = (True, f"{whole[0]} is whole-tree: it covers every change")
        else:
            hit = first_match(changed, tinp)
            out["tests"] = (True, f"{hit[0]}: {hit[1]}") if hit else (
                False, "no changed file is in tests/, in the image, or named by tests/ or the code it imports")
    return {j: out[j] for j in JOBS if j in out}


def read_list(path):
    raw = open(path, "rb").read().decode("utf-8", "surrogateescape")
    sep = "\0" if "\0" in raw else "\n"
    return sorted({p.strip() if sep == "\n" else p for p in raw.split(sep) if p.strip()})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--changed-from")
    src.add_argument("--changed-list")
    ap.add_argument("--full", default="")
    ap.add_argument("--github-output")
    ap.add_argument("--lane", choices=JOBS)
    ap.add_argument("--inputs", choices=("smoke", "scan", "installer", "tests", "image"))
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    if a.inputs:
        d = derive(repo)
        inp = d["tests"][0] if a.inputs == "tests" else d[a.inputs]
        for k in sorted(inp.paths):
            print(f"{k}{'/' if inp.paths[k][0] else ''}\t{inp.paths[k][1]}")
        for k in sorted(inp.names):
            print(f"name {k}\t{inp.names[k]}")
        return 0
    if a.changed_list:
        changed = read_list(a.changed_list)
    elif a.changed_from:
        changed = changed_from(repo, a.changed_from)
    else:
        ap.error("say --changed-from <ref> or --changed-list <file>")
    result = plan(repo, changed, a.full, jobs=(a.lane,) if a.lane else JOBS)
    if a.lane:
        runs, why = result[a.lane]
        print(why)
        return 0 if runs else 1
    n = "no" if changed is None else len(changed)
    print(f"ci plan: {n} changed file(s)" + (f": {', '.join(changed[:5])}{' ...' if len(changed) > 5 else ''}"
                                               if changed else ""))
    for j, (runs, why) in result.items():
        print(f"  {'run ' if runs else 'skip'}  {j:<9} {why}")
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8") as fh:
            for j, (runs, why) in result.items():
                one = " ".join(why.split()).replace("%", "%25")
                fh.write(f"{j}={'true' if runs else 'false'}\n{j}_why={one}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:   # a caller reads any failure as "run every job"
        print(f"ci_plan: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
