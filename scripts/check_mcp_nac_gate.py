#!/usr/bin/env python3
"""Invariant: mcp-nac-gate (P3, the real inbound wall).

The /mcp device->host gate refuses a QUARANTINED bastion before any dispatch, permits a
healthy one, and EXEMPTS bearer-only (NULL fp) + internal loopback callers — full access
for a pasted token is the user's sovereign choice.

Imports the MCP route module, so it runs in-pod:
  docker exec <c> python3 /app/scripts/check_mcp_nac_gate.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.routes.mcp import _nac_gate


class _FakeNac:
    """Stand-in PDP: evaluate() returns a canned decision so this tests the GATE wiring
    (exempt/deny/permit branching), not the decision logic (covered by check_nac_engine
    + check_posture_redecides)."""
    def __init__(self, allowed):
        self._allowed = allowed

    async def evaluate(self, token_id):
        return {"allowed": self._allowed, "why": "test", "posture": "degraded"}


async def run():
    fails = []
    device = {"id": 5, "hardware_fingerprint": "FP-DEVICE", "entity_id": 1}
    bearer_only = {"id": 6, "hardware_fingerprint": None, "entity_id": 1}
    internal = {"_internal": True, "id": 0, "hardware_fingerprint": None}

    # device-class token, PDP quarantines -> gate DENIES (returns the decision)
    if await _nac_gate(device, nac=_FakeNac(False)) is None:
        fails.append("device-class quarantined token must be DENIED by the gate")
    # device-class token, PDP allows -> gate PERMITS (returns None)
    if await _nac_gate(device, nac=_FakeNac(True)) is not None:
        fails.append("device-class permitted token must pass the gate")
    # bearer-only (NULL fp) -> EXEMPT even if the PDP would deny (sovereign full access)
    if await _nac_gate(bearer_only, nac=_FakeNac(False)) is not None:
        fails.append("bearer-only (NULL fp) token must be EXEMPT")
    # internal loopback -> EXEMPT
    if await _nac_gate(internal, nac=_FakeNac(False)) is not None:
        fails.append("internal loopback caller must be EXEMPT")
    # no policy authored (default-preserve) -> PERMIT: a real engine with no pool and
    # no rules admits, which is the engine's own default.
    from server.network.nac_controller import NACController
    if await _nac_gate(device, nac=NACController(pool=None, entity_id=None)) is not None:
        fails.append("device token must be permitted when no policy is authored (default-preserve)")
    # no engine loaded at all -> REFUSE a governed device. In this process the server's
    # lifespan never ran, so get_nac_controller() answers None, as it does in the server
    # when the engine's import or construction failed at start.
    if await _nac_gate(device, nac=None) is None:
        fails.append("a governed device must be refused when no network engine is loaded")
    return fails


def main():
    fails = asyncio.run(run())
    if fails:
        print("MCP-NAC-GATE CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("MCP-NAC-GATE INVARIANTS OK — quarantined device denied, healthy permitted, "
          "bearer-only + internal exempt, default-preserve permits, no engine refuses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
