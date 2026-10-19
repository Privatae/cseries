#!/usr/bin/env python3
"""Gate: the desktop bridge keeps the machine usable — 2026-09-03, entity 9128.

A desktop document in `desktop` mode takes over drawing the app icons from the
shell. One Host's restyled desktop rendered a backdrop, a clock and an orb —
and the user reported no way to open anything. Whatever a given document's own
reason, the shell had no floor under it: a document that draws nothing leaves
the user with no launcher at all.

The bridge (dashboard/base/js/desktop-surface.js, BRIDGE_SRC — injected into
the frame before any document script) now carries a safety net: after the app
list arrives, if the document has drawn no `[data-cseries-app]` within a grace
period, the bridge appends a default grid and renders it. The document can
still restyle or replace it; it can no longer leave the desktop empty.

Pins: the net exists in the injected source, keys on the launchable-element
selector, is gated on desktop mode + a non-empty app list, survives a document
whose <body> has not parsed yet, and the whole bridge parses as JavaScript
(node --check, when node is available).
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "dashboard/base/js/desktop-surface.js").read_text(encoding="utf-8")

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


m = re.search(r"const BRIDGE_SRC = `(.*?)`;", SRC, re.S)
check("BRIDGE_SRC is present", bool(m))
bridge = m.group(1) if m else ""

check("the net runs after every desktop:state", "safetyNet();" in bridge
      and bridge.index("port.onmessage") < bridge.index("safetyNet();"))
check("the net keys on the launchable selector",
      'document.querySelector("[data-cseries-app]")' in bridge)
check("the net is gated on desktop mode and a non-empty app list",
      'S.mode!=="desktop"||!S.apps.length' in bridge)
check("the net renders through the shell's own grid renderer (data-cseries-apps)",
      'g.setAttribute("data-cseries-apps","")' in bridge and "render(g)" in bridge)
check("the net is marked so a document can find and restyle it",
      "data-cseries-net" in bridge)
check("the net waits a grace period rather than racing the document",
      re.search(r"setTimeout\(function\(\)\{if\(document\.querySelector", bridge) is not None)
check("the net survives a document whose body has not parsed yet",
      "document.body||document.documentElement" in bridge)

node = shutil.which("node")
if node:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(bridge)
        path = fh.name
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    check("the bridge parses as JavaScript (node --check)", r.returncode == 0,
          r.stderr.strip()[:200])
    r2 = subprocess.run([node, "--check", str(ROOT / "dashboard/base/js/desktop-surface.js")],
                        capture_output=True, text=True)
    check("desktop-surface.js parses as JavaScript", r2.returncode == 0,
          r2.stderr.strip()[:200])
else:
    print("SKIP node --check (no node on this machine)")

if fails:
    print(f"FAIL: desktop bridge safety net — {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("OK: desktop bridge safety net — a desktop document can no longer leave the machine "
      "with no launchable app")
