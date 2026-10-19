import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  BotIcon,
  BoxesIcon,
  ChevronDownIcon,
  ClockIcon,
  GlobeIcon,
  HammerIcon,
  ListChecksIcon,
  PackageIcon,
  SquareIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Spinner } from "@/components/ui/spinner";
import {
  adoptRunInto,
  approvePhase,
  buildFromProject,
  cancelAgent,
  announceApproval,
  cancelScheduled,
  fetchAgents,
  fetchApprovals,
  fetchBenchRuns,
  fetchModules,
  fetchModuleVersions,
  fetchPhases,
  fetchPhaseSteps,
  fetchRunSessions,
  fetchScheduled,
  publishProjectAsSite,
  setModuleVersion,
  undoModuleVersion,
} from "@/lib/api";
import type { ModulesResult, ScheduledTimer } from "@/lib/api";
import { Input } from "@/components/ui/input";
import type { SitePublishResult } from "@/lib/api";
import {
  applySessionEvent,
  isLiveRun,
  type SessionEvent,
} from "@/lib/run-live";
import { RunTerminal } from "@/components/workbench/run-terminal";
import { VersionView, type VersionSrc } from "@/components/workbench/version-view";
import { cn } from "@/lib/utils";
import type {
  AgentRow,
  AppState,
  BenchRun,
  CodeApproval,
  ModuleRow,
  ModuleVersion,
  Phase,
  RunningBuild,
  PhaseStep,
  RunSession,
} from "@/lib/types";

const ACTIVE_POLL_MS = 3000;
const IDLE_POLL_MS = 15000;

/** A live (non-terminal) registry status. The registry speaks entity_tasks
 * statuses — a running agent is `in_progress` (the old "running" check never
 * matched, so the bench treated every agent as finished). */
const isLiveStatus = (s?: string) =>
  s === "queued" || s === "running" || s === "in_progress";

/** A registry row that mirrors work on the harness POOL — a single build
 * (`factory_build`) or a multi-harness fan-out (`fan_out`). Both
 * render as builds, scope to their project, carry no veto (the row is not
 * the run), and refresh the RESULT preview when they finish. */
const isPoolRun = (t?: string) => t === "factory_build" || t === "fan_out";

/**
 * The bench — the whole lifecycle of a project's work, live: what the
 * project is made of, the plan, the running work and the live output.
 *   MODULES     — at the top: each module → its versions, one
 *                 "open version" button each, plus the finished runs built for
 *                 it that are not a version yet (candidates, "use as version").
 *                 Installed / running is a badge here, on the module the app
 *                 belongs to — the run card no longer carries it.
 *   PLAN        — modules → phases, each with its status (landed or failed).
 *   WORKING NOW — only work in flight: dispatched agents and pool builds that
 *                 have not landed. Finished work leaves this strip.
 *   RUNS        — the numbered log, collapsed by default (modules are the
 *                 unit). One row per
 *                 harness_run, labelled #<id>, newest first, expanding to the
 *                 same RunTerminal the chat shows. The number is the point: it is
 *                 how the user names a run out loud, and harness_transcript
 *                 takes run_id. Superseded the old per-session RunCards,
 *                 which showed the same work with nothing to point at.
 * Plus the project's Code Approvals — its pull requests in plain English,
 * each opening the approval card over the chat. "Watching the host operate N
 * coding sessions for you."
 *
 * DROPPED: LADDER (a run's ladder state belongs on the run row it came from,
 * not in a permanent section), ACTIVITY (a lower-fidelity duplicate of the
 * runs log), and RESULT. RESULT also carried a
 * live bug: it probed /user-apps/<id>/index.html but framed /user-apps/<id>/,
 * and the directory form is not served, so FastAPI's JSON 404 body rendered as
 * the literal text {"detail":"Not Found"}. It was stale by construction anyway
 * — since run-scoped worktrees (0fa7afbf) the project dir only changes on an
 * explicit land/pick, so what it framed was rarely the run just finished. The
 * run row's own "open app" does that job properly.
 */
export function OpsPane({
  projectId,
  onOpenApproval,
  approvalsTick = 0,
  onAgentDelivered,
  onApprovalAnnounced,
  onRunningBuilds,
  benchSlot,
  onSessions,
  className,
}: {
  projectId: number | null;
  /** The book's own section over the project's runs (src/book/bench.tsx, the
   * strands), drawn first, from the runs this pane already polls; the book
   * passes it so the strands live in the book's code alone. */
  benchSlot?: (sessions: RunSession[]) => ReactNode;
  /** The runs this pane polls, each time they change (the book's "This
   * page" offers the latest finished build from them, src/book/
   * latest-build.tsx): no second poll of the same list. */
  onSessions?: (sessions: RunSession[]) => void;
  /** Raise a Code Approval's card over the chat. The project's repository is
   * the context — there is no repository picker inside a project. */
  onOpenApproval?: (number: number) => void;
  /** Bumped when an approval changed elsewhere (merged, sent back). */
  approvalsTick?: number;
  /** Fires once per dispatched agent that reached a terminal/delivered state
   * between polls, WITH its identity — the chat pane fetches until THAT task's
   * delivered turn is on screen. The flip is `delivered_at`, which is stamped
   * seconds before the report row is written (the phrasing model call sits in
   * between), so "refetch once" was never enough: it fetched into the gap. */
  onAgentDelivered?: (info: {
    projectId: number | null;
    origin_app_id: string | null;
    taskId: string;
  }) => void;
  /** This list wrote a merge's chat line (a merge the card did not see, e.g.
   * it was closed first, or one made elsewhere): the chat fetches until it
   * shows. */
  onApprovalAnnounced?: (projectId: number, number: number) => void;
  /** The pool builds running RIGHT NOW for this project, reported up so the
   * chat pane can show a live terminal for one it has no tool chip for — an
   * auto-advanced phase. This pane already polls the registry, so
   * the chat side needs no second poll and no new endpoint. */
  onRunningBuilds?: (builds: RunningBuild[]) => void;
  className?: string;
}) {
  const [sessions, setSessions] = useState<RunSession[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  // The numbered runs log. One entry per harness_run row, labelled with its
  // id — the number the person reads here is the number they say in
  // chat ("take a look at run 14"), and harness_transcript resolves it.
  const [benchRuns, setBenchRuns] = useState<BenchRun[]>([]);
  const [phases, setPhases] = useState<Phase[]>([]);
  // Modules, their current version, candidates and app state —
  // re-read on every tick so a run landing as a version shows up with the
  // build that made it.
  const [modules, setModules] = useState<ModulesResult | null>(null);
  // Timers the host set for itself. It could already set
  // one — schedule_task, fired by the offline task runner on a 30s poll — but
  // a pending timer rendered only in the Agents app, so a build you are
  // watching here said "I'll check back later" and the bench showed nothing
  // to check back. The time has to be visible where the promise was made.
  const [timers, setTimers] = useState<ScheduledTimer[]>([]);
  const [err, setErr] = useState("");
  const timer = useRef<number | null>(null);
  // task_id -> {live, delivered} from the PREVIOUS poll, to detect flips.
  const prevAgents = useRef<Map<string, { live: boolean; delivered: boolean }>>(
    new Map(),
  );
  const onDeliveredRef = useRef(onAgentDelivered);
  onDeliveredRef.current = onAgentDelivered;
  const onRunningRef = useRef(onRunningBuilds);
  onRunningRef.current = onRunningBuilds;
  const onSessionsRef = useRef(onSessions);
  onSessionsRef.current = onSessions;
  useEffect(() => {
    onSessionsRef.current?.(sessions);
  }, [sessions]);

  const tick = useCallback(async () => {
    if (timer.current) window.clearTimeout(timer.current);
    if (document.hidden) {
      timer.current = window.setTimeout(tick, IDLE_POLL_MS);
      return;
    }
    const [runs, ags, ph, brl, sch, mods] = await Promise.all([
      fetchRunSessions(projectId),
      fetchAgents(false),
      projectId != null ? fetchPhases(projectId) : Promise.resolve(null),
      fetchBenchRuns(projectId),
      fetchScheduled(),
      projectId != null ? fetchModules(projectId) : Promise.resolve(null),
    ]);
    if (projectId == null) setModules(null);
    // Same never-blank rule as the runs log: a failed read keeps what is shown.
    else if (mods?.ok) setModules(mods.data);
    // Timers for THIS project, plus one-shot un-scoped ones (an ad-hoc "I'll
    // check back" set from Circles carries no project). RECURRING un-scoped
    // tasks are excluded: on a live Host the only such scheduled task was the
    // platform's standing weekly changelog with a null
    // project, so without this every project bench would carry a "Coming
    // back to this" card for a job that has nothing to do with the project.
    // Same never-blank-on-a-transient-failure rule as the runs log.
    if (sch.ok) {
      setTimers(
        (sch.data.scheduled ?? []).filter((t) =>
          t.project_id == null ? !t.recurrence : t.project_id === projectId,
        ),
      );
    }
    // Never blanks the log on a transient failure: a numbered list the user is
    // reading references from must not flicker empty between polls.
    if (brl.ok) setBenchRuns(brl.data.runs ?? []);
    if (runs.ok) {
      const list = runs.data.sessions ?? [];
      setSessions(list);
      setErr("");
    } else {
      setErr(runs.message);
    }
    if (ags.ok) {
      const list = ags.data.agents ?? [];
      setAgents(list);
      // An agent finished (live→terminal) or its delivery landed since the
      // last poll → tell the chat pane WHICH one, so it can wait for its row
      // instead of quitting on the first unfamiliar id it sees.
      const finished: {
        projectId: number | null;
        origin_app_id: string | null;
        taskId: string;
      }[] = [];
      const next = new Map<string, { live: boolean; delivered: boolean }>();
      for (const a of list) {
        const key = String(a.task_id ?? "");
        if (!key) continue;
        const now = { live: isLiveStatus(a.status), delivered: !!a.delivered_at };
        const prev = prevAgents.current.get(key);
        if (prev && ((prev.live && !now.live) || (!prev.delivered && now.delivered))) {
          finished.push({
            // A build stamps project_id at the top level; a dispatched agent
            // carries its scope only in origin. Report the thread the report
            // was actually delivered to, so the chat pane waits on the right
            // one — or, lacking that, on none.
            projectId: a.project_id ?? a.origin_project_id ?? null,
            origin_app_id: a.origin_app_id ?? null,
            taskId: key,
          });
        }
        next.set(key, now);
      }
      prevAgents.current = next;
      for (const f of finished) onDeliveredRef.current?.(f);
      // Report the pool builds in flight for THIS project (see onRunningBuilds).
      // A SELECTED project only: under "all work" (projectId == null) the chat
      // pane is the General thread, where no build has a chip because every
      // build belongs to some project — so passing them all mounted a terminal
      // for every running build on the box, including other projects' and
      // user-started ones.
      onRunningRef.current?.(
        projectId == null
          ? []
          : list
          .filter(
            (a) =>
              a.task_type === "factory_build" &&
              isLiveStatus(a.status) &&
              a.task_id != null &&
              a.project_id === projectId,
          )
          .map((a) => ({
            taskId: String(a.task_id),
            title: a.title ?? "build",
            harness: a.harness ?? undefined,
          })),
      );
    }
    if (ph?.ok) setPhases(ph.data.phases ?? []);
    else if (projectId == null) setPhases([]);
    const active =
      (runs.ok &&
        (runs.data.sessions ?? []).some(
          (s) => s.status === "queued" || s.status === "running",
        )) ||
      (ags.ok && (ags.data.agents ?? []).some((a) => isLiveStatus(a.status)));
    timer.current = window.setTimeout(
      tick,
      active ? ACTIVE_POLL_MS : IDLE_POLL_MS,
    );
  }, [projectId]);

  useEffect(() => {
    tick();
    const vis = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", vis);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", vis);
    };
  }, [tick]);

  // SSE live tail — one EventSource per ACTIVE fan-out session on
  // /api/review/sessions/{id}/events, so per-variant state flips land within
  // ~1s instead of on the next 3s poll. The route is deliberately token-less
  // (status-only frames, the same sensitivity class as the sessions list —
  // review.session_events), so a plain EventSource works from this iframe;
  // under a future strict token mode the middleware's app_token COOKIE path
  // carries it (EventSource sends same-origin cookies). The poll above keeps
  // running as the fallback and the completeness source: on stream error we
  // just close — the next poll re-opens it (a natural ~3s retry cadence) —
  // and idle behavior is unchanged (no active session, no stream).
  const streams = useRef<Map<string, EventSource>>(new Map());
  useEffect(() => {
    const live = new Set(
      sessions.filter((s) => isLiveRun(s.status)).map((s) => s.session_id),
    );
    for (const [sid, es] of streams.current) {
      if (!live.has(sid)) {
        es.close();
        streams.current.delete(sid);
      }
    }
    for (const sid of live) {
      if (streams.current.has(sid)) continue;
      const es = new EventSource(
        `/api/review/sessions/${encodeURIComponent(sid)}/events`,
      );
      es.onmessage = (m) => {
        let ev: SessionEvent;
        try {
          ev = JSON.parse(m.data) as SessionEvent;
        } catch {
          return; // malformed frame — the poll still covers
        }
        setSessions((prev) => applySessionEvent(prev, ev));
        if (!isLiveRun(ev.status)) {
          // Final frame: the server closes after it. Close our side and poll
          // now so completion side-effects (RESULT refresh, agent delivery)
          // fire immediately instead of on the next scheduled tick.
          es.close();
          streams.current.delete(sid);
          tick();
        }
      };
      es.onerror = () => {
        es.close();
        streams.current.delete(sid); // poll = fallback; next tick retries
      };
      streams.current.set(sid, es);
    }
  }, [sessions, tick]);
  useEffect(() => {
    const open = streams.current;
    return () => {
      for (const es of open.values()) es.close();
      open.clear();
    };
  }, []);

  // Agents + factory builds scoped to the pane's selection. Only the LIVE
  // ones render now (the "Working now" strip): finished work is the runs
  // log's job, and it carries a number you can point at. Factory builds carry
  // a project_id — under a selected project they scope to it; under 'all work'
  // (and for dispatched agents, which carry none) everything shows.
  const visibleAgents = agents.filter(
    (a) =>
      !isPoolRun(a.task_type) ||
      projectId == null ||
      a.project_id == null ||
      a.project_id === projectId,
  );
  const runningAgents = visibleAgents.filter((a) => isLiveStatus(a.status));

  return (
    // The book's right side (src/book/), under its "This page" section: no
    // header and no column of its own.
    <aside className={cn("flex min-h-0 w-full flex-col", className)} data-chrome="page">
      <div className="min-h-0 flex-1 overflow-y-auto">
        {benchSlot?.(sessions)}

        {projectId != null && modules != null && (
          <ModulesSection data={modules} onChanged={tick} projectId={projectId} />
        )}

        {projectId != null && phases.length > 0 && (
          <>
            <SectionLabel className="mt-2">
              <ListChecksIcon className="size-3.5" />
              Plan
            </SectionLabel>
            <PlanByModule phases={phases} projectId={projectId} />
          </>
        )}

        {/* Working now — the live strip. Only work IN FLIGHT: a dispatched
            agent has no harness_run row, so without this a running dispatch
            would be invisible here, and "never leave the user wondering what's
            happening" is the standing bar. Finished agents drop out; their
            report is in the chat thread where it was delivered. */}
        {runningAgents.length > 0 && (
          <>
            <SectionLabel className={phases.length > 0 ? "mt-2" : undefined}>
              <BotIcon className="size-3.5" />
              Working now
              <Badge className="ml-1 animate-pulse" variant="secondary">
                live
              </Badge>
            </SectionLabel>
            {runningAgents.map((a) => (
              <AgentCard a={a} key={String(a.task_id)} onChanged={tick} />
            ))}
          </>
        )}

        {err && <p className="px-4 py-2 text-destructive text-xs">{err}</p>}

        {timers.length > 0 && (
          <>
            <SectionLabel className="mt-2">Coming back to this</SectionLabel>
            {timers.map((t) => (
              <TimerCard key={t.event_id} onCancelled={tick} t={t} />
            ))}
          </>
        )}

        <RunsLog
          className={
            phases.length > 0 || runningAgents.length > 0 ? "mt-2" : undefined
          }
          runs={benchRuns}
        />

        {projectId != null && (
          <CodeApprovalsSection
            onAnnounced={onApprovalAnnounced}
            onOpen={(n) => onOpenApproval?.(n)}
            projectId={projectId}
            tick={approvalsTick}
          />
        )}

        {projectId != null && (
          <>
            <SectionLabel className="mt-2">Ship this project</SectionLabel>
            <PublishSection projectId={projectId} />
          </>
        )}
      </div>
    </aside>
  );
}

/** A timer the host set for itself, and WHEN it fires.
 *
 * The host can set a timer for itself, and that time is visible. The scenario is a build landing on an app
 * that is still booting — the host looks, sees it mid-boot, and says it will
 * check again later. Before this the only way that promise could be kept was
 * the user manually waking the host, and the only place a pending timer
 * rendered was the Agents app, not the bench the build is on.
 *
 * The absolute local time is what is shown, not "in about 20 minutes": the
 * user needs to know whether to wait or walk away, and a relative string
 * stops being true the moment the tab sits idle.
 */
function TimerCard({
  t,
  onCancelled,
}: {
  t: ScheduledTimer;
  onCancelled: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const at = t.next_fire_at ? new Date(t.next_fire_at) : null;
  const overdue = at != null && at.getTime() < Date.now();
  return (
    <div className="mx-3 mb-1 rounded-md border border-border px-3 py-2">
      <div className="flex items-start gap-2">
        <ClockIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-xs">{t.title}</p>
          <p className="text-muted-foreground text-xs">
            {at == null
              ? "no fire time recorded"
              : overdue
                ? // Due but not yet run: the runner polls every 30s, so this
                  // is normal for a moment and stuck if it persists. Say
                  // which, rather than showing a time that has passed.
                  `due ${at.toLocaleTimeString()} — running shortly`
                : `checks back at ${at.toLocaleTimeString()}`}
            {t.recurrence ? ` · ${t.recurrence}` : ""}
            {t.lane === "op" ? " · no model runs" : ""}
          </p>
        </div>
        <Button
          className="h-6 shrink-0 px-2 text-xs"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            await cancelScheduled(t.event_id);
            setBusy(false);
            onCancelled();
          }}
          size="sm"
          variant="ghost"
        >
          {busy ? <Spinner className="size-3" /> : "Cancel"}
        </Button>
      </div>
    </div>
  );
}

/** One plan phase — status, progress, and a fold that lazily loads its steps
 * (fresh on every open, so a running build's step flips show up). */
function PhaseCard({ projectId, p: pIn }: { projectId: number; p: Phase }) {
  const [open, setOpen] = useState(false);
  const [steps, setSteps] = useState<PhaseStep[] | null>(null);
  // The user's approval lands here optimistically (the pane's next poll
  // confirms it from the server); the button must never stay clickable after
  // the row it acts on has moved.
  const [override, setOverride] = useState<Partial<Phase> | null>(null);
  const [approving, setApproving] = useState(false);
  const [approveErr, setApproveErr] = useState("");
  const p: Phase = override ? { ...pIn, ...override } : pIn;
  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    setSteps(null);
    const r = await fetchPhaseSteps(projectId, p.id);
    setSteps(r.ok ? (r.data.steps ?? []) : []);
  };
  const total = p.step_count ?? 0;
  const done = p.steps_done ?? 0;
  // v124: gated + unapproved -> "Approve" makes the phase buildable
  // (server/routes/projects.py approve_phase). Nothing else has a click.
  // A phase that landed ('complete', or an old 'review' row) just shows as
  // landed; acceptance checks are information only.
  const awaitingApproval = p.gate === "user" && !p.approved_at;
  const landed = p.status === "complete" || p.status === "review";
  const checksNote =
    landed && (p.acceptance_checks ?? 0) > 0
      ? `checks: ${p.acceptance_checks} listed`
      : "";
  const proofLabel = awaitingApproval
    ? "awaiting your approval"
    : p.gate === "user" && p.approved_at && !landed
      ? "approved"
      : checksNote;
  const approve = async () => {
    setApproving(true);
    setApproveErr("");
    const r = await approvePhase(projectId, p.id);
    setApproving(false);
    if (!r.ok) {
      setApproveErr(r.message || "approve failed");
      return;
    }
    setOverride({
      status: r.data.status,
      approved_at: new Date().toISOString(),
    });
  };
  return (
    <div className="mx-3 mb-1.5 rounded-md border border-border px-2.5 py-1.5">
      <button
        className="flex w-full items-center gap-2 text-left"
        onClick={toggle}
        type="button"
      >
        <span className="font-mono text-muted-foreground text-xs">
          {p.phase_number}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm">{p.title}</span>
        <span className={cn("shrink-0 text-xs", statusTone(p.status))}>
          {p.status === "review" ? "complete" : p.status}
          {total > 0 ? ` · ${done}/${total}` : ""}
        </span>
        <ChevronDownIcon
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {(proofLabel || approveErr) && (
        <div className="mt-1 flex items-center gap-2">
          <span
            className={cn(
              "text-[11px]",
              awaitingApproval
                ? "text-warning"
                : "text-muted-foreground",
            )}
          >
            {proofLabel}
          </span>
          {awaitingApproval && (
            <Button
              className="h-6 px-2 text-[11px]"
              disabled={approving}
              onClick={approve}
              size="sm"
              title="Allow this phase to build. Nothing builds it until you do."
              type="button"
              variant="outline"
            >
              {approving ? "…" : "Approve"}
            </Button>
          )}
          {approveErr && (
            <span className="text-[11px] text-destructive">{approveErr}</span>
          )}
        </div>
      )}
      {((p.waits_for?.length ?? 0) > 0 || p.harness || p.integration_error) && (
        <div className="mt-1 flex items-center gap-2 text-[11px]" data-split>
          {(p.waits_for?.length ?? 0) > 0 && (
            <span className="text-muted-foreground" data-waits-for>
              after {p.waits_for?.join(", ")}
            </span>
          )}
          {p.harness && (
            <span className="text-muted-foreground" data-engine>
              {p.harness}
              {p.model ? ` · ${p.model}` : ""}
            </span>
          )}
          {p.integration_error && (
            <span
              className="text-warning"
              data-split-conflict
              title={p.integration_error}
            >
              did not land — collided with another phase
            </span>
          )}
        </div>
      )}
      {open && (
        <div className="mt-1.5 flex flex-col gap-0.5 border-border border-t pt-1.5">
          {steps === null ? (
            <p className="text-muted-foreground text-xs">loading steps…</p>
          ) : steps.length === 0 ? (
            <p className="text-muted-foreground text-xs">no steps yet</p>
          ) : (
            steps.map((s) => (
              <p className="flex items-center gap-2 text-xs" key={s.id}>
                <span className="font-mono text-muted-foreground">
                  {p.phase_number}.{s.step_number}
                </span>
                <span className="min-w-0 flex-1 truncate">{s.title}</span>
                <span className={cn("shrink-0", statusTone(s.status))}>
                  {s.status}
                </span>
              </p>
            ))
          )}
        </div>
      )}
    </div>
  );
}



/**
 * One-click "Publish as site" — the funnel's publish leg. The kernel zips the
 * project's built web output server-side; nobody handles a zip. A new site
 * goes live public (the private look at a build is its artifact); its owner sets other visibility in the Sites app.
 */
function PublishSection({ projectId }: { projectId: number }) {
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<SitePublishResult | null>(null);
  // "Install as my app" used to live inside the RESULT section. That section is
  // gone (it framed a stale project dir and rendered a raw JSON 404), but the
  // ACTION is not part of what was wrong with it — both buttons here are
  // dispositions of a finished project, so this is where it belongs.
  const [installBusy, setInstallBusy] = useState(false);
  const [installMsg, setInstallMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const install = async () => {
    if (installBusy) return;
    setInstallBusy(true);
    setInstallMsg(null);
    const r = await buildFromProject(projectId);
    setInstallBusy(false);
    setInstallMsg(
      r.ok
        ? { ok: true, text: `Installed as your app (${r.data.id})` }
        : { ok: false, text: r.message },
    );
  };

  const submit = async () => {
    const s = slug.trim().toLowerCase();
    if (!s || busy) return;
    setBusy(true);
    setErr("");
    setDone(null);
    const r = await publishProjectAsSite(projectId, s);
    setBusy(false);
    if (!r.ok) {
      setErr(r.message);
      return;
    }
    setDone(r.data);
  };

  // The box is narrow on the right side (about 180 px, 150 on a phone), so
  // the placeholder is the field's name only and the address it makes is a
  // line of its own under the box.
  const slugHintId = `publish-slug-hint-${projectId}`;
  return (
    <div className="px-4 py-2">
      <div className="flex items-center gap-2">
        <Input
          aria-describedby={slugHintId}
          aria-label="Site name"
          className="h-8 text-sm"
          data-publish-slug
          placeholder="Site name"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <Button
          className="shrink-0 gap-1.5"
          disabled={!slug.trim() || busy}
          onClick={submit}
          size="sm"
        >
          {busy ? <Spinner className="size-4" /> : <GlobeIcon className="size-4" />}
          Publish as site
        </Button>
      </div>
      <p className="break-words pt-1 text-muted-foreground text-xs" data-publish-slug-hint id={slugHintId}>
        Its address becomes {slug.trim().toLowerCase() || "name"}.dev…
      </p>
      {err && <p className="pt-2 text-destructive text-xs">{err}</p>}
      {done && (
        <div className="pt-2 text-xs">
          <p>
            Live at{" "}
            <a
              className="font-medium text-primary underline"
              href={`https://${done.hostname}/`}
              rel="noreferrer"
              target="_blank"
            >
              {done.hostname}
            </a>{" "}
            ({done.runtime})
          </p>
          <p className="pt-1 text-muted-foreground">
            {done.staging_note ??
              "live — a new site is public; set who can see it in the Sites app"}
          </p>
        </div>
      )}
      {!done && !err && (
        <p className="pt-1.5 text-muted-foreground text-xs">
          Publishes this project's built output as a public site. Set who
          can see it in Sites.
        </p>
      )}
      <div className="mt-2 flex items-center gap-2 border-border border-t pt-2">
        <Button
          className="shrink-0 gap-1.5"
          disabled={installBusy}
          onClick={install}
          size="sm"
          variant="outline"
        >
          {installBusy ? (
            <Spinner className="size-4" />
          ) : (
            <PackageIcon className="size-4" />
          )}
          Install as my app
        </Button>
        {installMsg && (
          <p
            className={cn(
              "min-w-0 flex-1 text-xs",
              installMsg.ok ? "text-muted-foreground" : "text-destructive",
            )}
          >
            {installMsg.text}
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * RunsLog — the numbered runs log: a collapsible log on the right to run down
 * the numbered runs and open each app, so a run can be referenced by number
 * in the conversation.
 *
 * Replaces the old Runs section (per-session RunCards + finished AgentCards),
 * which listed the same work in a shape nobody could point at. Every entry is
 * ONE harness_run row, labelled with its id. Two properties make that number
 * worth printing:
 *   - it is per-VARIANT, so a 7-harness fan-out is 7 addressable rows — the
 *     unit the user actually has an opinion about ("I like that design");
 *   - it is stable forever (curation strips transcripts, never rows), so a
 *     number said tomorrow still resolves.
 * The host reads the same number: harness_transcript(run_id=…).
 *
 * Expanding a row mounts the SAME RunTerminal the chat feed uses — identical
 * scrollback and the identical "open app" button, not a second rendering of
 * the truth. The chat copy stays exactly where it was.
 */
function RunsLog({ runs, className }: { runs: BenchRun[]; className?: string }) {
  // Closed by default: the modules above are the unit now, and
  // the log is where a number is looked up, not where the eye starts.
  const [open, setOpen] = useState(false);
  return (
    <Collapsible className={className} onOpenChange={setOpen} open={open}>
      <CollapsibleTrigger className="group flex w-full items-center gap-1 px-4 pt-3 pb-1 text-left font-medium text-muted-foreground text-xs uppercase tracking-wide">
        <HammerIcon className="size-3.5" />
        Runs
        {runs.length > 0 && (
          <span className="font-normal normal-case tracking-normal">
            ({runs.length})
          </span>
        )}
        <ChevronDownIcon className="ml-auto size-3.5 transition-transform group-data-[state=closed]:-rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        {runs.length === 0 ? (
          <p className="px-4 py-2 text-muted-foreground text-xs">
            No runs yet. Every coding run the host starts lands here with a
            number you can point at in chat.
          </p>
        ) : (
          runs.map((r) => <RunLogRow key={r.id} r={r} />)
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

function RunLogRow({ r }: { r: BenchRun }) {
  const [open, setOpen] = useState(false);
  // Archived rows kept their identity but lost their transcript to curation —
  // the terminal would mount empty, so say so instead of showing a blank.
  const stripped = r.status === "archived";
  return (
    <Collapsible
      className="mx-3 mb-1 rounded-md border border-border"
      onOpenChange={setOpen}
      open={open}
    >
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-2.5 py-1.5 text-left">
        {/* The number IS the affordance — tabular so the column scans, and
            selectable so it can be copied straight into a message. */}
        <span className="shrink-0 select-text font-medium font-mono text-muted-foreground text-xs tabular-nums">
          #{r.id}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs">
          {r.harness}
          {r.model ? (
            <span className="text-muted-foreground"> · {r.model}</span>
          ) : null}
        </span>
        <span
          className={cn(
            "shrink-0 text-[11px]",
            r.ok ? "text-muted-foreground" : "text-destructive",
          )}
        >
          {r.ok ? "ok" : "failed"}
        </span>
        {r.elapsed_s > 0 && (
          <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">
            {r.elapsed_s >= 60
              ? `${Math.round(r.elapsed_s / 60)}m`
              : `${Math.round(r.elapsed_s)}s`}
          </span>
        )}
        <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-[state=closed]:-rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent className="px-2.5 pb-2">
        {stripped ? (
          <p className="py-1 text-muted-foreground text-xs">
            Archived — this run's outcome is kept, its scrollback was stripped
            to reclaim space. Still referenceable as run #{r.id}.
          </p>
        ) : (
          // Every run previews its OWN worktree — a run's result
          // is what that run built, never the project's last landed state.
          <RunTerminal
            harness={r.harness}
            sessionId={r.session_id}
            startOpen={false}
          />
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

function SectionLabel({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-1 px-4 pt-3 pb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide",
        className,
      )}
    >
      {children}
    </div>
  );
}

function statusTone(s?: string) {
  switch (s) {
    case "running":
    case "queued":
    case "in_progress":
    // phase/step in-flight vocabulary (project_phases / phase_steps)
    case "speccing":
    case "implementing":
    case "checking":
    // ladder: a passing rung parked on the human gate (ladder.py _climb)
    case "awaiting_review":
      return "text-warning";
    case "done":
    case "completed":
    case "complete":
    // old rows from before the proving step was removed: landed
    case "review":
    case "accepted":
      return "text-success";
    case "failed":
    case "timed_out":
    // ladder terminal states: every rung spent / the climb crashed
    case "exhausted":
    case "error":
      return "text-destructive";
    default:
      return "text-muted-foreground";
  }
}

/** A dispatched background agent — delegated work the user can WATCH and
 * KILL (the Circles contract). A RUNNING agent shows its live tool-by-tool
 * activity feed (the rolling `activity` array the registry persists); a
 * finished one opens with its actual output (or failure) prominent, so
 * "what is/did my agent do(ing)" is always answerable from the bench.
 * Field names match the registry API (_row_to_agent): result/error/verdict —
 * the old output/failure keys never existed on this endpoint. */
function AgentCard({ a, onChanged }: { a: AgentRow; onChanged: () => void }) {
  const [killing, setKilling] = useState(false);
  const live = isLiveStatus(a.status);
  // Finished work opens with its output visible (prominent by default);
  // running agents show the live feed and fold the rest behind a click.
  const [open, setOpen] = useState(!live);
  useEffect(() => {
    // Auto-open the output the moment a watched agent finishes.
    if (!live) setOpen(true);
  }, [live]);
  const kill = async () => {
    if (killing || a.task_id == null) return;
    setKilling(true);
    await cancelAgent(a.task_id);
    setKilling(false);
    onChanged();
  };
  const failure = (a.error || "").trim();
  const body = (failure || a.result || "").trim();
  const activity = a.activity ?? [];
  const feed = live ? activity.slice(-6) : open ? activity : [];
  const blocked = a.verdict === "blocked";
  // A build on the harness pool: rendered as a build, no veto — cancelling
  // the row wouldn't stop the pool run (the backend refuses it too). A
  // fan-out row (several harnesses on one task) is the same
  // kind of thing: pool work the row merely mirrors.
  const isBuild = isPoolRun(a.task_type);
  const isFanOut = a.task_type === "fan_out";
  const violations = a.violations ?? [];
  return (
    <div className="mx-3 mb-2 rounded-md border border-border border-l-2 border-l-primary px-2.5 py-2">
      <div className="flex items-center gap-2">
        {isBuild ? (
          <HammerIcon className="size-4 shrink-0 text-primary" />
        ) : (
          <BotIcon className="size-4 shrink-0 text-primary" />
        )}
        <button
          className="min-w-0 flex-1 text-left"
          onClick={() => setOpen((v) => !v)}
          type="button"
        >
          <p className="truncate text-sm">
            {a.title || `${isFanOut ? "compare" : isBuild ? "build" : "agent"} ${a.task_id}`}
          </p>
          <p className={cn("text-xs", statusTone(a.status))}>
            {isBuild
              ? `${isFanOut ? "compare" : "build"}${a.harness ? ` on ${a.harness}` : ""} · `
              : ""}
            {a.status}
            {a.verdict && !live ? ` · ${a.verdict}` : ""}
            {a.tokens ? ` · ${Number(a.tokens).toLocaleString()} tok` : ""}
            {/* A FINISHED agent with a null cost is unpriced, not free. Agent
                runs are priced from the model catalog and come back null when
                it holds no published rate for the model that ran
                (server/routes/inference.py estimate_call_cost_usd, called by
                brain/mastercode/runtime/agent_dispatch.py _est_cost — never a
                hardcoded table that invents a rate rather than admit it). An
                empty cell is the one thing that cannot mean, because a
                person reads it as $0. Explicit null only: a row still running,
                or an older record without the field, has not REPORTED a cost
                yet, which is not the same as having failed to price one. */}
            {a.est_cost_usd
              ? ` · $${Number(a.est_cost_usd).toFixed(4)}`
              : a.est_cost_usd === null && !live
                ? " · unpriced"
                : ""}
          </p>
        </button>
        {live &&
          !isBuild &&
          (killing ? (
            <Spinner className="size-3.5" />
          ) : (
            <Button
              className="size-6 text-destructive hover:text-destructive"
              onClick={kill}
              size="icon"
              title="Kill this agent"
              variant="ghost"
            >
              <SquareIcon className="size-3.5" />
            </Button>
          ))}
      </div>
      {a.status === "cancelled" && (
        <p className="mt-1 text-muted-foreground text-xs">
          killed by{" "}
          {a.cancelled_by === "user_veto"
            ? "you (veto)"
            : a.cancelled_by === "entity_tasks_tool"
              ? "the host (task-queue tool)"
              : (a.cancelled_by ?? "unknown — pre-audit record")}
        </p>
      )}
      {blocked && (a.denied_tools?.length ?? 0) > 0 && (
        // The reason comes from the record (scoped_agent.blocked_reason): a
        // 'blocked' verdict can be a code-exec tool stripped, a call outside
        // the grant, or a grant too narrow for the tool — one fixed caption
        // here mislabelled a grant refusal as "code work".
        // Older records carry no reason; say so and
        // let the expanded row (the result text) speak.
        <p className="mt-1 text-destructive text-xs">
          blocked: {a.denied_tools?.join(", ")} —{" "}
          {(a.blocked_reason ?? "").trim() ||
            "refused before or during the run — expand the row for the reason"}
        </p>
      )}
      {isBuild && !live && violations.length > 0 && (
        <p className="mt-1 text-warning text-xs">
          {violations.length} build-contract violation
          {violations.length === 1 ? "" : "s"} left: {violations.join(" · ")}
        </p>
      )}
      {/* The save point. A phase that commits and lands in the rolling PR used
          to leave no trace on the bench, which reads as "the build did
          nothing". Say the commit and the PR, or say plainly why neither
          happened — never nothing. */}
      {isBuild && !live && a.repo_sync && (
        <p className="mt-1 text-muted-foreground text-xs">
          {a.repo_sync.committed && a.repo_sync.pushed ? (
            <>
              <span className="text-success">✓</span> committed{" "}
              <span className="font-mono">{a.repo_sync.commit}</span>
              {a.repo_sync.pr ? (
                <>
                  {" "}
                  to{" "}
                  {a.repo_sync.url ? (
                    <a
                      className="underline"
                      href={a.repo_sync.url}
                      rel="noreferrer"
                      target="_blank"
                    >
                      PR #{a.repo_sync.pr}
                    </a>
                  ) : (
                    `PR #${a.repo_sync.pr}`
                  )}
                </>
              ) : null}
            </>
          ) : (
            <>
              <span className="text-warning">→</span> not committed:{" "}
              {a.repo_sync.error || a.repo_sync.note || "no save point"}
            </>
          )}
        </p>
      )}
      {isBuild && !live && a.auto_merge && (
        <p className="mt-1 text-xs">
          {a.auto_merge.merged ? (
            <span className="text-success">
              ✓ Auto-Approve merged Code Approval #{a.auto_merge.pr} — the plan
              is done and this is on your default branch
            </span>
          ) : (
            <span className="text-warning">
              Auto-Approve held off: {a.auto_merge.reason}
            </span>
          )}
        </p>
      )}
      {feed.length > 0 && (
        <div className="mt-2 flex flex-col gap-0.5">
          {live && (
            <p className="text-muted-foreground text-xs uppercase tracking-wide">
              live activity
            </p>
          )}
          {feed.map((ev, i) => (
            <p
              className="truncate font-mono text-muted-foreground text-xs"
              key={`${ev.at}-${ev.event}-${i}`}
            >
              <span className={ev.event === "result" ? "text-success" : "text-warning"}>
                {ev.event === "result" ? "✓" : "→"}
              </span>{" "}
              {ev.tool}
              {ev.event === "start" && ev.args && ev.args !== "{}"
                ? ` ${ev.args}`
                : ""}
              {ev.event === "result" && ev.status && ev.status !== "ok"
                ? ` — ${ev.status}`
                : ""}
            </p>
          ))}
        </div>
      )}
      {open && (
        <pre className={cn(
          "mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-background/80 p-2 font-mono text-xs",
          failure ? "text-destructive" : "",
        )}>
          {body || (live ? "(running — output lands here when it finishes)" : "(no output recorded)")}
        </pre>
      )}
    </div>
  );
}




// How often the list re-reads the forge while a project is open: a person can
// approve in the platform's window long after the card that opened it closed,
// and the chat line for that merge is asked for from here.
const APPROVALS_POLL_MS = 30_000;

/** The project's Code Approvals — its pull requests, in plain English.
 * Each row opens the
 * approval card over the chat; nothing here merges. */
function CodeApprovalsSection({
  projectId,
  tick,
  onOpen,
  onAnnounced,
}: {
  projectId: number;
  onAnnounced?: (projectId: number, number: number) => void;
  tick: number;
  onOpen: (number: number) => void;
}) {
  const [rows, setRows] = useState<CodeApproval[] | null>(null);
  const [connected, setConnected] = useState(true);
  const [where, setWhere] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Held in a ref so an inline callback from the parent never restarts the poll.
  const onAnnouncedRef = useRef(onAnnounced);
  onAnnouncedRef.current = onAnnounced;

  useEffect(() => {
    let alive = true;
    setRows(null);
    const load = async () => {
      const r = await fetchApprovals(projectId);
      if (!alive) return;
      if (!r.ok) {
        setErr(r.message);
        setRows((cur) => cur ?? []);
        return;
      }
      setErr(null);
      setConnected(r.data.connected !== false);
      setWhere(r.data.on_machine && r.data.machine ? r.data.machine : null);
      const list = r.data.approvals ?? [];
      setRows(list);
      // The server marks the merges whose chat line is owed (merged since the
      // project first listed its approvals, not yet announced).
      for (const a of list) {
        if (!a.announce) continue;
        void announceApproval(projectId, a.number).then((r) => {
          if (r.ok && r.data.announced) onAnnouncedRef.current?.(projectId, a.number);
        });
      }
    };
    void load();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, APPROVALS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [projectId, tick]);

  const waiting = (rows ?? []).filter((a) => a.state === "waiting").length;

  return (
    <>
      <SectionLabel className="mt-2">
        Code Approvals
        {waiting > 0 && (
          <Badge className="ml-1 h-4 px-1.5 text-[10px]" variant="default">
            {waiting} waiting
          </Badge>
        )}
      </SectionLabel>
      <div className="px-3 pb-4">
        {!connected ? (
          <p className="px-1 py-1 text-muted-foreground text-xs">
            The Host has no forge login yet, so there is nothing to approve here.
          </p>
        ) : rows == null ? (
          <p className="px-1 py-1 text-muted-foreground text-xs">loading…</p>
        ) : rows.length === 0 ? (
          <p className="px-1 py-1 text-muted-foreground text-xs">
            {err ?? "Nothing waiting for you. When the Host has a change ready, it appears here."}
          </p>
        ) : (
          rows.map((a) => (
            <button
              className="mb-1.5 block w-full rounded-md border border-border px-2.5 py-2 text-left hover:border-primary"
              key={a.number}
              onClick={() => onOpen(a.number)}
              type="button"
            >
              <p className="truncate text-sm">{a.title}</p>
              {a.summary && a.summary !== a.title && (
                <p className="truncate text-muted-foreground text-xs">{a.summary}</p>
              )}
              <p className="mt-1 flex flex-wrap items-center gap-1 text-muted-foreground text-xs">
                <Badge variant={a.state === "waiting" ? "default" : "secondary"}>
                  {a.state_words}
                </Badge>
                {!a.ready && (a.state === "waiting" || a.state === "changes_asked") && (
                  <Badge variant="destructive">checks found problems</Badge>
                )}
                {a.state === "approved" && a.merged_by && <span>by {a.merged_by}</span>}
                {a.models.length > 0 && <span className="truncate">· {a.models.join(", ")}</span>}
              </p>
            </button>
          ))
        )}
        {where && (
          <p className="px-1 pt-1 text-muted-foreground text-xs">
            This project's code lives on {where}.
          </p>
        )}
      </div>
    </>
  );
}

// ---- MODULES ----------------------------------------------------------------

const TARGET_WORDS: Record<string, string> = {
  private_app: "private app",
  public_site: "public site",
  store_app: "store app",
  component: "component",
  op: "op — a program for one of your machines",
};

function openApp(appId: string) {
  // The same cseries:open-app message the host's open_app call is relayed as
  // (lib/stream.ts) — opens the app the way the desktop does.
  (window.parent || window).postMessage({ type: "cseries:open-app", app_id: appId }, window.location.origin);
}

/** Installed / running — OBSERVED by the platform (app_state), never inferred.
 * A button only when there is an installed app to open. */
function AppBadge({ app }: { app: AppState | null | undefined }) {
  if (!app?.app_id) return null;
  if (!app.installed) {
    return (
      <span
        className="shrink-0 rounded-full border border-border px-1.5 text-[10px] text-muted-foreground"
        data-app-badge="not-installed"
        title={app.manifest_error ? `app.yaml does not load: ${app.manifest_error}` : "not installed — nothing runs yet"}
      >
        not installed
      </span>
    );
  }
  return (
    <button
      className={cn(
        "shrink-0 rounded-full px-1.5 text-[10px]",
        app.running ? "bg-success text-primary-foreground hover:bg-success/90" : "border border-success/50 text-success",
      )}
      data-app-badge={app.running ? "running" : "installed"}
      onClick={() => app.app_id && openApp(app.app_id)}
      title={app.running ? "installed and running — open its window" : "installed, not running — opening its window starts it"}
      type="button"
    >
      {app.running ? "running" : "installed"}
    </button>
  );
}

function ModulesSection({
  data,
  projectId,
  onChanged,
}: {
  data: ModulesResult;
  projectId: number;
  onChanged: () => void;
}) {
  const [view, setView] = useState<VersionSrc | null>(null);
  return (
    <>
      <SectionLabel>
        <BoxesIcon className="size-3.5" />
        Modules
      </SectionLabel>
      {data.modules.length === 0 ? (
        <div className="mx-3 mb-1 flex items-center gap-2 rounded-md border border-border border-dashed px-2.5 py-1.5">
          <p className="min-w-0 flex-1 text-muted-foreground text-xs">
            One app at the project root — no modules yet. The host declares
            modules when a project needs more than one app.
          </p>
          <AppBadge app={data.root_app} />
        </div>
      ) : (
        data.modules.map((m) => (
          <ModuleCard
            key={m.name}
            m={m}
            onChanged={onChanged}
            onOpen={setView}
            projectId={projectId}
          />
        ))
      )}
      {view && <VersionView onClose={() => setView(null)} src={view} />}
    </>
  );
}

function ModuleCard({
  m,
  projectId,
  onOpen,
  onChanged,
}: {
  m: ModuleRow;
  projectId: number;
  onOpen: (src: VersionSrc) => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<ModuleVersion[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const cur = m.current_version;
  // Re-read the versions whenever the pointer or the count moves (a landing,
  // an undo, a pick), and whenever the card is opened.
  const stamp = `${cur?.number ?? "-"}/${m.version_count}`;
  useEffect(() => {
    if (!open) return;
    let alive = true;
    void (async () => {
      const r = await fetchModuleVersions(projectId, m.name);
      if (alive) setVersions(r.ok ? (r.data.versions ?? []) : []);
    })();
    return () => {
      alive = false;
    };
  }, [open, stamp, projectId, m.name]);

  const act = async (key: string, fn: () => Promise<{ ok: boolean; message?: string }>, done: string) => {
    setBusy(key);
    setNote(null);
    const r = await fn();
    setBusy(null);
    setNote(r.ok ? { ok: true, text: done } : { ok: false, text: r.message || "failed" });
    onChanged();
  };

  return (
    <div className="mx-3 mb-1.5 rounded-md border border-border px-2.5 py-1.5" data-module={m.name}>
      <button className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((v) => !v)} type="button">
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium text-sm">{m.name}</span>
          <span className="block truncate text-[11px] text-muted-foreground">
            {TARGET_WORDS[m.target] ?? m.target}
          </span>
        </span>
        <AppBadge app={m.app} />
        <span className="shrink-0 font-mono text-muted-foreground text-xs">
          {cur ? `v${cur.number}` : "no version"}
        </span>
        <ChevronDownIcon
          className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")}
        />
      </button>
      {!open && m.candidates.length > 0 && (
        <p className="mt-0.5 text-[11px] text-warning">
          {m.candidates.length} run{m.candidates.length === 1 ? "" : "s"} to compare — open to pick
        </p>
      )}
      {open && (
        <div className="mt-1.5 flex flex-col gap-0.5 border-border border-t pt-1.5">
          {m.candidates.length > 0 && (
            <>
              <p className="text-[11px] text-muted-foreground uppercase tracking-wide">Candidates</p>
              {m.candidates.map((c) => (
                <div className="flex items-center gap-2 text-xs" data-candidate={c.run_id} key={c.run_id}>
                  <span className="shrink-0 font-mono text-muted-foreground tabular-nums">#{c.run_id}</span>
                  <span className="min-w-0 flex-1 truncate">
                    {c.harness}
                    {c.model ? <span className="text-muted-foreground"> · {c.model}</span> : null}
                  </span>
                  <OpenVersionButton
                    onClick={() =>
                      onOpen({
                        kind: "run",
                        sessionId: c.session_id,
                        harness: c.harness,
                        label: `${m.name} · run #${c.run_id} (${c.harness}) — not a version yet`,
                      })
                    }
                  />
                  <Button
                    className="h-6 px-2 text-[11px]"
                    disabled={busy != null}
                    onClick={() =>
                      act(`adopt-${c.run_id}`, () => adoptRunInto(c.run_id, m.name), `run #${c.run_id} is now the current version of ${m.name}`)
                    }
                    size="sm"
                    title={`Land run #${c.run_id}'s files in ${m.name} as its next version (reversible)`}
                    variant="outline"
                  >
                    {busy === `adopt-${c.run_id}` ? <Spinner className="size-3" /> : "use as version"}
                  </Button>
                </div>
              ))}
            </>
          )}
          <p className={cn("text-[11px] text-muted-foreground uppercase tracking-wide", m.candidates.length > 0 && "mt-1")}>
            Versions
          </p>
          {versions === null ? (
            <p className="text-muted-foreground text-xs">loading versions…</p>
          ) : versions.length === 0 ? (
            <p className="text-muted-foreground text-xs">
              no version yet — a build into {m.name}, or a run you use, becomes version 1
            </p>
          ) : (
            versions.map((v) => (
              <div className="flex items-center gap-2 text-xs" data-version={v.number} key={v.number}>
                <span className="shrink-0 font-medium font-mono tabular-nums">v{v.number}</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground" title={v.note}>
                  {v.source}
                  {v.run_id ? ` · run #${v.run_id}` : ""}
                  {v.commit ? ` · ${v.commit}` : ""}
                  {v.undone ? " · undone" : ""}
                </span>
                {v.current && (
                  <span className="shrink-0 rounded-full bg-primary/10 px-1.5 text-[10px] text-primary">current</span>
                )}
                {v.restorable ? (
                  <OpenVersionButton
                    onClick={() =>
                      onOpen({
                        kind: "module",
                        projectId,
                        module: m.name,
                        version: v.number,
                        label: `${m.name} · version ${v.number}`,
                      })
                    }
                  />
                ) : (
                  <span
                    className="shrink-0 text-[10px] text-muted-foreground"
                    title="Only the newest 10 versions and the current one keep their files; this one keeps its record and commit"
                  >
                    files pruned
                  </span>
                )}
                {!v.current && v.restorable && (
                  <Button
                    className="h-6 px-2 text-[11px]"
                    disabled={busy != null}
                    onClick={() =>
                      act(`cur-${v.number}`, () => setModuleVersion(projectId, m.name, v.number), `${m.name} is at version ${v.number}`)
                    }
                    size="sm"
                    title="Make this the module's current version — nothing is deleted"
                    variant="ghost"
                  >
                    {busy === `cur-${v.number}` ? <Spinner className="size-3" /> : "make current"}
                  </Button>
                )}
                {v.current && (
                  <Button
                    className="h-6 px-2 text-[11px]"
                    disabled={busy != null}
                    onClick={() => act("undo", () => undoModuleVersion(projectId, m.name), `${m.name}: version ${v.number} undone`)}
                    size="sm"
                    title="Go back to the version before this one — this one stays and can be made current again"
                    variant="ghost"
                  >
                    {busy === "undo" ? <Spinner className="size-3" /> : "undo"}
                  </Button>
                )}
              </div>
            ))
          )}
          {note && (
            <p className={cn("pt-1 text-[11px]", note.ok ? "text-muted-foreground" : "text-destructive")}>
              {note.text}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function OpenVersionButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-muted"
      data-open-version
      onClick={onClick}
      type="button"
    >
      open version
    </button>
  );
}

/** PLAN, grouped: module → phases. Phases with no module are
 * the project root's; a plan with no modules at all renders flat, as before. */
function PlanByModule({ phases, projectId }: { phases: Phase[]; projectId: number }) {
  const anyModule = phases.some((p) => p.module);
  if (!anyModule) {
    return (
      <>
        {phases.map((p) => (
          <PhaseCard key={p.id} p={p} projectId={projectId} />
        ))}
      </>
    );
  }
  const groups: { module: string | null; phases: Phase[] }[] = [];
  for (const p of phases) {
    const key = p.module ?? null;
    let g = groups.find((x) => x.module === key);
    if (!g) {
      g = { module: key, phases: [] };
      groups.push(g);
    }
    g.phases.push(p);
  }
  return (
    <>
      {groups.map((g) => (
        <div data-plan-module={g.module ?? "root"} key={g.module ?? "root"}>
          <p className="px-4 pt-1 pb-0.5 font-medium text-xs">{g.module ?? "project root"}</p>
          {g.phases.map((p) => (
            <PhaseCard key={p.id} p={p} projectId={projectId} />
          ))}
        </div>
      ))}
    </>
  );
}
