"""
Run watchdog — deterministic behavior analysis for coding-harness / agent runs
(the Ladder's monitoring half).

Rules over counters, never a model judging a model: parse a run transcript
into (tool, normalized-args) events and flag the failure shapes the host
must kill or annotate — endless thinking (stall), looping (identical calls),
flailing (churn without progress). Thresholds are research-anchored defaults
(AutoGen caps consecutive auto-replies at 10, LangGraph's default recursion
limit is 25, Claude Code's bash timeout is 120s default / 600s max); we sit
lower because a rung-kill is cheap — the next rung picks up.

Post-run only: this reads a FINISHED run's transcript (harness_run is the
durable record) to classify why it failed. It stops nothing. A live run is
judged by the harness pod's own liveness watchdog (harness_core: no output,
no CPU, no data moving), never by thresholds here. The live detector that
once sat at the end of this file (progress_verdict, with its stall/flail
constants) never had a caller and was removed, so those numbers cannot be
wired in as a time limit later.
"""

import json
import logging

log = logging.getLogger("privatae.pc.run_watchdog")

# ── Thresholds (research-anchored defaults; tuned from the run corpus,
#    never asked of the user) ─────────────────────────────────────────
LOOP_WARN = 3          # consecutive identical (tool, args) calls → annotate
LOOP_KILL = 5          # → classify a finished, failed run as 'loop'


def _normalize_args(obj) -> str:
    """Canonical form of a tool call's input so identical calls hash equal."""
    try:
        return json.dumps(obj, sort_keys=True, default=str)[:2000]
    except (TypeError, ValueError):
        return str(obj)[:2000]


def _tool_events(transcript: str):
    """Extract (tool_name, normalized_args) in document order from a run
    transcript. Harness CLIs emit different shapes (claude JSON array,
    stream-JSON lines, nested message dicts), so this walks whatever JSON
    parses and collects every {"type": "tool_use", "name": ...} block.
    Returns None when nothing parses — caller reports parsed=False rather
    than guessing.
    """
    roots = []
    text = (transcript or "").strip()
    if not text:
        return None
    try:
        roots = [json.loads(text)]
    except (json.JSONDecodeError, ValueError):
        for line in text.splitlines():          # JSONL fallback
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                roots.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    if not roots:
        return None
    events = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name"):
                events.append((str(node["name"]), _normalize_args(node.get("input"))))
                continue                        # a tool_use block has no nested calls
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return events


def analyze_transcript(transcript: str) -> dict:
    """Deterministic loop/churn stats for one finished run. Degrades honest:
    an unparseable transcript yields parsed=False and no verdict — never a
    guess."""
    events = _tool_events(transcript)
    if events is None:
        return {"parsed": False, "tool_calls": 0, "distinct_calls": 0,
                "max_consec_identical": 0, "top_repeat": None, "verdict": None}
    max_run, cur_run, prev = 0, 0, None
    top_repeat = None
    for ev in events:
        cur_run = cur_run + 1 if ev == prev else 1
        if cur_run > max_run:
            max_run, top_repeat = cur_run, ev[0]
        prev = ev
    verdict = None
    if max_run >= LOOP_KILL:
        verdict = "loop"
    elif max_run >= LOOP_WARN:
        verdict = "loop-suspect"
    return {
        "parsed": True,
        "tool_calls": len(events),
        "distinct_calls": len(set(events)),
        "max_consec_identical": max_run,
        "top_repeat": top_repeat,
        "verdict": verdict,
    }


# What stopped a run, read from the words the stopping code writes (the pod's
# watchdog in harness_core, the claude-code harness, the kernel's stream
# backstop). Ordered: the first match names the stop.
_STOP_REASONS = (
    ("made no progress", "no-progress"),          # progress backstop: busy, nothing written
    ("waited on the model", "model-hung"),        # one model call never answered
    ("showed no sign of life", "stuck"),          # liveness stop
    ("stopped as stuck", "stuck"),
    ("live stream died", "stream-lost"),          # kernel lost the pod's feed
    ("exceeded", "timed-out"),                    # an older image's total cap
    ("timed out after", "timed-out"),
)


def classify_failure(per_state: dict, stats: dict | None,
                     max_minutes: int | None = None) -> str:
    """One evidence-grade reason for a failed variant, from what actually
    stopped it: credential | loop | no-progress | model-hung | stuck |
    stream-lost | timed-out | error. per_state = the session's per-harness row.

    Never from elapsed time. This used to call any failure at 0.95 x
    max_minutes or later "stalled-at-cap" and hand that to the next rung,
    from when max_minutes was a total cap on a run; it no longer is (a run
    has no time limit), so a long run that failed for its own reason was
    mislabelled and a real liveness stop was labelled plain "error".
    `max_minutes` is accepted for old callers and not read."""
    if (per_state.get("error_type") or "") == "credential_invalid":
        return "credential"
    if stats and stats.get("verdict") == "loop":
        return "loop"
    err = (per_state.get("error") or "").lower()
    for needle, reason in _STOP_REASONS:
        if needle in err:
            return reason
    return "error"
