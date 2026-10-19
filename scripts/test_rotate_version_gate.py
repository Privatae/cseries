#!/usr/bin/env python3
"""The rotate directive's client-version gate — fleet safety, so it gets a gate of its own.

`server/network/topology.py:735 _probes_before_committing` decides whether a bastion is told to
move relay endpoints. A client older than 0.13.13 applies a changed singular endpoint BLIND and
persists it; if it cannot reach the new one it loses its tunnel, and the correction rides the
tunnel that just died. There is no way back in band — the box has to be touched by hand.

So the only safe direction for this function is FALSE. An unknown, malformed or missing version
must read as "cannot probe", because being wrong that way costs a withheld directive and being
wrong the other way costs a machine nobody can reach. That asymmetry is what this file pins, and
it is why the unknown-input cases below are not padding.

Measured on the fleet the day the gate shipped (2026-09-07): 22 clients on 0.13.13 and one on
0.13.9+lab. The gate held that one on the edge relay while the other 22 moved to the router —
exactly the behaviour asserted here.

Container-free: imports the module directly, no database, no network.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from server.network.topology import _probes_before_committing as probes  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {name}")


print("clients that CAN probe — the directive is safe to send")
check("the floor itself, 0.13.13", probes("0.13.13"), True)
check("later patch, 0.13.14", probes("0.13.14"), True)
check("later minor, 0.14.0", probes("0.14.0"), True)
check("later major, 1.0.0", probes("1.0.0"), True)
check("build suffix on a new-enough client, 0.14.0+lab", probes("0.14.0+lab"), True)
check("prerelease suffix, 0.14.0-rc1", probes("0.14.0-rc1"), True)
check("surrounding whitespace is not a version difference", probes("  0.13.13  "), True)
check("patch compares numerically not lexically, 0.13.100", probes("0.13.100"), True)
check("minor compares numerically, 0.100.0", probes("0.100.0"), True)

print("\nclients that CANNOT probe — the directive must be withheld")
check("one patch below the floor, 0.13.12", probes("0.13.12"), False)
check("the version really on the fleet, 0.13.9+lab", probes("0.13.9+lab"), False)
check("an old pii build, 0.11.1-pii", probes("0.11.1-pii"), False)
check("much older, 0.9.13", probes("0.9.13"), False)

print("\nunknown input — the safe direction is FALSE, always")
check("empty string", probes(""), False)
check("None", probes(None), False)
check("not a version at all", probes("garbage"), False)
check("truncated, 0.13", probes("0.13"), False)
check("non-numeric component, 0.13.x", probes("0.13.x"), False)
check("leading dot, .13.13", probes(".13.13"), False)
check("empty components, ...", probes("..."), False)

print()
if FAILS:
    print(f"FAILED — {len(FAILS)} case(s):")
    for f in FAILS:
        print(f"  {f}")
    sys.exit(1)
print("PASSED — the gate only ever restricts, and unknown reads as cannot-probe")
