#!/usr/bin/env python3
"""GET /api/network/nac/mgmt/purposes: the jobs the Management lane offers the
person, so a Host with no model to draft a grant can still be given one.

  1. Every job a draft can be made from without naming an image is offered,
     with its own words, what still asks every time, and exactly the ops
     grant_vocab.expand adds for it; `contain` is among them.
  2. A job that runs an image the person names (builds, service) is not
     offered: its draft would be refused without the image.
  3. The route is read-only and scoped (read:topology), like the grant list.

Run: python3 scripts/test_mgmt_purposes_route.py"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

from server.network import grant_vocab  # noqa: E402
from server.routes import network as routes  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


out = asyncio.run(routes.nac_mgmt_purposes())
offered = {p["name"]: p for p in out["purposes"]}
for name, p in grant_vocab.PURPOSES.items():
    if p.get("image"):
        check(f"{name} (runs a named image) is not offered", name not in offered)
        continue
    o = offered.get(name)
    check(f"{name} is offered", o is not None)
    if o:
        check(f"{name}: its own words", o["job"] == p["job"] and o["asks"] == (p.get("gated") or ""))
        check(f"{name}: exactly the ops a draft from it adds",
              sorted(o["ops"]) == sorted(grant_vocab.expand([name])), o["ops"])
check("contain is offered", grant_vocab.CONTAIN_OP in (offered.get("contain") or {}).get("ops", []))

route = next(r for r in routes.router.routes if getattr(r, "path", "").endswith("/nac/mgmt/purposes"))
check("GET only", route.methods == {"GET"}, route.methods)
deps = [getattr(d.call, "__qualname__", "") + repr(getattr(d.call, "__closure__", None)) for d in route.dependant.dependencies]
check("scoped by a permission dependency", bool(route.dependencies), deps)

print("FAIL: " + ", ".join(fails) if fails else f"mgmt purposes route: {len(offered)} jobs offered, all checks passed")
sys.exit(1 if fails else 0)
