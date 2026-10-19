#!/usr/bin/env python3
"""Inference usage telemetry — the log line and the DB row both have to land.

Two measured defects, both silent:

  1. Nothing configured the ROOT logger in the entity process (entrypoint.sh:516
     runs uvicorn with --log-level info, which only touches uvicorn's own
     loggers), so every INFO from mastercode.* went to Python's lastResort
     handler and was dropped at WARNING. Measured on one Host: zero
     INFERENCE_USAGE lines in 2,500 log lines. Fixed in server/main.py.
  2. _log_inference's DB half wrote to `inference_log`, a table that exists in
     no init-db.sql or migration, and it could never run anyway because
     set_inference_pool() had no caller. It now writes inference_cost_log —
     the table the Compute app reads — and brain_service.initialize() sets the
     pool.

Plus the tool-call round noise: "reasoning but no content" fired on every
tool-call round of every reasoning model, because empty content plus tool_calls
is normal and the tool_calls check happened 70 lines later.

Pure: no network, no DB (the pool is a recorder double). Run in-container by
smoke_test.sh.
"""
import asyncio
import json
import os
import sys

FAILED = 0


def check(label: str, cond: bool) -> None:
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED += 1


class FakePool:
    """Records every execute() the way asyncpg's Pool would accept it."""

    def __init__(self, raises: bool = False):
        self.calls: list[tuple] = []
        self.raises = raises

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.raises:
            raise RuntimeError("simulated write failure")
        return "INSERT 0 1"


def main() -> int:
    sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
    sys.path.insert(0, os.path.join(os.environ.get("APP_ROOT", "/app"), "brain"))
    from mastercode import provider_gateway as gw

    print("_log_inference — no pool means no write, and never an exception")
    gw.set_inference_pool(None)
    asyncio.run(gw._log_inference(
        source="unit", model="m", provider="p", total_tokens=10,
        prompt_tokens=7, completion_tokens=3, entity_id=1))
    check("returns cleanly with _inference_pool unset", gw._inference_pool is None)

    print("\n_log_inference — with a pool, one row into inference_cost_log")
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_inference(
        source="host_turn", model="claude-sonnet-4-6", provider="anthropic",
        total_tokens=1234, elapsed_ms=987.6,
        prompt_tokens=1000, completion_tokens=234, entity_id=9003,
        cache_read_tokens=800, cache_write_tokens=50,
    ))
    check("exactly one INSERT issued", len(pool.calls) == 1)
    sql, args = pool.calls[0] if pool.calls else ("", ())
    check("the target table is inference_cost_log (the one that EXISTS)",
          "INSERT INTO inference_cost_log" in sql)
    check("it does NOT write the phantom inference_log table",
          "INTO inference_log" not in sql)
    for col in ("entity_id", "source_module", "model_id", "provider", "purpose",
                "tokens_in", "tokens_out", "est_cost_usd", "cost_method",
                "latency_ms", "metadata",
                # migrations/v148 (2026-09-15) — the cached subset as columns,
                # so a rollup can sum it without parsing JSON.
                "cache_read_tokens", "cache_write_tokens"):
        check(f"column {col} is in the INSERT", col in sql)
    check("entity_id is the caller's", args[0] == 9003)
    check("source_module carries the gateway's `source`", args[1] == "host_turn")
    check("model_id carries the model", args[2] == "claude-sonnet-4-6")
    check("provider carries the provider", args[3] == "anthropic")
    # Positional indices shifted +1 from args[4] on 2026-08-28: `purpose` is
    # now a BOUND param ($5) instead of the SQL literal 'inference', so failed
    # calls can share this ledger as 'inference_error' (see
    # scripts/test_background_activity.py). Everything from tokens_in right
    # moved one place.
    check("purpose is bound, and defaults to 'inference'", args[4] == "inference")
    check("tokens_in is the lane-normalized TOTAL input", args[5] == 1000)
    check("tokens_out is completion_tokens", args[6] == 234)
    # est_cost_usd + cost_method (2026-08-24). This used to assert the SQL held
    # a literal 0.0 — "no rate card in the pod". The premise went stale: the
    # pod warms the vendors' own published rates into _CATALOG_CACHE /
    # _DIRECT_MODELS_CACHE at startup, which is what the model picker renders.
    # est_cost_usd is now priced from those, and cost_method says which basis
    # applied so a 0 can never be read as "free". With a COLD catalog (this
    # harness) the honest answer is 'unpriced' — assert that, because a cold
    # cache must degrade to a labelled zero and never to an invented price.
    check("est_cost_usd is bound, not a hardcoded literal",
          "0.0," not in sql and "$8" in sql)
    check("a cold catalog degrades to a labelled zero, never an invented price",
          args[7] == 0.0 and args[8] == "unpriced")
    check("latency_ms is an int (the column is INTEGER)",
          isinstance(args[9], int) and args[9] == 987)
    meta = json.loads(args[10])
    check("metadata carries cache_read_tokens", meta.get("cache_read_tokens") == 800)
    check("metadata carries cache_write_tokens", meta.get("cache_write_tokens") == 50)
    check("metadata carries total_tokens", meta.get("total_tokens") == 1234)
    # v148 columns. They carry the SAME counts the metadata does — the JSON is
    # what every row written before that migration has, and the columns are what
    # a GROUP BY can sum. Appended to the column list on purpose, so every
    # positional index asserted above is unmoved.
    check("cache_read_tokens is bound as a column, not only in the JSON",
          args[11] == 800)
    check("cache_write_tokens is bound as a column", args[12] == 50)
    # The "priced" marker (2026-09-15) says WHICH rate each part of the cost got
    # — published cache rate vs a fallback to the input rate. It must never
    # appear on a row that was not priced: an unpriced row with a marker claims
    # a derivation that never ran, and its ABSENCE is what reprice_cache_blind
    # uses to find rows written by the old cache-blind formula.
    check("no 'priced' marker on an unpriced row (cold catalog here)",
          "priced" not in meta)

    print("\n_log_inference — with a WARM catalog, the cached subset is priced "
          "at the cache rate")
    # The defect: prompt_tokens is the lane-normalized TOTAL input with the
    # cached subset INCLUDED (provider_gateway:1335-1337, :111-181), and it
    # was all priced at the input rate. Vendors charge a fraction for a cache
    # read (verified live 2026-09-15: anthropic/claude-opus-5 publishes prompt
    # 0.000005 and input_cache_read 0.0000005), and hit rates run ~84%.
    from server.routes import inference as inf
    _prev_cat = inf._CATALOG_CACHE.get("data")
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "vendor/cached", "input_per_token": "0.000010",
         "output_per_token": "0.000050",
         "cache_read_per_token": 0.000001, "cache_write_per_token": 0.0000125},
    ]}
    inf._DIRECT_MODELS_CACHE.clear()
    try:
        pool = FakePool()
        gw.set_inference_pool(pool)
        asyncio.run(gw._log_inference(
            source="host_turn", model="vendor/cached", provider="openrouter",
            total_tokens=10500, elapsed_ms=1.0,
            prompt_tokens=10000, completion_tokens=500, entity_id=9003,
            cache_read_tokens=8000, cache_write_tokens=1000,
        ))
        _, args = pool.calls[0]
        want = 1000 * 1e-5 + 8000 * 1e-6 + 1000 * 1.25e-5 + 500 * 5e-5
        blind = 10000 * 1e-5 + 500 * 5e-5
        check("est_cost_usd prices uncached / cache-read / cache-write apart",
              abs(args[7] - want) < 1e-12)
        check("which is LESS than the whole-input-at-input-rate number it used "
              "to write", args[7] < blind)
        check("cost_method is still one of v113's four values ('list')",
              args[8] == "list")
        _m = json.loads(args[10])
        check("the row records WHICH rates applied, for a later reprice",
              _m.get("priced", {}).get("cache_read") == "published"
              and _m.get("priced", {}).get("cache_write") == "published")
    finally:
        inf._CATALOG_CACHE["data"] = _prev_cat

    print("\n_normalize_openai_usage — prompt_tokens is the TRUE total input, "
          "whichever cache convention arrived")
    # TWO conventions come through one wire. cached_tokens is a SUBSET of
    # prompt_tokens; Anthropic's cache_read/cache_creation fields sit BESIDE an
    # input that EXCLUDES them. Unnormalised, the second shape makes the
    # estimator compute uncached = total - read - write, go NEGATIVE, clamp at
    # zero, and price the call's real uncached input at $0.
    t, p_, c, cr, cw = gw._normalize_openai_usage({
        "total_tokens": 10500, "prompt_tokens": 10000, "completion_tokens": 500,
        "prompt_tokens_details": {"cached_tokens": 8000}})
    check("cached_tokens is a SUBSET — prompt_tokens is left alone",
          (t, p_, c, cr, cw) == (10500, 10000, 500, 8000, 0))
    t, p_, c, cr, cw = gw._normalize_openai_usage(
        {"total_tokens": 1500, "prompt_tokens": 1000, "completion_tokens": 500,
         "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 1000},
        provider="passthrough", model="m")
    check("the passthrough shape re-adds the cache counts to the input",
          p_ == 10000 and cr == 8000 and cw == 1000)
    check("and the total is raised with it, never left inconsistent", t == 10500)
    check("completion is untouched", c == 500)
    # A passthrough row, priced end to end: without the normalisation the
    # estimator sees 1000 input against 9000 cached, clamps, and charges the
    # 1000 real uncached tokens nothing.
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "vendor/cached", "input_per_token": "0.000010",
         "output_per_token": "0.000050",
         "cache_read_per_token": 0.000001, "cache_write_per_token": 0.0000125},
    ]}
    inf._DIRECT_MODELS_CACHE.clear()
    try:
        normalised, _ = inf.estimate_call_cost_detail(
            "vendor/cached", p_, c, cache_read_tokens=cr, cache_write_tokens=cw)
        unnormalised, marker = inf.estimate_call_cost_detail(
            "vendor/cached", 1000, 500, cache_read_tokens=8000,
            cache_write_tokens=1000)
        check("normalised, the 1,000 uncached tokens are actually charged",
              abs(normalised - (1000 * 1e-5 + 8000 * 1e-6 + 1000 * 1.25e-5
                                + 500 * 5e-5)) < 1e-12)
        check("unnormalised they would have priced at $0 (the clamp fires)",
              marker.get("uncached") == "clamped"
              and abs(unnormalised - (normalised - 1000 * 1e-5)) < 1e-12)
    finally:
        inf._CATALOG_CACHE["data"] = _prev_cat
    # The arithmetic test, not a provider allowlist: a provider that uses the
    # Anthropic field NAMES but already includes them in its input is reporting
    # a conforming total and must not be double-counted.
    t, p_, c, cr, cw = gw._normalize_openai_usage({
        "total_tokens": 10500, "prompt_tokens": 10000, "completion_tokens": 500,
        "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 1000})
    check("an input that CAN contain the counts is already a total — no re-add",
          p_ == 10000 and t == 10500)
    t, p_, c, cr, cw = gw._normalize_openai_usage({})
    check("an empty usage object yields no counts and never raises",
          (t, p_, c, cr, cw) == (0, None, None, 0, 0))
    # The clamp survives as the last line of defence, but it is now a WARNING:
    # if it fires, a lane is mis-reporting and the normalisation has to learn
    # its shape — that cannot be a debug line nobody sees.
    import inspect as _inspect
    _src = _inspect.getsource(inf.estimate_call_cost_detail)
    check("the estimator's clamp logs at WARNING, with provider + both counts",
          "logger.warning(" in _src and 'provider or "?"' in _src
          and "cache_read=%d + cache_write=%d" in _src)

    print("\n_log_inference — the _call_local shape (no token counts at all)")
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_inference(
        source="local_turn", model="qwen", provider="local",
        total_tokens=42, elapsed_ms=10.0, entity_id=9003))
    _, args = pool.calls[0]
    check("tokens_in coalesces to 0 (column is NOT NULL DEFAULT 0)",
          args[5] == 0)
    check("tokens_out coalesces to 0", args[6] == 0)
    check("cache fields still present, as zeros",
          json.loads(args[10]).get("cache_read_tokens") == 0)

    print("\n_log_inference — caller metadata survives the cache merge")
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_inference(
        source="s", model="m", provider="p", total_tokens=1,
        entity_id=9003, metadata={"route": "fallback"}))
    check("the passed-in metadata key is still there",
          json.loads(pool.calls[0][1][10]).get("route") == "fallback")

    print("\n_log_inference — entity_id falls back to ENTITY_ID (readers filter on it)")
    _prev = os.environ.get("ENTITY_ID")
    os.environ["ENTITY_ID"] = "7"
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_inference(source="s", model="m", provider="p",
                                  total_tokens=1, entity_id=None))
    check("a None entity_id becomes the pod's ENTITY_ID", pool.calls[0][1][0] == 7)
    os.environ.pop("ENTITY_ID")
    pool = FakePool()
    gw.set_inference_pool(pool)
    asyncio.run(gw._log_inference(source="s", model="m", provider="p",
                                  total_tokens=1, entity_id=None))
    check("with no ENTITY_ID it stays NULL rather than guessing",
          pool.calls[0][1][0] is None)
    if _prev is not None:
        os.environ["ENTITY_ID"] = _prev

    print("\n_log_inference — a write failure is warned, never raised")
    gw.set_inference_pool(FakePool(raises=True))
    try:
        asyncio.run(gw._log_inference(source="s", model="m", provider="p",
                                      total_tokens=1, entity_id=1))
        check("a failing pool does not propagate the exception", True)
    except Exception:
        check("a failing pool does not propagate the exception", False)
    gw.set_inference_pool(None)

    print("\n_reasoning_only_reply — the tool-call round must not warn")
    reasoning_msg = {"content": "", "reasoning": "let me think about this"}
    check("reasoning + empty content + NO tool_calls -> warn",
          gw._reasoning_only_reply(reasoning_msg, "") is True)
    check("reasoning + empty content + tool_calls -> do NOT warn (the noise case)",
          gw._reasoning_only_reply(
              {**reasoning_msg,
               "tool_calls": [{"id": "c1", "function": {"name": "read_file"}}]},
              "") is False)
    check("reasoning_content is read too (the DeepSeek field name)",
          gw._reasoning_only_reply(
              {"content": "", "reasoning_content": "planning"}, "") is True)
    check("content present -> never warn, tool_calls or not",
          gw._reasoning_only_reply(reasoning_msg, "here is the answer") is False)
    check("no reasoning at all -> never warn",
          gw._reasoning_only_reply({"content": ""}, "") is False)
    check("an empty tool_calls list is not a tool-call round",
          gw._reasoning_only_reply({**reasoning_msg, "tool_calls": []}, "") is True)
    check("a non-dict message is handled",
          gw._reasoning_only_reply(None, "") is False)

    print("\nroot logging — server/main.py configures it before the app imports")
    root = os.environ.get("APP_ROOT", "/app")
    main_py = os.path.join(root, "server/main.py")
    src = open(main_py, encoding="utf-8").read() if os.path.exists(main_py) else ""
    check("server/main.py located", bool(src))
    check("the root logger is configured", "logging.basicConfig(" in src)
    check("the level comes from LOG_LEVEL with an INFO default",
          'os.environ.get("LOG_LEVEL", "INFO")' in src)
    check("the format carries the logger NAME (so the module is attributable)",
          "%(name)s" in src)
    check("it runs BEFORE the app imports, or the loggers built at import "
          "time would already be attached",
          src.index("logging.basicConfig(")
          < src.index("from server.middleware.localhost_only"))
    check("the noisy third-party loggers are quieted",
          all(f'"{n}"' in src for n in ("aiohttp", "httpx", "httpcore", "asyncio")))

    print("\nLOG_LEVEL — a typo must not crash-loop the pod at import")
    # Exercise the SHIPPED resolver without importing server.main (that pulls
    # the whole app in). Lift the function out of the source by AST and run it.
    # basicConfig(level="BOGUS") raises ValueError, and an unhandled raise at
    # module import is a pod that never starts because an env var was misspelled.
    import ast
    import logging as _logging
    _fn = next((n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef)
                and n.name == "_resolve_log_level"), None)
    check("_resolve_log_level is a module-level function in server/main.py",
          _fn is not None)
    _ns = {"logging": _logging}
    exec(compile(ast.Module(body=[_fn], type_ignores=[]), "<main.py>", "exec"), _ns)
    resolve = _ns["_resolve_log_level"]
    check("INFO resolves to logging.INFO", resolve("INFO") == _logging.INFO)
    check("a lowercase value still resolves", resolve("debug") == _logging.DEBUG)
    check("surrounding whitespace is tolerated",
          resolve("  warning ") == _logging.WARNING)
    check("a BOGUS value resolves to None (the fallback signal), not a raise",
          resolve("bogus") is None)
    check("an empty value resolves to None", resolve("") is None)
    check("None resolves to None", resolve(None) is None)
    check("the caller's fallback is INFO",
          "logging.INFO" in src.split("logging.basicConfig(")[1].split(")")[0])
    check("the bad value is named in a warning rather than swallowed",
          "names no logging level" in src)
    # And prove what is being defended against: getLevelName returns a STRING
    # for an unknown name, which is exactly the input basicConfig rejects.
    check("getLevelName('BOGUS') is a str, so isinstance(int) is the right test",
          isinstance(_logging.getLevelName("BOGUS"), str))
    try:
        _logging.getLogger("telemetry-test-probe").setLevel("BOGUS")
        check("a raw bad level IS rejected by logging (the crash this prevents)",
              False)
    except ValueError:
        check("a raw bad level IS rejected by logging (the crash this prevents)",
              True)

    print("\npool wiring — brain_service hands the gateway its pool at startup")
    bsrc = open(os.path.join(root, "server/brain_service.py"),
                encoding="utf-8").read()
    check("brain_service calls set_inference_pool", "set_inference_pool(_pool)" in bsrc)
    check("it is wired after the pool exists (create_pool comes first)",
          bsrc.index("asyncpg.create_pool") < bsrc.index("set_inference_pool(_pool)"))

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
