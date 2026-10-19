"""Typed canonical provider credentials — the ONE resolver every credential
consumer uses.

The model: one provider can hold TWO Keys entries, differentiated by type —
    <provider>          cred_type api_key      (e.g. 'anthropic', 'xai')
    <provider>-oauth    cred_type oauth_token  (e.g. 'anthropic-oauth')
Applications declare which types they accept; the HOST selects which entry an
application uses via the per-consumer wiring (behavioral_params.credential_wiring
— JSON, no schema change). This module is the read path for all of it.

Generalizes the proven openrouter ladder (provider_gateway's
_load_user_openrouter_key, which now delegates its vault rungs here):
canonical name first, fuzzy-matched free-typed names self-healed to canonical,
env fallback last. `resolve()` is the one entry point — accepted-types
preference order plus an optional explicit wiring name (the host's
consumer→entry selection, build item 2). Consumers today: the brain's remote
lanes (provider_gateway — openrouter + direct providers), the inference
routes, and the training worker. Intended next: the harness materializer
(harness_keys) and the factory (the Claude-subscription host engine, once
listed here, is deleted). The openrouter resolver's legacy behavioral_params
migration stays IN provider_gateway (openrouter-specific, one-time).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("privatae.vault.providers")

# ── Canonical provider registry ──────────────────────────────────────
# base_url/env overridable per deployment (config-driven, no rebuild); the
# registry is the ONLY place a provider endpoint is named. `wire`:
#   openai    — OpenAI-compatible chat-completions
#   anthropic — Messages API (provider_gateway speaks it directly since the
#               orchestrator v2 routing cut — the SDK lane is gone from
#               production routing; Anthropic is one provider among them,
#               keyed in the Keys app by API key or OAuth token)
# `kind` (the distinction the whole credential/harness mess was missing):
#   first_party — the company sells its OWN models (Kimi, OpenAI, xAI,
#                 Anthropic). A first-party key belongs with its harness:
#                 a Kimi key runs Kimi Code, and Kimi Code runs on nothing else.
#   generic     — a service selling EVERYONE's models (OpenRouter, DeepInfra,
#                 Together, Fireworks, Groq). Generics serve every text role
#                 and exactly the harnesses that have no vendor of their own
#                 (opencode). A first-party harness must NEVER
#                 silently borrow a generic key — that borrowing is how the
#                 platform manufactured a fake "kimi" credential from a user's
#                 OpenRouter key and routed Kimi Code through a reseller.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {"base_url": os.environ.get("OPENROUTER_BASE_URL",
                                              "https://openrouter.ai/api/v1"),
                   "wire": "openai", "env_key": "OPENROUTER_API_KEY",
                   "kind": "generic"},
    # `embeddings`: the OpenAI-wire base also serves POST /embeddings — a UI
    # hint for the Orchestrator's embedding picker. Hint only: every embedding
    # switch is probe-gated (one real embed must return a vector), so a wrong
    # flag fails loudly at pick time, never silently.
    "openai":     {"base_url": os.environ.get("OPENAI_BASE_URL",
                                              "https://api.openai.com/v1"),
                   "wire": "openai", "env_key": "OPENAI_API_KEY",
                   "kind": "first_party", "embeddings": True},
    "xai":        {"base_url": os.environ.get("XAI_BASE_URL",
                                              "https://api.x.ai/v1"),
                   "wire": "openai", "env_key": "XAI_API_KEY",
                   "kind": "first_party"},
    # Google (Gemini) — first-party, so the Keys app offers Google as a
    # provider too. The base is Google's OWN
    # OpenAI-compatibility layer, not the native generateContent API, so it
    # rides the existing `openai` wire with no new transport: POST
    # {base}/chat/completions and {base}/embeddings both serve there
    # (ai.google.dev/gemini-api/docs/openai, read 2026-08-31 — the trailing
    # slash the docs show is stripped by chat_completions_url's rstrip).
    #
    # env_key GEMINI_API_KEY is Google's own name for it AND the one this
    # codebase already materializes for the antigravity harness's key lane
    # (system-apps/_harness_runtime/specs.py:1499) — one name, both lanes.
    # The provider id stays `google` (the vendor) rather than `gemini` (one of
    # its model families): the sign-in slot is filled by a GOOGLE account.
    "google":     {"base_url": os.environ.get(
                       "GEMINI_BASE_URL",
                       "https://generativelanguage.googleapis.com/v1beta/openai"),
                   "wire": "openai", "env_key": "GEMINI_API_KEY",
                   "kind": "first_party", "embeddings": True},
    # Kimi (Moonshot AI) — first-party vendor API. Moonshot API keys work on
    # the subscription plan, so the plain api_key slot serves both.
    "kimi":       {"base_url": os.environ.get("MOONSHOT_BASE_URL",
                                              "https://api.moonshot.ai/v1"),
                   "wire": "openai", "env_key": "MOONSHOT_API_KEY",
                   "kind": "first_party"},
    "anthropic":  {"base_url": os.environ.get("ANTHROPIC_BASE_URL",
                                              "https://api.anthropic.com"),
                   "wire": "anthropic", "env_key": "ANTHROPIC_API_KEY",
                   "kind": "first_party"},
    # Generic providers (OpenRouter is ONE option, never the sole vendor — same
    # model, N providers, cheapest real path). Endpoints are
    # each vendor's documented OpenAI-compatible base; all env-overridable like
    # the rows above.
    "deepinfra":  {"base_url": os.environ.get("DEEPINFRA_BASE_URL",
                                              "https://api.deepinfra.com/v1/openai"),
                   "wire": "openai", "env_key": "DEEPINFRA_API_KEY",
                   "kind": "generic", "embeddings": True},
    "together":   {"base_url": os.environ.get("TOGETHER_BASE_URL",
                                              "https://api.together.xyz/v1"),
                   "wire": "openai", "env_key": "TOGETHER_API_KEY",
                   "kind": "generic", "embeddings": True},
    "fireworks":  {"base_url": os.environ.get("FIREWORKS_BASE_URL",
                                              "https://api.fireworks.ai/inference/v1"),
                   "wire": "openai", "env_key": "FIREWORKS_API_KEY",
                   "kind": "generic", "embeddings": True},
    "groq":       {"base_url": os.environ.get("GROQ_BASE_URL",
                                              "https://api.groq.com/openai/v1"),
                   "wire": "openai", "env_key": "GROQ_API_KEY",
                   "kind": "generic"},
}


def generic_providers() -> tuple:
    """The generic providers (sell everyone's models), in registry order.
    THE source for 'which providers may serve opencode' — derived from
    the registry's own kind flags, never a second hand-kept list that can
    drift."""
    return tuple(p for p, s in PROVIDERS.items() if s.get("kind") == "generic")

# Providers the entity brain dials directly with the user's own Keys
# credential — the whole registry. Anthropic joined at the orchestrator v2
# routing cut: provider_gateway speaks the Messages wire itself,
# the SDK lane is no longer a production route, and Claude models still never
# transit OpenRouter (coerced to provider 'anthropic' at resolution).
DIRECT_PROVIDERS: tuple = tuple(PROVIDERS)

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def canonical_name(provider: str, cred_type: str = "api_key") -> str:
    """The conventioned Keys name: '<provider>' for api_key,
    '<provider>-oauth' for OAuth-shaped credentials."""
    p = (provider or "").strip().lower()
    return p if cred_type == "api_key" else f"{p}-oauth"


def chat_completions_url(provider: str) -> Optional[str]:
    """Full chat-completions endpoint for an OpenAI-wire provider."""
    spec = PROVIDERS.get((provider or "").strip().lower())
    if not spec or spec["wire"] != "openai":
        return None
    return spec["base_url"].rstrip("/") + _CHAT_COMPLETIONS_SUFFIX


# ── Per-KEY base-URL resolution ──────────────────────────────────────
# Measured: a Kimi-for-Coding SUBSCRIPTION key (sk-k…)
# is rejected 401 by api.moonshot.ai AND api.moonshot.cn, and authenticates on
# https://api.kimi.com/coding/v1 — GET /models serves the coding models
# (kimi-for-coding, k3, k3-256k, …) and POST /chat/completions completes. So
# for kimi the working base is a property of the KEY, not the provider row:
# platform (pay-per-token) keys live on the registry base, subscription keys
# on the alt base. Resolution probes GET {base}/models once per key and
# caches. Providers with no alt bases resolve to their registry base with
# zero probes, so every other provider is untouched by this machinery.
_ALT_BASE_URLS: dict[str, tuple[str, ...]] = {
    "kimi": (os.environ.get("KIMI_SUBSCRIPTION_BASE_URL",
                            "https://api.kimi.com/coding/v1"),),
}
# Alt bases that mean "flat subscription plan" (no per-token list price — the
# catalog UI shows 'subscription' instead of a fabricated number).
_SUBSCRIPTION_BASES: frozenset[str] = frozenset(
    b for bases in _ALT_BASE_URLS.values() for b in bases)
_BASE_URL_CACHE: dict[str, tuple[str, float]] = {}  # key-fp -> (base, exp)
_BASE_URL_TTL_OK = 3600.0
_BASE_URL_TTL_FAIL = 120.0  # every base rejected the key — retry soon


def _key_fp(provider: str, api_key: str) -> str:
    import hashlib
    return provider + ":" + hashlib.sha256(
        (api_key or "").encode("utf-8")).hexdigest()[:16]


def is_subscription_base(base_url: str) -> bool:
    return (base_url or "").rstrip("/") in {
        b.rstrip("/") for b in _SUBSCRIPTION_BASES}


def resolve_base_url_sync(provider: str, api_key: str) -> str:
    """BLOCKING (network probe on first sight of a key) — call off the event
    loop, or through resolve_base_url(). Returns the base_url the KEY actually
    authenticates on: the registry base when the provider has no alt bases or
    when probing fails for transport reasons (a blip must never switch lanes),
    the first base whose GET /models accepts the key otherwise."""
    p = (provider or "").strip().lower()
    spec = PROVIDERS.get(p)
    if not spec:
        return ""
    default = spec["base_url"].rstrip("/")
    alts = _ALT_BASE_URLS.get(p)
    if not alts or not api_key:
        return default
    fp = _key_fp(p, api_key)
    now = time.monotonic()
    cached = _BASE_URL_CACHE.get(fp)
    if cached and cached[1] > now:
        return cached[0]

    import urllib.request
    import urllib.error
    proxies = urllib.request.getproxies()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))

    def _accepts(base: str) -> Optional[bool]:
        """True/False = the base answered (key accepted / rejected).
        None = transport failure — no verdict on the key."""
        req = urllib.request.Request(base.rstrip("/") + "/models", headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "cseries-kernel/0.13"})
        try:
            opener.open(req, timeout=10).read(64)
            return True
        except urllib.error.HTTPError as e:
            return False if e.code in (401, 403) else None
        except Exception:
            return None

    verdict = _accepts(default)
    if verdict is True or verdict is None:
        # Accepted, or no verdict (transport blip): stay on the registry base.
        ttl = _BASE_URL_TTL_OK if verdict is True else _BASE_URL_TTL_FAIL
        _BASE_URL_CACHE[fp] = (default, now + ttl)
        return default
    for alt in alts:
        if _accepts(alt) is True:
            log.info("provider %s key authenticates on alt base %s "
                     "(registry base rejected it)", p, alt)
            _BASE_URL_CACHE[fp] = (alt.rstrip("/"), now + _BASE_URL_TTL_OK)
            return alt.rstrip("/")
    # Every base rejected the key — report the DEFAULT base so the caller's
    # upstream error (401 with the vendor's own message) stays truthful.
    _BASE_URL_CACHE[fp] = (default, now + _BASE_URL_TTL_FAIL)
    return default


async def resolve_base_url(provider: str, api_key: str) -> str:
    """Async wrapper over resolve_base_url_sync — cache hits stay on the loop,
    a first-sight probe runs in a worker thread."""
    p = (provider or "").strip().lower()
    if p not in _ALT_BASE_URLS or not api_key:
        spec = PROVIDERS.get(p)
        return spec["base_url"].rstrip("/") if spec else ""
    fp = _key_fp(p, api_key)
    cached = _BASE_URL_CACHE.get(fp)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    return await asyncio.to_thread(resolve_base_url_sync, p, api_key)


def _normalize(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def stored_type_serves(stored_type: Optional[str], cred_type: str) -> bool:
    """Whether a vault row's STORED cred_type can serve a request for
    `cred_type`. Shared by the resolver's fuzzy rung and the Keys UI's
    slot-coverage read, so a row can never be 'stored' to one and invisible to
    the other.

    NOT yet universal: _github_repo_token (brain/mastercode/runtime/tools.py)
    walks its own copy of the fuzzy ladder with NO type check. Adding this
    there would be a REGRESSION, not a fix — that path has never filtered on
    type, so a working PAT stored as cred_type 'token' (an option the Keys UI
    offers) resolves today and would stop. It needs the credential-owner
    model, not this predicate bolted on.

    A row carrying no cred_type is legacy: it predates the typed model, so the
    name's own 'oauth' marker (matches_provider) is the only signal and this
    admits it. A row that DOES carry a type must match exactly — otherwise an
    oauth_token gets handed to an api_key consumer and re-stored as an api_key
    (type confusion on the credential path).

    NOTE: this governs the FUZZY rung only. An exact canonical-name hit is
    opened by name without a type check (_resolve_typed), and the coverage read
    mirrors that — the two must stay in step.
    """
    st = (stored_type or "").strip()
    ctype = "api_key" if cred_type == "api_key" else "oauth_token"
    return (not st) or st == ctype


def matches_provider(provider: str, cred_type: str, logical_name: str) -> bool:
    """Fuzzy free-typed-name match, TYPE-aware: 'Open Router', 'xai key',
    'Anthropic OAuth' all resolve. The 'oauth' marker in the name decides the
    type side — an api_key lookup must never swallow the oauth entry and vice
    versa (the whole point of the typed model). Deliberately ONLY 'oauth':
    'token' is ambiguous (users call API keys tokens all the time)."""
    n = _normalize(logical_name)
    if _normalize(provider) not in n:
        return False
    return ("oauth" in n) == (cred_type != "api_key")


@dataclass(frozen=True)
class ResolvedCred:
    """A resolved Keys credential: the plaintext plus the metadata consumers
    route on. `source` names the rung that hit — 'vault' (exact canonical
    name), 'vault-aliased' (free-typed name, self-healed to canonical),
    'wired' (explicit host wiring name), 'env' (platform/legacy pods),
    'deactivated' (the user switched this credential off in Keys — empty
    secret, reads as 'not connected', and deliberately blocks the env rung)."""
    provider: str
    name: str        # the Keys logical name that resolved
    cred_type: str   # vault cred_type column value: 'api_key' | 'oauth_token'
    # repr=False: a ResolvedCred formatted into a log line or an f-string must
    # never carry the plaintext key or sign-in token with it.
    secret: str = field(repr=False)
    source: str


async def _resolve_typed(
    pool, entity_id: int, provider: str, cred_type: str,
    *, actor: str, env_fallback: bool,
) -> Optional[ResolvedCred]:
    """The typed ladder for ONE cred_type: exact canonical vault name → fuzzy
    free-typed name (self-healed: re-stored under canonical so the exact rung
    hits next time) → env (api_key only; an OAuth grant never lives in env).
    NEVER raises — a resolver failure means 'not connected', and the consumer
    surfaces its own guidance."""
    p = (provider or "").strip().lower()
    if p not in PROVIDERS:
        return None
    name = canonical_name(p, cred_type)
    ctype = "api_key" if cred_type == "api_key" else "oauth_token"
    try:
        from server.vault import api as _vault
        if pool is not None and entity_id:
            # One listing up front: a DEACTIVATED canonical row is a hard stop
            # — the user switched this credential off, so no fuzzy name and no
            # env fallback may resurrect it. An empty-secret marker goes back
            # so callers read 'not connected' with the reason intact.
            secrets = await _vault.list_secrets(pool, entity_id=entity_id)
            for cred in secrets:
                if cred.get("logical_name") == name and cred.get("deactivated_at"):
                    return ResolvedCred(p, name, ctype, "", "deactivated")
            raw = await _vault.open_secret(
                pool, entity_id=entity_id, name=name, actor=actor)
            if raw:
                return ResolvedCred(
                    p, name, ctype,
                    raw.decode("utf-8", "strict").strip(), "vault")
            for cred in secrets:
                if cred.get("deactivated_at"):
                    continue  # off is off — a fuzzy alias must not bypass it
                lname = cred.get("logical_name") or ""
                # The STORED type wins over the name's 'oauth' marker when the
                # row carries one. Shared with the Keys coverage read via
                # stored_type_serves so the two can never diverge (they did:
                # a 'harness_kimi'-typed row read as stored in the Keys UI and
                # as absent here, so the app said connected and routing said no
                # key).
                if not stored_type_serves(cred.get("cred_type"), cred_type):
                    continue
                if lname != name and matches_provider(p, cred_type, lname):
                    raw = await _vault.open_secret(
                        pool, entity_id=entity_id, name=lname, actor=actor)
                    if raw:
                        secret = raw.decode("utf-8", "strict").strip()
                        await _vault.store_secret(
                            pool, entity_id=entity_id, name=name,
                            cred_type=ctype,
                            plaintext=secret.encode("utf-8"), actor=actor)
                        log.info("%s credential found under Keys name %r — "
                                 "aliased to canonical %r", p, lname, name)
                        return ResolvedCred(p, name, ctype, secret,
                                            "vault-aliased")
    except Exception as exc:
        log.warning("vault %s/%s read failed (env fallback=%s): %s",
                    p, cred_type, env_fallback, exc)
    if env_fallback and cred_type == "api_key":
        env_val = (os.environ.get(PROVIDERS[p]["env_key"]) or "").strip()
        if env_val:
            return ResolvedCred(p, name, "api_key", env_val, "env")
    return None


async def resolve_credential(
    pool, entity_id: int, provider: str, cred_type: str = "api_key",
    *, actor: str = "providers-resolver", env_fallback: bool = True,
) -> Optional[str]:
    """Single-type resolution: the secret text or None. Thin wrapper over the
    typed ladder for callers that want exactly one type's plaintext."""
    rc = await _resolve_typed(pool, entity_id, provider, cred_type,
                              actor=actor, env_fallback=env_fallback)
    return rc.secret if rc else None


async def resolve(
    pool, entity_id: int, provider: str,
    accepted_types: tuple = ("api_key", "oauth_token"),
    wiring: Optional[str] = None,
    *, actor: str = "providers-resolver", env_fallback: bool = True,
) -> Optional[ResolvedCred]:
    """THE resolver entry point. `accepted_types` is the consumer's declared
    preference order — the default is API KEY FIRST (keys work by default;
    the OAuth token slot is for login surfaces the user builds themselves);
    `wiring` is an explicit Keys-entry name (the host's
    per-consumer selection) that short-circuits the ladder. A wired name whose
    entry is absent falls back to the type ladder — a stale wiring must
    degrade to 'whatever is connected', never to a dead lane."""
    wired_name = (wiring or "").strip()
    if wired_name:
        try:
            from server.vault import api as _vault
            if pool is not None and entity_id:
                raw = await _vault.open_secret(
                    pool, entity_id=entity_id, name=wired_name, actor=actor)
                if raw:
                    ctype = ("oauth_token" if "oauth" in _normalize(wired_name)
                             else "api_key")
                    return ResolvedCred(
                        (provider or "").strip().lower(), wired_name, ctype,
                        raw.decode("utf-8", "strict").strip(), "wired")
                log.warning("wiring names Keys entry %r but it is absent — "
                            "falling back to the type ladder", wired_name)
        except Exception as exc:
            log.warning("wired Keys read %r failed (type-ladder fallback): %s",
                        wired_name, exc)
    # A degenerate (whitespace-only) entry of an earlier accepted type must not
    # shadow a real credential of a later one — but if NOTHING resolves, the
    # first empty hit is still what we return, so single-type callers keep the
    # historical '' (the openrouter parity case) instead of flipping to None.
    empty_hit = None
    for cred_type in accepted_types:
        rc = await _resolve_typed(pool, entity_id, provider, cred_type,
                                  actor=actor, env_fallback=env_fallback)
        if rc and rc.secret:
            return rc
        if rc and empty_hit is None:
            empty_hit = rc
    return empty_hit


async def resolve_wired(
    pool, entity_id: int, consumer: str, provider: str,
    accepted_types: tuple = ("api_key", "oauth_token"),
    *, actor: str = "providers-resolver",
) -> Optional[dict]:
    """Host-wired resolution for a CONSUMER (an application/lane): looks up
    the host's explicit wiring (behavioral_params.credential_wiring[consumer]
    = a Keys name) and hands it to `resolve`. Returns {name, cred_type,
    secret} or None (dict shape predates ResolvedCred; kept stable)."""
    wired_name = ""
    try:
        if pool is not None and entity_id:
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id=$1",
                entity_id)
            bp = (row and row["behavioral_params"]) or {}
            if isinstance(bp, str):
                import json as _json
                try:
                    bp = _json.loads(bp)
                except Exception:
                    bp = {}
            wired_name = ((bp.get("credential_wiring") or {}).get(consumer)
                          or "").strip() if isinstance(bp, dict) else ""
    except Exception as exc:
        log.warning("wiring read failed for %s (type-order fallback): %s",
                    consumer, exc)
    rc = await resolve(pool, entity_id, provider, accepted_types,
                       wiring=wired_name or None, actor=actor)
    if rc is None:
        return None
    return {"name": rc.name, "cred_type": rc.cred_type, "secret": rc.secret}


# ── Host-run sign-ins that serve a provider's own model API ──────────
# provider id -> the harness whose vendor login the HOST already runs and the
# platform already keeps renewed (routes/harnesses.py: `grok login
# --device-auth`, sealed as harness:grok:auth, refreshed by
# maintain_refreshable_creds). A Host's text roles may run on
# the user's xAI account through that SAME sign-in — no second login flow.
#
# WHEN A SIGN-IN EXISTS, THE API KEY IS NEVER USED. An xAI API key bills per token at
# console.x.ai
# and never touches the user's subscription; the subscription is reachable
# only through the sign-in. So the key is not a fallback behind the sign-in —
# it is what a Host uses only when there is no sign-in at all. Order, for
# model calls, web search and the catalog alike:
#   1. a live sign-in -> its token. xAI answering 401/403 to it is reported in
#      words (signin_refused_message); nothing is retried on the key.
#   2. a sign-in that is present but not usable — expired and not yet renewed,
#      or a sealed artifact with no token the parser can read — is reported
#      (signin_unusable_message); the key is not used.
#   3. a sign-in switched off in Keys is ABSENT (open_secret reads a
#      deactivated row as None), so it is step 4 or 5.
#   4. no sign-in -> the key (today's behaviour).
#   5. neither   -> the message naming both ways in (no_credential_message).
# Behind the sign-in nothing resolves the key at all: resolve_for_inference
# returns before reading it.
#
# Measured live: a Grok sign-in
# token answered 200 on api.x.ai /v1/models,
# /v1/chat/completions and /v1/responses with grok-4.7. The stub gate
# (scripts/test_host_subscriptions.py) makes no vendor calls.
#
# xai ONLY, deliberately. Every other OAuth slot (anthropic/openai/google) is a
# coding-engine login, and nothing here says its token is accepted by that
# vendor's public model API for a Host's calls; a provider joins this map on a
# measurement, never by symmetry. Anthropic's own key-first order
# (provider_gateway._call_anthropic) is untouched by any of this.
_SIGNIN_INFERENCE: dict[str, str] = {"xai": "grok"}
# How the vendor is named to the user ("xAI sign-in" / "xAI API key").
_SIGNIN_VENDOR: dict[str, str] = {"xai": "xAI"}
# A token this close to expiry is treated as expired: dialling with it would
# spend the call on a 401 the renewal loop is about to make unnecessary.
_SIGNIN_EXPIRY_SKEW_MS = 60_000


def signin_serves_inference(provider: str) -> bool:
    return (provider or "").strip().lower() in _SIGNIN_INFERENCE


def _vendor(provider: str) -> str:
    p = (provider or "").strip().lower()
    return _SIGNIN_VENDOR.get(p, p)


def vendor_name(provider: str) -> str:
    """How the vendor is named to the user: 'xAI' for xai."""
    return _vendor(provider)


def signin_harness(provider: str) -> Optional[str]:
    """The harness whose vendor login fills `provider`'s sign-in, when that
    sign-in serves the Host's model calls (_SIGNIN_INFERENCE) AND the Keys
    slot map wires the same harness (_signin_slot) — else None.

    The Keys app's "Sign in with …" button (vault.list_providers'
    signin_from_keys) and the routes behind it (POST /api/vault/providers/
    {provider}/signin[/finish]) both read this, so the button shows exactly
    where the routes accept, and only for a sign-in a Host can run on. That is
    xai alone today; the Anthropic and OpenAI sign-ins stay out of Keys."""
    p = (provider or "").strip().lower()
    if not signin_serves_inference(p) or _signin_slot(p) is None:
        return None
    return _SIGNIN_INFERENCE[p]


def _signin_slot(provider: str) -> Optional[dict]:
    """{harness, harness_name, vault_name} for the provider's host-run login —
    the SAME derivation the Keys app's OAuth slot reads (harness_state.
    oauth_login_slots), so the slot the user sees lit and the credential the
    Host dials can never name different rows. None when unwired."""
    p = (provider or "").strip().lower()
    if p not in _SIGNIN_INFERENCE:
        return None
    try:
        from server.network.harness_state import oauth_login_slots
        slot = oauth_login_slots().get(p)
    except Exception as exc:  # pragma: no cover - defensive
        log.info("sign-in slot for %s unavailable: %r", p, exc)
        return None
    if not slot or slot.get("harness") != _SIGNIN_INFERENCE[p]:
        return None
    return slot


def credential_label(provider: str, kind: str) -> str:
    """'xAI sign-in' | 'xAI API key' — how routing status names what served."""
    return f"{_vendor(provider)} {'sign-in' if kind == 'signin' else 'API key'}"


def signin_button_label(provider: str, action: str) -> str:
    """The words on the Keys app's sign-in button for `provider`: 'Sign in
    with xAI' when there is no sign-in to use (none, or switched off),
    'Reconnect with xAI' when one is stored. The Keys payload carries this
    exact string (vault.list_providers `signin_label`) and the messages below
    quote it, so what a message tells the user to press is what Keys shows."""
    v = _vendor(provider)
    return f"Reconnect with {v}" if action == "reconnect" else f"Sign in with {v}"


def _press_in_keys(provider: str, action: str) -> str:
    return (f"press {signin_button_label(provider, action)} on the "
            f"{(provider or '').strip().lower()} row in the Keys app")


def no_credential_message(provider: str) -> str:
    """Step 5 — neither a sign-in nor a key. Names BOTH ways in. Also what a
    Host reads when the sign-in is switched off and there is no key; Keys
    shows the same Sign in button in both states."""
    v = _vendor(provider)
    if not signin_harness(provider):
        return (f"no {v} key — add an {v} API key in the Keys app (name it "
                f"{provider!r}), or route this role to another provider or a "
                f"local model in the Orchestrator.")
    return (f"no {v} sign-in or key — {_press_in_keys(provider, 'signin')}, or "
            f"add an {v} API key there (name it {provider!r}), or route this "
            f"role to another provider or a local model in the Orchestrator.")


def _key_while_signed_in(provider: str) -> str:
    # The one way onto the key while a sign-in exists: switch the sign-in off.
    # Keys shows the sealed sign-in in its Vault section under its stored name
    # (harness:grok:auth, a "Builder credential" row with Deactivate), not as
    # "sign-in" — so name that row and that button.
    v = _vendor(provider)
    name = (_signin_slot(provider) or {}).get("vault_name")
    if not name:
        return f"Your {v} API key is not used while you are signed in."
    return (f"Your {v} API key is not used while you are signed in; to use it, "
            f"press Deactivate on {name} in the Vault section of the Keys app.")


def signin_expired_message(provider: str) -> str:
    v = _vendor(provider)
    return (f"Your {v} sign-in has expired and has not been renewed yet. "
            f"To renew it, {_press_in_keys(provider, 'reconnect')}, or route "
            f"this role to another provider in the Orchestrator. "
            + _key_while_signed_in(provider))


def signin_unreadable_message(provider: str) -> str:
    v = _vendor(provider)
    step = _press_in_keys(provider, "reconnect")
    return (f"Your {v} sign-in could not be read. {step[:1].upper()}{step[1:]}, "
            f"or route this role to another provider in the Orchestrator. "
            + _key_while_signed_in(provider))


def signin_unusable_message(provider: str, source: str) -> str:
    """Step 2 — a sign-in is present but cannot be dialled. `source` is the
    resolve_signin marker: 'signin-expired' or 'signin-unreadable'."""
    if source == "signin-unreadable":
        return signin_unreadable_message(provider)
    return signin_expired_message(provider)


def signin_refused_message(provider: str, status: int) -> str:
    v = _vendor(provider)
    step = _press_in_keys(provider, "reconnect")
    return (f"{v} refused your sign-in for model calls (HTTP {status}). "
            f"{step[:1].upper()}{step[1:]}, or route this role to another "
            f"provider in the Orchestrator. " + _key_while_signed_in(provider))


# The resolve_signin markers for a sign-in that is PRESENT but cannot be
# dialled. Callers stop on these with signin_unusable_message — never the key.
SIGNIN_UNUSABLE: frozenset[str] = frozenset({"signin-expired", "signin-unreadable"})


# The credential that last served each sign-in-capable provider's model call,
# so routing status can say "xAI sign-in" or "xAI API key". Process-local on
# purpose: the brain's model calls and the Orchestrator's routing read run in
# the same kernel process (server/launch.py runs one uvicorn process), and the
# durable record of every call is its inference-ledger row (`credential` in its
# metadata, provider_gateway._call_remote).
_LAST_SERVED: dict[str, dict] = {}


def note_served(provider: str, kind: str) -> dict:
    p = (provider or "").strip().lower()
    rec = {"credential": kind, "label": credential_label(p, kind),
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _LAST_SERVED[p] = rec
    return rec


def last_served(provider: str) -> Optional[dict]:
    rec = _LAST_SERVED.get((provider or "").strip().lower())
    return dict(rec) if rec else None


async def resolve_signin(
    pool, entity_id: int, provider: str, *, actor: str = "providers-resolver",
) -> Optional[ResolvedCred]:
    """The provider's host-run sign-in as a Bearer credential, or None when
    there is NO sign-in.

    Reads the SEALED login artifact (open_secret — a deactivated row reads as
    absent there, so switching the sign-in off in Keys switches it off here)
    and extracts the access token with the one parser for that shape
    (routes/harnesses.access_token_from_text). source='signin' on success.
    A sign-in that is present but cannot be dialled comes back with an empty
    secret and a marker (SIGNIN_UNUSABLE), so the caller says why and never
    falls through to the API key: 'signin-expired' when its token is expired,
    'signin-unreadable' when the artifact holds no token the parser reads or
    the read itself failed. Only None means "no sign-in". NEVER raises."""
    p = (provider or "").strip().lower()
    slot = _signin_slot(p)
    if not slot or pool is None or not entity_id:
        return None
    name = slot["vault_name"]
    try:
        from server.vault import api as _vault
        raw = await _vault.open_secret(
            pool, entity_id=entity_id, name=name, actor=actor)
        if not raw:
            return None
        text = raw.decode("utf-8", "strict")
        from server.routes.harnesses import (
            access_token_from_text, expiry_ms_from_text)
        token = access_token_from_text(text)
        if not token:
            return ResolvedCred(p, name, "oauth_token", "", "signin-unreadable")
        exp_ms = expiry_ms_from_text(text)
        if exp_ms is not None and exp_ms - _SIGNIN_EXPIRY_SKEW_MS <= time.time() * 1000:
            return ResolvedCred(p, name, "oauth_token", "", "signin-expired")
        return ResolvedCred(p, name, "oauth_token", token, "signin")
    except Exception as exc:
        # Type only: a decode error's text can carry a byte of the secret.
        log.warning("sign-in read %s/%s failed: %s", p, name, type(exc).__name__)
        return ResolvedCred(p, name, "oauth_token", "", "signin-unreadable")


async def resolve_key(
    pool, entity_id: int, provider: str, *, actor: str = "providers-resolver",
) -> Optional[ResolvedCred]:
    """The provider's API key as a ResolvedCred (source 'key'), or None.

    Goes through resolve_credential, looked up at CALL time on purpose: the
    gates that stub the key (test_web_research_dialers, test_go_ahead_guard, …)
    patch that module attribute, and must keep reaching every model call."""
    p = (provider or "").strip().lower()
    key = await resolve_credential(pool, entity_id, p, "api_key", actor=actor)
    return ResolvedCred(p, canonical_name(p), "api_key", key, "key") if key else None


async def resolve_for_inference(
    pool, entity_id: int, provider: str, *, actor: str = "providers-resolver",
) -> Optional[ResolvedCred]:
    """The ONE credential a Host's OpenAI-wire call on `provider` dials. For a
    sign-in provider (xai): the sign-in whenever one exists — live, or a
    SIGNIN_UNUSABLE marker the caller reports — and the key ONLY when there is
    no sign-in; the key is not even read behind a sign-in. For every other
    provider: the key exactly as the call path read it before (resolve_key).
    None when there is nothing. source 'key' | 'signin' | 'signin-expired' |
    'signin-unreadable'. NEVER raises."""
    p = (provider or "").strip().lower()
    if not signin_serves_inference(p):
        return await resolve_key(pool, entity_id, p, actor=actor)
    s = await resolve_signin(pool, entity_id, p, actor=actor)
    if s is not None:
        return s
    return await resolve_key(pool, entity_id, p, actor=actor)
