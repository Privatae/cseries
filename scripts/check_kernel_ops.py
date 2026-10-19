#!/usr/bin/env python3
"""Invariant: kernel_op surface integrity (Phase 2 of software-for-the-AI).

The generic host→kernel-route bridge (runtime/kernel_ops.py) is safe only
while these hold:

  1. kernel_op is registered everywhere it must be: META_TOOL_CAPS (caps
     gate), PUBLIC_TOOL_SCHEMAS + PUBLIC_META_TOOL_NAMES (schema + inline
     dispatch), DIRECT_OP_TOOLS (the operate lane).
  2. Every exposed prefix resolves to at least one real route in the baked
     contract — a dead prefix means the doc advertises a surface that 404s.
  3. Walled paths actually exist in the contract, or, for a WebSocket path,
     in the generator's websocket_routes (a walled path that vanished means
     the wall protects nothing and the route may have moved).
  4. No exposed+unwalled route's scope maps to 0 entity bits — that would
     be a surface every call fail-closes on; wall it or extend the entity
     vocabulary deliberately.
  5. The credential-reveal route is walled.

Run in-pod: docker exec <c> python3 /app/scripts/check_kernel_ops.py
"""
import json
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_BRAIN", "/app/brain"))

from mastercode.public_tool_registry import (
    PUBLIC_META_TOOL_NAMES, PUBLIC_TOOL_SCHEMAS,
)
from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
from mastercode.runtime.kernel_ops import (_EXPOSED_PREFIXES, _WALLED_PATHS,
                                           _WALLED_OPS)
from mastercode.runtime.meta_tool_caps import ENTITY_SCOPE_TO_BITS, META_TOOL_CAPS

DOCS = os.environ.get("CSERIES_DOCS", "/app/docs")

fails: list[str] = []

# 1 — registered everywhere
if "kernel_op" not in META_TOOL_CAPS:
    fails.append("kernel_op missing from META_TOOL_CAPS (default-deny at call)")
if "kernel_op" not in PUBLIC_TOOL_SCHEMAS:
    fails.append("kernel_op missing from PUBLIC_TOOL_SCHEMAS (never offered)")
if "kernel_op" not in PUBLIC_META_TOOL_NAMES:
    fails.append("kernel_op missing from PUBLIC_META_TOOL_NAMES (no inline dispatch)")
if "kernel_op" not in DIRECT_OP_TOOLS:
    fails.append("kernel_op missing from DIRECT_OP_TOOLS (not operate-routable)")

# 2-5 — against the baked contract
try:
    caps = json.load(open(os.path.join(DOCS, "capabilities.json")))
    routes = caps["api_contract"]["routes"]
except Exception as e:
    print(f"FAIL: cannot read capabilities.json: {e}")
    sys.exit(1)
paths = {r["path"] for r in routes}
# The contract carries HTTP routes only, so a wall on a WebSocket path is checked
# against the WebSocket list the generator records beside it.
ws_paths = set(caps.get("websocket_routes", []))


def _exposed(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _EXPOSED_PREFIXES)


for p in _EXPOSED_PREFIXES:
    if not any(rp == p or rp.startswith(p + "/") for rp in paths):
        fails.append(f"exposed prefix {p} matches no contract route (dead surface)")
for w in _WALLED_PATHS:
    if w not in paths and w not in ws_paths:
        fails.append(f"walled path {w} not in contract — the route moved; re-wall it")
# Method-aware walls must also still name a real route, and must sit INSIDE an
# exposed prefix — a walled op outside one is dead config that reads as
# protection while protecting nothing.
for m, w in _WALLED_OPS:
    if (m, w) not in {(r["method"], r["path"]) for r in routes}:
        fails.append(f"walled op {m} {w} not in contract — the route moved; re-wall it")
    elif not _exposed(w):
        fails.append(f"walled op {m} {w} is not under an exposed prefix — "
                     "it is already unreachable, so this wall is misleading; remove it")

for r in routes:
    if _exposed(r["path"]) and r["path"] not in _WALLED_PATHS:
        sc = r.get("scope")
        if sc is not None and not ENTITY_SCOPE_TO_BITS.get(sc, 0):
            fails.append(
                f"exposed route {r['method']} {r['path']} scope {sc!r} maps to 0 "
                "entity bits — every call would fail-close; wall it or add the "
                "scope to the entity vocabulary deliberately")

if "/api/applications/sites/db/reveal" not in _WALLED_PATHS:
    fails.append("sites/db/reveal (credential bytes) is not walled")

if fails:
    for f in fails:
        print(f"FAIL: {f}")
    sys.exit(1)

n = sum(1 for p in paths if _exposed(p) and p not in _WALLED_PATHS)
print(f"OK: kernel_op surface — registered in all four tables; "
      f"{n} paths exposed, {len(_WALLED_PATHS)} walled, "
      f"{len(_WALLED_OPS)} method-walled")
