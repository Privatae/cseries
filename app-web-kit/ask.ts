// The ask card — what a Host raises when it needs the person to decide
// something mid-turn: an overlay over the
// chat, not a message in it.
//
// Nothing here is new machinery. The Host asks with the `escalate` tool it
// already has (a decision ticket: sitrep + 1–4 options); the chat client sees
// that tool's call and result on the turn stream it already reads; this file
// turns the pair into an Ask the overlay can show, and turns the person's
// click into the words the Host reads next — as a nudge into the running turn
// (Circles) or as the next message when the turn has ended. The ticket stays
// the durable record: an ask nobody answers is still in Tickets.
//
// Pure functions, no React, no fetch — so `node --test` pins them from the
// app test dirs without a DOM.

export interface AskCredentialRequest {
  name: string;
  cred_type?: string;
  purpose?: string;
}

export interface AskOption {
  id: string;
  label: string;
  consequence?: string;
  recommend?: boolean;
  /** A credential ask is answered in the Tickets pane (the value is sealed
   * into Keys there); the overlay names it and points at Tickets. */
  credential_request?: AskCredentialRequest;
}

export interface AskAttachment {
  /** One line naming what the text is ("What it changes on web-1"). */
  label: string;
  /** Shown folded under the sitrep, as it is (a diff, a change list). */
  text: string;
}

export interface Ask {
  /** The decision ticket behind the card — what the answer is applied to. */
  ticketId: number;
  /** The tool call that raised it (dedupe key when a stream is replayed). */
  toolCallId: string;
  sitrep: string;
  options: AskOption[];
  /** The option that takes a code from the person's authenticator — a
   * pipeline change adding a check that runs outside the platform. Read only
   * from a RESULT_ASK_TOOLS result (the kernel's words), never from an
   * `escalate` the Host wrote: a Host-made card must not ask for the code. */
  stepupOption?: string;
  /** The line under the code box saying what the code is for (stepupAsk);
   * absent = a pipeline change's "adds a check that runs outside the
   * platform". */
  stepupWhy?: string;
  /** What the card shows folded under the words — the dry run's diff for an
   * op on a machine. Read only from a
   * RESULT_ASK_TOOLS result: the platform wrote it from the machine's own
   * report; a Host's `escalate` carries no attachment. */
  attachment?: AskAttachment;
}

interface ToolLike {
  id: string;
  name: string;
  args: unknown;
  result: unknown;
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/** Platform tools whose RESULT carries the ask the platform itself filed —
 * `{ask: {ticket_id, sitrep, options}}`, written by the kernel from validated
 * data (the `pipeline` proposal; `mgmt_run`'s op door: the approval card for
 * an op on a machine — its `ask` is the pod's alone, a box's words are strings inside
 * stdout/stderr; `context_ingest`'s extract: the card for reading a document
 * on a machine — the pod builds that result dict and the machine's
 * answer lands inside its `text`; `turn_type`'s draft / change / retire:
 * the card for a turn type, written by the platform from the validated
 * profile). Named, never "any result with an ask": a tool that returns
 * fetched or user content could otherwise put a crafted sitrep over a real
 * open ticket. */
const RESULT_ASK_TOOLS = new Set(["pipeline", "mgmt_run", "context_ingest", "turn_type"]);

/** An Ask when this tool invocation is an `escalate` that filed an OPEN
 * ticket, or a RESULT_ASK_TOOLS call whose result carries the ask it filed;
 * null for anything else — another tool, an auto-resolved ticket (a learned
 * pattern already answered it), an error, or no options a person could
 * click.
 *
 * A registry op the Host runs through `operate` (session.py's operate
 * branch → direct_ops.run_direct_op) streams under the name "operate" with
 * the op's own name in `args.tool` and the op's own envelope as the result,
 * unwrapped — so the tool is named by `args.tool` there. The Host chooses
 * which tool to run; the result is still the kernel's. */
function opName(t: ToolLike): string {
  if (t.name !== "operate") return t.name;
  const tool = asRecord(t.args)?.tool;
  return typeof tool === "string" ? tool.trim() : "";
}

export function askFromTool(t: ToolLike): Ask | null {
  const envelope = asRecord(t.result);
  if (!envelope || envelope.status !== "ok") return null;
  const res = asRecord(envelope.result);
  if (!res) return null;
  let ticketId: number;
  let src: Record<string, unknown> | null;
  let stepupOption: string | undefined;
  let attachment: AskAttachment | undefined;
  if (t.name === "escalate") {
    if (res.status !== "open" || res.auto_resolved === true) return null;
    ticketId = Number(res.ticket_id);
    src = asRecord(t.args);
  } else if (RESULT_ASK_TOOLS.has(opName(t))) {
    src = asRecord(res.ask);
    ticketId = Number(src?.ticket_id);
    if (typeof src?.stepup_option === "string" && src.stepup_option) {
      stepupOption = src.stepup_option;
    }
    const att = asRecord(src?.attachment);
    if (att && typeof att.text === "string" && att.text) {
      attachment = { label: typeof att.label === "string" ? att.label : "", text: att.text };
    }
  } else {
    return null;
  }
  if (!Number.isFinite(ticketId) || ticketId <= 0) return null;
  if (!src) return null;
  const sitrep = typeof src.sitrep === "string" ? src.sitrep.trim() : "";
  const options = parseOptions(src.options);
  if (!options.length) return null;
  const ask: Ask = { ticketId, toolCallId: t.id, sitrep, options };
  if (stepupOption && options.some((o) => o.id === stepupOption)) ask.stepupOption = stepupOption;
  if (attachment) ask.attachment = attachment;
  return ask;
}

function parseOptions(value: unknown): AskOption[] {
  const raw = Array.isArray(value) ? value : [];
  const options: AskOption[] = [];
  for (const o of raw) {
    const r = asRecord(o);
    if (!r || typeof r.id !== "string" || !r.id) continue;
    const cred = asRecord(r.credential_request);
    options.push({
      id: r.id,
      label: typeof r.label === "string" && r.label ? r.label : r.id,
      consequence: typeof r.consequence === "string" ? r.consequence : undefined,
      recommend: r.recommend === true,
      credential_request:
        cred && typeof cred.name === "string" && cred.name
          ? {
              name: cred.name,
              cred_type: typeof cred.cred_type === "string" ? cred.cred_type : undefined,
              purpose: typeof cred.purpose === "string" ? cred.purpose : undefined,
            }
          : undefined,
    });
  }
  return options;
}

// ── Cards read back from the conversation's tickets ────────────────────────
//
// Tickets render in the chat. The kernel links every ticket it
// files during a turn — and a connector's approval card, filed outside one —
// to that conversation (metadata.asked_in), and GET /api/tickets?asked_in=
// lists them. The chat raises each open one as its card on opening the
// conversation and on its poll, so a card is no longer lost on a reload or a
// thread switch, and a ticket filed outside the turn comes up where it
// belongs.

/** Which option on a ticket takes a code from the person's authenticator,
 * and the line that says why. Read from the kind + payload only the
 * platform's own writers set (POST /api/tickets strips both,
 * server/routes/tickets.py _strip_platform_locks), never from an option the
 * Host wrote.
 *
 *   pipeline_proposal  'apply' adds a check that runs outside the platform
 *                      (the platform's own `pipeline` tool).
 *   connector_pairing  'Let this account in' lets a messaging account start
 *                      turns in the person's chat (server/network/
 *                      nac_connector.py). */
export interface StepupAsk {
  /** The option id that needs the code. */
  option: string;
  /** Shown under the code box. */
  why: string;
}

export function stepupAsk(metadata: unknown): StepupAsk | null {
  const md = asRecord(metadata) ?? {};
  if (md.kind === "pipeline_proposal" && asRecord(md.pipeline_proposal)?.requires_stepup === true) {
    return { option: "apply", why: "This adds a check that runs outside the platform." };
  }
  // A post on the members' board the Host wrote (runtime/board_post_approval):
  // its 'Post it' takes the code (server/routes/tickets.decide_ticket_service).
  if (md.kind === "board_post_proposal") {
    return { option: "post", why: "This puts text on the members' board, under your Host's name." };
  }
  if (md.kind === "connector_pairing") {
    const option = asRecord(md.connector_pairing)?.stepup_option;
    if (typeof option === "string" && option) {
      return {
        option,
        why: "This lets a messaging account start turns in your chat.",
      };
    }
  }
  return null;
}

/** The room key GET /api/tickets?asked_in= takes for the conversation on
 * screen: a Circles thread (`thread:<uuid>`), the original Circles
 * conversation (`thread:original`), or a Workbench project (`project:<id>`). */
export function circlesRoom(threadId: string | null | undefined): string {
  const t = (threadId || "").trim().toLowerCase();
  return !t || t === "original" || t === "self" ? "thread:original" : `thread:${t}`;
}

export function projectRoom(projectId: number): string {
  return `project:${projectId}`;
}

/** The room key a recorded `asked_in` names — the same three as above, or
 * null for a room no chat shows (another app's lane). */
export function roomOf(askedIn: unknown): string | null {
  const r = asRecord(askedIn);
  if (!r) return null;
  const thread = typeof r.thread_id === "string" ? r.thread_id : null;
  if (thread) return circlesRoom(thread);
  const pid = r.project_id;
  if (pid != null && pid !== "" && Number.isFinite(Number(pid))) return projectRoom(Number(pid));
  return r.app_id === "circles" ? "thread:original" : null;
}

/** An Ask from a ticket row the conversation's list returned: the sitrep and
 * options as the kernel stored them, the code box only for a PLATFORM-SET
 * kind (stepupAsk), and each credential ask's own field. Null for anything
 * not open or with nothing a person could click. Never an attachment: that
 * is read only from the platform tool's own result (askFromTool). */
export function askFromTicket(ticket: unknown): Ask | null {
  const t = asRecord(ticket);
  if (!t || t.status !== "open") return null;
  const ticketId = Number(t.id);
  if (!Number.isFinite(ticketId) || ticketId <= 0) return null;
  const options = parseOptions(t.options);
  if (!options.length) return null;
  const sitrep = typeof t.sitrep === "string" ? t.sitrep.trim() : "";
  const ask: Ask = { ticketId, toolCallId: `ticket:${ticketId}`, sitrep, options };
  const st = stepupAsk(t.metadata);
  if (st && options.some((o) => o.id === st.option)) {
    ask.stepupOption = st.option;
    ask.stepupWhy = st.why;
  }
  return ask;
}

/** The asks to raise from a conversation's ticket list, oldest first (the
 * order they were asked in), leaving out any already settled here. */
export function asksFromTickets(tickets: unknown, settled: (ticketId: number) => boolean): Ask[] {
  const rows = Array.isArray(tickets) ? tickets : [];
  const out: Ask[] = [];
  for (const row of rows) {
    const a = askFromTicket(row);
    if (a && !settled(a.ticketId)) out.push(a);
  }
  return out.sort((a, b) => a.ticketId - b.ticketId);
}

/** The tickets of the cards up in a conversation that its open-ticket list
 * did not name. Each may have been answered somewhere else since it came up —
 * in Network, in Tickets, in another window — and clicking it would only get
 * "ticket status is 'decided'". But absence is only a
 * lead: a card the running turn raised after the read started, one whose
 * ticket is linked to no room this list covers, or one past the list's
 * limit is just as absent and still open. So these are read back
 * (closedAsks) before any card goes.
 *
 * `key`: the Workbench keeps every project's cards in one queue, keyed by
 * thread; its list is one project's, so only that thread's cards are asked
 * about. Circles holds one thread's cards and passes none. */
export function unlistedAsks(
  queue: readonly (Ask & { key?: string })[],
  tickets: unknown,
  key?: string,
): number[] {
  const listed = new Set<number>();
  for (const row of Array.isArray(tickets) ? tickets : []) {
    const r = asRecord(row);
    if (r && r.status === "open") listed.add(Number(r.id));
  }
  const out = new Set<number>();
  for (const a of queue) {
    if (key !== undefined && a.key !== key) continue;
    if (!listed.has(a.ticketId)) out.add(a.ticketId);
  }
  return [...out].sort((a, b) => a - b);
}

/** What reading one ticket back gives (GET /api/tickets/{id}: the row, or the
 * HTTP failure). */
export type TicketRead = { ok: true; data: unknown } | { ok: false; status: number };

/** Of `ticketIds`, the ones no longer open, each read back with `read`: a row
 * whose status is not "open", or a 404 (the ticket is gone). A failed read
 * (offline, a 5xx) says nothing about the ticket and keeps its card. */
export async function closedAsks(
  ticketIds: readonly number[],
  read: (ticketId: number) => Promise<TicketRead>,
): Promise<number[]> {
  const closed = await Promise.all(
    ticketIds.map(async (id) => {
      try {
        const r = await read(id);
        return (r.ok ? !ticketIsOpen(r.data) : r.status === 404) ? id : null;
      } catch {
        return null;
      }
    }),
  );
  return closed.filter((id): id is number => id !== null);
}

/** The note a sealed credential ask is decided with: the name it was stored
 * under (never the value), then the person's own words. The Tickets pane
 * writes the same. */
export function sealedNote(name: string, note: string): string {
  const stored = `stored in Keys as "${name}"`;
  const n = (note || "").trim();
  return n ? `${stored}. ${n}` : stored;
}

/** Why a credential could not be stored, for the card. 409 is the vault's
 * if_absent refusal: a key of that name appeared since the ask was raised,
 * and the paste must not replace it. */
export function sealErrorText(status: number, message: string, name: string): string {
  return status === 409
    ? `A key named "${name}" already exists in Keys. Replace it there if you mean to, or ask your Host for a different name.`
    : `Could not store it in Keys: ${status || ""} ${message}`.trim();
}

// ── "Not now", remembered ───────────────────────────────────────────────────
//
// A card set aside stays set aside: the conversation's list would otherwise
// raise the same open ticket again on the next poll or reload. The keys are
// `ticket:<id>` and `pairing:<grant id>`; the list is the newest SET_ASIDE_MAX.

export const SET_ASIDE_MAX = 300;

export function parseSetAside(raw: string | null | undefined): string[] {
  try {
    const v = JSON.parse(raw || "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function withSetAside(raw: string | null | undefined, key: string): string {
  const cur = parseSetAside(raw).filter((k) => k !== key);
  cur.push(key);
  return JSON.stringify(cur.slice(-SET_ASIDE_MAX));
}

// ── The pairing code card ───────────────────────────────────────────────────
//
// When the Host starts a messaging connector's pairing (propose_nac_policy
// kind=connector), the chat shows the one-time code on a card. The tool's
// result carries only the grant id (draft_id): the kernel never puts the code
// where the model can read it. The card fetches the code itself, from the
// route that answers the person's own windows only
// (GET /api/network/nac/connector/{id}/code).

export interface PairingAsk {
  grantId: number;
  platform: string;
  /** The tool call that raised it, or `connector:<id>` when it was read back
   * from the open pairings of this conversation. */
  toolCallId: string;
}

/** A PairingAsk when this tool invocation is a connector pairing the kernel
 * started (`propose_nac_policy` kind=connector, directly or through
 * `operate`); null for anything else. */
export function pairingFromTool(t: ToolLike): PairingAsk | null {
  if (opName(t) !== "propose_nac_policy") return null;
  const envelope = asRecord(t.result);
  if (!envelope || envelope.status !== "ok") return null;
  const res = asRecord(envelope.result);
  if (!res || res.kind !== "connector") return null;
  const grantId = Number(res.draft_id);
  if (!Number.isFinite(grantId) || grantId <= 0) return null;
  return {
    grantId,
    platform: typeof res.platform === "string" ? res.platform : "",
    toolCallId: t.id,
  };
}

/** The open pairing this conversation started, from the person's connector
 * list (GET /api/network/nac/connector): still waiting for its code, the code
 * still held, and asked in `room`. The newest one wins (one pairing per app). */
export function pairingFromConnectors(list: unknown, room: string): PairingAsk | null {
  const rows = Array.isArray(list) ? list : [];
  let best: PairingAsk | null = null;
  for (const row of rows) {
    const r = asRecord(row);
    if (!r || r.status !== "pairing" || r.code_held !== true) continue;
    if (roomOf(r.asked_in) !== room) continue;
    const grantId = Number(r.id);
    if (!Number.isFinite(grantId) || grantId <= 0) continue;
    if (!best || grantId > best.grantId) {
      best = {
        grantId,
        platform: typeof r.platform === "string" ? r.platform : "",
        toolCallId: `connector:${grantId}`,
      };
    }
  }
  return best;
}

const PLATFORM_LABEL: Record<string, string> = {
  telegram: "Telegram",
  discord: "Discord",
  slack: "Slack",
};

/** What the person does with the code. */
export function pairingInstruction(platform: string): string {
  const label = PLATFORM_LABEL[platform] ?? "the messaging app";
  return `Send this code to your bot in a ${label} direct message.`;
}

/** m:ss for the countdown; 0:00 once it has run out. */
export function countdownText(secondsLeft: number): string {
  const s = Math.max(0, Math.floor(secondsLeft));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** A code an authenticator shows: six digits, spaces ignored. */
export function stepupCodeReady(code: string): boolean {
  return /^\d{6}$/.test((code || "").replace(/\s/g, ""));
}

/** One card at a time: a second ask waits behind the first. The same ticket
 * never queues twice (a reattached stream replays its frames). */
export function queueAsk<T extends Ask>(queue: T[], ask: T): T[] {
  if (queue.some((a) => a.ticketId === ask.ticketId)) return queue;
  return [...queue, ask];
}

export function settleAsk<T extends Ask>(queue: T[], ticketId: number): T[] {
  return queue.filter((a) => a.ticketId !== ticketId);
}

/** The words the Host reads after the click — the decision is already on the
 * ticket; this is the person's own act, in the conversation's record. */
export function answerText(ask: Ask, option: AskOption, note: string): string {
  const n = (note || "").trim();
  return `Ticket #${ask.ticketId} — I chose “${option.label}”${n ? `: ${n}` : "."}`;
}

/** Said into a RUNNING turn only, so the Host stops waiting; when the turn has
 * ended there is nobody to tell, and the ticket stays open in Waiting on you. The
 * person's note rides along — "Not now" takes a note the Host reads. */
export function notNowText(ask: Ask, note = ""): string {
  const n = (note || "").trim();
  return `Not now on ticket #${ask.ticketId} — I'll answer it later, from Waiting on you${n ? `: ${n}` : "."}`;
}

/** Whether a ticket read back from the server is still open — the check
 * before a replayed or reloaded stream raises a card for a ticket the person
 * already answered. Anything but a plain "open" is not raised. */
export function ticketIsOpen(ticket: unknown): boolean {
  const t = asRecord(ticket);
  return !!t && t.status === "open";
}
