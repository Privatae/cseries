#!/usr/bin/env python3
"""Gate wrapper: the Network app's contain-pending surface, under node.

scripts/test_network_contain_pending.js loads the REAL system-apps/network/
app.js in a minimal DOM sandbox and pins the words the user reads for a kill
order the box has not carried out yet. This wrapper exists so the
container-free gate list, which runs python files, can run it.
"""
import os
import subprocess
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "test_network_contain_pending.js")],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
