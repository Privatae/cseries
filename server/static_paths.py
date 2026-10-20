"""Containment for files served from a directory the Host can write to.

starlette's StaticFiles checks that a looked-up file stays inside its own
directory, but a subclass that joins a second directory itself gets no such
check. The request path reaches get_response percent-decoded and normalised
with os.path.normpath, which keeps a leading '..': '/%2e%2e/secret.txt'
arrives as '../secret.txt'. inside() resolves the joined path (symlinks
included) and returns it only when it is still under base.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def inside(base: Path, rel: str) -> Optional[Path]:
    """base/rel resolved, or None when it would leave base."""
    root = Path(base).resolve()
    try:
        candidate = (root / rel).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate == root or root in candidate.parents:
        return candidate
    return None
