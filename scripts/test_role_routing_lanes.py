#!/usr/bin/env python3
"""
Role-routing ratchet — the per-role {provider, model} invariant.

What this pins (orchestrator v2 routing cut, 2026-08-04): there are no
"lanes" and no compile-time default model anywhere. A role's route is a
{provider, model} pair (+ optional fallback pair) where the provider is a
Keys-registry id or 'local'. The Claude Agent SDK is NOT a production option:
'claude-sdk'/'subscription' stored anywhere normalize to EMPTY and surface as
unconfigured (the hard cut), never re-routed to another provider.

The bug class this exists to prevent: a silent re-route. Pre-cut, a role with
no explicit route fell through to a compile-time default (DEFAULT_LANE /
DEFAULT_ROLE_MODEL); post-cut it must resolve EMPTY with an actionable reason
("pick a provider and model in the Orchestrator").

Gates:
  1. normalize_provider alias table: openrtr/self → openrouter; privatae →
     anthropic for Claude models, openrouter otherwise; claude-sdk and
     subscription → ("", "") (the hard cut); a Claude-shaped pick on
     openrouter coerces to provider 'anthropic' with the Anthropic-native id.
  2. provider_allowed_for_role: every registry provider + local serve any
     text role; the SDK aliases serve nothing.
  3. resolve_role_route: a stored new-shape route resolves verbatim (incl.
     the fallback pair); legacy {lane, direct_provider} migrates; a legacy
     claude-sdk route drops to unconfigured; an unrouted role resolves EMPTY
     with a reason naming the role; an image role NEVER inherits the legacy
     text config.
  4. No compile-time defaults survive in inference_config.

Runs inside the image (python3 /app/scripts/test_role_routing_lanes.py) and
repo-side (APP_ROOT=<repo> python3 scripts/test_role_routing_lanes.py).
"""

import asyncio
import json
import logging
import os
import sys

logging.disable(logging.CRITICAL)

# Path bootstrap: in-image /app, else the repo root relative to this file.
_ROOT = os.environ.get("APP_ROOT") or (
    "/app" if os.path.exists("/app/brain/mastercode/inference_config.py")
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "brain"))

from mastercode.inference_config import (            # noqa: E402
    normalize_provider, provider_allowed_for_role, resolve_role_route,
)
import mastercode.inference_config as ic             # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -- " + str(detail)) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


class FakePool:
    """Minimal asyncpg-shaped stub: resolve_role_route only reads
    entity_registry.behavioral_params for the entity."""

    def __init__(self, behavioral_params: dict):
        self._bp = behavioral_params

    async def fetchrow(self, _q, *_a):
        return {"behavioral_params": json.dumps(self._bp)}

    async def fetch(self, _q, *_a):
        return []

    async def execute(self, _q, *_a):
        return None


# Placeholder ids only — never a real model literal. The claude-shaped ones
# reference the alias-map ids inference_config itself defines (the map is the
# sanctioned home; testing ITS normalization is the point).
async def main():
    # ── 1. normalize_provider: the ONE place legacy values die ───────────────
    check("normalize: openrtr → openrouter",
          normalize_provider("openrtr", "org/model-a") == ("openrouter", "org/model-a"))
    check("normalize: self → openrouter",
          normalize_provider("self", "org/model-a") == ("openrouter", "org/model-a"))
    check("normalize: privatae + claude model → anthropic",
          normalize_provider("privatae", "claude-sonnet-5")
          == ("anthropic", "claude-sonnet-5"))
    # The privatae→anthropic path keeps the picked slug verbatim; the id
    # normalizes at dial time (provider_gateway._call_anthropic →
    # anthropic_api_model_id). Only the openrouter coercion normalizes inline.
    check("normalize: privatae + claude slug → anthropic (slug verbatim)",
          normalize_provider("privatae", "anthropic/claude-sonnet-5")
          == ("anthropic", "anthropic/claude-sonnet-5"))
    check("normalize: privatae + non-claude model → openrouter",
          normalize_provider("privatae", "org/model-a") == ("openrouter", "org/model-a"))
    check("normalize: claude-sdk is a hard cut (empty, unroutable)",
          normalize_provider("claude-sdk", "claude-sonnet-5") == ("", ""))
    check("normalize: subscription is a hard cut (empty, unroutable)",
          normalize_provider("subscription", "claude-sonnet-5") == ("", ""))
    check("normalize: claude-on-openrouter coerces to anthropic",
          normalize_provider("openrouter", "anthropic/claude-opus-4.8")
          == ("anthropic", "claude-opus-4-8"))
    check("normalize: OpenRouter's dotted opus 5.5 slug → the Anthropic API id",
          normalize_provider("openrouter", "anthropic/claude-opus-5.5")
          == ("anthropic", "claude-opus-5-5"))
    check("normalize: any dotted Claude slug gets the dashed API id (fable 5.1)",
          normalize_provider("openrouter", "anthropic/claude-fable-5.1")
          == ("anthropic", "claude-fable-5-1"))
    check("normalize: OpenRouter's ~anthropic/claude-fable-latest → a real API id",
          normalize_provider("openrouter", "~anthropic/claude-fable-latest")
          == ("anthropic", "claude-fable-5-1"))
    check("normalize: blank provider stays empty",
          normalize_provider("", "org/model-a") == ("", ""))

    # ── 2. provider_allowed_for_role ─────────────────────────────────────────
    for prov in ("openrouter", "local", "anthropic", "kimi", "openrtr"):
        check(f"guard: {prov} allowed for chat",
              provider_allowed_for_role(prov, "chat"))
    for prov in ("subscription", "claude-sdk"):
        check(f"guard: {prov} refused for chat",
              not provider_allowed_for_role(prov, "chat"))

    # ── 3a. Stored new-shape route resolves verbatim (incl. fallback) ────────
    pool = FakePool({"model_routing": {"chat": {
        "provider": "xai", "model": "model-a",
        "fallback_provider": "openrouter", "fallback_model": "org/model-b"}}})
    r = await resolve_role_route(pool, 1, "chat")
    check("route: stored new-shape resolves verbatim",
          r["provider"] == "xai" and r["model"] == "model-a"
          and r["fallback_provider"] == "openrouter"
          and r["fallback_model"] == "org/model-b"
          and not r.get("unconfigured"), r)

    # ── 3b. Legacy {lane, direct_provider} migrates to the direct provider ───
    pool = FakePool({"model_routing": {"chat": {
        "lane": "openrtr", "model": "model-a", "direct_provider": "xai"}}})
    r = await resolve_role_route(pool, 1, "chat")
    check("route: legacy lane+direct_provider migrates to provider",
          r["provider"] == "xai" and r["model"] == "model-a"
          and not r.get("unconfigured"), r)

    # ── 3c. Legacy bare openrtr lane normalizes to openrouter ────────────────
    pool = FakePool({"model_routing": {"chat": {
        "lane": "openrtr", "model": "org/model-a"}}})
    r = await resolve_role_route(pool, 1, "chat")
    check("route: legacy bare openrtr lane → openrouter",
          r["provider"] == "openrouter" and r["model"] == "org/model-a", r)

    # ── 3d. Legacy claude-sdk route drops to UNCONFIGURED (the hard cut) ─────
    pool = FakePool({"model_routing": {"chat": {
        "lane": "claude-sdk", "model": "claude-sonnet-5"}}})
    r = await resolve_role_route(pool, 1, "chat")
    check("route: stored claude-sdk route is unconfigured, never re-routed",
          not r["provider"] and bool(r.get("unconfigured")), r)

    # ── 3e. Unrouted role resolves EMPTY with an actionable reason ───────────
    r = await resolve_role_route(FakePool({}), 1, "consolidation")
    check("route: unrouted role is empty + unconfigured",
          not r["provider"] and not r["model"] and bool(r.get("unconfigured")), r)
    reason = (r.get("unconfigured") or "").lower()
    check("route: the reason names the role and the Orchestrator",
          "consolidation" in reason and "orchestrator" in reason, reason)

    # ── 3f. An image role NEVER inherits the legacy text config ──────────────
    pool = FakePool({"inference_provider": "openrouter",
                     "inference_model": "org/legacy-text-model"})
    r = await resolve_role_route(pool, 1, "image_gen")
    check("route: image role never falls back to the text model",
          not r["provider"] and r["model"] != "org/legacy-text-model"
          and bool(r.get("unconfigured")), r)

    # ── 4. No compile-time defaults survive ──────────────────────────────────
    for gone in ("LANES", "DEFAULT_LANE", "LANE_DEFAULT_MODEL",
                 "DEFAULT_ROLE_MODEL", "DEFAULT_ROLE_MODELS", "DEFAULT_MODEL",
                 "CLAUDE_SDK_MODELS", "CODE_HARNESS_DEFAULT_MODEL",
                 "CODE_BENCH_MODELS", "BUILD_MODEL_MAP", "BUILD_MODEL_DEFAULT"):
        check(f"config: {gone} is deleted", not hasattr(ic, gone))

    print()
    if FAILS:
        print("FAILED: role-routing ratchet (%d)" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("PASS: role-routing ratchet — providers+local routable, the SDK lane "
          "is a hard cut, unrouted roles surface unconfigured")
    return 0


sys.exit(asyncio.run(main()))
