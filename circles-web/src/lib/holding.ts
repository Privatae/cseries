import type { ChatMessage } from "./types";

/** The thread's turn is HOLDING: its streaming reply shows the kernel's
 * `waiting` card (session.py's hold — the turn waits for a build, an agent or
 * an answer it started). The person's composer stays a Send composer then:
 * what they type goes INTO that turn. */
export function isHolding(messages: ChatMessage[]): boolean {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    return (
      !!m.streaming &&
      m.tools.some((t) => t.name === "waiting" && t.state === "input-available")
    );
  }
  return false;
}
