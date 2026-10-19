#!/usr/bin/env python3
"""Ratchet: the Response Shaping slider preview bands must match the backend renderer.

The graded tendency prose exists twice by design (contained here rather
than dedup'd):

  server: brain/mastercode/runtime/prompt_builder.py
          _weights_to_tendencies `bands` dict + band_index thresholds —
          what the entity's system prompt actually says.
  client: system-apps/settings/app.js
          RS_BANDS + rsBand — the LIVE PREVIEW under each slider of the
          Settings app's Response Shaping tab.

A server prose edit that skips the client makes the preview lie about the
prompt the slider produces (the exact trust surface the sliders were
built on). Full dedup would mean serving prose over an endpoint and a
circles bundle rebuild for zero behavior change — this gate makes the
duplication un-driftable instead: it AST-parses the python, regex-parses
the JS, and fails on any dimension/band/threshold mismatch.

Runs in-tree, no DB, no docker. Called from scripts/smoke_test.sh.
Exit 0 = in sync. Exit 1 = drift (or either side unparseable — a parse
failure is drift in disguise, fail closed).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY_PATH = REPO / "brain/mastercode/runtime/prompt_builder.py"
CLIENT_PATH = REPO / "system-apps/settings/app.js"


def server_bands() -> tuple[dict[str, list[str]], list[float]]:
    tree = ast.parse(PY_PATH.read_text())
    bands: dict[str, list[str]] | None = None
    thresholds: list[float] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_weights_to_tendencies"):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Assign)
                        and any(getattr(t, "id", "") == "bands" for t in sub.targets)
                        and isinstance(sub.value, ast.Dict)):
                    bands = {
                        k.value: [e.value for e in v.elts]
                        for k, v in zip(sub.value.keys, sub.value.values)
                    }
                if (isinstance(sub, ast.FunctionDef)
                        and sub.name == "band_index"):
                    for cmp_ in ast.walk(sub):
                        if isinstance(cmp_, ast.Compare):
                            c = cmp_.comparators[0]
                            if isinstance(c, ast.Constant):
                                thresholds.append(float(c.value))
    if bands is None:
        raise SystemExit("FAIL: could not parse server bands dict")
    return bands, thresholds


def client_bands() -> tuple[dict[str, list[str]], list[float]]:
    text = CLIENT_PATH.read_text()
    m = re.search(r"var RS_BANDS\s*=\s*\{(.*?)\n\s*\};", text, re.S)
    if not m:
        raise SystemExit("FAIL: could not find RS_BANDS in the Settings app")
    bands: dict[str, list[str]] = {}
    for dim_m in re.finditer(r"(\w+):\s*\[(.*?)\],", m.group(1), re.S):
        prose = re.findall(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'", dim_m.group(2))
        bands[dim_m.group(1)] = [(a or b).replace('\\"', '"').replace("\\'", "'") for a, b in prose]
    fn = re.search(r"function rsBand\(v\) \{.*?\n\s*\}", text, re.S)
    if not fn:
        raise SystemExit("FAIL: could not find rsBand in the Settings app")
    thresholds = [float(v) for v in re.findall(r"v < ([0-9.]+)", fn.group(0))]
    return bands, thresholds


def main() -> int:
    s_bands, s_thresh = server_bands()
    c_bands, c_thresh = client_bands()
    fails: list[str] = []

    if s_thresh != c_thresh:
        fails.append(f"band thresholds differ: server={s_thresh} client={c_thresh}")

    for dim in sorted(set(s_bands) | set(c_bands)):
        sv, cv = s_bands.get(dim), c_bands.get(dim)
        if sv is None:
            fails.append(f"{dim}: in the Settings app but not the server renderer")
            continue
        if cv is None:
            fails.append(f"{dim}: in the server renderer but not the Settings app")
            continue
        if len(sv) != 5 or len(cv) != 5:
            fails.append(f"{dim}: band count server={len(sv)} client={len(cv)} (want 5)")
            continue
        for i, (a, b) in enumerate(zip(sv, cv)):
            if a != b:
                fails.append(f"{dim}[{i}]: server={a!r} != client={b!r}")

    if fails:
        print("FAIL: band drift between prompt_builder and the Settings app (RS_BANDS):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"PASSED: {len(s_bands)} dimensions x 5 bands + thresholds in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
