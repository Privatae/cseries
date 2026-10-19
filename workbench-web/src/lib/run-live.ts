// Pure logic for the bench's live run rendering — the SSE frame merge and
// the MoA-arm chip lines. Split from ops-pane.tsx so `node --test` can
// exercise it directly (the circles-stream.ts pattern).

import type { HarnessState, LadderRung, RunSession } from "@/lib/types";

/** One frame of GET /api/review/sessions/{id}/events (review.session_events).
 * Frames are SLIM by design: per-variant state/elapsed_seconds/diff_bytes/
 * model/error only — the same sensitivity class as the token-less sessions
 * list. */
export interface SessionEvent {
  session_id: string;
  status: string;
  per_harness?: Record<string, HarnessState>;
  has_picks?: boolean;
}

export const isLiveRun = (status?: string) =>
  status === "queued" || status === "running";

/** Merge one SSE status frame into the polled sessions list. Per-variant rows
 * merge OVER the existing row (slim frames omit error_type etc. — dropping
 * the polled fields would flicker them away between frames). Unknown session
 * ids are ignored: the poll — the fallback and the completeness source —
 * introduces new rows; the tail only makes known rows flip faster. */
export function applySessionEvent(
  list: RunSession[],
  ev: SessionEvent,
): RunSession[] {
  let changed = false;
  const next = list.map((s) => {
    if (s.session_id !== ev.session_id) return s;
    changed = true;
    const per: Record<string, HarnessState> = { ...(s.per_harness ?? {}) };
    for (const [vid, row] of Object.entries(ev.per_harness ?? {})) {
      per[vid] = { ...per[vid], ...row };
    }
    return {
      ...s,
      status: ev.status,
      has_picks: ev.has_picks ?? s.has_picks,
      per_harness: per,
    };
  });
  return changed ? next : list;
}

/** The run's MoA arms as compact chips, only what the manifest recorded:
 * Self-MoA sample counts per harness, per-run vCPU size, model-wait window,
 * and seeded (edit) vs greenfield worktrees. Older sessions predate these
 * manifest fields and yield no chips. */
export function moaChips(s: RunSession): string[] {
  const chips: string[] = [];
  for (const [h, n] of Object.entries(s.samples ?? {})) {
    if (typeof n === "number" && n > 1) chips.push(`${h} ×${n}`);
  }
  if (s.vcpus) chips.push(`${s.vcpus} vCPU`);
  if (s.max_minutes) chips.push(`model wait ${s.max_minutes}m`);
  if (s.seeded != null) chips.push(s.seeded ? "seeded" : "greenfield");
  return chips;
}

/** One configured rung, compact: harness (or "full bench" for '*'), model,
 * Self-MoA sample count, and its max_minutes override (in minutes). */
export function rungLabel(r: LadderRung): string {
  const parts = [r.harness === "*" ? "full bench" : r.harness];
  if (r.model) parts.push(r.model);
  if (typeof r.samples === "number" && r.samples > 1) parts.push(`×${r.samples}`);
  if (r.max_minutes) parts.push(`${r.max_minutes}m`);
  return parts.join(" · ");
}
