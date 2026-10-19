"""mastercode.runtime.run_failure — WHY a harness run failed, as a kind.

The bench's track record (coding_bench → harness_run) counted every `ok=false`
row as one failure of that (harness, model). A run on claude-code /
claude-fable-5, "the live stream died mid-run after 2
frames" — a transport loss on the pod stream — was scored against the model,
so the bench would pick against Fable for a problem Fable never saw.

failure_kind() reads the run's own error text (and exit code) and names the
kind. It is deliberately lexical over the strings the platform itself writes
(review.py, ladder.py, the harness runtime) plus the vendor CLI errors seen
on real runs; an unrecognized failure is `error`, never a guess.

Kinds, and whether they say anything about the MODEL:
  transport   the stream/bridge/operator/network died — not the model
  credential  the harness was not signed in / key invalid — not the model
  capacity    no memory / plan room / 402 at wake — not the model
  stalled     no output for the cap, exceeded the cap, timed out — ambiguous
  harness     the CLI exited non-zero with no usable output — ambiguous
  error       anything else (the model's own failure, or unclassified)
MODEL_FAILURE_KINDS is the set the bench may hold against a model.
"""
from __future__ import annotations

import re

# ANCHORED patterns, never bare substrings. The first cut matched "cap",
# "401", "sign in", "insufficient" anywhere — so "Cannot find module
# 'capitalize'", "redesign index.html", "line 1401" and "insufficient
# arguments" all read as not-the-model, the inverse of the bug this module
# fixes (the probes are in the gate). Each
# pattern below is either the platform's own phrasing (review.py, ladder.py,
# the harness runtime, tools.py) or a vendor error seen verbatim on
# real runs, bounded so a word inside the model's own output cannot
# trip it.
TRANSPORT_PATTERNS = (
    r"\blive stream died\b", r"\bstream died\b", r"\bserver disconnected\b",
    r"\boperator unreachable\b", r"\bbridge not connected\b", r"\bbridge call failed\b",
    r"\bprovider\.connection_err\b", r"\bconnection (?:error|reset|refused)\b",
    r"\breqwest error\b", r"\beconnreset\b", r"\bsocket hang up\b",
    r"\bharness wake failed\b", r"\bfailed to run prompt\b",
)
CREDENTIAL_PATTERNS = (
    r"\bnot connected\b.{0,40}\bsign in\b", r"\bsign in on your own surface\b",
    r"\bcredential(?:s)? (?:missing|expired|invalid|refused)\b", r"\bcredential_invalid\b",
    r"\binvalid api key\b", r"\bapi key (?:missing|invalid|expired|not set)\b",
    r"\b(?:http|status|code)\s*401\b", r"\(401\)", r"\b401 unauthorized\b",
)
CAPACITY_PATTERNS = (
    r"\bwaiting for memory\b", r"\bno room\b", r"\bplan has room\b",
    r"\b(?:http|status|code)\s*402\b", r"\(402\)", r"\bpayment required\b",
    r"\binsufficient (?:credits?|balance|funds|quota)\b", r"\bquota exceeded\b",
    r"\brate.?limit(?:ed)?\b", r"\b(?:http|status|code)\s*429\b", r"\(429\)",
)
STALL_PATTERNS = (
    r"\bno output for \d+", r"\bstopped as stuck\b", r"\bexceeded \d+\s*min cap\b",
    r"\bexceeded .{0,30}\bcap\b", r"\btimed out\b", r"\btimeout\b",
)

_TRANSPORT = [re.compile(p) for p in TRANSPORT_PATTERNS]
_CREDENTIAL = [re.compile(p) for p in CREDENTIAL_PATTERNS]
_CAPACITY = [re.compile(p) for p in CAPACITY_PATTERNS]
_STALL = [re.compile(p) for p in STALL_PATTERNS]

MODEL_FAILURE_KINDS = frozenset({"error", "harness", "stalled"})
NOT_THE_MODEL = frozenset({"transport", "credential", "capacity"})


def failure_kind(error, exit_code=None, ok: bool | None = False) -> str | None:
    """The kind of a failed run; None for a run that did not fail."""
    if ok:
        return None
    err = " ".join(str(error or "").split()).lower()
    if not err and exit_code in (None, 0):
        return None
    if any(p.search(err) for p in _TRANSPORT):
        return "transport"
    if any(p.search(err) for p in _CREDENTIAL):
        return "credential"
    if any(p.search(err) for p in _CAPACITY):
        return "capacity"
    if any(p.search(err) for p in _STALL):
        return "stalled"
    if err.startswith("harness exited") and ("no output" in err or err.endswith("exited 1")):
        return "harness"
    return "error"


def counts_against_model(kind: str | None) -> bool:
    """Whether a failure of this kind belongs in the model's score."""
    return kind is not None and kind not in NOT_THE_MODEL


def summarize_runs(rows: list, *, window: int | None = None) -> list[dict]:
    """Per (harness, model) track record from harness_run rows
    ({harness, model, ok, error, exit_code, elapsed_seconds, created_at, id}),
    newest first. Failures are split by kind, and `model_failed` counts only
    the kinds that say something about the model. `window` is the number of
    most-recent runs the rows were drawn from; it rides every group so the
    counts read as "within the last N runs", never as lifetime."""
    groups: dict[tuple, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        key = (str(r.get("harness") or ""), str(r.get("model") or "(default)"))
        g = groups.setdefault(key, {
            "harness": key[0], "model": key[1], "runs": 0, "ok": 0, "failed": 0,
            "model_failed": 0, "failed_by_kind": {}, "elapsed": [],
            "last_run": None, "last_error": None, "last_error_kind": None, "_last_id": -1,
        })
        g["runs"] += 1
        ok = bool(r.get("ok"))
        if ok:
            g["ok"] += 1
        else:
            g["failed"] += 1
            kind = failure_kind(r.get("error"), r.get("exit_code"), ok) or "error"
            g["failed_by_kind"][kind] = g["failed_by_kind"].get(kind, 0) + 1
            if counts_against_model(kind):
                g["model_failed"] += 1
        if r.get("elapsed_seconds") is not None:
            try:
                g["elapsed"].append(float(r["elapsed_seconds"]))
            except (TypeError, ValueError):
                pass
        ca = r.get("created_at")
        if g["last_run"] is None or (ca is not None and str(ca) > str(g["last_run"])):
            g["last_run"] = ca
        rid = int(r.get("id") or 0)
        if not ok and r.get("error") and rid >= g["_last_id"]:
            g["_last_id"] = rid
            g["last_error"] = str(r["error"])[:160]
            g["last_error_kind"] = failure_kind(r.get("error"), r.get("exit_code"), ok)
    out = []
    for g in groups.values():
        el = g.pop("elapsed")
        g.pop("_last_id", None)
        g["avg_seconds"] = round(sum(el) / len(el), 1) if el else None
        g["last_run"] = g["last_run"].isoformat() if hasattr(g["last_run"], "isoformat") else g["last_run"]
        if window:
            g["window"] = int(window)
        out.append(g)
    out.sort(key=lambda g: str(g.get("last_run") or ""), reverse=True)
    return out
