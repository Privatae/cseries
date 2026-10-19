// The running token count on a page of the book: the page's head shows
// every Host turn on it added up, and each reply shows what it added.
// Pure, so test/page-count.test.ts runs it under plain `node --test`.
//
// The count's formatting is the book's, shared with Circles
// (app-web-kit/book/page-count.ts, imported by a relative path: plain
// `node --test` cannot read the app's @kit alias). What is the Workbench's:
//   - GET /api/projects/{id}/page `totals` (context_budget.running_spend), in
//     the Workbench's words;
//   - the same page's `engines` (compute.project_run_usage): the coding-engine
//     runs launched from it, the second line on hover.

import { pageCount as countOf, fmtK, fmtUsd, type PageCount } from "../../../app-web-kit/book/page-count.ts";
import type { EngineUsage, RunningTotals } from "./types";

export {
  TICK_MS,
  fmtK,
  fmtUsd,
  replyAdds,
  shortWhen,
  spendTokens,
  tickValue,
} from "../../../app-web-kit/book/page-count.ts";
export type { PageCount } from "../../../app-web-kit/book/page-count.ts";

/** Who counts, in the hover's words. */
export const COUNTER = "the Workbench";

/** The page's running count, from GET /api/projects/{id}/page `totals`
 * (app-web-kit/book/page-count.ts pageCount, in the Workbench's words). */
export function pageCount(totals: RunningTotals | null | undefined, now: Date = new Date()): PageCount {
  return countOf(totals, COUNTER, now);
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The second line on hover: the coding-engine runs launched from the page,
 * as the Compute app counts and prices them. Null when none ran. */
export function engineLine(engines: EngineUsage | null | undefined): string | null {
  if (!engines || !engines.runs) return null;
  const bits = [plural(engines.runs, "run", "runs")];
  if (engines.tokens != null) {
    const covered = engines.tokens_covered_runs ?? engines.runs;
    bits.push(
      `${fmtK(engines.tokens)} tokens` + (covered < engines.runs ? ` (from ${covered} of ${engines.runs} runs)` : ""),
    );
  } else {
    bits.push("tokens not reported");
  }
  if ((engines.privatae_usd ?? 0) > 0) bits.push(`${fmtUsd(engines.privatae_usd)} in compute`);
  if (engines.vendor_est_usd != null) bits.push(`the engines' own estimate ${fmtUsd(engines.vendor_est_usd)}`);
  return `Coding engines run from this page: ${bits.join(", ")}.`;
}
