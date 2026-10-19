#!/usr/bin/env python3
"""Gate: the `context` tool reads on-disk skills the table does not hold.

Two skill stores exist — entity_context `skills/*` rows and the Skills
app's directory (server/routes/skills.py:_get_location), where every
/app/default-skills/*.md is seeded. 2026-09-03: a Host burned twelve
tool rounds on "File not found: skills/desktop.md" with the file on disk,
then built a desktop without the skill. context_ops.py now falls back to
the directory for `skills/<name>` and `default-skills/<name>`.

Container-free: a temp directory stands in for the Skills location and a
stub pool answers the table queries. Pins:
  - a disk-only skill reads (fuzzy .md, both prefixes), tagged skills_dir
  - a table row wins over a same-named disk file
  - traversal, dotfiles, subpaths and the .pre-refresh/.retired backups
    are refused; a non-skill prefix never touches the disk
  - list unions disk skills under the prefix asked for, no duplicates,
    and a non-skill prefix lists no disk entries
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "brain"))
sys.path.insert(0, str(HERE))

from mastercode.runtime import context_ops  # noqa: E402


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class _StubPool:
    """Answers context_ops' table queries from a dict of path -> content."""

    def __init__(self, rows: dict[str, str]):
        self.rows = rows

    async def fetchrow(self, sql, entity_id, path):
        if path in self.rows:
            return _Row(content_type="text/markdown", updated_at=None)
        return None

    async def fetch(self, sql, entity_id, *args):
        prefix = args[0][:-1] if args else ""
        return [
            _Row(file_path=p, content_type="text/markdown", backend="pg",
                 size=len(c), machine="platform", updated_at=None)
            for p, c in sorted(self.rows.items()) if p.startswith(prefix)
        ]


class _StubStore:
    def __init__(self, rows):
        self.rows = rows

    async def get(self, path):
        return self.rows.get(path)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skills-gate-"))
    (tmp / "desktop.md").write_text("# desktop\nthe floor and the template\n")
    (tmp / "shared.md").write_text("DISK COPY\n")
    (tmp / "system.md.pre-refresh-20260719").write_text("old\n")
    (tmp / "copilot.md.retired-20260823").write_text("gone\n")
    (tmp / ".seeded").mkdir()
    (tmp / ".seeded" / "desktop.md.sha256").write_text("abc\n")
    outside = tmp.parent / f"{tmp.name}-outside.md"
    outside.write_text("must never be readable\n")

    rows = {"skills/shared.md": "TABLE COPY\n", "skills/only_in_table.md": "t\n",
            "notes/x/DESIGN.md": "d\n"}
    pool = _StubPool(rows)
    context_ops._skills_dir = lambda: tmp
    context_ops._store = lambda pool_, eid: _StubStore(rows)

    fails: list[str] = []

    def expect(cond, msg):
        if not cond:
            fails.append(msg)

    async def run():
        r = await context_ops._read(pool, 1, {"path": "skills/desktop.md"})
        expect(r["status"] == "ok" and r.get("backend") == "skills_dir"
               and "the floor" in r["content"], f"disk-only skill read: {r}")
        r = await context_ops._read(pool, 1, {"path": "default-skills/desktop"})
        expect(r["status"] == "ok" and r.get("backend") == "skills_dir",
               f"default-skills/ prefix + fuzzy .md: {r}")
        r = await context_ops._read(pool, 1, {"path": "skills/shared.md"})
        expect(r["status"] == "ok" and r["content"] == "TABLE COPY\n"
               and "backend" not in r, f"table row must win over disk: {r}")
        for bad in (f"skills/../{outside.name}", "skills/.seeded/desktop.md.sha256",
                    "skills/sub/desktop.md", "skills/system.md.pre-refresh-20260719",
                    "skills/copilot.md.retired-20260823", "skills/", "skills/.md",
                    "default-skills/nope.md", "skillsdesktop.md"):
            r = await context_ops._read(pool, 1, {"path": bad})
            expect(r["status"] == "error", f"must refuse {bad!r}: {r}")
        r = await context_ops._read(pool, 1, {"path": "notes/x/DESIGN.md"})
        expect(r["status"] == "ok" and "backend" not in r, f"non-skill path untouched: {r}")

        r = await context_ops._list(pool, 1, {"prefix": "skills/"})
        paths = [f["path"] for f in r["files"]]
        expect(paths == sorted(paths) and len(paths) == len(set(paths)),
               f"list sorted, no duplicates: {paths}")
        expect(set(paths) == {"skills/shared.md", "skills/only_in_table.md", "skills/desktop.md"},
               f"list unions disk-only skills, backups excluded: {paths}")
        by = {f["path"]: f for f in r["files"]}
        expect(by["skills/shared.md"]["backend"] == "pg", "shared.md listed from the table")
        expect(by["skills/desktop.md"]["backend"] == "skills_dir", "desktop.md listed from disk")
        r = await context_ops._list(pool, 1, {"prefix": "default-skills/"})
        expect([f["path"] for f in r["files"]] == ["default-skills/desktop.md", "default-skills/shared.md"],
               f"default-skills/ lists disk files under that prefix: {r['files']}")
        r = await context_ops._list(pool, 1, {"prefix": "default-sk"})
        expect([f["path"] for f in r["files"]] == ["default-skills/desktop.md", "default-skills/shared.md"],
               f"a partial default-skills prefix still lists disk files: {r['files']}")
        r = await context_ops._read(pool, 1, {"path": "skills/desktop.md"})
        expect(r.get("size") == (tmp / "desktop.md").stat().st_size, f"read size is bytes: {r.get('size')}")
        r = await context_ops._list(pool, 1, {"prefix": "notes/"})
        expect(all(f["backend"] != "skills_dir" for f in r["files"]) and r["count"] == 1,
               f"non-skill prefix lists no disk entries: {r}")
        r = await context_ops._list(pool, 1, {})
        expect(r["count"] == 4 and sum(1 for f in r["files"] if f["backend"] == "skills_dir") == 1,
               f"unprefixed list = 3 rows + 1 disk-only: {r['count']}")

    asyncio.run(run())
    for f in fails:
        print("FAIL:", f)
    if fails:
        return 1
    print("[context-skills-disk] all gates pass")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.exit(main())
