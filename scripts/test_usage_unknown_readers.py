#!/usr/bin/env python3
"""A model call whose provider reported no usage is UNKNOWN (migrations/v214,
provider_gateway._call_remote: NULL tokens, metadata usage=not_reported, the
per-call entry in/out None). Three readers turned that unknown back into a
zero that reads as a free call; this drives each one.

  1. inference.reprice_unpriced runs at every pod start (warm_model_catalog)
     over cost_method='unpriced' AND purpose='inference', which is exactly the
     row an unreported call writes. It priced `int(tokens_in or 0)` = 0 at the
     list rate and restated the row as cost_method='list', est_cost_usd 0.0,
     a priced free call.
  2. inference.estimate_turn_cost priced a turn none of whose calls reported
     usage at (0, 0) tokens: "$0.00, list" under the turn, beside tokens None.
  3. context_budget.last_turn_spend fell back to per_call[-1] for the
     projection when the meter measured no call; on an empty-reply turn that
     entry is the unreported one, and the next turn projected at $0.00.
  4. A turn where only SOME calls reported is priced from those calls alone.
     estimate_turn_cost labelled that 'list' and last_turn_spend gave it as
     the turn's whole cost; both now say the figure is partial, and
     last_turn_spend counts unreported calls from the row's
     tokens_unknown_calls (the stored per-call list is cut at 24).

Run: APP_ROOT=<repo> PYTHONPATH=brain:. python3 scripts/test_usage_unknown_readers.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))
logging.disable(logging.CRITICAL)

failures: list = []


def check(label, ok, detail=""):
    print(f"[usage-unknown] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    from server import brain_service
    from server.routes import inference as inf
    from mastercode import context_budget as cb

    # One list-priced model, one the catalog does not know.
    price = inf.ModelPrice(0.000001, 0.000002, None, None)
    saved = (inf._price_for, inf._direct_models, brain_service.get_pool)
    inf._price_for = lambda mid: price if mid == "m-list" else None

    async def _no_warm(_p):
        return [], None
    inf._direct_models = _no_warm

    unknown_call = {"in": None, "cached": None, "cache_write": None, "out": None,
                    "usage": "not_reported"}
    reported_call = {"in": 1000, "cached": 0, "cache_write": 0, "out": 100}
    try:
        # ── 1. reprice_unpriced leaves an unreported row unpriced ───────────
        sent: list = []
        updates: list = []

        class _Pool:
            async def fetch(self, sql, *a):
                sent.append(sql)
                # The rows the pre-fix query matched: an unreported call (NULL
                # tokens) and an ordinary unpriced one written on a cold catalog.
                return [
                    {"id": 1, "model_id": "m-list", "provider": "p", "tokens_in": None,
                     "tokens_out": None, "cache_read_tokens": None,
                     "cache_write_tokens": None,
                     "metadata": json.dumps({"usage": "not_reported",
                                             "cache_read_tokens": None,
                                             "cache_write_tokens": None})},
                    {"id": 2, "model_id": "m-list", "provider": "p", "tokens_in": 1000,
                     "tokens_out": 100, "cache_read_tokens": 0, "cache_write_tokens": 0,
                     "metadata": json.dumps({})},
                ]

            async def executemany(self, sql, rows):
                updates.append((sql, list(rows)))

        brain_service.get_pool = lambda: _Pool()
        asyncio.run(inf.reprice_unpriced())
        touched = {r[0] for _sql, rows in updates for r in rows}
        check("reprice: an ordinary unpriced row is priced (the control)", 2 in touched,
              repr(updates))
        check("reprice: a row whose provider reported no usage is never restated "
              "as a priced $0 call", 1 not in touched, repr(updates))
        check("reprice: the query itself leaves NULL-token rows out",
              bool(sent) and "tokens_in IS NOT NULL" in sent[0], sent[:1])

        # ── 2. estimate_turn_cost: no call reported -> no price ────────────
        usd, basis = inf.estimate_turn_cost("m-list", "p", 0, 0,
                                            per_call=[unknown_call, unknown_call])
        check("turn price: a turn none of whose calls reported usage is 'unpriced', "
              "never $0 at list", usd is None and basis == "unpriced", repr((usd, basis)))
        usd, basis = inf.estimate_turn_cost("m-list", "p", 1000, 100,
                                            per_call=[reported_call, unknown_call])
        check("turn price: a turn where only some calls reported is priced from them "
              "and says the figure is 'partial', never 'list'",
              basis == "partial" and usd and usd > 0, repr((usd, basis)))
        usd, basis = inf.estimate_turn_cost("m-list", "p", 2000, 200,
                                            per_call=[reported_call, reported_call])
        check("turn price: every call reported -> 'list' (the control)",
              basis == "list" and usd and usd > 0, repr((usd, basis)))
        usd, basis = inf.estimate_turn_cost(
            "m-list", "xai", 1000, 100,
            per_call=[{**reported_call, "credential": "api_key"},
                      {**unknown_call, "credential": "signin"}])
        check("turn price: an unreported call on the sign-in leaves the key's price "
              "whole ('list')", basis == "list" and usd and usd > 0, repr((usd, basis)))
        usd, basis = inf.estimate_turn_cost(
            "m-list", "xai", 0, 0,
            per_call=[{**unknown_call, "credential": "signin"}])
        check("turn price: unreported calls on the sign-in are still the plan",
              usd is None and basis == "subscription", repr((usd, basis)))
        usd, basis = inf.estimate_turn_cost(
            "m-list", "xai", 0, 0,
            per_call=[{**unknown_call, "credential": "api_key"}])
        check("turn price: unreported calls on the API key are 'unpriced', never $0",
              usd is None and basis == "unpriced", repr((usd, basis)))

        # ── 3. last_turn_spend: the projection never stands on an unknown ──
        def _row(per_call, spend, unknown_calls=None):
            class _P:
                async def fetchrow(self, *a, **k):
                    return {"ledger": {"per_call": per_call, "spend": spend},
                            "unknown_calls": unknown_calls}
            return _P()

        _lt, last = asyncio.run(cb.last_turn_spend(
            _row([reported_call, unknown_call],
                 {"calls": 2, "last_call": None, "tokens": None}), 1))
        check("projection: the last call that reported usage, not the unknown one after it",
              last == reported_call, repr(last))
        _lt, last = asyncio.run(cb.last_turn_spend(
            _row([unknown_call, unknown_call], {"calls": 2}), 1))
        check("projection: no call reported usage -> no basis to project from",
              last is None, repr(last))
        _lt, last = asyncio.run(cb.last_turn_spend(_row([reported_call], {}), 1))
        check("projection: an old row with no spend still reads its last call (the control)",
              last == reported_call, repr(last))

        # ── 4. last_turn_spend: the panel's "actually cost" says partial ───
        lt, _last = asyncio.run(cb.last_turn_spend(
            _row([reported_call, unknown_call],
                 {"calls": 2, "spent_usd": 0.0012, "tokens": None}), 1))
        check("last turn: some calls unreported -> the figure is marked partial",
              lt and lt.get("spent_partial") is True and lt.get("spent_usd") == 0.0012,
              repr(lt))
        # 30 calls, the stored list cut at 24 with every unreported one past
        # the cut: only the row's own count knows.
        lt, _last = asyncio.run(cb.last_turn_spend(
            _row([reported_call] * 24, {"calls": 30, "spent_usd": 0.03, "tokens": None},
                 unknown_calls="6"), 1))
        check("last turn: the row's tokens_unknown_calls counts calls past the 24 kept",
              lt and lt.get("spent_partial") is True and lt.get("calls_usage_unknown") == 6,
              repr(lt))
        lt, _last = asyncio.run(cb.last_turn_spend(
            _row([unknown_call, unknown_call],
                 {"calls": 2, "spent_usd": 0.0, "tokens": None}, unknown_calls=2), 1))
        check("last turn: no call reported -> no dollar figure, never $0",
              lt and lt.get("spent_usd") is None and not lt.get("spent_partial"), repr(lt))
        lt, _last = asyncio.run(cb.last_turn_spend(
            _row([reported_call], {"calls": 1, "spent_usd": 0.0012, "tokens": None}), 1))
        check("last turn: every call reported -> whole, not partial (the control)",
              lt and lt.get("spent_partial") is False and lt.get("spent_usd") == 0.0012,
              repr(lt))
    finally:
        inf._price_for, inf._direct_models, brain_service.get_pool = saved

    print(f"\n[usage-unknown] {'all passed' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
