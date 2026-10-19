import { RunCard, type RunMeta } from "@/components/workbench/run-card";
import { VersionView } from "@/components/workbench/version-view";
import { useRunFeed } from "@/hooks/use-run-feed";
import { killRun } from "@/lib/api";
import { modelOf } from "@/lib/run-feed";
import { hostBrief } from "@/lib/run-steps";
import { OctagonXIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

/**
 * RunTerminal — the inline run block: the host
 * visibly running a coding harness, in the conversation. Consumes the
 * per-frame NDJSON stream (from_seq resume built in, so a reload mid-run
 * picks up where the scrollback left off, not from silence) and renders it as
 * the run card (run-card.tsx, workbench 0.24.0): steps while it runs, then the
 * run's own version loaded in place when the terminal frame lands. A "raw"
 * toggle shows the frames verbatim — no-black-box is a click, not a promise.
 *
 * Self-sufficient off a sessionId: the session detail (manifest) names the
 * harness, so a reloaded chip that only knows its task id still mounts.
 */
/** How long a session may stay unresolvable before we call it absent. A live
 * run's dir appears in well under a second; this is generous so a slow
 * workspace prep or a transport blip never mislabels a healthy build. */
const RESOLVE_WINDOW_MS = 30_000;

/** A run's record (GET /api/review/sessions/{id}), the fields read here. */
export type RunSessionDetail = {
  manifest?: {
    harnesses?: string[];
    seeded?: boolean;
    project_id?: number;
    prompt?: string;
    /** what each run's harness actually received, keyed by run
     * label (review.py _record_sent_prompt) */
    sent_prompt?: unknown;
    module?: string | null;
  };
  entrypoints?: Record<string, boolean>;
};

/** What the card knows of one engine's run, off the run's record: the brief
 * it was sent, whether its worktree has a page to show (review.py
 * _session_entrypoints), the module it was built for. */
export function runMetaOf(d: RunSessionDetail, h: string): RunMeta {
  return {
    seeded: d.manifest?.seeded,
    projectId: typeof d.manifest?.project_id === "number" ? d.manifest.project_id : undefined,
    prompt: hostBrief(d.manifest, h),
    hasEntry: d.entrypoints?.[h] === true,
    module: typeof d.manifest?.module === "string" ? d.manifest.module : null,
  };
}

/** What a run card's "Open version" opens. ONE open, in every run state:
 * THIS run's version — its own worktree through the per-run
 * preview route, adopted or not, installed or not — with the run's changes
 * beside it, opening on the changes when the run left no page to show. The
 * card opens it, and so does the book's "This page" for the page's latest
 * finished build (src/book/latest-build.tsx), so the two can never differ. */
export function RunVersionWindow({
  sessionId,
  harness,
  meta,
  onClose,
}: {
  sessionId: string;
  harness: string;
  meta?: RunMeta | null;
  onClose: () => void;
}) {
  return (
    <VersionView
      onClose={onClose}
      src={{
        kind: "run",
        sessionId,
        harness,
        label: `${harness} · what run ${sessionId.slice(0, 8)} built${meta?.module ? ` for ${meta.module}` : ""}`,
      }}
      startOn={meta?.hasEntry === false ? "changes" : "preview"}
    />
  );
}

export function RunTerminal({
  sessionId,
  harness: harnessProp,
  startOpen,
}: {
  sessionId: string;
  harness?: string;
  startOpen?: boolean;
}) {
  const [harness, setHarness] = useState<string | null>(harnessProp ?? null);
  const [meta, setMeta] = useState<RunMeta | null>(null);
  const [missing, setMissing] = useState(false);
  const [runEnded, setRunEnded] = useState(false);

  // Resolve harness/model/project/prompt off the session manifest. Always
  // fetched (the prompt lives only there); the harness is taken from it when
  // not supplied by the caller.
  //
  // RETRIES while the session is not resolvable yet, and that is the point.
  // The chat mounts this terminal the instant the `build` tool returns its
  // task_id, but the session dir is created by the DETACHED build a moment
  // later — the session dir can trail the task row by ~650ms (longer
  // when prepare_workspace has a real fetch to do). A single fetch inside that
  // window got a 404, latched `missing` forever, and printed "no recorded
  // session (predates the run surface)" over a perfectly healthy running
  // build for the rest of its life. It is a race, so it hit some runs and not
  // others. Same for the 200-but-manifest-not-written-yet case one step later.
  //
  // A live run's session WILL appear, so "not there" is only meaningful after
  // it has stayed not-there. Give up after RESOLVE_WINDOW_MS and say plainly
  // that we could not find it — without claiming to know why.
  useEffect(() => {
    let alive = true;
    let waited = 0;
    const attempt = async (): Promise<void> => {
      if (!alive) return;
      const again = () => {
        if (!alive) return;
        if (waited >= RESOLVE_WINDOW_MS) {
          if (!harness) setMissing(true);
          return;
        }
        const delay = Math.min(2000, 250 + waited);
        waited += delay;
        window.setTimeout(() => void attempt(), delay);
      };
      try {
        const r = await fetch(
          `/api/review/sessions/${encodeURIComponent(sessionId)}`,
          { headers: { Accept: "application/json" } },
        );
        if (!r.ok) {
          // 404 = not created YET, or reclaimed, or genuinely absent. Only
          // time can tell those apart, so wait rather than guess.
          if (r.status === 404) again();
          return;
        }
        const d = (await r.json()) as RunSessionDetail;
        const h = harness ?? d.manifest?.harnesses?.[0];
        if (!h) {
          // The dir exists but manifest.json has not landed yet — the second
          // half of the same race. Retryable, not missing.
          again();
          return;
        }
        if (alive) {
          if (!harness) setHarness(h);
          setMeta(runMetaOf(d, h));
        }
      } catch {
        // A transport blip is not evidence the session is absent either.
        again();
      }
    };
    void attempt();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // hasEntry is a POINT-IN-TIME answer — review._session_entrypoints looks for
  // an index.html in the run's OWN worktree — and the effect above runs once,
  // on mount. A greenfield worktree is empty at mount BY CONSTRUCTION, so for
  // every from-scratch run the answer captured there is false, and nothing ever
  // asked again: a terminal that mounted against an empty worktree, whose run
  // wrote index.html minutes later, offered only the diff. Seeded runs inherit the project's index.html at t=0, which
  // is exactly why this never showed on edit-mode builds.
  //
  // Safe to ask once at the end: the worktree is collected BEFORE the terminal
  // frame is written (same run: collect 21:43:25.980, terminal frame
  // 21:43:26.040), so by the time `ended` flips the files are already on disk.
  //
  // Deliberately a SEPARATE effect. The resolve above owns the `missing` latch
  // and the retry window; re-running it to refresh one boolean would put a
  // settled run back through logic whose entire job is deciding whether a
  // session exists yet — and that logic latches.
  useEffect(() => {
    if (!runEnded || !harness) return;
    let alive = true;
    void (async () => {
      try {
        const r = await fetch(
          `/api/review/sessions/${encodeURIComponent(sessionId)}`,
          { headers: { Accept: "application/json" } },
        );
        if (!r.ok || !alive) return;
        const d = (await r.json()) as RunSessionDetail;
        if (!alive) return;
        // MERGE, never replace: a refresh must not drop the prompt, model or
        // project id the resolve effect already put here.
        setMeta((m) => ({
          ...(m ?? {}),
          hasEntry: runMetaOf(d, harness).hasEntry,
        }));
      } catch {
        // Leave the mount-time answer in place. A failed refresh is not
        // evidence there is nothing to open.
      }
    })();
    return () => {
      alive = false;
    };
  }, [runEnded, harness, sessionId]);

  if (missing) {
    // Only after RESOLVE_WINDOW_MS of retries. States what we observed, not a
    // cause: a session can be absent because the run predates the run surface,
    // because the retention sweep reclaimed it, or because it never started —
    // and this component cannot tell those apart. Asserting one of them (it
    // used to claim "predates the run surface") is wrong most of the time.
    return (
      <div className="mb-2 rounded-[14px] border border-border border-dashed px-3.5 py-2.5 text-[12.5px] text-muted-foreground">
        run {sessionId}: no recorded session found — the run row still holds
        its transcript.
      </div>
    );
  }
  if (!harness) {
    return (
      <div className="mb-2 flex items-center gap-2 rounded-[14px] border border-border bg-card px-3.5 py-2.5 text-[12.5px] text-muted-foreground">
        <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-link/20 border-t-link" />
        loading run {sessionId}…
      </div>
    );
  }
  return (
    <RunTerminalStream
      harness={harness}
      meta={meta ?? undefined}
      onEnded={() => setRunEnded(true)}
      sessionId={sessionId}
      startOpen={startOpen}
    />
  );
}

function RunTerminalStream({
  sessionId,
  harness,
  meta,
  startOpen,
  onEnded,
}: {
  sessionId: string;
  harness: string;
  meta?: RunMeta;
  startOpen?: boolean;
  /** Fired ONCE when this run reaches a terminal frame, so the parent can
   * re-ask what the run left behind. `ended` lives here (it is derived from
   * the frame fold) while `meta` lives up there. */
  onEnded?: () => void;
}) {
  // The engine's stream, shared with anything else showing this run (the
  // book's strands): lib/run-feed.ts holds the reader that used to live
  // here — the from_seq reconnect loop, a clean end that resumes while the
  // run is live, frames in 140 ms batches, Raw's last 4000 — and opens it for
  // the first reader and closes it after the last.
  const { frames, rawLines, streamErr, skew } = useRunFeed(sessionId, harness);
  const [versionOpen, setVersionOpen] = useState(false);
  // What the Kill button last did — "stopping…", what stopped, or why not.
  const [killNote, setKillNote] = useState<string | null>(null);
  // The card's clocks run on the pod's clock (lib/strands.ts podNow, from
  // the frames' skew).

  const model = modelOf(frames);

  // The run reached a terminal frame, whatever its outcome. Tell the parent
  // exactly once. Guarded by a ref rather than the dep list because
  // `onEnded` is an inline arrow upstream and so has a new identity every
  // render — the effect re-runs, the notification does not.
  const ended = !model.live;
  const toldParent = useRef(false);
  useEffect(() => {
    if (ended && !toldParent.current) {
      toldParent.current = true;
      onEnded?.();
    }
  }, [ended, onEnded]);

  const live = model.live;
  // Queued for memory (fan-out waves): the run will start, but nothing is
  // running on a pod yet, so there is nothing to kill.
  const waiting = live && model.waiting != null;

  // THE KILL. For a run that keeps working and gets nowhere —
  // the idle watchdog never stops a busy run. One click: a stop is always
  // allowed and never asks first. The pod's stop ends every run it holds, so
  // the button's title says so, and the note names any other run that
  // stopped with this one.
  const kill = async () => {
    setKillNote("stopping…");
    const r = await killRun(sessionId, harness);
    if (!r.ok) {
      setKillNote(`could not kill it: ${r.message}`);
      return;
    }
    const d = r.data;
    // The halt lands before the stop, so it holds even when the stop fails.
    const halted = d.halted_project_id ? "; the plan has stopped advancing until it is built again" : "";
    if (d.errors?.length) {
      setKillNote(`could not kill it: ${d.errors.map((e) => e.error).join("; ")}${halted}`);
    } else if (!d.stopped?.length) {
      setKillNote(`${d.detail || "nothing was running — it may have just ended"}${halted}`);
    } else {
      // Every run that stopped besides this one: other work on the pod, and
      // this compare's other variants on the same engine (on its pod, or
      // still waiting to start).
      const others = d.stopped.filter((x) => x.other_session || x.label !== harness).length;
      setKillNote(
        `killed${others ? ` — ${others} other run${others === 1 ? "" : "s"} stopped too` : ""}${halted}`,
      );
    }
  };

  return (
    <>
      <RunCard
        harness={harness}
        killButton={
          <>
            {live && !waiting && (
              <button
                className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-destructive/50 px-2.5 text-[12px] text-destructive hover:bg-destructive/10 disabled:cursor-not-allowed disabled:opacity-50"
                data-kill-run
                disabled={killNote === "stopping…"}
                onClick={() => void kill()}
                title={`end this run now on its coding engine; any other run on the same ${harness} engine stops with it and the project's plan stops advancing`}
                type="button"
              >
                <OctagonXIcon className="size-[13px]" />
                Kill
              </button>
            )}
          </>
        }
        killNote={killNote}
        M={model}
        meta={meta}
        onOpenVersion={() => setVersionOpen(true)}
        rawLines={rawLines}
        sessionId={sessionId}
        skew={skew}
        startOpen={startOpen}
        streamErr={streamErr}
      />
      {versionOpen && (
        <RunVersionWindow harness={harness} meta={meta} onClose={() => setVersionOpen(false)} sessionId={sessionId} />
      )}
    </>
  );
}
