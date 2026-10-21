#!/usr/bin/env python3
"""Gate wrapper: the Memory app's Slots lens and the Compute app's model-call
list, under node.

scripts/test_app_lists_say_when_cut.js loads the REAL system-apps/oracle and
system-apps/compute app.js in a minimal DOM sandbox and pins that each list
says when its cap cut it and renders every server string as text. This
wrapper exists so the container-free gate list, which runs python files, can
run it.
"""
import os
import subprocess
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "test_app_lists_say_when_cut.js")],
                       capture_output=True, text=True, timeout=120)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
