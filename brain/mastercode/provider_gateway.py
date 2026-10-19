"""
Provider Gateway — the Orchestrator's execution arm.

Every call runs on the USER's OWN provider account, dispatched by PROVIDER
(server.vault.providers registry id, or 'local'):

  - OpenAI-wire providers (openrouter, openai, xai, google, kimi, deepinfra,
    together, fireworks, groq) → _call_remote (chat-completions). `google` is
    Gemini dialed on Google's OWN OpenAI-compatibility base, so it needs no
    wire of its own.
  - anthropic → _call_anthropic (Messages API; the user's own anthropic
    Keys entry — API key or OAuth token).
  - local → _call_local (the user's pod/bastion model server, XML tool
    injection + streaming).

A route may carry {fallback_provider, fallback_model}: when the primary call
errors (provider outage, model unavailable), call_model retries once on the
fallback and annotates the result — surfacing the failover, never hiding it.

There is no platform inference lane and no Claude-SDK dispatch: the SDK is
not a production option; its engine module stays in the
image but nothing here routes to it.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import time as _time
import uuid as _uuid

import aiohttp

log = logging.getLogger("cseries.mastercode.provider_gateway")

# ---------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------

_session_pool: dict[str, aiohttp.ClientSession] = {}
_connector: aiohttp.TCPConnector | None = None


def _get_connector() -> aiohttp.TCPConnector:
    global _connector
    if _connector is None or _connector.closed:
        _connector = aiohttp.TCPConnector(
            limit=20, limit_per_host=10, ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
    return _connector


async def _get_session(base_url: str) -> aiohttp.ClientSession:
    if base_url in _session_pool:
        session = _session_pool[base_url]
        if not session.closed:
            return session
    session = aiohttp.ClientSession(
        connector=_get_connector(), connector_owner=False,
        timeout=aiohttp.ClientTimeout(total=120),
    )
    _session_pool[base_url] = session
    return session


async def close_all_sessions():
    for session in _session_pool.values():
        if not session.closed:
            await session.close()
    _session_pool.clear()
    global _connector
    if _connector and not _connector.closed:
        await _connector.close()
    _connector = None


# ---------------------------------------------------------------
# Config imports
# ---------------------------------------------------------------

from mastercode.inference_config import (
    OPENROUTER_URL, OPENROUTER_API_KEY, LOCAL_URL,
)

# ---------------------------------------------------------------
# Testing harness hooks
# ---------------------------------------------------------------

_prompt_console = None
_kill_switch = None
_inference_pool = None


def set_prompt_console(console):
    global _prompt_console
    _prompt_console = console


def set_kill_switch(switch):
    global _kill_switch
    _kill_switch = switch


def set_inference_pool(pool):
    global _inference_pool
    _inference_pool = pool


def _normalize_openai_usage(usage: dict, *, provider=None, model=None):
    """An OpenAI-shaped `usage` object -> (total, prompt, completion,
    cache_read, cache_write), with prompt ALWAYS the true total input.

    TWO CONVENTIONS ARRIVE THROUGH THIS ONE WIRE AND THEY ARE NOT THE SAME
    FACT. OpenRouter and OpenAI report cache hits in
    `prompt_tokens_details.cached_tokens`, a SUBSET of prompt_tokens. A
    provider that relays Anthropic's usage verbatim inside an OpenAI-shaped
    object reports `cache_read_input_tokens` / `cache_creation_input_tokens`
    BESIDE an input count that EXCLUDES them — the same layout the SDK lane
    unconditionally re-adds at :1337. Every reader downstream (pricing, the
    turn budget, the Compute rollups) is written against "prompt_tokens is the
    true total input", so the second shape has to be converted to the first
    here, at the only point where the two are still distinguishable.

    WHAT IT COSTS TO SKIP THIS, which is why it is a normalisation and not a
    clamp: the cost estimate computes uncached = total - read - write. On the
    unnormalised passthrough shape that goes NEGATIVE, floors at zero, and
    prices the call's real uncached input at $0 — a silent undercount in the
    lane whose entire job is to be the number a budget is built on.
    inference.py's clamp stays as the last line of defence and now logs at
    WARNING, because by the time tokens reach it the two shapes are one number
    and the information needed to tell them apart is gone.

    THE TEST IS ARITHMETIC, NOT A PROVIDER ALLOWLIST: if the reported input
    cannot contain the cached counts, it was never a total. A conforming
    provider is untouched, a cached_tokens-style provider is untouched (its
    count IS a subset), and a provider nobody has met yet is handled the day it
    appears rather than the day someone notices the bill.

    AND IT IS ONE-SIDED — THE LIMIT, STATED PLAINLY BECAUSE IT IS NOT OBVIOUS
    FROM THE CODE. The condition fires only when the reported input is SMALLER
    than read + write. A passthrough provider whose uncached remainder happens
    to EXCEED its cached counts (a long fresh prompt against a small cached
    prefix) is arithmetically indistinguishable from a conforming total from
    in here, so it is not normalised — and that row undercounts its total input
    by exactly the cached count, which then prices at the cheap cache rate
    instead of being counted as input at all. The error is bounded by
    read + write and is always in the UNDER direction.

    Why it is left that way: the alternative is to normalise whenever the
    Anthropic field names appear, which would DOUBLE-COUNT every provider that
    uses those names correctly — an over-count on a shape that is known to
    exist, traded for an under-count on one that is hypothetical. If a real
    provider is ever found in this state, the fix is to name it (a per-provider
    flag keyed on the lane), not to loosen the arithmetic. The lower-bound
    reading also keeps the estimate honest in the one direction that matters
    for a budget: it can be low, never invented high."""
    total = usage.get("total_tokens", 0)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    ptd = usage.get("prompt_tokens_details") or {}
    ptd_cached = int(ptd.get("cached_tokens") or 0)
    an_read = int(usage.get("cache_read_input_tokens") or 0)
    an_write = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = ptd_cached or an_read
    cache_write = an_write
    if (an_read or an_write) and not ptd_cached and prompt is not None \
            and int(prompt) < an_read + an_write:
        uncached = int(prompt)
        prompt = uncached + an_read + an_write
        log.info("usage normalised for %s/%s: prompt_tokens %d excluded "
                 "cache_read=%d cache_write=%d; true total input is %d",
                 provider, model, uncached, an_read, an_write, prompt)
        # Keep the row internally consistent. A provider that under-reports the
        # input under-reports the total with it; only ever RAISE a total, so one
        # that was already right is left alone.
        _sum = prompt + int(completion or 0)
        if int(total or 0) < _sum:
            total = _sum
    return total, prompt, completion, cache_read, cache_write


# Extra keys for the inference_cost_log rows of ONE call_model call: its
# `_log_meta` kwarg, set for the length of that call only (call_model). A
# dispatched agent passes its task id (scoped_agent), so background activity
# can name the agent, not only "an agent". Merged under the row's own keys.
_CALL_LOG_META: contextvars.ContextVar = contextvars.ContextVar("call_log_meta", default=None)


async def _log_inference(source, model, provider, total_tokens, elapsed_ms=0,
                         prompt_tokens=None, completion_tokens=None,
                         entity_id=None, metadata=None,
                         cache_read_tokens=None, cache_write_tokens=None,
                         purpose="inference"):
    _call_meta = _CALL_LOG_META.get()
    if isinstance(_call_meta, dict) and _call_meta:
        metadata = {**_call_meta, **(metadata or {})}
    # `purpose` discriminates the row class in the ONE ledger. It was a
    # hardcoded 'inference' in the INSERT below; failed calls now land here
    # too as 'inference_error' (see _log_call_failure), so every cost reader
    # excludes `purpose IS DISTINCT FROM 'inference_error'` to keep its call
    # COUNTS unchanged (inference.py get_inference_budget, compute.py's
    # rollups). Those readers must NOT filter `purpose = 'inference'`: this
    # ledger is multi-purpose — synaptive_handler.py:220-221 writes 'shape'
    # rows the Budget app is meant to include — so an equality filter would
    # silently drop a live lane.
    # Cache telemetry rides the metadata JSONB (no schema change): on the
    # Anthropic wire prompt_tokens is the UNCACHED remainder only — without
    # these two fields a 140K-context turn logs as "4 in" and cache hit rate
    # is unmeasurable. One INFO line per call so lane-vs-lane testing can be
    # done from the pod log without a DB round-trip.
    # A call whose provider reported no usage (metadata usage=not_reported,
    # _call_remote) is stored as unknown: NULL counts (migrations/v214), no
    # price, never a row of zeros a rollup would read as a free call.
    _unknown = isinstance(metadata, dict) and metadata.get("usage") == "not_reported"
    if cache_read_tokens is not None or cache_write_tokens is not None:
        metadata = {
            **(metadata or {}),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cache_write_tokens": int(cache_write_tokens or 0),
        }
    log.info(
        "INFERENCE_USAGE source=%s provider=%s model=%s in=%s cache_read=%s "
        "cache_write=%s out=%s",
        source, provider, model, prompt_tokens,
        cache_read_tokens if cache_read_tokens is not None else "-",
        cache_write_tokens if cache_write_tokens is not None else "-",
        completion_tokens,
    )
    if not _inference_pool:
        return
    # The DB half writes to inference_cost_log — the ledger that actually
    # EXISTS in the pod DB (init-db.sql:1407) and that the Compute app reads
    # (server/routes/compute.py:1159-1174, server/routes/inference.py:2380-2409).
    # The old target, `inference_log`, was never created by init-db.sql or any
    # migration, so this write could only ever have raised; it never did,
    # because set_inference_pool() (:104) was imported and never called and
    # _inference_pool stayed None. Both halves were dead.
    #
    # est_cost_usd is the vendor ESTIMATE lane, priced from the PROVIDER'S OWN
    # published per-token rate — never a Privatae charge (the platform bills
    # compute only and never sees the vendor's bill).
    #
    # This was a hardcoded 0.0 with the note "there is no rate card in the pod".
    # Refusing to invent pricing was right; the premise had gone stale. The pod
    # HAS the vendors' rates — warm_model_catalog() fills _CATALOG_CACHE at
    # startup and the Keys lookups fill _DIRECT_MODELS_CACHE, both carrying the
    # input_per_token/output_per_token the model picker renders.
    # cached_model_prices() reads those same caches, so nothing is fetched here.
    #
    # cost_method carries WHY the number is what it is, so a zero can never be
    # mistaken for free: 'list' priced, 'unpriced' no published rate or a cold
    # catalog. Measured on one Host: 256 calls over three months logged
    # $0 while the catalog showed the price of those same models.
    #
    # CACHE-AWARE, and it has to be. prompt_tokens below is
    # the lane-normalized TOTAL input, cached tokens INCLUDED (the Anthropic
    # normalisation adds them at :1253-1255, the OpenAI wire already has them
    # at :1544-1546) — so pricing all of it at the input rate charged every
    # cache hit full freight. Measured hit rates on real hosts are ~84%, and
    # a vendor's cache-read rate is commonly a tenth of its input rate, so the
    # estimate ran several times the bill. The counts are already right here as
    # arguments; only the formula was missing them. `priced` records WHICH rate
    # each part got, because a published cache rate and a fallback to the input
    # rate are different kinds of number and a later pass has to tell them
    # apart — the cost_method vocabulary stays the four values v113 declares.
    _est, _method = 0.0, "unpriced"
    _priced: dict = {}
    # WHICH CREDENTIAL SERVED (sign-in providers only — xai; _call_remote puts
    # it in the metadata). It decides the basis, not the catalog: a call on the
    # user's sign-in spends their plan ('subscription'), a call on the API key
    # bills per token — whatever flag the catalog row carries from the last
    # /models listing, and whatever OpenRouter lists the same model at.
    _cred = metadata.get("credential") if isinstance(metadata, dict) else None
    # Pricing runs for real calls only. A failed call bought nothing, and
    # price_basis() would label it 'free' or 'list' — both of which read as
    # "this happened and cost nothing" rather than "this never landed".
    # 'unpriced' (the v113 default) is the honest label; the vocabulary stays
    # the four values v113 declares and the Compute app renders.
    if purpose == "inference" and not _unknown:
        try:
            from server.routes.inference import estimate_call_cost_detail
            _v, _priced = estimate_call_cost_detail(
                model, int(prompt_tokens or 0), int(completion_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_write_tokens=int(cache_write_tokens or 0),
                provider=provider, credential=_cred)
            if _v is not None:
                _est, _method = float(_v), "list"
            else:
                # Not priced — but say WHICH not-priced: a flat plan has no
                # per-token rate, a local model is genuinely free, and only a
                # missing published rate is truly 'unpriced' (v113's vocabulary).
                from server.routes.inference import price_basis
                _method = price_basis(model, provider, credential=_cred)
        except Exception as _e:
            # A pricing miss must never cost the row. Tokens remain the honest
            # number and the row still lands, labelled 'unpriced'.
            log.debug("inference price lookup unavailable: %s", _e)
    if entity_id is None:
        # entity_id is NULLable in the schema, but every reader filters on it
        # (inference.py:2376 `WHERE entity_id = $1`), so a NULL row is a row
        # the Budget app cannot see. The pod always sets ENTITY_ID (same parse
        # as :1213).
        _eid_s = os.environ.get("ENTITY_ID", "")
        entity_id = int(_eid_s) if _eid_s.isdigit() else None
    try:
        await _inference_pool.execute(
            "INSERT INTO inference_cost_log "
            "(entity_id, source_module, model_id, provider, purpose, "
            "tokens_in, tokens_out, est_cost_usd, cost_method, latency_ms, metadata, "
            # Columns since migrations/v148. They were already in
            # the metadata below and stay there — the JSON is what every row
            # written before v148 has, and the columns are what a SQL rollup can
            # sum without parsing JSON. APPENDED, not inserted mid-list, so the
            # existing positional bindings are untouched.
            "cache_read_tokens, cache_write_tokens) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)",
            entity_id, source, model, provider, purpose,
            # tokens_in is the lane-normalized TOTAL input: on the OpenAI wire
            # prompt_tokens already includes the cached subset (:1379), and the
            # Anthropic path adds cache_read + cache_write before calling
            # (:1102). _call_local (:1648) passes neither, hence the `or 0`.
            # NULL only when the provider reported no usage (v214).
            (None if _unknown else int(prompt_tokens or 0)),
            (None if _unknown else int(completion_tokens or 0)),
            _est, _method,
            int(elapsed_ms),
            json.dumps({
                **(metadata or {}),
                "cache_read_tokens": None if _unknown else int(cache_read_tokens or 0),
                "cache_write_tokens": None if _unknown else int(cache_write_tokens or 0),
                "total_tokens": None if _unknown else int(total_tokens or 0),
                # Only on a row that was actually priced. An empty marker on an
                # unpriced row would claim a derivation that never happened —
                # and its absence is exactly what reprice_cache_blind() uses to
                # find the rows written by the old cache-blind formula.
                **({"priced": _priced} if _priced else {}),
            }),
            (None if _unknown else int(cache_read_tokens or 0)),
            (None if _unknown else int(cache_write_tokens or 0)),
        )
    except Exception as e:
        log.warning("inference_cost_log write failed: %s", e)


def _error_class(result: dict) -> str:
    """The failure's KIND, from the flags call_model's own paths already set.
    Ordered most-specific first: an unconfigured route is not an outage, and
    saying so is the difference between 'go pick a model' and 'wait'."""
    if result.get("killed"):
        return "killed"
    if result.get("unconfigured"):
        return "unconfigured"
    if result.get("lane_unavailable"):
        return "lane_unavailable"
    return "error"


async def _log_call_failure(result, *, source, model, provider, entity_id,
                            call_start, purpose="inference_error"):
    """One ledger row per FAILED model call — the missing half of _log_inference.

    _log_inference is called ONLY from the three success paths (_call_anthropic,
    _call_remote, _call_local). Every failure path beside them records through
    `_prompt_console.log_response(..., error=...)` instead — and
    set_prompt_console (:94, and runtime/triage.py:39) has NO caller anywhere in
    this repo, so _prompt_console is None in the pod and all of those hooks are
    no-ops. Net effect before this: a background role that stopped answering
    left a pod log line and nothing queryable. That is the same silent-lane
    shape as a consolidation outage that ran 16 days unseen.

    Rows land in the SAME ledger under purpose='inference_error', so:
      - cost readers excluding that one value keep their exact counts;
      - the activity feed reads one table for both halves;
      - no migration (purpose is an existing column, previously hardcoded).
    Zero tokens, zero dollars, cost_method 'unpriced' — a call that never
    landed bought nothing. Never raises: telemetry cannot break a turn.
    """
    try:
        err = str(result.get("error") or "")[:400]
        meta = {"error": err, "error_class": _error_class(result)}
        if result.get("picture_refused"):
            meta["picture_refused"] = True
        fo = result.get("failed_over")
        if isinstance(fo, dict):
            # The retry is logged on its own row; this records that the
            # PRIMARY fell over, which is the thing worth alerting on.
            meta["failed_over_to"] = (f"{fo.get('to_provider')}/"
                                      f"{fo.get('to_model')}")
        await _log_inference(
            source=source, model=model, provider=provider,
            total_tokens=0, prompt_tokens=0, completion_tokens=0,
            elapsed_ms=(_time.time() - call_start) * 1000,
            entity_id=entity_id, metadata=meta, purpose=purpose,
        )
    except Exception as e:
        log.warning("inference failure-log write failed: %s", e)


# ---------------------------------------------------------------
# XML Tool Prompt Building (for local models without tool_choice)
# ---------------------------------------------------------------

def build_xml_tool_prompt(tools: list[dict]) -> str:
    lines = [
        "## Tools",
        "You have tools. To use one, output this XML block:",
        "",
        "<function_calls>",
        '<invoke name="TOOL_NAME">',
        '<parameter name="PARAM">VALUE</parameter>',
        "</invoke>",
        "</function_calls>",
        "",
        "Multiple tools: use multiple <invoke> blocks inside one <function_calls>.",
        "After outputting <function_calls>, STOP. Do not add text after it.",
        "If you do not need a tool, just respond normally without XML.",
        "",
        "Available tools:",
    ]
    for tool in tools:
        func = tool.get("function", {})
        name = func.get("name", "unknown")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        lines.append(f"\n**{name}**: {desc}")
        if props:
            lines.append("  Parameters:")
            for pname, pinfo in props.items():
                req_mark = " (REQUIRED)" if pname in required else ""
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                enum = pinfo.get("enum")
                enum_str = f", one of: {enum}" if enum else ""
                lines.append(f"    - {pname} ({ptype}{req_mark}): {pdesc}{enum_str}")
    return "\n".join(lines)


def parse_xml_tool_calls(content: str) -> tuple[str, list[dict]]:
    fc_match = re.search(r'<function_calls>\s*(.*?)\s*</function_calls>', content, re.DOTALL)
    if not fc_match:
        fc_match = re.search(r'<function_calls>\s*(.*)', content, re.DOTALL)
        if not fc_match:
            return content, []

    text_before = content[:fc_match.start()].strip()
    calls_xml = fc_match.group(1)

    tool_calls = []
    for invoke_match in re.finditer(
        r'<invoke\s+name=["\']([^"\']+)["\']\s*>(.*?)</invoke>',
        calls_xml, re.DOTALL,
    ):
        func_name = invoke_match.group(1)
        params_xml = invoke_match.group(2)
        args = {}
        for param_match in re.finditer(
            r'<parameter\s+name=["\']([^"\']+)["\']\s*>(.*?)</parameter>',
            params_xml, re.DOTALL,
        ):
            key = param_match.group(1)
            val = param_match.group(2).strip()
            try:
                args[key] = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                args[key] = val
        tool_calls.append({
            "id": f"xml_{_uuid.uuid4().hex[:12]}",
            "function": {"name": func_name, "arguments": json.dumps(args)},
        })

    return text_before, tool_calls


def strip_thinking(content: str) -> str:
    """Strip reasoning/thinking blocks from model output.

    Handles:
      - DeepSeek-R1 style `<think>...</think>` tags
      - Anthropic extended-thinking `<thinking>...</thinking>`
      - Bare `Thinking Process:` prefix
      - Some local-model patterns that emit `**Thinking:**` / `Reasoning:`
        prefixes before the actual answer.

    The PLAN_OPENERS prefix-strip was removed once the
    directive-synthesis layer switched to internal-state phrasing by
    default (codebook_synthesis.synthesize_turn_directive). The model
    is no longer fed imperative "Be direct.", "Lead with warmth."
    prose to echo back; the source of the planning-prose leaks is
    gone, and the bandage with it.

    Caller is responsible for pulling separate `reasoning_content` /
    `reasoning` fields off the API response — those never enter this
    function. This only handles in-band tags / prefixes the model
    itself emitted inside the `content` string.
    """
    if not content:
        return content
    # In-band <think>...</think> or <thinking>...</thinking> blocks.
    for closer in ("</think>", "</thinking>"):
        if closer in content:
            content = content.split(closer, 1)[1].strip()
    # If everything was wrapped in an open block with no close, drop it.
    if content.startswith(("<think>", "<thinking>")):
        return ""
    # Prefix-only leakage patterns from misbehaving providers.
    lstripped = content.lstrip()
    for prefix in ("Thinking Process:", "**Thinking:**", "Reasoning:", "**Reasoning:**"):
        if lstripped.startswith(prefix):
            return ""
    return content


def _extract_reasoning(msg: dict) -> str:
    """Pull a reasoning string off the OpenAI-style message dict.

    DeepSeek returns `reasoning_content`. OpenRouter normalizes that
    (and Anthropic extended thinking) to `reasoning` in many cases.
    We accept either, prefer reasoning_content (more specific), and
    return "" when neither is present. Caller stores this separately
    from `content` so it never lands in the visible chat reply.
    """
    if not isinstance(msg, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _reasoning_only_reply(msg: dict, content: str) -> bool:
    """True when a reply carried reasoning and NOTHING usable came back.

    Empty content is only a lost answer when there are also no tool_calls. A
    model that reasons and then calls a tool legitimately returns empty content
    — that is an ordinary tool-call round, and warning on it fires on every
    such round for every reasoning model. The real case this catches is a
    background JSON call (no tools) whose whole answer landed in `reasoning`.
    """
    if not isinstance(msg, dict):
        return False
    if content:
        return False
    if msg.get("tool_calls"):
        return False
    return bool(_extract_reasoning(msg))


# ---------------------------------------------------------------
# Cache boundary
# ---------------------------------------------------------------

CACHE_BREAK_MARKER = "<!-- CACHE_BREAK -->"

# Cache-affinity key. Providers shard their prefix cache across replicas, so
# without a stable per-conversation key consecutive turns can land on
# different replicas and miss. MEASURED: sending it is
# what took Together from 36.5% to 62.6% — without it Together evicted
# mid-run; with it the cached read grew cleanly to 15680.
#
# `prompt_cache_key` is the ONE field: probed live against the seven
# OpenAI-wire providers that existed at the time, six of which accept it
# (openrouter, openai, xai, fireworks, deepinfra, together). `session_id` is
# NOT a substitute — it 400s on openai and fireworks — so there is deliberately
# only one name here.
#
# `google` joined the registry after that probe and has NOT been
# measured, so it is deliberately absent from both lists rather than guessed
# into either: absence means "send it", and if Gemini's compat layer rejects
# the field the runtime path below records it in _CACHE_KEY_REJECTED and
# retries once, process-scoped. Costs one call, once — the safe direction. Move
# google into _CACHE_KEY_UNSUPPORTED only on a measurement, never on a hunch.
_CACHE_KEY_UNSUPPORTED: frozenset = frozenset({
    # 400 "property 'prompt_cache_key' is unsupported"; caches nothing anyway.
    "groq",
})
# Providers observed rejecting the key at RUNTIME (a vendor changed its API).
# Process-scoped so the retry costs one call, once, not one per turn.
_CACHE_KEY_REJECTED: set = set()


def split_cache_break(content: str) -> tuple[str, str | None]:
    """Split assembled system content at the CACHE_BREAK marker into
    (static_prefix, per_turn_suffix). No marker (or a non-string content)
    → (content, None), so callers stay byte-identical."""
    if not isinstance(content, str) or CACHE_BREAK_MARKER not in content:
        return content, None
    static, _, dynamic = content.partition(CACHE_BREAK_MARKER)
    return static.rstrip(), dynamic.strip()


def _apply_cache_boundary(messages: list[dict], model: str) -> list[dict]:
    """Turn the inert CACHE_BREAK marker into a real provider cache hint.

    Claude models on the openrtr lane (OpenRouter passes Anthropic-protocol
    fields through verbatim — the same passthrough the cache_read/creation
    usage parse in _call_remote reads back): the leading system message
    becomes a two-block content array — static prefix FIRST and marked
    cache_control {"type": "ephemeral"}, per-turn suffix unmarked.

    Every other lane/model returns `messages` UNCHANGED (byte-identical):
    OpenAI-style automatic prefix caching already keys on the byte-stable
    leading prefix of the single system string, and the claude-sdk lane's
    CLI manages its own breakpoints (its cache telemetry already flows).

    The marker is now usually ABSENT here — session.py splits it off and puts
    the per-turn half after history (cache-stable wire shape). A missing
    marker therefore means "the whole system message IS the stable prefix",
    not "don't cache": returning early on that would silently drop the hint
    for exactly the shape it was built to reward."""
    if not messages or messages[0].get("role") != "system":
        return messages
    from mastercode.inference_config import _is_claude_model
    if not _is_claude_model(model):
        return messages
    static, dynamic = split_cache_break(messages[0].get("content"))
    if not (static or "").strip():
        return messages          # cache_control on an empty block is a 400
    blocks = [{
        "type": "text",
        "text": static,
        "cache_control": {"type": "ephemeral"},
    }]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return [{"role": "system", "content": blocks}, *messages[1:]]


# ---------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------

async def _load_user_openrouter_key() -> str:
    """The user's OpenRouter key from the Keys app vault (canonical logical name
    'openrouter', sealed to this host — the platform holds ciphertext only).

    Resolution ladder, self-healing toward canonical:
      1-2. the shared typed resolver (server/vault/providers.resolve): exact
         vault name 'openrouter', then any free-typed api-key entry that
         normalizes to openrouter ('Open Router') — re-stored under the
         canonical name so the exact rung hits next time
      3. legacy plaintext behavioral_params.user_openrouter_key (the old
         Orchestrator BYOK panel) — migrated INTO the vault and
         stripped from behavioral_params, one-time; openrouter-specific, so
         it stays here rather than in the shared resolver
      4. env OPENROUTER_API_KEY (platform/legacy pods)
    """
    try:
        from server import brain_service
        from server.vault import api as _vault
        from server.vault import providers as _providers
        pool = brain_service.get_pool()
        eid_s = os.environ.get("ENTITY_ID", "")
        if pool is not None and eid_s.isdigit():
            eid = int(eid_s)
            cred = await _providers.resolve(
                pool, eid, "openrouter", ("api_key",),
                actor="inference", env_fallback=False,
            )
            if cred:
                return cred.secret
            # 3. legacy plaintext behavioral_params copy → migrate + strip
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid,
            )
            params = row["behavioral_params"] if row else None
            if isinstance(params, str):
                params = json.loads(params)
            legacy = (params or {}).get("user_openrouter_key", "").strip()
            if legacy:
                await _vault.store_secret(
                    pool, entity_id=eid, name="openrouter", cred_type="api_key",
                    plaintext=legacy.encode("utf-8"), actor="inference",
                )
                params.pop("user_openrouter_key", None)
                await pool.execute(
                    "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
                    json.dumps(params), eid,
                )
                log.info("legacy plaintext openrouter key migrated to the vault and stripped")
                return legacy
    except Exception as exc:
        log.warning("vault openrouter key read failed (falling back to env): %s", exc)
    return OPENROUTER_API_KEY


async def _iter_sse_lines(content):
    """Yield newline-delimited lines from an aiohttp body with NO cap on how
    long one line may be.

    `async for line in resp.content` is StreamReader.readline(), which raises
    LineTooLong once a single line passes read_bufsize*2 — 524288 bytes on
    aiohttp's default, which this tree never overrides. One SSE `data:` line is
    a whole chunk object, so a single fat delta (a large tool-call argument
    blob) takes the turn down with it.

    That ceiling is not hypothetical: it killed one long build's terminal
    frame, discarding a build that had already succeeded. See
    server.routes.review._iter_ndjson_lines for the full measurement.

    iter_any() -> readany(), which has no size check and waits through the same
    timer, so the sock_read window that catches a dead stream is unchanged.

    Same framing the two OpenRouter readers below already do inline; this exists
    so the local reader can adopt it without reindenting its body.
    """
    buf = b""
    async for chunk in content.iter_any():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line
    if buf:
        yield buf


# The close of reasoning written inline in the text (strip_thinking reads the
# same two tags).
_INLINE_REASONING_CLOSE = re.compile(r"</think>|</thinking>", re.I)


async def _consume_openrouter_stream(resp, stream_cb, *, reasoning_inline: bool = False,
                                     on_alive=None) -> dict:
    """Accumulate an OpenRouter SSE stream into a chat.completion-shaped dict,
    awaiting stream_cb(text) per visible content delta. Reasoning deltas are
    accumulated separately and NEVER streamed to the user (same rule as the
    buffered path's reasoning split). The final chunk's usage rides
    stream_options.include_usage. Serves every OpenAI-wire provider
    _call_remote dials (openrouter, openai, xai, kimi and the other registry
    providers on that wire, or a custom base_url).

    `reasoning_inline`: the route's model writes its reasoning
    in `content` itself, closed by </think> — often with no opening tag (a
    chat template that opens the block in the prompt), so the session's
    stream filter, which holds a block only from its opening tag, cannot see
    it start. Content is then held back until the close: what comes before
    it is reasoning (kept with `reasoning`, never streamed, as strip_thinking
    drops it from the final content), and what follows streams as it comes.
    A completion that never closes was not reasoning: it is sent when the
    stream ends, as strip_thinking keeps it (an unclosed block that OPENS
    with <think> is dropped, as there).

    The hold is only for reasoning inline in the content.
    A provider that returns the reasoning in its OWN field — a delta with
    `reasoning` / `reasoning_content` / `reasoning_details`: OpenRouter's
    normalised reasoning, Groq's `reasoning_format: "parsed"` — writes only
    the reply in `content`, and no </think> ever comes: from that delta on
    the content streams as it comes (what was held before it goes first).
    A caller that asked for the reasoning apart (_call_remote, Groq parsed)
    passes reasoning_inline False and nothing is held at all."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    # Content held back while a reasoning_inline route may still be writing
    # reasoning (None: streaming as it comes).
    inline_hold: list[str] | None = [] if reasoning_inline else None
    inline_lstrip = False       # drop the blank space right after the close
    tool_calls: dict[int, dict] = {}
    usage: dict = {}
    model_id = ""
    resp_id = ""
    finish_reason = None
    buf = b""
    async for chunk in resp.content.iter_any():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if on_alive is not None:
                on_alive()     # every event is a sign of life (reasoning included)
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            model_id = obj.get("model") or model_id
            # The completion's id, on every chunk: the provider's handle on
            # this response (the per-call ledger keeps it, request_id).
            resp_id = obj.get("id") or resp_id
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                # Why the model stopped: "length" is the
                # output limit, read by the caller as a cut — never dropped.
                finish_reason = ch.get("finish_reason") or finish_reason
                delta = ch.get("delta") or {}
                if inline_hold is not None and (
                        delta.get("reasoning") or delta.get("reasoning_content")
                        or delta.get("reasoning_details")):
                    # The reasoning comes in its own field: the content is
                    # the reply, and streams as it comes.
                    _was_held, inline_hold = "".join(inline_hold), None
                    if _was_held:
                        await stream_cb(_was_held)
                txt = delta.get("content")
                if txt:
                    content_parts.append(txt)
                    if inline_hold is not None:
                        inline_hold.append(txt)
                        held = "".join(inline_hold)
                        m = _INLINE_REASONING_CLOSE.search(held)
                        if m is None:
                            txt = ""
                        else:
                            reasoning_parts.append(re.sub(
                                r"^\s*<think(?:ing)?>", "", held[:m.start()], flags=re.I))
                            txt, inline_hold, inline_lstrip = held[m.end():], None, True
                    if inline_lstrip and txt:
                        txt = txt.lstrip()
                        inline_lstrip = not txt
                    if txt:
                        await stream_cb(txt)
                r = delta.get("reasoning") or delta.get("reasoning_content")
                if r:
                    reasoning_parts.append(r)
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    slot = tool_calls.setdefault(i, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
    if inline_hold:
        # Never closed: it was the reply, not reasoning (strip_thinking).
        rest = strip_thinking("".join(inline_hold).strip())
        if rest:
            await stream_cb(rest)
    msg: dict = {"content": "".join(content_parts)}
    if reasoning_parts:
        msg["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"choices": [{"message": msg, "finish_reason": finish_reason}],
            "usage": usage, "model": model_id, **({"id": resp_id} if resp_id else {})}


# ── Why a model stopped ─────────────────────
# Without this, the Anthropic parser kept only usage, and a tool
# call the output limit cut mid-arguments ran as `{}` — the Host was told
# "operate requires `tool`" and blamed itself. These are the stop reasons that
# mean the model was STOPPED, not finished: the output limit on both wires,
# and the context window (Anthropic 4.5+: model_context_window_exceeded).
_CUT_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded", "length"})

# A call whose output may run past this many tokens is streamed even when the
# caller did not ask for a stream: Anthropic's own guidance is to stream above
# ~16K output ("non-streaming requests hit SDK HTTP timeouts at high
# max_tokens" — idle connections drop), and this lane rides a proxy. Up to
# this value the call is sent exactly as before.
_STREAM_ABOVE = 16384


def _web_plugin_model(model: str | None) -> bool:
    """OpenRouter's web plugin (`<model>:online`): its sources come back as
    `annotations` on the buffered message — the only place OpenRouter's web
    search page documents them (openrouter.ai/docs/features/web-search, read
    2026-10-02; nothing there on a streamed shape) — so such a call is not
    streamed by the gateway for its length."""
    return str(model or "").endswith(":online")


async def _discard_stream(_text: str) -> None:
    """stream_cb for a call the gateway streams for its length only."""
    return None


def _stop_fields(stop_reason, tool_calls: list | None,
                 incomplete_ids: list | None, max_tokens) -> dict:
    """The result keys every lane adds: `stop_reason` (the provider's own
    string), `output_limit` (the max_tokens sent), and — when the model was
    stopped by the limit — `truncated: True` with `truncated_tool_call_ids`:
    the calls it was still writing. On Anthropic that is the tool_use block
    that never closed (streamed) or the last one (buffered: the API gives the
    cut block's partial input, which can read as a valid `{}`); on the OpenAI
    wire the last call, and any whose arguments do not parse."""
    out: dict = {"stop_reason": stop_reason, "output_limit": max_tokens}
    if str(stop_reason or "") not in _CUT_STOP_REASONS:
        return out
    out["truncated"] = True
    ids: list[str] = list(incomplete_ids or [])
    calls = [c for c in (tool_calls or []) if isinstance(c, dict)]
    if calls and incomplete_ids is None:
        last = calls[-1]
        if last.get("id"):
            ids.append(str(last["id"]))
    # Any call whose arguments do not parse in a completion the limit stopped
    # is a cut call too — whichever way the provider closed its block
    # (nothing verifies a cut block never gets content_block_stop).
    for c in calls:
        try:
            json.loads((c.get("function") or {}).get("arguments") or "")
        except (ValueError, TypeError):
            if c.get("id") and str(c["id"]) not in ids:
                ids.append(str(c["id"]))
    out["truncated_tool_call_ids"] = ids
    return out


# What an OUTPUT-limit refusal names: the output parameter or
# the output limit itself — "output token limit", "completion token limit" and
# "output limit" too, so a refusal that also says "token limit" is read here,
# first, and learned, never taken for a window overflow. One list, shared with
# the turn's overflow test (context_budget.is_context_overflow).
# Each needle is from a provider's REAL output-limit refusal:
#   Anthropic "max_tokens: 128001 > 128000, which is the maximum allowed number
#   of output tokens for claude-opus-4-6" (anthropics/claude-code#23923);
#   OpenAI "max_tokens is too large: 5500. This model supports at most 4096
#   completion tokens, whereas you provided 5500." (openai/openai-python#687).
# No provider's real string was found for "output limit" / "output token
# limit" / "completion token limit" / "max_output_tokens", so none is here.
_OVERSIZE_NEEDLES = ("max_tokens", "output tokens", "completion tokens")


def _refused_output(status: int, body: str, sent: int | None) -> tuple[int, bool] | None:
    """When a provider refuses a call's max_tokens: (the max_tokens to resend
    THIS call with, whether that is the MODEL's limit to remember), else None.

    Two different refusals:
      - the window: Anthropic "input length and `max_tokens` exceed context
        limit: A + B > C", the OpenAI/vLLM "maximum context length is C …
        (A in the messages, B in the completion)". The room C − A belongs to
        THIS call's input only — resent at it, never remembered (remembering
        it shrank claude-opus-5-5 from 128,000 to 4,000 for the pod's life);
      - the model's own output maximum: Anthropic "max_tokens: 200000 >
        128000, which is the maximum allowed number of output tokens", OpenAI
        "supports at most 128000 completion tokens". Resent at it AND
        remembered (learn_output_limit).
    A refusal of the size that names no number is resent once at the size every
    call carried before (16,384), not remembered."""
    if status != 400 or not sent or not body:
        return None
    # Everything below reads the refusal's own MESSAGE — never ids or other
    # fields of the body — except the typed code, read from
    # its field.
    from mastercode.context_budget import refusal_parts as _rp
    _msg, _code = _rp(body)
    low = _msg.lower()
    # kimi's window refusal: "Your request exceeded model token limit: 262144
    # (requested: 269030)" (MoonshotAI/kimi-cli#2011) — the number is the
    # WINDOW and "requested" is input + max_tokens: this call is resent at the
    # room left, and nothing is learned.
    mk = re.search(r"token limit:\s*(\d[\d,]*)\s*\(requested:\s*(\d[\d,]*)\)", low)
    if mk:
        win, req = (int(x.replace(",", "")) for x in mk.groups())
        room = win - (req - int(sent))
        return (room, False) if 0 < room < sent else None
    # kimi: "prompt tokens + max_tokens exceeds the model specification"
    # (platform.kimi.ai/docs/api/errors) — the combined size, no numbers: this
    # call at 16,384, never learned.
    if "exceeds the model specification" in low:
        from mastercode.context_budget import DEFAULT_OUTPUT_RESERVE as _d1
        return (_d1, False) if sent > _d1 else None
    m = re.search(r"(\d[\d,]*)\s*\+\s*(\d[\d,]*)\s*>\s*(\d[\d,]*)", _msg)
    if m and ("context" in low or "max_tokens" in low):
        a, _b, c = (int(x.replace(",", "")) for x in m.groups())
        room = c - a
        return (room, False) if 0 < room < sent else None
    mc = re.search(r"maximum context length is\s*(\d[\d,]*)", low)
    mi = (re.search(r"(\d[\d,]*)\s*in the messages", low)
          or re.search(r"(\d[\d,]*)\s*of text input", low))
    if mc and mi:
        room = int(mc.group(1).replace(",", "")) - int(mi.group(1).replace(",", ""))
        return (room, False) if 0 < room < sent else None
    from mastercode.context_budget import DEFAULT_OUTPUT_RESERVE as _d
    # The model's own OUTPUT maximum first: a refusal that
    # names the output parameter and a number below what was sent is that,
    # whatever else its sentence says — learned, so it is sent right the next
    # time. Only then the window-overflow shapes.
    # The refusal's MESSAGE, not its JSON envelope: a body's "code": 400 is
    # not a limit (it was once learned as one). The output test is the
    # turn's own (context_budget._is_output_refusal: output wording and no
    # window wording), so both sides read a refusal the same way.
    from mastercode.context_budget import _is_output_refusal
    _names_output = _is_output_refusal(_msg.lower())
    if _names_output:
        nums = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]{2,}", _msg)]
        below = [n for n in nums if 0 < n < sent]
        if below:
            return max(below), True
    if _typed_window_overflow(body):
        # The provider says input + output exceed the WINDOW but names no
        # numbers: resend THIS call at what main always sent, never
        # remembered — the output side is fixed before any history is cut
        # (the underestimate path).
        return (_d, False) if sent > _d else None
    if not _names_output:
        return None
    return (_d, False) if sent > _d else None


# Words a window-overflow refusal carries when its provider documents only the
# error TYPE (kimi: "If input plus max_completion_tokens exceeds the model
# context window, the API returns invalid_request_error" — platform.kimi.ai
# /docs/api/chat, read 2026-10-02, no message wording published).
# Window wording. "exceed" alone is not window wording (it also describes an
# unrelated count). "token limit" IS kept: kimi's
# own refusal reads "Your request exceeded model token limit: 262144", and an
# OUTPUT-limit refusal that also says "token limit" is already read first, by
# _refused_output's output check, so nothing is mistaken for it.
_WINDOW_WORDS = re.compile(r"context (?:window|length)|maximum context|token limit", re.I)


def _typed_window_overflow(body: str) -> bool:
    """A 400 whose JSON says input + output exceed the window, by type:
    OpenRouter's typed error.metadata.error_type 'context_length_exceeded'
    ("The combined input and output tokens exceed the model's context window"
    — openrouter.ai/docs/api-reference/errors, read 2026-10-02), or an
    `invalid_request_error` whose message speaks of the context window or
    length, or a model token limit (kimi documents the type only, so the
    wording test is ours; "exceed" alone is not a window)."""
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return False
    err = obj.get("error") if isinstance(obj, dict) else None
    if not isinstance(err, dict):
        return False
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    if str(meta.get("error_type") or err.get("code") or "") == "context_length_exceeded":
        return True
    return (str(err.get("type") or "") == "invalid_request_error"
            and bool(_WINDOW_WORDS.search(str(err.get("message") or ""))))


def _learned_output_limit(status: int, body: str, sent: int | None) -> int | None:
    """The MODEL's output limit a refusal states, to remember — None for a
    refusal about the window (that room is one call's, _refused_output)."""
    r = _refused_output(status, body, sent)
    return r[0] if r and r[1] else None


# ---------------------------------------------------------------
# Anthropic call (Messages API — the user's own 'anthropic' Keys entry)
# ---------------------------------------------------------------

_ANTHROPIC_VERSION = "2023-06-01"

# The Messages API validates tool names against ^[a-zA-Z0-9_-]{1,128}$ — both
# in the tools array and in replayed tool_use history blocks. The public tool
# registry deliberately uses dotted names (review.compare, system.restart_app,
# codex.run…), which 400 the whole request on this wire while passing on the
# OpenAI wire. Sanitize names on the wire ONLY; dispatch and history keep the
# canonical names via the per-request map below (root-caused from a live
# "tools.24.custom.name" 400).
_ANTHROPIC_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _anthropic_safe_name(name: str) -> str:
    return _ANTHROPIC_TOOL_NAME_RE.sub("_", name or "")[:128] or "_"


def _tool_name_map(tools, safe_fn) -> dict[str, str]:
    """Wire-safe name -> canonical name for this request's tool list, using
    `safe_fn` as the per-wire sanitizer. Collisions (two canonical names
    sanitizing identically) disambiguate with a numeric suffix so two tools
    can never alias one wire name."""
    mapping: dict[str, str] = {}
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        safe = safe_fn(name)
        if safe in mapping and mapping[safe] != name:
            i = 2
            while f"{safe}_{i}" in mapping and mapping[f"{safe}_{i}"] != name:
                i += 1
            safe = f"{safe}_{i}"
        mapping[safe] = name
    return mapping


def _anthropic_tool_name_map(tools) -> dict[str, str]:
    return _tool_name_map(tools, _anthropic_safe_name)


# Direct OpenAI-wire providers validate function names too. Kimi's API requires
# ^[A-Za-z][A-Za-z0-9_-]*$ — measured live on a workbench
# turn, provider=kimi model=k3: `kimi 400: function name is invalid, must start
# with a letter and can contain letters, numbers, underscores, and dashes` —
# and OpenAI's own spec allows [A-Za-z0-9_-]{1,64}. The registry's dotted names
# (review.compare, ladder.run, system.restart_app, <harness>.run) 400 the whole
# request there, exactly as they did on the Messages wire above. Same remedy:
# sanitize on the wire ONLY and map the response back, so dispatch and stored
# history only ever see canonical names. OpenRouter too: it
# forwards names to the upstream model's API, which applies its own rule (see
# the call site in _call_remote).
_WIRE_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _openai_wire_safe_name(name: str) -> str:
    safe = _WIRE_TOOL_NAME_RE.sub("_", name or "") or "_"
    if not safe[0].isalpha():
        safe = "t_" + safe
    return safe[:64]


def _openai_wire_tool_name_map(tools) -> dict[str, str]:
    return _tool_name_map(tools, _openai_wire_safe_name)


def _history_tool_stubs(messages, keep=()) -> list[dict]:
    """Tool-shaped stubs for every tool name REPLAYED in the history.

    A round sent with no tools at all (the text-only fallback and forced-text
    rounds in session.py) still replays earlier assistant tool_calls and tool
    results under their registry names — and the provider validates those too.
    Measured via openrouter -> openai/gpt-4o-mini with tools absent:
    history name 'ladder.run' -> 400 "Invalid 'messages[1].tool_calls[0]
    .function.name'", 'ladder_run' -> 200. So the wire map has to cover names
    the history carries, not only the ones in the tools array.

    `keep` holds names that must go out EXACTLY as they are: a provider's own
    built-in tools (Kimi's `$web_search`, declared through native_tools) are
    echoed back in history under their built-in name, and the provider matches
    on it — renaming that echo to t__web_search would break the search it
    injects. Anything that is not a well-formed message or tool
    call is skipped rather than raised on: this runs on every call now."""
    seen, out = set(keep), []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        # Assistant tool_calls only — the same shape _openai_wire_rename renames,
        # so the two helpers can never disagree about what is in play.
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        names = [((tc.get("function") or {}) if isinstance(tc, dict) else {}).get("name")
                 for tc in (calls if isinstance(calls, list) else [])]
        if m.get("role") == "tool":
            names.append(m.get("name"))
        for n in names:
            if isinstance(n, str) and n and n not in seen:
                seen.add(n)
                out.append({"function": {"name": n}})
    return out


def _openai_wire_rename(tools, messages, name_map: dict[str, str]):
    """Return (tools, messages) with canonical tool names replaced by their
    wire-safe form — in the tools array AND in replayed assistant tool_calls /
    tool-result `name` fields (a provider that validates the schema names
    validates the history against them too). Copies only what it touches;
    the caller's lists are never mutated. Identity when nothing needs renaming."""
    to_wire = {v: k for k, v in name_map.items()}
    if all(k == v for k, v in to_wire.items()):
        return tools, messages
    new_tools = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        cname = fn.get("name")
        if cname and to_wire.get(cname, cname) != cname:
            new_tools.append({**t, "function": {**fn, "name": to_wire[cname]}})
        else:
            new_tools.append(t)
    new_msgs = []
    for m in messages or []:
        if not isinstance(m, dict):
            new_msgs.append(m)
            continue
        if (m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list)
                and m["tool_calls"]):
            tcs = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    tcs.append(tc)
                    continue
                fn = tc.get("function") or {}
                cname = fn.get("name") or ""
                # Only names this request mapped are renamed. A name outside the
                # map (a provider built-in, kept out on purpose) goes as it came.
                wire = to_wire.get(cname, cname)
                tcs.append({**tc, "function": {**fn, "name": wire}}
                           if wire != cname else tc)
            new_msgs.append({**m, "tool_calls": tcs})
        elif m.get("role") == "tool" and isinstance(m.get("name"), str) and m["name"]:
            cname = m["name"]
            wire = to_wire.get(cname, cname)
            new_msgs.append({**m, "name": wire} if wire != cname else m)
        else:
            new_msgs.append(m)
    return new_tools, new_msgs


def _openai_wire_restore(msg: dict, name_map: dict[str, str]) -> None:
    """Map a response message's tool_calls back from wire-safe to canonical
    names, in place. No-op without a map."""
    if not name_map:
        return
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        w = fn.get("name") or ""
        if w in name_map:
            fn["name"] = name_map[w]


def _anthropic_messages_payload(messages, model, temperature, max_tokens, tools,
                                name_map=None, tool_choice=None):
    """OpenAI-shaped messages/tools → Messages API payload.

    - System messages lift to the top-level `system` param; a CACHE_BREAK
      marker in the joined system text splits it into two blocks with
      cache_control on the static prefix (the anthropic-side cache hint —
      same semantics as _apply_cache_boundary on the OpenRouter path).
    - Content becomes BLOCKS, never bare strings: assistant tool_calls →
      tool_use blocks; role:"tool" results → tool_result blocks in a user
      turn (consecutive results merge — parallel calls require it);
      consecutive same-role messages merge their blocks (the API requires
      strict user/assistant alternation). Empty text turns are DROPPED —
      the API 400s on empty content, which is exactly what an assistant
      message that only carried tool_calls would become if flattened to
      text.
    - OpenAI tool schemas become {name, description, input_schema}.
    - `temperature` is NEVER sent: Anthropic's current models 400 on it
      (`temperature is deprecated for this model`). The argument is accepted
      and ignored so the call_model contract stays provider-independent.
    """
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # OpenAI content parts
            return "".join(p.get("text", "") for p in content
                           if isinstance(p, dict) and p.get("type") == "text")
        return "" if content is None else str(content)

    def _blocks_of(content) -> list[dict]:
        """User-turn content → Anthropic content blocks. Strings become one
        text block; OpenAI content-part lists keep text parts and convert
        image_url parts (data:/http URLs) to native image blocks; parts
        already Anthropic-native (image/document from the attachments path,
        session.py) pass through. Empty text is dropped, never sent."""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content.strip() else []
        if not isinstance(content, list):
            text = "" if content is None else str(content)
            return [{"type": "text", "text": text}] if text.strip() else []
        blocks: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                t = part.get("text") or ""
                if t.strip():
                    blocks.append({"type": "text", "text": t})
            elif ptype == "image_url":
                url = str((part.get("image_url") or {}).get("url") or "")
                header, comma, data = url.partition(",")
                params = header.split(";")
                if (comma and params[0][:5].lower() == "data:"
                        and params[-1].strip().lower() == "base64"):
                    # Base64 only when its header says so; a data: URL's
                    # scheme and `base64` may be written in any case.
                    blocks.append({"type": "image", "source": {
                        "type": "base64",
                        "media_type": params[0][5:].strip().lower() or "image/jpeg",
                        "data": data}})
                elif url.startswith(("http://", "https://")):
                    blocks.append({"type": "image",
                                   "source": {"type": "url", "url": url}})
                else:
                    # Never dropped without a word: the model is told a
                    # picture was there and could not be sent.
                    blocks.append({"type": "text", "text": (
                        "[A picture is not shown here: it was given as "
                        + ("a data: URL that is not base64" if url[:5].lower() == "data:"
                           else "no url" if not url else "a url that is not http(s)")
                        + ", which this provider does not take.]")})
            elif ptype in ("image", "document"):
                blocks.append(part)
        return blocks

    if name_map is None:
        name_map = _anthropic_tool_name_map(tools)
    # canonical -> wire-safe (history tool_use names must match the tools
    # array; names of tools no longer offered still sanitize deterministically)
    _to_wire = {v: k for k, v in name_map.items()}

    system_parts: list[str] = []
    converted: list[dict] = []

    def _append(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})

    for _i, msg in enumerate(messages):
        role = (msg or {}).get("role")
        content = (msg or {}).get("content")
        if role == "system":
            text = _text_of(content)
            if not text.strip():
                continue
            # Only a LEADING system message lifts to the top-level `system`
            # param. A later one is per-turn context that session.py placed
            # after history on purpose (cache-stable wire shape) — hoisting it
            # would drag it back in front of history and re-break the prefix,
            # which is the exact bug this shape exists to fix. Carry it as a
            # user turn instead: the Messages API's mid-conversation system
            # role is model-gated (not on every Claude model we route to),
            # while a user turn works everywhere, and a user turn is the shape
            # the A/B actually measured at 62.8%.
            if not converted:
                system_parts.append(text)
            else:
                _append("user", [{"type": "text", "text": text}])
            continue
        if role == "assistant":
            blocks: list[dict] = []
            text = _text_of(content)
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(inp, dict):
                        inp = {"_value": inp}
                except Exception:
                    inp = {"_raw": fn.get("arguments") or ""}
                _cname = fn.get("name") or ""
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{_uuid.uuid4().hex[:12]}",
                    "name": _to_wire.get(_cname) or _anthropic_safe_name(_cname),
                    "input": inp,
                })
            _append("assistant", blocks)
            continue
        if role == "tool":
            _append("user", [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "",
                "content": _text_of(content),
            }])
            continue
        # user (and anything else) rides as a user turn — content blocks, so
        # attachments (images/documents) survive onto this wire; empty turns
        # are dropped, never sent (the API rejects empty content).
        _append("user", _blocks_of(content))

    if not converted:
        converted = [{"role": "user",
                      "content": [{"type": "text", "text": "(empty)"}]}]
    if converted[0]["role"] != "user":
        converted.insert(0, {"role": "user",
                             "content": [{"type": "text",
                                          "text": "(continuation)"}]})
    # No `temperature` on this wire. Anthropic's current models reject the
    # knob outright — measured live through
    # this exact path (claude-fable-5-1): `anthropic 400: "temperature" is
    # deprecated for this model.` Every turn failed. The parameter stays in
    # the signature so callers are unchanged; the model's own default applies.
    payload: dict = {
        "model": model,
        "messages": converted,
        "max_tokens": max_tokens,
    }
    if system_parts:
        # Breakpoint 1 — the system prefix, ALWAYS marked when non-empty.
        # It used to be marked only when a CACHE_BREAK marker was present;
        # session.py now splits the marker off before it gets here, so that
        # condition would silently drop the system cache hint entirely. An
        # empty static prefix is never marked — cache_control on an empty
        # text block is a 400.
        static, dynamic = split_cache_break("\n\n".join(system_parts))
        blocks = []
        if static.strip():
            blocks.append({"type": "text", "text": static,
                           "cache_control": {"type": "ephemeral"}})
        if dynamic:
            blocks.append({"type": "text", "text": dynamic})
        if blocks:
            payload["system"] = blocks

    # Breakpoint 2 — the end of HISTORY. This is the one that makes the thread
    # itself cacheable rather than just the system prompt: with the per-turn
    # block moved after history, everything up to here is byte-stable turn to
    # turn. Measured — cached read went from pinned at
    # 1526 every turn to 1526 -> 6553 -> 11523 -> 16537.
    # converted[-1] is the final user turn (per-turn block + this turn's
    # message, merged by _append), which varies and is deliberately unmarked.
    # Copy rather than mutate: _blocks_of passes caller-owned image/document
    # parts through by reference. Max 4 breakpoints per request; this is #2.
    if len(converted) >= 2:
        _tail = converted[-2].get("content") or []
        if _tail and isinstance(_tail[-1], dict):
            _tail[-1] = {**_tail[-1], "cache_control": {"type": "ephemeral"}}
    if tools:
        atools = []
        for t in tools:
            fn = (t or {}).get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            atools.append({
                "name": _to_wire.get(name) or _anthropic_safe_name(name),
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters")
                                or {"type": "object", "properties": {}},
            })
        if atools:
            payload["tools"] = atools
            # "none": the same tools ride, and no call may be made — a
            # search-mode spend stop keeps the cached list (tool_choice
            # touches only the message blocks of the cache).
            payload["tool_choice"] = ({"type": "none"} if tool_choice == "none"
                                      else {"type": "auto"})
    return payload


def _anthropic_citations(relay: dict) -> list[dict]:
    """[{url_citation:{url,title}}] from a Messages response that used the
    server web_search tool: text blocks carry `citations` (type
    web_search_result_location: url, title) and web_search_tool_result
    blocks carry the result list (type web_search_result: url, title).
    Deduped by URL; [] when the response used no search."""
    out: list[dict] = []
    seen: set = set()

    def _add(url, title):
        u = (url or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append({"url_citation": {"url": u, "title": title or u}})

    for block in (relay or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            for c in block.get("citations") or []:
                if isinstance(c, dict) and c.get("url"):
                    _add(c.get("url"), c.get("title"))
        elif btype == "web_search_tool_result":
            content = block.get("content")
            if isinstance(content, list):
                for r in content:
                    if isinstance(r, dict) and r.get("url"):
                        _add(r.get("url"), r.get("title"))
    return out


def _anthropic_result_message(relay: dict, name_map=None) -> tuple[str, str, list]:
    """Buffered Messages response → (content, reasoning, tool_calls).
    thinking blocks accumulate into `reasoning` (never the visible reply);
    tool_use blocks become OpenAI-style tool_calls (names mapped back from
    the wire-safe form to the canonical registry names)."""
    name_map = name_map or {}
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in (relay or {}).get("content") or []:
        btype = (block or {}).get("type")
        if btype == "text":
            content_parts.append(block.get("text") or "")
        elif btype == "thinking":
            reasoning_parts.append(block.get("thinking") or "")
        elif btype == "tool_use":
            _wname = block.get("name") or ""
            tool_calls.append({
                "id": block.get("id") or f"toolu_{_uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name_map.get(_wname, _wname),
                             "arguments": json.dumps(block.get("input") or {})},
            })
    return "".join(content_parts), "".join(reasoning_parts), tool_calls


async def _consume_anthropic_stream(resp, stream_cb, name_map=None, on_alive=None) -> dict:
    """Accumulate a Messages SSE stream into the OpenAI-shaped relay dict the
    remote path produces: text deltas stream via stream_cb; thinking deltas
    accumulate into `reasoning` (never streamed); tool_use blocks assemble
    from input_json_delta partials."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_blocks: dict[int, dict] = {}
    usage: dict = {}
    model_id = ""
    stop_reason = None
    # Blocks in the shape a buffered response carries them, for the citation
    # reader (_anthropic_citations) — a non-streamed call that the gateway
    # streams for its length (_STREAM_ABOVE) keeps its sources.
    blocks: dict[int, dict] = {}
    buf = b""
    async for chunk in resp.content.iter_any():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            if on_alive is not None:
                on_alive()     # every event is a sign of life (thinking included)
            try:
                obj = json.loads(line[5:].strip())
            except Exception:
                continue
            etype = obj.get("type")
            if etype == "message_start":
                msg = obj.get("message") or {}
                model_id = msg.get("model") or model_id
                usage.update(msg.get("usage") or {})
            elif etype == "content_block_start":
                cb = obj.get("content_block") or {}
                idx = obj.get("index", 0)
                if cb.get("type") == "tool_use":
                    tool_blocks[idx] = {
                        "id": cb.get("id") or "",
                        "name": cb.get("name") or "",
                        "arguments": "",
                        # True at content_block_stop: a block the output
                        # limit cut never gets one.
                        "complete": False,
                    }
                elif cb.get("type") in ("text", "web_search_tool_result"):
                    blocks[idx] = {**cb, "citations": list(cb.get("citations") or [])}
            elif etype == "content_block_delta":
                delta = obj.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    txt = delta.get("text") or ""
                    if txt:
                        content_parts.append(txt)
                        await stream_cb(txt)
                elif dtype == "thinking_delta":
                    reasoning_parts.append(delta.get("thinking") or "")
                elif dtype == "input_json_delta":
                    slot = tool_blocks.get(obj.get("index", 0))
                    if slot is not None:
                        slot["arguments"] += delta.get("partial_json") or ""
                elif dtype == "citations_delta":
                    blk = blocks.get(obj.get("index", 0))
                    if blk is not None and isinstance(delta.get("citation"), dict):
                        blk["citations"].append(delta["citation"])
            elif etype == "content_block_stop":
                slot = tool_blocks.get(obj.get("index", 0))
                if slot is not None:
                    slot["complete"] = True
            elif etype == "message_delta":
                usage.update(obj.get("usage") or {})
                stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason
    msg: dict = {"content": "".join(content_parts)}
    if reasoning_parts:
        msg["reasoning"] = "".join(reasoning_parts)
    incomplete: list[str] = []
    if tool_blocks:
        _nm = name_map or {}
        msg["tool_calls"] = []
        for _, b in sorted(tool_blocks.items()):
            tid = b["id"] or f"toolu_{_uuid.uuid4().hex[:12]}"
            msg["tool_calls"].append(
                {"id": tid, "type": "function",
                 "function": {"name": _nm.get(b["name"], b["name"]),
                              "arguments": b["arguments"] or "{}"}})
            if not b["complete"]:
                incomplete.append(tid)
    return {"choices": [{"message": msg}], "usage": usage, "model": model_id,
            "stop_reason": stop_reason, "incomplete_tool_ids": incomplete,
            "content": [blocks[i] for i in sorted(blocks)]}


# ── vendor-reported quota, read from the response itself ─────────────────────
# "% used" is what people expect, and it must be accurate rather than guessed,
# because vendors change the token allotment silently.
#
# That rules out a rate card of allotments in our code: the moment a vendor
# moves the number, a hardcoded denominator lies and keeps lying, and nothing
# in the system would notice. It also rules out asking the user to type their
# plan size — same staleness, now with their name on it.
#
# So the denominator is only ever the VENDOR'S OWN, read off the response we
# already receive. Nothing here encodes what the limit IS, or even what the
# headers are called: it captures whatever rate-limit headers arrive, verbatim.
# If a vendor renames or drops them we capture less and report no percentage —
# never a wrong one. If a vendor silently changes the allotment, the next call
# carries the new number and the percentage self-corrects with no code change.
#
# Read-only and total: a header we do not understand is stored, not discarded,
# so the shape is discoverable from real traffic instead of from a guess.
def _request_id_header(resp) -> str | None:
    """The provider's request id from the response headers, when it sends one
    (x-request-id / request-id). The body's completion id is preferred."""
    try:
        h = resp.headers or {}
        for k in ("x-request-id", "request-id", "x-amzn-requestid"):
            v = h.get(k)
            if v:
                return str(v)[:128]
    except Exception:  # noqa: BLE001 — a header we cannot read is no id
        return None
    return None


def _quota_headers(resp) -> dict:
    """Every rate-limit-ish header the vendor sent, lowercased. {} when none.

    Deliberately pattern-matched rather than an allowlist of known names —
    Anthropic, OpenAI-shaped and OpenRouter lanes each spell these differently,
    and an allowlist would silently miss a vendor we have not met."""
    try:
        out = {}
        for k, v in (resp.headers or {}).items():
            lk = str(k).lower()
            if "ratelimit" in lk.replace("-", "").replace("_", ""):
                out[lk] = str(v)[:64]
        return out
    except Exception:
        return {}


def _refuses_effort(status: int, body: str, payload: dict) -> bool:
    """True when an Anthropic 400 refuses the request's output_config.effort:
    its message names the effort, or names output_config while the effort is
    all the request put there (a model that takes no output_config at all).
    Read from the refusal's own message (context_budget.refusal_parts), never
    from ids or other fields of the body."""
    if status != 400 or not body:
        return False
    from mastercode.context_budget import refusal_parts
    msg = (refusal_parts(body)[0] or "").lower()
    if "effort" in msg:
        return True
    oc = payload.get("output_config")
    return "output_config" in msg and isinstance(oc, dict) and set(oc) == {"effort"}


def _drop_effort(payload: dict) -> None:
    """Take output_config.effort off a payload, in place, and output_config
    with it when nothing else is in it."""
    oc = payload.get("output_config")
    if not isinstance(oc, dict):
        return
    oc = {k: v for k, v in oc.items() if k != "effort"}
    if oc:
        payload["output_config"] = oc
    else:
        payload.pop("output_config", None)


async def _call_anthropic(
    messages: list[dict],
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    session_id: str = None,
    tools: list[dict] = None,
    entry_id=None,
    call_start: float = 0,
    _source: str = "unknown",
    _entity_id: int = None,
    stream_cb=None,
    base_url: str = None,
    native_tools: list[dict] = None,
    extra_body: dict = None,
    idle_s: float = None,
    on_alive=None,
    tool_choice: str | None = None,
    effort: str | None = None,
) -> dict:
    """Container-side inference on the Anthropic Messages API — provider
    'anthropic', keyed by the user's own Keys entry (canonical 'anthropic'
    API key first, 'anthropic-oauth' token second — keys work by default,
    OAuth is the build-your-own-surface option). Same result contract as
    _call_remote. Claude never transits OpenRouter; this is its direct path.

    `effort` is the reasoning effort the PERSON chose for this route
    (call_model's chosen_effort), never the OpenAI wire's "low" fallback. It
    goes out as output_config.effort only at a level the model's catalog row
    accepts (inference_config.anthropic_wire_effort); None sends nothing and
    the model's own default applies, exactly as before effort was wired. A
    400 that refuses the effort anyway is re-sent once without it. The result
    says what was sent under `effort` (inference_config.describe_effort).
    """
    from server.vault.providers import PROVIDERS, resolve
    from mastercode.inference_config import (
        anthropic_api_model_id, anthropic_default_effort,
        anthropic_model_efforts_warm, describe_effort)
    eid_s = os.environ.get("ENTITY_ID", "")
    pool = None
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception:
        pass
    cred = await resolve(pool, int(eid_s) if eid_s.isdigit() else 0,
                         "anthropic", ("api_key", "oauth_token"),
                         actor="inference")
    api_key = cred.secret if cred else ""
    if not api_key:
        return {"error": "no Anthropic credential — add an API key (or OAuth "
                         "token) in the Keys app (name it 'anthropic'), or "
                         "route this role to another provider or a local "
                         "model in the Orchestrator.",
                "content": "", "lane_unavailable": True}

    model = anthropic_api_model_id(model)
    endpoint = ((base_url or PROVIDERS["anthropic"]["base_url"]).rstrip("/")
                + "/v1/messages")
    headers = {"anthropic-version": _ANTHROPIC_VERSION,
               "Content-Type": "application/json"}
    if cred and cred.cred_type == "oauth_token":
        # OAuth access tokens are only accepted with the oauth beta header
        # (the token is the user's own — build-your-own-login-surface
        # scope).
        headers["Authorization"] = f"Bearer {api_key}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = api_key

    name_map = _anthropic_tool_name_map(tools)
    payload = _anthropic_messages_payload(messages, model, temperature,
                                          max_tokens, tools, name_map=name_map,
                                          tool_choice=tool_choice)
    if native_tools:
        # Provider-native SERVER tools (e.g. web_search_20250305 — the
        # runtime/web_research.py search mechanism), appended verbatim: the
        # API runs them itself, they never come back as tool_calls.
        payload.setdefault("tools", []).extend(native_tools)
    if extra_body:
        payload.update(extra_body)
    # Reasoning effort: only a level the PERSON chose, and only one this
    # model's catalog row accepts. Nothing chosen sends nothing, so an unset
    # route keeps the model's own default (claude-haiku-5-5 and
    # claude-opus-5-5 run at medium, most others at high). Merged into any
    # output_config the caller sent, never replacing it. The same level rides
    # every call of a turn: a top-level effort that changes between requests
    # restarts the prompt cache.
    if effort:
        await anthropic_model_efforts_warm(model)
    _effort = describe_effort("anthropic", model, effort, None)
    if _effort["sent"]:
        _oc = payload.get("output_config")
        payload["output_config"] = {**(_oc if isinstance(_oc, dict) else {}),
                                    "effort": _effort["sent"]}
    elif effort:
        log.warning("anthropic effort %r not sent for %s: %s; the model's default "
                    "applies", effort, model, _effort.get("why"))
    # Streamed when the caller streams, and when the output may be long
    # (_STREAM_ABOVE): same relay either way, sources included.
    _streamed = (stream_cb is not None or on_alive is not None
                 or int(max_tokens or 0) > _STREAM_ABOVE)
    if _streamed:
        payload["stream"] = True

    try:
        # idle_s: the longest wait between two bytes, for a caller that asks
        # (web_research's dialers); every other call is unchanged, its
        # liveness kept by the turn's own idle timer. Streamed calls only: a
        # buffered answer sends no byte until it is written, so there the
        # limit would be a total that cuts a live answer.
        timeout = aiohttp.ClientTimeout(total=None, connect=30,
                                        **({"sock_read": idle_s} if idle_s and _streamed else {}))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            # At most one re-send for each of the two refusals below; any
            # other non-200 answer returns at once.
            _limit_resent = _effort_resent = False
            for _attempt in (1, 2, 3):
                async with session.post(endpoint, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        # The model allows less output than was asked: the
                        # provider says how much — learn it, send that once.
                        _ref = (_refused_output(resp.status, body, max_tokens)
                                if not _limit_resent else None)
                        if _ref:
                            _limit_resent = True
                            if _ref[1]:
                                from mastercode.context_budget import learn_output_limit
                                learn_output_limit("anthropic", model, _ref[0])
                            log.warning("anthropic refused max_tokens=%s for %s — resending "
                                        "at %s (%s)", max_tokens, model, _ref[0],
                                        "the model's limit, remembered" if _ref[1]
                                        else "this call only")
                            max_tokens = payload["max_tokens"] = _ref[0]
                            continue
                        # The effort, refused although the catalog listed it
                        # (the list changed, or another field of this request
                        # rules that level out): sent again once without it,
                        # at the model's default, and the result says so.
                        if (_effort["sent"] and not _effort_resent
                                and _refuses_effort(resp.status, body, payload)):
                            _effort_resent = True
                            from mastercode.context_budget import refusal_parts as _rp
                            _why = _rp(body)[0][:200]
                            log.warning("anthropic refused effort=%s for %s (%s) — resending "
                                        "without it; the model's default applies",
                                        _effort["sent"], model, _why)
                            _drop_effort(payload)
                            _effort = {**_effort, "sent": None,
                                       "default": anthropic_default_effort(model),
                                       "why": f"the API refused it: {_why}"}
                            continue
                        log.error("anthropic error: %s %s", resp.status, body[:300])
                        # The whole body rides on the result (error_body) so the
                        # turn classifies the refusal by its message and typed
                        # code, not a 200-character slice.
                        out = {"error": f"anthropic {resp.status}: {body[:200]}",
                               "content": "", "error_body": body[:20000]}
                        if resp.status == 429 or resp.status >= 500:
                            out["lane_unavailable"] = True
                        elapsed = (_time.time() - call_start) * 1000
                        if _prompt_console and entry_id:
                            _prompt_console.log_response(entry_id, "", 0, elapsed,
                                                         error=out["error"])
                        return out
                    # The vendor's OWN quota numbers, read off this very
                    # response — the only denominator that cannot go stale when a
                    # vendor silently changes the allotment.
                    _quota = _quota_headers(resp)
                    if _streamed:
                        relay = await _consume_anthropic_stream(
                            resp, stream_cb or _discard_stream, name_map=name_map,
                            on_alive=on_alive)
                    else:
                        relay = await resp.json()
                    break

        if _streamed:
            msg = (relay.get("choices") or [{}])[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            reasoning = msg.get("reasoning") or ""
            tool_calls = msg.get("tool_calls") or []
            _incomplete = relay.get("incomplete_tool_ids") or []
        else:
            content, reasoning, tool_calls = _anthropic_result_message(
                relay, name_map=name_map)
            content = content.strip()
            _incomplete = None
        _stop = _stop_fields(relay.get("stop_reason"), tool_calls, _incomplete, max_tokens)

        if not content and not tool_calls:
            if _stop.get("truncated"):
                return {"error": (f"the model reached its output limit ({max_tokens} "
                                  f"tokens) before writing any reply "
                                  f"(stop_reason={relay.get('stop_reason')})"),
                        "content": "", **_stop}
            # The stop reason rides the error too: a search turn the API
            # paused before any text (pause_turn) is not an empty answer.
            return {"error": "empty response from anthropic", "content": "",
                    "stop_reason": relay.get("stop_reason")}

        # Usage: the Messages wire reports input_tokens EXCLUDING cached
        # tokens (cache_read/cache_creation are separate fields) — the true
        # input total is the sum, matching what input_total_tokens means on
        # the OpenAI wire (where prompt_tokens already includes the subset).
        u = relay.get("usage") or {}
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_write = int(u.get("cache_creation_input_tokens") or 0)
        prompt_tokens = int(u.get("input_tokens") or 0) + cache_read + cache_write
        completion_tokens = int(u.get("output_tokens") or 0)
        tokens_used = prompt_tokens + completion_tokens
        elapsed = (_time.time() - call_start) * 1000

        await _log_inference(
            source=_source, model=model, provider="anthropic",
            total_tokens=tokens_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed, entity_id=_entity_id,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            # The vendor's own quota numbers, verbatim, on the row. Rides the
            # metadata JSONB — no schema change, same lane the cache split
            # uses. Absent when the vendor sent none.
            metadata={"quota": _quota} if _quota else None,
        )
        if _prompt_console and entry_id:
            _prompt_console.log_response(entry_id, content, tokens_used, elapsed)

        result: dict = {
            "content": strip_thinking(content),
            "tokens_used": tokens_used,
            "model": relay.get("model") or model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "input_total_tokens": prompt_tokens,
        }
        # Vendor-reported quota, verbatim. Rides the result so the caller can
        # log it; empty when this vendor sends no such headers, which must
        # read as "no percentage available", never as 0% or 100%.
        if _quota:
            result["quota_headers"] = _quota
        # The effort this call actually ran at: the level sent, or None and
        # the model's default (named when the docs name it), with why a
        # chosen level was not sent. What the turn records, never the pick.
        result["effort"] = dict(_effort)
        if reasoning:
            result["reasoning"] = reasoning
        if tool_calls:
            result["tool_calls"] = tool_calls
            result["raw_message"] = {"content": content,
                                     "tool_calls": tool_calls}
        result.update(_stop)
        if stream_cb is None:
            # Web-search citations (server web_search tool): text-block
            # `citations[]` + web_search_tool_result blocks → the SAME
            # annotations[].url_citation shape the OpenAI wire returns, so
            # web_research reads one shape for every provider. A call the
            # gateway streamed for its length carries the same blocks.
            _cites = _anthropic_citations(relay)
            if _cites:
                result["annotations"] = _cites
        return result

    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
        log.error("anthropic transport failure (lane unavailable): %s", exc)
        elapsed = (_time.time() - call_start) * 1000
        if _prompt_console and entry_id:
            _prompt_console.log_response(entry_id, "", 0, elapsed, error=str(exc))
        return {"error": f"inference lane unavailable: {exc}", "content": "",
                "lane_unavailable": True}
    except Exception as exc:
        log.error("anthropic call failed: %s", exc, exc_info=True)
        elapsed = (_time.time() - call_start) * 1000
        if _prompt_console and entry_id:
            _prompt_console.log_response(entry_id, "", 0, elapsed, error=str(exc))
        return {"error": f"anthropic inference failure: {exc}", "content": ""}


async def _call_remote(
    messages: list[dict],
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    session_id: str = None,
    tools: list[dict] = None,
    entry_id=None,
    call_start: float = 0,
    _source: str = "unknown",
    _entity_id: int = None,
    reasoning_effort: str = None,
    stream_cb=None,
    provider: str = "openrouter",
    base_url: str = None,
    native_tools: list[dict] = None,
    extra_body: dict = None,
    idle_s: float = None,
    reasoning_inline: bool = False,
    on_alive=None,
    cache_key: str = None,
    tool_choice: str | None = None,
) -> dict:
    """Container-side remote inference on an OpenAI-wire provider.

    DIRECT to the provider with the USER'S OWN key (Keys app vault; env
    fallback), over the node-local governed egress proxy (HTTPS_PROXY —
    content-blind, the pod's only route out). No bridge relay, no platform
    key, no platform charge (the platform never meters tokens). The
    response contract is provider-independent — same result dict (content /
    reasoning split / tokens / tool_calls), same _log_inference row (the
    Compute app's spend view), same infra-vs-model error semantics
    (transport / 5xx / 429 → lane_unavailable).

    `provider` is the registry id from the role route ('openrouter',
    'openai', 'xai', 'kimi', …) — the call dials that provider's endpoint
    with its canonical Keys entry. 'openrouter' additionally runs the
    legacy-key migration ladder (_load_user_openrouter_key). Anthropic is
    NOT served here — the Messages wire has its own door (_call_anthropic).

    When `stream_cb` is provided the call streams (SSE) and stream_cb(text)
    is awaited per visible delta; the returned dict is identical either way.

    Reasoning is asked for SEPARATELY from the reply where the provider has
    a documented way to ask: OpenRouter's `reasoning.exclude:
    false` (its reasoning arrives in `reasoning`, read below and in the
    stream), and Groq's `reasoning_format: "parsed"` on a route flagged
    `reasoning_inline` (Groq's default for its reasoning models is the
    reasoning inline in <think> tags; the flag says this route's model is
    one). A `reasoning_inline` route also has its stream held back until
    the reasoning closes (_consume_openrouter_stream) — only while the
    reasoning is inline: not on Groq parsed, and not once a delta carries
    the reasoning in its own field.
    """
    provider = (provider or "openrouter").strip().lower()
    # Sign-in providers only (xai): which credential is on the wire ('signin' |
    # 'api_key'). It rides the result and the ledger row, and the cost of the
    # call follows it (inference.price_basis: the sign-in is the user's plan).
    cred_kind = None
    if provider == "openrouter":
        api_key = await _load_user_openrouter_key()
        endpoint = base_url.rstrip("/") + "/chat/completions" if base_url else OPENROUTER_URL
        keys_hint = ("no OpenRouter key — add one in the Keys app "
                     "(name it 'openrouter'), or route this role to another "
                     "provider or a local model in the Orchestrator.")
    else:
        from server.vault.providers import (
            PROVIDERS, chat_completions_url, resolve_base_url,
            resolve_for_inference, signin_serves_inference, SIGNIN_UNUSABLE,
            signin_unusable_message, no_credential_message)
        spec = PROVIDERS.get(provider)
        if not spec or spec["wire"] != "openai":
            return {"error": f"provider {provider!r} is not an OpenAI-wire "
                             f"inference provider",
                    "content": "", "lane_unavailable": True}
        eid_s = os.environ.get("ENTITY_ID", "")
        _eid = int(eid_s) if eid_s.isdigit() else 0
        pool = None
        try:
            from server import brain_service
            pool = brain_service.get_pool()
        except Exception:
            pass
        # xai: its host-run sign-in whenever one exists, and the API key ONLY
        # when there is none — never behind it (server/vault/providers.
        # _SIGNIN_INFERENCE has the order and why). Every other provider: its
        # key, exactly as before.
        cred = await resolve_for_inference(pool, _eid, provider, actor="inference")
        if cred and cred.source in SIGNIN_UNUSABLE:
            # Present but expired / unreadable: say so. Nothing is dialled,
            # and the key is not used while a sign-in exists.
            return {"error": signin_unusable_message(provider, cred.source),
                    "content": "", "lane_unavailable": True}
        api_key = cred.secret if cred else None
        if signin_serves_inference(provider) and cred:
            cred_kind = "signin" if cred.source == "signin" else "api_key"
        if base_url:
            endpoint = base_url.rstrip("/") + "/chat/completions"
        elif api_key:
            # Per-KEY base: a kimi subscription key dials the coding base the
            # key authenticates on, never the platform base that 401s it
            # (server/vault/providers.resolve_base_url — cached per key;
            # providers without alt bases resolve with zero probes).
            resolved = await resolve_base_url(provider, api_key)
            endpoint = ((resolved.rstrip("/") + "/chat/completions")
                        if resolved else chat_completions_url(provider))
        else:
            endpoint = chat_completions_url(provider)
        keys_hint = (no_credential_message(provider)
                     if signin_serves_inference(provider) else
                     f"no {provider} key — add one in the Keys app (name it "
                     f"{provider!r}), or route this role to another provider "
                     f"or a local model in the Orchestrator.")
    if not api_key:
        return {"error": keys_hint, "content": "", "lane_unavailable": True}

    # Cache boundary: only the OpenRouter
    # sub-lane can honor Anthropic-protocol cache hints (field passthrough);
    # direct providers and non-Claude models get the messages untouched.
    if provider == "openrouter":
        messages = _apply_cache_boundary(messages, model)

    # OpenAI-wire providers reject the registry's dotted tool names
    # (Kimi: leading letter + [A-Za-z0-9_-]; OpenAI: [A-Za-z0-9_-]{1,64}).
    # Wire-only rename, mapped back on the response — see _openai_wire_rename.
    #
    # OPENROUTER INCLUDED. It was exempt on the belief that
    # "dotted names pass there". They pass to OpenRouter and are then handed to
    # the upstream model's API, which applies its own rule: measured on one
    # Host, openrouter + openai/gpt-4o-mini answered 400 "Invalid
    # 'tools[16].function.name': string does not match pattern
    # '^[a-zA-Z0-9_-]+$'" on a Workbench turn — the Workbench surface is where
    # the dotted names (ladder.run, review.compare, …) live — and the turn
    # ended with no reply at all. The only cost the exemption guarded against
    # is a one-time prompt-cache re-key: the rename is deterministic, so the
    # wire names are stable turn to turn.
    # Tools first, so a current tool keeps the plain wire name if a history
    # name ever collides with it after sanitising.
    native_names = {((t or {}).get("function") or {}).get("name")
                    for t in (native_tools or []) if isinstance(t, dict)} - {None}
    name_map = _openai_wire_tool_name_map(
        list(tools or []) + _history_tool_stubs(messages, keep=native_names))
    # A built-in's name never gets a wire alias, even if a tool shares it.
    name_map = {w: c for w, c in name_map.items() if c not in native_names}
    if name_map:
        _renamed_tools, messages = _openai_wire_rename(tools, messages, name_map)
        tools = _renamed_tools if tools else tools

    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if provider != "openrouter":
        # Kimi's subscription (coding) base rejects any temperature but the
        # model default — measured live through this exact path:
        # `kimi 400: invalid temperature: only 1 is allowed for this model`.
        # Omit the knob there and the model's own default applies.
        from server.vault.providers import is_subscription_base
        if is_subscription_base(endpoint.rsplit("/chat/completions", 1)[0]):
            payload.pop("temperature", None)
    if tools:
        payload["tools"] = tools
        # "none" keeps the same tools on the wire with no call allowed (a
        # search-mode spend stop): the prefix stays the cached one.
        payload["tool_choice"] = "none" if tool_choice == "none" else "auto"
    if native_tools:
        # Provider-native tool objects appended verbatim (e.g. Moonshot's
        # {"type":"builtin_function","function":{"name":"$web_search"}} —
        # runtime/web_research.py). Not converted, not validated here.
        payload["tools"] = list(payload.get("tools") or []) + list(native_tools)
        payload.setdefault("tool_choice", "auto")
    if extra_body:
        # Provider-specific request fields (e.g. xAI `search_parameters`).
        payload.update(extra_body)
    if reasoning_effort:
        # OpenRouter's unified reasoning control.
        payload["reasoning"] = {"effort": reasoning_effort}
        if provider == "openrouter":
            # ...and the reasoning back in its own field, never in the reply
            # (`exclude: false`: OpenRouter's documented default — "Optional:
            # Default is false. All models support this.", openrouter.ai/docs
            # /use-cases/reasoning-tokens, read 2026-10-02 — asked for
            # explicitly, so sending it changes nothing; a direct provider is
            # sent only what it was before).
            payload["reasoning"]["exclude"] = False
    if reasoning_inline and provider == "groq":
        # Groq: the reasoning in `reasoning`, not inline (its default "raw").
        payload["reasoning_format"] = "parsed"
    # Streamed when the caller streams, and when the output may be long
    # (_STREAM_ABOVE) — except a call carrying provider-native tools or extra
    # fields (web search: its sources ride the buffered message — annotations,
    # xAI's top-level citations — which the stream reader does not collect).
    _streamed = stream_cb is not None or (
        (on_alive is not None or int(max_tokens or 0) > _STREAM_ABOVE)
        and not native_tools and not extra_body and not _web_plugin_model(model))
    if _streamed:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    # Pin this conversation to the replica holding its prefix (see
    # _CACHE_KEY_UNSUPPORTED). A Host turn passes `cache_key`, one value per
    # conversation (session.conversation_cache_key), so every turn of a thread
    # lands together. Its `session_id` is the turn's own and is None on chat
    # entries, which is why Host turns key on the conversation instead.
    # Other callers (a dispatched agent's run) still pin on their session id.
    # Nothing stable to pin to → skip.
    _pin = cache_key or session_id
    if (_pin and provider not in _CACHE_KEY_UNSUPPORTED
            and provider not in _CACHE_KEY_REJECTED):
        payload["prompt_cache_key"] = str(_pin)

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # xAI keeps its prompt cache per server. On chat completions the
        # x-grok-conv-id HEADER is what sends a conversation's calls to the
        # server holding its prefix; prompt_cache_key is the Responses API's
        # name for it (docs.x.ai, prompt caching: maximizing cache hits). Both
        # credentials (sign-in and API key) dial here. The same pin as the
        # body key: the conversation's cache_key on a Host turn (whose
        # session_id is None), else the caller's session id.
        if provider == "xai" and _pin:
            headers["x-grok-conv-id"] = str(_pin)
        # trust_env=True → the request rides the node-local governed egress
        # proxy (HTTPS_PROXY), the pod's only route to the public internet.
        # idle_s on streamed calls only (see _call_anthropic).
        timeout = aiohttp.ClientTimeout(total=None, connect=30,
                                        **({"sock_read": idle_s} if idle_s and _streamed else {}))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for _attempt in (1, 2):
                async with session.post(endpoint, headers=headers, json=payload) as resp:
                    # Same rule as the anthropic lane: the vendor's own quota
                    # numbers, off this response, never a figure of ours.
                    _quota = _quota_headers(resp)
                    _req_hdr = _request_id_header(resp)
                    if resp.status != 200:
                        body = await resp.text()
                        # A provider that stops accepting the cache-affinity key
                        # must DEGRADE (lose caching), never fail the turn. Cost
                        # is one retry, once per process, then it stops being
                        # sent at all for that provider.
                        if (resp.status == 400 and _attempt == 1
                                and "prompt_cache_key" in payload
                                and "prompt_cache_key" in body):
                            _CACHE_KEY_REJECTED.add(provider)
                            payload.pop("prompt_cache_key", None)
                            log.warning(
                                "%s rejected prompt_cache_key — retrying without "
                                "it; prompt caching is degraded for this provider "
                                "until restart", provider)
                            continue
                        # The model allows less output than was asked and the
                        # provider says how much: learn it, send that once.
                        _ref = (_refused_output(resp.status, body, max_tokens)
                                if _attempt == 1 else None)
                        if _ref:
                            if _ref[1]:
                                from mastercode.context_budget import learn_output_limit
                                learn_output_limit(provider, model, _ref[0])
                            log.warning("%s refused max_tokens=%s for %s — resending at %s "
                                        "(%s)", provider, max_tokens, model, _ref[0],
                                        "the model's limit, remembered" if _ref[1]
                                        else "this call only")
                            max_tokens = payload["max_tokens"] = _ref[0]
                            continue
                        log.error("%s error: %s %s", provider, resp.status, body[:300])
                        out = {"error": f"{provider} {resp.status}: {body[:200]}",
                               "content": "", "error_body": body[:20000]}
                        # 429 / 5xx = provider-side outage or limit — the model never
                        # got a fair shot → lane miss, excluded from model_stats.
                        # Other 4xx = a model/request failure, unflagged.
                        if resp.status == 429 or resp.status >= 500:
                            out["lane_unavailable"] = True
                        if cred_kind == "signin" and resp.status in (401, 403):
                            # The vendor turned the SIGN-IN away. Nothing is
                            # retried on the API key — it is never used while a
                            # sign-in exists (providers._SIGNIN_INFERENCE). Say
                            # so in words the user can act on; the model never
                            # got a shot, so it is a lane miss and the route's
                            # fallback provider (the user's own choice) may answer.
                            from server.vault.providers import signin_refused_message
                            out["error"] = signin_refused_message(provider, resp.status)
                            out["lane_unavailable"] = True
                        if cred_kind:
                            out["credential"] = cred_kind
                        elapsed = (_time.time() - call_start) * 1000
                        if _prompt_console and entry_id:
                            _prompt_console.log_response(entry_id, "", 0, elapsed,
                                                         error=out["error"])
                        return out

                    if _streamed:
                        # Held to </think> only where the reasoning is
                        # inline: Groq parsed puts it in its own field, so
                        # its content is the reply.
                        relay = await _consume_openrouter_stream(
                            resp, stream_cb or _discard_stream,
                            reasoning_inline=bool(reasoning_inline)
                            and payload.get("reasoning_format") != "parsed",
                            on_alive=on_alive)
                    else:
                        relay = await resp.json()
                    break

        # OpenRouter returns OpenAI-compatible chat.completion shape.
        choices = (relay or {}).get("choices") or []
        if not choices:
            return {"error": "no choices in inference response", "content": ""}

        msg = choices[0].get("message") or {}
        # Wire-safe tool names → canonical, before anything downstream reads
        # them (dispatch, stored history, raw_message).
        _openai_wire_restore(msg, name_map)
        # Pull reasoning_content / reasoning OUT of the message first so
        # it never leaks into the visible reply. DeepSeek + extended-
        # thinking models put their internal planning in this separate
        # field; older parsers that only read msg.content would either
        # miss the answer (when content was empty and reasoning had it)
        # or worse, surface raw planning text as the Host's reply.
        reasoning = _extract_reasoning(msg)
        content = strip_thinking((msg.get("content") or "").strip())
        if _reasoning_only_reply(msg, content):
            # Some models put EVERYTHING in reasoning when prompted simply.
            # Log it but emit empty content rather than showing planning as
            # the answer — caller will retry / surface "no response" error.
            # tool_calls are checked HERE and not 70 lines later (:1433): a
            # reasoning model that calls a tool always has empty content, so
            # the old `reasoning and not content` test fired on every
            # tool-call round of every turn.
            log.warning(
                "Inference response had reasoning (%d chars) but no content. "
                "Model=%s. Dropping reasoning from visible reply.",
                len(reasoning), model,
            )
        _usage = (relay.get("usage") or {})
        tokens_used, prompt_tokens, completion_tokens, cache_read, cache_write = \
            _normalize_openai_usage(_usage, provider=provider, model=model)
        # No usage object at all: the counts are UNKNOWN, never zero (two xAI
        # calls with no usage were once stored as 0 in / 0 out). None rides the result, the per-call ledger and the
        # inference_cost_log row (NULL, migrations/v214).
        _usage_reported = isinstance(relay.get("usage"), dict) and bool(relay.get("usage"))
        if not _usage_reported:
            prompt_tokens = completion_tokens = cache_read = cache_write = None
            tokens_used = 0
        elapsed = (_time.time() - call_start) * 1000

        await _log_inference(
            source=_source, model=model,
            provider=provider,
            total_tokens=tokens_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed, entity_id=_entity_id,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            metadata=({**({"quota": _quota} if _quota else {}),
                       **({"credential": cred_kind} if cred_kind else {}),
                       **({} if _usage_reported else {"usage": "not_reported"})}
                      or None),
        )
        if cred_kind:
            # Routing status reads this: "xAI sign-in" or "xAI API key".
            from server.vault.providers import note_served
            note_served(provider, cred_kind)

        if _prompt_console and entry_id:
            _prompt_console.log_response(entry_id, content, tokens_used, elapsed)

        result: dict = {
            "content": content,
            "tokens_used": tokens_used,
            "model": relay.get("model") or model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            # Normalized total context size. On THIS wire prompt_tokens
            # already contains the cached subset; the SDK lane must ADD its
            # cache fields to reach the same meaning. Downstream consumers
            # read input_total_tokens and never branch on wire dialect.
            "input_total_tokens": int(prompt_tokens) if prompt_tokens is not None else None,
        }
        if not _usage_reported:
            result["usage_reported"] = False
        # The provider's id for this response and a short hash of the cache
        # pin this call carried (prompt_cache_key / x-grok-conv-id): the
        # per-call ledger keeps both (context_budget._LEDGER_ONLY_CALL_KEYS),
        # so a cold miss on an unchanged prefix can be taken to the provider
        # with the request it happened on.
        _rid = relay.get("id") or _req_hdr
        if _rid:
            result["request_id"] = str(_rid)[:128]
        if _pin and ("prompt_cache_key" in payload or provider == "xai"):
            result["cache_key_sha"] = hashlib.sha256(str(_pin).encode("utf-8")).hexdigest()[:12]
        if _quota:
            result["quota_headers"] = _quota
        if cred_kind:
            result["credential"] = cred_kind
        if reasoning:
            # Stash reasoning on the result for caller-side logging /
            # provenance. It is NEVER concatenated into `content`.
            result["reasoning"] = reasoning
        if msg.get("annotations"):
            # OpenRouter web-plugin citations (url_citation) — real source
            # provenance for the web_search tool. Additive; only present when
            # the model ran with the ':online' web plugin, so no effect on
            # ordinary turns. (OpenAI's chat wire uses the same shape.)
            result["annotations"] = msg["annotations"]
        elif isinstance(relay.get("citations"), list) and relay.get("citations"):
            # xAI Live Search returns a top-level list of URLs — normalize to
            # the same annotations[].url_citation shape (web_research reads
            # one shape for every provider).
            result["annotations"] = [
                {"url_citation": {"url": u, "title": u}}
                for u in relay["citations"] if isinstance(u, str) and u.strip()
            ]
        if msg.get("tool_calls"):
            result["tool_calls"] = msg["tool_calls"]
            result["raw_message"] = msg
        result.update(_stop_fields(choices[0].get("finish_reason"),
                                   msg.get("tool_calls"), None, max_tokens))
        if result.get("truncated") and not content and not msg.get("tool_calls"):
            result["error"] = (f"the model reached its output limit ({max_tokens} "
                               f"tokens) before writing any reply")
        return result

    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
        # Couldn't COMPLETE the call to the relay (connection refused, DNS, timeout)
        # — a transport/infra miss, NOT a model failure. Structurally flagged so the
        # agent verdict is 'unavailable' and model_stats excludes it. This is what
        # replaces the open-lane error-string heuristic in scoped_agent: the signal
        # is the exception TYPE, not the prose. (The one residual — the relay
        # flattening a downstream provider outage vs a bad-model 4xx into one error
        # string, below — can only be split by categorizing at the relay itself.)
        log.error("openrouter transport failure (lane unavailable): %s", exc)
        elapsed = (_time.time() - call_start) * 1000
        if _prompt_console and entry_id:
            _prompt_console.log_response(entry_id, "", 0, elapsed, error=str(exc))
        return {"error": f"inference lane unavailable: {exc}", "content": "",
                "lane_unavailable": True}
    except Exception as exc:
        log.error("openrouter call failed: %s", exc, exc_info=True)
        elapsed = (_time.time() - call_start) * 1000
        if _prompt_console and entry_id:
            _prompt_console.log_response(entry_id, "", 0, elapsed, error=str(exc))
        return {"error": f"openrouter inference failure: {exc}", "content": ""}


# ---------------------------------------------------------------
# Local call (Ollama / OpenAI-compatible — streaming with XML tool injection)
# ---------------------------------------------------------------

def local_chat_url(endpoint: str) -> str:
    """The chat-completions route for a local model server.

    The resolver hands us a bare ORIGIN — model_backends.resolve_model_endpoint
    returns scheme://host:port with NO path — but neither ollama nor
    llama.cpp/vLLM serves chat completions at "/", so posting it verbatim 404s.
    LOCAL_URL, by contrast, is already a full route, so only extend what still
    needs it.

    This mirrors the normalization the embed lane has always done (memory_engine:
    bare host -> /v1/embeddings). /v1/chat/completions is the OpenAI-compatible
    route ollama, llama-server and vLLM all serve.
    """
    ep = (endpoint or "").strip()
    if not ep:
        return ep
    low = ep.rstrip("/").lower()
    if low.endswith("/chat/completions"):
        return ep                                     # caller named a full route
    if low.endswith("/v1"):
        return ep.rstrip("/") + "/chat/completions"
    return ep.rstrip("/") + "/v1/chat/completions"


async def _call_local(
    messages: list[dict],
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    session_id: str = None,
    tools: list[dict] = None,
    entry_id=None,
    call_start: float = 0,
    _source: str = "unknown",
    _entity_id: int = None,
    base_url: str | None = None,
    on_alive=None,
) -> dict:
    if tools:
        xml_prompt = build_xml_tool_prompt(tools)
        messages = list(messages)
        if messages and messages[0]["role"] == "system":
            messages[0] = {**messages[0], "content": messages[0]["content"] + "\n\n" + xml_prompt}
        else:
            messages.insert(0, {"role": "system", "content": xml_prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens

    # The endpoint comes from the resolver's LIVE bastion-heartbeat lookup —
    # routes never store one (a pinned mesh address goes stale on re-enrollment
    # and hangs chat). Arriving here without one means no
    # machine on the user's network serves this model right now: surface that
    # immediately, never dial the dead in-pod default (pods run no local
    # Ollama). An operator-wired LOCAL_INFERENCE_URL env stays authoritative.
    _endpoint = (base_url or "").strip()
    if not _endpoint and os.environ.get("LOCAL_INFERENCE_URL"):
        _endpoint = LOCAL_URL
    if not _endpoint:
        return {
            "error": f"local inference unavailable: no machine on your network "
                     f"is serving {model!r} right now — bring it online, or "
                     f"route this role to another provider in the Orchestrator",
            "content": "", "provider": "local",
            "local_failed": True, "lane_unavailable": True,
        }

    _endpoint = local_chat_url(_endpoint)

    try:
        session = await _get_session("http://127.0.0.1")
        async with session.post(
            _endpoint, headers={"Content-Type": "application/json"},
            json=payload,
            # No total cap (long local generations are legitimate), but connect
            # must fail FAST — a blackholed mesh address otherwise hangs the
            # turn in "thinking" for minutes before erroring. sock_read guards
            # a stream that dies mid-generation without closing.
            timeout=aiohttp.ClientTimeout(
                total=None, connect=10, sock_connect=10, sock_read=600),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("Local model error: %s %s", resp.status, body[:200])
                # The server's own words are kept ONLY when they are a window
                # refusal. vLLM's template echoes token
                # counts, never the prompt: "This model's maximum context
                # length is {max_model_len} tokens. However, you requested
                # {token_num} tokens ({n} in the messages, {m} in the
                # completion). Please reduce the length of the messages or
                # completion." (vllm/entrypoints/openai/serving_engine.py,
                # docs.vllm.ai v0.10.2 serving_engine; seen live in
                # vllm-project/vllm#20409). Any other local error text — whose
                # templates were not checked — is not stored: it could carry the
                # input, and it would reach the failure ledger and agent run
                # errors. error_body stays on the in-process result only (the
                # turn classifies on it; nothing persists it). Ollama mostly
                # truncates and answers 200 rather than refusing
                # (ollama/ollama#17944).
                from mastercode.context_budget import refusal_parts as _rpl
                _lmsg = _rpl(body)[0]
                # Only vLLM's own templates, at the START of the message, with
                # their numbers — never a window phrase found
                # anywhere in an error that might echo input. Read from vLLM's
                # source, vllm/entrypoints/openai/serving_engine.py,
                # OpenAIServing._validate_input (docs.vllm.ai v0.10.2 API page,
                # fetched 2026-10-02; the page gives no line numbers):
                #   "This model's maximum context length is {max_model_len}
                #    tokens. However, you requested {token_num} tokens in the
                #    input for {operation}. Please reduce the length of the input."
                #   "This model's maximum context length is {max_model_len}
                #    tokens. However, your request has {token_num} input tokens.
                #    Please reduce the length of the input messages."
                #   "'max_tokens' or 'max_completion_tokens' is too large:
                #    {max_tokens}. This model's maximum context length is
                #    {max_model_len} tokens and your request has {token_num}
                #    input tokens ({max_tokens} > {max_model_len} - {token_num})."
                # None echoes the prompt: numbers, and a fixed operation word.
                _keep = bool(re.match(
                    r"(?:This model's maximum context length is \d+ tokens\. However, (?:you requested|your request has) \d+"
                    r"|'max_tokens' or 'max_completion_tokens' is too large: \d+\. This model's maximum context length is \d+ tokens)",
                    _lmsg))
                return {"error": (f"Local model error: {resp.status}: {_lmsg[:300]}" if _keep
                                  else f"Local model error: {resp.status}"),
                        "content": "", "error_body": body[:20000]}

            full_content = ""
            _local_finish = None
            phase = "detect"
            thinking_text = ""
            thinking_start_time = None
            detect_buffer = ""
            response_buffer = ""

            async for raw_line in _iter_sse_lines(resp.content):
                if on_alive is not None:
                    on_alive()
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                payload_str = line[6:].strip()
                if payload_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices")
                if not choices:
                    continue
                _local_finish = choices[0].get("finish_reason") or _local_finish
                delta = choices[0].get("delta", {})
                delta_text = delta.get("content", "")
                if not delta_text:
                    continue

                full_content += delta_text

                if phase == "detect":
                    detect_buffer += delta_text
                    if detect_buffer.lstrip().startswith("Thinking Process:") or detect_buffer.lstrip().startswith("<think>"):
                        phase = "thinking"
                        thinking_start_time = _time.time()
                        thinking_text = detect_buffer
                        if _prompt_console and session_id:
                            _prompt_console.push_activity(session_id, "thinking_start", {})
                            _prompt_console.push_activity(session_id, "thinking_chunk", {"chunk": detect_buffer})
                    elif "<function_calls>" in detect_buffer:
                        phase = "tool_xml"
                    elif len(detect_buffer) >= 25:
                        phase = "response"
                        if _prompt_console and session_id:
                            _prompt_console.push_activity(session_id, "message_chunk", {"chunk": detect_buffer})
                    continue

                if phase == "thinking":
                    thinking_text += delta_text
                    if "</think>" in thinking_text:
                        parts = thinking_text.split("</think>", 1)
                        remainder = parts[1]
                        elapsed_think = _time.time() - thinking_start_time if thinking_start_time else 0
                        if _prompt_console and session_id:
                            _prompt_console.push_activity(session_id, "thinking_end", {"elapsed": round(elapsed_think, 1)})
                        if "<function_calls>" in remainder:
                            phase = "tool_xml"
                        else:
                            phase = "response"
                            if remainder.strip() and _prompt_console and session_id:
                                _prompt_console.push_activity(session_id, "message_chunk", {"chunk": remainder})
                    else:
                        if _prompt_console and session_id:
                            _prompt_console.push_activity(session_id, "thinking_chunk", {"chunk": delta_text})
                    continue

                if phase == "response":
                    if _prompt_console and session_id:
                        response_buffer += delta_text
                        if "<function_calls>" in response_buffer:
                            pre_tool = response_buffer.split("<function_calls>", 1)[0]
                            if pre_tool.strip():
                                _prompt_console.push_activity(session_id, "message_chunk", {"chunk": pre_tool})
                            phase = "tool_xml"
                        elif len(response_buffer) > 50:
                            flush = response_buffer[:-16]
                            response_buffer = response_buffer[-16:]
                            if flush:
                                _prompt_console.push_activity(session_id, "message_chunk", {"chunk": flush})

            if response_buffer and phase == "response" and _prompt_console and session_id:
                _prompt_console.push_activity(session_id, "message_chunk", {"chunk": response_buffer})

            content = full_content.strip()

            if phase == "thinking":
                log.warning("Thinking consumed all tokens (no </think>), %d chars", len(content))
                if _prompt_console and session_id:
                    elapsed_think = _time.time() - thinking_start_time if thinking_start_time else 0
                    _prompt_console.push_activity(session_id, "thinking_end", {"elapsed": round(elapsed_think, 1)})
                content = ""

            content = strip_thinking(content)
            tokens_used = len(full_content) // 4

            await _log_inference(
                source=_source, model=model, provider="local",
                total_tokens=tokens_used,
                elapsed_ms=(_time.time() - call_start) * 1000,
                entity_id=_entity_id,
            )

            elapsed = (_time.time() - call_start) * 1000
            if _prompt_console and entry_id:
                _prompt_console.log_response(entry_id, content, tokens_used, elapsed)

            text_content, tool_calls = parse_xml_tool_calls(content)
            _stop = _stop_fields(_local_finish, tool_calls, None, max_tokens)
            if tool_calls:
                return {"content": text_content, "tokens_used": tokens_used, "model": model,
                        "tool_calls": tool_calls, "raw_message": {"content": text_content, "tool_calls": tool_calls},
                        **_stop}

            return {"content": content, "tokens_used": tokens_used, "model": model, **_stop}

    except Exception as exc:
        # C5 — the entity explicitly selected provider=local (sovereign). A
        # local transport error (Ollama not warm) must NOT silently become a
        # BILLED remote platform call: that's a billing surprise AND a
        # sovereignty breach, both unsurfaced. Surface the local error so the
        # live consumer (runtime/session.py: `if "error" in result and not
        # result.get("content"): return result`) shows the failure instead of
        # a billed answer. Failover, when the route configures one, happens
        # one level up: call_model retries on the route's fallback_provider.
        log.error("Local model failed (provider=local): %s", exc, exc_info=True)
        return {
            "error": f"local inference unavailable: {exc}",
            "content": "",
            "provider": "local",
            "local_failed": True,
            "lane_unavailable": True,   # infra miss — endpoint unreachable, not a model failure
        }


# ---------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------

def _tools_wire_stamp(tools) -> dict:
    """What `tools=` weighed on one call: its JSON size and the first 12 hex
    of its sha256, computed exactly as session._bytes_sha and the context
    ledger's `tools.chars` compute them, so a per-call value can be compared
    with the turn's `first_call_sha`. The ledger had only the turn's final
    list, which hides a list that changed mid-turn (an orient open, an exit's
    reduced list) — the change that rewrites the provider's cache."""
    if not tools:
        return {"chars": 0, "sha": None}
    try:
        raw = json.dumps(tools, default=str)
    except Exception:  # noqa: BLE001 — a size we cannot take is unknown, not zero
        return {"chars": None, "sha": None}
    return {"chars": len(raw), "sha": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]}


async def call_model(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int | None = None,
    session_id: str = None,
    fast: bool = False,
    tools: list[dict] = None,
    model_override: str = None,
    provider: str = None,
    provider_model: str = None,
    **kwargs,
) -> dict:
    """The one model call (see _call_model). `_log_meta` (a dict) rides
    every inference_cost_log row this call writes, success or failure, under
    the row's own keys (_CALL_LOG_META)."""
    _meta = kwargs.pop("_log_meta", None)
    _tok = _CALL_LOG_META.set(dict(_meta) if isinstance(_meta, dict) and _meta else None)
    try:
        return await _call_model(
            messages, temperature=temperature, max_tokens=max_tokens,
            session_id=session_id, fast=fast, tools=tools,
            model_override=model_override, provider=provider,
            provider_model=provider_model, **kwargs)
    finally:
        _CALL_LOG_META.reset(_tok)


async def _call_model(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int | None = None,
    session_id: str = None,
    fast: bool = False,
    tools: list[dict] = None,
    model_override: str = None,
    provider: str = None,
    provider_model: str = None,
    **kwargs,
) -> dict:
    """
    Unified model calling interface — dispatches on PROVIDER.

    `provider` is the route's provider: a registry id (openrouter, openai,
    xai, kimi, anthropic, deepinfra, together, fireworks, groq) or 'local'.
    Legacy values normalize (openrtr/self → openrouter); the removed SDK
    lane's aliases normalize to EMPTY and surface as unconfigured — they
    never execute (the SDK is not a production option).
    `provider_model` is the provider-native model id; there is NO default
    model — a call that arrives without one is a caller that skipped the
    Orchestrator config, and it errors instead of guessing.

    Failover: when the route carries fallback_provider (kwargs), an errored
    primary — provider outage OR the model unavailable there — retries once
    on the fallback (fallback_model, defaulting to the primary's model id)
    and the result is annotated with `failed_over`. Surfaced, never hidden.

    `max_tokens` None (the default) is the dialled
    model's OWN output limit (context_budget.output_limit_for: catalog,
    provider-stated, static table; half the window at most; 16,384 only when
    nothing can say), resolved per dial so a failover model gets its own. An
    int is sent as given. Every result says why the model stopped:
    `stop_reason`, `output_limit`, and `truncated` + `truncated_tool_call_ids`
    when the limit cut it (_stop_fields).

    Reasoning effort, two kwargs: `reasoning_effort` is what the OpenAI wire
    sends (inference_config.route_effort: the route's pick, else "low");
    `chosen_effort` is the person's pick alone, None when they made none
    (inference_config.chosen_effort), and the only one the Anthropic wire
    reads. The local wire takes neither.
    """
    if _kill_switch and _kill_switch.is_killed():
        return {"error": "Kill switch active", "content": "", "killed": True}

    entry_id = None
    if _prompt_console:
        entry_id = _prompt_console.log_request(
            session_id=session_id, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )

    call_start = _time.time()

    from mastercode.inference_config import normalize_provider
    model = (provider_model or model_override or "").strip()
    prov = (provider or "").strip().lower()
    if prov:
        prov, model = normalize_provider(prov, model)
        if not prov:
            _sdk = {"error": "this route names the removed Claude-SDK lane — "
                             "the SDK is not a production option. Pick a "
                             "provider and model in the Orchestrator "
                             "(Anthropic is an ordinary provider in Keys).",
                    "content": "", "unconfigured": True,
                    "lane_unavailable": True}
            await _log_call_failure(
                _sdk, source=kwargs.get("_source", "unknown"),
                model=model or "(none)", provider=(provider or "").strip(),
                entity_id=kwargs.get("_entity_id"), call_start=call_start)
            return _sdk
    else:
        # Bare call, no provider stated: the historical default remote.
        # Claude still coerces off OpenRouter onto the Anthropic API.
        prov, model = normalize_provider("openrouter", model)
    if not model:
        _unconf = {"error": "no model configured — this caller resolved no "
                            "route; pick a provider and model in the "
                            "Orchestrator.",
                   "content": "", "unconfigured": True}
        # The single most important row this lane writes. An unrouted role is
        # exactly how fact derivation once went dead for 16 days
        # (turn_consolidator.py:139-151): the bare call returned this dict, the
        # caller read the empty content as "nothing to do", and nothing
        # anywhere recorded that a model was never reached.
        await _log_call_failure(
            _unconf, source=kwargs.get("_source", "unknown"),
            model=(provider_model or model_override or "") or "(none)",
            provider=prov, entity_id=kwargs.get("_entity_id"),
            call_start=call_start)
        return _unconf

    # THE HOST MODEL STANDARD at the wire (runtime/model_standard): the
    # backstop behind apply_role_route's write-time check, for every path
    # that reaches a model without a stored route (thread overrides, turn
    # types, dispatches naming a model, consolidation, review). Inert while
    # the standard is not enforced. The admission bench is exempt: it exists
    # to test models that are not approved yet (_source is an internal kwarg
    # set by platform code, never by a user; tests/test_model_standard.py
    # asserts runtime/model_bench.py is its only setter). A refused primary is
    # NOT rescued by an approved fallback: the route itself is unapproved, and
    # answering on a different model instead would be a silent substitution.
    _bench = kwargs.get("_source") == "model_bench"
    if not _bench:
        from mastercode.runtime import model_standard as _standard
        # judge_effort=False: effort is judged where the route is saved
        # (apply_role_route); see model_standard.refusal.
        _std = _standard.refusal(prov, model, judge_effort=False)
        if _std:
            _refused = {"error": _std, "content": "", "unconfigured": True,
                        "model_not_approved": True}
            await _log_call_failure(
                _refused, source=kwargs.get("_source", "unknown"), model=model,
                provider=prov, entity_id=kwargs.get("_entity_id"),
                call_start=call_start)
            return _refused

    _caller_max = max_tokens
    # Stamped on every dialled result, primary and failover alike: the same
    # tools ride both dials (see the failover note below).
    _tools_wire = _tools_wire_stamp(tools)

    async def _output_limit(p: str, m: str) -> int:
        """The caller's max_tokens, else model m's own output limit — and in
        either case never more than the room the window leaves after THIS
        call's input (context_budget.call_output_limit)."""
        from mastercode import context_budget as _cbm
        try:
            win = await _cbm.model_context_window(m, provider=p)
            if not _caller_max and (not win or _cbm.model_output_limit(m, p) is None):
                # Cold catalog: read the route's rows once (bounded wait, the
                # turn path's warm), then ask again.
                from server.routes.inference import warm_route_catalog
                await warm_route_catalog(p)
                win = await _cbm.model_context_window(m, provider=p)
        except Exception:  # noqa: BLE001
            win = None
        lim = int(_caller_max) if _caller_max else _cbm.output_limit_for(m, p, win)
        return _cbm.call_output_limit(lim, win, messages, tools)

    async def _dial(p: str, m: str, base_url=None) -> dict:
        # Chat attachments in the shape THIS wire takes (attachment_parts): an
        # image/document block is Anthropic-native, and the OpenAI wire has no
        # such part. Converted per dial, from the caller's own messages, so a
        # failover onto the other wire starts from the originals. A model whose
        # catalog row says it reads no pictures gets a note per picture; one
        # whose row says nothing is sent them, and if the provider refuses the
        # request the call is re-dialled ONCE with notes — a picture never
        # costs the turn.
        from mastercode import attachment_parts as _ap
        from mastercode.runtime import turn_pictures as _tp
        wire = "anthropic" if p == "anthropic" else "openai"
        notes = _ap.model_reads_images(p, m) is False
        # A picture THIS route refused earlier in this turn goes to it as a
        # note, not again; another route (the failover) is still sent it.
        _refused_before = _tp.refused((p, m))
        _sent = await _ap.for_wire(messages, wire, images_as_notes=notes, refused=_refused_before)
        result = await _dial_wire(p, m, base_url, _sent)
        if (not notes and _ap.refused_input(result)
                and _ap.has_pictures(await _ap.for_wire(messages, wire,
                                                        refused=_refused_before))):
            log.info("dial %s/%s refused a turn with pictures (%s); re-dialling with "
                     "notes in their place", p, m, str(result.get("error"))[:160])
            # The refusal is a failed call in its own right: recorded, so the
            # failure ledger shows it even when the re-dial succeeds (the model
            # itself sees a note in place of each picture and can say so).
            # Logged under the ordinary failure purpose ('inference_error' —
            # the label every ledger reader counts as a failure); what makes it
            # a picture refusal travels in the row's metadata.
            await _log_call_failure(
                {**result, "picture_refused": True},
                source=kwargs.get("_source", "unknown"), model=m,
                provider=p, entity_id=kwargs.get("_entity_id"),
                call_start=call_start)
            # The notes quote the refusal, so the model reports what happened
            # rather than guessing a cause (attachment_parts.refusal_why).
            _why = _ap.refusal_why(result)
            _why_raw = str(result.get("error") or "")
            result = await _dial_wire(p, m, base_url,
                                      await _ap.for_wire(messages, wire, images_as_notes=True,
                                                         notes_why=_why))
            # The turn remembers the refused pictures, so this route's next
            # round sends their notes instead of being refused again — but
            # only when the same request WITHOUT them was not refused too:
            # then the pictures were the difference. A refusal the notes get
            # as well (an expired key, a missing model, a prompt too long)
            # says nothing about them, and must not hide them from the next
            # round or from the failover.
            if not _ap.refused_input(result):
                # The picture the refusal names, when it names one (the
                # Messages API does: "messages.4.content.6.image…"), and none
                # when it names a picture that is not one of ours; else every
                # picture the request carried.
                _named = (_ap.refused_picture_shas({"error": _why_raw}, messages, _sent)
                          if wire == "anthropic" else None)
                _tp.note_refused((p, m), _named if _named is not None else
                                 (_ap.picture_shas(messages) - set(_refused_before)),
                                 _why)
        if isinstance(result, dict):
            result["tools_wire"] = dict(_tools_wire)
        return result

    async def _dial_wire(p: str, m: str, base_url, wire_messages) -> dict:
        max_tokens = await _output_limit(p, m)
        if p == "local":
            # The local wire writes tools into the prompt as XML and has no
            # tool_choice: a call that may make none is sent none.
            return await _call_local(
                messages=wire_messages, model=m,
                temperature=temperature, max_tokens=max_tokens,
                session_id=session_id,
                tools=None if kwargs.get("tool_choice") == "none" else tools,
                entry_id=entry_id, call_start=call_start,
                _source=kwargs.get("_source", "unknown"),
                _entity_id=kwargs.get("_entity_id"),
                base_url=base_url,
                on_alive=kwargs.get("on_alive"),
            )
        if p == "anthropic":
            return await _call_anthropic(
                messages=wire_messages, model=m,
                temperature=temperature, max_tokens=max_tokens,
                session_id=session_id, tools=tools,
                entry_id=entry_id, call_start=call_start,
                _source=kwargs.get("_source", "unknown"),
                _entity_id=kwargs.get("_entity_id"),
                stream_cb=kwargs.get("stream_cb"),
                base_url=base_url,
                native_tools=kwargs.get("native_tools"),
                extra_body=kwargs.get("extra_body"),
                on_alive=kwargs.get("on_alive"),
                tool_choice=kwargs.get("tool_choice"),
                idle_s=kwargs.get("idle_s"),
                # The PERSON's pick only (inference_config.chosen_effort),
                # never reasoning_effort: that one carries the OpenAI wire's
                # "low" fallback, and an unset Anthropic route sends nothing.
                effort=kwargs.get("chosen_effort"),
            )
        return await _call_remote(
            messages=wire_messages, model=m,
            temperature=temperature, max_tokens=max_tokens,
            session_id=session_id, tools=tools,
            entry_id=entry_id, call_start=call_start,
            _source=kwargs.get("_source", "unknown"),
            _entity_id=kwargs.get("_entity_id"),
            reasoning_effort=kwargs.get("reasoning_effort"),
            stream_cb=kwargs.get("stream_cb"),
            provider=p,
            base_url=base_url,
            native_tools=kwargs.get("native_tools"),
            extra_body=kwargs.get("extra_body"),
            # The flag describes the route's model: a failover onto another
            # model id does not inherit it.
            reasoning_inline=bool(kwargs.get("reasoning_inline")) and m == model,
            on_alive=kwargs.get("on_alive"),
            cache_key=kwargs.get("cache_key"),
            tool_choice=kwargs.get("tool_choice"),
            idle_s=kwargs.get("idle_s"),
        )

    result = await _dial(prov, model, base_url=kwargs.get("base_url"))

    # Failover: an errored primary (provider outage OR the model unavailable
    # there — any error with no content) retries once on the route's
    # fallback provider. Annotated on the result — a silent downgrade is a
    # billing/trust bug.
    fb_raw = (kwargs.get("fallback_provider") or "").strip()
    if (fb_raw and isinstance(result, dict)
            and result.get("error") and not result.get("content")
            and not result.get("killed") and not result.get("unconfigured")):
        fb_provider, fb_model = normalize_provider(
            fb_raw, (kwargs.get("fallback_model") or "").strip())
        fb_model = fb_model or model
        # The fallback answers with the same tools, so it meets the same
        # standard (see the primary's check above). A refused fallback leaves
        # the primary's error as the result — the failover simply does not
        # happen — and says why in the log.
        if fb_provider and not _bench:
            from mastercode.runtime import model_standard as _standard
            _fstd = _standard.refusal(fb_provider, fb_model, judge_effort=False)
            if _fstd:
                log.warning("provider failover skipped: %s", _fstd)
                fb_provider = ""
        if fb_provider and (fb_provider, fb_model) != (prov, model):
            fb_base = (kwargs.get("fallback_base_url") or "").strip() or None
            if fb_provider == "local" and not fb_base:
                # Local routes never store an endpoint — live lookup, same
                # rule as the primary local path.
                try:
                    from mastercode.model_backends import match_local_model
                    hit = await match_local_model(
                        _inference_pool, kwargs.get("_entity_id"), fb_model)
                    fb_base = hit["endpoint"] if hit else None
                except Exception:
                    fb_base = None
            log.warning("provider failover: %s/%s errored (%s) — retrying on "
                        "%s/%s", prov, model, str(result.get("error"))[:120],
                        fb_provider, fb_model)
            retry = await _dial(fb_provider, fb_model, base_url=fb_base)
            _fo = None
            if isinstance(retry, dict):
                # Both ends of the downgrade: the turn row and the UI stamp
                # WHO ANSWERED from to_provider/to_model (provenance: nobody
                # should have to ask what model ran it), and from_* +
                # primary_error say why.
                _fo = {
                    "from_provider": prov, "from_model": model,
                    "to_provider": fb_provider, "to_model": fb_model,
                    "primary_error": str(result.get("error"))[:200],
                }
                retry["failed_over"] = _fo
            if isinstance(retry, dict) and retry.get("error") \
                    and not retry.get("content"):
                # Both ends failed. The fallback's OWN row records only its own
                # failure: `retry` now carries the failed_over annotation meant
                # for the CALLER, and logging it here would stamp this row's
                # failed_over_to pointing at itself.
                await _log_call_failure(
                    {k: v for k, v in retry.items() if k != "failed_over"},
                    source=kwargs.get("_source", "unknown"),
                    model=fb_model, provider=fb_provider,
                    entity_id=kwargs.get("_entity_id"), call_start=call_start)
            # The PRIMARY's row is the one that carries the failover stamp —
            # it is the row that says "this provider died, X answered
            # instead". `result` never carries the annotation (it goes on
            # `retry`, which is what the caller sees), so thread it in.
            await _log_call_failure(
                {**result, "failed_over": _fo} if _fo else result,
                source=kwargs.get("_source", "unknown"),
                model=model, provider=prov,
                entity_id=kwargs.get("_entity_id"), call_start=call_start)
            return retry
    if isinstance(result, dict) and result.get("error") \
            and not result.get("content"):
        # Every remaining failed call — provider 4xx/5xx, transport loss,
        # empty response. (The kill switch returns above this, before
        # call_start exists; a severed API is a deliberate operator action,
        # not an issue to alert the user about.) The success paths log through
        # _log_inference; this is the other half, and the ONE place that sees
        # every provider's failures (the per-provider except blocks each
        # record through the unwired _prompt_console).
        await _log_call_failure(
            result, source=kwargs.get("_source", "unknown"),
            model=model, provider=prov,
            entity_id=kwargs.get("_entity_id"), call_start=call_start)
    return result


# Backward-compatible aliases
_call_entity_model = call_model
_build_xml_tool_prompt = build_xml_tool_prompt
_parse_xml_tool_calls = parse_xml_tool_calls
