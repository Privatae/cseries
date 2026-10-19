import { test } from "node:test";
import assert from "node:assert/strict";
import { readIndex, type HeldSend } from "../../app-web-kit/book/nudge.ts";

// Circles' conversation holds each nudge it sends until the turn's
// nudge_read receipt (use-circles-chat nudgeRunningTurn), on the book's rule
// (app-web-kit/book/nudge.ts readIndex). It holds only the thread on screen's
// copies, all under one key, and each copy carries the id it was nudged with
// (POST /api/circles/nudge client_id), which the receipt echoes.

const KEY = "this-thread";
const held = (text: string, clientId?: string): HeldSend => ({ key: KEY, text, ...(clientId ? { clientId } : {}) });
const SECRET_WORDS = "my key is sk-live-0123456789abcdef, use it for the deploy";
const AS_READ = "my key is [REDACTED:api_key], use it for the deploy";

test("a receipt with the words' id settles that copy, whatever the platform cut from the text", () => {
  // The old rule matched the receipt's opening against the held text; the
  // secret cut from it changed the opening, so the copy was never settled and
  // went again as the next message once the turn ended.
  assert.equal(readIndex([held(SECRET_WORDS, "n-1-1")], KEY, AS_READ, "n-1-1"), 0);
});

test("a receipt with an id settles only the copy with that id, even among the same words", () => {
  const pending = [held("Use the red one.", "n-1-1"), held("Use the red one.", "n-1-2")];
  assert.equal(readIndex(pending, KEY, "Use the red one.", "n-1-2"), 1);
  assert.equal(readIndex(pending, KEY, "Use the red one.", "n-9-9"), -1);
});

test("a receipt with no id (an older kernel, the ask card's decide) matches by the text's opening", () => {
  const answer = "Ticket #41 — I chose “Elm Lantern”, and keep the dock on the left, the way it was";
  assert.equal(readIndex([held(answer, "n-1-1")], KEY, answer.slice(0, 60)), 0);
  assert.equal(readIndex([held(answer, "n-1-1")], KEY, "Ticket #42 — I chose “Tin Robot”."), -1);
  assert.equal(readIndex([held(answer, "n-1-1")], KEY, ""), -1);
});
