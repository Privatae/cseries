#!/usr/bin/env python3
"""
Pinned-model ratchet — no hardcoded model or provider names (the catalog is
dynamic), and no pinned models: the orchestrator selects the model for every
caller.

A model id hardcoded in runtime code bypasses the orchestrator's routing table
(inference_config → behavioral_params.model_routing). This linter finds every
model-id literal in runtime .py OUTSIDE the sanctioned config home, and — like
the orphan-table linter — FAILS on any pin not in the baseline. Freeze the
current set, then shrink the baseline as each caller is wired to
resolve_role_model(). New pins fail on day one.

Static, no DB.
  python3 scripts/audit_pinned_models.py            # gate vs baseline
  python3 scripts/audit_pinned_models.py --list     # print current pins
  python3 scripts/audit_pinned_models.py --update-baseline
"""
from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASELINE = os.path.join(_ROOT, "scripts", "pinned_models_baseline.txt")

_CODE_DIRS = ("server", "brain", "cli", "system-apps", "sidecars")

# The ONLY places a model id may legitimately appear as a literal: the routing
# config's alias map + feature-locked batch default, and the dormant engine.
_ALLOWED_FILES = {
    "brain/mastercode/inference_config.py",          # _ANTHROPIC_API_ID (alias normalization) + LOCKED_MODEL_DEFAULTS (training batch, Anthropic-only API)
    "brain/mastercode/runtime/tool_result_wall.py",  # HOST_ENGINE_MODELS — the claude-code HARNESS's model list for the coding matrix (moved from the deleted host_sdk_engine.py); a picker catalog, not a route
}

# Model-id shapes to catch (inside quotes). Embedding models are a separate
# sovereign lane (behavioral_params.embedding_model), not a text-routing pin —
# excluded via _EMBED_RE below.
_MODEL_RE = re.compile(
    r"""['"]("""
    r"""(?:anthropic/|openai/|openrouter/|x-ai/|moonshotai/|qwen/|deepseek/|google/)?"""
    # NOTE: every family class must include '-' so multi-hyphen ids like
    # grok-4-1-fast-non-reasoning / gpt-4o-mini are matched in full (the quotes
    # anchor the match, so a class that stops at the first hyphen never matches).
    r"""(?:claude-(?:opus|sonnet|haiku|fable|instant)[-\w.]*"""
    r"""|grok[-\w.]+"""
    r"""|gpt[-\w.]+|o[13]-[-\w.]+"""
    r"""|gemini[-\w.]+"""
    r"""|deepseek[-/\w.]+"""
    r"""|kimi[-\w.]+|moonshot[-\w.]*"""
    r"""|mistral[-\w.]*|mixtral[-\w.]*"""
    r"""|llama[-\w.]*"""
    r"""|qwen[-/\w.]*"""
    r""")"""
    r""")['"]"""
)
# Embedding + code-index models — sovereign local lane, not a text-routing pin.
_EMBED_RE = re.compile(r"embedding|embed|qwen3-embedding|nomic|bge-|coder", re.I)


def _iter_py():
    for d in _CODE_DIRS:
        root = os.path.join(_ROOT, d)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


def find_pins() -> set[str]:
    """Set of 'relpath::model' pairs — every model-id literal in runtime code
    outside the sanctioned config files."""
    pins: set[str] = set()
    for path in _iter_py():
        rel = os.path.relpath(path, _ROOT)
        if rel in _ALLOWED_FILES:
            continue
        if os.path.basename(path).startswith("test_"):
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _MODEL_RE.finditer(text):
            model = m.group(1)
            # skip embedding-lane model ids (sovereign, separately configured)
            if _EMBED_RE.search(model):
                continue
            # Not a model id: identifiers use '_' (grok_binary_path); real ids use
            # '-'. And a real id carries a version digit or an org '/' prefix — a
            # bare family label (claude-sonnet, qwen) is a dropdown key, not a pin.
            if "_" in model:
                continue
            if "/" not in model and not any(c.isdigit() for c in model):
                continue
            pins.add(f"{rel}::{model}")
    return pins


def load_baseline() -> set[str]:
    if not os.path.exists(_BASELINE):
        return set()
    out: set[str] = set()
    for line in open(_BASELINE, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    pins = find_pins()

    if arg == "--list":
        for p in sorted(pins):
            print(p)
        print(f"\n{len(pins)} pinned model literal(s) in runtime code outside inference_config.")
        return 0

    if arg == "--update-baseline":
        with open(_BASELINE, "w", encoding="utf-8") as fh:
            fh.write("# Pinned model literals in runtime code (file::model), outside the\n")
            fh.write("# sanctioned config (inference_config.py / tool_result_wall.py).\n")
            fh.write("# Each is a caller still to be wired to\n")
            fh.write("# resolve_role_model(). SHRINK this list as pins are removed; the gate\n")
            fh.write("# FAILS on any pin NOT listed here (a new hardcoded model).\n")
            for p in sorted(pins):
                fh.write(p + "\n")
        print(f"baseline written: {len(pins)} pin(s) -> {_BASELINE}")
        return 0

    # _iter_py skips a code dir that is not there, so a removed or renamed one
    # would shrink what this reads while it still said OK.
    missing = [d for d in _CODE_DIRS if not os.path.isdir(os.path.join(_ROOT, d))]
    if missing:
        print("FAIL  code dir(s) this audit reads are not in the tree: " + ", ".join(missing)
              + " — point _CODE_DIRS at where the runtime code lives now")
        return 1

    baseline = load_baseline()
    new_pins = pins - baseline
    removed = baseline - pins
    if removed:
        print(f"note: {len(removed)} baseline pin(s) removed (trim the baseline): "
              + ", ".join(sorted(removed)))
    if new_pins:
        print("FAIL  new hardcoded model literal(s) — route via inference_config instead:")
        for p in sorted(new_pins):
            print(f"        - {p}")
        print("      A caller's model must come from resolve_role_model(); if this is a "
              "sanctioned exception add it to scripts/pinned_models_baseline.txt with a reason.")
        return 1
    print(f"OK    no new pinned models ({len(pins)} known, baseline current)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
