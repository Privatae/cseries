// The running token count on a page of the book: the page's head shows
// every Host turn on it added up, and each reply shows what it added.
// Pure, type-only imports, so `node --test` runs it
// (workbench-web/test/page-count.test.ts, through lib/page-count.ts).
//
// What it reads:
//   - the page's `totals` (brain/mastercode/context_budget.py running_spend,
//     which sums any conversation's scope): tokens from each turn's spend
//     record, summed; dollars from the price each reply prints
//     (est_cost_usd), summed, an estimate;
//   - a reply's own row: context_ledger.spend.tokens and est_cost_usd, or,
//     for the turn that just streamed, the same two off its context frame.

/** The conversation's running totals (brain/mastercode/context_budget.py
 * running_spend): tokens from each turn's own spend record, summed; dollars
 * from the price each reply prints (est_cost_usd), summed, an estimate. */
export interface RunningTotals {
  /** Host replies whose tokens are counted (they carry a spend record). */
  turns_counted: number;
  /** Host replies from before the record rode the turn: in no figure. */
  turns_unknown: number;
  /** The oldest counted turn: where the total starts when some are unknown. */
  first_counted_at: string | null;
  calls: number;
  tokens: { uncached: number; cache_read: number; cache_write: number; output: number };
  tokens_total: number;
  /** Calls whose provider reported no usage: their tokens are in no figure. */
  calls_usage_unknown: number;
  /** The replies' prices summed; null when no reply carries one. */
  est_usd: number | null;
  turns_priced: number;
  /** Host replies whose price nobody knows (free and subscription ones are
   * not among them: they add no dollars). */
  turns_unpriced: number;
  /** est_usd leaves something out: an unpriced reply, or a reply priced for
   * only some of its calls. */
  usd_partial: boolean;
  usd_label: string;
}

/** Tokens, short: 940, 21.4k, 214.6k, 1.2M. */
export function fmtK(n: number): string {
  const v = Math.max(0, Math.round(n));
  if (v >= 999_950) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1000) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

/** Dollars as the Circles meta line prints a turn's price
 * (circles-web/src/lib/metrics-format.ts fmtUsd): four decimals under a cent,
 * two from a cent up, "$0" only for a real zero. */
export function fmtUsd(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.00005) return "<$0.0001";
  if (usd < 0.01) return `$${usd.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`;
  return `$${usd.toFixed(2)}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A moment, short, in the person's own time: "21:41" today, "Oct 3" this
 * year, "Oct 3, 2025" before. "" when unreadable. */
export function shortWhen(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  if (d.toDateString() === now.toDateString()) {
    return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  const day = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return d.getFullYear() === now.getFullYear() ? day : `${day}, ${d.getFullYear()}`;
}

/** A spend record's tokens, summed over its four kinds (uncached, read from
 * cache, written to cache, output): the figure the page's total sums for the
 * turn. Null when there is no record or no number in it. */
export function spendTokens(
  spend: { tokens?: Record<string, unknown> | null } | null | undefined,
): number | null {
  const t = spend?.tokens;
  if (!t || typeof t !== "object") return null;
  let sum = 0;
  let any = false;
  for (const k of ["uncached", "cache_read", "cache_write", "output"]) {
    const v = (t as Record<string, unknown>)[k];
    if (typeof v === "number" && Number.isFinite(v)) {
      sum += v;
      any = true;
    }
  }
  return any ? sum : null;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export interface PageCount {
  /** counted: there are counted turns. unknown: Host replies, none of them
   * counted (all from before the count). none: no Host turn on the page.
   * unavailable: the page's totals could not be read. */
  state: "counted" | "unknown" | "none" | "unavailable";
  /** What the head shows: "214.6k tokens · $1.84". "" = show nothing. */
  text: string;
  tokens: number | null;
  /** "since Oct 3" when older turns are not counted; else null. */
  since: string | null;
  /** "$1.84", "$1.84 (partial)", or null when no reply carries a price. */
  usd: string | null;
  /** The dollars leave something out. */
  partial: boolean;
  /** What the figure is made of, for the hover, one sentence a line. */
  lines: string[];
}

/** The page's running count, from its `totals`.
 *
 * Dollars show plain only when every reply that costs anything was priced;
 * otherwise they are marked partial. Replies on a subscription or a free
 * model add none and are not missing a price. When some replies are from
 * before the count existed, the figure starts at the oldest counted turn and
 * says so ("since Oct 3"). `counter` names who counts in the hover's words
 * ("from before the Workbench counted tokens"). */
export function pageCount(
  totals: RunningTotals | null | undefined,
  counter: string,
  now: Date = new Date(),
): PageCount {
  if (!totals) {
    return { state: "unavailable", text: "", tokens: null, since: null, usd: null, partial: false, lines: [] };
  }
  const counted = totals.turns_counted ?? 0;
  const unknown = totals.turns_unknown ?? 0;
  if (counted === 0) {
    if (unknown === 0) {
      return { state: "none", text: "", tokens: null, since: null, usd: null, partial: false, lines: [] };
    }
    return {
      state: "unknown",
      text: "tokens not counted",
      tokens: null,
      since: null,
      usd: null,
      partial: false,
      lines: [
        `The ${plural(unknown, "reply", "replies")} on this page ${unknown === 1 ? "is" : "are"} from before ` +
          `${counter} counted each turn's tokens, so there is no total yet. The next turn starts one.`,
      ],
    };
  }
  const tokens = totals.tokens_total ?? 0;
  const t = totals.tokens ?? { uncached: 0, cache_read: 0, cache_write: 0, output: 0 };
  const input = (t.uncached ?? 0) + (t.cache_read ?? 0) + (t.cache_write ?? 0);
  const since = unknown > 0 && totals.first_counted_at ? `since ${shortWhen(totals.first_counted_at, now)}` : null;
  const partial = totals.est_usd != null && !!totals.usd_partial;
  const usd = totals.est_usd != null ? `${fmtUsd(totals.est_usd)}${partial ? " (partial)" : ""}` : null;

  const cacheBits: string[] = [];
  if (t.cache_read) cacheBits.push(`${fmtK(t.cache_read)} of it read from cache`);
  if (t.cache_write) cacheBits.push(`${fmtK(t.cache_write)} written to cache`);
  const lines = [
    `Every Host turn on this page, added up: ${fmtK(input)} in` +
      (cacheBits.length ? ` (${cacheBits.join(", ")})` : "") +
      ` and ${fmtK(t.output ?? 0)} out, over ${plural(totals.calls ?? 0, "model call", "model calls")}.`,
  ];
  if (since) {
    lines.push(
      `Counted ${since}: ${plural(unknown, "older reply is", "older replies are")} from before ${counter} ` +
        "counted tokens.",
    );
  }
  if ((totals.calls_usage_unknown ?? 0) > 0) {
    const n = totals.calls_usage_unknown;
    lines.push(`${plural(n, "model call", "model calls")} reported no usage; ${n === 1 ? "its" : "their"} tokens are not in it.`);
  }
  if (totals.est_usd != null) {
    lines.push("Dollars: an estimate, each reply's list price added up. Never a bill.");
    if (partial) {
      const u = totals.turns_unpriced ?? 0;
      lines.push(
        u > 0
          ? `Partial: ${plural(u, "reply has", "replies have")} no price, so the dollars leave ${u === 1 ? "it" : "them"} out.`
          : "Partial: some replies were priced for only part of their model calls.",
      );
    }
  } else if ((totals.turns_unpriced ?? 0) > 0) {
    lines.push("No dollars: no reply here has a price.");
  } else {
    lines.push("No dollars: replies on a subscription or a free model add none per token.");
  }
  return {
    state: "counted",
    text: `${fmtK(tokens)} tokens${since ? ` ${since}` : ""}${usd ? ` · ${usd}` : ""}`,
    tokens,
    since,
    usd,
    partial,
    lines,
  };
}

/** What a reply carries about its cost: its spend record's tokens, its
 * list-price estimate, and the word that stands where no price does. */
export interface ReplyCost {
  spend_tokens?: number | null;
  est_cost_usd?: number | null;
  price_basis?: string | null;
}

/** What one reply added to its page: "+21.4k tokens" and its price, the
 * price the way Circles prints it (a basis word where there is none). Tokens
 * are null for a row from before the spend record (it is in no total, and
 * its meta line keeps its plain token figure); price null for a row with
 * neither a price nor a basis. */
export function replyAdds(mt: ReplyCost | null | undefined): { tokens: string | null; price: string | null } {
  if (!mt) return { tokens: null, price: null };
  const tokens = mt.spend_tokens != null ? `+${fmtK(mt.spend_tokens)} tokens` : null;
  let price: string | null = null;
  if (mt.est_cost_usd != null) {
    price = mt.price_basis === "partial" ? `${fmtUsd(mt.est_cost_usd)} (partial)` : fmtUsd(mt.est_cost_usd);
  } else if (mt.price_basis === "free" || mt.price_basis === "subscription" || mt.price_basis === "unpriced") {
    price = mt.price_basis;
  }
  return { tokens, price };
}

/** The count at a moment of a tick: from `from` to `to` over `ms`, easing
 * out (the mockup's count-up when a turn lands). */
export function tickValue(from: number, to: number, elapsedMs: number, ms: number): number {
  if (ms <= 0 || elapsedMs >= ms) return to;
  const k = Math.max(0, elapsedMs) / ms;
  return from + (to - from) * (1 - (1 - k) ** 3);
}

/** How long the count takes to tick up to a new total. */
export const TICK_MS = 700;
