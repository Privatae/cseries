import type { ToolInvocation } from "@/lib/types";

/**
 * A turn's tool calls as the reply shows them: every call of one kind folded
 * into ONE group, in the order the kind first appeared, so forty reads are one
 * "Read · 40" line that counts up while the turn runs, not forty cards: no
 * one wants to read forty read cards stacked in a conversation.
 *
 * A dispatched agent is never folded: it is delegated work the person must be
 * able to watch and stop on its own card (tool-card.tsx AgentToolCard).
 *
 * `operate` is the action door: each call runs a different action
 * (operate(tool='revoke_nac_rule'), operate(tool='workspace_file')). Folded by
 * the door alone, a firewall rule revoked and five files written would read
 * as one "Operate · 6", and what changed would be hidden behind a click. So
 * an operate call folds with the calls that ran the SAME action ("Operate
 * workspace file · 5"). `read` stays one line whatever it read: it changes
 * nothing, and it is the forty-call stack this folds.
 */
export type ToolGroup = {
  key: string;
  name: string;
  /** The line's words before the count: "Read", "Operate workspace file". */
  label: string;
  tools: ToolInvocation[];
};

/** "web_search" → "Web search", as a single call's card titles it. */
export function prettyToolName(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1).replace(/[_.]/g, " ");
}

/** The action an `operate` call ran: the persisted chip's `operate_tool`
 * (api.ts mapToolCall), else the live call's own `tool` argument. */
export function operatedTool(t: ToolInvocation): string {
  if (t.name !== "operate") return "";
  if (t.operateTool) return t.operateTool;
  const a = t.args;
  const inner = a && typeof a === "object" ? (a as { tool?: unknown }).tool : undefined;
  return typeof inner === "string" ? inner.trim() : "";
}

export function groupTools(tools: readonly ToolInvocation[]): ToolGroup[] {
  const groups: ToolGroup[] = [];
  const byKey = new Map<string, ToolGroup>();
  for (const t of tools) {
    if (t.kind === "agent") {
      groups.push({ key: `agent:${t.id}`, name: t.name, label: prettyToolName(t.name), tools: [t] });
      continue;
    }
    const inner = operatedTool(t);
    const key = inner ? `operate:${inner}` : `tool:${t.name}`;
    let g = byKey.get(key);
    if (!g) {
      const label = inner
        ? `${prettyToolName(t.name)} ${inner.replace(/[_.]/g, " ")}`
        : prettyToolName(t.name);
      g = { key, name: t.name, label, tools: [] };
      byKey.set(key, g);
      groups.push(g);
    }
    g.tools.push(t);
  }
  return groups;
}

/** The group's one state, as a single call's badge would show it: any call
 * failed → failed; else any still running → running; else done. */
export function groupState(tools: readonly ToolInvocation[]): ToolInvocation["state"] {
  if (tools.some((t) => t.state === "output-error")) return "output-error";
  const running = tools.find((t) => t.state !== "output-available" && t.state !== "output-error");
  return running ? running.state : "output-available";
}
