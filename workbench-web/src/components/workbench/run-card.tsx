import { blockedNotice, useVersionDoc, type VersionSrc } from "@/components/workbench/version-view";
import { useRunPatch } from "@/hooks/use-run-feed";
import { engineColor, podNow } from "@/lib/strands";
import {
  activityNote,
  argStr,
  clock,
  dur,
  durShort,
  editVerb,
  engineMono,
  engineName,
  groupSteps,
  groupTitle,
  isTableLine,
  nowLine,
  type PatchFile,
  type RunModel,
  shortPath,
  type Step,
  tableCells,
  tableOf,
  type ToolCat,
  type ToolStep,
  tokensLine,
  toolLabel,
  type EditStep,
} from "@/lib/run-steps";
import { cn } from "@/lib/utils";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CircleIcon,
  CodeIcon,
  CoinsIcon,
  FilePlusIcon,
  FileTextIcon,
  GlobeIcon,
  HourglassIcon,
  LayersIcon,
  ListChecksIcon,
  type LucideIcon,
  Maximize2Icon,
  MessageSquareIcon,
  OctagonXIcon,
  PencilIcon,
  RotateCwIcon,
  ScrollTextIcon,
  SearchIcon,
  ShieldCheckIcon,
  SparklesIcon,
  SquareArrowOutUpRightIcon,
  TerminalIcon,
  TriangleAlertIcon,
  WifiOffIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react";
import { type CSSProperties, Fragment, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * RunCard — what a coding run looks like in the conversation (harness-feed
 * redesign, workbench 0.24.0). The
 * frames are folded into steps (lib/run-steps.ts): one row per command, read
 * or write with its time and result, the engine's messages as prose, thinking
 * as one timed row, bursts of three or more calls folded into one row. A
 * "now" line says what the engine is doing this second.
 *
 * When the run lands, its VERSION loads in the card — the page the per-run
 * preview route serves, sealed exactly as "open version" seals it (the shared
 * useVersionDoc) — so scrolling the conversation shows each version as it
 * looks, without clicking open version first; open it once it is the one
 * you want.
 * The frame takes no pointer or wheel events, so the page scrolls past it.
 */

export type RunMeta = {
  model?: string;
  seeded?: boolean;
  projectId?: number;
  prompt?: string;
  hasEntry?: boolean;
  module?: string | null;
};

const CAT_ICON: Record<ToolCat, LucideIcon> = {
  cmd: TerminalIcon,
  read: FileTextIcon,
  search: SearchIcon,
  edit: PencilIcon,
  web: GlobeIcon,
  plan: ListChecksIcon,
  other: WrenchIcon,
};

const isWrite = (change: string) => /write|create|add/i.test(change);

/** A ticking clock while the run is live: the device clock in seconds, the
 * same basis the chat's working line uses (chat-pane.tsx WorkingLine). The
 * card turns it to the pod's clock when it is given a skew (the book). */
export function useNow(live: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [live]);
  return live ? now : Date.now() / 1000;
}

export function PathText({ path, className }: { path: string; className?: string }) {
  const s = shortPath(path);
  const i = s.lastIndexOf("/");
  if (i < 0) return <b className={cn("font-semibold", className)}>{s}</b>;
  let dir = s.slice(0, i + 1);
  if (dir.length > 36) dir = `…/${dir.split("/").filter(Boolean).slice(-1).join("/")}/`;
  return (
    <span className={className} title={s}>
      <span className="text-muted-foreground/70">{dir}</span>
      <b className="font-semibold">{s.slice(i + 1)}</b>
    </span>
  );
}

// ── the engine's prose: a small markdown subset as React elements ──────────
// Never HTML: this is model-written text inside the user's own Host turn.
function inline(t: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+)\*\*/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = re.exec(t))) {
    if (m.index > last) out.push(t.slice(last, m.index));
    const k = `${key}-${n++}`;
    if (m[1] != null) {
      out.push(
        <span className="cursor-help text-link underline decoration-dotted underline-offset-[3px]" key={k} title={m[2]}>
          {m[1]}
        </span>,
      );
    } else if (m[3] != null) {
      out.push(
        <code className="rounded-[5px] border border-border bg-secondary px-1 font-mono text-[12px] text-foreground" key={k}>
          {m[3]}
        </code>,
      );
    } else {
      out.push(<strong key={k}>{m[4]}</strong>);
    }
    last = m.index + m[0].length;
  }
  if (last < t.length) out.push(t.slice(last));
  return out;
}

export function MiniMarkdown({ text, caret }: { text: string; caret?: boolean }) {
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let table: string[][] | null = null;
  const flushT = () => {
    if (!table) return;
    const k = `t${blocks.length}`;
    const { head, body } = tableOf(table);
    blocks.push(
      <div className="my-1 max-w-full overflow-x-auto" key={k}>
        <table className="md-table" data-md-table>
          {head && (
            <thead>
              <tr>
                {head.map((c, i) => (
                  <th key={`${k}-h${i}`}>{inline(c, `${k}-h${i}`)}</th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {body.map((r, ri) => (
              <tr key={`${k}-r${ri}`}>
                {r.map((c, ci) => (
                  <td key={`${k}-r${ri}c${ci}`}>{inline(c, `${k}-r${ri}c${ci}`)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
    table = null;
  };
  const flushP = () => {
    if (para.length) {
      const k = `p${blocks.length}`;
      blocks.push(<p className="mb-1.5 last:mb-0" key={k}>{inline(para.join(" "), k)}</p>);
      para = [];
    }
  };
  const flushL = () => {
    if (list) {
      const k = `l${blocks.length}`;
      const items = list.items.map((it, i) => <li className="my-0.5" key={`${k}-${i}`}>{inline(it, `${k}-${i}`)}</li>);
      blocks.push(
        list.ordered ? (
          <ol className="mb-2 list-decimal pl-5" key={k}>{items}</ol>
        ) : (
          <ul className="mb-2 list-disc pl-5" key={k}>{items}</ul>
        ),
      );
      list = null;
    }
  };
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const l = raw.trimEnd();
    let m: RegExpExecArray | null;
    if (isTableLine(l)) {
      flushP();
      flushL();
      (table ??= []).push(tableCells(l));
      continue;
    }
    flushT();
    if (!l.trim()) { flushP(); flushL(); continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) {
      flushP(); flushL();
      const k = `h${blocks.length}`;
      blocks.push(<div className="mt-2.5 mb-1 font-semibold text-foreground first:mt-0" key={k}>{inline(m[2], k)}</div>);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(l.trim())) { flushP(); flushL(); blocks.push(<hr className="my-2.5 border-border" key={`r${blocks.length}`} />); continue; }
    if ((m = /^\s*[-*•]\s+(.*)$/.exec(l))) {
      flushP();
      if (!list || list.ordered) { flushL(); list = { ordered: false, items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(l))) {
      flushP();
      if (!list || !list.ordered) { flushL(); list = { ordered: true, items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if (list && /^\s{2,}\S/.test(raw)) { list.items[list.items.length - 1] += ` ${l.trim()}`; continue; }
    flushL();
    para.push(l.trim());
  }
  flushP();
  flushL();
  flushT();
  return (
    <>
      {blocks}
      {caret && <span className="ml-0.5 inline-block h-3.5 w-[7px] animate-run-caret rounded-[1px] bg-link align-[-2px]" />}
    </>
  );
}

// ── small pieces ───────────────────────────────────────────────────────────
function Pill({ tone, icon: Icon, pulse, children }: { tone: "live" | "ok" | "bad" | "warn" | "idle"; icon?: LucideIcon; pulse?: boolean; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex h-[22px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 font-medium font-sans text-[11.5px]",
        tone === "live" && "border-link/30 bg-link/10 text-link",
        tone === "ok" && "border-success/30 bg-success/10 text-success",
        tone === "bad" && "border-destructive/40 bg-destructive/10 text-destructive",
        tone === "warn" && "border-warning/35 bg-warning/10 text-warning",
        tone === "idle" && "border-border bg-muted-foreground/5 text-muted-foreground",
      )}
    >
      {pulse && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
      {Icon && <Icon className="size-3" />}
      {children}
    </span>
  );
}

function Spinner() {
  return <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-link/20 border-t-link" />;
}

function StatusMark({ s, live }: { s: { status: string }; live: boolean }) {
  if (s.status === "run" && live) return <Spinner />;
  if (s.status === "err") return <XIcon className="size-[13px] text-destructive" />;
  if (s.status === "ok") return <CheckIcon className="size-[13px] text-success" />;
  return <CircleIcon aria-label="no result recorded" className="size-2 text-muted-foreground/60" />;
}

function stepTime(s: { status: string; t0: number; t1: number | null }, live: boolean, now: number): string {
  if (s.status === "run" && live) return durShort(now - s.t0);
  if (s.t1 != null && s.t1 - s.t0 >= 0.95) return durShort(s.t1 - s.t0);
  return "";
}

/** The whole run at a glance: blue = a command or tool call, orange = a file
 * written, grey = thinking. */
function ActivityStrip({ M, now }: { M: RunModel; now: number }) {
  const t0 = M.start ?? now;
  const end = M.live ? Math.max(now, t0 + 30) : Math.max(M.lastT ?? t0 + 1, t0 + 1);
  const span = Math.max(1, end - t0);
  const X = (t: number) => ((t - t0) / span) * 1000;
  const marks: ReactNode[] = [];
  if (M.start != null) {
    marks.push(<rect height="18" key="run" style={{ fill: "color-mix(in srgb, var(--primary) 10%, transparent)" }} width={Math.max(0, X(M.live ? now : (M.lastT ?? t0)) - X(M.start))} x={X(M.start)} y="0" />);
  }
  for (const s of M.steps) {
    if (s.type === "think") marks.push(<rect height="6" key={s.id} style={{ fill: "color-mix(in srgb, var(--muted-foreground) 32%, transparent)" }} rx="2" width={Math.max(2, X(s.t1 ?? now) - X(s.t0))} x={X(s.t0)} y="6" />);
  }
  for (const s of M.steps) {
    const isEd = s.type === "edit" || (s.type === "tool" && s.cat === "edit");
    if (s.type === "tool" && !isEd) marks.push(<rect fill={s.status === "err" ? "var(--destructive)" : "var(--tool)"} height="12" key={s.id} width="2.4" x={X(s.t0) - 1.2} y="3" />);
    else if (isEd) marks.push(<rect fill="var(--edit)" height="18" key={s.id} width="4" x={X(s.t0) - 2} y="0" />);
    else if (s.type === "error") marks.push(<rect fill="var(--destructive)" height="18" key={s.id} width="4" x={X(s.t0) - 2} y="0" />);
  }
  if (M.live && M.start != null) marks.push(<rect fill="var(--link)" height="18" key="now" width="1.6" x={X(now) - 0.8} y="0" />);
  return (
    <div className="h-[22px] pr-3.5 pl-[60px]">
      <svg
        aria-label="Activity over time: blue is a command or tool call, orange is a file written, grey is thinking"
        className="block h-[18px] w-full rounded bg-muted/70 shadow-[inset_0_0_0_1px_var(--border)]"
        preserveAspectRatio="none"
        role="img"
        viewBox="0 0 1000 18"
      >
        {marks}
      </svg>
    </div>
  );
}

function Stats({ M, now }: { M: RunModel; now: number }) {
  const c = M.counts;
  const items: [LucideIcon, string, number, string, string][] = [];
  if (c.cmd) items.push([TerminalIcon, "text-tool", c.cmd, c.cmd === 1 ? "command" : "commands", "commands run"]);
  if (M.reads.size) items.push([FileTextIcon, "text-tool", M.reads.size, "read", "files read"]);
  if (c.search) items.push([SearchIcon, "text-tool", c.search, c.search === 1 ? "search" : "searches", "searches"]);
  if (c.web) items.push([GlobeIcon, "text-tool", c.web, "web", "web lookups"]);
  const other = (c.other ?? 0) + (c.plan ?? 0);
  if (other) items.push([WrenchIcon, "text-tool", other, "other", "other tool calls"]);
  if (M.written.size) items.push([PencilIcon, "text-edit", M.written.size, "written", "files the engine reported writing"]);
  if (M.msgs) items.push([MessageSquareIcon, "", M.msgs, M.msgs === 1 ? "message" : "messages", "messages from the engine"]);
  if (M.errors) items.push([TriangleAlertIcon, "text-destructive", M.errors, "failed", "tool calls that returned an error"]);
  if (M.retries) items.push([RotateCwIcon, "text-destructive", M.retries, M.retries === 1 ? "retry" : "retries", "retries"]);
  return (
    <div className="flex flex-wrap gap-3.5 pt-1 pr-3.5 pb-2.5 pl-[60px] text-[11.5px] text-muted-foreground">
      {items.length === 0 ? (
        <span className={cn(M.live && M.think && "run-shimmer animate-run-shimmer")}>{activityNote(M, now)}</span>
      ) : (
        items.map(([Icon, cls, n, word, title]) => (
          <span className="inline-flex items-center gap-1.5 tabular-nums" key={title} title={title}>
            <Icon className={cn("size-3", cls)} />
            <b className="font-semibold text-foreground">{n}</b> {word}
          </span>
        ))
      )}
    </div>
  );
}

function OutputBlock({ s, all, onAll }: { s: ToolStep | EditStep; all: boolean; onAll: () => void }) {
  const L = s.type === "tool" ? toolLabel(s, false) : null;
  const raw = s.result == null ? "" : typeof s.result === "string" ? s.result : JSON.stringify(s.result, null, 2);
  const lines = raw.replace(/\r\n?/g, "\n").replace(/\n+$/, "").split("\n");
  const total = raw ? lines.length : 0;
  const shown = all ? lines : lines.slice(0, 24);
  const content = argStr(s.args, ["content", "new_string", "CodeContent", "patch", "input"]);
  const isEdit = s.type === "edit" || (s.type === "tool" && s.cat === "edit");
  return (
    <div className={cn("my-1.5 overflow-hidden rounded-[9px] border border-border bg-background")}>
      <pre className={cn("max-h-[340px] overflow-auto whitespace-pre-wrap break-words px-[11px] py-[9px] font-mono text-[11.5px] leading-relaxed", s.status === "err" ? "text-destructive" : "text-muted-foreground")}>
        {L?.cmd &&
          L.cmd.split("\n").map((l, i) => (
            <Fragment key={`c${i}`}>
              <span className="text-link">
                <span className="text-muted-foreground/70">{i ? " " : "$"}</span> {l}
              </span>
              {"\n"}
            </Fragment>
          ))}
        {isEdit && content && `${content.split("\n").slice(0, all ? 400 : 24).join("\n")}${raw ? "\n\n" : ""}`}
        {raw ? shown.join("\n") : !L?.cmd && !content ? <span className="text-link">no output</span> : null}
      </pre>
      <div className="flex items-center justify-between gap-2 border-border border-t bg-muted/60 px-[11px] py-1 text-[11px] text-muted-foreground/70">
        <span>
          {s.type === "tool" && s.exit != null ? `exit ${s.exit} · ` : ""}
          {total ? `${total} line${total === 1 ? "" : "s"}` : ""}
          {s.t1 != null && s.t1 - s.t0 >= 0.05 ? ` · ${durShort(s.t1 - s.t0)}` : ""}
        </span>
        {total > 24 && (
          <button className="text-link underline decoration-link/35 underline-offset-[3px] hover:decoration-link" onClick={onAll} type="button">
            {all ? "Show less" : `Show all ${total} lines`}
          </button>
        )}
      </div>
    </div>
  );
}

function Rail({ icon: Icon, tone, small }: { icon: LucideIcon; tone?: "tool" | "edit" | "bad" | "warn" | "say"; small?: boolean }) {
  return (
    <div
      className={cn(
        "relative z-[1] grid shrink-0 place-items-center border",
        small ? "size-5 rounded-md border-transparent" : "size-[26px] rounded-lg border-border bg-card",
        tone === "tool" && "text-tool",
        tone === "edit" && "border-edit/35 text-edit",
        tone === "bad" && "border-destructive/45 text-destructive",
        tone === "warn" && "text-warning",
        tone === "say" && "text-foreground",
        !tone && "text-muted-foreground",
        small && "border-transparent",
      )}
    >
      <Icon className={small ? "size-3" : "size-[13px]"} />
    </div>
  );
}

type StepUi = {
  isOpen: (id: string) => boolean;
  toggle: (id: string) => void;
  all: Set<string>;
  toggleAll: (id: string) => void;
};

function StepRow({ s, M, now, ui, small, last, engine }: { s: Step; M: RunModel; now: number; ui: StepUi; small?: boolean; last?: boolean; engine: string }) {
  const grid = cn("grid py-[3px]", small ? "grid-cols-[20px_minmax(0,1fr)] gap-x-2 py-px" : "grid-cols-[26px_minmax(0,1fr)] gap-x-2.5");
  const row = cn("-ml-2 flex min-w-0 items-center gap-2 rounded-[7px] px-2", small ? "min-h-[22px] text-[12.5px]" : "min-h-[26px]");
  const open = ui.isOpen(s.id);
  const chev = <ChevronRightIcon className={cn("size-[13px] text-muted-foreground/70 transition-transform", open && "rotate-90")} />;
  switch (s.type) {
    case "say":
      return (
        <div className={grid}>
          <Rail icon={MessageSquareIcon} small={small} tone="say" />
          <div className="max-w-[72ch] pt-[3px] pb-0.5 text-[13.5px] text-foreground leading-[1.62]">
            <MiniMarkdown caret={M.live && last && s.delta && now - s.t1 < 3} text={s.text} />
          </div>
        </div>
      );
    case "think": {
      const live = s.t1 == null && M.live;
      const has = s.text.trim().length > 0;
      return (
        <div className={grid}>
          <Rail icon={SparklesIcon} small={small} />
          <div className="min-w-0">
            {has ? (
              <button className={cn(row, "w-full cursor-pointer text-left hover:bg-secondary/75")} onClick={() => ui.toggle(s.id)} type="button">
                <ThinkLabel live={live} now={now} s={s} />
                <span className="ml-auto">{chev}</span>
              </button>
            ) : (
              <div className={row}>
                <ThinkLabel live={live} now={now} s={s} />
              </div>
            )}
            {has && open && (
              <div className="my-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-r-lg border-border border-l-2 bg-muted/50 px-3 py-2 text-muted-foreground text-[12.5px] leading-relaxed">
                {s.text.trim()}
              </div>
            )}
          </div>
        </div>
      );
    }
    case "tool":
    case "edit": {
      let Icon: LucideIcon;
      let tone: "tool" | "edit";
      let verb: string;
      let subject: ReactNode;
      let extra: string | undefined;
      if (s.type === "edit") {
        Icon = isWrite(s.change) ? FilePlusIcon : PencilIcon;
        tone = "edit";
        verb = editVerb(s, M.live);
        subject = <PathText path={s.path} />;
      } else {
        const L = toolLabel(s, M.live);
        Icon = s.cat === "edit" ? (isWrite(s.change ?? s.tool) ? FilePlusIcon : PencilIcon) : CAT_ICON[s.cat];
        tone = s.cat === "edit" ? "edit" : "tool";
        verb = L.verb;
        extra = L.extra;
        subject =
          L.kind === "code" ? <code className="font-mono text-[12px] text-foreground">{L.subject}</code> : L.kind === "path" ? <PathText path={L.subject} /> : L.subject;
      }
      const canOpen = s.type === "tool" || Boolean(argStr(s.args, ["content", "new_string", "CodeContent"]));
      const body = (
        <>
          <span className="shrink-0 text-muted-foreground">{verb}</span>
          <span className="min-w-0 truncate text-foreground">
            {subject}
            {extra && <span className="text-muted-foreground/70"> · {extra}</span>}
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-2 font-mono text-[11px] text-muted-foreground/70 tabular-nums">
            {stepTime(s, M.live, now)}
            <StatusMark live={M.live} s={s} />
            {canOpen && chev}
          </span>
        </>
      );
      return (
        <div className={grid}>
          <Rail icon={Icon} small={small} tone={s.status === "err" ? "bad" : tone} />
          <div className="min-w-0">
            {canOpen ? (
              <button className={cn(row, "w-full cursor-pointer text-left hover:bg-secondary/75")} onClick={() => ui.toggle(s.id)} type="button">
                {body}
              </button>
            ) : (
              <div className={row}>{body}</div>
            )}
            {canOpen && open && <OutputBlock all={ui.all.has(s.id)} onAll={() => ui.toggleAll(s.id)} s={s} />}
          </div>
        </div>
      );
    }
    case "error":
      return (
        <div className={grid}>
          <Rail icon={TriangleAlertIcon} small={small} tone="bad" />
          <Callout tone="bad">{s.text}</Callout>
        </div>
      );
    case "retry":
      return (
        <div className={grid}>
          <Rail icon={RotateCwIcon} small={small} tone="warn" />
          <Callout tone="warn">
            Retrying, attempt {s.attempt}
            {s.reason ? `: ${s.reason}` : ""}
          </Callout>
        </div>
      );
    case "killed":
      return (
        <div className={grid}>
          <Rail icon={OctagonXIcon} small={small} />
          <Callout tone="idle">Stopped by {s.by === "host" ? "your Host" : "you"}.</Callout>
        </div>
      );
    case "lost":
      return (
        <div className={grid}>
          <Rail icon={WifiOffIcon} small={small} tone="warn" />
          <Callout tone="warn">Live feed lost. The run is still going, and its output lands here when it finishes.</Callout>
        </div>
      );
    case "report": {
      const all = ui.all.has(s.id);
      return (
        <div className={grid}>
          <Rail icon={ScrollTextIcon} small={small} tone="say" />
          <div className="my-1 overflow-hidden rounded-[10px] border border-border bg-muted">
            <div className="flex items-center justify-between border-border border-b px-[11px] py-[7px] text-[11.5px] text-muted-foreground">
              <span>Closing report from {engine}</span>
              <button className="text-link underline decoration-link/35 underline-offset-[3px]" onClick={() => ui.toggleAll(s.id)} type="button">
                {all ? "Show less" : "Show all"}
              </button>
            </div>
            <div className={cn("relative px-3 py-2.5 text-[13px] text-foreground leading-[1.62]", !all && "max-h-[200px] overflow-hidden after:absolute after:inset-x-0 after:bottom-0 after:h-12 after:bg-gradient-to-b after:from-transparent after:to-muted")}>
              <MiniMarkdown text={s.text} />
            </div>
          </div>
        </div>
      );
    }
    default:
      return (
        <div className={grid}>
          <Rail icon={CircleIcon} small={small} />
          <div className="break-words pt-[5px] font-mono text-[11.5px] text-muted-foreground/70">{s.text}</div>
        </div>
      );
  }
}

function ThinkLabel({ s, live, now }: { s: Extract<Step, { type: "think" }>; live: boolean; now: number }) {
  if (live) {
    return (
      <span className="text-muted-foreground">
        <span className="run-shimmer animate-run-shimmer">Thinking</span>{" "}
        <span className="font-mono text-[11px] text-muted-foreground/70">
          {clock(now - s.t0)}
          {s.beats ? ` · ${s.beats} heartbeat${s.beats === 1 ? "" : "s"}` : ""}
        </span>
      </span>
    );
  }
  const d = (s.t1 ?? s.t0) - s.t0;
  return <span className="text-muted-foreground">{d >= 1 ? `Thought for ${dur(d)}` : "Thought briefly"}</span>;
}

function Callout({ tone, icon: Icon, children }: { tone: "bad" | "warn" | "ok" | "idle"; icon?: LucideIcon; children: ReactNode }) {
  return (
    <div
      className={cn(
        "my-0.5 flex items-start gap-2.5 rounded-[9px] border px-[11px] py-2 text-[12.5px] leading-normal",
        tone === "bad" && "border-destructive/35 bg-destructive/[.08] text-destructive",
        tone === "warn" && "border-warning/30 bg-warning/[.07] text-warning",
        tone === "ok" && "border-success/30 bg-success/[.06] text-success",
        tone === "idle" && "border-border bg-muted/60 text-muted-foreground",
      )}
    >
      {Icon && <Icon className="mt-0.5 size-3.5 shrink-0" />}
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function GroupRow({ g, M, now, ui, engine }: { g: { id: string; steps: ToolStep[] }; M: RunModel; now: number; ui: StepUi; engine: string }) {
  const open = ui.isOpen(g.id);
  const running = M.live && g.steps.some((s) => s.status === "run");
  const errs = g.steps.filter((s) => s.status === "err").length;
  const cats = [...new Set(g.steps.map((s) => s.cat))].slice(0, 3);
  const t0 = g.steps[0].t0;
  const t1 = g.steps[g.steps.length - 1].t1;
  const time = running ? durShort(now - t0) : t1 != null && t1 - t0 >= 1 ? durShort(t1 - t0) : "";
  return (
    <div className="grid grid-cols-[26px_minmax(0,1fr)] gap-x-2.5 py-[3px]">
      <Rail icon={LayersIcon} tone="tool" />
      <div className="min-w-0">
        <button className="-ml-2 flex min-h-[26px] w-full min-w-0 cursor-pointer items-center gap-2 rounded-[7px] px-2 text-left hover:bg-secondary/75" onClick={() => ui.toggle(g.id)} type="button">
          <span className="inline-flex shrink-0 gap-[3px] text-tool">
            {cats.map((c) => {
              const I = CAT_ICON[c];
              return <I className="size-3" key={c} />;
            })}
          </span>
          <span className={cn("min-w-0 truncate", running && "run-shimmer animate-run-shimmer")}>{groupTitle(g.steps)}</span>
          <span className="ml-auto flex shrink-0 items-center gap-2 font-mono text-[11px] text-muted-foreground/70 tabular-nums">
            {time}
            {running ? <Spinner /> : errs ? <span className="font-sans text-destructive">{errs} failed</span> : <CheckIcon className="size-[13px] text-success" />}
            <ChevronRightIcon className={cn("size-[13px] transition-transform", open && "rotate-90")} />
          </span>
        </button>
        {open && (
          <div className="my-0.5">
            {g.steps.map((s) => (
              <StepRow engine={engine} key={s.id} M={M} now={now} s={s} small ui={ui} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Now({ M, now, engine }: { M: RunModel; now: number; engine: string }) {
  const n = nowLine(M, now);
  if (!n) return null;
  let icon: ReactNode = <Spinner />;
  let warn = false;
  let text: ReactNode = null;
  let sub: ReactNode = null;
  switch (n.kind) {
    case "waiting":
      warn = true;
      icon = <HourglassIcon className="size-3.5 shrink-0 text-warning" />;
      text = "Waiting for memory";
      sub = n.since != null ? clock(now - n.since) : null;
      break;
    case "starting":
      icon = <HourglassIcon className="size-3.5 shrink-0 text-link" />;
      text = `Starting ${engine}`;
      break;
    case "lost":
      warn = true;
      icon = <WifiOffIcon className="size-3.5 shrink-0 text-warning" />;
      text = "Live feed lost. The run is still going";
      break;
    case "thinking":
      text = <span className="run-shimmer animate-run-shimmer">Thinking</span>;
      sub = `${clock(now - n.since)}${n.beats ? ` · ${n.beats} heartbeat${n.beats === 1 ? "" : "s"}` : ""}`;
      break;
    case "step": {
      const s = n.step;
      if (s.type === "edit") text = <>Writing <PathText path={s.path} /></>;
      else {
        const L = toolLabel(s, true);
        text = (
          <>
            {L.verb}{" "}
            {L.kind === "code" ? <code className="font-mono text-[12px] text-foreground">{L.subject}</code> : L.kind === "path" ? <PathText path={L.subject} /> : L.subject}
          </>
        );
      }
      sub = durShort(now - n.since);
      break;
    }
    case "writing":
      text = <span className="run-shimmer animate-run-shimmer">Writing a message</span>;
      break;
    case "working":
      text = <span className="run-shimmer animate-run-shimmer">Working</span>;
      sub = n.lastAct != null ? `last activity ${durShort(now - n.lastAct)} ago` : null;
      break;
  }
  return (
    <div className={cn("flex min-w-0 flex-1 items-center gap-2 text-[12.5px]", warn && "text-warning")}>
      {icon}
      <span className="min-w-0 truncate">{text}</span>
      {sub && <span className="shrink-0 font-mono text-[11px] text-muted-foreground/70 tabular-nums">{sub}</span>}
    </div>
  );
}

// ── the landed version, inline ─────────────────────────────────────────────
/** The run's own page, loaded in the card. A 1280×800 viewport scaled to the
 * card's width — the layout the version has on a laptop — that takes no
 * pointer or wheel events, so the conversation scrolls past it; a click opens
 * the version window. */
export function RunPreview({ src, onOpen, entryHint }: { src: Extract<VersionSrc, { kind: "run" }>; onOpen: () => void; entryHint?: string }) {
  const [entry, setEntry] = useState<string | null>(null);
  const doc = useVersionDoc(src, entry);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(0.5);
  const [ready, setReady] = useState(false);
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const fit = () => el.clientWidth && setScale(el.clientWidth / 1280);
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [doc.html]);
  useEffect(() => setReady(false), [doc.html]);
  const page = entry ?? doc.shown ?? entryHint ?? "index.html";

  if (doc.notFound) {
    return <div className="mx-3.5 mb-3 pl-[46px] text-[12px] text-muted-foreground/70">This run left no page to preview. Open version shows what it changed.</div>;
  }
  return (
    <div className="mx-3.5 mb-3 animate-run-in overflow-hidden rounded-xl border border-border bg-background" data-run-preview>
      <div className="flex h-[30px] items-center gap-2 border-border border-b bg-muted px-2.5 text-[11.5px] text-muted-foreground">
        <span aria-hidden className="mr-1 inline-flex gap-[5px]">
          <i className="size-2 rounded-full bg-border" />
          <i className="size-2 rounded-full bg-border" />
          <i className="size-2 rounded-full bg-border" />
        </span>
        <span className="min-w-0 truncate font-mono text-muted-foreground">
          {page} · {src.label}
        </span>
        {doc.blocked.length > 0 ? (
          <span className="ml-auto inline-flex shrink-0 cursor-help items-center gap-1.5 whitespace-nowrap text-warning" data-blocked-refs title={blockedNotice(doc.blocked)}>
            <ShieldCheckIcon className="size-3" />
            sealed · {doc.blocked.length} external {doc.blocked.length === 1 ? "reference" : "references"} blocked
          </span>
        ) : (
          <span className="ml-auto inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-muted-foreground/70">
            <ShieldCheckIcon className="size-3" />
            sealed · no network
          </span>
        )}
      </div>
      {doc.pages && !entry && !doc.shown && !doc.html ? (
        <div className="px-3 py-3 text-[12.5px]" data-entry-choices>
          <p className="mb-2 text-muted-foreground">{doc.pages.detail} — pick one to preview it here.</p>
          <div className="flex flex-wrap gap-1.5">
            {doc.pages.entries.map((p) => (
              <button className="rounded-md border border-border px-2 py-1 font-mono text-[12px] hover:border-primary hover:bg-secondary" data-entry={p} key={p} onClick={() => setEntry(p)} type="button">
                {p}
              </button>
            ))}
          </div>
        </div>
      ) : doc.err ? (
        <div
          className={cn("px-3 py-3 text-[12px]", doc.ended ? "text-muted-foreground" : "font-mono text-destructive")}
          data-version-error={doc.ended ? "session" : "other"}
        >
          {doc.err}
        </div>
      ) : (
        <div
          className="group relative cursor-pointer overflow-hidden"
          onClick={onOpen}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen())}
          ref={boxRef}
          role="button"
          style={{ height: Math.round(800 * scale) }}
          tabIndex={0}
          title="Open this version"
        >
          {!ready && (
            <div className="absolute inset-0 flex items-center justify-center gap-2 text-[12px] text-muted-foreground/70">
              <Spinner />
              Loading the app
            </div>
          )}
          {doc.html != null && (
            <iframe
              className={cn("pointer-events-none absolute top-0 left-0 h-[800px] w-[1280px] origin-top-left border-0 bg-white transition-opacity duration-300", ready ? "opacity-100" : "opacity-0")}
              onLoad={() => setReady(true)}
              sandbox="allow-scripts"
              srcDoc={doc.html}
              style={{ transform: `scale(${scale})` }}
              tabIndex={-1}
              title={`${src.label} preview`}
            />
          )}
          <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-b from-transparent from-40% to-[color-mix(in_srgb,var(--background)_60%,transparent)] opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
            <span className="inline-flex h-7 items-center gap-1.5 rounded-lg bg-primary px-2.5 font-semibold text-[12px] text-primary-foreground shadow-[0_8px_24px_-6px_color-mix(in_srgb,var(--shade)_70%,transparent)]">
              <SquareArrowOutUpRightIcon className="size-[13px]" />
              Open version
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

function ChangedFiles({ files }: { files: PatchFile[] }) {
  const tA = files.reduce((a, f) => a + f.add, 0);
  const tD = files.reduce((a, f) => a + f.del, 0);
  const shown = files.slice(0, 8);
  return (
    <div>
      <h4 className="mb-1.5 font-semibold text-[11px] text-muted-foreground/70 uppercase tracking-[.07em]">
        Changed files
        <span className="ml-1.5 font-medium font-mono text-muted-foreground normal-case tracking-normal">
          {files.length} · +{tA} −{tD}
        </span>
      </h4>
      <div className="grid gap-0.5" data-changed-files>
        {shown.map((f) => {
          const Icon = f.status === "added" ? FilePlusIcon : f.status === "deleted" ? XIcon : PencilIcon;
          const t = f.add + f.del;
          const a = t ? Math.round((f.add / t) * 5) : 0;
          return (
            <div className="flex min-w-0 items-center gap-2 font-mono text-[12px]" key={f.path}>
              <Icon className={cn("size-[13px] shrink-0", f.status === "added" ? "text-success" : f.status === "deleted" ? "text-destructive" : "text-edit")} />
              <PathText className="min-w-0 flex-1 truncate" path={f.path} />
              <span className={cn("w-[52px] shrink-0 text-right text-[11px] text-success", !f.add && "opacity-35")}>+{f.add}</span>
              <span className={cn("w-[52px] shrink-0 text-right text-[11px] text-destructive", !f.del && "opacity-35")}>−{f.del}</span>
              <span aria-hidden className="ml-1.5 inline-flex shrink-0 gap-0.5">
                {Array.from({ length: 5 }, (_, k) => (
                  <i className={cn("size-[7px] rounded-[1.5px]", !t ? "bg-border" : k < a ? "bg-success" : "bg-destructive")} key={k} />
                ))}
              </span>
            </div>
          );
        })}
        {files.length > shown.length && <div className="font-mono text-[12px] text-muted-foreground/70">and {files.length - shown.length} more</div>}
      </div>
    </div>
  );
}

function Done({ M, patch, activity, onActivity, pane }: { M: RunModel; patch: PatchFile[] | null; activity: boolean; onActivity: () => void; pane?: boolean }) {
  // A killed run did not fail: someone stopped it. Never the error colour.
  const stopped = M.killedBy != null;
  const bad = !stopped && (M.state === "failed" || M.state === "timeout");
  const warn = !bad && !stopped && M.accept != null && !M.accept.ok;
  const verb = stopped
    ? `Stopped by ${M.killedBy === "host" ? "your Host" : "you"} after`
    : M.state === "timeout" ? "Timed out after" : M.state === "failed" ? "Failed after" : "Completed in";
  const el = M.elapsed ?? (M.lastT ?? 0) - (M.start ?? M.lastT ?? 0);
  const steps = M.steps.filter((s) => s.type === "tool" || s.type === "edit").length;
  const tok = tokensLine(M);
  return (
    <div className="grid gap-3 border-border border-t py-3 pr-3.5 pl-[60px]">
      <div className="-ml-[46px] flex items-center gap-2.5 font-semibold text-[14px]">
        <span
          className={cn(
            "grid size-[34px] shrink-0 place-items-center rounded-full",
            stopped ? "bg-muted text-muted-foreground shadow-[inset_0_0_0_1px_var(--border)]" : bad ? "bg-destructive/10 text-destructive shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--destructive)_40%,transparent)]" : warn ? "bg-warning/10 text-warning shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--warning)_35%,transparent)]" : "bg-success/10 text-success shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--success)_35%,transparent)]",
          )}
        >
          {stopped ? <OctagonXIcon className="size-[17px]" /> : bad ? <XIcon className="size-[17px]" /> : warn ? <TriangleAlertIcon className="size-[17px]" /> : <CheckIcon className="size-[17px]" />}
        </span>
        <span>
          {verb} {dur(el)}
        </span>
        <span className="font-normal text-[12.5px] text-muted-foreground">
          {M.exit != null ? `exit ${M.exit}` : ""}
          {M.retries ? ` · ${M.retries} ${M.retries === 1 ? "retry" : "retries"}` : ""}
        </span>
      </div>
      {stopped && <Callout tone="idle">What it wrote before the stop is kept.</Callout>}
      {bad && M.failure && (
        <Callout icon={TriangleAlertIcon} tone="bad">
          {M.failure}
        </Callout>
      )}
      {M.accept &&
        (M.accept.ok ? (
          <Callout icon={ShieldCheckIcon} tone="ok">
            {M.accept.checks} acceptance check{M.accept.checks === 1 ? "" : "s"} passed
          </Callout>
        ) : (
          <Callout icon={ShieldCheckIcon} tone="warn">
            {M.accept.failed.length} of {M.accept.checks || M.accept.failed.length} acceptance check{(M.accept.checks || M.accept.failed.length) === 1 ? "" : "s"} failed
            {M.accept.failed.map((c) => (
              <code className="block break-all font-mono text-[11.5px]" key={c}>
                {c}
              </code>
            ))}
          </Callout>
        ))}
      {patch && patch.length > 0 ? (
        <ChangedFiles files={patch} />
      ) : M.written.size > 0 ? (
        <div>
          <h4 className="mb-1.5 font-semibold text-[11px] text-muted-foreground/70 uppercase tracking-[.07em]">Files it wrote</h4>
          <div className="grid gap-0.5">
            {[...M.written.entries()].map(([p, ch]) => (
              <div className="flex min-w-0 items-center gap-2 font-mono text-[12px]" key={p}>
                {isWrite(ch) ? <FilePlusIcon className="size-[13px] shrink-0 text-edit" /> : <PencilIcon className="size-[13px] shrink-0 text-edit" />}
                <PathText className="min-w-0 flex-1 truncate" path={p} />
              </div>
            ))}
          </div>
        </div>
      ) : null}
      <div className="flex flex-wrap gap-3.5 text-[12px] text-muted-foreground">
        {tok && (
          <span className="inline-flex items-center gap-1.5" title="as reported by the engine">
            <CoinsIcon className="size-3" />
            {tok}
          </span>
        )}
        {steps > 0 && (
          <span className="inline-flex items-center gap-1.5">
            <LayersIcon className="size-3" />
            {steps} step{steps === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {!pane && (
        <div>
          <button className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-border px-2.5 text-[12px] text-muted-foreground hover:border-muted-foreground/40 hover:bg-secondary/60 hover:text-foreground" onClick={onActivity} type="button">
            {activity ? <ChevronDownIcon className="size-[13px]" /> : <ChevronRightIcon className="size-[13px]" />}
            {activity ? "Hide activity" : "Show activity"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── the card ───────────────────────────────────────────────────────────────
export function RunCard({
  sessionId,
  harness,
  meta,
  M,
  rawLines,
  streamErr,
  startOpen,
  onOpenVersion,
  killButton,
  killNote,
  skew,
}: {
  sessionId: string;
  harness: string;
  meta?: RunMeta;
  M: RunModel;
  rawLines: string[];
  streamErr: string | null;
  startOpen?: boolean;
  onOpenVersion: () => void;
  /** How far the browser's clock is from the pod's (lib/strands.ts noteSkew),
   * so the live clocks read the pod's time as the frames do. The book passes
   * it; without it the card keeps the browser's clock. */
  skew?: number | null;
  /** The run's Kill (run-terminal.tsx owns what it does and says). */
  killButton?: ReactNode;
  /** What the Kill button last did. */
  killNote?: string | null;
}) {
  const now = podNow(useNow(M.live), skew);
  const engine = engineName(harness);
  // The card carries its engine's colour, the colour of its strand on the
  // right side (lib/strands.ts engineColor).
  const [activityUser, setActivityUser] = useState<boolean | null>(startOpen ?? null);
  const [raw, setRaw] = useState(false);
  const [brief, setBrief] = useState(false);
  const [full, setFull] = useState(false);
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set());
  const [closedIds, setClosedIds] = useState<Set<string>>(() => new Set());
  const [allIds, setAllIds] = useState<Set<string>>(() => new Set());
  const activity = full || (activityUser ?? M.live);
  const items = groupSteps(M.steps);
  const lastItem = items[items.length - 1];
  // The newest burst stays open while it runs.
  const autoOpen = M.live && lastItem?.type === "group" ? lastItem.id : null;
  const ui: StepUi = {
    isOpen: (id) => openIds.has(id) || (!closedIds.has(id) && autoOpen === id),
    toggle: (id) => {
      const open = openIds.has(id) || (!closedIds.has(id) && autoOpen === id);
      setOpenIds((s) => {
        const n = new Set(s);
        if (open) n.delete(id);
        else n.add(id);
        return n;
      });
      setClosedIds((s) => {
        const n = new Set(s);
        if (open) n.add(id);
        else n.delete(id);
        return n;
      });
    },
    all: allIds,
    toggleAll: (id) =>
      setAllIds((s) => {
        const n = new Set(s);
        if (n.has(id)) n.delete(id);
        else n.add(id);
        return n;
      }),
  };
  const patch = useRunPatch(sessionId, harness, !M.live);

  // Sticky scroll while live unless the person scrolled up.
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const stopped = !M.live && M.killedBy != null;
  const bad = !stopped && (M.state === "failed" || M.state === "timeout");
  const warn = !M.live && !bad && !stopped && M.accept != null && !M.accept.ok;
  const started = M.start != null;
  const sub = [meta?.seeded == null ? "" : meta.seeded ? "Edit" : "New build", meta?.module ? `for ${meta.module}` : ""].filter(Boolean).join(" · ");
  const label = `${engine} · what ${/^factory-build-(\d+)$/.test(sessionId) ? `build ${sessionId.split("-").pop()}` : `run ${sessionId.slice(0, 8)}`} built${meta?.module ? ` for ${meta.module}` : ""}`;

  let pill: ReactNode;
  if (!started) pill = M.waiting != null ? <Pill icon={HourglassIcon} tone="warn">Waiting for memory</Pill> : <Pill icon={HourglassIcon} tone="idle">Starting</Pill>;
  else if (M.live) pill = M.lost ? <Pill icon={WifiOffIcon} tone="warn">Running · feed lost</Pill> : <Pill pulse tone="live">{M.think ? "Thinking" : "Working"}</Pill>;
  else if (stopped) pill = <Pill icon={OctagonXIcon} tone="idle">Stopped</Pill>;
  else if (M.state === "timeout") pill = <Pill icon={XIcon} tone="bad">Timed out</Pill>;
  else if (M.state === "failed") pill = <Pill icon={XIcon} tone="bad">Failed</Pill>;
  else if (warn) pill = <Pill icon={TriangleAlertIcon} tone="warn">Check failed</Pill>;
  else pill = <Pill icon={CheckIcon} tone="ok">Done</Pill>;

  const card = (
    <article
      className={cn(
        "relative mb-2 overflow-hidden rounded-[14px] border bg-card text-[13px] shadow-[inset_0_1px_0_color-mix(in_srgb,var(--foreground)_3%,transparent),0_10px_30px_-18px_color-mix(in_srgb,var(--shade)_80%,transparent)] transition-[border-color,box-shadow] duration-500",
        "bg-[radial-gradient(120%_140px_at_0_0,color-mix(in_srgb,var(--link)_5%,transparent),transparent_70%)]",
        M.live && started ? "border-primary/60 shadow-[0_0_0_1px_color-mix(in_srgb,var(--primary)_12%,transparent),0_0_32px_-10px_color-mix(in_srgb,var(--primary)_45%,transparent)]" : bad ? "border-destructive/45" : "border-border",
        full && "fixed inset-[22px] z-[60] mb-0 flex flex-col",
      )}
      data-harness={harness}
      data-run-card
      data-run-state={M.live ? (started ? "live" : M.waiting != null ? "waiting" : "starting") : M.state}
      data-session={sessionId}
      style={{ "--engine-color": engineColor(harness) } as CSSProperties}
    >
      <div className={cn("h-0.5", M.live && started && "run-edge animate-run-edge")} />
      <header className="flex items-center gap-3 px-3.5 pt-2.5 pb-2">
        <div
          className={cn(
            "relative grid size-[34px] shrink-0 place-items-center rounded-[9px] border border-border bg-gradient-to-b from-secondary to-muted font-mono font-semibold text-[11.5px] text-link tracking-[.04em]",
            M.live && started && "shadow-[0_0_0_1px_color-mix(in_srgb,var(--link)_25%,transparent),0_0_18px_-4px_color-mix(in_srgb,var(--link)_45%,transparent)]",
          )}
        >
          {engineMono(harness)}
          <i
            className={cn(
              "absolute -right-[3px] -bottom-[3px] size-2.5 rounded-full border-2 border-card",
              M.live ? (started ? "animate-pulse bg-link" : "bg-muted-foreground/60") : stopped ? "bg-muted-foreground/60" : bad ? "bg-destructive" : warn ? "bg-warning" : "bg-success",
            )}
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2 whitespace-nowrap font-semibold text-[14px] tracking-[.1px]">
            {engine}
            {M.model && <span className="rounded-md border border-border bg-muted px-1.5 py-px font-medium font-mono text-[11px] text-muted-foreground">{M.model}</span>}
          </div>
          <div className="mt-px flex items-center gap-1.5 overflow-hidden whitespace-nowrap text-[12px] text-muted-foreground">
            {sub}
            {sub && meta?.prompt && <span className="text-border">|</span>}
            {meta?.prompt && (
              <button className="text-link underline decoration-link/35 underline-offset-[3px] hover:decoration-link" onClick={() => setBrief((v) => !v)} type="button">
                {brief ? "Hide brief" : "Brief from your Host"}
              </button>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          {pill}
          <span className="min-w-[44px] text-right font-medium font-mono text-[12.5px] text-foreground tabular-nums">
            {!started ? "0:00" : M.live ? clock(now - (M.start ?? now)) : clock(M.elapsed ?? (M.lastT ?? 0) - (M.start ?? 0))}
          </span>
        </div>
      </header>
      <ActivityStrip M={M} now={now} />
      <Stats M={M} now={now} />
      {brief && meta?.prompt && (
        <div className="mr-3.5 mb-2.5 ml-[60px] overflow-hidden rounded-[10px] border border-border bg-muted">
          <div className="flex justify-between border-border border-b px-2.5 py-[7px] text-[11.5px] text-muted-foreground">
            <span>What your Host sent {engine}</span>
            <span>{meta.prompt.length.toLocaleString()} chars</span>
          </div>
          <pre className="max-h-[260px] overflow-auto whitespace-pre-wrap break-words px-3 py-2.5 font-mono text-muted-foreground text-[11.5px] leading-relaxed">{meta.prompt}</pre>
        </div>
      )}
      {/* The preview route's own answer decides (a 404 is the "no page" line):
          meta.hasEntry is a mount-time answer that reads false for every
          greenfield run until the end-of-run refresh lands. */}
      {!M.live && <RunPreview onOpen={onOpenVersion} src={{ kind: "run", sessionId, harness, label }} />}
      {!M.live && <Done activity={activity} M={M} onActivity={() => setActivityUser(!activity)} patch={patch} />}
      {(raw || activity) && (
        <div
          className={cn("overflow-auto border-border border-t bg-gradient-to-b from-[color-mix(in_srgb,var(--background)_35%,transparent)] to-transparent to-[40px]", full ? "min-h-0 flex-1" : "max-h-[420px]")}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
          }}
          ref={bodyRef}
        >
          {raw ? (
            <div className="px-3 py-2 font-mono text-[11px] text-muted-foreground leading-normal">
              {rawLines.map((l, i) => (
                <div className="whitespace-pre-wrap break-all border-border/50 border-b py-0.5" key={i}>
                  {l}
                </div>
              ))}
            </div>
          ) : items.length === 0 ? (
            <div className="py-4 pr-3.5 pl-[60px] text-[12.5px] text-muted-foreground/70">
              {M.waiting != null ? (
                <Callout icon={HourglassIcon} tone="warn">
                  {M.waiting}
                </Callout>
              ) : !started ? (
                `Waiting for ${engine} to start.`
              ) : (
                "Nothing to show yet."
              )}
            </div>
          ) : (
            <div className="relative px-3.5 pt-2 pb-3 before:absolute before:top-[18px] before:bottom-[18px] before:left-[26px] before:w-px before:bg-border">
              {items.map((it, i) =>
                it.type === "group" ? (
                  <GroupRow engine={engine} g={it} key={it.id} M={M} now={now} ui={ui} />
                ) : (
                  <StepRow engine={engine} key={it.id} last={i === items.length - 1} M={M} now={now} s={it} ui={ui} />
                ),
              )}
            </div>
          )}
        </div>
      )}
      {streamErr && M.live && <div className="border-border border-t px-3.5 py-1 text-[12px] text-warning">{streamErr}</div>}
      {killNote && (
        <div className="border-border border-t px-3.5 py-1.5 text-[12px] text-muted-foreground" data-kill-note>
          {killNote}
        </div>
      )}
      <footer className="flex min-h-[42px] items-center gap-2.5 border-border border-t bg-muted/70 py-2 pr-2.5 pl-3.5">
        <Now engine={engine} M={M} now={now} />
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {killButton}
          <button
            className={cn("inline-flex h-7 items-center gap-1.5 rounded-lg border border-border px-2.5 text-[12px] hover:border-muted-foreground/40 hover:bg-secondary/60 hover:text-foreground", raw ? "bg-secondary text-foreground" : "text-muted-foreground")}
            onClick={() => setRaw((v) => !v)}
            title="Show the frames exactly as they arrived"
            type="button"
          >
            <CodeIcon className="size-[13px]" />
            Raw
          </button>
          <button
            className="inline-grid size-7 place-items-center rounded-lg border border-border text-muted-foreground hover:border-muted-foreground/40 hover:bg-secondary/60 hover:text-foreground"
            onClick={() => setFull((v) => !v)}
            title={full ? "Close" : "Expand"}
            type="button"
          >
            {full ? <XIcon className="size-[13px]" /> : <Maximize2Icon className="size-[13px]" />}
          </button>
          <button
            className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-primary bg-primary px-2.5 font-semibold text-[12px] text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:border-border disabled:bg-transparent disabled:font-medium disabled:text-muted-foreground/70"
            data-open-version
            disabled={M.live}
            onClick={onOpenVersion}
            title={M.live ? "Opens when the run lands" : "Open what this run built: its own files, whether or not it was adopted, and its changes"}
            type="button"
          >
            <SquareArrowOutUpRightIcon className="size-[13px]" />
            Open version
          </button>
        </div>
      </footer>
    </article>
  );
  return (
    <>
      {full && <div className="fixed inset-0 z-[55] bg-[color-mix(in_srgb,var(--shade)_60%,transparent)] backdrop-blur-[3px]" onClick={() => setFull(false)} />}
      {card}
    </>
  );
}
