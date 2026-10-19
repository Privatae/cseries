#!/usr/bin/env python3
"""Ratchet: the inference re-pricing pass corrects labels, never invents them.

WHY IT EXISTS. est_cost_usd and cost_method are derived ONCE, at write time,
from in-memory caches. The one that matters for first-party models
(_DIRECT_MODELS_CACHE) is filled only by _direct_models(), whose callers are
all UI routes — so background inference, which is 88% of calls on one Host,
runs with it empty and writes 'unpriced'.

Measured 2026-09-09 on one Host, over the 1,074 unpriced inference rows.
ORDER MATTERS, and the shipped order is catalog-then-direct:
  * OpenRouter catalog warm (431 models), direct caches COLD
        -> 13 priced, 0 relabelled     (a fix aimed at this cache does nothing)
  * SHIPPED ORDER, catalog then direct providers
        -> 36 priced ($18.67), 1,009 relabelled k3 -> subscription,
           29 still unpriced
'unpriced' goes from 94% of inference calls to 2.7%, and none of it is new
instrumentation — the tokens were always on the row; only the derived label
was thrown away.

THE TWO RULES THIS GUARDS
  1. Never restate an answered row. Only cost_method='unpriced' is touched,
     enforced in the UPDATE's own WHERE so a concurrent writer cannot lose a
     race. That is also what makes the pass idempotent.
  2. Never invent a price. The pass calls estimate_call_cost_usd() and
     price_basis() — the SAME functions provider_gateway uses at write time —
     so a re-priced row and a freshly written one cannot disagree. A model the
     platform still cannot price stays 'unpriced'.

Run: python3 scripts/test_inference_reprice.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "server" / "routes" / "inference.py"
GW = ROOT / "brain" / "mastercode" / "provider_gateway.py"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def _fn_source(src: str, name: str) -> str:
    for node in ast.parse(src).body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def main() -> int:
    src = SRC.read_text()
    gw = GW.read_text()

    body = _fn_source(src, "reprice_unpriced")
    check(bool(body), "reprice_unpriced is gone from inference.py")
    # Comments stripped before any presence check. This file explains the bug
    # by naming the very symbols it asserts on, and the first version of the
    # _direct_models check passed against a body where the CALL was deleted
    # and only the comment remained — a gate fooled by its own explanation.
    code = "\n".join(l for l in (body or "").splitlines()
                     if not l.lstrip().startswith("#"))
    if not body:
        print("FAILED\n  - " + failures[0], file=sys.stderr)
        return 1

    # ── rule 1: never restate an answered row ──────────────────────────────
    updates = re.findall(r"UPDATE inference_cost_log.*?(?=\"\"\"|\",)", body, re.S)
    check(bool(updates), "the pass no longer issues an UPDATE")
    for u in updates:
        check("cost_method = 'unpriced'" in u or "cost_method='unpriced'" in u,
              "an UPDATE lacks the `AND cost_method = 'unpriced'` guard — it "
              "can restate a row that was already priced, and the pass stops "
              "being idempotent")
    check("WHERE cost_method = 'unpriced'" in body,
          "the SELECT no longer filters to unpriced rows")

    # ── rule 2: never invent a price ───────────────────────────────────────
    # estimate_call_cost_detail() is the function BOTH lanes call as of
    # 2026-09-15; estimate_call_cost_usd() is a one-line wrapper over it that
    # drops the "which rates applied" half. Either satisfies the rule this
    # guards — ONE pricing definition — but the write path needs the marker, so
    # the detail form is what it uses and what is asserted here.
    check("estimate_call_cost_detail(" in code and "price_basis(" in code,
          "the pass no longer uses the write path's own pricing functions — a "
          "second pricing definition WILL drift from provider_gateway's")
    check("estimate_call_cost_detail" in gw and "price_basis" in gw,
          "provider_gateway no longer uses those functions; the two lanes have "
          "diverged and re-priced rows will disagree with fresh ones")
    # The cache split (2026-09-15). A re-priced row and a freshly-written one
    # must agree, and the write path is now cache-aware: pricing from tokens_in
    # alone would re-introduce the cache-blind number on every row touched.
    check("cache_read_tokens=" in code and "cache_write_tokens=" in code,
          "the pass prices without the cache counts, so it re-derives the "
          "cache-BLIND number the write path no longer writes")
    check("cache_read_tokens=" in gw and "cache_write_tokens=" in gw,
          "provider_gateway prices without the cache counts it already has in "
          "hand — every cached token charged at the full input rate")
    # a still-unknown model must be left alone, not defaulted
    check('!= "unpriced"' in body or "!= 'unpriced'" in body,
          "the pass writes a label even when price_basis returns 'unpriced' — "
          "that is a no-op write at best and a fabricated label at worst")

    # ── the cache that actually matters ────────────────────────────────────
    check("await _direct_models(" in code,
          "the pass no longer warms the FIRST-PARTY caches. Measured: with "
          "only the OpenRouter catalog warm it relabels 0 of 1,009 k3 rows, "
          "because k3's flat-plan status lives in _DIRECT_MODELS_CACHE")

    # ── it must not be able to take the pod down ───────────────────────────
    check(code.count("except Exception") >= 3,
          "the pass has fewer than three guards; it runs at startup off the "
          "catalog warm task and a provider outage must never break boot")

    warm = _fn_source(src, "warm_model_catalog")
    check("reprice_unpriced" in warm,
          "warm_model_catalog no longer triggers the pass, so nothing ever "
          "corrects rows written while the caches were cold")
    check(re.search(r"if ok:\s*\n\s*await reprice_unpriced", warm) is not None,
          "the pass is no longer gated on a SUCCESSFUL catalog build — "
          "re-pricing against a degraded catalog would relabel rows from a "
          "cache that is itself incomplete")

    # ── the SECOND pass: rows priced by the cache-blind formula ────────────
    # reprice_cache_blind touches rows that ARE answered, which is the opposite
    # starting state from reprice_unpriced — so its never-restate guard cannot
    # be 'unpriced' and has to be the marker instead. These pin that it still
    # has one, and that the marker is what makes it idempotent.
    cb = _fn_source(src, "reprice_cache_blind")
    check(bool(cb), "reprice_cache_blind is gone from inference.py")
    cbc = "\n".join(l for l in (cb or "").splitlines()
                    if not l.lstrip().startswith("#"))
    if cb:
        cb_updates = re.findall(r"UPDATE inference_cost_log.*?(?=\"\"\"|\",)",
                                cb, re.S)
        check(bool(cb_updates), "the cache pass no longer issues an UPDATE")
        for u in cb_updates:
            check("cost_method = 'list'" in u,
                  "the cache pass can now touch a 'subscription', 'free' or "
                  "'unpriced' row — only a row someone priced from a rate card "
                  "may be re-derived")
            check("NOT (metadata ? 'priced')" in u,
                  "the cache pass lost its marker guard in the UPDATE — it can "
                  "restate a row it (or a fresh write) already derived, and it "
                  "stops being idempotent")
        check("'list'" in cbc and "NOT (metadata ? 'priced')" in cbc,
              "the SELECT no longer scopes to priced-but-unmarked rows")
        check("estimate_call_cost_detail(" in cbc,
              "the cache pass has its own pricing arithmetic — it must call "
              "the write path's function or the two will disagree")
        check("if usd is None:" in cbc and "continue" in cbc,
              "a model the catalog can no longer price must keep its existing "
              "number; zeroing it would destroy the only estimate that exists")
        check("reprice_cache_blind" in warm,
              "warm_model_catalog no longer triggers the cache pass")
        check(re.search(r"if ok:(.|\n)*?await reprice_cache_blind", warm)
              is not None,
              "the cache pass is no longer gated on a SUCCESSFUL catalog "
              "build — re-deriving against a degraded catalog would rewrite "
              "good dollars from an incomplete cache")

    if failures:
        print("FAILED: the re-pricing pass can restate or invent\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("PASSED: re-pricing corrects labels without restating or inventing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
