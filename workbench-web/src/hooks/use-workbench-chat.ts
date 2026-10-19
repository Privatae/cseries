import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTurn,
  fetchActiveTurn,
  fetchPendingWords,
  localId,
  nudgeTurn as nudgeTurnApi,
  openTurnStream,
  sendMessageStream,
} from "@/lib/api";
import { createTextBatcher } from "@kit/stream-batch";
import {
  type Ask,
  askFromTool,
  asksFromTickets,
  closedAsks,
  type PairingAsk,
  pairingFromTool,
  parseSetAside,
  queueAsk,
  settleAsk as settleAskIn,
  ticketIsOpen,
  unlistedAsks,
  withSetAside,
} from "@kit/ask";
import { readIndex, sameNudge } from "@/lib/pending-sends";
import { isCrisisFrame } from "@kit/crisis-notice";
import { useCrisisNotice } from "@kit/ui/crisis-notice";
import { adoptKept, dropKept, keepWords, pageLoadId, readIdsIn, readKept, tabStore, wordsId } from "@/lib/kept-words";
import { spendTokens } from "@/lib/page-count";

// Cards set aside with "Not now" (or a pairing card closed), remembered on
// this device so a project's ticket list does not raise them again.
const SET_ASIDE_KEY = "workbench.asks.setAside";
function isSetAside(key: string): boolean {
  try {
    return parseSetAside(window.localStorage.getItem(SET_ASIDE_KEY)).includes(key);
  } catch {
    return false;
  }
}
function rememberSetAside(key: string): void {
  try {
    window.localStorage.setItem(SET_ASIDE_KEY, withSetAside(window.localStorage.getItem(SET_ASIDE_KEY), key));
  } catch {
    /* private mode: set aside for this page only */
  }
}
import { getTicket } from "@/lib/api";
import { isLocalEcho, mergeHistory, stampTurnToken } from "@/lib/history-merge";
import { consumeStream, type ContextFrame, summarizeToolArgs, toolVerb } from "@/lib/stream";
import type {
  ActiveTurn,
  ChatMessage,
  ChatStatus,
  ToolInvocation,
} from "@/lib/types";

/**
 * useWorkbenchChat — live thread state for EVERY conversation with the host,
 * keyed by thread (general vs project id), so switching projects never
 * destroys an in-flight turn. Adapted from circles-web's use-circles-chat:
 * same AI SDK Data Stream folding (text deltas append, `9:` opens a tool
 * card, `a:` resolves it, `3:`/HTTP set a terminal error), but the target is
 * a project id on /api/brain/messages, not a circles thread.
 *
 * Why a map: a single-thread version reset — and ABORTED the
 * live stream — on every project switch, so a user who navigated away
 * mid-turn came back to nothing (the assistant turn only persists server-side
 * at completion, minutes later on a build turn). Streams now keep running per
 * thread; returning shows the turn still streaming. Only the explicit Stop
 * button aborts.
 */

export interface ThreadState {
  messages: ChatMessage[];
  status: ChatStatus;
  /** Latest conversation-depth frame (`2:[{t:"context",...}]`) for this
   * thread — drives the header's depth bar. Null on older backends. */
  context?: ContextFrame | null;
  /** True once history has been loaded (or a send started) for this thread —
   * the pane seeds unloaded threads from the server exactly once. */
  loaded: boolean;
}

const EMPTY_THREAD: ThreadState = { messages: [], status: "ready", loaded: false };

// Messages waiting for their thread to go idle — the ask card's answer given
// while that thread's turn still runs. Held at module level, not in the pane:
// the pane unmounts when no project is selected, and an answer already decided
// on the ticket must not vanish with it. Drained
// by whichever mounted hook next sees the thread idle; sent through the
// ordinary send. The decide itself speaks the answer into the
// turn that asked (session.answer_into_turn), and that turn holds for it: its
// nudge_read receipt settles the held copy here (lib/pending-sends.ts), so the
// answer is not sent a second time as a new turn.
// `echoId`: on a page of the book, the nudge line drawn for these words. When
// they go as the next message instead (the turn ended before reading them),
// that line goes too, and the message's own echo stands in its place.
// `inFlight`: the nudge request is out. The server may be taking the words
// (pending_words), so the copy waits for its answer before it can go as a
// message: held, it is dropped; refused or failed, it goes.
const PENDING_SENDS: Array<{
  key: string;
  projectId: number | null;
  text: string;
  echoId?: string;
  clientId?: string;
  inFlight?: boolean;
}> = [];

// Words the server took into a turn without writing them down (PII mode, the
// nudge route answers `held: false`) are kept in this tab's sessionStorage
// under this name until a turn has them (lib/kept-words.ts). PAGE tells this
// load's copies from a reload's.
const KEPT = "workbench.keptWords";
const PAGE = pageLoadId();
const kept = () => tabStore();
// The sender ids this page load has seen a turn READ (nudge_read receipts).
const READ_IDS = new Set<string>();

/** The sender ids a turn READ (its nudge_read receipts), from its replayed
 * stream; null when the turn is not known (gone from the registry). */
async function turnReadIds(turnId: string): Promise<Set<string> | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10_000);
  try {
    const r = await openTurnStream(turnId, 0, ctrl.signal);
    if (!r.ok) return null;
    return readIdsIn(await r.text());
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** Thread key for a chat target: the projectless "General" bench vs a project. */
export const threadKey = (projectId: number | null): string =>
  projectId == null ? "general" : `p:${projectId}`;

/** The project of a thread key (threadKey's inverse). */
const projectOf = (key: string): number | null => (key.startsWith("p:") ? Number(key.slice(2)) : null);

/** Lines for words the server holds (GET /api/brain/pending) that no line on
 * screen shows yet: each a nudge line, unread, just before the reply
 * streaming now (else at the end). Matched by the id the words were sent
 * with. */
export function withHeldLines(
  prev: ChatMessage[],
  words: Array<{ id: string; text: string; createdAt: string }>,
): ChatMessage[] {
  const missing = words.filter((w) => !prev.some((m) => m.nudge === true && m.nudgeId === w.id));
  if (!missing.length) return prev;
  const lines: ChatMessage[] = missing.map((w) => ({
    id: localId("u"),
    role: "user",
    text: w.text,
    tools: [],
    createdAt: w.createdAt,
    nudge: true,
    nudgeId: w.id,
  }));
  let i = -1;
  for (let k = prev.length - 1; k >= 0; k--) {
    if (prev[k].role === "assistant" && prev[k].streaming) {
      i = k;
      break;
    }
  }
  return i < 0 ? [...prev, ...lines] : [...prev.slice(0, i), ...lines, ...prev.slice(i)];
}

/** The kernel filed this turn under a different project than the window it
 * was typed into (its work landed there — runtime/turn_home.py). */
export interface ThreadMove {
  from: number | null;
  to: number;
  name: string | null;
  /** The moved turn's token (its session frame), so a page that watched it
   * can mark the project it moved to as having seen its reply. */
  turnToken?: string | null;
}

/** What became of words sent into a running turn: in it (the turn reads
 * them, and its nudge_read receipt settles the held copy); or held to go as
 * the next message, because the turn ended before it read them (it answered
 * 409, or it ended before its id came: "turn-ended"), or because the request
 * failed while the turn may still run ("not-sent" — the page must not say the
 * turn ended; a receipt still settles the copy if the turn read them). */
export type NudgeOutcome = "into-turn" | "turn-ended" | "not-sent";

/** The marker the kernel leaves in the source thread — same words, so the
 * server row replaces this local echo on the next merge instead of
 * rendering beside it (mergeHistory drops an echo by role + text). Twin of
 * turn_home.marker_text. */
export function movedMarkerText(to: number, name: string | null): string {
  const label = `project #${to}` + (name ? ` “${name}”` : "");
  return (
    `→ This exchange moved to ${label} — that is the project it worked on. ` +
    "Nothing was deleted; the conversation continues in that project's thread."
  );
}

export function useWorkbenchChat(
  opts: {
    onThreadMoved?: (move: ThreadMove) => void;
  } = {},
) {
  // Every nudge is its own line on the page, and it stays one across a
  // reload. The registry's `nudge` frame, which a reattach replays from the
  // turn's start, draws a line for words no line on screen shows yet (sent
  // before the reload, or from another window); and a nudge whose words go
  // as the next message instead gives up its line to that message.
  const [threads, setThreads] = useState<Record<string, ThreadState>>({});
  const threadsRef = useRef<Record<string, ThreadState>>({});
  threadsRef.current = threads;
  // The reasoning-effort pick, per thread: every send of that
  // thread carries it (a retry and a held message included), and the route
  // checks it against the Host model. Kept for the page's life, never
  // stored; "" = the model's default.
  const [efforts, setEfforts] = useState<Record<string, string>>({});
  const effortsRef = useRef<Record<string, string>>({});
  effortsRef.current = efforts;
  const setEffort = useCallback((projectId: number | null, effort: string) => {
    const k = threadKey(projectId);
    setEfforts((cur) => {
      const next = { ...cur };
      if (effort) next[k] = effort;
      else delete next[k];
      return next;
    });
  }, []);
  // Asks the Host raised, per thread (an `escalate` whose result is an open
  // ticket, on a live turn), and the open tickets of the project on screen
  // (raiseTickets: the pane reads them on opening the project and on its
  // poll). A card set aside with "Not now" is remembered
  // (isSetAside) and not raised again.
  const [asks, setAsks] = useState<Array<Ask & { key: string }>>([]);
  // The cards up now, for the poll's read-back (raiseTickets): which of one
  // project's cards its open-ticket list no longer names.
  const asksRef = useRef<Array<Ask & { key: string }>>([]);
  asksRef.current = asks;
  // The pairing code card, per thread: a connector pairing the Host started
  // there (its tool result carries the grant id, never the code).
  const [pairings, setPairings] = useState<Array<PairingAsk & { key: string }>>([]);
  const settledPairingsRef = useRef<Set<number>>(new Set());
  // Tickets answered or set aside on this page: a reattach replays the frames
  // and the same escalate result arrives again.
  const settledRef = useRef<Set<number>>(new Set());
  // The call side of each tool (name + args) by toolCallId, so the result
  // frame — id only — can be read as an Ask.
  const toolMetaRef = useRef<Map<string, { name: string; args: unknown }>>(new Map());
  // `setAside`: "Not now" — remembered on this device (the ticket stays open
  // in Tickets).
  const settleAsk = useCallback((ticketId: number, setAside = false) => {
    settledRef.current.add(ticketId);
    if (setAside) rememberSetAside(`ticket:${ticketId}`);
    setAsks((q) => settleAskIn(q, ticketId));
  }, []);
  /** The open tickets of one project's thread, read by the pane: each one not
   * answered or set aside comes up as its card on that thread, oldest first.
   * A card up on that thread whose ticket the list no longer names is read
   * back, and goes when the ticket is no longer open — answered in Network,
   * in Tickets or in another window. */
  const raiseTickets = useCallback((tickets: unknown, key: string) => {
    const fresh = asksFromTickets(
      tickets,
      (id) => settledRef.current.has(id) || isSetAside(`ticket:${id}`),
    );
    if (fresh.length) {
      setAsks((q) => fresh.reduce((acc, a) => queueAsk(acc, { ...a, key }), q));
    }
    const unlisted = unlistedAsks(asksRef.current, tickets, key);
    if (!unlisted.length) return;
    void closedAsks(unlisted, getTicket).then((closed) => {
      if (!closed.length) return;
      // Settled, so a list read before the answer and landing after it
      // does not raise the card again.
      for (const id of closed) settledRef.current.add(id);
      setAsks((q) => q.filter((a) => !closed.includes(a.ticketId)));
    });
  }, []);
  const raisePairing = useCallback((pa: PairingAsk | null, key: string) => {
    if (!pa || settledPairingsRef.current.has(pa.grantId) || isSetAside(`pairing:${pa.grantId}`)) return;
    setPairings((q) => (q.some((p) => p.grantId === pa.grantId) ? q : [...q, { ...pa, key }]));
  }, []);
  /** The pairing card closed: by the person (remembered, like Not now), or
   * because the pairing moved on. */
  const settlePairing = useCallback((grantId: number, setAside: boolean) => {
    settledPairingsRef.current.add(grantId);
    if (setAside) rememberSetAside(`pairing:${grantId}`);
    setPairings((q) => q.filter((p) => p.grantId !== grantId));
  }, []);
  // The caller's move handler, in a ref: an inline callback from the pane
  // changes identity every render and must not re-create the fold.
  const onThreadMovedRef = useRef(opts.onThreadMoved);
  onThreadMovedRef.current = opts.onThreadMoved;
  // Turns already relocated (thread>thread:turn) — a reattach replay must
  // not move or announce the same turn twice.
  const movedTurnsRef = useRef<Set<string>>(new Set());

  /** Carry this turn (its user echo + the assistant bubble) from the thread
   * it was typed into to the thread the kernel filed it under, leaving the
   * marker behind. One updater over both keys, so it reads the post-fold
   * state and never a stale ref. */
  const relocateTurn = useCallback(
    (fromKey: string, toKey: string, asstId: string, marker: ChatMessage) => {
      setThreads((prev) => {
        const from = prev[fromKey] ?? EMPTY_THREAD;
        const idx = from.messages.findIndex((m) => m.id === asstId);
        if (idx < 0) return prev;
        const start = idx > 0 && from.messages[idx - 1].role === "user" ? idx - 1 : idx;
        const carried = from.messages.slice(start, idx + 1);
        const rest = [...from.messages.slice(0, start), ...from.messages.slice(idx + 1)];
        const to = prev[toKey] ?? EMPTY_THREAD;
        return {
          ...prev,
          [fromKey]: { ...from, messages: [...rest, marker] },
          [toKey]: { ...to, messages: [...to.messages, ...carried], loaded: true },
        };
      });
    },
    [],
  );
  // One in-flight controller per thread — a switch never touches another
  // thread's stream.
  const abortsRef = useRef<Record<string, AbortController | null>>({});
  // Last text sent per thread — the replay payload for retry() (the failed
  // user turn is otherwise lost; a 502 used to force a full retype).
  const lastSentRef = useRef<Record<string, string>>({});
  // The level that message was held to (the book's hold-to-think), so a
  // retry runs at it again; absent = the thread's pick.
  const lastEffortRef = useRef<Record<string, string | undefined>>({});
  // The registry turn id per thread (from the 2:[{type:"turn"}] frame) — the
  // reattach + SERVER-SIDE cancel handle for the in-flight turn.
  const turnIdsRef = useRef<Record<string, string | null>>({});
  // Guards duplicate concurrent reattach attempts per thread.
  const reattachingRef = useRef<Record<string, boolean>>({});
  // The platform's crisis notice: raised by a turn's governance
  // frame, up until the person dismisses it, across a reload. Not a thread
  // message: it sits over the page, whatever thread is open.
  const crisis = useCrisisNotice();
  const raiseCrisis = crisis.raise;
  // The reattach, for the fold (built before it): a turn that ended without
  // reading words sent into it, which the server sent on as the thread's next
  // turn (nudge_requeued), is followed the moment it ends.
  const reattachRef = useRef<((projectId: number | null) => Promise<boolean>) | null>(null);
  // The threads whose turn just ended that way, still to follow. The follow
  // runs from an effect, once a render has committed the thread as no longer
  // streaming: reattachTurn reads the status off threadsRef, which a render
  // fills in, and a timer set at the turn's end could run before that render
  // (on a loaded machine it did), find the thread "streaming" and give up for
  // good, leaving the page off the turn the server started.
  const followRef = useRef<Set<string>>(new Set());
  // The kept-words pass (deliverKept), for the fold: a turn's end runs it.
  const deliverKeptRef = useRef<((projectId: number | null, tries?: number) => Promise<void>) | null>(null);

  const patchThread = useCallback(
    (key: string, fn: (t: ThreadState) => ThreadState) =>
      setThreads((prev) => ({ ...prev, [key]: fn(prev[key] ?? EMPTY_THREAD) })),
    [],
  );

  /** Adopt server truth for one thread (history load / refetch). Never aborts
   * anything; the pane guards against calling this over a live stream. */
  const reset = useCallback(
    (key: string, history: ChatMessage[]) => {
      // The thread's cards go with its messages (the tickets stay in Tickets).
      setAsks((q) => q.filter((a) => a.key !== key));
      patchThread(key, (t) => ({
        messages: history,
        status:
          t.status === "submitted" || t.status === "streaming" ? t.status : "ready",
        loaded: true,
      }));
    },
    [patchThread],
  );

  /** Adopt a fetched history window by MERGING it into the thread (see
   * mergeHistory). Safe over a live stream: the streaming bubble rides the
   * optimistic tail untouched, so callers no longer need the status guard
   * they needed with reset(). */
  const reconcile = useCallback(
    (key: string, history: ChatMessage[]) => {
      patchThread(key, (t) => ({
        ...t,
        messages: mergeHistory(t.messages, history),
        loaded: true,
      }));
    },
    [patchThread],
  );

  /** Drop a thread's client state so the next visit loads fresh server truth.
   * Needed when the server-side thread SHRANK (general→project adoption moves
   * its turns away) — the never-wipe rule in the pane rightly refuses shorter
   * histories, so an explicit invalidation is the only honest path. No-op on
   * a live stream: the in-flight turn is the freshest truth. */
  const invalidate = useCallback(
    (key: string) => {
      const cur = threadsRef.current[key];
      if (cur && (cur.status === "submitted" || cur.status === "streaming")) return;
      patchThread(key, () => ({ ...EMPTY_THREAD }));
    },
    [patchThread],
  );

  /** Explicit user stop for one thread — the ONLY navigation-independent abort.
   * Server-side first: the turn is DETACHED in the turn registry, so aborting
   * the local fetch alone would only stop watching while the engine (and its
   * spend) kept running — the exact hidden-process failure this replaces. */
  const stop = useCallback(
    (key: string) => {
      const tid = turnIdsRef.current[key];
      if (tid) {
        void cancelTurn(tid);
        turnIdsRef.current[key] = null;
      }
      abortsRef.current[key]?.abort();
      abortsRef.current[key] = null;
      patchThread(key, (t) => ({
        ...t,
        status: "ready",
        messages: t.messages.map((m) =>
          m.streaming ? { ...m, streaming: false } : m,
        ),
      }));
    },
    [patchThread],
  );

  /** The shared frame-folding handlers — send and reattach must render one
   * turn identically, so there is exactly one construction of these.
   * `onContent` (send-path only) feeds the auto-retry's "anything visible
   * streamed yet?" check. */
  const makeFold = useCallback(
    (key: string, asstId: string, t0: number, onContent?: () => void) => {
      const setStatus = (status: ChatStatus) =>
        patchThread(key, (t) => ({ ...t, status }));
      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        patchThread(key, (t) => ({
          ...t,
          messages: t.messages.map((m) => (m.id === asstId ? fn(m) : m)),
        }));
      let liveUsage: {
        promptTokens?: number;
        completionTokens?: number;
        cachedTokens?: number;
        cacheWriteTokens?: number;
      } | null =
        null;
      // The turn's context frame (2:[{t:"context"}]) — carries WHO ANSWERED
      // (model/provider/failed_over) so the finished turn's meta line can
      // print it live, exactly as the persisted row will after a reload.
      let liveContext: ContextFrame | null = null;
      // Set by the kernel's thread_moved frame: this turn's home is another
      // project. Acted on at onDone, once the bubble is final.
      let moved: { to: number; name: string | null } | null = null;
      // The registry turn id as this fold saw it (turnIdsRef is cleared at
      // onDone before the relocation runs).
      let foldTurnId: string | null = null;
      // The turn's token, off its session frame: the moved-turn notice
      // carries it (ThreadMove.turnToken).
      let foldToken: string | null = null;
      // Nudge lines this fold has matched to a registry `nudge` frame: each
      // line answers one frame, so the same words sent
      // twice draw twice.
      const claimedNudges = new Set<string>();
      // Set by a nudge_requeued frame: the server started the thread's next
      // turn with words this turn never read. Followed at onDone.
      let followNext = false;
      // Text and reasoning land at most once per frame (app-web-kit/stream-batch).
      // Every other frame flushes first, so the order the user sees is unchanged.
      const live = createTextBatcher((text, reasoning) =>
        patch((m) => ({
          ...m,
          ...(text !== null ? { text } : {}),
          ...(reasoning !== null ? { reasoning } : {}),
        })),
      );
      return {
        onText: (_delta: string, full: string) => {
          onContent?.();
          live.text(full);
        },
        onReasoning: (_delta: string, full: string) => live.reasoning(full),
        onTool: ({
          toolCallId,
          toolName,
          args,
        }: {
          toolCallId: string;
          toolName: string;
          args: Record<string, unknown>;
        }) => {
          onContent?.();
          live.flush();
          toolMetaRef.current.set(toolCallId, { name: toolName, args });
          patch((m) => {
            const tool: ToolInvocation = {
              id: toolCallId,
              name: toolName,
              args,
              summary: summarizeToolArgs(args, toolName),
              verb: toolVerb(args),
              state: "input-available",
              // A dispatch spawns a background AGENT — render it as
              // delegated work from the first frame.
              kind: toolName === "dispatch" ? "agent" : "tool",
            };
            return { ...m, tools: [...m.tools, tool] };
          });
        },
        onResult: ({
          toolCallId,
          result,
          status: st,
        }: {
          toolCallId: string;
          result: unknown;
          status: "running" | "ok" | "error";
        }) => {
          live.flush();
          // An `escalate` that filed an open ticket is an ask: raise the card
          // on this thread.
          const meta = toolMetaRef.current.get(toolCallId);
          if (meta) {
            const ask = askFromTool({ id: toolCallId, name: meta.name, args: meta.args, result });
            if (ask && !settledRef.current.has(ask.ticketId) && !isSetAside(`ticket:${ask.ticketId}`)) {
              // Not settled here — but a reload mid-turn replays with an
              // empty settled set, so the server's word on the ticket is read
              // before the card is raised.
              void getTicket(ask.ticketId).then((r) => {
                if (!r.ok || !ticketIsOpen(r.data) || settledRef.current.has(ask.ticketId)) return;
                setAsks((q) => queueAsk(q, { ...ask, key }));
              });
            }
            // A connector pairing the Host just started: the code card. It
            // reads the code itself; a replayed stream for a pairing already
            // closed reads 404 and the card never shows.
            const pa = pairingFromTool({ id: toolCallId, name: meta.name, args: meta.args, result });
            if (pa) raisePairing(pa, key);
          }
          patch((m) => ({
            ...m,
            tools: m.tools.map((t) => {
              if (t.id !== toolCallId) return t;
              const taskId =
                result && typeof result === "object" && "task_id" in result
                  ? String((result as { task_id?: string | number }).task_id ?? "")
                  : undefined;
              return {
                ...t,
                result,
                taskId: taskId || t.taskId,
                state: st === "error" ? "output-error" : "output-available",
              };
            }),
          }));
        },
        onData: (item: unknown) => {
          const it = item as {
            type?: string;
            turn_id?: string;
            turn_token?: string;
            to_project_id?: unknown;
            to_project_name?: unknown;
            text?: unknown;
            client_id?: unknown;
          };
          // The sender's id for a nudge (frame or receipt), when it gave one.
          const frameNudgeId = typeof it?.client_id === "string" && it.client_id ? it.client_id : undefined;
          // The runtime's session frame names this turn's token — the one its
          // saved rows carry. Stamp it on both echoes so the merge swaps them
          // for those rows exactly, whatever text the saved reply ended with.
          if (it && it.type === "session" && it.turn_token) {
            const token = it.turn_token;
            foldToken = token;
            patchThread(key, (t) => ({
              ...t,
              messages: stampTurnToken(t.messages, asstId, token),
            }));
          }
          // The registry's `nudge` frame: words someone sent into this turn
          // (turn_registry._nudge). On a page of the book, words no line on
          // screen shows yet get their own line: the person's own, replayed
          // after a reload before the turn read them (the saved row comes
          // only once it does), or another window's. The frame holds the
          // words cut at 500 characters, so a line is matched by nudgeKey:
          // the same cut, so words of any length match their own frame.
          if (it && it.type === "nudge" && typeof it.text === "string" && it.text.trim()) {
            const words = it.text.trim();
            patchThread(key, (t) => {
              const shown = t.messages.find(
                (m) =>
                  m.role === "user" &&
                  m.nudge &&
                  !claimedNudges.has(m.id) &&
                  sameNudge(m, { text: words, nudgeId: frameNudgeId }),
              );
              if (shown) {
                claimedNudges.add(shown.id);
                return t;
              }
              const line: ChatMessage = {
                id: localId("u"),
                role: "user",
                text: words,
                tools: [],
                createdAt: new Date().toISOString(),
                nudge: true,
                ...(frameNudgeId ? { nudgeId: frameNudgeId } : {}),
                ...(foldToken ? { turnToken: foldToken } : {}),
              };
              claimedNudges.add(line.id);
              return { ...t, messages: [...t.messages, line] };
            });
          }
          // The registry's turn-metadata frame: the reattach + cancel handle.
          if (it && it.type === "turn" && it.turn_id) {
            turnIdsRef.current[key] = it.turn_id;
            foldTurnId = it.turn_id;
          }
          // The running turn READ a message (session.py's drain): the ask
          // card's answer, spoken into the turn by the decide. A copy held for
          // this thread's idle moment is settled — it was already read.
          if (it && it.type === "nudge_read" && typeof it.text === "string" && it.text) {
            const i = readIndex(PENDING_SENDS, key, it.text, frameNudgeId);
            const settled = i >= 0 ? PENDING_SENDS.splice(i, 1)[0] : null;
            // The turn has the words: a copy this tab kept of them goes.
            const settledId = settled?.clientId ?? frameNudgeId;
            if (settledId) {
              READ_IDS.add(settledId);
              dropKept(kept(), KEPT, settledId);
            }
            // The nudge's line says it was read, so the page shows which
            // side delivered the words: this turn, not a next message.
            const read = { text: it.text, nudgeId: frameNudgeId };
            patchThread(key, (t) => {
              const at = t.messages.findIndex(
                (m) => m.role === "user" && m.nudge && !m.nudgeRead && sameNudge(m, read),
              );
              if (at < 0) return t;
              const messages = [...t.messages];
              messages[at] = { ...messages[at], nudgeRead: true };
              return { ...t, messages };
            });
          }
          // The platform's crisis check flagged the person's words this turn
          // (2:[{type:"governance",kind:"crisis"}]): the platform's own notice
          // over the page — the reply is the Host's and streams on. Keyed by
          // the turn, so a replay of a turn whose notice was dismissed does
          // not raise it again.
          if (isCrisisFrame(it)) {
            raiseCrisis(foldToken ?? turnIdsRef.current[key] ?? asstId);
          }
          // The words of this send are in the thread already (a second send of
          // words kept in this tab, whose turn the server no longer lists):
          // nothing ran, and its bubbles give way to the row that holds them.
          if (it && it.type === "words_delivered") {
            patchThread(key, (t) => {
              const i = t.messages.findIndex((m) => m.id === asstId);
              if (i < 0) return t;
              const prevMsg = t.messages[i - 1];
              const from = i > 0 && prevMsg.role === "user" && isLocalEcho(prevMsg.id) ? i - 1 : i;
              return { ...t, messages: [...t.messages.slice(0, from), ...t.messages.slice(i + 1)] };
            });
          }
          // The turn ended without reading words sent into it, and the server
          // sent them on as the thread's next turn (they are the server's,
          // pending_words): their lines give way to that turn's own message,
          // and the page follows it once this one ends.
          if (it && it.type === "nudge_requeued") {
            const raw = (it as { client_ids?: unknown }).client_ids;
            const ids = Array.isArray(raw) ? (raw.filter((x) => typeof x === "string") as string[]) : [];
            if (ids.length) {
              patchThread(key, (t) => ({
                ...t,
                messages: t.messages.filter(
                  (m) => !(m.role === "user" && m.nudge && !m.nudgeRead && m.nudgeId && ids.includes(m.nudgeId)),
                ),
              }));
            }
            followNext = true;
          }
          if (it && it.type === "thread_moved" && typeof it.to_project_id === "number") {
            moved = {
              to: it.to_project_id,
              name: typeof it.to_project_name === "string" && it.to_project_name ? it.to_project_name : null,
            };
          }
        },
        onContext: (cf: ContextFrame) => {
          liveContext = cf;
          patchThread(key, (t) => ({ ...t, context: cf }));
        },
        onError: (message: string) => {
          live.flush();
          patch((m) => ({ ...m, streaming: false, error: message }));
          setStatus("error");
        },
        onUsage: (u: {
          promptTokens?: number;
          completionTokens?: number;
          // The cache split brain_service has always emitted
          // (brain_service.py:646-649). The Workbench dropped it.
          cachedTokens?: number;
          cacheWriteTokens?: number;
        }) => {
          liveUsage = u;
        },
        onDone: () => {
          live.flush();
          const genMs = Math.round(performance.now() - t0);
          const tin = liveUsage?.promptTokens ?? null;
          const tout = liveUsage?.completionTokens ?? null;
          const tcached = liveUsage?.cachedTokens ?? null;
          const tcw = liveUsage?.cacheWriteTokens ?? null;
          turnIdsRef.current[key] = null;
          patch((m) => {
            const silent = !m.text && m.tools.length === 0 && !m.error;
            return {
              ...m,
              streaming: false,
              metrics: {
                gen_ms: genMs,
                tokens_in: tin,
                tokens_out: tout,
                tokens_cached: tcached,
                tokens_cache_write: tcw,
                tokens_total:
                  tin != null || tout != null ? (tin ?? 0) + (tout ?? 0) : null,
                model: liveContext?.model ?? null,
                provider: liveContext?.provider ?? null,
                failed_over: liveContext?.failed_over ?? null,
                context_ledger: liveContext?.ledger ?? null,
                close_turn: liveContext?.close_turn ?? null,
                // What the turn added to its page, before its row is saved:
                // the same spend record and price the row is stamped with.
                spend_tokens: spendTokens(liveContext?.ledger?.spend),
                est_cost_usd: liveContext?.est_cost_usd ?? null,
                price_basis: liveContext?.price_basis ?? null,
              },
              error: silent
                ? "No output received from the host — silent turns are a bug, not a shrug. Check host status in the Orchestrator and report this."
                : m.error,
            };
          });
          setStatus("ready");
          // Words a previous load of this tab kept for the page (PII mode):
          // the turn has ended, so those it never read go now.
          setTimeout(() => void deliverKeptRef.current?.(projectOf(key)), 0);
          // Followed after the render that shows the thread ready (the effect
          // over threads below), so the reattach is never refused as a thread
          // still streaming.
          if (followNext) followRef.current.add(key);
          if (moved) {
            const toKey = threadKey(moved.to);
            // A reattach replays the turn from seq 0, so the same
            // thread_moved frame can arrive twice; the relocation is
            // idempotent, the notice + follow must be once per turn.
            const turnTag = `${key}>${toKey}:${foldTurnId ?? asstId}`;
            const seen = movedTurnsRef.current.has(turnTag);
            movedTurnsRef.current.add(turnTag);
            if (toKey !== key && !seen) {
              relocateTurn(key, toKey, asstId, {
                id: localId("m"),
                role: "assistant",
                text: movedMarkerText(moved.to, moved.name),
                tools: [],
                createdAt: new Date().toISOString(),
              });
              const from = key.startsWith("p:") ? Number(key.slice(2)) : null;
              onThreadMovedRef.current?.({ from, to: moved.to, name: moved.name, turnToken: foldToken });
            }
          }
        },
      };
    },
    [patchThread, relocateTurn, raiseCrisis],
  );

  /**
   * Reattach to the thread's RUNNING turn, if any — the tab-switch /
   * laptop-sleep / dead-connection recovery path. Replays the whole turn from
   * seq 0 (optimistic bubbles from before the disconnect are replaced) and
   * follows live. Returns true when it attached. Finished turns are ignored:
   * their frames persisted as normal turn rows, the history load renders them.
   */
  const reattachTurn = useCallback(
    async (projectId: number | null): Promise<boolean> => {
      const key = threadKey(projectId);
      if (reattachingRef.current[key]) return false;
      const cur = threadsRef.current[key];
      if (cur && (cur.status === "submitted" || cur.status === "streaming")) {
        return false;
      }
      reattachingRef.current[key] = true;
      try {
        const r = await fetchActiveTurn(projectId);
        if (!r.ok || !r.data.turn || r.data.turn.status !== "running") {
          return false;
        }
        // A message sent from this page while the answer was on its way is a
        // turn this page already streams: attaching to it would draw it twice
        // (a page started from the Index sends its first message as it opens).
        const live = threadsRef.current[key];
        if (live && (live.status === "submitted" || live.status === "streaming")) {
          return false;
        }
        const turn: ActiveTurn = r.data.turn;
        const asstId = localId("a");
        const now = new Date().toISOString();
        patchThread(key, (t) => {
          const msgs = [...t.messages];
          while (
            msgs.length > 0 &&
            (msgs[msgs.length - 1].streaming ||
              (msgs[msgs.length - 1].role === "user" &&
                msgs[msgs.length - 1].text.trim() === turn.user_message.trim()))
          ) {
            msgs.pop();
          }
          return {
            messages: [
              ...msgs,
              {
                id: localId("u"),
                role: "user",
                text: turn.user_message,
                tools: [],
                createdAt: now,
              },
              {
                id: asstId,
                role: "assistant",
                text: "",
                tools: [],
                createdAt: now,
                streaming: true,
              },
            ],
            status: "streaming",
            loaded: true,
          };
        });
        turnIdsRef.current[key] = turn.turn_id;
        const ctrl = new AbortController();
        abortsRef.current[key] = ctrl;
        // Elapsed continuity: the turn started before we attached.
        const t0 = performance.now() - turn.elapsed_s * 1000;
        const resp = await openTurnStream(turn.turn_id, 0, ctrl.signal);
        if (!resp.ok) {
          patchThread(key, (t) => ({ ...t, status: "ready" }));
          return false;
        }
        void consumeStream(resp, makeFold(key, asstId, t0))
          .catch(() => patchThread(key, (t) => ({ ...t, status: "ready" })))
          .finally(() => {
            if (abortsRef.current[key] === ctrl) abortsRef.current[key] = null;
          });
        return true;
      } finally {
        reattachingRef.current[key] = false;
      }
    },
    [patchThread, makeFold],
  );

  /** Draw the words the server holds for this page (sent into a turn, not
   * read by one yet), so they are seen to survive a reload, a restart of
   * the Host included: the page keeps no copy of words the server took. */
  const showHeld = useCallback(
    async (projectId: number | null) => {
      const key = threadKey(projectId);
      const r = await fetchPendingWords(projectId);
      if (!r.ok) return;
      const words = (r.data.words ?? []).map((w) => ({
        id: w.client_id || `held-${w.id}`,
        text: w.text,
        createdAt: w.received_at || new Date().toISOString(),
      }));
      // And the words this tab keeps for the page (PII mode: the server
      // wrote nothing down), until a turn has them.
      for (const w of readKept(kept(), KEPT)) {
        if (w.key === key) words.push({ id: w.clientId, text: w.text, createdAt: w.at });
      }
      if (words.length) patchThread(key, (t) => ({ ...t, messages: withHeldLines(t.messages, words) }));
    },
    [patchThread],
  );

  /** Reattach to the thread's running turn (reattachTurn), then draw what the
   * server still holds for the page: its words have no frame in a turn when
   * the turn they went into is gone. */
  const reattach = useCallback(
    async (projectId: number | null): Promise<boolean> => {
      const attached = await reattachTurn(projectId);
      void showHeld(projectId);
      if (!attached) void deliverKeptRef.current?.(projectId);
      return attached;
    },
    [reattachTurn, showHeld],
  );
  reattachRef.current = reattach;

  /** Send a message on the thread. `opts.effort`: the level this one message
   * runs at, over the thread's pick (the book's hold-to-think); the route
   * checks it against the Host model like the pick (brain.py). */
  const send = useCallback(
    async (
      projectId: number | null,
      text: string,
      opts?: { effort?: string | null; onAccepted?: () => void; clientId?: string },
    ) => {
      const key = threadKey(projectId);
      const trimmed = text.trim();
      if (!trimmed) return;
      const cur = threadsRef.current[key];
      if (cur && (cur.status === "submitted" || cur.status === "streaming")) return;
      const held = opts?.effort || undefined;

      const now = new Date().toISOString();
      const userMsg: ChatMessage = {
        id: wordsId("u"),
        role: "user",
        text: trimmed,
        tools: [],
        createdAt: now,
        ...(held ? { heldEffort: held } : {}),
      };
      // The words' id: their own when they were nudged first (a second send
      // under it delivers nothing new, it follows the turn that has them),
      // else this message's.
      const wordsKey = opts?.clientId || userMsg.id;
      const asstId = localId("a");
      const asstMsg: ChatMessage = {
        id: asstId,
        role: "assistant",
        text: "",
        tools: [],
        createdAt: now,
        streaming: true,
      };
      patchThread(key, (t) => ({
        messages: [...t.messages, userMsg, asstMsg],
        status: "submitted",
        loaded: true,
      }));
      lastSentRef.current[key] = trimmed;
      lastEffortRef.current[key] = held;

      const setStatus = (status: ChatStatus) =>
        patchThread(key, (t) => ({ ...t, status }));
      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        patchThread(key, (t) => ({
          ...t,
          messages: t.messages.map((m) => (m.id === asstId ? fn(m) : m)),
        }));

      // One turn attempt against the same optimistic bubbles. Auto-retries
      // ONCE when the failure happened before anything visible streamed
      // (502/503/504 response, or a fetch/stream death with zero content) —
      // idempotent by construction: the server never streamed the turn, so
      // re-sending can't double-render. Mid-stream deaths after content are
      // NEVER auto-retried (content already showed); the visible error +
      // Retry button covers those.
      const attemptTurn = async (attempt: number): Promise<void> => {
        const ctrl = new AbortController();
        abortsRef.current[key] = ctrl;
        let gotContent = false;
        // Client-side turn clock for the live bubble — the durable stamp
        // (server-side gen_ms/tokens in the turn row's metadata) replaces it
        // on the next history load.
        const t0 = performance.now();
        try {
          // The same words' id on both attempts: a retry whose first attempt
          // reached a running turn, whose words the server holds, delivers
          // nothing again (brain_service.send_message claim_resend).
          const res = await sendMessageStream(projectId, trimmed, ctrl.signal,
                                              held || effortsRef.current[key] || null, wordsKey);
          if (!res.ok) {
            if ([502, 503, 504].includes(res.status) && attempt === 0) {
              await new Promise((r) => setTimeout(r, 600));
              return attemptTurn(1);
            }
            let detail = "";
            try {
              const j = (await res.json()) as { detail?: string; error?: string };
              detail = j.detail || j.error || "";
            } catch {
              /* non-JSON */
            }
            patch((m) => ({
              ...m,
              streaming: false,
              error: `${res.status} — ${detail || res.statusText || "request failed"}`,
            }));
            setStatus("error");
            return;
          }
          opts?.onAccepted?.();

          setStatus("streaming");
          await consumeStream(
            res,
            makeFold(key, asstId, t0, () => {
              gotContent = true;
            }),
          );
        } catch (e) {
          const err = e as Error;
          if (err.name === "AbortError") {
            patch((m) => ({ ...m, streaming: false }));
            setStatus("ready");
            return;
          }
          if (!gotContent && attempt === 0) {
            // Failed before anything visible streamed — same idempotent
            // class as a 502 response; one silent retry. (Even if the server
            // DID start the turn, the registry dedups the identical resend
            // into a reattach — no double turn, no double spend.)
            await new Promise((r) => setTimeout(r, 600));
            return attemptTurn(1);
          }
          // The CONNECTION died mid-stream, not the turn — it keeps running
          // detached in the registry. Reattach instead of a dead-end error.
          setStatus("ready");
          const attached = await reattach(projectId);
          if (!attached) {
            patch((m) => ({
              ...m,
              streaming: false,
              error: err.message || "network error",
            }));
            setStatus("error");
          }
        } finally {
          // Only clear OUR controller: after an auto-retry, attempt N+1 has
          // already installed its own — nulling it here killed the Stop
          // button for the retried stream (it kept writing after Stop).
          if (abortsRef.current[key] === ctrl) abortsRef.current[key] = null;
        }
      };
      await attemptTurn(0);
    },
    [patchThread, makeFold, reattach],
  );

  /** Re-send the thread's last user message after a failed turn. Drops the
   * failed assistant bubble + its user bubble first so the retried turn
   * doesn't render twice. No-op while a stream is live or with nothing to
   * replay. */
  const retry = useCallback(
    (projectId: number | null) => {
      const key = threadKey(projectId);
      const text = lastSentRef.current[key];
      const cur = threadsRef.current[key];
      if (!text || !cur) return;
      if (cur.status === "submitted" || cur.status === "streaming") return;
      patchThread(key, (t) => {
        const msgs = [...t.messages];
        const last = msgs[msgs.length - 1];
        if (last && last.role === "assistant" && (last.error || !last.text)) {
          msgs.pop();
          const prev = msgs[msgs.length - 1];
          if (prev && prev.role === "user" && prev.text === text) msgs.pop();
        }
        return { ...t, messages: msgs, status: "ready" };
      });
      void send(projectId, text, { effort: lastEffortRef.current[key] });
    },
    [patchThread, send],
  );

  // Send now if the thread is idle, else when it next is. The check runs on
  // every threads change, so the moment a turn ends its waiting message goes;
  // a pending entry is removed BEFORE the send, so it can never go twice. A
  // nudge's words going as a message take the place of their nudge line: the
  // message's own echo stands for them from here.
  const drainPending = useCallback(() => {
    for (const p of PENDING_SENDS.slice()) {
      if (p.inFlight) continue;
      const st = threadsRef.current[p.key]?.status ?? "ready";
      if (st === "submitted" || st === "streaming") continue;
      PENDING_SENDS.splice(PENDING_SENDS.indexOf(p), 1);
      if (p.echoId || p.clientId) {
        // Its line: the echo drawn when it was sent, or (words a reload read
        // back from this tab) the line drawn under its id.
        const echo = p.echoId;
        const cid = p.clientId;
        patchThread(p.key, (t) => ({
          ...t,
          messages: t.messages.filter(
            (m) => !(m.id === echo || (cid && m.nudge && !m.nudgeRead && m.nudgeId === cid)),
          ),
        }));
      }
      // A copy kept in this tab goes once a turn has the words: the send is
      // accepted (or has failed, as the page's own copy does).
      const cid = p.clientId;
      const settle = () => cid && dropKept(kept(), KEPT, cid);
      // Under the words' own id: a page that sends them twice (a reload before
      // the answer, a duplicated tab) or after the turn read them gets that
      // turn back, and the Host has them once.
      void send(p.projectId, p.text, { onAccepted: settle, clientId: cid }).finally(settle);
    }
  }, [send, patchThread]);

  /** Words a previous load of this tab kept for the page (PII mode) and no
   * turn has yet: once no turn runs on the page, those the turn they went
   * into never read (its replayed receipts say) go as the next message,
   * once. While a turn runs this waits: the reattach replays its receipts,
   * and its end runs this again. */
  const deliverKept = useCallback(
    async (projectId: number | null, tries = 0): Promise<void> => {
      const key = threadKey(projectId);
      const theirs = readKept(kept(), KEPT).filter((w) => w.key === key && w.page !== PAGE);
      if (!theirs.length) return;
      const act = await fetchActiveTurn(projectId);
      if (!act.ok || (act.data.turn && act.data.turn.status === "running")) {
        // A turn runs (or the answer did not come): the end of a turn this
        // page follows runs this again; a turn that is just ending, or one
        // this page does not follow, is looked at again shortly.
        if (tries < 5) setTimeout(() => void deliverKeptRef.current?.(projectId, tries + 1), 1000);
        return;
      }
      const unread: typeof theirs = [];
      for (const w of theirs) {
        const read = w.turnId ? await turnReadIds(w.turnId) : null;
        if (read && read.has(w.clientId)) dropKept(kept(), KEPT, w.clientId);
        else unread.push(w);
      }
      // Read again: a receipt, or another pass, may have settled one meanwhile.
      const still = readKept(kept(), KEPT).filter((w) => w.page !== PAGE).map((w) => w.clientId);
      const take = unread.filter(
        (w) => still.includes(w.clientId) && !READ_IDS.has(w.clientId) && !PENDING_SENDS.some((p) => p.clientId === w.clientId),
      );
      if (!take.length) return;
      adoptKept(kept(), KEPT, take.map((w) => w.clientId), PAGE);
      for (const w of take) PENDING_SENDS.push({ key, projectId, text: w.text, clientId: w.clientId });
      drainPending();
    },
    [drainPending],
  );
  deliverKeptRef.current = deliverKept;
  const sendWhenIdle = useCallback(
    (projectId: number | null, text: string) => {
      PENDING_SENDS.push({ key: threadKey(projectId), projectId, text });
      drainPending();
    },
    [drainPending],
  );
  // The composer's Send while the thread's turn runs (a nudge): the words go
  // into that turn — the registry's nudge, so every attached client draws
  // them — and the kernel writes them as a user row when the turn reads them.
  // The server holds the words from its answer on (`held`, pending_words):
  // the page keeps no copy, shows what is still held after a
  // reload (showHeld), and follows the turn the server sends them on to.
  // Until that answer, and where the server writes nothing (PII mode), a copy
  // is held as for the ask card: the turn's nudge_read receipt settles it; if
  // the turn ends first (409) or the request fails, it goes as the next
  // message. A turn just starting has no id yet: its first frame brings
  // it, and the nudge waits for it for as long as the turn runs — never a
  // fixed time, so a slow start is not taken for a turn that ended.
  const nudgeTurn = useCallback(
    async (projectId: number | null, raw: string): Promise<NudgeOutcome> => {
      const key = threadKey(projectId);
      // The words as the turn reads them: push_nudge strips them, and the
      // receipt carries them so. The held copy must be the same words, or a
      // nudge the turn read would ALSO go as the next message (twice).
      const text = raw.trim();
      const echoId = wordsId("u");
      // The words' id (client_id): the frame, the turn's receipt and the saved
      // row echo it, so the page knows its own words by it, whatever their
      // text became on the way (a secret shape cut, the frame's 500 cut).
      const clientId = echoId;
      PENDING_SENDS.push({ key, projectId, text, clientId, echoId });
      patchThread(key, (t) => ({
        ...t,
        messages: [
          ...t.messages,
          {
            id: echoId,
            role: "user",
            text,
            tools: [],
            createdAt: new Date().toISOString(),
            nudge: true,
            nudgeId: clientId,
          },
        ],
      }));
      let turnId = turnIdsRef.current[key];
      while (!turnId) {
        const st = threadsRef.current[key]?.status ?? "ready";
        if (st !== "submitted" && st !== "streaming") break;
        await new Promise((res) => setTimeout(res, 100));
        turnId = turnIdsRef.current[key];
      }
      if (!turnId) {
        // The turn ended before its id came (the loop above stops only then).
        drainPending();
        return "turn-ended";
      }
      const mine = PENDING_SENDS.find((p) => p.clientId === clientId);
      if (mine) mine.inFlight = true;
      const r = await nudgeTurnApi(turnId, text, clientId);
      if (mine) mine.inFlight = false;
      if (!r.ok) {
        drainPending();
        return r.status === 409 ? "turn-ended" : "not-sent";
      }
      if (r.data.held) {
        // The server wrote the words down before it answered and owns them
        // from here: the turn reads them, or they start the thread's next
        // turn (nudge_requeued), after a restart too. No copy is kept to send
        // again — a second one would reach the Host twice. Without `held`
        // (PII mode writes nothing) the copy stays until the turn's receipt.
        const at = PENDING_SENDS.findIndex((p) => p.clientId === clientId);
        if (at >= 0) PENDING_SENDS.splice(at, 1);
      } else {
        // Nothing written down (PII mode): the copy is kept in this tab too,
        // so a reload does not lose it — unless the receipt has come already
        // (the copy is gone from the list).
        if (PENDING_SENDS.some((p) => p.clientId === clientId) && !READ_IDS.has(clientId)) {
          keepWords(kept(), KEPT, {
            key,
            text,
            clientId,
            turnId,
            page: PAGE,
            at: new Date().toISOString(),
            projectId,
          });
        }
        drainPending();
      }
      return "into-turn";
    },
    [patchThread, drainPending],
  );
  useEffect(() => {
    drainPending();
  }, [threads, drainPending]);

  // A turn that ended with words the server sent on as the thread's next turn
  // (followRef): follow it now that the commit shows the thread done
  // streaming. A thread busy again (a message sent meanwhile) waits for that
  // turn's end, and is followed then if the server's turn still runs.
  useEffect(() => {
    for (const key of [...followRef.current]) {
      const st = threads[key]?.status;
      if (st === "submitted" || st === "streaming") continue;
      followRef.current.delete(key);
      void reattachRef.current?.(projectOf(key));
    }
  }, [threads]);

  return {
    threads,
    threadsRef,
    asks,
    settleAsk,
    raiseTickets,
    pairings,
    raisePairing,
    settlePairing,
    crisisNotice: crisis.open,
    dismissCrisisNotice: crisis.dismiss,
    sendWhenIdle,
    nudgeTurn,
    send,
    efforts,
    setEffort,
    stop,
    reset,
    reconcile,
    retry,
    invalidate,
    reattach,
  };
}
