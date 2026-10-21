#!/usr/bin/env python3
"""Background model calls ask for the model's OWN output limit, and a reply
that limit cuts off is reported as cut — never as a parse failure.

Every one of these callers asked for a fixed figure the platform picked, and
every figure was reached in production:

  research      web research 4,096 / 8,192, deep_research 4,096 / 2,048.
  summaries     classifier 800, patterns 400, relationship 400, thread summary and
                batch 8,000. (The relationship model and its session-close call were
                retired with the simulation; its rows here went with it.)

The diff reviewer (MAX_OUTPUT_TOKENS = 8000, hit exactly) is gone; section 4
pins that it does not come back as a caller. The memory_graph extraction and
the batch, classifier and pattern callers went with their code: the memory
graph, the batch consolidator and the second model deciding what to remember
are gone, so the thread summary is the one summary caller left.

The rule: each model's own published limit; read the stop
reason; a cut-off write is reported as cut. `max_tokens=None` is the gateway
contract for "the routed model's own limit" (provider_gateway.call_model
resolves it from the catalog, held to half the window).

Pure: no network, no DB, no cluster — fake pools and fake model_fns.
    APP_ROOT=<repo> python3 scripts/test_background_calls_use_model_output_limit.py
"""
import ast
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_root = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, "brain"))

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS += 1


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


LOGS = _Logs()
logging.getLogger().addHandler(LOGS)
logging.getLogger().setLevel(logging.DEBUG)


def _cut(content: str, limit: int = 128000) -> dict:
    """A gateway result the model's output limit stopped (the gateway's contract)."""
    return {"content": content, "truncated": True, "stop_reason": "max_tokens",
            "output_limit": limit, "truncated_tool_call_ids": []}


# ── 0. no fixed figure left at any of these call sites (static sweep) ───────
print("== 0. every max_tokens at these call sites is the model's own limit ==")
_SITES = {
    "brain/mastercode/runtime/web_research.py": "research",
    "brain/mastercode/skills/deep_research.py": "research",
    "brain/mastercode/memory_consolidation.py": "summaries",
}
for rel, row in _SITES.items():
    tree = ast.parse(open(os.path.join(_root, rel), encoding="utf-8").read())
    fixed = []
    for node in ast.walk(tree):
        # max_tokens=<int literal> on any call
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in ("max_tokens", "max_output_tokens") \
                        and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, int):
                    fixed.append(f"call:{node.lineno}={kw.value.value}")
        # a max_tokens parameter defaulting to an int literal
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            pos = a.posonlyargs + a.args
            for arg, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                if arg.arg == "max_tokens" and isinstance(d, ast.Constant) \
                        and isinstance(d.value, int):
                    fixed.append(f"default:{node.name}={d.value}")
            for arg, d in zip(a.kwonlyargs, a.kw_defaults):
                if arg.arg == "max_tokens" and isinstance(d, ast.Constant) \
                        and isinstance(d.value, int):
                    fixed.append(f"default:{node.name}={d.value}")
        # module output-cap constants as int literals
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, int):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in (
                        "MAX_OUTPUT_TOKENS", "CLASSIFIER_MAX_TOKENS",
                        "SUMMARY_MAX_TOKENS", "BATCH_MAX_TOKENS"):
                    fixed.append(f"{t.id}={node.value.value}")
    check(f"{row} {rel}: no fixed output figure", not fixed, ", ".join(fixed))

# research takes its links from web_research.research_links,
# whose search call is the one research makes on the model.
_wr = open(os.path.join(_root, "brain/mastercode/runtime/web_research.py"), encoding="utf-8").read()
_line = next((ln for ln in _wr.splitlines()
              if "await search(" in ln and "system=RESEARCH_SYSTEM" in ln), "")
check("research's search call (web_research.research_links): the model's own limit, not 8192",
      bool(_line) and "max_tokens=None" in _line and "8192" not in _line, _line.strip())


CALLS: list[dict] = []


def _fn(reply):
    async def f(messages, **kw):
        CALLS.append({"messages": messages, **kw})
        return reply(messages) if callable(reply) else dict(reply)
    return f


# ── 3. web research and deep research ────────────────────────────────────────
print("\n== 3. web research: the model's limit, a cut answer is flagged ==")
from mastercode import provider_gateway as pg  # noqa: E402
from mastercode.runtime import web_research as wr  # noqa: E402

GW: list[dict] = []


async def _gw(messages=None, **kw):
    GW.append(kw)
    return {"content": "The answer, cut mid", "truncated": True, "output_limit": 128000,
            "stop_reason": "max_tokens",
            "annotations": [{"url_citation": {"url": "https://example.org/a", "title": "A"}}]}


pg.call_model = _gw
route = {"provider": "anthropic", "model": "claude-opus-5-5", "_role": "chat"}
GW.clear()
envw = asyncio.run(wr.search_on_route("q", route))
check("search_on_route's default is the model's own limit (None reaches the gateway)",
      bool(GW) and "max_tokens" in GW[0] and GW[0]["max_tokens"] is None, str(GW[:1]))
_r = envw.get("result") if isinstance(envw.get("result"), dict) else {}
check("a cut answer is flagged on the ok envelope, with a note naming the limit",
      envw.get("status") == "ok" and _r.get("truncated") is True
      and "cut off" in (_r.get("note") or "") and "128000" in (_r.get("note") or ""),
      str(envw)[:300])

# The Responses-API dialer (openai / xai) leaves max_output_tokens off.
SENT: list[dict] = []


async def _post(endpoint, headers, body, total_timeout=180):
    SENT.append(body)
    return 200, json.dumps({
        "model": "gpt-6-astra", "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{
            "type": "output_text", "text": "partial answer",
            "annotations": [{"type": "url_citation", "url": "https://example.org/b",
                             "title": "B"}]}]}]})


async def _cred(*a, **k):
    return "sk-test"


import server.vault.providers as _prov  # noqa: E402
_prov.resolve_credential = _cred
wr._post_json = _post
SENT.clear()
outr, _m = asyncio.run(wr._search_responses_api(
    "openai", "q", {"provider": "openai", "model": "gpt-6-astra"}, "sys", None,
    ("web_search",)))
check("the Responses API body carries no fixed max_output_tokens",
      bool(SENT) and "max_output_tokens" not in SENT[0], str(SENT[:1]))
check("an 'incomplete: max_output_tokens' answer is carried as truncated",
      outr.get("truncated") is True and outr.get("content") == "partial answer", str(outr))

import inspect  # noqa: E402
check("search()'s default is the model's own limit",
      inspect.signature(wr.search).parameters["max_tokens"].default is None)


# ── 4. no thread summary or diff review call remains ────────────────────────
# The rolling summary was a model call outside the Host's turn; it is retired
# (migrations/v241), and what falls out of a window rides as the Host's own
# close_turn notes (evicted_record), which no model writes.
print("\n== 4. no thread summary or diff review call remains ==")
check("no thread summary module ships",
      not os.path.exists(os.path.join(_root, "brain/mastercode/thread_consolidator.py")))
# The diff reviewer (T-6) was a model call on every landed build; removed
# 2026-10-20 (the Host reviews in its own turn).
check("no diff review module ships",
      not os.path.exists(os.path.join(_root, "brain/mastercode/runtime/diff_review.py")))

print()
print("FAIL" if FAILS else "PASS", f"({FAILS} failing checks)")
sys.exit(1 if FAILS else 0)
