import { test } from "node:test";
import assert from "node:assert/strict";
import {
  deliveryIsNamed,
  deliveryLanded,
  deliveryTargetsThread,
  evaluateDeliveryFetch,
} from "../src/lib/history-merge.ts";
import type { ChatMessage } from "../src/lib/types.ts";

const row = (
  id: string,
  role: "user" | "assistant",
  text: string,
  createdAt: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage => ({ id, role, text, tools: [], createdAt, ...extra });

/** The old success test, reproduced verbatim so the regression is pinned
 * against the thing that actually shipped, not a paraphrase of it. */
const threadGrew = (fetched: ChatMessage[], onScreen: ChatMessage[]): boolean => {
  const knownIds = new Set(onScreen.map((m) => m.id));
  return fetched.some((m) => !knownIds.has(m.id));
};

// Project 1: the person asked "did you fix it?" (row 565);
// the host answered that a build was running (row 566). Both are on screen as
// optimistic echoes — this client never swaps them for their server rows. The
// bench then saw task 126's `delivered_at` and fired, ~7s BEFORE the report row
// (572) was written.
const onScreenAtTheFlip = [
  row("562", "assistant", "I found it — and this time…", "2026-09-05T05:18:18Z"),
  row("u-1757081412000-1", "user", "did you fix it?", "2026-09-05T14:10:12Z"),
  row("h-1757081491000-2", "assistant", "It's running now on the kimi harness…", "2026-09-05T14:11:31Z"),
];
// What the delta fetch returned at 14:14:15: the user's own exchange, landed as
// server rows. The report does not exist yet.
const fetchedIntoTheGap = [
  row("562", "assistant", "I found it — and this time…", "2026-09-05T05:18:18Z"),
  row("565", "user", "did you fix it?", "2026-09-05T14:10:12Z"),
  row("566", "assistant", "It's running now on the kimi harness…", "2026-09-05T14:11:31Z"),
];
const theReport = row(
  "572",
  "assistant",
  "The follow-up pass of project 1 landed — the kimi harness (k3) finished it cleanly…",
  "2026-09-05T14:14:19Z",
  { agentTaskId: "126" },
);

test("REGRESSION: the old 'thread grew' test passed on the user's own turn", () => {
  // This is why the report never reached the screen: the loop called this
  // success and returned, 4s before the report row existed.
  assert.equal(threadGrew(fetchedIntoTheGap, onScreenAtTheFlip), true);
});

test("a delivery is not landed until ITS row is present", () => {
  const d = { rowId: 572, taskId: "126" };
  assert.equal(deliveryLanded(fetchedIntoTheGap, d), false);
  assert.equal(deliveryLanded([...fetchedIntoTheGap, theReport], d), true);
});

test("falls back to the task id when the kernel sent no row id", () => {
  const d = { rowId: null, taskId: "126" };
  assert.equal(deliveryLanded(fetchedIntoTheGap, d), false);
  assert.equal(deliveryLanded([...fetchedIntoTheGap, theReport], d), true);
});

test("the row id wins over the task id, and a different task never matches", () => {
  assert.equal(
    deliveryLanded([theReport], { rowId: 999, taskId: "126" }),
    false,
    "rowId is authoritative — a stale task match must not satisfy it",
  );
  assert.equal(deliveryLanded([theReport], { rowId: null, taskId: "127" }), false);
});

test("numeric row ids match the string ids the pane keys rows by", () => {
  // chat-pane maps server rows with `id: String(t.id)`; the frame carries an int.
  assert.equal(deliveryLanded([theReport], { rowId: 572, taskId: null }), true);
});

test("an unnamed delivery (socket reopen) names no row, so callers fall back", () => {
  const reopen = { rowId: null, taskId: null };
  assert.equal(deliveryIsNamed(reopen), false);
  assert.equal(deliveryLanded([theReport], reopen), false);
  assert.equal(deliveryIsNamed({ rowId: 572, taskId: null }), true);
  assert.equal(deliveryIsNamed({ rowId: null, taskId: "126" }), true);
});

test("an empty thread never reports a delivery as landed", () => {
  assert.equal(deliveryLanded([], { rowId: 572, taskId: "126" }), false);
});

// --- the LOOP's decision, not just its helper -------------------------------
// Review: the regression test above pins a copy of the old rule inside
// this file, so reverting the caller to `if (grew) return` would still pass it.
// evaluateDeliveryFetch IS what the loop calls, so these pin the caller.

test("LOOP: a fetch into the write gap adopts the window but is NOT done", () => {
  const { adopt, done } = evaluateDeliveryFetch(onScreenAtTheFlip, fetchedIntoTheGap, {
    rowId: 572,
    taskId: "126",
  });
  assert.equal(adopt, true, "the user's own rows landed — adopt them");
  assert.equal(done, false, "but the report is not here yet — keep waiting");
});

test("LOOP: the fetch that carries the report is done", () => {
  const { adopt, done } = evaluateDeliveryFetch(
    onScreenAtTheFlip,
    [...fetchedIntoTheGap, theReport],
    { rowId: 572, taskId: "126" },
  );
  assert.equal(adopt, true);
  assert.equal(done, true);
});

test("LOOP: an unnamed delivery keeps the old 'it grew' behaviour", () => {
  const unnamed = { rowId: null, taskId: null };
  assert.equal(evaluateDeliveryFetch(onScreenAtTheFlip, fetchedIntoTheGap, unnamed).done, true);
  assert.equal(evaluateDeliveryFetch(onScreenAtTheFlip, onScreenAtTheFlip, unnamed).done, false);
});

test("LOOP: a window with nothing new is neither adopted nor done", () => {
  const { adopt, done } = evaluateDeliveryFetch(
    [...fetchedIntoTheGap],
    [...fetchedIntoTheGap],
    { rowId: 572, taskId: "126" },
  );
  assert.equal(adopt, false);
  assert.equal(done, false);
});

// --- routing: the "could not be shown" notice must never be a lie -----------

test("ROUTING: a named delivery only polls the thread its row went into", () => {
  const wb = (projectId: number | null) => ({
    rowId: 572,
    taskId: "126",
    projectId,
    appId: "workbench" as string | null,
  });
  assert.equal(deliveryTargetsThread(wb(1), 1), true);
  assert.equal(deliveryTargetsThread(wb(1), 2), false);
  assert.equal(deliveryTargetsThread(wb(1), null), false, "project row vs General");
  assert.equal(deliveryTargetsThread(wb(null), null), true, "General vs General");
  assert.equal(
    deliveryTargetsThread(wb(null), 1),
    false,
    "a General/Circles-shaped delivery must not false-alarm on a project thread",
  );
});

test("ROUTING: a Circles-origin delivery is never the Workbench's to wait for", () => {
  const circles = { rowId: 572, taskId: "126", projectId: null, appId: "circles" };
  assert.equal(deliveryTargetsThread(circles, null), false);
  assert.equal(deliveryTargetsThread(circles, 1), false);
});

test("ROUTING: the bench path carries no app id and is treated as ours", () => {
  const bench = { rowId: null, taskId: "126", projectId: 1, appId: null };
  assert.equal(deliveryTargetsThread(bench, 1), true);
  assert.equal(deliveryTargetsThread(bench, 2), false);
});

test("ROUTING: an unnamed delivery is valid against whatever is on screen", () => {
  const reopen = { rowId: null, taskId: null, projectId: null, appId: null };
  assert.equal(deliveryTargetsThread(reopen, 1), true);
  assert.equal(deliveryTargetsThread(reopen, null), true);
});

// Second review: the bench sends appId null for a DISPATCHED agent,
// whose scope lives only in result.origin. Before origin_app_id existed, such a
// row was {projectId: null, appId: null} and matched the General thread — so it
// polled General for 60s for a row sitting in Circles or in a project, then
// announced the report "could not be shown here". Unattributable is not ours.
test("ROUTING: a named delivery with no origin at all never claims a thread", () => {
  const orphan = { rowId: null, taskId: "126", projectId: null, appId: null };
  assert.equal(deliveryTargetsThread(orphan, null), false, "General must not claim it");
  assert.equal(deliveryTargetsThread(orphan, 1), false);
});

test("ROUTING: a bench row that DOES carry its origin routes on it", () => {
  // origin_project_id flows into projectId; origin_app_id into appId.
  const agentInProject = { rowId: null, taskId: "126", projectId: 7, appId: "workbench" };
  assert.equal(deliveryTargetsThread(agentInProject, 7), true);
  assert.equal(deliveryTargetsThread(agentInProject, 1), false);
  const agentInGeneral = { rowId: null, taskId: "126", projectId: null, appId: "workbench" };
  assert.equal(deliveryTargetsThread(agentInGeneral, null), true, "General dispatch, General open");
  assert.equal(deliveryTargetsThread(agentInGeneral, 7), false);
});

test("ROUTING: a build with a project but no origin app is still ours", () => {
  // Pre-2026-09-05 factory_build rows: project_id stamped, no origin_app_id.
  const build = { rowId: null, taskId: "126", projectId: 1, appId: null };
  assert.equal(deliveryTargetsThread(build, 1), true, "the incident shape");
  assert.equal(deliveryTargetsThread(build, 2), false);
  assert.equal(deliveryTargetsThread(build, null), false);
});
