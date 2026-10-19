import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { elapsedWords, toolLabel, turnStep } from "@/components/chat/turn-status";
import {
  type AgentRow,
  type BackgroundActivity,
  fetchAgents,
  fetchBackgroundActivity,
  fetchWork,
  type WorkThread,
} from "@/lib/api";
import { pageOfTurnKey } from "@/lib/cards";
import type { ActiveTurn, ChatMessage } from "@/lib/types";
import { Pop, Working } from "./bits";

// The working line: one line above your Host's box that says, while the Host
// works, that it is working, what it is doing right now and for how long; and
// a panel behind it listing everything running (this page's turn, a turn on
// another page, each background agent) and any background work that failed.
// Nothing running and nothing failed: it takes no space at all.
//
// It replaces two things. The turn's own status line under the streaming
// reply, and the "<Host> is working" line under the conversation, were inside
// the scrolled conversation: scrolled up, or on a phone with the keyboard open
// and "Where this stands" above, neither was in sight. And the background
// ribbon above the box ("Background · unknown" and the like) showed every
// background call, done or not. The ribbon was also the only place a
// background role's FAILURE surfaced (fact derivation ran dead for 16 days in
// 2026-08 because nothing said so), so failures come here, and only failures.

/** Background work is minutes-scale; a turn's agents are read again the
 * moment the turn ends, so this poll is only the fallback. */
const POLL_MS = 15_000;
/** The newest failure the person put away with Dismiss, on this device. */
const SEEN_KEY = "circles.activity.failuresSeen";
const RUNNING = new Set(["queued", "in_progress", "running"]);

function readSeen(): number {
  try {
    return Number(window.localStorage.getItem(SEEN_KEY) || 0) || 0;
  } catch {
    return 0;
  }
}

/** "2m ago", "3h ago". */
function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

/** What the person can do about a failure, when the class says. */
function issueHint(row: BackgroundActivity): string | null {
  switch (row.error_class) {
    case "unconfigured":
      return "No model is routed for this. Pick one in the Orchestrator.";
    case "lane_unavailable":
      return "The provider was unreachable. It will be retried.";
    default:
      return null;
  }
}

/** Provider errors arrive as "{provider} {status}: {body}" with a JSON body
 * (provider_gateway builds them so). A person never reads raw JSON: the
 * vendor's own message is lifted out and the prefix that says who failed is
 * kept. An unreadable error still shows, as its prefix. */
export function readableError(raw: string | null): string {
  const text = (raw || "").trim();
  if (!text) return "failed";
  const brace = text.indexOf("{");
  if (brace < 0) return text;
  const prefix = text.slice(0, brace).trim();
  try {
    const body = JSON.parse(text.slice(brace)) as Record<string, unknown>;
    const err = body.error;
    const msg =
      typeof err === "string"
        ? err
        : err && typeof err === "object"
          ? ((err as Record<string, unknown>).message as string | undefined)
          : (body.message as string | undefined);
    if (typeof msg === "string" && msg.trim()) return prefix ? `${prefix} ${msg.trim()}` : msg.trim();
  } catch {
    /* clipped upstream: half an object */
  }
  return prefix || "failed";
}

/** What failed, in words. A call the server could not place
 * (server/routes/inference.py _activity_label on an empty source_module)
 * reads "Background · Unknown": that says nothing, so it names the model
 * that was called instead. */
export function failedWhat(row: BackgroundActivity): string {
  const src = (row.source_module || "").trim().toLowerCase();
  if (!src || src === "unknown") return row.model ? `A background call to ${row.model}` : "A background call";
  return `${row.group} · ${row.detail}`;
}

/** The internal thread a running turn is (its registry lane), if any. */
export function workOfTurn(work: ReadonlyArray<WorkThread>, threadKey: string | null | undefined): WorkThread | null {
  const key = threadKey ?? "";
  return work.find((w) => key.endsWith(`|${w.lane}`)) ?? null;
}

/** An internal thread's step: what its item says it is on, else its status. */
export function workStep(w: WorkThread): string {
  if (w.status === "done") return "done";
  if (w.status === "blocked") return "blocked";
  if (w.status === "open") return "waiting to start";
  return w.step || "working";
}

/** A running agent's step, from its live feed: the tool it started last. */
function agentStep(a: AgentRow): string {
  if (a.status === "queued") return "waiting to start";
  const last = (a.activity ?? []).at(-1);
  return last?.event === "start" && last.tool ? toolLabel(last.tool) : "working";
}

/** A reply's step, kept as its words arrive: "Writing" while they do, the
 * tool it runs, else "Thinking" (turnStep), ticking every second. */
function useLiveStep(message: ChatMessage | null): { step: string; now: number } {
  const [now, setNow] = useState(() => Date.now());
  const lastWordsAt = useRef(0);
  const len = message?.text.length ?? 0;
  const prev = useRef(len);
  useEffect(() => {
    if (len > prev.current) lastWordsAt.current = Date.now();
    prev.current = len;
  }, [len]);
  const live = !!message?.streaming;
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [live]);
  // Words this render draws that the effect above has not stamped yet
  // arrived just now. The stamp is a ref, and a ref changing draws nothing,
  // so without this the step kept its old word until the next second.
  const at = len > prev.current ? Date.now() : lastWordsAt.current;
  return { step: turnStep(message, at, now), now };
}

/** "Writing · ", "Reading a file · ": a beside reply's step in the panel. */
function BesideStep({ message }: { message: ChatMessage }) {
  const { step } = useLiveStep(message);
  return <span data-activity-beside-step>{step} · </span>;
}

/**
 * The working line of a reply streaming BESIDE the turn this window follows
 * (your Host's one conversation: a message on another topic, sent while a
 * turn ran, runs as its own turn at once). It sits under that reply, in the
 * conversation, so two topics running at once are both seen working, each
 * with its own step and clock; the line above the box stays the followed
 * turn's, and its panel lists both.
 */
export function BesideWorking({ hostName, message }: { hostName: string; message: ChatMessage }) {
  const { step, now } = useLiveStep(message);
  const secs = (now - Date.parse(message.createdAt)) / 1000;
  const on = message.topic?.title ? ` on ${message.topic.title}` : "";
  return (
    <div className="act beside" data-beside-working data-host-working>
      <Working label={`${hostName} is working${on}`} />
      <span className="who">{hostName} is working</span>
      <span className="step" data-activity-step>
        {on ? ` ·${on}` : ""} · {step}
      </span>
      {Number.isFinite(secs) && <span className="secs"> · {elapsedWords(secs)}</span>}
    </div>
  );
}

export function ActivityLine({
  hostName,
  pageId,
  message,
  lastReplyId,
  busy,
  turnId,
  turns,
  titleOf,
  onOpenPage,
  beside = [],
}: {
  /** Replies this window streams beside the followed turn, on other topics
   * (one conversation): their turns are listed with their steps. */
  beside?: ReadonlyArray<ChatMessage>;
  hostName: string;
  /** The page in front. */
  pageId: string | null;
  /** This window's streaming reply on the page in front, if any. */
  message: ChatMessage | null;
  /** The id of the last reply on screen, streaming or not: the streamed
   * reply still there when the stream stops means the turn ended here. */
  lastReplyId: string | null;
  /** This window's stream of the page's turn is live (submitted/streaming). */
  busy: boolean;
  /** The registry id of the turn this window streams (the turn frame's). */
  turnId: string | null;
  /** The platform's running turns, every page (GET /api/circles/turns/active):
   * a turn this window is not streaming (a reload mid-turn, a wake, another
   * window) still shows, with its own clock. */
  turns: ReadonlyArray<ActiveTurn>;
  titleOf: (pageId: string) => string;
  onOpenPage: (pageId: string) => void;
}) {
  // ── background agents and failures ──
  const [agents, setAgents] = useState<AgentRow[]>([]);
  // The internal threads the Host runs on its own (slice 6), each with its
  // work item's step.
  const [work, setWork] = useState<WorkThread[]>([]);
  // The failures in the window, newest first: the route lists them by
  // themselves (failed=true), so a failure followed by any number of calls
  // that went well is still here to be named. `issues` is the route's count
  // of every failure in the same window.
  const [rows, setRows] = useState<BackgroundActivity[]>([]);
  const [issues, setIssues] = useState(0);
  const [seen, setSeen] = useState(readSeen);
  const read = useCallback(async () => {
    const [a, b, w] = await Promise.all([fetchAgents(true), fetchBackgroundActivity(100, 24, true), fetchWork()]);
    if (a.ok) setAgents((a.data.agents ?? []).filter((x) => RUNNING.has(x.status)));
    if (w.ok) setWork(w.data.threads ?? []);
    // A backend without the route (404) or a failed read: nothing to say.
    if (b.ok) {
      setRows((b.data.activity ?? []).filter((r) => !r.ok));
      setIssues(Number(b.data.issues) || 0);
    }
  }, []);
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") void read();
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [read]);
  // An agent the turn dispatched shows once its dispatch has answered, and
  // stays after the turn ends; the turn's end is read at once too.
  const dispatched = message?.tools.filter((t) => t.name === "dispatch" && t.state !== "input-available").length ?? 0;
  useEffect(() => {
    if (!busy || dispatched) void read();
  }, [busy, dispatched, read]);
  // A thread's turn starting or ending is read at once (the turns poll).
  const laneTurns = turns.filter((t) => /\|task-\d+-t\d+$/.test(t.thread_key ?? "")).map((t) => t.turn_id).join(",");
  useEffect(() => {
    void read();
  }, [laneTurns, read]);
  // The amber state keys on `issues`. Of the failures, those newer than the
  // newest one Dismiss put away on this device (ids are BIGSERIAL) are new.
  // The list holds the newest 100; any it does not hold are older than every
  // one it does, so they are new only when every listed one is.
  const failures = useMemo(() => rows.filter((r) => r.id > seen), [rows, seen]);
  const unlisted = rows.length > 0 && failures.length === rows.length ? Math.max(0, issues - rows.length) : 0;
  const failCount = issues > 0 ? failures.length + unlisted : 0;
  const dismiss = () => {
    const top = Math.max(seen, ...rows.map((r) => r.id));
    try {
      window.localStorage.setItem(SEEN_KEY, String(top));
    } catch {
      /* private mode: for this page only */
    }
    setSeen(top);
    setOpen(false);
  };

  // ── the turn on the page in front ──
  // A turn this window streamed and saw END is over, whatever a poll read
  // before it ended still says. Only when it ended here: leaving the page
  // mid-turn stops this window's stream (the page's reset aborts it) while
  // the turn runs on, and the reply it streamed is gone from the screen
  // then; that turn is still running work, listed and counted. A turn this
  // window streams again (a reattach) is not over either.
  const ended = useRef<Set<string>>(new Set());
  const lastTurn = useRef<{ id: string; reply: string | null } | null>(null);
  if (busy && turnId) {
    lastTurn.current = { id: turnId, reply: message?.id ?? null };
    ended.current.delete(turnId);
  }
  useEffect(() => {
    if (busy || !lastTurn.current) return;
    const { id, reply } = lastTurn.current;
    lastTurn.current = null;
    if (reply && reply === lastReplyId) ended.current.add(id);
  }, [busy, lastReplyId]);
  const running = turns.filter((t) => t.status === "running" && !ended.current.has(t.turn_id));
  const pageTurn =
    running.find((t) => (busy && turnId ? t.turn_id === turnId : t.thread_key && pageOfTurnKey(t.thread_key) === pageId)) ??
    null;
  const working = busy || !!pageTurn;
  const hereTopic = pageTurn?.thread_key ? pageOfTurnKey(pageTurn.thread_key) : null;
  const startMs = pageTurn ? pageTurn.started_at * 1000 : message ? Date.parse(message.createdAt) : NaN;
  // Your Host's turns on other pages: the Host is working there, so the line
  // says so on this page too, and its panel is how to see them.
  // Internal threads' turns are listed with their work items, not as
  // conversations.
  const others = running.filter((t) => t !== pageTurn && !/\|task-\d+-t\d+$/.test(t.thread_key ?? ""));
  const working_threads = work.filter((w) => w.status === "open" || w.status === "working");
  const otherTitle = (t: ActiveTurn) => {
    const id = t.thread_key ? pageOfTurnKey(t.thread_key) : null;
    return id ? titleOf(id) : "another conversation";
  };

  // When words last arrived: the step says "Writing" only while they do,
  // and only while they are the newest thing the turn did. A tool starting
  // or answering after them means the words have stopped. Words a reattach
  // replays (a reload mid-turn, a wake) were written before it, however
  // fast they arrive: they never count, and until the stream's first live
  // frame no step is said at all (`message.replaying`).
  const replaying = !!message?.replaying;
  const lastWordsAt = useRef(0);
  const order = useRef({ n: 0, words: 0, tools: 0 });
  const wordsLen = message?.text.length ?? 0;
  const prevLen = useRef(wordsLen);
  useEffect(() => {
    if (wordsLen > prevLen.current && !replaying) {
      lastWordsAt.current = Date.now();
      order.current.words = ++order.current.n;
    }
    prevLen.current = wordsLen;
  }, [wordsLen, replaying]);
  const toolsSig = message ? message.tools.map((t) => t.state).join(",") : "";
  const prevSig = useRef(toolsSig);
  useEffect(() => {
    if (toolsSig) order.current.tools = ++order.current.n;
    prevSig.current = toolsSig;
  }, [toolsSig]);
  // The effects above stamp words and tools AFTER the render that draws
  // them, into refs, and a ref changing draws nothing. So this render reads
  // what they WILL say: a tool that changed in it (started or answered) is
  // the newest thing the turn did, words or not, since the effects order it
  // after any words; else words it carries that are not stamped yet arrived
  // just now. Without this the first words after a silence, and the first
  // live words after a reload, drew "Thinking", and a tool answering after
  // words drew "Writing", for up to a second, until the clock's next tick.
  const freshTool = !!toolsSig && toolsSig !== prevSig.current;
  const freshWords = wordsLen > prevLen.current && !replaying;
  const wordsAt = freshTool
    ? 0
    : freshWords
      ? Date.now()
      : order.current.words > order.current.tools
        ? lastWordsAt.current
        : 0;

  // A reply streaming beside whose turn the poll has not listed yet.
  const besideUnlisted = beside.filter((b) => b.streaming && !running.some((t) => t.turn_id === b.besideTurnId));
  const live =
    working || others.length > 0 || agents.length > 0 || besideUnlisted.length > 0 || working_threads.length > 0;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [live]);

  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useEffect(() => setOpen(false), [pageId]);

  if (!live && failCount === 0) return null;

  // The step is said only when this window's stream shows it. A turn here
  // this window is not streaming (another window's, or one a reload has not
  // reattached yet), or whose reattached stream has carried only its past so
  // far, says only that it runs, and for how long.
  const hereStep = busy && !replaying ? turnStep(message, wordsAt, now) : null;
  const step = working
    ? hereStep
    : others.length === 1
      ? `on ${otherTitle(others[0])}`
      : others.length > 1
        ? `on ${others.length} conversations`
        : besideUnlisted.length > 0
          ? `on ${besideUnlisted[0].topic?.title || "another topic"}`
          : working_threads.length > 0
            ? `${working_threads.length} thread${working_threads.length === 1 ? "" : "s"} on their own · ${workStep(working_threads[0])}`
            : agents.length === 1
          ? `agent: ${agents[0].title || "a background agent"}`
          : `${agents.length} background agents`;
  const secs = working
    ? Number.isNaN(startMs) ? null : (now - startMs) / 1000
    : others.length === 1
      ? now / 1000 - others[0].started_at
      : others.length === 0 && agents.length === 1 && typeof (agents[0] as { age_s?: number }).age_s === "number"
        ? ((agents[0] as { age_s?: number }).age_s as number)
        : null;
  const extra =
    (working || others.length > 0) && agents.length > 0 ? ` · +${agents.length} agent${agents.length === 1 ? "" : "s"}` : "";
  const failWords = `${failCount} background task${failCount === 1 ? "" : "s"} failed`;

  return (
    <div className="act-wrap" data-activity-wrap>
      <button
        aria-expanded={open}
        aria-label={live ? `${hostName} is working. What is running` : failWords}
        className={`act${live ? "" : " fail"}`}
        data-activity
        data-host-working={working || undefined}
        onClick={() => setOpen((v) => !v)}
        // The panel closes on a press outside it; this press toggles it.
        onMouseDown={(e) => e.stopPropagation()}
        type="button"
      >
        {live ? (
          <>
            <Working label={`${hostName} is working`} />
            <span className="who">{hostName} is working</span>
            {(step || extra) && (
              <span className="step" data-activity-step>
                {step ? ` · ${step}` : ""}
                {extra}
              </span>
            )}
            {secs != null && (
              <span className="secs" data-activity-elapsed>
                {" · "}
                {elapsedWords(secs)}
              </span>
            )}
            {failCount > 0 && (
              <span className="bad" title={failWords}>
                {" "}⚠ {failCount}
              </span>
            )}
          </>
        ) : (
          <span className="bad">⚠ {failWords}</span>
        )}
        <span aria-hidden className="caret">
          {open ? "▾" : "▴"}
        </span>
      </button>
      {open && (
        <Pop label={`What ${hostName} is doing`} onClose={close} style={{ bottom: "calc(100% + 6px)", left: 0, right: 0 }}>
          <div className="act-pop" data-activity-panel>
            <h4>What {hostName} is doing</h4>
            {!live && failCount === 0 ? <p>Nothing is running.</p> : null}
            {(working || others.length > 0) && (
              <ul>
                {working && (hereTopic ?? pageId) && (
                  <li data-activity-turn={hereTopic ?? pageId ?? undefined}>
                    {/* The followed turn's own topic (one conversation: the
                        person's topic may have moved on to one beside it). */}
                    <span className="t">{titleOf((hereTopic ?? pageId) as string)}</span>
                    <span className="d">
                      this page{hereStep ? ` · ${hereStep}` : ""}
                      {secs != null ? ` · ${elapsedWords(secs)}` : ""}
                    </span>
                  </li>
                )}
                {others.map((t) => {
                  const id = t.thread_key ? pageOfTurnKey(t.thread_key) : null;
                  const side = beside.find((b) => b.besideTurnId === t.turn_id);
                  return (
                    <li data-activity-turn={id ?? t.turn_id} key={t.turn_id}>
                      <span className="t">{id ? titleOf(id) : "Another conversation"}</span>
                      <span className="d">
                        {side ? <BesideStep message={side} /> : null}
                        {elapsedWords(now / 1000 - t.started_at)}
                        {id && (
                          <>
                            {" · "}
                            <button
                              className="link"
                              onClick={() => {
                                setOpen(false);
                                onOpenPage(id);
                              }}
                              type="button"
                            >
                              Open
                            </button>
                          </>
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {work.length > 0 && (
              <>
                <div className="k">Threads working on their own</div>
                <ul>
                  {work.map((w) => {
                    const turn = running.find((t) => (t.thread_key ?? "").endsWith(`|${w.lane}`));
                    return (
                      <li data-activity-work={w.item_id} key={w.item_id}>
                        <span className="t">
                          Thread {w.thread} of {w.of}: {w.goal}
                        </span>
                        <span className="d" data-activity-work-step>
                          {workStep(w)}
                          {turn ? ` · ${elapsedWords(now / 1000 - turn.started_at)}` : ""}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
            {agents.length > 0 && (
              <>
                <div className="k">Background agents</div>
                <ul>
                  {agents.map((a) => (
                    <li data-activity-agent={String(a.task_id)} key={String(a.task_id)}>
                      <span className="t">{a.title || `Agent #${a.task_id}`}</span>
                      <span className="d">{agentStep(a)}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {failCount > 0 && (
              <>
                <div className="k bad">Failed in the background</div>
                <ul>
                  {failures.map((r) => {
                    const hint = issueHint(r);
                    return (
                      <li data-activity-failure={r.id} key={r.id}>
                        <span className="t">
                          {failedWhat(r)}
                          <span className="d"> · {relTime(r.at)}</span>
                        </span>
                        {hint && <span className="d">{hint}</span>}
                        <span className="d">{readableError(r.error)}</span>
                        {r.failed_over_to ? <span className="d">Failed over to {r.failed_over_to}.</span> : null}
                      </li>
                    );
                  })}
                </ul>
                {unlisted > 0 && <p data-activity-unlisted>and {unlisted} earlier, in the last day</p>}
                <button className="btn quiet" data-activity-dismiss onClick={dismiss} type="button">
                  Dismiss
                </button>
              </>
            )}
          </div>
        </Pop>
      )}
    </div>
  );
}
