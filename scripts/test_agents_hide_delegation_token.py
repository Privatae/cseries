#!/usr/bin/env python3
"""GET /api/network/agents never carries an agent's delegation token.

A delegation tag is a bearer value: whoever holds it can launch an agent that
reads as REQUESTED (sanctioned) on the Host's bastions. mint_agent_delegation
(server/routes/network.py) returns it ONCE, list_agent_delegations lists the
tags "without the secret token", and v139 revokes nac_agent_delegations from
the recall role for the same reason. But the bastion reports the tag an agent
carries, ingest copies it into nac_agent_activity.delegation_token, and
list_observations SELECTed that column into every row it returned. The agents
route (scope read:topology) handed those rows out unchanged, so any app holding
read:topology received every agent's tag.

What the app needs is whether the tag is valid (`requested`, computed in SQL)
and whether one was present at all (`delegation_tag_present`, computed in SQL),
never the value.

Container-free: the real route and the real list_observations over a pool that
returns, as a database would, a row for an agent carrying a tag. The SQL itself
runs against the kernel's schema in test_agents_hide_delegation_token_pg.py.

Run:  python3 scripts/test_agents_hide_delegation_token.py
"""
import asyncio
import json
import os
import re
import sys

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server import brain_service  # noqa: E402
from server.routes import network as net_routes  # noqa: E402

# Invented: the shape mint_agent_delegation produces ("del_" + urlsafe), no real tag.
TAG = "del_INVENTEDgateTAGvalue0000"
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:400]) if extra else ""))
    if not cond:
        FAILS.append(msg)


class Row(dict):
    """asyncpg Record enough: subscript + .keys()."""


def _row(pid, tag, requested, present):
    return Row({"token_id": 7, "hostname": "box", "pid": pid, "ppid": 1, "agent_user": "u",
                # One signature per pid: one family each, so unrequested_count counts two.
                "signature": f"agent-{pid}", "cmdline": "agent --run", "llm_endpoints": "[]",
                # What the column holds. A SELECT that still projects it hands it over.
                "delegation_token": tag,
                "delegation_tag_present": present,
                "first_seen": None, "last_seen": None, "kind": "agent", "family": None,
                "origin": None, "evidence": "[]", "active": True, "requested": requested,
                "acknowledged": False, "contain_pending_since": None})


class Pool:
    def __init__(self):
        self.q = None

    async def fetch(self, q, *a):
        self.q = q
        return [_row(1001, TAG, True, True),          # valid tag
                _row(1002, TAG + "x", False, True),   # a tag, but not a valid one
                _row(1003, None, False, False)]       # no tag


def main():
    pool = Pool()
    saved = (brain_service._pool, brain_service._entity_id)
    brain_service._pool, brain_service._entity_id = pool, 9003
    try:
        body = asyncio.run(net_routes.get_agents())
    finally:
        brain_service._pool, brain_service._entity_id = saved

    wire = json.dumps(body, default=str)
    check(TAG not in wire, "the route's response carries no delegation token value", wire)
    agents = body.get("agents") or []
    check(len(agents) == 3, "all three observations are listed", len(agents))
    check(all("delegation_token" not in a for a in agents),
          "no row has a delegation_token key", [sorted(a) for a in agents])
    by_pid = {a.get("pid"): a for a in agents}
    check([by_pid.get(p, {}).get("requested") for p in (1001, 1002, 1003)] == [True, False, False],
          "requested still says which tag is valid",
          [by_pid.get(p, {}).get("requested") for p in (1001, 1002, 1003)])
    check([by_pid.get(p, {}).get("delegation_tag_present") for p in (1001, 1002, 1003)] == [True, True, False],
          "delegation_tag_present says a tag was carried, without its value",
          [by_pid.get(p, {}).get("delegation_tag_present") for p in (1001, 1002, 1003)])
    check(body.get("unrequested_count") == 2, "unrequested_count is unchanged (two families unrequested)",
          body.get("unrequested_count"))

    q = pool.q or ""
    select_list = q.split("FROM nac_agent_activity a")[0]
    # Drop parenthesised sub-expressions: the column may be READ in a predicate
    # (the EXISTS that computes `requested`), never projected as a value.
    flat = select_list
    while True:
        nxt = re.sub(r"\([^()]*\)", "", flat)
        if nxt == flat:
            break
        flat = nxt
    check("delegation_token" not in flat,
          "list_observations' SELECT list does not project the token column", flat)
    check(re.search(r"\)\s*AS delegation_tag_present", q) is not None,
          "list_observations computes delegation_tag_present in SQL", q)

    with open(os.path.join(_ROOT, "system-apps", "network", "app.js"), encoding="utf-8") as f:
        js = f.read()
    check("delegation_token" not in js, "the Network app never reads delegation_token")
    check("a.delegation_tag_present" in js, "the Network app reads delegation_tag_present for the inspector")


main()
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for m in FAILS:
        print("  - " + m)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran")
    sys.exit(1)
print(f"agents route hides the delegation token: {CHECKS}/{CHECKS} checks passed")
