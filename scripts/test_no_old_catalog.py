#!/usr/bin/env python3
# gate-scope: whole-tree — the whole tree, for the retired catalog
"""Ratchet: the old asset catalog and the three coding-agent packs stay out.

The design system was replaced, for the Host's own desktop UI and
by a platform library users submit and share, so the old open-design catalog
goes. The coding packs (Superpowers, Karpathy, Everything Claude Code) are
skills the Host never needs or uses. Both came
out in one change:

  - the catalog router (/api/catalog/*, server/routes/catalog.py), the
    `manage_catalog_entry` tool, the Host MCP server's `design_system` build
    argument, projects.design_system_slug, the catalog tables, every
    factory-assets/ file an install wrote, the artifact_skill kind and the
    bridge op catalog.fetch_asset (on the platform side);
  - the packs' hard rules (origins external:superpowers, external:karpathy,
    external:ecc) and their skill files.

migrations/v206_remove_old_catalog_and_coding_packs.sql removes the data. This
keeps the code from growing back: no file in the tree names any of it, outside
the places that must.

Allowed to name it, each for a reason:
  migrations/                 shipped migrations are history and never edited
                              (migrations/README.md); v206 itself is here
  docs/archive/               retired designs, kept as the record
  scripts/declared_tables_baseline.txt, scripts/orphan_tables_baseline.txt
                              table-hygiene lists generated from every CREATE
                              TABLE in the tree, and v009 still declares the
                              catalog tables (scripts/audit_orphan_tables.py)
  scripts/test_remove_old_catalog_pg.py
                              runs v206 against a real database and has to
                              name what it checks is gone
  this file

Also checked: the deleted files stay deleted.

Pure: reads files, imports nothing from the tree, runs nothing. Container-free.
    APP_ROOT=<repo> python3 scripts/test_no_old_catalog.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)

BANNED = (
    "catalog_entries",
    "catalog_installs",
    "manage_catalog_entry",
    "design_system_slug",
    "factory-assets/",
    "catalog.fetch_asset",
    "/api/catalog",
    "artifact_skill",
    "external:superpowers",
    "external:karpathy",
    "external:ecc",
)

ALLOWED_DIRS = ("migrations/", "docs/archive/", "docs/audits/")
ALLOWED_FILES = {
    "scripts/declared_tables_baseline.txt",
    "scripts/orphan_tables_baseline.txt",
    "scripts/test_remove_old_catalog_pg.py",
    "scripts/test_no_old_catalog.py",
}

DELETED = (
    "server/routes/catalog.py",
    "scripts/regen_catalog_design_systems.py",
    "scripts/regen_catalog_artifact_skills.py",
)

# Directories that are never source: VCS state, dependency trees, caches.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache"}

FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  ok    " if ok else "  FAIL  ") + label + (f"\n          {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(label)


def allowed(rel: str) -> bool:
    return rel in ALLOWED_FILES or any(rel.startswith(d) for d in ALLOWED_DIRS)


def scan(root: Path) -> list[tuple[str, int, str]]:
    """(path, line, term) for every banned term in a text file under root,
    outside the allowed places. A file that is not UTF-8 text (an image, a
    signed bundle) is skipped: none of these names can be written into one by
    hand, and a bundle is repacked from system-apps/, which is scanned."""
    hits = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            path = Path(dirpath) / f
            rel = path.relative_to(root).as_posix()
            if allowed(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not any(t in text for t in BANNED):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for t in BANNED:
                    if t in line:
                        hits.append((rel, n, t))
    return hits


def self_test() -> None:
    """The scan finds a planted name and honours exactly the allowed places."""
    with tempfile.TemporaryDirectory(prefix="no-old-catalog-") as d:
        r = Path(d)
        (r / "server").mkdir()
        (r / "migrations").mkdir()
        (r / "docs" / "archive").mkdir(parents=True)
        (r / "node_modules").mkdir()
        (r / "server" / "x.py").write_text("ok = 1\nq = 'SELECT 1 FROM catalog_entries'\n")
        (r / "server" / "y.yaml").write_text("# PUT /api/catalog/default-design\n")
        (r / "migrations" / "v1.sql").write_text("-- history names catalog_entries\n")
        (r / "docs" / "archive" / "old.md").write_text("manage_catalog_entry\n")
        (r / "docs" / "live.md").write_text("origin external:karpathy\n")
        (r / "node_modules" / "z.js").write_text("design_system_slug\n")
        (r / "bin.dat").write_bytes(b"\xff\xfecatalog_entries")
        got = sorted(scan(r))
    want = [("docs/live.md", 1, "external:karpathy"),
            ("server/x.py", 2, "catalog_entries"),
            ("server/y.yaml", 1, "/api/catalog")]
    check("self-test: a planted name is found by file and line; migrations/, docs/archive/, "
          "dependency trees and non-text files are passed over", got == want, f"got {got}")


def main() -> int:
    print("[no-old-catalog] the asset catalog and the coding-agent packs stay out of the tree")
    self_test()
    for rel in DELETED:
        check(f"{rel} stays deleted", not (ROOT / rel).exists())
    hits = scan(ROOT)
    shown = "\n          ".join(f"{p}:{n}  {t}" for p, n, t in hits[:40])
    more = f"\n          … and {len(hits) - 40} more" if len(hits) > 40 else ""
    check(f"no file outside the allowed places names any of {len(BANNED)} removed things",
          not hits, f"{len(hits)} hit(s):\n          {shown}{more}")
    if FAILED:
        print(f"\n[no-old-catalog] FAIL ({len(FAILED)} failing)")
        return 1
    print("\n[no-old-catalog] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
