"""Per-turn SPEND ALLOWANCE for the host's tool loop.

WHAT THIS REPLACES. Until 2026-09-16 the tool loop stopped at a constant:
session.py `_cap_for_model` returned 20/15/10/50 rounds by matching substrings
("opus", "sonnet", "haiku"/"flash"/"mini"/"kimi") against the model name. That
number meant nothing to the person paying for the turn — it is not time, not
quality, and not money — and the substring tiering was a guess about capability
with no registry behind it. A round of Opus on a 200k prompt and a round of a
cheap model on a 4k prompt were counted the same.

WHAT IT IS INSTEAD. The person says what one turn is worth to them, in dollars.
The loop prices every call it makes, projects what the NEXT round would cost
before starting it, and stops starting rounds it cannot afford. The number is
fed back to the model in plain words, so the model spends what is left
deliberately instead of being cut off mid-investigation.

WHY A PER-TURN BUDGET WORKS HERE AT ALL. Turn cost stays roughly flat
turn-to-turn on this platform because context does NOT accumulate across turns:
the per-round pre-send trim holds the assembled list at the dial
(context_budget.trim_with_hysteresis, session.py:3739) and the memory pre-fire
pulls only what the turn needs. On a runtime where the transcript grows without
bound, "$0.50 per turn" would mean something different on turn 40 than on turn
1, and a per-turn dollar figure would not be tunable.

IT IS A BUDGET, NOT A HARD CAP: here is what the turn has to work with; it
need not use it all but should not go over. The
honest bound is one output reserve of overspend, because a response's length
cannot be known before it is generated and a response is NEVER cut mid-stream.
Every projection therefore reserves `effective_max` output tokens at the output
rate — the loop refuses to START a round it cannot pay for, including that
round's worst-case answer.

UNPRICEABLE ROUTES. A subscription plan has no per-token rate, a local model on
the user's own hardware is free, and a model missing from the catalog has no
published rate (server/routes/inference.py:386 `price_basis` names all three).
A dollar allowance cannot be enforced against any of them, and inventing a
dollar figure would be the "zero that looks like data" that lane already
refuses. So the meter falls back to a TOKEN allowance derived from the context
budget and SAYS SO — `basis` is on the wire and the UI prints which one is in
force.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── The two researched numbers ────────────────────────────────────────────
#
# BACKSTOP_TOOL_ROUNDS — the runaway backstop that replaces the per-model
# tiers. It is NOT a budget and must never be what stops a real workflow
# (the round ceiling is a high, disable-able runaway backstop; caps protect
# the platform, never throttle the user). Sized above the whole published range of
# agent-runtime defaults rather than at their middle, because every one of
# those defaults is doing the job this allowance now does — bounding cost —
# while this constant only has to catch a genuine non-terminating loop.
#
# What production agent runtimes default to (read from each project's own
# SOURCE on 2026-09-16, normalised to tool rounds; URLs in the commit message):
#   Vercel AI SDK 5      stopWhen=stepCountIs(1)        1     (no loop by default)
#   OpenAI Agents SDK    DEFAULT_MAX_TURNS              10    (py and js)
#   LlamaIndex           DEFAULT_MAX_ITERATIONS         20
#   smolagents           MultiStepAgent.max_steps       20
#   CrewAI               BaseAgent.max_iter             25    (docs say 20)
#   Pydantic AI          UsageLimits.request_limit      50    (≈49 rounds)
#   AutoGen 0.2          MAX_CONSECUTIVE_AUTO_REPLY     100
#   LangGraph            DEFAULT_RECURSION_LIMIT     10007    (super-steps;
#                                                    ≈5000 rounds for ReAct)
#   AutoGen 0.4+ teams   max_turns                    None
#   Claude Agent SDK     max_turns                    None
#   Anthropic Tool Runner max_iterations              None
#
# Those split into two populations that mean different things, and the split
# is what picks this number. The 10–25 band is a WORKFLOW ceiling: it is
# expected to be hit and the runtime treats hitting it as a failed task
# (OpenAI raises MaxTurnsExceeded). The 50–10007 band and the three `None`
# defaults are RUNAWAY BACKSTOPS: set far above any real workflow so they
# only ever catch a pathological loop. The spend allowance above is now the
# workflow ceiling, so this constant belongs in the backstop population.
#
# 200: an order of magnitude above the workflow cluster, twice the largest
# finite non-graph backstop (AutoGen 0.2's 100), and far below LangGraph's
# effectively-unbounded 10007. 100 was rejected because it would bind on real
# measured work — real sessions run past 100 tool calls in a turn, and
# a backstop that stops those is the constant this ship exists to delete.
#
# Anthropic publishes no recommended iteration ceiling for agentic loops; the
# only bounded-loop guidance it publishes is the BUDGET form (beta task
# budgets, "a soft hint, not a hard cap", sized from measured p99 spend rather
# than a fixed constant) — which is the shape this module implements.
BACKSTOP_TOOL_ROUNDS = 200

# TOKEN_ALLOWANCE_MULTIPLIER — the token allowance on an unpriceable route,
# expressed as a multiple of the thread's context budget (the dial). One turn
# is allowed to move this many times its own context through the model.
#
# Why a multiple of the context budget and not a flat number: the context
# budget is the only per-thread size the user has already chosen, it is
# already in tokens, and it is roughly what one round costs — so the
# multiplier reads as "about this many full-context rounds", the same shape as
# the round ceilings every runtime ships.
#
# 20 is the MODE and (to within half a round) the median of the count-based
# defaults listed above — LlamaIndex 20, smolagents 20, CrewAI 25, OpenAI 10,
# median 22.5. This is the WORKFLOW-ceiling population on purpose, the
# opposite of BACKSTOP_TOOL_ROUNDS: on an unpriceable route this token figure
# IS the budget the person set, so it should sit where the industry puts a
# ceiling that is meant to be reached, not where it puts a runaway catch.
#
# THE ONE LIMIT, stated rather than smoothed over: the multiplier multiplies
# the HISTORY budget, while a real round also carries the system blocks and
# the tool schemas, which the history budget excludes by construction
# (derive_turn_budget, context_budget.py:177 subtracts measured system). So
# the effective round count is at or below 20, and further below it the harder
# the user has dialled history down. That errs toward stopping early, which is
# the safe direction for a budget, and the panel shows the resulting token
# figure so it is a number the person can see rather than infer.
#
# It is deliberately a multiplier on a user-set dial rather than a second
# magic number: someone who turns their context depth down has also turned
# their unpriced-route allowance down, with no second control to find.
TOKEN_ALLOWANCE_MULTIPLIER = 20

# THE DEFAULT, when nobody set a dollar figure (no per-thread dial, no
# Host-wide turn_allowance_usd). Until 2026-10-19 the default was none: a turn
# ran to BACKSTOP_TOOL_ROUNDS, 200 rounds of whatever the window held, with
# nothing between a looping turn and the owner's bill. The default
# is now the workflow ceiling this file already researched, in the unit every
# route can meter: DEFAULT_TURN_ROUNDS full-context rounds, counted in tokens
# (the context budget times the rounds), so a priced route, a subscription and
# a local model are all bounded alike and no dollar figure is invented.
#   DEFAULT_TURN_ROUNDS = TOKEN_ALLOWANCE_MULTIPLIER (20): the mode and median
#     of the workflow ceilings listed above (LlamaIndex 20, smolagents 20,
#     CrewAI 25, OpenAI Agents SDK 10).
#   DEFAULT_SCHEDULED_ROUNDS = 10: a scheduled run has no person watching to
#     say "go on", so it takes the low end of the same population (the OpenAI
#     Agents SDK's DEFAULT_MAX_TURNS, which raises when reached).
# With the default 32k history ceiling (context_budget.
# DEFAULT_HISTORY_CEILING_TOKENS) a turn's default is about 20 x (32k history
# + the system blocks) tokens. The owner changes both in dollars or rounds
# (context_budget.read_turn_defaults); 0 rounds turns the default off.
DEFAULT_TURN_ROUNDS = TOKEN_ALLOWANCE_MULTIPLIER
DEFAULT_SCHEDULED_ROUNDS = 10


def meter_budget(ctx_budget, *, allowance_usd, default_rounds, system_tokens,
                 history_tokens) -> int | None:
    """The context budget a turn's meter counts its rounds in (TurnSpend
    ctx_budget). The turn's own when its window is known. When it is not
    (context_budget.model_context_window: None, so the legacy history caps
    ran and no context budget exists) and the built-in default is what
    bounds the turn (no dollar figure, default_rounds on), one round is the
    system blocks plus that legacy history: without a unit the default never
    engages and the turn runs to BACKSTOP_TOOL_ROUNDS. None otherwise: an
    owner's dollar figure on such a route keeps the basis it had."""
    if ctx_budget:
        return int(ctx_budget)
    if (allowance_usd is None or allowance_usd <= 0) and default_rounds:
        unit = max(0, int(system_tokens or 0)) + max(0, int(history_tokens or 0))
        return unit or None
    return None

# A turn is "near the limit" while it can still afford the next round but not
# two of them. At that point the loop says how much room is left — feedback,
# never a cap.
NEAR_LIMIT_FACTOR = 2.0

# The earliest call the allowance may refuse. The first model call is already
# spent before any allowance can act on it, and refusing the call that feeds
# the first tool RESULTS back would throw that spend away for nothing — the
# model would answer having asked a question it never heard the answer to. So
# one complete tool round (call → tools → call) always runs, and the earliest
# refusal is the third model call of the turn.
MIN_CALLS_BEFORE_STOP = 2

_HUGE = 1_000_000  # "disabled" backstop — large enough to never bind


def backstop_rounds(behavioral_params: dict | None = None) -> int:
    """The runaway backstop for one turn, in tool rounds: the person's limit
    `turn.tool_rounds` (runtime/user_limits), stored at
    behavioral_params["max_tool_rounds"] as before, then env
    CSERIES_MAX_TOOL_ROUNDS, then BACKSTOP_TOOL_ROUNDS. 0 or less is no limit
    (a number no turn can reach): with it off, the spend allowance, the
    dead-tool ledger and the all-fail break are the only things that end a
    tool loop. Anything unparseable falls back to the default rather than
    raising — a bad dial must never fail a turn.
    """
    from mastercode.runtime import user_limits
    return user_limits.int_value("turn.tool_rounds", behavioral_params, off=_HUGE)


def _usd(n: float) -> str:
    """A dollar figure a person reads without decoding. Sub-cent amounts keep
    enough digits to be a number rather than '$0.00'."""
    n = float(n)
    if n >= 0.01 or n == 0:
        return f"${n:,.2f}"
    return f"${n:.4f}"


def _tok(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M tokens"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k tokens"
    return f"{n} tokens"


def stop_line(*, written: bool, card: bool) -> str:
    """The plain line that ends a turn's row after an allowance stop: it says the turn reached its budget — before a reply was
    written, when nothing visible was — and points at the continue card when
    one was raised."""
    line = ("This turn reached its budget." if written
            else "This turn reached its budget before a reply was written.")
    if card:
        line += " The choice to go on is in the question box above."
    return line


def continue_card_line(card_where: str | None = None) -> str:
    """What the answer written after an allowance stop is told about going
    on (review r12). With the stop's continue card up (`card_where`), the card
    asks it; without one (no question box on this lane) the answer still only
    reports — a "want me to continue?" there is on a call with no tools, where
    no repair could run."""
    if card_where:
        return (f"A card {card_where} already asks the person whether to spend more "
                "and carry on; their answer starts that follow-up. So do not ask it: "
                "answer the person now with what you already have — what is done, "
                "and which parts are left.")
    return ("Answer the person now with what you already have: say plainly what is "
            "done and which parts are left, as a statement. Do not ask whether to "
            "go on.")


class TurnSpend:
    """The per-turn meter. One instance per chat turn, fed every model call.

    State is deliberately per-TURN and not per-thread: the allowance is what
    this turn may spend, and a user nudge mid-loop resets the ROUND counter
    (session.py's `_nudge_resets`) but never the money — those dollars are
    already gone, and re-granting them on a nudge would make the budget
    unbounded for anyone who types twice.
    """

    def __init__(self, *, model: str | None, provider: str | None = None,
                 allowance_usd: float | None = None,
                 ctx_budget: int | None = None,
                 price=None, priced: bool | None = None,
                 default_rounds: int | None = None):
        self.model = model
        self.provider = provider
        self.allowance_usd = (float(allowance_usd)
                              if allowance_usd is not None else None)
        self.ctx_budget = int(ctx_budget) if ctx_budget else None
        # The built-in default (DEFAULT_TURN_ROUNDS) when no dollar figure is
        # set: that many full-context rounds, in tokens. None or 0: no default.
        self.default_rounds = int(default_rounds) if default_rounds else None
        self.default_in_force = False
        self.price = price
        self.calls = 0
        self.spent_usd = 0.0
        self.spent_tokens = 0
        # Set when a call could not be priced although the basis is "usd" —
        # a mid-turn failover onto a model with no published rate. The turn
        # keeps metering what it CAN price (under-counting overspends, it
        # never cuts a turn short on a wrong number) and the flag rides the
        # ledger so the miss is visible rather than silently absorbed.
        self.unpriced_calls = 0
        # Last call's usage, the base of every projection.
        self._last: dict = {}
        # Whole-turn token totals BY KIND. The per-call list on the turn row is
        # truncated to 24 entries, so anything derived by re-summing it
        # under-reports a long turn; these are the authoritative totals and
        # they ride the ledger in `spend`.
        self.tokens = {"uncached": 0, "cache_read": 0, "cache_write": 0,
                       "output": 0}
        # The last steering line actually handed to the model, and the round
        # count the last near-limit line quoted — the pair that keeps the loop
        # from narrating the budget every round (see check()).
        self._last_message: str | None = None
        self._last_near_rounds: int | None = None
        # Set when a mid-turn failover moved the basis (e.g. a priced model
        # failing over to a subscription one). Reported so the panel can say
        # the unit changed rather than quietly showing dollars for a turn that
        # stopped being denominated in them.
        self.basis_changed_from: str | None = None
        # The last id the wire reported. Kept apart from self.model, which is
        # the id the turn PRICES at: on the Anthropic lane those differ on
        # every call (see _adopt_served) and conflating them is what broke
        # metering on that route.
        self.served_model: str | None = None
        if priced is None:
            priced = self._route_is_priced(price)
        self.basis = self._resolve_basis(bool(priced))

    # ── construction ─────────────────────────────────────────────────────
    @staticmethod
    def _route_is_priced(price) -> bool:
        """A route is priceable only when a published rate is actually
        non-zero. cached_model_prices() admits a row when EITHER rate is
        present (inference.py:245-251), so a row of two zeros is possible —
        and pricing a turn against it would report every turn as free."""
        if price is None:
            return False
        try:
            return (float(price.input_per_token) > 0
                    or float(price.output_per_token) > 0)
        except (TypeError, ValueError, AttributeError):
            return False

    def _resolve_basis(self, priced: bool) -> str:
        """"usd" | "tokens" | "none" — which allowance is actually in force.

        "usd"    the route publishes per-token rates and the user set dollars.
        "tokens" the user set an allowance the route cannot denominate in
                 dollars (subscription / local / unpriced), so it is metered
                 as a multiple of the context budget instead.
        "none"   no allowance is in force — either the user set none, or the
                 route is unpriceable AND no context budget exists to derive
                 a token allowance from. The loop runs to the backstop.
        """
        if self.allowance_usd is None or self.allowance_usd <= 0:
            if self.default_rounds and self.ctx_budget:
                self.default_in_force = True
                return "tokens"
            return "none"
        if priced:
            return "usd"
        if self.ctx_budget:
            return "tokens"
        return "none"

    @staticmethod
    def _lookup_price(model: str | None):
        """The catalog's ModelPrice for an id, or None. Never raises: a cold
        catalog or a missing pricing module reads as unpriceable, the same
        path a subscription model takes. cached_model_prices() is an in-memory
        read of an already-warmed cache (inference.py:204 — it never fetches),
        so calling this per round is free."""
        try:
            from server.routes.inference import _price_for
            return _price_for(str(model or ""))
        except Exception as e:  # noqa: BLE001 - pricing must never fail a turn
            log.debug("turn allowance: price lookup unavailable (%s)", e)
            return None

    @classmethod
    def for_route(cls, model: str | None, provider: str | None, *,
                  allowance_usd: float | None, ctx_budget: int | None,
                  default_rounds: int | None = None):
        """Build against the live catalog for the ROUTED model. The served
        model can differ from round to round (failover), so this is only the
        opening position — note_call re-resolves per call."""
        return cls(model=model, provider=provider,
                   allowance_usd=allowance_usd, ctx_budget=ctx_budget,
                   price=cls._lookup_price(model), default_rounds=default_rounds)

    def _adopt_served(self, model: str | None, failed_over=None) -> None:
        """Re-point the meter at the model that ACTUALLY answered this round.

        A DIFFERENT MODEL STRING IS NOT A FAILOVER, and treating it as one
        broke the feature on the primary lane. The direct Anthropic wire
        rewrites the route id before every call — anthropic_api_model_id
        (inference_config.py:129) maps `anthropic/claude-haiku-4.5` to
        `claude-haiku-4-5-20251001` at provider_gateway.py:1283, and :1381
        returns that rewritten id back as `model`. So on Anthropic the served
        string differs from the routed one on EVERY call, by design. And only
        the OpenRouter-keyed row prices — Anthropic's own /v1/models publishes
        no rates — so the dated id does not resolve a price. An earlier
        revision read that as "failover to an unpriced model" on call 1 of
        every Anthropic turn: basis flipped usd -> tokens, spent_usd stayed
        None forever, and a $1.00 budget silently became an 800k-token one.
        Measured: routed id prices True, served id prices False.

        THE AUTHORITATIVE SIGNAL IS `failed_over`, not a string comparison.
        provider_gateway annotates a genuine failover (:2103, :2120) and
        session.py already reads it for provenance (:3917). So:

          - a served id that PRICES is adopted — it is a better handle on what
            answered, whatever the reason it differs;
          - a served id that does NOT price leaves price and model alone. On
            the Anthropic lane that is the normal case and the routed id is
            the one that prices, which is exactly what should be billed;
          - the BASIS moves only on a real failover to a model with no rate.

        Cheap enough to do every round: _lookup_price reads the warmed
        in-memory catalog and never fetches (inference.py:204).

        The basis can only move in ways that stay honest:
          usd -> tokens   a genuine failover landed on a model with no
                          per-token rate. Dollars spent so far are real and
                          stay on the record, but they cannot be added to, so
                          metering continues in the unit that still works and
                          basis_changed_from says so rather than leaving a
                          dollar total that stopped growing for no visible
                          reason.
          tokens -> usd   is NOT taken. A turn already metering tokens has no
                          dollar figure for what it spent, so switching would
                          compare a partial spend against a full allowance.
        """
        if model:
            self.served_model = model
        price = self._lookup_price(model) if model else None
        if self._route_is_priced(price):
            # Whatever it is called, it prices — bill at it from here.
            self.model = model
            self.price = price
            return
        # It does not price. That is only a problem if something genuinely
        # failed over TO it; otherwise it is an id form the catalog does not
        # key on (the Anthropic lane's dated id), and the routed model's price
        # remains the right one.
        if not isinstance(failed_over, dict):
            return
        to_model = failed_over.get("to_model") or model
        if self._route_is_priced(self._lookup_price(to_model)):
            return
        if self.basis == "usd":
            self.basis_changed_from = "usd"
            self.basis = "tokens" if self.ctx_budget else "none"
            log.warning(
                "turn allowance: failover %s -> %s, which has no published "
                "per-token rate — the turn's basis moves usd -> %s "
                "(spent $%.6f so far, which stands)",
                failed_over.get("from_model"), to_model, self.basis,
                self.spent_usd)

    # ── metering ─────────────────────────────────────────────────────────
    @property
    def allowance_tokens(self) -> int | None:
        if self.basis != "tokens" or not self.ctx_budget:
            return None
        rounds = self.default_rounds if self.default_in_force else TOKEN_ALLOWANCE_MULTIPLIER
        return int(self.ctx_budget) * int(rounds or TOKEN_ALLOWANCE_MULTIPLIER)

    def note_call(self, res: dict, model: str | None = None) -> dict:
        """Price one completed model call and fold it into the turn.

        `model` is the SERVED model for that round, not the routed one: a
        failover moves the model mid-turn (session.py `_served`) and the
        call must be priced at the rate of whoever actually answered it.

        Returns the per-call ledger entry — {in, cached, cache_write, out,
        usd} — which the caller appends to context_ledger.per_call.
        """
        if not isinstance(res, dict):
            return {}
        if res.get("usage_reported") is False:
            # The provider reported no usage (provider_gateway._call_remote):
            # the entry says unknown, nothing is priced or counted, and the
            # projection keeps the last call it could measure.
            self._adopt_served(model, res.get("failed_over"))
            self.calls += 1
            entry = {"in": None, "cached": None, "cache_write": None, "out": None,
                     "usage": "not_reported"}
            if res.get("credential") in ("signin", "api_key"):
                entry["credential"] = res["credential"]
            return entry
        try:
            t_in = int(res.get("input_total_tokens")
                       or res.get("prompt_tokens") or 0)
            cached = int(res.get("cache_read_tokens") or 0)
            c_write = int(res.get("cache_write_tokens") or 0)
            t_out = int(res.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            return {}
        # Whoever actually answered sets the rates for this call AND for
        # every projection after it — judged on whether the served id prices,
        # with `failed_over` (not a string diff) as the failover signal.
        self._adopt_served(model, res.get("failed_over"))
        entry = {"in": t_in, "cached": cached, "cache_write": c_write,
                 "out": t_out}
        # Which credential served the call, where the provider has two (xai:
        # the user's sign-in, or the per-token API key — provider_gateway
        # stamps it on the result). It rides the per-call entry so the turn's
        # printed price can follow it (inference.estimate_turn_cost per_call).
        cred = res.get("credential")
        if cred in ("signin", "api_key"):
            entry["credential"] = cred
        self.calls += 1
        self.spent_tokens += t_in + t_out
        # Clamped for the same reason estimate_call_cost_detail clamps it
        # (inference.py:339): a lane that reports cache counts excluding them
        # from the input total would otherwise drive the uncached share
        # negative and credit the turn.
        self.tokens["uncached"] += max(0, t_in - cached - c_write)
        self.tokens["cache_read"] += cached
        self.tokens["cache_write"] += c_write
        self.tokens["output"] += t_out
        usd = None
        if cred == "signin":
            # The call spent the user's PLAN, not a per-token balance, so it
            # adds no dollars — even where the route prices (OpenRouter lists
            # the same model per token). The turn is metered the way any
            # flat-plan route is: in tokens, or not at all. Dollars a key call
            # already spent this turn stand, and basis_changed_from says the
            # unit moved; with none spent the turn simply IS a plan turn.
            if self.basis == "usd":
                if self.spent_usd > 0:
                    self.basis_changed_from = "usd"
                self.basis = "tokens" if self.ctx_budget else "none"
        elif self.basis == "usd":
            usd = self._price_call(self.model, t_in, cached, c_write, t_out,
                                   credential=cred)
            if usd is None:
                # Near-unreachable now that _adopt_served moves the basis off
                # "usd" when the served model has no rate — kept as the net
                # under it, so a pricing miss is still counted rather than
                # quietly adding $0 to the turn.
                self.unpriced_calls += 1
            else:
                self.spent_usd += usd
        if usd is not None:
            entry["usd"] = round(usd, 8)
        # Snapshotted AFTER pricing so the stored last_call is the same dict
        # the per-call list holds — a reader comparing them must not find the
        # ledger's copy carrying a `usd` its authoritative twin lacks.
        self._last = dict(entry)
        return entry

    def _price_call(self, model: str | None, t_in: int, cached: int,
                    c_write: int, t_out: int,
                    credential: str | None = None) -> float | None:
        """One call's list-price estimate, through the SAME estimator the
        cost log writes with (provider_gateway.py:264) so the turn's running
        total and the row that lands in inference_cost_log agree — including
        the credential that served it."""
        try:
            from server.routes.inference import estimate_call_cost_detail
            v, _ = estimate_call_cost_detail(
                str(model or ""), t_in, t_out,
                cache_read_tokens=cached, cache_write_tokens=c_write,
                provider=self.provider, credential=credential)
            return None if v is None else float(v)
        except Exception as e:  # noqa: BLE001
            log.debug("turn allowance: call pricing failed (%s)", e)
            return None

    # ── projection ───────────────────────────────────────────────────────
    def project(self, new_tokens: int, output_reserve: int) -> float | None:
        """What the NEXT round would cost, in the units of `basis`.

        The shape of a tool-loop round is known, so this is arithmetic and
        not a guess: the next call re-sends everything the last call sent,
        plus what the loop appended since (the assistant's tool_calls message
        and every tool result — that is `new_tokens`), and then generates an
        answer that may run to the full output reserve.

        THE CACHE SPLIT. Whatever the last round reported as cache_read OR
        cache_write is the prefix that the next round will cache-READ: a
        written block is readable on the very next call, which is what the
        cache is for. So the two are added together and priced at the
        cache-read rate — NOT re-charged at the write rate, which would treat
        round 1's full-prefix write as recurring and overstate every later
        round several-fold. The remainder of last round's input, plus the new
        tokens, prices at the full input rate. When the provider publishes no
        cache-read rate, the input rate stands in — the same non-invented
        fallback estimate_call_cost_detail uses (inference.py:350).

        A route with prompt-length tiers is priced at the tier the NEXT
        prompt reaches (last input + new tokens), every part of it, the way
        the vendor bills it (_rates_for).

        Returns None when no allowance is in force.
        """
        if self.basis == "none":
            return None
        new_tokens = max(0, int(new_tokens or 0))
        reserve = max(0, int(output_reserve or 0))
        last_in = int(self._last.get("in") or 0)
        prefix = min(int(self._last.get("cached") or 0)
                     + int(self._last.get("cache_write") or 0), last_in)
        carried = max(0, last_in - prefix)
        if self.basis == "tokens":
            # A token allowance meters raw throughput — there is no cheaper
            # rate to give the cached share credit for.
            return float(last_in + new_tokens + reserve)
        p = self._rates_for(last_in + new_tokens)
        if p is None:
            return None
        try:
            in_rate = float(p.input_per_token)
            out_rate = float(p.output_per_token)
            read_rate = (float(p.cache_read) if p.cache_read is not None
                         else in_rate)
        except (TypeError, ValueError, AttributeError):
            return None
        return ((carried + new_tokens) * in_rate
                + prefix * read_rate
                + reserve * out_rate)

    def affordable_output(self, new_tokens: int) -> int | None:
        """The most output tokens the next call can write and stay inside the
        allowance: what is left after the next call's INPUT (project with no
        output), divided by the output rate. None when no allowance is in
        force or the route has no output price — then nothing is clamped.
        The caller holds each request's max_tokens to max(this, one realistic reply), so the worst overspend
        is one realistic reply, as check()'s projection assumes."""
        allowance = self.allowance
        if allowance is None:
            return None
        input_cost = self.project(new_tokens, 0)
        if input_cost is None:
            return None
        left = allowance - self.spent - input_cost
        if self.basis == "tokens":
            return max(0, int(left))
        try:
            nxt = int(self._last.get("in") or 0) + max(0, int(new_tokens or 0))
            out_rate = float(self._rates_for(nxt).output_per_token)
        except (TypeError, ValueError, AttributeError):
            return None
        if out_rate <= 0:
            return None
        return max(0, int(left / out_rate))

    def _rates_for(self, prompt_tokens: int):
        """The route's rates for a prompt of this length: a prompt-length
        tier's when the next prompt reaches one (inference.ModelPrice.tier_for,
        Claude Haiku 5.5's "over 100,000 tokens" set), else the base price. A
        price object without tiers is used as it stands."""
        p = self.price
        tier_for = getattr(p, "tier_for", None)
        if callable(tier_for):
            return tier_for(prompt_tokens)[1]
        return p

    @property
    def spent(self) -> float:
        return self.spent_usd if self.basis == "usd" else float(self.spent_tokens)

    @property
    def allowance(self) -> float | None:
        if self.basis == "usd":
            return self.allowance_usd
        if self.basis == "tokens":
            a = self.allowance_tokens
            return float(a) if a else None
        return None

    def remaining(self) -> float | None:
        a = self.allowance
        return None if a is None else max(0.0, a - self.spent)

    # ── the decision ─────────────────────────────────────────────────────
    def check(self, new_tokens: int, output_reserve: int) -> tuple[str, str | None]:
        """("ok" | "near" | "stop", steering message or None) for the round
        about to start.

        "stop" is structural, not advisory: the caller makes the call with
        tools=None, exactly like the exhaustion path, so the model CANNOT
        spend another round however it reads the message. The message exists
        so the answer acknowledges what was left undone instead of pretending
        the investigation finished.

        NO ALLOWANCE MESSAGE APPEARS BEFORE THE FIRST FULL TOOL ROUND HAS RUN.
        MIN_CALLS_BEFORE_STOP gates "near" as well as "stop": until the model
        has seen one set of tool results it has no basis for deciding what to
        spend the rest on, and a budget line in front of that just biases the
        very first decision of the turn.

        THE NEAR LINE IS NEVER REPEATED VERBATIM. Because the projection
        always reserves a full output allowance (16384 tokens on any window
        of 32k+, which is ~$0.25 on a $15/M-output model), "near" is the
        steady state on any modest budget, not a rare event — it can hold for
        every remaining round of the turn. Re-appending the same ~350-character
        sentence each round would spend the person's budget telling them about
        their budget, and it moves the cached prefix every round as well. So a
        line is emitted only when the ROUND COUNT it quotes has dropped since
        the last one — "you have about 3 rounds left" is worth saying once,
        and again at 2, but not four times while the cents tick down. Text
        equality alone would not have been enough: the remaining figure moves
        every round, so every line would have differed and every line would
        have been appended.
        """
        allowance = self.allowance
        if allowance is None:
            return "ok", None
        projected = self.project(new_tokens, output_reserve)
        if projected is None:
            return "ok", None
        if self.calls < MIN_CALLS_BEFORE_STOP:
            # One complete tool round always runs, and runs unprompted.
            return "ok", None
        left = max(0.0, allowance - self.spent)
        if self.spent + projected > allowance:
            # Stop is terminal and fires once — never deduped away. It is the
            # message that explains an answer the person did not ask for yet.
            msg = self._stop_message(left)
            self._last_message = msg
            return "stop", msg
        if left < NEAR_LIMIT_FACTOR * projected:
            rounds = int(left // projected) if projected > 0 else 0
            if self._last_near_rounds is not None and rounds >= self._last_near_rounds:
                # Same room as last time we said so — stay quiet.
                return "ok", None
            msg = self._near_message(left, rounds)
            if msg == self._last_message:
                return "ok", None
            self._last_near_rounds = rounds
            self._last_message = msg
            return "near", msg
        return "ok", None

    def would_stop(self, new_tokens: int, output_reserve: int) -> bool:
        """Would check() answer "stop" for this round? The same test with no
        side effects: nothing is recorded, so a near line check() would give
        the next ordinary round is not spent here."""
        allowance = self.allowance
        if allowance is None or self.calls < MIN_CALLS_BEFORE_STOP:
            return False
        projected = self.project(new_tokens, output_reserve)
        return projected is not None and self.spent + projected > allowance

    def _amounts(self, left: float) -> tuple[str, str, str]:
        """(left, spent, budget) as a person reads them, in the basis's units."""
        if self.basis == "usd":
            return (_usd(left), _usd(self.spent_usd), _usd(self.allowance_usd or 0.0))
        return (_tok(int(left)), _tok(self.spent_tokens), _tok(self.allowance_tokens or 0))

    def _stop_message(self, left: float, card_where: str | None = None) -> str:
        have, spent, budget = self._amounts(left)
        # Whether to spend more is the person's call, and it is asked in the
        # question box — the continue card the stop raises (session.py,
        # continue_card below) — never in the answer written with no tools,
        # where nothing could repair a "want me to continue?" (review r12).
        return (
            f"BUDGET REACHED. This turn had a budget of {budget}; you have "
            f"spent {spent} and only {have} is left, which is not enough for "
            "another round of tool calls. Do NOT call any more tools. "
            + continue_card_line(card_where))

    def stop_message(self, card_where: str | None = None) -> str:
        """The stop's steering text, once the stop has raised its continue
        card (`card_where`: where it waits — go_ahead_guard.card_from_tool)."""
        return self._stop_message(max(0.0, (self.allowance or 0.0) - self.spent), card_where)

    def would_overrun(self, new_tokens: int, output_reserve: int) -> bool:
        """Would the next call, at `output_reserve`, take the turn past its
        allowance? check()'s test WITHOUT the MIN_CALLS_BEFORE_STOP gate (that
        gate keeps one complete tool round; a continuation of a cut reply is
        not a tool round). Used for the continuation."""
        allowance = self.allowance
        if allowance is None:
            return False
        projected = self.project(new_tokens, output_reserve)
        return projected is not None and self.spent + projected > allowance

    def continue_card(self, cut_reply: bool | str = False) -> dict:
        """The escalate arguments of the continue card an allowance stop
        raises in the question box: the person decides whether the work goes
        on with more spent. Their answer — either one — starts a turn of its
        own, with a budget of its own (tickets.decide_ticket_service ->
        session.answer_after_turn), so "Stop here" too is read by the Host;
        what it says is the Host is told to leave the rest (review r13)."""
        _have, spent, budget = self._amounts(0.0)
        if cut_reply == "empty":
            # The cut completion wrote no visible text (its whole output went
            # on reasoning) and the budget had no room to continue it (review
            # R7-2): there is no cut reply above — say the reply was never
            # written.
            sitrep = (f"This turn reached its budget ({budget}; {spent} spent) before its "
                      "reply was written: the model's output ran out while it was still "
                      "working the answer out, so this turn has no reply to it.")
        elif cut_reply:
            # The reply reached the model's output limit and the budget had no
            # room to continue it: it ends where it was cut,
            # and the model was never asked to sum up — say exactly that.
            sitrep = (f"This turn reached its budget ({budget}; {spent} spent) while the "
                      "reply was being written: the reply above stops where it was cut "
                      "off, mid-way, and was not finished or summed up.")
        else:
            # The card is raised BEFORE the tools-off answer is written, so it
            # claims nothing about that answer: the reply below
            # it is either the answer the model wrote, or a line saying no
            # reply was written — the card is true either way.
            sitrep = (f"This turn reached its budget ({budget}; {spent} spent) before the "
                      "work was finished.")
        return {
            "source": "turn_allowance",
            "sitrep": sitrep,
            "options": [
                {"id": "continue", "label": "Continue",
                 "consequence": "A new turn picks up what is left, with a budget of its own."},
                {"id": "stop", "label": "Stop here",
                 "consequence": "The Host is told to leave the rest; what is done stays."},
            ],
        }

    def _near_message(self, left: float, rounds: int) -> str:
        if self.basis == "usd":
            have = _usd(left)
            budget = _usd(self.allowance_usd or 0.0)
        else:
            have = _tok(int(left))
            budget = _tok(self.allowance_tokens or 0)
        room = ("no room for another full round after this one" if rounds <= 0
                else f"about {rounds} more round{'s' if rounds != 1 else ''} "
                     "of tool calls")
        return (
            f"Budget check: {have} of this turn's {budget} budget is left — "
            f"{room}. This is information, not an instruction to stop: spend "
            "what is left on whatever is most worth knowing, and start "
            "gathering what you need for the answer."
        )

    # ── reporting ────────────────────────────────────────────────────────
    def summary(self) -> dict:
        """What rides the turn row and the context frame.

        THIS IS THE AUTHORITATIVE RECORD OF THE TURN, and it exists because
        the per-call list beside it is not. session.py stores
        context_ledger.per_call truncated to 24 entries, so anything a reader
        re-derives by summing that list under-reports every turn longer than
        24 calls — a 40-call turn read back as "$0.55" when it cost $0.90,
        with the token lines wrong by the same 16 calls. Everything a reader
        needs is therefore totalled HERE, over every call, before truncation:
        spent_usd, spent_tokens, tokens by kind, and calls.

        `last_call` is here for the same reason from the other end: the
        projection needs the turn's FINAL call, and per_call[-1] after
        truncation is call #24, not call #40. Carrying the split explicitly
        makes the projection independent of how the list was cut.
        """
        out: dict = {
            "allowance_basis": self.basis,
            "calls": self.calls,
            "spent_usd": round(self.spent_usd, 8) if self.basis == "usd" else None,
            "spent_tokens": self.spent_tokens,
            "tokens": dict(self.tokens),
            "last_call": dict(self._last) if self._last else None,
        }
        if self.allowance_usd is not None:
            out["turn_allowance_usd"] = self.allowance_usd
        if self.basis == "tokens":
            out["allowance_tokens"] = self.allowance_tokens
        if self.default_in_force:
            # No figure was set: the built-in default bounded this turn.
            out["allowance_default_rounds"] = self.default_rounds
        if self.unpriced_calls:
            out["unpriced_calls"] = self.unpriced_calls
        if self.basis_changed_from:
            out["basis_changed_from"] = self.basis_changed_from
        return out


def new_input_tokens(messages: list, mark: int) -> int:
    """The tokens appended to the loop's message list since the last model
    call — the assistant's tool_calls message and every tool result — which
    the next round pays full input rate for.

    THE LOOP AND THE GATE MUST RUN THE SAME ARITHMETIC. This lived inline in
    session.py's tool loop, which meant the gate could only re-implement it
    and then prove its own copy right. It is one expression; having two of
    them is how a projection silently stops matching the thing it projects.

    `mark` is len(messages) as of the last call, recorded AFTER the overflow
    wrapper may have trimmed the list in place, so it indexes what the
    provider actually saw. A mark past the end (the list was trimmed shorter
    since) yields 0 rather than raising — erring low there is right, because
    a trim means those tokens are not being sent again.
    """
    try:
        from mastercode import context_budget as _cb
        tail = messages[max(0, int(mark)):] if messages else []
        return sum(_cb._msg_chars(m) for m in tail) // _cb.CHARS_PER_TOKEN
    except Exception as e:  # noqa: BLE001 - never fail a turn on an estimate
        log.debug("new_input_tokens estimate failed (treated as 0): %s", e)
        return 0
