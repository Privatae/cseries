import { Fragment, memo, useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  CheckIcon,
  PaperclipIcon,
  RotateCcwIcon,
  SquareIcon,
  TriangleAlertIcon,
} from "lucide-react";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
} from "@/components/ai-elements/conversation";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ToolCard } from "@/components/chat/tool-card";
import { RunTerminal } from "@/components/workbench/run-terminal";
import { CompareTerminals } from "@/components/workbench/compare-terminals";
import {
  buildTaskIdFor,
  fanOutSessionIdFor,
  isFanOutChip,
  orphanBuildsFor,
} from "@/lib/fan-out";
import {
  decideTicket,
  fetchHistory,
  fetchPairingCode,
  fetchProjectRepository,
  getEmbedJob,
  listConnectors,
  listRoomTickets,
  mapPersistedToolChip,
  type PersistedToolChip,
  setAutoMerge,
  storeVaultSecret,
  uploadContextFile,
} from "@/lib/api";
import { CodeApprovalCard } from "@/components/workbench/code-approval-card";
import {
  answerText,
  type AskCredentialRequest,
  type AskOption,
  notNowText,
  pairingFromConnectors,
  projectRoom,
  sealErrorText,
} from "@kit/ask";
import { AskOverlay } from "@kit/ui/ask-overlay";
import { PairingCodeOverlay } from "@kit/ui/pairing-code-overlay";
import { CrisisNotice } from "@kit/ui/crisis-notice";
import { ArtifactCard, artifactIdOf, artifactsOfTurn, VisualsAllowed } from "@kit/visuals";
import {
  deliveryIsNamed,
  deliveryLanded,
  deliveryTargetsThread,
  evaluateDeliveryFetch,
  historyKeys,
  isLocalEcho,
} from "@/lib/history-merge";
import {
  applyMemory,
  describeFinding,
  hardFindings,
  keysHint,
  redactSecrets,
  rememberDecision,
  replaceWithKeysRefs,
  scanSecrets,
  secretValue,
  softFindings,
  type Finding,
} from "@/lib/secret-shapes";
import type {
  ApprovalLineEvent,
  DeliveryEvent,
  ProjectRepository,
  RunningBuild,
} from "@/lib/types";
import { threadKey, useWorkbenchChat, type ThreadMove } from "@/hooks/use-workbench-chat";
import { useEffortOptions } from "@/hooks/use-effort-options";
import { PageControls } from "@/components/workbench/page-controls";
import { keepPick } from "@/lib/effort";
import type { ChatMessage, TurnMetrics } from "@/lib/types";
import { parseContextLedger, parseFailedOver, type ContextLedger } from "@/lib/stream";
import { CloseTurnReview, parseCloseTurn } from "@kit/close-turn";
import { cn } from "@/lib/utils";
import { climbLevels, composerHint, pageEffort } from "@kit/book/hold-to-think";
import { replyAdds, spendTokens } from "@/lib/page-count";
import { PressButton, type PressHandle } from "@kit/book/press-button";
import { NudgeLine as BookNudgeLine } from "@kit/book/nudge-line";

/** The thread's newest Host bubble: while a turn runs, its live echo, which
 * carries the turn's token once the session frame stamped it. */
function lastReply(messages: ChatMessage[]): ChatMessage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant") return messages[i];
  }
  return null;
}

/** How often the open project re-reads its own open tickets and raises them
 * as cards: the cadence of the Circles rail's badge poll. */
const POLL_CARDS_MS = 10_000;

/** Whether the composer takes the focus as a thread opens: not from a person
 * turning pages from the book's tabs with the keyboard, whose focus stays on
 * the tab, as the WAI-ARIA tabs pattern has it (src/book/tabs.tsx). A tab
 * clicked holds no keyboard focus (:focus-visible), so a click still hands
 * the composer its focus. */
function composerTakesFocus(): boolean {
  const el = document.activeElement;
  try {
    return !(el instanceof HTMLElement && el.matches('[role="tab"]:focus-visible'));
  } catch {
    return true; // a browser without :focus-visible
  }
}

/**
 * The conversation with the host — the center of the Workbench. Same
 * orchestrator as Circles, workbench-class system prompt (app_id rides the
 * app token). The thread follows the selected project; memory turns are
 * project-tagged server-side, so the host is the same mind with the
 * project's context pulled in.
 *
 * Thread state lives in useWorkbenchChat's per-thread map, so switching
 * projects mid-turn never destroys the live stream — returning shows the
 * turn still streaming (the pane used to go blank when the person navigated
 * away during a build turn and back).
 */
export function ChatPane({
  projectId,
  projectName,
  delivery = null,
  composerSeed = null,
  firstMessage = null,
  onFirstMessageTaken,
  onActivity,
  onDeliveryMissed,
  onThreadMoved,
  runningBuilds = [],
  approval = null,
  onApprovalClosed,
  onApprovalsChanged,
  approvalLine = null,
  onApprovalLine,
  onApprovalLineDone,
  controlsSlot = null,
  onTurnSettled,
  beforeSend,
  reloadNonce,
  className,
}: {
  projectId: number | null;
  projectName: string;
  /** The book's "This page" on its right side: the page names the project
   * above the conversation itself, so the pane has no header, and its effort
   * pick and context panel go here. */
  controlsSlot?: HTMLElement | null;
  /** The shown thread's turn ended (its stream settled): the person watched
   * the reply arrive, and the book marks the page seen, naming the turn by
   * its token (null when its session frame never came) so its reply never
   * lights the page's tab, however late it is written. */
  onTurnSettled?: (projectId: number | null, turnToken: string | null) => void;
  /** The Code Approval to raise over the chat (its number), from the bench's
   * list; null = none open. */
  approval?: number | null;
  onApprovalClosed?: () => void;
  /** An approval merged or was sent back — the bench's list re-reads. */
  onApprovalsChanged?: () => void;
  /** The latest merge whose chat line was written (the card or the bench's
   * list wrote it); the pane fetches until that line is on screen. */
  approvalLine?: ApprovalLineEvent | null;
  /** Raise that wait: the card calls it after the merge it watched. */
  onApprovalLine?: (projectId: number, number: number, written?: boolean) => void;
  /** That wait ended (the line is on screen, or the miss was reported). */
  onApprovalLineDone?: (seq: number) => void;
  /** Pool builds in flight for this project, from the bench poll. A build the
   * HOST started on its own — approved-plan auto-advance — has no `build` tool
   * call in any turn, so buildTaskIdFor can never find it and the conversation
   * showed nothing while it ran ("when a build action is going on it
   * no longer shows the live feed in the chat stream where i can see whats going
   * on, it shows up when its done"). Any of these without a chip of its own gets
   * a live terminal below the thread. */
  runningBuilds?: RunningBuild[];
  /** The latest delivery (socket frame or bench flip). The pane fetches until
   * THAT row is on screen — never merely until the thread grows. */
  delivery?: DeliveryEvent | null;
  /** The delivered row never made it to the screen — say so, visibly. */
  onDeliveryMissed?: (message: string) => void;
  /** Fired when the user sends into a thread: the book orders its pages by
   * most recent activity, so the one being worked moves up (0.13.4). */
  onActivity?: (projectId: number | null) => void;
  /** Text to put in the composer — what the user typed into the Start
   * dialog before picking or creating this project. Seeded, not
   * sent: they press Send. The nonce lets the same text seed twice. */
  composerSeed?: { text: string; nonce: number } | null;
  /** The book only: the words the person typed in the Index's "starts a new
   * page", to go as this page's first message. The pane sends them once it
   * shows that page and has read its conversation, and calls
   * `onFirstMessageTaken` as it does, so the book drops the request and a pane
   * that mounts again never sends them twice. Words holding a secret shape are
   * not sent: they wait in the box, where the composer's own strip decides. */
  firstMessage?: { projectId: number; text: string; nonce: number } | null;
  onFirstMessageTaken?: (nonce: number) => void;
  /** The kernel filed the turn under another project (its work landed there);
   * the pane has already carried the bubbles across — the app follows. */
  onThreadMoved?: (move: ThreadMove) => void;
  /** The book only: run before a message is sent (never before a
   * nudge); false holds the words in the box. An archived page comes back
   * to the book here before anything is written on it. */
  beforeSend?: () => Promise<boolean>;
  /** The book only: a new value reads the conversation again and
   * follows the page's running turn, as opening the page does. A message
   * sent from the Index is written on a page (the Host's note of why it
   * picked it, then the turn) that may be the page already in front. */
  reloadNonce?: number;
  className?: string;
}) {
  const chat = useWorkbenchChat({ onThreadMoved });
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  // The thread whose conversation was last read from the server (or whose
  // read failed): what a first message waits for.
  const [readKey, setReadKey] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  // Held in a ref, not the delivery effect's deps: an inline callback from App
  // would change identity every render and restart the poll mid-wait.
  const missedRef = useRef(onDeliveryMissed);
  missedRef.current = onDeliveryMissed;
  // chat.reconcile identity is stable (useCallback) — safe in deps.
  const { reconcile, threadsRef } = chat;
  const key = threadKey(projectId);
  const thread = chat.threads[key];
  const messages = thread?.messages ?? [];
  // Builds this thread already shows via a `build` tool chip mount their own
  // terminal inside the Turn; only the rest need one here.
  const orphanBuilds = orphanBuildsFor(runningBuilds, messages);
  const status = thread?.status ?? "ready";
  const busy = status === "submitted" || status === "streaming";
  // A turn HOLDING for what it started (its waiting card is up) keeps the
  // composer a Send composer: what the person types goes into that turn.
  // Stop stays beside it.
  // Send becomes Nudge whenever a turn runs: the words reach the running turn
  // without stopping it.
  const nudging = busy;

  // The shown thread's turn ended: busy, then not, on the SAME thread. Moving
  // from a busy thread to an idle one is not an ending. The turn is named by
  // its token, taken while it ran: when it ends, a turn whose work landed in
  // another project has already been carried there (useWorkbenchChat
  // relocateTurn), and its echo with it.
  const settledRef = useRef(onTurnSettled);
  settledRef.current = onTurnSettled;
  const runningTokenRef = useRef<{ key: string; token: string | null }>({ key, token: null });
  useEffect(() => {
    if (!busy) return;
    const reply = lastReply(messages);
    if (reply) runningTokenRef.current = { key, token: reply.turnToken ?? null };
  }, [busy, key, messages]);
  const lastBusyRef = useRef<{ key: string; busy: boolean }>({ key, busy });
  useEffect(() => {
    const prev = lastBusyRef.current;
    lastBusyRef.current = { key, busy };
    if (prev.key === key && prev.busy && !busy) {
      const held = runningTokenRef.current;
      const token = (held.key === key ? held.token : null) ?? lastReply(messages)?.turnToken ?? null;
      settledRef.current?.(projectId, token);
    }
    // `messages` is read for the fallback only: the ending is busy's edge.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, busy, projectId]);

  // The thread's reasoning-effort pick: the Host model's levels, and
  // a pick the model no longer takes (it changed in the Orchestrator) drops
  // back to the default rather than being refused on the next send.
  const effortOptions = useEffortOptions(projectId);
  const effortPick = chat.efforts[key] ?? "";
  const { setEffort } = chat;
  useEffect(() => {
    if (effortPick && effortOptions && !keepPick(effortPick, effortOptions)) {
      setEffort(projectId, "");
    }
  }, [effortOptions, effortPick, projectId, setEffort]);
  // Hold-to-think, on a page of the book (app-web-kit/book/hold-to-think.ts):
  // the page's effort, and the levels a held Send climbs to from it.
  const pageLevel = pageEffort(effortPick, effortOptions);
  const climb = climbLevels(effortOptions, pageLevel);
  const pressRef = useRef<PressHandle | null>(null);
  const hintId = useId();

  // The project's Code Repository: named and linked at the top of the
  // project, and whether the Host may merge there — Auto-Approve,
  // beside the input, is offered only then.
  const [repoInfo, setRepoInfo] = useState<ProjectRepository | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);
  useEffect(() => {
    setRepoInfo(null);
    if (projectId == null) return;
    let alive = true;
    (async () => {
      const r = await fetchProjectRepository(projectId);
      if (alive && r.ok) setRepoInfo(r.data);
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  const loadHistory = useCallback(
    async (sinceISO?: string): Promise<ChatMessage[] | null> => {
      const r = await fetchHistory(projectId, sinceISO);
      if (!r.ok) return null;
      const turns = (r.data.turns ?? [])
        .slice()
        .sort((a, b) =>
          String(a.created_at || "").localeCompare(String(b.created_at || "")),
        );
      const keys = historyKeys(
        turns.map((t) => ({ createdAt: t.created_at, author: t.role, text: t.content })),
      );
      return turns.map((t, i) => ({
          // The turn-row id is the STABLE React key — regenerating ids per
          // fetch (the old localId("h")) remounted the entire thread DOM on
          // every adoption.
          // No-id fallback: built from the row, never echo-shaped
          // (history-merge.historyKeys).
          id: t.id != null ? String(t.id) : keys[i],
          role: t.role,
          text: t.content || "",
          // Rehydrate persisted tool/agent chips (session.py _tc_meta) —
          // cards used to vanish from history on reload.
          tools: Array.isArray(t.metadata?.tool_calls)
            ? (t.metadata.tool_calls as PersistedToolChip[]).map(
                mapPersistedToolChip,
              )
            : [],
          createdAt: t.created_at || "",
          // Delivered-turn identity (session.py `_agent_tags`) — the delivery
          // poll's success test when the trigger carried no row id.
          agentTaskId:
            t.metadata?.task_id != null ? String(t.metadata.task_id) : undefined,
          codeApproval:
            typeof t.metadata?.code_approval === "number" ? t.metadata.code_approval : undefined,
          // The live turn that wrote it (session.py `_turn_tags`) — how the
          // merge swaps this turn's streamed echo for the saved row.
          turnToken:
            typeof t.metadata?.turn_token === "string" ? t.metadata.turn_token : undefined,
          // Words the person sent into a running turn, saved when it read
          // them (session.py's drain) — the merge swaps their echo for it.
          nudge: t.metadata?.nudge === true ? true : undefined,
          // The id the words were nudged with: a reload matches its line to
          // this row by it (lib/pending-sends sameNudge).
          nudgeId:
            t.metadata?.nudge === true && typeof t.metadata?.client_id === "string"
              ? t.metadata.client_id
              : undefined,
          metrics: turnMetrics(t.metadata),
        }));
    },
    [projectId],
  );

  // A Code Approval's merge wrote its line into this project's thread: fetch
  // until THAT line (tagged code_approval = its number) is on screen. Its own
  // wait, never the delivery slot — an unnamed "something landed" there would
  // cancel a named wait for an agent's report. Time spent while a reply
  // streams does not count against the fetches, and a line queued for PII
  // tokenizing is waited for like any other.
  // A wait that ended, shown or reported, is handed back (onApprovalLineDone)
  // and App drops the event, so it never runs again; one cut short by leaving
  // the project runs again on return.
  const lineDoneRef = useRef(onApprovalLineDone);
  lineDoneRef.current = onApprovalLineDone;
  useEffect(() => {
    if (!approvalLine || approvalLine.projectId !== projectId) return;
    const { seq, lines } = approvalLine;
    const missing = (rows: ChatMessage[]) =>
      lines.filter((l) => !rows.some((m) => m.codeApproval === l.number));
    let alive = true;
    (async () => {
      // Only fetches count: while a reply streams the loop only sleeps, for
      // as long as the reply takes.
      let fetches = 0;
      while (alive && fetches < 24) {
        const onScreen = threadsRef.current[key]?.messages ?? [];
        if (missing(onScreen).length === 0) {
          lineDoneRef.current?.(seq);
          return;
        }
        const st = threadsRef.current[key]?.status ?? "ready";
        if (st === "ready" || st === "error") {
          const fetched = await loadHistory();
          if (!alive) return;
          fetches++;
          // Done only once the rows were adopted: a turn that started during
          // the fetch leaves them unadopted, and the next pass fetches again.
          const st2 = threadsRef.current[key]?.status ?? "ready";
          if (fetched && (st2 === "ready" || st2 === "error")) {
            reconcile(key, fetched);
            if (missing([...onScreen, ...fetched]).length === 0) {
              lineDoneRef.current?.(seq);
              return;
            }
          }
        }
        await new Promise((res) => setTimeout(res, 2500));
      }
      if (alive) {
        lineDoneRef.current?.(seq);
        // Only a line known to be written is reported missing; one another
        // writer held may not exist, and its writer raises its own wait.
        const lost = missing(threadsRef.current[key]?.messages ?? []).filter((l) => l.written);
        if (lost.length === 0) return;
        const names = lost.map((l) => `#${l.number}`).join(", ");
        missedRef.current?.(
          lost.length === 1
            ? `Code Approval ${names} was approved, but its line in this thread could not be shown here — reload to see it.`
            : `Code Approvals ${names} were approved, but their lines in this thread could not be shown here — reload to see them.`,
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, [approvalLine, key, projectId, loadHistory, reconcile, threadsRef]);

  // History load on thread switch. Adoption is a MERGE (reconcile): server
  // turn ids are the React keys and unchanged rows keep object identity, so
  // a revisit where nothing changed renders as a no-op instead of remounting
  // the whole thread. Never-wipe and live-stream safety live inside
  // mergeHistory (stale windows ignored, the streaming tail is preserved).
  useEffect(() => {
    let alive = true;
    const cur = threadsRef.current[key];
    if (cur && (cur.status === "submitted" || cur.status === "streaming")) {
      setLoading(false);
      if (composerTakesFocus()) inputRef.current?.focus();
      return;
    }
    setLoading(!cur?.loaded);
    (async () => {
      const history = await loadHistory();
      if (!alive) return;
      if (history) reconcile(key, history);
      setLoading(false);
      setReadKey(key);
      if (composerTakesFocus()) inputRef.current?.focus();
    })();
    return () => {
      alive = false;
    };
  }, [key, loadHistory, reconcile, threadsRef, reloadNonce]);

  // Live delivery: fetch until THE DELIVERED ROW is on screen. The row is
  // named — by entity_memory id (the /ws frame carries the id the kernel just
  // wrote) or by task id (the bench flip) — and that name is the success test.
  //
  // It used to be "the thread grew", and that test was always already
  // satisfied: this client never swaps its own just-streamed turn's optimistic
  // echoes for their server rows, so the user's own last exchange looked NEW
  // to the first delta fetch after any trigger. The bench fires on
  // `delivered_at`, which the kernel stamps BEFORE the report's phrasing model
  // call — so the loop fetched into that gap, saw the user's own turn, called
  // it success and quit: delivered_at stamped, the report row written seven
  // seconds later, and nothing on screen ever after.
  //
  // Intermediate windows are still adopted as they arrive (mergeHistory is
  // never-wipe and stream-safe — that is what finally replaces those echoes),
  // and exhausting the window is REPORTED, never swallowed.
  useEffect(() => {
    if (!delivery) return;
    // Not this thread's row: a named delivery belongs to exactly one thread,
    // so polling here could only time out and then announce that a report
    // "could not be shown" — about one that landed correctly elsewhere. That
    // notice has to be true every time or it is worse than the silence it
    // replaced. Opening the right thread loads it in full.
    if (!deliveryTargetsThread(delivery, projectId)) return;
    let alive = true;
    const { taskId } = delivery;
    const named = deliveryIsNamed(delivery);
    (async () => {
      for (let attempt = 0; attempt < 24 && alive; attempt++) {
        // Never adopt over a live stream: the landed row for the streaming
        // turn would render NEXT TO its own echo until the text settles.
        const st = threadsRef.current[key]?.status ?? "ready";
        if (st === "ready" || st === "error") {
          const cur = threadsRef.current[key]?.messages ?? [];
          if (named && deliveryLanded(cur, delivery)) return;
          // Delta cursor: newest SERVER-BACKED row — an optimistic echo
          // carries the client clock, which can run ahead and hide the row
          // the poll exists to catch. `since` is inclusive; the merge dedups
          // the boundary row by id.
          const backed = cur.filter((m) => !isLocalEcho(m.id));
          const newestISO = backed.length
            ? backed[backed.length - 1].createdAt
            : undefined;
          const fetched = await loadHistory(newestISO || undefined);
          if (!alive) return;
          if (fetched) {
            const { adopt, done } = evaluateDeliveryFetch(cur, fetched, delivery);
            const st2 = threadsRef.current[key]?.status ?? "ready";
            if (adopt && (st2 === "ready" || st2 === "error")) {
              reconcile(key, fetched);
            }
            if (done) return;
          }
        }
        await new Promise((res) => setTimeout(res, 2500));
      }
      if (alive && named) {
        missedRef.current?.(
          `A finished ${taskId ? `task ${taskId}` : "job"} wrote its report into this thread, but it could not be shown here — reload to see it.`,
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, [delivery, key, projectId, loadHistory, reconcile, threadsRef]);

  // The Start dialog's text lands in the composer of the project it opened.
  useEffect(() => {
    if (!composerSeed?.text) return;
    setDraft(composerSeed.text);
    setTimeout(() => inputRef.current?.focus(), 0);
  }, [composerSeed]);

  // Detached-turn reattach: a turn keeps running in the server's
  // turn registry when this tab sleeps or its connection dies. On thread
  // open, tab wake, and network recovery, look for the running turn and
  // reattach its live stream — never a dead chat over a working host.
  const { reattach } = chat;
  useEffect(() => {
    void reattach(projectId);
    const onWake = () => {
      if (document.visibilityState !== "visible") return;
      void reattach(projectId);
    };
    window.addEventListener("visibilitychange", onWake);
    window.addEventListener("online", onWake);
    return () => {
      window.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("online", onWake);
    };
  }, [projectId, reattach, reloadNonce]);

  // Secret shapes (brain/mastercode/secret_shapes.json — the same rules the
  // pod redacts from at the top of send_message). A hit holds the send behind
  // a strip: hard hits (a vendor key, a PEM, a JWT) will be cut by the pod
  // anyway, so the honest offer is "send redacted" or "edit"; soft hits
  // (assignment, entropy) are the user's call. The draft stays in the
  // textarea while held, so "Edit" is just dismissing the strip.
  const [heldFindings, setHeldFindings] = useState<Finding[] | null>(null);
  const [store, setStore] = useState<{ busy: boolean; error: string | null }>({
    busy: false,
    error: null,
  });

  // The level the person held Send to (hold-to-think, on a page of the book),
  // kept for the send the secret strip may hold back first; null: a tap,
  // which sends at the thread's pick.
  const heldLevelRef = useRef<string | null>(null);
  const sendText = (text: string) => {
    setHeldFindings(null);
    setStore({ busy: false, error: null });
    if (nudging) {
      void chat.nudgeTurn(projectId, text).then((outcome) => {
        // The turn ended first (409, or before its id came): the words go as
        // the next message, and the page says so where the nudge line stood.
        // A request that failed says only that: the turn may still run.
        if (outcome === "turn-ended") {
          setNote("That turn ended before it read your words, so they go as your next message.");
        } else if (outcome === "not-sent") {
          setNote("Your words could not be sent into the running turn, so they go as your next message.");
        }
      });
    } else {
      const level = heldLevelRef.current;
      chat.send(projectId, text, level ? { effort: level } : undefined);
    }
    heldLevelRef.current = null;
    setDraft("");
    onActivity?.(projectId);
  };

  // The ask card: the click decides the
  // ticket the Host filed, then the Host reads the person's words as the next
  // message ON THE THREAD THAT ASKED. The decide speaks the answer into the
  // turn that asked while it runs (session.answer_into_turn), and that turn
  // holds for it; from there the server delivers it (answered_into_turn).
  // Only when the decide delivered nothing is a copy held here, sent as a
  // message when the thread is idle — to that thread, whichever project is open by then
  // (one pane serves every project). It goes through chat.send
  // directly, never the composer's sendText, so whatever the person had
  // typed in the box stays there. "Not now" only sets the card aside — the
  // ticket stays open in Tickets.
  const ask = chat.asks.find((a) => a.key === key) ?? null;
  const askWaiting = Math.max(0, chat.asks.filter((a) => a.key === key).length - 1);
  const [askBusy, setAskBusy] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  // An error belongs to the card it was shown on. When that card goes — the
  // poll drops one answered elsewhere, often right after its click failed
  // with "ticket status is 'decided'", or Not now — the
  // next card starts clean.
  const shownAskId = ask?.ticketId ?? null;
  useEffect(() => setAskError(null), [shownAskId]);
  const onAskAnswer = async (option: AskOption, note: string, stepupCode?: string) => {
    if (!ask) return;
    setAskBusy(true);
    setAskError(null);
    const r = await decideTicket(ask.ticketId, option.id, note, stepupCode ?? null);
    if (!r.ok) {
      setAskError(`Couldn’t record that — ${r.message}`.trim());
      setAskBusy(false);
      return;
    }
    chat.settleAsk(ask.ticketId);
    if (r.data.answer_turn_started) {
      // The turn that asked had ended: the kernel started the answer's turn
      // in this thread itself (one path for every surface). Follow it live;
      // never send the answer a second time.
      void chat.reattach(projectId);
    } else if (r.data.answered_into_turn) {
      // The decide put the answer into the turn that asked, and from there
      // it is the server's to deliver: that turn reads it, or the server
      // sends it on as the thread's next turn.
      // Nothing is held here, so it never goes a second time.
    } else {
      // Now if the thread is idle, when it next is otherwise — and held in
      // the hook's module, not here, so the pane going away does not lose
      // it. A copy the running turn reads (its nudge_read receipt) is settled.
      chat.sendWhenIdle(projectId, answerText(ask, option, note));
    }
    onActivity?.(projectId);
    setAskBusy(false);
  };
  const onAskNotNow = (note: string) => {
    if (!ask) return;
    // Set aside, and remembered: the project's ticket list will not raise it
    // again (it stays open in Tickets).
    chat.settleAsk(ask.ticketId, true);
    // A bare Not now sets the card aside and says nothing (nobody is nudged
    // here); a note is the person's words for the Host and goes as the next
    // message ("Not now" takes a note the Host reads).
    if (note.trim()) {
      chat.sendWhenIdle(projectId, notNowText(ask, note));
      onActivity?.(projectId);
    }
  };
  // A credential ask is answered on the card itself, not in Circles ›
  // Tickets: the value goes straight to the
  // vault, if_absent so an existing key is never replaced, and the card then
  // decides the ticket with the key's name only.
  const onSealCredential = async (req: AskCredentialRequest, value: string) => {
    const r = await storeVaultSecret(
      req.name,
      req.cred_type === "api_key" ? "api_key" : "opaque",
      value,
      true,
    );
    return r.ok ? null : sealErrorText(r.status, r.message, req.name);
  };

  // The project's own cards ("tickets should render inchat"): its
  // open tickets — every one the platform filed for a turn here — come up as
  // the ask card on opening the project and every POLL_CARDS_MS; on opening,
  // an open pairing started here brings back its code card.
  const pairing = chat.pairings.find((p) => p.key === key) ?? null;
  const { raiseTickets, raisePairing } = chat;
  const refreshCards = useCallback(
    async (withPairing: boolean) => {
      if (projectId == null) return;
      const k = threadKey(projectId);
      const room = projectRoom(projectId);
      const r = await listRoomTickets(room);
      if (r.ok) raiseTickets(r.data.tickets, k);
      if (withPairing) {
        const c = await listConnectors();
        if (c.ok) raisePairing(pairingFromConnectors(c.data.connectors, room), k);
      }
    },
    [projectId, raiseTickets, raisePairing],
  );
  useEffect(() => {
    void refreshCards(true);
    const id = window.setInterval(() => void refreshCards(false), POLL_CARDS_MS);
    return () => window.clearInterval(id);
  }, [refreshCards]);

  // "Store in Keys": seal every hard hit into the vault (the Keys app's own
  // POST /api/vault/secrets — write:vault, declared in app.yaml), then send
  // the draft with (stored in Keys as "…") where each key was. A vendor key
  // lands on its canonical provider name, so routing picks it up with no
  // rename. One failure stops the whole thing: nothing is sent, the strip
  // says why, and Edit / Send redacted are still there.
  const storeHeld = async (findings: Finding[]) => {
    setStore({ busy: true, error: null });
    const stored: Array<{ f: Finding; name: string }> = [];
    for (const f of hardFindings(findings)) {
      const hint = keysHint(f);
      if (!hint) continue;
      const r = await storeVaultSecret(hint.name, hint.cred_type, secretValue(draft, f));
      if (!r.ok) {
        setStore({ busy: false, error: `Could not store the ${f.label}: ${r.message}` });
        return;
      }
      stored.push({ f, name: r.data.name });
      // Remember it: the same key pasted again is swapped for this
      // reference on sight instead of being held again.
      await rememberDecision(secretValue(draft, f), { kind: "stored", name: r.data.name });
    }
    sendText(replaceWithKeysRefs(draft, stored));
  };

  // "Send as-is" on a soft hit: remember the value so the same string is
  // not held again in this browser.
  const allowAndSend = async (findings: Finding[]) => {
    for (const f of softFindings(findings)) {
      await rememberDecision(secretValue(draft, f), { kind: "allowed" });
    }
    sendText(draft);
  };

  // A quiet line under the composer when memory acted on its own ("Replaced
  // a key you already stored…"). Clears itself.
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    if (!note) return;
    const t = setTimeout(() => setNote(null), 8000);
    return () => clearTimeout(t);
  }, [note]);

  const submit = async (level: string | null = null) => {
    if (!draft.trim() || (busy && !nudging)) return;
    if (!nudging && beforeSend && !(await beforeSend())) return;
    heldLevelRef.current = nudging ? null : level;
    const findings = scanSecrets(draft);
    if (findings.length) {
      // Fingerprint memory first: a key already in Keys becomes its
      // reference, a soft hit already allowed is not asked about again.
      const m = await applyMemory(draft, findings);
      if (m.notes.length) setNote(m.notes.join(" "));
      if (m.remaining.length) {
        if (m.text !== draft) setDraft(m.text);
        setHeldFindings(m.remaining);
        return;
      }
      sendText(m.text);
      return;
    }
    sendText(draft);
  };

  // The book's "starts a new page": the words typed in the Index are this
  // page's first message. They wait until the pane shows the page and has read
  // its conversation, then go as a message, once: the book drops the request
  // as the pane takes it (firstTakenRef holds the same for a request that comes
  // round again). The box, and what the person has typed in it, are not
  // touched. Words that hold a secret shape go to the box instead, where the
  // composer's own strip (submit) decides what is sent.
  const firstTakenRef = useRef<number | null>(null);
  const { send: sendFirst } = chat;
  useEffect(() => {
    if (!firstMessage || firstMessage.projectId !== projectId || readKey !== key) return;
    if (firstTakenRef.current === firstMessage.nonce) return;
    firstTakenRef.current = firstMessage.nonce;
    onFirstMessageTaken?.(firstMessage.nonce);
    if (scanSecrets(firstMessage.text).length) {
      setDraft(firstMessage.text);
      setTimeout(() => inputRef.current?.focus(), 0);
      return;
    }
    void sendFirst(projectId, firstMessage.text);
    onActivity?.(projectId);
  }, [firstMessage, projectId, readKey, key]);

  // Composer upload → embedded project context. Files land in the brain's
  // context intake tagged project:<id> (general thread: untagged), then the
  // strip tracks the per-file embed job so "searchable yet?" is visible.
  const [uploads, setUploads] = useState<UploadRow[]>([]);
  const [dragging, setDragging] = useState(false);
  // dragenter/dragleave fire per child element; a depth count keeps the
  // drop state from flickering as the pointer crosses them.
  const dragDepth = useRef(0);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadSeq = useRef(0);

  const uploadFiles = async (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (!files.length) return;
    const tags = projectId != null ? [`project:${projectId}`] : [];
    await Promise.all(
      files.map(async (f) => {
        const rowId = `u${uploadSeq.current++}`;
        const patch = (p: Partial<UploadRow>) =>
          setUploads((u) => u.map((x) => (x.id === rowId ? { ...x, ...p } : x)));
        setUploads((u) => [
          ...u,
          { id: rowId, name: f.name, phase: "uploading", done: 0, total: 0 },
        ]);
        const r = await uploadContextFile(f, tags);
        if (!r.ok) {
          patch({ phase: "error", note: r.message });
          return;
        }
        if (r.data.error) {
          // A 200 carrying {error} is a refusal (too large, empty): the file
          // did NOT land, and "stored" would be a lie.
          patch({ phase: "error", note: r.data.error });
          return;
        }
        if (r.data.errors?.length) {
          patch({ phase: "error", note: r.data.errors[0] });
          return;
        }
        // A file the host can read (PDF, Office…) was read on upload: its
        // text's embed job is the one to follow.
        const read = r.data.read;
        const jobId = r.data.job_id ?? read?.embed_job_id ?? null;
        if (jobId == null) {
          // Every null-job case says what actually happened: stored and read
          // but not embedded (no embedding model), stored but not readable
          // (and why), or a plain store.
          let note = "stored";
          if (read?.error) note = `stored — not read: ${read.error}`;
          else if (read?.ok && read.chunks)
            note = `read (${read.chunks} part${read.chunks === 1 ? "" : "s"}), NOT embedded — ${
              read.embed_not_queued || "no embed job was queued"}`;
          else if (read?.ok) note = `stored — ${read.note || "no text in it"}`;
          else if (r.data.embed_skipped)
            note = "stored, NOT embedded — configure an embedding model, then re-embed from the Context app";
          patch({ phase: "done", note });
          return;
        }
        patch({ phase: "embedding" });
        // Poll to a TERMINAL state — never guess. The old loop capped at ~5min
        // and then claimed "done" (also on a single failed poll), so big files
        // showed done while embedding was still running, paused, or dead.
        // Backs off 1.5s → 5s after the first minute; a paused job (embedder
        // unreachable, auto-retrying server-side) stays visible as embedding.
        let misses = 0;
        for (let i = 0; ; i++) {
          const j = await getEmbedJob(jobId);
          if (!j.ok) {
            if (++misses >= 4) {
              patch({
                phase: "error",
                note: "lost the embed job's status — check the Context app",
              });
              return;
            }
          } else {
            misses = 0;
            patch({ done: j.data.embedded, total: j.data.total });
            if (["done", "failed", "cancelled"].includes(j.data.status)) {
              patch({
                phase: j.data.failed > 0 ? "error" : "done",
                note: j.data.failed > 0 ? `${j.data.failed} chunks failed` : undefined,
              });
              return;
            }
            patch({
              note:
                j.data.status === "paused"
                  ? "paused — embedder unreachable, retrying"
                  : undefined,
            });
          }
          await new Promise((res) => setTimeout(res, i < 40 ? 1500 : 5000));
        }
      }),
    );
    setTimeout(
      () => setUploads((u) => u.filter((x) => x.phase === "error")),
      5000,
    );
  };

  return (
    <section
      className={cn("relative flex min-w-0 flex-1 flex-col", className)}
      data-chrome="page"
      // Drop files anywhere on the thread: they land in the project's
      // context through the same upload the paperclip uses — stored on the
      // Host, read, embedded, tagged with this project.
      onDragEnter={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) {
          e.preventDefault();
          dragDepth.current += 1;
          setDragging(true);
        }
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDragOver={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
      }}
      onDrop={(e) => {
        if (!e.dataTransfer?.files?.length) return;
        e.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        void uploadFiles(e.dataTransfer.files);
      }}
    >
      {dragging && (
        <div
          aria-live="polite"
          className="pointer-events-none absolute inset-2 z-20 flex items-center justify-center rounded-xl border-2 border-primary border-dashed bg-background/80 font-medium text-sm"
        >
          {projectId != null
            ? `Drop to add to ${projectName} — the host reads it right away`
            : "Drop to add to your context — the host reads it right away"}
        </div>
      )}
      {/* No header: the page names the project above the conversation, and
          the effort pick and context panel go to the book's "This page". */}
      {controlsSlot &&
        createPortal(
          <PageControls
            effort={effortPick}
            frame={thread?.context ?? null}
            messages={messages}
            onEffort={(v) => setEffort(projectId, v)}
            options={effortOptions}
            projectId={projectId}
          />,
          controlsSlot,
        )}

      <Conversation className="min-h-0 flex-1">
        <ConversationContent className="mx-auto w-full max-w-3xl">
          {loading ? (
            <p className="py-8 text-center text-muted-foreground text-sm">
              loading thread…
            </p>
          ) : messages.length === 0 ? (
            <ConversationEmptyState
              description="Say what you want to build. Alignment happens here — the host researches, plans, and dispatches agents and harnesses when it's time to build. Everything it runs shows up on the bench to the right."
              title={
                projectId != null
                  ? `Start the ${projectName} conversation`
                  : "What are we building?"
              }
            />
          ) : (
            messages.map((m, i) => (
              <Turn
                key={m.id}
                m={m}
                onRetry={
                  i === messages.length - 1 && m.error
                    ? () => chat.retry(projectId)
                    : undefined
                }
              />
            ))
          )}
          {/* Builds nobody in this thread asked for by tool call — the chain
              started them. Rendered with the SAME RunTerminal a chip mounts, so
              a hands-off phase looks exactly like one the user kicked off, and
              the host's report lands underneath it when the phase finishes. */}
          {orphanBuilds.map((b) => (
            <OrphanBuildRun b={b} key={b.taskId} />
          ))}
        </ConversationContent>
      </Conversation>

      <footer className="border-border border-t px-4 py-3">
        {heldFindings && (
          <div className="mx-auto mb-2 w-full max-w-3xl">
            <SecretStrip
              findings={heldFindings}
              onEdit={() => {
                setHeldFindings(null);
                inputRef.current?.focus();
              }}
              onSendAsIs={() => void allowAndSend(heldFindings)}
              onSendRedacted={() =>
                sendText(redactSecrets(draft, ["hard", "soft"]).text)
              }
              onStore={() => void storeHeld(heldFindings)}
              store={store}
            />
          </div>
        )}
        {note && !heldFindings && (
          <p className="mx-auto mb-2 w-full max-w-3xl text-muted-foreground text-xs" role="status">
            {note}
          </p>
        )}
        {uploads.length > 0 && (
          <div className="mx-auto mb-2 flex w-full max-w-3xl flex-col gap-1">
            {uploads.map((u) => (
              <UploadStrip key={u.id} u={u} />
            ))}
          </div>
        )}
        {/* The platform's crisis notice: over the page, not in the thread
            (use-workbench-chat raises it from the turn's governance frame). */}
        <CrisisNotice onDismiss={chat.dismissCrisisNotice} open={chat.crisisNotice} />
        {pairing ? (
          // The code card first: the approval card it leads to comes after.
          <PairingCodeOverlay
            fetchCode={fetchPairingCode}
            onClose={(reason) => {
              chat.settlePairing(pairing.grantId, reason === "dismissed");
              // The bot reported the code: its approval card is filed now.
              if (reason === "done") void refreshCards(false);
            }}
            pairing={pairing}
          />
        ) : (
          <AskOverlay
            ask={ask}
            busy={askBusy}
            error={askError}
            onAnswer={onAskAnswer}
            onNotNow={onAskNotNow}
            onSealCredential={onSealCredential}
            waiting={askWaiting}
          />
        )}
        {projectId != null && (
          <CodeApprovalCard
            number={approval}
            onAskChanges={(message) => {
              // The person's words go to the Host as their next message on
              // this thread, as an ask card's answer does.
              chat.sendWhenIdle(projectId, message);
              onActivity?.(projectId);
              onApprovalsChanged?.();
            }}
            onClose={() => onApprovalClosed?.()}
            onMerged={(n, line) => {
              if (line !== "none") onApprovalLine?.(projectId, n, line === "written");
              onApprovalsChanged?.();
            }}
            projectId={projectId}
          />
        )}
        <div className="mx-auto flex w-full max-w-3xl items-end gap-2">
          <input
            className="hidden"
            multiple
            onChange={(e) => {
              uploadFiles(e.target.files);
              e.target.value = "";
            }}
            ref={fileRef}
            type="file"
          />
          <Button
            onClick={() => fileRef.current?.click()}
            size="icon"
            title={
              projectId != null
                ? `Add files to ${projectName} (or drop them here) — the host reads PDFs, Office files and text right away`
                : "Add files to your context (or drop them here) — the host reads PDFs, Office files and text right away"
            }
            variant="ghost"
          >
            <PaperclipIcon className="size-4" />
          </Button>
          <Textarea
            aria-describedby={hintId}
            className="max-h-40 min-h-[44px] flex-1 resize-none"
            onBlur={() => pressRef.current?.cancel()}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                // The Enter that ends an IME composition commits the words; it
                // never sends. Safari reports it with isComposing false and
                // keyCode 229.
                if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
                // Enter held is the press held (hold-to-think), sent on
                // letting go; Enter while a turn runs nudges at once.
                e.preventDefault();
                if (e.repeat) return;
                if (!nudging && climb.length && draft.trim()) pressRef.current?.start();
                else void submit();
              }
            }}
            onKeyUp={(e) => {
              if (e.key === "Enter") pressRef.current?.release();
            }}
            placeholder={nudging ? "Nudge the Host while it works…" : "Write on this page…"}
            ref={inputRef}
            rows={1}
            value={draft}
          />
          {/* Stop while a turn runs, beside a Nudge that sends the words into
              it; otherwise the press (hold-to-think). */}
          <>
            {busy && (
              <Button
                data-stop
                onClick={() => chat.stop(key)}
                size="icon"
                title="Stop the turn"
                variant="ghost"
              >
                <SquareIcon className="size-4" />
              </Button>
            )}
            {nudging ? (
              <Button
                className="book-nudge-btn"
                data-nudge-btn
                disabled={!draft.trim()}
                onClick={() => void submit()}
                title="Your words reach the Host before its next step, without stopping it"
                type="button"
              >
                Nudge
              </Button>
            ) : (
              <PressButton
                climb={climb}
                describedBy={hintId}
                disabled={!draft.trim()}
                level={pageLevel}
                levels={effortOptions?.efforts ?? []}
                onSend={(reached) => void submit(reached)}
                ref={pressRef}
              />
            )}
          </>
        </div>
        <p className="book-hint" data-composer-hint id={hintId}>
          {composerHint({ nudging, page: pageLevel, climb })}
        </p>
        {projectId != null && (repoInfo?.host_can_merge || repoInfo?.auto_approve) && (
          // Auto-Approve: the person's standing permission
          // for the Host to approve, and so merge, its own Code Approvals in
          // this project. Beside the input, not in the bench, because it is a
          // choice about how the conversation's work lands.
          <label className="mx-auto mt-1.5 flex w-full max-w-3xl cursor-pointer items-center gap-2 text-muted-foreground text-xs">
            <input
              checked={repoInfo.auto_approve}
              className="size-3.5 accent-primary"
              // Turning it off is always allowed; on only where the Host can merge.
              disabled={autoBusy || (!repoInfo.host_can_merge && !repoInfo.auto_approve)}
              onChange={async (e) => {
                const next = e.target.checked;
                setAutoBusy(true);
                // Optimistic, then reconciled from the answer — a failed write
                // must not leave the box showing a permission the server is
                // not applying.
                setRepoInfo((cur) => (cur ? { ...cur, auto_approve: next } : cur));
                const r = await setAutoMerge(projectId, next);
                setRepoInfo((cur) =>
                  cur ? { ...cur, auto_approve: r.ok ? Boolean(r.data.auto_merge) : !next } : cur,
                );
                if (!r.ok) setNote(r.message);
                setAutoBusy(false);
              }}
              type="checkbox"
            />
            <span>
              Auto-Approve
              {repoInfo.auto_approve
                ? " — the Host approves and merges its own changes when the plan finishes and every check passes; each one is noted here"
                : " — you approve each change"}
            </span>
          </label>
        )}
      </footer>
    </section>
  );
}

interface UploadRow {
  id: string;
  name: string;
  phase: "uploading" | "embedding" | "done" | "error";
  done: number;
  total: number;
  note?: string;
}

/** The secret-shapes hold, above the composer. Same copy as the Circles
 * composer's strip; the draft is still in the textarea while this shows. */
function SecretStrip({
  findings,
  onEdit,
  onSendAsIs,
  onSendRedacted,
  onStore,
  store,
}: {
  findings: Finding[];
  onEdit: () => void;
  onSendAsIs: () => void;
  onSendRedacted: () => void;
  onStore: () => void;
  store: { busy: boolean; error: string | null };
}) {
  const hard = hardFindings(findings);
  const soft = softFindings(findings);
  const list = (fs: Finding[]) => fs.map(describeFinding).join(", ");
  const storeNames = hard.map((f) => keysHint(f)?.name).filter((n): n is string => !!n);
  const storeLabel =
    storeNames.length === 1 ? `Store in Keys as "${storeNames[0]}"` : `Store ${storeNames.length} in Keys`;
  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs"
      role="alert"
    >
      <div className="flex items-start gap-2">
        <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          {hard.length > 0 ? (
            <p>
              This looks like a credential: <strong>{list(hard)}</strong>. Your
              Host will only see <code>[REDACTED:…]</code> in its place — keys
              belong in the Keys app, not in chat.
            </p>
          ) : (
            <p>
              This might be a secret: <strong>{list(soft)}</strong>. Send it as
              written, or send with that part redacted.
            </p>
          )}
          {hard.length > 0 && soft.length > 0 && (
            <p className="mt-1 text-muted-foreground">
              Also possibly sensitive: {list(soft)} — redacted too if you send
              redacted.
            </p>
          )}
          {storeNames.length > 0 && (
            <p className="mt-1 text-muted-foreground">
              Or seal it into Keys right here — the message then says where it
              went. This replaces any key already stored under that name.
            </p>
          )}
          {store.error && <p className="mt-1 text-destructive">{store.error}</p>}
        </div>
      </div>
      <div className="flex flex-wrap justify-end gap-1.5">
        <Button disabled={store.busy} onClick={onEdit} size="xs" type="button" variant="ghost">
          Edit message
        </Button>
        {storeNames.length > 0 && (
          <Button disabled={store.busy} onClick={onStore} size="xs" type="button" variant="outline">
            {store.busy ? "Sealing…" : storeLabel}
          </Button>
        )}
        {hard.length === 0 && (
          <Button disabled={store.busy} onClick={onSendAsIs} size="xs" type="button" variant="outline">
            Send as-is
          </Button>
        )}
        <Button disabled={store.busy} onClick={onSendRedacted} size="xs" type="button">
          Send redacted
        </Button>
      </div>
    </div>
  );
}

/** One file's upload → embed lifecycle, above the composer. */
function UploadStrip({ u }: { u: UploadRow }) {
  const pct = u.total > 0 ? Math.round((u.done / u.total) * 100) : 0;
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-1.5 text-xs">
      <PaperclipIcon className="size-3 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">{u.name}</span>
      {u.phase === "uploading" && (
        <span className="shrink-0 text-muted-foreground">uploading…</span>
      )}
      {u.phase === "embedding" && (
        <>
          <div className="h-1 w-28 shrink-0 overflow-hidden rounded-full bg-border">
            <div
              className="h-full bg-primary transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className="shrink-0 text-muted-foreground">
            embedding {u.done}/{u.total || "…"}
          </span>
        </>
      )}
      {u.phase === "done" && (
        <span className="flex shrink-0 items-center gap-1 text-muted-foreground">
          <CheckIcon className="size-3" />
          {u.note ?? `embedded ${u.done}/${u.total}`}
        </span>
      )}
      {u.phase === "error" && (
        <span className="flex min-w-0 shrink-0 items-center gap-1 text-destructive">
          <TriangleAlertIcon className="size-3" />
          <span className="max-w-64 truncate">{u.note ?? "failed"}</span>
        </span>
      )}
    </div>
  );
}

/** Pull the per-turn metrics stamp out of a turn row's metadata. */
function turnMetrics(md?: Record<string, unknown>): TurnMetrics | undefined {
  // A row that carries only the provenance stamp (model/provider) is still
  // worth a meta line — the model that answered is never hidden behind a
  // missing gen_ms.
  if (!md || (md.gen_ms == null && !md.model)) return undefined;
  return {
    gen_ms: (md.gen_ms as number) ?? null,
    tokens_total: (md.tokens_total as number) ?? null,
    tokens_in: (md.tokens_in as number) ?? null,
    tokens_out: (md.tokens_out as number) ?? null,
    tokens_cached: (md.tokens_cached as number) ?? null,
    tokens_cache_write: (md.tokens_cache_write as number) ?? null,
    model: (md.model as string) ?? null,
    provider: (md.provider as string) ?? null,
    failed_over: parseFailedOver(md.failed_over),
    context_ledger: parseContextLedger(md.context_ledger),
    close_turn: parseCloseTurn(md.close_turn),
    // What the turn added to its page (the book's per-reply count): the spend
    // record the page's total sums, and the price the row is stamped with.
    spend_tokens: spendTokens(parseContextLedger(md.context_ledger)?.spend),
    est_cost_usd: typeof md.est_cost_usd === "number" && Number.isFinite(md.est_cost_usd) ? md.est_cost_usd : null,
    price_basis: typeof md.price_basis === "string" && md.price_basis ? md.price_basis : null,
    effort_sent:
      md.effort && typeof md.effort === "object" && typeof (md.effort as { sent?: unknown }).sent === "string"
        ? ((md.effort as { sent: string }).sent || null)
        : null,
  };
}

const fmtChars = (n?: number | null): string =>
  n == null ? "?" : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);

/** The context ledger, expanded under a turn: one line per component of the
 * request, one line per model call. Sizes are chars (≈ tokens × 4) for the
 * assembled parts and tokens for the per-call usage — the row says which. */
function ContextLedgerView({ l }: { l: ContextLedger }) {
  const rows: string[] = [];
  const calls = l.calls ?? l.per_call?.length ?? 0;
  if (l.system_chars != null) rows.push(`system prompt · ${fmtChars(l.system_chars)} chars`);
  const pt = l.per_turn_blocks ?? {};
  const ptKeys = Object.keys(pt);
  if (ptKeys.length) {
    rows.push(
      `per-turn blocks · ${ptKeys.map((k) => `${k} ${fmtChars(pt[k])}`).join(", ")}`,
    );
  }
  if (l.history) {
    rows.push(
      `history · ${l.history.turns ?? 0} turns verbatim, ${fmtChars(l.history.chars)} chars${l.history.summary ? " + rolling summary" : ""}`,
    );
  }
  if (l.tools) {
    rows.push(`tool schemas · ${l.tools.count ?? 0} tools, ${fmtChars(l.tools.chars)} chars (sent on every call)`);
  }
  if (l.tool_results?.length) {
    rows.push(
      `tool results · ${l.tool_results.map((r) => `${r.tool ?? "?"} ${fmtChars(r.chars)}`).join(", ")}`,
    );
  }
  if (l.user_chars != null) rows.push(`your message · ${fmtChars(l.user_chars)} chars`);
  const perCall = (l.per_call ?? []).map(
    (c, i) =>
      `call ${i + 1} · ${fmtTokens(c.in ?? 0)} in${c.cached ? ` (${fmtTokens(c.cached)} cached)` : ""} / ${fmtTokens(c.out ?? 0)} out`,
  );
  return (
    <div className="mt-1 rounded-md border border-border/60 bg-muted/30 px-2 py-1.5 text-[11px] leading-5 text-muted-foreground">
      <div className="font-medium text-foreground/80">
        Context used · {calls} model call{calls === 1 ? "" : "s"}
        {calls > 1 ? " (the token total above is the sum across calls)" : ""}
      </div>
      {rows.map((r) => (
        <div key={r}>{r}</div>
      ))}
      {perCall.length > 0 && <div className="mt-1">{perCall.join(" · ")}</div>}
    </div>
  );
}

function fmtClock(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return sameDay
    ? time
    : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/** The quiet meta line under a turn: time · model · provider · took Ns · N tok.
 * Provenance is PRINTED, never tucked into a hover title — the model that
 * answered is the first thing anyone auditing a reply needs to know. */
function TurnMeta({ m }: { m: ChatMessage }) {
  // A reply says what it added to the page's running count, "+21.4k tokens",
  // and its price (lib/page-count.ts replyAdds); the person's message held to
  // think harder says the level it went at.
  const clock = fmtClock(m.createdAt);
  const mt = m.metrics;
  const bits: string[] = [];
  if (clock) bits.push(clock);
  if (m.role === "user" && m.heldEffort) bits.push(`held · ${m.heldEffort}`);
  const adds = m.role === "assistant" ? replyAdds(mt) : null;
  let failedOverNote: string | null = null;
  if (m.role === "assistant" && mt) {
    if (mt.model) {
      bits.push(mt.provider && mt.provider !== mt.model ? `${mt.model} · ${mt.provider}` : mt.model);
    } else if (mt.provider) {
      bits.push(mt.provider);
    }
    if (mt.gen_ms != null) bits.push(`took ${fmtDuration(mt.gen_ms)}`);
    if (mt.effort_sent) bits.push(`thought at ${mt.effort_sent}`);
    const tok = mt.tokens_total ?? ((mt.tokens_in ?? 0) + (mt.tokens_out ?? 0) || null);
    if (adds?.tokens) {
      // What this reply added to the page's count, with the in/out split, so
      // the cached share stays readable: the cached tokens are part of "in",
      // and the line says so.
      const cached = mt.tokens_cached ?? 0;
      const cachedBit = cached > 0 ? `, ${fmtTokens(cached)} of it cached` : "";
      const split =
        mt.tokens_in != null && mt.tokens_out != null
          ? ` (${fmtTokens(mt.tokens_in)} in${cachedBit} / ${fmtTokens(mt.tokens_out)} out)`
          : "";
      bits.push(`${adds.tokens}${split}`);
    } else if (tok != null && tok > 0) {
      // tokens_in is the TRUE context size (cached + uncached); the cached
      // figure shows how much of it the cache served. Without it the line
      // cannot distinguish what was actually consumed from what cost almost
      // nothing — which on a subscription is the difference between burning a
      // refresh window and coasting through it. Circles has shown this since
      // the split existed; the Workbench dropped it at four separate points.
      // Rows with no split (older turns, lanes that don't report it) fall
      // back to the plain in/out form rather than printing a false 0 cached.
      const cached = mt.tokens_cached ?? 0;
      const cachedBit = cached > 0 ? ` · ${fmtTokens(cached)} cached` : "";
      const split =
        mt.tokens_in != null && mt.tokens_out != null
          ? ` (${fmtTokens(mt.tokens_in)} in${cachedBit} / ${fmtTokens(mt.tokens_out)} out)`
          : "";
      bits.push(`${fmtTokens(tok)} tok${split}`);
    }
    if (adds?.price) bits.push(adds.price);
    const fo = mt.failed_over;
    if (fo && (fo.from_provider || fo.from_model)) {
      const from = [fo.from_model, fo.from_provider].filter(Boolean).join(" · ");
      failedOverNote = `failed over from ${from}${fo.primary_error ? ` — ${fo.primary_error}` : ""}`;
    }
  }
  const ledger = m.role === "assistant" ? mt?.context_ledger ?? null : null;
  const review = m.role === "assistant" ? mt?.close_turn ?? null : null;
  const [showLedger, setShowLedger] = useState(false);
  const [showReview, setShowReview] = useState(false);
  if (bits.length === 0) return null;
  return (
    <div
      className={cn(
        "mt-0.5 text-[11px] text-muted-foreground/70",
        m.role === "user" ? "text-right" : "text-left",
      )}
      data-turn-adds={adds?.tokens ?? undefined}
      data-turn-price={adds?.price ?? undefined}
    >
      {bits.join(" · ")}
      {failedOverNote && (
        <span className="ml-1 text-warning" title={failedOverNote}>
          · {failedOverNote.length > 90 ? failedOverNote.slice(0, 90) + "…" : failedOverNote}
        </span>
      )}
      {ledger && (
        <>
          {" · "}
          <button
            type="button"
            className="underline decoration-dotted underline-offset-2 hover:text-foreground"
            onClick={() => setShowLedger((v) => !v)}
            aria-expanded={showLedger}
          >
            {showLedger ? "hide context" : "context"}
          </button>
          {showLedger && <ContextLedgerView l={ledger} />}
        </>
      )}
      {review && (
        <>
          {" · "}
          <button
            type="button"
            className="underline decoration-dotted underline-offset-2 hover:text-foreground"
            onClick={() => setShowReview((v) => !v)}
            aria-expanded={showReview}
          >
            {showReview ? "hide review" : "review"}
          </button>
          {showReview && <CloseTurnReview s={review} />}
        </>
      )}
    </div>
  );
}

/** A build with no tool chip in this thread — the approved-plan chain started
 * it, so no turn ever called `build` and buildTaskIdFor has nothing to find.
 *
 * Rendered with the SAME RunTerminal a chip mounts (the session id is the
 * build's billing key by construction, tools._run_phase_build
 * billing_key=f"factory-build-{task_id}"), so a hands-off phase streams exactly
 * like one the user asked for. Deliberately NOT a synthetic tool chip: putting
 * a tool call in the transcript that the model never made would corrupt the
 * history the next turn is built from.
 *
 * The caption states WHAT is running and nothing about WHY. It used to say
 * "started automatically to continue the approved plan", which this component
 * cannot actually know: the registry row carries the origin, the client does
 * not, and in the seconds before a user's own `build` call returns its task_id
 * the bench poll surfaces that build here too — so a user-started build was
 * briefly labelled automatic. A caption that can be wrong is worse than a
 * caption that is merely brief.
 */
function OrphanBuildRun({ b }: { b: RunningBuild }) {
  return (
    <div className="mx-auto w-full max-w-3xl">
      <p className="mb-1 text-muted-foreground text-xs">
        {b.title}
        {b.harness ? ` · on ${b.harness}` : ""} — running now
      </p>
      <RunTerminal
        harness={b.harness}
        sessionId={`factory-build-${b.taskId}`}
        startOpen
      />
    </div>
  );
}


/** Memoized: a streaming frame or a composer keystroke re-renders ONLY the
 * turn whose props changed, not all 60. History objects keep their identity
 * across adoptions (mergeHistory), so the memo actually holds. */
const Turn = memo(function Turn({
  m,
  onRetry,
}: {
  m: ChatMessage;
  onRetry?: () => void;
}) {
  // Words sent into a running turn are their own line (the mockup's
  // "NUDGE · 21:41"), not another message.
  if (m.role === "user" && m.nudge) return <NudgeLine m={m} />;
  // Every assistant turn in a Workbench thread is the user's OWN Host — the
  // thread is this Host's project memory, with no peers or people in it — so
  // its visuals run and its artifact ids name artifacts in this Host's store.
  const ownHost = m.role === "assistant";
  // The artifacts this turn made or changed, each shown once as the page;
  // rev = calls finished on it, so it redraws as writes land.
  const artifacts = ownHost
    ? artifactsOfTurn(m.tools).map((id) => ({
        id,
        rev: m.tools.filter((t) => t.state === "output-available" && artifactIdOf(t) === id).length,
      }))
    : [];
  return (
    <VisualsAllowed.Provider value={ownHost}>
    <Message from={m.role}>
      <MessageContent>
        {m.reasoning && (
          <ReasoningFeed live={!!m.streaming && !m.text} text={m.reasoning} />
        )}
        {m.tools.length > 0 && (
          <div className="mb-1 flex flex-col gap-1">
            {m.tools.map((t) => {
              const buildTask = buildTaskIdFor(t);
              // A compare chip that errored has no session to show; one that
              // ran mounts n terminals (live result, persisted session id,
              // or the registry row by task id as the last resort).
              const fanOut =
                isFanOutChip(t) && t.state !== "output-error"
                  ? { sessionId: fanOutSessionIdFor(t), taskId: t.taskId }
                  : null;
              return (
                <Fragment key={t.id}>
                  <ToolCard tool={t} />
                  {buildTask != null && (
                    // The run surface: a build's
                    // live terminal mounts INLINE under its tool chip — the
                    // session id is the build's billing key by construction
                    // (tools._run_phase_build billing_key=f"factory-build-
                    // {task_id}"; review.run_single_harness_build session_id
                    // = billing_key).
                    <RunTerminal sessionId={`factory-build-${buildTask}`} />
                  )}
                  {fanOut != null && (fanOut.sessionId || fanOut.taskId) && (
                    // n harnesses → n terminals + n "open app" (0.13.3).
                    <CompareTerminals sessionId={fanOut.sessionId} taskId={fanOut.taskId} />
                  )}
                </Fragment>
              );
            })}
          </div>
        )}
        {artifacts.map((a) => (
          <ArtifactCard id={a.id} key={`artifact-${a.id}`} rev={a.rev} />
        ))}
        {m.text ? (
          // isAnimating is what makes Streamdown flag a fence that is still
          // open (isIncomplete is computed only while animating), so a
          // visual is not run half-written.
          <MessageResponse isAnimating={!!m.streaming}>{m.text}</MessageResponse>
        ) : m.streaming && !m.reasoning && m.tools.length === 0 ? (
          <WorkingLine since={m.createdAt} />
        ) : null}
        {m.error && (
          <div className="mt-1 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-destructive text-sm">
            <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
            <span className="min-w-0 flex-1 break-words">{m.error}</span>
            {onRetry && (
              <Button
                className="h-6 shrink-0 gap-1 px-2 text-xs"
                onClick={onRetry}
                size="sm"
                variant="outline"
              >
                <RotateCcwIcon className="size-3" />
                Retry
              </Button>
            )}
          </div>
        )}
        {!m.streaming && <TurnMeta m={m} />}
      </MessageContent>
    </Message>
    </VisualsAllowed.Provider>
  );
});

/** Words the person sent into a running turn, on a page of the book: the
 * book's nudge line (app-web-kit/book/nudge-line.tsx). The same line whether
 * it is the echo of a nudge just sent, one a reload's replay drew (the
 * registry's `nudge` frame), or the user row the turn saved when it read it
 * (metadata.nudge, session.py's drain). */
function NudgeLine({ m }: { m: ChatMessage }) {
  // Which side delivers the words: the turn read them (its receipt, or the
  // row it saved when it did), or, until then, they wait; words the turn
  // never reads give way to the next message's own line.
  const read = !!m.nudgeRead || !isLocalEcho(m.id);
  return <BookNudgeLine clock={fmtClock(m.createdAt)} read={read} text={m.text} />;
}

/** Live "working" line with a ticking clock — never bare dots with nothing
 * behind them. Shown only for the brief window before the
 * first reasoning/tool/text frame; the reasoning feed takes over from there. */
function WorkingLine({ since }: { since: string }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const started = new Date(since).getTime();
  const s = Number.isNaN(started)
    ? 0
    : Math.max(0, Math.round((Date.now() - started) / 1000));
  return (
    <div className="flex items-center gap-1.5 py-1 text-muted-foreground text-sm">
      <span className="size-1.5 animate-pulse rounded-full bg-primary [animation-delay:-0.2s]" />
      <span className="size-1.5 animate-pulse rounded-full bg-primary [animation-delay:-0.1s]" />
      <span className="size-1.5 animate-pulse rounded-full bg-primary" />
      <span className="text-xs">working · {s}s</span>
    </div>
  );
}

/** The host's live thinking (`g:` frames), streamed into the bubble as it
 * happens. Auto-open while reasoning, auto-collapse once the reply starts;
 * the user can toggle either way. Live-only — history doesn't carry it. */
function ReasoningFeed({ text, live }: { text: string; live: boolean }) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null); // null = auto
  const open = userOpen ?? live;
  const boxRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (open && live && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [text, open, live]);
  return (
    <div className="mb-1">
      <button
        className="flex items-center gap-1.5 text-muted-foreground text-xs hover:text-foreground"
        onClick={() => setUserOpen(!open)}
        type="button"
      >
        <span aria-hidden>∴</span>
        <span>{live ? "Reasoning…" : "Reasoned"}</span>
        <span aria-hidden className="text-[10px]">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div
          className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-border border-dashed bg-muted/30 px-3 py-2 text-muted-foreground text-xs"
          ref={boxRef}
        >
          {text}
        </div>
      )}
    </div>
  );
}
