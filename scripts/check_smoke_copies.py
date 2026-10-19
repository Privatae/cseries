#!/usr/bin/env python3
"""Every /app/scripts/*.py that smoke runs IN the container has a COPY line.

scripts/ is copied into the image selectively (Dockerfile.linux), so a gate
script wired into smoke_test.sh without its own COPY fails as file-not-found.
smoke_test.sh checks this for itself, but smoke runs after a ~4-minute image
build, so the miss was found only then. One build stopped exactly there
(test_modules_versions.py, wired in by a merge with no COPY). This is the same
check, run from
the tree, so the pre-merge lane catches it before anything is built.

Pure: reads two files, runs nothing.
Run: APP_ROOT=<repo> python3 scripts/check_smoke_copies.py
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    smoke = open(os.path.join(ROOT, "scripts", "smoke_test.sh"), encoding="utf-8").read()
    dockerfile = open(os.path.join(ROOT, "Dockerfile.linux"), encoding="utf-8").read()
    wanted = sorted(set(re.findall(r"/app/scripts/([A-Za-z0-9_]+\.py)", smoke)))
    # Only a real COPY line counts: a name mentioned in a comment is not in
    # the image.
    copied = set(re.findall(r"^\s*COPY\b[^\n]*\bscripts/([A-Za-z0-9_]+\.py)\b", dockerfile, re.M))
    missing = [name for name in wanted if name not in copied]
    if missing:
        print("FAIL  smoke runs these in the container but Dockerfile.linux never COPYs them:")
        for name in missing:
            print(f"        {name}   (add: COPY --chown=privatae:privatae "
                  f"scripts/{name} /app/scripts/{name})")
        return 1
    print(f"OK    all {len(wanted)} in-container smoke scripts are COPY'd into the image")
    return 0


if __name__ == "__main__":
    sys.exit(main())
