"""Conservative masking for database connection URLs.

Stdlib-only leaf module so the invariant is unit-testable in isolation
(scripts/test_sites_db_mask.py): the masked form NEVER contains the password,
and an unparseable URL masks to None rather than risking a leak.
"""
from __future__ import annotations

import re
from typing import Optional

# scheme://user:password@rest — password = everything between the first ":"
# after the userinfo start and the LAST "@" before the host (passwords may
# themselves contain "@" or ":", so be greedy up to the final "@").
_USERINFO = re.compile(r"^([a-z][a-z0-9+.-]*://[^:/?#@]+):([^/?#]*)@", re.IGNORECASE)

MASK = "••••"


def mask_db_url(url: str) -> Optional[str]:
    """Replace the password in a connection URL with a mask.

    Returns None when the URL does not match the expected
    scheme://user:password@host shape — callers must treat None as
    "display nothing", never fall back to the raw URL.
    """
    if not isinstance(url, str) or not url:
        return None
    m = _USERINFO.match(url)
    if not m:
        return None
    password = m.group(2)
    masked = f"{m.group(1)}:{MASK}@{url[m.end():]}"
    # Belt and braces: a non-trivial password must not survive anywhere in the
    # masked output (e.g. repeated in a query param).
    if password and len(password) >= 4 and password in masked:
        return None
    return masked
