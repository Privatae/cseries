"""
Inference routes — model catalog, per-role provider/model routing, billing mode.

Routing is per-role {provider, model, fallback_provider?, fallback_model?}:
provider is a Keys-registry id (server.vault.providers) or "local" (the user's
own machines). The lane model is gone (the SDK hard cut): no
claude-sdk lane, no default model — an unrouted role resolves EMPTY and the
Orchestrator surfaces it. Every token runs on the USER's OWN provider account
(compute-only billing; there is no platform inference key).

Roles: chat, embedding, image, image_gen (ROLES below).
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import NamedTuple

from fastapi import APIRouter, HTTPException, Depends
from server.applications.middleware import requires_permission
from pydantic import BaseModel

from server import brain_service

logger = logging.getLogger("privatae.pc.inference")

router = APIRouter(prefix="/api/inference", tags=["inference"])

# Text roles that pick a MODEL. There is no 'code' role: coding is done by the
# host through the selected coding HARNESS(es) (build), not a raw model.
#
# THREE text roles: the orchestrator was too complicated, so model calls happen
# only where reasoning happens. plan, research, agents, tutor and journal are
# gone. Measured on one Host over the 14 days
# before the cut: plan 0 attributable calls, review 0, tutor 0, journal 0 (chat
# sessions are ephemeral, so the session-close recap never fired), research 17,
# agents 15 — against chat 291 and consolidation 545. Each removed role was a
# seat the user had to fill for a lane that either never fired or had one
# obvious home, so every consumer now names one of the three at its call site
# instead of resolving a role of its own. What survives is where reasoning
# actually happens: `chat` is the host itself. There is no `review` role:
# the Host reviews in its own turn, or has a coding engine review.
# `consolidation` (the memory lane, shown as Memory)
# is gone (memory ops are just tool calls): no turn
# asks a second model to decide a memory, and the background loop, when it
# runs, uses the chat route (turn_consolidator._resolve_consolidation_route).
#
# The role KEYS are unchanged, so nothing stored needs migrating: a route the
# user saved under a removed key is simply ignored on read — it is not in
# ROLES, so it reaches neither the resolver nor the picker.
#
# 'image_gen' (make an image) once sat beside a 'vision' role (read
# one) that is gone again. Images are part of the Host,
# not separate — a picture goes to the Host's OWN model, so there is no second
# model to route for reading one. A stored `vision` route is ignored on read
# (not in this list), never migrated; a PUT naming it gets the plain "Invalid
# role" refusal below, which names the roles that exist. The catalog CAPABILITY
# 'vision' (the model accepts image input, _CAPABILITY_MODALITY) stays: it is
# how a Host model that can see is picked.
# image_gen is NOT a text role — see TEXT_ROLES in
# scripts/test_recommended_routes.py, which excludes it alongside
# embedding/image so the one-click recommendation gate doesn't demand a chat
# plan for an image model.
# `review` went 2026-10-20: its one consumer, the diff reviewer that ran a
# model on every landed build, was removed (the Host reviews in its own turn,
# or has a coding engine review when the person wants a second opinion). A
# route still stored under it is never resolved, like the other retired roles.
ROLES = ["chat",
         "embedding", "image", "image_gen"]


class SetRoleModel(BaseModel):
    role: str
    # The route: a Keys-registry provider id (or "local") + that provider's
    # native model id. The pair is normalized on write (normalize_provider) —
    # a Claude-shaped pick on openrouter stores as provider 'anthropic'
    # (Claude never transits OpenRouter).
    provider: str | None = None
    model: str | None = None
    # Optional failover pair — the gateway retries on it when the primary
    # provider errors or the model is unavailable there. fallback_model None =
    # retry the primary model id on the fallback provider.
    fallback_provider: str | None = None
    fallback_model: str | None = None
    # Reasoning effort for this role. Absent/"" = the provider's
    # own default — NOT a silent "low". Validated against TEXT_ROLE_EFFORTS on
    # write, so an unsendable value can never be stored.
    effort: str | None = None
    # The route's model writes its reasoning inline in the reply, closed by
    # </think> with no opening tag: the OpenAI-wire adapter then
    # holds the stream back until the close (provider_gateway). Off unless
    # set — which models do this is the person's knowledge, not a guess here.
    reasoning_inline: bool | None = None
    # Optional endpoint/connection: base_url is for BYO-endpoint roles
    # (embedding/image) only — text-role local routes resolve their endpoint
    # live from bastion heartbeats, never a stored pin.
    base_url: str | None = None
    connection_id: str | None = None
    # Legacy lane payloads are accepted ONLY to be refused with a pointed 400
    # (lanes were hard-cut).
    lane: str | None = None


class SetBillingMode(BaseModel):
    mode: str


class HarnessSel(BaseModel):
    # id + optional per-harness overrides (Circles → Harness settings). `model`
    # routes the harness to a specific model (all harnesses honor it via
    # fan_out harness_model → /run); `effort` sizes extended thinking, honored
    # only where the harness supports it (claude).
    id: str
    model: str | None = None
    effort: str | None = None


class SetCodingHarnesses(BaseModel):
    harnesses: list[HarnessSel]


class SetCodingDefault(BaseModel):
    # Orchestrator → Coding Engines → the Default tick. null = clear the tick
    # (an unnamed build then refuses when several engines are connected).
    harness: str | None = None


# Model catalog cache. The catalog rarely changes but building it does blocking
# network I/O (Ollama + OpenRouter), so we (a) build it OFF the event loop via
# asyncio.to_thread and (b) cache the result. Without this, one slow/hanging
# OpenRouter fetch (e.g. DNS failure) froze the pod's single-worker event loop
# and stalled the whole dashboard.
_CATALOG_CACHE: dict = {"data": None, "exp": 0.0}
_CATALOG_TTL_OK = 300.0     # successful fetch cached 5 min
_CATALOG_TTL_FAIL = 30.0    # degraded (no OpenRouter) — retry sooner


@router.get("/catalog", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_model_catalog():
    """Return the full inference catalog: live OpenRouter models + local Ollama
    models. The user picks which of these are their FAVORITES (see /favorites);
    the picker defaults to that user-built set instead of the full ~339 list.

    The fetch is blocking (urllib), so it runs in a worker thread and is cached
    — it must never block the event loop (single uvicorn worker), or a hanging
    OpenRouter request stalls every other request on the pod.
    """
    now = time.monotonic()
    cached = _CATALOG_CACHE["data"]
    if cached is not None and _CATALOG_CACHE["exp"] > now:
        return {**cached, "local_models": await _table_local_models()}
    result = await asyncio.to_thread(_build_model_catalog)
    ok = result.get("total_openrouter", 0) > 0

    # Never let a degraded build (OpenRouter fetch failed → 0 models) OVERWRITE a
    # previously-good catalog. The picker reads openrouter_models off this response;
    # a single transient fetch failure used to (a) return [] to the picker AND
    # (b) poison the cache with empties for _CATALOG_TTL_FAIL seconds — so the
    # Models tab went (and stayed) dark even though OpenRouter was reachable the
    # whole time. Root cause of the e2e count=0. Now: a degraded build serves the
    # last-known-good catalog (flagged `degraded`) and only retries the fetch
    # sooner; we cache empties ONLY when there is no good prior to fall back to.
    if not ok and cached is not None and cached.get("total_openrouter", 0) > 0:
        stale = dict(cached)
        stale["degraded"] = True
        stale["local_models"] = await _table_local_models()
        _CATALOG_CACHE["exp"] = now + _CATALOG_TTL_FAIL  # retry the live fetch sooner
        return stale

    result["degraded"] = not ok
    ttl = _CATALOG_TTL_OK if ok else _CATALOG_TTL_FAIL
    _CATALOG_CACHE["data"] = result
    _CATALOG_CACHE["exp"] = now + ttl
    return {**result, "local_models": await _table_local_models()}


def cached_context_lengths() -> dict:
    """model_id → context_length from the last good catalog build ({} on a
    cold cache). Read-only accessor for the turn path's context budgeting
    (conversation-depth P1): the chat hot path reads the window
    from the SAME catalog the model picker shows, WITHOUT triggering a
    catalog fetch — a cold cache just means 'window unknown' and the turn
    falls back to the legacy history caps.

    Also folds in the direct-models rows (_DIRECT_MODELS_CACHE —
    the bare ids the Keys app lists for a first-party key, with the
    context_length the vendor's own /models published or the OpenRouter
    suffix-enrichment filled). Before this, a first-party OpenAI/xAI/Kimi
    route ("gpt-5.2", not "openai/gpt-5.2") missed the OpenRouter-keyed map
    and ran unbudgeted. OpenRouter ids win on collision (same window
    either way; the OR row is the one the picker shows)."""
    out = {}
    for p, ent in list(_DIRECT_MODELS_CACHE.items()):
        for m in (ent or {}).get("data") or []:
            try:
                if isinstance(m, dict) and m.get("id") and m.get("context_length"):
                    out[str(m["id"])] = int(m["context_length"])
            except (TypeError, ValueError):
                continue
    data = _CATALOG_CACHE.get("data") or {}
    for m in data.get("openrouter_models") or []:
        try:
            if m.get("id") and m.get("context_length"):
                out[m["id"]] = int(m["context_length"])
        except (TypeError, ValueError):
            continue
    return out


def cached_max_output_tokens() -> dict:
    """model_id → the model's own published output limit (tokens), from the
    last good catalog build ({} on a cold cache). The output twin of
    cached_context_lengths, with the same sources and precedence and the same
    no-fetch rule (a turn once sent a fixed 16,384 while the catalog it
    already fetched said 128,000)."""
    out = {}
    for _p, ent in list(_DIRECT_MODELS_CACHE.items()):
        for m in (ent or {}).get("data") or []:
            try:
                if isinstance(m, dict) and m.get("id") and m.get("max_output_tokens"):
                    out[str(m["id"])] = int(m["max_output_tokens"])
            except (TypeError, ValueError):
                continue
    data = _CATALOG_CACHE.get("data") or {}
    for m in data.get("openrouter_models") or []:
        try:
            if m.get("id") and m.get("max_output_tokens"):
                out[m["id"]] = int(m["max_output_tokens"])
        except (TypeError, ValueError):
            continue
    return out


_ROUTE_WARMS: dict = {}


async def _warm_route_catalog_now(p: str) -> None:
    try:
        if p and p not in ("openrouter", "local") and p not in _DIRECT_MODELS_CACHE:
            await _direct_models(p)
        if not (_CATALOG_CACHE.get("data") or {}).get("openrouter_models"):
            now = time.monotonic()
            if (_CATALOG_CACHE.get("exp") or 0) <= now:
                # Claimed before the fetch, so a second turn arriving while
                # this one fetches does not start another.
                _CATALOG_CACHE["exp"] = now + _CATALOG_TTL_FAIL
                result = await asyncio.to_thread(_build_model_catalog)
                if result.get("total_openrouter", 0) > 0:
                    result["degraded"] = False
                    _CATALOG_CACHE["data"] = result
                    _CATALOG_CACHE["exp"] = time.monotonic() + _CATALOG_TTL_OK
    except Exception as e:  # noqa: BLE001
        logger.debug("warm_route_catalog(%s) failed (non-fatal): %s", p, e)


async def warm_route_catalog(provider: str | None, wait_s: float = 5.0) -> None:
    """Fill the catalog rows a route reads, when they are cold. The turn read
    its model's window and output limit from caches that only fill when the
    startup warm finishes or someone opens the Keys app; until then a
    first-party route (kimi "k3": 89 of 301 turns on one Host) ran with the
    window "unknown" and its history cut to 12 messages / 8,000 chars. One fetch
    per provider per cache TTL through the same cached builders the picker uses
    (failures cached briefly), never a fetch per turn. The turn waits at most
    `wait_s` for it; a slower fetch keeps going in the background and fills the
    cache for the next turn. Never raises."""
    p = (provider or "").strip().lower()
    # Only inside a running server (its brain pool is up): the warm reads the
    # Keys vault and the vendors' lists, and a test or a script importing the
    # turn must never reach the network through it.
    try:
        if brain_service.get_pool() is None:
            return
    except Exception:  # noqa: BLE001
        return
    task = _ROUTE_WARMS.get(p)
    if task is None or task.done():
        try:
            task = asyncio.get_running_loop().create_task(_warm_route_catalog_now(p))
        except RuntimeError:
            return
        _ROUTE_WARMS[p] = task
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return


def cached_input_modalities(provider: str, model: str):
    """What `model` on `provider` can READ, from the last catalog build, or
    None when that is not known (cold cache, or a row that publishes no
    modalities — absent means unknown, never "text only"). Read-only, no
    fetch: the turn path asks this per call, the same way it reads
    cached_context_lengths."""
    p = (provider or "").strip().lower()
    mid = (model or "").strip()
    if not mid:
        return None
    rows = []
    if p == "openrouter":
        rows = (_CATALOG_CACHE.get("data") or {}).get("openrouter_models") or []
    else:
        rows = (_DIRECT_MODELS_CACHE.get(p) or {}).get("data") or []
    for m in rows:
        if isinstance(m, dict) and str(m.get("id") or "") == mid:
            mods = m.get("input_modalities")
            if isinstance(mods, list) and mods:
                return [str(x).lower() for x in mods]
            return None
    return None


def cached_model_efforts(provider: str, model: str) -> tuple | None:
    """The effort levels `model` on `provider` accepts, from the last catalog
    build: the row's `efforts` (_dm_row reads it off the provider's own
    /models capabilities; Anthropic's is the one that publishes it). () when
    the row says the model takes no effort, None when no row says anything — a
    cold cache, a model the list does not carry, or a provider that publishes
    no such field. Read-only, no fetch, like cached_input_modalities.

    A row id matches exactly, or with a date suffix on either side
    (claude-opus-4-5 and claude-opus-4-5-20251101 are the same model)."""
    from mastercode.inference_config import undated_model_id
    p = (provider or "").strip().lower()
    mid = (model or "").strip()
    if not mid:
        return None
    if p == "openrouter":
        rows = (_CATALOG_CACHE.get("data") or {}).get("openrouter_models") or []
    else:
        rows = (_DIRECT_MODELS_CACHE.get(p) or {}).get("data") or []
    base = undated_model_id(mid)
    near = None
    for m in rows:
        if not isinstance(m, dict) or not isinstance(m.get("efforts"), list):
            continue
        rid = str(m.get("id") or "")
        if rid == mid:
            return tuple(str(x) for x in m["efforts"])
        if near is None and undated_model_id(rid) == base:
            near = tuple(str(x) for x in m["efforts"])
    return near


class ModelPrice(NamedTuple):
    """One model's published per-token rates. A TUPLE on purpose: every caller
    that was written against the old `(input_per_token, output_per_token)`
    return of cached_model_prices() keeps working — p[0] and p[1] are the same
    two numbers in the same order.

    cache_read / cache_write are None when the provider publishes NO cache rate,
    never 0.0. A fabricated free cache rate is the same lie _dm_row already
    refuses for input price (:1493 "a per-token 'free' there would be a
    fabrication"), and it is the more dangerous one here: cache reads are ~84%
    of input tokens on a real host, so pricing them at zero would understate the
    vendor's bill by roughly as much as pricing them at full input rate
    overstates it."""
    input_per_token: float
    output_per_token: float
    cache_read: float | None = None
    cache_write: float | None = None
    # PROMPT-LENGTH TIERS. ((min_prompt_tokens, ModelPrice), ...)
    # ascending; empty for a model with one price at every length. Claude
    # Haiku 5.5 is the first: "a prompt of over 100,000 tokens pays higher
    # prices" (platform.claude.com pricing page), a whole second rate set,
    # which OpenRouter publishes as pricing.overrides (_or_price_tiers).
    tiers: tuple = ()

    def tier_for(self, prompt_tokens: int) -> "tuple[int | None, ModelPrice]":
        """(minimum, rates) for a prompt of this many tokens, cached tokens
        included: the highest tier whose minimum the prompt reaches, or
        (None, self). The whole call moves to the tier, not just the tokens
        past the line. A prompt AT the minimum takes the tier: OpenRouter
        names the field a minimum while Anthropic says "over", and the one
        token between them is resolved high, the side this estimator errs
        to everywhere else."""
        n = int(prompt_tokens or 0)
        hit: tuple = (None, self)
        for floor, rates in self.tiers:
            if n >= floor:
                hit = (floor, rates)
        return hit


def _validated(provider: str | None, model_id: str | None) -> bool:
    """The platform's validated list (runtime/model_validation), read safely:
    a lookup failure is 'not validated', never a failed catalog."""
    try:
        from mastercode.runtime.model_validation import is_validated
        return bool(is_validated(provider or "", model_id or ""))
    except Exception:  # noqa: BLE001
        return False


def _standard(provider: str | None, model_id: str | None) -> dict:
    """The Host model standard's verdict for a catalog row
    (runtime/model_standard): canonical identity, status, eligible,
    approved, enforced. summary() never raises; a lookup failure reads as
    unassessed, never a failed catalog."""
    try:
        from mastercode.runtime.model_standard import summary
        return summary(provider or "", model_id or "")
    except Exception:  # noqa: BLE001
        return {"canonical": None, "status": "unidentified", "eligible": False,
                "approved": False, "enforced": False}


def cached_model_prices() -> dict:
    """model_id -> ModelPrice(input, output, cache_read, cache_write) from the last good
    catalog build ({} on a cold cache). The pricing twin of
    cached_context_lengths(): reads the SAME already-warmed caches and likewise
    never triggers a fetch.

    Why it exists: provider_gateway._log_inference wrote est_cost_usd as a
    hardcoded 0.0 because "there is no rate card in the pod". Refusing to
    invent pricing was right; the premise is stale. The pod HAS the vendors'
    own published rates — warm_model_catalog() fills _CATALOG_CACHE at startup
    and first-party lookups fill _DIRECT_MODELS_CACHE, and both row shapes
    already carry input_per_token / output_per_token exactly as the model
    picker renders them. Measured on one Host: every inference row
    logged $0 (256 calls of real spend reading as free) while
    the catalog was showing the price of those same models.

    This is the ESTIMATE lane and nothing else: the vendor's LIST price for the
    tokens actually used. Never what the user was billed (Privatae bills
    compute only).

    Subscription rows are EXCLUDED rather than priced at zero. A flat-plan
    model has no per-token price, and pricing its tokens at 0 reads as "this
    was free" when the honest answer is "not per-token priced" — the same
    zero-that-looks-like-data this lane keeps producing.

    The row also carries the CACHE rates where the provider
    publishes them (cache_read_per_token / cache_write_per_token on the catalog
    row). Absent stays absent — see ModelPrice."""
    out: dict[str, ModelPrice] = {}

    def _rate(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    def _tiers(m: dict, pin: float, pout: float) -> tuple:
        # A tier restates what changes at its length. A missing input or
        # output rate inherits the base one, the only published number for
        # it. A missing CACHE rate stays None, so the estimator falls back to
        # the tier's own input rate and labels it: inheriting the base cache
        # rate would price a long prompt's cached share at the short-prompt
        # rate, low by the whole tier step.
        got = []
        for t in m.get("price_tiers") or []:
            try:
                floor = int(t["min_prompt_tokens"])
                tin, tout = _rate(t.get("input_per_token")), _rate(t.get("output_per_token"))
                got.append((floor, ModelPrice(
                    pin if tin is None else tin, pout if tout is None else tout,
                    _rate(t.get("cache_read_per_token")),
                    _rate(t.get("cache_write_per_token")))))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return tuple(sorted(got, key=lambda x: x[0]))

    def _put(m: dict) -> None:
        try:
            mid = m.get("id")
            if not mid or m.get("subscription"):
                return
            pin, pout = m.get("input_per_token"), m.get("output_per_token")
            if pin is None and pout is None:
                return                      # provider publishes no price
            pin, pout = float(pin or 0.0), float(pout or 0.0)
            out[str(mid)] = ModelPrice(
                pin, pout,
                _rate(m.get("cache_read_per_token")),
                _rate(m.get("cache_write_per_token")),
                _tiers(m, pin, pout))
        except (TypeError, ValueError):
            return

    # Same precedence as cached_context_lengths: direct-model rows first,
    # OpenRouter wins on collision (it is the row the picker shows).
    for _p, ent in list(_DIRECT_MODELS_CACHE.items()):
        for m in (ent or {}).get("data") or []:
            if isinstance(m, dict):
                _put(m)
    data = _CATALOG_CACHE.get("data") or {}
    for m in data.get("openrouter_models") or []:
        if isinstance(m, dict):
            _put(m)
    # _price_for's fallback for a bare first-party id builds the dashed
    # anthropic/claude-opus-5-5, but OpenRouter files that model as the dotted
    # anthropic/claude-opus-5.5. File each dotted Anthropic row under the
    # dashed spelling too (the rule _enrich_first_party_from_openrouter
    # matches by), or a call made before anything loaded Anthropic's own
    # /v1/models is written 'unpriced' when its price is sitting right here.
    for k in [k for k in out if k.startswith("anthropic/") and "." in k]:
        out.setdefault(k.replace(".", "-"), out[k])
    return out


def _price_for(model_id: str) -> ModelPrice | None:
    """The catalog's ModelPrice for an id, or None. Shared by the estimator and
    anything that needs to know WHETHER a model is per-token priced."""
    if not model_id:
        return None
    prices = cached_model_prices()
    p = prices.get(str(model_id))
    if p is None:
        # A first-party id ("gpt-5.2") can miss the OpenRouter-keyed map that
        # carries the same model prefixed ("openai/gpt-5.2") — the same
        # fallback the context-window lookup uses.
        for pref in set(_OR_VENDOR_PREFIX.values()):
            p = prices.get(f"{pref}{model_id}")
            if p is not None:
                break
    return p


def estimate_call_cost_detail(model_id: str, tokens_in: int, tokens_out: int, *,
                              cache_read_tokens: int = 0,
                              cache_write_tokens: int = 0,
                              provider: str | None = None,
                              credential: str | None = None
                              ) -> tuple[float | None, dict]:
    """(usd, priced) for ONE call — the estimator, and a record of WHICH rate
    was applied to each part of it. `usd` is None exactly when
    estimate_call_cost_usd() returns None.

    WHY THE CACHE SPLIT EXISTS. tokens_in here is the
    lane-normalized TOTAL input — the Anthropic path adds cache_read +
    cache_write to input_tokens (provider_gateway:1335-1337) and the OpenAI wire
    already includes the cached subset, normalised to that at
    :111-181 (_normalize_openai_usage). Pricing all of it at the
    full input rate is what this function used to do, and cache reads are
    typically a small fraction of the input rate (verified live against
    openrouter.ai/api/v1/models on 2026-09-15: anthropic/claude-opus-5 publishes
    prompt 0.000005 and input_cache_read 0.0000005, i.e. one tenth). With
    measured hit rates around 84% the old estimate ran several times the
    vendor's actual bill, which is the wrong direction for a dollar-denominated
    per-turn budget to be built on.

    THE ARITHMETIC:
        uncached = tokens_in - cache_read - cache_write   (clamped at 0)
        usd = uncached*input + cache_read*R + cache_write*W + tokens_out*output
    R is the published cache-read rate, and the INPUT rate when the provider
    publishes none — the older behaviour, kept because it is the only
    non-invented number available and it errs high rather than low. Which one
    applied is in the returned `priced` dict ('published' vs
    'input_rate_fallback'), so a later pass can tell an exact row from an
    approximate one without re-deriving it.

    THE CLAMP IS THE LAST LINE OF DEFENCE, NOT THE FIX. The shape that
    produces a negative remainder — a provider relaying Anthropic's usage
    fields inside an OpenAI-shaped object, where the reported input EXCLUDES
    the cached counts — is normalised at the wire, where the two conventions
    can still be told apart (provider_gateway:168-180). By the time tokens
    reach this function they are one number and the distinction is gone, so
    clamping here can only stop a negative from CREDITING the estimate; it
    cannot recover the uncached tokens that a bad total lost, and it will
    charge them at $0. That is why it logs at WARNING with the provider, the
    model and both counts: if it ever fires, a lane is mis-reporting usage and
    the normalisation above needs to learn its shape. `provider` is carried
    for that message alone and never affects a price.

    `credential` is which credential SERVED the call, where the provider has
    more than one kind (xai: 'signin' | 'api_key', from the gateway's result;
    None everywhere else, which changes nothing). A call on the sign-in spent
    the user's plan and has no per-token price — even though _price_for would
    find the same model's per-token rate on OpenRouter's x-ai/ row — so it is
    None here and price_basis() names it 'subscription'."""
    if credential == "signin":
        return None, {}
    p = _price_for(model_id)
    if p is None:
        return None, {}
    t_in = int(tokens_in or 0)
    t_out = int(tokens_out or 0)
    c_read = max(0, int(cache_read_tokens or 0))
    c_write = max(0, int(cache_write_tokens or 0))
    priced: dict = {"input": "published", "output": "published"}
    # tokens_in is the whole prompt, cached tokens included, which is the
    # length a prompt-length tier is judged on (ModelPrice.tier_for).
    floor, p = p.tier_for(t_in)
    if floor is not None:
        priced["prompt_tier"] = floor
    uncached = t_in - c_read - c_write
    if uncached < 0:
        logger.warning(
            "estimate: %s/%s reports cache_read=%d + cache_write=%d against "
            "only %d input tokens — the lane is not normalising its usage; "
            "clamping uncached to 0, so this call's uncached input prices at "
            "$0 (see provider_gateway:168-180)",
            provider or "?", model_id, c_read, c_write, t_in)
        uncached = 0
        priced["uncached"] = "clamped"
    usd = (uncached * p.input_per_token) + (t_out * p.output_per_token)
    if c_read:
        rate = p.cache_read if p.cache_read is not None else p.input_per_token
        priced["cache_read"] = ("published" if p.cache_read is not None
                                else "input_rate_fallback")
        usd += c_read * rate
    if c_write:
        rate = p.cache_write if p.cache_write is not None else p.input_per_token
        priced["cache_write"] = ("published" if p.cache_write is not None
                                 else "input_rate_fallback")
        usd += c_write * rate
    return round(usd, 8), priced


def estimate_call_cost_usd(model_id: str, tokens_in: int, tokens_out: int, *,
                           cache_read_tokens: int = 0,
                           cache_write_tokens: int = 0,
                           provider: str | None = None) -> float | None:
    """List-price estimate for ONE call, or None when the model has no
    published per-token price (subscription, unlisted, or a cold catalog).

    None is the whole point: it is distinguishable from 0.0. The column is NOT
    NULL, so the caller does not write NULL — it writes 0.0 alongside a
    cost_method label from price_basis() (list | subscription | free |
    unpriced), which carries the same distinction where the column cannot.
    Cache-aware; the cache counts DEFAULT TO ZERO, so a caller
    that does not pass them gets exactly the old whole-input-at-input-rate
    number. See estimate_call_cost_detail(), which this wraps — a caller that
    needs to record which rates applied should call that one."""
    return estimate_call_cost_detail(
        model_id, tokens_in, tokens_out,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens, provider=provider)[0]


def price_basis(model_id: str, provider: str | None = None,
                credential: str | None = None) -> str:
    """Why a call's dollars are what they are — the cost_method vocabulary
    v113 declares: list | subscription | free | unpriced.

    Without this the gateway labelled EVERYTHING it could not price
    'unpriced' ("no published rate or a cold catalog"), including the two
    cases the platform actually knows the answer to: a flat-plan model has no
    per-token price by definition, and a local model is genuinely free. A
    migration that documents four values while the code writes two is a
    vocabulary that promises a distinction it never makes.

    `credential` (see estimate_call_cost_detail) outranks the catalog: the
    catalog row's `subscription` flag says which credential LISTED the model,
    not which one served this call. 'signin' is the plan; 'api_key' bills per
    token, so it is never 'subscription' — with no rate known it is
    'unpriced'."""
    if (provider or "").strip().lower() == "local":
        return "free"                       # the user's own hardware
    if credential == "signin":
        return "subscription"
    if credential == "api_key":
        return "unpriced"
    mid = str(model_id or "")
    if mid:
        # Subscription rows are deliberately excluded from cached_model_prices
        # (a flat plan has no per-token rate), so find them in the catalog
        # directly rather than inferring from the pricing miss.
        for src in (list(_DIRECT_MODELS_CACHE.values())
                    + [{"data": (_CATALOG_CACHE.get("data") or {}).get(
                        "openrouter_models") or []}]):
            for m in (src or {}).get("data") or []:
                if isinstance(m, dict) and str(m.get("id")) == mid:
                    if m.get("subscription"):
                        return "subscription"
                    break
    return "unpriced"


def _credential_split(per_call) -> tuple[list, list] | None:
    """(plan_calls, paid_calls) from a turn's per-call ledger
    (turn_allowance.TurnSpend.note_call entries), or None when no call named
    the credential that served it — every provider but a sign-in one, which
    leaves estimate_turn_cost exactly as it was. plan_calls ran on a sign-in
    (the user's plan); paid_calls are every other call."""
    calls = [c for c in (per_call or []) if isinstance(c, dict)]
    if not any(c.get("credential") for c in calls):
        return None
    plan = [c for c in calls if c.get("credential") == "signin"]
    paid = [c for c in calls if c.get("credential") != "signin"]
    return plan, paid


def _tiered_turn_cost(model_id: str, calls: list, t_in: int, provider,
                      credential) -> float | None:
    """A turn's price summed call by call, for a model with prompt-length
    tiers (ModelPrice.tier_for), or None to use the totals arithmetic.

    A tier is judged on ONE call's prompt. estimate_turn_cost is handed the
    turn's summed input, so on the totals three 40k-token Claude Haiku 5.5
    calls read as one 120k prompt and every token priced at the over-100k
    rates, five times what the vendor bills. The per-call ledger session.py
    passes carries each call's own length. It is used only when it accounts
    for exactly the input being priced; anything else (no ledger, a call the
    meter missed) keeps the totals. Calls whose usage was not reported are
    left out, as they are from the totals ('partial')."""
    p = _price_for(model_id)
    if p is None or not getattr(p, "tiers", ()):
        return None
    rows = [c for c in calls if c.get("usage") != "not_reported"]
    if not rows:
        return None
    try:
        parts = [(int(c.get("in") or 0), int(c.get("out") or 0),
                  int(c.get("cached") or 0), int(c.get("cache_write") or 0))
                 for c in rows]
    except (TypeError, ValueError):
        return None
    if sum(n for n, _o, _r, _w in parts) != t_in:
        return None
    total = 0.0
    for n_in, n_out, c_r, c_w in parts:
        usd, _ = estimate_call_cost_detail(
            model_id, n_in, n_out, cache_read_tokens=c_r, cache_write_tokens=c_w,
            provider=provider, credential=credential)
        if usd is None:
            return None
        total += usd
    return total


def estimate_turn_cost(model_id: str | None, provider: str | None,
                       tokens_in: int, tokens_out: int, *,
                       cache_read_tokens: int = 0,
                       cache_write_tokens: int = 0,
                       routed_model_id: str | None = None,
                       failed_over=None,
                       per_call=None) -> tuple[float | None, str]:
    """(usd, basis) for a WHOLE turn's token totals at list rates — the one
    estimator behind the price printed under a turn, a regenerated alternate
    and each side of an A/B compare.

    WHICH ID PRICES. `model_id` is the SERVED id (what the gateway reported
    back); `routed_model_id` is the id the route named. They differ on every
    call of the direct Anthropic lane by design — the wire rewrites
    `anthropic/claude-haiku-4.5` to a dated id that no price catalog keys on
    (turn_allowance._adopt_served has the measurement: routed id prices True,
    served id prices False). So the rule here is the meter's rule: price at
    the served id when it prices, else at the routed id when that prices,
    else report the basis word. Without this an Anthropic turn printed
    "unpriced" under every reply while the allowance was metering real
    dollars for it.

    `failed_over` is the gateway's failover record (a dict when the primary
    errored and the fallback answered). When it is set, the routed id is NOT
    a fallback for pricing: the served model is a genuinely different model
    that took the call, and if it has no rate the honest answer is the basis
    word, not the routed model's dollars labelled 'list'. Same refusal the
    meter makes (turn_allowance._adopt_served moves the basis instead).

    `basis` is the v113 cost_method vocabulary: 'list' when a price was
    computed, else why not — 'free' (local), 'subscription' (flat plan),
    'unpriced' (no published rate or a cold catalog). 'partial' is 'list'
    for a turn where only some priced calls reported usage: the dollars cover
    those calls alone. The reader prints the
    basis word where a dollar figure would go, so "$0" never stands in for
    "we could not price this" (the zero-that-looks-like-data this codebase
    refuses).

    Same arithmetic as the cost log's per-call rows (estimate_call_cost_detail),
    applied to the turn's accumulated counts. On a turn whose model FAILED
    OVER mid-way this prices every round at the model that finished it; the
    running allowance prices each call at the model in force for that call,
    so on a failover turn the two totals can differ by the pre-failover
    rounds. Stated so nobody reads the two as one number.

    COST FOLLOWS THE CREDENTIAL THAT SERVED EACH CALL. On a
    provider with a sign-in path (xai) each call names its credential, and
    `per_call` is the turn's per-call ledger carrying it. Calls on the sign-in
    spent the user's plan and add nothing per token; every other call prices
    as before. So: all on the sign-in -> 'subscription'; some on the key (the
    sign-in was connected, or switched off or removed, part way through the
    turn — the key is never used while one exists) -> only THOSE calls' tokens
    are priced; all on the key -> the totals, never labelled 'subscription'
    off a catalog row the sign-in listed. Without per_call, or when no call
    named a credential, nothing here changes."""
    try:
        t_in, t_out = int(tokens_in or 0), int(tokens_out or 0)
        c_r, c_w = int(cache_read_tokens or 0), int(cache_write_tokens or 0)
    except (TypeError, ValueError):
        return None, "unpriced"
    credential = None
    split = _credential_split(per_call)
    if split is not None:
        plan, paid = split
        if not paid:
            return None, "subscription"
        try:
            if plan:
                t_in = sum(int(c.get("in") or 0) for c in paid)
                t_out = sum(int(c.get("out") or 0) for c in paid)
                c_r = sum(int(c.get("cached") or 0) for c in paid)
                c_w = sum(int(c.get("cache_write") or 0) for c in paid)
        except (TypeError, ValueError):
            return None, "unpriced"
        if all(c.get("credential") == "api_key" for c in paid):
            credential = "api_key"
    # No call that would be priced reported usage (provider_gateway
    # _call_remote, usage not_reported): the tokens are unknown, so is the
    # price. Zero tokens at list would print a priced free turn.
    _priced_calls = (paid if split is not None
                     else [c for c in (per_call or []) if isinstance(c, dict)])
    if _priced_calls and all(c.get("usage") == "not_reported" for c in _priced_calls):
        return None, "unpriced"
    # Some of them reported and some did not: the dollars below cover only the
    # calls that reported, so they are a floor, and 'partial' says so where
    # 'list' would present them as the turn's whole price.
    _partial = any(c.get("usage") == "not_reported" for c in _priced_calls)
    served = str(model_id or "")
    routed = str(routed_model_id or "")
    if isinstance(failed_over, dict) and failed_over and served:
        routed = ""     # a real failover: only the model that answered may price
    for mid in (served, routed):
        if not mid:
            continue
        try:
            usd = _tiered_turn_cost(mid, _priced_calls, t_in, provider, credential)
            if usd is None:
                usd, _priced = estimate_call_cost_detail(
                    mid, t_in, t_out, cache_read_tokens=c_r, cache_write_tokens=c_w,
                    provider=provider, credential=credential)
        except Exception:  # noqa: BLE001 — a pricing miss is a basis, never a raise
            usd = None
        if usd is not None:
            return round(float(usd), 6), ("partial" if _partial else "list")
    return None, price_basis(served or routed, provider, credential=credential)


def turn_metrics_from_result(result, gen_ms: int | None) -> dict:
    """The SAME TurnMetrics shape circles' thread read builds from a stored
    row, for a turn that just ran and has no row to read back: a regenerated
    alternate, each side of an A/B compare. Tokens are the runtime's
    accumulated totals across every tool round of that run
    (session.send_message's resp); the price is estimate_turn_cost at the
    served model, with the basis word when there is none. This is what makes
    a regenerate a PRICE comparison against the original,
    not only a text one. Lives here, beside the estimator, so it imports
    without the route module's server-wide import chain."""
    r = result if isinstance(result, dict) else {}
    try:
        tokens_in = int(r.get("input_total_tokens") or r.get("prompt_tokens") or 0)
        tokens_out = int(r.get("completion_tokens") or 0)
        cached = int(r.get("cache_read_tokens") or 0)
        cache_write = int(r.get("cache_write_tokens") or 0)
    except (TypeError, ValueError):
        tokens_in = tokens_out = cached = cache_write = 0
    model = r.get("model")
    provider = r.get("provider")
    ctx = r.get("context") if isinstance(r.get("context"), dict) else {}
    if ctx.get("price_basis"):
        # The run already priced itself (session.py stamps the same numbers on
        # resp["context"] as on the turn row), from its FULL per-call ledger —
        # including which credential served each call. Re-deriving from the
        # totals here could not see that, and would print a sign-in turn at
        # OpenRouter's per-token list price.
        usd, basis = ctx.get("est_cost_usd"), ctx["price_basis"]
    else:
        # `routed_model` rides the runtime's resp beside the served `model` so
        # a replay on the direct Anthropic lane prices at the id that has a rate.
        usd, basis = estimate_turn_cost(model, provider, tokens_in, tokens_out,
                                        cache_read_tokens=cached,
                                        cache_write_tokens=cache_write,
                                        routed_model_id=r.get("routed_model"),
                                        failed_over=r.get("failed_over"))
    return {
        "gen_ms": gen_ms,
        "tokens_total": tokens_in + tokens_out,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_cached": cached or None,
        "tokens_cache_write": cache_write or None,
        "model": model,
        "provider": provider,
        "failed_over": r.get("failed_over"),
        "est_cost_usd": usd,
        "price_basis": basis,
    }


def openrouter_vendor_prefix(provider: str | None) -> str | None:
    """The OpenRouter id prefix for a first-party provider ("openai" →
    "openai/"), or None. Lets the turn path's window lookup try the
    prefixed id when a bare first-party id misses the catalog."""
    return _OR_VENDOR_PREFIX.get(provider or "")


def _row_cache_tokens(r) -> tuple[int, int]:
    """(cache_read, cache_write) for one inference_cost_log row, from the
    COLUMNS where they exist and from the metadata JSONB where they do not.

    Both places are read because both are real: the counts have ridden the
    metadata since provider_gateway started logging them (:292-293) and the
    columns arrive only with migrations/v148, so every row written before that
    migration ran has them in the JSON and nowhere else. asyncpg hands a JSONB
    column back as a dict or as a str depending on codec registration, so both
    shapes are parsed — same defensive read as _lane_of in routes/agents.py."""
    def _col(name):
        try:
            return r[name]
        except (KeyError, IndexError, TypeError):
            return None
    read, write = _col("cache_read_tokens"), _col("cache_write_tokens")
    if read is None or write is None:
        meta = _col("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if read is None:
            read = meta.get("cache_read_tokens")
        if write is None:
            write = meta.get("cache_write_tokens")
    try:
        return max(0, int(read or 0)), max(0, int(write or 0))
    except (TypeError, ValueError):
        return 0, 0


def _row_credential(r) -> str | None:
    """Which credential served one inference_cost_log row — the `credential`
    _call_remote writes into the metadata for a sign-in provider (xai). A row
    of such a provider WITHOUT one predates the sign-in path, when every one of
    its calls went out on the API key, so it reads 'api_key'. Every other
    provider: None, which leaves its pricing exactly as it was."""
    try:
        meta = r["metadata"] or {}
    except (KeyError, IndexError, TypeError):
        meta = {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = {}
    cred = meta.get("credential") if isinstance(meta, dict) else None
    if cred:
        return str(cred)
    try:
        from server.vault.providers import signin_serves_inference
        return "api_key" if signin_serves_inference(r["provider"] or "") else None
    except Exception:  # noqa: BLE001 — a row we cannot read prices as before
        return None


async def reprice_unpriced(*, max_rows: int = 20000) -> dict:
    """Re-derive cost for inference rows written while the catalog was cold.

    WHY THIS IS NEEDED AT ALL. est_cost_usd and cost_method are computed ONCE,
    at write time, from an in-memory cache that warm_model_catalog() fills
    after a deliberate 0-30s jitter. Any call in that window — and every call
    on every pod after a fleet roll — is written 'unpriced' at $0 and never
    revisited. Measured on one Host: the SAME model and provider
    flips within a single day (k3/kimi on 08-28: 25 'subscription' and 33
    'unpriced'; on 09-03: 23 and 145), which is the signature of cache
    temperature rather than of anything about the calls. 1,009 of 1,057 k3
    calls read 'unpriced' when the platform demonstrably knows they are a flat
    plan, and claude-fable-5-1 went 24 unpriced on 09-06 then 22 priced on
    09-07 for $10.06.

    EFFECT IN THE ORDER THIS ACTUALLY RUNS, and the order changes the answer:
    the catalog warms FIRST, then the direct providers below. Over the 1,074
    unpriced rows that yields 36 priced ($18.67), 1,009 relabelled
    k3 -> subscription, and 29 left unpriced. An earlier draft of this
    docstring said 13 and $0.0277 — measured with the direct caches warmed
    BEFORE the catalog, which is not what happens here. With the catalog first,
    _enrich_first_party_from_openrouter() fills claude-fable-5-1 from its
    OpenRouter row, so fable's 23 rows price at Anthropic list. Correct for
    this entity: its anthropic credential is an api_key, i.e. pay-per-token.

    A SEMANTIC CHOICE, recorded because whoever next touches pricing will meet
    it: 13 of those 36 are kimi-k3/kimi-k2.6, priced from the moonshotai/*
    OpenRouter listing, on the SAME flat-plan key whose k3 rows are labelled
    'subscription'. One plan, two treatments. That is deliberate rather than
    overlooked — it is precisely what the write path does, and matching it is
    what keeps a re-priced row and a freshly written one in agreement. It
    follows from what the estimate lane claims to be: the LIST cost of the
    tokens used, with 'subscription' reserved for models positively known to be
    flat-plan. If that reading should change, change it in
    estimate_call_cost_detail() so BOTH paths move together — never here alone.

    Nothing was lost, which is why this is a backfill and not new
    instrumentation: model_id, provider and both token counts are on every
    row. Only the derived cost was thrown away.

    SAFETY. Touches rows with cost_method='unpriced' ONLY — a 'list',
    'subscription' or 'free' row is already answered and is never restated.
    Prices come from estimate_call_cost_detail()/price_basis(), the SAME two
    functions the write path calls (provider_gateway:242-257) —
    estimate_call_cost_usd() is a thin wrapper over the first — so a re-priced
    row and a freshly-written one cannot disagree. A model still unknown to
    the catalog stays 'unpriced' — this never invents a number. Idempotent:
    once a row is priced it no longer matches, so a second pass is a no-op.

    CACHE: the cache counts have ridden the metadata JSONB since
    they were first logged, and rows written before v148 have them ONLY there.
    They are read from either place and passed to the estimator, so a row this
    pass prices gets the same cache-aware number the write path would give it
    today. The `priced` marker goes on with it — which is what lets
    reprice_cache_blind() below find the rows that were priced before any of
    this existed.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"repriced": 0, "skipped": "no pool"}
    try:
        rows = await pool.fetch(
            """SELECT id, model_id, provider, tokens_in, tokens_out,
                      cache_read_tokens, cache_write_tokens, metadata
                 FROM inference_cost_log
                WHERE cost_method = 'unpriced'
                  AND purpose = 'inference'
                  -- NULL tokens: the provider reported no usage (v214). Its
                  -- price is unknown, and 0 tokens at list would restate it
                  -- as a priced free call.
                  AND tokens_in IS NOT NULL
                ORDER BY created_at DESC
                LIMIT $1""", int(max_rows))
        # THE FIRST-PARTY CACHE IS THE ONE THAT MATTERS, and it is not the
        # OpenRouter catalog. price_basis() and estimate_call_cost_usd() read
        # _DIRECT_MODELS_CACHE too, and that is filled ONLY by _direct_models(),
        # whose callers are all UI routes (the Models tab, the coding matrix).
        # Background inference is 88% of calls here and never touches them, so
        # it runs with that cache empty and writes 'unpriced'.
        #
        # Measured on one Host: with the OpenRouter catalog fully warm (431
        # models) but the direct caches cold, 1,074 unpriced rows yielded 13
        # prices and ZERO relabels. Warming the direct providers first turned
        # 1,009 of them into 'subscription' — k3 is a flat plan, which the pod
        # could answer all along and simply had not loaded.
        for prov in {r["provider"] for r in rows if r["provider"]}:
            try:
                await _direct_models(prov)
            except Exception as exc:            # a dead provider must not stop the pass
                logger.debug("reprice: could not warm %s models (%s)", prov, exc)
    except Exception as exc:
        logger.warning("reprice: could not read the log (%s)", exc)
        return {"repriced": 0, "skipped": "read failed"}
    if not rows:
        return {"repriced": 0, "relabelled": 0}

    priced: list[tuple] = []
    relabelled: list[tuple] = []
    for r in rows:
        if r["tokens_in"] is None or r["tokens_out"] is None:
            continue        # usage not reported (v214): unknown stays unpriced
        model, provider = r["model_id"], r["provider"]
        c_read, c_write = _row_cache_tokens(r)
        # The credential that served THIS row decides its basis, never the
        # catalog's current flag: once the catalog is listed on a sign-in its
        # rows read 'subscription', and without this every key-served row of
        # that model would be relabelled a plan call.
        cred = _row_credential(r)
        try:
            # estimate_call_cost_usd() is the one-value wrapper over this; the
            # detail form is used because the marker has to land on the row.
            usd, marker = estimate_call_cost_detail(
                model, int(r["tokens_in"] or 0), int(r["tokens_out"] or 0),
                cache_read_tokens=c_read, cache_write_tokens=c_write,
                provider=provider, credential=cred)
        except Exception:
            usd, marker = None, {}
        if usd is not None:
            priced.append((r["id"], float(usd),
                           json.dumps({"priced": marker})))
            continue
        # No per-token rate — but say WHICH kind of not-priced. 'unpriced' is
        # already what the row says, so only a CHANGE is worth a write.
        try:
            basis = price_basis(model, provider, credential=cred)
        except Exception:
            basis = "unpriced"
        if basis != "unpriced":
            relabelled.append((r["id"], basis))

    try:
        if priced:
            await pool.executemany(
                "UPDATE inference_cost_log SET est_cost_usd = $2, "
                "cost_method = 'list', "
                # MERGE, never replace: the metadata already carries the cache
                # counts and whatever the caller put there, and this row's only
                # new fact is which rates were applied.
                "metadata = coalesce(metadata, '{}'::jsonb) || $3::jsonb "
                "WHERE id = $1 AND cost_method = 'unpriced'",
                priced)
        if relabelled:
            await pool.executemany(
                "UPDATE inference_cost_log SET cost_method = $2 "
                "WHERE id = $1 AND cost_method = 'unpriced'", relabelled)
    except Exception as exc:
        logger.warning("reprice: update failed (%s)", exc)
        return {"repriced": 0, "skipped": "write failed"}

    if priced or relabelled:
        logger.info("reprice: %d rows priced from the rate card, %d relabelled "
                    "(of %d unpriced examined)",
                    len(priced), len(relabelled), len(rows))
    return {"repriced": len(priced), "relabelled": len(relabelled),
            "examined": len(rows)}


async def reprice_cache_blind(*, max_rows: int = 20000) -> dict:
    """Re-derive cost for rows priced BEFORE the estimate knew about caching.

    THE DEFECT BEING CORRECTED. The write path once handed
    estimate_call_cost_usd() the lane-normalized TOTAL input and nothing else,
    so every cached token was priced at the full input rate
    (provider_gateway:242-249 against the counts already sitting in the row's
    own metadata). Cache reads are commonly a tenth of the input rate
    (anthropic/claude-opus-5: 0.0000005 vs 0.000005, verified live against
    openrouter.ai/api/v1/models 2026-09-15), so those rows read HIGH — the
    opposite direction from the $0 problem v113 was written for, and just as
    wrong for a budget to be built on.

    WHICH ROWS, and why the marker is the test rather than a date. A row
    qualifies when all three hold:
      * cost_method = 'list'      — it was priced; nothing else was.
      * metadata has no 'priced'  — no pass has ever recorded which rates it
                                    applied, which is true of exactly the rows
                                    written by the cache-blind formula.
      * metadata carries a NON-ZERO cache_read/cache_write count — with no
                                    cached tokens the two formulas give the
                                    identical number, so there is nothing to
                                    correct and nothing to restate.
    A date cutoff would need the ship time of this change on each host and
    would be wrong on any host that rolled late; the marker is carried by the
    data itself.

    SAFETY, and it is the SAME rule as reprice_unpriced's, applied to a
    different starting state. That pass may never touch an answered row; this
    one touches ONLY answered rows, so the guard cannot be 'unpriced' and
    instead is: the row must still be 'list' AND still be unmarked at UPDATE
    time (both in the WHERE), so a concurrent writer or a second pass cannot
    restate it. 'subscription', 'free' and 'unpriced' rows are never touched —
    a deliberately-answered row stays answered, and an unpriced one is
    reprice_unpriced's business, not this one's. Idempotent by construction:
    the marker this writes is what disqualifies the row next time.

    It never invents: the price comes from estimate_call_cost_detail(), the
    write path's own function, and a model the catalog can no longer price is
    left exactly as it is rather than zeroed.

    IT RE-DERIVES AT TODAY'S CATALOG PRICE, not at the price that was live when
    the call was made — the same property reprice_unpriced has, stated here
    because this is the pass that moves rows which already carried a number. A
    row whose model has since changed price therefore moves by that delta as
    well as by the cache correction, and afterwards the two are not separable.
    That is the honest reading of what this lane claims to be (the LIST cost of
    the tokens used, from the only rate card the pod has), and the alternative
    is not available: nothing records the rate that applied at call time, so
    "re-derive at the historical price" is a number the platform cannot
    produce. If per-row rate history is ever wanted it has to be captured at
    WRITE time — it cannot be recovered here."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"repriced": 0, "skipped": "no pool"}
    try:
        rows = await pool.fetch(
            """SELECT id, model_id, provider, tokens_in, tokens_out,
                      cache_read_tokens, cache_write_tokens, metadata
                 FROM inference_cost_log
                WHERE cost_method = 'list'
                  AND purpose = 'inference'
                  AND NOT (metadata ? 'priced')
                  -- Compared AS JSONB rather than cast to bigint: a cast runs
                  -- on whatever the row actually holds, and one row with a
                  -- non-numeric value there would abort the whole SELECT. An
                  -- absent key compares NULL and the row drops out, which is
                  -- the right answer — no cached tokens, nothing to correct.
                  AND (metadata->'cache_read_tokens' <> '0'::jsonb
                       OR metadata->'cache_write_tokens' <> '0'::jsonb)
                ORDER BY created_at DESC
                LIMIT $1""", int(max_rows))
        for prov in {r["provider"] for r in rows if r["provider"]}:
            try:
                await _direct_models(prov)     # same warm as reprice_unpriced
            except Exception as exc:
                logger.debug("reprice_cache: could not warm %s models (%s)",
                             prov, exc)
    except Exception as exc:
        logger.warning("reprice_cache: could not read the log (%s)", exc)
        return {"repriced": 0, "skipped": "read failed"}
    if not rows:
        return {"repriced": 0, "examined": 0}

    updates: list[tuple] = []
    for r in rows:
        c_read, c_write = _row_cache_tokens(r)
        if not (c_read or c_write):
            continue                # the two formulas agree; leave it alone
        try:
            usd, marker = estimate_call_cost_detail(
                r["model_id"], int(r["tokens_in"] or 0),
                int(r["tokens_out"] or 0),
                cache_read_tokens=c_read, cache_write_tokens=c_write,
                provider=r["provider"], credential=_row_credential(r))
        except Exception:
            usd = None
        if usd is None:
            continue                # can't price it now — the old number stays
        updates.append((r["id"], float(usd), json.dumps({"priced": marker})))

    try:
        if updates:
            await pool.executemany(
                "UPDATE inference_cost_log SET est_cost_usd = $2, "
                "metadata = coalesce(metadata, '{}'::jsonb) || $3::jsonb "
                "WHERE id = $1 AND cost_method = 'list' "
                "  AND NOT (metadata ? 'priced')", updates)
    except Exception as exc:
        logger.warning("reprice_cache: update failed (%s)", exc)
        return {"repriced": 0, "skipped": "write failed"}

    if updates:
        logger.info("reprice_cache: %d rows re-derived cache-aware "
                    "(of %d cache-blind 'list' rows examined)",
                    len(updates), len(rows))
    return {"repriced": len(updates), "examined": len(rows)}


async def warm_model_catalog() -> None:
    """Fill the catalog cache once, off the request path (server startup).
    Until this ran, cached_context_lengths() was empty on every fresh pod
    until a browser happened to open a model picker — so voice, cron,
    peer-host and API traffic ran with 'window unknown' and no budgeting.
    Same build + same never-poison-a-good-cache rule as GET /catalog; a
    failed fetch leaves the cache empty and the 30s retry TTL in place."""
    try:
        # A fleet roll restarts hundreds of pods inside minutes; spread the
        # first fetch so OpenRouter sees a trickle, not a wall. A first turn
        # inside this window simply runs on the legacy caps, as before.
        import random
        await asyncio.sleep(random.uniform(0, 30))
        now = time.monotonic()
        result = await asyncio.to_thread(_build_model_catalog)
        ok = result.get("total_openrouter", 0) > 0
        if not ok and (_CATALOG_CACHE.get("data") or {}).get("total_openrouter", 0) > 0:
            return
        result["degraded"] = not ok
        _CATALOG_CACHE["data"] = result
        _CATALOG_CACHE["exp"] = now + (_CATALOG_TTL_OK if ok else _CATALOG_TTL_FAIL)
        logger.info("model catalog warmed at startup: %d openrouter models%s",
                    result.get("total_openrouter", 0), "" if ok else " (degraded)")
        # The cache is warm NOW — but every call logged before this moment was
        # priced against a COLD one and written 'unpriced' permanently. The
        # sleep above is 0-30s by design and a fleet roll restarts every pod at
        # once, so that window is routine, not an edge case. Re-price what we
        # can now answer.
        if ok:
            await reprice_unpriced()
            # And the other stale-derivation class: rows that WERE priced, by
            # the formula that charged every cached token at the full input
            # rate. Same gate (a degraded catalog must not re-derive anything)
            # and the same never-restate-an-answered-row discipline, expressed
            # there as the marker guard.
            await reprice_cache_blind()
    except Exception as e:
        logger.warning("model catalog startup warm failed (non-fatal): %s", e)


async def _table_local_models() -> list[dict]:
    """Local models for the catalog, from the host's LOCAL MODEL TABLE
    (network_bastion_heartbeat.models via mastercode.model_backends) — the
    same source as GET /local-models. Replaces the retired pod-loopback
    Ollama probe (pod-local Ollama is removed, so that probe could
    only ever return []). Fetched fresh on every catalog request — the
    table follows bastion liveness, so it must not sit behind the 5-min
    catalog cache — and overlaid onto the cached OpenRouter build."""
    pool = brain_service.get_pool()
    if not pool:
        return []
    from mastercode.model_backends import list_local_models
    rows = await list_local_models(pool, brain_service._entity_id)
    return [
        {"id": r["id"], "name": r["id"], "provider": "local",
         "available": True, "bastion": r.get("bastion")}
        for r in rows
    ]


def _or_cache_rates(pricing: dict) -> dict:
    """OpenRouter's published per-token CACHE rates for one model, as catalog-row
    keys — {} when it publishes none.

    KEY NAMES VERIFIED LIVE against https://openrouter.ai/api/v1/models on
    2026-09-15: of 443 models, 276 publish `input_cache_read` and 82 publish
    `input_cache_write`, in the SAME units and the same USD-per-token string
    form as `prompt`/`completion` (anthropic/claude-opus-5 reads prompt
    0.000005, input_cache_read 0.0000005, input_cache_write 0.00000625).

    ABSENT IS ABSENT. Not one of the 443 publishes `input_cache_read: "0"`, so a
    missing key means "this provider did not say", never "cache reads are free"
    — and the estimator must fall back to the input rate rather than to zero.

    `input_cache_write_1h` (31 models) is deliberately NOT read: it is the rate
    for the 1-hour cache TTL, and the gateway asks for no such thing — it marks
    its prefix `cache_control {"type": "ephemeral"}` with no `ttl` key at all
    (provider_gateway.py:584 and :1024), i.e. the default TTL, whose rate is
    `input_cache_write`.

    A ZERO CACHE RATE BESIDE A NON-ZERO INPUT RATE IS NOT READ AS FREE. Today's
    sweep found none — zero of 443 models publish "0" for either key — so this
    costs nothing now and exists because of what it would mean if that changed:
    a free cache read on a paid model would zero out ~84% of a turn's input
    cost, which is a far larger error than the one this whole change corrects,
    and it is indistinguishable from a serialisation bug upstream. Treated as
    unparsed, so the estimator falls back to the input rate and labels it —
    the same "we do not know, and we err high" the missing-key path takes, and
    the same rule the Together branch applies to its own units (:1885-1893).
    Logged at DEBUG, not warning: nothing is known to emit it, and a warning
    for a condition with no observed instance is noise until it has one.

    A zero cache rate on a zero input rate IS kept — that is a free model
    stating it is free about every part of itself, and there is no discrepancy
    to be suspicious of."""
    out: dict = {}
    _pin = None
    try:
        _p = (pricing or {}).get("prompt")
        _pin = None if _p is None else float(_p)
    except (TypeError, ValueError):
        _pin = None
    for src, dst in (("input_cache_read", "cache_read_per_token"),
                     ("input_cache_write", "cache_write_per_token")):
        v = (pricing or {}).get(src)
        if v is None:
            continue
        try:
            rate = float(v)
        except (TypeError, ValueError):
            continue
        if rate == 0.0 and _pin is not None and _pin > 0.0:
            logger.debug(
                "openrouter publishes %s=0 against a non-zero prompt rate of "
                "%g — treating the cache rate as unpublished rather than free",
                src, _pin)
            continue
        out[dst] = rate
    return out


def _or_price_tiers(pricing: dict) -> dict:
    """OpenRouter's prompt-length price tiers for one model, as the catalog-row
    key `price_tiers` — {} when it publishes none, so a flat-priced row is
    unchanged.

    SHAPE READ LIVE from openrouter.ai/api/v1/models on 2026-10-07, for
    anthropic/claude-haiku-5.5: pricing.overrides = [{"min_prompt_tokens":
    100000, "prompt": "0.0000005", "completion": "0.0000025",
    "input_cache_read": "0.00000005", "input_cache_write": "0.000000625",
    "input_cache_write_1h": "0.000001"}], the same USD-per-token strings as
    the base prompt/completion. Anthropic's pricing page states the same two
    rate sets.

    Each tier keeps only what it publishes, under the row's own key names;
    cached_model_prices decides what an absent rate inherits. An override
    with no readable positive minimum is dropped: a tier with no threshold
    would apply at every length, which is a guess about the provider's
    price, not its published price. The cache keys pass through
    _or_cache_rates, so a tier gets the same zero-is-not-free rule as the
    base row."""
    tiers = []
    for o in (pricing or {}).get("overrides") or []:
        if not isinstance(o, dict):
            continue
        try:
            floor = int(o.get("min_prompt_tokens"))
        except (TypeError, ValueError):
            continue
        if floor <= 0:
            continue
        t: dict = {"min_prompt_tokens": floor}
        for src, dst in (("prompt", "input_per_token"),
                         ("completion", "output_per_token")):
            v = _dm_num(o.get(src))
            if v is not None:
                t[dst] = v
        t.update(_or_cache_rates(o))
        tiers.append(t)
    return {"price_tiers": sorted(tiers, key=lambda t: t["min_prompt_tokens"])} if tiers else {}


def _or_max_output(m: dict) -> int | None:
    """OpenRouter's published output limit for one model
    (top_provider.max_completion_tokens), or None when it publishes none."""
    tp = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
    v = _dm_num(tp.get("max_completion_tokens"))
    return int(v) if v and v > 0 else None


def _build_model_catalog() -> dict:
    """Blocking catalog build (Ollama + OpenRouter HTTP). Runs in a worker
    thread via asyncio.to_thread — never call directly from the event loop."""
    import urllib.request

    # Local models come from the heartbeat table (_table_local_models),
    # overlaid async by get_model_catalog — never from a pod-loopback
    # probe (pod-local Ollama is removed).
    local_models: list = []

    from mastercode.inference_config import BILLING_MODE, OPENROUTER_BASE_URL

    # Live OpenRouter catalog.
    #
    # The entity pod has no direct internet — its only egress is the user bastion
    # proxy (HTTPS_PROXY in the pod env). The models endpoint is public (no key
    # needed: an empty/absent Authorization still returns the full 339-model list),
    # so the ONLY way this comes back empty is a transport failure: a transient
    # proxy/DNS blip, or — the e2e-observed case — the proxy env not yet present so
    # the pod's default-deny network policy blocks the request. A single such blip
    # used to silently yield 0 models (swallowed below) and blank the picker. So:
    # build an explicit proxy-aware opener from the live env (don't rely on a
    # urllib global captured at import time) and RETRY a few times before giving up.
    openrouter_models = []
    fetch_ok = False
    proxies = urllib.request.getproxies()  # reads HTTP(S)_PROXY from the live env
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    # The models endpoint is public; a key only personalizes the list. There is
    # no platform key — the env rung is the user's own key.
    key = os.environ.get("OPENROUTER_API_KEY")
    headers = {"User-Agent": "cseries-kernel/0.11"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # The base the chat calls use (OPENROUTER_BASE_URL), so the list and the
    # calls come from the same place.
    req = urllib.request.Request(
        f"{OPENROUTER_BASE_URL}/models", headers=headers,
    )
    last_err = None
    for attempt in range(3):
        try:
            resp = opener.open(req, timeout=10)
            body = json.loads(resp.read())
            for m in body.get("data", []):
                mid = m.get("id")
                if not mid:
                    continue
                pricing = m.get("pricing") or {}
                # MODALITIES. Carried because nothing in the
                # platform could previously answer "which models can do
                # images" — the Orchestrator's image roles fell back to a
                # free-text BYO endpoint box, i.e. the user guessing. Verified
                # live against openrouter.ai/api/v1/models on 2026-08-31: 395
                # models, 229 with 'image' in input_modalities (vision), 11 in
                # output_modalities (generation). Absent/malformed reads as an
                # EMPTY list, never an assumed ["text"] — an unknown capability
                # must not render as a claimed one.
                arch = m.get("architecture") or {}
                def _mods(k):
                    v = arch.get(k)
                    return [str(x) for x in v] if isinstance(v, list) else []
                openrouter_models.append({
                    "id": mid,
                    "name": m.get("name") or mid,
                    "provider": "openrouter",
                    "available": True,
                    # VALIDATED: passed the admission bench under
                    # the current suite, per the platform-shipped list
                    # (runtime/model_validation). A product-truth label like
                    # the price, never a per-host opinion.
                    "validated": _validated("openrouter", mid),
                    # THE HOST MODEL STANDARD: per-model verdict,
                    # same for every provider serving the model.
                    "standard": _standard("openrouter", mid),
                    "context_length": m.get("context_length"),
                    # THE MODEL'S OWN OUTPUT LIMIT.
                    # OpenRouter publishes it per model as
                    # top_provider.max_completion_tokens (anthropic/claude-
                    # opus-5.5 reads 128000, moonshotai/kimi-k3 943718). The
                    # turn sent a fixed 16,384 instead, and a desktop design
                    # was cut off mid tool call. Absent stays absent: None is
                    # "not published", never a limit.
                    "max_output_tokens": _or_max_output(m),
                    "input_per_token": pricing.get("prompt"),
                    "output_per_token": pricing.get("completion"),
                    # CACHE RATES, same units as prompt/completion.
                    # Key absent -> key absent on the row; see _or_cache_rates.
                    **_or_cache_rates(pricing),
                    # PROMPT-LENGTH TIERS; see _or_price_tiers.
                    **_or_price_tiers(pricing),
                    "description": (m.get("description") or "")[:240],
                    "input_modalities": _mods("input_modalities"),
                    "output_modalities": _mods("output_modalities"),
                })
            fetch_ok = True
            break
        except Exception as e:
            last_err = e
            openrouter_models = []  # partial parse on a mid-read failure — start clean
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    if not fetch_ok:
        logger.warning(
            "openrouter catalog fetch failed after 3 attempts (proxies=%s): %s",
            list(proxies.keys()), last_err,
        )

    # Embedding catalog is sourced from /api/embedding/catalog (through
    # the bridge to the platform). Inference catalog stays focused on
    # chat/research/code routing. Pod-local Ollama is removed.
    embedding_models: list = []

    return {
        "local_models": local_models,
        "openrouter_models": openrouter_models,
        "embedding_models": embedding_models,
        "billing_mode": BILLING_MODE,
        "openrouter_key_set": bool(os.environ.get("OPENROUTER_API_KEY")),
        "roles": ROLES,
        "role_descriptions": ROLE_DESCRIPTIONS,
        "total_openrouter": len(openrouter_models),
        "total_embedding": len(embedding_models),
    }


# ---------------------------------------------------------------------------
# Favorites — the user-built model shortlist.
#
# The catalog returns ~339 OpenRouter models; the picker DEFAULTS to this
# user-curated favorites set instead of dumping the whole list. Stored at
# behavioral_params.favorite_models (a JSON list of strings). The user owns
# their selection — there is no curated/validated/benchmark set anymore.
#
# Entries are provider-scoped: "provider::model_id" — the same
# model id on two providers is two different accounts, so the favorite must
# name whose key pays. Legacy bare slugs read as openrouter, and openrouter
# entries KEEP the bare form on write so the stored list stays compatible
# with anything still expecting bare slugs. Migrate-on-read only: a read
# never rewrites storage.
# ---------------------------------------------------------------------------

class FavoriteModel(BaseModel):
    model_id: str
    provider: str | None = None   # omitted → openrouter (the legacy default)


def _split_favorite(entry: str) -> tuple[str, str]:
    """One stored favorite → (provider, model_id). 'provider::model_id' parses
    directly; a legacy bare slug reads as openrouter."""
    s = (entry or "").strip()
    if "::" in s:
        p, _, m = s.partition("::")
        p, m = p.strip().lower(), m.strip()
        if p and m:
            return p, m
    return "openrouter", s


def _favorites_from_params(params: dict) -> list[str]:
    """behavioral_params.favorite_models as a de-duped, order-preserving list.
    Tolerant of a missing/non-list value (fresh entities)."""
    favs = params.get("favorite_models") or []
    if not isinstance(favs, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for f in favs:
        s = str(f).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _favorites_by_provider(favs: list[str]) -> dict:
    """Stored favorites grouped by provider — {"openrouter": [...], "kimi":
    [...]} — bare model ids per group, de-duped, order-preserving."""
    out: dict[str, list[str]] = {}
    for f in favs:
        p, m = _split_favorite(f)
        if m and m not in out.setdefault(p, []):
            out[p].append(m)
    return out


def _favorites_payload(favs: list[str]) -> dict:
    """The favorites read shape: legacy bare openrouter slugs under
    `favorites` (circles-web reads that), plus the provider-scoped grouping
    the Orchestrator picker uses."""
    by_provider = _favorites_by_provider(favs)
    return {"favorites": by_provider.get("openrouter", []),
            "by_provider": by_provider}


async def _load_favorites(pool, eid) -> list[str]:
    """Read behavioral_params.favorite_models as a de-duped, order-preserving
    list of stored entries. Tolerant of a NULL/str behavioral_params."""
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)
    if not isinstance(params, dict):
        params = {}
    return _favorites_from_params(params)


@router.get("/favorites", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_favorites() -> dict:
    """The user's favorited models — the default view of the model catalog."""
    pool = brain_service.get_pool()
    if not pool:
        return {"favorites": [], "by_provider": {}}
    return _favorites_payload(await _load_favorites(pool, brain_service._entity_id))


@router.post("/favorites", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def add_favorite(body: FavoriteModel) -> dict:
    """Add a model to favorites (idempotent per provider). {model_id} alone
    favorites the openrouter entry (legacy); {provider, model_id} scopes the
    pick to that provider's account. Returns the updated favorites payload."""
    model_id = (body.model_id or "").strip()
    if not model_id:
        raise HTTPException(400, "model_id required")

    from mastercode.inference_config import normalize_provider
    from server.vault.providers import PROVIDERS
    provider, model_id = normalize_provider(
        (body.provider or "").strip() or "openrouter", model_id)
    if not provider or (provider != "local" and provider not in PROVIDERS):
        raise HTTPException(400, f"unknown provider {body.provider!r}")
    # Stored form: a bare slug for openrouter (legacy-compatible), else
    # "provider::model_id".
    entry = model_id if provider == "openrouter" else f"{provider}::{model_id}"

    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)
    if not isinstance(params, dict):
        params = {}

    favs = _favorites_from_params(params)
    # Dedup on the PARSED pair so a legacy bare slug and its "openrouter::"
    # form can't coexist as two rows for the same pick.
    if (provider, model_id) not in {_split_favorite(f) for f in favs}:
        favs.append(entry)
    params["favorite_models"] = favs

    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )
    return _favorites_payload(favs)


@router.delete("/favorites/{model_id:path}", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def remove_favorite(model_id: str) -> dict:
    """Remove a model from favorites. Returns the updated payload. The path
    param is a bare slug (openrouter) or the URL-encoded 'provider::model_id'
    form for non-openrouter entries; the :path converter keeps slugs with a
    '/' (e.g. a vendor/model id) intact."""
    target = _split_favorite(model_id)
    if not target[1]:
        raise HTTPException(400, "model_id required")

    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)
    if not isinstance(params, dict):
        params = {}

    # Match on the parsed pair so either stored form (bare / "provider::")
    # is found.
    favs = [f for f in _favorites_from_params(params)
            if _split_favorite(f) != target]
    params["favorite_models"] = favs

    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )
    return _favorites_payload(favs)


ROLE_DESCRIPTIONS = {
    "chat":          "Talks with you — every Circles and Workbench turn, the VS Code editor, and every lane that has no model of its own: the Workbench's phase planner, web research, and the background agents your host dispatches.",
    "embedding":     "Vector embedding model. Used by every memory write + retrieval. Picking a different dim re-embeds existing rows on the next memory-app sweep. Local options use your pod's Ollama; platform options use the platform's hosted catalog over the bridge.",
}

# Coding Engines (Orchestrator): one row per harness — connection,
# its default model, its default effort, and ONE "Default" tick. The per-engine
# overrides persist at behavioral_params.coding_harnesses ([{id, model?,
# effort?}]; an entry exists only where an override was saved — it is NOT a
# selection any more) and the tick at behavioral_params.default_coding_harness
# (harness_state). An unnamed build runs on the default engine
# (review.resolve_build_target); which engines take a task AT ONCE is never
# stored here — that is a coding protocol (server/routes/coding_protocols.py)
# the host reads and drives through build / ladder.run.
# HARNESS_APP_IDS is owned by review.py — import so there is ONE source of truth.
from server.routes.review import HARNESS_APP_IDS, known_coding_harness_entries  # noqa: E402

HARNESS_LABELS = {
    "claude-code": "Claude Code",
    "codex":       "Codex",
    "cursor":      "Cursor",
    "grok":        "Grok",
    "kimi":        "Kimi Code",
    "antigravity": "Antigravity CLI",
}

# Per-harness MODEL OPTIONS — no typed model names anywhere (a typed pick is a
# guess the user can't verify until the configuration fires).
# Each harness runs its own provider's models, so the option list is DERIVED
# from the live OpenRouter catalog by vendor slug — native ids (the slug minus
# the vendor prefix) are what each harness's --model actually takes. Two
# exceptions: claude-code takes Anthropic-native ids, listed by
# _claude_code_models (HOST_ENGINE_MODELS plus the catalog's Claude rows,
# dashed); opencode runs ANY OpenRouter model and takes the full slug
# verbatim (None below).
HARNESS_MODEL_VENDORS: dict = {
    # codex is NOT derived from the catalog any more — see CODEX_CLI_MODELS
    # below. The reseller's view of OpenAI (openai/gpt-5.3-codex still listed)
    # is not what the pinned Codex CLI can serve (rust-v0.149.0 dropped it);
    # A task once dispatched exactly that phantom model.
    "grok":     ("x-ai",),
    # cursor is NOT derived from the catalog any more, for the
    # same reason as codex and one more: Cursor serves its OWN ACCOUNT catalog.
    # `cursor-agent --list-models` calls the Cursor backend, and the CLI's
    # refusal path names the same field ("Cannot use this model: X. Available
    # models: …"), so OpenRouter's view of openai/anthropic/google/x-ai is not
    # that list. This row is what put `gpt-5.6-luna-pro` in the picker; the host
    # dispatched it and the CLI refused it in 7 s.
    # Cursor's models now come from its own CLI, cached per entity by the run
    # path (review._maybe_cache_model_catalog -> harness_state.record_model_catalog)
    # and read in _models_for below. Being ABSENT here is load-bearing: it is
    # what stops _harness_model_options from ever inventing cursor ids again.
    "opencode": None,
    # kimi dials Moonshot's vendor API on a bare key, so its option list is the
    # NATIVE Moonshot ids (the slug minus the vendor prefix) — same derivation
    # as codex/grok, not the parameterized None. A JSON provider spec can point
    # it at another endpoint; the runtime's normalize_model accepts either the
    # prefixed slug or the native id so both forms dial correctly.
    "kimi":     ("moonshotai",),
}


# The Codex CLI's OWN model catalog — `codex debug models` on the pinned binary
# (rust-v0.160.1, read 2026-10-06; listed rows only). This is a MIRROR of
# system-apps/_harness_runtime/specs.py CODEX_MODELS / CODEX_DEFAULT_MODEL:
# the sidecar reads the live table off its binary (and serves it on /status as
# `models`), but the matrix renders without a pod, so the kernel carries the
# same snapshot. scripts/test_harness_effort_wiring.py gates the pairing —
# a CODEX_RELEASE_TAG bump moves both copies together, never one.
# `efforts` per model is the catalog's supported_reasoning_levels verbatim.
CODEX_CLI_DEFAULT_MODEL = "gpt-6.1-sol"
CODEX_CLI_MODELS: tuple = (
    {"id": "gpt-6-astra",   "label": "GPT-6-Astra",   "default_effort": "low",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-6.1-sol",   "label": "GPT-6.1-Sol",   "default_effort": "low",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-6-sol",     "label": "GPT-6-Sol",     "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-6-luna",    "label": "GPT-6-Luna",    "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "gpt-5.6-sol",   "label": "GPT-5.6-Sol",   "default_effort": "low",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6-Terra", "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-5.6-luna",  "label": "GPT-5.6-Luna",  "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "gpt-5.5",       "label": "GPT-5.5",       "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh")},
)
CODEX_CLI_MODEL_EFFORTS: dict = {m["id"]: tuple(m["efforts"]) for m in CODEX_CLI_MODELS}

# The PLATFORM's effort for a codex run that names none, per model — mirror of
# specs.py CODEX_DEFAULT_EFFORTS, gated by test_harness_effort_wiring.py. NOT
# the same thing as a row's `default_effort` above: that is the catalog's own
# default_reasoning_level and display-only, while this is what the sidecar
# actually passes as `-c model_reasoning_effort`. Carried here so the matrix
# can say "omitting effort means medium" without waking a pod: astra, and
# gpt-6.1-sol (the CLI's no-model default since rust-v0.160.1).
CODEX_CLI_DEFAULT_EFFORTS: dict = {"gpt-6-astra": "medium", "gpt-6.1-sol": "medium"}

# What a harness runs when the dispatch names no model — stated on the matrix
# row (`default_model`) so the host can say "omitting model means X" instead
# of guessing (coding_bench op=matrix showed model_override=null and nothing
# else). claude-code: the sidecar's DEFAULT_MODEL
# (system-apps/claude-code-harness/bin/main.py, env CLAUDE_DEFAULT_MODEL
# default); codex: the CLI banner with no --model. Mirrors, gated by
# scripts/test_harness_effort_wiring.py.
HARNESS_DEFAULT_MODELS: dict = {
    "claude-code": "claude-opus-5",
    "codex":       CODEX_CLI_DEFAULT_MODEL,
}

# Engines that run NOTHING when the dispatch names no model. Mirror of the
# runtime specs' contract (system-apps/_harness_runtime/specs.py: kimi and
# opencode declare `default_model=<ENV> or None` — "empty means unconfigured
# and the run errors instead of guessing a model", the no-pins doctrine).
# Every other engine's CLI/SDK picks its own model when none is
# named (claude-code and codex above; cursor/grok/antigravity per account).
# Read by review.resolve_build_target so a build on one of these with no saved
# model is REFUSED before a pod wakes. Measured on one Host: four wakes of the
# kimi pod — three "Server disconnected", then "kimi has no model configured"
# from inside the pod — before anything could run, each reported to the user
# as a failed build. Pinned against the specs source by
# scripts/test_build_harness_resolution.py.
HARNESS_MODEL_REQUIRED: frozenset = frozenset({"kimi", "opencode"})


def _claude_version(native_id: str) -> tuple[int, int] | None:
    """(major, minor) of a dashed Claude id, both namings: claude-opus-5-5 ->
    (5, 5), claude-haiku-4-5-20251001 -> (4, 5) (an 8-digit date is not a
    version), claude-3-haiku -> (3, 0). None when the id carries no version."""
    nums: list[int] = []
    for tok in str(native_id or "").split("-")[1:]:
        if tok.isdigit() and len(tok) <= 2:
            nums.append(int(tok))
            if len(nums) == 2:
                break
        elif nums:
            break
    if not nums:
        return None
    return nums[0], (nums[1] if len(nums) > 1 else 0)


def _claude_code_models() -> list[str]:
    """claude-code's model ids: the ONE list its picker (the coding matrix) and
    its save validator read.

    HOST_ENGINE_MODELS first, in its order: the floor a cold catalog still
    lists. Then every anthropic/claude-* row of the OpenRouter catalog this
    Host has cached, as the dashed Anthropic id the CLI's --model takes
    (inference_config.anthropic_api_model_id), newest first. The tuple alone
    was the whole list, so a new Claude model could be neither picked nor
    saved until someone edited it: Claude Haiku 5.5 was missing for hours
    after release and the Host built on Haiku 4.5 instead.
    Left out: variant rows (`:batch`, `:thinking`, `[1m]`,
    `-latest`) and anything older than the tuple's oldest model. The catalog
    rows carry no release date (_build_model_catalog keeps none), so "older"
    is the version in the id.

    And a catalog row is offered only when the platform's model record
    (runtime/approved_models.json, read through model_standard) lists it,
    whatever its status there. OpenRouter keeps serving Claude models the
    platform never assessed: once the live catalog's version cut
    alone added Opus 4.5, 4.6 and 4.7 and Sonnet 4.5 (Sonnet 4.5 reported
    deprecated by Anthropic), the phantom-model shape codex and cursor already
    had (HARNESS_MODEL_VENDORS). A new Claude release appears here when the
    record gains it, as Haiku 5.5 did, without a second hand-kept list."""
    from mastercode.runtime.tool_result_wall import HOST_ENGINE_MODELS
    from mastercode.inference_config import anthropic_api_model_id
    from mastercode.runtime import model_standard
    out = list(HOST_ENGINE_MODELS)
    floors = [v for v in (_claude_version(m) for m in HOST_ENGINE_MODELS) if v is not None]
    if not floors:
        return out
    floor = min(floors)
    seen = set(out)
    extra: list[tuple[tuple[int, int], str]] = []
    rows = (_CATALOG_CACHE.get("data") or {}).get("openrouter_models") or []
    for m in rows:
        slug = str((m or {}).get("id") or "")
        if not slug.startswith("anthropic/claude-"):
            continue
        if ":" in slug or "[" in slug or "-latest" in slug:
            continue
        native = anthropic_api_model_id(slug)
        if native in seen:
            continue
        v = _claude_version(native)
        if v is None or v < floor:
            continue
        if model_standard.entry_for(model_standard.canonical_model("anthropic", native)) is None:
            continue
        seen.add(native)
        extra.append((v, native))
    extra.sort(reverse=True)
    return out + [n for _, n in extra]


def _harness_model_options() -> dict:
    """{harness_id: [model ids]} for the picker + the PUT validator. Reads the
    CACHED catalog only (the /catalog handler owns the fetch) — a cold cache
    yields empty vendor lists, the picker derives from its own loaded catalog,
    and the validator fails open on an empty list rather than blocking a save
    on a catalog blip. codex's list is the CLI's own catalog (CODEX_CLI_MODELS),
    never the OpenRouter vendor filter — see the comment on the constant.
    claude-code's is _claude_code_models (the tuple plus the cached catalog's
    Claude rows)."""
    out = {"claude-code": _claude_code_models(),
           "codex": [m["id"] for m in CODEX_CLI_MODELS]}
    cat = _CATALOG_CACHE["data"] or {}
    slugs = [m.get("id") or "" for m in cat.get("openrouter_models", [])]
    for hid, vendors in HARNESS_MODEL_VENDORS.items():
        if vendors is None:
            out[hid] = [s for s in slugs if s]
            continue
        native = set()
        for s in slugs:
            vend, _, rest = s.partition("/")
            if vend in vendors and rest:
                native.add(rest)
        out[hid] = sorted(native)
    return out


# Per-harness REASONING-EFFORT options. Same doctrine as the model list: the UI
# offers only what the harness actually accepts, so a value that would be
# dropped (or rejected) at dispatch can never be saved in the first place.
# A harness ABSENT here has no effort control — that is the honest default, not
# an oversight: an effort we have not verified against the vendor's own
# accepted set would be a guess, and a guessed knob is worse than no knob.
#   claude-code — the set this picker has always offered.
#   kimi        — the binary's [thinking].effort, read off the vendor's own
#                 shipped model catalog (support_efforts), verified for 0.32.0.
#   codex       — `model_reasoning_effort`, passed as a -c config override.
#                 The set is the UNION of supported_reasoning_levels across the
#                 CLI's listed models (CODEX_CLI_MODELS, read off rust-v0.160.1
#                 2026-10-06); validate_harness_effort narrows it PER MODEL
#                 (CODEX_CLI_MODEL_EFFORTS) when the override names one, and
#                 the sidecar drops anything its live catalog does not list for
#                 the model it runs. `none` is withheld — "no reasoning" is a
#                 model choice, not an effort level. (This once was
#                 the 0.135.0 binary's flat enum incl. `minimal`, which no
#                 listed model declares, and without `max`/`ultra`, which the
#                 5.6 models do.)
# STILL ABSENT and correctly so: cursor, grok, opencode. Their argv
# builders in _harness_runtime/specs.py take an `effort` parameter and never
# read it, so offering the control would let a user set a value that is
# silently discarded. Wiring one means changing its argv builder first — the
# gate in scripts/test_harness_effort_wiring.py enforces exactly that pairing.
#   antigravity — `--effort low|medium|high`, the binary's own flag help
#                 ("Reasoning effort for the current CLI session (low|medium|
#                 high)", agy 1.1.17 `--help`). The sidecar
#                 drops anything outside that set (specs.py AGY_EFFORTS).
# Per-PROVIDER reasoning-effort options for TEXT ROLES. Text roles lacked effort
# levels, yet reasoning=low and reasoning=max for kimi behave like two different
# models; the user needs to be able to find the sweet spot for host reasoning.
#
# SAME DOCTRINE as HARNESS_EFFORTS below, for the same reason: the UI offers
# only what the provider actually accepts AND what the gateway actually sends,
# so a value that would be dropped or rejected can never be saved. A provider
# ABSENT here has no effort control, and that is the honest default rather than
# an oversight.
#
# LISTED, both verified:
#   openrouter — OpenRouter's unified `reasoning: {effort}` control, which is
#                the exact shape provider_gateway._call_remote sends
#                (provider_gateway.py:1203-1205). Enum read off OpenRouter's own
#                reasoning-tokens documentation 2026-08-22: max, xhigh, high,
#                medium, low, minimal, none. `none` is withheld for the same
#                reason codex withholds it below — "no reasoning" is a model
#                choice, not an effort level.
#   kimi       — ("low", "high", "max"), the vendor's own shipped catalog
#                (support_efforts). NOT re-derived here: it is the same set
#                HARNESS_EFFORTS["kimi"] already carries for the same vendor,
#                so the two cannot drift. Note there is no "medium" — which is
#                exactly why an invented ladder would have been wrong.
#   anthropic  — the Messages API's output_config.effort,
#                which provider_gateway._call_anthropic now sends (its `effort`
#                parameter, fed by call_model's chosen_effort). The set below is
#                the API's whole enum; which levels a MODEL takes is per model,
#                read off the Models API's capabilities.effort (_dm_row), and
#                validate_role_effort checks the pair by the same rule the
#                gateway sends by (inference_config.anthropic_wire_effort). An
#                unset route sends NOTHING and runs at the model's own default:
#                the "low" the OpenAI wire falls back to never reaches it.
#
# ABSENT and correctly so:
#   local      — _call_local takes no reasoning_effort parameter.
#   openai, xai, deepinfra, together, fireworks, groq — these DO reach
#                _call_remote, but it sends OpenRouter's `reasoning: {effort}`
#                object, which is not those vendors' own documented field shape
#                (OpenAI, for one, takes a top-level `reasoning_effort` string).
#                Offering a knob whose wire format we have not verified against
#                each vendor is the guess this doctrine exists to prevent.
#
# The pairing is enforced by scripts/test_role_effort_wiring.py, the text-role
# mirror of scripts/test_harness_effort_wiring.py.
TEXT_ROLE_EFFORTS: dict = {
    "openrouter": ("minimal", "low", "medium", "high", "xhigh", "max"),
}

# The Messages API's effort levels, low to high: the mirror of
# inference_config.ANTHROPIC_EFFORTS (brain/), held equal by
# scripts/test_anthropic_effort.py. Mirrored rather than imported because this
# module loads where brain/ is not on the path
# (scripts/test_harness_effort_wiring.py imports it with the repo root alone).
ANTHROPIC_EFFORT_LEVELS: tuple = ("low", "medium", "high", "xhigh", "max")
TEXT_ROLE_EFFORTS["anthropic"] = ANTHROPIC_EFFORT_LEVELS


def text_role_efforts(provider: str) -> tuple:
    """The efforts this provider accepts for a text role, or () for none.
    Per provider; anthropic narrows per model (effort_options)."""
    return TEXT_ROLE_EFFORTS.get((provider or "").strip().lower(), ())


def validate_role_effort(provider: str, effort: str | None,
                         model: str | None = None) -> str | None:
    """None when the pair is allowed; an error sentence otherwise.

    Fails CLOSED, unlike the harness-model validator: an unknown value is a
    real mistake rather than a cold cache. On anthropic the check is per MODEL
    and is the gateway's own rule (inference_config.anthropic_wire_effort:
    the level must be one the model's catalog row lists), so a level the wire
    would not send cannot be saved; with no model, or with a catalog that
    cannot say, it is refused rather than guessed. An async caller warms the
    Anthropic catalog first (await_effort_catalog)."""
    eff = (effort or "").strip()
    if not eff:
        return None                      # absent = the provider's own default
    allowed = text_role_efforts(provider)
    if not allowed:
        return (f"provider {provider!r} has no reasoning-effort control — the "
                "gateway does not send one for it")
    if eff not in allowed:
        return (f"effort {eff!r} is not accepted by {provider!r}. "
                f"Valid: {', '.join(allowed)}")
    if (provider or "").strip().lower() == "anthropic":
        if not (model or "").strip():
            return "an Anthropic effort is set per model: pick the model first"
        from mastercode.inference_config import anthropic_wire_effort
        sent, why = anthropic_wire_effort(model, eff)
        if not sent:
            return f"effort {eff!r} can't be used here: {why}"
    return None


# The Anthropic list read an effort check is waiting on: one at a time, so a
# turn's calls, the chat boxes and the Orchestrator share a read in flight
# instead of each starting its own (await_effort_catalog).
_EFFORT_READS: dict = {}


async def await_effort_catalog(provider: str | None, model: str | None,
                               wait_s: float = 20.0) -> None:
    """Fill the catalog rows an effort check reads, when they are cold: the
    Anthropic list carries each model's levels (the Models API's
    capabilities.effort). One fetch per cache lifetime under the user's own
    key (_direct_models, 15 s at most); a failure is cached briefly and the
    check then refuses, saying the list could not be read. Waits `wait_s` at
    most; a slower fetch finishes in the background and fills the cache for
    the next read. Every other provider's set is static and needs nothing.

    Read through _direct_models' own TTLs, so a read that failed is tried
    again once its failure has expired (30 s). The gateway fills its list here
    too (inference_config.anthropic_model_efforts_warm): warm_route_catalog
    reads a provider only when it has no cache entry at all, so after one
    failed read a person's level was never sent again for the process's life."""
    from mastercode.inference_config import anthropic_model_efforts, normalize_provider
    p, m = normalize_provider(provider or "", model or "")
    if p != "anthropic" or not m or anthropic_model_efforts(m) is not None:
        return
    task = _EFFORT_READS.get(p)
    if task is None or task.done():
        task = asyncio.ensure_future(_direct_models(p))
        _EFFORT_READS[p] = task
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
    except Exception as e:  # noqa: BLE001 — the check refuses on its own
        logger.info("anthropic catalog for the effort check unavailable: %s", e)


def effort_options(provider: str | None, model: str | None) -> dict:
    """What a chat box or the Orchestrator may offer as the reasoning effort
    for `model` on `provider`, from the rule the gateway sends by:

      {provider, model, efforts: [levels the pick may be],
       default: the level that runs when none is picked, when known,
       known: False when the catalog could not say (efforts is then [])}

    anthropic: the model's own levels (the Models API), and its documented
    default (inference_config.anthropic_default_effort) so a blank pick reads
    "Default · medium" rather than nothing. Every other provider: its static
    set (TEXT_ROLE_EFFORTS) and no stated default. Read-only; await
    await_effort_catalog first to fill a cold Anthropic list."""
    from mastercode.inference_config import (
        anthropic_api_model_id, anthropic_default_effort, anthropic_model_efforts,
        normalize_provider)
    p, m = normalize_provider(provider or "", model or "")
    if p == "anthropic" and m:
        m = anthropic_api_model_id(m)   # the id the wire sends (_call_anthropic)
    out: dict = {"provider": p, "model": m, "efforts": [], "default": None, "known": True}
    if p == "anthropic":
        accepted = anthropic_model_efforts(m) if m else None
        out["efforts"] = list(accepted or ())
        out["known"] = accepted is not None
        # No default named for a model that takes no effort at all.
        out["default"] = anthropic_default_effort(m) if accepted != () else None
        return out
    out["efforts"] = list(text_role_efforts(p))
    return out


async def check_thread_effort(effort: str | None, provider: str | None = None,
                              model: str | None = None) -> str | None:
    """None when a chat box's per-thread effort pick may ride this turn; an
    error sentence otherwise. The pick is checked against the model the turn
    runs on: the box's model pick when it names one, else the chat route.
    The same rule as the Orchestrator's write (validate_role_effort), so a
    level the gateway would not send is refused before the turn starts."""
    eff = (effort or "").strip()
    if not eff:
        return None
    p, m = (provider or "").strip(), (model or "").strip()
    if not (p and m):
        pool = brain_service.get_pool()
        if not pool:
            return "the Host is not ready yet"
        from mastercode.inference_config import resolve_role_route
        route = await resolve_role_route(pool, brain_service._entity_id, "chat")
        p, m = route.get("provider") or "", route.get("model") or ""
    from mastercode.inference_config import normalize_provider
    p, m = normalize_provider(p, m)
    if not (p and m):
        return "no model is set up for this conversation, so there is no effort to set"
    await await_effort_catalog(p, m)
    return validate_role_effort(p, eff, m)


HARNESS_EFFORTS: dict = {
    # The SDK's own EffortLevel literal, which the sidecar sends as the
    # `effort` option (claude-code-harness/bin/main.py _SDK_EFFORTS). xhigh
    # is included: the sidecar always took it.
    "claude-code": ("low", "medium", "high", "xhigh", "max"),
    "codex":       tuple(e for e in ("low", "medium", "high", "xhigh", "max", "ultra")
                         if any(e in m["efforts"] for m in CODEX_CLI_MODELS)),
    "kimi":        ("low", "high", "max"),
    "antigravity": ("low", "medium", "high"),
    # grok 0.2.112's `--reasoning-effort` canonical tiers, read off the pinned
    # binary's own embedded docs (specs.py GROK_EFFORTS holds the measurement).
    # `none`/`minimal` are omitted on the codex rule — the picker offers tiers a
    # user would choose, not the vendor's opt-out sentinels; the sidecar still
    # accepts them if something else ever sends one.
    "grok":        ("low", "medium", "high", "xhigh", "max"),
}

# kimi's text-role set IS its harness set — same vendor, same shipped catalog.
# Assigned here rather than duplicated above so the two can never drift.
TEXT_ROLE_EFFORTS["kimi"] = HARNESS_EFFORTS["kimi"]


def validate_harness_effort(harness_id: str, effort: str,
                            model: str | None = None) -> str | None:
    """None if `effort` is one this harness accepts, else an error string. A
    harness with no declared set takes no effort at all — saying so beats
    silently storing a value its dispatch will discard. When the harness's
    catalog states efforts PER MODEL (codex) and `model` names one, the check
    is that model's own list — `ultra` is real on gpt-5.6-sol and not on
    gpt-5.5, and the run would drop it there."""
    opts = HARNESS_EFFORTS.get(harness_id) or ()
    if not opts:
        return (f"{harness_id} has no reasoning-effort control — leave it "
                f"unset")
    if effort not in opts:
        return (f"{harness_id} can't run effort {effort!r} — it accepts "
                f"{', '.join(opts)}")
    if harness_id == "codex" and model:
        per_model = CODEX_CLI_MODEL_EFFORTS.get(model)
        if per_model and effort not in per_model:
            return (f"codex can't run effort {effort!r} on {model} — that model "
                    f"accepts {', '.join(per_model)}")
    return None


def validate_harness_model(harness_id: str, model: str,
                           options: dict | None = None) -> str | None:
    """None if `model` is one the harness's provider actually offers, else an
    error string. Fails open when the option list is empty (cold catalog /
    provider catalog error) — the UI only offers listed models, so an empty
    list means we can't check, not that the pick is wrong.

    `options` = {harness_id: [model ids]} — pass `await harness_model_options()`
    (the coding-matrix build) so the check runs against the SAME list the
    picker rendered. Without it this falls back to the OpenRouter-vendor
    derivation, which is only right for vendor-account harnesses: for a
    key-driven harness (kimi, opencode) the picker lists what the
    user's KEY serves and OpenRouter's view of the vendor never matched it —
    'kimi can't run 'k3'' refused the picker's own first option (measured:
    key served k3/k3-256k/kimi-for-coding*, catalog derivation
    gave kimi-k3/kimi-k2.6/…)."""
    src = options if options is not None else _harness_model_options()
    opts = src.get(harness_id) or []
    if opts and harness_id == "claude-code":
        # One model, several spellings: the picker lists the dated
        # claude-haiku-4-5-20251001, and Anthropic's alias claude-haiku-4-5
        # (or OpenRouter's claude-haiku-4.5) names the same snapshot. Compared
        # on the Anthropic-native id the wire sends (anthropic_api_model_id).
        from mastercode.inference_config import anthropic_api_model_id
        if anthropic_api_model_id(model) in {anthropic_api_model_id(o) for o in opts}:
            return None
    if opts and model not in opts:
        return (f"{harness_id} can't run {model!r} — pick one of its own "
                f"provider's models (the picker lists them)")
    return None


async def harness_model_options() -> dict:
    """{harness_id: [model ids]} from the coding-matrix build — the models the
    key that will actually fire each harness serves (`_build_coding_matrix`,
    the one resolution the screen, the harness_connections tool and every
    validator share). A harness whose catalog errored maps to [] so the
    validator fails open for it, exactly like a cold catalog. Any build
    failure → {} (fail open everywhere) — a save must never be blocked by a
    catalog blip."""
    try:
        m = await _build_coding_matrix()
    except Exception as e:
        logger.info("harness_model_options: matrix build failed (%s) — "
                    "model validation fails open", e)
        return {}
    out: dict = {}
    for r in m.get("harnesses", []):
        hid = r.get("id")
        if not hid:
            continue
        if r.get("models_error"):
            out[hid] = []
            continue
        out[hid] = [x.get("id") for x in (r.get("models") or []) if x.get("id")]
    return out


async def _installed_harness_options() -> list[dict]:
    """Installed subset of the known coding harnesses, with display label +
    logo url (served from each harness app's own icon). Empty if the app
    registry is unavailable."""
    from server.routes.harnesses import connected_harness_ids
    # Available = credentials connected — not dock-app-installed.
    # Icons live in the dashboard base assets (moved out of the Connections app
    # — the connections app is deleted) for ALL harnesses.
    #
    # RESOLVED. claude-code once pointed at its own app icon — the
    # CEIGAS shield, the wrong mark next to "Claude Code" —
    # and the fix at the time was to delete that one file so the <img onerror>
    # rendered the label alone. What that missed is that the SAME shield was
    # still sitting under codex, copilot, cursor, grok and opencode: those five
    # .png files were byte-identical copies of it (one md5, measured), so five
    # harnesses wore another company's mark while the three "missing" ones were
    # the only honest rows on the screen.
    # All eight now carry their own vendor mark, each fetched from that vendor
    # and verified distinct (8 of 8 hashes). The onerror fallback stays — it is
    # what a NEW harness id should hit until someone adds its mark, and that
    # blank is correct where a wrong logo is not.
    def _icon(h):
        return f"/assets/harness/{h}.png"
    model_opts = _harness_model_options()
    return [
        {"id": h, "label": HARNESS_LABELS.get(h, h), "icon_url": _icon(h),
         # The pickable model set for this harness — the UI renders a select
         # from this (no typed model names); empty = cold catalog, the picker
         # derives the same list from its own loaded catalog.
         "models": model_opts.get(h, []),
         # The reasoning-effort set this harness accepts; empty = no effort
         # control for it. The picker renders from THIS rather than testing the
         # harness id, so a harness that gains effort support gets the knob by
         # adding one row to HARNESS_EFFORTS.
         "efforts": list(HARNESS_EFFORTS.get(h, ()))}
        for h in connected_harness_ids(HARNESS_APP_IDS)
    ]


# ── Provider key status (the picker's provider cells) ───────────────────────
# Every Keys-registry provider (server/vault/providers PROVIDERS) annotated
# with whether THIS entity holds a credential for it — an api_key OR an OAuth
# token (resolve's typed ladder: canonical vault name → fuzzy alias → env) —
# plus 'local' (the user's own machines; no key, always available). The
# routing read runs on every picker load, so the annotation is cached lightly;
# it is best-effort — a vault hiccup must never fail the routing read.
_PROVIDER_STATUS_CACHE: dict = {"data": None, "exp": 0.0}
_PROVIDER_STATUS_TTL = 30.0


def invalidate_provider_caches(provider: str) -> None:
    """Drop one provider's entries from every inference cache. Called from the
    vault lifecycle routes (store / revoke / deactivate) so a deliberate key
    action shows on every Orchestrator surface NOW instead of whenever the
    30s/120s/300s TTLs happen to lapse — a user who just deactivated a key and
    still sees it offered reads the UI as lying. Best-effort, never raises."""
    p = (provider or "").strip().lower()
    if not p:
        return
    try:
        _PROVIDER_STATUS_CACHE["exp"] = 0.0
        _DIRECT_MODELS_CACHE.pop(p, None)
        _BALANCES_CACHE.pop(p, None)
    except Exception:  # pragma: no cover - defensive
        pass


async def _provider_status(pool, eid) -> list[dict]:
    now = time.monotonic()
    cached = _PROVIDER_STATUS_CACHE["data"]
    if cached is not None and _PROVIDER_STATUS_CACHE["exp"] > now:
        return cached
    out: list[dict] = []
    try:
        from server.vault.providers import (
            PROVIDERS, resolve, resolve_signin, signin_serves_inference,
            credential_label, last_served)
        for p in PROVIDERS:
            via = None
            try:
                if signin_serves_inference(p):
                    # xai: the host-run sign-in whenever one exists — live, or
                    # expired / unreadable (it still counts as held: the
                    # renewal loop restores an expired one, and the key is
                    # not used behind it either way) — and the API key only
                    # when there is no sign-in, read only then
                    # (server/vault/providers._SIGNIN_INFERENCE). `via` names
                    # what the next call dials. Only an api_key counts as the
                    # key: it is all the call path dials (resolve_key), so an
                    # oauth-typed '<provider>-oauth' row is never labelled
                    # 'xAI API key'.
                    s = await resolve_signin(pool, eid, p, actor="orchestrator-routing")
                    if s is not None:
                        via, has = "signin", True
                    else:
                        cred = await resolve(pool, eid, p, ("api_key",),
                                             actor="orchestrator-routing")
                        has = bool(cred and cred.secret)
                        via = "api_key" if has else None
                else:
                    # api_key OR oauth_token both count as 'connected'
                    # (anthropic ships both shapes). resolve never raises by
                    # contract — belt and braces.
                    cred = await resolve(
                        pool, eid, p, ("api_key", "oauth_token"),
                        actor="orchestrator-routing")
                    has = bool(cred and cred.secret)
            except Exception:
                has = False
            row = {"id": p, "has_key": has}
            if via:
                row["via"] = via
                row["via_label"] = credential_label(p, via)
            if signin_serves_inference(p):
                # What ACTUALLY served the last model call ("xAI sign-in" /
                # "xAI API key"), process-local (providers._LAST_SERVED).
                ls = last_served(p)
                if ls:
                    row["last_served"] = ls
            if PROVIDERS[p].get("embeddings"):
                # UI hint: this provider's base also serves /embeddings, so
                # the embedding picker offers it (probe-gated at switch).
                row["embeddings"] = True
            out.append(row)
    except Exception as e:
        logger.debug("provider key-status annotation unavailable: %s", e)
        return out
    out.append({"id": "local", "has_key": True, "embeddings": True})
    _PROVIDER_STATUS_CACHE["data"] = out
    _PROVIDER_STATUS_CACHE["exp"] = now + _PROVIDER_STATUS_TTL
    return out


def _normalize_stored_route(entry) -> dict | None:
    """One stored model_routing entry → the new {provider, model, ...} shape,
    or None when it normalizes empty (a stored SDK-lane route reads as ABSENT
    — the hard cut). Mirrors the stored-entry normalization in
    resolve_role_route: legacy {lane, model, direct_provider?} migrates on
    read (an explicit direct_provider beats the lane id), {provider,
    model_id} reads as-is. READ-path only — storage is never rewritten here.
    """
    if not isinstance(entry, dict):
        return None
    from mastercode.inference_config import normalize_provider, _normalize_fallback
    raw_provider = (entry.get("provider") or entry.get("lane") or "")
    raw_model = (entry.get("model") or entry.get("model_id") or "")
    if not (entry.get("provider") or "").strip() and entry.get("lane"):
        # legacy lane shape: an explicit direct_provider beats the lane id
        raw_provider = (entry.get("direct_provider") or "").strip() or raw_provider
    provider, model = normalize_provider(raw_provider, raw_model)
    if not provider or not model:
        return None
    out: dict = {"provider": provider, "model": model}
    fb_p, fb_m = _normalize_fallback(
        entry.get("fallback_provider"), entry.get("fallback_model"))
    if fb_p:
        out["fallback_provider"] = fb_p
        if fb_m:
            out["fallback_model"] = fb_m
    eff = (entry.get("effort") or "").strip()
    if eff:
        out["effort"] = eff
    base_url = (entry.get("base_url") or "").strip()
    if base_url:
        out["base_url"] = base_url
    if entry.get("connection_id"):
        out["connection_id"] = entry["connection_id"]
    return out


@router.get("/routing", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_current_routing():
    """Current per-role routes + what each role EFFECTIVELY resolves to +
    provider key status + provider-scoped favorites + the coding-harness
    state (coding_harnesses + installed harness_options).

    `routing` is the stored model_routing normalized on READ to the
    {provider, model, ...} shape (legacy lane entries migrate on read; SDK
    entries read as absent). `effective` is resolve_role_route per role —
    the single read path every dispatch site uses, so this matches what
    really fires; an unrouted role carries its `unconfigured` reason (there
    is NO default model).
    """
    from mastercode.inference_config import resolve_role_route

    pool = brain_service.get_pool()
    base = {
        "routing": {},
        "roles": ROLES,
        "role_descriptions": ROLE_DESCRIPTIONS,
        "harness_options": [],
        "coding_harnesses": [],
        "effective": {},
        # Every Keys-registry provider + 'local', annotated with key-held
        # status. Empty when the pool is down — the picker degrades.
        "providers": [],
        "favorites_by_provider": {},
        # {provider: [efforts]} for the Routing tab's effort control. Only
        # providers the gateway actually sends an effort for appear — an absent
        # provider renders no control rather than a knob that does nothing
        # (see TEXT_ROLE_EFFORTS).
        "role_efforts": {k: list(v) for k, v in TEXT_ROLE_EFFORTS.items()},
    }
    if not pool:
        return base

    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )

    # behavioral_params is NULL for entities that never set one (e.g. fresh /
    # test entities) — guard so the picker's routing read doesn't 500 (it did:
    # 'NoneType' object has no attribute 'get'). Mirrors resolve_role_model's bp guard.
    params = row["behavioral_params"] if row else None
    if isinstance(params, str):
        params = json.loads(params)
    if not isinstance(params, dict):
        params = {}

    stored = params.get("model_routing") or {}
    routing: dict = {}
    if isinstance(stored, dict):
        for r, entry in stored.items():
            # A role that no longer exists is dropped here rather than
            # migrated (no migration code for a rename — the stored
            # key is simply ignored). The filter matters because the picker
            # round-trips what it is handed into every save, so a `plan` or
            # `agents` entry left over from the nine-role era would be
            # rewritten forever. Same reason known_coding_harness_entries
            # filters a retired harness id below.
            if r not in ROLES:
                continue
            n = _normalize_stored_route(entry)
            if n is not None:
                routing[r] = n
    base["routing"] = routing
    # Through the reader-side filter: a retired harness id lingering in the
    # stored list must never reach the picker (it would round-trip it into
    # every save — see set_coding_harnesses).
    base["coding_harnesses"] = known_coding_harness_entries(params.get("coding_harnesses"))
    # The Default tick (Orchestrator → Coding Engines): an unnamed build's
    # engine. Null = no tick.
    _d = str(params.get("default_coding_harness") or "").strip()
    base["default_coding_harness"] = _d if _d in HARNESS_APP_IDS else None
    base["harness_options"] = await _installed_harness_options()
    base["favorites_by_provider"] = _favorites_by_provider(
        _favorites_from_params(params))

    # Effective route per role — the picker shows what actually fires
    # (nothing hidden). The route dict carries the transitional
    # model_id/lane aliases from the resolver; new readers use
    # provider/model only.
    effective: dict = {}
    for role in ROLES:
        try:
            route = await resolve_role_route(pool, eid, role)
        except Exception:
            route = {}
        effective[role] = {
            **route,
            "source": ("explicit" if role in routing else
                       "derived" if route.get("model") else "unconfigured"),
        }
    base["effective"] = effective
    # The effort control of each text row, for the route the row shows (the
    # stored one, else the effective one): on anthropic the levels are the
    # MODEL's own and the blank pick names its default ("Default · medium").
    # role_efforts above stays per provider for every other row.
    role_effort_options: dict = {}
    for role in ROLES:
        if role in ("embedding", "image", "image_gen"):
            continue
        shown = routing.get(role) or effective.get(role) or {}
        if shown.get("provider") and shown.get("model"):
            await await_effort_catalog(shown["provider"], shown["model"], wait_s=5.0)
            role_effort_options[role] = effort_options(shown["provider"], shown["model"])
    base["role_effort_options"] = role_effort_options
    base["providers"] = await _provider_status(pool, eid)
    return base


@router.get("/effort-options", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_effort_options(provider: str | None = None, model: str | None = None):
    """The reasoning efforts a chat box may offer for a turn, and the level
    that runs when none is picked (effort_options). provider + model name the
    picked model; without them, the chat route's model runs (the chat box's
    "Default model"). The per-thread pick is validated on send by the same
    rule (validate_role_effort)."""
    p, m = (provider or "").strip(), (model or "").strip()
    if not (p and m):
        pool = brain_service.get_pool()
        if not pool:
            raise HTTPException(503, "Brain not initialized")
        from mastercode.inference_config import resolve_role_route
        route = await resolve_role_route(pool, brain_service._entity_id, "chat")
        p, m = route.get("provider") or "", route.get("model") or ""
    await await_effort_catalog(p, m)
    return effort_options(p, m)


@router.get("/local-models", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_local_models():
    """The host's LOCAL MODEL TABLE — models served by the host's own online
    machines over its private mesh (network_bastion_heartbeat.models, read via
    mastercode.model_backends). Empty until bastions advertise their served
    models; an openrouter routing pick whose model appears here flips to
    provider 'local' (see set_role_model)."""
    pool = brain_service.get_pool()
    if not pool:
        return {"local_models": []}
    from mastercode.model_backends import list_local_models
    return {"local_models": await list_local_models(pool, brain_service._entity_id)}


async def _owner_endpoints_view(pool) -> dict:
    from mastercode import owner_endpoints as oe
    urls = await oe.configured(pool, brain_service._entity_id)
    answers = await asyncio.gather(*(oe.probe(u) for u in urls))
    return {
        "endpoints": [
            {"url": u, "label": oe.label(u), "reachable": bool(a),
             "kind": (a or {}).get("kind"), "models": (a or {}).get("models", [])}
            for u, a in zip(urls, answers)
        ],
        "max": oe.MAX_ENDPOINTS,
    }


@router.get("/local-endpoints", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_local_endpoints():
    """The model servers the owner named by address (mastercode/owner_endpoints),
    each asked live whether it answers and what it serves."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")
    return await _owner_endpoints_view(pool)


class LocalEndpointsBody(BaseModel):
    endpoints: list[str]


@router.put("/local-endpoints", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def put_local_endpoints(body: LocalEndpointsBody):
    """Replace the owner's list of model servers. Their models join the local
    model table as soon as they answer."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")
    from mastercode import owner_endpoints as oe
    try:
        await oe.save(pool, brain_service._entity_id, body.endpoints)
    except oe.EndpointError as e:
        raise HTTPException(400, str(e))
    return await _owner_endpoints_view(pool)


# Provider model catalogs — each provider's OWN /models endpoint, dialed with
# the user's canonical Keys credential (OpenAI-wire /models for most; the
# Anthropic Messages API's /v1/models for 'anthropic'). This is
# what makes each provider cell a PICKER instead of a typed guess: the
# option list is what the provider actually serves, not what
# the user hopes it serves.
_DIRECT_MODELS_CACHE: dict = {}
_DIRECT_MODELS_TTL = 300.0
_DIRECT_MODELS_TTL_FAIL = 30.0   # failed fetches cache briefly — retry soon


# OpenRouter slug prefix per FIRST-PARTY vendor. Used to fill pricing/context
# a vendor's own /models omits (OpenAI publishes neither; Anthropic no price)
# from the already-cached OpenRouter public catalog, where the listed price for
# a first-party model IS that vendor's list price. Never applied to generic
# hosts (Groq/Together/Fireworks/DeepInfra set their OWN prices for the same
# OSS slugs — an OR number there would be another host's price).
_OR_VENDOR_PREFIX = {"openai": "openai/", "anthropic": "anthropic/",
                     "xai": "x-ai/", "kimi": "moonshotai/",
                     # google/ read off openrouter.ai/api/v1/models itself
                     # (2026-08-31): every Gemini id is google/gemini-*.
                     "google": "google/"}


def _enrich_first_party_from_openrouter(p: str, models: list) -> None:
    """Fill ABSENT price/context fields on a first-party vendor's rows from
    the warm OpenRouter catalog cache (never fetches — cold cache = no
    enrichment, fields stay honestly absent). Match by slug suffix:
    openai/gpt-5.2 → gpt-5.2; a dated native id (claude-x-20260115) takes the
    LONGEST OR suffix it extends on a '-' boundary. Enriched rows carry
    price_source so the UI can say where the number came from."""
    prefix = _OR_VENDOR_PREFIX.get(p)
    if not prefix or not models:
        return
    cat = (_CATALOG_CACHE.get("data") or {}).get("openrouter_models") or []
    # Anthropic's own ids spell the version with dashes (claude-opus-5-5,
    # claude-haiku-4-5-20251001); OpenRouter spells it with dots
    # (anthropic/claude-opus-5.5, anthropic/claude-haiku-4.5). Matched raw, a
    # dotted row never matched exactly and the '-' boundary rule fell through
    # to the OLDER model's row. Measured against the live OpenRouter catalog
    # 2026-09-27: claude-opus-5-5 priced as claude-opus-5 ($5/$25 for $4/$20),
    # claude-fable-5-1 took claude-fable-5's cache-read rate (1e-6 for
    # 2.5e-7), and claude-opus-4-8 / claude-haiku-4-5 went unpriced. Anthropic
    # only: OpenAI's own ids keep the dot (gpt-5.2).
    dots_to_dashes = p == "anthropic"
    by_suffix: dict = {}
    for m in cat:
        mid = str(m.get("id") or "")
        if not mid.startswith(prefix) or ":" in mid:   # skip :free variants
            continue
        key = mid[len(prefix):].lower()
        if dots_to_dashes:
            key = key.replace(".", "-")
        by_suffix[key] = m
    if not by_suffix:
        return
    suffixes = sorted(by_suffix, key=len, reverse=True)
    for row in models:
        rid = str(row.get("id") or "").lower()
        src = by_suffix.get(rid)
        if src is None:
            src = next((by_suffix[s] for s in suffixes
                        if rid.startswith(s + "-")), None)
        if src is None:
            continue
        def _f(v):
            try:
                return float(v)   # OR caches prices as strings ("0.0000025")
            except (TypeError, ValueError):
                return None
        if "input_per_token" not in row and not row.get("subscription"):
            pin = _f(src.get("input_per_token"))
            pout = _f(src.get("output_per_token"))
            if pin is not None or pout is not None:
                row["input_per_token"] = pin or 0.0
                row["output_per_token"] = pout or 0.0
                row["price_source"] = "vendor list via OpenRouter"
                # The CACHE rates ride the same enrichment, and this is the only
                # path that can supply them for a first-party Anthropic route:
                # reprice_unpriced's own measurement (:320) is that
                # claude-fable-5-1 gets its price from its OpenRouter row, and
                # OpenRouter publishes input_cache_read + input_cache_write for
                # every anthropic/* model (verified live 2026-09-15). Carried
                # only when the input price was — a cache rate without the input
                # rate it falls back to is not usable.
                for _k in ("cache_read_per_token", "cache_write_per_token"):
                    _v = _f(src.get(_k))
                    if _v is not None:
                        row[_k] = _v
                # The prompt-length tiers belong to the same rate set
                # (_or_price_tiers). Without them the first-party row prices
                # flat, and _price_for finds this bare id before the prefixed
                # OpenRouter row that has them.
                if isinstance(src.get("price_tiers"), list) and src["price_tiers"]:
                    row["price_tiers"] = [dict(t) for t in src["price_tiers"]
                                          if isinstance(t, dict)]
        if not row.get("context_length") and src.get("context_length"):
            row["context_length"] = int(src["context_length"])
        # The output limit rides the same match: a
        # first-party row whose vendor publishes none takes OpenRouter's.
        if not row.get("max_output_tokens") and src.get("max_output_tokens"):
            row["max_output_tokens"] = int(src["max_output_tokens"])
        # Modalities. First-party vendors don't publish them on
        # their own /models, so the OpenRouter row is the only source we have
        # for "can this model see / draw". Carried with provenance: the
        # picker labels an inherited capability differently from one the
        # provider itself stated, because this is a SUFFIX match, not an
        # identity — and offering a model that can't see is the bug the
        # image roles already had.
        for k in ("input_modalities", "output_modalities"):
            if not row.get(k) and src.get(k):
                row[k] = list(src[k])
                row["modality_source"] = "vendor list via OpenRouter"



def _dm_num(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None

def _dm_row(m: dict) -> dict | None:
    """One provider-specific model object → the shared row shape. Fields
    are picked up under every name the providers use; absent stays absent.
    DeepInfra nests everything one level down under `metadata` (verified
    live 2026-08-06) — flattened here first."""
    meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
    mid = str(m.get("id") or m.get("model_name") or "").strip()
    if not mid:
        return None
    row: dict = {"id": mid}
    name = m.get("name") or m.get("display_name")
    if name and name != mid:
        row["name"] = str(name)
    # `max_input_tokens` FIRST: Anthropic's /v1/models is the one catalog
    # here that publishes the context window and the OUTPUT cap under two
    # different names, and it names the window `max_input_tokens` (measured
    # 2026-09-06: claude-fable-5-1 reads max_input_tokens 1000000,
    # max_tokens 128000). Reading `max_tokens` for a Claude row therefore
    # recorded the output cap as the context window — an 8x undercount that
    # the turn budget then spends against. `max_tokens` stays in the chain
    # LAST for the providers that do use it to mean the window; it is only
    # ever consulted now when nothing more specific was published.
    ctx = _dm_num(m.get("context_length") or m.get("context_window")
               or m.get("max_input_tokens") or meta.get("context_length")
               or meta.get("max_input_tokens")
               or m.get("max_tokens") or meta.get("max_tokens"))
    if ctx:
        row["context_length"] = int(ctx)
    # THE OUTPUT LIMIT, under the names the
    # providers publish it: an explicit max_output_tokens /
    # max_completion_tokens, OpenRouter's top_provider.max_completion_tokens,
    # and Anthropic's `max_tokens` — but `max_tokens` ONLY where the window
    # was published separately as max_input_tokens (Anthropic's /v1/models,
    # measured: claude-fable-5-1 max_input_tokens 1000000,
    # max_tokens 128000). Elsewhere `max_tokens` means the window (the chain
    # above), so it is not read as an output limit there.
    _tp = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
    out_lim = _dm_num(m.get("max_output_tokens") or m.get("max_completion_tokens")
                      or meta.get("max_output_tokens")
                      or meta.get("max_completion_tokens")
                      or _tp.get("max_completion_tokens"))
    if not out_lim and (m.get("max_input_tokens") or meta.get("max_input_tokens")):
        out_lim = _dm_num(m.get("max_tokens") or meta.get("max_tokens"))
    if out_lim and out_lim > 0:
        row["max_output_tokens"] = int(out_lim)
    created = _dm_num(m.get("created"))
    if created:
        row["created"] = int(created)
    # Model type where the provider declares one (Together: chat /
    # embedding / image / …) — the embedding picker filters on it.
    mtype = m.get("type")
    if isinstance(mtype, str) and mtype:
        row["type"] = mtype
    # Modalities — what the model can READ and EMIT, the source
    # of truth for the Orchestrator's "which models can do images" question.
    # OpenRouter's own /models nests them under `architecture` (verified live
    # 2026-08-31); a provider that publishes neither leaves them ABSENT, and
    # absent means unknown, never "text only" — see get_capable_models, which
    # reports "couldn't look" separately from "none".
    arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
    for k in ("input_modalities", "output_modalities"):
        v = arch.get(k)
        if isinstance(v, list) and v:
            row[k] = [str(x) for x in v]
    # Together declares an image MODEL TYPE instead of modalities — same fact
    # in the other vocabulary, so it answers the same question.
    if row.get("type") == "image" and "output_modalities" not in row:
        row["output_modalities"] = ["image"]
    # Which reasoning-effort levels the model takes, where the provider says.
    # Anthropic's /v1/models carries capabilities.effort: {supported, low,
    # medium, high, max, xhigh (or null)}, each level {supported: bool}
    # (platform.claude.com/docs/en/api/models/list). The
    # documented levels only, low to high (ANTHROPIC_EFFORT_LEVELS): the
    # gateway sends nothing it does not know. [] = the model takes no effort;
    # ABSENT = the provider said nothing, read as unknown
    # (cached_model_efforts), never as "none".
    _caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    _eff = _caps.get("effort") if isinstance(_caps.get("effort"), dict) else None
    if _eff is not None and isinstance(_eff.get("supported"), bool):
        row["efforts"] = ([lv for lv in ANTHROPIC_EFFORT_LEVELS
                           if isinstance(_eff.get(lv), dict) and _eff[lv].get("supported") is True]
                          if _eff["supported"] else [])
    # Pricing, where published, in four shapes: OpenRouter
    # {pricing:{prompt,completion}} (USD/token strings); Together
    # {pricing:{input,output}} (USD per MILLION — verified live
    # 2026-08-10: moonshotai/Kimi-K3 reads 3/15, the vendor's $3/$15 per M
    # list); DeepInfra keyed {metadata:{pricing:{input_tokens,
    # output_tokens}}} (USD per MILLION); DeepInfra public
    # {pricing:{cents_per_*_token}} (cents per token).
    #
    # CACHE RATES, where the provider publishes them. Same two
    # rules as the prices above: the provider's own units, and ABSENT stays
    # absent — a cache rate this file invents would be read as fact by the
    # estimator, which is the exact failure the hourly branch below refuses.
    # OpenRouter shape: pricing.input_cache_read / input_cache_write, USD per
    # token (verified live 2026-09-15 — see _or_cache_rates). Together shape:
    # pricing.cached_input, USD per MILLION like its input/output — the shape
    # this repo pins in scripts/test_direct_models_pricing.py:52 (Kimi-K3, 0.3
    # against an input of 3); NOT re-verified against a live Together key here,
    # because there is no Together credential on this box.
    pricing = m.get("pricing") or {}
    pin = _dm_num(pricing.get("prompt"))
    pout = _dm_num(pricing.get("completion"))
    t_in = _dm_num(pricing.get("input"))
    t_out = _dm_num(pricing.get("output"))
    t_cache = _dm_num(pricing.get("cached_input"))
    if pin is not None or pout is not None:
        row["input_per_token"] = pin or 0.0
        row["output_per_token"] = pout or 0.0
        row.update(_or_cache_rates(pricing))
        row.update(_or_price_tiers(pricing))
    elif t_in is not None or t_out is not None:
        # An hourly-priced row with 0/0 per-token stays unpriced — a per-token
        # 'free' there would be a fabrication.
        # NOT a test for dedicated-endpoint-only: measured, the
        # dedicated-only mistralai/Mistral-7B-Instruct-v0.1 and
        # Mixtral-8x7B-Instruct-v0.1 both carry hourly=0 AND real per-token
        # prices, so this branch prices them like any other row. Whether a
        # model is actually servable is decided by _together_mark_serving from
        # /v1/endpoints; nothing in the pricing block can tell you.
        hourly = _dm_num(pricing.get("hourly")) or 0.0
        if (t_in or t_out) or not hourly:
            row["input_per_token"] = (t_in or 0.0) / 1_000_000.0
            row["output_per_token"] = (t_out or 0.0) / 1_000_000.0
            # Only alongside a priced row: a cached_input on a row that stayed
            # unpriced would be a cache rate with no input rate to fall back to.
            #
            # AND ONLY IF IT IS CHEAPER THAN INPUT. The key name and the
            # per-MILLION unit are pinned by this repo's fixture and have NOT
            # been re-verified against a live Together key (there is no Together
            # credential on this box), so the one thing that could be silently
            # wrong is the UNIT — read a per-token rate as per-million and the
            # cache would price a million times too high, which is worse than
            # not pricing it. A cache read costing MORE than fresh input is not
            # a thing any vendor sells, so that reading is treated as unparsed:
            # the key stays absent and the estimator falls back to the input
            # rate, which is the honest "we don't know" and errs high. Logged at
            # warning because it means the shape changed, not that the model is
            # odd. Equal is allowed — a provider may genuinely not discount.
            if t_cache is not None:
                _cr = t_cache / 1_000_000.0
                if _cr > row["input_per_token"]:
                    logger.warning(
                        "%s: pricing.cached_input reads %g/token against an "
                        "input rate of %g — a cache read above input is not a "
                        "real rate, so it is being ignored (units changed?)",
                        mid, _cr, row["input_per_token"])
                else:
                    row["cache_read_per_token"] = _cr
    else:
        m_in = _dm_num((meta.get("pricing") or {}).get("input_tokens"))
        m_out = _dm_num((meta.get("pricing") or {}).get("output_tokens"))
        if m_in is not None or m_out is not None:
            row["input_per_token"] = (m_in or 0.0) / 1_000_000.0
            row["output_per_token"] = (m_out or 0.0) / 1_000_000.0
        else:
            cin = _dm_num(pricing.get("cents_per_input_token"))
            cout = _dm_num(pricing.get("cents_per_output_token"))
            if cin is not None or cout is not None:
                row["input_per_token"] = (cin or 0.0) / 100.0
                row["output_per_token"] = (cout or 0.0) / 100.0
    return row


def _together_mark_serving(rows: dict, endpoints: list) -> None:
    """Mark Together rows in place from its /v1/endpoints list.

    Callable = a serverless endpoint (Together runs it on demand), or a
    dedicated endpoint the account owns that is currently STARTED. Everything
    else gets needs_endpoint=True — advisory, never a filter (endpoint ids and
    model ids are not the same namespace; see the call site).

    A STOPPED dedicated endpoint is NOT callable: it has to be started first,
    which is exactly what the 400 tells the user to go do.

    NO LIST MEANS NO VERDICT. An empty/unparseable `endpoints` marks NOTHING,
    because 'every model in the catalog is broken' is a far worse thing to say
    wrongly than saying nothing — and the two are indistinguishable from here
    if Together ever renames the wrapper key this is unpacked from. A list that
    DOES parse but contains nothing servable is real evidence and still marks:
    the distinction is 'we learned nothing' vs 'we learned nothing is served'.
    """
    if not endpoints:
        return
    served: dict = {}
    for e in endpoints or []:
        if not isinstance(e, dict):
            continue
        mid = str(e.get("model") or "")
        kind = e.get("type")
        if not mid:
            continue
        if kind == "serverless" or (kind == "dedicated"
                                    and e.get("state") == "STARTED"):
            # serverless wins a tie: it needs no action from the user.
            if served.get(mid) != "serverless":
                served[mid] = str(kind)
    for r in rows.values():
        kind = served.get(r["id"])
        if kind:
            r["serving"] = kind
        else:
            r["needs_endpoint"] = True
    if rows and not any(r.get("serving") for r in rows.values()):
        # Parsed a real endpoint list and still matched nothing. Possible
        # (an account whose dedicated endpoints are all stopped), but it is
        # also the shape a schema change would take, and it flags the WHOLE
        # catalog — loud enough to look at, not fatal.
        logger.warning(
            "together: %d endpoints parsed but none matched any of %d catalog "
            "rows — every row now reads 'needs endpoint'", len(endpoints), len(rows))


async def _direct_models(p: str) -> tuple[list, str | None]:
    """(model rows, error) for a registry provider, from its own /models under
    the user's key — cached per provider. Shared by the /direct-models route
    and the coding-matrix feed so both surfaces show the same list.

    Rows are dicts {id, name?, created?, context_length?, type?,
    input_per_token?, output_per_token?, subscription?, price_source?,
    serving?, needs_endpoint?} —
    richer than bare ids because the catalog surfaces show pricing where it
    can be known truthfully: OpenRouter, DeepInfra,
    Together and Groq publish per-token prices in their /models responses;
    openai/anthropic/xai rows fill list price + context from the warm
    OpenRouter catalog cache (first-party only, provenance in price_source);
    a kimi subscription base marks rows subscription=True (flat plan — no
    per-token price exists). Price fields stay ABSENT (never zero) where none
    of that applies (Fireworks publishes nothing).

    Response-shape note: Together answers a BARE ARRAY, not {data: [...]} —
    that mismatch is why its catalog once read as unreachable.
    Both shapes parse here.

    Together also carries `serving` ('serverless' | 'dedicated') on rows its
    /v1/endpoints list says are callable, and `needs_endpoint` on rows it does
    not — most of that catalog is dedicated-endpoint-only and 400s when called.
    Advisory, never a filter: see the note at the call site for
    why an unmatched row is still offered."""
    from server.vault.providers import (
        PROVIDERS, resolve, resolve_base_url_sync, is_subscription_base,
        resolve_for_inference, signin_serves_inference, SIGNIN_UNUSABLE,
        signin_unusable_message, signin_refused_message, no_credential_message)
    now = time.monotonic()
    cached = _DIRECT_MODELS_CACHE.get(p)
    if cached and cached["exp"] > now:
        # Failures cache too (short TTL) — a 401 provider must not re-hit
        # upstream on every Models-tab render.
        return cached["data"], cached.get("err")
    pool = brain_service.get_pool()
    cred = None
    if pool:
        try:
            if signin_serves_inference(p):
                # The call path's own resolver: the sign-in whenever one
                # exists, the API key only when there is none (never behind
                # it) — so the list shown is what the Host's calls reach.
                cred = await resolve_for_inference(
                    pool, brain_service._entity_id, p,
                    actor="orchestrator-direct-models")
            else:
                cred = await resolve(
                    pool, brain_service._entity_id, p, ("api_key", "oauth_token"),
                    actor="orchestrator-direct-models")
        except Exception:
            cred = None
    if cred and cred.source in SIGNIN_UNUSABLE:
        return [], signin_unusable_message(p, cred.source)
    if not cred or not cred.secret:
        return [], (no_credential_message(p) if signin_serves_inference(p)
                    else f"no {p} key in Keys")
    key = cred.secret
    via_signin = cred.source == "signin"

    def _http_json(url: str, headers: dict):
        import urllib.request
        proxies = urllib.request.getproxies()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(url, headers=headers)
        return json.loads(opener.open(req, timeout=15).read())

    def _fetch():
        # Per-KEY base: kimi subscription keys authenticate on the coding base,
        # not the Moonshot platform base (measured) — every other
        # provider resolves to its registry base with zero probes.
        base = resolve_base_url_sync(p, key) or PROVIDERS[p]["base_url"].rstrip("/")
        # A sign-in spends the user's plan, not a per-token balance — the same
        # 'subscription' label a flat-plan base gets.
        subscription = is_subscription_base(base) or via_signin
        ua = {"User-Agent": "cseries-kernel/0.13"}
        if PROVIDERS[p]["wire"] == "anthropic":
            # Messages API: GET {base_url}/v1/models — x-api-key for an API
            # key, Bearer for an OAuth token (same auth split as the call path
            # in provider_gateway), plus the pinned API version header.
            from mastercode.provider_gateway import _ANTHROPIC_VERSION
            headers = {"anthropic-version": _ANTHROPIC_VERSION, **ua}
            if cred.cred_type == "oauth_token":
                headers["Authorization"] = f"Bearer {key}"
                # Same split as the call path: OAuth tokens need the beta header.
                headers["anthropic-beta"] = "oauth-2025-04-20"
            else:
                headers["x-api-key"] = key
            body = _http_json(base + "/v1/models", headers)
            items = body.get("data") if isinstance(body, dict) else body
            return sorted((r for r in (_dm_row(m) for m in (items or [])) if r),
                          key=lambda r: r["id"])
        headers = {"Authorization": f"Bearer {key}", **ua}
        body = _http_json(base + "/models", headers)
        # {data: [...]} is the common shape; Together answers a bare array.
        items = body.get("data") if isinstance(body, dict) else body
        rows = {r["id"]: r for r in (_dm_row(m) for m in (items or [])) if r}
        if subscription:
            # A flat plan has no per-token list price — the UI shows
            # 'subscription' instead of '—' or an invented number.
            for r in rows.values():
                r["subscription"] = True
        if p == "together":
            # Together's /models lists EVERY model it knows, including ones that
            # are dedicated-endpoint-only. Calling one of those returns
            # 400 "Unable to access non-serverless model <id>" — measured
            # 2026-08-15 on mistralai/Mistral-7B-Instruct-v0.1,
            # mistralai/Mixtral-8x7B-Instruct-v0.1 and a fine-tune, while
            # Qwen/Qwen2.5-7B-Instruct-Turbo and meta-llama/Llama-3.3-70B-
            # Instruct-Turbo answered normally. 177 of 282 catalog rows had no
            # serving endpoint that day, so this is the majority case, not an
            # edge one. NOTHING in the /models row distinguishes them: type,
            # pricing.hourly, `running` and the uuid prefix were all measured
            # identical across working and failing models.
            #
            # /v1/endpoints IS authoritative — {model, type: serverless|
            # dedicated, state: STARTED|STOPPED}. A serverless endpoint is
            # callable; a dedicated one only while STARTED (that is why the
            # account's own arize-ai/qwen-2-1.5b-instruct works).
            #
            # MARK, NEVER FILTER. Endpoint ids are not model ids: the endpoint
            # `thinkingmachines/Inkling` serves the catalog model
            # `thinkingmachines/Inkling-Small`, which answers fine despite no
            # exact match. Hiding unmatched rows would have hidden a working
            # model, so an unmatched row is advisory only. Matching was 8/8 on
            # models it claims are callable, with no false positives.
            try:
                eps = _http_json(base + "/endpoints", headers)
                _together_mark_serving(
                    rows, eps if isinstance(eps, list) else (eps.get("data") or []))
            except Exception as e:
                # No endpoint list → mark nothing. An unmarked catalog is the
                # older behaviour; a catalog where every row claims
                # "needs an endpoint" would be a worse lie than silence.
                logger.info("together endpoint list unavailable: %s", e)
        if p == "deepinfra":
            # DeepInfra's keyed OpenAI-wire /models returns bare ids; pricing +
            # context live on its PUBLIC /models/list (no auth). Enrich the
            # served set from it — the only generic provider that publishes
            # per-token prices, so this is what puts real numbers in its pane.
            try:
                pub = _http_json("https://api.deepinfra.com/models/list", ua)
                for m in (pub if isinstance(pub, list) else []):
                    r = _dm_row(m)
                    if r and r["id"] in rows:
                        rows[r["id"]].update(
                            {k: v for k, v in r.items()
                             if k not in rows[r["id"]] or k in (
                                 "input_per_token", "output_per_token",
                                 "context_length")})
            except Exception as e:
                logger.info("deepinfra public pricing list unavailable: %s", e)
        return sorted(rows.values(), key=lambda r: r["id"])

    try:
        models = await asyncio.to_thread(_fetch)
    except Exception as e:
        # Surface the upstream REASON, not a generic "unreachable" — a 401
        # means "re-issue the key in Keys" and a 403 spending-limit means "top
        # up at the vendor", and the user can only act on the truth (seen
        # on kimi/xai rows). Provider account details in the message
        # are shown only to the account owner.
        logger.warning("provider %s /models fetch failed: %s", p, e)
        import urllib.error
        if isinstance(e, urllib.error.HTTPError):
            detail = ""
            try:
                body = json.loads(e.read() or b"{}")
                err = body.get("error") or {}
                detail = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                detail = ""
            msg = f"{p} catalog: HTTP {e.code}" + (f" — {str(detail)[:140]}" if detail else "")
            if via_signin and e.code in (401, 403):
                # Said in words; never listed again on the key.
                msg = signin_refused_message(p, e.code)
        else:
            msg = f"{p} catalog unreachable"
        # Cache the failure briefly: a broken provider must not re-hit
        # upstream (15s timeout each) on every Models-tab render.
        _DIRECT_MODELS_CACHE[p] = {"data": [], "err": msg,
                                   "exp": now + _DIRECT_MODELS_TTL_FAIL}
        return [], msg
    _enrich_first_party_from_openrouter(p, models)
    _DIRECT_MODELS_CACHE[p] = {"data": models, "exp": now + _DIRECT_MODELS_TTL}
    return models, None


@router.get("/direct-models/{provider}", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_direct_provider_models(provider: str):
    """Model ids `provider` LISTS, from its own /models under the user's key.
    Empty + error string when there is no key or the catalog is unreachable —
    the picker shows that honestly instead of degrading to free text.

    Listed is not the same as servable: 177 of Together's 282 rows needed a
    dedicated endpoint when measured. Those carry needs_endpoint (see
    _together_mark_serving); rows known callable carry serving."""
    from server.vault.providers import PROVIDERS
    p = (provider or "").strip().lower()
    if p not in PROVIDERS:
        raise HTTPException(400, f"unknown provider {p!r}. Valid: {sorted(PROVIDERS)}")
    models, err = await _direct_models(p)
    # VALIDATED stamped AT THE ROUTE, on copies: _direct_models' rows are
    # cached and folded into the shared catalog, and a stamp inside the cache
    # would persist across a list change.
    stamped = [({**m, "validated": _validated(p, m.get("id")),
                 "standard": _standard(p, m.get("id"))} if isinstance(m, dict) else m)
               for m in (models or [])]
    out: dict = {"provider": p, "models": stamped}
    if err:
        out["error"] = err
    return out


# Which model can SEE and which can DRAW. Before this the
# Orchestrator's image roles offered a free-text "BYO endpoint" box plus an
# unfiltered model list, so picking a working image model required knowing
# something the product never told you. Capability is read from the catalog's
# modality lists — never guessed from a model NAME (a substring check on
# "vision"/"image" would both miss and over-claim).
_CAPABILITY_MODALITY = {
    # role → (which modality list, the modality that must appear in it)
    "vision":    ("input_modalities", "image"),
    "image_gen": ("output_modalities", "image"),
}


@router.get("/capable-models/{role}", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_capable_models(role: str, provider: str | None = None):
    """Models that can actually do `role`, per provider the user holds a key for.

    role: a CAPABILITY, not a route role — 'vision' (the model reads images:
    what a Host model needs to see a picture it is shown) | 'image_gen' (makes
    an image).

    Every row is a model whose catalog entry DECLARES the modality. A provider
    whose catalog we couldn't read returns its error rather than an empty list,
    because "no image models" and "couldn't look" are different sentences and
    the picker must not print the first when it means the second.

    Rows carry `verified`: True when the provider's own catalog stated the
    modality, False when it was inherited from the matching OpenRouter row by
    suffix match (_enrich_first_party_from_openrouter). The UI says which.
    """
    from server.vault.providers import PROVIDERS
    r = (role or "").strip().lower()
    if r not in _CAPABILITY_MODALITY:
        raise HTTPException(
            400, f"unknown capability {r!r}. Valid: {sorted(_CAPABILITY_MODALITY)}")
    field, needed = _CAPABILITY_MODALITY[r]

    wanted = [(provider or "").strip().lower()] if provider else sorted(PROVIDERS)
    for p in wanted:
        if p not in PROVIDERS:
            raise HTTPException(400, f"unknown provider {p!r}. Valid: {sorted(PROVIDERS)}")

    out: dict = {"role": r, "providers": {}}
    for p in wanted:
        models, err = await _direct_models(p)
        if err:
            out["providers"][p] = {"error": err}
            continue
        def _row(m, verified):
            return {
                "id": m.get("id"),
                "name": m.get("name") or m.get("id"),
                "context_length": m.get("context_length"),
                "input_per_token": m.get("input_per_token"),
                "output_per_token": m.get("output_per_token"),
                "verified": verified,
            }
        rows, declares_any = [], False
        for m in models or []:
            if isinstance(m.get("input_modalities"), list) or isinstance(
                    m.get("output_modalities"), list):
                declares_any = True
            mods = m.get(field)
            if isinstance(mods, list) and needed in mods:
                rows.append(_row(m, not m.get("modality_source")))
        # A provider whose catalog publishes NO modality data at all is a THIRD
        # state, and collapsing it into "none of their models can do this" is a
        # false claim — the same couldn't-look-vs-none distinction the error
        # branch above makes. xAI is the live case: its /models carries no
        # modality fields, and the OpenRouter enrichment cannot fill the gap
        # because OpenRouter lists no image-OUTPUT x-ai model at all (checked
        # live 2026-08-31: 6 x-ai models, 6 vision-capable, 0 generating) —
        # while xAI itself serves grok-imagine-image* on /images/generations
        # (docs.x.ai/docs/models). Filtering silently would hide exactly the
        # models the user holds a key for. Hand back the full list, flagged, so
        # the picker can say so and let the user choose.
        if not declares_any and models:
            out["providers"][p] = {"capability_published": False,
                                   "models": [_row(m, False) for m in models]}
            continue
        out["providers"][p] = {"capability_published": True, "models": rows}
    return out


# Harnesses that run their OWN sidecar instead of the shared _harness_runtime
# — so they cannot appear in ALL_SPECS — but DO expose the vendor's own
# POST /login on their pod.
#
# claude-code is the only one, and it cannot simply be added to ALL_SPECS to
# express this: _harness_runtime/__main__.py serves a harness by looking its id
# up there, so an entry would wire the shared /run routes for a harness that
# has its own runtime, and the runtime tests assert shared-/run properties
# across every spec. Its login is the Claude Agent SDK's OWN bundled `claude`
# binary running `auth login --claudeai`
# (system-apps/claude-code-harness/bin/main.py) — the same
# the-vendor-owns-the-login rule every other harness follows.
#
# BOTH ids are listed on purpose. This predicate is called with two different
# id spaces and claude is the one harness where they differ:
#   "claude-code" — the harness/app id (HARNESS_APP_IDS, the login flow)
#   "claude-sdk"  — the PROVIDERS entry id, which is what _build_coding_matrix
#                   passes (_HARNESS_PROVIDER = {"claude-code": "claude-sdk"})
# Listing only the harness id left the matrix's claude row with no Connect
# action while codex and grok lit up — caught by exercising the real
# _build_coding_matrix in the live pod, not by reading this file.
_STANDALONE_LOGIN_HARNESSES = frozenset({"claude-code", "claude-sdk"})


def _harness_has_login(harness_id: str) -> bool:
    """True when this harness's pod carries the vendor's own login command.

    The runtime specs (HarnessSpec.login_argv) stay the primary source, so a
    shared-runtime harness gains a Connect button the moment its spec gains a
    login and loses it the moment that goes away. The one addition is
    _STANDALONE_LOGIN_HARNESSES above, for a harness whose pod has a /login but
    which structurally cannot live in ALL_SPECS — see the note there for why
    that is a real constraint and not a shortcut. False on any import trouble:
    offering a login that cannot run is worse than sending the user to Keys."""
    if harness_id in _STANDALONE_LOGIN_HARNESSES:
        return True
    try:
        from _harness_runtime import specs as harness_specs
    except Exception:
        # Two layouts, and only ONE of them was covered before:
        #   source tree — <repo>/system-apps/_harness_runtime/
        #   entity IMAGE — /app/cli/_harness_runtime/   <- this was missing
        # In the image this file is /app/server/routes/inference.py, so the
        # three-dirname walk below lands on /app/system-apps, which does not
        # exist there. The module is COPY'd to /app/cli (the same path
        # cseries_sidecar rides on, which is why the claude-code sidecar's
        # `sys.path.insert(0, "/app/cli")` has always worked).
        #
        # Measured in a live pod before this fix:
        #   from _harness_runtime import specs
        #     -> ModuleNotFoundError: No module named '_harness_runtime'
        #   _harness_has_login('codex') = False   (and grok the same)
        # So the vendor-login action was dead for EVERY harness — the
        # "Connect Codex" row in the coding matrix came from the paste-a-key
        # branch, not the login branch. The host could never offer a vendor
        # login to anyone, which is why the broken login round-trip went
        # unnoticed: it was unreachable from the surface that offers it.
        for cand in ("/app/cli",
                     os.path.join(os.path.dirname(os.path.dirname(
                         os.path.dirname(__file__))), "system-apps")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
        try:
            from _harness_runtime import specs as harness_specs
        except Exception as e:
            logger.debug("harness specs unavailable for login check: %s", e)
            return False
    # ALL_SPECS (specs.py:965) — the registry name; there is no `SPECS`.
    spec = getattr(harness_specs, "ALL_SPECS", {}).get(harness_id)
    return bool(spec is not None and getattr(spec, "login_argv", None))


async def _build_coding_matrix(*, kick_catalogs: bool = False) -> dict:
    """ONE resolution for the coding matrix: every harness, its band, the
    key it actually runs on, the models that key's provider serves, its
    effort set, and its saved overrides. The screen, the
    harness_connections host tool and any future validator read THIS build
    — what you see, what the host says, and what fires can never be three
    different stories.

    Every row is returned, always — a harness with no key is a row with an
    action, never a missing row (the old picker's vanishing-harness problem).

    `kick_catalogs`: a harness whose catalog only its own CLI
    knows (cursor) is READ by waking its pod. Only the two callers where
    someone is LOOKING at the list pass True — the GET /coding-matrix route
    behind the screen, and the harness_connections host tool. Validators and
    coding_protocols.preflight build the matrix to CHECK things and must not
    start a pod as a side effect; they see cached rows or none."""
    from server.routes.review import _CATALOG_STATUS_HARNESSES as _CLI_CATALOG_HARNESSES
    from server.routes.harnesses import _harness_app_to_provider, _creds_status
    from server.network.harness_keys import (
        _fallback_providers, harness_band, KEYS_HARNESS_PROVIDERS)
    from server.network import harness_state
    from server.vault.providers import generic_providers

    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    app_to_provider = _harness_app_to_provider()

    # One behavioral_params read for the per-engine overrides, the default
    # tick and the wiring.
    selection: list = []
    wiring: dict = {}
    default_engine: str | None = None
    if pool is not None:
        try:
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
            bp = (row and row["behavioral_params"]) or {}
            if isinstance(bp, str):
                bp = json.loads(bp or "{}")
            if isinstance(bp, dict):
                selection = known_coding_harness_entries(bp.get("coding_harnesses"))
                w = bp.get("credential_wiring") or {}
                wiring = w if isinstance(w, dict) else {}
                d = str(bp.get("default_coding_harness") or "").strip()
                default_engine = d if d in HARNESS_APP_IDS else None
        except Exception as e:
            logger.info("coding-matrix: params read failed: %s", e)
    overrides: dict = {}
    for h in selection:
        hid = (h.get("id") if isinstance(h, dict) else h) or ""
        hid = str(hid).strip()
        if not hid:
            continue
        if isinstance(h, dict):
            o = {}
            if h.get("model"):
                o["model"] = h["model"]
            if h.get("effort"):
                o["effort"] = h["effort"]
            overrides[hid] = o

    # Which registry providers the user holds a key for (30s-cached).
    status = await _provider_status(pool, eid)
    held = {s["id"] for s in status if s.get("has_key") and s["id"] != "local"}

    cred_recs = await harness_state.credential_records()
    dead = await harness_state.disconnected_set()
    vendor_models = _harness_model_options()
    # What each harness's OWN CLI last reported in this entity's pod (cursor
    # today). One read for the whole matrix; {} until a run has cached one.
    catalogs = await harness_state.model_catalogs()

    # Price join: the OpenRouter catalog cache carries pricing; DeepInfra rows
    # already bring their own from _direct_models. Attached where
    # known, absent (never zero) where not.
    or_prices: dict = {}
    cat = _CATALOG_CACHE["data"] or {}
    for m in cat.get("openrouter_models", []):
        if m.get("id"):
            or_prices[m["id"]] = (
                m.get("input_per_token"), m.get("output_per_token"),
                {k: m[k] for k in ("cache_read_per_token", "cache_write_per_token")
                 if k in m})

    async def _models_for(h: str, entry_id: str, provider: str | None) -> tuple[list, str | None]:
        if h == "claude-code":
            # The same list _harness_model_options hands the validator.
            return [{"id": m} for m in vendor_models.get("claude-code", [])], None
        if entry_id in KEYS_HARNESS_PROVIDERS and _fallback_providers(entry_id):
            # Key-driven harnesses (kimi + the generic band): the models the
            # ACTUAL provider serves under the user's key — never a reseller's
            # view of the vendor.
            if not provider:
                return [], "no key"
            rows, err = await _direct_models(provider)
            out = []
            for r in rows:
                m: dict = dict(r)
                if provider == "openrouter" and "input_per_token" not in m \
                        and m.get("id") in or_prices:
                    _op = or_prices[m["id"]]
                    m["input_per_token"], m["output_per_token"] = _op[0], _op[1]
                    # The cache rates travel with the price they belong to, or
                    # this row would claim an input rate and silently drop the
                    # cache rate that goes with it.
                    for _k, _v in (_op[2] or {}).items():
                        m[_k] = _v
                out.append(m)
            return out, err
        if h == "codex":
            # The pinned Codex CLI's OWN catalog (what --model can take), with
            # each model's accepted efforts + default so the picker can narrow
            # the effort knob per model. Never the reseller's list — see
            # CODEX_CLI_MODELS.
            #
            # `default_effort` answers "what runs if I pick no effort", so it is
            # the EFFECTIVE default: the platform's own (CODEX_CLI_DEFAULT_EFFORTS
            # — what the sidecar actually passes) where there is one, else the
            # catalog's default_reasoning_level. `vendor_default_effort` carries
            # the CLI's own value either way, so the UI can show that the
            # platform is overriding it rather than quietly disagreeing with the
            # binary.
            return [{"id": m["id"], "label": m["label"],
                     "efforts": list(m["efforts"]),
                     "default_effort": CODEX_CLI_DEFAULT_EFFORTS.get(
                         m["id"], m["default_effort"]),
                     "vendor_default_effort": m["default_effort"]}
                    for m in CODEX_CLI_MODELS], None
        if h in _CLI_CATALOG_HARNESSES:
            # These catalogs are PER-ACCOUNT and only the vendor's own CLI knows
            # them, so there is nothing to mirror here (codex's table ships in
            # its binary and renders offline; these are backend calls). The rows
            # below are what `cursor-agent --list-models` / `agy models`
            # reported IN THIS ENTITY'S pod, cached after a run. No cache yet =
            # say so; an empty list with a stated reason is the honest answer,
            # and the model validator fails open on it, so nothing the user
            # saved gets blocked. Cached -> serve. Not cached -> the row's
            # `models_loading` path below WAKES THE POD AND READS IT; never a
            # sentence in place of the data.
            #
            # antigravity once matched NO branch
            # here and fell through to the vendor-catalog line below, where
            # _harness_model_options has no antigravity key — so its picker was
            # an empty list with models_error=null: no models, and no reason.
            cached_rows = list((catalogs.get(h) or {}).get("models") or [])
            return ([dict(r) for r in cached_rows], None)
        # Vendor-ACCOUNT harnesses (grok): no API key exists to ask the vendor's
        # /models with, so the vendor-filtered catalog list is the best
        # available source — flagged as such in models_source.
        return [{"id": m} for m in vendor_models.get(h, [])], None

    def _catalog_loading(h: str, key_state: str, models: list) -> tuple[bool, str | None]:
        """(models_loading, models_error) for a CLI-catalog harness (cursor)
        whose list is empty. Connected + empty = start the wake-and-read NOW
        (review.kick_model_catalog_refresh runs it in the background so this
        render returns at once; single-flight, so N concurrent renders share
        ONE wake) and report `loading` truthfully — it is a claim about a task
        that exists. After a refresh that FAILED, carry its text (the vendor's
        own "no models for this account" etc.) so the row says something the
        user can act on. The screen polls the matrix while a row is loading."""
        from server.routes import review as _rv
        if h not in _rv._CATALOG_STATUS_HARNESSES or models or key_state != "connected":
            return False, None
        if _rv.model_catalog_refresh_in_flight(h):
            return True, None          # a read someone else started is still running
        if kick_catalogs and _rv.kick_model_catalog_refresh(h):
            return True, None
        # Not kicking (a validator/preflight build), or the kick was refused
        # (cooldown after a failed read): carry the last failure text if any.
        return False, _rv.model_catalog_last_error(h)

    rows = []
    for h in HARNESS_APP_IDS:
        entry = app_to_provider.get(h)
        if not entry:
            continue
        entry_id = entry["id"]
        band = harness_band(entry_id)
        st = _creds_status(entry)
        state = harness_state.key_state(
            present=bool(st.get("present")), valid=bool(st.get("valid")),
            disconnected=entry_id in dead)
        rec = cred_recs.get(entry_id)
        rec = rec if isinstance(rec, dict) else None

        allowed = _fallback_providers(entry_id)
        options = [p for p in allowed if p in held]
        wired = (wiring.get(f"harness:{entry_id}") or "").strip().lower()
        wired = wired if wired in allowed else None
        if band == "generic":
            # What it IS running on (provenance) beats what it WOULD pick.
            running = (rec or {}).get("name")
            provider = (running if running in allowed
                        else wired if wired in options
                        else options[0] if options else None)
        elif entry_id in KEYS_HARNESS_PROVIDERS and allowed:
            provider = allowed[0]          # first-party bound (kimi)
        else:
            provider = None                # vendor account

        models, models_err = await _models_for(h, entry_id, provider)
        models_loading = False
        if not models and not models_err:
            models_loading, models_err = _catalog_loading(h, state, models)

        # How this harness CONNECTS decides what its action does. A vendor
        # account (no provider key can drive it) connects through the vendor's
        # OWN login, driven by the host in conversation — kind='login'.
        # Everything else is a credential the user pastes, so Keys is the flow.
        by_login = not allowed and _harness_has_login(entry_id)
        action = None
        if state == "disconnected":
            action = ({"label": f"Reconnect {HARNESS_LABELS.get(h, h)}", "kind": "login"}
                      if by_login else
                      {"label": "Add a key to reconnect", "app": "keys"})
        elif state == "expired":
            action = ({"label": f"Sign in to {HARNESS_LABELS.get(h, h)} again", "kind": "login"}
                      if by_login else
                      {"label": "Add a fresh key", "app": "keys"})
        elif state == "not_set_up":
            if by_login:
                action = {"label": f"Connect {HARNESS_LABELS.get(h, h)}", "kind": "login"}
            elif band == "generic":
                action = {"label": "Add a generic provider key", "app": "keys"}
            elif provider:
                action = {"label": f"Add a {provider} key", "app": "keys",
                          "provider": provider}
            else:
                action = {"label": f"Connect {HARNESS_LABELS.get(h, h)}",
                          "app": entry.get("install_app_id", h)}

        rows.append({
            "id": h,
            "label": HARNESS_LABELS.get(h, h),
            "icon_url": f"/assets/harness/{h}.png",
            "band": band,
            "vendor": entry.get("vendor"),
            "provider": provider,
            "provider_options": options if band == "generic" else [],
            "wired": wired,
            "key": {"state": state,
                    "name": (rec or {}).get("name"),
                    "source": (rec or {}).get("source"),
                    "expires_at": st.get("expires_at")},
            "models": models,
            "models_error": models_err,
            # True while the wake-and-read for this row's CLI catalog is in
            # flight (cursor); the screen polls the matrix until it clears.
            "models_loading": models_loading,
            "models_source": ("provider" if (entry_id in KEYS_HARNESS_PROVIDERS
                                             and allowed) else
                              "engine" if h == "claude-code" else
                              # cli_catalog = the vendor CLI's own list. codex's
                              # is mirrored in-image; cursor's and antigravity's
                              # are read per account in the pod and cached
                              # (see _models_for).
                              "cli_catalog" if h == "codex"
                              or h in _CLI_CATALOG_HARNESSES else
                              "vendor_catalog"),
            "efforts": list(HARNESS_EFFORTS.get(h, ())),
            # What fires when no model override is saved — the host reads this
            # row to answer "which model?" (None = the harness decides and we
            # have not mirrored its default).
            "default_model": HARNESS_DEFAULT_MODELS.get(h),
            # The Default tick: an unnamed build runs here. One row at most.
            "default": h == default_engine,
            "model_override": overrides.get(h, {}).get("model"),
            "effort_override": overrides.get(h, {}).get("effort"),
            "action": action,
        })
    return {
        "harnesses": rows,
        # The ticked default engine (None = no tick: an unnamed build refuses
        # when several engines are connected, stated instead of guessed).
        "default_engine": default_engine,
        "generic_providers_held": sorted(set(generic_providers()) & held),
    }


@router.get("/coding-matrix", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_coding_matrix():
    """The coding matrix: every harness, its band (first-party / generic), the
    key it runs on, its models, efforts, saved overrides, and the one action
    that fixes a row that can't fire. One build, consumed by the Orchestrator
    screen and the harness_connections host tool alike."""
    return await _build_coding_matrix(kick_catalogs=True)


class SetHarnessWiring(BaseModel):
    harness: str
    provider: str | None = None   # null = clear the choice (back to the default order)


@router.put("/harness-wiring", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def set_harness_wiring(body: SetHarnessWiring):
    """The generic-band provider picker's write: which of the user's generic
    keys this harness runs on. Refuses anything outside the harness's allowed
    set — the wiring reorders, it never widens (a kimi wiring naming
    openrouter must stay impossible end to end: refused here, refused again at
    every read). On success the credential is re-materialized immediately
    (force) so the choice takes effect NOW, not at the next time the file
    happens to be invalid."""
    from server.network.harness_keys import (
        _fallback_providers, harness_band, materialize_from_keys)
    from server.network import harness_state
    h = (body.harness or "").strip().lower()
    if harness_band(h) != "generic":
        raise HTTPException(
            400, f"{h!r} is not a generic-band harness — first-party harnesses "
                 "are bound to their own provider and cannot be rewired")
    p = (body.provider or "").strip().lower() or None
    allowed = _fallback_providers(h)
    if p is not None and p not in allowed:
        raise HTTPException(
            400, f"{h} may not run on {p!r}. Allowed: {list(allowed)}")
    if not await harness_state.set_wiring(h, p):
        raise HTTPException(503, "could not store the wiring")
    status = await materialize_from_keys(h, force=True)
    return {"ok": True, "harness": h, "provider": p, "materialize": status}


@router.put("/routing", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def set_role_model(body: SetRoleModel):
    """Assign a {provider, model} route to a role (optional failover pair).

    The USER's write (the Orchestrator grid, the Circles picker): applies at
    once, logged set_by='user'. The host's own writes go through
    apply_role_route with their own set_by, and the host's tool decides
    first whether a write is allowed to apply at all (runtime/route_change)."""
    return await apply_role_route(body, set_by="user")


ROUTE_SET_BY = ("user", "decision", "host", "host_revert", "canary")


async def previous_approved_route(pool, entity_id: int, role: str,
                                  current_provider: str | None,
                                  current_model: str | None) -> dict | None:
    """The route the host may REVERT to for free: the newest route the USER
    approved for this role (an Orchestrator write or an approved proposal)
    that is not the one in force — one step back, never further. The revert
    rule's one read (migrations/v158). The host's own rows do not count, and
    a route approved long ago and deliberately left is not a standing
    licence to return to it.

    A CANARY REJECTION RETIRES AN APPROVAL. A set_by='canary' row carries the
    (provider, model) it reverted FROM (rejected_provider / rejected_model —
    recorded on the row, never inferred from adjacency, so a user apply that
    interleaves cannot be blamed for it); that route's older approvals no
    longer count until the user approves it again (a newer user/decision
    row). Without this the host could put the rejected model straight back
    as a free revert one call after the platform took it away."""
    try:
        rows = await pool.fetch(
            "SELECT provider, model, effort, fallback_provider, fallback_model, set_by, at, id "
            "FROM model_route_log WHERE entity_id = $1 AND role = $2 "
            "AND set_by IN ('user', 'decision') ORDER BY at DESC, id DESC LIMIT 60",
            int(entity_id), role)
        # The rejections, read on their own so a canary row is never lost
        # off the end of the approvals window.
        crows = await pool.fetch(
            "SELECT rejected_provider, rejected_model, at, id FROM model_route_log "
            "WHERE entity_id = $1 AND role = $2 AND set_by = 'canary' "
            "ORDER BY at DESC, id DESC LIMIT 60",
            int(entity_id), role)
    except Exception as e:  # noqa: BLE001 — a cold table has no approved route
        logging.getLogger("cseries.inference").debug("model_route_log read failed: %s", e)
        return None
    rows = list(rows or [])
    cp, cm = str(current_provider or "").lower(), str(current_model or "").lower()
    # Rejected pairs, with the moment of rejection (at, id — the id breaks a
    # same-tick tie, so an approval written in the same instant as a revert
    # but after it is not retired). Newest rejection wins the key.
    rejected: dict[tuple[str, str], tuple] = {}
    for r in crows or []:
        if r.get("rejected_provider") and r.get("rejected_model"):
            key = (str(r["rejected_provider"]).lower(), str(r["rejected_model"]).lower())
            if key not in rejected:
                rejected[key] = (r["at"], int(r["id"] or 0))
    for r in rows:
        key = (str(r["provider"]).lower(), str(r["model"]).lower())
        if key == (cp, cm):
            continue
        rej = rejected.get(key)
        if rej is not None and (r["at"], int(r["id"] or 0)) <= rej:
            continue    # approved before the canary rejected it: retired
        return {"provider": r["provider"], "model": r["model"], "effort": r["effort"],
                "fallback_provider": r["fallback_provider"], "fallback_model": r["fallback_model"]}
    return None


async def _log_route_apply(pool, entity_id: int, role: str, entry: dict,
                           set_by: str, decision_id: int | None,
                           rejected: tuple[str, str] | None = None) -> None:
    """One append-only row per apply (migrations/v158). Never raises.
    `rejected` is the (provider, model) a canary revert came FROM, recorded
    on the canary row itself."""
    if set_by not in ROUTE_SET_BY:
        set_by = "host"
    rp, rm = (str(rejected[0]), str(rejected[1])) if rejected else (None, None)
    try:
        await pool.execute(
            "INSERT INTO model_route_log (entity_id, role, provider, model, effort, "
            "fallback_provider, fallback_model, set_by, decision_id, rejected_provider, rejected_model) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            int(entity_id), role, str(entry.get("provider") or ""), str(entry.get("model") or ""),
            entry.get("effort"), entry.get("fallback_provider"), entry.get("fallback_model"),
            set_by, int(decision_id) if decision_id is not None else None, rp, rm)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("cseries.inference").warning("model_route_log write failed (non-fatal): %s", e)


async def apply_role_route(body: SetRoleModel, *, set_by: str = "user",
                           decision_id: int | None = None,
                           rejected: tuple[str, str] | None = None):
    """Validate and WRITE a role's route, then log who set it. Every writer
    lands here — the Orchestrator PUT (set_by='user'), an approved proposal
    (set_by='decision', server/routes/tickets.py on the user's apply), the host filling an
    unrouted role (set_by='host') or reverting to an approved route
    (set_by='host_revert'). Whether the host may write at all is decided
    BEFORE this, in runtime/route_change; this function only writes."""
    if body.role not in ROLES:
        raise HTTPException(400, f"Invalid role: {body.role}. Valid: {ROLES}")

    # The lane model is GONE (hard cut): a legacy {lane, ...}
    # payload with no provider gets a pointed refusal instead of a silent
    # migration guess.
    if (body.lane or "").strip() and not (body.provider or "").strip():
        raise HTTPException(
            400, "lanes are gone — send {provider, model} (provider is a Keys "
                 "registry id or 'local')")

    from mastercode.inference_config import (
        normalize_provider, provider_allowed_for_role, _BYO_ALLOWED_ROLES,
    )
    raw_provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    if not raw_provider:
        raise HTTPException(400, "provider is required — send {provider, model}")
    if not model:
        raise HTTPException(400, "model is required — there is no default model")
    if not provider_allowed_for_role(raw_provider, body.role):
        raise HTTPException(
            400, f"Invalid provider for role {body.role!r}: {raw_provider!r}. "
                 "Valid: a Keys registry id (openrouter, anthropic, …) or "
                 "'local'. There is no Claude-subscription lane — Anthropic is an "
                 "ordinary provider keyed in the Keys app.")

    # Store the NORMALIZED pair: legacy aliases resolve (openrtr → openrouter)
    # and a Claude-shaped pick on openrouter coerces to provider 'anthropic'
    # with the Anthropic-native id (Claude never transits OpenRouter).
    provider, model = normalize_provider(raw_provider, model)

    # Optional failover pair — validated the same way as the primary, EXCEPT
    # blank/SDK values silently drop to None (a stale subscription fallback
    # just disappears; fallback_model None = retry the primary model id on
    # the fallback provider).
    fallback_provider = fallback_model = None
    if (body.fallback_provider or "").strip():
        fp, fm = normalize_provider(
            body.fallback_provider.strip(), (body.fallback_model or "").strip())
        if fp:
            if not provider_allowed_for_role(fp, body.role):
                raise HTTPException(
                    400, f"Invalid fallback provider: {body.fallback_provider!r}")
            fallback_provider, fallback_model = fp, (fm or None)

    # base_url (a caller-supplied endpoint) is for BYO-endpoint roles
    # (embedding/image, which register their own endpoint — s2-embed-byo /
    # s2-image-route). Text roles take one only on provider 'local', and even
    # there it is never stored (see the local-first block below).
    base_url = (body.base_url or "").strip() or None
    if base_url is not None:
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise HTTPException(400, f"base_url must be an http(s) URL, got: {base_url!r}")
        if provider != "local" and body.role not in _BYO_ALLOWED_ROLES:
            raise HTTPException(
                400,
                f"role '{body.role}' can't carry a base_url endpoint on provider "
                f"{provider!r}: text roles take an endpoint only on provider "
                "'local'.",
            )

    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    # Embedding has its OWN store + re-embed flow (embedding.py), not a
    # model_routing entry. embedding.py normalizes the provider into its own
    # local|privatae taxonomy, so the routing provider id passes through.
    if body.role == "embedding":
        from server.routes.embedding import apply_embedding_switch
        res = await apply_embedding_switch(
            pool, brain_service._entity_id, model=model, provider=provider,
        )
        if res.get("error"):
            raise HTTPException(400, res["error"])
        # The ledger is the memory of EVERY entry the table has held —
        # embedding included, even though its store lives elsewhere.
        await _log_route_apply(pool, brain_service._entity_id, "embedding",
                               {"provider": provider, "model": model}, set_by, decision_id)
        return {"set": True, "role": "embedding", **res}

    eid = brain_service._entity_id

    # Local-first default: an openrouter pick whose model a
    # machine on the user's own network serves right now (the local model
    # table, mastercode.model_backends) flips to provider 'local' — the user
    # runs it themselves for free instead of via OpenRouter. Only openrouter
    # carries that ambiguity: a DIRECT provider pick (kimi, openai, …) names
    # whose key pays, so it is honored verbatim.
    #
    # No pinned endpoints (after a chat hang): a text
    # route stores {provider, model} ONLY. The serving machine's address is
    # resolved live from bastion heartbeats at call time (resolve_role_route) —
    # a write-time pin goes stale when the machine re-enrolls with a new mesh
    # address, and the route then dials a dead address forever. A DIRECT local
    # pick is refused outright when nothing on the network serves the model:
    # an unroutable local route must never be stored.
    defaulted_local = None
    if body.role not in _BYO_ALLOWED_ROLES:
        if provider == "local" and base_url:
            raise HTTPException(
                400, "local routes don't take an endpoint on the route — add the "
                     "server under Model servers (PUT /api/inference/local-endpoints) "
                     "and pick its model; the serving server is looked up at call time")
        _local_hit = None
        if provider in ("openrouter", "local"):
            try:
                from mastercode.model_backends import match_local_model
                _local_hit = await match_local_model(pool, eid, model)
            except Exception:
                _local_hit = None
        # Not while the Host model standard is enforced: a local serving has
        # no provable identity (runtime/model_standard), so the flip would
        # turn an approved OpenRouter pick into a refused one.
        _standard_on = False
        try:
            from mastercode.runtime import model_standard as _ms
            _standard_on = _ms.enforced()
        except Exception:  # noqa: BLE001
            _standard_on = False
        if provider == "openrouter" and _local_hit and not _standard_on:
            defaulted_local = _local_hit["bastion"]
            provider = "local"
        if provider == "local" and not _local_hit:
            raise HTTPException(
                409, f"no machine on your network is serving {model!r} right "
                     "now — provider 'local' only routes models your own "
                     "online machines actually serve")
        if provider == "local":
            base_url = None    # never stored; resolved live on every call

    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)

    # The engine/route mutual exclusion died with the host-engine switch:
    # the resolver can no longer resolve a Claude-subscription engine route, so
    # a stored route can never hide behind a stale host_engine_source flag.

    # Effort: validated against what this PROVIDER accepts and what the
    # gateway actually sends (TEXT_ROLE_EFFORTS). Fails the write rather than
    # storing a value that would be dropped at dial time — a saved setting that
    # does nothing is worse than a refused one.
    effort = (body.effort or "").strip()
    # EXCEPT after the local-first flip: the caller validly asked for an
    # openrouter effort, WE moved the route to 'local' (whose dial path,
    # _call_local, takes no effort at all) — refusing here would 400 exactly
    # the pick local-first exists to reward. Drop the effort instead of
    # storing a dead knob, and say so on the response (effort_dropped).
    effort_dropped = False
    if effort and defaulted_local:
        effort = ""
        effort_dropped = True
    # Per MODEL on anthropic (the level must be one the model's catalog row
    # lists, the rule the gateway sends by), so the Anthropic list is read
    # first when it is cold.
    if effort:
        await await_effort_catalog(provider, model)
    _eff_err = validate_role_effort(provider, effort, model)
    if _eff_err:
        raise HTTPException(400, _eff_err)

    # THE HOST MODEL STANDARD (runtime/model_standard): a
    # text role may be saved only on an approved model — judged per model,
    # whichever provider serves it — and so may its fallback, which answers
    # with the same tools when the primary errors. Checked AFTER the
    # local-first flip and the effort rules, so the verdict is on the pair
    # that will actually be stored. Inert until the data file's `enforce` is
    # true; refusal() returns None until then.
    # Image roles (no NSFW or uncensored image models):
    # image generation is held to the image allowlist. A pinned endpoint is
    # unidentified. (A model that READS images is the Host model itself,
    # so the text standard above already covers it.)
    if body.role in ("image", "image_gen"):
        from mastercode.runtime import model_standard
        _ident = None if base_url else provider
        _std = model_standard.refusal_image(_ident, model)
        if _std:
            raise HTTPException(409, _std)
    if body.role not in _BYO_ALLOWED_ROLES:
        from mastercode.runtime import model_standard
        _std = model_standard.refusal(provider, model, effort or None)
        if _std:
            raise HTTPException(409, _std)
        if fallback_provider:
            _fstd = model_standard.refusal(fallback_provider, fallback_model or model,
                                           effort or None)
            if _fstd:
                raise HTTPException(409, f"fallback: {_fstd}")

    routing = params.get("model_routing", {})
    entry = {"provider": provider, "model": model}
    if effort:
        entry["effort"] = effort
    if body.reasoning_inline is True:
        entry["reasoning_inline"] = True
    if fallback_provider:
        entry["fallback_provider"] = fallback_provider
        if fallback_model:
            entry["fallback_model"] = fallback_model
    if base_url is not None:
        entry["base_url"] = base_url
    if body.connection_id:
        entry["connection_id"] = body.connection_id
    routing[body.role] = entry
    params["model_routing"] = routing

    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )

    # A route was just saved: requests held because this Host had no chat model
    # (identity_hold / offline_task_runner) can run now.
    if body.role == "chat":
        try:
            from mastercode.brain import offline_task_runner as _otr
            from mastercode.runtime.identity_hold import spawn as _spawn
            _spawn(_otr.release_saved_requests(pool, eid))
        except Exception as _rel_err:  # noqa: BLE001
            logging.getLogger("cseries.inference").debug(
                "release of held requests skipped (non-fatal): %s", _rel_err)

    # Memory refactor follow-up: fire memory.config.push so
    # central tracks the change (config_versions.sequence bump +
    # platform_audit_chain row). Fire-and-forget — local write already
    # succeeded; central tracking is best-effort. The bridge will push
    # the value back down as a noop (existing_seq >= incoming).
    asyncio.create_task(_report_config_change_to_central(
        f"behavioral_params.model_routing.{body.role}",
        routing[body.role],
    ))
    # The history the revert rule reads (v158): who set it, and from which
    # decision if a proposal was approved.
    await _log_route_apply(pool, eid, body.role, entry, set_by, decision_id, rejected)

    out = {"set": True, "role": body.role, "provider": provider, "model": model,
           "set_by": set_by}
    if entry.get("reasoning_inline"):
        out["reasoning_inline"] = True
    if fallback_provider:
        out["fallback_provider"] = fallback_provider
        if fallback_model:
            out["fallback_model"] = fallback_model
    if defaulted_local:
        out["defaulted_local"] = True
        out["bastion"] = defaulted_local
        if effort_dropped:
            out["effort_dropped"] = True
    return out


async def _report_config_change_to_central(field_path: str, value) -> None:
    """Best-effort report to bridge so central audit + config_versions
    track the change. Swallows all failures; the local write is the
    source of truth post-Ship-E."""
    try:
        import server.main as server_main
        client = server_main.get_bridge_client()
        if client is None:
            return
        eid = brain_service._entity_id
        await client.request("memory.config.push", {
            "target_entity_id": eid,
            "field_path": field_path,
            "value": value,
        })
    except Exception as e:
        # Don't surface — local write already succeeded, this is audit-only.
        import logging
        logging.getLogger("cseries.inference").debug(
            "memory.config.push to central failed (non-fatal): %s", e,
        )


@router.delete("/routing/{role}", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def clear_role_model(role: str):
    """Clear a role's route — the role resolves UNCONFIGURED until the next
    pick (there is no default model)."""
    if role not in ROLES:
        raise HTTPException(400, f"Invalid role: {role}")

    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)

    routing = params.get("model_routing", {})
    routing.pop(role, None)
    params["model_routing"] = routing

    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )

    asyncio.create_task(_report_config_change_to_central(
        f"behavioral_params.model_routing.{role}",
        None,
    ))

    return {"cleared": True, "role": role}


@router.put("/coding-harnesses", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def set_coding_harnesses(body: SetCodingHarnesses):
    """Persist the per-engine settings (Orchestrator → Coding Engines): stored
    at behavioral_params.coding_harnesses as a list of {id, model?, effort?}.
    This list is NOT a selection — an entry is simply an
    engine with a saved default model/effort, honoured by build (single and compare)
    and the merge path alike. The Default tick is its own key
    (PUT /coding-default)."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    # Every KNOWN harness is storable — connected or not. The selection is the
    # user's list of builders; connection is checked where it matters, at
    # dispatch (a compare intersects the selection with the connected
    # set, review.py:1844). Gating the SAVE on connection is what made the
    # Parallel Builders ticks "uncheck themselves": the
    # stored list carried codex/copilot/grok from when they were connected,
    # every save round-trips the whole list, so ticking kimi 400'd on 'codex'
    # and the picker reloaded the old state — the tick vanished, silently.
    valid = set(HARNESS_APP_IDS)
    eid = brain_service._entity_id

    async def _read_params() -> dict:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
        )
        p = {}
        if row and row["behavioral_params"]:
            p = row["behavioral_params"]
            if isinstance(p, str):
                p = json.loads(p)
            if not isinstance(p, dict):
                p = {}
        return p

    # This early read serves `stored_ids` ONLY — the write below re-reads,
    # because the validation loop can await a cold provider catalog and
    # harness_state's single-key jsonb_set writers (the 300s materialize
    # loop, revoke tombstones, wiring) may land in between; writing an old
    # snapshot back would clobber them.
    stored_params = await _read_params()
    # Ids ALREADY STORED that are no longer harnesses: a retired harness
    # (removed from HARNESS_APP_IDS) lingers in the stored list until a save
    # rewrites it, and both save paths round-trip the whole list — so a 400
    # on it would wedge every later save with no way to clear it (the picker
    # renders no row for an unknown id). A stale STORED id is DROPPED with a
    # log; an unknown id that was NOT stored is a caller typo and stays a real
    # 400 — that guard is what keeps a guessed builder out of config.
    stored_ids: set[str] = set()
    for h in (stored_params.get("coding_harnesses") or []):
        sid = (h.get("id") if isinstance(h, dict) else h) or ""
        if str(sid).strip():
            stored_ids.add(str(sid).strip())
    cleaned: list[dict] = []
    dropped: list[str] = []
    seen: set[str] = set()
    model_opts: dict | None = None
    for h in body.harnesses:
        hid = (h.id or "").strip()
        if hid not in valid:
            if hid in stored_ids:
                logger.info("dropping stored coding harness %r — no longer a harness", hid)
                dropped.append(hid)
                continue
            raise HTTPException(400, f"Unknown harness: {hid!r}. Known: {sorted(valid)}")
        if hid in seen:
            continue
        seen.add(hid)
        entry: dict = {"id": hid}
        if h.model:
            m = h.model.strip()
            # No typed model names: a model override must be one this harness's
            # provider actually offers (400 on anything else, so a guess can
            # never land in config and fail later when the dispatch fires).
            # Checked against the coding-matrix list — the one the picker
            # rendered — never a different derivation of "the vendor's models".
            if model_opts is None:
                model_opts = await harness_model_options()
            err = validate_harness_model(hid, m, model_opts)
            if err:
                raise HTTPException(400, err)
            entry["model"] = m
        if h.effort:
            e = h.effort.strip().lower()
            # A harness with NO declared efforts silently drops the value
            # rather than 400ing. Rejecting would wedge the caller: both save
            # paths round-trip the full {id, model, effort} entry, and the UI
            # renders no effort control for such a harness — so a legacy value
            # (only reachable via a direct PUT) would fail EVERY later save
            # with no way to clear it. Dropping matches what the dispatch does
            # with an unusable effort anyway.
            if hid in HARNESS_EFFORTS:
                # It DOES take efforts: a wrong one is a real 400, same
                # contract as the model override. Checked against the model
                # saved on the same row when the catalog is per model (codex).
                err = validate_harness_effort(hid, e, model=entry.get("model"))
                if err:
                    raise HTTPException(400, err)
                entry["effort"] = e
            else:
                logger.info("dropping effort %r on %s — it has no effort control", e, hid)
        cleaned.append(entry)

    # Fresh read immediately before the whole-json write (see above).
    params = await _read_params()
    params["coding_harnesses"] = cleaned
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )
    asyncio.create_task(_report_config_change_to_central(
        "behavioral_params.coding_harnesses", cleaned,
    ))
    return {"set": True, "coding_harnesses": cleaned, "dropped": dropped}


@router.put("/coding-default", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def set_coding_default(body: SetCodingDefault):
    """The Default tick in Orchestrator → Coding Engines: the ONE engine a
    build runs on when nothing names one (review.resolve_build_target). Any
    KNOWN engine is storable, connected or not — connection is checked at
    build time, where a ticked-but-disconnected default is a refusal that
    names it (never a silent swap). null clears the tick."""
    from server.network.harness_state import set_default_coding_harness
    h = (body.harness or "").strip() or None
    if h is not None and h not in HARNESS_APP_IDS:
        raise HTTPException(400, f"Unknown engine: {h!r}. Known: {sorted(HARNESS_APP_IDS)}")
    if not await set_default_coding_harness(h):
        raise HTTPException(503, "could not store the default engine")
    asyncio.create_task(_report_config_change_to_central(
        "behavioral_params.default_coding_harness", h,
    ))
    return {"set": True, "default_coding_harness": h}


# ── Voice ─────────────────────────────────────────────────────────────────
# Talking to your host out loud. This is a PROVIDER choice and nothing else:
# realtime.py's mint calls the provider's own ephemeral-token endpoint and the
# MODEL is chosen by the browser app when it opens the socket — so there is no
# {provider, model} pair here, and this is deliberately NOT a ROLES entry.
# set_role_model refuses a modelless route ("model is required — there is no
# default model"), and inventing a model id to satisfy it would be exactly the
# kind of unverifiable pin the no-pins doctrine forbids. Same precedent as
# embedding: a capability whose shape isn't provider+model gets its own flow.
class SetVoiceProvider(BaseModel):
    provider: str | None = None


@router.get("/voice", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_voice_provider():
    """The realtime-voice provider this host speaks through, plus the ones it
    COULD use and whether a key is held for each. `provider` is null when
    nothing is chosen — an unset voice lane is a normal state, reported as
    such rather than silently defaulted."""
    from server.routes.realtime import available_voice_providers, stored_voice_provider
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")
    eid = brain_service._entity_id
    return {
        "provider": await stored_voice_provider(pool, eid),
        "options": await available_voice_providers(pool, eid),
    }


@router.put("/voice", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def set_voice_provider(body: SetVoiceProvider):
    """Choose the realtime-voice provider. null/"" clears it. The provider must
    be one realtime.py can actually mint against (a built-in entry or an
    installed realtime_voice manifest) — an unmintable pick is refused here
    rather than stored and discovered when the user first tries to speak."""
    from server.routes.realtime import _resolve_provider, available_voice_providers
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")
    eid = brain_service._entity_id
    p = (body.provider or "").strip().lower() or None
    if p is not None:
        options = await available_voice_providers(pool, eid)
        opt = next((o for o in options if o["provider"] == p), None)
        if opt is None or _resolve_provider(p) is None:
            raise HTTPException(
                400, f"{p!r} can't serve realtime voice — no built-in entry and no "
                     f"installed realtime_voice manifest declaring it. "
                     f"Known: {[o['provider'] for o in options]}")
        # Key possession is checked HERE too, not just in the picker: the UI
        # hides keyless options, but the route is callable directly and
        # storing a keyless provider only defers the failure to the moment the
        # user first tries to speak — a 400 they cannot connect to this
        # choice. Refusing now is what the docstring promises.
        if not opt.get("has_key"):
            raise HTTPException(
                400, f"no {p} key stored — add one in the Keys app first, then "
                     f"choose {p} for voice")

    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)
    if p is None:
        params.pop("voice_provider", None)
    else:
        params["voice_provider"] = p
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )
    asyncio.create_task(_report_config_change_to_central(
        "behavioral_params.voice_provider", p,
    ))
    return {"set": True, "provider": p}


@router.get("/billing", dependencies=[Depends(requires_permission("read:billing"))])
async def get_billing_mode():
    """Get current billing mode. There is no platform key — every
    token runs on the user's own provider account."""
    from mastercode.inference_config import BILLING_MODE
    return {
        "mode": BILLING_MODE,
        "openrouter_key_set": bool(os.environ.get("OPENROUTER_API_KEY")),
    }


# Provider account balances — the Balances tab's data. The
# inert billing-mode switch is gone from the UI: there is no platform inference
# billing, so the only money question is what each of the USER's provider
# accounts holds. Only OpenRouter and Kimi publish a balance over their API
# (verified live 2026-08-06: /credits 401-exists, /users/me/balance 401-exists;
# DeepInfra/Together/Fireworks/Groq balance paths all 404 — no endpoint, and
# xAI/OpenAI/Anthropic expose billing to console/admin surfaces only), so other
# rows are marked unsupported instead of inventing numbers.
_BALANCES_CACHE: dict = {}
_BALANCES_TTL = 120.0


@router.get("/balances", dependencies=[Depends(requires_permission("read:billing"))])
async def get_provider_balances(fresh: bool = False):
    """Per-provider account balance under the user's stored Keys credential.
    Rows: {provider, key, deactivated?, supported, balance?, currency?,
    detail?, error?}. `key: false` = nothing stored (or switched off) — the UI
    points at the Keys app; `supported: false` = the provider exposes no
    balance endpoint. Rows are cached 120s; fresh=1 bypasses (the tab's
    Refresh button).

    A provider whose model calls run on a host-run sign-in (xai) reports that
    sign-in whenever one exists: the row reads as HELD (key: true, signin:
    true, supported: false, a detail naming the sign-in, and saying so when it
    has expired). Its calls run on the sign-in and never on the API key, so
    'no key — add it in the Keys app' would send the user after a per-token
    key the Host would not use, and the key is not read here at all. A plan
    has no balance to read; none is invented."""
    from server.vault.providers import (
        PROVIDERS, resolve, resolve_signin, signin_serves_inference,
        credential_label)
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    now = time.monotonic()

    def _http_json(url: str, key: str) -> dict:
        import urllib.request
        proxies = urllib.request.getproxies()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {key}", "User-Agent": "cseries-kernel/0.13"})
        return json.loads(opener.open(req, timeout=10).read())

    out: list[dict] = []
    for p in PROVIDERS:
        cached = None if fresh else _BALANCES_CACHE.get(p)
        if cached and cached["exp"] > now:
            out.append(cached["row"])
            continue
        row: dict = {"provider": p, "key": False, "supported": False}
        if pool and signin_serves_inference(p):
            try:
                s = await resolve_signin(pool, eid, p, actor="orchestrator-balances")
            except Exception:  # noqa: BLE001 — never raises by contract; belt
                s = None
            if s is not None:
                label = credential_label(p, "signin")
                row.update({
                    "key": True, "signin": True, "supported": False,
                    "detail": (f"{label} — your plan has no balance to read"
                               if s.secret else
                               f"{label} has expired and has not been renewed"
                               if s.source == "signin-expired" else
                               f"{label} could not be read")})
                _BALANCES_CACHE[p] = {"row": row, "exp": now + _BALANCES_TTL}
                out.append(row)
                continue
        cred = None
        if pool:
            try:
                cred = await resolve(pool, eid, p, ("api_key",),
                                     actor="orchestrator-balances")
            except Exception:
                cred = None
        if cred and cred.source == "deactivated":
            row["deactivated"] = True
        elif cred and cred.secret:
            row["key"] = True
            base = PROVIDERS[p]["base_url"].rstrip("/")
            try:
                if p == "openrouter":
                    body = await asyncio.to_thread(
                        _http_json, base + "/credits", cred.secret)
                    d = (body or {}).get("data") or {}
                    total = float(d.get("total_credits") or 0.0)
                    used = float(d.get("total_usage") or 0.0)
                    row.update({"supported": True,
                                "balance": round(total - used, 2),
                                "currency": "USD",
                                "detail": f"{total:.2f} credits − {used:.2f} used"})
                elif p == "kimi":
                    from server.vault.providers import (
                        is_subscription_base, resolve_base_url)
                    kbase = await resolve_base_url(p, cred.secret) or base
                    if is_subscription_base(kbase):
                        # Kimi-for-Coding: a flat plan with no balance
                        # endpoint — say so instead of dialing a 401.
                        row.update({"supported": False,
                                    "detail": "subscription plan"})
                    else:
                        body = await asyncio.to_thread(
                            _http_json, kbase + "/users/me/balance", cred.secret)
                        d = (body or {}).get("data") or {}
                        avail = float(d.get("available_balance") or 0.0)
                        cash = float(d.get("cash_balance") or 0.0)
                        voucher = float(d.get("voucher_balance") or 0.0)
                        # Moonshot balances are USD on the .ai platform, CNY
                        # on .cn — label by the base actually dialed.
                        row.update({"supported": True, "balance": round(avail, 2),
                                    "currency": "CNY" if ".cn" in kbase else "USD",
                                    "detail": f"cash {cash:.2f} + voucher {voucher:.2f}"})
                # every other registry provider: supported stays False.
            except Exception as e:
                logger.info("balance fetch for %s failed: %s", p, e)
                import urllib.error
                if isinstance(e, urllib.error.HTTPError):
                    detail = ""
                    try:
                        body = json.loads(e.read() or b"{}")
                        err = body.get("error") or {}
                        detail = err.get("message") if isinstance(err, dict) else str(err)
                    except Exception:
                        detail = ""
                    row.update({"supported": True,
                                "error": f"HTTP {e.code}" + (f" — {str(detail)[:120]}" if detail else "")})
                else:
                    row.update({"supported": True, "error": "balance unreachable"})
        _BALANCES_CACHE[p] = {"row": row, "exp": now + _BALANCES_TTL}
        out.append(row)
    return {"balances": out}


@router.put("/billing", dependencies=[Depends(requires_permission("write:billing"))])
async def set_billing_mode(body: SetBillingMode):
    """Switch billing mode: privatae, self, or local."""
    if body.mode not in ("privatae", "self", "local"):
        raise HTTPException(400, f"Invalid mode: {body.mode}. Valid: privatae, self, local")

    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
    )
    params = {}
    if row and row["behavioral_params"]:
        params = row["behavioral_params"]
        if isinstance(params, str):
            params = json.loads(params)

    params["billing_mode"] = body.mode

    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(params), eid,
    )

    return {"mode": body.mode}


# The Orchestrator BYOK card is gone: provider keys live ONLY in
# the Keys app (server/vault). The pre-vault plaintext migration stays in
# provider_gateway._load_user_openrouter_key (openrouter-specific, one-time).


@router.get("/budget", dependencies=[Depends(requires_permission("read:inference_log"))])
async def get_inference_budget(hours: int = 24):
    """Local inference-cost rollup for the Budget app.

    Reads inference_cost_log (written per model call by the provider gateway's
    _log_inference, and per shape activation by synaptive_handler) and returns:
      - totals: today_usd, yesterday_usd, month_to_date_usd, calls_today
      - by_module[]: source_module, calls, tokens_in, tokens_out, cost_usd  (last N hours)
      - by_model[]:  model_id,      calls, tokens_in, tokens_out, cost_usd  (last N hours)

    This is not a platform bill (the platform bills compute only). It's the
    container-local per-call cost log so the entity can see its own spend.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")
    eid = brain_service._entity_id

    hours = max(1, min(int(hours), 24 * 31))
    totals_row = await pool.fetchrow(
        """
        SELECT
            COALESCE(SUM(est_cost_usd) FILTER (
                WHERE created_at >= date_trunc('day', now())
            ), 0)::float8 AS today_usd,
            COALESCE(SUM(est_cost_usd) FILTER (
                WHERE created_at >= date_trunc('day', now() - interval '1 day')
                  AND created_at <  date_trunc('day', now())
            ), 0)::float8 AS yesterday_usd,
            COALESCE(SUM(est_cost_usd) FILTER (
                WHERE created_at >= date_trunc('month', now())
            ), 0)::float8 AS month_to_date_usd,
            COALESCE(COUNT(*) FILTER (
                WHERE created_at >= date_trunc('day', now())
            ), 0)::bigint AS calls_today
        FROM inference_cost_log
        WHERE entity_id = $1
          -- Exclude ONLY the new failure rows (purpose='inference_error',
          -- provider_gateway._log_call_failure): they carry zero dollars, but
          -- calls_today is a COUNT(*), so a provider outage would otherwise
          -- read as a busy day.
          -- NOT `purpose = 'inference'`: this ledger is multi-purpose.
          -- synaptive_handler.py:220-221 writes purpose='shape' rows that this
          -- endpoint is documented to include ("per shape activation by
          -- synaptive_handler", in the docstring above), and retired pipelines
          -- left other values behind. An equality filter silently dropped the
          -- synaptive lane out of by_module. IS DISTINCT FROM is also
          -- NULL-safe, and `purpose` is nullable (init-db.sql:1420).
          AND purpose IS DISTINCT FROM 'inference_error'
        """,
        eid,
    )
    by_module = await pool.fetch(
        """
        SELECT source_module,
               COUNT(*)::bigint            AS calls,
               COALESCE(SUM(tokens_in), 0)::bigint  AS tokens_in,
               COALESCE(SUM(tokens_out), 0)::bigint AS tokens_out,
               COALESCE(SUM(est_cost_usd), 0)::float8 AS cost_usd
        FROM inference_cost_log
        WHERE entity_id = $1
          AND created_at >= now() - ($2::text || ' hours')::interval
          AND purpose IS DISTINCT FROM 'inference_error'
        GROUP BY source_module
        ORDER BY cost_usd DESC, calls DESC
        """,
        eid, str(hours),
    )
    by_model = await pool.fetch(
        """
        SELECT model_id,
               COUNT(*)::bigint            AS calls,
               COALESCE(SUM(tokens_in), 0)::bigint  AS tokens_in,
               COALESCE(SUM(tokens_out), 0)::bigint AS tokens_out,
               COALESCE(SUM(est_cost_usd), 0)::float8 AS cost_usd
        FROM inference_cost_log
        WHERE entity_id = $1
          AND created_at >= now() - ($2::text || ' hours')::interval
          AND purpose IS DISTINCT FROM 'inference_error'
        GROUP BY model_id
        ORDER BY cost_usd DESC, calls DESC
        """,
        eid, str(hours),
    )
    return {
        "totals": dict(totals_row) if totals_row else {
            "today_usd": 0.0, "yesterday_usd": 0.0,
            "month_to_date_usd": 0.0, "calls_today": 0,
        },
        "by_module": [dict(r) for r in by_module],
        "by_model":  [dict(r) for r in by_model],
        "window_hours": hours,
    }


# ── Background activity feed ────────────────────────────────────────────────
#
# What the Host did that the user never saw. The chat turn writes its own
# source_module (`entity_runtime`, runtime/tools.py:8196) and already has a
# status line on screen, so it is excluded here — everything else is work the
# user is otherwise blind to.
#
# Plain-language labels, never the raw module name: "turn_consolidator" is an
# implementation detail, "Memory · turning conversations into memories" is what
# the user is owed. An unrecognised source is TITLED, never hidden — a lane
# nobody labelled is exactly the lane worth seeing.
_ACTIVITY_LABELS = {
    # The owner's Training Memory run (brain/training_memory/worker.py): the
    # classifier batch over imported sessions, one row per finished batch.
    "training_memory":     ("Memory",   "Classifying sessions for Training Memory"),
    # The owner's Test button on a model route (test_role_model below).
    "role_test":           ("Settings", "Testing a model route"),
    "diff_review":         ("Review",   "Reviewing a change"),
    "image_look":          ("Pictures", "Looking at a picture"),
    # Writes purpose='shape' rows per turn (synaptive_handler.py:216-227) —
    # embedding + classifying what was just said. Named, because the fallback
    # would render it "Background · Synaptive handler", which tells the user
    # nothing.
    "synaptive_handler":   ("Memory",   "Reading a conversation turn"),
    "brain_synthesis":     ("Thinking", "Connecting what it knows"),
    "web_search":          ("Research", "Searching the web"),
    "deep_research":       ("Research", "Reading sources in depth"),
    "security_scan":       ("Security", "Checking for issues"),
    # runtime/scoped_agent.AGENT_SOURCE_MODULE: a dispatched agent's calls.
    "scoped_agent":        ("Agents",   "An agent the Host dispatched, at work"),
}
_CHAT_SOURCE_MODULE = "entity_runtime"


def _col(row, key):
    """A column of an activity row, None when the row does not carry it."""
    try:
        return row[key] if key in row.keys() else None
    except Exception:  # noqa: BLE001
        return None


def _activity_label(source_module: str) -> tuple[str, str]:
    """(group, detail) for a source_module. Unknown lanes get a readable
    fallback rather than being dropped."""
    known = _ACTIVITY_LABELS.get(source_module)
    if known:
        return known
    pretty = (source_module or "unknown").replace("_", " ").replace(".", " · ")
    return ("Background", pretty[:1].upper() + pretty[1:])


@router.get("/activity",
            dependencies=[Depends(requires_permission("read:inference_log"))])
async def get_background_activity(limit: int = 20, hours: int = 24, failed: bool = False,
                                  include_turns: bool = False):
    """Background model calls — what the Host ran that the user never saw.

    Every entry carries its cost (`cost_usd`, the list-price estimate the
    gateway logged; null when the route publishes no rate). `include_turns`
    adds the calls of the Host's own turns (source_module entity_runtime),
    so the feed can show every model call the Host made, not only the ones
    outside a turn.

    Reads BOTH halves of inference_cost_log: completed calls
    (purpose='inference', written by the gateway's _log_inference) and failed
    ones (purpose='inference_error', written by _log_call_failure). Before that
    second writer existed a failure wrote nothing anywhere, so this feed could
    only ever have shown successes — the opposite of what it is for.

    `issues` counts failures in the window so the UI can badge without a
    second call.

    `failed=true` lists only the failures in the window (the same rows,
    newest first). Without it a failure followed by `limit` calls that went
    well is counted in `issues` and never listed, so a panel that says what
    failed could not say it. Absent, the answer is as before.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")
    eid = brain_service._entity_id

    limit = max(1, min(int(limit), 100))
    hours = max(1, min(int(hours), 24 * 7))

    rows = await pool.fetch(
        """
        SELECT id, source_module, model_id, provider, purpose,
               tokens_in, tokens_out, latency_ms, metadata, created_at, est_cost_usd,
               cost_method
        FROM inference_cost_log
        WHERE entity_id = $1
          AND ($6::boolean OR source_module IS DISTINCT FROM $2)
          AND created_at >= now() - ($3::text || ' hours')::interval
          AND (NOT $5::boolean OR purpose = 'inference_error')
        ORDER BY created_at DESC, id DESC
        LIMIT $4
        """,
        eid, _CHAT_SOURCE_MODULE, str(hours), limit, bool(failed), bool(include_turns),
    )
    issues = await pool.fetchval(
        """
        SELECT COUNT(*)::bigint FROM inference_cost_log
        WHERE entity_id = $1
          AND source_module IS DISTINCT FROM $2
          AND created_at >= now() - ($3::text || ' hours')::interval
          AND purpose = 'inference_error'
        """,
        eid, _CHAT_SOURCE_MODULE, str(hours),
    )

    out = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        meta = meta or {}
        group, detail = (("Turn", "Your Host's own turn") if r["source_module"] == _CHAT_SOURCE_MODULE
                         else _activity_label(r["source_module"]))
        # A dispatched agent's row carries its task id (scoped_agent
        # _log_meta), so the line names the agent the Agents app lists.
        _task_id = meta.get("task_id")
        if (r["source_module"] == "scoped_agent" and isinstance(_task_id, int)
                and not isinstance(_task_id, bool)):
            detail = f"Agent {_task_id}, at work"
        else:
            _task_id = None
        ok = r["purpose"] != "inference_error"
        out.append({
            "id": r["id"],
            "at": r["created_at"].isoformat() if r["created_at"] else None,
            "group": group,
            "detail": detail,
            "source_module": r["source_module"],
            "task_id": _task_id,
            "model": r["model_id"],
            "provider": r["provider"],
            "tokens_in": int(r["tokens_in"] or 0),
            "tokens_out": int(r["tokens_out"] or 0),
            "latency_ms": int(r["latency_ms"] or 0),
            # The list-price estimate the gateway logged, and what kind of
            # figure it is (cost_method: 'list' priced at published rates;
            # 'subscription', 'local' or 'unpriced' have no per-call dollar
            # figure, so cost_usd is None, never a $0 that reads as free).
            "cost_usd": (round(float(_col(r, "est_cost_usd") or 0.0), 6)
                         if _col(r, "cost_method") == "list" else None),
            "cost_basis": _col(r, "cost_method"),
            "ok": ok,
            # Surfaced verbatim, never prettified into a shrug: an
            # unconfigured role and a provider outage need different actions.
            "error": None if ok else (meta.get("error") or "failed"),
            "error_class": None if ok else meta.get("error_class"),
            "failed_over_to": meta.get("failed_over_to"),
        })

    return {
        "activity": out,
        "issues": int(issues or 0),
        "window_hours": hours,
    }


@router.get("/test/{role}", dependencies=[Depends(requires_permission("execute:inference"))])
async def test_role_model(role: str):
    """Quick test — send a simple prompt to the model this role resolves to."""
    if role not in ROLES:
        raise HTTPException(400, f"Invalid role: {role}")

    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "Brain not initialized")

    if role == "embedding":
        # Embedding embeds, it doesn't chat — there is no chat prompt to fire.
        # Its config/store live at /api/embedding/*.
        return {"role": role, "status": "error",
                "error": "the embedding role has no chat test — see /api/embedding/current"}

    if role in ("image", "image_gen"):
        # Same reason (when these became settable): an image model
        # answers /images/generations, not a bare text
        # prompt. Firing one of the test_prompts below at it would bill the
        # user for a call that proves nothing and report a red that isn't real.
        return {"role": role, "status": "error",
                "error": f"the {role} role has no chat test — it is exercised by "
                         "asking your host for an image"}

    from mastercode.inference_config import resolve_role_route
    eid = brain_service._entity_id
    route = await resolve_role_route(pool, eid, role)
    provider = route.get("provider") or ""
    model = route.get("model") or ""
    if not provider or not model:
        # Unconfigured is a normal state post-cut — surface the
        # resolver's reason, never a KeyError on a stored shape.
        return {"role": role, "status": "unconfigured",
                "error": route.get("unconfigured")
                         or f"no {role} route — pick a provider and model in the Orchestrator"}

    test_prompts = {
        "chat": "Say hello in one sentence.",
        "plan": "List 3 steps to build a REST API. One sentence each.",
        "research": "What is the capital of France? One sentence.",
    }

    try:
        from mastercode.provider_gateway import call_model

        t0 = time.time()
        result = await call_model(
            [{"role": "user", "content": test_prompts.get(role, "Say hi.")}],
            temperature=0.3, max_tokens=100,
            provider=provider, provider_model=model,
            base_url=route.get("base_url"),
            _source="role_test",
        )
        elapsed = int((time.time() - t0) * 1000)

        return {
            "role": role, "provider": provider, "model": model,
            "response": (result.get("content") or "")[:200],
            "elapsed_ms": elapsed, "status": "ok" if not result.get("error") else "error",
            "error": result.get("error"),
        }
    except Exception as e:
        return {"role": role, "provider": provider, "model": model, "error": str(e), "status": "error"}
