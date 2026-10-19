import { test } from "node:test";
import assert from "node:assert/strict";
import { sendRevision } from "../src/lib/tickets-view.ts";

// support and support:<id> open the Support card: lib/cards.ts cardsTarget,
// pinned in test/cards.test.ts. What waits on you, drafts and the platform's
// questions included, is the cards' (cards-app.tsx; Waiting on you).

test("Send names the draft version on screen; one read without a version names 0", () => {
  // The Host can rewrite a draft while it is open; the server
  // refuses a Send naming an older version, so a missing one must not skip it.
  assert.equal(sendRevision({ revision: 3 }), 3);
  assert.equal(sendRevision({ revision: 0 }), 0);
  assert.equal(sendRevision({}), 0);
  assert.equal(sendRevision({ revision: null }), 0);
});
