"""
Integrations API — list providers, list/create/revoke connections,
OAuth authorize + callback.

Provider-specific sync code lives in providers/<key>.py; this router
dispatches the generic bits + per-provider hooks.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Depends
from server.applications.manifest import RESERVED_APP_IDS
from server.applications.middleware import requires_permission
from server.peer import app_lane
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from server.integrations.core import connection as conn_mod
from server.integrations.core import credentials, manifests, oauth, storage
from server.integrations.core.models import ConnectionStatus

logger = logging.getLogger("privatae.pc.routes.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _entity_user_id() -> str:
    return str(os.environ.get("ENTITY_ID", "0"))


def _dashboard_origin(request: Request) -> str:
    """Base URL the BROWSER sees — the address an OAuth redirect has to come
    back to. A C-Series Host is opened on one address, its CSERIES_URL
    (server/home_origin.py), so that is the answer whatever name this request
    arrived under: a redirect URI registered with a provider must not change
    with the Host header a request happened to carry."""
    from server.home_origin import HOME
    return HOME.origin


def _import_provider(key: str):
    """Dynamically import the provider module for a given key. Only
    providers that ship a Python module get OAuth + sync support."""
    import importlib
    try:
        return importlib.import_module(f"server.integrations.providers.{key}")
    except ModuleNotFoundError:
        return None


@router.get("/providers", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def list_providers():
    """List all provider manifests available to install."""
    providers = manifests.load_all()
    out = []
    for key, p in providers.items():
        out.append({
            "key": key,
            "display_name": p.display_name,
            "categories": p.categories,
            "connection_kind": p.connection_kind,
            "connection_provider": p.connection_provider or p.key,
            "auth_mode": p.auth_mode.value,
            "resources": [r.name for r in p.resources],
        })
    # Sort by category then name for predictable UI
    out.sort(key=lambda p: (tuple(p["categories"]) or ("zzz",), p["display_name"]))
    return {"providers": out, "count": len(out)}


@router.get("/providers/{key}", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def get_provider(key: str):
    p = manifests.get(key)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    return {
        "key": p.key,
        "display_name": p.display_name,
        "categories": p.categories,
        "connection_kind": p.connection_kind,
        "connection_provider": p.connection_provider or p.key,
        "auth_mode": p.auth_mode.value,
        "auth_scopes": (p.auth or {}).get("scopes") or [],
        "resources": [
            {"name": r.name, "canonical_model": r.canonical_model,
             "sync_strategy": (r.sync or {}).get("strategy")}
            for r in p.resources
        ],
        "webhooks": list((p.webhooks or {}).keys()),
        "reauth_triggers": p.reauth_triggers,
    }


@router.get("/connections", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def list_connections():
    """All connections for the current entity."""
    conns = await conn_mod.list_for_user(_entity_user_id())
    return {"connections": conns, "count": len(conns)}


class RevokeRequest(BaseModel):
    pass


class ApiKeyConnectRequest(BaseModel):
    api_key: str
    display_name: str | None = None


@router.delete("/connections/{connection_id}", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def revoke_connection(connection_id: str):
    row = await storage.get_connection(connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    if row["user_id"] != _entity_user_id():
        raise HTTPException(status_code=403, detail="not owner")
    await conn_mod.revoke_vault(connection_id)
    return {"revoked": connection_id}


@router.post("/providers/{key}/connect-apikey", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def connect_with_apikey(key: str, req: ApiKeyConnectRequest):
    """Connect a provider using an API key instead of OAuth."""
    p = manifests.get(key)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    if p.auth_mode.value != "api_key":
        raise HTTPException(status_code=400, detail=f"{key} does not use API key auth")

    api_key = req.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key must not be empty")

    conn = await conn_mod.create_connection_vault(
        provider_key=key,
        user_id=_entity_user_id(),
        credentials_payload={"api_key": api_key},
        connection_config={"display_name": req.display_name or p.display_name},
        status=ConnectionStatus.ACTIVE,
    )
    return {"ok": True, "connection_id": conn.id}


class OAuthClientRequest(BaseModel):
    client_id: str
    client_secret: str
    label: str | None = None


def _known_families() -> set[str]:
    """Families named by at least one installed manifest. Registering a client
    for a family nothing authorizes against is a typo, not a feature."""
    return {(p.auth or {}).get("family", key)
            for key, p in manifests.load_all().items()}


@router.get("/families", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def list_families(request: Request):
    """Every OAuth family the installed manifests use, where this host's client
    for it comes from, and the exact redirect URIs to register. Never returns a
    client_secret.

    `source`: 'host' (the user registered their own), 'platform' (served by the
    bridge or the provisioned config file), or 'none' — and 'none' is what makes
    a 503 on authorize explainable instead of mysterious.

    The redirect URIs follow whose client is in use. On the PLATFORM's client
    they are the one callback already registered on it, and
    `redirect_uris_need_registering` is false — there is nothing for the user to
    do. On a HOST-owned client they are built from the origin of this request,
    which is the only place that knows it: the host has no stored browser-facing
    address (entity_endpoints carries a mesh IPv6, not a URL). Registering a
    mistyped one is the classic setup failure, so the host hands over the exact
    string rather than describing its shape.
    """
    host_rows = {r["family"]: r for r in await storage.list_oauth_clients()}
    origin = _dashboard_origin(request)
    providers = manifests.load_all()
    out = []
    from server.integrations.providers import _google_base as _g
    for family in sorted(_known_families()):
        row = host_rows.get(family)
        # RESOLVED, never inferred from the row. `oauth_clients` holding a row
        # does not mean that client is the one being served: _load_host_providers
        # skips one whose vault ref will not open and falls back to the
        # platform's. Reading the row alone made this surface say "configured"
        # while authorize 503'd — the answers have to come from one source or
        # the user is debugging a contradiction.
        source = await oauth.client_source(family) or "none"
        usable = source != "none"
        keys = sorted(k for k, p in providers.items()
                      if (p.auth or {}).get("family", k) == family)
        # WHICH redirect URI is registered depends on WHOSE client is in use, so
        # ask the same function authorize does rather than re-deriving it here.
        # A host riding the platform's client has nothing to register at all —
        # the platform registered its one callback once — and telling that user
        # to paste URIs into a Google console is the setup step that made this
        # look impossible to do.
        #
        # PLATFORM_CALLBACK_BASE is a Google-family fact: it is the URI
        # registered on the platform's Google client. A future non-google family
        # riding a platform client would have its own, so it is not reported
        # here rather than reported wrongly.
        choice = await _g.choose_client(family, origin)
        on_platform = choice.on_platform and family == "google"
        out.append({
            "family": family,
            "source": source,
            "configured": usable,
            # Set only in the one state the two can disagree in: a client is
            # registered but its sealed secret cannot be opened right now, so
            # the platform's is serving in its place.
            "unusable_reason": ("registered client could not be opened from the "
                                "vault; the platform's client is serving instead"
                                if row and source != "host" else None),
            "label": (row or {}).get("label"),
            "updated_at": (row or {}).get("updated_at"),
            "providers": keys,
            # One per provider: Google matches the redirect EXACTLY, and each
            # manifest calls back on its own key.
            "redirect_uris": [_g.redirect_uri(k, origin, platform_client=on_platform)
                              for k in keys],
            # False when the platform's client is in use: these are already
            # registered and there is nothing for the user to do with them.
            "redirect_uris_need_registering": not on_platform,
            # Set when /authorize would refuse from THIS origin, so the setup
            # surface does not read "ready" for a flow that will not start.
            # The case: the platform's client is what would be sent, and this
            # host was opened somewhere its single callback cannot return to.
            "authorize_blocked": choice.refusal,
            "scopes": sorted({s for k in keys
                              for s in ((providers[k].auth or {}).get("scopes") or [])}),
        })
    return {"families": out, "count": len(out)}


@router.put("/families/{family}/client", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def put_family_client(family: str, req: OAuthClientRequest):
    """Register THIS host's own OAuth client for a provider family.

    The path that exists because the platform does not ship a Google client: the
    user creates a project in their own Google Cloud console and hands the
    client_id/secret here, once. Every manifest in the family then authorizes
    against it, and the tokens land in the host vault with a refresh_token — which
    is the part a browser-only token flow cannot do, and why a connection made
    this way survives a page reload.

    The secret is sealed into the vault immediately; only its ref is stored.
    """
    family = family.strip().lower()
    if family not in _known_families():
        raise HTTPException(status_code=404, detail=f"no installed manifest uses family '{family}'")

    client_id = req.client_id.strip()
    client_secret = req.client_secret.strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required")

    ref = await credentials.put_vault(
        {"client_id": client_id, "client_secret": client_secret},
        cred_type="oauth_app_client", actor="integrations:oauth_client",
    )
    if not ref:
        # put_vault returns None when the host vault is unavailable. Storing the
        # secret anywhere else would be a downgrade the user did not consent to.
        raise HTTPException(status_code=503, detail="host vault unavailable; client not stored")

    previous = await storage.delete_oauth_client(family)
    await storage.put_oauth_client(family, ref, label=req.label)
    if previous and previous != ref:
        try:
            await credentials.revoke_vault(previous, actor="integrations:oauth_client")
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("could not revoke superseded client secret %s: %s", previous, e)
    oauth.invalidate_cache()
    logger.info("host oauth client registered for family=%s", family)
    return {"ok": True, "family": family, "source": "host"}


@router.delete("/families/{family}/client", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def delete_family_client(family: str):
    """Forget this host's own client for a family and destroy the sealed secret.

    Existing connections are NOT revoked here: their tokens were issued to this
    client_id and stop refreshing once it is gone, which is a reconnect, not a
    data loss. Revoking the grants as well would be a second, unasked decision.
    """
    family = family.strip().lower()
    ref = await storage.delete_oauth_client(family)
    if ref is None:
        raise HTTPException(status_code=404, detail=f"no host client registered for '{family}'")
    # The row is ALREADY gone, so the cache is already wrong — clearing it
    # cannot wait on the vault destroy succeeding. Unguarded, a revoke_vault
    # that raised left a deleted client being served for up to _CACHE_TTL_S.
    try:
        await credentials.revoke_vault(ref, actor="integrations:oauth_client")
    finally:
        oauth.invalidate_cache()
    return {"ok": True, "family": family, "removed": True}


class GrantRequest(BaseModel):
    #: Optional narrowing, ENFORCED at every call. Omit for every method the
    #: provider allows; ["GET"] is read-only — which is the question a user has
    #: now that Gmail asks for `modify`: may this app only read my mail, or also
    #: send and delete it.
    methods: list[Literal["GET", "POST", "PUT", "PATCH", "DELETE"]] | None = None


@router.get("/connections/{connection_id}/apps", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def list_connection_apps(connection_id: str):
    """Which apps the host let use this connection."""
    if not await storage.get_connection(connection_id):
        raise HTTPException(status_code=404, detail="connection not found")
    grants = await storage.list_grants(connection_id=connection_id)
    return {"connection_id": connection_id, "apps": grants, "count": len(grants)}


@router.put("/connections/{connection_id}/apps/{app_id}", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def grant_connection_to_app(connection_id: str, app_id: str, req: GrantRequest):
    """Let one installed app use one connection.

    THE HOST DECIDES THIS, NOT THE APP. A connection belongs to the host; an app
    asking to use one proves nothing about whether it should. Without a row here
    the broker refuses, so holding `use:integration` grants an app exactly
    nothing until the user says which account it may touch.

    Revoking is its own act (DELETE), separate from uninstalling the app and
    separate from disconnecting the account — the user may want any one of the
    three without the other two.
    """
    row = await storage.get_connection(connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    if row["user_id"] != _entity_user_id():
        raise HTTPException(status_code=403, detail="not owner")
    if not app_lane.valid_app_id(app_id) or app_id in RESERVED_APP_IDS:
        raise HTTPException(status_code=400, detail="a real installed app id is required")

    await storage.grant_app(connection_id, app_id, req.methods)
    logger.info("integration grant: app=%s may use connection=%s methods=%s",
                app_id, connection_id, req.methods or "*")
    return {"ok": True, "connection_id": connection_id, "app_id": app_id,
            "methods": req.methods}


@router.delete("/connections/{connection_id}/apps/{app_id}", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def revoke_connection_from_app(connection_id: str, app_id: str):
    """Take a connection back from an app. The account itself is untouched."""
    if not await storage.revoke_app_grant(connection_id, app_id):
        raise HTTPException(status_code=404, detail="no such grant")
    logger.info("integration grant revoked: app=%s connection=%s", app_id, connection_id)
    return {"ok": True, "connection_id": connection_id, "app_id": app_id, "revoked": True}


class ProviderCallRequest(BaseModel):
    #: A path UNDER the manifest's proxy.base_url. Never a full URL — that is
    #: the whole containment story, see the route docstring.
    path: str = Field(min_length=1, max_length=2000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    query: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


#: Headers an app may not set. Authorization is the point of the broker; the
#: rest are hop-by-hop or host-identity fields aiohttp owns.
_REFUSED_HEADERS = {"authorization", "host", "content-length", "connection",
                    "transfer-encoding", "proxy-authorization"}


@router.post("/connections/{connection_id}/call", dependencies=[Depends(requires_permission("use:integration"))])
async def call_provider(connection_id: str, req: ProviderCallRequest, request: Request):
    """Make one authenticated call to the provider, on the host's connection.

    This is what makes a connection USEFUL to an app. Before it, the framework
    could authorize an account and sync it into a local store that nothing
    exposed — so an app wanting live mail had to run its own OAuth, which is
    exactly what a mail-routing app had to do, and why its users each needed a Google Cloud
    project.

    The app never sees the token. It names a PATH; the host resolves the
    connection, refreshes the access token if it has expired, attaches it, makes
    the call through the governed egress proxy, and returns only the response.
    Token rotation is invisible to the app, which is the other half of why this
    beats a browser flow: nothing to re-authorize on reload.

    Three things bound it, and all three are checked here rather than trusted:

    * WHICH APP — read off the window token, never a parameter.
    * WHICH CONNECTION — only one the host granted this app (integration_grants).
      No row, no call, whatever scopes the app holds.
    * WHERE — `path` is resolved under the manifest's own `proxy.base_url` and
      the result must still be on that origin. An app cannot pass an absolute
      URL, walk out with `..`, or aim a protocol-relative `//evil.test` at
      somewhere else: the access token only ever reaches the provider the
      connection is for.
    """
    tok_obj = getattr(request.state, "app_token", None)
    app_id = getattr(tok_obj, "app_id", None)
    if not app_lane.valid_app_id(app_id):
        raise HTTPException(401, "application token required")
    if app_id in RESERVED_APP_IDS:
        raise HTTPException(403, "the broker is for installed apps")

    row = await storage.get_connection(connection_id)
    if not row:
        raise HTTPException(404, "connection not found")
    if row["user_id"] != _entity_user_id():
        raise HTTPException(403, "not owner")

    grant = await storage.get_grant(connection_id, app_id)
    if grant is None:
        raise HTTPException(
            403,
            f"'{app_id}' has not been granted this connection. The host grants it: "
            f"PUT /api/integrations/connections/{connection_id}/apps/{app_id}")

    # The grant's narrowing, ENFORCED. An earlier draft stored a narrowing and
    # never read it back, so a user who granted a mail app read-only had in fact
    # granted it send and delete — the row said one thing and the code did
    # another. A control the enforcement path ignores is worse than no control,
    # because the user stops looking for the risk they think they closed.
    allowed = grant.get("methods")
    if allowed and req.method not in allowed:
        raise HTTPException(
            403,
            f"'{app_id}' is granted {allowed} on this connection, not {req.method}")

    p = manifests.get(row["provider_key"])
    if not p:
        raise HTTPException(404, "provider manifest not found")
    base_url = ((p.proxy or {}).get("base_url") or "").rstrip("/")
    if not base_url:
        raise HTTPException(501, f"{row['provider_key']} declares no proxy.base_url")

    url = _resolve_under(base_url, req.path)

    access_token = await conn_mod.ensure_fresh_token(connection_id)
    if not access_token:
        raise HTTPException(
            409,
            "this connection needs to be reconnected (no usable token). "
            f"Re-authorize: GET /api/integrations/providers/{row['provider_key']}/authorize")

    headers = {k: v for k, v in (req.headers or {}).items()
               if k.lower() not in _REFUSED_HEADERS}
    if req.query:
        url = f"{url}?{urlencode(req.query, doseq=True)}"

    # The SAME broker the vault uses (server/vault/authz._do_http_call): it
    # injects the secret, rides trust_env so the call exits through the governed
    # host-egress proxy like all third-party traffic, and returns status + body
    # without ever echoing the credential.
    from server.vault.authz import _do_http_call
    try:
        result = await _do_http_call(access_token.encode(), {
            "url": url, "method": req.method, "headers": headers,
            "json": req.body, "auth_scheme": "bearer",
            # NOT the default (True). The whole guarantee of this route is that
            # the app's reach is the provider named by the connection; following
            # a redirect hands the app's own headers and the response body to
            # whatever origin the provider names next, which is outside the
            # containment _resolve_under just established. aiohttp does strip
            # Authorization across origins (3.14.3, client.py:974-978), so the
            # TOKEN is safe either way — this closes the rest. The sibling
            # pinned caller, reach-out, passes it for the same reason
            # (vault/authz.py:256-269).
            "allow_redirects": False,
        })
    except Exception as e:
        logger.warning("integration call failed app=%s conn=%s: %s", app_id, connection_id, e)
        raise HTTPException(502, f"provider call failed: {e}")

    logger.info("integration call app=%s conn=%s %s %s -> %s",
                app_id, connection_id, req.method, req.path, result.get("status"))
    out = {"ok": True, "provider": row["provider_key"], **result}
    # A 3xx is a DEAD END here, and saying so beats handing back a bare status
    # the app can neither follow nor see the target of. Redirects are not
    # followed on purpose (see allow_redirects above), and _do_http_call returns
    # no headers, so there is no Location to pass on. Without this note an app
    # sees "302, empty body" and has nothing to reason about.
    if 300 <= int(result.get("status") or 0) < 400:
        out["note"] = ("the provider redirected; the broker does not follow "
                       "redirects, so this call ended here")
    return out


def _resolve_under(base_url: str, path: str) -> str:
    """Join `path` under `base_url`, or refuse.

    urljoin alone is not enough and is the trap here: it REPLACES the base for
    an absolute URL, and a protocol-relative '//evil.test/x' keeps the scheme
    while swapping the host. Both would hand the provider's access token to
    somewhere the user never connected. So the result is re-parsed and its
    origin compared to the base's — the check is on the URL that will actually
    be called, not on the string that was passed in.
    """
    from urllib.parse import urljoin, urlparse
    if "://" in path or path.startswith("//"):
        raise HTTPException(400, "path must be relative to the provider's base_url")
    candidate = urljoin(base_url + "/", path.lstrip("/"))
    b, c = urlparse(base_url), urlparse(candidate)
    if (c.scheme, c.netloc) != (b.scheme, b.netloc) or not c.path.startswith(b.path):
        raise HTTPException(400, "path escapes the provider's base_url")
    return candidate


@router.get("/providers/{key}/authorize", dependencies=[Depends(requires_permission("write:portal_sources"))])
async def provider_authorize(key: str, request: Request):
    """Begin an OAuth flow. Returns the redirect URL the dashboard should
    send the user's browser to. We don't 302 directly — the dashboard
    opens it in a popup so the user stays in C-Series.
    """
    p = manifests.get(key)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    if p.auth_mode.value != "oauth2":
        raise HTTPException(status_code=400, detail=f"{key} is not OAuth2")

    module = _import_provider(key)
    if not module or not hasattr(module, "build_authorize_url"):
        raise HTTPException(status_code=501,
                            detail=f"{key} does not implement OAuth authorize")

    family = (p.auth or {}).get("family", key)
    origin = _dashboard_origin(request)

    # Ask the SAME function build_authorize_url enforces, so the error the
    # person reads and the decision the code makes cannot drift apart.
    from server.integrations.providers import _google_base as _g
    choice = await _g.choose_client(family, origin)
    if choice.refusal == "no_client":
        # The platform serves a client for this family, so reaching here means
        # the bridge could not hand one over — a platform-side fault, not
        # something the user forgot to do. Say that, and mention the
        # bring-your-own path as the alternative it is rather than as the
        # instruction it used to be: the bare "not configured" read as a fault
        # nobody owned, and sent the first app that hit it off to build its own
        # browser OAuth flow instead.
        raise HTTPException(
            status_code=503,
            detail=(f"the '{family}' connection is temporarily unavailable — this "
                    f"host could not obtain an OAuth client from the platform. "
                    f"Retry shortly. To use your own {family} project instead, "
                    f"PUT /api/integrations/families/{family}/client."))
    if choice.refusal == "unroutable_origin":
        # Refused BEFORE consent. The platform's OAuth client has one registered
        # callback and it can only bounce a code back to a host address; this
        # host was opened on something else, so the flow would have died after
        # the person had already granted Google access.
        raise HTTPException(
            status_code=400,
            detail=(f"this host was opened on {origin}, which the platform's "
                    f"OAuth callback cannot return to. Open it on its public "
                    f"host address and try again, or register your "
                    f"own {family} client with "
                    f"PUT /api/integrations/families/{family}/client."))

    url = await module.build_authorize_url(_entity_user_id(), origin)
    if not url:
        raise HTTPException(status_code=500, detail="failed to build authorize URL")
    return {"authorize_url": url}


@router.get("/callback/{key}")
async def provider_callback(key: str, request: Request,
                             code: str | None = None,
                             state: str | None = None,
                             error: str | None = None):
    """OAuth redirect target. Google redirects the browser here with
    ?code=&state=. We exchange, stash the tokens, then render a small
    HTML page that closes the popup and pings the opener window."""
    if error:
        return HTMLResponse(_oauth_complete_page(error=error), status_code=200)
    if not code or not state:
        return HTMLResponse(_oauth_complete_page(error="missing_params"), status_code=400)

    # Reached without a session, so only an installed provider's module is
    # looked up, never whatever name the path carries.
    if not manifests.get(key):
        raise HTTPException(status_code=404, detail="provider not found")
    module = _import_provider(key)
    if not module or not hasattr(module, "handle_callback"):
        raise HTTPException(status_code=404, detail=f"{key} has no callback handler")

    try:
        result = await module.handle_callback(code, state, _dashboard_origin(request))
    except Exception as e:
        logger.exception("%s callback failed: %s", key, e)
        return HTMLResponse(_oauth_complete_page(error="callback_exception"), status_code=500)

    if not result.get("ok"):
        return HTMLResponse(
            _oauth_complete_page(error=result.get("error", "unknown")),
            status_code=400,
        )

    # Kick off the background sync loop for this connection (non-blocking).
    connection_id = result.get("connection_id")
    if connection_id and hasattr(module, "run_sync_loop"):
        import asyncio
        asyncio.create_task(module.run_sync_loop(connection_id))

    return HTMLResponse(_oauth_complete_page(
        provider=key,
        connection_id=connection_id,
        account=result.get("account_email"),
    ))


def _oauth_complete_page(*, provider: str | None = None,
                          connection_id: str | None = None,
                          account: str | None = None,
                          error: str | None = None) -> str:
    """Minimal HTML page that closes the popup and messages the opener.
    Kept self-contained so we don't bring a template engine in for this."""
    import html
    if error:
        status_label = f"Error: {html.escape(error)}"
        post_msg = {"type": "cseries.integrations.oauth", "ok": False, "error": error}
    else:
        status_label = f"Connected {html.escape(provider or '')}"
        if account:
            status_label += f" · {html.escape(account)}"
        post_msg = {
            "type": "cseries.integrations.oauth",
            "ok": True,
            "provider": provider,
            "connection_id": connection_id,
            "account": account,
        }
    import json as _json
    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Privatae Host · Integration</title>
<style>
 body {{font:14px ui-sans-serif,system-ui; background:#0d0d12; color:#e9e5d9;
        display:flex; align-items:center; justify-content:center; height:100vh; margin:0;}}
 .box {{padding:20px 28px; border:1px solid #1f1f25; border-radius:6px;
        background:#131319; max-width:360px; text-align:center;}}
 .sys {{font-size:11px; letter-spacing:0.12em; text-transform:uppercase;
        color:#9a9587; margin-bottom:10px;}}
 .msg {{font-size:15px;}}
 .hint {{font-size:12px; color:#7a766a; margin-top:12px;}}
</style></head>
<body><div class=\"box\">
 <div class=\"sys\">Integration</div>
 <div class=\"msg\">{status_label}</div>
 <div class=\"hint\">This window will close automatically.</div>
</div>
<script>
 try {{
   if (window.opener) window.opener.postMessage({_json.dumps(post_msg)}, window.location.origin);
 }} catch(e) {{}}
 setTimeout(() => window.close(), 1200);
</script></body></html>"""


@router.get("/connections/{connection_id}", dependencies=[Depends(requires_permission("read:portal_sources"))])
async def get_connection_detail(connection_id: str):
    row = await storage.get_connection(connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    if row["user_id"] != _entity_user_id():
        raise HTTPException(status_code=403, detail="not owner")
    provider = manifests.get(row["provider_key"])
    syncs = await storage.list_syncs(connection_id)
    return {
        "id": row["id"],
        "provider_key": row["provider_key"],
        "provider_name": provider.display_name if provider else row["provider_key"],
        "status": row["status"],
        "connection_config": row.get("connection_config", {}),
        "created_at": row.get("created_at"),
        "last_sync_at": row.get("last_sync_at"),
        "last_error": row.get("last_error"),
        "syncs": syncs,
    }
