#!/usr/bin/env python3
"""Gate: finding containment against a REAL PostgreSQL — migration v172, the
reconcile's lapse and late-answer SQL, the one-open-order index, and the reaper.

WHY A REAL DATABASE. scripts/test_contain_finding.py drives the same functions
against a recording pool, which hands back whatever the test says `lapsed` or
the late-answer window returns — so the SQL that computes them never ran.
Here the real functions (topology._reconcile_finding_contain,
request_contain_finding, expire_lapsed_containments) run through asyncpg
against a throwaway postgres:18.4 with the real migrations applied, and time is
the database's own now(). Skips LOUDLY (exit 2) without Docker: a silent skip
proves nothing.

THE SCHEMA IS THE KERNEL'S. This gate used to apply three
hand-picked migrations (v065, v164, v172) to a stock postgres:18.4 over a hand
subset of two tables. v179 then added nac_finding_contain.approval, the code
writes it, and the gate failed on main with "column approval does not exist" —
correct code, stale copy. It now runs on the kernel's database image
(cseries-postgres: init-db.sql needs pgvector and AGE) with the schema the kernel
builds: scripts/kernel_schema.py runs entrypoint.sh's own schema section — init-db.sql,
v139, then EVERY migration in order — on a fresh database, so the next
migration is in this gate the day it lands.

Server: CONTAIN_TEST_DSN / PEER_TEST_DSN / TEST_DSN when set, else a throwaway
cseries-postgres started here (CONTAIN_TEST_PG_NAME names it).

Run:  APP_ROOT=<repo> python3 scripts/test_contain_finding_sql.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

FP = "0123456789abcdef0123456789abcdef"
FP2 = "fedcba9876543210fedcba9876543210"
ENTITY, TOKEN = 9003, 7

# The rows the functions join against, in the REAL tables: a bastion token of
# this entity and its heartbeat.
FIXTURE = (
    ("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
     "VALUES ($1, $2, 'gate', '\\x00'::bytea, 'contain-gate', 'bastion')", (TOKEN, ENTITY)),
    ("INSERT INTO network_bastion_heartbeat (token_id, hostname, last_seen_at) VALUES ($1, 'box', now())",
     (TOKEN,)),
)

FAILURES: list[str] = []
PASSES = 0


def check(label: str, ok: bool, detail="") -> None:
    global PASSES
    if ok:
        PASSES += 1
        print(f"  ok    {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail != "" else ""))


async def run(dsn: str) -> None:
    import asyncpg
    from server.network import nac_mgmt, topology
    audits: list = []

    async def fake_audit(pool, eid, server, op, exit_code=None, decision=None, **kw):
        audits.append((decision, exit_code))
    nac_mgmt.audit_mgmt = fake_audit

    conn = await asyncpg.connect(dsn)
    try:
        for sql, args in FIXTURE:
            await conn.execute(sql, *args)
        # ── the migration, applied again on top of every later one ──
        sql = open(os.path.join(_ROOT, "migrations", "v172_nac_finding_contain.sql")).read()
        await conn.execute(sql)
        await conn.execute(sql)
        check("v172 applies twice (idempotent)", True)

        # ── one open order per fingerprint, enforced by the index ──
        q1 = await topology.request_contain_finding(conn, 9003, 7, FP)
        q2 = await topology.request_contain_finding(conn, 9003, 7, FP)
        check("the first order is queued, a second for the same fingerprint is 'open'",
              (q1, q2) == ("queued", "open"), (q1, q2))

        # ── delivery, then the box's answer clears it with its own words ──
        oid = await conn.fetchval("SELECT id FROM nac_finding_contain WHERE fp=$1", FP)
        result: dict = {}
        await topology._reconcile_finding_contain(conn, 9003, 7, {"hostname": "box"}, result)
        # Since v179 an order carries the person's approval to the box; one placed
        # without an approval is delivered with approval None.
        check("a pending order is delivered with its id (and no approval)",
              result.get("contain_findings") == [{"id": oid, "fp": FP, "approval": None}], result)
        result = {}
        await topology._reconcile_finding_contain(conn, 9003, 7, {"hostname": "box", "contain_results": [
            {"id": oid, "fp": FP, "outcome": "killed", "stopped": 2, "at": "x"}]}, result)
        row = await conn.fetchrow("SELECT cleared_at, outcome, stopped FROM nac_finding_contain WHERE id=$1", oid)
        check("the answer clears it: outcome and count stored, not delivered again",
              row["cleared_at"] is not None and row["outcome"] == "killed" and row["stopped"] == 2
              and "contain_findings" not in result, (dict(row), result))
        q3 = await topology.request_contain_finding(conn, 9003, 7, FP)
        check("once cleared, the same fingerprint can be ordered again", q3 == "queued", q3)

        # ── the lapse, on the database's own clock ──
        await conn.execute("DELETE FROM nac_finding_contain")
        await conn.execute("INSERT INTO nac_finding_contain (token_id, fp, requested_at) "
                           "VALUES (7, $1, now() - interval '2 hours'), (7, $2, now() - interval '5 minutes')",
                           FP, FP2)
        result = {}
        await topology._reconcile_finding_contain(conn, 9003, 7, {"hostname": "box"}, result)
        rows = {r["fp"]: r for r in await conn.fetch("SELECT fp, cleared_at, outcome FROM nac_finding_contain")}
        check("an order older than the TTL lapses as 'expired' (computed by the database)",
              rows[FP]["outcome"] == "expired" and rows[FP]["cleared_at"] is not None, dict(rows[FP]))
        check("an order inside the TTL is still delivered and open",
              rows[FP2]["cleared_at"] is None and result.get("contain_findings") == [
                  {"id": await conn.fetchval("SELECT id FROM nac_finding_contain WHERE fp=$1", FP2), "fp": FP2,
                   "approval": None}],
              (dict(rows[FP2]), result))

        # ── v179: the approval given with the order rides to the box ──
        await conn.execute("DELETE FROM nac_finding_contain")
        await topology.request_contain_finding(conn, ENTITY, TOKEN, FP, approval="signed-approval-1")
        result = {}
        await topology._reconcile_finding_contain(conn, ENTITY, TOKEN, {"hostname": "box"}, result)
        got = result.get("contain_findings") or [{}]
        check("an order placed with an approval delivers that approval (v179)",
              got[0].get("approval") == "signed-approval-1", result)

        # ── a late answer corrects an expiry — inside the window only ──
        await conn.execute("DELETE FROM nac_finding_contain")
        ids = {}
        for label, fp, outcome, cleared in (("recent", FP, "expired", "30 minutes"),
                                            ("old", FP2, "expired", "3 hours"),
                                            ("done", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "killed", "10 minutes")):
            ids[label] = await conn.fetchval(
                "INSERT INTO nac_finding_contain (token_id, fp, requested_at, cleared_at, outcome) "
                f"VALUES (7, $1, now() - interval '4 hours', now() - interval '{cleared}', $2) RETURNING id",
                fp, outcome)
        answers = [{"id": ids["recent"], "fp": FP, "outcome": "killed", "stopped": 1, "at": "x"},
                   {"id": ids["old"], "fp": FP2, "outcome": "killed", "stopped": 1, "at": "x"},
                   {"id": ids["done"], "fp": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "outcome": "in_baseline",
                    "stopped": 0, "at": "x"}]
        await topology._reconcile_finding_contain(conn, 9003, 7, {"hostname": "box", "contain_results": answers}, {})
        got = {r["id"]: r["outcome"] for r in await conn.fetch("SELECT id, outcome FROM nac_finding_contain")}
        check("a late answer rewrites an expiry inside the window", got[ids["recent"]] == "killed", got)
        check("…but not one expired outside the window", got[ids["old"]] == "expired", got)
        check("…and never rewrites an outcome that was not 'expired'", got[ids["done"]] == "killed", got)
        check("the rewrite is audited once", [a for a in audits if a[0].startswith("late answer")] == [
            (f"late answer killed finding {FP[:12]} (1 stopped) — was recorded expired", 0)], audits)

        # ── the reaper, for a box that stopped beating ──
        await conn.execute("DELETE FROM nac_finding_contain")
        await conn.execute("INSERT INTO nac_finding_contain (token_id, fp, requested_at) "
                           "VALUES (7, $1, now() - interval '2 hours')", FP)
        n = await topology.expire_lapsed_containments(conn)
        out = await conn.fetchval("SELECT outcome FROM nac_finding_contain WHERE fp=$1", FP)
        check("the reaper lapses a finding order no beat will expire", n == 1 and out == "expired", (n, out))
    finally:
        await conn.close()


def main() -> int:
    server = (os.environ.get("CONTAIN_TEST_DSN") or os.environ.get("PEER_TEST_DSN")
              or os.environ.get("TEST_DSN"))
    if not server and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing "
              "without one.", file=sys.stderr)
        return 2
    with (contextlib.nullcontext(server) if server
          else throwaway_server(os.environ.get("CONTAIN_TEST_PG_NAME"))) as srv:
        with kernel_schema_db(srv, entity_id=ENTITY) as dsn:
            print("finding containment against a real PostgreSQL, on the kernel's schema")
            asyncio.run(run(dsn))
    print(f"\n{PASSES}/{PASSES + len(FAILURES)} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
