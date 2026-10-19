// Compare chips in the conversation (workbench 0.13.3; renamed with the one
// build door, 2026-09-22): pure helpers the chat pane uses to decide WHICH
// compare session a chip refers to and WHICH variants to mount a RunTerminal
// for. A compare is `build(action='run', variants=[…])`; history written
// before that carries the retired `review.fan_out` name and is still read. Kept free of React
// so they are testable under node --test like run-live.ts.
//
// A single build chip resolves its run as `factory-build-${taskId}` by
// construction; a compare's session id is random, so it has to ride the
// chip: live chips carry it in the tool result ({status, result:{session_id,
// …}}), reloaded chips in the persisted chip (session.py _tc_meta writes
// session_id since 2026-08-20), and chips persisted BEFORE that only know
// their task_id — those fall back to the registry row (GET /api/agents now
// exposes session_id on fan_out rows) via fanOutSessionFromAgentRows.

import type { AgentRow, RunningBuild } from "./types";

type ChipLike = { name: string; args?: unknown; sessionId?: string; result?: unknown };

function resultSessionId(result: unknown): string | null {
  const r = result as
    | { session_id?: unknown; result?: { session_id?: unknown } }
    | undefined;
  const sid = r?.result?.session_id ?? r?.session_id;
  return typeof sid === "string" && sid ? sid : null;
}

/** Which chip is a compare: a `build` call that named `variants` (live args),
 * or one whose result / persisted chip carries a compare session id (a single
 * build never has one — its run is `factory-build-<task>`), or a history chip
 * under the retired `review.fan_out` name. */
export function isFanOutChip(t: ChipLike): boolean {
  if (t.name === "review.fan_out") return true;
  if (t.name !== "build") return false;
  const a = (t.args ?? {}) as { variants?: unknown };
  if (Array.isArray(a.variants) && a.variants.length > 0) return true;
  return !!(t.sessionId || resultSessionId(t.result));
}

/** The compare session id a chip refers to, or null when the chip is not a
 * compare / has no id of its own (then see the agents fallback). */
export function fanOutSessionIdFor(t: ChipLike): string | null {
  if (!isFanOutChip(t)) return null;
  if (t.sessionId) return t.sessionId;
  return resultSessionId(t.result);
}

/** Fallback for chips persisted without a session id: find the compare
 * registry row (task_type `fan_out`) with this task id. */
export function fanOutSessionFromAgentRows(
  rows: AgentRow[] | undefined,
  taskId: string | undefined,
): string | null {
  if (!taskId || !rows) return null;
  const row = rows.find(
    (a) => String(a.task_id) === String(taskId) && a.task_type === "fan_out",
  );
  const sid = row?.session_id;
  return typeof sid === "string" && sid ? sid : null;
}

/** The variant ids to mount terminals for, off the session detail: the
 * manifest's `variants[].vid` (Self-MoA samples are their own variants:
 * claude-code, claude-code-s2, …), else its `harnesses`, else the keys of
 * per_harness state. Order preserved, duplicates dropped. */
export function variantIdsFor(detail: {
  manifest?: { variants?: { vid?: string }[]; harnesses?: string[] };
  per_harness?: Record<string, unknown>;
}): string[] {
  const out: string[] = [];
  const push = (v: unknown) => {
    if (typeof v === "string" && v && !out.includes(v)) out.push(v);
  };
  for (const v of detail.manifest?.variants ?? []) push(v?.vid);
  if (out.length === 0) for (const h of detail.manifest?.harnesses ?? []) push(h);
  if (out.length === 0) for (const h of Object.keys(detail.per_harness ?? {})) push(h);
  return out;
}



/** The task id a `build` invocation produced — live result shape
 * ({status, result:{task_id}}), older top-level shape, or the persisted chip's
 * taskId. null = no run to mount (e.g. action='plan'). */
export function buildTaskIdFor(t: {
  name: string;
  args?: unknown;
  sessionId?: string;
  taskId?: string;
  result?: unknown;
}): string | null {
  if (t.name !== "build") return null;
  // A compare's task id is not a factory build: it mounts the compare
  // terminals (isFanOutChip), never `factory-build-<task>`.
  if (isFanOutChip(t)) return null;
  if (t.taskId != null) return String(t.taskId);
  const r = t.result as
    | { task_id?: number | string; result?: { task_id?: number | string } }
    | undefined;
  const tid = r?.result?.task_id ?? r?.task_id;
  return tid != null ? String(tid) : null;
}

/** Builds running for this project that the THREAD cannot show on its own.
 *
 * The chat mounts a run terminal off a `build` tool chip. A phase started by
 * approved-plan auto-advance (tools._autoadvance_start_phase) has no tool call
 * in any turn, so it had no chip, no terminal, and nothing in the conversation
 * moved while it ran — the user only saw it in the right pane, and only learned
 * it happened when the host reported it afterwards.
 *
 * Returns the running builds with no chip of their own, so the chat can mount
 * one terminal each without ever doubling a build the thread already shows. */
export function orphanBuildsFor(
  running: RunningBuild[],
  turns: { tools: { name: string; taskId?: string; result?: unknown }[] }[],
): RunningBuild[] {
  const chipped = new Set<string>();
  for (const m of turns) {
    for (const t of m.tools) {
      const id = buildTaskIdFor(t);
      if (id != null) chipped.add(id);
    }
  }
  return running.filter((b) => !chipped.has(b.taskId));
}
