// A turn's tool calls fold into one line per kind (lib/tool-groups.ts).
// Forty read calls are one line that counts up, not forty cards. Invented
// calls only.
import assert from "node:assert/strict";
import { test } from "node:test";
import { groupState, groupTools } from "../src/lib/tool-groups.ts";
import type { ToolInvocation } from "../src/lib/types.ts";

const call = (id: string, name: string, state: ToolInvocation["state"] = "output-available",
              kind?: "agent"): ToolInvocation => ({ id, name, args: {}, summary: "", state, kind });

test("forty reads are one group, in the order the kind first appeared", () => {
  const tools = [call("f0", "find_tools")];
  for (let i = 0; i < 40; i++) tools.push(call(`r${i}`, "read"));
  tools.push(call("f1", "find_tools"), call("o0", "operate"));
  const g = groupTools(tools);
  assert.deepEqual(g.map((x) => [x.name, x.tools.length]), [["find_tools", 2], ["read", 40], ["operate", 1]]);
});

test("a group counts up as calls arrive, keeping its place", () => {
  const first = groupTools([call("r0", "read"), call("o0", "operate")]);
  const later = groupTools([call("r0", "read"), call("o0", "operate"), call("r1", "read")]);
  assert.equal(first[0].key, later[0].key);
  assert.deepEqual(later.map((x) => [x.name, x.tools.length]), [["read", 2], ["operate", 1]]);
});

test("a dispatched agent keeps its own card, never folded", () => {
  const g = groupTools([call("a0", "dispatch", "input-available", "agent"),
                        call("a1", "dispatch", "input-available", "agent"), call("r0", "read")]);
  assert.deepEqual(g.map((x) => [x.key, x.tools.length]), [["agent:a0", 1], ["agent:a1", 1], ["tool:read", 1]]);
});

test("operate folds by the action it ran, live (args.tool) and reloaded (operate_tool)", () => {
  const op = (id: string, inner: string, persisted = false): ToolInvocation =>
    persisted
      ? { id, name: "operate", args: {}, summary: "", state: "output-available", operateTool: inner }
      : { id, name: "operate", args: { tool: inner, args: {} }, summary: "", state: "output-available" };
  const g = groupTools([op("o0", "workspace_file"), op("o1", "revoke_nac_rule", true),
                        op("o2", "workspace_file", true), call("r0", "read"), op("o3", "workspace_file")]);
  assert.deepEqual(g.map((x) => [x.key, x.label, x.tools.length]), [
    ["operate:workspace_file", "Operate workspace file", 3],
    ["operate:revoke_nac_rule", "Operate revoke nac rule", 1],
    ["tool:read", "Read", 1],
  ]);
  // An operate call that names no action still folds, under the door's name.
  assert.deepEqual(groupTools([call("x", "operate"), call("y", "operate")]).map((x) => [x.key, x.label]),
                   [["tool:operate", "Operate"]]);
});

test("the grouped line reads as words aloud and stays shut on a failure", async () => {
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("../src/components/chat/tool-card.tsx", import.meta.url), "utf8");
  const card = src.slice(src.indexOf("export function ToolGroupCard"), src.indexOf("// ── Dispatched-agent card"));
  assert.ok(card.includes('<span aria-hidden="true">·</span>') && card.includes('<span className="sr-only"> calls</span>'),
            "the dot is hidden from a screen reader and the count is spoken as calls");
  const groupOpen = [...card.matchAll(/<Tool\b[^>]*?defaultOpen=\{([^}]*)\}/g)].map((m) => m[1]);
  assert.deepEqual(groupOpen, ["opened"], "only the person's own choice opens the group, never a failure");
});

test("a lone card the person opened stays open when the second call makes it a group", async () => {
  // The lone ToolCard → group Tool swap remounts the card; ToolGroupCard is
  // not remounted (keyed by the group), so it holds what the person chose.
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("../src/components/chat/tool-card.tsx", import.meta.url), "utf8");
  const card = src.slice(src.indexOf("export function ToolGroupCard"), src.indexOf("// ── Dispatched-agent card"));
  assert.ok(card.includes("const [opened, setOpened] = useState(false);"), "the group holds the choice");
  assert.ok(/<ToolCard\b[^>]*onOpenChange=\{setOpened\}/.test(card), "the lone card reports the person's toggle");
  assert.ok(/<Tool className="mb-2" defaultOpen=\{defaultOpen\} onOpenChange=\{onOpenChange\}>/.test(src),
            "ToolCard hands the toggle to its Collapsible");
});

test("a lone call that cannot render costs that call, not every later call of its kind", async () => {
  // chat-message.tsx's boundary is keyed by the GROUP; once caught there, a
  // throw would hide the whole group as it grows. Each call has its own.
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("../src/components/chat/tool-card.tsx", import.meta.url), "utf8");
  const card = src.slice(src.indexOf("export function ToolGroupCard"), src.indexOf("// ── Dispatched-agent card"));
  const lone = card.slice(card.indexOf("if (tools.length === 1)"), card.indexOf("const failed"));
  assert.ok(/<ErrorBoundary key=\{t\.id\}[^>]*>\s*<ToolCard\b/.test(lone), "the lone card has its own boundary");
});

test("the group's state: failed beats running beats done", () => {
  assert.equal(groupState([call("a", "read"), call("b", "read", "output-error")]), "output-error");
  assert.equal(groupState([call("a", "read"), call("b", "read", "input-available")]), "input-available");
  assert.equal(groupState([call("a", "read"), call("b", "read")]), "output-available");
});
