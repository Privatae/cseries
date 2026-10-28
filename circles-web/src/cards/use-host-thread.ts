import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  answerText,
  type AskCredentialRequest,
  type AskOption,
  notNowText,
  type PairingAsk,
  pairingFromConnectors,
  roomOf,
  sealErrorText,
} from "@kit/ask";
import { CONVERSATION_KEY, useCirclesChat } from "@/hooks/use-circles-chat";
import {
  createThread,
  decideTicket,
  deleteConversation,
  deleteThread,
  fetchFeed,
  fetchThread,
  type FeedCursor,
  listConnectors,
  listMarkers,
  listRoomTickets,
  listThreadWakeups,
  listTurnTypes,
  mapThreadMessages,
  patchThread,
  pickTopic,
  putLastOpenThread,
  splitThread,
  storeVaultSecret,
  type ThreadMessageRaw,
  type ThreadWakeup,
} from "@/lib/api";
import { fetchHostThreads } from "@/lib/cards-api";
import { ORIGINAL, type ThreadRow } from "@/lib/cards";
import { isHolding } from "@/lib/holding";
import { isLocalEcho } from "@/lib/history-merge";
import { type Landing, normalizeThreadId, topicTag } from "@/lib/turn-types";
import type { AttachmentBlock, CarryRef, ChatMessage, FeedTopic, Marker, ModelPick, Target, TurnType } from "@/lib/types";

const SELF: Target = { kind: "self", id: "self", label: "Your Host" };

/** GET /api/tickets?asked_in= for every topic of your Host's conversation at
 * once (routes/tickets.py CONVERSATION_ROOM). */
export const CONVERSATION_ROOM = "thread:*";

/** The open pairing started on any topic of the conversation: the newest
 * one of those each topic's own room would show (pairingFromConnectors). */
export function conversationPairing(list: unknown): PairingAsk | null {
  const rows = Array.isArray(list) ? list : [];
  const rooms = new Set<string>();
  for (const row of rows) {
    const r = row && typeof row === "object" ? roomOf((row as { asked_in?: unknown }).asked_in) : null;
    if (r && r.startsWith("thread:")) rooms.add(r);
  }
  let best: PairingAsk | null = null;
  for (const room of rooms) {
    const pa = pairingFromConnectors(rows, room);
    if (pa && (!best || pa.grantId > best.grantId)) best = pa;
  }
  return best;
}

/**
 * Your Host's back: one conversation, the page on screen, held for the whole
 * app so a turn keeps streaming while the person reads another card, and so
 * Ask Ivy can hand a message to a page the back is not showing.
 *
 * The turn machinery is the rail's own (hooks/use-circles-chat.ts): a send
 * streams the runtime's frames, a turn that outlives the connection is
 * reattached (on opening a page, on wake, on the network coming back), words
 * typed while a turn runs go into it as a nudge, and the ask card comes up
 * for the page's open tickets. It also keeps what the rail's thread list and
 * chat view did to a page: rename, close and reopen, clear, delete, the
 * topic markers with Split here, and the turn-type pick for the next send.
 * Regenerate and A/B are not carried over.
 *
 * ONE CONVERSATION. The back opens on a
 * blank page with a greeting at every login, never on the page last open
 * (last-open is for links only, openOn). The person never picks a thread:
 * each send asks the Host for the topic (pick_topic), and `threadId` is only
 * the topic their last message went to.
 *
 * ONE THREAD ON SCREEN: circles chat history shows only the thread the
 * message was placed in. The back shows the thread the person's last
 * message went to (`viewTopic`: threadId, else on a blank page the one they last wrote in,
 * which "Where this stands" names): this visit's messages of that thread
 * (`shown`), and its earlier ones only when the person asks for them
 * (Earlier messages, that thread's own pages of GET /api/circles/feed?thread=).
 * When the platform lands a message in another thread, the screen follows
 * the words to that thread; when the Host's pick files an exchange there
 * (thread_moved), it follows only from the thread the exchange left. The
 * back once showed one feed over every topic, and scrolling near its top
 * read the next 50 rows of it on its own: a reply growing past the box did that, and the page jumped into old history.
 * While a turn runs, the Host is asked first (POST /api/circles/topic): words
 * for the running topic go into it as a nudge, words for another topic start
 * their own turn at once, side by side. The root thread is written to only
 * when the person chooses "Write in Root" on its line (rootPin): it is never
 * picked into or out of.
 */
export function useHostThread() {
  // The Host's pick filed a turn's exchange elsewhere: what to do is set
  // once the page's own callbacks exist (below).
  const movedRef = useRef<(to: FeedTopic, from: string | null, side: boolean) => void>(() => {});
  const chat = useCirclesChat({
    echoNudges: true,
    onThreadMoved: (to, from, side) => movedRef.current(to, from, side),
  });
  const {
    messages,
    messagesRef,
    status,
    asks,
    settleAsk,
    raiseAgain,
    raiseTickets,
    pairing,
    raisePairing,
    settlePairing,
    nudgeRunningTurn,
    send,
    stop,
    reset,
    reconcile,
    reattach,
  } = chat;

  const [threads, setThreads] = useState<ThreadRow[]>([]);
  const [threadsError, setThreadsError] = useState<string | null>(null);
  // undefined until the last open page is known (boot), then the page.
  const [threadId, setThreadIdState] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const savedRef = useRef<string | null>(null);
  // The page's topic markers (GET /api/circles/threads/{id}/markers), each
  // with a Split here; the rail read them with the page (conversations-
  // pane.tsx loadThread).
  const [markers, setMarkers] = useState<Marker[]>([]);
  // The Host's turn types (GET /api/turn-types) and the pick for the NEXT
  // send: "" lets the platform decide (the page's own type, else the scorer).
  // A Host with no types has none and the back shows no picker.
  const [turnTypes, setTurnTypes] = useState<TurnType[]>([]);
  const [turnTypeId, setTurnTypeId] = useState("");

  const setThreadId = useCallback((id: string) => {
    setThreadIdState(id);
    if (savedRef.current === id) return;
    savedRef.current = id;
    void putLastOpenThread(id);
  }, []);

  // Boot: a blank page. The newest page of the feed is read and held back:
  // it is what the first scroll up shows, and everything newer than its
  // newest row is this visit's and shows as it comes. A link that names a
  // page opens that page (openOn); last-open serves links only.
  const linkRef = useRef<string | null>(null);
  /** The blank page: no topic until the person writes, and the next message
   * picks its topic. There is no original conversation,
   * so this is where a link naming it, or a deleted page, lands. */
  const openBlank = useCallback(() => {
    linkRef.current = null;
    savedRef.current = null;
    setThreadIdState(undefined);
  }, []);
  const openOn = useCallback(
    (id: string) => {
      if (normalizeThreadId(id) === ORIGINAL) {
        openBlank();
        return;
      }
      linkRef.current = id;
      setThreadId(id);
    },
    [setThreadId, openBlank],
  );
  // The newest row of the conversation when the page opened (its time and
  // id): everything after it is this visit's and comes with the feed's
  // re-reads; a thread's earlier messages are read from it back.
  const floorRef = useRef<string>("");
  const floorIdRef = useRef<number | null>(null);
  const [booted, setBooted] = useState(false);
  // The root thread's id, once a feed row names it, and whether the person
  // chose to write there (the only way words reach it from this page).
  const rootIdRef = useRef<string | null>(null);
  const [rootPin, setRootPin] = useState<string | null>(null);
  const noteRoot = useCallback((rows: ChatMessage[]) => {
    for (const m of rows) if (m.topic?.root) rootIdRef.current = m.topic.id;
  }, []);
  // The topic the person last wrote to before this visit (the newest of
  // their own messages in the feed): what "Where this stands" shows on a
  // blank page, until they write (then threadId, their topic now).
  const [lastWritten, setLastWritten] = useState<string | null>(null);
  useEffect(() => {
    let off = false;
    void fetchFeed().then((r) => {
      if (off) return;
      if (r.ok) {
        const rows = mapThreadMessages(r.data.messages, true);
        noteRoot(rows);
        const newest = rows.length ? rows[rows.length - 1] : null;
        floorRef.current = newest ? newest.createdAt : "";
        floorIdRef.current = newest && /^\d+$/.test(newest.id) ? Number(newest.id) : null;
        const mine = [...rows].reverse().find((m) => m.role === "user" && !m.topicMarker && m.topic?.id);
        setLastWritten(mine?.topic?.id ?? null);
        // A turn still running on that topic (a reload mid-turn): followed
        // again, so the page shows it working at once.
        const last = mine?.topic?.id ?? ORIGINAL;
        chat.adoptThread(`self:${normalizeThreadId(last)}`);
        void reattachRef.current(last);
      }
      setBooted(true);
      // The conversation's cards, at once: a blank page names no topic.
      void refreshCardsRef.current(true);
    });
    return () => {
      off = true;
    };
  }, [noteRoot]);

  // The thread on screen (see the head of this hook): the one the person's
  // last message went to, else the one they last wrote in before this visit.
  const viewTopic = (() => {
    const t = normalizeThreadId(threadId ?? lastWritten);
    return t === ORIGINAL ? null : t;
  })();
  const viewTopicRef = useRef(viewTopic);
  viewTopicRef.current = viewTopic;
  /** What the back shows: the messages of the thread on screen. An echo
   * that names no topic yet (a send whose landing has not come back) is the
   * person's words of now, and shows wherever they are. */
  const shown = useMemo(
    () => (viewTopic ? messages.filter((m) => !m.topic || normalizeThreadId(m.topic.id) === viewTopic) : messages),
    [messages, viewTopic],
  );

  // Each thread's earlier messages: the page before what of it is on screen,
  // read ahead when the thread comes on screen and held (so Earlier messages
  // shows only when there is something, and puts it up at once), and the
  // cursor to the page before that (null: nothing before).
  type Earlier = { held: ChatMessage[]; next: FeedCursor | null };
  const earlierRef = useRef<Map<string, Earlier>>(new Map());
  const earlierGenRef = useRef<Map<string, number>>(new Map());
  const [earlierTick, setEarlierTick] = useState(0);
  /** Where a thread's earlier messages start: before its oldest saved row on
   * screen, else before this visit (the floor), else its newest page. */
  const earlierCursor = useCallback((topic: string): FeedCursor | null => {
    for (const m of messagesRef.current) {
      if (isLocalEcho(m.id) || !/^\d+$/.test(m.id) || !m.topic || normalizeThreadId(m.topic.id) !== topic) continue;
      return { before: m.createdAt, before_id: Number(m.id) };
    }
    if (floorRef.current && floorIdRef.current != null) {
      return { before: floorRef.current, before_id: floorIdRef.current + 1 };
    }
    return null;
  }, [messagesRef]);
  /** Read the page before `cursor` of thread `topic` (mapped, its topic on
   * every row); null when the read failed or a newer read of the thread
   * started meanwhile. */
  const readEarlier = useCallback(async (topic: string, cursor: FeedCursor | null, gen: number) => {
    const r = await fetchFeed(cursor, topic);
    if (!r.ok || earlierGenRef.current.get(topic) !== gen) return null;
    const rows = mapThreadMessages(r.data.messages, true).filter(
      (m) => !m.topic || normalizeThreadId(m.topic.id) === topic,
    );
    noteRoot(rows);
    return { rows, next: r.data.next };
  }, [noteRoot]);
  /** Forget what was read of thread `topic`'s earlier messages (its rows on
   * screen changed under it: a link read the thread again), so it is read
   * again from what is on screen now. */
  const forgetEarlier = useCallback((topic: string) => {
    earlierRef.current.delete(topic);
    earlierGenRef.current.set(topic, (earlierGenRef.current.get(topic) ?? 0) + 1);
    setEarlierTick((t) => t + 1);
  }, []);
  useEffect(() => {
    if (!booted || !viewTopic || earlierRef.current.has(viewTopic)) return;
    const gen = (earlierGenRef.current.get(viewTopic) ?? 0) + 1;
    earlierGenRef.current.set(viewTopic, gen);
    const topic = viewTopic;
    void readEarlier(topic, earlierCursor(topic), gen).then((got) => {
      if (!got) return;
      earlierRef.current.set(topic, { held: got.rows, next: got.next });
      setEarlierTick((t) => t + 1);
    });
  }, [booted, viewTopic, earlierTick, readEarlier, earlierCursor]);
  const earlierOfView = viewTopic ? earlierRef.current.get(viewTopic) : undefined;
  const hasEarlier = !!earlierOfView && (earlierOfView.held.length > 0 || earlierOfView.next != null);

  /** Put the page before what is on screen of the thread in view above it.
   * `put` makes the change: it is handed the change, so the caller can make
   * it in one commit and keep the reader's place (own-back.tsx). True when
   * something was added. */
  const earlierBusyRef = useRef(false);
  const loadEarlier = useCallback(
    async (put: (apply: () => void) => void = (apply) => apply()): Promise<boolean> => {
      const topic = viewTopicRef.current;
      if (!topic || earlierBusyRef.current) return false;
      const st = earlierRef.current.get(topic);
      if (!st) return false;
      earlierBusyRef.current = true;
      try {
        let rows = st.held;
        let next = st.next;
        if (!rows.length) {
          if (!next) return false;
          const gen = earlierGenRef.current.get(topic) ?? 0;
          const got = await readEarlier(topic, next, gen);
          if (!got) return false;
          rows = got.rows;
          next = got.next;
        }
        // Another thread came on screen while the page was read: it waits,
        // held, for this thread's next Earlier.
        const away = viewTopicRef.current !== topic;
        earlierRef.current.set(topic, { held: away ? rows : [], next });
        setEarlierTick((t) => t + 1);
        if (!rows.length || away) return false;
        put(() => chat.prependOlder(rows));
        return true;
      } finally {
        earlierBusyRef.current = false;
      }
    },
    [chat.prependOlder, readEarlier],
  );

  /** This visit's part of the feed read again and merged in place: what the
   * Host wrote in any topic since the page opened, a turn's saved rows, a
   * topic line. Returns the rows read (this visit's), or null. */
  const reloadFeed = useCallback(async (): Promise<ChatMessage[] | null> => {
    const r = await fetchFeed();
    if (!r.ok) return null;
    const rows = mapThreadMessages(r.data.messages, true).filter((m) => m.createdAt > floorRef.current);
    noteRoot(rows);
    if (rows.length) reconcile(rows);
    return rows;
  }, [reconcile, noteRoot]);

  const loadThreads = useCallback(async () => {
    const r = await fetchHostThreads();
    if (r.ok) {
      setThreadsError(null);
      setThreads(r.data.threads);
    } else {
      setThreadsError(`${r.status || ""} ${r.message}`.trim());
    }
    return r.ok ? r.data.threads : null;
  }, []);

  useEffect(() => {
    void loadThreads();
    // Best-effort, as the rail's was: a server with no turn types answers
    // 404 and no picker shows.
    void listTurnTypes().then((r) => r.ok && setTurnTypes(r.data.types ?? []));
  }, [loadThreads]);

  // The markers belong to the page they were read for: a stale answer for a
  // page left since is dropped, so a Split here never names another page's
  // turn (the server refuses one, but the back should not offer it).
  const markersForRef = useRef<string | null>(null);
  const loadMarkers = useCallback(async (id: string) => {
    markersForRef.current = id;
    const r = await listMarkers(id);
    if (markersForRef.current === id) setMarkers(r.ok ? (r.data.markers ?? []) : []);
  }, []);

  // Which page the messages on screen belong to: a SWITCH resets, a REFRESH
  // merges in place. A send the platform landed on another topic moves
  // nothing (one conversation): the feed on screen holds every
  // topic, so the live turn streams on where it is (handleLanded).
  const loadedKeyRef = useRef<string | null>(null);
  const landedThreadRef = useRef<string | null>(null);

  const loadThread = useCallback(
    async (id: string) => {
      const key = `self:${id}`;
      const isRefresh = loadedKeyRef.current === key;
      if (!isRefresh) {
        setLoading(true);
        setError(null);
      }
      const r = await fetchThread("self", id);
      const rows: ThreadMessageRaw[] | null = r.ok ? r.data.messages : null;
      if (!r.ok && !isRefresh) setError(`${r.status || ""} ${r.message}`.trim());
      if (rows) {
        // The page's own read names no topic on its rows: they are this
        // page's, and the back shows them as its.
        const topic = topicTag(id);
        const mapped = mapThreadMessages(rows, true).map((m) => (m.topic || !topic ? m : { ...m, topic }));
        if (isRefresh) reconcile(mapped);
        else {
          reset(mapped, key);
          if (topic) forgetEarlier(topic.id);
        }
        loadedKeyRef.current = key;
      } else if (!isRefresh) {
        reset([]);
        loadedKeyRef.current = null;
      }
      if (!isRefresh) setLoading(false);
    },
    [reset, reconcile, forgetEarlier],
  );

  // The topic each card's question was asked on (its ticket's asked_in): an
  // answer goes back there, whatever topic the person last wrote to.
  const askTopicRef = useRef<Map<number, string>>(new Map());
  // The conversation's cards (one conversation): the open tickets
  // the Host asked on ANY of its topics come up as the ask card, and an open
  // pairing started on any of them brings back its code card. They follow
  // what the person looks at, the conversation as a whole, never the topic
  // their last message went to (a question asked on another topic would
  // otherwise wait until they wrote there again).
  const refreshCards = useCallback(
    async (withPairing: boolean) => {
      const r = await listRoomTickets(CONVERSATION_ROOM);
      if (r.ok) {
        for (const t of r.data.tickets as unknown[]) {
          const tk = t as { id?: unknown; metadata?: { asked_in?: unknown } };
          const room = roomOf(tk.metadata?.asked_in);
          if (typeof tk.id === "number" && room?.startsWith("thread:")) {
            askTopicRef.current.set(tk.id, room.slice("thread:".length));
          }
        }
        raiseTickets(r.data.tickets, CONVERSATION_KEY);
      }
      if (withPairing) {
        const c = await listConnectors();
        if (c.ok) raisePairing(conversationPairing(c.data.connectors), CONVERSATION_KEY);
      }
    },
    [raiseTickets, raisePairing],
  );
  const refreshCardsRef = useRef(refreshCards);
  refreshCardsRef.current = refreshCards;
  // The topic the turn this window follows runs in (one conversation):
  // words for it go into that turn as a nudge. A reattach follows the
  // turn of the topic it was asked for.
  const runningTopicRef = useRef<string>(ORIGINAL);
  const reattachRef = useRef(reattach);
  reattachRef.current = async (id?: string | null) => {
    const attached = await reattach(id);
    if (attached && id) runningTopicRef.current = normalizeThreadId(id);
    return attached;
  };

  useEffect(() => {
    if (!threadId) return;
    setMarkers([]);
    // A pick was for the page the person was on.
    setTurnTypeId("");
    if (linkRef.current !== threadId) {
      // The topic the person's last message went to: its cards, and a turn
      // running on it. The feed on screen stays as it is.
      void refreshCardsRef.current(true);
      return;
    }
    linkRef.current = null;
    void loadThread(threadId).then(() => {
      void refreshCardsRef.current(true);
      void reattachRef.current(threadId);
      void loadMarkers(threadId);
    });
  }, [threadId, loadThread, loadMarkers]);

  const statusRef = useRef(status);
  statusRef.current = status;
  // A turn that ran on while the window slept, or ended unseen: attach to it
  // again, or read the page.
  useEffect(() => {
    if (!threadId) return;
    const onWake = () => {
      if (document.visibilityState !== "visible") return;
      void (async () => {
        const attached = await reattachRef.current(threadId);
        if (!attached && statusRef.current === "ready") void reloadFeed();
      })();
    };
    window.addEventListener("visibilitychange", onWake);
    window.addEventListener("online", onWake);
    return () => {
      window.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("online", onWake);
    };
  }, [threadId, reloadFeed]);

  /** Read the conversation again, in place (a delivery landed in a topic,
   * the Host's own work reported), and attach to a turn running on the
   * person's topic that this window did not start. */
  const refresh = useCallback(() => {
    if (statusRef.current !== "ready") return;
    if (!threadId) {
      void reloadFeed();
      return;
    }
    void reattachRef.current(threadId).then((attached) => {
      if (!attached) void reloadFeed();
    });
  }, [threadId, reloadFeed]);

  // A turn this window did not send (one reattached after a reload, or
  // started elsewhere) ends: read the page so its saved rows (the reply, a
  // nudge the turn read) take their places. A send's own reload is sendHere's.
  const sendingRef = useRef(false);
  const prevStatusRef = useRef(status);
  useEffect(() => {
    const was = prevStatusRef.current;
    prevStatusRef.current = status;
    const ran = was === "submitted" || was === "streaming";
    if (!ran || status === "submitted" || status === "streaming" || sendingRef.current) return;
    const timers = [0, 1600, 4000].map((ms) =>
      window.setTimeout(() => {
        if (statusRef.current === "ready") void reloadFeed();
      }, ms),
    );
    return () => timers.forEach((t) => window.clearTimeout(t));
  }, [status, reloadFeed]);

  const holding = (status === "submitted" || status === "streaming") && isHolding(messages);
  const holdingRef = useRef(holding);
  holdingRef.current = holding;

  const handleLanded = useCallback(
    (landing: Landing) => {
      // The topic the message went to is the person's topic now, and the
      // thread on screen: its messages of this visit with this send's own
      // words and the reply being written, at the bottom. Nothing older is
      // read in: that thread's earlier messages are an Earlier away.
      landedThreadRef.current = landing.threadId;
      runningTopicRef.current = landing.threadId;
      setThreadId(landing.threadId);
      void loadThreads();
    },
    [loadThreads, setThreadId],
  );

  // The Host's pick filed a turn's exchange in another topic (thread_moved):
  // the followed turn's words go there. The screen goes with them only when
  // it shows the thread the exchange left (the frame's `from`; for the
  // followed turn with no `from`, the topic it ran on), for the followed
  // turn and a turn beside alike. A person reading another topic, where
  // their last message went (a turn they started beside), stays there: the
  // screen never moves on its own. The blank page, with no
  // thread on it, follows the followed turn: its words are the only ones there.
  movedRef.current = (to, from, side) => {
    const left = normalizeThreadId(from ?? (side ? null : runningTopicRef.current));
    if (!side) {
      runningTopicRef.current = to.id;
      landedThreadRef.current = to.id;
    }
    const view = viewTopicRef.current;
    const onLeft = view ? left !== ORIGINAL && left === view : !side;
    if (onLeft || normalizeThreadId(to.id) === view) setThreadId(to.id);
    void loadThreads();
  };

  /** A message for another topic while a turn runs: its own turn, started
   * now beside the running one, streaming live into the conversation with
   * its own working line (useCirclesChat sendBeside). Once its stream ends
   * the conversation is read again, so its saved rows take the echoes'
   * places. */
  const sendBeside = useCallback(
    async (
      text: string,
      topic: FeedTopic,
      picked: boolean,
      opts: { model?: ModelPick | null; effort?: string | null },
    ) => {
      const err = await chat.sendBeside(SELF, text, {
        threadId: topic.id,
        topicPicked: picked,
        model: opts.model ?? null,
        effort: opts.effort || null,
        topic,
        onEnded: () => {
          for (const ms of [0, 1500, 4000]) window.setTimeout(() => void reloadFeed(), ms);
          void loadThreads();
        },
      });
      if (!err) setThreadId(topic.id);
      return err;
    },
    [chat.sendBeside, setThreadId, reloadFeed, loadThreads],
  );

  /** Words typed while a turn runs (one conversation): the Host picks their
   * topic first. The running turn's topic: into that turn. A topic whose
   * turn already streams beside: into that one. Any other topic: its own
   * turn, now, beside the running one. No answer from the pick: into the
   * running turn, as before the conversation was one. */
  const whileRunning = useCallback(
    async (
      text: string,
      opts: { model?: ModelPick | null; effort?: string | null },
      fallbackNudge: boolean,
    ): Promise<string | null | { send: FeedTopic | null }> => {
      const words = text.trim();
      const running = runningTopicRef.current;
      let topic: FeedTopic | null = rootPin ? { id: rootPin, title: "Root", root: true } : null;
      let picked = false;
      // Words that go into the running turn: the screen is on its thread, so
      // their line is seen going in (the person may be on another thread
      // since, with a turn beside it).
      const intoRunning = async () => {
        if (normalizeThreadId(running) !== ORIGINAL) setThreadId(running);
        await nudgeRunningTurn(SELF, words, running, true);
      };
      if (!topic) {
        const last = threadId && threadId !== rootIdRef.current ? threadId : undefined;
        const t = await pickTopic(words, last);
        if (!t.ok) {
          if (!fallbackNudge) return { send: null };
          await intoRunning();
          return null;
        }
        topic = { id: t.data.thread_id, title: t.data.title || "", root: t.data.root };
        picked = true;
      }
      const dest = normalizeThreadId(topic.id);
      if (fallbackNudge && dest === normalizeThreadId(running)) {
        await intoRunning();
        return null;
      }
      if (chat.besideTopics().some((b) => normalizeThreadId(b) === dest)) {
        if (await chat.nudgeBeside(topic.id, words)) {
          setThreadId(topic.id);
          return null;
        }
      }
      // No turn followed here: an ordinary send, to the topic just picked.
      if (!fallbackNudge) return { send: picked ? topic : null };
      return sendBeside(words, topic, picked, opts);
    },
    [threadId, rootPin, nudgeRunningTurn, chat.besideTopics, chat.nudgeBeside, sendBeside, setThreadId],
  );

  /**
   * Send on the page on screen. While a turn runs and holds for what it
   * started, the words go into it. On a closed page the page is opened again
   * first (plan: "writing on one reopens it"). A carried message (Ask Ivy)
   * may go with no words of its own. Returns an error to show, or null.
   */
  const sendHere = useCallback(
    async (
      text: string,
      opts: {
        attachments?: AttachmentBlock[];
        model?: ModelPick | null;
        effort?: string | null;
        carry?: CarryRef | null;
        /** What an app handed the chat for this send (cseries:compose pills):
         * already folded into the words, named on the turn's frame too. */
        pills?: string[];
        /** The page to send on, when it is not the one on screen yet (Ask
         * Ivy switches the back to it first). */
        on?: string;
      } = {},
    ): Promise<string | null> => {
      // The topic the person was last in, for the Host's pick: never the
      // root thread unless they chose to write there.
      const pinned = opts.on ? null : rootPin;
      const lastTopic = threadId && threadId !== rootIdRef.current ? threadId : undefined;
      const id = opts.on ?? pinned ?? lastTopic;
      let given: FeedTopic | null = null;
      if (holdingRef.current && text.trim() && !opts.carry) {
        const r = await whileRunning(text, opts, true);
        if (r === null || typeof r === "string") return r;
      }
      // No turn followed here, but one streams beside on another topic: words
      // for that topic go into it (a send into a running topic would stream
      // the same turn twice); any other topic is an ordinary send.
      if (
        !holdingRef.current &&
        text.trim() &&
        !opts.carry &&
        !opts.on &&
        !(opts.attachments && opts.attachments.length) &&
        chat.besideTopics().length > 0
      ) {
        const r = await whileRunning(text, opts, false);
        if (r === null || typeof r === "string") return r;
        given = r.send;
      }
      const row = id ? threads.find((t) => t.id === id) : undefined;
      if (opts.on && id && row?.closed_at && id !== ORIGINAL) {
        const r = await patchThread(id, { closed: false });
        if (!r.ok) return `Couldn't open the thread again: ${r.message}`;
        void loadThreads();
      }
      const preNewest = (() => {
        const cur = messagesRef.current;
        for (let i = cur.length - 1; i >= 0; i--) if (!isLocalEcho(cur[i].id)) return cur[i].createdAt;
        return "";
      })();
      landedThreadRef.current = null;
      sendingRef.current = true;
      try {
        // The turn-type pick goes with one send, then the page's own rule.
        const typeId = opts.carry ? null : turnTypeId || null;
        setTurnTypeId("");
        runningTopicRef.current = normalizeThreadId(given ? given.id : id);
        // A send that names its topic (the pick just made beside a running
        // turn, the root thread chosen) lands where it was sent, which is no
        // move, so the landing does not take the screen there: it goes now,
        // with the words, as a turn started beside does (sendBeside).
        // Otherwise the back stayed on the other topic and the words, tagged
        // with theirs, were on no screen.
        const named = given ? given.id : pinned;
        if (named && normalizeThreadId(named) !== ORIGINAL && named !== threadId) setThreadId(named);
        await send(SELF, text, {
          attachments: opts.attachments ?? [],
          model: opts.model ?? null,
          effort: opts.effort || null,
          threadId: given ? given.id : (id ?? null),
          turnTypeId: typeId,
          // The Host picks the topic, unless the words say where they go: a
          // carried message, a turn-type pick, the root thread chosen, a link,
          // or the pick made just now (whileRunning).
          pickTopic: !given && !opts.carry && !typeId && !pinned && !opts.on,
          topicPicked: !!given,
          pills: opts.pills && opts.pills.length ? opts.pills : undefined,
          carry: opts.carry ?? null,
          onLanded: handleLanded,
        });
        // The topic the words went to: where the platform landed them, else
        // the one they were sent on.
        const sentTo = normalizeThreadId(landedThreadRef.current ?? (given ? given.id : id));
        const newerThan = preNewest;
        // Swap the streamed echo for its saved row once it has landed: this
        // visit's part of the feed, every topic. Done when THIS topic's
        // reply is in, newer than anything on screen before the send; a
        // reply another topic's turn saved meanwhile is not this one's.
        for (let attempt = 0; attempt < 4; attempt++) {
          const fetched = await reloadFeed();
          let last: ChatMessage | undefined;
          for (let i = (fetched?.length ?? 0) - 1; i >= 0 && !last; i--) {
            const m = fetched![i];
            if (m.role === "assistant" && (!m.topic || normalizeThreadId(m.topic.id) === sentTo)) last = m;
          }
          if (last && last.createdAt > newerThan) break;
          await new Promise((res) => setTimeout(res, 400));
        }
      } finally {
        sendingRef.current = false;
      }
      void loadThreads();
      return null;
    },
    [threadId, threads, rootPin, whileRunning, chat.besideTopics, messagesRef, send, handleLanded, reloadFeed,
      loadThreads, turnTypeId, setThreadId],
  );

  /** Ask Ivy: give one message of a person's chat to page `id`. The back
   * turns to that page first and waits for it to be read, so the turn's
   * stream is the page's own and nothing on screen resets under it. Nothing
   * goes to anyone else (the server reads the message itself). */
  const giveTo = useCallback(
    async (id: string, carry: CarryRef): Promise<string | null> => {
      if (statusRef.current === "submitted" || statusRef.current === "streaming") {
        return "Your Host is working on a page now. Try again when it has answered.";
      }
      // One conversation: the carried message goes on the topic named, and
      // its turn streams into the feed on screen.
      if (id !== threadId) setThreadId(id);
      return sendHere("", { carry, on: id });
    },
    [threadId, setThreadId, sendHere],
  );

  /** Words typed while the followed turn streams (the composer's nudge):
   * the Host picks their topic, as for every message (whileRunning). */
  const nudge = useCallback(
    async (text: string, opts: { model?: ModelPick | null; effort?: string | null } = {}) => {
      if (!text.trim()) return null;
      const r = await whileRunning(text, opts, true);
      return r === null || typeof r === "string" ? r : null;
    },
    [whileRunning],
  );

  const newThread = useCallback(async (): Promise<string | null> => {
    const r = await createThread();
    if (!r.ok) return `Couldn't start a new thread: ${r.message}`;
    await loadThreads();
    setThreadId(r.data.thread.id);
    return null;
  }, [loadThreads, setThreadId]);

  /** A decision picked from Waiting on you comes up as its card even if it
   * was set aside with "Not now" here before, and even when its page is the
   * one already on screen (no switch would read the tickets again). */
  const askAgain = useCallback((ticketId: number) => {
    raiseAgain(ticketId);
    void refreshCardsRef.current(false);
  }, [raiseAgain]);

  // ── What the rail's thread list did to a page (conversation-sidebar.tsx
  // ThreadList; conversations-pane.tsx handleRenameThread, handleToggle-
  // ThreadClosed, handleDelete, handleDeleteThread, handleSplit). Each
  // returns an error to show, or null. ─────────────────────────────────────

  /** Rename a page (PATCH /api/circles/threads/{id}). The original
   * conversation has no row and keeps its name. */
  const renameThread = useCallback(
    async (id: string, title: string): Promise<string | null> => {
      const t = title.trim();
      if (!t || id === ORIGINAL) return null;
      const r = await patchThread(id, { title: t });
      if (!r.ok) return `Couldn't rename the thread: ${r.message}`;
      await loadThreads();
      return null;
    },
    [loadThreads],
  );

  /** Close or reopen a page. A closed page goes under "Closed"; writing on
   * it opens it again (sendHere). */
  const closeThread = useCallback(
    async (id: string, closed: boolean): Promise<string | null> => {
      if (id === ORIGINAL) return null;
      const r = await patchThread(id, { closed });
      if (!r.ok) return `Couldn't ${closed ? "close" : "reopen"} the thread: ${r.message}`;
      await loadThreads();
      return null;
    },
    [loadThreads],
  );

  /** Clear a page's messages (DELETE /api/circles/thread/self/{id}): they are
   * archived, not erased, and the page stays. The rail's Clear on your Host's
   * row did this to the page on screen. */
  const clearThread = useCallback(
    async (id: string): Promise<string | null> => {
      const r = await deleteConversation("self", id);
      if (!r.ok) return `Couldn't clear the thread: ${r.message}`;
      if (id === threadId) {
        reset([]);
        loadedKeyRef.current = null;
        void loadThread(id);
      }
      setMarkers([]);
      await loadThreads();
      return null;
    },
    [threadId, reset, loadThread, loadThreads],
  );

  /** The scheduled tasks set on a page that deleting it cancels, named
   * before the delete (a cancelled task does not come back). */
  const wakeupsOf = useCallback(
    async (id: string): Promise<ThreadWakeup[] | null> => {
      const r = await listThreadWakeups(id);
      return r.ok ? (r.data.wakeups ?? []) : null;
    },
    [],
  );

  /** Delete a page (DELETE /api/circles/threads/{id}): its messages and what
   * the Host learned in it leave its memory (archived), its scheduled tasks
   * are cancelled, and it leaves the tabs. Deleting the page on screen lands
   * on the blank page: there is no original conversation to fall back to,
   * and the next message picks its topic. */
  const removeThread = useCallback(
    async (id: string): Promise<string | null> => {
      if (id === ORIGINAL) return null;
      const r = await deleteThread(id);
      if (!r.ok) return `Couldn't delete the thread: ${r.message}`;
      if (id === threadId) openBlank();
      await loadThreads();
      return null;
    },
    [threadId, loadThreads, openBlank],
  );

  /** Split here: the page's rows from the marker's turn on move to a new
   * page, which opens. */
  const splitAt = useCallback(
    async (marker: Marker): Promise<string | null> => {
      if (!threadId || marker.from_turn_id == null) return null;
      const r = await splitThread(threadId, { from_turn_id: marker.from_turn_id });
      if (!r.ok) return `Couldn't split the thread: ${r.message}`;
      await loadThreads();
      setThreadId(r.data.thread.id);
      return null;
    },
    [threadId, loadThreads, setThreadId],
  );

  // ── The ask card (the rail's, ConversationsPane handleAskAnswer) ──────────
  const [askBusy, setAskBusy] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const shownAskId = asks[0]?.ticketId ?? null;
  useEffect(() => setAskError(null), [shownAskId]);

  /** The topic a card's question was asked on: its ticket's, else the one
   * the followed turn runs on, else the person's. */
  const topicOfAsk = useCallback(
    (ticketId: number): string =>
      askTopicRef.current.get(ticketId) ??
      ((statusRef.current === "submitted" || statusRef.current === "streaming")
        ? runningTopicRef.current
        : (threadId ?? ORIGINAL)),
    [threadId],
  );

  /** Say `text` to the Host on `topic`: into its running turn when the turn
   * this window follows is that topic's, as its own turn beside it when
   * another topic's turn runs, else as a message there. The screen goes to
   * `topic` with the words, so the answer and its reply are seen. */
  const tellHost = useCallback(
    async (text: string, topic: string, fallback = true): Promise<void> => {
      const st = statusRef.current;
      const follow = () => {
        if (normalizeThreadId(topic) !== ORIGINAL) setThreadId(topic);
      };
      if (st === "submitted" || st === "streaming") {
        if (normalizeThreadId(topic) === normalizeThreadId(runningTopicRef.current)) {
          if (fallback) follow();
          await nudgeRunningTurn(SELF, text, topic, fallback);
          return;
        }
        if (!fallback) return;
        follow();
        await chat.sendBeside(SELF, text, {
          threadId: topic,
          topicPicked: true,
          topic: { id: topic, title: "" },
          onEnded: () => void reloadFeed(),
        });
        return;
      }
      if (fallback) {
        follow();
        runningTopicRef.current = normalizeThreadId(topic);
      }
      if (fallback) await send(SELF, text, { threadId: topic, onLanded: handleLanded });
    },
    [send, nudgeRunningTurn, chat.sendBeside, reloadFeed, setThreadId, handleLanded],
  );

  const answerAsk = useCallback(
    async (option: AskOption, note: string, stepupCode?: string) => {
      const ask = asks[0];
      if (!ask) return;
      const topic = topicOfAsk(ask.ticketId);
      setAskBusy(true);
      setAskError(null);
      const r = await decideTicket(ask.ticketId, option.id, false, note, null, stepupCode ?? null);
      if (!r.ok) {
        setAskError(`Couldn’t record that — ${r.message}`.trim());
        setAskBusy(false);
        return;
      }
      settleAsk(ask.ticketId);
      if (r.data.answer_turn_started) {
        // The turn that asked had ended: the kernel started the answer's turn
        // on this page itself. Follow it live, on screen; never send the
        // answer again.
        if (normalizeThreadId(topic) !== ORIGINAL) setThreadId(topic);
        void reattachRef.current(topic);
      } else if (r.data.answered_into_turn) {
        // The decide put the answer into the turn that asked; from there it
        // is the server's to deliver. Nudging it
        // again would put it in front of the Host twice.
      } else {
        await tellHost(answerText(ask, option, note), topic);
      }
      setAskBusy(false);
      void loadThreads();
    },
    [asks, topicOfAsk, settleAsk, tellHost, loadThreads, setThreadId],
  );

  const notNowAsk = useCallback(
    async (note: string) => {
      const ask = asks[0];
      if (!ask) return;
      settleAsk(ask.ticketId, true);
      // Words only with a note: a bare Not now is said only to a running
      // turn of the topic that asked.
      await tellHost(notNowText(ask, note), topicOfAsk(ask.ticketId), note.trim().length > 0);
    },
    [asks, topicOfAsk, settleAsk, tellHost],
  );

  const sealCredential = useCallback(async (req: AskCredentialRequest, value: string) => {
    const r = await storeVaultSecret(req.name, req.cred_type === "api_key" ? "api_key" : "opaque", value, true);
    return r.ok ? null : sealErrorText(r.status, r.message, req.name);
  }, []);

  return {
    chat,
    // Every message this window holds, every topic (the turn machinery's).
    messages,
    // What the back shows: the thread on screen's (viewTopic).
    shown,
    viewTopic,
    status,
    holding,
    booted,
    // "Where this stands": the topic the person last wrote to.
    standsTopicId: threadId ?? lastWritten,
    // The thread on screen has earlier messages, read by loadEarlier.
    hasEarlier,
    loadEarlier,
    rootPin,
    setRootPin,
    threads,
    threadsError,
    threadId,
    setThreadId,
    openOn,
    openBlank,
    loading,
    error,
    loadThreads,
    refresh,
    refreshCards,
    sendHere,
    giveTo,
    nudge,
    stop,
    newThread,
    markers,
    turnTypes,
    turnTypeId,
    setTurnTypeId,
    renameThread,
    closeThread,
    clearThread,
    wakeupsOf,
    removeThread,
    splitAt,
    asks,
    askAgain,
    askBusy,
    askError,
    answerAsk,
    notNowAsk,
    sealCredential,
    pairing,
    settlePairing,
  };
}

export type HostThread = ReturnType<typeof useHostThread>;
