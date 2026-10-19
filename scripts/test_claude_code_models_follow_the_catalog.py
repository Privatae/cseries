#!/usr/bin/env python3
"""Claude Code's model list follows the catalog the Host already holds.

Found on one Host (2026-10-07, project 9423). The claude-code picker and the
save validator both read one fixed tuple, tool_result_wall.HOST_ENGINE_MODELS.
A new Claude model was missing from both until someone edited that tuple:
Claude Haiku 5.5 was missing for hours after its release, and the Host built
on Haiku 4.5 instead in the same turn.

What this pins:
  1. With the OpenRouter catalog cached, the claude-code list is the tuple
     plus the anthropic/claude-* rows the platform's model record
     (approved_models.json) lists, as dashed native ids: a Haiku 5.5 the
     tuple lacks, and a new model once the record gains it.
  1b. A catalog row the record does not list is left out, whatever its
     version: the live catalog on 2026-10-07 still served Opus 4.5/4.6/4.7
     and Sonnet 4.5, which the platform never assessed.
  2. Variants are not models: `:batch`, `:thinking`, `[1m]` and `-latest`
     rows are left out, and so is anything older than the tuple's oldest.
  3. A cold catalog lists the tuple, exactly.
  4. The picker (the coding matrix, `_models_for`) and the save validator
     read the same list, sync and async.

Container-free: the catalog cache is set by hand; no pod, no network.

Run: APP_ROOT=<repo> python3 scripts/test_claude_code_models_follow_the_catalog.py
"""
import asyncio
import logging
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
logging.disable(logging.CRITICAL)

from server.routes import inference as inf  # noqa: E402
from mastercode.runtime import tool_result_wall as trw  # noqa: E402
from mastercode.runtime import model_standard as ms  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# The tuple as it stood before Haiku 5.5 was added by hand.
TUPLE_BEFORE = tuple(m for m in trw.HOST_ENGINE_MODELS if m != "claude-haiku-5-5")

CATALOG = [
    {"id": "anthropic/claude-haiku-5.5"},
    {"id": "anthropic/claude-sonnet-6"},          # hypothetical: a model nobody listed
    {"id": "anthropic/claude-opus-5.5"},          # already in the tuple
    {"id": "anthropic/claude-haiku-4.5"},         # maps onto the tuple's dated id
    {"id": "anthropic/claude-3-haiku"},           # older than the tuple's oldest
    {"id": "anthropic/claude-3.7-sonnet"},
    {"id": "anthropic/claude-sonnet-4"},
    {"id": "anthropic/claude-opus-4.1"},
    {"id": "anthropic/claude-sonnet-6:batch"},    # variants
    {"id": "anthropic/claude-3.7-sonnet:thinking"},
    {"id": "anthropic/claude-sonnet-6[1m]"},
    {"id": "anthropic/claude-opus-latest"},
    {"id": "~anthropic/claude-fable-latest"},
    {"id": "openai/gpt-6-sol"},                   # another vendor
    # Served by OpenRouter on 2026-10-07 (openrouter.ai/api/v1/models) and
    # newer than the tuple's oldest, but absent from approved_models.json.
    {"id": "anthropic/claude-opus-4.7"},
    {"id": "anthropic/claude-opus-4.6"},
    {"id": "anthropic/claude-opus-4.5"},
    {"id": "anthropic/claude-sonnet-4.5"},
    {"id": "anthropic/claude-sonnet-5.5"},        # listed in the record
]

# The real record plus the hypothetical new model, as the record would carry
# it the day it is assessed (pending_card, like Haiku 5.5).
_real_load = ms.load


def _record_with_sonnet_6(force=False):
    doc = dict(_real_load(force))
    doc["models"] = list(doc.get("models") or []) + [
        {"canonical": "anthropic/claude-sonnet-6", "status": "pending_card"}]
    return doc


async def _no_status(pool, eid):
    return []


saved = (dict(inf._CATALOG_CACHE), trw.HOST_ENGINE_MODELS, inf._provider_status)
inf._provider_status = _no_status   # no key held: the matrix dials no provider
try:
    print("== 1b. a catalog row the model record does not list is not offered ==")
    inf._CATALOG_CACHE["data"] = {"openrouter_models": CATALOG}
    real = inf._harness_model_options().get("claude-code") or []
    for gone in ("claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
                 "claude-sonnet-4-5", "claude-sonnet-6"):
        check(f"{gone} (not in approved_models.json) is absent", gone not in real, real)
        check(f"...and a save of it is refused",
              inf.validate_harness_model("claude-code", gone) is not None)
    check("claude-sonnet-5-5 (listed in the record) is offered", "claude-sonnet-5-5" in real, real)

    ms.load = _record_with_sonnet_6
    print("== 1-2. a warm catalog adds new Claude models, never variants or old ones ==")
    trw.HOST_ENGINE_MODELS = TUPLE_BEFORE
    inf._CATALOG_CACHE["data"] = {"openrouter_models": CATALOG}
    opts = inf._harness_model_options().get("claude-code") or []
    check("Claude Haiku 5.5 is listed though the tuple lacks it", "claude-haiku-5-5" in opts, opts)
    check("a new model is listed as its dashed native id", "claude-sonnet-6" in opts, opts)
    check("every tuple model is still listed, first, in its order",
          opts[:len(TUPLE_BEFORE)] == list(TUPLE_BEFORE), opts)
    check("no id is listed twice", len(opts) == len(set(opts)), opts)
    check("the catalog's Haiku 4.5 is the tuple's dated id, not a second entry",
          "claude-haiku-4-5" not in opts and "claude-haiku-4-5-20251001" in opts, opts)
    for old in ("claude-3-haiku", "claude-3-7-sonnet", "claude-sonnet-4", "claude-opus-4-1"):
        check(f"{old} (older than the tuple's oldest) is absent", old not in opts, opts)
    check("no variant row is listed (:batch, :thinking, [1m], -latest)",
          not any(":" in m or "[" in m or "latest" in m for m in opts), opts)
    check("no other vendor's model is listed", not any("gpt" in m for m in opts), opts)

    check("the save validator accepts the new model",
          inf.validate_harness_model("claude-code", "claude-sonnet-6") is None,
          inf.validate_harness_model("claude-code", "claude-sonnet-6"))
    check("...and Haiku 5.5",
          inf.validate_harness_model("claude-code", "claude-haiku-5-5") is None,
          inf.validate_harness_model("claude-code", "claude-haiku-5-5"))
    check("...and refuses an old one",
          inf.validate_harness_model("claude-code", "claude-3-haiku") is not None)
    check("...and a variant",
          inf.validate_harness_model("claude-code", "claude-sonnet-6:batch") is not None)

    print("== 4. the picker and the validator are one list ==")
    m = run(inf._build_coding_matrix())
    row = [r for r in m.get("harnesses", []) if r.get("id") == "claude-code"]
    picked = [x.get("id") for x in (row[0].get("models") or [])] if row else None
    check("the coding matrix's claude-code models are the same list", picked == opts, picked)
    async_opts = run(inf.harness_model_options()).get("claude-code")
    check("harness_model_options (the save path's list) is the same list",
          async_opts == opts, async_opts)
    check("a save validated against the matrix accepts the new model",
          inf.validate_harness_model("claude-code", "claude-sonnet-6",
                                     options=run(inf.harness_model_options())) is None)

    print("== 5. a dated id and its alias are one model ==")
    trw.HOST_ENGINE_MODELS = saved[1]
    inf._CATALOG_CACHE["data"] = {"openrouter_models": CATALOG}
    opts = inf._harness_model_options().get("claude-code") or []
    matrix_opts = run(inf.harness_model_options())
    for spelling in ("claude-haiku-4-5", "claude-haiku-4.5", "claude-haiku-4-5-20251001",
                     "anthropic/claude-haiku-4-5"):
        check(f"a claude-code save of {spelling} passes",
              inf.validate_harness_model("claude-code", spelling) is None,
              inf.validate_harness_model("claude-code", spelling))
        check(f"...and against the matrix's list",
              inf.validate_harness_model("claude-code", spelling, options=matrix_opts) is None)
    for unlisted in ("claude-haiku-4-6", "claude-haiku-4-5-20990101", "claude-sonnet-4-5"):
        check(f"an unlisted id ({unlisted}) is still refused",
              inf.validate_harness_model("claude-code", unlisted) is not None)
    check("the picker shows one Haiku 4.5 row",
          [m for m in opts if m.startswith("claude-haiku-4")] == ["claude-haiku-4-5-20251001"], opts)
    from mastercode.inference_config import anthropic_api_model_id as _api
    for spelling in ("claude-haiku-4-5", "claude-haiku-4.5", "claude-haiku-4-5-20251001"):
        check(f"model_standard resolves {spelling} to anthropic/claude-haiku-4-5",
              ms.canonical_model("anthropic", spelling) == "anthropic/claude-haiku-4-5",
              ms.canonical_model("anthropic", spelling))
        check(f"...and the wire sends the pinned snapshot for {spelling}",
              _api(spelling) == "claude-haiku-4-5-20251001", _api(spelling))

    print("== 3. a cold catalog lists the tuple ==")
    trw.HOST_ENGINE_MODELS = saved[1]
    inf._CATALOG_CACHE["data"] = None
    cold = inf._harness_model_options().get("claude-code")
    check("the cold list is the tuple, exactly", cold == list(saved[1]), cold)
    inf._CATALOG_CACHE["data"] = {"openrouter_models": []}
    check("an empty catalog lists the tuple too",
          inf._harness_model_options().get("claude-code") == list(saved[1]))
    m = run(inf._build_coding_matrix())
    row = [r for r in m.get("harnesses", []) if r.get("id") == "claude-code"]
    check("the cold matrix lists the tuple",
          row and [x.get("id") for x in row[0].get("models") or []] == list(saved[1]), row)
finally:
    inf._CATALOG_CACHE.clear()
    inf._CATALOG_CACHE.update(saved[0])
    trw.HOST_ENGINE_MODELS = saved[1]
    inf._provider_status = saved[2]
    ms.load = _real_load

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
