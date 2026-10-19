import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** "3h ago" style relative time; falls back to the date past a week. */
export function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 604800) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
/**
 * JSON.stringify for tool payloads that cannot throw and cannot flood the
 * DOM: survives circular refs ("[circular]") and BigInt, pretty-prints JSON
 * held as a string (the unparsed-envelope case), and clips output over
 * `maxChars` with an explicit truncation marker. Display-side truncation
 * only — the underlying payload is untouched.
 */
export function safeStringify(value: unknown, maxChars = 50_000): string {
  const clip = (s: string) =>
    s.length > maxChars
      ? `${s.slice(0, maxChars)}\n… (truncated — ${(s.length - maxChars).toLocaleString()} more chars)`
      : s;

  if (value === null || value === undefined) return "";
  if (typeof value === "string") {
    const s = value.trim();
    if (s.startsWith("{") || s.startsWith("[")) {
      try {
        return clip(JSON.stringify(JSON.parse(s), null, 2));
      } catch {
        /* not actually JSON — show as-is */
      }
    }
    return clip(value);
  }

  const seen = new WeakSet<object>();
  try {
    const json = JSON.stringify(
      value,
      (_k, v: unknown) => {
        if (typeof v === "bigint") return v.toString();
        if (v && typeof v === "object") {
          if (seen.has(v)) return "[circular]";
          seen.add(v);
        }
        return v;
      },
      2,
    );
    return clip(json ?? String(value));
  } catch {
    try {
      return clip(String(value));
    } catch {
      return "[unrenderable value]";
    }
  }
}
