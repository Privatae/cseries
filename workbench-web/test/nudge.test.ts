import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeHistory } from "../src/lib/history-merge.ts";
import type { ChatMessage } from "../src/lib/types.ts";

// While a turn runs, what the person types goes into it (a nudge), and is
// saved as a user row when the turn reads it — while any turn runs, not only
// while one holds.

const msg = (role: "user" | "assistant", text: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ id: `${role[0]}-1754870000000-${text.length}`, role, text, tools: [], createdAt: "2026-10-01T00:00:00Z", ...extra });

test("the nudge's saved user row replaces its echo (no second copy, nothing lost on reload)", () => {
  const echo = msg("user", "also make the dock blue", { nudge: true });
  const local = [msg("user", "Build it."), msg("assistant", "Elm Lantern is building — task 420."), echo];
  const server: ChatMessage[] = [
    { id: "6401", role: "user", text: "Build it.", tools: [], createdAt: "2026-10-01T00:00:00Z" },
    { id: "6402", role: "assistant", text: "Elm Lantern is building — task 420.", tools: [], createdAt: "2026-10-01T00:00:01Z" },
    { id: "6403", role: "user", text: "also make the dock blue", tools: [], createdAt: "2026-10-01T00:00:30Z", nudge: true },
  ];
  const out = mergeHistory(local, server);
  assert.equal(out.filter((m) => m.text === "also make the dock blue").length, 1);
  assert.equal(out[out.length - 1].id, "6403");
});

test("until the turn reads it, the nudge's echo stays", () => {
  const echo = msg("user", "also make the dock blue", { nudge: true });
  const local = [msg("user", "Build it."), echo];
  const server: ChatMessage[] = [
    { id: "6401", role: "user", text: "Build it.", tools: [], createdAt: "2026-10-01T00:00:00Z" },
  ];
  assert.ok(mergeHistory(local, server).some((m) => m === echo));
});

test("while a turn runs a send goes into the turn, and a copy is held until the turn reads it", async () => {
  const fs = await import("node:fs");
  const pane = fs.readFileSync(new URL("../src/components/workbench/chat-pane.tsx", import.meta.url), "utf8");
  const hook = fs.readFileSync(new URL("../src/hooks/use-workbench-chat.ts", import.meta.url), "utf8");
  assert.ok(pane.includes("const nudging = busy;")
    && pane.includes("if (nudging) {\n      void chat.nudgeTurn(projectId, text)"),
    "a send while a turn runs goes into the turn");
  assert.ok(hook.includes("PENDING_SENDS.push({ key, projectId, text, ")
    && hook.includes("const r = await nudgeTurnApi(turnId, text, clientId);"),
    "held as the next message if the turn ends before reading it");
});
