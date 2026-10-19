// A card answered somewhere else goes, Workbench side: one
// queue holds every project's cards, keyed by thread; the poll reads one
// project's open tickets, so only that project's cards are read back, and a
// card goes once its ticket is no longer open. app-web-kit/ask.ts, as
// use-workbench-chat's raiseTickets calls it.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  type Ask,
  asksFromTickets,
  closedAsks,
  type TicketRead,
  unlistedAsks,
} from "../../app-web-kit/ask.ts";

const gateCard = (id: number) => ({
  id,
  status: "open",
  sitrep: "Phase 2 is gated on your approval",
  options: [
    { id: "approve", label: "Approve — build it" },
    { id: "send_back", label: "Not yet — send it back" },
  ],
  metadata: { kind: "phase_gate" },
});

const keyed = (rows: unknown[], key: string): Array<Ask & { key: string }> =>
  asksFromTickets(rows, () => false).map((a) => ({ ...a, key }));

function server(rows: Record<number, string>, reads: number[] = []) {
  return async (id: number): Promise<TicketRead> => {
    reads.push(id);
    return id in rows ? { ok: true, data: { id, status: rows[id] } } : { ok: false, status: 404 };
  };
}

test("approved elsewhere while the card is up on project 3: its next poll drops it", async () => {
  let queue = keyed([gateCard(70)], "p:3");
  const unlisted = unlistedAsks(queue, [], "p:3");
  assert.deepEqual(unlisted, [70]);
  const closed = await closedAsks(unlisted, server({ 70: "decided" }));
  queue = queue.filter((a) => !closed.includes(a.ticketId));
  assert.deepEqual(queue, []);
});

test("another project's cards are not this list's to judge", async () => {
  const queue = [...keyed([gateCard(70)], "p:3"), ...keyed([gateCard(80)], "p:4")];
  // Project 3's list names its own card; project 4's card is not in it.
  assert.deepEqual(unlistedAsks(queue, [gateCard(70)], "p:3"), []);
  assert.deepEqual(unlistedAsks(queue, [], "p:4"), [80]);
});

test("a card whose ticket reads back open stays, whatever the list said", async () => {
  const reads: number[] = [];
  const queue = keyed([gateCard(90)], "p:5");
  const unlisted = unlistedAsks(queue, [], "p:5");
  assert.deepEqual(await closedAsks(unlisted, server({ 90: "open" }, reads)), []);
  assert.deepEqual(reads, [90]);
});
