#!/usr/bin/env python3
"""Reasoning effort on the Anthropic wire.

Container-free: APP_ROOT=<repo> python3 scripts/test_anthropic_effort.py.
No pod, no DB, no network: the HTTP session, the credential and the database
pool are stand-ins.

Until this change the Anthropic lane sent no effort at all:
provider_gateway._dial_wire passed reasoning_effort to _call_remote only, so
an Anthropic Host ran at each model's own default whatever was set, while the
turn logged MODEL_ACTUAL reasoning=low. There was no way to tell
whether Claude Haiku 5.5's output is the model or the effort.

  A. the catalog reads each model's levels off the Models API
     (GET /v1/models capabilities.effort)
  B. the level a model runs at when none is sent, from the effort docs
  C. one rule decides what goes on the wire (anthropic_wire_effort)
  D. _call_anthropic sends output_config.effort only for a chosen level the
     model accepts; a 400 about the effort is re-sent once without it
  E. call_model: the person's pick reaches the Anthropic wire and the "low"
     fallback never does; the OpenAI wire is unchanged; every caller that
     sends route_effort sends chosen_effort beside it
  F. what the turn says it ran at (describe_effort, the MODEL_ACTUAL line)
  G. the Orchestrator's writer and the effort options use the same rule
  H. a chat box's per-thread pick rides the turn and is checked on send
     (Circles /api/circles/messages, Workbench /api/brain/messages)
  I. the Claude Code harness offers xhigh, which its sidecar sends
  J. a failed read of the model list is read again once the failure has
     expired, so a person's level is sent again

The fixture rows are shaped from the Models API's documented schema
(platform.claude.com/docs/en/api/models/list), with each model's levels as the
effort docs state them (platform.claude.com/docs/en/build-with-claude/effort):
Claude Haiku 5.5 all five, Claude Sonnet 4.6 no xhigh, Claude Opus 4.5 low to
high, Claude Haiku 4.5 none.

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import ast
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
        print(f"[anthropic-effort] FAIL {name} {str(extra)[:600]}")


def run(coro):
    return asyncio.run(coro)


def _try(fn, *a, **kw):
    """fn(*a, **kw), or the exception it raised (a missing function on an
    older tree reads as a failed check, never a crash of the whole gate)."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return e


from server.routes import inference as INF  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode.runtime import model_standard as MS  # noqa: E402

ALL5 = ("low", "medium", "high", "xhigh", "max")


def _model_obj(mid, levels, supported=True, xhigh_null=False):
    eff = {"supported": supported}
    for lv in ALL5:
        eff[lv] = {"supported": lv in levels}
    if xhigh_null:
        eff["xhigh"] = None
    return {"type": "model", "id": mid, "display_name": mid, "max_input_tokens": 1000000,
            "max_tokens": 128000, "created_at": "2026-10-01T00:00:00Z",
            "capabilities": {"effort": eff, "image_input": {"supported": True},
                             "thinking": {"supported": True,
                                          "types": {"adaptive": {"supported": True},
                                                    "enabled": {"supported": False}}}}}


CATALOG = [
    _model_obj("claude-haiku-5-5", ALL5),
    _model_obj("claude-sonnet-4-6", ("low", "medium", "high", "max"), xhigh_null=True),
    _model_obj("claude-opus-4-5-20251101", ("low", "medium", "high"), xhigh_null=True),
    _model_obj("claude-haiku-4-5-20251001", (), supported=False),
]

# ── A. the catalog reads the levels ───────────────────────────────────────
print("A. the catalog reads each model's effort levels off the Models API")
rows = [INF._dm_row(m) for m in CATALOG]
by_id = {r["id"]: r for r in rows}
check("A1 claude-haiku-5-5: all five levels, low to high",
      by_id["claude-haiku-5-5"].get("efforts") == list(ALL5), by_id["claude-haiku-5-5"])
check("A2 claude-sonnet-4-6: xhigh null is not a level it takes",
      by_id["claude-sonnet-4-6"].get("efforts") == ["low", "medium", "high", "max"],
      by_id["claude-sonnet-4-6"])
check("A3 claude-haiku-4-5: effort unsupported reads as [] (takes none), not absent",
      by_id["claude-haiku-4-5-20251001"].get("efforts") == [], by_id["claude-haiku-4-5-20251001"])
plain = INF._dm_row({"id": "gpt-x", "context_length": 128000})
check("A4 a provider that publishes no capabilities leaves `efforts` absent (unknown)",
      "efforts" not in plain, plain)
INF._DIRECT_MODELS_CACHE["anthropic"] = {"data": rows, "exp": 1e18}
cme = getattr(INF, "cached_model_efforts", None)
check("A5 cached_model_efforts reads the row: exact id",
      callable(cme) and cme("anthropic", "claude-haiku-5-5") == ALL5,
      cme and cme("anthropic", "claude-haiku-5-5"))
check("A6 ...an alias finds its dated row (claude-opus-4-5 -> claude-opus-4-5-20251101)",
      callable(cme) and cme("anthropic", "claude-opus-4-5") == ("low", "medium", "high"))
check("A7 ...a model the list does not carry is unknown (None), never 'none'",
      callable(cme) and cme("anthropic", "claude-not-listed") is None
      and cme("anthropic", "claude-haiku-4-5") == ())

# ── B. the default a model runs at, from the docs ─────────────────────────
print("B. the level a model runs at when none is sent (effort docs)")
dflt = getattr(IC, "anthropic_default_effort", None)
check("B1 Claude Haiku 5.5 and Claude Opus 5.5 default to medium",
      callable(dflt) and dflt("claude-haiku-5-5") == "medium" and dflt("claude-opus-5-5") == "medium")
check("B2 Claude Sonnet 5.5 and Claude Fable 5.1 default to high",
      callable(dflt) and dflt("claude-sonnet-5-5") == "high" and dflt("claude-fable-5-1") == "high")
check("B3 a dated id finds its model; an id the docs do not name has no default",
      callable(dflt) and dflt("claude-opus-4-5-20251101") == "high"
      and dflt("claude-future-9") is None and dflt("claude-haiku-4-5") is None)
# The docs page's list of models that support effort (featureMetadata
# supportedModels, its one dated id undated here), and the two it names as
# defaulting to medium.
DOCS_SUPPORTED = {"claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5",
                  "claude-mythos-preview", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                  "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5-5",
                  "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5-5"}
table = getattr(IC, "_ANTHROPIC_DEFAULT_EFFORT", {})
check("B4 the default table names exactly the docs' supported models",
      set(table) == DOCS_SUPPORTED, sorted(set(table) ^ DOCS_SUPPORTED))
check("B5 ...medium for exactly the two the docs name, high for the rest",
      {m for m, v in table.items() if v == "medium"} == {"claude-opus-5-5", "claude-haiku-5-5"}
      and all(v in ("medium", "high") for v in table.values()), table)
check("B6 the server's level list mirrors the brain's (one enum, cannot drift)",
      getattr(INF, "ANTHROPIC_EFFORT_LEVELS", None) == getattr(IC, "ANTHROPIC_EFFORTS", ()) == ALL5)
src_ic = open(os.path.join(APP_ROOT, "brain/mastercode/inference_config.py"), encoding="utf-8").read()
check("B7 the table cites its source page",
      "platform.claude.com/docs/en/build-with-claude/effort" in src_ic)

# ── C. one rule decides what goes on the wire ─────────────────────────────
print("C. anthropic_wire_effort: a chosen level the model accepts, else nothing")
awe = getattr(IC, "anthropic_wire_effort", None)


def c(m, e):
    return awe(m, e) if callable(awe) else ("missing", "missing")


check("C1 nothing chosen sends nothing, and says nothing", c("claude-haiku-5-5", None) == (None, None))
check("C2 a level the model lists is sent", c("claude-haiku-5-5", "high") == ("high", None))
r = c("claude-sonnet-4-6", "xhigh")
check("C3 a level its row does not list is not sent, and the reason names its levels",
      r[0] is None and "accepts low, medium, high, max" in str(r[1]), r)
r = c("claude-haiku-4-5", "low")
check("C4 a model that takes no effort sends none", r[0] is None and "no effort" in str(r[1]), r)
r = c("claude-not-listed", "high")
check("C5 a model the list cannot speak for sends none (never a guess)",
      r[0] is None and "does not say" in str(r[1]), r)
r = c("claude-haiku-5-5", "minimal")
check("C6 an OpenRouter-only level is not an Anthropic one",
      r[0] is None and "not an Anthropic effort level" in str(r[1]), r)

# ── D. the gateway ───────────────────────────────────────────────────────
print("D. _call_anthropic puts it on the request, and recovers from a refusal")
SENT: list = []
REPLIES: list = []


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


# The gateway opens aiohttp.ClientSession(...) through its own module
# attribute (provider_gateway.aiohttp); the stand-in replaces that attribute,
# so every request body is recorded and answered from REPLIES.
PG.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=lambda **kw: None,
                                   ClientError=OSError)


async def _key():
    return "sk-test"


PG._load_user_openrouter_key = _key
import server.vault.providers as VP  # noqa: E402


async def _resolve(*a, **kw):
    return types.SimpleNamespace(secret="sk-ant", cred_type="api_key")


VP.resolve = _resolve


async def _noop(*a, **kw):
    return None


PG._log_inference = _noop
PG._log_call_failure = _noop


def _ok(text="done", model="claude-haiku-5-5"):
    return {"json": {"model": model, "stop_reason": "end_turn",
                     "usage": {"input_tokens": 5, "output_tokens": 3},
                     "content": [{"type": "text", "text": text}]}}


def _refusal(message, status=400):
    return {"status": status, "text": json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": message}})}


def _anth(model, effort=None, **kw):
    """_call_anthropic, passing `effort` only when given: on a tree whose
    _call_anthropic has no such parameter the call raises and the check fails."""
    if effort is not None:
        kw["effort"] = effort
    return _try(lambda: run(PG._call_anthropic([{"role": "user", "content": "go"}], model,
                                               max_tokens=1000, **kw)))


SENT.clear()
REPLIES[:] = [_ok()]
res = _anth("claude-haiku-5-5", "high")
check("D1 a chosen level the model takes goes out as output_config.effort",
      len(SENT) == 1 and SENT[0].get("output_config") == {"effort": "high"} and "effort" not in SENT[0],
      SENT[:1] or res)
check("D2 ...and the result says it was sent",
      isinstance(res, dict) and (res.get("effort") or {}).get("sent") == "high", res)
SENT.clear()
REPLIES[:] = [_ok()]
res = _anth("claude-haiku-5-5")
check("D3 nothing chosen: no output_config at all (the model's own default, as before)",
      len(SENT) == 1 and "output_config" not in SENT[0], SENT[:1])
check("D4 ...and the result names that default instead of a level that was never sent",
      isinstance(res, dict) and res.get("effort") == {"sent": None, "default": "medium"}, res)
SENT.clear()
REPLIES[:] = [_ok(model="claude-sonnet-4-6")]
res = _anth("claude-sonnet-4-6", "xhigh")
check("D5 a level the model's row does not list is not sent",
      len(SENT) == 1 and "output_config" not in SENT[0], SENT[:1])
check("D6 ...the result says why, and names the default that ran",
      isinstance(res, dict) and (res.get("effort") or {}).get("sent") is None
      and (res.get("effort") or {}).get("default") == "high"
      and "does not accept" in str((res.get("effort") or {}).get("why")), res)
SENT.clear()
REPLIES[:] = [_refusal("output_config.effort: 'max' is not supported for this model"), _ok()]
res = _anth("claude-haiku-5-5", "max")
check("D7 a 400 about the effort is re-sent ONCE, without it",
      len(SENT) == 2 and SENT[0].get("output_config") == {"effort": "max"}
      and "output_config" not in SENT[1], SENT)
check("D8 ...the turn gets its answer, and the result says the API refused the level",
      isinstance(res, dict) and res.get("content") == "done"
      and (res.get("effort") or {}).get("sent") is None
      and "refused" in str((res.get("effort") or {}).get("why")), res)
SENT.clear()
REPLIES[:] = [_refusal("messages: roles must alternate")]
res = _anth("claude-haiku-5-5", "high")
check("D9 any other 400 is not retried (one request, the error returned)",
      len(SENT) == 1 and isinstance(res, dict) and str(res.get("error", "")).startswith("anthropic 400"),
      (SENT, res))
SENT.clear()
REPLIES[:] = [_refusal("output_config.effort: not supported")]
res = _anth("claude-haiku-5-5")
check("D10 with no effort sent, a 400 that names effort is not re-sent",
      len(SENT) == 1 and isinstance(res, dict) and res.get("error"), (SENT, res))
SENT.clear()
REPLIES[:] = [_ok()]
fmt = {"format": {"type": "json_schema", "schema": {"type": "object"}}}
res = _anth("claude-haiku-5-5", "low", extra_body={"output_config": dict(fmt)})
check("D11 a caller's own output_config is kept, the effort merged into it",
      len(SENT) == 1 and SENT[0].get("output_config") == {**fmt, "effort": "low"}, SENT[:1])
SENT.clear()
REPLIES[:] = [_refusal("max_tokens: 1000 > 800, which is the maximum allowed number of output tokens"),
              _refusal("output_config.effort: not supported"), _ok()]
res = _anth("claude-haiku-5-5", "high")
check("D12 an output-limit refusal and an effort refusal on one call are each re-sent once",
      len(SENT) == 3 and SENT[1].get("max_tokens") == 800 and SENT[1].get("output_config")
      and "output_config" not in SENT[2] and isinstance(res, dict) and res.get("content") == "done",
      [(s.get("max_tokens"), s.get("output_config")) for s in SENT])

# ── E. call_model: the person's pick, never the fallback ──────────────────
print("E. call_model: chosen_effort reaches the Anthropic wire, the 'low' fallback never")


def _cm(**kw):
    return _try(lambda: run(PG.call_model([{"role": "user", "content": "hi"}], max_tokens=1000, **kw)))


SENT.clear()
REPLIES[:] = [_ok()]
_cm(provider="anthropic", provider_model="claude-haiku-5-5",
    reasoning_effort="high", chosen_effort="high")
check("E1 a route with a chosen effort sends it on the Anthropic wire",
      len(SENT) == 1 and SENT[0].get("output_config") == {"effort": "high"}, SENT[:1])
SENT.clear()
REPLIES[:] = [_ok()]
_cm(provider="anthropic", provider_model="claude-haiku-5-5", reasoning_effort="low")
check("E2 an unset route (route_effort's 'low', no chosen_effort) sends nothing there",
      len(SENT) == 1 and "output_config" not in SENT[0], SENT[:1])
SENT.clear()
REPLIES[:] = [{"json": {"model": "moonshotai/kimi-k3", "choices": [
    {"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}}]
_cm(provider="openrouter", provider_model="moonshotai/kimi-k3",
    reasoning_effort="low", chosen_effort="high")
check("E3 the OpenAI wire still sends reasoning_effort, exactly as before",
      len(SENT) == 1 and SENT[0].get("reasoning") == {"effort": "low", "exclude": False}
      and "output_config" not in SENT[0], SENT[:1])
ce = getattr(IC, "chosen_effort", None)
check("E4 chosen_effort: the route's own pick, or None (never 'low')",
      callable(ce) and ce({"effort": " high "}) == "high" and ce({}) is None
      and ce({"effort": "  "}) is None and ce(None) is None)
check("E5 route_effort is unchanged: the pick, else 'low'",
      IC.route_effort({}) == "low" and IC.route_effort({"effort": "max"}) == "max")
from mastercode.runtime import tools as RT  # noqa: E402
check("E6 the turn's wrapper (runtime/tools._call_entity_model) takes chosen_effort",
      "chosen_effort" in inspect.signature(RT._call_entity_model).parameters)


def _callers_missing_chosen() -> list[str]:
    """Every call that sends reasoning_effort=route_effort(...) and every
    kw.setdefault("reasoning_effort", ...) must send chosen_effort beside it,
    or that lane drops the person's pick on the Anthropic wire."""
    missing = []
    for base in ("brain", "server"):
        for dirpath, _dirs, files in os.walk(os.path.join(APP_ROOT, base)):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                src = open(path, encoding="utf-8", errors="ignore").read()
                if "reasoning_effort" not in src:
                    continue
                rel = os.path.relpath(path, APP_ROOT)
                tree = ast.parse(src)
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    body_src = ast.get_source_segment(src, fn) or ""
                    for node in ast.walk(fn):
                        if not isinstance(node, ast.Call):
                            continue
                        kws = {k.arg: k.value for k in node.keywords if k.arg}
                        val = kws.get("reasoning_effort")
                        if (isinstance(val, ast.Call) and getattr(val.func, "id", "") == "route_effort"
                                and "chosen_effort" not in kws):
                            missing.append(f"{rel}:{node.lineno}")
                        if (getattr(node.func, "attr", "") == "setdefault" and node.args
                                and isinstance(node.args[0], ast.Constant)
                                and node.args[0].value == "reasoning_effort"
                                and 'setdefault("chosen_effort"' not in body_src):
                            missing.append(f"{rel}:{node.lineno}")
    return sorted(set(missing))


_miss = _callers_missing_chosen()
check("E7 every lane that sends route_effort also sends chosen_effort", not _miss, _miss)
SESSION = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/session.py"), encoding="utf-8").read()
_lit = None
for _n in ast.walk(ast.parse(SESSION)):
    if (isinstance(_n, ast.Assign) and len(_n.targets) == 1
            and isinstance(_n.targets[0], ast.Name) and _n.targets[0].id == "_chat_call_kw"
            and isinstance(_n.value, ast.Dict)):
        _lit = _n.value
check("E8 the Host turn's call kwargs carry chosen_effort, so EVERY call of the turn sends it "
      "(a top-level effort that changes between requests restarts Anthropic's cache)",
      _lit is not None and any(isinstance(k, ast.Constant) and k.value == "chosen_effort"
                               for k in _lit.keys))

# ── F. what the turn says it ran at ───────────────────────────────────────
print("F. describe_effort: the MODEL_ACTUAL line and the turn's record")
de = getattr(IC, "describe_effort", None)
el = getattr(IC, "effort_label", None)


def lab(p, m, chosen, reff):
    return el(de(p, m, chosen, reff)) if callable(de) and callable(el) else None


check("F1 an unset Anthropic Host reads 'default (medium)', never the 'low' it never sent",
      lab("anthropic", "claude-haiku-5-5", None, "low") == "default (medium)",
      lab("anthropic", "claude-haiku-5-5", None, "low"))
check("F2 a chosen, accepted level reads as itself",
      lab("anthropic", "claude-haiku-5-5", "high", "high") == "high")
_l = lab("anthropic", "claude-sonnet-4-6", "xhigh", "xhigh") or ""
check("F3 a dropped pick reads as the default that ran, and why the pick was not sent",
      _l.startswith("default (high); xhigh not sent: ") and "does not accept" in _l, _l)
check("F4 a model whose default the docs do not name reads 'default', no invented level",
      lab("anthropic", "claude-not-listed", None, "low") == "default")
_loc = lab("local", "qwen3:8b", None, "low") or ""
check("F5 the local wire sends none, and says so instead of 'low'",
      _loc.startswith("default (") and "low" not in _loc.split("(", 1)[0], _loc)
check("F6 the OpenAI wire reads what it sends, as before",
      lab("openrouter", "moonshotai/kimi-k3", None, "low") == "low"
      and lab("kimi", "k3", "high", "high") == "high")
_at = SESSION.find('"MODEL_ACTUAL')
_ma = SESSION[_at:_at + 400]
import re as _re  # noqa: E402
check("F7 session.py's MODEL_ACTUAL prints the described effort, not the raw fallback",
      "_eff_label(_turn_effort)" in _ma
      and _re.search(r"_turn_effort = _eff_desc\(\s*_inference_provider, _inference_model,\s*"
                     r"_chosen_effort, _reasoning_effort\)", SESSION) is not None, _ma)
check("F8 the model standard judges a chosen Anthropic level, and an unset one not at all",
      MS.effective_effort("anthropic", "high") == "high" and MS.effective_effort("anthropic", None) is None
      and MS.effective_effort("openrouter", None) == "low" and MS.effective_effort("xai", "high") is None)

# ── G. the writer and the options use the same rule ───────────────────────
print("G. validate_role_effort / effort_options / the routing writer")
vre = INF.validate_role_effort


def v(p, e, m=None):
    try:
        return vre(p, e, m)
    except TypeError as ex:      # an older two-argument validator
        return f"TypeError: {ex}"


check("G1 a level the model's row lists is accepted", v("anthropic", "high", "claude-haiku-5-5") is None,
      v("anthropic", "high", "claude-haiku-5-5"))
check("G2 a level it does not list is refused, naming what it accepts",
      "accepts low, medium, high, max" in str(v("anthropic", "xhigh", "claude-sonnet-4-6")),
      v("anthropic", "xhigh", "claude-sonnet-4-6"))
check("G3 a model that takes none is refused any",
      v("anthropic", "low", "claude-haiku-4-5") is not None)
check("G4 with no model, an Anthropic effort is refused (it is per model)",
      v("anthropic", "high") is not None)
check("G5 blank is always allowed: the model's own default",
      v("anthropic", "", "claude-haiku-5-5") is None and v("anthropic", None) is None)
check("G6 OpenRouter and kimi are unchanged",
      v("openrouter", "minimal") is None and v("kimi", "medium") is not None and v("kimi", "max") is None)
eo = getattr(INF, "effort_options", None)
check("G7 effort_options: Haiku 5.5's own levels, and its default for the blank pick",
      callable(eo) and eo("anthropic", "claude-haiku-5-5") == {
          "provider": "anthropic", "model": "claude-haiku-5-5", "efforts": list(ALL5),
          "default": "medium", "known": True},
      callable(eo) and eo("anthropic", "claude-haiku-5-5"))
check("G8 ...a model that takes none offers none and names no default",
      callable(eo) and eo("anthropic", "claude-haiku-4-5")["efforts"] == []
      and eo("anthropic", "claude-haiku-4-5")["default"] is None)
check("G9 ...a Claude pick from the OpenRouter catalog is read as the Anthropic route it runs on",
      callable(eo) and eo("privatae", "anthropic/claude-haiku-5.5")["model"] == "claude-haiku-5-5"
      and eo("privatae", "anthropic/claude-haiku-5.5")["efforts"] == list(ALL5))
check("G10 ...other providers keep their static sets",
      callable(eo) and eo("openrouter", "moonshotai/kimi-k3")["efforts"]
      == list(INF.TEXT_ROLE_EFFORTS["openrouter"]))
check("G11 the routing GET hands the picker per-role options",
      "role_effort_options" in open(os.path.join(APP_ROOT, "server/routes/inference.py"),
                                    encoding="utf-8").read())

WRITES: list = []


class _WPool:
    async def fetchrow(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, sql, *args):
        WRITES.append(args)


_real_pool, _real_log, _real_report = (INF.brain_service.get_pool, INF._log_route_apply,
                                       INF._report_config_change_to_central)
INF.brain_service.get_pool = lambda: _WPool()
INF._log_route_apply = _noop
INF._report_config_change_to_central = _noop


def _write(**body):
    WRITES.clear()
    return _try(run, INF.apply_role_route(INF.SetRoleModel(role="review", **body)))


def _stored():
    js = [w for w in WRITES if w and isinstance(w[0], str)]
    return json.loads(js[-1][0])["model_routing"]["review"] if js else None


r = _write(provider="anthropic", model="claude-haiku-5-5", effort="high")
check("G12 the Orchestrator's writer stores an accepted Anthropic level",
      _stored() == {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "high"}, (r, _stored()))
r = _write(provider="anthropic", model="claude-sonnet-4-6", effort="xhigh")
check("G13 ...and refuses one the model does not take, with the reason (400)",
      getattr(r, "status_code", None) == 400 and "accepts" in str(getattr(r, "detail", "")), r)
INF.brain_service.get_pool, INF._log_route_apply, INF._report_config_change_to_central = \
    _real_pool, _real_log, _real_report

# ── H. a chat box's per-thread pick ───────────────────────────────────────
print("H. the per-thread effort pick rides the turn")


class _RoutePool:
    def __init__(self, routing):
        self.routing = routing

    async def fetchrow(self, *a, **k):
        return {"behavioral_params": {"model_routing": self.routing}}


_pool = _RoutePool({"chat": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "high"}})
rr = IC.resolve_role_route
r = run(rr(_pool, 1, "chat", thread_override={"effort": "low"}))
check("H1 a thread's pick replaces the stored route's effort for that turn",
      r.get("model") == "claude-haiku-5-5" and r.get("effort") == "low", r)
r = run(rr(_pool, 1, "chat", thread_override={"provider": "privatae",
                                              "model_id": "anthropic/claude-haiku-5.5", "effort": "max"}))
check("H2 ...and rides a thread's model pick too (the Claude pick runs on anthropic)",
      r.get("provider") == "anthropic" and IC.anthropic_api_model_id(r.get("model")) == "claude-haiku-5-5"
      and r.get("effort") == "max", r)
r = run(rr(_pool, 1, "chat", thread_override={"context_budget": 5000}))
check("H3 an override with no pick leaves the stored effort alone", r.get("effort") == "high", r)
r = run(rr(_RoutePool({}), 1, "chat", thread_override={"effort": "low"}))
check("H4 with nothing routed, a pick routes nothing", not r.get("model") and not r.get("effort"), r)

cte = getattr(INF, "check_thread_effort", None)
check("H5 check_thread_effort refuses a level the picked model does not take",
      callable(cte) and "accepts" in str(run(cte("xhigh", "anthropic", "claude-sonnet-4-6"))))
check("H6 ...accepts one it does, read through the OpenRouter-catalog spelling",
      callable(cte) and run(cte("high", "privatae", "anthropic/claude-haiku-5.5")) is None)

# Drive the two send routes for real: only the turn itself is captured.
# circles.py imports server.main at module scope, and server.main mounts the
# routers (circles among them) at import time: the circular import every
# Circles route test breaks with a stand-in module.
sys.modules.setdefault("server.main", types.ModuleType("server.main"))
from server import brain_service as BS  # noqa: E402
from server.routes import brain as BR  # noqa: E402
from server.routes import circles as CR  # noqa: E402
import mastercode.runtime.turn_types as TT  # noqa: E402
SENDS: list = []


def _fake_send(**kw):
    """brain_service.send_message's stand-in: records what the route passed
    when the route calls it, and streams nothing."""
    SENDS.append(kw)

    async def _frames():
        if False:
            yield ""
    return _frames()


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {"accept": "text/x-vercel-ai-data-stream"}
        self.state = types.SimpleNamespace(app_token=None)

    async def json(self):
        return self._body


_saved = (BS.send_message, BS.get_pool, BS._entity_id)
BS.send_message = _fake_send
BS.get_pool = lambda: _pool
BS._entity_id = 1
SENDS.clear()
out = _try(run, BR.send_message_session_free(_Req({"message": "hi", "project_id": 7, "effort": "low"})))
check("H7 Workbench: a valid pick rides the turn as thread_override {effort}",
      len(SENDS) == 1 and SENDS[0].get("thread_override") == {"effort": "low"}, (out, SENDS))
SENDS.clear()
out = _try(run, BR.send_message_session_free(_Req({"message": "hi", "project_id": 7})))
check("H8 Workbench: no pick sends the turn exactly as before (no override)",
      len(SENDS) == 1 and SENDS[0].get("thread_override") is None, SENDS)
_pool.routing["chat"] = {"provider": "anthropic", "model": "claude-sonnet-4-6"}
SENDS.clear()
out = _try(run, BR.send_message_session_free(_Req({"message": "hi", "effort": "xhigh"})))
check("H9 Workbench: a level the routed model does not take is refused (400), no turn runs",
      getattr(out, "status_code", None) == 400 and not SENDS, (out, SENDS))


async def _no_thread(body, eid, request_id, **kw):
    return None


async def _no_entry(key):
    return None


async def _stay(*a, **kw):
    return {"moved": False}


_csaved = (CR._thread_id_from_body, CR._thread_entry, CR._audit, TT.resolve_thread_for_message)
CR._thread_id_from_body, CR._thread_entry, CR._audit = _no_thread, _no_entry, _noop
TT.resolve_thread_for_message = _stay
_pool.routing["chat"] = {"provider": "anthropic", "model": "claude-haiku-5-5"}
SENDS.clear()
out = _try(run, CR.send_message(_Req({"target_kind": "self", "target_id": "self", "body": "hi",
                                      "effort": "max"})))
check("H10 Circles: a valid pick rides the turn's thread_override",
      len(SENDS) == 1 and (SENDS[0].get("thread_override") or {}).get("effort") == "max", (out, SENDS))
SENDS.clear()
out = _try(run, CR.send_message(_Req({"target_kind": "self", "target_id": "self", "body": "hi",
                                      "model": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
                                      "effort": "xhigh"})))
check("H11 Circles: checked against the PICKED model (Sonnet 4.6 takes no xhigh): 400, no turn",
      getattr(out, "status_code", None) == 400 and not SENDS, (out, SENDS))
SENDS.clear()
out = _try(run, CR.send_message(_Req({"target_kind": "self", "target_id": "self", "body": "hi"})))
check("H12 Circles: no pick, no effort on the override",
      len(SENDS) == 1 and "effort" not in (SENDS[0].get("thread_override") or {}), SENDS)
CR._thread_id_from_body, CR._thread_entry, CR._audit, TT.resolve_thread_for_message = _csaved
BS.send_message, BS.get_pool, BS._entity_id = _saved

# ── I. the Claude Code harness offers xhigh ───────────────────────────────
print("I. the Claude Code harness")
check("I1 claude-code offers xhigh, between high and max",
      INF.HARNESS_EFFORTS.get("claude-code") == ("low", "medium", "high", "xhigh", "max"),
      INF.HARNESS_EFFORTS.get("claude-code"))
check("I2 ...and the validator takes it", INF.validate_harness_effort("claude-code", "xhigh") is None)

# ── J. a failed read of the model list is read again ──────────────────────
# The gateway filled a cold list through warm_route_catalog,
# which reads a provider only when it has NO cache entry. One failed read
# leaves an entry (the failure, cached 30 s), so every later call skipped the
# read for the process's life and a person's level was never sent again,
# though the Orchestrator and the chat boxes, which read through the TTLs,
# would have read the list afresh. Stand-in read; never the network.
print("J. a failed read of the Anthropic model list is read again")
READS: list = []
READ_MODE = {"fail": True, "slow": 0.0}


async def _fake_direct_models(p):
    import time as _t
    now = _t.monotonic()
    c = INF._DIRECT_MODELS_CACHE.get(p)
    if c and c["exp"] > now:
        return c["data"], c.get("err")
    READS.append(p)
    if READ_MODE["slow"]:
        await asyncio.sleep(READ_MODE["slow"])
    if READ_MODE["fail"]:
        INF._DIRECT_MODELS_CACHE[p] = {"data": [], "err": "anthropic catalog unreachable",
                                       "exp": now + INF._DIRECT_MODELS_TTL_FAIL}
        return [], "anthropic catalog unreachable"
    INF._DIRECT_MODELS_CACHE[p] = {"data": rows, "exp": now + INF._DIRECT_MODELS_TTL}
    return rows, None


_jsaved = (INF._direct_models, INF.brain_service.get_pool, INF._DIRECT_MODELS_CACHE.get("anthropic"),
           INF._CATALOG_CACHE.get("data"), INF._CATALOG_CACHE.get("exp"))
INF._direct_models = _fake_direct_models
# The OpenRouter half of warm_route_catalog stays quiet (it builds that list
# over the network when it is empty): only the Anthropic read is under test.
INF._CATALOG_CACHE["data"] = {"openrouter_models": [{"id": "x/y"}]}
INF._CATALOG_CACHE["exp"] = 1e18
INF.brain_service.get_pool = lambda: object()      # inside a running server
warm = IC.anthropic_model_efforts_warm
try:
    INF._DIRECT_MODELS_CACHE.pop("anthropic", None)
    READS.clear()
    first = _try(run, warm("claude-haiku-5-5"))
    check("J1 a cold list is read once, and a failed read says nothing (None)",
          first is None and READS == ["anthropic"], (first, READS))
    again = _try(run, warm("claude-haiku-5-5"))
    check("J2 ...within the failure's 30 s it is not read again", READS == ["anthropic"], READS)
    # The failure's time passes, and the list can be read again.
    INF._DIRECT_MODELS_CACHE["anthropic"]["exp"] = 0
    READ_MODE["fail"] = False
    later = _try(run, warm("claude-haiku-5-5"))
    check("J3 once the failure has expired the list is read again, and the levels are known",
          later == ALL5 and READS == ["anthropic", "anthropic"], (later, READS))
    INF._DIRECT_MODELS_CACHE.pop("anthropic", None)
    READ_MODE["fail"] = True
    _try(run, warm("claude-haiku-5-5"))                 # fails, cached
    INF._DIRECT_MODELS_CACHE["anthropic"]["exp"] = 0    # ...and expires
    READ_MODE["fail"] = False
    SENT.clear()
    REPLIES[:] = [_ok()]
    res = _anth("claude-haiku-5-5", "high")
    check("J4 ...so the next call after an expired failure sends the person's level",
          len(SENT) == 1 and SENT[0].get("output_config") == {"effort": "high"}
          and isinstance(res, dict) and (res.get("effort") or {}).get("sent") == "high",
          (SENT[:1], res if not isinstance(res, dict) else res.get("effort")))
    INF._DIRECT_MODELS_CACHE.pop("anthropic", None)
    READS.clear()
    READ_MODE.update(fail=False, slow=0.05)

    async def _two():
        return await asyncio.gather(warm("claude-haiku-5-5"), warm("claude-haiku-5-5"))
    both = _try(run, _two())
    check("J5 two calls on a cold list share one read", READS == ["anthropic"]
          and isinstance(both, list) and both == [ALL5, ALL5], (both, READS))
    READ_MODE["slow"] = 0.0
    INF._DIRECT_MODELS_CACHE.pop("anthropic", None)
    INF.brain_service.get_pool = lambda: None
    READS.clear()
    out = _try(run, warm("claude-haiku-5-5"))
    check("J6 outside a running server (no pool) nothing is read", out is None and READS == [],
          (out, READS))
finally:
    (INF._direct_models, INF.brain_service.get_pool, _a, _d, _e) = _jsaved
    if _a is None:
        INF._DIRECT_MODELS_CACHE.pop("anthropic", None)
    else:
        INF._DIRECT_MODELS_CACHE["anthropic"] = _a
    INF._CATALOG_CACHE["data"], INF._CATALOG_CACHE["exp"] = _d, _e

print()
if FAILS:
    print(f"[anthropic-effort] FAIL ({len(FAILS)} failing): {FAILS}")
    sys.exit(1)
print("[anthropic-effort] PASS (0 failing)")
