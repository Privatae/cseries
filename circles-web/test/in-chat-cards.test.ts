// The cards that come up in the chat they belong to (2026-10-05): a ticket
// read back from the conversation's list, the pairing code card, a credential
// sealed from the card, and "Not now" remembered. app-web-kit/ask.ts, shared
// by Circles and Workbench.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  askFromTicket,
  asksFromTickets,
  circlesRoom,
  countdownText,
  pairingFromConnectors,
  pairingFromTool,
  pairingInstruction,
  parseSetAside,
  projectRoom,
  roomOf,
  SET_ASIDE_MAX,
  sealedNote,
  sealErrorText,
  withSetAside,
} from "../../app-web-kit/ask.ts";

// The row nac_connector._file_approval_ticket files, as GET /api/tickets
// returns it.
const pairingTicket = {
  id: 41,
  status: "open",
  sitrep: "Let a Telegram account talk to your Host",
  options: [
    { id: "let_in", label: "Let this account in", consequence: "Needs a code from your authenticator." },
    { id: "dont_pair", label: "Don't pair" },
  ],
  metadata: {
    kind: "connector_pairing",
    connector_pairing: { grant_id: 7, stepup_option: "let_in" },
    asked_in: { app_id: "circles", project_id: null, thread_id: "0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e" },
  },
};

test("a connector's approval ticket becomes the card with the code box the platform named", () => {
  const a = askFromTicket(pairingTicket);
  assert.ok(a);
  assert.equal(a.ticketId, 41);
  assert.equal(a.toolCallId, "ticket:41");
  assert.deepEqual(a.options.map((o) => o.id), ["let_in", "dont_pair"]);
  assert.equal(a.stepupOption, "let_in");
  assert.equal(a.stepupWhy, "This lets a messaging account start turns in your chat.");
});

test("a Host-written ticket never gets a code box, whatever its options say", () => {
  const a = askFromTicket({
    id: 5,
    status: "open",
    sitrep: "Pick one",
    options: [{ id: "let_in", label: "Let this account in" }],
    metadata: { kind: "decision", connector_pairing: { stepup_option: "let_in" } },
  });
  assert.ok(a);
  assert.equal(a.stepupOption, undefined);
});

test("a credential ask carries its field; nothing closed or empty is raised", () => {
  const a = askFromTicket({
    id: 9,
    status: "open",
    sitrep: "I need the bot token",
    options: [{ id: "seal", label: "Store it", credential_request: { name: "telegram-bot", cred_type: "opaque" } }],
    metadata: {},
  });
  assert.deepEqual(a?.options[0].credential_request, { name: "telegram-bot", cred_type: "opaque", purpose: undefined });
  assert.equal(askFromTicket({ ...pairingTicket, status: "decided" }), null);
  assert.equal(askFromTicket({ ...pairingTicket, options: [] }), null);
  assert.equal(askFromTicket({ ...pairingTicket, id: "x" }), null);
  assert.equal(askFromTicket(null), null);
});

test("a conversation's list is raised oldest first, leaving out what was settled", () => {
  const rows = [
    { ...pairingTicket, id: 12 },
    { ...pairingTicket, id: 3 },
    { ...pairingTicket, id: 8 },
  ];
  assert.deepEqual(asksFromTickets(rows, (id) => id === 8).map((a) => a.ticketId), [3, 12]);
  assert.deepEqual(asksFromTickets("nope", () => false), []);
});

test("rooms: the keys GET /api/tickets?asked_in= takes, and what a recorded asked_in names", () => {
  assert.equal(circlesRoom("original"), "thread:original");
  assert.equal(circlesRoom(null), "thread:original");
  assert.equal(circlesRoom("0E7C9A4E-2B1F-4B7E-9C1D-2F3A4B5C6D7E"), "thread:0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e");
  assert.equal(projectRoom(251), "project:251");
  assert.equal(roomOf(pairingTicket.metadata.asked_in), "thread:0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e");
  assert.equal(roomOf({ app_id: "workbench", project_id: 251, thread_id: null }), "project:251");
  assert.equal(roomOf({ app_id: "circles", project_id: null, thread_id: null }), "thread:original");
  assert.equal(roomOf({ app_id: "scheduled_task" }), null);
  assert.equal(roomOf(null), null);
});

test("the pairing card comes from the kernel's connector result, never with a code in it", () => {
  const tool = {
    id: "call_1",
    name: "propose_nac_policy",
    args: { kind: "connector", app_id: "tg", platform: "telegram" },
    result: { status: "ok", result: { draft_id: 7, kind: "connector", platform: "telegram" } },
  };
  assert.deepEqual(pairingFromTool(tool), { grantId: 7, platform: "telegram", toolCallId: "call_1" });
  // Through `operate`, named by args.tool.
  assert.equal(
    pairingFromTool({ ...tool, name: "operate", args: { tool: "propose_nac_policy" } })?.grantId,
    7,
  );
  // Not a connector, refused, or another tool: no card.
  assert.equal(pairingFromTool({ ...tool, result: { status: "ok", result: { draft_id: 7, kind: "policy" } } }), null);
  assert.equal(pairingFromTool({ ...tool, result: { status: "error", result: "refused" } }), null);
  assert.equal(pairingFromTool({ ...tool, name: "escalate" }), null);
});

test("an open pairing started in this conversation is found again on opening it", () => {
  const room = "thread:0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e";
  const asked = pairingTicket.metadata.asked_in;
  const list = [
    { id: 3, status: "pairing", code_held: true, platform: "telegram", asked_in: asked },
    { id: 5, status: "pairing", code_held: true, platform: "telegram", asked_in: asked },
    { id: 6, status: "pairing", code_held: false, platform: "telegram", asked_in: asked },
    { id: 8, status: "awaiting_approval", code_held: false, platform: "telegram", asked_in: asked },
    { id: 9, status: "pairing", code_held: true, platform: "telegram", asked_in: { app_id: "workbench", project_id: 2 } },
  ];
  assert.deepEqual(pairingFromConnectors(list, room), { grantId: 5, platform: "telegram", toolCallId: "connector:5" });
  assert.equal(pairingFromConnectors(list, "thread:original"), null);
});

test("what the card says", () => {
  assert.equal(pairingInstruction("telegram"), "Send this code to your bot in a Telegram direct message.");
  assert.equal(countdownText(598), "9:58");
  assert.equal(countdownText(-3), "0:00");
});

test("a sealed credential is decided with its name, never its value", () => {
  assert.equal(sealedNote("telegram-bot", ""), 'stored in Keys as "telegram-bot"');
  assert.equal(sealedNote("telegram-bot", " thanks "), 'stored in Keys as "telegram-bot". thanks');
  assert.match(sealErrorText(409, "conflict", "telegram-bot"), /already exists in Keys/);
  assert.equal(sealErrorText(500, "boom", "x"), "Could not store it in Keys: 500 boom");
});

test("Not now is remembered, newest kept, bounded", () => {
  let raw: string | null = null;
  raw = withSetAside(raw, "ticket:1");
  raw = withSetAside(raw, "pairing:7");
  raw = withSetAside(raw, "ticket:1");
  assert.deepEqual(parseSetAside(raw), ["pairing:7", "ticket:1"]);
  assert.deepEqual(parseSetAside("{not json"), []);
  for (let i = 0; i < SET_ASIDE_MAX + 5; i++) raw = withSetAside(raw, `ticket:${100 + i}`);
  const kept = parseSetAside(raw);
  assert.equal(kept.length, SET_ASIDE_MAX);
  assert.equal(kept[kept.length - 1], `ticket:${100 + SET_ASIDE_MAX + 4}`);
});
