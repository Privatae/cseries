#!/usr/bin/env python3
"""Each model's OWN output limit, and a cut that is said out loud.

A desktop design on one Host (claude-opus-5-5) was cut at a fixed
16,384-token output limit — the model's own is 128,000. The cut tool call ran as
`{}`, the Host was told "operate requires `tool`" and blamed itself. Nothing
read the stop reason, and the catalog fetched each model's output limit and
dropped it.

Container-free: APP_ROOT=<repo> python3 scripts/test_model_output_limit.py.
No pod, no DB, no network: the HTTP session and the credential are stand-ins.

  A. the catalog keeps the output limit (OpenRouter top_provider.
     max_completion_tokens; Anthropic /v1/models max_tokens beside
     max_input_tokens; carried to a first-party row by the OpenRouter match)
     and context_budget resolves it: catalog, provider-stated, static table,
     half the window at most, 16,384 only when nothing can say
  B. the gateway reads why the model stopped, on both wires, streamed and
     buffered: `truncated`, `truncated_tool_call_ids`, `output_limit`
  C. a long call the caller did not stream is streamed by the gateway
     (Anthropic: "stream for anything above ~16K output")
  D. a provider that refuses the max_tokens names its limit: learned, resent
  E. call_model with no max_tokens sends the dialled model's own limit
  F. a cold catalog on the turn path is warmed once from the provider's own
     list (89 of 301 k3 turns on one Host ran with the window unknown)

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys
import types

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[output-limit] FAIL {name} {extra}")


from server.routes import inference as INF  # noqa: E402
from mastercode import context_budget as CB  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402

# ── A. the catalog keeps it, context_budget resolves it ───────────────────
or_opus = {"id": "anthropic/claude-opus-5.5", "context_length": 1000000,
           "top_provider": {"max_completion_tokens": 128000}}
check("A1 OpenRouter's top_provider.max_completion_tokens is read",
      getattr(INF, "_or_max_output", lambda m: None)(or_opus) == 128000)
check("A1b ...and an absent one stays absent (never a limit)",
      getattr(INF, "_or_max_output", lambda m: 1)({"id": "x/y"}) is None)
_src = open(os.path.join(APP_ROOT, "server/routes/inference.py")).read()
_build = _src[_src.index("def _build_model_catalog"):]
_build = _build[:_build.index("fetch_ok = True")]
check("A2 the catalog build puts it on the OpenRouter row",
      '"max_output_tokens": _or_max_output(m)' in _build)

row = INF._dm_row({"id": "claude-fable-5-1", "max_input_tokens": 1000000, "max_tokens": 128000})
check("A3 Anthropic /v1/models: window from max_input_tokens, output from max_tokens",
      row.get("context_length") == 1000000 and row.get("max_output_tokens") == 128000, row)
row2 = INF._dm_row({"id": "some-model", "max_tokens": 32768})
check("A4 a provider whose max_tokens means the WINDOW gets no output limit from it",
      row2.get("context_length") == 32768 and "max_output_tokens" not in row2, row2)
row3 = INF._dm_row({"id": "k", "max_output_tokens": 65536, "context_length": 262144})
check("A5 an explicit max_output_tokens is read", row3.get("max_output_tokens") == 65536, row3)

# caches → accessor → resolver
INF._CATALOG_CACHE["data"] = {"openrouter_models": [
    {"id": "anthropic/claude-opus-5.5", "context_length": 1000000, "max_output_tokens": 128000},
    {"id": "moonshotai/kimi-k3", "context_length": 1048576, "max_output_tokens": 943718},
    {"id": "tiny/model-8k", "context_length": 8192, "max_output_tokens": 8192},
], "total_openrouter": 3}
INF._CATALOG_CACHE["exp"] = 1e18
INF._DIRECT_MODELS_CACHE.clear()
check("A6 cached_max_output_tokens reads the warm catalog",
      getattr(INF, "cached_max_output_tokens", dict)().get("moonshotai/kimi-k3") == 943718)
first = [{"id": "kimi-k3"}]
INF._enrich_first_party_from_openrouter("kimi", first)
check("A7 a first-party row with no published limit takes OpenRouter's by the same match",
      first[0].get("max_output_tokens") == 943718, first)

mol = getattr(CB, "model_output_limit", None)
olf = getattr(CB, "output_limit_for", None)
check("A8 context_budget.model_output_limit exists", callable(mol) and callable(olf))
if callable(mol) and callable(olf):
    check("A9 a dashed first-party Anthropic id finds the dotted OpenRouter row",
          mol("claude-opus-5-5", "anthropic") == 128000, mol("claude-opus-5-5", "anthropic"))
    check("A10 OpenRouter id, exact", mol("moonshotai/kimi-k3", "openrouter") == 943718)
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
    check("A11 cold catalog: Claude falls to the static table (128K)",
          mol("claude-opus-5-5", "anthropic") == 128000 and mol("claude-haiku-4-5", "anthropic") == 64000)
    check("A12 cold catalog, unknown model: unknown", mol("nobody/knows", "openrouter") is None)
    check("A13 unknown → 16,384 (the only place the old number lives)",
          olf("nobody/knows", "openrouter", None) == CB.DEFAULT_OUTPUT_RESERVE == 16384)
    check("A14 never more than half the window (a request must stay valid)",
          olf("claude-opus-5-5", "anthropic", 100000) == 50000)
    CB.learn_output_limit("kimi", "k3", 32000)
    check("A15 what a provider stated beats the table", mol("k3", "kimi") == 32000)
    CB._LEARNED_OUTPUTS.clear()

# A2: kimi publishes no output limit — the declared alias table
if callable(mol):
    INF._DIRECT_MODELS_CACHE.clear()
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "moonshotai/kimi-k3", "context_length": 1048576, "max_output_tokens": 943718},
        {"id": "moonshotai/kimi-k2.7-code", "context_length": 262144, "max_output_tokens": 235929}],
        "total_openrouter": 2}
    # kimi's own rows, as its coding endpoint lists them (context only)
    INF._DIRECT_MODELS_CACHE["kimi"] = {"data": [
        INF._dm_row({"id": "k3", "display_name": "K3", "context_length": 1048576,
                     "limit": {"context": 1048576}}),
        INF._dm_row({"id": "k3-256k", "display_name": "K3-256k", "context_length": 262144,
                     "limit": {"context": 262144}})], "exp": 1e18}
    check("A16 the alias table is declared and pins k3 to OpenRouter's kimi-k3",
          CB.OUTPUT_LIMIT_ALIASES.get(("kimi", "k3")) == "moonshotai/kimi-k3"
          and CB.OUTPUT_LIMIT_ALIASES.get(("kimi", "k3-256k")) == "moonshotai/kimi-k3")
    check("A17 a k3 route resolves to the published 943,718, not 16,384",
          mol("k3", "kimi") == 943718, mol("k3", "kimi"))
    _w = asyncio.run(CB.model_context_window("k3", provider="kimi"))
    check("A18 ...and a k3 call carries half its own 1,048,576 window (524,288), never 16,384",
          olf("k3", "kimi", _w) == 524288, (olf("k3", "kimi", _w), _w))
    _w2 = asyncio.run(CB.model_context_window("k3-256k", provider="kimi"))
    check("A19 k3-256k: the same row, held to half ITS window (131,072)",
          olf("k3-256k", "kimi", _w2) == 131072, (olf("k3-256k", "kimi", _w2), _w2))
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
    INF._DIRECT_MODELS_CACHE.clear()
    check("A20 cold catalog: k3 still resolves to 943,718 (the published figure, pinned), not 16,384",
          mol("k3", "kimi") == 943718, mol("k3", "kimi"))
    check("A21 an unmapped kimi id stays unknown (no guessed identity)",
          mol("kimi-for-coding", "kimi") is None)
    # A window-only route (kimi-for-coding: context_length 1,048,576,
    # no output limit anywhere) asks for HALF its window, never 16,384.
    INF._DIRECT_MODELS_CACHE["kimi"] = {"data": [
        INF._dm_row({"id": "kimi-for-coding", "display_name": "K2.8 Preview",
                     "context_length": 1048576, "limit": {"context": 1048576}})], "exp": 1e18}
    _wk = asyncio.run(CB.model_context_window("kimi-for-coding", provider="kimi"))
    check("A22 a window-only route (no published output limit) sends half its window, not 16,384",
          _wk == 1048576 and olf("kimi-for-coding", "kimi", _wk) == 524288,
          (_wk, olf("kimi-for-coding", "kimi", _wk)))
    import logging as _lg
    _seen_warn: list = []

    class _H(_lg.Handler):
        def emit(self, rec):
            _seen_warn.append(rec.getMessage())
    _h = _H()
    CB.log.addHandler(_h)
    try:
        _none = olf("nobody/at-all", "openrouter", None)
    finally:
        CB.log.removeHandler(_h)
    check("A23 neither known: 16,384, and a warning says so",
          _none == 16384 and any("neither the output limit nor the window" in m for m in _seen_warn),
          (_none, _seen_warn))
    INF._DIRECT_MODELS_CACHE.clear()

# ── B/C/D. the gateway: stop reasons, internal streaming, learning ────────
SENT: list[dict] = []
REPLIES: list = []


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for c in self._chunks:
            yield c


class _Resp:
    headers: dict = {}

    def __init__(self, spec):
        self.status = spec.get("status", 200)
        self._spec = spec
        self.content = _Content([("data: " + json.dumps(e) + "\n").encode()
                                 for e in spec.get("sse", [])])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._spec.get("json", {})

    async def text(self):
        return self._spec.get("text", json.dumps(self._spec.get("json", {})))


class _Session:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        SENT.append(dict(json))
        return _Resp(REPLIES.pop(0))


PG.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=lambda **kw: None,
                                   ClientError=OSError)


async def _key():
    return "sk-test"


PG._load_user_openrouter_key = _key
import server.vault.providers as VP  # noqa: E402


async def _resolve(*a, **kw):
    return types.SimpleNamespace(secret="sk-ant", cred_type="api_key")


VP.resolve = _resolve


async def _noop_log(*a, **kw):
    return None


PG._log_inference = _noop_log
streamed: list[str] = []


async def _cb(t):
    streamed.append(t)

# B1: Anthropic stream, the tool_use block cut (no content_block_stop) at max_tokens
_cut_sse = [
    {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 10}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Writing the desktop."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_ok", "name": "recall"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"q\": \"x\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_cut", "name": "operate"}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"tool\": \"artifact\", \"args\": {\"html\": \"<div>"}},
    {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 16384}},
]


def run(coro):
    return asyncio.run(coro)


REPLIES[:] = [{"sse": _cut_sse}]
res = run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-opus-5-5",
                             max_tokens=16384, stream_cb=_cb))
check("B1 Anthropic stream: the stop reason is read", res.get("stop_reason") == "max_tokens", res.get("stop_reason"))
check("B2 ...the cut is flagged, with the limit sent", res.get("truncated") is True and res.get("output_limit") == 16384)
check("B3 ...only the block that never closed is named as cut",
      res.get("truncated_tool_call_ids") == ["toolu_cut"], res.get("truncated_tool_call_ids"))

# B4: Anthropic buffered at max_tokens: the cut block's input reads as a valid {}
REPLIES[:] = [{"json": {"model": "claude-opus-5-5", "stop_reason": "max_tokens",
                        "usage": {"input_tokens": 5, "output_tokens": 100},
                        "content": [{"type": "text", "text": "ok"},
                                    {"type": "tool_use", "id": "toolu_last", "name": "operate", "input": {}}]}}]
res = run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-opus-5-5", max_tokens=1000))
check("B4 Anthropic buffered: the last call is the cut one even though `{}` parses",
      res.get("truncated") is True and res.get("truncated_tool_call_ids") == ["toolu_last"], res)

REPLIES[:] = [{"json": {"model": "claude-opus-5-5", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 3},
                        "content": [{"type": "text", "text": "done"}]}}]
res = run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-opus-5-5", max_tokens=1000))
check("B5 a finished reply is not flagged", res.get("stop_reason") == "end_turn" and not res.get("truncated"))

# B6: OpenAI wire, streamed, finish_reason length mid-arguments
_or_sse = [
    {"model": "moonshotai/kimi-k3", "choices": [{"delta": {"content": "Here"}, "finish_reason": None}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "operate", "arguments": "{\"tool\": \"art"}}]}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"prompt_tokens": 3, "completion_tokens": 9}},
]
REPLIES[:] = [{"sse": _or_sse}]
res = run(PG._call_remote([{"role": "user", "content": "go"}], "moonshotai/kimi-k3",
                          max_tokens=16384, stream_cb=_cb, provider="openrouter"))
check("B6 OpenAI wire stream: finish_reason length is a cut, and the call is named",
      res.get("truncated") is True and res.get("truncated_tool_call_ids") == ["call_1"]
      and res.get("stop_reason") == "length", res)

# C: a long call nobody streamed is streamed by the gateway — same relay
REPLIES[:] = [{"sse": [
    {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 4}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "long answer"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}]}]
SENT.clear()
res = run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-opus-5-5", max_tokens=128000))
check("C1 max_tokens 128,000 with no stream_cb goes out streamed",
      SENT and SENT[0].get("stream") is True and SENT[0].get("max_tokens") == 128000, SENT[:1])
check("C2 ...and returns the same result as a buffered call", res.get("content") == "long answer", res)
REPLIES[:] = [{"json": {"model": "m", "stop_reason": "end_turn", "usage": {},
                        "content": [{"type": "text", "text": "short"}]}}]
SENT.clear()
run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-opus-5-5", max_tokens=16384))
check("C3 up to 16,384 a non-streamed call is sent exactly as before", SENT and "stream" not in SENT[0])

# D: the provider refuses the max_tokens and names its limit
REPLIES[:] = [
    {"status": 400, "text": '{"type":"error","error":{"type":"invalid_request_error","message":'
                            '"max_tokens: 128000 > 64000, which is the maximum allowed number of '
                            'output tokens for claude-old"}}'},
    {"sse": [  # the call is long, so it goes out streamed (C1)
        {"type": "message_start", "message": {"model": "claude-old", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fine"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}]}]
SENT.clear()
res = run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-old", max_tokens=128000))
check("D1 a refused max_tokens is resent at the limit the provider named",
      len(SENT) == 2 and SENT[0].get("max_tokens") == 128000
      and SENT[1].get("max_tokens") == 64000 and res.get("content") == "fine",
      [s.get("max_tokens") for s in SENT])
check("D2 ...and remembered for the next call", CB.model_output_limit("claude-old", "anthropic") == 64000)
check("D3 a refusal of the size that names no number resends THIS call at 16,384, remembering nothing",
      PG._refused_output(400, '{"error":"max_tokens is too large for this model"}', 524288) == (16384, False)
      and PG._learned_output_limit(400, '{"error":"max_tokens is too large for this model"}', 524288) is None
      and PG._refused_output(400, '{"error":"max_tokens is too large"}', 8000) is None)
CB._LEARNED_OUTPUTS.clear()

# E: call_model with no max_tokens sends the dialled model's own limit
_seen: dict = {}


async def _fake_anthropic(**kw):
    _seen["anthropic"] = kw.get("max_tokens")
    return {"content": "ok", "tokens_used": 1}


async def _fake_remote(**kw):
    _seen["remote"] = kw.get("max_tokens")
    return {"content": "ok", "tokens_used": 1}

_real = (PG._call_anthropic, PG._call_remote)
PG._call_anthropic, PG._call_remote = _fake_anthropic, _fake_remote
try:
    from mastercode.runtime import model_standard as _MS
    _ms_ref = _MS.refusal
    _MS.refusal = lambda *a, **kw: None
    import mastercode.attachment_parts as _AP

    async def _for_wire(messages, wire, images_as_notes=False, **_kw):  # refused=, notes_why=
        return messages
    _AP_saved = (_AP.for_wire, _AP.model_reads_images)
    _AP.for_wire = _for_wire
    _AP.model_reads_images = lambda p, m: None
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "moonshotai/kimi-k3", "context_length": 1048576, "max_output_tokens": 943718}],
        "total_openrouter": 1}
    run(PG.call_model([{"role": "user", "content": "x"}], provider="anthropic",
                      provider_model="claude-opus-5-5"))
    check("E1 call_model(max_tokens=None) on claude-opus-5-5 sends 128,000",
          _seen.get("anthropic") == 128000, _seen)
    run(PG.call_model([{"role": "user", "content": "x"}], provider="openrouter",
                      provider_model="moonshotai/kimi-k3"))
    check("E2 ...and on k3 (1,048,576 window) sends half the window at most, from its own 943,718",
          _seen.get("remote") == 524288, _seen)
    run(PG.call_model([{"role": "user", "content": "x"}], max_tokens=777, provider="openrouter",
                      provider_model="moonshotai/kimi-k3"))
    check("E3 an explicit max_tokens is still sent as given", _seen.get("remote") == 777, _seen)
finally:
    PG._call_anthropic, PG._call_remote = _real
    _MS.refusal = _ms_ref
    _AP.for_wire, _AP.model_reads_images = _AP_saved

# ── F. the turn path warms a cold route once ──────────────────────────────
_calls: list = []


async def _dm(p):
    _calls.append(p)
    INF._DIRECT_MODELS_CACHE[p] = {"data": [{"id": "k-new", "context_length": 262144}], "exp": 1e18}
    return INF._DIRECT_MODELS_CACHE[p]["data"], None

INF._DIRECT_MODELS_CACHE.clear()
INF._CATALOG_CACHE["data"] = {"openrouter_models": [{"id": "x/y"}], "total_openrouter": 1}
_saved_dm = INF._direct_models
INF._direct_models = _dm
_saved_gp = INF.brain_service.get_pool
_nopool = INF.brain_service.get_pool()
try:
    check("F0 outside a running server (no brain pool) the warm never reaches the network",
          _nopool is not None or (run(INF.warm_route_catalog("kimi")) is None and _calls == []), _calls)
    INF.brain_service.get_pool = lambda: object()
    before = run(CB.model_context_window("k-new", provider="kimi"))
    run(INF.warm_route_catalog("kimi"))
    after = run(CB.model_context_window("k-new", provider="kimi"))
    run(INF.warm_route_catalog("kimi"))
    check("F1 a cold first-party route is unknown, then known after one warm",
          before is None and after == 262144, (before, after))
    check("F2 ...from the provider's own list, fetched once", _calls == ["kimi"], _calls)
finally:
    INF._direct_models = _saved_dm
    INF.brain_service.get_pool = _saved_gp
_sess = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/session.py")).read()
check("F3 the turn warms the route when its window or output limit is unknown",
      "await warm_route_catalog(_chat_route.get(\"provider\"))" in _sess)
check("F4 no model call in the turn carries the old fixed number",
      "max_tokens=16384" not in _sess and "max_tokens: int = 16384" not in _sess)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
