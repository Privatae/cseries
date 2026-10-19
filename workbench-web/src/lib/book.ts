// The book of pages, the Workbench's one layout: the rules the book's shell runs on. Pure, so
// test/book.test.ts runs it under plain `node --test`. The book itself is
// src/book/. What it reads: GET /api/projects, whose rows carry the page marks
// (server/routes/projects.py _PAGE_MARKS_COLS), and GET /api/brain/turns/active.
//
// The rules any app's pages share (the instants, the light rule, the pages the
// Host works on, what changed since the person looked, the colour reading)
// are the kit's (app-web-kit/book/rules.ts, scheme.ts), re-exported here under
// the names the Workbench has always used. Imported by a relative path: plain
// `node --test` cannot read the app's @kit alias. The tabs, the page turn and
// the phone layout are the Workbench's own and stay below.

import {
  instantMicros,
  isLater,
  landedUnseen,
  workingPages as workingOn,
} from "../../../app-web-kit/book/rules.ts";
import type { TabLight } from "../../../app-web-kit/book/rules.ts";
import type { ActiveTurn, Project } from "./types";

export {
  baselineOf,
  changedSince,
  instantMicros,
  isLater,
  landedUnseen,
  newlyLit,
  prefersReducedMotion,
  stoppedWorking,
  tabLight,
  withBaselines,
} from "../../../app-web-kit/book/rules.ts";
export type { TabLight } from "../../../app-web-kit/book/rules.ts";
export { alphaOf, isDarkSurface, luminanceOf } from "../../../app-web-kit/book/scheme.ts";

// ── Which pages, in which order ─────────────────────────────────────────────

/** How many pages show as tabs on the book's edge. The rest are reached by
 * flipping past the seventh, or through the Index. */
export const TAB_COUNT = 7;

/** A page still in the book: every project that is not archived (archived
 * pages become the Index's chapters). The list route's own test is
 * `p.status != 'archived'` (projects.py list_projects). */
export function isOpenPage(p: Pick<Project, "status">): boolean {
  return p.status !== "archived";
}

/** The book's order: its open pages, newest by last_activity_at first; a page
 * with no stamp goes last and a tie goes to the newer id. That is the list
 * route's ORDER BY (last_activity_at DESC NULLS LAST, p.id DESC), kept here
 * because the book moves a page up at once when the person writes on it. */
export function bookOrder<T extends Project>(rows: T[]): T[] {
  return rows.filter(isOpenPage).sort((a, b) => {
    const ta = instantMicros(a.last_activity_at);
    const tb = instantMicros(b.last_activity_at);
    if (ta !== tb) {
      if (ta == null) return 1;
      if (tb == null) return -1;
      return tb - ta;
    }
    return b.id - a.id;
  });
}

/** The pages that have a tab: the first TAB_COUNT of the book's order. */
export function tabPages<T extends Project>(rows: T[], count = TAB_COUNT): T[] {
  return bookOrder(rows).slice(0, count);
}

/** The rows with `id` stamped as active at `iso`: the person wrote on it, so
 * it goes to the top now; the next read of the list says where it really is. */
export function touched<T extends Project>(rows: T[], id: number, iso: string): T[] {
  return rows.map((r) => (r.id === id ? { ...r, last_activity_at: iso } : r));
}

// ── Lit tabs (the light rule: app-web-kit/book/rules.ts) ─────────────────────

// newlyLit (the tabs whose light came on since the last reading) is the
// kit's, re-exported above: the cards lift a card by the same rule.

/** The tab's second line: what waits, what landed, or where the page is. */
export function tabNote(row: Project, light: TabLight, working: boolean): string {
  if (light === "amber") {
    const asks = row.ask_cards_open ?? 0;
    const approvals = row.approvals_waiting ?? 0;
    const n = row.waiting_on_you ?? asks + approvals;
    if (asks > 0 && approvals === 0) return `${asks} ${asks === 1 ? "question" : "questions"} for you`;
    if (approvals > 0 && asks === 0) {
      return approvals === 1 ? "1 Code Approval waits" : `${approvals} Code Approvals wait`;
    }
    return `${n} waiting on you`;
  }
  if (light === "green") return "something landed";
  if (working) return "the Host is working";
  if (row.step_in_flight_title) return `building: ${row.step_in_flight_title}`;
  return row.group && row.group !== "archived" ? String(row.group) : (row.status ?? "");
}

// ── The Host at work ────────────────────────────────────────────────────────

/** The page a turn is on, from its thread key: a project's thread is
 * `project|<entity>|<id>` (server/turn_registry.py compose_thread_key). */
export function projectOfTurnKey(threadKey: string): number | null {
  const m = /^project\|[^|]*\|(\d+)$/.exec(threadKey);
  return m ? Number(m[1]) : null;
}

/** The pages the Host is working on now: the RUNNING turns among GET
 * /api/brain/turns/active's `turns` (every turn of the Host, running or
 * finished in the last ~15 minutes), by their thread key
 * (projectOfTurnKey), on the kit's rule. */
export function workingPages(turns: ReadonlyArray<Pick<ActiveTurn, "status" | "thread_key">> | null | undefined): Set<number> {
  return workingOn(turns, projectOfTurnKey);
}

// stoppedWorking (the pages whose turn stopped since the last read) is the
// kit's, re-exported above.

/** How often the book reads which turns are running. GET
 * /api/brain/turns/active reads the turn registry in memory and touches no
 * database, so a short cadence costs the Host nothing; it moves the working
 * dots and, when a turn stops, sends the book to the list at once. */
export const TURNS_POLL_MS = 4_000;

/** How often the book reads GET /api/projects on its own. It is the only way
 * the book learns of what no turn ending announces, and that is mostly what
 * lights amber: an ask card filed by a turn that holds on another page (its
 * turn is still running), a Code Approval a build opened, an approval counted
 * in another window; and a report delivered while the socket was down. The
 * list reads each project through an index: 13.7 ms at 300 projects and 100k
 * turn rows (measured), so a read every 10 s costs the
 * Host's database about a seven-hundredth of one core, and an amber light is
 * at most 10 s late. Host replies come sooner, when their turn stops
 * (TURNS_POLL_MS), and agents' and builds' reports on the /ws push. Paused
 * while the window is hidden; read again when it shows. */
export const LIST_POLL_MS = 10_000;

/** The waits before the book reads the project list again when its first
 * read failed (a refused connection, a 502 while the kernel reloads). The
 * book picks the page it opens from that read, so without another one it
 * stays on "Opening the book…" with no page open, even after the list poll
 * reads the rows. Doubling from a quarter second, then every 4 s until a
 * read succeeds; well inside LIST_POLL_MS at first. */
export const BOOT_RETRY_MS = [250, 500, 1_000, 2_000, 4_000];

/** The wait before boot read number `attempt` + 1 (0 after the first). */
export function bootRetryMs(attempt: number): number {
  return BOOT_RETRY_MS[Math.min(Math.max(0, attempt), BOOT_RETRY_MS.length - 1)];
}

// ── The seen mark ───────────────────────────────────────────────────────────
// The book marks a page seen (PUT /api/projects/{id}/seen) when
//   - it comes to the front (seenOnOpen);
//   - a turn on it ends while it is in front (seenOnSettle): the person
//     watched the reply arrive, and the mark names that turn (its token, off
//     the stream's 2:[{type:"session"}] frame). The reply's row is written
//     after the stream ends, however late (a failing embedder holds it ~92 s,
//     PII mode longer), and the server never lets that row light the tab
//     (projects.seen_turns, v217; unseen_landed_at leaves it out);
//   - its own row says something it did not watch landed after its mark while
//     it is in front (seenOnList): a reply sent from another window or a
//     connector, an agent's report, a build that finished.
// Each returns the page to mark, or null.

export function seenOnOpen(prev: number | null, next: number | null): number | null {
  return next != null && next !== prev ? next : null;
}

export function seenOnSettle(open: number | null, settled: number | null): number | null {
  return open != null && settled === open ? open : null;
}

export function seenOnList(open: number | null, rows: Project[]): number | null {
  if (open == null) return null;
  const row = rows.find((r) => r.id === open);
  return row && row.seen_at != null && isLater(landedUnseen(row), row.seen_at) ? open : null;
}

/** The rows with `id`'s mark moved to `seenAt`, the server's answer, and never
 * back: the route keeps the later of the two (projects.py
 * mark_project_seen_service, GREATEST). */
export function withSeen<T extends Project>(rows: T[], id: number, seenAt: string): T[] {
  return rows.map((r) =>
    r.id === id && isLater(seenAt, r.seen_at) ? { ...r, seen_at: seenAt } : r,
  );
}

/** When, after a page's own turn ended in front of the person, the book reads
 * the list again to catch its reply's row landing (the row is written after
 * the stream; the running count and "Where this stands" read it). Usually it
 * is there within a second or two; the list's own 10 s read covers a slower
 * write. Nothing here decides a light: the mark named the turn. */
export const SETTLE_RECHECK_MS: readonly number[] = [1_200, 3_500, 8_000];

/** Whether the page in front should read its page data again: something
 * landed on it (its row's last_landed_at moved), the watched reply included,
 * which a light leaves out but the count and "Where this stands" do not. */
export function landedMoved(prev: string | null | undefined, row: Project | null | undefined): boolean {
  if (!row || prev === undefined) return false;
  return instantMicros(row.last_landed_at ?? null) !== instantMicros(prev ?? null);
}

/** A fresh read of the list, adopted. A read that left before a mark was
 * stored carries the older mark; the server never moves a mark back, so the
 * later of the two is the true one and is kept. */
export function adoptList<T extends Project>(current: T[], incoming: T[]): T[] {
  const seen = new Map(current.map((r) => [r.id, r.seen_at ?? null]));
  return incoming.map((r) => {
    const mine = seen.get(r.id);
    return mine && isLater(mine, r.seen_at) ? { ...r, seen_at: mine } : r;
  });
}

/** Whether ← or → turns the page. Not with a modifier held, not while a
 * dialog is up, and not inside a text field: the arrows belong to the text
 * there. The page's own composer is the exception while it is empty, because
 * the chat focuses it whenever a page opens, and an empty box has nothing for
 * the arrows to move through. */
export function arrowsTurnPages(at: {
  modifier: boolean;
  dialogOpen: boolean;
  inTextField: boolean;
  composerEmpty: boolean;
}): boolean {
  if (at.modifier || at.dialogOpen) return false;
  return !at.inTextField || at.composerEmpty;
}

// ── The phone layout ────────────────────────────────────────────────────────

/** Narrower than this, the book is the phone layout: a full-width page, the
 * tabs a strip along the bottom with the Index tab in it, and the right side
 * a sheet over the page ("This page", in the strip). The Workbench's desktop
 * window is never narrower than 1024 px, so this is a phone's or a small
 * tablet's screen. Kept equal to book.css's @media rule. */
export const PHONE_MAX_PX = 860;
export const PHONE_QUERY = `(max-width: ${PHONE_MAX_PX}px)`;

/** Whether the window is the phone layout's width. False where the browser
 * cannot say. */
export function isPhoneWidth(
  win?: { matchMedia?: (query: string) => { matches: boolean } } | null,
): boolean {
  try {
    return !!win?.matchMedia?.(PHONE_QUERY).matches;
  } catch {
    return false;
  }
}

// ── Turning the page ────────────────────────────────────────────────────────

export interface FlipMotion {
  kind: "turn" | "fade";
  ms: number;
}

/** The mockup's page turn, about the page's left edge. */
export const TURN_MS = 720;
/** The crossfade that replaces it for prefers-reduced-motion. */
export const FADE_MS = 180;

export function flipMotion(reducedMotion: boolean): FlipMotion {
  return reducedMotion ? { kind: "fade", ms: FADE_MS } : { kind: "turn", ms: TURN_MS };
}

/** ← and → in the book's order (ids, newest first): → turns to the next
 * older page, ← to the next newer one. Null at either end, for any other key,
 * or when the page in front is not in the order. */
export function flipNeighbour(order: number[], open: number | null, key: string): number | null {
  if (open == null) return null;
  const i = order.indexOf(open);
  if (i < 0) return null;
  if (key === "ArrowRight") return i + 1 < order.length ? order[i + 1] : null;
  if (key === "ArrowLeft") return i > 0 ? order[i - 1] : null;
  return null;
}

/** A key pressed on a tab (`from`, by page id) in the book's vertical
 * tablist (WAI-ARIA tabs pattern): ↓ the next tab, ↑ the one before, Home the
 * first, End the last. Null past either end (the book's arrows do not wrap
 * either), for any other key, or when `from` is not a tab. ← and → are the
 * book's own page turns (flipNeighbour), from a tab as from anywhere. */
export function tabKeyTarget(tabs: number[], from: number | null, key: string): number | null {
  const i = from == null ? -1 : tabs.indexOf(from);
  if (i < 0) return null;
  if (key === "ArrowDown") return i + 1 < tabs.length ? tabs[i + 1] : null;
  if (key === "ArrowUp") return i > 0 ? tabs[i - 1] : null;
  if (key === "Home") return i > 0 ? tabs[0] : null;
  if (key === "End") return i < tabs.length - 1 ? tabs[tabs.length - 1] : null;
  return null;
}

// ── Scrolling to something on the page ─────────────────────────────────────

/** Bring `el` into view by scrolling its nearest scrolling ancestor only, to
 * its middle ("center") or its top ("start"). Element.scrollIntoView also
 * scrolls every clipped ancestor that can be scrolled from code — the book's
 * sheet and desk, which never scroll themselves — and slid the page's head
 * out of place when a card near the end of the conversation was asked for. */
export function scrollWithin(el: HTMLElement, block: "center" | "start", smooth: boolean): void {
  let box = el.parentElement;
  while (box && !(box.scrollHeight > box.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) {
    box = box.parentElement;
  }
  if (!box) return;
  const r = el.getBoundingClientRect();
  const b = box.getBoundingClientRect();
  const at = r.top - b.top + box.scrollTop;
  const top = block === "center" ? at - (box.clientHeight - r.height) / 2 : at;
  box.scrollTo({ top: Math.max(0, top), behavior: smooth ? "smooth" : "auto" });
}
