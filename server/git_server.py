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
  - for git itself: no file holds the token. ~/.gitconfig names a credential
    helper (server/git_credential_helper.py, helper_command) that asks this
    process over a Unix socket (serve_credentials), and each answer reads the
    token from Keys for that one sign-in. The Host's own git calls pass the
    same helper with -c (helper_args). sync_git_files, at start and on every
    connect or disconnect, writes the helper and the git identity, and takes
    out of ~/.git-credentials the plain-text line earlier versions (and
    entrypoint.sh, from GITEA_URL, GITEA_USERNAME and GITEA_API_TOKEN) wrote
    there.

With nothing connected, forge_identity() answers from those three variables,
so an install that sets them keeps working unchanged, else from a
~/.git-credentials the person wrote themselves.

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
    server, else the install's GITEA_* variables, else a ~/.git-credentials
    the person wrote (this module writes none). GITEA_* comes before the
    file: entrypoint.sh used to write their line first in it, and now that
    nothing does, what is left there is the person's own servers, never the
    install's forge. None when there is none."""
    c = _connected
    if c is not None:
        return c.user, c.token, c.netloc
    env = _env_identity()
    if env is not None:
        return env
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


# ── what git reads: the helper, the identity, no token file ────────────────

def _cred_path() -> Path:
    return Path(os.path.expanduser("~/.git-credentials"))


def _gitconfig() -> str:
    return os.path.expanduser("~/.gitconfig")


# The socket the helper asks on. In a folder of its own, mode 0700, made by
# this process, so only this container's uid reaches it; the listener checks
# the caller's uid as well (_peer_ok). /tmp is the container's own tmpfs: no
# other container, and nothing on the machine, shares it.
SOCK_ENV = "CSERIES_GIT_CREDENTIAL_SOCK"
_SOCK_DEFAULT = "/tmp/cseries-git/credential.sock"
HELPER = Path(__file__).resolve().with_name("git_credential_helper.py")
_REQUEST_MAX = 64 * 1024


def credential_socket() -> str:
    return os.environ.get(SOCK_ENV) or _SOCK_DEFAULT


def helper_command() -> str:
    """The credential.helper value: git runs it with `get` (or store/erase)
    appended, through the shell because of the leading "!"."""
    import shlex
    import sys
    return "!" + " ".join(shlex.quote(x) for x in
                          (sys.executable, "-I", str(HELPER), credential_socket()))


def helper_args() -> tuple[str, ...]:
    """-c arguments for a git call of the Host's own: no other helper, only
    this one. The git calls that sign in to the coding forge through git's
    own credential lookup pass them (codesearch.repo_sync,
    gitea_propose._git_work), so none depends on what ~/.gitconfig holds."""
    return ("-c", "credential.helper=", "-c", f"credential.helper={helper_command()}")


def _is_our_helper(value: str) -> bool:
    return value.startswith("!") and HELPER.name in value


def _signs_in_as(line: str, user: str, netloc: str) -> bool:
    """Whether a credential line signs `user` in at `netloc`, whatever token it
    carries: the file may hold a token Keys no longer has."""
    m = re.fullmatch(r"https://([^:/@\s]+):([^@\s]+)@(\S+)", line.strip())
    return bool(m) and m.group(1) == quote(user, safe='') and m.group(3) == netloc


def _authority(netloc: str) -> str:
    """host[:port] of a netloc, lowercased: what git sends as `host=`."""
    return netloc.split("/", 1)[0].lower()


def _line_origin(line: str) -> str:
    m = re.fullmatch(r"(https?)://[^@\s]*@([^/\s]+)\S*", line.strip())
    return f"{m.group(1)}://{m.group(2)}" if m else ""


def drop_written_lines(ours: set) -> tuple[int, list[str]]:
    """Take out of ~/.git-credentials every line this module or entrypoint.sh
    wrote there: one that signs in one of `ours` ((user, netloc) pairs: the
    connected server, the one the settings row names, the install's GITEA_*),
    whatever token it carries. Both writers wrote exactly that shape and
    nothing else (https://<user>:<token>@<netloc>, one line). Any other line
    is the person's own and stays; the file goes only when nothing else is
    left in it. Returns (lines taken out, the https://host of each line left)."""
    path = _cred_path()
    try:
        if path.is_symlink() or not path.is_file():
            return 0, []
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError):
        return 0, []
    keep = [ln for ln in lines if not any(_signs_in_as(ln, u, n) for u, n in ours)]
    dropped = len(lines) - len(keep)
    if dropped:
        if keep:
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(keep) + "\n")
            os.replace(tmp, path)
        else:
            path.unlink(missing_ok=True)
    return dropped, [_line_origin(ln) for ln in keep]


def _git_config(*args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "config", "--file", _gitconfig(), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("git config %s failed: %s", args[:1], e)
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _set_helper(left: list[str], forge_hosts: set) -> None:
    """~/.gitconfig's credential.helper: this module's helper, and no `store`.
    `store` (which this module and entrypoint.sh used to set) is taken out
    because git hands every sign-in that worked to every helper to keep, and
    `store` would write the token back into ~/.git-credentials. When that
    file still holds the person's own lines (`left`), `store` stays for each
    of their servers alone, never for the coding forge's own host."""
    have = (_git_config("--get-all", "credential.helper") or "").splitlines()
    want = helper_command()
    if "store" in have:
        _git_config("--unset-all", "credential.helper", "^store$")
    for v in have:
        if _is_our_helper(v) and v != want:
            _git_config("--unset-all", "credential.helper", "^" + re.escape(v) + "$")
    if want not in have:
        _git_config("--add", "credential.helper", want)
    for origin in sorted(set(left)):
        if origin and origin.split("://", 1)[-1].lower() not in forge_hosts:
            _git_config(f"credential.{origin}.helper", "store")


def _set_git_identity(user: str, email: str) -> None:
    _git_config("user.name", user)
    _git_config("user.email", email)


def sync_git_files(previous: Optional[Connected] = None) -> None:
    """Make what git reads say what forge_identity() says, with no token on
    the disk: the credential helper (_set_helper), and the git identity of the
    connected server, else of the install's GITEA_* identity as entrypoint.sh
    sets it; else, when `previous` was connected, its identity is taken away
    (matched by account, not by token). Every time, the plain-text lines
    earlier versions wrote into ~/.git-credentials for any of these accounts
    are taken out (drop_written_lines)."""
    c = _connected
    env = _env_identity()
    ours = {(x.user, x.netloc) for x in (c, previous) if x is not None and x.user}
    if env is not None:
        ours.add((env[0], env[2]))
    dropped, left = drop_written_lines(ours)
    if dropped:
        log.info("took %d plain-text git sign-in line(s) out of the git credential file; git "
                 "now asks the Host for the token at each sign-in", dropped)
    _set_helper(left, {_authority(n) for _u, n in ours})
    if c is not None:
        _set_git_identity(c.user, _email_for(c.user, c.netloc, c.email))
        return
    if env is not None:
        _set_git_identity(env[0], f"{env[0]}@privatae.ai")
        return
    if previous is not None and _git_config("--get", "user.name") == previous.user:
        _git_config("--unset", "user.name")
        _git_config("--unset", "user.email")


# ── the helper's answers ───────────────────────────────────────────────────

async def credential_for(pool, entity_id: int, protocol: str, host: str) -> Optional[tuple[str, str]]:
    """(username, token) for one git sign-in at https://<host>, or None.
    The connected server's token is read from Keys now, for this answer
    alone, so a token revoked or switched off in Keys stops working at the
    next sign-in. With none there, the install's GITEA_* identity, as
    forge_identity() falls back. Only https, and only the forge's own host."""
    if protocol != "https" or not host:
        return None
    host = host.lower()
    s = await _read_settings(pool, entity_id) if pool is not None else {}
    if s.get("url") and s.get("username"):
        from server.vault import api as vault_api
        try:
            netloc = clean_url(s["url"])
        except GitServerError:
            netloc = ""
        raw = None
        if netloc:
            raw = await vault_api.open_secret(pool, entity_id=entity_id, name=TOKEN_NAME,
                                              actor="git-credential-helper", sign_in=True)
        if raw:
            if _authority(netloc) != host:
                return None
            return str(s["username"]), raw.decode("utf-8", "replace").strip()
    env = _env_identity()
    if env is not None and _authority(env[2]) == host:
        return env[0], env[1]
    return None


def _parse_request(data: bytes) -> dict:
    out: dict = {}
    for line in data.decode("utf-8", "replace").splitlines():
        k, sep, v = line.partition("=")
        if sep and k in ("protocol", "host"):
            out[k] = v.strip()
    return out


def _peer_ok(writer) -> bool:
    """The caller runs as this process's own uid (SO_PEERCRED)."""
    import socket
    import struct
    sock = writer.get_extra_info("socket")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, AttributeError):
        return False
    return uid == os.getuid()


_cred_server = None


async def serve_credentials(pool, entity_id: int) -> None:
    """Listen on credential_socket() for the helper's requests, for the life
    of the process. Never raises: without it git gets no token for the forge,
    and the Host's API clients still work."""
    global _cred_server
    import contextlib
    import stat as _stat

    async def handle(reader, writer):
        try:
            if not _peer_ok(writer):
                return
            data = await asyncio.wait_for(reader.read(_REQUEST_MAX), timeout=10)
            req = _parse_request(data)
            got = await credential_for(pool, entity_id, req.get("protocol", ""),
                                       req.get("host", ""))
            if got:
                writer.write(f"username={got[0]}\npassword={got[1]}\n".encode())
                await writer.drain()
        except Exception as e:  # noqa: BLE001 — an empty answer is "no credential"
            log.warning("git credential request failed: %s", e.__class__.__name__)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    path = Path(credential_socket())
    try:
        if _cred_server is not None:
            _cred_server.close()
        folder = path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(folder)
        if not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f"{folder} is not a folder of this Host's own")
        os.chmod(folder, 0o700)
        path.unlink(missing_ok=True)
        _cred_server = await asyncio.start_unix_server(handle, path=str(path))
        os.chmod(path, 0o600)
        log.info("git credential helper listening on %s", path)
    except Exception as e:  # noqa: BLE001
        log.warning("git credential helper not started: %s", e)


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
    and what git reads written from it (sync_git_files, which also takes out
    the plain-text token line an earlier version left on the volume, with
    nothing connected too). Never raises."""
    global _connected
    from server.vault import api as vault_api
    try:
        s = await _read_settings(pool, entity_id)
        if not s.get("url"):
            await asyncio.to_thread(sync_git_files, None)
            return
        raw = await vault_api.open_secret(pool, entity_id=entity_id, name=TOKEN_NAME,
                                          actor="git-server:start", sign_in=True)
        if not raw:
            log.warning("git server %s is set but its token is not in Keys", s.get("url"))
            # An earlier version's file may still hold the token, and
            # forge_identity() reads that file: take it out, so a token
            # removed or switched off in Keys is not used through it.
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
