#!/usr/bin/env python3
"""test_openrouter_wire_tool_names.py — the tool names OpenRouter is sent are
names its upstream accepts, and the ones that come back are the registry's.

THE BUG (measured on entity 9163). The gateway renamed the
registry's dotted tool names (ladder.run, review.compare, …) for every
OpenAI-wire provider EXCEPT openrouter, on the belief that dotted names "pass
there". OpenRouter passes them straight to the upstream model's API, and
openai/gpt-4o-mini answered 400 "Invalid 'tools[16].function.name': string does
not match pattern '^[a-zA-Z0-9_-]+$'" — on a Workbench turn, where those names
live, the whole turn died with no reply. A live end-to-end test saw it as "the
Host never called its build tool".

Drives the real _call_remote with a stand-in HTTP session, so the assertion is
about the request that would actually leave the pod:
  1. openrouter gets wire-safe names in the tools array AND in replayed history;
  2. a tool call that comes back under its wire name reaches the caller under
     its registry name (it is what gets dispatched and stored);
  3. names that were already safe are sent untouched.

Container-free: APP_ROOT=<repo> python3 scripts/<this>.py. No pod, no network.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from mastercode import provider_gateway as PG  # noqa: E402

SAFE = re.compile(r"^[a-zA-Z0-9_-]+$")          # the pattern OpenAI returned
SENT: list[dict] = []
REPLY: dict = {}


class _Resp:
    status = 200
    headers: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return REPLY

    async def text(self):
        return json.dumps(REPLY)


class _Session:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        SENT.append(json)
        return _Resp()


async def _key():
    return "sk-test"


PG.aiohttp = types.SimpleNamespace(ClientSession=_Session,
                                   ClientTimeout=lambda **kw: None)
PG._load_user_openrouter_key = _key


def tool(name):
    return {"type": "function",
            "function": {"name": name, "description": name,
                         "parameters": {"type": "object", "properties": {}}}}


TOOLS = [tool("build"), tool("review.compare"), tool("ladder.run"), tool("recall")]
HISTORY = [
    {"role": "user", "content": "compare these"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "ladder.run", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "ladder.run", "content": "ok"},
    {"role": "user", "content": "now build it"},
]


def call(provider="openrouter"):
    SENT.clear()
    return asyncio.run(PG._call_remote(
        [dict(m) for m in HISTORY], "openai/gpt-4o-mini-2024-07-18",
        tools=[dict(t) for t in TOOLS], provider=provider))


# ── 1. what leaves the pod ────────────────────────────────────────────────
print("== 1. openrouter is sent names its upstream accepts ==")
REPLY.clear()
REPLY.update({"choices": [{"message": {"role": "assistant", "content": "done"}}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
out = call()
payload = SENT[-1] if SENT else {}
names = [t["function"]["name"] for t in payload.get("tools") or []]
check("a request was sent", bool(payload), str(out)[:200])
check("every tool name matches ^[a-zA-Z0-9_-]+$ (a dotted name was a 400)",
      names and all(SAFE.match(n) for n in names), str(names))
check("the dotted names were renamed, not dropped", len(names) == len(TOOLS), str(names))
check("the one that failed live now goes out as ladder_run", "ladder_run" in names, str(names))
check("names that were already safe are untouched",
      "build" in names and "recall" in names, str(names))
hist = payload.get("messages") or []
replayed = [tc["function"]["name"] for m in hist for tc in (m.get("tool_calls") or [])]
results = [m.get("name") for m in hist if m.get("role") == "tool"]
check("replayed assistant tool_calls use the wire name too",
      replayed and all(SAFE.match(n) for n in replayed), str(replayed))
check("replayed tool results use the wire name too",
      results and all(SAFE.match(n or "") for n in results), str(results))
check("the caller's history was not mutated",
      HISTORY[1]["tool_calls"][0]["function"]["name"] == "ladder.run")

# ── 2. what comes back ────────────────────────────────────────────────────
print("== 2. a tool call under its wire name reaches the caller under its registry name ==")
# The wire form _openai_wire_safe_name gives review.compare: dots -> underscores.
wire = "review_compare"
check("review.compare went out as review_compare", wire in names, str(names))
REPLY.clear()
REPLY.update({"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
    {"id": "c2", "type": "function", "function": {"name": wire, "arguments": "{}"}}]}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
out = call()
blob = json.dumps(out)
check("the returned tool call is 'review.compare', the name dispatch knows",
      '"review.compare"' in blob and f'"{wire}"' not in blob, blob[:300])

# ── 3. a round with NO tools still replays dotted history ─────────────────
print("== 3. a tool-less round (text fallback) sends wire-safe names in its history ==")
# Measured against openrouter -> openai/gpt-4o-mini with tools absent:
# history name 'ladder.run' -> 400, 'ladder_run' -> 200.
REPLY.clear()
REPLY.update({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
SENT.clear()
asyncio.run(PG._call_remote([dict(m) for m in HISTORY], "openai/gpt-4o-mini-2024-07-18",
                            tools=None, provider="openrouter"))
p3 = SENT[-1] if SENT else {}
h3 = p3.get("messages") or []
names3 = ([tc["function"]["name"] for m in h3 for tc in (m.get("tool_calls") or [])]
          + [m.get("name") for m in h3 if m.get("role") == "tool"])
check("history names are wire-safe with no tools in the request",
      names3 and all(SAFE.match(n or "") for n in names3), str(names3))
check("...and no tools array appears from nowhere", not p3.get("tools"), str(p3.get("tools")))

# ── 4. the streamed reply is restored too ──────────────────────────────────
print("== 4. a STREAMED tool call comes back under its registry name ==")


class _Content:
    def __init__(self, data):
        self.data = data

    async def iter_any(self):
        yield self.data


class _StreamResp(_Resp):
    def __init__(self):
        self.content = _Content(
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c3",'
            b'"function":{"name":"ladder_run","arguments":"{}"}}]}}]}\n'
            b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n'
            b'data: [DONE]\n')


class _StreamSession(_Session):
    def post(self, url, headers=None, json=None):
        SENT.append(json)
        return _StreamResp()


async def _cb(_text):
    return None

PG.aiohttp = types.SimpleNamespace(ClientSession=_StreamSession,
                                   ClientTimeout=lambda **kw: None)
out4 = asyncio.run(PG._call_remote([dict(m) for m in HISTORY], "openai/gpt-4o-mini-2024-07-18",
                                   tools=[dict(t) for t in TOOLS], provider="openrouter",
                                   stream_cb=_cb))
blob4 = json.dumps(out4)
check("streamed 'ladder_run' reaches the caller as 'ladder.run'",
      '"ladder.run"' in blob4 and '"ladder_run"' not in blob4, blob4[:300])

# ── 5. a provider built-in keeps its own name ─────────────────────────────
print("== 5. a built-in declared through native_tools is echoed back untouched ==")
# Kimi's $web_search: web_research._search_kimi declares it via native_tools and
# replays the call under that name; the provider matches on it.
PG.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=lambda **kw: None)
REPLY.clear()
REPLY.update({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
KIMI_HIST = [
    {"role": "user", "content": "search"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "w1", "type": "builtin_function",
         "function": {"name": "$web_search", "arguments": "{}"}},
        {"id": "l1", "type": "function",
         "function": {"name": "ladder.run", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "w1", "name": "$web_search", "content": "{}"},
    {"role": "tool", "tool_call_id": "l1", "name": "ladder.run", "content": "ok"},
]
SENT.clear()
asyncio.run(PG._call_remote([dict(m) for m in KIMI_HIST], "openai/gpt-4o-mini-2024-07-18",
                            tools=None, provider="openrouter",
                            native_tools=[{"type": "builtin_function",
                                           "function": {"name": "$web_search"}}]))
h5 = (SENT[-1] if SENT else {}).get("messages") or []
n5 = ([tc["function"]["name"] for m in h5 for tc in (m.get("tool_calls") or [])]
      + [m.get("name") for m in h5 if m.get("role") == "tool"])
check("$web_search goes out as $web_search (was t__web_search)",
      n5.count("$web_search") == 2 and "t__web_search" not in n5, str(n5))
check("...while ladder.run in the same history is still renamed",
      "ladder_run" in n5 and "ladder.run" not in n5, str(n5))

# ── 6. malformed history is passed through, not raised on ──────────────────
print("== 6. odd message shapes do not crash the call ==")
ODD = [
    {"role": "assistant", "content": "", "tool_calls": {"not": "a list"}},
    {"role": "assistant", "content": "", "tool_calls": ["not-a-dict", None]},
    {"role": "tool", "tool_call_id": "x", "name": None, "content": "ok"},
    {"role": "user", "content": "go"},
]
SENT.clear()
try:
    out6 = asyncio.run(PG._call_remote(list(ODD), "openai/gpt-4o-mini-2024-07-18",
                                       tools=[dict(t) for t in TOOLS], provider="openrouter"))
    check("the call completes", bool(SENT), str(out6)[:200])
except Exception as exc:  # noqa: BLE001
    check("the call completes", False, f"{type(exc).__name__}: {exc}")
# The rename helpers themselves, on every odd shape including a bare string
# (which the full call cannot reach them with: _apply_cache_boundary, older
# code that runs first on openrouter, already assumes dict messages).
try:
    stubs = PG._history_tool_stubs(["a bare string"] + ODD)
    renamed = PG._openai_wire_rename(None, ["a bare string"] + ODD, {"ladder_run": "ladder.run"})
    check("the helpers pass odd shapes through without raising",
          stubs == [] and len(renamed[1]) == len(ODD) + 1, str(stubs))
except Exception as exc:  # noqa: BLE001
    check("the helpers pass odd shapes through without raising", False,
          f"{type(exc).__name__}: {exc}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("ok  openrouter gets wire-safe tool names and hands back registry names")
