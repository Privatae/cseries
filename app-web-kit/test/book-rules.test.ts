import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  baselineOf,
  changedSince,
  tabLight,
  withBaselines,
  workingPages,
  type BookPage,
  type PageOfTurnKey,
} from "../book/rules.ts";
import * as rules from "../book/rules.ts";

// The book's rules as the kit has them (app-web-kit/book/rules.ts), on pages
// that name no project: string ids, as Circles has them (a thread's uuid, or
// "original" for the first conversation), and the reader each app supplies to
// map a running turn's thread key to its page (server/turn_registry.py
// compose_thread_key). The Workbench's own reader, on its numeric project ids,
// is pinned in workbench-web/test/book.test.ts through lib/book.ts.

const THREAD_A = "5f0c2a9e-8d1b-4c3e-9a7f-1b2c3d4e5f60";
const THREAD_B = "0b7e1f42-3c9d-4a8e-b2f1-6d5c4b3a2918";

/** Circles' reader: a thread is `thread|<entity>|<id>`, the original
 * conversation `circles|<entity>|none`. */
const circlesPage: PageOfTurnKey = (key) => {
  const t = /^thread\|[^|]*\|(.+)$/.exec(key);
  if (t) return t[1];
  return /^circles\|[^|]*\|none$/.test(key) ? "original" : null;
};

/** A project reader on string ids: `project|<entity>|<id>`. */
const projectPage: PageOfTurnKey = (key) => /^project\|[^|]*\|(\d+)$/.exec(key)?.[1] ?? null;

const turns = [
  { status: "running", thread_key: `thread|ent-1|${THREAD_A}` },
  { status: "done", thread_key: `thread|ent-1|${THREAD_B}` },
  { status: "running", thread_key: "circles|ent-1|none" },
  { status: "running", thread_key: "project|ent-1|12" },
  { status: "running", thread_key: "workbench|ent-1|none" },
  { status: "running" },
];

test("a Circles-shaped key lights the working dots on its own page, and nothing else", () => {
  const working = workingPages(turns, circlesPage);
  assert.deepEqual([...working].sort(), ["original", THREAD_A].sort());
  assert.ok(!working.has(THREAD_B), "a finished turn is not working");
  assert.ok(!working.has("12"), "a project's turn is on none of Circles' pages");
});

test("project|… keys still work on the kit's rule", () => {
  assert.deepEqual([...workingPages(turns, projectPage)], ["12"]);
  assert.deepEqual([...workingPages(null, circlesPage)], []);
});

test("tabs light by their marks, keyed by string ids", () => {
  const SEEN = "2026-10-10T09:00:00.000000+00:00";
  const LATER = "2026-10-10T09:00:00.000400+00:00";
  const pages: BookPage[] = [
    { id: "original", name: "The original conversation", seen_at: SEEN, unseen_landed_at: LATER },
    { id: THREAD_A, name: "Thursday dinner", seen_at: SEEN, unseen_landed_at: null, waiting_on_you: 2 },
    { id: THREAD_B, name: "Gift for Dad", seen_at: null, last_landed_at: SEEN },
  ];
  assert.equal(tabLight(pages[0], false), "green", "something landed 0.4 ms after the mark");
  assert.equal(tabLight(pages[0], true), null, "the page in front never lights green");
  assert.equal(tabLight(pages[1], true), "amber", "something waits, open or not");

  const baselines = withBaselines(new Map<string, string | null>(), pages);
  assert.deepEqual([...baselines.keys()], [THREAD_B], "only a page with no seen mark is held to a baseline");
  assert.equal(baselineOf(baselines, THREAD_B), SEEN);
  assert.equal(baselineOf(baselines, "original"), undefined);
  assert.equal(tabLight(pages[2], false, baselineOf(baselines, THREAD_B)), null);
  assert.equal(tabLight({ ...pages[2], last_landed_at: LATER }, false, baselineOf(baselines, THREAD_B)), "green");
});

test("what changed since the person looked: to the microsecond, and nothing with no mark", () => {
  const SEEN = "2026-10-10T09:00:00.000000+00:00";
  assert.equal(changedSince("2026-10-10T09:00:00.000400+00:00", SEEN), true);
  assert.equal(changedSince(SEEN, SEEN), false);
  assert.equal(changedSince("2026-10-10T09:00:00.000400+00:00", null), false);
  assert.equal(changedSince(null, SEEN), false);
});

test("the kit's look carries no palette of its own: every colour is a desktop token", async () => {
  const css = (await readFile(new URL("../book/book.css", import.meta.url), "utf8"))
    .replace(/\/\*[\s\S]*?\*\//g, "");
  // A token's stock value, as its fallback, is the desktop's own default.
  const bare = css.replace(/var\(--[\w-]+,\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))\)/g, "var()");
  // The one literal is the shadows' darkness (--shade), which has no hue.
  assert.deepEqual(bare.match(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)/g), ["#000"]);
  assert.ok(/--shade: #000;/.test(bare));
});

test("the kit's look carries the typefaces it names, from beside it", async () => {
  const css = await readFile(new URL("../book/book.css", import.meta.url), "utf8");
  const urls = [...css.matchAll(/url\("\.\/(fonts\/[^"]+)"\)/g)].map((m) => m[1]);
  assert.equal(urls.length, 5, "four faces and their licence");
  for (const u of urls) await readFile(new URL(`../book/${u}`, import.meta.url));
});

test("newlyLit: lights that came on or changed colour since the last reading, never on the first", () => {
  const { newlyLit } = rules;
  assert.deepEqual(newlyLit(null, new Map([["a", "amber"]])), []);
  const prev = new Map<string, "amber" | "green" | null>([["a", "amber"], ["b", null]]);
  assert.deepEqual(newlyLit(prev, new Map([["a", "amber"], ["b", "green"], ["c", "green"], ["d", null]])), ["b", "c"]);
  assert.deepEqual(newlyLit(prev, new Map([["a", "green"]])), ["a"]);
  assert.deepEqual(newlyLit(prev, new Map([["a", null]])), []);
});

test("stoppedWorking: the pages whose turn stopped since the last read", () => {
  const { stoppedWorking } = rules;
  assert.deepEqual(stoppedWorking(new Set(["original", THREAD_A]), new Set([THREAD_A])), ["original"]);
  assert.deepEqual(stoppedWorking(new Set([7]), new Set([7, 12])), []);
});
