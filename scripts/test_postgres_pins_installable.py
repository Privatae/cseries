#!/usr/bin/env python3
"""The database image's pinned PGDG packages stay installable.

Dockerfile.postgres pins the server and its extensions to exact versions
(the supply-chain class). PGDG's live repo drops a version once a newer
one ships: pgvector 0.8.5-1.pgdg12+1 went that way, and every build
without a cached apt layer failed with "Version '0.8.5-1.pgdg12+1' for
'postgresql-17-pgvector' was not found" (on CI, and the release build,
which runs without cache). PGDG's archive keeps every version, so the image
reads it beside the live repo, under the same signing key.

This reads the Dockerfile only (no network, no docker): every PGDG package it
installs is pinned, and the archive is a source whenever one is.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile.postgres"
LIVE = "http://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main"
ARCHIVE = "https://apt-archive.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg-archive main"
KEYRING = "[signed-by=/etc/apt/keyrings/postgresql.gpg]"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    text = DOCKERFILE.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    pgdg = re.findall(r"\b(postgresql-[\w.-]+?)(=[^\s\\]+)?(?=\s)", code)
    check("the image installs PGDG packages", bool(pgdg))
    unpinned = sorted({name for name, pin in pgdg if not pin})
    check("every PGDG package is pinned to an exact version", not unpinned, f"unpinned: {unpinned}")

    sources = re.findall(r'echo "deb (\[[^\]]*\]) (\S+ \S+ \S+)"', code)
    check("the live PGDG repo is a source", any(src == LIVE for _, src in sources), str(sources))
    check("the PGDG archive is a source, so a pinned version the live repo dropped still installs",
          any(src == ARCHIVE for _, src in sources), str(sources))
    check("both are verified against the PGDG key",
          all(key == KEYRING for key, src in sources if src in (LIVE, ARCHIVE)), str(sources))
    check("the archive line adds to the source list rather than replacing it",
          re.search(r'echo "deb [^"]*pgdg-archive main"\s*\\\s*\n\s*>> /etc/apt/sources\.list\.d/pgdg\.list', code)
          is not None)

    print(f"\npostgres pins: {'all gates passed' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
