#!/usr/bin/env python3
"""test_memory_rows_dated.py — every memory row the prompt renders carries its
DATE, and a row in the host's own words (turn / summary) says so and is
capped to the newest few in "Relevant Right Now".

Found once: five coding_bench snapshots from different
dates, undated, disagreeing with each other, all rendered as if current.
A config snapshot is a READ with a date, not a fact.

Container-free: the line formatter + static checks on the three render
sites. APP_ROOT=<repo> python3 scripts/<this>.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from mastercode.memory_engine import (  # noqa: E402
    format_memory_line, OWN_WORDS_TYPES, PROMPT_MEMORY_TYPES,
)

dt = datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
check("datetime → [YYYY-MM-DD] prefix", format_memory_line("the fact", dt, "fact") == "\n- [2026-08-04] the fact")
check("ISO string (retrieve()'s `age`) → same prefix",
      format_memory_line("the fact", "2026-08-04T12:30:00+00:00", "fact") == "\n- [2026-08-04] the fact")
check("turn row is marked as the host's own words",
      format_memory_line("I fired coding_bench…", dt, "turn") == "\n- [2026-08-04, your own words then] I fired coding_bench…")
check("summary rows are own-words too", "summary" in OWN_WORDS_TYPES and "turn" in OWN_WORDS_TYPES)
check("no date → says undated, never silent", format_memory_line("x", None, "fact").startswith("\n- [undated] "))
check("garbage date → undated", format_memory_line("x", "yesterday", "fact").startswith("\n- [undated] "))
check("skill_memory is not own-words", "your own words" not in format_memory_line("rule", dt, "skill_memory"))
check("own words never ride the prompt unasked", not (set(OWN_WORDS_TYPES) & PROMPT_MEMORY_TYPES))
check("facts never ride the prompt unasked", "fact" not in PROMPT_MEMORY_TYPES)

src = (ROOT / "brain/mastercode/memory_engine.py").read_text()
check("Relevant Right Now renders through format_memory_line",
      'format_memory_line(content, h.get("created_at"), h.get("memory_type"))' in src)
check("Things You Were Taught renders through format_memory_line",
      'format_memory_line(item["content"], item.get("age"), "skill_memory")' in src)
check("standing sections render through format_memory_line",
      'format_memory_line(item["content"], item.get("age"), item.get("type"))' in src)
check("boot-path teachings carry created_at as age", '"source": "skill_memory", "age": r["created_at"]' in src)
# The documents section (user_context, the last bare line) left the memory
# pre-fire: documents are recall's. So no bare line is left at
# all; the one `\n- ` line builder is format_memory_line's own return.
_bare = [i for i in range(len(src)) if src.startswith('f"\\n- {', i)]
_fml = src.index("def format_memory_line(")
_fml_end = src.index("\ndef ", _fml + 1)
check("no bare memory line is left: the only line builder is format_memory_line's own",
      len(_bare) == 1 and _fml < _bare[0] < _fml_end, f"{len(_bare)} builders")
check("the documents section is out of the memory pre-fire (left to recall)",
      'line = f"\\n- {content}"' not in src and "### From Your Research & Documents" not in src)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
