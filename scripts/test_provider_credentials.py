#!/usr/bin/env python3
"""Gate: typed canonical provider credentials.

Pins the resolver's pure logic (registry shape, canonical names, TYPE-aware
fuzzy matching, endpoint construction) and the route-level carriage contract
(the per-role {provider, model, fallback_provider?, fallback_model?} shape —
no direct_provider; lane/model_id are transitional aliases only). Vault/DB
paths are exercised with a stub pool — this gate runs in the smoke container
with no live Postgres dependency.

Run: APP_ROOT=<repo> python3 scripts/test_provider_credentials.py
"""
import asyncio
import os
import sys

failures = []


def check(label, ok, detail=""):
    print(f"[provider-creds] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)

    from server.vault import providers as pv

    # ── registry shape ──────────────────────────────────────────────
    # google rides Google's own OpenAI-compatibility base, so it needs no new wire.
    check("registry: the canonical providers (core four + kimi + google + OSS-direct rows)",
          set(pv.PROVIDERS) == {"openrouter", "openai", "xai", "kimi", "anthropic",
                                "google", "deepinfra", "together", "fireworks",
                                "groq"})
    check("registry: google rides the openai wire on Google's compat base",
          pv.PROVIDERS["google"]["wire"] == "openai"
          and pv.PROVIDERS["google"]["kind"] == "first_party"
          and pv.chat_completions_url("google")
          == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
          f'google={pv.PROVIDERS["google"]}')
    check("registry: anthropic IS a direct provider (the SDK lane is cut)",
          "anthropic" in pv.DIRECT_PROVIDERS
          and set(pv.DIRECT_PROVIDERS) == set(pv.PROVIDERS))
    check("registry: every provider has base_url + wire + env_key",
          all({"base_url", "wire", "env_key"} <= set(s)
              for s in pv.PROVIDERS.values()))

    # ── canonical names ─────────────────────────────────────────────
    check("canonical_name: api_key = bare provider",
          pv.canonical_name("anthropic") == "anthropic"
          and pv.canonical_name("XAI") == "xai")
    check("canonical_name: oauth = provider-oauth",
          pv.canonical_name("anthropic", "oauth_token") == "anthropic-oauth"
          and pv.canonical_name("openai", "oauth_token") == "openai-oauth")

    # ── typed fuzzy matching ────────────────────────────────────────
    check("fuzzy: free-typed api-key names match",
          pv.matches_provider("openrouter", "api_key", "Open Router")
          and pv.matches_provider("xai", "api_key", "xAI key")
          and pv.matches_provider("openai", "api_key", "my openai api key"))
    check("fuzzy: api_key lookup NEVER swallows an oauth entry",
          not pv.matches_provider("anthropic", "api_key", "anthropic-oauth")
          and not pv.matches_provider("anthropic", "api_key", "Anthropic OAuth"))
    check("fuzzy: oauth lookup NEVER swallows an api-key entry",
          not pv.matches_provider("anthropic", "oauth_token", "anthropic")
          and pv.matches_provider("anthropic", "oauth_token", "anthropic-oauth"))
    check("fuzzy: wrong provider never matches",
          not pv.matches_provider("openai", "api_key", "xai key"))

    # ── stored-type gate: the ONE rule the resolver and the Keys UI share ──
    # A row whose stored cred_type names an OWNER rather than a shape (the
    # harness_* / *_oauth / database_url family written server-side) must not
    # serve a typed slot. Both _resolve_typed's fuzzy rung and the Keys
    # coverage read call this, so "the app says connected, routing says no
    # key" cannot come back.
    check("stored_type: exact shape match serves",
          pv.stored_type_serves("api_key", "api_key")
          and pv.stored_type_serves("oauth_token", "oauth_token"))
    check("stored_type: crossed shapes never serve",
          not pv.stored_type_serves("oauth_token", "api_key")
          and not pv.stored_type_serves("api_key", "oauth_token"))
    check("stored_type: legacy rows (no type) fall back to the name marker",
          pv.stored_type_serves(None, "api_key")
          and pv.stored_type_serves("", "api_key")
          and pv.stored_type_serves("   ", "oauth_token"))
    check("stored_type: OWNER-shaped types never serve a slot",
          not pv.stored_type_serves("harness_kimi", "api_key")
          and not pv.stored_type_serves("harness_opencode", "api_key")
          and not pv.stored_type_serves("claude_oauth", "oauth_token")
          and not pv.stored_type_serves("gitea_oauth", "oauth_token")
          and not pv.stored_type_serves("database_url", "api_key"))

    # Regression: 'harness:kimi:provider' (cred_type
    # 'harness_kimi') fuzzy-matches the kimi api_key slot BY NAME, so the Keys
    # chip lit while resolve() skipped the row on type — the Orchestrator
    # reported no kimi key for a key the app said was stored. The name still
    # matches; the stored type is what must refuse it.
    check("regression: harness:kimi:provider name-matches but must NOT serve",
          pv.matches_provider("kimi", "api_key", "harness:kimi:provider")
          and not pv.stored_type_serves("harness_kimi", "api_key"))

    # The coverage read's exact-canonical arm is deliberately UNtyped, because
    # _resolve_typed opens an exact canonical name without checking the type.
    # Pinned so the two arms are never "fixed" into disagreement.
    check("stored_type: governs the FUZZY arm only (exact stays untyped)",
          "kimi" == pv.canonical_name("kimi", "api_key"))

    # ── endpoints ───────────────────────────────────────────────────
    check("chat_completions_url: OpenAI-wire providers only",
          (pv.chat_completions_url("xai") or "").endswith("/chat/completions")
          and (pv.chat_completions_url("openai") or "").startswith("https://api.openai.com")
          and pv.chat_completions_url("anthropic") is None
          and pv.chat_completions_url("nonsense") is None)

    # ── resolve_credential: env fallback + unknown provider ─────────
    os.environ["XAI_API_KEY"] = "env-xai-1"
    got = asyncio.run(pv.resolve_credential(None, 0, "xai", "api_key"))
    check("resolve: env fallback for api_key with no pool", got == "env-xai-1")
    got = asyncio.run(pv.resolve_credential(
        None, 0, "xai", "oauth_token"))
    check("resolve: oauth NEVER falls back to env", got is None)
    got = asyncio.run(pv.resolve_credential(None, 0, "not-a-provider"))
    check("resolve: unknown provider → None", got is None)
    os.environ.pop("XAI_API_KEY", None)

    # ── route carriage (inference_config) ───────────────────────────
    # (placeholder ids only — never a real model literal)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    from mastercode import inference_config as ic
    r = ic._route("xai", "model-a")
    check("route: new shape carries provider/model (+ transitional aliases)",
          r["provider"] == "xai" and r["model"] == "model-a"
          and r["lane"] == "xai" and r["model_id"] == "model-a")
    check("route: no direct_provider remnant (the lanes are gone)",
          "direct_provider" not in r)
    check("route: fallback pair defaults empty",
          r["fallback_provider"] is None and r["fallback_model"] is None)
    r = ic._route("openrouter", "org/model-b",
                  fallback_provider="xai", fallback_model="model-c")
    check("route: fallback pair carried verbatim",
          r["fallback_provider"] == "xai" and r["fallback_model"] == "model-c")
    check("route: empty/display routes keep the same shape (parity)",
          {"provider", "model", "lane", "model_id", "fallback_provider",
           "fallback_model"} <= set(ic._empty_route("x"))
          and {"provider", "model", "lane", "model_id", "fallback_provider",
               "fallback_model"} <= set(ic._display_route("local", "m")))

    # ── wrapper signature parity (once a chat crasher) ──────────────
    # session.py forwards route fields into the _call_entity_model wrapper;
    # the wrapper dropping one is a repeat crash class (base_url, then
    # direct_provider). AST check: every kwarg the session turn passes must
    # be a parameter the wrapper accepts — catches the NEXT field too.
    import ast
    from turn_source import turn_source  # session.py and the modules split out of it
    sess_tree = ast.parse(turn_source(app_root))
    with open(os.path.join(app_root, "brain/mastercode/runtime/tools.py")) as f:
        tools_tree = ast.parse(f.read())
    wrapper_params: set = set()
    for node in ast.walk(tools_tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == "_call_entity_model":
            a = node.args
            wrapper_params = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
            break
    forwarded: set = set()
    for node in ast.walk(sess_tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if called == "_call_entity_model":
                forwarded |= {k.arg for k in node.keywords if k.arg}
    check("wrapper parity: session.py kwargs ⊆ _call_entity_model signature",
          bool(wrapper_params) and forwarded <= wrapper_params,
          f"wrapper missing: {sorted(forwarded - wrapper_params)}")

    print(f"[provider-creds] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
