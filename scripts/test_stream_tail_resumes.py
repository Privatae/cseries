#!/usr/bin/env python3
"""Gate: a variant waiting for memory never freezes in the Workbench.

Two halves, both real (review Q8 on feat/fanout-waves, 2026-09-06):

1. The per-variant stream route (review.stream_session_harness_events) tails
   for the NEXT frame with an IDLE window, not a total. Before this it grew
   `idle` on every poll and never reset it on a yielded frame — a 30-minute
   cap per connection. A run streaming longer than that lost its tab; with
   fan-out waves a queued variant started that clock on "waiting for memory"
   and crossed it in the ordinary case (wave-1 run 20 min + its own run
   15 min). Driven here with the windows shortened to milliseconds.

2. The run terminal resumes from its cursor when a stream ends cleanly while
   the run is still live (run-frames.ts resumeAfterCleanEnd, called from
   run-terminal.tsx) — the route still returns after its idle window with no
   terminal frame, and a waiting variant can out-wait any window. The pure
   half is under `node --test`; this gate pins the wiring in the component
   source, since a return on clean end is exactly the line that froze it.

Run from the repo root:  python3 scripts/test_stream_tail_resumes.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(1, str(_ROOT / "brain"))

from server.routes import review  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


IDLE_S, POLL_S = 0.4, 0.05


async def _drain(resp, out: list, done: asyncio.Event) -> None:
    async for chunk in resp.body_iterator:
        for line in chunk.decode().splitlines():
            if line.strip():
                out.append(json.loads(line))
    done.set()


async def main() -> None:
    saved = (review.SESSIONS_ROOT, review._STREAM_TAIL_IDLE_S, review._STREAM_TAIL_POLL_S)
    with tempfile.TemporaryDirectory() as td:
        try:
            review.SESSIONS_ROOT = Path(td)
            review._STREAM_TAIL_IDLE_S = IDLE_S
            review._STREAM_TAIL_POLL_S = POLL_S
            sd = Path(td) / "s1"
            sd.mkdir()
            vid = "kimi"
            # the kernel's waiting frame, written by _apply_capacity before any run
            review._append_run_frame(sd, vid, "status", {"state": "waiting", "detail": "w"})

            print("== 1. the tail is IDLE: frames keep a connection alive past the old total ==")
            got: list = []
            done = asyncio.Event()
            resp = await review.stream_session_harness_events("s1", vid, from_seq=0)
            t0 = time.monotonic()
            task = asyncio.create_task(_drain(resp, got, done))
            # frames every 0.15 s for 1.2 s = 3x the idle window, each inside it
            for i in range(8):
                await asyncio.sleep(0.15)
                review._append_run_frame(sd, vid, "text", {"text": f"line {i}"})
            await asyncio.sleep(0.1)
            check("the connection outlived the idle window while frames kept coming",
                  not done.is_set() and time.monotonic() - t0 > IDLE_S * 2, f"done={done.is_set()}")
            check("every frame was delivered, in file order, waiting frame first",
                  [e["seq"] for e in got] == list(range(9))
                  and got[0]["payload"]["state"] == "waiting", str([e["seq"] for e in got]))
            # now silence: the connection ends after ~IDLE_S with NO terminal frame
            t1 = time.monotonic()
            await asyncio.wait_for(done.wait(), timeout=IDLE_S * 4)
            gap = time.monotonic() - t1
            check("silence ends the connection after the idle window (not sooner, not much later)",
                  IDLE_S * 0.8 <= gap <= IDLE_S * 2.5, f"{gap:.2f}s")
            check("...and it ended WITHOUT a terminal frame (the run is still live)",
                  not any(e.get("kind") == "status" and e["payload"].get("state") in ("complete", "timeout")
                          for e in got))
            await task

            print()
            print("== 2. resume from the cursor delivers what lands after the clean end ==")
            cursor = got[-1]["seq"] + 1        # run-frames.ts nextSeq
            got2: list = []
            done2 = asyncio.Event()
            resp2 = await review.stream_session_harness_events("s1", vid, from_seq=cursor)
            task2 = asyncio.create_task(_drain(resp2, got2, done2))
            await asyncio.sleep(0.1)
            review._append_run_frame(sd, vid, "text", {"text": "after the wait"})
            review._append_run_frame(sd, vid, "status",
                                     {"state": "complete", "ok": True, "exit_code": 0})
            await asyncio.wait_for(done2.wait(), timeout=IDLE_S * 4)
            await task2
            check("the resumed stream starts at the cursor — nothing replayed, nothing skipped",
                  [e["seq"] for e in got2] == [cursor, cursor + 1], str([e["seq"] for e in got2]))
            check("...and ends at the terminal frame",
                  got2[-1]["kind"] == "status" and got2[-1]["payload"]["state"] == "complete")

            print()
            print("== 3. a variant with NO file yet is waited for, not 404'd, and still idles out ==")
            got3: list = []
            done3 = asyncio.Event()
            resp3 = await review.stream_session_harness_events("s1", "grok", from_seq=0)
            t2 = time.monotonic()
            task3 = asyncio.create_task(_drain(resp3, got3, done3))
            await asyncio.wait_for(done3.wait(), timeout=IDLE_S * 4)
            await task3
            check("no file -> clean end after the idle window, no frames",
                  got3 == [] and time.monotonic() - t2 >= IDLE_S * 0.8)
        finally:
            (review.SESSIONS_ROOT, review._STREAM_TAIL_IDLE_S, review._STREAM_TAIL_POLL_S) = saved

    print()
    print("== 4. the Workbench resumes a cleanly-ended live stream (source wiring) ==")
    # Workbench 0.27.0 (book slice 4): the run card's read loop moved, unchanged,
    # from run-terminal.tsx to lib/run-feed.ts feedLoop, so the card and the
    # book's strands share one stream; run-terminal.tsx reads through it.
    # workbench-web/test/run-feed.test.ts drives the loop itself (resume from
    # the cursor after a clean end while live, the boundary frame once).
    term = (_ROOT / "workbench-web" / "src" / "components" / "workbench" / "run-terminal.tsx").read_text()
    src = (_ROOT / "workbench-web" / "src" / "lib" / "run-feed.ts").read_text()
    frames = (_ROOT / "workbench-web" / "src" / "lib" / "run-frames.ts").read_text()
    check("run-frames.ts exports resumeAfterCleanEnd = state is still live",
          re.search(r"export function resumeAfterCleanEnd\(sm: RunSummary\): boolean \{\s*"
                    r"return sm\.state === \"live\";", frames) is not None)
    loop = src[src.index("for await (const f of d.open(nextSeq, d.signal))"):src.index("} catch (e) {")]
    check("the terminal no longer returns on a clean end while live",
          "return; // clean end" not in loop
          and re.search(r"if \(!resumeAfterCleanEnd\(summary\)\) return \"ended\";\s*"
                        r"await sleep\(RESUME_DELAY_MS\);", loop) is not None, loop[-400:])
    # workbench 0.24.0: the cursor is the loop's own `nextSeq`, advanced past
    # every seq the loop kept, and the summary fold is run-frames.foldSummary.
    check("...and resumes from the fold's cursor",
          "readRunStream(sessionId, harness, from, signal)" in src
          and "nextSeq = f.seq + 1;" in src and "summary = foldSummary(summary, f);" in src
          and "useRunFeed(sessionId, harness)" in term)
    check("route constants exist with the documented defaults",
          review._STREAM_TAIL_IDLE_S == 1800.0 and review._STREAM_TAIL_POLL_S == 0.5)


asyncio.new_event_loop().run_until_complete(main())

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
