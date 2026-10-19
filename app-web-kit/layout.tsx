import { cn } from "@/lib/utils";
import { ChevronLeftIcon, TriangleAlertIcon } from "lucide-react";
import type { ReactNode } from "react";

/** Two-column pane: a fixed sidebar + a flexible main column.
 *
 * Below md (the phone breakpoint, matching the shell's) the two columns
 * become one screen at a time:
 * - master-detail: pass `mobileDetail={selection != null}` and
 *   `onMobileBack` — the sidebar is the full-width list, the main slides
 *   in with a back bar when something is selected.
 * - `mobileDetail` defaults true, so mains-only panes (settings) just show
 *   their content on phone with the decorative sidebar hidden.
 * - `mobileStack`: one scrolling column, main first, sidebar below — for
 *   panes where both halves matter and neither is a selection list
 *   (policy: posture form + rules list).
 * Desktop rendering is unchanged in every mode.
 */
export function TwoPane({
  sidebar,
  children,
  mobileDetail = true,
  onMobileBack,
  mobileStack = false,
}: {
  sidebar: ReactNode;
  children: ReactNode;
  mobileDetail?: boolean;
  onMobileBack?: () => void;
  mobileStack?: boolean;
}) {
  if (mobileStack) {
    return (
      <div className="flex h-full min-w-0 flex-1 flex-col-reverse overflow-y-auto md:flex-row md:overflow-visible">
        <aside className="flex w-full shrink-0 flex-col border-border border-t bg-sidebar md:h-full md:w-80 md:border-t-0 md:border-r">
          {sidebar}
        </aside>
        <main className="flex min-w-0 flex-1 shrink-0 flex-col bg-background md:h-full">
          {children}
        </main>
      </div>
    );
  }
  return (
    <div className="flex h-full min-w-0 flex-1">
      <aside
        className={cn(
          "h-full w-full shrink-0 flex-col border-border bg-sidebar md:flex md:w-80 md:border-r",
          mobileDetail ? "hidden" : "flex",
        )}
      >
        {sidebar}
      </aside>
      <main
        className={cn(
          "h-full min-w-0 flex-1 flex-col bg-background",
          mobileDetail ? "flex" : "hidden md:flex",
        )}
      >
        {mobileDetail && onMobileBack && (
          <button
            className="flex shrink-0 items-center gap-1 border-border border-b px-3 py-2 text-left text-muted-foreground text-sm md:hidden"
            onClick={onMobileBack}
            type="button"
          >
            <ChevronLeftIcon className="size-4" />
            Back
          </button>
        )}
        {children}
      </main>
    </div>
  );
}

export function SidebarHeader({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-2 border-border border-b px-4 py-3">
      <span className="font-semibold text-sidebar-foreground">{title}</span>
      {children}
    </div>
  );
}

export function SidebarScroll({ children }: { children: ReactNode }) {
  return <div className="min-h-0 flex-1 overflow-y-auto p-2">{children}</div>;
}

export function SidebarSection({
  label,
  count,
  children,
}: {
  label: string;
  count?: number | string | null;
  children: ReactNode;
}) {
  const showCount = count !== undefined && count !== null && count !== "" && count !== 0;
  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 px-2 pb-1">
        <span className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
          {label}
        </span>
        {showCount && <span className="text-muted-foreground text-xs">{count}</span>}
      </div>
      {children}
    </div>
  );
}

export function SidebarEmpty({ children }: { children: ReactNode }) {
  return <div className="px-2 py-2 text-muted-foreground text-xs">{children}</div>;
}

/** A selectable list row used across sidebars. */
export function ListRow({
  active,
  onClick,
  children,
  className,
}: {
  active?: boolean;
  onClick?: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <button
      className={cn(
        "flex w-full items-center gap-3 rounded-md px-2 py-2 text-left transition-colors",
        active
          ? "bg-sidebar-accent text-sidebar-accent-foreground"
          : "text-sidebar-foreground hover:bg-sidebar-accent/50",
        className,
      )}
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  );
}

export function Avatar({ label, className }: { label: string; className?: string }) {
  const initials =
    label
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((p) => p[0]?.toUpperCase() ?? "")
      .join("") || "?";
  return (
    <span
      className={cn(
        "flex size-8 shrink-0 items-center justify-center rounded-full bg-primary/15 font-medium text-primary text-xs",
        className,
      )}
    >
      {initials}
    </span>
  );
}

export function MainScroll({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-h-0 flex-1 overflow-y-auto", className)}>{children}</div>
  );
}

export function DetailEmpty({
  icon,
  title,
  description,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-10 text-center">
      {icon && <div className="text-muted-foreground">{icon}</div>}
      <div className="space-y-1">
        <div className="font-medium text-foreground text-sm">{title}</div>
        {description && (
          <div className="mx-auto max-w-sm text-muted-foreground text-sm">
            {description}
          </div>
        )}
      </div>
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="m-2 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-destructive text-xs">
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0">
        <div className="break-words">{message}</div>
        {onRetry && (
          <button
            className="mt-1 underline underline-offset-2"
            onClick={onRetry}
            type="button"
          >
            Retry
          </button>
        )}
      </div>
    </div>
  );
}

export type PillTone = "neutral" | "success" | "warning" | "danger" | "accent";

const toneClass: Record<PillTone, string> = {
  neutral: "bg-muted text-muted-foreground",
  success: "bg-success/15 text-success",
  warning: "bg-warning/15 text-warning",
  danger: "bg-destructive/15 text-destructive",
  accent: "bg-primary/15 text-primary",
};

export function Pill({
  tone = "neutral",
  children,
  className,
}: {
  tone?: PillTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 font-medium text-[11px]",
        toneClass[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function MainHeader({ children }: { children: ReactNode }) {
  return (
    <header className="flex items-center gap-3 border-border border-b px-5 py-3">
      {children}
    </header>
  );
}

export function LoadingLine({ children = "Loading…" }: { children?: ReactNode }) {
  return <div className="px-2 py-3 text-muted-foreground text-sm">{children}</div>;
}

/** Compact native select for pane filters (empty-string values allowed). */
export function FilterSelect({
  value,
  onChange,
  options,
  className,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <select
      aria-label={ariaLabel}
      className={cn(
        "h-7 rounded-md border border-input bg-card px-2 text-foreground text-xs outline-none focus:ring-1 focus:ring-ring",
        className,
      )}
      onChange={(e) => onChange(e.target.value)}
      value={value}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

/** Title a snake_case/kebab token as words ("pr_shipped" -> "Pr shipped"). */
export function titleizeToken(token: string): string {
  const s = token.replaceAll(/[_-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** Format an ISO timestamp as a short, locale time/date label. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}
