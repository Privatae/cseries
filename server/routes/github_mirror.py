"""
GitHub mirrors — the agent-free half of the project repo pipeline:
connect GitHub once, pick repos, and gitea itself keeps them
current as native pull-mirrors. Zero inference on the green path.

Auth model — DEVICE FLOW, deliberately:
  • GitHub OAuth *web* flow allows ONE registered callback URL per app; entity
    hostnames are per-handle, so it breaks exactly like the gitea
    "Unregistered Redirect URI" incident. The device flow
    has no redirect at all — works from every entity host, forever.
  • No client secret in the pod: device flow needs only the public client_id
    (GITHUB_OAUTH_CLIENT_ID env — config-driven, no fallback).
  • The user's GitHub OAuth token is stored as the typed Keys 'github'
    credential — the ONE credential of record, shared with
    the github_repo tool's ladder (_github_repo_token).

Mirroring runs AS THE USER via their gitea OAuth bearer (same session as the
app's other user panels): repos land under the user's own gitea account. For a
PRIVATE source, gitea must hold a token to run future pulls — surfaced in the
UI as an explicit consent line before the mirror is created.
"""

import logging
import os
import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.applications.middleware import requires_permission
from server.routes.gitea_client import gitea_request

logger = logging.getLogger("privatae.pc.github_mirror")

router = APIRouter(prefix="/api/gitea", tags=["gitea-mirrors"])

_READ = Depends(requires_permission("read:review"))
_WRITE = Depends(requires_permission("write:review"))

_GH_TIMEOUT = aiohttp.ClientTimeout(total=25)
_MIRROR_INTERVAL = "8h0m0s"   # gitea's own default cadence; "Sync now" covers immediacy

# state -> {device_code, interval, expires, last_poll} — in-memory only; the
# device_code is worthless after expiry and never leaves the server.
_DEVICE: dict[str, dict] = {}


def _client_id() -> str:
    cid = (os.environ.get("GITHUB_OAUTH_CLIENT_ID") or "").strip()
    if not cid:
        raise HTTPException(
            503, "GitHub connect is not configured on this platform "
                 "(GITHUB_OAUTH_CLIENT_ID unset)")
    return cid


def _pool_eid():
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "Brain not initialized")
    eid = int(os.environ.get("ENTITY_ID", "0") or 0)
    return pool, eid


async def _gh(method: str, url: str, *, token: str | None = None,
              form: dict | None = None) -> tuple[int, object]:
    """One shape for every github.com/api.github.com call this module makes.
    trust_env=True: entity pods are IPv6-only — the IPv4 internet is reachable
    ONLY through the squid proxy env (same pattern as realtime.py's token
    fetch); without it these calls dead-end."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"
    async with aiohttp.ClientSession(timeout=_GH_TIMEOUT, trust_env=True) as s:
        async with s.request(method, url, headers=headers, data=form) as r:
            try:
                data = await r.json()
            except Exception:
                data = await r.text()
            return r.status, data


async def _keys_github_token() -> str | None:
    pool, eid = _pool_eid()
    try:
        from mastercode.runtime.tools import _github_repo_token
        return await _github_repo_token(pool, eid)
    except Exception as e:
        logger.warning("Keys github token read failed: %s", e)
        return None


async def _user_bearer() -> str:
    """The USER's gitea OAuth bearer — the refresh-aware variant, so mirrors
    keep working across pod restarts without a fresh sign-in."""
    from server.routes.gitea import _user_bearer_fresh
    tok = await _user_bearer_fresh()
    if not tok:
        raise HTTPException(409, "sign in to gitea first (Your repos → Sign in)")
    return tok


def _reap_device() -> None:
    now = time.time()
    for k in [k for k, v in _DEVICE.items() if v["expires"] < now]:
        _DEVICE.pop(k, None)


# ── GitHub connection (device flow → Keys) ─────────────────────────────


@router.post("/github/device/start", dependencies=[_WRITE])
async def device_start():
    """Begin the GitHub device flow: returns the code the user enters at
    github.com/login/device. Nothing secret goes to the browser."""
    _reap_device()
    status, data = await _gh(
        "POST", "https://github.com/login/device/code",
        form={"client_id": _client_id(), "scope": "repo"})
    if status != 200 or not isinstance(data, dict) or "device_code" not in data:
        raise HTTPException(502, f"GitHub device start failed: {str(data)[:200]}")
    import secrets as _secrets
    state = _secrets.token_urlsafe(24)
    _DEVICE[state] = {
        "device_code": data["device_code"],
        "interval": int(data.get("interval") or 5),
        "expires": time.time() + int(data.get("expires_in") or 900),
        "last_poll": 0.0,
    }
    # The Gitea app links this address. Only an https one is passed on (an
    # href would run a `javascript:` URL); anything else is GitHub's page.
    verification_uri = str(data.get("verification_uri") or "")
    if not verification_uri.startswith("https://"):
        verification_uri = "https://github.com/login/device"
    return {"state": state,
            "user_code": data.get("user_code"),
            "verification_uri": verification_uri,
            "expires_in": int(data.get("expires_in") or 900)}


class DevicePoll(BaseModel):
    state: str


@router.post("/github/device/poll", dependencies=[_WRITE])
async def device_poll(body: DevicePoll):
    """Poll the device grant. On success the OAuth token is written to Keys as
    the typed 'github' credential and never returned to the browser."""
    entry = _DEVICE.get(body.state)
    if not entry:
        raise HTTPException(404, "unknown or expired connect attempt — start again")
    wait = entry["interval"] - (time.time() - entry["last_poll"])
    if wait > 0:
        return {"pending": True, "retry_in": round(wait, 1)}
    entry["last_poll"] = time.time()

    status, data = await _gh(
        "POST", "https://github.com/login/oauth/access_token",
        form={"client_id": _client_id(),
              "device_code": entry["device_code"],
              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if not isinstance(data, dict):
        raise HTTPException(502, f"GitHub token poll failed: {str(data)[:200]}")
    err = data.get("error")
    if err == "authorization_pending":
        return {"pending": True, "retry_in": entry["interval"]}
    if err == "slow_down":
        entry["interval"] = int(data.get("interval") or entry["interval"] + 5)
        return {"pending": True, "retry_in": entry["interval"]}
    if err:
        _DEVICE.pop(body.state, None)
        raise HTTPException(400, f"GitHub connect failed: {err}")

    token = (data.get("access_token") or "").strip()
    if not token:
        raise HTTPException(502, "GitHub returned no access token")
    _DEVICE.pop(body.state, None)

    pool, eid = _pool_eid()
    from server.vault import api as vault
    await vault.store_secret(
        pool, entity_id=eid, name="github", cred_type="api_key",
        plaintext=token.encode(), actor=f"github_mirror:{eid}")

    st, user = await _gh("GET", "https://api.github.com/user", token=token)
    login = user.get("login") if st == 200 and isinstance(user, dict) else None
    return {"pending": False, "connected": True, "login": login}


@router.get("/github/status", dependencies=[_READ])
async def github_status():
    """Is a Keys 'github' credential present and alive? For GitHub-App tokens
    also says whether the app is INSTALLED anywhere (installed + manage_url),
    so the panel can walk the user to repo selection instead of dead-ending on
    an empty picker. installed is absent for classic-OAuth tokens."""
    token = await _keys_github_token()
    if not token:
        return {"connected": False}
    st, user = await _gh("GET", "https://api.github.com/user", token=token)
    if st != 200 or not isinstance(user, dict):
        return {"connected": False, "stale": True}
    out: dict = {"connected": True, "login": user.get("login")}
    st, inst = await _gh(
        "GET", "https://api.github.com/user/installations?per_page=100",
        token=token)
    if st == 200 and isinstance(inst, dict) and "installations" in inst:
        installs = [i for i in (inst.get("installations") or []) if isinstance(i, dict)]
        out["installed"] = bool(installs)
        slug = next((i.get("app_slug") for i in installs if i.get("app_slug")), None)
        out["manage_url"] = (f"https://github.com/apps/{slug}/installations/new"
                             if slug else None)
    return out


def _picker_repo(r: dict) -> dict:
    return {"full_name": r.get("full_name"), "private": bool(r.get("private")),
            "description": (r.get("description") or "")[:140],
            "updated_at": r.get("updated_at"),
            "default_branch": r.get("default_branch")}


# Every repository is listed (the picker once stopped at 300, so an older
# repository of a user with more could never be picked).
# GitHub pages at 100; the walk follows the pages to the end. _GH_PAGE_BACKSTOP
# only stops a listing that never ends (100,000 repositories), and says so.
_GH_PAGE_BACKSTOP = 1000


def _repo_matches(r: dict, q: str) -> bool:
    if not q:
        return True
    q = q.lower()
    return q in (r.get("full_name") or "").lower() or q in (r.get("description") or "").lower()


@router.get("/github/repos", dependencies=[_READ])
async def github_repos(q: str = ""):
    """The connected user's GitHub repos (for the mirror picker). `q` narrows
    them to names or descriptions containing it (a search, never a cap).

    The platform registration is a GitHub APP (device flow, Contents
    read-only): its user tokens see ONLY repositories where the user has
    INSTALLED the app and selected repos — a freshly authorized user gets an
    empty /user/repos. So list through the app's installations, and when
    there are none yet say so (needs_install + manage_url) instead of
    rendering an empty picker. /user/repos stays as the fallback for a
    classic-OAuth client id, where /user/installations doesn't apply."""
    token = await _keys_github_token()
    if not token:
        raise HTTPException(409, "connect GitHub first")

    st, inst = await _gh(
        "GET", "https://api.github.com/user/installations?per_page=100",
        token=token)
    if st == 200 and isinstance(inst, dict) and "installations" in inst:
        installs = [i for i in (inst.get("installations") or []) if isinstance(i, dict)]
        # app_slug rides on every installation object — it's how the picker
        # links "choose repositories" without the platform configuring a slug.
        slug = next((i.get("app_slug") for i in installs if i.get("app_slug")), None)
        manage_url = f"https://github.com/apps/{slug}/installations/new" if slug else None
        if not installs:
            return {"repos": [], "clipped": False,
                    "needs_install": True, "manage_url": None}
        repos: list[dict] = []
        seen: set[str] = set()
        truncated = False       # an installation had more pages than we fetched
        fetch_failed = False    # a listing call errored — distinct from "no repos"
        for i in installs:
            iid = i.get("id")
            if not iid:
                continue
            for page in range(1, _GH_PAGE_BACKSTOP + 1):
                st, data = await _gh(
                    "GET",
                    f"https://api.github.com/user/installations/{iid}"
                    f"/repositories?per_page=100&page={page}",
                    token=token)
                if st != 200 or not isinstance(data, dict):
                    fetch_failed = True
                    break
                batch = [r for r in (data.get("repositories") or [])
                         if isinstance(r, dict) and r.get("full_name")]
                for r in batch:
                    if r["full_name"] in seen:
                        continue
                    seen.add(r["full_name"])
                    repos.append(_picker_repo(r))
                if len(batch) < 100:
                    break
                if page == _GH_PAGE_BACKSTOP:
                    truncated = True
        if fetch_failed and not repos:
            # don't misreport a GitHub outage as "no repositories selected"
            raise HTTPException(502, "GitHub repository listing failed — try again")
        repos.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        out = [r for r in repos if _repo_matches(r, q)]
        res = {"repos": out, "total": len(out), "clipped": truncated or fetch_failed,
               "needs_install": False, "manage_url": manage_url}
        if fetch_failed:
            res["note"] = ("GitHub stopped answering part-way through the listing — "
                           "some repositories may be missing; try again")
        return res

    # classic-OAuth fallback (no installations concept on those tokens)
    repos = []
    truncated = False
    for page in range(1, _GH_PAGE_BACKSTOP + 1):
        st, data = await _gh(
            "GET",
            "https://api.github.com/user/repos"
            f"?affiliation=owner&sort=updated&per_page=100&page={page}",
            token=token)
        if st != 200 or not isinstance(data, list):
            raise HTTPException(502, f"GitHub repo list failed (HTTP {st})")
        repos += [_picker_repo(r) for r in data]
        if len(data) < 100:
            break
        if page == _GH_PAGE_BACKSTOP:
            truncated = True
    out = [r for r in repos if _repo_matches(r, q)]
    return {"repos": out, "total": len(out), "clipped": truncated,
            "needs_install": False, "manage_url": None}


# ── Mirrors (gitea-native, as the USER) ────────────────────────────────


@router.get("/mirrors", dependencies=[_READ])
async def list_mirrors():
    """The user's gitea repos that are pull-mirrors."""
    bearer = await _user_bearer()
    out = []
    for page in (1, 2, 3, 4):
        data = await gitea_request("GET", "/api/v1/user/repos",
                                   params={"limit": 50, "page": page}, bearer=bearer)
        for r in data or []:
            if not r.get("mirror"):
                continue
            out.append({"full_name": r.get("full_name"),
                        "original_url": r.get("original_url"),
                        "private": bool(r.get("private")),
                        "mirror_interval": r.get("mirror_interval"),
                        "updated_at": r.get("mirror_updated") or r.get("updated_at")})
        if not data or len(data) < 50:
            break
    return {"mirrors": out}


class CreateMirror(BaseModel):
    owner_repo: str            # github "owner/repo"
    private_source: bool = False


@router.post("/mirrors", dependencies=[_WRITE])
async def create_mirror(body: CreateMirror):
    """Mirror one GitHub repo into the USER's gitea account (mirror: true —
    gitea keeps pulling on its own; no agent ever runs for this)."""
    import re
    owner_repo = body.owner_repo.strip().strip("/")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$", owner_repo):
        raise HTTPException(400, "owner_repo must look like 'owner/repo'")
    bearer = await _user_bearer()
    repo_name = owner_repo.split("/")[1]

    payload = {
        "clone_addr": f"https://github.com/{owner_repo}.git",
        "repo_name": repo_name,
        "service": "github",
        "mirror": True,
        "mirror_interval": _MIRROR_INTERVAL,
        "private": True,  # mirrors are private in the community regardless of source
        "description": f"Mirror of github.com/{owner_repo}",
    }
    if body.private_source:
        token = await _keys_github_token()
        if not token:
            raise HTTPException(409, f"{owner_repo} is private — connect GitHub first")
        # Consent shown in the UI: gitea stores this token (encrypted) to run
        # future pulls of the private source.
        payload["auth_token"] = token

    data = await gitea_request("POST", "/api/v1/repos/migrate",
                               json=payload, bearer=bearer)
    return {"ok": True, "full_name": (data or {}).get("full_name"),
            "interval": _MIRROR_INTERVAL}


@router.post("/mirrors/{owner}/{name}/sync", dependencies=[_WRITE])
async def sync_mirror(owner: str, name: str):
    """Ask gitea to pull the mirror now (manual immediacy on top of the cadence)."""
    from server.routes.gitea import _slug
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    bearer = await _user_bearer()
    await gitea_request("POST", f"/api/v1/repos/{owner}/{name}/mirror-sync",
                        bearer=bearer)
    return {"ok": True}


@router.delete("/mirrors/{owner}/{name}", dependencies=[_WRITE])
async def delete_mirror(owner: str, name: str):
    """Remove a mirror. Guarded: refuses on a non-mirror repo — this surface
    can only delete what it created. GitHub is never touched (pull-only)."""
    from server.routes.gitea import _slug
    owner, name = _slug(owner, "owner"), _slug(name, "repo")
    bearer = await _user_bearer()
    repo = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}", bearer=bearer)
    if not (repo or {}).get("mirror"):
        raise HTTPException(409, f"{owner}/{name} is not a mirror — not deletable here")
    await gitea_request("DELETE", f"/api/v1/repos/{owner}/{name}", bearer=bearer)
    return {"ok": True}
