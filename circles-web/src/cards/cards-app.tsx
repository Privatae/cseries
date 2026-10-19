import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { newlyLit, stoppedWorking, withBaselines, workingPages, type TabLight } from "@kit/book/rules";
import { CrisisNotice } from "@kit/ui/crisis-notice";
import { TooltipProvider } from "@/components/ui/tooltip";
import { fetchActiveTurn, supportBadgeCounts } from "@/lib/api";
import { onAppView, viewFromHash } from "@/lib/app-view";
import { onComposeWanted } from "@/lib/compose-inbox";
import { fetchComingUp, fetchDeck, fetchDoor, fetchWaiting, openApp, type WaitingPayload } from "@/lib/cards-api";
import {
  buildDeck,
  cardLight,
  cardLights,
  cardOf,
  cardsTarget,
  deckOrder,
  findCards,
  hostNameOf,
  hostNamesOf,
  keepPlaces,
  pageOfTurnKey,
  selfEdge,
  threadLights,
  waitingGo,
  type Card,
  type CardsTarget,
  type DeckPayload,
  type DoorItem,
  type Side,
  type TopicRow,
} from "@/lib/cards";
import type { ActiveTurn, CalendarEvent } from "@/lib/types";
import { hueOf, Pop } from "./bits";
import { Connect, DeckCard, DoorCard, PhoneToday, Today, waitKey, type WaitItem } from "./deck";
import { ContactsSheet } from "./contacts";
import { ASK_KINDS, NOTICE_KIND, NoteSheet, useInboxNotes } from "./inbox-notes";
import { DecisionsSheet, SupportSheet } from "./tickets";
import { BlankBack, GroupBack, GroupFront, NoFront, PeerBack, RoomBack, YouFront } from "./faces";
import { OwnBack } from "./own-back";
import { PersonFront } from "./person-front";
import { SupportFront } from "./support-front";
import { Guard } from "./guards";
import { useHostThread } from "./use-host-thread";

/** Narrower than this, the phone layout (mockup:585: a screen under 700 px). */
const PHONE_QUERY = "(max-width: 699px)";
const TURNS_POLL_MS = 4_000;
// The conversations list reads every room's unread through the bridge
// (server/circles_marks.py circle_unread); the /ws push already brings a
// message the moment it lands, so the timer is only the fallback.
const DECK_POLL_MS = 30_000;
const WAITING_POLL_MS = 15_000;
const COMING_POLL_MS = 60_000;
const LIFT_MS = 1_400;
const TOAST_MS = 3_200;

function useMedia(query: string): boolean {
  const get = () => {
    try {
      return window.matchMedia(query).matches;
    } catch {
      return false;
    }
  };
  const [on, setOn] = useState(get);
  useEffect(() => {
    let m: MediaQueryList;
    try {
      m = window.matchMedia(query);
    } catch {
      return;
    }
    const f = () => setOn(m.matches);
    m.addEventListener("change", f);
    f();
    return () => m.removeEventListener("change", f);
  }, [query]);
  return on;
}

/** Every few seconds while the window shows; read at once when it shows
 * again. */
function usePoll(fn: () => void, ms: number) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") ref.current();
    };
    const id = window.setInterval(tick, ms);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [ms]);
}

/**
 * Circles, as cards. The phone is the reference layout: the deck is the first screen and
 * a card the next.
 * The desktop spreads the deck, the card held, and a column of what waits on
 * you and what's coming up.
 */
export function CardsApp() {
  const phone = useMedia(PHONE_QUERY);
  const reduced = useMedia("(prefers-reduced-motion: reduce)");
  const ht = useHostThread();

  const [deck, setDeck] = useState<DeckPayload | null>(null);
  const [deckErr, setDeckErr] = useState<string | null>(null);
  const [order, setOrder] = useState<string[]>([]);
  const [held, setHeld] = useState<string | null>(null);
  const [side, setSide] = useState<Side>("back");
  const [screen, setScreen] = useState<"deck" | "card">("deck");
  const [waiting, setWaiting] = useState<WaitingPayload | null>(null);
  const [door, setDoor] = useState<DoorItem[] | null>(null);
  const [coming, setComing] = useState<CalendarEvent[] | null>(null);
  const [turns, setTurns] = useState<ActiveTurn[]>([]);
  const [baselines, setBaselines] = useState<Map<string, string | null>>(new Map());
  const [lifted, setLifted] = useState<Set<string>>(new Set());
  const [bumps, setBumps] = useState<Record<string, number>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [contactsOpen, setContactsOpen] = useState(false);
  // What a rail pane showed in full, over the cards: a decision (or all of
  // them), a support ticket (or a new one), a message from the inbox.
  const [decisionsSheet, setDecisionsSheet] = useState<{ focus: number | null } | null>(null);
  const [supportSheet, setSupportSheet] = useState<{ id: number | null } | null>(null);
  const [noteSheet, setNoteSheet] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [supportFocus, setSupportFocus] = useState<number | null>(null);
  const [topics, setTopics] = useState<Map<number, TopicRow[]>>(new Map());
  // A topic of another Host's to open on its back (a search hit), until that
  // back has opened it.
  const [peerTopic, setPeerTopic] = useState<{ host: number; id: string } | null>(null);
  const topicOpened = useCallback(() => setPeerTopic(null), []);

  const hostName = hostNameOf(deck);
  const say = useCallback((s: string) => setToast(s), []);
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), TOAST_MS);
    return () => window.clearTimeout(t);
  }, [toast]);
  const bump = useCallback((key: string) => setBumps((b) => ({ ...b, [key]: (b[key] ?? 0) + 1 })), []);

  // ── reads ──
  const readDeck = useCallback(async () => {
    const r = await fetchDeck();
    if (r.ok) {
      setDeck(r.data);
      setDeckErr(null);
    } else setDeckErr(`${r.status || ""} ${r.message}`.trim());
  }, []);
  // A read that fails says so where its list would be (the rail's Inbox and
  // Tickets showed theirs with a Retry); the next poll tries again anyway.
  const [waitErr, setWaitErr] = useState<string | null>(null);
  // The platform's team asked something on a support ticket (the rail's
  // Tickets mark): Support's light, and an item in Waiting on you.
  const [supportAsks, setSupportAsks] = useState(0);
  const inbox = useInboxNotes();
  const readInboxNotes = inbox.read;
  const readWaiting = useCallback(async () => {
    const [w, d, b] = await Promise.all([fetchWaiting(), fetchDoor(), supportBadgeCounts(), readInboxNotes()]);
    if (w.ok) setWaiting(w.data);
    if (d.ok) setDoor(d.data.items);
    if (b.ok) setSupportAsks(b.data.needs_info ?? 0);
    const bad = [!w.ok ? `what waits (${w.message})` : "", !d.ok ? `the door (${d.message})` : ""].filter(Boolean);
    setWaitErr(bad.length ? `Couldn't read ${bad.join(" or ")}.` : null);
  }, [readInboxNotes]);
  const readComing = useCallback(async () => {
    const r = await fetchComingUp(4);
    setComing(r.ok ? r.data.events : []);
  }, []);
  const readTurns = useCallback(async () => {
    const r = await fetchActiveTurn();
    if (r.ok) setTurns(r.data.turns ?? []);
  }, []);
  useEffect(() => {
    void readDeck();
    void readWaiting();
    void readComing();
    void readTurns();
  }, [readDeck, readWaiting, readComing, readTurns]);
  usePoll(() => void readDeck(), DECK_POLL_MS);
  usePoll(() => {
    void readWaiting();
    void ht.loadThreads();
    // The open page's ask card: a ticket filed there outside a turn (a
    // connector's approval) comes up without leaving the page, as the rail's
    // 10 s re-read of the open thread's tickets did.
    void htRef.current.refreshCards(false);
  }, WAITING_POLL_MS);
  usePoll(() => void readComing(), COMING_POLL_MS);
  usePoll(() => void readTurns(), TURNS_POLL_MS);
  // This window's turn ended, or one began: read the running turns now, so
  // the working line and the tabs never wait out a poll.
  useEffect(() => {
    void readTurns();
  }, [ht.status, readTurns]);

  // ── your Host's pages: lights and the turns running on them ──
  useEffect(() => setBaselines((prev) => withBaselines(prev, ht.threads)), [ht.threads]);
  const working = useMemo(() => workingPages(turns, pageOfTurnKey), [turns]);
  const prevWorking = useRef<Set<string>>(new Set());
  const htRef = useRef(ht);
  htRef.current = ht;
  useEffect(() => {
    const stopped = stoppedWorking(prevWorking.current, working);
    prevWorking.current = working;
    if (!stopped.length) return;
    // A turn that stops is something landing: read the pages now.
    void htRef.current.loadThreads();
    if (htRef.current.threadId && stopped.includes(htRef.current.threadId)) htRef.current.refresh();
  }, [working]);
  const backInFront = held === "you" && side === "back" && (!phone || screen === "card");
  const lights = useMemo(
    () => threadLights(ht.threads, backInFront ? (ht.threadId ?? null) : null, baselines),
    [ht.threads, ht.threadId, backInFront, baselines],
  );
  const selfLit = selfEdge(lights);
  const self = useMemo(() => selfLit, [selfLit.amber, selfLit.green]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── the deck ──
  const drafts = waiting?.support_drafts.length ?? 0;
  // Support's light: a draft waiting for Send, or a question from the
  // platform's team waiting for an answer.
  const supportLit = drafts + supportAsks;
  // Every peer Host's name the list has given: a Host you block leaves the
  // list's Hosts, and its person's back keeps its name from here.
  const hostNamesRef = useRef<Map<number, string>>(new Map());
  const cards = useMemo(() => {
    for (const [id, n] of hostNamesOf(deck)) hostNamesRef.current.set(id, n);
    return buildDeck(deck, { supportDrafts: drafts, hostNames: hostNamesRef.current });
  }, [deck, drafts]);
  useEffect(() => {
    if (deck && order.length === 0) setOrder(deckOrder(cards).map((c) => c.key));
  }, [deck, cards, order.length]);
  const shown = useMemo(() => keepPlaces(order, cards), [order, cards]);
  const byKey = useMemo(() => new Map(cards.map((c) => [c.key, c])), [cards]);
  const redraw = useCallback(() => setOrder(deckOrder(cards).map((c) => c.key)), [cards]);

  const cardLightMap = useMemo(() => {
    const m = new Map<string, TabLight>();
    for (const c of cards) m.set(c.key, cardLight(cardLights(c, self, supportLit)));
    return m;
  }, [cards, self, supportLit]);
  // A card that gets something new lifts where it is (mockup:862-871).
  const prevLights = useRef<Map<string, TabLight> | null>(null);
  // Compared only once the deck, what waits and your Host's pages have all
  // been read: a light that comes on because its first read arrived late is
  // not something new.
  const allRead = !!deck && !!waiting && ht.threads.length > 0;
  useEffect(() => {
    if (!allRead) return;
    const lit = newlyLit(prevLights.current, cardLightMap);
    prevLights.current = cardLightMap;
    if (!lit.length) return;
    setLifted((cur) => new Set([...cur, ...lit]));
    for (const k of lit) bump(k);
    window.setTimeout(() => setLifted((cur) => new Set([...cur].filter((k) => !lit.includes(k)))), LIFT_MS);
  }, [cardLightMap, allRead, bump]);

  // ── holding a card ──
  // Set the moment a card is opened, before the state lands: the desktop's
  // default (your card) must never replace a card a link opened in the same
  // commit.
  const openedRef = useRef(false);
  const open = useCallback(
    (key: string, want?: Side) => {
      const c = byKey.get(key);
      if (!c) return;
      let s = want ?? c.opensOn;
      if (s === "front" && !c.hasFront) s = "back";
      if (s === "back" && !c.hasBack) s = "front";
      openedRef.current = true;
      setHeld(key);
      setSide(s);
      setScreen("card");
    },
    [byKey],
  );
  const flip = useCallback(() => {
    const c = held ? byKey.get(held) : null;
    if (!c || !c.hasFront || !c.hasBack) return;
    setSide((s) => (s === "front" ? "back" : "front"));
  }, [held, byKey]);

  // ── links (lib/app-view.ts; lib/cards.ts cardsTarget) ──
  const pendingRef = useRef<CardsTarget | null>(cardsTarget(viewFromHash(window.location.hash)));
  // The `inbox` link keeps the door in sight while the reads above it land
  // (the door is drawn only once read, and Waiting on you grows above it), for
  // a few seconds and only until the person scrolls or touches the page.
  const doorFocusUntil = useRef(0);
  const softRef = useRef(false);
  const apply = useCallback(
    (t: CardsTarget): boolean => {
      if ("app" in t) {
        openApp(t.app, t.view);
        return true;
      }
      if ("door" in t) {
        setScreen("deck");
        doorFocusUntil.current = Date.now() + 5000;
        window.setTimeout(() => document.querySelector("[data-door-list]")?.scrollIntoView({ block: "center" }), 60);
        return true;
      }
      if ("waiting" in t) {
        setScreen("deck");
        if (t.decisionId != null) {
          // decision:<id> names the older decisions inbox (server/routes/
          // decisions.py writes the link with its own id), never a ticket's:
          // open that message once the inbox has been read.
          if (inbox.notes == null) return false;
          const m = inbox.notes.find((x) => x.kind === "decision" && x.decision_id === t.decisionId);
          if (m) {
            setFocus(`note:${m.message_id}`);
            setNoteSheet(m.message_id);
          } else say("That decision isn't waiting any more.");
        }
        return true;
      }
      if (!deck) return false;
      const c = cardOf(cards, t.card);
      if (!c) {
        // A card the door just let in waits for the deck's next read.
        if (softRef.current) return false;
        say("That card isn't in your deck.");
        return true;
      }
      softRef.current = false;
      if (t.thread) ht.openOn(t.thread);
      if (t.supportId != null) setSupportFocus(t.supportId);
      open(c.key, t.side);
      return true;
    },
    [deck, cards, ht, open, say, inbox.notes],
  );
  useEffect(() => {
    const t = pendingRef.current;
    if (t && apply(t)) pendingRef.current = null;
  }, [apply]);
  useEffect(
    () =>
      onAppView((view) => {
        const t = cardsTarget(view);
        if (t && !apply(t)) pendingRef.current = t;
      }),
    [apply],
  );
  // The desktop holds your card from the start; the phone starts on the deck.
  useEffect(() => {
    if (!deck || held || pendingRef.current || openedRef.current) return;
    if (!phone) open("you", "back");
  }, [deck, held, phone, open]);
  // Another app handed your Host's chat something (lib/compose-inbox.ts) and
  // its composer is not drawn: open your card's back, where the composer
  // takes it. Once per arrival, so a later read of the deck never pulls the
  // person back here.
  const [composeWant, setComposeWant] = useState(0);
  useEffect(() => onComposeWanted(() => setComposeWant((n) => n + 1)), []);
  useEffect(() => {
    if (!composeWant || !deck) return;
    setComposeWant(0);
    open("you", "back");
  }, [composeWant, deck, open]);
  useEffect(() => {
    if (Date.now() > doorFocusUntil.current) return;
    const id = window.requestAnimationFrame(() =>
      document.querySelector("[data-door-list]")?.scrollIntoView({ block: "center" }),
    );
    return () => window.cancelAnimationFrame(id);
  }, [deck, door, waiting, inbox.notes, supportAsks, screen]);
  useEffect(() => {
    const stop = () => {
      doorFocusUntil.current = 0;
    };
    const kinds = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const k of kinds) window.addEventListener(k, stop, { passive: true });
    return () => {
      for (const k of kinds) window.removeEventListener(k, stop);
    };
  }, []);

  // ── live pushes (the rail's /ws, conversations-pane.tsx) ──
  const heldRef = useRef(held);
  heldRef.current = held;
  const readDeckRef = useRef(readDeck);
  readDeckRef.current = readDeck;
  const readWaitingRef = useRef(readWaiting);
  readWaitingRef.current = readWaiting;
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let alive = true;
    let dropped = false;
    const onMessage = (ev: MessageEvent) => {
      let msg: { type?: string; message_kind?: string } | null = null;
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg?.type === "chat_message") {
        htRef.current.refresh();
        void htRef.current.loadThreads();
        return;
      }
      if (msg?.type === "inbox.message") {
        void readDeckRef.current();
        void readWaitingRef.current();
        if (heldRef.current) bump(heldRef.current);
      }
    };
    const connect = () => {
      if (!alive) return;
      try {
        ws = new WebSocket(`${proto}//${location.host}/ws`);
      } catch {
        retry = setTimeout(connect, 5000);
        return;
      }
      ws.addEventListener("message", onMessage);
      // Back after a drop: whatever was pushed meanwhile was missed, so the
      // deck, Waiting on you and your Host's pages are read again (the
      // rail's conversations pane did this on reconnect).
      ws.addEventListener("open", () => {
        if (!dropped) return;
        dropped = false;
        void readDeckRef.current();
        void readWaitingRef.current();
        void htRef.current.loadThreads();
        htRef.current.refresh();
        if (heldRef.current) bump(heldRef.current);
      });
      ws.addEventListener("close", () => {
        dropped = true;
        if (alive) retry = setTimeout(connect, 5000);
      });
    };
    connect();
    return () => {
      alive = false;
      if (retry) clearTimeout(retry);
      try {
        ws?.removeEventListener("message", onMessage);
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [bump]);

  // ── what waits on you ──
  const asks = useMemo(
    () => (inbox.notes ?? []).filter((m) => (ASK_KINDS as readonly string[]).includes(m.kind)),
    [inbox.notes],
  );
  // The platform's notices for the Support card: a "New peer" notice is the
  // door's found Host already, so it is not said twice.
  const notices = useMemo(() => {
    const atDoor = new Set((door ?? []).map((i) => i.id));
    return (inbox.notes ?? []).filter((m) => m.kind === NOTICE_KIND && !atDoor.has(m.message_id));
  }, [inbox.notes, door]);
  const waitItems: WaitItem[] | null = useMemo(() => {
    if (!waiting && !door) return null;
    const out: WaitItem[] = [];
    for (const d of waiting?.decisions ?? []) out.push({ kind: "decision", d });
    for (const m of asks) out.push({ kind: "note", m });
    for (const s of waiting?.support_drafts ?? []) out.push({ kind: "draft", s });
    if (supportAsks > 0) out.push({ kind: "support_q", n: supportAsks });
    for (const i of door ?? []) out.push({ kind: "door", i });
    return out;
  }, [waiting, door, asks, supportAsks]);
  const goWait = useCallback(
    (w: WaitItem) => {
      setFocus(waitKey(w));
      if (w.kind === "decision") {
        const r = w.d.room;
        // Asked nowhere a page can show it (an app's ticket, an import, a
        // support card on the operator Host, a page deleted since): the
        // decision in full, where it can be answered.
        if (r.kind === "none" || r.gone) {
          setDecisionsSheet({ focus: w.d.id });
          return;
        }
        apply(waitingGo(w.d));
        if (r.kind !== "project") ht.askAgain(w.d.id);
        return;
      }
      if (w.kind === "note") {
        setNoteSheet(w.m.message_id);
        return;
      }
      if (w.kind === "draft") {
        setSupportFocus(w.s.id);
        open("support", "front");
        return;
      }
      if (w.kind === "support_q") {
        open("support", "front");
        return;
      }
      setScreen("deck");
      say(`${w.i.from?.name || "Someone"} is at the door, at the bottom of your cards.`);
      window.setTimeout(
        () => document.querySelector(`[data-door="${CSS.escape(w.i.id)}"]`)?.scrollIntoView({ block: "center" }),
        60,
      );
    },
    [apply, open, say, ht],
  );

  // ── search: names, Host names, page and topic titles; nothing leaves ──
  const found = useMemo(
    () => findCards(cards, query, { threads: ht.threads, topics }),
    [cards, query, ht.threads, topics],
  );
  const pickFound = (i: number) => {
    const f = found[i];
    if (!f) return;
    setQuery("");
    setSearchOpen(false);
    if (f.card.kind === "you" && f.thread) {
      ht.setThreadId(f.thread.id);
      open("you", "back");
      return;
    }
    if (f.thread && f.card.hostId != null) setPeerTopic({ host: f.card.hostId, id: f.thread.id });
    open(f.card.key, f.thread ? "back" : undefined);
  };
  const searchBox = (
    <label className="search" data-search>
      <span aria-hidden>⌕</span>
      <input
        aria-label="Find a person, a Host or a conversation"
        autoComplete="off"
        data-search-input
        onChange={(e) => {
          setQuery(e.target.value);
          setSearchOpen(true);
        }}
        onFocus={() => setSearchOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") pickFound(0);
          if (e.key === "Escape") {
            setQuery("");
            setSearchOpen(false);
          }
        }}
        placeholder="Find a person, a Host or a conversation"
        value={query}
      />
      {searchOpen && query.trim() && (
        <Pop label="Found" onClose={() => setSearchOpen(false)} style={{ top: 40, left: 0, right: 0 }}>
          {found.length === 0 ? (
            <p style={{ margin: 0 }}>No card, Host or conversation by that name.</p>
          ) : (
            <ul data-found>
              {found.slice(0, 12).map((f, i) => (
                <li key={`${f.card.key}:${f.thread?.id ?? ""}`}>
                  <button data-found-card={f.card.key} onClick={() => pickFound(i)} type="button">
                    <span>{f.thread ? f.thread.title : f.card.name}</span>
                    <span className="why">{f.thread ? `${f.card.name} · ${f.why}` : f.why}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Pop>
      )}
    </label>
  );
  // ── the card held ──
  const card: Card | null = held ? (byKey.get(held) ?? null) : null;
  const hue = card ? (card.kind === "you" ? 0 : hueOf(card.email || card.key)) : 0;
  const hint =
    !card || !card.hasBack || !card.hasFront
      ? ""
      : side === "back"
        ? "the back · tap the corner for the front"
        : "the front · tap the corner for the back";
  const giveTo = useCallback(
    (threadId: string, messageId: string, quote: string) => {
      const c = held ? byKey.get(held) : null;
      if (!c?.email) return Promise.resolve("This isn't a person's chat.");
      return ht.giveTo(threadId, { kind: "dm", ref: c.email, message_id: messageId, quote, who: c.name });
    },
    [held, byKey, ht],
  );
  const frontVisible = !!card && side === "front" && (!phone || screen === "card");
  const backVisible = !!card && side === "back" && (!phone || screen === "card");
  const bumpKey = held ? (bumps[held] ?? 0) : 0;
  const keepTopics = useCallback((id: number, t: TopicRow[]) => setTopics((m) => new Map(m).set(id, t)), []);

  const front = (() => {
    if (!card) return null;
    switch (card.kind) {
      case "you":
        return <YouFront hostName={hostName} hue={0} onTurnCorner={flip} />;
      case "person":
        return (
          <PersonFront
            card={card}
            hostName={hostName}
            hue={hue}
            onGive={giveTo}
            onRead={() => void readDeck()}
            onToast={say}
            onTurnCorner={flip}
            phone={phone}
            refreshKey={bumpKey}
            threads={ht.threads}
            visible={frontVisible}
          />
        );
      case "group":
        return <GroupFront card={card} onTurnCorner={flip} />;
      case "support":
        return (
          <SupportFront
            focusId={supportFocus}
            hostName={hostName}
            notices={notices}
            onChanged={() => void readWaiting()}
            onOpenNote={setNoteSheet}
            onOpenTicket={(id) => setSupportSheet({ id })}
            onToast={say}
            refreshKey={bumpKey}
          />
        );
      default:
        return <NoFront card={card} />;
    }
  })();
  const back = (() => {
    if (!card) return null;
    switch (card.kind) {
      case "you":
        return (
          <OwnBack
            hostName={hostName}
            ht={ht}
            hue={0}
            lights={lights}
            askHidden={!!(decisionsSheet || supportSheet || noteSheet)}
            onDecisionDetails={(id) => setDecisionsSheet({ focus: id })}
            onToast={say}
            onTurnCorner={flip}
            turns={turns}
            visible={backVisible}
            working={working}
          />
        );
      case "person":
        return card.blankBack || card.hostId == null ? (
          <BlankBack card={card} hue={hue} onTurnCorner={flip} />
        ) : (
          <PeerBack
            card={card}
            hue={hue}
            onChanged={() => void readDeck()}
            onToast={say}
            groups={deck?.groups ?? []}
            onTopics={keepTopics}
            onTurnCorner={flip}
            onTopicOpened={topicOpened}
            openTopic={peerTopic}
            person={card.name}
            refreshKey={bumpKey}
            visible={backVisible}
          />
        );
      case "host":
        return (
          <PeerBack
            card={card}
            hue={hue}
            onChanged={() => void readDeck()}
            onToast={say}
            groups={deck?.groups ?? []}
            onTopics={keepTopics}
            onTurnCorner={null}
            onTopicOpened={topicOpened}
            openTopic={peerTopic}
            person={null}
            refreshKey={bumpKey}
            visible={backVisible}
          />
        );
      case "group":
        return <GroupBack card={card} onTurnCorner={flip} />;
      case "room":
        return (
          <RoomBack
            card={card}
            onLeft={() => {
              setHeld(null);
              setScreen("deck");
              void readDeck();
            }}
            onToast={say}
            refreshKey={bumpKey}
            visible={backVisible}
          />
        );
      default:
        return null;
    }
  })();

  return (
    <TooltipProvider>
      <div
        className="cards"
        data-cards
        data-phone={phone || undefined}
        data-reduced={reduced || undefined}
        data-screen={phone ? screen : "card"}
      >
        {/* The platform's crisis notice: over every card, not in the thread
            (use-circles-chat raises it from the turn's governance frame). */}
        <CrisisNotice onDismiss={ht.chat.dismissCrisisNotice} open={ht.chat.crisisNotice} />
        <header className="top">
          <div className="brand">Circles</div>
          {!phone && searchBox}
        </header>
        <main className="table">
          <Guard label="deck">
          <nav aria-label="Your cards" className="deck" data-deck>
            <div className="phonebar">
              <span className="brand">Circles</span>
              <button
                aria-expanded={searchOpen}
                aria-label="Find a person, a Host or a conversation"
                className="ib"
                data-phone-search
                onClick={() => setSearchOpen((v) => !v)}
                type="button"
              >
                ⌕
              </button>
            </div>
            {phone && searchOpen && <div className="phone-search">{searchBox}</div>}
            {phone && (
              <PhoneToday
                coming={coming}
                focus={focus}
                hostName={hostName}
                items={waitItems}
                onAllDecisions={() => setDecisionsSheet({ focus: null })}
                onGo={goWait}
              />
            )}
            <div className="caps">Your cards</div>
            {deckErr && !deck ? (
              <div className="empty" role="alert">
                Couldn’t read your cards: {deckErr}{" "}
                <button className="link" onClick={() => void readDeck()} type="button">
                  Try again
                </button>
              </div>
            ) : !deck ? (
              <div className="empty">Reading your cards…</div>
            ) : (
              shown.map((c) => (
                <DeckCard
                  card={c}
                  held={!phone && held === c.key}
                  hostName={hostName}
                  key={c.key}
                  lift={lifted.has(c.key)}
                  lights={cardLights(c, self, supportLit)}
                  onOpen={() => (!phone && held === c.key ? flip() : open(c.key))}
                  working={c.kind === "you" && working.size > 0}
                />
              ))
            )}
            {connecting ? (
              <Connect onClose={() => setConnecting(false)} onToast={say} />
            ) : (
              <button className="dc add" data-connect-open onClick={() => setConnecting(true)} type="button">
                + Connect with someone
              </button>
            )}
            {/* The Address book's list: every contact, the blocked ones (who
                leave the deck) with Unblock, and Add by handle. */}
            <span style={{ position: "relative", display: "block" }}>
              <button className="link allcontacts" data-contacts-open onClick={() => setContactsOpen(true)} type="button">
                All your contacts ›
              </button>
              {contactsOpen && (
                <Guard label="contacts" onClose={() => setContactsOpen(false)}>
                  <ContactsSheet
                    onChanged={() => void readDeck()}
                    onClose={() => setContactsOpen(false)}
                    onMessage={(ref) => {
                      const c = cardOf(cards, ref);
                      if (!c) {
                        say("That contact has no card in your deck yet.");
                        return;
                      }
                      setContactsOpen(false);
                      open(c.key, ref.kind === "dm" ? "front" : "back");
                    }}
                    onToast={say}
                  />
                </Guard>
              )}
            </span>
            {waitErr && (
              <div className="empty" data-wait-error role="alert">
                {waitErr}{" "}
                <button className="link" onClick={() => void readWaiting()} type="button">
                  Try again
                </button>
              </div>
            )}
            {door && door.length > 0 && (
              <>
                <div className="caps" data-door-list>
                  At the door
                </div>
                {door.map((i) => (
                  <DoorCard
                    item={i}
                    key={i.id}
                    onDone={(accepted) => {
                      void readWaiting();
                      const email = accepted?.email?.toLowerCase();
                      if (email) {
                        // Accepted: the person is one of your cards now, and
                        // their card opens (mockup:332) once the deck has it.
                        pendingRef.current = { card: { kind: "dm", email }, side: "front" };
                        softRef.current = true;
                      }
                      void readDeck();
                    }}
                    onOpen={() => setNoteSheet(i.id)}
                    onToast={say}
                  />
                ))}
              </>
            )}
          </nav>
          </Guard>
          <section aria-label="The card you hold" className="hold" data-hold={held ?? ""} data-side={side}>
            <div className="cardbar">
              <button
                data-back-to-deck
                onClick={() => {
                  setScreen("deck");
                  redraw();
                }}
                type="button"
              >
                ‹ Cards
              </button>
              <span className="hint" data-flip-hint>
                {hint}
              </span>
            </div>
            {card ? (
              <div
                className={`card3d${side === "back" ? " flipped" : ""}`}
                data-card3d={card.key}
                data-motion={reduced ? "fade" : "turn"}
                key={card.key}
              >
                <div
                  aria-hidden={side !== "front"}
                  className="facecard front"
                  data-face-side="front"
                  inert={side !== "front"}
                >
                  <Guard label="front">{front}</Guard>
                </div>
                <div
                  aria-hidden={side !== "back"}
                  className="facecard back"
                  data-face-side="back"
                  inert={side !== "back"}
                >
                  <Guard label="back">{back}</Guard>
                </div>
              </div>
            ) : (
              <div className="blank">Pick a card.</div>
            )}
          </section>
          {!phone && (
            <Guard label="waiting">
            <Today
              coming={coming}
              focus={focus}
              hostName={hostName}
              items={waitItems}
              onAllDecisions={() => setDecisionsSheet({ focus: null })}
              onGo={goWait}
            />
            </Guard>
          )}
        </main>
        {decisionsSheet && (
          <Guard label="decisions" onClose={() => setDecisionsSheet(null)}>
          <DecisionsSheet
            focusId={decisionsSheet.focus}
            onChanged={(id) => {
              // Answered here: its ask card on your Host's back goes now,
              // not at the next read of the page's tickets.
              ht.chat.settleAsk(id);
              void readWaiting();
              void ht.loadThreads();
              void ht.refreshCards(false);
            }}
            onClose={() => setDecisionsSheet(null)}
          />
          </Guard>
        )}
        {supportSheet && (
          <Guard label="support" onClose={() => setSupportSheet(null)}>
          <SupportSheet
            onChanged={() => {
              void readWaiting();
              bump("support");
            }}
            onClose={() => setSupportSheet(null)}
            onCreated={(id) => {
              void readWaiting();
              bump("support");
              setSupportSheet({ id });
            }}
            ticketId={supportSheet.id}
          />
          </Guard>
        )}
        {noteSheet && (
          <Guard label="note" onClose={() => setNoteSheet(null)}>
          <NoteSheet
            hostName={hostName}
            id={noteSheet}
            onArchived={() => void readWaiting()}
            onClose={() => setNoteSheet(null)}
            onTellHost={() => {
              setNoteSheet(null);
              ht.openBlank();
              open("you", "back");
            }}
          />
          </Guard>
        )}
        {toast && (
          <div aria-live="polite" className="toast" data-toast role="status">
            {toast}
          </div>
        )}
      </div>
    </TooltipProvider>
  );
}
