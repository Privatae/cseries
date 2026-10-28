import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { AskOverlay } from "@kit/ui/ask-overlay";
import { PairingCodeOverlay } from "@kit/ui/pairing-code-overlay";
import { climbLevels, pageEffort } from "@kit/book/hold-to-think";
import { NudgeLine } from "@kit/book/nudge-line";
import type { TabLight } from "@kit/book/rules";
import { ErrorBoundary } from "@/components/error-boundary";
import { ChatMessageView } from "@/components/chat/chat-message";
import { ConversationDepth } from "@/components/chat/conversation-depth";
import { EffortPicker } from "@/components/chat/effort-picker";
import { DEFAULT_VALUE, decodePick, ModelPicker } from "@/components/chat/model-picker";
import { useEffortPick } from "@/hooks/use-effort-pick";
import { fetchFavoriteIds, fetchModelCatalog, fetchPairingCode } from "@/lib/api";
import { fetchHostPage, markSeen, openApp, type HostPage } from "@/lib/cards-api";
import {
  carriedParts,
  clockOf,
  dayWord,
  ORIGINAL,
  threadTitle,
  turnTypeDefault,
} from "@/lib/cards";
import { isLocalEcho } from "@/lib/history-merge";
import type { ActiveTurn, CatalogModel, ChatMessage } from "@/lib/types";
import { ActivityLine, BesideWorking } from "./activity";
import { Corner, Face } from "./bits";
import { HostComposer } from "./composer";
import { StandsBox } from "./stands";
import type { HostThread } from "./use-host-thread";

/** "Today 18:02", "Yesterday 18:02", "Monday 19:10". */
function whenOf(iso: string): string {
  const d = dayWord(iso);
  const c = clockOf(iso);
  return d && c ? `${d} ${c}` : c;
}

/** One message on your Host's back (mockup:380-384): who and when, then the
 * words. Your Host's are the shared message view (tool calls, the reply's
 * cost line, visuals), with no Regenerate and no A/B; yours are your words,
 * with a carried message's quote above them; a nudge is the book's line. */
const HostMessage = memo(function HostMessage({ m, hostName }: { m: ChatMessage; hostName: string }) {
  const unnamed = useMemo(() => ({ ...m, author: undefined, via: undefined }), [m]);
  if (m.role === "user" && m.nudge) {
    // Read: the turn's receipt marked its line, or the turn saved it as a row.
    const read = !!m.nudgeRead || !isLocalEcho(m.id);
    return <NudgeLine clock={clockOf(m.createdAt)} read={read} text={m.text} />;
  }
  if (m.role === "user") {
    const echo = Array.isArray(m.carry) ? m.carry[0] : m.carry;
    const parts = carriedParts(m.text);
    const quote = parts?.quote ?? echo?.quote ?? null;
    const from = parts?.who ?? echo?.who ?? null;
    const words = parts ? parts.words : m.text;
    return (
      <div className="hm you" data-msg="you" data-msg-topic={m.topic?.id}>
        <div className="lb">
          <span className="w">You</span>
          <span className="t">{whenOf(m.createdAt)}</span>
        </div>
        {(quote != null || m.carry) && (
          <div className="quote" data-carried>
            <small>{from ? `from ${from.replace(/\s*\([^)]*@[^)]*\)\s*$/, "")}'s card` : "from a person's card"}</small>
            {quote ?? ""}
          </div>
        )}
        {words ? <div className="tx">{words}</div> : null}
      </div>
    );
  }
  return (
    <div className="hm" data-msg="host" data-msg-topic={m.topic?.id}>
      <div className="lb">
        <span className="w">{hostName}</span>
        <span className="t">{whenOf(m.createdAt)}</span>
      </div>
      <div className="body">
        <ErrorBoundary label="message">
          {/* The label above names who wrote it; the shared view's own
              author line would say it twice. */}
          <ChatMessageView message={unnamed} />
        </ErrorBoundary>
        {/* A reply on another topic, streaming beside the turn the line above
            the box follows: its own working line, so both are seen alive. */}
        {m.beside && m.streaming && <BesideWorking hostName={hostName} message={m} />}
      </div>
    </div>
  );
});

/** The blank page every login opens on (one conversation, slice 5): a
 * greeting, and the earlier messages of the thread the person last wrote in
 * (the one "Where this stands" names) one click away. */
function HostGreeting({ hostName, hasEarlier, onEarlier }: { hostName: string; hasEarlier: boolean; onEarlier: () => void }) {
  const h = new Date().getHours();
  const hello = h < 5 ? "Hello" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  return (
    <div className="blank greeting" data-greeting>
      <p>
        {hello}. {hostName} is here. Write what you need below; {hostName} finds where it belongs.
      </p>
      {hasEarlier && (
        <button className="link" data-earlier onClick={onEarlier} type="button">
          Earlier messages ↑
        </button>
      )}
    </div>
  );
}

/** The message the reader is reading in scroller `b`: the first one whose
 * bottom is below the scroller's top, and how far below the top it starts. */
function readingAt(b: HTMLElement): { el: HTMLElement; off: number } | null {
  const top = b.getBoundingClientRect().top;
  for (const el of b.querySelectorAll<HTMLElement>("[data-msg], [data-nudge-line], [data-topic-line]")) {
    const r = el.getBoundingClientRect();
    if (r.bottom > top + 1) return { el, off: r.top - top };
  }
  return null;
}

/** Where the conversation turns to another topic: one line. The Host's own
 * switch line ("continuing: …, Oct 2" / "new topic: …") is drawn as is; a
 * turn with no such line (the Host's own work, an older stretch) gets the
 * topic's title. The root thread's line offers "Write in Root", the one way
 * words reach it from here. */
function TopicLine({ text, root, onRoot }: { text: string; root?: boolean; onRoot?: () => void }) {
  return (
    <div className="marker topic" data-topic-line role="separator">
      <span>{text}</span>
      {root && onRoot && (
        <button className="link" data-write-root onClick={onRoot} type="button">
          Write in Root
        </button>
      )}
    </div>
  );
}

export function OwnBack({
  ht,
  hostName,
  hue,
  visible,
  turns,
  onTurnCorner,
  onToast,
  onDecisionDetails,
  askHidden,
}: {
  /** The decision on the ask card, in full (its context, Dismiss, "and
   * similar going forward"): the Decisions sheet. */
  onDecisionDetails: (ticketId: number) => void;
  /** A sheet is over the cards: the ask card waits behind it. */
  askHidden: boolean;
  ht: HostThread;
  hostName: string;
  hue: number;
  /** The back is the side in front, on the card held. */
  visible: boolean;
  lights: ReadonlyMap<string, TabLight>;
  working: ReadonlySet<string>;
  /** The platform's running turns, every page (cards-app's turns poll). */
  turns: ReadonlyArray<ActiveTurn>;
  onTurnCorner: () => void;
  onToast: (s: string) => void;
}) {
  const threadId = ht.threadId;
  const row = ht.threads.find((t) => t.id === threadId);
  const busy = ht.status === "submitted" || ht.status === "streaming";

  // The model and effort pickers (mismatch 7's default: on the back's
  // header, as the rail has them in the chat header).
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [favoriteIds, setFavoriteIds] = useState<string[]>([]);
  const [pickValue, setPickValue] = useState(DEFAULT_VALUE);
  useEffect(() => setPickValue(DEFAULT_VALUE), [threadId]);
  useEffect(() => {
    if (!visible || models.length) return;
    let off = false;
    void Promise.all([fetchModelCatalog(), fetchFavoriteIds()]).then(([cat, favs]) => {
      if (off) return;
      if (cat.ok) setModels(cat.data);
      setFavoriteIds(favs);
    });
    return () => {
      off = true;
    };
  }, [visible, models.length]);
  const currentModel = useMemo(() => decodePick(pickValue), [pickValue]);
  const depthTarget = useMemo(
    () => ({ kind: "self", id: threadId ?? ORIGINAL, label: hostName }),
    [threadId, hostName],
  );
  const effortPick = useEffortPick(currentModel, `self:${threadId ?? ""}`, true);
  const level = pageEffort(effortPick.effort, effortPick.options);
  const climb = climbLevels(effortPick.options, level);

  // Where this stands and the running count (the page route), read when
  // the page opens, when its turn ends, and when something lands on it.
  const [page, setPage] = useState<HostPage | null | undefined>(undefined);
  const [pageErr, setPageErr] = useState<string | null>(null);
  const [seenBefore, setSeenBefore] = useState<string | null | undefined>(undefined);
  const readPage = useCallback(async (id: string) => {
    const r = await fetchHostPage(id);
    if (r.ok) {
      setPage(r.data);
      setPageErr(null);
    } else {
      setPage(null);
      setPageErr(r.message);
    }
  }, []);
  const threadsRef = useRef(ht.threads);
  threadsRef.current = ht.threads;
  // "Where this stands" is a topic's: the one the person last wrote to
  // (ht.standsTopicId), named on its one line. A blank page after login shows
  // the topic of their newest message before it.
  const standsId = ht.standsTopicId ?? null;
  const standsRow = ht.threads.find((t) => t.id === standsId);
  useEffect(() => {
    if (!standsId) return;
    setPage(undefined);
    // The mark as it stood when this opening began.
    setSeenBefore(threadsRef.current.find((t) => t.id === standsId)?.seen_at ?? null);
    void readPage(standsId);
  }, [standsId, readPage]);
  const landed = standsRow?.last_landed_at ?? null;
  const landedRef = useRef(landed);
  useEffect(() => {
    if (landedRef.current === landed) return;
    landedRef.current = landed;
    if (standsId) void readPage(standsId);
  }, [landed, standsId, readPage]);

  // The seen mark: when the page is in front, and when its own turn ends in
  // front of the person, naming that turn, so its reply's row landing later
  // never lights the tab (circles_marks.mark_seen).
  const loadThreadsRef = useRef(ht.loadThreads);
  loadThreadsRef.current = ht.loadThreads;
  useEffect(() => {
    if (!visible || !threadId) return;
    void markSeen("self", threadId).then(() => void loadThreadsRef.current());
  }, [visible, threadId]);
  const messagesRef = useRef(ht.messages);
  messagesRef.current = ht.messages;
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const prevBusy = useRef(busy);
  useEffect(() => {
    const was = prevBusy.current;
    prevBusy.current = busy;
    if (!was || busy || !threadId) return;
    const last = [...messagesRef.current].reverse().find((m) => m.role === "assistant" && !m.beside);
    const token = last?.turnToken ?? null;
    if (visibleRef.current) {
      void markSeen("self", threadId, { turnToken: token }).then(() => void loadThreadsRef.current());
    } else void loadThreadsRef.current();
    void readPage(threadId);
    const t = [1200, 3500].map((ms) => window.setTimeout(() => void readPage(threadId), ms));
    return () => t.forEach((x) => window.clearTimeout(x));
  }, [busy, threadId, readPage]);

  const [sendErr, setSendErr] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  // The thread on screen (ht.viewTopic): only its messages are drawn.
  const shown = ht.shown;
  // Follow the newest words only while the reader is at the bottom (the
  // rail's chat view, ai-elements/conversation.tsx): scrolled up to read,
  // a streaming reply does not pull the page away, and a button goes back.
  // Nothing else moves the page: earlier messages come only from Earlier
  // messages, and when they do the reader stays on what they were reading.
  // (A scroll that ended near the top once read the next 50 rows of every
  // topic on its own, and the reply growing past the box was
  // such a scroll; its place was put back a frame before the rows were
  // drawn, so the page sat at the top of them.)
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  const toBottom = useCallback(() => {
    const b = bodyRef.current;
    if (b) b.scrollTop = b.scrollHeight;
    atBottomRef.current = true;
    setAtBottom(true);
  }, []);
  // Before the frame is drawn, so no frame shows the page anywhere else.
  useLayoutEffect(() => {
    // Another thread on screen (the words went there), or a page read again
    // from the start: from its end, the newest message.
    atBottomRef.current = true;
    toBottom();
  }, [ht.viewTopic, ht.loading, toBottom]);
  // Any change to what is drawn (new words, a tool card, a row swapped for
  // its echo), in the same commit.
  useLayoutEffect(() => {
    if (atBottomRef.current) toBottom();
  }, [shown, toBottom]);
  // What grows without new words (a tool card opening, a picture or a code
  // block drawn late) keeps the newest message in sight too.
  useEffect(() => {
    const inner = innerRef.current;
    if (!inner || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (atBottomRef.current) {
        const b = bodyRef.current;
        if (b) b.scrollTop = b.scrollHeight;
      }
    });
    ro.observe(inner);
    return () => ro.disconnect();
  }, []);
  /** Earlier messages: the thread's page before what is on screen goes above
   * it in one commit, and the message the reader was reading stays where it
   * was on the screen. With nothing on screen (the greeting), the page opens
   * on its newest message. */
  const loadEarlier = useCallback(async () => {
    let kept = false;
    const added = await ht.loadEarlier((apply) => {
      const b = bodyRef.current;
      const at = b ? readingAt(b) : null;
      // Reading back, not following the newest words: the commit below must
      // not take the page to its end (a short thread fits the box, so it
      // reads as "at the bottom" until the page before goes in above it).
      if (at) atBottomRef.current = false;
      flushSync(apply);
      if (b && at && at.el.isConnected) {
        b.scrollTop += at.el.getBoundingClientRect().top - b.getBoundingClientRect().top - at.off;
        kept = true;
        // Read now, not at the scroll event: the page grew above the reader,
        // and nothing may take that for "at the bottom" in between.
        atBottomRef.current = b.scrollHeight - b.scrollTop - b.clientHeight < 48;
        setAtBottom(atBottomRef.current);
      }
    });
    if (added && !kept) toBottom();
  }, [ht.loadEarlier, toBottom]);
  const onBodyScroll = () => {
    const b = bodyRef.current;
    if (!b) return;
    const at = b.scrollHeight - b.scrollTop - b.clientHeight < 48;
    atBottomRef.current = at;
    setAtBottom(at);
  };

  const pick = (id: string) => {
    if (id === threadId) return;
    ht.setThreadId(id);
  };
  void pick;
  const titleOf = useCallback(
    (id: string) => {
      const t = ht.threads.find((x) => x.id === id);
      return t ? threadTitle(t) : threadTitle({ id, title: null });
    },
    [ht.threads],
  );
  // This window's reply on the page in front, while it streams.
  // A reply streaming beside, on another topic, is not this window's
  // followed turn: it has its own line under it (BesideWorking).
  const lastReply = useMemo(
    () => [...ht.messages].reverse().find((m) => m.role === "assistant" && !m.beside),
    [ht.messages],
  );
  const streamingReply = busy && lastReply?.streaming ? lastReply : null;
  const besideReplies = useMemo(() => ht.messages.filter((m) => m.beside && m.streaming), [ht.messages]);

  return (
    <>
      <Corner face={<Face hue={hue} name="You" size={16} />} label="You" onTurn={onTurnCorner} turnTo="front" />
      <div className="chead">
        <span className="kind">■ The back · your Host</span>
        <Face big host hue={hue} name={hostName} size={52} />
        <div className="who">
          <h2>{hostName}</h2>
          <div className="sub">
            your Host, an AI ·{" "}
            <button className="link" data-open-settings onClick={() => openApp("settings", "shaping")} type="button">
              how {hostName} thinks is in Settings ›
            </button>
          </div>
          <div className="pickers">
            <ModelPicker
              favoriteIds={favoriteIds}
              models={models}
              onChange={setPickValue}
              size="xs"
              value={pickValue}
            />
            <EffortPicker onChange={effortPick.setEffort} options={effortPick.options} value={effortPick.effort} />
            {/* The rail's chat header kept this beside the pickers: how deep
                the page's next turn reads, its budget and its context
                (chat/conversation-depth.tsx). */}
            {threadId && threadId !== ORIGINAL && (
              <ErrorBoundary label="depth">
                <ConversationDepth
                  currentModel={currentModel}
                  frame={ht.chat.contextFrame}
                  messages={shown}
                  models={models}
                  target={depthTarget}
                />
              </ErrorBoundary>
            )}
            {ht.turnTypes.length > 0 && (
              <select
                aria-label="Turn type for your next message"
                className="turntype"
                data-turn-type
                onChange={(e) => ht.setTurnTypeId(e.target.value)}
                title="Turn type for your next message"
                value={ht.turnTypeId}
              >
                <option value="">{turnTypeDefault(row)}</option>
                {ht.turnTypes.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
      </div>
      {/* One conversation (slice 5): no thread tabs and no "new thread".
          The Host picks the topic of every message, and the back shows that
          thread, named in "Where this stands" below. */}
      {ht.rootPin && (
        <div className="err" data-root-pin style={{ padding: "6px 14px" }}>
          Writing in Root: only your own words go there.{" "}
          <button className="link" data-root-unpin onClick={() => ht.setRootPin(null)} type="button">
            Back to the conversation
          </button>
        </div>
      )}
      <StandsBox
        error={pageErr}
        page={page}
        pageId={standsId}
        seenBefore={seenBefore}
        topic={standsId ? titleOf(standsId) : null}
      />
      <div className="ask-wrap">
        {ht.pairing ? (
          <PairingCodeOverlay
            fetchCode={fetchPairingCode}
            onClose={(reason) => {
              if (ht.pairing) ht.settlePairing(ht.pairing.grantId, reason === "dismissed");
              if (reason === "done") void ht.refreshCards(false);
            }}
            pairing={ht.pairing}
          />
        ) : (
          <AskOverlay
            // Hidden, not set aside, while the decision is open in full.
            ask={askHidden ? null : (ht.asks[0] ?? null)}
            busy={ht.askBusy}
            error={ht.askError}
            onAnswer={ht.answerAsk}
            onDetails={onDecisionDetails}
            onNotNow={ht.notNowAsk}
            onSealCredential={ht.sealCredential}
            waiting={Math.max(0, ht.asks.length - 1)}
          />
        )}
      </div>
      <div
        className="cbody"
        data-book-nudges
        data-host-thread={threadId}
        data-view-topic={ht.viewTopic ?? undefined}
        onScroll={onBodyScroll}
        ref={bodyRef}
      >
        <div data-cbody-in ref={innerRef}>
        <ErrorBoundary
          fallback={(reset) => (
            <div className="blank" role="alert">
              Couldn’t draw this thread.{" "}
              <button className="link" onClick={reset} type="button">
                Try again
              </button>
            </div>
          )}
          key={ht.viewTopic ?? ""}
          label="host-thread"
        >
        {ht.error ? (
          <div className="blank" role="alert">
            Couldn’t read this thread: {ht.error}{" "}
            <button className="link" onClick={() => ht.refresh()} type="button">
              Try again
            </button>
          </div>
        ) : ht.loading ? (
          <div className="blank">Reading…</div>
        ) : shown.length === 0 ? (
          <HostGreeting hasEarlier={ht.hasEarlier} hostName={hostName} onEarlier={() => void loadEarlier()} />
        ) : (
          <>
            {ht.hasEarlier && (
              // Never the scroll anchor: the page read goes in below it.
              <div className="blank earlier-bar" style={{ padding: "6px 0" }}>
                <button className="link" data-earlier onClick={() => void loadEarlier()} type="button">
                  Earlier messages ↑
                </button>
              </div>
            )}
            {shown.map((m, i) => {
              if (m.topicMarker) return <TopicLine key={m.id} text={m.text} />;
              const prev = i > 0 ? shown[i - 1] : undefined;
              const switched = !!m.topic && !!prev?.topic && prev.topic.id !== m.topic.id;
              return (
                <Fragment key={m.id}>
                  {switched && m.topic && (
                    <TopicLine
                      onRoot={() => ht.setRootPin(m.topic?.id ?? null)}
                      root={m.topic.root}
                      text={m.topic.root ? "Root" : m.topic.title}
                    />
                  )}
                  <HostMessage hostName={hostName} m={m} />
                </Fragment>
              );
            })}
          </>
        )}
        </ErrorBoundary>
        </div>
      </div>
      {!atBottom && (
        <button
          aria-label="Go to the newest message"
          className="tobottom"
          data-to-bottom
          onClick={toBottom}
          title="Go to the newest message"
          type="button"
        >
          ↓
        </button>
      )}
      {/* The one working line, just above the box, outside the scrolled
          conversation so it is always in sight: your Host is working, what it
          is doing now, for how long; behind it, everything running and any
          background work that failed. Nothing at all when idle and well. */}
      <ErrorBoundary label="activity">
        <ActivityLine
          beside={besideReplies}
          busy={busy}
          hostName={hostName}
          lastReplyId={lastReply?.id ?? null}
          message={streamingReply}
          onOpenPage={pick}
          pageId={threadId ?? null}
          titleOf={titleOf}
          turnId={busy ? ht.chat.turnIdRef.current : null}
          turns={turns}
        />
      </ErrorBoundary>
      <HostComposer
        busy={busy}
        climb={climb}
        error={sendErr}
        level={level}
        levels={effortPick.options?.efforts ?? []}
        nudging={busy}
        onNudge={(text) => {
          setSendErr(null);
          atBottomRef.current = true;
          // The Host picks the words' topic: the running turn's, a topic
          // already running beside it, or a new turn beside it.
          void ht
            .nudge(text, { model: currentModel, effort: effortPick.effort || null })
            .then((e) => e && setSendErr(e));
        }}
        onSend={(text, files, reached, pills) => {
          setSendErr(null);
          // Your own words go to the bottom, with the reply under them, even
          // if you had scrolled up to read.
          atBottomRef.current = true;
          void ht
            .sendHere(text, {
              attachments: files,
              model: currentModel,
              effort: reached || effortPick.effort || null,
              pills,
            })
            .then((e) => e && setSendErr(e));
        }}
        onStop={() => {
          ht.stop();
          onToast("Stopped.");
        }}
        placeholder={ht.rootPin ? `Write to ${hostName} · Root` : `Write to ${hostName}`}
      />
    </>
  );
}
