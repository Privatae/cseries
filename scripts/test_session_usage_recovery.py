#!/usr/bin/env python3
"""Ratchet: usage recovered from a harness's session dir is counted ONCE.

WHY. Some CLIs put no usage on stdout but keep the vendor's own accounting in
the session directory they write for `--resume`. kimi is the live case:
measured 2026-09-09 on one Host, 34 runs in 30 days reported zero tokens
through the stream while every run shipped a native-session tar containing the
numbers. diagnostics._probe_usage fires on exactly that, and its detail line
("a harness gap the platform owns") was right about ownership and wrong about
it being unclosable.

THE TWO TRAPS, both measured on a real tar before the reader was written, and
both of which produce a plausible wrong number rather than an error:

  1. DOUBLE EMISSION. Every usage object appears twice in wire.jsonl — once as
     a root {"type":"usage.record"} line, once nested in the following
     {"event": ...} line. 50 occurrences, 25 distinct. A reader that walks for
     any "usage" key reports exactly 2x the truth.

  2. LOOKS CUMULATIVE, ISN'T. inputCacheRead rises monotonically
     (18,944 -> 127,744) because each turn re-reads a longer cached prefix.
     But `output` falls (14,280 -> 393) and last/SUM is 0.006, so these are
     PER-TURN records and must be SUMMED. Treating the rise as cumulative and
     taking `last` under-reports ~25x.

Run: python3 scripts/test_session_usage_recovery.py
"""
from __future__ import annotations

import ast
import io
import json
import logging
import pathlib
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "server" / "routes" / "compute.py"
REVIEW = ROOT / "server" / "routes" / "review.py"

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def _lift():
    """compute.py's usage helpers, executed without FastAPI/asyncpg."""
    src = SRC.read_text()
    fn = {"usage_frames_from_session_tar", "aggregate_usage_events", "_usage_frame"}
    asg = {"_SESSION_USAGE_READERS", "_USAGE_ALIASES", "_USAGE_COST_KEYS", "_USAGE_AGG",
           # the per-harness cache convention _usage_frame reads (2026-10-07)
           "_CACHE_SEPARATE",
           # the session tar's member cap (2026-09-28), read by the tar reader
           "_SESSION_TAR_MAX_MEMBERS", "_SESSION_USAGE_MAX_RECORDS", "_SESSION_LINE_MAX",
           "_SESSION_MAX_LINES"}
    keep = []
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name in fn:
            keep.append(n)
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) in asg for t in n.targets):
            keep.append(n)
        elif isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) in asg:
            keep.append(n)
    ns = {"json": json, "log": logging.getLogger("t")}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<compute>", "exec"), ns)
    for need in fn | asg:
        if need not in ns:
            raise AssertionError(f"{need} is gone from compute.py")
    return ns


def _tar(lines: list[str]) -> bytes:
    """A tar shaped like kimi's: wire.jsonl under agents/main/."""
    buf, body = io.BytesIO(), "\n".join(lines).encode()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("sessions/s1/agents/main/wire.jsonl")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def main() -> int:
    ns = _lift()
    frames_of = ns["usage_frames_from_session_tar"]
    agg_of = ns["aggregate_usage_events"]

    # The real shape: each record emitted twice, root + nested under /event.
    rec = {"type": "usage.record", "usageScope": "turn", "model": "k3",
           "usage": {"inputOther": 100, "output": 10,
                     "inputCacheRead": 1000, "inputCacheCreation": 0}}
    echo = {"type": "assistant", "event": {"usage": rec["usage"]}}
    lines = []
    for _ in range(3):
        lines += [json.dumps(rec), json.dumps(echo)]

    got = frames_of(_tar(lines), "kimi")
    check("the nested echo is not counted — 3 records, not 6", len(got) == 3,
          f"got {len(got)}; a reader walking for any 'usage' key doubles the run")

    a = agg_of(got, "kimi")
    # Keyed on OUTPUT: it is untouched by the cache-summing rule, so this
    # measures the sum-vs-last decision and nothing else.
    check("per-turn records are SUMMED, not treated as cumulative",
          a.get("output_tokens") == 30,
          f"output={a.get('output_tokens')} — 10 would mean 'last' was used, "
          "a ~25x under-report on a real run")
    check("kimi's field names map onto the canonical ones",
          a.get("cached_input_tokens") == 3000 and a.get("cache_write_tokens") == 0,
          f"cached={a.get('cached_input_tokens')} — inputCacheRead/"
          "inputCacheCreation must be aliased")
    # kimi's `inputOther` is the input OTHER THAN cache — its own measured
    # total equals inputOther + inputCacheRead + output on every sample. Cache
    # is therefore SUMMED INTO input, the same convention as the Anthropic
    # wire. The first version of this gate asserted the opposite and would have
    # locked in a 13x under-report.
    check("cache reads are SUMMED INTO input, not treated as inside it",
          a.get("input_tokens") == 3300 and a.get("total_tokens") == 3330,
          f"input={a.get('input_tokens')} total={a.get('total_tokens')}; "
          "300/330 means the frame was read as cache-inclusive")
    check("cached_share stays <= 1.0, compute.py's own invariant",
          a.get("cached_input_tokens", 0) <= a.get("input_tokens", 0),
          f"cached={a.get('cached_input_tokens')} > input={a.get('input_tokens')}")

    check("kimi is absent from _USAGE_AGG, i.e. the default 'sum'",
          "kimi" not in ns["_USAGE_AGG"],
          "listing kimi as 'last' would keep only the final turn")

    # Never invent a frame.
    check("a tar with no usage yields nothing",
          frames_of(_tar([json.dumps({"type": "assistant", "content": "hi"})]), "kimi") == [],
          "a status-only session must not produce a zero-token frame")
    check("a harness with no reader yields nothing",
          frames_of(_tar(lines), "codex") == [],
          "only harnesses with a declared reader are parsed")
    check("an unreadable tar is survivable", frames_of(b"not a tar", "kimi") == [],
          "a corrupt tar costs telemetry, never the run")
    check("sampled variants share the base harness's reader",
          len(frames_of(_tar(lines), "kimi-s2")) == 3,
          "fan-out variants are '<harness>-s2' and must resolve to 'kimi'")

    # The recovery must run at COLLECTION, so telemetry outlives the tar.
    rsrc = REVIEW.read_text()
    check("recovery is wired into _collect_session",
          # the collected tar's FILE since 2026-09-28 (spilled to disk, never
          # held in memory): review._collect_session passes `final`
          "_usage_from_session(final" in rsrc,
          "parsing at read time would let retention destroy the history")
    check("recovery cannot take down a run",
          "session-usage recovery failed" in rsrc,
          "the call must be guarded — telemetry is never worth a build")

    # ── the run row and the frame table must not disagree ─────────────────
    csrc = SRC.read_text()
    check("the run row consults frames when the stream gave nothing",
          'tsrc == "unknown"' in csrc and "FROM harness_usage_event" in csrc,
          "without this the solo path records 0 tokens while /windows reports "
          "real ones from the same frames — two readers, one table, opposite "
          "answers")
    check("the batch path backfills the already-written run row",
          "UPDATE harness_usage SET tokens_total" in rsrc
          and "token_source = 'unknown'" in rsrc,
          "the batch lane records the run BEFORE collecting the session, so "
          "the row needs backfilling; guarded on 'unknown' so a real stream "
          "reading is never overwritten")
    check("'session' is counted in the provenance breakdown",
          "token_source = 'session'" in csrc and '"session": int(r["tok_session"])' in csrc,
          "an uncounted provenance value makes token_sources stop summing to "
          "runs — the exact defect adding 'stream' fixed")
    check("the stream wins when it carries usage — no double on a version bump",
          'events.jsonl' in rsrc and '"kind":"usage"' in rsrc,
          "kimi emits no stdout usage TODAY (0.36.1 -> 0.41.0 in three weeks); "
          "the day a version does, the solo path writes tar frames BEFORE the "
          "stream lane writes its own and the already-written check cannot see "
          "them — a silent exact doubling")
    check("collection is idempotent without a unique index",
          "SELECT 1 FROM harness_usage_event" in rsrc,
          "harness_usage_event has no uniqueness on (session_id, harness, "
          "seq), so a retried collection would silently double a run")

    print()
    if FAILED:
        print(f"[session-usage] {len(FAILED)} gate(s) FAILED: {', '.join(FAILED)}")
        return 1
    print("[session-usage] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
