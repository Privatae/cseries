#!/usr/bin/env python3
"""Ratchet: no two migration files may map to the same version number.

Run:  python3 scripts/test_migration_versions.py

WHY THIS EXISTS (measured 2026-08-15). `migrations/v095_constitutional_pack_global.sql`
was merged alongside the pre-existing `migrations/v095_context_filename_lexemes.sql`.
Both filenames are distinct, both look fine in review, and the collision is
invisible unless you replay the runner's arithmetic — `entrypoint.sh:229` reduces
a filename to an INTEGER:

    version=$(echo "$base" | sed -E 's/^v0*([0-9]+).*$/\\1/')

and then skips any file whose integer is already in `schema_migrations`
(entrypoint.sh:230-234). So of two files sharing a number, whichever the shell
glob reaches first applies and records the version — and the other is skipped
SILENTLY, on every boot, forever. Not an error, not a warning: the container
starts clean and one migration simply never runs.

The damage is worse than a migration that fails, because a failure at least
exits 1 (entrypoint.sh:236-239) and is impossible to miss. This is a migration
that never runs and never says so. On a fresh entity the loser's schema change
is absent; on an existing entity the winner is whichever was recorded first, so
the NEW migration is the one that never applies — the two cases silently
disagree about what the schema is.

It surfaced only because the smoke test counts applied-vs-present migrations
(101 of 102) and `/api/system/health/deep` reported `degraded`. That count check
tells you a migration is missing; it cannot tell you WHICH or WHY. This does.

The rule is structural, not stylistic: the version integer — not the filename —
is the primary key of a migration.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

# The exact reduction entrypoint.sh performs. Kept here as a mirror on purpose:
# if the shell ever changes how it derives the version, this gate must change
# with it, and a divergence is easier to spot in one named function.
_VERSION_RE = re.compile(r"^v0*([0-9]+).*$")


def version_of(stem: str) -> int | None:
    m = _VERSION_RE.match(stem)
    return int(m.group(1)) if m else None


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "migrations"
    if not root.is_dir():
        print(f"FAIL  no migrations directory at {root}")
        return 1

    by_version: dict[int, list[str]] = defaultdict(list)
    unparsed: list[str] = []
    for path in sorted(root.glob("v*.sql")):
        v = version_of(path.stem)
        if v is None:
            unparsed.append(path.name)
        else:
            by_version[v].append(path.name)

    collisions = {v: names for v, names in by_version.items() if len(names) > 1}

    for v, names in sorted(collisions.items()):
        print(f"FAIL  migration version {v} is claimed by {len(names)} files: "
              f"{', '.join(sorted(names))}")
        print(f"      entrypoint.sh applies whichever the glob reaches first and "
              f"SKIPS the rest — silently, on every boot. Renumber all but one to "
              f"the next free version (highest present: {max(by_version)}).")
    for name in unparsed:
        print(f"FAIL  {name} does not match v<NNN>_<slug>.sql — entrypoint.sh "
              f"cannot derive a version from it")

    if collisions or unparsed:
        return 1

    print(f"OK    {sum(len(n) for n in by_version.values())} migrations, "
          f"{len(by_version)} distinct versions, highest v{max(by_version):03d} "
          f"— no collisions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
