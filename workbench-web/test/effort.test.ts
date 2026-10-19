import { test } from "node:test";
import assert from "node:assert/strict";
import { defaultLabel, hasChoices, keepPick, type EffortOptions } from "../src/lib/effort.ts";

// The project thread's reasoning-effort pick (the Workbench header).
// The levels and the default come from the server (GET
// /api/inference/effort-options); these pin how the chat box words them and
// keeps a pick honest.

const haiku: EffortOptions = {
  provider: "anthropic", model: "claude-haiku-5-5",
  efforts: ["low", "medium", "high", "xhigh", "max"], default: "medium", known: true,
};
const sonnet46: EffortOptions = {
  provider: "anthropic", model: "claude-sonnet-4-6",
  efforts: ["low", "medium", "high", "max"], default: "high", known: true,
};
const takesNone: EffortOptions = {
  provider: "anthropic", model: "claude-haiku-4-5", efforts: [], default: null, known: true,
};
const openrouter: EffortOptions = {
  provider: "openrouter", model: "moonshotai/kimi-k3",
  efforts: ["minimal", "low", "medium", "high", "xhigh", "max"], default: null, known: true,
};

test("the blank pick reads as the default that runs, naming its level when known", () => {
  assert.equal(defaultLabel(haiku), "Default · medium");
  assert.equal(defaultLabel(sonnet46), "Default · high");
});

test("...and as 'Default' when the level is not named — never an empty label", () => {
  assert.equal(defaultLabel(openrouter), "Default");
  assert.equal(defaultLabel(null), "Default");
});

test("a pick the new model takes is kept across a model change", () => {
  assert.equal(keepPick("high", sonnet46), "high");
});

test("a pick the new model does not take drops back to its default", () => {
  assert.equal(keepPick("xhigh", sonnet46), "");
  assert.equal(keepPick("high", takesNone), "");
  assert.equal(keepPick("high", null), "");
});

test("no pick stays no pick", () => {
  assert.equal(keepPick("", haiku), "");
});

test("a model that takes no effort offers no control", () => {
  assert.equal(hasChoices(takesNone), false);
  assert.equal(hasChoices(null), false);
  assert.equal(hasChoices(haiku), true);
});
