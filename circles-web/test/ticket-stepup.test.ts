import { test } from "node:test";
import assert from "node:assert/strict";
// The rule moved into the shared kit (2026-10-05): the ask card over either
// chat reads it for a ticket raised in the conversation, as the Tickets pane does.
import { stepupAsk } from "../../app-web-kit/ask.ts";

test("a pipeline proposal that adds an outside check asks for the code on 'apply'", () => {
  const a = stepupAsk({ kind: "pipeline_proposal", pipeline_proposal: { requires_stepup: true } });
  assert.equal(a?.option, "apply");
  assert.match(a?.why ?? "", /outside the platform/);
  assert.equal(stepupAsk({ kind: "pipeline_proposal", pipeline_proposal: { requires_stepup: false } }), null);
});

test("a messaging connector's pairing card asks for the code on the option the platform named", () => {
  // The shape server/network/nac_connector.py files (metadata.connector_pairing.stepup_option).
  const a = stepupAsk({ kind: "connector_pairing", connector_pairing: { grant_id: 4, stepup_option: "let_in" } });
  assert.deepEqual(a, { option: "let_in", why: "This lets a messaging account start turns in your chat." });
});

test("a board post card asks for the code on 'Post it'", () => {
  // The kind runtime/board_post_approval files; decide_ticket_service checks the code.
  const a = stepupAsk({ kind: "board_post_proposal", board_post: { op_id: "host-1", body: "Hello" } });
  assert.deepEqual(a, { option: "post", why: "This puts text on the members' board, under your Host's name." });
});

test("no code box without the platform's own payload", () => {
  for (const md of [
    null, undefined, "x", [], {},
    { kind: "connector_pairing" },
    { kind: "connector_pairing", connector_pairing: { stepup_option: "" } },
    { kind: "connector_pairing", connector_pairing: { stepup_option: 3 } },
    { connector_pairing: { stepup_option: "let_in" } },
    { kind: "decision", requires_stepup: true },
  ]) {
    assert.equal(stepupAsk(md), null, JSON.stringify(md));
  }
});
