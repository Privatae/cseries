#!/usr/bin/env python3
"""Gate: migrations/v206 on the kernel's real schema — the old asset catalog
and the three coding-agent packs leave, and nothing else does.

The old open-design catalog goes, and so do the coding packs,
skills the Host never needs or uses. The database is the kernel's current
schema (scripts/kernel_schema.py), put back as a Host had it just before v206:
the tree's own migrations that made the catalog (v009, v010, v011, v012, v024)
run again over it, which gives back both catalog tables with their seeds and
projects.design_system_slug with its index. Two active native Hosts
are seeded the way the packs reached a live Host (v004, v005, v007 and v008 run
again, now that there is a Host to seed), with the rest of what v206 removes
and the things it must not:

  removed
    - every hard rule with origin external:superpowers, external:karpathy or
      external:ecc (13 per Host: the count read on a live Host);
    - every pack skill file, found by its 'Origin: external:' line (8 per Host,
      as on a live Host);
    - every file a catalog install wrote: factory-assets/<kind>s/<slug>/ and
      that kind's fixed files (DESIGN.md, tokens.css, components.html;
      SKILL.md), the files uninstall used to remove;
    - both catalog tables, with every row: the OpenDesign seeds (v010, v012)
      and a design made with the retired design-from-a-website feature
      (origin url-import, like a design made from a company website);
    - projects.design_system_slug and its index;
  kept
    - a rule the person gave (another origin), and a pack-like rule with no
      origin;
    - a skill file the person wrote, and one they wrote at a pack's own path
      before the seed ran (the seed was ON CONFLICT DO NOTHING, so the file at
      that path is theirs and has no Origin line);
    - a non-skill context file that quotes an Origin line;
    - a file the Host or the person wrote under factory-assets/ themselves:
      a note beside an installed design system's files, and one elsewhere
      under that prefix (uninstall always left those alone);
    - the project that named a design system, minus the column.

v206 is applied, then applied again by hand (a second run changes nothing and
does not fail). Before any of that, a fresh Host's schema, built through v206,
has no catalog tables and no design-system column.

Mutation-checked by hand: with the skills/ delete
left unfiltered (every skills/ row instead of the marked ones), the two
"stay" checks on the person's files fail. With the install-file delete
widened back to every factory-assets/ row (the first draft of v206), the
"stay" check fails on the two files the Host wrote there.

Server: TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres
through docker (kernel_schema.throwaway_server). SKIP-LOUD (exit 2) with
neither.

    APP_ROOT=<repo> python3 scripts/test_remove_old_catalog_pg.py
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

import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

MIG = ROOT / "migrations"
V206 = MIG / "v206_remove_old_catalog_and_coding_packs.sql"
SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
PACK_ORIGINS = ["external:superpowers", "external:karpathy", "external:ecc"]
# The files seed() puts where a catalog install wrote them (catalog.py's
# install_path plus the kind's fixed files), named one by one.
INSTALL_FILES = ["factory-assets/design_systems/runwayml/DESIGN.md",
                 "factory-assets/design_systems/runwayml/tokens.css",
                 "factory-assets/design_systems/runwayml/components.html",
                 "factory-assets/artifact_skills/fal-tryon/SKILL.md"]
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def migration(prefix: str) -> str:
    [f] = list(MIG.glob(f"{prefix}_*.sql"))
    return f.read_text()


# The migrations that made the old catalog, in order (v024 adds the CHECK on
# catalog_entries.kind). Each is idempotent, so run over the current schema they
# put back what v206 dropped, and the seeds.
CATALOG_MIGRATIONS = ("v009", "v010", "v011", "v012", "v024")


async def put_catalog_back(conn) -> None:
    for prefix in CATALOG_MIGRATIONS:
        await conn.execute(migration(prefix))


async def seed(conn) -> dict:
    """Two Hosts seeded as the packs reached a live Host, plus what must survive."""
    a = await conn.fetchval("INSERT INTO entity_registry (entity_type, display_name, status) "
                            "VALUES ('native', 'Sage', 'active') RETURNING id")
    b = await conn.fetchval("INSERT INTO entity_registry (entity_type, display_name, status) "
                            "VALUES ('native', 'Other', 'active') RETURNING id")
    # Host b's person wrote a file at a pack's own path before the seed ran.
    await conn.execute("INSERT INTO entity_context (entity_id, file_path, content_type, content) "
                       "VALUES ($1, 'skills/deep_research.md', 'text/markdown', "
                       "'My own research steps.')", b)
    for p in ("v004", "v005", "v007", "v008"):
        await conn.execute(migration(p))
    for e in (a, b):
        await conn.execute("INSERT INTO entity_context (entity_id, file_path, content_type, content) VALUES "
                           "($1, 'skills/my_own.md', 'text/markdown', 'How I ship a site.'), "
                           "($1, 'notes/packs.md', 'text/markdown', "
                           "'Quoted: Origin: external:superpowers/executing-plans'), "
                           "($1, 'factory-assets/design_systems/runwayml/DESIGN.md', 'text/markdown', 'x'), "
                           "($1, 'factory-assets/design_systems/runwayml/tokens.css', 'text/css', 'x'), "
                           "($1, 'factory-assets/design_systems/runwayml/components.html', 'text/html', 'x'), "
                           "($1, 'factory-assets/artifact_skills/fal-tryon/SKILL.md', 'text/markdown', 'x'), "
                           # Written by the Host or the person, not by an install:
                           # a note beside the installed files, and one elsewhere.
                           "($1, 'factory-assets/design_systems/runwayml/notes.md', 'text/markdown', "
                           "'What I changed for the shop.'), "
                           "($1, 'factory-assets/palettes/mine.md', 'text/markdown', 'My palette.')", e)
        await conn.execute("INSERT INTO behavioral_corrections (entity_id, correction, severity, origin) "
                           "VALUES ($1, 'Lead with the answer.', 'hard', 'user_correction'), "
                           "($1, 'Think before coding, my way.', 'hard', NULL)", e)
    cid = await conn.fetchval(
        "INSERT INTO catalog_entries (kind, slug, title, publisher_name, origin) "
        "VALUES ('design_system', 'example-site', 'example.com', 'Sage', 'url-import') RETURNING catalog_id")
    await conn.execute("INSERT INTO catalog_installs (entity_id, catalog_id, install_path) "
                       "VALUES ($1, $2, 'factory-assets/design_systems/privatae-ai/')", a, cid)
    pid = await conn.fetchval("INSERT INTO projects (entity_id, name, design_system_slug) "
                              "VALUES ($1, 'site', 'runwayml') RETURNING id", a)
    return {"a": a, "b": b, "project": pid}


async def counts(conn, e: int) -> dict:
    return {
        "pack_rules": await conn.fetchval(
            "SELECT count(*) FROM behavioral_corrections WHERE entity_id=$1 AND origin = ANY($2::text[])",
            e, PACK_ORIGINS),
        "pack_files": await conn.fetchval(
            "SELECT count(*) FROM entity_context WHERE entity_id=$1 AND file_path LIKE 'skills/%' "
            "AND content LIKE '%Origin: external:%'", e),
        "assets": await conn.fetchval(
            "SELECT count(*) FROM entity_context WHERE entity_id=$1 AND file_path = ANY($2::text[])",
            e, INSTALL_FILES),
    }


async def gone(conn) -> dict:
    """What v206 removes from the schema itself."""
    return {
        "entries": await conn.fetchval("SELECT to_regclass('public.catalog_entries') IS NOT NULL"),
        "installs": await conn.fetchval("SELECT to_regclass('public.catalog_installs') IS NOT NULL"),
        "column": await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='projects' AND column_name='design_system_slug')"),
        "index": await conn.fetchval("SELECT to_regclass('public.idx_projects_design_system_slug') IS NOT NULL"),
    }


NONE_LEFT = {"entries": False, "installs": False, "column": False, "index": False}


async def transition(dsn: str) -> dict:
    conn = await asyncpg.connect(dsn)
    try:
        await put_catalog_back(conn)
        ids = await seed(conn)
        a, b = ids["a"], ids["b"]
        before_a, before_b = await counts(conn, a), await counts(conn, b)
        print(f"       before: Host a {before_a}, Host b {before_b}, "
              f"catalog_entries {await conn.fetchval('SELECT count(*) FROM catalog_entries')}")
        check("the seed reproduces a live Host: 13 pack rules and 8 pack files on a Host",
              before_a == {"pack_rules": 13, "pack_files": 8, "assets": 4}, str(before_a))
        check("...and the person's own file at a pack path kept the seed out (7 pack files)",
              before_b["pack_files"] == 7, str(before_b))
        check("the catalog holds both seeded kinds and the url-import design",
              await conn.fetchval("SELECT count(DISTINCT kind) FROM catalog_entries") == 2
              and await conn.fetchval("SELECT count(*) FROM catalog_entries WHERE origin='url-import'") == 1)
        check("before v206 the tables, the column and its index are there",
              await gone(conn) == {"entries": True, "installs": True, "column": True, "index": True})

        sql = V206.read_text()
        await conn.execute(sql)
        try:
            await conn.execute(sql)
            again = None
        except Exception as e:                                   # pragma: no cover
            again = e
        check("v206 applies, and a second run does not fail", again is None, repr(again))

        after_a, after_b = await counts(conn, a), await counts(conn, b)
        check("no pack rule, pack file or catalog asset is left on either Host",
              after_a == after_b == {"pack_rules": 0, "pack_files": 0, "assets": 0}, f"{after_a} {after_b}")
        check("no rule with a pack origin is left anywhere",
              await conn.fetchval("SELECT count(*) FROM behavioral_corrections WHERE origin = ANY($1::text[])",
                                  PACK_ORIGINS) == 0)
        check("the person's rules stay (another origin, and none)",
              await conn.fetchval("SELECT count(*) FROM behavioral_corrections WHERE entity_id = ANY($1::int[]) "
                                  "AND (origin IS NULL OR origin = 'user_correction')", [a, b]) == 4)
        kept = {(r["entity_id"], r["file_path"]) for r in await conn.fetch(
            "SELECT entity_id, file_path FROM entity_context WHERE entity_id = ANY($1::int[])", [a, b])}
        theirs = ("skills/my_own.md", "notes/packs.md",
                  "factory-assets/design_systems/runwayml/notes.md", "factory-assets/palettes/mine.md")
        want = {(e, p) for e in (a, b) for p in theirs} | {(b, "skills/deep_research.md")}
        check("the person's skill files, their file at a pack path, a note quoting an Origin line, and "
              "the files they wrote under factory-assets/ themselves stay",
              kept == want, f"kept {sorted(kept)}")
        check("...and the file at the pack path is still theirs",
              await conn.fetchval("SELECT content FROM entity_context WHERE entity_id=$1 "
                                  "AND file_path='skills/deep_research.md'", b) == "My own research steps.")
        check("both catalog tables, the project column and its index are gone",
              await gone(conn) == NONE_LEFT, str(await gone(conn)))
        check("the project that named a design system is still there",
              await conn.fetchval("SELECT name FROM projects WHERE id=$1", ids["project"]) == "site")
        return ids
    finally:
        await conn.close()


async def still_gone(dsn: str, label: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        check(label, await gone(conn) == NONE_LEFT, str(await gone(conn)))
    finally:
        await conn.close()


def main() -> int:
    print("[v206] the old catalog and the coding packs leave the kernel's schema; nothing else does")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
              "this gate proves nothing without one.")
        return 2
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv:
        with kernel_schema_db(srv) as dsn:
            asyncio.run(still_gone(dsn, "a fresh Host's schema (through v206) has no catalog tables, "
                                        "no design-system column and no index"))
            asyncio.run(transition(dsn))
    if failures:
        print(f"\n[v206] FAIL ({len(failures)} failing)")
        return 1
    print("\n[v206] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
