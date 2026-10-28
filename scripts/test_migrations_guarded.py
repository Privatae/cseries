#!/usr/bin/env python3
# gate-scope: whole-tree — every migration
"""Migration guard lint — the invariant ratchet for the v080 crashloop class.

A migration that touches a table ONLY via `ALTER TABLE IF EXISTS <t>` is
declaring the table may be absent (lazily-created schemas like harness_usage).
Any OTHER statement in the same file that names that table unguarded
(CREATE INDEX ... ON <t>, bare ALTER TABLE <t>, INSERT INTO <t>) hard-fails
on databases where the table is missing — which crashloops the pod on update
(entity-9102, on one update). This lint fails the suite
the moment such a file lands.

SECOND INVARIANT, same shape, different failure: a migration that
COMMITs inside a DO block must never be run inside an enclosing transaction.
v148 backfills in committed batches so a large table cannot hold every row lock
for the length of a boot-blocking migration — and `COMMIT` inside PL/pgSQL is
legal ONLY when the block is not already in a transaction. Two things can break
that, both silently and from a distance: a top-level BEGIN; in the same file, or
psql gaining -1/--single-transaction in entrypoint.sh's apply loop. Either turns
the batching into an instant boot failure — measured on cseries-postgres:smoke,
both produce `ERROR: invalid transaction termination` while the
unwrapped block runs clean. Neither is visible from the
migration itself, so both are checked here.

Pure static check — no database. Run: python3 scripts/test_migrations_guarded.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIG_DIR = ROOT / "migrations"
ENTRYPOINT = ROOT / "entrypoint.sh"

# DO blocks carry an arbitrary dollar-quote TAG ($$ or $backfill$), and the tag
# is what makes the block's own body unambiguous — so match the tag and require
# the same one to close it.
DO_BLOCK_RE = re.compile(r"DO\s+\$(\w*)\$(.*?)\$\1\$", re.S | re.I)
# A standalone COMMIT statement, not the word inside a comment or an identifier.
COMMIT_RE = re.compile(r"^\s*COMMIT\s*;", re.M | re.I)
BEGIN_RE = re.compile(r"^\s*BEGIN\s*;", re.M | re.I)

IF_EXISTS_RE = re.compile(r"ALTER\s+TABLE\s+IF\s+EXISTS\s+(\w+)", re.I)
# Unguarded statements that name a table directly.
UNGUARDED_RES = (
    re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+(\w+)", re.I),
    re.compile(r"ALTER\s+TABLE\s+(?!IF\s+EXISTS)(\w+)", re.I),
    re.compile(r"INSERT\s+INTO\s+(\w+)", re.I),
)


def strip_guarded_blocks(sql: str) -> str:
    """Drop DO $$ ... $$ blocks — statements inside are assumed guarded (the
    to_regclass pattern); the lint only polices top-level statements."""
    return re.sub(r"DO\s+\$\$.*?\$\$", "", sql, flags=re.S | re.I)


def main() -> int:
    failures = []
    for f in sorted(MIG_DIR.glob("*.sql")):
        sql = f.read_text()
        optional = set(t.lower() for t in IF_EXISTS_RE.findall(sql))
        if not optional:
            continue
        top = strip_guarded_blocks(sql)
        for rx in UNGUARDED_RES:
            for tbl in rx.findall(top):
                if tbl.lower() in optional:
                    failures.append(
                        f"{f.name}: table {tbl!r} is IF-EXISTS-optional but an "
                        f"unguarded statement names it — wrap it in "
                        f"DO $$ ... IF to_regclass('{tbl}') IS NOT NULL ... $$"
                    )
    # ── COMMIT-inside-DO must not be wrapped in a transaction ───────────
    commit_in_do = []
    for f in sorted(MIG_DIR.glob("*.sql")):
        sql = f.read_text()
        blocks = DO_BLOCK_RE.findall(sql)
        if not any(COMMIT_RE.search(body) for _tag, body in blocks):
            continue
        commit_in_do.append(f.name)
        # The same file must not open its own transaction. Look at the text
        # OUTSIDE every DO block: `BEGIN` also opens a PL/pgSQL block, and that
        # one is not a transaction.
        top = DO_BLOCK_RE.sub("", sql)
        if BEGIN_RE.search(top):
            failures.append(
                f"{f.name}: COMMITs inside a DO block AND carries a top-level "
                f"BEGIN; — PL/pgSQL cannot COMMIT inside an enclosing "
                f"transaction, so this file aborts the boot it first applies "
                f"on with 'ERROR: invalid transaction termination'. "
                f"Drop the BEGIN/COMMIT wrapper (psql -f autocommits each "
                f"statement) or drop the batching."
            )

    # ── and the apply loop must not wrap them either ────────────────────
    if commit_in_do:
        if not ENTRYPOINT.exists():
            failures.append(
                f"entrypoint.sh not found, so the apply loop could not be "
                f"checked — {', '.join(commit_in_do)} COMMIT inside a DO block "
                f"and are only safe if psql applies them without a wrapping "
                f"transaction"
            )
        else:
            apply_lines = [ln for ln in ENTRYPOINT.read_text().splitlines()
                           if "psql" in ln and '-f "$f"' in ln]
            if not apply_lines:
                # Fail LOUDLY rather than pass on a check that found nothing:
                # the loop being unrecognisable is exactly when this matters.
                failures.append(
                    "entrypoint.sh: could not find the per-migration `psql "
                    "... -f \"$f\"` line, so the no-single-transaction "
                    "invariant is unverified. If the apply loop was rewritten, "
                    "update this lint with it — "
                    + ", ".join(commit_in_do) + " depend on it."
                )
            for ln in apply_lines:
                if re.search(r"(?:^|\s)(?:-1|--single-transaction)(?:\s|$)", ln):
                    failures.append(
                        "entrypoint.sh: the migration apply line runs psql with "
                        "--single-transaction/-1, which wraps every migration "
                        "in one transaction. "
                        + ", ".join(commit_in_do) + " COMMIT inside a DO block "
                        "and will abort the boot: `ERROR: invalid transaction "
                        "termination`."
                    )

    if failures:
        print("FAILED: migration guard lint")
        for msg in failures:
            print("  -", msg)
        return 1
    print(f"PASSED: migration guard lint ({len(commit_in_do)} batched-COMMIT "
          f"migration(s) verified unwrapped: {', '.join(commit_in_do) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
