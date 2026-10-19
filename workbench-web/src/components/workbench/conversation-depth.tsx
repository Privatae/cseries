import { LayersIcon, SlidersHorizontalIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  fetchModelCatalog,
  fetchThreadContext,
  putThreadContextBudget,
  putThreadTurnAllowance,
  type ThreadContextInfo,
} from "@/lib/api";
import { contextChanged, type ContextFrame } from "@/lib/stream";
import type { CatalogModel, ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

// Conversation depth: the header's
// view into the thread's context — a slim used/window bar with the budget
// marker, a settings dialog (budget slider + consolidator shortcut), and the
// View-context panel that shows exactly what fires with the next message.
// Every input degrades: no thread-context route (404) → the bar runs on
// stream frames + per-turn metrics and the panel says so; no frames at all
// (older backend) → the whole cluster hides until the first turn's metrics.

/** 41200 → "41.2k", 200000 → "200k", 1000000 → "1M". */
function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${+(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** A dollar figure a person reads without decoding. Sub-cent amounts keep
 * enough digits to be a number rather than "$0.00". */
function fmtUsd(n: number): string {
  if (n >= 0.01 || n === 0) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(4)}`;
}

/** The per-turn spend budget: a dollar box, with what the next turn is
 * projected to cost and what the last one actually cost underneath it.
 *
 * Empty means no budget — the turn runs until the model stops asking for
 * tools, exactly as it did before this field existed. A number means the
 * host stops starting new rounds of tool calls once it cannot afford the
 * next one; it never cuts an answer off mid-sentence, so a turn can end up
 * to one reply's worth over.
 *
 * When the model has no published per-token price the budget cannot be held
 * in dollars at all, and the panel says so rather than showing a dollar
 * figure that means nothing. */
function TurnBudget({
  info,
  onSave,
}: {
  info: ThreadContextInfo | null;
  onSave: (usd: number | null) => void;
}) {
  // Local text, so a half-typed "0." is not parsed into a save.
  const [text, setText] = useState<string>(
    info?.turn_allowance_usd != null ? String(info.turn_allowance_usd) : "",
  );
  useEffect(() => {
    setText(
      info?.turn_allowance_usd != null ? String(info.turn_allowance_usd) : "",
    );
  }, [info?.turn_allowance_usd]);

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    },
    [],
  );
  const queueSave = useCallback(
    (raw: string) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        const t = raw.trim();
        if (t === "") {
          onSave(null);
          return;
        }
        const v = Number(t);
        if (Number.isFinite(v) && v > 0) onSave(v);
      }, 500);
    },
    [onSave],
  );

  const basis = info?.allowance_basis ?? "none";
  const projected = info?.projected_next_turn_usd ?? null;
  const last = info?.last_turn ?? null;

  return (
    <div className="grid gap-1.5 border-border border-t pt-3">
      <label
        className="text-muted-foreground text-xs"
        htmlFor="turn-budget-usd"
      >
        Budget for each turn
      </label>
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground text-sm">$</span>
        <input
          className="w-24 rounded-md border border-border bg-background px-2 py-1 text-foreground text-sm tabular-nums"
          id="turn-budget-usd"
          inputMode="decimal"
          min={0}
          onChange={(e) => {
            setText(e.target.value);
            queueSave(e.target.value);
          }}
          placeholder="no limit"
          step="0.01"
          type="number"
          value={text}
        />
        {text.trim() !== "" && (
          <button
            className="text-muted-foreground text-xs underline-offset-2 hover:text-foreground hover:underline"
            onClick={() => {
              setText("");
              queueSave("");
            }}
            type="button"
          >
            Clear
          </button>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">
        What one reply may spend on looking things up. Leave it empty for no
        limit. The host stops starting new rounds once it can&apos;t afford
        the next one — it never cuts a reply off mid-sentence, so a turn can
        run a little over.
      </p>
      {basis === "tokens" && (
        <p className="text-[11px] text-warning">
          This model has no per-token price, so the budget is measured in
          tokens
          {info?.allowance_tokens != null
            ? `: ${fmtTok(info.allowance_tokens)} per turn.`
            : "."}
        </p>
      )}
      {basis === "none" && text.trim() !== "" && (
        <p className="text-[11px] text-warning">
          This model has no per-token price and this thread has no history
          budget to measure against, so no limit is in force this turn.
        </p>
      )}
      <div className="grid gap-0.5 text-[11px] text-muted-foreground">
        <div>
          Next turn, roughly:{" "}
          <span className="text-foreground tabular-nums">
            {projected != null ? fmtUsd(projected) : "not known yet"}
          </span>
        </div>
        <div>
          Last turn actually cost:{" "}
          <span className="text-foreground tabular-nums">
            {last?.spent_usd != null ? fmtUsd(last.spent_usd) : "not known yet"}
          </span>
          {last?.spent_usd != null && last.spent_partial && " (partial)"}
          {last?.calls != null && ` · ${last.calls} model calls`}
        </div>
        {last?.spent_usd != null && last.spent_partial && (
          <div className="text-warning">
            Some calls in that turn reported no usage, so the figure above
            covers only the calls that did.
          </div>
        )}
        {last?.basis_changed_from === "usd" && (
          <div className="text-warning">
            Part way through that turn the model was swapped for a backup that
            has no per-token price, so the dollar figure above covers only the
            part before the swap. The rest was measured in tokens.
          </div>
        )}
        {last && (
          <div>
            Last turn used{" "}
            <span className="tabular-nums">
              {fmtTok(last.tokens.uncached)} new
            </span>
            ,{" "}
            <span className="tabular-nums">
              {fmtTok(last.tokens.cache_read)} reused
            </span>{" "}
            and{" "}
            <span className="tabular-nums">{fmtTok(last.tokens.output)}</span>{" "}
            written.
          </div>
        )}
      </div>
    </div>
  );
}

/** The last assistant turn that carries a metrics stamp. */
function lastAssistantWithMetrics(
  messages: ChatMessage[],
): ChatMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "assistant" && m.metrics?.tokens_in != null) return m;
  }
  return undefined;
}

/** Slim used/window fill with the budget marker. Green under budget, amber
 * past 80% of it, red over — red means the consolidator is carrying dropped
 * history. Hover shows the exact numbers. Renders nothing without data. */
function DepthBar({
  used,
  windowTokens,
  budget,
}: {
  used: number | null;
  windowTokens: number | null;
  budget: number | null;
}) {
  if (used == null || !windowTokens) return null;
  const effective = budget ?? windowTokens;
  const tone =
    used > effective
      ? "bg-destructive"
      : used > 0.8 * effective
        ? "bg-warning"
        : "bg-success";
  const fill = Math.min(100, (used / windowTokens) * 100);
  const marker =
    budget != null ? Math.min(100, (budget / windowTokens) * 100) : null;
  const title = `${fmtTok(used)} / ${fmtTok(windowTokens)}${
    budget != null ? ` · budget ${fmtTok(budget)}` : ""
  }`;
  return (
    <div
      className="relative h-1 w-20 shrink-0 rounded-full bg-secondary"
      title={title}
    >
      <div
        className={cn("h-full rounded-full transition-all", tone)}
        style={{ width: `${fill}%` }}
      />
      {marker != null && (
        <div
          className="absolute -inset-y-0.5 w-px bg-foreground/60"
          style={{ left: `${marker}%` }}
        />
      )}
    </div>
  );
}

/** Context settings: the per-thread budget slider (debounced PUT) and the
 * model's window. */
function ContextSettings({
  windowTokens,
  budget,
  info,
  onSave,
  onSaveAllowance,
}: {
  windowTokens: number | null;
  budget: number | null;
  info: ThreadContextInfo | null;
  onSave: (budget: number | null) => void;
  onSaveAllowance: (usd: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  // Local slider value; null = auto (server-derived). Resyncs from the prop
  // whenever the dialog opens or the server value changes underneath us.
  const [value, setValue] = useState<number | null>(budget);
  useEffect(() => {
    if (open) setValue(budget);
  }, [open, budget]);

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queueSave = useCallback(
    (v: number | null) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => onSave(v), 400);
    },
    [onSave],
  );
  useEffect(
    () => () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    },
    [],
  );

  // Server default is 2048 (DEFAULT_HISTORY_CAP_TOKENS); the dial
  // spans from the 1k floor to the full window.
  const min = 1_000;
  const max = Math.max(min, windowTokens ?? 200_000);
  // Unset = server default (2048), not a fraction of the window.
  const shown = value ?? 2_048;

  return (
    <Dialog onOpenChange={setOpen} open={open}>
      <DialogTrigger asChild>
        <button
          aria-label="Context settings"
          className="rounded-full border border-border p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          title="Context settings — history budget"
          type="button"
        >
          <SlidersHorizontalIcon className="size-3.5" />
        </button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Context settings</DialogTitle>
          <DialogDescription>
            How much of the model&apos;s window this thread may spend. Over
            budget, the consolidator carries older history as a rolling
            summary.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-1.5">
          <div className="flex items-baseline justify-between text-xs">
            <span className="text-muted-foreground">History budget</span>
            <span className="text-foreground tabular-nums">
              {value == null ? "auto" : fmtTok(value)}
            </span>
          </div>
          <input
            aria-label="History budget in tokens"
            className="w-full accent-primary"
            max={max}
            min={min}
            onChange={(e) => {
              const v = Number(e.target.value);
              setValue(v);
              queueSave(v);
            }}
            step={1000}
            type="range"
            value={shown}
          />
          <div className="flex justify-between text-[10px] text-muted-foreground">
            <span>{fmtTok(min)}</span>
            <span>{windowTokens ? `${fmtTok(max)} window` : "window unknown"}</span>
          </div>
          {value != null && (
            <button
              className="justify-self-start text-muted-foreground text-xs underline-offset-2 hover:text-foreground hover:underline"
              onClick={() => {
                setValue(null);
                queueSave(null);
              }}
              type="button"
            >
              Reset to auto (server-derived)
            </button>
          )}
        </div>
        <TurnBudget info={info} onSave={onSaveAllowance} />
        <div className="border-border border-t pt-3 text-muted-foreground text-xs">
          <div>
            Model window:{" "}
            <span className="text-foreground tabular-nums">
              {windowTokens ? fmtTok(windowTokens) : "unknown"}
            </span>
          </div>
          {info && (info.summary_version != null || info.consolidated_at) && (
            <div className="mt-1">
              Consolidator: summary v{info.summary_version ?? "—"}
              {info.consolidated_at &&
                ` · ${new Date(info.consolidated_at).toLocaleString()}`}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** What fires with the next message: block composition, the history window,
 * the rolling summary, and the memories prefired on the last turn. */
function ContextPanel({ info }: { info: ThreadContextInfo | null }) {
  if (!info) {
    return (
      <p className="text-muted-foreground text-sm">
        This host doesn&apos;t expose thread context yet — the depth bar still
        tracks each turn&apos;s tokens.
      </p>
    );
  }
  // The two sections below describe the prompt THIS thread builds. When there
  // is no such build, one plain sentence replaces both — an empty list alone
  // reads as a bug, and for a peer thread it would be a bug report about
  // something working correctly.
  const noBuildNote =
    info.prefire_state === "not_built_here"
      ? "This is a peer thread. Your host does not build a prompt here, so there is nothing to show."
      : info.prefire_state === "no_build_yet"
        ? "Nothing assembled yet for this thread. It fills in after the first turn."
        : null;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 text-sm wrap-anywhere">
      {noBuildNote && (
        <p className="text-muted-foreground text-sm">{noBuildNote}</p>
      )}
      {!noBuildNote && info.blocks.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
            Prompt blocks
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)] gap-0.5">
            {info.blocks.map((b, i) => (
              <div
                className="flex items-baseline justify-between gap-4 text-xs"
                key={`${b.name}-${i}`}
              >
                <span className="min-w-0 truncate text-muted-foreground">{b.name}</span>
                <span className="shrink-0 text-foreground tabular-nums">
                  {fmtTok(b.chars)} chars
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      {info.thread_state && (
        <div>
          <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
            Where this thread stands
          </div>
          <p className="whitespace-pre-wrap rounded-md border border-border bg-muted/30 px-3 py-2 text-muted-foreground text-xs">
            {info.thread_state}
          </p>
        </div>
      )}
      {info.summary_excerpt && (
        <div>
          <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
            Rolling summary
            {info.summary_version != null && ` · v${info.summary_version}`}
          </div>
          <p className="whitespace-pre-wrap rounded-md border border-border bg-muted/30 px-3 py-2 text-muted-foreground text-xs">
            {info.summary_excerpt}
          </p>
        </div>
      )}
      {info.history_preview.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
            History window
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)] gap-1">
            {info.history_preview.map((h, i) => (
              <div className="text-xs" key={i}>
                <span className="text-muted-foreground">{h.role}: </span>
                <span className="text-foreground">{h.excerpt}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {!noBuildNote && info.last_prefired.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
            Memories prefired last turn
          </div>
          <div className="flex flex-wrap gap-1.5">
            {info.last_prefired.map((m, i) => (
              <span
                className="inline-flex max-w-full items-center rounded-md bg-muted px-2 py-0.5 text-muted-foreground text-xs"
                key={`${i}-${m.slice(0, 24)}`}
              >
                <span className="min-w-0 truncate">{m}</span>
              </span>
            ))}
          </div>
        </div>
      )}
      {!noBuildNote &&
        info.blocks.length === 0 &&
        info.history_preview.length === 0 &&
        !info.summary_excerpt &&
        !info.thread_state &&
        info.last_prefired.length === 0 && (
          <p className="text-muted-foreground text-sm">
            Nothing assembled yet — send a message first.
          </p>
        )}
    </div>
  );
}

export function ConversationDepth({
  projectId,
  frame,
  messages,
}: {
  /** null = the General bench thread. */
  projectId: number | null;
  /** The latest `2:[{t:"context"}]` frame, when the backend stamps one. */
  frame: ContextFrame | null;
  messages: ChatMessage[];
}) {
  const [info, setInfo] = useState<ThreadContextInfo | null>(null);
  const [catalog, setCatalog] = useState<CatalogModel[] | null>(null);
  const [lit, setLit] = useState(false);
  const [viewOpen, setViewOpen] = useState(false);
  // The consolidator stamps the user has SEEN (last GET / panel open). A
  // frame whose stamps moved past these lights the View-context button.
  const seenRef = useRef<{
    summary_version: number | null;
    consolidated_at: string | null;
  }>({ summary_version: null, consolidated_at: null });
  // The user's last slider move, until a frame/GET confirms it server-side.
  // undefined = untouched; null = reset to auto.
  const [localBudget, setLocalBudget] = useState<number | null | undefined>(
    undefined,
  );

  const adopt = useCallback((d: ThreadContextInfo) => {
    setInfo(d);
    seenRef.current = {
      summary_version: d.summary_version,
      consolidated_at: d.consolidated_at,
    };
  }, []);

  // Baseline per thread. 404 (older backend) → info stays null; the bar runs
  // on stream frames + turn metrics instead.
  useEffect(() => {
    let live = true;
    setInfo(null);
    setLit(false);
    setLocalBudget(undefined);
    seenRef.current = { summary_version: null, consolidated_at: null };
    void fetchThreadContext(projectId).then((r) => {
      if (live && r.ok) adopt(r.data);
    });
    return () => {
      live = false;
    };
  }, [projectId, adopt]);

  // The consolidator returned: next-turn context changed out-of-band → light.
  useEffect(() => {
    if (frame && contextChanged(frame, seenRef.current)) setLit(true);
  }, [frame]);

  const lastAsst = lastAssistantWithMetrics(messages);
  const used = frame?.tokens_in ?? lastAsst?.metrics?.tokens_in ?? null;
  const modelId = lastAsst?.metrics?.model ?? null;

  // Window: frame → GET → catalog (the model the last turn ran on). The
  // catalog is only fetched when nothing cheaper supplied a window.
  const windowKnown = frame?.window != null || info?.window != null;
  useEffect(() => {
    if (windowKnown || catalog !== null) return;
    let live = true;
    void fetchModelCatalog().then((r) => {
      if (live && r.ok) setCatalog(r.data);
    });
    return () => {
      live = false;
    };
  }, [windowKnown, catalog]);

  const windowTokens =
    frame?.window ??
    info?.window ??
    (modelId
      ? (catalog?.find((m) => m.id === modelId)?.contextLength ?? null)
      : null);
  const budget =
    frame?.budget ?? (localBudget !== undefined ? localBudget : null) ??
    info?.budget ??
    null;

  const saveBudget = useCallback(
    (b: number | null) => {
      setLocalBudget(b); // optimistic; the next frame/GET is the truth
      setInfo((cur) => (cur ? { ...cur, budget: b } : cur));
      void putThreadContextBudget(projectId, b);
    },
    [projectId],
  );

  const saveAllowance = useCallback(
    (usd: number | null) => {
      // Optimistic, same as the depth dial: the next GET is the truth. The
      // basis can only be recomputed server-side (it depends on whether the
      // routed model publishes rates), so it is left alone until then.
      setInfo((cur) => (cur ? { ...cur, turn_allowance_usd: usd } : cur));
      void putThreadTurnAllowance(projectId, usd).then(() => {
        void fetchThreadContext(projectId).then((r) => {
          if (r.ok) adopt(r.data);
        });
      });
    },
    [projectId, adopt],
  );

  // Opening the panel refetches (freshest composition), marks the
  // consolidator stamps seen, and clears the light.
  const onViewOpen = useCallback(
    (open: boolean) => {
      setViewOpen(open);
      if (!open) return;
      setLit(false);
      void fetchThreadContext(projectId).then((r) => {
        if (r.ok) adopt(r.data);
      });
    },
    [projectId, adopt],
  );

  // Nothing to say yet (no turn, no route): stay out of the header entirely.
  if (used == null && windowTokens == null && info == null) return null;

  return (
    <div className="flex items-center gap-2">
      <DepthBar budget={budget} used={used} windowTokens={windowTokens} />
      <ContextSettings
        budget={budget}
        info={info}
        onSave={saveBudget}
        onSaveAllowance={saveAllowance}
        windowTokens={windowTokens}
      />
      <Dialog onOpenChange={onViewOpen} open={viewOpen}>
        <DialogTrigger asChild>
          <button
            aria-label="View next-turn context"
            className={cn(
              "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors hover:bg-accent",
              lit
                ? "border-warning/60 text-warning"
                : "border-border text-muted-foreground",
            )}
            title={
              lit
                ? "Context changed — the consolidator updated this thread's summary"
                : "What fires with your next message"
            }
            type="button"
          >
            <LayersIcon className="size-3.5" />
            <span>Context</span>
            {lit && (
              <span
                aria-hidden
                className="size-1.5 animate-pulse rounded-full bg-warning"
              />
            )}
          </button>
        </DialogTrigger>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Next-turn context</DialogTitle>
            <DialogDescription>
              Exactly what fires with your next message — blocks, history
              window, rolling summary, and prefired memories.
            </DialogDescription>
          </DialogHeader>
          <ContextPanel info={info} />
        </DialogContent>
      </Dialog>
    </div>
  );
}
