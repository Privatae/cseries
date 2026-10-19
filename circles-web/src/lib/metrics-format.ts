// The quiet meta line under a turn, a regenerated alternate, or one side of
// an A/B compare: who answered · took Ns · N tok (in / out) · $price.
// One formatter for all three so a regenerate or a compare reads as a PRICE
// comparison against the original in the same words (2026-09-18).
import type { TurnMetrics } from "./types";

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

export function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/** Dollars with enough digits to compare two cheap turns: a fraction of a
 * cent keeps four decimals ("$0.0031"), a cent or more keeps two ("$0.12").
 * A genuine zero prints "$0" — only when the rate is zero. A price too small
 * for four decimals prints "<$0.0001" rather than rounding to a "$0" that
 * would read as free beside the other side's real figure. */
export function fmtUsd(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.00005) return "<$0.0001";
  if (usd < 0.01) return `$${usd.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`;
  return `$${usd.toFixed(2)}`;
}

/** The price bit, or the basis word where a price would go. Returns null when
 * the row predates the stamp (no basis, no price), so old turns print nothing
 * rather than "unpriced". */
export function costLabel(mt: Pick<TurnMetrics, "est_cost_usd" | "price_basis">): string | null {
  // "partial": some calls reported no usage, so the dollars cover only the
  // calls that did — a floor, printed as one.
  if (mt.est_cost_usd != null) {
    return mt.price_basis === "partial"
      ? `${fmtUsd(mt.est_cost_usd)} (partial)`
      : fmtUsd(mt.est_cost_usd);
  }
  switch (mt.price_basis) {
    case "free":
      return "free";
    case "subscription":
      return "subscription";
    case "unpriced":
      return "unpriced";
    default:
      return null;
  }
}

/** The bits of the meta line, in print order. Provenance first (WHO
 * ANSWERED is printed, never hover-only), then duration,
 * tokens with the cached split, then the price. `withModel: false` drops
 * the provenance bit for a line under a heading that already names the
 * model (an alternate, an A/B column). */
export function metricsBits(mt: TurnMetrics, opts: { withModel?: boolean } = {}): string[] {
  const bits: string[] = [];
  if (opts.withModel !== false) {
    if (mt.model) {
      bits.push(mt.provider && mt.provider !== mt.model ? `${mt.model} · ${mt.provider}` : mt.model);
    } else if (mt.provider) {
      bits.push(mt.provider);
    }
  }
  if (mt.gen_ms != null) bits.push(`took ${fmtDuration(mt.gen_ms)}`);
  const tok = mt.tokens_total ?? ((mt.tokens_in ?? 0) + (mt.tokens_out ?? 0) || null);
  if (tok != null && tok > 0) {
    // tokens_in is the TRUE context size (cached + uncached); the cached
    // figure shows how much of it was served from cache. Rows without the
    // cache split fall back to the plain in/out form.
    const cached = mt.tokens_cached ?? 0;
    const cachedBit = cached > 0 ? ` · ${fmtTokens(cached)} cached` : "";
    const split =
      mt.tokens_in != null && mt.tokens_out != null
        ? ` (${fmtTokens(mt.tokens_in)} in${cachedBit} / ${fmtTokens(mt.tokens_out)} out)`
        : "";
    bits.push(`${fmtTokens(tok)} tok${split}`);
  }
  const cost = costLabel(mt);
  if (cost) bits.push(cost);
  return bits;
}

/** The failover note, when the primary errored and the fallback answered. */
export function failedOverNote(mt: TurnMetrics): string | null {
  const fo = mt.failed_over;
  if (!fo || !(fo.from_provider || fo.from_model)) return null;
  const from = [fo.from_model, fo.from_provider].filter(Boolean).join(" · ");
  return `failed over from ${from}${fo.primary_error ? ` — ${fo.primary_error}` : ""}`;
}
