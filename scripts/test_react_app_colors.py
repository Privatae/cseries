#!/usr/bin/env python3
"""React-app color ratchet. The React apps (circles-web, workbench-web) must
use the shared cseries green-black design tokens, NOT the retired blue-tinted
cseries surface palette (2026-07-04: the blue is gone for good,
tokens.css:43; the apps drifted and shipped blue on 2026-07-07). Bans the old
blue surface hexes in the built + source CSS; they must be var(--cseries-*)."""
import re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
BLUE = ["#0b0f14", "#0e141b", "#141a22", "#1b2430", "#1f2937", "#243140"]
def main() -> int:
    bad = []
    for css in list((ROOT / "circles-web/src").glob("index.css")) + \
               list((ROOT / "workbench-web/src").glob("index.css")):
        text = css.read_text()
        for hexv in BLUE:
            if re.search(re.escape(hexv), text, re.I):
                bad.append(f"{css.relative_to(ROOT)}: old blue-cseries {hexv} "
                           f"— use var(--cseries-*) green-black tokens")
    if bad:
        print("FAIL: React apps carry retired blue surfaces:")
        for b in bad: print("  " + b)
        return 1
    print("OK — React app CSS uses cseries green-black tokens (no blue-cseries surfaces)")
    return 0
if __name__ == "__main__":
    sys.exit(main())
