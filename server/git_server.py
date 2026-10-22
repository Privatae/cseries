"""The git server a person connects their Host to, and the one resolver for the
Host's own coding forge.

A Host on its own has no git forge until the person names one: a Gitea or
Forgejo server they already run, and an account on it they made for the Host.
Settings, Git server, connects it (server/routes/git_server.py). This module
keeps what was connected and answers, for every reader of the Host's own coding
forge (its project repositories, Code Approvals, Auto-Approve, the Gitea app's
Host panel, the git command line), which server that is and how the Host signs
in there: forge_identity().

Where each part is kept:
  - the token: sealed in Keys under TOKEN_NAME (server/vault/api.py), never in
    a settings row. The vault holds that name the way it holds an MCP
    server's sign-in (vault/api.GIT_SERVER_TOKEN_NAME): it reads as absent,
    cannot be written and cannot be used by a broker action unless this
    module passes sign_in=True, so no tool, plan or other app can send it
    elsewhere or swap in another account's token;
  - the address and the two usernames: entity_registry.behavioral_params under
    SETTINGS_KEY, where the Host keeps its other settings (backup, harnesses);
  - for the git command line: ~/.git-credentials and ~/.gitconfig, written from
    what is connected at start and on every connect or disconnect
    (sync_git_files). That file holds the token in plain text on the Host's
    volume, as it always has when an install sets GITEA_URL, GITEA_USERNAME
    and GITEA_API_TOKEN (entrypoint.sh).

With nothing connected, forge_identity() answers from that file or those three
variables, so an install that sets them keeps working unchanged.

Only the Host's own coding forge comes from here. Privatae's forge (capability
proposals, bug reports, the community board, and which destinations always take
the platform's way out in nac_egress) stays on GITEA_URL, so nothing meant for
Privatae is ever sent to a person's own server.

Only https: every client of the coding forge (gitea_client.gitea_request,
project_forge.open_forge, the save point's git push, the per-run harness token)
builds https:// addresses, so a plain http server could be stored but never
reached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

log = logging.getLogger("cseries.git_server")

# The one name the token is sealed under in Keys, and the type it carries.
# Equal to vault/api.GIT_SERVER_TOKEN_NAME, which the vault holds for this
# module alone (scripts/test_git_server_connect.py checks they agree).
TOKEN_NAME = "git-server"
TOKEN_TYPE = "git_server_token"
# The behavioral_params key holding the address and usernames.
SETTINGS_KEY = "git_server"

_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
# A Gitea or Forgejo access token is 40 hex characters. Anything URL-safe is
# taken, so a server that issues longer ones still connects; nothing that would
# need escaping inside https://user:token@host.
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{8,255}")
_PATH_SEG = re.compile(r"[A-Za-z0-9._~-]+")
_TIMEOUT_S = 20


class GitServerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Connected:
    """The connected server. `netloc` is host[:port][/path], the form every
    client builds https://{netloc}/... from; `person` is the person's own
    username there, or ""."""
    netloc: str
    user: str
    token: str = field(repr=False)
    person: str = ""
    email: str = ""

    @property
    def url(self) -> str:
        return f"https://{self.netloc}"


_connected: Optional[Connected] = None
_lock = asyncio.Lock()


def connected() -> Optional[Connected]:
    """The server connected in Settings, or None."""
    return _connected


def _env_identity() -> Optional[tuple[str, str, str]]:
    """The install's forge identity from GITEA_URL, GITEA_USERNAME and
    GITEA_API_TOKEN, all three set (entrypoint.sh's condition), or None."""
    url = (os.environ.get("GITEA_URL") or "").strip()
    user = (os.environ.get("GITEA_USERNAME") or "").strip()
    token = (os.environ.get("GITEA_API_TOKEN") or "").strip()
    if not (url and user and token):
        return None
    netloc = url.split("://", 1)[-1].rstrip("/")
    return (user, token, netloc) if netloc else None


def forge_identity() -> Optional[tuple[str, str, str]]:
    """(user, token, netloc) of the Host's own coding forge: the connected
    server, else ~/.git-credentials, else the install's GITEA_* variables.
    None when there is none."""
    c = _connected
    if c is not None:
        return c.user, c.token, c.netloc
    try:
        from server.network import nac_harness
        cred = nac_harness._read_git_credential()
    except Exception as e:  # noqa: BLE001 — an unreadable file is "no file"
        log.warning("git credential read failed: %s", e)
        cred = None
    return cred or _env_identity()


def forge_netloc() -> str:
    """The coding forge's netloc, or "" when there is none. Never raises."""
    ident = forge_identity()
    return ident[2] if ident else ""


def clean_url(raw: object) -> str:
    """The netloc (host[:port][/path]) of a git server address, or
    GitServerError(400) saying what is wrong with it."""
    if not isinstance(raw, str) or not raw.strip():
        raise GitServerError(400, "give the git server's address, for example https://git.example.org")
    text = raw.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise GitServerError(400, "that address is not a web address") from None
    if parts.scheme.lower() != "https":
        raise GitServerError(400, "only https addresses can be connected: the Host's git clients "
                                  "reach a git server over https only, so a plain http server "
                                  "could never be used")
    if parts.username or parts.password:
        raise GitServerError(400, "give the address without a name or password in it; the "
                                  "username and token have their own fields")
    if parts.query or parts.fragment:
        raise GitServerError(400, "give the server's address only, with nothing after ? or #")
    host = (parts.hostname or "").lower()
    if not host or any(c.isspace() for c in host):
        raise GitServerError(400, "that address names no server")
    shown = f"[{host}]" if ":" in host else host
    if port is not None:
        shown += f":{port}"
    segs = [s for s in parts.path.split("/") if s]
    if any(not _PATH_SEG.fullmatch(s) or s in (".", "..") for s in segs):
        raise GitServerError(400, "that address has a path a git server would not be served under")
    return shown + ("/" + "/".join(segs) if segs else "")


def clean_user(raw: object, what: str) -> str:
    u = raw.strip() if isinstance(raw, str) else ""
    if not _USER.fullmatch(u):
        raise GitServerError(400, f"{what} is letters, digits, dots, dashes or underscores, "
                                  "at most 40 characters")
    return u


def clean_token(raw: object) -> str:
    t = raw.strip() if isinstance(raw, str) else ""
    if not _TOKEN.fullmatch(t):
        raise GitServerError(400, "that is not an access token: make one on the server, in the "
                                  "Host account's Settings, Applications")
    return t


def _host_of(netloc: str) -> str:
    h = netloc.split("/", 1)[0]
    if h.startswith("["):
        return h.split("]", 1)[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


# A person's own git server is often on their LAN, so private addresses are
# allowed. The Host's own loopback and link-local addresses are not: inside the
# Host 127.0.0.1 is the Host itself, and 169.254.169.254 is a cloud machine's
# metadata service, neither of them a git server, and the check below sends the
# token to whatever answers. CSERIES_GIT_SERVER_ALLOW_LOOPBACK=1 lifts this for
# a test machine; only whoever sets the Host container's environment can set it.
async def _refuse_own_addresses(netloc: str) -> None:
    import ipaddress
    host = _host_of(netloc).strip("[]")
    if os.environ.get("CSERIES_GIT_SERVER_ALLOW_LOOPBACK", "").strip() == "1":
        return
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, 443)
        except OSError:
            return              # the sign-in below says the server could not be reached
        addrs = set()
        for info in infos:
            try:
                addrs.add(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                continue
    for a in addrs:
        a = getattr(a, "ipv4_mapped", None) or a
        if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
            raise GitServerError(400, f"{_host_of(netloc)} is an address of this Host itself or a "
                                      "link-local one, not a git server: give the address your "
                                      "git server answers on, a LAN address is fine")


# ── the check a connect runs ───────────────────────────────────────────────

async def check_server(netloc: str, user: str, token: str) -> dict:
    """Sign in to the server as the Host's account and see what it may do.

    1. GET /api/v1/user must answer, and its login must be `user`: Gitea signs
       a request in as the token's owner whatever username comes with it, so
       a token of another account would push and merge as that account.
    2. POST /api/v1/user/repos with an empty name: the server checks the
       token's permission before it reads the body, so a token that may create
       repositories gets a validation refusal (422) and one that may not gets
       403, and nothing is created either way.

    Returns {login, email, can_create_repos}; raises GitServerError with the
    words the person reads."""
    import aiohttp
    from server.routes.gitea_client import gitea_proxy
    await _refuse_own_addresses(netloc)
    base = f"https://{netloc}"
    auth = aiohttp.BasicAuth(user, token)
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{base}/api/v1/user", auth=auth, proxy=gitea_proxy(),
                             headers={"Accept": "application/json"}) as r:
                text = await r.text()
                status = r.status
            if status == 401:
                raise GitServerError(400, f"{netloc} did not accept that token")
            if status == 403:
                raise GitServerError(400, f"that token may not read its own account on {netloc}: "
                                          "give it the user read permission (read:user)")
            if status == 404:
                raise GitServerError(400, f"{netloc} has no Gitea or Forgejo API at {base}/api/v1")
            if status >= 300:
                raise GitServerError(502, f"{netloc} answered {status} when the Host signed in")
            try:
                me = json.loads(text)
            except ValueError:
                me = None
            if not isinstance(me, dict) or not me.get("login"):
                raise GitServerError(400, f"{base} did not answer as a Gitea or Forgejo server")
            login = str(me["login"])
            if login.lower() != user.lower():
                raise GitServerError(400, f"that token belongs to {login} on {netloc}, not to "
                                          f"{user}: make the token while signed in as {user}")
            async with s.post(f"{base}/api/v1/user/repos", auth=auth, proxy=gitea_proxy(),
                              json={"name": ""}, headers={"Accept": "application/json"}) as r:
                probe = r.status
    except GitServerError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise GitServerError(502, f"the Host could not reach {base}: {e.__class__.__name__}") from None
    if probe in (400, 422):
        can_create = True
    elif probe in (401, 403):
        raise GitServerError(400, f"that token cannot create repositories on {netloc}: give it "
                                  "write permission for user (write:user), which the server asks for to create "
                                  "a repository, with repository and issue")
    else:
        raise GitServerError(502, f"{netloc} answered {probe} when the Host asked whether it can "
                                  "create a repository")
    return {"login": login, "email": str(me.get("email") or "").strip(),
            "can_create_repos": can_create}


# ── the settings row ───────────────────────────────────────────────────────

async def _read_settings(pool, entity_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp or "{}")
        except ValueError:
            bp = {}
    got = bp.get(SETTINGS_KEY) if isinstance(bp, dict) else None
    return got if isinstance(got, dict) else {}


async def _write_settings(pool, entity_id: int, value: Optional[dict]) -> None:
    if value is None:
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = "
            "COALESCE(behavioral_params, '{}'::jsonb) - $2::text WHERE id = $1",
            entity_id, SETTINGS_KEY)
        return
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = jsonb_set("
        "COALESCE(behavioral_params, '{}'::jsonb), $2::text[], $3::jsonb, true) WHERE id = $1",
        entity_id, [SETTINGS_KEY], json.dumps(value))


def _email_for(user: str, netloc: str, given: str) -> str:
    return given or f"{user}@noreply.{_host_of(netloc).strip('[]')}"


# ── the files the git command line reads ───────────────────────────────────

def _cred_path() -> Path:
    return Path(os.path.expanduser("~/.git-credentials"))


def _gitconfig() -> str:
    return os.path.expanduser("~/.gitconfig")


def _cred_line(user: str, token: str, netloc: str) -> str:
    return f"https://{quote(user, safe='')}:{quote(token, safe='')}@{netloc}"


def _signs_in_as(line: str, user: str, netloc: str) -> bool:
    """Whether a credential line signs `user` in at `netloc`, whatever token it
    carries: the file may hold a token Keys no longer has."""
    m = re.fullmatch(r"https://([^:/@\s]+):([^@\s]+)@(\S+)", line.strip())
    return bool(m) and m.group(1) == quote(user, safe='') and m.group(3) == netloc


def _write_cred_file(user: str, token: str, netloc: str) -> None:
    path = _cred_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_cred_line(user, token, netloc) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _git_config(*args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "config", "--file", _gitconfig(), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("git config %s failed: %s", args[:1], e)
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _set_git_identity(user: str, email: str) -> None:
    _git_config("credential.helper", "store")
    _git_config("user.name", user)
    _git_config("user.email", email)


def sync_git_files(previous: Optional[Connected] = None) -> None:
    """Make ~/.git-credentials and the git identity say what forge_identity()
    says. Connected: that server. Else the install's GITEA_* identity, as
    entrypoint.sh writes it. Else, when `previous` was connected, its line and
    identity are taken away, matched by account and address, not by token; a
    file that holds something else is left alone."""
    c = _connected
    if c is not None:
        _write_cred_file(c.user, c.token, c.netloc)
        _set_git_identity(c.user, _email_for(c.user, c.netloc, c.email))
        return
    env = _env_identity()
    if env is not None:
        user, token, netloc = env
        _write_cred_file(user, token, netloc)
        _set_git_identity(user, f"{user}@privatae.ai")
        return
    if previous is None:
        return
    path = _cred_path()
    try:
        first = (path.read_text().strip().splitlines() or [""])[0].strip()
    except OSError:
        first = ""
    if first and _signs_in_as(first, previous.user, previous.netloc):
        path.unlink(missing_ok=True)
    if _git_config("--get", "user.name") == previous.user:
        _git_config("--unset", "user.name")
        _git_config("--unset", "user.email")


# ── connect, disconnect, status, start ─────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def connect(pool, entity_id: int, *, url: object, username: object, token: object,
                  person_username: object = None) -> dict:
    """Check the server and, when the check passes, keep it: the token sealed
    in Keys, the rest in the settings row, the git files rewritten."""
    global _connected
    from server.vault import api as vault_api
    from server.vault import identity as vault_identity
    netloc = clean_url(url)
    user = clean_user(username, "the Host's username")
    tok = clean_token(token)
    person = ""
    if isinstance(person_username, str) and person_username.strip():
        person = clean_user(person_username, "your username")
        if person.lower() == user.lower():
            raise GitServerError(400, "your username is the account you made for the Host: "
                                      "give your own, or leave it empty")
    async with _lock:
        held = await pool.fetchval(
            "SELECT cred_type FROM vault_envelope WHERE owner_identity = $1 AND logical_name = $2 "
            "AND superseded_at IS NULL LIMIT 1",
            vault_identity.host_identity(entity_id), TOKEN_NAME)
        if held is not None and held != TOKEN_TYPE:
            raise GitServerError(409, f"your Keys app already holds a key named {TOKEN_NAME!r} "
                                      "that is not a git server token: rename or remove it first")
        checked = await check_server(netloc, user, tok)
        await vault_api.store_secret(pool, entity_id=entity_id, name=TOKEN_NAME,
                                     cred_type=TOKEN_TYPE, plaintext=tok.encode(),
                                     actor="settings:git-server", sign_in=True)
        settings = {"url": f"https://{netloc}", "username": checked["login"],
                    "person_username": person, "email": checked["email"],
                    "checked_at": _now(), "can_create_repos": checked["can_create_repos"]}
        await _write_settings(pool, entity_id, settings)
        previous = _connected
        _connected = Connected(netloc, checked["login"], tok, person, checked["email"])
        try:
            await asyncio.to_thread(sync_git_files, previous)
        except Exception as e:  # noqa: BLE001 — the API clients work without the file
            log.warning("git files not written after connect: %s", e)
    log.info("git server connected: %s as %s", netloc, checked["login"])
    return await status(pool, entity_id)


async def disconnect(pool, entity_id: int) -> dict:
    """Forget the connected server: the token out of Keys, the settings row
    cleared, the git files back to what the install sets (or gone)."""
    global _connected
    from server.vault import api as vault_api
    async with _lock:
        # What was connected, from the row when nothing is loaded (its token
        # left Keys), so the file's line for it goes too.
        previous = _connected or _from_settings(await _read_settings(pool, entity_id))
        await vault_api.revoke_secret(pool, entity_id=entity_id, name=TOKEN_NAME,
                                      actor="settings:git-server")
        await _write_settings(pool, entity_id, None)
        _connected = None
        try:
            await asyncio.to_thread(sync_git_files, previous)
        except Exception as e:  # noqa: BLE001
            log.warning("git files not reset after disconnect: %s", e)
    log.info("git server disconnected")
    return await status(pool, entity_id)


async def status(pool, entity_id: int) -> dict:
    """What Settings shows: the connected server and its last check, or the
    forge the install set, or nothing."""
    s = await _read_settings(pool, entity_id) if pool is not None else {}
    c = _connected
    out = {"connected": c is not None, "url": None, "username": None,
           "person_username": None, "checked_at": None, "can_create_repos": None,
           "token_missing": False, "install_forge": None}
    if s:
        out.update({"url": s.get("url"), "username": s.get("username"),
                    "person_username": s.get("person_username") or None,
                    "checked_at": s.get("checked_at"),
                    "can_create_repos": s.get("can_create_repos")})
        out["token_missing"] = c is None
    if c is None:
        ident = forge_identity()
        out["install_forge"] = ident[2] if ident else None
    return out


async def load(pool, entity_id: int) -> None:
    """At start: the connected server back from the settings row and Keys,
    and the git files written from it. Never raises."""
    global _connected
    from server.vault import api as vault_api
    try:
        s = await _read_settings(pool, entity_id)
        if not s.get("url"):
            return
        raw = await vault_api.open_secret(pool, entity_id=entity_id, name=TOKEN_NAME,
                                          actor="git-server:start", sign_in=True)
        if not raw:
            log.warning("git server %s is set but its token is not in Keys", s.get("url"))
            # The file still holds the token from the last start, and
            # forge_identity() reads it there: take it out, so a token removed
            # or switched off in Keys is not used through the file.
            await asyncio.to_thread(sync_git_files, _from_settings(s))
            return
        netloc = clean_url(s["url"])
        _connected = Connected(netloc, str(s.get("username") or ""),
                               raw.decode("utf-8", "replace").strip(),
                               str(s.get("person_username") or ""), str(s.get("email") or ""))
        await asyncio.to_thread(sync_git_files, None)
    except Exception as e:  # noqa: BLE001 — the Host starts without its git server
        log.warning("git server not loaded at start: %s", e)


def _from_settings(s: dict) -> Optional[Connected]:
    """The server a settings row names, with no token, or None: what
    sync_git_files needs to take its line out of the file."""
    try:
        netloc = clean_url((s or {}).get("url"))
    except GitServerError:
        return None
    user = str((s or {}).get("username") or "")
    return Connected(netloc, user, "") if user else None


async def keys_changed(pool, entity_id: int) -> None:
    """The token was revoked, switched off or switched on in the Keys app:
    stop using it now, or take it up again, as the next start would. The
    settings row stays, so Settings says the token is no longer in Keys and
    offers Disconnect or a new connect. Never raises."""
    global _connected
    try:
        async with _lock:
            previous = _connected
            _connected = None
            if previous is not None:
                await asyncio.to_thread(sync_git_files, previous)
        await load(pool, entity_id)
    except Exception as e:  # noqa: BLE001 — Keys' own change has already happened
        log.warning("git server not updated after a Keys change: %s", e)


# ── where the person approves a change on the connected server ─────────────

def pr_page(repo_full: str, number: int, html_url: Optional[str]) -> Optional[str]:
    """The pull request's own page on the connected server, or None when
    nothing is connected. The forge's html_url when it is on the address the
    person connected (the server's configured root can differ from it), else
    the page built from that address."""
    c = _connected
    if c is None:
        return None
    if isinstance(html_url, str) and html_url.startswith(c.url + "/"):
        return html_url
    return f"{c.url}/{repo_full.strip('/')}/pulls/{int(number)}"
