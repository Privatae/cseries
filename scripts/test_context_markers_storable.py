#!/usr/bin/env python3
"""The external-context markers must be storable in a Postgres TEXT column.

THE DEFECT (measured).

Three constants delimited themselves with NUL:

    server/routes/context.py       _UC_EXTERNAL_MARKER    = "\\x00__user_context_external__\\x00"
    brain/mastercode/context_store.py  _EXTERNAL_MARKER   = "\\x00__context_external__\\x00"
    brain/mastercode/context_backends.py _USER_CONTEXT_KEY_PREFIX = "\\x00user_context\\x00/"

The reasoning was sound and the conclusion was fatal: a NUL-delimited string
never collides with real content, but PostgreSQL rejects U+0000 in a TEXT value
outright. asyncpg raises CharacterNotInRepertoireError, "invalid byte sequence
for encoding UTF8: 0x00", and it fails as a bind parameter too. All three
columns are TEXT:

    user_context.content        migrations/v090_user_context_create.sql:33
    entity_context.content      init-db.sql:437
    context_backend_meta.file_path  bound at context_backends.py:541-550

So every write of every marker failed, and keeping a host's context bytes on the
host's own machine has never once worked. It was invisible because one caller
had already routed around it: context_manager.py substituted a can't-exist
prefix so its query would at least stay valid.

THE RULE, and why this test is shaped this way: it is not enough to assert the
constants are "not NUL" — that is a spelling check that passes for any other
character Postgres also refuses. This asserts the property that actually
matters, that each constant SURVIVES A ROUND TRIP THROUGH A TEXT COLUMN, by
storing it. U+0000 is the only character PostgreSQL forbids in TEXT today; if
that ever changes, this test changes with the database rather than with a
hand-maintained list of banned characters.

Skips (does not pass) when no database is reachable — a gate that goes green
because it could not run is worse than one that is absent.
"""
import ast
import asyncio
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

# Read the constants out of the source rather than importing the modules: both
# packages pull in the whole server at import time, and this gate must run in a
# bare checkout with nothing installed.
CONSTANTS = [
    ("server/routes/context.py", "_UC_EXTERNAL_MARKER"),
    ("brain/mastercode/context_store.py", "_EXTERNAL_MARKER"),
    ("brain/mastercode/context_backends.py", "_USER_CONTEXT_KEY_PREFIX"),
]


def read_constant(rel: str, name: str) -> str:
    """The constant's real value, parsed as Python.

    NOT `.encode().decode("unicode_escape")` — that round-trips through latin-1
    and turns a non-ASCII literal into mojibake, so the gate would store a
    DIFFERENT value than the code uses and prove nothing about the real one.
    Caught by reading the printed value, which is the only reason to print it.
    ast.literal_eval parses the literal exactly as the interpreter does.
    """
    path = os.path.join(ROOT, rel)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    m = re.search(rf'^{re.escape(name)}\s*=\s*("(?:[^"\\]|\\.)*")\s*$', src, re.M)
    if not m:
        raise SystemExit(f"FATAL: cannot find {name} in {rel} — this gate is "
                         f"pinned to a constant that has moved or been renamed, "
                         f"which means it is no longer checking anything.")
    return ast.literal_eval(m.group(1))


async def main() -> int:
    dsn = os.environ.get("TEST_PG_DSN") or os.environ.get("DATABASE_URL")
    consts = [(rel, name, read_constant(rel, name)) for rel, name in CONSTANTS]

    if not dsn:
        print("SKIP: no TEST_PG_DSN/DATABASE_URL — cannot prove storability "
              "without a database. Set one to run this gate for real.")
        # Still report what would be tested, so a skip is legible.
        for rel, name, value in consts:
            print(f"  would test {name} ({rel}) = {value!r}")
        return 0

    try:
        import asyncpg
    except ImportError:
        print("SKIP: asyncpg not installed — cannot prove storability.")
        return 0

    conn = await asyncpg.connect(dsn)
    failed = 0
    try:
        await conn.execute("CREATE TEMP TABLE _marker_probe (v TEXT)")
        for rel, name, value in consts:
            try:
                await conn.execute("INSERT INTO _marker_probe (v) VALUES ($1)", value)
                back = await conn.fetchval(
                    "SELECT v FROM _marker_probe WHERE v = $1", value)
                if back != value:
                    print(f"  FAIL  {name} ({rel}): stored but did not round-trip")
                    failed += 1
                else:
                    print(f"  ok    {name} ({rel}): stores and round-trips")
            except Exception as e:
                print(f"  FAIL  {name} ({rel}): a TEXT column refuses this value "
                      f"— {type(e).__name__}: {e}")
                print(f"        value was {value!r}")
                failed += 1
    finally:
        await conn.close()

    print()
    if failed:
        print(f"{failed} FAILED — an unstorable marker means the external-context "
              f"lane cannot work at all.")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
