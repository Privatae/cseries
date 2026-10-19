// The book's Index and its chapters: the rules the Index runs on. Pure, with type-only imports, so
// test/book-index.test.ts runs it under plain `node --test`. The Index itself
// is src/book/index-page.tsx, the riffle src/book/riffle.ts. What it reads:
// POST /api/projects/index while the person types (the words in its body)
// and POST /api/workbench/index/send on Enter in "send" mode
// (server/routes/workbench_index.py), POST /api/projects on Enter in "new"
// mode (the Start box's own route), and GET /api/projects/buckets for the
// chapters.

import type { Bucket, Project } from "./types.ts";
import { instantMicros } from "./book.ts";
import { suggestTitle } from "./project-groups.ts";
import { redactSecrets, scanSecrets } from "./secret-shapes.ts";

// ── The modes of Enter ──────────────────────────────────────────────────────

/** Enter in the Index: "send" gives the message to the page the Host picks
 * (the default); "open" only opens the top page, with no model call; "new"
 * starts a new page with the words as its first message, whatever the Host
 * would have picked. */
export type IndexMode = "send" | "open" | "new";

/** Where the book keeps the person's choice of mode, in this browser. Only
 * "send" and "open" are kept: "new" makes a page and a Code Repository on
 * every Enter, so it is chosen each time and never comes back on its own. */
export const INDEX_MODE_KEY = "workbench.book.index.mode";

export function readIndexMode(stored: string | null | undefined): IndexMode {
  return stored === "open" ? "open" : "send";
}

/** The mode to store for the person's choice, or null when it is not kept. */
export function modeToKeep(mode: IndexMode): IndexMode | null {
  return mode === "new" ? null : mode;
}

/** How long the Index waits after a keystroke before it asks again. The
 * search reads no model, and answers in tens of milliseconds at 300 pages
 * (scripts/test_workbench_index_pg.py T), so the pages re-sort as the person
 * types. */
export const INDEX_DEBOUNCE_MS = 120;

/** How many pages the Index lists (the route's default). */
export const INDEX_LIMIT = 20;

// ── What the search answers ─────────────────────────────────────────────────

export interface IndexPart {
  text: string;
  mark: boolean;
}

/** The words a page matched, with the matched ones marked
 * (workbench_index._snippets). */
export interface IndexSnippet {
  from: "conversation" | "name" | "description" | "phases" | string;
  text: string;
  parts: IndexPart[];
  role?: string | null;
  at?: string | null;
}

/** One page as POST /api/projects/index lists it. In order only: there is no
 * score (weight decides the order and is never shown). */
export interface IndexPage {
  id: number;
  name: string;
  status: string;
  group?: string;
  archived: boolean;
  bucket_id: number | null;
  last_activity_at: string | null;
  snippet: IndexSnippet | null;
}

export interface IndexAnswer {
  q: string;
  pages: IndexPage[];
  semantic: boolean;
  conversation: { used: string[]; too_common: string[] };
}

/** The excerpt a result shows: the matched words when there are any, else
 * nothing (the title says the rest). Long excerpts are cut, never mid-mark. */
export function excerptParts(snippet: IndexSnippet | null | undefined, max = 180): IndexPart[] {
  if (!snippet || snippet.from === "name") return [];
  const out: IndexPart[] = [];
  let used = 0;
  for (const p of snippet.parts ?? []) {
    if (used >= max) break;
    const room = max - used;
    if (p.text.length <= room || p.mark) {
      out.push(p);
      used += p.text.length;
    } else {
      // Cut on a word: a word the limit falls inside is left out whole.
      let cut = p.text.slice(0, room);
      if (/\S/.test(p.text[room] ?? "")) cut = cut.replace(/\s*\S*$/, "");
      out.push({ text: cut.trimEnd() + "…", mark: false });
      used = max;
    }
  }
  return out;
}

// ── Chapters ────────────────────────────────────────────────────────────────

/** "Loose pages": the archived pages filed in no chapter. */
export const LOOSE_PAGES = "Loose pages";

export interface Chapter {
  /** The bucket (project_folders row); null is Loose pages. */
  id: number | null;
  name: string;
  /** The bucket's colour when it is a hex the book can trust, else null. */
  color: string | null;
  /** Its archived pages, most recently active first: the chapter's own. */
  archived: Project[];
  /** Its pages still open in the book (they have tabs too), most recently
   * active first. Loose pages has none: an open page filed nowhere is just
   * a page of the book. */
  open: Project[];
}

/** Most recently active first; a page with no stamp last, a tie to the newer
 * id (the list route's own order). */
export function byActivity<T extends Project>(rows: T[]): T[] {
  return [...rows].sort((a, b) => {
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

function hexColor(c: string | null | undefined): string | null {
  return c && /^#[0-9a-fA-F]{6}$/.test(c) ? c : null;
}

/** The chapters the Index shows: every bucket, in the buckets' own order,
 * each with its archived pages and its open ones; then Loose pages, the
 * archived pages in no bucket (or in one that is gone). */
export function chaptersOf(rows: Project[], buckets: Bucket[]): Chapter[] {
  const known = new Map<number, Chapter>();
  const out: Chapter[] = buckets.map((b) => {
    const c: Chapter = { id: b.id, name: b.name, color: hexColor(b.color), archived: [], open: [] };
    known.set(b.id, c);
    return c;
  });
  const loose: Chapter = { id: null, name: LOOSE_PAGES, color: null, archived: [], open: [] };
  for (const p of rows) {
    const archived = p.status === "archived";
    const c = p.bucket_id != null ? known.get(p.bucket_id) : undefined;
    if (c) (archived ? c.archived : c.open).push(p);
    else if (archived) loose.archived.push(p);
  }
  for (const c of [...out, loose]) {
    c.archived = byActivity(c.archived);
    c.open = byActivity(c.open);
  }
  return [...out, loose];
}

/** The chapter a page is filed in: its bucket's, else Loose pages. */
export function chapterOf(chapters: Chapter[], page: Pick<Project, "id" | "bucket_id">): Chapter | null {
  const own = page.bucket_id != null ? chapters.find((c) => c.id === page.bucket_id) : undefined;
  return own ?? chapters.find((c) => c.id === null) ?? null;
}

/** "6 pages" / "1 page", and the open ones when there are any. */
export function chapterCount(c: Chapter): string {
  const n = c.archived.length;
  const pages = `${n} ${n === 1 ? "page" : "pages"}`;
  return c.open.length ? `${pages} · ${c.open.length} open` : pages;
}

/** The line an archived page shows under its title. */
export function archivedNote(chapter: Chapter | null): string {
  const where = !chapter || chapter.id === null ? "with the loose pages" : `in the ${chapter.name} chapter`;
  return `This page is archived, ${where}. Write on it and it comes back to the top of the book.`;
}

// ── The riffle ──────────────────────────────────────────────────────────────

/** At most this many pages go past in a riffle, besides the page in front. */
export const RIFFLE_MAX = 7;
/** One page's turn in a riffle (the mockup's riff keyframes). */
export const RIFFLE_TURN_MS = 460;

/** The pages a riffle shows going past before it lands on `targetId`, in the
 * order they turn: the chapter's pages that come before the target (newest
 * first), the nearest last, at most `max`. When fewer than three come before
 * it, the pages after it make up the run, so a riffle always reads as one.
 * With no target (opening the chapter itself), its first `max` pages. */
export function riffleRun<T extends { id: number }>(pages: T[], targetId: number | null, max = RIFFLE_MAX): T[] {
  if (targetId == null) return pages.slice(0, max);
  const i = pages.findIndex((p) => p.id === targetId);
  if (i < 0) return pages.slice(0, max);
  const before = pages.slice(Math.max(0, i - max), i);
  if (before.length >= 3) return before;
  const after = pages.slice(i + 1, i + 1 + (max - before.length));
  return [...before, ...after.reverse()];
}

/** When each page of a riffle starts to turn, and when the riffle is over
 * (the mockup's riffle: one every 120 ms, the last two slowing down). `n`
 * counts every page that turns, the one in front included. */
export function riffleSchedule(n: number): { delays: number[]; total: number } {
  const delays: number[] = [];
  for (let i = 0; i < n; i++) {
    delays.push(60 + i * 120 + (i > n - 3 ? (i - (n - 3)) * 110 : 0));
  }
  const last = delays.length ? delays[delays.length - 1] : 0;
  return { delays, total: last + RIFFLE_TURN_MS + 60 };
}

// ── Sending from the Index ──────────────────────────────────────────────────

/** A fresh id for one message sent from the Index: the server runs a send
 * once per id (workbench_index.index_send_once), so a retry of the same
 * message reuses it and a double Enter can never start two turns. */
export function newRequestId(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** A message sent from the Index, from Enter until what it came to has been
 * read: its words, its request id, and its outcome (null while the Host picks
 * the page, or while the page they start is made). The book holds it
 * (book-app.tsx), so closing the Index and opening it again mid-pick neither
 * loses it nor lets the words go twice. */
export interface IndexPick {
  text: string;
  requestId: string;
  outcome: SendOutcome | null;
  /** The words start a new page ("new" mode): no Host picks, a page is made.
   * Absent for a send. */
  startsPage?: boolean;
}

/** The request id to send `text` from the Index with, after `prev`: null
 * while `prev` is still out (one send at a time); `prev`'s own id when it
 * was these words and no answer came back (the server may have run it: with
 * the same id a retry gets that answer, never a second turn); else a fresh
 * one. */
export function pickRequestId(
  prev: IndexPick | null,
  text: string,
  fresh: () => string = newRequestId,
): string | null {
  if (prev && prev.outcome == null) return null;
  if (prev && prev.text === text && prev.outcome?.kind === "error") return prev.requestId;
  return fresh();
}

export interface AskCandidate {
  project_id: number;
  name: string;
  archived: boolean;
}

/** What a send from the Index came to, for the book to act on. */
export type SendOutcome =
  /** The Host picked a page and the turn started there (or went into the turn
   * already running there): the book turns to it. */
  | {
      kind: "landed";
      projectId: number;
      name: string;
      restored: boolean;
      created: boolean;
      reason: string | null;
    }
  /** The page was picked and written on, but the turn did not start. */
  | { kind: "turn_refused"; projectId: number; name: string; restored: boolean; created: boolean; error: string }
  /** The Host could not pick: nothing ran, and the person says which page. */
  | { kind: "ask"; failure: string; candidates: AskCandidate[] }
  /** The Host picked an archived page that cannot come back: nothing ran. */
  | { kind: "restore_refused"; projectId: number | null; name: string; reason: string }
  | { kind: "error"; message: string };

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** The route's answer (status and body; a 409 is FastAPI's {detail: {...}})
 * as a SendOutcome. */
export function sendOutcome(status: number, body: unknown): SendOutcome {
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  if (status === 200) {
    const state = str(b.state);
    const projectId = Number(b.project_id);
    const name = str(b.project_name);
    if (state === "ask") {
      const raw = Array.isArray(b.candidates) ? b.candidates : [];
      return {
        kind: "ask",
        failure: str(b.failure),
        candidates: raw
          .map((c) => c as Record<string, unknown>)
          .filter((c) => Number.isFinite(Number(c.project_id)))
          .map((c) => ({ project_id: Number(c.project_id), name: str(c.name), archived: c.archived === true })),
      };
    }
    if (!Number.isFinite(projectId)) return { kind: "error", message: "The answer named no page." };
    if (state === "refused") {
      return {
        kind: "turn_refused",
        projectId,
        name,
        restored: b.restored === true,
        created: b.created === true,
        error: str(b.error) || "the turn could not start",
      };
    }
    return {
      kind: "landed",
      projectId,
      name,
      restored: b.restored === true,
      created: b.created === true,
      reason: str(b.reason) || null,
    };
  }
  const detail = b.detail;
  if (status === 409 && detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if (d.state === "restore_refused") {
      const id = Number(d.project_id);
      return {
        kind: "restore_refused",
        projectId: Number.isFinite(id) ? id : null,
        name: str(d.project_name),
        reason: str(d.reason) || "it could not come back",
      };
    }
    return { kind: "error", message: str(d.reason) || "The message was not sent." };
  }
  if (typeof detail === "string" && detail) return { kind: "error", message: detail };
  return { kind: "error", message: status ? `The message was not sent (HTTP ${status}).` : "The message was not sent." };
}

/** What the Index says while a send is out, and after one that did not
 * land. */
export function outcomeLine(o: SendOutcome): string {
  switch (o.kind) {
    case "landed":
      return o.created ? `Started a new page: ${o.name}` : `Sent to ${o.name}`;
    case "turn_refused":
      return `Written on ${o.name}, but the Host did not start: ${o.error}`;
    case "ask":
      return "The Host could not pick a page. Which page is this for?";
    case "restore_refused":
      return `${o.name || "That page"} could not come back from the archive: ${o.reason}`;
    case "error":
      return o.message;
  }
}

/** Picking the page takes one model call; the Index says so while it runs. */
export const PICKING_LINE = "Picking the page…";

/** Starting a page makes a project and its Code Repository; the Index says so
 * while that runs. */
export const STARTING_LINE = "Starting the page…";

/** What the Index's status line says about the send in hand: PICKING_LINE
 * while the Host picks (STARTING_LINE while a new page is made); what an
 * answer that landed nowhere came to; else nothing (a landing shows itself:
 * the book turns to the page). */
export function pickLine(pick: IndexPick | null): string {
  if (!pick) return "";
  if (pick.outcome == null) return pick.startsPage ? STARTING_LINE : PICKING_LINE;
  if (pick.outcome.kind === "landed" || pick.outcome.kind === "turn_refused") return "";
  return outcomeLine(pick.outcome);
}

// ── Starting a page from the Index ──────────────────────────────────────────

/** The title of a page started from words that suggest none. */
export const NEW_PAGE_TITLE = "New page";

/** The title a new page gets from the words that start it: the Start box's
 * own suggestion (lib/project-groups.ts suggestTitle). */
export function newPageTitle(words: string): string {
  return suggestTitle(words) || NEW_PAGE_TITLE;
}

/** The title and description a page started from the Index is made with
 * (POST /api/projects): the words with every secret shape cut, hard and soft,
 * as the composer's "Send redacted" cuts them. The page is made before the
 * person has seen the composer's strip, so nothing of a secret goes with it;
 * the words themselves go on to the page's box, where the strip decides
 * (ChatPane firstMessage). The title is suggested from the words with the
 * secrets left out altogether (a cut marker's colon would end it early). */
export function startWords(words: string): { title: string; description: string } {
  const { text: description, findings } = redactSecrets(words, ["hard", "soft"]);
  let bare = "";
  let at = 0;
  for (const f of findings) {
    bare += `${words.slice(at, f.start)} `;
    at = f.end;
  }
  bare += words.slice(at);
  return { title: newPageTitle(bare), description };
}

/** The text the Index's search sends (the body of POST /api/projects/index):
 * the words with every secret shape, hard and soft, taken out. The search
 * runs as the person types and reaches the server's embedding call, and the
 * server cuts hard shapes only, so a password typed into the box would
 * otherwise leave the browser as it is. A space stands in for each cut, so
 * the words around it still search. A shape is found once it is whole: a
 * secret typed a character at a time is sent as the prefix the rules do not
 * yet recognise, in a body and never a URL. */
export function indexQueryText(q: string): string {
  const findings = scanSecrets(q);
  if (!findings.length) return q;
  let out = "";
  let at = 0;
  for (const f of findings) {
    if (f.start > at) out += q.slice(at, f.start);
    out += " ";
    at = Math.max(at, f.end);
  }
  out += q.slice(at);
  return out.replace(/\s+/g, " ").trim();
}

/** What making the page came to (POST /api/projects), as the outcome a send
 * comes to: a page made has landed (its first message goes through the page
 * itself, ChatPane); one that was not says why. */
export function startOutcome(
  r: { ok: true; data: { id: number; name?: string } } | { ok: false; message: string },
  title: string,
): SendOutcome {
  if (!r.ok) return { kind: "error", message: `The page could not be started: ${r.message}` };
  const projectId = Number(r.data.id);
  if (!Number.isFinite(projectId)) return { kind: "error", message: "The answer named no page." };
  return { kind: "landed", projectId, name: r.data.name || title, restored: false, created: true, reason: null };
}

// ── Filing a page in a chapter ──────────────────────────────────────────────

/** What a tab carries when it is dragged onto a chapter (src/book/tabs.tsx
 * starts the drag, src/book/index-page.tsx takes the drop). */
export const BOOK_DRAG_TYPE = "application/x-workbench-page";
