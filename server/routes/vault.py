"""
Credential vault — HTTP API.

Host-owned secrets (WG keys, mTLS material, bastion bearers, tokens, ...): store a
value sealed to THIS host, list metadata, read it back in-pod, rotate by
re-storing, revoke, and add an opt-in break-glass recipient. The platform stores
ciphertext + the host's public key only — it cannot read these.

Scopes: read:vault / write:vault. The dashboard + the network app call these.
Cross-identity USE (one identity using another identity's credential) is gated by
the bridge-signed permission slip in server/vault/authz.py — not this router.
"""
from __future__ import annotations

import base64
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import brain_service
from server import trusted_proxies
from server.applications.middleware import requires_any_permission, requires_permission
from server.network import nac_stepup
from server.operator_identity import request_email
from server.vault import api as vault
from server.vault import authz
from server.vault import identity as vault_identity
from server.vault import providers as vault_providers

logger = logging.getLogger("privatae.pc.vault")

router = APIRouter(prefix="/api/vault", tags=["vault"])


def _actor(request: Request, action: str) -> str:
    """Audit actor string recording WHO (the router-verified operator email) and
    FROM WHERE (client IP) triggered a vault op — the provenance the 2026-07-17
    cross-tenant incident had no way to record. Lands verbatim in
    vault_audit.actor (server/vault/store.py). The address is believed only
    from a trusted proxy, and through remote access it reads "remote"
    (trusted_proxies.audit_source)."""
    email = request_email(request.headers) or "unknown"
    ip = trusted_proxies.audit_source(request)
    return f"user:{email} ip:{ip} {action}"


def _entity_id() -> int:
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        raise HTTPException(503, "ENTITY_ID not set; vault unavailable")
    return int(eid)


def _pool():
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain_not_initialized")
    return pool


def _bust_provider_caches(name: str) -> None:
    """A vault write on a provider's canonical entry ('<provider>' or
    '<provider>-oauth') must show on the Orchestrator's cached surfaces
    immediately, not at TTL lapse. Best-effort like the harness hook below."""
    n = (name or "").strip().lower()
    pid = n[:-6] if n.endswith("-oauth") else n
    if pid not in vault_providers.PROVIDERS:
        return
    try:
        from server.routes import inference as inference_routes
        inference_routes.invalidate_provider_caches(pid)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("vault: provider-cache bust for %r failed: %r", name, e)


class StoreReq(BaseModel):
    name: str
    cred_type: str = "opaque"
    value_b64: str
    # True = refuse (409) if a key of this name already exists, instead of
    # superseding it. The Circles ticket card sets it (2026-09-24): a
    # credential ask names a NEW key at create time, but a key stored under
    # that name between the ask and the user's paste must not be replaced by
    # the paste. Replacing a key is the user's own act in the Keys app.
    if_absent: bool = False


class RegisterUserKeyReq(BaseModel):
    user_id: str
    pubkey_b64: str  # raw 32-byte X25519 public key, base64
    # The user's second factor. Registering a recipient key decides who can open
    # everything sealed TO that user from then on; it is the user's own act.
    stepup_code: str
    # A second key must be asked for, not slipped in: register_pubkey upserts,
    # and everything already sealed to the OLD key stays sealed to it.
    replace: bool = False


class SealForUserReq(BaseModel):
    user_id: str
    name: str
    cred_type: str = "opaque"
    value_b64: str


class BreakGlassReq(BaseModel):
    recipient_pubkey_b64: str  # raw 32-byte X25519 public key, base64


class RevealReq(BaseModel):
    stepup_code: str  # TOTP code — the ONLY gate that returns raw bytes


class UseReq(BaseModel):
    action: str
    request: dict | None = None


@router.get("/secrets", dependencies=[Depends(requires_permission("read:vault"))])
async def list_secrets() -> dict:
    rows = await vault.list_secrets(_pool(), entity_id=_entity_id())
    return {
        "secrets": [
            {
                "name": r["logical_name"],
                "cred_type": r["cred_type"],
                "created_at": r["created_at"].isoformat(),
                # Deactivated = stored but switched off (temporary; reversible
                # from the UI). Consumers of this list must read it as absent.
                "deactivated": bool(r.get("deactivated_at")),
            }
            for r in rows
        ]
    }


def _oauth_slot(provider: str, login: dict, rows: list, active: list) -> dict:
    """The provider's OAuth slot. The HOST fills this one — by running the
    vendor's own login through the harness lane — so it carries source 'host'
    and the Keys app offers no Add on it: OAuth lights up once a harness has
    authenticated through the host, so there is no separate login service.

    Two things can fill it, and both count:
      - the login artifact the host sealed (harness:claude_oauth,
        harness:codex:credentials, harness:grok:auth) — the live path;
      - the legacy canonical '<provider>-oauth' row, or a fuzzy free-typed
        oauth name, from when this slot still had an Add button. Those still
        resolve for routing (vault/providers.py::resolve walks oauth_token), so
        dropping them from this read would blank a working credential off the
        screen while it kept working underneath.

    `backed_by` names the row actually behind the slot, so the payload never
    claims a name that does not exist. `deactivated` mirrors the api_key slot:
    off is off, and it suppresses `stored` rather than sitting alongside it."""
    canon = vault_providers.canonical_name(provider, "oauth_token")
    login_name = login["vault_name"]
    # An ACTIVE candidate outranks a deactivated one, whatever the order.
    # Both can exist at once — a legacy pasted '<provider>-oauth' beside a
    # host-sealed login artifact — and picking the login row just because it is
    # listed first made the slot report the state of a row the resolver had
    # already skipped: switch the sign-in off while the legacy paste stays on,
    # and the slot read "Off" while resolve()'s oauth ladder still served the
    # paste. That is the app-says-one-thing / routing-says-another split this
    # endpoint exists to prevent, so `deactivated` may only describe the row
    # that is actually answering.
    present = [c for c in (login_name, canon)
               if any(r["logical_name"] == c for r in rows)]
    backed_by = next(
        (c for c in present if any(r["logical_name"] == c for r in active)),
        present[0] if present else "",
    )
    deactivated = bool(backed_by) and any(
        r["logical_name"] == backed_by and r.get("deactivated_at") for r in rows
    )
    stored = (not deactivated) and (
        any(r["logical_name"] in (login_name, canon) for r in active)
        or any(
            vault_providers.matches_provider(provider, "oauth_token", r["logical_name"])
            and vault_providers.stored_type_serves(r.get("cred_type"), "oauth_token")
            for r in active
        )
    )
    out = {
        "cred_type": "oauth_token",
        "name": backed_by or login_name,
        "stored": stored,
        "deactivated": deactivated,
        "source": "host",
        "harness": login["harness"],
        "harness_name": login["harness_name"],
    }
    # The one sign-in Keys may START itself: a sign-in a
    # Host can run its model calls on (vault/providers.signin_harness — xai
    # today), so a user whose only subscription is that one can get a Host
    # model without first having a Host to ask. The button posts to
    # /api/vault/providers/{provider}/signin below; every other sign-in slot
    # stays the chip the Host fills.
    if vault_providers.signin_harness(provider) == login["harness"]:
        out["signin_from_keys"] = True
        out["vendor"] = vault_providers.vendor_name(provider)
    return out


async def _keys_signin_button(provider: str, slot: dict) -> None:
    """The button a `signin_from_keys` slot carries, and why. Sets
    signin_state  'none' | 'off' | 'live' | 'expired' | 'unreadable'
    signin_action 'signin' (none, off) | 'reconnect' (anything stored)
    signin_label  vault_providers.signin_button_label — the exact words every
                  message that sends the user here quotes (no_credential_,
                  signin_expired_, signin_unreadable_, signin_refused_message).

    A stored sign-in is judged the way a Host's model call judges it,
    vault_providers.resolve_signin over the sealed row: an expired or
    unreadable one still reads as stored, and that is exactly when the user
    needs the button back. A live one keeps Reconnect too:
    xAI refusing a token (HTTP 401/403 on a model call) is invisible here,
    and its message sends the user to this same button. resolve_signin opens
    the sealed row server-side (one audited vault read per listing, actor
    'keys:signin-state'); only the state leaves this function, never a byte
    of the grant, and it never writes the vault."""
    if slot.get("deactivated"):
        state = "off"
    elif not slot.get("stored"):
        state = "none"
    else:
        rc = None
        try:
            rc = await vault_providers.resolve_signin(
                _pool(), _entity_id(), provider, actor="keys:signin-state")
        except Exception as e:  # noqa: BLE001 — type only: never a byte of the grant
            logger.info("vault: sign-in state read for %s failed: %s",
                        provider, type(e).__name__)
        source = getattr(rc, "source", None)
        # None with the slot lit = only a legacy '<provider>-oauth' row is
        # there; a Host's model call sees no sign-in at all, so: sign in.
        state = {"signin": "live", "signin-expired": "expired",
                 "signin-unreadable": "unreadable"}.get(source, "none")
    action = "signin" if state in ("none", "off") else "reconnect"
    slot.update(signin_state=state, signin_action=action,
                signin_label=vault_providers.signin_button_label(provider, action))


@router.get("/providers", dependencies=[Depends(requires_permission("read:vault"))])
async def list_providers() -> dict:
    """Read-only view of the typed-credential registry (server/vault/providers
    PROVIDERS) for the Keys UI: each provider, its wire, and its canonical slot
    names with whether each slot currently holds an active credential.

    Each slot carries a `source`, and it decides whether the Keys app may offer
    an Add button at all:
      'user' — the api_key slot. The user pastes it; routing runs on it.
      'host' — the OAuth slot. The HOST fills it, by running the vendor's own
               login through the harness lane, so the UI offers NO Add (OAuth
               lights up once a harness has authenticated through the host; there
               is no separate login service).
               See _oauth_slot above.

    A provider gets an OAuth slot ONLY if some harness login can fill it
    (harness_state.oauth_login_slots — anthropic/openai/xai/google today, via
    Claude Code / Codex / Grok / Antigravity CLI; that map is derived, so this
    sentence is the thing that goes stale, never the behaviour).
    MARKETPLACE providers (kind 'generic' — OpenRouter,
    DeepInfra, Together, Fireworks, Groq — resellers of everyone's models) never
    get one: there is no OAuth surface to sign into at a reseller. Neither does a
    first-party provider with no OAuth harness
    (kimi) — a slot nothing can fill is a control the user can only stare at,
    the same dishonesty as the menu that read "Autofill 0" beside two sites.
    `stored` is resolver-truth, and is computed the way the resolver computes
    it: an exact canonical name (opened by name, no type check — _resolve_typed
    does the same), OR a fuzzy free-typed name whose STORED cred_type can serve
    the slot (matches_provider + stored_type_serves, the resolver's own fuzzy
    rung). A deactivated row counts on NEITHER side — off is off — and is
    reported per-slot via `deactivated` so the UI can offer re-activation.

    The type half used to be missing here, so a row like 'harness:kimi:provider'
    (cred_type 'harness_kimi') lit the Keys slot while resolve() skipped it —
    the app showed a key the Orchestrator could not use. Both sides now share
    stored_type_serves; they cannot drift apart again.

    Metadata only — NEVER values. This is a READ: it must not call resolve(),
    whose fuzzy rung self-heals by re-storing under the canonical name (a
    listing endpoint must not mutate the vault). Same gate as GET /secrets.
    The one sealed read it makes is resolve_signin for the sign-in Keys can
    start (_keys_signin_button), which never writes and returns only a state."""
    rows = await vault.list_secrets(_pool(), entity_id=_entity_id())
    active = [r for r in rows if not r.get("deactivated_at")]
    names = [r["logical_name"] for r in active]
    # Which providers have an OAuth slot at all, and which harness login fills
    # each — derived from the harness catalog + the sealing modules, never a
    # second list here. Local import, like every other harness_state caller in
    # this router (the catalog imports routes, so module level would cycle).
    from server.network.harness_state import oauth_login_slots
    oauth_logins = oauth_login_slots()
    providers = []
    for p, spec in vault_providers.PROVIDERS.items():
        kind = spec.get("kind", "generic")
        slot_types = ("api_key",)
        if kind != "generic" and p in oauth_logins:
            slot_types = ("api_key", "oauth_token")
        slots = []
        for ctype in slot_types:
            if ctype == "oauth_token":
                slot = _oauth_slot(p, oauth_logins[p], rows, active)
                if slot.get("signin_from_keys"):
                    await _keys_signin_button(p, slot)
                slots.append(slot)
                continue
            canon = vault_providers.canonical_name(p, ctype)
            deactivated = any(
                r["logical_name"] == canon and r.get("deactivated_at") for r in rows
            )
            # A deactivated CANONICAL row is the resolver's hard stop
            # (_resolve_typed returns the empty 'deactivated' marker before it
            # ever reaches the fuzzy rung), so it must suppress `stored` here
            # too. Without this, a user who switches the canonical entry off
            # while a free-typed alias for the same provider stays active reads
            # 'Stored' in Keys while routing resolves nothing — precisely the
            # app-says-connected/routing-says-no-key split this endpoint exists
            # to prevent. Pinned in scripts/test_credential_resolver.py.
            stored = (not deactivated) and (canon in names or any(
                vault_providers.matches_provider(p, ctype, r["logical_name"])
                and vault_providers.stored_type_serves(r.get("cred_type"), ctype)
                for r in active
            ))
            # `source` tells the UI who fills a slot. 'user' means an Add button
            # is legitimate; 'host' (the OAuth slot below) means the host fills
            # it by running the vendor's login, and the UI must offer no Add at
            # all.
            slots.append({"cred_type": ctype, "name": canon, "stored": stored,
                          "deactivated": bool(deactivated), "source": "user"})
        # `kind` is the provider model (first_party sells its own models and
        # binds to its own builder; generic — 'marketplace' in the UI — resells
        # everyone's). The Keys app groups on it, and it must come from the
        # registry so Keys and the Orchestrator can never disagree about which
        # is which.
        providers.append({"provider": p, "wire": spec["wire"],
                          "kind": kind, "slots": slots})
    return {"providers": providers}


# ── Sign in with xAI, from the Keys app ────────────────────────────────────
# A user whose only subscription is Grok has no model, so no Host to ask
# "connect Grok"; the Host's harness_connections op='login' was the only way to
# start that sign-in. These routes put the same sign-in behind a button in
# Keys. They are thin on purpose: the work is review.harness_login and
# review.harness_login_finish, the path the Host's tool already drives
# (brain/mastercode/runtime/tools.py op='login' / 'login_status') — the
# vendor's own `grok login` run in the user's Grok pod, the pod held while the
# vendor waits on the browser, the grant persisted and sealed. Nothing here
# wakes, holds, releases or persists anything itself; it translates the answer
# into a state and a sentence for the dialog.
#
# Only a provider whose sign-in serves the Host's own model calls
# (vault/providers.signin_harness — xai) is accepted. Every other provider is
# a 404 that starts nothing: the Anthropic and OpenAI sign-ins stay out of
# Keys.
#
# Both routes can outlast the 30 s the router gives a browser request (a cold
# wake, then up to 45 s for the vendor to print its link — harness_core
# make_login's default read window), so they run through long_request: the
# answer when it is ready in time, else 202 + a job the dialog reads at
# GET /api/vault/jobs/{id} under the same write:vault.
#
# And only the KEYS app may call them (_keys_app_only), whatever else holds
# write:vault (Workbench, Browser and Circles do). Starting a sign-in is the
# user's own act on the Keys screen, like the grant there; it holds the
# metered Grok pod for up to 15 minutes; and a finished one can set the chat
# route, a change every other path makes under write:inference_routing.
_SIGNIN_JOB_AREA = "vault-signin"
_SIGNIN_POLL = "/api/vault/jobs/{job}"
_SIGNIN_APP = "keys"


def _keys_app_only(request: Request) -> None:
    """Refuse every caller but the Keys app's own window token. Runs after
    requires_permission('write:vault'), which has already refused a missing
    token (401) or one without the scope (403)."""
    tok = getattr(request.state, "app_token", None)
    if tok is None:
        raise HTTPException(401, "application token required")
    if getattr(tok, "app_id", None) != _SIGNIN_APP:
        raise HTTPException(
            403, "Only the Keys app can start or check a sign-in. Open Keys and "
                 "press the button on the provider's row.")


def _signin_target(provider: str) -> tuple[str, str]:
    """(provider, harness) for a sign-in Keys may start, else a plain 404."""
    p = (provider or "").strip().lower()
    harness = vault_providers.signin_harness(p)
    if not harness:
        raise HTTPException(
            404, "There is no sign-in to start here for this provider. Add its "
                 "API key in the Keys app instead.")
    return p, harness


def _https_link(url) -> str | None:
    """The vendor's link only if it is a plain https URL: it is rendered as a
    link the user clicks, so nothing else is handed on."""
    u = str(url or "").strip()
    if not u.lower().startswith("https://") or any(c.isspace() for c in u):
        return None
    return u


async def _route_chat_if_unrouted(provider: str, pool, eid: int) -> tuple:
    """(routed, note). Route `chat` to the provider just signed in to — ONLY
    when the Host has no chat route at all: resolve_role_route answers
    `unconfigured` (no model_routing entry and no legacy config). A Host that
    already has a chat model keeps it untouched, and routed is None.

    The model: the provider's chat candidates in
    system-apps/orchestrator/recommended-routes.json, in order (web_research.
    recommended_chat_candidates), kept ONLY where the Host model standard
    lists the model eligible (model_standard.check: approved_models.json
    status 'eligible' with its card — whether or not the standard is enforced
    yet), cut further to what the writer accepts once it is enforced
    (model_standard.approved_candidates), and then the first of those the
    provider's live catalog serves on the sign-in just made
    (inference._direct_models). There is no unverified pick: a catalog that
    cannot be read, or serves none of them, routes nothing and the note says
    so. The one-click setup's "first candidate unverified" rule
    (web_research._served_model) is deliberately not used here: nobody is
    looking at a plan preview, and a Host must not start on a model the
    standard does not list eligible. Written through apply_role_route with
    set_by='user': the click on Sign in is the person's act.

    There is deliberately no typed fallback model: a model id literal in
    server code is the pin scripts/audit_pinned_models.py fails on; the ids
    live in that data file."""
    vendor = vault_providers.vendor_name(provider)
    pick = f"Pick a Host model in the Orchestrator app to run on your {vendor} sign-in."
    try:
        from mastercode.inference_config import resolve_role_route
        cur = await resolve_role_route(pool, eid, "chat")
    except Exception as e:  # noqa: BLE001
        logger.info("vault: chat route read after the %s sign-in failed: %r", provider, e)
        return None, "Your Host model was left as it was."
    if not (cur or {}).get("unconfigured"):
        return None, None
    from mastercode.runtime import model_standard, web_research
    cands = [c for c in web_research.recommended_chat_candidates(provider)
             if model_standard.check(provider, c).get("eligible")]
    cands = model_standard.approved_candidates(provider, cands)
    if not cands:
        return None, (f"Your Host model was not set: no recommended {vendor} model meets "
                      f"the Host model standard. {pick}")
    from server.routes import inference as inference_routes
    try:
        rows, err = await inference_routes._direct_models(provider)
    except Exception as e:  # noqa: BLE001
        rows, err = [], type(e).__name__
    served = {str((r or {}).get("id") or "").strip() for r in (rows or [])}
    model = next((c for c in cands if c in served), None)
    if model is None:
        why = (f"{vendor}'s model list could not be read on your sign-in" if err or not rows
               else f"your {vendor} sign-in does not offer {' or '.join(cands)}")
        return None, f"Your Host model was not set: {why}. {pick}"
    try:
        res = await inference_routes.apply_role_route(
            inference_routes.SetRoleModel(role="chat", provider=provider, model=model),
            set_by="user")
    except HTTPException as e:
        return None, f"Your Host model was not set: {e.detail}. {pick}"
    except Exception as e:  # noqa: BLE001
        logger.warning("vault: routing chat to %s after the sign-in failed: %r", provider, e)
        return None, f"Your Host model was not set. {pick}"
    return {"role": "chat", "provider": res.get("provider") or provider,
            "model": res.get("model") or model, "verified": True}, None


async def _signin_landed(provider: str, actor: str) -> dict:
    """The vendor says done: read the sign-in back the way a Host's model call
    will (vault/providers.resolve_signin over the sealed row), so "signed in"
    is an observation, then apply the chat rule above. Never the API key."""
    vendor = vault_providers.vendor_name(provider)
    rc, pool, eid = None, None, 0
    try:
        pool, eid = _pool(), _entity_id()
        rc = await vault_providers.resolve_signin(pool, eid, provider, actor=actor)
    except Exception as e:  # noqa: BLE001 — type only: never a byte of the grant
        logger.warning("vault: reading the %s sign-in back failed: %s",
                       provider, type(e).__name__)
    if rc is None or rc.source != "signin":
        return {"state": "failed", "ok": False, "done": True,
                "error": f"{vendor} finished the sign-in, but your Host could not save "
                         f"it. Start again; if it keeps happening, ask your Host."}
    # The credential behind this provider just changed. Drop its cached
    # catalog and status (failures cache too) so the route below reads the
    # catalog on the new sign-in, not an answer from before it.
    try:
        from server.routes import inference as inference_routes
        inference_routes.invalidate_provider_caches(provider)
    except Exception as e:  # noqa: BLE001 — best-effort, like every cache drop
        logger.info("vault: provider cache drop after the %s sign-in failed: %r",
                    provider, e)
    routed, note = await _route_chat_if_unrouted(provider, pool, eid)
    out = {"state": "signed_in", "ok": True, "done": True, "routed": routed}
    if note:
        out["route_note"] = note
    return out


async def _signin_start(provider: str, harness: str, actor: str) -> dict:
    from server.routes import review
    vendor = vault_providers.vendor_name(provider)
    base = {"provider": provider}
    try:
        res = await review.harness_login(review.HarnessLoginReq(harness=harness))
    except Exception as e:  # noqa: BLE001 — its 503 is the pod not starting
        logger.warning("vault: %s sign-in start failed: %s",
                       provider, getattr(e, "detail", None) or type(e).__name__)
        return {**base, "state": "failed", "ok": False, "done": True,
                "error": f"Your Host could not start the {vendor} sign-in just now. "
                         "Try again in a minute."}
    if res.get("done"):
        if res.get("ok"):
            return {**base, **(await _signin_landed(provider, actor))}
        return {**base, "state": "failed", "ok": False, "done": True,
                "error": f"{vendor} ended the sign-in without signing you in. "
                         "Start again for a fresh link."}
    url = _https_link(res.get("url"))
    if not res.get("ok") or not url:
        return {**base, "state": "failed", "ok": False, "done": False,
                "error": f"The {vendor} sign-in did not give a link to open. "
                         "Try again in a minute."}
    out = {**base, "state": "waiting", "ok": True, "done": False, "url": url}
    code = str(res.get("user_code") or "").strip()
    if code:
        out["user_code"] = code
    return out


async def _signin_finish(provider: str, harness: str, actor: str) -> dict:
    from server.routes import review
    vendor = vault_providers.vendor_name(provider)
    base = {"provider": provider}
    # Not a verdict on the sign-in: harness_login_finish HOLDS the pod on a
    # transport fault, so the dialog keeps checking.
    unreachable = {**base, "state": "unreachable", "ok": False, "done": False,
                   "error": f"Your Host could not reach the {vendor} sign-in just now. "
                            "It will keep checking."}
    try:
        res = await review.harness_login_finish(review.HarnessLoginReq(harness=harness))
    except Exception as e:  # noqa: BLE001 — its 503 is a wake that failed this once
        logger.info("vault: %s sign-in check failed: %s",
                    provider, getattr(e, "detail", None) or type(e).__name__)
        return unreachable
    if res.get("supported") is False:
        return {**base, "state": "failed", "ok": False, "done": False,
                "error": f"Your Host cannot check this {vendor} sign-in from Keys yet. "
                         "Ask your Host to finish connecting it."}
    if res.get("done"):
        if res.get("ok"):
            return {**base, **(await _signin_landed(provider, actor))}
        return {**base, "state": "failed", "ok": False, "done": True,
                "error": f"{vendor} ended the sign-in without signing you in. "
                         "Start again for a fresh link."}
    if not res.get("ok"):
        return unreachable
    if res.get("login_waiting") is False:
        # The sidecar's "no login in flight": the pod was reclaimed or the
        # vendor's wait ran out. Polling on would only wake a fresh pod.
        return {**base, "state": "expired", "ok": False, "done": False,
                "error": f"That {vendor} sign-in is no longer waiting; it may have "
                         "timed out. Start again for a fresh link."}
    return {**base, "state": "waiting", "ok": True, "done": False}


@router.post("/providers/{provider}/signin",
             dependencies=[Depends(requires_permission("write:vault")),
                           Depends(_keys_app_only)])
async def start_provider_signin(provider: str, request: Request):
    """Start the vendor's own sign-in for `provider` (xai only). Answers
    {provider, state, ok, done, url?, user_code?, error?, routed?,
    route_note?}: state 'waiting' carries the link to open (and the one-time
    code when the vendor prints one apart from the link); 'signed_in' when the
    vendor finished at once; 'failed' with a sentence. Or 202 + a job."""
    p, harness = _signin_target(provider)
    actor = _actor(request, f"signin:{p}")
    vendor = vault_providers.vendor_name(p)
    from server.applications import long_request
    return await long_request.run(
        f"the {vendor} sign-in", lambda: _signin_start(p, harness, actor),
        area=_SIGNIN_JOB_AREA, poll=_SIGNIN_POLL,
        message=f"Starting the {vendor} sign-in.")


@router.post("/providers/{provider}/signin/finish",
             dependencies=[Depends(requires_permission("write:vault")),
                           Depends(_keys_app_only)])
async def finish_provider_signin(provider: str, request: Request):
    """Has the user finished in the vendor's page? Polled by the dialog.
    state: 'waiting' | 'unreachable' (keep checking) | 'signed_in' (with
    `routed`: the chat route set on it, or null when chat already had one, and
    `route_note` when chat had none and could not be set) | 'expired' |
    'failed' (stop, with a sentence). Or 202 + a job."""
    p, harness = _signin_target(provider)
    actor = _actor(request, f"signin-finish:{p}")
    vendor = vault_providers.vendor_name(p)
    from server.applications import long_request
    return await long_request.run(
        f"checking the {vendor} sign-in", lambda: _signin_finish(p, harness, actor),
        area=_SIGNIN_JOB_AREA, poll=_SIGNIN_POLL,
        message=f"Checking the {vendor} sign-in.")


@router.get("/jobs/{job_id}", dependencies=[Depends(requires_permission("write:vault")),
                                             Depends(_keys_app_only)])
async def signin_job(job_id: str) -> dict:
    """A sign-in start or check that outlasted its request: {status: running}
    | {status: done, result} | {status: failed, http_status, detail}. Reads
    only this area's jobs, under the scope that started them."""
    from server.applications import long_request
    out = long_request.status(job_id, area=_SIGNIN_JOB_AREA)
    if out is None:
        raise HTTPException(404, "That sign-in request is no longer known (it finished "
                                 "more than 15 minutes ago, or the Host restarted). "
                                 "Start the sign-in again.")
    return out


@router.post("/secrets", dependencies=[Depends(requires_permission("write:vault"))])
async def store_secret(req: StoreReq, request: Request) -> dict:
    try:
        pt = base64.b64decode(req.value_b64)
    except Exception:
        raise HTTPException(400, "value_b64 is not valid base64")
    try:
        vid = await vault.store_secret(
            _pool(), entity_id=_entity_id(), name=req.name,
            cred_type=req.cred_type, plaintext=pt, actor=_actor(request, "put"),
            if_absent=req.if_absent,
        )
    except vault.store.NameTaken:
        # Refused inside the store's own transaction (row locked), so a key
        # stored between the caller's look and this write is not replaced.
        raise HTTPException(
            409, f"a key named {req.name!r} already exists — replace it in the "
                 "Keys app if you mean to, or ask your Host for a different name")
    except vault.SignInName as e:
        raise HTTPException(409, f"{e}: {vault.held_name_words(req.name)['where']}") from None
    out: dict = {"ok": True, "id": vid, "name": req.name}
    _bust_provider_caches(req.name)
    # Storing a credential for a harness is the RECONNECT: it clears the
    # disconnect tombstone a revoke set, so the reconcilers pick the harness up
    # again on their next pass. Without this a disconnected harness could never
    # come back — the tombstone would outlive the new credential.
    try:
        from server.network import harness_state
        harness = await harness_state.reconnect_for_credential(req.name)
        if harness:
            out["reconnected"] = harness
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("vault: harness reconnect for %r failed: %r", req.name, e)
    return out


@router.post(
    "/user-keys",
    dependencies=[Depends(requires_permission("write:vault"))],
)
async def register_user_key(req: RegisterUserKeyReq) -> dict:
    """Register a sovereign USER's X25519 PUBLIC key (32 raw bytes, base64). The
    matching private key NEVER reaches the platform — it lives only in the user's
    mesh. Required before a credential can be sealed TO this user.

    USER-ONLY, by structure (2026-08-18). This row is the recipient of everything
    later sealed to `user:<id>` (POST /seal-for-user, keys_manage seal_for_user),
    and register_pubkey UPSERTS — so whoever can write it can redirect every
    future seal to a key of their choosing. Until today the keys_manage meta-tool
    could (op='register_user_key', no factor), which meant a prompt-injected Host
    could point the user's own sealed credentials at an attacker's key. That op is
    gone; this route takes the user's TOTP step-up (the same factor as a reveal
    and as the backup key routes) and refuses to REPLACE an existing key unless
    told so, exactly like /api/backup/key/register. Also in kernel_ops
    _WALLED_PATHS: never a host op, whatever gets exposed later."""
    pool = _pool()
    user_id = (req.user_id or "").strip()
    if not user_id:
        raise HTTPException(400, "user_id is required")
    if user_id == str(_entity_id()):
        # The OWNER's own key has a wizard: /api/backup/key/{generate,register}
        # writes vault_identity_pubkey AND backup_user_key together, with its own
        # replace consent, so the seal-for-user lane and the backup lane can never
        # point at two different keys for user:<eid>. Registering it here would
        # move one store and not the other. Refuse and say where it lives.
        raise HTTPException(
            409,
            "this is your own key: register or replace it in the Settings app, "
            "Backup tab (POST /api/backup/key/register), which keeps the backup "
            "lane and the seal-for-user lane on the same key")
    if not await nac_stepup.is_enrolled(pool):
        raise HTTPException(409, "enrol a second factor before registering a user key")
    if not await nac_stepup.verify_stepup(pool, req.stepup_code):
        raise HTTPException(403, "invalid step-up code")
    try:
        pub = base64.b64decode(req.pubkey_b64, validate=True)
    except Exception:
        raise HTTPException(400, "pubkey_b64 is not valid base64")
    if len(pub) != 32:
        raise HTTPException(400, "public key must be 32 raw bytes (X25519)")
    ident = vault_identity.user_identity(user_id)
    try:
        existing = await vault_identity.pubkey_for(pool, ident)
    except KeyError:
        existing = None
    if existing is not None and not req.replace:
        if bytes(existing) == pub:
            return {"ok": True, "user_id": user_id, "registered": True,
                    "unchanged": True}
        raise HTTPException(
            409,
            f"a public key is already registered for user {user_id!r}; "
            "everything sealed to it so far stays sealed to it. Send replace=true "
            "if replacing it is what you want.")
    await vault_identity.register_pubkey(pool, ident, pub)
    logger.info("vault: user key %s for %s (%s)",
                "replaced" if existing is not None else "registered", ident,
                "user step-up verified")
    return {"ok": True, "user_id": user_id, "registered": True,
            "replaced": existing is not None}


@router.post(
    "/seal-for-user",
    dependencies=[Depends(requires_permission("write:vault"))],
)
async def seal_for_user(req: SealForUserReq) -> dict:
    """Seal a secret TO a sovereign user: only the user's private key (held in their
    mesh) can open it — the host/platform can NEVER reopen it. The user's public key
    must have been registered via POST /user-keys first."""
    try:
        pt = base64.b64decode(req.value_b64)
    except Exception:
        raise HTTPException(400, "value_b64 is not valid base64")
    try:
        vid = await vault.seal_for_user(
            _pool(), user_id=req.user_id, name=req.name,
            cred_type=req.cred_type, plaintext=pt, actor="user:seal",
        )
    except KeyError:
        raise HTTPException(
            409,
            f"no registered public key for user {req.user_id!r}; POST /api/vault/user-keys first",
        )
    return {"ok": True, "id": vid, "name": req.name,
            "sealed_for_user": req.user_id, "host_can_read": False}


@router.get("/secrets/{name}", dependencies=[Depends(requires_permission("read:vault"))])
async def secret_meta(name: str) -> dict:
    """Metadata for one credential — NEVER the bytes. The raw value is reachable
    only through POST /reveal (TOTP-gated) or brokered through POST /use."""
    rows = await vault.list_secrets(_pool(), entity_id=_entity_id())
    for r in rows:
        if r["logical_name"] == name:
            return {"name": name, "cred_type": r["cred_type"],
                    "created_at": r["created_at"].isoformat()}
    raise HTTPException(404, f"no active secret named {name!r}")


@router.get("/stepup", dependencies=[Depends(requires_permission("read:vault"))])
async def stepup_status() -> dict:
    """Whether a second factor is enrolled — the gate POST /reveal enforces.

    Exists so the UI can ASK before it demands. Without it the Keys app opened a
    6-box authenticator prompt at every Reveal, including for a user who has
    never set a factor up: it asked for a code that could not exist, and only
    discovered the truth from the 409 AFTER they typed six digits. It asked for
    something never set up; it needs a check, then setup if missing. A read, on the
    router that owns the gate — the same fact
    nac_commit already returns as `stepup_enrolled` on the network reads.

    read:vault, not write: this leaks one boolean about the host's own posture,
    and every app that can reach a reveal already holds it."""
    return {"enrolled": await nac_stepup.is_enrolled(_pool())}


@router.post("/secrets/{name}/reveal", dependencies=[Depends(requires_permission("write:vault"))])
async def reveal_secret(name: str, req: RevealReq, request: Request) -> dict:
    """Return the RAW secret value, gated by a TOTP step-up. This is the ONLY path
    that returns bytes, and only to the user who holds the second factor — the
    agent / tool surface (keys_manage) can never reach it. First enroll the factor
    via POST /api/network/nac/stepup/enroll (same factor the Network app uses)."""
    pool = _pool()
    if not await nac_stepup.is_enrolled(pool):
        raise HTTPException(409, "enroll a second factor before revealing a key")
    if not await nac_stepup.verify_stepup(pool, req.stepup_code):
        raise HTTPException(403, "invalid step-up code")
    # The person's own step-up reveal may show an MCP server's sign-in too.
    val = await vault.open_secret(pool, entity_id=_entity_id(), name=name, actor=_actor(request, "reveal"),
                                  include_deactivated=True, sign_in=True)
    if val is None:
        raise HTTPException(404, f"no active secret named {name!r}")
    return {"name": name, "value_b64": base64.b64encode(val).decode("ascii")}


@router.post("/secrets/{name}/use", dependencies=[Depends(requires_permission("write:vault"))])
async def use_secret(name: str, req: UseReq, request: Request) -> dict:
    """Broker a USE of the credential — the bytes never leave the broker; only the
    result of the action is returned (http_call / sign / browser_type)."""
    try:
        result = await authz.use_credential(
            _pool(), entity_id=_entity_id(), name=name, action=req.action,
            request=req.request or {}, actor=_actor(request, f"use:{req.action}"),
        )
    except authz.VaultAuthzError as e:
        raise HTTPException(400, str(e))
    return {"name": name, "action": req.action, "result": result}


class DeactivateReq(BaseModel):
    deactivated: bool


@router.post("/secrets/{name}/deactivate", dependencies=[Depends(requires_permission("write:vault"))])
async def set_secret_deactivated(name: str, req: DeactivateReq, request: Request) -> dict:
    """The temporary off-switch: deactivate keeps the sealed
    envelope but every resolution path (provider resolver, harness materializer,
    brokered use) reads the credential as ABSENT — no re-issue needed to come
    back, unlike revoke. Reactivate restores service in place.

    Deliberately NOT wired to the harness disconnect tombstones: a deactivate
    is reversible by definition, so it must not destroy runtime material the
    re-activate would have to rebuild. A harness whose credential is off simply
    fails to materialize and reads not_set_up in the matrix until re-enabled."""
    ok = await vault.set_secret_deactivated(
        _pool(), entity_id=_entity_id(), name=name, deactivated=req.deactivated,
        actor=_actor(request, "deactivate" if req.deactivated else "activate"))
    if not ok:
        raise HTTPException(404, f"no active secret named {name!r}")
    _bust_provider_caches(name)
    await _git_server_keys_changed(name)
    return {"ok": True, "name": name, "deactivated": req.deactivated}


async def _git_server_keys_changed(name: str) -> None:
    """The connected git server's token, revoked or switched off or on here:
    the Host stops using it now, or takes it up again, rather than at its next
    start (server/git_server.keys_changed)."""
    if vault.is_git_server_token(name):
        from server import git_server
        await git_server.keys_changed(_pool(), _entity_id())


_OWN_HARNESS_SCOPE = "disconnect:own_harness"


def _disconnect_only(request: Request) -> bool:
    """True when the caller reached the delete through disconnect:own_harness alone."""
    tok = getattr(request.state, "app_token", None)
    return tok is not None and "write:vault" not in tok.permissions


def _own_harness_only(request: Request, name: str) -> None:
    """A caller holding only disconnect:own_harness may remove only the sign-in of the
    engine it is. The app id is the engine's app id; only claude's differs from the id
    harness_state files its sign-in under (review._HARNESS_PROVIDER)."""
    if not _disconnect_only(request):
        return
    tok = request.state.app_token
    from server.network import harness_state
    from server.routes.review import _HARNESS_PROVIDER
    app_id = getattr(tok, "app_id", None) or ""
    own = _HARNESS_PROVIDER.get(app_id, app_id)
    if not own or harness_state.harness_for_keys_name(name) != own:
        raise HTTPException(
            403, f"app {app_id!r} may disconnect only its own sign-in, not {name!r}")


@router.delete("/secrets/{name}",
               dependencies=[Depends(requires_any_permission("write:vault", _OWN_HARNESS_SCOPE))])
async def revoke_secret(name: str, request: Request) -> dict:
    """Revoke a credential. When the name belongs to a coding harness, this also
    DISCONNECTS that harness — superseding the envelope alone left the runtime
    file on disk and the reconcilers rebuilt it within one interval, so the
    harness kept working and the revoke was cosmetic.

    write:vault revokes any credential. disconnect:own_harness revokes only the
    calling engine app's own sign-in (_own_harness_only): it is how the Claude Code
    app's Disconnect reaches this full disconnect without holding every key.

    `disconnected` in the response says whether the harness half ran, so the UI
    can tell the user what actually happened rather than assuming.

    An engine app's Disconnect runs the harness half even when there is no Vault
    copy to revoke (`revoked` is then None). The reconcile seals a copy only
    within 300 s of a sign-in, so a Disconnect inside that window finds none, and
    without the marker a run that finishes afterwards writes the credential back.

    For a harness credential the marker is written FIRST, before the Vault copy is
    revoked. If it cannot be written the answer is 503 and nothing is removed: the
    sign-in keeps working and the app says the disconnect failed, instead of the
    Vault copy and file going while the harness signs back in."""
    _own_harness_only(request, name)
    from server.network import harness_state
    try:
        marked, newly = await harness_state.record_disconnect(name)
    except harness_state.DisconnectNotRecorded as e:
        logger.warning("vault: disconnect marker for %r not written; nothing revoked", name)
        raise HTTPException(503, f"could not disconnect {name!r}: {e}")
    ok = await vault.revoke_secret(_pool(), entity_id=_entity_id(), name=name, actor=_actor(request, "revoke"))
    if not ok and not _disconnect_only(request):
        await harness_state.lift_unused_disconnect(marked, newly)
        raise HTTPException(404, f"no active secret named {name!r}")
    if ok:
        _bust_provider_caches(name)
        await _git_server_keys_changed(name)
    out: dict = {"ok": True, "revoked": name if ok else None}
    try:
        harness = await harness_state.disconnect_for_credential(
            name, actor=_actor(request, "revoke"))
        if harness:
            out["disconnected"] = harness
    except Exception as e:  # pragma: no cover - defensive
        # The credential IS revoked; the harness half is best-effort. Say so
        # rather than reporting a clean disconnect we did not achieve.
        logger.warning("vault: harness disconnect for %r failed: %r", name, e)
        out["disconnect_error"] = str(e)[:160]
    if not ok and not out.get("disconnected"):
        # Nothing was revoked and the disconnect did not run: the app must not read
        # this as done.
        raise HTTPException(503, f"could not disconnect {name!r}: "
                                 f"{out.get('disconnect_error') or 'no harness'}")
    return out


@router.post(
    "/secrets/{name}/break-glass",
    dependencies=[Depends(requires_permission("write:vault"))],
)
async def add_break_glass(name: str, req: BreakGlassReq) -> dict:
    try:
        pub = base64.b64decode(req.recipient_pubkey_b64)
    except Exception:
        raise HTTPException(400, "recipient_pubkey_b64 is not valid base64")
    if len(pub) != 32:
        raise HTTPException(400, "recipient public key must be 32 raw bytes (X25519)")
    try:
        await vault.add_break_glass(
            _pool(), entity_id=_entity_id(), name=name, recipient_pubkey=pub
        )
    except KeyError:
        raise HTTPException(404, f"no active secret named {name!r}")
    except vault.SignInName as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True, "name": name, "break_glass_added": True}
