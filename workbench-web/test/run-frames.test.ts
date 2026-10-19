import { test } from "node:test";
import assert from "node:assert/strict";
import {
  emptyFold,
  foldFrame,
  frameToLines,
  lastErrorLine,
  MAX_LINES,
  summaryLine,
  type RunFrame,
} from "../src/lib/run-frames.ts";

const f = (seq: number, kind: string, payload: Record<string, unknown>): RunFrame => ({
  seq,
  kind,
  payload,
});

test("frameToLines: kinds map to terminal styles; unknown kinds are never dropped", () => {
  assert.deepEqual(frameToLines(f(0, "text", { text: "hello" })), [
    { seq: 0, style: "text", text: "hello" },
  ]);
  assert.equal(frameToLines(f(1, "thinking", { text: "hm" }))[0].style, "thinking");
  // 0.13.4: a file op shows its path on the header line (the substance of
  // the call); the full tool-frame rendering is covered in run-frames-tools.
  assert.equal(
    frameToLines(f(2, "tool_call", { tool: "write", input: { path: "a.html" } }))[0].text,
    "→ write a.html",
  );
  assert.equal(
    frameToLines(f(3, "file_edit", { path: "index.html", bytes: 13074 }))[0].text,
    "+ index.html (12.8 KB)",
  );
  assert.equal(frameToLines(f(4, "error", { message: "boom" }))[0].style, "error");
  // events.py design rule mirrored: unclassifiable → text, not silence.
  assert.equal(frameToLines(f(5, "someday-kind", { text: "raw line" }))[0].style, "text");
  // usage and terminal status frames render no body lines (they fold).
  assert.deepEqual(frameToLines(f(6, "usage", { total_tokens: 9 })), []);
  assert.deepEqual(frameToLines(f(7, "status", { state: "complete", ok: true })), []);
});

test("retry frame renders amber and counts in the summary (harness_core RUN_RETRY)", () => {
  const fr = f(8, "status", {
    state: "retrying",
    attempt: 2,
    reason: "provider.connection_error: Connection error.",
  });
  const [line] = frameToLines(fr);
  assert.equal(line.style, "retry");
  assert.match(line.text, /retrying \(attempt 2\)/);
  let st = emptyFold();
  st = foldFrame(st, fr);
  assert.equal(st.summary.retries, 1);
  assert.equal(st.summary.state, "live");
});

test("fold: multi-line text splits, files dedupe, terminal frame closes the run", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "text", { text: "a\nb" }));
  assert.equal(st.lines.length, 2);
  st = foldFrame(st, f(1, "file_edit", { path: "index.html", bytes: 10 }));
  st = foldFrame(st, f(2, "file_edit", { path: "index.html", bytes: 20 }));
  st = foldFrame(st, f(3, "file_edit", { path: "app.yaml", bytes: 5 }));
  assert.deepEqual(st.summary.files, ["index.html", "app.yaml"]);
  st = foldFrame(
    st,
    f(4, "status", {
      state: "complete",
      ok: true,
      exit_code: 0,
      elapsed_seconds: 136.59,
      model: "k3",
      retries: 1,
    }),
  );
  assert.equal(st.summary.state, "complete");
  assert.equal(st.summary.exitCode, 0);
  assert.equal(st.summary.model, "k3");
  assert.equal(st.summary.retries, 1);
  assert.equal(summaryLine(st.summary), "exit 0 · 2m 17s · 2 files · 1 retry");
});

test("failed terminal frame surfaces the CLI's own failure line off stderr", () => {
  let st = emptyFold();
  st = foldFrame(
    st,
    f(0, "status", {
      state: "complete",
      ok: false,
      exit_code: 1,
      elapsed_seconds: 296.4,
      stderr:
        "reasoning noise\n" +
        "error: failed to run prompt: provider.connection_error: Connection error.\n" +
        "See log: /x/kimi-code.log\n",
    }),
  );
  assert.equal(st.summary.state, "failed");
  // the marked line wins over the trailing "See log" line
  assert.match(st.summary.failureLine ?? "", /provider\.connection_error/);
  assert.match(summaryLine(st.summary), /^failed · exit 1/);
});

test("from_seq replay: the boundary frame folds exactly once", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "text", { text: "one" }));
  st = foldFrame(st, f(0, "text", { text: "one" })); // replayed boundary
  st = foldFrame(st, f(1, "text", { text: "two" }));
  assert.equal(st.lines.length, 2);
  assert.equal(st.nextSeq, 2);
});

test("token deltas join into ONE block; a whole-message frame never glues", () => {
  // The shredded-prose defect, from an antigravity capture:
  // the ACTIVE chunk ends mid-sentence and its DONE frame continues it.
  let st = emptyFold();
  st = foldFrame(st, f(0, "text", { text: "…declaring id, title, ", delta: true }));
  st = foldFrame(st, f(1, "text", { text: "entrypoint, zero permissions.", delta: true }));
  assert.equal(st.lines.length, 1);
  assert.equal(st.lines[0].text, "…declaring id, title, entrypoint, zero permissions.");
  // An UNMARKED frame is a whole message — its own line, never appended.
  st = foldFrame(st, f(2, "text", { text: "a complete message" }));
  assert.equal(st.lines.length, 2);
  assert.equal(st.lines[1].text, "a complete message");
  // …and it closes the run: the next chunk starts a fresh block.
  st = foldFrame(st, f(3, "text", { text: "new chunk", delta: true }));
  assert.equal(st.lines.length, 3);
  assert.equal(st.lines[2].text, "new chunk");
});

test("a non-delta frame between chunks closes the block (no gluing across a tool call)", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "text", { text: "before ", delta: true }));
  st = foldFrame(st, f(1, "tool_call", { tool: "run_command", args: { command: "ls" } }));
  st = foldFrame(st, f(2, "text", { text: "after", delta: true }));
  const texts = st.lines.map((l) => l.text);
  // "before " keeps its own trailing space — the vendor puts the spacing
  // INSIDE the chunk so pieces concatenate cleanly; nothing trims it.
  assert.ok(texts.includes("before "), texts.join("|"));
  assert.ok(texts.includes("after"), texts.join("|"));
  assert.ok(!texts.some((t) => t.includes("before after")), texts.join("|"));
  // the tool call sits between them, so the two chunks are separate blocks
  assert.ok(texts.indexOf("after") > texts.indexOf("→ run_command"), texts.join("|"));
});

test("thinking and text deltas never merge with each other", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "thinking", { text: "reasoning ", delta: true }));
  st = foldFrame(st, f(1, "text", { text: "prose", delta: true }));
  assert.equal(st.lines.length, 2);
  assert.equal(st.lines[0].style, "thinking");
  assert.equal(st.lines[1].style, "text");
});

test("joined deltas containing newlines stay multi-line and keep growing", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "text", { text: "line one\nline ", delta: true }));
  assert.deepEqual(st.lines.map((l) => l.text), ["line one", "line "]);
  st = foldFrame(st, f(1, "text", { text: "two", delta: true }));
  assert.deepEqual(st.lines.map((l) => l.text), ["line one", "line two"]);
});

test("scrollback is capped at MAX_LINES", () => {
  let st = emptyFold();
  for (let i = 0; i < MAX_LINES + 50; i++) {
    st = foldFrame(st, f(i, "text", { text: `l${i}` }));
  }
  assert.equal(st.lines.length, MAX_LINES);
  assert.equal(st.lines[st.lines.length - 1].text, `l${MAX_LINES + 49}`);
});

test("lastErrorLine picks the marked line, else the last line", () => {
  assert.equal(lastErrorLine(""), null);
  assert.equal(lastErrorLine("just prose\nmore prose"), "more prose");
  assert.match(
    lastErrorLine("a\nerror: it failed\ntrailing note") ?? "",
    /error: it failed/,
  );
});

// ── stream_lost (C3, 2026-08-22) ────────────────────────────────────────────
// The kernel's own ClientTimeout cut the NDJSON stream, the run carried on
// under buffered /run for another quarter hour, and
// the terminal simply froze at the last frame with a pulsing "live" badge over
// it. Nothing said the feed had died, so a working build read as hung.
test("stream_lost renders a notice and does NOT close the run", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "text", { text: "working" }));
  st = foldFrame(st, f(1, "status", { state: "stream_lost", detail: "TimeoutError()" }));

  // It is visible...
  const last = st.lines[st.lines.length - 1];
  assert.match(last.text, /live feed lost/);
  assert.match(last.text, /still going/);
  assert.equal(last.style, "retry", "not an error — nothing failed");

  // ...it is flagged...
  assert.equal(st.summary.streamLost, true);
  // ...and the RUN is still live. Closing it here would report a failure that
  // did not happen.
  assert.equal(st.summary.state, "live");
  assert.equal(summaryLine(st.summary), "running · no live feed");
});

test("stream_lost never leaks the raw exception text to the user", () => {
  const lines = frameToLines(
    f(0, "status", { state: "stream_lost", detail: "TimeoutError()" }),
  );
  assert.equal(lines.length, 1);
  assert.doesNotMatch(lines[0].text, /TimeoutError/);
});

test("a terminal frame after stream_lost still sets the real outcome", () => {
  let st = emptyFold();
  st = foldFrame(st, f(0, "status", { state: "stream_lost" }));
  st = foldFrame(
    st,
    f(1, "status", { state: "complete", ok: true, exit_code: 0, elapsed_seconds: 1836 }),
  );
  assert.equal(st.summary.state, "complete");
  assert.equal(st.summary.streamLost, true, "the feed loss is still recorded");
  assert.match(summaryLine(st.summary), /exit 0/);
});

test("queued for memory (fan-out waves): waiting, not live and not failed, until the run's first frame", () => {
  let st = emptyFold();
  st = foldFrame(
    st,
    f(0, "status", {
      state: "waiting",
      detail: "Waiting for memory: kimi needs 512Mi and this plan has 32Mi free while antigravity, codex and claude-code run. It starts as memory frees.",
    }),
  );
  assert.equal(st.summary.state, "live");
  assert.match(st.summary.waiting ?? "", /^Waiting for memory: kimi needs 512Mi/);
  assert.equal(summaryLine(st.summary), "waiting for memory");
  // the reason is a line in the scrollback
  assert.equal(st.lines.length, 1);
  assert.equal(st.lines[0].style, "status");
  assert.match(st.lines[0].text, /^· waiting — Waiting for memory: kimi/);
  // the pod's first frame continues the file's numbering (review.py
  // _stream_harness_run offsets by what is on disk) and clears the wait
  st = foldFrame(st, f(1, "text", { text: "Reading the brief" }));
  assert.equal(st.summary.waiting, null);
  assert.equal(summaryLine(st.summary), "running");
  assert.equal(st.lines.length, 2);
  // a variant that can never fit closes on the terminal frame as before
  let never = emptyFold();
  never = foldFrame(never, f(0, "status", { state: "waiting", detail: "w" }));
  never = foldFrame(never, f(1, "status", { state: "complete", ok: false, exit_code: null }));
  assert.equal(never.summary.state, "failed");
  assert.equal(never.summary.waiting, null);
});

test("a clean stream end while the run is live means resume from the cursor, not terminal", async () => {
  const { resumeAfterCleanEnd } = await import("../src/lib/run-frames.ts");
  // waiting for memory: the route's idle window expires with no terminal frame
  let st = emptyFold();
  st = foldFrame(st, f(0, "status", { state: "waiting", detail: "w" }));
  assert.equal(resumeAfterCleanEnd(st.summary), true);
  // mid-run silence long enough for the route to give up: still live → resume
  st = foldFrame(st, f(1, "text", { text: "working" }));
  assert.equal(resumeAfterCleanEnd(st.summary), true);
  assert.equal(st.nextSeq, 2); // the cursor the reconnect resumes from
  // a lost feed is still a live run → resume
  st = foldFrame(st, f(2, "status", { state: "stream_lost" }));
  assert.equal(resumeAfterCleanEnd(st.summary), true);
  // the terminal frame ends it
  st = foldFrame(st, f(3, "status", { state: "complete", ok: true, exit_code: 0 }));
  assert.equal(resumeAfterCleanEnd(st.summary), false);
  let t = emptyFold();
  t = foldFrame(t, f(0, "status", { state: "timeout", ok: false }));
  assert.equal(resumeAfterCleanEnd(t.summary), false);
});
