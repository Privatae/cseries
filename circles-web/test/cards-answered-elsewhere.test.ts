// A card answered somewhere else goes: the person approves
// in Network, in Tickets or in another window while the card is up in this
// thread; the next poll of the thread's open tickets no longer names it, the
// card reads its ticket back, and goes once the ticket is no longer open. A
// card the list merely does not name, and whose ticket is still open, stays.
// app-web-kit/ask.ts, as use-circles-chat's raiseTickets calls it.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  asksFromTickets,
  closedAsks,
  queueAsk,
  type TicketRead,
  unlistedAsks,
} from "../../app-web-kit/ask.ts";

const approvalCard = {
  id: 41,
  status: "open",
  sitrep: "Let a Telegram account talk to your Host",
  options: [
    { id: "let_in", label: "Let this account in" },
    { id: "dont_pair", label: "Don't pair" },
  ],
  metadata: { kind: "connector_pairing", connector_pairing: { grant_id: 7, stepup_option: "let_in" } },
};

/** GET /api/tickets/{id}, answered from `rows` (absent = 404); records reads. */
function server(rows: Record<number, string>, reads: number[] = []) {
  return async (id: number): Promise<TicketRead> => {
    reads.push(id);
    return id in rows ? { ok: true, data: { id, status: rows[id] } } : { ok: false, status: 404 };
  };
}

test("approved in Network while the card is up: the next poll drops it", async () => {
  // Opening the thread: the ticket is open and comes up as its card.
  let queue = asksFromTickets([approvalCard], () => false);
  assert.deepEqual(queue.map((a) => a.ticketId), [41]);
  // Network decides it; the poll's open list no longer names it.
  const unlisted = unlistedAsks(queue, []);
  assert.deepEqual(unlisted, [41]);
  const closed = await closedAsks(unlisted, server({ 41: "decided" }));
  assert.deepEqual(closed, [41]);
  queue = queue.filter((a) => !closed.includes(a.ticketId));
  assert.deepEqual(queue, [], "the card is gone, so it cannot be clicked into a 409");
});

test("a card the list still names is not read back", async () => {
  const queue = asksFromTickets([approvalCard], () => false);
  assert.deepEqual(unlistedAsks(queue, [approvalCard]), []);
});

test("absence alone never drops a card: a ticket that reads back open stays", async () => {
  // A live turn raised ticket 50 after the poll's read started: the list
  // does not name it yet, and it is open.
  const live = { ...approvalCard, id: 50 };
  let queue = asksFromTickets([approvalCard], () => false);
  queue = queueAsk(queue, asksFromTickets([live], () => false)[0]);
  const reads: number[] = [];
  const unlisted = unlistedAsks(queue, [approvalCard]);
  assert.deepEqual(unlisted, [50]);
  assert.deepEqual(await closedAsks(unlisted, server({ 50: "open" }, reads)), []);
  assert.deepEqual(reads, [50]);
});

test("a failed read keeps the card; a ticket that is gone (404) drops it", async () => {
  const flaky = async (id: number): Promise<TicketRead> =>
    id === 60 ? { ok: false, status: 503 } : { ok: false, status: 404 };
  assert.deepEqual(await closedAsks([60, 61], flaky), [61]);
  const throws = async (): Promise<TicketRead> => {
    throw new Error("offline");
  };
  assert.deepEqual(await closedAsks([62], throws), []);
});

test("a row the list returns that is not open does not count as listed", () => {
  const queue = asksFromTickets([approvalCard], () => false);
  assert.deepEqual(unlistedAsks(queue, [{ ...approvalCard, status: "decided" }]), [41]);
});
