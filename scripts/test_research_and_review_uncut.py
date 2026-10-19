#!/usr/bin/env python3
"""Cuts found next to the output-limit fixes.

Container-free: APP_ROOT=<repo> python3 scripts/test_research_and_review_uncut.py.
No pod, no DB, no network (stand-in HTTP and model).

  X1 deep_research: the web answer is not cut at 3,000 characters, and the
     evidence for the synthesis is never sliced through its JSON (it was
     `json.dumps(...)[:12000]`); over half the model's window, only the web
     answer is shortened, with a marker saying how much was left out
  X2 diff_review: a call is given up for SILENCE, not elapsed time (a call that
     keeps showing life past the idle window completes); the call cap is a
     runaway backstop far above the old 4, and hitting it is said
  W  web search: a Responses-API search is streamed and keeps its sources
     (response.completed, or the annotation events), with no 180 s total; an
     OpenRouter `:online` call stays buffered (its sources ride the message)
  X3 memory embeddings: a long memory is embedded window by window into one
     vector (embed_windows.embed_whole, or the same method where that module
     is absent), never cut at 2,000 / 8,000 characters

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[g1-round2] FAIL {name} {extra}")


from mastercode.skills import deep_research as DR  # noqa: E402
from mastercode.runtime import diff_review as DV  # noqa: E402
from mastercode.runtime import web_research as WR  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import context_budget as CB  # noqa: E402

# ── X1 deep_research ──────────────────────────────────────────────────────
_src = open(os.path.join(APP_ROOT, "brain/mastercode/skills/deep_research.py")).read()
check("X1a the web answer is taken whole (no [:3000])", "[:3000]" not in _src)
check("X1b the evidence is never sliced through its JSON (no [:12000])", "[:12000]" not in _src)
_ev = getattr(DR, "_evidence_for_window", None)
check("X1c the evidence is sized from the synthesis model's window", callable(_ev))
if callable(_ev):
    async def _win(model, provider=None):
        return 4000          # tokens -> half is 8,000 chars
    _saved = CB.model_context_window
    CB.model_context_window = _win
    try:
        big = "w" * 20000
        out = asyncio.run(_ev([], [], [], big, {"model": "m", "provider": "p"}))
        small = asyncio.run(_ev([], [], [], "short answer", {"model": "m", "provider": "p"}))
    finally:
        CB.model_context_window = _saved
    obj = json.loads(out)
    check("X1d over half the window: still valid JSON, the web answer shortened with a marker",
          len(out) <= 8000 and "more characters of the web answer left out" in obj["web_answer"], len(out))
    check("X1e under it: whole", json.loads(small)["web_answer"] == "short answer")

# ── X2 diff_review ────────────────────────────────────────────────────────
GOOD = json.dumps({"ok": True, "score": 90, "summary": "Does what the brief asks.",
                   "blocking": [], "non_blocking": []})
DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"


async def _alive_but_slow(messages, **kw):
    alive = kw.get("on_alive")
    for _ in range(6):                 # 0.3 s total, a sign of life every 0.05 s
        await asyncio.sleep(0.05)
        if alive:
            alive()
    return {"content": GOOD}

env = asyncio.run(DV.review_diff(diff=DIFF, brief="b", model_fn=_alive_but_slow, call_timeout_s=0.15))
check("X2a a call that keeps showing life runs past the idle window and completes",
      env.get("ran") is True and env.get("ok") is True, env)


async def _silent(messages, **kw):
    await asyncio.sleep(0.5)
    return {"content": GOOD}

env = asyncio.run(DV.review_diff(diff=DIFF, brief="b", model_fn=_silent, call_timeout_s=0.1))
check("X2b a silent call is given up, and the reason says no sign of life",
      env.get("ran") is False and "no sign of life" in (env.get("reason") or ""), env.get("reason"))
check("X2c the call cap is a runaway backstop (>= 40, was 4)", DV.MAX_CALLS >= 40, DV.MAX_CALLS)
check("X2d the idle window is minutes, not the old 120 s total", DV.CALL_TIMEOUT_S >= 300, DV.CALL_TIMEOUT_S)


async def _good(messages, **kw):
    return {"content": GOOD}

_two = DIFF + "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
env = asyncio.run(DV.review_diff(diff=_two, brief="b", model_fn=_good, max_call_chars=60, max_calls=1))
check("X2e hitting the backstop is said (backstop_hit + a note), and the skipped file is named",
      env.get("backstop_hit", {}).get("max_calls") == 1 and env.get("files_skipped")
      and "backstop" in (env.get("note") or ""), env)
check("X2f the reviewer asks for the model's own output limit", DV.MAX_OUTPUT_TOKENS is None)

# ── W web search ──────────────────────────────────────────────────────────
SENT: list = []


class _Content:
    def __init__(self, lines):
        self._lines = lines

    async def iter_any(self):
        for ln in self._lines:
            yield (ln + "\n").encode()


class _Resp:
    def __init__(self, lines):
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream"}
        self.content = _Content(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return ""


STREAM: list = []
TIMEOUTS: list = []


class _Sess:
    def __init__(self, timeout=None, **kw):
        TIMEOUTS.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        SENT.append(dict(json))
        return _Resp(STREAM)


import aiohttp  # noqa: E402
_real_sess = aiohttp.ClientSession
aiohttp.ClientSession = _Sess
try:
    final = {"status": "completed", "model": "gpt-x", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "Answer.", "annotations": [
            {"type": "url_citation", "url": "https://a.example", "title": "A"}]}]}]}
    STREAM[:] = ['data: {"type":"response.output_text.delta","delta":"Ans"}',
                 "data: " + json.dumps({"type": "response.completed", "response": final})]
    st, txt = asyncio.run(WR._post_json("https://x/responses", {}, {"model": "gpt-x", "input": "q"}))
    content, anns = WR._parse_responses_output(json.loads(txt))
    check("Wa a Responses-API search is streamed", SENT and SENT[-1].get("stream") is True)
    check("Wb ...and keeps its sources from response.completed",
          content == "Answer." and anns and anns[0]["url_citation"]["url"] == "https://a.example", (content, anns))
    STREAM[:] = ['data: {"type":"response.output_text.delta","delta":"Part "}',
                 'data: {"type":"response.output_text.delta","delta":"two."}',
                 'data: ' + json.dumps({"type": "response.output_text.annotation.added",
                                        "annotation": {"type": "url_citation", "url": "https://b.example"}})]
    st, txt = asyncio.run(WR._post_json("https://x/responses", {}, {"model": "gpt-x", "input": "q"}))
    content, anns = WR._parse_responses_output(json.loads(txt))
    check("Wc with no completed event, the deltas and annotation events still give text + sources",
          content == "Part two." and anns and anns[0]["url_citation"]["url"] == "https://b.example", (content, anns))
    _t = TIMEOUTS[-1]
    check("Wd no wall clock: no total, an idle (sock_read) window instead",
          getattr(_t, "total", 1) is None and (getattr(_t, "sock_read", 0) or 0) >= 300, _t)
finally:
    aiohttp.ClientSession = _real_sess

check("We an OpenRouter :online call is not streamed for its length (its sources ride the message)",
      PG._web_plugin_model("openai/gpt-x:online") and not PG._web_plugin_model("openai/gpt-x"))

# ── X3 memory embeddings: windowed, never cut ─────────────────────────────
from mastercode import memory_engine as ME  # noqa: E402
_me_src = open(os.path.join(APP_ROOT, "brain/mastercode/memory_engine.py")).read()
check("X3a no embed call slices its text (text[:8000] / text[:2000] are gone)",
      '"input": text[:8000]' not in _me_src and "text_capped = text[:2000]" not in _me_src)
_sent: list = []


async def _one(text, model, **kw):
    _sent.append(text)
    return [1.0, 0.0] if len(_sent) % 2 else [0.0, 1.0]

if hasattr(ME, "_embed_one_config_driven"):
    _saved_one = ME._embed_one_config_driven
    ME._embed_one_config_driven = _one
    try:
        long_text = "a" * 8000 + "b" * 8000 + "c" * 4000
        vec = asyncio.run(ME._embed_text_config_driven(long_text, "m", entity_id=1, pool=None))
        _sent_long = list(_sent)
        _sent.clear()
        short = asyncio.run(ME._embed_text_config_driven("short", "m", entity_id=1, pool=None))
    finally:
        ME._embed_one_config_driven = _saved_one
    check("X3b a 20,000-char memory is embedded whole, window by window (every character sent)",
          "".join(_sent_long) == long_text and all(len(x) <= 8000 for x in _sent_long), [len(x) for x in _sent_long])
    check("X3c ...into ONE vector of the model's dimension", isinstance(vec, list) and len(vec) == 2, vec)
    check("X3d a text that fits one window is one call, unchanged", _sent == ["short"] and short == [1.0, 0.0])
else:
    check("X3b the embed lane windows long texts (_embed_one_config_driven)", False)

# ── H1 the transcript wall sized from the window; H2 recall paging ────────
from mastercode.runtime import tool_result_wall as TW  # noqa: E402
_saved_b = TW._bound_tool_result
try:
    def _b_new(result, context_window=None):
        return "x"
    TW._bound_tool_result = _b_new
    _with = CB.wall_kwargs(1_000_000)

    def _b_old(result):
        return "x"
    TW._bound_tool_result = _b_old
    _without = CB.wall_kwargs(1_000_000)
finally:
    TW._bound_tool_result = _saved_b
check("H1a the wall gets the window where it takes one, and nothing where it does not",
      _with == {"context_window": 1_000_000} and _without == {}, (_with, _without))
_sess_src = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/session.py")).read()
_sa_src = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/scoped_agent.py")).read()
check("H1b both Host-loop result sites and the agent loop pass the active window",
      # the dedup-gate site bounds tool_result; the main site bounds the
      # search-deduped copy (_sent_result, runtime/search_dedup.py)
      _sess_src.count("_bound_tool_result(tool_result, **_cb.wall_kwargs(_ctx_window))") == 1
      and _sess_src.count("_bound_tool_result(_sent_result, **_cb.wall_kwargs(_ctx_window))") == 1
      and "_bound_tool_result(tool_result, **_cb.wall_kwargs(_agent_window))" in _sa_src)
from mastercode.runtime.session import _build_host_orchestration_tools  # noqa: E402
_recall = next(t for t in _build_host_orchestration_tools() if t["function"]["name"] == "recall")
_props = _recall["function"]["parameters"]["properties"]
check("H2a recall takes limit and offset (it had no way to read past its first page)",
      "limit" in _props and "offset" in _props, sorted(_props))
# Since the brief prompt, recall runs the five-store search
# (memory_retrieval.unified_search), which pages memory and documents.
_rc = _sess_src[_sess_src.index("_out = await unified_search(") - 1400:_sess_src.index("_out = await unified_search(") + 600]
check("H2b ...and passes them to the engine",
      "limit=_lim, offset=_off" in _rc and "_lim = max(1, int(func_args.get(\"limit\")" in _rc
      and "_off = max(0, int(func_args.get(\"offset\")" in _rc)

# ── H4 a live build is never reaped by the stuck horizon ──────────────────
from mastercode.runtime import agent_dispatch as AD  # noqa: E402
from mastercode.runtime import tools as TL  # noqa: E402


class _MPool:
    def __init__(self):
        self.executed: list = []

    async def fetch(self, q, *a):
        if "factory_build" in q:
            return [{"task_id": 77}]
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        self.executed.append(q)
        return "OK"


async def _h4():
    mp = _MPool()
    d = AD.AgentDispatch(mp)
    d.runtime = object()
    live = asyncio.ensure_future(asyncio.sleep(5))
    TL._FACTORY_BUILD_TASKS[77] = live
    try:
        await d._monitor_tick(600)
        cancelled = live.cancelled() or live.done()
    finally:
        TL._FACTORY_BUILD_TASKS.pop(77, None)
        live.cancel()
    return mp.executed, cancelled

_ex, _cancelled = asyncio.run(_h4())
check("H4 a build whose task is alive in this process is not reaped past the 4 h horizon "
      "(not cancelled, not marked 'build stuck')",
      not _cancelled and not any("build stuck" in q for q in _ex), (_cancelled, _ex[:2]))

# ── H5 "not checked" is said as not checked; H6 every landed file ─────────
from mastercode.runtime import session as SS  # noqa: E402
_c, _t, _lvl = SS._verify_delivery({"app_id": "podapp", "status": "failed",
                                    "verify": {"verdict": "not checked", "version": "0.4.1",
                                               "reason": "the platform never woke its pod"}})
check("H5 a proof the platform never ran reads 'Not checked', not 'Not delivered', and is no error",
      _c.startswith("Not checked: podapp v0.4.1") and "no repair round was spent" in _c
      and "Not delivered" not in _c and _lvl != "error", (_c[:120], _lvl))
check("H6 every landed file is listed, with the count (it was the first 20, no total, no marker)",
      "_files[:20]" not in _sess_src
      and 'f"Files landed in the project ({len(_files)}): "' in _sess_src)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
