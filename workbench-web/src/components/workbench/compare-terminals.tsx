import { RunTerminal } from "@/components/workbench/run-terminal";
import { useRunFeeds } from "@/hooks/use-run-feed";
import { fetchAgents } from "@/lib/api";
import { fanOutSessionFromAgentRows, variantIdsFor } from "@/lib/fan-out";
import { modelOf } from "@/lib/run-feed";
import { clock } from "@/lib/run-steps";
import { epochOf, podNow, runSkew, runSummary, runTitle, sessionSeconds } from "@/lib/strands";
import { Columns2Icon, XIcon } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

/**
 * CompareTerminals — the compare view: the same task on N engines, N
 * terminals in the conversation, N "open version" buttons. A compare chip mounts it under
 * itself the way a single build chip mounts one RunTerminal: one inline
 * terminal PER VARIANT (live scrollback off /sessions/{id}/stream/{variant},
 * exit line when the terminal frame lands) and, once a variant lands, its own
 * "open version" rendering THAT variant's worktree — nothing has landed in
 * the project until the user picks, so the project's own preview would be
 * the wrong face.
 *
 * A compare once ran clean twice and the chat showed a chip and nothing else.
 * A fan-out build of n runs shows n live views and n open-app buttons.
 *
 * Resolves the session id in this order: the chip itself (live tool result
 * or persisted sessionId), else the compare registry row with the chip's
 * task id (GET /api/agents) — chips written before the chip carried
 * session_id only know their task id.
 *
 * The compare gets a run header (slice 4): what it is, how many engines
 * still run, the session clock; and an engine grid: "Side by side" lays the
 * cards out across the window.
 */
export function CompareTerminals({
  sessionId: sessionIdProp,
  taskId,
}: {
  sessionId: string | null;
  taskId?: string;
}) {
  const [sessionId, setSessionId] = useState<string | null>(sessionIdProp);
  const [variants, setVariants] = useState<string[] | null>(null);
  const [missing, setMissing] = useState<string | null>(null);
  // The run's start and end, off its manifest (review.py: time.time()), for
  // the book's session clock.
  const [times, setTimes] = useState<{ started_at?: unknown; completed_at?: unknown } | null>(null);

  // Fallback: a chip with no session id of its own → the registry row.
  useEffect(() => {
    if (sessionId || !taskId) return;
    let alive = true;
    (async () => {
      const r = await fetchAgents();
      if (!alive) return;
      const sid = r.ok ? fanOutSessionFromAgentRows(r.data.agents, taskId) : null;
      if (sid) setSessionId(sid);
      else setMissing(`compare ${taskId}: no session recorded on the row`);
    })();
    return () => {
      alive = false;
    };
  }, [sessionId, taskId]);

  // The variant list off the session manifest (Self-MoA samples are their
  // own variants). Re-polled while the list is still empty — a fan-out that
  // has just been queued may not have written per-harness state yet.
  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async (attempt: number) => {
      try {
        const r = await fetch(`/api/review/sessions/${encodeURIComponent(sessionId)}`, {
          headers: { Accept: "application/json" },
        });
        if (!alive) return;
        if (r.status === 401 && attempt < 3) {
          timer = setTimeout(() => load(attempt + 1), 120 * (attempt + 1));
          return;
        }
        if (!r.ok) {
          if (r.status === 404) setMissing(`run ${sessionId.slice(0, 8)}: no recorded session`);
          return;
        }
        const d = (await r.json()) as Parameters<typeof variantIdsFor>[0] & {
          manifest?: { started_at?: unknown; completed_at?: unknown };
        };
        const ids = variantIdsFor(d);
        if (!alive) return;
        setTimes({ started_at: d.manifest?.started_at, completed_at: d.manifest?.completed_at });
        if (ids.length > 0) setVariants(ids);
        else if (attempt < 20) timer = setTimeout(() => load(attempt + 1), 1500);
        else setMissing(`run ${sessionId.slice(0, 8)}: no harness variants recorded`);
      } catch {
        if (alive && attempt < 20) timer = setTimeout(() => load(attempt + 1), 2500);
      }
    };
    void load(0);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId]);

  if (missing) {
    return (
      <div className="mb-2 rounded-md border border-dashed px-3 py-2 text-muted-foreground text-xs">
        {missing}. It is still listed under Runs.
      </div>
    );
  }
  if (!sessionId || !variants) {
    return (
      <div className="mb-2 rounded-md border px-3 py-2 text-muted-foreground text-xs">
        loading compare{sessionId ? ` ${sessionId.slice(0, 8)}` : ""}…
      </div>
    );
  }
  const cards = variants.map((v) => <RunTerminal harness={v} key={`${sessionId}:${v}`} sessionId={sessionId} />);
  return (
    <BookCompare sessionId={sessionId} times={times} variants={variants}>
      {cards}
    </BookCompare>
  );
}

/** The compare on a page of the book: the run header over the cards, and the
 * engine grid when laid side by side across the window. The cards are
 * the same elements in both, so nothing reloads and no stream opens twice. */
function BookCompare({
  sessionId,
  variants,
  times,
  children,
}: {
  sessionId: string;
  variants: string[];
  times: { started_at?: unknown; completed_at?: unknown } | null;
  children: ReactNode;
}) {
  const [wide, setWide] = useState(false);
  const feeds = useRunFeeds(sessionId, variants);
  const models = feeds.map((f) => modelOf(f.frames));
  // A finished compare's manifest carries completed_at: its frames, still
  // loading, never set the clock running.
  const live = models.some((M) => M.live) && epochOf(times?.completed_at) == null;
  const skew = runSkew(feeds.map((f) => f.skew));
  const [deviceNow, setDeviceNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => setDeviceNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [live]);
  const now = podNow(live ? deviceNow : Date.now() / 1000, skew);
  const secs = sessionSeconds(times, models, now, live);
  useEffect(() => {
    if (!wide) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setWide(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [wide]);
  return (
    <section
      aria-label={runTitle(sessionId, variants)}
      className="book-compare"
      data-compare={sessionId}
      data-wide={wide || undefined}
    >
      <header className="book-compare-head">
        <div className="book-compare-id">
          <b>{runTitle(sessionId, variants)}</b>
          <span>{runSummary(models)}</span>
        </div>
        {secs != null && (
          <span className="book-compare-clock" data-session-clock title="Time since your Host sent the brief">
            {clock(secs)}
          </span>
        )}
        <button
          aria-pressed={wide}
          className="book-compare-wide"
          data-side-by-side
          onClick={() => setWide((w) => !w)}
          title={wide ? "Back to the page (Esc)" : "Lay the engines side by side across the window"}
          type="button"
        >
          {wide ? <XIcon className="size-3.5" /> : <Columns2Icon className="size-3.5" />}
          {wide ? "Back to the page" : "Side by side"}
        </button>
      </header>
      <div className="book-engines" data-engines={variants.length}>
        {children}
      </div>
    </section>
  );
}
