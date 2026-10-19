#!/usr/bin/env python3
"""Background-activity telemetry — a FAILED model call has to leave a row.

The defect this covers, measured 2026-08-28:

  _log_inference is called from the three SUCCESS paths only
  (_call_anthropic, _call_remote, _call_local). Every failure path beside them
  recorded through `_prompt_console.log_response(..., error=...)` — and
  set_prompt_console (provider_gateway.py, runtime/triage.py) has NO CALLER
  anywhere in the repo, so _prompt_console is None in the pod and all of those
  hooks are no-ops. Net: a failed model call wrote nothing anywhere. A
  background role that stopped answering left a pod log line and no queryable
  record — the same silent-lane shape as the 2026-08-04 consolidation outage
  that ran 16 days unseen.

  _log_call_failure closes it, writing to the SAME ledger under
  purpose='inference_error' so cost readers that filter purpose='inference'
  keep their exact COUNT(*)s.

Pure: no network, no DB (the pool is a recorder double), same shape as
scripts/test_inference_usage_telemetry.py.
"""
import asyncio
import json
import os
import sys

FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    suffix = f"  [{detail}]" if detail and not cond else ""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{suffix}")
    if not cond:
        FAILED += 1


class FakePool:
    """Records every execute() the way asyncpg's Pool would accept it."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"


def _row(pool):
    """The argument tuple of the last INSERT."""
    return pool.calls[-1][1] if pool.calls else ()


def main() -> int:
    root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, root)
    sys.path.insert(0, os.path.join(root, "brain"))
    from mastercode import provider_gateway as gw

    # Positional order of _log_inference's INSERT (verified against the SQL):
    #   0 entity_id, 1 source_module, 2 model_id, 3 provider, 4 purpose,
    #   5 tokens_in, 6 tokens_out, 7 est_cost_usd, 8 cost_method,
    #   9 latency_ms, 10 metadata
    I_SRC, I_PURPOSE, I_TOK_IN, I_TOK_OUT = 1, 4, 5, 6
    I_COST, I_METHOD, I_LATENCY, I_META = 7, 8, 9, 10

    print("_error_class — the failure's KIND, most specific first")
    check("killed wins over everything",
          gw._error_class({"killed": True, "unconfigured": True,
                           "lane_unavailable": True}) == "killed")
    check("unconfigured is not reported as an outage",
          gw._error_class({"unconfigured": True,
                           "lane_unavailable": True}) == "unconfigured")
    check("lane_unavailable when only that is set",
          gw._error_class({"lane_unavailable": True}) == "lane_unavailable")
    check("plain error is the floor", gw._error_class({}) == "error")

    print("\n_log_inference — the SUCCESS lane is unchanged (regression)")
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_inference(
        source="entity_runtime", model="m", provider="anthropic",
        total_tokens=10, prompt_tokens=7, completion_tokens=3, entity_id=9003))
    check("one INSERT issued", len(pool.calls) == 1)
    row = _row(pool)
    check("purpose defaults to 'inference' — cost readers filter on it",
          row[I_PURPOSE] == "inference", str(row[I_PURPOSE]))
    check("purpose is a BOUND PARAM, never a SQL literal",
          "'inference'" not in pool.calls[0][0])

    print("\n_log_call_failure — one row, marked as an error, costing nothing")
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_call_failure(
        {"error": "anthropic 529: overloaded", "content": "",
         "lane_unavailable": True},
        source="thread_consolidator", model="claude-sonnet-4-6",
        provider="anthropic", entity_id=9003, call_start=gw._time.time() - 1.5))
    check("exactly one INSERT issued", len(pool.calls) == 1)
    row = _row(pool)
    check("lands in inference_cost_log",
          "INSERT INTO inference_cost_log" in pool.calls[0][0])
    check("purpose = 'inference_error'", row[I_PURPOSE] == "inference_error",
          str(row[I_PURPOSE]))
    check("zero tokens in", row[I_TOK_IN] == 0, str(row[I_TOK_IN]))
    check("zero tokens out", row[I_TOK_OUT] == 0, str(row[I_TOK_OUT]))
    check("zero dollars — a call that never landed bought nothing",
          row[I_COST] == 0.0, str(row[I_COST]))
    check("cost_method stays inside v113's four-value vocabulary",
          row[I_METHOD] in ("list", "subscription", "free", "unpriced"),
          str(row[I_METHOD]))
    check("...'unpriced' specifically (not 'free', which reads as $0 spent)",
          row[I_METHOD] == "unpriced", str(row[I_METHOD]))
    meta = json.loads(row[I_META])
    check("the provider's own error text is preserved verbatim",
          meta.get("error") == "anthropic 529: overloaded",
          str(meta.get("error")))
    check("error_class is classified for the UI",
          meta.get("error_class") == "lane_unavailable",
          str(meta.get("error_class")))
    check("latency is recorded (the call took real time before failing)",
          row[I_LATENCY] >= 1000, str(row[I_LATENCY]))

    print("\ncall_model — a REAL failover stamps the row that fell over")
    # Through call_model, not a hand-built dict: the annotation is set on
    # `retry` (what the caller sees), never on `result`, so the primary's row
    # only carries it because call_model threads it in. An isolated
    # _log_call_failure test cannot catch that wiring — it passed while the
    # primary row was in fact unstamped and the fallback row pointed at itself.
    pool = FakePool()
    gw.set_inference_pool(pool)
    _orig_remote = gw._call_remote

    async def _dead_primary(**kw):
        # Fails for the primary model, succeeds on the fallback model.
        if kw.get("model") == "primary-model":
            return {"error": "openrouter 503: upstream down", "content": "",
                    "lane_unavailable": True}
        return {"content": "answered", "tokens_used": 5, "model": kw.get("model")}

    gw._call_remote = _dead_primary
    try:
        res = asyncio.run(gw.call_model(
            [{"role": "user", "content": "hi"}],
            provider="openrouter", provider_model="primary-model",
            fallback_provider="openrouter", fallback_model="backup-model",
            _source="thread_consolidator", _entity_id=9003))
    finally:
        gw._call_remote = _orig_remote

    check("the caller still gets the fallback's answer",
          res.get("content") == "answered", str(res.get("content")))
    check("exactly ONE failure row — the successful retry is not an error",
          len(pool.calls) == 1, str(len(pool.calls)))
    if len(pool.calls) == 1:
        row = _row(pool)
        meta = json.loads(row[I_META])
        check("the row is the PRIMARY's, not the fallback's",
              row[2] == "primary-model", str(row[2]))
        check("and it names who answered instead",
              meta.get("failed_over_to") == "openrouter/backup-model",
              str(meta.get("failed_over_to")))

    print("\ncall_model — both ends dead: two rows, neither pointing at itself")
    pool = FakePool()
    gw.set_inference_pool(pool)

    async def _all_dead(**kw):
        return {"error": f"openrouter 503: {kw.get('model')} down",
                "content": "", "lane_unavailable": True}

    gw._call_remote = _all_dead
    try:
        asyncio.run(gw.call_model(
            [{"role": "user", "content": "hi"}],
            provider="openrouter", provider_model="primary-model",
            fallback_provider="openrouter", fallback_model="backup-model",
            _source="thread_consolidator", _entity_id=9003))
    finally:
        gw._call_remote = _orig_remote

    check("one row per dial", len(pool.calls) == 2, str(len(pool.calls)))
    by_model = {c[1][2]: json.loads(c[1][I_META]) for c in pool.calls}
    check("the fallback's row does NOT stamp failed_over_to at itself",
          by_model.get("backup-model", {}).get("failed_over_to") is None,
          str(by_model.get("backup-model", {}).get("failed_over_to")))
    check("the primary's row still names where it fell over to",
          by_model.get("primary-model", {}).get("failed_over_to")
          == "openrouter/backup-model",
          str(by_model.get("primary-model", {}).get("failed_over_to")))

    print("\n_log_call_failure — telemetry can never break a turn")

    class Exploding:
        async def execute(self, *_a):
            raise RuntimeError("simulated write failure")

    gw.set_inference_pool(Exploding())
    try:
        asyncio.run(gw._log_call_failure(
            {"error": "x"}, source="s", model="m", provider="p",
            entity_id=1, call_start=gw._time.time()))
        check("a failing ledger write is swallowed, not raised", True)
    except Exception as e:  # noqa: BLE001
        check("a failing ledger write is swallowed, not raised", False, str(e))

    print("\ncall_model — an UNROUTED role writes the row that was missing")
    # This is the 2026-08-04 case: the bare call returned
    # {"unconfigured": True, "content": ""}, the caller read the empty content
    # as "nothing to extract", and nothing recorded that no model was reached.
    pool = FakePool()
    gw.set_inference_pool(pool)
    res = asyncio.run(gw.call_model(
        [{"role": "user", "content": "hi"}],
        provider="", provider_model="", _source="thread_consolidator",
        _entity_id=9003))
    check("still returns the unconfigured contract callers depend on",
          res.get("unconfigured") is True and res.get("content") == "",
          str({k: res.get(k) for k in ("unconfigured", "content")}))
    check("and NOW it leaves a row", len(pool.calls) == 1, str(len(pool.calls)))
    if pool.calls:
        row = _row(pool)
        meta = json.loads(row[I_META])
        check("attributed to the caller that went dark",
              row[I_SRC] == "thread_consolidator", str(row[I_SRC]))
        check("classified 'unconfigured' — go pick a model, not wait it out",
              meta.get("error_class") == "unconfigured",
              str(meta.get("error_class")))

    print("\ncost readers — exclude the error rows, never whitelist 'inference'")
    # The ledger is MULTI-PURPOSE: synaptive_handler.py:220-221 writes
    # purpose='shape' rows get_inference_budget is documented to include, and
    # retired pipelines left other values behind. `purpose = 'inference'`
    # therefore drops live lanes out of the Budget and Compute rollups while
    # looking like a no-op. The correct predicate excludes only the new class.
    import re as _re
    for rel in ("server/routes/inference.py", "server/routes/compute.py"):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            src = fh.read()
        # Anchored on AND/WHERE so it matches a real filter clause, not prose
        # in a comment that happens to quote the wrong predicate.
        #
        # EXEMPT: a query that also filters `cost_method = 'unpriced'` is the
        # re-pricing BACKFILL (inference.py reprice_unpriced), not a rollup
        # reader, and there the equality filter is REQUIRED rather than wrong.
        # cost_method defaults to 'unpriced' (init-db.sql:1559) and
        # synaptive_handler's purpose='shape' INSERT never sets it, so those
        # rows DO match `cost_method='unpriced'` — they are local, provider
        # 'local', deliberately est_cost_usd 0.0 embedding rows. Dropping the
        # purpose filter there would send the backfill to price them through
        # estimate_call_cost_usd() and invent money for work that is free.
        # This gate is about read-side rollups silently losing lanes; it must
        # not push a writer into fabricating costs.
        #
        # EXEMPT for the SAME reason (2026-09-15): reprice_cache_blind, the
        # second backfill, scoped by `NOT (metadata ? 'priced')`. It re-derives
        # rows priced before the estimate knew about caching, and it has to stay
        # on purpose='inference' for exactly the reason above — synaptive's
        # 'shape' rows are deliberately free and are not its business. That
        # marker predicate is named literally rather than exempting any query
        # with a cost_method filter, because compute.py's rollups legitimately
        # carry `FILTER (WHERE cost_method = 'list')` and must never be exempt.
        bad_re = _re.compile(r"(?:AND|WHERE)\s+purpose\s*=\s*'inference'")
        n_bad = 0
        for m in bad_re.finditer(src):
            # Look at the enclosing SQL literal, not the whole file.
            start = src.rfind('"""', 0, m.start())
            end = src.find('"""', m.end())
            stmt = src[start:end] if start != -1 and end != -1 else ""
            if _re.search(r"cost_method\s*=\s*'unpriced'", stmt):
                continue  # the backfill — correctly scoped
            if "NOT (metadata ? 'priced')" in stmt:
                continue  # the cache-aware backfill — correctly scoped
            n_bad += 1
        n_good = len(_re.findall(
            r"purpose IS DISTINCT FROM 'inference_error'", src))
        check(f"{rel}: no equality filter on purpose", n_bad == 0, str(n_bad))
        check(f"{rel}: excludes error rows explicitly", n_good >= 3, str(n_good))

    print()
    # smoke_test.sh greps the LAST line for "PASS" — same contract as
    # test_inference_usage_telemetry.py.
    if FAILED:
        print(f"FAILED — {FAILED} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
