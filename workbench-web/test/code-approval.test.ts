import { test } from "node:test";
import assert from "node:assert/strict";
import {
  addApprovalLine,
  parseApprovalBody,
  repoView,
  splitLinks,
  withApprovalLine,
  withoutApprovalLine,
} from "../src/lib/code-approval.ts";

// The body the Host writes (server/routes/code_approvals.py build_body).
const BODY = [
  "<!-- code-approval v1 -->",
  "**What this changes**",
  "- Add a login page",
  "",
  "**Why**",
  "You asked: “a login page”",
  "[Open the project](https://host-a.example.test/?open=workbench&view=project:12)",
  "",
  "**What was checked**",
  "- The build contract found problems, so this is not ready to approve:",
  "  - index.html is missing",
  "- harness-check: passed (3 checked)",
  "",
  "**Who wrote it**",
  "- Add a login page: Claude Code on claude-opus-5.",
  "",
  "<details>",
  "<summary>Details</summary>",
  "",
  "- 3 files changed (+40 −2)",
  "- Latest save point: phase 1: add a login page",
  "",
  "</details>",
  "<!-- models: claude-opus-5 -->",
  "<!-- build-contract: violations -->",
].join("\n");

test("the fixed format reads as sections, details folded, markers gone", () => {
  const p = parseApprovalBody(BODY);
  assert.equal(p.plain, true);
  assert.deepEqual(p.sections.map((s) => s.heading), [
    "What this changes", "Why", "What was checked", "Who wrote it",
  ]);
  assert.deepEqual(p.sections[0].lines, ["Add a login page"]);
  assert.deepEqual(p.sections[2].lines, [
    "The build contract found problems, so this is not ready to approve:",
    "  • index.html is missing",
    "harness-check: passed (3 checked)",
  ]);
  assert.deepEqual(p.details, ["3 files changed (+40 −2)", "Latest save point: phase 1: add a login page"]);
  const all = JSON.stringify(p);
  assert.ok(!all.includes("<!--"), "no hidden marker reaches the screen");
  assert.ok(!all.includes("<details>") && !all.includes("<summary>"));
});

test("a body not in the format is shown as the text it is", () => {
  const p = parseApprovalBody("Fixes the thing.\n\n<script>alert(1)</script>");
  assert.equal(p.plain, false);
  assert.equal(p.sections.length, 0);
  assert.equal(p.text, "Fixes the thing.\n\n<script>alert(1)</script>");
  assert.equal(parseApprovalBody(null).text, "");
});

test("only https links become links", () => {
  assert.deepEqual(splitLinks("see [the project](https://x.example.test/?open=workbench) now"), [
    { text: "see " },
    { text: "the project", href: "https://x.example.test/?open=workbench" },
    { text: " now" },
  ]);
  for (const bad of ["[x](javascript:alert(1))", "[x](http://plain.example)", "[x](data:text/html,hi)"]) {
    assert.ok(splitLinks(bad).every((p) => !p.href), bad);
  }
});

test("a Code Repository opens in the Gitea app's repo view", () => {
  assert.equal(repoView("entity-1/shop-p12"), "repo:entity-1/shop-p12");
});


// The chat's merge-line wait (App's one ApprovalLineEvent).
test("a merge that lands while a wait runs joins it, so the first keeps its wait", () => {
  let n = 0;
  const seq = () => ++n;
  const a = addApprovalLine(null, 7, 11, true, seq);
  const b = addApprovalLine(a, 7, 12, true, seq);
  assert.deepEqual(b?.lines, [{ number: 11, written: true }, { number: 12, written: true }]);
  assert.equal(b?.seq, 2);
});

test("a held line joins quietly, and never downgrades a written one", () => {
  let n = 0;
  const seq = () => ++n;
  const a = addApprovalLine(null, 7, 11, true, seq);
  assert.equal(addApprovalLine(a, 7, 11, false, seq), a, "held after written: nothing new");
  const b = addApprovalLine(a, 7, 12, false, seq);
  assert.deepEqual(b?.lines, [{ number: 11, written: true }, { number: 12, written: false }]);
  const c = addApprovalLine(b, 7, 12, true, seq);
  assert.deepEqual(c?.lines, [{ number: 11, written: true }, { number: 12, written: true }]);
});

test("the same written line twice is nothing new; another project starts its own wait", () => {
  let n = 0;
  const seq = () => ++n;
  const a = addApprovalLine(null, 7, 11, true, seq);
  assert.equal(addApprovalLine(a, 7, 11, true, seq), a);
  assert.equal(n, 1, "no seq spent on nothing new");
  const other = addApprovalLine(a, 8, 3, true, seq);
  assert.deepEqual(other, { seq: 2, projectId: 8, lines: [{ number: 3, written: true }] });
});


test("a late answer for the project just left never touches the wait on screen", () => {
  let n = 0;
  const seq = () => ++n;
  let waits = withApprovalLine({}, 7, 11, true, seq); // P = 7, left behind
  waits = withApprovalLine(waits, 9, 4, true, seq); // Q = 9, on screen now
  const q = waits[9];
  const after = withApprovalLine(waits, 7, 12, true, seq); // P's late announce lands
  assert.equal(after[9], q, "Q's wait is the same object, so its loop is not restarted");
  assert.deepEqual(after[7]?.lines, [{ number: 11, written: true }, { number: 12, written: true }]);
  assert.equal(withApprovalLine(after, 9, 4, true, seq), after, "nothing new: the same map");
});

test("a finished wait drops only its own project's entry; an old seq drops nothing", () => {
  let n = 0;
  const seq = () => ++n;
  let waits = withApprovalLine({}, 7, 11, true, seq); // seq 1
  waits = withApprovalLine(waits, 7, 12, true, seq); // P's wait joined: seq 2
  waits = withApprovalLine(waits, 9, 4, true, seq); // Q: seq 3
  assert.equal(withoutApprovalLine(waits, 1), waits, "the joined wait's old seq matches nothing");
  const done = withoutApprovalLine(waits, 2);
  assert.deepEqual(Object.keys(done), ["9"]);
  assert.equal(done[9], waits[9]);
});
