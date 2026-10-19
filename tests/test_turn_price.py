"""The price on the turn (2026-09-18): inference.estimate_turn_cost and the
metrics a regenerate / A/B side carries (circles._metrics_from_result).

Proven against an injected catalog, never a live one:
  1. A priced model yields dollars with the cache split applied, basis 'list'.
  2. A local model is 'free', a flat-plan model is 'subscription', an unknown
     id is 'unpriced' — each with usd None, never 0.
  3. turn_metrics_from_result (the builder behind circles' _metrics_from_result)
     builds the same shape the thread read builds, from the runtime's
     accumulated totals, and degrades to zeros on junk.

Run: pytest tests/test_turn_price.py
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from server.routes import inference as inf  # noqa: E402


@pytest.fixture
def catalog(monkeypatch):
    """One priced model (with published cache rates), one flat-plan model,
    injected into the direct-models cache the estimator reads."""
    monkeypatch.setattr(inf, "_DIRECT_MODELS_CACHE", {
        "testprov": {"data": [
            {"id": "priced-1", "input_per_token": 0.000002, "output_per_token": 0.00001,
             "cache_read_per_token": 0.0000002, "cache_write_per_token": 0.0000025},
            {"id": "flat-1", "subscription": True},
        ]},
    })
    monkeypatch.setattr(inf, "_CATALOG_CACHE", {"data": {"openrouter_models": []}, "exp": 0.0})
    return inf


def test_priced_model_gets_dollars_with_the_cache_split(catalog):
    # 10k in of which 6k cached and 1k cache-written, 2k out.
    usd, basis = inf.estimate_turn_cost("priced-1", "testprov", 10_000, 2_000,
                                        cache_read_tokens=6_000, cache_write_tokens=1_000)
    assert basis == "list"
    expected = (3_000 * 0.000002) + (6_000 * 0.0000002) + (1_000 * 0.0000025) + (2_000 * 0.00001)
    assert usd == pytest.approx(expected, rel=1e-6)
    # Same arithmetic as the per-call estimator the cost log writes with.
    direct, _ = inf.estimate_call_cost_detail("priced-1", 10_000, 2_000,
                                              cache_read_tokens=6_000, cache_write_tokens=1_000)
    assert usd == pytest.approx(direct, rel=1e-6)


def test_served_id_that_does_not_price_falls_back_to_the_routed_id(catalog):
    """The direct Anthropic lane: the served id is a dated form no catalog
    keys on, the routed id prices. The meter's rule, applied here."""
    usd, basis = inf.estimate_turn_cost("priced-1-20251001", "testprov", 1_000, 100,
                                        routed_model_id="priced-1")
    assert basis == "list" and usd == pytest.approx(1_000 * 0.000002 + 100 * 0.00001, rel=1e-6)
    # Served wins when it prices (a genuine failover onto a priced model).
    assert inf.estimate_turn_cost("priced-1", "testprov", 1_000, 100,
                                  routed_model_id="flat-1")[1] == "list"
    # Neither prices: the basis word is the served id's.
    assert inf.estimate_turn_cost("nope-dated", "testprov", 10, 5,
                                  routed_model_id="nope") == (None, "unpriced")
    assert inf.estimate_turn_cost("", "testprov", 10, 5, routed_model_id="flat-1") == (None, "subscription")
    # A GENUINE failover onto a model with no rate: the routed id is not a
    # pricing fallback — the answer is the basis word, never the routed
    # model's dollars labelled 'list' (the meter refuses the same way).
    fo = {"from_model": "priced-1", "to_model": "nope-dated", "to_provider": "testprov"}
    assert inf.estimate_turn_cost("nope-dated", "testprov", 1_000, 100,
                                  routed_model_id="priced-1", failed_over=fo) == (None, "unpriced")
    # A failover onto a model that prices is priced at that model.
    assert inf.estimate_turn_cost("priced-1", "testprov", 1_000, 100,
                                  routed_model_id="flat-1", failed_over=fo)[1] == "list"
    mfo = inf.turn_metrics_from_result({"model": "nope-dated", "routed_model": "priced-1",
                                        "provider": "testprov", "prompt_tokens": 1_000,
                                        "completion_tokens": 100, "failed_over": fo}, 10)
    assert mfo["price_basis"] == "unpriced" and mfo["est_cost_usd"] is None
    # turn_metrics_from_result reads routed_model off the runtime's resp.
    m = inf.turn_metrics_from_result({"model": "priced-1-20251001", "routed_model": "priced-1",
                                      "provider": "testprov", "prompt_tokens": 1_000,
                                      "completion_tokens": 100}, 10)
    assert m["price_basis"] == "list" and m["model"] == "priced-1-20251001"


def test_basis_words_where_there_is_no_price(catalog):
    assert inf.estimate_turn_cost("anything", "local", 10, 5) == (None, "free")
    assert inf.estimate_turn_cost("flat-1", "testprov", 10, 5) == (None, "subscription")
    assert inf.estimate_turn_cost("no-such-model", "testprov", 10, 5) == (None, "unpriced")
    assert inf.estimate_turn_cost("", None, 10, 5) == (None, "unpriced")
    assert inf.estimate_turn_cost("priced-1", "testprov", "x", 5) == (None, "unpriced")


def test_metrics_from_result_matches_the_thread_read_shape(catalog):
    _metrics_from_result = inf.turn_metrics_from_result
    m = _metrics_from_result({
        "model": "priced-1", "provider": "testprov",
        "input_total_tokens": 10_000, "prompt_tokens": 4_000, "completion_tokens": 2_000,
        "cache_read_tokens": 6_000, "cache_write_tokens": 0,
        "failed_over": None,
    }, 1234)
    assert m["gen_ms"] == 1234
    assert m["tokens_in"] == 10_000 and m["tokens_out"] == 2_000 and m["tokens_total"] == 12_000
    assert m["tokens_cached"] == 6_000 and m["tokens_cache_write"] is None
    assert m["model"] == "priced-1" and m["provider"] == "testprov"
    assert m["price_basis"] == "list" and m["est_cost_usd"] > 0
    # The keys the thread read exposes for a stored turn, and nothing missing.
    for k in ("gen_ms", "tokens_total", "tokens_in", "tokens_out", "tokens_cached",
              "tokens_cache_write", "model", "provider", "failed_over",
              "est_cost_usd", "price_basis"):
        assert k in m
    # Junk degrades to zeros and 'unpriced', never a raise.
    j = _metrics_from_result("not a dict", None)
    assert j["tokens_total"] == 0 and j["est_cost_usd"] is None and j["price_basis"] == "unpriced"
    j2 = _metrics_from_result({"model": "priced-1", "prompt_tokens": "junk"}, 5)
    assert j2["tokens_in"] == 0 and j2["gen_ms"] == 5
