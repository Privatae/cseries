// Typed Circles API client. Every call returns a discriminated result so the
// UI can render a real error state instead of a silent empty/"Loading…".
//
// Auth is same-origin cookie: the app window carries the launch token that
// applications.launch_app mints, so plain same-origin fetch is authorized.
// Endpoints + response shapes verified against the live route handlers.

import type { EffortOptions } from "./effort";
import type {
  ActiveTurn,
  AttachmentBlock,
  CarryRef,
  CatalogModel,
  ChatMessage,
  Contact,
  InboxMessage,
  Marker,
  ModelPick,
  PolicyRule,
  Posture,
  SharedItem,
  SupportBadges,
  SupportCategory,
  SupportTicket,
  Thread,
  Ticket,
  ToolInvocation,
  ToolState,
  TurnType,
} from "./types";

// Shared client core — see app-web-kit/api.ts (ApiResult, the 401-retry
// request loop, getJSON). Endpoint functions below stay app-local.
export type { ApiResult } from "@kit/api";
import { getJSON, request, type ApiResult } from "@kit/api";
import type { FeedTopic } from "./types";
import { historyKeys } from "./history-merge";

function qs(params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ============================================================================
// Conversations + chat
// ============================================================================

export interface ToolCallRaw {
  toolCallId?: string;
  id?: string;
  toolName?: string;
  name?: string;
  /** Persisted-chip name key (session.py _tc_meta writes `tool`). */
  tool?: string;
  args?: unknown;
  input?: unknown;
  /** Persisted-chip one-line args summary (session.py _tc_meta). */
  args_summary?: string;
  /** Persisted-chip op/action the tool ran (session.py _tc_meta `op`). */
  op?: string;
  result?: unknown;
  output?: unknown;
  status?: string;
  /** Persisted-chip error detail (session.py _tc_meta). */
  error?: string | null;
  /** "agent" when this chip is a dispatch (persisted by session.py _tc_meta). */
  kind?: string;
  /** Dispatched agent's task id (kind === "agent"). */
  task_id?: string | number;
  /** Persisted-chip inner tool of an `operate` call (session.py tool_call_chip). */
  operate_tool?: string;
  /** Persisted-chip result record (runtime/tool_trail.py digest_tool_action). */
  digest?: Record<string, unknown>;
}

/** Agent attribution on a delivered turn (routes/circles.py, origin='agent'). */
export interface AgentRaw {
  task_id?: string | number;
  title?: string;
  status?: string;
  cost_usd?: number | null;
}

export interface ThreadMessageRaw {
  id?: string;
  author_kind?: string;
  author_name?: string;
  /** The host that carried a person's message (Peers/People threads). */
  via?: string | null;
  /** A host's message naming a person this Host could not verify: the
   * address it named (routes/circles.py _read_entity_thread). */
  claimed_person?: string | null;
  body?: string;
  created_at?: string;
  tool_calls?: ToolCallRaw[];
  agent?: AgentRaw;
  /** The live turn that wrote the row (_normalize_turn_rows). */
  turn_token?: string;
  /** Words the person sent into a running turn, saved when it read them. */
  nudge?: boolean;
  /** Ask Ivy: the carried message the turn's user row records. */
  carry?: CarryRef | CarryRef[];
  /** A person's chat: a reading notice's value. */
  reading_notice?: string;
  /** Another Host's conversation: the topic the message is in. */
  topic_id?: string | null;
  /** Your Host's one conversation (GET /api/circles/feed): the topic the row
   * was said in, and whether it is the Host's topic-switch line. */
  feed_topic?: FeedTopic;
  topic_marker?: boolean;
  /** Per-turn metrics surfaced by _normalize_turn_rows (assistant rows). */
  metrics?: {
    gen_ms?: number | null;
    tokens_total?: number | null;
    tokens_in?: number | null;
    tokens_out?: number | null;
    tokens_cached?: number | null;
    tokens_cache_write?: number | null;
    model?: string | null;
    provider?: string | null;
    failed_over?: {
      from_provider?: string | null;
      from_model?: string | null;
      to_provider?: string | null;
      to_model?: string | null;
      primary_error?: string | null;
    } | null;
    /** Price; absent on rows written before the stamp. */
    est_cost_usd?: number | null;
    price_basis?: string | null;
    close_turn?: import("@kit/close-turn").CloseTurnSignal | null;
  };
}

// ---- Dispatched agents (spawned by the host) — list + veto (kill) ----
// The host's `dispatch` spawns a background scoped agent. GET /api/agents lists
// them (live status + est cost); POST /api/agents/{id}/cancel is the veto
// (server/routes/agents.py). Circles shows both on the agent card so no
// dispatched agent runs blind or un-killable.
export interface AgentActivity {
  at?: string;
  event?: string; // start | result
  tool?: string;
  status?: string;
}

export interface AgentRow {
  task_id: number | string;
  title?: string;
  // queued | in_progress | running | completed | failed | timed_out | cancelled
  status: string;
  est_cost_usd?: number | null;
  /** The agent's final output / terminal failure — rendered in the chat card
   * so a finished agent is never a black box. */
  result?: string | null;
  error?: string | null;
  /** Rolling live tool-by-tool feed the registry persists while it runs. */
  activity?: AgentActivity[];
}

/** One internal thread the Host runs on its own (GET /api/circles/work):
 * its work item's goal, status and current step. */
export interface WorkThread {
  run_id: number;
  project_id: number;
  item_id: number;
  thread: number;
  of: number;
  goal: string;
  status: "open" | "working" | "blocked" | "done";
  step: string | null;
  updated_at: string | null;
  /** The registry lane its turns run on: a running turn's thread_key ends
   * with `|<lane>`. */
  lane: string;
}

/** The internal threads of every running run of work. */
export function fetchWork(): Promise<ApiResult<{ threads: WorkThread[] }>> {
  return getJSON<{ threads: WorkThread[] }>("/api/circles/work");
}

export function fetchAgents(
  activeOnly = false,
): Promise<ApiResult<{ agents: AgentRow[] }>> {
  // limit=100 (route default is 20): the newest-20 window silently dropped
  // older still-running agents, freezing their status cards forever.
  return getJSON<{ agents: AgentRow[] }>(
    `/api/agents?limit=100${activeOnly ? "&active_only=true" : ""}`,
  );
}

export function cancelAgent(
  taskId: string | number,
): Promise<ApiResult<{ task_id: number; status: string }>> {
  return request<{ task_id: number; status: string }>(
    "POST",
    `/api/agents/${encodeURIComponent(String(taskId))}/cancel`,
  );
}

// ---- Conversation depth (GET/PUT /api/circles/thread-context/{kind}/{id}) ----
// What the next turn ships with: the model's window, the thread's token
// budget, the consolidator stamps, and a preview of the assembled context
// (blocks, history window, rolling summary, prefired memories). 404 on older
// backends — callers degrade to stream-frame + turn-metrics data only.

export interface ThreadContextBlock {
  name: string;
  chars: number;
}

export interface ThreadContextHistoryRow {
  role: string;
  excerpt: string;
}

/** Why the prompt-block composition and the pre-fired list are empty, when
 * they are. The backend decides it (context_budget.build_thread_context); the
 * panel only renders it.
 *  - "thread"         this thread's own last build is what is reported.
 *  - "no_build_yet"   nothing has been built for this thread yet; one turn
 *                     fills it in.
 *  - "not_built_here" a peer thread. The other host builds that prompt, so
 *                     there is nothing here to report, and never will be. */
export type PrefireState = "thread" | "no_build_yet" | "not_built_here";
/** Which unit the per-turn allowance is actually enforced in. "usd" when the
 * routed model publishes per-token rates; "tokens" when it does not (a
 * subscription plan, a local model, or one missing from the catalog) and the
 * allowance falls back to a multiple of the context budget; "none" when no
 * allowance is in force at all. */
export type AllowanceBasis = "usd" | "tokens" | "none";

export interface ThreadLastTurn {
  calls: number | null;
  spent_usd: number | null;
  /** Some calls reported no usage, so spent_usd covers only the rest
   * (context_budget.last_turn_spend). */
  spent_partial: boolean;
  /** Set when a mid-turn failover moved the unit the turn was metered in
   * (e.g. "usd" — it started in dollars and finished in tokens). */
  basis_changed_from: string | null;
  tokens: {
    uncached: number;
    cache_read: number;
    cache_write: number;
    output: number;
  };
}

export interface ThreadContextInfo {
  window: number | null;
  budget: number | null;
  summary_version: number | null;
  consolidated_at: string | null;
  blocks: ThreadContextBlock[];
  history_preview: ThreadContextHistoryRow[];
  summary_excerpt: string | null;
  /** The thread's goal and the last turn's handoff, as the next turn gets
   * them (consolidator off). null = nothing to say, or the loop runs. */
  thread_state: string | null;
  last_prefired: string[];
  prefire_state: PrefireState;
  /** What the person set for one turn, in dollars. null = no budget set. */
  turn_allowance_usd: number | null;
  allowance_basis: AllowanceBasis;
  /** The token figure actually enforced when the basis is "tokens". */
  allowance_tokens: number | null;
  /** What the next turn's first call is projected to cost at today's rates. */
  projected_next_turn_usd: number | null;
  last_turn: ThreadLastTurn | null;
  /** The knowledge table of contents, when this backend sends
   * one. Rendered generically — every item carries an `id`; unknown keys are
   * ignored so shape drift here can't break the panel. */
  toc: Record<string, unknown> | null;
}

function numOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function normalizeLastTurn(v: unknown): ThreadLastTurn | null {
  if (!v || typeof v !== "object") return null;
  const r = v as Record<string, unknown>;
  const t = (r.tokens ?? {}) as Record<string, unknown>;
  return {
    calls: numOrNull(r.calls),
    spent_usd: numOrNull(r.spent_usd),
    spent_partial: r.spent_partial === true,
    basis_changed_from:
      typeof r.basis_changed_from === "string" ? r.basis_changed_from : null,
    tokens: {
      uncached: numOrNull(t.uncached) ?? 0,
      cache_read: numOrNull(t.cache_read) ?? 0,
      cache_write: numOrNull(t.cache_write) ?? 0,
      output: numOrNull(t.output) ?? 0,
    },
  };
}

/** An older backend sends no basis at all — that reads as "no allowance in
 * force", which is exactly what an older backend means. */
function normalizeBasis(v: unknown): AllowanceBasis {
  return v === "usd" || v === "tokens" ? v : "none";
}

/** Tolerant normalizer: any missing key degrades to null/[], never throws. */
function normalizeThreadContext(raw: unknown): ThreadContextInfo {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    window: numOrNull(r.window),
    budget: numOrNull(r.budget),
    summary_version: numOrNull(r.summary_version),
    consolidated_at:
      typeof r.consolidated_at === "string" ? r.consolidated_at : null,
    blocks: Array.isArray(r.blocks)
      ? r.blocks.map((b) => ({
          name: String((b as ThreadContextBlock)?.name ?? "block"),
          chars: numOrNull((b as ThreadContextBlock)?.chars) ?? 0,
        }))
      : [],
    history_preview: Array.isArray(r.history_preview)
      ? r.history_preview.map((h) => ({
          role: String((h as ThreadContextHistoryRow)?.role ?? ""),
          excerpt: String((h as ThreadContextHistoryRow)?.excerpt ?? ""),
        }))
      : [],
    summary_excerpt:
      typeof r.summary_excerpt === "string" ? r.summary_excerpt : null,
    thread_state:
      typeof r.thread_state === "string" ? r.thread_state : null,
    last_prefired: Array.isArray(r.last_prefired)
      ? r.last_prefired.map(String)
      : [],
    // Missing or unrecognised falls back to "thread", i.e. render exactly as
    // before. An older backend does not send this key at all, and defaulting
    // it to one of the empty states would hide that host's real blocks behind
    // an explanation that is not true of it.
    prefire_state:
      r.prefire_state === "no_build_yet" || r.prefire_state === "not_built_here"
        ? r.prefire_state
        : "thread",
    turn_allowance_usd: numOrNull(r.turn_allowance_usd),
    allowance_basis: normalizeBasis(r.allowance_basis),
    allowance_tokens: numOrNull(r.allowance_tokens),
    projected_next_turn_usd: numOrNull(r.projected_next_turn_usd),
    last_turn: normalizeLastTurn(r.last_turn),
    toc:
      r.toc && typeof r.toc === "object"
        ? (r.toc as Record<string, unknown>)
        : null,
  };
}

export async function fetchThreadContext(
  kind: string,
  id: string,
): Promise<ApiResult<ThreadContextInfo>> {
  const r = await getJSON<unknown>(
    `/api/circles/thread-context/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
  );
  if (!r.ok) return r;
  return { ok: true, data: normalizeThreadContext(r.data) };
}

/** Set (or clear, budget=null → server-derived default) the thread's token
 * budget. The depth bar's marker and the next turn's trim follow it. */
export function putThreadContextBudget(
  kind: string,
  id: string,
  budget: number | null,
): Promise<ApiResult<{ ok?: boolean; budget?: number | null }>> {
  return request(
    "PUT",
    `/api/circles/thread-context/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
    { budget },
  );
}

/** Set (or clear, null) what one turn on this thread may spend, in dollars.
 * Sent on its own: the endpoint keys each field on presence, so this leaves
 * the context budget exactly as it was. */
export function putThreadTurnAllowance(
  kind: string,
  id: string,
  usd: number | null,
): Promise<ApiResult<{ ok?: boolean; turn_allowance_usd?: number | null }>> {
  return request(
    "PUT",
    `/api/circles/thread-context/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
    { turn_allowance_usd: usd },
  );
}

/** One page of a thread. Peers (entity) and People (dm) threads are read a
 * page at a time, newest first: `total` is every message in the thread and
 * `next_before` (when set) reads the page before this one. */
export interface ThreadPage {
  messages: ThreadMessageRaw[];
  total?: number;
  next_before?: string | null;
}

export function fetchThread(
  kind: string,
  id: string,
  before?: string | null,
): Promise<ApiResult<ThreadPage>> {
  const q = before ? `?before=${encodeURIComponent(before)}` : "";
  return getJSON<ThreadPage>(
    `/api/circles/thread/${encodeURIComponent(kind)}/${encodeURIComponent(id)}${q}`,
  );
}

export function sendMessageStream(
  target: { kind: string; id: string },
  body: string,
  signal: AbortSignal,
  opts?: {
    model?: ModelPick | null;
    attachments?: AttachmentBlock[];
    pills?: string[];
    /** The Circles thread id ("original" | uuid) — sent only for target_kind
     * "self" so the turn lands in the right scope. */
    threadId?: string | null;
    /** A turn type picked in the dropdown: the message goes
     * to that type's thread. Omitted = the platform decides (bound thread,
     * else the scorer, else it stays). */
    turnTypeId?: string | null;
    /** The thread's reasoning-effort pick, beside the model pick:
     * sent only when picked, so the route's own effort (or the model's
     * default) runs otherwise. Checked again by the send route. */
    effort?: string | null;
    /** Ask Ivy: one message of a person's chat handed to
     * this turn. The server reads it itself; only its id goes. Self only. */
    carry?: CarryRef | null;
    /** The words' id (self only; 1-64 of A-Z a-z 0-9 _ -): a second send
     * under it delivers nothing new. */
    clientId?: string;
    /** One conversation (self only): the Host picks the topic for this
     * message; `threadId` is only the topic the person was last in. */
    pickTopic?: boolean;
    /** One conversation (self only): the topic was picked already (POST
     * /api/circles/topic, while a turn ran); `threadId` is that topic. */
    topicPicked?: boolean;
  },
): Promise<Response> {
  const payload: Record<string, unknown> = {
    target_kind: target.kind,
    target_id: target.id,
    body,
  };
  if (target.kind === "self" && opts?.threadId) {
    payload.thread_id = opts.threadId;
  }
  if (target.kind === "self" && opts?.turnTypeId) {
    payload.turn_type_id = opts.turnTypeId;
  }
  // Per-thread model override (honored for the self-thread; ignored elsewhere by
  // the backend). Only sent when picked so default routing is untouched.
  if (opts?.model) payload.model = opts.model;
  // Its effort pick rides the same way (self-thread only).
  if (target.kind === "self" && opts?.effort) payload.effort = opts.effort;
  if (target.kind === "self" && opts?.clientId) payload.client_id = opts.clientId;
  if (target.kind === "self" && opts?.pickTopic) payload.pick_topic = true;
  if (target.kind === "self" && opts?.topicPicked) payload.topic_picked = true;
  if (target.kind === "self" && opts?.carry) {
    payload.carry = { kind: opts.carry.kind, ref: opts.carry.ref, message_id: opts.carry.message_id };
  }
  // Workbench-format content blocks (up to 20, <=30MB each). Only sent when present.
  if (opts?.attachments && opts.attachments.length > 0) {
    payload.attachments = opts.attachments;
  }
  // The FRAME: what only this surface knows about the turn — the pills the
  // user attached from an app. The backend validates the keys
  // (runtime/frame.py) and renders them into the host's "This Turn" block.
  if (opts?.pills && opts.pills.length > 0) {
    payload.frame = { pills: opts.pills };
  }
  return fetch("/api/circles/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/x-vercel-ai-data-stream",
    },
    body: JSON.stringify(payload),
    signal,
  });
}

// ---- One conversation ----
// Circles opens on a blank page; earlier messages are read a page at a time
// across every topic (GET /api/circles/feed), and the Host picks the topic of
// each message (pick_topic on the send, or POST /api/circles/topic first
// while a turn runs).

export interface FeedCursor {
  before: string;
  before_id: number;
}

export interface FeedPage {
  messages: ThreadMessageRaw[];
  next: FeedCursor | null;
}

/** One page of the one conversation, oldest first; no cursor = the newest. */
export function fetchFeed(cursor?: FeedCursor | null): Promise<ApiResult<FeedPage>> {
  return getJSON<FeedPage>(
    `/api/circles/feed${qs({ before: cursor?.before, before_id: cursor?.before_id })}`,
  );
}

/** Where the Host puts a message (routes/circles.py _topic_json). */
export interface TopicPick {
  thread_id: string;
  title: string;
  new: boolean;
  moved: boolean;
  marker: string;
  picked: boolean;
  root: boolean;
  follow_on?: boolean;
}

/** Ask the Host which topic `body` belongs to, before sending it while a
 * turn runs. `threadId`: the topic the person was last in, if any. */
export function pickTopic(body: string, threadId?: string | null): Promise<ApiResult<TopicPick>> {
  return request<TopicPick>("POST", "/api/circles/topic", threadId ? { body, thread_id: threadId } : { body });
}

// ---- Detached turns (GET/POST /api/circles/turns/*) ----
// The turn registry surface: see the running turn, reattach to its stream
// after a disconnect (phone sleep, tab switch), and stop it for real.

/** `thread` scopes the lookup to one Circles thread ("original" | uuid) so a
 * running turn on one thread never surfaces as active on another. Omitted
 * for non-self conversations, which have no thread of their own. */
export function fetchActiveTurn(
  thread?: string | null,
): Promise<ApiResult<{ ok: boolean; turn: ActiveTurn | null; turns: ActiveTurn[] }>> {
  return getJSON<{ ok: boolean; turn: ActiveTurn | null; turns: ActiveTurn[] }>(
    `/api/circles/turns/active${qs({ thread })}`,
  );
}

/** Reattach to a turn's frame stream. Replays buffered frames from
 * `fromSeq` (0 = the whole turn) then follows live — same wire format as
 * sendMessageStream, so the same consumer parses it. */
export function openTurnStream(
  turnId: string,
  fromSeq: number,
  signal: AbortSignal,
): Promise<Response> {
  return fetch(
    `/api/circles/turns/${encodeURIComponent(turnId)}/stream?from_seq=${fromSeq}`,
    { headers: { Accept: "text/x-vercel-ai-data-stream" }, signal },
  );
}

/** The real stop button: cancels the DETACHED turn server-side (tears down
 * the engine and its subprocess), not just the local fetch. */
export function cancelTurn(
  turnId: string,
): Promise<ApiResult<{ ok: boolean; status: string }>> {
  return request(
    "POST",
    `/api/circles/turns/${encodeURIComponent(turnId)}/cancel`,
  );
}

// ---- Delete / clear a conversation (DELETE /api/circles/thread/{kind}/{ref}) ----
// Soft, no hard data loss: self-thread turns flip to status='archived';
// entity DM rows get inbox_messages.archived_at set. Returns the cleared count.
export function deleteConversation(
  kind: string,
  ref: string,
): Promise<ApiResult<{ ok: boolean; cleared: number }>> {
  return request(
    "DELETE",
    `/api/circles/thread/${encodeURIComponent(kind)}/${encodeURIComponent(ref)}`,
  );
}

// ============================================================================
// Circles threads (server/routes/circles.py /threads)
// ============================================================================

/** The "original" entry first, then newest first. */
/** The entity's ACTIVE turn types (server/routes/turn_types.py) — the
 * dropdown's list. Empty for a Host whose user has built none. */
export function listTurnTypes(): Promise<ApiResult<{ types: TurnType[] }>> {
  return getJSON<{ types: TurnType[] }>("/api/turn-types");
}

export function createThread(
  title?: string,
): Promise<ApiResult<{ thread: Thread }>> {
  return request("POST", "/api/circles/threads", title ? { title } : {});
}

export function patchThread(
  id: string,
  body: { title?: string; closed?: boolean },
): Promise<ApiResult<{ thread: Thread }>> {
  return request(
    "PATCH",
    `/api/circles/threads/${encodeURIComponent(id)}`,
    body,
  );
}

/** Delete a thread: its messages and everything the Host learned in it leave
 * the Host's memory (archived, not erased), the wake-ups set in it are
 * cancelled, and it leaves the list (DELETE /api/circles/threads/{id}). The
 * original conversation cannot be deleted — only cleared. */
export function deleteThread(
  id: string,
): Promise<
  ApiResult<{
    ok: boolean;
    deleted: { id: string; messages: number; learned: number; cancelled: number };
  }>
> {
  return request("DELETE", `/api/circles/threads/${encodeURIComponent(id)}`);
}

/** A scheduled task set in a thread that has not finished — what deleting
 * the thread cancels. */
export interface ThreadWakeup {
  id: number;
  title: string;
  fire_at: string | null;
  recurrence: string | null;
}

export function listThreadWakeups(
  threadId: string,
): Promise<ApiResult<{ wakeups: ThreadWakeup[] }>> {
  return getJSON<{ wakeups: ThreadWakeup[] }>(
    `/api/circles/threads/${encodeURIComponent(threadId)}/wakeups`,
  );
}

export function listMarkers(
  threadId: string,
): Promise<ApiResult<{ markers: Marker[] }>> {
  return getJSON<{ markers: Marker[] }>(
    `/api/circles/threads/${encodeURIComponent(threadId)}/markers`,
  );
}

/** Split from a marker: rows from `from_turn_id` onward move to a new thread
 * (origin='split', parent_thread_id = threadId). Returns the new thread. */
export function splitThread(
  threadId: string,
  body: { from_turn_id: number; title?: string },
): Promise<ApiResult<{ thread: Thread }>> {
  return request(
    "POST",
    `/api/circles/threads/${encodeURIComponent(threadId)}/split`,
    body,
  );
}

// ============================================================================
// Model catalog + per-turn model actions
// ============================================================================

interface CatalogRawModel {
  id: string;
  name?: string;
  input_per_token?: string | number | null;
  output_per_token?: string | number | null;
  context_length?: number | null;
}
interface CatalogRaw {
  local_models?: CatalogRawModel[];
  openrouter_models?: CatalogRawModel[];
}

// Per-token price (string or number from the catalog) -> USD per 1M tokens.
function perMillion(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n * 1_000_000 : null;
}

// Full catalog flattener (local Ollama first, then the platform 'privatae' lane).
// Used as the NAME source for resolving favorite ids; not shown wholesale in the
// in-chat picker (that would dump all ~339 OpenRouter models).
export async function fetchModelCatalog(): Promise<ApiResult<CatalogModel[]>> {
  const r = await getJSON<CatalogRaw>("/api/inference/catalog");
  if (!r.ok) return r;
  const local = r.data.local_models ?? [];
  const remote = r.data.openrouter_models ?? [];
  const models: CatalogModel[] = [
    ...local.map((m) => ({
      id: m.id,
      name: `${m.name || m.id} (local)`,
      provider: "local",
      inputPerM: perMillion(m.input_per_token),
      outputPerM: perMillion(m.output_per_token),
      contextLength: m.context_length ?? null,
    })),
    ...remote.map((m) => ({
      id: m.id,
      name: m.name || m.id,
      provider: "privatae",
      inputPerM: perMillion(m.input_per_token),
      outputPerM: perMillion(m.output_per_token),
      contextLength: m.context_length ?? null,
    })),
  ];
  return { ok: true, data: models };
}

// The reasoning efforts a turn may run at: the levels of the model the
// turn runs on (the picked one, else the Host model route) and the level that
// runs when none is picked (server/routes/inference.py effort_options).
export function fetchEffortOptions(pick: ModelPick | null): Promise<ApiResult<EffortOptions>> {
  const q = pick
    ? `?provider=${encodeURIComponent(pick.provider)}&model=${encodeURIComponent(pick.model_id)}`
    : "";
  return getJSON<EffortOptions>(`/api/inference/effort-options${q}`);
}

// The host's FAVORITE model ids (set in the Privatae app). The in-chat picker
// shows the FULL searchable catalog but surfaces these in a "Favorites" group at
// the top so the common picks are one click away. Empty list is fine — the
// picker still shows the full catalog (searchable) and "Default model".
export async function fetchFavoriteIds(): Promise<string[]> {
  const r = await getJSON<{ favorites?: string[] }>("/api/inference/favorites");
  return r.ok ? r.data.favorites ?? [] : [];
}

// Seal a credential the composer caught into the vault (POST /api/vault/secrets,
// write:vault). Same call the Keys app makes; a same-name entry is superseded.
export function storeVaultSecret(
  name: string,
  credType: "api_key" | "opaque",
  value: string,
  // true = refuse (409) rather than supersede a key that already has this
  // name. The ticket card uses it; the composer's "Store in Keys" does not.
  ifAbsent = false,
): Promise<ApiResult<{ ok: boolean; id: string; name: string }>> {
  const bytes = new TextEncoder().encode(value);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return request("POST", "/api/vault/secrets", {
    name,
    cred_type: credType,
    value_b64: btoa(bin),
    if_absent: ifAbsent,
  });
}

// Mid-stream nudge into the live tool loop (POST /api/circles/nudge).
// `clientId` is the sender's id for the words (1-64 of A-Z a-z 0-9 _ -): the
// turn's nudge_read receipt and the saved row carry it back.
// `held`: the server has written the words down (pending_words) and owns
// them from here — the turn reads them, or they start the thread's next turn,
// after a restart too — so the page keeps no copy to send again.
export function nudgeSession(
  sessionId: string,
  text: string,
  clientId?: string,
): Promise<ApiResult<{ ok: boolean; session_id: string; queued_chars: number; held?: boolean; own_turn?: boolean }>> {
  return request("POST", "/api/circles/nudge", {
    session_id: sessionId,
    text,
    ...(clientId ? { client_id: clientId } : {}),
  });
}

/** Words the server holds for a page of your Host (GET /api/circles/pending):
 * sent while a turn ran and not yet read by a turn. Shown as sent and
 * waiting, after a reload too. */
export interface HeldWords {
  id: string;
  text: string;
  client_id: string | null;
  received_at: string | null;
}
export function fetchPendingWords(thread?: string | null): Promise<ApiResult<{ ok: boolean; words: HeldWords[] }>> {
  return getJSON<{ ok: boolean; words: HeldWords[] }>(`/api/circles/pending${qs({ thread })}`);
}

// ============================================================================
// Reviews / tickets
// ============================================================================

export function listTickets(status?: string): Promise<ApiResult<{ tickets: Ticket[] }>> {
  return getJSON<{ tickets: Ticket[] }>(`/api/tickets${qs({ status })}`);
}

/** The open tickets of one conversation: `room` is
 * thread:<uuid> | thread:original (app-web-kit/ask.ts circlesRoom). The chat
 * raises each as its card. */
export function listRoomTickets(room: string): Promise<ApiResult<{ tickets: Ticket[] }>> {
  return getJSON<{ tickets: Ticket[] }>(`/api/tickets${qs({ status: "open", asked_in: room, limit: 50 })}`);
}

// ---- The pairing code card (read:topology) ----

/** The one-time code of an open connector pairing — this window only; the
 * route refuses any app that is not the person's own (routes/network.py). */
export function fetchPairingCode(
  grantId: number,
): Promise<ApiResult<{ code: string; seconds_left: number; platform?: string }>> {
  return getJSON(`/api/network/nac/connector/${grantId}/code`);
}

/** The connector grants (no code): the chat finds the open pairing it started. */
export function listConnectors(): Promise<ApiResult<{ connectors: unknown[] }>> {
  return getJSON<{ connectors: unknown[] }>("/api/network/nac/connector");
}

// ---- Where Circles reopens (server/ui_last_open.py) ----

export function getLastOpenThread(): Promise<ApiResult<{ ref: string | null }>> {
  return getJSON<{ ref: string | null }>("/api/circles/last-open");
}

export function putLastOpenThread(ref: string): Promise<ApiResult<{ ref: string }>> {
  return request("PUT", "/api/circles/last-open", { ref });
}

export function getTicket(id: number): Promise<ApiResult<Ticket>> {
  return getJSON<Ticket>(`/api/tickets/${id}`);
}

/** What the kernel did with the answer: spoke it into the turn that asked
 * (`answered_into_turn`), or — that turn having ended — started a turn with
 * it in the thread it asked in (`answer_turn_started`, one server path for
 * every surface). */
export interface DecideResult {
  ticket_id: number;
  pattern_id: number | null;
  answered_into_turn?: boolean;
  answer_turn_started?: boolean;
}

export function decideTicket(
  id: number,
  optionId: string,
  andSimilar: boolean,
  note = "",
  seenEventMax: number | null = null,
  stepupCode: string | null = null,
): Promise<ApiResult<DecideResult>> {
  // `note` is the person's own words on the decision — for a one-option
  // ("done") ticket it is the place to write what they did. The route has
  // always accepted it; the pane just never sent one.
  // `seenEventMax` (a support card from another Host): the
  // platform event mark the card was RENDERED with. The Host refreshes such
  // a card while it sits open (the user wrote again); the decide applies
  // only if the card still shows what the person read, else 409 and the
  // pane reloads it.
  const body: Record<string, unknown> = { option_id: optionId, and_similar: andSimilar, note };
  if (typeof seenEventMax === "number") body.seen_event_max = seenEventMax;
  // A pipeline change adding a check that runs outside the platform: the
  // code from the person's authenticator, checked and burned by the kernel.
  if (stepupCode) body.stepup_code = stepupCode;
  return request("POST", `/api/tickets/${id}/decide`, body);
}

export function dismissTicket(id: number): Promise<ApiResult<{ ticket_id: number }>> {
  return request("POST", `/api/tickets/${id}/dismiss`, {});
}

// ============================================================================
// Support tickets (the platform-help half of the Tickets pane)
// ============================================================================

export function listSupport(params: {
  status?: string;
  category?: string;
  refresh?: boolean;
} = {}): Promise<ApiResult<{ tickets: SupportTicket[] }>> {
  return getJSON<{ tickets: SupportTicket[] }>(
    `/api/support${qs({
      status: params.status,
      category: params.category,
      refresh: params.refresh === false ? "false" : undefined,
    })}`,
  );
}

export function supportBadgeCounts(): Promise<ApiResult<SupportBadges>> {
  return getJSON<SupportBadges>("/api/support/badge-counts");
}

export function getSupport(id: number): Promise<ApiResult<SupportTicket>> {
  return getJSON<SupportTicket>(`/api/support/${id}`);
}

export function openSupport(body: {
  category: SupportCategory;
  title: string;
  problem: string;
  context?: string;
  project_id?: number | null;
  severity?: string;
}): Promise<ApiResult<{ ticket_id: number; status: string; shared_items: number }>> {
  return request("POST", "/api/support", body);
}

export function setSupportShared(
  id: number,
  included: Record<string, boolean>,
): Promise<ApiResult<{ ticket_id: number; shared_context: SharedItem[] }>> {
  return request("POST", `/api/support/${id}/shared`, { included });
}

// `revision` is the draft version on the person's screen (GET /api/support/{id}
// returns it). The Host can rewrite a draft while it is open here; the server
// refuses a Send naming an older version (409), so nothing unseen is sent.
export function sendSupport(
  id: number,
  revision: number,
): Promise<ApiResult<{ ticket_id: number; platform_id: number; platform_status: string }>> {
  return request("POST", `/api/support/${id}/send`, { revision }, 60000);
}

export function discardSupport(id: number): Promise<ApiResult<{ ticket_id: number }>> {
  return request("POST", `/api/support/${id}/discard`, {});
}

export function closeSupport(id: number): Promise<ApiResult<{ ticket_id: number }>> {
  return request("POST", `/api/support/${id}/close`, {});
}

export function replySupport(
  id: number,
  body: string,
): Promise<ApiResult<{ ticket_id: number; thread_len: number }>> {
  return request("POST", `/api/support/${id}/reply`, { body });
}

// ============================================================================
// Contacts + phone alias
// ============================================================================

export function listContacts(): Promise<ApiResult<{ contacts: Contact[]; count: number }>> {
  return getJSON<{ contacts: Contact[]; count: number }>("/api/contacts/list");
}

export function createContact(body: {
  handle: string;
  display_name: string | null;
  kind: string;
}): Promise<ApiResult<{ contact: Contact }>> {
  return request("POST", "/api/contacts", body);
}

export function updateContact(
  id: number,
  body: { display_name?: string; notes?: string; status?: string },
): Promise<ApiResult<{ contact: Contact }>> {
  return request("PATCH", `/api/contacts/${id}`, body);
}

export function deleteContact(id: number): Promise<ApiResult<{ deleted: number }>> {
  return request("DELETE", `/api/contacts/${id}`);
}

export function requestByPhone(
  alias: string,
): Promise<ApiResult<{ sent_to: string; message_id: number | null }>> {
  return request("POST", "/api/contacts/request-by-phone", { alias });
}

export function getMyAlias(): Promise<ApiResult<{ alias: string | null }>> {
  return getJSON<{ alias: string | null }>("/api/phone-alias");
}

export function setMyAlias(alias: string): Promise<ApiResult<{ alias: string }>> {
  return request("PUT", "/api/phone-alias", { alias });
}

export function lookupAlias(alias: string): Promise<
  ApiResult<{ alias: string; found: boolean; entity_id?: number; display_name?: string | null }>
> {
  return request("POST", "/api/phone-alias/lookup", { alias });
}

// ============================================================================
// Domain policy
// ============================================================================

export function getPolicy(): Promise<
  ApiResult<{ posture: Posture; rules: PolicyRule[] }>
> {
  return getJSON<{ posture: Posture; rules: PolicyRule[] }>("/api/domain-policy");
}

export function setPosture(posture: Posture): Promise<ApiResult<{ posture: Posture }>> {
  return request("PUT", "/api/domain-policy/posture", { posture });
}

export function addPolicyRule(body: {
  peer: string;
  allow_inbox: boolean;
  allow_dm: boolean;
  allow_circle_invite: boolean;
  note?: string;
}): Promise<ApiResult<{ rule: PolicyRule }>> {
  return request("POST", "/api/domain-policy/rules", body);
}

export function deletePolicyRule(id: number): Promise<ApiResult<{ deleted: number }>> {
  return request("DELETE", `/api/domain-policy/rules/${id}`);
}

// ============================================================================
// Inbox
// ============================================================================

export function listInbox(opts: {
  unread?: boolean;
  kind?: string;
  limit?: number;
}): Promise<ApiResult<{ messages: InboxMessage[]; count: number; unread?: number }>> {
  return getJSON<{ messages: InboxMessage[]; count: number; unread?: number }>(
    `/api/inbox/list${qs({
      unread: opts.unread ? 1 : undefined,
      kind: opts.kind,
      limit: opts.limit ?? 100,
    })}`,
  );
}

// GET with a server-side mark-read side effect.
export function readInbox(id: string): Promise<ApiResult<InboxMessage>> {
  return getJSON<InboxMessage>(`/api/inbox/read/${encodeURIComponent(id)}`);
}

export function archiveInbox(
  id: string,
): Promise<ApiResult<{ message_id: string; archived: boolean }>> {
  return request("POST", `/api/inbox/archive/${encodeURIComponent(id)}`, {});
}

// ── Sharing a machine's models with another host ───────────────────────────
// The inbox card for a `machine_request` reads the offer row (which carries the
// id an accept needs), the user's own machines (to pick one and its models),
// and then answers. Accepting WIDENS — another host gets a lane on a machine
// the user owns — so it takes their step-up code, exactly like approving a
// pairing. Declining writes nothing anywhere near the machine.

export type MachineOffer = {
  id: number;
  direction: "in" | "out";
  peer_entity_id: number;
  peer_handle: string | null;
  peer_display: string | null;
  purpose: string | null;
  models_asked: string[];
  state: string;
  server: string | null;
  granted_models: string[] | null;
};

export type ShareableBastion = {
  hostname: string | null;
  models: string[];
  online: boolean;
  anchor: { anchor_host?: string } | null;
};

export function listMachineOffers(
  direction?: "in" | "out",
): Promise<ApiResult<{ offers: MachineOffer[] }>> {
  const q = direction ? `?direction=${direction}` : "";
  return request("GET", `/api/network/bastions/machine-offers${q}`);
}

export function listBastions(): Promise<ApiResult<{ bastions: ShareableBastion[] }>> {
  return request("GET", "/api/network/bastions");
}

// ============================================================================
// Bastions folder (server/network/bastion_directory.py)
// ============================================================================
// The machines this Host is paired to, and the other Hosts each one announced.
// Every field is the machine's own report. `null` means the machine never
// said (a bastion too old to report it), which is NOT the same as "none".

export type PairingState = "active" | "pending" | "revoked";

export type MachineHost = {
  host_name: string;
  handle: string;
  state: PairingState;
  since: string | null;
  /** Null until peer discovery resolves the name to a Host. */
  host_id: number | null;
  /** The machine is still announcing this Host. A request needs it. */
  current: boolean;
  is_anchor: boolean;
};

export type MachineAnchor = {
  host_name: string;
  handle: string;
  /** Typed at the machine by whoever anchored it; never verified. */
  owner: string | null;
  /** Whether the anchor claim's signature checked out. */
  verified: boolean;
  /** The machine says THIS Host is its anchor. Null when its report was
   * too large to keep whole, so the answer is not known. */
  is_me: boolean | null;
};

export type BastionMachine = {
  token_id: number;
  machine: string;
  online: boolean;
  /** False until the machine sends its first heartbeat. A pairing waiting
   * for the anchor sends none until it is approved. */
  heard: boolean;
  last_seen_at: string | null;
  mode: "local" | "networked" | null;
  anchored: boolean | null;
  anchor: MachineAnchor | null;
  hosts: MachineHost[] | null;
};

export function listBastionDirectory(): Promise<ApiResult<{ machines: BastionMachine[] }>> {
  return getJSON<{ machines: BastionMachine[] }>("/api/circles/bastions");
}

// `models` is required, and deliberately: an approval naming none is an
// approval to use EVERY model on that machine, which is not what was asked for.
export function acceptMachineOffer(body: {
  offer_id: number;
  server: string;
  models: string[];
  stepup_code: string;
}): Promise<ApiResult<{ accepted: boolean; subject: string; may_use: string }>> {
  return request("POST", "/api/network/bastions/machine-offers/accept", body);
}

export function declineMachineOffer(
  offer_id: number,
): Promise<ApiResult<{ declined: boolean; note: string }>> {
  return request("POST", "/api/network/bastions/machine-offers/decline", { offer_id });
}

// ── Rendezvous: interest tags, and the introductions they produce ──────────
// A list of bare strings is the ONLY thing that leaves the pod — no memory,
// no profile, no summary of either — and the platform batches once an hour,
// introducing every host that listed a tag at least two hosts listed.

export type RendezvousInterest = {
  tag: string;
  /** When the platform last acknowledged the whole list. Null = not listed. */
  listed_at?: string | null;
  /** Why the last publish did not land. `publish` stamps it on every live row
   *  when the bridge is unreachable (server/groups/rendezvous.py:294-297). */
  last_error?: string | null;
  created_at?: string | null;
  withdrawn_at?: string | null;
};

export function listInterests(
  includeWithdrawn = false,
): Promise<ApiResult<{ interests: RendezvousInterest[] }>> {
  return getJSON<{ interests: RendezvousInterest[] }>(
    `/api/groups/rendezvous${qs({ include_withdrawn: includeWithdrawn ? "true" : "" })}`,
  );
}

// The POST is STORE-THEN-PUBLISH and the answer reports each half separately
// (server/routes/groups.py:194-209): `ok`/`listed` false with `error` set means
// the tags are saved on this Host but the platform does not have them yet.
// Rendering that as success would be a lie the user acts on.
//
// `dropped` carries back anything that normalised to nothing — a 70-character
// tag, or one made entirely of punctuation (rendezvous.py:153-158). When even
// one other tag survives the call SUCCEEDS, so a UI that does not show this
// field tells the user "listed" while one of their interests is simply absent.
export type RendezvousListed = {
  /** The stored, normalised, sorted set. */
  tags: string[];
  dropped: string[];
  ok: boolean;
  listed: boolean;
  seq?: number;
  error?: string;
  platform?: unknown;
};

export function setInterests(tags: string[]): Promise<ApiResult<RendezvousListed>> {
  return request("POST", "/api/groups/rendezvous", { tags });
}

/** Withdraw one tag, or every tag when none is named. `ok` is the PLATFORM
 *  half — the local withdrawal has already happened either way. */
export function withdrawInterest(
  tag?: string,
): Promise<
  ApiResult<{
    withdrawn: string;
    rows?: string | number;
    ok: boolean;
    tag: string | null;
    all: boolean;
    platform?: unknown;
  }>
> {
  return request("DELETE", `/api/groups/rendezvous${qs({ tag })}`);
}

// Connect with a PERSON by their account email (Circles › People). Never says
// whether the address exists — the answer is their acceptance.
export function requestByEmail(
  email: string,
  note?: string,
): Promise<ApiResult<{ ok: boolean; sent_to: string }>> {
  return request("POST", "/api/contacts/request-by-email", { email, note });
}

// ============================================================================
// Mapping raw history rows -> ChatMessage
// ============================================================================

// The shape history-merge.isLocalEcho recognises (`u-<ms>-<n>`, the
// Workbench's too). The old `u-<base36>` never matched it, so the merge took
// every live echo for a saved row and kept or dropped it by comparing the
// browser's clock with the server's.
let _seq = 0;
export function localId(prefix: string): string {
  _seq += 1;
  return `${prefix}-${Date.now()}-${_seq}`;
}

function toolStateFrom(status: string | undefined, hasResult: boolean): ToolState {
  if (status === "error" || status === "denied" || status === "blocked") {
    return "output-error";
  }
  return hasResult ? "output-available" : "input-available";
}

export function mapToolCall(raw: ToolCallRaw): ToolInvocation {
  const args = raw.args ?? raw.input ?? {};
  // Persisted chips (session.py _tc_meta) carry {tool, args_summary, status,
  // error, kind?, task_id?} — before this read, replayed cards fell back to
  // the literal name "tool" with no summary.
  const name = raw.toolName || raw.name || raw.tool || "tool";
  const result =
    raw.result ?? raw.output ?? (raw.error ? { status: "error", result: raw.error } : undefined);
  const hasResult = result !== undefined && result !== null;
  return {
    id: raw.toolCallId || raw.id || localId("tc"),
    name,
    args,
    summary: raw.args_summary || "",
    verb: (raw.op || "").replace(/[_.]/g, " ").toLowerCase(),
    result: hasResult ? result : undefined,
    state: toolStateFrom(raw.status, hasResult),
    kind: raw.kind === "agent" || name === "dispatch" ? "agent" : "tool",
    taskId: raw.task_id != null ? String(raw.task_id) : undefined,
    operateTool: typeof raw.operate_tool === "string" ? raw.operate_tool : undefined,
    digest: raw.digest && typeof raw.digest === "object" ? raw.digest : undefined,
  };
}

export function mapAgentAttribution(raw: AgentRaw | undefined) {
  if (!raw) return undefined;
  return {
    taskId: raw.task_id != null ? String(raw.task_id) : undefined,
    title: raw.title,
    status: raw.status,
    costUsd: raw.cost_usd,
  };
}

/** `ownThread`: these rows are the SELF thread (the caller knows the target
 * kind; nothing in a row does). Only then is an assistant row marked as the
 * user's own Host's turn — what lets its visuals run and its artifact
 * chips show (app-web-kit/visuals isOwnHostTurn). */
export function mapThreadMessages(rows: ThreadMessageRaw[], ownThread = false): ChatMessage[] {
  const keys = historyKeys(
    rows.map((m) => ({ createdAt: m.created_at, author: m.author_name, text: m.body })),
  );
  return rows.map((m, i) => {
    const mine = m.author_kind === "self" || m.author_kind === "user";
    // Peers/People threads name the writer (a person or a host) and, for a
    // person, the host that carried it. The self thread never does.
    const named = m.author_kind === "person" || m.author_kind === "entity";
    return {
      // The self-thread carries the entity_memory turn id (a number over JSON);
      // coerce to string so the id is uniform and the Regenerate/Compare gate
      // (which inspects the id) is safe. Falls back to a synthetic id.
      // A row with no id (the circle branch of get_thread sends none) gets a
      // key built from the row (history-merge.historyKeys).
      id: m.id != null ? String(m.id) : keys[i],
      role: mine ? "user" : "assistant",
      text: m.body ?? "",
      tools: Array.isArray(m.tool_calls) ? m.tool_calls.map(mapToolCall) : [],
      createdAt: m.created_at ?? new Date(0).toISOString(),
      metrics: m.metrics ?? undefined,
      agent: mapAgentAttribution(m.agent),
      turnToken: m.turn_token || undefined,
      nudge: m.nudge === true ? true : undefined,
      carry: m.carry && typeof m.carry === "object" ? m.carry : undefined,
      readingNotice: m.reading_notice === "on" || m.reading_notice === "off" ? m.reading_notice : undefined,
      topic: m.feed_topic && m.feed_topic.id ? m.feed_topic : undefined,
      topicMarker: m.topic_marker === true ? true : undefined,
      author: named ? m.author_name : undefined,
      via: m.via ?? undefined,
      claimedPerson: m.claimed_person || undefined,
      authorKind:
        m.author_kind === "person" || m.author_kind === "entity" || m.author_kind === "self"
          ? m.author_kind
          : undefined,
      ownHost: ownThread && !mine ? true : undefined,
    };
  });
}

/** Send to a peer host or a person (Peers / People threads): a plain JSON
 * round-trip — no model turn, no stream. The reply is the other side's, when
 * they answer; the pane reloads the thread on the inbox push. */
export function sendPlainMessage(
  target: { kind: string; id: string },
  body: string,
): Promise<ApiResult<{ ok: boolean; delivered?: boolean }>> {
  return request("POST", "/api/circles/messages", {
    target_kind: target.kind,
    target_id: target.id,
    body,
  });
}

// ── Coding Engines (Circles → Settings, READ-ONLY) ─────────────────────
// The engines' saved defaults (behavioral_params.coding_harnesses — one entry
// per engine with a saved model/effort; NOT a selection) and
// the Default tick (behavioral_params.default_coding_harness) are DECIDED in
// the Orchestrator app (the single orchestration surface);
// Circles only reads them to show what a build runs on.
export interface HarnessOption {
  id: string;
  label: string;
  icon_url?: string;
}
export interface CodingHarness {
  id: string;
  model?: string | null;
  effort?: string | null;
}

// ── Background activity ────────────────────────────────────────────────
// Model calls the Host made that the user never saw — memory consolidation,
// research, summarising. The visible chat turn is excluded server-side (it
// has its own status line). `ok:false` rows are failures the user should be
// told about, not swallowed.
export interface BackgroundActivity {
  id: number;
  at: string | null;
  group: string;
  detail: string;
  source_module: string;
  model: string | null;
  provider: string | null;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  ok: boolean;
  error: string | null;
  error_class: string | null;
  failed_over_to?: string | null;
}
export interface ActivityPayload {
  activity: BackgroundActivity[];
  issues: number;
  window_hours: number;
}
/** `failed`: only the failures in the window, newest first, however many
 * calls went well after them (the route's `failed=true`). A server without
 * the option answers with every call, as before. */
export async function fetchBackgroundActivity(
  limit = 20,
  hours = 24,
  failed = false,
): Promise<ApiResult<ActivityPayload>> {
  return getJSON<ActivityPayload>(
    `/api/inference/activity${qs({ limit, hours, failed: failed ? "true" : undefined })}`,
  );
}
