"""ComputeView — cross-harness usage, spend, and "which harness do you ship?".

The capability no single vendor can offer: Privatae sits between the user and
EVERY harness, so it aggregates usage across all of them AND attributes it to the
outcome — which harness's code the user actually committed. Headline metric:
COST PER ACCEPTED COMMIT (a cheap harness nobody ships from is worse value than a
pricier one you always pick).

Capture methodology is labeled per number and surfaced honestly: vendor billing
is dashboard-locked (OAuth-session creds), so token counts are `parsed` (codex
prints them) / `sdk` (claude-code) / `unknown`, and cost is OUR compute fee
(metered pod time × the G3e rate), not a vendor invoice. Directional, transparent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from server import brain_service
from server.applications.middleware import requires_permission

# ONE definition, imported from review. An earlier version of this line
# duplicated the constant and justified it with an import cycle that does not
# exist: review imports compute only at CALL time (no module-scope import), and
# both import orders load cleanly. That comment asserted a mechanism nobody had
# checked — exactly the kind of claim that should never have been written down.
from server.routes.review import SESSIONS_ROOT  # noqa: E402

log = logging.getLogger("privatae.compute")
router = APIRouter(prefix="/api/compute", tags=["compute"])

# Our compute fee basis (G3e metering). Rate is a pricing decision (still TBD) — 0
# until set, so usage is captured NOW and cost lights up the moment a rate exists.
COMPUTE_RATE_UD_PER_S = float(os.environ.get("COMPUTE_RATE_MICRODOLLARS_PER_S", "0"))

# THE SCHEMA IS DECLARED, NOT ENSURED.
# harness_usage (init-db.sql:1547), harness_usage_event
# (migrations/v114_harness_usage_event.sql:57) and harness_run
# (migrations/v079_harness_run.sql:15) — plus every index and the four
# ALTERs this module used to run — are all declared in the shipped schema and
# applied at boot. NOTE harness_usage's declaration is init-db.sql, which is NOT
# a migration: entrypoint.sh:314 runs it only on an empty database. It is
# present on all 8 live hosts so nothing is missing today, but a host that
# lacked it would have no creator — see the init-db.sql note in
# migrations/v147_band_c_kernel_vault_apps.sql, which is where that distinction
# cost something. The block that used to sit here called itself belt-and-suspenders and
# named its own canonical migration on every statement (v079, v080, v089, v112,
# v114, v128); it was a mirror, and a mirror in a request handler is what kept
# the kernel needing DDL rights to serve a page.


# ── stream-captured harness usage ────────────────────────────────────────────
# Every vendor CLI emits usage in its event stream and the pod runtime ALREADY
# normalizes it into `usage` frames (system-apps/_harness_runtime/events.py:
# parse_claude_family, parse_grok, parse_codex_events, parse_kimi_roles,
# parse_antigravity, parse_opencode). Those frames land on the host at
# <session_dir>/<label>.events.jsonl — the file a Workbench run card streams and replays.
#
# They were then discarded twice: the frontend has an explicit
# `case "usage": return []`, and the host's _parse_usage never sees them
# because it reads run_out (transcript + files), not the stream. That is why
# kimi reports tokens in chat and zeros in builds — same vendor, and in the
# harness lane the number arrives and gets binned. Measured on one Host:
# 119 cursor runs, 100 grok, 28 kimi, all with tokens_total = 0.
#
# There is NO /usage command to call: the vendor binaries were probed
# (cseries-harness-kimi:v0.1.7, -codex:v0.2.9) and neither has a usage
# subcommand — /usage is an interactive TUI slash command, and the pods run
# these non-interactively. The stream is the only non-interactive source, and
# it is already flowing.
#
# Field names are NOT consistent. Only codex and opencode arrive with known
# names (the pod parser normalizes opencode's tokens{}/cache{} nesting);
# claude, grok, kimi and antigravity are passed through VERBATIM — whatever
# the vendor called them is what lands. Hence an alias map plus an explicit
# `unmapped` record: a vendor renaming a field degrades to a visible unknown
# rather than a silent zero, which is the failure this lane keeps producing.
_USAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": (
        # `inputOther` is kimi's UNCACHED remainder (wire.jsonl usage.record),
        # the same semantics as the Anthropic wire's prompt_tokens — cache
        # reads are counted separately below, never folded in here.
        "input_tokens", "prompt_tokens", "promptTokens", "input", "tokens_in",
        "inputTokens", "inputOther"),
    "output_tokens": (
        "output_tokens", "completion_tokens", "completionTokens", "output",
        "tokens_out", "outputTokens"),
    "cached_input_tokens": (
        "cached_input_tokens", "cache_read_input_tokens", "cache_read_tokens",
        "cached_tokens", "cacheReadTokens", "cache_read", "inputCacheRead"),
    "cache_write_tokens": (
        "cache_write_input_tokens", "cache_creation_input_tokens",
        "cache_write_tokens", "cacheWriteTokens", "cache_write",
        "inputCacheCreation"),
    "reasoning_tokens": (
        # `thinking_tokens` is antigravity's spelling (all 29 usage frames of
        # one run); it was landing in `unmapped`. On every one of
        # those frames it is <= output_tokens — the reasoning share of the
        # output, the same relation codex's reasoning_output_tokens has.
        "reasoning_output_tokens", "reasoning_tokens", "reasoningTokens",
        "reasoning", "thinking_tokens"),
    "total_tokens": ("total_tokens", "totalTokens", "total", "tokens_total"),
}
# Vendor-reported dollars (opencode's step_finish carries `cost`). DISPLAY
# ONLY — the estimate lane, beside harness_usage.vendor_cost_microdollars.
# Never billed: Privatae bills compute, and the vendor's bill is never
# visible to the platform.
_USAGE_COST_KEYS = ("cost_usd", "cost", "total_cost_usd")

# How a run's total is derived from its frames — stated per harness with the
# evidence for each, because an earlier version of this comment asserted more
# than it had checked. What is actually established:
#
#   grok        'last'  — events.py:114 documents the final event as carrying
#                         "usage":{…} plus modelUsage, "always the LAST event".
#                         That comment is grok's, and only grok's.
#   antigravity 'last'  — emits usage TWICE: on every agent_response DONE frame
#                         (events.py:613-618) AND on the result frame
#                         (:658-661). Under 'sum' a run counts its turns and
#                         then the envelope again. 'last' takes the envelope,
#                         which is the vendor's own summary. UNVERIFIED against
#                         a real multi-turn agy run — if the result usage is
#                         per-result rather than cumulative this UNDERCOUNTS.
#                         Recorded as a known open question, not a certainty.
#   claude-code 'last'  — INERT, and kept only so the entry is not silently
#                         re-added on a wrong premise. The claude-code sidecar
#                         emits no kind="usage" frame at all: usage rides its
#                         RESULT payload (claude-code-harness/bin/main.py:671,
#                         678), which _parse_usage already reads as 'sdk'. That
#                         is why claude-code was the one engine reporting
#                         tokens before this work.
#   cursor      'last'  — parse_claude_family only emits usage from non-result
#                         frames (the result path returns early, events.py:64),
#                         so whether cursor produces any usage frame is
#                         UNVERIFIED. If it does not, cursor stays at zero and
#                         the fix has not reached it.
#   codex       'sum'   — one usage per turn.completed (events.py:179-180).
#   opencode    'sum'   — one per step_finish, i.e. per model call (:679).
#   kimi        'sum'   — default; its cardinality is UNVERIFIED, resting on a
#                         synthetic fixture rather than a real multi-event run.
#
# The default is 'sum' — right for an incremental emitter, wrong for a
# cumulative one. Every UNVERIFIED entry above is why harness_usage_event
# stores frames individually: a wrong rule stays correctable from captured
# data instead of being a number that is simply lost.
_USAGE_AGG = {"claude-code": "last", "cursor": "last", "grok": "last",
              "antigravity": "last"}

# Harnesses whose usage reports cache reads SEPARATELY from input, under
# field names that do not say so (the Anthropic spellings and kimi's
# inputOther are recognised by name below). Per harness, each backed by the
# vendor's own numbers on a captured run, not by a field name:
#
#   antigravity  one run, all 29 frames: total_tokens ==
#                input_tokens + output_tokens EXACTLY, i.e. the vendor's total
#                leaves cache out; and input + cache_read_tokens tracks the
#                growing context (23,557 -> 25,550 -> ... -> 141,168) while
#                input alone jumps around it (2,257 next to 138,911 cached).
#                Read as inclusive, recorded input was ~6x low.
#   cursor       one run: the result frame's cacheReadTokens 205,468 >
#                inputTokens 57,238 — a subset cannot exceed its whole.
#   opencode     one run: tokens.total 49,744 == input 16,720 + cache.read
#                1,024 + output 2 + reasoning 31,998 EXACTLY.
_CACHE_SEPARATE = frozenset({"antigravity", "cursor", "opencode"})


def _usage_frame(payload: dict, harness: str = "") -> dict:
    """One `usage` frame payload -> canonical fields + what could not be mapped.

    `harness` is the base harness id (no "-sN" sample suffix); it selects the
    per-harness cache convention (_CACHE_SEPARATE).

    Returns {} for a payload carrying no recognizable numbers, so a status-ish
    frame never manufactures a zero-token row."""
    if not isinstance(payload, dict):
        return {}
    out: dict = {}
    taken: set = set()
    for canon, names in _USAGE_ALIASES.items():
        for n in names:
            v = payload.get(n)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and v >= 0:
                out[canon] = int(v)
                taken.add(n)
                break
    for n in _USAGE_COST_KEYS:
        v = payload.get(n)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out["cost_usd"] = float(v)
            taken.add(n)
            break
    if not out:
        return {}
    # TWO WIRE CONVENTIONS, and getting this wrong silently undercounts.
    # On the OpenAI-shaped wire prompt_tokens ALREADY INCLUDES the cached
    # prefix, so cache is a subset of input. On the Anthropic Messages wire it
    # does NOT: input_tokens is the uncached remainder and cache_read /
    # cache_creation are separate fields, so the true input is their SUM —
    # stated outright in provider_gateway.py:1158-1161, which this originally
    # claimed the opposite of. Left uncorrected, an anthropic-shaped frame
    # (cursor and grok mirror Claude's stream-json) would undercount every run
    # by its whole cached prefix, and cached_share could exceed 1.0.
    #
    # The discriminator is the vendor's own FIELD NAME, not a guess about the
    # vendor: cache_read_input_tokens / cache_creation_input_tokens are the
    # Anthropic spellings. Anything else (cached_tokens, cache_read_tokens…)
    # is the inclusive convention.
    # kimi's wire.jsonl uses a third spelling of the SAME convention, and its
    # own field name says so: `inputOther` is the input OTHER THAN cache, with
    # inputCacheRead / inputCacheCreation alongside. Confirmed against the
    # vendor's own accounting rather than inferred from the name — kimi emits a
    # measured token total next to each usage.record, and on every sample it
    # equals inputOther + inputCacheRead + output EXACTLY (34,796 / 35,239 /
    # 44,362 / 46,703 / 47,939), never inputOther + output. Corroborated by
    # shape: 24 of 25 frames in one run have inputCacheRead > inputOther, which
    # is impossible if cache sat inside it.
    #
    # Read as inclusive, a kimi run under-reports by its whole cached prefix —
    # 180,699 instead of 2,479,835 on the run in this PR's description, and
    # cached_share reads 20.5 where compute.py's own invariant says <= 1.0.
    _anthropic_shape = any(
        k in payload for k in ("cache_read_input_tokens",
                               "cache_creation_input_tokens",
                               "inputCacheRead", "inputCacheCreation"))
    _separate = str(harness or "").split("-s")[0] in _CACHE_SEPARATE
    if _anthropic_shape or _separate:
        out["input_tokens"] = (int(out.get("input_tokens") or 0)
                               + int(out.get("cached_input_tokens") or 0)
                               + int(out.get("cache_write_tokens") or 0))
        out["input_basis"] = "anthropic"      # input was uncached-only; summed
        # A vendor total that left cache out (antigravity's) is now smaller
        # than input + output; the true total is never below that. A total
        # that already carries more (opencode adds reasoning on top of
        # output) is kept.
        if out.get("total_tokens"):
            out["total_tokens"] = max(int(out["total_tokens"]),
                                      int(out["input_tokens"])
                                      + int(out.get("output_tokens") or 0))
    # Derive the frame's own total when the vendor did not send one. input is
    # now the TRUE total context under either convention, so cache is never
    # added on top a second time. Applied per frame so a window query sums
    # real numbers, and so frames and the run total cannot disagree.
    if not out.get("total_tokens"):
        out["total_tokens"] = int(out.get("input_tokens") or 0) + int(
            out.get("output_tokens") or 0)
    # Numbers the vendor sent that no alias claimed — kept so a rename is
    # VISIBLE in the data rather than silently dropping to zero.
    unmapped = {k: v for k, v in payload.items()
                if k not in taken and isinstance(v, (int, float))
                and not isinstance(v, bool)}
    if unmapped:
        out["unmapped"] = unmapped
    return out


# ── usage from a harness's OWN session state ────────────────────────────
#
# Some CLIs carry no usage on stdout but keep it in the session directory they
# write for `--resume`. kimi is the live case: measured on one Host,
# 34 runs in 30 days reported ZERO tokens through the stream (diagnostics.py
# _probe_usage fires on exactly this), while every one of those runs shipped a
# native-session tar whose agents/main/wire.jsonl holds the vendor's own
# per-turn accounting. The stream really is empty; the telemetry was never
# missing, only unread.
#
# TWO TRAPS, both measured on a real tar before this was written:
#
#   1. EVERY usage OBJECT APPEARS TWICE — once as a root-level
#      {"type":"usage.record", ...} line and once nested inside the following
#      {"event": ...} line. 50 occurrences, 25 distinct. Walking the file for
#      any "usage" key doubles the run. Only root-level usage.record is read.
#
#   2. IT LOOKS CUMULATIVE AND IS NOT. inputCacheRead rises monotonically
#      (18,944 -> 127,744) because each turn re-reads a longer cached prefix.
#      `output` is not monotonic (14,280 -> 393) and last/SUM is 0.006, so
#      these are PER-TURN records (usageScope:"turn") and must be summed.
#      Reading the rise as cumulative and taking `last` would under-report by
#      ~25x; summing a genuinely cumulative emitter would over-report as badly.
#      kimi is therefore absent from _USAGE_AGG, i.e. the default "sum".
_SESSION_USAGE_READERS = {
    # harness id -> (member-name predicate, line->payload extractor)
    "kimi": (
        lambda n: n.endswith("/wire.jsonl") or n == "wire.jsonl",
        lambda o: (o.get("usage")
                   if o.get("type") == "usage.record"
                   and isinstance(o.get("usage"), dict) else None),
    ),
}


# == review._EXPORT_MAX_MEMBERS. A literal, not an import: this module is also
# loaded on its own (scripts/test_session_usage_recovery.py), and
# scripts/test_sidecar_workspace_routes_harness_only.py pins the two equal.
_SESSION_TAR_MAX_MEMBERS = 200_000
# Usage records one session tar may yield. A run writes one per model turn
# (the measured kimi run: 25); past this the rest are ignored, so a tar of
# 64 MiB of one-line records cannot become millions of kept strings.
_SESSION_USAGE_MAX_RECORDS = 50_000
# One line of a session file; a usage record is well under 1 KB.
_SESSION_LINE_MAX = 1 << 20
# Lines read from one session file (a real wire.jsonl is thousands): bounds the
# TIME a file of 60 MiB of newlines can cost, not only the memory.
_SESSION_MAX_LINES = 1_000_000


def usage_frames_from_session_tar(tar_bytes, harness: str) -> list:
    """Vendor usage frames recovered from a harness's native-session tar.

    Returns raw {"kind":"usage","payload":{...}} lines in the shape
    aggregate_usage_events already consumes, so the tar lane and the stream
    lane fold through ONE aggregation rule rather than two. [] when this
    harness has no reader, the tar is unreadable, or it carried no usage —
    never a zero-token frame, same rule as _usage_frame.
    """
    reader = _SESSION_USAGE_READERS.get(str(harness or "").split("-s")[0])
    if not reader or not tar_bytes:
        return []
    want, extract = reader
    out: list = []
    try:
        import io, tarfile
        # "r:" (uncompressed only), a member cap and a record cap, read line
        # by line: the tar is the harness pod's. The sidecar has
        # always written it uncompressed (mode "w"), so a compressed body is
        # refused rather than inflated; the caller spilled it to disk under
        # a cap and pre-scanned its raw headers (review._collect_session), and
        # passes the PATH. Bytes are accepted too (tests). A member is never
        # read whole: `f.read().splitlines()` held it three times over and
        # turned 64 MiB of newlines into 64 million strings.
        opener = ({"fileobj": io.BytesIO(bytes(tar_bytes))}
                  if isinstance(tar_bytes, (bytes, bytearray, memoryview))
                  else {"name": str(tar_bytes)})
        try:
            # the TarInfo that drops tarfile's per-member pax dict (see
            # harness_input.lean_tarinfo); this function is also run lifted
            # out of the module (scripts/test_session_usage_recovery.py),
            # where `server` is not importable
            from server import harness_input as _hin
            _tinfo = _hin.lean_tarinfo()
        except ImportError:
            _tinfo = tarfile.TarInfo
        with tarfile.open(mode="r:", tarinfo=_tinfo, **opener) as tf:
            for i, m in enumerate(tf):
                if i >= _SESSION_TAR_MAX_MEMBERS:
                    raise ValueError(f"more than {_SESSION_TAR_MAX_MEMBERS} members")
                if not m.isfile() or m.issparse() or not want(m.name):
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                for _line_no in range(_SESSION_MAX_LINES):
                    if len(out) >= _SESSION_USAGE_MAX_RECORDS:
                        break
                    raw = f.readline(_SESSION_LINE_MAX)
                    if not raw:
                        break
                    if len(raw) == _SESSION_LINE_MAX and not raw.endswith(b"\n"):
                        # A line longer than any usage record: skip the rest
                        # of it, a bounded piece at a time.
                        while True:
                            more = f.readline(_SESSION_LINE_MAX)
                            if not more or more.endswith(b"\n"):
                                break
                        continue
                    raw = raw.decode("utf-8", "replace").strip()
                    if not raw:
                        continue
                    try:
                        o = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(o, dict):
                        continue
                    payload = extract(o)
                    if isinstance(payload, dict) and payload:
                        out.append(json.dumps({"kind": "usage", "payload": payload}))
    except Exception as exc:                      # a bad tar costs telemetry, never a run
        log.warning("session-usage read failed for %s: %s", harness, exc)
        return []
    return out


def aggregate_usage_events(lines, harness: str = "") -> dict:
    """Fold a variant's events.jsonl into one run-level usage record.

    `lines` is any iterable of raw JSONL strings (or already-decoded dicts).
    Returns {} when the stream carried no usage frame at all — the caller then
    falls back to _parse_usage rather than writing a zero that looks like data.
    """
    # Sampled variants are "<harness>-s2", "<harness>-s3" — same CLI, same
    # emission shape, so they share the base harness's rule.
    base = str(harness or "").split("-s")[0]
    frames: list = []
    for ln in lines or ():
        try:
            ev = ln if isinstance(ln, dict) else json.loads(ln)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict) or ev.get("kind") != "usage":
            continue
        f = _usage_frame(ev.get("payload") or {}, base)
        if f:
            frames.append(f)
    if not frames:
        return {}
    mode = _USAGE_AGG.get(base, "sum")
    keys = ("input_tokens", "output_tokens", "cached_input_tokens",
            "cache_write_tokens", "reasoning_tokens", "total_tokens")
    if mode == "last":
        agg = {k: int(frames[-1].get(k) or 0) for k in keys}
        cost = float(frames[-1].get("cost_usd") or 0.0)
        # PERSIST ONLY THE ADDITIVE SET. A cumulative emitter's earlier frames
        # are each contained in the final one, so storing them would make
        # SUM(total_tokens) over the table disagree with this run total — and
        # the window reads (/api/compute/windows, harness_usage_windows) do
        # exactly that SUM. Two read paths applying different rules to one
        # table is the drift shape this branch exists to kill, so the fix is
        # structural rather than teaching the rule to every reader: what lands
        # in harness_usage_event is always additive, and any SUM anywhere is
        # therefore correct. `frames_seen` keeps the raw count for provenance.
        frames_seen = len(frames)
        frames = [frames[-1]]
    else:
        frames_seen = len(frames)
        agg = {k: sum(int(f.get(k) or 0) for f in frames) for k in keys}
        cost = sum(float(f.get("cost_usd") or 0.0) for f in frames)
    # The vendor's own total when it sent one, else derived. Cache reads are a
    # SUBSET of input on every vendor shape observed, never added on top.
    if not agg["total_tokens"]:
        agg["total_tokens"] = agg["input_tokens"] + agg["output_tokens"]
    agg["cost_usd"] = cost
    agg["frames"] = frames          # the ADDITIVE set — safe to SUM anywhere
    agg["frame_count"] = len(frames)
    agg["frames_seen"] = frames_seen
    agg["agg_mode"] = mode
    return agg


_TOK_RE = re.compile(r"tokens?\s+used[:\s]+([\d,]+)", re.IGNORECASE)


def _parse_usage(run_out: dict) -> tuple[int, str]:
    """(tokens_total, source). Honest provenance: explicit sidecar field > SDK
    usage > parsed transcript (codex prints 'tokens used: 2,921') > unknown."""
    for k in ("tokens_total", "total_tokens", "tokens"):
        v = run_out.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v), "sdk"
    u = run_out.get("usage")
    if isinstance(u, dict):
        t = int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
        if t:
            return t, "sdk"
    # codex prints "tokens used\n2,925" to STDERR, not stdout — and the diff/
    # transcript can be non-empty, so an `or`-chain would stop before stderr.
    # Concatenate every text source so the parse sees the token line wherever
    # the vendor put it.
    txt = "\n".join(
        str(run_out.get(k) or "")
        for k in ("transcript", "transcript_tail", "content", "stderr", "stderr_tail")
    )
    m = _TOK_RE.search(txt)
    if m:
        try:
            return int(m.group(1).replace(",", "")), "parsed"
        except ValueError:
            pass
    return 0, "unknown"


def _cost(compute_s: Optional[float]) -> tuple[int, str]:
    """Our compute fee — the metered, ours-to-charge part. Subscription harnesses
    have no per-token vendor price, so cost = compute_s × rate. 0 ('unpriced')
    until the G3e rate is set; usage is captured regardless."""
    if compute_s and COMPUTE_RATE_UD_PER_S > 0:
        return int(compute_s * COMPUTE_RATE_UD_PER_S), "compute"
    return 0, "unpriced"


async def charge_harness_compute(*, session_id, harness,
                                 compute_s, vcpus=None,
                                 run_id=None) -> tuple[Optional[str], Optional[int]]:
    """Bill OUR compute fee for one completed run through the bridge
    (harness.compute_charge) — the platform billing path this pod's local
    harness_usage table can never reach (the BILLABLES.md gap). The pod mints
    the idempotency key (run_id) ONCE and retries transient failures with the
    same key, so a timeout can never double-charge. The BRIDGE prices the run
    (compute_s × its rate) — nothing here sets a cost.

    `run_id` LETS THE CALLER SUPPLY THAT KEY instead of minting one.
    A freshly-minted key is only idempotent WITHIN one call: two actors billing
    the same delivered window open two rows and charge the customer twice. The
    operator now bills a window that was closed out and abandoned — reading the
    metered-since/metered-until annotations that outlive this process's memory —
    so the release path passes the operator's own `window_id` for that window
    and both charges collapse onto one harness_compute_log row. Omitted (the
    default) keeps the previous behaviour exactly: a per-call uuid.

    Returns (billing_ref, cost_microdollars) on success, (None, None) when the
    charge could not be recorded — logged LOUD; the usage row then carries
    billing_ref NULL, which the billing audit greps for. Best-effort: never
    raises, never blocks dispatch.
    """
    try:
        cs = float(compute_s or 0.0)
    except (TypeError, ValueError):
        cs = 0.0
    if cs <= 0:
        return None, None  # nothing metered → nothing to bill
    from server.applications.local_runtime import in_use as _local_runtime
    if _local_runtime():
        # The owner's own machine ran it (the local runtime): there is no
        # compute fee to charge, and nothing is left unbilled.
        log.info("harness compute on this machine: session=%s harness=%s compute_s=%.1f "
                 "(not billed)", session_id, harness, cs)
        return None, None
    try:
        from server.main import get_bridge_client
        bc = get_bridge_client()
    except Exception:
        bc = None
    if bc is None or not getattr(bc, "_connected", False):
        log.error("harness compute charge SKIPPED (bridge not connected) — "
                  "session=%s harness=%s compute_s=%.1f UNBILLED", session_id, harness, cs)
        return None, None
    # A caller-supplied key must be a non-empty string the bridge will accept
    # (<=128 chars, op_harness_compute_charge); anything else falls back to a
    # fresh uuid rather than failing the charge — an unbilled run is the one
    # outcome never allowed.
    run_id = run_id if isinstance(run_id, str) and 0 < len(run_id) <= 128 else uuid.uuid4().hex
    params = {"run_id": run_id, "compute_s": cs,
              "harness": harness, "session_id": session_id}
    if vcpus:
        params["vcpus"] = int(vcpus)   # rate is per-vCPU-second; bridge prices ×vcpus
    for attempt in (1, 2, 3):
        try:
            env = await bc.request_full("harness.compute_charge", params)
            if env.get("ok"):
                return run_id, (env.get("data") or {}).get("cost_microdollars")
            code = int(env.get("code") or 0)
            if 400 <= code < 500:  # refused, not transient — retrying won't heal it
                log.error("harness compute charge refused (%s): %s — UNBILLED",
                          code, env.get("error"))
                return None, None
            log.warning("harness compute charge attempt %d got %s: %s",
                        attempt, code, env.get("error"))
        except Exception as exc:
            log.warning("harness compute charge attempt %d failed: %s", attempt, exc)
        await asyncio.sleep(attempt)
    log.error("harness compute charge FAILED after retries — session=%s harness=%s "
              "run_id=%s compute_s=%.1f UNBILLED (billing_ref NULL)",
              session_id, harness, run_id, cs)
    return None, None


async def record_harness_usage(pool, *, session_id, project_id, harness,
                               run_out: dict, compute_s=None,
                               billing_ref=None, billed_cost_microdollars=None,
                               phase_id=None) -> None:
    """One harness_usage row at run completion. Best-effort — never blocks dispatch.

    billing_ref / billed_cost_microdollars come from charge_harness_compute: when
    the bridge accepted the charge, the row records the ledger tie-back and the
    ACTUAL priced cost (cost_method 'billed') instead of the local estimate, so
    ComputeView shows what the platform recorded for the monthly bill. A charge
    the bridge accepted without a price keeps the local estimate."""
    if pool is None:
        return
    try:
        # The STREAM is the best source: the vendor's own usage object, already
        # normalized by the pod runtime. _parse_usage stays as the fallback for
        # a run with no stream (buffered path) — its 'sdk'/'parsed'/'unknown'
        # provenance is unchanged, and 'stream' joins it as a fourth value.
        _agg = run_out.get("usage_stream") if isinstance(run_out, dict) else None
        if isinstance(_agg, dict) and _agg.get("total_tokens"):
            tokens, tsrc = int(_agg["total_tokens"]), "stream"
        else:
            tokens, tsrc = _parse_usage(run_out)
        if tsrc == "unknown" and pool is not None and session_id:
            # The harness's own session state may already have been collected
            # for this run (the solo path collects BEFORE recording; the batch
            # path after, which _usage_from_session handles from its side).
            # Without this the run row says 0 tokens while /api/compute/windows
            # reports real ones from the same frames — two readers, one table,
            # opposite answers.
            try:
                row = await pool.fetchrow(
                    "SELECT sum(total_tokens)::bigint AS t FROM harness_usage_event"
                    " WHERE session_id = $1 AND harness = $2", session_id, harness)
                if row and row["t"]:
                    tokens, tsrc = int(row["t"]), "session"
            except Exception:
                pass
        cost, cmethod = _cost(compute_s)
        if billed_cost_microdollars is not None:
            cost, cmethod = int(billed_cost_microdollars), "billed"
        # Vendor-reported run dollars (subscription side) — display-only,
        # never billed by the platform. claude-code's SDK puts them on the
        # run result; every other harness reports them in its STREAM, which
        # aggregate_usage_events already folded into usage_stream.cost_usd
        # (opencode's step_finish `cost`, grok's `end` total_cost_usd). Read
        # only from the result, opencode's $0.329 and grok's $1.976 in one
        # measured run never reached this row. NULL stays NULL: "not reported"
        # is not $0.
        vendor_ud = None
        v = run_out.get("total_cost_usd")
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0) \
                and isinstance(_agg, dict):
            v = _agg.get("cost_usd")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            vendor_ud = int(round(float(v) * 1_000_000))
        eid = None
        try:
            eid = int(os.environ.get("ENTITY_ID", "0")) or None
        except ValueError:
            pass
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO harness_usage
                   (session_id, project_id, harness, model, run_kind, tokens_total,
                    token_source, wall_clock_s, compute_s, cost_microdollars,
                    cost_method, ok, entity_id, billing_ref, vendor_cost_microdollars,
                    phase_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
                session_id, project_id, harness, run_out.get("model"),
                # All fan_out harnesses — incl claude-code, now a peer pod — run
                # via the pod /run path; "run" is the accurate kind.
                run_out.get("run_kind") or "run",
                tokens, tsrc, float(run_out.get("elapsed_seconds") or 0),
                compute_s, cost, cmethod, bool(run_out.get("ok")),
                eid, billing_ref, vendor_ud,
                # NULL for a fan-out or a merge — those have no phase. Set only
                # by the phased build door, which is what makes cost-by-phase
                # answerable (v112).
                int(phase_id) if phase_id is not None else None,
            )
    except Exception as exc:
        log.warning("record_harness_usage failed (%s): %s", harness, exc)


async def record_usage_events(pool, *, session_id, project_id, harness,
                              agg: dict, phase_id=None) -> int:
    """Persist every usage FRAME a run's stream carried. Returns rows written.

    Best-effort and never blocks a build: a failure here costs telemetry, not
    the user's work. The run-level total still lands on harness_usage
    regardless — these rows exist so a wrong per-harness aggregation rule
    (compute._USAGE_AGG) stays correctable from captured data, and so the
    session / 5-hour / weekly quota windows have timestamped frames to read
    instead of a single run total that cannot answer them."""
    if pool is None or not isinstance(agg, dict):
        return 0
    frames = agg.get("frames") or []
    if not frames:
        return 0
    try:
        import json as _json
        rows = 0
        async with pool.acquire() as conn:
            for i, f in enumerate(frames):
                _un = f.get("unmapped")
                await conn.execute(
                    """INSERT INTO harness_usage_event
                       (session_id, harness, project_id, phase_id, seq,
                        input_tokens, output_tokens, cached_input_tokens,
                        cache_write_tokens, reasoning_tokens, total_tokens,
                        cost_usd, unmapped)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)""",
                    session_id, harness, project_id,
                    int(phase_id) if phase_id is not None else None, i,
                    int(f.get("input_tokens") or 0), int(f.get("output_tokens") or 0),
                    int(f.get("cached_input_tokens") or 0),
                    int(f.get("cache_write_tokens") or 0),
                    int(f.get("reasoning_tokens") or 0),
                    int(f.get("total_tokens") or 0),
                    float(f.get("cost_usd") or 0.0),
                    _json.dumps(_un) if _un else None)
                rows += 1
        return rows
    except Exception as exc:
        log.warning("record_usage_events failed (%s): %s", harness, exc)
        return 0


async def link_runs_to_commit(pool, run_ids, commit_sha: str) -> int:
    """Write the save-point commit onto the runs whose files it carried
    (project_git.sync_project_repo, after a successful push). Best-effort,
    idempotent; returns the number of rows stamped. The commit itself carries
    the same link the other way as a `Host-Run:` trailer, so the record is
    readable from git alone (attestation.collect_git)."""
    ids = sorted({int(r) for r in (run_ids or []) if r is not None})
    if pool is None or not ids or not commit_sha:
        return 0
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                """UPDATE harness_run
                      SET metadata = metadata || jsonb_build_object('commit_sha', $2::text)
                    WHERE id = ANY($1::bigint[])""",
                ids, str(commit_sha))
        try:
            return int(res.split()[-1])
        except (ValueError, IndexError):
            return 0
    except Exception as exc:
        log.warning("link_runs_to_commit failed (%s -> %s): %s", ids, commit_sha[:12], exc)
        return 0


async def record_harness_run(pool, *, session_id, project_id, harness,
                             run_out: dict):
    """Persist the FULL harness run (transcript + metadata) to host Postgres so the
    run survives the ephemeral pod — the pod's own session files die at scale-to-0,
    and manifest.json keeps only a 2000-char tail. Complements record_harness_usage
    (metrics only). Best-effort — never blocks dispatch. Nothing here judges: it
    persists everything; the deterministic trim sweep (curated_at IS NULL) later
    keeps high-value runs and archives the junk (persist-all).

    Returns the new run id (the number the user refers to — "#628"), or None
    when nothing was recorded, so the caller can carry it to the save-point
    commit (the commit↔run link)."""
    if pool is None:
        return None
    try:
        transcript = (run_out.get("transcript_full") or run_out.get("transcript")
                      or run_out.get("content") or "")
        meta = {"run_kind": run_out.get("run_kind") or "run"}
        stderr_tail = run_out.get("stderr_tail") or run_out.get("stderr")
        if stderr_tail:
            meta["stderr_tail"] = str(stderr_tail)[-2000:]
        if run_out.get("vcpus"):
            meta["vcpus"] = run_out["vcpus"]   # P5: the compute-size dimension
        if run_out.get("retries"):
            # The pod re-ran the CLI after a transient provider connection
            # failure (harness_core RUN_RETRY).
            # Persisted so recurrence is countable:
            # metadata->>'retries' across rows measures how often the
            # transient strikes even when the retry saves the run.
            meta["retries"] = int(run_out["retries"])
            if run_out.get("first_attempt"):
                meta["first_attempt"] = run_out["first_attempt"]
        diff_bytes = int(run_out["diff_bytes"]
                         if run_out.get("diff_bytes") is not None
                         else len(run_out.get("diff") or ""))
        async with pool.acquire() as conn:
            rid = await conn.fetchval(
                """INSERT INTO harness_run
                   (session_id, project_id, harness, model, ok, exit_code, error,
                    transcript, diff_bytes, elapsed_seconds, compute_s, metadata)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
                   RETURNING id""",
                session_id, project_id, harness, run_out.get("model"),
                bool(run_out.get("ok")), run_out.get("exit_code"),
                run_out.get("error"), transcript, diff_bytes,
                float(run_out.get("elapsed_seconds") or 0),
                run_out.get("compute_s"), json.dumps(meta),
            )
            return int(rid) if rid is not None else None
    except Exception as exc:
        log.warning("record_harness_run failed (%s): %s", harness, exc)
        return None


# ── Workbench P4: deterministic harness_run curation ─────────────────────
# Persist-ALL, then trim: every run lands in harness_run in full;
# this sweep later keeps the high-value rows and strips the junk's
# bulk. DETERMINISTIC by doctrine — thresholds and joins only, no LLM anywhere
# (modeled on memory_consolidation._decay_old + the turn_consolidator queue:
# curated_at IS NULL is the work queue, stamped once, idempotent, bounded).
#
# Value signal = the human decision, nothing else: a run whose variant the user
# COMMITTED (harness_usage.committed, set by mark_committed at pick/ship time)
# keeps its full transcript and gains importance; everything else archives —
# transcript stripped (the PVC manifest keeps a 2000-char tail for forensics),
# metrics row intact for ComputeView/audit. 24h grace before a row is eligible:
# picks and merges happen within a working day; after that an unpicked run's
# bulk is junk by definition.
CURATE_GRACE_HOURS = float(os.environ.get("HARNESS_RUN_CURATE_GRACE_H", "24"))
CURATE_BATCH = 200   # bounded per pass, same discipline as turn_consolidator


async def curate_harness_runs(pool, *, grace_hours: float = None,
                              batch: int = CURATE_BATCH) -> dict:
    """One curation pass. Atomic single statement: pick the oldest uncurated
    rows past the grace window, mark committed-variant rows kept (importance
    up, transcript retained) and the rest archived (transcript stripped,
    importance floored). Returns {kept, archived}. Safe to re-run — a row is
    curated exactly once (curated_at stamp)."""
    if pool is None:
        return {"kept": 0, "archived": 0}
    g = CURATE_GRACE_HOURS if grace_hours is None else float(grace_hours)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH batch AS (
                SELECT hr.id,
                       EXISTS (SELECT 1 FROM harness_usage hu
                                WHERE hu.session_id = hr.session_id
                                  AND hu.harness    = hr.harness
                                  AND hu.committed) AS kept
                  FROM harness_run hr
                 WHERE hr.curated_at IS NULL
                   AND hr.created_at < now() - make_interval(secs => $1)
                 ORDER BY hr.created_at ASC
                 LIMIT $2
            )
            UPDATE harness_run hr
               SET curated_at = now(),
                   status     = CASE WHEN b.kept THEN 'active' ELSE 'archived' END,
                   importance = CASE WHEN b.kept
                                     THEN GREATEST(hr.importance, 1.5)
                                     ELSE 0.1 END,
                   transcript = CASE WHEN b.kept THEN hr.transcript ELSE NULL END
              FROM batch b
             WHERE hr.id = b.id
            RETURNING b.kept, hr.session_id, hr.harness
            """,
            g * 3600.0, batch,
        )
    kept = sum(1 for r in rows if r["kept"])
    out = {"kept": kept, "archived": len(rows) - kept}
    # Reclaim the BUILT OUTPUT of runs nobody picked. A run's
    # worktree is run-scoped and permanent (review.py::_prepare_worktree) — that
    # is what makes "open app" on run #43 still show run #43 — so something has
    # to bound the growth, and "was it ever picked?" is the same signal this
    # sweep already uses for transcripts. A PICKED run keeps its files
    # indefinitely: it is the one the user chose to build on.
    #
    # Deliberately after the UPDATE and best-effort per row: a filesystem
    # problem must never roll back or block the curation itself, and the row
    # survives regardless, so the RUN NUMBER keeps resolving either way — only
    # the openable output goes.
    out["worktrees_reclaimed"] = 0
    for r in rows:
        if r["kept"] or not r["session_id"] or not r["harness"]:
            continue
        try:
            wt = (SESSIONS_ROOT / str(r["session_id"]) / "work" / str(r["harness"]))
            # Confine to the sessions tree: session_id/harness come from the DB,
            # and a rmtree driven by a stored string gets a containment check,
            # not trust.
            wt_real = wt.resolve()
            if SESSIONS_ROOT.resolve() not in wt_real.parents:
                log.warning("curation: refusing to reclaim out-of-tree path %s", wt_real)
                continue
            if wt_real.is_dir():
                shutil.rmtree(wt_real, ignore_errors=True)
                out["worktrees_reclaimed"] += 1
        except Exception as exc:
            log.warning("curation: worktree reclaim failed for run %s/%s: %s",
                        r["session_id"], r["harness"], exc)
    if rows:
        log.info("harness_run curation: kept %d, archived %d, worktrees reclaimed %d",
                 out["kept"], out["archived"], out["worktrees_reclaimed"])
    return out


async def mark_committed(pool, session_id: str, picks: dict) -> None:
    """picks = {path: harness} that the user SHIPPED — the accept signal behind
    cost-per-accepted-commit. Best-effort."""
    if pool is None or not picks:
        return
    from collections import Counter
    counts = Counter(picks.values())
    try:
        async with pool.acquire() as conn:
            for harness, n in counts.items():
                await conn.execute(
                    """UPDATE harness_usage
                       SET committed = true, committed_files = committed_files + $3
                       WHERE session_id = $1 AND harness = $2""",
                    session_id, harness, int(n),
                )
    except Exception as exc:
        log.warning("mark_committed failed: %s", exc)


def _window_days(window: str) -> int:
    return {"7d": 7, "30d": 30, "90d": 90, "all": 36500}.get(window, 30)


def _vendor_cost(vendor_ud, reported_runs, runs) -> dict:
    """The vendor-reported dollars of a summary row: a number when at least
    one run reported one, else null and "not reported" — never a $0.00 that
    was not measured (vendor_cost_microdollars is NULL when no cost came).
    The sum covers only the runs that reported, so the label says how many of
    the row's runs that is, as the windows route does for usage frames; a sum
    over 1 of 10 runs is not the window's vendor charge."""
    n, total = int(reported_runs or 0), int(runs or 0)
    if vendor_ud is None:
        return {"vendor_cost_usd": None, "vendor_cost_runs": 0,
                "vendor_cost": ("not reported — no run in this window reported a vendor "
                                "cost, so the charge is unknown (not $0)")}
    label = f"${int(vendor_ud) / 1_000_000:.4f} reported by the vendor on {n} of {total} runs"
    if n < total:
        label += " — the other runs reported none, so this covers only those"
    return {"vendor_cost_usd": round(int(vendor_ud) / 1_000_000, 4),
            "vendor_cost_runs": n, "vendor_cost": label}


@router.get("/summary", dependencies=[Depends(requires_permission("read:inference_log"))])
async def summary(window: str = Query("30d")):
    """The leaderboard: per harness — runs, acceptance rate, cost, and the
    headline cost-per-accepted-commit."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"harnesses": [], "totals": {}}
    d = _window_days(window)
    rows = await pool.fetch(
        f"""SELECT harness, count(*) AS runs,
                   count(*) FILTER (WHERE committed) AS accepted,
                   coalesce(sum(tokens_total),0) AS tokens,
                   coalesce(sum(compute_s),0) AS compute_s,
                   coalesce(sum(cost_microdollars),0) AS cost_ud,
                   -- NOT coalesced: NULL when no run reported a vendor cost
                   -- (the windows route's rule) — "not reported", never $0.
                   sum(vendor_cost_microdollars) AS vendor_ud,
                   count(vendor_cost_microdollars) AS vendor_runs,
                   coalesce(sum(committed_files),0) AS committed_files
              FROM harness_usage
             WHERE created_at >= now() - INTERVAL '{d} days'
             GROUP BY harness ORDER BY accepted DESC, runs DESC""")
    out = []
    for r in rows:
        runs, acc, cost_usd = r["runs"], r["accepted"], int(r["cost_ud"]) / 1_000_000
        out.append({
            "harness": r["harness"], "runs": runs, "accepted": acc,
            "acceptance_rate": round(acc / runs, 3) if runs else 0.0,
            "tokens": int(r["tokens"]), "compute_s": round(float(r["compute_s"]), 1),
            "committed_files": int(r["committed_files"]),
            "cost_usd": round(cost_usd, 4),
            **_vendor_cost(r["vendor_ud"], r["vendor_runs"], runs),
            "cost_per_accepted_usd": round(cost_usd / acc, 4) if acc else None,
        })
    tot = await pool.fetchrow(
        f"""SELECT count(*) runs, count(*) FILTER (WHERE committed) accepted,
                   coalesce(sum(tokens_total),0) tokens,
                   coalesce(sum(cost_microdollars),0) cost_ud,
                   sum(vendor_cost_microdollars) vendor_ud,
                   count(vendor_cost_microdollars) vendor_runs
              FROM harness_usage WHERE created_at >= now() - INTERVAL '{d} days'""")
    return {
        "window": window,
        "harnesses": out,
        "totals": {"runs": tot["runs"], "accepted": tot["accepted"],
                   "tokens": int(tot["tokens"]), "cost_usd": round(int(tot["cost_ud"]) / 1_000_000, 4),
                   **_vendor_cost(tot["vendor_ud"], tot["vendor_runs"], tot["runs"])},
        "methodology": ("tokens: parsed (codex) / sdk (claude-code) / unknown (others); "
                        "cost = our metered compute fee (rate TBD), not a vendor invoice; "
                        "vendor_cost = the vendor's own reported dollars (claude-code SDK "
                        "total_cost_usd) — subscription-side, display-only, never billed"),
    }


@router.get("/trends", dependencies=[Depends(requires_permission("read:inference_log"))])
async def trends(window: str = Query("30d")):
    pool = brain_service.get_pool()
    if pool is None:
        return {"series": []}
    d = _window_days(window)
    rows = await pool.fetch(
        f"""SELECT date_trunc('day', created_at) AS day, harness,
                   count(*) runs, coalesce(sum(tokens_total),0) tokens,
                   coalesce(sum(cost_microdollars),0) cost_ud
              FROM harness_usage WHERE created_at >= now() - INTERVAL '{d} days'
             GROUP BY day, harness ORDER BY day""")
    return {"window": window, "series": [
        {"day": r["day"].date().isoformat(), "harness": r["harness"], "runs": r["runs"],
         "tokens": int(r["tokens"]), "cost_usd": round(int(r["cost_ud"]) / 1_000_000, 4)}
        for r in rows]}


@router.get("/sessions", dependencies=[Depends(requires_permission("read:inference_log"))])
async def recent_sessions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """The demo: per fan_out, every harness's usage side-by-side + which shipped.
    `offset` pages past the first `limit` sessions."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"sessions": []}
    rows = await pool.fetch(
        """SELECT session_id, harness, model, tokens_total, compute_s,
                  cost_microdollars, committed, committed_files, ok, created_at
             FROM harness_usage
            WHERE session_id IN (
                SELECT session_id FROM harness_usage WHERE session_id IS NOT NULL
                GROUP BY session_id ORDER BY max(created_at) DESC, session_id
                LIMIT $1 OFFSET $2)
            ORDER BY created_at DESC""", limit, offset)
    sess: dict = {}
    for r in rows:
        s = sess.setdefault(r["session_id"], {"session_id": r["session_id"], "harnesses": []})
        s["harnesses"].append({
            "harness": r["harness"], "model": r["model"], "tokens": int(r["tokens_total"]),
            "compute_s": round(float(r["compute_s"] or 0), 1),
            "cost_usd": round(int(r["cost_microdollars"]) / 1_000_000, 4),
            "committed": r["committed"], "committed_files": r["committed_files"], "ok": r["ok"]})
    return {"sessions": list(sess.values()), "offset": offset,
            "next_offset": (offset + len(sess)) if len(sess) == limit else None}


# ── Harness RANKING — cross-harness market research, ranked by ACCEPTANCE ────────
# Privatae sits between the user and EVERY harness, so it can answer the question
# no single vendor can: across all of them, WHICH harness's output does the user
# actually keep? Acceptance is the only signal here — we NEVER judge or rank by a
# model's opinion of code quality. Two acceptance signals, both human decisions:
#   • base pick  — the harness the user chose as the merge base / the compare pick
#                  (review.py /pick {harness} and /merge {base})
#   • graft      — a per-element bit the user pulled FROM a harness into the merge
#                  (review.py /merge element_picks {element: harness})
# Sources: harness_usage table (runs/accepted, project linkage, window timestamp)
# + /var/lib/cseries/harness-preferences.jsonl (the pick/merge decision records).
_PREFS_PATH = Path("/var/lib/cseries/harness-preferences.jsonl")


def _read_preference_records() -> list[dict]:
    """Parse harness-preferences.jsonl into decision records. Tolerant of partial
    last lines (the file is appended to live by review.py). Two record shapes:
      pick  : {session_id, harness, note, prompt, at}            (human compare pick)
      merge : {session_id, base, graft_from, preference,
               element_picks, spec, commit_message, merge_ok}    (adaptive merge round)
    """
    if not _PREFS_PATH.exists():
        return []
    recs: list[dict] = []
    try:
        with _PREFS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # partial/torn line from a concurrent append
    except OSError as exc:
        log.warning("read preferences.jsonl failed: %s", exc)
    return recs


def _empty_rank() -> dict:
    return {"runs": 0, "accepted": 0, "base_picks": 0,
            "graft_contributions": 0, "acceptance_rate": 0.0}


def _rank_from(usage_rows, pref_recs, session_filter: Optional[set]) -> list[dict]:
    """Build per-harness ranking from usage rows + preference records.

    session_filter (or None for global) scopes the jsonl decisions to the same
    sessions the usage rows cover, so a per-project view counts only that
    project's picks/grafts. runs/accepted come from harness_usage (the accept
    column is already flipped by mark_committed for both pick and merge paths);
    base_picks/graft_contributions are counted from the jsonl decision records.
    """
    by: dict[str, dict] = {}

    def slot(h: str) -> dict:
        if h not in by:
            by[h] = {"harness": h, **_empty_rank()}
        return by[h]

    for r in usage_rows:
        # A compare's later runs are metered as <harness>-sN; this ranking is
        # per HARNESS, and the pick log records the base harness, so the run
        # label folds to it (a variant number means nothing across sessions).
        s = slot(re.sub(r"-s(?:[2-9]|1[0-6])$", "", str(r["harness"])))
        s["runs"] += int(r["runs"])
        s["accepted"] += int(r["accepted"])

    for rec in pref_recs:
        if session_filter is not None and rec.get("session_id") not in session_filter:
            continue
        # base pick — human chose this harness as the winner/base.
        base = rec.get("harness") or rec.get("base")
        if base:
            slot(str(base))["base_picks"] += 1
        # graft contributions — per-element bits pulled FROM a harness into the merge.
        for h in (rec.get("element_picks") or {}).values():
            if h:
                slot(str(h))["graft_contributions"] += 1

    out = list(by.values())
    for s in out:
        s["acceptance_rate"] = (round(s["accepted"] / s["runs"], 3)
                                if s["runs"] else 0.0)
    # Rank by the acceptance signal: accepted runs, then base picks, then grafts.
    out.sort(key=lambda s: (s["accepted"], s["base_picks"],
                            s["graft_contributions"], s["runs"]), reverse=True)
    return out


@router.get("/ranking", dependencies=[Depends(requires_permission("read:inference_log"))])
async def ranking(window: str = Query("30d"), project_id: Optional[int] = Query(None)):
    """Cross-harness market research, ranked by ACCEPTANCE — never by a model's
    opinion of code quality. Per harness: {runs, accepted, base_picks,
    graft_contributions, acceptance_rate}. Always returns the GLOBAL ranking; when
    project_id is supplied, also returns the per-project ranking scoped to that
    project's sessions. Honest provenance: acceptance comes from human decisions
    (the compare pick + the merge base/graft), not from any model judging output.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"window": window, "global": [], "project": None}
    d = _window_days(window)

    # Global usage (runs/accepted) within the window.
    g_rows = await pool.fetch(
        f"""SELECT harness, count(*) AS runs,
                   count(*) FILTER (WHERE committed) AS accepted
              FROM harness_usage
             WHERE created_at >= now() - INTERVAL '{d} days'
             GROUP BY harness""")
    # Sessions in-window (any project) → scopes jsonl decisions to the window.
    g_sess = await pool.fetch(
        f"""SELECT DISTINCT session_id FROM harness_usage
             WHERE session_id IS NOT NULL
               AND created_at >= now() - INTERVAL '{d} days'""")
    global_sessions = {r["session_id"] for r in g_sess}

    pref_recs = _read_preference_records()
    out = {
        "window": window,
        "global": _rank_from(g_rows, pref_recs, global_sessions),
        "project": None,
        "project_id": project_id,
        "methodology": ("ranked by ACCEPTANCE (human decisions: compare pick + "
                        "merge base/graft), NEVER by a model judging code quality; "
                        "runs/accepted from harness_usage, base_picks/grafts from "
                        "harness-preferences.jsonl"),
    }

    if project_id is not None:
        p_rows = await pool.fetch(
            f"""SELECT harness, count(*) AS runs,
                       count(*) FILTER (WHERE committed) AS accepted
                  FROM harness_usage
                 WHERE project_id = $1
                   AND created_at >= now() - INTERVAL '{d} days'
                 GROUP BY harness""", project_id)
        p_sess = await pool.fetch(
            f"""SELECT DISTINCT session_id FROM harness_usage
                 WHERE session_id IS NOT NULL AND project_id = $1
                   AND created_at >= now() - INTERVAL '{d} days'""", project_id)
        project_sessions = {r["session_id"] for r in p_sess}
        out["project"] = _rank_from(p_rows, pref_recs, project_sessions)

    return out


# ═════════════════════════════════════════════════════════════════════════════
# Compute analytics — the full cost-and-usage surface behind the
# rebuilt Compute app. Read-only. Three money lanes, kept STRUCTURALLY separate
# in every payload (collapsing them is the failure mode):
#
#   billed    — Privatae billables. The only real money: harness_usage rows with
#               cost_method='billed' record the amount the bridge charge actually
#               accepted (record_harness_usage above; the bridge prices the run
#               and records it in harness_compute_log, which the platform rolls
#               up into the monthly invoice).
#   estimate  — vendor/inference spend. The vendor bill is never visible to the
#               platform; vendor_cost_microdollars (the vendor's own reported
#               dollars) and inference_cost_log.est_cost_usd are ESTIMATES and
#               every payload labels them so.
#   usage     — resource metering that isn't money: compute seconds, tokens,
#               cache reads (entity_memory turn rows' context_ledger), bytes.
#               Some of it is metered-and-charged (harness compute), some is
#               metered platform-side and not exposed to this pod (egress), and
#               some is not metered at all (storage) — /billables says which,
#               per row, instead of inventing a number.
#
# Error contract (401/bridge-down must never render as "Loading…" or as
# legitimately-empty): these endpoints raise 503 when the pool isn't ready
# — an empty 200 is reserved for a genuinely empty dataset.
# ═════════════════════════════════════════════════════════════════════════════


def _require_pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain not initialized — analytics unavailable")
    return pool


# Fixed fragments shared by the rollups. cost_method vocabulary (this file):
#   'billed'   — the bridge accepted harness.compute_charge; the row carries the
#                ACTUAL priced amount (record_harness_usage).
#   'compute'  — local estimate (compute_s × local rate) — the charge lane did
#                not price it. Estimated Privatae fee, labeled as such.
#   'unpriced' — no rate set; usage captured, cost 0 by construction.
_HU_MONEY_COLS = """
       count(*)                                    AS runs,
       count(*) FILTER (WHERE ok)                  AS ok_runs,
       count(*) FILTER (WHERE committed)           AS accepted,
       coalesce(sum(committed_files), 0)           AS committed_files,
       -- TOKENS: summed only over runs that actually carry a count. A run with
       -- token_source='unknown' stores tokens_total=0 at ingest, so including
       -- it did not merely dilute the total — it made "no data" arithmetically
       -- indistinguishable from "zero tokens". Measured on one Host: 58 of 73
       -- runs, and every kimi/cursor/opencode run, reported a confident 0.
       -- NULL here (no run had a count) reaches the UI and renders "—";
       -- tok_unknown below says how much of the window the number covers.
       sum(tokens_total) FILTER (WHERE token_source <> 'unknown')
                                                   AS tokens,
       coalesce(sum(compute_s), 0)                 AS compute_s,
       coalesce(sum(wall_clock_s), 0)              AS wall_clock_s,
       coalesce(sum(cost_microdollars)
                FILTER (WHERE cost_method = 'billed'), 0)  AS billed_ud,
       coalesce(sum(cost_microdollars)
                FILTER (WHERE cost_method = 'compute'), 0) AS est_compute_ud,
       -- VENDOR: deliberately NOT coalesced. vendor_cost_microdollars is NULL
       -- whenever the harness reported no vendor cost — on one Host that was
       -- 100% of 73 runs — and coalescing it to 0 published a "$0.00
       -- vendor-reported" card, a daily chart line pinned to zero, and three
       -- table columns of $0.00, none of which was ever measured.
       sum(vendor_cost_microdollars)               AS vendor_ud,
       -- How many runs that sum covers: the rest reported none, so a sum
       -- over 1 of 10 runs is not the group's vendor spend.
       count(vendor_cost_microdollars)             AS vendor_runs,
       count(*) FILTER (WHERE compute_s > 0
                          AND billing_ref IS NULL) AS unbilled_runs,
       count(*) FILTER (WHERE token_source = 'sdk')     AS tok_sdk,
       count(*) FILTER (WHERE token_source = 'parsed')  AS tok_parsed,
       -- 'stream' was missing here, so its runs fell out of the provenance
       -- breakdown entirely: codex reported {sdk:0, parsed:0, unknown:9}
       -- against runs=11, and the two stream runs (carrying real token counts)
       -- simply vanished. Measured live 2026-09-09: sdk 10, stream 5,
       -- parsed 0, unknown 58 — 'parsed' is dead vocabulary today, kept
       -- because a row could still carry it.
       count(*) FILTER (WHERE token_source = 'stream')  AS tok_stream,
       -- 'session' = recovered from the harness's own session state after the
       -- fact (usage_frames_from_session_tar). Counted here or the
       -- token_sources breakdown stops summing to runs, which is the exact
       -- defect adding 'stream' fixed.
       count(*) FILTER (WHERE token_source = 'session') AS tok_session,
       count(*) FILTER (WHERE token_source = 'unknown') AS tok_unknown,
       count(*) FILTER (WHERE cost_method = 'billed')   AS cm_billed,
       count(*) FILTER (WHERE cost_method = 'compute')  AS cm_estimated,
       count(*) FILTER (WHERE cost_method = 'unpriced') AS cm_unpriced
"""

# Chat-lane usage: assistant turn rows persist a context_ledger
# (metadata.context_ledger.per_call = [{in, cached, out}, …] — session.py
# _note_served) into entity_memory via write_turn_row. Tokens only, never
# dollars: the platform holds no rate card for the user's own providers.
#
# `in` ALREADY CONTAINS `cached`. _note_served (session.py:3567) fills it from
# `input_total_tokens`, which session.py:3538-3543 defines as the lane-
# NORMALIZED true context size per call: the gateways have already reconciled
# the two wire conventions (OpenAI-shaped counts the cached prefix inside
# prompt_tokens, Anthropic-shaped does not) before it reaches this ledger.
# So `cached` is a SUBSET of `in`, never a sibling addend:
#   cache hit rate = cached / in        ← correct
#   cache hit rate = cached / (in+cached)  ← double-counts, halves the answer
# Measured on one Host (77 turns, 235 calls): the wrong form
# reported 45.6% against a true 83.9%. Verified over the same 235 calls,
# `cached > in` occurs 0 times. Do not sum these two columns for a total,
# either — `in` IS the total.
_CHAT_LANE_FROM = """
  FROM entity_memory em
  CROSS JOIN LATERAL jsonb_array_elements(
        em.metadata -> 'context_ledger' -> 'per_call') AS pc
 WHERE em.memory_type = 'turn'
   AND em.metadata ? 'context_ledger'
   AND em.entity_id = $1
"""


def _money_row(r: dict) -> dict:
    """Serialize one _HU_MONEY_COLS row: microdollars → USD, lane-tagged."""
    runs, accepted = int(r["runs"]), int(r["accepted"])
    billed = int(r["billed_ud"])
    est = int(r["est_compute_ud"])
    privatae_ud = billed + est
    return {
        "runs": runs,
        "ok_runs": int(r["ok_runs"]),
        "accepted": accepted,
        "acceptance_rate": round(accepted / runs, 3) if runs else 0.0,
        "committed_files": int(r["committed_files"]),
        # None when NO run in this group carried a token count. int() on it
        # would recreate the confident zero the SQL just stopped producing.
        "tokens": None if r["tokens"] is None else int(r["tokens"]),
        # How much of the window `tokens` actually covers. A number computed
        # from 2 of 20 runs is not wrong, but it is not the window's total
        # either, and the reader has no way to tell without this.
        # runs MINUS unknown, not a sum of the known labels: the sum form
        # already undercounted once by omitting 'stream', and it would again
        # the next time the vocabulary gains a value. This matches exactly what
        # the tokens filter above includes.
        "tokens_covered_runs": runs - int(r["tok_unknown"]),
        "compute_s": round(float(r["compute_s"]), 1),
        "wall_clock_s": round(float(r["wall_clock_s"]), 1),
        # Lane: billed (real) vs estimated (our fee, uncharged) — never merged
        # without the split riding along.
        "billed_usd": round(billed / 1_000_000, 4),
        "est_compute_usd": round(est / 1_000_000, 4),
        "privatae_usd": round(privatae_ud / 1_000_000, 4),
        "cost_per_accepted_usd": (round(privatae_ud / accepted / 1_000_000, 4)
                                  if accepted and privatae_ud else None),
        # Lane: vendor estimate (subscription-side, never billed by Privatae).
        # None = the harness reported no vendor cost for ANY run here. Distinct
        # from 0.0, which would mean it reported and the answer was nothing.
        "vendor_est_usd": (None if r["vendor_ud"] is None
                           else round(int(r["vendor_ud"]) / 1_000_000, 4)),
        # The runs vendor_est_usd covers, and the same in words: with fewer
        # than `runs` it is the reported runs' dollars, not the group's.
        "vendor_est_runs": int(r["vendor_runs"]),
        "vendor_est": _vendor_cost(r["vendor_ud"], r["vendor_runs"], runs)["vendor_cost"],
        # Confidence provenance — surfaced, never silently averaged over.
        "unbilled_runs": int(r["unbilled_runs"]),
        "token_sources": {"sdk": int(r["tok_sdk"]), "parsed": int(r["tok_parsed"]),
                          "stream": int(r["tok_stream"]),
                          "session": int(r["tok_session"]),
                          "unknown": int(r["tok_unknown"])},
        "cost_methods": {"billed": int(r["cm_billed"]),
                         "estimated": int(r["cm_estimated"]),
                         "unpriced": int(r["cm_unpriced"])},
    }


# The row for a project that has CHAT turns but no harness runs.
#
# Built from one explicit key list rather than a hand-copied literal at the
# call site, because the literal broke exactly as you would expect: adding
# tok_stream to _HU_MONEY_COLS left the copy behind, and /projects — the
# DEFAULT TAB — raised KeyError for every chat-only project. One Host had five
# of them, so the app's opening screen was an error card. Keep this list beside
# _HU_MONEY_COLS: anything counted or summed there needs a zero here.
# DERIVED FROM THE SQL, never restated. A second hand-maintained list is what
# broke /projects: tok_stream was added to _HU_MONEY_COLS, the copy was not
# updated, and the default tab raised KeyError for every chat-only project.
# Replacing the copy with a cross-check would only have narrowed the window —
# measured, a regex over `AS (tok_|cm_)` sees 7 of the 18 aliases, so a future
# `sum(...) AS foo_ud` reproduces the same KeyError with the gate green.
# Deriving removes the drift instead of policing it.
_HU_NULL_KEYS = frozenset({"tokens", "vendor_ud"})
_HU_ZERO_KEYS = tuple(k for k in re.findall(r"\bAS (\w+)", _HU_MONEY_COLS)
                      if k not in _HU_NULL_KEYS)


def _no_harness_row() -> dict:
    """Serialized money row for a project with no harness runs in the window.

    tokens and vendor are None rather than 0, for the same reason the rest of
    this change exists: no harness ran, so nothing measured them. Zeroing them
    put "$0.00 vendor-reported" back on the headline card (it sums only rows
    that reported, and these would have counted as reporting) and printed
    "0 tokens" under the tooltip "every run in this project reported a token
    count" — on projects that had no runs at all.
    """
    row: dict = {k: 0 for k in _HU_ZERO_KEYS}
    row.update({k: None for k in _HU_NULL_KEYS})
    return _money_row(row)


# What each money lane of a project row means, in the words the /projects tab
# shows. One copy: by_project returns it, and so does project_run_usage for
# the Workbench page, so a figure never reaches either with other words.
PROJECT_LANES = {
    "billed_usd": "Privatae billable — the amount the platform actually charged",
    "est_compute_usd": "Privatae compute fee estimated locally — the charge lane did not price these runs",
    "vendor_est_usd": "ESTIMATE — the vendor's own reported dollars; the vendor bill is never visible to Privatae and this is never billed",
    "chat": "usage only (tokens + cache) — no rate card exists for the user's own providers, so no dollars are invented",
}


@router.get("/projects", dependencies=[Depends(requires_permission("read:inference_log"))])
async def by_project(window: str = Query("30d")):
    """Per-project rollup — the headline view: what has this project cost?

    Per project: the Privatae-billable lane (billed + estimated compute fee),
    the vendor-estimate lane (vendor-reported run dollars), resource usage
    (runs/tokens/compute/wall-clock), and the chat lane (host turns + token/
    cache usage from the turn rows' context ledger — usage only, no dollars).
    """
    pool = _require_pool()
    d = _window_days(window)
    hu_rows = await pool.fetch(
        f"""SELECT hu.project_id, p.name AS project_name, {_HU_MONEY_COLS}
              FROM harness_usage hu
              LEFT JOIN projects p ON p.id = hu.project_id
             WHERE hu.created_at >= now() - INTERVAL '{d} days'
             GROUP BY hu.project_id, p.name""")
    chat_rows = await pool.fetch(
        f"""SELECT em.project_id,
                   count(DISTINCT em.id) AS turns,
                   count(*) AS calls,
                   coalesce(sum((pc->>'in')::bigint), 0)     AS tokens_in,
                   coalesce(sum((pc->>'cached')::bigint), 0) AS tokens_cached,
                   coalesce(sum((pc->>'out')::bigint), 0)    AS tokens_out
            {_CHAT_LANE_FROM}
               AND em.created_at >= now() - INTERVAL '{d} days'
             GROUP BY em.project_id""", brain_service._entity_id)
    # Project names for chat-only projects (the hu_rows join covers the rest).
    chat_only_pids = [int(r["project_id"]) for r in chat_rows
                      if r["project_id"] is not None
                      and not any(h["project_id"] == r["project_id"] for h in hu_rows)]
    name_by_pid: dict = {}
    if chat_only_pids:
        for nr in await pool.fetch(
                "SELECT id, name FROM projects WHERE id = ANY($1::int[])",
                chat_only_pids):
            name_by_pid[nr["id"]] = nr["name"]
    by_pid: dict = {}
    for r in hu_rows:
        pid = r["project_id"]
        by_pid[pid] = {
            "project_id": pid,
            "project_name": r["project_name"] or ("(no project)" if pid is None
                                                  else f"project {pid}"),
            **_money_row(dict(r)),
            "chat": None,
        }
    for r in chat_rows:
        pid = r["project_id"]
        chat = {
            "turns": int(r["turns"]), "calls": int(r["calls"]),
            "tokens_in": int(r["tokens_in"]),
            "tokens_cached": int(r["tokens_cached"]),
            "tokens_out": int(r["tokens_out"]),
            # cached / in, NOT cached / (in + cached) — see _CHAT_LANE_FROM.
            "cache_hit_rate": (round(int(r["tokens_cached"]) / int(r["tokens_in"]), 3)
                               if int(r["tokens_in"]) else 0.0),
        }
        if pid in by_pid:
            by_pid[pid]["chat"] = chat
        else:
            by_pid[pid] = {
                "project_id": pid,
                "project_name": name_by_pid.get(pid) or ("(no project)" if pid is None
                                                         else f"project {pid}"),
                **_no_harness_row(),
                "chat": chat,
            }
    projects = sorted(by_pid.values(),
                      # `or 0.0`: vendor_est_usd is now None wherever nothing
                      # reported one, and Python refuses to order None against
                      # a float. Without this, any tie on privatae_usd between
                      # a reporting and a non-reporting project is a TypeError
                      # — and a fee of 0 is the documented state when the
                      # charge lane does not price a run.
                      key=lambda p: (p["privatae_usd"],
                                     p["vendor_est_usd"] or 0.0, p["runs"]),
                      reverse=True)
    return {
        "window": window,
        "projects": projects,
        "lanes": dict(PROJECT_LANES),
    }


async def project_run_usage(pool, project_id: int) -> dict:
    """One project's coding-engine runs, every one it has had: the row the
    /projects tab shows for it (by_project), from the same harness_usage sum
    (_HU_MONEY_COLS) and the same serialization (_money_row), with no window.
    Nothing is priced here. Of its dollar lanes only billed_usd is a charge;
    est_compute_usd and vendor_est_usd are estimates, and `lanes` says so in
    the tab's own words."""
    row = await pool.fetchrow(
        f"SELECT {_HU_MONEY_COLS} FROM harness_usage WHERE project_id = $1", int(project_id))
    out = _money_row(dict(row))
    out["lanes"] = {k: PROJECT_LANES[k] for k in ("billed_usd", "est_compute_usd", "vendor_est_usd")}
    return out


@router.get("/harnesses", dependencies=[Depends(requires_permission("read:inference_log"))])
async def by_harness(window: str = Query("30d")):
    """Per-harness and per-(harness, model) rollups: runs, ok/failed, tokens,
    billed compute, estimated vendor cost, median + p95 wall-clock, and cost
    per accepted run (committed = the human acceptance signal)."""
    pool = _require_pool()
    d = _window_days(window)
    pct = """
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY wall_clock_s) AS med_wall_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY wall_clock_s) AS p95_wall_s
    """
    h_rows = await pool.fetch(
        f"""SELECT harness, {_HU_MONEY_COLS}, {pct}
              FROM harness_usage
             WHERE created_at >= now() - INTERVAL '{d} days'
             GROUP BY harness""")
    hm_rows = await pool.fetch(
        f"""SELECT harness, model, {_HU_MONEY_COLS}, {pct}
              FROM harness_usage
             WHERE created_at >= now() - INTERVAL '{d} days'
             GROUP BY harness, model""")

    def _ser(rows, with_model=False):
        out = []
        for r in rows:
            row = {"harness": r["harness"], **_money_row(dict(r)),
                   "med_wall_s": round(float(r["med_wall_s"] or 0), 1),
                   "p95_wall_s": round(float(r["p95_wall_s"] or 0), 1)}
            if with_model:
                row["model"] = r["model"]
            out.append(row)
        out.sort(key=lambda x: (x["accepted"], x["runs"]), reverse=True)
        return out

    return {"window": window,
            "harnesses": _ser(h_rows),
            "harness_models": _ser(hm_rows, with_model=True),
            "methodology": (
                "cost_per_accepted = (billed + estimated Privatae compute fee) "
                "/ accepted runs; committed=true is the acceptance signal "
                "(mark_committed at pick/ship time). vendor_est_usd is the "
                "vendor's own reported dollars — an estimate, never billed. "
                "token_sources / cost_methods give per-row confidence; rows "
                "with unbilled_runs > 0 metered compute whose charge never "
                "succeeded (billing_ref NULL — an auditable defect state).")}


@router.get("/billables", dependencies=[Depends(requires_permission("read:inference_log"))])
async def billables(window: str = Query("30d")):
    """Every Privatae billable + every metered resource, one row each: its
    meter, its rate, whether it is charged or visibility-only, and its value
    in this window — or an HONEST not-metered / not-exposed state. No row
    here ever invents a number."""
    pool = _require_pool()
    d = _window_days(window)
    hc = await pool.fetchrow(
        f"""SELECT {_HU_MONEY_COLS}
              FROM harness_usage
             WHERE created_at >= now() - INTERVAL '{d} days'""")
    m = _money_row(dict(hc))
    rows = [
        {
            "id": "harness_compute",
            "name": "Harness compute (vCPU-seconds)",
            "lane": "billed",
            "charged": True,
            "meter": "per-run pod compute time × vCPUs, priced by the platform "
                     "at charge time (rate is platform-side; each billed row "
                     "records the exact amount; billed on the monthly "
                     "invoice)",
            "status": "live",
            "billed_usd": m["billed_usd"],
            "est_compute_usd": m["est_compute_usd"],
            "compute_s": m["compute_s"],
            "billed_runs": m["cost_methods"]["billed"],
            "unpriced_runs": m["cost_methods"]["unpriced"],
            "unbilled_runs": m["unbilled_runs"],
            "note": ("unbilled_runs = runs with metered compute whose charge "
                     "never reached the ledger (billing_ref NULL) — a defect "
                     "state, shown so it can be audited, never hidden"),
        },
        {
            "id": "egress_public",
            "name": "Public-site egress",
            "lane": "billed",
            "charged": True,
            "meter": "bytes served to the internet by published sites; "
                     "$5/TB after 1 TB included per site subscription",
            "status": "not_exposed",
            "note": ("metered platform-side per site and month; not yet "
                     "readable from this Host — no bridge lane exposes it. "
                     "Shown here so the billable is never invisible; the "
                     "number will appear when the lane exists."),
        },
        {
            "id": "egress_host",
            "name": "Host container egress",
            "lane": "usage",
            "charged": False,
            "meter": "per-container network bytes, daily",
            "status": "not_exposed",
            "note": ("metered platform-side for cost visibility only — never "
                     "charged. Not yet readable from this Host."),
        },
        {
            "id": "storage",
            "name": "Storage",
            "lane": "usage",
            "charged": False,
            "meter": None,
            "status": "not_metered",
            "note": ("your plan includes a fixed storage allocation; actual "
                     "usage is not metered anywhere yet, so no number is "
                     "shown rather than a guessed one"),
        },
        {
            "id": "inference",
            "name": "Inference (your own providers)",
            "lane": "estimate",
            "charged": False,
            "meter": "per-call token usage + estimated cost, container-local",
            "status": "live",
            "note": ("Privatae bills compute only — inference is bring-your-own "
                     "and passes through. Everything under the Inference view "
                     "is an ESTIMATE: the vendor bill is never visible."),
        },
    ]
    return {"window": window, "billables": rows}


@router.get("/runs", dependencies=[Depends(requires_permission("read:inference_log"))])
async def runs(window: str = Query("30d"),
               project_id: Optional[int] = Query(None),
               harness: Optional[str] = Query(None),
               limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)):
    """The drill-down list: project → harness → run. harness_run rows joined
    to their harness_usage metrics (same session + harness, nearest in time)."""
    pool = _require_pool()
    d = _window_days(window)
    conds, args = [], []
    if project_id is not None:
        args.append(project_id)
        conds.append(f"AND hr.project_id = ${len(args)}")
    if harness:
        args.append(harness)
        conds.append(f"AND hr.harness = ${len(args)}")
    args.append(limit)
    lim = f"${len(args)}"
    args.append(offset)
    off = f"${len(args)}"
    rows = await pool.fetch(
        f"""SELECT hr.id, hr.session_id, hr.project_id, p.name AS project_name,
                   hr.harness, hr.model, hr.ok, hr.exit_code, hr.status,
                   (hr.error IS NOT NULL AND hr.error <> '') AS has_error,
                   hr.diff_bytes, hr.elapsed_seconds, hr.compute_s,
                   hr.metadata->>'vcpus'   AS vcpus,
                   hr.metadata->>'retries' AS retries,
                   hr.created_at,
                   u.tokens_total, u.token_source, u.cost_microdollars,
                   u.cost_method, u.vendor_cost_microdollars, u.committed,
                   u.billing_ref
              FROM harness_run hr
              LEFT JOIN projects p ON p.id = hr.project_id
              LEFT JOIN LATERAL (
                    SELECT hu.* FROM harness_usage hu
                     WHERE hu.session_id = hr.session_id
                       AND hu.harness = hr.harness
                     ORDER BY abs(extract(epoch FROM hu.created_at - hr.created_at))
                     LIMIT 1) u ON true
             WHERE hr.created_at >= now() - INTERVAL '{d} days'
                   {' '.join(conds)}
             ORDER BY hr.created_at DESC
             LIMIT {lim} OFFSET {off}""", *args)
    out = []
    for r in rows:
        cm = r["cost_method"]
        cost_ud = int(r["cost_microdollars"] or 0)
        out.append({
            "id": r["id"], "session_id": r["session_id"],
            "project_id": r["project_id"], "project_name": r["project_name"],
            "harness": r["harness"], "model": r["model"],
            "ok": r["ok"], "exit_code": r["exit_code"], "status": r["status"],
            "has_error": r["has_error"], "diff_bytes": int(r["diff_bytes"] or 0),
            "elapsed_s": round(float(r["elapsed_seconds"] or 0), 1),
            "compute_s": round(float(r["compute_s"] or 0), 1),
            "vcpus": r["vcpus"], "retries": r["retries"],
            "created_at": r["created_at"].isoformat(),
            # Per-run, the same rule as the rollups: a run whose token_source
            # is 'unknown' stores 0, so `or 0` published "not captured" as a
            # measured zero on a fourth table column and in the detail panel.
            "tokens": (None if r["token_source"] == "unknown"
                       or r["tokens_total"] is None else int(r["tokens_total"])),
            "token_source": r["token_source"],
            "billed_usd": round(cost_ud / 1_000_000, 4) if cm == "billed" else 0.0,
            "est_compute_usd": round(cost_ud / 1_000_000, 4) if cm == "compute" else 0.0,
            "cost_method": cm,
            "vendor_est_usd": (None if r["vendor_cost_microdollars"] is None else
                               round(int(r["vendor_cost_microdollars"]) / 1_000_000, 4)),
            "committed": bool(r["committed"]),
            "billing_ref": r["billing_ref"],
            "unbilled": bool((r["compute_s"] or 0) > 0 and r["billing_ref"] is None
                             and r["tokens_total"] is not None),
        })
    return {"window": window, "runs": out, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}", dependencies=[Depends(requires_permission("read:inference_log"))])
async def run_detail(run_id: int):
    """One run, in full (transcript tail capped server-side — full transcripts
    can be megabytes and curation may have stripped them)."""
    pool = _require_pool()
    r = await pool.fetchrow(
        """SELECT hr.id, hr.session_id, hr.project_id, p.name AS project_name,
                  hr.harness, hr.model, hr.ok, hr.exit_code, hr.error,
                  hr.status, hr.importance, hr.diff_bytes, hr.elapsed_seconds,
                  hr.compute_s, hr.metadata, hr.created_at, hr.curated_at,
                  right(hr.transcript, 8000) AS transcript_tail,
                  coalesce(length(hr.transcript), 0) AS transcript_chars,
                  u.tokens_total, u.token_source, u.cost_microdollars,
                  u.cost_method, u.vendor_cost_microdollars, u.committed,
                  u.committed_files, u.billing_ref, u.wall_clock_s
             FROM harness_run hr
             LEFT JOIN projects p ON p.id = hr.project_id
             LEFT JOIN LATERAL (
                   SELECT hu.* FROM harness_usage hu
                    WHERE hu.session_id = hr.session_id
                      AND hu.harness = hr.harness
                    ORDER BY abs(extract(epoch FROM hu.created_at - hr.created_at))
                    LIMIT 1) u ON true
            WHERE hr.id = $1""", run_id)
    if r is None:
        raise HTTPException(404, f"run {run_id} not found")
    meta = r["metadata"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    cm = r["cost_method"]
    cost_ud = int(r["cost_microdollars"] or 0)
    return {
        "id": r["id"], "session_id": r["session_id"],
        "project_id": r["project_id"], "project_name": r["project_name"],
        "harness": r["harness"], "model": r["model"], "ok": r["ok"],
        "exit_code": r["exit_code"], "error": r["error"], "status": r["status"],
        "diff_bytes": int(r["diff_bytes"] or 0),
        "elapsed_s": round(float(r["elapsed_seconds"] or 0), 1),
        "compute_s": round(float(r["compute_s"] or 0), 1),
        "metadata": meta or {},
        "created_at": r["created_at"].isoformat(),
        "curated_at": r["curated_at"].isoformat() if r["curated_at"] else None,
        "transcript_tail": r["transcript_tail"],
        "transcript_chars": int(r["transcript_chars"]),
        "transcript_stripped": (int(r["transcript_chars"]) == 0
                                and r["curated_at"] is not None),
        "usage": None if r["tokens_total"] is None and r["cost_method"] is None else {
            # Per-run, the same rule as the rollups: a run whose token_source
            # is 'unknown' stores 0, so `or 0` published "not captured" as a
            # measured zero on a fourth table column and in the detail panel.
            "tokens": (None if r["token_source"] == "unknown"
                       or r["tokens_total"] is None else int(r["tokens_total"])),
            "token_source": r["token_source"],
            "wall_clock_s": round(float(r["wall_clock_s"] or 0), 1),
            "billed_usd": round(cost_ud / 1_000_000, 4) if cm == "billed" else 0.0,
            "est_compute_usd": round(cost_ud / 1_000_000, 4) if cm == "compute" else 0.0,
            "cost_method": cm,
            "vendor_est_usd": (None if r["vendor_cost_microdollars"] is None else
                               round(int(r["vendor_cost_microdollars"]) / 1_000_000, 4)),
            "committed": bool(r["committed"]),
            "committed_files": int(r["committed_files"] or 0),
            "billing_ref": r["billing_ref"],
        },
    }


@router.get("/inference", dependencies=[Depends(requires_permission("read:inference_log"))])
async def inference_estimates(window: str = Query("30d")):
    """The ESTIMATE lane, windowed: per-module and per-model estimated
    inference cost (inference_cost_log — container-local, explicitly not the
    platform billing ledger) plus the chat lane's token + cache usage from the
    turn rows' context ledger. Cache hit rate is first-class: without it a
    140K-context turn reads as '4 tokens in'.

    THE TWO LANES ARE SEPARATE SOURCES AND ARE NEVER SUMMED. by_module/by_model
    read inference_cost_log; chat_by_model reads the turn rows' context ledger
    (_CHAT_LANE_FROM). tokens_in means the same thing in both — the TRUE TOTAL
    input, with the cached count a SUBSET of it (app.yaml 0.2.2) — and the
    estimate lane reports that subset too, from
    inference_cost_log's own cache_read_tokens column rather than leaving the
    reader to infer it from the chat lane. est_cost_usd got SMALLER on that
    date for every cache-hitting model while no token count moved: cached
    tokens are now priced at the vendor's published cache rate instead of its
    input rate (server/routes/inference.py:estimate_call_cost_detail)."""
    pool = _require_pool()
    d = _window_days(window)
    by_module = await pool.fetch(
        f"""SELECT source_module, count(*)::bigint AS calls,
                   coalesce(sum(tokens_in), 0)::bigint  AS tokens_in,
                   coalesce(sum(tokens_out), 0)::bigint AS tokens_out,
                   -- A SUBSET of tokens_in, never added to it — the same rule
                   -- the chat lane follows (app.yaml 0.2.2). Columns since
                   -- migrations/v148; they are why the estimate below is no
                   -- longer the whole input priced at the input rate.
                   coalesce(sum(cache_read_tokens), 0)::bigint  AS tokens_cached,
                   coalesce(sum(cache_write_tokens), 0)::bigint AS tokens_cache_write,
                   coalesce(sum(est_cost_usd), 0)::float8 AS est_cost_usd,
                   -- The three states cost_method already distinguishes, kept
                   -- apart instead of summed into one $0.00. See the note on
                   -- _price_coverage below.
                   count(*) FILTER (WHERE cost_method = 'list')::bigint
                       AS priced_calls,
                   count(*) FILTER (WHERE cost_method = 'subscription')::bigint
                       AS subscription_calls,
                   count(*) FILTER (WHERE cost_method = 'unpriced')::bigint
                       AS unpriced_calls,
                   coalesce(sum(tokens_in + tokens_out)
                       FILTER (WHERE cost_method = 'unpriced'), 0)::bigint
                       AS unpriced_tokens
              FROM inference_cost_log
             WHERE created_at >= now() - INTERVAL '{d} days'
               AND purpose IS DISTINCT FROM 'inference_error'
             GROUP BY source_module ORDER BY est_cost_usd DESC, calls DESC""")
    by_model = await pool.fetch(
        f"""SELECT model_id, count(*)::bigint AS calls,
                   coalesce(sum(tokens_in), 0)::bigint  AS tokens_in,
                   coalesce(sum(tokens_out), 0)::bigint AS tokens_out,
                   -- A SUBSET of tokens_in, never added to it — the same rule
                   -- the chat lane follows (app.yaml 0.2.2). Columns since
                   -- migrations/v148; they are why the estimate below is no
                   -- longer the whole input priced at the input rate.
                   coalesce(sum(cache_read_tokens), 0)::bigint  AS tokens_cached,
                   coalesce(sum(cache_write_tokens), 0)::bigint AS tokens_cache_write,
                   coalesce(sum(est_cost_usd), 0)::float8 AS est_cost_usd,
                   -- The three states cost_method already distinguishes, kept
                   -- apart instead of summed into one $0.00. See the note on
                   -- _price_coverage below.
                   count(*) FILTER (WHERE cost_method = 'list')::bigint
                       AS priced_calls,
                   count(*) FILTER (WHERE cost_method = 'subscription')::bigint
                       AS subscription_calls,
                   count(*) FILTER (WHERE cost_method = 'unpriced')::bigint
                       AS unpriced_calls,
                   coalesce(sum(tokens_in + tokens_out)
                       FILTER (WHERE cost_method = 'unpriced'), 0)::bigint
                       AS unpriced_tokens
              FROM inference_cost_log
             WHERE created_at >= now() - INTERVAL '{d} days'
               AND purpose IS DISTINCT FROM 'inference_error'
             GROUP BY model_id ORDER BY est_cost_usd DESC, calls DESC""")
    chat = await pool.fetch(
        f"""SELECT em.metadata->>'model' AS model,
                   count(DISTINCT em.id) AS turns,
                   count(*) AS calls,
                   coalesce(sum((pc->>'in')::bigint), 0)     AS tokens_in,
                   coalesce(sum((pc->>'cached')::bigint), 0) AS tokens_cached,
                   coalesce(sum((pc->>'out')::bigint), 0)    AS tokens_out
            {_CHAT_LANE_FROM}
               AND em.created_at >= now() - INTERVAL '{d} days'
             GROUP BY em.metadata->>'model'
             ORDER BY tokens_in DESC""", brain_service._entity_id)
    chat_out = []
    for r in chat:
        t_in, t_cached = int(r["tokens_in"]), int(r["tokens_cached"])
        chat_out.append({
            "model": r["model"] or "(unknown)",
            "turns": int(r["turns"]), "calls": int(r["calls"]),
            "tokens_in": t_in, "tokens_cached": t_cached,
            "tokens_out": int(r["tokens_out"]),
            # cached / in, NOT cached / (in + cached) — see _CHAT_LANE_FROM.
            "cache_hit_rate": (round(t_cached / t_in, 3) if t_in else 0.0),
        })
    models = [dict(r) for r in by_model]
    # PRICE COVERAGE — what fraction of this window the headline actually
    # priced. Without it the "Estimated inference" total reads as the window's
    # spend when it is only the priced slice: measured on one Host, 22 of 1,148
    # calls carried a list price and 1,078 calls / 20.6M tokens were 'unpriced'
    # yet rendered as an unremarkable $0.00 per row. A reader cannot distinguish
    # "this model is free" from "we have no rate card for this model", and those
    # are opposite facts.
    #
    # 'subscription' is deliberately its own bucket, not folded into unpriced:
    # a flat-plan call genuinely has no marginal cost, so $0.00 IS its honest
    # answer — the only one of the three for which zero is a measurement.
    coverage = {
        "priced_calls": sum(int(m["priced_calls"]) for m in models),
        "subscription_calls": sum(int(m["subscription_calls"]) for m in models),
        "unpriced_calls": sum(int(m["unpriced_calls"]) for m in models),
        "unpriced_tokens": sum(int(m["unpriced_tokens"]) for m in models),
    }
    coverage["total_calls"] = (coverage["priced_calls"]
                               + coverage["subscription_calls"]
                               + coverage["unpriced_calls"])
    return {
        "window": window,
        "by_module": [dict(r) for r in by_module],
        "by_model": models,
        "chat_by_model": chat_out,
        "price_coverage": coverage,
        "disclaimer": ("ESTIMATES ONLY. The vendor bill is never visible to "
                       "Privatae — these figures come from per-call token "
                       "counts and vendor-reported run costs, and are never "
                       "billed. Chat-lane rows are usage only (no dollars): "
                       "no rate card exists for your own providers."),
    }


@router.get("/timeseries", dependencies=[Depends(requires_permission("read:inference_log"))])
async def timeseries(window: str = Query("30d")):
    """Daily series, one array per lane — the lanes stay separate all the way
    to the wire so no chart can accidentally merge billed money with estimates."""
    pool = _require_pool()
    d = _window_days(window)
    hu = await pool.fetch(
        f"""SELECT date_trunc('day', created_at)::date AS day,
                   count(*) AS runs,
                   -- Same not-measured-is-not-zero rule as _HU_MONEY_COLS. A
                   -- day with no reported vendor cost must come back NULL so
                   -- the chart omits it, rather than 0 so the chart draws a
                   -- confident flat line along the axis.
                   sum(tokens_total) FILTER (WHERE token_source <> 'unknown')
                       AS tokens,
                   coalesce(sum(compute_s), 0) AS compute_s,
                   coalesce(sum(cost_microdollars)
                            FILTER (WHERE cost_method = 'billed'), 0)  AS billed_ud,
                   coalesce(sum(cost_microdollars)
                            FILTER (WHERE cost_method = 'compute'), 0) AS est_compute_ud,
                   sum(vendor_cost_microdollars) AS vendor_ud,
                   -- The day's runs that sum covers (the rest reported none).
                   count(vendor_cost_microdollars) AS vendor_runs
              FROM harness_usage
             WHERE created_at >= now() - INTERVAL '{d} days'
             GROUP BY 1 ORDER BY 1""")
    infer = await pool.fetch(
        f"""SELECT date_trunc('day', created_at)::date AS day,
                   count(*) AS calls,
                   coalesce(sum(est_cost_usd), 0)::float8 AS est_cost_usd
              FROM inference_cost_log
             WHERE created_at >= now() - INTERVAL '{d} days'
               AND purpose IS DISTINCT FROM 'inference_error'
             GROUP BY 1 ORDER BY 1""")
    chat = await pool.fetch(
        f"""SELECT date_trunc('day', em.created_at)::date AS day,
                   count(DISTINCT em.id) AS turns,
                   coalesce(sum((pc->>'in')::bigint), 0)     AS tokens_in,
                   coalesce(sum((pc->>'cached')::bigint), 0) AS tokens_cached
            {_CHAT_LANE_FROM}
               AND em.created_at >= now() - INTERVAL '{d} days'
             GROUP BY 1 ORDER BY 1""", brain_service._entity_id)
    return {
        "window": window,
        "harness_daily": [
            {"day": r["day"].isoformat(), "runs": int(r["runs"]),
             "tokens": None if r["tokens"] is None else int(r["tokens"]),
             "compute_s": round(float(r["compute_s"]), 1),
             "billed_usd": round(int(r["billed_ud"]) / 1_000_000, 4),
             "est_compute_usd": round(int(r["est_compute_ud"]) / 1_000_000, 4),
             "vendor_est_usd": (None if r["vendor_ud"] is None
                                else round(int(r["vendor_ud"]) / 1_000_000, 4)),
             # A day where only some runs reported is those runs' dollars.
             "vendor_est_runs": int(r["vendor_runs"]),
             "vendor_est": _vendor_cost(r["vendor_ud"], r["vendor_runs"], r["runs"])["vendor_cost"]}
            for r in hu],
        "inference_daily": [
            {"day": r["day"].isoformat(), "calls": int(r["calls"]),
             "est_cost_usd": round(float(r["est_cost_usd"]), 4)}
            for r in infer],
        "chat_daily": [
            {"day": r["day"].isoformat(), "turns": int(r["turns"]),
             "tokens_in": int(r["tokens_in"]),
             "tokens_cached": int(r["tokens_cached"])}
            for r in chat],
    }


# ── subscription usage windows ───────────────────────────────────────────────
# Session / 5-hour / weekly harness usage is tracked SEPARATELY, not as a
# usage metric — i.e. as consumption against a subscription's refresh
# windows rather than as another throughput chart.
#
# WHAT THIS HONESTLY IS, and is not. It reports MEASURED CONSUMPTION in a
# trailing window. It does NOT report quota remaining, and deliberately does
# not pretend to: that needs two things the platform does not have — the
# vendor's quota SIZE for the user's plan, and the vendor's reset BOUNDARY
# (fixed-start or rolling, and from when). Neither is published on any surface
# we read, and inventing a denominator would turn a real number into a
# confident wrong one. `quota_basis: "unknown"` says so in the payload rather
# than in a comment nobody sees.
#
# 5h is the window the subscription lanes actually refresh on (seen
# on a Host's own Kimi usage). 24h and 7d bracket it.
_USAGE_WINDOWS = (("session", None), ("5h", 5 * 3600),
                  ("24h", 24 * 3600), ("7d", 7 * 24 * 3600))


@router.get("/windows", dependencies=[Depends(requires_permission("read:inference_log"))])
async def usage_windows(session_id: Optional[str] = Query(None),
                        harness: Optional[str] = Query(None)):
    """Per-harness token consumption over the subscription refresh windows.

    Reads harness_usage_event — the per-frame usage captured from each run's
    own event stream (v114). A run total cannot answer "how much of this
    5-hour window have I spent"; timestamped frames can, which is why the
    frames are stored individually rather than folded away.

    The `session` window needs a session_id and is omitted without one."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "Brain not initialized")

    out = []
    for key, seconds in _USAGE_WINDOWS:
        if key == "session" and not session_id:
            continue
        clauses, params = ["1=1"], []
        if seconds is not None:
            params.append(seconds)
            clauses.append(
                f"created_at > now() - make_interval(secs => ${len(params)})")
        else:
            params.append(session_id)
            clauses.append(f"session_id = ${len(params)}")
        if harness:
            params.append(harness)
            # Fan-out variants persist as "<harness>-s2"/"-s3" (the variant
            # label is what keys a run), so an exact match on "kimi"
            # silently drops its own sampled frames. Match the base id or
            # any of its variants — the same base-id rule _USAGE_AGG uses.
            clauses.append(
                f"(harness = ${len(params)} OR harness LIKE ${len(params)} || '-s%')")
        try:
            rows = await pool.fetch(
                f"""SELECT harness,
                           COUNT(*)                        AS frames,
                           COUNT(DISTINCT session_id)      AS sessions,
                           COALESCE(SUM(total_tokens),0)   AS tokens,
                           COALESCE(SUM(input_tokens),0)   AS input_tokens,
                           COALESCE(SUM(output_tokens),0)  AS output_tokens,
                           COALESCE(SUM(cached_input_tokens),0) AS cached_tokens,
                           COALESCE(SUM(reasoning_tokens),0)    AS reasoning_tokens,
                           ROUND(COALESCE(SUM(cost_usd),0)::numeric, 6) AS vendor_cost_usd,
                           COUNT(*) FILTER (WHERE cost_usd > 0) AS cost_frames,
                           MIN(created_at) AS first_at,
                           MAX(created_at) AS last_at
                      FROM harness_usage_event
                     WHERE {' AND '.join(clauses)}
                     GROUP BY harness
                     ORDER BY tokens DESC""", *params)
        except Exception as exc:
            log.warning("usage window %s failed: %s", key, exc)
            rows = []
        items = []
        for r in rows:
            tok = int(r["tokens"])
            cached = int(r["cached_tokens"])
            items.append({
                "harness": r["harness"],
                "frames": int(r["frames"]),
                "sessions": int(r["sessions"]),
                "tokens": tok,
                "input_tokens": int(r["input_tokens"]),
                "output_tokens": int(r["output_tokens"]),
                "cached_tokens": cached,
                "reasoning_tokens": int(r["reasoning_tokens"]),
                # Cache reads are a SUBSET of input on every vendor shape
                # observed, so this is the share of input served from cache —
                # the cheap part of a subscription's spend.
                "cached_share": (round(cached / int(r["input_tokens"]), 4)
                                 if int(r["input_tokens"]) else None),
                # Vendor-REPORTED dollars only (opencode sends its own cost).
                # The estimate lane; never a Privatae charge. cost_usd is NOT
                # NULL DEFAULT 0 (v114) and a frame stores 0 when the vendor
                # sent no cost (_usage_frame keeps only a cost > 0), so a sum
                # of 0 over frames none of which carried one is "not
                # reported" — null, never $0.00. The Host's
                # harness_usage_windows tool reads the table the same way.
                "vendor_cost_usd": (float(r["vendor_cost_usd"] or 0)
                                    if int(r["cost_frames"] or 0) > 0 else None),
                "vendor_cost": (f"${float(r['vendor_cost_usd'] or 0):.4f} reported by the "
                                f"vendor on {int(r['cost_frames'])} of {int(r['frames'])} "
                                "usage frames"
                                if int(r["cost_frames"] or 0) > 0 else
                                "not reported — this harness's stream carried no cost, "
                                "so the charge is unknown (not $0)"),
                "first_at": r["first_at"].isoformat() if r["first_at"] else None,
                "last_at": r["last_at"].isoformat() if r["last_at"] else None,
            })
        out.append({
            "window": key,
            "seconds": seconds,
            "harnesses": items,
            "tokens": sum(i["tokens"] for i in items),
        })
    return {
        "windows": out,
        "session_id": session_id,
        # Say what the numbers ARE. Consumption measured from the runs'
        # own streams — not quota remaining, which would need the vendor's
        # plan size and reset boundary. Neither is readable from here.
        "basis": "measured consumption from each run's event stream",
        "quota_basis": "unknown — vendor plan size and reset boundary are not published to the platform",
    }
