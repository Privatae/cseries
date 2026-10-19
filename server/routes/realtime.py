"""
Realtime voice — server-side ephemeral-token mint.

A realtime-voice app runs in the browser and CANNOT hold the provider API key
(browsers can't set an Authorization header on a WebSocket upgrade). The app
POSTs here; this route resolves the user's OWN sealed provider key from the host
vault (server/vault), calls the provider's ephemeral-token endpoint, and returns
ONLY the short-lived client_secret. The raw key never reaches the browser.

Contract — actual xAI API (a voice app's bundled API notes once documented
this wrong as a bare int; that produces HTTP 422 "expected struct
ExpiresAfter" — this was the connect/token-mint failure):
    POST https://api.x.ai/v1/realtime/client_secrets
    Authorization: Bearer <xai:voice_api_key>
    body { "expires_after": {"seconds": <seconds>} }
    -> { client_secret: {...} }   (the app then opens the WS with it)

This is the missing server half of the open-voice-agent substrate: a voice
app's frontend was built against this route, but the route did not exist,
so the browser POST fell through every API router to the SPA static catch-all
(app.mount("/", OverlayStaticFiles())) — StaticFiles answers only GET/HEAD, so a
POST returned a misleading 405. A real router handler matches before the mount.

Identity half (the repeatable voice-agent method):
    GET /api/realtime/instructions  -> { instructions, display_name }
The `instructions` string is THIS host's own voice system prompt (identity preamble +
voice discipline), built from the same prompt_builder leaves the native voice pipeline
uses for a 'voice_call' medium. A voice app's two-call flow is therefore identical for
every user's host: GET /instructions -> session.update(instructions) ; then
POST /client_secrets -> open the provider WS. No voice app needs a
hardcoded persona; each host serves its own. Only the host's PUBLIC persona crosses to
the 3rd-party provider — see prompt_builder.build_voice_system_prompt for the boundary.
"""
from __future__ import annotations

import json as _json
import logging
import os

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, status

from server import brain_service
from server.applications.middleware import requires_permission
from server.vault.api import open_secret

log = logging.getLogger("privatae.realtime")

router = APIRouter(prefix="/api/realtime", tags=["realtime"])

# expires_after clamp — provider ephemeral tokens are deliberately short-lived.
# The browser mints one per session and re-mints on reconnect, so a tight ceiling
# costs nothing and shrinks the exposure window of a leaked ephemeral secret.
_TTL_DEFAULT = 60
_TTL_MIN = 10
_TTL_MAX = 600

# Per-provider realtime config, keyed by provider slug. The key itself is the
# user's OWN BYO credential sealed in the host vault — never embedded here.
#
# `vault_names` is a LOOKUP ORDER, not a single name, because two surfaces seal
# a provider key under different names and only one of them was ever checked:
#
#   "<provider>:voice_api_key"  the explicit voice slot. NOTHING in the product
#                               writes it — no route, no app, no connect flow.
#                               Only a hand-typed Custom key in the Keys app
#                               produces it. Tried first so existing installs
#                               that did exactly that keep working.
#   "<provider>"                the CANONICAL Keys slot
#                               (server/vault/providers.canonical_name), which
#                               is what the Keys app's "Provider key" path
#                               seals — readonly name, and the path its own UI
#                               hint tells users to prefer.
#
# Before this, voice opened ONLY the first form, so a user who connected xAI the
# obvious way sealed "xai" and got a 400 "voice not connected" telling them to
# connect the key they had already connected. That is the likeliest reason a
# working voice app still failed to start a session.
#
# This map is now only the BUILT-IN fallback. Providers are resolved from the
# INSTALLED MANIFESTS first (connection_kind: realtime_voice), so approving a
# provider proposal actually turns it on — see _resolve_provider below. xai stays
# here because it predates the manifest lane and must keep working untouched.
_PROVIDERS: dict[str, dict] = {
    "xai": {
        "vault_names": ("xai:voice_api_key", "xai"),
        "token_url": "https://api.x.ai/v1/realtime/client_secrets",
    },
}
_DEFAULT_PROVIDER = "xai"


def _resolve_provider(slug: str) -> dict | None:
    """Provider config for `slug`, from an installed realtime_voice manifest if
    there is one, else the built-in map above. Returns None if neither has it.

    A promoted manifest is SUFFICIENT: it carries realtime.token_url (the mint
    endpoint this route calls) and the key doubles as the canonical vault slot,
    so no code change is needed to add a provider. Both manifest URLs clear the
    egress allowlist at proposal time (server/proposals/checks.py).

    Built-in entries win on key collision so a promoted manifest can never
    silently repoint an existing provider's token mint at another host.
    """
    slug = (slug or "").strip().lower()
    if slug in _PROVIDERS:
        return _PROVIDERS[slug]
    try:
        from server.integrations.core import manifests as _manifests
        prov = _manifests.get(slug)
    except Exception as e:  # manifests dir absent (dev/tests) — fall through
        log.debug("realtime: manifest lookup failed for %r: %s", slug, e)
        return None
    if prov is None or (prov.connection_kind or "") != "realtime_voice":
        return None
    token_url = ((prov.realtime or {}).get("token_url") or "").strip()
    if not token_url.startswith("https://"):
        # Authoring validates this; a hand-edited manifest could still miss it.
        log.warning("realtime: manifest %r has no usable token_url — ignoring", slug)
        return None
    return {
        # Same lookup order as the built-ins: explicit voice slot, then the
        # canonical Keys slot the "Provider key" path seals under.
        "vault_names": (f"{slug}:voice_api_key", slug),
        "token_url": token_url,
    }


async def stored_voice_provider(pool, entity_id: int) -> str | None:
    """The realtime-voice provider the user CHOSE in the Orchestrator
    (behavioral_params.voice_provider, init-db.sql:67), or None.

    None means "not chosen" and callers must treat it as exactly that. It is
    read here and nowhere defaulted: the only place a fallback is applied is
    mint_client_secret, which says which provider it fell back to.
    """
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
    except Exception as e:
        log.debug("realtime: voice_provider read failed: %s", e)
        return None
    if not row or not row["behavioral_params"]:
        return None
    bp = row["behavioral_params"]
    if isinstance(bp, str):
        try:
            bp = _json.loads(bp)
        except Exception:
            return None
    if not isinstance(bp, dict):
        return None
    p = bp.get("voice_provider")
    return p.strip().lower() if isinstance(p, str) and p.strip() else None


async def available_voice_providers(pool, entity_id: int) -> list[dict]:
    """Every provider that could serve realtime voice, each with whether the
    user actually holds a key for it — the Orchestrator renders a keyless one
    disabled rather than offering a pick that 400s at first speech.

    Sources are the same two _resolve_provider uses: the built-in map and
    installed realtime_voice manifests. `has_key` is a real vault probe over
    the provider's name lookup order, not an inference from the registry.
    """
    slugs = list(_PROVIDERS)
    try:
        from server.integrations.core import manifests as _manifests
        for slug, prov in (_manifests.load_all() or {}).items():
            if (getattr(prov, "connection_kind", "") or "") == "realtime_voice":
                if slug not in slugs:
                    slugs.append(slug)
    except Exception as e:
        log.debug("realtime: manifest enumeration unavailable: %s", e)

    out: list[dict] = []
    for slug in slugs:
        cfg = _resolve_provider(slug)
        if cfg is None:
            continue
        has_key = False
        if pool is not None:
            for name in cfg["vault_names"]:
                try:
                    if await open_secret(pool, entity_id=entity_id, name=name):
                        has_key = True
                        break
                except Exception:
                    continue
        out.append({"provider": slug, "has_key": has_key})
    return out


class _UpstreamError(Exception):
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        super().__init__(f"{status_code}: {text[:200]}")


async def _mint_upstream(token_url: str, api_key: str, payload: dict) -> dict:
    """Call the provider's ephemeral-token endpoint. trust_env=True routes the
    call through the pod's egress proxy (HTTPS_PROXY) — the only sanctioned
    outbound path (mirrors server/vault/authz.py). Isolated so it is trivially
    mockable in tests."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
        async with s.post(
            token_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        ) as resp:
            text = await resp.text()
            if resp.status // 100 != 2:
                raise _UpstreamError(resp.status, text)
            try:
                return await resp.json()
            except Exception:
                return _json.loads(text)


def _clamp_ttl(raw) -> int:
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        ttl = _TTL_DEFAULT
    return max(_TTL_MIN, min(_TTL_MAX, ttl))


@router.post("/client_secrets")
async def mint_client_secret(
    request: Request,
    _: None = Depends(requires_permission("execute:inference")),
) -> dict:
    """Mint a short-lived provider ephemeral token for a browser realtime-voice
    session. The user's sealed key stays server-side; only the ephemeral
    client_secret is returned."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    ttl = _clamp_ttl(body.get("expires_after"))

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    entity_id = int(os.environ.get("ENTITY_ID", "0") or 0)

    # Provider precedence (2026-08-31): what the caller asked for, then what the
    # user CHOSE in the Orchestrator, then the built-in default. The middle rung
    # is new — until it existed the choice was _DEFAULT_PROVIDER ("xai") and a
    # user holding a key for any other realtime provider had no way to say so,
    # so voice just failed for them. The pool read moved above this block
    # because the stored choice needs it.
    asked = str(body.get("provider") or "").strip().lower()
    # Read the stored choice under the entity id the CONFIG was written with
    # (brain_service._entity_id — what /api/inference/voice uses), not the
    # ENTITY_ID env this function resolves KEYS under. They are both 1 on a
    # personal host, but where a relay config makes them differ, reading under
    # the wrong one silently ignores the user's choice (review N4). Keys stay
    # on entity_id: that is this file's own long-standing vault convention.
    _cfg_eid = getattr(brain_service, "_entity_id", entity_id) or entity_id
    chosen = await stored_voice_provider(pool, _cfg_eid)
    provider = asked or chosen or _DEFAULT_PROVIDER
    cfg = _resolve_provider(provider)
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown realtime provider {provider!r} — no built-in entry and no "
            f"installed realtime_voice manifest declaring it",
        )

    # Resolve the user's OWN sealed provider key (host-owned envelope, opened
    # in-pod with the host key). Never returned to the caller. Tries each name
    # in _PROVIDERS[...]["vault_names"] in order — see the note there for why
    # there is more than one.
    key = None
    for _name in cfg["vault_names"]:
        key = await open_secret(pool, entity_id=entity_id, name=_name)
        if key:
            log.info("%s realtime: resolved vault key %r (entity=%d)",
                     provider, _name, entity_id)
            break
    if not key:
        _tried = " or ".join(repr(n) for n in cfg["vault_names"])
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider} voice not connected — no sealed key for this host "
            f"under {_tried}. Add your {provider} key in the Keys app "
            f"(Provider key seals it as {cfg['vault_names'][-1]!r}).",
        )
    api_key = (
        key.decode().strip() if isinstance(key, (bytes, bytearray)) else str(key).strip()
    )

    try:
        # xAI expects expires_after as an OBJECT {seconds: N}, NOT a bare int
        # (a bare int → HTTP 422 "expected struct ExpiresAfter"). anchor is
        # unsupported by xAI. Verified live against api.x.ai 2026-07-14.
        data = await _mint_upstream(cfg["token_url"], api_key, {"expires_after": {"seconds": ttl}})
    except _UpstreamError as e:
        log.warning("%s client_secrets mint rejected: %s %s", provider, e.status_code, e.text[:200])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"{provider} token mint rejected ({e.status_code})"
        ) from None
    except aiohttp.ClientError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"{provider} token mint unreachable: {e}"
        ) from None

    log.info("minted %s realtime ephemeral token (entity=%d, ttl=%ds)", provider, entity_id, ttl)
    # Pass the provider response through unchanged — its whole purpose is to be
    # handed to the browser. The app accepts {client_secret:{value}} | {client_secret} | {value}.
    return data


@router.get("/instructions")
async def voice_instructions(
    request: Request,
    _: None = Depends(requires_permission("read:brain_personality")),
) -> dict:
    """Return this host's realtime-voice system prompt (identity + voice discipline),
    built from THIS entity's own registry identity. A browser realtime-voice app GETs
    this and passes it as the provider session's `instructions` (xAI/OpenAI realtime),
    so the model speaks AS this host instead of a persona hardcoded into the app — the
    repeatable method for any user's voice agent. Public persona only (see
    build_voice_system_prompt): no memory, user data, or tool inventory crosses to the
    3rd-party provider."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    entity_id = int(os.environ.get("ENTITY_ID", "0") or 0)
    row = await pool.fetchrow(
        "SELECT display_name, master_directive, current_weights, seed_weights, "
        "behavioral_params, inversion_mode FROM entity_registry WHERE id = $1",
        entity_id,
    )
    if not row or not (row["display_name"] and row["master_directive"]):
        # Identity is the whole point — fail loud rather than hand the provider a
        # half-empty "You are None" prompt.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "host identity not provisioned (missing display_name/master_directive)",
        )

    # Lazy import (matches synaptive_handler): the prompt builders live in the brain
    # package, imported at call time so the route module loads before brain is on-path.
    from mastercode.runtime.prompt_builder import build_voice_system_prompt

    instructions = build_voice_system_prompt(dict(row))
    log.info(
        "served realtime voice instructions (entity=%d, %d chars)", entity_id, len(instructions)
    )
    return {
        "instructions": instructions,
        "display_name": (row["display_name"] or "").strip(),
    }
