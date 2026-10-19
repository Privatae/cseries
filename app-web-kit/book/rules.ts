// The book of pages' rules that belong to no one app: the instants, when a
// page's tab lights, which pages the Host is working on, and what changed
// since the person last looked. The Workbench's book (workbench-web/src/book/)
// runs on them, and Circles' cards will. Pure, with relative imports only, so
// `node --test` runs it (app-web-kit/test/book-rules.test.ts, and
// workbench-web/test/book.test.ts through lib/book.ts).
//
// A page here names no project. The kit's page type carries a string id: a
// Circles thread's uuid, or "original" for its first conversation. Every rule
// only compares ids, never reads them as numbers, so an app that keeps numeric
// ids (the Workbench's project ids) passes them as they are. The tabs, the
// page turn and the phone layout are each app's own (the Workbench's are in
// workbench-web/src/book/ and src/lib/book.ts).

// ── Instants ────────────────────────────────────────────────────────────────

/** An ISO instant in microseconds since the epoch; null when absent or
 * unreadable. The server writes datetimes with six fraction digits
 * (datetime.isoformat) and Date.parse keeps three, so without the rest a reply
 * written 0.4 ms after the seen mark would read as landing at the same moment
 * and its tab would stay dark. */
export function instantMicros(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  const frac = /:\d{2}\.(\d+)/.exec(iso)?.[1] ?? "";
  return ms * 1000 + (frac.length > 3 ? Number(frac.slice(3, 6).padEnd(3, "0")) : 0);
}

/** `a` is strictly later than `b`. A missing `a` never is; a missing `b`
 * (nothing to be later than) is passed by any `a`. */
export function isLater(a: string | null | undefined, b: string | null | undefined): boolean {
  const x = instantMicros(a);
  if (x == null) return false;
  const y = instantMicros(b);
  return y == null || x > y;
}

// ── A page ──────────────────────────────────────────────────────────────────

/** A page's id: a string on the kit's own page type; any id compared by
 * identity works with the rules. */
export type PageId = string | number;

/** The marks that light a page's tab, as the server's list rows carry them
 * (the Workbench's GET /api/projects, server/routes/projects.py
 * _PAGE_MARKS_COLS). */
export interface PageMarks {
  /** When the person last had the page open. Null: never, since the mark
   * has existed. */
  seen_at?: string | null;
  /** The newest moment the Host produced something on the page: a reply, a
   * report delivered into it, a task that finished. The person's own words
   * never count. */
  last_landed_at?: string | null;
  /** The newest of what landed after seen_at that the person did not watch
   * arrive. Absent from a server that does not send it. */
  unseen_landed_at?: string | null;
  /** How many things wait on the person on this page. Each app says what it
   * counts. */
  waiting_on_you?: number | null;
}

/** A page of a book: its id, its title, when it was last written on, and the
 * marks that light its tab. */
export interface BookPage<Id extends PageId = string> extends PageMarks {
  id: Id;
  name: string;
  last_activity_at?: string | null;
}

// ── Lit tabs ────────────────────────────────────────────────────────────────

export type TabLight = "amber" | "green" | null;

/** What landed on a page that can light it: the server's unseen_landed_at,
 * the newest landing after the seen mark that the person did not watch
 * arrive. A reply whose turn ended in front of the person is marked as
 * watched by its turn's token and never counts, however late its row is
 * written; anything else that lands after the mark always does (the
 * Workbench's projects._PAGE_MARKS_JOINS; scripts/test_workbench_seen_mark_pg
 * .py). A row from a server without the field reads last_landed_at, the
 * earlier rule. */
export function landedUnseen(row: PageMarks): string | null | undefined {
  return row.unseen_landed_at !== undefined ? row.unseen_landed_at : row.last_landed_at;
}

/** A tab's light.
 *  - amber while something waits on the person (waiting_on_you), open page
 *    or not;
 *  - green when something landed after the person last had the page open,
 *    other than a reply they watched arrive (landedUnseen later than seen_at;
 *    the mark may be one the book stored after the list was read), and the
 *    page is not open now.
 * Amber wins: it asks for the person, green only tells them.
 *
 * A page nobody has opened since the seen mark existed has no seen_at.
 * Lighting it for everything that ever landed there would light every tab the
 * first time the book opens, so such a page is held against `baseline`: its
 * last_landed_at when this book first read it (withBaselines). Only what lands
 * after that lights it; with no baseline at all it stays dark. Such a page has
 * no watched reply: the page is marked on opening, before any turn on it. */
export function tabLight(row: PageMarks, open: boolean, baseline?: string | null): TabLight {
  if ((row.waiting_on_you ?? 0) > 0) return "amber";
  if (open) return null;
  if (row.seen_at != null) return isLater(landedUnseen(row), row.seen_at) ? "green" : null;
  if (baseline === undefined) return null;
  return isLater(row.last_landed_at, baseline) ? "green" : null;
}

/** The pages whose light came on, or changed colour, since the last reading:
 * what a book says politely to a screen reader, and what the cards lift. Nothing
 * on the first reading (`prev` null), when each page's own name already
 * carries its state, and nothing for a page going dark. A lit page that has
 * just come in counts as lit. (Lifted from workbench-web/src/lib/book.ts,
 * which re-exports it under the same name.) */
export function newlyLit<Id extends PageId>(
  prev: ReadonlyMap<Id, TabLight> | null,
  next: ReadonlyMap<Id, TabLight>,
): Id[] {
  if (prev == null) return [];
  const out: Id[] = [];
  for (const [id, light] of next) {
    if (light != null && prev.get(id) !== light) out.push(id);
  }
  return out;
}

/** The baselines for pages with no seen mark: each one's last_landed_at as
 * the book first read it. A page already held keeps its first one, so what
 * lands later is measured against what was there when the book opened. */
export function withBaselines<Id extends PageId>(
  prev: ReadonlyMap<Id, string | null>,
  rows: ReadonlyArray<PageMarks & { id: Id }>,
): Map<Id, string | null> {
  const next = new Map(prev);
  for (const r of rows) {
    if (r.seen_at == null && !next.has(r.id)) next.set(r.id, r.last_landed_at ?? null);
  }
  return next;
}

/** The baseline to hand tabLight: undefined when none was taken. */
export function baselineOf<Id extends PageId>(
  map: ReadonlyMap<Id, string | null>,
  id: Id,
): string | null | undefined {
  return map.has(id) ? (map.get(id) ?? null) : undefined;
}

// ── The Host at work ────────────────────────────────────────────────────────

/** Which page a turn is on, read from its thread key (server/turn_registry.py
 * compose_thread_key), or null for a turn on none of this book's pages. Each
 * app supplies its own: a Workbench project is `project|<entity>|<id>`, a
 * Circles thread `thread|<entity>|<id>`, and Circles' original conversation
 * `circles|<entity>|none`. */
export type PageOfTurnKey<Id extends PageId = string> = (threadKey: string) => Id | null;

/** The pages the Host is working on now: the RUNNING turns among the turn
 * registry's list (every turn of the Host, running or finished in the last
 * ~15 minutes), each read to its page by `pageOf`. */
export function workingPages<Id extends PageId>(
  turns: ReadonlyArray<{ status: string; thread_key?: string | null }> | null | undefined,
  pageOf: PageOfTurnKey<Id>,
): Set<Id> {
  const out = new Set<Id>();
  for (const t of turns ?? []) {
    if (t.status !== "running" || !t.thread_key) continue;
    const id = pageOf(t.thread_key);
    if (id != null) out.add(id);
  }
  return out;
}

/** The pages whose turn stopped running since the last read. Something just
 * landed on each, so a book reads its list now rather than at its next poll.
 * (Lifted from workbench-web/src/lib/book.ts, which re-exports it.) */
export function stoppedWorking<Id extends PageId>(prev: ReadonlySet<Id>, next: ReadonlySet<Id>): Id[] {
  return [...prev].filter((id) => !next.has(id));
}

// ── Where this stands ───────────────────────────────────────────────────────

/** A line of "Where this stands" written after the person last had the page
 * open, which the page tints. `seenBefore` is the mark as it stood when this
 * opening began; with none there is nothing to compare, and nothing is
 * tinted. */
export function changedSince(at: string | null | undefined, seenBefore: string | null | undefined): boolean {
  return seenBefore != null && isLater(at, seenBefore);
}

// ── Less motion ─────────────────────────────────────────────────────────────

/** Whether the person asked for less motion. False where the browser cannot
 * say. */
export function prefersReducedMotion(
  win?: { matchMedia?: (query: string) => { matches: boolean } } | null,
): boolean {
  try {
    return !!win?.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}
