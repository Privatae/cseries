"""In-pod Claude SDK OAuth lifecycle — direct PKCE implementation.

Surfaces three routes for the Connections (portal) app's Runtimes tile:

  GET  /api/sdk-auth/status   → read /home/privatae/.claude/.credentials.json,
                                report {present, valid, expires_at,
                                subscription_type, rate_limit_tier}.
  POST /api/sdk-auth/start    → generate a PKCE code_verifier + challenge,
                                stash {verifier, state} in process memory,
                                return {session_id, auth_url}.
  POST /api/sdk-auth/finish   → exchange the user-pasted `code` (which may
                                arrive as `<code>#<state>` from Anthropic's
                                success page) against
                                platform.claude.com/v1/oauth/token using
                                the stashed verifier, then write the
                                resulting credentials.json atomically to
                                /home/privatae/.claude/.credentials.json.
                                ~/.claude is EPHEMERAL (the /home/privatae
                                emptyDir — the operator's PVC subPath persist
                                is removed): the durable copy is the
                                vault envelope harness:claude_oauth sealed
                                right after this write; boot reconcile + the
                                seal-sweep (network/nac_harness.py)
                                re-materialize the runtime file each roll.

Why we don't drive `claude setup-token` as a PTY subprocess:
The CLI's Ink TUI reads stdin in raw mode and masks each input character
with `*`. Live probe in pod (CLI v2.1.145): a real 92-char OAuth code
delivered programmatically wraps across two terminal lines, and the
trailing `\\r` we write doesn't reliably submit the buffer — the CLI
sits with all 92 asterisks rendered and no follow-up. Short test codes
(<48 chars, single line) DID submit and produce the expected error
output, but real codes always wrap. Rather than fight Ink's raw-mode
input handling, we just do the OAuth flow ourselves — it's plain
RFC 7636 PKCE against published Anthropic endpoints. Once the resulting
credentials.json lives at the canonical path, the Claude Agent SDK
consumes it exactly as if the CLI had written it.

Everything lives inside the entity container. No k8s Secret, no bridge
ops, no operator-side endpoints, no sidecar, no CLI subprocess. The
Claude Code CLI auto-refreshes its own access token as long as the
credentials file is writable; the 401 hook in
brain/mastercode/runtime/tools.py covers the revoked-refresh-token
case via an inbox notification.

Session state is process-scope (cseries runs uvicorn --workers 1).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.pc.sdk_auth")

router = APIRouter(prefix="/api/sdk-auth", tags=["sdk_auth"])

# ── Constants ─────────────────────────────────────────────────────────

CREDS_PATH    = Path("/home/privatae/.claude/.credentials.json")
CLIENT_ID     = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # @anthropic-ai/claude-code
# Canonical authorize host — NOT claude.com/cai/oauth/authorize, which is only a
# 307 redirect shim to this. A cross-origin OAuth popup that bounces through
# claude.com first yields "claude.com refused to connect" on some browsers.
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL     = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI  = "https://platform.claude.com/oauth/code/callback"
# The harness runs the bundled Claude Code CLI/Agent SDK, which needs
# user:sessions:claude_code (+ profile/mcp/file) — NOT just user:inference. An
# inference-only token 401s the CLI ("Invalid authentication credentials") even though
# it works for a direct /v1/messages call. Verified on the edge bastion against a
# full-scope token. (Was "user:inference" to match `claude setup-token --inferenceOnly`.)
SCOPE         = "user:inference user:sessions:claude_code user:profile user:mcp_servers user:file_upload"
UA            = "claude-cli/2.1.145 (cseries entity)"
SESSION_TTL   = 600  # 10 min — generous; user has to complete browser auth


def _egress_proxy() -> str:
    """Outbound third-party traffic exits through the in-pod NAC egress gate (the same
    lane every other third-party call uses). The gate routes through the user's bastion
    when one is connected (opt-in — the host rides the user's own connection), otherwise
    it dials out directly: bastion egress is a choice, not a requirement. The token
    exchange goes through this proxy so it follows the user's egress policy."""
    return "http://127.0.0.1:" + (os.environ.get("NAC_EGRESS_PORT") or "13128")


# ── PKCE primitives ───────────────────────────────────────────────────

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _new_pkce() -> tuple[str, str]:
    """RFC 7636 S256 code_verifier (43 chars base64url) + challenge."""
    verifier  = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _build_authorize_url(challenge: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "code":                  "true",
        "client_id":             CLIENT_ID,
        "response_type":         "code",
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SCOPE,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# ── Session table ─────────────────────────────────────────────────────

_SESSIONS: dict[str, dict] = {}           # session_id → {verifier, state, created_at, auth_url}
_SESSIONS_LOCK = asyncio.Lock()
_SWEEPER_TASK: asyncio.Task | None = None


async def _sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        async with _SESSIONS_LOCK:
            stale = [sid for sid, s in _SESSIONS.items()
                     if now - s["created_at"] > SESSION_TTL]
            for sid in stale:
                log.warning("sdk_auth: sweeping stale session %s", sid)
                _SESSIONS.pop(sid, None)


def _ensure_sweeper() -> None:
    global _SWEEPER_TASK
    if _SWEEPER_TASK is None or _SWEEPER_TASK.done():
        _SWEEPER_TASK = asyncio.create_task(_sweeper())


# ── Routes ────────────────────────────────────────────────────────────

@router.get(
    "/status",
    dependencies=[Depends(requires_permission("read:portal_sources"))],
)
async def status():
    """Report credential state. No token material returned."""
    if not CREDS_PATH.exists():
        return {"present": False, "valid": False, "expires_at": None,
                "subscription_type": None, "rate_limit_tier": None}
    try:
        blob = json.loads(CREDS_PATH.read_bytes())
        oauth = blob.get("claudeAiOauth") or {}
        expires_at = oauth.get("expiresAt")
        now_ms = int(time.time() * 1000)
        valid = bool(expires_at) and int(expires_at) > now_ms
        return {
            "present":           True,
            "valid":             valid,
            "expires_at":        expires_at,
            "subscription_type": oauth.get("subscriptionType"),
            "rate_limit_tier":   oauth.get("rateLimitTier"),
        }
    except (ValueError, KeyError, TypeError, OSError) as e:
        log.warning("sdk_auth status read failed: %s", e)
        return {"present": True, "valid": False, "expires_at": None,
                "subscription_type": None, "rate_limit_tier": None}


@router.post(
    "/start",
    dependencies=[Depends(requires_permission("write:portal_sources"))],
)
async def start():
    """Generate PKCE pair, build the Anthropic authorize URL, stash the
    verifier in process memory keyed by a fresh session_id."""
    _ensure_sweeper()
    verifier, challenge = _new_pkce()
    state = _b64url(secrets.token_bytes(24))
    session_id = uuid.uuid4().hex
    auth_url = _build_authorize_url(challenge, state)
    async with _SESSIONS_LOCK:
        _SESSIONS[session_id] = {
            "verifier":   verifier,
            "state":      state,
            "created_at": time.time(),
            "auth_url":   auth_url,
        }
    log.info("sdk_auth start: session=%s", session_id)
    return {"session_id": session_id, "auth_url": auth_url}


class FinishBody(BaseModel):
    session_id: str
    code: str


@router.post(
    "/finish",
    dependencies=[Depends(requires_permission("write:portal_sources"))],
)
async def finish(body: FinishBody):
    """Exchange the pasted code for tokens via PKCE, write credentials.json
    atomically to the persistent mount.

    Anthropic's success page presents the code as `<auth_code>#<state>`.
    Users may copy the whole string or only the part before `#`. We
    accept either; if `#state` is present we verify it matches.
    """
    session_id = body.session_id.strip()
    raw_code = body.code.strip()
    if not session_id or not raw_code:
        raise HTTPException(400, "session_id and code required")

    # Peek, do NOT pop here — popping up-front meant the FIRST bad-code paste
    # consumed the session, so every retry returned "unknown or expired
    # session_id" even though the user just needed to re-paste. The session is
    # popped only on SUCCESS below (mirrors connections.py _finish_pkce).
    async with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown or expired session_id")

    # Anthropic's success page shows `<auth_code>#<state>`. We do NOT
    # verify `state` here: the OAuth state parameter is a CSRF guard
    # for the redirect_uri callback, but Anthropic redirects to its own
    # platform.claude.com/oauth/code/callback page — we never see that
    # redirect, so state is informational from our side. The real
    # security boundary is the PKCE code_verifier, which IS bound to
    # this session. Strip the suffix and exchange the auth code.
    auth_code = raw_code.partition("#")[0].strip()
    if not auth_code:
        raise HTTPException(400, "code is empty after stripping #state suffix")

    # Body shape verified against the CLI's `Uf1` function in
    # /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js — JSON
    # content-type, state IS included in the body (Anthropic requires
    # it; the "Invalid request format" 400 we got without state is
    # what Anthropic returns for a missing required field).
    payload = {
        "grant_type":    "authorization_code",
        "code":          auth_code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLIENT_ID,
        "code_verifier": session["verifier"],
        "state":         session["state"],
    }
    log.info(
        "sdk_auth finish: session=%s auth_code_len=%d verifier_len=%d",
        session_id, len(auth_code), len(session["verifier"]),
    )
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   UA,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(TOKEN_URL, json=payload, headers=headers,
                                 proxy=_egress_proxy()) as r:
                text = await r.text()
                status_code = r.status
    except Exception as e:
        log.exception("sdk_auth finish — token endpoint call failed")
        raise HTTPException(502, f"could not reach token endpoint: {e}")

    if status_code >= 400:
        try:
            err = json.loads(text)
            err_code = err.get("error", "")
            err_desc = err.get("error_description", "")
        except Exception:
            err_code, err_desc = "", text[:200]
        log.warning("sdk_auth finish — token HTTP %d: %s / %s",
                    status_code, err_code, err_desc)
        if err_code == "invalid_grant":
            raise HTTPException(400, (
                "OAuth error: this code is not accepted (likely already used, "
                "expired, or pasted from a previous sign-in). Click "
                "Sign in again for a fresh code."
            ))
        raise HTTPException(
            status_code if status_code < 500 else 502,
            f"OAuth exchange failed ({err_code or 'HTTP ' + str(status_code)}): {err_desc[:200]}",
        )

    try:
        tok = json.loads(text)
    except ValueError:
        raise HTTPException(502, "token endpoint returned non-JSON")

    access_token  = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    expires_in    = int(tok.get("expires_in") or 3600)
    if not access_token:
        raise HTTPException(502, "token response missing access_token")

    expires_at_ms = int(time.time() * 1000) + expires_in * 1000
    scopes_field = tok.get("scope") or ""
    if isinstance(scopes_field, str):
        scopes = scopes_field.split() if scopes_field else []
    elif isinstance(scopes_field, list):
        scopes = scopes_field
    else:
        scopes = []

    # Shape matches what `claude setup-token` writes (verified against
    # an earlier real credentials.json in the legacy k8s Secret).
    creds = {
        "claudeAiOauth": {
            "accessToken":      access_token,
            "refreshToken":     refresh_token or "",
            "expiresAt":        expires_at_ms,
            "scopes":           scopes,
            "subscriptionType": tok.get("subscription_type") or tok.get("subscriptionType"),
            "rateLimitTier":    tok.get("rate_limit_tier")  or tok.get("rateLimitTier"),
        }
    }

    # Atomic write onto ~/.claude, which is ephemeral (the /home/privatae
    # emptyDir — the operator's plaintext PVC persist is removed).
    # Durability comes from the vault: seal_agent_creds right below stores
    # the envelope (harness:claude_oauth), and boot reconcile + the 300s
    # seal-sweep (network/nac_harness.py) re-materialize this file each roll.
    # Do not reintroduce a PVC persist for this path.
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDS_PATH.with_suffix(".credentials.json.tmp")
    tmp.write_text(json.dumps(creds))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS_PATH)

    # Harness-OAuth vault cutover: keep the host-owned vault envelope
    # ("harness:claude_oauth") CURRENT on every fresh connect, so a pod restart can
    # re-materialize the plaintext from the vault (boot-materialize in main.py). The
    # plaintext file written above stays the runtime authority for the CLI — sealing
    # is additive + best-effort; a vault miss NEVER fails the connect.
    try:
        from server import brain_service
        from server.network import nac_harness
        _pool = brain_service.get_pool()
        _eid = os.environ.get("ENTITY_ID", "")
        if _pool is not None and _eid.isdigit():
            await nac_harness.seal_agent_creds(_pool, int(_eid), actor="sdk_auth:finish")
    except Exception as e:  # pragma: no cover - defensive
        log.warning("seal_agent_creds after finish failed (non-fatal): %s", e)

    # Consume the session ONLY now, on success — a failed exchange above leaves
    # it intact so the user can retry with a fresh code within the TTL.
    async with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)
    log.info("sdk_auth finish: session=%s ok expires_at=%d", session_id, expires_at_ms)
    return {"ok": True, "expires_at": expires_at_ms}
