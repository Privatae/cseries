"""server/mcp_oauth.py — signing in to an MCP server with OAuth.

The MCP authorization spec, revision 2026-07-28 (basic/authorization and its
pages authorization-server-discovery, client-registration and
security-considerations), for a connection in mcp_connections that holds no
key of its own. The table is migrations/v239 `mcp_oauth`; the wire to the MCP
server is server/mcp_client.py, which reports a 401 (or a 403 with a
challenge) as McpError(kind='auth') with the server's WWW-Authenticate
headers.

DISCOVERY. A server that wants a sign-in names its Protected Resource Metadata
(RFC 9728) in the challenge, or serves it at the well-known path under its
endpoint, then at the root. That document names the authorization server; its
metadata is read from the RFC 8414 and OpenID Connect paths in the spec's
order, and used only when its `issuer` is the issuer the URL was built from
and it lists S256 in code_challenge_methods_supported (the spec's MUSTs: no
PKCE, no sign-in).

REGISTRATION, in the spec's order: a client the person registered themselves
and entered on the server's card ('pre'); a Client ID Metadata Document, when
the authorization server supports them and the document lists this Host's
redirect URI ('cimd'); Dynamic Client Registration, deprecated but what most
servers still offer ('dcr'). A registration is kept per connection and bound to
the issuer that made it. The document is the one C-Series publishes
(CIMD_URL_DEFAULT, from .github/pages), which lists the default address's
redirect URI, so every Host on that address signs in as the same client;
CSERIES_OAUTH_CLIENT_METADATA_URL names another, and set empty it turns them
off. The Host reads the document before it uses it, and goes on to the next
way when it cannot.

THE SIGN-IN is the person's act, from the Network app (routes/mcp_oauth.py):
begin() returns the authorization URL with a PKCE S256 challenge, a `state`,
the `resource` (RFC 8707) and the scope; the browser comes back to
/api/mcp/oauth/callback, where finish() takes the pending sign-in by the
state's hash (single use, ten minutes), checks `iss` against the issuer
recorded before the redirect (RFC 9207), and only then trades the code.

THE REDIRECT URI is https on the Host's own address when CSERIES_URL is https.
On the default address (http://home.cseries.localhost:8800) it is
http://127.0.0.1:<port>: an authorization server sends a code only to https or
to the loopback, and RFC 8252 asks for the loopback address rather than a name.
The browser has no session on 127.0.0.1, so the callback is reachable without
one (middleware/localhost_only.OAUTH_CALLBACK_PATHS) and is bound by the state.

TOKENS live in the vault, never in a table: the access token is
`mcp-oauth:<name>:<connection id>`, injected by the vault broker as a bearer
like any key (vault/authz.py, action mcp_call), and the refresh token and any
client secret are `mcp-oauth-refresh:<name>:<connection id>`. The vault
itself reads names under `mcp-oauth` as absent, and refuses to write them,
for every caller but this module, the MCP client's own call and the person's
step-up reveal (vault/api.SIGN_IN_PREFIX); keys_manage, connect and the broker
also say so plainly. So a token reaches its own server and nothing else. A token is refreshed shortly before it expires and once after a
401; a refused refresh needs a new sign-in. A 403 with insufficient_scope keeps
the token and asks for a step-up: the next sign-in asks for the union of the
scopes already asked for and the ones the server named.

Every request this module makes goes through the egress gate, like the
client's, after the host's name is checked to resolve only to public
addresses (mcp_connections.public_host_refusal), and never follows a redirect.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as _dt
import hashlib
import itertools
import json
import logging
import os
import re
import secrets as _secrets
import time
from typing import Any, Optional
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import aiohttp

from server import mcp_client
# The vault keeps the names under this prefix for the sign-in alone.
from server.vault.api import SIGN_IN_PREFIX as SECRET_PREFIX

log = logging.getLogger("privatae.mcp_oauth")

CALLBACK_PATH = "/api/mcp/oauth/callback"
CIMD_URL_ENV = "CSERIES_OAUTH_CLIENT_METADATA_URL"
# Published by .github/workflows/pages.yml from .github/pages. An authorization
# server does not follow a redirect to it, so the address must answer the
# document itself; that workflow's header says what would move it.
CIMD_URL_DEFAULT = "https://privatae.github.io/cseries/oauth/client-metadata.json"
CLIENT_NAME = "C-Series"
PENDING_TTL_S = 600
# A token this close to its expiry is refreshed before it is used.
REFRESH_EARLY_S = 60
# This many sign-ins asked for the access the server names and it still
# refused: another sign-in will not satisfy it, and the person is told so.
MAX_STEP_UPS = 3
# One request to a metadata, registration or token endpoint, from the name
# lookup to the last byte (_http).
HTTP_TIMEOUT_S = 20.0
# A Sign in press waits for the connection's lock this long past one request
# (HTTP_TIMEOUT_S), then says to try again: the lock is held for at most one
# token request and the reads and writes around it (refresh), so this is for
# a database that stalls, not for a slow authorization server.
LOCK_MARGIN_S = 5.0
MAX_DOC_BYTES = 256 * 1024
_TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,256}$")   # RFC 6749 3.3


class OAuthError(ValueError):
    """A sign-in step that did not work, said for the person and the Host. It
    never carries a token, a code or a client secret."""
    caller_refusal = True


class SignInNeeded(OAuthError):
    """The server needs the person to sign in (again)."""


class NeedsClient(OAuthError):
    """The authorization server offers no way for this Host to register
    itself: the person registers a client there and enters it."""

    def __init__(self, message: str, *, issuer: str, redirect_uri: str):
        super().__init__(message)
        self.issuer = issuer
        self.redirect_uri = redirect_uri


class _TokenRefused(OAuthError):
    def __init__(self, message: str, *, error: Optional[str], status: int):
        super().__init__(message)
        self.error = error
        self.status = status


# ── small pieces ─────────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _aware(v: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=_dt.timezone.utc)
    return v


def _error_code(v: Any) -> Optional[str]:
    """An OAuth `error` value fit to repeat: a plain token, else None. The
    authorization server's own wording (error_description) is never passed to
    the Host; the Network app shows it to the person, escaped."""
    s = str(v or "")
    return s if _ERROR_CODE_RE.match(s) else None


def _scopes(v: Any) -> list[str]:
    """A space-separated scope string, or a list, as an ordered list of valid
    scope tokens."""
    items = v.split() if isinstance(v, str) else (v if isinstance(v, list) else [])
    out: list[str] = []
    for s in items:
        if isinstance(s, str) and _SCOPE_TOKEN_RE.match(s) and s not in out:
            out.append(s)
    return out


def _union(*lists: list[str]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        for s in lst:
            if s not in out:
                out.append(s)
    return out


def access_secret_name(conn: dict) -> str:
    return f"{SECRET_PREFIX}:{conn['name']}:{conn['id']}"


def refresh_secret_name(conn: dict) -> str:
    return f"{SECRET_PREFIX}-refresh:{conn['name']}:{conn['id']}"


def is_reserved_key_name(name: Any) -> bool:
    """A vault name kept for the MCP client (vault/api.is_sign_in_name)."""
    from server.vault.api import is_sign_in_name
    return is_sign_in_name(name)


# ── WWW-Authenticate (RFC 9110 11.6.1) ──────────────────────────────────────

def parse_challenges(values) -> list[tuple[str, dict]]:
    """[(scheme, {param: value})] from WWW-Authenticate header values. A
    header can carry several challenges, separated by commas, each a scheme
    and its auth-params (token=token or token="quoted"). A token68 credential
    (Basic's) reads as a scheme with no params, which is harmless here: only
    the Bearer challenge is used."""
    out: list[tuple[str, dict]] = []
    for v in values or ():
        v = str(v)
        i, n = 0, len(v)
        cur: Optional[tuple[str, dict]] = None
        while i < n:
            while i < n and v[i] in " \t,":
                i += 1
            if i >= n:
                break
            m = _TOKEN_RE.match(v, i)
            if not m:
                break
            tok, j = m.group(0), m.end()
            k = j
            while k < n and v[k] in " \t":
                k += 1
            if cur is not None and k < n and v[k] == "=" and not v.startswith("==", k):
                k += 1
                while k < n and v[k] in " \t":
                    k += 1
                if k < n and v[k] == '"':
                    k += 1
                    buf = []
                    while k < n and v[k] != '"':
                        if v[k] == "\\" and k + 1 < n:
                            k += 1
                        buf.append(v[k])
                        k += 1
                    k += 1
                    val = "".join(buf)
                else:
                    m2 = _TOKEN_RE.match(v, k)
                    val = m2.group(0) if m2 else ""
                    k = m2.end() if m2 else k
                cur[1].setdefault(tok.lower(), val)
                i = k
            else:
                # A token68 value straight after a scheme ends with '=' padding.
                while j < n and v[j] == "=":
                    j += 1
                cur = (tok.lower(), {})
                out.append(cur)
                i = j
    return out


def bearer_challenge(values) -> Optional[dict]:
    """The Bearer challenge's params, or None when the server sent none."""
    for scheme, params in parse_challenges(values):
        if scheme == "bearer":
            return params
    return None


# ── URLs ─────────────────────────────────────────────────────────────────────

def canonical_resource(url: str) -> str:
    """The MCP server's canonical URI (spec "Canonical Server URI"): https, the
    host lowercased, no default port, no query or fragment, and no trailing
    slash on a bare origin."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    netloc = host + (f":{port}" if port and port != 443 else "")
    path = p.path or ""
    if path == "/":
        path = ""
    return urlunsplit(("https", netloc, path, "", ""))


def _same_origin_prefix(resource: str, server_url: str) -> bool:
    """Whether a Protected Resource Metadata `resource` names this server: the
    same origin, and a path that is the server's own or a parent of it on a
    segment boundary. The MCP Python SDK applies the same rule
    (check_resource_allowed); RFC 9728 asks for an exact match, and a parent
    path only widens which of the server's own paths the token is for."""
    try:
        r, s = urlsplit(resource), urlsplit(server_url)
        if r.scheme.lower() != "https" or s.scheme.lower() != "https":
            return False
        if (r.hostname or "").lower() != (s.hostname or "").lower() or (r.port or 443) != (s.port or 443):
            return False
    except ValueError:
        return False
    if r.query or r.fragment:
        return False
    rp, sp = (r.path or "").rstrip("/"), (s.path or "").rstrip("/")
    return sp == rp or sp.startswith(rp + "/")


def _https_url(v: Any) -> Optional[str]:
    """`v` when it is an absolute https URL with a host and no credentials."""
    if not isinstance(v, str) or not v.lower().startswith("https://"):
        return None
    try:
        p = urlsplit(v)
        _ = p.port
    except ValueError:
        return None
    if not p.hostname or p.username is not None or p.password is not None or p.fragment:
        return None
    return v


def prm_urls(server_url: str) -> list[str]:
    """The well-known Protected Resource Metadata URLs, in the spec's order:
    under the endpoint's path, then at the root."""
    p = urlsplit(server_url)
    origin = f"https://{p.netloc}"
    path = (p.path or "").rstrip("/")
    urls = []
    if path:
        urls.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return urls


def as_metadata_urls(issuer: str) -> list[str]:
    """The authorization server metadata URLs, in the spec's order: RFC 8414
    with the path inserted, OpenID Connect with it inserted, OpenID Connect
    with it appended; for an issuer with no path, the first two."""
    p = urlsplit(issuer)
    origin = f"https://{p.netloc}"
    path = (p.path or "").rstrip("/")
    if path:
        return [f"{origin}/.well-known/oauth-authorization-server{path}",
                f"{origin}/.well-known/openid-configuration{path}",
                f"{origin}{path}/.well-known/openid-configuration"]
    return [f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration"]


def redirect_uri() -> str:
    """Where the authorization server sends the browser back. OAuthError when
    this Host is opened on an address no authorization server will send a
    code to (http on a name that is not this machine's)."""
    from server.home_origin import HOME
    if HOME.scheme == "https":
        return f"{HOME.origin}{CALLBACK_PATH}"
    host = HOME.host
    if host == "localhost" or host.endswith(".localhost"):
        port = f":{HOME.port}" if HOME.port else ""
        return f"http://127.0.0.1{port}{CALLBACK_PATH}"
    raise OAuthError(f"C-Series is opened on {HOME.origin}, and an authorization server sends a "
                     "sign-in back only to an https address or to this machine. Open C-Series on "
                     "https (CSERIES_URL), or on this machine, to sign in to a server.")


# ── one request to a metadata, registration or token endpoint ───────────────

async def _http(method: str, url: str, *, what: str, form: Optional[dict] = None,
                json_body: Optional[dict] = None, headers: Optional[dict] = None,
                timeout_s: Optional[float] = None) -> tuple[int, Any]:
    """(status, the JSON body or None) of one request through the egress gate.
    https only, no redirects, the host checked to resolve only to public
    addresses and not to be one of the platform's own. `timeout_s` (default
    HTTP_TIMEOUT_S) covers the whole of it, the name lookup included: a
    refresh holds the connection's lock for this, and a Sign in press waits
    behind it."""
    budget = HTTP_TIMEOUT_S if timeout_s is None else float(timeout_s)
    end = time.monotonic() + budget
    if not _https_url(url):
        raise OAuthError(f"{what} is not an https URL")
    host = (urlsplit(url).hostname or "").lower()
    from server.mcp_connections import public_host_refusal
    from server.network.nac_egress import _is_platform_dest
    if _is_platform_dest(host):
        raise OAuthError(f"{what} is on {host}, one of the platform's own services")
    why = await public_host_refusal(host, timeout_s=budget)
    if why:
        raise OAuthError(f"{what} was not reached: {why}")
    left = end - time.monotonic()
    if left <= 0:
        raise OAuthError(f"{what} did not answer in time ({host})")
    h = {"Accept": "application/json", **(headers or {})}
    data: Optional[bytes] = None
    if form is not None:
        data = urlencode(form).encode("ascii")
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        h["Content-Type"] = "application/json"
    try:
        async with mcp_client._session(left) as session:
            async with session.request(method, url, data=data, headers=h,
                                       proxy=mcp_client.gate_proxy(), allow_redirects=False) as resp:
                raw = await mcp_client._read_capped(resp, MAX_DOC_BYTES)
                status = resp.status
    except mcp_client.McpError as e:
        raise OAuthError(f"{what}: {e}") from None
    except asyncio.TimeoutError:
        raise OAuthError(f"{what} did not answer in time ({host})") from None
    except aiohttp.ClientHttpProxyError as e:
        raise OAuthError(f"the egress gate refused {host} (HTTP {e.status}): if your egress rules end "
                         f"in a deny, allow {host} in the Network app") from None
    except aiohttp.ClientError as e:
        raise OAuthError(f"{what} was not reached ({host}: {type(e).__name__})") from None
    try:
        body = json.loads(raw) if raw.strip() else None
    except ValueError:
        body = None
    return status, body


# ── the state of one connection's sign-in ───────────────────────────────────

_COLS = ("connection_id, entity_id, status, resource, resource_metadata_url, issuer, "
         "authorization_endpoint, token_endpoint, registration_endpoint, iss_parameter_supported, "
         "cimd_supported, as_scopes_supported, resource_scopes, token_auth_methods, scope, "
         "challenged_scope, client_id, client_registration, client_auth_method, redirect_uri, "
         "expires_at, has_refresh, signed_in_at, refreshed_at, step_ups, last_error, updated_at")


def _row(r) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in ("as_scopes_supported", "resource_scopes", "token_auth_methods"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except ValueError:
                d[k] = None
    return d


async def load(pool, conn: dict) -> Optional[dict]:
    """The connection's sign-in state, or None when its server never asked
    for one (or the table predates v239)."""
    import asyncpg
    try:
        return _row(await pool.fetchrow(f"SELECT {_COLS} FROM mcp_oauth WHERE connection_id = $1",
                                        int(conn["id"])))
    except asyncpg.UndefinedTableError:
        return None


async def states_for(pool, conn_ids: list) -> dict[int, dict]:
    """{connection id: its sign-in state} for the Network app's list."""
    import asyncpg
    ids = [int(i) for i in conn_ids if i is not None]
    if not ids:
        return {}
    try:
        rows = await pool.fetch(f"SELECT {_COLS} FROM mcp_oauth WHERE connection_id = ANY($1::bigint[])",
                                ids)
    except asyncpg.UndefinedTableError:
        return {}
    return {int(r["connection_id"]): _row(r) for r in rows}


def summary(oauth: Optional[dict]) -> Optional[dict]:
    """What the Host and the Network app read about a sign-in: never a token,
    a client secret or the authorization server's own wording."""
    if not oauth:
        return None
    if oauth["status"] == "active":
        state = "more_access_needed" if oauth.get("challenged_scope") else "signed_in"
    else:
        state = "sign_in_needed"
    issuer = oauth.get("issuer") or ""
    exp = _aware(oauth.get("expires_at"))
    return {"state": state,
            "authorization_server": urlsplit(issuer).hostname if issuer else None,
            "scope": oauth.get("scope"),
            "more_scope": oauth.get("challenged_scope"),
            "expires_at": exp.isoformat() if exp else None,
            "renews": bool(oauth.get("has_refresh")),
            "registration": oauth.get("client_registration"),
            "step_ups": int(oauth.get("step_ups") or 0),
            "error": oauth.get("last_error")}


def capable_refusal(conn: dict) -> Optional[str]:
    """Why a connection cannot sign in with OAuth, or None when it can: an
    active remote connection that holds no key of its own."""
    if conn.get("app_id"):
        return "it is one of your installed apps, which needs no sign-in"
    if conn.get("key_name"):
        return (f"it is connected with your key {conn['key_name']}; disconnect it and connect it "
                "again with no key to sign in instead")
    if conn.get("status") != "active":
        return ("its egress rule is waiting for your approval in the Network app"
                if conn.get("status") == "draft" else "it is disconnected")
    return None


# ── the vault entries ───────────────────────────────────────────────────────

async def _load_secrets(pool, eid: int, conn: dict) -> dict:
    from server.vault import api as vault_api
    raw = await vault_api.open_secret(pool, entity_id=int(eid), name=refresh_secret_name(conn), sign_in=True)
    if not raw:
        return {}
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


async def _save_secrets(pool, eid: int, conn: dict, values: dict) -> None:
    from server.vault import api as vault_api
    keep = {k: v for k, v in values.items() if isinstance(v, str) and v}
    if not keep:
        await vault_api.revoke_secret(pool, entity_id=int(eid), name=refresh_secret_name(conn))
        return
    await vault_api.store_secret(pool, entity_id=int(eid), name=refresh_secret_name(conn),
                                 cred_type="mcp_oauth_refresh", plaintext=json.dumps(keep).encode("utf-8"),
                                 sign_in=True)


async def _drop_tokens(pool, eid: int, conn: dict, *, keep_client_secret: bool = True) -> None:
    from server.vault import api as vault_api
    await vault_api.revoke_secret(pool, entity_id=int(eid), name=access_secret_name(conn))
    sec = await _load_secrets(pool, eid, conn)
    await _save_secrets(pool, eid, conn,
                        {"client_secret": sec.get("client_secret")} if keep_client_secret else {})


# ── discovery ───────────────────────────────────────────────────────────────

async def _fetch_prm(server_url: str, challenge: Optional[dict]) -> tuple[str, dict]:
    """(url, document) of the server's Protected Resource Metadata: the URL its
    challenge named, else the well-known paths in order."""
    urls: list[str] = []
    named = _https_url((challenge or {}).get("resource_metadata"))
    if named:
        urls.append(named)
    urls += [u for u in prm_urls(server_url) if u not in urls]
    last = "nothing answered"
    for u in urls:
        try:
            status, body = await _http("GET", u, what="the server's resource metadata")
        except OAuthError as e:
            last = str(e)
            continue
        if status == 200 and isinstance(body, dict):
            return u, body
        last = f"{u} answered HTTP {status}"
    raise OAuthError(f"the server wants a sign-in but does not say where: no OAuth resource "
                     f"metadata ({last}). If it offers a key or token instead, connect it with that.")


async def _fetch_as(issuer: str) -> dict:
    """The authorization server's metadata, validated: its issuer is exactly
    `issuer`, it lists S256, and its endpoints are https."""
    last = "nothing answered"
    mismatch = None
    for u in as_metadata_urls(issuer):
        try:
            status, body = await _http("GET", u, what="the authorization server's metadata")
        except OAuthError as e:
            last = str(e)
            continue
        if status != 200 or not isinstance(body, dict):
            last = f"{u} answered HTTP {status}"
            continue
        if body.get("issuer") != issuer:
            # RFC 8414 3.3: a document whose issuer is not the one the URL was
            # built from MUST NOT be used. Said above any later miss: it is the
            # reason the person needs to hear.
            mismatch = f"{u} names another issuer than {issuer}, so it was not used"
            continue
        methods = body.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise OAuthError(f"the authorization server {urlsplit(issuer).hostname} does not support "
                             "PKCE with S256, which the MCP spec requires; this Host will not sign in "
                             "there")
        rtypes = body.get("response_types_supported")
        if isinstance(rtypes, list) and "code" not in rtypes:
            raise OAuthError("the authorization server does not offer the authorization code flow")
        if not _https_url(body.get("authorization_endpoint")) or not _https_url(body.get("token_endpoint")):
            raise OAuthError("the authorization server's endpoints are not https URLs")
        return body
    raise OAuthError(f"the authorization server {urlsplit(issuer).hostname} publishes no usable "
                     f"metadata ({mismatch or last})")


async def discover(pool, eid: int, conn: dict, challenge: Optional[dict]) -> dict:
    """Find the server's authorization server and keep what was found. A
    sign-in made with another authorization server is dropped: its tokens and
    its registration belong to that one (spec "Authorization Server Binding")."""
    prm_url, prm = await _fetch_prm(conn["url"], challenge)
    servers = [s for s in (prm.get("authorization_servers") or []) if _https_url(s)] \
        if isinstance(prm.get("authorization_servers"), list) else []
    if not servers:
        raise OAuthError("the server's resource metadata names no https authorization server")
    named = prm.get("resource")
    if named is not None and (not isinstance(named, str) or not _same_origin_prefix(named, conn["url"])):
        # Only a host name is repeated: this text reaches the Host, and the
        # document is the server's to fill with anything.
        try:
            where = urlsplit(named).hostname if isinstance(named, str) else None
        except ValueError:
            where = None
        if not (where and re.fullmatch(r"[a-z0-9.-]{1,253}", where)):
            where = "an address that is not this server's"
        raise OAuthError(f"the server's resource metadata is for another resource (on {where}), "
                         "not this server")
    # The `resource` sent with every request: the server's own identifier when
    # its metadata names one (the authorization server knows it by that), else
    # the canonical form of the endpoint. A bare origin goes without its slash.
    if isinstance(named, str):
        resource = named.rstrip("/") if urlsplit(named).path in ("", "/") else named
    else:
        resource = canonical_resource(conn["url"])
    issuer = servers[0]
    meta = await _fetch_as(issuer)
    # Under refresh()'s lock: a refresh already under way at the previous
    # authorization server finishes, and stores what it got, before this
    # reads the row and drops that server's tokens; one that starts after
    # finds the sign-in needed. Unlocked, a refresh answered after the drop
    # put the old server's tokens back on a row naming the new one, marked
    # active, and the next refresh sent the old refresh token to the new
    # server. The wait is bounded (_lock_bounded): nothing is written yet, so
    # a press that gives up loses nothing.
    async with _lock_bounded(conn):
        return await _keep_discovered(pool, eid, conn, challenge, prm_url, prm, resource, issuer, meta)


async def _keep_discovered(pool, eid: int, conn: dict, challenge: Optional[dict], prm_url: str, prm: dict,
                           resource: str, issuer: str, meta: dict) -> dict:
    """discover()'s write of what it found, under the connection's lock."""
    prev = await load(pool, conn)
    same_issuer = prev is not None and prev.get("issuer") == issuer
    challenged = " ".join(_scopes((challenge or {}).get("scope"))) or None
    vals = {
        "resource": resource, "resource_metadata_url": prm_url, "issuer": issuer,
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        "registration_endpoint": _https_url(meta.get("registration_endpoint")),
        "iss_parameter_supported": meta.get("authorization_response_iss_parameter_supported") is True,
        "cimd_supported": meta.get("client_id_metadata_document_supported") is True,
        "as_scopes_supported": json.dumps(_scopes(meta.get("scopes_supported"))),
        "resource_scopes": json.dumps(_scopes(prm.get("scopes_supported"))),
        "token_auth_methods": json.dumps([m for m in (meta.get("token_endpoint_auth_methods_supported") or [])
                                          if isinstance(m, str)][:10]),
    }
    if prev is None:
        await pool.execute(
            "INSERT INTO mcp_oauth (connection_id, entity_id, status, resource, resource_metadata_url, "
            "issuer, authorization_endpoint, token_endpoint, registration_endpoint, "
            "iss_parameter_supported, cimd_supported, as_scopes_supported, resource_scopes, "
            "token_auth_methods, challenged_scope) VALUES ($1, $2, 'sign_in_needed', $3, $4, $5, $6, "
            "$7, $8, $9, $10, $11::jsonb, $12::jsonb, $13::jsonb, $14) "
            "ON CONFLICT (connection_id) DO NOTHING",
            int(conn["id"]), int(eid), vals["resource"], vals["resource_metadata_url"], issuer,
            vals["authorization_endpoint"], vals["token_endpoint"], vals["registration_endpoint"],
            vals["iss_parameter_supported"], vals["cimd_supported"], vals["as_scopes_supported"],
            vals["resource_scopes"], vals["token_auth_methods"], challenged)
    else:
        if not same_issuer:
            await _drop_tokens(pool, eid, conn, keep_client_secret=False)
        await pool.execute(
            "UPDATE mcp_oauth SET resource = $2, resource_metadata_url = $3, issuer = $4, "
            "authorization_endpoint = $5, token_endpoint = $6, registration_endpoint = $7, "
            "iss_parameter_supported = $8, cimd_supported = $9, as_scopes_supported = $10::jsonb, "
            "resource_scopes = $11::jsonb, token_auth_methods = $12::jsonb, "
            "challenged_scope = COALESCE($13, challenged_scope), updated_at = now(), "
            "status = CASE WHEN $14 THEN status ELSE 'sign_in_needed' END, "
            "client_id = CASE WHEN $14 OR client_registration = 'cimd' THEN client_id ELSE NULL END, "
            "client_registration = CASE WHEN $14 OR client_registration = 'cimd' "
            "    THEN client_registration ELSE NULL END, "
            "client_auth_method = CASE WHEN $14 THEN client_auth_method ELSE NULL END, "
            "has_refresh = CASE WHEN $14 THEN has_refresh ELSE FALSE END "
            "WHERE connection_id = $1",
            int(conn["id"]), vals["resource"], vals["resource_metadata_url"], issuer,
            vals["authorization_endpoint"], vals["token_endpoint"], vals["registration_endpoint"],
            vals["iss_parameter_supported"], vals["cimd_supported"], vals["as_scopes_supported"],
            vals["resource_scopes"], vals["token_auth_methods"], challenged, same_issuer)
    return await load(pool, conn)


async def probe_challenge(conn: dict) -> dict:
    """The server's Bearer challenge to a request with no token: what a
    sign-in started before the server ever asked needs. OAuthError when the
    server answers without one."""
    from server.mcp_connections import cache_key, public_host_refusal
    why = await public_host_refusal(urlsplit(conn["url"]).hostname or "")
    if why:
        raise OAuthError(f"{conn['name']} was not contacted: {why}")
    try:
        await mcp_client.request(conn["url"], "tools/list", {}, headers={}, era=conn.get("era"),
                                 cache_key=f"probe:{cache_key(conn)}")
    except mcp_client.McpError as e:
        if e.kind == "auth":
            return bearer_challenge(e.www_authenticate) or {}
        raise OAuthError(f"{conn['name']} could not be asked how to sign in: {e}") from None
    finally:
        mcp_client.forget_session(f"probe:{cache_key(conn)}")
    raise OAuthError(f"{conn['name']} answered without a sign-in, so it needs none")


# ── registration ────────────────────────────────────────────────────────────

_CIMD_CACHE: dict[str, tuple[float, Any]] = {}
_CIMD_TTL_S = 3600
# The document is a small static file. A Host that cannot read it in a few
# seconds registers itself instead, and does not try it again for a while: on
# a network that drops the traffic, every sign-in would otherwise wait out the
# whole timeout first.
_CIMD_TIMEOUT_S = 5.0
_CIMD_MISS_TTL_S = 300


def cimd_url() -> Optional[str]:
    """The Client ID Metadata Document this Host would sign in with: the one
    CSERIES_OAUTH_CLIENT_METADATA_URL names, else C-Series's own. None when the
    variable is set empty, or names no https URL with a path free of "." and
    ".." segments (the spec's form)."""
    url = os.environ.get(CIMD_URL_ENV, CIMD_URL_DEFAULT).strip()
    p = urlsplit(url) if url else None
    if not url or not _https_url(url) or not p or p.path in ("", "/"):
        return None
    if any(unquote(seg) in (".", "..") for seg in p.path.split("/")):
        return None
    return url


async def _cimd_client_id(redirect: str, *, miss_before: Optional[float] = None) -> Optional[str]:
    """This Host's Client ID Metadata Document URL, when the document is what
    an authorization server will accept for this redirect URI: its client_id
    is its own URL and it lists the redirect URI. None otherwise (the next way
    to register is tried). A remembered miss recorded before `miss_before`
    (a time.monotonic() value) is not trusted, and the document is read again."""
    url = cimd_url()
    if not url:
        return None
    hit = _CIMD_CACHE.get(url)
    if hit and hit[1] is None and miss_before is not None and hit[0] < miss_before:
        hit = None
    if hit and time.monotonic() - hit[0] < (_CIMD_TTL_S if hit[1] is not None else _CIMD_MISS_TTL_S):
        doc = hit[1]
    else:
        try:
            status, doc = await _http("GET", url, what="this Host's client metadata document",
                                      timeout_s=_CIMD_TIMEOUT_S)
        except OAuthError as e:
            log.info("client metadata document %s not used: %s", url, e)
            status, doc = None, None
        if status != 200:
            doc = None
        _CIMD_CACHE[url] = (time.monotonic(), doc)
    if not isinstance(doc, dict) or doc.get("client_id") != url:
        return None
    if redirect not in (doc.get("redirect_uris") or []):
        return None
    return url


def _auth_method_for(oauth: dict, has_secret: bool, offered: Optional[str] = None) -> str:
    methods = oauth.get("token_auth_methods") or []
    if not has_secret:
        return "none"
    if offered in ("client_secret_basic", "client_secret_post"):
        return offered
    if "client_secret_basic" in methods or not methods:
        return "client_secret_basic"
    if "client_secret_post" in methods:
        return "client_secret_post"
    return "client_secret_basic"


async def _register(pool, eid: int, conn: dict, oauth: dict, redirect: str, *,
                    client_id: Optional[str] = None, client_secret: Optional[str] = None) -> dict:
    """This Host's client at the authorization server, registering it when
    there is none for this issuer and redirect URI yet. Returns the row."""
    cid = str(client_id or "").strip()
    if cid:
        if len(cid) > 512 or any(ord(c) < 0x21 or ord(c) > 0x7E for c in cid):
            raise OAuthError("the client ID is not a plain string")
        secret = str(client_secret or "").strip() or None
        sec = await _load_secrets(pool, eid, conn)
        await _save_secrets(pool, eid, conn, {"refresh_token": sec.get("refresh_token"),
                                              "client_secret": secret})
        method = _auth_method_for(oauth, bool(secret))
        await pool.execute(
            "UPDATE mcp_oauth SET client_id = $2, client_registration = 'pre', client_auth_method = $3, "
            "redirect_uri = $4, updated_at = now() WHERE connection_id = $1",
            int(conn["id"]), cid, method, redirect)
        return await load(pool, conn)
    # A miss is remembered so that a sign-in with another way to register does
    # not wait on the document each time. Where the authorization server offers
    # no registration, the alternative is asking the person to register a
    # client by hand, so a miss from an earlier sign-in is read again; one from
    # this sign-in is not.
    miss_before = None if oauth.get("registration_endpoint") else time.monotonic()
    if oauth.get("client_id") and (oauth.get("client_registration") == "cimd"
                                   or oauth.get("redirect_uri") == redirect):
        if oauth.get("client_registration") != "cimd" \
                or await _cimd_client_id(redirect, miss_before=miss_before) == oauth["client_id"]:
            return oauth
    if oauth.get("cimd_supported"):
        url = await _cimd_client_id(redirect, miss_before=miss_before)
        if url:
            await pool.execute(
                "UPDATE mcp_oauth SET client_id = $2, client_registration = 'cimd', "
                "client_auth_method = 'none', redirect_uri = $3, updated_at = now() "
                "WHERE connection_id = $1", int(conn["id"]), url, redirect)
            return await load(pool, conn)
    reg = oauth.get("registration_endpoint")
    if reg:
        loopback = redirect.startswith("http://127.0.0.1")
        status, body = await _http("POST", reg, what="the authorization server's registration", json_body={
            "client_name": CLIENT_NAME, "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native" if loopback else "web"})
        if status not in (200, 201) or not isinstance(body, dict) or not isinstance(body.get("client_id"), str) \
                or not body["client_id"]:
            code = _error_code((body or {}).get("error")) if isinstance(body, dict) else None
            raise OAuthError(f"the authorization server refused to register this Host "
                             f"({code or f'HTTP {status}'})"
                             + (": it may not accept a redirect to " + redirect if code and "redirect" in code
                                else ""))
        secret = body.get("client_secret") if isinstance(body.get("client_secret"), str) else None
        sec = await _load_secrets(pool, eid, conn)
        await _save_secrets(pool, eid, conn, {"refresh_token": sec.get("refresh_token"),
                                              "client_secret": secret})
        offered = body.get("token_endpoint_auth_method") if isinstance(body.get("token_endpoint_auth_method"), str) \
            else None
        method = _auth_method_for(oauth, bool(secret), offered)
        await pool.execute(
            "UPDATE mcp_oauth SET client_id = $2, client_registration = 'dcr', client_auth_method = $3, "
            "redirect_uri = $4, updated_at = now() WHERE connection_id = $1",
            int(conn["id"]), body["client_id"][:512], method, redirect)
        return await load(pool, conn)
    raise NeedsClient(
        f"{urlsplit(oauth['issuer']).hostname} does not let this Host register itself. Register an "
        f"OAuth client there with the redirect URI {redirect}, then enter its client ID (and secret, if "
        "it gave one) on the server's card in the Network app.",
        issuer=oauth["issuer"], redirect_uri=redirect)


# ── the sign-in ──────────────────────────────────────────────────────────────

def _scope_for(oauth: dict) -> Optional[str]:
    """The scope to ask for (spec "Scope Selection Strategy"): what the
    server's challenge named, with what was asked for before (a step-up keeps
    what it had), else the resource's scopes_supported, else none at all.
    offline_access is added when the authorization server lists it, so a
    refresh token can be issued."""
    asked = _union(_scopes(oauth.get("scope")), _scopes(oauth.get("challenged_scope")))
    if not asked:
        asked = _scopes(oauth.get("resource_scopes"))
    if asked and "offline_access" in (oauth.get("as_scopes_supported") or []) and "offline_access" not in asked:
        asked.append("offline_access")
    return " ".join(asked) or None


async def begin(pool, eid: int, conn: dict, *, client_id: Optional[str] = None,
                client_secret: Optional[str] = None) -> dict:
    """Start a sign-in: {authorize_url, authorization_server, redirect_uri}.
    The person's browser opens authorize_url."""
    why = capable_refusal(conn)
    if why:
        raise OAuthError(f"{conn['name']} cannot sign in: {why}")
    oauth = await load(pool, conn)
    # Discovery runs again at every sign-in. A server that moves to another
    # authorization server says so only in its resource metadata, and the old
    # one's issuer, endpoints and registration must not be used for it (spec
    # "Authorization Server Binding"). The metadata URL found last time is
    # tried first, then the well-known paths.
    if oauth is None or not oauth.get("issuer"):
        challenge = await probe_challenge(conn)
    else:
        challenge = {"resource_metadata": oauth.get("resource_metadata_url")}
    oauth = await discover(pool, eid, conn, challenge)
    redirect = redirect_uri()
    oauth = await _register(pool, eid, conn, oauth, redirect, client_id=client_id,
                            client_secret=client_secret)
    scope = _scope_for(oauth)
    verifier = _b64url(_secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(_secrets.token_bytes(32))
    await pool.execute("DELETE FROM mcp_oauth_pending WHERE expires_at < now()")
    await pool.execute(
        "INSERT INTO mcp_oauth_pending (state_hash, connection_id, entity_id, issuer, "
        "iss_parameter_supported, code_verifier, redirect_uri, client_id, scope, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now() + make_interval(secs => $10))",
        _hash_state(state), int(conn["id"]), int(eid), oauth["issuer"],
        bool(oauth.get("iss_parameter_supported")), verifier, redirect, oauth["client_id"], scope,
        float(PENDING_TTL_S))
    params = {"response_type": "code", "client_id": oauth["client_id"], "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
              "resource": oauth["resource"]}
    if scope:
        params["scope"] = scope
    ae = oauth["authorization_endpoint"]
    url = ae + ("&" if urlsplit(ae).query else "?") + urlencode(params)
    return {"authorize_url": url, "authorization_server": urlsplit(oauth["issuer"]).hostname,
            "redirect_uri": redirect, "server": conn["name"], "scope": scope}


async def _token_request(pool, eid: int, conn: dict, oauth: dict, form: dict) -> dict:
    """POST to the token endpoint, authenticating the client the way it was
    registered. The validated token response, or _TokenRefused/OAuthError."""
    sec = await _load_secrets(pool, eid, conn)
    secret = sec.get("client_secret")
    method = oauth.get("client_auth_method") or "none"
    headers: dict = {}
    form = dict(form)
    if secret and method == "client_secret_basic":
        # RFC 6749 2.3.1: both parts form-urlencoded before the Basic encoding.
        pair = f"{quote(str(form.get('client_id') or ''), safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        form.pop("client_id", None)
    elif secret and method == "client_secret_post":
        form["client_secret"] = secret
    status, body = await _http("POST", oauth["token_endpoint"], form=form, headers=headers,
                               what="the authorization server's token endpoint")
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("access_token"), str) \
            or not body["access_token"]:
        code = _error_code(body.get("error")) if isinstance(body, dict) else None
        raise _TokenRefused(f"the authorization server refused the token request ({code or f'HTTP {status}'})",
                            error=code, status=status)
    if str(body.get("token_type") or "").lower() != "bearer":
        raise OAuthError(f"the authorization server issued a {str(body.get('token_type'))[:20]!r} token; "
                         "this client sends bearer tokens only")
    return body


async def _store_tokens(pool, eid: int, conn: dict, body: dict, *, asked_scope: Optional[str],
                        signed_in: bool) -> None:
    from server.vault import api as vault_api
    await vault_api.store_secret(pool, entity_id=int(eid), name=access_secret_name(conn),
                                 cred_type="mcp_oauth_access", plaintext=body["access_token"].encode("utf-8"),
                                 sign_in=True)
    sec = await _load_secrets(pool, eid, conn)
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    # A refresh that issues no new refresh token keeps the old one (RFC 6749 6).
    keep = refresh or (None if signed_in else sec.get("refresh_token"))
    await _save_secrets(pool, eid, conn, {"refresh_token": keep, "client_secret": sec.get("client_secret")})
    expires_in = body.get("expires_in")
    try:
        secs = int(expires_in) if expires_in is not None and not isinstance(expires_in, bool) else None
    except (TypeError, ValueError):
        secs = None
    granted = " ".join(_scopes(body.get("scope"))) or asked_scope
    if signed_in:
        # A step-up sign-in (one the server asked for, challenged_scope set)
        # keeps the step-up count, so on_auth_error can tell a server that no
        # sign-in satisfies; any other sign-in starts it again. The CASE reads
        # the row as it was before this UPDATE.
        await pool.execute(
            "UPDATE mcp_oauth SET status = 'active', scope = $2, challenged_scope = NULL, "
            "expires_at = CASE WHEN $3::int IS NULL THEN NULL ELSE now() + make_interval(secs => $3::int) END, "
            "has_refresh = $4, signed_in_at = now(), refreshed_at = NULL, "
            "step_ups = CASE WHEN challenged_scope IS NULL THEN 0 ELSE step_ups END, last_error = NULL, "
            "updated_at = now() WHERE connection_id = $1",
            int(conn["id"]), granted, secs if secs and secs > 0 else None, bool(keep))
    else:
        await pool.execute(
            "UPDATE mcp_oauth SET status = 'active', scope = COALESCE($2, scope), "
            "expires_at = CASE WHEN $3::int IS NULL THEN NULL ELSE now() + make_interval(secs => $3::int) END, "
            "has_refresh = $4, refreshed_at = now(), last_error = NULL, updated_at = now() "
            "WHERE connection_id = $1",
            int(conn["id"]), " ".join(_scopes(body.get("scope"))) or None,
            secs if secs and secs > 0 else None, bool(keep))


async def finish(pool, eid: int, params: dict) -> dict:
    """The callback: {ok, server} or {ok: False, error, detail}. Takes the
    pending sign-in by its state's hash (used once, ten minutes), applies the
    RFC 9207 check before acting on anything in the answer, then trades the
    code with its PKCE verifier."""
    state = str(params.get("state") or "")
    if not state or len(state) > 512:
        return {"ok": False, "error": "no_state", "detail": "the answer carries no sign-in state"}
    pend = await pool.fetchrow(
        "DELETE FROM mcp_oauth_pending WHERE state_hash = $1 AND entity_id = $2 "
        "RETURNING connection_id, issuer, iss_parameter_supported, code_verifier, redirect_uri, "
        "client_id, scope, expires_at > now() AS live", _hash_state(state), int(eid))
    if pend is None or not pend["live"]:
        return {"ok": False, "error": "unknown_state",
                "detail": "this sign-in was finished already or ran out; start it again from the Network app"}
    iss = params.get("iss")
    if iss is not None:
        if str(iss) != pend["issuer"]:
            return {"ok": False, "error": "issuer_mismatch",
                    "detail": "the answer came from another authorization server than the one this "
                              "sign-in went to, so it was ignored"}
    elif pend["iss_parameter_supported"]:
        return {"ok": False, "error": "issuer_missing",
                "detail": "the authorization server left its issuer out of the answer, so it was ignored"}
    from server.mcp_connections import _row as _conn_row
    from server.mcp_connections import _COLS as _CONN_COLS
    conn = _conn_row(await pool.fetchrow(f"SELECT {_CONN_COLS} FROM mcp_connections WHERE id = $1 "
                                         "AND entity_id = $2", int(pend["connection_id"]), int(eid)))
    if params.get("error") is not None:
        code = _error_code(params.get("error")) or "error"
        desc = str(params.get("error_description") or "")[:300]
        if conn is not None:
            await pool.execute("UPDATE mcp_oauth SET last_error = $2, updated_at = now() "
                               "WHERE connection_id = $1", int(conn["id"]), f"sign-in refused ({code})")
        return {"ok": False, "error": code, "detail": desc or "the authorization server refused the sign-in",
                "server": conn["name"] if conn else None}
    code = str(params.get("code") or "")
    if not code or len(code) > 4096:
        return {"ok": False, "error": "no_code", "detail": "the answer carries no authorization code"}
    if conn is None or capable_refusal(conn):
        return {"ok": False, "error": "not_connected",
                "detail": "the server was disconnected during the sign-in"}
    oauth = await load(pool, conn)
    if oauth is None or oauth.get("issuer") != pend["issuer"] or oauth.get("client_id") != pend["client_id"]:
        return {"ok": False, "error": "server_changed", "server": conn["name"],
                "detail": "the server's authorization server changed during the sign-in; start it again"}
    try:
        body = await _token_request(pool, eid, conn, oauth, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": pend["redirect_uri"],
            "client_id": pend["client_id"], "code_verifier": pend["code_verifier"],
            "resource": oauth["resource"]})
    except OAuthError as e:
        await pool.execute("UPDATE mcp_oauth SET last_error = $2, updated_at = now() WHERE connection_id = $1",
                           int(conn["id"]), str(e)[:300])
        return {"ok": False, "error": getattr(e, "error", None) or "token_refused", "detail": str(e),
                "server": conn["name"]}
    # Stored under refresh()'s lock. A refresh of the sign-in this one
    # replaces, answered after it, would otherwise write its tokens (the old
    # grant's, its scope without a step-up) over these, or end this sign-in
    # when its old refresh token is refused; it finishes first now, and this
    # replaces what it stored. The row is read again under the lock: a move to
    # another authorization server while the code was traded keeps nothing.
    # The code is spent by now, so this wait has no timeout of its own (one
    # that gave up would lose the sign-in): it is bounded by what holds the
    # lock, at most one token request (refresh: one in flight, the ones
    # queued behind a failure give up) and the reads and writes around it.
    async with _LOCKS.setdefault(int(conn["id"]), asyncio.Lock()):
        now = await load(pool, conn)
        if now is None or now.get("issuer") != pend["issuer"] or now.get("client_id") != pend["client_id"]:
            return {"ok": False, "error": "server_changed", "server": conn["name"],
                    "detail": "the server's authorization server changed during the sign-in; start it again"}
        await _store_tokens(pool, eid, conn, body, asked_scope=pend["scope"], signed_in=True)
    from server import mcp_connections
    mcp_client.forget_session(mcp_connections.cache_key(conn))
    mcp_connections.schedule_fetch(pool, eid, conn["name"], delay=0)
    return {"ok": True, "server": conn["name"]}


# ── using the token ─────────────────────────────────────────────────────────

_LOCKS: dict[int, asyncio.Lock] = {}
# The last renewal on each connection that failed and left the sign-in as it
# was (a timeout, a 5xx, a 429, the gate, or its caller gone): (when the token
# it was for was issued, its place in _SEQ, the error). A refresh queued
# behind it returns that error instead of asking again (refresh).
_FAILED: dict[int, tuple[Optional[_dt.datetime], int, OAuthError]] = {}
_SEQ = itertools.count(1)


class _Busy(OAuthError):
    """The connection's lock was not free within one request and a margin."""


@contextlib.asynccontextmanager
async def _lock_bounded(conn: dict):
    """The connection's lock, waited for no longer than one request and
    LOCK_MARGIN_S; _Busy, saying to try again in a moment, after that."""
    lock = _LOCKS.setdefault(int(conn["id"]), asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), HTTP_TIMEOUT_S + LOCK_MARGIN_S)
    except TimeoutError:
        raise _Busy(f"{conn['name']} is still renewing its sign-in: try again in a moment") from None
    try:
        yield
    finally:
        lock.release()


def _again(e: OAuthError) -> OAuthError:
    """A copy of a recorded renewal failure, for a refresh queued behind it."""
    if isinstance(e, _TokenRefused):
        return _TokenRefused(str(e), error=e.error, status=e.status)
    return OAuthError(str(e))


async def mark_sign_in_needed(pool, eid: int, conn: dict, reason: str, *,
                              challenged: Optional[str] = None) -> None:
    await pool.execute(
        "UPDATE mcp_oauth SET status = 'sign_in_needed', has_refresh = FALSE, expires_at = NULL, "
        "last_error = $2, challenged_scope = COALESCE($3, challenged_scope), updated_at = now() "
        "WHERE connection_id = $1", int(conn["id"]), reason[:300], challenged)
    await _drop_tokens(pool, eid, conn)


def _issued_at(oauth: dict) -> Optional[_dt.datetime]:
    """When the token in the vault was issued: the last refresh, else the sign-in."""
    return _aware(oauth.get("refreshed_at")) or _aware(oauth.get("signed_in_at"))


async def _end_if_current(pool, eid: int, conn: dict, seen: Optional[_dt.datetime], reason: str, *,
                          challenged: Optional[str] = None) -> bool:
    """End the sign-in a refused token belonged to. `seen` is when that token
    was issued (_issued_at, as access_secret returned it; None for a request
    sent with no token). True when the person must sign in now: ended here, or
    ended already. False when a refresh or a new sign-in replaced that token
    while the refused request was on its way, and that sign-in is kept: a
    late answer about an old token does not end a newer one. Under the lock
    refresh(), finish() and discover() write under, so the row read here is
    the one the tokens in the vault belong to."""
    async with _LOCKS.setdefault(int(conn["id"]), asyncio.Lock()):
        oauth = await load(pool, conn)
        if oauth is None or oauth["status"] != "active":
            return True
        if _issued_at(oauth) != _aware(seen):
            return False
        await mark_sign_in_needed(pool, eid, conn, reason, challenged=challenged)
        return True


async def refresh(pool, eid: int, conn: dict, *, seen: Optional[_dt.datetime]) -> None:
    """Trade the refresh token for a new access token. One at a time per
    connection: a refresh token is rotated (OAuth 2.1 4.3.1), so a second
    refresh with the old one would be refused and end the sign-in. `seen` is
    when the token the caller found was issued (_issued_at); a caller that
    waited while another refreshed, or while the person signed in again, uses
    that newer token instead of refreshing a second time.

    A refresh that waited while the one ahead of it failed for the same token
    (_FAILED) returns that failure without a request of its own, so the lock
    is held for one token request (at most HTTP_TIMEOUT_S, the name lookup
    included) however many calls are queued: each one waiting through its own
    request made a Sign in press behind them wait for all of them."""
    cid = int(conn["id"])
    asked = next(_SEQ)
    lock = _LOCKS.setdefault(cid, asyncio.Lock())
    async with lock:
        oauth = await load(pool, conn)
        if oauth is None or oauth["status"] != "active":
            raise SignInNeeded(f"{conn['name']} needs you to sign in again")
        issued = _issued_at(oauth)
        if issued != _aware(seen):
            return
        last = _FAILED.get(cid)
        if last is not None and last[0] == issued and last[1] > asked:
            raise _again(last[2])
        sec = await _load_secrets(pool, eid, conn)
        rt = sec.get("refresh_token")
        if not rt:
            await mark_sign_in_needed(pool, eid, conn, "the sign-in ran out and gave no way to renew it")
            raise SignInNeeded(f"{conn['name']}'s sign-in ran out")
        try:
            body = await _token_request(pool, eid, conn, oauth, {
                "grant_type": "refresh_token", "refresh_token": rt, "client_id": oauth["client_id"],
                "resource": oauth["resource"]})
        except _TokenRefused as e:
            # A 4xx is the authorization server's answer about this refresh
            # token (RFC 6749 5.2; RFC 8707 adds invalid_target), and asking
            # again gets the same answer; a 5xx or a 429 passes, and the
            # sign-in is kept for the next try.
            if e.error in ("invalid_grant", "invalid_client", "unauthorized_client") \
                    or (400 <= e.status < 500 and e.status != 429):
                _FAILED.pop(cid, None)
                await mark_sign_in_needed(pool, eid, conn, f"renewing the sign-in was refused ({e.error or e.status})")
                raise SignInNeeded(f"{conn['name']}'s sign-in can no longer be renewed") from None
            _FAILED[cid] = (issued, next(_SEQ), e)
            raise
        except OAuthError as e:
            _FAILED[cid] = (issued, next(_SEQ), e)
            raise
        except asyncio.CancelledError:
            # No answer came: the ones queued behind this give up too, rather
            # than each waiting through a request of its own.
            _FAILED[cid] = (issued, next(_SEQ), OAuthError(
                f"renewing {conn['name']}'s sign-in stopped before the authorization server answered"))
            raise
        _FAILED.pop(cid, None)
        await _store_tokens(pool, eid, conn, body, asked_scope=None, signed_in=False)


async def access_secret(pool, eid: int, conn: dict, *, after_401: bool = False,
                        seen: Optional[_dt.datetime] = None) -> tuple[Optional[str], Optional[_dt.datetime]]:
    """(the vault name of a usable access token for this connection, when that
    token was issued), refreshing it first when it is about to expire, or
    after the server refused the token issued at `seen` (after_401). (None,
    None) when the server never asked for a sign-in. SignInNeeded when one is
    needed now. The time goes back with a refusal of this token
    (on_auth_error), so that a refusal ends only the sign-in it was about."""
    oauth = await load(pool, conn)
    if oauth is None:
        return None, None
    if oauth["status"] != "active":
        raise SignInNeeded(f"{conn['name']} needs you to sign in")
    issued = _issued_at(oauth)
    if after_401 and issued != _aware(seen):
        # The refused token was replaced while that request was on its way
        # (another call's refresh, or the person signed in again): the retry
        # takes the one there now.
        return access_secret_name(conn), issued
    exp = _aware(oauth.get("expires_at"))
    soon = exp is not None and (exp - _now()).total_seconds() <= REFRESH_EARLY_S
    if after_401 or soon:
        if oauth.get("has_refresh"):
            await refresh(pool, eid, conn, seen=issued)
            now = await load(pool, conn)
            return access_secret_name(conn), _issued_at(now) if now else None
        if after_401 or (exp is not None and exp <= _now()):
            if await _end_if_current(pool, eid, conn, issued, "the server no longer accepts the sign-in"):
                raise SignInNeeded(f"{conn['name']} needs you to sign in again")
            now = await load(pool, conn)
            return access_secret_name(conn), _issued_at(now) if now else None
    return access_secret_name(conn), issued


async def on_auth_error(pool, eid: int, conn: dict, err: "mcp_client.McpError", *,
                        seen: Optional[_dt.datetime] = None) -> str:
    """Record what a 401 or 403 means for the sign-in, and say what the person
    does next (the Host passes it on; it never asks for a password or token).
    `seen` is when the refused token was issued (access_secret's second
    value; None for a request sent with no token): a refusal of a token that
    a refresh or a new sign-in has replaced since leaves that sign-in as it is."""
    ch = bearer_challenge(err.www_authenticate) or {}
    oauth = await load(pool, conn)
    where = f"the Sign in button on {conn['name']}'s card in the Network app (NAC -> Egress)"
    replaced = (f"{conn['name']} refused a token that has been replaced since the call was sent (the sign-in "
                "was renewed, or the person signed in again): try the call again.")
    if err.http_status == 403:
        more_scopes = _scopes(ch.get("scope"))
        more = " ".join(more_scopes)
        if oauth is None or oauth["status"] != "active":
            return f"{conn['name']} refused the call (403) and needs you to sign in: {where}."
        # Read and written under the lock finish() stores a sign-in under, so a
        # step-up sign-in that finished while this call was on its way is not
        # counted as refused, and does not show as needing more access.
        async with _LOCKS.setdefault(int(conn["id"]), asyncio.Lock()):
            oauth = await load(pool, conn)
            if oauth is None or oauth["status"] != "active":
                return f"{conn['name']} refused the call (403) and needs you to sign in: {where}."
            if _issued_at(oauth) != _aware(seen):
                return replaced
            merged = " ".join(_union(_scopes(oauth.get("challenged_scope")), more_scopes)) or None
            # step_ups counts the sign-ins that already asked for what the
            # server names and still did not satisfy it. Counted once per
            # sign-in (a second refusal before the person signs in again adds
            # nothing); a challenge naming a scope this sign-in never asked
            # for is a new step-up and starts the count again.
            steps = int(oauth.get("step_ups") or 0)
            if not oauth.get("challenged_scope"):
                steps = steps + 1 if set(more_scopes) <= set(_scopes(oauth.get("scope"))) else 0
            await pool.execute("UPDATE mcp_oauth SET challenged_scope = $2, step_ups = $3, updated_at = now() "
                               "WHERE connection_id = $1", int(conn["id"]), merged, steps)
        if steps >= MAX_STEP_UPS:
            return (f"{conn['name']} still refuses this ({more or 'no scope named'}) after {steps} sign-ins "
                    "that asked for that access; the account you signed in with may not be allowed to do "
                    "this.")
        return (f"{conn['name']} needs more access for this ({more or 'it named no scope'}): ask the person "
                f"to sign in again with {where}. The rest of its tools keep working meanwhile.")
    # 401: no sign-in yet, or the one there is no longer accepted.
    named = _https_url(ch.get("resource_metadata"))
    if oauth is None or not oauth.get("issuer") or (named and named != oauth.get("resource_metadata_url")):
        try:
            oauth = await discover(pool, eid, conn, ch)
        except _Busy:
            return (f"{conn['name']} refused the call (401) while its sign-in was being renewed: try the "
                    "call again in a moment.")
        except OAuthError as e:
            return f"{conn['name']} wants a sign-in, but it cannot be done from here: {e}"
    if oauth["status"] == "active":
        # Only the sign-in the refused token belonged to ends: a reply that
        # was on its way while the person signed in again is about the old one.
        if not await _end_if_current(pool, eid, conn, seen, "the server no longer accepts the sign-in",
                                     challenged=" ".join(_scopes(ch.get("scope"))) or None):
            return replaced
    elif ch.get("scope"):
        await pool.execute("UPDATE mcp_oauth SET challenged_scope = $2, updated_at = now() "
                           "WHERE connection_id = $1", int(conn["id"]), " ".join(_scopes(ch.get("scope"))))
    return (f"{conn['name']} needs the person to sign in: ask them to press {where}. "
            "Never ask for their password or a token in chat.")


async def sign_out(pool, eid: int, conn: dict) -> None:
    """Forget this connection's tokens; the server needs a new sign-in."""
    await mark_sign_in_needed(pool, eid, conn, "signed out")


async def forget(pool, eid: int, conn: dict) -> None:
    """Everything about a connection's sign-in, for a disconnect."""
    import asyncpg
    from server.vault import api as vault_api
    try:
        await pool.execute("DELETE FROM mcp_oauth_pending WHERE connection_id = $1", int(conn["id"]))
        await pool.execute("DELETE FROM mcp_oauth WHERE connection_id = $1", int(conn["id"]))
    except asyncpg.UndefinedTableError:
        return
    for name in (access_secret_name(conn), refresh_secret_name(conn)):
        await vault_api.revoke_secret(pool, entity_id=int(eid), name=name)


async def sweep(pool, eid: int) -> int:
    """Revoke the token entries of connections that are gone (a rule removed
    in the Network app revokes its connection inside that transaction, where
    the vault cannot be reached). Returns how many were revoked."""
    import asyncpg
    from server.vault import api as vault_api
    names = [r.get("logical_name") for r in await vault_api.list_secrets(pool, entity_id=int(eid))]
    ours = [n for n in names if isinstance(n, str) and is_reserved_key_name(n)]
    if not ours:
        return 0
    try:
        live = {int(r["id"]) for r in await pool.fetch(
            "SELECT id FROM mcp_connections WHERE entity_id = $1 AND status <> 'revoked'", int(eid))}
    except asyncpg.UndefinedTableError:
        return 0
    n = 0
    for name in ours:
        try:
            cid = int(name.rsplit(":", 1)[-1])
        except ValueError:
            continue
        if cid not in live:
            await vault_api.revoke_secret(pool, entity_id=int(eid), name=name)
            n += 1
    return n


__all__ = [
    "CALLBACK_PATH", "SECRET_PREFIX", "CIMD_URL_ENV", "CIMD_URL_DEFAULT", "OAuthError", "SignInNeeded",
    "NeedsClient", "parse_challenges", "bearer_challenge", "canonical_resource", "prm_urls", "as_metadata_urls",
    "redirect_uri", "cimd_url", "load", "states_for", "summary", "capable_refusal", "discover", "probe_challenge",
    "begin", "finish", "refresh", "access_secret", "on_auth_error", "sign_out", "forget", "sweep",
    "access_secret_name", "refresh_secret_name", "is_reserved_key_name", "mark_sign_in_needed",
]
