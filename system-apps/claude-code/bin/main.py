#!/usr/bin/env python3
"""Claude Code sidecar — the connection panel for the Claude Code harness.

It reports whether the user's own Claude credential is present and
fresh, and lets them disconnect it. It runs NO agent. Claude Code
builds run in the claude-code harness, in its own container on the
harness pool (system-apps/claude-code-harness), reached through the
Host's build tool.

The platform hosts no login surface for it. It reads the user's own
credential from the entity's on-disk creds file — the same file every
other harness reads, delivered by the standard wake path — refreshes it
in-pod when it expires, and reports it. Until 2026-08-15 this file also
spoke to a platform-hosted Anthropic OAuth flow on the platform frontend;
that surface and every caller of it were deleted, because a vendor's
login belongs to the vendor.

RETIRED 2026-09-27 — POST /sessions and POST /sessions/continue. They
started a Claude Agent SDK session INSIDE the Host's container, with
file-write, file-edit and shell tools (server/ceigas/ceigas_mcp.py)
running as subprocesses beside the kernel. Nothing in the product called
them: the app UI only listed and closed sessions, the tickets route only
closed one (for a ticket key nothing ever wrote), and the Host's
kernel_op tool cannot reach /system-apps/ paths. Both now answer 410
Gone. Everything that existed only for them is removed with them: the
MCP wiring, the transcript writer, and the in-memory session store with
its list, close, reaper and /gc-orphans routes (that store's only writer
was POST /sessions, so after the upgrade restart it can never hold a
session), plus /token-pull, which warmed the token for the first agent
run and had no caller. /factory-run, which ran builds in this container
the same way, was removed on 2026-07-11 for the same reason.

THE SDK CACHE IS GONE TOO (2026-09-28). This sidecar used to pip-install
the Claude Agent SDK (~230 MB) into its own install dir at every start for
one remaining reader, the Designs app's import-from-URL job. That job is
retired — website-to-desktop is the Host's artifact op='desktop_from_url',
which reads a site's palette with no model (server/applications/
site_style.py) — so nothing reads the cache, and the start now deletes a
cache an earlier version left behind. The coding harness is untouched: it
bakes its own SDK into its image (sidecars/harness-claude-code/Dockerfile).
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Where earlier versions pip-installed the Claude Agent SDK. Nothing reads it
# any more (see the module docstring); _drop_stale_sdk_cache removes it once.
_STALE_SDK_CACHE_DIR = "/var/lib/cseries/apps/claude-code/python-libs"


def _drop_stale_sdk_cache() -> None:
    """Delete the SDK cache an earlier version of this app provisioned —
    ~230 MB per Host with no reader. The creator of the cache owns cleaning
    it up; a failure is logged and never blocks the sidecar."""
    import shutil
    path = Path(_STALE_SDK_CACHE_DIR)
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        logging.getLogger("claude-code-sidecar").info("removed the retired SDK cache %s", path)
    except OSError as e:
        logging.getLogger("claude-code-sidecar").warning("could not remove %s: %s", path, e)


sys.path.insert(0, "/app/cli")
# Same guard as claude-code-harness/bin/main.py: parents[2] only exists in the
# source-tree layout (system-apps/<app>/bin/main.py). Any image layout that
# flattens this file to a shallower path would raise IndexError at import.
_parents = Path(__file__).resolve().parents
_sysapps = _parents[2] if len(_parents) > 2 else None
if _sysapps is not None and (_sysapps / "_harness_runtime").is_dir():  # source tree only
    sys.path.insert(0, str(_sysapps))
from cseries_sidecar import sidecar, APP_ID  # noqa: E402
# ONE shared implementation of the Claude OAuth read/refresh/get-token logic
# (also used by the claude-code-harness pod). In the entity image it resolves
# from /app/cli/_harness_runtime/ — the same path cseries_sidecar rides on.
from _harness_runtime import claude_creds  # noqa: E402

log = logging.getLogger("claude-code-sidecar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ── Config / paths ────────────────────────────────────────────────────

# REMOVED 2026-08-15 — the platform-hosted Anthropic OAuth surface.
# FRONTEND_BASE / INTERNAL_TOKEN existed only to reach the platform frontend's
# /api/integrations/claude, which built Anthropic authorize URLs with the
# Claude Code CLI's own client_id, held the PKCE verifier server-side,
# exchanged the code at Anthropic's token endpoint, and stored the grant.
# That is the platform standing in the middle of a vendor's login. Every
# login surface must be the vendor's own, with the host acting only as the
# operator that runs it — the same rule for every vendor, no exceptions and
# no per-vendor shortcuts. The frontend route is deleted in this same
# change; nothing here talks to it any more.
#
# What remains is the path every other harness already uses: the user's own
# credential in the vault -> the entity's on-disk creds file ->
# LOCAL_CREDS_PATH below, self-refreshed in-pod. _get_token already
# preferred that and called the frontend "a legacy fallback only", so this
# removes dead-by-default code rather than a working path.

ENTITY_ID = int(os.environ.get("ENTITY_ID", "0"))

# The plain reason both retired routes return. A caller that still posts here
# learns where the work goes instead of getting a bare 404.
SESSIONS_GONE = (
    "Claude Code sessions no longer start inside the Host. Claude Code "
    "builds run in their own container on the harness pool; the Host starts "
    "one with its build tool (build, harness='claude-code')."
)


# ── Token cache (memory only) ─────────────────────────────────────────

_token_lock = threading.Lock()
_cached_token = {"value": None, "expires_at": None, "fetched_at": 0.0,
                 "connected_at": None}


def _mark_connected_locked() -> None:
    """Stamp a real connect time the first time a token becomes available.
    Caller must hold _token_lock. Distinct from expires_at — that's the
    token's deadline, not when the subscription was wired up."""
    if not _cached_token.get("connected_at"):
        _cached_token["connected_at"] = _now_iso()


# The entity's OWN Claude creds — the same file every other harness reads
# via _HARNESS_CRED_FILES, delivered by the standard wake path from the
# user's own credential. This is now the ONLY source; the central-frontend
# fetch that used to sit here was the platform-hosted OAuth store and is
# gone (see the config note above).
LOCAL_CREDS_PATH = Path("/home/privatae/.claude/.credentials.json")
_REFRESH_UA = "claude-cli/2.1.145 (cseries entity)"


def _get_token() -> str | None:
    """Return a valid Claude access token.

    The ONLY source is the entity's own on-disk creds — sovereign, and the
    same file every other harness reads. Self-refreshes an expired token via
    the on-disk refresh_token (shared impl: _harness_runtime/claude_creds;
    refresh exits through the user's bastion via the in-pod egress proxy).

    No creds -> None, and the caller reports not-connected. There is
    deliberately no second source: the central-frontend fallback that used
    to live here was the platform's own Anthropic OAuth store.
    """
    with _token_lock:
        now = time.time()
        if (_cached_token["value"]
                and (now - _cached_token["fetched_at"]) < 300
                and (not _cached_token["expires_at"]
                     or _cached_token["expires_at"] > now * 1000 + 60_000)):
            return _cached_token["value"]

        proxy = "http://127.0.0.1:" + (os.environ.get("NAC_EGRESS_PORT") or "13128")
        got = claude_creds.get_local_token(
            creds_path=LOCAL_CREDS_PATH, user_agent=_REFRESH_UA,
            proxy_url=proxy, log=log, now_ms=now * 1000)
        if got:
            token, expires_at = got
            _cached_token.update(value=token, expires_at=expires_at, fetched_at=now)
            _mark_connected_locked()
            return token

        _cached_token["value"] = None
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── HTTP handlers ─────────────────────────────────────────────────────
# The full route table is pinned by scripts/test_claude_code_sessions_retired.py:
# a new handler here fails that gate until it is added there on purpose.

@sidecar.handler("GET", "/health")
def health(request=None):
    return {"ok": True, "app_id": APP_ID}


@sidecar.handler("GET", "/status")
def status(request=None):
    """The connection panel's poll (app.js refresh(), every 8 s)."""
    token = _get_token()
    return {
        "connected": bool(token),
        "expires_at": _cached_token.get("expires_at"),
        "connected_at": _cached_token.get("connected_at"),
    }


@sidecar.handler("POST", "/disconnect")
def disconnect(request=None):
    """Remove the on-disk creds and clear the sidecar cache.

    The second half of the app's Disconnect. The first is the Host's own
    disconnect, which app.js calls before this (DELETE /api/vault/secrets/
    harness:claude_oauth): it sets the marker that keeps the Host from
    writing the file back from its Vault copy, and revokes that copy. This
    file alone is not a disconnect; the Host's reconcile restored it within
    300 s. Clearing the cache alone would let the next _get_token re-read
    disk and silently reconnect.
    """
    try:
        LOCAL_CREDS_PATH.unlink(missing_ok=True)
    except Exception as e:
        log.warning("could not remove local creds on disconnect: %s", e)
    with _token_lock:
        _cached_token["value"] = None
        _cached_token["fetched_at"] = 0
        _cached_token["connected_at"] = None
    return {"ok": True}


@sidecar.handler("POST", "/sessions")
def create_session(request=None):
    """GONE (410). Started a Claude Agent SDK session inside the Host's
    container; see the module docstring."""
    return {"error": "gone", "reason": SESSIONS_GONE}, 410


@sidecar.handler("POST", "/sessions/continue")
def continue_session(request=None):
    """GONE (410). Resumed a session POST /sessions started, inside the
    Host's container; see the module docstring."""
    return {"error": "gone", "reason": SESSIONS_GONE}, 410


# ── Boot ──────────────────────────────────────────────────────────────
# Behind __main__ (the manifest runs `python3 bin/main.py`), so a test can
# import this module and drive its handlers without binding a port.

if __name__ == "__main__":
    _drop_stale_sdk_cache()
    log.info("claude-code sidecar entity=%d", ENTITY_ID)
    sidecar.serve()
