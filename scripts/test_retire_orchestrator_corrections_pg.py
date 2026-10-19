#!/usr/bin/env python3
"""Gate: migrations/v227 switches off the five orchestrator-era learned rules,
and nothing else.

The Host calls an agent only for untrusted work outside the
platform; everything in the platform it does itself. Five Training Memory
rules on one Host say the opposite: ids
459, 177, 89, 542 and 69 there. The seed below carries their text as read on
that Host (gentle, role 'coding', no origin). They reach a turn
only through the similarity lane, which matches nothing today for want of a
vector at the Host's embedding size; a backfill would bring them back, so
v227 retires them first.

The database is the kernel's current schema (scripts/kernel_schema.py), which
has already run v227 over a database with no rules in it. Two Hosts, each
seeded with the five rules plus the rules that must stay: the other learned
rules of the same import (one hard, one gentle), a rule the person gave with
an origin that quotes one of the five word for word, and a rule of their own
that talks about agents in other words.

v227's file is run over those rows, then run again (the second run changes
nothing and does not fail). Every retired row keeps its text and still passes
the tamper digest.

Server: TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres
through docker (kernel_schema.throwaway_server). SKIP-LOUD (exit 2) with
neither.

    APP_ROOT=<repo> python3 scripts/test_retire_orchestrator_corrections_pg.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "brain"))

import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

V227 = ROOT / "migrations" / "v227_retire_orchestrator_era_corrections.sql"
SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")

# As read on one Host (the first 260 characters of each; the rest
# of 177 and 542 is filler of the same kind).
RETIRED = {
    459: ("When describing entity capabilities, emphasize management and orchestration of "
          "software factories rather than direct coding. Entities facilitate and coordinate "
          "development processes, they do not write code themselves."),
    177: ("When a user expresses repeated frustration after multiple failed attempts to resolve "
          "an issue, acknowledge the frustration explicitly, apologize for the repeated failures, "
          "and immediately offer to escalate to another agent or provide an alternative path "
          "forward rather than attempting another fix cycle."),
    89: ("When user requests system revert or restoration, immediately acknowledge the request and "
         "execute restoration procedures without proceeding with new operations. Do not continue "
         "with planned dispatches or changes until restoration is complete and confirmed."),
    542: ("When discussing technical system design, prioritize structural and functional "
          "requirements over personality-driven behavioral guidance. For coding agents, specify "
          "only role-based instructions and semantic retrieval parameters—exclude entity memory "
          "injection and personality traits."),
    69: ("When identifying system architecture problems, clearly distinguish between harness-level "
         "issues and entity-level behavioral issues. Do not conflate orchestrator design constraints "
         "with entity response quality. Provide complete analysis before truncating."),
}
KEPT = [
    # The same import's other rules (ids 364 hard, and a gentle one).
    ("When a user reports that a problem persists after your suggested solution, acknowledge the "
     "failure explicitly, apologize for the wasted time, and pivot immediately to deeper "
     "investigation or alternative approaches.", "hard", "coding", None),
    ("When a user reports technical errors or blockers, immediately acknowledge the specific "
     "error and provide alternative troubleshooting steps.", "gentle", "coding", None),
    # A rule the person gave, quoting one of the five word for word.
    ("Entities facilitate and coordinate development processes, they do not write code "
     "themselves.", "hard", None, "user_correction"),
    # The person's own rule about agents, in other words.
    ("Escalate to me, not to another agent, when a fix fails twice.", "gentle", None, None),
]
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


async def seed(conn) -> list[int]:
    hosts = []
    for name in ("Host A", "Other"):
        e = await conn.fetchval("INSERT INTO entity_registry (entity_type, display_name, status) "
                                "VALUES ('native', $1, 'active') RETURNING id", name)
        hosts.append(e)
        for text in RETIRED.values():
            await conn.execute("INSERT INTO behavioral_corrections (entity_id, correction, severity, role) "
                               "VALUES ($1, $2, 'gentle', 'coding')", e, text)
        for text, sev, role, origin in KEPT:
            await conn.execute("INSERT INTO behavioral_corrections "
                               "(entity_id, correction, severity, role, origin) VALUES ($1, $2, $3, $4, $5)",
                               e, text, sev, role, origin)
    return hosts


async def active(conn, e: int) -> set[str]:
    return {r["correction"] for r in await conn.fetch(
        "SELECT correction FROM behavioral_corrections WHERE entity_id=$1 AND active", e)}


async def transition(dsn: str) -> None:
    from mastercode.runtime.behavioral_correction_digest import (
        compute_correction_digest, validate_correction_row)
    conn = await asyncpg.connect(dsn)
    try:
        hosts = await seed(conn)
        # A digest on every seeded row, as a committed rule carries one.
        for r in await conn.fetch("SELECT * FROM behavioral_corrections"):
            await conn.execute("UPDATE behavioral_corrections SET row_digest=$2 WHERE id=$1",
                               r["id"], compute_correction_digest(dict(r)))
        everything = set(RETIRED.values()) | {t for t, *_ in KEPT}
        for e in hosts:
            check(f"before v227's file runs over them, all nine rules are active on Host {e}", await active(conn, e) == everything)

        sql = V227.read_text()
        await conn.execute(sql)
        try:
            await conn.execute(sql)
            again = None
        except Exception as ex:                                   # pragma: no cover
            again = ex
        check("v227 applies, and a second run does not fail", again is None, repr(again))

        for e in hosts:
            left = await active(conn, e)
            check(f"none of the five orchestrator-era rules is active on Host {e}",
                  not (left & set(RETIRED.values())), str(left & set(RETIRED.values())))
            check(f"...and every other rule is (the import's others, the person's quote, their own) on Host {e}",
                  left == {t for t, *_ in KEPT}, str(left))
        rows = await conn.fetch("SELECT * FROM behavioral_corrections WHERE NOT active")
        check("ten rows retired in all, five per Host, each kept with its text",
              len(rows) == 10 and {r["correction"] for r in rows} == set(RETIRED.values()))
        check("every retired row still passes the tamper digest",
              all(validate_correction_row(dict(r))[0] for r in rows),
              str([(r["id"], validate_correction_row(dict(r))) for r in rows]))
    finally:
        await conn.close()


def main() -> int:
    print("[v227] the orchestrator-era learned rules are switched off; nothing else is")
    if not V227.exists():
        print(f"  FAIL {V227.name} does not exist")
        return 1
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
              "this gate proves nothing without one.")
        return 2
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv:
        with kernel_schema_db(srv) as dsn:
            asyncio.run(transition(dsn))
    if failures:
        print(f"\n[v227] FAIL ({len(failures)} failing)")
        return 1
    print("\n[v227] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
