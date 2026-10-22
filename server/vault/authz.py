"""
Credential vault — the authorization / broker layer.

This is the module the rest of server/vault/ (api.py, store.py, routes/vault.py)
references as the place a credential is USED, so the raw secret bytes NEVER reach
the agent / model / chat-tool layer. The agent asks the broker to *do something*
with a credential; the broker opens the sealed envelope server-side, performs the
action, and returns ONLY the result.

Two paths:

  use_credential(...)  — SAME-OWNER use. The host uses its OWN sealed credential
      (api key / token / signing key) to perform an authorized action. The broker
      opens the envelope IN-POD (the host owns the key), performs the action
      (an authenticated HTTP call with the secret injected server-side, or a
      signature over a challenge), returns ONLY the result, and writes a
      vault_audit row. The caller never sees the secret bytes.

  resolve_use(...)     — CROSS-IDENTITY use (one identity using ANOTHER's
      credential). DENY-BY-DEFAULT: requires a live bridge-signed permission slip
      (Ed25519, offline-verified via server/peer/slip_verify against PeerTrust's
      pinned keys + synced CRL). The revocation view must be fresh (PeerTrust.ready)
      or we refuse (fail closed). Cross-identity NEVER returns raw bytes either.

Reveal/export of the raw secret TO THE USER is a separate, TOTP-gated path in
server/routes/vault.py — deliberately not here (that path is the only one that
ever returns bytes, and only to the user who owns the key).
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import asyncpg

from server.peer.slip_verify import verify_slip
from server.vault import api as vault_api
from server.vault import destination, identity, store

log = logging.getLogger("privatae.vault.authz")

# Cap brokered HTTP response bodies returned to the caller (bytes of text).
_MAX_BODY = 64 * 1024

# The browser control API. Two shapes, and the broker must not care which:
#
#   in-pod sidecar  -> pod loopback (both containers share the netns)
#   on-demand harness -> its own pod, reached at the harness Service
#
# Loopback stays the fallback because it is what an entity running the old
# always-on sidecar still uses. _browser_base_url() prefers the harness
# Service whenever the Browser app is awake as one, so a single entity can
# move between the two shapes without this module being reconfigured.
_BROWSER_CONTROL_URL = os.environ.get(
    "BROWSER_CONTROL_URL", "http://127.0.0.1:8830").rstrip("/")

_BROWSER_APP_ID = "browser"


def _browser_base_url() -> str:
    """Where the browser control API actually is, right now.

    Deliberately NOT cached: the harness scales to zero and its Service host
    is only known after a wake, so a value resolved at import would be wrong
    for the entire life of the process. Falls back to loopback on any import
    or lookup failure — an entity on the in-pod sidecar has no harness
    backend to ask, and that is not an error.
    """
    try:
        from server.applications import harness_backend
        host = harness_backend.upstream_host(_BROWSER_APP_ID)
        if host:
            return f"http://{host}"
    except Exception:
        pass
    return _BROWSER_CONTROL_URL

# Bearer token for that API. Empty = the sidecar is unauthenticated behind
# its loopback bind (the in-pod default today) and we send no header — so
# this is inert until the browser moves to its own pod and the operator
# starts issuing a token to both sides. Read once at import, like the URL
# above; both change together, in a pod restart.
_BROWSER_CONTROL_TOKEN = os.environ.get("BROWSER_CONTROL_TOKEN", "").strip()


def _browser_headers() -> dict:
    """Auth header for the browser control API, or none while it is open."""
    return ({"X-Browser-Token": _BROWSER_CONTROL_TOKEN}
            if _BROWSER_CONTROL_TOKEN else {})


# Public aliases. server/routes/browser_forms.py dials the SAME control API
# in the other direction — reading a form the user filled, rather than typing
# a secret into one — and how the sidecar is found (harness Service when
# awake, pod loopback for the legacy in-pod placement) must not be worked out
# in two places. Re-deriving that lookup there is how the two ends drift onto
# different shapes of the same deployment.
def browser_base_url() -> str:
    return _browser_base_url()


def browser_headers() -> dict:
    return _browser_headers()


# Saved-form-entry naming, mirrored from server/browser_autofill.py. Declared
# here rather than imported because that module imports THIS one; the two
# constants are pinned equal by tests/test_autofill_broker_guard.py so they
# cannot drift.
_FORM_PREFIX = "form"
_FORM_SECRET_TYPE = "form_secret"


# Mirrors the sidecar's own session-name rule. Enforced HERE too because the
# name is interpolated into a URL path — see _do_browser_type.
_SESSION_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


class VaultAuthzError(Exception):
    """Raised when a credential use is refused or fails. Carries no secret bytes."""


# ──────────────────────────────────────────────────────────────────────────────
# Same-owner use: the host uses its OWN credential, agent never sees the bytes.
# ──────────────────────────────────────────────────────────────────────────────
async def use_credential(
    pool: asyncpg.Pool, *, entity_id: int, name: str, action: str,
    request: Optional[dict] = None, actor: Optional[str] = None,
    path_secret: bool = False, frame_send: Optional[Any] = None,
    mcp_send: Optional[Any] = None,
) -> Any:
    """Open the host's OWN sealed credential `name` in-pod and perform `action`
    with it, returning ONLY the result (never the secret). Supported actions:
      http_call — authenticated outbound request (the secret is injected as the
                  Authorization/header server-side; only the response is returned)
      sign      — HMAC-SHA256 a challenge with the secret as the key
      browser_type — type the secret into a field in a browser-sidecar session
                  (a login FORM, which http_call cannot do). The requester names
                  the credential, the session and the ref; the bytes go over pod
                  loopback to the sidecar and only which-field-was-filled comes
                  back. The sidecar refuses a target that is not a password
                  field unless explicitly overridden.
    Writes a vault_audit `use:<action>` row (redacted — no secret, no headers).

    `path_secret` turns on http_call's auth_scheme='path' (the secret goes into
    the URL PATH, for a platform that wants it there: Telegram's /bot<token>/).
    It is a keyword only kernel code passes — the connector binding
    (server/network/nac_connector.platform_call) — never a field of `request`,
    so no tool argument can put a secret into a URL.

    `frame_send` turns on the ws_token_frame action: a WebSocket frame whose
    d.token the secret fills, handed to `frame_send` (an async callable that
    writes it to the socket the kernel holds) and never returned. Like
    path_secret it is a keyword only kernel code passes — the Discord
    gateway relay (server/connectors/gateway.py), whose platform carries
    its bot token inside the Identify and Resume payloads — so no tool
    argument can ask for it.

    `mcp_send` turns on the mcp_call action: the auth header this broker
    builds for http_call (auth_headers) is handed to `mcp_send`, an async
    callable that makes one MCP exchange through the egress gate
    (server/mcp_connections.py, server/mcp_client.py), and what it returns
    is the result. The header never leaves that call. Like frame_send it is
    a keyword only kernel code passes. One `use:mcp_call` row per use."""
    owner = identity.host_identity(entity_id)
    actor = actor or owner
    # An MCP server's sign-in (server/mcp_oauth.py) goes only to that server,
    # through the kernel's MCP client; never to a URL some other call names.
    # The git server's token is used by the Host's git clients only, never by
    # a broker action, the MCP client's included.
    if vault_api.is_sign_in_name(name) and (action != "mcp_call"
                                            or vault_api.is_git_server_token(name)):
        w = vault_api.held_name_words(name)
        raise VaultAuthzError(f"{name!r} is {w['what']}; only {w['user']} uses it")
    # mcp_call is the kernel's MCP client alone: refused before anything is
    # opened (or audited as opened) when no exchange was handed in.
    if action == "mcp_call" and mcp_send is None:
        raise VaultAuthzError("mcp_call is only for the kernel's MCP client")
    # Only the MCP client's own call may open a sign-in (vault/api.SIGN_IN_PREFIX).
    signed = {"sign_in": True} if action == "mcp_call" else {}
    secret = await vault_api.open_secret(pool, entity_id=entity_id, name=name, actor=actor, **signed)
    if secret is None:
        raise VaultAuthzError(f"no active credential named {name!r}")
    request = request or {}
    detail: dict[str, Any] = {"action": action}
    try:
        if action == "http_call":
            # The host is read from the request as given: in path mode that is
            # the template, which holds the placeholder and never the secret.
            detail.update({"method": (request.get("method") or "GET").upper(),
                           "host": urlparse(request.get("url") or "").netloc})
            if path_secret:
                detail["path_secret"] = True
            result = await _do_http_call(secret, request, path_secret=path_secret)
            detail["status"] = result.get("status")
        elif action == "ws_token_frame":
            if frame_send is None:
                raise VaultAuthzError("ws_token_frame is only for a binding that declares it")
            detail.update({"host": str(request.get("host") or "")[:255],
                           "op": request.get("op")})
            result = await _do_ws_token_frame(secret, request, frame_send)
        elif action == "mcp_call":
            if mcp_send is None:
                raise VaultAuthzError("mcp_call is only for the kernel's MCP client")
            # WHICH server, method and tool, and the HTTP status: never the
            # header, the arguments or the answer.
            detail.update({"host": urlparse(request.get("url") or "").netloc,
                           "server": str(request.get("server") or "")[:64],
                           "method": str(request.get("mcp_method") or "")[:64],
                           "tool": str(request.get("tool") or "")[:128] or None})
            sval = _secret_text(secret)
            try:
                result = await mcp_send(auth_headers(sval, request))
            except Exception as e:
                # A server that echoes the key in an error must not hand it to
                # the model through the error text.
                _withhold_in_error(e, sval)
                raise
            # ...nor through a result: the key is replaced wherever it appears.
            if hasattr(result, "result"):
                result.result = _withhold(result.result, sval)
            detail["status"] = getattr(result, "http_status", None)
        elif action == "sign":
            result = _do_sign(secret, request)
            detail.update({"alg": result.get("alg")})
        elif action == "browser_type":
            request = await _guard_form_entry(pool, entity_id, name, request)
            result = await _do_browser_type(secret, request)
            # WHICH field in WHICH session, never the value — the audit
            # row is the record that a credential was used, not a copy of
            # it. Same discipline as http_call, which logs host+status
            # and never the Authorization header.
            detail.update({"session": result.get("session"),
                           "ref": result.get("ref"),
                           "field_type": result.get("field_type")})
        else:
            raise VaultAuthzError(
                f"unsupported action {action!r} "
                f"(supported: http_call, sign, browser_type, mcp_call)"
            )
    finally:
        # open_secret already audited the get_envelope; record the USE separately,
        # redacted — the secret and any auth headers are NEVER written to the log.
        await _audit_use(pool, actor=actor, owner=owner, name=name, detail=detail)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Cross-identity use: deny-by-default, requires a verified bridge-signed slip.
# Runs on the OWNER's pod (the host that holds the credential). The requester
# presents a slip; we verify it names them + this action, then perform the use
# with OUR key and return only the result. The requester never receives bytes.
# ──────────────────────────────────────────────────────────────────────────────
async def resolve_use(
    pool: asyncpg.Pool, *, requester_entity_id: int, entity_id: int, name: str,
    action: str, slip: Optional[dict], request: Optional[dict] = None,
    actor: Optional[str] = None, peer_trust: Any = None,
) -> dict[str, Any]:
    """Authorize and perform a CROSS-IDENTITY credential use. `entity_id` owns the
    credential (this host); `requester_entity_id` is the peer asking to use it.
    Deny-by-default: a live, unrevoked, bridge-signed slip naming (grantee=requester,
    action='vault.use:<name>') is required, verified offline against PeerTrust's
    pinned keys + CRL. Fails CLOSED if the revocation view isn't fresh."""
    trust = peer_trust if peer_trust is not None else _get_peer_trust()
    if trust is None or not getattr(trust, "ready", False):
        raise VaultAuthzError(
            "cross-identity use refused: revocation view not fresh (fail closed)"
        )
    if not slip:
        raise VaultAuthzError(
            "cross-identity use refused: a bridge-signed permission slip is required"
        )
    ok, reason, _info = verify_slip(
        slip, requester_entity_id, f"vault.use:{name}",
        trusted_keys=trust.keys, revoked_ids=trust.revoked,
        expected_issuer=entity_id,
    )
    if not ok:
        await _audit_use(
            pool, actor=f"peer:{requester_entity_id}",
            owner=identity.host_identity(entity_id), name=name,
            detail={"action": action, "cross_identity": True, "refused": reason},
        )
        raise VaultAuthzError(f"slip refused: {reason}")
    # Slip authorizes; perform the use with our own key. Same no-bytes guarantee.
    return await use_credential(
        pool, entity_id=entity_id, name=name, action=action, request=request,
        actor=actor or f"peer:{requester_entity_id}",
    )


# ── actions ───────────────────────────────────────────────────────────────────
# The placeholder a path-mode URL carries where the secret goes, and what a
# secret must be to go into a path unencoded (a Telegram bot token is
# <digits>:<letters, digits, _ and ->). A secret with anything else is refused
# rather than percent-encoded: an encoded secret is a different string to the
# platform, and a '/' or '?' in it would move it out of the segment it was
# put in.
PATH_SECRET_PLACEHOLDER = "{secret}"
_PATH_SAFE_SECRET = re.compile(r"^[A-Za-z0-9:_-]{8,256}$")


def _path_secret_url(url: str, sval: str) -> str:
    """The URL with the secret put into its PATH, or VaultAuthzError. The
    placeholder must appear exactly once and only in the path: a URL that
    names it in the host, the query or the fragment is refused, so the secret
    can never ride in a query string a server or proxy logs."""
    from urllib.parse import urlsplit, urlunsplit
    if url.count(PATH_SECRET_PLACEHOLDER) != 1:
        raise VaultAuthzError("a path-secret url names its placeholder exactly once")
    parts = urlsplit(url)
    if PATH_SECRET_PLACEHOLDER not in parts.path:
        raise VaultAuthzError("the secret may go into the url path only")
    if parts.scheme != "https":
        raise VaultAuthzError("a path secret travels over https only")
    if not _PATH_SAFE_SECRET.match(sval):
        raise VaultAuthzError("this credential is not one that can go into a url path")
    return urlunsplit(parts._replace(path=parts.path.replace(PATH_SECRET_PLACEHOLDER, sval)))


KEY_WITHHELD = "[key withheld]"


def _withhold(value, sval: str):
    """`value` with every occurrence of the secret replaced, walked through
    dicts and lists. A server holds the key it was sent; echoing it back must
    not put it in front of the model."""
    if not sval:
        return value
    if isinstance(value, str):
        return value.replace(sval, KEY_WITHHELD) if sval in value else value
    if isinstance(value, dict):
        return {k: _withhold(v, sval) for k, v in value.items()}
    if isinstance(value, list):
        return [_withhold(v, sval) for v in value]
    return value


def _withhold_in_error(e: BaseException, sval: str) -> None:
    if not sval:
        return
    e.args = tuple(_withhold(a, sval) for a in e.args)
    if getattr(e, "data", None) is not None:
        try:
            e.data = _withhold(e.data, sval)
        except AttributeError:
            pass


def _secret_text(secret) -> str:
    return (secret.decode("utf-8", "ignore") if isinstance(secret, (bytes, bytearray))
            else str(secret)).strip()


def auth_headers(sval: str, request: dict) -> dict[str, str]:
    """The header that carries the secret, by `auth_scheme` (bearer, the
    default; header, with `auth_header` naming it and an optional
    `auth_prefix`; basic). One rule for http_call and mcp_call, so the two
    can never put a credential in different places."""
    scheme = (request.get("auth_scheme") or "bearer").lower()
    header_name = request.get("auth_header") or "Authorization"
    if scheme == "bearer":
        return {header_name: f"Bearer {sval}"}
    if scheme == "header":
        # A prefix for platforms that want one before the token (Discord's
        # `Authorization: Bot <token>`).
        return {header_name: str(request.get("auth_prefix") or "") + sval}
    if scheme == "basic":
        return {header_name: "Basic " + base64.b64encode(sval.encode()).decode()}
    raise VaultAuthzError(f"unsupported auth_scheme {scheme!r} (bearer/header/basic)")


async def _do_http_call(secret: bytes, request: dict, *,
                        path_secret: bool = False) -> dict[str, Any]:
    """Make an authenticated outbound request with `secret` injected server-side.
    Returns status + (truncated) body only — never echoes the credential.

    auth_scheme='path' puts the secret into the URL path (the {secret}
    placeholder) and is honoured only with path_secret=True, which no
    `request` field can set. In that mode redirects are never followed, an
    error names only its type (an HTTP client's own message can carry the URL),
    and the secret is cut out of the body before it is returned."""
    import aiohttp

    url = request.get("url")
    if not url:
        raise VaultAuthzError("http_call requires 'url'")
    method = (request.get("method") or "GET").upper()
    headers = dict(request.get("headers") or {})
    scheme = (request.get("auth_scheme") or "bearer").lower()
    sval = _secret_text(secret)
    if scheme == "path":
        if not path_secret:
            raise VaultAuthzError("auth_scheme 'path' is only for a binding that declares it")
        url = _path_secret_url(str(url), sval)
    elif PATH_SECRET_PLACEHOLDER in str(url):
        raise VaultAuthzError("the url names a secret placeholder, but this is not a path-secret call")
    else:
        headers.update(auth_headers(sval, request))
    if scheme == "path":
        return await _path_secret_request(url, method, headers, request, sval)

    timeout = aiohttp.ClientTimeout(total=float(request.get("timeout") or 30))
    # trust_env=True so brokered third-party calls honor HTTP(S)_PROXY/NO_PROXY and
    # exit through the governed host-egress proxy (squid), like all other third-party
    # egress — not straight out of the pod. NO_PROXY keeps cluster-internal direct.
    # REDIRECTS ARE THE CALLER'S CHOICE, and a caller that PINNED a destination
    # must be able to say no. Default unchanged (aiohttp follows), because every
    # existing caller names the URL at call time and a redirect it follows goes
    # somewhere it already chose to trust.
    #
    # It matters for a caller whose whole guarantee is "this credential only ever
    # reaches THIS host" — reach-out channels, which pin the host at creation.
    # Measured against aiohttp 3.14.3, because assuming either way
    # would have been a guess: across a cross-origin redirect aiohttp STRIPS
    # `Authorization`, and FORWARDS a custom header verbatim. `auth_scheme=
    # 'header'` with a caller-chosen `auth_header` (see above) is exactly that
    # shape, so a pinned destination answering 307 could hand an X-API-Key style
    # secret to any host it named. The pin is checked before the call; this is
    # what stops the destination moving after it.
    #
    # NEVER THIS HOST ITSELF, on any hop: destination.session_kwargs
    # refuses loopback, the pod's own addresses and the kernel's own names
    # before each request aiohttp sends, redirects included, and again on the
    # addresses the connection uses. The caller picks the URL and the headers,
    # so a call to [::1]:8800 was a request to this kernel from its own loopback.
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True,
                                         **destination.session_kwargs()) as s:
            async with s.request(
                method, url, headers=headers,
                json=request.get("json"), data=request.get("body"),
                allow_redirects=bool(request.get("allow_redirects", True)),
            ) as resp:
                status = resp.status
                wait = _retry_after_header(resp)
                text = await resp.text()
    except destination.SelfDestinationRefused as e:
        raise VaultAuthzError(f"http_call refused: {e}") from None
    body = text[:_MAX_BODY]
    parsed: Any = None
    try:
        import json as _json
        parsed = _json.loads(text) if text.strip() else None
    except Exception:
        parsed = None
    out = {"status": status, "body": body, "json": parsed, "truncated": len(text) > _MAX_BODY}
    if wait is not None:
        out["retry_after_header"] = wait
    return out


def _retry_after_header(resp) -> Optional[str]:
    """The upstream's Retry-After header (RFC 9110 10.2.3: seconds, or an
    HTTP date), as it came, when it came: how long a rate-limited caller is
    asked to wait. Only this one header is passed back, never the rest."""
    v = (getattr(resp, "headers", None) or {}).get("Retry-After")
    return str(v).strip()[:64] if v is not None and str(v).strip() else None


async def _path_secret_request(url: str, method: str, headers: dict, request: dict,
                               sval: str) -> dict[str, Any]:
    """The path-mode request (see _do_http_call). Separate so that nothing on
    this path can hand the real URL back: aiohttp's own exceptions carry it
    (ClientResponseError.request_info, InvalidURL), so an error is reported
    by type and host only, and the body is scrubbed of the secret."""
    import aiohttp
    host = urlparse(url).hostname or ""
    timeout = aiohttp.ClientTimeout(total=float(request.get("timeout") or 30))
    try:
        # Never this Host itself (see _do_http_call). The refusal names
        # the host only, which in path mode never holds the secret.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True,
                                         **destination.session_kwargs()) as s:
            async with s.request(method, url, headers=headers,
                                 json=request.get("json"), data=request.get("body"),
                                 allow_redirects=False) as resp:
                status = resp.status
                wait = _retry_after_header(resp)
                # Bounded: a little past what is returned, so `truncated` is
                # still known, and never a body of the platform's choosing.
                # aiohttp's read(n) answers with whatever has ARRIVED, up to
                # n, not n bytes: one call cut a body that came in more than
                # one piece (a Telegram getUpdates with many updates) short,
                # unmarked and unparsed. So read until the end or the bound.
                buf = bytearray()
                while len(buf) <= _MAX_BODY:
                    chunk = await resp.content.read(_MAX_BODY + 1 - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                raw = bytes(buf)
        text = raw.decode("utf-8", "replace")
    except destination.SelfDestinationRefused as e:
        raise VaultAuthzError(f"http_call refused: {e}") from None
    except Exception as e:  # noqa: BLE001 — never re-raised with its message
        # "did not go through", not "failed": only a connector takes this lane
        # (nac_connector.platform_call), it answers the connector 502 with
        # these words, and the connector logs them on the line that says it
        # is trying again, which the app proof must not read as an error.
        raise VaultAuthzError(f"the call to {host} did not go through ({type(e).__name__})") from None
    text = text.replace(sval, "[redacted]")
    body = text[:_MAX_BODY]
    parsed: Any = None
    try:
        import json as _json
        parsed = _json.loads(text) if text.strip() else None
    except Exception:
        parsed = None
    out = {"status": status, "body": body, "json": parsed, "truncated": len(text) > _MAX_BODY}
    if wait is not None:
        out["retry_after_header"] = wait.replace(sval, "[redacted]")
    return out


async def _do_ws_token_frame(secret: bytes, request: dict, frame_send) -> dict[str, Any]:
    """Fill d.token of the JSON frame in request['frame'] with the secret and
    hand the frame's text to `frame_send`. What comes back is its size, never
    the frame: the secret goes to the socket and nowhere else. A frame over
    request['max_bytes'] once filled is not sent."""
    import copy
    import json as _json
    frame = request.get("frame")
    if not isinstance(frame, dict) or not isinstance(frame.get("d"), dict):
        raise VaultAuthzError("ws_token_frame needs a frame object with a d object")
    sval = (secret.decode("utf-8", "ignore") if isinstance(secret, (bytes, bytearray))
            else str(secret)).strip()
    filled = copy.deepcopy(frame)
    filled["d"]["token"] = sval
    text = _json.dumps(filled, ensure_ascii=False, separators=(",", ":"))
    size = len(text.encode("utf-8"))
    limit = request.get("max_bytes")
    if isinstance(limit, int) and size > limit:
        raise VaultAuthzError(f"the frame is {size} bytes with the token in it, over {limit}")
    await frame_send(text)
    return {"sent": True, "bytes": size}


async def _guard_form_entry(
    pool: asyncpg.Pool, entity_id: int, name: str, request: dict,
) -> dict:
    """Enforce the autofill invariants HERE, where every caller passes.

    Origin: there are THREE ways to
    reach `browser_type`, and only one of them checked anything:

      1. the `autofill` host tool  -> server/browser_autofill.fill  (checked)
      2. the `keys_manage` tool, op='use', action passed through verbatim
      3. POST /api/vault/secrets/{name}/use, likewise, gated only on
         write:vault — which the Browser app now holds

    Doors 2 and 3 let a caller name a saved `form_secret` and pass
    `allow_non_password: true`, typing the user's password into a visible
    text box with no standing grant anywhere in the picture. kimi
    demonstrated door 2 end to end; door 3 is its sibling, found by asking
    whether the same shape existed elsewhere. A guard that lives in one
    caller is not a guard, so it moves to the broker — the one place all
    three converge, and the only place that can read the stored cred_type
    rather than believe the caller about it.

    Applies ONLY to `form:<host>:<key>` credentials. Every other credential
    reaching browser_type (a hand-added site login, say) behaves exactly as
    before — this narrows nothing that was not autofill's to begin with.

    Three things are taken out of the caller's hands:
      - the standing per-site grant must be live, or nothing is typed;
      - `expect_host` is derived from the NAME, not the request, so a caller
        cannot point a saved credential at whatever host its session happens
        to be sitting on;
      - `allow_non_password` is forced False for a stored form_secret.
    """
    if not name.startswith(f"{_FORM_PREFIX}:"):
        return request
    parts = name.split(":", 2)
    if len(parts) != 3 or not parts[1]:
        raise VaultAuthzError(
            f"{name!r} is not a usable form entry name (form:<host>:<key>)")
    host = parts[1].lower()

    # Imported lazily: browser_autofill imports THIS module, so a top-level
    # import here is a cycle.
    from server import browser_autofill as autofill

    if not await autofill.is_granted(pool, entity_id=entity_id, host=host):
        raise VaultAuthzError(
            f"browser_type REFUSED: no standing grant for {host}. Saved form "
            f"entries may only be typed for a site the user has granted, and "
            f"the grant is checked here, not by the caller.")

    # Record the exercise HERE too, for the same reason the guard lives here:
    # autofill.fill was the only thing touching last_used_at, so a use through
    # POST /api/vault/secrets/{name}/use left the grant looking untouched. Keys
    # shows "never used yet" as the signal to prune on, so under-reporting
    # would have a user revoke a grant that is in fact in use.
    #
    # Fires per FIELD, and on authorization rather than on a successful type,
    # so it answers "is anything using this grant" rather than "did a field
    # land" — which is the question the pruning UI actually asks.
    await autofill._touch(pool, entity_id=entity_id, host=host)

    cred_type = await store.cred_type_of(
        pool, owner_identity=identity.host_identity(entity_id), logical_name=name)
    guarded = dict(request)
    guarded["expect_host"] = host
    if cred_type == _FORM_SECRET_TYPE:
        # The one that matters. A value the user saved from a password box
        # may only ever go back into one, whatever the caller asked for.
        guarded["allow_non_password"] = False
    return guarded


async def _do_browser_type(secret: bytes, request: dict) -> dict[str, Any]:
    """Type the credential into a field in a browser-sidecar session.

    Why this action exists: a login FORM is not an API call, so
    `http_call` cannot perform one, and without this the only way to sign
    a recipe into a site is for the caller — and therefore whatever model
    drives the caller — to hold the password. Here the requester names
    the credential and the field; the bytes go from the sealed envelope
    to the sidecar over the pod's loopback and are never returned,
    logged, or seen by a model.

    The sidecar does its own check that the target really is a password
    field. The requester still chooses WHICH element, and a hostile page
    can offer a convincing decoy, so that refusal surfaces here as a
    failure rather than as a quietly-leaked secret.

    trust_env is OFF, unlike _do_http_call: this is a loopback call to
    our own pod and must never be handed to the egress proxy.
    """
    import aiohttp

    session = str(request.get("session") or "").strip()
    ref = str(request.get("ref") or "").strip()
    expect_host = str(request.get("expect_host") or "").strip().lower()
    if not session or not ref:
        raise VaultAuthzError("browser_type requires 'session' and 'ref'")
    # The session name is interpolated into a URL path below. Unvalidated,
    # "bank/../../../../auth/anthropic" normalised to a different endpoint
    # on the sidecar — confined to that origin, but a path-traversal all the
    # same, and it bypassed the sidecar's own name validation entirely.
    if not _SESSION_NAME_RX.match(session):
        raise VaultAuthzError(
            f"session {session!r} is not a valid session name "
            f"(lowercase letters, digits and hyphens)")
    # WHERE the credential may be typed. Without this the broker would put
    # a bank password into any session the caller named, on whatever page
    # it happened to be showing — so a recipe steered onto an attacker's
    # site (the realistic prompt-injection outcome) still got the secret
    # typed for it. The page's LIVE url is checked, not the caller's claim
    # about it.
    #
    # What this does NOT defend against: a caller that is itself malicious.
    # It holds write:vault, and `http_call` already lets it send any
    # host-owned credential to a URL of its choosing. This is a guard on a
    # confused caller, not a hostile one, and that is the threat that
    # actually exists here.
    if not expect_host:
        raise VaultAuthzError(
            "browser_type requires 'expect_host' — the host this credential "
            "is for. The broker refuses to type a secret into a page without "
            "being told where it is supposed to be going.")

    sval = (secret.decode("utf-8", "ignore") if isinstance(secret, (bytes, bytearray))
            else str(secret))
    # Read the session's LIVE page before typing anything into it.
    info_url = f"{_browser_base_url()}/v1/sessions/{session}"
    timeout_s = float(request.get("timeout") or 30)
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_s), trust_env=False) as s_:
            async with s_.get(info_url, headers=_browser_headers()) as r:
                if r.status >= 400:
                    raise VaultAuthzError(
                        f"browser_type: no such session {session!r} ({r.status})")
                # The browser pod's answer: capped and value-budgeted
                # (server/harness_input.py); a refusal is an ordinary error.
                from server import harness_input as _hin
                info = await _hin.read_json(r, 4 * 1024 * 1024, "browser session info")
    except VaultAuthzError:
        raise
    except Exception as e:
        raise VaultAuthzError(
            f"browser_type could not read the session: {type(e).__name__}")

    page_host = (urlparse(info.get("url") or "").hostname or "").lower()
    if not page_host:
        raise VaultAuthzError(
            f"browser_type: session {session!r} is not on a page yet "
            f"(url={info.get('url')!r}) — navigate before typing a secret")
    if page_host != expect_host:
        raise VaultAuthzError(
            f"browser_type REFUSED: session {session!r} is on {page_host!r}, "
            f"but this credential was declared for {expect_host!r}")

    url = f"{_browser_base_url()}/v1/sessions/{session}/act"
    payload = {
        "action": "type_secret",
        "ref": ref,
        "text": sval,
        "allow_non_password": bool(request.get("allow_non_password", False)),
        "clear": bool(request.get("clear", True)),
    }
    timeout = aiohttp.ClientTimeout(total=float(request.get("timeout") or 30))
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as s:
            async with s.post(url, json=payload, headers=_browser_headers()) as resp:
                status = resp.status
                from server import harness_input as _hin
                buf = await _hin.read_body(resp, 4 * 1024 * 1024, "browser type_secret")
                try:
                    body = await _hin.parse_json(buf, "browser type_secret")
                except _hin.HarnessInputRefused:
                    body = {"detail": bytes(buf[:500]).decode("utf-8", "replace")}
    except VaultAuthzError:
        raise
    except Exception as e:
        # The payload is in scope here — surface the TYPE only, never a
        # message that could carry the request body back to the caller.
        raise VaultAuthzError(
            f"browser_type could not reach the browser sidecar: {type(e).__name__}")

    if status >= 400:
        detail = body.get("detail") if isinstance(body, dict) else ""
        raise VaultAuthzError(f"browser_type refused by the sidecar ({status}): {detail}")
    body = body if isinstance(body, dict) else {}
    # Echo only the non-secret half of the sidecar's answer.
    return {"session": session, "ref": ref, "host": page_host,
            "field_type": body.get("field_type"),
            "url": body.get("url"), "title": body.get("title")}


def _do_sign(secret: bytes, request: dict) -> dict[str, Any]:
    """Sign a challenge with the credential as the key (the key never leaves)."""
    alg = (request.get("alg") or "hmac-sha256").lower()
    challenge_b64 = request.get("challenge_b64")
    if not challenge_b64:
        raise VaultAuthzError("sign requires 'challenge_b64'")
    try:
        challenge = base64.b64decode(challenge_b64)
    except Exception:
        raise VaultAuthzError("challenge_b64 is not valid base64")
    if alg == "hmac-sha256":
        import hashlib
        import hmac
        key = bytes(secret) if isinstance(secret, (bytes, bytearray)) else str(secret).encode()
        sig = hmac.new(key, challenge, hashlib.sha256).digest()
        return {"alg": alg, "signature_b64": base64.b64encode(sig).decode()}
    raise VaultAuthzError(f"unsupported sign alg {alg!r} (supported: hmac-sha256)")


# ── plumbing ──────────────────────────────────────────────────────────────────
async def _audit_use(
    pool: asyncpg.Pool, *, actor: str, owner: str, name: str, detail: dict,
) -> None:
    async with pool.acquire() as conn:
        await store._audit(conn, actor, f"use:{detail.get('action', '?')}", owner, name, detail)


def _get_peer_trust():
    """The live PeerTrust set up at lifespan (pinned bridge keys + synced CRL).
    None before peer trust is wired — callers fail closed on None."""
    try:
        from server import main as _main
        return getattr(_main, "_peer_trust_ref", None)
    except Exception:
        return None
