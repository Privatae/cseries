import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PlayIcon, XIcon } from "lucide-react";
import { ArchiveDialog } from "@/components/workbench/archive-dialog";
import { ChatPane } from "@/components/workbench/chat-pane";
import { OpsPane } from "@/components/workbench/ops-pane";
import { StartDialog } from "@/components/workbench/start-dialog";
import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useDeliverySocket, type DeliveryNotice } from "@/hooks/use-delivery-socket";
import {
  createProject,
  fetchActiveTurn,
  fetchBuckets,
  fetchHistory,
  fetchProjectPage,
  fetchProjects,
  getLastOpenProject,
  markProjectSeen,
  placeProject,
  putLastOpenProject,
  restoreProject,
  sendFromIndex,
} from "@/lib/api";
import { onAppView, projectOfView, viewFromHash } from "@/lib/app-view";
import {
  LIST_POLL_MS,
  PHONE_QUERY,
  SETTLE_RECHECK_MS,
  TURNS_POLL_MS,
  adoptList,
  arrowsTurnPages,
  bookOrder,
  bootRetryMs,
  flipMotion,
  flipNeighbour,
  isPhoneWidth,
  landedMoved,
  prefersReducedMotion,
  scrollWithin,
  seenOnList,
  seenOnOpen,
  seenOnSettle,
  stoppedWorking,
  tabPages,
  touched,
  withBaselines,
  withSeen,
  workingPages,
} from "@/lib/book";
import {
  archivedNote,
  chapterOf,
  chaptersOf,
  outcomeLine,
  pickRequestId,
  riffleRun,
  sendOutcome,
  startOutcome,
  startWords,
  type Chapter,
  type IndexPick,
} from "@/lib/book-index";
import { withApprovalLine, withoutApprovalLine } from "@/lib/code-approval";
import { applyPlace } from "@/lib/project-groups";
import { latestBuild, latestBuildKey } from "@/lib/strands";
import type {
  ApprovalLineEvent,
  Bucket,
  DeliveryEvent,
  Project,
  ProjectPage,
  RunSession,
  RunningBuild,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { BookBench } from "./bench";
import { turnAway } from "./flip";
import { INDEX_INPUT_ID, IndexPage } from "./index-page";
import { LatestBuildOpen } from "./latest-build";
import { PageHead, Stands, Tally } from "./page-top";
import { riffle, type RiffleLeaf } from "./riffle";
import { BOOK_PAGE_ID, BOOK_RIGHT_ID, BookTabs, focusTab } from "./tabs";

/** How much of the retired General thread's opening message pre-fills the
 * Start box: enough to recognise the conversation, not the whole turn. */
const GENERAL_SEED_CHARS = 400;

/** The person's turns still in the retired General thread (the projectless
 * thread, now gone), or null when there are none or the read
 * failed. The Start dialog offers them to the project it opens, once. */
async function generalTurns(): Promise<{ userTurns: number; seedText: string } | null> {
  // An answer that is not a list of turns counts as none: the boot waits on
  // this read, and the list poll waits on the boot.
  let h: Awaited<ReturnType<typeof fetchHistory>>;
  try {
    h = await fetchHistory(null);
  } catch {
    return null;
  }
  if (!h.ok || !Array.isArray(h.data?.turns)) return null;
  const users = h.data.turns.filter((t) => t.role === "user" && t.metadata?.source_kind !== "thread_marker");
  return users.length
    ? { userTurns: users.length, seedText: (users[0].content || "").slice(0, GENERAL_SEED_CHARS) }
    : null;
}

/** A page as it goes past in a riffle: its chapter, when it was last
 * written on, its title. */
function leafOf(p: Project, chapters: Chapter[]): RiffleLeaf {
  const c = chapterOf(chapters, p);
  const at = p.last_activity_at ? new Date(p.last_activity_at) : null;
  return {
    chapter: !c || c.id == null ? "Loose pages" : `${c.name} chapter`,
    when: at && !Number.isNaN(at.getTime()) ? at.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "",
    title: p.name,
  };
}

/**
 * The Workbench as a book of pages: every open project is a page (its kind
 * and running token count, its title, where it stands, the conversation, the
 * box to write on it, which nudges a running turn and holds to think harder);
 * tabs on the book's edge turn to the newest seven and light up when
 * something lands; the right side is "This page" and the bench's sections. On
 * a phone the page is full width, the tabs a strip along the bottom, and the
 * right side a sheet over the page. The Index (src/book/index-page.tsx) is a
 * page of its own over the page in front: the pages re-sort as the person
 * types, Enter sends the message to the page the Host picks or opens the top
 * one, and the buckets are its chapters, opened with a riffle
 * (src/book/riffle.ts) and filed into by dragging a tab. The book is the
 * Workbench's one layout (src/main.tsx): the rail, chat and bench columns are
 * gone, and what only they offered and the book still needs came here:
 * archiving a page (with its Code Repository, when the person asks), deleting
 * a chapter (index-page.tsx), and the one-time move of the retired General
 * thread's turns into a project (the Start dialog).
 *
 * It holds ONE chat pane and ONE bench, in their "page" chrome: the
 * conversation client (hooks/use-workbench-chat.ts) keeps every page's
 * thread, so turning the page never stops a stream, and the wiring between
 * them below carries deliveries, running builds and approvals.
 */
export function BookApp() {
  // Every project the Workbench lists (archived ones too: a page opened from
  // the Index may be one), with the page marks the tabs read.
  const [rows, setRows] = useState<Project[]>([]);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const [loaded, setLoaded] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);
  const openRef = useRef(openId);
  openRef.current = openId;
  const [pages, setPages] = useState<Record<number, ProjectPage | null>>({});
  const [pageErrors, setPageErrors] = useState<Record<number, string | null>>({});
  // The seen mark as it stood when each page's current opening began: what
  // "Where this stands" tints against.
  const [seenBefore, setSeenBefore] = useState<Record<number, string | null>>({});
  const [working, setWorking] = useState<Set<number>>(() => new Set());
  const workingRef = useRef(working);
  // Pages with no seen mark are held against what had landed when the book
  // first read them (lib/book.ts tabLight).
  const baselinesRef = useRef<Map<number, string | null>>(new Map());
  // The Index: a page of its own, drawn over the page in front.
  const [indexOpen, setIndexOpen] = useState(false);
  const indexOpenRef = useRef(false);
  const setIndex = useCallback((open: boolean) => {
    indexOpenRef.current = open;
    setIndexOpen(open);
  }, []);
  // The chapters are the buckets.
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const bucketsRef = useRef(buckets);
  bucketsRef.current = buckets;
  const [draggingTab, setDraggingTab] = useState<number | null>(null);
  // The message sent from the Index, and what it came to (lib/book-index.ts
  // IndexPick). Held here, not by the Index, so that one is out at a time
  // however often the Index is closed and opened meanwhile.
  const [indexPick, setIndexPickState] = useState<IndexPick | null>(null);
  const indexPickRef = useRef<IndexPick | null>(null);
  const setIndexPick = useCallback((p: IndexPick | null) => {
    indexPickRef.current = p;
    setIndexPickState(p);
  }, []);
  // A message sent from the Index was written on a page and started its turn
  // there: the page reads its conversation again (the Host's note of why it
  // picked the page is in it) and follows the turn (ChatPane reloadNonce).
  const [reloadNonce, setReloadNonce] = useState(0);
  // The words that start a new page from the Index ("new" mode), until the
  // pane that shows the page has taken them as its first message (ChatPane
  // firstMessage).
  const [firstMessage, setFirstMessage] = useState<{ projectId: number; text: string; nonce: number } | null>(null);
  // An archived page that could not come back when written on: why.
  const [restoreRefused, setRestoreRefused] = useState<{ id: number; reason: string } | null>(null);
  const [startOpen, setStartOpen] = useState(false);
  // Turns still in the retired General thread, offered to the Start dialog
  // once (generalTurns).
  const [pendingGeneral, setPendingGeneral] = useState<{ userTurns: number; seedText: string } | null>(null);
  // The page being archived (ArchiveDialog asks first, and asks about its
  // Code Repository where that can be honoured).
  const [archiving, setArchiving] = useState<Project | null>(null);
  const [composerSeed, setComposerSeed] = useState<{ text: string; nonce: number } | null>(null);
  const [controlsSlot, setControlsSlot] = useState<HTMLElement | null>(null);
  // The phone layout (lib/book.ts PHONE_QUERY): the tabs are a strip along
  // the bottom, and the right side is a sheet over the page, opened from it.
  const [phone, setPhone] = useState(() => isPhoneWidth(window));
  const [sheetOpen, setSheetOpen] = useState(false);
  useEffect(() => {
    let mq: MediaQueryList | null = null;
    try {
      mq = window.matchMedia(PHONE_QUERY);
    } catch {
      return;
    }
    const on = () => setPhone(mq?.matches ?? false);
    on();
    mq.addEventListener("change", on);
    return () => mq?.removeEventListener("change", on);
  }, []);
  useEffect(() => {
    if (!phone) setSheetOpen(false);
  }, [phone]);

  // ── Deliveries, notices, running builds, the Code Approval raised over
  //    the chat and its chat lines ──────────────────────────────────────
  const [notices, setNotices] = useState<(DeliveryNotice & { id: number })[]>([]);
  const pushNotice = useCallback((n: DeliveryNotice) => {
    const id = Date.now() + Math.random();
    setNotices((cur) => [...cur.slice(-4), { ...n, id }]);
    window.setTimeout(() => setNotices((cur) => cur.filter((x) => x.id !== id)), 12000);
  }, []);
  const [delivery, setDelivery] = useState<DeliveryEvent | null>(null);
  const announceDelivery = useCallback(
    (info: {
      projectId: number | null;
      taskId?: number | string | null;
      rowId?: number | null;
      appId?: string | null;
      origin_app_id?: string | null;
    }) =>
      setDelivery((cur) => ({
        seq: (cur?.seq ?? 0) + 1,
        projectId: info.projectId,
        taskId: info.taskId != null ? String(info.taskId) : null,
        rowId: info.rowId ?? null,
        appId: info.appId ?? info.origin_app_id ?? null,
      })),
    [],
  );
  const onDeliveryMissed = useCallback(
    (message: string) => pushNotice({ message, level: "error" }),
    [pushNotice],
  );
  useDeliverySocket(announceDelivery, pushNotice);
  const [runningBuilds, setRunningBuilds] = useState<RunningBuild[]>([]);
  // The runs the bench polls (OpsPane onSessions), for "This page"'s Open on
  // the latest finished build. Held in a ref: the book draws again only when
  // that build moves (latestBuildKey), not on every poll.
  const runSessionsRef = useRef<RunSession[]>([]);
  const [latestKey, setLatestKey] = useState("");
  const onRunSessions = useCallback((list: RunSession[]) => {
    runSessionsRef.current = list;
    setLatestKey(latestBuildKey(latestBuild(list, openRef.current)));
  }, []);
  const [approval, setApproval] = useState<number | null>(null);
  const [approvalsTick, setApprovalsTick] = useState(0);
  const [approvalLines, setApprovalLines] = useState<Record<number, ApprovalLineEvent>>({});
  const approvalLineSeq = useRef(0);
  const announceApprovalLine = useCallback((projectId: number, number: number, written = true) => {
    setApprovalLines((cur) =>
      withApprovalLine(cur, projectId, number, written, () => ++approvalLineSeq.current));
  }, []);
  const approvalLineDone = useCallback(
    (seq: number) => setApprovalLines((cur) => withoutApprovalLine(cur, seq)),
    [],
  );
  useEffect(() => {
    setApproval(null);
  }, [openId]);

  // ── Reads ──────────────────────────────────────────────────────────────
  const loadRows = useCallback(async (): Promise<Project[] | null> => {
    const r = await fetchProjects();
    if (!r.ok) return null;
    baselinesRef.current = withBaselines(baselinesRef.current, r.data);
    setRows((cur) => adoptList(cur, r.data));
    setLoaded(true);
    return r.data;
  }, []);
  const loadBuckets = useCallback(async () => {
    const r = await fetchBuckets();
    if (r.ok) setBuckets(r.data.buckets);
  }, []);

  const pageReads = useRef(new Set<number>());
  const loadPage = useCallback(async (id: number) => {
    if (pageReads.current.has(id)) return;
    pageReads.current.add(id);
    try {
      const r = await fetchProjectPage(id);
      if (r.ok) {
        setPages((cur) => ({ ...cur, [id]: r.data }));
        setPageErrors((cur) => ({ ...cur, [id]: null }));
      } else {
        setPages((cur) => ({ ...cur, [id]: cur[id] ?? null }));
        setPageErrors((cur) => ({ ...cur, [id]: r.message }));
      }
    } finally {
      pageReads.current.delete(id);
    }
  }, []);

  // PUT /api/projects/{id}/seen, one request at a time per page (a second
  // caller shares the one in flight). The answer is the mark as stored,
  // adopted at once so no tab and no poll reads the old one, and returned.
  // `turnToken`: the page's turn the person watched end (markSeenNow).
  const marking = useRef(new Map<number, Promise<string | null>>());
  const markSeen = useCallback((id: number, turnToken: string | null = null): Promise<string | null> => {
    const inFlight = marking.current.get(id);
    if (inFlight && !turnToken) return inFlight;
    const p = (async () => {
      try {
        const r = await markProjectSeen(id, turnToken);
        if (!r.ok || !r.data.seen_at) return null;
        const at = String(r.data.seen_at);
        setRows((cur) => withSeen(cur, id, at));
        return at;
      } finally {
        marking.current.delete(id);
      }
    })();
    marking.current.set(id, p);
    return p;
  }, []);
  // A mark sent from now on: one already on its way may have left before the
  // moment that matters to the caller, so it is let land and asked again.
  // With the turn the person watched end, which the server then never lets
  // light the page, however late its reply is written (projects.seen_turns).
  const markSeenNow = useCallback(
    async (id: number, turnToken: string | null = null): Promise<string | null> => {
      const inFlight = marking.current.get(id);
      if (inFlight) await inFlight;
      return markSeen(id, turnToken);
    },
    [markSeen],
  );

  // ── Boot: the page a link asked for, else the one last open, else the
  //    newest; an empty book offers Start ──────────────────────────────────
  const requestedRef = useRef<number | null>(projectOfView(viewFromHash(window.location.hash)));
  const bootedRef = useRef(false);
  // The person closed every page before the boot ended (onArchived).
  const closedAllRef = useRef(false);
  useEffect(() => {
    void loadBuckets();
    let alive = true;
    let retry: number | undefined;
    // A read due while the window is hidden waits until it shows, as the list
    // poll does (its interval and visibilitychange listener below).
    let dueWhenShown: number | null = null;
    const onShow = () => {
      if (document.hidden || dueWhenShown == null) return;
      const attempt = dueWhenShown;
      dueWhenShown = null;
      void boot(attempt);
    };
    const boot = async (attempt: number): Promise<void> => {
      let reads: [Project[] | null, Awaited<ReturnType<typeof getLastOpenProject>>, Awaited<ReturnType<typeof generalTurns>>] | null;
      try {
        reads = await Promise.all([loadRows(), getLastOpenProject(), generalTurns()]);
      } catch {
        // A read that threw (an answer that is not what it should be) is a
        // read that failed: it is read again below, never left to stop the
        // boot, since the list poll waits for the boot (bootedRef).
        reads = null;
      }
      if (!alive || bootedRef.current) return;
      const [list, last, general] = reads ?? [null, null, null];
      if (!Array.isArray(list) || last == null) {
        // The list was not read, so there is nothing to pick a page from: read
        // it again (lib/book.ts BOOT_RETRY_MS says why).
        retry = window.setTimeout(() => {
          retry = undefined;
          if (document.hidden) dueWhenShown = attempt + 1;
          else void boot(attempt + 1);
        }, bootRetryMs(attempt));
        return;
      }
      bootedRef.current = true;
      // Turns still in the retired General thread: the Start dialog opens
      // with them, and the project it opens adopts them (start-dialog.tsx).
      if (general) {
        setPendingGeneral(general);
        setStartOpen(true);
      }
      const all = list;
      const asked = requestedRef.current;
      requestedRef.current = null;
      // A last-open answer with no body (null) is no page last open: it is read
      // after bootedRef is set, so a throw here would leave no page open.
      const lastId = last.ok && last.data?.ref ? Number(last.data.ref) : null;
      // After the person closed every page (onArchived) only a page asked for
      // since then opens: the default pick is not theirs, and is older.
      const first =
        (asked != null && all.find((p) => p.id === asked)) ||
        (!closedAllRef.current && lastId != null && all.find((p) => p.id === lastId)) ||
        (!closedAllRef.current && bookOrder(all)[0]) ||
        null;
      if (asked != null && first?.id !== asked) {
        pushNotice({ message: `Project ${asked} is not in your Workbench any more.`, level: "error" });
      }
      // Only when no page is open yet: the tabs come from any read of the list
      // (the poll's too), and a page the person turned to while this read was
      // out stays in front.
      if (first) setOpenId((cur) => cur ?? first.id);
    };
    document.addEventListener("visibilitychange", onShow);
    void boot(0);
    return () => {
      alive = false;
      if (retry) window.clearTimeout(retry);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [loadRows, loadBuckets, pushNotice]);

  // A view the desktop sends while the book is open: that page, to the front.
  // (Its turn is goTo, defined below and held in a ref so this subscribes
  // once: a request is never dropped for a turn still running.) The
  // newest choice wins: once a page is open, the person's or an earlier
  // view's, a view turns to its page even before the boot has ended, and the
  // boot then keeps it (its pick applies only when no page is open).
  const flipRef = useRef<(id: number) => void>(() => {});
  useEffect(
    () =>
      onAppView((view) => {
        const id = projectOfView(view);
        if (id == null) return;
        if (bootedRef.current || openRef.current != null) flipRef.current(id);
        else requestedRef.current = id;
      }),
    [],
  );

  // ── Opening a page: mark it seen, read its page, remember it ───────────
  const prevOpenRef = useRef<number | null>(null);
  useEffect(() => {
    const id = seenOnOpen(prevOpenRef.current, openId);
    prevOpenRef.current = openId;
    if (id == null) return;
    const row = rowsRef.current.find((p) => p.id === id);
    setSeenBefore((cur) => ({ ...cur, [id]: row?.seen_at ?? null }));
    void markSeen(id);
    void loadPage(id);
    // The Workbench opens where it was left, whichever
    // layout left it there.
    void putLastOpenProject(id);
  }, [openId, markSeen, loadPage]);

  // Something the person did not watch landed on the page in front (its row
  // says so): mark it seen. And whatever landed on it, the reply they watched
  // included, read the page again: its running count and "Where this stands"
  // move with it, and the count ticks up (src/book/page-top.tsx).
  const landedRef = useRef<{ id: number | null; at: string | null | undefined }>({ id: null, at: undefined });
  useEffect(() => {
    const row = openId == null ? null : rows.find((r) => r.id === openId);
    const prev = landedRef.current.id === openId ? landedRef.current.at : undefined;
    if (row) landedRef.current = { id: openId, at: row.last_landed_at ?? null };
    const moved = landedMoved(prev, row);
    const id = seenOnList(openId, rows);
    if (id != null) void markSeen(id);
    if (openId != null && (moved || id != null)) void loadPage(openId);
  }, [rows, openId, markSeen, loadPage]);

  // The page's own turn ended while it was in front: the person watched the
  // reply arrive. Mark it seen with that turn (markSeenNow: a mark sent after
  // the stream ended), so its reply, written after the stream, never lights
  // the page whenever it lands. Then read the page and the list, and the list
  // again a few times (SETTLE_RECHECK_MS): the reply's row usually lands within
  // a second or two, and the count ticks up when it does.
  const rechecks = useRef<number[]>([]);
  useEffect(() => () => rechecks.current.forEach((t) => window.clearTimeout(t)), []);
  const onTurnSettled = useCallback(
    (projectId: number | null, turnToken: string | null) => {
      const id = seenOnSettle(openRef.current, projectId);
      if (id == null) return;
      void markSeenNow(id, turnToken);
      void loadPage(id);
      void loadRows();
      rechecks.current.forEach((t) => window.clearTimeout(t));
      rechecks.current = SETTLE_RECHECK_MS.map((ms) => window.setTimeout(() => void loadRows(), ms));
    },
    [markSeenNow, loadPage, loadRows],
  );

  // ── Polls ──────────────────────────────────────────────────────────────
  // Running turns, often (an in-memory read, lib/book.ts TURNS_POLL_MS): the
  // working dots, and a turn that stopped sends the book to the list now.
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      if (!alive) return;
      if (!document.hidden) {
        const r = await fetchActiveTurn(null);
        if (!alive) return;
        if (r.ok) {
          const next = workingPages(r.data.turns);
          const stopped = stoppedWorking(workingRef.current, next);
          workingRef.current = next;
          setWorking(next);
          if (stopped.length) void loadRows();
        }
      }
      timer = window.setTimeout(tick, TURNS_POLL_MS);
    };
    void tick();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [loadRows]);

  // The list on its own (LIST_POLL_MS says why 10 s), while the window shows;
  // and at once when it shows again, or when a delivery lands anywhere. Not
  // before the boot has read it: until then the boot's reads, and its retries,
  // are the list's reads, and one from here beside them is the same read twice.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (!document.hidden && bootedRef.current) void loadRows();
    }, LIST_POLL_MS);
    const onShow = () => {
      if (!document.hidden && bootedRef.current) void loadRows();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [loadRows]);
  useEffect(() => {
    if (delivery) void loadRows();
  }, [delivery, loadRows]);

  // ── Turning the page ───────────────────────────────────────────────────
  const order = useMemo(() => bookOrder(rows).map((p) => p.id), [rows]);
  const orderRef = useRef(order);
  orderRef.current = order;
  const tabs = useMemo(() => tabPages(rows), [rows]);
  const bookRef = useRef<HTMLDivElement | null>(null);
  const sheetRef = useRef<HTMLElement | null>(null);
  const turningRef = useRef(false);
  // Turn what is in front (a page, or the Index) away; the caller draws what
  // comes next underneath. False while another turn runs.
  const turnSheet = useCallback((): boolean => {
    if (turningRef.current) return false;
    const sheet = sheetRef.current;
    const book = bookRef.current;
    if (sheet && book && (openRef.current != null || indexOpenRef.current)) {
      turningRef.current = true;
      turnAway(sheet, book, flipMotion(prefersReducedMotion(window)), () => {
        turningRef.current = false;
      });
    }
    return true;
  }, []);
  // Turn to a page; true when the book turned to it. From the Index, the
  // page under it is a page to turn to too.
  const flipTo = useCallback((id: number): boolean => {
    // One turn at a time, as in the mockup: a press during a turn is dropped.
    if ((id === openRef.current && !indexOpenRef.current) || !turnSheet()) return false;
    setIndex(false);
    setOpenId(id);
    return true;
  }, [turnSheet, setIndex]);
  // Land on a page whatever is turning: a send's answer is never dropped.
  const goTo = useCallback((id: number) => {
    if (!flipTo(id)) {
      setIndex(false);
      setOpenId(id);
    }
  }, [flipTo, setIndex]);
  flipRef.current = goTo;

  // A riffle through `run` (a chapter's pages), landing on what `land`
  // draws; with reduced motion, a crossfade (src/book/riffle.ts).
  const riffleTo = useCallback((run: Project[], land: () => void) => {
    const sheet = sheetRef.current;
    const book = bookRef.current;
    if (!sheet || !book || turningRef.current) {
      land();
      return;
    }
    const chapters = chaptersOf(rowsRef.current, bucketsRef.current);
    turningRef.current = true;
    riffle(sheet, book, run.map((p) => leafOf(p, chapters)), flipMotion(prefersReducedMotion(window)), land, () => {
      turningRef.current = false;
    });
  }, []);

  // ── The Index ──────────────────────────────────────────────────────────
  const openIndex = useCallback(() => {
    setSheetOpen(false);
    if (indexOpenRef.current) {
      document.getElementById(INDEX_INPUT_ID)?.focus();
      return;
    }
    if (!turnSheet()) return;
    setIndex(true);
    void loadBuckets();
  }, [turnSheet, setIndex, loadBuckets]);
  const closeIndex = useCallback(() => {
    if (!indexOpenRef.current) return;
    turnSheet();
    setIndex(false);
  }, [turnSheet, setIndex]);

  // A page opened from the Index: a turn, or for an archived page a riffle
  // through its chapter to it. It stays archived until it is written on.
  const openFromIndex = useCallback((id: number) => {
    const row = rowsRef.current.find((p) => p.id === id);
    if (row?.status !== "archived") {
      goTo(id);
      return;
    }
    const chapters = chaptersOf(rowsRef.current, bucketsRef.current);
    riffleTo(riffleRun(chapterOf(chapters, row)?.archived ?? [], id), () => {
      setIndex(false);
      setOpenId(id);
    });
  }, [goTo, riffleTo, setIndex]);

  // Enter in the Index's "send" mode: the Host picks the page, and the book
  // lands there: a turn, a riffle when the page came back from the archive.
  // The turn already runs on it; the page reads its conversation again and
  // follows it (reloadNonce). One message is out at a time (pickRequestId):
  // a second Enter while the Host picks sends nothing, wherever it is
  // pressed; the same words again after an answer that never came reuse
  // their request id, so the server's answer is the one send's.
  const sendIndex = useCallback(
    async (text: string): Promise<void> => {
      const requestId = pickRequestId(indexPickRef.current, text);
      if (requestId == null) return;
      const out: IndexPick = { text, requestId, outcome: null };
      setIndexPick(out);
      // The rows as they stand: an archived pick's chapter, for its riffle.
      const before = rowsRef.current;
      const r = await sendFromIndex(text, openRef.current, requestId);
      const o = sendOutcome(r.status, r.body);
      setIndexPick({ ...out, outcome: o });
      if (o.kind !== "landed" && o.kind !== "turn_refused") {
        // The Index says what it came to; closed meanwhile, a notice does,
        // and the Index shows it again when opened.
        if (!indexOpenRef.current) {
          pushNotice(
            o.kind === "ask"
              ? { title: "Your message from the Index", message: "The Host could not pick its page. Open the Index to choose one.", level: "info" }
              : { title: "Your message from the Index", message: outcomeLine(o), level: "error" },
          );
        }
        return;
      }
      const id = o.projectId;
      let row = before.find((p) => p.id === id) ?? null;
      if (!row) {
        const list = await loadRows();
        row = list?.find((p) => p.id === id) ?? null;
      }
      if (o.restored && row) {
        const chapters = chaptersOf(before, bucketsRef.current);
        riffleTo(riffleRun(chapterOf(chapters, row)?.archived ?? [], id), () => {
          setIndex(false);
          setOpenId(id);
        });
      } else {
        goTo(id);
      }
      const at = new Date().toISOString();
      setRows((cur) =>
        touched(
          cur.map((p) => (p.id === id && p.status === "archived" ? { ...p, status: "active", group: "active" } : p)),
          id,
          at,
        ),
      );
      setReloadNonce((n) => n + 1);
      void loadRows();
      if (o.kind === "turn_refused") {
        pushNotice({ title: `Written on ${o.name}`, message: `The Host did not start: ${o.error}`, level: "error" });
        setComposerSeed({ text, nonce: Date.now() });
      }
    },
    [loadRows, riffleTo, goTo, setIndex, setIndexPick, pushNotice],
  );

  // Enter in the Index's "new" mode: a page of its own for these words,
  // whatever the Host would have picked. The page is made as the Start box
  // makes one (POST /api/projects: its title suggested from the words, the
  // words its description); the book turns to it, and the words are its first
  // message (ChatPane firstMessage). One page is made at a time, by the gate a
  // send runs under (pickRequestId): a second Enter while it is made starts
  // nothing, wherever it is pressed.
  const startPage = useCallback(
    async (text: string): Promise<void> => {
      const requestId = pickRequestId(indexPickRef.current, text);
      if (requestId == null) return;
      const out: IndexPick = { text, requestId, outcome: null, startsPage: true };
      setIndexPick(out);
      // Made with the words' secret shapes cut (startWords): the words
      // themselves go only to the page's own pane, below.
      const { title, description } = startWords(text);
      const r = await createProject(title, description);
      const o = startOutcome(r, title);
      setIndexPick({ ...out, outcome: o });
      if (!r.ok || o.kind !== "landed") {
        // The Index says what it came to; closed meanwhile, a notice does.
        if (!indexOpenRef.current) {
          pushNotice({ title: "Your message from the Index", message: outcomeLine(o), level: "error" });
        }
        return;
      }
      const id = o.projectId;
      // The page is in the book before it turns: its tab and its title. The
      // list read after says where it really stands.
      const at = new Date().toISOString();
      setRows((cur) =>
        cur.some((p) => p.id === id)
          ? cur
          : [{ id, name: o.name, description, status: "planning", bucket_id: null, last_activity_at: at }, ...cur],
      );
      setFirstMessage({ projectId: id, text, nonce: Date.now() });
      goTo(id);
      void loadRows();
      if (r.data.repo_note) {
        pushNotice({ title: `Code Repository for ${o.name}`, message: r.data.repo_note, level: "info" });
      }
    },
    [loadRows, goTo, setIndexPick, pushNotice],
  );

  // The Host could not pick, and the person did: the page, with the
  // message in its box to send.
  const askPick = useCallback((id: number, text: string) => {
    setIndexPick(null);
    openFromIndex(id);
    setComposerSeed({ text, nonce: Date.now() });
  }, [openFromIndex, setIndexPick]);

  // A chapter opened: a riffle through its pages, landing on its contents.
  const openChapter = useCallback((chapter: Chapter, land: () => void) => {
    riffleTo(chapter.archived.length ? riffleRun(chapter.archived, null) : riffleRun(chapter.open, null), land);
  }, [riffleTo]);

  // A tab dropped on a chapter: the page is filed there (PATCH
  // /api/projects/{id}/place), at once and then as
  // the server has it.
  const fileInChapter = useCallback(async (id: number, bucketId: number | null) => {
    setDraggingTab(null);
    const row = rowsRef.current.find((p) => p.id === id);
    const from = row?.bucket_id != null && bucketsRef.current.some((b) => b.id === row.bucket_id) ? row.bucket_id : null;
    if (!row || from === bucketId) return;
    setRows((cur) => applyPlace(cur, bucketsRef.current, id, bucketId, null));
    const r = await placeProject(id, bucketId, null);
    const where = bucketId == null ? "no chapter" : (bucketsRef.current.find((b) => b.id === bucketId)?.name ?? "the chapter");
    if (r.ok) pushNotice({ message: `Filed ${row.name} in ${where}.`, level: "success" });
    else pushNotice({ message: `Could not file ${row.name}: ${r.message}`, level: "error" });
    await Promise.all([loadRows(), loadBuckets()]);
  }, [loadRows, loadBuckets, pushNotice]);

  // A page archived from "This page" (ArchiveDialog asked first, and about
  // its Code Repository where that can be honoured): it leaves the tabs for
  // its chapter in the Index, and the book turns to the page after it in the
  // book's order, else the one before, else the empty book.
  const onArchived = useCallback(async (id: number, note: string) => {
    setArchiving(null);
    const row = rowsRef.current.find((p) => p.id === id);
    const order = orderRef.current;
    const at = order.indexOf(id);
    const next = at < 0 ? null : (order[at + 1] ?? order[at - 1] ?? null);
    setRows((cur) => cur.map((p) => (p.id === id ? { ...p, status: "archived", group: "archived" } : p)));
    pushNotice({ message: note || `Archived ${row?.name ?? `project ${id}`}. The Index keeps it in its chapter.`,
                 level: note ? "info" : "success" });
    if (openRef.current === id) {
      if (next != null) goTo(next);
      else {
        // Nothing open is the person's choice: the boot, if it has not ended,
        // neither picks a page nor opens a link's page over it.
        closedAllRef.current = true;
        requestedRef.current = null;
        setOpenId(null);
      }
    }
    await loadRows();
  }, [goTo, loadRows, pushNotice]);

  // Writing on an archived page brings it back first (PUT status active, the
  // same restore the Index's chapters use); a refused restore says why and
  // nothing is sent.
  const restoreBeforeSend = useCallback(async (): Promise<boolean> => {
    const id = openRef.current;
    const row = id == null ? null : rowsRef.current.find((p) => p.id === id);
    if (id == null || row?.status !== "archived") return true;
    const r = await restoreProject(id);
    if (!r.ok) {
      setRestoreRefused({ id, reason: r.message });
      return false;
    }
    setRestoreRefused(null);
    setRows((cur) => cur.map((p) => (p.id === id ? { ...p, status: "active", group: "active" } : p)));
    void loadRows();
    return true;
  }, [loadRows]);

  // ← and → turn the page; / opens the Index; Escape closes the phone's sheet,
  // else the Index (the Index takes its own Escape first: out of a chapter).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (e.key === "Escape" && document.querySelector(".book-desk[data-sheet]")) {
        setSheetOpen(false);
        return;
      }
      if (e.key === "Escape" && indexOpenRef.current && !document.querySelector("[role='dialog']")) {
        closeIndex();
        return;
      }
      const target = e.target instanceof HTMLElement ? e.target : null;
      const field = target?.closest(
        "input, textarea, select, [contenteditable='true'], [contenteditable='']",
      );
      const inTextField = field != null;
      // Shift is not a modifier for "/": it is Shift+7 on a German keyboard
      // and Shift+: on a French one.
      const ctrlLike = e.altKey || e.ctrlKey || e.metaKey;
      const at = {
        modifier: ctrlLike || e.shiftKey,
        dialogOpen: document.querySelector("[role='dialog']") != null,
        inTextField,
        composerEmpty:
          field instanceof HTMLTextAreaElement && field.closest(".book-sheet") != null && field.value === "",
      };
      if (e.key === "/" && !inTextField && !at.dialogOpen && !ctrlLike) {
        e.preventDefault();
        openIndex();
        return;
      }
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (!arrowsTurnPages(at)) return;
      const to = flipNeighbour(orderRef.current, openRef.current, e.key);
      if (to == null) return;
      e.preventDefault();
      // From a tab, the keyboard's focus moves with the page (tabs.tsx).
      if (flipTo(to) && target?.closest(".book-tabs")) focusTab(to);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flipTo, openIndex, closeIndex]);

  // ── What the page shows ────────────────────────────────────────────────
  // The page's latest finished build, read again when it moves and when the
  // page turns (the list then still holds the last page's runs, which are not
  // this page's, until the bench reads this page's).
  const latest = useMemo(
    () => latestBuild(runSessionsRef.current, openId),
    // latestKey is when the runs' answer moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [latestKey, openId],
  );
  const openRow = openId == null ? null : (rows.find((p) => p.id === openId) ?? null);
  const openName = openRow?.name ?? (openId == null ? "" : `project ${openId}`);
  // A page opened from a chapter of the Index, still archived.
  const archived = openRow?.status === "archived";
  const archivedLine = useMemo(
    () => (archived && openRow ? archivedNote(chapterOf(chaptersOf(rows, buckets), openRow)) : null),
    [archived, openRow, rows, buckets],
  );

  // Writing on a page moves it to the top at once; the list read after says
  // where it really is.
  const onActivity = useCallback(
    (id: number | null) => {
      if (id != null) setRows((cur) => touched(cur, id, new Date().toISOString()));
      void loadRows();
    },
    [loadRows],
  );

  // A strand on the right side turns to its engine's card on the page: the
  // phone's sheet closes first, the card scrolls into the
  // middle of the page and glows once. False when the card is not there.
  const jumpToCard = useCallback((sessionId: string, harness: string): boolean => {
    const sel = `[data-run-card][data-session="${CSS.escape(sessionId)}"][data-harness="${CSS.escape(harness)}"]`;
    const card = sheetRef.current?.querySelector<HTMLElement>(sel);
    if (!card) return false;
    setSheetOpen(false);
    const reduced = prefersReducedMotion(window);
    window.requestAnimationFrame(() => {
      scrollWithin(card, "center", !reduced);
      card.removeAttribute("data-flash");
      void card.offsetWidth;
      card.setAttribute("data-flash", "");
      window.setTimeout(() => card.removeAttribute("data-flash"), 1600);
    });
    return true;
  }, []);

  return (
    <TooltipProvider>
      <div className="book-desk" data-book data-phone={phone || undefined} data-sheet={sheetOpen || undefined}>
        <svg aria-hidden className="book-grain" height="100%" width="100%">
          {/* Grey grain, multiplied into the desk: it darkens the desk's own
              colour and adds no hue of its own (book.css .book-grain). */}
          <filter id="book-grain-noise">
            <feTurbulence baseFrequency=".8" numOctaves={3} stitchTiles="stitch" type="fractalNoise" />
            <feColorMatrix values="0 0 0 0 .5  0 0 0 0 .5  0 0 0 0 .5  0 0 0 .18 0" />
          </filter>
          <rect filter="url(#book-grain-noise)" height="100%" width="100%" />
        </svg>
        <div className="book-bar">
          <span className="book-who">
            <b>WORKBENCH</b> · THE BOOK
          </span>
        </div>

        <div className="book-book" ref={bookRef}>
          <div className="book-under" style={{ transform: "translate(9px,7px) rotate(.35deg)" }} />
          <div className="book-under" style={{ transform: "translate(5px,4px) rotate(.18deg)" }} />
          <article
            aria-label={indexOpen ? "Index" : openName || "The book"}
            className="book-sheet"
            data-archived={archived || undefined}
            data-page-id={openId ?? undefined}
            id={BOOK_PAGE_ID}
            ref={sheetRef}
            role="tabpanel"
          >
            <div className="book-shade" />
            <div aria-hidden={indexOpen || undefined} className="book-inner" inert={indexOpen}>
              {openId != null ? (
                <>
                  <PageHead page={pages[openId]} row={openRow} />
                  <h1 className="book-title">{openName}</h1>
                  {archivedLine && (
                    <p className="book-archived" data-archived-note>
                      {archivedLine}
                      {restoreRefused?.id === openId && (
                        <span data-restore-refused>
                          {" "}It could not come back: {restoreRefused.reason}
                        </span>
                      )}
                    </p>
                  )}
                  <Stands
                    error={pageErrors[openId] ?? null}
                    page={pages[openId]}
                    pageId={openId}
                    seenBefore={seenBefore[openId]}
                  />
                  <ChatPane
                    approval={approval}
                    approvalLine={approvalLines[openId] ?? null}
                    className="min-h-0"
                    composerSeed={composerSeed}
                    controlsSlot={controlsSlot}
                    delivery={delivery}
                    firstMessage={firstMessage}
                    onActivity={onActivity}
                    onApprovalClosed={() => setApproval(null)}
                    onApprovalLine={announceApprovalLine}
                    onApprovalLineDone={approvalLineDone}
                    onApprovalsChanged={() => setApprovalsTick((t) => t + 1)}
                    onDeliveryMissed={onDeliveryMissed}
                    onFirstMessageTaken={(nonce) => setFirstMessage((cur) => (cur?.nonce === nonce ? null : cur))}
                    onThreadMoved={(move) => {
                      // The turn's work landed in another project, so the
                      // kernel filed it there (runtime/turn_home.py): follow it
                      // if the person is still on the page it left. They
                      // watched its reply, which lands on that page: it is
                      // marked seen there with the turn, as it would have been
                      // here.
                      pushNotice({
                        title: `Moved to ${move.name ?? `project ${move.to}`}`,
                        message:
                          "That is the project this exchange worked on. The conversation continues there; a note marks where it left.",
                        level: "success",
                      });
                      if (openRef.current === move.from) {
                        flipTo(move.to);
                        void markSeenNow(move.to, move.turnToken ?? null);
                      }
                      void loadRows();
                    }}
                    beforeSend={restoreBeforeSend}
                    onTurnSettled={onTurnSettled}
                    projectId={openId}
                    projectName={openName}
                    reloadNonce={reloadNonce}
                    runningBuilds={runningBuilds}
                  />
                </>
              ) : (
                <div className="book-empty" data-book-empty>
                  {loaded ? (
                    <>
                      <p>
                        No open pages yet. Say what you want to work on, and the
                        Workbench finds the project or starts one.
                      </p>
                      <Button onClick={() => setStartOpen(true)}>
                        <PlayIcon className="size-4" />
                        Start
                      </Button>
                    </>
                  ) : (
                    <p>Opening the book…</p>
                  )}
                </div>
              )}
            </div>
            {indexOpen && (
              <IndexPage
                buckets={buckets}
                dragging={draggingTab}
                onAskPick={askPick}
                onBucketsChanged={() => void Promise.all([loadBuckets(), loadRows()])}
                onChapter={openChapter}
                onClose={closeIndex}
                onFile={(id, bucketId) => void fileInChapter(id, bucketId)}
                onOpen={openFromIndex}
                onPickRead={() => {
                  if (indexPickRef.current?.outcome) setIndexPick(null);
                }}
                onSend={(text) => void sendIndex(text)}
                onStartPage={(text) => void startPage(text)}
                pick={indexPick}
                rows={rows}
              />
            )}
          </article>
        </div>

        <BookTabs
          baselines={baselinesRef.current}
          indexOpen={indexOpen}
          onDragTab={setDraggingTab}
          onIndex={openIndex}
          onOpen={(id) => {
            setSheetOpen(false);
            return flipTo(id);
          }}
          onSheet={() => setSheetOpen((v) => !v)}
          openId={openId}
          phone={phone}
          sheetOpen={sheetOpen}
          tabs={tabs}
          working={working}
        />

        <aside
          aria-label="This page and its work"
          className="book-right"
          data-sheet-open={sheetOpen || undefined}
          id={BOOK_RIGHT_ID}
        >
          {phone && (
            <button className="book-sheet-close" data-sheet-close onClick={() => setSheetOpen(false)} type="button">
              Back to the page
            </button>
          )}
          {indexOpen && (
            <section className="book-sec" data-sec="index">
              <div className="book-k">
                <span>The Index</span>
                <span>every page</span>
              </div>
              <p className="book-ix-about">
                Finds every page, open and archived, by its words and its conversation. With “sends it” on,
                Enter gives your message to the page the Host picks, and the Host writes on that page why.
                “Starts a new page” makes a page of its own for your words instead.
                Chapters hold your pages by topic; drag a tab onto one to file its page there.
              </p>
            </section>
          )}
          <section className="book-sec" data-sec="this-page">
            <div className="book-k">
              <span>This page</span>
              <span>{openName}</span>
            </div>
            {openId != null && <LatestBuildOpen build={latest} />}
            <div ref={setControlsSlot} />
            {openId != null && (
              // The running count here too, split on hover (the plan's "This
              // page"): the same figure as the page's head.
              <div className="book-ctl" data-ctl="count">
                <span>On this page, all turns</span>
                <Tally page={pages[openId]} projectId={openId} />
              </div>
            )}
            {openRow && !archived && (
              // Archiving moves the page into its chapter in the Index; it
              // comes back when written on.
              <div className="book-ctl" data-ctl="archive">
                <span>Done with this page?</span>
                <button className="book-ix-link" data-archive-page onClick={() => setArchiving(openRow)} type="button">
                  Archive it
                </button>
              </div>
            )}
          </section>
          <OpsPane
            approvalsTick={approvalsTick}
            benchSlot={(sessions) =>
              openId != null ? (
                <BookBench key={openId} onJump={jumpToCard} projectId={openId} sessions={sessions} />
              ) : null
            }
            onAgentDelivered={announceDelivery}
            onApprovalAnnounced={announceApprovalLine}
            onOpenApproval={(n) => setApproval(n)}
            onRunningBuilds={setRunningBuilds}
            onSessions={onRunSessions}
            projectId={openId}
          />
          <p className="book-keys">
            <kbd>←</kbd> <kbd>→</kbd> turn the page · <kbd>/</kbd> the Index · hold Send or <kbd>Enter</kbd> to
            think harder
          </p>
        </aside>

        {notices.length > 0 && (
          <div className="book-notices">
            {notices.map((n) => (
              <div
                className={cn(
                  // Opaque: a notice sits over the page's words.
                  "rounded-md border px-3 py-2 text-xs",
                  n.level === "error"
                    ? "border-destructive/50 bg-[color-mix(in_oklab,var(--destructive)_12%,var(--background))] text-destructive"
                    : n.level === "success"
                      ? "border-success/40 bg-[color-mix(in_oklab,var(--success)_12%,var(--background))] text-foreground"
                      : "border-border bg-background text-foreground",
                )}
                key={n.id}
                role="status"
              >
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    {n.title && <p className="truncate font-medium">{n.title}</p>}
                    <p className="break-words">{n.message}</p>
                  </div>
                  <button
                    aria-label="Dismiss"
                    className="shrink-0 opacity-70 hover:opacity-100"
                    onClick={() => setNotices((cur) => cur.filter((x) => x.id !== n.id))}
                    type="button"
                  >
                    <XIcon className="size-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <StartDialog
        onOpenChange={setStartOpen}
        onOpened={(p) => {
          setComposerSeed(p.text ? { text: p.text, nonce: Date.now() } : null);
          if (p.note) pushNotice({ title: `Code Repository for ${p.name}`, message: p.note, level: "info" });
          // The General thread's turns moved into this project: done.
          if (p.adopted) setPendingGeneral(null);
          void loadRows().then(() => flipTo(p.id));
        }}
        open={startOpen}
        pendingGeneral={pendingGeneral}
      />
      <ArchiveDialog
        onArchived={(id, note) => void onArchived(id, note)}
        onClose={() => setArchiving(null)}
        project={archiving}
      />
    </TooltipProvider>
  );
}
