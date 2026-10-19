#!/usr/bin/env python3
"""The Host turn's model call, through the REAL wrapper (a release that crashed every turn).

Container-free: APP_ROOT=<repo> python3 scripts/test_chat_call_kw_reaches_wrapper.py.
No pod, no DB, no network: only the HTTP session and the credential lookup are
stand-ins.

One release put `"on_alive": _turn_alive` into the turn's `_chat_call_kw`
(session.py) while runtime/tools._call_entity_model did not accept it, and
every chat turn died with "_call_entity_model() got an unexpected keyword
argument 'on_alive'". Every turn-level test replaces session._call_entity_model
with a stub that takes **kw, so the real wrapper's signature was never met.

This test builds the dict the way session.py builds it: the `_chat_call_kw`
literal is read out of session.py and evaluated, and the explicit keywords come
from session.py's own call sites that spread it. Nothing here restates the key
list, so a key added to the turn tomorrow is driven through the wrapper too.

  1 the turn's kwargs bind to the real _call_entity_model and the call returns
    the model's answer (fails on the crashing release with the TypeError)
  2 on_alive reaches the wire: it fires for every streamed event of the call
  3 ...and on the route's failover dial: a primary that errors fails over, and
    the fallback's stream marks the turn alive too

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import ast
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
        print(f"[chat-call-kw] FAIL {name} {extra}")


SESSION = os.path.join(APP_ROOT, "brain/mastercode/runtime/session.py")
_tree = ast.parse(open(SESSION).read())

# ── the turn's kwargs dict, as session.py writes it ───────────────────────
_literal = None
for _n in ast.walk(_tree):
    if (isinstance(_n, ast.Assign) and len(_n.targets) == 1
            and isinstance(_n.targets[0], ast.Name) and _n.targets[0].id == "_chat_call_kw"
            and isinstance(_n.value, ast.Dict)):
        _literal = _n.value
        break
if _literal is None:
    print("[chat-call-kw] FAIL session.py no longer assigns _chat_call_kw a dict literal "
          "— point this test at what builds the turn's call kwargs now")
    sys.exit(1)

ALIVE: list = []


def _turn_alive(*a, **kw):
    ALIVE.append(1)


ROUTE = {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": None,
         "fallback_provider": "anthropic", "fallback_model": "claude-sonnet-4-6",
         "reasoning_inline": False}
_names = {n.id for n in ast.walk(_literal) if isinstance(n, ast.Name)}
_ns: dict = {n: None for n in _names}
CONV_KEY = "conv-test-key"
_ns.update({"_inference_provider": ROUTE["provider"], "_inference_model": ROUTE["model"],
            "_route_base_url": ROUTE["base_url"], "_chat_route": ROUTE,
            "_turn_alive": _turn_alive, "bool": bool, "_cache_key": CONV_KEY})
CHAT_CALL_KW = eval(compile(ast.Expression(_literal), SESSION, "eval"), {"__builtins__": {}}, _ns)

# The keywords session.py passes NEXT TO the spread, at every call that
# reaches _call_entity_model with it (directly or through
# _call_with_overflow_retry, which forwards its **kw unchanged).
_EXPLICIT: set = set()
for _n in ast.walk(_tree):
    if not isinstance(_n, ast.Call):
        continue
    _f = _n.func
    _name = _f.id if isinstance(_f, ast.Name) else getattr(_f, "attr", None)
    if _name not in ("_call_entity_model", "_call_with_overflow_retry"):
        continue
    if any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id == "_chat_call_kw"
           for k in _n.keywords):
        _EXPLICIT |= {k.arg for k in _n.keywords if k.arg}


async def _stream_cb(*a, **kw):
    return None

_VALUES = {"temperature": 0.3, "max_tokens": 4096, "session_id": "turn-test",
           "tools": [{"type": "function", "function": {"name": "noop", "description": "x",
                                                        "parameters": {"type": "object", "properties": {}}}}],
           "reasoning_effort": "low", "stream_cb": _stream_cb}
EXPLICIT_KW = {k: _VALUES.get(k) for k in _EXPLICIT}

check("the turn's kwargs carry on_alive (what the crashing release sent)", "on_alive" in CHAT_CALL_KW,
      sorted(CHAT_CALL_KW))

# ── only the HTTP layer and the credential are stand-ins ──────────────────
from mastercode import provider_gateway as PG  # noqa: E402
from server.routes import inference as INF  # noqa: E402

INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
INF._CATALOG_CACHE["exp"] = 1e18
INF._DIRECT_MODELS_CACHE.clear()

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
        SENT.append(dict(json or {}))
        return _Resp(REPLIES.pop(0))


PG.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=lambda **kw: None,
                                   ClientError=OSError)
import server.vault.providers as VP  # noqa: E402


async def _resolve(*a, **kw):
    return types.SimpleNamespace(secret="sk-ant-test", cred_type="api_key")

VP.resolve = _resolve


async def _noop(*a, **kw):
    return None

PG._log_inference = _noop
PG._log_call_failure = _noop

_OK = [{"type": "message_start", "message": {"model": "m", "usage": {}}},
       {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
       {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fine"}},
       {"type": "content_block_stop", "index": 0},
       {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}]

from mastercode.runtime.tools import _call_entity_model  # noqa: E402  (the REAL wrapper)

MSGS = [{"role": "system", "content": "you are the host"}, {"role": "user", "content": "hi"}]


def _turn_call():
    return asyncio.run(_call_entity_model(list(MSGS), **EXPLICIT_KW, **CHAT_CALL_KW))


# 1 + 2: the primary answers
ALIVE.clear()
REPLIES[:] = [{"sse": _OK}]
try:
    res = _turn_call()
    err = None
except TypeError as e:
    res, err = {}, e
check("1 the turn's kwargs bind to the real _call_entity_model and the answer comes back",
      err is None and res.get("content") == "fine", err or res)
check("2 on_alive fires for every streamed event of the turn's call",
      err is None and len(ALIVE) == len(_OK), (len(ALIVE), len(_OK)))

# 3: the primary errors, the route's fallback answers
ALIVE.clear()
SENT.clear()
REPLIES[:] = [{"status": 500, "text": '{"type":"error","error":{"type":"api_error","message":"down"}}'},
              {"sse": _OK}]
try:
    res = _turn_call()
    err = None
except TypeError as e:
    res, err = {}, e
check("3a a primary that errors fails over to the route's fallback",
      err is None and res.get("content") == "fine" and bool(res.get("failed_over"))
      and len(SENT) == 2, err or {k: res.get(k) for k in ("content", "failed_over", "error")})
check("3b ...and the fallback's stream marks the turn alive",
      err is None and len(ALIVE) == len(_OK), (len(ALIVE), len(_OK)))

# 4: the conversation's cache key. Host turns passed session_id=None on chat
# entries and nothing on the tool-loop calls, so no OpenAI-wire provider got
# prompt_cache_key (or xAI x-grok-conv-id) on a Host turn. The key now rides
# _chat_call_kw, which every model call of the turn spreads.
check("4a the turn's kwargs carry the conversation's cache key",
      CHAT_CALL_KW.get("cache_key") == CONV_KEY, sorted(CHAT_CALL_KW))

try:
    from mastercode.runtime.session import conversation_cache_key as _ck  # noqa: E402
except ImportError:
    def _ck(*a):
        return "missing"
_a = _ck(3, {"kind": "thread", "ref": "t-1"})
check("4b one key per conversation: stable, distinct per thread and per Host, "
      "no internal id in it, none without a source context",
      _a == _ck(3, {"kind": "thread", "ref": "t-1"})
      and _a != _ck(3, {"kind": "thread", "ref": "t-2"})
      and _a != _ck(4, {"kind": "thread", "ref": "t-1"})
      and _a != _ck(3, {"kind": "project", "ref": "t-1"})
      and "t-1" not in _a and _a.startswith("conv-")
      and _ck(3, None) is None and _ck(3, {}) is None,
      (_a, _ck(3, None)))

# ...and it reaches an OpenAI-wire provider's request, on the turn's real
# kwargs through the real wrapper (only the credential and HTTP are stubbed).
_HEADERS: list = []


class _HSession(_Session):
    def post(self, url, headers=None, json=None):
        _HEADERS.append(dict(headers or {}))
        return super().post(url, headers=headers, json=json)


async def _cred(*a, **kw):
    return types.SimpleNamespace(secret="sk-test", source="api_key", cred_type="api_key")

VP.resolve_for_inference = _cred
PG.aiohttp = types.SimpleNamespace(ClientSession=_HSession, ClientTimeout=lambda **kw: None,
                                   ClientError=OSError)
_OAI = {"json": {"choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 2}}}
_oai_kw = dict(CHAT_CALL_KW, provider="openai", provider_model="gpt-5", fallback_provider=None,
               fallback_model=None, base_url=None)
for k in ("stream_cb",):
    EXPLICIT_KW.pop(k, None)
SENT.clear(); _HEADERS.clear()
REPLIES[:] = [_OAI]
try:
    res = asyncio.run(_call_entity_model(list(MSGS), **dict(EXPLICIT_KW, session_id=None), **_oai_kw))
    err = None
except TypeError as e:
    res, err = {}, e
check("4c ...and reaches the provider as prompt_cache_key on a chat turn whose "
      "session_id is None",
      err is None and len(SENT) == 1 and SENT[0].get("prompt_cache_key") == CONV_KEY,
      err or [(s.get("prompt_cache_key"), res.get("error")) for s in SENT])

print(f"[chat-call-kw] {'PASS (0 failing)' if not FAILS else f'FAIL ({len(FAILS)} failing): {FAILS}'}")
sys.exit(1 if FAILS else 0)
