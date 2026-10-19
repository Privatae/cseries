#!/usr/bin/env python3
"""Gate: a stream that dies mid-run must NOT re-run (and re-bill) the build.

Origin: one build streamed ~15 minutes of real
work, lost its stream, and the kernel re-ran the whole build on buffered /run —
one phase, two executions, two token bills, 1836s of wall clock.

The first fix returned the frame count from _stream_harness_run. That misses its
own motivating case: the function's normal failure is to RAISE (a sock_read
TimeoutError, a ClientPayloadError on a reset — exactly what 313 produced), and
a raise never reaches a return, so the count stayed 0 and the re-run fired
anyway. The count now rides a caller-owned holder
that is stamped as each frame LANDS.

Also pins the buffered fallback's timeout, which the same fix got wrong in the
other direction: sock_read on an endpoint that sends nothing until the child
exits is a TOTAL cap wearing another name, and setting it to the idle window
capped every buffered build at 13 minutes — shorter than the pod's own deadline,
so the kernel abandoned healthy runs before the harness could return its
graceful payload.

Run: APP_ROOT=<repo> python3 scripts/test_stream_fallback_billing.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))

from server.routes import review  # noqa: E402

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


# ── a response that yields N frames and then RAISES, like a dropped stream ──
# Models iter_any(), which is what the reader calls: `async for x in content`
# is readline(), and readline dies on a frame over 512 KiB (see
# test_stream_frame_size.py). A double that still offered only __aiter__ would
# be modelling an interface production no longer uses.
class _DyingContent:
    def __init__(self, lines, exc):
        self._lines, self._exc = lines, exc

    def iter_any(self):
        async def gen():
            for ln in self._lines:
                yield ln
            raise self._exc
        return gen()


class _Resp:
    status = 200

    def __init__(self, lines, exc):
        self.content = _DyingContent(lines, exc)

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, lines, exc):
        self._lines, self._exc = lines, exc

    def post(self, *a, **kw):
        return _Resp(self._lines, self._exc)


LINES = [
    json.dumps({"ts": 1, "seq": 0, "kind": "text",
                "payload": {"text": "working"}}).encode() + b"\n",
    json.dumps({"ts": 2, "seq": 1, "kind": "tool_call",
                "payload": {"tool": "Bash", "args": {"command": "ls"}}}).encode() + b"\n",
    json.dumps({"ts": 3, "seq": 2, "kind": "file_edit",
                "payload": {"path": "server/main.py"}}).encode() + b"\n",
]

# The exceptions a real dropped stream raises. asyncio.TimeoutError is the one
# such a build produced (its str() is empty — see the repr fix).
for exc_label, exc in (
    ("asyncio.TimeoutError (what 313 raised)", asyncio.TimeoutError()),
    ("a connection reset mid-body", ConnectionResetError("reset")),
):
    with tempfile.TemporaryDirectory() as td:
        sdir = Path(td)
        seen = {"n": 0}
        raised = None
        try:
            asyncio.run(review._stream_harness_run(
                _Http(LINES, exc), "http://h", {"prompt": "x"}, "kimi", sdir,
                idle_timeout=30, frames_seen=seen))
        except Exception as e:      # noqa: BLE001
            raised = e
        check(f"{exc_label}: the stream really does raise", raised is not None)
        # THE REGRESSION: the count must survive the raise.
        check(f"{exc_label}: frames_seen survives the raise",
              seen["n"] == 3, f'got {seen["n"]}')
        check(f"{exc_label}: the frames are on disk to be kept",
              review._events_path(sdir, "kimi").read_text().count("\n") == 3)

# A stream that yields NOTHING and dies leaves 0 — the caller may safely re-run.
with tempfile.TemporaryDirectory() as td:
    sdir = Path(td)
    seen = {"n": 0}
    try:
        asyncio.run(review._stream_harness_run(
            _Http([], asyncio.TimeoutError()), "http://h", {"prompt": "x"},
            "kimi", sdir, idle_timeout=30, frames_seen=seen))
    except Exception:
        pass
    check("a stream that produced nothing leaves the count at 0 (re-run is safe)",
          seen["n"] == 0, f'got {seen["n"]}')

# ── the caller's branch + the buffered timeout, read from source ────────────
SRC = (_ROOT / "server" / "routes" / "review.py").read_text()
check("the caller reads the count from the holder, not a return value",
      'stream_frames = frames_seen["n"]' in SRC)
check("_stream_harness_run no longer returns a tuple",
      "return terminal, seq" not in SRC)
check("frames > 0 skips the re-run and keeps the partial work",
      "if not run_out and stream_frames > 0:" in SRC
      and "NOT re-running the build" in SRC)

# The buffered fallback has NO total: it
# waits for as long as the pod answers its health probe. A sock_read on an
# endpoint that sends nothing until exit would be a total cap in disguise, so
# neither may come back.
check("the buffered fallback waits on the pod's liveness, not a total or a sock_read",
      "_buffered_run_watched(" in SRC
      and "timeout=aiohttp.ClientTimeout(total=max_minutes * 60 + 30)," not in SRC,
      "the buffered /run must not carry a wall-clock total")
check("...and the streaming path still uses sock_read (there, frames ARE the signal)",
      "timeout=aiohttp.ClientTimeout(sock_read=idle_timeout)," in SRC)
check("red/green — the 13-minute disguised cap is gone",
      "ClientTimeout(sock_read=_STREAM_IDLE_TIMEOUT_S)" not in SRC)

# ── the run block must END, whichever fallback path ran ────────────────────
# The terminal closes only on a complete/timeout status frame. Neither fallback
# wrote one: the frames-kept branch synthesized run_out kernel-side, and the
# buffered result was never appended on ANY path — so a run sat at "running ·
# no live feed" until the tail route gave up at 1800s, and the diff button
# (which needs a terminal state) never appeared on exactly the run whose kept
# partial work it exists to show. run-frames.ts even promised "the terminal
# status frame still arrives later via the buffered result", describing a
# writer that did not exist.
check("the frames-kept branch appends a terminal frame",
      'if not run_out and stream_frames > 0:' in SRC
      and SRC.split('if not run_out and stream_frames > 0:')[1]
             .split('elif not run_out:')[0]
             .count('_append_run_frame') == 1)
check("...and it is a TERMINAL state, so the block closes",
      '"state": "complete",\n                    "ok": False,' in SRC)
check("the buffered path appends one too (the pre-existing hole)",
      '"ok": bool(run_out.get("ok")),' in SRC)
check("neither terminal frame carries the transcript, diff or creds",
      '"transcript"' not in SRC.split("stream_frames = frames_seen")[1].split("finally:")[0]
      and "rotated_creds" not in SRC.split("stream_frames = frames_seen")[1].split("finally:")[0])

TERM = (_ROOT / "workbench-web" / "src" / "components" / "workbench"
        / "run-terminal.tsx").read_text()
# The button lives in the run card since workbench 0.24.0 (run-card.tsx);
# the rule is the same: enabled the moment the run is not live, whatever its
# outcome — never only on a successful one.
CARD = (_ROOT / "workbench-web" / "src" / "components" / "workbench"
        / "run-card.tsx").read_text()
_open_btn = CARD[CARD.index("data-open-version"):][:600]
check("the open-version button works on any ENDED run, not only a successful one",
      "disabled={M.live}" in _open_btn
      and CARD.count('M.state === "complete"') == 0
      and TERM.count('sm.state === "complete"') == 0,
      "a failed run keeps partial work worth reviewing")

# ── a session that is not there YET is not a session that is missing ───────
# The chat mounts the terminal the instant `build` returns its task_id, but the
# DETACHED build creates the session dir ~650ms later (measured, task
# 314: task row 18:38:23.891, session dir 18:38:24.545). The mount fetched ONCE
# and latched `missing` on that 404 — printing "no recorded session (predates
# the run surface)" over a healthy running build for the rest of its life. A
# race, so it hit some runs and not others.
check("a 404 retries instead of latching missing",
      "if (r.status === 404) again();" in TERM)
check("a 200 whose manifest has not landed yet also retries",
      "// half of the same race. Retryable, not missing." in TERM)
check("a transport blip is not treated as absence either",
      TERM.count("again();") >= 3)
check("missing is only declared after the retry window",
      "if (waited >= RESOLVE_WINDOW_MS) {" in TERM
      and "RESOLVE_WINDOW_MS = 30_000" in TERM)
check("the retry is bounded, not unbounded",
      "Math.min(2000, 250 + waited)" in TERM)
# Checks the RENDERED sentence, not the file: the comments above it explain the
# history and legitimately name the old wording.
_rendered = "\n".join(l for l in TERM.splitlines() if not l.lstrip().startswith("//"))
check("the message no longer asserts a cause it cannot know",
      "no recorded session (predates the run surface)" not in _rendered
      and "no recorded session found" in _rendered)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
