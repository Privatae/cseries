#!/usr/bin/env python3
"""An empty Anthropic reply says what it was.

Container-free: APP_ROOT=<repo> python3 scripts/test_anthropic_empty_reply_record.py.
No pod, no DB, no network: the HTTP session, the credential and the ledger
writer are stand-ins.

A reply with no text and no tool call used to come back as "empty response
from anthropic" with only the stop reason beside it, and the failure row kept
the error string alone, so an error event in the middle of the stream and a
reply that only thought read the same. (A safety decline, stop_reason refusal,
is now its own kind: scripts/test_anthropic_refusal.py.) Now the empty reply's
result and its failure row carry:

  A. the stream's stop_details, the type of every content block it opened, and
     any error event it sent (_consume_anthropic_stream)
  B. _call_anthropic's empty result carries those and the provider's request id
  C. _log_call_failure puts them on the inference_error row's metadata
  D. a reply that wrote text is unchanged

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import copy
import json
import logging
import os
import sys
import types

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.disable(logging.CRITICAL)

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[anthropic-empty-reply] FAIL {name} {str(extra)[:600]}")


def run(coro):
    return asyncio.run(coro)


from mastercode import provider_gateway as PG  # noqa: E402

SENT: list = []
REPLIES: list = []


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for ch in self._chunks:
            yield ch


class _Resp:
    def __init__(self, spec):
        self.status = spec.get("status", 200)
        self.headers = spec.get("headers", {})
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
        SENT.append(copy.deepcopy(json))
        return _Resp(REPLIES.pop(0))


PG.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=lambda **kw: None,
                                   ClientError=OSError)
import server.vault.providers as VP  # noqa: E402


async def _resolve(*a, **kw):
    return types.SimpleNamespace(secret="sk-ant-test", cred_type="api_key")


VP.resolve = _resolve
LOGGED: list = []


async def _capture(**kw):
    LOGGED.append(kw)


PG._log_inference = _capture

START = {"type": "message_start",
         "message": {"model": "claude-test-model", "usage": {"input_tokens": 12}}}
THINK = [{"type": "content_block_start", "index": 0,
          "content_block": {"type": "thinking", "thinking": ""}},
         {"type": "content_block_delta", "index": 0,
          "delta": {"type": "thinking_delta", "thinking": ""}},
         {"type": "content_block_stop", "index": 0}]
REFUSAL_END = {"type": "message_delta",
               "delta": {"stop_reason": "refusal",
                         "stop_details": {"type": "refusal", "category": "cyber",
                                          "explanation": "an invented explanation"}},
               "usage": {"output_tokens": 7}}
EMPTY_END = {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 57}}
ERROR_EVENT = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
TEXT = [{"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 1}]
TEXT_END = {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3}}


async def _discard(_t):
    return None


# ── A. the stream consumer ───────────────────────────────────────────────────
print("A. _consume_anthropic_stream keeps what an empty reply carried")
relay = run(PG._consume_anthropic_stream(_Resp({"sse": [START, *THINK, REFUSAL_END]}), _discard))
check("A1 the stop reason is kept", relay.get("stop_reason") == "refusal", relay)
check("A2 the stop details are kept",
      (relay.get("stop_details") or {}).get("category") == "cyber", relay)
check("A3 every content block's type is listed", relay.get("block_types") == ["thinking"], relay)
check("A4 no error event means none recorded", relay.get("stream_error") is None, relay)
relay = run(PG._consume_anthropic_stream(_Resp({"sse": [START, *THINK, ERROR_EVENT]}), _discard))
check("A5 an error event in the stream is recorded",
      (relay.get("stream_error") or {}).get("type") == "overloaded_error", relay)
check("A6 ...and the stop reason stays unknown, never invented", relay.get("stop_reason") is None, relay)

# ── B. _call_anthropic's empty result ───────────────────────────────────────
print("B. _call_anthropic returns the empty reply's own account")


def _anth():
    return run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-test-model",
                                  max_tokens=1000, on_alive=lambda: None))


SENT.clear()
REPLIES[:] = [{"sse": [START, *THINK, EMPTY_END], "headers": {"request-id": "req_invented_1"}}]
res = _anth()
check("B1 it is marked an empty reply", res.get("empty_reply") is True and res.get("error"), res)
check("B2 it says what happened", "ended its turn without writing anything" in str(res.get("error")), res)
check("B3 the stop reason rides it", res.get("stop_reason") == "end_turn", res)
check("B4 the block types ride it", res.get("block_types") == ["thinking"], res)
check("B5 the provider's request id rides it", res.get("request_id") == "req_invented_1", res)
REPLIES[:] = [{"sse": [START, *THINK, ERROR_EVENT]}]
res = _anth()
check("B6 an error event rides it", (res.get("stream_error") or {}).get("type") == "overloaded_error", res)
check("B7 a response with no request id says none", res.get("request_id") is None, res)
REPLIES[:] = [{"sse": [START, *THINK, REFUSAL_END], "headers": {"request-id": "req_invented_1b"}}]
res = _anth()
check("B8 a safety decline is its own kind, never labelled an empty reply",
      res.get("refusal") is True and not res.get("empty_reply"), res)

# ── C. the failure row ───────────────────────────────────────────────────────
print("C. _log_call_failure writes them on the inference_error row")
EXTRAS = ("stop_details", "block_types", "stream_error", "request_id")
LOGGED.clear()
run(PG._log_call_failure({"error": "claude-test-model ended its turn without writing anything "
                                   "(stop_reason: end_turn)", "empty_reply": True,
                          "stop_reason": "end_turn", "stop_details": {"note": "invented"},
                          "block_types": ["thinking"], "stream_error": None,
                          "request_id": "req_invented_2"},
                         source="test", model="claude-test-model", provider="anthropic",
                         entity_id=1, call_start=0))
meta = (LOGGED[-1] if LOGGED else {}).get("metadata") or {}
check("C1 the row carries the stop reason", meta.get("stop_reason") == "end_turn", meta)
check("C2 ...the stop details", (meta.get("stop_details") or {}).get("note") == "invented", meta)
check("C3 ...the block types", meta.get("block_types") == ["thinking"], meta)
check("C4 ...and the request id", meta.get("request_id") == "req_invented_2", meta)
check("C5 an absent field is left off, not written as null", "stream_error" not in meta, meta)
LOGGED.clear()
run(PG._log_call_failure({"error": "some other failure"}, source="test", model="m",
                         provider="anthropic", entity_id=1, call_start=0))
meta = (LOGGED[-1] if LOGGED else {}).get("metadata") or {}
check("C7 any other failure's row is unchanged",
      set(meta) == {"error", "error_class"}, meta)
# Other failures carry stop_reason and, on the OpenAI wire, request_id: their
# rows record the stop reason (every failure does) and none of the empty
# reply's own account.
CUT_END = {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
           "usage": {"output_tokens": 1000}}
REPLIES[:] = [{"sse": [START, *THINK, CUT_END], "headers": {"request-id": "req_invented_3"}}]
cut = _anth()
check("C8 (setup) the output limit stopped it before any reply",
      cut.get("truncated") and cut.get("stop_reason") == "max_tokens", cut)
LOGGED.clear()
run(PG._log_call_failure(cut, source="test", model="claude-test-model", provider="anthropic",
                         entity_id=1, call_start=0))
meta = (LOGGED[-1] if LOGGED else {}).get("metadata") or {}
check("C8 an output-limit failure's row carries none of the empty reply's account",
      not any(k in meta for k in EXTRAS), meta)
LOGGED.clear()
run(PG._log_call_failure({"error": "the model reached its output limit (100 tokens) before "
                                   "writing any reply", "content": "", "stop_reason": "length",
                          "output_limit": 100, "truncated": True,
                          "request_id": "gen-invented-4"},
                         source="test", model="m", provider="openrouter", entity_id=1,
                         call_start=0))
meta = (LOGGED[-1] if LOGGED else {}).get("metadata") or {}
check("C9 an OpenAI-wire failure carrying a request id: none of the empty reply's account",
      not any(k in meta for k in EXTRAS), meta)
# The real empty result, not a hand-built one, reaches the row.
REPLIES[:] = [{"sse": [START, *THINK, EMPTY_END], "headers": {"request-id": "req_invented_5"}}]
empty = _anth()
LOGGED.clear()
run(PG._log_call_failure(empty, source="test", model="claude-test-model", provider="anthropic",
                         entity_id=1, call_start=0))
meta = (LOGGED[-1] if LOGGED else {}).get("metadata") or {}
check("C10 _call_anthropic's own empty result lands whole on the row",
      meta.get("stop_reason") == "end_turn" and meta.get("block_types") == ["thinking"]
      and meta.get("request_id") == "req_invented_5", meta)
check("C11 ...and the row is JSON as written", bool(json.dumps(meta)), meta)

# ── B'. the buffered (non-streamed) call ─────────────────────────────────────
print("B'. a buffered call's empty reply says the same")
REPLIES[:] = [{"json": {"type": "message", "model": "claude-test-model",
                        "content": [{"type": "thinking", "thinking": ""}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 12, "output_tokens": 7}},
               "headers": {"request-id": "req_invented_6"}}]
SENT.clear()
res = run(PG._call_anthropic([{"role": "user", "content": "go"}], "claude-test-model",
                             max_tokens=1000))
check("B'0 (setup) the call was not streamed", SENT and not SENT[-1].get("stream"), SENT[-1:])
check("B'1 it is marked an empty reply", res.get("empty_reply") is True, res)
check("B'2 the block types come from the buffered content", res.get("block_types") == ["thinking"], res)
check("B'3 the request id rides it", res.get("request_id") == "req_invented_6", res)
check("B'4 no stream, so no stream error", res.get("stream_error") is None, res)

# ── D. a reply with text ─────────────────────────────────────────────────────
print("D. a reply that wrote something is unchanged")
REPLIES[:] = [{"sse": [START, *THINK, *TEXT, TEXT_END]}]
res = _anth()
check("D1 the text is the reply", res.get("content") == "hello" and not res.get("error"), res)
check("D2 none of the empty-reply fields ride a real reply",
      not any(k in res for k in ("empty_reply", "block_types", "stream_error", "request_id")), res)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")
