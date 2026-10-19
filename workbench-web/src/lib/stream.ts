/**
 * stream.ts — TS port (workbench copy) of the kernel's shared AI SDK Data Stream
 * Protocol client (dashboard/base/js/chat-stream.js). Parses exactly what
 * brain_service.send_message emits over /api/brain/messages:
 *
 *   f:{"messageId"}                      — start step        (no dispatch)
 *   2:[]                                 — heartbeat          (silent)
 *   2:[{type:"session",session_id}]      — typed data items   (onData)
 *   2:[{t:"context",...}]                — context accounting (onContext)
 *   9:{"toolCallId","toolName","args"}   — tool call          (onTool)
 *   a:{"toolCallId","result"}            — tool result        (onResult)
 *   0:"text content"                     — text delta         (onText)
 *   d:/e:                                — finish             (no dispatch)
 *   3:"error message"                    — error (terminal)   (onError)
 *
 * A malformed frame is dropped and the next is tried (matches the kernel
 * client). Aborts propagate via the caller's AbortSignal on the fetch.
 */

import type { CloseTurnSignal } from "@kit/close-turn";

/** The frame's close_turn signal (runtime/close_turn.normalize's shape), or
 * null. Kept here, not imported from the kit, so node's test runner can load
 * this file (a type-only import is erased). */
function parseCloseTurn(v: unknown): CloseTurnSignal | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  return Object.keys(v).length ? (v as CloseTurnSignal) : null;
}

export type ToolStatusKind = "running" | "ok" | "error";

/** Conversation-depth frame (`2:[{"t":"context",...}]`) — the turn's context
 * accounting: true billed input tokens, the model's window, the thread's
 * budget, and the consolidator stamps (summary_version / consolidated_at)
 * that light the View-context button when they move. Every field is
 * nullable: older backends omit the frame entirely, partial ones omit keys. */
export interface ContextFrame {
  tokens_in: number | null;
  window: number | null;
  budget: number | null;
  summary_version: number | null;
  consolidated_at: string | null;
  /** Provenance (2026-08-18): who answered — the served model + provider,
   * and the failover record when the primary errored and the route's
   * fallback provider took the call. Same values the turn row persists. */
  model: string | null;
  provider: string | null;
  failed_over: FailedOver | null;
  /** The context ledger — what the turn actually sent, sizes only. */
  ledger: ContextLedger | null;
  /** The Host's close_turn signal, so the live turn shows Review before reload. */
  close_turn: CloseTurnSignal | null;
  /** What the turn cost, the same estimate and basis word its row is stamped
   * with (brain_service.py's context frame), so the live turn prints its
   * price before a reload. Optional: frames built by hand may leave them. */
  est_cost_usd?: number | null;
  price_basis?: string | null;
}

/** session.py's per-turn context ledger (metadata.context_ledger): one
 * entry per model call plus the sizes of what was assembled. Sizes only,
 * never content. Every field optional — older rows predate it. */
export interface ContextLedger {
  calls?: number;
  per_call?: { in?: number; cached?: number; out?: number }[];
  system_chars?: number;
  per_turn_blocks?: Record<string, number>;
  history?: { turns?: number; chars?: number; summary?: boolean };
  tools?: { count?: number; chars?: number | null };
  tool_results?: { tool?: string; chars?: number | null }[];
  user_chars?: number;
  /** The turn's spend record (turn_allowance.TurnSpend.summary), totalled
   * over EVERY call before per_call is cut at 24: what the page's running
   * count sums (context_budget.running_spend). Rows from before 2026-09-16
   * have none. */
  spend?: {
    calls?: number;
    tokens?: { uncached?: number; cache_read?: number; cache_write?: number; output?: number };
  };
}

export function parseContextLedger(v: unknown): ContextLedger | null {
  if (!v || typeof v !== "object") return null;
  return v as ContextLedger;
}

/** The gateway's failover annotation: primary errored, fallback answered. */
export interface FailedOver {
  from_provider?: string | null;
  from_model?: string | null;
  to_provider?: string | null;
  to_model?: string | null;
  primary_error?: string | null;
}

const numOrNull = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;
const strOrNull = (v: unknown): string | null =>
  typeof v === "string" && v ? v : null;

export function parseFailedOver(v: unknown): FailedOver | null {
  if (!v || typeof v !== "object") return null;
  const f = v as Record<string, unknown>;
  return {
    from_provider: strOrNull(f.from_provider),
    from_model: strOrNull(f.from_model),
    to_provider: strOrNull(f.to_provider),
    to_model: strOrNull(f.to_model),
    primary_error: strOrNull(f.primary_error),
  };
}

/** Normalize a `2:`-channel data item into a ContextFrame, or null when the
 * item isn't a context frame. Tolerates missing/extra keys. */
export function parseContextFrame(item: unknown): ContextFrame | null {
  if (!item || typeof item !== "object") return null;
  const it = item as Record<string, unknown>;
  if (it.t !== "context") return null;
  return {
    tokens_in: numOrNull(it.tokens_in),
    window: numOrNull(it.window),
    budget: numOrNull(it.budget),
    summary_version: numOrNull(it.summary_version),
    consolidated_at:
      typeof it.consolidated_at === "string" ? it.consolidated_at : null,
    model: strOrNull(it.model),
    provider: strOrNull(it.provider),
    failed_over: parseFailedOver(it.failed_over),
    ledger: parseContextLedger(it.ledger),
    close_turn: parseCloseTurn(it.close_turn),
    est_cost_usd: numOrNull(it.est_cost_usd),
    price_basis: strOrNull(it.price_basis),
  };
}

/** Did the consolidator return since `seen` (the stamps the last GET/panel
 * open recorded)? consolidated_at is the primary signal; summary_version is
 * the fallback when the backend stamps no timestamp. A frame carrying
 * neither never lights the button. */
export function contextChanged(
  frame: ContextFrame,
  seen: { summary_version: number | null; consolidated_at: string | null },
): boolean {
  if (frame.consolidated_at != null) {
    return frame.consolidated_at !== seen.consolidated_at;
  }
  if (frame.summary_version != null) {
    return (
      seen.consolidated_at == null &&
      frame.summary_version !== seen.summary_version
    );
  }
  return false;
}

export interface StreamUsage {
  /** True total input context (cached + uncached) — brain_service normalizes
   * across lanes before emitting the frame. */
  promptTokens?: number;
  completionTokens?: number;
  /** Prefix tokens served FROM cache this turn (a SUBSET of promptTokens).
   * brain_service has always emitted these (brain_service.py:646-649) and
   * Circles has always shown them; the Workbench declared a narrower type and
   * dropped them on the floor, so its chat line could not distinguish real
   * consumption from a cache hit. */
  cachedTokens?: number;
  /** Prefix tokens WRITTEN to cache this turn (also a subset). */
  cacheWriteTokens?: number;
}

export interface StreamHandlers {
  onText?: (delta: string, fullSoFar: string) => void;
  /** Live reasoning delta (`g:` frame) — the host's thinking, streamed while
   * it works. Fired with the delta and the accumulated reasoning so far. */
  onReasoning?: (delta: string, fullSoFar: string) => void;
  onTool?: (t: {
    toolCallId: string;
    toolName: string;
    args: Record<string, unknown>;
  }) => void;
  onResult?: (r: {
    toolCallId: string;
    result: unknown;
    status: ToolStatusKind;
  }) => void;
  onData?: (item: unknown) => void;
  /** Conversation-depth accounting frame (`2:[{t:"context",...}]`), fired
   * once per turn when the backend stamps it. The item also passes through
   * onData unchanged. */
  onContext?: (frame: ContextFrame) => void;
  onError?: (message: string) => void;
  /** Token usage off the d:/e: finish frames (brain_service emits
   * {finishReason, usage:{promptTokens, completionTokens}}). Fired once. */
  onUsage?: (usage: StreamUsage) => void;
  onDone?: (info: { fullText: string }) => void;
}

/** Status kind for a tool chip given its result envelope. */
export function toolStatus(result: unknown): ToolStatusKind {
  if (!result) return "running";
  const s =
    typeof result === "object" && result !== null
      ? (result as { status?: string }).status
      : null;
  if (s === "error" || s === "denied" || s === "blocked") return "error";
  return "ok";
}

const ARG_SUMMARY_KEYS = [
  "intent",
  "task",
  "message",
  "query",
  "pattern",
  "name",
  "session_id",
  "key",
  "search",
];

/** The operation a mixed-op tool ran (`op` / `action`), as words — the card
 * title is "<Tool> <verb>" ("Gitea grep", "Artifact write"), never the raw
 * argument. The argument is the SUBJECT and renders muted and quoted after
 * the title (mirrors circles-web). */
export function toolVerb(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const a = args as Record<string, unknown>;
  for (const k of ["op", "action"]) {
    const v = a[k];
    if (typeof v === "string" && v) return v.replace(/[_.]/g, " ").toLowerCase();
  }
  return "";
}

/** Pull a one-line summary out of a tool's args for the collapsed header. */
export function summarizeToolArgs(args: unknown, toolName?: string): string {
  if (!args || typeof args !== "object") return "";
  const a = args as Record<string, unknown>;
  const trunc = (v: string) => (v.length > 90 ? `${v.slice(0, 87)}…` : v);
  // Stage-honest build cards (the card used to say "build … running" during
  // the PLANNING/research stage, which told the user nothing). Name the stage the user is actually in; code only runs on the
  // build pool after the plan is approved.
  if (toolName === "build") {
    const intent = typeof a.intent === "string" && a.intent ? ` — ${trunc(a.intent)}` : "";
    const action = typeof a.action === "string" ? a.action : "";
    if (action === "plan") return `planning${intent} (researching first — no code runs yet)`;
    if (action === "continue") return `continuing the build on the build pool${intent}`;
    if (action === "build") return `building on the build pool${intent}`;
    if (action === "run") {
      const prompt =
        typeof a.prompt === "string" && a.prompt ? ` — ${trunc(a.prompt)}` : intent;
      const n = Array.isArray(a.variants) ? a.variants.length : 0;
      return n > 0
        ? `comparing on ${n} engine${n === 1 ? "" : "s"}${prompt}`
        : `building on the build pool${prompt}`;
    }
  }
  // A wrapper-shaped call ({tool, args} — the retired engine's `read`
  // meta-tool used this shape; nothing shipped emits it since 2026-08-19,
  // kept only so an old row still titles by its INNER tool instead of the
  // meaningless wrapper name): surface the inner tool + its most telling arg.
  if (typeof a.tool === "string" && a.tool) {
    const inner =
      a.args && typeof a.args === "object"
        ? (a.args as Record<string, unknown>)
        : {};
    for (const k of ARG_SUMMARY_KEYS) {
      const v = inner[k];
      if (typeof v === "string" && v) return `${a.tool} — ${trunc(v)}`;
    }
    const op = inner.op ?? inner.action;
    if (typeof op === "string" && op) return `${a.tool} · ${op}`;
    return String(a.tool);
  }
  for (const k of ARG_SUMMARY_KEYS) {
    const v = a[k];
    if (typeof v === "string" && v) {
      return trunc(v);
    }
  }
  return "";
}

export async function consumeStream(
  response: Response,
  handlers: StreamHandlers,
): Promise<{ fullText: string }> {
  const h = handlers;
  if (!response.body) {
    h.onError?.("no response body");
    return { fullText: "" };
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let fullText = "";
  let fullReasoning = "";
  let terminalEmitted = false;
  let usageEmitted = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        if (colon < 0) continue;
        const prefix = line.slice(0, colon);
        const rest = line.slice(colon + 1);
        let payload: unknown;
        try {
          payload = JSON.parse(rest);
        } catch {
          continue;
        }

        if (prefix === "0") {
          const t = typeof payload === "string" ? payload : "";
          if (t) {
            fullText += t;
            h.onText?.(t, fullText);
          }
        } else if (prefix === "g") {
          const t = typeof payload === "string" ? payload : "";
          if (t) {
            fullReasoning += t;
            h.onReasoning?.(t, fullReasoning);
          }
        } else if (prefix === "9") {
          const p = payload as {
            toolCallId: string;
            toolName?: string;
            args?: Record<string, unknown>;
          };
          // Host-driven app open: relay the host's open_app tool CALL to the
          // desktop shell (same-origin only), which opens the window under
          // the user's session — mirrors dashboard chat-stream.js.
          if (p.toolName === "open_app" && p.args && typeof p.args.app_id === "string" && p.args.app_id) {
            try {
              const openMsg: Record<string, unknown> = { type: "cseries:open-app", app_id: p.args.app_id };
              if (typeof p.args.view === "string" && p.args.view) openMsg.view = p.args.view;
              (window.parent || window).postMessage(openMsg, window.location.origin);
            } catch { /* not framed — host still reports it */ }
          }
          h.onTool?.({
            toolCallId: p.toolCallId,
            toolName: p.toolName || "tool",
            args: p.args || {},
          });
        } else if (prefix === "a") {
          const p = payload as { toolCallId: string; result: unknown };
          h.onResult?.({
            toolCallId: p.toolCallId,
            result: p.result,
            status: toolStatus(p.result),
          });
        } else if (prefix === "2") {
          if (Array.isArray(payload)) {
            for (const item of payload) {
              if (!item) continue;
              h.onData?.(item);
              const cf = parseContextFrame(item);
              if (cf) h.onContext?.(cf);
            }
          }
        } else if (prefix === "3") {
          terminalEmitted = true;
          h.onError?.(typeof payload === "string" ? payload : "(error)");
        } else if (prefix === "d" || prefix === "e") {
          // finish-step / finish-message — both carry the same usage object;
          // dispatch the first one seen so callers get counts exactly once.
          if (!usageEmitted && payload && typeof payload === "object") {
            const u = (payload as { usage?: StreamUsage }).usage;
            if (u && (u.promptTokens != null || u.completionTokens != null)) {
              usageEmitted = true;
              h.onUsage?.(u);
            }
          }
        }
        // f: — start step. No dispatch.
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released */
    }
  }

  if (!terminalEmitted) h.onDone?.({ fullText });
  return { fullText };
}
