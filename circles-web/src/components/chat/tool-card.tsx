import {
  Tool,
  ToolContent,
  ToolHeader,
  ToolInput,
  ToolOutput,
} from "@/components/ai-elements/tool";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type AgentRow, cancelAgent, fetchAgents } from "@/lib/api";
import type { ToolInvocation } from "@/lib/types";
import { groupState, type ToolGroup } from "@/lib/tool-groups";
import { ErrorBoundary } from "@/components/error-boundary";
import { cn } from "@/lib/utils";
import { useCallback, useEffect, useRef, useState } from "react";

function errorTextFor(tool: ToolInvocation): string | undefined {
  if (tool.state !== "output-error") return undefined;
  const r = tool.result;
  if (r && typeof r === "object") {
    const o = r as { message?: string; error?: string; result?: string };
    return o.message || o.error || o.result || "Tool call failed";
  }
  if (typeof r === "string" && r) return r;
  return "Tool call failed";
}

/**
 * A tool the host ran, rendered as a collapsible card: name + live status badge
 * in the header, parameters + result inside. Every web search, memory read, or
 * edit the Host runs is visible with its real payload — no blind activity.
 *
 * A dispatched AGENT is not a tool the host ran; it's delegated work the user
 * must be able to WATCH and KILL (spec "Agent UI"), so it renders as an
 * AgentToolCard with live status + a veto instead.
 */
export function ToolCard({
  tool,
  defaultOpen,
  onOpenChange,
}: {
  tool: ToolInvocation;
  defaultOpen?: boolean;
  /** The person opened or shut it (ToolGroupCard carries it into the group). */
  onOpenChange?: (open: boolean) => void;
}) {
  if (tool.kind === "agent") return <AgentToolCard tool={tool} />;

  const errorText = errorTextFor(tool);
  // Title = "<Tool> <verb>" ("Gitea grep", "Artifact write", "Read"); the
  // subject (the search term, the file, the name) is the muted, quoted
  // subtitle. It used to be "<Tool> · <first string arg>", so a host grepping
  // for a sentence headlined the card with that sentence.
  const pretty =
    tool.name.charAt(0).toUpperCase() + tool.name.slice(1).replace(/[_.]/g, " ");
  // The build card's summary is a stage SENTENCE ("planning — … (researching
  // first — no code runs yet)"), not an argument: it stays the headline's
  // plain continuation, unquoted, and the verb is dropped where it would only
  // repeat the name ("Build build").
  const isBuild = tool.name === "build";
  const verb = tool.verb && tool.verb !== pretty.toLowerCase() ? tool.verb : "";
  const title = verb && !isBuild ? `${pretty} ${verb}` : pretty;
  const subtitle = tool.summary
    ? isBuild
      ? tool.summary
      : `“${tool.summary}”`
    : undefined;
  const hasOutput = errorText !== undefined || tool.result !== undefined;

  return (
    <Tool className="mb-2" defaultOpen={defaultOpen} onOpenChange={onOpenChange}>
      <ToolHeader
        state={tool.state}
        subtitle={subtitle}
        title={title}
        toolName={tool.name}
        type="dynamic-tool"
      />
      <ToolContent>
        <ToolInput input={tool.args} />
        {hasOutput && (
          <ToolOutput
            errorText={errorText}
            output={errorText ? undefined : tool.result}
          />
        )}
      </ToolContent>
    </Tool>
  );
}

/**
 * Every call of one kind in a turn, as ONE line that counts up ("Read · 40"),
 * opening onto each call's own card (lib/tool-groups.ts). A lone call is its
 * own card.
 *
 * A failure does NOT open the group: the line says it ("2 failed", the Error
 * badge) and the failed call's card is open inside. Opening it would put the
 * forty-card stack back on every reload of a turn where one read failed, and
 * a live turn could not do it anyway (`defaultOpen` counts only when the group
 * first mounts, at its second call, before any failure has arrived).
 *
 * The lone card becomes the group at the kind's second call, and that swap
 * remounts it; this component is not remounted (chat-message.tsx keys it by
 * the group), so it carries the person's own choice across: a card they
 * opened mid-turn stays open as the group, never snaps shut under them.
 */
export function ToolGroupCard({ group }: { group: ToolGroup }) {
  const { label, name, tools } = group;
  const [opened, setOpened] = useState(false);
  if (tools.length === 1) {
    const t = tools[0];
    // Its own boundary, keyed by the call: the one around this component is
    // keyed by the group, and a throw caught there would hide every later
    // call of the kind behind one notice.
    return (
      <ErrorBoundary key={t.id} label={`tool:${t.name}`}>
        <ToolCard defaultOpen={t.state === "output-error"} onOpenChange={setOpened} tool={t} />
      </ErrorBoundary>
    );
  }
  const failed = tools.filter((t) => t.state === "output-error").length;
  return (
    <Tool className="mb-2" defaultOpen={opened}>
      <ToolHeader
        state={groupState(tools)}
        subtitle={failed ? `${failed} failed` : undefined}
        // Read aloud as "Read 40 calls", not "Read middle dot 40".
        title={
          <>
            {label} <span aria-hidden="true">·</span> {tools.length}
            <span className="sr-only"> calls</span>
          </>
        }
        toolName={name}
        type="dynamic-tool"
      />
      <ToolContent className="space-y-0 p-2">
        {tools.map((t) => (
          <ErrorBoundary key={t.id} label={`tool:${t.name}`}>
            <ToolCard defaultOpen={t.state === "output-error"} tool={t} />
          </ErrorBoundary>
        ))}
      </ToolContent>
    </Tool>
  );
}

// ── Dispatched-agent card: live status + veto ────────────────────────────────

const AGENT_TERMINAL = new Set([
  "completed",
  "failed",
  "timed_out",
  "cancelled",
]);

function seedStatusFor(tool: ToolInvocation): string {
  if (tool.state === "output-error") return "failed";
  const r = tool.result as { status?: string } | undefined;
  // dispatch returns {status:"dispatched", task_id}; treat as running.
  if (r?.status && r.status !== "dispatched" && r.status !== "ok") {
    return r.status;
  }
  return "running";
}

function statusView(status: string): {
  label: string;
  dot: string;
  variant: "secondary" | "destructive" | "outline";
} {
  switch (status) {
    case "completed":
      return { label: "Completed", dot: "bg-green-600", variant: "secondary" };
    case "failed":
      return { label: "Failed", dot: "bg-current", variant: "destructive" };
    case "timed_out":
      return { label: "Timed out", dot: "bg-current", variant: "destructive" };
    case "cancelled":
      return {
        label: "Killed",
        dot: "bg-muted-foreground",
        variant: "outline",
      };
    case "queued":
      return {
        label: "Queued",
        dot: "animate-pulse bg-current",
        variant: "secondary",
      };
    default:
      return {
        label: "Running",
        dot: "animate-pulse bg-current",
        variant: "secondary",
      };
  }
}

function fmtCost(n: number): string {
  if (n <= 0) return "";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

/** Poll the agent registry for this task's live status until it's terminal.
 * Seeds from the dispatch result so a fresh card reads "Running" immediately,
 * and keeps a reloaded card honest (the persisted chip only knows the status
 * at dispatch time). Stops on a terminal status or after a ~6-minute cap. */
function useAgentLiveStatus(taskId: string | undefined, seed: string) {
  const [status, setStatus] = useState(seed);
  const [cost, setCost] = useState<number | null>(null);
  // The full registry row — the card renders the live activity feed and the
  // terminal output from it, so a chat agent is never a black box.
  const [row, setRow] = useState<AgentRow | null>(null);
  const statusRef = useRef(seed);
  statusRef.current = status;

  useEffect(() => {
    if (!taskId || AGENT_TERMINAL.has(statusRef.current)) return;
    let stopped = false;
    let ticks = 0;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      // Poll to a TERMINAL state — no tick cap. The old 120-tick (~6 min)
      // ceiling silently stopped watching, freezing "Running" forever on any
      // agent that outlived it. Backs off 3s → 10s after ~5 min; a hidden tab
      // skips the fetch and re-checks on the next tick.
      if (document.hidden) {
        timer = setTimeout(poll, 10000);
        return;
      }
      const r = await fetchAgents(false);
      if (stopped) return;
      if (r.ok) {
        const row = r.data.agents.find(
          (a) => String(a.task_id) === String(taskId),
        );
        if (row) {
          setRow(row);
          setStatus(row.status);
          if (row.est_cost_usd != null) setCost(row.est_cost_usd);
          if (AGENT_TERMINAL.has(row.status)) return; // done — stop polling
        }
      }
      timer = setTimeout(poll, ++ticks < 100 ? 3000 : 10000);
    };
    timer = setTimeout(poll, 1500);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [taskId]);

  return { status, cost, row, setStatus };
}

function AgentToolCard({ tool }: { tool: ToolInvocation }) {
  const { status, cost, row, setStatus } = useAgentLiveStatus(
    tool.taskId,
    seedStatusFor(tool),
  );
  const [cancelling, setCancelling] = useState(false);
  const [killErr, setKillErr] = useState<string | null>(null);

  const terminal = AGENT_TERMINAL.has(status);
  const view = statusView(status);
  const argScope = (tool.args as { tool_scope?: unknown })?.tool_scope;
  const scope = Array.isArray(argScope) ? (argScope as string[]) : [];
  const dispatchErr =
    tool.state === "output-error" ? errorTextFor(tool) : undefined;

  const kill = useCallback(async () => {
    if (!tool.taskId) return;
    setCancelling(true);
    setKillErr(null);
    const r = await cancelAgent(tool.taskId);
    setCancelling(false);
    if (r.ok) setStatus(r.data.status || "cancelled");
    else setKillErr(r.message || "Couldn't kill the agent — try again.");
  }, [tool.taskId, setStatus]);

  return (
    <div className="mb-2 rounded-md border border-l-2 border-l-primary p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span aria-hidden>⚙</span>
            <span className="font-medium text-sm">
              Agent{tool.taskId ? ` #${tool.taskId}` : ""}
            </span>
            <Badge
              className="gap-1.5 rounded-full text-xs"
              variant={view.variant}
            >
              <span className={cn("size-1.5 rounded-full", view.dot)} />
              {view.label}
            </Badge>
            {cost != null && fmtCost(cost) ? (
              <span className="text-muted-foreground text-xs tabular-nums">
                {fmtCost(cost)}
              </span>
            ) : cost === null && terminal ? (
              /* A FINISHED agent with no cost is unpriced, not free. The run is
                 priced from the model catalog, which returns null when it holds
                 no published rate for the model that ran (server/routes/
                 inference.py estimate_call_cost_usd, via agent_dispatch.py
                 _est_cost). Rendering nothing reads as $0 to a person, and $0
                 is the one thing it does not mean. Only once terminal: while
                 the agent runs, no cost has been reported yet. */
              <span
                className="text-muted-foreground text-xs"
                title="no published rate card for this model, so no dollar figure can be honestly given"
              >
                unpriced
              </span>
            ) : null}
          </div>
          {tool.summary && (
            <div className="mt-1 text-muted-foreground text-xs">
              {tool.summary}
            </div>
          )}
          {scope.length > 0 && (
            <div className="mt-0.5 text-muted-foreground text-xs">
              tools: {scope.join(", ")}
            </div>
          )}
          {dispatchErr && (
            <div className="mt-1 text-destructive text-xs">{dispatchErr}</div>
          )}
          {killErr && (
            <div className="mt-1 text-destructive text-xs">{killErr}</div>
          )}
          {!terminal && (row?.activity?.length ?? 0) > 0 && (
            <div className="mt-1.5 space-y-0.5">
              {(row?.activity ?? []).slice(-4).map((a, i) => (
                <div className="text-muted-foreground text-xs" key={i}>
                  <span aria-hidden>{a.event === "result" ? "✓" : "→"}</span>{" "}
                  {a.tool}
                  {a.status && a.status !== "ok" ? ` (${a.status})` : ""}
                </div>
              ))}
            </div>
          )}
          {terminal && (row?.error || row?.result) && (
            <div className="mt-1.5 whitespace-pre-wrap break-words text-xs">
              {(() => {
                const outcome = String(row?.error || row?.result || "");
                if (outcome.length <= 700) return outcome;
                return (
                  <details>
                    <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                      {outcome.slice(0, 300)}… (show all)
                    </summary>
                    {outcome}
                  </details>
                );
              })()}
            </div>
          )}
        </div>
        {!terminal && tool.taskId && (
          <Button
            className="shrink-0"
            disabled={cancelling}
            onClick={kill}
            size="xs"
            variant="destructive"
          >
            {cancelling ? "Killing…" : "Kill"}
          </Button>
        )}
      </div>
    </div>
  );
}
