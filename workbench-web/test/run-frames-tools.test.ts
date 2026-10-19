import { test } from "node:test";
import assert from "node:assert/strict";
import {
  clipBody,
  frameToLines,
  TOOL_BODY_MAX_LINES,
  toolCallLines,
  toolResultLines,
  type RunFrame,
} from "../src/lib/run-frames.ts";

const f = (seq: number, kind: string, payload: Record<string, unknown>): RunFrame => ({
  seq,
  kind,
  payload,
});

test("tool_call: the harness's shell command prints verbatim (normalizer keys it `args`)", () => {
  // The exact payload shape the claude-code sidecar streams (factory-build-299).
  const lines = frameToLines(
    f(1, "tool_call", {
      tool: "Bash",
      args: {
        command: 'ls -la /var/lib/cseries/projects/claude-code 2>/dev/null; echo "---"; pwd',
        description: "Inspect working directory",
      },
    }),
  );
  assert.equal(lines[0].text, "→ Bash — Inspect working directory");
  assert.equal(lines[0].style, "tool");
  assert.equal(
    lines[1].text,
    '$ ls -la /var/lib/cseries/projects/claude-code 2>/dev/null; echo "---"; pwd',
  );
  assert.equal(lines[1].style, "text");
});

test("tool_call: multi-line heredoc commands keep their lines; file ops show the path; blobs are sized", () => {
  const cmd = "cat > app.yaml <<'YAML'\nid: finance\nname: Finance\nYAML\ncat app.yaml";
  const lines = toolCallLines(2, "Bash", { command: cmd, description: "Write app.yaml" });
  assert.equal(lines[0].text, "→ Bash — Write app.yaml");
  assert.deepEqual(
    lines.slice(1).map((l) => l.text),
    ["$ cat > app.yaml <<'YAML'", "id: finance", "name: Finance", "YAML", "cat app.yaml"],
  );
  const w = toolCallLines(3, "Write", { file_path: "/p/index.html", content: "x".repeat(5000) });
  assert.equal(w[0].text, "→ Write /p/index.html");
  assert.equal(w[1].text, "  content: 5000 chars");
  // legacy `input` key still renders
  const legacy = frameToLines(f(4, "tool_call", { tool: "write", input: { path: "a.html" } }));
  assert.equal(legacy[0].text, "→ write a.html");
  // unknown-shape args: one JSON line, never dropped
  const other = toolCallLines(5, "Skill", { skill: "dataviz", args: "light fintech" });
  assert.equal(other[0].text, "→ Skill");
  assert.match(other[1].text, /"skill":"dataviz"/);
});

test("tool_result: output body renders (clipped); errors render as errors; empty stays one line", () => {
  const ok = frameToLines(
    f(6, "tool_result", { tool: "", status: "ok", result: "total 32\n-rw-r--r-- app.yaml\n-rw-r--r-- index.html" }),
  );
  assert.equal(ok[0].text, "✓");
  assert.deepEqual(
    ok.slice(1).map((l) => l.text),
    ["  total 32", "  -rw-r--r-- app.yaml", "  -rw-r--r-- index.html"],
  );
  const err = toolResultLines(7, "Bash", "error", "command not found: bun");
  assert.equal(err[0].text, "✗ Bash — error");
  assert.equal(err[0].style, "error");
  assert.equal(err[1].style, "error");
  assert.deepEqual(toolResultLines(8, "Read", "ok", ""), [{ seq: 8, style: "tool", text: "✓ Read" }]);
  assert.deepEqual(toolResultLines(9, "", "ok", undefined), [{ seq: 9, style: "tool", text: "✓" }]);
});

test("clipBody: bounded by lines and chars with an honest +N tail", () => {
  const big = Array.from({ length: 120 }, (_, i) => `line ${i}`).join("\n");
  const out = clipBody(big);
  assert.equal(out.length, TOOL_BODY_MAX_LINES + 1);
  assert.equal(out[out.length - 1], "… +80 more lines (raw shows all)");
  const wide = clipBody("a".repeat(2000) + "\n" + "b".repeat(2000));
  assert.equal(wide.length, 2);
  assert.equal(wide[1], "… +1 more line (raw shows all)");
  assert.deepEqual(clipBody("one\r\ntwo"), ["one", "two"]);
});
