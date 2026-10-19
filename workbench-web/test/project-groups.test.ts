import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyPlace,
  countsOf,
  groupOf,
  groupOfProject,
  sortInBucket,
  suggestTitle,
} from "../src/lib/project-groups.ts";
import type { Bucket, Project } from "../src/lib/types.ts";

const p = (id: number, status: string, extra: Partial<Project> = {}): Project => ({
  id,
  name: `p${id}`,
  status,
  ...extra,
});

test("groupOf mirrors the server mapping: review folds into active, planning is pending", () => {
  assert.equal(groupOf("active"), "active");
  assert.equal(groupOf("review"), "active");
  assert.equal(groupOf("planning"), "pending");
  assert.equal(groupOf("complete"), "completed");
  assert.equal(groupOf("archived"), "archived");
  assert.equal(groupOf(undefined), "pending");
});

test("a server-stamped group wins over the status mapping", () => {
  assert.equal(groupOfProject(p(1, "active", { group: "archived" })), "archived");
});

test("counts add up across the four statuses", () => {
  const list = [p(5, "active"), p(4, "planning"), p(3, "review"), p(2, "complete"), p(1, "archived")];
  assert.deepEqual(countsOf(list), { active: 2, pending: 1, completed: 1, archived: 1, total: 5 });
});

const B: Bucket[] = [
  { id: 2, name: "Clients", color: null, sort_order: 1 },
  { id: 1, name: "Home", color: null, sort_order: 0 },
];

test("sortInBucket: manual puts placed projects first by position, the rest keep the server's recent order", () => {
  // server order = recent activity DESC
  const list = [p(9, "active"), p(8, "active", { sort_order: 2 }), p(7, "active"), p(6, "active", { sort_order: 1 })];
  assert.deepEqual(sortInBucket(list, "manual").map((x) => x.id), [6, 8, 9, 7]);
  assert.deepEqual(sortInBucket(list, "recent").map((x) => x.id), [9, 8, 7, 6]);
});

test("applyPlace: a drop in front of a row renumbers the target bucket 1..n, the mover in its slot", () => {
  const list = [p(1, "active", { bucket_id: 1 }), p(2, "active", { bucket_id: 1 }), p(3, "active", { bucket_id: 1 }), p(4, "active", { bucket_id: 2 })];
  const out = applyPlace(list, B, 4, 1, 2);
  const home = sortInBucket(out.filter((x) => x.bucket_id === 1), "manual").map((x) => x.id);
  assert.deepEqual(home, [1, 4, 2, 3]);
  assert.deepEqual(out.filter((x) => x.bucket_id === 1).map((x) => [x.id, x.sort_order]).sort((a, b) => Number(a[0]) - Number(b[0])), [[1, 1], [2, 3], [3, 4], [4, 2]]);
  // the source bucket is untouched
  assert.equal(out.find((x) => x.id === 4)?.bucket_id, 1);
});

test("applyPlace: no target row = last; null bucket = unfiled; reorder within one bucket", () => {
  const list = [p(1, "active", { bucket_id: 1, sort_order: 1 }), p(2, "active", { bucket_id: 1, sort_order: 2 }), p(3, "active")];
  const toEnd = applyPlace(list, B, 1, 1, null);
  assert.deepEqual(sortInBucket(toEnd.filter((x) => x.bucket_id === 1), "manual").map((x) => x.id), [2, 1]);
  const unfile = applyPlace(list, B, 2, null, 3);
  assert.equal(unfile.find((x) => x.id === 2)?.bucket_id, null);
  assert.deepEqual(sortInBucket(unfile.filter((x) => x.bucket_id == null), "manual").map((x) => x.id), [2, 3]);
});

test("suggestTitle: first clause, leading intent phrase dropped, capped, capitalised", () => {
  assert.equal(suggestTitle("I want to build a budget tracker for my family. It should have envelopes."), "Budget tracker for my family");
  // 8 words would be 62 chars; the 60-char cap drops the last whole word.
  assert.equal(suggestTitle("help me set up a podcast pipeline that transcribes episodes and publishes notes"), "Podcast pipeline that transcribes episodes and publishes");
  assert.equal(suggestTitle("Trading software — backtesting for equities"), "Trading software");
  assert.equal(suggestTitle("   "), "");
  assert.ok(suggestTitle("x".repeat(200)).length <= 60);
});
