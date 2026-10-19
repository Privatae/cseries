#!/usr/bin/env python3
"""
Gate — de-lock the vendor-locked callers (2026-07-12: no vendor-locked code;
that is not what this platform is about).

Proves:
  - triage's grok LLM classifier is RIPPED (heuristic-only; no model/provider);
  - image generation routes through the user's OWN model (the routable
    'image_gen' role), with no xAI default and no platform billing; reading an
    image is the Host model's own job since 2026-10-12, so 'vision' is no
    longer a role at all;
  - the dead model constants are gone and no stale model ids remain;
  - the lane-era compile-time defaults are GONE from inference_config (per-role
    {provider, model} routing, 2026-08-04: unrouted = unconfigured,
    never a silent default).

The pinned-model ratchet (scripts/audit_pinned_models.py) enforces "no NEW pins";
this asserts the specific de-lock landed + that the config home is well-formed.

  python3 scripts/test_pin_removal.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_failures = 0


def check(label, cond, detail=""):
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


def _read(rel):
    return open(os.path.join(_ROOT, rel), encoding="utf-8").read()


# ── the config home reflects the de-lock ────────────────────────────────────
spec = importlib.util.spec_from_file_location(
    "mastercode.inference_config", os.path.join(_ROOT, "brain/mastercode/inference_config.py"))
cfg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfg)

check("LOCKED_MODEL_DEFAULTS reduced to just 'training' (triage ripped; image routable)",
      set(cfg.LOCKED_MODEL_DEFAULTS) == {"training"}, str(sorted(cfg.LOCKED_MODEL_DEFAULTS)))
check("image_gen is a BYO-endpoint routable role",
      "image_gen" in cfg._BYO_ALLOWED_ROLES, str(sorted(cfg._BYO_ALLOWED_ROLES)))
check("vision is not a role (2026-10-12: pictures go to the Host model)",
      "vision" not in cfg._BYO_ALLOWED_ROLES, str(sorted(cfg._BYO_ALLOWED_ROLES)))
# The lane-era compile-time defaults are GONE (per-role {provider, model}
# routing, 2026-08-04): an unrouted role surfaces 'unconfigured', never
# a silent default — so no DEFAULT_* / LANES name may survive in the config home.
for gone in ("DEFAULT_ROLE_MODEL", "DEFAULT_ROLE_MODELS", "LANES", "DEFAULT_LANE",
             "LANE_DEFAULT_MODEL", "DEFAULT_MODEL", "CLAUDE_SDK_MODELS",
             "CODE_HARNESS_DEFAULT_MODEL", "CODE_BENCH_MODELS",
             "BUILD_MODEL_MAP", "BUILD_MODEL_DEFAULT"):
    check(f"no compile-time default survives: {gone} deleted",
          not hasattr(cfg, gone))

infc = _read("brain/mastercode/inference_config.py")
check("resolver returns empty (no text fallback) for image_gen",
      'role in ("image", "image_gen")' in infc)
check("resolver never reads a stored vision route (it returns empty for it)",
      'if role == "vision":' in infc and "there is no vision route" in infc)


# ── triage: grok LLM classifier ripped, then the classifier itself ──────────
# The heuristic that replaced the Grok arm in 2026-07 turned out to have no
# caller either, and was deleted 2026-09-19 with _triage_message and
# _extract_chat_signals. The check that it "still calls the heuristic
# classifier" therefore no longer describes anything; what this file is really
# guarding — that no pinned Grok triage model came back — is the three checks
# above plus the absence check below.
triage = _read("brain/mastercode/runtime/triage.py")
check("triage: _classify_with_grok removed", "_classify_with_grok" not in triage)
check("triage: no weights.grok_client import", "weights.grok_client" not in triage)
check("triage: no _TRIAGE_MODEL / grok literal",
      "_TRIAGE_MODEL" not in triage and "grok-4-1-fast-non-reasoning" not in triage)
check("triage: the classifier stays deleted, not reinstated behind a model",
      "_triage_heuristic" not in triage and "async def _triage_message" not in triage)


# ── image bot: routes through the user's model, no xAI, no platform billing ──
img = _read("brain/mastercode/image_engine.py")
check("image bot: XAI_BASE / IMAGE_MODEL / VISION_MODEL literals gone",
      "XAI_BASE" not in img and "IMAGE_MODEL" not in img and "VISION_MODEL" not in img)
check("image bot: platform xAI _get_key removed", "def _get_key" not in img)
check("image bot: platform billing (wallet gate + debit) removed",
      "_debit_image" not in img and "_wallet_balance" not in img)
check("image bot: generate resolves the image_gen role",
      '_resolve_image_route(context, "image_gen")' in img)


# ── dead constants / stale ids ──────────────────────────────────────────────
cons = _read("brain/mastercode/memory_consolidation.py")
check("consolidation dead GROK_MODEL removed", "GROK_MODEL" not in cons)
# The claude_code bot (and its build-model map) was retired 2026-08-22 — the
# file is gone, so there is nothing left to pin here; the guard against a
# stray call is code_exec_guard.RETIRED_CODE_TOOLS.
check("claude_code bot retired (file gone)",
      not os.path.exists(os.path.join(_ROOT, "brain/mastercode/public_bots/000100010111_claude_code.py")))


print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("PASSED: vendor-lock removed — no pinned triage model, no triage classifier, image_gen routable")
