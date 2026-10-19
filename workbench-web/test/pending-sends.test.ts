import { test } from "node:test";
import assert from "node:assert/strict";
import { readIndex, sameNudge, type PendingSend } from "../src/lib/pending-sends.ts";

const held = (key: string, text: string): PendingSend => ({ key, projectId: 251, text });
const ANSWER = "Ticket #41 — I chose “Elm Lantern”.";

test("a receipt for the held answer settles it (the turn read it)", () => {
  assert.equal(readIndex([held("p:251", ANSWER)], "p:251", ANSWER), 0);
});

test("a receipt on another thread settles nothing", () => {
  assert.equal(readIndex([held("p:251", ANSWER)], "p:7", ANSWER), -1);
});

test("a receipt for other words settles nothing", () => {
  assert.equal(readIndex([held("p:251", ANSWER)], "p:251", "Ticket #42 — I chose “Tin Robot”."), -1);
});

test("the receipt matches by the text's opening, as the Circles card does", () => {
  const long = `${ANSWER.slice(0, -1)}: and keep the dock on the left, the way it was before today`;
  assert.equal(readIndex([held("p:251", long)], "p:251", long.slice(0, 60)), 0);
});

test("an empty receipt settles nothing", () => {
  assert.equal(readIndex([held("p:251", ANSWER)], "p:251", ""), -1);
});

test("nudgeKey cuts as the registry's nudge frame does: 500 code points, so any length matches its frame", async () => {
  const { nudgeKey } = await import("../src/lib/pending-sends.ts");
  const whole = ("Rework the checkout now: " + "keep the steps short, ".repeat(20) + "\u{1F642} " +
    "and the totals clear. ".repeat(10)).trim();
  // What the server's frame carries: text.strip()[:500] (Python code points).
  const frame = Array.from(whole).slice(0, 500).join("");
  assert.equal(nudgeKey(whole), nudgeKey(frame));
  assert.equal(Array.from(nudgeKey(whole)).length, 500);
  assert.notEqual(nudgeKey(whole), whole.slice(0, 500).trim()); // not a UTF-16 cut
  assert.equal(nudgeKey("  Use the red button. "), "Use the red button.");
  assert.notEqual(nudgeKey("Use the red button."), nudgeKey("Use the blue button."));
});

// A nudge is known by the id it was sent with.
// The turn reads a nudge after the platform cut any secret shape from it, so a
// pasted key comes back as [REDACTED:<id>] and its text never matches.
test("a receipt carrying the sender's id settles the send held with that id, whatever its text", () => {
  const sent = "use key sk-ant-api03-XXXXXXXX for the deploy, then go";
  const p = [{ key: "p:251", projectId: 251, text: sent, clientId: "u-1-1" }];
  assert.equal(readIndex(p, "p:251", "use key [REDACTED:anthropic_key] for the deploy, then go", "u-1-1"), 0);
  assert.equal(readIndex(p, "p:251", sent, "u-9-9"), -1); // another sender's words, same text
  assert.equal(readIndex(p, "p:7", sent, "u-1-1"), -1); // another thread
});

test("sameNudge: by the id when the server side carries one, else by nudgeKey", () => {
  assert.equal(sameNudge({ text: "a key sk-1", nudgeId: "u-1" }, { text: "a key [REDACTED:x]", nudgeId: "u-1" }), true);
  assert.equal(sameNudge({ text: "same", nudgeId: "u-1" }, { text: "same", nudgeId: "u-2" }), false);
  assert.equal(sameNudge({ text: "  same words\n" }, { text: "same words" }), true);
  assert.equal(sameNudge({ text: "same", nudgeId: "u-1" }, { text: "same" }), true);
});
