// One reader per engine's stream, shared by everything that shows it. The run card on the
// page and the strands on the book's right side both read the same engine;
// before this each RunTerminal opened its own NDJSON stream, so a second
// reader would have meant a second connection and a second fold of every
// frame. Now a stream is opened by its first reader, shared by the rest, and
// closed when the last one goes, and the fold is computed once per batch.
//
// The read loop is RunTerminalStream's, moved here unchanged (run-terminal.tsx
// before this slice): the from_seq cursor, each seq folded once, a clean end
// while the run is live resumes (run-frames.ts resumeAfterCleanEnd), a drop
// retries after 2.5 s with the reason shown, frames reach readers in 140 ms
// batches, and Raw keeps the last 4000 frames as they arrived. Each frame's
// arrival time is folded into the stream's clock skew (strands.ts noteSkew),
// except a frame replayed on opening: its age is unknown, so arrival minus ts
// says nothing about the clocks (counted, after a reload the live clocks
// would read short by the newest frame's age until the next frame came).
// Kept free of React so `node --test` drives it with a fake stream.

import {
  emptySummary,
  foldSummary,
  readRunStream,
  RESUME_DELAY_MS,
  resumeAfterCleanEnd,
  type RunFrame,
  type RunSummary,
} from "./run-frames.ts";
import { foldSteps, parsePatch, type PatchFile, type RunModel } from "./run-steps.ts";
import { noteSkew } from "./strands.ts";

export interface RunFeed {
  frames: RunFrame[];
  /** the frames as they arrived, JSON, the last RAW_MAX (the card's Raw) */
  rawLines: string[];
  /** the last drop's reason while it retries, else null */
  streamErr: string | null;
  /** min(arrival − ts) over this stream's frames: strands.ts noteSkew */
  skew: number | null;
}

export const BATCH_MS = 140;
export const RAW_MAX = 4000;
export const RETRY_MS = 2500;
/** A connection opens with the frames already written, all at once; the first
 * pause this long between two frames ends that replay, and the frames after it
 * were written live. */
export const REPLAY_GAP_S = 0.25;

export const EMPTY_FEED: RunFeed = Object.freeze({ frames: [], rawLines: [], streamErr: null, skew: null }) as RunFeed;

type Open = (fromSeq: number, signal: AbortSignal) => AsyncIterable<RunFrame>;

export interface Arrival {
  frame: RunFrame;
  /** when it arrived, on this browser's clock, in seconds */
  rx: number;
  /** it came in the burst a connection opens with (already written) */
  replay?: boolean;
}

export interface LoopDeps {
  open: Open;
  signal: AbortSignal;
  /** frames read since the last call, each with its arrival time (seconds)
   * and whether it came in the connection's opening replay */
  onBatch: (batch: Arrival[]) => void;
  onError: (message: string | null) => void;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Read one engine's stream until its run is over (or the signal aborts). */
export async function feedLoop(d: LoopDeps): Promise<"ended" | "aborted"> {
  const now = d.now ?? (() => Date.now() / 1000);
  const sleep = d.sleep ?? defaultSleep;
  const setTimer = d.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms));
  const clearTimer = d.clearTimer ?? ((t: unknown) => clearTimeout(t as ReturnType<typeof setTimeout>));
  let nextSeq = 0;
  let summary: RunSummary = emptySummary();
  let buf: Arrival[] = [];
  let timer: unknown = null;
  const flush = () => {
    if (timer != null) clearTimer(timer);
    timer = null;
    if (d.signal.aborted || buf.length === 0) return;
    const batch = buf;
    buf = [];
    d.onBatch(batch);
  };
  try {
    for (;;) {
      try {
        // This connection's opening replay lasts until the first pause.
        let replay = true;
        let prevRx: number | null = null;
        for await (const f of d.open(nextSeq, d.signal)) {
          if (d.signal.aborted) return "aborted";
          const rx = now();
          if (replay && prevRx != null && rx - prevRx >= REPLAY_GAP_S) replay = false;
          prevRx = rx;
          // from_seq replay can re-deliver the boundary frame; keep each seq once.
          if (typeof f.seq === "number") {
            if (f.seq < nextSeq) continue;
            nextSeq = f.seq + 1;
          }
          summary = foldSummary(summary, f);
          buf.push({ frame: f, rx, replay });
          if (timer == null) timer = setTimer(flush, BATCH_MS);
        }
        flush();
        // Clean end: the route returns at the terminal frame, and also after
        // its idle window with none (a variant waiting for memory). Still
        // live means resume from the cursor.
        if (d.signal.aborted) return "aborted";
        if (!resumeAfterCleanEnd(summary)) return "ended";
        await sleep(RESUME_DELAY_MS);
        if (d.signal.aborted) return "aborted";
      } catch (e) {
        flush();
        if (d.signal.aborted) return "aborted";
        if (summary.state !== "live") return "ended";
        d.onError((e as Error)?.message || "stream dropped — retrying");
        await sleep(RETRY_MS);
        if (d.signal.aborted) return "aborted";
        d.onError(null);
      }
    }
  } finally {
    if (timer != null) clearTimer(timer);
  }
}

/** A batch folded into a feed: the frames appended, Raw capped, the skew
 * moved by each live frame's arrival. Pure. */
export function withBatch(feed: RunFeed, batch: readonly Arrival[]): RunFeed {
  if (batch.length === 0) return feed;
  let skew = feed.skew;
  for (const { frame, rx, replay } of batch) if (!replay) skew = noteSkew(skew, frame.ts, rx);
  return {
    frames: feed.frames.concat(batch.map((b) => b.frame)),
    rawLines: feed.rawLines.concat(batch.map((b) => JSON.stringify(b.frame))).slice(-RAW_MAX),
    streamErr: feed.streamErr,
    skew,
  };
}

// ── the shared readers ──────────────────────────────────────────────────────
type Entry = { feed: RunFeed; subs: Set<() => void>; ctrl: AbortController };
const entries = new Map<string, Entry>();
const keyOf = (sessionId: string, harness: string) => `${sessionId}\u0000${harness}`;

let opener: Open | null = null;
/** Tests only: read frames from somewhere other than the route. */
export function setFeedOpener(o: Open | null): void {
  opener = o;
}

function start(sessionId: string, harness: string, e: Entry): void {
  const set = (feed: RunFeed) => {
    if (entries.get(keyOf(sessionId, harness)) !== e) return;
    e.feed = feed;
    for (const cb of [...e.subs]) cb();
  };
  void feedLoop({
    open: opener ?? ((from, signal) => readRunStream(sessionId, harness, from, signal)),
    signal: e.ctrl.signal,
    onBatch: (batch) => set(withBatch(e.feed, batch)),
    onError: (message) => set({ ...e.feed, streamErr: message }),
  }).catch(() => {
    /* aborted with its last reader */
  });
}

/** Read an engine's stream while `onChange` wants it. The first reader opens
 * the stream; the last one to leave closes it and forgets its frames, as an
 * unmounted run card did. */
export function subscribeRunFeed(sessionId: string, harness: string, onChange: () => void): () => void {
  const k = keyOf(sessionId, harness);
  let e = entries.get(k);
  if (!e) {
    e = { feed: EMPTY_FEED, subs: new Set(), ctrl: new AbortController() };
    entries.set(k, e);
    start(sessionId, harness, e);
  }
  const entry = e;
  entry.subs.add(onChange);
  return () => {
    entry.subs.delete(onChange);
    if (entry.subs.size === 0 && entries.get(k) === entry) {
      entries.delete(k);
      entry.ctrl.abort();
    }
  };
}

/** The engine's feed as it stands (the same object until it changes). */
export function runFeed(sessionId: string, harness: string): RunFeed {
  return entries.get(keyOf(sessionId, harness))?.feed ?? EMPTY_FEED;
}

/** Streams open right now (tests). */
export const openFeeds = (): number => entries.size;

// ── the fold, once per batch ────────────────────────────────────────────────
const folds = new WeakMap<RunFrame[], RunModel>();
/** foldSteps over a feed's frames, computed once for every reader of it. */
export function modelOf(frames: RunFrame[]): RunModel {
  let M = folds.get(frames);
  if (!M) {
    M = foldSteps(frames);
    folds.set(frames, M);
  }
  return M;
}

// ── what a run changed ──────────────────────────────────────────────────────
const patches = new Map<string, Promise<PatchFile[] | null>>();
/** A landed run's patch per file (the diff route serves diffs/<harness>.patch),
 * read once for the card and the scoreboard alike. A read that fails, or finds
 * the patch empty (its write can trail the run's last frame), is not kept, so
 * the next asker reads it again, as a card mounted again did; no counts beat
 * wrong counts. */
export function loadPatch(sessionId: string, harness: string): Promise<PatchFile[] | null> {
  const k = keyOf(sessionId, harness);
  let p = patches.get(k);
  if (!p) {
    p = (async () => {
      for (let attempt = 0; ; attempt++) {
        try {
          const r = await fetch(
            `/api/review/sessions/${encodeURIComponent(sessionId)}/diff/${encodeURIComponent(harness)}`,
            { headers: { Accept: "application/json" } },
          );
          // The app token's delivery race (the run stream's same backoff).
          if (r.status === 401 && attempt < 3) {
            await new Promise((res) => setTimeout(res, 120 * (attempt + 1)));
            continue;
          }
          if (!r.ok) return null;
          const d = (await r.json()) as { diff?: string };
          return parsePatch(d.diff ?? "");
        } catch {
          return null;
        }
      }
    })();
    patches.set(k, p);
    void p.then((files) => {
      if ((files == null || files.length === 0) && patches.get(k) === p) patches.delete(k);
    });
  }
  return p;
}
