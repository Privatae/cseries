#!/usr/bin/env python3
"""Gate: harness canonical-key fallback.

`harness-<id>` always wins. When it is ABSENT, a harness whose creds file can
actually run on a generic API key falls back to the user's canonical provider
credential (server/vault/providers.resolve). The invariants this pins:

  - vendor CLIs (codex/cursor/grok) NEVER fall back — their creds are
    vendor-account artifacts and a synthesized key would only fail at run time
  - opencode falls back to a BARE openrouter key (its file's shape) and to
    nothing else
  - kimi (first-party) runs on its own provider's key only — never a
    generic key, never anthropic
  - an explicit harness-<id> entry beats any canonical key

Vault + pool are stubs — runs with no live Postgres.
Run: APP_ROOT=<repo> python3 scripts/test_harness_keys_fallback.py
"""
import asyncio
import json
import os
import sys

failures = []


def check(label, ok, detail=""):
    print(f"[harness-fallback] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(coro):
    return asyncio.run(coro)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    for p in (app_root, os.path.join(app_root, "brain")):
        if p not in sys.path:
            sys.path.insert(0, p)

    from server.network import harness_keys as hk
    from server.vault import providers as pv

    # ── the fallback rule itself is the security surface ─────────────
    # The model: FIRST-PARTY providers sell their own models
    # and belong with their harness; GENERIC providers sell everyone's models
    # and serve only the harness with no vendor of its own (opencode).
    # A first-party harness must never silently borrow a generic key.
    for vendor in ("codex", "cursor", "grok"):
        check(f"vendor CLI {vendor} has NO provider fallback",
              hk._fallback_providers(vendor) == ())
    check("registry: kind flag on every provider, generics are exactly the five",
          set(pv.generic_providers())
          == {"openrouter", "deepinfra", "together", "fireworks", "groq"}
          and all(s.get("kind") in ("first_party", "generic")
                  for s in pv.PROVIDERS.values()))
    check("opencode falls back to openrouter only (wrapper limitation)",
          hk._fallback_providers("opencode") == ("openrouter",))

    # ── synthesis shapes ─────────────────────────────────────────────
    raw = hk._synthesize_creds("opencode", "openrouter", "sk-or-abc")
    check("opencode synthesis is the bare key", raw == b"sk-or-abc")

    check("opencode synthesis from a non-openrouter provider is refused "
          "(a generic key other than openrouter, or any first-party key)",
          hk._synthesize_creds("opencode", "groq", "gsk-key") is None
          and hk._synthesize_creds("opencode", "xai", "xai-key") is None
          and hk._synthesize_creds("opencode", "kimi", "sk-moonshot") is None)
    check("opencode synthesis from anthropic is refused",
          hk._synthesize_creds("opencode", "anthropic", "sk-ant") is None)
    check("empty secret never synthesizes",
          hk._synthesize_creds("opencode", "openrouter", "") is None)

    # ── kimi: FIRST-PARTY — its own provider's key, and NOTHING else ──
    # The regression that rewrote this block: with no Kimi key stored, the old
    # fallback walked on to openrouter and MANUFACTURED a kimi credential out
    # of the user's OpenRouter key — the Keys chip lit for
    # a provider the user does not have, and Kimi Code's runs paid a reseller's fee.
    # A first-party harness with no first-party key is NOT CONNECTED, full stop.
    check("kimi fallback is its own provider ONLY",
          hk._fallback_providers("kimi") == ("kimi",))
    raw = hk._synthesize_creds("kimi", "kimi", "sk-moonshot")
    check("kimi synthesis from its own provider key is the bare key "
          "(the runtime wraps it to Moonshot)", raw == b"sk-moonshot")
    check("kimi synthesis from a generic provider is REFUSED "
          "(the manufactured-credential regression)",
          hk._synthesize_creds("kimi", "openrouter", "sk-or-abc") is None
          and hk._synthesize_creds("kimi", "groq", "gsk-key") is None)
    check("kimi synthesis from anthropic is refused",
          hk._synthesize_creds("kimi", "anthropic", "sk-ant") is None)

    # ── the ladder: canonical resolve feeds the fallback ─────────────
    class StubRC:
        def __init__(self, secret):
            self.secret = secret

    async def fake_resolve(pool, eid, provider, accepted_types=("api_key",),
                           wiring=None, *, actor=None, env_fallback=True):
        return StubRC("sk-or-live") if provider == "openrouter" else None

    orig_resolve = pv.resolve
    pv.resolve = fake_resolve
    try:
        raw, canon = run(hk._fallback_bytes(object(), 3, "opencode"))
        check("opencode fallback resolves the canonical openrouter key",
              raw == b"sk-or-live" and canon == "openrouter")

        raw, canon = run(hk._fallback_bytes(object(), 3, "grok"))
        check("vendor CLI gets no bytes from the fallback path",
              raw is None and canon == "")

        # one Host's exact live scenario: an OpenRouter key and NO Kimi key.
        # Kimi Code must come back not-connected — not running on a borrowed
        # generic key.
        raw, canon = run(hk._fallback_bytes(object(), 3, "kimi"))
        check("kimi with only an openrouter key stored gets NOTHING "
              "(not connected beats silently borrowed)",
              raw is None and canon == "")

        async def raising_resolve(*a, **k):
            raise RuntimeError("vault down")

        pv.resolve = raising_resolve
        raw, canon = run(hk._fallback_bytes(object(), 3, "opencode"))
        check("a raising resolver degrades to no-fallback, never raises",
              raw is None and canon == "")
    finally:
        pv.resolve = orig_resolve

    print(f"[harness-fallback] "
          f"{'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
