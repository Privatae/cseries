import { test } from "node:test";
import assert from "node:assert/strict";
// Another Host asking to use one of the person's machines: the pop-up the
// anchor Host files from a heartbeat (server/network/pairing_grants.py
// _raise_card), raised by the chat from the conversation's open tickets.
import { askFromTicket, choicesAsk, pickedChoices, stepupAsk } from "../../app-web-kit/ask.ts";

// The shape pairing_grants._raise_card files (metadata.bastion_pairing).
const card = {
  id: 12,
  status: "open",
  sitrep: "a-host wants to use box-a. Let it in?",
  options: [
    { id: "let_in", label: "Let it in", consequence: "Needs a code from your authenticator." },
    { id: "never", label: "Never" },
  ],
  metadata: {
    kind: "bastion_pairing",
    bastion_pairing: {
      token_id: 7,
      server: "box-a",
      subject: "a-host.zone.example.test",
      stepup_option: "let_in",
      ceiling: [
        { id: "observe", label: "See how the machine is doing", on: true },
        { id: "egress", label: "Send its internet traffic out", on: false },
        { id: "inference", label: "Use the AI models on it", on: true },
      ],
    },
  },
};

test("the card asks for the code on 'Let it in' only", () => {
  assert.deepEqual(stepupAsk(card.metadata), {
    option: "let_in",
    why: "This lets another Host use one of your machines.",
  });
  const ask = askFromTicket(card);
  assert.equal(ask?.stepupOption, "let_in");
  assert.deepEqual(ask?.options.map((o) => o.id), ["let_in", "never"]);
});

test("the card carries the ceiling as boxes, the defaults ticked", () => {
  const ask = askFromTicket(card);
  assert.deepEqual(ask?.choices?.items.map((c) => [c.id, c.on]), [
    ["observe", true],
    ["egress", false],
    ["inference", true],
  ]);
  // untouched → the defaults; a tick and an untick → in the card's order
  assert.deepEqual(pickedChoices(ask!.choices!, {}), ["observe", "inference"]);
  assert.deepEqual(pickedChoices(ask!.choices!, { egress: true, observe: false }), ["egress", "inference"]);
});

test("no boxes and no code without the platform's own kind", () => {
  for (const md of [
    null, {}, { kind: "decision" },
    { bastion_pairing: card.metadata.bastion_pairing },
    { kind: "bastion_pairing" },
    { kind: "bastion_pairing", bastion_pairing: { ceiling: [] } },
  ]) {
    assert.equal(choicesAsk(md), null, JSON.stringify(md));
  }
  assert.equal(stepupAsk({ bastion_pairing: card.metadata.bastion_pairing }), null);
  assert.equal(askFromTicket({ ...card, metadata: { kind: "decision" } })?.choices, undefined);
});
