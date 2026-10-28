import { useCallback, useEffect, useRef, useState } from "react";
import {
  consumeCirclesStream,
  type ContextFrame,
  summarizeToolArgs,
  toolVerb,
  type CirclesStreamHandlers,
} from "@/lib/circles-stream";
import {
  cancelTurn,
  fetchActiveTurn,
  localId,
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
import { fetchPendingWords, getTicket, nudgeSession } from "@/lib/api";
import { nudgeKey, readIndex, type HeldSend } from "@kit/book/nudge";
import { isCrisisFrame } from "@kit/crisis-notice";
import { useCrisisNotice } from "@kit/ui/crisis-notice";
import { adoptKept, dropKept, keepWords, pageLoadId, readIdsIn, readKept, tabStore, wordsId } from "@/lib/kept-words";

// Words the server took into a turn without writing them down (PII mode,
// `held: false`) are kept in this tab's sessionStorage under this name until
// a turn has them (lib/kept-words.ts). PAGE tells this load's copies from a
// reload's.
const KEPT = "circles.keptWords";
const PAGE = pageLoadId();
const kept = () => tabStore();

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

// Cards set aside with "Not now" (or a pairing card closed), remembered on
// this device so the conversation's ticket list does not raise them again.
const SET_ASIDE_KEY = "circles.asks.setAside";
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
/** Undo a "Not now": the person asked for that card again (a decision picked
 * from Waiting on you), so the page's ticket list raises it once more. */
function forgetSetAside(key: string): void {
  try {
    const cur = parseSetAside(window.localStorage.getItem(SET_ASIDE_KEY)).filter((k) => k !== key);
    window.localStorage.setItem(SET_ASIDE_KEY, JSON.stringify(cur));
  } catch {
    /* private mode */
  }
}
import { isLocalEcho, mergeHistory, retagTurn, stampTurnToken } from "@/lib/history-merge";
import { type Landing, landingFrom, selfThreadKey, thisTurnStart, topicTag } from "@/lib/turn-types";
import type {
  ActiveTurn,
  AttachmentBlock,
  CarryRef,
  ChatMessage,
  ChatStatus,
  FeedTopic,
  ModelPick,
  Target,
  ToolInvocation,
} from "@/lib/types";

/** Options threaded through send -> sendMessageStream -> /messages body. */
export interface SendOptions {
  model?: ModelPick | null;
  /** The thread's reasoning-effort pick; null = the model's default. */
  effort?: string | null;
  attachments?: AttachmentBlock[];
  // References the user attached from an app this send (composer pills).
  // Ride the request's `frame` so the host's per-turn block names them.
  pills?: string[];
  /** The active Circles thread id ("original" | uuid), self-thread only. */
  threadId?: string | null;
  /** A turn type picked in the dropdown, self-thread only. */
  turnTypeId?: string | null;
  /** Called once the send is accepted with WHERE the message landed. The pane moves the view when `moved` is true. */
  onLanded?: (landing: Landing) => void;
  /** Called once the server has accepted the send: a turn has its words. */
  onAccepted?: () => void;
  /** The words' id, when they were nudged first (self only): a second send
   * under it delivers nothing new, it follows the turn that has them
   * (brain_service.send_message). */
  clientId?: string;
  /** Ask Ivy: a message of a person's chat given to this turn (self only).
   * The send may carry no words of its own then. */
  carry?: CarryRef | null;
  /** One conversation (self only): the Host picks the topic; `threadId` is
   * only the topic the person was last in. */
  pickTopic?: boolean;
  /** One conversation (self only): `threadId` was picked already. */
  topicPicked?: boolean;
}

/**
 * useCirclesChat — owns the live thread state for one conversation.
 *
 * Drives /api/circles/messages and folds the AI SDK Data Stream frames into
 * the assistant turn as they arrive: text deltas append, `g:` streams the
 * host's live reasoning, `9:` adds a running tool card, `a:` resolves it,
 * `3:`/HTTP set a terminal error. Every failure lands as a visible error on
 * the turn — nothing is swallowed.
 *
 * Detached turns: the turn runs server-side in the turn
 * registry, independent of this connection. Phone sleeps / tab switches kill
 * only our tail; `reattach()` (called by the pane on mount/wake/reconnect)
 * finds the running turn and replays + follows it. `stop()` cancels the turn
 * SERVER-SIDE — it is a real stop, not a look-away.
 */
/** The key every copy this hook holds is filed under (the book's HeldSend
 * key, app-web-kit/book/nudge.ts). The hook holds only the copies of the
 * thread on screen: reset() drops the rest when the thread changes. */
const HELD_KEY = "this-thread";

/** The Circles thread id of a hook thread key ("self:<id>"), or undefined for
 * a key that is not your Host's page. */
function selfThreadOf(key: string | null): string | undefined {
  return key && key.startsWith("self:") ? key.slice(5) : undefined;
}

/** A URL-quoted header (x-topic-title), read back; "" when it is absent or
 * not quoted right. */
function headerText(v: string | null): string {
  if (!v) return "";
  try {
    return decodeURIComponent(v);
  } catch {
    return "";
  }
}

/** The topic of the reply `asstId` (where lines drawn for its turn go). */
function replyTopic(prev: ChatMessage[], asstId: string): FeedTopic | undefined {
  return prev.find((m) => m.id === asstId)?.topic;
}

/** Lines for words the server holds (GET /api/circles/pending) that are not
 * on screen yet: each a nudge line, unread, just before the reply streaming
 * now (else at the end). Matched by the id the words were sent with. */
function withHeldLines(
  prev: ChatMessage[],
  words: Array<{ id: string; text: string; createdAt: string }>,
  topic?: FeedTopic,
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
    ...(topic ? { topic } : {}),
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

/** The key of your Host's whole conversation (one conversation):
 * cards read for it (raiseTickets, raisePairing) come up whatever topic the
 * stream on screen is in. */
export const CONVERSATION_KEY = "self:*";

export function useCirclesChat(opts: {
  /** Draw a nudge's line the moment it is sent, mark it read on the turn's
   * receipt, and take it away when the turn ended without reading it and the
   * words go as the next message instead (the cards' back, as the
   * Workbench's page does). Off for the rail, which draws a nudge once read. */
  echoNudges?: boolean;
  /** The Host's pick filed a turn's exchange in another topic (its
   * thread_moved frame): `to` is where, `from` the thread it left, `side`
   * whether it was a turn streaming beside the followed one. */
  onThreadMoved?: (to: FeedTopic, from: string | null, side: boolean) => void;
} = {}) {
  const echoNudges = !!opts.echoNudges;
  const onThreadMovedRef = useRef(opts.onThreadMoved);
  onThreadMovedRef.current = opts.onThreadMoved;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Mirror messages into a ref so async continuations (the post-send thread
  // refresh) can read the CURRENT thread length, not a stale closure value.
  const messagesRef = useRef<ChatMessage[]>([]);
  messagesRef.current = messages;
  const [status, setStatus] = useState<ChatStatus>("ready");
  // Latest conversation-depth frame (`2:[{t:"context",...}]`) — drives the
  // header's depth bar and lights the View-context button. Null until the
  // backend stamps one; reset() clears it on thread switch.
  const [contextFrame, setContextFrame] = useState<ContextFrame | null>(null);
  // Asks the Host raised (an `escalate` whose result is an open ticket, on a
  // live turn) and the open tickets of the thread on screen (raiseTickets:
  // the pane reads them on opening the thread and on its poll) —
  // the overlay shows the first, the rest wait. A card set aside with "Not
  // now" is remembered (isSetAside) and not raised again.
  const [asks, setAsks] = useState<Ask[]>([]);
  // The cards up now, for the poll's read-back (raiseTickets): which of them
  // the thread's open-ticket list no longer names.
  const asksRef = useRef<Ask[]>([]);
  asksRef.current = asks;
  // The pairing code card: a connector pairing the Host started in this
  // thread (its tool result carries the grant id, never the code).
  const [pairing, setPairing] = useState<PairingAsk | null>(null);
  const settledPairingsRef = useRef<Set<number>>(new Set());
  // Tickets answered or set aside on this page. A reattach replays the turn's
  // frames from the start, so the same escalate result arrives again; a
  // settled ticket must not come back as a card.
  const settledRef = useRef<Set<number>>(new Set());
  // Which thread the hook is on (the pane's `${kind}:${ref}` key, given to
  // reset(); a send computes the same). A ticket read that started on one
  // thread and resolves after a switch must not put its card on the next
  // thread — and on a page reload the reattached stream and the thread load
  // race, so a read whose thread is not yet known is kept, not dropped.
  const threadKeyRef = useRef<string | null>(null);
  // Nudges this page sent into a running turn and has not yet seen READ (the
  // runtime answers each drained nudge with a 2: nudge frame). A turn that
  // ends with one still unread never read it — the ask card's answer would
  // be decided on the ticket and lost — so it goes as the next message.
  // Only words the server did NOT take are held here (the
  // request failed, or the turn had no session yet), or did not write down
  // (PII mode, `held: false`): words the nudge route held are the server's
  // (pending_words), never sent again from this page.
  const pendingNudgesRef = useRef<Array<HeldSend & { target: Target; threadId: string | null }>>([]);
  // The page whose turn the server started with words this page sent (a
  // nudge_requeued frame, or a nudge that started a turn of its own): the
  // ready edge attaches to that turn, so the words are seen going on.
  const followNextRef = useRef<string | null | undefined>(undefined);
  // The call side of each tool (name + args), by toolCallId, so the result
  // frame — which carries only the id — can be read as an Ask.
  const toolMetaRef = useRef<Map<string, { name: string; args: unknown }>>(new Map());
  const abortRef = useRef<AbortController | null>(null);
  // Replies streaming beside the followed turn, on other topics (sendBeside):
  // each one's stream, and its turn's session once its frame arrives (words
  // for that topic go into that turn).
  const besideAbortRef = useRef<Map<string, AbortController>>(new Map());
  const besideSessionRef = useRef<Map<string, string>>(new Map());
  // Captured from the `2:[{type:"session"}]` frame — the live tool-loop
  // (Nudge) target for the in-flight turn.
  const sessionIdRef = useRef<string | null>(null);
  // Captured from the `2:[{type:"turn"}]` frame — the registry id of the
  // in-flight turn: the reattach + server-side-cancel target.
  const turnIdRef = useRef<string | null>(null);
  // Guards duplicate concurrent reattach attempts (visibility + online can
  // fire together on wake).
  const reattachingRef = useRef(false);
  // The platform's crisis notice: raised by the turn's
  // governance frame, up until the person dismisses it, across a reload.
  // Not a thread message, so reset() leaves it.
  const crisis = useCrisisNotice();
  const raiseCrisis = crisis.raise;

  /** Draw the words the server holds for page `threadKey` (sent while a turn
   * ran, not read yet), so they are seen to survive a reload. Dropped when
   * the page changed while the read was out. */
  const showHeld = useCallback(async (threadKey: string | null) => {
    const id = selfThreadOf(threadKey);
    if (id === undefined || !echoNudges) return;
    const r = await fetchPendingWords(id);
    if (!r.ok || threadKeyRef.current !== threadKey) return;
    const words = (r.data.words ?? []).map((w) => ({
      id: w.client_id || `held-${w.id}`,
      text: w.text,
      createdAt: w.received_at || new Date().toISOString(),
    }));
    // And the words this tab keeps for the page (PII mode: the server wrote
    // nothing down), until a turn has them.
    for (const w of readKept(kept(), KEPT)) {
      if (w.key === threadKey) words.push({ id: w.clientId, text: w.text, createdAt: w.at });
    }
    if (words.length) setMessages((prev) => withHeldLines(prev, words, topicTag(id)));
  }, [echoNudges]);

  // Re-run the ready edge below (its entries are read from the ref).
  const [drainTick, setDrainTick] = useState(0);
  // The sender ids this page has seen a turn READ (nudge_read receipts).
  const readIdsRef = useRef<Set<string>>(new Set());
  // deliverKept, for its own later look (a turn that is just ending).
  const deliverKeptRef = useRef<((threadKey: string | null, tries?: number) => Promise<void>) | null>(null);

  /** Words a previous load of this tab kept for page `threadKey` (PII mode)
   * and no turn has yet: once no turn runs on the page, those the turn they
   * went into never read (its replayed receipts say) go as the next
   * message, through the ready edge, once. While a turn runs this waits:
   * the reattach replays its receipts, and its end runs this again. */
  const deliverKept = useCallback(async (threadKey: string | null, tries = 0): Promise<void> => {
    const id = selfThreadOf(threadKey);
    if (id === undefined) return;
    const theirs = readKept(kept(), KEPT).filter((w) => w.key === threadKey && w.page !== PAGE);
    if (!theirs.length) return;
    const act = await fetchActiveTurn(id);
    if (!act.ok || (act.data.turn && act.data.turn.status === "running")) {
      // A turn runs (or the answer did not come): the end of a turn this
      // page follows runs this again; a turn that is just ending, or one this
      // page does not follow, is looked at again shortly.
      if (tries < 5) setTimeout(() => void deliverKeptRef.current?.(threadKey, tries + 1), 1000);
      return;
    }
    const unread: typeof theirs = [];
    for (const w of theirs) {
      const read = w.turnId ? await turnReadIds(w.turnId) : null;
      if (read && read.has(w.clientId)) dropKept(kept(), KEPT, w.clientId);
      else unread.push(w);
    }
    if (threadKeyRef.current !== threadKey) return;
    // Read again: a receipt, or another pass, may have settled one meanwhile.
    const still = readKept(kept(), KEPT).filter((w) => w.page !== PAGE).map((w) => w.clientId);
    const take = unread.filter(
      (w) =>
        still.includes(w.clientId) &&
        !readIdsRef.current.has(w.clientId) &&
        !pendingNudgesRef.current.some((p) => p.clientId === w.clientId),
    );
    if (!take.length) return;
    adoptKept(kept(), KEPT, take.map((w) => w.clientId), PAGE);
    for (const w of take) {
      pendingNudgesRef.current.push({
        key: HELD_KEY,
        target: w.target ?? { kind: "self", id: "self", label: "Your Host" },
        text: w.text,
        threadId: w.threadId ?? null,
        clientId: w.clientId,
      });
    }
    setDrainTick((t) => t + 1);
  }, []);
  deliverKeptRef.current = deliverKept;

  const reset = useCallback((history: ChatMessage[], threadKey: string | null = null) => {
    abortRef.current?.abort();
    abortRef.current = null;
    // A reply streaming beside goes with the screen it was on; its turn runs
    // on and its saved rows are read with the conversation.
    besideAbortRef.current.forEach((c) => c.abort());
    besideAbortRef.current.clear();
    besideSessionRef.current.clear();
    sessionIdRef.current = null;
    turnIdRef.current = null;
    setContextFrame(null);
    setMessages(history);
    setStatus("ready");
    // A card belongs to the thread that raised it; leaving the thread leaves
    // the card (the ticket is still in Tickets), and a read still in flight
    // for the old thread lands nowhere. A nudge still unread for the old
    // thread's turn is dropped with it: that turn is gone from this page.
    threadKeyRef.current = threadKey;
    // A copy this tab keeps of words the page leaves unsent goes to the next
    // visit of their page (or a reload), which sends them if no turn read them.
    const left = pendingNudgesRef.current.map((p) => p.clientId).filter((c): c is string => !!c);
    if (left.length) adoptKept(kept(), KEPT, left, "");
    pendingNudgesRef.current = [];
    followNextRef.current = undefined;
    setAsks([]);
    setPairing(null);
    void showHeld(threadKey);
    void deliverKept(threadKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Adopt a refetched thread by MERGING it into what's on screen (see
   * mergeHistory): server ids keep React keys stable and unchanged rows keep
   * object identity, so a background refresh renders as an append instead of
   * remounting the whole conversation. Unlike reset() this never aborts a
   * stream, never clears the depth frame, and never blanks the thread. */
  const reconcile = useCallback((history: ChatMessage[]) => {
    setMessages((prev) => mergeHistory(prev, history));
  }, []);

  /** Put an OLDER page of the thread above what's on screen (Peers and
   * People threads are read a page at a time). Rows already shown are kept as
   * they are; a later refresh keeps them too (mergeHistory never shrinks the
   * top). A saved row of a turn whose echo is on screen (a turn reattached
   * after a reload, whose rows are dated before the page opened) is that
   * echo's twin and is left out: the merge swaps the echo for it. */
  const prependOlder = useCallback((older: ChatMessage[]) => {
    setMessages((prev) => {
      const ids = new Set(prev.map((m) => m.id));
      const turns = new Set(prev.filter((m) => m.turnToken && !m.nudge).map((m) => `${m.role}:${m.turnToken}`));
      const fresh = older.filter(
        (m) => !ids.has(m.id) && !(m.turnToken && !m.nudge && turns.has(`${m.role}:${m.turnToken}`)),
      );
      return fresh.length ? [...fresh, ...prev] : prev;
    });
  }, []);

  /** FOLLOW the live turn into the thread the platform landed it in. The screen becomes that thread's saved history with this
   * send's own local bubbles (the optimistic user echo + the streaming
   * reply) kept at the end. Unlike reset() this never aborts the stream —
   * the stream IS the reply that landed there — and unlike reconcile() it
   * does not keep the thread the message was sent FROM on screen. Session,
   * turn and asks belong to the live turn and stay. */
  const follow = useCallback((history: ChatMessage[], threadKey: string | null = null, cutAt?: number) => {
    setMessages((prev) => {
      // THIS send's bubbles are the trailing run of local echoes — an earlier
      // errored echo from the thread the message was sent from does not
      // travel with the view.
      const tail: ChatMessage[] = [];
      for (let i = prev.length - 1; i >= 0 && isLocalEcho(prev[i].id); i--) tail.unshift(prev[i]);
      // The turn writes its user row at the start (and its reply at the end),
      // so the landed thread's history may already carry THIS turn: keep the
      // echoes (the stream is keyed on them) and drop the saved twin and
      // anything after it — that is this turn — until the post-send
      // reconcile swaps them exactly as it does for any send.
      const cut = cutAt ?? thisTurnStart(history, tail.find((m) => m.role === "user")?.text);
      return [...history.slice(0, cut), ...tail];
    });
    threadKeyRef.current = threadKey;
  }, []);

  const stop = useCallback(() => {
    // Server-side first: the turn is DETACHED — aborting the local fetch
    // alone would only stop watching while the engine (and its subscription
    // spend) kept running. That exact failure is why the registry exists.
    const tid = turnIdRef.current;
    if (tid) {
      void cancelTurn(tid);
    }
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("ready");
    // Stop is the followed turn's: a reply streaming beside, on another
    // topic, runs on.
    setMessages((prev) =>
      prev.map((m) => (m.streaming && !m.beside ? { ...m, streaming: false } : m)),
    );
  }, []);

  /** The shared frame-folding handlers — send and reattach must render one
   * turn identically, so there is exactly one construction of these. */
  const makeHandlers = useCallback(
    (asstId: string, t0: number, replay = false, side = false): CirclesStreamHandlers => {
      // `side`: a turn on another topic streaming beside the one this window
      // follows (sendBeside). Its frames draw its own reply only: the
      // window's session, turn, status and depth frame stay the followed
      // turn's.
      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) => prev.map((m) => (m.id === asstId ? fn(m) : m)));
      // A reattach replays everything the turn did before, as fast as the
      // wire carries it, then follows live. The server marks the seam with a
      // `replayed` frame (routes/circles.py turn_stream); the message stays
      // `replaying` until the first frame after it, which it is patched off
      // in the same commit as that frame's own effect. A server without the
      // mark leaves it replaying to the end: no step is said, never a wrong one.
      let replaying = replay;
      let caughtUp = false;
      const arrived = () => {
        if (!replaying || !caughtUp) return;
        replaying = false;
        patch((m) => ({ ...m, replaying: false }));
      };
      // The turn's context frame — carries WHO ANSWERED (model/provider/
      // failed_over) for the live meta line.
      let liveContext: ContextFrame | null = null;
      // This turn's token, once its session frame arrives — stamped on the
      // nudge bubbles drawn after it (stampTurnToken covers earlier ones).
      let turnToken: string | null = null;
      let liveUsage: {
        promptTokens?: number;
        completionTokens?: number;
        cachedTokens?: number;
        cacheWriteTokens?: number;
      } | null = null;
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
        onText: (_delta, full) => {
          live.text(full);
          // The first live words: drawn now, with `replaying` off, in one
          // commit, so they read as words arriving.
          if (replaying && caughtUp) {
            live.flush();
            arrived();
          }
        },
        onReasoning: (_delta, full) => {
          live.reasoning(full);
          arrived();
        },
        onTool: ({ toolCallId, toolName, args }) => {
          live.flush();
          arrived();
          toolMetaRef.current.set(toolCallId, { name: toolName, args });
          patch((m) => {
            const tool: ToolInvocation = {
              id: toolCallId,
              name: toolName,
              args,
              summary: summarizeToolArgs(args, toolName),
              verb: toolVerb(args),
              state: "input-available",
              // A dispatch spawns a background AGENT — render it as one
              // from the first frame, not as a generic tool card.
              kind: toolName === "dispatch" ? "agent" : "tool",
            };
            return { ...m, tools: [...m.tools, tool] };
          });
        },
        onResult: ({ toolCallId, result, status: st }) => {
          live.flush();
          arrived();
          // An `escalate` that filed an open ticket is an ask: raise the card —
          // unless it was settled on this page, or (a reload mid-turn replays
          // the frames with an empty settled set) the ticket is no longer open
          // on the server. The read is cheap and the card is not.
          const meta = toolMetaRef.current.get(toolCallId);
          if (meta) {
            const ask = askFromTool({ id: toolCallId, name: meta.name, args: meta.args, result });
            if (ask && !settledRef.current.has(ask.ticketId) && !isSetAside(`ticket:${ask.ticketId}`)) {
              const raisedOn = threadKeyRef.current;
              void getTicket(ask.ticketId).then((r) => {
                const now = threadKeyRef.current;
                if (raisedOn && now && raisedOn !== now) return;
                if (!r.ok || !ticketIsOpen(r.data) || settledRef.current.has(ask.ticketId)) return;
                setAsks((q) => queueAsk(q, ask));
              });
            }
            // A connector pairing the Host just started: the code card. It
            // reads the code itself; a replayed stream for a pairing already
            // closed reads 404 and the card never shows.
            const pa = pairingFromTool({ id: toolCallId, name: meta.name, args: meta.args, result });
            if (pa && !settledPairingsRef.current.has(pa.grantId) && !isSetAside(`pairing:${pa.grantId}`)) {
              setPairing(pa);
            }
          }
          patch((m) => ({
            ...m,
            tools: m.tools.map((t) => {
              if (t.id !== toolCallId) return t;
              // dispatch returns {status:"dispatched", task_id} — carry the
              // task id onto the agent card.
              const taskId =
                result && typeof result === "object" && "task_id" in result
                  ? String((result as { task_id?: string | number }).task_id ?? "")
                  : undefined;
              return {
                ...t,
                result,
                taskId: taskId || t.taskId,
                // st is pre-normalized by toolStatus(): denied/blocked → "error".
                state: st === "error" ? "output-error" : "output-available",
              };
            }),
          }));
        },
        onData: (item) => {
          live.flush();
          const it = item as {
            type?: string;
            session_id?: string;
            turn_token?: string;
            turn_id?: string;
            text?: string;
            client_id?: unknown;
          };
          // The seam between the replay and the live frames (see `replaying`).
          if (it && it.type === "replayed") {
            caughtUp = true;
            return;
          }
          arrived();
          if (it && it.type === "session" && it.session_id) {
            if (side) besideSessionRef.current.set(asstId, it.session_id);
            else sessionIdRef.current = it.session_id;
          }
          // This turn's token — the one its saved rows carry. Stamped on both
          // echoes so the merge swaps them for those rows exactly, whatever
          // text the saved reply ended with.
          if (it && it.type === "session" && it.turn_token) {
            const token = it.turn_token;
            turnToken = token;
            setMessages((prev) => stampTurnToken(prev, asstId, token));
          }
          // The registry's turn-metadata frame: remember the turn id — it is
          // the reattach + cancel handle for THIS turn.
          if (it && it.type === "turn" && it.turn_id) {
            if (side) {
              const tid = it.turn_id;
              patch((m) => ({ ...m, besideTurnId: tid }));
            } else turnIdRef.current = it.turn_id;
          }
          // Another client (or a retry of ours) nudged the running turn —
          // surface the redirect in the thread, just before the live bubble,
          // instead of hiding it.
          // The runtime READ a nudge (its receipt). Ours: it no longer needs
          // to be sent as a message when the turn ends, and — since a nudge
          // this page sent has no other frame — it is drawn as the person's
          // words now. Not ours: another client's message, already drawn by
          // the registry's `nudge` frame; nothing to add. Matched by the id
          // the words were nudged with, which the receipt echoes (client_id):
          // the platform cuts secret shapes from a nudge before the turn
          // reads it, so the receipt's text is not always the text as sent.
          // A receipt with no id (an older kernel) is matched on the opening
          // of the text (app-web-kit/book/nudge.ts readIndex).
          if (it && it.type === "nudge_read" && it.text) {
            const seen = String(it.text);
            const readId = typeof it.client_id === "string" && it.client_id ? it.client_id : null;
            const idx = readIndex(pendingNudgesRef.current, HELD_KEY, seen, readId);
            const held = idx >= 0 ? pendingNudgesRef.current.splice(idx, 1)[0] : null;
            // The turn has the words: a copy this tab kept of them goes.
            const settledId = held?.clientId ?? readId;
            if (settledId) {
              readIdsRef.current.add(settledId);
              dropKept(kept(), KEPT, settledId);
            }
            if (echoNudges) {
              // The receipt marks the line read: the one drawn when it was
              // sent (or by the registry's frame), matched by its id, else by
              // its words; a receipt with no line yet (a reload replaying the
              // turn) draws its line, read.
              const id = held?.clientId ?? readId;
              setMessages((prev) => {
                const at = prev.findIndex(
                  (m) =>
                    m.nudge === true &&
                    !m.nudgeRead &&
                    isLocalEcho(m.id) &&
                    (id ? m.nudgeId === id : nudgeKey(m.text) === nudgeKey(seen)),
                );
                if (at >= 0) {
                  return prev.map((m, i) =>
                    i === at ? { ...m, text: seen, nudgeRead: true, ...(turnToken ? { turnToken } : {}) } : m,
                  );
                }
                const topic = replyTopic(prev, asstId);
                const line: ChatMessage = {
                  id: localId("u"),
                  role: "user",
                  text: seen,
                  tools: [],
                  createdAt: new Date().toISOString(),
                  nudge: true,
                  nudgeRead: true,
                  ...(id ? { nudgeId: id } : {}),
                  ...(turnToken ? { turnToken } : {}),
                  ...(topic ? { topic } : {}),
                };
                const i = prev.findIndex((m) => m.id === asstId);
                return i < 0 ? [...prev, line] : [...prev.slice(0, i), line, ...prev.slice(i)];
              });
            } else if (held) {
              setMessages((prev) => {
                const topic = replyTopic(prev, asstId);
                const bubble: ChatMessage = {
                  id: localId("u"),
                  role: "user",
                  text: seen,
                  tools: [],
                  createdAt: new Date().toISOString(),
                  nudge: true,
                  ...(turnToken ? { turnToken } : {}),
                  ...(topic ? { topic } : {}),
                };
                const i = prev.findIndex((m) => m.id === asstId);
                if (i < 0) return [...prev, bubble];
                return [...prev.slice(0, i), bubble, ...prev.slice(i)];
              });
            }
          }
          // The words of this send are in the thread already (a second send of
          // words kept in this tab, whose turn the server no longer lists):
          // nothing ran, and its bubbles give way to the row that holds them.
          if (it && it.type === "words_delivered") {
            setMessages((prev) => {
              const i = prev.findIndex((m) => m.id === asstId);
              if (i < 0) return prev;
              const from = i > 0 && prev[i - 1].role === "user" && isLocalEcho(prev[i - 1].id) ? i - 1 : i;
              return [...prev.slice(0, from), ...prev.slice(i + 1)];
            });
          }
          // The turn ended without reading words sent into it, and the server
          // sent them on as the thread's next turn: their lines give way to
          // that turn's own message, which the ready edge attaches to.
          if (it && it.type === "nudge_requeued") {
            const ids = Array.isArray((it as { client_ids?: unknown }).client_ids)
              ? ((it as { client_ids: unknown[] }).client_ids.filter((x) => typeof x === "string") as string[])
              : [];
            if (ids.length) setMessages((prev) => prev.filter((m) => !(m.nudge && !m.nudgeRead && m.nudgeId && ids.includes(m.nudgeId))));
            if (!side) followNextRef.current = selfThreadOf(threadKeyRef.current) ?? null;
          }
          if (it && it.type === "nudge" && it.text) {
            const frameId = typeof it.client_id === "string" && it.client_id ? it.client_id : null;
            setMessages((prev) => {
              // echoNudges: a line already drawn for these words (sent from
              // here) is not drawn twice.
              if (echoNudges && frameId && prev.some((m) => m.nudge === true && m.nudgeId === frameId)) return prev;
              const topic = replyTopic(prev, asstId);
              const bubble: ChatMessage = {
                id: localId("u"),
                role: "user",
                text: it.text as string,
                tools: [],
                createdAt: new Date().toISOString(),
                nudge: true,
                ...(echoNudges && frameId ? { nudgeId: frameId } : {}),
                ...(turnToken ? { turnToken } : {}),
                ...(topic ? { topic } : {}),
              };
              const idx = prev.findIndex((m) => m.id === asstId);
              if (idx < 0) return [...prev, bubble];
              return [...prev.slice(0, idx), bubble, ...prev.slice(idx)];
            });
          }
          // The Host's pick filed this exchange in another topic (its
          // pick_thread; brain_service.py's thread_moved frame, sent once the
          // move is made): the turn's echoes go with it, and the window is
          // told, so the thread on screen follows the words.
          if (it && it.type === "thread_moved") {
            const mv = it as { to?: { kind?: unknown; ref?: unknown }; from?: { kind?: unknown; ref?: unknown };
              to_title?: unknown };
            const to = mv.to && mv.to.kind === "thread" && mv.to.ref ? topicTag(String(mv.to.ref),
              typeof mv.to_title === "string" ? mv.to_title : "") : undefined;
            if (to) {
              setMessages((prev) => retagTurn(prev, asstId, to));
              const from = mv.from && mv.from.kind === "thread" && mv.from.ref ? String(mv.from.ref) : null;
              if (!side) threadKeyRef.current = selfThreadKey(to.id);
              onThreadMovedRef.current?.(to, from, side);
            }
          }
          // The platform's crisis check flagged the person's words this turn
          // (2:[{type:"governance",kind:"crisis"}]): the platform's own notice,
          // over the page — the reply is the Host's and streams on. Keyed by
          // the turn, so a replay of a turn whose notice was dismissed does
          // not raise it again.
          if (isCrisisFrame(it)) {
            raiseCrisis(turnToken ?? turnIdRef.current ?? asstId);
          }
        },
        onError: (message) => {
          live.flush();
          arrived();
          patch((m) => ({ ...m, streaming: false, error: message }));
          if (!side) setStatus("error");
        },
        onContext: (cf) => {
          liveContext = cf;
          if (!side) setContextFrame(cf);
        },
        onUsage: (u) => {
          liveUsage = u;
        },
        onDone: () => {
          live.flush();
          arrived();
          const genMs = Math.round(performance.now() - t0);
          // promptTokens is already the lane-normalized true context size
          // (cached + uncached) — the cached split rides alongside for the
          // meta line. Old servers omit the cache fields; render degrades.
          const tin = liveUsage?.promptTokens ?? null;
          const tout = liveUsage?.completionTokens ?? null;
          const tcached = liveUsage?.cachedTokens ?? null;
          const tcacheW = liveUsage?.cacheWriteTokens ?? null;
          if (!side) turnIdRef.current = null;
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
                tokens_cache_write: tcacheW,
                model: liveContext?.model ?? null,
                provider: liveContext?.provider ?? null,
                failed_over: liveContext?.failed_over ?? null,
                // The price rides the same frame as the provenance, so the
                // live turn prints it without waiting for a reload.
                est_cost_usd: liveContext?.est_cost_usd ?? null,
                price_basis: liveContext?.price_basis ?? null,
                close_turn: liveContext?.close_turn ?? null,
                tokens_total:
                  tin != null || tout != null ? (tin ?? 0) + (tout ?? 0) : null,
              },
              error: silent
                ? "No output received from the host — silent turns are a bug, not a shrug. Check host status in the Orchestrator and report this."
                : m.error,
            };
          });
          if (!side) setStatus("ready");
        },
      };
    },
    [echoNudges, raiseCrisis],
  );

  /**
   * Reattach to the thread's running (or just-finished) turn, if any.
   * Called by the pane on mount, visibilitychange→visible, and online —
   * the phone-wake path. Replays the whole turn from seq 0 (the optimistic
   * bubbles from before the disconnect are replaced wholesale) and follows
   * live. No-op when nothing is running or we're already streaming.
   * Returns true when it attached.
   *
   * `threadId` scopes the lookup to one Circles thread ("original" | uuid,
   * self conversation only) — the server uses it to find the running turn's
   * lane, so a turn on another thread never reattaches here.
   */
  const reattach = useCallback(async (threadId?: string | null): Promise<boolean> => {
    if (reattachingRef.current) return false;
    if (status === "submitted" || status === "streaming") return false;
    reattachingRef.current = true;
    try {
      const res = await fetchActiveTurn(threadId);
      // Running turns only: a finished turn's frames are persisted as normal
      // turn rows — the pane's thread reload renders those; replaying them
      // here would double the bubble.
      if (!res.ok || !res.data.turn || res.data.turn.status !== "running") {
        return false;
      }
      const turn: ActiveTurn = res.data.turn;

      // Drop any local remnants of this same turn (optimistic bubbles from a
      // send whose connection died) — the replay re-renders it completely.
      const asstId = localId("a");
      const now = new Date().toISOString();
      // The topic the turn was asked for: its echoes show on that topic.
      const topic = topicTag(threadId);
      setMessages((prev) => {
        const trimmed = [...prev];
        while (
          trimmed.length > 0 &&
          (trimmed[trimmed.length - 1].streaming ||
            // This turn's nudges: the replay redraws them from its frames.
            trimmed[trimmed.length - 1].nudge ||
            (trimmed[trimmed.length - 1].role === "user" &&
              trimmed[trimmed.length - 1].text.trim() ===
                turn.user_message.trim()))
        ) {
          trimmed.pop();
        }
        return [
          ...trimmed,
          {
            id: localId("u"),
            role: "user",
            text: turn.user_message,
            tools: [],
            createdAt: now,
            ...(topic ? { topic } : {}),
          },
          {
            id: asstId,
            role: "assistant",
            text: "",
            tools: [],
            createdAt: now,
            streaming: turn.status === "running",
            // A running turn is the self conversation's own Host's.
            ownHost: true,
            // What the stream carries first is the turn's past (makeHandlers).
            replaying: true,
            ...(topic ? { topic } : {}),
          },
        ];
      });
      turnIdRef.current = turn.turn_id;
      setStatus("streaming");

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      // Elapsed continuity: the turn started before we attached.
      const t0 = performance.now() - turn.elapsed_s * 1000;
      const resp = await openTurnStream(turn.turn_id, 0, ctrl.signal);
      if (!resp.ok) {
        setStatus("ready");
        return false;
      }
      void consumeCirclesStream(resp, makeHandlers(asstId, t0, true)).catch(() => {
        setStatus("ready");
      });
      // The replay redraws this turn's nudges from its frames; words the
      // server still holds for the page have no frame, so they are read back.
      void showHeld(threadKeyRef.current);
      return true;
    } finally {
      reattachingRef.current = false;
    }
  }, [status, makeHandlers, showHeld]);

  const send = useCallback(
    async (target: Target, text: string, opts?: SendOptions) => {
      const trimmed = text.trim();
      const attachments = opts?.attachments ?? [];
      // Allow an attachment-only send (the backend gives it a stub body); only
      // bail when there's nothing at all to send.
      const carry = target.kind === "self" ? (opts?.carry ?? null) : null;
      if (!trimmed && attachments.length === 0 && !carry) return;
      if (status === "submitted" || status === "streaming") return;

      // The thread this stream belongs to, in the pane's key shape.
      threadKeyRef.current =
        target.kind === "self" ? `self:${opts?.threadId ?? "original"}` : `${target.kind}:${target.id}`;

      const now = new Date().toISOString();
      // Optimistic user bubble. For an attachment-only send, mirror the
      // backend's stub body so the bubble isn't blank.
      const optimisticText =
        trimmed ||
        (attachments.length === 1
          ? "(sent an attachment)"
          : `(sent ${attachments.length} attachments)`);
      // The topic the words go to, when the send names it (no pick): they
      // show on it at once. A picked send's echoes take their topic from the
      // landing below, and until then show on whatever topic is on screen.
      const sentTopic = target.kind === "self" && !opts?.pickTopic ? topicTag(opts?.threadId) : undefined;
      const userMsg: ChatMessage = {
        id: localId("u"),
        role: "user",
        text: carry && !trimmed ? "" : optimisticText,
        tools: [],
        createdAt: now,
        ...(carry ? { carry } : {}),
        ...(sentTopic ? { topic: sentTopic } : {}),
      };
      const asstId = localId("a");
      const asstMsg: ChatMessage = {
        id: asstId,
        role: "assistant",
        text: "",
        tools: [],
        createdAt: now,
        streaming: true,
        // Only the self thread streams a model turn (Peers/People send plain).
        ownHost: target.kind === "self",
        ...(sentTopic ? { topic: sentTopic } : {}),
      };
      setMessages((prev) => [...prev, userMsg, asstMsg]);
      setStatus("submitted");

      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) => prev.map((m) => (m.id === asstId ? fn(m) : m)));

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      sessionIdRef.current = null;
      turnIdRef.current = null;
      // Client-side turn clock + usage capture for the live bubble — the
      // durable stamp (server-side gen_ms/tokens in the turn row's metadata)
      // replaces these on the next thread load.
      const t0 = performance.now();

      // Where the message actually landed — the thread it was sent into
      // until the server says otherwise.
      let landedThreadId: string | null | undefined = opts?.threadId;
      try {
        const res = await sendMessageStream(target, trimmed, ctrl.signal, {
          model: opts?.model,
          effort: opts?.effort,
          attachments,
          pills: opts?.pills,
          threadId: opts?.threadId,
          turnTypeId: opts?.turnTypeId,
          carry,
          clientId: opts?.clientId,
          pickTopic: opts?.pickTopic,
          topicPicked: opts?.topicPicked,
        });
        if (!res.ok) {
          let detail = "";
          try {
            const j = (await res.json()) as {
              detail?:
                | string
                | { message?: string; error?: { message?: string; code?: string } };
              error?: string;
            };
            if (typeof j.detail === "string") {
              detail = j.detail;
            } else if (j.detail && typeof j.detail === "object") {
              detail = j.detail.error?.message || j.detail.message || "";
            }
            detail = detail || j.error || "";
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

        // The platform may have landed this message in another type's
        // thread. The live stream is the same turn either way; only the key
        // the pane reattaches on, and the view, follow it.
        if (target.kind === "self") {
          const landing = landingFrom(res.headers, opts?.threadId);
          if (landing) {
            landedThreadId = landing.threadId;
            threadKeyRef.current = selfThreadKey(landing.threadId);
            // This send's echoes are on the topic it landed on.
            const tag = topicTag(landing.threadId, headerText(res.headers.get("x-topic-title")));
            if (tag) {
              setMessages((prev) =>
                prev.map((m) => (m.id === asstId || m.id === userMsg.id ? { ...m, topic: tag } : m)),
              );
            }
            if (landing.moved) opts?.onLanded?.(landing);
          }
        }
        setStatus("streaming");
        await consumeCirclesStream(res, makeHandlers(asstId, t0));
      } catch (e) {
        const err = e as Error;
        if (err.name === "AbortError") {
          patch((m) => ({ ...m, streaming: false }));
          setStatus("ready");
          return;
        }
        // The CONNECTION died, not the turn — it keeps running detached in
        // the registry. Reattach instead of surfacing a dead-end network
        // error (the phone-screen-off case). Falls back to the error only
        // when there is genuinely nothing to reattach to.
        setStatus("ready");
        const attached = await reattach(target.kind === "self" ? landedThreadId : null);
        if (!attached) {
          patch((m) => ({
            ...m,
            streaming: false,
            error: err.message || "network error",
          }));
          setStatus("error");
        }
      } finally {
        abortRef.current = null;
      }
    },
    [status, makeHandlers, reattach],
  );

  /** Your Host's one conversation: a message for another topic while the
   * followed turn runs. Its own turn starts now and streams LIVE into the
   * conversation beside the followed one: its words, tools and steps as they
   * come, and its own working line (the reply is `beside`). The followed
   * turn's stream, status, Stop and depth frame are left as they are. When
   * its stream ends (or the connection goes, the turn running on detached)
   * `onEnded` runs, so the page reads the saved rows. An error to show, or
   * null. */
  const sendBeside = useCallback(
    async (
      target: Target,
      text: string,
      opts: SendOptions & { topic?: FeedTopic | null; onEnded?: () => void } = {},
    ): Promise<string | null> => {
      const trimmed = text.trim();
      if (!trimmed) return null;
      const now = new Date().toISOString();
      const topic = opts.topic && opts.topic.id ? opts.topic : undefined;
      const asstId = localId("a");
      setMessages((prev) => [
        ...prev,
        { id: localId("u"), role: "user", text: trimmed, tools: [], createdAt: now, ...(topic ? { topic } : {}) },
        {
          id: asstId,
          role: "assistant",
          text: "",
          tools: [],
          createdAt: now,
          streaming: true,
          ownHost: true,
          beside: true,
          ...(topic ? { topic } : {}),
        },
      ]);
      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) => prev.map((m) => (m.id === asstId ? fn(m) : m)));
      const ctrl = new AbortController();
      besideAbortRef.current.set(asstId, ctrl);
      const over = () => {
        besideAbortRef.current.delete(asstId);
        besideSessionRef.current.delete(asstId);
      };
      const t0 = performance.now();
      let res: Response;
      try {
        res = await sendMessageStream(target, trimmed, ctrl.signal, {
          model: opts.model,
          effort: opts.effort,
          threadId: opts.threadId,
          topicPicked: opts.topicPicked,
          pickTopic: opts.pickTopic,
          clientId: opts.clientId,
        });
      } catch (e) {
        over();
        const why = (e as Error).message || "network error";
        patch((m) => ({ ...m, streaming: false, error: why }));
        return `Couldn't send: ${why}`;
      }
      if (!res.ok) {
        over();
        patch((m) => ({ ...m, streaming: false, error: `${res.status} — ${res.statusText || "request failed"}` }));
        return `Couldn't send (${res.status}).`;
      }
      opts.onAccepted?.();
      void consumeCirclesStream(res, makeHandlers(asstId, t0, false, true))
        .catch(() => {
          /* the connection went: the turn runs on detached, and its saved
             rows come with the conversation's next read (onEnded) */
        })
        .finally(() => {
          over();
          patch((m) => (m.streaming ? { ...m, streaming: false } : m));
          opts.onEnded?.();
        });
      return null;
    },
    [makeHandlers],
  );

  /** Your Host's one conversation: the page names no topic until the person
   * writes, but a reload may find words held for (or kept in this tab for)
   * the topic of their last message. Adopt that topic's key without reading
   * anything in its place: the held words are drawn and kept words go as
   * their next message, as reset() does when a page opens. */
  const adoptThread = useCallback((threadKey: string) => {
    threadKeyRef.current = threadKey;
    void showHeld(threadKey);
    void deliverKept(threadKey);
  }, [showHeld, deliverKept]);

  /** The topics with a reply streaming beside the followed turn. */
  const besideTopics = useCallback(
    (): string[] =>
      messagesRef.current.filter((m) => m.beside && m.streaming && m.topic).map((m) => m.topic!.id),
    [],
  );

  /** Words for a topic whose turn streams beside the followed one: into that
   * turn, with their line just before its reply. False when no such turn has
   * a session yet, or the server did not take them (the caller sends them as
   * a message then). */
  const nudgeBeside = useCallback(async (topicId: string, text: string): Promise<boolean> => {
    const reply = messagesRef.current.find((m) => m.beside && m.streaming && m.topic?.id === topicId);
    const sid = reply ? besideSessionRef.current.get(reply.id) : undefined;
    if (!reply || !sid) return false;
    const clientId = wordsId("n");
    const r = await nudgeSession(sid, text, clientId);
    if (!r.ok) return false;
    setMessages((prev) => {
      const i = prev.findIndex((m) => m.id === reply.id);
      const line: ChatMessage = {
        id: localId("u"),
        role: "user",
        text,
        tools: [],
        createdAt: new Date().toISOString(),
        nudge: true,
        nudgeId: clientId,
        ...(reply.topic ? { topic: reply.topic } : {}),
      };
      return i < 0 ? [...prev, line] : [...prev.slice(0, i), line, ...prev.slice(i)];
    });
    return true;
  }, []);

  /** Say `text` into the RUNNING turn. True when the text is in the hook's
   * hands: either queued for the loop, or — the request failed while the
   * stream is up — held until the turn ends and sent as the next message
   * then. Whether the loop reads a queued nudge is known only from the 2:
   * nudge_read receipt, so a queued text is held the same way until then.
   * False only when there is no session to nudge, or the text is worth
   * saying only to a running turn (`fallback` false: a bare Not now) and the
   * request failed. A caller must not fall back to send() itself for a
   * failed request: its send is the click's render, whose guard refuses
   * while the turn streams — the answer would be decided and lost. */
  const nudgeRunningTurn = useCallback(
    async (target: Target, text: string, threadId: string | null, fallback = true): Promise<boolean> => {
      // Pending BEFORE anything else: if the turn ends while the request is
      // in flight, the ready edge below must already see it, or the answer
      // waits for the person's next turn and arrives out of order. And a
      // turn with no session id yet (the ready edge just started one, its
      // session frame not in) has nothing to nudge — the entry waits for
      // that turn's end the same way, never for a send that would refuse.
      // The words' id (client_id): the turn's nudge_read receipt echoes it,
      // and the held copy is settled by it.
      const entry = { key: HELD_KEY, target, text, threadId, clientId: wordsId("n") };
      if (fallback) pendingNudgesRef.current.push(entry);
      if (echoNudges && fallback) {
        // Its line, at once, just before the reply it went into.
        setMessages((prev) => {
          const topic = topicTag(threadId);
          const line: ChatMessage = {
            id: localId("u"),
            role: "user",
            text,
            tools: [],
            createdAt: new Date().toISOString(),
            nudge: true,
            nudgeId: entry.clientId,
            ...(topic ? { topic } : {}),
          };
          let i = -1;
          for (let k = prev.length - 1; k >= 0; k--) {
            if (prev[k].role === "assistant" && prev[k].streaming) {
              i = k;
              break;
            }
          }
          return i < 0 ? [...prev, line] : [...prev.slice(0, i), line, ...prev.slice(i)];
        });
      }
      const sid = sessionIdRef.current;
      if (!sid) return fallback;
      const r = await nudgeSession(sid, text, entry.clientId);
      if (r.ok && r.data.held === false && !r.data.own_turn) {
        // The server took the words into the turn but wrote nothing down (PII
        // mode: no raw text at rest), so a restart could lose them. The copy
        // stays: the turn's nudge_read receipt settles it, and with none the
        // ready edge sends it as the next message, as for a failed request.
        // It is kept in this tab too, so a reload does not lose it — unless
        // the receipt has come already (the copy is gone from the list).
        if (pendingNudgesRef.current.includes(entry) && !readIdsRef.current.has(entry.clientId)) {
          keepWords(kept(), KEPT, {
            key: `self:${threadId ?? "original"}`,
            text,
            clientId: entry.clientId,
            turnId: turnIdRef.current,
            page: PAGE,
            at: new Date().toISOString(),
            target,
            threadId,
          });
        }
        return true;
      }
      if (r.ok) {
        // The server took the words and owns them from here (written to the
        // Host's database before it answered): the turn reads them, or they
        // start the thread's next turn, after a restart too. No copy is kept
        // to send again — a second one would reach the Host twice.
        const at = pendingNudgesRef.current.indexOf(entry);
        if (at >= 0) pendingNudgesRef.current.splice(at, 1);
        if (r.data.own_turn) followNextRef.current = threadId;
        return true;
      }
      // A failed request leaves the entry pending: the server never had the
      // words, so the ready edge sends them as the next message.
      return fallback;
    },
    [echoNudges],
  );
  // The turn ended — cleanly or in error: anything nudged and never read
  // goes as a message. One at a time — send() streams the next turn, and
  // this runs again when it ends. A turn that failed is still an ended turn:
  // its unread answer must not wait for the person's next turn (out of
  // order) or die with a thread switch.
  // `drainTick` re-runs this after each attempt: a send the server rejects
  // leaves the status where it was (no edge), and the entries behind it
  // would otherwise wait for the person's next turn. Bounded by the list —
  // each attempt takes its entry out first.
  useEffect(() => {
    if (status !== "ready" && status !== "error") return;
    if (followNextRef.current !== undefined) {
      // The server started the thread's next turn with words sent here:
      // follow it, as a reattach does on opening the page.
      const tid = followNextRef.current;
      followNextRef.current = undefined;
      void reattach(tid).finally(() => setDrainTick((t) => t + 1));
      return;
    }
    const p = pendingNudgesRef.current.shift();
    if (!p) {
      // Words a previous load of this tab kept (PII mode) and no turn read.
      void deliverKept(threadKeyRef.current);
      return;
    }
    // Its line gives way to the message's own (echoNudges).
    if (echoNudges && p.clientId) setMessages((prev) => prev.filter((m) => !(m.nudge && m.nudgeId === p.clientId)));
    // A copy kept in this tab goes once a turn has the words: the send is
    // accepted (or has failed, as the page's own copy does).
    const settle = () => p.clientId && dropKept(kept(), KEPT, p.clientId);
    // Under the words' own id: a page that sends them twice (a reload before
    // the answer, a duplicated tab) or after the turn read them gets that
    // turn back, and the Host has them once.
    void send(p.target, p.text, { threadId: p.threadId, onAccepted: settle, clientId: p.clientId })
      .finally(() => {
        settle();
        setDrainTick((t) => t + 1);
      });
    // `send` is recreated on status changes; this effect only cares about
    // the ready/error edge and its own tick, and reads the list from the ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, drainTick]);

  // The card is answered (or set aside): drop it, the next one shows.
  // `setAside`: "Not now" — remembered on this device, so the thread's ticket
  // list does not raise it again (the ticket stays open in Tickets).
  const settleAsk = useCallback((ticketId: number, setAside = false) => {
    settledRef.current.add(ticketId);
    if (setAside) rememberSetAside(`ticket:${ticketId}`);
    setAsks((q) => settleAskIn(q, ticketId));
  }, []);

  /** A card set aside with "Not now" is wanted again (the person picked it
   * from Waiting on you): the next read of the page's tickets raises it. */
  const raiseAgain = useCallback((ticketId: number) => {
    settledRef.current.delete(ticketId);
    forgetSetAside(`ticket:${ticketId}`);
  }, []);

  /** The open tickets of the page on screen, read by the cards (on opening
   * the page, and on the 15 s poll: src/cards/use-host-thread.ts
   * refreshCards, src/cards/cards-app.tsx): each one not answered or set
   * aside comes up as its card, oldest first. Dropped when the page has
   * changed since the read started. A card up whose ticket the list no longer
   * names is read back, and goes when the ticket is no longer open — answered
   * in Network, in Waiting on you or in another window. */
  const raiseTickets = useCallback((tickets: unknown, threadKey: string) => {
    if (threadKey !== CONVERSATION_KEY && threadKeyRef.current !== threadKey) return;
    const fresh = asksFromTickets(
      tickets,
      (id) => settledRef.current.has(id) || isSetAside(`ticket:${id}`),
    );
    if (fresh.length) setAsks((q) => fresh.reduce((acc, a) => queueAsk(acc, a), q));
    const unlisted = unlistedAsks(asksRef.current, tickets);
    if (!unlisted.length) return;
    void closedAsks(unlisted, getTicket).then((closed) => {
      if (!closed.length) return;
      // Settled, so a list read before the answer and landing after it
      // does not raise the card again.
      for (const id of closed) settledRef.current.add(id);
      setAsks((q) => q.filter((a) => !closed.includes(a.ticketId)));
    });
  }, []);

  /** An open pairing this thread started, read back on opening the thread. */
  const raisePairing = useCallback((pa: PairingAsk | null, threadKey: string) => {
    if (!pa || (threadKey !== CONVERSATION_KEY && threadKeyRef.current !== threadKey)) return;
    if (settledPairingsRef.current.has(pa.grantId) || isSetAside(`pairing:${pa.grantId}`)) return;
    setPairing((cur) => cur ?? pa);
  }, []);

  /** The pairing card closed: by the person (remembered, like Not now), or
   * because the pairing moved on. */
  const settlePairing = useCallback((grantId: number, setAside: boolean) => {
    settledPairingsRef.current.add(grantId);
    if (setAside) rememberSetAside(`pairing:${grantId}`);
    setPairing((cur) => (cur && cur.grantId === grantId ? null : cur));
  }, []);

  return {
    messages,
    messagesRef,
    status,
    contextFrame,
    asks,
    settleAsk,
    raiseAgain,
    raiseTickets,
    pairing,
    raisePairing,
    settlePairing,
    crisisNotice: crisis.open,
    dismissCrisisNotice: crisis.dismiss,
    nudgeRunningTurn,
    send,
    sendBeside,
    adoptThread,
    besideTopics,
    nudgeBeside,
    stop,
    reset,
    reconcile,
    prependOlder,
    follow,
    reattach,
    sessionIdRef,
    turnIdRef,
  };
}
