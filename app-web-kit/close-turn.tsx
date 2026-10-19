// app-web-kit/close-turn.tsx — the Review button's panel (2026-09-30).
//
// The Host ends every turn with `close_turn`: structured signal about the turn
// (its type, which calls helped and which were dead, what would have made it
// faster, the topic, what the user wants, the thread goal, the handoff). It is
// no hidden note — the user sees exactly what the Host sent, here, under the
// turn it closed. Stored on the turn row as metadata.close_turn and carried on
// the live turn's context frame (session.py), in the shape
// runtime/close_turn.normalize keeps. Read-only: nothing here writes.

import { cn } from "@/lib/utils";

export interface CloseTurnSignal {
  type?: { name?: string; is_new?: boolean };
  useful_calls?: string[];
  dead_calls?: string[];
  faster?: { kind?: string; value?: string }[];
  topic?: string;
  title?: string;
  preferences?: { text?: string; reason?: string; scope?: { kind?: string; ref?: string } }[];
  goal?: { set?: string; met?: boolean };
  handoff?: { next_step?: string; open_work?: string[]; identifiers?: string[] };
}

export function parseCloseTurn(v: unknown): CloseTurnSignal | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const s = v as CloseTurnSignal;
  return Object.keys(s).length ? s : null;
}

const list = (xs?: string[]) => (xs ?? []).filter(Boolean).join(", ");

function scopeLabel(sc?: { kind?: string; ref?: string }): string {
  if (!sc?.kind || sc.kind === "global") return "everywhere";
  return sc.ref ? `${sc.kind} ${sc.ref}` : `this ${sc.kind}`;
}

/** One row per field the Host sent; a field it left out prints nothing. */
export function CloseTurnReview({ s, className }: { s: CloseTurnSignal; className?: string }) {
  const rows: [string, string][] = [];
  if (s.type?.name) rows.push(["type", `${s.type.name}${s.type.is_new ? " (new)" : ""}`]);
  if (s.useful_calls?.length) rows.push(["helped", list(s.useful_calls)]);
  if (s.dead_calls?.length) rows.push(["did nothing", list(s.dead_calls)]);
  for (const f of s.faster ?? []) {
    if (f.value) rows.push([`faster · ${f.kind ?? "?"}`, f.value]);
  }
  if (s.topic) rows.push(["topic", s.topic]);
  if (s.title) rows.push(["title", s.title]);
  for (const p of s.preferences ?? []) {
    if (p.text) {
      rows.push([`you want · ${scopeLabel(p.scope)}`, p.reason ? `${p.text} — ${p.reason}` : p.text]);
    }
  }
  if (s.goal?.set) rows.push(["goal", s.goal.set]);
  if (s.goal?.met) rows.push(["goal", "met"]);
  if (s.handoff?.next_step) rows.push(["next", s.handoff.next_step]);
  if (s.handoff?.open_work?.length) rows.push(["open", list(s.handoff.open_work)]);
  if (s.handoff?.identifiers?.length) rows.push(["ids", list(s.handoff.identifiers)]);
  return (
    <div
      className={cn(
        "mt-1 rounded-md border border-border/60 bg-muted/30 px-2 py-1.5 text-[11px] leading-5 text-muted-foreground",
        className,
      )}
    >
      <div className="font-medium text-foreground/80">What the Host noted about this turn</div>
      {rows.length === 0 ? (
        <div>nothing beyond closing the turn</div>
      ) : (
        rows.map(([k, v], i) => (
          <div key={`${k}-${i}`} className="break-words">
            <span className="text-foreground/70">{k}</span> · {v}
          </div>
        ))
      )}
    </div>
  );
}
