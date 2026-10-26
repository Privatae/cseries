#!/usr/bin/env python3
"""test_image_lane_egress_and_recommendation.py — the two image-lane defects
read off a Host's Circles thread on 2026-09-20, each pinned by driving the
code rather than grepping it.

1. EGRESS. An entity pod has no direct route to the internet and no DNS for
   it; every outbound call rides the node-local proxy named in HTTPS_PROXY,
   which aiohttp honours only when the session is built with trust_env=True.
   image_engine built both of its sessions without it, so "generate a picture
   of a horse" died with "Cannot connect to host openrouter.ai:443 ...
   Temporary failure in name resolution" before the user's image model was
   reached, and vision (`understand`, removed 2026-10-12) had the same hole. Measured in the pod:
   `curl --noproxy '*' https://openrouter.ai` → could not resolve host;
   the same URL through the env proxy → 200. The engine's path is driven
   here against a recording aiohttp stand-in, so a session built the old way
   fails this gate instead of failing the user. A picture from the web that
   the Host looks at (action='look') is fetched by url_reader.fetch_bytes,
   pinned below to the same proxy and url_reader's internal-host rules.

2. RECOMMENDATION. `model_routing op='recommend'` for an image role walked
   the keyed providers in sorted order and took the first provider with any
   rows. get_capable_models returns a provider's WHOLE catalog, flagged
   capability_published=False, when that provider states no modality data —
   a third state the picker renders as "unknown" — and the recommender read
   that list as "capable". deepinfra sorts before openrouter, so the host was
   told (as a fix ready to apply) to route image_gen to
   Audio8/Audio8-TTS-Preview-0.6b, a text-to-speech model, while a documented
   Nano Banana route on openrouter went unoffered. The picker now skips
   unpublished providers and ranks across providers by how well a pick is
   justified (doc-grounded preference > the already-routed chat model > first
   row). The diagnostics block's half of the same fix is in
   test_host_diagnostics.py.

Container-free: APP_ROOT=<repo> python3 scripts/<this>.py. No pod, no DB,
no network.
"""
from __future__ import annotations

import asyncio
import os
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


# ── 1. egress: both engine sessions ride the proxy ───────────────────────
print("== 1. image_engine sessions honour the pod's egress proxy ==")
from mastercode import image_engine  # noqa: E402

_sessions: list[dict] = []


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class _Session:
    """Records the kwargs the engine built it with; answers every POST."""
    def __init__(self, **kw):
        _sessions.append(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, **kw):
        if url.endswith("/images/generations"):
            return _Resp({"data": [{"url": "https://cdn.example/horse.png", "revised_prompt": "a horse"}]})
        return _Resp({"choices": [{"message": {"content": "a horse in a meadow"}}]})


class _Timeout:
    def __init__(self, **kw):
        pass


class _Pool:
    async def execute(self, *a, **k):
        return None

    async def fetchrow(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []


async def _route(context, role):
    return {"base": "https://openrouter.ai/api/v1", "model": "google/gemini-3-pro-image", "key": "k"}


image_engine.aiohttp = types.SimpleNamespace(ClientSession=_Session, ClientTimeout=_Timeout)
image_engine._resolve_image_route = _route

gen = asyncio.run(image_engine.run(image_engine.EngineContext(
    3, _Pool(), args={"action": "generate", "prompt": "a chestnut horse"})))
check("generate reaches the endpoint and returns the image",
      gen.get("status") == "ok" and gen.get("count") == 1
      and gen["images"][0]["url"].endswith("horse.png"), str(gen)[:200])
check("generate builds its session with trust_env=True (the proxy is the only way out)",
      len(_sessions) == 1 and _sessions[0].get("trust_env") is True, str(_sessions))

_ur = (ROOT / "brain/mastercode/runtime/url_reader.py").read_text(encoding="utf-8")
_fb = _ur[_ur.index("async def fetch_bytes("):]
# fetch_bytes opens each hop's session with _aiohttp_session, which keeps
# trust_env=True either way (with no proxy it adds only the checked resolver:
# scripts/test_url_reader_internal_names.py).
_as = _ur[_ur.index("def _aiohttp_session("):_ur.index("def _time_left(")]
check("a web picture for look is fetched on the proxy (url_reader.fetch_bytes, trust_env=True)",
      "_aiohttp_session(cur)" in _fb and "normalize_url(url)" in _fb
      and "_next_hop(" in _fb and _as.count("aiohttp.ClientSession(trust_env=True") == 2,
      _fb[:200])

# A route pinned to a PRIVATE address (the user's own machine, loopback) is
# dialled direct: NO_PROXY cannot name a CIDR and the node proxy refuses
# private destinations, so trust_env there would turn a working local model
# into a 403 (review of this branch, F7).
for _priv in ("http://192.168.1.20:11434/v1", "http://127.0.0.1:8080/v1",
              "http://[fd12:3456:789a::16]:11434/v1", "http://10.1.2.3:8000",
              "http://100.101.102.103:11434/v1"):
    check(f"a private/loopback endpoint is dialled direct: {_priv}",
          image_engine._session_kwargs(_priv) == {"trust_env": False},
          str(image_engine._session_kwargs(_priv)))
for _pub in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1",
             "http://ollama.svc.cluster.local:11434", "http://[2a01:4f8::1]:443", None):
    check(f"a public or named endpoint rides the proxy (NO_PROXY still applies to names): {_pub}",
          image_engine._session_kwargs(_pub) == {"trust_env": True},
          str(image_engine._session_kwargs(_pub)))

_sessions.clear()
image_engine._resolve_image_route = (
    lambda context, role: _async_route({"base": "http://192.168.1.20:11434/v1", "model": "sdxl", "key": "k"}))


async def _async_route(v):
    return v

loc = asyncio.run(image_engine.run(image_engine.EngineContext(
    3, _Pool(), args={"action": "generate", "prompt": "a horse"})))
check("generate against a private endpoint builds a direct session",
      loc.get("status") == "ok" and _sessions == [{"trust_env": False}], f"{loc.get('status')} {_sessions}")

# Source-level ratchet as well, the way test_github_tool.py pins api_get: a
# refactor that opens a third session in this file without the per-endpoint
# decision must fail here, not in a user's chat.
_src = (ROOT / "brain/mastercode/image_engine.py").read_text(encoding="utf-8")
_opened = _src.count("aiohttp.ClientSession(")
_decided = _src.count("aiohttp.ClientSession(**_session_kwargs(")
check("every aiohttp session image_engine opens is built through _session_kwargs",
      _opened >= 1 and _opened == _decided, f"opened={_opened} decided={_decided}")

# ── 2. recommendation: unknown is not able; justified beats alphabetical ──
print()
print("== 2. image-role recommendation across keyed providers ==")
from mastercode.runtime.tools import _pick_image_recommendation, _preferred_image_model  # noqa: E402

deepinfra_all = {"capability_published": False, "models": [
    {"id": "Audio8/Audio8-TTS-Preview-0.6b", "name": "Audio8 TTS"},
    {"id": "black-forest-labs/FLUX-2", "name": "FLUX 2"}]}
openrouter_ok = {"capability_published": True, "models": [
    {"id": "openai/gpt-image-2", "name": "GPT Image 2"},
    {"id": "google/gemini-3.1-flash-image", "name": "Nano Banana 2"}]}
prefs = {"openrouter": [{"model": "google/gemini-3.1-flash-image", "why": "Nano Banana 2, via OpenRouter"}]}

pick, skipped = _pick_image_recommendation(
    "image_gen", ["deepinfra", "openrouter"],
    {"deepinfra": deepinfra_all, "openrouter": openrouter_ok}, preferences=prefs)
check("a provider that publishes no capability data is skipped, and named as skipped",
      skipped == ["deepinfra"], f"skipped={skipped}")
check("the documented route wins over the alphabetically-first provider's first row",
      pick == {"provider": "openrouter", "model": "google/gemini-3.1-flash-image",
               "why": "Nano Banana 2, via OpenRouter", "capability_published": True,
               "documented": True}, str(pick))
check("a TTS model is never the recommendation for image_gen",
      pick is not None and "Audio8" not in pick["model"])

only_unpub, sk2 = _pick_image_recommendation(
    "image_gen", ["deepinfra"], {"deepinfra": deepinfra_all}, preferences=prefs)
check("only unpublished providers on hand → no recommendation, provider named",
      only_unpub is None and sk2 == ["deepinfra"], f"{only_unpub} {sk2}")

# An unpublished provider with a DOCUMENTED model that its catalog serves is
# still recommendable — the doc named a working id (review of this branch, F6).
doc_unpub, sk3 = _pick_image_recommendation(
    "image_gen", ["deepinfra"], {"deepinfra": deepinfra_all},
    preferences={"deepinfra": [{"model": "black-forest-labs/FLUX-2", "why": "documented"}]})
check("a documented model on an unpublished provider is offered, and the provider is not 'skipped'",
      doc_unpub is not None and doc_unpub["model"] == "black-forest-labs/FLUX-2"
      and doc_unpub["why"] == "documented" and sk3 == [], f"{doc_unpub} {sk3}")
check("that envelope tells the truth about the catalog: unpublished, but documented",
      doc_unpub is not None and doc_unpub["capability_published"] is False
      and doc_unpub["documented"] is True, str(doc_unpub))
check("but the already-routed chat model on an unpublished provider is NOT enough (its modality is unknown)",
      _pick_image_recommendation("image_gen", ["deepinfra"], {"deepinfra": deepinfra_all},
                                 already_routed="Audio8/Audio8-TTS-Preview-0.6b") == (None, ["deepinfra"]))

# Rank across providers: the already-routed chat model on a LATER provider
# beats an arbitrary first row on an earlier one. (These ran on a 'vision'
# role until 2026-10-12; the ranking does not depend on the role's name, and
# image_gen is the one image role left.)
anthropic_vis = {"capability_published": True, "models": [
    {"id": "claude-opus-5", "name": "Opus 5"}, {"id": "claude-sonnet-5", "name": "Sonnet 5"}]}
openrouter_vis = {"capability_published": True, "models": [
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek"}]}
pick_v, _ = _pick_image_recommendation(
    "image_gen", ["anthropic", "openrouter"],
    {"anthropic": anthropic_vis, "openrouter": openrouter_vis},
    preferences={}, already_routed="deepseek/deepseek-v4.1-flash")
check("the already-routed chat model beats an earlier provider's first row",
      pick_v is not None and pick_v["provider"] == "openrouter"
      and pick_v["model"] == "deepseek/deepseek-v4.1-flash"
      and "already answering your chat" in pick_v["why"], str(pick_v))

pick_f, _ = _pick_image_recommendation(
    "image_gen", ["anthropic", "openrouter"],
    {"anthropic": anthropic_vis, "openrouter": openrouter_vis}, preferences={})
check("with nothing better justified, the first published row is offered with no claim",
      pick_f == {"provider": "anthropic", "model": "claude-opus-5", "why": "",
                 "capability_published": True, "documented": False}, str(pick_f))
check("the recommendation never leaks the internal rank",
      all("_rank" not in (p or {}) for p in (pick, pick_v, pick_f)))

# The per-provider helper still answers alone (its other caller shape).
row = _preferred_image_model("image_gen", "openrouter", openrouter_ok["models"],
                             preference=prefs["openrouter"])
check("_preferred_image_model keeps returning the matched catalog row",
      row is not None and row["id"] == "google/gemini-3.1-flash-image" and row.get("_rank") == 0, str(row))
check("an empty catalog yields no pick",
      _preferred_image_model("image_gen", "x", []) is None
      and _pick_image_recommendation("image_gen", ["x"], {"x": {"models": []}})[0] is None)

# ── 3. the tool op itself, end to end over stubbed readers ───────────────
# The wiring that fetches a pane per keyed provider and turns a skip into a
# note is what the diagnostics block reads; the pure picker alone cannot
# prove it. server.* is stubbed at its import seam (the imports are inside
# the function), the recommended-routes.json is the real file.
print()
print("== 3. model_routing op='recommend' end to end ==")
import mastercode.runtime.tools as tools_mod  # noqa: E402

_calls: list[tuple] = []


async def _get_current_routing():
    return {"effective": {"chat": {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}},
            "providers": [{"id": "deepinfra", "has_key": True}, {"id": "openrouter", "has_key": True},
                          {"id": "local", "has_key": True}, {"id": "openai", "has_key": False}]}


async def _get_capable_models(role, provider=None):
    _calls.append((role, provider))
    pane = {"deepinfra": deepinfra_all, "openrouter": openrouter_ok}.get(provider) or {"models": []}
    return {"role": role, "providers": {provider: pane}}


_inf = types.SimpleNamespace(ROLES=("chat", "image_gen"),
                             get_current_routing=_get_current_routing,
                             get_capable_models=_get_capable_models)
_bs = types.SimpleNamespace(get_pool=lambda: object(), _entity_id=9003)
_prov = types.SimpleNamespace(PROVIDERS={"deepinfra": {}, "openrouter": {}, "openai": {}, "local": {}})
_server = types.ModuleType("server")
_server.brain_service = _bs
_routes = types.ModuleType("server.routes")
_routes.inference = _inf
_vault = types.ModuleType("server.vault")
_vault.providers = _prov
for _name, _mod in (("server", _server), ("server.brain_service", _bs), ("server.routes", _routes),
                    ("server.routes.inference", _inf), ("server.vault", _vault),
                    ("server.vault.providers", _prov)):
    sys.modules[_name] = _mod

rec = asyncio.run(tools_mod._exec_model_routing({"op": "recommend"}))
res = rec.get("result") or {}
check("the op answers ok with the unrouted image roles named",
      rec.get("status") == "ok" and set(res.get("unrouted") or []) >= {"image_gen"}, str(rec)[:300])
check("image_gen is recommended from the documented openrouter route, not deepinfra's first row",
      (res.get("recommendations") or {}).get("image_gen", {}).get("provider") == "openrouter"
      and (res.get("recommendations") or {}).get("image_gen", {}).get("model") == "google/gemini-3.1-flash-image",
      str(res.get("recommendations")))
# No vision recommendation and no vision pane fetched (2026-10-12): the Host
# model reads images itself, so there is no second model to recommend.
check("no vision recommendation is made and no vision pane is fetched",
      "vision" not in (res.get("recommendations") or {})
      and all(r != "vision" for r, _ in _calls), str(res.get("recommendations")))
check("the skipped provider is explained in notes, per role",
      any("deepinfra" in n and "image_gen" in n for n in res.get("notes") or []), str(res.get("notes")))
check("the local provider is never asked for a catalog pane (it is not a Keys provider)",
      all(p != "local" for _, p in _calls), str(_calls))

print()
if failures:
    print(f"FAIL {len(failures)}: {failures}")
    sys.exit(1)
print("PASS image lane: egress rides the proxy; unknown capability is never a recommendation")
