#!/usr/bin/env python3
"""Gate: a coding run's own event stream is the source of its token count.

THE DEFECT (measured on one Host). harness_usage.tokens_total was
0 for every engine except claude-code: cursor 119 runs / 0 with tokens, grok
100 / 0, kimi 28 / 0, antigravity 6 / 0, opencode 3 / 0, codex 20 / 1 (that one
scraped from stderr).

It is NOT that the vendors withhold it. Kimi reports tokens perfectly on the
CHAT lane — 241 calls, 241 with tokens, 2,359,068 in — same vendor, same
account. The harness lane lost it: every vendor CLI emits usage in its event
stream, the pod runtime already normalizes it into `usage` frames
(_harness_runtime/events.py), those frames land at
<session_dir>/<label>.events.jsonl, and then they were dropped twice — the
frontend has an explicit `case "usage": return []`, and record_harness_usage
reads run_out (transcript + files), never the stream.

There is no /usage command to call instead: the real binaries were probed
(cseries-harness-kimi:v0.1.7, cseries-harness-codex:v0.2.9) and neither has a usage
subcommand — /usage is an interactive TUI slash command and the pods run these
CLIs non-interactively.

Pure: no DB, no pod. The normalizer and the events-file reader are EXECUTED.

Run: APP_ROOT=<repo> python3 scripts/test_harness_stream_usage.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"[stream-usage] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[stream-usage] FAIL {label}" + (f" — {detail}" if detail else ""))


def ev(**p):
    return json.dumps({"kind": "usage", "payload": p})


def main() -> int:
    from server.routes.compute import aggregate_usage_events as agg

    # ── the six real vendor shapes, taken from the pod parsers ──────────────
    # codex turn.completed (events.py:179-180) — exact vendor field names,
    # emitted PER TURN, so a run's total is their sum.
    r = agg([ev(input_tokens=12000, cached_input_tokens=9000,
                cache_write_input_tokens=500, output_tokens=800,
                reasoning_output_tokens=300),
             ev(input_tokens=15000, cached_input_tokens=12000,
                output_tokens=600, reasoning_output_tokens=120)], "codex")
    check("codex turn.completed frames sum",
          r["input_tokens"] == 27000 and r["output_tokens"] == 1400
          and r["cached_input_tokens"] == 21000 and r["reasoning_tokens"] == 420,
          str(r))

    # opencode step_finish — the pod parser already normalizes tokens{}/cache{},
    # and the vendor sends its OWN cost. Per model call, so summed.
    # Totals as opencode states them: tokens.total = input + cache.read +
    # output + reasoning (measured exactly on one run —
    # cache is NOT inside input). This fixture used to carry a total of 4600
    # for input 4000 + cache 3000 + output 500, a shape opencode never sends.
    r = agg([ev(input_tokens=4000, output_tokens=500, reasoning_tokens=100,
                total_tokens=7600, cache_read_tokens=3000, cost_usd=0.0123),
             ev(input_tokens=2000, output_tokens=250, total_tokens=2250,
                cost_usd=0.0061)], "opencode")
    check("opencode frames sum and carry the vendor's own cost",
          r["total_tokens"] == 9850 and r["input_tokens"] == 9000
          and abs(r["cost_usd"] - 0.0184) < 1e-9, str(r))

    # claude family sends ONE CUMULATIVE object on its final event
    # (events.py:114 "always the LAST event") — summing would double-count.
    r = agg([ev(input_tokens=50, output_tokens=10),
             ev(input_tokens=90000, output_tokens=4200,
                cache_read_input_tokens=70000,
                cache_creation_input_tokens=1500)], "claude-code")
    # input_tokens is 90000 + 70000 + 1500 = 161500, NOT 90000: this fixture
    # uses the Anthropic field names, and on that wire input_tokens is the
    # UNCACHED remainder with cache_read/cache_creation carried separately
    # (provider_gateway.py:1158-1161). Treating cache as a subset there — as
    # the first version of this code did — undercounts every cursor/grok run
    # by its whole cached prefix and lets cached_share exceed 1.0.
    check("claude-family takes the LAST cumulative frame, never the sum",
          r["agg_mode"] == "last" and r["input_tokens"] == 161500
          and r["cached_input_tokens"] == 70000
          and r["cache_write_tokens"] == 1500, str(r))
    check("...and the anthropic wire's split is summed into the true input",
          r["frames"][-1].get("input_basis") == "anthropic"
          and r["cached_input_tokens"] <= r["input_tokens"],
          "cached must never exceed input under either convention")
    _oa = agg([ev(prompt_tokens=76500, completion_tokens=400,
                  cached_tokens=70000)], "kimi")
    check("...while the OpenAI wire's inclusive input is left alone",
          _oa["input_tokens"] == 76500
          and _oa["cached_input_tokens"] <= _oa["input_tokens"])

    # Stored frames must be ADDITIVE under both modes, so SUM() in the window
    # reads can never disagree with the run total (two read paths, one table).
    for _h, _lines in (("claude-code", [ev(input_tokens=50, output_tokens=10),
                                        ev(input_tokens=9000, output_tokens=400)]),
                       ("codex", [ev(input_tokens=1200, output_tokens=80),
                                  ev(input_tokens=1500, output_tokens=60)])):
        _r = agg(_lines, _h)
        check(f"{_h}: SUM over stored frames == the run total",
              sum(f["total_tokens"] for f in _r["frames"]) == _r["total_tokens"],
              f"mode={_r['agg_mode']} seen={_r['frames_seen']} stored={_r['frame_count']}")
    check("...and a sampled variant shares the base harness's rule",
          agg([ev(input_tokens=7), ev(input_tokens=9)],
              "claude-code-s2")["input_tokens"] == 9)

    # A vendor that renames a field must degrade to a VISIBLE unknown, never a
    # silent zero — the exact failure this whole lane keeps producing.
    r = agg([ev(prompt_tokens=8000, completion_tokens=900, brand_new_field=42)],
            "kimi")
    check("an aliased field name still maps",
          r["input_tokens"] == 8000 and r["output_tokens"] == 900, str(r))
    check("an UNKNOWN numeric field is surfaced, not dropped",
          r["frames"][0].get("unmapped") == {"brand_new_field": 42}, str(r))

    # No usable usage must yield {} so the caller falls back rather than
    # writing a zero that reads as data.
    check("a usage frame with no numbers yields {} (no manufactured zero)",
          agg([ev(note="done")], "grok") == {})
    check("a stream with no usage frame at all yields {}",
          agg([json.dumps({"kind": "text", "payload": {"text": "hi"}})], "kimi") == {})

    # Frames and the run total must agree by construction, so a per-frame
    # window query ("how much of this 5-hour window") sums real numbers.
    r = agg([ev(input_tokens=12000, output_tokens=800),
             ev(input_tokens=15000, output_tokens=600)], "codex")
    check("each frame carries its own derived total",
          [f["total_tokens"] for f in r["frames"]] == [12800, 15600], str(r))
    check("...and they sum to the run total",
          sum(f["total_tokens"] for f in r["frames"]) == r["total_tokens"])

    # ── the events FILE is actually read (the join that was missing) ────────
    from server.routes.review import attach_stream_usage, _events_path
    d = Path(tempfile.mkdtemp())
    _events_path(d, "kimi").write_text("\n".join([
        json.dumps({"kind": "status", "payload": {"state": "init"}}),
        ev(prompt_tokens=18432, completion_tokens=1204, cached_tokens=15000)]))
    run_out = {"ok": True}
    attach_stream_usage(run_out, d, "kimi", "kimi")
    check("attach_stream_usage folds the run's events.jsonl onto the result",
          (run_out.get("usage_stream") or {}).get("total_tokens") == 19636,
          str(run_out.get("usage_stream")))

    missing = {"ok": True}
    attach_stream_usage(missing, d, "no-such-variant", "kimi")
    check("a missing events file leaves the result untouched",
          "usage_stream" not in missing)

    # ── the recorder prefers the stream over the stderr scrape ─────────────
    csrc = (_ROOT / "server/routes/compute.py").read_text()
    check("record_harness_usage prefers the stream aggregate",
          'run_out.get("usage_stream")' in csrc and '"stream"' in csrc)
    check("...and still falls back to _parse_usage without one",
          "tokens, tsrc = _parse_usage(run_out)" in csrc)
    rsrc = (_ROOT / "server/routes/review.py").read_text()
    # a call site runs it directly or in a worker thread (it reads the whole
    # events file: off the loop)
    _sites = rsrc.count("attach_stream_usage(") - 1 + rsrc.count("to_thread(attach_stream_usage,")
    check("BOTH run paths attach stream usage (build + fan-out)",
          "def attach_stream_usage(" in rsrc and _sites >= 2,
          f"found {_sites} call sites")
    check("per-frame rows are persisted for the quota windows",
          "record_usage_events" in csrc and "harness_usage_event" in csrc)
    mig = _ROOT / "migrations" / "v114_harness_usage_event.sql"
    check("the per-frame table ships as a migration", mig.is_file())

    # ── the subscription refresh windows (session / 5h / 24h / 7d) ─────────
    # A flat plan has no per-token price, so "what did that cost" means how
    # much of a refresh window the work ate. That question needs timestamped
    # FRAMES — a run total cannot answer it — which is why v114 stores them.
    check("the windows endpoint exists and covers 5h/24h/7d",
          "/windows" in csrc and "5 * 3600" in csrc
          and "24 * 3600" in csrc and "7 * 24 * 3600" in csrc)
    check("...with a per-session window when a session is named",
          '("session", None)' in csrc)
    check("...reporting the cached share (the cheap part of a subscription)",
          '"cached_share"' in csrc)
    # The honesty constraint: consumption is measurable, quota remaining is
    # not. The vendor's plan size and reset boundary are published nowhere the
    # platform reads, and a fabricated denominator would turn a real number
    # into a confident wrong one.
    check("it states the numbers are CONSUMPTION, not quota remaining",
          '"quota_basis"' in csrc and "measured consumption" in csrc)
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as _P
    check("the host can ask for it in conversation",
          "harness_usage_windows" in _P)
    _d = _P.get("harness_usage_windows", {}).get("function", {}).get("description", "")
    check("...and the tool forbids stating a quota percentage",
          "never quota remaining" in _d and "invent a denominator" in _d, _d[:80])

    print()
    if FAILED:
        print(f"[stream-usage] {len(FAILED)} gate(s) FAILED: {', '.join(FAILED)}")
        return 1
    print("[stream-usage] all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
