#!/usr/bin/env python3
"""Gate wrapper: the Network app's Agents view words for what the bastion
keeps (v256), under node.

scripts/test_network_agent_facts.js loads the REAL system-apps/network/app.js
in a minimal DOM sandbox and pins how the account behind an agent, the file it
runs, its unit and each machine's tool servers are worded, and that every one
of those machine-written values is drawn as text. This wrapper exists so the
container-free gate list, which runs python files, can run it.
"""
import os
import subprocess
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "test_network_agent_facts.js")],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
