import { test } from "node:test";
import assert from "node:assert/strict";
import { historyKeys, isLocalEcho, mergeHistory, stampTurnToken } from "../src/lib/history-merge.ts";
import { isOwnHostTurn } from "../../app-web-kit/visuals/visual.ts";
import type { ChatMessage } from "../src/lib/types.ts";

const row = (
  id: string,
  role: "user" | "assistant",
  text: string,
  createdAt: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage => ({ id, role, text, tools: [], createdAt, ...extra });

test("isLocalEcho matches localId shapes only", () => {
  assert.equal(isLocalEcho("u-1754870000000-3"), true);
  assert.equal(isLocalEcho("h-1754870000000-12"), true);
  assert.equal(isLocalEcho("6425"), false); // numeric turn-row id
  assert.equal(isLocalEcho("0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e"), false); // uuid
});

test("unchanged rows keep OBJECT IDENTITY across adoption", () => {
  const a = row("1", "user", "hi", "2026-08-11T00:00:01Z");
  const b = row("2", "assistant", "hello", "2026-08-11T00:00:02Z");
  const local = [a, b];
  const server = [
    row("1", "user", "hi", "2026-08-11T00:00:01Z"),
    row("2", "assistant", "hello", "2026-08-11T00:00:02Z"),
  ];
  const out = mergeHistory(local, server);
  assert.equal(out[0], a); // same reference, not just equal content
  assert.equal(out[1], b);
});

test("a delta fetch appends without dropping rows outside the window", () => {
  const local = [
    row("1", "user", "old", "2026-08-11T00:00:01Z"),
    row("2", "assistant", "older reply", "2026-08-11T00:00:02Z"),
  ];
  const server = [row("3", "assistant", "delivered", "2026-08-11T00:00:09Z")];
  const out = mergeHistory(local, server);
  assert.deepEqual(
    out.map((m) => m.id),
    ["1", "2", "3"],
  );
  assert.equal(out[0], local[0]);
});

test("a stale window (older than what's on screen) is ignored", () => {
  const local = [
    row("5", "user", "newest", "2026-08-11T00:05:00Z"),
  ];
  const server = [row("3", "assistant", "old", "2026-08-11T00:03:00Z")];
  assert.equal(mergeHistory(local, server), local);
});

test("streaming echo survives adoption; landed echo is replaced by the real row", () => {
  const streaming = row("a-1754870000000-2", "assistant", "partial", "2026-08-11T00:00:05Z", {
    streaming: true,
  });
  const userEcho = row("u-1754870000000-1", "user", "do the thing", "2026-08-11T00:00:04Z");
  const local = [row("1", "user", "hi", "2026-08-11T00:00:01Z"), userEcho, streaming];
  // Server has persisted the user turn but not the assistant yet.
  const server = [
    row("1", "user", "hi", "2026-08-11T00:00:01Z"),
    row("2", "user", "do the thing", "2026-08-11T00:00:04Z"),
  ];
  const out = mergeHistory(local, server);
  assert.deepEqual(
    out.map((m) => m.id),
    ["1", "2", "a-1754870000000-2"], // echo user turn replaced by row 2; stream kept
  );
  // Once the assistant turn lands and streaming ended, the echo dedups away.
  const done = { ...streaming, streaming: false };
  const server2 = [
    ...server,
    row("3", "assistant", "partial", "2026-08-11T00:00:06Z"),
  ];
  const out2 = mergeHistory([out[0], out[1], done], server2);
  assert.deepEqual(
    out2.map((m) => m.id),
    ["1", "2", "3"],
  );
});

test("errored echo is never wiped by a refetch", () => {
  const failed = row("a-1754870000000-9", "assistant", "", "2026-08-11T00:00:05Z", {
    error: "502 — upstream",
  });
  const local = [row("1", "user", "hi", "2026-08-11T00:00:01Z"), failed];
  const server = [row("1", "user", "hi", "2026-08-11T00:00:01Z")];
  const out = mergeHistory(local, server);
  assert.deepEqual(out.map((m) => m.id), ["1", "a-1754870000000-9"]);
});

test("changed server content replaces the local object", () => {
  const localRow = row("2", "assistant", "streamed text", "2026-08-11T00:00:02Z");
  const serverRow = row("2", "assistant", "streamed text (edited)", "2026-08-11T00:00:02Z");
  const out = mergeHistory([localRow], [serverRow]);
  assert.equal(out[0], serverRow);
});

// The window showed replies twice and out
// of order until a reload. The saved reply is not always the streamed text —
// here, text streamed ahead of a tool call that the saved row does not carry —
// so the text match never fired and the finished echo stayed pinned below
// every newer row.
test("a finished echo is swapped for its saved row by turn token, not text", () => {
  const T = "5f0c1e2d3b4a";
  const local = [
    row("10", "user", "earlier", "2026-09-24T19:30:00Z"),
    row("u-1790000000000-1", "user", "now try again all 4", "2026-09-24T19:36:29Z", { turnToken: T }),
    row("a-1790000000000-2", "assistant", "Let me check the files.All four are off.", "2026-09-24T19:36:30Z", { turnToken: T }),
  ];
  const server = [
    row("8865", "user", "now try again all 4", "2026-09-24T19:36:29Z", { turnToken: T }),
    row("8866", "assistant", "All four are off.", "2026-09-24T19:37:19Z", { turnToken: T }),
    row("8867", "assistant", "All four variants finished.", "2026-09-24T19:49:03Z"),
  ];
  const out = mergeHistory(local, server);
  assert.deepEqual(out.map((m) => m.id), ["10", "8865", "8866", "8867"]);
});

test("without a token the text match still swaps an identical echo", () => {
  const local = [
    row("1", "user", "hi", "2026-08-11T00:00:01Z"),
    row("a-1790000000000-3", "assistant", "hello", "2026-08-11T00:00:02Z"),
  ];
  const server = [row("2", "assistant", "hello", "2026-08-11T00:00:03Z")];
  assert.deepEqual(mergeHistory(local, server).map((m) => m.id), ["1", "2"]);
});

test("a streaming echo stays even when its turn's rows are saved", () => {
  // The row lands a moment before the stream closes; until then the live
  // bubble sits with its turn, just ahead of the saved reply.
  const T = "abc";
  const local = [
    row("a-1790000000000-4", "assistant", "partial", "2026-08-11T00:00:02Z", { turnToken: T, streaming: true }),
  ];
  const server = [row("2", "assistant", "final", "2026-08-11T00:00:03Z", { turnToken: T })];
  assert.deepEqual(mergeHistory(local, server).map((m) => m.id), ["a-1790000000000-4", "2"]);
});

test("stampTurnToken marks the reply and the user echo that prompted it", () => {
  const msgs = [
    row("7", "user", "saved earlier", "2026-08-11T00:00:00Z"),
    row("u-1790000000000-5", "user", "go", "2026-08-11T00:00:01Z"),
    row("a-1790000000000-6", "assistant", "", "2026-08-11T00:00:02Z"),
  ];
  const out = stampTurnToken(msgs, "a-1790000000000-6", "tok");
  assert.deepEqual(out.map((m) => m.turnToken), [undefined, "tok", "tok"]);
  // A saved user row is never restamped.
  const saved = [msgs[0], msgs[2]];
  assert.deepEqual(stampTurnToken(saved, "a-1790000000000-6", "tok").map((m) => m.turnToken), [undefined, "tok"]);
  // Unknown bubble: untouched, same array.
  assert.equal(stampTurnToken(msgs, "missing", "tok"), msgs);
});

test("a nudge (never saved) stays beside its turn, ahead of the saved reply", () => {
  const T = "tok-n";
  const local = [
    row("u-1790000000000-7", "user", "build it", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("u-1790000000000-8", "user", "use blue", "2026-09-24T10:00:05Z", { turnToken: T, nudge: true }),
    row("a-1790000000000-9", "assistant", "Done, in blue.", "2026-09-24T10:00:01Z", { turnToken: T }),
  ];
  const server = [
    row("20", "user", "build it", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("21", "assistant", "Done, in blue.", "2026-09-24T10:00:30Z", { turnToken: T }),
    row("22", "assistant", "A job finished.", "2026-09-24T10:05:00Z"),
  ];
  const out = mergeHistory(local, server);
  assert.deepEqual(out.map((m) => m.id), ["20", "u-1790000000000-8", "21", "22"]);
});

test("a nudge whose text equals the user's message is not swapped for that row", () => {
  const T = "tok-m";
  const local = [
    row("u-1790000000000-10", "user", "go", "2026-09-24T10:00:05Z", { turnToken: T, nudge: true }),
  ];
  const server = [row("30", "user", "go", "2026-09-24T10:00:00Z", { turnToken: T })];
  assert.deepEqual(mergeHistory(local, server).map((m) => m.id), ["30", "u-1790000000000-10"]);
});

test("a reply stopped before any row was written stays under its own question", () => {
  const T = "tok-s";
  const local = [
    row("u-1790000000000-11", "user", "try it", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("a-1790000000000-12", "assistant", "Starting…", "2026-09-24T10:00:01Z", { turnToken: T }),
  ];
  const server = [
    row("40", "user", "try it", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("41", "assistant", "A job finished.", "2026-09-24T10:03:00Z"),
  ];
  const out = mergeHistory(local, server);
  assert.deepEqual(out.map((m) => m.id), ["40", "a-1790000000000-12", "41"]);
});

test("stampTurnToken stamps nudges back to the prompting message, and no further", () => {
  const msgs = [
    row("u-1790000000000-13", "user", "earlier turn", "2026-09-24T09:00:00Z"),
    row("u-1790000000000-14", "user", "build it", "2026-09-24T10:00:00Z"),
    row("u-1790000000000-15", "user", "use blue", "2026-09-24T10:00:02Z", { nudge: true }),
    row("a-1790000000000-16", "assistant", "", "2026-09-24T10:00:01Z"),
  ];
  const out = stampTurnToken(msgs, "a-1790000000000-16", "T");
  assert.deepEqual(out.map((m) => m.turnToken), [undefined, "T", "T", "T"]);
});

test("Circles' own localId is recognised as a live echo", async () => {
  // api.ts pulls in @kit aliases node cannot resolve here, so run the
  // function as written in the source.
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
  const body = src.match(/let _seq = 0;\nexport function localId\(prefix: string\): string \{\n([\s\S]*?)\n\}/);
  assert.ok(body, "localId not found in api.ts");
  const localId = new Function("prefix", `let _seq = 0;\n${body[1]}`) as (p: string) => string;
  assert.equal(isLocalEcho(localId("u")), true);
  assert.equal(isLocalEcho(localId("a")), true);
});

test("an echo placed with its turn is still there after further merges", () => {
  const T = "tok-r";
  let screen = [
    row("u-1790000000000-17", "user", "build it", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("u-1790000000000-18", "user", "use blue", "2026-09-24T10:00:05Z", { turnToken: T, nudge: true }),
    row("a-1790000000000-19", "assistant", "Done.", "2026-09-24T10:00:01Z", { turnToken: T }),
  ];
  const server = [
    row("50", "user", "build it", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("51", "assistant", "Done.", "2026-09-24T10:00:30Z", { turnToken: T }),
  ];
  const want = ["50", "u-1790000000000-18", "51"];
  screen = mergeHistory(screen, server);
  assert.deepEqual(screen.map((m) => m.id), want);
  screen = mergeHistory(screen, [...server, row("52", "assistant", "A job finished.", "2026-09-24T10:05:00Z")]);
  assert.deepEqual(screen.map((m) => m.id), [...want, "52"]);
  screen = mergeHistory(screen, [row("52", "assistant", "A job finished.", "2026-09-24T10:05:00Z")]);
  assert.deepEqual(screen.map((m) => m.id), [...want, "52"]);
});

test("rows with no server id keep their key across refreshes and never pile up", () => {
  // The circle branch of get_thread sends no id; historyKeys builds one from
  // the row. 30 messages, pairs sharing a timestamp, some with none, and a
  // window of 20 that slides as new ones arrive.
  const all = Array.from({ length: 30 }, (_, i) => ({
    createdAt: i % 7 === 3 ? null : `2026-09-24T10:${String(Math.floor(i / 2)).padStart(2, "0")}:00Z`,
    author: i % 2 ? "host-a" : "Alex Rivera",
    text: i % 5 === 0 ? "ok" : `msg ${i}`,
  }));
  const fetch = (end: number) => {
    const win = all.slice(Math.max(0, end - 20), end);
    const keys = historyKeys(win);
    return win.map((r, i) =>
      row(keys[i], r.author === "Alex Rivera" ? "user" : "assistant", r.text, r.createdAt ?? "1970-01-01T00:00:00Z"),
    );
  };
  assert.equal(fetch(20).some((m) => isLocalEcho(m.id)), false);
  assert.equal(new Set(fetch(30).map((m) => m.id)).size, 20);
  let screen = fetch(20);
  for (let n = 0; n < 3; n++) screen = mergeHistory(screen, fetch(20));
  assert.equal(screen.length, 20);
  // Sliding: every row still in the window keeps the key it had.
  const before = new Map(fetch(25).map((m) => [m.text + m.createdAt + m.role, m.id]));
  for (const m of fetch(26)) {
    const k = before.get(m.text + m.createdAt + m.role);
    if (k) assert.equal(m.id, k);
  }
});

test("an errored bubble goes once its own turn's reply is saved", () => {
  // The connection died mid-stream and the turn finished on the server.
  const T = "tok-e";
  const local = [
    row("u-1790000000000-20", "user", "go", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("a-1790000000000-21", "assistant", "Half a rep", "2026-09-24T10:00:01Z", { turnToken: T, error: "network error" }),
  ];
  const server = [
    row("60", "user", "go", "2026-09-24T10:00:00Z", { turnToken: T }),
    row("61", "assistant", "Half a reply, then the rest.", "2026-09-24T10:00:40Z", { turnToken: T }),
  ];
  assert.deepEqual(mergeHistory(local, server).map((m) => m.id), ["60", "61"]);
});

test("historyKeys: a writer name with separators cannot forge another row's key", () => {
  const a = historyKeys([
    { createdAt: "T", author: "A", text: "x" },
    { createdAt: "T", author: "A", text: "x" },
  ]);
  const forged = historyKeys([{ createdAt: "T", author: `A:${a[0].split(":").pop()}`, text: "x" }]);
  assert.notEqual(forged[0], a[1]);
  assert.equal(new Set([...a, ...forged]).size, 3);
});

test("an older row SHARING the window's first timestamp is kept (ordered by timestamp, then id)", () => {
  // A page of the thread was put above (Load earlier); its newest row shares a
  // timestamp with the oldest row of the newest-N window a refresh brings.
  const local = [
    row("a", "assistant", "older, same second", "2026-10-02T09:00:00Z"),
    row("b", "assistant", "window start", "2026-10-02T09:00:00Z"),
    row("c", "assistant", "newest", "2026-10-02T09:00:05Z"),
  ];
  const server = [
    row("b", "assistant", "window start", "2026-10-02T09:00:00Z"),
    row("c", "assistant", "newest", "2026-10-02T09:00:05Z"),
  ];
  const out = mergeHistory(local, server);
  assert.deepEqual(out.map((m) => m.id), ["a", "b", "c"]);
});

test("the saved row that replaces a streamed echo keeps the own-Host mark", () => {
  // The live bubble ran its visual, then the
  // post-send reload swapped it for the saved row — which the server sends
  // as author_kind "entity" with the Host's name. Gated on those fields, the
  // page turned into code and the artifact card vanished a second after
  // every turn. The mark rides the saved row (mapThreadMessages ownThread).
  const T = "tok-own";
  const local = [
    row("1", "user", "hi", "2026-10-03T20:00:00Z"),
    row("u-1790000000000-7", "user", "draw it", "2026-10-03T20:01:00Z", { turnToken: T }),
    row("a-1790000000000-8", "assistant", "Here.", "2026-10-03T20:01:01Z", { turnToken: T, ownHost: true }),
  ];
  const server = [
    row("1", "user", "hi", "2026-10-03T20:00:00Z"),
    row("2", "user", "draw it", "2026-10-03T20:01:00Z", { turnToken: T }),
    row("3", "assistant", "Here.", "2026-10-03T20:01:05Z", {
      turnToken: T, author: "host-a", authorKind: "entity", ownHost: true,
    }),
  ];
  const out = mergeHistory(local, server);
  const reply = out.filter((m) => m.role === "assistant");
  assert.deepEqual(reply.map((m) => m.id), ["3"]);
  assert.equal(isOwnHostTurn(reply[0]), true);
});
