#!/usr/bin/env python3
"""A turn's spend allowance, cut replies, statuses and provider window refusals.

Container-free: APP_ROOT=<repo> python3 scripts/test_turn_allowance_and_cut_replies.py.
The real EntityRuntime.send_message with a scripted model where the loop is
under test (the Harness of scripts/test_turn_cut_output.py); no pod, no DB,
no network.

  A    every request, the first included, is held to what the remaining
       allowance can pay for at the route's output price (never below one
       realistic reply), so the worst overspend is one realistic reply
  spend a continuation of a cut reply passes the same spend check
  B    a cut that left no visible text is not glued onto the narration before
       it
  C    an input estimate that runs high never cuts a request below the old
       fixed min(limit, 16,384)
  D    failure-shaped statuses (rate_limited, failed, ungrounded) are failures;
       other unknown statuses are a neutral OTHER in the rollup, never ok and
       never in the error lane
  UND  OpenRouter's typed context_length_exceeded and kimi's
       invalid_request_error about the window resend THAT call smaller instead
       of falling through to the history-cutting retry

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

T = os.path.join(APP_ROOT, "scripts", "test_turn_cut_output.py")
_src = open(T).read()
_g = {"__file__": T, "__name__": "round5"}
exec(compile(_src.split("# 1 + 2: the cut tool call")[0], "harness", "exec"), _g)
Harness, _tc, reply_of, tool_msgs = _g["Harness"], _g["_tc"], _g["reply_of"], _g["tool_msgs"]

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[turn-allowance] FAIL {name} {extra}")


from mastercode.runtime import turn_allowance as ta  # noqa: E402
from mastercode.runtime import tool_call_ledger as L  # noqa: E402
from mastercode import context_budget as CB  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from server.routes import inference as INF  # noqa: E402


# ── A: the spend repro, then the turn ─────────────────────────────────────
class P:  # illustrative rates (USD/token)
    input_per_token, output_per_token, cache_read, cache_write = 5e-6, 25e-6, 0.5e-6, 6.25e-6
LAST = {"input_total_tokens": 100_000, "cache_read_tokens": 80_000, "cache_write_tokens": 10_000,
        "completion_tokens": 500}
m = ta.TurnSpend(model="m", allowance_usd=2.00, price=P())
for _ in range(ta.MIN_CALLS_BEFORE_STOP):
    m.note_call(LAST, model="m")
reserve = CB.reply_reserve(1_000_000)
limit = CB.call_output_limit(128_000, 1_000_000, [{"role": "user", "content": "x" * 400_000}])
aff = m.affordable_output(5_000)
request = max(1, min(limit, max(aff, reserve)))
worst = m.spent + m.project(5_000, request)
check("the repro: a request held to the allowance cannot overrun by a whole call "
      f"(worst case ${worst:.2f} on $2.00, was $3.32)",
      worst <= 2.00 + reserve * P.output_per_token + 1e-9 and request < limit, (aff, request, worst))
check("...and the cap never goes below one realistic reply", request >= reserve, request)

INF._CATALOG_CACHE["data"] = {"openrouter_models": [
    {"id": "anthropic/claude-opus-5.5", "context_length": 1000000, "max_output_tokens": 128000,
     "input_per_token": "0.000005", "output_per_token": "0.000025",
     "cache_read_per_token": "0.0000005", "cache_write_per_token": "0.00000625"}],
    "total_openrouter": 1}
INF._CATALOG_CACHE["exp"] = 1e18
BIG = {"input_total_tokens": 100_000, "prompt_tokens": 100_000, "completion_tokens": 500,
       "cache_read_tokens": 0, "cache_write_tokens": 0}


def _turn(script, allowance, **kw):
    h = Harness(script, **kw)
    orig_run = h.run

    async def run():
        return await orig_run()
    import mastercode.context_budget as _cbm

    async def _allow(*a, **k):
        return allowance
    saved = _cbm.read_thread_allowance
    _cbm.read_thread_allowance = _allow
    try:
        asyncio.run(run())
    finally:
        _cbm.read_thread_allowance = saved
    return h

_rounds = [{"content": "", **BIG,
            "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": f"b{i}"}, i)],
            "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": f"b{i}"}, i)]}}
           for i in (1, 2, 3)]
h = _turn(_rounds + [{"content": "Done.", **BIG}], 3.00,
          meta_results={"build": [{"status": "error", "result": "x"}] * 3}, wb=True)
_mts = [k.get("max_tokens") for k in h.kw]
check("in a priced turn with an allowance every call — the first included — is held to "
      "what is left, shrinking as it is spent, never below one reply",
      _mts and _mts[0] < 128_000 and all(x >= 16_384 for x in _mts)
      and all(a >= b for a, b in zip(_mts, _mts[1:])) and _mts[-1] < _mts[0], _mts)
h0 = _turn(_rounds + [{"content": "Done.", **BIG}], None,
           meta_results={"build": [{"status": "error", "result": "x"}] * 3}, wb=True)
check("with no allowance nothing is clamped", all(x == 128_000 for x in
      [k.get("max_tokens") for k in h0.kw]), [k.get("max_tokens") for k in h0.kw])

# ── A: the FIRST call is held to the allowance too ($0.50 on a $5/$25 route
# sent 128,000 first — up to $3.70) ─────────────────────────────────────────
INF._CATALOG_CACHE["data"] = {"openrouter_models": [
    {"id": "anthropic/claude-opus-5.5", "context_length": 1000000, "max_output_tokens": 128000,
     "input_per_token": "0.000005", "output_per_token": "0.000025",
     "cache_read_per_token": "0.0000005", "cache_write_per_token": "0.00000625"}],
    "total_openrouter": 1}
hF = _turn(_rounds[:1] + [{"content": "Done.", **BIG}], 0.50,
           meta_results={"build": [{"status": "error", "result": "x"}]}, wb=True)
_m0 = hF.kw[0].get("max_tokens") if hF.kw else None
check("on $0.50 the first call is held to what $0.50 pays for (~20k at $25/M), never 128,000, "
      "and never below one reply (16,384)",
      isinstance(_m0, int) and 16_384 <= _m0 <= 20_000, _m0)
_worst_first = _m0 * 25e-6 if isinstance(_m0, int) else 9.9
check(f"...so its output can cost at most ${_worst_first:.2f}, not $3.20",
      _worst_first <= 0.50 + 1e-9 or _m0 == 16_384, _worst_first)
_cut_first = [{"content": "The first reply, cut", **BIG, "stop_reason": "max_tokens", "truncated": True,
               "output_limit": 20000},
              {"content": " and continued.", **BIG}]
hC = _turn(list(_cut_first), 0.50, wb=True)
check("a continuation after a cut FIRST reply passes the spend check too (out of room: not continued)",
      len(hC.calls) == 1, len(hC.calls))
hC2 = _turn(list(_cut_first), None, wb=True)
check("...and with no allowance it is continued", len(hC2.calls) == 2, len(hC2.calls))

# ── spend: a continuation passes the same check ──────────────────────────
_cut = [{"content": "", **BIG,
         "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "c"}, 1)],
         "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "c"}, 1)]}},
        {"content": "A long answer, cut", **BIG, "stop_reason": "max_tokens", "truncated": True,
         "output_limit": 128000},
        {"content": " and continued.", **BIG}]
h1 = _turn(list(_cut), 1.10, meta_results={"build": [{"status": "ok", "result": "x"}]}, wb=True)
check("spend: out of allowance, the cut reply is NOT continued (it was never checked)",
      len(h1.calls) == 2, len(h1.calls))
h2 = _turn(list(_cut), None, meta_results={"build": [{"status": "ok", "result": "x"}]}, wb=True)
check("spend: ...with no allowance it is continued as before", len(h2.calls) == 3, len(h2.calls))

# ── B: a cut with no visible text ────────────────────────────────────────
INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
hB = asyncio.run(Harness([
    {"content": "Checking the files.", "tool_calls": [_tc("build", {"action": "start", "brief": "b"})],
     "raw_message": {"content": "Checking the files.", "tool_calls": [_tc("build", {"action": "start", "brief": "b"})]}},
    {"content": "", "stop_reason": "max_tokens", "truncated": True, "output_limit": 1024},
    {"content": "Here is what I found."}],
    meta_results={"build": [{"status": "ok", "result": "done"}]}, wb=True).run())
check("a cut with no visible text is not glued onto the narration",
      reply_of(hB) == "Checking the files.\n\nHere is what I found.", repr(reply_of(hB)))

# ── C: a high input estimate ──────────────────────────────────────────────
huge = [{"role": "user", "content": "x" * 800_000}]            # ~200k tokens: no room by estimate
check("a high input estimate never cuts a request below the old fixed 16,384",
      CB.call_output_limit(128_000, 200_000, huge) == 16_384, CB.call_output_limit(128_000, 200_000, huge))
check("...held to the model's own limit when that is smaller",
      CB.call_output_limit(8_000, 200_000, huge) == 8_000)
check("...and to half the window on a small one", CB.call_output_limit(128_000, 20_000, huge) == 10_000)

# ── D: failure-shaped statuses ────────────────────────────────────────────
check("rate_limited (github_lane) is a failure, as unavailable — never a success",
      L.classify({"status": "rate_limited"}) == "error" and L.normalize_status("rate_limited") == "unavailable")
check("failed and ungrounded are failures",
      L.classify({"status": "failed"}) == "error" and L.classify({"status": "ungrounded"}) == "error")


class _RP:
    def __init__(self):
        self.q = []

    async def fetch(self, q, *a):
        self.q.append(q)
        if "tool_call_log" in q:
            return [{"lane": "host", "role": "chat", "provider": "p", "model": "m", "bucket": "other", "n": 3},
                    {"lane": "host", "role": "chat", "provider": "p", "model": "m", "bucket": "ok", "n": 5}]
        return []

_rp = _RP()
roll = asyncio.run(L.rollup(_rp, 3, 7))
_ent = (roll.get("models") or [{}])[0]
check("an unknown outcome status is a neutral OTHER in the rollup — not ok, not a model fault",
      any("CASE WHEN reason = 'ok' AND (status <> 'ok') THEN 'other'" in q for q in _rp.q)
      and _ent.get("reasons") == {"other": 3, "ok": 5} and _ent.get("model_fault") == 0, (_ent, _rp.q[:1]))
_hr = Harness([
    {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "r"})],
     "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "r"})]}},
    {"content": "GitHub held us off."}],
    meta_results={"build": [{"status": "rate_limited", "result": "retry in 60s"}]}, wb=True)
asyncio.run(_hr.run())
_rows = [a for q, a in _hr.pool.executed if "INSERT INTO error_events" in q]
check("rate_limited reaches the error lane as a platform 'unavailable' fault",
      len(_rows) == 1 and json.loads(_rows[0][-1]).get("fault") == "platform", _rows)

# ── the matchers, on REAL provider strings only — each with its source. No
# fixture claims a provider's words without one.
def _inv(m_):
    return json.dumps({"error": {"type": "invalid_request_error", "message": m_}})


def _anth(m_):
    return json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": m_}})


def _ovf(body, prov="kimi 400: "):
    return CB.is_context_overflow({"error": prov + body, "content": ""})

# anthropics/claude-code#23923
ANTH_OUT = _anth("max_tokens: 128001 > 128000, which is the maximum allowed number of output tokens for claude-opus-4-6")
# openai/openai-python#687
OAI_OUT = _inv("max_tokens is too large: 5500. This model supports at most 4096 completion tokens, whereas you provided 5500.")
# OpenHands/OpenHands#7023
ANTH_ROOM = _anth("input length and `max_tokens` exceed context limit: 196973 + 8192 > 204648, decrease input length "
                  "or `max_tokens` and try again")
# Doriandarko/claude-engineer#224
ANTH_PROMPT = _anth("prompt is too long: 203073 tokens > 200000 maximum")
# dyad-sh/dyad#1558 (OpenRouter)
OR_CTX = "This endpoint's maximum context length is 262144 tokens. However, you requested about 504212 tokens"
# openrouter.ai/docs/api-reference/errors (read 2026-10-02): the typed field and its documented description
OR_TYPED = json.dumps({"error": {"code": 400,
                                 "message": "The combined input and output tokens exceed the model's context window.",
                                 "metadata": {"error_type": "context_length_exceeded"}}})
# MoonshotAI/kimi-cli#2011, BerriAI/litellm#43013 — a WINDOW refusal
KIMI_WIN = _inv("Invalid request: Your request exceeded model token limit: 262144 (requested: 269030)")
# platform.kimi.ai/docs/api/errors (400 invalid_request_error)
KIMI_INPUT = _inv("Input token length too long")
KIMI_SPEC = _inv("prompt tokens + max_tokens exceeds the model specification")
# inference_cost_log, 2026-09-16, k3 (read-only)
KIMI_QUOTA = json.dumps({"error": {"message": "You've reached your 5-hour usage limit. Your quota will reset when "
                                              "the current 5-hour window ends."}})
# platform.kimi.ai/docs/api/errors (429)
KIMI_429 = ["Token quota is insufficient", "Organization-level TPM limit reached",
            "Organization-level TPD limit reached"]

check("Anthropic's output maximum is learned (claude-code#23923)",
      PG._refused_output(400, ANTH_OUT, 128_001) == (128_000, True) and not _ovf(ANTH_OUT, "anthropic 400: "))
check("OpenAI's output maximum is learned (openai-python#687)",
      PG._refused_output(400, OAI_OUT, 5_500) == (4_096, True) and not _ovf(OAI_OUT, "openai 400: "))
check("Anthropic 'A + B > C': this call at the room left, nothing learned; an overflow on the turn side",
      PG._refused_output(400, ANTH_ROOM, 8_192) == (7_675, False) and _ovf(ANTH_ROOM, "anthropic 400: "))
check("Anthropic 'prompt is too long': no resend (the input alone is over), an overflow",
      PG._refused_output(400, ANTH_PROMPT, 128_000) is None and _ovf(ANTH_PROMPT, "anthropic 400: "))
check("OpenRouter 'maximum context length … you requested about': an overflow",
      _ovf(OR_CTX, "openrouter 400: "))
check("OpenRouter's typed context_length_exceeded: this call at 16,384, never learned",
      PG._refused_output(400, OR_TYPED, 524_288) == (16_384, False))
_kw = PG._refused_output(400, KIMI_WIN, 131_072)
check("kimi's window refusal: this call at the room left (262144 - (269030 - 131072) = 124186), never "
      "learned as an output limit, and an overflow on the turn side",
      _kw == (124_186, False) and PG._learned_output_limit(400, KIMI_WIN, 131_072) is None and _ovf(KIMI_WIN), _kw)
check("kimi 'Input token length too long': no resend, an overflow (history is trimmed)",
      PG._refused_output(400, KIMI_INPUT, 131_072) is None and _ovf(KIMI_INPUT))
check("kimi 'prompt tokens + max_tokens exceeds the model specification': this call at 16,384, never "
      "learned, and an overflow on the turn side",
      PG._refused_output(400, KIMI_SPEC, 131_072) == (16_384, False) and _ovf(KIMI_SPEC))
check("kimi's real quota refusal (it says 'window'): never output, never overflow, never resent",
      PG._refused_output(403, KIMI_QUOTA, 131_072) is None and PG._refused_output(400, KIMI_QUOTA, 131_072) is None
      and not _ovf(KIMI_QUOTA, "kimi 403: "))
check("kimi's documented 429s: never output, never overflow",
      all(not _ovf(_inv(m_), "kimi 429: ") and PG._refused_output(400, _inv(m_), 131_072) is None for m_ in KIMI_429))
check("an invented 'Daily output limit of 10000 tokens' on a 400 is never learned",
      PG._refused_output(400, _inv("Daily output limit of 10000 tokens"), 128_000) is None)
check("no needle without a source: 'output limit' / 'output token limit' / 'completion token limit' / "
      "'max_output_tokens' are gone from the gateway's output list",
      not any(n in PG._OVERSIZE_NEEDLES for n in ("output limit", "output token limit", "completion token limit",
                                                  "max_output_tokens", "max_completion_tokens")))

# ── ids that contain "tpm"/"tpd" never change a refusal's class ────────────
def _with(body, **extra):
    obj = json.loads(body)
    return json.dumps({**extra, **obj})


ANTH_OUT_ID = _with(ANTH_OUT, request_id="req-011C-tpm-A9x-TPD")
ANTH_ROOM_ID = _with(ANTH_ROOM, request_id="req-011C-tpm-A9x-TPD")
ANTH_PROMPT_ID = _with(ANTH_PROMPT, request_id="req-011C-tpm-A9x-TPD")
OR_TYPED_ID = _with(OR_TYPED, user_id="user-2-tpd-9-TPM")
check("Anthropic's output maximum is still learned with a request_id containing 'tpm'",
      PG._refused_output(400, ANTH_OUT_ID, 128_001) == (128_000, True), PG._refused_output(400, ANTH_OUT_ID, 128_001))
check("'A + B > C' still resends at the room left with such an id",
      PG._refused_output(400, ANTH_ROOM_ID, 8_192) == (7_675, False), PG._refused_output(400, ANTH_ROOM_ID, 8_192))
check("OpenRouter's typed overflow still resends with a user_id containing 'tpd'",
      PG._refused_output(400, OR_TYPED_ID, 524_288) == (16_384, False), PG._refused_output(400, OR_TYPED_ID, 524_288))
check("'prompt is too long' is still an overflow on the turn side with such an id (in the first "
      "200 characters of the error string)",
      CB.is_context_overflow({"error": "anthropic 400: " + ANTH_PROMPT_ID[:200], "content": ""})
      and CB.is_context_overflow({"error": "anthropic 400: " + ANTH_PROMPT_ID[:200],
                                  "error_body": ANTH_PROMPT_ID, "content": ""}))
check("the sourced kimi 403 and 429s stay 'not size' (quota, whole words, message only)",
      not _ovf(KIMI_QUOTA, "kimi 403: ") and PG._refused_output(400, KIMI_QUOTA, 131_072) is None
      and all(not _ovf(_inv(m_), "kimi 429: ") for m_ in KIMI_429))

# ── Gemini, xAI and the local route ───────────────────────────────────────
# agno-agi/agno#10449, use-agent-os/agent-os#657
# Gemini's OpenAI-compatible endpoint answers a LIST of one error object —
# the real envelope (mealie-recipes/mealie#8551, NousResearch/hermes-agent#125497).
GEMINI_WIN = json.dumps([{"error": {"code": 400, "message": "The input token count (1048577) exceeds the "
                                                            "maximum number of tokens allowed (1048576).",
                                   "status": "INVALID_ARGUMENT"}}])
# agno-agi/agno#10449
XAI_WIN = _inv("This model's maximum prompt length is 131072 but the request contains 140000 tokens.")
check("Gemini's window refusal, in its real LIST envelope, is unwrapped and is an overflow",
      CB.refusal_parts(GEMINI_WIN)[0].startswith("The input token count")
      and _ovf(GEMINI_WIN, "google 400: "), CB.refusal_parts(GEMINI_WIN))
check("xAI's window refusal is an overflow", _ovf(XAI_WIN, "xai 400: "))
# The message is vLLM's, from infiniflow/ragflow#10406 (a local route); the
# JSON envelope around it is ours (only "message" is read). Likewise the
# Gemini and xAI messages above are sourced and their envelopes are ours.
VLLM_WIN = json.dumps({"message": "This model's maximum context length is 32768 tokens. "
                                  "However, your request has 32836 input tokens"})


class _LResp:
    status = 400

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return VLLM_WIN


class _LSess:
    def post(self, *a, **kw):
        return _LResp()


async def _lsess(_base):
    return _LSess()

_gs_saved = PG._get_session
PG._get_session = _lsess
try:
    _lr = asyncio.run(PG._call_local([{"role": "user", "content": "x"}], "qwen", base_url="http://10.0.0.5:8000"))
finally:
    PG._get_session = _gs_saved
check("a local route keeps the server's own words, so vLLM's window refusal is an overflow "
      "(it was 'Local model error: 400' only)",
      "maximum context length" in str(_lr.get("error")) and CB.is_context_overflow(_lr), _lr.get("error"))

# Any other local error text is not stored (it could echo the input).
class _LResp2(_LResp):
    async def text(self):
        return json.dumps({"message": "Invalid prompt: SECRET-PROMPT-TEXT could not be parsed"})


class _LSess2:
    def post(self, *a, **kw):
        return _LResp2()


async def _lsess2(_base):
    return _LSess2()
PG._get_session = _lsess2
try:
    _lr2 = asyncio.run(PG._call_local([{"role": "user", "content": "x"}], "qwen", base_url="http://10.0.0.5:8000"))
finally:
    PG._get_session = _gs_saved
check("a local error that is not a window refusal stores only its status — no server text",
      _lr2.get("error") == "Local model error: 400" and "SECRET" not in str(_lr2.get("error")), _lr2.get("error"))

# ── Groq — the message, and the typed code past character 200 ───────────────
# thepersonalaicompany/amurex-backend#51
GROQ_WIN = json.dumps({"error": {"message": "Please reduce the length of the messages or completion.",
                                 "type": "invalid_request_error", "param": "messages",
                                 "code": "context_length_exceeded"}})
check("Groq's window message is an overflow by its own words",
      CB.is_context_overflow({"error": "groq 400: " + GROQ_WIN[:200], "error_body": GROQ_WIN, "content": ""}))
LONG_CODE = json.dumps({"error": {"message": "Request too large for this model.", "type": "invalid_request_error",
                                  "param": "messages[0].content " + "x" * 240,
                                  "code": "context_length_exceeded"}})
check("the typed code is read from the whole body, even past character 200",
      LONG_CODE.index("context_length_exceeded") > 200
      and CB.is_context_overflow({"error": "groq 400: " + LONG_CODE[:200], "error_body": LONG_CODE,
                                  "content": ""}))

# ── the gateway KEEPS the whole refusal body (error_body) — driven
# through the real _call_remote, offline: an OpenRouter refusal whose typed
# code sits past character 200 is still read as an overflow by the turn.
import types as _types  # noqa: E402
_OR_LONG = json.dumps({"error": {"message": "Request too large for this endpoint.",
                                 "metadata": {"provider_name": "x" * 230,
                                              "error_type": "context_length_exceeded"}}})


class _ORResp:
    status = 400
    headers: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return _OR_LONG


class _ORSess:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **kw):
        return _ORResp()


async def _orkey():
    return "sk-or"

_saved_http, _saved_key = PG.aiohttp, PG._load_user_openrouter_key
PG.aiohttp = _types.SimpleNamespace(ClientSession=_ORSess, ClientTimeout=lambda **kw: None, ClientError=OSError)
PG._load_user_openrouter_key = _orkey
try:
    _orres = asyncio.run(PG._call_remote([{"role": "user", "content": "x"}], "openai/gpt-x", max_tokens=1000,
                                         provider="openrouter"))
finally:
    PG.aiohttp, PG._load_user_openrouter_key = _saved_http, _saved_key
check("the real _call_remote keeps the whole body, so a typed code past character 200 is read",
      _OR_LONG.index("context_length_exceeded") > 200 and _orres.get("error_body") == _OR_LONG
      and CB.is_context_overflow(_orres), {k: str(v)[:80] for k, v in _orres.items()})
check("the quota guard and the Groq needle are gone",
      not hasattr(CB, "is_quota_refusal") and not hasattr(CB, "QUOTA_WORDS")
      and "please reduce the length of the messages or completion" not in CB._OVERFLOW_NEEDLES)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
