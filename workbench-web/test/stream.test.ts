import { test } from "node:test";
import assert from "node:assert/strict";
import {
  consumeStream,
  contextChanged,
  parseContextFrame,
} from "../src/lib/stream.ts";

// Build a Response whose body streams the given AI SDK data-stream frames.
function streamOf(frames: string[]): Response {
  return new Response(frames.join("\n") + "\n");
}

test("2: context frame dispatches onContext normalized, still passes onData", async () => {
  const frames = [
    'f:{"messageId":"m1"}',
    '2:[{"t":"context","tokens_in":41200,"window":200000,"budget":120000,"summary_version":3,"consolidated_at":"2026-08-04T12:00:00Z"}]',
    '0:"hi"',
    "e:{}",
  ];
  const contexts: unknown[] = [];
  const data: unknown[] = [];
  await consumeStream(streamOf(frames), {
    onContext: (f) => contexts.push(f),
    onData: (item) => data.push(item),
  });
  assert.deepEqual(contexts, [
    {
      tokens_in: 41200,
      window: 200000,
      budget: 120000,
      summary_version: 3,
      consolidated_at: "2026-08-04T12:00:00Z",
      model: null,
      provider: null,
      failed_over: null,
      ledger: null,
      close_turn: null,
      est_cost_usd: null,
      price_basis: null,
    },
  ]);
  // The raw item still rides onData like every other typed data item.
  assert.equal(data.length, 1);
  assert.equal((data[0] as { t?: string }).t, "context");
});

test("non-context 2: items never fire onContext", async () => {
  const contexts: unknown[] = [];
  await consumeStream(
    streamOf(['2:[{"type":"turn","turn_id":"t-1"}]', "2:[]", "e:{}"]),
    { onContext: (f) => contexts.push(f) },
  );
  assert.deepEqual(contexts, []);
});

test("parseContextFrame tolerates missing/null fields (lagging backend)", () => {
  assert.deepEqual(parseContextFrame({ t: "context", tokens_in: 900 }), {
    tokens_in: 900,
    window: null,
    budget: null,
    summary_version: null,
    consolidated_at: null,
    model: null,
    provider: null,
    failed_over: null,
    ledger: null,
    close_turn: null,
    est_cost_usd: null,
    price_basis: null,
  });
  assert.deepEqual(parseContextFrame({ t: "context" }), {
    tokens_in: null,
    window: null,
    budget: null,
    summary_version: null,
    consolidated_at: null,
    model: null,
    provider: null,
    failed_over: null,
    ledger: null,
    close_turn: null,
    est_cost_usd: null,
    price_basis: null,
  });
  // The Host's close_turn signal rides the frame (the Review button).
  const closed = parseContextFrame({ t: "context", close_turn: { type: { name: "desktop edit" } } });
  assert.deepEqual(closed?.close_turn, { type: { name: "desktop edit" } });
  assert.equal(parseContextFrame({ t: "context", close_turn: {} })?.close_turn, null);
  assert.equal(parseContextFrame({ t: "context", close_turn: ["x"] })?.close_turn, null);
  assert.equal(parseContextFrame({ type: "session" }), null);
  assert.equal(parseContextFrame(null), null);
  assert.equal(parseContextFrame("context"), null);
  assert.equal(parseContextFrame({ t: "context", window: "200k" })?.window, null);
});

test("contextChanged lights only when the consolidator stamps move", () => {
  const seen = { summary_version: 3, consolidated_at: "2026-08-04T12:00:00Z" };
  const base = {
    tokens_in: 1,
    window: 1,
    budget: null,
    summary_version: 3,
    consolidated_at: "2026-08-04T12:00:00Z",
  };
  assert.equal(contextChanged(base, seen), false);
  assert.equal(
    contextChanged({ ...base, consolidated_at: "2026-08-04T12:05:00Z" }, seen),
    true,
  );
  assert.equal(
    contextChanged(base, { summary_version: null, consolidated_at: null }),
    true,
  );
  assert.equal(
    contextChanged(
      { ...base, consolidated_at: null, summary_version: 4 },
      { summary_version: 3, consolidated_at: null },
    ),
    true,
  );
  assert.equal(
    contextChanged({ ...base, consolidated_at: null, summary_version: null }, seen),
    false,
  );
});

test("the context frame keeps the turn's price and its spend record (the book's per-reply count)", () => {
  const f = parseContextFrame({
    t: "context",
    est_cost_usd: 0.1842,
    price_basis: "list",
    ledger: { calls: 3, spend: { calls: 3, tokens: { uncached: 900, cache_read: 19000, cache_write: 1100, output: 400 } } },
  });
  assert.equal(f?.est_cost_usd, 0.1842);
  assert.equal(f?.price_basis, "list");
  assert.deepEqual(f?.ledger?.spend?.tokens, { uncached: 900, cache_read: 19000, cache_write: 1100, output: 400 });
  assert.equal(parseContextFrame({ t: "context", est_cost_usd: "0.18" })?.est_cost_usd, null);
});
