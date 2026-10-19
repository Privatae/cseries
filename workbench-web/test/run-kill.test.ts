import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  emptyFold,
  foldFrame,
  frameToLines,
  summaryLine,
  type RunFrame,
} from "../src/lib/run-frames.ts";

const f = (seq: number, kind: string, payload: Record<string, unknown>): RunFrame => ({
  seq,
  kind,
  payload,
});

const fold = (frames: RunFrame[]) => frames.reduce((st, fr) => foldFrame(st, fr), emptyFold());

test("a killed run reads 'stopped by you', never 'failed'", () => {
  const st = fold([
    f(0, "text", { text: "working" }),
    f(1, "status", { state: "killed", by: "user", detail: "stopped by you" }),
    f(2, "status", { state: "complete", ok: false, exit_code: -9, stderr: "Killed" }),
  ]);
  assert.equal(st.summary.state, "failed");
  assert.equal(st.summary.killedBy, "user");
  assert.match(summaryLine(st.summary), /^stopped by you/);
  assert.doesNotMatch(summaryLine(st.summary), /failed/);
});

test("the terminal frame's killed_by alone is enough (the frame-less kill path)", () => {
  const st = fold([
    f(0, "status", { state: "complete", ok: false, exit_code: null, killed_by: "host" }),
  ]);
  assert.equal(st.summary.killedBy, "host");
  assert.match(summaryLine(st.summary), /^stopped by your Host/);
});

test("a plain failure is still a failure", () => {
  const st = fold([f(0, "status", { state: "complete", ok: false, exit_code: 1 })]);
  assert.equal(st.summary.killedBy, null);
  assert.match(summaryLine(st.summary), /^failed/);
});

test("the killed frame shows in the scrollback and does not close the run", () => {
  const lines = frameToLines(f(3, "status", { state: "killed", by: "user", detail: "stopped by you" }));
  assert.equal(lines.length, 1);
  assert.match(lines[0].text, /killed — stopped by you/);
  const st = fold([f(0, "status", { state: "killed", by: "user" })]);
  assert.equal(st.summary.state, "live");
});

test("the run terminal offers Kill only while the run is on a pod, and posts to the kill route", () => {
  const term = readFileSync(new URL("../src/components/workbench/run-terminal.tsx", import.meta.url), "utf8");
  assert.ok(term.includes("{live && !waiting && ("), "kill shows only while live and not queued");
  assert.ok(term.includes("data-kill-run"));
  assert.ok(term.includes("await killRun(sessionId, harness)"));
  assert.ok(term.includes("stops with it"), "the button says other runs on the pod stop too");
  assert.ok(!term.includes("window.confirm("), "a stop never asks first");
  // The halt lands before the stop: every note — killed, failed, already
  // ended — says when the plan stopped advancing.
  assert.ok(term.includes("the plan has stopped advancing until it is built again"));
  assert.equal(term.split("${halted}").length - 1, 3, "all three notes carry the halt");
  // The note counts every run that stopped besides this one — other
  // sessions' runs on the pod AND this compare's other variants on the same
  // engine — not only other sessions'.
  assert.ok(
    term.includes("d.stopped.filter((x) => x.other_session || x.label !== harness)"),
    "the note counts this compare's other variants too",
  );
  const api = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
  assert.ok(api.includes("`/api/review/sessions/${encodeURIComponent(sessionId)}/kill`"));
});
