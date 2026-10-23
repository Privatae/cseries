#!/usr/bin/env python3
"""Gate: a turn is bounded by default, in history and in spend, and the owner
can change both (context_budget.read_turn_defaults, derive_turn_budget,
runtime/turn_allowance.TurnSpend, PUT /api/brain/turn-defaults).

Until 2026-10-19 the history budget was the whole window (fill-to-fit) and no
spend limit applied unless one was set: only the 200-round backstop stopped a
turn. Here:

  H. the default history ceiling is DEFAULT_HISTORY_CEILING_TOKENS (32,000),
     well under every window; the window's own ceiling still holds when it is
     smaller; the per-thread dial may go above the default up to the window's
     ceiling; 0 is fill-to-fit;
  S. with no dollar figure set, a turn is held to DEFAULT_TURN_ROUNDS
     full-context rounds and a scheduled run to DEFAULT_SCHEDULED_ROUNDS, in
     tokens on every route; a dollar figure, when set, is the allowance; 0
     rounds turns the default off;
  O. the owner's values override the built-ins (behavioral_params), are read
     back, and are refused when they are not numbers or not positive;
  W. session.py reads them for every turn and a scheduled run takes its own.

Pure: fake pools, no database. APP_ROOT=<repo> python3 scripts/<this>.py
"""
import re
import asyncio
import json
import os
import pathlib
import sys
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]
FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


from mastercode import context_budget as cb  # noqa: E402
from mastercode.runtime import turn_allowance as ta  # noqa: E402

print("H. history")
check("H the default ceiling is 32,000 tokens", getattr(cb, "DEFAULT_HISTORY_CEILING_TOKENS", None) == 32_000)
W1M = dict(window=1_000_000, output_reserve=8_000, system_tokens=20_000)
try:
    d_ceiling = cb.derive_turn_budget(**W1M, ceiling=32_000)
except TypeError as e:
    d_ceiling = repr(e)
check("H on a 1M window the default is the ceiling, not the window", d_ceiling == 32_000, d_ceiling)
check("H a small window's own ceiling still holds",
      cb.derive_turn_budget(window=16_000, output_reserve=4_000, system_tokens=6_000, ceiling=32_000)
      == int((16_000 - 4_000 - 6_000) * 0.95))
check("H the per-thread dial may go above the default, up to the window's ceiling",
      cb.derive_turn_budget(**W1M, override=200_000, ceiling=32_000) == 200_000
      and cb.derive_turn_budget(**W1M, override=5_000_000, ceiling=32_000) == int((1_000_000 - 28_000) * 0.95))
check("H 0 is fill-to-fit", cb.derive_turn_budget(**W1M, ceiling=0) == int((1_000_000 - 28_000) * 0.95))


class _Pool:
    def __init__(self, bp):
        self.bp, self.writes = bp, []

    async def fetchrow(self, sql, *a):
        return {"behavioral_params": json.dumps(self.bp) if self.bp is not None else None}

    async def execute(self, sql, *a):
        self.writes.append(a)
        self.bp = json.loads(a[1])
        return "UPDATE 1"


print("S. spend")
d = asyncio.run(cb.read_turn_defaults(_Pool(None), 1))
check("S the built-ins: 20 rounds a turn, 10 a scheduled run, no dollar figure",
      d == {"history_ceiling_tokens": 32_000, "turn_allowance_usd": None, "scheduled_task_allowance_usd": None,
            "turn_rounds": 20, "scheduled_rounds": 10}, d)
check("S DEFAULT_TURN_ROUNDS is the researched workflow ceiling (TOKEN_ALLOWANCE_MULTIPLIER)",
      ta.DEFAULT_TURN_ROUNDS == ta.TOKEN_ALLOWANCE_MULTIPLIER == 20 and ta.DEFAULT_SCHEDULED_ROUNDS == 10)
for priced in (True, False):
    sp = ta.TurnSpend(model="m", allowance_usd=None, ctx_budget=40_000, priced=priced, default_rounds=20)
    check(f"S no figure set ({'priced' if priced else 'unpriced'} route): the default bounds it, in tokens",
          sp.basis == "tokens" and sp.allowance_tokens == 800_000 and sp.default_in_force, (sp.basis, sp.allowance))
sp = ta.TurnSpend(model="m", allowance_usd=None, ctx_budget=40_000, priced=True, default_rounds=10)
check("S a scheduled run's default is 10 rounds", sp.allowance_tokens == 400_000)
sp = ta.TurnSpend(model="m", allowance_usd=1.5, ctx_budget=40_000, priced=True, default_rounds=20)
check("S a dollar figure, when set, is the allowance", sp.basis == "usd" and sp.allowance == 1.5
      and not sp.default_in_force)
sp = ta.TurnSpend(model="m", allowance_usd=None, ctx_budget=40_000, priced=True, default_rounds=0)
check("S 0 rounds turns the default off", sp.basis == "none")
check("S the turn's ledger says when the default bounded it",
      ta.TurnSpend(model="m", allowance_usd=None, ctx_budget=1000, priced=False, default_rounds=20)
      .summary().get("allowance_default_rounds") == 20)

print("U. a route whose window is unknown")
# context_budget.model_context_window returns None for a model the catalog,
# the provider's list and the static table do not know (a local model under
# its own name): the turn then runs on the legacy history caps with no
# context budget, and the default above, metered in context budgets, never
# engaged, so the turn ran to BACKSTOP_TOOL_ROUNDS.
_mb = getattr(ta, "meter_budget", None)
try:
    unit = _mb(None, allowance_usd=None, default_rounds=20, system_tokens=15_000, history_tokens=2_000)
except TypeError as e:
    unit = repr(e)
check("U no dollar figure and an unknown window: a round is the system blocks plus the legacy history",
      unit == 17_000, unit)
sp = ta.TurnSpend(model="m", allowance_usd=None, ctx_budget=unit if isinstance(unit, int) else None,
                  priced=False, default_rounds=10)
check("U ...so the default bounds the turn, in tokens (a scheduled run: 10 such rounds)",
      sp.basis == "tokens" and sp.default_in_force and sp.allowance_tokens == 170_000,
      (sp.basis, sp.allowance_tokens))
check("U a known window meters in the turn's own context budget",
      callable(_mb) and _mb(40_000, allowance_usd=None, default_rounds=20, system_tokens=15_000,
                            history_tokens=2_000) == 40_000)
check("U an owner's dollar figure, or the default turned off, keeps the unknown window unmetered as before",
      callable(_mb) and _mb(None, allowance_usd=1.5, default_rounds=20, system_tokens=15_000,
                            history_tokens=2_000) is None
      and _mb(None, allowance_usd=None, default_rounds=0, system_tokens=15_000, history_tokens=2_000) is None)

print("O. the owner's values")
d = asyncio.run(cb.read_turn_defaults(_Pool({"history_ceiling_tokens": 64000, "turn_allowance_usd": 2.5,
                                             "scheduled_task_allowance_usd": 0.5, "turn_rounds": 0}), 1))
check("O the owner's values override the built-ins",
      d["history_ceiling_tokens"] == 64000 and d["turn_allowance_usd"] == 2.5
      and d["scheduled_task_allowance_usd"] == 0.5 and d["turn_rounds"] == 0, d)
d = asyncio.run(cb.read_turn_defaults(_Pool({"turn_allowance_usd": "lots", "history_ceiling_tokens": -5}), 1))
check("O an unreadable or negative value falls back or floors", d["turn_allowance_usd"] is None
      and d["history_ceiling_tokens"] == 0, d)

from server import brain_service as BS  # noqa: E402
from server.routes import brain as BR  # noqa: E402


class _Req:
    # The two allowances are the person's limits (runtime/user_limits,
    # 2026-10-20): only the person's surfaces write them, so the request
    # carries the Settings app's token unless a test says otherwise.
    def __init__(self, body, app_id="settings"):
        self._b = body
        self.state = type("S", (), {"app_token": type("T", (), {"app_id": app_id})()})()

    async def json(self):
        return self._b


fake = _Pool({"unrelated": 1})
BS._pool, BS._entity_id = fake, 1
out = asyncio.run(BR.put_turn_defaults(_Req({"history_ceiling_tokens": 48000, "turn_allowance_usd": 1.25})))
check("O PUT /turn-defaults stores the owner's values beside the others",
      out.get("ok") and fake.bp == {"unrelated": 1, "history_ceiling_tokens": 48000, "turn_allowance_usd": 1.25}
      and out["in_force"]["history_ceiling_tokens"] == 48000, (out, fake.bp))
asyncio.run(BR.put_turn_defaults(_Req({"turn_allowance_usd": None})))
check("O null clears one back to the built-in", "turn_allowance_usd" not in fake.bp, fake.bp)
for bad in ({"turn_allowance_usd": 0}, {"history_ceiling_tokens": "x"}, {"turn_rounds": -1}, {"nope": 1}):
    try:
        asyncio.run(BR.put_turn_defaults(_Req(bad)))
        check(f"O refused: {bad}", False)
    except Exception as e:  # noqa: BLE001
        check(f"O refused: {bad}", getattr(e, "status_code", None) == 400, repr(e))
try:
    asyncio.run(BR.put_turn_defaults(_Req({"turn_allowance_usd": 9.0}, app_id="some-community-app")))
    check("O a community app holding write:session cannot set them", False)
except Exception as e:  # noqa: BLE001
    check("O a community app holding write:session cannot set them",
          getattr(e, "status_code", None) == 403 and "turn_allowance_usd" not in fake.bp, repr(e))
got = asyncio.run(BR.get_turn_defaults())
check("O GET /turn-defaults shows what is in force, what was set and the built-ins",
      got["set"] == {"history_ceiling_tokens": 48000} and got["built_in"]["turn_rounds"] == 20, got)

print("W. the turn reads them")
src = turn_source(ROOT)
check("W every turn reads the defaults and a scheduled run takes its own",
      "_turn_defaults = await _cb.read_turn_defaults(self.pool, sess.entity_id)" in src
      and '_scheduled_run = (app_id or "") == "scheduled_task"' in src
      and 'ceiling=(_turn_defaults or {}).get("history_ceiling_tokens")' in src
      # both spend meters, and the meter's budget (turn_allowance.meter_budget)
      # counted from a word start: the turn hands _default_rounds=_default_rounds
      # to the history step, which is not a meter
      and len(re.findall(r"(?<![\w])default_rounds=_default_rounds", src)) == 3)
check("W both spend meters take the meter's budget, which an unknown window still has",
      "_spend_ctx_budget = _ta_m.meter_budget(" in src
      and src.count("ctx_budget=_spend_ctx_budget or _ctx_budget") == 2)

print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
