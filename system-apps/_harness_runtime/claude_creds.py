"""Shared Claude subscription-OAuth credential logic.

ONE implementation of read-local-creds / refresh-local-token / get-local-token
for the call sites that previously each carried a near-identical copy:

  * system-apps/claude-code-harness/bin/main.py (was :107-171) — Lane-A pod
    harness; direct egress, UA "claude-cli/2.1.145 (cseries harness)".
  * system-apps/claude-code/bin/main.py (was :206-302) — in-process sidecar;
    refresh exits through the in-pod egress proxy (NAC_EGRESS_PORT), UA
    "claude-cli/2.1.145 (cseries entity)". Its _get_token keeps its own token
    cache + legacy central-frontend fallback AROUND get_local_token here.

The per-caller differences are parameters (user_agent, proxy_url, now_ms),
never forks of the logic.

brain/mastercode/runtime/host_sdk_engine.py `_get_subscription_token` is
DELIBERATELY NOT refactored onto this module (documented decision): the kernel
image does not ship system-apps/ next to brain/, host_sdk_engine is
stdlib-only + self-contained at import (scripts/test_host_sdk_wall.py loads it
straight from file), and its inline read has a different contract — returns {}
not None, applies NO 60s refresh buffer, and refreshes via the kernel shim
_exec_system_refresh_sdk_auth (never a second refresh path). Coupling the
host engine's import to app-image layout for those five lines is the risk the
refactor was told to avoid.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

_module_log = logging.getLogger("claude_creds")

# Anthropic's public OAuth client id for the Claude CLI/SDK flow — identical
# in both pre-refactor copies (claude-code-harness/bin/main.py:80,
# claude-code/bin/main.py:183).
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def read_local_creds(creds_path: Path) -> dict | None:
    """Read claudeAiOauth from the on-disk creds file, or None.
    (was claude-code-harness/bin/main.py:107-110 == claude-code/bin/main.py:206-211)"""
    try:
        return json.loads(Path(creds_path).read_text()).get("claudeAiOauth") or None
    except Exception:
        return None


def refresh_local_token(oauth: dict, *, creds_path: Path, user_agent: str,
                        proxy_url: str | None = None,
                        log: logging.Logger = _module_log) -> dict | None:
    """Refresh an expired token via the on-disk refresh_token, rewriting the
    creds file atomically. Uses the claude-cli User-Agent — the SDK's own
    refresh 1010s from inside a pod without it.
    (was claude-code-harness/bin/main.py:114-153 == claude-code/bin/main.py:214-260;
    the only deltas were the UA string and claude-code's egress-proxy opener,
    both parameters here.)"""
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return None
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        "https://platform.claude.com/v1/oauth/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", user_agent)
    req.add_header("Accept", "application/json")
    try:
        if proxy_url:
            # claude-code sidecar lane: no direct internet from the pod —
            # refresh exits through the user's bastion via the in-pod egress
            # proxy, same lane as the connect/token exchange.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
            with opener.open(req, timeout=15) as r:
                new = json.loads(r.read())
        else:
            with urllib.request.urlopen(req, timeout=15) as r:
                new = json.loads(r.read())
    except Exception as e:
        log.warning("claude token refresh failed: %s", e)
        return None
    now_ms = int(time.time() * 1000)
    refreshed = {
        "accessToken": new.get("access_token"),
        "refreshToken": new.get("refresh_token") or refresh_token,
        "expiresAt": now_ms + int(new.get("expires_in") or 0) * 1000,
        "scopes": (new.get("scope") or "user:inference").split(),
        "subscriptionType": oauth.get("subscriptionType") or "claude_pro",
        "rateLimitTier": oauth.get("rateLimitTier"),
    }
    try:
        creds_path = Path(creds_path)
        # with_name, not with_suffix: on `.credentials.json` with_suffix swaps
        # the `.json` for the whole argument and produced
        # `.credentials.credentials.json.tmp` — a name no by-name export filter
        # recognised, in the one directory claude-code's session export tars.
        # A rename that failed here left both tokens exportable (review,
        # 2026-09-22). `.credentials.json.tmp` is what the name says it is.
        tmp = creds_path.with_name(creds_path.name + ".tmp")
        tmp.write_text(json.dumps({"claudeAiOauth": refreshed}, indent=2))
        tmp.chmod(0o600)
        tmp.replace(creds_path)
    except Exception as e:
        log.warning("could not persist refreshed claude token: %s", e)
    return refreshed


def get_local_token(*, creds_path: Path, user_agent: str,
                    proxy_url: str | None = None,
                    log: logging.Logger = _module_log,
                    now_ms: float | None = None) -> tuple[str, int | None] | None:
    """Return (access_token, expires_at_ms) from the on-disk creds,
    self-refreshing when expired or within 60s of expiry; None when no valid
    token can be produced. `now_ms` lets a caller pin the clock it already
    sampled (claude-code passes its cache-check timestamp).
    (was the local-creds path of claude-code-harness/bin/main.py:156-171 ==
    claude-code/bin/main.py:280-291)"""
    oauth = read_local_creds(creds_path)
    if not (oauth and oauth.get("accessToken")):
        return None
    if now_ms is None:
        now_ms = time.time() * 1000
    exp = oauth.get("expiresAt") or 0
    if exp and exp <= now_ms + 60_000:  # expired or within 60s
        refreshed = refresh_local_token(oauth, creds_path=creds_path,
                                        user_agent=user_agent,
                                        proxy_url=proxy_url, log=log)
        if refreshed and refreshed.get("accessToken"):
            oauth = refreshed
    if oauth.get("accessToken") and (not oauth.get("expiresAt")
                                     or oauth["expiresAt"] > now_ms):
        return oauth["accessToken"], oauth.get("expiresAt")
    return None
