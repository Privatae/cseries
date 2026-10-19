#!/usr/bin/env python3
"""Output limits and the context window: the request, the stream and history.

Container-free: APP_ROOT=<repo> python3 scripts/test_output_limits_and_context_window.py.
No pod, no DB, no network (stand-in HTTP session and credential).

  - a refusal about the WINDOW ("A + B > C", "maximum context length")
    resends THAT call at the room left and remembers nothing; a refusal about
    the model's own output maximum is resent at it AND remembered
    (repro_learned_room.py, exactly)
  - history and the mid-turn trim keep a realistic reply free, never the
    output limit (repro_history_budget.py numbers: the branch's
    budget equals main's on every route), and each request is clamped to the
    room the window leaves after that call's input
  - the Anthropic stream: a cut tool call is named whichever way its block
    closed (with or without content_block_stop)
  - the gateway calls on_alive for every streamed event — text, thinking and
    tool-argument deltas
  - on a cold catalog k3-256k's alias figure is held to half ITS window
    (262,144 -> 131,072)

(The Host-loop halves of the stream checks are in
scripts/test_turn_cut_output.py.)

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
        print(f"[g1-round4] FAIL {name} {extra}")


from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import context_budget as CB  # noqa: E402
from server.routes import inference as INF  # noqa: E402

INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
INF._CATALOG_CACHE["exp"] = 1e18
INF._DIRECT_MODELS_CACHE.clear()

m, p, win = "claude-opus-5-5", "anthropic", 1_000_000
before = CB.output_limit_for(m, p, win)
room_body = ('{"type":"error","error":{"type":"invalid_request_error","message":"input length and '
             '`max_tokens` exceed context limit: 996000 + 128000 > 1000000, decrease input length or '
             '`max_tokens` and try again"}}')
ref = PG._refused_output(400, room_body, 128000)
check("the window refusal resends THIS call at the room left (4,000), not remembered",
      ref == (4000, False), ref)
check("...and nothing is learned from it (the repro: 128,000 stays 128,000)",
      PG._learned_output_limit(400, room_body, 128000) is None
      and CB.output_limit_for(m, p, win) == before == 128000, CB.output_limit_for(m, p, win))
oa_body = ("This model's maximum context length is 131072 tokens. However, you requested 140000 tokens "
           "(74464 in the messages, 65536 in the completion). Please reduce the length of the messages "
           "or completion.")
check("the OpenAI/vLLM window refusal: this call at 131,072 - 74,464 = 56,608, not remembered",
      PG._refused_output(400, oa_body, 65536) == (56608, False), PG._refused_output(400, oa_body, 65536))
max_body = ('{"type":"error","error":{"message":"max_tokens: 200000 > 128000, which is the maximum '
            'allowed number of output tokens for claude-x"}}')
check("a refusal about the model's output maximum is resent at it AND remembered",
      PG._refused_output(400, max_body, 200000) == (128000, True)
      and PG._learned_output_limit(400, max_body, 200000) == 128000)

# The call path: a window refusal then success — resent once at the room, the
# limit table untouched.
SENT: list = []
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
        self.content = _Content([("data: " + json.dumps(e) + "\n").encode() for e in spec.get("sse", [])])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._spec.get("json", {})

    async def text(self):
        return self._spec.get("text", "")


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
import server.vault.providers as VP  # noqa: E402


async def _resolve(*a, **kw):
    return types.SimpleNamespace(secret="sk-ant", cred_type="api_key")

VP.resolve = _resolve


async def _noop(*a, **kw):
    return None

PG._log_inference = _noop
_OK = [{"type": "message_start", "message": {"model": "m", "usage": {}}},
       {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
       {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fine"}},
       {"type": "content_block_stop", "index": 0},
       {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}]
REPLIES[:] = [{"status": 400, "text": room_body}, {"sse": _OK}]
res = asyncio.run(PG._call_anthropic([{"role": "user", "content": "x"}], m, max_tokens=128000))
check("the call path: resent once at the room, and the answer arrives",
      [s_.get("max_tokens") for s_ in SENT] == [128000, 4000] and res.get("content") == "fine",
      [s_.get("max_tokens") for s_ in SENT])
check("...and the model's limit is unchanged afterwards", CB.output_limit_for(m, p, win) == 128000)
CB._LEARNED_OUTPUTS.clear()

SYS = 20_000
_rows = []
for prov, model, w in (("kimi", "k3", 1_048_576), ("anthropic", "claude-opus-5-5", 1_000_000),
                       ("anthropic", "claude-sonnet-4-6", 200_000), ("kimi", "kimi-for-coding", 262_144),
                       ("openrouter", "some/unknown-128k", 131_072)):
    main_budget = CB.derive_turn_budget(window=w, output_reserve=CB.clamp_output_reserve(w, 16384),
                                        system_tokens=SYS)
    branch_budget = CB.derive_turn_budget(window=w, output_reserve=CB.reply_reserve(w), system_tokens=SYS)
    _rows.append((model, main_budget, branch_budget, w - 16384, w - CB.reply_reserve(w)))
check("history budget on the five measured routes equals main's (k3 961k, not 479k)",
      all(r[1] == r[2] for r in _rows) and _rows[0][2] > 960_000, _rows)
check("the mid-turn trim target equals main's (window - 16,384)", all(r[3] == r[4] for r in _rows), _rows)
_sess = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/session.py")).read()
check("the turn derives history and the trim target from the reply reserve, not the output limit",
      "output_reserve=_reply_reserve," in _sess
      and "_ctx_target = (_ctx_window - _reply_reserve)" in _sess
      and "output_reserve=effective_max" not in _sess
      and "_ctx_target = (_ctx_window - effective_max)" not in _sess)
_sa = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/scoped_agent.py")).read()
check("the agent's trim keeps the reply reserve too (the sent limit no longer becomes it)",
      "_agent_reserve = _cb.clamp_output_reserve(_agent_window, _sent)" not in _sa)
big = [{"role": "user", "content": "x" * 600_000}]                       # ~150k tokens
mt = CB.call_output_limit(64_000, 200_000, big)
check("each request is clamped to the room after its input: haiku 64k limit, 200k window, ~150k in",
      1024 <= mt <= 200_000 - 150_000 and mt < 64_000, mt)
check("a small input keeps the full limit",
      CB.call_output_limit(128_000, 1_000_000, [{"role": "user", "content": "hi"}]) == 128_000)
_seen: dict = {}


async def _fake_anth(**kw):
    _seen["mt"] = kw.get("max_tokens")
    return {"content": "ok"}

_real_anth = PG._call_anthropic
PG._call_anthropic = _fake_anth
from mastercode.runtime import model_standard as _MS  # noqa: E402
import mastercode.attachment_parts as _AP  # noqa: E402
_ms_ref, _ap_ref = _MS.refusal, (_AP.for_wire, _AP.model_reads_images)
_MS.refusal = lambda *a, **kw: None


async def _for_wire(messages, wire, images_as_notes=False, **_kw):  # refused=, notes_why=
    return messages
_AP.for_wire, _AP.model_reads_images = _for_wire, (lambda p_, m_: None)
try:
    asyncio.run(PG.call_model(big, provider="anthropic", provider_model="claude-haiku-4-5"))
finally:
    PG._call_anthropic = _real_anth
    _MS.refusal = _ms_ref
    _AP.for_wire, _AP.model_reads_images = _ap_ref
check("call_model clamps the request to the room its input leaves (haiku-4-5, ~150k in)",
      isinstance(_seen.get("mt"), int) and 1024 <= _seen["mt"] <= 50_000, _seen)

# ── the Anthropic stream ─────────────────────────────────────────
alive: list = []
_closed_cut = [
    {"type": "message_start", "message": {"model": "m", "usage": {}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_c", "name": "operate"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"tool\": \"art"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "ifact\", \"html\": \"<di"}},
    {"type": "content_block_stop", "index": 1},           # closed although cut
    {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {}},
]
_open_cut = [e for e in _closed_cut if not (e["type"] == "content_block_stop" and e["index"] == 1)]
for label, events in (("closed", _closed_cut), ("never closed", _open_cut)):
    alive.clear()
    REPLIES[:] = [{"sse": events}]
    r = asyncio.run(PG._call_anthropic([{"role": "user", "content": "x"}], m, max_tokens=128000,
                                       on_alive=lambda: alive.append(1)))
    check(f"a cut tool call whose block {label} is named as cut",
          r.get("truncated") is True and r.get("truncated_tool_call_ids") == ["toolu_c"], r)
check("on_alive fires for every streamed event — thinking and tool-argument deltas included",
      len(alive) == len(_open_cut), (len(alive), len(_open_cut)))

INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
INF._DIRECT_MODELS_CACHE.clear()
w256 = asyncio.run(CB.model_context_window("k3-256k", provider="kimi"))
check("cold catalog: k3-256k's window is its own (262,144) and its 943,718 is held to half (131,072)",
      w256 == 262_144 and CB.output_limit_for("k3-256k", "kimi", w256) == 131_072,
      (w256, CB.output_limit_for("k3-256k", "kimi", w256)))

# ── H7: the wall's window is set for the turn and for an agent run ────────
import contextvars  # noqa: E402
from mastercode.runtime import tool_result_wall as TRW  # noqa: E402
_had = hasattr(TRW, "TOOL_WALL_WINDOW")
_saved_var = getattr(TRW, "TOOL_WALL_WINDOW", None)
try:
    if hasattr(TRW, "TOOL_WALL_WINDOW"):
        delattr(TRW, "TOOL_WALL_WINDOW")
    check("H7a without the window setter in the tree the setter is a no-op, never an error",
          CB.set_tool_wall_window(1_000_000) is False)
    TRW.TOOL_WALL_WINDOW = contextvars.ContextVar("tool_wall_window_test", default=None)

    async def _in_task():
        ok = CB.set_tool_wall_window(1_000_000)
        return ok, TRW.TOOL_WALL_WINDOW.get()
    _ok, _got = asyncio.run(_in_task())
    check("H7b with TOOL_WALL_WINDOW the window is set for the task", _ok and _got == 1_000_000,
          (_ok, _got))
    async def _unknown_in_task():
        TRW.TOOL_WALL_WINDOW.set(1_000_000)          # what a parent (the Host turn) held
        ok = CB.set_tool_wall_window(None)
        return ok, TRW.TOOL_WALL_WINDOW.get()
    _ok, _got = asyncio.run(_unknown_in_task())
    check("H7c ...and an unknown window sets it to None (the legacy 100 KB wall applies), "
          "never keeping what the parent held", _ok is False and _got is None,
          (_ok, _got))
finally:
    if _had:
        TRW.TOOL_WALL_WINDOW = _saved_var
    elif hasattr(TRW, "TOOL_WALL_WINDOW"):
        delattr(TRW, "TOOL_WALL_WINDOW")
check("H7d the Host turn sets it once from the routed window, and every agent run from its own",
      "_cb.set_tool_wall_window(_ctx_window)" in _sess
      and "_cb.set_tool_wall_window(_agent_window)" in _sa)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
