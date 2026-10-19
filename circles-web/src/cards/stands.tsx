import { useEffect, useState } from "react";
import { Stands, Tally } from "@kit/book/page-top";
import type { HostPage } from "@/lib/cards-api";

/** Which pages the person left "Where this stands" open on, on this device. */
const OPEN_KEY = "circles.stands.open";

function readOpen(): Record<string, boolean> {
  try {
    const v = JSON.parse(window.localStorage.getItem(OPEN_KEY) || "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, boolean>) : {};
  } catch {
    return {};
  }
}

function writeOpen(pageId: string, open: boolean): void {
  try {
    const all = readOpen();
    if (open) all[pageId] = true;
    else delete all[pageId];
    window.localStorage.setItem(OPEN_KEY, JSON.stringify(all));
  } catch {
    /* private mode: remembered for this page view only */
  }
}

/** The collapsed line's words: the first line the open box shows (the kit's
 * Stands lists Goal, then Next, then Open), or what it says instead. */
export function standsFirstLine(page: HostPage | null | undefined, error: string | null): string {
  if (page === undefined) return "Reading…";
  const w = page?.where_this_stands ?? null;
  if (w?.goal) return `Goal: ${w.goal}`;
  if (w?.next_step) return `Next: ${w.next_step}`;
  if (w?.open_work?.length) return `Open: ${w.open_work.join("; ")}`;
  if (error) return `Could not read it: ${error}`;
  return "Nothing yet";
}

/**
 * "Where this stands" on your Host's back, one line until it is opened
 * (open in full it took up half the visible chat, so it collapses). Collapsed: its label and the first line of it, cut to the
 * width; a tap opens it in full. Each page remembers which way it was left.
 *
 * Your Host's back is one conversation (slice 5), and a briefing is a
 * topic's: it shows the topic the person last wrote to, and its line says
 * which topic that is (`topic`), open or closed.
 */
export function StandsBox({
  page,
  pageId,
  error,
  seenBefore,
  topic = null,
}: {
  /** The topic's title (one conversation): named on the line. */
  topic?: string | null;
  page: HostPage | null | undefined;
  pageId: string | null;
  error: string | null;
  seenBefore: string | null | undefined;
}) {
  const [open, setOpen] = useState(() => !!pageId && !!readOpen()[pageId]);
  useEffect(() => setOpen(!!pageId && !!readOpen()[pageId]), [pageId]);
  const first = standsFirstLine(page, error);
  return (
    <div className="stands-box" data-open={open || undefined} data-stands-box>
      <div className="stands-row">
        <button
          aria-expanded={open}
          aria-label={
            open
              ? "Where this stands: close"
              : `Where this stands${topic ? ` on ${topic}` : ""}: ${first}`
          }
          className="stands-toggle"
          data-stands-toggle
          onClick={() => {
            const next = !open;
            setOpen(next);
            if (pageId) writeOpen(pageId, next);
          }}
          type="button"
        >
          <span aria-hidden className="caret">
            {open ? "▾" : "▸"}
          </span>
          <span className="k">Where this stands</span>
          {topic && (
            <span className="topic" data-stands-topic>
              {topic}
            </span>
          )}
          {!open && <span className="first">{first}</span>}
        </button>
        <div className="sf">
          <span>so far</span>
          <Tally counter="Circles" pageKey={pageId} totals={page?.totals} />
        </div>
      </div>
      {open && <Stands error={error} page={page} seenBefore={seenBefore} />}
    </div>
  );
}
