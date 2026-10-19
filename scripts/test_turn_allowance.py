#!/usr/bin/env python3
"""Smoke gate: the per-turn SPEND ALLOWANCE that replaced the tool loop's
per-model round cap (brain/mastercode/runtime/turn_allowance.py).

WHAT THIS RATCHETS:
  1. BASIS RESOLUTION — a route with published per-token rates meters in
     "usd"; a subscription / local / uncatalogued route falls back to
     "tokens" (context budget x TOKEN_ALLOWANCE_MULTIPLIER) and NEVER
     fabricates dollars; with neither a price nor a context budget the basis
     is "none" and nothing is enforced. A published row of two ZEROS is not
     a price.
  2. PROJECTION ARITHMETIC — the next round is priced as (last round's
     carried-forward input + this round's new tool-result tokens) at the
     input rate + (last round's cache_read + cache_write) at the cache-READ
     rate + a full output reserve at the output rate. A provider that
     publishes no cache-read rate falls back to the input rate (the same
     non-invented fallback estimate_call_cost_detail uses), which is strictly
     more expensive — the safe direction.
  3. THE STOP DECISION — spent + projected > allowance returns "stop" with
     exactly ONE steering message, and the loop's call site turns tools off
     for that call (structural, like the exhaustion path) rather than asking
     the model nicely.
  4. NEAR-LIMIT FEEDBACK — with room for one more round but not two, the
     verdict is "near": a line saying how much is left, fed back, with
     nothing disabled (feedback, never a cap).
  5. NEVER STOP ON ROUND 1 — the first tool round always completes. Refusing
     the call that feeds the first tool RESULTS back would throw away the
     spend already made and answer from a question whose answer was never
     read.
  6. THE BACKSTOP — one high constant, no model-name substrings, overridable
     from behavioral_params or the environment, and DISABLED by a value <= 0.
  7. PERSISTENCE — read_thread_allowance reads both co-located stores:
     turn_allowances["<kind>:<ref>"] (workbench) and
     circles_thread_models["self"]["turn_allowance_usd"] (circles); a
     non-positive or unparseable stored value reads as "unset", never as
     "spend nothing".
  8. THE CALL SITE — session.py's tool loop still routes its tools= argument
     through the variable the stop decision clears, and the per-model
     substring cap is gone for good.

Fully DB-free and network-free: the persistence half runs against a fake pool
(read_thread_allowance only ever calls pool.fetchrow), and pricing runs against
a seeded catalog cache rather than a live one. So there is no `--pure` split —
every check below runs everywhere, including outside a container.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")
sys.path.insert(0, "brain")
sys.path.insert(0, ".")

GATE_MODEL = "openai/allowance-gate-model"
GATE_MODEL_NOCACHE = "openai/allowance-gate-nocache"
GATE_MODEL_FREE = "openai/allowance-gate-zero-rates"
# The Anthropic lane: routed id prices, the id that comes back off the wire
# does not. Verified against the live map —
# anthropic_api_model_id("anthropic/claude-haiku-4.5") is
# "claude-haiku-4-5-20251001", and _price_for says True / False on that pair.
ANTHROPIC_ROUTED = "anthropic/claude-haiku-4.5"
ANTHROPIC_SERVED = "claude-haiku-4-5-20251001"
FAILOVER_PRICED = "openai/allowance-gate-failover-target"

# Round rates so every expected figure below is exact, not a tolerance.
IN_RATE = 1e-6        # $1.00 per 1M input tokens
OUT_RATE = 5e-6       # $5.00 per 1M output tokens
READ_RATE = 1e-7      # $0.10 per 1M cached-read tokens
WRITE_RATE = 1.25e-6


class FakePool:
    """Everything read_thread_allowance / read_thread_budget touch: one
    fetchrow returning a behavioral_params row. `raises` models a database
    that is down — both readers must degrade to None, never propagate."""

    def __init__(self, behavioral_params, *, raises: bool = False,
                 as_text: bool = False):
        self._bp = behavioral_params
        self._raises = raises
        self._as_text = as_text

    async def fetchrow(self, _sql, *_args):
        self.last_sql, self.last_args = _sql, _args
        if self._raises:
            raise RuntimeError("pool is down")
        # The ledger read is a different query against a different table; this
        # pool only stands in for behavioral_params, so "no turn row yet" is
        # the honest answer for it. The scoping checks below assert on the SQL
        # this captured, not on a row.
        if "context_ledger" in _sql:
            return None
        if self._bp is None:
            return None
        bp = json.dumps(self._bp) if self._as_text else self._bp
        return {"behavioral_params": bp}


def seed_catalog():
    """Put three priced rows in the inference catalog cache the pricing lane
    already reads (server/routes/inference.py `_CATALOG_CACHE`), the same way
    scripts/test_context_budget.py seeds it for the window lookup. Returns a
    restore callable, or None when the module cannot be imported at all."""
    try:
        from server.routes import inference as inf
    except Exception:
        return None
    saved = inf._CATALOG_CACHE.get("data")
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": GATE_MODEL, "context_length": 200_000,
         "input_per_token": IN_RATE, "output_per_token": OUT_RATE,
         "cache_read_per_token": READ_RATE,
         "cache_write_per_token": WRITE_RATE},
        # Publishes input/output but NO cache rates — the fallback path.
        {"id": GATE_MODEL_NOCACHE, "context_length": 200_000,
         "input_per_token": IN_RATE, "output_per_token": OUT_RATE},
        # Admitted to the price map (one rate key present) but both rates are
        # zero. Pricing a turn against this would report every turn as free.
        {"id": GATE_MODEL_FREE, "context_length": 200_000,
         "input_per_token": 0.0, "output_per_token": 0.0},
        # The ANTHROPIC LANE SHAPE. Only the OpenRouter-keyed slug carries
        # rates; the dated id the wire actually sends
        # (anthropic_api_model_id, inference_config.py:129) is deliberately
        # absent, exactly as the live catalog has it.
        {"id": ANTHROPIC_ROUTED, "context_length": 200_000,
         "input_per_token": IN_RATE, "output_per_token": OUT_RATE,
         "cache_read_per_token": READ_RATE,
         "cache_write_per_token": WRITE_RATE},
        # A priced model to fail over TO. ALL FOUR rates are exactly double
        # the fixture above, so "billed at the new model's rates" is a clean
        # 2x; leaving the cache rates off would make the cached share fall
        # back to the input rate and the relationship would not be 2x.
        {"id": FAILOVER_PRICED, "context_length": 200_000,
         "input_per_token": IN_RATE * 2, "output_per_token": OUT_RATE * 2,
         "cache_read_per_token": READ_RATE * 2,
         "cache_write_per_token": WRITE_RATE * 2},
    ]}

    def restore():
        inf._CATALOG_CACHE["data"] = saved
    return restore


def price_checks(check, ta) -> None:
    """Basis resolution + projection arithmetic, against injected prices so
    the numbers are exact and no catalog is involved."""

    class P:
        def __init__(self, i, o, r=None, w=None):
            self.input_per_token = i
            self.output_per_token = o
            self.cache_read = r
            self.cache_write = w

    priced = P(IN_RATE, OUT_RATE, READ_RATE, WRITE_RATE)
    nocache = P(IN_RATE, OUT_RATE, None, None)
    zeros = P(0.0, 0.0, None, None)

    # ── 0. would_stop: check()'s stop test, with nothing recorded ──
    # (close_turn asks it before its one extra call; asking check() there
    # recorded a near line the next ordinary round then stayed quiet about.)
    import copy as _copy
    _agree = _clean = True
    _outcomes = set()
    _cap = int(ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=40_000, price=None).allowance)
    _states = []
    for _calls in range(0, ta.MIN_CALLS_BEFORE_STOP + 2):
        for _new in (0, 5_000, 40_000):
            for _spent in (0, _cap // 2, int(_cap * 0.9), _cap - 1_000, _cap):
                _states.append((_calls, _new, _spent))
            # exactly at the line: spent + projected == allowance is not a stop
            _probe = ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=40_000, price=None)
            _probe._last.update({"in": 11, "cached": 3})
            _states.append((_calls, _new, _cap - int(_probe.project(_new, 16_384))))
            _states.append((_calls, _new, _cap - int(_probe.project(_new, 16_384)) + 1))
    for _calls, _new, _spent in _states:
        sp = ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=40_000, price=None)
        sp.calls, sp.spent_tokens = _calls, _spent
        # state a careless read could disturb in place
        sp.tokens.update({k: 7 for k in sp.tokens})
        sp._last.update({"in": 11, "cached": 3})
        before = _copy.deepcopy(sp.__dict__)
        ws = sp.would_stop(_new, 16_384)
        _outcomes.add(ws)
        _clean &= sp.__dict__ == before
        _agree &= ws == (_copy.deepcopy(sp).check(_new, 16_384)[0] == "stop")
    check("would_stop agrees with check() on every state tried — every call count up to past "
          "MIN_CALLS_BEFORE_STOP, and spent exactly at and one past the line (both answers among them)",
          _agree and _outcomes == {True, False}, str(_outcomes))
    check("would_stop records nothing (deep compare of the object's state)", _clean)

    # ── 1. BASIS ──
    m = ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=40_000,
                     price=priced)
    check("basis: a published per-token route meters in dollars",
          m.basis == "usd", m.basis)
    m_sub = ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=40_000,
                         price=None)
    check("basis: an unpriceable route falls back to TOKENS, never fake dollars",
          m_sub.basis == "tokens", m_sub.basis)
    check("basis tokens: allowance is the context budget x the multiplier",
          m_sub.allowance_tokens == 40_000 * ta.TOKEN_ALLOWANCE_MULTIPLIER,
          str(m_sub.allowance_tokens))
    check("basis tokens: the dollar figure is NOT reported as spendable dollars",
          m_sub.summary().get("spent_usd") is None)
    m_zero = ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=40_000,
                          price=zeros)
    check("basis: a catalog row of two ZERO rates is not a price "
          "(a $0 turn total would be a fabrication, not data)",
          m_zero.basis == "tokens", m_zero.basis)
    m_none = ta.TurnSpend(model="m", allowance_usd=0.50, ctx_budget=None,
                          price=None)
    check("basis: unpriceable AND no context budget → 'none', nothing enforced",
          m_none.basis == "none" and m_none.allowance is None, m_none.basis)
    m_unset = ta.TurnSpend(model="m", allowance_usd=None, ctx_budget=40_000,
                           price=priced)
    check("basis: no allowance set → 'none' (the loop runs to the backstop, "
          "exactly as it did before this dial existed)",
          m_unset.basis == "none", m_unset.basis)
    m_zero_allow = ta.TurnSpend(model="m", allowance_usd=0.0,
                                ctx_budget=40_000, price=priced)
    check("basis: a zero allowance reads as unset, never as 'refuse to answer'",
          m_zero_allow.basis == "none", m_zero_allow.basis)

    # ── 2. PROJECTION ──
    LAST = {"input_total_tokens": 100_000, "cache_read_tokens": 80_000,
            "cache_write_tokens": 10_000, "completion_tokens": 500}
    NEW, RESERVE = 5_000, 2_000
    # prefix = 80k read + 10k written = 90k, carried = 100k − 90k = 10k.
    EXPECT = ((10_000 + NEW) * IN_RATE + 90_000 * READ_RATE
              + RESERVE * OUT_RATE)          # 0.015 + 0.009 + 0.010 = 0.034

    m = ta.TurnSpend(model=GATE_MODEL, allowance_usd=10.0, price=priced)
    entry = m.note_call(LAST, model=GATE_MODEL)
    check("per-call ledger entry carries cache_write (the field the "
          "projection needs and the old ledger lacked)",
          entry.get("cache_write") == 10_000 and entry.get("in") == 100_000
          and entry.get("cached") == 80_000 and entry.get("out") == 500,
          str(entry))
    got = m.project(NEW, RESERVE)
    check("projection (published cache rate): carried+new at input, "
          "read+written at CACHE-READ, reserve at output",
          abs(got - EXPECT) < 1e-12, f"{got} vs {EXPECT}")
    check("projection is materially cheaper than pricing the whole prompt at "
          "the input rate (the cache split is the point)",
          got < (100_000 + NEW) * IN_RATE + RESERVE * OUT_RATE)

    m2 = ta.TurnSpend(model=GATE_MODEL_NOCACHE, allowance_usd=10.0,
                      price=nocache)
    m2.note_call(LAST, model=GATE_MODEL_NOCACHE)
    EXPECT_NC = ((10_000 + NEW) * IN_RATE + 90_000 * IN_RATE
                 + RESERVE * OUT_RATE)
    got_nc = m2.project(NEW, RESERVE)
    check("projection (NO published cache rate): the cached prefix falls back "
          "to the input rate — the only non-invented number available",
          abs(got_nc - EXPECT_NC) < 1e-12, f"{got_nc} vs {EXPECT_NC}")
    check("the unpublished-cache fallback errs HIGH, never low",
          got_nc > got, f"{got_nc} vs {got}")

    m3 = ta.TurnSpend(model="not-in-any-catalog-xyz", allowance_usd=10.0,
                      ctx_budget=40_000, price=None)
    m3.note_call(LAST, model="not-in-any-catalog-xyz")
    check("projection (unpriceable route): raw tokens — last input + new + "
          "reserve, with no cheaper rate to credit the cached share",
          m3.project(NEW, RESERVE) == float(100_000 + NEW + RESERVE),
          str(m3.project(NEW, RESERVE)))
    check("unpriceable route meters tokens, and spends no dollars",
          m3.spent_tokens == 100_500 and m3.spent_usd == 0.0,
          f"{m3.spent_tokens}/{m3.spent_usd}")

    check("no allowance in force → no projection at all",
          ta.TurnSpend(model="m", allowance_usd=None,
                       price=priced).project(NEW, RESERVE) is None)

    # THE OVERSPEND BOUND IS ONE REALISTIC REPLY. A response is never cut
    # mid-stream. Requests now carry the
    # model's own output limit (128,000+), so the projection's reserve alone no
    # longer bounds what a call can write: every request after the first is
    # ALSO held to what the remaining allowance pays for (affordable_output),
    # never below one realistic reply. The projection still scales with the
    # reserve it is given.
    m4 = ta.TurnSpend(model=GATE_MODEL, allowance_usd=10.0, price=priced)
    m4.note_call(LAST, model=GATE_MODEL)
    check("the projection scales with the output reserve",
          abs((m4.project(NEW, 2 * RESERVE) - m4.project(NEW, RESERVE))
              - RESERVE * OUT_RATE) < 1e-12)
    _aff = m4.affordable_output(NEW)
    check("affordable_output: what is left after the next call's input, at the output rate",
          _aff is not None and abs(_aff - (10.0 - m4.spent - m4.project(NEW, 0)) / OUT_RATE) <= 1, _aff)
    check("...a request held to max(affordable, one reply) can overspend by one reply at most",
          m4.spent + m4.project(NEW, max(_aff, RESERVE)) <= 10.0 + RESERVE * OUT_RATE + 1e-9)
    check("no allowance (or no price) -> nothing to clamp",
          ta.TurnSpend(model="m", allowance_usd=None, price=priced).affordable_output(NEW) is None)

    # PROMPT-LENGTH TIERS (Claude Haiku 5.5). The next round's
    # prompt is last input + new, and a prompt at or over a tier's minimum
    # pays that tier's rates for every token ("a prompt of over 100,000 tokens
    # pays higher prices", Anthropic pricing page).
    try:
        from server.routes.inference import ModelPrice
    except Exception:  # noqa: BLE001
        ModelPrice = None
    check("ModelPrice is importable for the tier checks", ModelPrice is not None)
    if ModelPrice is not None:
        upper = ModelPrice(IN_RATE * 5, OUT_RATE * 5, READ_RATE * 5, WRITE_RATE * 5)
        tiered = ModelPrice(IN_RATE, OUT_RATE, READ_RATE, WRITE_RATE,
                            tiers=((100_000, upper),))
        m5 = ta.TurnSpend(model=GATE_MODEL, allowance_usd=10.0, price=tiered)
        m5._last.update({"in": 100_000, "cached": 80_000, "cache_write": 10_000})
        want = ((10_000 + NEW) * IN_RATE * 5 + 90_000 * READ_RATE * 5
                + RESERVE * OUT_RATE * 5)
        got5 = m5.project(NEW, RESERVE)
        check("projection: a next prompt over the tier's minimum prices at the tier's rates",
              got5 is not None and abs(got5 - want) < 1e-12, f"{got5} vs {want}")
        _aff5 = m5.affordable_output(NEW)
        check("affordable_output divides by the TIER's output rate",
              _aff5 is not None
              and abs(_aff5 - (10.0 - m5.project(NEW, 0)) / (OUT_RATE * 5)) <= 1, _aff5)
        m6 = ta.TurnSpend(model=GATE_MODEL, allowance_usd=10.0, price=tiered)
        m6._last.update({"in": 50_000, "cached": 40_000, "cache_write": 0})
        want = (10_000 + NEW) * IN_RATE + 40_000 * READ_RATE + RESERVE * OUT_RATE
        got6 = m6.project(NEW, RESERVE)
        check("projection: a next prompt under the minimum keeps the base rates",
              got6 is not None and abs(got6 - want) < 1e-12, f"{got6} vs {want}")

    # ── 3/4/5. THE DECISION ──
    def meter_at(spent_calls: int, allowance: float):
        mm = ta.TurnSpend(model=GATE_MODEL, allowance_usd=allowance,
                          price=priced)
        for _ in range(spent_calls):
            mm.note_call(LAST, model=GATE_MODEL)
        return mm

    # One call priced at the fixture above costs:
    #   uncached 10k @ in + 80k @ read + 10k @ write + 500 @ out
    one_call = (10_000 * IN_RATE + 80_000 * READ_RATE
                + 10_000 * WRITE_RATE + 500 * OUT_RATE)
    check("a completed call is priced through the same estimator the cost log "
          "writes with",
          abs(meter_at(1, 10.0).spent_usd - one_call) < 1e-9,
          f"{meter_at(1, 10.0).spent_usd} vs {one_call}")

    # STOP: two calls in, and what is left cannot cover another round.
    tight = meter_at(2, 2 * one_call + EXPECT / 2)
    verdict, msg = tight.check(NEW, RESERVE)
    check("stop: spent + projected over the allowance → 'stop'",
          verdict == "stop", verdict)
    check("stop: exactly one steering message, and it says what to do",
          isinstance(msg, str) and msg.count("BUDGET REACHED") == 1
          and "Do NOT call any more tools" in msg, str(msg)[:120])
    check("stop: the message names the budget in dollars a person reads",
          "$" in msg, str(msg)[:120])

    # NEAR: affordable once, not twice.
    near = meter_at(2, 2 * one_call + 1.5 * EXPECT)
    verdict_n, msg_n = near.check(NEW, RESERVE)
    check("near: room for one more round but not two → 'near'",
          verdict_n == "near", verdict_n)
    check("near: it is FEEDBACK, not a cap — it says so and disables nothing",
          isinstance(msg_n, str) and "not an instruction to stop" in msg_n,
          str(msg_n)[:140])
    check("near: BUDGET REACHED is never in the near-limit line",
          "BUDGET REACHED" not in msg_n)

    # OK: plenty left.
    ok = meter_at(2, 2 * one_call + 40 * EXPECT)
    check("ok: comfortably affordable → no message at all",
          ok.check(NEW, RESERVE) == ("ok", None), str(ok.check(NEW, RESERVE)))

    # NEVER STOP ON ROUND 1.
    round1 = meter_at(1, one_call / 1000)     # allowance already blown
    v1, m1 = round1.check(NEW, RESERVE)
    check("never stop on round 1: the first tool round always completes, "
          "however small the budget",
          v1 != "stop", f"{v1} / {str(m1)[:80]}")
    check("never stop on round 1: the SECOND check with the same numbers does "
          "stop (so the guard is the call count, not a broken comparison)",
          meter_at(2, one_call / 1000).check(NEW, RESERVE)[0] == "stop")
    check("MIN_CALLS_BEFORE_STOP is the documented 2 (one complete round)",
          ta.MIN_CALLS_BEFORE_STOP == 2, str(ta.MIN_CALLS_BEFORE_STOP))

    # NO ALLOWANCE MESSAGE AT ALL BEFORE THE FIRST FULL ROUND. The minimum
    # once gated only "stop", so a tight budget could still put a
    # near-limit line in front of the model's very first decision — the one
    # moment it has no tool results to reason from, and the worst moment to
    # bias with budget talk.
    near1 = meter_at(1, one_call + 1.2 * EXPECT)
    check("near does NOT fire on round 1 either — no budget line before the "
          "first tool round has run",
          near1.check(NEW, RESERVE) == ("ok", None),
          str(near1.check(NEW, RESERVE)))
    check("...and the same numbers DO produce near once a round has "
          "completed", meter_at(2, 2 * one_call + 1.2 * EXPECT
                                ).check(NEW, RESERVE)[0] == "near")

    # THE NEAR LINE IS NOT REPEATED EVERY ROUND. The projection always
    # reserves a full output allowance (16384 tokens on a 32k+ window), so
    # "near" is the steady state on a modest budget, not a rare event. The
    # loop once appended the same ~350-character sentence every
    # round — spending the budget narrating the budget, and moving the cached
    # prefix each time.
    rep = meter_at(2, 2 * one_call + 1.9 * EXPECT)
    v_a, m_a = rep.check(NEW, RESERVE)
    v_b, m_b = rep.check(NEW, RESERVE)
    v_c, m_c = rep.check(NEW, RESERVE)
    check("near speaks the first time it has something to say",
          v_a == "near" and m_a, str(v_a))
    check("near is SILENT on the immediately following identical check "
          "(no second copy of the same sentence)",
          (v_b, m_b) == ("ok", None), f"{v_b} / {str(m_b)[:60]}")
    check("...and silent again on the third",
          (v_c, m_c) == ("ok", None), f"{v_c} / {str(m_c)[:60]}")
    # It speaks again when the room actually shrinks.
    rep.note_call(LAST, model=GATE_MODEL)
    v_d, m_d = rep.check(NEW, RESERVE)
    check("near speaks again once the remaining-rounds figure has dropped",
          v_d in ("near", "stop") and bool(m_d), f"{v_d} / {str(m_d)[:60]}")
    check("a stop message is never suppressed by the dedup — it explains an "
          "answer the person did not ask for yet",
          all(meter_at(2, 2 * one_call + EXPECT / 2).check(NEW, RESERVE)[1]
              for _ in range(2)))

    # The token basis makes the same three decisions in its own unit.
    tok = ta.TurnSpend(model="not-in-any-catalog-xyz", allowance_usd=1.0,
                       ctx_budget=1_000, price=None)
    tok.note_call(LAST, model="not-in-any-catalog-xyz")
    tok.note_call(LAST, model="not-in-any-catalog-xyz")
    v_t, m_t = tok.check(NEW, RESERVE)
    check("tokens basis: an over-budget turn stops in its own unit",
          v_t == "stop", v_t)
    check("tokens basis: the message talks TOKENS and never dollars",
          "tokens" in (m_t or "") and "$" not in (m_t or ""), str(m_t)[:140])

    # ── MID-TURN FAILOVER: THE METER FOLLOWS THE MODEL THAT ANSWERED ──
    # Price and basis were once resolved ONCE from the routed
    # model. A failover then left the meter wrong in two directions at once:
    # note_call priced at the served model while project() kept quoting the
    # dead model's rates, and basis never moved — so a priced -> subscription
    # failover went on calling itself "usd" while adding $0 a call.
    # (a) THE ANTHROPIC LANE — a different served id that is NOT a failover.
    # This is the regression that shipped: the direct Anthropic wire rewrites
    # the route id on EVERY call (anthropic_api_model_id,
    # inference_config.py:129 -> provider_gateway.py:1283) and hands the dated
    # id back as `model` (:1381). Only the OpenRouter-keyed slug prices, so a
    # string-diff failover test read every single Anthropic call as "failover
    # to an unpriced model": basis flipped on call 1, spent_usd stayed None,
    # and a $1.00 budget silently became an 800k-token one on the primary
    # lane. Anthropic models MUST route through the Anthropic API here, so
    # this is not an edge case — it is the main path.
    check("the fixture reproduces the live shape: routed prices, the served "
          "wire id does not",
          ta.TurnSpend._route_is_priced(
              ta.TurnSpend._lookup_price(ANTHROPIC_ROUTED))
          and not ta.TurnSpend._route_is_priced(
              ta.TurnSpend._lookup_price(ANTHROPIC_SERVED)))
    anth = ta.TurnSpend.for_route(ANTHROPIC_ROUTED, "anthropic",
                                  allowance_usd=1.0, ctx_budget=40_000)
    for _ in range(3):
        anth.note_call(LAST, model=ANTHROPIC_SERVED)   # no failed_over key
    check("Anthropic lane: the basis STAYS usd across the whole turn",
          anth.basis == "usd" and anth.basis_changed_from is None,
          f"{anth.basis} / {anth.basis_changed_from}")
    check("Anthropic lane: dollars actually accrue, at the ROUTED id's price",
          abs(anth.spent_usd - 3 * one_call) < 1e-9,
          f"{anth.spent_usd} vs {3 * one_call}")
    check("Anthropic lane: the meter keeps pricing at the routed id and does "
          "not adopt an id the catalog cannot price",
          anth.model == ANTHROPIC_ROUTED, str(anth.model))
    check("Anthropic lane: the wire's id is still recorded as provenance",
          anth.served_model == ANTHROPIC_SERVED, str(anth.served_model))
    check("Anthropic lane: a $1.00 budget is still a DOLLAR budget",
          anth.allowance == 1.0 and anth.allowance_tokens is None,
          f"{anth.allowance} / {anth.allowance_tokens}")
    check("Anthropic lane: projection quotes real rates, not zero",
          anth.project(NEW, RESERVE) > 0, str(anth.project(NEW, RESERVE)))

    # (b) A GENUINE FAILOVER to a model with no rate — basis moves.
    FO_UNPRICED = {**LAST, "failed_over": {
        "from_provider": "openrouter", "from_model": GATE_MODEL,
        "to_provider": "somewhere", "to_model": "no-such-model-anywhere"}}
    fo = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                allowance_usd=1.0, ctx_budget=40_000)
    fo.note_call(LAST, model=GATE_MODEL)
    spent_before = fo.spent_usd
    check("failover: the priced first call is real money",
          spent_before > 0 and fo.basis == "usd", f"{spent_before}/{fo.basis}")
    fo.note_call(FO_UNPRICED, model="no-such-model-anywhere")
    check("a REAL failover (failed_over present) to an unpriced model moves "
          "the basis off usd rather than silently adding $0 a call",
          fo.basis == "tokens", fo.basis)
    check("failover: the dollars already spent STAND (they were really spent)",
          abs(fo.spent_usd - spent_before) < 1e-12, str(fo.spent_usd))
    check("failover: the change is REPORTED, so the panel can say the unit "
          "moved instead of showing a total that stopped growing",
          fo.summary().get("basis_changed_from") == "usd", str(fo.summary()))
    check("failover: token metering continues across the switch",
          fo.spent_tokens == 2 * (100_000 + 500), str(fo.spent_tokens))
    fo_nb = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                   allowance_usd=1.0, ctx_budget=None)
    fo_nb.note_call(LAST, model=GATE_MODEL)
    fo_nb.note_call(FO_UNPRICED, model="no-such-model-anywhere")
    check("failover with no context budget to fall back on → 'none' "
          "(nothing enforceable is left, and it says so)",
          fo_nb.basis == "none"
          and fo_nb.summary().get("basis_changed_from") == "usd",
          str(fo_nb.summary()))

    # (c) A FAILOVER TO A PRICED MODEL — price adopted, basis unchanged.
    FO_PRICED = {**LAST, "failed_over": {
        "from_provider": "openrouter", "from_model": GATE_MODEL,
        "to_provider": "openai", "to_model": FAILOVER_PRICED}}
    fp = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                allowance_usd=10.0, ctx_budget=40_000)
    fp.note_call(LAST, model=GATE_MODEL)
    first = fp.spent_usd
    fp.note_call(FO_PRICED, model=FAILOVER_PRICED)
    check("failover to a PRICED model keeps the usd basis",
          fp.basis == "usd" and fp.basis_changed_from is None, fp.basis)
    check("...adopts the new model for pricing",
          fp.model == FAILOVER_PRICED, str(fp.model))
    check("...and bills the second call at the NEW model's rates, not the "
          "old one's (this fixture's target is twice the price)",
          abs((fp.spent_usd - first) - 2 * first) < 1e-9,
          f"delta {fp.spent_usd - first} vs first {first}")
    # And the projection follows the rates too — the half the old code got
    # wrong even when it priced the call correctly.
    same = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                  allowance_usd=10.0, ctx_budget=40_000)
    same.note_call(LAST, model=GATE_MODEL)
    same.note_call(LAST, model=GATE_MODEL)
    check("...so later PROJECTIONS quote the failover target's rates, not the "
          "dead model's (exactly 2x here, since the target costs twice)",
          abs(fp.project(NEW, RESERVE) - 2 * same.project(NEW, RESERVE)) < 1e-12,
          f"{fp.project(NEW, RESERVE)} vs 2x {same.project(NEW, RESERVE)}")

    # The reverse basis move is deliberately NOT taken.
    back = ta.TurnSpend.for_route("not-in-any-catalog-xyz", "openrouter",
                                  allowance_usd=1.0, ctx_budget=40_000)
    back.note_call(LAST, model="not-in-any-catalog-xyz")
    back.note_call(LAST, model=GATE_MODEL)
    check("tokens -> usd is NOT taken: a turn already metering tokens has no "
          "dollar figure for what it spent, so switching would compare a "
          "partial spend against a full allowance",
          back.basis == "tokens", back.basis)
    check("...and the newly-priced model is still adopted for pricing",
          back.model == GATE_MODEL, str(back.model))

    # ── 6. THE BACKSTOP ──
    check("backstop: one constant, in the runaway band (>= 100), not the "
          "10-25 workflow band the old per-model tiers sat in",
          ta.BACKSTOP_TOOL_ROUNDS >= 100, str(ta.BACKSTOP_TOOL_ROUNDS))
    check("backstop: the default with no override",
          ta.backstop_rounds(None) == ta.BACKSTOP_TOOL_ROUNDS)
    check("backstop: behavioral_params override wins",
          ta.backstop_rounds({"max_tool_rounds": 7}) == 7)
    check("backstop: DISABLED by <= 0 (a number no turn can reach)",
          ta.backstop_rounds({"max_tool_rounds": 0}) >= 1_000_000
          and ta.backstop_rounds({"max_tool_rounds": -1}) >= 1_000_000,
          str(ta.backstop_rounds({"max_tool_rounds": 0})))
    check("backstop: garbage falls back to the default rather than raising "
          "(a bad dial must never fail a turn)",
          ta.backstop_rounds({"max_tool_rounds": "banana"})
          == ta.BACKSTOP_TOOL_ROUNDS)
    _saved_env = os.environ.get("CSERIES_MAX_TOOL_ROUNDS")
    try:
        os.environ["CSERIES_MAX_TOOL_ROUNDS"] = "3"
        check("backstop: env override applies when behavioral_params is silent",
              ta.backstop_rounds(None) == 3 and ta.backstop_rounds({}) == 3)
        check("backstop: behavioral_params still outranks the env",
              ta.backstop_rounds({"max_tool_rounds": 9}) == 9)
        os.environ["CSERIES_MAX_TOOL_ROUNDS"] = "0"
        check("backstop: env can disable it too",
              ta.backstop_rounds(None) >= 1_000_000)
    finally:
        if _saved_env is None:
            os.environ.pop("CSERIES_MAX_TOOL_ROUNDS", None)
        else:
            os.environ["CSERIES_MAX_TOOL_ROUNDS"] = _saved_env


def catalog_checks(check, ta) -> None:
    """for_route against the real catalog + estimator, so the wiring between
    this module and server/routes/inference.py is exercised, not assumed."""
    m = ta.TurnSpend.for_route(GATE_MODEL, "openai",
                               allowance_usd=0.25, ctx_budget=40_000)
    check("for_route: a catalogued priced model resolves the usd basis",
          m.basis == "usd", m.basis)
    m.note_call({"input_total_tokens": 100_000, "cache_read_tokens": 80_000,
                 "cache_write_tokens": 10_000, "completion_tokens": 500},
                model=GATE_MODEL)
    expect = (10_000 * IN_RATE + 80_000 * READ_RATE
              + 10_000 * WRITE_RATE + 500 * OUT_RATE)
    check("for_route: the real estimator prices the call the same way the "
          "cost log does (cache-aware, four rates)",
          abs(m.spent_usd - expect) < 1e-9, f"{m.spent_usd} vs {expect}")
    m_nc = ta.TurnSpend.for_route(GATE_MODEL_NOCACHE, "openai",
                                  allowance_usd=0.25, ctx_budget=40_000)
    check("for_route: a model publishing no cache rates is still usd-based",
          m_nc.basis == "usd", m_nc.basis)
    m_free = ta.TurnSpend.for_route(GATE_MODEL_FREE, "openai",
                                    allowance_usd=0.25, ctx_budget=40_000)
    check("for_route: a zero-rate catalog row does NOT become a usd basis",
          m_free.basis == "tokens", m_free.basis)
    m_missing = ta.TurnSpend.for_route("not-in-any-catalog-xyz", "openai",
                                       allowance_usd=0.25, ctx_budget=40_000)
    check("for_route: an uncatalogued model falls back to tokens",
          m_missing.basis == "tokens", m_missing.basis)
    m_no_budget = ta.TurnSpend.for_route("not-in-any-catalog-xyz", "openai",
                                         allowance_usd=0.25, ctx_budget=None)
    check("for_route: uncatalogued AND no context budget → 'none'",
          m_no_budget.basis == "none", m_no_budget.basis)


async def persistence_checks(check, cb) -> None:
    """Both co-located stores round-trip, against a fake pool."""
    WB = {"kind": "project", "ref": "7"}
    CIRCLES = {"kind": "app", "ref": "circles"}

    check("no behavioral_params at all → None",
          await cb.read_thread_allowance(FakePool(None), 1, WB) is None)
    check("empty behavioral_params → None",
          await cb.read_thread_allowance(FakePool({}), 1, WB) is None)
    check("no source context → None (there is no thread to read)",
          await cb.read_thread_allowance(FakePool({}), 1, None) is None)

    bp = {
        "turn_allowances": {"project:7": 0.75, "app:workbench": 2.5},
        "circles_thread_models": {"self": {"provider": "self", "model_id": "m",
                                           "context_budget": 55_000,
                                           "turn_allowance_usd": 1.25}},
    }
    check("workbench store: turn_allowances['project:7'] round-trips",
          await cb.read_thread_allowance(FakePool(bp), 1, WB) == 0.75,
          str(await cb.read_thread_allowance(FakePool(bp), 1, WB)))
    check("circles store: circles_thread_models.self.turn_allowance_usd",
          await cb.read_thread_allowance(FakePool(bp), 1, CIRCLES) == 1.25,
          str(await cb.read_thread_allowance(FakePool(bp), 1, CIRCLES)))
    check("an unset scope reads None, not another thread's value",
          await cb.read_thread_allowance(
              FakePool(bp), 1, {"kind": "project", "ref": "999"}) is None)
    # The scoped key wins over the circles co-location for the circles thread.
    bp_both = dict(bp)
    bp_both["turn_allowances"] = dict(bp["turn_allowances"],
                                      **{"app:circles": 3.5})
    check("the scoped key outranks the circles co-located entry "
          "(same precedence read_thread_budget uses)",
          await cb.read_thread_allowance(FakePool(bp_both), 1, CIRCLES) == 3.5)

    check("behavioral_params handed back as TEXT (asyncpg codec-dependent) "
          "parses the same",
          await cb.read_thread_allowance(
              FakePool(bp, as_text=True), 1, WB) == 0.75)
    check("a pool that raises degrades to None, never propagates",
          await cb.read_thread_allowance(FakePool(bp, raises=True), 1, WB)
          is None)

    for bad, label in ((0, "zero"), (-1.0, "negative"), ("abc", "unparseable"),
                       (None, "null")):
        check(f"a {label} stored allowance reads as UNSET, never as "
              "'spend nothing'",
              await cb.read_thread_allowance(
                  FakePool({"turn_allowances": {"project:7": bad}}), 1, WB)
              is None)

    # The two dials are independent: reading one must never disturb the other.
    check("the context budget still reads from its own key alongside the "
          "allowance (the two dials do not share storage)",
          await cb.read_thread_budget(FakePool(bp), 1, CIRCLES) == 55_000)

    # ── WHAT IS SAVED IS WHAT IS READ (circles per-thread entries) ──
    # circles keys its entries by _thread_key (routes/circles.py:471): "self"
    # for the self-thread, "<kind>:<id>" for every other. The reader only ever
    # looked at ["self"], so a value saved on any other circles thread went to
    # a key nothing consulted — saved, invisible, governing nothing. Both
    # dials had it; both are fixed by the same lookup.
    PEER = {"kind": "peer", "ref": "friend@example.com"}
    bp_dm = {"circles_thread_models": {
        "dm:friend@example.com": {"context_budget": 33_000,
                                  "turn_allowance_usd": 0.4}}}
    check("a dm entry's allowance is READ on the circles surface, not "
          "orphaned at a key nobody consults",
          await cb.read_thread_allowance(
              FakePool(bp_dm), 1, PEER, "circles") == 0.4,
          str(await cb.read_thread_allowance(FakePool(bp_dm), 1, PEER, "circles")))
    check("the same fix reaches the context dial, which had the bug first",
          await cb.read_thread_budget(
              FakePool(bp_dm), 1, PEER, "circles") == 33_000)
    bp_ent = {"circles_thread_models": {
        "entity:42": {"turn_allowance_usd": 0.9}}}
    check("an entity entry is read too (dm and entity both map to peer:<id>)",
          await cb.read_thread_allowance(
              FakePool(bp_ent), 1, {"kind": "peer", "ref": "42"},
              "circles") == 0.9)
    check("a peer scope does NOT pick up the self-thread's value",
          await cb.read_thread_allowance(
              FakePool({"circles_thread_models":
                        {"self": {"turn_allowance_usd": 7.0}}}),
              1, PEER, "circles") is None)
    check("the scoped turn_allowances key still outranks the circles entry",
          await cb.read_thread_allowance(
              FakePool({"turn_allowances": {"peer:friend@example.com": 2.0},
                        **bp_dm}), 1, PEER, "circles") == 2.0)
    check("a circle thread (no source context at all) reads None",
          await cb.read_thread_allowance(FakePool(bp_dm), 1, None, "circles")
          is None)

    # THE LEGACY PEER FALLBACK IS SURFACE-GATED. A peer source context carries
    # no surface marker, and three places build one — circles
    # (routes/circles.py:1161), memory_engine.derive_source_context (:1042)
    # and synaptive_handler (:981), the inbound host-to-host lane. Ungated, a
    # circles dm entry would supply the budget for a synaptive-lane turn with
    # the same peer, which nobody set it for. Only the circles panel says
    # "circles"; the turn path never does.
    check("a peer turn from ANOTHER surface does not inherit the circles dm "
          "entry (this is the turn path's call — no surface argument)",
          await cb.read_thread_allowance(FakePool(bp_dm), 1, PEER) is None,
          str(await cb.read_thread_allowance(FakePool(bp_dm), 1, PEER)))
    check("...and the context dial is gated the same way",
          await cb.read_thread_budget(FakePool(bp_dm), 1, PEER) is None)
    check("an explicitly non-circles surface is refused too",
          await cb.read_thread_allowance(
              FakePool(bp_dm), 1, PEER, "synaptive") is None)
    check("the self-thread branch needs NO gate — app:circles names the "
          "surface itself",
          await cb.read_thread_allowance(
              FakePool({"circles_thread_models":
                        {"self": {"turn_allowance_usd": 1.5}}}),
              1, CIRCLES) == 1.5)

    # ── last_turn_spend IS SCOPED TO THE THREAD ──
    # The prompt blocks are scoped the same way: an
    # entity-wide read renders whichever thread spoke last onto the thread you
    # are looking at. "What your last turn cost" carries exactly the same
    # hazard, and the projection built on it inherits the error.
    pool = FakePool({})
    await cb.last_turn_spend(pool, 1, {"kind": "project", "ref": "7"})
    check("last_turn_spend scopes a PROJECT thread by project_id, not "
          "entity-wide",
          "project_id" in pool.last_sql and 7 in pool.last_args,
          f"{pool.last_sql[:200]} :: {pool.last_args}")
    pool2 = FakePool({})
    await cb.last_turn_spend(pool2, 1, CIRCLES)
    check("last_turn_spend scopes an APP thread by the source_context "
          "descriptor (the same predicate memory_engine._turn_row_where "
          "gives recall, so the two cannot drift)",
          "source_context" in pool2.last_sql and "circles" in pool2.last_args,
          f"{pool2.last_sql[:200]} :: {pool2.last_args}")
    pool3 = FakePool({})
    await cb.last_turn_spend(pool3, 1, {"kind": "peer", "ref": "h-9"})
    check("last_turn_spend scopes a PEER thread too",
          "'peer'" in pool3.last_sql and "h-9" in pool3.last_args,
          f"{pool3.last_sql[:200]} :: {pool3.last_args}")
    pool4 = FakePool({})
    await cb.last_turn_spend(pool4, 1, None)
    check("no thread descriptor reads entity-wide rather than scoping to an "
          "empty session (which would return nothing on every thread)",
          "source_session" not in pool4.last_sql and pool4.last_args == (1,),
          f"{pool4.last_sql[:200]} :: {pool4.last_args}")
    check("every scoped query still filters to live turn rows",
          all(x in pool2.last_sql for x in ("memory_type = 'turn'",
                                            "status = 'active'",
                                            "context_ledger")),
          pool2.last_sql[:240])


def wire_checks(check, ta) -> None:
    """EXECUTED end-to-end checks, not source greps.

    These three were once source assertions: they matched strings
    in session.py and proved only that the strings were there. Each one now
    runs the real code over real shapes, so it fails if the behaviour changes
    even when the source still reads correctly.
    """
    # ── (a) A RECORDED PASSTHROUGH USAGE OBJECT, ALL THE WAY TO THE METER ──
    # The shape that motivated the cache-aware pricing ship: a provider
    # relaying Anthropic's usage verbatim inside an OpenAI-shaped object,
    # where the reported input EXCLUDES the cached counts. If the meter ever
    # reads that raw, uncached goes negative, clamps to 0, and the turn's real
    # uncached input prices at $0 — an undercount in the one lane a budget is
    # built on.
    try:
        from mastercode.provider_gateway import _normalize_openai_usage
    except Exception as e:  # noqa: BLE001
        check("provider_gateway importable for the usage-shape check", False, str(e))
        return
    passthrough = {                      # input EXCLUDES the cache counts
        "total_tokens": 12_700,
        "prompt_tokens": 2_000,
        "completion_tokens": 700,
        "cache_read_input_tokens": 8_000,
        "cache_creation_input_tokens": 2_000,
    }
    total, prompt, completion, c_read, c_write = _normalize_openai_usage(
        passthrough, provider="openrouter", model=GATE_MODEL)
    check("normalisation makes prompt_tokens the TRUE total input "
          "(2k uncached + 8k read + 2k written = 12k)",
          prompt == 12_000, f"got {prompt}")
    lane = {"input_total_tokens": prompt, "completion_tokens": completion,
            "cache_read_tokens": c_read, "cache_write_tokens": c_write,
            "tokens_used": total}
    m = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                               allowance_usd=5.0, ctx_budget=40_000)
    entry = m.note_call(lane, model=GATE_MODEL)
    check("the meter lands the right split off a real passthrough usage "
          "object: 2k uncached, 8k read, 2k written, 700 out",
          (entry["in"] - entry["cached"] - entry["cache_write"]) == 2_000
          and entry["cached"] == 8_000 and entry["cache_write"] == 2_000
          and entry["out"] == 700, str(entry))
    expect = (2_000 * IN_RATE + 8_000 * READ_RATE
              + 2_000 * WRITE_RATE + 700 * OUT_RATE)
    check("...and prices it at the four rates, with the uncached share NOT "
          "silently charged at $0",
          abs(m.spent_usd - expect) < 1e-9, f"{m.spent_usd} vs {expect}")
    check("the turn's by-kind totals match the call that produced them",
          m.tokens == {"uncached": 2_000, "cache_read": 8_000,
                       "cache_write": 2_000, "output": 700}, str(m.tokens))
    # Fed RAW (un-normalised), the same call would price its uncached input at
    # nothing. This is the regression the normalisation exists to stop, and it
    # is asserted so nobody "simplifies" the lane dict back to the wire shape.
    raw = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                 allowance_usd=5.0, ctx_budget=40_000)
    raw.note_call({"input_total_tokens": passthrough["prompt_tokens"],
                   "completion_tokens": 700,
                   "cache_read_tokens": 8_000, "cache_write_tokens": 2_000},
                  model=GATE_MODEL)
    check("the un-normalised shape really would undercount (so the check "
          "above is load-bearing, not decoration)",
          raw.spent_usd < m.spent_usd, f"{raw.spent_usd} vs {m.spent_usd}")

    # ── (b) THE LOOP'S OWN _new_tok ARITHMETIC, ON A REALISTIC SLICE ──
    # turn_allowance.new_input_tokens is the function session.py calls; the
    # gate runs THAT, not a copy of it.
    msgs = [{"role": "system", "content": "IDENTITY: host."},
            {"role": "user", "content": "find every caller"}]
    mark = len(msgs)                      # where the last model call stood
    msgs.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "code_grep", "arguments": '{"q": "x"}'}}]})
    for i in range(3):
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": "R" * 30_000})       # three 30KB results
    new_tok = ta.new_input_tokens(msgs, mark)
    # 3 x 30000 chars of results + the tool_calls message, at 4 chars/token.
    check("new_input_tokens counts the whole appended tail (~22.5k tokens "
          "for three 30KB tool results plus the tool_calls message)",
          22_500 <= new_tok <= 23_000, str(new_tok))
    check("new_input_tokens counts only what came AFTER the mark "
          "(the system block and the question are already paid for)",
          ta.new_input_tokens(msgs, 0) > new_tok)
    check("a mark past the end (the list was trimmed shorter) reads 0, never "
          "raises", ta.new_input_tokens(msgs, 999) == 0)
    check("an empty list reads 0", ta.new_input_tokens([], 0) == 0)

    # And the verdict that slice actually produces. A 22.5k-token tool haul on
    # top of the call above, against an allowance with no room for it, stops.
    tight = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                   allowance_usd=5.0, ctx_budget=40_000)
    tight.note_call(lane, model=GATE_MODEL)
    tight.note_call(lane, model=GATE_MODEL)
    projected = tight.project(new_tok, 16_384)
    tight.allowance_usd = tight.spent_usd + projected / 2      # cannot afford it
    verdict, msg = tight.check(new_tok, 16_384)
    check("the loop's real slice + a budget that cannot cover it → 'stop'",
          verdict == "stop" and "BUDGET REACHED" in (msg or ""),
          f"{verdict} / {str(msg)[:80]}")
    roomy = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                   allowance_usd=5.0, ctx_budget=40_000)
    roomy.note_call(lane, model=GATE_MODEL)
    roomy.note_call(lane, model=GATE_MODEL)
    roomy.allowance_usd = roomy.spent_usd + 50 * projected
    check("the same slice with room to spare → 'ok', no message",
          roomy.check(new_tok, 16_384) == ("ok", None))

    # ── (c) THE LEDGER ROUND-TRIP, TRUNCATION AND ALL ──
    # 40 calls metered, per_call stored as [:24] exactly as session.py does,
    # read back through last_turn_spend. The totals must be the turn's, not
    # the visible 24's, and the projection must get call #40.
    import asyncio, json as _json
    big = ta.TurnSpend.for_route(GATE_MODEL, "openrouter",
                                 allowance_usd=100.0, ctx_budget=40_000)
    per_call = []
    for i in range(40):
        per_call.append(big.note_call(
            {"input_total_tokens": 10_000 + i, "cache_read_tokens": 6_000,
             "cache_write_tokens": 1_000, "completion_tokens": 100 + i},
            model=GATE_MODEL))
    ledger = {"calls": len(per_call), "per_call": per_call[:24],
              "spend": {**big.summary(), "stopped_on_allowance": False}}
    check("the truncated list really is short (the bug's precondition)",
          len(ledger["per_call"]) == 24 and ledger["calls"] == 40)

    class LedgerPool:
        async def fetchrow(self, _sql, *_a):
            return {"ledger": _json.dumps(ledger)}

    from mastercode import context_budget as cb
    last_turn, last_call = asyncio.run(
        cb.last_turn_spend(LedgerPool(), 1, None))
    check("round-trip: spent_usd is the WHOLE turn's, not the visible 24's",
          abs(last_turn["spent_usd"] - round(big.spent_usd, 6)) < 1e-6,
          f'{last_turn["spent_usd"]} vs {big.spent_usd}')
    visible = sum(c.get("usd") or 0 for c in ledger["per_call"])
    check("...and it is materially more than summing the truncated list "
          "(the exact failure)",
          last_turn["spent_usd"] > visible * 1.3,
          f'{last_turn["spent_usd"]} vs truncated {visible}')
    check("round-trip: calls reports 40, not 24",
          last_turn["calls"] == 40, str(last_turn["calls"]))
    check("round-trip: the token lines are the whole turn's too",
          last_turn["tokens"] == big.tokens,
          f'{last_turn["tokens"]} vs {big.tokens}')
    check("round-trip: the truncation is declared, so a reader knows the "
          "list is a sample",
          last_turn["per_call_truncated"] is True)
    check("round-trip: the projection gets call #40, not per_call[-1] (#24)",
          last_call == per_call[-1] and last_call["out"] == 139,
          f'{last_call} vs #24 {per_call[23]}')
    # Older rows have no `spend` key at all and must still read back.
    legacy = {"calls": 3, "per_call": per_call[:3]}

    class LegacyPool:
        async def fetchrow(self, _sql, *_a):
            return {"ledger": _json.dumps(legacy)}

    lt_old, lc_old = asyncio.run(
        cb.last_turn_spend(LegacyPool(), 1, None))
    check("a pre-2026-09-16 row with no `spend` key still reads back off the "
          "per-call list", lt_old["calls"] == 3 and lc_old == per_call[2],
          str(lt_old))
    check("...and an untruncated legacy row is not flagged as truncated",
          lt_old["per_call_truncated"] is False)



def nudge_metering_check(check) -> None:
    """The nudge re-call must be metered, and must advance the mark.

    STRUCTURAL, and said plainly rather than dressed up: this one branch fires
    only when a person POSTs /api/chat/nudge mid-tool-loop, so reaching it in
    a gate would mean standing up the whole turn path and a live provider.
    What IS asserted is scoped to the branch rather than the file — the span
    between the nudge's own model call and the `continue` that ends it — so it
    cannot pass on an `_acc_usage` that belongs to some other call site.

    What it guards: this call was once the one model call in the
    turn that no accumulator saw. Its tokens were missing from the usage frame
    and its dollars invisible to the allowance, so a turn could be nudged
    repeatedly and spend past its budget unnoticed; and because _acc_usage is
    also what advances `_msgs_mark`, the next round re-counted the nudge text
    as new input.
    """
    path = None
    for cand in ("brain/mastercode/runtime/session.py",
                 "/app/brain/mastercode/runtime/session.py"):
        if os.path.exists(cand):
            path = cand
            break
    if path is None:
        check("session.py locatable for the nudge check", False, "not found")
        return
    src = open(path, encoding="utf-8").read()
    i = src.find("_cm_nudge(")
    check("the nudge's own model call is still there to meter", i > 0)
    if i <= 0:
        return
    j = src.find("continue", i)
    branch = src[i:j] if j > i else ""
    check("the nudge call is metered in the same branch (its dollars reach "
          "the allowance, not just the token counters)",
          "_acc_usage(result)" in branch, branch[-200:])
    check("...and its tokens reach the usage frame too",
          "_prompt_acc" in branch and "_completion_acc" in branch)
    check("_acc_usage is what advances the mark, so metering the nudge also "
          "stops the next round double-counting the nudge text",
          "_msgs_mark[0] = len(grok_messages)" in src
          and "_acc_usage" in src)


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [turn-allowance] OK {name}")
        else:
            failures.append(name)
            print(f"  [turn-allowance] FAIL {name} {detail}")

    from mastercode.runtime import turn_allowance as ta
    from mastercode import context_budget as cb

    # The pricing lane reads the inference catalog cache, so seed it once
    # around every section rather than per-check: price_checks meters real
    # completed calls through estimate_call_cost_detail (it must — a mid-turn
    # failover changes the model, so the SERVED id is what gets priced).
    restore = seed_catalog()
    if restore is None:
        check("catalog wiring (server.routes.inference importable)", False,
              "could not import server.routes.inference — run this in the "
              "container or from the repo root")
        return 1
    try:
        price_checks(check, ta)
        catalog_checks(check, ta)
        wire_checks(check, ta)
    finally:
        restore()
    asyncio.run(persistence_checks(check, cb))
    nudge_metering_check(check)

    if failures:
        print(f"  [turn-allowance] {len(failures)} FAILURES: {failures}")
        return 1
    print("  [turn-allowance] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
