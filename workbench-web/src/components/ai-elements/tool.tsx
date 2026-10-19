// Based on AI Elements by Vercel (https://github.com/vercel/ai-elements),
// licensed under Apache-2.0: third_party/licenses/ai-elements-1.9.1.LICENSE.
// Modified by Privatae LLC where it differs from that original.
"use client";

import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { DynamicToolUIPart, ToolUIPart } from "ai";
import {
  CheckCircleIcon,
  ChevronDownIcon,
  CircleIcon,
  ClockIcon,
  WrenchIcon,
  XCircleIcon,
} from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { isValidElement } from "react";

import { CodeBlock } from "./code-block";

export type ToolProps = ComponentProps<typeof Collapsible>;

export const Tool = ({ className, ...props }: ToolProps) => (
  <Collapsible
    className={cn("group not-prose mb-4 w-full rounded-md border", className)}
    {...props}
  />
);

export type ToolPart = ToolUIPart | DynamicToolUIPart;

export type ToolHeaderProps = {
  title?: string;
  /** The call's subject — muted after the title so an argument never reads
   * as the card's headline. The caller decides whether it is quoted (an
   * argument is; a stage sentence like the build card's is not). */
  subtitle?: string;
  className?: string;
} & (
  | { type: ToolUIPart["type"]; state: ToolUIPart["state"]; toolName?: never }
  | {
      type: DynamicToolUIPart["type"];
      state: DynamicToolUIPart["state"];
      toolName: string;
    }
);

const statusLabels: Record<ToolPart["state"], string> = {
  "approval-requested": "Awaiting Approval",
  "approval-responded": "Responded",
  "input-available": "Running",
  "input-streaming": "Pending",
  "output-available": "Completed",
  "output-denied": "Denied",
  "output-error": "Error",
};

const statusIcons: Record<ToolPart["state"], ReactNode> = {
  "approval-requested": <ClockIcon className="size-4 text-warning" />,
  "approval-responded": <CheckCircleIcon className="size-4 text-link" />,
  "input-available": <ClockIcon className="size-4 animate-pulse" />,
  "input-streaming": <CircleIcon className="size-4" />,
  "output-available": <CheckCircleIcon className="size-4 text-success" />,
  "output-denied": <XCircleIcon className="size-4 text-warning" />,
  "output-error": <XCircleIcon className="size-4 text-destructive" />,
};

export const getStatusBadge = (status: ToolPart["state"]) => (
  <Badge className="gap-1.5 rounded-full text-xs" variant="secondary">
    {statusIcons[status]}
    {statusLabels[status]}
  </Badge>
);

export const ToolHeader = ({
  className,
  title,
  subtitle,
  type,
  state,
  toolName,
  ...props
}: ToolHeaderProps) => {
  const derivedName =
    type === "dynamic-tool" ? toolName : type.split("-").slice(1).join("-");

  return (
    <CollapsibleTrigger
      className={cn(
        "flex w-full items-center justify-between gap-4 p-3",
        className
      )}
      {...props}
    >
      <div className="flex min-w-0 items-center gap-2">
        <WrenchIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="shrink-0 font-medium text-sm">{title ?? derivedName}</span>
        {subtitle && (
          <span className="truncate text-muted-foreground text-sm">
            {subtitle}
          </span>
        )}
        {getStatusBadge(state)}
      </div>
      <ChevronDownIcon className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-180" />
    </CollapsibleTrigger>
  );
};

export type ToolContentProps = ComponentProps<typeof CollapsibleContent>;

export const ToolContent = ({ className, ...props }: ToolContentProps) => (
  <CollapsibleContent
    className={cn(
      "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 space-y-4 p-4 text-popover-foreground outline-none data-[state=closed]:animate-out data-[state=open]:animate-in",
      className
    )}
    {...props}
  />
);

// ── Humane payload rendering ─────────────────────────────────────────────────
// Users read these cards mid-conversation: parameters and results render as
// compact key → value prose, never as a raw JSON dump. The raw payload stays one click away behind a "raw" fold.

const compactValue = (v: unknown, max = 160): string => {
  if (v == null) return "—";
  if (typeof v === "string") return v.length > max ? `${v.slice(0, max - 1)}…` : v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) {
    if (v.length <= 8 && v.every((x) => typeof x === "string")) {
      return (v as string[]).join(", ");
    }
    return `${v.length} items`;
  }
  try {
    const s = JSON.stringify(v);
    return s.length > max ? `${s.slice(0, max - 1)}…` : s;
  } catch {
    return String(v);
  }
};

const KeyValueRows = ({ obj }: { obj: Record<string, unknown> }) => (
  <dl className="space-y-1">
    {Object.entries(obj).map(([k, v]) => (
      <div className="flex gap-2 text-xs" key={k}>
        <dt className="shrink-0 font-medium text-muted-foreground">{k}</dt>
        <dd className="min-w-0 break-words text-foreground">{compactValue(v)}</dd>
      </div>
    ))}
  </dl>
);

const RawFold = ({ value }: { value: unknown }) => (
  <details>
    <summary className="cursor-pointer select-none text-muted-foreground text-xs hover:text-foreground">
      raw
    </summary>
    <div className="mt-1 rounded-md bg-muted/50">
      <CodeBlock code={JSON.stringify(value, null, 2)} language="json" />
    </div>
  </details>
);

/** Long text renders in full only on request — the first screenful reads
 * inline, the rest folds. Display folds, data never truncates. */
const LongText = ({ text }: { text: string }) => {
  const LIMIT = 1200;
  if (text.length <= LIMIT) {
    return <div className="whitespace-pre-wrap break-words text-xs">{text}</div>;
  }
  return (
    <div className="text-xs">
      <div className="whitespace-pre-wrap break-words">{text.slice(0, LIMIT)}…</div>
      <details>
        <summary className="cursor-pointer select-none text-muted-foreground hover:text-foreground">
          show all ({text.length.toLocaleString()} chars)
        </summary>
        <div className="whitespace-pre-wrap break-words">{text.slice(LIMIT)}</div>
      </details>
    </div>
  );
};

export type ToolInputProps = ComponentProps<"div"> & {
  input: ToolPart["input"];
};

export const ToolInput = ({ className, input, ...props }: ToolInputProps) => {
  const obj =
    input && typeof input === "object" && !Array.isArray(input)
      ? (input as Record<string, unknown>)
      : null;
  if (obj && Object.keys(obj).length === 0) {
    return null; // a no-arg call needs no "Parameters {}" noise
  }
  return (
    <div className={cn("space-y-2 overflow-hidden", className)} {...props}>
      <h4 className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
        Parameters
      </h4>
      {obj ? (
        <>
          <KeyValueRows obj={obj} />
          <RawFold value={input} />
        </>
      ) : (
        <div className="text-xs">{compactValue(input, 400)}</div>
      )}
    </div>
  );
};

export type ToolOutputProps = ComponentProps<"div"> & {
  output: ToolPart["output"];
  errorText: ToolPart["errorText"];
};

export const ToolOutput = ({
  className,
  output,
  errorText,
  ...props
}: ToolOutputProps) => {
  if (!(output || errorText)) {
    return null;
  }

  let Output: ReactNode = <div>{output as ReactNode}</div>;
  let raw: unknown = null;

  if (typeof output === "string") {
    Output = <LongText text={output} />;
  } else if (typeof output === "object" && !isValidElement(output)) {
    raw = output;
    const o = output as Record<string, unknown>;
    // C-Series tool envelope is {status, result} — the result IS the payload.
    const inner = "result" in o ? o.result : o;
    if (typeof inner === "string") {
      Output = <LongText text={inner} />;
    } else if (inner && typeof inner === "object" && !Array.isArray(inner)) {
      Output = <KeyValueRows obj={inner as Record<string, unknown>} />;
    } else {
      Output = <div className="text-xs">{compactValue(inner, 400)}</div>;
    }
  }

  return (
    <div className={cn("space-y-2", className)} {...props}>
      <h4 className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
        {errorText ? "Error" : "Result"}
      </h4>
      <div
        className={cn(
          "overflow-x-auto rounded-md p-2 text-xs [&_table]:w-full",
          errorText
            ? "bg-destructive/10 text-destructive"
            : "bg-muted/50 text-foreground"
        )}
      >
        {errorText && <div>{errorText}</div>}
        {!errorText && Output}
      </div>
      {!errorText && raw != null && <RawFold value={raw} />}
    </div>
  );
};
