import { test } from "node:test";
import assert from "node:assert/strict";
import { costLabel, fmtUsd, metricsBits } from "../src/lib/metrics-format.ts";

test("fmtUsd keeps enough digits to compare cheap turns", () => {
  assert.equal(fmtUsd(0), "$0");
  // Too small for four decimals is never "$0" — that would read as free.
  assert.equal(fmtUsd(0.000001), "<$0.0001");
  assert.equal(fmtUsd(0.00003), "<$0.0001");
  assert.equal(fmtUsd(0.0000499), "<$0.0001");
  assert.equal(fmtUsd(0.00005), "$0.0001");
  assert.equal(fmtUsd(0.0031), "$0.0031");
  assert.equal(fmtUsd(0.003), "$0.003");
  assert.equal(fmtUsd(0.01), "$0.01");
  assert.equal(fmtUsd(0.1234), "$0.12");
  assert.equal(fmtUsd(2.5), "$2.50");
});

test("costLabel prints the basis word where a price would go, and nothing for old rows", () => {
  assert.equal(costLabel({ est_cost_usd: 0.42 }), "$0.42");
  assert.equal(costLabel({ est_cost_usd: null, price_basis: "free" }), "free");
  assert.equal(costLabel({ est_cost_usd: null, price_basis: "subscription" }), "subscription");
  assert.equal(costLabel({ est_cost_usd: null, price_basis: "unpriced" }), "unpriced");
  // A priced row wins over its basis word.
  assert.equal(costLabel({ est_cost_usd: 0.001, price_basis: "list" }), "$0.001");
  // Rows written before the stamp carry neither — print nothing, never "unpriced".
  assert.equal(costLabel({}), null);
  assert.equal(costLabel({ price_basis: "list" }), null);
});

test("costLabel says a price is partial when some calls reported no usage", () => {
  // inference.estimate_turn_cost labels such a turn 'partial': the dollars
  // cover only the calls that reported, so a bare "$0.42" would overstate it.
  assert.equal(costLabel({ est_cost_usd: 0.42, price_basis: "partial" }), "$0.42 (partial)");
  assert.equal(costLabel({ est_cost_usd: 0.42, price_basis: "list" }), "$0.42");
});

test("metricsBits: provenance, duration, tokens with cached split, then price", () => {
  const bits = metricsBits({
    model: "k3",
    provider: "kimi",
    gen_ms: 39000,
    tokens_total: 140000,
    tokens_in: 139400,
    tokens_out: 588,
    tokens_cached: 53000,
    est_cost_usd: 0.0512,
    price_basis: "list",
  });
  assert.deepEqual(bits, [
    "k3 · kimi",
    "took 39s",
    "140.0k tok (139.4k in · 53.0k cached / 588 out)",
    "$0.05",
  ]);
});

test("metricsBits: a local run says free, an old row says nothing about price", () => {
  assert.deepEqual(
    metricsBits({ model: "qwen3.5:9b", provider: "local", gen_ms: 800, tokens_in: 10, tokens_out: 5, price_basis: "free" }),
    ["qwen3.5:9b · local", "took 800ms", "15 tok (10 in / 5 out)", "free"],
  );
  assert.deepEqual(metricsBits({ model: "m", gen_ms: 1500 }), ["m", "took 1.5s"]);
  assert.deepEqual(metricsBits({}), []);
});

test("metricsBits without the model, for a line under a heading that names it", () => {
  assert.deepEqual(
    metricsBits({ model: "k3", provider: "kimi", gen_ms: 1500, est_cost_usd: 0.02 }, { withModel: false }),
    ["took 1.5s", "$0.02"],
  );
});
