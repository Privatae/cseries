"""The sign-in an outside agent's MCP client runs against this Host.

MCP authorization (protocol 2025-11-25): the client finds this Host's
authorization server from the protected-resource metadata named in /mcp's
401 (RFC 9728), registers itself (RFC 7591), sends the person to the connect
page with a PKCE challenge (RFC 7636), and trades the code for tokens. This
Host is the authorization server because the consent is its own: it grants
rights over this Host's machines, and only this Host can write that page from
its records.

Client ID Metadata Documents are not offered (the metadata says so): they
would make this Host fetch a URL the client chose, which is a call through the
Host's egress gate that this build does not make. Every current client also
speaks dynamic registration.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from server.agent_access import pages, person, store
from server.agent_access.origin import agent_resources, authorization_origin, issuer_for, public_origin

router = APIRouter()
log = logging.getLogger("cseries.agent_access")

BODY_MAX = 64 * 1024
SCOPE = "machines"

# The paths a client reaches with no session of the person's: each answers for
# itself (the metadata is public; registration hands out an id that grants
# nothing until the person connects it; the token and revoke endpoints check
# the code, the PKCE verifier or the token). The front door and the localhost
# gate let them through to their handler by localhost_only.AGENT_PATHS, which
# is this set and /mcp (scripts/test_agent_access_door.py keeps the two equal).
# /oauth/authorize is a page of the person's and is not here.
SELF_AUTH_PATHS = frozenset({
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/oauth/register", "/oauth/token", "/oauth/revoke",
})


def _pool_eid():
    from server import brain_service
    return brain_service.get_pool(), int(getattr(brain_service, "_entity_id", 0) or 0)


def resource_metadata_url(origin: str) -> str:
    return origin + "/.well-known/oauth-protected-resource/mcp"


def challenge_header(origin: str) -> str:
    """WWW-Authenticate for /mcp's 401 (RFC 9728 §5.1)."""
    return f'Bearer resource_metadata="{resource_metadata_url(origin)}", scope="{SCOPE}"'


async def _read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > BODY_MAX:
        raise store.AgentAccessError("invalid_request", "request body too large", status=413)
    out = bytearray()
    async for chunk in request.stream():
        out += chunk
        if len(out) > BODY_MAX:
            raise store.AgentAccessError("invalid_request", "request body too large", status=413)
    return bytes(out)


async def _form(request: Request) -> dict[str, str]:
    raw = await _read_body(request)
    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        raise store.AgentAccessError("invalid_request", "the body is not UTF-8") from None
    out = {}
    for key, values in parsed.items():
        if len(values) != 1:
            raise store.AgentAccessError("invalid_request", f"'{key}' given more than once")
        out[key] = values[0]
    return out


def _oauth_error(err: store.AgentAccessError) -> JSONResponse:
    return JSONResponse({"error": err.error, "error_description": err.detail},
                        status_code=err.status, headers={"Cache-Control": "no-store"})


def _no_pool() -> JSONResponse:
    return JSONResponse({"error": "temporarily_unavailable",
                         "error_description": "this Host is starting; try again shortly"},
                        status_code=503, headers={"Retry-After": "5"})


# ── metadata ────────────────────────────────────────────────────────────────

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request):
    origin = await public_origin(request)
    return JSONResponse({
        "resource": origin + "/mcp",
        "authorization_servers": [origin],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [SCOPE],
        "resource_name": "Your Host",
    }, headers={"Cache-Control": "max-age=300"})


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server(request: Request):
    origin = await public_origin(request)
    return JSONResponse({
        "issuer": origin,
        # On a loopback agent origin, the desktop's own connect page, where
        # the person is signed in and their passkey works (origin.py).
        "authorization_endpoint": authorization_origin(origin) + "/oauth/authorize",
        "token_endpoint": origin + "/oauth/token",
        "registration_endpoint": origin + "/oauth/register",
        "revocation_endpoint": origin + "/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": False,
    }, headers={"Cache-Control": "max-age=300"})


# ── registration (RFC 7591) ─────────────────────────────────────────────────

@router.post("/oauth/register")
async def register(request: Request):
    pool, eid = _pool_eid()
    if pool is None:
        return _no_pool()
    try:
        raw = await _read_body(request)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise store.AgentAccessError("invalid_client_metadata", "the body is not JSON") from None
        if not isinstance(body, dict):
            raise store.AgentAccessError("invalid_client_metadata", "the body must be an object")
        method = body.get("token_endpoint_auth_method", "none")
        if method != "none":
            raise store.AgentAccessError(
                "invalid_client_metadata",
                "this Host registers public clients only (token_endpoint_auth_method 'none', with PKCE)")
        grants = body.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
            raise store.AgentAccessError("invalid_client_metadata",
                                         "grant_types may hold authorization_code and refresh_token only")
        responses = body.get("response_types") or ["code"]
        if responses != ["code"]:
            raise store.AgentAccessError("invalid_client_metadata", "response_types must be ['code']")
        made = await store.register_client(pool, eid, body.get("client_name"), body.get("redirect_uris"))
    except store.AgentAccessError as err:
        return _oauth_error(err)
    import time
    return JSONResponse({
        "client_id": made["client_id"],
        "client_id_issued_at": int(time.time()),
        "client_name": made["client_name"],
        "redirect_uris": made["redirect_uris"],
        "grant_types": grants,
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201, headers={"Cache-Control": "no-store"})


# ── the connect page ────────────────────────────────────────────────────────

def _redirect_with(redirect_uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=303,
                            headers={"Cache-Control": "no-store"})


def _html(text: str, status: int = 200, nonce: str = "") -> HTMLResponse:
    # A page offering a passkey runs its one script (pages.PASSKEY_SCRIPT, by
    # this nonce) and may fetch from this origin, for the passkey check; no other.
    script = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if nonce else ""
    return HTMLResponse(text, status_code=status, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; {script}"
                                   "form-action 'self' http: https:; frame-ancestors 'none'",
        "X-Frame-Options": "DENY"})


async def _authorize_request(pool, eid: int, q: dict) -> tuple[dict, dict]:
    """(the client's row, the checked request). Raises AgentAccessError with
    `status` 400 for anything that must not be redirected (an unknown client or
    a redirect URI it did not register), else with the error to send back."""
    client = await store.client(pool, eid, q.get("client_id") or "")
    if client is None:
        raise store.AgentAccessError("invalid_client", "this agent is not registered with this Host")
    redirect_uri = q.get("redirect_uri") or ""
    if not store.redirect_matches(client["redirect_uris"], redirect_uri):
        raise store.AgentAccessError("invalid_request",
                                     "the redirect address is not one this agent registered")
    checked = {"client_id": client["client_id"], "redirect_uri": redirect_uri,
               "state": q.get("state") or "", "code_challenge": q.get("code_challenge") or "",
               "resource": q.get("resource") or ""}
    return client, checked


NO_RESOURCE = ("the authorization request has no resource parameter (RFC 8707), which MCP clients "
               "must send: the address of the MCP server this agent is for")


def _no_resource(client: dict, checked: dict, origin: str):
    """A request without `resource` is refused: MCP 2025-11-25 makes clients
    send it, and a code issued without it would bind its tokens to no address.
    To an agent the person connected before, whose redirect address they
    approved then, as RFC 6749 §4.1.2.1 says: back to that address with
    error=invalid_request naming the parameter. To one never connected, on
    this page, as the other malformed requests are: anyone may register any
    https address, and an automatic redirect would make this Host's address a
    link to a stranger's site (RFC 9700 §4.11.2)."""
    if client.get("connected_at") is not None:
        return _redirect_error(checked, origin, "invalid_request", NO_RESOURCE)
    return _html(pages.message("This link cannot connect an agent",
                               "The agent's link has no resource parameter (RFC 8707), which MCP "
                               "clients must send: the address of the MCP server it is for. Update "
                               "the agent, then open its link again.", error=True), 400)


def _redirect_error(checked: dict, iss: str, error: str, detail: str) -> RedirectResponse:
    params = {"error": error, "error_description": detail, "iss": iss}
    if checked.get("state"):
        params["state"] = checked["state"]
    return _redirect_with(checked["redirect_uri"], params)


@router.get("/oauth/authorize")
async def authorize_page(request: Request):
    pool, eid = _pool_eid()
    if pool is None:
        return _html(pages.message("Your Host is starting", "Try again in a few seconds."), 503)
    if not person.person_email(request):
        return _html(pages.message("Sign in to your Host first",
                                   "Only you can connect an agent to this Host. Sign in, then "
                                   "open the agent's link again.", error=True), 401)
    q = {k: v[0] for k, v in parse_qs(request.url.query, keep_blank_values=True).items() if v}
    origin = await public_origin(request)
    try:
        client, checked = await _authorize_request(pool, eid, q)
    except store.AgentAccessError as err:
        return _html(pages.message("This link cannot connect an agent", err.detail, error=True), 400)
    # A malformed request is answered on this page, never by sending the
    # person on to the redirect address: anyone may register a client with
    # any https address, so an automatic error redirect would make this
    # Host's own address a link that lands its person on a stranger's site
    # (RFC 9700 §4.11.2). The agent's software is told by its own timeout.
    if q.get("response_type") != "code":
        return _html(pages.message("This link cannot connect an agent",
                                   "The agent's link asks for a kind of sign-in this Host does not "
                                   "give (response_type must be code).", error=True), 400)
    if q.get("code_challenge_method") != "S256" or not store.valid_challenge(checked["code_challenge"]):
        return _html(pages.message("This link cannot connect an agent",
                                   "The agent's link has no PKCE code_challenge with method S256, "
                                   "which this Host requires.", error=True), 400)
    if not checked["resource"]:
        return _no_resource(client, checked, origin)
    if checked["resource"] not in agent_resources(origin):
        return _html(pages.message("This link cannot connect an agent",
                                   "The agent's link names another server; this Host serves only "
                                   + " and ".join(sorted(agent_resources(origin))) + ".",
                                   error=True), 400)
    token = await person.sign_form(pool, {"k": "connect", **checked})
    factors = await person.factors(pool, request)
    nonce = pages.passkey_nonce(factors)
    return _html(pages.connect_page(client_name=client["client_name"],
                                    redirect_host=urlsplit(checked["redirect_uri"]).netloc,
                                    form_token=token, factors=factors, nonce=nonce), nonce=nonce)


@router.post("/oauth/authorize")
async def authorize_answer(request: Request):
    pool, eid = _pool_eid()
    if pool is None:
        return _html(pages.message("Your Host is starting", "Try again in a few seconds."), 503)
    if not person.person_email(request):
        return _html(pages.message("Sign in to your Host first",
                                   "Only you can connect an agent to this Host.", error=True), 401)
    try:
        form = await _form(request)
    except store.AgentAccessError as err:
        return _html(pages.message("That answer could not be read", err.detail, error=True), err.status)
    checked = await person.read_form(pool, form.get("form") or "")
    if not checked or checked.get("k") != "connect":
        return _html(pages.message("This page has expired",
                                   "Open the agent's link again to connect it.", error=True), 400)
    origin = await public_origin(request)
    try:
        client, _ = await _authorize_request(pool, eid, checked)
    except store.AgentAccessError as err:
        return _html(pages.message("This agent cannot be connected", err.detail, error=True), 400)
    if not checked.get("resource"):
        # A page signed before this Host required the resource.
        return _no_resource(client, checked, origin)
    if checked["resource"] not in agent_resources(origin):
        return _html(pages.message("This agent cannot be connected",
                                   "The agent's link names another server.", error=True), 400)
    # The issuer the client discovered (RFC 9207): the address it asked for
    # (an agent on this computer found this Host on 127.0.0.1), else this
    # page's own.
    iss = issuer_for(checked["resource"], origin)
    if form.get("decision") != "approve":
        return _redirect_error(checked, iss, "access_denied", "the person did not connect this agent")
    read = form.get("read") == "1"
    from server.network.nac_stepup import StepupLocked
    try:
        ok, why = await person.confirm(pool, request, form.get("code") or "")
    except StepupLocked as locked:
        ok, why = False, person.locked_words(locked)
    if not ok:
        factors = await person.factors(pool, request)
        nonce = pages.passkey_nonce(factors)
        return _html(pages.connect_page(client_name=client["client_name"],
                                        redirect_host=urlsplit(checked["redirect_uri"]).netloc,
                                        form_token=form["form"], error=why, read_checked=read,
                                        factors=factors, nonce=nonce), 403, nonce)
    await store.connect(pool, eid, client["id"], read=read)
    code = await store.issue_code(pool, client["id"], checked["redirect_uri"],
                                  checked["code_challenge"], checked["resource"])
    log.info("agent connected: connection=%s read=%s", client["id"], read)
    params = {"code": code, "iss": iss}
    if checked.get("state"):
        params["state"] = checked["state"]
    return _redirect_with(checked["redirect_uri"], params)


# ── tokens (RFC 6749 §4.1.3, §6) and revocation (RFC 7009) ──────────────────

@router.post("/oauth/token")
async def token(request: Request):
    pool, eid = _pool_eid()
    if pool is None:
        return _no_pool()
    try:
        form = await _form(request)
        grant = form.get("grant_type")
        client_id = form.get("client_id") or ""
        if await store.client(pool, eid, client_id) is None:
            raise store.AgentAccessError("invalid_client", "this agent is not registered with this Host",
                                         status=401)
        if grant == "authorization_code":
            out = await store.redeem_code(pool, eid, client_id, form.get("code") or "",
                                          form.get("redirect_uri") or "", form.get("code_verifier") or "",
                                          form.get("resource") or "")
        elif grant == "refresh_token":
            out = await store.refresh_tokens(pool, eid, client_id, form.get("refresh_token") or "",
                                             form.get("resource") or "")
        else:
            raise store.AgentAccessError("unsupported_grant_type",
                                         "grant_type must be authorization_code or refresh_token")
    except store.AgentAccessError as err:
        return _oauth_error(err)
    return JSONResponse({**out, "scope": SCOPE},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/revoke")
async def revoke(request: Request):
    pool, eid = _pool_eid()
    if pool is None:
        return _no_pool()
    try:
        form = await _form(request)
    except store.AgentAccessError as err:
        return _oauth_error(err)
    await store.revoke_token(pool, eid, form.get("token") or "")
    return JSONResponse({}, headers={"Cache-Control": "no-store"})
