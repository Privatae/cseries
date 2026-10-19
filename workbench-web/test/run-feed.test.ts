// lib/run-feed.ts — one reader per engine's stream, shared by the run card and
// the book's strands. The loop is RunTerminalStream's (from_seq cursor, each
// seq once, resume after a clean end while live, a drop retried with its
// reason shown), moved; these pin that it still behaves so, that two readers
// share one stream, and that each frame's arrival moves the clock skew.
import assert from "node:assert/strict";
import test from "node:test";
import type { RunFrame } from "../src/lib/run-frames.ts";
import {
  EMPTY_FEED,
  feedLoop,
  modelOf,
  openFeeds,
  RAW_MAX,
  runFeed,
  setFeedOpener,
  subscribeRunFeed,
  withBatch,
} from "../src/lib/run-feed.ts";

const fr = (seq: number, ts: number, kind = "text", payload: Record<string, unknown> = { text: `f${seq}` }): RunFrame => ({
  seq,
  ts,
  kind,
  payload,
});
const terminal = (seq: number, ts: number): RunFrame => fr(seq, ts, "status", { state: "complete", ok: true, exit_code: 0 });
async function* gen(frames: RunFrame[], fail?: Error): AsyncGenerator<RunFrame> {
  for (const f of frames) yield f;
  if (fail) throw fail;
}
const fast = { sleep: async () => {}, setTimer: (fn: () => void) => setTimeout(fn, 0), clearTimer: (t: unknown) => clearTimeout(t as ReturnType<typeof setTimeout>) };

test("a clean end while the run is live resumes from the cursor, and a replayed boundary frame is kept once", async () => {
  const asked: number[] = [];
  const reads = [
    [fr(0, 100), fr(1, 101)],
    // The route re-delivers the boundary frame on a resume.
    [fr(1, 101), fr(2, 102), terminal(3, 103)],
  ];
  const got: number[] = [];
  const ctrl = new AbortController();
  const out = await feedLoop({
    ...fast,
    open: (from) => {
      asked.push(from);
      return gen(reads.shift() ?? []);
    },
    signal: ctrl.signal,
    onBatch: (b) => got.push(...b.map((x) => x.frame.seq)),
    onError: () => {},
    now: () => 200,
  });
  assert.equal(out, "ended");
  assert.deepEqual(asked, [0, 2], "the second read asks from the next seq");
  assert.deepEqual(got, [0, 1, 2, 3]);
});

test("a dropped stream says why while it retries, then clears the reason; a finished run is not retried", async () => {
  const errors: (string | null)[] = [];
  let n = 0;
  const ctrl = new AbortController();
  const out = await feedLoop({
    ...fast,
    open: () => (n++ === 0 ? gen([fr(0, 1)], new Error("run stream HTTP 502")) : gen([terminal(1, 2)])),
    signal: ctrl.signal,
    onBatch: () => {},
    onError: (m) => errors.push(m),
  });
  assert.equal(out, "ended");
  assert.deepEqual(errors, ["run stream HTTP 502", null]);
  const after: (string | null)[] = [];
  const out2 = await feedLoop({
    ...fast,
    open: () => gen([terminal(0, 1)], new Error("dropped after the end")),
    signal: new AbortController().signal,
    onBatch: () => {},
    onError: (m) => after.push(m),
  });
  assert.equal(out2, "ended");
  assert.deepEqual(after, [], "the run is over: nothing to retry or report");
});

test("the frames a connection opens with are a replay; frames after the first pause were written live", async () => {
  // Arrivals: three at once (already written), then one 2.5 s later.
  const arrivals = [10.0, 10.01, 10.02, 12.5, 12.6];
  let i = 0;
  const got: { seq: number; replay?: boolean }[] = [];
  await feedLoop({
    ...fast,
    open: () => gen([fr(0, 1), fr(1, 2), fr(2, 3), fr(3, 4), terminal(4, 5)]),
    signal: new AbortController().signal,
    onBatch: (b) => got.push(...b.map((x) => ({ seq: x.frame.seq, replay: x.replay }))),
    onError: () => {},
    now: () => arrivals[i++],
  });
  assert.deepEqual(got.map((g) => g.replay), [true, true, true, false, false]);
});

test("a replayed frame never sets the clock: its age is unknown (counted, the clocks read short after a reload)", () => {
  // The pod is 45 s behind; the newest replayed frame was written 24 s ago.
  const replayed = withBatch(EMPTY_FEED, [{ frame: fr(0, 1000 - 45 - 24), rx: 1000, replay: true }]);
  assert.equal(replayed.skew, null, "counted, it would say 69 s: 24 s too much");
  const live = withBatch(replayed, [{ frame: fr(1, 1002 - 45), rx: 1002.2 }]);
  assert.equal(Math.round(live.skew! * 10) / 10, 45.2, "the first live frame sets it");
});

test("each frame carries its arrival, and the feed's skew is the least arrival-minus-ts", () => {
  const feed = withBatch(EMPTY_FEED, [
    { frame: fr(0, 1000), rx: 1030.6 },
    { frame: fr(1, 1001), rx: 1031.2 },
    { frame: fr(2, 1002), rx: 1040 },
  ]);
  assert.equal(Math.round(feed.skew! * 10) / 10, 30.2);
  assert.deepEqual(feed.frames.map((f) => f.seq), [0, 1, 2]);
  assert.equal(feed.rawLines[0], JSON.stringify(fr(0, 1000)), "Raw shows the frame as it arrived, nothing added");
  let big = EMPTY_FEED;
  big = withBatch(big, Array.from({ length: RAW_MAX + 5 }, (_, i) => ({ frame: fr(i, 1), rx: 1 })));
  assert.equal(big.rawLines.length, RAW_MAX, "Raw keeps the last 4000");
  assert.equal(big.frames.length, RAW_MAX + 5, "the fold keeps every frame");
});

test("two readers share one stream; the last to go closes it", async () => {
  let opened = 0;
  let aborted = 0;
  setFeedOpener((_from, signal) => {
    opened++;
    signal.addEventListener("abort", () => aborted++);
    return (async function* () {
      yield fr(0, Date.now() / 1000, "tool_call", { tool: "Bash", args: { command: "ls" } });
      // Live: the stream stays open until the readers go.
      await new Promise<void>((r) => signal.addEventListener("abort", () => r()));
    })();
  });
  try {
    let a = 0;
    let b = 0;
    const offA = subscribeRunFeed("s1", "codex", () => a++);
    const offB = subscribeRunFeed("s1", "codex", () => b++);
    assert.equal(opened, 1, "the second reader opened nothing");
    assert.equal(openFeeds(), 1);
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(a >= 1 && b >= 1, "both heard the batch");
    const feed = runFeed("s1", "codex");
    assert.equal(feed.frames.length, 1);
    assert.equal(modelOf(feed.frames), modelOf(feed.frames), "the fold is computed once for every reader");
    assert.equal(modelOf(feed.frames).counts.cmd, 1);
    offA();
    assert.equal(aborted, 0, "one reader left: the stream stays");
    offB();
    assert.equal(aborted, 1, "the last reader left: the stream is closed");
    assert.equal(openFeeds(), 0);
    assert.equal(runFeed("s1", "codex"), EMPTY_FEED, "and its frames forgotten, as an unmounted card's were");
  } finally {
    setFeedOpener(null);
  }
});
