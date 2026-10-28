#!/usr/bin/env python3
# gate-scope: whole-tree — every default-skills/connectors*.md
"""Gate: every connectors skill rides a turn whole, with room to spare.

A skill body rides the turn only up to prefire_tools.SKILL_INJECT_CHARS
(brain/mastercode/runtime/prefire_tools.py; render_skill_context cuts it at
that many characters and points at `context(action='read')` for the rest).
default-skills/connectors.md had grown to 18.5K characters, so "What the
person sees" and everything after it fell past the cut. Since 2026-10-05 each
platform's setup is its own skill (connectors-telegram, connectors-slack,
connectors-discord), loaded on demand with `context(action='read',
path='default-skills/<name>')` (runtime/context_ops.py: _SKILL_PREFIXES and
_skill_file read a default skill from the skills folder), and connectors.md
keeps the shared flow and names the one to load.

This gate checks, on the files as the loader reads them
(skill_loader._parse_frontmatter, as session.py's prefire does):
  1. every default-skills/connectors*.md body is under the cut with a margin
     (MARGIN of it), so a later edit has room before it is cut;
  2. connectors.md names each platform skill and how to load it, and each
     platform skill exists, with a name: matching its file and a description;
  3. the load path it names resolves: context_ops._skill_file finds each one
     in the skills folder skill_loader.get_location names.
No database, no network.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

from mastercode.runtime.prefire_tools import SKILL_INJECT_CHARS  # noqa: E402
from mastercode.runtime.skill_loader import _parse_frontmatter  # noqa: E402

MARGIN = 0.10           # a tenth of the cut stays free
PLATFORM_SKILLS = ("connectors-telegram", "connectors-slack", "connectors-discord")
FAILS: list[str] = []


def check(label: str, ok: bool, detail="") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    limit = int(SKILL_INJECT_CHARS * (1 - MARGIN))
    files = sorted((ROOT / "default-skills").glob("connectors*.md"))
    check("the connectors skills are connectors.md and one per platform",
          sorted(p.stem for p in files) == sorted(("connectors",) + PLATFORM_SKILLS),
          [p.stem for p in files])
    for p in files:
        meta, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        n = len(body.strip())
        check(f"{p.name}: its body ({n:,} characters) rides a turn whole, under "
              f"{limit:,} ({int(MARGIN * 100)}% under the {SKILL_INJECT_CHARS:,} cut)", n <= limit, n)
        check(f"{p.name}: name: is its file's, and it has a description",
              meta.get("name") == p.stem and len(meta.get("description") or "") > 40, meta.get("name"))
    shared = (ROOT / "default-skills" / "connectors.md").read_text(encoding="utf-8")
    check("connectors.md says how to load a platform's skill",
          re.search(r"context\(action='read',\s*path='default-skills/connectors-<platform>'\)",
                    shared) is not None)
    for name in PLATFORM_SKILLS:
        check(f"connectors.md names {name}", f"`{name}`" in shared)
    from mastercode.runtime import context_ops, skill_loader
    saved = skill_loader.get_location
    skill_loader.get_location = lambda: ROOT / "default-skills"
    try:
        for name in PLATFORM_SKILLS:
            got = context_ops._skill_file(f"default-skills/{name}")
            check(f"context(action='read', path='default-skills/{name}') resolves to the skill file",
                  got is not None and got.name == f"{name}.md", got)
    finally:
        skill_loader.get_location = saved
    print(f"\n{'FAILED' if FAILS else 'PASSED'}: connectors skills fit ({len(FAILS)} failure(s))")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
