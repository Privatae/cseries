"""A call through runtime/tools._call_entity_model that names no max_tokens is
sent the routed model's own output limit.

There is no fixed 16,384-token output ceiling on the
Host turn: provider_gateway.call_model resolves max_tokens=None, per dial, to
the dialled model's own limit (context_budget.output_limit_for, then
call_output_limit). The runtime wrapper every in-pod caller goes through still
defaulted max_tokens to 16384, so a caller that left it out (memory_engine's
extraction, handed the wrapper as model_fn by session.extract_from_transcript)
was held to the old ceiling. A caller that wants a small reply for a fixed
purpose passes its own number, and that number is sent as given.

Only the HTTP session, the credential lookup and the call logs are stand-ins;
the wrapper, the gateway and the limit resolver are the real ones, reading a
catalog row seeded here. No pod, no DB, no network.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from mastercode import context_budget as CB  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode.runtime.tools import _call_entity_model  # noqa: E402  (the REAL wrapper)
from server.routes import inference as INF  # noqa: E402
import server.vault.providers as VP  # noqa: E402

PRIMARY, PRIMARY_OUT = "claude-opus-5-5", 128_000
FALLBACK, FALLBACK_OUT = "claude-sonnet-4-6", 64_000
WINDOW = 1_000_000
MSGS = [{"role": "system", "content": "Return ONLY a valid JSON array."},
        {"role": "user", "content": "[USER]: hi\n"}]

_SSE = [{"type": "message_start", "message": {"model": "m", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "[]"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}]
_JSON = {"model": "m", "content": [{"type": "text", "text": "[]"}],
         "stop_reason": "end_turn", "usage": {}}
_OK = {"status": 200, "sse": _SSE, "json": _JSON}
_DOWN = {"status": 500, "text": '{"type":"error","error":{"type":"api_error","message":"down"}}'}


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
        return self._spec.get("text", "")


@pytest.fixture
def wire(monkeypatch):
    sent: list = []
    replies: list = []

    class _Session:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent.append(dict(json or {}))
            return _Resp(replies.pop(0))

    async def _resolve(*a, **kw):
        return types.SimpleNamespace(secret="sk-ant-test", cred_type="api_key")

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(PG, "aiohttp", types.SimpleNamespace(
        ClientSession=_Session, ClientTimeout=lambda **kw: None, ClientError=OSError))
    monkeypatch.setattr(VP, "resolve", _resolve)
    monkeypatch.setattr(PG, "_log_inference", _noop)
    monkeypatch.setattr(PG, "_log_call_failure", _noop)
    monkeypatch.setattr(INF, "warm_route_catalog", _noop)
    monkeypatch.setattr(CB, "_LEARNED_OUTPUTS", {})
    monkeypatch.setitem(INF._CATALOG_CACHE, "data", {"openrouter_models": [], "total_openrouter": 0})
    monkeypatch.setitem(INF._DIRECT_MODELS_CACHE, "anthropic", {"data": [
        {"id": PRIMARY, "context_length": WINDOW, "max_output_tokens": PRIMARY_OUT},
        {"id": FALLBACK, "context_length": WINDOW, "max_output_tokens": FALLBACK_OUT}]})
    return sent, replies


def _call(**kw):
    return asyncio.new_event_loop().run_until_complete(
        _call_entity_model(list(MSGS), temperature=0.2, provider="anthropic",
                           provider_model=PRIMARY, **kw))


def test_a_call_that_names_no_max_tokens_is_sent_the_models_own_limit(wire):
    sent, replies = wire
    replies[:] = [_OK]
    res = _call()
    assert res.get("content") == "[]", res
    assert len(sent) == 1
    assert sent[0]["max_tokens"] == PRIMARY_OUT, sent[0]["max_tokens"]
    assert res.get("output_limit") == PRIMARY_OUT, res.get("output_limit")


def test_a_caller_that_names_its_own_number_is_sent_that_number(wire):
    sent, replies = wire
    replies[:] = [_OK]
    _call(max_tokens=700)
    assert [s["max_tokens"] for s in sent] == [700]


def test_the_failover_model_is_sent_its_own_limit_not_the_primarys(wire):
    # Why the wrapper passes None through instead of resolving the number
    # itself: the gateway resolves per dial, so the route's fallback model is
    # held to its own limit. A number fixed before the call would be the
    # primary's on both dials.
    sent, replies = wire
    replies[:] = [_DOWN, _OK]
    res = _call(fallback_provider="anthropic", fallback_model=FALLBACK)
    assert res.get("failed_over"), res
    assert [s["max_tokens"] for s in sent] == [PRIMARY_OUT, FALLBACK_OUT]
