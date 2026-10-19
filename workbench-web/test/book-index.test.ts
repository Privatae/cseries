import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  LOOSE_PAGES,
  RIFFLE_MAX,
  RIFFLE_TURN_MS,
  archivedNote,
  byActivity,
  chapterCount,
  chapterOf,
  chaptersOf,
  excerptParts,
  modeToKeep,
  newPageTitle,
  newRequestId,
  outcomeLine,
  pickLine,
  pickRequestId,
  readIndexMode,
  riffleRun,
  riffleSchedule,
  sendOutcome,
  startOutcome,
  startWords,
  indexQueryText,
} from "../src/lib/book-index.ts";
import type { IndexPick } from "../src/lib/book-index.ts";
import type { Bucket, Project } from "../src/lib/types.ts";

const at = (min: number) => new Date(Date.UTC(2026, 9, 9, 12, 0) - min * 60_000).toISOString();
const page = (id: number, min: number, extra: Partial<Project> = {}): Project => ({
  id,
  name: `Page ${id}`,
  status: "active",
  last_activity_at: at(min),
  bucket_id: null,
  ...extra,
});
const bucket = (id: number, name: string, color: string | null = null): Bucket => ({ id, name, color, sort_order: id });

test("readIndexMode: send is the default; only 'open' is open", () => {
  assert.equal(readIndexMode(null), "send");
  assert.equal(readIndexMode(""), "send");
  assert.equal(readIndexMode("open"), "open");
  assert.equal(readIndexMode("OPEN"), "send");
});

test("modes: 'new' is chosen each time, never read back, never kept", () => {
  // Starting a page makes a project and a Code Repository on every Enter, so
  // an Index that opened in that mode would make one for a search it meant.
  assert.equal(readIndexMode("new"), "send");
  assert.equal(modeToKeep("send"), "send");
  assert.equal(modeToKeep("open"), "open");
  assert.equal(modeToKeep("new"), null);
});

test("chaptersOf: every bucket in its order, its archived and open pages newest first, then Loose pages", () => {
  const rows = [
    page(1, 30, { bucket_id: 7, status: "archived" }),
    page(2, 10, { bucket_id: 7, status: "archived" }),
    page(3, 5, { bucket_id: 7 }),
    page(4, 1, { status: "archived" }),
    page(5, 2),
    page(6, 3, { bucket_id: 99, status: "archived" }), // a bucket that is gone
    page(7, 4, { bucket_id: 8, status: "archived" }),
  ];
  const ch = chaptersOf(rows, [bucket(7, "Billing", "#10b981"), bucket(8, "Garden", "teal")]);
  assert.deepEqual(ch.map((c) => [c.id, c.name]), [[7, "Billing"], [8, "Garden"], [null, LOOSE_PAGES]]);
  assert.deepEqual(ch[0].archived.map((p) => p.id), [2, 1]);
  assert.deepEqual(ch[0].open.map((p) => p.id), [3]);
  assert.equal(ch[0].color, "#10b981");
  assert.equal(ch[1].color, null, "a colour that is not a hex is not trusted");
  assert.deepEqual(ch[2].archived.map((p) => p.id), [4, 6], "archived in no bucket, or in one that is gone");
  assert.deepEqual(ch[2].open, [], "an open page in no chapter is just a page of the book");
  assert.equal(chapterOf(ch, rows[0])?.name, "Billing");
  assert.equal(chapterOf(ch, rows[3])?.name, LOOSE_PAGES);
  assert.equal(chapterCount(ch[0]), "2 pages · 1 open");
  assert.equal(chapterCount(ch[1]), "1 page");
  assert.match(archivedNote(ch[0]), /in the Billing chapter\. Write on it and it comes back to the top of the book\.$/);
  assert.match(archivedNote(ch[2]), /with the loose pages/);
});

test("byActivity: newest first, no stamp last, a tie to the newer id", () => {
  const rows = [page(1, 5), page(2, 1), { ...page(3, 0), last_activity_at: null }, page(4, 5)];
  assert.deepEqual(byActivity(rows).map((p) => p.id), [2, 4, 1, 3]);
});

test("riffleRun: the pages before the target, nearest last; made up from after it near the top; at most RIFFLE_MAX", () => {
  const pages = Array.from({ length: 12 }, (_, i) => ({ id: i + 1 }));
  assert.deepEqual(riffleRun(pages, 10).map((p) => p.id), [3, 4, 5, 6, 7, 8, 9]);
  assert.deepEqual(riffleRun(pages, 4).map((p) => p.id), [1, 2, 3]);
  assert.deepEqual(riffleRun(pages, 2).map((p) => p.id), [1, 8, 7, 6, 5, 4, 3]);
  assert.deepEqual(riffleRun(pages, 1).map((p) => p.id), [8, 7, 6, 5, 4, 3, 2]);
  assert.deepEqual(riffleRun(pages, null).map((p) => p.id), [1, 2, 3, 4, 5, 6, 7], "a chapter opened: its first pages");
  assert.deepEqual(riffleRun([{ id: 1 }], 1), [], "a chapter of one page: nothing goes past");
  assert.equal(RIFFLE_MAX, 7);
});

test("riffleSchedule: one page every 120 ms, the last two slowing; over after the last page's turn", () => {
  const { delays, total } = riffleSchedule(5);
  assert.deepEqual(delays, [60, 180, 300, 530, 760]);
  assert.equal(total, 760 + RIFFLE_TURN_MS + 60);
  assert.deepEqual(riffleSchedule(0), { delays: [], total: RIFFLE_TURN_MS + 60 });
});

test("excerptParts: the matched words, never the name again, cut on a word and never inside a mark", () => {
  assert.deepEqual(excerptParts(null), []);
  assert.deepEqual(excerptParts({ from: "name", text: "Quokka", parts: [{ text: "Quokka", mark: true }] }), []);
  const parts = [
    { text: "Saw the ", mark: false },
    { text: "quokka", mark: true },
    { text: " near the feeder again, and again, and again", mark: false },
  ];
  const out = excerptParts({ from: "conversation", text: "", parts }, 30);
  assert.deepEqual(out.slice(0, 2), parts.slice(0, 2));
  assert.equal(out[2].text, " near the feeder…");
  assert.equal(out[2].mark, false);
});

test("newRequestId: 32 hex characters, the server's id shape, a new one each time", () => {
  const a = newRequestId();
  assert.match(a, /^[0-9a-f]{32}$/);
  assert.notEqual(a, newRequestId());
});

test("sendOutcome: started, a turn refused, an ask, a refused restore (FastAPI's 409 detail), errors", () => {
  assert.deepEqual(
    sendOutcome(200, { state: "started", project_id: 22, project_name: "Newsletter signup", restored: false, created: false, reason: "r" }),
    { kind: "landed", projectId: 22, name: "Newsletter signup", restored: false, created: false, reason: "r" },
  );
  assert.equal((sendOutcome(200, { state: "nudged", project_id: 3, project_name: "x" }) as { kind: string }).kind, "landed");
  assert.deepEqual(
    sendOutcome(200, { state: "refused", project_id: 3, project_name: "x", restored: true, error: "busy" }),
    { kind: "turn_refused", projectId: 3, name: "x", restored: true, created: false, error: "busy" },
  );
  assert.deepEqual(
    sendOutcome(200, { state: "ask", failure: "no call", candidates: [{ project_id: 4, name: "A", archived: true }, { name: "no id" }] }),
    { kind: "ask", failure: "no call", candidates: [{ project_id: 4, name: "A", archived: true }] },
  );
  assert.deepEqual(
    sendOutcome(409, { detail: { state: "restore_refused", project_id: 29, project_name: "Old", reason: "its Code Repository is taken" } }),
    { kind: "restore_refused", projectId: 29, name: "Old", reason: "its Code Repository is taken" },
  );
  assert.deepEqual(sendOutcome(400, { detail: "text is required" }), { kind: "error", message: "text is required" });
  assert.equal((sendOutcome(0, null) as { kind: string }).kind, "error");
  assert.equal(sendOutcome(500, null).kind, "error");
  assert.equal(outcomeLine({ kind: "restore_refused", projectId: 1, name: "Old", reason: "taken" }),
    "Old could not come back from the archive: taken");
  assert.equal(outcomeLine({ kind: "landed", projectId: 1, name: "Kites", restored: false, created: true, reason: null }),
    "Started a new page: Kites");
});

test("pickRequestId: one message out at a time; the same words after no answer keep their id; else a new one", () => {
  let n = 0;
  const fresh = () => `id-${++n}`;
  assert.equal(pickRequestId(null, "hello", fresh), "id-1");
  const out: IndexPick = { text: "hello", requestId: "id-1", outcome: null };
  assert.equal(pickRequestId(out, "hello", fresh), null, "a second Enter while the Host picks sends nothing");
  assert.equal(pickRequestId(out, "other words", fresh), null, "...whatever is typed");
  const lost: IndexPick = { ...out, outcome: { kind: "error", message: "timed out" } };
  assert.equal(pickRequestId(lost, "hello", fresh), "id-1", "no answer came: the retry is the same send");
  assert.equal(pickRequestId(lost, "hello again", fresh), "id-2", "other words are another send");
  const landed: IndexPick = {
    ...out,
    outcome: { kind: "landed", projectId: 3, name: "x", restored: false, created: false, reason: null },
  };
  assert.equal(pickRequestId(landed, "hello", fresh), "id-3", "an answered send is done: the same words again are a new one");
});

test("pickLine: picking while out; what an answer that landed nowhere came to; nothing for a landing", () => {
  const out: IndexPick = { text: "hi", requestId: "a", outcome: null };
  assert.equal(pickLine(null), "");
  assert.equal(pickLine(out), "Picking the page…");
  assert.equal(
    pickLine({ ...out, outcome: { kind: "landed", projectId: 1, name: "x", restored: false, created: false, reason: null } }),
    "",
  );
  assert.equal(
    pickLine({ ...out, outcome: { kind: "turn_refused", projectId: 1, name: "x", restored: false, created: false, error: "e" } }),
    "",
  );
  assert.equal(
    pickLine({ ...out, outcome: { kind: "restore_refused", projectId: 9, name: "Old", reason: "taken" } }),
    "Old could not come back from the archive: taken",
  );
  assert.equal(pickLine({ ...out, outcome: { kind: "ask", failure: "f", candidates: [] } }),
    "The Host could not pick a page. Which page is this for?");
});

test("a page started from the Index: its words are out while it is made, and the gate holds them", () => {
  let n = 0;
  const fresh = () => `id-${++n}`;
  const making: IndexPick = { text: "A kite shop for my sister", requestId: "id-1", outcome: null, startsPage: true };
  assert.equal(pickLine(making), "Starting the page…", "no Host picks: a page is made");
  assert.equal(pickLine({ ...making, startsPage: undefined }), "Picking the page…");
  assert.equal(pickRequestId(making, "A kite shop for my sister", fresh), null, "a second Enter while it is made starts nothing");
  assert.equal(pickRequestId(making, "other words", fresh), null, "...whatever is typed");
  const done: IndexPick = { ...making, outcome: startOutcome({ ok: true, data: { id: 31, name: "Kite shop" } }, "x") };
  assert.equal(pickLine(done), "", "a page made shows itself: the book turns to it");
  assert.equal(pickRequestId(done, "A kite shop for my sister", fresh), "id-1", "made: the same words may start another page");
  const failed: IndexPick = { ...making, outcome: startOutcome({ ok: false, message: "timed out" }, "x") };
  assert.equal(pickLine(failed), "The page could not be started: timed out", "a page that was not made says why, the words stay");
});

test("startOutcome: a page made has landed, created; a failure says why; no id is an error", () => {
  assert.deepEqual(
    startOutcome({ ok: true, data: { id: 31, name: "Kite shop" } }, "Kites"),
    { kind: "landed", projectId: 31, name: "Kite shop", restored: false, created: true, reason: null },
  );
  assert.equal((startOutcome({ ok: true, data: { id: 31 } }, "Kites") as { name: string }).name, "Kites", "the title asked for stands in for a name the answer left out");
  assert.deepEqual(startOutcome({ ok: false, message: "write:projects not granted" }, "Kites"), {
    kind: "error",
    message: "The page could not be started: write:projects not granted",
  });
  assert.equal(startOutcome({ ok: true, data: { id: Number.NaN } }, "Kites").kind, "error");
  assert.equal(
    outcomeLine(startOutcome({ ok: true, data: { id: 31, name: "Kite shop" } }, "Kites")),
    "Started a new page: Kite shop",
  );
});

test("startWords: the page is made with every secret shape cut, hard and soft; plain words are kept", () => {
  // The page is made (POST /api/projects) before the composer's strip has
  // been shown: nothing of a secret may go with it. Fixtures as
  // secret-shapes.test.ts.
  const hard = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij";
  const soft = "hunter2hunter2";
  const words = `Build a bot with ${hard} and password = ${soft} for the shop`;
  const { title, description } = startWords(words);
  for (const s of [hard, soft, "sk-ant"]) {
    assert.ok(!description.includes(s), `the description keeps none of ${s}: ${description}`);
    assert.ok(!title.includes(s), `the title keeps none of ${s}: ${title}`);
  }
  assert.ok(description.includes("[REDACTED:anthropic-key]"), description);
  assert.ok(!title.includes("REDACTED") && title.startsWith("Bot with"), title);
  assert.deepEqual(startWords(`${hard}`), { title: "New page", description: "[REDACTED:anthropic-key]" });
  const plain = "I want to build a kite shop. It should take cards.";
  assert.deepEqual(startWords(plain), { title: newPageTitle(plain), description: plain });
});

test("newPageTitle: the Start box's suggestion from the words; 'New page' when they suggest none", () => {
  assert.equal(newPageTitle("I want to build a kite shop. It should take cards."), "Kite shop");
  assert.equal(newPageTitle("Is the confirmation email ready to read? And the subject line?"), "Is the confirmation email ready to read");
  assert.equal(newPageTitle("   "), "New page");
});

test("indexQueryText: a secret pasted into the Index is not in what the search sends", () => {
  const key = "sk-ant-api03-" + "Q".repeat(40) + "-AbCdEf";
  const q = `deploy the shop with ${key} please`;
  const out = indexQueryText(q);
  assert.ok(!out.includes(key), out);
  assert.ok(!out.includes("sk-ant"), out);
  assert.equal(out, "deploy the shop with please");
  // A soft shape (password = …) is taken out too: the server's own cut is hard only.
  const soft = indexQueryText("login password = hunter2hunter2 for the shop");
  assert.ok(!soft.includes("hunter2hunter2"), soft);
  // Words with no secret shape go through untouched.
  assert.equal(indexQueryText("checkout feels slow on phones"), "checkout feels slow on phones");
});

test("indexQueryText: typed a character at a time, no search holds a whole secret, and the finished one is cut out", () => {
  // The search runs after every pause in the typing, so each prefix of what is
  // typed is asked (INDEX_DEBOUNCE_MS). A shape is found once it is whole: a
  // shorter prefix of a secret is sent as it is, which is why the words go in a
  // POST body and never in a URL (scripts/test_workbench_book.py, "TYPED").
  const key = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij";
  const value = "hunter2hunter2";
  for (const [typed, secret] of [
    [`password = ${value}`, value],
    [`Build a bot with ${key} for the shop`, key],
  ] as const) {
    for (let i = 1; i <= typed.length; i++) {
      const sent = indexQueryText(typed.slice(0, i));
      assert.ok(!sent.includes(secret), `prefix ${i} of ${JSON.stringify(typed)} sent ${JSON.stringify(sent)}`);
    }
    // All typed: not a trace of the secret is left, and the words around it are.
    const done = indexQueryText(typed);
    assert.ok(!done.includes(secret.slice(0, 7)), done);
  }
  assert.equal(indexQueryText(`password = ${value}`), "password =");
  assert.equal(indexQueryText(`Build a bot with ${key} for the shop`), "Build a bot with for the shop");
});

test("the Index's search and the Start box's search send POST bodies: no typed text is put in a URL", () => {
  // api.ts imports through the app's aliases, which plain `node --test` does
  // not resolve, so its source is read (as run-kill.test.ts reads it). The
  // body holds the words as JSON; encodeURIComponent threw URIError on a lone
  // surrogate half, and a URL is kept by whatever sits in front of the pod.
  const api = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
  const fn = (name: string) => {
    const at = api.indexOf(`export function ${name}(`);
    assert.ok(at >= 0, `${name} is in api.ts`);
    return api.slice(at, api.indexOf("\n}\n", at));
  };
  assert.match(fn("searchIndex"), /request<IndexAnswer>\("POST", "\/api\/projects\/index", \{ q: indexQueryText\(q\), limit \}\)/);
  assert.match(fn("searchProjects"), /request<SearchResult>\("POST", "\/api\/projects\/search", \{ q, limit \}\)/);
  for (const name of ["searchIndex", "searchProjects"]) {
    assert.ok(!/encodeURIComponent|\?q=|getJSON/.test(fn(name)), fn(name));
  }
});
