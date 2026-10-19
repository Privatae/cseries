import { test } from "node:test";
import assert from "node:assert/strict";
import {
  consumeCirclesStream,
  contextChanged,
  parseContextFrame,
  summarizeToolArgs,
  toolStatus,
} from "../src/lib/circles-stream.ts";

// Build a Response whose body streams the given AI SDK data-stream frames.
function streamOf(frames: string[]): Response {
  return new Response(frames.join("\n") + "\n");
}

test("parses a full turn: text deltas, tool call + result, finish", async () => {
  const frames = [
    'f:{"messageId":"m1"}',
    "2:[]",
    '0:"Hello"',
    '0:", world"',
    '9:{"toolCallId":"t1","toolName":"web_search","args":{"query":"privatae"}}',
    '2:[{"type":"session","session_id":"s-42"}]',
    'a:{"toolCallId":"t1","result":{"status":"ok","data":"hit"}}',
    'd:{"finishReason":"stop"}',
    'e:{"finishReason":"stop","isContinued":false}',
  ];

  const text: string[] = [];
  const tools: string[] = [];
  const results: { id: string; status: string }[] = [];
  const data: unknown[] = [];
  let doneText = "";
  let errored = false;

  await consumeCirclesStream(streamOf(frames), {
    onText: (delta) => text.push(delta),
    onTool: (t) => tools.push(t.toolName),
    onResult: (r) => results.push({ id: r.toolCallId, status: r.status }),
    onData: (item) => data.push(item),
    onError: () => {
      errored = true;
    },
    onDone: ({ fullText }) => {
      doneText = fullText;
    },
  });

  assert.deepEqual(text, ["Hello", ", world"]);
  assert.equal(doneText, "Hello, world");
  assert.deepEqual(tools, ["web_search"]);
  assert.deepEqual(results, [{ id: "t1", status: "ok" }]);
  assert.deepEqual(data, [{ type: "session", session_id: "s-42" }]);
  assert.equal(errored, false);
});

test("g: frames stream reasoning deltas with accumulation, apart from text", async () => {
  const frames = [
    'f:{"messageId":"m1"}',
    'g:"thinking about"',
    'g:" the answer"',
    '0:"The answer."',
    'e:{"finishReason":"stop","isContinued":false}',
  ];
  const deltas: string[] = [];
  let fullReasoning = "";
  let doneText = "";
  await consumeCirclesStream(streamOf(frames), {
    onReasoning: (delta, full) => {
      deltas.push(delta);
      fullReasoning = full;
    },
    onDone: ({ fullText }) => {
      doneText = fullText;
    },
  });
  assert.deepEqual(deltas, ["thinking about", " the answer"]);
  assert.equal(fullReasoning, "thinking about the answer");
  // Reasoning never leaks into the visible reply text.
  assert.equal(doneText, "The answer.");
});

test("g: frames are silently ignored without an onReasoning handler", async () => {
  let doneText = "";
  await consumeCirclesStream(streamOf(['g:"hmm"', '0:"reply"', "e:{}"]), {
    onDone: ({ fullText }) => {
      doneText = fullText;
    },
  });
  assert.equal(doneText, "reply");
});

test("terminal 3: frame surfaces an error and suppresses onDone", async () => {
  let err = "";
  let doneCalled = false;
  await consumeCirclesStream(streamOf(['0:"partial"', '3:"bridge wedged"']), {
    onError: (m) => {
      err = m;
    },
    onDone: () => {
      doneCalled = true;
    },
  });
  assert.equal(err, "bridge wedged");
  assert.equal(doneCalled, false);
});

test("malformed frame is dropped, stream continues", async () => {
  const text: string[] = [];
  await consumeCirclesStream(streamOf(["9:{not json}", '0:"after"']), {
    onText: (d) => text.push(d),
  });
  assert.deepEqual(text, ["after"]);
});

test("tool result status maps denied/blocked to error", () => {
  assert.equal(toolStatus({ status: "ok" }), "ok");
  assert.equal(toolStatus({ status: "denied" }), "error");
  assert.equal(toolStatus({ status: "blocked" }), "error");
  assert.equal(toolStatus(null), "running");
});

test("summarizeToolArgs picks the first meaningful key and truncates", () => {
  assert.equal(summarizeToolArgs({ query: "find me" }), "find me");
  assert.equal(summarizeToolArgs({ nothing: 1 }), "");
  assert.equal(summarizeToolArgs({ message: "x".repeat(120) }).length, 88);
});

test("2: context frame dispatches onContext normalized, still passes onData", async () => {
  const frames = [
    'f:{"messageId":"m1"}',
    '2:[{"t":"context","tokens_in":41200,"window":200000,"budget":120000,"summary_version":3,"consolidated_at":"2026-08-04T12:00:00Z"}]',
    '0:"hi"',
    "e:{}",
  ];
  const contexts: unknown[] = [];
  const data: unknown[] = [];
  await consumeCirclesStream(streamOf(frames), {
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
      est_cost_usd: null,
      price_basis: null,
      close_turn: null,
    },
  ]);
  // The raw item still rides onData like every other typed data item.
  assert.equal(data.length, 1);
  assert.equal((data[0] as { t?: string }).t, "context");
});

test("non-context 2: items never fire onContext", async () => {
  const contexts: unknown[] = [];
  await consumeCirclesStream(
    streamOf([
      '2:[{"type":"session","session_id":"s-1"}]',
      '2:[{"type":"turn","turn_id":"t-1"}]',
      "2:[]",
      "e:{}",
    ]),
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
    est_cost_usd: null,
    price_basis: null,
    close_turn: null,
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
    est_cost_usd: null,
    price_basis: null,
    close_turn: null,
  });
  // The Host's close_turn signal rides the frame (the Review button).
  const closed = parseContextFrame({ t: "context", close_turn: { goal: { met: true } } });
  assert.deepEqual(closed?.close_turn, { goal: { met: true } });
  assert.equal(parseContextFrame({ t: "context", close_turn: {} })?.close_turn, null);
  // The price rides the frame when the backend stamps it (2026-09-18).
  const priced = parseContextFrame({ t: "context", est_cost_usd: 0.0031, price_basis: "list" });
  assert.equal(priced?.est_cost_usd, 0.0031);
  assert.equal(priced?.price_basis, "list");
  assert.equal(parseContextFrame({ t: "context", est_cost_usd: "0.1" })?.est_cost_usd, null);
  // Not a context frame → null.
  assert.equal(parseContextFrame({ type: "session" }), null);
  assert.equal(parseContextFrame(null), null);
  assert.equal(parseContextFrame("context"), null);
  // Garbage field types coerce to null rather than NaN leaking into the UI.
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
  // Same stamps → not lit (ordinary turn).
  assert.equal(contextChanged(base, seen), false);
  // New consolidation timestamp → lit.
  assert.equal(
    contextChanged({ ...base, consolidated_at: "2026-08-04T12:05:00Z" }, seen),
    true,
  );
  // Consolidator ran for the first time since we looked (seen had none).
  assert.equal(
    contextChanged(base, { summary_version: null, consolidated_at: null }),
    true,
  );
  // No timestamp on the frame → version bump is the fallback signal.
  assert.equal(
    contextChanged(
      { ...base, consolidated_at: null, summary_version: 4 },
      { summary_version: 3, consolidated_at: null },
    ),
    true,
  );
  // Frame carrying neither stamp never lights.
  assert.equal(
    contextChanged({ ...base, consolidated_at: null, summary_version: null }, seen),
    false,
  );
});
