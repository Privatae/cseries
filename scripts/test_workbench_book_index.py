#!/usr/bin/env python3
"""The Workbench book's Index, as a gate of its own: the "index" group of
scripts/test_workbench_book.py (typing, the three Enter modes, chapters and
their riffle, a new page, secret shapes in what is typed, the switch).

The book's scenarios run as three gates so that no one process outgrows the
gate pool's 180 s CPU budget; see the docstring of test_workbench_book.py,
which holds the scenarios, the mock kernel and every check.

  python3 scripts/test_workbench_book_index.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_workbench_book  # noqa: E402

if __name__ == "__main__":
    sys.exit(test_workbench_book.main("index"))
