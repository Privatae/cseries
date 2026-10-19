import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { timeAgo } from "@kit/utils";
import {
  baselineOf,
  newlyLit,
  tabKeyTarget,
  tabLight,
  tabNote,
  type TabLight,
} from "@/lib/book";
import { BOOK_DRAG_TYPE } from "@/lib/book-index";
import type { Project } from "@/lib/types";

/** The id of the book's one page, the panel every tab controls
 * (book-app.tsx). */
export const BOOK_PAGE_ID = "book-page";

/** The id of the right side ("This page" and the bench), which the phone's
 * strip opens as a sheet over the page. */
export const BOOK_RIGHT_ID = "book-right";

/** Put the keyboard's focus on a page's tab, when it has one. */
export function focusTab(id: number): void {
  document.querySelector<HTMLElement>(`.book-tabs [data-tab="${id}"]`)?.focus();
}

/** How long a polite word stays in the status line. Cleared after, so the same
 * words said again later are a change a screen reader reads out. */
const SAID_MS = 8_000;

/**
 * The tabs on the book's edge: the first seven pages of the book's order
 * (lib/book.ts tabPages), each lit green when something landed since the
 * person last had it open, amber while something waits on them, and with
 * working dots while the Host has a turn running there. Clicking one turns to
 * it; dragging one onto a chapter of the Index files its page there. Under
 * them, the Index tab, which turns to the Index (slice 5).
 *
 * A vertical tablist (WAI-ARIA tabs pattern): the page in front is the
 * selected tab and the one tab stop; ↓ and ↑ turn to the next and the previous
 * tab, Home and End to the first and the last, and the keyboard's focus moves
 * with them; ← and → turn pages from here as from anywhere in the book. A tab
 * that lights is said once, politely, in a status line a screen reader reads.
 *
 * On a phone (slice 3b) the same tabs are a strip along the bottom, with the
 * Index tab and a "This page" tab for the right side in it: a horizontal
 * tablist, where ← and → (the book's own page turns) move along it.
 */
export function BookTabs({
  tabs,
  openId,
  indexOpen,
  working,
  baselines,
  phone,
  sheetOpen,
  onOpen,
  onIndex,
  onSheet,
  onDragTab,
}: {
  tabs: Project[];
  openId: number | null;
  /** The Index is in front (slice 5): no page's tab is, the Index tab is. */
  indexOpen: boolean;
  working: ReadonlySet<number>;
  baselines: ReadonlyMap<number, string | null>;
  /** The phone layout: the tabs are a strip along the bottom. */
  phone: boolean;
  /** The phone's sheet (the right side over the page) is open. */
  sheetOpen: boolean;
  /** Turn to a page; true when the book turned to it (a press during a turn is
   * dropped). */
  onOpen: (id: number) => boolean;
  onIndex: () => void;
  /** The phone's "This page" tab: the right side, over the page. */
  onSheet: () => void;
  /** A tab is being dragged (its page id), or the drag ended (null): the
   * Index's chapters take it (src/book/index-page.tsx). Desktop only; a
   * phone's touch drags nothing. */
  onDragTab: (id: number | null) => void;
}) {
  const shown = tabs.map((row) => {
    const open = row.id === openId && !indexOpen;
    const light = tabLight(row, open, baselineOf(baselines, row.id));
    const isWorking = working.has(row.id);
    return { row, open, light, isWorking, note: tabNote(row, light, isWorking) };
  });
  // The one tab stop: the page in front's tab, or the first tab while the
  // page in front has none (one past the seventh).
  const stop = shown.some((t) => t.open) ? openId : (shown[0]?.row.id ?? null);

  // A tab that lights is said once. The first reading only sets what the
  // tabs read now: their names carry it.
  const [said, setSaid] = useState("");
  const lightsRef = useRef<Map<number, TabLight> | null>(null);
  const lightsKey = shown.map((t) => `${t.row.id}:${t.light ?? ""}`).join(",");
  useEffect(() => {
    if (shown.length === 0) return;
    const next = new Map(shown.map((t) => [t.row.id, t.light] as const));
    const lit = newlyLit(lightsRef.current, next);
    lightsRef.current = next;
    if (lit.length === 0) return;
    setSaid(
      shown
        .filter((t) => lit.includes(t.row.id))
        .map((t) => `${t.row.name}: ${t.note}`)
        .join(". "),
    );
    // `shown` is read through lightsKey: what is said changes only with a light.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lightsKey]);
  useEffect(() => {
    if (!said) return;
    const t = window.setTimeout(() => setSaid(""), SAID_MS);
    return () => window.clearTimeout(t);
  }, [said]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    // Along a strip, ↓ and ↑ go nowhere; ← and → are the book's.
    if (phone && (e.key === "ArrowDown" || e.key === "ArrowUp")) return;
    const from = (e.target as HTMLElement).closest<HTMLElement>("[data-tab]")?.dataset.tab;
    const to = tabKeyTarget(
      shown.map((t) => t.row.id),
      from ? Number(from) : null,
      e.key,
    );
    if (to == null) return;
    e.preventDefault();
    if (onOpen(to)) focusTab(to);
  };

  return (
    <div className="book-edge" data-book-edge>
      <div
        aria-label="Pages"
        aria-orientation={phone ? "horizontal" : "vertical"}
        className="book-tabs"
        onKeyDown={onKeyDown}
        role="tablist"
      >
        {shown.map(({ row, open, light, isWorking, note }) => {
          const when = light === null && !isWorking && row.last_activity_at ? timeAgo(row.last_activity_at) : "";
          return (
            <button
              aria-controls={BOOK_PAGE_ID}
              aria-selected={open}
              className="book-tab"
              data-light={light ?? undefined}
              data-tab={row.id}
              data-working={isWorking || undefined}
              draggable={!phone}
              key={row.id}
              onClick={() => onOpen(row.id)}
              onDragEnd={() => onDragTab(null)}
              onDragStart={(e) => {
                e.dataTransfer.setData(BOOK_DRAG_TYPE, String(row.id));
                e.dataTransfer.effectAllowed = "move";
                // Marked on the next task, after the browser has started the
                // drag (change nothing under the pointer while dragstart
                // runs, or Chromium and WebKit drop the drag).
                window.setTimeout(() => onDragTab(row.id), 0);
              }}
              role="tab"
              tabIndex={row.id === stop ? 0 : -1}
              title={row.name}
              type="button"
            >
              <span className="book-tab-title">{row.name}</span>
              <span className="book-tab-note">
                {note}
                {when ? ` · ${when}` : ""}
              </span>
              {isWorking && (
                <span aria-label="the Host is working here" className="book-dots" role="img">
                  <i />
                  <i />
                  <i />
                </span>
              )}
            </button>
          );
        })}
      </div>
      <p aria-live="polite" className="sr-only" data-tabs-said role="status">
        {said}
      </p>
      <button
        aria-controls={BOOK_PAGE_ID}
        aria-pressed={indexOpen}
        className="book-ixtab"
        data-index-tab
        data-open={indexOpen || undefined}
        onClick={onIndex}
        title="Index: write your message, or what you remember (press /)"
        type="button"
      >
        <svg aria-hidden fill="none" height="11" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 12 12" width="11">
          <circle cx="5" cy="5" r="3.8" />
          <path d="M8 8l3 3" />
        </svg>
        <b>Index</b>
        <span>find any page, or just write</span>
      </button>
      <button
        aria-controls={BOOK_RIGHT_ID}
        aria-expanded={phone ? sheetOpen : undefined}
        className="book-sheettab"
        data-sheet-tab
        onClick={onSheet}
        title="This page: the model, the effort, the context, and the bench"
        type="button"
      >
        <svg aria-hidden fill="none" height="11" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 12 12" width="11">
          <path d="M1.5 3h9M1.5 6h9M1.5 9h6" />
        </svg>
        <b>This page</b>
        <span>model, effort, bench</span>
      </button>
    </div>
  );
}
