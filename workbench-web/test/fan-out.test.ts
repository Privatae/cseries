import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildTaskIdFor,
  fanOutSessionFromAgentRows,
  fanOutSessionIdFor,
  isFanOutChip,
  orphanBuildsFor,
  variantIdsFor,
} from "../src/lib/fan-out.ts";

const SID = "7030c80567d64eef89b052f0777dda0b";

test("fanOutSessionIdFor: live chip — session id rides the tool result", () => {
  assert.equal(
    fanOutSessionIdFor({
      name: "review.fan_out",
      result: { status: "ok", result: { session_id: SID, task_id: 297, status: "queued" } },
    }),
    SID,
  );
  // older top-level shape
  assert.equal(
    fanOutSessionIdFor({ name: "review.fan_out", result: { session_id: SID } }),
    SID,
  );
});

test("fanOutSessionIdFor: reloaded chip — persisted sessionId wins", () => {
  assert.equal(
    fanOutSessionIdFor({ name: "review.fan_out", sessionId: SID, result: { status: "ok" } }),
    SID,
  );
});

test("fanOutSessionIdFor: not a compare / no id → null (never a single build's)", () => {
  assert.equal(fanOutSessionIdFor({ name: "build", result: { result: { task_id: 298 } } }), null);
  assert.equal(fanOutSessionIdFor({ name: "review.fan_out", result: { status: "ok" } }), null);
  assert.equal(fanOutSessionIdFor({ name: "review.fan_out", result: { status: "error", result: "x" } }), null);
});

test("fanOutSessionFromAgentRows: matches the fan_out row by task id only", () => {
  const rows = [
    { task_id: 298, task_type: "factory_build", session_id: null },
    { task_id: 297, task_type: "fan_out", session_id: SID },
  ];
  assert.equal(fanOutSessionFromAgentRows(rows, "297"), SID);
  assert.equal(fanOutSessionFromAgentRows(rows, "298"), null);
  assert.equal(fanOutSessionFromAgentRows(rows, undefined), null);
  assert.equal(fanOutSessionFromAgentRows(undefined, "297"), null);
});

test("variantIdsFor: variants (incl. Self-MoA samples) > harnesses > per_harness keys", () => {
  assert.deepEqual(
    variantIdsFor({
      manifest: {
        variants: [{ vid: "claude-code" }, { vid: "claude-code-s2" }, { vid: "kimi" }],
        harnesses: ["claude-code", "kimi"],
      },
    }),
    ["claude-code", "claude-code-s2", "kimi"],
  );
  assert.deepEqual(
    variantIdsFor({ manifest: { harnesses: ["claude-code", "kimi"] } }),
    ["claude-code", "kimi"],
  );
  assert.deepEqual(
    variantIdsFor({ per_harness: { kimi: {}, codex: {} } }),
    ["kimi", "codex"],
  );
  assert.deepEqual(variantIdsFor({}), []);
  // duplicates dropped, blanks ignored
  assert.deepEqual(
    variantIdsFor({ manifest: { variants: [{ vid: "kimi" }, { vid: "kimi" }, {}] } }),
    ["kimi"],
  );
});

test("isFanOutChip", () => {
  assert.equal(isFanOutChip({ name: "review.fan_out" }), true); // history chips
  assert.equal(isFanOutChip({ name: "build" }), false);
});

// ── the one build door (2026-09-22): a compare is build(action='run', variants) ──
const VARIANTS = [{ harness: "codex", model: "gpt-5" }, { harness: "codex", model: "o4" }];

test("a build run with variants is a compare chip, not a single build", () => {
  const live = {
    name: "build",
    args: { action: "run", prompt: "p", variants: VARIANTS },
    result: { status: "ok", result: { session_id: SID, task_id: 401 } },
  };
  assert.equal(isFanOutChip(live), true);
  assert.equal(fanOutSessionIdFor(live), SID);
  assert.equal(buildTaskIdFor(live), null);
  // still running, args only (no result yet)
  const pending = { name: "build", args: { action: "run", variants: VARIANTS } };
  assert.equal(isFanOutChip(pending), true);
  assert.equal(buildTaskIdFor(pending), null);
  // reloaded: persisted chips carry no args, only the session id
  const reloaded = { name: "build", args: {}, sessionId: SID, taskId: "401" };
  assert.equal(isFanOutChip(reloaded), true);
  assert.equal(buildTaskIdFor(reloaded), null);
});

test("a build run with no variants is a single build", () => {
  const single = {
    name: "build",
    args: { action: "run", prompt: "p" },
    result: { status: "ok", result: { task_id: 402 } },
  };
  assert.equal(isFanOutChip(single), false);
  assert.equal(fanOutSessionIdFor(single), null);
  assert.equal(buildTaskIdFor(single), "402");
});

// ── orphan builds: the auto-advanced phase (C6, 2026-08-22) ─────────────────
// The chat mounts a run terminal off a `build` tool chip. A phase started by
// approved-plan auto-advance has no tool call in any turn, so it had no chip,
// no terminal, and the conversation showed nothing while it ran — the user saw
// it only in the right pane and only learned it happened when the host reported
// it afterwards.
const turn = (tools: { name: string; taskId?: string; result?: unknown }[]) => ({
  tools,
});

test("a build with no chip in the thread is surfaced", () => {
  const running = [{ taskId: "312", title: "build phase 3" }];
  assert.deepEqual(orphanBuildsFor(running, [turn([])]), running);
});

test("a build the thread ALREADY shows is not doubled", () => {
  const running = [{ taskId: "311", title: "build phase 6" }];
  const turns = [turn([{ name: "build", taskId: "311" }])];
  assert.deepEqual(orphanBuildsFor(running, turns), []);
});

test("the real chain shape: one asked-for phase, two auto-advanced", () => {
  // Task 311 came from a chat `build` call; 312 and 313
  // were started by the chain and have no chip anywhere.
  const running = [
    { taskId: "311", title: "build phase 6" },
    { taskId: "312", title: "build phase 3" },
    { taskId: "313", title: "build phase 4" },
  ];
  const turns = [turn([{ name: "build", taskId: "311" }])];
  assert.deepEqual(
    orphanBuildsFor(running, turns).map((b) => b.taskId),
    ["312", "313"],
  );
});

test("a chip resolves its id from the LIVE result shape too", () => {
  const turns = [
    turn([{ name: "build", result: { status: "ok", result: { task_id: 312 } } }]),
  ];
  assert.deepEqual(
    orphanBuildsFor([{ taskId: "312", title: "x" }], turns),
    [],
    "a chip whose id is only in the tool result still counts as shown",
  );
});

test("a non-build chip never suppresses a running build", () => {
  const turns = [turn([{ name: "research", taskId: "312" }])];
  assert.equal(orphanBuildsFor([{ taskId: "312", title: "x" }], turns).length, 1);
});

test("buildTaskIdFor ignores plan-only calls and other tools", () => {
  assert.equal(buildTaskIdFor({ name: "build", result: { status: "ok" } }), null);
  assert.equal(buildTaskIdFor({ name: "research", taskId: "9" }), null);
  assert.equal(buildTaskIdFor({ name: "build", result: { task_id: 7 } }), "7");
});
