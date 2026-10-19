// What the cards took over from the rail when it went: the pure rules behind the ported controls.
import { test } from "node:test";
import assert from "node:assert/strict";
import { groupJoinOutcome, markersBefore, turnTypeDefault, withOlder } from "../src/lib/cards.ts";
import { acceptsFile, MAX_FILE_BYTES, sortFiles } from "../src/lib/attachments.ts";

const msgs = [
  { id: "10", createdAt: "2026-10-01T10:00:00Z" },
  { id: "11", createdAt: "2026-10-01T11:00:00Z" },
  { id: "12", createdAt: "2026-10-01T12:00:00Z" },
];

test("a marker pinned to a turn goes before that turn's message, and only there", () => {
  const mk = [{ summary_id: 1, topic: "Lake", at: "2026-10-01T09:00:00Z", from_turn_id: 11 }];
  assert.deepEqual(markersBefore(mk, msgs, 0), []);
  assert.deepEqual(markersBefore(mk, msgs, 1), mk);
  assert.deepEqual(markersBefore(mk, msgs, 2), []);
});

test("a marker with no turn goes before the first message at or after its moment", () => {
  const mk = [{ summary_id: 2, topic: "Gift", at: "2026-10-01T11:30:00Z", from_turn_id: null }];
  assert.deepEqual(markersBefore(mk, msgs, 1), []);
  assert.deepEqual(markersBefore(mk, msgs, 2), mk);
  // Before the first message when it is older than all of them.
  const early = [{ summary_id: 3, topic: "Start", at: "2026-09-01T00:00:00Z", from_turn_id: null }];
  assert.deepEqual(markersBefore(early, msgs, 0), early);
  assert.deepEqual(markersBefore(early, msgs, 5), []);
});

test("the turn-type picker offers staying in the page's own type, else the Host decides", () => {
  assert.equal(turnTypeDefault({ type_name: "Research" }), "Stay in Research");
  assert.equal(turnTypeDefault({ type_name: "  " }), "Let the Host decide");
  assert.equal(turnTypeDefault(null), "Let the Host decide");
});

test("an earlier page goes above the newest, each message once", () => {
  const older = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const newest = [{ id: "c" }, { id: "d" }];
  assert.deepEqual(
    withOlder(older, newest).map((r) => r.id),
    ["a", "b", "c", "d"],
  );
  assert.deepEqual(withOlder([], newest), newest);
});

test("letting a Host into a group says what it did", () => {
  assert.equal(
    groupJoinOutcome({ added: false, contact_grant_delivered: true }, "Wren", "Book club"),
    "Wren was already on Book club. They are a contact now.",
  );
  assert.match(
    groupJoinOutcome({ invite: { status: "delivered" } }, "Wren", "Book club"),
    /^Wren is on Book club, and their Host has the invite\. They are a contact here; their Host has not confirmed/,
  );
  assert.match(
    groupJoinOutcome({ invite: { status: "refused", reason: "full" } }, "Wren", "Book club"),
    /refused the invite \(full\), so it will not be tried again/,
  );
  assert.match(groupJoinOutcome({}, "Wren", "Book club"), /invited and waiting for their Host/);
});

test("the composer takes the Host's reading types and names what it refuses", () => {
  assert.equal(acceptsFile({ name: "photo.png", type: "image/png" }), true);
  assert.equal(acceptsFile({ name: "notes.md", type: "" }), true);
  assert.equal(acceptsFile({ name: "report.pdf", type: "application/pdf" }), true);
  assert.equal(acceptsFile({ name: "setup.exe", type: "application/x-msdownload" }), false);
  const files = [
    { name: "a.png", type: "image/png", size: 10 },
    { name: "b.exe", type: "application/x-msdownload", size: 10 },
    { name: "c.pdf", type: "application/pdf", size: MAX_FILE_BYTES + 1 },
  ];
  const s = sortFiles(files);
  assert.deepEqual(s.take.map((f) => f.name), ["a.png"]);
  assert.deepEqual(s.wrongType.map((f) => f.name), ["b.exe"]);
  assert.deepEqual(s.tooBig.map((f) => f.name), ["c.pdf"]);
  assert.equal(MAX_FILE_BYTES, 25 * 1024 * 1024);
});
