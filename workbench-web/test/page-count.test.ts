import { test } from "node:test";
import assert from "node:assert/strict";
import {
  TICK_MS,
  engineLine,
  fmtK,
  fmtUsd,
  pageCount,
  replyAdds,
  shortWhen,
  spendTokens,
  tickValue,
} from "../src/lib/page-count.ts";
import type { EngineUsage, RunningTotals } from "../src/lib/types.ts";

// The running token count on a page of the book: the head's figure and its
// hover, each reply's own "+N tokens", and the engines' second line. The
// server side (context_budget.running_spend, the page route) is
// scripts/test_workbench_page_data_pg.py's.

const NOW = new Date(2026, 9, 8, 21, 49); // local time, so the labels are too

const totals = (extra: Partial<RunningTotals> = {}): RunningTotals => ({
  turns_counted: 12,
  turns_unknown: 0,
  first_counted_at: new Date(2026, 9, 3, 9, 15).toISOString(),
  calls: 41,
  tokens: { uncached: 30_100, cache_read: 160_212, cache_write: 9_900, output: 14_400 },
  tokens_total: 214_612,
  calls_usage_unknown: 0,
  est_usd: 1.8412,
  turns_priced: 12,
  turns_unpriced: 0,
  usd_partial: false,
  usd_label: "ESTIMATE — …",
  ...extra,
});

test("tokens short: 940, 21.4k, 214.6k, 1.2M; never 1000.0k", () => {
  assert.equal(fmtK(940), "940");
  assert.equal(fmtK(21_400), "21.4k");
  assert.equal(fmtK(214_612), "214.6k");
  assert.equal(fmtK(999_949), "999.9k");
  assert.equal(fmtK(999_950), "1.0M");
  assert.equal(fmtK(1_204_000), "1.2M");
  assert.equal(fmtK(0), "0");
  assert.equal(fmtK(-3), "0");
});

test("dollars as Circles prints a turn's price", () => {
  assert.equal(fmtUsd(1.8412), "$1.84");
  assert.equal(fmtUsd(0.0031), "$0.0031");
  assert.equal(fmtUsd(0.00002), "<$0.0001");
  assert.equal(fmtUsd(0), "$0");
});

test("every turn priced: tokens and dollars, '214.6k tokens · $1.84'", () => {
  const c = pageCount(totals(), NOW);
  assert.equal(c.state, "counted");
  assert.equal(c.text, "214.6k tokens · $1.84");
  assert.equal(c.tokens, 214_612);
  assert.equal(c.partial, false);
  assert.equal(c.since, null);
  assert.match(c.lines[0], /^Every Host turn on this page, added up: 200\.2k in \(160\.2k of it read from cache, 9\.9k written to cache\) and 14\.4k out, over 41 model calls\.$/);
  assert.ok(c.lines.some((l) => l.startsWith("Dollars: an estimate")));
  assert.ok(!c.lines.some((l) => l.startsWith("Partial")));
});

test("a reply without a price: the dollars are marked partial, and the hover says how many", () => {
  const c = pageCount(totals({ turns_unpriced: 2, usd_partial: true }), NOW);
  assert.equal(c.text, "214.6k tokens · $1.84 (partial)");
  assert.equal(c.partial, true);
  assert.ok(c.lines.includes("Partial: 2 replies have no price, so the dollars leave them out."));
  // one reply priced for only some of its calls (basis "partial")
  const floor = pageCount(totals({ usd_partial: true }), NOW);
  assert.equal(floor.text, "214.6k tokens · $1.84 (partial)");
  assert.ok(floor.lines.includes("Partial: some replies were priced for only part of their model calls."));
  const one = pageCount(totals({ turns_unpriced: 1, usd_partial: true }), NOW);
  assert.ok(one.lines.includes("Partial: 1 reply has no price, so the dollars leave it out."));
});

test("no reply priced: no dollars at all, and the hover says why", () => {
  const unpriced = pageCount(totals({ est_usd: null, turns_priced: 0, turns_unpriced: 12, usd_partial: true }), NOW);
  assert.equal(unpriced.text, "214.6k tokens");
  assert.equal(unpriced.usd, null);
  assert.equal(unpriced.partial, false);
  assert.ok(unpriced.lines.includes("No dollars: no reply here has a price."));
  const subscription = pageCount(totals({ est_usd: null, turns_priced: 0 }), NOW);
  assert.equal(subscription.text, "214.6k tokens");
  assert.ok(subscription.lines.includes("No dollars: replies on a subscription or a free model add none per token."));
});

test("older turns uncounted: the figure reads 'since <first counted turn>'", () => {
  const c = pageCount(totals({ turns_unknown: 30 }), NOW);
  assert.equal(c.since, "since Oct 3");
  assert.equal(c.text, "214.6k tokens since Oct 3 · $1.84");
  assert.ok(c.lines.includes("Counted since Oct 3: 30 older replies are from before the Workbench counted tokens."));
  // counted from today: the time; from another year: the year too
  const today = pageCount(totals({ turns_unknown: 1, first_counted_at: new Date(2026, 9, 8, 9, 5).toISOString() }), NOW);
  assert.equal(today.since, "since 9:05");
  assert.ok(today.lines.includes("Counted since 9:05: 1 older reply is from before the Workbench counted tokens."));
  const lastYear = pageCount(totals({ turns_unknown: 1, first_counted_at: new Date(2025, 11, 30, 9, 5).toISOString() }), NOW);
  assert.equal(lastYear.since, "since Dec 30, 2025");
  // every turn counted: no "since"
  assert.equal(pageCount(totals({ turns_unknown: 0 }), NOW).since, null);
});

test("unknown: Host replies on the page, none counted, so no figure but a plain word", () => {
  const c = pageCount(totals({ turns_counted: 0, turns_unknown: 7, tokens_total: 0, est_usd: null, turns_priced: 0 }), NOW);
  assert.equal(c.state, "unknown");
  assert.equal(c.text, "tokens not counted");
  assert.equal(c.tokens, null);
  assert.equal(c.usd, null);
  assert.match(c.lines[0], /^The 7 replies on this page are from before the Workbench counted each turn's tokens/);
  const one = pageCount(totals({ turns_counted: 0, turns_unknown: 1, tokens_total: 0 }), NOW);
  assert.match(one.lines[0], /^The 1 reply on this page is from before/);
});

test("nothing to count, or the totals could not be read: the head shows nothing", () => {
  const none = pageCount(totals({ turns_counted: 0, turns_unknown: 0, tokens_total: 0, est_usd: null }), NOW);
  assert.equal(none.state, "none");
  assert.equal(none.text, "");
  for (const t of [null, undefined]) {
    const c = pageCount(t, NOW);
    assert.equal(c.state, "unavailable");
    assert.equal(c.text, "");
  }
});

test("calls that reported no usage are named on hover: their tokens are in no figure", () => {
  const c = pageCount(totals({ calls_usage_unknown: 3 }), NOW);
  assert.ok(c.lines.includes("3 model calls reported no usage; their tokens are not in it."));
  assert.ok(pageCount(totals({ calls_usage_unknown: 1 }), NOW).lines.includes("1 model call reported no usage; its tokens are not in it."));
});

const engines = (extra: Partial<EngineUsage> = {}): EngineUsage => ({
  runs: 2,
  tokens: 1_204_000,
  tokens_covered_runs: 2,
  billed_usd: 0.31,
  est_compute_usd: 0.28,
  privatae_usd: 0.59,
  vendor_est_usd: null,
  vendor_est_runs: 0,
  ...extra,
});

test("the engines' runs launched from the page: the second line on hover", () => {
  assert.equal(engineLine(engines()), "Coding engines run from this page: 2 runs, 1.2M tokens, $0.59 in compute.");
  assert.equal(
    engineLine(engines({ runs: 3, tokens_covered_runs: 2, vendor_est_usd: 0.25 })),
    "Coding engines run from this page: 3 runs, 1.2M tokens (from 2 of 3 runs), $0.59 in compute, the engines' own estimate $0.25.",
  );
  assert.equal(
    engineLine(engines({ runs: 1, tokens: null, tokens_covered_runs: 0, privatae_usd: 0 })),
    "Coding engines run from this page: 1 run, tokens not reported.",
  );
  assert.equal(engineLine(engines({ runs: 0 })), null);
  assert.equal(engineLine(null), null);
});

test("each reply's own '+N tokens' is its spend record summed, the figure the total sums", () => {
  const spend = { tokens: { uncached: 900, cache_read: 19_000, cache_write: 1_100, output: 400 } };
  assert.equal(spendTokens(spend), 21_400);
  assert.equal(spendTokens({ tokens: { output: 12 } }), 12);
  assert.equal(spendTokens({ tokens: { output: "12" } }), null);
  assert.equal(spendTokens({}), null);
  assert.equal(spendTokens(null), null);
  assert.deepEqual(replyAdds({ spend_tokens: 21_400, est_cost_usd: 0.1842, price_basis: "list" }), {
    tokens: "+21.4k tokens",
    price: "$0.18",
  });
  assert.deepEqual(replyAdds({ spend_tokens: 900, est_cost_usd: 0.0031, price_basis: "partial" }), {
    tokens: "+900 tokens",
    price: "$0.0031 (partial)",
  });
  assert.deepEqual(replyAdds({ spend_tokens: 900, est_cost_usd: null, price_basis: "subscription" }), {
    tokens: "+900 tokens",
    price: "subscription",
  });
  // a row from before the spend record: no "+", and no price word to invent
  assert.deepEqual(replyAdds({ tokens_total: 19_400 }), { tokens: null, price: null });
  assert.deepEqual(replyAdds(undefined), { tokens: null, price: null });
});

test("the total ticks up to the new figure, easing out, and lands exactly on it", () => {
  assert.equal(tickValue(100, 200, 0, TICK_MS), 100);
  assert.equal(tickValue(100, 200, TICK_MS, TICK_MS), 200);
  assert.equal(tickValue(100, 200, TICK_MS * 2, TICK_MS), 200);
  const half = tickValue(100, 200, TICK_MS / 2, TICK_MS);
  assert.ok(half > 150 && half < 200, String(half)); // more than halfway at half time
  assert.equal(tickValue(100, 200, 50, 0), 200);
  assert.equal(TICK_MS, 700);
});

test("a moment, short: the time today, the day this year, the year before", () => {
  assert.equal(shortWhen(new Date(2026, 9, 8, 21, 41).toISOString(), NOW), "21:41");
  assert.equal(shortWhen(new Date(2026, 0, 2, 8, 0).toISOString(), NOW), "Jan 2");
  assert.equal(shortWhen(new Date(2024, 1, 29, 8, 0).toISOString(), NOW), "Feb 29, 2024");
  assert.equal(shortWhen("not a date", NOW), "");
  assert.equal(shortWhen(null, NOW), "");
});
