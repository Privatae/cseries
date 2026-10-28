#!/usr/bin/env python3
"""Safety-filter declines on the Anthropic wire, and the Orchestrator fallback.

Container-free: APP_ROOT=<repo> python3 scripts/test_anthropic_refusal.py.
No pod, no DB, no network: the HTTP session, the credential and the ledger
writes are stand-ins.

A Host on claude-opus-5-5 kept ending turns on "empty response from
anthropic". The gateway threw away stop_reason and had no reading of
stop_reason "refusal": a safety-filter decline, which Anthropic returns as an
HTTP 200 with stop_details {category, explanation}, before any output or
part-way through it (platform.claude.com/docs/en/build-with-claude/
refusals-and-fallback). The Host has to handle the safety classifiers, and
the fallback is the one set in the Orchestrator. So a decline is answered by the route's own Orchestrator fallback, through the
one failover call_model already has, and the Host is told. Every value here
is invented.

  A. the request is unchanged: no `fallbacks` field, no extra beta
  B. a decline before any output: a sentence a person can read, never the reply
  C. a decline part-way: the partial is not returned as the reply
  D. a decline with an Orchestrator fallback set: the same request runs on it,
     and the served model is recorded
  E. a decline with no fallback set: nothing re-runs, and the sentence says so
  F. an end_turn with nothing in it: said as what happened, not re-sent
  G. the rows say why the model stopped; a decline is priced like any call,
     never a $0 the skill's docs do not give
  H. diagnose shows each decline where it reads provider failures, and puts
     one in the Host's block from the first
  I. the Host can offer a fallback model through model_routing
  J. the person reads the sentence; a refused partial is not kept as the reply
  K. a reasoning_extraction decline is never sent to a fallback
  L. a turn's reply: a partial the filter stopped is withdrawn from what is
     stored, and the fallback's answer stands alone

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import copy
import inspect
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
        print(f"[anthropic-refusal] FAIL {name} {str(extra)[:600]}")


def run(coro):
    return asyncio.run(coro)


def _try(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 — a missing function reads as a failed check
        return e


from mastercode import provider_gateway as PG  # noqa: E402
from mastercode.runtime import diagnostics as DG  # noqa: E402
from mastercode.runtime import route_change as RC  # noqa: E402
import server.vault.providers as VP  # noqa: E402

# ── stand-ins ─────────────────────────────────────────────────────────────
SENT: list = []        # (headers, body) per request
REPLIES: list = []
ROWS: list = []        # every _log_inference call's kwargs
STREAMED: list = []


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for ch in self._chunks:
            yield ch


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
        return copy.deepcopy(self._spec.get("json", {}))

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
        SENT.append((dict(headers or {}), copy.deepcopy(json)))
        return _Resp(REPLIES.pop(0))


PG.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=lambda **kw: None,
                                   ClientError=OSError)
CRED = {"type": "api_key"}


async def _resolve(*a, **kw):
    return types.SimpleNamespace(secret="sk-ant-test", cred_type=CRED["type"])


VP.resolve = _resolve


async def _capture_row(**kw):
    ROWS.append(kw)


PG._log_inference = _capture_row


async def _cb(t):
    STREAMED.append(t)


def _msg(content, stop_reason="end_turn", model="claude-opus-5-5", usage=None, stop_details=None):
    return {"json": {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
                     "content": content, "stop_reason": stop_reason, "stop_details": stop_details,
                     "usage": usage or {"input_tokens": 40, "output_tokens": 9}}}


def _ok(text="done", model="claude-opus-5-5"):
    return _msg([{"type": "text", "text": text}], model=model)


def _refusal(category="cyber", content=None, usage=None, model="claude-opus-5-5"):
    return _msg(content or [], stop_reason="refusal", model=model,
                usage=usage or {"input_tokens": 40, "output_tokens": 0},
                stop_details={"type": "refusal", "category": category,
                              "explanation": ("This request was declined." if category else None)})


def _sse(*events):
    return {"sse": list(events)}


def _start(model="claude-opus-5-5", inp=40):
    return {"type": "message_start", "message": {"model": model, "usage": {"input_tokens": inp,
                                                                           "output_tokens": 1}}}


def _text_block(i, text):
    return [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": i}]


def _stop_delta(stop_reason, category=None, out=0):
    sd = ({"type": "refusal", "category": category, "explanation": None}
          if stop_reason == "refusal" else None)
    return {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_details": sd},
            "usage": {"output_tokens": out}}


def _anth(model="claude-opus-5-5", stream=False, **kw):
    if stream:
        kw["stream_cb"] = _cb
    return _try(lambda: run(PG._call_anthropic([{"role": "user", "content": "go"}], model,
                                               max_tokens=1000, **kw)))


def _cm(**kw):
    kw.setdefault("provider", "anthropic")
    kw.setdefault("provider_model", "claude-opus-5-5")
    return _try(lambda: run(PG.call_model([{"role": "user", "content": "check the bastion"}],
                                          max_tokens=1000, **kw)))


def _reset(*replies):
    SENT.clear()
    ROWS.clear()
    STREAMED.clear()
    REPLIES[:] = list(replies)


def _failure_rows():
    return [r for r in ROWS if r.get("purpose") == "inference_error"]


def _md(r):
    return (r or {}).get("metadata") or {}


NO_FB = "No fallback model is set for this in the Orchestrator."

# ── A. the request ────────────────────────────────────────────────────────
print("A. the request is unchanged: the fallback is the Orchestrator's, not Anthropic's")
_reset(_ok())
_anth("claude-opus-5-5")
check("A1 no `fallbacks` field: Anthropic does not pick the model",
      len(SENT) == 1 and "fallbacks" not in SENT[0][1], SENT[:1])
check("A2 no anthropic-beta header on an API key", "anthropic-beta" not in SENT[0][0], SENT[0][0])
CRED["type"] = "oauth_token"
_reset(_ok())
_anth("claude-opus-5-5")
check("A3 an OAuth credential sends its own beta only",
      SENT[0][0].get("anthropic-beta") == "oauth-2025-04-20", SENT[0][0])
CRED["type"] = "api_key"
check("A4 the provider registry carries no fallback setting of its own",
      "server_side_fallback" not in VP.PROVIDERS["anthropic"], VP.PROVIDERS["anthropic"])

# ── B. a decline before any output ────────────────────────────────────────
print("B. a decline before any output")
_reset(_refusal("cyber"))
res = _anth("claude-opus-5-5")
check("B1 the error is a sentence a person can read, with the category",
      isinstance(res, dict)
      and res.get("error") == "Anthropic's safety filter declined this request (category: cyber).",
      res)
check("B2 ...machine-readable: refusal, its category, the stop reason, no content",
      res.get("refusal") is True and res.get("refusal_category") == "cyber"
      and res.get("stop_reason") == "refusal" and res.get("content") == "", res)
check("B3 ...never \"empty response\", never \"the platform\"",
      "empty response" not in str(res.get("error")) and "platform" not in str(res.get("error")).lower())
check("B4 ...the explanation rides for display", res.get("refusal_explanation") == "This request was declined.")
check("B5 one request: the gateway does not re-send it to the same model", len(SENT) == 1)
_reset(_refusal(None))
res = _anth("claude-opus-5-5")
check("B6 a null category (a real value) is left out of the sentence and kept as None",
      res.get("error") == "Anthropic's safety filter declined this request."
      and "refusal_category" in res and res["refusal_category"] is None, res)
_reset(_sse(_start(), _stop_delta("refusal", "bio")))
res = _anth("claude-opus-5-5", stream=True)
check("B7 streamed: the message_delta's stop_details is read",
      res.get("refusal") is True and res.get("refusal_category") == "bio"
      and "(category: bio)" in str(res.get("error")) and not res.get("refusal_partial"), res)

# ── C. a decline part-way ─────────────────────────────────────────────────
print("C. a decline part-way: the partial is not the reply")
_reset(_sse(_start(), *_text_block(0, "Here is how the first half"), _stop_delta("refusal", "cyber", 12)))
res = _anth("claude-opus-5-5", stream=True)
check("C1 the partial text is not returned as content",
      isinstance(res, dict) and res.get("content") == "" and res.get("refusal_partial") is True, res)
check("C2 ...the sentence says it stopped part-way and was discarded",
      res.get("error") == ("Anthropic's safety filter stopped this reply part-way (category: cyber); "
                           "the part already shown was discarded."), res.get("error"))
_reset(_sse(_start(),
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_x", "name": "read_file", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": "{\"path\": \"/srv/a"}},
            _stop_delta("refusal", None, 8)))
res = _anth("claude-opus-5-5", stream=True, tools=[{"type": "function", "function": {
    "name": "read_file", "parameters": {"type": "object"}}}])
check("C3 a tool call it was writing is not returned to be run",
      "tool_calls" not in res and res.get("refusal_partial") is True, res)
_reset(_refusal("cyber", content=[{"type": "text", "text": "Partial"}],
                usage={"input_tokens": 40, "output_tokens": 3}))
res = _anth("claude-opus-5-5")
check("C4 buffered with text: the same", res.get("content") == "" and res.get("refusal_partial"), res)

# ── D. a decline with an Orchestrator fallback ───────────────────────────
print("D. a decline with an Orchestrator fallback set: the same request runs on it")
_reset(_refusal("cyber"), _ok("Here is the bastion check.", model="claude-opus-4-8"))
res = _cm(fallback_provider="anthropic", fallback_model="claude-opus-4-8")
check("D1 two requests: the primary, then the fallback model",
      len(SENT) == 2 and SENT[0][1]["model"] == "claude-opus-5-5"
      and SENT[1][1]["model"] == "claude-opus-4-8", [s[1].get("model") for s in SENT])
check("D2 ...the same request (the same messages) runs on the fallback",
      len(SENT) == 2 and SENT[0][1]["messages"] == SENT[1][1]["messages"])
check("D3 the reply is delivered from the fallback",
      isinstance(res, dict) and res.get("content") == "Here is the bastion check."
      and not res.get("error"), res)
_fo = (res or {}).get("failed_over") or {}
check("D4 ...the result names the model that served it, and why",
      res.get("model") == "claude-opus-4-8" and _fo.get("to_model") == "claude-opus-4-8"
      and _fo.get("from_model") == "claude-opus-5-5" and _fo.get("answered") is True
      and _fo.get("primary_refusal") == {"category": "cyber"}
      and "safety filter declined" in str(_fo.get("primary_error")), _fo)
fr = _failure_rows()
check("D5 the primary's failure row: the decline, its category, and who answered",
      len(fr) == 1 and _md(fr[0]).get("refusal") is True
      and _md(fr[0]).get("refusal_category") == "cyber"
      and _md(fr[0]).get("failed_over_to") == "anthropic/claude-opus-4-8"
      and _md(fr[0]).get("fallback_answered") is True, fr)
_spend = [r for r in ROWS if r.get("purpose") != "inference_error"]
check("D6 each model's spend is its own row, at its own model",
      sorted(r["model"] for r in _spend) == ["claude-opus-4-8", "claude-opus-5-5"], _spend)
_reset(_refusal("cyber"), _refusal("cyber", model="claude-opus-4-8"))
res = _cm(fallback_provider="anthropic", fallback_model="claude-opus-4-8")
fr = _failure_rows()
_prim = [r for r in fr if r.get("model") == "claude-opus-5-5"]
check("D7 the fallback declined too: an error, and the primary's row says it did not answer",
      res.get("error") and not res.get("content")
      and ((res.get("failed_over") or {}).get("answered") is False)
      and len(fr) == 2 and _prim and _md(_prim[0]).get("fallback_answered") is False, (res, fr))
_reset(_sse(_start(), *_text_block(0, "Step one: "), _stop_delta("refusal", "cyber", 6)),
       _sse(_start("claude-opus-4-8"), *_text_block(0, "Full answer."), _stop_delta("end_turn", out=4)))
res = _cm(fallback_provider="anthropic", fallback_model="claude-opus-4-8", stream_cb=_cb)
check("D8 a decline part-way also runs on the fallback, and its answer is the reply",
      res.get("content") == "Full answer." and len(SENT) == 2
      and (res.get("failed_over") or {}).get("primary_refusal") == {"category": "cyber", "partial": True},
      res)

# ── E. no fallback set ───────────────────────────────────────────────────
print("E. a decline with no fallback set: nothing re-runs")
_reset(_refusal("cyber"))
res = _cm()
check("E1 one request: nothing is re-run", len(SENT) == 1, len(SENT))
check("E2 the person reads the plain sentence",
      res.get("error") == f"Anthropic's safety filter declined this request (category: cyber). {NO_FB}",
      res.get("error"))
check("E3 ...and the turn's failure row records it, with no_fallback",
      len(_failure_rows()) == 1 and _md(_failure_rows()[0]).get("no_fallback") is True
      and _md(_failure_rows()[0]).get("refusal_category") == "cyber"
      and NO_FB in str(_md(_failure_rows()[0]).get("error")), _failure_rows())
_reset(_refusal("cyber"))
res = _cm(fallback_provider="anthropic")
check("E4 a fallback that is the same model is not tried, and the sentence says why",
      len(SENT) == 1 and "was not tried: anthropic/claude-opus-5-5 is the same model"
      in str(res.get("error")) and not res.get("no_fallback"), res)
_reset({"status": 500, "text": "{\"type\":\"error\",\"error\":{\"type\":\"api_error\",\"message\":\"boom\"}}"})
res = _cm()
check("E5 any other failure keeps its own words (the fallback sentence is for declines)",
      str(res.get("error", "")).startswith("anthropic 500") and NO_FB not in str(res.get("error")), res)

# ── F. end_turn with nothing in it ───────────────────────────────────────
print("F. an end_turn with nothing in it")
_reset(_msg([], stop_reason="end_turn", usage={"input_tokens": 40, "output_tokens": 2}))
res = _anth("claude-opus-5-5")
check("F1 said as what happened, with the stop reason",
      res.get("error") == "claude-opus-5-5 ended its turn without writing anything "
                          "(stop_reason: end_turn)" and res.get("stop_reason") == "end_turn"
      and not res.get("refusal"), res)
check("F2 not re-sent (the stop-reasons page: an unchanged re-send does not help)", len(SENT) == 1)
_reset(_msg([], stop_reason="end_turn"))
_cm()
_m = _md((_failure_rows() or [{}])[0])
check("F3 its failure row carries its stop_reason",
      _m.get("stop_reason") == "end_turn" and "ended its turn" in str(_m.get("error")), _m)
_reset(_msg([], stop_reason="pause_turn"))
res = _anth("claude-opus-5-5")
check("F4 pause_turn keeps its stop_reason (web_research reads it)",
      res.get("stop_reason") == "pause_turn" and "paused its turn" in str(res.get("error")), res)

# ── G. the rows ───────────────────────────────────────────────────────────
print("G. the spend rows say why the model stopped, and what a decline cost")
_reset(_ok())
_anth("claude-opus-5-5")
check("G1 an ordinary reply's spend row is unchanged (no stop_reason added)",
      ROWS and ROWS[0].get("metadata") is None, ROWS)
_reset(_msg([], stop_reason="end_turn"))
_anth("claude-opus-5-5")
check("G2 an empty reply's spend row says why it stopped",
      ROWS and _md(ROWS[0]).get("stop_reason") == "end_turn", ROWS)
# The skill's docs say only that a decline before any output is billed by
# category (the list is on Anthropic's page) and that one after output began
# bills at normal rates: no row may claim $0 for either.
for _c in ("cyber", "general_harms", None, "bio"):
    _reset(_refusal(_c))
    _anth("claude-opus-5-5")
    _r = ROWS[0] if ROWS else {}
    check(f"G3 a {_c} decline before any output: tokens kept, priced like any call, no $0",
          _r.get("prompt_tokens") == 40 and "reported_cost_usd" not in _r
          and _md(_r).get("refusal_billing") == "by_category"
          and _md(_r).get("refusal_category") == _c, _r)
_reset(_refusal(None, usage={"input_tokens": 40, "output_tokens": 57}))
_anth("claude-opus-5-5")
_r = ROWS[0] if ROWS else {}
check("G5 a decline after output (thinking included) is billed at normal rates",
      "reported_cost_usd" not in _r and _md(_r).get("refusal_billing") == "normal", _r)

# ── H. diagnose ──────────────────────────────────────────────────────────
print("H. diagnose: declines where it reads provider failures")
_now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()


def _row(meta, model="claude-opus-5-5"):
    return {"created_at": _now, "provider": "anthropic", "model_id": model,
            "source_module": "session", "metadata": json.dumps(meta)}


_no_fb = {"error": f"Anthropic's safety filter declined this request (category: cyber). {NO_FB}",
          "error_class": "error", "stop_reason": "refusal", "refusal": True,
          "refusal_category": "cyber", "no_fallback": True}
_answered = {"error": "Anthropic's safety filter declined this request (category: cyber).",
             "error_class": "error", "stop_reason": "refusal", "refusal": True,
             "refusal_category": "cyber", "failed_over_to": "anthropic/claude-opus-4-8",
             "fallback_answered": True}
g = DG.failure_groups([_row(_no_fb), _row(_answered)], [], [])
pg = {("no" if x.get("no_fallback") else "fb"): x for x in g["provider"]}
check("H1 a decline lands in failures.provider with its category",
      pg.get("no", {}).get("refusal") is True and pg["no"].get("refusal_category") == "cyber"
      and pg.get("fb", {}).get("refusal_category") == "cyber", g["provider"])
check("H2 ...with what the fallback did: none set, or which model answered",
      pg.get("no", {}).get("no_fallback") is True
      and pg.get("fb", {}).get("fallback") == "anthropic/claude-opus-4-8"
      and pg["fb"].get("fallback_answered") is True, g["provider"])
_lines = DG.render_failures_text(g)
check("H3 the text form says it is a safety-filter decline and what answered",
      "safety-filter decline (category cyber)" in _lines
      and "fallback anthropic/claude-opus-4-8 answered" in _lines
      and "no fallback is set in the Orchestrator" in _lines, _lines)
_other = DG.failure_groups([_row({"error": "anthropic 500: {\"error\":{\"message\":\"boom\"}}"})], [], [])
check("H4 any other provider failure is unchanged (no decline fields)",
      not _other["provider"][0].get("refusal") and "safety-filter" not in DG.render_failures_text(_other))


async def _probe(f):
    async def _reader():
        return f
    return await DG._probe_failures(None, 1, {"failures": _reader})


_f = {"since_hours": 24, "as_of": _now, **DG.failure_groups([_row(_no_fb)], [], [])}
faults = run(_probe(_f))
check("H5 ONE decline reaches the Host's block (others need three)",
      len(faults) == 1 and "safety filter declined 1 request" in faults[0]["title"], faults)
check("H6 ...with no fallback: the fact, and the door (model_routing files a card the person approves)",
      faults and "model_routing" in faults[0]["detail"] and "fallback_model" in faults[0]["detail"]
      and "card the person approves" in faults[0]["detail"]
      and "offer" not in faults[0]["detail"].lower()
      and faults[0]["evidence"].get("refusal_category") == "cyber",
      faults)
_f = {"since_hours": 24, "as_of": _now, **DG.failure_groups([_row(_answered)], [], [])}
faults = run(_probe(_f))
check("H7 ...the fallback answered: which model answered, and why, as a fact",
      len(faults) == 1 and "anthropic/claude-opus-4-8 answered instead" in faults[0]["title"]
      and "answered by anthropic/claude-opus-4-8, not claude-opus-5-5" in faults[0]["detail"]
      and "tell the person" not in faults[0]["detail"].lower(), faults)
# Every kind of decline the block carries states what happened and the door,
# never a line for the Host to say (the Host decides what to tell the person).
# Invented groups.
_kinds = {"answered": {"fallback": "anthropic/claude-opus-4-8", "fallback_answered": True},
          "fallback failed too": {"fallback": "anthropic/claude-opus-4-8", "fallback_answered": False},
          "not retried": {"not_retried": True},
          "no fallback": {"no_fallback": True}}
for _k, _extra in _kinds.items():
    _d = DG._decline_fault({"provider": "anthropic", "model": "claude-opus-5-5", "count": 1,
                            "message": "declined", "refusal": True, "refusal_category": "cyber",
                            "last_seen": "2026-01-02T03:04:05+00:00", **_extra}, 24)["detail"]
    check(f"H7b the {_k} decline states the fact, never a script for the Host",
          not any(w in _d.lower() for w in ("tell the person", "say ", "offer", "propose it")), _d)
_f = {"since_hours": 24, "as_of": _now, **_other}
check("H8 a single other provider failure still stays out of the block", run(_probe(_f)) == [])

# ── I. the Host can offer a fallback model ───────────────────────────────
print("I. model_routing can set a fallback model")
from mastercode import public_tool_registry as _PTR  # noqa: E402
_props = (((getattr(_PTR, "PUBLIC_TOOL_SCHEMAS", {}).get("model_routing") or {}).get("function") or {})
          .get("parameters") or {}).get("properties") or {}
check("I1 the tool takes fallback_model beside fallback_provider",
      "fallback_model" in _props and "fallback_provider" in _props, sorted(_props))
_cur = {"provider": "anthropic", "model": "claude-opus-5-5", "fallback_provider": None}
_want = {"provider": "anthropic", "model": "claude-opus-5-5", "fallback_provider": "anthropic",
         "fallback_model": "claude-opus-4-8"}
check("I2 adding a fallback to a working route is the person's decision (a card), never applied",
      RC.classify(_cur, _want, previous_approved=None) == RC.PROPOSE)
check("I3 ...and a fallback model alone is a change, not a no-op",
      RC.classify({**_cur, "fallback_provider": "anthropic", "fallback_model": "claude-opus-4-7"},
                  _want, previous_approved=None) == RC.PROPOSE)
_pp = RC.proposal_prompt("chat", _cur, _want, None)
check("I4 the card names the fallback model, and says it keeps the route rather than moving it",
      "set its fallback to anthropic / claude-opus-4-8" in _pp and "Move" not in _pp, _pp)
from mastercode.runtime import tools_shared as TS  # noqa: E402
from mastercode.runtime import tools_settings as TSET  # noqa: E402
check("I5 the write door takes fallback_model and puts it on the route",
      "fallback_model" in inspect.signature(TS._host_route_write).parameters
      and "body.fallback_model = fallback_model" in inspect.getsource(TS._host_route_write)
      and '{"fallback_model": fallback_model}' in inspect.getsource(TS._host_route_write))
check("I6 model_routing op='set' passes it through",
      'fallback_model=(str(a["fallback_model"])' in inspect.getsource(TSET._exec_model_routing))

# ── J. what the person reads, and what is kept ───────────────────────────
print("J. the person reads the sentence; a refused partial is not kept")
BS_SRC = open(os.path.join(APP_ROOT, "server/brain_service.py"), encoding="utf-8").read()
check("J1 the turn's error goes to the chat as its terminal 3: frame, verbatim",
      'yield "3:" + json.dumps(str(response["error"])) + "\\n"' in BS_SRC)
from mastercode.runtime.turn_trace import sent_reply  # noqa: E402
_pieces = [{"text": "Earlier narration."}, {"text": ""}]
check("J3 ...and a blanked piece is left out of what is kept",
      sent_reply({"pieces": _pieces}) == "Earlier narration.")
_copy = {p: open(os.path.join(APP_ROOT, p), encoding="utf-8").read() for p in (
    "default-skills/routing.md", "system-apps/orchestrator/app.yaml",
    "dashboard/base/js/model-picker.js")}
check("J4 the Orchestrator copy and the routing skill say the fallback also answers a decline",
      all("safety filter declines" in t for t in _copy.values()),
      [p for p, t in _copy.items() if "safety filter declines" not in t])

# ── K. reasoning_extraction ───────────────────────────────────────────────
print("K. a reasoning_extraction decline is never sent to a fallback")
# claude-api skill, shared/model-migration.md, Opus 5.5 safeguards: such
# declines "are not retried on a fallback model"; the fix is the request.
_reset(_refusal("reasoning_extraction"), _ok("should never be asked", model="claude-opus-4-8"))
res = _cm(fallback_provider="anthropic", fallback_model="claude-opus-4-8")
check("K1 one request even with a fallback set", len(SENT) == 1, [s[1].get("model") for s in SENT])
check("K2 the sentence says what to ask instead, and offers no fallback",
      "short explanation of the answer" in str(res.get("error"))
      and "another model is not tried" in str(res.get("error"))
      and NO_FB not in str(res.get("error")) and res.get("refusal_not_retried") is True
      and not res.get("no_fallback") and not res.get("failed_over"), res)
check("K3 ...never \"the platform\"", "platform" not in str(res.get("error")).lower())
fr = _failure_rows()
check("K4 its failure row says it was not retried",
      len(fr) == 1 and _md(fr[0]).get("refusal_not_retried") is True
      and not _md(fr[0]).get("no_fallback"), fr)
_g = DG.failure_groups([_row({"error": res.get("error"), "error_class": "error",
                              "refusal": True, "refusal_category": "reasoning_extraction",
                              "refusal_not_retried": True})], [], [])
_f = {"since_hours": 24, "as_of": _now, **_g}
faults = run(_probe(_f))
check("K5 diagnose states no fallback helps it, and names no fallback door",
      len(faults) == 1 and "No fallback helps with this one" in faults[0]["detail"]
      and "model_routing" not in faults[0]["detail"]
      and faults[0]["evidence"].get("not_retried") is True, faults)
_reset(_refusal("cyber"), _ok("Here.", model="claude-opus-4-8"))
res = _cm(fallback_provider="anthropic", fallback_model="claude-opus-4-8")
check("K6 any other category still runs on the fallback", len(SENT) == 2 and res.get("content") == "Here.")

# ── L. the turn's reply ─────────────────────────────────────────────────
print("L. a turn's reply: the declined partial is not kept")
from mastercode.runtime.turn_replies import TurnReplies  # noqa: E402


def _replies():
    ev: list = []

    async def _pcb(e):
        ev.append(e)
    tr = TurnReplies(progress_cb=_pcb, sess=types.SimpleNamespace(session_id="s-test", unseen_lane=False),
                     turn_token="t-refusal-test", result=lambda: {}, ct_reply_result=lambda: None,
                     row_project_id=lambda: None, turn_tags=lambda: {}, asked_in_box=lambda: False,
                     tools=lambda: [])
    return tr, ev


def _turn(tr, **kw):
    async def _go():
        cb = await tr._next_stream_cb()
        r = await PG.call_model([{"role": "user", "content": "check the bastion"}], max_tokens=1000,
                                provider="anthropic", provider_model="claude-opus-5-5",
                                stream_cb=cb, **kw)
        await tr._settle(r)
        return r
    return _try(lambda: run(_go()))


def _deltas(ev):
    return "".join(e.get("text") or "" for e in ev if e.get("event") == "text_delta")


_reset(_sse(_start(), *_text_block(0, "Step one: "), _stop_delta("refusal", "cyber", 6)),
       _sse(_start("claude-opus-4-8"), *_text_block(0, "Full answer."), _stop_delta("end_turn", out=4)))
_tr, _ev = _replies()
res = _turn(_tr, fallback_provider="anthropic", fallback_model="claude-opus-4-8")
check("L1 the fallback answered a part-way decline: the stored reply is its answer alone",
      isinstance(res, dict) and res.get("content") == "Full answer."
      and _tr._reply_all() == "Full answer.", (res, _tr._reply_all()))
check("L2 ...live, the chat stream only appends: the partial already shown stays, and the "
      "fallback's answer starts a new paragraph", _deltas(_ev) == "Step one: \n\nFull answer.",
      repr(_deltas(_ev)))
_reset(_sse(_start(), *_text_block(0, "Step one: "), _stop_delta("refusal", "cyber", 6)))
_tr, _ev = _replies()
res = _turn(_tr)
check("L3 no fallback: nothing of the declined partial is stored, and the error says it was discarded",
      _tr._reply_all() == "" and "discarded" in str(res.get("error")), (_tr._reply_all(), res))
_reset(_sse(_start(), *_text_block(0, "## "), _stop_delta("refusal", "cyber", 2)))
_tr, _ev = _replies()
res = _turn(_tr)
check("L4 a partial still held back when the decline came is never sent", _deltas(_ev) == "",
      repr(_deltas(_ev)))

print()
if FAILS:
    print(f"[anthropic-refusal] FAIL ({len(FAILS)} failing): {FAILS}")
    sys.exit(1)
print("[anthropic-refusal] PASS (0 failing)")
