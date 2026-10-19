import { test } from "node:test";
import assert from "node:assert/strict";
import { blockNote } from "../src/lib/block-note.ts";
import type { Contact } from "../src/lib/types.ts";

function contact(over: Partial<Contact>): Contact {
  return {
    id: 1, handle: "h", display_name: null, kind: "host", notes: null,
    status: "active", last_contact_at: null, created_at: null, peer_entity_id: null,
    ...over,
  };
}

const juno = contact({ id: 1, handle: "juno", display_name: "Juno", peer_entity_id: 9122 });
const ana = contact({ id: 2, handle: "ana@example.com", display_name: "Ana", kind: "human",
                      peer_entity_id: 9122 });
const bob = contact({ id: 3, handle: "bob@example.com", display_name: "Bob", kind: "human",
                      peer_entity_id: 9122 });

test("a Host: blocking it does not stop the people on it", () => {
  assert.equal(blockNote(juno, [juno, ana]), "Blocking Juno doesn't stop messages from Ana.");
  assert.equal(blockNote({ ...juno, status: "blocked" }, [juno, ana, bob]),
               "Messages from Ana and Bob still arrive.");
  assert.equal(blockNote(juno, [juno]), "Blocking Juno doesn't stop messages from the people who use it.");
});

test("a Host added by hand, with no Host id: says the block can't take effect yet", () => {
  const byHand = { ...juno, peer_entity_id: null };
  assert.equal(blockNote(byHand, [byHand, ana]),
               "Your Host doesn't know which Host this is yet, so blocking it can't take effect.");
});

test("a person: blocking them blocks their Host, named when it is a contact", () => {
  assert.equal(blockNote(ana, [juno, ana]), "Blocking Ana also blocks their Host, Juno.");
  assert.equal(blockNote({ ...ana, status: "blocked" }, [juno, ana]), "This also blocks their Host, Juno.");
  assert.equal(blockNote(ana, [ana]), "Blocking Ana also blocks their Host.");
});

test("a person whose Host is unknown: only their own messages", () => {
  const solo = { ...ana, peer_entity_id: null };
  assert.equal(blockNote(solo, [solo]),
               "Blocking Ana stops their messages. Their Host isn't known yet, so it won't be blocked.");
  assert.equal(blockNote({ ...solo, status: "blocked" }, [solo]),
               "Their Host isn't known yet, so it isn't blocked.");
});

test("a Host blocked through a person says so, and drops that person from who still arrives", () => {
  const blockedAna = { ...ana, status: "blocked" as const };
  const through = { ...juno, blocked_through: ["Ana"] };
  assert.equal(blockNote(through, [through, blockedAna, bob]),
               "Blocked because you blocked Ana. Messages from Bob still arrive.");
});
