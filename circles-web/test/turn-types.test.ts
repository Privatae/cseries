import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ORIGINAL_THREAD,
  landingFrom,
  normalizeThreadId,
  selfThreadKey,
} from "../src/lib/turn-types.ts";

function headers(map: Record<string, string>) {
  const lower: Record<string, string> = {};
  for (const [k, v] of Object.entries(map)) lower[k.toLowerCase()] = v;
  return { get: (name: string) => lower[name.toLowerCase()] ?? null };
}

test("no landing header → null, so a client behaves exactly as before", () => {
  assert.equal(landingFrom(headers({}), "original"), null);
  assert.equal(landingFrom(headers({ "x-thread-model-refused": "x" }), null), null);
});

test("landed in the thread it was sent into → not moved", () => {
  const l = landingFrom(headers({ "x-landed-thread": "original" }), "original");
  assert.deepEqual(l, { threadId: "original", typeId: null, typeName: null, at: null, moved: false });
  const u = "11111111-1111-1111-1111-111111111111";
  const l2 = landingFrom(headers({ "x-landed-thread": u }), u);
  assert.equal(l2?.moved, false);
});

test("landed in another type's thread → moved, with the type named", () => {
  const u = "22222222-2222-2222-2222-222222222222";
  const l = landingFrom(
    headers({ "x-landed-thread": u, "x-landed-type": "t-1", "x-landed-type-name": "Finance" }),
    "original",
  );
  assert.deepEqual(l, { threadId: u, typeId: "t-1", typeName: "Finance", at: null, moved: true });
});

test("absent, null, 'original' and the historic 'self' are all the original conversation", () => {
  for (const v of [undefined, null, "", "original", "self", "  original "]) {
    assert.equal(normalizeThreadId(v), ORIGINAL_THREAD);
  }
  assert.equal(normalizeThreadId(" abc "), "abc");
  const l = landingFrom(headers({ "x-landed-thread": "original" }), "self");
  assert.equal(l?.moved, false);
});

test("the self thread key matches the hook's shape", () => {
  assert.equal(selfThreadKey(null), "self:original");
  assert.equal(selfThreadKey("self"), "self:original");
  assert.equal(selfThreadKey("abc"), "self:abc");
});

import { thisTurnStart } from "../src/lib/turn-types.ts";

test("thisTurnStart: the turn begins at the last user row when it is this message", () => {
  const h = [
    { role: "user", text: "old question" },
    { role: "assistant", text: "old answer" },
    { role: "user", text: "pay the invoice" },
    { role: "assistant", text: "done" },
  ];
  assert.equal(thisTurnStart(h, "pay the invoice"), 2);
  assert.equal(thisTurnStart(h, " pay the invoice "), 2);
});

test("thisTurnStart: not in the history yet → the whole history is earlier history", () => {
  const h = [
    { role: "user", text: "old question" },
    { role: "assistant", text: "old answer" },
  ];
  assert.equal(thisTurnStart(h, "pay the invoice"), 2);
  assert.equal(thisTurnStart(h, ""), 2);
  assert.equal(thisTurnStart([], "x"), 0);
});

test("thisTurnStart: only the LAST user row counts (an earlier identical message is history)", () => {
  const h = [
    { role: "user", text: "ok" },
    { role: "assistant", text: "a" },
    { role: "user", text: "something else" },
  ];
  assert.equal(thisTurnStart(h, "ok"), 3);
});

test("landing reads the server's landing moment", () => {
  const l = landingFrom(
    headers({ "x-landed-thread": "abc", "x-landed-at": "2026-09-26T10:00:00+00:00" }),
    "original",
  );
  assert.equal(l?.at, "2026-09-26T10:00:00+00:00");
});

test("thisTurnStart: the landing moment wins over identical text", () => {
  const h = [
    { role: "user", text: "ok", createdAt: "2026-09-26T09:00:00Z" },
    { role: "assistant", text: "earlier reply", createdAt: "2026-09-26T09:00:05Z" },
  ];
  // This "ok" is not saved yet: the earlier identical "ok" is history.
  assert.equal(thisTurnStart(h, "ok", "2026-09-26T10:00:00Z"), 2);
  const h2 = [...h, { role: "user", text: "ok", createdAt: "2026-09-26T10:00:01Z" }];
  assert.equal(thisTurnStart(h2, "ok", "2026-09-26T10:00:00Z"), 2);
  // Without the moment, the text rule still applies.
  assert.equal(thisTurnStart(h, "ok"), 0);
});
