#!/usr/bin/env python3
"""Gate: a harness run is killed for going QUIET, never for taking LONG.

No time budgets: with a live feed there is no need for one. Spending the
tokens on a big build only to have it killed at the end, and risk losing it,
costs real money; if it takes 30 minutes, let it run.

The rule this replaced was `deadline = started + max_minutes*60`, checked only
after a stdout line arrived — so it killed the run that was actively producing
output and could not touch one that had gone silent. Measured on one Host's
build: killed at 15 minutes of real work, reported as "kimi exceeded
15min cap" after 30.6 minutes of wall clock, because the kernel then re-ran the
whole build on the buffered fallback and billed the phase twice.

These gates run the REAL streaming generator against scripted child processes.

Run: python3 scripts/test_harness_run_liveness.py
"""
import os
import sys
import time
from pathlib import Path

# /app in the image; APP_ROOT=<repo> to run against the source tree — the same
# convention (and the same two path entries) as test_harness_runtime.py:47-50,
# so smoke_test.sh can invoke this the way it invokes that one.
_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "cli"))          # cseries_sidecar
sys.path.insert(0, str(_ROOT / "system-apps"))  # _harness_runtime

# Small windows so the gates run in seconds, not minutes. The production
# defaults are HARNESS_IDLE_MINUTES (10) and HARNESS_STREAM_IDLE_S (780).
#
# 1.5s, down from 3s: this gate was 25s of a 108s pre-merge run,
# all of it waiting out the window across five scenarios. The run watchdog
# polls at _idle_poll_s(window) = 0.375s for a 1.5s window (harness_core.py,
# `_idle_poll_s` and the run loop that reads it), so silence is detected in
# ~1.9s; the chatty scenarios tick every 0.3s, five times inside the window,
# so a scheduler hiccup on a loaded box — this gate now runs in a parallel
# pool — cannot mis-kill a live run.
WINDOW_S = 1.5
os.environ["HARNESS_IDLE_MINUTES"] = str(WINDOW_S / 60)
os.environ["HARNESS_IDLE_FLOOR_S"] = "1"           # let the short window apply
os.environ["HARNESS_RUN_RETRY_LIMIT"] = "0"
os.environ["HARNESS_ORPHAN_DRAIN_S"] = "1.5"  # keep the leaked-child gate quick

from _harness_runtime import harness_core as hc  # noqa: E402

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


check("idle window is env-tunable, not a literal",
      abs(hc.IDLE_MINUTES - WINDOW_S / 60) < 1e-9, f"got {hc.IDLE_MINUTES}")


class _Spec:
    """Minimal HarnessSpec stand-in: a python child we fully script."""
    id = "kimi"
    display = "kimi"
    run_cwd = False
    workspace = Path("/tmp")
    normalize_model = None
    catch_run_errors = True
    default_max_minutes = 10
    baked = True                 # skip _bootstrap_binary
    auth_error = "no auth"
    derive_creds = None

    @staticmethod
    def run_auth_ok(spec, creds):
        return True

    class _Log:
        def warning(self, *a, **k): pass
        def exception(self, *a, **k): pass
        def info(self, *a, **k): pass
    logger = _Log()

    def __init__(self, script: str):
        self._script = script

    def build_argv(self, spec, prompt, project_dir, model, effort):
        return [sys.executable, "-u", "-c", self._script]

    def build_env(self, spec, creds):
        return dict(os.environ)

    def report_model(self, model):
        return model or "test-model"


def _run(script: str, body: dict) -> tuple[list, float]:
    """Drive the real make_run_stream generator. Returns (frames, elapsed)."""
    spec = _Spec(script)
    # Neutralise the bits that need a real pod/workspace/credentials.
    hc.worktree_diff = lambda *_a, **_k: ""            # type: ignore[assignment]
    hc._rotated_creds = lambda *_a, **_k: None         # type: ignore[assignment]
    hc._read_creds = lambda *_a, **_k: {}              # type: ignore[assignment]
    hc._export_creds = lambda *_a, **_k: {}            # type: ignore[assignment]
    handler = hc.make_run_stream(spec)

    class _Req:
        def __init__(self, b): self.body = __import__("json").dumps(b)
    t0 = time.time()
    frames = list(handler(_Req(body)))
    return frames, time.time() - t0


def _terminal(frames: list) -> dict:
    for f in reversed(frames):
        if f.get("kind") == "status" and f.get("payload", {}).get("state") in (
                "complete", "timeout"):
            return f["payload"]
    return {}


# ── 1. Output keeps a run alive indefinitely ────────────────────────────────
# Emits a line every 0.3 s for ~3.6 s — more than twice the 1.5 s idle window.
# Every frame resets the clock, so total elapsed never decides anything. This
# is the whole property: length does not kill, silence does.
CHATTY = (
    "import sys,time\n"
    "for i in range(12):\n"
    "    print('{\"type\":\"tick\",\"i\":%d}' % i); time.sleep(0.3)\n"
)
frames, elapsed = _run(CHATTY, {"prompt": "x", "max_minutes": 60})
term = _terminal(frames)
check("a run that keeps emitting output is never stopped",
      term.get("state") == "complete", f"state={term.get('state')} elapsed={elapsed:.1f}s")
check("...even though it ran for MULTIPLES of the idle window",
      elapsed > 2 * (hc.IDLE_MINUTES * 60), f"elapsed={elapsed:.1f}s")
check("...and is reported ok", term.get("ok") is True, f"{term.get('ok')}")

# ── 2. A SILENT run is killed, and says why ─────────────────────────────────
SILENT = "import time\ntime.sleep(60)\n"
frames, elapsed = _run(SILENT, {"prompt": "x", "max_minutes": 60})
term = _terminal(frames)
check("a run that goes silent past the idle window is stopped",
      term.get("state") == "timeout", f"state={term.get('state')}")
check("...promptly — the watchdog does not wait for a line that never comes",
      elapsed < 20.0, f"elapsed={elapsed:.1f}s")
check("...and the message says STUCK, not 'exceeded a cap'",
      "no output" in (term.get("error") or "")
      and "not stopped for running long" in (term.get("error") or ""),
      f"error={term.get('error')!r}")

# ── 3. stderr counts as liveness ────────────────────────────────────────────
# kimi streams its reasoning to stderr. A run can be plainly working for minutes
# with stdout silent; counting only stdout would kill exactly those runs.
STDERR_ONLY = (
    "import sys,time\n"
    "for i in range(8):\n"
    "    print('thinking...', file=sys.stderr); time.sleep(0.3)\n"
    "print('{\"type\":\"done\"}')\n"
)
frames, elapsed = _run(STDERR_ONLY, {"prompt": "x", "max_minutes": 60})
term = _terminal(frames)
check("a run whose only output is stderr is NOT killed as idle",
      term.get("state") == "complete", f"state={term.get('state')} elapsed={elapsed:.1f}s")
check("...and it really did run past the idle window on stderr alone",
      elapsed > WINDOW_S, f"elapsed={elapsed:.1f}s")

# ── 4. partial work is preserved on a kill ───────────────────────────────────
# One frame, then silence. That silence is a MODEL TURN (an
# event landed, no tool is in flight) and is bounded by the run's own
# max_minutes, not the idle window — the exact shape that killed astra twice
# on one Host (a completed `pip install`, then ten quiet
# minutes waiting on the model). So the budget here is the window, and the
# kill this case proves is the budget kill; the property is the same: what
# streamed before the kill is in the terminal frame, and the error says so.
# See test_harness_watchdog_phases.py for the phase rule itself.
PARTIAL = (
    "import sys,time\n"
    "print('{\"type\":\"tick\"}')\n"
    "time.sleep(60)\n"
)
frames, _ = _run(PARTIAL, {"prompt": "x", "max_minutes": WINDOW_S / 60})
term = _terminal(frames)
check("a budget-killed run still returns its transcript so far",
      "tick" in (term.get("transcript") or ""), f"{term.get('transcript')!r}")
check("...and says the partial work is kept",
      "Partial work is kept" in (term.get("error") or ""), f"{term.get('error')!r}")

# ── 5. a leaked child must not keep the run alive forever ──────────────────
# The reader loop leaves `for line in proc.stdout` only at EOF, so a child that
# inherited stdout holds the pipe open after its parent is killed. That was
# survivable while a total deadline existed; with the cap gone, nothing else
# would ever end the run. The kill now targets the process GROUP — it does NOT
# close stdout, which deadlocks against the blocked reader.
#
# The child below spawns a grandchild that inherits stdout, sleeps far past the
# idle window, and exits immediately itself — so the ONLY way this run ends is
# if the grandchild is killed too.
LEAKY = (
    "import subprocess,sys,time\n"
    "print('{\"type\":\"tick\"}')\n"
    "subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
    "time.sleep(0.2)\n"
)
frames, elapsed = _run(LEAKY, {"prompt": "x", "max_minutes": 60})
term = _terminal(frames)
check("a run whose child leaked stdout still terminates",
      term != {}, f"no terminal frame after {elapsed:.1f}s")
check("...and it ends promptly, not at the grandchild's 120s sleep",
      elapsed < 30.0, f"elapsed={elapsed:.1f}s")

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
