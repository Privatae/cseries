import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applySessionEvent,
  isLiveRun,
  moaChips,
  rungLabel,
} from "../src/lib/run-live.ts";
import type { RunSession } from "../src/lib/types.ts";

const session = (over: Partial<RunSession> = {}): RunSession => ({
  session_id: "abc",
  status: "running",
  prompt: "build the thing",
  per_harness: {
    "claude-code": { state: "running", model: "m1", error_type: undefined },
    codex: { state: "queued" },
  },
  ...over,
});

test("applySessionEvent merges a slim frame over the polled row", () => {
  const list = [
    session({
      per_harness: {
        "claude-code": {
          state: "running",
          model: "m1",
          error_type: "execution_error",
          elapsed_seconds: 12,
        },
      },
    }),
  ];
  const next = applySessionEvent(list, {
    session_id: "abc",
    status: "running",
    per_harness: { "claude-code": { state: "done", diff_bytes: 42 } },
  });
  const row = next[0].per_harness?.["claude-code"];
  assert.equal(row?.state, "done");
  assert.equal(row?.diff_bytes, 42);
  // slim frames omit these — the polled values must survive the merge
  assert.equal(row?.error_type, "execution_error");
  assert.equal(row?.elapsed_seconds, 12);
  assert.equal(row?.model, "m1");
});

test("applySessionEvent updates rollup status + has_picks, keeps others", () => {
  const list = [session(), session({ session_id: "other", status: "queued" })];
  const next = applySessionEvent(list, {
    session_id: "abc",
    status: "done",
    has_picks: true,
    per_harness: {},
  });
  assert.equal(next[0].status, "done");
  assert.equal(next[0].has_picks, true);
  assert.equal(next[1].status, "queued");
  assert.equal(next[1], list[1]); // untouched row keeps identity
});

test("applySessionEvent ignores unknown session ids (poll introduces rows)", () => {
  const list = [session()];
  const next = applySessionEvent(list, {
    session_id: "nope",
    status: "done",
    per_harness: {},
  });
  assert.equal(next, list); // no-op keeps list identity — no re-render
});

test("applySessionEvent adds variants the poll has not seen yet", () => {
  const list = [session({ per_harness: {} })];
  const next = applySessionEvent(list, {
    session_id: "abc",
    status: "running",
    per_harness: { "codex-s2": { state: "running" } },
  });
  assert.equal(next[0].per_harness?.["codex-s2"]?.state, "running");
});

test("isLiveRun matches the rollup's live states only", () => {
  assert.equal(isLiveRun("queued"), true);
  assert.equal(isLiveRun("running"), true);
  assert.equal(isLiveRun("done"), false);
  assert.equal(isLiveRun("partial"), false);
  assert.equal(isLiveRun(undefined), false);
});

test("moaChips renders only what the manifest recorded", () => {
  assert.deepEqual(moaChips(session()), []); // pre-MoA session: no chips
  assert.deepEqual(
    moaChips(
      session({
        samples: { codex: 3, "claude-code": 1 },
        vcpus: 4,
        max_minutes: 8,
        seeded: true,
      }),
    ),
    ["codex ×3", "4 vCPU", "model wait 8m", "seeded"],
  );
  assert.deepEqual(moaChips(session({ seeded: false })), ["greenfield"]);
});

test("rungLabel is compact and maps '*' to the full bench", () => {
  assert.equal(
    rungLabel({ harness: "codex", model: "m2", samples: 3, max_minutes: 8 }),
    "codex · m2 · ×3 · 8m",
  );
  assert.equal(rungLabel({ harness: "*", max_minutes: 10 }), "full bench · 10m");
  assert.equal(rungLabel({ harness: "grok", samples: 1 }), "grok");
});
