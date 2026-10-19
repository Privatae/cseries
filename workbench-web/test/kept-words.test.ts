import { test } from "node:test";
import assert from "node:assert/strict";
import { adoptKept, dropKept, keepWords, readIdsIn, readKept, wordsId, type KeptWords } from "../src/lib/kept-words.ts";

// In PII mode the nudge route writes nothing (held: false), so the page's copy
// is kept in this tab's sessionStorage until a turn has the words
// (src/lib/kept-words.ts; use-workbench-chat nudgeTurn, deliverKept).

class MemStore {
  m = new Map<string, string>();
  get length() { return this.m.size; }
  clear() { this.m.clear(); }
  getItem(k: string) { return this.m.has(k) ? (this.m.get(k) as string) : null; }
  key(i: number) { return [...this.m.keys()][i] ?? null; }
  removeItem(k: string) { this.m.delete(k); }
  setItem(k: string, v: string) { this.m.set(k, v); }
}
const N = "workbench.keptWords";
const w = (clientId: string, page = "pg-a"): KeptWords => ({
  key: "self:original", text: `words ${clientId}`, clientId, turnId: "t1", page, at: "2026-10-11T00:00:00Z",
});

test("kept words are read back, replaced under the same id, and go when a turn has them", () => {
  const s = new MemStore() as unknown as Storage;
  keepWords(s, N, w("n-1"));
  keepWords(s, N, w("n-2"));
  keepWords(s, N, { ...w("n-1"), text: "again" });
  assert.deepEqual(readKept(s, N).map((x) => [x.clientId, x.text]), [["n-2", "words n-2"], ["n-1", "again"]]);
  dropKept(s, N, "n-2");
  dropKept(s, N, "n-1");
  assert.equal(s.getItem(N), null, "nothing left: the key goes");
});

test("a reload's copies are taken over by the load that sends them", () => {
  const s = new MemStore() as unknown as Storage;
  keepWords(s, N, w("n-1", "pg-old"));
  adoptKept(s, N, ["n-1"], "pg-new");
  assert.equal(readKept(s, N)[0].page, "pg-new");
});

test("the ids a turn read come from its replayed receipts only", () => {
  const stream = [
    '2:[{"type":"session","session_id":"s"}]',
    '0:"Noted. "',
    '2:[{"type":"nudge_read","text":"x","client_id":"n-1"}]',
    '2:[{"type":"nudge","text":"y","client_id":"n-2"}]',
    "2:not json nudge_read",
  ].join("\n");
  assert.deepEqual([...readIdsIn(stream)], ["n-1"]);
});

test("no storage, or junk in it, reads as nothing", () => {
  assert.deepEqual(readKept(null, N), []);
  const s = new MemStore() as unknown as Storage;
  s.setItem(N, "{not json");
  assert.deepEqual(readKept(s, N), []);
});

test("a hard secret shape is cut before the words rest in the tab; the rest is kept as typed", () => {
  // A card's note, or a key the strip left in place (no Keys name for it),
  // reaches the nudge uncut; the pod cuts it from the words anyway
  // (session.push_nudge, send_message), so what rests on the device is what
  // the Host gets. Fixture as secret-shapes.test.ts.
  const s = new MemStore() as unknown as Storage;
  const key = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij";
  keepWords(s, N, { ...w("n-s"), text: `Use ${key} for the shop.` });
  const raw = s.getItem(N) || "";
  assert.ok(!raw.includes(key) && !raw.includes("sk-ant"), raw);
  assert.equal(readKept(s, N)[0].text, "Use [REDACTED:anthropic-key] for the shop.");
  keepWords(s, N, { ...w("n-p"), text: "Avoid tolls." });
  assert.equal(readKept(s, N)[1].text, "Avoid tolls.");
});

test("the id words are nudged and sent again under is a random tag of the local-echo shape", () => {
  // The server answers a second send under one id from the turn that has the
  // words, so two pages must never make the same one.
  const a = wordsId("n");
  const b = wordsId("n");
  assert.match(a, /^n-\d+-\d{11,}$/);
  assert.ok(a.length <= 64 && /^[A-Za-z0-9_-]+$/.test(a));
  assert.notEqual(a, b);
});
