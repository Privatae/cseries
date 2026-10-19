"""Tests for sql_proxy.authorize_sql + classify_statement.

Pure-function tests. No DB. Run as:
    python3 brain/mastercode/sandbox/test_sql_proxy.py
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sql_proxy import (
    authorize_sql,
    classify_statement,
    READ_CONTEXT, READ_MEMORY, WRITE_CONTEXT, WRITE_MEMORY, ADMIN, SHELL_EXEC,
)


CASES = [
    # (sql, bits, expected_allowed, description)
    # ── READ paths ─────────────────────────────────────────────────
    ("SELECT 1", READ_MEMORY, True, "plain SELECT under READ_MEMORY"),
    ("SELECT * FROM entity_memory", READ_CONTEXT, True, "SELECT under READ_CONTEXT"),
    ("SELECT 1", 0, False, "SELECT under zero bits → deny"),
    ("WITH foo AS (SELECT 1) SELECT * FROM foo", READ_MEMORY, True, "CTE under READ"),
    ("EXPLAIN SELECT 1", READ_MEMORY, True, "EXPLAIN under READ"),
    # ── WRITE paths ────────────────────────────────────────────────
    ("INSERT INTO entity_memory (k, v) VALUES ($1, $2)", WRITE_MEMORY, True, "INSERT into allowed table"),
    ("UPDATE entity_memory SET v=$1 WHERE k=$2", WRITE_MEMORY, True, "UPDATE allowed table"),
    ("DELETE FROM entity_memory WHERE k=$1", WRITE_MEMORY, True, "DELETE allowed table"),
    ("INSERT INTO entity_registry (id, bot_capabilities) VALUES (1, 255)", WRITE_MEMORY, False, "INSERT into ADMIN-only table denied"),
    ("UPDATE entity_registry SET bot_capabilities=255 WHERE id=1", WRITE_MEMORY, False, "UPDATE entity_registry denied"),
    ("DELETE FROM ceigas_audit_log", WRITE_MEMORY, False, "DELETE audit log denied"),
    ("INSERT INTO entity_memory VALUES (1)", READ_MEMORY, False, "INSERT under READ_MEMORY only → deny"),
    ("INSERT INTO unknown_table VALUES (1)", WRITE_MEMORY, False, "INSERT into non-allowlisted table denied"),
    # ── DDL ────────────────────────────────────────────────────────
    ("DROP TABLE entity_memory", WRITE_MEMORY, False, "DROP under WRITE → deny"),
    ("CREATE TABLE foo (id INT)", WRITE_MEMORY, False, "CREATE under WRITE → deny"),
    ("ALTER TABLE entity_memory ADD COLUMN x INT", WRITE_MEMORY, False, "ALTER under WRITE → deny"),
    ("GRANT ALL ON entity_memory TO public", WRITE_MEMORY, False, "GRANT under WRITE → deny"),
    ("DROP TABLE foo", ADMIN, True, "DROP under ADMIN → allow"),
    # ── ADMIN bypass ───────────────────────────────────────────────
    ("DELETE FROM entity_registry", ADMIN, True, "ADMIN bypasses table allowlist"),
    ("CREATE TABLE foo (id INT)", ADMIN, True, "ADMIN allows DDL"),
    # ── Transactions ───────────────────────────────────────────────
    ("BEGIN", WRITE_MEMORY, True, "BEGIN under write bit"),
    ("COMMIT", WRITE_MEMORY, True, "COMMIT under write bit"),
    ("BEGIN", READ_MEMORY, False, "BEGIN under read-only → deny"),
    # ── Comment-leading SQL ────────────────────────────────────────
    ("-- audit comment\nSELECT 1", READ_MEMORY, True, "leading line comment ignored"),
    ("/* block */ SELECT 1", READ_MEMORY, True, "leading block comment ignored"),
    ("-- a\n-- b\nUPDATE entity_memory SET v=1", WRITE_MEMORY, True, "two comments then UPDATE"),
    # ── Unknown / fail-closed ─────────────────────────────────────
    ("SET search_path = public", WRITE_MEMORY, False, "SET → unknown class → deny"),
    ("LOCK TABLE entity_memory", WRITE_MEMORY, False, "LOCK → unknown → deny"),
    ("", READ_MEMORY, False, "empty SQL → deny"),
    ("   ", READ_MEMORY, False, "whitespace only → deny"),
    # ── Phase A.2 — CTE write bypass + stacked-statement guard ─────
    # The CRITICAL finding §1.4 in the security review: WITH foo AS
    # (UPDATE entity_registry ...) SELECT * FROM foo would have been
    # classified as READ and allowed. The new keyword-anywhere scan
    # denies all of these.
    (
        "WITH wrote AS (UPDATE entity_registry SET bot_capabilities=255 WHERE id=3 RETURNING *) SELECT * FROM wrote",
        WRITE_MEMORY,
        False,
        "CTE-hidden UPDATE on ADMIN-only table → deny via hidden-DML scan",
    ),
    (
        "WITH ins AS (INSERT INTO entity_registry VALUES (99) RETURNING *) SELECT * FROM ins",
        WRITE_MEMORY,
        False,
        "CTE-hidden INSERT into ADMIN-only table → deny",
    ),
    (
        "WITH d AS (DELETE FROM ceigas_audit_log RETURNING id) SELECT count(*) FROM d",
        WRITE_MEMORY,
        False,
        "CTE-hidden DELETE on audit log → deny",
    ),
    (
        "SELECT 1; UPDATE entity_registry SET bot_capabilities=255 WHERE id=3",
        READ_MEMORY,
        False,
        "stacked statement: SELECT then UPDATE → deny (UPDATE found in body)",
    ),
    (
        "SELECT 1 /* harmless */ ; DROP TABLE entity_memory",
        READ_MEMORY,
        False,
        "stacked DDL behind a comment → deny",
    ),
    (
        "-- INSERT INTO X (decoy in comment)\nSELECT 1",
        READ_MEMORY,
        True,
        "INSERT inside a line comment → stripped → still READ → allow",
    ),
    (
        "SELECT 'INSERT INTO x VALUES (1)' AS note",
        READ_MEMORY,
        True,
        "DML keyword inside a single-quoted string literal → stripped → allow",
    ),
    (
        "SELECT \"UPDATE\" FROM entity_memory",
        READ_MEMORY,
        True,
        "DML keyword used as a double-quoted identifier → stripped → allow",
    ),
    (
        "WITH r AS (SELECT * FROM entity_memory) SELECT * FROM r",
        READ_MEMORY,
        True,
        "WITH with a pure-SELECT CTE body — no DML keyword → allow",
    ),
    (
        "WITH wrote AS (UPDATE entity_memory SET v='x' WHERE k='y' RETURNING *) SELECT * FROM wrote",
        ADMIN,
        True,
        "ADMIN bypasses the hidden-DML scan",
    ),
    (
        "BEGIN; UPDATE entity_registry SET bot_capabilities=255 WHERE id=3; COMMIT",
        WRITE_MEMORY,
        False,
        "BEGIN-then-UPDATE inside a TXN-classified leading statement → deny",
    ),
    (
        "EXPLAIN UPDATE entity_registry SET bot_capabilities=255",
        WRITE_MEMORY,
        False,
        "EXPLAIN UPDATE — classifier sees EXPLAIN (READ) but UPDATE in body → deny",
    ),
]


def main() -> int:
    fail = 0
    for sql, bits, expected, desc in CASES:
        actual, reason = authorize_sql(sql, bits)
        if actual != expected:
            print(f"FAIL: {desc}")
            print(f"  sql=  {sql!r}")
            print(f"  bits= {bits:#x}")
            print(f"  expected={expected}  actual={actual}  reason={reason!r}")
            fail += 1
    print(f"{len(CASES) - fail}/{len(CASES)} cases passed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
