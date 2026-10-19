import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CRISIS_NOTICE_KEEP,
  dismissCrisisNotice,
  isCrisisFrame,
  parseCrisisNotice,
  raiseCrisisNotice,
} from "../crisis-notice.ts";

// The platform's crisis notice as each app remembers it on the device
// (app-web-kit/crisis-notice.ts): up on a turn's governance frame, until the
// person dismisses it — across a reload — and a dismissed turn's replayed
// frames never raise it again.

test("only the governance frame of kind crisis raises the notice", () => {
  assert.equal(isCrisisFrame({ type: "governance", kind: "crisis" }), true);
  assert.equal(isCrisisFrame({ type: "governance", kind: "other" }), false);
  assert.equal(isCrisisFrame({ type: "session", kind: "crisis" }), false);
  assert.equal(isCrisisFrame(null), false);
  assert.equal(isCrisisFrame("governance"), false);
});

test("a frame raises it; a reload reads it back up until it is dismissed", () => {
  const up = raiseCrisisNotice(parseCrisisNotice(null), "turn-1");
  assert.equal(up.pending, "turn-1");
  // The reload: what was stored is read back.
  assert.equal(parseCrisisNotice(JSON.stringify(up)).pending, "turn-1");
  const down = dismissCrisisNotice(up);
  assert.equal(down.pending, null);
  assert.deepEqual(down.dismissed, ["turn-1"]);
  assert.equal(parseCrisisNotice(JSON.stringify(down)).pending, null);
});

test("a dismissed turn's replayed frame does not raise it; another turn's does", () => {
  const down = dismissCrisisNotice(raiseCrisisNotice(parseCrisisNotice(null), "turn-1"));
  assert.equal(raiseCrisisNotice(down, "turn-1").pending, null);
  assert.equal(raiseCrisisNotice(down, "turn-2").pending, "turn-2");
});

test("dismissing with nothing up changes nothing; the kept list is bounded", () => {
  const none = parseCrisisNotice(null);
  assert.deepEqual(dismissCrisisNotice(none), none);
  let s = parseCrisisNotice(null);
  for (let i = 0; i < CRISIS_NOTICE_KEEP + 5; i++) s = dismissCrisisNotice(raiseCrisisNotice(s, `t${i}`));
  assert.equal(s.dismissed.length, CRISIS_NOTICE_KEEP);
  assert.equal(s.dismissed.at(-1), `t${CRISIS_NOTICE_KEEP + 4}`);
});

test("a stored value that is not the notice's shape reads as nothing up", () => {
  for (const raw of ["", "not json", "[]", "42", '{"pending": 7, "dismissed": "x"}']) {
    assert.deepEqual(parseCrisisNotice(raw), { pending: null, dismissed: [] });
  }
});
