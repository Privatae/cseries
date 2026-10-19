// lib/strands.ts — the run board on the book's right side (slice 4): the
// shared timeline as strands, the scoreboard with "first to finish", the
// session clock and the pod clock correction. Frame shapes are the harness runtime's (events.py): Claude Code's
// "extended thinking — N heartbeats" status frames and its tool_call /
// tool_result / file_edit, codex's command_execution as tool_call + a
// tool_result carrying exit_code, a variant queued for memory, the terminal
// status frame.
import assert from "node:assert/strict";
import test from "node:test";
import { foldSteps, isTableLine, tableCells, tableOf, type PatchFile, type RunModel } from "../src/lib/run-steps.ts";
import type { RunFrame } from "../src/lib/run-frames.ts";
import {
  briefLine,
  epochOf,
  firstToFinish,
  laneState,
  latestBuild,
  latestBuildKey,
  noteSkew,
  podNow,
  runEngines,
  runLabel,
  runSkew,
  runSummary,
  runTitle,
  scoreboard,
  sessionSeconds,
  SKEW_LIMIT_S,
  strands,
  strandWords,
  tickStep,
} from "../src/lib/strands.ts";

const T0 = 1_790_643_225; // the run was sent (manifest started_at)
let seq = 0;
const f = (t: number, kind: string, payload: Record<string, unknown>): RunFrame => ({ seq: seq++, ts: T0 + t, kind, payload });
const done = (t: number, extra: Record<string, unknown> = {}) =>
  f(t, "status", { state: "complete", ok: true, exit_code: 0, elapsed_seconds: t - 2, model: "m", ...extra });

/** Claude Code finishing at +372 s: think, read, run, write, say, done. */
function claude(endAt = 372): RunModel {
  seq = 0;
  return foldSteps([
    f(2, "status", { state: "thinking", detail: "thinking block (text not returned by the model)" }),
    f(3, "tool_call", { tool: "Bash", args: { command: "ls -la", description: "List project files" } }),
    f(3.5, "tool_result", { tool: "", status: "ok", result: "index.html" }),
    f(5, "status", { state: "thinking", detail: "extended thinking — 1 heartbeats" }),
    f(30, "status", { state: "thinking", detail: "extended thinking — 21 heartbeats" }),
    f(40, "tool_call", { tool: "Read", args: { file_path: "/var/lib/cseries/projects/p/checkout.js" } }),
    f(40.2, "tool_result", { tool: "", status: "ok", result: "1\tconst pay = …" }),
    f(120, "file_edit", { path: "checkout.js", tool: "Write" }),
    f(120.1, "tool_result", { tool: "", status: "ok", result: "File created successfully at: checkout.js" }),
    f(200, "tool_call", { tool: "Bash", args: { command: "npm test" } }),
    f(212, "tool_result", { tool: "", status: "error", result: "Exit code 1\n1 failing" }),
    f(300, "text", { text: "Payment form now loads last." }),
    done(endAt),
  ]);
}

/** Codex, still running at +450 s, queued for memory for its first 40 s. */
function codexLive(): RunModel {
  seq = 0;
  return foldSteps([
    f(0, "status", { state: "waiting", detail: "Waiting for memory: codex needs 512Mi and this plan has 32Mi free." }),
    f(40, "status", { state: "thread.started", detail: "th_1" }),
    f(41, "tool_call", { tool: "exec_command", args: { command: "rg pay src" }, id: "i1" }),
    f(42, "tool_result", { tool: "exec_command", status: "ok", result: "src/pay.js", exit_code: 0, id: "i1" }),
    f(90, "file_edit", { path: "src/pay.js", change: "update", id: "i2" }),
    f(400, "tool_call", { tool: "exec_command", args: { command: "npm test" }, id: "i3" }),
  ]);
}

// ── the pod's clock ──────────────────────────────────────────────────────────
test("the skew is the smallest arrival-minus-ts: the pod's clock plus the quickest delivery", () => {
  let s: number | null = null;
  // The pod is 30 s behind this browser; frames take 0.4 s, 0.1 s, 2 s to arrive.
  for (const [ts, rx] of [[1000, 1030.4], [1001, 1031.1], [1002, 1034]] as const) s = noteSkew(s, ts, rx);
  assert.equal(Math.round(s! * 10) / 10, 30.1);
  // Ahead of the browser: negative.
  assert.equal(noteSkew(null, 2000, 1975.2)! < 0, true);
  assert.equal(Math.round(noteSkew(null, 2000, 1975.2)! * 10) / 10, -24.8);
});

test("frames older than two minutes (a replay after a reload) or with no ts never set the clock", () => {
  assert.equal(noteSkew(null, 1000, 1000 + SKEW_LIMIT_S), null, "exactly the limit is out");
  assert.equal(noteSkew(null, 1000, 1000 + 3600), null, "an hour-old frame replayed");
  assert.equal(noteSkew(null, 1000, 1000 - 500), null, "a clock five minutes ahead is not trusted");
  assert.equal(noteSkew(5, undefined, 1000), 5);
  assert.equal(noteSkew(5, "1000", 1000), 5, "a ts must be a number");
  assert.equal(noteSkew(5, 1000, 1010), 5, "a slower frame never raises the skew");
});

test("one skew for the run: the least over its engines; none known, the browser's clock", () => {
  assert.equal(runSkew([null, 3.2, 2.9, undefined]), 2.9);
  assert.equal(runSkew([null, undefined]), null);
  assert.equal(podNow(1000, 2.5), 997.5);
  assert.equal(podNow(1000, null), 1000);
});

test("a skewed pod clock: the live time reads the same whichever way the clocks differ", () => {
  // The browser's clock is `device`; the pod's is device + offset. Frames are
  // stamped by the pod and arrive 0.2 s later. 12 s into the run:
  for (const offset of [40, -40, 0]) {
    seq = 0;
    const start = 5000 + offset;
    const frames = [f(0, "tool_call", { tool: "Bash", args: { command: "ls" } })];
    frames[0].ts = start;
    const M = foldSteps(frames);
    const arrived = start - offset + 0.2;
    const skew = noteSkew(null, frames[0].ts, arrived);
    const deviceNow = arrived + 12;
    const [row] = scoreboard([{ id: "claude-code", M }], podNow(deviceNow, skew));
    assert.equal(row.time, "0:12", `pod ${offset >= 0 ? "ahead" : "behind"} by ${Math.abs(offset)} s`);
    // Without the correction it read 0:00 (clamped) or 0:52.
    const [raw] = scoreboard([{ id: "claude-code", M }], deviceNow);
    if (offset !== 0) assert.notEqual(raw.time, "0:12");
  }
});

test("a session row's times are epoch seconds, as review.py writes them", () => {
  assert.equal(epochOf(1790643225.4), 1790643225.4);
  assert.equal(epochOf("1790643225.4"), 1790643225.4);
  assert.equal(epochOf("2026-10-08T12:00:00Z"), Date.parse("2026-10-08T12:00:00Z") / 1000);
  assert.equal(epochOf(null), null);
  assert.equal(epochOf(0), null);
  assert.equal(epochOf("soon"), null);
});

// ── the timeline ─────────────────────────────────────────────────────────────
test("tick spacing: the steps, at most so many ticks across", () => {
  assert.equal(tickStep(100), 15, "8 across a full-width board");
  assert.equal(tickStep(500), 120);
  assert.equal(tickStep(100, 4), 30, "4 across the book's right side");
  assert.equal(tickStep(30_000, 4), 7200, "past the last step: two hours");
});

test("strands: one per engine in the run's order, on one axis from when the run was sent", () => {
  const cc = claude();
  const cx = codexLive();
  const now = T0 + 450;
  const box = { left: 100, right: 10, width: 410, ticks: 4 }; // a 300 px plot, the book's 4 ticks
  const g = strands({ started_at: T0 }, [{ id: "claude-code", M: cc }, { id: "codex", M: cx, ph: { state: "running" } }], now, box);
  assert.deepEqual(g.strands.map((s) => s.id), ["claude-code", "codex"]);
  assert.equal(g.t0, T0);
  assert.equal(g.anyLive, true);
  assert.equal(g.span, 450 * 1.04, "while one runs the span is now + 4%");
  const X = (t: number) => 100 + ((t - T0) / g.span) * 300;
  assert.equal(g.nowX, X(now), "the now line");
  assert.deepEqual(g.ticks.map((t) => t.label), ["0:00", "2:00", "4:00", "6:00"]);
  const [a, b] = g.strands;
  // Claude Code: done; its strand runs from its first frame to its last.
  assert.equal(a.state, "done");
  assert.deepEqual(a.run, { x0: X(T0 + 2), x1: X(T0 + 372) });
  assert.deepEqual(a.end, { x: X(T0 + 372), kind: "done", ran: 370 });
  assert.deepEqual(a.marks.map((m) => m.kind), ["tool", "tool", "edit", "error"]);
  assert.deepEqual(a.marks.map((m) => m.label), ["Ran List project files", "Read checkout.js", "Wrote checkout.js", "Ran npm test"]);
  assert.equal(a.marks[2].x, X(T0 + 120));
  // Its 25 s of thinking is a band; the empty 1 s block is not.
  assert.equal(a.thinks.length, 1);
  assert.equal(a.thinks[0].x0, X(T0 + 5));
  assert.equal(a.thinks[0].x1, X(T0 + 40));
  // Codex: waited for memory until its first real frame (+41 s: thread.started
  // is a lifecycle frame, not work), then runs to now with a live head.
  assert.equal(b.state, "running");
  assert.deepEqual(b.queued?.x0, X(T0));
  assert.deepEqual(b.queued?.x1, X(T0 + 41));
  // The fold clears its reason once the run starts; the band says "waiting
  // for memory" without it (src/book/bench.tsx).
  assert.equal(b.queued?.note, "");
  assert.deepEqual(b.run, { x0: X(T0 + 41), x1: X(now) });
  assert.deepEqual(b.end, { x: X(now), kind: "live", ran: null });
  assert.equal(b.marks.length, 3, "a command, a file written, a command still running");
});

test("strands once every engine is over: the span is the latest end, no now line", () => {
  const g = strands({ started_at: T0 }, [{ id: "claude-code", M: claude(372) }, { id: "codex", M: claude(500) }], T0 + 900, {
    left: 0,
    right: 0,
    width: 500,
  });
  assert.equal(g.anyLive, false);
  assert.equal(g.span, 500);
  assert.equal(g.nowX, null);
  assert.equal(g.strands[1].end?.x, 500, "the last to end ends at the right edge");
});

test("an engine the status feed already calls done no longer holds the board live", () => {
  const g = strands({ started_at: T0 }, [{ id: "codex", M: codexLive(), ph: { state: "done" } }], T0 + 450, {
    left: 0,
    right: 0,
    width: 100,
  });
  assert.equal(g.anyLive, false);
  assert.equal(g.nowX, null);
});

test("no start time on the row: the axis starts at the first engine's first moment", () => {
  const g = strands(null, [{ id: "codex", M: codexLive() }], T0 + 450, { left: 0, right: 0, width: 100 });
  assert.equal(g.t0, T0, "its queued frame");
});

test("a queued engine has no strand yet, only its wait; a failed or stopped one ends in its own mark", () => {
  seq = 0;
  const queued = foldSteps([f(0, "status", { state: "waiting", detail: "Waiting for memory" })]);
  seq = 0;
  const failed = foldSteps([f(1, "tool_call", { tool: "Bash", args: { command: "x" } }), f(9, "status", { state: "complete", ok: false, exit_code: 1, stderr: "Error: boom" })]);
  seq = 0;
  const stopped = foldSteps([
    f(1, "tool_call", { tool: "Bash", args: { command: "x" } }),
    f(5, "status", { state: "killed", by: "user" }),
    f(6, "status", { state: "complete", ok: false, exit_code: 143, killed_by: "user" }),
  ]);
  const g = strands({ started_at: T0 }, [{ id: "grok", M: queued }, { id: "kimi", M: failed }, { id: "cursor", M: stopped }], T0 + 60, {
    left: 0,
    right: 0,
    width: 100,
  });
  assert.equal(g.strands[0].state, "waiting");
  assert.equal(g.strands[0].queued?.note, "Waiting for memory", "while it waits, its reason");
  assert.equal(g.strands[0].run, null);
  assert.equal(g.strands[0].end, null);
  assert.ok(g.strands[0].queued);
  assert.equal(g.strands[1].end?.kind, "failed");
  assert.equal(g.strands[2].end?.kind, "stopped");
  assert.equal(g.strands[2].state, "stopped");
});

// ── the scoreboard ───────────────────────────────────────────────────────────
test("first to finish: the earliest last frame among engines that completed", () => {
  const early = claude(300);
  const late = claude(372);
  assert.equal(firstToFinish([late, early]), 1);
  assert.equal(firstToFinish([codexLive(), late]), 1, "a running engine never finished");
  seq = 0;
  const failedEarly = foldSteps([f(1, "tool_call", { tool: "Bash", args: {} }), f(50, "status", { state: "complete", ok: false })]);
  assert.equal(firstToFinish([failedEarly, late]), 1, "a failed run that ended first did not finish first");
  seq = 0;
  const timedOut = foldSteps([f(1, "tool_call", { tool: "Bash", args: {} }), f(60, "status", { state: "timeout", ok: false })]);
  assert.equal(firstToFinish([timedOut, late]), 1);
  assert.equal(firstToFinish([failedEarly, timedOut]), -1, "none finished");
});

test("the scoreboard reads right: state, run time, counts, changes, and who finished first", () => {
  const files: PatchFile[] = [
    { path: "checkout.js", add: 40, del: 12, status: "modified", lines: [] },
    { path: "pay.css", add: 8, del: 0, status: "added", lines: [] },
  ];
  const rows = scoreboard(
    [
      { id: "codex", M: codexLive(), ph: { state: "running" } },
      { id: "claude-code", M: claude(372), ph: { state: "done", elapsed_seconds: 371 }, files },
    ],
    T0 + 450,
  );
  const [cx, cc] = rows;
  assert.equal(cx.state, "running");
  assert.equal(cx.time, "6:49", "live: now − its first real frame (+41 s)");
  assert.equal(cx.changes, null, "no changes while it runs");
  assert.equal(cx.first, false);
  assert.equal(cc.state, "done");
  assert.equal(cc.time, "6:10", "the terminal frame's elapsed_seconds (370)");
  assert.equal(cc.first, true);
  assert.deepEqual(cc.changes, { add: 48, del: 12 });
  assert.deepEqual([cc.commands, cc.read, cc.written, cc.messages, cc.failed], [2, 1, 1, 1, 1]);
  assert.equal(
    strandWords(cc),
    "Claude Code: done after 6:10, first to finish. 2 commands, 1 file read, 1 file written, 1 message, 1 failed call, +48 −12 lines.",
  );
  assert.equal(strandWords(cx).startsWith("Codex: running, 6:49 so far."), true, strandWords(cx));
});

test("one engine alone is never 'first to finish'; before it starts its time is a dash", () => {
  assert.equal(scoreboard([{ id: "claude-code", M: claude() }], T0 + 900)[0].first, false);
  seq = 0;
  const queued = foldSteps([f(0, "status", { state: "waiting", detail: "w" })]);
  const [r] = scoreboard([{ id: "grok", M: queued }], T0 + 10);
  assert.equal(r.time, "—");
  assert.equal(r.state, "waiting");
  assert.equal(laneState(foldSteps([]), { state: "queued" }), "queued");
  assert.equal(laneState(foldSteps([]), { queued_note: "Waiting for memory" }), "waiting");
});

// ── the run header ───────────────────────────────────────────────────────────
test("the session clock runs while any engine does, and stops at completed_at, else the last frame", () => {
  const cc = claude(372);
  assert.equal(sessionSeconds({ started_at: T0 }, [cc, codexLive()], T0 + 450), 450);
  assert.equal(sessionSeconds({ started_at: T0, completed_at: T0 + 380 }, [cc], T0 + 9999), 380);
  assert.equal(sessionSeconds({ started_at: T0 }, [cc], T0 + 9999), 372);
  assert.equal(sessionSeconds({}, [cc], T0 + 10), null);
  // An old run whose frames have not loaded yet reads as live from its frames;
  // the caller that knows it is over stops the clock all the same.
  assert.equal(sessionSeconds({ started_at: T0, completed_at: T0 + 380 }, [foldSteps([])], T0 + 99_999, false), 380);
});

test("the run's engines, title, brief line and label", () => {
  assert.deepEqual(runEngines({ harnesses: ["claude-code"], per_harness: { "claude-code": {}, "claude-code-s2": {} } }), [
    "claude-code",
    "claude-code-s2",
  ]);
  assert.deepEqual(runEngines({ harnesses: ["codex", "grok", "codex"] }), ["codex", "grok"]);
  assert.equal(runTitle("a1b2", ["claude-code", "codex"]), "Compare · 2 engines");
  assert.equal(runTitle("factory-build-416", ["claude-code"]), "Build 416 · Claude Code");
  assert.equal(runTitle("x", []), "Run");
  assert.equal(briefLine("a1b2", "\n  Make checkout usable sooner on phones.\nKeep prices."), "Make checkout usable sooner on phones.");
  assert.equal(briefLine("factory-build-416", "You are working in /var/lib/…"), "", "a build's prompt opens the same way every time");
  assert.equal(runLabel({ session_id: "factory-build-416", per_harness: { codex: {} } }), "Build 416 · Codex");
  assert.equal(runLabel({ session_id: "a1b2", prompt: "Faster checkout\nmore" }), "Faster checkout");
  assert.equal(runSummary([claude(), codexLive()]), "1 running · 2 engines");
  assert.equal(runSummary([claude()]), "1 engine");
});

// ── markdown tables in an engine's prose ────────────────────────────────────
test("a markdown table: its separator row makes the header and is never drawn", () => {
  const lines = ["| Profile | Before | After |", "|---|:---:|---:|", "| mid-range phone | 3.8 s | 1.1 s |", "| laptop | 1.2 s | 0.6 s |"];
  assert.equal(lines.every(isTableLine), true);
  assert.equal(isTableLine("Not | a table"), false);
  const t = tableOf(lines.map(tableCells));
  assert.deepEqual(t.head, ["Profile", "Before", "After"]);
  assert.deepEqual(t.body, [
    ["mid-range phone", "3.8 s", "1.1 s"],
    ["laptop", "1.2 s", "0.6 s"],
  ]);
  assert.deepEqual(tableOf([["a", "b"], ["c", "d"]]), { head: null, body: [["a", "b"], ["c", "d"]] }, "no separator, no header");
});

// ── the page's latest finished build (This page's Open) ─────────────────────
test("the latest finished build: the newest run on the page with an engine done", () => {
  const run = (sid: string, at: number, per: Record<string, string>, project_id: number | null = 21) => ({
    session_id: sid,
    started_at: at,
    project_id,
    harnesses: Object.keys(per),
    per_harness: Object.fromEntries(Object.entries(per).map(([h, state]) => [h, { state }])),
  });
  assert.equal(latestBuild([], 21), null, "no runs, nothing to open");
  assert.equal(latestBuild([run("a", T0, { codex: "running" })], 21), null, "nothing finished yet");
  assert.equal(latestBuild([run("a", T0, { codex: "done" })], null), null, "no page, nothing to open");
  const older = run("factory-build-7", T0, { "claude-code": "done" });
  assert.deepEqual(latestBuild([older], 21), { sessionId: "factory-build-7", engines: ["claude-code"], done: ["claude-code"] });
  // A newer run still going: the finished one stays, until the newer one finishes.
  const newer = run("cmp", T0 + 600, { "claude-code": "running", codex: "queued" });
  assert.equal(latestBuild([newer, older], 21)?.sessionId, "factory-build-7");
  const half = run("cmp", T0 + 600, { "claude-code": "done", codex: "running" });
  assert.deepEqual(latestBuild([half, older], 21), { sessionId: "cmp", engines: ["claude-code", "codex"], done: ["claude-code"] },
    "a compare offers the engines already done");
  const both = run("cmp", T0 + 600, { "claude-code": "done", codex: "done" });
  assert.deepEqual(latestBuild([both, older], 21)?.done, ["claude-code", "codex"], "in the run's order");
  assert.notEqual(latestBuildKey(latestBuild([half, older], 21)), latestBuildKey(latestBuild([both, older], 21)),
    "the key moves when another engine finishes");
  // By when it was sent, whatever the list's order.
  assert.equal(latestBuild([older, both], 21)?.sessionId, "cmp");
  // A newer run whose engines all failed built nothing: the older build stays.
  assert.equal(latestBuild([run("bad", T0 + 900, { codex: "failed" }), older], 21)?.sessionId, "factory-build-7");
  // Another page's run, or one with no page, is not this page's.
  assert.equal(latestBuild([run("elsewhere", T0 + 900, { codex: "done" }, 22), older], 21)?.sessionId, "factory-build-7");
  assert.equal(latestBuild([run("nowhere", T0 + 900, { codex: "done" }, null)], 21), null);
  assert.equal(latestBuildKey(null), "");
});
