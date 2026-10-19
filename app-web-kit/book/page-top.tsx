import { useEffect, useId, useRef, useState } from "react";
import { TICK_MS, fmtK, pageCount, tickValue, type RunningTotals } from "./page-count.ts";
import { changedSince, isLater, prefersReducedMotion } from "./rules.ts";

/** The goal and the last turn's handoff (brain/mastercode/runtime/
 * close_turn.py thread_state_parts, which reads any conversation's scope):
 * what the next turn starts from, as fields, beside the text the turn gets
 * (`text`). */
export interface WhereThisStands {
  text: string | null;
  goal: string | null;
  goal_at: string | null;
  next_step: string | null;
  open_work: string[];
  identifiers: string[];
  handoff_at: string | null;
}

/**
 * The page's running token count: every Host turn on it added up, from the
 * page's `totals` (context_budget.running_spend), worded by
 * book/page-count.ts ("214.6k tokens · $1.84"; the dollars marked partial
 * when a reply has no price; "since Oct 3" when older turns were never
 * counted). Hover or focus shows what it is made of, and, when the app has
 * one, a second line of its own (`extra`). When a turn lands the page is read
 * again and the figure ticks up to the new total. `pageKey` is the page the
 * totals are for: a new page shows its figure, never ticks to it.
 */
export function Tally({
  totals,
  pageKey,
  counter,
  extra,
}: {
  totals: RunningTotals | null | undefined;
  pageKey: string | number | null;
  /** Who counts, in the hover's words (pageCount). */
  counter: string;
  extra?: string | null;
}) {
  const count = pageCount(totals, counter);
  const tipId = useId();
  const target = count.tokens;
  const [shown, setShown] = useState<number | null>(target);
  const [bump, setBump] = useState(false);
  const lastRef = useRef<{ pageKey: string | number | null; tokens: number | null }>({ pageKey, tokens: target });
  useEffect(() => {
    const last = lastRef.current;
    lastRef.current = { pageKey, tokens: target };
    // Ticks only when this page's total grows; another page, or a first
    // reading, just shows.
    if (last.pageKey !== pageKey || last.tokens == null || target == null || target <= last.tokens) {
      setShown(target);
      return;
    }
    setBump(true);
    const off = window.setTimeout(() => setBump(false), TICK_MS + 900);
    if (prefersReducedMotion(window)) {
      setShown(target);
      return () => window.clearTimeout(off);
    }
    const from = last.tokens;
    const t0 = performance.now();
    let raf = 0;
    const step = () => {
      const elapsed = performance.now() - t0;
      setShown(tickValue(from, target, elapsed, TICK_MS));
      if (elapsed < TICK_MS) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(off);
      setShown(target);
    };
  }, [pageKey, target]);

  if (!count.text && !extra) return null;
  const text =
    count.state === "counted" && shown != null
      ? `${fmtK(shown)} tokens${count.since ? ` ${count.since}` : ""}${count.usd ? ` · ${count.usd}` : ""}`
      : count.text || "no Host turns yet";
  return (
    <span className="book-tally-wrap">
      <button
        aria-describedby={tipId}
        className="book-tally"
        data-bump={bump || undefined}
        data-partial={count.partial || undefined}
        data-state={count.state}
        data-tally
        data-total={count.tokens ?? undefined}
        type="button"
      >
        {text}
      </button>
      <span className="book-tip" data-tally-tip id={tipId} role="tooltip">
        {count.lines.map((l) => (
          <span key={l}>{l}</span>
        ))}
        {extra && (
          <span className="book-tip-engines" data-tally-engines>
            {extra}
          </span>
        )}
      </span>
    </span>
  );
}

function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString()
    ? `at ${time}`
    : `on ${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

/**
 * "Where this stands": the conversation's goal and what its last turn handed
 * the next one (the page's where_this_stands, which is
 * close_turn.thread_state_parts: the same rows the next turn starts from).
 * What was written after the person last had the page open is tinted;
 * `seenBefore` is the page's seen mark as it stood when this opening began.
 */
export function Stands({
  page,
  seenBefore,
  error,
}: {
  /** undefined while the first read is on its way. */
  page: { where_this_stands?: WhereThisStands | null } | null | undefined;
  seenBefore: string | null | undefined;
  error: string | null;
}) {
  const w = page?.where_this_stands ?? null;
  const goalNew = changedSince(w?.goal_at, seenBefore);
  const handoffNew = changedSince(w?.handoff_at, seenBefore);
  const lines: Array<{ key: string; label: string; text: string; tint: boolean }> = [];
  if (w?.goal) lines.push({ key: "goal", label: "Goal", text: w.goal, tint: goalNew });
  if (w?.next_step) lines.push({ key: "next", label: "Next", text: w.next_step, tint: handoffNew });
  if (w?.open_work?.length) {
    lines.push({ key: "open", label: "Open", text: w.open_work.join("; "), tint: handoffNew });
  }
  const updated = isLater(w?.handoff_at, w?.goal_at) ? w?.handoff_at : (w?.goal_at ?? null);
  return (
    <>
      <section aria-label="Where this stands" className="book-stands" data-stands>
        <div className="book-k">WHERE THIS STANDS</div>
        {page === undefined ? (
          <p className="book-stands-empty">Reading…</p>
        ) : lines.length > 0 ? (
          <ul>
            {lines.map((l) => (
              <li data-line={l.key} data-tinted={l.tint || undefined} key={l.key}>
                <b>{l.label}</b>
                <span className={l.tint ? "book-hl" : undefined}>{l.text}</span>
              </li>
            ))}
            {w?.identifiers?.length ? (
              <li data-line="ids">
                <span className="book-ids">{w.identifiers.join(" · ")}</span>
              </li>
            ) : null}
          </ul>
        ) : (
          <p className="book-stands-empty">
            {error
              ? `Could not read where this stands: ${error}`
              : "Nothing yet. It fills in when the Host ends its first turn here."}
          </p>
        )}
      </section>
      {updated && (
        <div className="book-upd">
          The Host updated this {clock(updated)}
          {goalNew || handoffNew ? " · tinted: changed since you last looked" : ""}
        </div>
      )}
    </>
  );
}
