#!/usr/bin/env python3
"""Gate wrapper: the Oversight app's detection line, under node.

scripts/test_oversight_detect_line.js loads the REAL system-apps/oversight/
app.js in a minimal DOM sandbox and pins that a pass which left a class out
(the bastion's `skipped`) never reads as "compared, no differences" for it.
This wrapper exists so the container-free gate list, which runs python files,
can run it.
"""
import os
import subprocess
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "test_oversight_detect_line.js")],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
