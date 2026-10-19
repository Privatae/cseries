#!/usr/bin/env python3
"""Gate: the research lane never hands the host a fabricated answer, and a
failure always names who failed.

Origin: research said it needed the Claude SDK, which had been
removed long before. Three defects, one symptom:

  1. Kimi's `$web_search` on the subscription (coding) endpoint ran the search
     but never injected the results: the tool call came back with a search_id
     claiming ~8.5k tokens of results, and the next round's prompt grew by only
     the ~150-token echo. k3 then answered with reasoning and no content
     (~75% of calls), k2.6 with planning prose and guessed python.org URLs.
     runtime/web_research._search_kimi now VERIFIES injection from
     prompt_tokens and refuses, naming the model and the numbers.
  2. xAI's chat/completions Live Search answers `410 Live search is
     deprecated` — the dialer is now the Responses API `web_search` tool,
     the same wire _search_openai already used.
  3. The pod's app-gated skill files were an old text ("research(query)
     — single-shot Claude SDK call with WebSearch + WebFetch") because gated
     skills were seeded once and never refreshed. The host repeated that file
     verbatim as the cause. default-skills/claude-sdk.md is now claude-code.md
     with no research claim, research.md describes the real lane, and
     entrypoint.sh refreshes gated skills that are present.

This gate pins all three without a live key: the gateway and the HTTP POST
are stubbed; the skill files and entrypoint are read as text.

Run: APP_ROOT=<repo> python3 scripts/test_web_research_dialers.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


from mastercode.runtime import web_research as wr  # noqa: E402
from mastercode import provider_gateway as pg  # noqa: E402


# ── stubs ───────────────────────────────────────────────────────────────
def _kimi_tool_call(claimed: int = 8550, sid: str = "abc"):
    return {"id": "tool_1", "type": "builtin_function",
            "function": {"name": "$web_search",
                         "arguments": json.dumps({"search_result": {"search_id": sid},
                                                  "usage": {"total_tokens": claimed}})}}


class _FakeGateway:
    """Scripted call_model: each call pops the next result; records payloads."""
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        if not self.rounds:
            raise AssertionError("gateway called more times than scripted")
        return self.rounds.pop(0)


async def _with_gateway(rounds, coro_fn):
    fake = _FakeGateway(rounds)
    orig = pg.call_model
    pg.call_model = fake
    try:
        return await coro_fn(), fake
    finally:
        pg.call_model = orig


# ── 1. kimi: injection verified ──────────────────────────────────────────
async def t_kimi():
    route = {"provider": "kimi", "model": "k3", "_role": "research"}
    q = "current python version"

    # (a) search ran, results NOT injected: prompt grew by the echo only.
    rounds = [
        {"content": "", "prompt_tokens": 252, "tool_calls": [_kimi_tool_call(8550)]},
        {"content": "", "prompt_tokens": 410, "reasoning": "search returned a search_id but no content"},
    ]
    res, fake = await _with_gateway(rounds, lambda: wr.search_on_route(q, route, system=wr.RESEARCH_SYSTEM))
    check("kimi: results not injected -> unavailable", res["status"] == "unavailable", json.dumps(res)[:300])
    check("kimi: the reason NAMES the model and the numbers",
          "kimi/k3" in res["result"] and "8550" in res["result"] and "158" in res["result"],
          res["result"][:300])
    check("kimi: the reason says what to change (the host model in the Orchestrator)",
          "host model in the Orchestrator" in res["result"], res["result"][:300])
    check("kimi: envelope carries provider/model for the host",
          res.get("provider") == "kimi" and res.get("model") == "k3", json.dumps(res)[:200])
    check("kimi: exactly two rounds dialed (search + echo), no blind retry loop",
          len(fake.calls) == 2, str(len(fake.calls)))
    echo_msgs = fake.calls[1]["messages"]
    check("kimi: echo replays the tool call typed 'function' (builtin_function 400s on the coding base)",
          echo_msgs[-2]["role"] == "assistant" and echo_msgs[-2]["tool_calls"][0]["type"] == "function"
          and echo_msgs[-1]["role"] == "tool" and echo_msgs[-1]["tool_call_id"] == "tool_1",
          json.dumps(echo_msgs[-2:])[:300])

    # (b) results WERE injected (prompt grew by the claimed tokens): the model's
    # cited answer goes through untouched.
    rounds = [
        {"content": "", "prompt_tokens": 252, "tool_calls": [_kimi_tool_call(8550)]},
        {"content": "Python 3.14.7 (2026-08-05).\n\nSources:\nhttps://www.python.org/downloads/",
         "prompt_tokens": 252 + 8550 + 160},
    ]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route(q, route, system=wr.RESEARCH_SYSTEM))
    check("kimi: results injected -> ok with the cited answer",
          res["status"] == "ok" and res["result"]["sources"]
          and res["result"]["sources"][0]["url"] == "https://www.python.org/downloads/"
          and res["result"]["provider"] == "kimi", json.dumps(res)[:300])

    # (c) wire reports no prompt_tokens at all: the injection cannot be
    # attested, so the prose URLs are NOT sources and the envelope is
    # `ungrounded` with the prose withheld. (This once passed as
    # ok — "never a false refusal on a missing number" — which is exactly how
    # an unsearched answer reached a build spec on entity-9128 project 1.)
    rounds = [
        {"content": "", "prompt_tokens": None, "tool_calls": [_kimi_tool_call(8550)]},
        {"content": "An answer. Sources:\nhttps://example.org/a", "prompt_tokens": None},
    ]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route(q, route, system=wr.RESEARCH_SYSTEM))
    check("kimi: no prompt_tokens on the wire -> injection unprovable -> ungrounded, prose withheld",
          res["status"] == "ungrounded" and "An answer" not in json.dumps(res)
          and "example.org" not in json.dumps(res), json.dumps(res)[:200])

    # (d) borderline: injected well over half the claim -> passes.
    rounds = [
        {"content": "", "prompt_tokens": 100, "tool_calls": [_kimi_tool_call(1000)]},
        {"content": "ok https://x.y/z", "prompt_tokens": 100 + 700},
    ]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route(q, route, system=wr.RESEARCH_SYSTEM))
    check("kimi: 70% of the claim injected -> accepted (ratio floor is 0.5)", res["status"] == "ok")


# ── 1b. grounding: no provider citation -> no answer ──────────────────────
# entity-9128 project 1: the research run's first web_search answer began
# "I don't have live web access in this conversation" and, with a `Sources:`
# line the model typed, went through as status ok and into a spec the host
# presented as "verified against the official docs". A URL in the prose is
# not a citation. Now: no annotations -> `ungrounded`, prose withheld.
async def t_grounding():
    route = {"provider": "openrouter", "model": "x/y", "_role": "research"}
    prose = ("I don't have live web access in this conversation, but trading bot "
             "needs Python 3.9-3.12.\n\nSources:\nhttps://www.trading-bot.io/en/stable/")
    rounds = [{"content": prose, "prompt_tokens": 40}]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route("q", route))
    dumped = json.dumps(res)
    check("no annotations -> status ungrounded", res["status"] == "ungrounded", dumped[:300])
    check("ungrounded: the prose is withheld under EVERY key",
          "live web access" not in dumped and "Python 3.9" not in dumped, dumped[:300])
    check("ungrounded: a URL the model typed is not a source",
          "trading-bot.io" not in dumped, dumped[:300])
    check("ungrounded: result names query/provider/model/via + a reason that says why",
          res["result"].get("query") == "q" and res["result"].get("provider") == "openrouter"
          and str(res["result"].get("model")).startswith("x/y") and res["result"].get("via")
          and "citation" in res["result"].get("reason", ""), dumped[:300])
    # With annotations the same prose is a grounded answer.
    rounds = [{"content": prose, "prompt_tokens": 40,
               "annotations": [{"type": "url_citation", "url_citation": {
                   "url": "https://www.trading-bot.io/en/stable/installation/", "title": "Install"}}]}]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route("q", route))
    check("provider annotations -> ok with those sources (and only those)",
          res["status"] == "ok"
          and [s["url"] for s in res["result"]["sources"]]
          == ["https://www.trading-bot.io/en/stable/installation/"]
          and res["result"]["answer"] == prose, json.dumps(res)[:300])
    # The tool the host calls passes the shape through unchanged, and the
    # module-level reason helper reads it.
    check("failure_reason reads the ungrounded reason",
          "citation" in wr.failure_reason({"status": "ungrounded",
                                           "result": {"reason": "no citation"}}))
    # search() walks the candidate list and the LAST shape survives with the
    # combined sentence. research_routes yields at most ONE candidate since the
    # three-role cut (the host's own route, if its provider can search), so the
    # two-entry case is the walk itself under test, not a shape production can
    # still make — the stub says so rather than implying a research route that
    # no longer exists.
    async def _routes(pool, eid):
        return [{"provider": "openrouter", "model": "a/b", "_role": "chat"},
                {"provider": "openrouter", "model": "c/d", "_role": "chat"}]
    orig = wr.research_routes
    wr.research_routes = _routes
    try:
        rounds = [{"content": "memory answer", "prompt_tokens": 1},
                  {"content": "another memory answer", "prompt_tokens": 1}]
        res, fake = await _with_gateway(rounds, lambda: wr.search("q", pool=object(), entity_id=9003))
    finally:
        wr.research_routes = orig
    dumped = json.dumps(res)
    check("search(): ungrounded on every route -> ungrounded, both routes named, no prose",
          res["status"] == "ungrounded" and len(fake.calls) == 2
          and "chat role → openrouter/a/b" in res["result"]["reason"]
          and "chat role → openrouter/c/d" in res["result"]["reason"]
          and "memory answer" not in dumped, dumped[:400])
    from mastercode.runtime.tools import _exec_research
    orig2 = wr.search

    async def _fake_search(q, **kw):
        return {"status": "ungrounded", "result": {"query": q, "reason": "r"}}
    wr.search = _fake_search
    try:
        # research takes its links from the search: a search that
        # attested no source reaches the host as it came, never as pages.
        out = await _exec_research({"query": "q"}, entity_id=9003, pool=object(), thread_key="t")
    finally:
        wr.search = orig2
    check("tools.research hands the ungrounded envelope to the host unchanged",
          out.get("status") == "ungrounded" and "answer" not in (out.get("result") or {}))


# ── 2. a reasoning-only reply is named, not left bare ────────────────────
async def t_reasoning_only():
    route = {"provider": "openrouter", "model": "some/model", "_role": "chat"}
    rounds = [{"content": "", "reasoning": "thinking…", "prompt_tokens": 10}]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route("q", route))
    check("reasoning-only reply -> unavailable sentence names provider/model",
          res["status"] == "unavailable" and "openrouter/some/model" in res["result"]
          and "reasoning only" in res["result"], json.dumps(res)[:300])


# ── 3. xai + openai on the Responses API ────────────────────────────────
class _FakePost:
    def __init__(self, script):
        self.script = list(script)   # [(status, body_dict_or_text)]
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, endpoint, headers, body, total_timeout=180):
        self.calls.append((endpoint, json.loads(json.dumps(body))))
        status, payload = self.script.pop(0)
        return status, (payload if isinstance(payload, str) else json.dumps(payload))


def _responses_body(text, urls, model="grok-4.6", top_citations=None):
    body = {"model": model, "output": [{"type": "message", "content": [{
        "type": "output_text", "text": text,
        "annotations": [{"type": "url_citation", "url": u, "title": u} for u in urls]}]}]}
    if top_citations:
        body["citations"] = top_citations
    return body


async def _with_post(script, coro_fn):
    import server.vault.providers as prov
    from server import brain_service
    fake = _FakePost(script)
    orig_post, orig_cred, orig_pool = wr._post_json, prov.resolve_credential, brain_service.get_pool
    wr._post_json = fake

    async def _cred(pool, eid, provider, cred_type="api_key", **kw):
        return "sk-test"
    prov.resolve_credential = _cred
    brain_service.get_pool = lambda: None
    try:
        return await coro_fn(), fake
    finally:
        wr._post_json, prov.resolve_credential, brain_service.get_pool = orig_post, orig_cred, orig_pool


async def t_responses():
    # xai: one POST to /responses with the web_search tool; citations parsed.
    route = {"provider": "xai", "model": "grok-4.6", "_role": "research"}
    script = [(200, _responses_body("Python 3.14.7.", ["https://www.python.org/downloads/"],
                                    top_citations=["https://devguide.python.org/versions"]))]
    res, fake = await _with_post(script, lambda: wr.search_on_route("q", route))
    ep, body = fake.calls[0]
    check("xai: dials <base>/responses, not chat/completions",
          ep.endswith("/v1/responses") and "chat/completions" not in ep, ep)
    check("xai: sends tools=[{type: web_search}] (the Agent Tools API), no search_parameters",
          body.get("tools") == [{"type": "web_search"}] and "search_parameters" not in body,
          json.dumps(body)[:200])
    check("xai: ok envelope with url_citation + top-level citations folded in",
          res["status"] == "ok" and [s["url"] for s in res["result"]["sources"]]
          == ["https://www.python.org/downloads/", "https://devguide.python.org/versions"],
          json.dumps(res)[:300])
    check("xai: the model the wire served is reported (grok-4.6)", res["result"]["model"] == "grok-4.6")

    # xai: a 410 is surfaced, named, never retried as something else.
    script = [(410, '{"error":"Live search is deprecated."}')]
    res, fake = await _with_post(script, lambda: wr.search_on_route("q", route))
    check("xai: non-200 -> unavailable naming the status", res["status"] == "unavailable"
          and "xai 410" in res["result"], json.dumps(res)[:200])
    check("xai: no second tool name to fall back to -> exactly one POST", len(fake.calls) == 1)

    # openai: web_search 400 -> retried once as web_search_preview.
    route = {"provider": "openai", "model": "gpt-4o-mini", "_role": "research"}
    script = [(400, '{"error":{"message":"web_search is not supported"}}'),
              (200, _responses_body("ans", ["https://a.b/c"], model="gpt-4o-mini-2024-07-18"))]
    res, fake = await _with_post(script, lambda: wr.search_on_route("q", route))
    check("openai: 400 on web_search -> retried as web_search_preview, then ok",
          res["status"] == "ok" and len(fake.calls) == 2
          and fake.calls[0][1]["tools"] == [{"type": "web_search"}]
          and fake.calls[1][1]["tools"] == [{"type": "web_search_preview"}], json.dumps(res)[:200])
    check("openai: endpoint is the OpenAI base + /responses",
          fake.calls[0][0] == "https://api.openai.com/v1/responses", fake.calls[0][0])

    # _parse_responses_output: empty text -> empty content (caller -> unavailable)
    content, anns = wr._parse_responses_output({"output": [{"type": "reasoning"}]})
    check("responses parser: no message items -> empty content, no annotations",
          content == "" and anns == [])


# ── 4. search(): every failing route is named, in order ──────────────────
async def t_search_all_fail():
    async def _routes(pool, eid):
        return [{"provider": "kimi", "model": "k3", "_role": "chat"},
                {"provider": "openrouter", "model": "x/y", "_role": "chat"}]
    orig = wr.research_routes
    wr.research_routes = _routes
    try:
        rounds = [
            {"content": "", "prompt_tokens": 252, "tool_calls": [_kimi_tool_call(8550)]},
            {"content": "", "prompt_tokens": 410},
            {"content": "", "error": "openrouter 429: rate limited"},
        ]
        res, _ = await _with_gateway(rounds, lambda: wr.search(
            "q", pool=object(), entity_id=9003))
    finally:
        wr.research_routes = orig
    check("search(): all routes fail -> one sentence listing each route and its reason, in order",
          res["status"] == "unavailable" and res["result"].startswith("No route could search. ")
          and res["result"].index("chat role → kimi/k3") < res["result"].index("chat role → openrouter/x/y")
          and "429" in res["result"], res["result"][:400])


# ── 5. the skill files + entrypoint: no SDK claim reaches the host ───────
def t_skills():
    ds = _ROOT / "default-skills"
    check("default-skills/claude-sdk.md is gone (renamed)", not (ds / "claude-sdk.md").exists())
    cc = (ds / "claude-code.md").read_text() if (ds / "claude-code.md").exists() else ""
    check("default-skills/claude-code.md exists with name: claude-code",
          bool(cc) and re.search(r"^name:\s*claude-code\s*$", cc, re.M) is not None)
    m = re.search(r"^allowed-tools:\s*(.*)$", cc, re.M)
    tools = [t.strip() for t in (m.group(1).split(",") if m else [])]
    check("claude-code.md does not claim the research tool", "research" not in tools, str(tools))
    check("claude-code.md says web research is not this harness's job",
          "Web research is NOT this harness's job" in cc)
    # No harness skill (gated on an app) may list research/web_search: those
    # are host tools on the routed model, and listing them under a harness is
    # exactly how "research needs the Claude SDK" was produced.
    bad = []
    for f in sorted(ds.glob("*.md")):
        text = f.read_text()
        gate = re.search(r"^present_if_app_installed:\s*(\S+)", text, re.M)
        if not gate or gate.group(1) == "always":
            continue
        mm = re.search(r"^allowed-tools:\s*(.*)$", text, re.M)
        tl = [t.strip() for t in (mm.group(1).split(",") if mm else [])]
        if "research" in tl or "web_search" in tl:
            bad.append(f.name)
    check("no app-gated harness skill lists research/web_search as its tool", not bad, str(bad))
    rs = (ds / "research.md").read_text()
    check("research.md names the real tools (research, web_search, read_url), not WebSearch/WebFetch",
          {"research", "web_search", "read_url"} <= set(
              t.strip() for t in (re.search(r"^allowed-tools:\s*(.*)$", rs, re.M) or [None, ""])[1].split(","))
          and "WebFetch" not in rs and "WebSearch" not in rs)
    check("research.md tells the host to relay a failure, not guess its cause",
          "A failure is a fact to relay" in rs)
    ep = (_ROOT / "entrypoint.sh").read_text()
    # Assert the ENTRY, not the whole line. This pinned the entire string, so
    # registering a second rename (freeagent.md -> bastion-agent.md)
    # red-failed a gate whose subject was untouched. The thing worth protecting
    # is that each rename stays registered; the list is meant to grow.
    renames = re.search(r'^SKILL_RENAMES="([^"]*)"', ep, re.M)
    renames = renames.group(1).split() if renames else []
    check("entrypoint.sh renames the legacy claude-sdk.md on existing pods",
          "claude-sdk.md:claude-code.md" in renames)
    # Dropping this one would strand a stale freeagent.md on every existing pod
    # forever — seeding never overwrites, so nothing else would ever replace it.
    check("entrypoint.sh renames freeagent.md to bastion-agent.md",
          "freeagent.md:bastion-agent.md" in renames)
    # entrypoint.sh IS the image ENTRYPOINT: if it does not parse, the container
    # never starts and every gate after it fails for a confusing reason. Nothing
    # in this repo ran a shell syntax check on it (0 hits for
    # `bash -n`). This also closes the one case the membership test above cannot
    # see — an unterminated quote — without giving up multi-line reflow support.
    import subprocess as _sp
    check("entrypoint.sh parses as shell (bash -n)",
          _sp.run(["bash", "-n", str(_ROOT / "entrypoint.sh")],
                  capture_output=True).returncode == 0)
    # A rename whose TARGET default does not exist would move a pod's file to a
    # name nothing ever refreshes. claude-code.md has this check already; the
    # entry added for the bastion-agent rename needs the same.
    check("default-skills/bastion-agent.md exists (the rename target)",
          (ds / "bastion-agent.md").is_file())
    # Entries, not the whole line, for the reason given for SKILL_RENAMES above.
    retired = re.search(r'^SKILL_RETIRED="([^"]*)"', ep, re.M)
    retired = retired.group(1).split() if retired else []
    check("entrypoint.sh retires the orphan copilot.md (harness retired 2026-08-21)",
          "copilot.md" in retired)
    # coding-tutor.md was seeded at install by skill_seeding (gated on the
    # vscode app, image-baked on every Host). The editor is gone, and
    # nothing unseeds a gated skill at uninstall, while skill_loader.list_skills
    # serves every .md in the skills dir: without this entry every Host keeps a
    # skill that sends it to an app and an `editor` tool that no longer exist.
    check("entrypoint.sh retires the orphan coding-tutor.md (VS Code editor removed 2026-10-11)",
          "coding-tutor.md" in retired, str(retired))
    # The loop skips a name the image still ships, so a retired name must be
    # gone from default-skills or the retirement never happens.
    check("every retired skill name is absent from default-skills",
          not [n for n in retired if (ds / n).exists()],
          str([n for n in retired if (ds / n).exists()]))
    # The refresh loop must no longer `continue` past every gated skill: the
    # gate-skip has to sit INSIDE the absent-dest branch.
    seed_block = ep[ep.index("Seed default skill files"):ep.index("Embeddings — bridge-hosted")]
    check("entrypoint.sh refreshes PRESENT gated skills (gate-skip only when dest is absent)",
          "if [ ! -e \"$dest\" ]; then\n            if [ -n \"$gate\" ]" in seed_block)
    # Static: the kimi dialer must check injection and the xai dialer must
    # not send Live Search.
    src = (_ROOT / "brain/mastercode/runtime/web_research.py").read_text()
    check("web_research: xai no longer sends search_parameters (Live Search, 410)",
          'extra_body={"search_parameters"' not in src
          and "_search_responses_api(\n        \"xai\"" in src)
    check("web_research: kimi dialer checks prompt growth against the claimed search usage",
          "KIMI_INJECTION_MIN_RATIO" in src and "_search_usage_tokens" in src)



# ── provider lanes ───────────────────────────────────────────────────────
async def t_provider_audit():
    # OpenRouter failover: the gateway dials fallback_model verbatim, so the
    # fallback must carry the web plugin suffix too or it never searches.
    route = {"provider": "openrouter", "model": "a/b",
             "fallback_provider": "openrouter", "fallback_model": "c/d"}
    rounds = [{"content": "x", "annotations": [{"url_citation": {"url": "https://e.x/"}}]}]
    _, fake = await _with_gateway(rounds, lambda: wr.search_on_route("q", route))
    kw = fake.calls[0]
    check("openrouter: the failover model is dialed with :online too",
          kw.get("fallback_model") == "c/d:online", str(kw.get("fallback_model")))
    check("openrouter: the primary model is dialed with :online",
          kw.get("provider_model") == "a/b:online", str(kw.get("provider_model")))
    # The research role is gone: no failure sentence may tell
    # the user to route it.
    check("ungrounded reason does not name the removed research role",
          "research role" not in wr.UNGROUNDED_REASON
          and "route research" not in open(wr.__file__).read(),
          wr.UNGROUNDED_REASON)


async def t_provider_audit_2():
    # Provider lanes, continued.
    q = "q"
    # A failed Responses-API answer says the provider's own reason.
    route = {"provider": "xai", "model": "grok-4.6"}
    res, _ = await _with_post([(200, {"status": "failed", "error": {"message": "upstream overloaded"}})],
                              lambda: wr.search_on_route(q, route))
    check("responses: a failed response names provider/model and the provider's reason",
          res["status"] == "unavailable" and "xai/grok-4.6" in res["result"]
          and "upstream overloaded" in res["result"] and "empty response" not in res["result"],
          json.dumps(res)[:300])
    # Every failure sentence names provider/model and a step.
    res, _ = await _with_post([(500, "boom")], lambda: wr.search_on_route(q, route))
    check("a raw dialer failure names provider/model and what to do",
          "xai/grok-4.6" in res["result"] and "Orchestrator" in res["result"], res["result"])
    # An incomplete answer stopped by a content filter says so.
    body = _responses_body("partial answer", ["https://a.b/c"])
    body.update(status="incomplete", incomplete_details={"reason": "content_filter"})
    res, _ = await _with_post([(200, body)], lambda: wr.search_on_route(q, route))
    check("responses: an answer a content filter stopped is truncated and says why",
          res["status"] == "ok" and res["result"].get("truncated") is True
          and "content filter" in res["result"].get("note", ""), json.dumps(res)[:300])
    # Anthropic pause_turn: the answer is marked as stopped early.
    route = {"provider": "anthropic", "model": "claude-x"}
    rounds = [{"content": "What I found so far.", "stop_reason": "pause_turn",
               "annotations": [{"url_citation": {"url": "https://a.b/p"}}]}]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route(q, route))
    check("anthropic: a paused search turn is marked stopped early, never finished",
          res["status"] == "ok" and res["result"].get("truncated") is True
          and "paused" in res["result"].get("note", ""), json.dumps(res)[:300])
    # Kimi: rounds used up while still searching is not an answer.
    route = {"provider": "kimi", "model": "k3"}
    rounds, pt = [], 200
    for _ in range(getattr(wr, "KIMI_MAX_ROUNDS", 5)):
        rounds.append({"content": "Searching https://guess.example/x", "prompt_tokens": pt,
                       "tool_calls": [_kimi_tool_call(1000)]})
        pt += 1200
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route(q, route))
    check("kimi: still searching when the rounds run out -> unavailable, never an answer",
          res["status"] == "unavailable" and "still searching" in res["result"], json.dumps(res)[:300])
    # Kimi: links read out of the answer text are marked and noted.
    rounds = [
        {"content": "", "prompt_tokens": 252, "tool_calls": [_kimi_tool_call(8550)]},
        {"content": "Python 3.14.7. https://www.python.org/downloads/", "prompt_tokens": 252 + 8550 + 160},
    ]
    res, _ = await _with_gateway(rounds, lambda: wr.search_on_route(q, route))
    check("kimi: sources taken from the answer text say so, and the note tells the reader",
          res["status"] == "ok" and res["result"]["sources"][0].get("from") == "answer text"
          and "answer text" in res["result"].get("note", ""), json.dumps(res)[:300])


async def t_post_json_stream():
    # _post_json against a local event stream: an error event is a failure,
    # and a stream with no final event is folded and logged.
    from aiohttp import web
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    events = {}

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for ev in events["list"]:
            await resp.write(("data: " + json.dumps(ev) + "\n\n").encode())
        await resp.write_eof()
        return resp
    app = web.Application()
    app.router.add_post("/responses", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        events["list"] = [{"type": "response.output_text.delta", "delta": "half"},
                          {"type": "error", "code": "server_error", "message": "stream broke"}]
        st, text = await wr._post_json(f"http://127.0.0.1:{port}/responses", {}, {})
        data = json.loads(text)
        check("stream: an error event comes back as a failed response with its message",
              st == 200 and data.get("status") == "failed"
              and data.get("error", {}).get("message") == "stream broke", text[:200])
        events["list"] = [{"type": "response.output_text.delta", "delta": "whole answer"}]
        st, text = await wr._post_json(f"http://127.0.0.1:{port}/responses", {}, {})
        check("stream: no final event -> the text is folded in, status unknown",
              st == 200 and wr._parse_responses_output(json.loads(text))[0] == "whole answer"
              and "status" not in json.loads(text), text[:200])
    finally:
        await runner.cleanup()


async def t_gateway_idle():
    # Every search dialer asks for the idle limit, and the gateway applies it:
    # a search call that goes silent fails after it, never waits on the
    # turn's own idle timer (30 minutes).
    import time as _time
    from aiohttp import web
    import server.vault.providers as provs
    from server import brain_service
    for route, rounds in (({"provider": "openrouter", "model": "a/b"}, [{"content": "x"}]),
                          ({"provider": "anthropic", "model": "c"}, [{"content": "x"}]),
                          ({"provider": "kimi", "model": "k3"}, [{"content": "x"}])):
        _, fake = await _with_gateway(rounds, lambda: wr.search_on_route("q", route))
        check(f"{route['provider']}: the search call asks for the idle limit",
              fake.calls[0].get("idle_s") == wr.WEB_SEARCH_IDLE_S, str(fake.calls[0].get("idle_s")))
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    async def stall(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await asyncio.sleep(6)
        return resp

    async def buffered(request):
        # A buffered answer still being written: no byte, then all of it.
        await asyncio.sleep(2)
        return web.json_response({"content": [{"type": "text", "text": "slow answer"}],
                                  "stop_reason": "end_turn", "usage": {}})
    app = web.Application()
    app.router.add_post("/v1/messages", stall)
    app.router.add_post("/slow/v1/messages", buffered)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class _Cred:
        secret, source, cred_type, kind = "sk-test", "api_key", "api_key", "api_key"

    async def _resolve(*a, **k):
        return _Cred()
    orig_resolve, orig_pool = provs.resolve, brain_service.get_pool
    provs.resolve, brain_service.get_pool = _resolve, (lambda: None)
    t0 = _time.monotonic()
    try:
        out = await asyncio.wait_for(pg._call_anthropic(
            messages=[{"role": "user", "content": "q"}], model="claude-x",
            max_tokens=pg._STREAM_ABOVE + 1,
            base_url=f"http://127.0.0.1:{port}", idle_s=1), timeout=15)
    except Exception as e:  # noqa: BLE001
        out = {"raised": repr(e)}
    took = _time.monotonic() - t0
    try:
        slow = await asyncio.wait_for(pg._call_anthropic(
            messages=[{"role": "user", "content": "q"}], model="claude-x", max_tokens=100,
            base_url=f"http://127.0.0.1:{port}/slow", idle_s=1), timeout=15)
    except Exception as e:  # noqa: BLE001
        slow = {"raised": repr(e)}
    finally:
        provs.resolve, brain_service.get_pool = orig_resolve, orig_pool
        await runner.cleanup()
    check("gateway: a call that goes silent fails at its idle limit, as an error result",
          isinstance(out, dict) and out.get("error") and took < 10, f"{out} after {took:.1f}s")
    check("gateway: a buffered call still being written is not cut at the idle limit",
          isinstance(slow, dict) and slow.get("content") == "slow answer", str(slow))


async def t_pause_no_text():
    # Anthropic paused the search before any text: the gateway keeps the stop
    # reason on its error, and the search says it was paused.
    from aiohttp import web
    import server.vault.providers as provs
    from server import brain_service
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    async def paused(request):
        return web.json_response({"id": "m", "type": "message", "role": "assistant", "model": "claude-x",
                                  "content": [], "stop_reason": "pause_turn",
                                  "usage": {"input_tokens": 5, "output_tokens": 0}})
    app = web.Application()
    app.router.add_post("/v1/messages", paused)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class _Cred:
        secret, source, cred_type, kind = "sk-test", "api_key", "api_key", "api_key"

    async def _resolve(*a, **k):
        return _Cred()
    orig_resolve, orig_pool = provs.resolve, brain_service.get_pool
    provs.resolve, brain_service.get_pool = _resolve, (lambda: None)
    try:
        out = await pg._call_anthropic(messages=[{"role": "user", "content": "q"}], model="claude-x",
                                       max_tokens=100, base_url=f"http://127.0.0.1:{port}")
    finally:
        provs.resolve, brain_service.get_pool = orig_resolve, orig_pool
        await runner.cleanup()
    check("gateway: an empty paused answer keeps its stop reason on the error",
          isinstance(out, dict) and out.get("error") and out.get("stop_reason") == "pause_turn", str(out))
    _, = [None]
    res, _f = await _with_gateway([out], lambda: wr.search_on_route("q", {"provider": "anthropic", "model": "claude-x"}))
    check("anthropic: a search paused before any text is unavailable and says it was paused",
          res["status"] == "unavailable" and "paused the search" in res["result"], json.dumps(res)[:300])


async def t_reader_fallback_trickle():
    # url_reader's fallback fetcher reads a body as long as bytes keep
    # arriving, as the curl_cffi fetcher does (stream=True: connect + curl's
    # low-speed limit, no total): a slow page longer than the limit is read
    # whole, never cut at a wall clock.
    import time as _time
    from aiohttp import web
    from mastercode.runtime import url_reader as ur
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    async def trickle(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/html"})
        await resp.prepare(request)
        for i in range(8):
            await resp.write(f"<p>part {i}</p>".encode())
            await asyncio.sleep(0.5)
        return resp
    app = web.Application()
    app.router.add_get("/slow", trickle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    saved = ur._FALLBACK_TIMEOUT_S
    ur._FALLBACK_TIMEOUT_S = 2
    try:
        out = await ur._fetch_aiohttp(f"http://127.0.0.1:{port}/slow")
    except Exception as e:  # noqa: BLE001
        out = {"raised": repr(e)}
    finally:
        ur._FALLBACK_TIMEOUT_S = saved
        await runner.cleanup()
    check("reader fallback: a page still arriving past the limit is read whole",
          "part 7" in (out.get("body") or ""), str(out)[:200])

async def main() -> int:
    await t_kimi()
    await t_grounding()
    await t_reasoning_only()
    await t_responses()
    await t_search_all_fail()
    await t_provider_audit()
    await t_provider_audit_2()
    await t_post_json_stream()
    await t_gateway_idle()
    await t_pause_no_text()
    await t_reader_fallback_trickle()
    t_skills()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} gate(s) FAILED: {_FAILURES}")
        return 1
    print("\nweb-research dialers: all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
