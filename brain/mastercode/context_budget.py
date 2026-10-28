"""Context budgeting for the chat turn path.

Before this module the history window was two blind caps (12 messages /
8000 chars, session.py) and the model's context window — known in the
inference catalog, shown in the Circles model picker — was never read by
the turn path. True overflow surfaced as a provider 4xx error string.

Here:
  - model_context_window: the selected model's context_length, from the
    cached inference catalog (same source as the picker; OpenRouter ids
    AND the direct-models rows the Keys app lists, so first-party routes
    resolve too) with a static Claude fallback; None = unknown → callers
    keep the legacy char caps.
  - derive_turn_budget: window − output_reserve − measured_system − 5%
    safety. That derived ceiling IS the default — the window fills with
    verbatim history until the thread stops fitting (the system that gets
    the most out of a small window should not hold back a large one). A per-thread override
    (the dial) turns depth DOWN; it never raises past the ceiling.
  - trim_messages_to_window: the ONE trim order when over — pre-fire hits
    (lowest-ranked first) → oldest verbatim turns (tool-call GROUPS move
    together — an assistant tool_calls message and its tool results are
    never separated, which is what the provider 400s on) → tool-result
    verbosity (an evicted result is REPLACED by a well-formed truncation
    envelope, never sliced mid-JSON). Identity/constitutional (the system
    block), the current user message and the rolling summary are never
    touched.

Every helper degrades to pre-feature behavior on any failure: unknown
window → None budget → legacy caps; trim failure → untrimmed messages.
"""

import json
import logging
import re

log = logging.getLogger("cseries.mastercode.context_budget")

# chars-per-token estimate for pre-send budgeting (provider-reported
# tokens_in is the post-hoc truth; this is the planning estimate).
CHARS_PER_TOKEN = 4

# The output limit used ONLY when nothing — the catalog, the provider's own
# model list, the static table below, or a provider's own rejection — can say
# what the routed model allows. A call cut there is read from the stop reason
# and reported to the model as cut off, so it can continue in parts — never
# run as `{}`.
DEFAULT_OUTPUT_RESERVE = 16384

# Static output limits for models the cached catalog does not know (catalog
# cold). Same matching as _STATIC_WINDOWS. Source: Anthropic's published
# limits — "every current model reaches 128K except Haiku 4.5, which caps at
# 64K" — and /v1/models measured 2026-09-06 (claude-fable-5-1 max_tokens
# 128000). The catch-all "claude" row is deliberately absent: an older Claude
# with a smaller limit must not be sent 128K, so it falls to the catalog or
# the default.
_STATIC_OUTPUTS: tuple[tuple[str, int], ...] = (
    ("haiku-4-5", 64_000),
    ("fable-5", 128_000),
    ("opus-5", 128_000),
    ("opus-4-6", 128_000),
    ("opus-4-7", 128_000),
    ("opus-4-8", 128_000),
    ("sonnet-5", 128_000),
    ("sonnet-4-6", 128_000),
)

# Route model ids whose provider publishes NO output limit, mapped to the
# OpenRouter catalog row of the same model.
# Kimi's coding endpoint (api.kimi.com/coding/v1, the base a subscription key
# authenticates on) lists its models with `context_length` and a `limit` object
# holding `context` only — measured live: no output
# field on any of its four rows; the platform base answers that key 401. No
# id there matches an OpenRouter id by rule (canonical_model reads kimi "k3"
# as moonshot/k3, OpenRouter's as moonshot/kimi-k3), so the pairing is
# DECLARED here, from the provider's own display names, and pinned by
# scripts/test_model_output_limit.py:
#   k3                        display "K3"                 -> moonshotai/kimi-k3
#   k3-256k                   display "K3-256k" (262,144)  -> moonshotai/kimi-k3
#   kimi-for-coding-highspeed display "K2.7 Code Highspeed" -> moonshotai/kimi-k2.7-code
# The row's output limit is still held to half THIS route's window (k3-256k:
# 131,072), and its window is the provider's own. kimi-for-coding (display
# "K2.8 Preview") has no OpenRouter row, so it is not mapped.
OUTPUT_LIMIT_ALIASES: dict[tuple[str, str], str] = {
    ("kimi", "k3"): "moonshotai/kimi-k3",
    ("kimi", "k3-256k"): "moonshotai/kimi-k3",
    ("kimi", "kimi-for-coding-highspeed"): "moonshotai/kimi-k2.7-code",
}
# The aliased rows' published limits when the OpenRouter catalog is cold, read
# from https://openrouter.ai/api/v1/models top_provider.max_completion_tokens
# on 2026-10-02. A warm catalog always wins (the vendor may move the number).
_STATIC_ALIAS_OUTPUTS: dict[str, int] = {
    "moonshotai/kimi-k3": 943_718,
    "moonshotai/kimi-k2.7-code": 235_929,
}
# The kimi ids' OWN windows, from kimi's coding-endpoint list (measured
# k3 1,048,576, k3-256k 262,144, kimi-for-coding 1,048,576,
# kimi-for-coding-highspeed 262,144) — used when that list is cold, so the
# half-window clamp always has the route's own window (on a cold catalog
# k3-256k's 943,718 would otherwise go out unclamped). Never the aliased
# OpenRouter row's window: k3-256k is the smaller one.
_STATIC_ROUTE_WINDOWS: dict[tuple[str, str], int] = {
    ("kimi", "k3"): 1_048_576,
    ("kimi", "k3-256k"): 262_144,
    ("kimi", "kimi-for-coding"): 1_048_576,
    ("kimi", "kimi-for-coding-highspeed"): 262_144,
}

# Output limits a provider TOLD us, from its own rejection of a larger
# max_tokens (provider_gateway._learned_output_limit) — (provider, model) →
# tokens. Process-local; a fresh pod learns again on its first oversize call.
_LEARNED_OUTPUTS: dict[tuple[str, str], int] = {}


def learn_output_limit(provider: str | None, model: str | None, limit: int) -> None:
    """Record the output limit a provider stated for a model."""
    if model and limit and int(limit) > 0:
        _LEARNED_OUTPUTS[((provider or "").strip().lower(), str(model))] = int(limit)


def model_output_limit(model: str | None, provider: str | None = None) -> int | None:
    """The model's own published output limit in tokens, or None when unknown.

    Source order: what the provider itself told us (a rejected oversize call),
    the cached inference catalog (OpenRouter's top_provider.max_completion_
    tokens and the direct-models rows: Anthropic's /v1/models max_tokens),
    with the same vendor-prefix / suffix matching model_context_window uses,
    then the static table. Never fetches."""
    if not model:
        return None
    learned = _LEARNED_OUTPUTS.get(((provider or "").strip().lower(), str(model)))
    if learned:
        return learned
    try:
        from server.routes.inference import (
            cached_max_output_tokens, openrouter_vendor_prefix,
        )
        table = cached_max_output_tokens()
        v = table.get(model)
        if not v:
            alias = OUTPUT_LIMIT_ALIASES.get(((provider or "").strip().lower(), str(model)))
            if alias:
                v = table.get(alias)
        if not v:
            prefix = openrouter_vendor_prefix(provider)
            if prefix:
                v = table.get(prefix + model)
            if not v:
                low = model.lower()
                dashed = low.replace(".", "-")
                for k, val in table.items():
                    kl = k.lower()
                    if ":" in kl:
                        continue
                    tail = kl.rsplit("/", 1)[-1]
                    if kl.endswith("/" + low) or (
                            "/" in kl and tail.replace(".", "-") == dashed):
                        v = val
                        break
        if v:
            return int(v)
    except Exception as e:
        log.debug("catalog output limit lookup failed (non-fatal): %s", e)
    alias = OUTPUT_LIMIT_ALIASES.get(((provider or "").strip().lower(), str(model)))
    if alias and alias in _STATIC_ALIAS_OUTPUTS:
        return _STATIC_ALIAS_OUTPUTS[alias]
    m = model.lower().replace(".", "-")
    for needle, lim in _STATIC_OUTPUTS:
        if needle in m:
            return lim
    return None


def set_tool_wall_window(window: int | None) -> bool:
    """Tell the tool-result wall and the readers' page sizes the routed
    model's window for the rest of THIS task — the Host turn, or one agent
    run: tool_result_wall.TOOL_WALL_WINDOW, a context variable. Unset, the
    legacy 100 KB wall and its page sizes apply. True when set; False where
    the wall does not take it or the window is unknown. Never raises.

    An UNKNOWN window is set too, as None: a dispatched agent runs in a task that copies the Host turn's
    context, so leaving the variable alone gave an agent whose model has no
    known window the Host's window — a 1M Host's ~1 MB wall and pages for a
    model that may hold a fraction of that, with no trim behind it. Unknown
    means the legacy wall, for this task, whatever its parent held."""
    try:
        from mastercode.runtime import tool_result_wall as _trw
        var = getattr(_trw, "TOOL_WALL_WINDOW", None)
        if var is None:
            return False
        var.set(int(window) if window else None)
        return bool(window)
    except Exception:  # noqa: BLE001
        return False


def wall_kwargs(window: int | None) -> dict:
    """Keyword arguments that size the tool-result transcript wall from the
    active model's window: the `context_window` that
    tool_result_wall._bound_tool_result takes — {} where the wall does not
    take it, so the call works either way."""
    try:
        import inspect
        from mastercode.runtime.tool_result_wall import _bound_tool_result as _b
        if "context_window" in inspect.signature(_b).parameters:
            return {"context_window": window}
    except Exception:  # noqa: BLE001
        pass
    return {}


def reply_reserve(window: int | None) -> int:
    """What history and the mid-turn trim keep free for the reply: a
    realistic reply — DEFAULT_OUTPUT_RESERVE,
    what main has always reserved — held to half the window. NOT the model's
    output limit: subtracting a 128,000-943,718-token limit from the history
    budget halved the history and in-turn context of many routes (k3 961k ->
    479k), against fill-to-fit and the large-data rule. The output
    limit governs the REQUEST (call_output_limit), never the history."""
    return clamp_output_reserve(window, DEFAULT_OUTPUT_RESERVE)


# The least output a call is sent with when the input already fills the window
# (the provider then answers with its own overflow, which the trim handles).
MIN_CALL_OUTPUT = 1024


def call_output_limit(limit: int, window: int | None, messages: list | None,
                      tools: list | None = None) -> int:
    """One call's max_tokens: the output limit, never more than the room the
    window leaves after THIS call's input — min(limit, window − estimated
    input − a margin), at least MIN_CALL_OUTPUT. The margin
    (2% of the window, at least 1,024) covers chars/4 undercounting JSON and
    code. Never below min(limit, 16,384, half the window) — what main always
    sent. Unknown window: the limit, unchanged."""
    if not window or int(window) <= 0 or not limit:
        return limit
    try:
        est = sum(_msg_chars(m) for m in (messages or []) if isinstance(m, dict)) // CHARS_PER_TOKEN
        if tools:
            est += len(json.dumps(tools, default=str)) // CHARS_PER_TOKEN
    except Exception:  # noqa: BLE001
        return limit
    room = int(window) - est - max(1024, int(window) // 50)
    # Floor: what main always sent (16,384), held to the limit and to half
    # the window — an input estimate that runs high must not cut a request
    # below what it always carried; MIN_CALL_OUTPUT only
    # where the window is too small for even that.
    floor = max(MIN_CALL_OUTPUT, min(int(limit), clamp_output_reserve(window, DEFAULT_OUTPUT_RESERVE)))
    return max(1, min(int(limit), max(room, floor)))


_WARNED_NO_LIMIT: set = set()


def output_limit_for(model: str | None, provider: str | None = None,
                     window: int | None = None) -> int:
    """The max_tokens a call to this model should carry:

      - its own published output limit, held to half its window when the
        window is known (clamp_output_reserve — output never takes the floor
        under input);
      - no published limit but a known window: HALF THE WINDOW. A provider
        that allows less refuses the size and
        names its limit; provider_gateway resends at that limit and remembers
        it (learn_output_limit), so the next call sends it first time;
      - neither known: DEFAULT_OUTPUT_RESERVE, with a warning (once per
        model) — the one case 16,384 is still sent, and a call cut there is
        reported to the model as cut."""
    lim = model_output_limit(model, provider)
    if lim:
        return clamp_output_reserve(window, lim)
    if window and int(window) > 0:
        return clamp_output_reserve(window, int(window))
    key = ((provider or "").strip().lower(), str(model or ""))
    if key not in _WARNED_NO_LIMIT:
        _WARNED_NO_LIMIT.add(key)
        log.warning("output limit: neither the output limit nor the window of %s/%s "
                    "is known (catalog, provider list, static table) — sending %d; "
                    "a call cut there is reported to the model as cut",
                    key[0] or "?", key[1] or "?", DEFAULT_OUTPUT_RESERVE)
    return DEFAULT_OUTPUT_RESERVE

# Floor under the history budget — even a pathological override leaves the
# model something to work with (and keeps the consolidator trigger sane).
MIN_HISTORY_TOKENS = 1000

# How coarsely history is dropped once the thread outgrows its budget, as a
# fraction of the budget (4 => quarter-budget blocks).
#
# WHY IT IS NOT 1-TURN-AT-A-TIME: prompt caching is a prefix match. Evicting
# the single oldest turn each time keeps the window maximally full, but it
# moves the FRONT of the prefix every turn, so once a thread reaches the
# fill-to-fit ceiling nothing before the newest turn can ever cache again —
# the exact long-thread case the cache-stable wire shape exists to fix.
# Dropping a coarse block instead leaves headroom, so the same cut point (and
# therefore the same cached prefix) survives many turns; the cost is carrying
# slightly less history than would strictly fit.
TRIM_BLOCK_DIVISOR = 4

# The default history ceiling: 32,000 tokens of verbatim history per call,
# well under every window a Host is routed to (3% of a 1M window, 16% of
# 200k, 25% of 128k), unless the window itself leaves less (the derived
# ceiling below always holds).
#
# WHY THIS NUMBER. The conv-bench LoCoMo sweep (scripts/conv-bench/README.md)
# is the code's one measurement of history depth: on a ~16k-token thread the
# whole verbatim thread scored F1 0.577 against the best curated arm's 0.357,
# a margin far outside its ~0.1 noise floor. That measured the regime where
# the whole thread fits; it measured nothing above it. 32k keeps that winning
# shape — every word verbatim — for any thread up to twice the measured one.
# Above it the oldest turns leave in blocks and their own close_turn notes
# ride in their place (evicted_record), and recall reaches the rest.
# THE TOKEN BUDGET. Fill-to-fit (the default until now) let a long thread's
# history grow to the derived ceiling on every call of every round: about
# 950k tokens per call on a 1M-window route (derive_turn_budget's own
# arithmetic), so up to ~19M for a 20-round turn. At 32k a call's history is
# bounded at 32k tokens and a 20-round turn's at 640k, and a thread's cost
# per message stops growing with its length.
# The owner changes it per Host (behavioral_params.history_ceiling_tokens,
# PUT /api/brain/turn-defaults; 0 or less = fill-to-fit, the old default) and
# per thread (the dial, which may go up to the derived ceiling).
DEFAULT_HISTORY_CEILING_TOKENS = 32_000

# Trim step 3: a tool result larger than this is replaced by a truncation
# envelope (never sliced — host_sdk_engine._bound_tool_result documents why:
# a mid-JSON cut with no marker made the model re-call tools and flail).
TOOL_RESULT_KEEP_CHARS = 2000

# Static window fallbacks for models the cached catalog doesn't know
# (catalog cold, or a provider whose models aren't in the OpenRouter list).
# Substring match on the lowercased model id with dots folded to dashes, so
# a dotted alias and its dashed form ("…-opus-4.8" and "…-opus-4-8") hit the
# same row. FIRST MATCH WINS — the narrow rows come before the catch-all.
#
# These are measured, not assumed. GET /v1/models on the
# platform's own Anthropic key publishes max_input_tokens per model, and
# every current Claude reads 1_000_000 — fable-5/5-1, opus-5/4-8/4-7/4-6,
# sonnet-5/4-6, and sonnet-4-5 too. Only the 4.5-era opus and haiku
# snapshots are 200k. 1M is the DEFAULT on those models (no beta header, no
# opt-in), so the blanket ("claude", 200_000) this replaces was cutting
# 800k off the window of every Claude route the catalog didn't know — which
# is every Anthropic-direct route, since the catalog reads the wrong field
# for them (see _dm_row in server/routes/inference.py). The catch-all stays
# 200k for ids not named here (claude-3-*, a family released after this
# line was written): too small costs history, too large costs the turn.
_STATIC_WINDOWS: tuple[tuple[str, int], ...] = (
    ("haiku", 200_000),
    ("opus-4-5", 200_000),
    ("fable-5", 1_000_000),
    ("opus-5", 1_000_000),
    ("opus-4-6", 1_000_000),
    ("opus-4-7", 1_000_000),
    ("opus-4-8", 1_000_000),
    ("sonnet-5", 1_000_000),
    ("sonnet-4-5", 1_000_000),
    ("sonnet-4-6", 1_000_000),
    ("claude", 200_000),
)

# Prefix marking the history message that carries the Host's own close_turn
# notes on the turns that fell out of the window (evicted_record). The trim
# spares it: it is the densest context in the window, and the only account of
# what fell out.
RECORD_PREFIX = "## Earlier in this conversation (your own end-of-turn notes)"
# ...and the thread-level message (runtime/close_turn.thread_state): the
# thread's goal and the last turn's handoff. Spared by the trim for the same
# reason.
THREAD_STATE_PREFIX = "## Where this thread stands"

# Overflow-shaped provider errors (the 4xx the design's retry-once handler
# catches). Matched case-insensitively against result["error"].
# Every needle is a provider's REAL wording, with its source:
_OVERFLOW_NEEDLES = (
    # OpenRouter/OpenAI-wire: "This endpoint's maximum context length is 262144
    # tokens. However, you requested about 504212 tokens" (dyad-sh/dyad#1558);
    # vLLM: "This model's maximum context length is 32768 tokens"
    # (infiniflow/ragflow#10406).
    "maximum context length",
    # OpenRouter's typed error.metadata.error_type (openrouter.ai/docs/
    # api-reference/errors, read 2026-10-02): "context_length_exceeded" — "The
    # combined input and output tokens exceed the model's context window".
    "context_length_exceeded",
    "context window",
    # Anthropic: "prompt is too long: 203073 tokens > 200000 maximum"
    # (Doriandarko/claude-engineer#224).
    "prompt is too long",
    # Anthropic: "input length and `max_tokens` exceed context limit: 196973 +
    # 8192 > 204648" (OpenHands/OpenHands#7023) — the gateway resends at the
    # room left first; this is for when that still overflows.
    "exceed context limit",
    # kimi: "Invalid request: Your request exceeded model token limit: 262144
    # (requested: 269030)" (MoonshotAI/kimi-cli#2011, BerriAI/litellm#43013)
    # — a WINDOW refusal: the number is the context size.
    "exceeded model token limit",
    # kimi (platform.kimi.ai/docs/api/errors, 400 invalid_request_error):
    # "Input token length too long" (the input alone exceeds the context) and
    # "prompt tokens + max_tokens exceeds the model specification".
    "input token length too long",
    "exceeds the model specification",
    # Gemini: "The input token count (N) exceeds the maximum number of tokens
    # allowed (M)." (agno-agi/agno#10449, use-agent-os/agent-os#657).
    "exceeds the maximum number of tokens allowed",
    # xAI: "This model's maximum prompt length is N but the request contains M
    # tokens." (agno-agi/agno#10449).
    "maximum prompt length is",
    # (Groq's "Please reduce the length of the messages or completion."
    # always comes with the typed code "context_length_exceeded", which
    # decides first — OVERFLOW_CODES — so it has no needle.)
    # vLLM (local routes): "This model's maximum context length is 32768 tokens.
    # However, your request has 32836 input tokens" (infiniflow/ragflow#10406)
    # — matched by "maximum context length" above. Ollama mostly truncates the
    # prompt and answers 200 rather than refusing (ollama/ollama#17944), so
    # there is no Ollama refusal to match.
)

# A refusal's TYPED code that means a window overflow: OpenRouter's
# error.metadata.error_type and the OpenAI-shaped error.code
# "context_length_exceeded" (openrouter.ai/docs/api-reference/errors; Groq's
# body, thepersonalaicompany/amurex-backend#51).
OVERFLOW_CODES = ("context_length_exceeded",)
def refusal_parts(text: str | None) -> tuple[str, str]:
    """(message, typed code) of a provider refusal — from its JSON body, or a
    "<provider> <status>: <body>" error string. The message is the refusal's
    own words only: never request ids or other fields. A body
    that is not JSON (or was cut short) yields its "message" field by pattern,
    else the text itself. The code is error.metadata.error_type, error.code or
    error.type, lowercased ("" when none)."""
    raw = str(text or "")
    body = raw
    m = re.match(r"^\s*[\w\-.]+ \d{3}: (.*)$", raw, re.S)
    if m:
        body = m.group(1)
    msg, code = "", ""
    try:
        obj = json.loads(body)
        # Gemini's OpenAI-compatible endpoint answers a LIST of one error
        # object: [{"error": {"code": 400, "message": …, "status":
        # "INVALID_ARGUMENT"}}] (mealie-recipes/mealie#8551,
        # NousResearch/hermes-agent#125497) — unwrapped.
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            obj = obj[0]
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            code = str(meta.get("error_type") or err.get("code") or err.get("type") or "")
        elif isinstance(obj, dict):
            msg = str(obj.get("message") or (err if isinstance(err, str) else "") or "")
    except (ValueError, TypeError):
        mm = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)', body)
        msg = mm.group(1) if mm else body
        mc = re.search(r'"(?:error_type|code)"\s*:\s*"([a-z_]+)"', body)
        code = mc.group(1) if mc else ""
    return (msg or body), code.lower()





def est_tokens(text: str) -> int:
    """chars//4 pre-send estimate (per-component, not one global guess)."""
    return len(text or "") // CHARS_PER_TOKEN


async def model_context_window(model: str | None, provider: str | None = None) -> int | None:
    """The model's context window in tokens, or None when unknown.

    Source order: the cached inference catalog (the same OpenRouter list
    the model picker shows plus the direct-models rows the Keys app lists;
    server.routes.inference owns the caches — a cold cache yields {} here,
    we never fetch on the chat hot path; server startup warms it), then
    the vendor-prefixed OpenRouter row for a bare first-party id (via
    `provider`, then any vendor's suffix), then the static fallback table.
    None → caller keeps legacy behavior.
    """
    if not model:
        return None
    try:
        from server.routes.inference import (
            cached_context_lengths, openrouter_vendor_prefix,
        )
        table = cached_context_lengths()
        cl = table.get(model)
        if not cl:
            # First-party routes carry the vendor's bare id ("gpt-5.2");
            # the OpenRouter rows are vendor-prefixed ("openai/gpt-5.2").
            # Try the provider's prefix, then any vendor's — same slug
            # rule _enrich_first_party_from_openrouter uses for pricing.
            prefix = openrouter_vendor_prefix(provider)
            if prefix:
                cl = table.get(prefix + model)
            if not cl:
                suffix = "/" + model.lower()
                for k, v in table.items():
                    if k.lower().endswith(suffix) and ":" not in k:
                        cl = v
                        break
        if cl:
            return int(cl)
    except Exception as e:
        log.debug("catalog context_length lookup failed (non-fatal): %s", e)
    rw = _STATIC_ROUTE_WINDOWS.get(((provider or "").strip().lower(), str(model)))
    if rw:
        return rw
    m = model.lower().replace(".", "-")
    for needle, window in _STATIC_WINDOWS:
        if needle in m:
            return window
    return None


def derive_turn_budget(
    *,
    window: int,
    output_reserve: int,
    system_tokens: int,
    override: int | None = None,
    ceiling: int | None = None,
) -> int:
    """The effective per-turn history budget (tokens).

    derived = what the window can carry: (window − output reserve −
    measured system blocks) × 0.95. An override (the per-thread dial) is the
    budget, never above `derived`. Without one, `ceiling` (the Host's default
    history ceiling, read_turn_defaults: DEFAULT_HISTORY_CEILING_TOKENS unless
    the owner changed it) caps `derived`; None or 0 is fill-to-fit, the
    budget IS `derived`."""
    derived = int((int(window) - int(output_reserve) - int(system_tokens)) * 0.95)
    if derived < MIN_HISTORY_TOKENS:
        derived = MIN_HISTORY_TOKENS
    if override:
        try:
            ov = int(override)
        except (TypeError, ValueError):
            ov = 0
        if ov > 0:
            return min(derived, ov)
    if ceiling:
        try:
            cap = int(ceiling)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            return max(min(derived, cap), MIN_HISTORY_TOKENS)
    return derived


async def read_turn_defaults(pool, entity_id: int) -> dict:
    """The Host's bounded defaults, as the owner set them
    (behavioral_params, PUT /api/brain/turn-defaults) over the built-in ones:

      history_ceiling_tokens  the default history ceiling
                              (DEFAULT_HISTORY_CEILING_TOKENS; 0 = fill-to-fit)
      turn_allowance_usd      the default spend per turn in dollars, None for
                              the built-in default below
      scheduled_task_allowance_usd   the same for a scheduled run
      turn_rounds             the built-in default when no dollar figure is
                              set: this many full-context rounds, metered in
                              tokens (turn_allowance.DEFAULT_TURN_ROUNDS;
                              0 = none)
      scheduled_rounds        the same for a scheduled run
                              (turn_allowance.DEFAULT_SCHEDULED_ROUNDS)

    A per-thread dial (read_thread_budget, read_thread_allowance) wins over
    every one of these. Never raises: an unreadable store is the built-ins."""
    from mastercode.runtime.turn_allowance import DEFAULT_SCHEDULED_ROUNDS, DEFAULT_TURN_ROUNDS
    out = {"history_ceiling_tokens": DEFAULT_HISTORY_CEILING_TOKENS,
           "turn_allowance_usd": None, "scheduled_task_allowance_usd": None,
           "turn_rounds": DEFAULT_TURN_ROUNDS, "scheduled_rounds": DEFAULT_SCHEDULED_ROUNDS}
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id)
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            bp = json.loads(bp)
    except Exception as e:  # noqa: BLE001
        log.debug("read_turn_defaults failed (built-ins apply): %s", e)
        return out
    if not isinstance(bp, dict):
        return out
    for key, cast in (("history_ceiling_tokens", int), ("turn_rounds", int),
                      ("scheduled_rounds", int)):
        if bp.get(key) is not None:
            try:
                out[key] = max(0, cast(bp[key]))
            except (TypeError, ValueError):
                pass
    for key in ("turn_allowance_usd", "scheduled_task_allowance_usd"):
        if bp.get(key) is not None:
            try:
                v = float(bp[key])
                out[key] = v if v > 0 else None
            except (TypeError, ValueError):
                pass
    return out


def clamp_output_reserve(window: int | None, max_tokens: int) -> int:
    """The output reserve a request may carry against a known window.

    A caller-default max_tokens (16384) is larger than some models' whole
    window; sending it makes the request invalid before a single history
    token is counted, and the trim target (window − reserve) goes negative.
    Output never takes more than half the window — the other half is the
    floor under input. Unknown window → the caller's value, unchanged."""
    if not window or window <= 0:
        return max_tokens
    return max(1, min(int(max_tokens), int(window) // 2))


def _circles_entry_value(bp: dict, src_ctx: dict, field: str,
                         surface: str | None = None):
    """`field` from the circles per-thread entry that matches this source
    context, or None.

    WHY THIS IS NOT JUST ["self"]. circles keys its entries by
    `_thread_key` (routes/circles.py:471): the self-thread is "self", and
    every other kind is "<kind>:<id>" — "dm:someone@example.com",
    "entity:42". The reader only ever looked at ["self"], so a budget saved
    on any other circles thread was written to a key nothing read: saved,
    invisible, governing nothing. That is fixed here rather than in the
    writer because the orphaned entries already exist in the field.

    The map runs the other way (circles thread -> source context) in
    routes/circles.py:1161 `_thread_src_ctx`: self -> app:circles,
    dm/entity -> peer:<id>, circle -> None. This inverts it — for a peer
    scope, either a dm or an entity entry may hold the value, and the id is
    the ref.

    Reading these is deliberately kept even though the circles PUT now
    refuses to create new ones (see put_thread_context): refusing to write
    a dead value and honouring one already written are not in conflict, and
    dropping the read would silently discard settings people have made.

    THE PEER BRANCH IS GATED ON `surface`, and it has to be. A peer source
    context is {kind: "peer", ref: <peer id>} with nothing in it naming the
    surface, and THREE places build one: circles' own _thread_src_ctx
    (routes/circles.py:1161), memory_engine.derive_source_context (:1042),
    and synaptive_handler (:981) — the inbound host-to-host lane, which is a
    different surface that can carry the SAME ref. Ungated, a legacy
    circles_thread_models["dm:<id>"] entry would supply the budget for a
    synaptive-lane turn with that peer, which nobody set it for. So the
    caller has to say it is the circles surface; the turn path never does,
    and the only caller that does is the circles thread-context panel, which
    is where the value was entered.

    The "self" branch needs no gate: app:circles names the surface itself.
    The same holds for a Circles thread: {kind: "thread"} is
    only ever built for the Circles self-thread, and its entry is keyed
    "thread:<uuid>" by routes/circles.py _thread_key. Without this branch a
    budget or allowance saved on a thread is written and never read on the
    paths that re-read from the DB (replay, and any turn without a
    thread_override).
    """
    entries = bp.get("circles_thread_models") or {}
    if not isinstance(entries, dict):
        return None
    kind, ref = src_ctx.get("kind"), str(src_ctx.get("ref") or "")
    if kind == "app" and ref == "circles":
        keys = ["self"]
    elif kind == "thread" and ref:
        keys = [f"thread:{ref}"]
    elif kind == "peer" and surface == "circles":
        keys = [f"dm:{ref}", f"entity:{ref}"]
    else:
        return None
    for k in keys:
        entry = entries.get(k)
        if isinstance(entry, dict) and entry.get(field) is not None:
            return entry[field]
    return None


async def read_thread_budget(pool, entity_id: int, src_ctx: dict | None,
                             surface: str | None = None) -> int | None:
    """The per-thread context_budget override (tokens), or None.

    Two co-located stores (same lifecycle as the per-thread model pick):
      - behavioral_params.context_budgets["<kind>:<ref>"] — the workbench /
        generic store (PUT /api/brain/thread-context writes here);
      - behavioral_params.circles_thread_models["self"]["context_budget"] —
        the circles self-thread, co-located with its model override (the
        circles send path also forwards it on thread_override, so this DB
        read is the fallback for paths that don't, e.g. replay).
    """
    if not src_ctx or not src_ctx.get("kind"):
        return None
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id,
        )
    except Exception as e:
        log.debug("read_thread_budget fetch failed (non-fatal): %s", e)
        return None
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except Exception:
            bp = {}
    if not isinstance(bp, dict):
        return None
    scope = f"{src_ctx.get('kind')}:{src_ctx.get('ref')}"
    val = (bp.get("context_budgets") or {}).get(scope)
    if val is None:
        val = _circles_entry_value(bp, src_ctx, "context_budget", surface)
    try:
        val = int(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


async def read_thread_allowance(pool, entity_id: int,
                                src_ctx: dict | None,
                                surface: str | None = None) -> float | None:
    """The per-thread turn spend allowance in DOLLARS, or None.

    The money twin of read_thread_budget above, deliberately in the SAME two
    co-located stores and read by the same scope key, because the two dials
    are set together on one panel and a user who moves one expects the other
    to survive:
      - behavioral_params.turn_allowances["<kind>:<ref>"] — workbench/generic
        (PUT /api/brain/thread-context writes here, beside context_budgets);
      - behavioral_params.circles_thread_models["self"]["turn_allowance_usd"]
        — the circles self-thread, beside its context_budget.

    None means "no allowance set": the tool loop runs to the backstop, which
    is what it did before this dial existed. A stored value <= 0 also reads
    as None rather than as "spend nothing" — a zero-dollar turn cannot even
    make the first call, so it can only be a mistake or a stale write, and
    refusing to answer at all is never what the person meant.

    No DDL: behavioral_params is JSONB, so a new key is a write, not a
    migration.
    """
    if not src_ctx or not src_ctx.get("kind"):
        return None
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id,
        )
    except Exception as e:
        log.debug("read_thread_allowance fetch failed (non-fatal): %s", e)
        return None
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except Exception:
            bp = {}
    if not isinstance(bp, dict):
        return None
    scope = f"{src_ctx.get('kind')}:{src_ctx.get('ref')}"
    val = (bp.get("turn_allowances") or {}).get(scope)
    if val is None:
        val = _circles_entry_value(bp, src_ctx, "turn_allowance_usd", surface)
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


async def latest_prompt_blocks(
    pool, entity_id: int, src_ctx: dict | None = None,
) -> list[dict]:
    """The latest prompt_log composition for the entity: the newest
    'static+per_rebuild' row's blocks followed by the newest per-turn
    build's blocks (the row carrying the memory block and the
    memory_prefire entry with its fired items). Injected
    single-block rows (log_injected_block) are skipped for the per-turn
    scope so the panel shows the build, not one injection.

    src_ctx: the {kind, ref} thread descriptor, restricting the
    PER-TURN half to one thread. Everything per-turn is thread-specific — the
    memory block and its memory_prefire items are retrieved for the message
    that was actually sent — and this read had no thread filter at all, so
    with two threads open the panel showed whichever one sent last. The
    static+per_rebuild half stays unfiltered on purpose: it is the cached
    system message and is identical whatever thread is open.

    Passing None reads the table entity-wide, which is what window_report
    (the host's mirror of its OWN last turn, whatever thread it was in) wants.

    A row whose source_context is NULL — every row written before
    migrations/v149_prompt_log_source_context.sql, and every build with no
    thread — is excluded from a filtered read rather than shown: a new thread
    reads EMPTY, never another thread's pre-fire."""
    sql = ("SELECT scope, blocks FROM prompt_log WHERE entity_id=$1"
           " ORDER BY log_id DESC LIMIT 40")
    args: list = [entity_id]
    if isinstance(src_ctx, dict) and src_ctx.get("kind"):
        sql = ("SELECT scope, blocks FROM prompt_log WHERE entity_id=$1"
               " AND (scope <> 'per_turn'"
               "      OR (source_context->>'kind' = $2"
               "          AND source_context->>'ref' = $3))"
               " ORDER BY log_id DESC LIMIT 40")
        args += [str(src_ctx.get("kind")), str(src_ctx.get("ref") or "")]
    try:
        rows = await pool.fetch(sql, *args)
    except Exception as e:
        log.debug("latest_prompt_blocks fetch failed (non-fatal): %s", e)
        return []
    static_blocks: list[dict] | None = None
    per_turn_blocks: list[dict] | None = None
    per_turn_fallback: list[dict] | None = None
    for r in rows:
        blocks = r["blocks"]
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except Exception:
                continue
        if not isinstance(blocks, list):
            continue
        scope = r["scope"] or ""
        # The writer labels a row "+".join(sorted(freshness values))
        # (prompt_schema._build_grouped) — i.e. "per_rebuild+static". This
        # reader compared against "static+per_rebuild" and never matched, so
        # the context viewer showed only the per-turn fallback (one 451-char
        # constitution injection) and none of the ~48k-char system build
        # Match on the parts, order-agnostic.
        parts = set(p for p in scope.split("+") if p)
        if parts == {"static", "per_rebuild"}:
            if static_blocks is None:
                static_blocks = blocks
        elif scope == "per_turn":
            if per_turn_fallback is None:
                per_turn_fallback = blocks
            # NEWEST full per-turn build wins, and finding it is not a reason
            # to stop: the static row is written by build_prompt at session
            # start / on a rebuild, so on a walk backwards from the newest row
            # it is normally BEHIND the per-turn rows. The `break` that used to
            # sit here fired the moment a per-turn build was found and returned
            # before the static row had been reached, dropping the whole cached
            # system half from the panel — the same symptom as the
            # scope-label mismatch above, arriving one line later. The loop
            # already exits as soon as BOTH halves are in hand.
            if per_turn_blocks is None and any(
                    b.get("name") == "memory" for b in blocks
                    if isinstance(b, dict)):
                per_turn_blocks = blocks
        if static_blocks is not None and per_turn_blocks is not None:
            break
    return (static_blocks or []) + (per_turn_blocks or per_turn_fallback or [])


# Window wording that makes an error a context overflow even when it also
# names the output parameter (Anthropic's "input length and `max_tokens`
# exceed context limit: A + B > C"; kimi's "prompt tokens + max_tokens exceeds
# the model specification").
_WINDOW_NEEDLES = ("context", "prompt is too long", "input length", "model specification")


def _is_output_refusal(err: str) -> bool:
    """An OUTPUT-limit refusal: it names the output parameter or limit
    (provider_gateway._OVERSIZE_NEEDLES) and no window wording. The gateway
    reads these first and resends at the stated limit; the turn
    must never take one for a context overflow and cut history for it, even
    when its sentence also says "token limit"."""
    try:
        from mastercode.provider_gateway import _OVERSIZE_NEEDLES as _out
    except Exception:  # noqa: BLE001
        _out = ("max_tokens", "output tokens", "completion tokens")
    return any(n in err for n in _out) and not any(w in err for w in _WINDOW_NEEDLES)


def is_context_overflow(result: dict | None) -> bool:
    """True when an inference result carries a provider context-overflow
    error (the 4xx the retry-once handler exists for) and no content. An
    output-limit refusal is never one (_is_output_refusal: the same order as
    the gateway, output check first)."""
    if not isinstance(result, dict):
        return False
    if result.get("content"):
        return False
    err = str(result.get("error") or "")
    if not err and not result.get("error_body"):
        return False
    # The refusal's own MESSAGE and TYPED CODE — from the full body the
    # gateway keeps (error_body), never a 200-character slice of it nor its
    # ids.
    msg, code = refusal_parts(result.get("error_body") or err)
    low = msg.lower()
    # (No quota guard: with it forced off, every sourced quota
    # and rate refusal still read "not size" and every sourced size refusal
    # was unchanged; its one live effect was a misfire on Gemini's list body.)
    if code in OVERFLOW_CODES:
        return True
    if _is_output_refusal(low):
        return False
    return any(n in low for n in _OVERFLOW_NEEDLES)


def _msg_chars(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, str):
        total = len(c)
    elif isinstance(c, list):
        # Multimodal content parts: text counts as chars; each image/
        # document part carries a large fixed token cost.
        total = 0
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                total += len(p.get("text") or "")
            else:
                total += 4000
    else:
        total = len(str(c or ""))
    # An assistant turn that only carried tool calls has content=None but
    # its arguments still ride the wire (and were the whole point of the
    # message) — count them, or a long tool loop estimates at ~0.
    for tc in m.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        total += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or "")) + 24
    return total


def _is_summary(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, str) and c.startswith((RECORD_PREFIX, THREAD_STATE_PREFIX))


def _tool_groups(messages: list[dict]) -> list[list[int]]:
    """Index groups over everything after the system block: an assistant
    message carrying tool_calls plus the role:"tool" results that answer
    it form ONE group; every other message is a group of one. The trim
    moves groups, never members — an orphaned tool result (or a call with
    no result) is a provider 400 on both wires.

    The FINAL index is included. It was excluded on the
    assumption that messages[-1] is always the current user message; inside
    the tool loop that is false — from round 2 on the list ends with a
    role="tool" result. That result therefore sat outside every group, and
    step 4 could drop the assistant tool_calls message that asked for it,
    leaving an orphan. The loop writes the trimmed list back
    (session.py, `msgs[:] = _pre`), so every later round 400s too.
    """
    groups: list[list[int]] = []
    open_ids: set[str] = set()
    cur: list[int] = []
    for i in range(1, len(messages)):
        m = messages[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            if cur:
                groups.append(cur)
            cur = [i]
            open_ids = {str((tc or {}).get("id") or "") for tc in m["tool_calls"]}
            continue
        if role == "tool" and cur and (
            not m.get("tool_call_id") or str(m.get("tool_call_id")) in open_ids
        ):
            cur.append(i)
            continue
        if cur:
            groups.append(cur)
            cur = []
            open_ids = set()
        groups.append([i])
    if cur:
        groups.append(cur)
    return groups


def _current_user_index(messages: list[dict]) -> int | None:
    """FALLBACK anchor for the person's words, for callers that supply none:
    the newest role="user" message before the first assistant tool_calls
    message — the last thing said before the loop started calling tools —
    else the newest role="user" message anywhere. None only when the list
    holds no user message at all.

    NO CONTENT INSPECTION. A "##" prefix used to disqualify a message here, on
    the theory that the runtime injects role=user blocks. It does not — every
    injected block goes in as role="system" (session.py:2477, :3043, :3169,
    :3204, :3224, :3240, :3548) — so the rule had no true positive and one bad
    false positive: a person who opens with a markdown heading ("## Deploy
    plan\\nwhich services restart?") had their question walked past, an older
    history turn pinned in its place, and the question evicted. An all-"##"
    list protected nothing at all.

    A caller that knows exactly which dicts it appended should pass `protect`
    rather than rely on this.
    """
    first_tc = next((i for i, m in enumerate(messages) if m.get("tool_calls")),
                    len(messages))
    for i in range(min(first_tc, len(messages)) - 1, 0, -1):
        if messages[i].get("role") == "user":
            return i
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].get("role") == "user":
            return i
    return None


def _protect_indices(messages: list[dict], protect: list | None) -> set[int]:
    """Indices of the caller's protected messages, matched by OBJECT IDENTITY.

    Identity — not content, and not a marker key written into the message.
    The runtime holds the very dicts it appended (the turn's user message,
    each mid-turn nudge) and the trim only ever rebuilds the list by filtering
    it, so those objects survive by reference. Nothing is added to any
    message, so there is nothing that can leak onto the wire or into the turn
    row, which a private key like "_q" could.
    """
    if not protect:
        return set()
    keep: set[int] = set()
    for i, m in enumerate(messages):
        if any(m is p for p in protect):
            keep.add(i)
    return keep


def _protected(messages: list[dict], groups: list[list[int]],
               protect: list | None = None) -> set[int]:
    """Indices the trim may never DROP (step 3 may still shrink a tool result
    inside one — verbosity is a pressure valve, presence is not):

      - messages[0], the identity/constitutional system block;
      - every message in `protect` — the person's words, identified by the
        caller that appended them: the turn's question and any mid-turn nudge
        (session.py). The runtime's OWN steering (dead-tool notices, STOP
        directives) is deliberately not in that list and stays evictable;
      - failing a usable `protect`, the fallback anchor (_current_user_index),
        so this never protects nothing;
      - the last message AND, with it, the whole group it belongs to — inside
        the tool loop that is the in-flight exchange (the assistant tool_calls
        message plus the results answering it). Dropping half of it orphans
        the other half; dropping all of it throws away the round the model is
        mid-way through.
    """
    keep = {0}
    if not messages:
        return keep
    last = len(messages) - 1
    keep.add(last)
    anchored = _protect_indices(messages, protect)
    if anchored:
        keep |= anchored
    else:
        cur = _current_user_index(messages)
        if cur is not None:
            keep.add(cur)
    for g in groups:
        if last in g:
            keep.update(g)
            break
    return keep


def _truncation_envelope(m: dict, keep: int) -> dict:
    """A well-formed replacement for an oversized tool result: the head of
    the original content plus a marker and byte count, as JSON — never a
    raw slice (a mid-JSON cut with no marker is what made models re-call
    tools and flail; host_sdk_engine._bound_tool_result records the bug)."""
    c = m.get("content")
    text = c if isinstance(c, str) else json.dumps(c, default=str)
    env = {
        "truncated": True,
        "total_chars": len(text),
        "note": ("result trimmed for context budget — re-call the tool with a "
                 "narrower query or limit/offset if you need the rest"),
        "head": text[:keep],
    }
    return {**m, "content": json.dumps(env)}


def _strip_prefire_lines(system_content: str, tokens_to_free: int) -> tuple[str, int]:
    """Drop lowest-ranked (last) pre-fire bullets from the memory block's
    '### Relevant Right Now' section until tokens_to_free is covered or the
    section is empty (then the header goes too). Returns (new_content,
    tokens_freed). Never touches anything outside the section: the section
    is the header plus its consecutive '- ' bullet lines, so a following
    block without a markdown header can't be swallowed."""
    marker = "### Relevant Right Now"
    idx = system_content.find(marker)
    if idx < 0:
        return system_content, 0
    lines = system_content[idx:].split("\n")
    # lines[0] is the header; bullets (ranked best-first) follow. The first
    # non-bullet line ends the section.
    sec_len = 1
    for line in lines[1:]:
        if line.startswith("- "):
            sec_len += 1
        else:
            break
    freed = 0
    budget_chars = tokens_to_free * CHARS_PER_TOKEN
    kept = lines[:sec_len]
    rest = lines[sec_len:]
    while len(kept) > 1 and freed < budget_chars:
        freed += len(kept[-1])
        kept.pop()
    if len(kept) <= 1:
        # Whole section goes, header included.
        freed += len(kept[0]) if kept else 0
        new_lines = rest
    else:
        new_lines = kept + rest
    return system_content[:idx] + "\n".join(new_lines), freed // CHARS_PER_TOKEN


def trim_messages_to_window(messages: list[dict], target_tokens: int | None,
                            protect: list | None = None) -> list[dict]:
    """Trim an assembled request to target_tokens using the ONE trim order:
    pre-fire hits (lowest-ranked first) → oldest verbatim
    turns → tool-result verbosity. The system block (identity/constitutional,
    messages[0]), the current user message and the rolling-summary message
    are never dropped.

    WHAT IS PROTECTED IS COMPUTED, NOT POSITIONAL. This used to
    assume the current user message is messages[-1]. That holds for a plain
    chat turn and is false for every tool-loop round after the first, where
    the list ends with a role="tool" result — so the person's actual question
    was droppable, and the trailing call/result pair splittable. See
    _protected / _current_user_index.

    `protect` is the caller's list of message dicts that are the PERSON's
    words — the turn's question, each mid-turn nudge — matched by object
    identity (_protect_indices). Pass it when you appended them and can still
    name them; omit it and the structural fallback anchor applies.

    Step 2 drops plain turns only. Step 3 REPLACES an oversized tool result
    with a JSON truncation envelope — never a raw slice. Step 4 (still
    over) drops tool-call GROUPS (an assistant tool_calls message + its
    role:"tool" results) as one unit, oldest first, so a result is never
    left without the call that asked for it.

    A target <= 0 (window smaller than the output reserve) trims to the
    floor MIN_HISTORY_TOKENS instead of returning untouched — the caller's
    identity check (`is not messages`) is how the overflow retry knows a
    trim happened, so "nothing changed" must mean nothing needed to.

    Returns the input list unchanged when target is None or the estimate
    already fits; a NEW list otherwise. Never raises — a trim failure
    returns what we have.
    """
    if target_tokens is None or len(messages) <= 2:
        return messages
    if target_tokens <= 0:
        target_tokens = MIN_HISTORY_TOKENS
    try:
        total = sum(_msg_chars(m) for m in messages) // CHARS_PER_TOKEN
        if total <= target_tokens:
            return messages
        over = total - target_tokens
        out = list(messages)

        # 1. pre-fire hits, lowest-ranked first (they live in the system
        #    block's memory section).
        sys_msg = out[0]
        if isinstance(sys_msg.get("content"), str) and "### Relevant Right Now" in sys_msg["content"]:
            new_content, freed = _strip_prefire_lines(sys_msg["content"], over)
            if freed > 0:
                out[0] = {**sys_msg, "content": new_content}
                over -= freed

        # 2. oldest plain verbatim turns (user/assistant without tool_calls),
        #    oldest first. The rolling summary (the densest thing in the
        #    window) is spared, and so is anything in `keep` — above all the
        #    current user message, which sits at the END of a plain chat turn
        #    but in the MIDDLE of a tool loop. Tool-call groups are NOT touched
        #    here — a result the model asked for this turn is worth more than
        #    an old exchange, so they shrink in step 3 and go only in step 4.
        keep = _protected(out, _tool_groups(out), protect)
        if over > 0:
            drop: set[int] = set()
            for i in range(1, len(out) - 1):
                if over <= 0:
                    break
                if i in keep:
                    continue
                m = out[i]
                if m.get("role") in ("user", "assistant") and not m.get("tool_calls") \
                        and not _is_summary(m):
                    over -= _msg_chars(m) // CHARS_PER_TOKEN
                    drop.add(i)
                    # A turn's tool note (search mode, runtime/tool_core:
                    # the schemas it called) belongs to the turn and leaves
                    # with it — left behind, it could start the window. Only
                    # system messages sit between a turn and its note.
                    if m.get("role") == "assistant":
                        from mastercode.runtime.tool_core import is_note as _is_tool_note
                        j = i + 1
                        while j < len(out) and out[j].get("role") == "system":
                            if j not in keep and _is_tool_note(out[j]):
                                over -= _msg_chars(out[j]) // CHARS_PER_TOKEN
                                drop.add(j)
                            j += 1
            if drop:
                out = [m for i, m in enumerate(out) if i not in drop]
                keep = _protected(out, _tool_groups(out), protect)

        # 3. tool-result verbosity: replace oversized remaining tool results
        #    (oldest first) with a truncation envelope. The summary and plain
        #    turns are not touched here. This runs over the FINAL index too:
        #    in a tool loop the newest 100 KB result IS the last message, and
        #    shrinking it is the only way to make room without dropping the
        #    in-flight exchange (it is protected from step 4). Truncation
        #    keeps the message — and so the call/result pairing — intact.
        if over > 0:
            for i in range(1, len(out)):
                if over <= 0:
                    break
                m = out[i]
                if m.get("role") != "tool" or _is_summary(m):
                    continue
                before = _msg_chars(m)
                if before > TOOL_RESULT_KEEP_CHARS:
                    out[i] = _truncation_envelope(m, TOOL_RESULT_KEEP_CHARS)
                    over -= (before - _msg_chars(out[i])) // CHARS_PER_TOKEN

        # 4. still over: tool-call GROUPS, oldest first, as units — an
        #    assistant tool_calls message and every result answering it go
        #    together or not at all (an orphan on either side is a 400). The
        #    trailing group (the exchange in flight) is in `keep` and stays.
        if over > 0:
            drop = set()
            for g in _tool_groups(out):
                if over <= 0:
                    break
                if keep & set(g):
                    continue
                if len(g) > 1 or out[g[0]].get("tool_calls"):
                    over -= sum(_msg_chars(out[i]) for i in g) // CHARS_PER_TOKEN
                    drop.update(g)
            if drop:
                out = [m for i, m in enumerate(out) if i not in drop]
        return out
    except Exception as e:
        log.warning("trim_messages_to_window failed (returning untrimmed): %s", e)
        return messages


def hysteresis_floor(target_tokens: int) -> int:
    """One TRIM_BLOCK_DIVISOR block below target — where trim_with_hysteresis
    lands when it fires (0.75x target at the current divisor of 4).

    Exported so callers can go TIGHTER than it instead of re-deriving the
    figure: an overflow retry that trims to this same number frees nothing,
    gets the input list back unchanged, and silently does not retry."""
    return target_tokens - max(target_tokens // TRIM_BLOCK_DIVISOR, 1)


def trim_with_hysteresis(messages: list[dict], target_tokens: int | None,
                         protect: list | None = None) -> list[dict]:
    """The tool loop's per-round pre-send trim: trim only when the assembled
    list is OVER target, and then to one TRIM_BLOCK_DIVISOR block BELOW it.

    trim_messages_to_window trims to exactly the target, which thrashes the
    prompt cache inside a tool loop: the next tool result pushes the list back
    over, another old turn is dropped, the cached prefix moves, and the
    provider re-writes the whole cache — every round. Leaving a block of
    headroom means the same cut point, and so the same cached prefix, survives
    several rounds. It is the same trade the history walk makes with chunked
    eviction (session.py, TRIM_BLOCK_DIVISOR): carry slightly less than would
    strictly fit, in exchange for a prefix that stops moving.

    Two thresholds are the whole point — trigger at `target_tokens`, land a
    block under it. Trimming to the lower figure whenever the list exceeds THAT
    figure would just move the same thrash down one block.

    Returns the input list unchanged (the SAME object — callers use identity to
    tell whether a trim happened) when target is None or the estimate already
    fits; a new list otherwise. Never raises: a sizing failure falls back to the
    exact trim.
    """
    if target_tokens is None or len(messages) <= 2:
        return messages
    try:
        total = sum(_msg_chars(m) for m in messages) // CHARS_PER_TOKEN
        if total <= target_tokens:
            return messages
        # One block under the target. NOT clamped up to MIN_HISTORY_TOKENS:
        # that floor belongs to the BUDGET, and applying it here would push
        # the floor ABOVE the target whenever the target is small (a narrow
        # window, or a hard-turned-down dial) and silently disable the trim —
        # sending an over-target request. trim_messages_to_window applies the
        # floor itself for a target <= 0.
        floor = hysteresis_floor(target_tokens)
    except Exception as e:
        log.warning("trim_with_hysteresis sizing failed (exact trim instead): %s", e)
        return trim_messages_to_window(messages, target_tokens, protect)
    return trim_messages_to_window(messages, floor, protect)


# ── A used tool result leaves the turn ──────────────────
# Measured on one turn (Grok): harness_connections op='matrix'
# (165,985 chars) and coding_bench (30,729) were answered after in the next
# call and then rode along in all five calls left, ~400k of the turn's 609,633
# input tokens. Nothing removed a result inside a running turn: the pre-send
# trim fires only near the context target (449k there; the turn peaked at
# 121k), and between turns results are already digests (metadata.tool_calls).
#
# The floor is the size below which a result stays for the whole turn. The
# averages over one Host's 30 days: operate 1.2k, gitea 4k, orient 6k — the
# reads a Host works from round to round — and the results this was built for
# from coding_bench's ~30k up to 166k. 16,000 sits well above the first group's
# averages and at half the smallest of the second; the averages alone do not
# give their tails, so `dropped[].recalled` in the turn's ledger records every
# re-call of a dropped result, which is how churn is measured.
DROP_FLOOR_CHARS = 16_000
# Through runtime/result_store.py, a result over
# result_store.STORE_OVER_CHARS is kept whole in the pod database
# and its stub carries the handle: read(handle) returns exactly what was cut,
# nothing runs again, so an action's result may go too, not only a read's.
# Such a registration (it names `handle`) leaves at this floor, the store's
# own threshold (a test pins the two equal): over it, nothing rides whole
# after the model has used it. The 16,000 above stays for a result with no
# handle — a turn without the read door, or a store write that failed — whose
# stub can only say "call it again".
HANDLE_DROP_FLOOR_CHARS = 8_000
DROPPED_RESULT_KEY = "result_dropped"
# What the stub carries from the call's history record: the same fields
# tool_trail.digest_tool_action puts on metadata.tool_calls[] (session.py
# reads them off the call's ta_entry, so there is one summariser).
DROP_TRAIL_KEYS = ("operate_tool", "read_tool", "op", "provider", "harness",
                   "next_step", "digest")


def dropped_result_stub(tool: str, chars: int, trail: dict | None = None,
                        handle: str | None = None, offset: int | None = None,
                        length: int | None = None) -> str:
    """The content a dropped tool result is replaced with: the tool, the
    result's size, the history digest of the call, and how to see it again —
    read(handle) when the result is stored (runtime/result_store.py), exactly
    the characters that were cut, else a call of the tool again.
    JSON, like every tool result."""
    body: dict = {"tool": tool, "chars": int(chars)}
    for k in DROP_TRAIL_KEYS:
        v = (trail or {}).get(k)
        if v not in (None, "", [], {}):
            body[k] = v
    if handle:
        body["handle"] = handle
        call = f"read(handle='{handle}'"
        if offset:
            call += f", offset={int(offset)}"
        if length:
            call += f", length={int(length)}"
        call += ")"
        body["note"] = (f"You used this result earlier in this turn and it was taken out to "
                        f"save space. It is kept whole: {call} returns exactly what was here.")
    else:
        body["note"] = (f"You used this result earlier in this turn and it was taken out to "
                        f"save space. Call {tool} again with the same arguments to see it in full.")
    return json.dumps({DROPPED_RESULT_KEY: body}, default=str)


def is_dropped_stub(content) -> bool:
    return isinstance(content, str) and content.startswith('{"' + DROPPED_RESULT_KEY + '"')


# A stored result the Host is still paging stays. Measured on a Host: a 66k
# stored file was read in 8-14.5k pages, each page was cut the call after it
# was read while the Host read on in the same file,
# and pages of it were read back five times in one turn, each cut moving the
# cached prefix. Two signals that the Host is still reading a handle, both
# from what it did: the round it just ran read that handle, or it read the
# handle again after a page of it was cut (the caller's keep_handles).
#
# What a handle being read keeps is bounded. Its pages are
# kept newest first up to READING_KEEP_CHARS (or the caller's keep_chars, a
# quarter of the turn's context target, when that is smaller); older pages of
# it leave as before. A page read twice (the same handle, offset and length:
# a stored row never changes, so the same characters) is kept once, its newest
# copy. Without the bound a file read page by page rode whole in every call
# until the window trim cut whole rounds. 96,000 holds the measured 66k file
# whole with two of its largest pages (14.5k) read again.
READING_KEEP_CHARS = 96_000


def _reading_kept(messages: list, last_reply: int, droppable: dict, reading: set,
                  keep_chars: int) -> set:
    """Indices of the pages of `reading` handles that stay: newest first, one
    copy per (handle, offset, length), up to keep_chars (the newest is kept
    whatever its size)."""
    kept: set = set()
    seen: set = set()
    used = 0
    full = False
    for i in range(last_reply - 1, 0, -1):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        meta = droppable.get(str(m.get("tool_call_id") or ""))
        c = m.get("content")
        if meta is None or meta.get("handle") not in reading or not isinstance(c, str) \
                or is_dropped_stub(c):
            continue
        page = (meta["handle"], meta.get("offset"), meta.get("length"))
        if page in seen:
            continue
        seen.add(page)
        if full or (kept and used + len(c) > keep_chars):
            # Past the bound everything older leaves, so what stays is the
            # newest run of pages, never a gap filled by a smaller old one.
            full = True
            continue
        used += len(c)
        kept.add(i)
    return kept


def _handles_read_in(message) -> set:
    """Handles a model message's tool calls page (read(handle=...))."""
    from mastercode.runtime.result_store import is_result_read
    out: set = set()
    for tc in (message or {}).get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except (TypeError, ValueError):
                continue
        if is_result_read(fn.get("name"), args):
            out.add(args["handle"].strip())
    return out


def drop_used_tool_results(messages: list[dict], mark: int, droppable: dict,
                           protect: list | None = None,
                           floor: int = DROP_FLOOR_CHARS,
                           keep_handles=None,
                           keep_chars: int | None = None) -> tuple[list[dict], list[dict]]:
    """Replace the content of every tool result the model has already answered
    after with a stub, when that pays. Returns (messages, dropped): the SAME
    list and [] when nothing is dropped, else a new list and one entry per
    dropped result {index, tool_call_id, tool, chars}.

    Eligible: a role="tool" message whose tool_call_id is in `droppable` (the
    caller registers results it can give back: a stored one, by its `handle`,
    any tool; else a read, by calling it again; a registration that names its
    message as `msg` matches that object only), with an assistant message
    after it (the model has seen it and responded), content over `floor`
    characters (HANDLE_DROP_FLOOR_CHARS when it names a handle), not already
    a stub, and not in `protect`. A registration whose handle the Host is
    still reading is not eligible: the last model message pages that handle,
    or it is in `keep_handles`. Of those pages, the newest copy of each stays
    up to READING_KEEP_CHARS (or `keep_chars` when smaller); the rest are
    eligible (_reading_kept).

    Only the CONTENT changes: role, tool_call_id and name stay, so the
    call/result pairing holds on both wires (provider_gateway translates a
    tool message to an Anthropic tool_result by tool_call_id and content).
    A dropped message is a NEW dict, never edited in place, so anything that
    holds the old one by identity (search_dedup) sees it gone.

    Cut only when it pays: D = characters the stubs remove; S = characters
    from the earliest eligible result up to `mark` (the length of the list the
    previous call sent) as they would stand after the cut — the part of the
    cached prefix that is re-sent uncached because the prefix moved. The cut
    happens when D >= S, and takes EVERY eligible result at once, so the
    prefix moves once rather than every round — trim_with_hysteresis's
    reasoning. Deterministic; never raises."""
    try:
        if not droppable or len(messages) <= 2:
            return messages, []
        last_reply = max((i for i, m in enumerate(messages)
                          if isinstance(m, dict) and m.get("role") == "assistant"), default=-1)
        keep = _protect_indices(messages, protect) | {0}
        reading = set(keep_handles or ()) | (
            _handles_read_in(messages[last_reply]) if last_reply >= 0 else set())
        cap = READING_KEEP_CHARS if not keep_chars or keep_chars <= 0 \
            else min(int(keep_chars), READING_KEEP_CHARS)
        still_read = _reading_kept(messages, last_reply, droppable, reading, cap) if reading else set()
        picks: list = []
        for i in range(1, last_reply):
            m = messages[i]
            if not isinstance(m, dict) or m.get("role") != "tool" or i in keep:
                continue
            meta = droppable.get(str(m.get("tool_call_id") or ""))
            c = m.get("content")
            if meta is None or not isinstance(c, str) or is_dropped_stub(c):
                continue
            if i in still_read:
                continue
            # A stored result (its registration names a handle) leaves at the
            # store's floor; one with no handle at the caller's.
            if len(c) <= (min(floor, HANDLE_DROP_FLOOR_CHARS) if meta.get("handle") else floor):
                continue
            if meta.get("msg") is not None and meta["msg"] is not m:
                # The id came back on a later result (a provider that reuses
                # call ids across rounds): the registration is that one's.
                continue
            stub = dropped_result_stub(meta.get("tool") or m.get("name") or "the tool",
                                       len(c), meta.get("trail"), handle=meta.get("handle"),
                                       offset=meta.get("offset"), length=meta.get("length"))
            picks.append((i, stub, len(c)))
        if not picks:
            return messages, []
        stubs = {i: s for i, s, _ in picks}
        removed = sum(n - len(s) for _, s, n in picks)
        first = picks[0][0]
        resent = sum(len(stubs[j]) if j in stubs else _msg_chars(messages[j])
                     for j in range(first, min(max(int(mark or 0), first), len(messages))))
        if removed < resent:
            return messages, []
        out = list(messages)
        dropped = []
        for i, s, n in picks:
            out[i] = {**messages[i], "content": s}
            dropped.append({"index": i, "tool_call_id": str(messages[i].get("tool_call_id") or ""),
                            "tool": json.loads(s)[DROPPED_RESULT_KEY]["tool"], "chars": n})
        return out, dropped
    except Exception as e:  # noqa: BLE001 — the turn goes on with what it had
        log.warning("drop_used_tool_results failed (nothing dropped): %s", e)
        return messages, []


def messages_hashes(messages: list[dict], prev_len: int) -> dict:
    """What a model call's message list was, as hashes for the context ledger
    (12 hex of sha256): `sha` over every message, `prefix_sha` over the first
    `prev_len` (the length the previous call sent; None on a turn's first
    call), `sys_sha` over messages[0]. Each message is hashed as its
    canonical JSON, one after another, so call k's prefix_sha equals call
    k-1's sha exactly when every message the previous call sent went out
    again unchanged — a cache miss with equal hashes was not our prefix
    moving. Measurement only."""
    import hashlib
    h = hashlib.sha256()
    prefix = None
    n = max(0, int(prev_len or 0))
    for i, m in enumerate(messages):
        if i == n and n:
            prefix = h.hexdigest()[:12]
        h.update(json.dumps(m, sort_keys=True, default=str).encode("utf-8", "replace"))
        h.update(b"\n")
    if n and prefix is None and n >= len(messages):
        prefix = h.hexdigest()[:12]
    sys_sha = (hashlib.sha256(json.dumps(messages[0], sort_keys=True, default=str)
                              .encode("utf-8", "replace") + b"\n").hexdigest()[:12]
               if messages else None)
    return {"sys_sha": sys_sha, "prefix_sha": prefix, "sha": h.hexdigest()[:12]}


# The per-call measurements for the replay rigs, not part of the Host's mirror.
_LEDGER_ONLY_CALL_KEYS = frozenset({"tools_chars", "tools_sha", "sys_sha", "prefix_sha", "sha",
                                    # the provider's response id and a short hash
                                    # of the cache pin the call carried
                                    "request_id", "cache_key_sha"})


async def window_report(pool, entity_id: int) -> dict:
    """The host's MIRROR: what its own last turn cost and what the prompt was made of,
    read back from the two records the platform already keeps — the turn
    row's context_ledger and prompt_log. Sizes only, never content. A Host
    otherwise cannot see how big its own prompt is, what a turn cost, or
    which part of the window an answer came from.

    {last_turn: {at, calls, per_call:[{in,cached,out}], tokens_in_total,
                 system_chars, static_appended_chars, per_turn_blocks,
                 tools:{count,chars}, history:{turns,chars,summary},
                 largest_tool_results:[{tool,chars}]},
     prompt_blocks: [{name, chars, freshness}] (latest build, largest first),
     prompt_total_chars}"""
    out: dict = {"last_turn": None, "prompt_blocks": [], "prompt_total_chars": 0}
    try:
        row = await pool.fetchrow(
            "SELECT created_at, metadata->'context_ledger' AS ledger FROM entity_memory"
            " WHERE entity_id=$1 AND memory_type='turn' AND metadata ? 'context_ledger'"
            " ORDER BY created_at DESC LIMIT 1",
            entity_id,
        )
        if row and row["ledger"]:
            led = row["ledger"]
            if isinstance(led, str):
                led = json.loads(led)
            # tools_chars/tools_sha (session._note_served)
            # are the ledger's measurement for the tool-search rig, not part
            # of the Host's mirror: the Host's view of each call stays the
            # entry it was before those keys existed.
            per_call = [{k: v for k, v in c.items() if k not in _LEDGER_ONLY_CALL_KEYS}
                        for c in (led.get("per_call") or []) if isinstance(c, dict)]
            results = sorted(
                (r for r in (led.get("tool_results") or [])
                 if isinstance(r, dict) and isinstance(r.get("chars"), int)),
                key=lambda r: -r["chars"])[:5]
            out["last_turn"] = {
                "at": row["created_at"].isoformat() if row["created_at"] else None,
                "calls": led.get("calls"),
                "per_call": per_call,
                "tokens_in_total": sum(int(c.get("in") or 0) for c in per_call),
                "tokens_cached_total": sum(int(c.get("cached") or 0) for c in per_call),
                # Calls whose provider reported no usage: their counts are
                # unknown (None), not in the totals above, never zero.
                "calls_usage_unknown": sum(1 for c in per_call if c.get("usage") == "not_reported"),
                "system_chars": led.get("system_chars"),
                "static_appended_chars": led.get("static_appended_chars"),
                "per_turn_blocks": led.get("per_turn_blocks") or {},
                "tools": led.get("tools") or {},
                "history": led.get("history") or {},
                "largest_tool_results": results,
            }
    except Exception as e:
        log.debug("window_report ledger read failed (non-fatal): %s", e)
    try:
        # No src_ctx on purpose: this is the host asking what ITS OWN last
        # turn was made of, whatever thread that turn happened in. The
        # thread filter belongs to the panel (build_thread_context), not here.
        blocks = [b for b in await latest_prompt_blocks(pool, entity_id)
                  if isinstance(b, dict) and isinstance(b.get("chars"), int)]
        # The turn-stable blocks session.py appends OUTSIDE the schema build
        # (the orchestrator catalog and the host rules) are logged as their
        # own per_rebuild rows, which latest_prompt_blocks deliberately skips
        # for the View Context panel. The mirror must count them: newest row
        # per block name.
        try:
            extra = await pool.fetch(
                "SELECT DISTINCT ON (blocks->0->>'name') blocks FROM prompt_log"
                " WHERE entity_id=$1 AND scope='per_rebuild'"
                " ORDER BY blocks->0->>'name', log_id DESC",
                entity_id,
            )
        except Exception as e:
            log.debug("window_report per_rebuild rows failed (non-fatal): %s", e)
            extra = []
        for r in extra:
            b = r["blocks"]
            if isinstance(b, str):
                try:
                    b = json.loads(b)
                except Exception:
                    continue
            for item in (b if isinstance(b, list) else []):
                if isinstance(item, dict) and isinstance(item.get("chars"), int):
                    blocks.append(item)
        blocks.sort(key=lambda b: -b["chars"])
        out["prompt_blocks"] = [
            {"name": b.get("name"), "chars": b["chars"], "freshness": b.get("freshness")}
            for b in blocks if b.get("name") != "memory_prefire"
        ]
        out["prompt_total_chars"] = sum(b["chars"] for b in out["prompt_blocks"])
    except Exception as e:
        log.debug("window_report blocks read failed (non-fatal): %s", e)
    return out


async def last_turn_spend(pool, entity_id: int,
                          src_ctx: dict | None = None,
                          ) -> tuple[dict | None, dict | None]:
    """(last_turn, last_call) read back from THIS THREAD's newest turn row's
    context_ledger, or (None, None).

    SCOPED TO THE THREAD, for the same reason as the prompt blocks: this
    payload is rendered on one thread's panel, and an
    entity-wide read shows whichever thread spoke last. "What your last turn
    cost" is a per-thread claim or it is misinformation — a cheap workbench
    turn would report the cost of an expensive circles one, and the projection
    built on it would be wrong in the same direction.

    The scope predicate is memory_engine._turn_row_where, the same one the
    recall ladder and the thread consolidator use, so there is one scope truth
    and this cannot drift from what the thread actually contains. If it cannot
    be imported the read degrades to entity-wide rather than returning
    nothing — a slightly wrong number beats an empty panel, and the fallback
    is logged.

    last_turn is what the panel shows as ACTUAL — {calls, spent_usd,
    tokens:{uncached, cache_read, cache_write, output}} — summed over the
    turn's per-call entries. `uncached` is derived, not stored: the ledger
    records the lane-normalised TOTAL input per call (session.py `_note_served`)
    and the cached/written subsets, so uncached is the remainder, clamped at 0
    for the same reason estimate_call_cost_detail clamps it (inference.py:339).

    last_call is the newest call's raw {in, cached, cache_write, out}, which
    is what a NEXT turn's first call will look like — the basis of the
    projection. It is the last call rather than the mean because that is the
    one whose context survived the turn's trimming.

    Older rows have no cache_write and no per-call usd;
    they read as 0 / None rather than breaking the panel.
    """
    args: list = [entity_id]
    where = ["entity_id = $1", "memory_type = 'turn'"]
    # Only WITH a thread descriptor. _turn_row_where's no-scope fallback is
    # `source_session = <session_id>` (memory_engine.py:1118), and this reader
    # has no session — passing "" there would scope the query to a session
    # that does not exist and silently return nothing on every thread.
    if isinstance(src_ctx, dict) and src_ctx.get("kind"):
        try:
            from mastercode.memory_engine import _turn_row_where
            _a: list = [entity_id]
            _w = _turn_row_where(_a, session_id="", source_context=src_ctx)
            where, args = _w, _a
        except Exception as e:
            log.debug("last_turn_spend scope unavailable, reading entity-wide "
                      "(non-fatal): %s", e)
    try:
        row = await pool.fetchrow(
            "SELECT metadata->'context_ledger' AS ledger,"
            " metadata->'tokens_unknown_calls' AS unknown_calls FROM entity_memory"
            " WHERE " + " AND ".join(where) +
            " AND metadata ? 'context_ledger'"
            " ORDER BY created_at DESC LIMIT 1",
            *args,
        )
    except Exception as e:
        log.debug("last_turn_spend fetch failed (non-fatal): %s", e)
        return None, None
    if not row or not row["ledger"]:
        return None, None
    led = row["ledger"]
    if isinstance(led, str):
        try:
            led = json.loads(led)
        except Exception:
            return None, None
    if not isinstance(led, dict):
        return None, None
    per_call = [c for c in (led.get("per_call") or []) if isinstance(c, dict)]
    spend = led.get("spend") if isinstance(led.get("spend"), dict) else {}
    if not per_call and not spend:
        return None, None

    def _i(c, k):
        try:
            return int(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    # THE PER-CALL LIST IS TRUNCATED AND MUST NOT BE SUMMED WHEN THE TOTALS
    # ARE ON THE ROW. session.py stores per_call[:24]; a 40-call turn
    # therefore re-sums to roughly 60% of what it cost, and the token lines
    # are short by the same 16 calls. `spend` (turn_allowance.TurnSpend
    # .summary) is totalled over EVERY call before the truncation happens, so
    # it is preferred wherever it exists and the sum below is the fallback for
    # older rows — which have no `spend` key, and for
    # which a truncated sum is the only number available and is at least
    # labelled by `calls`.
    tokens = spend.get("tokens") if isinstance(spend.get("tokens"), dict) else None
    spent = spend.get("spent_usd") if isinstance(
        spend.get("spent_usd"), (int, float)) else None
    calls = spend.get("calls") or led.get("calls") or len(per_call)
    if tokens is None:
        cache_read = sum(_i(c, "cached") for c in per_call)
        cache_write = sum(_i(c, "cache_write") for c in per_call)
        total_in = sum(_i(c, "in") for c in per_call)
        tokens = {
            "uncached": max(0, total_in - cache_read - cache_write),
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": sum(_i(c, "out") for c in per_call),
        }
        if spent is None:
            for c in per_call:
                v = c.get("usd")
                if isinstance(v, (int, float)):
                    spent = (spent or 0.0) + float(v)
    # Calls whose provider reported no usage. The turn row's own count
    # (session.py tokens_unknown_calls) covers every call; the stored per-call
    # list is cut at 24, so it is only the fallback.
    unknown = sum(1 for c in per_call if c.get("usage") == "not_reported")
    try:
        _uc = row.get("unknown_calls")
        if isinstance(_uc, str):
            _uc = json.loads(_uc)
        unknown = max(unknown, int(_uc or 0))
    except (TypeError, ValueError, AttributeError):
        pass
    # The meter adds nothing for an unreported call, so with some of them the
    # dollars cover only the calls that reported (partial), and with every
    # call unreported they are no figure at all.
    try:
        _all_unknown = unknown >= int(calls or 0) > 0
    except (TypeError, ValueError):
        _all_unknown = False
    if unknown and _all_unknown:
        spent = None
    last_turn = {
        "calls": calls,
        "spent_usd": round(float(spent), 6) if spent is not None else None,
        "tokens": {k: int(tokens.get(k) or 0)
                   for k in ("uncached", "cache_read", "cache_write", "output")},
        # True when the stored per-call list was cut, so a reader knows the
        # list is a sample and the totals above are not derived from it.
        "per_call_truncated": bool(per_call) and calls > len(per_call),
        # Calls whose provider reported no usage (their tokens are unknown and
        # are not in the totals).
        "calls_usage_unknown": unknown,
        # spent_usd covers only the calls that reported: a floor, not the
        # turn's price. The panel says so beside the figure.
        "spent_partial": bool(unknown) and spent is not None,
        # Set when a mid-turn failover moved the unit this turn was metered
        # in, so the panel can say the dollar figure stopped being the whole
        # story instead of showing a total that quietly stopped growing.
        "basis_changed_from": (spend.get("basis_changed_from")
                               if isinstance(spend.get("basis_changed_from"), str)
                               else None),
    }
    # The turn's REAL last call, for the projection. per_call[-1] is call #24
    # on a truncated row, not the one the thread actually finished on, and the
    # projection is meant to answer "what will the next turn open with".
    last_call = spend.get("last_call")
    if not isinstance(last_call, dict) or not last_call:
        # The meter keeps the last call it could measure; with none (every
        # call reported no usage, or a row from before the meter) the newest
        # call that reported does. An unreported one would project at $0.
        measured = [c for c in per_call if c.get("usage") != "not_reported"]
        last_call = measured[-1] if measured else None
    return last_turn, last_call


# The running total's arithmetic, done by the database in one read. Each turn
# row's `spend` (turn_allowance.TurnSpend.summary) is totalled over EVERY call
# of the turn, so the sum is the sum of those; the per-call list beside it is
# cut at 24 and is never read here. A value that is not a JSON number counts
# as absent, never as zero and never as an error.
_SPEND = "metadata->'context_ledger'->'spend'"
_HAS_SPEND = f"jsonb_typeof({_SPEND}) = 'object'"
_TOKEN_KINDS = ("uncached", "cache_read", "cache_write", "output")


def _json_num(path: str) -> str:
    """`path` (a jsonb expression) as numeric when it holds a number, else NULL."""
    return f"(CASE WHEN jsonb_typeof({path}) = 'number' THEN ({path})::text::numeric END)"


# A Host reply: a row that shows the per-turn figure (it carries the turn's
# metrics stamp, gen_ms or model: the client's own test, workbench-web
# chat-pane.tsx turnMetrics). Platform notes in the conversation (an agent's
# delivered result, a moved-thread marker, a stopped turn's note) carry no
# metrics stamp: they are not turns of the Host's model, so they are neither
# counted nor unknown, in tokens or in dollars.
_HOST_REPLY = (
    "metadata->>'role' = 'assistant'"
    " AND (metadata->>'gen_ms' IS NOT NULL OR COALESCE(metadata->>'model', '') <> '')")
# A Host reply whose spend is not on its row, because it was written before
# the meter rode the row.
_HOST_REPLY_NO_SPEND = f"{_HOST_REPLY} AND jsonb_typeof({_SPEND}) IS DISTINCT FROM 'object'"

# The running DOLLARS are the prices the replies print, never the meter's.
# Each Host reply's row carries est_cost_usd, its list-price estimate at the
# served model's published rates (session.py, inference.estimate_turn_cost),
# printed under the reply beside its tokens (circles-web metrics-format.ts
# costLabel). They are summed by the rule knowledge_toc.thread_weight sums a
# thread's cost by: assistant rows whose est_cost_usd is a JSON number. The
# meter's spend.spent_usd is a number only on a thread with a dollar allowance
# (turn_allowance.TurnSpend.summary, basis "usd"), so on every other thread
# it would leave the page with no dollars beside replies that each show some.
_EST_USD = "metadata->'est_cost_usd'"
_PRICED_REPLY = f"metadata->>'role' = 'assistant' AND jsonb_typeof({_EST_USD}) = 'number'"
# Where a reply has no dollars, it prints the basis word instead
# (estimate_turn_cost): "free" and "subscription" are known to add none per
# token. "unpriced", or no stamp at all (an older reply), is a
# price nobody knows, so the total is partial. So is a "partial" basis: its
# dollars cover only the calls that reported usage, a floor.
_REPLY_UNPRICED = (
    f"{_HOST_REPLY} AND jsonb_typeof({_EST_USD}) IS DISTINCT FROM 'number'"
    " AND COALESCE(metadata->>'price_basis', '') NOT IN ('free', 'subscription')")
_REPLY_PRICED_FLOOR = f"{_PRICED_REPLY} AND metadata->>'price_basis' = 'partial'"
USD_LABEL = ("ESTIMATE — each reply's list price at its model's published rates, the "
             "figure printed under the reply, summed; never billed")


async def running_spend(pool, entity_id: int, src_ctx: dict | None) -> dict | None:
    """What a conversation has spent so far: the sum of every turn row's own
    totals (context_ledger.spend) over the conversation `src_ctx` names, in
    the scope last_turn_spend reads (memory_engine._turn_row_where), so the
    running count and the panel's last turn always count the same rows. Takes
    any scope, so a Circles thread is summed by passing its descriptor.

    {turns_counted, turns_unknown, first_counted_at, calls,
     tokens:{uncached, cache_read, cache_write, output}, tokens_total,
     calls_usage_unknown,
     est_usd, turns_priced, turns_unpriced, usd_partial, usd_label}

    TOKENS come from the turns' spend records:
      - turns_counted: rows that carry a spend record; every token figure is
        theirs. first_counted_at is the oldest of them, which is where the
        total starts when turns_unknown is not 0.
      - turns_unknown: Host replies with no spend record (_HOST_REPLY_NO_SPEND).
        Their tokens are in no figure here.
      - calls_usage_unknown: calls whose provider reported no usage (the
        rows' tokens_unknown_calls); their tokens are in no figure either.

    DOLLARS are an estimate, the sum of the prices printed under the replies
    (est_cost_usd, _PRICED_REPLY), never the allowance meter's spent_usd:
      - est_usd: that sum, None when no reply carries a price.
      - turns_priced: the replies it sums.
      - turns_unpriced: Host replies whose price nobody knows (_REPLY_UNPRICED);
        a free or subscription reply is not one of them, it adds no dollars.
      - usd_partial: est_usd leaves something out: turns_unpriced is not 0, or
        a summed reply's price covers only part of its calls (basis
        "partial").
      - usd_label: says, in words, that the dollars are an estimate.

    None when `src_ctx` names no conversation: unlike the last-turn panel, a
    total has no entity-wide reading to fall back to.

    THE RUNS THAT THINK THERE COUNT (with_wakeup_rows). A fired wake-up turns
    on the hidden lane in its thread, and its own rows carry what it spent;
    recall and the transcript leave them out because the runner's report
    carries the reply, but the report carries no spend. With no original
    conversation every scheduled task runs in a topic,
    so a total that left those rows out would show a task's cost on no page
    at all. The last-turn panel (last_turn_spend) stays on the person's own
    turns: it is the base of the next turn's projection."""
    if not (isinstance(src_ctx, dict) and src_ctx.get("kind")):
        return None
    from mastercode.memory_engine import _turn_row_where
    args: list = [entity_id]
    where = " AND ".join(_turn_row_where(args, session_id="", source_context=src_ctx,
                                         with_wakeup_rows=True))
    calls = _json_num(_SPEND + "->'calls'")
    unknown_calls = _json_num("metadata->'tokens_unknown_calls'")
    token_sums = ",\n".join(
        "sum(" + _json_num(_SPEND + "->'tokens'->'" + k + "'") + ")"
        " FILTER (WHERE " + _HAS_SPEND + ") AS tok_" + k
        for k in _TOKEN_KINDS)
    row = await pool.fetchrow(
        f"""
        SELECT count(*) FILTER (WHERE {_HAS_SPEND}) AS turns_counted,
               count(*) FILTER (WHERE {_HOST_REPLY_NO_SPEND}) AS turns_unknown,
               min(created_at) FILTER (WHERE {_HAS_SPEND}) AS first_counted_at,
               sum({calls}) FILTER (WHERE {_HAS_SPEND}) AS calls,
               {token_sums},
               sum({unknown_calls}) FILTER (WHERE {_HAS_SPEND}) AS calls_usage_unknown,
               sum({_json_num(_EST_USD)}) FILTER (WHERE {_PRICED_REPLY}) AS est_usd,
               count(*) FILTER (WHERE {_PRICED_REPLY}) AS turns_priced,
               count(*) FILTER (WHERE {_REPLY_UNPRICED}) AS turns_unpriced,
               count(*) FILTER (WHERE {_REPLY_PRICED_FLOOR}) AS turns_floor
          FROM entity_memory
         WHERE {where}
        """,
        *args,
    )
    tokens = {k: int(row[f"tok_{k}"] or 0) for k in _TOKEN_KINDS}
    priced = int(row["turns_priced"] or 0)
    unpriced = int(row["turns_unpriced"] or 0)
    return {
        "scope": {"kind": str(src_ctx.get("kind")), "ref": str(src_ctx.get("ref") or "")},
        "turns_counted": int(row["turns_counted"] or 0),
        "turns_unknown": int(row["turns_unknown"] or 0),
        "first_counted_at": row["first_counted_at"],
        "calls": int(row["calls"] or 0),
        "tokens": tokens,
        "tokens_total": sum(tokens.values()),
        "calls_usage_unknown": int(row["calls_usage_unknown"] or 0),
        "est_usd": round(float(row["est_usd"]), 6) if priced and row["est_usd"] is not None else None,
        "turns_priced": priced,
        "turns_unpriced": unpriced,
        "usd_partial": unpriced > 0 or int(row["turns_floor"] or 0) > 0,
        "usd_label": USD_LABEL,
    }


async def build_thread_context(
    pool,
    entity_id: int,
    src_ctx: dict | None,
    *,
    thread_override: dict | None = None,
    surface: str | None = None,
) -> dict:
    """The GET thread-context payload (wire contract): {window, budget,
    summary_version, consolidated_at, blocks, history_preview,
    summary_excerpt, last_prefired} + `prefire_state` (additive),
    `thread_state` (additive: with the consolidator off, the goal + handoff
    that ride instead of the summary) and the spend allowance trio
    the budget field needs to be honest: {turn_allowance_usd,
    allowance_basis, projected_next_turn_usd, last_turn}.

    ALLOWANCE / PROJECTED / ACTUAL is the whole point of shipping all four
    together: a dollar box with nothing beside it asks the person to guess
    what a turn costs. `projected_next_turn_usd` is what the next turn's
    first call would cost at today's rates, `last_turn` is what the previous
    one actually cost, and `allowance_basis` says whether dollars mean
    anything on this route at all.

    Shared by the workbench (/api/brain/thread-context) and circles
    (/api/circles/thread-context/{kind}/{id}) handlers. Every section is
    independently best-effort — a missing piece reads as null/empty, never
    a 500.

    prefire_state SAYS WHETHER A PER-TURN BUILD FOR THIS THREAD WAS FOUND,
    because "this thread has no build" and "this thread's build pre-fired
    nothing" are different facts and an empty last_prefired alone cannot tell
    them apart:
      - "thread"      : a per-turn build for this thread is what is reported.
                        last_prefired MAY STILL BE EMPTY — see below — and the
                        blocks are real either way.
      - "no_build_yet": no per-turn build for this thread is in range (a new
                        thread, or one whose rows predate v149's stamping) —
                        transient, one turn fixes it.
      - "not_built_here": kind=peer. A circles dm/entity thread is the far
                        side of another host: its turns are INGESTED
                        (server/synaptive_handler.py), no prompt is built here,
                        so nothing is logged and nothing ever will be. Not a
                        gap — the correct answer for that thread kind.

    IT IS DERIVED FROM THE PER-TURN BLOCKS, NOT FROM THE PRE-FIRE ENTRY. The
    memory_prefire entry is absent from a perfectly normal build in two cases,
    both of them ordinary rather than exceptional: the memory block did not
    fire at all (prompt_schema._prefire_log_entry:1004-1005) or the pre-fire
    recall returned nothing (:1011-1012 — memory_engine.last_prefire_items:983
    documents "[] when the last turn fired none"). Keying the state on that
    entry reported "no_build_yet" for a thread holding four real blocks, and
    both panels then replaced the whole composition with "Nothing assembled
    yet". A host with a thin memory corpus would have seen that on every
    thread, which is every new host. The per-turn HALF is the honest signal:
    if any returned entry carries freshness "per_turn" (the writer stamps it,
    prompt_schema._write_prompt_log), a build for this thread was found.
    Additive on the wire: both clients map the payload field by field and
    coerce last_prefired through `Array.isArray(...) ? ... : []`
    (workbench-web/src/lib/api.ts:385, circles-web/src/lib/api.ts:254), so an
    unknown key is ignored by a client that has not been taught it."""
    out: dict = {
        "window": None,
        "budget": None,
        "summary_version": None,
        "consolidated_at": None,
        "blocks": [],
        "history_preview": [],
        "summary_excerpt": None,
        "thread_state": None,
        "last_prefired": [],
        "prefire_state": (
            "not_built_here"
            if isinstance(src_ctx, dict) and src_ctx.get("kind") == "peer"
            else "no_build_yet"
        ),
        "turn_allowance_usd": None,
        "allowance_basis": "none",
        "projected_next_turn_usd": None,
        "last_turn": None,
    }

    # Window: the chat route's model → catalog context_length. The route is
    # kept for the allowance section below, which has to price THAT model.
    route: dict = {}
    try:
        from mastercode.inference_config import resolve_role_route
        route = await resolve_role_route(
            pool, entity_id, "chat", thread_override=thread_override) or {}
        if route.get("model"):
            out["window"] = await model_context_window(
                route["model"], provider=route.get("provider"))
    except Exception as e:
        log.debug("thread-context window resolve failed (non-fatal): %s", e)

    # Blocks: re-derived from the latest prompt_log rows for THIS THREAD —
    # the entity-wide static build plus the newest per-turn build stamped with
    # this src_ctx. An entity-scoped read showed a panel opened on one thread
    # the other thread's blocks and pre-fired memories.
    # A thread with no build of its own reads the static half and an empty
    # last_prefired; prefire_state above says whether that is a thread waiting
    # for its first turn or a peer thread, which is never built here at all.
    blocks: list[dict] = []
    try:
        blocks = await latest_prompt_blocks(pool, entity_id, src_ctx)
        out["blocks"] = [
            {"name": b.get("name"), "chars": b.get("chars")}
            for b in blocks if isinstance(b, dict)
        ]
        # A per-turn entry means a build for THIS thread came back — whether
        # or not it pre-fired anything, and including the single injected-block
        # row latest_prompt_blocks falls back to when the full build is out of
        # range. Only then can last_prefired's emptiness be reported as "this
        # build fired nothing" rather than "there is no build".
        if any(isinstance(b, dict) and b.get("freshness") == "per_turn"
               for b in blocks):
            out["prefire_state"] = "thread"
        for b in blocks:
            if isinstance(b, dict) and b.get("name") == "memory_prefire" \
                    and isinstance(b.get("items"), list):
                out["last_prefired"] = b["items"]
    except Exception as e:
        log.debug("thread-context blocks failed (non-fatal): %s", e)

    # Budget: the per-thread override, else the derived default
    # (window − output reserve − measured system − 5%).
    try:
        override = await read_thread_budget(pool, entity_id, src_ctx, surface)
        if override is not None:
            out["budget"] = override
        elif out["window"]:
            system_tokens = sum(
                int(b.get("chars") or 0) for b in blocks if isinstance(b, dict)
            ) // CHARS_PER_TOKEN
            out["budget"] = derive_turn_budget(
                window=out["window"],
                output_reserve=output_limit_for(
                    route.get("model"), route.get("provider"), out["window"]),
                system_tokens=system_tokens)
    except Exception as e:
        log.debug("thread-context budget failed (non-fatal): %s", e)

    # SPEND ALLOWANCE: what the person set, whether dollars can be enforced on
    # this route, what the next turn is projected to cost, and what the last
    # one actually cost. Best-effort like every other section — a cold pricing
    # catalog degrades to basis "none" and a null projection, never a 500.
    try:
        from mastercode.runtime.turn_allowance import TurnSpend
        out["turn_allowance_usd"] = await read_thread_allowance(
            pool, entity_id, src_ctx, surface)
        meter = TurnSpend.for_route(
            route.get("model"), route.get("provider"),
            allowance_usd=out["turn_allowance_usd"],
            ctx_budget=out.get("budget"))
        out["allowance_basis"] = meter.basis
        if meter.basis == "tokens":
            out["allowance_tokens"] = meter.allowance_tokens
    except Exception as e:
        log.debug("thread-context allowance failed (non-fatal): %s", e)
    try:
        last_turn, last_call = await last_turn_spend(pool, entity_id, src_ctx)
        out["last_turn"] = last_turn
        # The projection is the LAST CALL's context re-priced at the four
        # rates — uncached input, cache read, cache write, output — because a
        # next turn opens with a prompt the same size as the one this thread
        # just finished on (per-round trim + prefire keep it flat; nothing
        # accumulates across turns). It is one CALL, not the whole turn: the
        # turn's total depends on how many tool rounds the model chooses, and
        # that is exactly what the allowance is there to govern.
        if last_call and route.get("model"):
            from server.routes.inference import estimate_call_cost_detail
            usd, _ = estimate_call_cost_detail(
                str(route["model"]),
                int(last_call.get("in") or 0), int(last_call.get("out") or 0),
                cache_read_tokens=int(last_call.get("cached") or 0),
                cache_write_tokens=int(last_call.get("cache_write") or 0),
                provider=route.get("provider"))
            out["projected_next_turn_usd"] = (None if usd is None
                                              else round(float(usd), 6))
    except Exception as e:
        log.debug("thread-context projection failed (non-fatal): %s", e)

    # Where the thread stands, as the next turn gets it: the thread's goal
    # and last handoff (close_turn.thread_state, the same call the turn
    # makes). No rolling summary rides any scope (evicted_record).
    try:
        from mastercode.runtime.close_turn import thread_state
        state = await thread_state(pool, entity_id, src_ctx)
        if state:
            out["thread_state"] = state[len(THREAD_STATE_PREFIX):].strip() or None
    except Exception as e:
        log.debug("thread-context state failed (non-fatal): %s", e)

    # History preview: the turns the scope would recall, excerpted, as the
    # next turn's model gets them (memory_engine.history_messages): the Host's
    # own words, and a platform notice on a reply as the platform's own note
    # row — never in the Host's words, and never dropped from the person's
    # view of the window.
    try:
        from mastercode.memory_engine import history_messages, recall_recent_turns
        turns = await recall_recent_turns(
            pool, entity_id, "", limit=12, source_context=src_ctx)
        out["history_preview"] = [
            {"role": m.get("role") or "user",
             "excerpt": (m.get("content") or "")[:200]}
            for m in history_messages(turns)
        ]
    except Exception as e:
        log.debug("thread-context history preview failed (non-fatal): %s", e)

    return out
