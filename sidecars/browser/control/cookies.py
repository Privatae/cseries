"""Where sidecar state lives on the entity PVC — and the scrub of what
used to live there.

Until 0.4.0 this module persisted one vendor's login: the shared page's
Playwright storage_state (cookies + localStorage) written to
/var/lib/cseries/connections/anthropic/cookies.json by the retired
/auth/anthropic/persist route and re-loaded into the shared page on every
boot. The browser carries no vendor's product login. The writer, the
reader and the revoke route are gone; what a pre-0.4.0 pod
left on the PVC is removed at boot by `scrub_legacy()` — the Disconnect
button that used to unlink it went with the front panel, so boot is the
only path that can.

A login the user wants kept lives in the sessions lane
(control/sessions.py), which holds nothing on disk.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("cseries-browser.cookies")

# The entity PVC, mounted at the same path in both containers.
STATE_ROOT = Path("/var/lib/cseries")

# The pre-0.4.0 vendor-login blob. 0600, uid 1000 — readable by anything
# running as the entity user, including the host's local_grep, which is
# jailed to STATE_ROOT and excludes nothing under it.
LEGACY_COOKIES_DIR = STATE_ROOT / "connections" / "anthropic"
LEGACY_COOKIES_PATH = LEGACY_COOKIES_DIR / "cookies.json"


def scrub_legacy() -> bool:
    """Remove the pre-0.4.0 cookie blob if a previous version left one.

    Returns True when something was removed. Never raises: a scrub that
    cannot complete is logged and the browser still boots — the file is a
    leftover to clear, not a dependency.
    """
    removed = False
    for p in (LEGACY_COOKIES_PATH, LEGACY_COOKIES_PATH.with_suffix(".json.tmp")):
        try:
            if p.exists():
                p.unlink()
                removed = True
                log.info("removed pre-0.4.0 vendor login blob %s", p)
        except OSError as exc:
            log.warning("could not remove %s: %s", p, exc)
    try:
        if LEGACY_COOKIES_DIR.is_dir() and not any(LEGACY_COOKIES_DIR.iterdir()):
            LEGACY_COOKIES_DIR.rmdir()
    except OSError:
        pass
    return removed
