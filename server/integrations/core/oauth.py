"""
Generic OAuth 2.0 runner — shared across every OAuth provider.

Provider-specific code (Gmail, Drive, Calendar, etc.) gives us:
  - client_family       → "google", "microsoft", "slack", etc. (looks up app creds)
  - authorize_url       → provider's authorize endpoint
  - token_url           → token exchange endpoint
  - scopes              → list of scope strings
  - extra_authorize     → dict of extra query params (access_type, prompt, ...)

And we do the rest:
  - state parameter signing (HMAC over nonce+connection_id)
  - authorize URL construction
  - token exchange
  - token refresh

Access/refresh tokens live in the NaCl vault (core/credentials.py). This
module never persists tokens — it only hands them back to the caller who
stashes them via core/connection.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from server.integrations.core import egress

logger = logging.getLogger("privatae.pc.integrations.oauth")

INTEGRATIONS_CONFIG_PATH = Path("/etc/privatae/certs/integrations.json")

# In-process cache of provider creds. Filled either from
# INTEGRATIONS_CONFIG_PATH on first access, or from a live tunnel fetch
# if the file is missing. 10-minute TTL.
_CACHE_TTL_S = 600
_cache: dict[str, Any] = {"ts": 0.0, "providers": {}}

# State HMAC key — derived once per install from the master vault key so
# state tokens survive restarts but not image rebuilds. Keeps CSRF protection
# bound to this specific PC container.
_STATE_KEY_PATH = Path("/app/secrets/oauth-state.key")


def _state_key() -> bytes:
    if _STATE_KEY_PATH.exists():
        return _STATE_KEY_PATH.read_bytes()
    _STATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    k = _secrets.token_bytes(32)
    _STATE_KEY_PATH.write_bytes(k)
    try:
        os.chmod(_STATE_KEY_PATH, 0o600)
    except OSError:
        pass
    return k


def _load_file_providers() -> dict[str, dict[str, str]]:
    """Synchronous read from the integrations config file shipped at provision
    time. Returns {} if the file is missing — upstream falls back to tunnel."""
    if not INTEGRATIONS_CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(INTEGRATIONS_CONFIG_PATH.read_text())
        return cfg.get("providers", {}) or {}
    except Exception as e:
        logger.warning("failed to read integrations config: %s", e)
        return {}


async def _load_host_providers() -> dict[str, dict[str, str]]:
    """The OAuth clients this HOST owns — the user's own Google Cloud project,
    registered through POST /api/integrations/families/{family}/client.

    Secrets live sealed in the host vault; `oauth_clients` holds only the ref.
    Never raises: a host with no client of its own must fall through to the
    platform's, not fail resolution.
    """
    try:
        from server.integrations.core import credentials, storage
        out: dict[str, dict[str, str]] = {}
        for row in await storage.list_oauth_clients():
            blob = await credentials.resolve(row["credentials_ref"])
            if not blob:
                logger.warning("oauth client for family=%s has an unresolvable ref",
                               row.get("family"))
                continue
            cid, csec = blob.get("client_id"), blob.get("client_secret")
            if cid and csec:
                out[row["family"]] = {"client_id": cid, "client_secret": csec}
        return out
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("host oauth client lookup failed: %s", e)
        return {}


async def _resolve_providers() -> dict[str, dict[str, str]]:
    """Cache-aware provider lookup: memory → host-owned → file.

    HOST-OWNED WINS. A user who registered their own Google client did so
    deliberately, and their grants were issued to that client_id — serving the
    platform's instead would invalidate every refresh token they hold. So when
    the platform later registers its own client, existing self-served hosts keep
    working untouched and only new ones pick up the platform default.
    """
    now = time.time()
    if (now - _cache["ts"]) < _CACHE_TTL_S and _cache["providers"]:
        return _cache["providers"]

    # No platform-held clients on a standalone Host: only the operator's file
    # and the owner's own registered clients.
    providers = dict(_load_file_providers())
    host_owned = await _load_host_providers()
    providers.update(host_owned)

    _cache["providers"] = providers
    # WHICH families the host's own client actually served — recorded here
    # because this is the only place that knows. `oauth_clients` holding a row
    # is not the same answer: _load_host_providers SKIPS a row whose vault ref
    # will not open, and callers that decide anything from the row instead of
    # from this end up pairing one client's id with another client's redirect
    # URI. None means "not known from this path" (see _resolve_providers_sync).
    _cache["host_families"] = set(host_owned)
    _cache["ts"] = now
    return providers


def invalidate_cache() -> None:
    """Drop the memoised provider table. Called after a client is registered or
    removed so the change takes effect on the next authorize instead of up to
    _CACHE_TTL_S later — a ten-minute wait after pasting a client ID reads as
    'it didn't work' and gets pasted again."""
    _cache["ts"] = 0.0
    _cache["providers"] = {}
    _cache["host_families"] = None


def _resolve_providers_sync() -> dict[str, dict[str, str]]:
    """Synchronous path — file only, and therefore BLIND to host-owned clients
    (the vault read is async). No live caller; prefer the async form. Do not
    reach for this to gate UI: it reports "no client" for a host that has one.

    IT MUST STAY UNCALLED, and here is the specific reason rather than a
    preference. It warms `_cache["providers"]` with file-only content AND sets
    `ts`, so for the next _CACHE_TTL_S seconds `_resolve_providers` early-returns
    without ever calling `_load_host_providers`. `app_credentials` would then
    serve the PLATFORM's client while `client_source`, seeing host_families as
    None, pays for the vault read and answers 'host' — the exact divergence
    between the client_id sent and the redirect URI chosen that choose_client
    exists to prevent. Calling this anywhere re-creates that bug at a distance.
    """
    now = time.time()
    if (now - _cache["ts"]) < _CACHE_TTL_S and _cache["providers"]:
        return _cache["providers"]
    providers = _load_file_providers()
    if providers:
        _cache["providers"] = providers
        # Blind to host-owned clients, so it must not leave a set here saying
        # there are none — client_source would then call every host-registered
        # client "the platform's".
        _cache["host_families"] = None
        _cache["ts"] = now
    return providers


async def app_credentials(family: str) -> dict[str, str] | None:
    """Async credentials lookup — host-owned, then file, then tunnel."""
    return (await _resolve_providers()).get(family)


def app_credentials_sync(family: str) -> dict[str, str] | None:
    """Synchronous fallback — file only, blind to host-owned clients."""
    return _resolve_providers_sync().get(family)


async def client_source(family: str) -> str | None:
    """WHOSE client `app_credentials` will actually hand back for this family:
    `'host'`, `'platform'`, or None if there is no usable client at all.

    THE ONE PLACE THAT ANSWERS THIS. Anything deciding behaviour from whether
    `oauth_clients` holds a row is asking a different question and will
    sometimes get a different answer: `_load_host_providers` skips a registered
    client whose sealed secret will not open, and falls through to the
    platform's. A caller that read the row would then send the platform's
    `client_id` with the host's own redirect URI, which Google refuses as
    redirect_uri_mismatch — and the only clue is a screen that says nothing
    about why.
    """
    creds = await app_credentials(family)
    if not (creds and creds.get("client_id") and creds.get("client_secret")):
        return None
    fams = _cache.get("host_families")
    if fams is None:
        # Cache was warmed by a path that cannot see host-owned clients. Pay
        # for the vault read rather than guess.
        fams = set(await _load_host_providers())
    return "host" if family in fams else "platform"


async def has_client(family: str) -> bool:
    """Is there a usable client for this family at all — from whoever.

    DERIVED from client_source rather than re-deriving the same test, so the
    two can never answer differently. It has no production caller left
    (choose_client carries the decision now) and is kept as the plain yes/no
    the gates assert on; re-implementing the credential check here is how the
    duplicate-predicate bug got in to begin with.
    """
    return await client_source(family) is not None


def sign_state(payload: dict[str, Any]) -> str:
    """Encode + sign a state JSON with HMAC-SHA256. Returns `<b64>.<hmac>`."""
    import base64
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    mac = hmac.new(_state_key(), b64.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{b64}.{mac}"


#: Separates the Host's own address from the signed blob in `state`.
#:
#: `~` because it is outside the base64url alphabet (A-Z a-z 0-9 - _) and
#: outside the hostname charset, so the split can never be ambiguous. A `.`
#: would not be: the signed blob is already `<b64>.<mac>` AND a hostname is full
#: of dots, so prefixing with one would hand the verifier a string whose HMAC
#: was never computed, and every platform-routed callback would fail.
STATE_PREFIX_SEP = "~"


def verify_state(state: str, max_age_s: int = 900) -> dict[str, Any] | None:
    """Verify the signature + freshness on a state token. Returns the
    decoded payload or None if invalid.

    Tolerates a routing prefix (`<host>~<b64>.<mac>`). The prefix is read by
    the platform callback to pick a destination and is NOT covered by this
    signature — deliberately, since the platform cannot hold this pod's key.
    Stripping it here is what keeps the trust boundary honest: the prefix
    decides only WHERE the code was sent, and the signature below decides
    whether this Host asked for it at all.
    """
    import base64
    if STATE_PREFIX_SEP in state:
        state = state.split(STATE_PREFIX_SEP, 1)[1]
    try:
        b64, mac = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_state_key(), b64.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        pad = "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64 + pad)
        payload = json.loads(raw)
    except Exception:
        return None
    ts = payload.get("ts", 0)
    if not isinstance(ts, (int, float)) or (time.time() - ts) > max_age_s:
        return None
    return payload


async def authorize_url(
    *,
    family: str,
    authorize_endpoint: str,
    scopes: list[str],
    redirect_uri: str,
    state_payload: dict[str, Any],
    extra_params: dict[str, str] | None = None,
    state_prefix: str | None = None,
) -> str | None:
    """Build the provider's authorize URL for the user's browser.

    `state_prefix` is the address the person opened this Host on, set only when
    riding the platform's OAuth client. It rides in front of the signed blob so
    the platform's single registered callback can tell which Host to hand the
    code back to without holding this pod's signing key.
    """
    creds = await app_credentials(family)
    if not creds:
        logger.warning("no OAuth creds for family=%s", family)
        return None

    payload = dict(state_payload)
    payload["ts"] = int(time.time())
    payload["nonce"] = _secrets.token_urlsafe(12)

    state = sign_state(payload)
    if state_prefix:
        if STATE_PREFIX_SEP in state_prefix:
            # Would split wrong at the far end and route somewhere unintended.
            logger.error("refusing state prefix containing %r: %r",
                         STATE_PREFIX_SEP, state_prefix)
            return None
        state = f"{state_prefix}{STATE_PREFIX_SEP}{state}"

    params: dict[str, str] = {
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if extra_params:
        params.update(extra_params)
    return f"{authorize_endpoint}?{urlencode(params)}"


async def exchange_code(
    *,
    family: str,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    extra_body: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens."""
    creds = await app_credentials(family)
    if not creds:
        raise RuntimeError(f"no OAuth creds for family={family}")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
    }
    if extra_body:
        body.update(extra_body)

    async with egress.session() as s:
        async with s.post(token_endpoint, data=body, timeout=30) as r:
            text = await r.text()
            if r.status >= 400:
                logger.warning("token exchange failed: %d %s", r.status, text[:200])
                raise RuntimeError(f"token exchange failed: {r.status}")
            tok = json.loads(text)
    tok["_received_at"] = int(time.time())
    return tok


async def refresh_token(
    *,
    family: str,
    token_endpoint: str,
    refresh_token_val: str,
    extra_body: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Use a refresh_token to get a fresh access_token."""
    creds = await app_credentials(family)
    if not creds:
        raise RuntimeError(f"no OAuth creds for family={family}")

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_val,
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
    }
    if extra_body:
        body.update(extra_body)

    async with egress.session() as s:
        async with s.post(token_endpoint, data=body, timeout=30) as r:
            text = await r.text()
            if r.status >= 400:
                logger.warning("refresh failed: %d %s", r.status, text[:200])
                raise RuntimeError(f"refresh failed: {r.status}")
            tok = json.loads(text)
    tok["_received_at"] = int(time.time())
    # Google sometimes omits refresh_token on refresh — preserve the old one
    if "refresh_token" not in tok:
        tok["refresh_token"] = refresh_token_val
    return tok


def token_expired(tok: dict[str, Any], skew_s: int = 60) -> bool:
    """Return True if access_token is (or will soon be) expired."""
    exp_in = tok.get("expires_in")
    recv = tok.get("_received_at", 0)
    if not exp_in or not recv:
        return True
    return (time.time() + skew_s) >= (recv + int(exp_in))
