#!/usr/bin/env python3
"""Gate: per-provider catalog row parsing + per-key base resolution
(Orchestrator 0.7.0).

Pins, against measured provider shapes (2026-08-10):
  1. Together pricing parses — {pricing:{input,output}} is USD per MILLION
     (moonshotai/Kimi-K3 read 3/15 live = the vendor's $3/$15 list). A
     dedicated-endpoint-only model (input/output 0, hourly>0) stays UNPRICED
     — a per-token 'free' there would be a fabrication.
  2. The OpenRouter / DeepInfra / cents shapes still parse (no regression).
  3. First-party enrichment: openai/anthropic/xai rows with no published
     price/context fill from the warm OpenRouter catalog cache by slug
     suffix ('-' boundary for dated ids), carry price_source provenance, and
     NEVER apply to generic hosts (another host's price).
  4. resolve_base_url_sync: a key the registry base 401s and the alt base
     accepts resolves to the alt (the Kimi-for-Coding subscription class);
     an accepted registry base wins; transport failure NEVER switches lanes;
     providers with no alt bases probe nothing.
  5. The embedding-model filter: explicit type wins; 'embed' in the id is
     the fallback; chat models never pass.

Offline: urllib openers are stubbed — no live Postgres, no network.

Run: APP_ROOT=<repo> python3 scripts/test_direct_models_pricing.py
"""
import os
import sys

failures = []


def check(label, ok, detail=""):
    print(f"[direct-models] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main():
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))

    from server.routes import inference as inf
    from server.routes.embedding import _looks_like_embedding_model
    from server.vault import providers as prov

    # ── 1. Together per-1M pricing ──────────────────────────────────
    r = inf._dm_row({"id": "moonshotai/Kimi-K3", "display_name": "Kimi K3",
                     "context_length": 1000000,
                     "pricing": {"hourly": 0, "input": 3, "output": 15,
                                 "cached_input": 0.3}})
    check("together input/output parse as $/1M",
          r and abs(r["input_per_token"] - 3e-6) < 1e-12
          and abs(r["output_per_token"] - 15e-6) < 1e-12, repr(r))
    r = inf._dm_row({"id": "x/dedicated-only",
                     "pricing": {"hourly": 2.5, "input": 0, "output": 0}})
    check("together dedicated-only (hourly, 0/0) stays unpriced",
          r is not None and "input_per_token" not in r, repr(r))
    r = inf._dm_row({"id": "x/free", "pricing": {"hourly": 0, "input": 0, "output": 0}})
    check("together true-free (0/0, no hourly) prices as 0/0",
          r and r.get("input_per_token") == 0.0 and r.get("output_per_token") == 0.0,
          repr(r))
    r = inf._dm_row({"id": "e/bge", "type": "embedding", "pricing": {"input": 0.01, "output": 0}})
    check("together type field passes through", r and r.get("type") == "embedding", repr(r))

    # ── 1b. Anthropic /v1/models: the window is max_input_tokens ─────
    # Live shape 2026-09-06. max_tokens is the OUTPUT cap here, and reading
    # it as the window recorded 128k for a 1M model — the turn budget then
    # spent against the wrong number.
    r = inf._dm_row({"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1",
                     "max_input_tokens": 1000000, "max_tokens": 128000})
    check("anthropic window comes from max_input_tokens, not the output cap",
          r and r.get("context_length") == 1000000, repr(r))
    r = inf._dm_row({"id": "legacy/window-as-max-tokens", "max_tokens": 32768})
    check("max_tokens still means the window when nothing else is published",
          r and r.get("context_length") == 32768, repr(r))

    # ── 2. Existing shapes unregressed ──────────────────────────────
    r = inf._dm_row({"id": "openai/gpt", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}})
    check("openrouter prompt/completion (USD/token strings) still parse",
          r and abs(r["input_per_token"] - 2.5e-6) < 1e-12, repr(r))
    r = inf._dm_row({"id": "di/keyed", "metadata": {"pricing": {"input_tokens": 0.05, "output_tokens": 0.21}}})
    check("deepinfra keyed metadata pricing still parses ($/1M)",
          r and abs(r["input_per_token"] - 0.05e-6) < 1e-15, repr(r))
    r = inf._dm_row({"id": "di/public", "pricing": {"cents_per_input_token": 0.0000005,
                                                    "cents_per_output_token": 0.0000021}})
    check("deepinfra public cents shape still parses",
          r and abs(r["input_per_token"] - 0.0000005 / 100.0) < 1e-15, repr(r))

    # ── 3. First-party OpenRouter enrichment ────────────────────────
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "openai/gpt-5.2", "input_per_token": "0.00000125",
         "output_per_token": "0.00001", "context_length": 400000},
        {"id": "anthropic/claude-opus-5", "input_per_token": "0.000005",
         "output_per_token": "0.000025", "context_length": 200000},
        {"id": "openai/gpt-oss-120b:free", "input_per_token": "0",
         "output_per_token": "0", "context_length": 131072},
    ]}
    rows = [{"id": "gpt-5.2"}, {"id": "gpt-unknown-model"}]
    inf._enrich_first_party_from_openrouter("openai", rows)
    check("openai exact-suffix row fills price + context + provenance",
          rows[0].get("input_per_token") == 1.25e-6
          and rows[0].get("context_length") == 400000
          and rows[0].get("price_source") == "vendor list via OpenRouter", repr(rows[0]))
    check("unmatched openai row stays honestly absent",
          "input_per_token" not in rows[1], repr(rows[1]))
    rows = [{"id": "claude-opus-5-20260115"}]
    inf._enrich_first_party_from_openrouter("anthropic", rows)
    check("dated anthropic id matches on the '-' boundary",
          rows[0].get("input_per_token") == 5e-6, repr(rows[0]))
    rows = [{"id": "gpt-5.2"}]
    inf._enrich_first_party_from_openrouter("groq", rows)
    check("generic hosts are NEVER OR-enriched (their prices are their own)",
          "input_per_token" not in rows[0], repr(rows[0]))
    rows = [{"id": "gpt-oss-120b"}]
    inf._enrich_first_party_from_openrouter("openai", rows)
    check(":free variant slugs are excluded from the enrichment map",
          "input_per_token" not in rows[0], repr(rows[0]))
    sub = [{"id": "k3", "subscription": True}]
    inf._enrich_first_party_from_openrouter("kimi", sub)
    check("subscription rows never get a per-token price grafted on",
          "input_per_token" not in sub[0], repr(sub[0]))
    # OpenRouter spells Anthropic versions with dots, Anthropic with dashes
    # (live catalog 2026-09-27). claude-opus-5-5 used to fall through the '-'
    # boundary rule onto claude-opus-5's row and price at $5/$25.
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "anthropic/claude-opus-5", "input_per_token": "0.000005",
         "output_per_token": "0.000025", "cache_read_per_token": "0.0000005"},
        {"id": "anthropic/claude-opus-5.5", "input_per_token": "0.000004",
         "output_per_token": "0.00002", "cache_read_per_token": "0.0000002"},
        {"id": "anthropic/claude-haiku-4.5", "input_per_token": "0.000001",
         "output_per_token": "0.000005"},
        {"id": "openai/gpt-5.2", "input_per_token": "0.00000125",
         "output_per_token": "0.00001"},
    ]}
    rows = [{"id": "claude-opus-5-5"}, {"id": "claude-opus-5"},
            {"id": "claude-haiku-4-5-20251001"}]
    inf._enrich_first_party_from_openrouter("anthropic", rows)
    check("claude-opus-5-5 takes anthropic/claude-opus-5.5's row, not claude-opus-5's",
          rows[0].get("input_per_token") == 4e-6
          and rows[0].get("output_per_token") == 2e-5
          and rows[0].get("cache_read_per_token") == 2e-7, repr(rows[0]))
    check("claude-opus-5 still takes its own row",
          rows[1].get("input_per_token") == 5e-6
          and rows[1].get("cache_read_per_token") == 5e-7, repr(rows[1]))
    check("a dated dashed anthropic id matches a dotted OpenRouter slug",
          rows[2].get("input_per_token") == 1e-6, repr(rows[2]))
    rows = [{"id": "gpt-5.2"}]
    inf._enrich_first_party_from_openrouter("openai", rows)
    check("openai ids keep their dot (the dash rewrite is anthropic-only)",
          rows[0].get("input_per_token") == 1.25e-6, repr(rows[0]))
    inf._CATALOG_CACHE["data"] = None

    # ── 4. Per-key base resolution ──────────────────────────────────
    import urllib.error
    import urllib.request

    class FakeOpener:
        """verdicts: url-prefix -> HTTP code (200 accepts, 401/403 reject)
        or 'net' (transport failure)."""
        def __init__(self, verdicts):
            self.verdicts = verdicts
            self.opened = []

        def open(self, req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            self.opened.append(url)
            for prefix, v in self.verdicts.items():
                if url.startswith(prefix):
                    if v == 200:
                        class R:  # minimal read()able
                            def read(self, n=-1):
                                return b"{}"
                        return R()
                    if v == "net":
                        raise OSError("unreachable")
                    raise urllib.error.HTTPError(url, v, "x", {}, None)
            raise AssertionError(f"unexpected probe url {url}")

    real_build = urllib.request.build_opener
    kimi_default = prov.PROVIDERS["kimi"]["base_url"].rstrip("/")
    alt = prov._ALT_BASE_URLS["kimi"][0].rstrip("/")
    try:
        # subscription key: default 401s, alt accepts → alt wins, cached
        fake = FakeOpener({kimi_default: 401, alt: 200})
        urllib.request.build_opener = lambda *a, **k: fake
        prov._BASE_URL_CACHE.clear()
        got = prov.resolve_base_url_sync("kimi", "sk-sub-key")
        check("subscription-class key resolves to the alt base", got == alt, got)
        probes = len(fake.opened)
        got2 = prov.resolve_base_url_sync("kimi", "sk-sub-key")
        check("resolution caches per key (no re-probe)",
              got2 == alt and len(fake.opened) == probes, f"{got2} probes={len(fake.opened)}")

        # platform key: default accepts → default, alt never probed
        fake = FakeOpener({kimi_default: 200})
        urllib.request.build_opener = lambda *a, **k: fake
        prov._BASE_URL_CACHE.clear()
        got = prov.resolve_base_url_sync("kimi", "sk-platform-key")
        check("platform-class key stays on the registry base",
              got == kimi_default and all(u.startswith(kimi_default) for u in fake.opened),
              f"{got} {fake.opened}")

        # transport failure: NEVER switches lanes
        fake = FakeOpener({kimi_default: "net", alt: 200})
        urllib.request.build_opener = lambda *a, **k: fake
        prov._BASE_URL_CACHE.clear()
        got = prov.resolve_base_url_sync("kimi", "sk-any")
        check("a transport blip never switches the lane",
              got == kimi_default, got)

        # both reject → default reported (upstream 401 stays truthful)
        fake = FakeOpener({kimi_default: 401, alt: 403})
        urllib.request.build_opener = lambda *a, **k: fake
        prov._BASE_URL_CACHE.clear()
        got = prov.resolve_base_url_sync("kimi", "sk-dead")
        check("every-base-rejected reports the default base",
              got == kimi_default, got)

        # no-alt provider: zero probes
        fake = FakeOpener({})
        urllib.request.build_opener = lambda *a, **k: fake
        got = prov.resolve_base_url_sync("openai", "sk-x")
        check("providers with no alt bases probe nothing",
              got == prov.PROVIDERS["openai"]["base_url"].rstrip("/")
              and not fake.opened, f"{got} {fake.opened}")
    finally:
        urllib.request.build_opener = real_build
        prov._BASE_URL_CACHE.clear()

    check("kimi alt base is flagged subscription",
          prov.is_subscription_base(alt) and not prov.is_subscription_base(kimi_default))

    # ── 5. Embedding-model filter ───────────────────────────────────
    check("explicit type=embedding passes",
          _looks_like_embedding_model({"id": "BAAI/bge-base-en-v1.5", "type": "embedding"}))
    check("explicit type=chat fails even with 'embed' in the id",
          not _looks_like_embedding_model({"id": "chat-embedded", "type": "chat"}))
    check("id fallback: text-embedding-3-large passes",
          _looks_like_embedding_model({"id": "text-embedding-3-large"}))
    check("id fallback: gpt-5.2 fails",
          not _looks_like_embedding_model({"id": "gpt-5.2"}))

    # ── 6. Together serving marks (measured live on one Host, 2026-08-15) ──
    # Ground truth: every id below was called with a real chat completion.
    # WORKED: Qwen/Qwen2.5-7B-Instruct-Turbo (serverless);
    #         arize-ai/qwen-2-1.5b-instruct (the account's OWN dedicated
    #         endpoint, STARTED — this is why it answered).
    # 400 "Unable to access non-serverless model":
    #         mistralai/Mixtral-8x7B-Instruct-v0.1 (no endpoint at all).
    eps = [
        {"model": "Qwen/Qwen2.5-7B-Instruct-Turbo", "type": "serverless",
         "state": "STARTED"},
        {"model": "arize-ai/qwen-2-1.5b-instruct", "type": "dedicated",
         "state": "STARTED"},
        {"model": "some/stopped-dedicated", "type": "dedicated",
         "state": "STOPPED"},
    ]
    rows = {i: {"id": i} for i in (
        "Qwen/Qwen2.5-7B-Instruct-Turbo",
        "arize-ai/qwen-2-1.5b-instruct",
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "some/stopped-dedicated",
    )}
    inf._together_mark_serving(rows, eps)
    check("serverless model marked serving=serverless",
          rows["Qwen/Qwen2.5-7B-Instruct-Turbo"].get("serving") == "serverless"
          and "needs_endpoint" not in rows["Qwen/Qwen2.5-7B-Instruct-Turbo"])
    check("STARTED dedicated endpoint marked serving=dedicated",
          rows["arize-ai/qwen-2-1.5b-instruct"].get("serving") == "dedicated"
          and "needs_endpoint" not in rows["arize-ai/qwen-2-1.5b-instruct"])
    check("model with no endpoint is flagged needs_endpoint",
          rows["mistralai/Mixtral-8x7B-Instruct-v0.1"].get("needs_endpoint") is True
          and "serving" not in rows["mistralai/Mixtral-8x7B-Instruct-v0.1"])
    check("STOPPED dedicated endpoint is NOT callable",
          rows["some/stopped-dedicated"].get("needs_endpoint") is True,
          repr(rows["some/stopped-dedicated"]))
    # Marking must never DROP a row: endpoint ids are not model ids — the
    # endpoint 'thinkingmachines/Inkling' serves catalog model
    # 'Inkling-Small', which answered 200 despite no exact match. Filtering
    # on this signal would have hidden a working model.
    check("marking never removes a row", len(rows) == 4, str(len(rows)))
    # NO LIST MEANS NO VERDICT. An empty or unparseable endpoint list marks
    # NOTHING: "every model in this catalog is broken" is far worse to say
    # wrongly than to say nothing, and an empty list is exactly what a renamed
    # response wrapper would look like from in here.
    for empty in ([], None):
        rows2 = {"a/b": {"id": "a/b"}}
        inf._together_mark_serving(rows2, empty)
        check(f"empty endpoint list ({empty!r}) marks nothing at all",
              "needs_endpoint" not in rows2["a/b"]
              and "serving" not in rows2["a/b"], repr(rows2))
    # But a list that DOES parse and simply serves nothing is real evidence —
    # that still marks. 'We learned nothing' vs 'we learned nothing is served'.
    rows3 = {"a/b": {"id": "a/b"}}
    inf._together_mark_serving(
        rows3, [{"model": "other/thing", "type": "dedicated", "state": "STOPPED"}])
    check("a parsed list that serves nothing still flags the rows",
          rows3["a/b"].get("needs_endpoint") is True, repr(rows3))
    # Junk entries must be skipped, not raise and not be read as serving.
    rows4 = {"a/b": {"id": "a/b"}}
    inf._together_mark_serving(rows4, ["nonsense", None, {}, {"type": "serverless"}])
    check("malformed endpoint entries are skipped without raising",
          rows4["a/b"].get("needs_endpoint") is True
          and "serving" not in rows4["a/b"], repr(rows4))

    # ── 6. Cache rates + the cache-aware estimator (2026-09-15) ─────
    # WHY: est_cost_usd priced the WHOLE of tokens_in at the input rate, and
    # tokens_in is the lane-normalized TOTAL input with the cached subset
    # INCLUDED (provider_gateway:1335-1337 and :111-181). Cache reads cost a
    # fraction of input — verified live against openrouter.ai/api/v1/models on
    # 2026-09-15, anthropic/claude-opus-5 publishes prompt 0.000005 and
    # input_cache_read 0.0000005 — and hit rates run ~84%, so the estimate ran
    # several times the vendor's bill. These pin the parse and the arithmetic.
    r = inf._dm_row({"id": "or/cached", "pricing": {
        "prompt": "0.000005", "completion": "0.000025",
        "input_cache_read": "0.0000005", "input_cache_write": "0.00000625",
        "input_cache_write_1h": "0.00001"}})
    check("openrouter input_cache_read/write parse as USD/token",
          r and r.get("cache_read_per_token") == 5e-7
          and r.get("cache_write_per_token") == 6.25e-6, repr(r))
    check("input_cache_write_1h is NOT read (the gateway asks for no 1h TTL)",
          r and "cache_write_1h_per_token" not in r
          and r.get("cache_write_per_token") == 6.25e-6, repr(r))
    r = inf._dm_row({"id": "or/uncached",
                     "pricing": {"prompt": "0.000005", "completion": "0.000025"}})
    check("a model publishing no cache rate leaves the keys ABSENT, not 0.0",
          r and "cache_read_per_token" not in r
          and "cache_write_per_token" not in r, repr(r))
    r = inf._dm_row({"id": "moonshotai/Kimi-K3",
                     "pricing": {"hourly": 0, "input": 3, "output": 15,
                                 "cached_input": 0.3}})
    check("together cached_input parses as $/1M like its input/output",
          r and abs(r.get("cache_read_per_token", -1) - 0.3e-6) < 1e-15, repr(r))
    r = inf._dm_row({"id": "x/dedicated-only",
                     "pricing": {"hourly": 2.5, "input": 0, "output": 0,
                                 "cached_input": 0.3}})
    check("a cache rate is never attached to a row that stayed unpriced",
          r is not None and "cache_read_per_token" not in r, repr(r))
    # THE UNIT GUARD. Together's key name and per-MILLION unit are pinned by the
    # fixture above and NOT re-verified against a live key, so the thing that
    # could be silently wrong is the UNIT — read a per-token rate as per-million
    # and the cache prices a million times too high. A cache read dearer than
    # fresh input is not a rate any vendor sells, so that reading is treated as
    # unparsed: the key stays absent and the estimator falls back to the input
    # rate. Erring toward "we do not know" beats erring toward a huge number.
    r = inf._dm_row({"id": "x/units-wrong",
                     "pricing": {"hourly": 0, "input": 3, "output": 15,
                                 "cached_input": 30}})
    check("a cached_input DEARER than input is ignored, not believed",
          r and r.get("input_per_token") == 3e-6
          and "cache_read_per_token" not in r, repr(r))
    r = inf._dm_row({"id": "x/no-discount",
                     "pricing": {"hourly": 0, "input": 3, "output": 15,
                                 "cached_input": 3}})
    check("equal to the input rate is allowed — a provider need not discount",
          r and abs(r.get("cache_read_per_token", -1) - 3e-6) < 1e-15, repr(r))
    # The same rule on the OPENROUTER side, for the same reason in the other
    # direction: a published 0 cache rate beside a PAID input rate would zero
    # ~84% of a turn's input cost — a bigger error than the one this change
    # fixes, and indistinguishable from an upstream serialisation bug. Today's
    # live sweep found none (0 of 443), so it is treated as unpublished and
    # logged at debug, not warning.
    r = inf._dm_row({"id": "or/zero-cache-paid-input",
                     "pricing": {"prompt": "0.000005", "completion": "0.000025",
                                 "input_cache_read": "0"}})
    check("openrouter cache rate of 0 on a PAID model is not read as free",
          r and r.get("input_per_token") == 5e-6
          and "cache_read_per_token" not in r, repr(r))
    r = inf._dm_row({"id": "or/free-model",
                     "pricing": {"prompt": "0", "completion": "0",
                                 "input_cache_read": "0"}})
    check("but a 0 cache rate on a 0 input rate is a real, kept 0",
          r and r.get("input_per_token") == 0.0
          and r.get("cache_read_per_token") == 0.0, repr(r))
    r = inf._dm_row({"id": "or/zero-write",
                     "pricing": {"prompt": "0.000005", "completion": "0.000025",
                                 "input_cache_read": "0.0000005",
                                 "input_cache_write": "0"}})
    check("the rule applies per key — a real read rate survives a zero write",
          r and r.get("cache_read_per_token") == 5e-7
          and "cache_write_per_token" not in r, repr(r))
    r = inf._dm_row({"id": "x/free-cache",
                     "pricing": {"hourly": 0, "input": 3, "output": 15,
                                 "cached_input": 0}})
    check("a PUBLISHED zero is a published price and is kept (absent is not)",
          r and r.get("cache_read_per_token") == 0.0, repr(r))

    # The estimator itself. Catalog stubbed; no network, no DB.
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        # both rates published
        {"id": "v/full", "input_per_token": "0.000010",
         "output_per_token": "0.000050",
         "cache_read_per_token": 0.000001, "cache_write_per_token": 0.0000125},
        # read published, write NOT (the common moonshotai/deepseek shape)
        {"id": "v/readonly", "input_per_token": "0.000010",
         "output_per_token": "0.000050", "cache_read_per_token": 0.000001},
        # neither published
        {"id": "v/none", "input_per_token": "0.000010",
         "output_per_token": "0.000050"},
    ]}
    inf._DIRECT_MODELS_CACHE.clear()
    # 10,000 total input of which 8,000 cache reads and 1,000 cache writes ->
    # 1,000 uncached. 1000*1e-5 + 8000*1e-6 + 1000*1.25e-5 + 500*5e-5
    usd, priced = inf.estimate_call_cost_detail(
        "v/full", 10000, 500, cache_read_tokens=8000, cache_write_tokens=1000)
    want = 1000 * 1e-5 + 8000 * 1e-6 + 1000 * 1.25e-5 + 500 * 5e-5
    check("cached tokens are priced at the published cache rates",
          usd is not None and abs(usd - want) < 1e-12, f"{usd} != {want}")
    check("the marker records that both cache rates were published",
          priced.get("cache_read") == "published"
          and priced.get("cache_write") == "published", repr(priced))
    # The whole point: the cache-blind number is strictly higher.
    blind = inf.estimate_call_cost_usd("v/full", 10000, 500)
    check("the old whole-input-at-input-rate number was higher",
          blind > usd, f"blind={blind} cache_aware={usd}")
    check("passing no cache counts still gives exactly the old number",
          abs(blind - (10000 * 1e-5 + 500 * 5e-5)) < 1e-12, repr(blind))

    usd, priced = inf.estimate_call_cost_detail(
        "v/readonly", 10000, 0, cache_read_tokens=8000, cache_write_tokens=1000)
    want = 1000 * 1e-5 + 8000 * 1e-6 + 1000 * 1e-5
    check("an unpublished cache-WRITE rate falls back to the input rate",
          usd is not None and abs(usd - want) < 1e-12, f"{usd} != {want}")
    check("and the fallback is LABELLED, not silently exact",
          priced.get("cache_read") == "published"
          and priced.get("cache_write") == "input_rate_fallback", repr(priced))

    usd, priced = inf.estimate_call_cost_detail(
        "v/none", 10000, 0, cache_read_tokens=8000)
    check("no published cache rate at all -> input rate, never 0.0",
          usd is not None and abs(usd - 10000 * 1e-5) < 1e-12, repr(usd))
    check("labelled input_rate_fallback so a later pass can tell",
          priced.get("cache_read") == "input_rate_fallback", repr(priced))

    # THE CLAMP. The two wires disagree about whether prompt_tokens already
    # includes the cache-CREATION count, so cached can exceed tokens_in. A
    # negative uncached remainder would CREDIT the estimate.
    usd, priced = inf.estimate_call_cost_detail(
        "v/full", 100, 0, cache_read_tokens=8000, cache_write_tokens=1000)
    check("uncached input is clamped at 0, never negative",
          usd is not None and abs(usd - (8000 * 1e-6 + 1000 * 1.25e-5)) < 1e-12,
          repr(usd))
    check("the clamp is recorded on the row",
          priced.get("uncached") == "clamped", repr(priced))

    check("an unknown model is still None, not a zero",
          inf.estimate_call_cost_usd("v/nonexistent", 10, 10,
                                     cache_read_tokens=5) is None)
    p = inf.cached_model_prices()["v/full"]
    check("cached_model_prices stays index-compatible ([0]/[1] unchanged)",
          p[0] == 1e-5 and p[1] == 5e-5, repr(p))
    check("and an unpublished cache rate reads None, never 0.0",
          inf.cached_model_prices()["v/none"].cache_read is None,
          repr(inf.cached_model_prices()["v/none"]))

    # Cold first-party cache: _price_for("claude-opus-5-5") falls back to
    # "anthropic/claude-opus-5-5", and OpenRouter files it dotted.
    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "anthropic/claude-opus-5.5", "input_per_token": "0.000004",
         "output_per_token": "0.00002", "cache_read_per_token": "0.0000002"},
    ]}
    inf._DIRECT_MODELS_CACHE.clear()
    p = inf._price_for("claude-opus-5-5")
    check("a dashed Anthropic id prices from OpenRouter's dotted row on a cold direct cache",
          p is not None and p[0] == 4e-6 and p[1] == 2e-5 and p.cache_read == 2e-7, repr(p))
    check("the dotted OpenRouter key is still there as published",
          "anthropic/claude-opus-5.5" in inf.cached_model_prices())

    # ── PROMPT-LENGTH TIERS (Claude Haiku 5.5, 2026-10-07) ──────────
    # The shape OpenRouter published for anthropic/claude-haiku-5.5, read live
    # from openrouter.ai/api/v1/models on 2026-10-07. Anthropic's pricing page:
    # "a prompt of over 100,000 tokens pays higher prices", with a whole
    # second set of rates.
    haiku = {"prompt": "0.0000001", "completion": "0.0000005",
             "input_cache_read": "0.00000001", "input_cache_write": "0.000000125",
             "input_cache_write_1h": "0.0000002",
             "overrides": [{"min_prompt_tokens": 100000,
                            "prompt": "0.0000005", "completion": "0.0000025",
                            "input_cache_read": "0.00000005",
                            "input_cache_write": "0.000000625",
                            "input_cache_write_1h": "0.000001"}]}
    TIER = {"min_prompt_tokens": 100000,
            "input_per_token": 5e-7, "output_per_token": 2.5e-6,
            "cache_read_per_token": 5e-8, "cache_write_per_token": 6.25e-7}
    r = inf._dm_row({"id": "anthropic/claude-haiku-5.5", "pricing": haiku})
    check("an OpenRouter-shape row carries its prompt-length tier",
          r and r.get("price_tiers") == [TIER], repr(r))
    r = inf._dm_row({"id": "v/flat", "pricing": {"prompt": "0.000001",
                                                 "completion": "0.000002"}})
    check("a flat-priced row carries no tier key at all",
          r and "price_tiers" not in r, repr(r))
    r = inf._dm_row({"id": "v/bad-tiers", "pricing": {
        "prompt": "0.000001", "completion": "0.000002",
        "overrides": [{"prompt": "0.000005"},                       # no minimum
                      {"min_prompt_tokens": "lots", "prompt": "1"},  # unreadable
                      {"min_prompt_tokens": 0, "prompt": "0.000005"},
                      "not-a-dict"]}})
    check("an override with no usable minimum is dropped, never guessed",
          r and "price_tiers" not in r, repr(r))
    r = inf._dm_row({"id": "v/partial", "pricing": {
        "prompt": "0.000001", "completion": "0.000002",
        "input_cache_read": "0.0000001",
        "overrides": [{"min_prompt_tokens": 50000, "prompt": "0.000004"}]}})
    check("a tier keeps only the rates it publishes (absent stays absent)",
          r and r.get("price_tiers") == [{"min_prompt_tokens": 50000,
                                          "input_per_token": 4e-6}], repr(r))

    inf._CATALOG_CACHE["data"] = {"openrouter_models": [
        {"id": "anthropic/claude-haiku-5.5", "input_per_token": "0.0000001",
         "output_per_token": "0.0000005", "cache_read_per_token": 1e-8,
         "cache_write_per_token": 1.25e-7, "price_tiers": [TIER]},
        {"id": "v/partial", "input_per_token": 1e-6, "output_per_token": 2e-6,
         "cache_read_per_token": 1e-7,
         "price_tiers": [{"min_prompt_tokens": 50000, "input_per_token": 4e-6}]},
    ]}
    inf._DIRECT_MODELS_CACHE.clear()
    usd, priced = inf.estimate_call_cost_detail(
        "claude-haiku-5-5", 90000, 1000, cache_read_tokens=80000)
    want = 10000 * 1e-7 + 80000 * 1e-8 + 1000 * 5e-7
    check("a prompt under the tier prices at the base rates",
          usd is not None and abs(usd - want) < 1e-12, f"{usd} != {want}")
    check("and records no tier", "prompt_tier" not in priced, repr(priced))
    usd, priced = inf.estimate_call_cost_detail(
        "claude-haiku-5-5", 150000, 1000, cache_read_tokens=140000)
    want = 10000 * 5e-7 + 140000 * 5e-8 + 1000 * 2.5e-6
    check("a prompt over the tier prices EVERY token at the tier's rates",
          usd is not None and abs(usd - want) < 1e-12, f"{usd} != {want}")
    check("the tier that applied is recorded on the row",
          priced.get("prompt_tier") == 100000, repr(priced))
    # Cached tokens are part of the prompt: the anthropic wire's prompt total
    # is input + cache_read + cache_write (provider_gateway.py:1768-1770).
    usd, _ = inf.estimate_call_cost_detail(
        "claude-haiku-5-5", 100000, 0, cache_read_tokens=99000)
    want = 1000 * 5e-7 + 99000 * 5e-8
    check("a mostly-cached prompt at the minimum still pays the tier",
          usd is not None and abs(usd - want) < 1e-12, f"{usd} != {want}")
    usd, priced = inf.estimate_call_cost_detail(
        "v/partial", 60000, 1000, cache_read_tokens=10000)
    want = 50000 * 4e-6 + 10000 * 4e-6 + 1000 * 2e-6
    check("a tier with no cache rate prices cache at the TIER's input rate "
          "(errs high), and inherits the base output rate it does not restate",
          usd is not None and abs(usd - want) < 1e-12, f"{usd} != {want}")
    check("that fallback is labelled", priced.get("cache_read") == "input_rate_fallback",
          repr(priced))
    p = inf._price_for("claude-haiku-5-5")
    check("the dashed first-party id finds the dotted row with its tier",
          p is not None and p[0] == 1e-7 and p[1] == 5e-7 and len(p.tiers) == 1,
          repr(p))
    inf._CATALOG_CACHE["data"] = None

    print(("PASS" if not failures else f"FAIL ({len(failures)}): {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
