"""Prompt-length tiers (Claude Haiku 5.5, 2026-10-07) on the two paths the
per-call estimator does not cover by itself.

  1. The printed turn price (estimate_turn_cost) is handed a TURN's summed
     input. A tier is judged per call — a prompt of over 100,000 tokens — so
     a turn of several calls under the line must not be priced at the tier
     because their sum crosses it. The per-call ledger session.py passes in
     carries each call's own length.
  2. A first-party Anthropic row takes its prices from its OpenRouter row
     (_enrich_first_party_from_openrouter). The tier rides with them, or the
     direct lane prices flat once Anthropic's own model list is cached
     (_price_for finds the bare id before the prefixed one).

Proven against an injected catalog, never a live one.

Run: pytest tests/test_prompt_tier_pricing.py
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from server.routes import inference as inf  # noqa: E402

BASE_IN, BASE_OUT = 1e-7, 5e-7
TIER_IN, TIER_OUT = 5e-7, 2.5e-6
TIER = {"min_prompt_tokens": 100000,
        "input_per_token": TIER_IN, "output_per_token": TIER_OUT,
        "cache_read_per_token": 5e-8, "cache_write_per_token": 6.25e-7}


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(inf, "_DIRECT_MODELS_CACHE", {
        "testprov": {"data": [
            {"id": "tiered-1", "input_per_token": BASE_IN, "output_per_token": BASE_OUT,
             "cache_read_per_token": 1e-8, "cache_write_per_token": 1.25e-7,
             "price_tiers": [TIER]},
        ]},
    })
    monkeypatch.setattr(inf, "_CATALOG_CACHE", {"data": {"openrouter_models": [
        {"id": "anthropic/claude-haiku-5.5", "input_per_token": "0.0000001",
         "output_per_token": "0.0000005", "cache_read_per_token": 1e-8,
         "cache_write_per_token": 1.25e-7, "price_tiers": [TIER]},
    ]}, "exp": 0.0})
    return inf


def _call(n_in, n_out, **kw):
    return {"in": n_in, "cached": 0, "cache_write": 0, "out": n_out, **kw}


def test_turn_of_calls_under_the_line_prices_at_base(catalog):
    # Three 40k-token calls: the turn's input sums to 120k, but no call
    # reached the 100k line, so the vendor billed every one at the base rates.
    calls = [_call(40_000, 1_000) for _ in range(3)]
    usd, basis = inf.estimate_turn_cost("tiered-1", "testprov", 120_000, 3_000,
                                        per_call=calls)
    want = 120_000 * BASE_IN + 3_000 * BASE_OUT
    assert basis == "list"
    assert usd == pytest.approx(want, abs=1e-9)


def test_only_the_call_over_the_line_takes_the_tier(catalog):
    calls = [_call(40_000, 1_000), _call(150_000, 2_000)]
    usd, basis = inf.estimate_turn_cost("tiered-1", "testprov", 190_000, 3_000,
                                        per_call=calls)
    want = (40_000 * BASE_IN + 1_000 * BASE_OUT
            + 150_000 * TIER_IN + 2_000 * TIER_OUT)
    assert basis == "list"
    assert usd == pytest.approx(want, abs=1e-9)


def test_a_call_with_no_usage_is_left_out_and_the_turn_is_partial(catalog):
    calls = [_call(40_000, 1_000), _call(70_000, 1_000),
             {"in": None, "cached": None, "cache_write": None, "out": None,
              "usage": "not_reported"}]
    usd, basis = inf.estimate_turn_cost("tiered-1", "testprov", 110_000, 2_000,
                                        per_call=calls)
    assert basis == "partial"
    assert usd == pytest.approx(110_000 * BASE_IN + 2_000 * BASE_OUT, abs=1e-9)


def test_flat_priced_turn_is_unchanged_by_the_ledger(catalog, monkeypatch):
    # A model with no tiers keeps the totals arithmetic exactly.
    monkeypatch.setattr(inf, "_DIRECT_MODELS_CACHE", {"testprov": {"data": [
        {"id": "flat-priced", "input_per_token": 2e-6, "output_per_token": 1e-5}]}})
    calls = [_call(60_000, 500), _call(60_000, 500)]
    usd, _ = inf.estimate_turn_cost("flat-priced", "testprov", 120_000, 1_000,
                                    per_call=calls)
    assert usd == pytest.approx(120_000 * 2e-6 + 1_000 * 1e-5, abs=1e-9)


def test_direct_anthropic_row_keeps_the_tier_once_its_list_is_cached(catalog, monkeypatch):
    rows = [{"id": "claude-haiku-5-5"}]
    inf._enrich_first_party_from_openrouter("anthropic", rows)
    assert rows[0].get("price_tiers") == [TIER]
    # The first-party list is now cached: _price_for finds the bare id first.
    monkeypatch.setitem(inf._DIRECT_MODELS_CACHE, "anthropic", {"data": rows})
    p = inf._price_for("claude-haiku-5-5")
    assert p is not None and len(p.tiers) == 1
    usd, priced = inf.estimate_call_cost_detail("claude-haiku-5-5", 150_000, 1_000)
    assert priced.get("prompt_tier") == 100000
    assert usd == pytest.approx(150_000 * TIER_IN + 1_000 * TIER_OUT, abs=1e-9)
