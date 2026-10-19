#!/usr/bin/env python3
"""Gate: the Training Memory loader's soft lane matches whole words, scoped by
role.

Host parity, on the path that brings
Training Memory rules (behavioral_corrections) to the Host: the
training_memory loader's soft lane matched the first ten words of the context
as substrings, so 'i' or 'a' matched nearly every rule; it now matches whole
words of four letters or more, scoped by role.

The constitution's hard lane by spike category and its similarity-lane
notice were checked here too; both left constitution/context.py with
the simulated psyche, which now reads
rules from entity_memory only, so their checks went with them.

Each check below fails on the tree before the whole-word change.

Server: TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres
through docker (kernel_schema.throwaway_server). SKIP-LOUD (exit 2) with
neither.

    APP_ROOT=<repo> python3 scripts/test_learned_rule_lanes_pg.py
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

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
failures: list[str] = []

SOFT_CODING = "Name the failing test first."
SOFT_UNSCOPED = "Say which file you changed."


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


async def run(dsn: str) -> None:
    from mastercode.behavioral_corrections import load_corrections

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        e = await pool.fetchval("INSERT INTO entity_registry (entity_type, display_name, status) "
                                "VALUES ('native', 'Host A', 'active') RETURNING id")
        await pool.execute(
            "INSERT INTO behavioral_corrections (entity_id, correction, severity, role, trigger_context) VALUES "
            "($1, $2, 'gentle', 'coding', 'when a test fails'), "
            "($1, $3, 'gentle', NULL, 'when a file changes')",
            e, SOFT_CODING, SOFT_UNSCOPED)

        # The loader's soft lane: whole words, by role.
        tiny = await load_corrections(pool, e, conversation_context="i a an to")
        check("short words match no soft rule ('i', 'a': a substring of nearly every context)",
              SOFT_CODING not in tiny and SOFT_UNSCOPED not in tiny, tiny)
        part = await load_corrections(pool, e, conversation_context="testing changes")
        check("a word matches whole words only ('testing' is not 'test')", SOFT_CODING not in part, part)
        unscoped = await load_corrections(pool, e, conversation_context="the test fails and the file changes")
        check("with no role, the soft lane takes only the rules that name none",
              SOFT_UNSCOPED in unscoped and SOFT_CODING not in unscoped, unscoped)
        scoped = await load_corrections(pool, e, conversation_context="the test fails", role="coding")
        check("with role='coding' it takes that role's rule", SOFT_CODING in scoped, scoped)
    finally:
        await pool.close()


def main() -> int:
    print("[learned-rule lanes] the soft lane: whole words, by role")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
              "this gate proves nothing without one.")
        return 2
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv:
        with kernel_schema_db(srv) as dsn:
            asyncio.run(run(dsn))
    if failures:
        print(f"\n[learned-rule lanes] FAIL ({len(failures)} failing)")
        return 1
    print("\n[learned-rule lanes] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
