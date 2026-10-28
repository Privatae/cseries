#!/usr/bin/env python3
"""list_observations, run on the kernel's schema, never returns a delegation token.

A delegation tag is a bearer value (v139 revokes nac_agent_delegations from the
recall role for it; v233 lists nac_agent_activity as SECRET because the same
value is copied per agent). list_observations used to SELECT
nac_agent_activity.delegation_token into every row, and GET /api/network/agents
(read:topology) handed it to every app holding that scope.

Why a real database: `requested` and `delegation_tag_present` are computed in
SQL. A stub pool would assert my reading of that SQL rather than the SQL;
test_agents_hide_delegation_token.py is the container-free half.

Rows: an agent with a valid tag, one with an expired tag, one with a revoked
tag, one with none. Each must come back with the right `requested` and
`delegation_tag_present`, and no token value may appear anywhere in the output.

Run:  python3 scripts/test_agents_hide_delegation_token_pg.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other container-free _pg
      gates do, so the pre-merge lane runs it.)
"""
import asyncio
import contextlib
import json
import os
import subprocess
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    # Exit 2 is a failure in the gate runner, never a pass.
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the SQL that computes requested and "
          "delegation_tag_present.")
    sys.exit(2)

from server.network import nac_agents  # noqa: E402
from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

ENTITY = 1
TOKEN = 90011
# Invented values in the shape mint_agent_delegation produces.
VALID = "del_INVENTEDvalidTAG000000"
EXPIRED = "del_INVENTEDexpiredTAG0000"
REVOKED = "del_INVENTEDrevokedTAG0000"
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:400]) if extra else ""))
    if not cond:
        FAILS.append(msg)


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await pool.execute(
            "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
            "VALUES ($1, $2, 'gate', '\\x00'::bytea, 'deleg-gate', 'bastion')", TOKEN, ENTITY)
        await pool.execute(
            "INSERT INTO nac_agent_delegations (token, server, expires_at, revoked_at) VALUES "
            "($1, '*', now() + interval '1 hour', NULL), "
            "($2, '*', now() - interval '1 hour', NULL), "
            "($3, 'box', now() + interval '1 hour', now())", VALID, EXPIRED, REVOKED)
        for pid, tag in ((1001, VALID), (1002, EXPIRED), (1003, REVOKED), (1004, None)):
            await pool.execute(
                "INSERT INTO nac_agent_activity (token_id, hostname, pid, signature, cmdline, "
                "delegation_token, kind) VALUES ($1, 'box', $2, $3, 'agent --run', $4, 'agent')",
                TOKEN, pid, f"agent-{pid}", tag)

        out = await nac_agents.list_observations(pool, ENTITY)
        by_pid = {r.get("pid"): r for r in out}
        check(sorted(by_pid) == [1001, 1002, 1003, 1004], "all four observations are listed", sorted(by_pid))
        wire = json.dumps(out, default=str)
        check(not any(t in wire for t in (VALID, EXPIRED, REVOKED)),
              "no delegation token value appears in list_observations' output", wire)
        check(all("delegation_token" not in r for r in out), "no row carries a delegation_token key",
              [sorted(r) for r in out])
        req = [by_pid.get(p, {}).get("requested") for p in (1001, 1002, 1003, 1004)]
        check(req == [True, False, False, False],
              "requested: only the valid, unexpired, unrevoked tag counts", req)
        pres = [by_pid.get(p, {}).get("delegation_tag_present") for p in (1001, 1002, 1003, 1004)]
        check(pres == [True, True, True, False],
              "delegation_tag_present: true whenever a tag was carried, valid or not", pres)
    finally:
        await pool.close()
    return 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=ENTITY) as _dsn:
    rc = asyncio.run(main(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"list_observations hides the delegation token: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
