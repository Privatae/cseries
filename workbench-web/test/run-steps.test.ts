// run-steps.ts — the run card's step model. Frame shapes are the real ones
// the harness runtime emits (system-apps/_harness_runtime/events.py): a
// Claude Code build, a Codex build, and a compare (antigravity, codex, grok,
// kimi).
import assert from "node:assert/strict";
import test from "node:test";
import {
  activityNote,
  cleanCmd,
  engineName,
  foldSteps,
  groupSteps,
  groupTitle,
  hostBrief,
  nowLine,
  parsePatch,
  tokensLine,
  toolLabel,
  type ToolStep,
} from "../src/lib/run-steps.ts";
import type { RunFrame } from "../src/lib/run-frames.ts";

let seq = 0;
const T0 = 1_790_000_000;
const f = (t: number, kind: string, payload: Record<string, unknown>): RunFrame => ({ seq: seq++, ts: T0 + t, kind, payload });
const reset = () => { seq = 0; };

test("Claude Code thinking heartbeats are ONE timed thinking step, not one row each", () => {
  reset();
  const M = foldSteps([
    f(0, "status", { state: "thinking", detail: "extended thinking — 1 heartbeats" }),
    f(25, "status", { state: "thinking", detail: "extended thinking — 21 heartbeats" }),
    f(50, "status", { state: "thinking", detail: "extended thinking — 41 heartbeats" }),
    f(98, "thinking", { text: "I'll build a rules-based editor." }),
    f(110, "tool_call", { tool: "Bash", args: { command: "ls" } }),
  ]);
  const thinks = M.steps.filter((s) => s.type === "thinking" || s.type === "think");
  assert.equal(thinks.length, 1);
  const t = thinks[0] as Extract<(typeof M.steps)[number], { type: "think" }>;
  assert.equal(t.t0, T0);
  assert.equal(t.t1, T0 + 110, "closed by the next non-thinking frame");
  assert.equal(t.text, "I'll build a rules-based editor.");
  assert.equal(M.think, null, "no open thinking span once a tool call arrived");
});

test("an empty thinking block under 3s is not a row, and does not split a burst", () => {
  reset();
  const M = foldSteps([
    f(0, "tool_call", { tool: "Bash", args: { command: "ls" } }),
    f(1, "tool_result", { tool: "", status: "ok", result: "a\nb" }),
    f(1.2, "status", { state: "thinking", detail: "thinking block (text not returned by the model)" }),
    f(2, "tool_call", { tool: "Bash", args: { command: "cat a" } }),
    f(2.5, "tool_result", { tool: "", status: "ok", result: "x" }),
    f(3, "tool_call", { tool: "Read", args: { file_path: "/var/lib/cseries/projects/factory-build-416-claude-code/index.html", limit: 240 } }),
    f(3.1, "tool_result", { tool: "", status: "ok", result: "1\t<!doctype html>" }),
  ]);
  const items = groupSteps(M.steps);
  assert.equal(items.length, 1, "three calls with a quiet think between them fold into one group");
  assert.equal(items[0].type, "group");
  assert.equal(groupTitle((items[0] as { steps: ToolStep[] }).steps), "Read 1 file, ran 2 commands");
});

test("a long or worded thinking block stays a row and does split a burst", () => {
  reset();
  const M = foldSteps([
    f(0, "tool_call", { tool: "Bash", args: { command: "a" } }),
    f(0.5, "tool_result", { tool: "", status: "ok", result: "" }),
    f(1, "tool_call", { tool: "Bash", args: { command: "b" } }),
    f(1.5, "tool_result", { tool: "", status: "ok", result: "" }),
    f(2, "status", { state: "thinking", detail: "x" }),
    f(40, "tool_call", { tool: "Bash", args: { command: "c" } }),
  ]);
  assert.deepEqual(groupSteps(M.steps).map((i) => i.type), ["tool", "tool", "think", "tool"]);
});

test("two calls in a row are rows; three are a group; a file write ends the group", () => {
  reset();
  const M = foldSteps([
    f(0, "tool_call", { tool: "shell", args: { command: "x" }, id: "i1" }),
    f(1, "tool_call", { tool: "shell", args: { command: "y" }, id: "i2" }),
    f(2, "tool_call", { tool: "shell", args: { command: "z" }, id: "i3" }),
    f(3, "file_edit", { path: "/var/lib/cseries/projects/codex/app.yaml", change: "update", id: "i4" }),
    f(4, "tool_call", { tool: "shell", args: { command: "w" }, id: "i5" }),
  ]);
  assert.deepEqual(groupSteps(M.steps).map((i) => i.type), ["group", "edit", "tool"]);
});

test("results pair by id (codex), by tool name (kimi's parallel calls), else oldest first (claude-code)", () => {
  reset();
  const codex = foldSteps([
    f(0, "tool_call", { tool: "shell", args: { command: "a" }, id: "item_1" }),
    f(1, "tool_call", { tool: "shell", args: { command: "b" }, id: "item_2" }),
    f(2, "tool_result", { tool: "shell", status: "error", result: "nope", exit_code: 1, id: "item_2" }),
  ]);
  const [a, b] = codex.steps as ToolStep[];
  assert.equal(a.status, "run");
  assert.equal(b.status, "err");
  assert.equal(b.exit, 1);
  assert.equal(codex.errors, 1);

  reset();
  const kimi = foldSteps([
    f(0, "tool_call", { tool: "Read", args: { path: "index.html" } }),
    f(0, "tool_call", { tool: "Grep", args: { pattern: "theme", path: "app.js" } }),
    f(1, "tool_result", { tool: "Grep", status: "ok", result: "12: theme" }),
    f(1, "tool_result", { tool: "Read", status: "ok", result: "<!DOCTYPE html>" }),
  ]);
  const [r, g] = kimi.steps as ToolStep[];
  assert.equal(r.result, "<!DOCTYPE html>");
  assert.equal(g.result, "12: theme");

  reset();
  const cc = foldSteps([
    f(0, "tool_call", { tool: "Bash", args: { command: "one" } }),
    f(1, "tool_call", { tool: "Bash", args: { command: "two" } }),
    f(2, "tool_result", { tool: "", status: "ok", result: "first" }),
  ]);
  assert.equal((cc.steps[0] as ToolStep).result, "first");
  assert.equal((cc.steps[1] as ToolStep).status, "run");
});

test("antigravity's write_to_file call and its file_edit are ONE row", () => {
  reset();
  const M = foldSteps([
    f(0, "tool_call", { tool: "write_to_file", args: { TargetFile: "/var/lib/cseries/projects/antigravity/index.html" } }),
    f(1, "tool_result", { tool: "write_to_file", status: "ok", result: "" }),
    f(1, "file_edit", { path: "/var/lib/cseries/projects/antigravity/index.html", change: "write" }),
  ]);
  assert.equal(M.steps.length, 1);
  const s = M.steps[0] as ToolStep;
  assert.equal(s.change, "write");
  assert.equal(toolLabel(s, false).verb, "Wrote");
  assert.deepEqual([...M.written.keys()], ["/var/lib/cseries/projects/antigravity/index.html"]);
});

test("Claude Code's Write arrives as a file_edit and waits for its result", () => {
  reset();
  const M = foldSteps([
    f(0, "file_edit", { path: "/var/lib/cseries/projects/x/server/editor.py", tool: "Write" }),
  ]);
  assert.equal(M.steps[0].type, "edit");
  assert.equal(nowLine(M, T0 + 2)?.kind, "step");
  reset();
  const done = foldSteps([
    f(0, "file_edit", { path: "/var/lib/cseries/projects/x/server/editor.py", tool: "Write" }),
    f(0.2, "tool_result", { tool: "", status: "ok", result: "File created successfully" }),
  ]);
  assert.equal((done.steps[0] as { status: string }).status, "ok");
});

test("waiting for memory: the run has not started, and the reason is the kernel's own", () => {
  reset();
  const note = "Waiting for memory: grok needs 512Mi and this plan has 32Mi free while codex, kimi and antigravity run. It starts as memory frees.";
  const waiting = foldSteps([f(0, "status", { state: "waiting", detail: note })]);
  assert.equal(waiting.start, null);
  assert.equal(waiting.waiting, note);
  assert.equal(waiting.queuedAt, T0);
  assert.deepEqual(nowLine(waiting, T0 + 60), { kind: "waiting", detail: note, since: T0 });
  reset();
  const started = foldSteps([
    f(0, "status", { state: "waiting", detail: note }),
    f(200, "thinking", { text: "The", delta: true }),
  ]);
  assert.equal(started.waiting, null);
  assert.equal(started.start, T0 + 200, "the run starts at its own first frame, not the wait");
});

test("grok's token deltas join into one message and one thinking block", () => {
  reset();
  const M = foldSteps([
    f(0, "thinking", { text: "The", delta: true }),
    f(0.1, "thinking", { text: " user", delta: true }),
    f(1, "text", { text: "I'll start", delta: true }),
    f(1.1, "text", { text: " by reading.", delta: true }),
    f(1.2, "usage", { input_tokens: 1 }),
    f(1.3, "text", { text: " Then edit.", delta: true }),
  ]);
  assert.deepEqual(M.steps.map((s) => s.type), ["think", "say"]);
  assert.equal((M.steps[1] as { text: string }).text, "I'll start by reading. Then edit.", "a usage frame does not split prose");
  assert.equal(M.msgs, 1);
});

test("whole messages are never glued together", () => {
  reset();
  const M = foldSteps([
    f(0, "text", { text: "First message." }),
    f(1, "text", { text: "Second message." }),
  ]);
  assert.equal(M.steps.length, 2);
});

test("the terminal frame lands the run; calls with no result read as unknown, not running", () => {
  reset();
  const M = foldSteps([
    f(0, "tool_call", { tool: "shell", args: { command: "python -m http.server 8765" }, id: "item_9" }),
    f(10, "status", { state: "complete", ok: true, exit_code: 0, elapsed_seconds: 642.0, model: "gpt-6-astra" }),
    f(11, "status", { state: "acceptance", ran: true, ok: false, checks: 1, failed: ["test -f src/pages/index.html"] }),
  ]);
  assert.equal(M.state, "complete");
  assert.equal(M.live, false);
  assert.equal(M.elapsed, 642);
  assert.equal(M.model, "gpt-6-astra");
  assert.equal((M.steps[0] as ToolStep).status, "unknown");
  assert.deepEqual(M.accept, { ran: true, ok: false, checks: 1, failed: ["test -f src/pages/index.html"] });
  assert.equal(nowLine(M, T0 + 20), null);
});

test("a failed run carries the CLI's own failure line", () => {
  reset();
  const M = foldSteps([f(0, "status", { state: "complete", ok: false, exit_code: 1, stderr: "warming up\nError: rate limit exceeded\n" })]);
  assert.equal(M.state, "failed");
  assert.equal(M.failure, "Error: rate limit exceeded");
});

test("lifecycle frames are not rows; an unknown status is a note; a redelivered seq folds once", () => {
  const frames: RunFrame[] = [
    { seq: 0, ts: T0, kind: "status", payload: { state: "thread.started", detail: "01a0" } },
    { seq: 1, ts: T0 + 1, kind: "status", payload: { state: "checkpoint", detail: "1.4s" } },
    { seq: 2, ts: T0 + 2, kind: "status", payload: { state: "compacting", detail: "context 80%" } },
    { seq: 2, ts: T0 + 2, kind: "status", payload: { state: "compacting", detail: "context 80%" } },
  ];
  const M = foldSteps(frames);
  assert.equal(M.steps.length, 1);
  assert.deepEqual(M.steps[0], { type: "note", id: "s2", t0: T0 + 2, text: "compacting — context 80%" });
});

test("antigravity's closing report rides on its done frame", () => {
  reset();
  const report = "# LCSW Clinical Exam Prep — Botanical Sanctuary UI/UX Redesign\n\nThe application has been redesigned into a calm study sanctuary.";
  const M = foldSteps([f(0, "status", { state: "done", detail: report })]);
  assert.equal(M.steps[0].type, "report");
});

test("cleanCmd strips the bash -lc wrapper, nested and with mixed quotes", () => {
  assert.equal(cleanCmd(`/usr/bin/bash -lc 'python -m http.server 8765'`), "python -m http.server 8765");
  assert.equal(cleanCmd(`/usr/bin/bash -lc "pwd && rg --files -g '"'!*node_modules*'"'"`), "pwd && rg --files -g '!*node_modules*'");
  assert.equal(
    cleanCmd(`/usr/bin/bash -lc "python - <<'PY'\nprint(1)\nPY\ncat > R.md <<'EOF'\nx\nEOF'`).split("\n")[0],
    "python - <<'PY'",
  );
  assert.equal(cleanCmd("ls -la"), "ls -la");
});

test("labels: a multi-line script says how long it is; a read names its lines", () => {
  reset();
  const M = foldSteps([
    f(0, "tool_call", { tool: "Bash", args: { command: "python3 - <<'EOF'\nimport re\nprint(re)\nEOF" } }),
    f(1, "tool_call", { tool: "Read", args: { file_path: "/var/lib/cseries/projects/p/index.html", offset: 240, limit: 520 } }),
  ]);
  const [cmd, read] = M.steps as ToolStep[];
  assert.deepEqual(toolLabel(cmd, false), { verb: "Ran", subject: "python3 - <<'EOF'", kind: "code", extra: "+3 lines", cmd: "python3 - <<'EOF'\nimport re\nprint(re)\nEOF" });
  assert.deepEqual(toolLabel(read, true), { verb: "Reading", subject: "/var/lib/cseries/projects/p/index.html", kind: "path", extra: "lines 240–759" });
});

test("the now line: thinking, a running call, a quiet spell", () => {
  reset();
  const thinking = foldSteps([f(0, "status", { state: "thinking", detail: "x" })]);
  assert.deepEqual(nowLine(thinking, T0 + 5), { kind: "thinking", since: T0 });
  reset();
  const quiet = foldSteps([
    f(0, "tool_call", { tool: "Bash", args: { command: "x" } }),
    f(1, "tool_result", { tool: "", status: "ok", result: "" }),
  ]);
  assert.deepEqual(nowLine(quiet, T0 + 40), { kind: "working", lastAct: T0 + 1 });
});

test("tokens: the terminal total, or one usage frame — never a guessed sum", () => {
  reset();
  assert.equal(tokensLine(foldSteps([f(0, "status", { state: "complete", ok: true, tokens_total: 110008 })])), "110k tokens");
  reset();
  assert.equal(tokensLine(foldSteps([f(0, "usage", { input_tokens: 752461, output_tokens: 17649 })])), "752k in · 18k out");
  reset();
  assert.equal(tokensLine(foldSteps([f(0, "usage", { input_tokens: 1, output_tokens: 1 }), f(1, "usage", { input_tokens: 2, output_tokens: 2 })])), null);
});

test("parsePatch: per-file counts and status off diffs/<harness>.patch", () => {
  const patch = [
    "diff --git a/app.yaml b/app.yaml",
    "index 5b01aec..bee3d8c 100644",
    "--- a/app.yaml",
    "+++ b/app.yaml",
    "@@ -2,7 +2,7 @@ id: lcsw-exam-prep",
    " name: LCSW Clinical Exam Prep",
    "-version: 0.4.0",
    "+version: 0.5.0",
    "diff --git a/scripts/verify_redesign.py b/scripts/verify_redesign.py",
    "new file mode 100644",
    "index 0000000..1a2b3c4",
    "--- /dev/null",
    "+++ b/scripts/verify_redesign.py",
    "@@ -0,0 +1,2 @@",
    "+import sys",
    "+print(sys.argv)",
  ].join("\n");
  const files = parsePatch(patch);
  assert.deepEqual(files.map(({ path, add, del, status }) => ({ path, add, del, status })), [
    { path: "app.yaml", add: 1, del: 1, status: "modified" },
    { path: "scripts/verify_redesign.py", add: 2, del: 0, status: "added" },
  ]);
  assert.equal(files[0].lines[0], "@@ -2,7 +2,7 @@ id: lcsw-exam-prep");
});

test("engine names, including a Self-MoA sample", () => {
  assert.equal(engineName("kimi"), "Kimi Code");
  assert.equal(engineName("codex-s2"), "Codex · sample 2");
  assert.equal(engineName("some-new-engine"), "some-new-engine");
});

test("a killed run reads as stopped by you or your Host, never as a failure", () => {
  reset();
  const M = foldSteps([
    f(0, "text", { text: "working" }),
    f(1, "status", { state: "killed", by: "user", detail: "stopped by you" }),
    f(2, "status", { state: "complete", ok: false, exit_code: -9, stderr: "Killed" }),
  ]);
  assert.equal(M.state, "failed");
  assert.equal(M.killedBy, "user");
  assert.equal(M.steps[1].type, "killed");
  reset();
  const host = foldSteps([f(0, "status", { state: "complete", ok: false, exit_code: null, killed_by: "host" })]);
  assert.equal(host.killedBy, "host", "the terminal frame's killed_by alone is enough");
  reset();
  const live = foldSteps([f(0, "status", { state: "killed", by: "user" })]);
  assert.equal(live.live, true, "the killed frame does not close the run");
});

test("grok's mid-step tool updates and cursor's repeated whole message are lifecycle, not rows", () => {
  reset();
  // The frames events.py emits for them (parse_grok tool_call_update with
  // status null / in_progress; parse_cursor's whole-message frame).
  const M = foldSteps([
    f(0, "tool_call", { tool: "read_file", args: { path: "a.txt" }, id: "c1" }),
    f(1, "status", { state: "tool_update", detail: "pending", id: "c1" }),
    f(2, "status", { state: "tool_update", detail: "in_progress", id: "c1" }),
    f(3, "tool_result", { tool: "read_file", status: "ok", result: "x", id: "c1" }),
    f(4, "text", { text: "Built ", delta: true }),
    f(5, "text", { text: "it.", delta: true }),
    f(6, "status", { state: "message_end", detail: "9 chars" }),
  ]);
  assert.equal(M.steps.filter((s) => s.type === "note").length, 0, JSON.stringify(M.steps));
  const says = M.steps.filter((s) => s.type === "say");
  assert.equal(says.length, 1);
  assert.equal((says[0] as { text: string }).text, "Built it.");
  const tool = M.steps.find((s) => s.type === "tool") as ToolStep;
  assert.equal(tool.status, "ok");
});

// The pod sent a thinking status
// frame every ~24 s for five minutes and the card's stats line read "No
// activity yet" the whole time. A live thinking stretch is activity.
test("a live thinking-only stretch reads as Thinking with its clock and heartbeats, never 'No activity yet'", () => {
  reset();
  const M = foldSteps([
    f(0, "status", { state: "thinking", detail: "extended thinking — 1 heartbeats" }),
    f(24.4, "status", { state: "thinking", detail: "extended thinking — 21 heartbeats" }),
    f(289.4, "status", { state: "thinking", detail: "extended thinking — 241 heartbeats" }),
  ]);
  assert.equal(M.live, true);
  assert.ok(M.think, "the thinking span is open");
  assert.equal(M.think?.beats, 241, "the latest heartbeat count is kept");
  assert.equal(activityNote(M, T0 + 300), "Thinking 5:00 · 241 heartbeats");
  const n = nowLine(M, T0 + 300);
  assert.deepEqual(n, { kind: "thinking", since: T0, beats: 241 });
});

test("before the first frame the stats line still says No activity yet; after a finished think it says how long", () => {
  reset();
  assert.equal(activityNote(foldSteps([]), T0), "No activity yet");
  reset();
  const done = foldSteps([
    f(0, "status", { state: "thinking", detail: "extended thinking — 1 heartbeats" }),
    f(333, "status", { state: "complete", ok: true }),
  ]);
  assert.equal(activityNote(done, T0 + 400), "Thought for 5m 33s");
});

// review.py _record_sent_prompt writes sent_prompt as
// {<run label>: <string>}: a fan-out sends each variant its own prompt.
test("the Host brief shows THIS run's sent_prompt entry, and never an empty brief", () => {
  const sent = { "claude-code": "contract + task", "claude-code-s2": "contract2 + task", codex: "" };
  assert.equal(hostBrief({ prompt: "task", sent_prompt: sent }, "claude-code"), "contract + task");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: sent }, "claude-code-s2"), "contract2 + task",
    "a fan-out variant reads its own label's entry");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: sent }, "codex"), "task", "an empty entry falls back");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: sent }, "cursor"), "task", "a missing key falls back");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: sent }, undefined), "task", "no label in hand falls back");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: "contract + task" }, "claude-code"), "task",
    "a string sent_prompt is not the contract and counts as not provided");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: ["x"] }, "0"), "task");
  assert.equal(hostBrief({ prompt: "task", sent_prompt: sent }, "constructor"), "task");
  assert.equal(hostBrief({ prompt: "task" }, "claude-code"), "task", "older runs fall back to the task prompt");
  assert.equal(hostBrief({ prompt: "", sent_prompt: { "claude-code": "" } }, "claude-code"), undefined,
    "nothing to show renders no brief, never an empty one");
  assert.equal(hostBrief({}, "claude-code"), undefined);
  assert.equal(hostBrief(undefined, "claude-code"), undefined);
});
