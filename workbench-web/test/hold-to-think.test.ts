import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CLIMB,
  HOLD_FIRST_MS,
  HOLD_SHOW_MS,
  HOLD_STEP_MS,
  LEVEL_WORDS,
  climbLevels,
  composerHint,
  holdProgress,
  levelAt,
  pageEffort,
  sendEffort,
} from "../src/lib/hold-to-think.ts";
import type { EffortOptions } from "../src/lib/effort.ts";

// Hold-to-think on the book's Send: a tap sends at
// the page's effort, holding climbs to high, then max, through the levels the
// page's model takes; and on a page, Send becomes Nudge while a turn runs.

const opts = (efforts: string[], dflt: string | null = "medium"): EffortOptions => ({
  provider: "anthropic",
  model: "claude-opus-5-5",
  efforts,
  default: dflt,
  known: true,
});
const OPUS = opts(["low", "medium", "high", "xhigh", "max"]);

test("the timings: a tap is under half a second; high at 0.5 s, max at 1.2 s", () => {
  assert.equal(HOLD_FIRST_MS, 500);
  assert.equal(HOLD_STEP_MS, 700);
  assert.ok(HOLD_SHOW_MS < HOLD_FIRST_MS);
  assert.deepEqual([...CLIMB], ["high", "max"]);
  const climb = ["high", "max"];
  assert.equal(levelAt(0, climb), null);
  assert.equal(levelAt(499, climb), null); // still a tap
  assert.equal(levelAt(500, climb), "high");
  assert.equal(levelAt(1199, climb), "high");
  assert.equal(levelAt(1200, climb), "max");
  assert.equal(levelAt(60_000, climb), "max"); // it stays at the top
  assert.equal(levelAt(5_000, []), null); // nothing to climb to: always a tap
});

test("the page's effort: its own pick, else the model's default, else unknown", () => {
  assert.equal(pageEffort("high", OPUS), "high");
  assert.equal(pageEffort("", OPUS), "medium");
  assert.equal(pageEffort(null, opts(["low", "high"], null)), null);
  assert.equal(pageEffort("", null), null);
});

test("the climb: high then max, only levels the model takes, only above the page's effort", () => {
  assert.deepEqual(climbLevels(OPUS, "medium"), ["high", "max"]);
  assert.deepEqual(climbLevels(OPUS, "low"), ["high", "max"]);
  assert.deepEqual(climbLevels(OPUS, "high"), ["max"]);
  assert.deepEqual(climbLevels(OPUS, "xhigh"), ["max"]);
  assert.deepEqual(climbLevels(OPUS, "max"), []); // already at the top: a hold is a tap
  // a model with no max (an OpenRouter route at high, say): high only
  assert.deepEqual(climbLevels(opts(["minimal", "low", "medium", "high"], null), null), ["high"]);
  // a model that takes max but not high
  assert.deepEqual(climbLevels(opts(["low", "max"]), "low"), ["max"]);
  // the page's effort unknown: every one the model takes
  assert.deepEqual(climbLevels(opts(["low", "medium", "high", "max"], null), null), ["high", "max"]);
  // a model that takes no effort, or options not read yet: nothing to climb
  assert.deepEqual(climbLevels(opts([]), null), []);
  assert.deepEqual(climbLevels(null, "medium"), []);
});

test("what a press sends: the level reached, else the page's pick, else nothing (the model's default)", () => {
  assert.equal(sendEffort("max", "high"), "max");
  assert.equal(sendEffort(null, "high"), "high");
  assert.equal(sendEffort(null, ""), null);
  assert.equal(sendEffort(null, undefined), null);
});

test("the circle round the press fills over the whole climb", () => {
  assert.equal(holdProgress(0, ["high", "max"]), 0);
  assert.equal(holdProgress(600, ["high", "max"]), 0.5);
  assert.equal(holdProgress(1200, ["high", "max"]), 1);
  assert.equal(holdProgress(5000, ["high", "max"]), 1);
  assert.equal(holdProgress(250, ["max"]), 0.5);
  assert.equal(holdProgress(250, []), 0);
});

test("each level is named in the page's words, as it climbs", () => {
  assert.equal(LEVEL_WORDS.high, "thinks longer");
  assert.equal(LEVEL_WORDS.max, "thinks hardest, costs most");
  for (const lv of ["minimal", "low", "medium", "high", "xhigh", "max"]) assert.ok(LEVEL_WORDS[lv], lv);
});

test("the line under the box: what Enter does now", () => {
  assert.equal(
    composerHint({ nudging: false, page: "medium", climb: ["high", "max"] }),
    "Enter sends at medium · hold Enter or the button to think harder (high, then max)",
  );
  assert.equal(composerHint({ nudging: false, page: "high", climb: ["max"] }),
    "Enter sends at high · hold Enter or the button to think harder (max)");
  assert.equal(composerHint({ nudging: false, page: null, climb: [] }), "Enter sends");
  assert.equal(
    composerHint({ nudging: true, page: "medium", climb: ["high", "max"] }),
    "Enter nudges: your words reach the Host before its next step, without stopping it.",
  );
});

