#!/usr/bin/env python3
"""The Workbench book's colours and its Open, as a gate of their own: the
"colours" group of scripts/test_workbench_book.py (the book takes the
desktop's tokens on a dark and a light design, on a phone, with a token the
browser cannot draw, and a mermaid fence; "This page" opens the latest build
without a scroll, live and on a phone).

The book's scenarios run as three gates so that no one process outgrows the
gate pool's 180 s CPU budget; see the docstring of test_workbench_book.py,
which holds the scenarios, the mock kernel and every check.

  python3 scripts/test_workbench_book_colours.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_workbench_book  # noqa: E402

if __name__ == "__main__":
    sys.exit(test_workbench_book.main("colours"))
