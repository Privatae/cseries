"""
Inference configuration — provider/model routing, the Orchestrator's backend.

Privatae does NOT proxy or bill inference (compute-only billing). Every token
runs on the USER's OWN provider account. There are no "lanes": a role's route
is a {provider, model} pair where

  provider — an id in the canonical Keys registry (server.vault.providers
             PROVIDERS: openrouter, openai, xai, kimi, anthropic, deepinfra,
             together, fireworks, groq) or "local" (the user's own machines,
             mesh-advertised). The provider is first-class because it ties to
             an ACCOUNT — a key in the Keys app; the same model (Kimi K3,
             Llama, …) is hosted by many providers and only the provider says
             whose key pays.
  model    — that provider's native model id, picked from the user's
             provider-scoped favorites (never typed, never a compile-time
             default).

plus an optional {fallback_provider, fallback_model} the gateway fails over to
when the primary provider errors or the model is unavailable there.

Stored in behavioral_params.model_routing[role]. THE RULES:

  * NO MODEL PINS. There is no default model anywhere in code. An unrouted
    role resolves EMPTY ("pick a provider and model in the Orchestrator") —
    surfaced, never a silent compile-time model.
  * The Claude Agent SDK is NOT a production option. 'claude-sdk' /
    'subscription' are not providers; stored routes naming them are dropped
    on read (the hard cut) and the write surface refuses them. Users may
    install the SDK themselves and build their own login surface — that is
    their app, not a shipped lane. Anthropic is an ordinary provider here:
    Claude models run on the Anthropic API with the user's own key/token.
  * Claude models NEVER transit OpenRouter — resolution coerces any
    Claude-shaped model pick to provider 'anthropic' (decision:
    anthropic-models-route-through-anthropic-api).

Legacy shapes are migrated on read: {lane, model, direct_provider?} and
{provider, model_id} both normalize through normalize_provider().
"""

import logging
import os

log = logging.getLogger("cseries.mastercode.inference_config")

# openrouter endpoint — always the USER's own key (there is no platform key).
# One base for every OpenRouter call: the chat endpoint below and the model
# list (server/routes/inference.py _build_model_catalog).
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
OPENROUTER_URL = OPENROUTER_BASE_URL + "/chat/completions"

# local endpoint default — overridden per-entity by the route's base_url
# (the user's pod/bastion model server).
LOCAL_URL = os.environ.get("LOCAL_INFERENCE_URL", "http://127.0.0.1:11434/v1/chat/completions")

# ── Transitional shims (importers being migrated off these) ──────────────────
# Inert legacy config: no platform lane exists. BILLING_MODE stays as the
# default for the legacy billing-mode store only; OPENROUTER_API_KEY is the
# env rung of the openrouter credential ladder.
BILLING_MODE = "self"           # legacy; no platform billing exists
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

DEFAULT_BEHAVIORAL_PARAMS = {
    "response_style": "conversational",
}


def _is_claude_model(model: str) -> bool:
    m = (model or "").strip().lower()
    return "claude" in m or m.startswith("anthropic/")


# ── Provider normalization — the ONE place legacy values die ─────────────────
# The SDK lane's hard cut lives here: every stored/thread/legacy value flows
# through normalize_provider() and the subscription aliases come out EMPTY
# (unroutable), which the resolver surfaces as "unconfigured".
_SDK_ALIASES = frozenset({"claude-sdk", "subscription", "claude_subscription"})


def normalize_provider(provider: str, model: str = "") -> tuple[str, str]:
    """Normalize a (provider, model) pair to the canonical routing shape.

    Returns ("", "") for the removed SDK lane and blank input — callers treat
    the empty provider as 'unconfigured'. Legacy aliases map: openrtr/self →
    openrouter; the dead 'privatae' platform lane → anthropic for Claude
    models, openrouter otherwise. Any Claude-shaped model on openrouter is
    coerced to provider 'anthropic' (Claude never transits OpenRouter) with
    the id normalized to the Anthropic-native form.
    """
    p = (provider or "").strip().lower()
    m = (model or "").strip()
    if not p:
        return "", ""
    if p in _SDK_ALIASES:
        return "", ""
    if p in ("openrtr", "self"):
        p = "openrouter"
    elif p == "privatae":
        p = "anthropic" if _is_claude_model(m) else "openrouter"
    if p == "openrouter" and _is_claude_model(m):
        p, m = "anthropic", anthropic_api_model_id(m)
    return p, m


def provider_allowed_for_role(provider: str, role: str) -> bool:
    """True when `provider` names a real, routable provider for `role`.
    Post-cut this is purely 'known provider or local': the SDK aliases
    normalize to empty (refused for EVERY role — the SDK is not a production
    option), and every registry provider may serve any text role. Kept as the
    single invariant point the write surface + smoke gate read."""
    p, _ = normalize_provider(provider, "")
    if not p:
        return False
    if p == "local":
        return True
    try:
        from server.vault.providers import PROVIDERS
        return p in PROVIDERS
    except Exception:  # fail-closed: an unreadable registry refuses everything
        log.warning("provider registry unreadable in provider_allowed_for_role",
                    exc_info=True)
        return False


# Anthropic-native id map — normalizes alias/OpenRouter-form slugs to the id
# the Anthropic API expects (used by the anthropic wire and, while it remains
# in the image, the dormant SDK engine). NOT a routing default: it never picks
# a model, it only rewrites the id a stored pick already named.
_ANTHROPIC_API_ID: dict[str, str] = {
    "anthropic/claude-haiku-4.5":    "claude-haiku-4-5-20251001",
    "claude-haiku-4.5":              "claude-haiku-4-5-20251001",
    # Anthropic's own alias for that snapshot (models overview: "The
    # convenience alias claude-haiku-4-5 resolves to the pinned snapshot
    # claude-haiku-4-5-20251001"). Mapped so the alias and the dated id are one
    # model to everything that compares native ids (the claude-code picker and
    # validator, server/routes/inference.py).
    "claude-haiku-4-5":              "claude-haiku-4-5-20251001",
    "anthropic/claude-haiku-4-5":    "claude-haiku-4-5-20251001",
    "anthropic/claude-haiku-latest": "claude-haiku-4-5-20251001",
    "anthropic/claude-sonnet-4-6":   "claude-sonnet-5",
    "anthropic/claude-sonnet-5":     "claude-sonnet-5",
    "anthropic/claude-sonnet-latest":"claude-sonnet-5",
    "anthropic/claude-opus-4.8":     "claude-opus-4-8",
    "anthropic/claude-opus-5":       "claude-opus-5",
    "claude-opus-5":                 "claude-opus-5",
    # `-latest` follows the current Opus. Moved 4.8 -> 5 on 2026-07-24 (Opus 5
    # release day) and 5 -> 5.5 on 2026-09-27: a user who picked "latest"
    # asked for the current model, and leaving it pinned to a superseded id is
    # the stale-vendor-code problem in a different costume. Anyone who wants
    # 5 or 4.8 specifically still has an explicit id above.
    "anthropic/claude-opus-latest":  "claude-opus-5-5",
    # OpenRouter lists ~anthropic/claude-fable-latest too (2026-09-27) and the
    # Anthropic API has no -latest id, so without this it was sent as
    # claude-fable-latest. Fable 5.1 is the current Fable (the pinned Agent
    # SDK's CLI 2.1.283 names it latest_per_family.fable).
    "anthropic/claude-fable-latest": "claude-fable-5-1",
}


def anthropic_api_model_id(model_id: str) -> str:
    """The Anthropic-native model id for a stored slug (alias/OpenRouter form
    tolerated). A no-op for already-native ids."""
    mid = (model_id or "").strip().lstrip("~")
    api_id = _ANTHROPIC_API_ID.get(mid)
    if api_id:
        return api_id
    mid = mid.split("/", 1)[1] if mid.lower().startswith("anthropic/") else mid
    # OpenRouter spells Claude versions with dots (anthropic/claude-opus-5.5,
    # anthropic/claude-fable-5.1 — openrouter.ai/api/v1/models, 2026-09-27);
    # every Anthropic API id spells them with dashes (claude-opus-5-5). The
    # prefix strip alone sent the dotted form, which is not an Anthropic id.
    return mid.replace(".", "-") if _is_claude_model(mid) else mid


def _route(provider: str, model: str,
           base_url: str | None = None,
           connection_id=None,
           fallback_provider: str | None = None,
           fallback_model: str | None = None,
           effort: str | None = None,
           reasoning_inline: bool = False) -> dict:
    """Normalize a resolution into the full route shape every caller receives.
    `provider` is the registry id (or 'local'); `model` its native id. The
    optional fallback pair is what the gateway fails over to when the primary
    provider errors or the model is unavailable there (fallback_model None =
    retry the same model id on the fallback provider).

    Transitional aliases: 'model_id' == model and 'lane' == provider, so
    callers mid-migration keep reading; new code reads provider/model only.

    `reasoning_inline` (review r13): the route's model writes its reasoning
    INLINE in the reply text and closes it with </think>, with no opening
    tag (a chat template that opens the block in the prompt). The OpenAI-wire
    adapter then holds the stream back until the close
    (provider_gateway._consume_openrouter_stream), so the reasoning is never
    shown live. Set per route by the person (the Orchestrator's routing
    write, PUT /api/inference/routing -> apply_role_route; a Circles thread
    override's model pick); OFF unless set — which models do this is not
    guessed here.
    """
    return {
        "provider": provider,
        "model": model,
        "model_id": model,      # back-compat alias
        "lane": provider,       # back-compat alias (TRANSITIONAL — remove with the 2-tuple callers)
        "base_url": base_url,
        "connection_id": connection_id,
        "fallback_provider": fallback_provider or None,
        "fallback_model": fallback_model or None,
        # Reasoning effort for this role (2026-08-22). None = the provider's
        # own default; the turn path must NOT substitute a literal of its own.
        "effort": effort or None,
        "reasoning_inline": reasoning_inline is True,
    }


def _empty_route(reason: str) -> dict:
    """No servable route — surfaced, never guessed. provider/model are EMPTY
    strings (falsy) so callers degrade to their own configure messaging (the
    image bot asks the user to pick an image model; the chat turn shows a
    configure prompt) instead of firing a mis-routed model."""
    return {
        "provider": "", "model": "",
        "model_id": "", "lane": "",
        "base_url": None, "connection_id": None,
        "fallback_provider": None, "fallback_model": None,
        "reasoning_inline": False,
        "unconfigured": reason,
    }


def _display_route(provider: str, model: str) -> dict:
    """A DISPLAY-ONLY route for stores that are not provider-routed
    (embedding). Keeps the store's own provider taxonomy verbatim so the
    routing surface shows what actually runs."""
    return {
        "provider": provider, "model": model,
        "model_id": model, "lane": provider,
        "base_url": None, "connection_id": None,
        "fallback_provider": None, "fallback_model": None,
    }


# ── Leak guard notes ─────────────────────────────────────────────────────────
# There is no platform lane to leak a prompt to; every provider is the user's
# own account or hardware. The only role restriction left: BYO-endpoint roles
# may pin a base_url, text roles resolve local endpoints live.
_BYO_ALLOWED_ROLES = frozenset({"embedding", "image", "image_gen"})  # BYO-endpoint roles (base_url ok)


# Default model for the training-memory classifier (training_memory/worker.py).
# It dispatches to Anthropic's BYOK BATCH API — the USER's own Anthropic key, not
# a platform vendor lock — but the batch API is Anthropic-only, so the model must
# be a Claude one. Feature-locked, NOT routing: exempt from the no-pins rule by
# construction (the batch API has no provider choice to route).
LOCKED_MODEL_DEFAULTS: dict[str, str] = {
    "training":  "claude-haiku-4-5-20251001",
}


async def _live_local_endpoint(pool, entity_id: int, model: str) -> str | None:
    """The endpoint of the ONLINE machine serving `model` on the host's own
    network RIGHT NOW (network_bastion_heartbeat via model_backends). Local
    routes never store an endpoint — a bastion's mesh address changes on
    re-enrollment, so a write-time pin goes stale, dials a dead address
    and hangs chat. None = nothing serves it, and the gateway surfaces that instead of dialing anything."""
    try:
        from mastercode.model_backends import match_local_model
        hit = await match_local_model(pool, entity_id, model)
    except Exception:
        hit = None
    return hit["endpoint"] if hit else None


def _normalize_fallback(raw_provider, raw_model) -> tuple[str | None, str | None]:
    """The optional failover pair on a stored route. An empty/SDK fallback
    provider silently drops (a stale subscription fallback just disappears);
    fallback_model defaults to None = retry the primary model id."""
    fp = (raw_provider or "").strip().lower()
    if not fp:
        return None, None
    fp, fm = normalize_provider(fp, (raw_model or "").strip())
    if not fp:
        return None, None
    return fp, (fm or None)


def route_effort(route: dict | None) -> str:
    """The reasoning effort to SEND for a resolved route — the user's per-role
    pick, or an explicit "low".

    One rule, one place. The chat turn has had it since 2026-08-22
    (runtime/session.py:3070) and the reason the fallback is an explicit "low"
    rather than None is the same everywhere: with no effort on the wire a
    reasoning-capable model decides for itself how much to think, and on the
    OpenAI wire that thinking is spent out of the SAME max_tokens as the answer
    (provider_gateway.py:1281-1283 sends `reasoning: {"effort": ...}` only when
    given one). In chat that leaked planning prose into the visible reply. In
    the BACKGROUND JSON lanes it ate the whole output budget: measured, the
    consolidation classifier truncated mid-string
    (raw='{"action":"UPDATE","target_id":…') and silently
    defaulted to ADD — duplicate memories; graph extraction logged "no JSON
    object in model reply (2241 chars)" and dead-lettered 12 rows at 5 fails
    each; the thread consolidator returned an empty summary. All three resolved
    the `consolidation` route and DROPPED its effort, so the model picked its own
    (thinking on) and the user's setting (for example `high`) never reached
    the call.

    An unset role therefore keeps exactly today's chat behaviour, and only a
    deliberate pick in the Orchestrator changes it.

    The Anthropic wire never sends this value: it sends chosen_effort() only,
    so the "low" here stays an OpenAI-wire fallback and an unset Anthropic
    route keeps the vendor's own default.
    """
    return ((route or {}).get("effort") or "").strip() or "low"


def chosen_effort(route: dict | None) -> str | None:
    """The effort the PERSON chose for this route (the Orchestrator's stored
    pick, or a chat box's per-thread pick), or None when they chose none.

    route_effort() cannot tell the two apart: it answers "low" for an unset
    route, which is right for the OpenAI wire (see above) and wrong for the
    Anthropic one, where every model has its own documented default and an
    unset route has always sent nothing. Callers that pass route_effort() as
    reasoning_effort pass this as chosen_effort beside it; the gateway's
    Anthropic branch reads only this one."""
    return ((route or {}).get("effort") or "").strip() or None


def thread_effort(thread_override: dict | None) -> str | None:
    """A per-thread effort pick carried on a chat box's thread_override, or
    None. Validated by the route that put it there, by the same per-provider
    and per-model rule as the Orchestrator's write (validate_role_effort)."""
    if not isinstance(thread_override, dict):
        return None
    return str(thread_override.get("effort") or "").strip() or None


# ── Anthropic effort (output_config.effort) ──────────────────────────────────
# The levels the Messages API takes, low to high, from
# https://platform.claude.com/docs/en/build-with-claude/effort ("Effort
# levels", read 2026-10-07). GA, no beta header, sent as
# output_config: {effort: <level>}, never a top-level field.
ANTHROPIC_EFFORTS: tuple = ("low", "medium", "high", "xhigh", "max")

# The level a model runs at when a request names none, from the same page
# (read 2026-10-07): "Most Claude models default to high effort ... Claude
# Opus 5.5 and Claude Haiku 5.5 default to medium", over that page's list of
# the models that support effort. Which levels a model ACCEPTS is not here:
# the Models API states it per model (GET /v1/models, capabilities.effort,
# platform.claude.com/docs/en/api/models/list) and the catalog reads it from
# there (server/routes/inference.py _dm_row). The default is the one fact that
# API does not carry. A model absent here has an unknown default and is shown
# as "default" with no level, never a guessed one.
# Keyed by the id without a date suffix: claude-opus-4-5 also covers
# claude-opus-4-5-20251101, the id that page lists.
# Pinned by scripts/test_anthropic_effort.py.
_ANTHROPIC_DEFAULT_EFFORT: dict[str, str] = {
    "claude-opus-5-5": "medium",
    "claude-haiku-5-5": "medium",
    "claude-fable-5-1": "high",
    "claude-mythos-5-1": "high",
    "claude-fable-5": "high",
    "claude-mythos-5": "high",
    "claude-mythos-preview": "high",
    "claude-opus-5": "high",
    "claude-opus-4-8": "high",
    "claude-opus-4-7": "high",
    "claude-opus-4-6": "high",
    "claude-opus-4-5": "high",
    "claude-sonnet-5-5": "high",
    "claude-sonnet-5": "high",
    "claude-sonnet-4-6": "high",
}


def undated_model_id(model_id: str | None) -> str:
    """claude-opus-4-5-20251101 -> claude-opus-4-5; any other id unchanged."""
    import re as _re
    return _re.sub(r"-\d{8}$", "", (model_id or "").strip())


def anthropic_default_effort(model: str | None) -> str | None:
    """The effort `model` runs at when none is sent, or None when the docs
    table above does not name the model."""
    mid = anthropic_api_model_id(model or "")
    return (_ANTHROPIC_DEFAULT_EFFORT.get(mid)
            or _ANTHROPIC_DEFAULT_EFFORT.get(undated_model_id(mid)))


def anthropic_model_efforts(model: str | None) -> tuple | None:
    """The effort levels `model` accepts, from its row in the Anthropic
    catalog (the Models API's capabilities.effort, read under the user's own
    key): () when the row says the model takes no effort, None when no row
    says anything (a cold catalog, or a model the list does not carry).
    Read-only, no fetch; anthropic_model_efforts_warm fills a cold catalog."""
    try:
        from server.routes.inference import cached_model_efforts
    except Exception:  # noqa: BLE001 — no catalog in this process: unknown
        return None
    return cached_model_efforts("anthropic", anthropic_api_model_id(model or ""))


async def anthropic_model_efforts_warm(model: str | None) -> tuple | None:
    """anthropic_model_efforts, after reading a cold Anthropic catalog: the
    same read the Orchestrator's writer and the chat boxes wait on
    (server/routes/inference.py await_effort_catalog: one read in flight at a
    time, 5 s at most here, and a failed read tried again once its 30 s have
    passed). Not warm_route_catalog: that reads a provider only when it has no
    cache entry at all, so one failed read left every later call without the
    list, and the person's level unsent, for the process's life. Only inside a running server (its brain pool is up): a test or a
    script importing the turn never reaches the network through it."""
    found = anthropic_model_efforts(model)
    if found is None:
        try:
            from server import brain_service
            if brain_service.get_pool() is None:
                return None
            from server.routes.inference import await_effort_catalog
            await await_effort_catalog("anthropic", model, wait_s=5.0)
        except Exception:  # noqa: BLE001 — still unknown; the caller says so
            return None
        found = anthropic_model_efforts(model)
    return found


def anthropic_wire_effort(model: str | None, chosen: str | None) -> tuple:
    """(the level to send as output_config.effort, why a chosen level is not
    sent). The one rule the gateway sends by and the routing writer and the
    chat boxes validate by.

    Nothing chosen: (None, None) and nothing is sent, as before effort was
    wired. A chosen level goes on the wire only where the model's catalog row
    lists it; anything else is (None, reason), and the vendor default applies."""
    eff = (chosen or "").strip().lower()
    if not eff:
        return None, None
    mid = anthropic_api_model_id(model or "")
    if eff not in ANTHROPIC_EFFORTS:
        return None, (f"{eff!r} is not an Anthropic effort level "
                      f"({', '.join(ANTHROPIC_EFFORTS)})")
    accepted = anthropic_model_efforts(mid)
    if accepted is None:
        return None, (f"the Anthropic model list does not say which efforts "
                      f"{mid or 'this model'} accepts (it has not loaded, or "
                      f"does not list the model)")
    if not accepted:
        return None, f"{mid} takes no effort setting"
    if eff not in accepted:
        return None, (f"{mid} does not accept effort {eff!r} "
                      f"(it accepts {', '.join(accepted)})")
    return eff, None


def describe_effort(provider: str | None, model: str | None,
                    chosen: str | None, reasoning_effort: str | None) -> dict:
    """What a call on this route puts on the wire as its reasoning effort:
    {"sent": the level, or None; "default": the level that applies when none
    is sent, when known; "chosen": the person's pick; "why": why a pick was
    not sent}. Never a value that is dropped before the wire — the
    MODEL_ACTUAL line and the turn's record read this.

      anthropic   chosen only, where the model accepts it (anthropic_wire_effort)
      local       nothing: _call_local takes no effort
      any other   reasoning_effort, as _call_remote sends it"""
    p = (provider or "").strip().lower()
    out: dict = {"sent": None, "default": None}
    if chosen:
        out["chosen"] = chosen
    if p == "anthropic":
        sent, why = anthropic_wire_effort(model, chosen)
        out["sent"] = sent
        if not sent:
            out["default"] = anthropic_default_effort(model)
        if why:
            out["why"] = why
        return out
    if p == "local":
        if reasoning_effort:
            out["why"] = "the local wire sends no effort"
        return out
    out["sent"] = (reasoning_effort or "").strip() or None
    return out


def effort_label(desc: dict | None) -> str:
    """describe_effort's answer as one short phrase: "high",
    "default (medium)", "default", with why a pick was not sent."""
    d = desc or {}
    if d.get("sent"):
        return str(d["sent"])
    label = f"default ({d['default']})" if d.get("default") else "default"
    if d.get("chosen") and d.get("why"):
        label += f"; {d['chosen']} not sent: {d['why']}"
    elif d.get("why"):
        label += f" ({d['why']})"
    return label


async def resolve_role_route(
    pool,
    entity_id: int,
    role: str,
    *,
    thread_override: dict | None = None,
    default_provider: str | None = None,
    default_model: str | None = None,
) -> dict:
    """Return the full resolved route {provider, model, fallback_provider,
    fallback_model, base_url, connection_id} (+ model_id/lane aliases) for
    `role`.

    Precedence:
      0. embedding — its OWN store + flow (embedding.py), display-only here.
      1. thread_override {provider|lane, model|model_id} — per-thread pick.
      2. behavioral_params.model_routing[role] — new {provider, model,
         fallback_provider?, fallback_model?} OR legacy {lane, model,
         direct_provider?} / {provider, model_id} (normalized on read).
      3. image roles — never a text-model fallback (an unrouted image role
         would send a text model to an image API).
      4. legacy working config (inference_*) — migrated on read.
      5. caller fallback (default_provider + default_model).
      6. EMPTY — 'pick a provider and model in the Orchestrator'. There is
         no default model anywhere.

    A thread_override that carries an `effort` (a chat box's per-thread
    pick) sets the effort of whichever text route resolves, the stored one
    included: the person can try another effort without picking a model.
    """
    route = await _resolve_role_route(
        pool, entity_id, role, thread_override=thread_override,
        default_provider=default_provider, default_model=default_model)
    _picked = thread_effort(thread_override)
    if (_picked and route.get("model")
            and role not in ("embedding", "image", "image_gen")):
        route = {**route, "effort": _picked}
    return route


async def _resolve_role_route(
    pool,
    entity_id: int,
    role: str,
    *,
    thread_override: dict | None = None,
    default_provider: str | None = None,
    default_model: str | None = None,
) -> dict:
    """resolve_role_route before a per-thread effort pick is applied."""
    import json as _json

    # embedding stays on its OWN store + flow (embedding.py); not a routed
    # role. Read the REAL field so the routing surface shows what actually
    # embeds (display-only: embedding keeps its own provider taxonomy).
    if role == "embedding":
        try:
            _row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id,
            )
        except Exception:
            _row = None
        _bp = (_row and _row["behavioral_params"]) or {}
        if isinstance(_bp, str):
            try:
                _bp = _json.loads(_bp)
            except Exception:
                _bp = {}
        em = (_bp.get("embedding_model") or "").strip() if isinstance(_bp, dict) else ""
        if em:
            _eprov = (_bp.get("embedding_provider") or "").strip().lower()
            # 'local' and registry provider ids (the provider embed lane,
            # 2026-08-10) display as themselves; platform backend names
            # collapse to 'privatae'.
            if _eprov != "local":
                try:
                    from server.vault.providers import PROVIDERS as _PREG
                    _eprov = _eprov if _eprov in _PREG else "privatae"
                except Exception:
                    _eprov = "privatae"
            return _display_route(_eprov, em)
        return _empty_route("embedding not configured (embedding.py owns this store)")

    # 'vision' is no longer a role (images are part of the Host). A picture goes to the Host's own model, so a `vision` route a user
    # saved earlier is never read — not even by a caller that still names it.
    if role == "vision":
        return _empty_route("there is no vision route: pictures go to the Host model "
                            "(the chat route), which must be one that reads images")

    # 1. per-thread override
    if isinstance(thread_override, dict):
        ov_provider, ov_model = normalize_provider(
            thread_override.get("lane") or thread_override.get("provider"),
            thread_override.get("model") or thread_override.get("model_id"),
        )
        if ov_provider and ov_model:
            ov_base = (thread_override.get("base_url") or "").strip() or None
            if ov_provider == "local" and role not in _BYO_ALLOWED_ROLES:
                # Text-role local routes carry no pinned endpoint — live-only.
                ov_base = await _live_local_endpoint(pool, entity_id, ov_model)
            return _route(ov_provider, ov_model, ov_base,
                          thread_override.get("connection_id"),
                          reasoning_inline=thread_override.get("reasoning_inline") is True)

    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id,
        )
    except Exception:
        row = None
    bp: dict = {}
    if row:
        bp = row["behavioral_params"] or {}
        if isinstance(bp, str):
            try:
                bp = _json.loads(bp)
            except Exception:
                bp = {}
        if not isinstance(bp, dict):
            bp = {}

    # 2. explicit per-role assignment. New shape {provider, model, ...}; legacy
    #    {lane, model, direct_provider?} → provider = direct_provider or lane;
    #    legacy {provider, model_id}. SDK-lane entries normalize empty and fall
    #    through (the hard cut — they are NOT migrated to another provider).
    routing = (bp.get("model_routing") or {}).get(role)
    if isinstance(routing, dict):
        raw_provider = (routing.get("provider") or routing.get("lane") or "")
        raw_model = (routing.get("model") or routing.get("model_id") or "")
        if not (routing.get("provider") or "").strip() and routing.get("lane"):
            # legacy lane shape: an explicit direct_provider beats the lane id
            raw_provider = (routing.get("direct_provider") or "").strip() or raw_provider
        provider, model = normalize_provider(raw_provider, raw_model)
        if provider and model:
            base_url = (routing.get("base_url") or "").strip() or None
            if provider == "local" and role not in _BYO_ALLOWED_ROLES:
                # Live-only endpoint: any stored base_url (legacy pin) is
                # IGNORED — the serving machine is looked up in the bastion
                # heartbeats at call time, so a re-enrolled machine's new
                # address is picked up and an offline one surfaces as
                # unavailable instead of a dead-address connect hang.
                base_url = await _live_local_endpoint(pool, entity_id, model)
            fb_provider, fb_model = _normalize_fallback(
                routing.get("fallback_provider"), routing.get("fallback_model"))
            return _route(provider, model, base_url,
                          routing.get("connection_id"),
                          fallback_provider=fb_provider,
                          fallback_model=fb_model,
                          # The user's per-role reasoning effort, stored
                          # beside the route (2026-08-22). Carried verbatim;
                          # it was validated against the provider's accepted
                          # set on write (inference.validate_role_effort).
                          effort=(routing.get("effort") or "").strip() or None,
                          # Set by the person on this route; off otherwise.
                          reasoning_inline=routing.get("reasoning_inline") is True)

    # Image roles have NO default: an unrouted image_gen role returns empty
    # (never a text-model fallback — that would send a text model to an image
    # API). The image bot then asks the user to pick an image model. These are
    # BYO-endpoint roles routed via the user's own provider.
    if role in ("image", "image_gen"):
        return _empty_route(f"no {role} route — pick an image model in the Orchestrator")

    # 4. legacy working config (inference_*) — migrated on read; SDK values
    #    normalize empty and fall through (hard cut).
    inf_provider = (bp.get("inference_provider") or "").strip()
    inf_model = (bp.get("inference_model") or "").strip()
    if inf_provider and inf_model:
        provider, model = normalize_provider(inf_provider, inf_model)
        if provider and model:
            if provider == "local" and role not in _BYO_ALLOWED_ROLES:
                return _route(provider, model,
                              await _live_local_endpoint(pool, entity_id, model))
            return _route(provider, model)

    # 5. caller fallback (default_provider + default_model) — the caller's own
    #    explicit last resort, normalized like everything else.
    if default_provider and default_model:
        provider, model = normalize_provider(default_provider, default_model)
        if provider and model:
            return _route(provider, model)

    # 6. unrouted — surfaced, never guessed. There is NO default model.
    return _empty_route(
        f"no {role} route — pick a provider and model in the Orchestrator")


async def resolve_role_model(
    pool,
    entity_id: int,
    role: str,
    *,
    default_provider: str | None = None,
    default_model: str | None = None,
) -> tuple[str, str]:
    """Return (provider, model) for `role`. Thin 2-tuple wrapper over
    resolve_role_route for stragglers — NOTE it drops base_url/fallback, so
    new callers should take the full route dict instead."""
    route = await resolve_role_route(
        pool, entity_id, role,
        default_provider=default_provider,
        default_model=default_model,
    )
    return route["provider"], route["model"]
