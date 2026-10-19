import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FADE_MS,
  LIST_POLL_MS,
  PHONE_MAX_PX,
  PHONE_QUERY,
  SETTLE_RECHECK_MS,
  TAB_COUNT,
  TURNS_POLL_MS,
  TURN_MS,
  adoptList,
  alphaOf,
  arrowsTurnPages,
  baselineOf,
  bookOrder,
  changedSince,
  flipMotion,
  flipNeighbour,
  instantMicros,
  isDarkSurface,
  isLater,
  isPhoneWidth,
  landedMoved,
  landedUnseen,
  luminanceOf,
  newlyLit,
  prefersReducedMotion,
  seenOnList,
  seenOnOpen,
  seenOnSettle,
  stoppedWorking,
  tabKeyTarget,
  tabLight,
  tabNote,
  tabPages,
  touched,
  withBaselines,
  withSeen,
  workingPages,
} from "../src/lib/book.ts";
import type { Project } from "../src/lib/types.ts";

// The book of pages: which pages are tabs and in what order, when a tab lights, when a page is
// marked seen, and how a page turns. The book (src/book/) renders these.

const row = (id: number, extra: Partial<Project> = {}): Project => ({
  id,
  name: `Project ${id}`,
  status: "active",
  group: "active",
  last_activity_at: null,
  seen_at: null,
  last_landed_at: null,
  waiting_on_you: 0,
  ask_cards_open: 0,
  approvals_waiting: 0,
  ...extra,
});

// ── instants: the server's six fraction digits ──────────────────────────────

test("instants keep the microseconds Date.parse drops", () => {
  const a = "2026-10-08T06:41:00.123400+00:00";
  const b = "2026-10-08T06:41:00.123900+00:00";
  assert.equal(Date.parse(a), Date.parse(b)); // what the browser alone sees
  assert.ok((instantMicros(b) as number) > (instantMicros(a) as number));
  assert.equal(isLater(b, a), true);
  assert.equal(isLater(a, b), false);
  assert.equal(isLater(a, a), false);
  assert.equal(instantMicros("2026-10-08T06:41:00+00:00"), Date.parse("2026-10-08T06:41:00Z") * 1000);
  assert.equal(instantMicros("2026-10-08T06:41:00.5Z"), Date.parse("2026-10-08T06:41:00.5Z") * 1000);
  assert.equal(instantMicros(null), null);
  assert.equal(instantMicros("not a time"), null);
  // Nothing is later than a missing instant's absence: a missing `a` never is,
  // and any `a` is later than a missing `b`.
  assert.equal(isLater(null, a), false);
  assert.equal(isLater(a, null), true);
});

// ── tab order ───────────────────────────────────────────────────────────────

test("tabs are the open pages, newest by last activity first, seven of them", () => {
  const rows: Project[] = [];
  for (let i = 1; i <= 10; i++) {
    rows.push(row(i, { last_activity_at: `2026-10-08T0${i % 10}:00:00+00:00` }));
  }
  rows.push(row(50, { status: "archived", group: "archived", last_activity_at: "2026-10-08T23:00:00+00:00" }));
  const tabs = tabPages(rows);
  assert.equal(TAB_COUNT, 7);
  assert.equal(tabs.length, 7);
  // 9 is 09:00, the newest; 10 is 00:00, the oldest; the archived page (the
  // newest of all) is not in the book.
  assert.deepEqual(tabs.map((r) => r.id), [9, 8, 7, 6, 5, 4, 3]);
  assert.deepEqual(bookOrder(rows).map((r) => r.id), [9, 8, 7, 6, 5, 4, 3, 2, 1, 10]);
  assert.equal(bookOrder(rows).some((r) => r.status === "archived"), false);
});

test("the order is the list route's: no stamp goes last, a tie goes to the newer id", () => {
  const rows = [
    row(1, { last_activity_at: "2026-10-08T10:00:00+00:00" }),
    row(2, { last_activity_at: null }),
    row(3, { last_activity_at: "2026-10-08T10:00:00+00:00" }),
    row(4, { last_activity_at: "2026-10-08T10:00:00.000001+00:00" }),
    row(5, { last_activity_at: null }),
  ];
  assert.deepEqual(bookOrder(rows).map((r) => r.id), [4, 3, 1, 5, 2]);
  // pending and completed pages are still open pages
  const mixed = [row(1, { status: "planning", group: "pending" }), row(2, { status: "complete", group: "completed" })];
  assert.deepEqual(bookOrder(mixed).map((r) => r.id), [2, 1]);
});

test("writing on an older page brings it to the top at once", () => {
  const rows = [
    row(1, { last_activity_at: "2026-10-08T10:00:00+00:00" }),
    row(2, { last_activity_at: "2026-10-08T09:00:00+00:00" }),
  ];
  const after = touched(rows, 2, "2026-10-08T11:00:00.000Z");
  assert.deepEqual(bookOrder(after).map((r) => r.id), [2, 1]);
  assert.equal(rows[1].last_activity_at, "2026-10-08T09:00:00+00:00"); // input untouched
});

// ── lighting ────────────────────────────────────────────────────────────────

const SEEN = "2026-10-08T10:00:00.000000+00:00";
const BEFORE = "2026-10-08T09:59:59.999999+00:00";
const AFTER = "2026-10-08T10:00:00.000001+00:00";

test("green: something landed after the person last had the page open, and it is not open", () => {
  assert.equal(tabLight(row(1, { seen_at: SEEN, last_landed_at: AFTER }), false), "green");
  assert.equal(tabLight(row(1, { seen_at: SEEN, last_landed_at: SEEN }), false), null);
  assert.equal(tabLight(row(1, { seen_at: SEEN, last_landed_at: BEFORE }), false), null);
  assert.equal(tabLight(row(1, { seen_at: SEEN, last_landed_at: null }), false), null);
  // the open page never lights green: the person is reading it
  assert.equal(tabLight(row(1, { seen_at: SEEN, last_landed_at: AFTER }), true), null);
});

test("amber while something waits on the person, open or not; amber wins over green", () => {
  const waiting = row(1, { waiting_on_you: 2, ask_cards_open: 1, approvals_waiting: 1, seen_at: SEEN, last_landed_at: AFTER });
  assert.equal(tabLight(waiting, false), "amber");
  assert.equal(tabLight(waiting, true), "amber");
  assert.equal(tabLight(row(1, { waiting_on_you: 0, seen_at: SEEN, last_landed_at: BEFORE }), false), null);
});

test("a page never opened since the mark existed lights only for what lands after the book first read it", () => {
  const first = row(1, { seen_at: null, last_landed_at: SEEN });
  const map = withBaselines(new Map(), [first, row(2, { seen_at: null, last_landed_at: null }), row(3, { seen_at: SEEN })]);
  assert.equal(map.get(1), SEEN);
  assert.equal(map.get(2), null);
  assert.equal(map.has(3), false); // it has a mark of its own
  // what was already there does not light it
  assert.equal(tabLight(first, false, baselineOf(map, 1)), null);
  // a later landing does
  assert.equal(tabLight(row(1, { seen_at: null, last_landed_at: AFTER }), false, baselineOf(map, 1)), "green");
  // a page with nothing landed when first read lights at its first landing
  assert.equal(tabLight(row(2, { seen_at: null, last_landed_at: BEFORE }), false, baselineOf(map, 2)), "green");
  // a baseline is taken once: a re-read does not move it forward
  const again = withBaselines(map, [row(1, { seen_at: null, last_landed_at: AFTER })]);
  assert.equal(again.get(1), SEEN);
  // no baseline taken: dark
  assert.equal(tabLight(row(9, { seen_at: null, last_landed_at: AFTER }), false, baselineOf(map, 9)), null);
});

test("the tab's second line says what waits, what landed, or where the page is", () => {
  assert.equal(tabNote(row(1, { waiting_on_you: 1, approvals_waiting: 1 }), "amber", false), "1 Code Approval waits");
  assert.equal(tabNote(row(1, { waiting_on_you: 3, approvals_waiting: 3 }), "amber", false), "3 Code Approvals wait");
  assert.equal(tabNote(row(1, { waiting_on_you: 1, ask_cards_open: 1 }), "amber", false), "1 question for you");
  assert.equal(tabNote(row(1, { waiting_on_you: 3, ask_cards_open: 1, approvals_waiting: 2 }), "amber", false), "3 waiting on you");
  assert.equal(tabNote(row(1), "green", false), "something landed");
  assert.equal(tabNote(row(1), null, true), "the Host is working");
  assert.equal(tabNote(row(1, { step_in_flight_title: "Login form" }), null, false), "building: Login form");
  assert.equal(tabNote(row(1, { group: "pending" }), null, false), "pending");
});

// ── the Host at work ────────────────────────────────────────────────────────

test("working dots: the running turns of project threads, by thread key", () => {
  const turns = [
    { status: "running" as const, thread_key: "project|128|7" },
    { status: "done" as const, thread_key: "project|128|8" },
    { status: "running" as const, thread_key: "thread|128|abc" },
    { status: "running" as const, thread_key: "workbench|128|none" },
    { status: "running" as const, thread_key: "project|128|12" },
    { status: "running" as const },
  ];
  assert.deepEqual([...workingPages(turns)].sort((a, b) => a - b), [7, 12]);
  assert.deepEqual([...workingPages(null)], []);
  // a turn that stopped since the last read sends the book to the list
  assert.deepEqual(stoppedWorking(new Set([7, 12]), new Set([12])), [7]);
  assert.deepEqual(stoppedWorking(new Set([7]), new Set([7, 12])), []);
});

test("the poll cadences: running turns often (memory only), the list on its own every 10 s", () => {
  assert.equal(TURNS_POLL_MS, 4000);
  assert.equal(LIST_POLL_MS, 10000);
  assert.ok(TURNS_POLL_MS < LIST_POLL_MS);
});

// ── seen-mark timing ────────────────────────────────────────────────────────

test("opening a page marks it seen, once per opening", () => {
  assert.equal(seenOnOpen(null, 7), 7); // the book's first page
  assert.equal(seenOnOpen(7, 8), 8); // a flip
  assert.equal(seenOnOpen(8, 8), null); // a re-render of the same page
  assert.equal(seenOnOpen(8, null), null); // nothing open
});

test("a turn landing while its page is open marks it seen; one on another page does not", () => {
  assert.equal(seenOnSettle(7, 7), 7);
  assert.equal(seenOnSettle(7, 8), null);
  assert.equal(seenOnSettle(null, 7), null);
  assert.equal(seenOnSettle(7, null), null);
});

test("the open page's row showing a landing after its mark marks it seen; nothing else does", () => {
  const rows = [
    row(7, { seen_at: SEEN, last_landed_at: AFTER }),
    row(8, { seen_at: SEEN, last_landed_at: AFTER }),
    row(9, { seen_at: SEEN, last_landed_at: BEFORE }),
    row(10, { seen_at: null, last_landed_at: AFTER }),
  ];
  assert.equal(seenOnList(7, rows), 7); // landed on the page in front
  assert.equal(seenOnList(9, rows), null); // nothing new
  assert.equal(seenOnList(null, rows), null);
  assert.equal(seenOnList(99, rows), null); // not in the list
  // no mark yet: the opening's own mark is on its way, so no second one
  assert.equal(seenOnList(10, rows), null);
});

test("the server's mark is adopted and never moves back, so the next poll asks nothing", () => {
  const rows = [row(7, { seen_at: SEEN, last_landed_at: AFTER }), row(8, { seen_at: SEEN })];
  const marked = withSeen(rows, 7, "2026-10-08T10:00:05.000000+00:00");
  assert.equal(marked[0].seen_at, "2026-10-08T10:00:05.000000+00:00");
  assert.equal(seenOnList(7, marked), null);
  assert.equal(tabLight(marked[0], false), null); // flipped away: dark
  assert.equal(marked[1], rows[1]); // other rows untouched
  // an older answer (a slow request) never moves a mark back
  assert.equal(withSeen(marked, 7, SEEN)[0].seen_at, "2026-10-08T10:00:05.000000+00:00");
  // and after the mark, a newer landing lights the tab again once it is not open
  const landed = marked.map((r) => (r.id === 7 ? { ...r, last_landed_at: "2026-10-08T10:00:06.000000+00:00" } : r));
  assert.equal(tabLight(landed[0], false), "green");
  assert.equal(seenOnList(7, landed), 7);
});

// The exact seen mark. The reply the person watched arrive is
// written after its stream ends, however late; the page's settle mark names
// its turn (PUT /seen {turn_token}) and the server's unseen_landed_at leaves
// it out, while anything else that lands after the mark is in it
// (scripts/test_workbench_seen_mark_pg.py proves the server side on a real
// database). The book reads that field and nothing else for green.
const MARK = "2026-10-08T10:00:05.000000+00:00";
const at = (s: number) => `2026-10-08T10:${String(Math.floor((5 + s) / 60)).padStart(2, "0")}:${String((5 + s) % 60).padStart(2, "0")}.000000+00:00`;

test("the watched reply never lights its tab, however late its row lands: the server leaves it out", () => {
  // Written 92 s after the mark (a failing embedder), 10 minutes after, an
  // hour after (PII mode's drain): last_landed_at moves, unseen stays empty.
  for (const late of [at(1), at(92), at(600), "2026-10-08T11:00:05.000000+00:00"]) {
    const r = row(7, { seen_at: MARK, last_landed_at: late, unseen_landed_at: null });
    assert.equal(tabLight(r, false), null, late);
    assert.equal(seenOnList(7, [r]), null, late);
  }
});

test("anything else that lands after the mark lights it: a report, another turn's reply, a finished task", () => {
  // The watched reply landed too (last_landed_at), but the server's unseen
  // landing is the report's, after the mark: lit, and marked once opened.
  const r = row(7, { seen_at: MARK, last_landed_at: at(92), unseen_landed_at: at(3) });
  assert.equal(tabLight(r, false), "green");
  assert.equal(tabNote(r, "green", false), "something landed");
  assert.equal(seenOnList(7, [r]), 7);
  // the open page never lights; amber still wins
  assert.equal(tabLight(r, true), null);
  assert.equal(tabLight({ ...r, waiting_on_you: 1, approvals_waiting: 1 }, false), "amber");
});

test("a mark the book stored after the list was read covers what that read called unseen", () => {
  // The read left before the PUT answered: its unseen landing is older than
  // the mark now held, so the page has been seen since.
  const read = row(7, { seen_at: MARK, last_landed_at: at(3), unseen_landed_at: at(3) });
  const marked = withSeen([read], 7, at(10));
  assert.equal(tabLight(marked[0], false), null);
  assert.equal(seenOnList(7, marked), null);
  // ...and a landing after that mark still lights
  assert.equal(tabLight({ ...marked[0], unseen_landed_at: at(11) }, false), "green");
});

test("a row from a server without unseen_landed_at reads last_landed_at (3a's rule)", () => {
  assert.equal(landedUnseen(row(7, { last_landed_at: at(3) })), at(3));
  assert.equal(landedUnseen(row(7, { last_landed_at: at(3), unseen_landed_at: null })), null);
  assert.equal(tabLight(row(7, { seen_at: MARK, last_landed_at: at(3) }), false), "green");
});

test("whatever lands on the page in front, the watched reply included, the page is read again", () => {
  const before = row(7, { seen_at: MARK, last_landed_at: at(-60), unseen_landed_at: null });
  const after = { ...before, last_landed_at: at(2) };
  assert.equal(landedMoved(at(-60), after), true); // the reply's row: the count ticks up
  assert.equal(landedMoved(at(-60), before), false);
  assert.equal(landedMoved(undefined, after), false); // a first reading: the opening reads it
  assert.equal(landedMoved(null, after), true); // the page's first landing
  assert.equal(landedMoved(at(2), null), false);
  // and the light stays out of it
  assert.equal(tabLight(after, false), null);
});

test("after a turn ends in front, the list is read again within seconds to catch its reply's row", () => {
  assert.deepEqual([...SETTLE_RECHECK_MS], [1200, 3500, 8000]);
  assert.ok(SETTLE_RECHECK_MS.every((ms, i) => i === 0 || ms > SETTLE_RECHECK_MS[i - 1]));
  assert.ok(SETTLE_RECHECK_MS[SETTLE_RECHECK_MS.length - 1] < LIST_POLL_MS);
});

// ── the phone ────────────────────────────────────────────────────────────────

test("the phone layout below 860 px, the same width book.css switches at", async () => {
  assert.equal(PHONE_MAX_PX, 860);
  assert.equal(PHONE_QUERY, "(max-width: 860px)");
  const asked: string[] = [];
  const win = (matches: boolean) => ({ matchMedia: (q: string) => (asked.push(q), { matches }) });
  assert.equal(isPhoneWidth(win(true)), true);
  assert.equal(isPhoneWidth(win(false)), false);
  assert.deepEqual(asked, [PHONE_QUERY, PHONE_QUERY]);
  assert.equal(isPhoneWidth(null), false);
  assert.equal(isPhoneWidth({ matchMedia: () => { throw new Error("no"); } }), false);
  const { readFile } = await import("node:fs/promises");
  const css = await readFile(new URL("../src/book/book.css", import.meta.url), "utf8");
  assert.ok(css.includes(`@media ${PHONE_QUERY} {`), "book.css switches to the phone at PHONE_QUERY");
});

test("a tab that lights is said once: not on the first reading, not going dark", () => {
  assert.deepEqual(newlyLit(null, new Map([[1, "amber"], [2, null]])), []);
  const prev = new Map<number, "amber" | "green" | null>([[1, "amber"], [2, null], [3, "green"]]);
  assert.deepEqual(newlyLit(prev, new Map([[1, "amber"], [2, "green"], [3, null]])), [2]);
  assert.deepEqual(newlyLit(prev, new Map([[1, "amber"], [2, null], [3, "amber"]])), [3]); // green -> amber
  assert.deepEqual(newlyLit(prev, new Map([[1, "amber"], [2, null], [9, "green"]])), [9]); // new to the tabs, lit
  assert.deepEqual(newlyLit(prev, new Map([[1, "amber"], [2, null], [3, "green"]])), []);
});

test("in the tablist, ↓ and ↑ move one tab, Home and End to either end; nothing past the ends", () => {
  const tabs = [21, 22, 23];
  assert.equal(tabKeyTarget(tabs, 21, "ArrowDown"), 22);
  assert.equal(tabKeyTarget(tabs, 22, "ArrowUp"), 21);
  assert.equal(tabKeyTarget(tabs, 23, "ArrowDown"), null);
  assert.equal(tabKeyTarget(tabs, 21, "ArrowUp"), null);
  assert.equal(tabKeyTarget(tabs, 22, "Home"), 21);
  assert.equal(tabKeyTarget(tabs, 22, "End"), 23);
  assert.equal(tabKeyTarget(tabs, 21, "Home"), null); // already there
  assert.equal(tabKeyTarget(tabs, 23, "End"), null);
  assert.equal(tabKeyTarget(tabs, 22, "ArrowRight"), null); // the book's own page turn
  assert.equal(tabKeyTarget(tabs, 99, "ArrowDown"), null);
  assert.equal(tabKeyTarget(tabs, null, "ArrowDown"), null);
});

test("a list read that left before the mark was stored never moves it back", () => {
  const MARK = "2026-10-08T10:00:05.000000+00:00";
  const current = [row(7, { seen_at: MARK }), row(8, { seen_at: SEEN })];
  // the read carries the marks as they were before the PUT answered
  const stale = [row(7, { seen_at: SEEN, last_landed_at: AFTER }), row(8, { seen_at: SEEN }), row(9)];
  const adopted = adoptList(current, stale);
  assert.equal(adopted[0].seen_at, MARK);
  assert.equal(adopted[0].last_landed_at, AFTER); // everything else is the read's
  assert.equal(adopted[1].seen_at, SEEN);
  assert.equal(adopted[2].id, 9);
  assert.equal(seenOnList(7, adopted), null); // so no second mark is asked for
  // a newer mark from the server (another window) is taken
  const NEWER = "2026-10-08T10:00:09.000000+00:00";
  assert.equal(adoptList(current, [row(7, { seen_at: NEWER })])[0].seen_at, NEWER);
});

test("'Where this stands' tints what changed since the person last looked, and only then", () => {
  assert.equal(changedSince(AFTER, SEEN), true);
  assert.equal(changedSince(BEFORE, SEEN), false);
  assert.equal(changedSince(SEEN, SEEN), false);
  assert.equal(changedSince(AFTER, null), false); // never looked: nothing to compare
  assert.equal(changedSince(null, SEEN), false);
});

// ── turning the page ────────────────────────────────────────────────────────

test("← and → turn pages outside text fields, and in the page's composer while it is empty", () => {
  const at = { modifier: false, dialogOpen: false, inTextField: false, composerEmpty: false };
  assert.equal(arrowsTurnPages(at), true);
  assert.equal(arrowsTurnPages({ ...at, inTextField: true }), false); // the arrows move the caret
  assert.equal(arrowsTurnPages({ ...at, inTextField: true, composerEmpty: true }), true);
  assert.equal(arrowsTurnPages({ ...at, modifier: true }), false);
  assert.equal(arrowsTurnPages({ ...at, dialogOpen: true }), false);
});

test("reduced motion selects a short crossfade; otherwise the page turns", () => {
  assert.deepEqual(flipMotion(false), { kind: "turn", ms: TURN_MS });
  assert.deepEqual(flipMotion(true), { kind: "fade", ms: FADE_MS });
  assert.equal(TURN_MS, 720);
  assert.ok(FADE_MS < 250);
  const media = (matches: boolean) => ({ matchMedia: (q: string) => ({ matches: matches && q === "(prefers-reduced-motion: reduce)" }) });
  assert.equal(prefersReducedMotion(media(true)), true);
  assert.equal(prefersReducedMotion(media(false)), false);
  assert.equal(prefersReducedMotion({}), false); // no matchMedia: the turn
  assert.equal(prefersReducedMotion(null), false);
  assert.equal(prefersReducedMotion({ matchMedia: () => { throw new Error("no"); } }), false);
  assert.equal(flipMotion(prefersReducedMotion(media(true))).kind, "fade");
});

test("→ turns to the next older page, ← to the next newer; nothing past either end", () => {
  const order = [9, 8, 7];
  assert.equal(flipNeighbour(order, 9, "ArrowRight"), 8);
  assert.equal(flipNeighbour(order, 8, "ArrowLeft"), 9);
  assert.equal(flipNeighbour(order, 7, "ArrowRight"), null);
  assert.equal(flipNeighbour(order, 9, "ArrowLeft"), null);
  assert.equal(flipNeighbour(order, 5, "ArrowRight"), null);
  assert.equal(flipNeighbour(order, null, "ArrowRight"), null);
  assert.equal(flipNeighbour(order, 9, "Enter"), null);
});

test("dark or light: the page's colour as the browser computes it", () => {
  // The stock dark tokens' page (--cseries-night) and the light design the
  // desktop contract's own test passes (scripts/test_desktop_contract.py LIGHT).
  assert.equal(isDarkSurface("rgb(14, 20, 22)"), true);
  assert.equal(isDarkSurface("rgb(244, 245, 247)"), false);
  assert.equal(isDarkSurface("rgba(244, 245, 247, 0.5)"), false);
  assert.equal(isDarkSurface("color(srgb 0.05 0.08 0.09)"), true);
  assert.equal(isDarkSurface("rgb(255 255 255)"), false);
  // Mid-grey sits either side of the point where light and dark words
  // contrast the same (relative luminance 0.179).
  assert.equal(isDarkSurface("rgb(110, 110, 110)"), true);
  assert.equal(isDarkSurface("rgb(125, 125, 125)"), false);
  assert.equal(isDarkSurface(""), null);
  assert.equal(isDarkSurface("var(--paper)"), null);
  // Drawn wholly clear (a background the browser could not draw): no answer of
  // its own; what shows is under it (src/book/scheme.ts reads the desk then).
  assert.equal(isDarkSurface("rgba(0, 0, 0, 0)"), null);
  assert.equal(isDarkSurface("rgba(244, 245, 247, 0)"), null);
  assert.equal(isDarkSurface("color(srgb 0.05 0.08 0.09 / 0)"), null);
  assert.equal(alphaOf("rgba(244, 245, 247, 0.5)"), 0.5);
  assert.equal(alphaOf("rgb(14 20 22 / 25%)"), 0.25);
  assert.equal(alphaOf("rgb(14, 20, 22)"), 1);
  assert.ok(Math.abs((luminanceOf("rgb(255, 255, 255)") ?? 0) - 1) < 1e-9);
  assert.equal(luminanceOf("rgb(0, 0, 0)"), 0);
});
