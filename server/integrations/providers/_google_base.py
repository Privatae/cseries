"""
Shared base for Google providers (Gmail, Drive, Calendar, Contacts, YouTube).

Every Google provider has the same OAuth plumbing (same client family,
same authorize/token endpoints, same refresh semantics, same revoke path).
What differs is the cursor strategy, the list-since call, and the canonical
record shape. Those live in the individual provider modules.

This module gives the per-provider modules three reusable pieces:

  build_authorize_url(provider_key, user_id, origin) -> str
  handle_callback(provider_key, code, state, origin,
                   after_exchange=optional) -> dict
  ensure_fresh_token(connection_id) -> access_token | None
  revoke_google(connection_id) -> dict
  run_sync_loop(connection_id, sync_fn, interval_s) -> never returns

Each provider module also supplies its own `sync(connection_id)` doing
the actual per-tick API calls and signal emission.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, NamedTuple
from urllib.parse import urlsplit

from server.integrations.core import (
    connection as conn_mod,
    egress,
    manifests,
    oauth,
    storage,
)
from server.integrations.core.models import ConnectionStatus

logger = logging.getLogger("privatae.pc.integrations.google_base")

GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: The ONE callback registered on the platform's Google OAuth client.
#:
#: Google matches redirect URIs exactly and accepts no wildcard, so a
#: platform-owned client cannot name every `<handle>.<host zone>`. It names
#: this instead; the platform route validates the handle carried in `state` and
#: bounces the browser — code and state untouched — to the Host that asked. The
#: Host still performs the token exchange itself, so no token is ever created or
#: held outside the user's own pod.
PLATFORM_CALLBACK_BASE = "https://privatae.ai/api/integrations/callback"

def _zone_suffixes() -> tuple[str, ...]:
    """Public zones a Host's browser address can live on, CANONICAL FIRST — the
    same list the router serves. No default: unset, a Host has no public
    address and bounce_target answers None.

    Every zone listed stays served, so the bounce must return to the one the
    person actually opened: each zone is gated by its own Authentik proxy provider with
    its own cookie domain, and landing them on the other one drops them at a
    sign-in screen in the middle of connecting their mail.

    Every entry is forced to start with a dot. Without that, an entry of
    `host.example.test` would make `evilhost.example.test` pass `endswith` — a
    suffix test on a hostname is only a domain test if it includes the label
    boundary.
    """
    raw = os.environ.get("HOST_ZONE_SUFFIXES", "")
    out = []
    for part in raw.split(","):
        s = part.strip().lower()
        if s:
            out.append(s if s.startswith(".") else "." + s)
    return tuple(out)


HOST_ZONE_SUFFIXES: tuple[str, ...] = _zone_suffixes()

#: A routing handle is the leftmost DNS label of a Host's address.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")

#: The router addresses each pod upstream as `entity-{eid}.<host zone>`.
#: That is a valid DNS label on a served zone, so
#: it passes every check below — but it is never a routing handle (those are 16
#: hex characters from secrets.token_hex(8)) and the registry has no such row.
#: Left alone it would fail closed at the platform callback, which is to say
#: AFTER the person had already granted Google consent.
_UPSTREAM_LABEL_PREFIX = "entity-"


def bounce_target(dashboard_origin: str) -> str | None:
    """The browser hostname to carry in `state`, or None if this Host has no
    public address for the platform's callback to bounce back to.

    THE ADDRESS THE PERSON IS ON IS THE ANSWER. They reached it, so the router
    resolved it, so it is a live label in `synaptive_handle_registry` — no
    lookup, no registry read, and nothing to keep in step as handles change.
    Deriving it instead from "this entity's handle" would pick a row that may
    not be the one they are browsing, and would go stale the moment it moved.

    The whole hostname travels, not just the label, so the bounce returns to the
    zone the flow started on.
    """
    host = (urlsplit(dashboard_origin).hostname or "").lower()
    for suffix in HOST_ZONE_SUFFIXES:
        if host.endswith(suffix):
            label = host[: -len(suffix)]
            if not _LABEL_RE.match(label):
                return None
            if label.startswith(_UPSTREAM_LABEL_PREFIX):
                return None
            return host
    return None


class ClientChoice(NamedTuple):
    """Which OAuth client this authorize will use, and whether it can proceed.

    ONE derivation, two readers: the route turns `refusal` into an HTTP status
    the person can act on, and `build_authorize_url` enforces it. Splitting that
    across two call sites is how the redirect URI and the client_id came to
    disagree in the first place.
    """

    on_platform: bool
    state_prefix: str | None
    #: None to proceed. 'no_client' or 'unroutable_origin' otherwise.
    refusal: str | None


async def choose_client(family: str, dashboard_origin: str) -> ClientChoice:
    """Pick the OAuth client for this authorize, from the SAME resolved table
    that supplies the credentials.

    The redirect URI must belong to whichever client's `client_id` is actually
    sent, and only `oauth.client_source` knows which that is. Deciding from a
    row in `oauth_clients` gets it wrong whenever a registered client's sealed
    secret will not open: the credentials fall back to the platform's while the
    row still exists, and the pair Google receives does not match.
    """
    source = await oauth.client_source(family)
    if source is None:
        return ClientChoice(False, None, "no_client")
    if source == "host":
        # Registered its own origin with Google, so it keeps the direct
        # callback and needs no bounce.
        return ClientChoice(False, None, None)
    prefix = bounce_target(dashboard_origin)
    if not prefix:
        # The platform's client is what would be sent, and there is nowhere for
        # its single callback to bounce a code back to — a Host opened on
        # localhost, a port-forward, or an IP. Refuse here, BEFORE consent,
        # rather than letting the person grant Google access and then land on a
        # redirect_uri_mismatch page that names no cause.
        return ClientChoice(True, None, "unroutable_origin")
    return ClientChoice(True, prefix, None)


def redirect_uri(provider_key: str, dashboard_origin: str,
                 *, platform_client: bool) -> str:
    """Callback URL Google redirects to after consent. Must match the URI
    registered on the OAuth client in use — and WHICH client that is decides
    which URI is right, so this takes the answer rather than guessing.

    A Host that registered its OWN client registered its own origin with Google,
    so it keeps the direct callback. A Host riding the platform's client must
    send the platform's single registered URI or Google refuses the authorize
    request with redirect_uri_mismatch.

    Google requires the value sent at token exchange to match the one sent at
    authorize, so both paths read it from here.
    """
    if platform_client:
        return f"{PLATFORM_CALLBACK_BASE}/{provider_key}"
    return f"{dashboard_origin}/api/integrations/callback/{provider_key}"


async def bearer_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


# ── Authorize URL ──────────────────────────────────────────

async def build_authorize_url(
    provider_key: str, user_id: str, dashboard_origin: str,
) -> str | None:
    p = manifests.get(provider_key)
    if not p:
        return None
    auth_cfg = p.auth or {}
    family = auth_cfg.get("family", "google")

    # The Host's address rides on the FRONT of state, outside the signed blob,
    # because the platform callback has to read it without holding this pod's
    # signing key. It is not a secret (it is the address in the person's URL bar)
    # and it is not trusted: the platform only uses it to pick a destination it
    # validates against the handle registry, and the signed blob inside still
    # has to verify HERE. A forged address therefore routes a code to a Host
    # that rejects it.
    choice = await choose_client(family, dashboard_origin)
    if choice.refusal:
        logger.warning("refusing authorize for family=%s (%s): %s",
                       family, provider_key, choice.refusal)
        return None
    on_platform, prefix = choice.on_platform, choice.state_prefix

    url = await oauth.authorize_url(
        family=family,
        authorize_endpoint=auth_cfg.get(
            "authorize_endpoint", "https://accounts.google.com/o/oauth2/v2/auth"),
        scopes=list(auth_cfg.get("scopes") or []),
        redirect_uri=redirect_uri(provider_key, dashboard_origin,
                                  platform_client=on_platform),
        state_payload={"provider": provider_key, "user_id": str(user_id)},
        extra_params=auth_cfg.get("extra_authorize") or {},
        state_prefix=prefix,
    )
    return url


# ── Callback ───────────────────────────────────────────────

async def handle_callback(
    provider_key: str, code: str, state: str, dashboard_origin: str,
    after_exchange: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Verify state, exchange code for tokens, create connection.

    after_exchange(connection_id, tokens) is called once the connection
    row exists. Providers use it to prime their cursor (Gmail historyId,
    Drive startPageToken, etc.).
    """
    payload = oauth.verify_state(state)
    if not payload or payload.get("provider") != provider_key:
        return {"ok": False, "error": "invalid_state"}

    p = manifests.get(provider_key)
    if not p:
        return {"ok": False, "error": "provider_unavailable"}

    auth_cfg = p.auth or {}
    family = auth_cfg.get("family", "google")
    token_endpoint = auth_cfg.get(
        "token_endpoint", "https://oauth2.googleapis.com/token")

    # WHICH redirect URI is decided by the STATE, not by asking the resolver
    # again. Google requires the value sent here to equal the one sent at
    # authorize, and between those two moments a Host could have registered its
    # own client — which would flip using_platform_client() and produce a
    # mismatch that reads as "token exchange failed" with nothing naming the
    # cause. The prefix's presence is the record of which URI was actually used.
    came_via_platform = oauth.STATE_PREFIX_SEP in state

    try:
        tok = await oauth.exchange_code(
            family=family,
            token_endpoint=token_endpoint,
            code=code,
            redirect_uri=redirect_uri(provider_key, dashboard_origin,
                                      platform_client=came_via_platform),
        )
    except Exception as e:
        logger.warning("%s token exchange failed: %s", provider_key, e)
        return {"ok": False, "error": "token_exchange_failed"}

    access_token = tok.get("access_token")
    account_email = await _fetch_userinfo_email(access_token) if access_token else None

    user_id = str(payload.get("user_id", "0"))
    connection = await conn_mod.create_connection_vault(
        provider_key=provider_key,
        user_id=user_id,
        credentials_payload=tok,
        connection_config={"account_email": account_email} if account_email else {},
        status=ConnectionStatus.ACTIVE,
    )
    logger.info("%s connection created: %s (%s)", provider_key, connection.id, account_email or "?")

    if after_exchange:
        try:
            await after_exchange(connection.id, tok)
        except Exception as e:
            logger.warning("%s after_exchange hook failed: %s", provider_key, e)

    return {
        "ok": True,
        "connection_id": connection.id,
        "account_email": account_email,
    }


async def _fetch_userinfo_email(access_token: str) -> str | None:
    try:
        async with egress.session() as s:
            async with s.get(
                GOOGLE_USERINFO,
                headers=await bearer_headers(access_token),
                timeout=15,
            ) as r:
                if r.status != 200:
                    return None
                j = await r.json()
                return j.get("email")
    except Exception:
        return None


# ── Token refresh ─────────────────────────────────────────

async def ensure_fresh_token(connection_id: str) -> str | None:
    """Return a valid access_token, refreshing if necessary.

    Delegates to connection.ensure_fresh_token, which is now the single
    implementation — the broker needs the same behaviour and must not import a
    provider-private module to get it. Kept as a name here because every Google
    provider calls `g.ensure_fresh_token`.
    """
    return await conn_mod.ensure_fresh_token(connection_id)


# ── Revoke ─────────────────────────────────────────────────

async def revoke_google(connection_id: str) -> dict[str, Any]:
    """Call Google's revoke endpoint then tear down the local connection."""
    tok = await conn_mod.get_credentials_resolve(connection_id)
    if tok and (tok.get("refresh_token") or tok.get("access_token")):
        try:
            async with egress.session() as s:
                await s.post(
                    GOOGLE_REVOKE,
                    data={"token": tok.get("refresh_token") or tok["access_token"]},
                    timeout=10,
                )
        except Exception as e:
            logger.warning("google revoke API call failed (continuing): %s", e)
    await conn_mod.revoke_vault(connection_id)
    return {"ok": True}


# ── Sync loop runner ──────────────────────────────────────

async def run_sync_loop(
    connection_id: str,
    sync_fn: Callable[[str], Awaitable[dict[str, Any]]],
    interval_s: int = 60,
    provider_key: str = "google",
) -> None:
    """Forever: poll sync_fn every interval_s. Called by scheduler."""
    logger.info("%s sync loop started for %s", provider_key, connection_id)
    while True:
        try:
            result = await sync_fn(connection_id)
            if not result.get("ok"):
                logger.warning("%s sync tick failed: %s", provider_key, result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("%s sync crashed: %s", provider_key, e)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
