#!/usr/bin/env python3
"""The model catalog tools return a PAGE, never the whole catalog.

WHAT THIS PROTECTS.
model_route op='catalog' and inference_catalog returned inference.get_model_
catalog() whole — ~400 rows with 240-char descriptions and modality lists,
~184KB as a tool result. The transcript wall (runtime/tool_result_wall.py)
caps a result at 100KB and tells the model to "use the tool's limit/offset
params", but neither tool had any: the caller got a cliff and no way around
it. Both tools now go through runtime/tools.py::_compact_catalog (defined
beside _CATALOG_DEFAULT_LIMIT / _CATALOG_MAX_LIMIT, runtime/tools.py:4111-
4115): filters (search / provider / capability), a page (limit default 25,
max 200; offset), compact rows, and an honest envelope (total_available /
total_matched / next_offset).

WHAT THIS DOES NOT CATCH. _compact_catalog is pure and is exercised here on a
synthetic 400-row catalog; that the two executors actually route through it is
a source-text check. A refactor that keeps the string but bypasses the helper
would pass. The durable protection is the comment at the helper.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_model_catalog_pagination.py
"""
import json
import os
import sys

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


def _fake_catalog(n: int = 400) -> dict:
    rows = []
    for i in range(n):
        vendor = ("anthropic", "openai", "qwen", "google")[i % 4]
        rows.append({
            "id": f"{vendor}/model-{i}",
            "name": f"{vendor.title()} Model {i}",
            "provider": "openrouter",
            "available": True,
            "context_length": 128000 + i,
            "input_per_token": "0.000001",
            "output_per_token": "0.000005",
            "description": "x" * 240,
            "input_modalities": ["text", "image"] if i % 5 == 0 else ["text"],
            "output_modalities": ["text", "image"] if i % 50 == 0 else ["text"],
        })
    local = [{"id": "qwen3:8b", "name": "qwen3:8b", "provider": "local",
              "available": True, "bastion": "home-gpu"}]
    return {"openrouter_models": rows, "local_models": local,
            "total_openrouter": n, "degraded": False, "roles": ["chat", "embedding", "image", "image_gen"]}


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("model catalog — a page, never the whole thing")

    from mastercode.runtime import tools as t  # noqa: E402
    from mastercode.runtime.tool_result_wall import _RESULT_MAX_BYTES  # noqa: E402

    pager = getattr(t, "_compact_catalog")
    default_limit = getattr(t, "_CATALOG_DEFAULT_LIMIT")
    max_limit = getattr(t, "_CATALOG_MAX_LIMIT")

    cat = _fake_catalog()
    raw_bytes = len(json.dumps(cat, default=str))
    check("the synthetic catalog reproduces the pathology (raw > the wall)",
          raw_bytes > _RESULT_MAX_BYTES, f"raw={raw_bytes} wall={_RESULT_MAX_BYTES}")

    # ── default page ──────────────────────────────────────────────────────
    out = pager(cat, {})
    body = json.dumps(out, default=str)
    check("default page is 25 rows", len(out["models"]) == 25, str(len(out["models"])))
    check("default page is far under the transcript wall (< 20KB)",
          len(body) < 20_000, f"{len(body)} bytes")
    check("envelope reports total_available = every row",
          out["total_available"] == 401, str(out["total_available"]))
    check("envelope reports total_matched = every row when unfiltered",
          out["total_matched"] == 401, str(out["total_matched"]))
    check("next_offset points at the next page", out["next_offset"] == 25, str(out["next_offset"]))
    check("rows are compact (no description, no modality lists)",
          all("description" not in r and "input_modalities" not in r for r in out["models"]))
    check("rows carry the pricing + context a pick needs",
          all(r.get("context_length") and r.get("input_per_token") for r in out["models"]))
    check("roles pass through", out.get("roles") == ["chat", "embedding", "image", "image_gen"])
    check("note tells the model how to narrow and page",
          "search=" in out["note"] and "next_offset" in out["note"])

    # ── paging ───────────────────────────────────────────────────────────
    last = pager(cat, {"offset": 400, "limit": 25})
    check("last page has the remaining row and no next_offset",
          len(last["models"]) == 1 and last["next_offset"] is None,
          f"{len(last['models'])} rows, next={last['next_offset']}")
    big = pager(cat, {"limit": 100000})
    check("limit is capped at the max", big["limit"] == max_limit
          and len(big["models"]) == max_limit)
    bad = pager(cat, {"limit": "lots", "offset": "-3"})
    check("garbage limit/offset fall back to defaults",
          bad["limit"] == default_limit and bad["offset"] == 0)

    # ── filters ──────────────────────────────────────────────────────────
    s = pager(cat, {"search": "ANTHROPIC"})
    check("search is a case-insensitive substring over id/name",
          s["total_matched"] == 100 and all("anthropic" in r["id"] for r in s["models"]),
          str(s["total_matched"]))
    p = pager(cat, {"provider": "local"})
    check("provider filter isolates local rows and keeps the bastion",
          p["total_matched"] == 1 and p["models"][0].get("bastion") == "home-gpu")
    v = pager(cat, {"capability": "vision"})
    check("capability=vision keeps only image-input rows",
          v["total_matched"] == 80 and all(r["vision"] for r in v["models"]),
          str(v["total_matched"]))
    g = pager(cat, {"capability": "image_gen"})
    check("capability=image_gen keeps only image-output rows",
          g["total_matched"] == 8 and all(r["image_gen"] for r in g["models"]),
          str(g["total_matched"]))
    empty = pager({}, {})
    check("an empty catalog is an honest empty page",
          empty["models"] == [] and empty["total_available"] == 0
          and empty["next_offset"] is None)
    deg = pager({**cat, "degraded": True}, {})
    check("degraded flag survives", deg["degraded"] is True)

    # An unknown capability must not silently match every
    # row while the envelope claims a filter ran; unknown modalities are not
    # "unable".
    unk = pager(cat, {"capability": "audio"})
    check("unknown capability is refused, not silently ignored",
          "error" in unk and unk["models"] == [] and unk["total_matched"] == 0,
          str(unk)[:200])
    lv = pager(cat, {"provider": "local", "capability": "vision"})
    check("local rows publish no modalities → counted as unknown, not 'none can'",
          lv["total_matched"] == 0 and lv.get("capability_unknown") == 1
          and "unknown is not 'unable'" in lv["note"], str(lv)[:300])
    check("vision filter counts the unpublished rows it left out",
          v.get("capability_unknown") == 1, str(v.get("capability_unknown")))
    check("unpublished rows carry null flags, never False",
          p["models"][0]["vision"] is None and p["models"][0]["image_gen"] is None,
          str(p["models"][0]))
    check("published rows carry boolean flags",
          isinstance(out["models"][0]["vision"], bool))

    # ── both executors route through the helper ──────────────────────────
    src = _read(repo, "brain/mastercode/runtime/tools.py")
    mr = src[src.find("async def _exec_model_route("):src.find("async def _exec_inference_catalog(")]
    ic = src[src.find("async def _exec_inference_catalog("):src.find("async def _exec_context(")]
    check("model_route op=catalog goes through the pager",
          "(await inf.get_model_catalog(), a)" in mr)
    check("inference_catalog goes through the pager",
          "(await inf.get_model_catalog(), args or {})" in ic)
    check("neither executor returns the raw catalog",
          '"result": await inf.get_model_catalog()}' not in mr + ic)

    # ── the schemas declare the knobs the wall tells the model to use ────
    reg = _read(repo, "brain/mastercode/public_tool_registry.py")
    for tool in ("model_route", "inference_catalog"):
        blk = reg[reg.find(f'"{tool}": {{'):]
        blk = blk[:blk.find("}}},") + 4]
        for prm in ('"search"', '"capability"', '"limit"', '"offset"'):
            check(f"{tool} schema declares {prm}", prm in blk)
        check(f"{tool} description says the catalog is never returned whole",
              "never returned whole" in blk)
    mr_blk = reg[reg.find('"model_route": {'):]
    mr_blk = mr_blk[:mr_blk.find("}}},") + 4]
    for dead in ("platform-billed", "'privatae' =", "'self' =", "prompt-bearing"):
        check(f"model_route description carries no dead lane vocabulary ({dead!r})",
              dead not in mr_blk)

    print()
    if FAILED:
        print(f"FAIL catalog_pagination: {FAILED} check(s) failed")
        return 1
    print("PASS catalog_pagination: the catalog tools return a page, never the whole thing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
