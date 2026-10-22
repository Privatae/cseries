"""
Shared gitea API client.

Calls the Host's OWN coding forge through the pod's egress proxy: the git
server connected in Settings, else the forge the install set (the token
entrypoint.sh writes into ~/.git-credentials from GITEA_*). Which one is
server/git_server.forge_identity, the one resolver every coding-forge reader
goes through.

Extracted from routes/workbench.py so BOTH the Workbench PR surface and the Gitea
app's /api/gitea/* surface call one client — no parallel implementations. The host
always acts as ITSELF via its own gitea credential.

Fails LOUD on a non-2xx (raises HTTPException so callers surface it); callers that
want to degrade (show a "connect gitea" state) check gitea_creds() first.
"""

import json as jsonlib
import logging

import aiohttp
from fastapi import HTTPException

logger = logging.getLogger("privatae.pc.gitea_client")

GITEA_TIMEOUT = aiohttp.ClientTimeout(total=20)


def gitea_creds():
    """(user, token, host) of the Host's own coding forge, or None
    (server/git_server.forge_identity)."""
    try:
        from server.git_server import forge_identity
        return forge_identity()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("gitea credential read failed: %s", e)
        return None


def gitea_proxy() -> str:
    from server.network.nac_harness import _egress_proxy
    return _egress_proxy()


def gitea_netloc() -> str:
    """The gitea host (netloc) — from the host credential, else GITEA_URL env.
    No default forge: with neither set this raises 409."""
    import os
    cred = gitea_creds()
    if cred:
        return cred[2]
    netloc = (os.environ.get("GITEA_URL") or "").strip().split("://", 1)[-1].rstrip("/")
    if not netloc:
        raise HTTPException(409, "no forge configured on this host (GITEA_URL)")
    return netloc


async def gitea_oauth_exchange(host: str, form: dict) -> dict:
    """POST the gitea OAuth2 token endpoint (/login/oauth/access_token) through the
    egress proxy — form-encoded, PKCE (no client secret). Returns the token JSON."""
    url = f"https://{host}/login/oauth/access_token"
    async with aiohttp.ClientSession(timeout=GITEA_TIMEOUT) as s:
        async with s.post(url, data=form, proxy=gitea_proxy(),
                          headers={"Accept": "application/json"}) as r:
            text = await r.text()
            if r.status >= 300:
                raise HTTPException(r.status, f"gitea oauth: {text[:300]}")
            try:
                return jsonlib.loads(text)
            except ValueError:
                raise HTTPException(502, "gitea oauth: non-JSON token response")


async def gitea_request(method: str, path: str, *, params=None, json=None,
                        raw_text: bool = False, bearer: str | None = None):
    """Call gitea's API through the egress proxy. `path` is the /api/v1/... suffix.
    By default authenticates as the HOST (the pod's per-entity credential); pass
    `bearer` to authenticate as the USER (their OAuth2 access token) instead.
    Returns parsed JSON (or raw text when raw_text). Raises HTTPException on non-2xx."""
    if bearer:
        # A person's token here is one the platform minted for Privatae's
        # forge. With a git server connected, the forge below is the person's
        # own server, which must never be handed that token.
        from server.git_server import connected
        if connected() is not None:
            raise HTTPException(409, "signing in as you is not available on a connected git "
                                     "server: open the server's own page")
        host = gitea_netloc()
        req_auth = None
        headers = {"Authorization": f"Bearer {bearer}"}
    else:
        cred = gitea_creds()
        if not cred:
            raise HTTPException(409, "no gitea credential on this host")
        host = cred[2]
        req_auth = aiohttp.BasicAuth(cred[0], cred[1])
        headers = None
    url = f"https://{host}{path}"
    async with aiohttp.ClientSession(timeout=GITEA_TIMEOUT) as s:
        async with s.request(
            method, url, params=params, json=json, headers=headers,
            proxy=gitea_proxy(), auth=req_auth,
        ) as r:
            # Read the body as text FIRST and check status before parsing JSON — a
            # non-JSON error body (squid 502 HTML, gitea maintenance page) must
            # surface as a clean HTTPException, not a raw 500 from a JSON parse.
            text = await r.text()
            if r.status == 401 and bearer:
                # The user token is dead (deleted in gitea settings, or pruned by a sibling
                # pod's mint). Forget it so the next call re-mints instead of 401-ing forever.
                try:
                    from server.routes import gitea as _gitea
                    if _gitea._user_tok.get("access_token") == bearer and _gitea._user_tok.get("login"):
                        await _gitea.forget_user_token()   # memory AND vault, or it reloads
                except Exception:
                    pass
            if r.status >= 300:
                detail = text
                try:
                    parsed = jsonlib.loads(text)
                    if isinstance(parsed, dict) and parsed.get("message"):
                        detail = parsed["message"]
                except ValueError:
                    pass
                raise HTTPException(r.status, f"gitea: {str(detail)[:300]}")
            if raw_text:
                return text
            # Several gitea endpoints return a 2xx with an EMPTY body (merge is
            # one: 200, no content). That is a success, not a protocol error —
            # raising here reported "502" on merges that had actually landed.
            if not text.strip():
                return {}
            try:
                return jsonlib.loads(text)
            except ValueError:
                raise HTTPException(502, "gitea: non-JSON response on a 2xx")


def split_repo(repo: str) -> tuple[str, str]:
    if "/" not in (repo or ""):
        raise HTTPException(400, "repo must be 'owner/name'")
    owner, name = repo.split("/", 1)
    return owner.strip(), name.strip()
