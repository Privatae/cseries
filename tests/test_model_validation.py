"""The validated gate (runtime/model_validation.py, validated-models.json).

Proven here against the shipped data file and a temporary one:
  1. The shipped file parses, names the CURRENT bench suite version, ships
     enforce=false, and every entry (none yet) has the full evidence shape.
  2. Lookups normalize like the writer (a Claude pick on openrouter matches an
     entry stored as anthropic), are case-insensitive, and ignore entries of
     an older suite.
  3. withhold_tools drops operate / dispatch / the host doors only when
     enforced AND unvalidated; a missing or broken file fails OPEN.
  4. The refusal envelope is stamped for the ledger.
  5. The catalog rows carry `validated`; the routing status carries it per
     role and the gate's posture.

Run: pytest tests/test_model_validation.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from mastercode.runtime import model_validation as MV  # noqa: E402
from mastercode.runtime import model_bench as MB  # noqa: E402

_REQUIRED = ("provider", "model", "suite_version", "suite_hash", "run_id", "at", "by")


def test_shipped_file_is_honest():
    p = _REPO / MV.DATA_FILE
    # In the image, beside the module that reads it — brain/ is what ships.
    assert p.parent == pathlib.Path(MV.__file__).resolve().parent
    assert MV._candidates() == [p.resolve()]
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["suite_version"] == MB.SUITE_VERSION
    assert doc["enforce"] is False, "enforcement is flipped by a reviewed change, never by default"
    for e in doc["validated"]:
        assert all(k in e for k in _REQUIRED), e
        assert e["suite_version"] == MB.SUITE_VERSION
    assert MV.load(force=True)["enforce"] is False
    assert MV.enforced() is False


@pytest.fixture
def datafile(tmp_path, monkeypatch):
    p = tmp_path / "validated-models.json"

    def _write(doc):
        p.write_text(json.dumps(doc), encoding="utf-8")
        MV.load(force=True)
        return p
    monkeypatch.setattr(MV, "_candidates", lambda: [p])
    yield _write
    MV.load(force=True)


def _entry(provider, model, version=MB.SUITE_VERSION):
    return {"provider": provider, "model": model, "suite_version": version, "suite_hash": "h",
            "run_id": 1, "at": "2026-09-18T00:00:00Z", "by": "operator"}


def test_lookup_normalizes_like_the_writer_and_ignores_old_suites(datafile):
    datafile({"suite_version": MB.SUITE_VERSION, "enforce": True,
              "validated": [_entry("kimi", "k3"), _entry("anthropic", "claude-opus-5"),
                            _entry("openai", "gpt-old", version="0")]})
    assert MV.enforced() is True
    assert MV.is_validated("kimi", "k3") and MV.is_validated("KIMI", "K3")
    # A Claude pick spelled the openrouter way normalizes to anthropic.
    from mastercode.inference_config import normalize_provider
    p, m = normalize_provider("openrouter", "anthropic/claude-opus-5")
    if p == "anthropic":
        assert MV.is_validated("openrouter", "anthropic/claude-opus-5")
    assert MV.is_validated("anthropic", "claude-opus-5")
    assert not MV.is_validated("openai", "gpt-old"), "a pass under an older suite is history"
    assert not MV.is_validated("openai", "gpt-x")
    assert MV.status_for("kimi", "k3") == {"validated": True, "enforced": True, "entry": _entry("kimi", "k3")}
    assert MV.status_for(None, None)["validated"] is False
    # Symmetry: a vendor prefix that repeats the provider is dropped on BOTH
    # sides, so one entry matches every spelling of one model.
    assert MV._key("anthropic", "anthropic/claude-opus-5") == MV._key("anthropic", "claude-opus-5")
    assert MV.is_validated("anthropic", "anthropic/claude-opus-5")
    # EVERY spelling keys as the model the anthropic WIRE sends for it — one
    # anthropic_api_model_id call on the stored id (provider_gateway.
    # _call_anthropic). The id map is keyed on the prefixed spelling, so a
    # prefixed alias keys as its API id, while a BARE alias the map does not
    # hold (claude-haiku-latest) is sent literally and keys as itself. The
    # earlier version of this test asserted the bare alias keyed as the API id
    # — the same collapse that filed a Sonnet 4.6 pass under Sonnet 5
    # (review of the Host model standard, 2026-09-29).
    from mastercode.inference_config import _ANTHROPIC_API_ID, anthropic_api_model_id
    assert _ANTHROPIC_API_ID, "the alias map is empty — fixture changed"
    for k, api_id in _ANTHROPIC_API_ID.items():
        bare = k.split("/", 1)[1] if k.startswith("anthropic/") else k
        want_prefixed = MV._key("anthropic", api_id)
        assert MV._key("anthropic", "anthropic/" + bare) == want_prefixed, k
        assert MV._key("openrouter", "anthropic/" + bare) == want_prefixed, k
        sent_bare = anthropic_api_model_id(bare)
        assert MV._key("anthropic", bare) == MV._key("anthropic", sent_bare), (k, bare)
    # The case that broke: Sonnet 4.6 spelled the way it is SENT keys as 4.6,
    # never as the Sonnet 5 the dashed OpenRouter alias is rewritten to.
    assert MV._key("anthropic", "claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")
    assert MV._key("openrouter", "anthropic/claude-sonnet-4.6") == ("anthropic", "claude-sonnet-4-6")
    assert MV._key("anthropic", "claude-sonnet-5") == ("anthropic", "claude-sonnet-5")


def test_the_briefing_never_teaches_a_withheld_verb():
    from mastercode.runtime.session import _build_orchestrator_prompt
    normal = _build_orchestrator_prompt([])
    # A validated route is taught both action verbs.
    assert "(run with `operate`)" in normal and "the only work a dispatched agent takes" in normal
    gated = _build_orchestrator_prompt([], withheld={"operate", "dispatch"},
                                       unvalidated_route=("openai", "gpt-x"))
    # A withheld verb is never taught: it is named only as absent on this route.
    assert "(run with `operate`)" not in gated and "the only work a dispatched agent takes" not in gated
    assert "no `operate` on this route" in gated and "no `dispatch` on this route" in gated
    assert "openai / gpt-x" in gated and "not validated for tool use" in gated
    assert "state-first" not in gated
    # Reading stays taught.
    assert "(run with `read`)" in gated and "`find_tools`" in gated


def test_a_model_is_the_weights_it_runs_on(datafile):
    # openrouter/X does not validate local/X.
    datafile({"suite_version": MB.SUITE_VERSION, "enforce": True,
              "validated": [_entry("openrouter", "qwen/qwen3.5-9b")]})
    assert MV.is_validated("openrouter", "qwen/qwen3.5-9b") and not MV.is_validated("local", "qwen/qwen3.5-9b")


def test_route_validated_covers_the_failover_pair(datafile):
    datafile({"suite_version": MB.SUITE_VERSION, "enforce": True,
              "validated": [_entry("kimi", "k3"), _entry("anthropic", "claude-opus-5")]})
    assert MV.route_validated("kimi", "k3") == (True, None)
    assert MV.route_validated("kimi", "k3", "anthropic", "claude-opus-5") == (True, None)
    # An unvalidated fallback answers with the same tools when the primary errors.
    assert MV.route_validated("kimi", "k3", "openai", "gpt-x") == (False, ("openai", "gpt-x"))
    # fallback_model None = the primary's id on the fallback provider.
    assert MV.route_validated("kimi", "k3", "openai", None) == (False, ("openai", "k3"))
    assert MV.route_validated("openai", "gpt-x") == (False, ("openai", "gpt-x"))
    assert MV.route_validated(None, None)[0] is False


def _verbs():
    from mastercode.runtime.session import _build_host_orchestration_tools
    return _build_host_orchestration_tools()


def test_withhold_only_when_enforced_and_unvalidated(datafile):
    tools = _verbs() + [{"type": "function", "function": {"name": "build"}},
                        {"type": "function", "function": {"name": "web_search"}}]
    kept, withheld = MV.withhold_tools(tools, validated=False, host_lane_names={"build"}, enforce=True)
    names = {t["function"]["name"] for t in kept}
    assert withheld == {"operate", "dispatch", "build"}
    # close_turn (the Host's end of turn) is not an action verb: it stays on
    # an unvalidated route, like recall and escalate.
    assert names == {"recall", "agent_status", "escalate", "close_turn", "web_search"}
    assert MV.withhold_tools(tools, validated=True, host_lane_names={"build"}, enforce=True)[1] == set()
    assert MV.withhold_tools(tools, validated=False, host_lane_names={"build"}, enforce=False)[1] == set()
    # enforce=None reads the file.
    datafile({"suite_version": MB.SUITE_VERSION, "enforce": True, "validated": []})
    assert MV.withhold_tools(tools, validated=False)[1] == {"operate", "dispatch"}


def test_a_missing_or_broken_file_fails_open(datafile, tmp_path):
    p = datafile({"suite_version": MB.SUITE_VERSION, "enforce": True, "validated": []})
    p.write_text("{not json", encoding="utf-8")
    doc = MV.load(force=True)
    assert doc["enforce"] is False and doc["validated"] == [] and doc.get("_error")
    assert MV.enforced() is False
    p.unlink()
    assert MV.load(force=True)["enforce"] is False and MV.enforced() is False


def test_refusal_is_stamped_for_the_ledger():
    r = MV.refusal("operate", "kimi", "k3")
    assert r["status"] == "denied" and r["reason"] == "not_advertised"
    assert "kimi / k3" in r["result"] and "validated" in r["result"]
    from mastercode.runtime.tool_call_ledger import classify
    assert classify(r) == "not_advertised"


def test_catalog_rows_and_routing_status_carry_validated(datafile, monkeypatch):
    datafile({"suite_version": MB.SUITE_VERSION, "enforce": False,
              "validated": [_entry("kimi", "k3")]})
    from server.routes import inference as inf
    assert inf._validated("kimi", "k3") is True and inf._validated("kimi", "k2") is False
    assert inf._validated(None, None) is False


def test_diagnostics_raise_a_review_fault_only_while_the_gate_enforces():
    import asyncio
    from mastercode.runtime import diagnostics as D

    def _status(enforced):
        async def routing_status():
            return {"unrouted": [], "providers_with_keys": ["kimi"],
                    "roles": {"chat": {"routed": True, "provider": "kimi", "model": "k3", "validated": False},
                              "review": {"routed": True, "provider": "kimi", "model": "k3", "validated": True},
                              # Unvalidated and NOT a fault: chat is the only
                              # role the gate takes anything from since the
                              # three-role cut, so an unvalidated memory model
                              # loses nothing and must not raise a block.
                              "consolidation": {"routed": True, "provider": "kimi", "model": "k3", "validated": False}},
                    "validation": {"enforced": enforced}}
        return routing_status

    async def routing_recommend():
        return {"recommendations": {}}
    # Observing (the shipped state): no fault — an attention block nobody can
    # act on is not "observes only".
    faults = asyncio.run(D._probe_routing(None, 7, {"routing_status": _status(False),
                                                     "routing_recommend": routing_recommend}))
    assert faults == []
    faults = asyncio.run(D._probe_routing(None, 7, {"routing_status": _status(True),
                                                     "routing_recommend": routing_recommend}))
    assert [f["id"] for f in faults] == ["routing.chat.unvalidated"]
    assert faults[0]["severity"] == "review" and "loses operate" in faults[0]["title"]
