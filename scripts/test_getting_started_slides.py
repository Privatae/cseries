#!/usr/bin/env python3
"""Getting Started's slides are as specified, and its engine runs them.

Runs scripts/test_getting_started_slides.js, which loads the real steps.js
and app.js: the Name slide comes right after Welcome and opens Settings at
About, and the walkthrough never sends the name itself (renaming takes
Settings' own token, server/routes/host_name.py); the bastion slide is gone;
the optional Privatae network slide says apps install without an account,
names the code page and Settings, opens Settings at Network and passes once
the Host is a member. Then checks the manifest declares the scopes those
calls need, and not write:host_name.

Container-free: APP_ROOT=<repo> python3 scripts/test_getting_started_slides.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1]).resolve()
FAILS = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


node = shutil.which("node")
check(bool(node), "node is on the PATH")
if node:
    r = subprocess.run([node, str(ROOT / "scripts/test_getting_started_slides.js")],
                       capture_output=True, text=True, timeout=60)
    print(r.stdout.rstrip())
    check(r.returncode == 0, "the slides and the engine behave as specified", r.stderr[-800:])

manifest = (ROOT / "system-apps/getting-started/app.yaml").read_text()
perms = [ln.split("#")[0].strip()[2:].strip() for ln in manifest.splitlines()
         if ln.startswith("  - ") and ":" in ln.split("#")[0]]
check("read:network_membership" in perms, "Getting Started declares read:network_membership", str(perms))
check("write:host_name" not in perms,
      "Getting Started does not declare write:host_name, which only Settings may use", str(perms))
check("read:embedding" not in perms and "/api/embedding/bastions" not in
      (ROOT / "system-apps/getting-started/steps.js").read_text(),
      "the bastion check is gone with its slide")

if FAILS:
    print(f"\nFAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("\nall checks passed")
