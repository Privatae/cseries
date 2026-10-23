#!/usr/bin/env python3
"""Every /app/scripts/*.py that smoke runs IN the container has a COPY line.

scripts/ is copied into the image selectively (Dockerfile.linux), so a gate
script wired into smoke_test.sh without its own COPY fails as file-not-found.
smoke_test.sh checks this for itself, but smoke runs after a ~4-minute image
build, so the miss was found only then. One build stopped exactly there
(test_modules_versions.py, wired in by a merge with no COPY). This is the same
check, run from
the tree, so the pre-merge lane catches it before anything is built.

A script smoke runs may import a helper from scripts/ (turn_source.py,
tools_source.py); that helper must be in the image too, or the script stops at
its import. The imports every load of a script runs (its top-level import
statements) are followed, to any depth: four gates once stopped in smoke on
`No module named 'tools_source'`, which this check, reading only smoke_test.sh,
did not see. So are the imports of Python written inline in smoke_test.sh
(`from turn_source import turn_source` after a sys.path insert of
/app/scripts). An import under an `if`, a `try` or a function runs only on the
path that reaches it, which the container may never take (test_circles_threads
exits in a pod before it imports kernel_schema), so it is not followed.

Pure: reads two files, runs nothing.
Run: APP_ROOT=<repo> python3 scripts/check_smoke_copies.py
"""
import ast
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script_imports(path: str) -> set:
    """The scripts/ modules a script's top-level import statements name
    (`import x`, `from x import y`): x.py next to it in scripts/."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        return set()
    names = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            names.add(n.module.split(".")[0])
    return {f"{m}.py" for m in names if os.path.isfile(os.path.join(ROOT, "scripts", f"{m}.py"))}


def main() -> int:
    smoke = open(os.path.join(ROOT, "scripts", "smoke_test.sh"), encoding="utf-8").read()
    dockerfile = open(os.path.join(ROOT, "Dockerfile.linux"), encoding="utf-8").read()
    wanted = sorted(set(re.findall(r"/app/scripts/([A-Za-z0-9_]+\.py)", smoke)))
    needed_by = {name: None for name in wanted}
    # Python written inline in smoke_test.sh (a heredoc, or a $'...' string
    # with \n) imports helpers from /app/scripts too (`from turn_source
    # import turn_source`); those need the same COPY.
    for m in re.finditer(r"(?:^|\\n|;)[ \t]*(?:from[ \t]+([A-Za-z_]\w*)(?:\.\w+)*[ \t]+import\b"
                         r"|import[ \t]+([A-Za-z_]\w*))", smoke, re.M):
        helper = f"{m.group(1) or m.group(2)}.py"
        if helper not in needed_by and os.path.isfile(os.path.join(ROOT, "scripts", helper)):
            needed_by[helper] = "smoke_test.sh, line %d" % (smoke.count("\n", 0, m.start()) + 1)
    todo = list(needed_by)
    while todo:
        name = todo.pop()
        path = os.path.join(ROOT, "scripts", name)
        if not os.path.exists(path):
            continue
        for helper in sorted(_script_imports(path)):
            if helper not in needed_by:
                needed_by[helper] = name
                todo.append(helper)
    wanted = sorted(needed_by)
    # Only a real COPY line counts: a name mentioned in a comment is not in
    # the image.
    copied = set(re.findall(r"^\s*COPY\b[^\n]*\bscripts/([A-Za-z0-9_]+\.py)\b", dockerfile, re.M))
    missing = [name for name in wanted if name not in copied]
    if missing:
        print("FAIL  smoke runs these in the container but Dockerfile.linux never COPYs them:")
        for name in missing:
            why = f"   (imported by {needed_by[name]})" if needed_by[name] else ""
            print(f"        {name}{why}   (add: COPY --chown=privatae:privatae "
                  f"scripts/{name} /app/scripts/{name})")
        return 1
    print(f"OK    all {len(wanted)} in-container smoke scripts are COPY'd into the image")
    return 0


if __name__ == "__main__":
    sys.exit(main())
