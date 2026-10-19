"""
Gitea app — three panels over the configured forge (GITEA_URL). The USER panels authenticate AS THE
USER via gitea's OAuth2/SSO (the same Authentik the dashboard uses), so the server
logs the app in as you, not as the host.

  • Your repos   (/user/repos, browse) — your OWN repositories, via your gitea OAuth2
    token (obtained through /oauth/login — seamless, you're already Authentik-SSO'd).
  • Host repos   (/repos, browse)       — the host's own gitea identity (entity-<id>),
    via the pod's per-entity credential. Its clean room.
  • Permissions  (/repo/.../host-access) — grant/revoke the HOST's access to YOUR
    repos: as the owner (your token) we add/remove the host as a collaborator.

Auth model: the pod holds the HOST credential (BasicAuth). The USER's OAuth2 token
is obtained via the SSO login flow and kept IN MEMORY only (never written to disk).
Public client + PKCE — no client secret to store; the client_id is public. Reads
gate read:review; grant/revoke gate write:review.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from server.applications.middleware import requires_permission
from server.deep_link import app_link
from server.routes.gitea_client import (
    gitea_creds, gitea_netloc, gitea_oauth_exchange, gitea_request,
)

router = APIRouter(prefix="/api/gitea", tags=["gitea"])
log = logging.getLogger("privatae.pc.gitea")

_READ = Depends(requires_permission("read:review"))
_WRITE = Depends(requires_permission("write:review"))

_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")


def _slug(seg: str, what: str) -> str:
    if not seg or not _SLUG.match(seg):
        raise HTTPException(400, f"invalid {what}")
    return seg


def _clean_path(path: str) -> str:
    p = (path or "").strip().lstrip("/")
    if ".." in p.split("/"):
        raise HTTPException(400, "invalid path")
    return p


def _repo_row(r: dict) -> dict:
    return {
        "full_name": r.get("full_name"), "name": r.get("name"),
        "owner": ((r.get("owner") or {}).get("login")),
        "private": bool(r.get("private")),
        "default_branch": r.get("default_branch"),
        "empty": bool(r.get("empty")),
        "updated_at": r.get("updated_at"),
    }


# ── OAuth2: log the app in AS THE USER via the shared SSO ─────────────────────
# gitea federates the same Authentik the dashboard uses, so authorize is seamless.
# Public client + PKCE (no client secret to hold); the client_id is public — like the
# OAuth client_ids already in connections.py. The user's access token lives in memory
# only, never on disk, and is used to call gitea AS THE USER.
GITEA_OAUTH_CLIENT_ID = os.environ.get(
    "GITEA_OAUTH_CLIENT_ID", "3563ccfa-9870-4d0d-b177-589885f600bc")

_pkce: dict[str, dict] = {}   # state -> {verifier, redirect_uri, ts}
_user_tok: dict = {}          # {access_token, refresh_token, expires_at}
_PKCE_TTL = 600


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _reap_pkce() -> None:
    now = time.time()
    for k in [k for k, v in _pkce.items() if now - v["ts"] > _PKCE_TTL]:
        _pkce.pop(k, None)


# ── durable user session ──────────────────────────────────────────────────────
# The user's gitea OAuth token lived IN MEMORY only (_user_tok), so every pod restart
# wiped it and the VS Code editor opened signed-out. Persist the session host-sealed
# per-entity (server/vault) so it survives rolls: after one seamless sign-in the editor
# is signed in on every open, forever (the refresh_token is auto-refreshed on use).
# It is the USER's token (user-identity-primary) held on their behalf by their own host.
_VAULT_NAME = "gitea:user_oauth"


def _gitea_entity_id() -> int:
    try:
        return int(os.environ.get("ENTITY_ID", "0") or 0)
    except ValueError:
        return 0


async def _persist_user_tok() -> None:
    """Durably persist the user's gitea OAuth session (host-sealed, per-entity). The
    refresh_token is the load-bearing bit; access tokens are short-lived. Fail-soft —
    a vault hiccup must never break sign-in."""
    rtok = _user_tok.get("refresh_token")
    if not rtok and not _user_tok.get("login"):
        return
    try:
        from server import brain_service
        from server.vault import api as vault
        payload = json.dumps({
            "refresh_token": rtok,
            "access_token": _user_tok.get("access_token"),
            "expires_at": _user_tok.get("expires_at"),
            "login": _user_tok.get("login"),
            "email": _user_tok.get("email"),
        }).encode()
        await vault.store_secret(
            brain_service.get_pool(), entity_id=_gitea_entity_id(),
            name=_VAULT_NAME, cred_type="gitea_oauth", plaintext=payload)
    except Exception:
        pass


async def _load_user_tok() -> None:
    """Prime _user_tok from the durable store when memory is empty (after a restart),
    so the editor is signed in on open with no re-login. Idempotent; fail-soft."""
    if _user_tok.get("access_token") or _user_tok.get("refresh_token"):
        return
    try:
        from server import brain_service
        from server.vault import api as vault
        raw = await vault.open_secret(
            brain_service.get_pool(), entity_id=_gitea_entity_id(), name=_VAULT_NAME)
        if raw:
            d = json.loads(raw.decode())
            if d.get("refresh_token") or (d.get("login") and d.get("access_token")):
                _user_tok.update({
                    "access_token": d.get("access_token"),
                    "refresh_token": d.get("refresh_token"),
                    "expires_at": d.get("expires_at"),
                    "login": d.get("login"),
                    "email": d.get("email"),
                })
    except Exception:
        pass


def _user_bearer() -> str | None:
    """The user's gitea OAuth2 access token, if signed in and unexpired."""
    tok = _user_tok.get("access_token")
    if not tok:
        return None
    exp = _user_tok.get("expires_at")
    if exp and time.time() > exp - 30:
        _user_tok.clear()
        return None
    return tok


# ── host sign-in (2026-08-28): the platform mints the user's token, no browser hop ──
# The platform forge answers only platform addresses, so the OAuth authorize page
# below is unreachable from the user's own network. The user is already signed in to
# this Host; the Host asks the platform (bridge op gitea.user_token, entity from mTLS)
# for a scoped PAT on the user's behalf: automatic, and revocable here.
# A PAT has no refresh token and no expiry; the record carries `login` for git basic-auth.
_connect_lock = asyncio.Lock()


async def _bridge_op(op: str, params: dict) -> dict:
    from server.main import get_bridge_client
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise HTTPException(503, "bridge not connected")
    try:
        env = await bc.request_full(op, params)
    except Exception as e:
        raise HTTPException(502, f"bridge unreachable: {e}")
    if not env.get("ok"):
        raise HTTPException(int(env.get("code") or 502), env.get("error") or f"{op} failed")
    return env.get("data") or {}


async def _ensure_user_token(request_email: str | None = None) -> str | None:
    """Return the user's gitea token, minting it through the platform when this Host
    holds none. The token is HOST-scoped: one per Host, for the Host's owner unless a
    connecting operator's verified email is given (then that operator, if the platform
    accepts them). Editor opens and the bastion helper call this with no email, so they
    mint for the owner. Serialised so a burst of first calls mints once. Fail-soft: None."""
    async with _connect_lock:
        await _load_user_tok()
        if _user_tok.get("access_token") and not _user_tok.get("refresh_token"):
            return _user_tok["access_token"]          # a platform-minted PAT: no expiry
        if _user_tok.get("access_token"):
            return None                               # legacy OAuth session; caller refreshes
        try:
            data = await _bridge_op("gitea.user_token", {"email": (request_email or "").lower()})
        except HTTPException:
            return None
        tok = data.get("token")
        if not tok:
            return None
        _user_tok.clear()
        _user_tok.update({"access_token": tok, "refresh_token": None, "expires_at": None,
                          "login": data.get("login"), "email": data.get("email")})
        await _persist_user_tok()
        return tok


async def forget_user_token() -> None:
    """A platform-minted token proved dead (401 from gitea): drop it from memory AND the
    vault, so the next call re-mints instead of _load_user_tok restoring the corpse."""
    if not _user_tok.get("login"):
        return
    _user_tok.clear()
    try:
        from server import brain_service
        from server.vault import api as vault
        await vault.revoke_secret(brain_service.get_pool(), entity_id=_gitea_entity_id(),
                                  name=_VAULT_NAME, actor="gitea-client:401")
    except Exception:
        pass


def user_login() -> str | None:
    """The user's gitea login when a platform-minted token is held (PAT basic-auth
    needs the real login; the OAuth flow used the literal `oauth2`)."""
    return _user_tok.get("login")


async def _user_bearer_fresh() -> str | None:
    """Like _user_bearer, but REFRESH via the stored refresh_token when the access
    token is near expiry instead of dropping the session — so long-lived surfaces
    (the VS Code editor's git credential broker) keep a valid token without forcing
    a re-login. Returns None only when there's no session and no usable refresh.
    Host sign-in first: with no session at all, mint through the platform."""
    await _load_user_tok()  # restore a persisted session after a pod restart
    tok = _user_tok.get("access_token")
    exp = _user_tok.get("expires_at")
    if tok and not (exp and time.time() > exp - 60):
        return tok
    rtok = _user_tok.get("refresh_token")
    if not rtok:
        if not tok:
            return await _ensure_user_token()
        return _user_bearer()
    try:
        fresh = await gitea_oauth_exchange(gitea_netloc(), {
            "grant_type": "refresh_token",
            "client_id": GITEA_OAUTH_CLIENT_ID,
            "refresh_token": rtok,
        })
    except Exception:
        return None
    at = (fresh or {}).get("access_token")
    if not at:
        return None
    _user_tok.clear()
    _user_tok.update({
        "access_token": at,
        # gitea rotates the refresh token on use; fall back to the old one if absent.
        "refresh_token": fresh.get("refresh_token") or rtok,
        "expires_at": (time.time() + fresh["expires_in"]) if fresh.get("expires_in") else None,
    })
    await _persist_user_tok()  # gitea rotated the refresh_token — re-seal it
    return at


@router.get("/oauth/login", dependencies=[_READ])
async def oauth_login(request: Request):
    """Start gitea SSO login — return the authorize URL (PKCE). The app opens it; the
    user is already Authentik-SSO'd so it's a seamless redirect back to the callback."""
    _reap_pkce()
    xf = request.headers.get("origin") or ""
    if not xf:
        h = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        xf = f"https://{h}" if h else ""
    redirect_uri = f"{xf.rstrip('/')}/api/gitea/oauth/callback"
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))
    _pkce[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "ts": time.time()}
    q = urlencode({
        "client_id": GITEA_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {"authorize_url": f"https://{gitea_netloc()}/login/oauth/authorize?{q}"}


@router.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = ""):
    """gitea redirects here with the code. Exchange it (PKCE) for the user's token,
    hold it in memory, and hand back a page that closes the popup / returns."""
    entry = _pkce.pop(state, None)
    if not entry or not code:
        return HTMLResponse(
            "<p>Sign-in link expired — reopen the Gitea app and try again.</p>",
            status_code=400)
    tok = await gitea_oauth_exchange(gitea_netloc(), {
        "grant_type": "authorization_code",
        "client_id": GITEA_OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": entry["redirect_uri"],
        "code_verifier": entry["verifier"],
    })
    _user_tok.clear()
    _user_tok.update({
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_at": (time.time() + tok["expires_in"]) if tok.get("expires_in") else None,
    })
    await _persist_user_tok()  # durable: the editor stays signed in across pod rolls
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Signed in</title>"
        "<body style='font-family:sans-serif;padding:2rem'>Signed in to gitea — you can close this tab."
        "<script>try{if(window.opener){window.opener.postMessage('gitea-oauth-done','*');window.close();}"
        "else{location.replace('/');}}catch(e){location.replace('/');}</script></body>")


@router.post("/connect", dependencies=[_READ])
async def connect(request: Request):
    """Host sign-in: obtain the user's gitea token through the platform — no popup.
    The requester's verified email (X-Authentik-Email, set by the direct router)
    is passed along; the platform honours it only if it is an operator of this Host."""
    email = (request.headers.get("x-authentik-email") or "").strip().lower() or None
    tok = await _ensure_user_token(email)
    if not tok:
        raise HTTPException(503, "gitea connect failed — the platform could not mint a token; try again")
    return {"authed": True, "login": user_login()}


@router.post("/disconnect", dependencies=[_WRITE])
async def disconnect(request: Request):
    """Revoke the platform-minted token(s) for this (user, Host) and forget the session."""
    # Revoke the HELD identity's tokens (the email the platform resolved at mint), not
    # whoever clicked — operator B disconnecting must kill operator A's PAT, not B's none.
    await _load_user_tok()   # a fresh pod holds it in the vault, not in memory
    email = (_user_tok.get("email") or request.headers.get("x-authentik-email") or "").strip().lower()
    out = {}
    if _user_tok.get("login") or not _user_tok.get("refresh_token"):
        try:
            out = await _bridge_op("gitea.user_token_revoke", {"email": email})
        except HTTPException as e:
            out = {"revoked": 0, "error": e.detail}
    _user_tok.clear()
    try:
        from server import brain_service
        from server.vault import api as vault
        await vault.revoke_secret(brain_service.get_pool(), entity_id=_gitea_entity_id(),
                                  name=_VAULT_NAME, actor="gitea-app:disconnect")
    except Exception:
        pass
    return {"authed": False, "revoked": out.get("revoked", 0), "error": out.get("error")}


@router.get("/oauth/status", dependencies=[_READ])
async def oauth_status():
    # Restore + refresh a persisted session first, so a freshly-rolled pod reports
    # authed (no spurious re-login prompt on editor open).
    fresh = await _user_bearer_fresh()
    return {"authed": fresh is not None, "login": user_login()}


# ── status ────────────────────────────────────────────────────────────────────
@router.get("/status", dependencies=[_READ])
async def status():
    cred = gitea_creds()
    if not cred:
        return {"connected": False, "reason": "no gitea credential on this host"}
    return {"connected": True, "host": cred[2], "host_identity": cred[0],
            "authed": _user_bearer() is not None}


# ── USER lens: your own repos (as YOU, via your OAuth2 token) ──────────────────
@router.get("/user/repos", dependencies=[_READ])
async def user_repos():
    bearer = _user_bearer()
    if not bearer:
        return {"connected": bool(gitea_creds()), "authed": False,
                "login_required": True, "repos": []}
    data = await gitea_request("GET", "/api/v1/user/repos",
                               params={"limit": 50, "sort": "updated"}, bearer=bearer)
    out = [_repo_row(r) for r in (data or []) if isinstance(r, dict)]
    return {"connected": True, "authed": True, "repos": out}


# ── HOST lens: the host's own repos (as the host) ─────────────────────────────
@router.get("/repos", dependencies=[_READ])
async def repos():
    if not gitea_creds():
        return {"connected": False, "repos": []}
    data = await gitea_request("GET", "/api/v1/user/repos",
                               params={"limit": 50, "sort": "updated"})
    out = [_repo_row(r) for r in (data or []) if isinstance(r, dict)]
    return {"connected": True, "host": gitea_netloc(), "repos": out}


# ── PERMISSIONS: grant/revoke the host's access to YOUR repos (as the owner) ───
class HostAccess(BaseModel):
    permission: str = "read"  # read | write | admin


def _require_user_bearer() -> str:
    b = _user_bearer()
    if not b:
        raise HTTPException(409, "sign in to gitea first (Your repos → Sign in)")
    return b


@router.get("/repo/{owner}/{name}/host-access", dependencies=[_READ])
async def get_host_access(owner: str, name: str):
    """What access does the HOST (entity-<id>) currently have on this repo?"""
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    bearer = _require_user_bearer()
    host_login = gitea_creds()[0]
    try:
        r = await gitea_request(
            "GET",
            f"/api/v1/repos/{owner}/{name}/collaborators/{quote(host_login, safe='')}/permission",
            bearer=bearer)
    except HTTPException as e:
        if e.status_code == 404:
            return {"host": host_login, "permission": "none"}
        raise
    perm = (r or {}).get("permission") if isinstance(r, dict) else None
    return {"host": host_login, "permission": perm or "none"}


@router.put("/repo/{owner}/{name}/host-access", dependencies=[_WRITE])
async def grant_host_access(owner: str, name: str, body: HostAccess):
    """Grant the HOST access to this repo — add entity-<id> as a collaborator, as you."""
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    bearer = _require_user_bearer()
    host_login = gitea_creds()[0]
    perm = body.permission if body.permission in ("read", "write", "admin") else "read"
    await gitea_request(
        "PUT",
        f"/api/v1/repos/{owner}/{name}/collaborators/{quote(host_login, safe='')}",
        json={"permission": perm}, raw_text=True, bearer=bearer)
    await _notify_host_of_grant(f"{owner}/{name}", perm)
    await _index_readable_repos_now(f"{owner}/{name}")
    return {"ok": True, "host": host_login, "permission": perm}


async def _index_readable_repos_now(granted: str = "") -> None:
    """Run the code index's next pass now (codesearch.repo_sync): a granted
    repo is indexed, a revoked one dropped, without waiting for the timer.
    The pass asks the forge itself; this only says when, and puts a repo just
    granted on record (code_repos, repo_sync.remember_grant) — a read grant
    on a public repo is not in gitea's listing, and a record outlives a
    restart before the pass, where a note in memory would not."""
    try:
        from codesearch import repo_sync
        if granted:
            from codesearch import store
            await repo_sync.remember_grant(store.connect(), granted)
        repo_sync.wake()
    except Exception as e:  # noqa: BLE001
        log.info("gitea: code index pass not woken: %s", type(e).__name__)


async def _notify_host_of_grant(repo: str, perm: str) -> None:
    """Tell the HOST a repo was just granted. Read access means index, so
    nothing is asked: the code index's next pass (woken by the
    grant route) clones and indexes it. Two writes, both best-effort:

    * the host's own message list (context store inbox/messages.json) — the
      one that lands in the host's system prompt as "Messages For You" on its
      next turn (runtime/session.py), so the host knows the repo is there;
    * a durable inbox row for the user (the same bus a finished build uses),
      payload.event='gitea_grant', so the grant is on the record and
      reach-out can fire on it.

    Nothing here clones or embeds: codesearch.repo_sync does, on the forge's
    own answer of what the host can read."""
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is not None:
            import uuid
            from datetime import datetime, timezone
            from mastercode.context_store import ContextStore
            await ContextStore(pool, brain_service._entity_id).append_to_list("inbox/messages.json", {
                "id": str(uuid.uuid4()),
                "content": (
                    f"The user just granted you {perm} access to the repository {repo} on "
                    f"the forge (Gitea app → Permissions). It is being indexed now: "
                    f"search_my_code finds it (named {repo!r}) in a few minutes, and the gitea "
                    f"tool reads it already."
                ),
                "source": "gitea",
                "action": "gitea_grant",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "delivered_to_session": None,
                "read": False,
            }, max_items=50)
    except Exception as e:  # noqa: BLE001
        log.info("gitea grant: host message not written: %s", type(e).__name__)
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        await handle_inbox_append({
            "subject": f"Your host can now read {repo}",
            "body": (f"You granted the host {perm} access to {repo}. It indexes the "
                     "repository so it can search it by meaning, and reads it live meanwhile. "
                     "Revoking the access removes the index."),
            "body_format": "text/markdown",
            "message_kind": "system_notification",
            "payload": {"event": "gitea_grant", "repo": repo, "permission": perm,
                        "link": app_link("gitea")},
        })
    except Exception as e:  # noqa: BLE001
        log.info("gitea grant: inbox notice not written: %s", type(e).__name__)


@router.delete("/repo/{owner}/{name}/host-access", dependencies=[_WRITE])
async def revoke_host_access(owner: str, name: str):
    """Revoke the HOST's access to this repo — remove entity-<id> as a collaborator."""
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    bearer = _require_user_bearer()
    host_login = gitea_creds()[0]
    await gitea_request(
        "DELETE",
        f"/api/v1/repos/{owner}/{name}/collaborators/{quote(host_login, safe='')}",
        raw_text=True, bearer=bearer)
    await _index_readable_repos_now()  # access gone: the next pass drops its index
    return {"ok": True, "host": host_login, "permission": "none"}


# ── Repo browse (uses YOUR token when signed in, else the host credential) ─────
@router.get("/repo/{owner}/{name}", dependencies=[_READ])
async def repo_overview(owner: str, name: str):
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    r = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}", bearer=_user_bearer())
    if not isinstance(r, dict):
        raise HTTPException(502, "unexpected gitea response")
    return {
        "full_name": r.get("full_name"), "name": r.get("name"),
        "description": r.get("description") or "",
        "private": bool(r.get("private")), "empty": bool(r.get("empty")),
        "default_branch": r.get("default_branch"),
        "size": r.get("size"), "updated_at": r.get("updated_at"),
    }


@router.get("/repo/{owner}/{name}/branches", dependencies=[_READ])
async def repo_branches(owner: str, name: str):
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    data = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}/branches",
                               params={"limit": 50}, bearer=_user_bearer())
    out = [{"name": b.get("name"), "commit_sha": ((b.get("commit") or {}).get("id"))}
           for b in (data or []) if isinstance(b, dict)]
    return {"branches": out}


@router.get("/repo/{owner}/{name}/commits", dependencies=[_READ])
async def repo_commits(owner: str, name: str, limit: int = Query(20, ge=1, le=50)):
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    data = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}/commits",
                               params={"limit": limit}, bearer=_user_bearer())
    out = []
    for c in (data or []):
        if not isinstance(c, dict):
            continue
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        out.append({
            "sha": c.get("sha"),
            "message": (commit.get("message") or "").strip().splitlines()[0][:200]
            if commit.get("message") else "",
            "author": author.get("name"), "date": author.get("date"),
        })
    return {"commits": out}


@router.get("/repo/{owner}/{name}/contents", dependencies=[_READ])
async def repo_contents(owner: str, name: str,
                        path: str = Query("", description="repo-relative path")):
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    rel = _clean_path(path)
    suffix = f"/{quote(rel, safe='/')}" if rel else ""
    data = await gitea_request(
        "GET", f"/api/v1/repos/{owner}/{name}/contents{suffix}", bearer=_user_bearer())

    if isinstance(data, list):
        entries = [{"name": e.get("name"), "path": e.get("path"),
                    "type": e.get("type"), "size": e.get("size")}
                   for e in data if isinstance(e, dict)]
        entries.sort(key=lambda e: (e.get("type") != "dir", (e.get("name") or "").lower()))
        return {"type": "dir", "path": rel, "entries": entries}

    if isinstance(data, dict) and data.get("type") == "file":
        return {"type": "file", "name": data.get("name"), "path": data.get("path"),
                "size": data.get("size"), "encoding": data.get("encoding"),
                "content": data.get("content")}

    raise HTTPException(404, "not a file or directory")
