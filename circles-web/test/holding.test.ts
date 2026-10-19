import { test } from "node:test";
import assert from "node:assert/strict";
import { isHolding } from "../src/lib/holding.ts";
import { mergeHistory } from "../src/lib/history-merge.ts";
import type { ChatMessage, ToolInvocation } from "../src/lib/types.ts";

// A turn HOLDING for what it started never
// takes the person's composer away — Send stays Send, and what they type goes
// into the holding turn (and is saved as a user row when the turn reads it).

const tool = (name: string, state: ToolInvocation["state"]): ToolInvocation =>
  ({ id: `call_${name}`, name, args: { task: "build task 420 (Elm Lantern)" }, summary: "", state }) as ToolInvocation;
const msg = (role: "user" | "assistant", text: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ id: `${role[0]}-1754870000000-${text.length}`, role, text, tools: [], createdAt: "2026-10-01T00:00:00Z", ...extra });

test("a streaming reply with its waiting card up is HOLDING", () => {
  assert.equal(isHolding([msg("user", "Build it."),
    msg("assistant", "Elm Lantern is building — task 420.", { streaming: true, tools: [tool("waiting", "input-available")] })]), true);
});

test("a streaming reply without a waiting card is not holding (Stop, as before)", () => {
  assert.equal(isHolding([msg("user", "Build it."),
    msg("assistant", "Thinking", { streaming: true, tools: [tool("build", "input-available")] })]), false);
});

test("the waiting card completed — the turn went on — is not holding", () => {
  assert.equal(isHolding([msg("assistant", "Elm Lantern landed.", { streaming: true,
    tools: [tool("waiting", "output-available")] })]), false);
});

test("an ended turn is not holding", () => {
  assert.equal(isHolding([msg("assistant", "Done.", { tools: [tool("waiting", "input-available")] })]), false);
});

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

test("while a turn holds, what you write goes into it, and Stop stays beside it", async () => {
  const fs = await import("node:fs");
  const comp = fs.readFileSync(new URL("../src/cards/composer.tsx", import.meta.url), "utf8");
  const host = fs.readFileSync(new URL("../src/cards/use-host-thread.ts", import.meta.url), "utf8");
  assert.ok(comp.includes('aria-label="Stop the turn"'), "Stop stays reachable beside the box");
  // One conversation (slice 5): the Host picks the topic first; words for
  // the running topic go into its turn (held as the next message if unread),
  // words for another topic start their own turn beside it.
  // The same goes for words typed while the turn streams (the composer's
  // nudge), and a topic already running beside takes its own words.
  assert.ok(host.includes("if (holdingRef.current && text.trim() && !opts.carry) {")
    && host.includes("const r = await whileRunning(text, opts, true);")
    && host.includes("await nudgeRunningTurn(SELF, words, running, true);")
    && host.includes("if (fallbackNudge && dest === normalizeThreadId(running)) {")
    && host.includes("if (await chat.nudgeBeside(topic.id, words)) {")
    && host.includes("return sendBeside(words, topic, picked, opts);"),
    "a send while the turn holds is nudged into it when it is that topic's, else runs beside it");
  assert.ok(/const nudge = useCallback\(\s*async \(text: string[^)]*\) => \{[\s\S]{0,200}whileRunning\(text, opts, true\)/.test(host),
    "words typed while the turn streams get the same topic pick");
});
