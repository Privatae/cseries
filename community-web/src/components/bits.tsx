// Small shared pieces: vote pill, author/time byline, status chip, markdown,
// error + empty states. One file — they're each a few lines and always used
// together.

import { useState } from "react";
import { AlertTriangle, ArrowBigUp, Flag, Lock, CheckCircle2, GitPullRequest } from "lucide-react";
import { Streamdown } from "streamdown";

import * as api from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";

export function VotePill({
  kind,
  id,
  votes,
  compact,
}: {
  kind: "thread" | "post";
  id: number;
  votes: number;
  compact?: boolean;
}) {
  const [count, setCount] = useState(votes);
  const [mine, setMine] = useState(false); // session-local; server converges
  const [busy, setBusy] = useState(false);

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    const r = await api.vote(kind, id, !mine);
    if (r.ok) {
      setCount(r.data.votes);
      setMine(!mine);
    }
    setBusy(false);
  };

  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        void toggle();
      }}
      title={mine ? "Remove vote" : "Vote up"}
      className={cn(
        "flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-xs",
        "hover:bg-accent transition-colors",
        mine ? "text-primary border-primary/50" : "text-muted-foreground",
        compact && "px-1",
      )}
    >
      <ArrowBigUp size={compact ? 13 : 15} className={mine ? "fill-current" : ""} />
      <span className="tabular-nums">{count}</span>
    </button>
  );
}

export function Byline({
  author,
  at,
  className,
}: {
  author: string;
  at: string;
  className?: string;
}) {
  return (
    <span className={cn("text-xs text-muted-foreground", className)}>
      <span className="text-foreground/80">{author}</span> · {timeAgo(at)}
    </span>
  );
}

export function StatusChip({
  status,
  proposalRef,
}: {
  status: string;
  proposalRef?: number;
}) {
  if (proposalRef) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-primary/15 px-2 py-0.5 text-[11px] text-primary">
        <GitPullRequest size={11} /> proposed #{proposalRef}
      </span>
    );
  }
  if (status === "locked") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
        <Lock size={11} /> locked
      </span>
    );
  }
  if (status === "resolved") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-success/15 px-2 py-0.5 text-[11px] text-success">
        <CheckCircle2 size={11} /> resolved
      </span>
    );
  }
  return null;
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-sm max-w-none text-sm leading-relaxed [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border [&_pre]:p-3 [&_code]:font-mono [&_a]:text-link">
      <Streamdown>{children}</Streamdown>
    </div>
  );
}

export function FlagButton({ kind, id }: { kind: "thread" | "post"; id: number }) {
  const [state, setState] = useState<"idle" | "confirm" | "done">("idle");
  if (state === "done") {
    return <span className="text-[11px] text-muted-foreground">reported</span>;
  }
  if (state === "confirm") {
    return (
      <button
        onClick={async (e) => {
          e.stopPropagation();
          await api.flag(kind, id, "member report");
          setState("done");
        }}
        className="text-[11px] text-destructive hover:underline"
      >
        confirm report?
      </button>
    );
  }
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        setState("confirm");
      }}
      title="Report to the moderation queue"
      className="text-muted-foreground/60 hover:text-destructive transition-colors"
    >
      <Flag size={12} />
    </button>
  );
}

/** Bridge/service outage → the routes 503 fail-closed. Show the truth. */
export function Unavailable({ message, retry }: { message: string; retry: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
      <AlertTriangle className="text-warning" size={28} />
      <div className="text-sm text-foreground/90">The community board is unreachable.</div>
      <div className="max-w-md text-xs text-muted-foreground">{message}</div>
      <button
        onClick={retry}
        className="mt-2 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
      >
        Try again
      </button>
    </div>
  );
}

export function Empty({ children }: { children: string }) {
  return (
    <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
      {children}
    </div>
  );
}
