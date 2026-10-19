#!/usr/bin/env python3
"""test_run_ledger_truth.py — the bench's numbers say what they measured:

  1. A factory build's registry row carries the BILLED cost and the token
     count WITH provenance (tools._build_usage_for_task); a harness that
     reported no usage reads None, never 0; `rounds` is None on a build.
     harness_costs says how many runs actually reported tokens.
  2. A failed run is classified by KIND (runtime/run_failure.py); the track
     record counts only model-attributable kinds in `model_failed`, and
     harness_transcript names the kind, so a dead stream is never scored
     against the model.

Container-free: pure functions, a stub pool, static checks.
APP_ROOT=<repo> python3 scripts/<this>.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 2. failure kinds, on the strings a live Host actually recorded ──────
from mastercode.runtime.run_failure import (  # noqa: E402
    failure_kind, summarize_runs, counts_against_model, NOT_THE_MODEL)

REAL = {
    "the live stream died mid-run after 2 frames; the build was not re-run and its partial work is kept": "transport",
    "the live stream died mid-run after 191 frames; the build was not re-run": "transport",
    "Server disconnected": "transport",
    "harness exited 1: error: failed to run prompt: provider.connection_err": "transport",
    "harness exited 1: Error: Internal error: { \"message\": \"reqwest error": "transport",
    "harness wake failed: harness must be one of codex|cursor|copilot|grok": "transport",
    "claude-code not connected — sign in on your own surface; the platform": "credential",
    "kimi produced no output for 10 minutes and was stopped as stuck — it w": "stalled",
    "kimi exceeded 15min cap": "stalled",
    "harness exited 1 with no output": "harness",
    "harness exited 1: ddEventListener, no inline handlers. Money formattin": "error",
}
for err, want in REAL.items():
    got = failure_kind(err, 1, False)
    check(f"kind[{want}]: {err[:48]}…", got == want, str(got))
# Negatives: a model's OWN broken output that happens
# to contain "cap", "sign in", "401", "insufficient", "429", "unauthorized"…
# must stay `error` — the kinds that exonerate the model are platform
# phrasings, matched on word boundaries.
NEG = [
    "harness exited 1: Error: Cannot find module 'capitalize'",
    "harness exited 1: redesign index.html: Unexpected token",
    "harness exited 1: SyntaxError in design-injector.js line 1401",
    "harness exited 1: TypeError: insufficient arguments to render()",
    "harness exited 1: escape sequence error",
    "harness exited 1: the capacity planner module failed",
    "harness exited 1: ENOENT: no such file 'landing-page-401.html'",
    "harness exited 1: assertion failed: expected 429 got 500",
    "harness exited 1: unauthorized_field is not defined",
    "harness exited 1: credentials.js: unexpected end of input",
]
for err in NEG:
    got = failure_kind(err, 1, False)
    check(f"negative stays a model failure: {err[18:58]}…", got == "error", str(got))
check("an ok run has no kind", failure_kind("anything", 0, True) is None)
check("a bare status 401 IS a credential failure", failure_kind("harness exited 1: HTTP 401 unauthorized", 1, False) == "credential")
check("a real rate limit IS capacity", failure_kind("harness exited 1: rate limited (429)", 1, False) == "capacity")
tr_w = summarize_runs([{"id": 1, "harness": "h", "model": "m", "ok": True, "created_at": None}], window=400)
check("track record rows carry the window they were drawn from", tr_w[0]["window"] == 400)
check("no error and exit 0 → no kind", failure_kind("", 0, False) is None)
check("transport/credential/capacity never count against the model",
      not any(counts_against_model(k) for k in NOT_THE_MODEL))
check("error/harness/stalled do count", all(counts_against_model(k) for k in ("error", "harness", "stalled")))

now = datetime(2026, 9, 6, 1, 18, tzinfo=timezone.utc)
rows = [
    {"id": 663, "harness": "claude-code", "model": "claude-fable-5", "ok": False,
     "error": "the live stream died mid-run after 2 frames; the build was not re-run", "exit_code": None,
     "elapsed_seconds": 62.76, "created_at": now},
    {"id": 650, "harness": "claude-code", "model": "claude-fable-5", "ok": True, "error": None,
     "exit_code": 0, "elapsed_seconds": 600.0, "created_at": now},
    {"id": 640, "harness": "claude-code", "model": "claude-fable-5", "ok": False,
     "error": "harness exited 1: syntax error in index.html", "exit_code": 1,
     "elapsed_seconds": 30.0, "created_at": now},
    {"id": 664, "harness": "kimi", "model": "k3", "ok": True, "error": None, "exit_code": 0,
     "elapsed_seconds": 245.1, "created_at": now},
]
tr = summarize_runs(rows)
cc = next(g for g in tr if g["harness"] == "claude-code")
check("track record: failed counts every failure", cc["runs"] == 3 and cc["ok"] == 1 and cc["failed"] == 2)
check("track record: model_failed excludes the dead stream", cc["model_failed"] == 1, str(cc))
check("track record: failures split by kind", cc["failed_by_kind"] == {"transport": 1, "error": 1}, str(cc["failed_by_kind"]))
check("track record: last error is the newest failure, with its kind",
      "stream died" in cc["last_error"] and cc["last_error_kind"] == "transport")
check("track record: avg seconds still there", cc["avg_seconds"] == round((62.76 + 600 + 30) / 3, 1))
kimi = next(g for g in tr if g["harness"] == "kimi")
check("track record: a clean row has no failure fields set", kimi["failed"] == 0 and kimi["last_error"] is None)

src_tools = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
check("coding_bench: track record comes from summarize_runs over harness_run rows",
      "_summarize_runs([dict(r) for r in rows], window=_TRACK_WINDOW)" in src_tools
      and "SELECT id, harness, model, ok, error, exit_code, elapsed_seconds, created_at" in src_tools)
check("harness_transcript: names the failure kind, with the exit code",
      '"failure_kind": _failure_kind(row["error"], row["exit_code"], row["ok"])' in src_tools
      and src_tools.count("error, exit_code, elapsed_seconds, model, diff_bytes, status") == 2)
check("coding_bench: the track record window rides the rows",
      "window=_TRACK_WINDOW" in src_tools)

# ── 1. the build's ledger row on its registry record ────────────────────
from mastercode.runtime.tools import _build_usage_for_task  # noqa: E402


class _Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


class _Pool:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return self.row


kimi_row = _Row(tokens_total=0, token_source="unknown", cost_microdollars=3400, cost_method="billed",
                compute_s=244.74, wall_clock_s=245.11, billing_ref="1f77db6b")
pool = _Pool(kimi_row)
u = asyncio.run(_build_usage_for_task(pool, 350))
check("build usage: looks up the build's own ledger session", pool.calls and "factory-build-350" in pool.calls[0][1])
check("build usage: billed cost in dollars", u["cost_usd"] == 0.0034 and u["cost_method"] == "billed")
check("build usage: unreported tokens read None, not 0", u["tokens"] is None and u["token_source"] == "unknown")
check("build usage: a build has no rounds", u["rounds"] is None)
sdk_row = _Row(tokens_total=51651, token_source="sdk", cost_microdollars=8660, cost_method="billed",
               compute_s=623.4, wall_clock_s=623.4, billing_ref="68baf0c1")
u2 = asyncio.run(_build_usage_for_task(_Pool(sdk_row), 348))
check("build usage: a reported count rides through", u2["tokens"] == 51651 and u2["token_source"] == "sdk")
check("build usage: no ledger row → nothing merged", asyncio.run(_build_usage_for_task(_Pool(None), 1)) == {})
check("build finish: merges the ledger row on both outcomes",
      "_ran.update(await _build_usage_for_task(pool, task_id))" in src_tools)
src_ad = (ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
check("agent rows carry cost_usd / cost_method / token_source",
      '"cost_usd": res.get("cost_usd")' in src_ad and '"cost_usd", "cost_method", "token_source",' in src_ad)
check("harness_costs: counts runs that reported tokens and blanks a zero with no data",
      "AS runs_with_tokens" in src_tools and 'd["tokens"] = None' in src_tools and "tokens_note" in src_tools)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
