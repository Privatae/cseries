#!/usr/bin/env python3
"""
`max_minutes` on a coding run is no longer a time cap: it is how long ONE
model call may go with nothing arriving before the run is stopped as hung.
This pins the places that used to call it a cap, a budget or a deadline so the
old wording cannot return. Each pattern below matched the pre-change text.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file, regex that must NOT match)
BANNED = [
    ("server/routes/ladder.py", r"WALL-CLOCK SAFETY CAP"),
    ("server/routes/ladder.py", r"fan_out enforces its own cap"),
    ("server/routes/ladder.py", r"platform wall-clock cap"),
    ("workbench-web/src/lib/run-live.ts", r"m cap`"),
    ("workbench-web/src/lib/run-live.ts", r"wall-clock cap"),
    ("workbench-web/src/lib/types.ts", r"wall-clock cap"),
    ("workbench-web/test/run-live.test.ts", r"\"8m cap\""),
    ("default-skills/coding-protocols.md", r"max_minutes=<time budget>"),
    ("default-skills/coding-protocols.md", r"wall-clock cap\s+applies regardless"),
    ("server/routes/review.py", r"TIME BUDGET\. `max_minutes`"),
    ("server/routes/review.py", r"total timeout is max_minutes\*60\+30"),
    ("server/routes/review.py", r"the\s+cap the user asked for is honoured"),
    ("cli/cseries_sidecar.py", r"max_minutes deadline is the only clock"),
    ("server/routes/coding_protocols.py", r"platform cap applies regardless"),
]


def main() -> int:
    bad = []
    for rel, pat in BANNED:
        text = (ROOT / rel).read_text(encoding="utf-8")
        if re.search(pat, text):
            bad.append(f"{rel}: still matches {pat!r}")
    for b in bad:
        print("FAIL", b)
    if not bad:
        print(f"OK: {len(BANNED)} stale max_minutes wordings are gone")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
