"""The Host model standard (runtime/model_standard.py, approved_models.json).

Proven here against the shipped data file and temporary ones:
  1. The shipped file parses, ships enforce=false, pins the floor to an index
     version, and every 'eligible' entry names a system card and clears the
     floor at its stated min_effort.
  2. One model, one identity: every provider spelling of the same weights
     resolves to the same canonical id, and a spelling that cannot be proven
     (local tag, quantisation, fine-tune, unknown variant) resolves to None.
  3. The verdict combines tests 1–2 (this file) with test 3 (the bench list):
     eligible alone is not approved; effort is judged as effective_effort()
     — the stored effort or the "low" the platform sends, on providers whose
     calls carry one; not judged where the platform cannot set it — and the
     writer and the gateway reach the SAME verdict for the same route.
  4. refusal() is None whenever the standard is not enforced, and names the
     reason when it is; a missing or broken data file fails OPEN.
  5. Behaviour at the two enforcement points: with enforce off call_model
     dials exactly as before; with it on an unapproved primary is refused
     without dialling, an unapproved fallback is skipped, the bench is exempt,
     and apply_role_route answers 409 before writing anything.

Run: pytest tests/test_model_standard.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from mastercode.runtime import model_standard as MS  # noqa: E402
from mastercode.runtime import model_validation as MV  # noqa: E402


def test_shipped_file_is_honest():
    p = _REPO / MS.DATA_FILE
    assert p.parent == pathlib.Path(MS.__file__).resolve().parent
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["enforce"] is False, "enforcement is flipped by a reviewed change, never by default"
    floor = doc["floor"]
    assert floor["index_version"] and floor["score"] > 0
    seen = set()
    for e in doc["models"]:
        c = e["canonical"]
        assert c == c.lower() and c.count("/") == 1, c
        assert c not in seen, f"duplicate entry {c}"
        seen.add(c)
        assert e["status"] in {"eligible", "pending_card", "no_card", "below_floor"}, e
        if e["status"] == "eligible":
            assert e["card"], f"{c}: eligible needs a system card"
            assert e["min_effort"] in MS._EFFORT_RANK, e
            assert e["aa_score_at_min"] >= floor["score"], f"{c}: below the floor at min_effort"
        if e["status"] == "below_floor":
            assert e["aa_score"] < floor["score"], e
    img_seen = set()
    for e in doc.get("image_models") or []:
        c = e["canonical"]
        assert c == c.lower() and c.count("/") == 1 and c not in img_seen, c
        img_seen.add(c)
        assert e["status"] in {"eligible", "pending_card", "pending_review", "excluded"}, e
        if e["status"] == "eligible":
            assert e["card"], f"{c}: an eligible image model needs a card"
        assert e.get("note"), f"{c}: every image entry says why"
    for prov, table in doc["aliases"].items():
        for k, v in table.items():
            assert v in seen, f"alias {prov}:{k} points at unlisted {v}"
    assert MS.load(force=True)["enforce"] is False
    assert MS.enforced() is False


@pytest.fixture
def datafile(tmp_path, monkeypatch):
    shipped = json.loads((_REPO / MS.DATA_FILE).read_text(encoding="utf-8"))
    p = tmp_path / "approved_models.json"

    def _write(**over):
        doc = {**shipped, **over}
        p.write_text(json.dumps(doc), encoding="utf-8")
        MS.load(force=True)
        return p
    monkeypatch.setattr(MS, "_path", lambda: p)
    _write()
    yield _write
    MS.load(force=True)


@pytest.mark.parametrize("provider,model,want", [
    # Anthropic direct, via OpenRouter (coerced), dotted, aliased "latest".
    ("anthropic", "claude-opus-5-5", "anthropic/claude-opus-5-5"),
    ("openrouter", "anthropic/claude-opus-5.5", "anthropic/claude-opus-5-5"),
    ("openrouter", "anthropic/claude-opus-5.5:batch", "anthropic/claude-opus-5-5"),
    # Identity is what the anthropic WIRE sends: one anthropic_api_model_id
    # call on the stored id. A direct "-latest" goes out literally, so it is
    # its own (unlisted) id; the OpenRouter dashed Sonnet 4.6 spelling is
    # rewritten to Sonnet 5 by the existing id map, so it IS Sonnet 5.
    ("anthropic", "claude-opus-latest", "anthropic/claude-opus-latest"),
    ("anthropic", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
    ("openrouter", "anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4-6"),
    ("openrouter", "anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-5"),
    ("anthropic", "claude-haiku-4.5", "anthropic/claude-haiku-4-5"),
    ("anthropic", "claude-haiku-4-5-20251001", "anthropic/claude-haiku-4-5"),
    ("anthropic", "claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
    ("openrouter", "anthropic/claude-haiku-5.5", "anthropic/claude-haiku-5-5"),
    # xAI: its own id and OpenRouter's x-ai/ prefix are the same model.
    ("xai", "grok-4.6", "xai/grok-4.6"),
    ("openrouter", "x-ai/grok-4.6", "xai/grok-4.6"),
    # OpenAI and Google.
    ("openai", "gpt-6-astra", "openai/gpt-6-astra"),
    ("openrouter", "openai/gpt-6-astra", "openai/gpt-6-astra"),
    ("google", "gemini-3.8-flash", "google/gemini-3.8-flash"),
    ("google", "models/gemini-3.8-flash", "google/gemini-3.8-flash"),
    ("openrouter", "google/gemini-3.8-flash", "google/gemini-3.8-flash"),
    # Moonshot: kimi provider and OpenRouter's moonshotai/.
    ("kimi", "kimi-k3", "moonshot/kimi-k3"),
    ("openrouter", "moonshotai/kimi-k3", "moonshot/kimi-k3"),
    # Open-weight labs on OpenRouter resolve only through the alias table.
    ("openrouter", "z-ai/glm-5.3", "zai/glm-5.3"),
])
def test_one_model_one_identity(datafile, provider, model, want):
    assert MS.canonical_model(provider, model) == want


@pytest.mark.parametrize("provider,model", [
    ("local", "qwen3.8-27b"),                                    # bare local tag
    ("local", "hf.co/HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive:Q4_K_M"),
    ("openrouter", "cognitivecomputations/dolphin-mistral-24b-venice-edition"),
    ("openrouter", "z-ai/glm-5.3-flashx"),                       # not the listed weights
    ("deepinfra", "deepseek-ai/DeepSeek-V4.1-Flash"),            # generic: alias only
    ("together", "moonshotai/Kimi-K3"),
    ("openrouter", "x-ai/grok-4.6:thinking"),                    # unknown variant
    ("openrouter", "no-slash"),
    # First-party prefixes on OpenRouter resolve by rule only to LISTED models:
    # OpenRouter serves open weights under them from third-party hosts.
    ("openrouter", "openai/gpt-6-astra-pro"),
    ("openrouter", "openai/gpt-oss-120b"),
    ("openrouter", "google/gemma-4-31b"),
    ("", "claude-opus-5-5"),
    ("anthropic", ""),
])
def test_unprovable_identity_resolves_to_none(datafile, provider, model):
    assert MS.canonical_model(provider, model) is None


def test_eligible_is_not_approved_without_the_bench(datafile):
    v = MS.check("anthropic", "claude-opus-5-5")
    assert v["canonical"] == "anthropic/claude-opus-5-5"
    assert v["status"] == "eligible" and v["eligible"] is True
    assert v["validated"] is False and v["approved"] is False
    assert "benches" in v["reason"]


def test_statuses_carry_their_reason(datafile):
    assert MS.check("openrouter", "moonshotai/kimi-k3")["status"] == "no_card"
    assert MS.check("xai", "grok-4.3")["status"] == "below_floor"
    assert MS.check("openai", "gpt-6-sol")["status"] == "pending_card"
    assert MS.check("openai", "gpt-6-astra-pro")["status"] == "unlisted"
    assert MS.check("anthropic", "claude-haiku-4.5")["status"] == "below_floor"
    # Haiku 5.5 clears the floor from medium effort (AA v4.3.2: low 29,
    # medium 34) but shipped 2026-10-07 with no system card yet.
    assert MS.check("anthropic", "claude-haiku-5-5")["status"] == "pending_card"
    assert MS.check("local", "llama")["status"] == "unidentified"
    for prov, mod in [("openrouter", "moonshotai/kimi-k3"), ("xai", "grok-4.3"),
                      ("openai", "gpt-6-sol"), ("local", "llama")]:
        v = MS.check(prov, mod)
        assert not v["eligible"] and not v["approved"] and v["reason"]


def test_approved_needs_all_three_and_respects_effort(datafile, monkeypatch):
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    # Grok 4.7 clears the floor only from 'high'. On OpenRouter the call
    # carries an effort, so it is judged: stored, or the default "low" sent.
    assert MS.check("openrouter", "x-ai/grok-4.7", "high")["approved"] is True
    low = MS.check("openrouter", "x-ai/grok-4.7", "low")
    assert low["effort_ok"] is False and low["approved"] is False and "effort" in low["reason"]
    unset = MS.check("openrouter", "x-ai/grok-4.7")
    assert unset["effort_ok"] is False, "no stored effort = the 'low' the platform sends"
    # On xai direct the platform cannot set effort: not judged either way.
    for eff in (None, "low", "max"):
        assert MS.check("xai", "grok-4.7", eff)["effort_ok"] is None
    # Validated but not eligible stays unapproved: the bench cannot admit a
    # model the published tests exclude.
    assert MS.check("openrouter", "moonshotai/kimi-k3")["approved"] is False


def test_a_saved_route_is_never_refused_at_send(datafile, monkeypatch):
    """Second review's blocking case: callers of one stored route send
    different efforts (the chat turn and consolidators "low", deep research,
    scoped agents and web research none, some hardcode "low"). Effort is judged
    at the WRITER only; the gateway's check is strictly weaker, so any route
    the writer accepts passes at send whatever effort the caller sends."""
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    datafile(enforce=True)
    routes = [("anthropic", "claude-sonnet-5"), ("anthropic", "claude-opus-4-8"),
              ("anthropic", "claude-sonnet-4-6"), ("xai", "grok-4.7"),
              ("openrouter", "x-ai/grok-4.7"), ("google", "gemini-3.8-flash"),
              ("openrouter", "anthropic/claude-sonnet-4.6"), ("openrouter", "x-ai/grok-4.6")]
    for prov, mod in routes:
        for stored in (None, "low", "medium", "high", "max"):
            if MS.refusal(prov, mod, stored) is not None:
                continue                       # the writer refuses: never stored
            assert MS.refusal(prov, mod, judge_effort=False) is None, (prov, mod, stored)
    # And the writer really does judge effort where the platform sets it.
    assert MS.refusal("openrouter", "x-ai/grok-4.7", "low")
    assert MS.refusal("openrouter", "x-ai/grok-4.7") , "no stored effort = the 'low' sent"
    assert MS.refusal("openrouter", "x-ai/grok-4.7", "high") is None


def test_the_bench_list_keys_the_model_the_wire_sends(datafile, monkeypatch):
    """Second review's other blocking case: with only a Sonnet 5 bench pass on
    file, Sonnet 4.6 must NOT read as validated (and vice versa)."""
    real = MV.is_validated

    def only_sonnet_5(p, m):
        return MV._key(p, m) == ("anthropic", "claude-sonnet-5")
    monkeypatch.setattr(MV, "is_validated", only_sonnet_5)
    assert MS.check("anthropic", "claude-sonnet-5")["validated"] is True
    for prov, mod in [("anthropic", "claude-sonnet-4-6"),
                      ("openrouter", "anthropic/claude-sonnet-4.6")]:
        v = MS.check(prov, mod)
        assert v["canonical"] == "anthropic/claude-sonnet-4-6"
        assert v["validated"] is False and v["approved"] is False, (prov, mod)
    monkeypatch.setattr(MV, "is_validated", real)


def test_catalog_judges_a_claude_row_as_the_route_it_stores(datafile, monkeypatch):
    """A Claude row in the OpenRouter catalog is stored (and sent) as provider
    anthropic, where effort is not judged — so the catalog must not show it
    unapproved for an effort it would never run at."""
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    s1 = MS.summary("openrouter", "anthropic/claude-sonnet-4.6")
    s2 = MS.summary("anthropic", "claude-sonnet-4-6")
    assert s1["canonical"] == s2["canonical"] == "anthropic/claude-sonnet-4-6"
    assert s1["approved"] == s2["approved"] is True


def test_effort_providers_mirror_text_role_efforts():
    """EFFORT_PROVIDERS must equal TEXT_ROLE_EFFORTS' keys; read by AST so the
    test needs no server imports."""
    import ast
    src = (_REPO / "server/routes/inference.py").read_text(encoding="utf-8")
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "TEXT_ROLE_EFFORTS":
            keys |= {k.value for k in node.value.keys}
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript)
                and getattr(node.targets[0].value, "id", "") == "TEXT_ROLE_EFFORTS"):
            keys.add(node.targets[0].slice.value)
    assert keys and keys == set(MS.EFFORT_PROVIDERS), keys


def test_refusal_only_when_enforced(datafile, monkeypatch):
    assert MS.refusal("local", "anything") is None                               # shipped: enforce false
    datafile(enforce=True)
    r = MS.refusal("xai", "grok-4.3")
    assert r and "not an approved Host model" in r and "floor" in r
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    assert MS.refusal("xai", "grok-4.6") is None


def test_only_a_real_true_enforces(datafile):
    datafile(enforce="false")
    assert MS.enforced() is False
    datafile(enforce=1)
    assert MS.enforced() is False
    datafile(enforce=True)
    assert MS.enforced() is True


def test_missing_or_broken_file_fails_open(tmp_path, monkeypatch):
    p = tmp_path / "approved_models.json"
    monkeypatch.setattr(MS, "_path", lambda: p)
    assert MS.load(force=True)["enforce"] is False and MS.refusal("local", "x") is None
    p.write_text("{not json", encoding="utf-8")
    assert MS.load(force=True)["enforce"] is False and MS.refusal("local", "x") is None
    MS.load(force=True)


def test_summary_never_raises(datafile, monkeypatch):
    monkeypatch.setattr(MS, "check", lambda *a, **k: 1 / 0)
    s = MS.summary("openrouter", "x-ai/grok-4.6")
    assert s["status"] == "unidentified" and s["approved"] is False


def test_only_the_admission_bench_claims_the_exemption():
    """The gateway exempts _source == "model_bench". That kwarg is set by
    platform code, never by a user; this ratchet keeps it to one setter."""
    import re
    setters = []
    for base in ("brain", "server"):
        for f in (_REPO / base).rglob("*.py"):
            t = f.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"""(_source\s*=\s*|["']_source["']\s*[:,]\s*)["']model_bench["']""", t):
                setters.append(f.relative_to(_REPO).as_posix())
    assert setters == ["brain/mastercode/runtime/model_bench.py"], setters


@pytest.fixture
def gateway(monkeypatch):
    """call_model with the network replaced: every dial is recorded and
    answers per `answers[(provider, model)]` (default: ok)."""
    from mastercode import provider_gateway as GW
    dials, answers = [], {}

    def _stub(default_provider):
        async def _f(*, messages, model, **kw):
            prov = kw.get("provider") or default_provider
            dials.append((prov, model, kw.get("reasoning_effort")))
            return answers.get((prov, model), {"content": "ok"})
        return _f
    monkeypatch.setattr(GW, "_call_remote", _stub(None))
    monkeypatch.setattr(GW, "_call_anthropic", _stub("anthropic"))
    monkeypatch.setattr(GW, "_call_local", _stub("local"))

    async def _nolog(*a, **k):
        return None
    monkeypatch.setattr(GW, "_log_call_failure", _nolog)
    return GW, dials, answers


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_gateway_unchanged_while_not_enforced(datafile, gateway):
    GW, dials, _ = gateway
    r = _run(GW.call_model([{"role": "user", "content": "hi"}],
                           provider="local", provider_model="some-local-tag"))
    assert r.get("content") == "ok" and dials and dials[0][:2] == ("local", "some-local-tag")


def test_gateway_refuses_unapproved_primary_without_dialling(datafile, gateway):
    GW, dials, _ = gateway
    datafile(enforce=True)
    r = _run(GW.call_model([{"role": "user", "content": "hi"}],
                           provider="xai", provider_model="grok-4.3",
                           fallback_provider="anthropic", fallback_model="claude-opus-5"))
    assert r.get("model_not_approved") is True and "floor" in r["error"]
    assert dials == [], "a refused primary is never sent — and never rescued by the fallback"


def test_gateway_skips_an_unapproved_fallback(datafile, gateway, monkeypatch):
    GW, dials, answers = gateway
    datafile(enforce=True)
    monkeypatch.setattr(MV, "is_validated",
                        lambda p, m: (p, m) == ("anthropic", "claude-opus-5"))
    answers[("anthropic", "claude-opus-5")] = {"error": "overloaded", "content": ""}
    r = _run(GW.call_model([{"role": "user", "content": "hi"}],
                           provider="anthropic", provider_model="claude-opus-5",
                           fallback_provider="xai", fallback_model="grok-4.3"))
    assert [d[:2] for d in dials] == [("anthropic", "claude-opus-5")]
    assert r.get("error") == "overloaded", "the primary's error stands; no failover"


def test_gateway_exempts_the_bench(datafile, gateway):
    GW, dials, _ = gateway
    datafile(enforce=True)
    r = _run(GW.call_model([{"role": "user", "content": "hi"}],
                           provider="xai", provider_model="grok-4.3", _source="model_bench"))
    assert r.get("content") == "ok" and dials[0][:2] == ("xai", "grok-4.3")


def test_writer_refuses_with_409_before_writing(datafile, monkeypatch):
    from fastapi import HTTPException
    from server.routes import inference as INF
    writes = []

    class Pool:
        async def fetchrow(self, *a, **k):
            return None

        async def execute(self, *a, **k):
            writes.append(a)

    monkeypatch.setattr(INF.brain_service, "get_pool", lambda: Pool())
    datafile(enforce=True)
    body = INF.SetRoleModel(role="chat", provider="xai", model="grok-4.3")
    with pytest.raises(HTTPException) as ei:
        _run(INF.apply_role_route(body))
    assert ei.value.status_code == 409 and "not an approved Host model" in ei.value.detail
    assert writes == []
    body = INF.SetRoleModel(role="chat", provider="anthropic", model="claude-opus-5",
                            fallback_provider="xai", fallback_model="grok-4.3")
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    with pytest.raises(HTTPException) as ei:
        _run(INF.apply_role_route(body))
    assert ei.value.status_code == 409 and ei.value.detail.startswith("fallback:")
    assert writes == []


def test_gateway_fallback_unchanged_while_not_enforced(datafile, gateway):
    GW, dials, answers = gateway
    answers[("xai", "grok-4.3")] = {"error": "down", "content": ""}
    r = _run(GW.call_model([{"role": "user", "content": "hi"}],
                           provider="xai", provider_model="grok-4.3",
                           fallback_provider="local", fallback_model="some-tag"))
    assert [d[:2] for d in dials] == [("xai", "grok-4.3"), ("local", "some-tag")]
    assert r.get("content") == "ok"


def _writer_env(monkeypatch, local_hit):
    from server.routes import inference as INF
    writes = []

    class Pool:
        async def fetchrow(self, *a, **k):
            return None

        async def execute(self, sql, *args):
            writes.append(args)

    async def _noop(*a, **k):
        return None

    async def _match(pool, eid, model):
        return local_hit
    monkeypatch.setattr(INF.brain_service, "get_pool", lambda: Pool())
    monkeypatch.setattr(INF, "_log_route_apply", _noop)
    monkeypatch.setattr(INF, "_report_config_change_to_central", _noop)
    import mastercode.model_backends as MB_
    monkeypatch.setattr(MB_, "match_local_model", _match)
    return INF, writes


def _stored_route(writes, role="chat"):
    # The route write is the last behavioral_params UPDATE; saving a chat route
    # also releases held requests (an UPDATE of calendar_events whose only
    # argument is the entity id), which is not a route write.
    params = json.loads([w for w in writes if isinstance(w[0], str)][-1][0])
    return params["model_routing"][role]


def test_writer_unchanged_while_not_enforced(datafile, monkeypatch):
    INF, writes = _writer_env(monkeypatch, None)
    _run(INF.apply_role_route(INF.SetRoleModel(role="chat", provider="xai", model="grok-4.3")))
    assert _stored_route(writes) == {"provider": "xai", "model": "grok-4.3"}


def test_local_first_flip_only_while_not_enforced(datafile, monkeypatch):
    hit = {"bastion": "desk", "endpoint": "http://10.0.0.2:11434"}
    INF, writes = _writer_env(monkeypatch, hit)
    body = dict(role="chat", provider="openrouter", model="x-ai/grok-4.6", effort="high")
    # Off: today's behaviour — the pick flips to the machine that serves it.
    _run(INF.apply_role_route(INF.SetRoleModel(**body)))
    assert _stored_route(writes)["provider"] == "local"
    # On: the flip would turn an approved pick into an unidentifiable one, so
    # the OpenRouter pick is stored as chosen.
    datafile(enforce=True)
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    _run(INF.apply_role_route(INF.SetRoleModel(**body)))
    assert _stored_route(writes) == {"provider": "openrouter", "model": "x-ai/grok-4.6",
                                     "effort": "high"}


def test_gateway_sends_an_approved_route_whatever_effort_the_caller_sends(datafile, gateway, monkeypatch):
    """Gateway-level twin of test_a_saved_route_is_never_refused_at_send:
    a route the writer accepts at "high" is dialled by call_model whether the
    caller sends "high", "low" or no effort at all."""
    GW, dials, _ = gateway
    datafile(enforce=True)
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    assert MS.refusal("openrouter", "x-ai/grok-4.7", "high") is None
    for eff in ("high", "low", None):
        kw = {"reasoning_effort": eff} if eff else {}
        r = _run(GW.call_model([{"role": "user", "content": "hi"}],
                               provider="openrouter", provider_model="x-ai/grok-4.7", **kw))
        assert r.get("content") == "ok", (eff, r)
    assert len(dials) == 3


def test_audit_routes_finds_what_the_writer_would_now_refuse(datafile, monkeypatch):
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    routing = {
        "chat": {"provider": "openrouter", "model": "x-ai/grok-4.7", "effort": "low"},
        "review": {"provider": "anthropic", "model": "claude-opus-5",
                   "fallback_provider": "xai", "fallback_model": "grok-4.3"},
        "consolidation": {"provider": "xai", "model": "grok-4.6"},
        "image_gen": {"provider": "openai", "model": "anything"},
    }
    rows = MS.audit_routes(routing)
    got = {(r["role"], r["which"]) for r in rows}
    assert got == {("chat", "primary"), ("review", "fallback")}, rows
    assert all(r["reason"] for r in rows)
    assert MS.audit_routes(None) == []


# ── image models (2026-09-29: no NSFW or uncensored image models) ────────────

@pytest.mark.parametrize("provider,model,status,approved", [
    ("openai", "gpt-image-2.5-sunburst", "eligible", True),
    ("openrouter", "openai/gpt-image-2.5-flare", "eligible", True),
    ("xai", "grok-imagine-image-2.0", "pending_card", False),
    ("google", "gemini-3.1-flash-image", "pending_review", False),
    ("openrouter", "black-forest-labs/flux.2-pro", "unidentified", False),
    ("together", "RunDiffusion/Juggernaut-pro-flux", "unidentified", False),
    ("local", "x/z-image-turbo", "unidentified", False),
    (None, "gpt-image-2.5-sunburst", "unidentified", False),     # pinned endpoint
    ("openai", "gpt-image-1", "unlisted", False),
])
def test_image_standard_verdicts(datafile, provider, model, status, approved):
    v = MS.check_image(provider, model)
    assert (v["status"], v["approved"]) == (status, approved), v
    if not approved:
        assert v["reason"]


def test_image_refusal_only_when_enforced(datafile):
    assert MS.refusal_image("local", "x/z-image-turbo") is None
    datafile(enforce=True)
    assert "not an approved image model" in MS.refusal_image("local", "x/z-image-turbo")
    assert MS.refusal_image("openai", "gpt-image-2.5-sunburst") is None


def test_image_generation_request_body_stays_closed():
    """The image approvals assume every provider runs at its default filter,
    which holds only while the platform sends model, prompt and n — plus, for
    OpenAI's own API, moderation="auto" stated explicitly — plus the two
    picture options size and quality (validated values; neither touches a
    filter) — and NOTHING else (no
    moderation="low", safety_tolerance or disable_safety_checker)."""
    import ast
    src = (_REPO / "brain/mastercode/image_engine.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_generate")
    posts = [kw.value for n in ast.walk(fn) if isinstance(n, ast.Call)
             for kw in n.keywords if kw.arg == "json"]
    assert len(posts) == 1 and isinstance(posts[0], ast.Name) and posts[0].id == "req_body"
    inits = [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
             and any(getattr(t, "id", None) == "req_body" for t in n.targets)]
    assert len(inits) == 1 and isinstance(inits[0], ast.Dict)
    assert sorted(k.value for k in inits[0].keys) == ["model", "n", "prompt"]
    extra = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Subscript) and getattr(t.value, "id", None) == "req_body"
                     for t in n.targets)]
    added = {n.targets[0].slice.value: n.value for n in extra}
    assert sorted(added) == ["moderation", "quality", "size"], sorted(added)
    assert added["moderation"].value == "auto"
    # size and quality are the Host's validated arguments, never a literal
    # that could name a filter setting.
    assert all(isinstance(added[k], ast.Name) and added[k].id == k
               for k in ("size", "quality"))
    mutations = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", None) == "req_body"]
    assert mutations == [], "no body.update()/setdefault()"


class _Ctx:
    def __init__(self, args):
        self.args, self.entity_id, self.pool = args, 1, None
    def log(self, *a):
        pass


def test_image_engine_refuses_before_any_request(datafile, monkeypatch):
    from mastercode import image_engine as IE
    async def route(context, role):
        return {"base": "https://example.invalid/v1", "model": "x/z-image-turbo",
                "key": "k", "std_provider": "local"}
    monkeypatch.setattr(IE, "_resolve_image_route", route)
    posted = []
    class Boom:
        def __init__(self, *a, **k):
            posted.append(1)
            raise AssertionError("no request may be made")
    monkeypatch.setattr(IE.aiohttp, "ClientSession", Boom)
    datafile(enforce=True)
    r = _run(IE._generate(_Ctx({"prompt": "a cat"})))
    assert r["status"] == "error" and r.get("model_not_approved") and posted == []
    # (Image understanding on a second model went 2026-10-12: the Host's own
    # model, already held to the text standard, is the one that sees.)
    assert not hasattr(IE, "_understand")


def test_writer_refuses_unapproved_image_routes(datafile, monkeypatch):
    from fastapi import HTTPException
    INF, writes = _writer_env(monkeypatch, None)
    datafile(enforce=True)
    for body in (dict(role="image_gen", provider="xai", model="grok-imagine-image-2.0"),
                 dict(role="image_gen", provider="openai", model="gpt-image-2.5-sunburst",
                      base_url="https://my.gw/v1")):
        with pytest.raises(HTTPException) as ei:
            _run(INF.apply_role_route(INF.SetRoleModel(**body)))
        assert ei.value.status_code == 409, body
    assert writes == []


def test_writer_refuses_the_retired_vision_role_naming_the_roles(datafile, monkeypatch):
    # 2026-10-12: a picture goes to the Host model, so 'vision' is no longer a
    # role. A write naming it is refused plainly (400, the valid roles named),
    # never a crash, and nothing is written — whatever the model.
    from fastapi import HTTPException
    INF, writes = _writer_env(monkeypatch, None)
    datafile(enforce=True)
    with pytest.raises(HTTPException) as ei:
        _run(INF.apply_role_route(INF.SetRoleModel(
            role="vision", provider="anthropic", model="claude-opus-5")))
    assert ei.value.status_code == 400
    assert "Invalid role" in str(ei.value.detail) and "image_gen" in str(ei.value.detail)
    assert "vision" not in INF.ROLES
    assert writes == []


def _capture_session(monkeypatch, IE, sent):
    class Resp:
        status = 200
        async def json(self):
            return {"data": [{"url": "https://img.example/1.png"}]}
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def post(self, url, headers=None, json=None, timeout=None):
            sent.append(json)
            return Resp()
    monkeypatch.setattr(IE.aiohttp, "ClientSession", Session)


class _Pool:
    async def execute(self, *a, **k):
        return None


def test_openai_direct_pins_moderation_auto_and_openrouter_does_not(datafile, monkeypatch):
    from mastercode import image_engine as IE
    sent = []
    _capture_session(monkeypatch, IE, sent)
    for std in ("openai", "openrouter", None):
        async def route(context, role, std=std):
            return {"base": "https://api.example/v1", "model": "gpt-image-2.5-sunburst",
                    "key": "k", "std_provider": std}
        monkeypatch.setattr(IE, "_resolve_image_route", route)
        ctx = _Ctx({"prompt": "a cat"})
        ctx.pool = _Pool()
        r = _run(IE._generate(ctx))
        assert r["status"] == "ok", r
    assert sent[0] == {"model": "gpt-image-2.5-sunburst", "prompt": "a cat", "n": 1,
                       "moderation": "auto"}
    assert sent[1] == sent[2] == {"model": "gpt-image-2.5-sunburst", "prompt": "a cat", "n": 1}
    # An older OpenAI image model takes no moderation field: never sent it.
    async def dalle(context, role):
        return {"base": "https://api.example/v1", "model": "dall-e-3", "key": "k",
                "std_provider": "openai"}
    monkeypatch.setattr(IE, "_resolve_image_route", dalle)
    ctx = _Ctx({"prompt": "a cat"})
    ctx.pool = _Pool()
    _run(IE._generate(ctx))
    assert sent[3] == {"model": "dall-e-3", "prompt": "a cat", "n": 1}


def test_a_pinned_image_endpoint_is_unidentified_through_the_real_resolver(datafile, monkeypatch):
    """The engine's own _resolve_image_route: a route that pins base_url
    yields std_provider None even when it names an approved provider+model,
    so with the standard enforced nothing is sent."""
    from mastercode import image_engine as IE
    import mastercode.inference_config as IC

    async def rrr(pool, eid, role, **k):
        return {"provider": "openai", "model": "gpt-image-2.5-sunburst",
                "base_url": "https://my.gateway/v1", "connection_id": 7}

    async def key(context, cid):
        return "k"
    monkeypatch.setattr(IC, "resolve_role_route", rrr)
    monkeypatch.setattr(IE, "_resolve_byo_key", key)
    route = _run(IE._resolve_image_route(_Ctx({}), "image_gen"))
    assert route["std_provider"] is None and route["base"] == "https://my.gateway/v1"
    sent = []
    _capture_session(monkeypatch, IE, sent)
    datafile(enforce=True)
    r = _run(IE._generate(_Ctx({"prompt": "a cat"})))
    assert r.get("model_not_approved") and sent == []


def test_recommendation_helpers_are_inert_off_and_match_the_writer_on(datafile, monkeypatch):
    cands = ["grok-4.3", "grok-4.6", "grok-4.7"]
    assert MS.approved_candidates("xai", cands) == cands                     # off: untouched
    assert MS.pick_allowed("image_gen", "local", "x/z-image-turbo") is True  # off: untouched
    datafile(enforce=True)
    monkeypatch.setattr(MV, "is_validated", lambda p, m: True)
    kept = MS.approved_candidates("xai", cands)
    assert kept == ["grok-4.6", "grok-4.7"]
    for c in cands:
        assert (c in kept) == (MS.refusal("xai", c) is None)                # = the writer
    assert MS.pick_allowed("image_gen", "openai", "gpt-image-2.5-sunburst") is True
    assert MS.pick_allowed("image_gen", "xai", "grok-imagine-image-2.0") is False


def test_the_recommender_uses_the_helpers():
    src = (_REPO / "brain/mastercode/runtime/tools.py").read_text(encoding="utf-8")
    rec = src[src.index('if op == "recommend":'):]
    rec = rec[: rec.index('"apply_with"')]
    assert "_ms.approved_candidates(pick, cands)" in rec
    assert "_ms.pick_allowed(role, _pp, _pm)" in rec


def test_moderation_pin_only_for_approved_openai_image_models(datafile):
    assert MS.pins_moderation("openai", "gpt-image-2.5-sunburst") is True
    assert MS.pins_moderation("openai", "gpt-image-2.5-flare") is True
    assert MS.pins_moderation("openai", "gpt-image-1.5") is False     # unconfirmed field support
    assert MS.pins_moderation("openai", "dall-e-3") is False
    assert MS.pins_moderation("openrouter", "openai/gpt-image-2.5-sunburst") is False
    assert MS.pins_moderation(None, "gpt-image-2.5-sunburst") is False
