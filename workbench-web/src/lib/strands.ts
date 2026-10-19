// The run board on the book's right side (ported from the retired Bench
// app): the shared timeline drawn as strands, the scoreboard
// with "first to finish", the run header and its session clock, and the pod
// clock correction. Pure, over run-steps.ts's RunModel, so `node --test`
// drives it; the book draws it (src/book/bench.tsx).

import type { HarnessState } from "./types.ts";
import {
  clock,
  engineName,
  quietThink,
  shortPath,
  toolLabel,
  type PatchFile,
  type RunModel,
} from "./run-steps.ts";

// ── the pod's clock ─────────────────────────────────────────────────────────
/** A frame carries its writer's `ts`, the pod's clock in epoch seconds. The
 * smallest arrival-minus-ts seen is how far this browser's clock is from the
 * pod's (plus the shortest delivery time). Past two minutes either way it is
 * not a clock difference but a replay of old frames, or a clock that is
 * wrong; those are ignored. */
export const SKEW_LIMIT_S = 120;

/** Fold one frame's arrival into the skew so far.
 * `rx`: when the frame arrived, on this browser's clock, in seconds. */
export function noteSkew(prev: number | null, ts: unknown, rx: number): number | null {
  if (typeof ts !== "number" || !Number.isFinite(ts) || !Number.isFinite(rx)) return prev;
  const d = rx - ts;
  if (Math.abs(d) < SKEW_LIMIT_S && (prev == null || d < prev)) return d;
  return prev;
}

/** One skew for a run: the smallest over its engines' streams, one for every
 * frame of the run. */
export function runSkew(skews: readonly (number | null | undefined)[]): number | null {
  let out: number | null = null;
  for (const s of skews) if (typeof s === "number" && Number.isFinite(s) && (out == null || s < out)) out = s;
  return out;
}

/** Now, on the pod's clock: the time the frames' own
 * times are compared with, so a live clock reads right whichever clock is
 * ahead. No skew yet: the browser's own clock. */
export const podNow = (deviceNow: number, skew: number | null | undefined): number =>
  deviceNow - (typeof skew === "number" && Number.isFinite(skew) ? skew : 0);

/** A session row's time as epoch seconds: the server writes time.time()
 * (review.py list_sessions_service: manifest started_at / completed_at). */
export function epochOf(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) && v > 0 ? v : null;
  if (typeof v === "string" && v.trim()) {
    const n = Number(v);
    if (Number.isFinite(n)) return n > 0 ? n : null;
    const ms = Date.parse(v);
    return Number.isFinite(ms) ? ms / 1000 : null;
  }
  return null;
}

// ── the run ─────────────────────────────────────────────────────────────────
/** The sessions list's row (review.py list_sessions_service), the fields the
 * board reads. */
export interface BoardRow {
  session_id: string;
  prompt?: string | null;
  started_at?: unknown;
  completed_at?: unknown;
  harnesses?: string[] | null;
  per_harness?: Record<string, HarnessState> | null;
}

/** The run's engines, in the order they were sent: the variants its state
 * names (review.py writes every variant there at dispatch, queued), else the
 * harnesses its manifest lists. The Bench took the union of the two, which
 * put a lane with no stream beside every Self-MoA sample's own. */
export function runEngines(row: Pick<BoardRow, "harnesses" | "per_harness"> | null | undefined): string[] {
  const out: string[] = [];
  const push = (v: unknown) => {
    if (typeof v === "string" && v && !out.includes(v)) out.push(v);
  };
  for (const k of Object.keys(row?.per_harness ?? {})) push(k);
  if (out.length === 0) for (const h of row?.harnesses ?? []) push(h);
  return out;
}

// ── the page's latest finished build ────────────────────────────────────────
/** What "This page" offers to open (src/book/latest-build.tsx): the newest
 * run on the page, by when it was sent, with an engine that finished with a
 * build (its per_harness state "done"), and those engines in the run's order.
 * A run whose engines all failed built nothing to open and is passed over; a
 * run still going offers the engines already done. */
export interface LatestBuild {
  sessionId: string;
  /** Every engine the run was sent to. */
  engines: string[];
  /** The ones that finished with a build. */
  done: string[];
}

/** The page's latest finished build off the sessions list (review.py
 * list_sessions_service, newest first), or null when no run on the page has
 * finished one. Only the page's own runs count. */
export function latestBuild(
  rows: readonly (BoardRow & { project_id?: number | null })[],
  projectId: number | null,
): LatestBuild | null {
  if (projectId == null) return null;
  const mine = rows
    .map((row, i) => ({ row, i, at: epochOf(row.started_at) ?? Number.NEGATIVE_INFINITY }))
    .filter(({ row }) => row.project_id === projectId)
    .sort((a, b) => (a.at !== b.at ? (b.at > a.at ? 1 : -1) : a.i - b.i));
  for (const { row } of mine) {
    const engines = runEngines(row);
    const done = engines.filter((h) => row.per_harness?.[h]?.state === "done");
    if (done.length) return { sessionId: row.session_id, engines, done };
  }
  return null;
}

/** One string per latest build and its finished engines: it changes when a
 * newer run finishes, or another engine of the same run does. */
export function latestBuildKey(b: LatestBuild | null): string {
  return b ? `${b.sessionId}|${b.done.join(",")}|${b.engines.join(",")}` : "";
}

/** A build's number off its session id (`factory-build-<task>`), else null. */
export function buildNo(sessionId: string): string | null {
  const m = /^factory-build-(\d+)$/.exec(sessionId);
  return m ? m[1] : null;
}

const firstLine = (t: unknown): string =>
  (String(t ?? "").split("\n").find((x) => x.trim()) ?? "").trim();

/** The run header's title. */
export function runTitle(sessionId: string, engines: readonly string[]): string {
  const n = engines.length;
  if (n > 1) return `Compare · ${n} engines`;
  if (n === 1) {
    const bn = buildNo(sessionId);
    return `Build${bn ? ` ${bn}` : ""} · ${engineName(engines[0])}`;
  }
  return "Run";
}

/** The brief's first line under the title. A build's prompt opens with the
 * same working-directory instructions every time, so a build shows none. */
export function briefLine(sessionId: string, prompt: unknown): string {
  return buildNo(sessionId) ? "" : firstLine(prompt);
}

/** How a run is named in the list of a page's runs. */
export function runLabel(row: Pick<BoardRow, "session_id" | "prompt" | "harnesses" | "per_harness">): string {
  const n = buildNo(row.session_id);
  const h = runEngines(row)[0];
  if (n) return `Build ${n}${h ? ` · ${engineName(h)}` : ""}`;
  return firstLine(row.prompt) || row.session_id;
}

/** The session clock: from the run's start to now while any engine runs;
 * stopped at completed_at once it is over, else at its last frame. null
 * before the run has a start. */
export function sessionSeconds(
  row: Pick<BoardRow, "started_at" | "completed_at"> | null | undefined,
  models: readonly RunModel[],
  now: number,
  /** Whether the run is still going, when the caller knows better than the
   * frames (an old run's frames still loading read as live). */
  running?: boolean,
): number | null {
  const started = epochOf(row?.started_at);
  if (started == null) return null;
  const live = running ?? models.some((M) => M.live);
  const lastT = Math.max(0, ...models.map((M) => M.lastT ?? 0));
  const end = live ? now : epochOf(row?.completed_at) || lastT || now;
  return Math.max(0, end - started);
}

/** "2 running · 3 engines": the run's summary line. */
export function runSummary(models: readonly RunModel[]): string {
  const n = models.length;
  if (!n) return "";
  const running = models.filter((M) => M.live && M.start != null).length;
  const engines = `${n} engine${n === 1 ? "" : "s"}`;
  return running ? `${running} running · ${engines}` : engines;
}

// ── engines' colours ────────────────────────────────────────────────────────
/** Each engine keeps one colour across runs (colour follows the engine,
 * never its lane). Validated on the page's paper (#fbfaf6) with the dataviz
 * skill's validate_palette.js: all seven pass in this order on neighbouring
 * pairs (lightness, chroma, colour-blind separation, contrast >= 3:1), and the
 * first three, the common compare, also pass every pair. Each strand is named
 * beside it, so colour is never the only way to tell them apart. */
export const ENGINE_COLORS: Record<string, string> = {
  "claude-code": "#c2571f",
  codex: "#3a67d0",
  grok: "#138a6a",
  kimi: "#6650b8",
  antigravity: "#9a6a00",
  cursor: "#b0477a",
  opencode: "#4c7a1a",
};
/** An engine the table does not name is drawn in the desktop's secondary ink
 * (--cseries-linen): the desktop contract holds it at 3:1 or more on the page,
 * whatever the design (server/applications/desktop_contract.py
 * CONTRAST_PAIRS), which a fixed grey could not promise on both a dark page and
 * a light one. The fallback is the stock value, for a frame whose tokens are
 * not set. Every consumer takes the colour as the value of the --engine-color
 * custom property (the strands and their names, the scoreboard, This page's
 * Open, the run card's edge), so it may be a var(). */
export const OTHER_ENGINE_COLOR = "var(--cseries-linen, #9ca3af)";

export function engineColor(id: string): string {
  const base = /^(.+?)-s\d+$/.exec(id)?.[1] ?? id;
  return ENGINE_COLORS[base] ?? OTHER_ENGINE_COLOR;
}

// ── a lane's state ──────────────────────────────────────────────────────────
export type LaneState = "queued" | "waiting" | "running" | "done" | "failed" | "timeout" | "stopped";

/** What an engine is doing, from its frames and the status feed (its lane's
 * and scoreboard's status, with "stopped" for a run someone killed: nothing
 * it was asked for finished, but it did not fail). */
export function laneState(M: RunModel, ph: HarnessState | null | undefined): LaneState {
  const failed = M.state === "failed" || M.state === "timeout" || ph?.state === "failed";
  if (M.start == null) {
    if (failed) return M.killedBy ? "stopped" : M.state === "timeout" ? "timeout" : "failed";
    return M.waiting != null || ph?.queued_note ? "waiting" : "queued";
  }
  if (M.live && !failed) return "running";
  if (M.killedBy) return "stopped";
  if (M.state === "timeout") return "timeout";
  if (failed) return "failed";
  return "done";
}

export const LANE_WORDS: Record<LaneState, string> = {
  queued: "queued",
  waiting: "waiting for memory",
  running: "running",
  done: "done",
  failed: "failed",
  timeout: "timed out",
  stopped: "stopped",
};

// ── the timeline, as strands ────────────────────────────────────────────────
/** Tick spacing: the first step that puts no more than `most` ticks across
 * the span, else two hours. A full-width board takes 8; the book's right
 * side is a third of that wide. */
export const TICK_STEPS = [15, 30, 60, 120, 300, 600, 900, 1800, 3600] as const;
export function tickStep(span: number, most = 8): number {
  return TICK_STEPS.find((s) => span / s <= most) ?? 7200;
}

export interface Engine {
  id: string;
  M: RunModel;
  ph?: HarnessState | null;
}

export interface StrandMark {
  x: number;
  t: number;
  kind: "tool" | "edit" | "error";
  /** what the step did, in words (its row on the card) */
  label: string;
}

export interface Strand {
  id: string;
  state: LaneState;
  /** waiting for memory: from when it was queued to its start (or now) */
  queued: { x0: number; x1: number; note: string } | null;
  /** from its first frame to now while it runs, else to its last frame */
  run: { x0: number; x1: number } | null;
  thinks: { x0: number; x1: number }[];
  marks: StrandMark[];
  /** where the strand ends: a live dot at now, or the finish mark */
  end: { x: number; kind: "live" | "done" | "failed" | "stopped"; ran: number | null } | null;
}

export interface Strands {
  t0: number;
  /** the seconds the width stands for */
  span: number;
  anyLive: boolean;
  ticks: { x: number; label: string }[];
  /** the now line, while any engine runs */
  nowX: number | null;
  strands: Strand[];
}

export interface StrandsBox {
  /** where 0:00 sits, and the room kept right of the last moment */
  left: number;
  right: number;
  width: number;
  /** most ticks across (tickStep) */
  ticks?: number;
}

/** The shared timeline: one strand per engine on one
 * time axis that starts when the run was sent. While any engine runs the
 * span is now plus 4% (at least a minute), so the strands lengthen toward the
 * right edge; once all are over it is the latest end. */
export function strands(
  row: Pick<BoardRow, "started_at"> | null | undefined,
  engines: readonly Engine[],
  now: number,
  box: StrandsBox,
): Strands {
  const models = engines.map((e) => e.M);
  const anyLive = engines.some((e) => {
    const st = e.ph?.state;
    return e.M.live && st !== "failed" && st !== "done";
  });
  const t0 =
    epochOf(row?.started_at) ??
    Math.min(...models.map((M) => M.queuedAt ?? M.start ?? now), now);
  const ends = models.map((M) => M.lastT ?? t0);
  const span = anyLive ? Math.max((now - t0) * 1.04, 60) : Math.max(1, ...ends.map((e) => e - t0));
  const plot = Math.max(1, box.width - box.left - box.right);
  const X = (t: number) => box.left + ((t - t0) / span) * plot;
  const step = tickStep(span, box.ticks ?? 8);
  const ticks: Strands["ticks"] = [];
  for (let t = 0; t <= span + 0.01; t += step) ticks.push({ x: X(t0 + t), label: clock(t) });

  const out: Strand[] = engines.map(({ id, M, ph }) => {
    const state = laneState(M, ph);
    const failed = M.state === "failed" || M.state === "timeout" || ph?.state === "failed";
    const queued =
      M.queuedAt != null
        ? { x0: X(M.queuedAt), x1: X(M.start ?? now), note: M.waiting ?? ph?.queued_note ?? "" }
        : null;
    if (M.start == null) return { id, state, queued, run: null, thinks: [], marks: [], end: null };
    const liveHere = M.live && !failed;
    const runEnd = liveHere ? now : (M.lastT ?? M.start);
    const thinks = M.steps
      .filter((s) => s.type === "think" && !quietThink(s))
      .map((s) => {
        const th = s as Extract<RunModel["steps"][number], { type: "think" }>;
        return { x0: X(th.t0), x1: X(th.t1 ?? now) };
      });
    const marks: StrandMark[] = [];
    for (const s of M.steps) {
      if (s.type === "edit") marks.push({ x: X(s.t0), t: s.t0, kind: "edit", label: `Wrote ${shortPath(s.path)}` });
      else if (s.type === "error") marks.push({ x: X(s.t0), t: s.t0, kind: "error", label: s.text });
      else if (s.type === "tool") {
        const L = toolLabel(s, false);
        const words = `${L.verb} ${L.kind === "path" ? shortPath(L.subject) : L.subject}`.trim();
        marks.push({
          x: X(s.t0),
          t: s.t0,
          kind: s.status === "err" ? "error" : s.cat === "edit" ? "edit" : "tool",
          label: words,
        });
      }
    }
    const ran = M.elapsed ?? ph?.elapsed_seconds ?? (M.lastT != null ? M.lastT - M.start : null);
    const end: Strand["end"] = liveHere
      ? { x: X(now), kind: "live", ran: null }
      : { x: X(runEnd), kind: state === "stopped" ? "stopped" : failed ? "failed" : "done", ran };
    return { id, state, queued, run: { x0: X(M.start), x1: X(runEnd) }, thinks, marks, end };
  });
  return { t0, span, anyLive, ticks, nowX: anyLive ? X(now) : null, strands: out };
}

// ── the scoreboard ──────────────────────────────────────────────────────────
export interface ScoreRow {
  id: string;
  state: LaneState;
  /** the engine's run time: "—" before it starts, live while it runs */
  time: string;
  commands: number;
  read: number;
  written: number;
  messages: number;
  failed: number;
  /** +lines −lines off its patch, once it landed and the patch is read */
  changes: { add: number; del: number } | null;
  first: boolean;
}

/** Which engine finished first: the earliest last frame among the engines
 * that completed (a failed, timed-out or stopped run never finished), or -1.
 * Shown only when more than one engine ran. */
export function firstToFinish(models: readonly RunModel[]): number {
  const fin = models.map((M) => (!M.live && M.state === "complete" && M.killedBy == null && M.lastT != null ? M.lastT : Infinity));
  const best = Math.min(...fin);
  return Number.isFinite(best) ? fin.indexOf(best) : -1;
}

/** The scoreboard's rows. `files`: each engine's patch,
 * when read. */
export function scoreboard(
  engines: readonly (Engine & { files?: PatchFile[] | null })[],
  now: number,
): ScoreRow[] {
  const first = engines.length > 1 ? firstToFinish(engines.map((e) => e.M)) : -1;
  return engines.map(({ id, M, ph, files }, i) => {
    const state = laneState(M, ph);
    const failed = M.state === "failed" || M.state === "timeout" || ph?.state === "failed";
    const el = M.elapsed ?? ph?.elapsed_seconds ?? null;
    const time =
      M.start == null ? "—" : M.live && !failed ? clock(now - M.start) : clock(el ?? (M.lastT ?? M.start) - M.start);
    const changes =
      M.live || !files
        ? null
        : { add: files.reduce((a, f) => a + f.add, 0), del: files.reduce((a, f) => a + f.del, 0) };
    return {
      id,
      state,
      time,
      commands: M.counts.cmd ?? 0,
      read: M.reads.size,
      written: M.written.size,
      messages: M.msgs,
      failed: M.errors,
      changes,
      first: i === first,
    };
  });
}

/** One engine's strand as a sentence, for the list a screen reader gets in
 * place of the drawing. */
export function strandWords(row: ScoreRow): string {
  const parts: string[] = [];
  const n = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;
  if (row.commands) parts.push(n(row.commands, "command"));
  if (row.read) parts.push(`${n(row.read, "file")} read`);
  if (row.written) parts.push(`${n(row.written, "file")} written`);
  if (row.messages) parts.push(n(row.messages, "message"));
  if (row.failed) parts.push(n(row.failed, "failed call"));
  if (row.changes) parts.push(`+${row.changes.add} −${row.changes.del} lines`);
  const head =
    row.state === "running"
      ? `running, ${row.time} so far`
      : row.state === "queued" || row.state === "waiting"
        ? LANE_WORDS[row.state]
        : `${LANE_WORDS[row.state]}${row.time !== "—" ? ` after ${row.time}` : ""}`;
  return `${engineName(row.id)}: ${head}${row.first ? ", first to finish" : ""}${parts.length ? `. ${parts.join(", ")}.` : "."}`;
}
